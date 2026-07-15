from __future__ import annotations

import inspect
import queue
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.analytics_ui import TrendView
from app.main import Dashboard, TREND_GROUP_METRICS, smoke


class HistoryIntegrationContractTests(unittest.TestCase):
    def test_required_trend_metrics_are_grouped_once(self):
        grouped = tuple(
            metric
            for metrics in TREND_GROUP_METRICS.values()
            for metric in metrics
        )
        self.assertEqual(
            grouped,
            (
                "input", "output", "total",
                "cached", "cache_reuse", "reasoning",
                "session_total", "turn_count",
                "five_hour", "weekly",
            ),
        )
        self.assertEqual(len(grouped), len(set(grouped)))

    def test_range_switch_reads_history_without_refreshing_sources_or_writing(self):
        source = inspect.getsource(Dashboard._change_trend_range)
        self.assertIn("_schedule_trend_query", source)
        self.assertNotIn("view_model.refresh", source)
        self.assertNotIn("quota_provider.refresh", source)
        self.assertNotIn("_record_history", source)

    def test_cached_thread_selection_queries_without_writing(self):
        source = inspect.getsource(Dashboard._apply_cached_snapshot)
        self.assertIn("_schedule_trend_query", source)
        self.assertNotIn("_record_history", source)

    def test_interactive_history_queries_run_off_the_tk_thread(self):
        schedule = inspect.getsource(Dashboard._schedule_trend_query)
        worker = inspect.getsource(Dashboard._trend_query_worker_loop)
        poll = inspect.getsource(Dashboard._poll_trend_query_results)
        refresh = inspect.getsource(Dashboard._refresh_trend_query)
        self.assertIn("_trend_query_requests", schedule)
        self.assertNotIn("threading.Thread", schedule)
        self.assertIn("_query_trend_view", worker)
        self.assertIn("self.root.after", poll)
        self.assertIn("_invalidate_pending_trend_queries", refresh)

    def test_sync_refresh_invalidates_queued_and_completed_async_queries(self):
        dashboard = object.__new__(Dashboard)
        dashboard._trend_query_generation = 3
        dashboard._trend_query_poll_scheduled = True
        dashboard._trend_query_requests = queue.Queue()
        dashboard._trend_query_results = queue.Queue()
        dashboard._trend_query_requests.put((3, 7, "thread-1"))
        dashboard._trend_query_results.put((3, object(), None))

        dashboard._invalidate_pending_trend_queries()

        self.assertEqual(dashboard._trend_query_generation, 4)
        self.assertFalse(dashboard._trend_query_poll_scheduled)
        self.assertTrue(dashboard._trend_query_requests.empty())
        self.assertTrue(dashboard._trend_query_results.empty())

    def test_missing_metric_is_unavailable_but_no_history_is_empty(self):
        now = datetime(2026, 7, 15, tzinfo=timezone.utc)
        sample = SimpleNamespace(sampled_at=now)
        with_missing_metric = TrendView(7, "available", (sample,), now)
        no_history = TrendView(7, "empty", (), None)

        self.assertEqual(
            Dashboard._trend_metric_quality(with_missing_metric, "reasoning", 0),
            "unavailable",
        )
        self.assertEqual(
            Dashboard._trend_metric_quality(no_history, "reasoning", 0),
            "empty",
        )

    def test_quota_staleness_does_not_change_token_metric_quality(self):
        now = datetime.now(timezone.utc)
        token_samples = (
            SimpleNamespace(sampled_at=now, total_tokens=1),
            SimpleNamespace(sampled_at=now, total_tokens=2),
        )
        quota_samples = (
            SimpleNamespace(
                five_hour_observed_at=now,
                five_hour_last_seen_at=now,
                five_hour_available=True,
                five_hour_stale=True,
            ),
        )
        view = TrendView(
            7, "available", token_samples, now, quota_samples=quota_samples,
            five_hour_last_seen_at=now,
            five_hour_available=True,
            five_hour_stale=True,
        )

        self.assertEqual(
            Dashboard._trend_metric_quality(view, "total", 2), "available",
        )
        self.assertEqual(
            Dashboard._trend_metric_quality(view, "five_hour", 1), "stale",
        )

    def test_quota_last_seen_controls_freshness_without_moving_value_time(self):
        now = datetime.now(timezone.utc)
        value_time = now - timedelta(minutes=10)
        sample = SimpleNamespace(
            five_hour_observed_at=value_time,
            five_hour_last_seen_at=now,
            five_hour_remaining_percent=42.0,
            five_hour_available=True,
            five_hour_stale=False,
        )
        fresh = TrendView(
            7, "stale", (), None,
            quota_samples=(sample,),
            five_hour_last_seen_at=now,
            five_hour_available=True,
            five_hour_stale=False,
        )
        old = TrendView(
            7, "available", (), None,
            quota_samples=(sample,),
            five_hour_last_seen_at=now - timedelta(minutes=4),
            five_hour_available=True,
            five_hour_stale=False,
        )

        self.assertEqual(
            Dashboard._trend_metric_quality(fresh, "five_hour", 1), "insufficient",
        )
        self.assertEqual(
            Dashboard._trend_metric_quality(old, "five_hour", 1), "stale",
        )

    def test_each_quota_window_has_independent_quality(self):
        now = datetime.now(timezone.utc)
        sample = SimpleNamespace(
            five_hour_observed_at=now,
            five_hour_remaining_percent=50.0,
            five_hour_available=True,
            five_hour_stale=False,
            weekly_observed_at=now,
            weekly_remaining_percent=40.0,
            weekly_available=True,
            weekly_stale=True,
        )
        view = TrendView(
            7, "available", (), None,
            quota_samples=(sample,),
            five_hour_last_seen_at=now,
            weekly_last_seen_at=now,
            five_hour_available=True,
            five_hour_stale=False,
            weekly_available=True,
            weekly_stale=True,
        )

        self.assertEqual(
            Dashboard._trend_metric_quality(view, "five_hour", 1), "insufficient",
        )
        self.assertEqual(
            Dashboard._trend_metric_quality(view, "weekly", 1), "stale",
        )

    def test_chart_points_use_metric_source_times(self):
        token_time = datetime(2026, 7, 15, 8, tzinfo=timezone.utc)
        quota_time = token_time + timedelta(minutes=1)
        capture_time = token_time + timedelta(hours=1)
        token = SimpleNamespace(
            source_observed_at=token_time,
            sampled_at=capture_time,
            source_available=True,
            total_tokens=100,
            source_type="dashboard",
            token_stale=False,
        )
        quota = SimpleNamespace(
            five_hour_observed_at=quota_time,
            sampled_at=capture_time,
            five_hour_available=True,
            five_hour_stale=False,
            five_hour_remaining_percent=50.0,
            five_hour_source="codex_app_server",
        )
        view = TrendView(7, "available", (token,), token_time, quota_samples=(quota,))
        dashboard = object.__new__(Dashboard)

        self.assertEqual(dashboard._trend_points(view, "total")[0].observed_at, token_time)
        self.assertEqual(
            dashboard._trend_points(view, "five_hour")[0].observed_at, quota_time,
        )

    def test_all_metrics_have_explicit_thread_or_global_scope(self):
        for metric in (
            "input", "output", "total", "cached", "cache_reuse", "reasoning",
            "session_total", "turn_count",
        ):
            self.assertEqual(Dashboard._trend_scope_key(metric), "trend_scope_thread")
        for metric in ("five_hour", "weekly"):
            self.assertEqual(Dashboard._trend_scope_key(metric), "trend_scope_global")
        self.assertEqual(
            Dashboard._history_scope_key("global_quota_history"), "trend_scope_global",
        )
        self.assertEqual(
            Dashboard._history_scope_key("token_monitor_history"), "trend_scope_thread",
        )

    def test_smoke_initializes_the_history_database(self):
        source = inspect.getsource(smoke)
        self.assertIn("UsageHistoryStore", source)
        self.assertIn("history.initialize()", source)


if __name__ == "__main__":
    unittest.main()
