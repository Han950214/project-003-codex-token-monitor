import inspect
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.desktop_widget as desktop_widget_module
import app.main as main_module
import app.ui_icons as ui_icons_module
from app.advisor import (
    ADVISOR_RULE_CODES,
    DATA_STALE_AFTER,
    NEW_THREAD_TURN_COUNT,
    OPTIMIZE_CACHE_HIT_PERCENT,
    OPTIMIZE_INPUT_TOKENS,
    QUOTA_RISK_REMAINING_PERCENT,
    AdvisorInput,
    Recommendation,
    build_advisor_input,
    evaluate_advice,
)
from app.analytics_ui import TrendView
from app.dashboard_mode import ALL_PAGES, AppShellState, NAVIGATION_ITEMS
from app.desktop_widget import DesktopMiniWidget, HOVER_ALPHA, format_percent
from app.codex_rollout import CodexSessionUsage, InstructionUsage, TokenUsage
from app.diagnostics import (
    DIAGNOSTIC_CHECK_CODES,
    DiagnosticContext,
    inspect_settings_file,
    run_diagnostics,
)
from app.i18n import TRANSLATIONS, translate
from app.main import CORE_METRICS, Dashboard
from app.new_thread import generic_handoff_template
from app.quota import CodexQuotaSnapshot, QuotaKind, QuotaWindow
from app.ui_presenter import _latest_metrics
from app.ui_format import (
    dashboard_layout_for_width, ellipsize_title, format_compact_token_count,
    format_full_token_count, metric_columns_for_width,
)
from app.ui_icons import CircularProgress, Sparkline, create_icon
from app.ui_settings import (
    load_auto_refresh_enabled,
    load_exit_behavior,
    load_language,
    load_startup_mode,
    load_widget_idle_opacity,
    load_widget_mode,
    save_auto_refresh_enabled,
    save_exit_behavior,
    save_language,
    save_startup_mode,
    save_widget_idle_opacity,
    save_widget_mode,
    validate_ui_settings,
)


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


class FakeVar:
    def __init__(self, *args, value="", **kwargs):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self.options = dict(kwargs)
        self.visible = None
        self.grid_calls = []
        self.configure_calls = []
        self.geometry_calls = []
        self.x = 10
        self.y = 20

    def grid(self, *args, **kwargs):
        self.visible = True
        self.grid_calls.append((args, kwargs))

    def grid_remove(self):
        self.visible = False

    def grid_forget(self):
        self.visible = False

    def grid_configure(self, *args, **kwargs):
        self.grid_calls.append((args, kwargs))

    def grid_columnconfigure(self, *args, **kwargs):
        self.configure_calls.append(("column", args, kwargs))

    def grid_rowconfigure(self, *args, **kwargs):
        self.configure_calls.append(("row", args, kwargs))

    def grid_propagate(self, value):
        self.configure_calls.append(("propagate", (value,), {}))

    def configure(self, **kwargs):
        self.options.update(kwargs)
        self.configure_calls.append(("configure", (), kwargs))

    def set(self, value):
        self.options["value"] = value

    def bind(self, *args, **kwargs):
        return None

    def winfo_x(self):
        return self.x

    def winfo_y(self):
        return self.y

    def geometry(self, value):
        self.geometry_calls.append(value)


class FakeCanvas:
    def __init__(self):
        self.size = 58
        self.track = "#DDE4EE"
        self.color = "#248A52"
        self.chart_width = 112
        self.chart_height = 28
        self.deleted = []
        self.arcs = []
        self.texts = []
        self.lines = []

    def delete(self, value):
        self.deleted.append(value)

    def create_arc(self, *args, **kwargs):
        self.arcs.append((args, kwargs))

    def create_text(self, *args, **kwargs):
        self.texts.append((args, kwargs))

    def create_line(self, *args, **kwargs):
        self.lines.append((args, kwargs))


class FakeRoot(FakeWidget):
    def __init__(self, width, window_scaling=1.0):
        super().__init__()
        self.width = width
        self.window_scaling = window_scaling

    def winfo_width(self):
        return self.width

    def _reverse_window_scaling(self, value):
        return int(value / self.window_scaling)


class QueryBomb:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected product-data query: {name}")


def advisor_input(**changes):
    base = AdvisorInput(
        data_available=True,
        data_age_seconds=10,
        source_status="normal",
        five_hour_remaining_percent=75.0,
        weekly_remaining_percent=80.0,
        turn_count=4,
        instruction_input_tokens=20_000,
        instruction_total_tokens=22_000,
        cached_input_tokens=10_000,
        session_total_tokens=100_000,
        session_status="in_progress",
        observed_at=NOW,
    )
    return replace(base, **changes)


class Phase3ModeTests(unittest.TestCase):
    def test_widget_mode_defaults_to_compact(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_widget_mode(Path(directory) / "missing.json"), "compact")

    def test_widget_mode_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertTrue(save_widget_mode("expanded", path))
            self.assertEqual(load_widget_mode(path), "expanded")

    def test_corrupt_and_unknown_widget_mode_uses_safe_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("broken", encoding="utf-8")
            self.assertEqual(load_widget_mode(path), "compact")
            path.write_text('{"widget_mode":"y"}', encoding="utf-8")
            self.assertEqual(load_widget_mode(path), "compact")

    def test_legacy_dashboard_mode_is_ignored_without_losing_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({
                "dashboard_mode": "retired-value",
                "language": "en",
                "widget_mode": "expanded",
            }), encoding="utf-8")
            self.assertEqual(validate_ui_settings(path), (True, "valid"))
            self.assertEqual(load_language(path), "en")
            self.assertEqual(load_widget_mode(path), "expanded")
            self.assertTrue(save_auto_refresh_enabled(True, path))
            self.assertTrue(load_auto_refresh_enabled(path))
            self.assertEqual(load_language(path), "en")
            self.assertEqual(load_widget_mode(path), "expanded")

    def test_navigation_preserves_selection_pagination_and_auto_refresh(self):
        state = AppShellState(
            page="sessions", selected_thread_id="thread-1", history_page=3,
            auto_refresh_enabled=True,
        )
        changed = state.navigate("session_detail")
        self.assertEqual(changed.selected_thread_id, "thread-1")
        self.assertEqual(changed.history_page, 3)
        self.assertTrue(changed.auto_refresh_enabled)
        self.assertEqual(changed.page, "session_detail")
        self.assertEqual(changed.navigate("overview").page, "overview")

    def test_invalid_navigation_returns_to_status_center(self):
        self.assertEqual(AppShellState(page="sessions").navigate("missing").page, "overview")

    def test_navigation_has_exactly_six_product_entries(self):
        self.assertEqual(NAVIGATION_ITEMS, (
            "overview", "sessions", "usage_trends", "recommendations",
            "tools", "settings",
        ))
        self.assertNotIn("session_detail", NAVIGATION_ITEMS)
        self.assertIn("session_detail", ALL_PAGES)

    def test_navigation_and_secondary_current_task_are_query_free(self):
        dashboard = object.__new__(Dashboard)
        dashboard.shell_state = AppShellState(
            selected_thread_id="thread-1", history_page=2,
            auto_refresh_enabled=True,
        )
        dashboard.current_nav_page = "overview"
        dashboard.page_frames = {page: FakeWidget() for page in ALL_PAGES}
        dashboard.nav_buttons = {page: FakeWidget() for page in NAVIGATION_ITEMS}
        dashboard.view_model = QueryBomb()
        dashboard.quota_provider = QueryBomb()

        Dashboard.show_page(dashboard, "session_detail")

        self.assertEqual(dashboard.shell_state.page, "session_detail")
        self.assertEqual(dashboard.shell_state.selected_thread_id, "thread-1")
        self.assertEqual(dashboard.shell_state.history_page, 2)
        self.assertTrue(dashboard.page_frames["session_detail"].visible)
        self.assertFalse(dashboard.page_frames["overview"].visible)
        self.assertEqual(
            dashboard.nav_buttons["sessions"].options["fg_color"],
            main_module.COLORS.accent,
        )

        Dashboard.show_page(dashboard, "overview")
        self.assertEqual(dashboard.shell_state.page, "overview")
        self.assertTrue(dashboard.page_frames["overview"].visible)

    def test_dashboard_mode_controls_are_not_built(self):
        source = inspect.getsource(Dashboard._build_header) + inspect.getsource(Dashboard._build_settings_page)
        self.assertNotIn("CTkSegmentedButton", source)
        self.assertNotIn("settings_dashboard_menu", source)

    def test_auto_refresh_and_exit_behavior_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_auto_refresh_enabled(True, path)
            save_exit_behavior("minimize", path)
            self.assertTrue(load_auto_refresh_enabled(path))
            self.assertEqual(load_exit_behavior(path), "minimize")


class Phase3FormatAndResponsiveTests(unittest.TestCase):
    def test_compact_token_examples_cover_k_m_and_b(self):
        cases = {
            999: "999",
            12_100: "12.1K",
            106_800: "106.8K",
            1_380_000: "1.38M",
            9_170_000: "9.17M",
            24_640_000: "24.64M",
            1_200_000_000: "1.20B",
        }
        self.assertEqual(
            {value: format_compact_token_count(value) for value in cases},
            cases,
        )

    def test_compact_token_boundaries_promote_units_stably(self):
        cases = (
            (999, "999"),
            (1_000, "1.0K"),
            (999_949, "999.9K"),
            (999_950, "1.00M"),
            (1_000_000, "1.00M"),
            (999_994_999, "999.99M"),
            (999_995_000, "1.00B"),
            (1_000_000_000, "1.00B"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(format_compact_token_count(value), expected)

    def test_compact_token_none_negative_and_nonmutation(self):
        record = {"total_tokens": -1_380_000, "status": "exact"}
        original = record.copy()
        self.assertEqual(format_compact_token_count(None), "—")
        self.assertEqual(format_compact_token_count(-999), "-999")
        self.assertEqual(format_compact_token_count(record["total_tokens"]), "-1.38M")
        self.assertEqual(record, original)

    def test_full_value_and_long_title_remain_available(self):
        title = "A" * 80
        compact_title = ellipsize_title(title, 20)
        self.assertEqual(format_full_token_count(1_380_000), "1,380,000")
        self.assertEqual(len(compact_title), 20)
        self.assertTrue(compact_title.endswith("…"))
        self.assertEqual(title, "A" * 80)

    def test_metric_column_helper_boundaries(self):
        cases = {
            1100: 6,
            1099: 3,
            900: 3,
            899: 2,
            380: 2,
            379: 1,
        }
        self.assertEqual(
            {width: metric_columns_for_width(width) for width in cases},
            cases,
        )

    def test_core_metric_layout_uses_wide_medium_and_narrow_columns(self):
        cases = {
            1100: ((0, 0), (0, 1), (0, 2), (0, 3)),
            1000: ((0, 0), (0, 1), (0, 2), (0, 3)),
            800: ((0, 0), (0, 1), (0, 2), (0, 3)),
            719: ((0, 0), (0, 1), (1, 0), (1, 1)),
            320: ((0, 0), (1, 0), (2, 0), (3, 0)),
        }
        for width, expected in cases.items():
            with self.subTest(width=width):
                dashboard = object.__new__(Dashboard)
                dashboard.core_cards_frame = FakeWidget()
                cards = [FakeWidget() for _ in CORE_METRICS]
                dashboard.core_metric_widgets = [
                    {"card": card} for card in cards
                ]

                Dashboard._layout_core_metrics(dashboard, width)

                actual = tuple(
                    (
                        card.grid_calls[-1][1]["row"],
                        card.grid_calls[-1][1]["column"],
                    )
                    for card in cards
                )
                self.assertEqual(actual, expected)

    def test_wide_status_layout_uses_balanced_columns(self):
        dashboard = object.__new__(Dashboard)
        dashboard.status_page = FakeWidget()
        cards = [FakeWidget() for _ in range(8)]
        (
            dashboard.status_advice_card,
            dashboard.core_metrics_panel,
            dashboard.observed_usage_card,
            dashboard.task_summary_card,
            dashboard.quota_center_card,
            dashboard.trend_preview_card,
            dashboard.status_recent_card,
            dashboard.quick_actions_card,
        ) = cards
        core_widths = []
        usage_widths = []
        dashboard._layout_core_metrics = core_widths.append
        dashboard._layout_observed_usage = usage_widths.append
        dashboard.snapshot = SimpleNamespace(
            selection_mode="pinned", selected_session=object(),
        )

        Dashboard._apply_status_layout(dashboard, 1_400)

        self.assertEqual(
            tuple(card.grid_calls[-1][1]["column"] for card in cards),
            (0, 0, 0, 1, 0, 0, 1, 0),
        )
        self.assertEqual(
            tuple(card.grid_calls[-1][1].get("columnspan", 1) for card in cards),
            (2, 2, 2, 1, 1, 1, 1, 2),
        )
        self.assertTrue(all(
            ("propagate", (True,), {}) in card.configure_calls
            for card in cards
        ))
        self.assertEqual(core_widths, [1_400])
        self.assertEqual(usage_widths, [1_400])

        Dashboard._apply_status_layout(dashboard, 1_200)
        self.assertTrue(all(
            card.configure_calls[-1] == ("propagate", (True,), {})
            for card in cards
        ))

    def test_dashboard_layout_helper_boundaries(self):
        cases = {
            1_400: "wide",
            1_199: "medium",
            900: "medium",
            899: "narrow",
        }
        self.assertEqual(
            {width: dashboard_layout_for_width(width) for width in cases},
            cases,
        )

    def test_history_filters_reflow_to_two_rows_below_wide_width(self):
        for width, expected_rows in (
            (1200, (0, 0, 0, 0, 0, 0, 0, 0, 0)),
            (900, (0, 0, 0, 0, 1, 1, 1, 1, 0)),
        ):
            with self.subTest(width=width):
                dashboard = object.__new__(Dashboard)
                dashboard.history_selector = FakeWidget()
                controls = [FakeWidget() for _ in range(9)]
                (
                    dashboard.task_selector_label,
                    dashboard.task_menu,
                    dashboard.session_search_label,
                    dashboard.session_search_entry,
                    dashboard.range_selector_label,
                    dashboard.range_menu,
                    dashboard.status_filter_label,
                    dashboard.status_filter_menu,
                    dashboard.history_detail_button,
                ) = controls

                Dashboard._layout_history_controls(dashboard, width)

                actual_rows = tuple(
                    control.grid_calls[-1][1]["row"] for control in controls
                )
                self.assertEqual(actual_rows, expected_rows)
                self.assertTrue(all(control.visible for control in controls))

    def test_history_status_filter_resets_pagination_and_rerenders_cached_rows(self):
        dashboard = object.__new__(Dashboard)
        dashboard.status_filter_labels = {"Running": "running"}
        dashboard.status_filter = "all"
        dashboard.current_page = 4
        dashboard.presentation = object()
        rendered = []
        dashboard._render_sessions = rendered.append

        Dashboard._change_status_filter(dashboard, "Running")

        self.assertEqual(dashboard.status_filter, "running")
        self.assertEqual(dashboard.current_page, 1)
        self.assertEqual(rendered, [dashboard.presentation])

    def test_resize_changes_layout_without_querying_product_data(self):
        cases = (
            (980, 1.0, True, 64),
            (980, 1.25, True, 64),
            (980, 1.5, True, 64),
            (1_180, 1.25, False, 184),
            (1_440, 1.0, False, 184),
            (1_440, 1.25, False, 184),
            (1_440, 1.5, False, 184),
        )
        for logical_width, scaling, collapsed, sidebar_width in cases:
            with self.subTest(logical_width=logical_width, scaling=scaling):
                dashboard = object.__new__(Dashboard)
                dashboard.root = FakeRoot(
                    int(logical_width * scaling), window_scaling=scaling,
                )
                dashboard._layout_job = "pending"
                dashboard._sidebar_collapsed = False
                dashboard.sidebar = FakeWidget()
                dashboard.brand_name = FakeWidget()
                dashboard.brand_icon = FakeWidget()
                dashboard.status_reason_label = FakeWidget()
                dashboard.auto_switch = FakeWidget()
                dashboard.mini_widget_button = FakeWidget()
                dashboard.header_settings_button = FakeWidget()
                dashboard.language_menu = FakeWidget()
                dashboard.header_message_label = FakeWidget()
                dashboard.view_model = QueryBomb()
                dashboard.quota_provider = QueryBomb()
                sidebar_label_calls = []
                layout_widths = []
                dashboard._apply_sidebar_labels = lambda: sidebar_label_calls.append(True)
                dashboard._apply_status_layout = layout_widths.append

                Dashboard._apply_responsive_layout(dashboard)

                self.assertIsNone(dashboard._layout_job)
                self.assertEqual(dashboard._sidebar_collapsed, collapsed)
                self.assertEqual(sidebar_label_calls, [True] if collapsed else [])
                self.assertEqual(
                    layout_widths,
                    [max(
                        320,
                        logical_width - sidebar_width - (main_module.SPACE_4 * 2),
                    )],
                )

    def test_selected_card_reflow_uses_scaled_width_after_sidebar(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = FakeRoot(1_375, window_scaling=1.25)
        dashboard._sidebar_collapsed = False

        logical_width = Dashboard._logical_window_width(dashboard)
        content_width = Dashboard._dashboard_content_width(
            dashboard, logical_width,
        )

        self.assertEqual(logical_width, 1_100)
        self.assertEqual(
            content_width,
            1_100 - 184 - (main_module.SPACE_4 * 2),
        )
        render_source = inspect.getsource(Dashboard._render_safe_overview)
        self.assertIn("self._dashboard_content_width(window_width)", render_source)

    def test_recent_task_selection_pins_cached_snapshot_without_leaving_status_center(self):
        snapshot = object()

        class CachedOnlyViewModel:
            def __init__(self):
                self.selected = []

            def select_cached_thread(self, thread_id):
                self.selected.append(thread_id)
                return snapshot

            def __getattr__(self, name):
                raise AssertionError(f"unexpected query path: {name}")

        dashboard = object.__new__(Dashboard)
        dashboard.status_recent_rows = [{"thread_id": "thread-cached"}]
        dashboard.presentation = None
        dashboard.view_model = CachedOnlyViewModel()
        applied = []
        pages = []
        dashboard._apply_cached_snapshot = applied.append
        dashboard.show_page = pages.append

        Dashboard._select_status_recent(dashboard, 0)

        self.assertEqual(dashboard.view_model.selected, ["thread-cached"])
        self.assertEqual(applied, [snapshot])
        self.assertEqual(pages, [])

    def test_recent_task_selection_resets_filter_and_positions_its_history_page(self):
        rows = tuple(
            SimpleNamespace(thread_id=f"thread-{index}")
            for index in range(23)
        )
        dashboard = object.__new__(Dashboard)
        dashboard.status_recent_rows = [{"thread_id": "thread-17"}]
        dashboard.presentation = SimpleNamespace(recent_sessions=rows)
        dashboard.status_filter = "attention"
        dashboard.status_filter_menu = FakeWidget()
        dashboard.language = "en"
        dashboard.page_size = 10
        dashboard.current_page = 1
        dashboard.view_model = SimpleNamespace(
            select_cached_thread=lambda _thread_id: object(),
        )
        dashboard._apply_cached_snapshot = lambda _snapshot: None
        pages = []
        dashboard.show_page = pages.append

        Dashboard._select_status_recent(dashboard, 0)

        self.assertEqual(dashboard.status_filter, "all")
        self.assertEqual(dashboard.status_filter_menu.options["value"], translate("filter_all", "en"))
        self.assertEqual(dashboard.current_page, 2)
        self.assertEqual(pages, [])


class Phase3LocalVisualTests(unittest.TestCase):
    def test_all_product_icons_are_locally_drawn_with_nonempty_pixels(self):
        captured = []

        def fake_ctk_image(**kwargs):
            captured.append(kwargs)
            return kwargs

        kinds = (
            "shield", "home", "history", "trend", "recommendation", "tools", "settings",
            "pulse", "open", "refresh", "widget", "more",
        )
        with patch.object(ui_icons_module.ctk, "CTkImage", fake_ctk_image):
            for kind in kinds:
                create_icon(kind, size=24, color="#3978F6")

        self.assertEqual(len(captured), len(kinds))
        for icon in captured:
            self.assertEqual(icon["size"], (24, 24))
            self.assertIsNotNone(icon["light_image"].getchannel("A").getbbox())
            alpha_histogram = icon["light_image"].getchannel("A").histogram()
            self.assertTrue(any(alpha_histogram[1:255]))
            self.assertIs(icon["light_image"], icon["dark_image"])

    def test_sparkline_requires_two_real_samples_and_does_not_invent_points(self):
        canvas = FakeCanvas()
        self.assertFalse(Sparkline.set_samples(canvas, (None, 12)))
        self.assertEqual(canvas.lines, [])

        self.assertTrue(Sparkline.set_samples(canvas, (10, None, 30)))
        self.assertEqual(len(canvas.lines), 1)
        points = canvas.lines[0][0]
        self.assertEqual(len(points), 4)
        self.assertEqual(points[0], 3)
        self.assertEqual(points[2], 109)

        canvas.lines.clear()
        self.assertTrue(Sparkline.set_samples(canvas, range(12)))
        self.assertEqual(len(canvas.lines[0][0]), 16)

    def test_circular_progress_uses_real_percentage_and_unknown_dash(self):
        canvas = FakeCanvas()
        CircularProgress.set(canvas, 63)
        self.assertEqual(len(canvas.arcs), 2)
        self.assertAlmostEqual(canvas.arcs[1][1]["extent"], -(63 * 3.6))
        self.assertEqual(canvas.texts[-1][1]["text"], "63%")

        canvas.arcs.clear()
        canvas.texts.clear()
        CircularProgress.set(canvas, None)
        self.assertEqual(len(canvas.arcs), 1)
        self.assertEqual(canvas.texts[-1][1]["text"], "—")


class Phase3AdvisorTests(unittest.TestCase):
    def test_advisor_uses_current_session_until_current_changes(self):
        def session(
            thread_id, *, turns, input_tokens, cached_tokens, total_tokens,
        ):
            usage = TokenUsage(
                input_tokens, cached_tokens, total_tokens - input_tokens,
                0, total_tokens,
            )
            return SimpleNamespace(
                thread_id=thread_id,
                instruction=InstructionUsage(
                    f"turn-{thread_id}", "exact", usage, 1, None,
                    0, 0, 0, True, False,
                ),
                thread_cumulative_usage=TokenUsage(
                    input_tokens, cached_tokens, total_tokens - input_tokens,
                    0, total_tokens,
                ),
                observed_at=NOW - timedelta(seconds=turns),
                status="exact",
                turn_count=turns,
            )

        current_a = session(
            "A", turns=5, input_tokens=1_000,
            cached_tokens=500, total_tokens=1_200,
        )
        historical_b = session(
            "B", turns=40, input_tokens=OPTIMIZE_INPUT_TOKENS,
            cached_tokens=0, total_tokens=OPTIMIZE_INPUT_TOKENS + 5_000,
        )
        historical_c = session(
            "C", turns=45, input_tokens=OPTIMIZE_INPUT_TOKENS + 10_000,
            cached_tokens=0, total_tokens=OPTIMIZE_INPUT_TOKENS + 15_000,
        )
        quota_snapshot = CodexQuotaSnapshot.unavailable(observed_at=NOW)

        def build(current, selected):
            snapshot = SimpleNamespace(
                current_session=current,
                selected_session=selected,
                sessions_result=SimpleNamespace(refreshed_at=NOW),
            )
            return build_advisor_input(snapshot, quota_snapshot, now=NOW)

        selected_a = build(current_a, current_a)
        selected_b = build(current_a, historical_b)
        selected_c = build(current_a, historical_c)

        self.assertEqual(selected_a, selected_b)
        self.assertEqual(selected_a, selected_c)
        self.assertEqual(
            (
                selected_b.turn_count,
                selected_b.instruction_input_tokens,
                selected_b.instruction_total_tokens,
                selected_b.cached_input_tokens,
                selected_b.session_total_tokens,
                selected_b.session_status,
                selected_b.thread_safe_id,
                selected_b.source_observed_at,
            ),
            (5, 1_000, 1_200, 500, 1_200, "exact", "A", current_a.observed_at),
        )
        self.assertEqual(evaluate_advice(selected_b).primary.code, "normal")
        self.assertEqual(evaluate_advice(selected_c).primary.code, "normal")

        current_changed = build(historical_b, current_a)
        self.assertEqual(current_changed.thread_safe_id, "B")
        self.assertEqual(
            evaluate_advice(current_changed).primary.code,
            "new_thread",
        )

    def test_rule_codes_and_thresholds_are_centralized(self):
        self.assertEqual(len(ADVISOR_RULE_CODES), 6)
        self.assertGreater(NEW_THREAD_TURN_COUNT, 0)
        self.assertGreater(OPTIMIZE_INPUT_TOKENS, 0)
        self.assertGreater(OPTIMIZE_CACHE_HIT_PERCENT, 0)
        self.assertGreater(QUOTA_RISK_REMAINING_PERCENT, 0)
        self.assertGreater(DATA_STALE_AFTER.total_seconds(), 0)

    def test_data_unavailable_has_highest_priority(self):
        result = evaluate_advice(advisor_input(
            data_available=False, five_hour_remaining_percent=1,
            turn_count=NEW_THREAD_TURN_COUNT,
            instruction_input_tokens=OPTIMIZE_INPUT_TOKENS,
            cached_input_tokens=0,
        ))
        self.assertEqual(result.primary.status, "data_unavailable")

    def test_view_quota_primary_action_routes_to_live_quota_overview(self):
        dashboard = object.__new__(Dashboard)
        dashboard.advisor_result = SimpleNamespace(
            primary=SimpleNamespace(primary_action="view_quota"),
        )
        pages = []
        dashboard.show_page = pages.append

        Dashboard._execute_primary_action(dashboard)

        self.assertEqual(pages, ["overview"])

    def test_dashboard_advisor_uses_same_resolved_running_activity_as_ui_scope(self):
        completed = SimpleNamespace(
            thread_id="completed", instruction=InstructionUsage(
                "done", "exact", TokenUsage(10, 2, 3, 0, 13), 1,
                None, 0, 0, 0, True, False,
            ),
            thread_cumulative_usage=TokenUsage(10, 2, 3, 0, 13),
            observed_at=NOW, status="exact", turn_count=1,
        )
        running = SimpleNamespace(
            thread_id="running", instruction=InstructionUsage(
                "live", "in_progress", TokenUsage(20, 4, 5, 0, 25), 1,
                None, 0, 0, 0, False, True,
            ),
            thread_cumulative_usage=TokenUsage(20, 4, 5, 0, 25),
            observed_at=NOW, status="in_progress", turn_count=2,
        )
        dashboard = object.__new__(Dashboard)
        dashboard.snapshot = SimpleNamespace(
            current_session=completed,
            current_thread_id=completed.thread_id,
            selected_session=completed,
            selected_thread_id=completed.thread_id,
            recent_sessions=(completed, running),
            selection_mode="auto",
            sessions_result=SimpleNamespace(refreshed_at=NOW),
        )
        dashboard.quota_snapshot = CodexQuotaSnapshot.unavailable(observed_at=NOW)
        dashboard.trend_view = SimpleNamespace(samples=(), quota_samples=())
        marker = object()
        with (
            patch.object(main_module, "build_advisor_input", return_value=marker) as build,
            patch.object(main_module, "evaluate_advice", return_value=marker),
        ):
            self.assertIs(Dashboard._evaluate_advisor(dashboard), marker)

        advisor_snapshot = build.call_args.args[0]
        self.assertIs(advisor_snapshot.current_session, running)

    def test_stale_data_is_data_unavailable_status(self):
        result = evaluate_advice(advisor_input(data_age_seconds=round(DATA_STALE_AFTER.total_seconds()) + 1))
        self.assertEqual((result.primary.code, result.primary.status), ("data_stale", "data_unavailable"))

    def test_quota_risk_precedes_optimize(self):
        result = evaluate_advice(advisor_input(
            five_hour_remaining_percent=QUOTA_RISK_REMAINING_PERCENT,
            instruction_input_tokens=OPTIMIZE_INPUT_TOKENS,
            cached_input_tokens=0,
        ))
        self.assertEqual(result.primary.status, "quota_risk")

    def test_new_thread_precedes_optimize(self):
        result = evaluate_advice(advisor_input(
            turn_count=NEW_THREAD_TURN_COUNT,
            instruction_input_tokens=OPTIMIZE_INPUT_TOKENS,
            cached_input_tokens=0,
        ))
        self.assertEqual(result.primary.status, "new_thread")

    def test_low_cache_reuse_on_large_input_suggests_optimize(self):
        result = evaluate_advice(advisor_input(
            instruction_input_tokens=OPTIMIZE_INPUT_TOKENS,
            cached_input_tokens=0,
        ))
        self.assertEqual(result.primary.status, "optimize")
        self.assertIn(("cache_hit_percent_derived", 0.0), result.primary.evidence)

    def test_normal_state_is_stable(self):
        self.assertEqual(evaluate_advice(advisor_input()).primary.code, "normal")

    def test_missing_numeric_fields_do_not_crash(self):
        result = evaluate_advice(advisor_input(
            turn_count=None, instruction_input_tokens=None,
            instruction_total_tokens=None, cached_input_tokens=None,
            session_total_tokens=None, five_hour_remaining_percent=None,
            weekly_remaining_percent=None,
        ))
        self.assertEqual(result.primary.status, "normal")

    def test_same_input_has_deterministic_output(self):
        data = advisor_input(turn_count=NEW_THREAD_TURN_COUNT)
        self.assertEqual(evaluate_advice(data), evaluate_advice(data))

    def test_evidence_rejects_content_fields_and_free_text(self):
        with self.assertRaises(ValueError):
            Recommendation("normal", "normal", "x", "y", "z", (("prompt", 1),), NOW)
        with self.assertRaises(ValueError):
            Recommendation("normal", "normal", "x", "y", "z", (("session_status", "project text"),), NOW)

    def test_advice_wording_marks_cache_rate_as_non_official(self):
        self.assertIn("不是官方", translate("advisor_optimize_body", "zh-CN"))
        self.assertIn("not an official", translate("advisor_optimize_body", "en"))


class Phase3DiagnosticsTests(unittest.TestCase):
    def context(self, root: Path, **changes):
        base = DiagnosticContext(
            version="0.1.0",
            runtime_mode="dashboard",
            frozen=False,
            codex_executable_found=True,
            quota_probe=lambda: "normal",
            rollout_root=root,
            rollout_probe=lambda: 2,
            state_path=root / "missing.sqlite",
            settings_path=root / "missing.json",
            startup_status=lambda: "unused",
            tray_started=True,
            refreshed_at=NOW,
        )
        return replace(base, **changes)

    def test_diagnostics_run_all_thirteen_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_diagnostics(self.context(Path(directory)), now=NOW)
        self.assertEqual(tuple(item.code for item in report.results), DIAGNOSTIC_CHECK_CODES)

    def test_one_failure_does_not_abort_other_checks(self):
        def fail():
            raise RuntimeError("connection")
        with tempfile.TemporaryDirectory() as directory:
            report = run_diagnostics(self.context(Path(directory), quota_probe=fail), now=NOW)
        self.assertEqual(len(report.results), 13)
        self.assertEqual(report.results[4].status, "failure")
        self.assertEqual(report.results[-1].status, "normal")

    def test_numeric_probe_failure_is_isolated(self):
        def fail():
            raise OSError("read")
        with tempfile.TemporaryDirectory() as directory:
            report = run_diagnostics(self.context(Path(directory), rollout_probe=fail), now=NOW)
        numeric = next(item for item in report.results if item.code == "safe_numeric_data")
        self.assertEqual(numeric.status, "failure")

    def test_invalid_settings_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(inspect_settings_file(path), "failure")
            self.assertEqual(validate_ui_settings(path), (False, "invalid_json"))

    def test_invalid_sqlite_schema_is_detected_without_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            sqlite3.connect(path).close()
            report = run_diagnostics(self.context(Path(directory), state_path=path), now=NOW)
        sqlite_result = next(item for item in report.results if item.code == "sqlite_adapter")
        self.assertEqual(sqlite_result.status, "failure")

    def test_stale_data_is_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_diagnostics(
                self.context(Path(directory), refreshed_at=NOW - timedelta(minutes=4)), now=NOW,
            )
        self.assertEqual(report.results[-1].status, "warning")

    def test_diagnostics_can_repeat_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self.context(Path(directory))
            self.assertEqual(run_diagnostics(context, now=NOW), run_diagnostics(context, now=NOW))

    def test_diagnostic_contract_contains_no_content_or_credentials(self):
        fields = set(DiagnosticContext.__dataclass_fields__)
        forbidden = {"prompt", "response", "message", "reasoning", "authorization", "cookie", "secret"}
        self.assertTrue(fields.isdisjoint(forbidden))

    def test_diagnostic_translations_exist_in_both_languages(self):
        for code in DIAGNOSTIC_CHECK_CODES:
            self.assertIn(f"diagnostic_name_{code}", TRANSLATIONS["zh-CN"])
            self.assertIn(f"diagnostic_name_{code}", TRANSLATIONS["en"])


class Phase3WidgetAndSettingsTests(unittest.TestCase):
    def test_all_settings_round_trip_without_losing_existing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_language("en", path)
            save_startup_mode("tray", path)
            save_widget_idle_opacity(0.7, path)
            save_widget_mode("expanded", path)
            save_auto_refresh_enabled(True, path)
            save_exit_behavior("minimize", path)
            self.assertEqual(load_language(path), "en")
            self.assertEqual(load_startup_mode(path), "tray")
            self.assertEqual(load_widget_idle_opacity(path), 0.7)
            self.assertEqual(load_widget_mode(path), "expanded")
            self.assertTrue(load_auto_refresh_enabled(path))
            self.assertEqual(load_exit_behavior(path), "minimize")

    def test_settings_validation_ignores_legacy_dashboard_mode_but_checks_widget_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({
                "dashboard_mode": "broken",
                "widget_mode": "expanded",
            }), encoding="utf-8")
            self.assertEqual(validate_ui_settings(path), (True, "valid"))
            path.write_text(json.dumps({"widget_mode": "broken"}), encoding="utf-8")
            self.assertFalse(validate_ui_settings(path)[0])

    def test_widget_compact_and_expanded_transitions_are_query_free(self):
        widget = object.__new__(DesktopMiniWidget)
        widget.settings_path = Path("unused.json")
        widget.root = object()
        widget.compact_frame = FakeWidget()
        widget.expanded_frame = FakeWidget()
        widget.window = FakeWidget()
        with (
            patch.object(desktop_widget_module, "save_widget_mode") as save_mode,
            patch.object(desktop_widget_module, "monitor_work_area", return_value=object()),
            patch.object(desktop_widget_module, "clamp_position", return_value=(10, 20)),
        ):
            DesktopMiniWidget.set_mode(widget, "expanded")
            self.assertEqual(widget.mode, "expanded")
            self.assertFalse(widget.compact_frame.visible)
            self.assertTrue(widget.expanded_frame.visible)
            self.assertTrue(widget.window.geometry_calls[-1].startswith("820x116+"))
            save_mode.assert_called_once_with("expanded", widget.settings_path)

            DesktopMiniWidget.set_mode(widget, "compact", persist=False)
            self.assertEqual(widget.mode, "compact")
            self.assertTrue(widget.compact_frame.visible)
            self.assertFalse(widget.expanded_frame.visible)
            self.assertTrue(widget.window.geometry_calls[-1].startswith("300x78+"))
            save_mode.assert_called_once()

    def test_widget_hover_opacity_remains_full(self):
        self.assertEqual(HOVER_ALPHA, 1.0)

    def test_unknown_widget_quota_is_dash(self):
        self.assertEqual(format_percent(None), "—")

    def test_widget_switch_does_not_rebuild_root(self):
        source = inspect.getsource(DesktopMiniWidget.set_mode)
        self.assertNotIn("CTk(", source)
        self.assertNotIn("CTkToplevel(", source)

    def test_settings_callbacks_do_not_read_product_data(self):
        source = "".join(inspect.getsource(method) for method in (
            Dashboard._settings_startup_changed,
            Dashboard._settings_widget_changed,
            Dashboard._settings_exit_changed,
            Dashboard._settings_opacity_changed,
        ))
        for forbidden in ("view_model", "quota_provider", "refresh("):
            self.assertNotIn(forbidden, source)


class Phase3QuotaPresentationTests(unittest.TestCase):
    def test_unknown_quota_uses_dashes_and_unavailable_state(self):
        window = QuotaWindow.unavailable(
            QuotaKind.FIVE_HOUR, NOW, "codex_app_server",
        )
        for language in ("zh-CN", "en"):
            dashboard = object.__new__(Dashboard)
            dashboard.language = language
            summary = Dashboard._format_quota_summary(dashboard, window)
            self.assertIn("—", summary)
            self.assertIn(translate("quota_unavailable", language), summary)

    def test_stale_quota_keeps_real_percentages_and_stale_marker(self):
        window = QuotaWindow.from_reset_duration(
            QuotaKind.FIVE_HOUR,
            used_percent=40,
            remaining_percent=60,
            reset_after=timedelta(hours=1),
            observed_at=NOW,
            source="codex_app_server",
        ).as_stale("refresh_failed")
        for language in ("zh-CN", "en"):
            dashboard = object.__new__(Dashboard)
            dashboard.language = language
            summary = Dashboard._format_quota_summary(dashboard, window)
            self.assertIn("60%", summary)
            self.assertIn("40%", summary)
            self.assertIn(translate("quota_stale", language), summary)


class Phase3ProductBoundaryTests(unittest.TestCase):
    @staticmethod
    def _semantic_session(thread_id, total, cumulative, *, status="exact"):
        usage = TokenUsage(total // 2, total // 4, total // 2, 0, total)
        cumulative_usage = TokenUsage(
            cumulative // 2, cumulative // 4, cumulative // 2, 0, cumulative,
        )
        instruction = InstructionUsage(
            f"turn-{thread_id}", status, usage, 1, None, 0, 0, 0,
            status == "exact", status == "in_progress",
        )
        return CodexSessionUsage(
            thread_id, f"Session {thread_id}", "safe timestamp fallback",
            f"rollout-{thread_id}.jsonl", instruction, cumulative_usage,
            NOW, NOW, status, turn_count=4,
        )

    def test_header_keeps_status_message_visibly_bound(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = object()
        dashboard.main_container = FakeWidget()
        dashboard.status_message_var = FakeVar(value="loaded")
        dashboard.page_title_var = None
        dashboard.auto_refresh_var = FakeVar(value=True)
        dashboard.ui_icons = {}
        dashboard.language = "en"
        with (
            patch.multiple(
                main_module.ctk,
                CTkFrame=FakeWidget,
                CTkLabel=FakeWidget,
                CTkButton=FakeWidget,
                CTkSwitch=FakeWidget,
                CTkOptionMenu=FakeWidget,
            ),
            patch.object(main_module.tk, "StringVar", FakeVar),
            patch.object(main_module, "WidgetTooltip", lambda *args, **kwargs: None),
            patch.object(main_module, "create_icon", lambda *args, **kwargs: object()),
        ):
            Dashboard._build_header(dashboard)

        self.assertIs(
            dashboard.header_message_label.options["textvariable"],
            dashboard.status_message_var,
        )
        self.assertTrue(dashboard.header_message_label.visible)
        self.assertTrue(dashboard.header_message_label.grid_calls)

    def test_unified_status_center_has_one_primary_action_control(self):
        source = inspect.getsource(Dashboard._build_status_advice)
        self.assertEqual(source.count("self.primary_action_button ="), 1)

    def test_overview_builds_four_non_quota_metrics_and_one_live_quota_region(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = object()
        dashboard.core_metric_widgets = []
        dashboard.status_recent_rows = []
        dashboard.ui_icons = {}
        dashboard.start_diagnostics = lambda: None
        dashboard._open_codex = lambda: None
        dashboard.show_page = lambda _page: None
        parent = FakeWidget()
        with (
            patch.multiple(
                main_module.ctk,
                CTkFrame=FakeWidget,
                CTkLabel=FakeWidget,
                CTkButton=FakeWidget,
                CTkProgressBar=FakeWidget,
            ),
            patch.object(main_module.tk, "StringVar", FakeVar),
            patch.object(main_module, "WidgetTooltip", lambda *args, **kwargs: None),
            patch.object(main_module, "CircularProgress", FakeWidget),
            patch.object(main_module, "Sparkline", FakeWidget),
            patch.object(main_module, "create_icon", lambda *args, **kwargs: object()),
        ):
            Dashboard._build_core_metrics_panel(dashboard, parent)
            Dashboard._build_quota_center_card(dashboard, parent)
            Dashboard._build_quick_actions_card(dashboard, parent)
            Dashboard._build_status_recent_card(dashboard, parent)

        self.assertEqual(
            tuple(item["semantic"] for item in dashboard.core_metric_widgets),
            CORE_METRICS,
        )
        self.assertEqual(len(dashboard.core_metric_widgets), 4)
        self.assertFalse({
            "five_hour_quota", "weekly_quota",
        } & set(CORE_METRICS))
        self.assertTrue(all(
            item["card"].options["width"] == 128
            for item in dashboard.core_metric_widgets
        ))
        self.assertTrue(all(
            ("propagate", (False,), {}) in item["card"].configure_calls
            for item in dashboard.core_metric_widgets
        ))
        self.assertEqual(set(dashboard.quota_window_widgets), {"five", "week"})
        self.assertEqual(len(dashboard.quick_action_buttons), 4)
        self.assertEqual(len(dashboard.status_recent_rows), 3)

    def test_recent_task_text_regions_select_their_row(self):
        class ClickBindingWidget(FakeWidget):
            instances = []

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.master = args[0] if args else None
                self.bindings = {}
                self.instances.append(self)

            def bind(self, sequence, callback, *args, **kwargs):
                self.bindings[sequence] = callback

        dashboard = object.__new__(Dashboard)
        dashboard.root = object()
        dashboard.status_recent_rows = []
        selected = []
        dashboard._select_status_recent = selected.append
        parent = ClickBindingWidget()

        with (
            patch.multiple(
                main_module.ctk,
                CTkFrame=ClickBindingWidget,
                CTkLabel=ClickBindingWidget,
                CTkButton=ClickBindingWidget,
            ),
            patch.object(main_module.tk, "StringVar", FakeVar),
            patch.object(main_module, "WidgetTooltip", lambda *args, **kwargs: None),
        ):
            Dashboard._build_status_recent_card(dashboard, parent)

        row = dashboard.status_recent_rows[0]["button"]
        text_regions = [
            widget for widget in ClickBindingWidget.instances
            if widget.master is row
        ]
        callbacks = [
            widget.bindings["<Button-1>"]
            for widget in text_regions
            if "<Button-1>" in widget.bindings
        ]

        self.assertEqual(len(callbacks), 3)
        for callback in callbacks:
            callback(SimpleNamespace())
        self.assertEqual(selected, [0, 0, 0])

    def test_product_semantics_labels_exist_in_both_languages(self):
        expected = {
            "core_metrics_current_active": ("当前运行任务", "Current active task"),
            "core_metrics_most_recent": ("最近活动任务", "Most recently active task"),
            "observed_usage_title": ("全部会话用量", "All-session usage"),
            "session_detail_title": ("选中会话", "Selected session"),
            "selected_session_cumulative": ("会话累计", "Session cumulative"),
            "selected_task_badge": ("已选中", "Selected"),
            "trend_summary_samples": ("已保存完成响应", "Saved completed responses"),
        }
        for key, (zh, en) in expected.items():
            with self.subTest(key=key):
                self.assertEqual(translate(key, "zh-CN"), zh)
                self.assertEqual(translate(key, "en"), en)

    def test_selected_session_surfaces_use_selected_cumulative_label(self):
        source = inspect.getsource(Dashboard._apply_language)
        self.assertEqual(source.count('"session": "selected_session_cumulative"'), 3)

    def test_core_metrics_use_current_while_selected_detail_uses_selected(self):
        dashboard = object.__new__(Dashboard)
        dashboard.language = "en"
        current = self._semantic_session("A", 100, 1_000, status="in_progress")
        current = replace(
            current,
            instruction=replace(current.instruction, unreconciled_events=1),
        )
        selected = replace(
            self._semantic_session("B", 200, 2_000),
            display_title="用户Prompt：分析内部财务数据和客户名单",
            title_source="codex_app_server.thread_display_title",
            full_title="User prompt: analyze confidential customer records",
        )
        dashboard.snapshot = SimpleNamespace(
            current_session=current,
            current_thread_id="A",
            selected_session=selected,
            selected_thread_id="B",
            recent_sessions=(current, selected),
            selection_mode="pinned",
        )
        dashboard.quota_snapshot = CodexQuotaSnapshot.unavailable(observed_at=NOW)
        dashboard.simple_quota_vars = {
            "five_remaining": FakeVar(), "five_used": FakeVar(), "five_reset": FakeVar(),
            "week_remaining": FakeVar(), "week_used": FakeVar(), "week_reset": FakeVar(),
        }
        dashboard.quota_window_widgets = {}
        for prefix in ("five", "week"):
            ring = FakeWidget()
            ring.set = lambda *args, **kwargs: None
            dashboard.quota_window_widgets[prefix] = {
                "state": FakeVar(), "state_label": FakeWidget(),
                "ring": ring, "progress": FakeWidget(),
            }
        dashboard.simple_task_vars = {
            name: FakeVar() for name in (
                "title", "status", "turns", "instruction", "session", "activity",
            )
        }
        dashboard.task_full_title_var = FakeVar()
        dashboard.task_summary_status_var = FakeVar()
        dashboard.task_summary_status = FakeWidget()
        dashboard.core_metrics_scope_var = FakeVar()
        dashboard.core_metric_widgets = []
        for semantic in ("current_turn", "session_total"):
            dashboard.core_metric_widgets.append({
                "semantic": semantic, "value": FakeVar(), "full": FakeVar(),
                "hint": FakeVar(), "progress": None, "ring": None,
                "sparkline": None,
            })
        dashboard.task_detail_vars = {
            name: FakeVar() for name in (
                "title", "status", "activity", "turns", "input", "output",
                "total", "cached", "reasoning", "cache", "session",
                "quota_five", "quota_weekly", "advice",
            )
        }
        dashboard.task_detail_viewing_var = FakeVar()
        dashboard.advisor_result = None
        dashboard._metric_trend_samples = lambda _semantic: ()
        dashboard._full_token_tooltip = lambda value: str(value)
        dashboard._format_quota_summary = lambda _window: "quota"

        Dashboard._render_safe_overview(dashboard)

        core = {item["semantic"]: item["value"].get() for item in dashboard.core_metric_widgets}
        self.assertEqual(core, {"current_turn": "100", "session_total": "1.0K"})
        selected_title = dashboard.simple_task_vars["title"].get()
        self.assertIn("Historical session", selected_title)
        self.assertIn("4 turns", selected_title)
        self.assertIn("Anonymous code", selected_title)
        self.assertNotIn("Session B", selected_title)
        visible_values = (
            *(variable.get() for variable in dashboard.simple_task_vars.values()),
            dashboard.task_full_title_var.get(),
            *(variable.get() for variable in dashboard.task_detail_vars.values()),
            dashboard.task_detail_viewing_var.get(),
        )
        visible_text = " ".join(str(value) for value in visible_values)
        for fragment in ("内部财务数据", "客户名单", "confidential customer records"):
            self.assertNotIn(fragment, visible_text)
        self.assertEqual(dashboard.task_detail_vars["total"].get(), "200")
        self.assertEqual(dashboard.task_detail_vars["session"].get(), "2,000")
        self.assertEqual(dashboard.core_metrics_scope_var.get(), "Current active task")

        selected_c = self._semantic_session("C", 300, 3_000)
        dashboard.snapshot = SimpleNamespace(
            current_session=current,
            current_thread_id="A",
            selected_session=selected_c,
            selected_thread_id="C",
            recent_sessions=(current, selected, selected_c),
            selection_mode="pinned",
        )

        Dashboard._render_safe_overview(dashboard)

        core = {item["semantic"]: item["value"].get() for item in dashboard.core_metric_widgets}
        self.assertEqual(core, {"current_turn": "100", "session_total": "1.0K"})
        selected_title = dashboard.simple_task_vars["title"].get()
        self.assertIn("Historical session", selected_title)
        self.assertIn("Anonymous code", selected_title)
        self.assertNotIn("Session C", selected_title)
        self.assertEqual(dashboard.task_detail_vars["total"].get(), "300")
        self.assertEqual(dashboard.task_detail_vars["session"].get(), "3,000")

    def test_recent_rows_distinguish_current_and_selected_badges(self):
        dashboard = object.__new__(Dashboard)
        dashboard.language = "en"
        current = self._semantic_session("A", 100, 1_000, status="in_progress")
        selected = self._semantic_session("B", 200, 2_000)
        dashboard.snapshot = SimpleNamespace(
            current_session=current, current_thread_id="A",
            selected_session=selected, selected_thread_id="B",
            recent_sessions=(current, selected), selection_mode="pinned",
        )
        dashboard.status_recent_rows = []
        for _ in range(2):
            dashboard.status_recent_rows.append({
                "thread_id": None,
                "title": FakeVar(), "full_title": FakeVar(),
                "detail": FakeVar(), "current": FakeVar(),
                "button": FakeWidget(), "badge_label": FakeWidget(),
            })
        rows = tuple(SimpleNamespace(
            thread_id=thread_id,
            display_title=(
                "用户Prompt：分析内部财务数据和客户名单"
                if thread_id == "A"
                else "User prompt: analyze confidential customer records"
            ),
            full_title=(
                "用户Prompt：分析内部财务数据和客户名单"
                if thread_id == "A"
                else "User prompt: analyze confidential customer records"
            ),
            last_activity=NOW,
            thread_total_tokens=100, status="exact", turn_count=4,
            title_source="codex_app_server.thread_display_title",
        ) for thread_id in ("A", "B"))

        Dashboard._render_status_recent(
            dashboard, SimpleNamespace(recent_sessions=rows),
        )

        self.assertEqual(dashboard.status_recent_rows[0]["current"].get(), "Current")
        self.assertEqual(dashboard.status_recent_rows[1]["current"].get(), "Selected")
        self.assertIn("Running now", dashboard.status_recent_rows[0]["title"].get())
        self.assertIn("Viewing", dashboard.status_recent_rows[1]["title"].get())
        self.assertEqual(
            dashboard.status_recent_rows[1]["title"].get().count("Viewing"), 1,
        )
        self.assertIn("Anonymous code", dashboard.status_recent_rows[0]["detail"].get())
        self.assertNotIn("Session A", dashboard.status_recent_rows[0]["title"].get())
        rendered = " ".join(
            str(variable.get())
            for row in dashboard.status_recent_rows
            for key, variable in row.items()
            if key in {"title", "full_title", "detail", "current"}
        )
        for fragment in ("内部财务数据", "客户名单", "confidential customer records"):
            self.assertNotIn(fragment, rendered)

    def test_mini_widget_uses_content_free_session_label(self):
        dashboard = object.__new__(Dashboard)
        dashboard.language = "en"
        current = self._semantic_session("current-private-id", 100, 1_000, status="in_progress")
        selected = replace(
            self._semantic_session("selected-private-id", 200, 2_000),
            display_title="用户Prompt：分析内部财务数据和客户名单",
            full_title="User prompt: analyze confidential customer records",
        )
        dashboard.snapshot = SimpleNamespace(
            current_session=current,
            current_thread_id=current.thread_id,
            selected_session=selected,
            selected_thread_id=selected.thread_id,
            recent_sessions=(current, selected),
            selection_mode="pinned",
        )
        dashboard._widget_thread_id = selected.thread_id

        cached = Dashboard._cached_mini_snapshot(selected)
        refreshed = Dashboard._safe_mini_snapshot(
            dashboard,
            replace(
                cached,
                title="用户Prompt：分析内部财务数据和客户名单",
                full_title="User prompt: analyze confidential customer records",
            ),
        )

        self.assertIn("Historical session", refreshed.title)
        self.assertIn("Viewing", refreshed.title)
        self.assertIn("Anonymous code", refreshed.title)
        self.assertEqual(refreshed.full_title, refreshed.title)
        for fragment in ("内部财务数据", "客户名单", "confidential customer records"):
            self.assertNotIn(fragment, refreshed.title)
            self.assertNotIn(fragment, refreshed.full_title)
        self.assertNotIn(selected.thread_id, refreshed.title)
        enter_source = inspect.getsource(Dashboard._enter_widget_mode)
        self.assertIn("self._safe_mini_snapshot(", enter_source)

    def test_saved_quota_samples_remain_usable_when_live_window_is_unavailable(self):
        observed = datetime.now(timezone.utc)
        quota_samples = tuple(
            SimpleNamespace(
                five_hour_observed_at=observed - timedelta(minutes=offset),
                five_hour_available=True,
                five_hour_used_percent=value,
            )
            for offset, value in ((2, 30.0), (1, 35.0))
        )
        view = TrendView(
            7,
            "available",
            (),
            observed,
            quota_samples=quota_samples,
            five_hour_last_seen_at=observed,
            five_hour_available=False,
        )

        self.assertEqual(
            Dashboard._trend_metric_quality(view, "five_hour", 2),
            "available",
        )

    def test_trend_preview_has_distinct_zero_one_and_two_sample_states(self):
        dashboard = object.__new__(Dashboard)
        dashboard.language = "en"
        dashboard.trend_metric = "total"
        dashboard.snapshot = SimpleNamespace(
            current_session=None,
            recent_sessions=(),
            selected_session=SimpleNamespace(
                thread_id="selected", turn_count=18,
                status="exact", instruction=None,
            ),
            selected_thread_id="selected",
            selection_mode="pinned",
        )
        dashboard.trend_quality_var = FakeVar()
        dashboard.trend_quality_message_var = FakeVar()
        dashboard.trend_quality_label = FakeWidget()
        dashboard.trend_scope_var = FakeVar()
        dashboard.trend_preview_scope_var = FakeVar()
        dashboard.trend_summary_vars = {
            key: FakeVar() for key in ("range", "samples", "updated")
        }
        dashboard.trend_metric_vars = {
            key: FakeVar() for key in ("current", "minimum", "maximum", "change")
        }
        dashboard.trend_chart = FakeWidget()
        dashboard.trend_chart.set_points = lambda _points: None
        dashboard.trend_preview_state_var = FakeVar()
        dashboard.trend_preview_state = FakeWidget()
        dashboard.trend_preview_message_var = FakeVar()
        dashboard.trend_preview_plot = FakeWidget()
        dashboard.trend_preview_plot.set_samples = (
            lambda values: len(tuple(values)) >= 2
        )
        dashboard._trend_points = (
            lambda view, _metric: tuple(range(len(view.samples)))
        )

        samples = tuple(
            SimpleNamespace(
                source_available=True,
                source_observed_at=NOW + timedelta(minutes=index),
                total_tokens=100 + index * 100,
                source_type="dashboard",
                token_stale=False,
            )
            for index in range(2)
        )
        dashboard.global_history_view = TrendView(
            90, "available", samples, samples[-1].source_observed_at,
        )

        dashboard.trend_view = TrendView(7, "empty", (), None)
        Dashboard._render_trends(dashboard)
        self.assertIn(
            "selected session has no matching history",
            dashboard.trend_quality_message_var.get(),
        )
        self.assertEqual(dashboard.trend_metric_vars["minimum"].get(), "—")
        self.assertEqual(dashboard.trend_metric_vars["maximum"].get(), "—")
        self.assertFalse(dashboard.trend_preview_plot.visible)

        dashboard.trend_view = TrendView(
            7, "available", samples[:1], samples[0].source_observed_at,
        )
        Dashboard._render_trends(dashboard)
        one_message = dashboard.trend_preview_message_var.get()
        self.assertIn("Only 1 completed response has been saved", one_message)
        self.assertIn("Response value: 100", one_message)
        self.assertIn("Selected session turns: 18", one_message)
        self.assertIn("Turns are not trend samples", one_message)
        self.assertEqual(dashboard.trend_metric_vars["minimum"].get(), "—")
        self.assertEqual(dashboard.trend_metric_vars["maximum"].get(), "—")
        self.assertFalse(dashboard.trend_preview_plot.visible)

        dashboard.trend_view = TrendView(
            7, "available", samples, samples[-1].source_observed_at,
        )
        Dashboard._render_trends(dashboard)
        self.assertIn(
            "2 saved completed responses",
            dashboard.trend_preview_message_var.get(),
        )
        self.assertEqual(dashboard.trend_metric_vars["minimum"].get(), "100")
        self.assertEqual(dashboard.trend_metric_vars["maximum"].get(), "200")
        self.assertTrue(dashboard.trend_preview_plot.visible)

    def test_metric_detail_keeps_six_required_technical_fields(self):
        metrics = _latest_metrics(None, "unavailable")
        self.assertEqual(
            [item.label for item in metrics],
            ["Input", "Output", "Total", "Cached", "Reasoning", "Cache Hit"],
        )

    def test_generic_handoff_template_contains_only_manual_placeholder(self):
        chinese = generic_handoff_template("zh-CN")
        english = generic_handoff_template("en")
        self.assertIn("手动整理", chinese)
        self.assertIn("请在这里", chinese)
        self.assertIn("organized manually", english)
        for value in (chinese.lower(), english.lower()):
            self.assertNotIn("response", value)
            self.assertNotIn("reasoning", value)
            self.assertNotIn("tool output", value)

    def test_new_thread_dialog_uses_one_toplevel_and_no_mainloop(self):
        source = inspect.getsource(Dashboard._show_new_thread_dialog)
        self.assertEqual(source.count("ctk.CTkToplevel("), 1)
        self.assertNotIn("mainloop", source)
        self.assertNotIn("view_model", source)

    def test_no_aos_runtime_dependency_is_added(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8").lower()
        build_requirements = Path("requirements-build.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("aos", requirements + build_requirements)

    def test_product_actions_do_not_offer_knowledge_features(self):
        source = inspect.getsource(Dashboard._build_tools_page)
        for forbidden in ("knowledge", "project_export", "context_restore", "scan_project"):
            self.assertNotIn(forbidden, source.lower())

    def test_tools_build_four_groups_and_disable_unimplemented_actions(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = object()
        dashboard.diagnostic_rows = []
        parent = FakeWidget()
        with (
            patch.multiple(
                main_module.ctk,
                CTkScrollableFrame=FakeWidget,
                CTkFrame=FakeWidget,
                CTkLabel=FakeWidget,
                CTkButton=FakeWidget,
            ),
            patch.object(main_module.tk, "StringVar", FakeVar),
        ):
            Dashboard._build_tools_page(dashboard, parent)

        self.assertEqual(set(dashboard.tool_group_titles), {
            "diagnostics", "data", "workflow", "help",
        })
        self.assertEqual(len(dashboard.tool_group_cards), 4)
        self.assertTrue(all(
            button.options.get("state") == "disabled"
            for button in dashboard.coming_soon_buttons
        ))

    def test_settings_builds_five_contract_groups(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = object()
        dashboard.auto_refresh_var = FakeVar(value=False)
        dashboard.startup_adapter = SimpleNamespace(
            is_enabled=lambda _path: False,
            is_supported=lambda: True,
        )
        parent = FakeWidget()
        with (
            patch.multiple(
                main_module.ctk,
                CTkScrollableFrame=FakeWidget,
                CTkFrame=FakeWidget,
                CTkLabel=FakeWidget,
                CTkButton=FakeWidget,
                CTkOptionMenu=FakeWidget,
                CTkSwitch=FakeWidget,
                CTkSlider=FakeWidget,
            ),
            patch.object(main_module.tk, "BooleanVar", FakeVar),
            patch.object(main_module.tk, "DoubleVar", FakeVar),
            patch.object(main_module.tk, "StringVar", FakeVar),
        ):
            Dashboard._build_settings_page(dashboard, parent)

        self.assertEqual(set(dashboard.settings_group_titles), {
            "general", "refresh", "windows", "widget", "privacy",
        })
        self.assertEqual(len(dashboard.settings_group_cards), 5)
        self.assertEqual(main_module.DEFAULT_AUTO_REFRESH_SECONDS, 60)

    def test_diagnostic_results_use_one_dialog_and_no_second_mainloop(self):
        source = inspect.getsource(Dashboard._show_diagnostic_dialog)
        self.assertEqual(source.count("ctk.CTkToplevel("), 1)
        self.assertNotIn("mainloop", source)
        self.assertNotIn("rollout_path", source)
        self.assertNotIn("exception", source.lower())

    def test_new_analytics_copy_excludes_stitch_off_domain_terms(self):
        keys = {
            "trend_page_description", "trend_quality_available_message",
            "trend_quality_insufficient_message", "trend_quality_unavailable_message",
            "trend_quality_stale_message", "recommendations_description",
            "tools_group_diagnostics", "tools_group_data", "tools_group_workflow",
            "tools_group_help", "backup_monitor_data", "restore_monitor_data",
            "clear_monitor_cache",
        }
        forbidden = (
            "enterprise", "latency", "cluster", "deployment", "infrastructure",
            "global node map", "telemetry", "raw trace", "dollar savings",
            "potential savings", "network bandwidth", "request rate",
        )
        for language in ("zh-CN", "en"):
            copy = " ".join(TRANSLATIONS[language][key] for key in keys).lower()
            for term in forbidden:
                self.assertNotIn(term, copy)

    def test_all_new_product_keys_are_bilingual(self):
        keys = {
            "nav_overview", "nav_sessions", "nav_usage_trends",
            "nav_recommendations", "nav_session_detail", "nav_tools",
            "nav_settings", "status_advice_title", "core_metrics_title",
            "core_metric_current_turn", "core_metric_session_total",
            "core_metric_cache_reuse", "core_metric_reasoning",
            "core_metric_five_hour_quota", "core_metric_weekly_quota",
            "quota_center_title",
            "current_task_card_title", "quick_actions_title",
            "one_click_diagnostics", "open_codex", "prepare_new_thread", "more_tools",
            "recent_tasks_title", "view_all_tasks", "five_hour_limit",
            "weekly_limit", "back_status_center", "prepare_new_thread",
            "diagnostics_title", "widget_compact", "widget_expanded",
            "trend_quality_available_title", "trend_quality_insufficient_title",
            "trend_quality_unavailable_title", "trend_quality_stale_title",
            "tools_group_diagnostics", "tools_group_data",
            "tools_group_workflow", "tools_group_help",
            "settings_group_general", "settings_group_refresh",
            "settings_group_windows", "settings_group_widget",
            "settings_group_privacy", "coming_soon",
        }
        self.assertTrue(keys.issubset(TRANSLATIONS["zh-CN"]))
        self.assertTrue(keys.issubset(TRANSLATIONS["en"]))


if __name__ == "__main__":
    unittest.main()
