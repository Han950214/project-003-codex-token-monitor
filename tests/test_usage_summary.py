from __future__ import annotations

import inspect
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest.mock import Mock

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
    source: str = "dashboard",
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
        model_safe_id=model,
        source_type=source,
        source_status="stale" if stale else "exact",
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


class UsageAggregationTests(unittest.TestCase):
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
            sample_id=1,
        )
        dashboard = usage_record(
            at=observed,
            fingerprint="dashboard-fingerprint",
            sample_id=2,
        )
        duplicate_refresh = usage_record(
            at=observed,
            fingerprint="dashboard-fingerprint",
            sample_id=3,
        )

        result = summarize((mini, dashboard, duplicate_refresh))

        self.assertEqual(result.observed_response_count, 1)
        self.assertEqual(result.total_tokens.value, 120)
        self.assertEqual(result.input_tokens.eligible_record_count, 1)

    def test_identical_values_are_distinct_by_response_time_and_model(self):
        same_time = NOW - timedelta(minutes=1)
        records = (
            usage_record(at=same_time - timedelta(seconds=1), sample_id=1),
            usage_record(at=same_time, sample_id=2),
            usage_record(at=same_time, model="model-2", sample_id=3),
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

    def test_thread_count_uses_only_valid_safe_ids_without_dropping_tokens(self):
        records = (
            usage_record(thread="thread-1", sample_id=1),
            usage_record(at=NOW - timedelta(seconds=1), thread=None, sample_id=2),
            usage_record(at=NOW - timedelta(seconds=2), thread="bad id", sample_id=3),
        )

        result = summarize(records)

        self.assertEqual(result.observed_response_count, 3)
        self.assertEqual(result.covered_thread_count, 1)
        self.assertEqual(result.coverage.thread_eligible_record_count, 1)
        self.assertEqual(result.coverage.thread_missing_record_count, 2)
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
        missing_time = usage_record()
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
    ) -> HistoryObservation:
        return HistoryObservation(
            sampled_at=sampled_at or at,
            source_observed_at=at,
            thread_safe_id=thread,
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

    def test_query_contract_bounds_membership_by_source_time_and_never_last_seen(self):
        source = inspect.getsource(UsageHistoryStore.summarize_usage)

        self.assertIn("source_observed_at_utc >= ?", source)
        self.assertIn("source_observed_at_utc <= ?", source)
        self.assertNotIn("last_seen_at_utc >=", source)
        self.assertIn(") AND sampled_at_utc >= ? AND sampled_at_utc <= ?", source)

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
