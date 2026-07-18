"""Deterministic aggregation for retained response-usage observations."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from typing import Iterable


MAX_SAFE_TOKEN_VALUE = (1 << 63) - 1
USAGE_STALE_AFTER = timedelta(minutes=3)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_RESPONSE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_METRIC_NAMES = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
)
_TERMINAL_RESPONSE_STATUSES = frozenset({"exact", "completed_partial"})
_IN_PROGRESS_STATUS = "in_progress"
_PARTIAL_TERMINAL_STATUS = "completed_partial"


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
    in_progress_observation_count: int = 0
    missing_response_identity_count: int = 0
    partial_terminal_response_count: int = 0
    messages: tuple[CoverageMessage, ...] = ()


@dataclass(frozen=True)
class FreshnessInfo:
    state: FreshnessState
    last_reliable_observed_at: datetime | None = None
    stale_after: timedelta = USAGE_STALE_AFTER


@dataclass(frozen=True)
class HighUsageThread:
    thread_safe_id: str
    safe_thread_label: str
    total_tokens: int
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    cache_reuse: float | None
    completed_response_count: int
    first_observed_at: datetime
    last_observed_at: datetime
    coverage_status: str


@dataclass(frozen=True)
class HighUsageResponse:
    response_safe_id: str
    thread_safe_id: str
    safe_thread_label: str
    total_tokens: int
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    cache_reuse: float | None
    observed_at: datetime
    coverage_status: str


@dataclass(frozen=True)
class LowCacheReuseThread:
    thread_safe_id: str
    safe_thread_label: str
    cache_reuse: float
    valid_input_tokens: int
    valid_cached_tokens: int
    valid_response_count: int
    first_observed_at: datetime
    last_observed_at: datetime
    coverage_status: str


@dataclass(frozen=True)
class UsageInsightsResult:
    range_id: UsageWindowKind
    range_start: datetime
    range_end: datetime
    generated_at: datetime
    source_available: bool
    coverage_status: CoverageState
    coverage_messages: tuple[CoverageMessage, ...]
    high_usage_threads: tuple[HighUsageThread, ...] = ()
    high_usage_responses: tuple[HighUsageResponse, ...] = ()
    low_cache_reuse_threads: tuple[LowCacheReuseThread, ...] = ()


@dataclass(frozen=True, slots=True)
class ObservedUsageRecord:
    """One safe local row projected for response-level aggregation."""

    source_observed_at: datetime | None
    recorded_at: datetime | None
    thread_safe_id: str | None
    response_safe_id: str | None
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
            if value.tzinfo is timezone.utc:
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
    insights: UsageInsightsResult
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

    @property
    def in_progress_observation_count(self) -> int:
        return self.coverage.in_progress_observation_count

    @property
    def in_progress_excluded(self) -> bool:
        return self.in_progress_observation_count > 0


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


@dataclass
class _MetricAccumulator:
    value: int = 0
    eligible: int = 0
    missing: int = 0
    invalid: int = 0

    def add(self, state: str, item: int | None) -> None:
        if state == "eligible":
            assert item is not None
            self.value += item
            self.eligible += 1
        elif state == "missing":
            self.missing += 1
        else:
            self.invalid += 1

    def freeze(self) -> MetricAggregate:
        return MetricAggregate(
            self.value if self.eligible else None,
            self.eligible,
            self.missing,
            self.invalid,
        )


@dataclass
class _SummaryAccumulator:
    metrics: dict[str, _MetricAccumulator] = field(
        default_factory=lambda: {
            name: _MetricAccumulator() for name in _METRIC_NAMES
        },
    )
    cached_sum: int = 0
    input_sum: int = 0
    cache_eligible: int = 0
    cache_missing: int = 0
    cache_invalid: int = 0
    response_count: int = 0
    covered_thread_count: int = 0
    covered_thread_ids: set[str] = field(default_factory=set)
    thread_eligible: int = 0
    thread_missing: int = 0
    first: datetime | None = None
    last: datetime | None = None
    latest_key: tuple[datetime, int] | None = None
    latest_stale: bool = False
    partial_terminal_count: int = 0

    def add(
        self,
        record: ObservedUsageRecord,
        states: dict[str, tuple[str, int | None]],
    ) -> None:
        observed_at = record.source_observed_at
        assert observed_at is not None
        for name, (state, item) in states.items():
            self.metrics[name].add(state, item)
        input_state, input_value = states["input_tokens"]
        cached_state, cached_value = states["cached_tokens"]
        if "invalid" in {input_state, cached_state}:
            self.cache_invalid += 1
        elif "missing" in {input_state, cached_state}:
            self.cache_missing += 1
        else:
            assert input_value is not None and cached_value is not None
            self.input_sum += input_value
            self.cached_sum += cached_value
            self.cache_eligible += 1
        self.response_count += 1
        assert record.thread_safe_id is not None
        if record.thread_safe_id not in self.covered_thread_ids:
            self.covered_thread_ids.add(record.thread_safe_id)
            self.covered_thread_count += 1
        self.thread_eligible += 1
        self.first = observed_at if self.first is None else min(self.first, observed_at)
        self.last = observed_at if self.last is None else max(self.last, observed_at)
        latest_key = (observed_at, record.sample_id)
        if self.latest_key is None or latest_key > self.latest_key:
            self.latest_key = latest_key
            self.latest_stale = record.token_stale
        if record.source_status.casefold() == _PARTIAL_TERMINAL_STATUS:
            self.partial_terminal_count += 1

    def frozen_metrics(self) -> dict[str, MetricAggregate]:
        return {name: metric.freeze() for name, metric in self.metrics.items()}

    def frozen_cache_reuse(self) -> RatioAggregate:
        value = (
            None
            if not self.cache_eligible or self.input_sum == 0
            else min(1.0, self.cached_sum / self.input_sum)
        )
        return RatioAggregate(
            value,
            self.cache_eligible,
            self.cache_missing,
            self.cache_invalid,
        )


@dataclass
class _ThreadUsageAccumulator:
    metrics: dict[str, _MetricAccumulator] = field(
        default_factory=lambda: {
            name: _MetricAccumulator() for name in _METRIC_NAMES
        },
    )
    completed_response_count: int = 0
    first: datetime | None = None
    last: datetime | None = None
    ranked_cache_input_sum: int = 0
    ranked_cache_cached_sum: int = 0
    ranked_cache_pair_count: int = 0
    cache_input_sum: int = 0
    cache_cached_sum: int = 0
    cache_pair_count: int = 0
    cache_first: datetime | None = None
    cache_last: datetime | None = None
    cache_partial: bool = False
    partial: bool = False

    def add(
        self,
        record: ObservedUsageRecord,
        states: dict[str, tuple[str, int | None]],
        *,
        include_high_usage: bool,
    ) -> None:
        observed_at = record.source_observed_at
        assert observed_at is not None
        input_state, input_value = states["input_tokens"]
        cached_state, cached_value = states["cached_tokens"]
        cache_pair_eligible = (
            input_state == cached_state == "eligible"
            and input_value is not None
            and cached_value is not None
            and input_value > 0
        )
        if cache_pair_eligible:
            self.cache_input_sum += input_value
            self.cache_cached_sum += cached_value
            self.cache_pair_count += 1
            self.cache_first = (
                observed_at if self.cache_first is None
                else min(self.cache_first, observed_at)
            )
            self.cache_last = (
                observed_at if self.cache_last is None
                else max(self.cache_last, observed_at)
            )
        elif input_state != "eligible" or cached_state != "eligible":
            self.cache_partial = True
        partial_terminal = (
            record.source_status.casefold() == _PARTIAL_TERMINAL_STATUS
        )
        if partial_terminal:
            self.cache_partial = True
        if not include_high_usage:
            return
        for name, (state, value) in states.items():
            self.metrics[name].add(state, value)
        self.completed_response_count += 1
        self.first = observed_at if self.first is None else min(self.first, observed_at)
        self.last = observed_at if self.last is None else max(self.last, observed_at)
        if (
            cache_pair_eligible
        ):
            self.ranked_cache_input_sum += input_value
            self.ranked_cache_cached_sum += cached_value
            self.ranked_cache_pair_count += 1
        self.partial = self.partial or partial_terminal or any(
            state != "eligible" for state, _value in states.values()
        )

    def freeze(self, thread_safe_id: str) -> HighUsageThread:
        assert self.first is not None and self.last is not None
        metrics = {name: item.freeze() for name, item in self.metrics.items()}
        total = metrics["total_tokens"].value
        assert total is not None
        cache_reuse = (
            self.ranked_cache_cached_sum / self.ranked_cache_input_sum
            if self.ranked_cache_pair_count and self.ranked_cache_input_sum > 0
            else None
        )
        return HighUsageThread(
            thread_safe_id=thread_safe_id,
            safe_thread_label="",
            total_tokens=total,
            input_tokens=metrics["input_tokens"].value,
            output_tokens=metrics["output_tokens"].value,
            cached_tokens=metrics["cached_tokens"].value,
            reasoning_tokens=metrics["reasoning_tokens"].value,
            cache_reuse=cache_reuse,
            completed_response_count=self.completed_response_count,
            first_observed_at=self.first,
            last_observed_at=self.last,
            coverage_status=(
                CoverageState.PARTIAL.value
                if self.partial else CoverageState.COMPLETE_FOR_LOCAL_HISTORY.value
            ),
        )

    def freeze_low_cache(self, thread_safe_id: str) -> LowCacheReuseThread | None:
        if (
            not self.cache_pair_count
            or self.cache_input_sum <= 0
            or self.cache_first is None
            or self.cache_last is None
        ):
            return None
        return LowCacheReuseThread(
            thread_safe_id=thread_safe_id,
            safe_thread_label="",
            cache_reuse=self.cache_cached_sum / self.cache_input_sum,
            valid_input_tokens=self.cache_input_sum,
            valid_cached_tokens=self.cache_cached_sum,
            valid_response_count=self.cache_pair_count,
            first_observed_at=self.cache_first,
            last_observed_at=self.cache_last,
            coverage_status=(
                CoverageState.PARTIAL.value
                if self.cache_partial
                else CoverageState.COMPLETE_FOR_LOCAL_HISTORY.value
            ),
        )


@dataclass
class _UsageInsightsAccumulator:
    threads: dict[str, _ThreadUsageAccumulator] = field(default_factory=dict)
    responses: list[HighUsageResponse] = field(default_factory=list)

    def add(
        self,
        record: ObservedUsageRecord,
        states: dict[str, tuple[str, int | None]],
    ) -> None:
        assert record.thread_safe_id is not None
        assert record.response_safe_id is not None
        assert record.source_observed_at is not None
        thread = self.threads.get(record.thread_safe_id)
        if thread is None:
            thread = _ThreadUsageAccumulator()
            self.threads[record.thread_safe_id] = thread
        total_state, total_value = states["total_tokens"]
        total_eligible = total_state == "eligible" and total_value is not None
        thread.add(record, states, include_high_usage=total_eligible)
        if not total_eligible:
            return
        assert total_value is not None
        if len(self.responses) >= 5:
            cutoff = self.responses[-1]
            if total_value < cutoff.total_tokens:
                return
            if total_value == cutoff.total_tokens:
                if record.source_observed_at < cutoff.observed_at:
                    return
                if (
                    record.source_observed_at == cutoff.observed_at
                    and record.response_safe_id >= cutoff.response_safe_id
                ):
                    return
        input_state, input_value = states["input_tokens"]
        cached_state, cached_value = states["cached_tokens"]
        cache_reuse = (
            cached_value / input_value
            if input_state == cached_state == "eligible"
            and input_value is not None
            and cached_value is not None
            and input_value > 0
            else None
        )
        partial = (
            record.source_status.casefold() == _PARTIAL_TERMINAL_STATUS
            or any(state != "eligible" for state, _value in states.values())
        )
        candidate = HighUsageResponse(
            response_safe_id=record.response_safe_id,
            thread_safe_id=record.thread_safe_id,
            safe_thread_label="",
            total_tokens=total_value,
            input_tokens=input_value if input_state == "eligible" else None,
            output_tokens=(
                states["output_tokens"][1]
                if states["output_tokens"][0] == "eligible" else None
            ),
            cached_tokens=cached_value if cached_state == "eligible" else None,
            reasoning_tokens=(
                states["reasoning_tokens"][1]
                if states["reasoning_tokens"][0] == "eligible" else None
            ),
            cache_reuse=cache_reuse,
            observed_at=record.source_observed_at,
            coverage_status=(
                CoverageState.PARTIAL.value
                if partial else CoverageState.COMPLETE_FOR_LOCAL_HISTORY.value
            ),
        )
        if len(self.responses) < 5:
            self.responses.append(candidate)
            self.responses.sort(key=_high_response_sort_key)
        else:
            self.responses[-1] = candidate
            self.responses.sort(key=_high_response_sort_key)

    def freeze(
        self,
        bounds: UsageWindowBounds,
        generated_at: datetime,
        coverage: CoverageInfo,
    ) -> UsageInsightsResult:
        top_threads: list[HighUsageThread] = []
        low_cache_threads: list[LowCacheReuseThread] = []
        for thread_safe_id, accumulator in self.threads.items():
            if accumulator.completed_response_count:
                top_threads.append(accumulator.freeze(thread_safe_id))
                top_threads.sort(key=_high_thread_sort_key)
                del top_threads[5:]
            low_cache = accumulator.freeze_low_cache(thread_safe_id)
            if low_cache is not None:
                low_cache_threads.append(low_cache)
                low_cache_threads.sort(key=_low_cache_sort_key)
                del low_cache_threads[3:]
        thread_ids = {
            item.thread_safe_id
            for item in (*top_threads, *self.responses, *low_cache_threads)
        }
        labels = safe_digest_labels(thread_ids)
        return UsageInsightsResult(
            range_id=bounds.scope,
            range_start=bounds.start_utc,
            range_end=bounds.end_utc,
            generated_at=generated_at,
            source_available=coverage.state is not CoverageState.UNAVAILABLE,
            coverage_status=coverage.state,
            coverage_messages=coverage.messages,
            high_usage_threads=tuple(
                replace(item, safe_thread_label=labels[item.thread_safe_id])
                for item in top_threads
            ),
            high_usage_responses=tuple(
                replace(item, safe_thread_label=labels[item.thread_safe_id])
                for item in self.responses
            ),
            low_cache_reuse_threads=tuple(
                replace(item, safe_thread_label=labels[item.thread_safe_id])
                for item in low_cache_threads
            ),
        )


def _high_thread_sort_key(item: HighUsageThread) -> tuple[object, ...]:
    return (-item.total_tokens, -item.last_observed_at.timestamp(), item.thread_safe_id)


def _high_response_sort_key(item: HighUsageResponse) -> tuple[object, ...]:
    return (-item.total_tokens, -item.observed_at.timestamp(), item.response_safe_id)


def _low_cache_sort_key(item: LowCacheReuseThread) -> tuple[object, ...]:
    return (item.cache_reuse, -item.valid_input_tokens, item.thread_safe_id)


def safe_digest_labels(values: Iterable[str]) -> dict[str, str]:
    """Return deterministic digest suffix labels without exposing full safe IDs."""

    identifiers = sorted(set(values))
    digests = {
        value: (value.split(":", 1)[1] if ":" in value else value)
        for value in identifiers
    }
    lengths = {value: 6 for value in identifiers}
    while True:
        groups: dict[str, list[str]] = {}
        for value in identifiers:
            label = digests[value][-lengths[value]:].upper()
            groups.setdefault(label, []).append(value)
        collisions = [group for group in groups.values() if len(group) > 1]
        if not collisions:
            break
        changed = False
        for group in collisions:
            for value in group:
                if lengths[value] < 12:
                    lengths[value] += 2
                    changed = True
        if not changed:
            break
    return {
        value: digests[value][-lengths[value]:].upper()
        for value in identifiers
    }


def aggregate_observed_usage(
    records: Iterable[ObservedUsageRecord],
    scope: UsageWindowKind | str,
    *,
    as_of_utc: datetime,
    local_timezone: tzinfo | None,
    first_retained_observed_at: datetime | None = None,
    unknown_time_record_count: int = 0,
    missing_response_identity_count: int = 0,
    records_grouped_by_response: bool = False,
) -> ObservedUsageSummary:
    """Aggregate one canonical terminal observation per safe response identity."""

    bounds = usage_window_bounds(
        scope,
        as_of_utc=as_of_utc,
        local_timezone=local_timezone,
    )
    quality = {
        "unknown": max(0, int(unknown_time_record_count)),
        "excluded": 0,
        "missing_identity": max(0, int(missing_response_identity_count)),
    }
    earliest_identified: datetime | None = None

    def identified() -> Iterable[ObservedUsageRecord]:
        nonlocal earliest_identified
        for record in records:
            if not isinstance(record, ObservedUsageRecord):
                raise TypeError("observed_usage_record_required")
            if not record.has_response_payload:
                continue
            if record.legacy_unknown_time or record.observed_time_invalid:
                if (
                    record.recorded_at is not None
                    and bounds.start_utc <= record.recorded_at <= bounds.end_utc
                ):
                    quality["unknown"] += 1
                continue
            observed_at = record.source_observed_at
            if observed_at is None:
                if (
                    record.recorded_at is not None
                    and bounds.start_utc <= record.recorded_at <= bounds.end_utc
                ):
                    quality["unknown"] += 1
                continue
            if observed_at > bounds.end_utc:
                continue
            in_window = bounds.start_utc <= observed_at
            lifecycle = record.source_status.casefold()
            identity_valid = (
                _valid_safe_identifier(record.thread_safe_id)
                and _valid_response_safe_id(record.response_safe_id)
            )
            if not identity_valid:
                if in_window:
                    quality["missing_identity"] += 1
                continue
            if lifecycle == _IN_PROGRESS_STATUS:
                yield record
                continue
            if lifecycle not in _TERMINAL_RESPONSE_STATUSES:
                if in_window:
                    quality["excluded"] += 1
                continue
            if record.is_derived or not record.source_available:
                if in_window:
                    quality["excluded"] += 1
                continue
            earliest_identified = (
                observed_at
                if earliest_identified is None
                else min(earliest_identified, observed_at)
            )
            yield record

    canonical = (
        _canonical_grouped_records(identified(), bounds.start_utc)
        if records_grouped_by_response
        else iter(_deduplicate_records(identified(), bounds.start_utc))
    )
    accumulator = _SummaryAccumulator()
    insights_accumulator = _UsageInsightsAccumulator()
    unresolved_in_progress = 0
    for lifecycle in canonical:
        unresolved_in_progress += lifecycle.unresolved_in_progress_observation_count
        record = lifecycle.terminal
        if record is None:
            continue
        observed_at = record.source_observed_at
        if observed_at is not None and bounds.start_utc <= observed_at <= bounds.end_utc:
            states = _normalized_metric_states(record)
            accumulator.add(record, states)
            insights_accumulator.add(record, states)

    metrics = accumulator.frozen_metrics()
    cache_reuse = accumulator.frozen_cache_reuse()
    history_started = (
        _aware_utc(first_retained_observed_at, "first_retained_observed_at")
        if first_retained_observed_at is not None
        else earliest_identified or accumulator.first
    )
    unknown_times = quality["unknown"]
    excluded = quality["excluded"]
    in_progress = unresolved_in_progress
    missing_identity = quality["missing_identity"]

    messages: list[CoverageMessage] = []
    if accumulator.response_count:
        for name, aggregate in metrics.items():
            if aggregate.eligible_record_count < accumulator.response_count:
                messages.append(CoverageMessage(
                    "metric_coverage",
                    name,
                    aggregate.eligible_record_count,
                    accumulator.response_count,
                ))
        if accumulator.thread_missing:
            messages.append(CoverageMessage(
                "thread_coverage",
                eligible_count=accumulator.thread_eligible,
                total_count=accumulator.response_count,
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
    if in_progress:
        messages.append(CoverageMessage(
            "in_progress_excluded",
            eligible_count=in_progress,
            total_count=in_progress,
        ))
    if missing_identity:
        messages.append(CoverageMessage(
            "missing_response_identity",
            eligible_count=missing_identity,
            total_count=missing_identity,
        ))
    if accumulator.partial_terminal_count:
        messages.append(CoverageMessage(
            "partial_terminal_observations",
            eligible_count=accumulator.partial_terminal_count,
            total_count=accumulator.partial_terminal_count,
        ))

    partial = bool(
        unknown_times
        or excluded
        or in_progress
        or missing_identity
        or accumulator.partial_terminal_count
        or accumulator.thread_missing
        or any(
            aggregate.missing_record_count or aggregate.invalid_record_count
            for aggregate in metrics.values()
        )
    )
    if not accumulator.response_count:
        if in_progress or missing_identity:
            coverage_state = CoverageState.PARTIAL
        elif unknown_times or excluded:
            coverage_state = CoverageState.UNKNOWN
        else:
            coverage_state = CoverageState.NO_OBSERVATIONS
    elif partial:
        coverage_state = CoverageState.PARTIAL
    elif history_started is None or history_started > bounds.start_utc:
        coverage_state = CoverageState.LIMITED_HISTORY
        messages.append(CoverageMessage("limited_history"))
    else:
        coverage_state = CoverageState.COMPLETE_FOR_LOCAL_HISTORY
        messages.append(CoverageMessage("all_retained_local_observations"))

    if accumulator.last is None:
        freshness_state = FreshnessState.UNAVAILABLE
    else:
        freshness_state = (
            FreshnessState.STALE
            if accumulator.latest_stale
            or bounds.end_utc - accumulator.last > USAGE_STALE_AFTER
            else FreshnessState.FRESH
        )

    total = metrics["total_tokens"]
    average = (
        None
        if total.value is None or total.eligible_record_count == 0
        else total.value / total.eligible_record_count
    )
    coverage = CoverageInfo(
        state=coverage_state,
        history_started_at=history_started,
        unknown_time_record_count=unknown_times,
        excluded_record_count=excluded,
        thread_eligible_record_count=accumulator.thread_eligible,
        thread_missing_record_count=accumulator.thread_missing,
        in_progress_observation_count=in_progress,
        missing_response_identity_count=missing_identity,
        partial_terminal_response_count=accumulator.partial_terminal_count,
        messages=tuple(messages),
    )
    freshness = FreshnessInfo(freshness_state, accumulator.last)
    insights = insights_accumulator.freeze(bounds, bounds.end_utc, coverage)
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
        accumulator.response_count,
        accumulator.covered_thread_count,
        average,
        accumulator.first,
        accumulator.last,
        coverage,
        freshness,
        insights,
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
    insights = UsageInsightsResult(
        range_id=bounds.scope,
        range_start=bounds.start_utc,
        range_end=bounds.end_utc,
        generated_at=bounds.end_utc,
        source_available=False,
        coverage_status=CoverageState.UNAVAILABLE,
        coverage_messages=coverage.messages,
    )
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
        insights,
        error_code,
    )


@dataclass(frozen=True)
class _ResponseLifecycle:
    terminal: ObservedUsageRecord | None
    unresolved_in_progress_observation_count: int = 0


def _deduplicate_records(
    records: Iterable[ObservedUsageRecord],
    window_start: datetime,
) -> tuple[_ResponseLifecycle, ...]:
    groups: dict[
        tuple[str, str], tuple[ObservedUsageRecord | None, int]
    ] = {}
    for record in records:
        assert record.thread_safe_id is not None
        assert record.response_safe_id is not None
        key = (record.thread_safe_id, record.response_safe_id)
        terminal, in_progress = groups.get(key, (None, 0))
        if _eligible_terminal(record):
            if (
                terminal is None
                or _response_candidate_rank(record) > _response_candidate_rank(terminal)
            ):
                terminal = record
        elif (
            record.source_status.casefold() == _IN_PROGRESS_STATUS
            and record.source_observed_at is not None
            and record.source_observed_at >= window_start
        ):
            in_progress += 1
        groups[key] = terminal, in_progress
    return tuple(
        _ResponseLifecycle(terminal, 0 if terminal is not None else in_progress)
        for _key, (terminal, in_progress) in sorted(groups.items())
    )


def _canonical_grouped_records(
    records: Iterable[ObservedUsageRecord],
    window_start: datetime,
) -> Iterable[_ResponseLifecycle]:
    """Release one response group at a time from identity-ordered rows."""

    active_key: tuple[str, str] | None = None
    active_winner: ObservedUsageRecord | None = None
    active_in_progress = 0
    for record in records:
        assert record.thread_safe_id is not None
        assert record.response_safe_id is not None
        key = (record.thread_safe_id, record.response_safe_id)
        if active_key is not None and key != active_key:
            yield _ResponseLifecycle(
                active_winner,
                0 if active_winner is not None else active_in_progress,
            )
            active_winner = None
            active_in_progress = 0
        active_key = key
        if (
            _eligible_terminal(record)
            and (
                active_winner is None
                or _response_candidate_rank(record)
                > _response_candidate_rank(active_winner)
            )
        ):
            active_winner = record
        elif (
            record.source_status.casefold() == _IN_PROGRESS_STATUS
            and record.source_observed_at is not None
            and record.source_observed_at >= window_start
        ):
            active_in_progress += 1
    if active_key is not None:
        yield _ResponseLifecycle(
            active_winner,
            0 if active_winner is not None else active_in_progress,
        )


def _eligible_terminal(record: ObservedUsageRecord) -> bool:
    return bool(
        record.source_status.casefold() in _TERMINAL_RESPONSE_STATUSES
        and record.source_available
        and not record.is_derived
    )


def _response_candidate_rank(
    record: ObservedUsageRecord,
) -> tuple[object, ...]:
    exact_rank = int(record.source_status.casefold() == "exact")
    completeness = sum(getattr(record, name) is not None for name in _METRIC_NAMES)
    source_rank = {"dashboard": 2, "mini": 1}.get(record.source_type, 0)
    observed_at = record.source_observed_at or datetime.min.replace(tzinfo=timezone.utc)
    recorded_at = record.recorded_at or datetime.min.replace(tzinfo=timezone.utc)
    return (
        exact_rank,
        observed_at,
        completeness,
        source_rank,
        int(record.source_available),
        int(not record.token_stale),
        recorded_at,
        record.sample_id,
    )


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


def _valid_safe_identifier(value: str | None) -> bool:
    return bool(isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value))


def _valid_response_safe_id(value: str | None) -> bool:
    return bool(isinstance(value, str) and _SAFE_RESPONSE_ID.fullmatch(value))


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}_timezone_required")
    return value.astimezone(timezone.utc)
