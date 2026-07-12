import json
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.ui_settings import (
    LanguageController, load_language, load_startup_mode, save_language,
    save_startup_mode,
)
from app.startup_settings import StartupSettingsDialog
from app.main import Dashboard


class UiSettingsTests(unittest.TestCase):
    def test_missing_corrupt_and_unknown_settings_fall_back_to_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            self.assertEqual(load_language(path), "zh-CN")
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_language(path), "zh-CN")
            path.write_text(json.dumps({"language": "unknown"}), encoding="utf-8")
            self.assertEqual(load_language(path), "zh-CN")

    def test_language_preference_is_persisted_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data" / "ui-settings.json"
            self.assertTrue(save_language("en", path))
            self.assertEqual(load_language(path), "en")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"language": "en"})

    def test_language_switch_only_notifies_view_and_does_not_call_loader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            view_update = Mock()
            data_loader = Mock()
            controller = LanguageController(view_update, path)

            result = controller.set_language("en")

            self.assertEqual(result, "en")
            view_update.assert_called_once_with("en")
            data_loader.assert_not_called()

    def test_dashboard_language_callback_does_not_refresh_or_read_data(self):
        source = inspect.getsource(Dashboard._change_language) + inspect.getsource(Dashboard._apply_language)
        self.assertNotIn("view_model", source)
        self.assertNotIn(".refresh(", source)

    def test_startup_mode_defaults_and_invalid_values_fall_back_to_dashboard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            self.assertEqual(load_startup_mode(path), "dashboard")
            path.write_text('{"startup_mode":"broken"}', encoding="utf-8")
            self.assertEqual(load_startup_mode(path), "dashboard")

    def test_all_startup_modes_round_trip_without_losing_language(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            save_language("en", path)
            for mode in ("dashboard", "widget", "tray"):
                self.assertTrue(save_startup_mode(mode, path))
                self.assertEqual(load_startup_mode(path), mode)
                self.assertEqual(load_language(path), "en")

    def test_settings_dialog_uses_one_toplevel_and_no_root_or_mainloop(self):
        source = inspect.getsource(StartupSettingsDialog)
        self.assertEqual(source.count("ctk.CTkToplevel("), 1)
        self.assertNotIn("ctk.CTk(", source)
        self.assertNotIn("mainloop(", source)

    def test_settings_dialog_does_not_transient_to_a_hidden_root(self):
        source = inspect.getsource(StartupSettingsDialog.show)
        self.assertIn("if self.root.winfo_viewable()", source)
        self.assertIn("window.transient(self.root)", source)
        self.assertIn("window.after(20, present)", source)

    def test_opening_settings_does_not_refresh_data(self):
        self.assertNotIn("refresh", inspect.getsource(Dashboard.show_settings))


if __name__ == "__main__":
    unittest.main()
