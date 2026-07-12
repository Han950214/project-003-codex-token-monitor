"""Pure conversion from a selected Codex session to Dashboard display values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from app.dashboard import DashboardSnapshot, instruction_usage


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
    status = (
        snapshot.selected_session.status if snapshot.selected_session
        else (instruction.status if instruction else "unavailable")
    )
    data_status, tone = _data_status(status)
    latest = _latest_metrics(instruction)
    current, cache = _telemetry_current(instruction)
    cumulative = (
        snapshot.selected_session.thread_cumulative_usage
        if snapshot.selected_session else snapshot.rollout.thread_cumulative_usage
    )
    session_total = f"{cumulative.total_tokens:,}" if cumulative else "—"
    sources = (
        SourceDisplay("Data Source", "Local Codex", UiTone.FRESH if snapshot.selected_session else UiTone.UNKNOWN),
        SourceDisplay("Current Task", status, tone),
        SourceDisplay("Model Calls", str(instruction.model_calls) if instruction else "—", tone),
        SourceDisplay("Task Elapsed", _duration(instruction.duration_ms, instruction.in_progress) if instruction else "—", tone),
        SourceDisplay("Data Sync", snapshot.state_reconciliation, _reconciliation_tone(snapshot.state_reconciliation)),
    )
    recent = tuple(_recent_row(item) for item in snapshot.recent_sessions)
    return DashboardPresentation(
        data_status, tone, status, latest, sources,
        _format_time(snapshot.selected_session.observed_at if snapshot.selected_session else snapshot.rollout.observed_at),
        _format_time(snapshot.sessions_result.refreshed_at if snapshot.sessions_result.sessions else snapshot.rollout.refreshed_at),
        format_auto_refresh(auto_refresh_enabled), recent, current, cache, session_total,
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
        session.thread_id, session.display_title, session.title_source, session.status,
        session.observed_at, f"{cumulative.total_tokens:,}" if cumulative else "—", hit,
    )


def _latest_metrics(instruction) -> tuple[MetricDisplay, ...]:
    if instruction is None or instruction.usage is None:
        return tuple(MetricDisplay(label, "—", "Unavailable", UiTone.UNKNOWN) for label in ("Input", "Output", "Total", "Cached", "Reasoning", "Cache Hit"))
    usage = instruction.usage
    hit = "—" if usage.input_tokens == 0 else f"{usage.cached_input_tokens / usage.input_tokens * 100:.1f}%"
    tone = UiTone.ESTIMATE if instruction.unreconciled_events else UiTone.FRESH
    values = (usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.cached_input_tokens, usage.reasoning_output_tokens)
    details = (
        "All model-call input context for this instruction",
        "All model-call output for this instruction, including Reasoning",
        "Input + Output for this instruction",
        "Cached subset of this instruction Input",
        "Reasoning subset of this instruction Output",
    )
    metrics = [MetricDisplay(label, f"{value:,}", detail, tone) for label, value, detail in zip(("Input", "Output", "Total", "Cached", "Reasoning"), values, details)]
    metrics.append(MetricDisplay("Cache Hit", hit, "Derived from Input; not an official rate", tone if hit != "—" else UiTone.UNKNOWN))
    return tuple(metrics)


def _telemetry_current(instruction) -> tuple[str, str]:
    if instruction is None or instruction.usage is None:
        return "—", "—"
    usage = instruction.usage
    hit = "—" if usage.input_tokens == 0 else f"{usage.cached_input_tokens / usage.input_tokens * 100:.1f}% derived"
    return f"{usage.total_tokens:,}", hit


def _data_status(status: str) -> tuple[DataStatus, UiTone]:
    if status == "in_progress":
        return DataStatus.RUNNING, UiTone.FRESH
    if status == "exact":
        return DataStatus.COMPLETED, UiTone.FRESH
    if status == "incomplete":
        return DataStatus.INCOMPLETE, UiTone.ERROR
    return DataStatus.UNAVAILABLE, UiTone.UNKNOWN


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
