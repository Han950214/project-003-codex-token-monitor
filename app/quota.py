"""Safe quota domain contract shared by providers and UI surfaces."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite


PERCENT_TOLERANCE = 0.5


class QuotaKind(str, Enum):
    FIVE_HOUR = "five_hour"
    WEEKLY = "weekly"


@dataclass(frozen=True)
class QuotaWindow:
    kind: QuotaKind
    used_percent: float | None
    remaining_percent: float | None
    reset_at: datetime | None
    observed_at: datetime
    source: str
    available: bool
    stale: bool = False
    error_code: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if self.reset_at is not None:
            _require_aware(self.reset_at, "reset_at")
        used = _percent(self.used_percent)
        remaining = _percent(self.remaining_percent)
        error = self.error_code
        available = bool(self.available)
        if used is None and remaining is not None:
            used = 100.0 - remaining
        elif remaining is None and used is not None:
            remaining = 100.0 - used
        elif used is not None and remaining is not None:
            if abs((used + remaining) - 100.0) > PERCENT_TOLERANCE:
                used = remaining = None
                available = False
                error = "percentage_mismatch"
        if used is None or remaining is None:
            available = False
            error = error or "percentage_unavailable"
        if self.reset_at is None:
            available = False
            error = error or "reset_unavailable"
        stale = bool(self.stale or (self.reset_at is not None and self.reset_at <= self.observed_at))
        object.__setattr__(self, "used_percent", used)
        object.__setattr__(self, "remaining_percent", remaining)
        object.__setattr__(self, "available", available)
        object.__setattr__(self, "stale", stale)
        object.__setattr__(self, "error_code", error)

    @classmethod
    def unavailable(
        cls,
        kind: QuotaKind,
        observed_at: datetime,
        source: str,
        error_code: str = "quota_unavailable",
    ) -> "QuotaWindow":
        return cls(kind, None, None, None, observed_at, source, False, False, error_code)

    @classmethod
    def from_reset_duration(
        cls,
        kind: QuotaKind,
        *,
        used_percent: float | None,
        remaining_percent: float | None,
        reset_after: timedelta,
        observed_at: datetime,
        source: str,
    ) -> "QuotaWindow":
        _require_aware(observed_at, "observed_at")
        return cls(
            kind,
            used_percent,
            remaining_percent,
            observed_at + reset_after,
            observed_at,
            source,
            True,
        )

    def as_stale(self, error_code: str) -> "QuotaWindow":
        return replace(self, stale=True, error_code=error_code)


@dataclass(frozen=True)
class CodexQuotaSnapshot:
    five_hour: QuotaWindow
    weekly: QuotaWindow
    refreshed_at: datetime
    source_status: str

    def __post_init__(self) -> None:
        _require_aware(self.refreshed_at, "refreshed_at")
        if self.five_hour.kind != QuotaKind.FIVE_HOUR:
            raise ValueError("five_hour_kind_invalid")
        if self.weekly.kind != QuotaKind.WEEKLY:
            raise ValueError("weekly_kind_invalid")

    @classmethod
    def unavailable(
        cls,
        *,
        observed_at: datetime | None = None,
        source: str = "codex_app_server",
        error_code: str = "quota_unavailable",
    ) -> "CodexQuotaSnapshot":
        observed_at = observed_at or datetime.now(timezone.utc)
        return cls(
            QuotaWindow.unavailable(QuotaKind.FIVE_HOUR, observed_at, source, error_code),
            QuotaWindow.unavailable(QuotaKind.WEEKLY, observed_at, source, error_code),
            observed_at,
            "unavailable",
        )

    def as_stale(self, observed_at: datetime, error_code: str) -> "CodexQuotaSnapshot":
        _require_aware(observed_at, "observed_at")
        return CodexQuotaSnapshot(
            self.five_hour.as_stale(error_code),
            self.weekly.as_stale(error_code),
            observed_at,
            "stale",
        )


def _percent(value: float | None) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not isfinite(number):
        raise ValueError("percentage_invalid")
    return min(100.0, max(0.0, number))


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field}_timezone_required")
