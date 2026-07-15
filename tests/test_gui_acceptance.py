from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.paths import DATA_DIR_ENV
from scripts.gui_acceptance import GEOMETRIES, PAGES, RANGES, SCALES, _isolated_data_root


class GuiAcceptanceLauncherTests(unittest.TestCase):
    def test_required_geometry_and_scale_matrix_is_explicit(self):
        self.assertEqual(GEOMETRIES, ("980x660", "1440x900"))
        self.assertEqual(SCALES, (1.0, 1.25, 1.5))
        self.assertEqual(PAGES, ("overview", "usage_trends", "recommendations"))
        self.assertEqual(RANGES, (7, 30, 90))

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


if __name__ == "__main__":
    unittest.main()
