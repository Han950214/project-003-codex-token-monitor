import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.codex_logs import load_latest_completed_response_usage
from app.metrics import PricingConfig, RunUsage, summarize_runs
from app.reporting import render_report
from app.telemetry_bar import build_telemetry_values_from_summary


class CodexLogsTests(unittest.TestCase):
    def _database(self, directory: str) -> Path:
        path = Path(directory) / "logs.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                """
                CREATE TABLE logs (
                    id INTEGER,
                    ts INTEGER,
                    ts_nanos INTEGER,
                    level TEXT,
                    target TEXT,
                    feedback_log_body TEXT
                )
                """
            )
            connection.commit()
        return path

    def _insert_body(self, path: Path, ts_nanos: int, body: str) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?)",
                (ts_nanos, ts_nanos, ts_nanos, "INFO", "codex", body),
            )
            connection.commit()

    def test_reads_latest_completed_response_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":4,"reasoning_tokens":5}}',
            )
            self._insert_body(
                path,
                2,
                '{"type":"response.completed","usage":{"input_tokens":10,"output_tokens":20,"total_tokens":35,"cached_tokens":7,"reasoning_tokens":5}}',
            )
            usage = load_latest_completed_response_usage(path)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.input_tokens, 10)
        self.assertEqual(usage.output_tokens, 20)
        self.assertEqual(usage.total_tokens, 35)
        self.assertEqual(usage.cached_tokens, 7)
        self.assertEqual(usage.reasoning_tokens, 5)

    def test_does_not_return_body_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            secret = "FULL_PROMPT_OUTPUT_BODY_SECRET"
            self._insert_body(
                path,
                1,
                f'{{"type":"response.completed","secret":"{secret}","usage":{{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}}}}',
            )
            usage = load_latest_completed_response_usage(path)
        self.assertIsNotNone(usage)
        self.assertNotIn(secret, repr(usage))

    def test_missing_field_falls_back_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0}}',
            )
            self.assertIsNone(load_latest_completed_response_usage(path))

    def test_negative_value_falls_back_none(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":-1,"reasoning_tokens":0}}',
            )
            self.assertIsNone(load_latest_completed_response_usage(path))

    def test_missing_table_and_file_fall_back_none(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty.sqlite"
            with closing(sqlite3.connect(empty)):
                pass
            self.assertIsNone(load_latest_completed_response_usage(empty))
            self.assertIsNone(load_latest_completed_response_usage(Path(directory) / "missing.sqlite"))

    def test_source_labels_do_not_mark_cost_as_real_billing(self):
        summary = summarize_runs(
            [],
            PricingConfig(1, 0.1, 2),
            latest_response_usage=RunUsage(input_tokens=100, output_tokens=50, optional_log_tokens=5, observed_cached_input_tokens=20),
        )
        values = build_telemetry_values_from_summary(summary, PricingConfig(1, 0.1, 2))
        report = render_report([], summary)
        self.assertIn("codex_logs_sqlite / real usage", values[3][1])
        self.assertIn("not billing", values[4][1])
        self.assertNotIn("real billing", values[4][1])
        self.assertIn("derived from codex_logs_sqlite / real usage, not official cache hit rate", report)
        self.assertIn("not billing", report)

    def test_latest_usage_does_not_aggregate_session_or_thread(self):
        summary = summarize_runs(
            [],
            PricingConfig(1, 0.1, 2),
            real_total_tokens=999,
            latest_response_usage=RunUsage(input_tokens=100, output_tokens=50, optional_log_tokens=5, observed_cached_input_tokens=20),
        )
        self.assertEqual(summary.session_tokens, 999)
        self.assertEqual(summary.total_tokens_source, "codex_state_sqlite")
        self.assertEqual(summary.current_run_tokens, 155)
        self.assertEqual(summary.current_usage_source, "codex_logs_sqlite")

    def test_environment_path_is_supported_without_real_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}}',
            )
            with patch.dict("os.environ", {"CODEX_LOGS_DB": str(path)}):
                usage = load_latest_completed_response_usage()
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 3)


if __name__ == "__main__":
    unittest.main()
