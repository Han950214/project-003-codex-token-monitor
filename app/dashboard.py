"""Multi-session Rollout-to-Dashboard selection and reconciliation boundary."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from app.codex_rollout import (
    CodexRolloutReader,
    CodexSessionUsage,
    InstructionUsage,
    RolloutSessionsResult,
    RolloutUsageResult,
)
from app.codex_state import CodexThreadMetadata, CodexThreadTotal, load_thread_metadata
from app.metrics import PricingConfig, SessionSummary


ACTIVE_EVENT_MAX_AGE = timedelta(minutes=10)


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


@dataclass(frozen=True)
class MiniThreadSnapshot:
    title: str
    instruction_total_tokens: int | None
    session_total_tokens: int | None
    status: str
    observed_at: datetime | None
    turn_count: int | None = None
    full_title: str | None = None


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
        self._known_paths: dict[str, Path] = {}
        self._known_sessions: dict[str, CodexSessionUsage] = {}
        self._title_cache: dict[str, str] = {}
        self._last_snapshot: DashboardSnapshot | None = None
        self.lookback_days = 7

    def set_auto_follow(self) -> DashboardSnapshot | None:
        self.selection_mode = "auto"
        self.selected_thread_id = None
        if self._last_snapshot and self._last_snapshot.recent_sessions:
            return self._cached_snapshot(self._last_snapshot.recent_sessions[0], "auto")
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
        for session in recent:
            self._known_sessions[session.thread_id] = session
            if session.rollout_path is not None:
                self._known_paths[session.thread_id] = session.rollout_path

        selected = self._select(recent)
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
            self.lookback_days,
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
            )
        instruction = session.instruction
        instruction_total = instruction.usage.total_tokens if instruction is not None and instruction.usage is not None else None
        cumulative = session.thread_cumulative_usage
        session_total = cumulative.total_tokens if cumulative is not None else None
        return MiniThreadSnapshot(
            session.display_title, instruction_total, session_total, status,
            session.observed_at, session.turn_count,
            session.full_title or session.display_title,
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
    return snapshot.selected_session.instruction if snapshot.selected_session else snapshot.rollout.instruction


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
