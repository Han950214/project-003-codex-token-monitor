"""Privacy-preserving reader for numeric instruction usage in Codex rollouts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any


CODEX_SESSIONS_DIR_ENV = "CODEX_SESSIONS_DIR"
DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
_MIN_TIME = datetime.min.replace(tzinfo=timezone.utc)


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
class RolloutUsageResult:
    rollout_filename: str | None
    thread_id: str | None
    instruction: InstructionUsage | None
    available: bool
    thread_cumulative_usage: TokenUsage | None = None
    observed_at: datetime | None = None
    refreshed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    turn_count: int = 0

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

    def refresh(self, sessions_dir: Path | None = None) -> RolloutUsageResult:
        """Compatibility wrapper returning only the latest active session."""
        result = self.refresh_sessions(sessions_dir, lookback_days=None)
        if not result.sessions:
            return RolloutUsageResult(None, None, None, False, refreshed_at=result.refreshed_at)
        session = result.sessions[0]
        return RolloutUsageResult(
            session.rollout_filename, session.thread_id, session.instruction, True,
            session.thread_cumulative_usage, session.observed_at, result.refreshed_at,
            session.turn_count,
        )

    def refresh_sessions(
        self,
        sessions_dir: Path | None = None,
        pinned_path: Path | None = None,
        lookback_days: int | None = None,
    ) -> RolloutSessionsResult:
        started = perf_counter()
        refreshed_at = datetime.now(timezone.utc)
        root = sessions_dir or configured_sessions_dir()
        if not root.is_dir():
            return RolloutSessionsResult((), None, 0, refreshed_at, self.candidate_limit)
        candidate_paths = list(root.rglob("rollout-*.jsonl"))
        self._remove_deleted_cache_entries()
        if lookback_days is not None:
            mtime_cutoff = (refreshed_at - timedelta(days=lookback_days + 1)).timestamp()
            candidate_paths = [path for path in candidate_paths if _safe_mtime(path) >= mtime_cutoff]
        candidates_found = len(candidate_paths)
        candidates = sorted(candidate_paths, key=_safe_mtime, reverse=True)[: self.candidate_limit]
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
            self.candidate_limit, candidates_found, min(candidates_found, self.candidate_limit),
            candidates_found > self.candidate_limit, files_parsed, files_reused,
            round((perf_counter() - started) * 1000),
        )

    def read_session(self, path: Path) -> CodexSessionUsage | None:
        refreshed_at = datetime.now(timezone.utc)
        _, result = self._read_cached(path, refreshed_at)[0]
        return self._as_session(result, path) if result.available and result.thread_id else None

    def _read_cached(self, path: Path, refreshed_at: datetime) -> tuple[tuple[datetime, RolloutUsageResult], bool]:
        key = _cache_key(path)
        if key is not None:
            cached = self._parse_cache.get(key)
            if cached is not None:
                return (cached.observed_at, replace(cached.result, refreshed_at=refreshed_at)), True
        item = self._read(path, refreshed_at)
        if key is not None:
            normalized_path = key[0]
            for stale_key in tuple(self._parse_cache):
                if stale_key[0] == normalized_path and stale_key != key:
                    del self._parse_cache[stale_key]
            self._parse_cache[key] = _CachedRollout(item[0], key[1:], replace(item[1], refreshed_at=_MIN_TIME))
        return item, False

    def _remove_deleted_cache_entries(self) -> None:
        for key in tuple(self._parse_cache):
            if not Path(key[0]).is_file():
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

    def _read(self, path: Path, refreshed_at: datetime) -> tuple[datetime, RolloutUsageResult]:
        events: list[tuple[str, dict[str, Any], datetime | None]] = []
        newest_token_time: datetime | None = None
        thread_cumulative_usage: TokenUsage | None = None
        thread_id: str | None = None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(record, dict):
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
                        thread_id = thread_id or event_thread
                    name = _event_name(record, payload)
                    if name not in {"task_started", "task_complete", "token_count"}:
                        continue
                    when = _event_time(record, payload)
                    events.append((name, payload, when))
                    if name == "token_count" and _usage_from_payload(payload) is not None:
                        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                        total = _usage_from_mapping(info.get("total_token_usage"))
                        if total is not None:
                            if when is None:
                                thread_cumulative_usage = thread_cumulative_usage or total
                            elif newest_token_time is None or when > newest_token_time:
                                thread_cumulative_usage = total
                                newest_token_time = when
        except OSError:
            return datetime.min.replace(tzinfo=timezone.utc), RolloutUsageResult(None, None, None, False, refreshed_at=refreshed_at)
        if thread_cumulative_usage is None:
            return datetime.min.replace(tzinfo=timezone.utc), RolloutUsageResult(None, None, None, False, refreshed_at=refreshed_at)
        instruction = _instruction_from_events(events)
        if instruction is None:
            return datetime.min.replace(tzinfo=timezone.utc), RolloutUsageResult(path.name, thread_id, None, False, thread_cumulative_usage, newest_token_time, refreshed_at)
        turn_count = len({
            _string(payload.get("turn_id"))
            for name, payload, _when in events
            if name == "task_started" and _string(payload.get("turn_id"))
        })
        result = RolloutUsageResult(
            path.name, thread_id, instruction, True, thread_cumulative_usage,
            newest_token_time, refreshed_at, turn_count,
        )
        return newest_token_time or datetime.min.replace(tzinfo=timezone.utc), result


def _instruction_from_events(events: list[tuple[str, dict[str, Any], datetime | None]]) -> InstructionUsage | None:
    cumulative: TokenUsage | None = None
    epoch = 0
    turns: dict[str, dict[str, Any]] = {}
    current_turn: str | None = None
    for name, payload, when in events:
        turn_id = _string(payload.get("turn_id"))
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
            }
            current_turn = turn_id
            continue
        if name == "task_complete" and turn_id in turns:
            turn = turns[turn_id]
            turn["completed"] = True
            duration = payload.get("duration_ms")
            turn["duration"] = duration if _integer(duration) is not None and _integer(duration) >= 0 else _duration_from_times(turn["when"], when)
            continue
        if name != "token_count":
            continue
        # An explicit event turn wins. Otherwise token snapshots continue to
        # belong to the most recently started turn after task_complete, until
        # the next task_started or EOF closes that association.
        active_id = turn_id if turn_id in turns else current_turn
        current = _usage_from_payload(payload)
        if current is None:
            if active_id in turns:
                turns[active_id]["rejected"] += 1
            continue
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total = _usage_from_mapping(info.get("total_token_usage"))
        if total is None:
            if active_id in turns:
                turns[active_id]["rejected"] += 1
            continue
        if cumulative is not None:
            delta = total.minus(cumulative)
            if total.values() == cumulative.values():
                if active_id in turns:
                    turns[active_id]["duplicates"] += 1
                    turns[active_id]["end"] = total
                continue
            if any(value < 0 for value in delta.values()):
                epoch += 1
                if active_id in turns:
                    turns[active_id]["unreconciled"] += 1
                    turns[active_id]["end"] = total
                cumulative = total
                continue
            if delta == current:
                if active_id in turns:
                    turns[active_id]["calls"].append(ModelCallUsage(current, epoch))
            elif active_id in turns:
                turns[active_id]["unreconciled"] += 1
            if active_id in turns:
                turns[active_id]["end"] = total
        elif active_id in turns:
            turns[active_id]["end"] = total
        cumulative = total
    if not turns:
        return None
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
        turn["completed"]
        and baseline is not None
        and end is not None
        and turn["baseline_epoch"] == (calls[-1].epoch if calls else turn["baseline_epoch"])
        and not turn["unreconciled"]
        and usage is not None
        and usage == end.minus(baseline)
    )
    status = "exact" if exact else ("in_progress" if not turn["completed"] else "incomplete")
    return InstructionUsage(turn_id, status, usage, len(calls), turn["duration"], turn["duplicates"], turn["rejected"], turn["unreconciled"], exact, not turn["completed"])


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
        return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    except OSError:
        return None


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left == right
