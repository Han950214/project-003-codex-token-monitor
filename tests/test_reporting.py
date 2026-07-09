import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.metrics import PricingConfig, summarize_runs
from app.reporting import export_report, render_report
from tests.test_storage import sample_run


class ReportingTests(unittest.TestCase):
    def test_report_contains_local_estimate_labels(self):
        runs = [sample_run()]
        summary = summarize_runs(runs, PricingConfig(1, 0.1, 2))
        report = render_report(runs, summary, datetime(2026, 7, 8, 9, 0, 0))
        self.assertIn("本地估算 / local estimate", report)
        self.assertIn("Session tokens", report)
        self.assertIn("Current cache hit", report)

    def test_report_uses_summaries_not_full_prompt_or_output(self):
        runs = [sample_run()]
        summary = summarize_runs(runs, PricingConfig(1, 0.1, 2))
        report = render_report(runs, summary)
        self.assertIn("Short prompt summary", report)
        self.assertIn("Short output summary", report)
        self.assertNotIn("FULL_PROMPT_SECRET", report)
        self.assertNotIn("FULL_OUTPUT_SECRET", report)

    def test_export_report_writes_markdown(self):
        runs = [sample_run()]
        summary = summarize_runs(runs, PricingConfig(1, 0.1, 2))
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.md"
            written = export_report(runs, summary, path, datetime(2026, 7, 8, 9, 0, 0))
            text = written.read_text(encoding="utf-8")
        self.assertEqual(written, path)
        self.assertIn("Codex Token Waste Report", text)


if __name__ == "__main__":
    unittest.main()
