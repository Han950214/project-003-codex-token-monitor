"""Multi-session Rollout-to-Dashboard selection and reconciliation boundary."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from time import perf_counter
from typing import Callable

from app.codex_rollout import (
    CompletedResponseUsageBatch,
    CodexRolloutReader,
    CodexSessionUsage,
    InstructionUsage,
    RolloutScanInterrupted,
    RolloutSessionsResult,
    RolloutUsageResult,
    SafeRolloutScanMetadata,
    configured_sessions_dir,
    make_response_safe_id,
)
from app.codex_state import CodexThreadMetadata, CodexThreadTotal, load_thread_metadata
from app.history import UsageHistoryStore
from app.metrics import PricingConfig, SessionSummary


ACTIVE_EVENT_MAX_AGE = timedelta(minutes=10)
BACKFILL_MAX_TIME_RANGE_DAYS = 30
BACKFILL_MAX_PROCESSED_FILES = 500
BACKFILL_MAX_SCAN_BYTES = 67_108_864
BACKFILL_MAX_SINGLE_FILE_BYTES = 4_194_304
BACKFILL_MAX_COMPLETED_RESPONSES = 5_000
BACKFILL_TIME_BUDGET_MS = 8_000
BACKFILL_DATABASE_BATCH_SIZE = 100


@dataclass(frozen=True)
class DashboardSnapshot:
    # First seven fields preserve the Phase 2.7-A construction contract.
    runs: object
    summary: SessionSummary
    rollout: RolloutUsageResult
    state_total: CodexThreadTotal | None
    state_reconciled: bool
    state_reconciliation: str = "unavailable"
    storage_error: str | None = None
    sessions_result: RolloutSessionsResult = field(default_factory=lambda: RolloutSessionsResult((), None, 0))
    recent_sessions: tuple[CodexSessionUsage, ...] = ()
    selected_session: CodexSessionUsage | None = None
    state_metadata: dict[str, CodexThreadMetadata] = field(default_factory=dict)
    selection_mode: str = "auto"
    selected_thread_id: str | None = None
    lookback_days: int = 7
    current_session: CodexSessionUsage | None = None
    current_thread_id: str | None = None


@dataclass(frozen=True)
class MiniThreadSnapshot:
    title: str
    instruction_total_tokens: int | None
    session_total_tokens: int | None
    status: str
    observed_at: datetime | None
    turn_count: int | None = None
    full_title: str | None = None
    response_safe_id: str | None = None
    response_status: str | None = None


@dataclass(frozen=True)
class ResponseHistoryBackfillResult:
    status: str
    candidate_file_count: int = 0
    processed_file_count: int = 0
    unchanged_file_count: int = 0
    skipped_file_count: int = 0
    completed_response_count: int = 0
    inserted_observation_count: int = 0
    scan_bytes: int = 0
    elapsed_ms: int = 0


class ResponseHistoryBackfillService:
    """Bounded synchronous worker body; callers decide which thread runs it."""

    def __init__(
        self,
        reader: CodexRolloutReader,
        history_store: UsageHistoryStore,
        *,
        sessions_dir: Path | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.reader = reader
        self.history_store = history_store
        self.sessions_dir = sessions_dir
        self.clock = clock

    def run_once(self, cancel_event: Event | None = None) -> ResponseHistoryBackfillResult:
        cancel = cancel_event or Event()
        started = perf_counter()
        deadline = started + (BACKFILL_TIME_BUDGET_MS / 1000)
        root = self.sessions_dir or configured_sessions_dir()
        if not root.is_dir():
            return ResponseHistoryBackfillResult(
                "unavailable",
                elapsed_ms=round((perf_counter() - started) * 1000),
            )
        cutoff = (
            self.clock().astimezone(timezone.utc)
            - timedelta(days=BACKFILL_MAX_TIME_RANGE_DAYS)
        ).timestamp()
        discovered: list[tuple[Path, SafeRolloutScanMetadata]] = []
        try:
            for path in root.rglob("rollout-*.jsonl"):
                if cancel.is_set():
                    return ResponseHistoryBackfillResult(
                        "cancelled",
                        candidate_file_count=len(discovered),
                        elapsed_ms=round((perf_counter() - started) * 1000),
                    )
                if perf_counter() >= deadline:
                    return ResponseHistoryBackfillResult(
                        "incomplete",
                        candidate_file_count=len(discovered),
                        elapsed_ms=round((perf_counter() - started) * 1000),
                    )
                metadata = self.reader.file_scan_metadata(path)
                if metadata.mtime_ns / 1_000_000_000 >= cutoff:
                    discovered.append((path, metadata))
        except OSError:
            return ResponseHistoryBackfillResult(
                "unavailable",
                elapsed_ms=round((perf_counter() - started) * 1000),
            )
        watermark_statuses = self.history_store.backfill_file_statuses(
            metadata for _path, metadata in discovered
        )
        current = {
            safe_file_hash
            for safe_file_hash, status in watermark_statuses.items()
            if status == "success"
        }
        pending = [
            item for item in discovered
            if item[1].safe_file_hash not in current
        ]
        pending.sort(key=lambda item: (
            item[1].safe_file_hash in watermark_statuses,
            -item[1].mtime_ns,
        ))
        processed = skipped = completed = inserted = scanned = 0
        status = "completed"
        for path, metadata in pending:
            if cancel.is_set():
                status = "cancelled"
                break
            if perf_counter() >= deadline:
                status = "incomplete"
                break
            if processed >= BACKFILL_MAX_PROCESSED_FILES:
                status = "incomplete"
                break
            if metadata.size > BACKFILL_MAX_SINGLE_FILE_BYTES:
                self.history_store.record_backfill_failure(
                    metadata, "file_too_large",
                )
                if self.history_store.last_error is not None:
                    status = "incomplete"
                    break
                processed += 1
                skipped += 1
                continue
            estimated_read = (
                0 if self.reader.has_cached_parse(metadata) else metadata.size
            )
            if scanned + estimated_read > BACKFILL_MAX_SCAN_BYTES:
                status = "incomplete"
                break
            try:
                batch = self.reader.read_completed_batch(
                    path,
                    cancel_event=cancel,
                    deadline=deadline,
                )
            except RolloutScanInterrupted:
                status = "cancelled" if cancel.is_set() else "incomplete"
                break
            scanned += batch.scan_metadata.bytes_scanned
            if scanned > BACKFILL_MAX_SCAN_BYTES:
                self.history_store.record_backfill_failure(
                    batch.scan_metadata, "scan_byte_budget_exceeded",
                )
                status = "incomplete"
                break
            if batch.scan_metadata.result_status != "success":
                self.history_store.record_backfill_failure(
                    batch.scan_metadata, "rollout_read_failed",
                )
                if self.history_store.last_error is not None:
                    status = "incomplete"
                    break
                processed += 1
                skipped += 1
                continue
            if completed + len(batch.responses) > BACKFILL_MAX_COMPLETED_RESPONSES:
                status = "incomplete"
                break
            responses = batch.responses
            if not responses:
                write_result = self.history_store.record_completed_batch(batch)
                if self.history_store.last_error is not None:
                    status = "incomplete"
                    break
                inserted += write_result.inserted_count
            else:
                for offset in range(0, len(responses), BACKFILL_DATABASE_BATCH_SIZE):
                    chunk = responses[offset:offset + BACKFILL_DATABASE_BATCH_SIZE]
                    final_chunk = offset + len(chunk) == len(responses)
                    write_result = self.history_store.record_completed_batch(
                        CompletedResponseUsageBatch(chunk, batch.scan_metadata),
                        mark_success=final_chunk,
                    )
                    if self.history_store.last_error is not None:
                        status = "incomplete"
                        break
                    inserted += write_result.inserted_count
                if status != "completed":
                    break
            processed += 1
            completed += len(responses)
        return ResponseHistoryBackfillResult(
            status,
            candidate_file_count=len(discovered),
            processed_file_count=processed,
            unchanged_file_count=len(current),
            skipped_file_count=skipped,
            completed_response_count=completed,
            inserted_observation_count=inserted,
            scan_bytes=scanned,
            elapsed_ms=round((perf_counter() - started) * 1000),
        )


class DashboardViewModel:
    def __init__(
        self,
        pricing: PricingConfig | None = None,
        runs_path: Path | None = None,
        *,
        rollout_sessions_loader: Callable[[], RolloutSessionsResult] | None = None,
        state_batch_loader: Callable[[tuple[str, ...]], dict[str, CodexThreadMetadata]] = load_thread_metadata,
        title_batch_loader: Callable[[], dict[str, str] | None] | None = None,
        rollout_reader: CodexRolloutReader | None = None,
        **legacy: object,
    ) -> None:
        self.pricing = pricing or PricingConfig(0, 0, 0)
        self.runs_path = runs_path
        self.rollout_reader = rollout_reader or CodexRolloutReader()
        legacy_rollout = legacy.get("rollout_loader")
        legacy_state = legacy.get("state_loader")
        self.rollout_sessions_loader = rollout_sessions_loader or (
            (lambda: _legacy_sessions(legacy_rollout())) if callable(legacy_rollout) else None
        )
        self.state_batch_loader = (
            (lambda ids: _legacy_metadata(ids, legacy_state))
            if callable(legacy_state) else state_batch_loader
        )
        self.title_batch_loader = title_batch_loader or (lambda: {})
        self.selection_mode = "auto"
        self.selected_thread_id: str | None = None
        self.current_thread_id: str | None = None
        self._known_paths: dict[str, Path] = {}
        self._known_sessions: dict[str, CodexSessionUsage] = {}
        self._title_cache: dict[str, str] = {}
        self._last_snapshot: DashboardSnapshot | None = None
        self.lookback_days = 7

    def set_auto_follow(self) -> DashboardSnapshot | None:
        self.selection_mode = "auto"
        self.selected_thread_id = None
        if self._last_snapshot and self._last_snapshot.current_session is not None:
            return self._cached_snapshot(self._last_snapshot.current_session, "auto")
        return None

    def select_cached_thread(self, thread_id: str) -> DashboardSnapshot | None:
        """Select from the last full refresh without storage or server reads."""
        if self._last_snapshot is None:
            return None
        selected = next((item for item in self._last_snapshot.recent_sessions if item.thread_id == thread_id), None)
        if selected is None or selected.status == "unavailable":
            return None
        self.selection_mode = "pinned"
        self.selected_thread_id = thread_id
        return self._cached_snapshot(selected, "pinned")

    def pin_thread(self, thread_id: str) -> bool:
        if not thread_id:
            return False
        known = self._known_sessions.get(thread_id)
        if known is not None and known.status == "unavailable":
            return False
        self.selection_mode = "pinned"
        self.selected_thread_id = thread_id
        return True

    def set_lookback_days(self, days: int) -> bool:
        if days not in {7, 30, 90}:
            return False
        self.lookback_days = days
        return True

    def refresh(self, _runs: object = None) -> DashboardSnapshot:
        result = (
            self.rollout_sessions_loader()
            if self.rollout_sessions_loader is not None
            else self.rollout_reader.refresh_sessions(lookback_days=self.lookback_days)
        )
        recent = [_effective_session_status(session, result.refreshed_at) for session in result.sessions]
        current = next(
            (item for item in recent if item.thread_id == result.latest_active_thread_id),
            recent[0] if recent else None,
        )
        self.current_thread_id = current.thread_id if current is not None else None
        for session in recent:
            self._known_sessions[session.thread_id] = session
            if session.rollout_path is not None:
                self._known_paths[session.thread_id] = session.rollout_path

        selected = current if self.selection_mode == "auto" else self._select(recent)
        if self.selection_mode == "pinned" and self.selected_thread_id and selected is None:
            selected = self._load_known_pinned(self.selected_thread_id)
            if selected is not None:
                selected = _effective_session_status(selected, result.refreshed_at)

        thread_ids = list(dict.fromkeys(
            [session.thread_id for session in recent]
            + ([selected.thread_id] if selected is not None else [])
        ))
        metadata = self.state_batch_loader(tuple(thread_ids)) if thread_ids else {}
        try:
            titles = self.title_batch_loader()
        except Exception:
            titles = None
        # A failed title batch must not erase titles already verified for a
        # Thread. A successful empty batch is still authoritative and clears it.
        if isinstance(titles, dict):
            self._title_cache = titles
        recent = [self._apply_title(session) for session in recent]
        if current is not None:
            current = next(
                (item for item in recent if item.thread_id == current.thread_id),
                self._apply_title(current),
            )
            self._known_sessions[current.thread_id] = current
        if selected is not None:
            selected = next(
                (item for item in recent if item.thread_id == selected.thread_id),
                self._apply_title(selected),
            )
            self._known_sessions[selected.thread_id] = selected

        state_item = metadata.get(selected.thread_id) if selected else None
        state_total = _as_total(state_item)
        reconciliation = _state_reconciliation(selected, state_item)
        summary = SessionSummary(0, 0, 0, 0, 0, 0, 0, 0, 0)
        rollout = RolloutUsageResult(
            selected.rollout_filename if selected else None,
            selected.thread_id if selected else self.selected_thread_id,
            selected.instruction if selected else None,
            bool(selected and selected.status != "unavailable"),
            selected.thread_cumulative_usage if selected else None,
            selected.observed_at if selected else None,
            result.refreshed_at,
            selected.turn_count if selected else 0,
        )
        snapshot = DashboardSnapshot(
            (), summary, rollout, state_total, reconciliation == "reconciled",
            reconciliation, None, result, tuple(recent), selected, metadata,
            self.selection_mode, selected.thread_id if selected else self.selected_thread_id,
            self.lookback_days, current, self.current_thread_id,
        )
        self._last_snapshot = snapshot
        return snapshot

    def refresh_thread(self, thread_id: str | None) -> MiniThreadSnapshot:
        """Refresh one already-known Thread without changing Dashboard selection."""
        if not thread_id:
            return MiniThreadSnapshot("", None, None, "no_selection", None, None)
        session = self._load_known_pinned(thread_id)
        if session is None:
            return MiniThreadSnapshot("", None, None, "unavailable", None, None)
        session = _effective_session_status(session, datetime.now(session.observed_at.tzinfo))
        self.state_batch_loader((thread_id,))
        session = self._apply_title(session)
        self._known_sessions[thread_id] = session
        status = display_session_status(session, session.instruction)
        if status == "unavailable":
            return MiniThreadSnapshot(
                session.display_title, None, None, status, session.observed_at,
                session.turn_count, session.full_title or session.display_title,
                response_status="unavailable",
            )
        instruction = session.instruction
        instruction_total = instruction.usage.total_tokens if instruction is not None and instruction.usage is not None else None
        cumulative = session.thread_cumulative_usage
        session_total = cumulative.total_tokens if cumulative is not None else None
        return MiniThreadSnapshot(
            session.display_title, instruction_total, session_total, status,
            session.observed_at, session.turn_count,
            session.full_title or session.display_title,
            make_response_safe_id(session.thread_id, instruction.turn_id)
            if instruction is not None else None,
            _history_instruction_status(instruction),
        )

    def _select(self, sessions: list[CodexSessionUsage]) -> CodexSessionUsage | None:
        if self.selection_mode == "auto":
            return sessions[0] if sessions else None
        return next((item for item in sessions if item.thread_id == self.selected_thread_id), None)

    def _load_known_pinned(self, thread_id: str) -> CodexSessionUsage | None:
        path = self._known_paths.get(thread_id)
        if path is not None and path.is_file():
            session = self.rollout_reader.read_session(path)
            if session is not None and session.thread_id == thread_id:
                self._known_sessions[thread_id] = session
                return session
        known = self._known_sessions.get(thread_id)
        return replace(known, instruction=None, thread_cumulative_usage=None, status="unavailable") if known else None

    def _apply_title(self, session: CodexSessionUsage) -> CodexSessionUsage:
        title = self._title_cache.get(session.thread_id)
        if title:
            full_title = _normalized_display_title(title)
            return replace(
                session,
                display_title=_bounded_display_title(full_title),
                title_source="codex_app_server.thread_display_title",
                full_title=full_title,
            )
        fallback = f"Codex Session · {session.observed_at.astimezone().strftime('%m-%d %H:%M')}"
        return replace(
            session, display_title=fallback, title_source="safe timestamp fallback",
            full_title=fallback,
        )

    def _cached_snapshot(self, selected: CodexSessionUsage, mode: str) -> DashboardSnapshot:
        previous = self._last_snapshot
        assert previous is not None
        state_item = previous.state_metadata.get(selected.thread_id)
        reconciliation = _state_reconciliation(selected, state_item)
        rollout = RolloutUsageResult(
            selected.rollout_filename, selected.thread_id, selected.instruction,
            selected.status != "unavailable", selected.thread_cumulative_usage,
            selected.observed_at, previous.rollout.refreshed_at, selected.turn_count,
        )
        snapshot = replace(
            previous, rollout=rollout, state_total=_as_total(state_item),
            state_reconciled=reconciliation == "reconciled",
            state_reconciliation=reconciliation, selected_session=selected,
            selection_mode=mode, selected_thread_id=selected.thread_id,
        )
        self._last_snapshot = snapshot
        return snapshot


def instruction_usage(snapshot: DashboardSnapshot) -> InstructionUsage | None:
    current = snapshot.current_session or snapshot.selected_session
    return current.instruction if current is not None else snapshot.rollout.instruction


def display_session_status(
    session: CodexSessionUsage | None, instruction: InstructionUsage | None
) -> str:
    """Derive user-facing task state without changing Rollout aggregation semantics."""
    if instruction is None:
        return "unavailable"
    if session is not None and session.status == "unavailable":
        return "unavailable"
    effective_session_status = session.status if session is not None else instruction.status
    if instruction.in_progress:
        if effective_session_status == "in_progress" and instruction.unreconciled_events == 0:
            return "in_progress"
        return "incomplete"
    return "exact" if instruction.exact else "completed_partial"


def _history_instruction_status(instruction: InstructionUsage | None) -> str:
    """Normalize parser facts for persistence without display-state overload."""

    if instruction is None:
        return "unavailable"
    if instruction.in_progress:
        return "in_progress"
    return "exact" if instruction.exact else "completed_partial"


def _legacy_sessions(rollout: RolloutUsageResult) -> RolloutSessionsResult:
    if not rollout.available or not rollout.thread_id:
        return RolloutSessionsResult((), None, 0, rollout.refreshed_at)
    observed = rollout.observed_at or rollout.refreshed_at
    status = rollout.instruction.status if rollout.instruction else "unavailable"
    session = CodexSessionUsage(
        rollout.thread_id, f"Codex Session · {observed.astimezone().strftime('%m-%d %H:%M')}",
        "safe timestamp fallback", rollout.rollout_filename or "rollout.jsonl",
        rollout.instruction, rollout.thread_cumulative_usage, observed,
        rollout.refreshed_at, status,
    )
    return RolloutSessionsResult((session,), session.thread_id, int(status == "in_progress"), rollout.refreshed_at)


def _legacy_metadata(ids: tuple[str, ...], loader: Callable[[str], CodexThreadTotal | None]) -> dict[str, CodexThreadMetadata]:
    if not ids:
        return {}
    item = loader(ids[0])
    if item is None:
        return {}
    return {item.thread_id: CodexThreadMetadata(item.thread_id, item.created_at, item.updated_at, item.model, item.model_provider, item.total_tokens)}


def _as_total(item: CodexThreadMetadata | None) -> CodexThreadTotal | None:
    if item is None or item.total_tokens is None:
        return None
    return CodexThreadTotal(
        item.thread_id, item.created_at, item.updated_at, item.model,
        item.model_provider, item.total_tokens,
    )


def _state_reconciliation(
    session: CodexSessionUsage | None, state: CodexThreadMetadata | None
) -> str:
    if session is None or state is None or session.thread_cumulative_usage is None:
        return "unavailable"
    if state.thread_id != session.thread_id or state.total_tokens is None:
        return "unavailable"
    return "reconciled" if state.total_tokens == session.thread_cumulative_usage.total_tokens else "mismatch"


def _bounded_display_title(value: str, limit: int = 72) -> str:
    title = _normalized_display_title(value)
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def _normalized_display_title(value: str) -> str:
    return " ".join(value.split())


def _effective_session_status(
    session: CodexSessionUsage, refreshed_at: datetime
) -> CodexSessionUsage:
    if session.status != "in_progress":
        return session
    if refreshed_at - session.observed_at <= ACTIVE_EVENT_MAX_AGE:
        return session
    # A stale unfinished Rollout proves only that its completion boundary is
    # missing; it does not prove the Codex task is still running.
    return replace(session, status="incomplete")
