import inspect
import sys
import types
import unittest
from unittest.mock import Mock, patch

from app.main import Dashboard
from app.system_tray import SystemTrayController, TrayState, load_tray_icon


class FakeMenu(tuple):
    SEPARATOR = object()

    def __new__(cls, *items):
        return tuple.__new__(cls, items)


class FakeMenuItem:
    def __init__(self, text, action, default=False):
        self.text, self.action, self.default = text, action, default


class FakeIcon:
    instances = []

    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.run_count = self.stop_count = self.update_count = 0
        self.instances.append(self)

    def run_detached(self):
        self.run_count += 1

    def stop(self):
        self.stop_count += 1

    def update_menu(self):
        self.update_count += 1


class SystemTrayTests(unittest.TestCase):
    def setUp(self):
        FakeIcon.instances.clear()
        self.pystray = types.SimpleNamespace(Icon=FakeIcon, Menu=FakeMenu, MenuItem=FakeMenuItem)
        self.root = Mock()
        self.callbacks = {name: Mock() for name in ("restore", "widget", "hide", "refresh", "toggle", "settings", "exit")}

    def controller(self):
        return SystemTrayController(
            self.root,
            on_restore_dashboard=self.callbacks["restore"],
            on_show_widget=self.callbacks["widget"],
            on_hide_to_tray=self.callbacks["hide"],
            on_manual_refresh=self.callbacks["refresh"],
            on_toggle_auto_refresh=self.callbacks["toggle"],
            on_settings=self.callbacks["settings"],
            on_exit=self.callbacks["exit"],
        )

    def test_project_icon_is_local_rgba_asset(self):
        image = load_tray_icon()
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.size, (32, 32))

    def test_tray_starts_once_and_repeated_start_reuses_icon(self):
        with patch.dict(sys.modules, {"pystray": self.pystray}):
            controller = self.controller()
            self.assertTrue(controller.start())
            self.assertTrue(controller.start())
        self.assertEqual(len(FakeIcon.instances), 1)
        self.assertEqual(FakeIcon.instances[0].run_count, 1)

    def test_tray_stops_once_and_removes_icon_reference(self):
        with patch.dict(sys.modules, {"pystray": self.pystray}):
            controller = self.controller()
            controller.start()
            icon = controller.icon
            controller.stop()
            controller.stop()
        self.assertEqual(icon.stop_count, 1)
        self.assertIsNone(controller.icon)

    def test_all_tray_actions_are_dispatched_through_root_after(self):
        controller = self.controller()
        for key in self.callbacks:
            controller._dispatch(key)
        self.assertEqual(self.root.after.call_count, len(self.callbacks))
        for call, callback in zip(self.root.after.call_args_list, self.callbacks.values()):
            self.assertEqual(call.args, (0, callback))

    def test_destroyed_root_stops_future_dispatch(self):
        self.root.after.side_effect = RuntimeError("destroyed")
        controller = self.controller()
        controller._dispatch("restore")
        controller._dispatch("refresh")
        self.assertTrue(controller.closing)
        self.assertEqual(self.root.after.call_count, 1)

    def test_menu_has_default_left_click_restore_and_explicit_exit(self):
        with patch.dict(sys.modules, {"pystray": self.pystray}):
            menu = self.controller()._build_menu()
        items = [item for item in menu if isinstance(item, FakeMenuItem)]
        self.assertTrue(items[0].default)
        self.assertEqual(items[0].text, "打开主界面")
        self.assertEqual(items[-1].text, "退出应用")

    def test_menu_language_and_auto_refresh_state_update_in_place(self):
        with patch.dict(sys.modules, {"pystray": self.pystray}):
            controller = self.controller()
            controller.start()
            icon = controller.icon
            controller.update(language="en", auto_refresh_enabled=True)
        self.assertEqual(controller.state, TrayState("en", True))
        self.assertIn("Auto Refresh: On", [item.text for item in icon.menu if isinstance(item, FakeMenuItem)])
        self.assertEqual(icon.update_count, 1)

    def test_tray_failure_is_non_fatal(self):
        broken = types.SimpleNamespace(Icon=Mock(side_effect=RuntimeError("no tray")), Menu=FakeMenu, MenuItem=FakeMenuItem)
        with patch.dict(sys.modules, {"pystray": broken}):
            controller = self.controller()
            self.assertFalse(controller.start())
        self.assertFalse(controller.started)

    def test_controller_source_has_no_tk_window_or_mainloop(self):
        source = inspect.getsource(SystemTrayController)
        self.assertNotIn("CTk(", source)
        self.assertNotIn("CTkToplevel(", source)
        self.assertNotIn("mainloop(", source)

    def test_dashboard_creates_one_tray_and_explicit_exit_bypasses_prompt(self):
        source = inspect.getsource(Dashboard.__init__)
        self.assertEqual(source.count("SystemTrayController("), 1)
        self.assertIn("on_exit=self.close", source)

    def test_hide_restore_and_widget_switch_do_not_refresh(self):
        source = inspect.getsource(Dashboard.hide_to_tray) + inspect.getsource(Dashboard.restore_dashboard) + inspect.getsource(Dashboard._enter_widget_mode)
        self.assertNotIn(".refresh(", source)
        self.assertIn('self.window_mode = "tray"', source)
        self.assertIn('self.window_mode = "dashboard"', source)
        self.assertIn('self.window_mode = "widget"', source)

    def test_taskbar_and_tray_have_distinct_state(self):
        taskbar = inspect.getsource(Dashboard._minimize_to_taskbar)
        tray = inspect.getsource(Dashboard.hide_to_tray)
        self.assertIn('self.window_mode = "taskbar"', taskbar)
        self.assertIn('self.window_mode = "tray"', tray)
        self.assertIn("self.root.iconify()", taskbar)
        self.assertIn("self.root.withdraw()", tray)

    def test_close_stops_refresh_provider_tray_widget_and_root(self):
        source = inspect.getsource(Dashboard.close)
        for expected in ("auto_refresh.close()", "quota_provider.close()", "tray.stop()", "mini_widget.destroy()", "root.destroy()"):
            self.assertIn(expected, source)


if __name__ == "__main__":
    unittest.main()
