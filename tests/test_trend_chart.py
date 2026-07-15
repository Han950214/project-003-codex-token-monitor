from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta, timezone

from app.trend_chart import (
    TrendCanvas,
    TrendPoint,
    downsample_peak_valley,
    nearest_trend_point,
)


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)


def point(minute: int, value: int | None, *, metric: str = "total_tokens") -> TrendPoint:
    return TrendPoint(NOW + timedelta(minutes=minute), metric, value, "token_monitor_history")


class TrendPointTests(unittest.TestCase):
    def test_dto_accepts_only_safe_numeric_metadata(self):
        self.assertEqual(point(0, 12).value, 12)
        with self.assertRaises(ValueError):
            TrendPoint(NOW.replace(tzinfo=None), "total_tokens", 1, "history")
        with self.assertRaises(ValueError):
            TrendPoint(NOW, "total_tokens", float("nan"), "history")
        with self.assertRaises(ValueError):
            TrendPoint(NOW, "total_tokens", 1, "unsafe\ncontent")


class DownsamplingTests(unittest.TestCase):
    def test_empty_single_two_and_below_limit(self):
        self.assertEqual(downsample_peak_valley((), 10), ())
        single = (point(0, 1),)
        pair = (point(0, 1), point(1, 2))
        self.assertEqual(downsample_peak_valley(single, 10), single)
        self.assertEqual(downsample_peak_valley(pair, 10), pair)

    def test_first_last_and_peak_valley_are_retained_with_a_bound(self):
        values = [10, 11, 12, 1000, 13, 12, 1, 11, 10, 9, 8, 7]
        points = tuple(point(index, value) for index, value in enumerate(values))
        sampled = downsample_peak_valley(points, 8)
        self.assertLessEqual(len(sampled), 8)
        self.assertIs(sampled[0], points[0])
        self.assertIs(sampled[-1], points[-1])
        self.assertIn(1000, [item.value for item in sampled])
        self.assertIn(1, [item.value for item in sampled])

    def test_missing_values_are_not_zero_filled(self):
        points = (point(0, None), point(1, 8), point(2, None), point(3, 9))
        sampled = downsample_peak_valley(points, 10)
        self.assertEqual([item.value for item in sampled], [8, 9])

    def test_duplicate_timestamps_keep_stable_input_order(self):
        first = point(0, 10)
        duplicate_a = TrendPoint(first.observed_at, "total_tokens", 20, "token_monitor_history")
        duplicate_b = TrendPoint(first.observed_at, "total_tokens", 30, "token_monitor_history")
        last = point(1, 40)
        sampled = downsample_peak_valley((first, duplicate_a, duplicate_b, last), 10)
        self.assertEqual(sampled, (first, duplicate_a, duplicate_b, last))

    def test_nearest_selection_is_stable_and_ignores_missing(self):
        missing = point(1, None)
        earlier = point(0, 10)
        later = point(2, 20)
        selected = nearest_trend_point((missing, earlier, later), NOW + timedelta(minutes=1))
        self.assertIs(selected, earlier)
        self.assertIsNone(nearest_trend_point((missing,), NOW))


class TrendCanvasContractTests(unittest.TestCase):
    def test_canvas_binds_resize_hover_leave_and_destroy(self):
        source = inspect.getsource(TrendCanvas.__init__)
        for event in (
            "<Configure>", "<Motion>", "<Button-1>", "<Leave>", "<Destroy>",
        ):
            self.assertIn(event, source)

    def test_data_or_label_changes_hide_an_open_tooltip(self):
        self.assertIn("self._hide_tooltip()", inspect.getsource(TrendCanvas.set_points))
        self.assertIn("self._hide_tooltip()", inspect.getsource(TrendCanvas.set_labels))

    def test_tooltip_localizes_source_and_marks_derived_values(self):
        source = inspect.getsource(TrendCanvas._show_tooltip)
        self.assertIn("self._source_labels.get", source)
        self.assertIn("labels.derived_yes", source)

    def test_dispose_cancels_redraw_and_destroys_tooltip(self):
        class Tooltip:
            def __init__(self):
                self.destroyed = False

            def destroy(self):
                self.destroyed = True

        class FakeCanvas:
            _disposed = False
            _redraw_after_id = "after-1"
            _tooltip = Tooltip()
            _tooltip_label = object()
            _rendered_points = [(1, 2, point(0, 3))]

            def __init__(self):
                self.cancelled = []

            def after_cancel(self, callback_id):
                self.cancelled.append(callback_id)

        canvas = FakeCanvas()
        tooltip = canvas._tooltip
        TrendCanvas._dispose(canvas)
        self.assertEqual(canvas.cancelled, ["after-1"])
        self.assertTrue(tooltip.destroyed)
        self.assertIsNone(canvas._redraw_after_id)
        self.assertEqual(canvas._rendered_points, [])


if __name__ == "__main__":
    unittest.main()
