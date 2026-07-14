import inspect
import unittest

import app.main as main


class ManualRunRemovalTests(unittest.TestCase):
    def test_manual_form_contract_is_not_exposed(self):
        self.assertFalse(hasattr(main, "MANUAL_FORM_FIELDS"))
        self.assertFalse(hasattr(main, "manual_form_position"))

    def test_dashboard_source_does_not_build_manual_run_ui(self):
        source = inspect.getsource(main.Dashboard)
        self.assertNotIn("_build_manual_input_page", source)
        self.assertNotIn("save_run", source)

    def test_dashboard_source_does_not_export_legacy_report(self):
        source = inspect.getsource(main)
        self.assertNotIn("from app.reporting import", source)
        self.assertNotIn("from app.storage import", source)

    def test_programmatic_session_highlight_does_not_pin_or_refresh(self):
        source = inspect.getsource(main.Dashboard._select_recent_row)
        self.assertIn("if self._rendering_sessions", source)
        self.assertIn("select_cached_thread", source)
        self.assertNotIn("refresh(", source)
        rendered = inspect.getsource(main.Dashboard._render_sessions_inner)
        self.assertIn("selection_set", rendered)
        self.assertIn("if selected_id in page_ids", rendered)

    def test_only_unavailable_rows_are_excluded_from_task_selector(self):
        source = inspect.getsource(main.Dashboard._render_sessions_inner)
        self.assertIn('row.status != "unavailable"', source)

    def test_time_range_selector_offers_seven_thirty_and_ninety_days(self):
        source = inspect.getsource(main.Dashboard._apply_language)
        self.assertIn('"last_7_days", "last_30_days", "last_90_days"', source)


if __name__ == "__main__":
    unittest.main()
