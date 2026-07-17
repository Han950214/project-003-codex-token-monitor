from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from app.main import Dashboard
from app.usage_insights_ui import build_usage_insights_view
from app.usage_summary import (
    CoverageState,
    HighUsageResponse,
    HighUsageThread,
    LowCacheReuseThread,
    UsageInsightsResult,
    UsageWindowKind,
)


NOW = datetime(2026, 7, 17, 13, 30, tzinfo=timezone.utc)


def result(
    *,
    coverage: CoverageState = CoverageState.COMPLETE_FOR_LOCAL_HISTORY,
    source_available: bool = True,
    item_count: int = 5,
) -> UsageInsightsResult:
    threads = tuple(
        HighUsageThread(
            thread_safe_id=f"sha256:{index:064x}",
            safe_thread_label=f"{index:06X}",
            total_tokens=(10 - index) * 1_000,
            input_tokens=(10 - index) * 700,
            output_tokens=(10 - index) * 300,
            cached_tokens=(10 - index) * 350,
            reasoning_tokens=(10 - index) * 100,
            cache_reuse=0.5,
            completed_response_count=index + 1,
            first_observed_at=NOW - timedelta(hours=1),
            last_observed_at=NOW - timedelta(minutes=index),
            coverage_status=(
                CoverageState.PARTIAL.value if index == 1
                else CoverageState.COMPLETE_FOR_LOCAL_HISTORY.value
            ),
        )
        for index in range(item_count)
    )
    responses = tuple(
        HighUsageResponse(
            response_safe_id=f"sha256:{(100 + index):064x}",
            thread_safe_id=threads[index].thread_safe_id,
            safe_thread_label=threads[index].safe_thread_label,
            total_tokens=(10 - index) * 900,
            input_tokens=(10 - index) * 600,
            output_tokens=(10 - index) * 300,
            cached_tokens=(10 - index) * 200,
            reasoning_tokens=None if index == 1 else (10 - index) * 50,
            cache_reuse=1 / 3,
            observed_at=NOW - timedelta(minutes=index),
            coverage_status=(
                CoverageState.PARTIAL.value if index == 1
                else CoverageState.COMPLETE_FOR_LOCAL_HISTORY.value
            ),
        )
        for index in range(item_count)
    )
    low_cache = tuple(
        LowCacheReuseThread(
            thread_safe_id=threads[index].thread_safe_id,
            safe_thread_label=threads[index].safe_thread_label,
            cache_reuse=index / 100,
            valid_input_tokens=1_000 + index,
            valid_cached_tokens=index * 10,
            valid_response_count=2,
            first_observed_at=NOW - timedelta(hours=1),
            last_observed_at=NOW - timedelta(minutes=index),
            coverage_status=CoverageState.COMPLETE_FOR_LOCAL_HISTORY.value,
        )
        for index in range(min(3, item_count))
    )
    return UsageInsightsResult(
        range_id=UsageWindowKind.ROLLING_5H,
        range_start=NOW - timedelta(hours=5),
        range_end=NOW,
        generated_at=NOW,
        source_available=source_available,
        coverage_status=coverage,
        coverage_messages=(),
        high_usage_threads=threads,
        high_usage_responses=responses,
        low_cache_reuse_threads=low_cache,
    )


class UsageInsightsPresentationTests(unittest.TestCase):
    def test_default_three_expand_five_and_low_cache_stays_three(self):
        collapsed = build_usage_insights_view(
            result(), "en", expanded_threads=False, expanded_responses=False,
        )
        expanded = build_usage_insights_view(
            result(), "en", expanded_threads=True, expanded_responses=True,
        )

        self.assertEqual([len(section.rows) for section in collapsed.sections], [3, 3, 3])
        self.assertEqual([len(section.rows) for section in expanded.sections], [5, 5, 3])
        self.assertTrue(collapsed.sections[0].can_expand)
        self.assertTrue(collapsed.sections[1].can_expand)
        self.assertFalse(collapsed.sections[2].can_expand)
        self.assertEqual(collapsed.range_label, "Last 5 hours")

    def test_rendered_text_is_bilingual_and_contains_no_full_safe_id(self):
        item = result(coverage=CoverageState.PARTIAL)
        english = build_usage_insights_view(
            item, "en", expanded_threads=False, expanded_responses=False,
        )
        chinese = build_usage_insights_view(
            item, "zh-CN", expanded_threads=False, expanded_responses=False,
        )
        english_text = repr(english)
        chinese_text = repr(chinese)

        self.assertIn("Highest usage sessions", english_text)
        self.assertIn("Instruction", english_text)
        self.assertIn("Some records have partial coverage", english_text)
        self.assertIn("高消耗会话", chinese_text)
        self.assertIn("指令", chinese_text)
        self.assertIn("部分记录覆盖有限", chinese_text)
        for thread in item.high_usage_threads:
            self.assertNotIn(thread.thread_safe_id, english_text)
            self.assertNotIn(thread.thread_safe_id, chinese_text)
        for response in item.high_usage_responses:
            self.assertNotIn(response.response_safe_id, english_text)
            self.assertNotIn(response.response_safe_id, chinese_text)

    def test_empty_in_progress_only_and_unavailable_clear_all_rows(self):
        empty = result(coverage=CoverageState.NO_OBSERVATIONS, item_count=0)
        pending_only = result(coverage=CoverageState.PARTIAL, item_count=0)
        unavailable = result(
            coverage=CoverageState.UNAVAILABLE,
            source_available=False,
            item_count=0,
        )

        empty_view = build_usage_insights_view(
            empty, "en", expanded_threads=True, expanded_responses=True,
        )
        pending_view = build_usage_insights_view(
            pending_only, "zh-CN", expanded_threads=True, expanded_responses=True,
        )
        unavailable_view = build_usage_insights_view(
            unavailable, "en", expanded_threads=True, expanded_responses=True,
        )

        self.assertEqual(empty_view.state_kind, "empty")
        self.assertEqual(
            empty_view.state_text, "No completed usage records in this range",
        )
        self.assertEqual(pending_view.state_kind, "empty")
        self.assertEqual(
            pending_view.state_text, "当前范围内还没有已完成的用量记录",
        )
        self.assertEqual(unavailable_view.state_kind, "unavailable")
        self.assertEqual(unavailable_view.state_text, "Usage insights are unavailable")
        for view in (empty_view, pending_view, unavailable_view):
            self.assertTrue(all(not section.rows for section in view.sections))


class UsageInsightsDashboardContractTests(unittest.TestCase):
    def test_existing_trends_page_owns_card_and_shared_range_refresh(self):
        build_source = inspect.getsource(Dashboard._build_usage_trends_page)
        range_source = inspect.getsource(Dashboard._change_usage_window)
        worker_source = inspect.getsource(Dashboard._trend_query_worker_loop)

        self.assertIn("_build_usage_insights_card(page)", build_source)
        self.assertIn("_render_usage_insights()", range_source)
        self.assertIn("_query_observed_usage", worker_source)
        self.assertNotIn("StringVar", worker_source)
        self.assertNotIn(".configure(", worker_source)
        card_source = inspect.getsource(Dashboard._build_usage_insights_card)
        self.assertNotIn("COLORS.hover", card_source)

    def test_expand_state_is_memory_only_and_rerenders(self):
        dashboard = Dashboard.__new__(Dashboard)
        dashboard.usage_insights_expanded = {
            "threads": False,
            "responses": False,
        }
        dashboard._render_usage_insights = Mock()

        Dashboard._toggle_usage_insights_group(dashboard, "threads")
        self.assertTrue(dashboard.usage_insights_expanded["threads"])
        dashboard._render_usage_insights.assert_called_once_with()

        Dashboard._toggle_usage_insights_group(dashboard, "cache")
        self.assertNotIn("cache", dashboard.usage_insights_expanded)
        self.assertEqual(dashboard._render_usage_insights.call_count, 1)


if __name__ == "__main__":
    unittest.main()
