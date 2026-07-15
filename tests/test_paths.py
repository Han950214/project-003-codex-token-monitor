import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import paths
from app.ui_settings import load_language


class RuntimePathsTests(unittest.TestCase):
    def test_source_resources_use_repository(self):
        self.assertEqual(paths.resource_root(frozen=False), paths.repository_root())
        self.assertEqual(
            paths.pricing_path(frozen=False),
            paths.repository_root() / "resources" / "pricing-config.sample.json",
        )

    def test_frozen_resources_use_bundle_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(paths.resource_root(frozen=True, bundle_root=root), root)
            self.assertEqual(paths.pricing_path(frozen=True, bundle_root=root).parent, root / "resources")

    def test_frozen_data_defaults_to_local_appdata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = paths.writable_root(frozen=True, environ={}, local_appdata=directory)
            self.assertEqual(root, Path(directory).resolve() / "CodexTokenMonitor")

    def test_history_defaults_to_user_data_even_in_source_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = paths.history_db_path(
                frozen=False, environ={}, local_appdata=directory,
            )
            expected = (
                Path(directory).resolve()
                / "CodexTokenMonitor" / "data" / "usage-history.sqlite3"
            )
            self.assertEqual(path, expected)
            self.assertFalse(path.is_relative_to(paths.repository_root()))

    def test_data_directory_override_controls_only_writable_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {paths.DATA_DIR_ENV: directory}
            root = Path(directory).resolve()
            self.assertEqual(paths.writable_root(frozen=True, environ=env), root)
            self.assertEqual(paths.runs_path(frozen=True, environ=env), root / "data" / "runs.json")
            self.assertEqual(paths.ui_settings_path(frozen=True, environ=env), root / "data" / "ui-settings.json")
            self.assertEqual(paths.history_db_path(frozen=False, environ=env), root / "data" / "usage-history.sqlite3")
            self.assertEqual(paths.reports_dir(frozen=True, environ=env), root / "reports")
            self.assertNotEqual(paths.pricing_path(frozen=False).parent, root)

    def test_writable_paths_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {paths.DATA_DIR_ENV: directory}
            writable = {
                paths.runs_path(environ=env),
                paths.ui_settings_path(environ=env),
                paths.reports_dir(environ=env),
            }
            self.assertEqual(len(writable), 3)

    def test_corrupt_settings_still_fall_back_to_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ui-settings.json"
            self.assertEqual(load_language(path), "zh-CN")
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_language(path), "zh-CN")

    def test_smoke_dispatch_does_not_build_tk_window(self):
        from app import main

        with patch.object(main, "smoke") as smoke, patch.object(main, "build_dashboard") as build:
            with patch("sys.argv", ["CodexTokenMonitor.exe", "--smoke"]):
                main.main()
        smoke.assert_called_once_with()
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
