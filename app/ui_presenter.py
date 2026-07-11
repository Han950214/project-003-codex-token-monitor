"""Pure, immutable conversion from Rollout snapshots to Dashboard display values."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from app.dashboard import DashboardSnapshot, instruction_usage
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
    title: str; model: str; mode: str; input_tokens: str; output_tokens: str; cached_tokens: str; total_tokens: str; ended_at: str
    def values(self) -> tuple[str, ...]:
        return (self.title, self.model, self.mode, self.input_tokens, self.output_tokens, self.cached_tokens, self.total_tokens, self.ended_at)


@dataclass(frozen=True)
class DashboardPresentation:
    data_status: DataStatus; status_tone: UiTone; status_message: str; latest_usage: tuple[MetricDisplay, ...]
    source_details: tuple[SourceDisplay, ...]; last_event: str; last_refresh: str; auto_refresh: str
    manual_runs: tuple[ManualRunRow, ...]; telemetry_current_total: str; telemetry_cache_hit: str; telemetry_session_total: str


def present_dashboard(snapshot: DashboardSnapshot, auto_refresh_enabled: bool, refreshing: bool = False, previous: DashboardPresentation | None = None) -> DashboardPresentation:
    if refreshing and previous is not None:
        return replace(previous, data_status=DataStatus.REFRESHING, status_tone=UiTone.FRESH, status_message="Previous values remain visible while new usage is loaded.", auto_refresh=format_auto_refresh(auto_refresh_enabled))
    instruction = instruction_usage(snapshot)
    if instruction is None:
        status, tone, message = DataStatus.NO_DATA, UiTone.UNKNOWN, "Rollout instruction usage is unavailable."
    elif instruction.in_progress:
        status, tone, message = DataStatus.FRESH_REAL, UiTone.FRESH, "Instruction is in progress; verified values can still increase."
    elif instruction.exact:
        status, tone, message = DataStatus.FRESH_REAL, UiTone.FRESH, "Exact instruction usage is available from the Codex rollout."
    else:
        status, tone, message = DataStatus.NO_DATA, UiTone.UNKNOWN, "Rollout instruction usage is incomplete."
    latest = _latest_metrics(instruction)
    current, cache = _telemetry_current(instruction)
    session_total = f"{snapshot.state_total.total_tokens:,}" if snapshot.state_total else "—"
    session_source = "codex_state_sqlite / same rollout thread" if snapshot.state_total else "unavailable"
    result = DashboardPresentation(
        status, tone, message, latest,
        (
            SourceDisplay("Rollout File", snapshot.rollout.rollout_filename or "—", tone),
            SourceDisplay("Thread", snapshot.rollout.thread_suffix or "—", tone),
            SourceDisplay("Instruction Status", instruction.status if instruction else "unavailable", tone),
            SourceDisplay("Model Calls", str(instruction.model_calls) if instruction else "—", tone),
            SourceDisplay("Instruction Elapsed", _duration(instruction.duration_ms) if instruction else "—", tone),
            SourceDisplay("State/Rollout", "reconciled" if snapshot.state_reconciled else "unavailable", UiTone.FRESH if snapshot.state_reconciled else UiTone.UNKNOWN),
        ),
        "—", "—", format_auto_refresh(auto_refresh_enabled), tuple(manual_run_row(run) for run in snapshot.runs), current, cache, session_total,
    )
    return replace(result, data_status=DataStatus.REFRESHING, status_tone=UiTone.FRESH, status_message="Previous values remain visible while new usage is loaded.") if refreshing else result


def format_auto_refresh(enabled: bool, interval_seconds: int = 60) -> str:
    return f"Auto Refresh: {'On' if enabled else 'Off'} ({interval_seconds}s)"


def manual_run_row(run: AgentRun) -> ManualRunRow:
    return ManualRunRow(run.title, run.model, run.mode, str(run.input_tokens), str(run.output_tokens), str(run.cached_tokens), str(run.total_tokens), run.ended_at)


def _latest_metrics(instruction) -> tuple[MetricDisplay, ...]:
    if instruction is None or instruction.usage is None:
        return tuple(MetricDisplay(label, "—", "Rollout unavailable", UiTone.UNKNOWN) for label in ("Input", "Output", "Total", "Cached", "Reasoning", "Cache Hit"))
    usage = instruction.usage
    hit = "—" if usage.input_tokens == 0 else f"{usage.cached_input_tokens / usage.input_tokens * 100:.1f}%"
    detail = "Verified instruction usage; still growing" if instruction.in_progress else "Exact instruction usage"
    values = (usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.cached_input_tokens, usage.reasoning_output_tokens)
    metrics = [MetricDisplay(label, f"{value:,}", detail, UiTone.FRESH) for label, value in zip(("Input", "Output", "Total", "Cached", "Reasoning"), values)]
    metrics.append(MetricDisplay("Cache Hit", hit, "Derived from Input; not an official rate", UiTone.FRESH if hit != "—" else UiTone.UNKNOWN))
    return tuple(metrics)


def _telemetry_current(instruction) -> tuple[str, str]:
    if instruction is None or instruction.usage is None:
        return "—", "—"
    usage = instruction.usage
    hit = "—" if usage.input_tokens == 0 else f"{usage.cached_input_tokens / usage.input_tokens * 100:.1f}% derived"
    return f"{usage.total_tokens:,}", hit


def _duration(value: int | None) -> str:
    return f"{value / 1000:.1f}s" if value is not None else "—"
