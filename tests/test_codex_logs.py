import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.codex_logs import (
    LogsAdapterStatus,
    load_latest_completed_response_result,
    load_latest_completed_response_usage,
)
from app.metrics import PricingConfig, RunUsage, summarize_runs
from app.reporting import render_report
from app.telemetry_bar import (
    build_latest_response_values,
    build_logs_adapter_metadata,
    build_telemetry_values_from_summary,
)


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

    def test_structured_success_includes_values_source_and_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":{"input_tokens":10,"output_tokens":20,"total_tokens":35,"cached_tokens":7,"reasoning_tokens":5}}',
            )
            result = load_latest_completed_response_result(path)
        self.assertEqual(result.status, LogsAdapterStatus.CONNECTED)
        self.assertEqual(result.source, "codex_logs_sqlite / real usage")
        self.assertEqual(
            (result.usage.input_tokens, result.usage.output_tokens, result.usage.total_tokens, result.usage.cached_tokens, result.usage.reasoning_tokens),
            (10, 20, 35, 7, 5),
        )

    def test_response_completed_like_does_not_require_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                'event=response.completed usage={"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}',
            )
            usage = load_latest_completed_response_usage(path)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 3)

    def test_nested_usage_details_are_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":{"input_tokens":10,"input_tokens_details":{"cached_tokens":4},"output_tokens":3,"output_tokens_details":{"reasoning_tokens":2},"total_tokens":13}}',
            )
            result = load_latest_completed_response_result(path)
        self.assertEqual(result.status, LogsAdapterStatus.CONNECTED)
        self.assertEqual(result.usage.cached_tokens, 4)
        self.assertEqual(result.usage.reasoning_tokens, 2)

    def test_plain_path_fallback_after_uri_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}}',
            )
            real_connect = sqlite3.connect
            calls = []

            def flaky_connect(*args, **kwargs):
                calls.append((args, kwargs))
                if kwargs.get("uri"):
                    raise sqlite3.OperationalError("uri open failed")
                return real_connect(*args, **kwargs)

            with patch("app.codex_logs.sqlite3.connect", side_effect=flaky_connect):
                usage = load_latest_completed_response_usage(path)
        self.assertIsNotNone(usage)
        self.assertEqual(usage.total_tokens, 3)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0][1].get("uri"))
        self.assertEqual(calls[1][0][0], str(path))

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
            result = load_latest_completed_response_result(path)
            self.assertEqual(result.status, LogsAdapterStatus.PARSE_FAILED)
            self.assertTrue(
                all("unknown / source: unknown" in value for _, value in build_latest_response_values(result))
            )

    def test_invalid_usage_json_has_parse_failed_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                'event=response.completed usage={"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0',
            )
            result = load_latest_completed_response_result(path)
        self.assertEqual(result.status, LogsAdapterStatus.PARSE_FAILED)

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
            self.assertEqual(load_latest_completed_response_result(empty).status, LogsAdapterStatus.OPEN_FAILED)
            self.assertEqual(
                load_latest_completed_response_result(Path(directory) / "missing.sqlite").status,
                LogsAdapterStatus.DATABASE_MISSING,
            )

    def test_accessible_database_without_completed_response_has_status(self):
        with tempfile.TemporaryDirectory() as directory:
            result = load_latest_completed_response_result(self._database(directory))
        self.assertEqual(result.status, LogsAdapterStatus.NO_RESPONSE_COMPLETED)

    def test_both_open_attempts_failing_has_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            with patch("app.codex_logs.sqlite3.connect", side_effect=sqlite3.OperationalError("secret detail")) as connect:
                result = load_latest_completed_response_result(path)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(result.status, LogsAdapterStatus.OPEN_FAILED)
        self.assertNotIn("secret detail", repr(result))

    def test_derived_cache_hit_and_zero_input_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":{"input_tokens":100,"output_tokens":20,"total_tokens":120,"cached_tokens":25,"reasoning_tokens":0}}',
            )
            values = build_latest_response_values(load_latest_completed_response_result(path))
            self.assertIn("25.0%", values[5][1])
            self.assertIn("not official", values[5][1])

            zero_path = Path(directory) / "zero.sqlite"
            with closing(sqlite3.connect(zero_path)) as connection:
                connection.execute("CREATE TABLE logs (ts_nanos INTEGER, feedback_log_body TEXT)")
                connection.execute(
                    "INSERT INTO logs VALUES (?, ?)",
                    (1, '{"type":"response.completed","usage":{"input_tokens":0,"output_tokens":2,"total_tokens":2,"cached_tokens":0,"reasoning_tokens":0}}'),
                )
                connection.commit()
            zero_values = build_latest_response_values(load_latest_completed_response_result(zero_path))
        self.assertEqual(zero_values[5][1], "unknown / source: unknown")

    def test_metadata_uses_event_time_only_when_timestamp_is_reliable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1_800_000_000_000_000_000,
                '{"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}}',
            )
            result = load_latest_completed_response_result(path)
        metadata = build_logs_adapter_metadata(result)
        self.assertEqual(metadata[0][1], "connected")
        self.assertEqual(metadata[1][0], "Latest response at")

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
