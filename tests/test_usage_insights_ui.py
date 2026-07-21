from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from app.codex_rollout import make_thread_safe_id
from app.main import Dashboard
from app.usage_insights_ui import (
    build_usage_insights_view,
    find_session_thread_id,
)
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
        self.assertEqual(collapsed.scope_label, "All sessions")

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
        self.assertIn("Completed response", english_text)
        self.assertIn("Some records have partial coverage", english_text)
        self.assertIn("高消耗会话", chinese_text)
        self.assertIn("已完成响应", chinese_text)
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

    def test_consumption_rank_uses_metadata_fallback_without_anonymous_code(self):
        view = build_usage_insights_view(
            result(item_count=1), "en",
            expanded_threads=False, expanded_responses=False,
        )
        thread_row = view.sections[0].rows[0]
        response_row = view.sections[1].rows[0]

        self.assertEqual((thread_row.kind, thread_row.rank), ("thread", 1))
        self.assertIn("Usage rank No. 1", thread_row.title)
        self.assertIn("Last completed", thread_row.title)
        self.assertIn("completed responses", thread_row.details)
        self.assertIn("Historical session", thread_row.details)
        self.assertIn("1 turns", thread_row.details)
        self.assertNotIn(thread_row.thread_safe_id, repr(thread_row))

        self.assertEqual((response_row.kind, response_row.rank), ("response", 1))
        self.assertIn("Usage rank No. 1", response_row.title)
        self.assertIn("Response completed", response_row.title)
        self.assertIn("Historical session", response_row.details)
        self.assertIn("Turns unknown", response_row.details)
        self.assertEqual(
            response_row.thread_safe_id,
            result(item_count=1).high_usage_responses[0].thread_safe_id,
        )

    def test_safe_ranking_identity_maps_only_to_an_in_memory_session(self):
        raw_id = "thread-alpha"
        safe_id = make_thread_safe_id(raw_id)
        sessions = (
            SimpleNamespace(thread_id="thread-beta"),
            SimpleNamespace(thread_id=raw_id),
        )

        self.assertEqual(find_session_thread_id(safe_id, sessions), raw_id)
        self.assertIsNone(
            find_session_thread_id(
                safe_id, (SimpleNamespace(thread_id=safe_id),),
            ),
        )
        self.assertIsNone(
            find_session_thread_id(
                make_thread_safe_id("missing"), sessions,
            ),
        )


class UsageInsightsDashboardContractTests(unittest.TestCase):
    def test_visible_page_routes_overview_renderer_to_exact_target(self):
        dashboard = Dashboard.__new__(Dashboard)
        dashboard.presentation = SimpleNamespace()
        dashboard._dirty_pages = {"overview", "session_detail"}
        dashboard._render_safe_overview = Mock()
        dashboard._render_observed_usage = Mock()
        dashboard._render_status_recent = Mock()
        dashboard._render_trends = Mock()
        dashboard._render_session_rows = True

        dashboard.current_nav_page = "session_detail"
        Dashboard._render_visible_page(dashboard)
        dashboard._render_safe_overview.assert_called_once_with(
            target="session_detail",
        )
        self.assertIn("overview", dashboard._dirty_pages)

        dashboard.current_nav_page = "overview"
        Dashboard._render_visible_page(dashboard)
        dashboard._render_safe_overview.assert_called_with(target="overview")
        self.assertEqual(dashboard._dirty_pages, set())

    def test_existing_trends_page_owns_card_and_shared_range_refresh(self):
        build_source = inspect.getsource(Dashboard._build_usage_trends_page)
        range_source = inspect.getsource(Dashboard._change_usage_window)
        schedule_source = inspect.getsource(Dashboard._schedule_trend_query)
        worker_source = inspect.getsource(Dashboard._trend_query_worker_loop)

        self.assertIn("_build_usage_insights_card(page)", build_source)
        self.assertIn("_schedule_trend_query()", range_source)
        self.assertIn("_mark_pages_dirty", schedule_source)
        self.assertIn("_render_visible_page", schedule_source)
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

    def test_ranking_location_selects_only_the_belonging_session(self):
        raw_id = "thread-alpha"
        selected_snapshot = object()
        dashboard = Dashboard.__new__(Dashboard)
        dashboard.language = "en"
        dashboard.usage_insights_sections = {
            "responses": {"rows": [{
                "thread_safe_id": make_thread_safe_id(raw_id),
                "kind": "response", "rank": 2,
            }]},
        }
        dashboard.snapshot = SimpleNamespace(
            recent_sessions=(SimpleNamespace(thread_id=raw_id),),
        )
        dashboard.view_model = Mock()
        dashboard.view_model.select_cached_thread.return_value = selected_snapshot
        dashboard._apply_cached_snapshot = Mock()
        dashboard.task_detail_viewing_var = Mock()
        dashboard.show_page = Mock()

        Dashboard._locate_usage_insight(dashboard, "responses", 0)

        dashboard.view_model.select_cached_thread.assert_called_once_with(raw_id)
        dashboard._apply_cached_snapshot.assert_called_once_with(selected_snapshot)
        dashboard.show_page.assert_called_once_with("session_detail")
        origin = dashboard.task_detail_viewing_var.set.call_args.args[0]
        self.assertIn("response usage ranking", origin)
        self.assertIn("No. 2", origin)

    def test_ranking_location_reuses_pinned_session_outside_recent_list(self):
        raw_id = "thread-pinned-outside-recent"
        pinned = SimpleNamespace(thread_id=raw_id, status="exact")
        recent = SimpleNamespace(thread_id="thread-recent", status="exact")
        snapshot = SimpleNamespace(
            current_session=recent,
            selected_session=pinned,
            recent_sessions=(recent,),
            selection_mode="pinned",
        )
        dashboard = Dashboard.__new__(Dashboard)
        dashboard.language = "en"
        dashboard.usage_insights_sections = {
            "threads": {"rows": [{
                "thread_safe_id": make_thread_safe_id(raw_id),
                "kind": "thread", "rank": 3,
            }]},
        }
        dashboard.snapshot = snapshot
        dashboard.view_model = Mock()
        dashboard.view_model.select_cached_thread.return_value = None
        dashboard._apply_cached_snapshot = Mock()
        dashboard.task_detail_viewing_var = Mock()
        dashboard.show_page = Mock()

        Dashboard._locate_usage_insight(dashboard, "threads", 0)

        dashboard.view_model.select_cached_thread.assert_called_once_with(raw_id)
        dashboard._apply_cached_snapshot.assert_called_once_with(snapshot)
        dashboard.show_page.assert_called_once_with("session_detail")

    def test_ranking_location_failure_stays_on_page_with_expand_fallback(self):
        dashboard = Dashboard.__new__(Dashboard)
        dashboard.language = "en"
        dashboard.usage_insights_sections = {
            "threads": {"rows": [{
                "thread_safe_id": make_thread_safe_id("missing"),
                "kind": "thread", "rank": 1,
            }]},
        }
        dashboard.snapshot = SimpleNamespace(
            recent_sessions=(SimpleNamespace(thread_id="loaded"),),
        )
        dashboard.view_model = Mock()
        dashboard.usage_insights_state_var = Mock()
        dashboard.usage_insights_state_label = Mock()
        dashboard.usage_insights_primary_button = Mock()
        dashboard.usage_insights_fallback_button = Mock()
        dashboard.show_page = Mock()

        Dashboard._locate_usage_insight(dashboard, "threads", 0)

        dashboard.view_model.select_cached_thread.assert_not_called()
        dashboard.show_page.assert_not_called()
        message = dashboard.usage_insights_state_var.set.call_args.args[0]
        self.assertIn("Unable to locate this session", message)
        self.assertIn("no session is switched", message)
        dashboard.usage_insights_primary_button.grid.assert_called_once_with()
        dashboard.usage_insights_fallback_button.grid.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
