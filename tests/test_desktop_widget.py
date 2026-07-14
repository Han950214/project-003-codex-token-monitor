import inspect
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app.desktop_widget as desktop_widget_module
from app.desktop_widget import (
    DEFAULT_ALPHA,
    DesktopMiniWidget,
    ExitChoiceDialog,
    HOVER_ALPHA,
    WIDGET_HEIGHT,
    WIDGET_MARGIN,
    WIDGET_WIDTH,
    WorkArea,
    WIDGET_ERROR,
    WIDGET_SUCCESS,
    WIDGET_UNKNOWN,
    WIDGET_WARNING,
    _bounded_title,
    _quota_color,
    clamp_position,
    format_percent,
    format_reset_time,
    format_token_total,
    top_right_position,
)
from app.i18n import TRANSLATIONS
from app.main import Dashboard
from app.quota import QuotaKind, QuotaWindow
from app.ui_settings import (
    load_language,
    load_widget_position,
    save_language,
    load_exit_action_for_today,
    save_widget_position,
    save_exit_action_for_today,
)


class DesktopWidgetFormattingTests(unittest.TestCase):
    def test_token_totals_use_compact_numbers_or_dash(self):
        self.assertEqual(format_token_total(5_092_543), "5.09M")
        self.assertEqual(format_token_total(25_434_615), "25.43M")
        self.assertEqual(format_token_total(108_872_954), "108.87M")
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

    def test_long_title_is_bounded_without_losing_full_title_source(self):
        title = "Codex session with a very long safe title that must not resize the widget"
        self.assertEqual(_bounded_title(title, 32), "Codex session with a very long…")
        self.assertLessEqual(len(_bounded_title(title, 32)), 32)

    def test_quota_color_tracks_reliability_and_remaining_risk(self):
        observed = datetime(2026, 7, 14, tzinfo=timezone.utc)

        def window(remaining: float) -> QuotaWindow:
            return QuotaWindow.from_reset_duration(
                QuotaKind.FIVE_HOUR,
                used_percent=100 - remaining,
                remaining_percent=remaining,
                reset_after=timedelta(hours=1),
                observed_at=observed,
                source="test",
            )

        self.assertEqual(_quota_color(window(80), "normal"), WIDGET_SUCCESS)
        self.assertEqual(_quota_color(window(20), "normal"), WIDGET_WARNING)
        self.assertEqual(_quota_color(window(80), "stale"), WIDGET_WARNING)
        self.assertEqual(_quota_color(window(80), "invalid"), WIDGET_ERROR)
        unavailable = QuotaWindow.unavailable(QuotaKind.FIVE_HOUR, observed, "test")
        self.assertEqual(_quota_color(unavailable, "unavailable"), WIDGET_UNKNOWN)


class DesktopWidgetPositionTests(unittest.TestCase):
    def test_top_right_uses_sixteen_pixel_margin(self):
        self.assertEqual(top_right_position(WorkArea(0, 0, 1920, 1040), WIDGET_WIDTH, WIDGET_HEIGHT, WIDGET_MARGIN), (1084, 16))

    def test_top_right_respects_offset_monitor_work_area(self):
        self.assertEqual(top_right_position(WorkArea(-1280, 0, 0, 984), WIDGET_WIDTH, WIDGET_HEIGHT, WIDGET_MARGIN), (-836, 16))

    def test_position_is_clamped_inside_visible_work_area(self):
        area = WorkArea(0, 0, 1920, 1040)
        self.assertEqual(clamp_position((-500, 3000), area, WIDGET_WIDTH, WIDGET_HEIGHT), (0, 924))

    def test_oversized_widget_falls_back_to_work_area_origin(self):
        area = WorkArea(100, 50, 300, 250)
        self.assertEqual(clamp_position((900, 900), area, WIDGET_WIDTH, WIDGET_HEIGHT), (100, 134))


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
        self.assertIn("WidgetTooltip(self.restore_button", source)
        self.assertIn("WidgetTooltip(self.refresh_button", source)
        self.assertIn("WidgetTooltip(self.more_button", source)
        self.assertIn('translate("more_tools"', source)
        self.assertIn("WidgetTooltip(self.collapse_button", source)
        self.assertIn("WidgetTooltip(self.exit_button", source)
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

    def test_exit_dialog_executes_full_content_build_before_mapping_window(self):
        events = []

        class FakeVariable:
            def __init__(self, *args, value=False, **kwargs):
                self.value = value

            def get(self):
                return self.value

        class FakeChild:
            def __init__(self, kind, *args, **kwargs):
                self.kind = kind
                events.append(("create", kind, kwargs.get("text")))

            def grid(self, *args, **kwargs):
                events.append(("grid", self.kind))

            def focus_set(self):
                events.append(("focus", self.kind))

        class FakeWindow:
            def title(self, value):
                events.append(("title", value))

            def withdraw(self):
                events.append(("withdraw",))

            def geometry(self, value):
                events.append(("geometry", value))

            def resizable(self, *args):
                pass

            def configure(self, **kwargs):
                pass

            def transient(self, owner):
                pass

            def grid_columnconfigure(self, *args, **kwargs):
                pass

            def protocol(self, *args):
                pass

            def bind(self, *args):
                pass

            def update_idletasks(self):
                events.append(("idle",))

            def deiconify(self):
                events.append(("deiconify",))

            def lift(self):
                pass

            def grab_set(self):
                events.append(("grab",))

            def winfo_exists(self):
                return True

        class FakeOwner:
            @staticmethod
            def winfo_rootx():
                return 100

            @staticmethod
            def winfo_rooty():
                return 80

            @staticmethod
            def winfo_width():
                return 800

            @staticmethod
            def winfo_height():
                return 600

        window = FakeWindow()
        factories = {
            "CTkLabel": lambda *args, **kwargs: FakeChild("label", *args, **kwargs),
            "CTkCheckBox": lambda *args, **kwargs: FakeChild("checkbox", *args, **kwargs),
            "CTkFrame": lambda *args, **kwargs: FakeChild("frame", *args, **kwargs),
            "CTkButton": lambda *args, **kwargs: FakeChild("button", *args, **kwargs),
        }
        with tempfile.TemporaryDirectory() as directory:
            dialog = ExitChoiceDialog(object(), Path(directory) / "settings.json")
            with (
                patch.object(desktop_widget_module.ctk, "CTkToplevel", return_value=window),
                patch.multiple(desktop_widget_module.ctk, **factories),
                patch.object(desktop_widget_module.tk, "BooleanVar", FakeVariable),
            ):
                dialog.show(owner=FakeOwner(), language="zh-CN", on_choice=lambda _action: None)

        created = [event[1] for event in events if event[0] == "create"]
        self.assertEqual(created.count("label"), 2)
        self.assertEqual(created.count("checkbox"), 1)
        self.assertEqual(created.count("button"), 2)
        self.assertLess(events.index(("withdraw",)), next(
            index for index, event in enumerate(events) if event[0] == "create"
        ))
        self.assertLess(events.index(("idle",)), events.index(("deiconify",)))
        self.assertIn(("grab",), events)

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

    def test_taskbar_callback_remains_available_from_dashboard(self):
        widget_source = inspect.getsource(DesktopMiniWidget.__init__)
        self.assertIn("on_minimize", widget_source)
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
        source = inspect.getsource(DesktopMiniWidget._build)
        self.assertIn("instruction_total_var", source)
        self.assertIn("session_total_var", source)
        self.assertIn("_build_horizontal_metric", source)

    def test_widget_keeps_horizontal_reference_size_and_full_title_tooltip(self):
        source = inspect.getsource(DesktopMiniWidget._build)
        self.assertIn("thread_full_title_var", source)
        self.assertIn("grid_propagate(False)", source)
        self.assertEqual((WIDGET_WIDTH, WIDGET_HEIGHT), (820, 116))

    def test_widget_uses_single_horizontal_quota_state(self):
        constructor = inspect.getsource(DesktopMiniWidget.__init__)
        self.assertIn("self.remaining_var", constructor)
        self.assertIn("self.reset_var", constructor)
        self.assertIn("self.quota_title_var", constructor)
        self.assertIn("self.quota_ring", constructor)
        self.assertNotIn("self.remaining_vars", constructor)
        self.assertNotIn("self.quota_rings", constructor)

    def test_horizontal_quota_update_uses_initialized_single_value_fields(self):
        class Sink:
            def __init__(self):
                self.value = None
                self.color = None

            def set(self, value):
                self.value = value

            def configure(self, **kwargs):
                self.color = kwargs.get("text_color")

        observed = datetime(2026, 7, 14, tzinfo=timezone.utc)
        window = QuotaWindow.from_reset_duration(
            QuotaKind.FIVE_HOUR,
            used_percent=25,
            remaining_percent=75,
            reset_after=timedelta(hours=1),
            observed_at=observed,
            source="test",
        )
        widget = DesktopMiniWidget.__new__(DesktopMiniWidget)
        widget.language = "zh-CN"
        widget.remaining_var = Sink()
        widget.reset_var = Sink()
        widget.compact_quota_label = Sink()
        widget.quota_value_label = Sink()
        widget.quota_ring = None

        widget._update_window(window, "normal")

        self.assertEqual(widget.remaining_var.value, "剩余 75%")
        self.assertEqual(widget.compact_quota_label.color, WIDGET_SUCCESS)
        self.assertEqual(widget.quota_value_label.color, WIDGET_SUCCESS)


if __name__ == "__main__":
    unittest.main()
