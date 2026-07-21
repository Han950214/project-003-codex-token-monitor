"""Pure conversion from a selected Codex session to Dashboard display values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from app.dashboard import DashboardSnapshot, display_session_status
from app.i18n import translate


class UiTone(str, Enum):
    FRESH = "fresh"
    ESTIMATE = "estimate"
    STALE = "stale"
    ERROR = "error"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


class DataStatus(str, Enum):
    RUNNING = "Running"
    COMPLETED = "Completed"
    COMPLETED_PARTIAL = "Completed (Partial Data)"
    INCOMPLETE = "Incomplete"
    UNAVAILABLE = "Unavailable"
    REFRESHING = "Refreshing"
    # Compatibility names for callers from earlier phases.
    FRESH_REAL = "Completed"
    LOCAL_ESTIMATE = "Local Estimate"
    NO_DATA = "Unavailable"
    STALE_DATA = "Stale Data"
    LOGS_ERROR = "Logs Error"
    STATE_ERROR = "State Error"


class UiDataScope(str, Enum):
    """Explicit product scopes used by the dashboard without changing data DTOs."""

    CURRENT_ACTIVITY = "current_activity"
    RECENT_ACTIVITY = "recent_activity"
    SELECTED_SESSION = "selected_session"
    GLOBAL_SUMMARY = "global_summary"
    LIVE_QUOTA = "live_quota"
    LOCAL_QUOTA_HISTORY = "local_quota_history"


class HistoryEmptyState(str, Enum):
    AVAILABLE = "available"
    FIRST_USE = "first_use"
    SELECTED_NO_HISTORY = "selected_no_history"
    RANGE_EMPTY = "range_empty"
    IN_PROGRESS_ONLY = "in_progress_only"
    NO_SELECTION = "no_selection"
    MAPPING_FAILED = "mapping_failed"
    PARTIAL = "partial"
    STALE = "stale"
    BACKFILL_INCOMPLETE = "backfill_incomplete"
    UNAVAILABLE = "unavailable"


class QuotaAvailability(str, Enum):
    LIVE_AND_HISTORY = "live_and_history"
    LIVE_ONLY = "live_only"
    HISTORY_ONLY = "history_only"
    WEEKLY_ONLY = "weekly_only"
    STALE_LIVE = "stale_live"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class MetricDisplay:
    label: str
    value: str
    detail: str
    tone: UiTone


@dataclass(frozen=True)
class SourceDisplay:
    label: str
    value: str
    tone: UiTone = UiTone.UNKNOWN


@dataclass(frozen=True)
class RecentSessionRow:
    thread_id: str
    display_title: str
    title_source: str
    status: str
    last_activity: object
    thread_total: str
    cache_hit: str
    turn_count: int = 0
    full_title: str = ""
    thread_total_tokens: int | None = None


@dataclass(frozen=True)
class DashboardPresentation:
    data_status: DataStatus
    status_tone: UiTone
    status_message: str
    latest_usage: tuple[MetricDisplay, ...]
    source_details: tuple[SourceDisplay, ...]
    last_event: str
    last_refresh: str
    auto_refresh: str
    recent_sessions: tuple[RecentSessionRow, ...]
    telemetry_current_total: str
    telemetry_cache_hit: str
    telemetry_session_total: str
    usage_scope: str = "unavailable"


@dataclass(frozen=True)
class UiScopeContract:
    activity_scope: UiDataScope
    activity_thread_id: str | None
    selected_scope: UiDataScope | None
    selected_thread_id: str | None
    selected_is_pinned: bool
    selected_is_activity: bool
    global_scope: UiDataScope = UiDataScope.GLOBAL_SUMMARY
    live_quota_scope: UiDataScope = UiDataScope.LIVE_QUOTA
    local_quota_history_scope: UiDataScope = UiDataScope.LOCAL_QUOTA_HISTORY


@dataclass(frozen=True)
class UiAction:
    kind: str
    label_key: str
    target_scope: UiDataScope | None = None


@dataclass(frozen=True)
class ActionableStateView:
    kind: HistoryEmptyState | QuotaAvailability
    title_key: str
    reason_key: str
    primary_action: UiAction | None
    fallback_action: UiAction | None
    realtime_impact_key: str


def resolve_activity_session(snapshot: DashboardSnapshot):
    """Resolve the explicit running session or the most recent activity."""

    candidates = list(getattr(snapshot, "recent_sessions", ()))
    current_session = getattr(snapshot, "current_session", None)
    if (
        current_session is not None
        and all(
            item.thread_id != current_session.thread_id
            for item in candidates
        )
    ):
        candidates.insert(0, current_session)
    explicit_running = next((
        item for item in candidates
        if (
            item.status == "in_progress"
            and item.instruction is not None
            and item.instruction.in_progress
        )
    ), None)
    activity = explicit_running
    if activity is None:
        activity = current_session
    if activity is None and candidates:
        activity = candidates[0]
    return activity


def build_ui_scope_contract(snapshot: DashboardSnapshot) -> UiScopeContract:
    """Classify display scopes from already-loaded state only."""

    activity = resolve_activity_session(snapshot)
    activity_is_running = bool(
        activity is not None
        and activity.status == "in_progress"
        and activity.instruction is not None
        and activity.instruction.in_progress
    )
    activity_thread_id = activity.thread_id if activity is not None else None
    selected_is_pinned = bool(
        getattr(snapshot, "selection_mode", "auto") == "pinned"
        and getattr(snapshot, "selected_session", None) is not None
    )
    selected_thread_id = (
        snapshot.selected_session.thread_id
        if selected_is_pinned else None
    )
    return UiScopeContract(
        activity_scope=(
            UiDataScope.CURRENT_ACTIVITY
            if activity_is_running
            else UiDataScope.RECENT_ACTIVITY
        ),
        activity_thread_id=activity_thread_id,
        selected_scope=(
            UiDataScope.SELECTED_SESSION if selected_is_pinned else None
        ),
        selected_thread_id=selected_thread_id,
        selected_is_pinned=selected_is_pinned,
        selected_is_activity=(
            selected_is_pinned
            and activity_thread_id is not None
            and activity_thread_id == selected_thread_id
        ),
    )


def classify_history_empty_state(
    *,
    source_available: bool,
    has_any_history: bool,
    has_range_rows: bool,
    in_progress_observation_count: int,
    selection_required: bool,
    mapping_failed: bool,
    coverage_state: str,
    stale: bool,
    backfill_incomplete: bool,
    selected_session_without_history: bool = False,
) -> HistoryEmptyState:
    """Choose one actionable local-history state with stable precedence."""

    if not source_available:
        return HistoryEmptyState.UNAVAILABLE
    if mapping_failed:
        return HistoryEmptyState.MAPPING_FAILED
    if backfill_incomplete:
        return HistoryEmptyState.BACKFILL_INCOMPLETE
    if selection_required:
        return (
            HistoryEmptyState.NO_SELECTION
            if has_any_history else HistoryEmptyState.FIRST_USE
        )
    if not has_range_rows and in_progress_observation_count > 0:
        return HistoryEmptyState.IN_PROGRESS_ONLY
    if selected_session_without_history:
        return HistoryEmptyState.SELECTED_NO_HISTORY
    if not has_any_history:
        return HistoryEmptyState.FIRST_USE
    if not has_range_rows:
        return HistoryEmptyState.RANGE_EMPTY
    if stale:
        return HistoryEmptyState.STALE
    if coverage_state in {"limited_history", "partial", "unknown"}:
        return HistoryEmptyState.PARTIAL
    return HistoryEmptyState.AVAILABLE


def build_history_state_view(state: HistoryEmptyState) -> ActionableStateView:
    """Return stable i18n keys and bounded actions for one history state."""

    actions = {
        "refresh": UiAction("refresh", "manual_refresh"),
        "choose_session": UiAction(
            "choose_session", "choose_session", UiDataScope.SELECTED_SESSION,
        ),
        "expand_range": UiAction(
            "expand_range", "expand_history_range",
            UiDataScope.SELECTED_SESSION,
        ),
        "view_all": UiAction(
            "view_all", "view_all_tasks", UiDataScope.GLOBAL_SUMMARY,
        ),
        "view_activity": UiAction(
            "view_activity", "view_current_activity",
            UiDataScope.CURRENT_ACTIVITY,
        ),
        "view_coverage": UiAction("view_coverage", "view_coverage_details"),
        "use_current": UiAction("use_current", "continue_with_current_data"),
        "keep_ranking": UiAction("keep_ranking", "keep_current_ranking"),
        "retry": UiAction("retry", "retry_later"),
    }
    mapping = {
        HistoryEmptyState.AVAILABLE: (None, None),
        HistoryEmptyState.FIRST_USE: (actions["refresh"], None),
        HistoryEmptyState.SELECTED_NO_HISTORY: (
            actions["choose_session"], actions["expand_range"],
        ),
        HistoryEmptyState.RANGE_EMPTY: (actions["expand_range"], actions["view_all"]),
        HistoryEmptyState.IN_PROGRESS_ONLY: (
            actions["view_activity"], actions["refresh"],
        ),
        HistoryEmptyState.NO_SELECTION: (actions["choose_session"], actions["view_all"]),
        HistoryEmptyState.MAPPING_FAILED: (
            actions["expand_range"], actions["keep_ranking"],
        ),
        HistoryEmptyState.PARTIAL: (
            actions["view_coverage"], actions["use_current"],
        ),
        HistoryEmptyState.STALE: (actions["refresh"], None),
        HistoryEmptyState.BACKFILL_INCOMPLETE: (
            actions["use_current"], actions["retry"],
        ),
        HistoryEmptyState.UNAVAILABLE: (actions["refresh"], None),
    }
    primary, fallback = mapping[state]
    return ActionableStateView(
        kind=state,
        title_key=f"history_state_{state.value}_title",
        reason_key=f"history_state_{state.value}_reason",
        primary_action=primary,
        fallback_action=fallback,
        realtime_impact_key=f"history_state_{state.value}_impact",
    )


@dataclass(frozen=True)
class QuotaAvailabilityContract:
    state: QuotaAvailability
    five_hour_live_available: bool
    weekly_live_available: bool
    local_history_available: bool
    local_history_source_available: bool
    live_stale: bool


def classify_quota_availability(
    five_hour_available: bool,
    weekly_available: bool,
    history_available: bool,
    live_stale: bool,
    *,
    history_source_available: bool = True,
) -> QuotaAvailabilityContract:
    """Keep official live quota and local quota history visibly distinct."""

    live_available = five_hour_available or weekly_available
    if live_available and live_stale:
        state = QuotaAvailability.STALE_LIVE
    elif weekly_available and not five_hour_available:
        state = QuotaAvailability.WEEKLY_ONLY
    elif live_available and history_available:
        state = QuotaAvailability.LIVE_AND_HISTORY
    elif live_available:
        state = QuotaAvailability.LIVE_ONLY
    elif history_available:
        state = QuotaAvailability.HISTORY_ONLY
    else:
        state = QuotaAvailability.UNAVAILABLE
    return QuotaAvailabilityContract(
        state=state,
        five_hour_live_available=five_hour_available,
        weekly_live_available=weekly_available,
        local_history_available=history_available,
        local_history_source_available=history_source_available,
        live_stale=live_stale,
    )


def build_quota_state_view(
    contract: QuotaAvailabilityContract,
) -> ActionableStateView:
    """Describe one live/history quota combination without conflating sources."""

    refresh = UiAction("refresh_quota", "refresh_official_quota", UiDataScope.LIVE_QUOTA)
    history = UiAction(
        "view_quota_history", "view_local_quota_history",
        UiDataScope.LOCAL_QUOTA_HISTORY,
    )
    state = contract.state
    if state is QuotaAvailability.LIVE_AND_HISTORY:
        variant = "live_and_history"
        primary, fallback = history, None
    elif state is QuotaAvailability.LIVE_ONLY:
        variant = "live_only"
        primary, fallback = refresh, None
    elif state is QuotaAvailability.HISTORY_ONLY:
        variant = "history_only"
        primary, fallback = refresh, history
    elif state is QuotaAvailability.WEEKLY_ONLY:
        variant = "weekly_only"
        primary = refresh
        fallback = history if contract.local_history_available else None
    elif state is QuotaAvailability.STALE_LIVE:
        variant = "stale_live"
        primary = refresh
        fallback = history if contract.local_history_available else None
    elif contract.local_history_source_available:
        variant = "both_empty"
        primary, fallback = refresh, None
    else:
        variant = "sources_unavailable"
        primary, fallback = refresh, None
    return ActionableStateView(
        kind=state,
        title_key=f"quota_state_{variant}_title",
        reason_key=f"quota_state_{variant}_reason",
        primary_action=primary,
        fallback_action=fallback,
        realtime_impact_key=f"quota_state_{variant}_impact",
    )


def present_dashboard(
    snapshot: DashboardSnapshot,
    auto_refresh_enabled: bool,
    refreshing: bool = False,
    previous: DashboardPresentation | None = None,
) -> DashboardPresentation:
    if refreshing and previous is not None:
        return replace(
            previous, data_status=DataStatus.REFRESHING, status_tone=UiTone.FRESH,
            status_message="Refreshing", auto_refresh=format_auto_refresh(auto_refresh_enabled),
        )
    current_session = resolve_activity_session(snapshot)
    instruction = (
        current_session.instruction
        if current_session is not None else snapshot.rollout.instruction
    )
    status = display_session_status(current_session, instruction)
    data_status, tone = _data_status(status)
    cumulative = (
        current_session.thread_cumulative_usage
        if current_session else snapshot.rollout.thread_cumulative_usage
    )
    latest = _latest_metrics(instruction, status, cumulative)
    current, cache = _telemetry_current(instruction, cumulative)
    session_total = f"{cumulative.total_tokens:,}" if cumulative else "—"
    usage_scope = "instruction" if instruction is not None and instruction.usage is not None else (
        "thread_cumulative" if cumulative is not None else "unavailable"
    )
    model_calls = "—" if instruction is None or (instruction.usage is None and instruction.model_calls == 0) else str(instruction.model_calls)
    sources = (
        SourceDisplay("Data Source", "Local Codex", UiTone.FRESH if current_session else UiTone.UNKNOWN),
        SourceDisplay("Current Task", status, tone),
        SourceDisplay("Model Calls", model_calls, tone),
        SourceDisplay("Task Elapsed", _duration(instruction.duration_ms, instruction.in_progress) if instruction else "—", tone),
        SourceDisplay("Data Sync", snapshot.state_reconciliation, _reconciliation_tone(snapshot.state_reconciliation)),
    )
    recent = tuple(_recent_row(item) for item in snapshot.recent_sessions)
    return DashboardPresentation(
        data_status, tone, _status_message(status), latest, sources,
        _format_time(current_session.observed_at if current_session else snapshot.rollout.observed_at),
        _format_time(snapshot.sessions_result.refreshed_at if snapshot.sessions_result.sessions else snapshot.rollout.refreshed_at),
        format_auto_refresh(auto_refresh_enabled), recent, current, cache, session_total, usage_scope,
    )


def format_auto_refresh(enabled: bool, interval_seconds: int = 60) -> str:
    return f"Auto Refresh: {'On' if enabled else 'Off'} ({interval_seconds}s)"


def disambiguated_session_labels(
    rows: tuple[RecentSessionRow, ...],
    language: str,
    *,
    activity_thread_id: str | None = None,
    activity_is_running: bool = False,
    selected_thread_id: str | None = None,
) -> dict[str, str]:
    bases: list[tuple[str, str]] = []
    for row in rows:
        if row.thread_id == activity_thread_id:
            role_key = (
                "ui_scope_current_activity"
                if activity_is_running else "ui_scope_recent_activity"
            )
        else:
            role_key = "historical_session_role"
        base = safe_session_primary_label(
            row,
            language,
            role_key=role_key,
            viewing=row.thread_id == selected_thread_id,
        )
        bases.append((row.thread_id, base))
    counts: dict[str, int] = {}
    result: dict[str, str] = {}
    for thread_id, base in bases:
        counts[base] = counts.get(base, 0) + 1
        result[thread_id] = base if counts[base] == 1 else f"{base} · {counts[base]}"
    return result


def safe_session_primary_label(
    session: object,
    language: str,
    *,
    role_key: str,
    viewing: bool,
) -> str:
    """Use the safe app-server title, or a metadata-only fallback."""

    observed_at = getattr(session, "last_activity", None) or getattr(
        session, "observed_at", None,
    )
    time_label = (
        observed_at.astimezone().strftime("%m-%d %H:%M")
        if observed_at is not None else "—"
    )
    turn_count = getattr(session, "turn_count", 0)
    turns = (
        translate("task_turns_value", language, value=turn_count)
        if isinstance(turn_count, int) and turn_count > 0
        else translate("session_turn_unknown", language)
    )
    title_source = getattr(session, "title_source", "")
    title = getattr(session, "full_title", None) or getattr(
        session, "display_title", None,
    )
    if title_source == "codex_app_server.thread_display_title" and title:
        return " ".join(str(title).split())
    return translate(
        "safe_session_primary",
        language,
        role=translate(role_key, language),
        time=time_label,
        turns=turns,
        viewing="",
    )


def _recent_row(session) -> RecentSessionRow:
    cumulative = session.thread_cumulative_usage
    hit = "—"
    if cumulative is not None and cumulative.input_tokens:
        hit = f"{cumulative.cached_input_tokens / cumulative.input_tokens * 100:.1f}%"
    title_source = getattr(session, "title_source", "safe timestamp metadata")
    safe_title = (
        getattr(session, "full_title", None) or getattr(session, "display_title", None)
        if title_source == "codex_app_server.thread_display_title"
        else f"Codex Session · {session.observed_at.astimezone().strftime('%m-%d %H:%M')}"
    )
    return RecentSessionRow(
        session.thread_id, safe_title, title_source,
        display_session_status(session, session.instruction),
        session.observed_at, f"{cumulative.total_tokens:,}" if cumulative else "—", hit,
        getattr(session, "turn_count", 0),
        safe_title,
        cumulative.total_tokens if cumulative else None,
    )


def _latest_metrics(instruction, status: str, cumulative=None) -> tuple[MetricDisplay, ...]:
    labels = ("Input", "Output", "Total", "Cached", "Reasoning", "Cache Hit")
    usage = instruction.usage if instruction is not None else None
    cumulative_fallback = usage is None and cumulative is not None
    if usage is None and not cumulative_fallback:
        return tuple(MetricDisplay(label, "—", "Unavailable", UiTone.UNKNOWN) for label in labels)
    usage = cumulative if cumulative_fallback else usage
    assert usage is not None
    hit = "—" if usage.input_tokens == 0 else f"{usage.cached_input_tokens / usage.input_tokens * 100:.1f}%"
    tone = UiTone.ESTIMATE if status == "completed_partial" or (instruction is not None and instruction.unreconciled_events) else (UiTone.STALE if status == "incomplete" else UiTone.FRESH)
    if cumulative_fallback:
        tone = UiTone.STALE
    values = (usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.cached_input_tokens)
    details = (
        "All model-call input context for this instruction",
        "All model-call output for this instruction, including Reasoning",
        "Input + Output for this instruction",
        "Cached subset of this instruction Input",
    )
    if cumulative_fallback:
        details = ("Thread cumulative usage; latest instruction unavailable",) * 4
    metrics = [MetricDisplay(label, f"{value:,}", detail, tone) for label, value, detail in zip(labels[:4], values, details)]
    reasoning_detail = "Thread cumulative usage; latest instruction unavailable" if cumulative_fallback else "Reasoning subset of this instruction Output"
    metrics.append(MetricDisplay("Reasoning", f"{usage.reasoning_output_tokens:,}", reasoning_detail, tone))
    cache_detail = "Thread cumulative usage; latest instruction unavailable" if cumulative_fallback else "Derived from Input; not an official rate"
    metrics.append(MetricDisplay("Cache Hit", hit, cache_detail, tone if hit != "—" else UiTone.UNKNOWN))
    return tuple(metrics)
def _telemetry_current(instruction, cumulative=None) -> tuple[str, str]:
    usage = instruction.usage if instruction is not None else None
    if usage is None:
        usage = cumulative
    if usage is None:
        return "—", "—"
    hit = "—" if usage.input_tokens == 0 else f"{usage.cached_input_tokens / usage.input_tokens * 100:.1f}% derived"
    return f"{usage.total_tokens:,}", hit


def _data_status(status: str) -> tuple[DataStatus, UiTone]:
    if status == "in_progress":
        return DataStatus.RUNNING, UiTone.FRESH
    if status == "exact":
        return DataStatus.COMPLETED, UiTone.FRESH
    if status == "completed_partial":
        return DataStatus.COMPLETED_PARTIAL, UiTone.ESTIMATE
    if status == "incomplete":
        return DataStatus.INCOMPLETE, UiTone.STALE
    return DataStatus.UNAVAILABLE, UiTone.UNKNOWN


def _status_message(status: str) -> str:
    if status == "completed_partial":
        return "The task ended, but some instruction tokens could not be reconciled exactly. Verified data is shown."
    if status == "incomplete":
        return "The completion boundary or usage reconciliation is incomplete. Available data is shown."
    return status


def _duration(value: int | None, in_progress: bool = False) -> str:
    if in_progress:
        return "Calculating"
    if value is None:
        return "—"
    seconds = max(round(value / 1000), 0)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def _format_time(value) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S") if value else "—"


def _reconciliation_tone(value: str) -> UiTone:
    return UiTone.FRESH if value == "reconciled" else (UiTone.ERROR if value == "mismatch" else UiTone.UNKNOWN)
