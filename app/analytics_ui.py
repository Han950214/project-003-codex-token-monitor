"""Provider-neutral analytics DTOs and deterministic trend summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from app.dashboard import DashboardSnapshot
    from app.history import HistoryQueryResult


TREND_QUALITY_STATES = ("empty", "available", "insufficient", "unavailable", "stale")
TREND_STALE_AFTER = timedelta(minutes=3)


@dataclass(frozen=True)
class SafeTrendSample:
    """One existing safe numeric session sample; no content fields are accepted."""

    observed_at: datetime
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int
    reasoning_tokens: int
    session_total_tokens: int | None
    turn_count: int
    cache_reuse_percent: float | None


@dataclass(frozen=True)
class TrendView:
    """Provider-neutral input shared by trend, overview, and Advisor UI."""

    range_days: int
    quality: str
    samples: tuple[object, ...]
    refreshed_at: datetime | None
    quota_samples: tuple[object, ...] = ()
    metrics_available: tuple[str, ...] = ()
    start_at: datetime | None = None
    end_at: datetime | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.range_days not in {7, 30, 90}:
            raise ValueError("unsupported_trend_range")
        if self.quality not in TREND_QUALITY_STATES:
            raise ValueError("unsupported_trend_quality")


def classify_trend_quality(
    sample_count: int,
    *,
    source_available: bool,
    refreshed_at: datetime | None,
    now: datetime,
) -> str:
    """Classify real sample availability without filling gaps or inventing points."""

    if not source_available or refreshed_at is None:
        return "unavailable"
    if now - refreshed_at > TREND_STALE_AFTER:
        return "stale"
    if sample_count == 0:
        return "empty"
    if sample_count < 2:
        return "insufficient"
    return "available"


@dataclass(frozen=True)
class TrendMetricSummary:
    metric: str
    current: float | None
    minimum: float | None
    maximum: float | None
    change: float | None
    sample_count: int
    start_at: datetime | None
    end_at: datetime | None
    scope: str
    derived: bool = False


_METRIC_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "total": "total_tokens",
    "cached": "cached_tokens",
    "reasoning": "reasoning_tokens",
    "session_total": "session_total_tokens",
    "turn_count": "turn_count",
    "five_hour": "five_hour_remaining_percent",
    "weekly": "weekly_remaining_percent",
}


def trend_view_from_query(result: "HistoryQueryResult") -> TrendView:
    """Convert the local-store query contract without touching source data."""

    return TrendView(
        result.range_days,
        result.status,
        tuple(result.samples),
        result.end_at,
        tuple(result.quota_samples),
        tuple(result.metrics_available),
        result.start_at,
        result.end_at,
        result.error_code,
    )


def metric_samples(view: TrendView, metric: str) -> tuple[tuple[object, float], ...]:
    """Return real non-missing metric values in chronological query order."""

    source: Iterable[object] = (
        view.quota_samples if metric in {"five_hour", "weekly"} else view.samples
    )
    values: list[tuple[object, float]] = []
    for sample in source:
        if getattr(sample, "legacy_unknown_time", False):
            continue
        if metric in {"five_hour", "weekly"}:
            prefix = "five_hour" if metric == "five_hour" else "weekly"
            if getattr(sample, f"{prefix}_available", True) is False:
                continue
        elif getattr(sample, "source_available", True) is False:
            continue
        value = _metric_value(sample, metric)
        if value is not None:
            values.append((sample, value))
    return tuple(values)


def summarize_metric(view: TrendView, metric: str) -> TrendMetricSummary:
    values = metric_samples(view, metric)
    numbers = [value for _, value in values]
    timestamps = [
        getattr(sample, "sampled_at", getattr(sample, "observed_at", None))
        for sample, _ in values
    ]
    return TrendMetricSummary(
        metric=metric,
        current=numbers[-1] if numbers else None,
        minimum=min(numbers) if numbers else None,
        maximum=max(numbers) if numbers else None,
        change=(numbers[-1] - numbers[-2]) if len(numbers) >= 2 else None,
        sample_count=len(numbers),
        start_at=timestamps[0] if timestamps else None,
        end_at=timestamps[-1] if timestamps else None,
        scope="global" if metric in {"five_hour", "weekly"} else "thread",
        derived=metric == "cache_reuse",
    )


def _metric_value(sample: object, metric: str) -> float | None:
    if metric == "cache_reuse":
        ratio = getattr(sample, "cache_reuse_ratio", None)
        if ratio is not None:
            return float(ratio) * 100.0
        percent = getattr(sample, "cache_reuse_percent", None)
        return None if percent is None else float(percent)
    field = _METRIC_FIELDS.get(metric)
    if field is None:
        raise ValueError("unsupported_trend_metric")
    value = getattr(sample, field, None)
    return None if value is None else float(value)


def build_trend_view(
    snapshot: "DashboardSnapshot | None",
    range_days: int,
    *,
    now: datetime | None = None,
) -> TrendView:
    """Project an existing snapshot into safe chronological trend samples."""

    if range_days not in {7, 30, 90}:
        range_days = 7
    now = now or datetime.now(timezone.utc)
    if snapshot is None:
        return TrendView(range_days, "unavailable", (), None)

    cutoff = now - timedelta(days=range_days)
    samples: list[SafeTrendSample] = []
    for session in reversed(snapshot.recent_sessions):
        if session.observed_at < cutoff:
            continue
        instruction = session.instruction
        usage = instruction.usage if instruction is not None else None
        if usage is None:
            continue
        reuse = None
        if usage.input_tokens > 0:
            reuse = usage.cached_input_tokens / usage.input_tokens * 100.0
        cumulative = session.thread_cumulative_usage
        samples.append(SafeTrendSample(
            observed_at=session.observed_at,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            reasoning_tokens=usage.reasoning_output_tokens,
            session_total_tokens=(cumulative.total_tokens if cumulative is not None else None),
            turn_count=session.turn_count,
            cache_reuse_percent=reuse,
        ))

    refreshed_at = snapshot.sessions_result.refreshed_at
    source_available = bool(snapshot.recent_sessions) or snapshot.rollout.available
    quality = classify_trend_quality(
        len(samples), source_available=source_available,
        refreshed_at=refreshed_at, now=now,
    )
    return TrendView(range_days, quality, tuple(samples), refreshed_at)
