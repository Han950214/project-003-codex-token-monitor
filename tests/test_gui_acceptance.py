from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.analytics_ui import metric_samples, summarize_metric, trend_view_from_query
from app.main import Dashboard
from app.paths import DATA_DIR_ENV
from app.usage_summary import UsageWindowKind
from scripts.gui_acceptance import (
    GEOMETRIES,
    PAGES,
    RANGES,
    SCALES,
    SCENARIOS,
    _apply_scenario,
    _change_observed_window,
    _geometry_for_scale,
    _isolated_data_root,
    _scroll_overview_to_usage,
    _scroll_trends_to_end,
    _show_trend_tooltip,
    build_scenario,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


class GuiAcceptanceLauncherTests(unittest.TestCase):
    def _scenario(self, name: str):
        temporary = tempfile.TemporaryDirectory(
            prefix="CodexTokenMonitor-GuiAcceptance-Test-",
        )
        self.addCleanup(temporary.cleanup)
        return build_scenario(name, Path(temporary.name), now=NOW)

    def test_required_geometry_scale_and_scenario_matrix_is_explicit(self):
        self.assertEqual(GEOMETRIES, ("980x660", "1440x900"))
        self.assertEqual(SCALES, (1.0, 1.25, 1.5))
        self.assertEqual(PAGES, ("overview", "usage_trends", "recommendations"))
        self.assertEqual(RANGES, (7, 30, 90))
        self.assertEqual(SCENARIOS, (
            "token_quota_independence",
            "quota_heartbeat",
            "quota_round_trip",
            "advisor_quota_sufficient",
            "advisor_quota_insufficient",
            "mini_dashboard_dedup",
            "observed_usage_complete",
            "observed_usage_partial",
            "observed_usage_empty",
            "observed_usage_unavailable",
        ))

    def test_geometry_is_normalized_for_customtkinter_window_scaling(self):
        self.assertEqual(_geometry_for_scale("980x660", 1.0), "980x660")
        self.assertEqual(_geometry_for_scale("980x660", 1.25), "784x528")
        self.assertEqual(_geometry_for_scale("1440x900", 1.5), "960x600")

    def test_observed_window_hook_uses_real_dashboard_change_callback(self):
        menu = Mock()
        dashboard = SimpleNamespace(
            usage_window_labels={"Today": UsageWindowKind.TODAY},
            observed_usage_window_menu=menu,
            _change_usage_window=Mock(),
        )

        _change_observed_window(dashboard, "today")

        menu.set.assert_called_once_with("Today")
        dashboard._change_usage_window.assert_called_once_with("Today")

    def test_launcher_requires_an_isolated_system_temp_directory(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, DATA_DIR_ENV):
                _isolated_data_root()

        safe = Path(tempfile.gettempdir()) / "CodexTokenMonitor-GuiAcceptance-Test"
        with patch.dict(os.environ, {DATA_DIR_ENV: str(safe)}, clear=False):
            self.assertEqual(_isolated_data_root(), safe.resolve())

        with patch.dict(os.environ, {DATA_DIR_ENV: str(Path.cwd())}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "system temp"):
                _isolated_data_root()

        with self.assertRaisesRegex(RuntimeError, "system temp"):
            build_scenario("quota_heartbeat", Path.cwd(), now=NOW)

    def test_tooltip_hook_targets_a_rendered_chart_point_without_mouse_control(self):
        tooltip = SimpleNamespace(lift=Mock(), update_idletasks=Mock())
        chart = SimpleNamespace(
            _rendered_points=[(12.4, 34.6, object()), (56.2, 78.8, object())],
            _redraw=Mock(),
            _tooltip=tooltip,
            update_idletasks=Mock(),
            event_generate=Mock(),
        )

        _show_trend_tooltip(SimpleNamespace(trend_chart=chart), 1)

        chart.update_idletasks.assert_called_once_with()
        chart._redraw.assert_called_once_with()
        chart.event_generate.assert_called_once_with("<Motion>", x=56, y=79)
        tooltip.lift.assert_called_once_with()
        tooltip.update_idletasks.assert_called_once_with()

    def test_scroll_hook_exposes_the_final_trend_summary_row(self):
        canvas = SimpleNamespace(yview_moveto=Mock())
        page = SimpleNamespace(_parent_canvas=canvas)
        chart = SimpleNamespace(master=SimpleNamespace(master=page))

        _scroll_trends_to_end(SimpleNamespace(trend_chart=chart))

        canvas.yview_moveto.assert_called_once_with(1.0)

    def test_scroll_hook_centers_observed_usage_card(self):
        canvas = SimpleNamespace(yview_moveto=Mock())

        _scroll_overview_to_usage(
            SimpleNamespace(status_page=SimpleNamespace(_parent_canvas=canvas))
        )

        canvas.yview_moveto.assert_called_once_with(0.26)

    def test_quota_only_change_does_not_add_or_refresh_token_observation(self):
        scenario = self._scenario("token_quota_independence")

        self.assertEqual(scenario.record_results, (True, True))
        self.assertEqual(scenario.before.sample_count, scenario.after.sample_count)
        self.assertEqual(scenario.after.sample_count, 1)
        self.assertEqual(scenario.before.token_end_at, scenario.after.token_end_at)
        self.assertEqual((scenario.before.status, scenario.after.status), ("stale", "stale"))
        self.assertEqual(
            len(metric_samples(trend_view_from_query(scenario.before), "total")),
            len(metric_samples(scenario.trend_view, "total")),
        )
        self.assertEqual(
            len(metric_samples(trend_view_from_query(scenario.before), "five_hour")),
            1,
        )
        self.assertEqual(len(metric_samples(scenario.trend_view, "five_hour")), 2)

    def test_same_value_quota_heartbeat_only_updates_last_seen(self):
        scenario = self._scenario("quota_heartbeat")
        before_view = trend_view_from_query(scenario.before)

        self.assertEqual(scenario.record_results, (True, False))
        self.assertEqual(scenario.before.sample_count, scenario.after.sample_count)
        self.assertEqual(scenario.before.token_end_at, scenario.after.token_end_at)
        self.assertEqual(
            len(metric_samples(before_view, "five_hour")),
            len(metric_samples(scenario.trend_view, "five_hour")),
        )
        self.assertEqual(len(metric_samples(scenario.trend_view, "five_hour")), 1)
        self.assertLess(
            scenario.before.five_hour_last_seen_at,
            scenario.after.five_hour_last_seen_at,
        )
        self.assertEqual(scenario.after.five_hour_last_seen_at, NOW)
        self.assertEqual(
            scenario.before.quota_samples[-1].five_hour_observed_at,
            scenario.after.quota_samples[-1].five_hour_observed_at,
        )
        self.assertFalse(scenario.after.five_hour_stale)
        self.assertEqual(scenario.after.status, "stale")

    def test_quota_round_trip_preserves_three_events_and_independent_weekly_heartbeat(self):
        scenario = self._scenario("quota_round_trip")
        five = metric_samples(scenario.trend_view, "five_hour")
        weekly = metric_samples(scenario.trend_view, "weekly")
        summary = summarize_metric(scenario.trend_view, "five_hour")
        expected_times = tuple(
            NOW - timedelta(minutes=2 - index) for index in range(3)
        )

        self.assertEqual(scenario.record_results, (True, True, True))
        self.assertEqual(scenario.after.sample_count, 0)
        self.assertEqual([value for _, value in five], [80.0, 70.0, 80.0])
        self.assertEqual(
            tuple(sample.five_hour_observed_at for sample, _ in five),
            expected_times,
        )
        self.assertEqual(len(set(expected_times)), 3)
        self.assertEqual(
            (
                summary.current,
                summary.minimum,
                summary.maximum,
                summary.change,
                summary.sample_count,
                summary.end_at,
                summary.scope,
            ),
            (80.0, 70.0, 80.0, 10.0, 3, NOW, "global"),
        )
        self.assertEqual([value for _, value in weekly], [60.0])
        self.assertIsNone(summarize_metric(scenario.trend_view, "weekly").change)
        self.assertEqual(scenario.after.five_hour_last_seen_at, NOW)
        self.assertEqual(scenario.after.weekly_last_seen_at, NOW)
        points = object.__new__(Dashboard)._trend_points(
            scenario.trend_view, "five_hour",
        )
        self.assertEqual(
            tuple((point.value, point.observed_at) for point in points),
            tuple(zip((80.0, 70.0, 80.0), expected_times, strict=True)),
        )

    def test_advisor_quota_sufficient_uses_five_prior_global_samples(self):
        scenario = self._scenario("advisor_quota_sufficient")
        quota_risk = next(
            item for item in scenario.advisor_result.recommendations
            if item.code == "quota_risk"
        )
        history = quota_risk.history_evidence

        self.assertIsNotNone(history)
        self.assertEqual(history.source, "global_quota_history")
        self.assertEqual(history.sample_count, 5)
        self.assertGreaterEqual(history.distinct_observation_count, 3)
        self.assertLess(history.range_ended_at, scenario.current_observed_at)
        self.assertEqual(len(scenario.before.quota_samples), 5)
        self.assertEqual(len(scenario.after.quota_samples), 6)

    def test_advisor_quota_insufficient_exposes_no_trend_conclusion(self):
        scenario = self._scenario("advisor_quota_insufficient")
        quota_risk = next(
            item for item in scenario.advisor_result.recommendations
            if item.code == "quota_risk"
        )

        self.assertIsNone(quota_risk.history_evidence)
        self.assertEqual(len(scenario.before.quota_samples), 4)
        self.assertEqual(len(scenario.after.quota_samples), 5)

    def test_mini_and_dashboard_same_observation_keep_one_complete_dashboard_point(self):
        scenario = self._scenario("mini_dashboard_dedup")

        self.assertEqual(scenario.record_results, (True, True))
        self.assertEqual((scenario.before.sample_count, scenario.after.sample_count), (1, 1))
        self.assertEqual(len(metric_samples(scenario.trend_view, "total")), 1)
        sample = scenario.after.samples[0]
        self.assertEqual(sample.source_type, "dashboard")
        self.assertEqual(
            (
                sample.input_tokens,
                sample.output_tokens,
                sample.cached_tokens,
                sample.reasoning_tokens,
            ),
            (1_200, 300, 600, 100),
        )

    def test_scenario_is_applied_to_real_dashboard_contract_with_its_range_and_page(self):
        scenario = self._scenario("token_quota_independence")
        dashboard = SimpleNamespace(
            language="en",
            trend_group_labels={"Token Trends": "tokens"},
            trend_group_menu=Mock(),
            trend_range_menu=Mock(),
            _configure_trend_metric_menu=Mock(),
            _render_observed_usage=Mock(),
            _render_trends=Mock(),
            _render_advisor=Mock(),
            _render_recommendations=Mock(),
            show_page=Mock(),
        )

        _apply_scenario(dashboard, scenario, scenario.default_page)

        self.assertEqual(dashboard.trend_range_days, 7)
        self.assertIs(dashboard.trend_view, scenario.trend_view)
        self.assertIs(dashboard.advisor_result, scenario.advisor_result)
        self.assertIs(dashboard.observed_usage_summary, scenario.usage_summary)
        self.assertEqual((dashboard.trend_group, dashboard.trend_metric), ("tokens", "total"))
        dashboard.trend_group_menu.set.assert_called_once_with("Token Trends")
        dashboard.trend_range_menu.set.assert_called_once_with("Last 7 days")
        dashboard._render_trends.assert_called_once_with()
        dashboard._render_observed_usage.assert_called_once_with()
        dashboard._render_advisor.assert_called_once_with()
        dashboard._render_recommendations.assert_called_once_with()
        dashboard.show_page.assert_called_once_with("usage_trends")

    def test_round_trip_scenario_applies_global_quota_metric(self):
        scenario = self._scenario("quota_round_trip")
        dashboard = SimpleNamespace(
            language="en",
            trend_group_labels={"Quota (Global)": "quota"},
            trend_group_menu=Mock(),
            trend_range_menu=Mock(),
            _configure_trend_metric_menu=Mock(),
            _render_observed_usage=Mock(),
            _render_trends=Mock(),
            _render_advisor=Mock(),
            _render_recommendations=Mock(),
            show_page=Mock(),
        )

        _apply_scenario(dashboard, scenario, scenario.default_page)

        self.assertEqual((dashboard.trend_group, dashboard.trend_metric), (
            "quota", "five_hour",
        ))
        dashboard.trend_group_menu.set.assert_called_once_with("Quota (Global)")
        dashboard.show_page.assert_called_once_with("usage_trends")

    def test_scenarios_store_only_production_safe_numeric_fields(self):
        forbidden = {
            "prompt", "response", "preview", "message", "tool_output",
            "reasoning_text", "thread_title", "local_path",
        }
        for name in SCENARIOS:
            with self.subTest(scenario=name):
                scenario = self._scenario(name)
                samples = (*scenario.after.samples, *scenario.after.quota_samples)
                for sample in samples:
                    self.assertTrue(forbidden.isdisjoint(vars(sample)))
                self.assertTrue(forbidden.isdisjoint(vars(scenario.usage_summary)))
                self.assertIn(Path(tempfile.gettempdir()).resolve(), scenario.store.path.parents)

    def test_observed_usage_gui_states_cover_complete_partial_empty_and_unavailable(self):
        expected = {
            "observed_usage_complete": "complete_for_local_history",
            "observed_usage_partial": "partial",
            "observed_usage_empty": "no_observations",
            "observed_usage_unavailable": "unavailable",
        }
        for name, state in expected.items():
            with self.subTest(name=name):
                scenario = self._scenario(name)
                self.assertEqual(scenario.usage_summary.coverage_state, state)
                self.assertEqual(scenario.default_page, "overview")


if __name__ == "__main__":
    unittest.main()
