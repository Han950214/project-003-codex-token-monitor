"""Pure conversion from a selected Codex session to Dashboard display values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from app.dashboard import DashboardSnapshot, display_session_status, instruction_usage


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
    instruction = instruction_usage(snapshot)
    status = display_session_status(snapshot.selected_session, instruction)
    data_status, tone = _data_status(status)
    cumulative = (
        snapshot.selected_session.thread_cumulative_usage
        if snapshot.selected_session else snapshot.rollout.thread_cumulative_usage
    )
    latest = _latest_metrics(instruction, status, cumulative)
    current, cache = _telemetry_current(instruction, cumulative)
    session_total = f"{cumulative.total_tokens:,}" if cumulative else "—"
    usage_scope = "instruction" if instruction is not None and instruction.usage is not None else (
        "thread_cumulative" if cumulative is not None else "unavailable"
    )
    model_calls = "—" if instruction is None or (instruction.usage is None and instruction.model_calls == 0) else str(instruction.model_calls)
    sources = (
        SourceDisplay("Data Source", "Local Codex", UiTone.FRESH if snapshot.selected_session else UiTone.UNKNOWN),
        SourceDisplay("Current Task", status, tone),
        SourceDisplay("Model Calls", model_calls, tone),
        SourceDisplay("Task Elapsed", _duration(instruction.duration_ms, instruction.in_progress) if instruction else "—", tone),
        SourceDisplay("Data Sync", snapshot.state_reconciliation, _reconciliation_tone(snapshot.state_reconciliation)),
    )
    recent = tuple(_recent_row(item) for item in snapshot.recent_sessions)
    return DashboardPresentation(
        data_status, tone, _status_message(status), latest, sources,
        _format_time(snapshot.selected_session.observed_at if snapshot.selected_session else snapshot.rollout.observed_at),
        _format_time(snapshot.sessions_result.refreshed_at if snapshot.sessions_result.sessions else snapshot.rollout.refreshed_at),
        format_auto_refresh(auto_refresh_enabled), recent, current, cache, session_total, usage_scope,
    )


def format_auto_refresh(enabled: bool, interval_seconds: int = 60) -> str:
    return f"Auto Refresh: {'On' if enabled else 'Off'} ({interval_seconds}s)"


def disambiguated_session_labels(
    rows: tuple[RecentSessionRow, ...], language: str
) -> dict[str, str]:
    bases: list[tuple[str, str]] = []
    for row in rows:
        title = row.display_title
        if row.title_source == "safe timestamp fallback":
            stamp = row.last_activity.astimezone().strftime("%m-%d %H:%M") if row.last_activity else "—"
            title = f"Codex 会话 · {stamp}" if language == "zh-CN" else f"Codex Session · {stamp}"
            base = title
        else:
            time_label = row.last_activity.astimezone().strftime("%H:%M") if row.last_activity else "—"
            base = f"{title} · {time_label}"
        bases.append((row.thread_id, base))
    counts: dict[str, int] = {}
    result: dict[str, str] = {}
    for thread_id, base in bases:
        counts[base] = counts.get(base, 0) + 1
        result[thread_id] = base if counts[base] == 1 else f"{base} · {counts[base]}"
    return result


def _recent_row(session) -> RecentSessionRow:
    cumulative = session.thread_cumulative_usage
    hit = "—"
    if cumulative is not None and cumulative.input_tokens:
        hit = f"{cumulative.cached_input_tokens / cumulative.input_tokens * 100:.1f}%"
    return RecentSessionRow(
        session.thread_id, session.display_title, session.title_source,
        display_session_status(session, session.instruction),
        session.observed_at, f"{cumulative.total_tokens:,}" if cumulative else "—", hit,
    )


def _latest_metrics(instruction, status: str, cumulative=None) -> tuple[MetricDisplay, ...]:
    labels = ("Input", "Output", "Current Total", "Cached", "Session Total", "Cache Hit")
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
    session_value = f"{cumulative.total_tokens:,}" if cumulative is not None else "—"
    session_detail = "Thread cumulative usage; latest instruction unavailable" if cumulative_fallback else "Selected session cumulative total"
    metrics.append(MetricDisplay("Session Total", session_value, session_detail, tone if cumulative is not None else UiTone.UNKNOWN))
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
