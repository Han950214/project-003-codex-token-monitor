import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.codex_rollout import InstructionUsage, TokenUsage
from app.analytics_ui import metric_samples, trend_view_from_query
from app.dashboard import MiniThreadSnapshot
from app.history import (
    MAX_HISTORY_ROWS,
    RETENTION_DAYS,
    SCHEMA_VERSION,
    HistoryObservation,
    UsageHistoryStore,
)
from app.quota import CodexQuotaSnapshot, QuotaKind, QuotaWindow


NOW = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)


def quota(
    *,
    five_remaining: float = 80.0,
    weekly_remaining: float = 70.0,
    observed_at: datetime = NOW,
    source_status: str = "normal",
) -> CodexQuotaSnapshot:
    five = QuotaWindow(
        QuotaKind.FIVE_HOUR,
        100.0 - five_remaining,
        five_remaining,
        NOW + timedelta(hours=4),
        observed_at,
        "codex_app_server",
        True,
        source_status == "stale",
        "quota_refresh_failed" if source_status == "stale" else None,
    )
    weekly = QuotaWindow(
        QuotaKind.WEEKLY,
        100.0 - weekly_remaining,
        weekly_remaining,
        NOW + timedelta(days=5),
        observed_at,
        "codex_app_server",
        True,
        source_status == "stale",
        "quota_refresh_failed" if source_status == "stale" else None,
    )
    return CodexQuotaSnapshot(five, weekly, observed_at, source_status)


def observation(
    *,
    sampled_at: datetime = NOW,
    source_observed_at: datetime = NOW - timedelta(seconds=10),
    thread: str = "thread-1",
    total: int = 120,
    session_total: int = 999,
    turns: int = 3,
    five_remaining: float = 80.0,
    weekly_remaining: float = 70.0,
    quota_observed_at: datetime = NOW,
    source_status: str = "exact",
    source_available: bool = True,
    token_stale: bool = False,
) -> HistoryObservation:
    return HistoryObservation(
        sampled_at=sampled_at,
        source_observed_at=source_observed_at,
        quota_observed_at=quota_observed_at,
        thread_safe_id=thread,
        model_safe_id="gpt-5",
        source_status=source_status,
        source_available=source_available,
        token_stale=token_stale,
        token_stale_reason="source_stale" if token_stale else None,
        input_tokens=100,
        output_tokens=20,
        total_tokens=total,
        cached_tokens=40,
        reasoning_tokens=5,
        session_total_tokens=session_total,
        turn_count=turns,
        quota_source_status="normal",
        five_hour_observed_at=quota_observed_at,
        five_hour_last_seen_at=quota_observed_at,
        five_hour_used_percent=100.0 - five_remaining,
        five_hour_remaining_percent=five_remaining,
        five_hour_reset_at=NOW + timedelta(hours=4),
        five_hour_source="codex_app_server",
        five_hour_available=True,
        weekly_observed_at=quota_observed_at,
        weekly_last_seen_at=quota_observed_at,
        weekly_used_percent=100.0 - weekly_remaining,
        weekly_remaining_percent=weekly_remaining,
        weekly_reset_at=NOW + timedelta(days=5),
        weekly_source="codex_app_server",
        weekly_available=True,
    )


class HistorySchemaTests(unittest.TestCase):
    def test_constants_match_phase_contract(self):
        self.assertEqual((SCHEMA_VERSION, RETENTION_DAYS, MAX_HISTORY_ROWS), (3, 90, 200_000))

    def test_new_database_initializes_versioned_schema_and_unique_index(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = UsageHistoryStore(path, clock=lambda: NOW)
            self.assertTrue(store.initialize())
            with closing(sqlite3.connect(path)) as connection, connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(usage_history_samples)"
                    )
                }
                indexes = {
                    row[1] for row in connection.execute(
                        "PRAGMA index_list(usage_history_samples)"
                    )
                }
            self.assertIn("sample_fingerprint", columns)
            self.assertIn("five_hour_last_seen_at_utc", columns)
            self.assertIn("weekly_last_seen_at_utc", columns)
            self.assertIn("five_hour_event_seq", columns)
            self.assertIn("weekly_event_seq", columns)
            self.assertIn("ux_usage_history_samples_fingerprint", indexes)

    def test_existing_database_and_unrelated_data_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE existing_data(value TEXT)")
                connection.execute("INSERT INTO existing_data VALUES('keep')")
            store = UsageHistoryStore(path, clock=lambda: NOW)
            self.assertTrue(store.initialize())
            self.assertTrue(store.initialize())
            with closing(sqlite3.connect(path)) as connection, connection:
                value = connection.execute("SELECT value FROM existing_data").fetchone()[0]
            self.assertEqual(value, "keep")

    def test_partial_empty_schema_is_completed_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE usage_history_samples(id INTEGER PRIMARY KEY)"
                )
            store = UsageHistoryStore(path, clock=lambda: NOW)
            self.assertTrue(store.initialize())
            self.assertTrue(store.initialize())
            with closing(sqlite3.connect(path)) as connection, connection:
                columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(usage_history_samples)"
                    )
                }
            self.assertIn("weekly_remaining_percent", columns)
            self.assertIn("sample_fingerprint", columns)

    def test_v1_database_adds_window_times_and_preserves_existing_row(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            observed = NOW - timedelta(minutes=2)
            observed_text = observed.isoformat(timespec="microseconds").replace(
                "+00:00", "Z",
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE usage_history_samples("
                    "id INTEGER PRIMARY KEY, sampled_at_utc TEXT, "
                    "quota_observed_at_utc TEXT, five_hour_available INTEGER, "
                    "five_hour_stale INTEGER, weekly_available INTEGER, "
                    "weekly_stale INTEGER, total_tokens INTEGER)"
                )
                connection.execute(
                    "INSERT INTO usage_history_samples VALUES("
                    "1, ?, ?, 1, 0, 1, 0, 321)",
                    (observed_text, observed_text),
                )
                connection.execute("PRAGMA user_version=1")

            store = UsageHistoryStore(path, clock=lambda: NOW)
            self.assertTrue(store.initialize())
            self.assertTrue(store.initialize())

            with closing(sqlite3.connect(path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                row = connection.execute(
                    "SELECT total_tokens, five_hour_observed_at_utc, "
                    "five_hour_last_seen_at_utc, weekly_observed_at_utc, "
                    "weekly_last_seen_at_utc FROM usage_history_samples WHERE id=1"
                ).fetchone()
            self.assertEqual(version, 3)
            self.assertEqual(row, (321, observed_text, observed_text, observed_text, observed_text))

    def test_v1_stale_quota_row_is_preserved_without_invented_window_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            observed_text = NOW.isoformat(timespec="microseconds").replace(
                "+00:00", "Z",
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE usage_history_samples("
                    "id INTEGER PRIMARY KEY, sampled_at_utc TEXT, "
                    "quota_observed_at_utc TEXT, five_hour_available INTEGER, "
                    "five_hour_stale INTEGER, five_hour_remaining_percent REAL, "
                    "weekly_available INTEGER, weekly_stale INTEGER)"
                )
                connection.execute(
                    "INSERT INTO usage_history_samples VALUES("
                    "1, ?, ?, 1, 1, 50.0, 0, 0)",
                    (observed_text, observed_text),
                )
                connection.execute("PRAGMA user_version=1")

            store = UsageHistoryStore(path, clock=lambda: NOW)
            self.assertTrue(store.initialize())
            result = store.query(7, "thread-1", now=NOW)

            with closing(sqlite3.connect(path)) as connection:
                row = connection.execute(
                    "SELECT five_hour_observed_at_utc, "
                    "five_hour_remaining_percent FROM usage_history_samples WHERE id=1"
                ).fetchone()
            self.assertEqual(row, (None, 50.0))
            self.assertEqual(result.quota_samples, ())

    def test_v2_reliable_quota_rows_backfill_independent_event_sequences(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            first = NOW - timedelta(minutes=2)
            second = NOW - timedelta(minutes=1)
            iso = lambda value: value.isoformat(timespec="microseconds").replace(
                "+00:00", "Z",
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE usage_history_samples("
                    "id INTEGER PRIMARY KEY, sampled_at_utc TEXT, "
                    "quota_observed_at_utc TEXT, five_hour_observed_at_utc TEXT, "
                    "five_hour_last_seen_at_utc TEXT, five_hour_used_percent REAL, "
                    "five_hour_remaining_percent REAL, five_hour_reset_at_utc TEXT, "
                    "five_hour_source TEXT, five_hour_available INTEGER, "
                    "five_hour_stale INTEGER, weekly_observed_at_utc TEXT, "
                    "weekly_last_seen_at_utc TEXT, weekly_used_percent REAL, "
                    "weekly_remaining_percent REAL, weekly_reset_at_utc TEXT, "
                    "weekly_source TEXT, weekly_available INTEGER, weekly_stale INTEGER, "
                    "sample_fingerprint TEXT)"
                )
                reset = iso(NOW + timedelta(hours=4))
                for row_id, observed, last_seen, remaining in (
                    (1, first, NOW, 80.0),
                    (2, second, second, 60.0),
                ):
                    connection.execute(
                        "INSERT INTO usage_history_samples VALUES("
                        "?, ?, ?, ?, ?, ?, ?, ?, 'codex_app_server', 1, 0, "
                        "?, ?, 30.0, 70.0, ?, 'codex_app_server', 1, 0, ?)",
                        (
                            row_id, iso(observed), iso(observed), iso(observed),
                            iso(last_seen), 100.0 - remaining, remaining, reset,
                            iso(observed), iso(observed),
                            iso(NOW + timedelta(days=5)), f"v2-{row_id}",
                        ),
                    )
                connection.execute("PRAGMA user_version=2")

            store = UsageHistoryStore(path, clock=lambda: NOW)
            self.assertTrue(store.initialize())
            self.assertTrue(store.initialize())

            with closing(sqlite3.connect(path)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                rows = connection.execute(
                    "SELECT five_hour_event_seq, weekly_event_seq "
                    "FROM usage_history_samples ORDER BY id"
                ).fetchall()
                meta = dict(connection.execute(
                    "SELECT key, value FROM usage_history_meta "
                    "WHERE key LIKE 'quota_%_active_%_v3'"
                ))
            self.assertEqual(version, 3)
            self.assertEqual(rows, [(1, 1), (2, 1)])
            self.assertEqual(meta["quota_five_hour_active_seq_v3"], "3")
            self.assertEqual(meta["quota_weekly_active_seq_v3"], "1")

            returned = observation(quota_observed_at=NOW)
            self.assertTrue(store.record(returned))
            points = metric_samples(
                trend_view_from_query(store.query(7, "thread-1", now=NOW)),
                "five_hour",
            )
            self.assertEqual([value for _, value in points], [80.0, 60.0, 80.0])
            self.assertEqual(
                [sample.five_hour_event_seq for sample, _ in points], [1, 2, 3],
            )

    def test_partial_schema_rows_survive_migration_and_first_retention_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE usage_history_samples("
                    "id INTEGER PRIMARY KEY, total_tokens INTEGER)"
                )
                connection.execute(
                    "INSERT INTO usage_history_samples(id, total_tokens) VALUES(1, 321)"
                )
            store = UsageHistoryStore(path, clock=lambda: NOW)

            self.assertTrue(store.initialize())
            self.assertTrue(store.initialize())

            with closing(sqlite3.connect(path)) as connection, connection:
                row = connection.execute(
                    "SELECT total_tokens, sampled_at_utc, source_available, "
                    "sample_fingerprint, legacy_unknown_time "
                    "FROM usage_history_samples WHERE id=1"
                ).fetchone()
            self.assertEqual(row, (321, NOW.isoformat(timespec="microseconds").replace(
                "+00:00", "Z",
            ), 0, "legacy-0000000000000001", 1))

    def test_unknown_time_migration_rows_never_become_trend_points(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE usage_history_samples("
                    "id INTEGER PRIMARY KEY, sampled_at_utc TEXT, "
                    "thread_safe_id TEXT, "
                    "source_available INTEGER, source_status TEXT, "
                    "total_tokens INTEGER)"
                )
                connection.executemany(
                    "INSERT INTO usage_history_samples "
                    "VALUES(?, NULL, 'thread-1', 1, 'exact', ?)",
                    ((1, 100), (2, 200)),
                )
            store = UsageHistoryStore(path, clock=lambda: NOW)
            self.assertTrue(store.initialize())
            self.assertTrue(store.record(observation(
                sampled_at=NOW + timedelta(seconds=1),
                source_observed_at=NOW + timedelta(seconds=1),
                total=300,
            )))

            result = store.query(7, "thread-1", now=NOW + timedelta(seconds=1))
            values = [
                value for _, value in metric_samples(
                    trend_view_from_query(result), "total",
                )
            ]

            self.assertEqual(values, [300.0])
            self.assertEqual([sample.total_tokens for sample in result.samples], [300])
            self.assertFalse(any(sample.legacy_unknown_time for sample in result.samples))

    def test_initialize_enables_wal_for_nonblocking_readers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = UsageHistoryStore(path, clock=lambda: NOW)

            self.assertTrue(store.initialize())

            with closing(sqlite3.connect(path)) as connection:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")

    def test_query_does_not_wait_for_python_mutation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            store = UsageHistoryStore(Path(directory) / "history.sqlite3", clock=lambda: NOW)
            self.assertTrue(store.initialize())
            result: list[object] = []
            worker = threading.Thread(target=lambda: result.append(store.query(7, now=NOW)))

            store._lock.acquire()
            try:
                worker.start()
                worker.join(timeout=1.0)
                self.assertFalse(worker.is_alive())
            finally:
                store._lock.release()
                worker.join(timeout=1.0)
            self.assertEqual(len(result), 1)

    def test_failed_unique_index_migration_rolls_back_partial_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "CREATE TABLE usage_history_samples("
                    "id INTEGER PRIMARY KEY, sample_fingerprint TEXT)"
                )
                connection.executemany(
                    "INSERT INTO usage_history_samples(sample_fingerprint) VALUES(?)",
                    [("duplicate",), ("duplicate",)],
                )
            store = UsageHistoryStore(path, clock=lambda: NOW)
            self.assertFalse(store.initialize())
            self.assertEqual(store.last_error, "history_migration_failed")
            with closing(sqlite3.connect(path)) as connection, connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(usage_history_samples)"
                    )
                }
            self.assertEqual(version, 0)
            self.assertEqual(columns, {"id", "sample_fingerprint"})


class HistoryObservationTests(unittest.TestCase):
    def test_from_dashboard_uses_only_normalized_safe_numbers(self):
        usage = TokenUsage(100, 40, 20, 5, 120)
        instruction = InstructionUsage(
            "turn-1", "exact", usage, 1, 1000, 0, 0, 0, True, False,
        )
        selected = SimpleNamespace(
            thread_id="thread-1",
            instruction=instruction,
            thread_cumulative_usage=TokenUsage(900, 300, 99, 10, 999),
            observed_at=NOW - timedelta(seconds=5),
            status="exact",
            turn_count=4,
        )
        snapshot = SimpleNamespace(
            selected_session=selected,
            rollout=SimpleNamespace(
                instruction=instruction,
                thread_cumulative_usage=selected.thread_cumulative_usage,
                thread_id="thread-1",
                observed_at=selected.observed_at,
                turn_count=4,
            ),
            selected_thread_id="thread-1",
            state_metadata={"thread-1": SimpleNamespace(model="gpt-5")},
            state_total=None,
        )
        item = HistoryObservation.from_dashboard(snapshot, quota(), sampled_at=NOW)
        self.assertEqual(
            (item.input_tokens, item.cached_tokens, item.reasoning_tokens),
            (100, 40, 5),
        )
        self.assertEqual((item.session_total_tokens, item.turn_count), (999, 4))
        self.assertEqual(item.thread_safe_id, "thread-1")

    def test_from_mini_preserves_only_available_mini_numbers(self):
        mini = MiniThreadSnapshot(
            "content title is ignored", 120, 999, "exact", NOW, 4,
        )
        item = HistoryObservation.from_mini(
            mini, quota(), "thread-1", sampled_at=NOW,
        )
        self.assertEqual((item.total_tokens, item.session_total_tokens), (120, 999))
        self.assertIsNone(item.input_tokens)
        self.assertNotIn("content title", repr(item))

    def test_unsafe_identifiers_are_irreversibly_normalized(self):
        item = observation(thread=r"C:\Users\name\secret project")
        self.assertTrue(item.thread_safe_id.startswith("sha256:"))
        self.assertNotIn("secret", item.thread_safe_id)

    def test_dto_and_database_contract_exclude_content_fields(self):
        names = {field.name.lower() for field in fields(HistoryObservation)}
        forbidden = {
            "prompt", "response", "preview", "message", "tool_output",
            "reasoning_text", "cookie", "authorization", "session_secret",
            "rollout_path", "title", "project_content",
        }
        self.assertTrue(names.isdisjoint(forbidden))
        self.assertIn("reasoning_tokens", names)


class HistoryStoreTests(unittest.TestCase):
    def make_store(self, directory: str, **kwargs) -> UsageHistoryStore:
        return UsageHistoryStore(
            Path(directory) / "history.sqlite3", clock=lambda: NOW, **kwargs,
        )

    def test_same_observation_is_deduplicated_atomically_and_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.make_store(directory)
            item = observation()
            self.assertTrue(first.record(item))
            self.assertFalse(first.record(item))
            restarted = self.make_store(directory)
            self.assertFalse(restarted.record(item))
            self.assertEqual(restarted.query(7, "thread-1", now=NOW).sample_count, 1)

    def test_local_capture_and_quota_observation_times_do_not_change_fingerprint(self):
        first = observation()
        later = replace(
            first,
            sampled_at=NOW + timedelta(minutes=1),
            quota_observed_at=NOW + timedelta(minutes=1),
        )
        self.assertEqual(first.sample_fingerprint, later.sample_fingerprint)

    def test_token_and_quota_changes_create_independent_logical_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first = observation()
            token_change = replace(
                first,
                sampled_at=NOW + timedelta(seconds=1),
                source_observed_at=NOW,
                input_tokens=101,
                total_tokens=121,
            )
            quota_change = replace(
                token_change,
                sampled_at=NOW + timedelta(seconds=2),
                quota_observed_at=NOW + timedelta(seconds=2),
                five_hour_observed_at=NOW + timedelta(seconds=2),
                five_hour_last_seen_at=NOW + timedelta(seconds=2),
                five_hour_used_percent=25.0,
                five_hour_remaining_percent=75.0,
            )
            self.assertTrue(store.record(first))
            self.assertTrue(store.record(token_change))
            self.assertTrue(store.record(quota_change))
            result = store.query(7, "thread-1", now=NOW + timedelta(seconds=2))
            view = trend_view_from_query(result)
            self.assertEqual(result.sample_count, 2)
            self.assertEqual(len(metric_samples(view, "total")), 2)
            self.assertEqual(result.end_at, NOW)
            self.assertEqual(len(metric_samples(view, "five_hour")), 2)

    def test_quota_only_change_does_not_refresh_stale_token_or_end_time(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            token_time = NOW - timedelta(minutes=10)
            first = observation(
                source_observed_at=token_time,
                token_stale=True,
                quota_observed_at=NOW - timedelta(minutes=1),
            )
            quota_change = replace(
                first,
                sampled_at=NOW,
                quota_observed_at=NOW,
                five_hour_observed_at=NOW,
                five_hour_last_seen_at=NOW,
                five_hour_used_percent=25.0,
                five_hour_remaining_percent=75.0,
            )
            self.assertTrue(store.record(first))
            self.assertTrue(store.record(quota_change))

            result = store.query(7, "thread-1", now=NOW)

            self.assertEqual(result.sample_count, 1)
            self.assertEqual(result.end_at, token_time)
            self.assertEqual(result.status, "stale")
            self.assertEqual(len(metric_samples(trend_view_from_query(result), "total")), 1)

    def test_same_quota_success_updates_last_seen_without_new_row_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first_seen = NOW - timedelta(minutes=10)
            later_seen = NOW
            first = observation(
                quota_observed_at=first_seen,
            )
            first = replace(
                first,
                five_hour_observed_at=first_seen,
                five_hour_last_seen_at=first_seen,
                weekly_observed_at=first_seen,
                weekly_last_seen_at=first_seen,
            )
            repeated = replace(
                first,
                sampled_at=later_seen,
                quota_observed_at=later_seen,
                five_hour_observed_at=later_seen,
                five_hour_last_seen_at=later_seen,
                weekly_observed_at=later_seen,
                weekly_last_seen_at=later_seen,
            )
            self.assertTrue(store.record(first))
            self.assertFalse(store.record(repeated))

            result = store.query(7, "thread-1", now=later_seen)
            restarted = self.make_store(directory).query(7, "thread-1", now=later_seen)
            with closing(sqlite3.connect(store.path)) as connection:
                row_count = connection.execute(
                    "SELECT COUNT(*) FROM usage_history_samples"
                ).fetchone()[0]

            self.assertEqual(row_count, 1)
            self.assertEqual(len(metric_samples(trend_view_from_query(result), "five_hour")), 1)
            self.assertEqual(result.five_hour_last_seen_at, later_seen)
            self.assertEqual(restarted.five_hour_last_seen_at, later_seen)
            self.assertEqual(result.end_at, first.source_observed_at)

    def test_five_hour_a_b_a_is_three_events_but_weekly_is_one(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first_at = NOW - timedelta(minutes=2)
            middle_at = NOW - timedelta(minutes=1)
            first = observation(quota_observed_at=first_at)
            first = replace(
                first,
                five_hour_observed_at=first_at,
                five_hour_last_seen_at=first_at,
                weekly_observed_at=first_at,
                weekly_last_seen_at=first_at,
            )
            middle = replace(
                first,
                sampled_at=middle_at,
                quota_observed_at=middle_at,
                five_hour_observed_at=middle_at,
                five_hour_last_seen_at=middle_at,
                weekly_observed_at=middle_at,
                weekly_last_seen_at=middle_at,
                five_hour_used_percent=40.0,
                five_hour_remaining_percent=60.0,
            )
            returned = replace(
                first,
                sampled_at=NOW,
                quota_observed_at=NOW,
                five_hour_observed_at=NOW,
                five_hour_last_seen_at=NOW,
                weekly_observed_at=NOW,
                weekly_last_seen_at=NOW,
            )

            self.assertEqual(
                [store.record(item) for item in (first, middle, returned)],
                [True, True, True],
            )
            restarted = self.make_store(directory)
            heartbeat_at = NOW + timedelta(seconds=1)
            heartbeat = replace(
                returned,
                sampled_at=heartbeat_at,
                quota_observed_at=heartbeat_at,
                five_hour_observed_at=heartbeat_at,
                five_hour_last_seen_at=heartbeat_at,
                weekly_observed_at=heartbeat_at,
                weekly_last_seen_at=heartbeat_at,
            )
            self.assertFalse(restarted.record(heartbeat))
            result = restarted.query(7, "thread-1", now=heartbeat_at)
            view = trend_view_from_query(result)

            self.assertEqual(
                [value for _, value in metric_samples(view, "five_hour")],
                [80.0, 60.0, 80.0],
            )
            self.assertEqual(
                [value for _, value in metric_samples(view, "weekly")], [70.0],
            )
            with closing(sqlite3.connect(store.path)) as connection:
                rows = connection.execute(
                    "SELECT five_hour_event_seq, weekly_event_seq, "
                    "five_hour_last_seen_at_utc "
                    "FROM usage_history_samples ORDER BY id"
                ).fetchall()
            self.assertEqual(
                [(five_seq, weekly_seq) for five_seq, weekly_seq, _ in rows],
                [(1, 1), (2, 1), (3, 1)],
            )
            self.assertEqual(
                datetime.fromisoformat(rows[0][2].replace("Z", "+00:00")),
                first_at,
            )
            self.assertEqual(
                datetime.fromisoformat(rows[2][2].replace("Z", "+00:00")),
                heartbeat_at,
            )
            self.assertEqual(result.five_hour_last_seen_at, heartbeat_at)

    def test_weekly_a_b_a_is_independent_from_five_hour(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first_at = NOW - timedelta(minutes=2)
            middle_at = NOW - timedelta(minutes=1)
            first = observation(quota_observed_at=first_at)
            first = replace(
                first,
                five_hour_observed_at=first_at,
                five_hour_last_seen_at=first_at,
                weekly_observed_at=first_at,
                weekly_last_seen_at=first_at,
            )
            middle = replace(
                first,
                sampled_at=middle_at,
                quota_observed_at=middle_at,
                five_hour_observed_at=middle_at,
                five_hour_last_seen_at=middle_at,
                weekly_observed_at=middle_at,
                weekly_last_seen_at=middle_at,
                weekly_used_percent=50.0,
                weekly_remaining_percent=50.0,
            )
            returned = replace(
                first,
                sampled_at=NOW,
                quota_observed_at=NOW,
                five_hour_observed_at=NOW,
                five_hour_last_seen_at=NOW,
                weekly_observed_at=NOW,
                weekly_last_seen_at=NOW,
            )
            for item in (first, middle, returned):
                self.assertTrue(store.record(item))

            view = trend_view_from_query(store.query(7, "thread-1", now=NOW))

            self.assertEqual(
                [value for _, value in metric_samples(view, "weekly")],
                [70.0, 50.0, 70.0],
            )
            self.assertEqual(
                [value for _, value in metric_samples(view, "five_hour")], [80.0],
            )

    def test_both_windows_change_together_with_stable_independent_order(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            times = (
                NOW - timedelta(minutes=2),
                NOW - timedelta(minutes=1),
                NOW,
            )
            first = observation(quota_observed_at=times[0])
            first = replace(
                first,
                five_hour_observed_at=times[0],
                five_hour_last_seen_at=times[0],
                weekly_observed_at=times[0],
                weekly_last_seen_at=times[0],
            )
            middle = replace(
                first,
                sampled_at=times[1],
                quota_observed_at=times[1],
                five_hour_observed_at=times[1],
                five_hour_last_seen_at=times[1],
                five_hour_used_percent=40.0,
                five_hour_remaining_percent=60.0,
                weekly_observed_at=times[1],
                weekly_last_seen_at=times[1],
                weekly_used_percent=50.0,
                weekly_remaining_percent=50.0,
            )
            returned = replace(
                first,
                sampled_at=times[2],
                quota_observed_at=times[2],
                five_hour_observed_at=times[2],
                five_hour_last_seen_at=times[2],
                weekly_observed_at=times[2],
                weekly_last_seen_at=times[2],
            )
            for item in (first, middle, returned):
                self.assertTrue(store.record(item))

            view = trend_view_from_query(store.query(7, "thread-1", now=NOW))
            five = metric_samples(view, "five_hour")
            weekly = metric_samples(view, "weekly")

            self.assertEqual([value for _, value in five], [80.0, 60.0, 80.0])
            self.assertEqual([value for _, value in weekly], [70.0, 50.0, 70.0])
            self.assertEqual(
                [sample.five_hour_event_seq for sample, _ in five], [1, 2, 3],
            )
            self.assertEqual(
                [sample.weekly_event_seq for sample, _ in weekly], [1, 2, 3],
            )
            self.assertEqual(
                [sample.five_hour_observed_at for sample, _ in five], list(times),
            )
            self.assertEqual(
                [sample.weekly_observed_at for sample, _ in weekly], list(times),
            )

    def test_reset_and_source_changes_allocate_new_window_events(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first = observation(quota_observed_at=NOW - timedelta(minutes=2))
            first = replace(
                first,
                five_hour_observed_at=NOW - timedelta(minutes=2),
                five_hour_last_seen_at=NOW - timedelta(minutes=2),
            )
            reset_change = replace(
                first,
                sampled_at=NOW - timedelta(minutes=1),
                quota_observed_at=NOW - timedelta(minutes=1),
                five_hour_observed_at=NOW - timedelta(minutes=1),
                five_hour_last_seen_at=NOW - timedelta(minutes=1),
                five_hour_reset_at=NOW + timedelta(hours=5),
            )
            source_change = replace(
                reset_change,
                sampled_at=NOW,
                quota_observed_at=NOW,
                five_hour_observed_at=NOW,
                five_hour_last_seen_at=NOW,
                five_hour_source="other_safe_source",
            )
            for item in (first, reset_change, source_change):
                self.assertTrue(store.record(item))

            points = metric_samples(
                trend_view_from_query(store.query(7, "thread-1", now=NOW)),
                "five_hour",
            )

            self.assertEqual([value for _, value in points], [80.0, 80.0, 80.0])
            self.assertEqual(
                [sample.five_hour_event_seq for sample, _ in points], [1, 2, 3],
            )

    def test_stale_status_does_not_become_value_event_and_fresh_recovery_heartbeats(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first_at = NOW - timedelta(minutes=2)
            stale_at = NOW - timedelta(minutes=1)
            first = observation(quota_observed_at=first_at)
            first = replace(
                first,
                five_hour_observed_at=first_at,
                five_hour_last_seen_at=first_at,
                weekly_observed_at=first_at,
                weekly_last_seen_at=first_at,
            )
            stale = replace(
                first,
                sampled_at=stale_at,
                quota_observed_at=stale_at,
                quota_source_status="stale",
                five_hour_stale=True,
                five_hour_error_code="quota_refresh_failed",
                weekly_stale=True,
                weekly_error_code="quota_refresh_failed",
            )
            recovered = replace(
                first,
                sampled_at=NOW,
                quota_observed_at=NOW,
                five_hour_observed_at=NOW,
                five_hour_last_seen_at=NOW,
                weekly_observed_at=NOW,
                weekly_last_seen_at=NOW,
            )

            self.assertTrue(store.record(first))
            self.assertTrue(store.record(stale))
            self.assertFalse(store.record(recovered))
            result = store.query(7, "thread-1", now=NOW)

            self.assertEqual(
                [value for _, value in metric_samples(
                    trend_view_from_query(result), "five_hour",
                )],
                [80.0],
            )
            self.assertEqual(result.five_hour_last_seen_at, NOW)
            self.assertFalse(result.five_hour_stale)

    def test_meta_missing_recovers_active_loop_event_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first_at = NOW - timedelta(minutes=3)
            middle_at = NOW - timedelta(minutes=2)
            returned_at = NOW - timedelta(minutes=1)
            first = observation(quota_observed_at=first_at)
            first = replace(
                first,
                five_hour_observed_at=first_at,
                five_hour_last_seen_at=first_at,
                weekly_observed_at=first_at,
                weekly_last_seen_at=first_at,
            )
            middle = replace(
                first,
                sampled_at=middle_at,
                quota_observed_at=middle_at,
                five_hour_observed_at=middle_at,
                five_hour_last_seen_at=middle_at,
                weekly_observed_at=middle_at,
                weekly_last_seen_at=middle_at,
                five_hour_used_percent=40.0,
                five_hour_remaining_percent=60.0,
            )
            returned = replace(
                first,
                sampled_at=returned_at,
                quota_observed_at=returned_at,
                five_hour_observed_at=returned_at,
                five_hour_last_seen_at=returned_at,
                weekly_observed_at=returned_at,
                weekly_last_seen_at=returned_at,
            )
            for item in (first, middle, returned):
                self.assertTrue(store.record(item))
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "DELETE FROM usage_history_meta "
                    "WHERE key LIKE 'quota_%_active_%_v3'"
                )
            heartbeat = replace(
                returned,
                sampled_at=NOW,
                quota_observed_at=NOW,
                five_hour_observed_at=NOW,
                five_hour_last_seen_at=NOW,
                weekly_observed_at=NOW,
                weekly_last_seen_at=NOW,
            )

            restarted = self.make_store(directory)
            self.assertFalse(restarted.record(heartbeat))
            result = restarted.query(7, "thread-1", now=NOW)

            self.assertEqual(
                [value for _, value in metric_samples(
                    trend_view_from_query(result), "five_hour",
                )],
                [80.0, 60.0, 80.0],
            )
            self.assertEqual(result.five_hour_last_seen_at, NOW)
            with closing(sqlite3.connect(store.path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM usage_history_samples"
                    ).fetchone()[0],
                    3,
                )

    def test_concurrent_same_transition_allocates_one_event(self):
        with tempfile.TemporaryDirectory() as directory:
            first_store = self.make_store(directory)
            second_store = self.make_store(directory)
            first = observation(quota_observed_at=NOW - timedelta(minutes=1))
            first = replace(
                first,
                five_hour_observed_at=NOW - timedelta(minutes=1),
                five_hour_last_seen_at=NOW - timedelta(minutes=1),
                weekly_observed_at=NOW - timedelta(minutes=1),
                weekly_last_seen_at=NOW - timedelta(minutes=1),
            )
            self.assertTrue(first_store.record(first))
            self.assertTrue(second_store.initialize())
            changed = replace(
                first,
                sampled_at=NOW,
                quota_observed_at=NOW,
                five_hour_observed_at=NOW,
                five_hour_last_seen_at=NOW,
                weekly_observed_at=NOW,
                weekly_last_seen_at=NOW,
                five_hour_used_percent=40.0,
                five_hour_remaining_percent=60.0,
            )
            barrier = threading.Barrier(2)
            results: list[bool] = []

            def write(store: UsageHistoryStore) -> None:
                barrier.wait()
                results.append(store.record(changed))

            threads = [
                threading.Thread(target=write, args=(store,))
                for store in (first_store, second_store)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(sorted(results), [False, True])
            with closing(sqlite3.connect(first_store.path)) as connection:
                rows = connection.execute(
                    "SELECT five_hour_event_seq FROM usage_history_samples ORDER BY id"
                ).fetchall()
            self.assertEqual(rows, [(1,), (2,)])

    def test_failed_insert_rolls_back_active_meta_and_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first = observation(quota_observed_at=NOW - timedelta(minutes=1))
            first = replace(
                first,
                five_hour_observed_at=NOW - timedelta(minutes=1),
                five_hour_last_seen_at=NOW - timedelta(minutes=1),
                weekly_observed_at=NOW - timedelta(minutes=1),
                weekly_last_seen_at=NOW - timedelta(minutes=1),
            )
            self.assertTrue(store.record(first))
            changed = replace(
                first,
                sampled_at=NOW,
                quota_observed_at=NOW,
                five_hour_observed_at=NOW,
                five_hour_last_seen_at=NOW,
                weekly_observed_at=NOW,
                weekly_last_seen_at=NOW,
                five_hour_used_percent=40.0,
                five_hour_remaining_percent=60.0,
            )
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute(
                    "CREATE TRIGGER reject_history_insert BEFORE INSERT ON "
                    "usage_history_samples BEGIN SELECT RAISE(ABORT, 'reject'); END"
                )
            self.assertFalse(store.record(changed))
            with closing(sqlite3.connect(store.path)) as connection:
                failed_seq = connection.execute(
                    "SELECT value FROM usage_history_meta "
                    "WHERE key='quota_five_hour_active_seq_v3'"
                ).fetchone()[0]
            self.assertEqual(failed_seq, "1")
            with closing(sqlite3.connect(store.path)) as connection, connection:
                connection.execute("DROP TRIGGER reject_history_insert")

            self.assertTrue(store.record(changed))
            with closing(sqlite3.connect(store.path)) as connection:
                rows = connection.execute(
                    "SELECT five_hour_event_seq FROM usage_history_samples ORDER BY id"
                ).fetchall()
                active_seq = connection.execute(
                    "SELECT value FROM usage_history_meta "
                    "WHERE key='quota_five_hour_active_seq_v3'"
                ).fetchone()[0]
            self.assertEqual(rows, [(1,), (2,)])
            self.assertEqual(active_seq, "2")

    def test_quota_windows_project_independently_when_reliable_times_cross(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            middle = observation(
                sampled_at=NOW - timedelta(minutes=5),
                source_observed_at=NOW - timedelta(minutes=5),
                quota_observed_at=NOW - timedelta(minutes=5),
                five_remaining=60.0,
                weekly_remaining=80.0,
            )
            middle = replace(
                middle,
                five_hour_observed_at=NOW - timedelta(minutes=5),
                five_hour_last_seen_at=NOW - timedelta(minutes=5),
                weekly_observed_at=NOW - timedelta(minutes=5),
                weekly_last_seen_at=NOW - timedelta(minutes=5),
            )
            crossed = observation(
                sampled_at=NOW,
                source_observed_at=NOW,
                quota_observed_at=NOW,
                five_remaining=50.0,
                weekly_remaining=90.0,
            )
            crossed = replace(
                crossed,
                five_hour_observed_at=NOW,
                five_hour_last_seen_at=NOW,
                weekly_observed_at=NOW - timedelta(minutes=10),
                weekly_last_seen_at=NOW - timedelta(minutes=10),
            )
            self.assertTrue(store.record(middle))
            self.assertTrue(store.record(crossed))

            result = store.query(7, "thread-1", now=NOW)
            view = trend_view_from_query(result)
            five = metric_samples(view, "five_hour")
            weekly = metric_samples(view, "weekly")

            self.assertEqual(
                [(sample.five_hour_observed_at, value) for sample, value in five],
                [
                    (NOW - timedelta(minutes=5), 60.0),
                    (NOW, 50.0),
                ],
            )
            self.assertEqual(
                [(sample.weekly_observed_at, value) for sample, value in weekly],
                [
                    (NOW - timedelta(minutes=10), 90.0),
                    (NOW - timedelta(minutes=5), 80.0),
                ],
            )
            self.assertEqual(result.five_hour_last_seen_at, NOW)
            self.assertEqual(
                result.weekly_last_seen_at, NOW - timedelta(minutes=5),
            )

    def test_mini_and_dashboard_same_token_observation_project_once_with_full_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            dashboard = observation(source_observed_at=NOW - timedelta(seconds=20))
            mini = replace(
                dashboard,
                sampled_at=NOW + timedelta(seconds=1),
                source_type="mini",
                input_tokens=None,
                output_tokens=None,
                cached_tokens=None,
                reasoning_tokens=None,
            )
            self.assertTrue(store.record(mini))
            self.assertTrue(store.record(dashboard))

            result = store.query(7, "thread-1", now=NOW)

            self.assertEqual(result.sample_count, 1)
            self.assertEqual(result.samples[0].source_type, "dashboard")
            self.assertEqual(
                (
                    result.samples[0].input_tokens,
                    result.samples[0].output_tokens,
                    result.samples[0].cached_tokens,
                    result.samples[0].reasoning_tokens,
                ),
                (100, 20, 40, 5),
            )

    def test_different_reliable_token_times_remain_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            first = observation(source_observed_at=NOW - timedelta(seconds=20))
            second = replace(
                first,
                sampled_at=NOW + timedelta(seconds=1),
                source_observed_at=NOW - timedelta(seconds=10),
            )
            store.record(first)
            store.record(second)

            result = store.query(7, "thread-1", now=NOW)

            self.assertEqual(result.sample_count, 2)
            self.assertEqual(
                [item.source_observed_at for item in result.samples],
                [NOW - timedelta(seconds=20), NOW - timedelta(seconds=10)],
            )

    def test_thread_filter_does_not_mix_tokens_and_quota_is_global(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.record(observation(thread="thread-1"))
            store.record(replace(
                observation(thread="thread-2"),
                sampled_at=NOW + timedelta(seconds=1),
                source_observed_at=NOW,
                five_hour_used_percent=25.0,
                five_hour_remaining_percent=75.0,
            ))
            result = store.query(7, "thread-1", now=NOW)
            self.assertEqual({item.thread_safe_id for item in result.samples}, {"thread-1"})
            self.assertEqual(result.sample_count, 1)
            self.assertEqual(result.end_at, NOW - timedelta(seconds=10))
            self.assertEqual(len(result.quota_samples), 2)
            self.assertTrue(all(item.thread_safe_id is None for item in result.quota_samples))

    def test_token_range_uses_source_time_not_local_sample_time(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            locally_new_source_old = observation(
                sampled_at=NOW,
                source_observed_at=NOW - timedelta(days=8),
                total=111,
            )
            locally_old_source_new = observation(
                sampled_at=NOW - timedelta(days=8),
                source_observed_at=NOW - timedelta(days=1),
                total=222,
            )
            self.assertTrue(store.record(locally_new_source_old))
            self.assertTrue(store.record(locally_old_source_new))

            result = store.query(7, "thread-1", now=NOW)

            self.assertEqual(result.sample_count, 1)
            self.assertEqual(result.samples[0].total_tokens, 222)
            self.assertEqual(result.start_at, NOW - timedelta(days=1))
            self.assertEqual(result.end_at, NOW - timedelta(days=1))

    def test_range_boundaries_are_utc_and_supported_ranges_query(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = UsageHistoryStore(path, clock=lambda: NOW)
            store.initialize()
            at_boundary = observation(
                sampled_at=NOW - timedelta(days=7),
                source_observed_at=NOW - timedelta(days=7),
            )
            before_boundary = replace(
                at_boundary,
                sampled_at=at_boundary.sampled_at - timedelta(microseconds=1),
                source_observed_at=at_boundary.source_observed_at - timedelta(microseconds=1),
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                for item in (at_boundary, before_boundary):
                    connection.execute(
                        "INSERT INTO usage_history_samples("
                        "schema_version, sampled_at_utc, source_observed_at_utc, "
                        "source_type, source_status, source_available, token_stale, "
                        "quota_source_status, five_hour_source, five_hour_available, "
                        "five_hour_stale, weekly_source, weekly_available, weekly_stale, "
                        "is_derived, sample_fingerprint) "
                        "VALUES(?, ?, ?, 'dashboard', 'exact', 1, 0, 'normal', "
                        "'unknown', 0, 0, 'unknown', 0, 0, 0, ?)",
                        (
                            1,
                            item.sampled_at.isoformat(timespec="microseconds").replace(
                                "+00:00", "Z"
                            ),
                            item.source_observed_at.isoformat(timespec="microseconds").replace(
                                "+00:00", "Z"
                            ),
                            item.sample_fingerprint,
                        ),
                    )
            self.assertEqual(store.query(7, now=NOW).sample_count, 1)
            self.assertEqual(store.query(30, now=NOW).sample_count, 2)
            self.assertEqual(store.query(90, now=NOW).sample_count, 2)
            with self.assertRaisesRegex(ValueError, "unsupported_history_range"):
                store.query(14, now=NOW)

    def test_empty_insufficient_available_stale_and_unavailable_states(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            self.assertEqual(store.query(7, "thread-1", now=NOW).status, "empty")
            store.record(observation())
            self.assertEqual(store.query(7, "thread-1", now=NOW).status, "insufficient")
            store.record(replace(
                observation(), sampled_at=NOW + timedelta(seconds=1),
                source_observed_at=NOW, total_tokens=121, input_tokens=101,
            ))
            self.assertEqual(store.query(7, "thread-1", now=NOW).status, "available")
            store.record(replace(
                observation(), sampled_at=NOW + timedelta(seconds=2),
                source_observed_at=NOW + timedelta(seconds=1),
                token_stale=True, token_stale_reason="source_stale",
            ))
            self.assertEqual(store.query(7, "thread-1", now=NOW).status, "stale")
            store.record(replace(
                observation(), sampled_at=NOW + timedelta(seconds=3),
                source_observed_at=NOW + timedelta(seconds=2),
                source_status="unavailable", source_available=False,
            ))
            self.assertEqual(store.query(7, "thread-1", now=NOW).status, "unavailable")

    def test_missing_thread_has_an_explicit_empty_result(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.record(observation(thread="thread-1"))

            result = store.query(7, "no_selection", now=NOW)

            self.assertEqual((result.status, result.sample_count, result.samples), (
                "empty", 0, (),
            ))

    def test_locked_database_fails_closed_without_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = UsageHistoryStore(path, clock=lambda: NOW)
            self.assertTrue(store.initialize())
            with closing(sqlite3.connect(path, timeout=0.1)) as blocker:
                blocker.execute("BEGIN EXCLUSIVE")

                self.assertFalse(store.record(observation()))

            self.assertEqual(store.last_error, "history_storage_locked")

    def test_query_open_failure_returns_unavailable(self):
        class BrokenQueryStore(UsageHistoryStore):
            def _connect(self):
                raise sqlite3.OperationalError("unable to open database file")

        with tempfile.TemporaryDirectory() as directory:
            store = BrokenQueryStore(Path(directory) / "history.sqlite3", clock=lambda: NOW)
            store._initialized = True

            result = store.query(7, "thread-1", now=NOW)

            self.assertEqual((result.status, result.error_code), (
                "unavailable", "history_storage_open_failed",
            ))

    def test_invalid_stored_row_returns_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = UsageHistoryStore(path, clock=lambda: NOW)
            self.assertTrue(store.initialize())
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "INSERT INTO usage_history_samples("
                    "sampled_at_utc, sample_fingerprint) VALUES(?, ?)",
                    ("not-a-time", "invalid-row"),
                )

            result = store.query(7, "thread-1", now=NOW)

            self.assertEqual((result.status, result.error_code), (
                "unavailable", "history_storage_invalid",
            ))

    def test_last_reliable_sample_age_marks_history_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.record(replace(
                observation(),
                sampled_at=NOW - timedelta(minutes=4),
                source_observed_at=NOW - timedelta(minutes=4),
            ))
            store.record(replace(
                observation(),
                sampled_at=NOW - timedelta(minutes=3, seconds=30),
                source_observed_at=NOW - timedelta(minutes=3, seconds=30),
                total_tokens=121,
                input_tokens=101,
            ))

            self.assertEqual(store.query(7, "thread-1", now=NOW).status, "stale")

    def test_stale_quota_does_not_mark_fresh_thread_tokens_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            store.record(observation())
            store.record(replace(
                observation(),
                sampled_at=NOW + timedelta(seconds=1),
                source_observed_at=NOW,
                total_tokens=121,
                quota_source_status="stale",
                five_hour_stale=True,
                weekly_stale=True,
            ))

            result = store.query(7, "thread-1", now=NOW + timedelta(seconds=1))

            self.assertEqual(result.status, "available")
            self.assertTrue(result.quota_samples[-1].five_hour_stale)

    def test_retention_and_capacity_delete_only_history_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            store = UsageHistoryStore(
                path, retention_days=90, max_rows=3, clock=lambda: NOW,
            )
            store.initialize()
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE other_business_data(value TEXT)")
                connection.execute("INSERT INTO other_business_data VALUES('keep')")
            for index in range(5):
                store.record(observation(
                    sampled_at=NOW + timedelta(seconds=index),
                    source_observed_at=NOW + timedelta(seconds=index),
                    total=120 + index,
                ))
            old = observation(
                sampled_at=NOW - timedelta(days=91),
                source_observed_at=NOW - timedelta(days=91),
                total=1,
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "INSERT INTO usage_history_samples("
                    "schema_version, sampled_at_utc, source_type, source_status, "
                    "source_available, token_stale, quota_source_status, "
                    "five_hour_source, five_hour_available, five_hour_stale, "
                    "weekly_source, weekly_available, weekly_stale, is_derived, "
                    "sample_fingerprint) VALUES(1, ?, 'dashboard', 'exact', 1, 0, "
                    "'normal', 'unknown', 0, 0, 'unknown', 0, 0, 0, ?)",
                    (
                        old.sampled_at.isoformat(timespec="microseconds").replace(
                            "+00:00", "Z"
                        ),
                        old.sample_fingerprint,
                    ),
                )
            self.assertGreaterEqual(store.prune(now=NOW), 1)
            with closing(sqlite3.connect(path)) as connection, connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM usage_history_samples"
                ).fetchone()[0]
                other = connection.execute(
                    "SELECT value FROM other_business_data"
                ).fetchone()[0]
            self.assertEqual(count, 3)
            self.assertEqual(other, "keep")


if __name__ == "__main__":
    unittest.main()
