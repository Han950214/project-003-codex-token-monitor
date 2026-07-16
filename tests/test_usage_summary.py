from __future__ import annotations

import inspect
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest.mock import Mock

from app.analytics_ui import metric_samples, trend_view_from_query
from app.codex_rollout import make_response_safe_id
from app.history import HistoryObservation, UsageHistoryStore
from app.i18n import translate
from app.main import Dashboard
from app.usage_summary import (
    MAX_SAFE_TOKEN_VALUE,
    CoverageState,
    FreshnessState,
    ObservedUsageRecord,
    UsageWindowKind,
    aggregate_observed_usage,
    unavailable_usage_summary,
    usage_window_bounds,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
ZERO = timedelta(0)


def _first_sunday_on_or_after(value: datetime) -> datetime:
    days_to_go = 6 - value.weekday()
    return value if days_to_go == 0 else value + timedelta(days=days_to_go)


class UsEastern(tzinfo):
    """Small rule-based timezone used to verify both DST transitions."""

    standard_offset = -5 * HOUR

    def tzname(self, value: datetime | None) -> str:
        return "EDT" if self.dst(value) else "EST"

    def utcoffset(self, value: datetime | None) -> timedelta:
        return self.standard_offset + self.dst(value)

    def dst(self, value: datetime | None) -> timedelta:
        if value is None or value.tzinfo is None:
            return ZERO
        start = _first_sunday_on_or_after(datetime(value.year, 3, 8, 2))
        end = _first_sunday_on_or_after(datetime(value.year, 11, 1, 2))
        local = value.replace(tzinfo=None)
        if start + HOUR <= local < end - HOUR:
            return HOUR
        if end - HOUR <= local < end:
            return ZERO if value.fold else HOUR
        if start <= local < start + HOUR:
            return HOUR if value.fold else ZERO
        return ZERO

    def fromutc(self, value: datetime) -> datetime:
        start = _first_sunday_on_or_after(datetime(value.year, 3, 8, 2)).replace(
            tzinfo=self,
        )
        end = _first_sunday_on_or_after(datetime(value.year, 11, 1, 2)).replace(
            tzinfo=self,
        )
        standard = value + self.standard_offset
        daylight = standard + HOUR
        if end <= daylight < end + HOUR:
            return standard.replace(fold=1)
        if standard < start or daylight >= end:
            return standard
        if start <= standard < end - HOUR:
            return daylight
        return standard


def usage_record(
    *,
    at: datetime = NOW,
    thread: str | None = "thread-1",
    model: str | None = "model-1",
    response: str | None = None,
    source: str = "dashboard",
    status: str = "exact",
    available: bool = True,
    stale: bool = False,
    input_tokens: object = 100,
    output_tokens: object = 20,
    total_tokens: object = 120,
    cached_tokens: object = 40,
    reasoning_tokens: object = 5,
    session_total_tokens: object = 1_000,
    fingerprint: str = "",
    sample_id: int = 1,
) -> ObservedUsageRecord:
    return ObservedUsageRecord(
        source_observed_at=at,
        recorded_at=at + timedelta(seconds=1),
        thread_safe_id=thread,
        response_safe_id=make_response_safe_id(
            thread or "missing-thread", response or f"response-{sample_id}",
        ),
        model_safe_id=model,
        source_type=source,
        source_status=status,
        source_available=available,
        token_stale=stale,
        token_stale_reason="source_stale" if stale else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        session_total_tokens=session_total_tokens,
        stored_fingerprint=fingerprint,
        sample_id=sample_id,
    )


def summarize(
    records: tuple[ObservedUsageRecord, ...],
    scope: UsageWindowKind = UsageWindowKind.ROLLING_5H,
    *,
    as_of: datetime = NOW,
    first: datetime | None = None,
    unknown: int = 0,
):
    bounds = usage_window_bounds(scope, as_of_utc=as_of, local_timezone=timezone.utc)
    return aggregate_observed_usage(
        records,
        scope,
        as_of_utc=as_of,
        local_timezone=timezone.utc,
        first_retained_observed_at=(
            first if first is not None else bounds.start_utc - timedelta(seconds=1)
        ),
        unknown_time_record_count=unknown,
    )


class UsageWindowTests(unittest.TestCase):
    def test_today_uses_local_calendar_midnight_not_last_24_hours(self):
        china = timezone(timedelta(hours=8), "Asia/Shanghai")
        as_of = datetime(2026, 7, 15, 18, 30, tzinfo=timezone.utc)

        bounds = usage_window_bounds(
            UsageWindowKind.TODAY,
            as_of_utc=as_of,
            local_timezone=china,
        )

        self.assertEqual(
            (bounds.start_utc, bounds.end_utc, bounds.local_timezone),
            (
                datetime(2026, 7, 15, 16, 0, tzinfo=timezone.utc),
                as_of,
                "Asia/Shanghai",
            ),
        )

    def test_rolling_windows_are_exact_elapsed_time(self):
        expected = {
            UsageWindowKind.ROLLING_5H: timedelta(hours=5),
            UsageWindowKind.ROLLING_7D: timedelta(days=7),
            UsageWindowKind.ROLLING_30D: timedelta(days=30),
        }
        for kind, elapsed in expected.items():
            with self.subTest(kind=kind):
                bounds = usage_window_bounds(
                    kind,
                    as_of_utc=NOW,
                    local_timezone=UsEastern(),
                )
                self.assertEqual(bounds.end_utc - bounds.start_utc, elapsed)

    def test_today_honors_dst_start_and_end_offsets(self):
        eastern = UsEastern()
        spring_as_of = datetime(2026, 3, 8, 16, tzinfo=timezone.utc)
        fall_as_of = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)

        spring = usage_window_bounds(
            UsageWindowKind.TODAY,
            as_of_utc=spring_as_of,
            local_timezone=eastern,
        )
        fall = usage_window_bounds(
            UsageWindowKind.TODAY,
            as_of_utc=fall_as_of,
            local_timezone=eastern,
        )

        self.assertEqual(spring.start_utc, datetime(2026, 3, 8, 5, tzinfo=timezone.utc))
        self.assertEqual(fall.start_utc, datetime(2026, 11, 1, 4, tzinfo=timezone.utc))
        self.assertEqual(spring_as_of - spring.start_utc, timedelta(hours=11))
        self.assertEqual(fall_as_of - fall.start_utc, timedelta(hours=13))

    def test_start_and_end_are_inclusive_and_adjacent_milliseconds_excluded(self):
        start = NOW - timedelta(hours=5)
        records = tuple(
            usage_record(at=at, sample_id=index)
            for index, at in enumerate((
                start - timedelta(milliseconds=1),
                start,
                start + timedelta(milliseconds=1),
                NOW,
                NOW + timedelta(milliseconds=1),
            ), 1)
        )

        result = summarize(records)

        self.assertEqual(result.observed_response_count, 3)
        self.assertEqual(result.total_tokens.value, 360)

    def test_progressive_snapshots_cross_each_window_by_canonical_terminal_time(self):
        for kind in UsageWindowKind:
            with self.subTest(kind=kind):
                bounds = usage_window_bounds(
                    kind, as_of_utc=NOW, local_timezone=timezone.utc,
                )
                response = f"response-{kind.value}"
                result = summarize((
                    usage_record(
                        at=bounds.start_utc - timedelta(microseconds=1),
                        response=response, status="in_progress",
                        total_tokens=100, sample_id=1,
                    ),
                    usage_record(
                        at=bounds.start_utc,
                        response=response, status="in_progress",
                        total_tokens=200, sample_id=2,
                    ),
                    usage_record(
                        at=bounds.start_utc + timedelta(microseconds=1),
                        response=response, status="exact",
                        input_tokens=180, output_tokens=120,
                        total_tokens=300, cached_tokens=90,
                        reasoning_tokens=60, sample_id=3,
                    ),
                ), scope=kind)

                self.assertEqual(result.observed_response_count, 1)
                self.assertEqual(result.total_tokens.value, 300)
                self.assertEqual(result.in_progress_observation_count, 1)

                exactly_at_start = summarize((usage_record(
                    at=bounds.start_utc,
                    response=f"at-start-{kind.value}",
                    status="exact",
                    sample_id=30,
                ),), scope=kind)
                just_before_start = summarize((usage_record(
                    at=bounds.start_utc - timedelta(microseconds=1),
                    response=f"before-start-{kind.value}",
                    status="exact",
                    sample_id=31,
                ),), scope=kind)
                self.assertEqual(exactly_at_start.observed_response_count, 1)
                self.assertEqual(just_before_start.observed_response_count, 0)

                outside = summarize((
                    usage_record(
                        at=NOW - timedelta(microseconds=1),
                        response=f"future-{kind.value}", status="in_progress",
                        total_tokens=200, sample_id=4,
                    ),
                    usage_record(
                        at=NOW + timedelta(microseconds=1),
                        response=f"future-{kind.value}", status="exact",
                        total_tokens=300, sample_id=5,
                    ),
                ), scope=kind)
                self.assertEqual(outside.observed_response_count, 0)
                self.assertIsNone(outside.total_tokens.value)

                updated = summarize((
                    usage_record(
                        at=bounds.start_utc - timedelta(microseconds=1),
                        response=f"updated-{kind.value}", source="dashboard",
                        input_tokens=60, output_tokens=40, total_tokens=100,
                        cached_tokens=30, reasoning_tokens=20, sample_id=6,
                    ),
                    usage_record(
                        at=bounds.start_utc + timedelta(microseconds=1),
                        response=f"updated-{kind.value}", source="mini",
                        input_tokens=None, output_tokens=None, total_tokens=300,
                        cached_tokens=None, reasoning_tokens=None, sample_id=7,
                    ),
                ), scope=kind)
                self.assertEqual(updated.observed_response_count, 1)
                self.assertEqual(updated.total_tokens.value, 300)


class UsageAggregationTests(unittest.TestCase):
    def test_progressive_snapshots_count_only_one_terminal_response(self):
        response = "response-progressive"
        records = (
            usage_record(
                at=NOW - timedelta(seconds=3), response=response,
                status="in_progress", input_tokens=60, output_tokens=40,
                total_tokens=100, cached_tokens=30, reasoning_tokens=20,
                sample_id=1,
            ),
            usage_record(
                at=NOW - timedelta(seconds=2), response=response,
                status="in_progress", input_tokens=120, output_tokens=80,
                total_tokens=200, cached_tokens=60, reasoning_tokens=40,
                sample_id=2,
            ),
            usage_record(
                at=NOW - timedelta(seconds=1), response=response,
                status="exact", input_tokens=180, output_tokens=120,
                total_tokens=300, cached_tokens=90, reasoning_tokens=60,
                sample_id=3,
            ),
        )

        result = summarize(records)

        self.assertEqual(result.observed_response_count, 1)
        self.assertEqual((
            result.input_tokens.value,
            result.output_tokens.value,
            result.total_tokens.value,
            result.cached_tokens.value,
            result.reasoning_tokens.value,
        ), (180, 120, 300, 90, 60))
        self.assertEqual(result.average_total_tokens_per_response, 300)
        self.assertEqual(result.cache_reuse.value, 0.5)
        self.assertEqual(result.in_progress_observation_count, 2)
        self.assertEqual(result.coverage.state, CoverageState.PARTIAL)
        self.assertIn(
            "in_progress_excluded",
            {message.code for message in result.coverage_messages},
        )

    def test_only_in_progress_has_no_fake_zero_and_explicit_exclusion(self):
        response = "response-active"
        records = (
            usage_record(
                at=NOW - timedelta(seconds=2), response=response,
                status="in_progress", total_tokens=100, sample_id=1,
            ),
            usage_record(
                at=NOW - timedelta(seconds=1), response=response,
                status="in_progress", total_tokens=200, sample_id=2,
            ),
        )

        result = summarize(records)

        self.assertEqual(result.observed_response_count, 0)
        for metric in (
            result.input_tokens, result.output_tokens, result.total_tokens,
            result.cached_tokens, result.reasoning_tokens,
        ):
            self.assertIsNone(metric.value)
        self.assertIsNone(result.average_total_tokens_per_response)
        self.assertEqual(result.coverage.state, CoverageState.PARTIAL)
        self.assertEqual(result.freshness.state, FreshnessState.UNAVAILABLE)
        self.assertEqual(result.in_progress_observation_count, 2)

    def test_in_progress_to_partial_terminal_counts_legal_fields_once(self):
        response = "response-partial"
        result = summarize((
            usage_record(
                at=NOW - timedelta(seconds=2), response=response,
                status="in_progress", total_tokens=100, sample_id=1,
            ),
            usage_record(
                at=NOW - timedelta(seconds=1), response=response,
                status="completed_partial", input_tokens=None,
                output_tokens=None, total_tokens=200, cached_tokens=None,
                reasoning_tokens=None, sample_id=2,
            ),
        ))

        self.assertEqual(result.observed_response_count, 1)
        self.assertEqual(result.total_tokens.value, 200)
        self.assertIsNone(result.input_tokens.value)
        self.assertEqual(result.coverage.partial_terminal_response_count, 1)
        self.assertEqual(result.coverage.state, CoverageState.PARTIAL)

    def test_post_complete_dashboard_to_mini_exact_update_uses_latest_terminal(self):
        response = "response-updated-exact"
        result = summarize((
            usage_record(
                at=NOW - timedelta(seconds=2), response=response,
                input_tokens=10, output_tokens=2, total_tokens=12,
                cached_tokens=4, reasoning_tokens=1, sample_id=1,
            ),
            usage_record(
                at=NOW - timedelta(seconds=1), response=response,
                source="mini",
                input_tokens=30, output_tokens=6, total_tokens=36,
                cached_tokens=12, reasoning_tokens=3, stale=True, sample_id=2,
            ),
        ))

        self.assertEqual(result.observed_response_count, 1)
        self.assertEqual(result.total_tokens.value, 36)

    def test_same_source_time_fresh_and_stale_exact_count_once(self):
        observed = NOW - timedelta(seconds=1)
        result = summarize((
            usage_record(
                at=observed, response="response-freshness", sample_id=1,
            ),
            usage_record(
                at=observed, response="response-freshness", stale=True,
                sample_id=2,
            ),
        ))

        self.assertEqual(result.observed_response_count, 1)
        self.assertEqual(result.total_tokens.value, 120)

    def test_distinct_response_ids_are_preserved_even_with_identical_values(self):
        observed = NOW - timedelta(seconds=1)
        result = summarize((
            usage_record(at=observed, response="response-a", sample_id=1),
            usage_record(at=observed, response="response-b", sample_id=2),
        ))

        self.assertEqual(result.observed_response_count, 2)
        self.assertEqual(result.total_tokens.value, 240)

    def test_legacy_rows_without_response_identity_are_excluded(self):
        legacy = usage_record()
        object.__setattr__(legacy, "response_safe_id", None)

        result = summarize((legacy,))

        self.assertEqual(result.observed_response_count, 0)
        self.assertIsNone(result.total_tokens.value)
        self.assertEqual(result.coverage.missing_response_identity_count, 1)
        self.assertEqual(result.coverage.state, CoverageState.PARTIAL)

    def test_single_and_multi_session_metrics_use_response_values_only(self):
        records = (
            usage_record(at=NOW - timedelta(minutes=2), session_total_tokens=9_000),
            usage_record(
                at=NOW - timedelta(minutes=1),
                thread="thread-2",
                model="model-2",
                input_tokens=200,
                output_tokens=50,
                total_tokens=250,
                cached_tokens=100,
                reasoning_tokens=10,
                session_total_tokens=50_000,
                sample_id=2,
            ),
        )

        result = summarize(records)

        self.assertEqual(result.input_tokens.value, 300)
        self.assertEqual(result.output_tokens.value, 70)
        self.assertEqual(result.total_tokens.value, 370)
        self.assertEqual(result.cached_tokens.value, 140)
        self.assertEqual(result.reasoning_tokens.value, 15)
        self.assertEqual(result.observed_response_count, 2)
        self.assertEqual(result.covered_thread_count, 2)
        self.assertEqual(result.average_total_tokens_per_response, 185.0)
        self.assertAlmostEqual(result.cache_reuse.value or 0.0, 140 / 300)

    def test_fingerprint_refresh_and_mini_dashboard_cross_read_count_once(self):
        observed = NOW - timedelta(minutes=1)
        mini = usage_record(
            at=observed,
            source="mini",
            input_tokens=None,
            output_tokens=None,
            cached_tokens=None,
            reasoning_tokens=None,
            fingerprint="mini-fingerprint",
            response="response-cross-read",
            sample_id=1,
        )
        dashboard = usage_record(
            at=observed,
            fingerprint="dashboard-fingerprint",
            response="response-cross-read",
            sample_id=2,
        )
        duplicate_refresh = usage_record(
            at=observed,
            fingerprint="dashboard-fingerprint",
            response="response-cross-read",
            sample_id=3,
        )

        result = summarize((mini, dashboard, duplicate_refresh))

        self.assertEqual(result.observed_response_count, 1)
        self.assertEqual(result.total_tokens.value, 120)
        self.assertEqual(result.input_tokens.eligible_record_count, 1)

    def test_identical_values_are_distinct_by_response_time_and_model(self):
        same_time = NOW - timedelta(minutes=1)
        records = (
            usage_record(at=same_time - timedelta(seconds=1), response="response-a", sample_id=1),
            usage_record(at=same_time, response="response-b", sample_id=2),
            usage_record(at=same_time, model="model-2", response="response-c", sample_id=3),
        )

        result = summarize(records)

        self.assertEqual(result.observed_response_count, 3)
        self.assertEqual(result.total_tokens.value, 360)

    def test_session_cumulative_and_quota_only_rows_are_never_summed(self):
        cumulative_only = usage_record(
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cached_tokens=None,
            reasoning_tokens=None,
            session_total_tokens=999_999,
        )
        quota_only = usage_record(
            at=NOW - timedelta(minutes=1),
            thread=None,
            model=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cached_tokens=None,
            reasoning_tokens=None,
            session_total_tokens=None,
            sample_id=2,
        )

        result = summarize((cumulative_only, quota_only))

        self.assertEqual(result.coverage.state, CoverageState.NO_OBSERVATIONS)
        self.assertIsNone(result.total_tokens.value)

    def test_missing_values_are_not_zero_and_only_total_is_supported(self):
        only_total = usage_record(
            input_tokens=None,
            output_tokens=None,
            total_tokens=75,
            cached_tokens=None,
            reasoning_tokens=None,
        )

        result = summarize((only_total,))

        self.assertEqual(result.total_tokens.value, 75)
        self.assertEqual(result.average_total_tokens_per_response, 75.0)
        self.assertIsNone(result.input_tokens.value)
        self.assertEqual(result.input_tokens.missing_record_count, 1)
        self.assertEqual(result.coverage.state, CoverageState.PARTIAL)

    def test_invalid_numeric_types_ranges_and_relations_are_excluded_per_field(self):
        records = (
            usage_record(
                at=NOW - timedelta(seconds=5),
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                cached_tokens=0,
                reasoning_tokens=0,
                sample_id=1,
            ),
            usage_record(
                at=NOW - timedelta(seconds=4),
                input_tokens="100",
                output_tokens=20,
                total_tokens=120,
                cached_tokens=10,
                reasoning_tokens=5,
                sample_id=2,
            ),
            usage_record(
                at=NOW - timedelta(seconds=3),
                input_tokens=-1,
                output_tokens=20,
                total_tokens=19,
                cached_tokens=0,
                reasoning_tokens=5,
                sample_id=3,
            ),
            usage_record(
                at=NOW - timedelta(seconds=2),
                input_tokens=100,
                output_tokens=float("inf"),
                total_tokens=120,
                cached_tokens=float("nan"),
                reasoning_tokens=5,
                sample_id=4,
            ),
            usage_record(
                at=NOW - timedelta(seconds=1),
                input_tokens=MAX_SAFE_TOKEN_VALUE + 1,
                output_tokens=20,
                total_tokens=120,
                cached_tokens=True,
                reasoning_tokens=30,
                sample_id=5,
            ),
        )

        result = summarize(records)

        self.assertEqual(result.input_tokens.eligible_record_count, 2)
        self.assertEqual(result.input_tokens.invalid_record_count, 3)
        self.assertEqual(result.cached_tokens.invalid_record_count, 2)
        self.assertGreaterEqual(result.reasoning_tokens.invalid_record_count, 1)
        self.assertEqual(result.coverage.state, CoverageState.PARTIAL)

    def test_cache_reuse_uses_only_records_with_both_fields_and_handles_zero_input(self):
        records = (
            usage_record(at=NOW - timedelta(seconds=3), input_tokens=100, cached_tokens=50),
            usage_record(
                at=NOW - timedelta(seconds=2),
                input_tokens=200,
                cached_tokens=None,
                total_tokens=220,
                sample_id=2,
            ),
            usage_record(
                at=NOW - timedelta(seconds=1),
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                cached_tokens=0,
                reasoning_tokens=0,
                sample_id=3,
            ),
        )

        result = summarize(records)
        zero_only = summarize((records[-1],))

        self.assertEqual(result.cache_reuse.value, 0.5)
        self.assertEqual(result.cache_reuse.eligible_record_count, 2)
        self.assertEqual(result.cache_reuse.missing_record_count, 1)
        self.assertIsNone(zero_only.cache_reuse.value)

    def test_invalid_thread_identity_is_excluded_from_strict_response_totals(self):
        records = (
            usage_record(thread="thread-1", sample_id=1),
            usage_record(at=NOW - timedelta(seconds=1), thread=None, sample_id=2),
            usage_record(at=NOW - timedelta(seconds=2), thread="bad id", sample_id=3),
        )

        result = summarize(records)

        self.assertEqual(result.observed_response_count, 1)
        self.assertEqual(result.covered_thread_count, 1)
        self.assertEqual(result.coverage.thread_eligible_record_count, 1)
        self.assertEqual(result.coverage.thread_missing_record_count, 0)
        self.assertEqual(result.coverage.missing_response_identity_count, 2)
        self.assertEqual(result.coverage.state, CoverageState.PARTIAL)

    def test_coverage_states_are_distinct_and_explain_field_counts(self):
        start = NOW - timedelta(hours=5)
        complete = summarize((usage_record(),), first=start - timedelta(seconds=1))
        limited = summarize((usage_record(),), first=NOW - timedelta(minutes=10))
        partial = summarize((usage_record(cached_tokens=None),), first=start)
        empty = summarize(())
        unknown = summarize((), unknown=2)
        unavailable = unavailable_usage_summary(
            UsageWindowKind.ROLLING_5H,
            as_of_utc=NOW,
            local_timezone=timezone.utc,
            error_code="history_query_failed",
        )

        self.assertEqual(complete.coverage.state, CoverageState.COMPLETE_FOR_LOCAL_HISTORY)
        self.assertEqual(limited.coverage.state, CoverageState.LIMITED_HISTORY)
        self.assertEqual(partial.coverage.state, CoverageState.PARTIAL)
        self.assertEqual(empty.coverage.state, CoverageState.NO_OBSERVATIONS)
        self.assertEqual(unknown.coverage.state, CoverageState.UNKNOWN)
        self.assertEqual(unavailable.coverage.state, CoverageState.UNAVAILABLE)
        cached = next(
            item for item in partial.coverage.messages if item.metric == "cached_tokens"
        )
        self.assertEqual((cached.eligible_count, cached.total_count), (0, 1))

    def test_fresh_stale_and_unavailable_are_independent_from_coverage(self):
        fresh = summarize((usage_record(),))
        old = summarize((usage_record(at=NOW - timedelta(minutes=4)),))
        marked = summarize((usage_record(stale=True),))
        empty = summarize(())

        self.assertEqual(fresh.freshness.state, FreshnessState.FRESH)
        self.assertEqual(old.freshness.state, FreshnessState.STALE)
        self.assertEqual(marked.freshness.state, FreshnessState.STALE)
        self.assertEqual(empty.freshness.state, FreshnessState.UNAVAILABLE)

    def test_missing_source_time_lowers_coverage_without_using_recorded_time(self):
        missing_time = usage_record(at=NOW - timedelta(seconds=1))
        object.__setattr__(missing_time, "source_observed_at", None)

        result = summarize((missing_time,))

        self.assertEqual(result.coverage.state, CoverageState.UNKNOWN)
        self.assertEqual(result.coverage.unknown_time_record_count, 1)
        self.assertEqual(result.observed_response_count, 0)


class UsageHistorySummaryStoreTests(unittest.TestCase):
    def make_store(self, directory: str) -> UsageHistoryStore:
        return UsageHistoryStore(
            Path(directory) / "usage-history.sqlite3",
            clock=lambda: NOW,
        )

    @staticmethod
    def observation(
        *,
        at: datetime,
        sampled_at: datetime | None = None,
        source: str = "dashboard",
        thread: str | None = "thread-1",
        input_tokens: int | None = 100,
        output_tokens: int | None = 20,
        total_tokens: int | None = 120,
        cached_tokens: int | None = 40,
        reasoning_tokens: int | None = 5,
        session_total_tokens: int | None = 5_000,
        response: str | None = None,
    ) -> HistoryObservation:
        return HistoryObservation(
            sampled_at=sampled_at or at,
            source_observed_at=at,
            thread_safe_id=thread,
            response_safe_id=make_response_safe_id(
                thread or "missing-thread",
                response or f"response-{int(at.timestamp() * 1_000_000)}",
            ),
            model_safe_id="model-1",
            source_type=source,
            source_status="exact",
            source_available=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_tokens=cached_tokens,
            reasoning_tokens=reasoning_tokens,
            session_total_tokens=session_total_tokens,
            turn_count=4,
        )

    def test_store_summarizes_global_responses_and_not_session_cumulative(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.record(self.observation(at=NOW - timedelta(minutes=2)))
            store.record(self.observation(
                at=NOW - timedelta(minutes=1),
                thread="thread-2",
                input_tokens=200,
                output_tokens=50,
                total_tokens=250,
                cached_tokens=100,
                reasoning_tokens=10,
                session_total_tokens=99_999,
            ))

            result = store.summarize_usage(
                UsageWindowKind.ROLLING_5H,
                as_of_utc=NOW,
                local_timezone=timezone.utc,
            )

            self.assertEqual(result.total_tokens.value, 370)
            self.assertEqual(result.observed_response_count, 2)
            self.assertEqual(result.covered_thread_count, 2)

    def test_store_progressive_snapshots_use_one_response_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            response = "response-progressive-store"
            for index, (status, total) in enumerate((
                ("in_progress", 100),
                ("in_progress", 200),
                ("exact", 300),
            ), 1):
                item = self.observation(
                    at=NOW - timedelta(seconds=4 - index),
                    sampled_at=NOW - timedelta(seconds=4 - index),
                    input_tokens=total * 3 // 5,
                    output_tokens=total * 2 // 5,
                    total_tokens=total,
                    cached_tokens=total * 3 // 10,
                    reasoning_tokens=total // 5,
                    response=response,
                )
                item = replace(item, source_status=status)
                self.assertTrue(store.record(item))

            result = store.summarize_usage(
                UsageWindowKind.ROLLING_5H,
                as_of_utc=NOW,
                local_timezone=timezone.utc,
            )

            self.assertEqual(result.observed_response_count, 1)
            self.assertEqual(result.total_tokens.value, 300)
            self.assertEqual(result.in_progress_observation_count, 2)

    def test_store_only_in_progress_then_partial_terminal_is_honest(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            response = "response-store-partial-terminal"
            for index, total in enumerate((100, 200), 1):
                observed = NOW - timedelta(seconds=4 - index)
                item = self.observation(
                    at=observed,
                    sampled_at=observed,
                    total_tokens=total,
                    response=response,
                )
                self.assertTrue(store.record(replace(
                    item, source_status="in_progress",
                )))

            in_progress = store.summarize_usage(
                UsageWindowKind.ROLLING_5H,
                as_of_utc=NOW,
                local_timezone=timezone.utc,
            )
            self.assertEqual(in_progress.coverage_state, "partial")
            self.assertEqual(in_progress.observed_response_count, 0)
            self.assertIsNone(in_progress.total_tokens.value)
            self.assertEqual(in_progress.in_progress_observation_count, 2)

            terminal_at = NOW - timedelta(seconds=1)
            terminal = self.observation(
                at=terminal_at,
                sampled_at=terminal_at,
                input_tokens=None,
                output_tokens=None,
                total_tokens=300,
                cached_tokens=None,
                reasoning_tokens=None,
                response=response,
            )
            self.assertTrue(store.record(replace(
                terminal, source_status="completed_partial",
            )))
            completed = store.summarize_usage(
                UsageWindowKind.ROLLING_5H,
                as_of_utc=NOW,
                local_timezone=timezone.utc,
            )
            self.assertEqual(completed.coverage_state, "partial")
            self.assertEqual(completed.observed_response_count, 1)
            self.assertEqual(completed.total_tokens.value, 300)
            self.assertEqual(completed.in_progress_observation_count, 2)

    def test_store_post_complete_cross_source_update_changes_summary_and_trend(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            response = "response-cross-source-update"
            self.assertTrue(store.record(self.observation(
                at=NOW - timedelta(seconds=2), source="dashboard",
                input_tokens=10, output_tokens=2, total_tokens=12,
                cached_tokens=4, reasoning_tokens=1, response=response,
            )))
            self.assertTrue(store.record(self.observation(
                at=NOW - timedelta(seconds=1), source="mini",
                input_tokens=None, output_tokens=None, total_tokens=36,
                cached_tokens=None, reasoning_tokens=None, response=response,
            )))

            summary = store.summarize_usage(
                UsageWindowKind.ROLLING_5H,
                as_of_utc=NOW,
                local_timezone=timezone.utc,
            )
            query = store.query(7, "thread-1", now=NOW)

            self.assertEqual(summary.observed_response_count, 1)
            self.assertEqual(summary.total_tokens.value, 36)
            self.assertEqual(query.sample_count, 1)
            self.assertEqual(query.samples[0].total_tokens, 36)
            self.assertEqual(query.samples[0].source_type, "mini")
            self.assertEqual(
                (
                    query.samples[0].input_tokens,
                    query.samples[0].output_tokens,
                    query.samples[0].cached_tokens,
                    query.samples[0].reasoning_tokens,
                ),
                (None, None, None, None),
            )

    def test_progressive_token_filter_preserves_quota_round_trip_and_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            response = "response-progressive-quota"
            reset = NOW + timedelta(hours=4)
            weekly_observed = NOW - timedelta(minutes=3)
            for index, (status, total, remaining) in enumerate((
                ("in_progress", 100, 80.0),
                ("in_progress", 200, 70.0),
                ("exact", 300, 80.0),
            ), 1):
                observed = NOW - timedelta(minutes=11 - index)
                item = self.observation(
                    at=observed,
                    sampled_at=observed,
                    input_tokens=total * 3 // 5,
                    output_tokens=total * 2 // 5,
                    total_tokens=total,
                    cached_tokens=total * 3 // 10,
                    reasoning_tokens=total // 5,
                    response=response,
                )
                item = replace(
                    item,
                    source_status=status,
                    quota_observed_at=observed,
                    quota_source_status="normal",
                    five_hour_observed_at=observed,
                    five_hour_last_seen_at=observed,
                    five_hour_used_percent=100.0 - remaining,
                    five_hour_remaining_percent=remaining,
                    five_hour_reset_at=reset,
                    five_hour_source="codex_app_server",
                    five_hour_available=True,
                    weekly_observed_at=weekly_observed,
                    weekly_last_seen_at=observed,
                    weekly_used_percent=40.0,
                    weekly_remaining_percent=60.0,
                    weekly_reset_at=NOW + timedelta(days=5),
                    weekly_source="codex_app_server",
                    weekly_available=True,
                )
                self.assertTrue(store.record(item))

            for index in range(500):
                observed = NOW - timedelta(minutes=7) + timedelta(milliseconds=index)
                item = self.observation(
                    at=observed,
                    sampled_at=observed,
                    input_tokens=180,
                    output_tokens=120,
                    total_tokens=300,
                    cached_tokens=90,
                    reasoning_tokens=60,
                    response=response,
                )
                item = replace(
                    item,
                    source_status="exact",
                    quota_observed_at=observed,
                    quota_source_status="normal",
                    five_hour_observed_at=observed,
                    five_hour_last_seen_at=observed,
                    five_hour_used_percent=20.0,
                    five_hour_remaining_percent=80.0,
                    five_hour_reset_at=reset,
                    five_hour_source="codex_app_server",
                    five_hour_available=True,
                    weekly_observed_at=weekly_observed,
                    weekly_last_seen_at=observed,
                    weekly_used_percent=40.0,
                    weekly_remaining_percent=60.0,
                    weekly_reset_at=NOW + timedelta(days=5),
                    weekly_source="codex_app_server",
                    weekly_available=True,
                )
                self.assertTrue(store.record(item))

            summary = store.summarize_usage(
                UsageWindowKind.ROLLING_5H,
                as_of_utc=NOW,
                local_timezone=timezone.utc,
            )
            view = trend_view_from_query(store.query(7, now=NOW))

            self.assertEqual(summary.observed_response_count, 1)
            self.assertEqual(summary.total_tokens.value, 300)
            self.assertEqual(
                [value for _sample, value in metric_samples(view, "five_hour")],
                [80.0, 70.0, 80.0],
            )
            self.assertEqual(
                [value for _sample, value in metric_samples(view, "weekly")],
                [60.0],
            )

    def test_store_uses_source_time_and_inclusive_as_of_not_recorded_or_last_seen(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.record(self.observation(
                at=NOW - timedelta(hours=5, milliseconds=1),
                sampled_at=NOW,
            ))
            store.record(self.observation(
                at=NOW - timedelta(hours=5),
                sampled_at=NOW - timedelta(days=3),
                input_tokens=101,
                total_tokens=121,
            ))
            store.record(self.observation(
                at=NOW + timedelta(milliseconds=1),
                total_tokens=122,
            ))

            result = store.summarize_usage(
                UsageWindowKind.ROLLING_5H,
                as_of_utc=NOW,
                local_timezone=timezone.utc,
            )

            self.assertEqual(result.observed_response_count, 1)
            self.assertEqual(result.total_tokens.value, 121)

    def test_store_merges_mini_dashboard_and_excludes_quota_only_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            at = NOW - timedelta(minutes=1)
            store.record(self.observation(
                at=at,
                source="mini",
                input_tokens=None,
                output_tokens=None,
                cached_tokens=None,
                reasoning_tokens=None,
            ))
            store.record(self.observation(at=at, sampled_at=NOW))
            store.record(HistoryObservation(
                sampled_at=NOW,
                quota_observed_at=NOW,
                source_type="dashboard",
                source_status="unavailable",
                quota_source_status="normal",
                five_hour_observed_at=NOW,
                five_hour_last_seen_at=NOW,
                five_hour_used_percent=20.0,
                five_hour_remaining_percent=80.0,
                five_hour_source="codex_app_server",
                five_hour_available=True,
            ))

            result = store.summarize_usage(
                UsageWindowKind.ROLLING_5H,
                as_of_utc=NOW,
                local_timezone=timezone.utc,
            )

            self.assertEqual(result.observed_response_count, 1)
            self.assertEqual(result.total_tokens.value, 120)
            self.assertEqual(result.input_tokens.value, 100)

    def test_duplicate_quota_last_seen_update_does_not_increase_response_count(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first = self.observation(at=NOW - timedelta(minutes=1))
            self.assertTrue(store.record(first))
            self.assertFalse(store.record(first))

            result = store.summarize_usage(
                UsageWindowKind.ROLLING_5H,
                as_of_utc=NOW,
                local_timezone=timezone.utc,
            )

            self.assertEqual(result.observed_response_count, 1)

    def test_global_source_time_index_exists_and_bounds_query_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            self.assertTrue(store.initialize())
            with closing(sqlite3.connect(store.path)) as connection:
                indexes = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA index_list(usage_history_samples)"
                    )
                }
                plan = connection.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM usage_history_samples "
                    "WHERE source_observed_at_utc >= ? "
                    "AND source_observed_at_utc <= ? "
                    "ORDER BY source_observed_at_utc, sampled_at_utc, id",
                    (
                        "2026-07-15T07:00:00.000000Z",
                        "2026-07-15T12:00:00.000000Z",
                    ),
                ).fetchall()

            self.assertIn("ix_usage_history_samples_source_observed", indexes)
            self.assertIn(
                "ix_usage_history_samples_source_observed",
                " ".join(str(item) for row in plan for item in row),
            )

    def test_query_contract_streams_one_identity_ordered_projection(self):
        source = inspect.getsource(UsageHistoryStore.summarize_usage)

        self.assertEqual(source.count("connection.execute("), 1)
        self.assertIn("_USAGE_SUMMARY_COLUMNS", source)
        self.assertIn("ORDER BY thread_safe_id, response_safe_id", source)
        self.assertNotIn("SELECT *", source)
        self.assertNotIn("last_seen_at_utc >=", source)
        self.assertNotIn(".fetchall()", source)
        self.assertIn("fetchmany(512)", source)
        self.assertIn("records_grouped_by_response=True", source)

    def test_unavailable_store_returns_non_crashing_unavailable_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            parent_file = Path(directory) / "not-a-directory"
            parent_file.write_text("x", encoding="utf-8")
            store = UsageHistoryStore(parent_file / "history.sqlite3", clock=lambda: NOW)

            result = store.summarize_usage(
                UsageWindowKind.TODAY,
                as_of_utc=NOW,
                local_timezone=timezone.utc,
            )

            self.assertEqual(result.coverage.state, CoverageState.UNAVAILABLE)
            self.assertEqual(result.freshness.state, FreshnessState.UNAVAILABLE)
            self.assertIsNotNone(result.error_code)


class UsageSummaryUiContractTests(unittest.TestCase):
    @staticmethod
    def dashboard(summary) -> Dashboard:
        dashboard = object.__new__(Dashboard)
        dashboard.language = "en"
        dashboard.observed_usage_summary = summary
        dashboard.observed_usage_metric_widgets = {
            name: {"value": Mock(), "full": Mock()}
            for name in ("total", "input", "output", "cached", "reasoning")
        }
        dashboard.observed_usage_aux_widgets = {
            name: {"value": Mock()}
            for name in ("responses", "sessions", "average", "cache_reuse")
        }
        dashboard.observed_usage_coverage_var = Mock()
        dashboard.observed_usage_coverage_label = Mock()
        dashboard._full_token_tooltip = lambda value: "—" if value is None else str(value)
        return dashboard

    def test_core_scope_labels_and_quota_title_are_explicit_in_both_languages(self):
        self.assertEqual(translate("core_metric_current_turn", "zh-CN"), "当前指令")
        self.assertEqual(translate("core_metric_current_turn", "en"), "Current response")
        self.assertEqual(translate("core_metric_session_total", "zh-CN"), "当前会话")
        self.assertEqual(translate("core_metric_session_total", "en"), "Current session")
        self.assertEqual(translate("quota_center_title", "zh-CN"), "官方额度状态")
        self.assertEqual(translate("quota_center_title", "en"), "Quota status")

    def test_no_history_renders_dashes_instead_of_misleading_zero(self):
        summary = summarize(())
        dashboard = self.dashboard(summary)

        Dashboard._render_observed_usage(dashboard)

        for widget in dashboard.observed_usage_metric_widgets.values():
            widget["value"].set.assert_called_once_with("—")
        dashboard.observed_usage_aux_widgets["responses"]["value"].set.assert_called_once_with("—")
        coverage = dashboard.observed_usage_coverage_var.set.call_args.args[0]
        self.assertIn("No observed data", coverage)

    def test_partial_history_renders_total_and_field_coverage_explanation(self):
        summary = summarize((usage_record(
            input_tokens=None,
            output_tokens=None,
            total_tokens=75,
            cached_tokens=None,
            reasoning_tokens=None,
        ),))
        dashboard = self.dashboard(summary)

        Dashboard._render_observed_usage(dashboard)

        dashboard.observed_usage_metric_widgets["total"]["value"].set.assert_called_once_with("75")
        dashboard.observed_usage_metric_widgets["input"]["value"].set.assert_called_once_with("—")
        coverage = dashboard.observed_usage_coverage_var.set.call_args.args[0]
        self.assertIn("Some historical fields are unavailable", coverage)
        self.assertIn("Input covers 0/1 responses", coverage)

    def test_only_in_progress_renders_no_completed_and_exclusion_messages(self):
        summary = summarize((
            usage_record(
                at=NOW - timedelta(seconds=2), response="response-active-ui",
                status="in_progress", total_tokens=100, sample_id=1,
            ),
            usage_record(
                at=NOW - timedelta(seconds=1), response="response-active-ui",
                status="in_progress", total_tokens=200, sample_id=2,
            ),
        ))
        dashboard = self.dashboard(summary)

        Dashboard._render_observed_usage(dashboard)

        for widget in dashboard.observed_usage_metric_widgets.values():
            widget["value"].set.assert_called_once_with("—")
        dashboard.observed_usage_aux_widgets["responses"]["value"].set.assert_called_once_with("—")
        coverage = dashboard.observed_usage_coverage_var.set.call_args.args[0]
        self.assertIn("No completed observations yet", coverage)
        self.assertIn("In-progress responses are not included yet", coverage)
        self.assertIn("They will be included after completion", coverage)

    def test_usage_window_switch_only_schedules_local_history_query(self):
        dashboard = object.__new__(Dashboard)
        dashboard.usage_window_kind = UsageWindowKind.TODAY
        dashboard.usage_window_labels = {
            "Last 5 hours": UsageWindowKind.ROLLING_5H,
        }
        dashboard._schedule_trend_query = Mock()
        dashboard._render_observed_usage = Mock()

        Dashboard._change_usage_window(dashboard, "Last 5 hours")

        self.assertEqual(dashboard.usage_window_kind, UsageWindowKind.ROLLING_5H)
        dashboard._schedule_trend_query.assert_called_once_with()
        dashboard._render_observed_usage.assert_called_once_with()
        source = inspect.getsource(Dashboard._change_usage_window)
        self.assertNotIn("view_model.refresh", source)
        self.assertNotIn("quota_provider.refresh", source)
        self.assertNotIn("_record_history", source)

    def test_history_worker_queries_trend_and_summary_off_tk_thread(self):
        source = inspect.getsource(Dashboard._trend_query_worker_loop)

        self.assertIn("_query_trend_view", source)
        self.assertIn("_query_observed_usage", source)
        self.assertNotIn("view_model.refresh", source)
        self.assertNotIn("quota_provider.refresh", source)
        self.assertNotIn("_record_history", source)


if __name__ == "__main__":
    unittest.main()
