"""Privacy-preserving reader for numeric instruction usage in Codex rollouts."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any


CODEX_SESSIONS_DIR_ENV = "CODEX_SESSIONS_DIR"
DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
_MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)
_THREAD_SAFE_ID_DOMAIN = b"codex-token-monitor:thread-safe-id:v1\0"
_RESPONSE_SAFE_ID_DOMAIN = b"codex-token-monitor:response-safe-id:v1\0"
_FILE_SAFE_ID_DOMAIN = b"codex-token-monitor:rollout-file-safe-id:v1\0"


def _is_safe_hash_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def make_thread_safe_id(thread_id: object) -> str | None:
    """Return a stable content-free identity for one Codex Thread."""

    if not isinstance(thread_id, str) or not thread_id:
        return None
    digest = hashlib.sha256(
        _THREAD_SAFE_ID_DOMAIN + thread_id.encode("utf-8"),
    ).hexdigest()
    return f"sha256:{digest}"


def make_response_safe_id(thread_id: object, turn_id: object) -> str | None:
    """Return a content-free stable identity for one response."""

    if not isinstance(thread_id, str) or not thread_id:
        return None
    if not isinstance(turn_id, str) or not turn_id:
        return None
    encoded = json.dumps(
        [thread_id, turn_id], separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    digest = hashlib.sha256(_RESPONSE_SAFE_ID_DOMAIN + encoded).hexdigest()
    return f"sha256:{digest}"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int

    @property
    def non_cached_input_tokens(self) -> int:
        return self.input_tokens - self.cached_input_tokens

    @property
    def non_reasoning_output_tokens(self) -> int:
        return self.output_tokens - self.reasoning_output_tokens

    def minus(self, previous: "TokenUsage") -> "TokenUsage":
        return TokenUsage(*(a - b for a, b in zip(self.values(), previous.values())))

    def values(self) -> tuple[int, int, int, int, int]:
        return (self.input_tokens, self.cached_input_tokens, self.output_tokens, self.reasoning_output_tokens, self.total_tokens)


@dataclass(frozen=True)
class ModelCallUsage:
    usage: TokenUsage
    epoch: int


@dataclass(frozen=True)
class InstructionUsage:
    turn_id: str
    status: str
    usage: TokenUsage | None
    model_calls: int
    duration_ms: int | None
    duplicate_snapshots: int
    rejected_events: int
    unreconciled_events: int
    exact: bool
    in_progress: bool


@dataclass(frozen=True)
class ResponseUsageCandidate:
    """Content-free terminal response usage discovered in one Rollout."""

    thread_safe_id: str
    response_safe_id: str
    status: str
    completion_time_utc: datetime | None
    observation_time_utc: datetime | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    safe_model_id: str | None = None
    trusted_call_count: int = 0
    integrity_status: str = "partial"
    safe_diagnostic_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _is_safe_hash_id(self.thread_safe_id):
            raise ValueError("thread_safe_id_invalid")
        if not _is_safe_hash_id(self.response_safe_id):
            raise ValueError("response_safe_id_invalid")
        if self.status not in {"exact", "completed_partial"}:
            raise ValueError("response_status_invalid")
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "trusted_call_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name}_invalid")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens_exceed_input")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning_tokens_exceed_output")
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens_inconsistent")


@dataclass(frozen=True)
class SafeRolloutScanMetadata:
    safe_file_hash: str
    size: int
    mtime_ns: int
    completed_response_count: int
    bytes_scanned: int
    result_status: str = "success"


@dataclass(frozen=True)
class CompletedResponseUsageBatch:
    responses: tuple[ResponseUsageCandidate, ...]
    scan_metadata: SafeRolloutScanMetadata


class RolloutScanInterrupted(RuntimeError):
    """Safe control-flow signal for cancellation or a bounded time budget."""


@dataclass(frozen=True)
class _SafeEvent:
    """Whitelist projection retained only for the duration of one parse."""

    name: str
    turn_key: str | None
    when: datetime | None
    duration_ms: int | None = None
    last_usage: TokenUsage | None = None
    total_usage: TokenUsage | None = None


@dataclass(frozen=True)
class RolloutUsageResult:
    rollout_filename: str | None
    thread_id: str | None
    instruction: InstructionUsage | None
    available: bool
    thread_cumulative_usage: TokenUsage | None = None
    observed_at: datetime | None = None
    refreshed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    turn_count: int = 0
    completed_responses: tuple[ResponseUsageCandidate, ...] = ()
    scan_metadata: SafeRolloutScanMetadata | None = None

    @property
    def thread_suffix(self) -> str | None:
        return self.thread_id[-8:] if self.thread_id else None


@dataclass(frozen=True)
class CodexSessionUsage:
    thread_id: str
    display_title: str
    title_source: str
    rollout_filename: str
    instruction: InstructionUsage | None
    thread_cumulative_usage: TokenUsage | None
    observed_at: datetime
    refreshed_at: datetime
    status: str
    rollout_path: Path | None = field(default=None, repr=False, compare=False)
    turn_count: int = 0
    full_title: str | None = None


@dataclass(frozen=True)
class RolloutSessionsResult:
    sessions: tuple[CodexSessionUsage, ...]
    latest_active_thread_id: str | None
    running_thread_count: int
    refreshed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    candidate_limit: int = 500
    candidates_found: int = 0
    candidates_loaded: int = 0
    candidate_truncated: bool = False
    files_parsed: int = 0
    files_reused_from_cache: int = 0
    refresh_elapsed_ms: int = 0


@dataclass(frozen=True)
class _CachedRollout:
    """Process-local safe parse result; no raw JSON or text payload is retained."""

    observed_at: datetime
    file_signature: tuple[int, int]
    result: RolloutUsageResult


def configured_sessions_dir() -> Path:
    configured = os.environ.get(CODEX_SESSIONS_DIR_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_SESSIONS_DIR


class CodexRolloutReader:
    """Reads event names and numeric metadata only; text fields are never retained."""

    def __init__(self, candidate_limit: int = 500) -> None:
        self.candidate_limit = candidate_limit
        self._parse_cache: dict[tuple[str, int, int], _CachedRollout] = {}
        self._cache_lock = threading.RLock()
        self._parse_inflight: dict[tuple[str, int, int], threading.Event] = {}

    def refresh(self, sessions_dir: Path | None = None) -> RolloutUsageResult:
        """Compatibility wrapper returning only the latest active session."""
        result = self.refresh_sessions(sessions_dir, lookback_days=None)
        if not result.sessions:
            return RolloutUsageResult(None, None, None, False, refreshed_at=result.refreshed_at)
        session = result.sessions[0]
        parsed_result: RolloutUsageResult | None = None
        if session.rollout_path is not None:
            (_, parsed_result), _reused = self._read_cached(
                session.rollout_path,
                result.refreshed_at,
            )
        return RolloutUsageResult(
            session.rollout_filename, session.thread_id, session.instruction, True,
            session.thread_cumulative_usage, session.observed_at, result.refreshed_at,
            session.turn_count,
            (
                parsed_result.completed_responses
                if parsed_result is not None else ()
            ),
            parsed_result.scan_metadata if parsed_result is not None else None,
        )

    def refresh_sessions(
        self,
        sessions_dir: Path | None = None,
        pinned_path: Path | None = None,
        lookback_days: int | None = None,
        candidate_limit: int | None = None,
    ) -> RolloutSessionsResult:
        started = perf_counter()
        refreshed_at = datetime.now(timezone.utc)
        root = sessions_dir or configured_sessions_dir()
        if not root.is_dir():
            return RolloutSessionsResult((), None, 0, refreshed_at, self.candidate_limit)
        candidate_paths = list(root.rglob("rollout-*.jsonl"))
        self._remove_deleted_cache_entries(candidate_paths)
        if lookback_days is not None:
            mtime_cutoff = (refreshed_at - timedelta(days=lookback_days + 1)).timestamp()
            candidate_paths = [path for path in candidate_paths if _safe_mtime(path) >= mtime_cutoff]
        candidates_found = len(candidate_paths)
        limit = candidate_limit or self.candidate_limit
        candidates = sorted(candidate_paths, key=_safe_mtime, reverse=True)[:limit]
        if pinned_path is not None and pinned_path.is_file() and not any(_same_path(pinned_path, path) for path in candidates):
            candidates.append(pinned_path)
        parsed: list[tuple[datetime, RolloutUsageResult]] = []
        files_parsed = files_reused = 0
        for path in candidates:
            item, reused = self._read_cached(path, refreshed_at)
            parsed.append(item)
            files_reused += int(reused)
            files_parsed += int(not reused)
        newest_by_thread: dict[str, RolloutUsageResult] = {}
        paths: dict[str, Path] = {}
        for path, item in zip(candidates, parsed):
            usage = item[1]
            if not usage.available or not usage.thread_id or usage.observed_at is None:
                continue
            previous = newest_by_thread.get(usage.thread_id)
            if previous is None or (previous.observed_at or _MIN_TIME) < usage.observed_at:
                newest_by_thread[usage.thread_id] = usage
                paths[usage.thread_id] = path
        sessions = tuple(
            self._as_session(item, paths[thread_id])
            for thread_id, item in sorted(
                newest_by_thread.items(),
                key=lambda pair: pair[1].observed_at or _MIN_TIME,
                reverse=True,
            )
            if lookback_days is None
            or (item.observed_at is not None and item.observed_at >= refreshed_at - timedelta(days=lookback_days))
        )
        latest = sessions[0].thread_id if sessions else None
        return RolloutSessionsResult(
            sessions, latest, sum(item.status == "in_progress" for item in sessions), refreshed_at,
            limit, candidates_found, min(candidates_found, limit),
            candidates_found > limit, files_parsed, files_reused,
            round((perf_counter() - started) * 1000),
        )

    def read_session(self, path: Path) -> CodexSessionUsage | None:
        refreshed_at = datetime.now(timezone.utc)
        _, result = self._read_cached(path, refreshed_at)[0]
        return self._as_session(result, path) if result.available and result.thread_id else None

    def read_completed_batch(
        self,
        path: Path,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> CompletedResponseUsageBatch:
        """Return all terminal response candidates from the same cached parse."""

        refreshed_at = datetime.now(timezone.utc)
        (_, result), reused = self._read_cached(
            path,
            refreshed_at,
            cancel_event=cancel_event,
            deadline=deadline,
            require_raw_thread_id=False,
        )
        metadata = result.scan_metadata or _scan_metadata(path, 0, "unavailable")
        if reused:
            metadata = replace(metadata, bytes_scanned=0)
        return CompletedResponseUsageBatch(result.completed_responses, metadata)

    @staticmethod
    def file_scan_metadata(path: Path) -> SafeRolloutScanMetadata:
        """Return content-free stat identity without opening the Rollout."""

        return _scan_metadata(path, 0, "pending")

    def has_cached_parse(self, metadata: SafeRolloutScanMetadata) -> bool:
        key = (metadata.safe_file_hash, metadata.size, metadata.mtime_ns)
        with self._cache_lock:
            return key in self._parse_cache

    def _read_cached(
        self,
        path: Path,
        refreshed_at: datetime,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
        require_raw_thread_id: bool = True,
    ) -> tuple[tuple[datetime, RolloutUsageResult], bool]:
        key = _cache_key(path)
        if key is None:
            return self._read(path, refreshed_at), False
        while True:
            cached: _CachedRollout | None = None
            with self._cache_lock:
                cached = self._parse_cache.get(key)
                if cached is None:
                    pending = self._parse_inflight.get(key)
                    if pending is None:
                        pending = threading.Event()
                        self._parse_inflight[key] = pending
                        break
            if cached is not None:
                cached_result = replace(
                    cached.result,
                    refreshed_at=refreshed_at,
                )
                if require_raw_thread_id and cached_result.available:
                    cached_result = replace(
                        cached_result,
                        thread_id=_read_thread_id_projection(path),
                    )
                return (cached.observed_at, cached_result), True
            while not pending.wait(0.05):
                _raise_if_scan_interrupted(cancel_event, deadline)
        try:
            item = self._read(
                path,
                refreshed_at,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            current_key = _cache_key(path)
            metadata = item[1].scan_metadata
            cacheable = bool(
                current_key == key
                and metadata is not None
                and metadata.result_status == "success"
            )
            if current_key != key:
                changed_metadata = _scan_metadata(
                    path,
                    metadata.bytes_scanned if metadata is not None else 0,
                    "changed_during_scan",
                )
                changed_metadata = replace(
                    changed_metadata,
                    completed_response_count=len(item[1].completed_responses),
                )
                item = (
                    item[0],
                    replace(item[1], scan_metadata=changed_metadata),
                )
            if cacheable:
                with self._cache_lock:
                    safe_identity = key[0]
                    for stale_key in tuple(self._parse_cache):
                        if stale_key[0] == safe_identity and stale_key != key:
                            del self._parse_cache[stale_key]
                    self._parse_cache[key] = _CachedRollout(
                        item[0],
                        key[1:],
                        replace(
                            item[1],
                            thread_id=None,
                            refreshed_at=_MIN_TIME,
                        ),
                    )
            return item, False
        finally:
            with self._cache_lock:
                event = self._parse_inflight.pop(key, None)
                if event is not None:
                    event.set()

    def _remove_deleted_cache_entries(self, existing_paths: list[Path]) -> None:
        existing_identities = {_safe_file_hash(path) for path in existing_paths}
        with self._cache_lock:
            for key in tuple(self._parse_cache):
                if key[0] not in existing_identities:
                    del self._parse_cache[key]

    @staticmethod
    def _as_session(result: RolloutUsageResult, path: Path) -> CodexSessionUsage:
        assert result.thread_id and result.instruction and result.thread_cumulative_usage and result.observed_at
        fallback = f"Codex Session · {result.observed_at.astimezone().strftime('%m-%d %H:%M')}"
        return CodexSessionUsage(
            result.thread_id, fallback, "safe timestamp fallback", path.name,
            result.instruction, result.thread_cumulative_usage, result.observed_at,
            result.refreshed_at, result.instruction.status, path, result.turn_count,
        )

    def _read(
        self,
        path: Path,
        refreshed_at: datetime,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> tuple[datetime, RolloutUsageResult]:
        events: list[_SafeEvent] = []
        newest_token_time: datetime | None = None
        thread_cumulative_usage: TokenUsage | None = None
        thread_id: str | None = None
        thread_identity_stable = True
        parse_integrity_ok = True
        bytes_scanned = 0
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if line_number == 1 or line_number % 128 == 0:
                        _raise_if_scan_interrupted(cancel_event, deadline)
                    bytes_scanned += len(line.encode("utf-8"))
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        parse_integrity_ok = False
                        continue
                    if not isinstance(record, dict):
                        parse_integrity_ok = False
                        continue
                    payload = record.get("payload")
                    payload = payload if isinstance(payload, dict) else {}
                    # Rollouts store the thread identifier in session metadata or
                    # later thread-goal events, not in every token snapshot.
                    event_thread = (
                        _string(record.get("thread_id"))
                        or _string(payload.get("thread_id"))
                        or _string(payload.get("threadId"))
                        or (_string(payload.get("id")) if not _string(payload.get("type")) else None)
                    )
                    if event_thread:
                        if thread_id is not None and event_thread != thread_id:
                            thread_identity_stable = False
                        thread_id = thread_id or event_thread
                    name = _event_name(record, payload)
                    if name not in {"task_started", "task_complete", "token_count"}:
                        continue
                    when = _event_time(record, payload)
                    turn_key = _string(payload.get("turn_id"))
                    if name == "task_started":
                        events.append(_SafeEvent(name, turn_key, when))
                    elif name == "task_complete":
                        duration = _integer(payload.get("duration_ms"))
                        events.append(_SafeEvent(
                            name,
                            turn_key,
                            when,
                            duration if duration is not None and duration >= 0 else None,
                        ))
                    else:
                        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                        current = _usage_from_mapping(info.get("last_token_usage"))
                        total = _usage_from_mapping(info.get("total_token_usage"))
                        events.append(_SafeEvent(
                            name, turn_key, when, last_usage=current, total_usage=total,
                        ))
                        if total is not None:
                            if when is None:
                                thread_cumulative_usage = thread_cumulative_usage or total
                            elif newest_token_time is None or when > newest_token_time:
                                thread_cumulative_usage = total
                                newest_token_time = when
        except OSError:
            failed_metadata = _scan_metadata(path, bytes_scanned, "read_failed")
            return datetime.min.replace(tzinfo=timezone.utc), RolloutUsageResult(
                None,
                None,
                None,
                False,
                refreshed_at=refreshed_at,
                scan_metadata=failed_metadata,
            )
        if thread_cumulative_usage is None:
            empty_metadata = _scan_metadata(path, bytes_scanned, "success")
            empty_metadata = replace(
                empty_metadata,
                bytes_scanned=empty_metadata.size,
            )
            return datetime.min.replace(tzinfo=timezone.utc), RolloutUsageResult(
                path.name,
                thread_id,
                None,
                False,
                refreshed_at=refreshed_at,
                scan_metadata=empty_metadata,
            )
        instruction, completed_responses = _instruction_and_completed_from_events(
            events,
            thread_id,
            thread_identity_stable=thread_identity_stable,
            parse_integrity_ok=parse_integrity_ok,
        )
        base_scan_metadata = _scan_metadata(path, bytes_scanned, "success")
        scan_metadata = replace(
            base_scan_metadata,
            completed_response_count=len(completed_responses),
            bytes_scanned=base_scan_metadata.size,
        )
        if instruction is None:
            return datetime.min.replace(tzinfo=timezone.utc), RolloutUsageResult(
                path.name, thread_id, None, False, thread_cumulative_usage,
                newest_token_time, refreshed_at, completed_responses=completed_responses,
                scan_metadata=scan_metadata,
            )
        turn_count = len({
            event.turn_key
            for event in events
            if event.name == "task_started" and event.turn_key
        })
        result = RolloutUsageResult(
            path.name, thread_id, instruction, True, thread_cumulative_usage,
            newest_token_time, refreshed_at, turn_count, completed_responses,
            scan_metadata,
        )
        return newest_token_time or datetime.min.replace(tzinfo=timezone.utc), result


def _instruction_and_completed_from_events(
    events: list[_SafeEvent],
    thread_id: str | None,
    *,
    thread_identity_stable: bool = True,
    parse_integrity_ok: bool = True,
) -> tuple[InstructionUsage | None, tuple[ResponseUsageCandidate, ...]]:
    cumulative: TokenUsage | None = None
    epoch = 0
    turns: dict[str, dict[str, Any]] = {}
    current_turn: str | None = None
    for event in events:
        name, turn_id, when = event.name, event.turn_key, event.when
        if name == "task_started" and turn_id:
            # A new start closes the preceding turn's association. Its end was
            # continuously updated by token snapshots, including post-complete
            # snapshots that arrived before this boundary.
            turns[turn_id] = {
                "completed": False,
                "baseline": cumulative,
                "baseline_epoch": epoch,
                "end": None,
                "calls": [],
                "duplicates": 0,
                "rejected": 0,
                "unreconciled": 0,
                "when": when,
                "duration": None,
                "completion_time": None,
                "last_token_time": None,
            }
            current_turn = turn_id
            continue
        if name == "task_complete" and turn_id in turns:
            turn = turns[turn_id]
            turn["completed"] = True
            turn["completion_time"] = when
            turn["duration"] = (
                event.duration_ms
                if event.duration_ms is not None
                else _duration_from_times(turn["when"], when)
            )
            continue
        if name != "token_count":
            continue
        # An explicit event turn wins. Otherwise token snapshots continue to
        # belong to the most recently started turn after task_complete, until
        # the next task_started or EOF closes that association.
        active_id = turn_id if turn_id is not None else current_turn
        if (
            turn_id is not None
            and turn_id not in turns
            and current_turn in turns
        ):
            turns[current_turn]["unreconciled"] += 1
        current = event.last_usage
        total = event.total_usage
        if total is None:
            if active_id in turns:
                turns[active_id]["rejected"] += 1
            continue
        if current is None:
            if active_id in turns:
                turns[active_id]["rejected"] += 1
                turns[active_id]["end"] = total
                turns[active_id]["last_token_time"] = when
            if (
                cumulative is not None
                and any(value < 0 for value in total.minus(cumulative).values())
            ):
                epoch += 1
                if active_id in turns:
                    turns[active_id]["unreconciled"] += 1
            cumulative = total
            continue
        if cumulative is not None:
            delta = total.minus(cumulative)
            if total.values() == cumulative.values():
                if active_id in turns:
                    turns[active_id]["duplicates"] += 1
                    turns[active_id]["end"] = total
                    turns[active_id]["last_token_time"] = when
                continue
            if any(value < 0 for value in delta.values()):
                epoch += 1
                if active_id in turns:
                    turns[active_id]["unreconciled"] += 1
                    turns[active_id]["end"] = total
                    turns[active_id]["last_token_time"] = when
                cumulative = total
                continue
            if delta == current:
                if active_id in turns:
                    turns[active_id]["calls"].append(ModelCallUsage(current, epoch))
            elif active_id in turns:
                turns[active_id]["unreconciled"] += 1
            if active_id in turns:
                turns[active_id]["end"] = total
                turns[active_id]["last_token_time"] = when
        elif active_id in turns:
            turns[active_id]["calls"].append(ModelCallUsage(current, epoch))
            turns[active_id]["end"] = total
            turns[active_id]["last_token_time"] = when
        cumulative = total
    if not turns:
        return None, ()
    completed_responses: list[ResponseUsageCandidate] = []
    for completed_turn_id, completed_turn in turns.items():
        if not completed_turn["completed"]:
            continue
        completed_calls = tuple(completed_turn["calls"])
        completed_usage = (
            _sum_usage(call.usage for call in completed_calls)
            if completed_calls else None
        )
        thread_safe_id = make_thread_safe_id(thread_id)
        response_safe_id = make_response_safe_id(thread_id, completed_turn_id)
        if (
            completed_usage is None
            or thread_safe_id is None
            or response_safe_id is None
            or thread_id is None
            or not thread_identity_stable
        ):
            continue
        completed_baseline = completed_turn["baseline"]
        completed_end = completed_turn["end"]
        completed_exact = bool(
            parse_integrity_ok
            and
            completed_turn["completion_time"] is not None
            and completed_turn["when"] is not None
            and completed_turn["completion_time"] >= completed_turn["when"]
            and completed_baseline is not None
            and completed_end is not None
            and completed_turn["baseline_epoch"]
            == (
                completed_calls[-1].epoch
                if completed_calls else completed_turn["baseline_epoch"]
            )
            and not completed_turn["rejected"]
            and not completed_turn["unreconciled"]
            and completed_usage == completed_end.minus(completed_baseline)
        )
        completion_time = (
            completed_turn["completion_time"]
            or completed_turn["last_token_time"]
        )
        diagnostics: list[str] = []
        if completed_baseline is None:
            diagnostics.append("baseline_missing")
        if completed_turn["when"] is None:
            diagnostics.append("start_time_missing")
        if completed_turn["completion_time"] is None:
            diagnostics.append("completion_time_fallback")
        elif (
            completed_turn["when"] is not None
            and completed_turn["completion_time"] < completed_turn["when"]
        ):
            diagnostics.append("completion_time_order_invalid")
        if not parse_integrity_ok:
            diagnostics.append("parse_incomplete")
        if completed_turn["rejected"]:
            diagnostics.append("usage_rejected")
        if completed_turn["unreconciled"]:
            diagnostics.append("usage_unreconciled")
        completed_responses.append(ResponseUsageCandidate(
            thread_safe_id=thread_safe_id,
            response_safe_id=response_safe_id,
            status="exact" if completed_exact else "completed_partial",
            completion_time_utc=completion_time,
            observation_time_utc=completed_turn["last_token_time"],
            input_tokens=completed_usage.input_tokens,
            output_tokens=completed_usage.output_tokens,
            total_tokens=completed_usage.total_tokens,
            cached_input_tokens=completed_usage.cached_input_tokens,
            reasoning_tokens=completed_usage.reasoning_output_tokens,
            trusted_call_count=len(completed_calls),
            integrity_status="closed" if completed_exact else "partial",
            safe_diagnostic_codes=tuple(diagnostics),
        ))
    candidates = list(turns.items())
    complete = [(turn_id, turn) for turn_id, turn in candidates if turn["completed"]]
    # Keep an active instruction preferred; otherwise retain the most recent
    # completed instruction, even when it is incomplete, instead of falling
    # through to an unavailable Rollout state.
    turn_id, turn = (next(((key, value) for key, value in reversed(candidates) if not value["completed"]), None) or (complete[-1] if complete else candidates[-1]))
    calls = tuple(turn["calls"])
    usage = _sum_usage(call.usage for call in calls) if calls else None
    baseline, end = turn["baseline"], turn["end"]
    exact = bool(
        parse_integrity_ok
        and
        turn["completed"]
        and turn["completion_time"] is not None
        and turn["when"] is not None
        and turn["completion_time"] >= turn["when"]
        and baseline is not None
        and end is not None
        and turn["baseline_epoch"] == (calls[-1].epoch if calls else turn["baseline_epoch"])
        and not turn["rejected"]
        and not turn["unreconciled"]
        and usage is not None
        and usage == end.minus(baseline)
    )
    status = "exact" if exact else ("in_progress" if not turn["completed"] else "incomplete")
    return (
        InstructionUsage(
            turn_id, status, usage, len(calls), turn["duration"],
            turn["duplicates"], turn["rejected"], turn["unreconciled"],
            exact, not turn["completed"],
        ),
        tuple(completed_responses),
    )


def _instruction_from_events(events: list[_SafeEvent]) -> InstructionUsage | None:
    """Compatibility helper for existing focused parser tests."""

    return _instruction_and_completed_from_events(events, None)[0]


def _usage_from_payload(payload: dict[str, Any]) -> TokenUsage | None:
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    return _usage_from_mapping(info.get("last_token_usage"))


def _usage_from_mapping(value: Any) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    numbers = tuple(_integer(value.get(key)) for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"))
    if any(number is None for number in numbers):
        return None
    usage = TokenUsage(*numbers)  # type: ignore[arg-type]
    if any(number < 0 for number in usage.values()) or usage.cached_input_tokens > usage.input_tokens or usage.reasoning_output_tokens > usage.output_tokens or usage.total_tokens != usage.input_tokens + usage.output_tokens:
        return None
    return usage


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _event_name(record: dict[str, Any], payload: dict[str, Any]) -> str:
    return _string(payload.get("type")) or _string(record.get("type")) or ""


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _event_time(record: dict[str, Any], payload: dict[str, Any]) -> datetime | None:
    value = record.get("timestamp", record.get("ts", payload.get("timestamp")))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
            return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).astimezone(timezone.utc)
        except ValueError:
            pass
    return None


def _read_thread_id_projection(path: Path) -> str | None:
    """Read only the transient raw Thread identity needed by current selection."""

    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                payload = payload if isinstance(payload, dict) else {}
                thread_id = (
                    _string(record.get("thread_id"))
                    or _string(payload.get("thread_id"))
                    or _string(payload.get("threadId"))
                    or (
                        _string(payload.get("id"))
                        if not _string(payload.get("type")) else None
                    )
                )
                if thread_id is not None:
                    return thread_id
    except OSError:
        return None
    return None


def _duration_from_times(start: datetime | None, end: datetime | None) -> int | None:
    return max(round((end - start).total_seconds() * 1000), 0) if start and end else None


def _sum_usage(usages: Any) -> TokenUsage:
    values = [0, 0, 0, 0, 0]
    for usage in usages:
        for index, value in enumerate(usage.values()):
            values[index] += value
    return TokenUsage(*values)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _cache_key(path: Path) -> tuple[str, int, int] | None:
    try:
        stat = path.stat()
        return (_safe_file_hash(path), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


def _safe_file_hash(path: Path) -> str:
    encoded = str(path.resolve()).casefold().encode("utf-8")
    return "sha256:" + hashlib.sha256(_FILE_SAFE_ID_DOMAIN + encoded).hexdigest()


def _scan_metadata(
    path: Path,
    bytes_scanned: int,
    result_status: str,
) -> SafeRolloutScanMetadata:
    try:
        stat = path.stat()
        return SafeRolloutScanMetadata(
            _safe_file_hash(path),
            stat.st_size,
            stat.st_mtime_ns,
            0,
            bytes_scanned,
            result_status,
        )
    except OSError:
        return SafeRolloutScanMetadata(
            _safe_file_hash(path), 0, 0, 0, bytes_scanned, result_status,
        )


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left == right


def _raise_if_scan_interrupted(
    cancel_event: threading.Event | None,
    deadline: float | None,
) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RolloutScanInterrupted("rollout_scan_cancelled")
    if deadline is not None and perf_counter() >= deadline:
        raise RolloutScanInterrupted("rollout_scan_time_budget_exceeded")
