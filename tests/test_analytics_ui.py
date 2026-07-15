from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.analytics_ui import (
    TREND_QUALITY_STATES,
    TREND_STALE_AFTER,
    build_trend_view,
    classify_trend_quality,
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
    def test_all_four_quality_states_are_explicit(self):
        self.assertEqual(
            TREND_QUALITY_STATES,
            ("available", "insufficient", "unavailable", "stale"),
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


if __name__ == "__main__":
    unittest.main()
