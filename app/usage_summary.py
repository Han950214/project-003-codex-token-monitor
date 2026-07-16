"""Deterministic aggregation for retained response-usage observations."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from typing import Iterable


MAX_SAFE_TOKEN_VALUE = (1 << 63) - 1
USAGE_STALE_AFTER = timedelta(minutes=3)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_METRIC_NAMES = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
)


class UsageWindowKind(str, Enum):
    TODAY = "today"
    ROLLING_5H = "rolling_5h"
    ROLLING_7D = "rolling_7d"
    ROLLING_30D = "rolling_30d"


class CoverageState(str, Enum):
    COMPLETE_FOR_LOCAL_HISTORY = "complete_for_local_history"
    LIMITED_HISTORY = "limited_history"
    PARTIAL = "partial"
    NO_OBSERVATIONS = "no_observations"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class FreshnessState(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class UsageWindowBounds:
    scope: UsageWindowKind
    start_utc: datetime
    end_utc: datetime
    local_timezone: str


@dataclass(frozen=True)
class MetricAggregate:
    value: int | None = None
    eligible_record_count: int = 0
    missing_record_count: int = 0
    invalid_record_count: int = 0


@dataclass(frozen=True)
class RatioAggregate:
    value: float | None = None
    eligible_record_count: int = 0
    missing_record_count: int = 0
    invalid_record_count: int = 0


@dataclass(frozen=True)
class CoverageMessage:
    code: str
    metric: str | None = None
    eligible_count: int = 0
    total_count: int = 0


@dataclass(frozen=True)
class CoverageInfo:
    state: CoverageState
    history_started_at: datetime | None = None
    unknown_time_record_count: int = 0
    excluded_record_count: int = 0
    thread_eligible_record_count: int = 0
    thread_missing_record_count: int = 0
    messages: tuple[CoverageMessage, ...] = ()


@dataclass(frozen=True)
class FreshnessInfo:
    state: FreshnessState
    last_reliable_observed_at: datetime | None = None
    stale_after: timedelta = USAGE_STALE_AFTER


@dataclass(frozen=True)
class ObservedUsageRecord:
    """One safe local row projected for response-level aggregation."""

    source_observed_at: datetime | None
    recorded_at: datetime | None
    thread_safe_id: str | None
    model_safe_id: str | None
    source_type: str
    source_status: str
    source_available: bool
    token_stale: bool
    token_stale_reason: str | None
    input_tokens: object = None
    output_tokens: object = None
    total_tokens: object = None
    cached_tokens: object = None
    reasoning_tokens: object = None
    session_total_tokens: object = None
    is_derived: bool = False
    legacy_unknown_time: bool = False
    observed_time_invalid: bool = False
    stored_fingerprint: str = ""
    sample_id: int = 0

    def __post_init__(self) -> None:
        for field_name in ("source_observed_at", "recorded_at"):
            value = getattr(self, field_name)
            if value is None:
                continue
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name}_timezone_required")
            object.__setattr__(self, field_name, value.astimezone(timezone.utc))

    @property
    def has_response_payload(self) -> bool:
        return any(getattr(self, name) is not None for name in _METRIC_NAMES)


@dataclass(frozen=True)
class ObservedUsageSummary:
    scope: UsageWindowKind
    window_start_utc: datetime
    window_end_utc: datetime
    local_timezone: str
    input_tokens: MetricAggregate
    output_tokens: MetricAggregate
    total_tokens: MetricAggregate
    cached_tokens: MetricAggregate
    reasoning_tokens: MetricAggregate
    cache_reuse: RatioAggregate
    observed_response_count: int
    covered_thread_count: int
    average_total_tokens_per_response: float | None
    first_reliable_observed_at: datetime | None
    last_reliable_observed_at: datetime | None
    coverage: CoverageInfo
    freshness: FreshnessInfo
    error_code: str | None = None

    @property
    def coverage_state(self) -> str:
        return self.coverage.state.value

    @property
    def freshness_state(self) -> str:
        return self.freshness.state.value

    @property
    def coverage_messages(self) -> tuple[CoverageMessage, ...]:
        return self.coverage.messages


def usage_window_bounds(
    scope: UsageWindowKind | str,
    *,
    as_of_utc: datetime,
    local_timezone: tzinfo | None,
) -> UsageWindowBounds:
    """Return inclusive UTC bounds using real elapsed or local-calendar time."""

    kind = UsageWindowKind(scope)
    as_of = _aware_utc(as_of_utc, "as_of_utc")
    if kind is UsageWindowKind.TODAY:
        if local_timezone is None:
            local = time.localtime(as_of.timestamp())
            midnight_epoch = time.mktime(
                (local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)
            )
            start = datetime.fromtimestamp(midnight_epoch, timezone.utc)
            timezone_name = "system_local"
        else:
            local = as_of.astimezone(local_timezone)
            local_midnight = datetime(
                local.year,
                local.month,
                local.day,
                tzinfo=local_timezone,
            )
            start = local_midnight.astimezone(timezone.utc)
            timezone_name = (
                getattr(local_timezone, "key", None)
                or local_timezone.tzname(local_midnight)
                or type(local_timezone).__name__
            )
    else:
        elapsed = {
            UsageWindowKind.ROLLING_5H: timedelta(hours=5),
            UsageWindowKind.ROLLING_7D: timedelta(days=7),
            UsageWindowKind.ROLLING_30D: timedelta(days=30),
        }[kind]
        start = as_of - elapsed
        if local_timezone is None:
            timezone_name = "system_local"
        else:
            timezone_name = (
                getattr(local_timezone, "key", None)
                or local_timezone.tzname(as_of.astimezone(local_timezone))
                or type(local_timezone).__name__
            )
    return UsageWindowBounds(kind, start, as_of, str(timezone_name))


def aggregate_observed_usage(
    records: Iterable[ObservedUsageRecord],
    scope: UsageWindowKind | str,
    *,
    as_of_utc: datetime,
    local_timezone: tzinfo | None,
    first_retained_observed_at: datetime | None = None,
    unknown_time_record_count: int = 0,
) -> ObservedUsageSummary:
    """Aggregate unique normalized response observations for one exact window."""

    bounds = usage_window_bounds(
        scope,
        as_of_utc=as_of_utc,
        local_timezone=local_timezone,
    )
    candidates: list[ObservedUsageRecord] = []
    unknown_times = max(0, int(unknown_time_record_count))
    excluded = 0
    for record in records:
        if not isinstance(record, ObservedUsageRecord):
            raise TypeError("observed_usage_record_required")
        if not record.has_response_payload:
            continue
        if record.legacy_unknown_time or record.observed_time_invalid:
            unknown_times += 1
            continue
        observed_at = record.source_observed_at
        if observed_at is None:
            unknown_times += 1
            continue
        if not bounds.start_utc <= observed_at <= bounds.end_utc:
            continue
        if record.is_derived or not record.source_available:
            excluded += 1
            continue
        candidates.append(record)

    unique = _deduplicate_records(candidates)
    metric_states = [_normalized_metric_states(record) for record in unique]
    metrics = {
        name: _aggregate_metric(states[name] for states in metric_states)
        for name in _METRIC_NAMES
    }
    cache_reuse = _aggregate_cache_reuse(metric_states)
    thread_ids = {
        record.thread_safe_id
        for record in unique
        if _valid_safe_identifier(record.thread_safe_id)
    }
    thread_eligible = sum(
        _valid_safe_identifier(record.thread_safe_id) for record in unique
    )
    thread_missing = len(unique) - thread_eligible
    observed_times = tuple(record.source_observed_at for record in unique)
    first = min(observed_times) if observed_times else None
    last = max(observed_times) if observed_times else None
    history_started = (
        _aware_utc(first_retained_observed_at, "first_retained_observed_at")
        if first_retained_observed_at is not None
        else first
    )

    messages: list[CoverageMessage] = []
    if unique:
        for name, aggregate in metrics.items():
            if aggregate.eligible_record_count < len(unique):
                messages.append(CoverageMessage(
                    "metric_coverage",
                    name,
                    aggregate.eligible_record_count,
                    len(unique),
                ))
        if thread_missing:
            messages.append(CoverageMessage(
                "thread_coverage",
                eligible_count=thread_eligible,
                total_count=len(unique),
            ))
    if unknown_times:
        messages.append(CoverageMessage(
            "unknown_observed_time",
            eligible_count=unknown_times,
            total_count=unknown_times,
        ))
    if excluded:
        messages.append(CoverageMessage(
            "excluded_observations",
            eligible_count=excluded,
            total_count=excluded,
        ))

    partial = bool(
        unknown_times
        or excluded
        or thread_missing
        or any(
            aggregate.missing_record_count or aggregate.invalid_record_count
            for aggregate in metrics.values()
        )
    )
    if not unique:
        coverage_state = (
            CoverageState.UNKNOWN
            if unknown_times or excluded
            else CoverageState.NO_OBSERVATIONS
        )
    elif partial:
        coverage_state = CoverageState.PARTIAL
    elif history_started is None or history_started > bounds.start_utc:
        coverage_state = CoverageState.LIMITED_HISTORY
        messages.append(CoverageMessage("limited_history"))
    else:
        coverage_state = CoverageState.COMPLETE_FOR_LOCAL_HISTORY
        messages.append(CoverageMessage("all_retained_local_observations"))

    if last is None:
        freshness_state = FreshnessState.UNAVAILABLE
    else:
        latest = max(unique, key=lambda item: (item.source_observed_at, item.sample_id))
        freshness_state = (
            FreshnessState.STALE
            if latest.token_stale or bounds.end_utc - last > USAGE_STALE_AFTER
            else FreshnessState.FRESH
        )

    total = metrics["total_tokens"]
    average = (
        None
        if total.value is None or total.eligible_record_count == 0
        else total.value / total.eligible_record_count
    )
    coverage = CoverageInfo(
        coverage_state,
        history_started,
        unknown_times,
        excluded,
        thread_eligible,
        thread_missing,
        tuple(messages),
    )
    freshness = FreshnessInfo(freshness_state, last)
    return ObservedUsageSummary(
        bounds.scope,
        bounds.start_utc,
        bounds.end_utc,
        bounds.local_timezone,
        metrics["input_tokens"],
        metrics["output_tokens"],
        metrics["total_tokens"],
        metrics["cached_tokens"],
        metrics["reasoning_tokens"],
        cache_reuse,
        len(unique),
        len(thread_ids),
        average,
        first,
        last,
        coverage,
        freshness,
    )


def unavailable_usage_summary(
    scope: UsageWindowKind | str,
    *,
    as_of_utc: datetime,
    local_timezone: tzinfo | None,
    error_code: str,
) -> ObservedUsageSummary:
    bounds = usage_window_bounds(
        scope,
        as_of_utc=as_of_utc,
        local_timezone=local_timezone,
    )
    empty = MetricAggregate()
    coverage = CoverageInfo(
        CoverageState.UNAVAILABLE,
        messages=(CoverageMessage("history_unavailable"),),
    )
    freshness = FreshnessInfo(FreshnessState.UNAVAILABLE)
    return ObservedUsageSummary(
        bounds.scope,
        bounds.start_utc,
        bounds.end_utc,
        bounds.local_timezone,
        empty,
        empty,
        empty,
        empty,
        empty,
        RatioAggregate(),
        0,
        0,
        None,
        None,
        None,
        coverage,
        freshness,
        error_code,
    )


def _deduplicate_records(
    records: Iterable[ObservedUsageRecord],
) -> tuple[ObservedUsageRecord, ...]:
    fingerprints: set[str] = set()
    groups: dict[tuple[object, ...], list[ObservedUsageRecord]] = {}
    for record in sorted(records, key=lambda item: item.sample_id):
        if record.stored_fingerprint and record.stored_fingerprint in fingerprints:
            continue
        if record.stored_fingerprint:
            fingerprints.add(record.stored_fingerprint)
        key = (
            record.thread_safe_id,
            record.source_observed_at,
            record.source_status,
            record.source_available,
            record.token_stale,
            record.token_stale_reason,
        )
        candidates = groups.setdefault(key, [])
        for index, existing in enumerate(candidates):
            if _records_compatible(existing, record):
                candidates[index] = _merge_records(existing, record)
                break
        else:
            candidates.append(record)
    merged = [record for candidates in groups.values() for record in candidates]
    merged.sort(key=lambda item: (item.source_observed_at, item.sample_id))
    return tuple(merged)


def _records_compatible(
    first: ObservedUsageRecord,
    second: ObservedUsageRecord,
) -> bool:
    identifiers_compatible = (
        first.model_safe_id is None
        or second.model_safe_id is None
        or first.model_safe_id == second.model_safe_id
    )
    return identifiers_compatible and all(
        getattr(first, name) is None
        or getattr(second, name) is None
        or getattr(first, name) == getattr(second, name)
        for name in _METRIC_NAMES
    )


def _merge_records(
    first: ObservedUsageRecord,
    second: ObservedUsageRecord,
) -> ObservedUsageRecord:
    def rank(record: ObservedUsageRecord) -> tuple[int, int, int]:
        completeness = sum(getattr(record, name) is not None for name in _METRIC_NAMES)
        source_rank = {"dashboard": 2, "mini": 1}.get(record.source_type, 0)
        return completeness, source_rank, -record.sample_id

    preferred, other = (first, second) if rank(first) >= rank(second) else (second, first)
    updates = {
        name: (
            getattr(preferred, name)
            if getattr(preferred, name) is not None
            else getattr(other, name)
        )
        for name in _METRIC_NAMES
    }
    if preferred.thread_safe_id is None:
        updates["thread_safe_id"] = other.thread_safe_id
    if preferred.model_safe_id is None:
        updates["model_safe_id"] = other.model_safe_id
    return replace(preferred, **updates)


def _normalized_metric_states(
    record: ObservedUsageRecord,
) -> dict[str, tuple[str, int | None]]:
    states = {
        name: _metric_state(getattr(record, name)) for name in _METRIC_NAMES
    }
    input_state, input_value = states["input_tokens"]
    output_state, output_value = states["output_tokens"]
    total_state, total_value = states["total_tokens"]
    cached_state, cached_value = states["cached_tokens"]
    reasoning_state, reasoning_value = states["reasoning_tokens"]
    if (
        input_state == cached_state == "eligible"
        and cached_value is not None
        and input_value is not None
        and cached_value > input_value
    ):
        states["cached_tokens"] = ("invalid", None)
    if (
        output_state == reasoning_state == "eligible"
        and reasoning_value is not None
        and output_value is not None
        and reasoning_value > output_value
    ):
        states["reasoning_tokens"] = ("invalid", None)
    if (
        input_state == output_state == total_state == "eligible"
        and input_value is not None
        and output_value is not None
        and total_value != input_value + output_value
    ):
        states["total_tokens"] = ("invalid", None)
    return states


def _metric_state(value: object) -> tuple[str, int | None]:
    if value is None:
        return "missing", None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > MAX_SAFE_TOKEN_VALUE
    ):
        return "invalid", None
    return "eligible", value


def _aggregate_metric(
    states: Iterable[tuple[str, int | None]],
) -> MetricAggregate:
    value = 0
    eligible = missing = invalid = 0
    for state, item in states:
        if state == "eligible":
            assert item is not None
            value += item
            eligible += 1
        elif state == "missing":
            missing += 1
        else:
            invalid += 1
    return MetricAggregate(value if eligible else None, eligible, missing, invalid)


def _aggregate_cache_reuse(
    metric_states: Iterable[dict[str, tuple[str, int | None]]],
) -> RatioAggregate:
    cached_sum = input_sum = 0
    eligible = missing = invalid = 0
    for states in metric_states:
        input_state, input_value = states["input_tokens"]
        cached_state, cached_value = states["cached_tokens"]
        if "invalid" in {input_state, cached_state}:
            invalid += 1
            continue
        if "missing" in {input_state, cached_state}:
            missing += 1
            continue
        assert input_value is not None and cached_value is not None
        input_sum += input_value
        cached_sum += cached_value
        eligible += 1
    value = None if not eligible or input_sum == 0 else min(1.0, cached_sum / input_sum)
    return RatioAggregate(value, eligible, missing, invalid)


def _valid_safe_identifier(value: str | None) -> bool:
    return bool(isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value))


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}_timezone_required")
    return value.astimezone(timezone.utc)
