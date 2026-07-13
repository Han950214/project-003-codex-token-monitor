import inspect
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.desktop_widget import (
    DEFAULT_ALPHA,
    DesktopMiniWidget,
    ExitChoiceDialog,
    HOVER_ALPHA,
    WIDGET_HEIGHT,
    WIDGET_MARGIN,
    WIDGET_WIDTH,
    WorkArea,
    clamp_position,
    format_percent,
    format_reset_time,
    format_token_total,
    top_right_position,
)
from app.i18n import TRANSLATIONS
from app.main import Dashboard
from app.ui_settings import (
    load_language,
    load_widget_position,
    save_language,
    load_exit_action_for_today,
    save_widget_position,
    save_exit_action_for_today,
)


class DesktopWidgetFormattingTests(unittest.TestCase):
    def test_token_totals_use_exact_grouped_numbers_or_dash(self):
        self.assertEqual(format_token_total(5_092_543), "5,092,543")
        self.assertEqual(format_token_total(25_434_615), "25,434,615")
        self.assertEqual(format_token_total(108_872_954), "108,872,954")
        self.assertEqual(format_token_total(None), "—")

    def test_integer_and_single_decimal_percent_format(self):
        self.assertEqual(format_percent(48), "48%")
        self.assertEqual(format_percent(48.46), "48.5%")

    def test_unknown_percent_is_dash(self):
        self.assertEqual(format_percent(None), "—")

    def test_percent_display_is_clamped(self):
        self.assertEqual(format_percent(-1), "0%")
        self.assertEqual(format_percent(101), "100%")

    def test_reset_time_formats_today_and_tomorrow_in_chinese(self):
        tz = timezone(timedelta(hours=8))
        observed = datetime(2026, 7, 12, 9, 0, tzinfo=tz)
        self.assertEqual(format_reset_time(observed.replace(hour=18), "zh-CN", observed), "今天 18:00")
        self.assertEqual(format_reset_time(observed + timedelta(days=1), "zh-CN", observed), "明天 09:00")

    def test_reset_time_formats_today_and_future_date_in_english(self):
        tz = datetime.now().astimezone().tzinfo
        observed = datetime(2026, 7, 12, 9, 0, tzinfo=tz)
        self.assertEqual(format_reset_time(observed.replace(hour=18), "en", observed), "Today 18:00")
        self.assertEqual(format_reset_time(observed + timedelta(days=3), "en", observed), "Jul 15, 09:00")

    def test_unknown_reset_time_is_dash(self):
        self.assertEqual(format_reset_time(None, "en"), "—")


class DesktopWidgetPositionTests(unittest.TestCase):
    def test_top_right_uses_sixteen_pixel_margin(self):
        self.assertEqual(top_right_position(WorkArea(0, 0, 1920, 1040), WIDGET_WIDTH, WIDGET_HEIGHT, WIDGET_MARGIN), (1564, 16))

    def test_top_right_respects_offset_monitor_work_area(self):
        self.assertEqual(top_right_position(WorkArea(-1280, 0, 0, 984), WIDGET_WIDTH, WIDGET_HEIGHT, WIDGET_MARGIN), (-356, 16))

    def test_position_is_clamped_inside_visible_work_area(self):
        area = WorkArea(0, 0, 1920, 1040)
        self.assertEqual(clamp_position((-500, 3000), area, WIDGET_WIDTH, WIDGET_HEIGHT), (0, 540))

    def test_oversized_widget_falls_back_to_work_area_origin(self):
        area = WorkArea(100, 50, 300, 250)
        self.assertEqual(clamp_position((900, 900), area, WIDGET_WIDTH, WIDGET_HEIGHT), (100, 50))


class DesktopWidgetSettingsAndLifecycleTests(unittest.TestCase):
    def test_widget_position_is_persisted_without_losing_language(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            save_language("en", path)
            save_widget_position(120, 80, path)
            self.assertEqual(load_language(path), "en")
            self.assertEqual(load_widget_position(path), (120, 80))

    def test_language_save_preserves_widget_position(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            save_widget_position(10, 20, path)
            save_language("zh-CN", path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["widget_position"], {"x": 10, "y": 20})

    def test_invalid_widget_position_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            path.write_text('{"widget_position":{"x":"10","y":20}}', encoding="utf-8")
            self.assertIsNone(load_widget_position(path))

    def test_exit_choice_is_remembered_only_for_selected_day(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            selected = date(2026, 7, 12)
            self.assertTrue(save_exit_action_for_today("minimize", path, today=selected))
            self.assertEqual(load_exit_action_for_today(path, today=selected), "minimize")
            self.assertIsNone(load_exit_action_for_today(path, today=selected + timedelta(days=1)))

    def test_invalid_exit_choice_is_not_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            self.assertFalse(save_exit_action_for_today("unknown", path))
            self.assertFalse(path.exists())

    def test_chinese_and_english_translation_key_sets_match(self):
        self.assertEqual(set(TRANSLATIONS["zh-CN"]), set(TRANSLATIONS["en"]))

    def test_widget_uses_one_toplevel_and_no_new_root_or_mainloop(self):
        source = inspect.getsource(DesktopMiniWidget)
        self.assertEqual(source.count("ctk.CTkToplevel("), 2)  # Widget plus opacity popover; no extra root.
        self.assertNotIn("ctk.CTk(", source)
        self.assertNotIn("mainloop(", source)

    def test_widget_is_readable_transparent_and_opaque_on_hover(self):
        self.assertGreater(DEFAULT_ALPHA, 0.5)
        self.assertLess(DEFAULT_ALPHA, HOVER_ALPHA)
        source = inspect.getsource(DesktopMiniWidget._bind_hover_opacity)
        self.assertIn('"<Enter>"', source)
        self.assertIn('"<Leave>"', source)

    def test_widget_icon_controls_have_localized_tooltips(self):
        source = inspect.getsource(DesktopMiniWidget._build)
        self.assertIn("WidgetTooltip(self.minimize_button", source)
        self.assertIn("WidgetTooltip(self.exit_button", source)
        self.assertIn('text="—"', source)
        self.assertIn('text="×"', source)

    def test_widget_idle_opacity_does_not_rebuild_window(self):
        source = inspect.getsource(DesktopMiniWidget.set_idle_opacity)
        self.assertIn("_refresh_pointer_opacity", source)
        self.assertNotIn("CTkToplevel", source)
    def test_exit_dialog_defaults_enter_escape_and_close_to_minimize(self):
        source = inspect.getsource(ExitChoiceDialog.show)
        self.assertIn('self._choose("minimize")', source)
        self.assertIn('translate("dont_ask_today"', source)
        self.assertIn('minimize_button.focus_set()', source)

    def test_dashboard_creates_single_widget_and_single_auto_refresh_controller(self):
        source = inspect.getsource(Dashboard.__init__)
        self.assertEqual(source.count("DesktopMiniWidget("), 1)
        self.assertEqual(source.count("AutoRefreshController("), 1)

    def test_main_and_widget_exit_routes_use_the_choice_prompt(self):
        source = inspect.getsource(Dashboard.__init__)
        self.assertIn('root.protocol("WM_DELETE_WINDOW", self.request_exit)', source)
        self.assertIn('on_exit=self.request_exit', source)
        decision = inspect.getsource(Dashboard._apply_exit_choice)
        self.assertIn('action == "exit"', decision)
        self.assertIn('self._minimize_to_taskbar()', decision)

    def test_widget_minimize_has_a_direct_taskbar_callback(self):
        widget_source = inspect.getsource(DesktopMiniWidget.__init__)
        self.assertIn("on_minimize", widget_source)
        build_source = inspect.getsource(DesktopMiniWidget._build)
        self.assertIn("command=self.on_minimize", build_source)
        self.assertNotIn("command=self.on_exit, width=64", build_source)
        dashboard_source = inspect.getsource(Dashboard.__init__)
        self.assertIn("on_minimize=self._minimize_to_taskbar", dashboard_source)

    def test_taskbar_minimize_hides_widget_and_suppresses_widget_intercept(self):
        minimize = inspect.getsource(Dashboard._minimize_to_taskbar)
        self.assertIn("self.mini_widget.hide()", minimize)
        self.assertIn("self.root.iconify()", minimize)
        unmap = inspect.getsource(Dashboard._on_root_unmap)
        self.assertIn("self._taskbar_mode", unmap)

    def test_minimize_captures_actual_selected_session_and_freezes_auto_follow(self):
        source = inspect.getsource(Dashboard._enter_widget_mode)
        self.assertIn("snapshot.selected_session", source)
        self.assertIn("view_model.pin_thread(thread_id)", source)

    def test_restore_reuses_dashboard_without_refreshing_data(self):
        source = inspect.getsource(Dashboard.restore_dashboard)
        self.assertIn("self.root.deiconify()", source)
        self.assertNotIn("refresh", source)
        self.assertNotIn("Dashboard(", source)

    def test_taskbar_restore_does_not_refresh_data(self):
        source = inspect.getsource(Dashboard._finish_taskbar_restore)
        self.assertNotIn("refresh", source)

    def test_main_header_has_direct_mini_widget_button(self):
        build_source = inspect.getsource(Dashboard._build_header)
        self.assertIn("self.mini_widget_button", build_source)
        self.assertIn("command=self._enter_widget_mode", build_source)

    def test_repeated_minimize_is_guarded(self):
        source = inspect.getsource(Dashboard._enter_widget_mode)
        self.assertIn("if self._widget_mode", source)

    def test_widget_does_not_display_thread_id_or_rollout_path(self):
        source = inspect.getsource(DesktopMiniWidget.update)
        self.assertNotIn("thread_id", source)
        self.assertNotIn("rollout", source)

    def test_widget_has_two_side_by_side_token_indicators(self):
        source = inspect.getsource(DesktopMiniWidget._build_thread_card)
        self.assertIn("instruction_total_var", source)
        self.assertIn("session_total_var", source)
        self.assertIn("column=1, rowspan=2", source)
        self.assertIn("height=52", source)

    def test_widget_keeps_token_card_title_to_two_lines_and_fixed_size(self):
        source = inspect.getsource(DesktopMiniWidget._build_thread_card)
        self.assertIn("height=36", source)
        self.assertEqual((WIDGET_WIDTH, WIDGET_HEIGHT), (340, 500))


if __name__ == "__main__":
    unittest.main()
