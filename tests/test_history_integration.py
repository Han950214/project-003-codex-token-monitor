from __future__ import annotations

import inspect
import queue
import unittest
from datetime import datetime, timezone
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
                sampled_at=now,
                five_hour_available=True,
                five_hour_stale=True,
            ),
        )
        view = TrendView(
            7, "available", token_samples, now, quota_samples=quota_samples,
        )

        self.assertEqual(
            Dashboard._trend_metric_quality(view, "total", 2), "available",
        )
        self.assertEqual(
            Dashboard._trend_metric_quality(view, "five_hour", 1), "stale",
        )

    def test_smoke_initializes_the_history_database(self):
        source = inspect.getsource(smoke)
        self.assertIn("UsageHistoryStore", source)
        self.assertIn("history.initialize()", source)


if __name__ == "__main__":
    unittest.main()
