import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app.codex_logs as codex_logs
from app.codex_logs import (
    CodexLogsReader,
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
        ts, nanos = divmod(ts_nanos, 1_000_000_000)
        self._insert_log(path, ts_nanos, ts, nanos, body)

    def _insert_log(self, path: Path, row_id: int, ts: int, ts_nanos: int, body: str) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "INSERT INTO logs VALUES (?, ?, ?, ?, ?, ?)",
                (row_id, ts, ts_nanos, "INFO", "codex", body),
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

    def test_reads_confirmed_sse_event_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '  SSE event: {"type":"response.completed","response":{"usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}}}  ',
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

    def test_sql_returns_only_scalars_and_never_passes_payload_to_python_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            secrets = ("PROMPT_SECRET_BEFORE", "OUTPUT_SECRET_AFTER", "CONTENT_SECRET_ADJACENT")
            self._insert_body(
                path,
                1,
                (
                    'SSE event: {"type":"response.completed","prompt":"PROMPT_SECRET_BEFORE",'
                    '"content":"CONTENT_SECRET_ADJACENT","response":{"usage":{"input_tokens":10,'
                    '"output_tokens":20,"total_tokens":30,"cached_tokens":4,'
                    '"reasoning_tokens":2}},"output":"OUTPUT_SECRET_AFTER"}'
                ),
            )
            with patch(
                "app.codex_logs._usage_values_from_row",
                wraps=codex_logs._usage_values_from_row,
            ) as parser:
                result = load_latest_completed_response_result(path)

        self.assertEqual(result.status, LogsAdapterStatus.CONNECTED)
        parser.assert_called_once()
        returned_row = parser.call_args.args[0]
        self.assertEqual(len(returned_row), 14)
        self.assertTrue(all(not isinstance(value, str) for value in returned_row))
        for secret in secrets:
            self.assertNotIn(secret, repr(returned_row))
            self.assertNotIn(secret, repr(result))

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

    def test_invalid_usage_object_has_parse_failed_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":"invalid usage object"}',
            )
            result = load_latest_completed_response_result(path)
        self.assertEqual(result.status, LogsAdapterStatus.PARSE_FAILED)

    def test_content_collision_is_not_a_structural_completed_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            fake_usage = '{"input_tokens":91,"output_tokens":92,"total_tokens":183,"cached_tokens":9,"reasoning_tokens":8}'
            self._insert_body(
                path,
                1,
                '{"type":"message.created","content":"response.completed ' + fake_usage.replace('"', '\\"') + '"}',
            )
            result = load_latest_completed_response_result(path)
        self.assertEqual(result.status, LogsAdapterStatus.NO_RESPONSE_COMPLETED)

    def test_unanchored_plain_text_collision_is_not_connected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                'prompt prefix event=response.completed usage={"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}',
            )
            result = load_latest_completed_response_result(path)
        self.assertEqual(result.status, LogsAdapterStatus.NO_RESPONSE_COMPLETED)

    def test_arbitrary_prefix_before_sse_anchor_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                'content prefix SSE event: {"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}}',
            )
            result = load_latest_completed_response_result(path)
        self.assertEqual(result.status, LogsAdapterStatus.NO_RESPONSE_COMPLETED)

    def test_root_usage_wins_over_content_fake_usage_and_json_syntax_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                (
                    '{"type":"response.completed",'
                    '"content":"fake usage={\\"input_tokens\\":999} brace } quote \\\"",'
                    '"usage":{"input_tokens":10,"output_tokens":20,"total_tokens":30,'
                    '"cached_tokens":4,"reasoning_tokens":2}}'
                ),
            )
            result = load_latest_completed_response_result(path)
        self.assertEqual(result.status, LogsAdapterStatus.CONNECTED)
        self.assertEqual(result.usage.input_tokens, 10)
        self.assertEqual(result.usage.cached_tokens, 4)

    def test_latest_structural_event_parse_failure_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}}',
            )
            self._insert_body(
                path,
                2,
                '{"type":"response.completed","usage":{"input_tokens":9}}',
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
        self.assertEqual(metadata[2][0], "Refreshed at")

    def test_every_logs_connection_enables_query_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_body(
                path,
                1,
                '{"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}}',
            )
            real_connect = sqlite3.connect
            statements = []

            class TrackingConnection:
                def __init__(self, connection):
                    self.connection = connection

                def execute(self, statement, parameters=()):
                    statements.append(statement.strip())
                    return self.connection.execute(statement, parameters)

                def close(self):
                    self.connection.close()

            def tracked_connect(*args, **kwargs):
                return TrackingConnection(real_connect(*args, **kwargs))

            with patch("app.codex_logs.sqlite3.connect", side_effect=tracked_connect):
                result = load_latest_completed_response_result(path)

        self.assertEqual(result.status, LogsAdapterStatus.CONNECTED)
        self.assertEqual(statements[0], "PRAGMA query_only=ON")

    def test_incremental_reader_initializes_then_uses_cursor_and_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_log(
                path,
                1,
                1_800_000_000,
                1,
                '{"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}}',
            )
            reader = CodexLogsReader(batch_limit=2)
            with patch("app.codex_logs._fetch_scan", wraps=codex_logs._fetch_scan) as scan:
                first = reader.refresh(path)
                first_cursor = reader.cursor
                for row_id in (2, 3, 4):
                    self._insert_log(
                        path,
                        row_id,
                        1_800_000_000,
                        row_id,
                        '{"type":"message.created"}',
                    )
                second = reader.refresh(path)

        self.assertEqual(first.status, LogsAdapterStatus.CONNECTED)
        self.assertTrue(first.incremental_reader_initialized)
        self.assertIsNone(scan.call_args_list[0].args[1])
        self.assertEqual(scan.call_args_list[1].args[1], first_cursor)
        self.assertEqual(scan.call_args_list[1].args[2], 2)
        self.assertEqual(reader.cursor.row_id, 3)
        self.assertEqual(second.usage, first.usage)
        self.assertFalse(second.new_event_found)

    def test_no_new_rows_preserve_usage_and_event_time_but_refresh_time_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_log(
                path,
                1,
                1_800_000_000,
                10,
                '{"type":"response.completed","usage":{"input_tokens":10,"output_tokens":2,"total_tokens":12,"cached_tokens":4,"reasoning_tokens":1}}',
            )
            reader = CodexLogsReader()
            first_now = datetime(2026, 7, 11, tzinfo=timezone.utc)
            first = reader.refresh(path, first_now)
            second = reader.refresh(path, first_now + timedelta(minutes=1))

        self.assertEqual(second.usage, first.usage)
        self.assertEqual(second.observed_at, first.observed_at)
        self.assertEqual(second.refreshed_at, first_now + timedelta(minutes=1))
        self.assertFalse(second.new_event_found)

    def test_incremental_valid_and_invalid_events_update_without_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_log(
                path,
                1,
                1_800_000_000,
                1,
                '{"type":"response.completed","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3,"cached_tokens":0,"reasoning_tokens":0}}',
            )
            reader = CodexLogsReader()
            initial = reader.refresh(path)
            self._insert_log(
                path,
                2,
                1_800_000_001,
                2,
                '{"type":"response.completed","usage":{"input_tokens":20,"output_tokens":3,"total_tokens":23,"cached_tokens":5,"reasoning_tokens":1}}',
            )
            updated = reader.refresh(path)
            self._insert_log(
                path,
                3,
                1_800_000_002,
                3,
                '{"type":"response.completed","usage":{"input_tokens":99}}',
            )
            invalid = reader.refresh(path)

        self.assertEqual(updated.status, LogsAdapterStatus.CONNECTED)
        self.assertEqual(updated.usage.input_tokens, 20)
        self.assertTrue(updated.new_event_found)
        self.assertGreater(updated.observed_at, initial.observed_at)
        self.assertEqual(invalid.status, LogsAdapterStatus.PARSE_FAILED)
        self.assertIsNone(invalid.usage)

    def test_same_timestamp_rows_use_id_as_stable_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_log(path, 1, 100, 5, '{"type":"message.created"}')
            reader = CodexLogsReader()
            reader.refresh(path)
            self._insert_log(
                path,
                2,
                100,
                5,
                '{"type":"response.completed","usage":{"input_tokens":7,"output_tokens":2,"total_tokens":9,"cached_tokens":1,"reasoning_tokens":0}}',
            )
            result = reader.refresh(path)

        self.assertEqual(result.status, LogsAdapterStatus.CONNECTED)
        self.assertEqual(result.usage.input_tokens, 7)
        self.assertEqual(reader.cursor.row_id, 2)

    def test_truncated_database_reinitializes_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_log(
                path,
                100,
                100,
                0,
                '{"type":"response.completed","usage":{"input_tokens":100,"output_tokens":2,"total_tokens":102,"cached_tokens":1,"reasoning_tokens":0}}',
            )
            reader = CodexLogsReader()
            reader.refresh(path)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("DELETE FROM logs")
                connection.commit()
            self._insert_log(
                path,
                1,
                1,
                0,
                '{"type":"response.completed","usage":{"input_tokens":5,"output_tokens":1,"total_tokens":6,"cached_tokens":0,"reasoning_tokens":0}}',
            )
            result = reader.refresh(path)

        self.assertEqual(result.status, LogsAdapterStatus.CONNECTED)
        self.assertEqual(result.usage.input_tokens, 5)
        self.assertEqual(reader.cursor.row_id, 1)

    def test_replaced_database_discards_previous_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            self._insert_log(
                path,
                1,
                100,
                0,
                '{"type":"response.completed","usage":{"input_tokens":10,"output_tokens":1,"total_tokens":11,"cached_tokens":0,"reasoning_tokens":0}}',
            )
            reader = CodexLogsReader()
            reader.refresh(path)

            replacement_directory = Path(directory) / "replacement"
            replacement_directory.mkdir()
            replacement = self._database(str(replacement_directory))
            self._insert_log(replacement, 200, 200, 0, '{"type":"message.created"}')
            replacement.replace(path)
            result = reader.refresh(path)

        self.assertEqual(result.status, LogsAdapterStatus.NO_RESPONSE_COMPLETED)
        self.assertIsNone(result.usage)
        self.assertEqual(reader.cursor.row_id, 200)

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
