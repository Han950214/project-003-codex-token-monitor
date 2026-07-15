"""Safe, in-memory analytics DTOs for the Phase 3.1-B1 UI.

This module accepts only the existing Dashboard snapshot.  It never reads
Rollout, SQLite, app-server, quota providers, or a persistence layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.dashboard import DashboardSnapshot


TREND_QUALITY_STATES = ("available", "insufficient", "unavailable", "stale")
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
    """Provider-neutral input for the Usage Trends page and future B2 charts."""

    range_days: int
    quality: str
    samples: tuple[SafeTrendSample, ...]
    refreshed_at: datetime | None

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
    if sample_count < 2:
        return "insufficient"
    return "available"


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
