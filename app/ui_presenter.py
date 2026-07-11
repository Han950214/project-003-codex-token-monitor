"""Pure, immutable conversion from runtime snapshots to Dashboard display values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from app.codex_logs import LogsAdapterStatus
from app.dashboard import DashboardSnapshot
from app.models import AgentRun


class UiTone(str, Enum):
    FRESH = "fresh"
    ESTIMATE = "estimate"
    STALE = "stale"
    ERROR = "error"
    UNKNOWN = "unknown"
    DISABLED = "disabled"


class DataStatus(str, Enum):
    FRESH_REAL = "Fresh · Real"
    LOCAL_ESTIMATE = "Local Estimate"
    NO_DATA = "No Data"
    REFRESHING = "Refreshing"
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
class ManualRunRow:
    title: str
    model: str
    mode: str
    input_tokens: str
    output_tokens: str
    cached_tokens: str
    total_tokens: str
    ended_at: str

    def values(self) -> tuple[str, ...]:
        return (
            self.title,
            self.model,
            self.mode,
            self.input_tokens,
            self.output_tokens,
            self.cached_tokens,
            self.total_tokens,
            self.ended_at,
        )


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
    manual_runs: tuple[ManualRunRow, ...]
    telemetry_current_total: str
    telemetry_cache_hit: str
    telemetry_session_total: str


LOGS_ERROR_STATUSES = {
    LogsAdapterStatus.DATABASE_MISSING,
    LogsAdapterStatus.OPEN_FAILED,
    LogsAdapterStatus.PARSE_FAILED,
}


def present_dashboard(
    snapshot: DashboardSnapshot,
    auto_refresh_enabled: bool,
    refreshing: bool = False,
    previous: DashboardPresentation | None = None,
) -> DashboardPresentation:
    """Build display-only values without reading or mutating any data source."""
    if refreshing and previous is not None:
        return replace(
            previous,
            data_status=DataStatus.REFRESHING,
            status_tone=UiTone.FRESH,
            status_message="Previous values remain visible while new usage is loaded.",
            auto_refresh=format_auto_refresh(auto_refresh_enabled),
        )

    logs = snapshot.logs
    has_real = logs.usage is not None and logs.status == LogsAdapterStatus.CONNECTED
    has_runs = bool(snapshot.runs)
    logs_error = logs.status in LOGS_ERROR_STATUSES

    if logs_error:
        status = DataStatus.LOGS_ERROR
        tone = UiTone.ERROR
        message = "Response usage is unavailable because the logs adapter could not read valid data."
    elif has_real:
        status = DataStatus.FRESH_REAL
        tone = UiTone.FRESH
        message = "Latest response usage is available from Codex logs."
    elif has_runs:
        status = DataStatus.LOCAL_ESTIMATE
        tone = UiTone.ESTIMATE
        message = "Showing the latest user-saved manual Run as a local estimate."
    else:
        status = DataStatus.NO_DATA
        tone = UiTone.UNKNOWN
        message = "No response usage is available yet. Use Manual Refresh or wait for Codex usage data."

    latest = _latest_metrics(snapshot, status)
    current_total, cache_hit = _telemetry_current(snapshot)
    session_total, session_source, session_tone = _session_total(snapshot)
    last_event = _format_time(logs.observed_at) if logs.observed_at else "—"
    last_refresh = _format_time(logs.refreshed_at)
    sources = (
        SourceDisplay("Session Total", session_total, session_tone),
        SourceDisplay("Usage Source", logs.source if has_real else ("local estimate" if has_runs and not logs_error else "unknown"), tone),
        SourceDisplay("Session Source", session_source, session_tone),
        SourceDisplay("Logs Adapter", logs.status.value, UiTone.ERROR if logs_error else tone),
        SourceDisplay("State Adapter", "Available" if snapshot.state_total is not None else "No state total available", UiTone.FRESH if snapshot.state_total is not None else UiTone.UNKNOWN),
        SourceDisplay("Freshness / Time", last_event if logs.observed_at else last_refresh, tone),
    )
    result = DashboardPresentation(
        data_status=status,
        status_tone=tone,
        status_message=message,
        latest_usage=latest,
        source_details=sources,
        last_event=last_event,
        last_refresh=last_refresh,
        auto_refresh=format_auto_refresh(auto_refresh_enabled),
        manual_runs=tuple(manual_run_row(run) for run in snapshot.runs),
        telemetry_current_total=current_total,
        telemetry_cache_hit=cache_hit,
        telemetry_session_total=session_total,
    )
    if refreshing:
        return replace(
            result,
            data_status=DataStatus.REFRESHING,
            status_tone=UiTone.FRESH,
            status_message="Previous values remain visible while new usage is loaded.",
        )
    return result


def format_auto_refresh(enabled: bool, interval_seconds: int = 60) -> str:
    return f"Auto Refresh: {'On' if enabled else 'Off'} ({interval_seconds}s)"


def manual_run_row(run: AgentRun) -> ManualRunRow:
    return ManualRunRow(
        title=run.title,
        model=run.model,
        mode=run.mode,
        input_tokens=str(run.input_tokens),
        output_tokens=str(run.output_tokens),
        cached_tokens=str(run.cached_tokens),
        total_tokens=str(run.total_tokens),
        ended_at=run.ended_at,
    )


def _latest_metrics(snapshot: DashboardSnapshot, status: DataStatus) -> tuple[MetricDisplay, ...]:
    usage = snapshot.logs.usage
    if status == DataStatus.FRESH_REAL and usage is not None:
        hit = "—" if usage.input_tokens <= 0 else f"{min(usage.cached_tokens, usage.input_tokens) / usage.input_tokens * 100:.1f}%"
        raw = (usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.cached_tokens, usage.reasoning_tokens)
        metrics = [MetricDisplay(label, f"{value:,}", "Real usage", UiTone.FRESH) for label, value in zip(("Input", "Output", "Total", "Cached", "Reasoning"), raw)]
        metrics.append(MetricDisplay("Cache Hit", hit, "Derived from real usage; not an official rate", UiTone.FRESH if hit != "—" else UiTone.UNKNOWN))
        return tuple(metrics)
    if status == DataStatus.LOCAL_ESTIMATE and snapshot.runs:
        run = snapshot.runs[-1]
        return (
            MetricDisplay("Input", f"{run.input_tokens:,}", "Local estimate", UiTone.ESTIMATE),
            MetricDisplay("Output", f"{run.output_tokens:,}", "Local estimate", UiTone.ESTIMATE),
            MetricDisplay("Total", f"{run.total_tokens:,}", "Local estimate", UiTone.ESTIMATE),
            MetricDisplay("Cached", f"{run.cached_tokens:,}", "Local estimate", UiTone.ESTIMATE),
            MetricDisplay("Reasoning", "—", "Not recorded by manual Runs", UiTone.UNKNOWN),
            MetricDisplay("Cache Hit", f"{run.cache_hit * 100:.1f}%", "Local estimate; not real Codex cache", UiTone.ESTIMATE),
        )
    detail = "Logs adapter unavailable" if status == DataStatus.LOGS_ERROR else "Unknown"
    return tuple(MetricDisplay(label, "—", detail, UiTone.ERROR if status == DataStatus.LOGS_ERROR else UiTone.UNKNOWN) for label in ("Input", "Output", "Total", "Cached", "Reasoning", "Cache Hit"))


def _telemetry_current(snapshot: DashboardSnapshot) -> tuple[str, str]:
    usage = snapshot.logs.usage
    if usage is not None and snapshot.logs.status == LogsAdapterStatus.CONNECTED:
        hit = "—" if usage.input_tokens <= 0 else f"{min(usage.cached_tokens, usage.input_tokens) / usage.input_tokens * 100:.1f}% derived"
        return f"{usage.total_tokens:,} real", hit
    if snapshot.runs:
        run = snapshot.runs[-1]
        return f"{run.total_tokens:,} estimate", f"{run.cache_hit * 100:.1f}% estimate"
    return "—", "—"


def _session_total(snapshot: DashboardSnapshot) -> tuple[str, str, UiTone]:
    if snapshot.state_total is not None:
        return f"{snapshot.state_total.total_tokens:,}", "codex_state_sqlite / real total", UiTone.FRESH
    if snapshot.runs:
        return f"{snapshot.summary.session_tokens:,} estimate", "local estimate", UiTone.ESTIMATE
    return "—", "unknown", UiTone.UNKNOWN


def _format_time(value) -> str:
    return value.astimezone().isoformat(timespec="seconds")
