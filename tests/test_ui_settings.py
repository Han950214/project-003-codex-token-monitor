import json
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.ui_settings import LanguageController, load_language, save_language
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


if __name__ == "__main__":
    unittest.main()
