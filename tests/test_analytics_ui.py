from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.analytics_ui import (
    SafeTrendSample,
    TREND_QUALITY_STATES,
    TREND_STALE_AFTER,
    TrendView,
    build_trend_view,
    classify_trend_quality,
    metric_observed_at,
    metric_samples,
    summarize_metric,
    trend_view_from_query,
)


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)


def session(minutes_ago: int, total: int, *, with_usage: bool = True):
    usage = None
    if with_usage:
        usage = SimpleNamespace(
            input_tokens=total - 20,
            cached_input_tokens=10,
            output_tokens=20,
            reasoning_output_tokens=5,
            total_tokens=total,
        )
    return SimpleNamespace(
        observed_at=NOW - timedelta(minutes=minutes_ago),
        instruction=SimpleNamespace(usage=usage) if with_usage else None,
        thread_cumulative_usage=SimpleNamespace(total_tokens=total * 2),
        turn_count=3,
    )


def snapshot(*sessions, refreshed_at: datetime = NOW, rollout_available: bool = True):
    return SimpleNamespace(
        recent_sessions=tuple(sessions),
        sessions_result=SimpleNamespace(refreshed_at=refreshed_at),
        rollout=SimpleNamespace(available=rollout_available),
    )


class TrendQualityTests(unittest.TestCase):
    def test_all_five_quality_states_are_explicit(self):
        self.assertEqual(
            TREND_QUALITY_STATES,
            ("empty", "available", "insufficient", "unavailable", "stale"),
        )
        self.assertEqual(
            classify_trend_quality(
                2, source_available=True, refreshed_at=NOW, now=NOW,
            ),
            "available",
        )
        self.assertEqual(
            classify_trend_quality(
                1, source_available=True, refreshed_at=NOW, now=NOW,
            ),
            "insufficient",
        )
        self.assertEqual(
            classify_trend_quality(
                0, source_available=False, refreshed_at=NOW, now=NOW,
            ),
            "unavailable",
        )
        self.assertEqual(
            classify_trend_quality(
                0, source_available=True, refreshed_at=NOW, now=NOW,
            ),
            "empty",
        )
        self.assertEqual(
            classify_trend_quality(
                2, source_available=True,
                refreshed_at=NOW - TREND_STALE_AFTER - timedelta(seconds=1),
                now=NOW,
            ),
            "stale",
        )

    def test_real_samples_are_preserved_without_synthetic_points(self):
        current = session(1, 120)
        older = session(2, 80)
        missing = session(3, 50, with_usage=False)

        view = build_trend_view(
            snapshot(current, older, missing), 7, now=NOW,
        )

        self.assertEqual(view.quality, "available")
        self.assertEqual([item.total_tokens for item in view.samples], [80, 120])
        self.assertEqual(len(view.samples), 2)
        self.assertEqual(view.samples[0].cache_reuse_percent, 10 / 60 * 100)

    def test_one_real_sample_is_insufficient_and_draws_no_extra_sample(self):
        view = build_trend_view(snapshot(session(1, 120)), 7, now=NOW)
        self.assertEqual(view.quality, "insufficient")
        self.assertEqual([item.total_tokens for item in view.samples], [120])

    def test_no_snapshot_is_unavailable(self):
        view = build_trend_view(None, 30, now=NOW)
        self.assertEqual((view.quality, view.samples), ("unavailable", ()))

    def test_range_filter_uses_existing_timestamps_only(self):
        view = build_trend_view(
            snapshot(session(1, 100), session(8 * 24 * 60, 200)),
            7,
            now=NOW,
        )
        self.assertEqual([item.total_tokens for item in view.samples], [100])
        self.assertEqual(view.quality, "insufficient")

    def test_local_query_projection_and_summary_keep_thread_and_global_scope(self):
        thread_samples = tuple(
            SimpleNamespace(
                sampled_at=NOW + timedelta(hours=1),
                source_observed_at=NOW - timedelta(minutes=2 - index),
                total_tokens=value,
                cache_reuse_ratio=0.25,
            )
            for index, value in enumerate((100, 160))
        )
        quota_samples = (
            SimpleNamespace(
                sampled_at=NOW + timedelta(hours=1),
                five_hour_observed_at=NOW - timedelta(minutes=1),
                five_hour_available=True,
                five_hour_used_percent=57.5,
                five_hour_remaining_percent=42.5,
                five_hour_reset_at=NOW + timedelta(hours=2),
                five_hour_source="codex_app_server",
                weekly_observed_at=NOW - timedelta(minutes=1),
                weekly_available=True,
                weekly_used_percent=25.0,
                weekly_remaining_percent=75.0,
                weekly_reset_at=NOW + timedelta(days=2),
                weekly_source="codex_app_server",
            ),
        )
        result = SimpleNamespace(
            range_days=7,
            status="available",
            samples=thread_samples,
            quota_samples=quota_samples,
            metrics_available=("total_tokens", "five_hour_remaining_percent"),
            start_at=thread_samples[0].source_observed_at,
            end_at=thread_samples[-1].source_observed_at,
            token_start_at=thread_samples[0].source_observed_at,
            token_end_at=thread_samples[-1].source_observed_at,
            quota_start_at=quota_samples[0].five_hour_observed_at,
            quota_end_at=quota_samples[0].five_hour_observed_at,
            five_hour_last_seen_at=NOW,
            weekly_last_seen_at=NOW - timedelta(seconds=5),
            error_code=None,
        )

        view = trend_view_from_query(result)
        total = summarize_metric(view, "total")
        quota = summarize_metric(view, "five_hour")
        reuse = summarize_metric(view, "cache_reuse")

        self.assertEqual((total.current, total.minimum, total.maximum, total.change), (160, 100, 160, 60))
        self.assertEqual(total.scope, "thread")
        self.assertEqual((quota.current, quota.scope), (42.5, "global"))
        self.assertEqual((reuse.current, reuse.derived), (25.0, True))
        self.assertEqual((total.start_at, total.end_at), (
            thread_samples[0].source_observed_at,
            thread_samples[-1].source_observed_at,
        ))
        self.assertEqual(quota.end_at, quota_samples[0].five_hour_observed_at)
        self.assertEqual((view.token_start_at, view.token_end_at), (
            result.token_start_at, result.token_end_at,
        ))
        self.assertEqual((view.quota_start_at, view.quota_end_at), (
            result.quota_start_at, result.quota_end_at,
        ))
        self.assertEqual(view.five_hour_last_seen_at, NOW)
        self.assertEqual(view.weekly_last_seen_at, NOW - timedelta(seconds=5))

    def test_history_token_metrics_never_fall_back_to_sampled_at(self):
        reliable = NOW - timedelta(minutes=5)
        sample = SimpleNamespace(
            sampled_at=NOW,
            source_observed_at=reliable,
            source_available=True,
            total_tokens=100,
        )
        unknown = SimpleNamespace(
            sampled_at=NOW,
            source_observed_at=None,
            observed_at=NOW,
            source_available=True,
            total_tokens=200,
        )
        view = TrendView(7, "insufficient", (sample, unknown), reliable)

        summary = summarize_metric(view, "total")

        self.assertEqual(summary.sample_count, 1)
        self.assertEqual((summary.minimum, summary.maximum, summary.change), (None, None, None))
        self.assertEqual((summary.start_at, summary.end_at), (reliable, reliable))
        self.assertEqual(metric_observed_at(unknown, "total"), None)

    def test_safe_snapshot_sample_uses_observed_at(self):
        sample = SafeTrendSample(NOW, 80, 20, 100, 10, 5, 200, 3, 12.5)
        view = TrendView(7, "insufficient", (sample,), NOW)

        self.assertEqual(summarize_metric(view, "total").end_at, NOW)

    def test_quota_metrics_deduplicate_each_window_independently(self):
        five_reset = NOW + timedelta(hours=4)
        week_reset = NOW + timedelta(days=4)

        def quota_sample(minutes: int, five: float, weekly: float):
            observed = NOW + timedelta(minutes=minutes)
            return SimpleNamespace(
                sampled_at=observed + timedelta(hours=2),
                five_hour_observed_at=observed,
                five_hour_last_seen_at=observed,
                five_hour_available=True,
                five_hour_used_percent=100.0 - five,
                five_hour_remaining_percent=five,
                five_hour_reset_at=five_reset,
                five_hour_source="codex_app_server",
                weekly_observed_at=observed,
                weekly_last_seen_at=observed,
                weekly_available=True,
                weekly_used_percent=100.0 - weekly,
                weekly_remaining_percent=weekly,
                weekly_reset_at=week_reset,
                weekly_source="codex_app_server",
            )

        first = quota_sample(0, 80.0, 90.0)
        weekly_only_change = quota_sample(1, 80.0, 85.0)
        five_only_change = quota_sample(2, 70.0, 85.0)
        view = TrendView(
            7, "available", (), None,
            quota_samples=(first, weekly_only_change, five_only_change),
        )

        five = metric_samples(view, "five_hour")
        weekly = metric_samples(view, "weekly")

        self.assertEqual([value for _, value in five], [80.0, 70.0])
        self.assertEqual([value for _, value in weekly], [90.0, 85.0])
        self.assertIs(five[0][0], weekly_only_change)
        self.assertIs(weekly[-1][0], five_only_change)

    def test_quota_summary_uses_window_observed_at_not_sampled_at(self):
        observed = NOW - timedelta(minutes=10)
        sample = SimpleNamespace(
            sampled_at=NOW,
            five_hour_observed_at=observed,
            five_hour_available=True,
            five_hour_used_percent=25.0,
            five_hour_remaining_percent=75.0,
            five_hour_reset_at=NOW + timedelta(hours=3),
            five_hour_source="codex_app_server",
        )
        view = TrendView(7, "insufficient", (), None, quota_samples=(sample,))

        summary = summarize_metric(view, "five_hour")

        self.assertEqual((summary.start_at, summary.end_at), (observed, observed))


if __name__ == "__main__":
    unittest.main()
