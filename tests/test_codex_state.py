import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.codex_state import load_latest_thread_total, load_thread_total


class CodexStateTests(unittest.TestCase):
    def _database(self, directory: str, include_preview: bool = False) -> Path:
        path = Path(directory) / "state.sqlite"
        preview_column = ", preview TEXT" if include_preview else ""
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                f"""
                CREATE TABLE threads (
                    id TEXT,
                    created_at INTEGER,
                    updated_at INTEGER,
                    model TEXT,
                    model_provider TEXT,
                    tokens_used INTEGER
                    {preview_column}
                )
                """
            )
            connection.commit()
        return path

    def test_reads_latest_non_null_total_and_safe_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            with closing(sqlite3.connect(path)) as connection:
                connection.executemany(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        ("old", 1, 2, "gpt-old", "openai", 100),
                        ("new", 3, 4, "gpt-new", "openai", 250),
                    ],
                )
                connection.commit()
            result = load_latest_thread_total(path)

        self.assertIsNotNone(result)
        self.assertEqual(result.thread_id, "new")
        self.assertEqual(result.total_tokens, 250)
        self.assertEqual(result.model, "gpt-new")

    def test_missing_database_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            result = load_latest_thread_total(Path(directory) / "missing.sqlite")
        self.assertIsNone(result)

    def test_missing_threads_table_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            with closing(sqlite3.connect(path)):
                pass
            result = load_latest_thread_total(path)
        self.assertIsNone(result)

    def test_null_tokens_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
                    ("thread", 1, 2, "gpt", "openai", None),
                )
                connection.commit()
            result = load_latest_thread_total(path)
        self.assertIsNone(result)

    def test_reads_requested_thread_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory)
            with closing(sqlite3.connect(path)) as connection:
                connection.executemany("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", [("wanted", 1, 1, "gpt", "openai", 99), ("newer", 2, 3, "gpt", "openai", 500)])
                connection.commit()
            result = load_thread_total("wanted", path)
        self.assertEqual(result.thread_id, "wanted")
        self.assertEqual(result.total_tokens, 99)

    def test_preview_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._database(directory, include_preview=True)
            secret = "FULL_PROMPT_OUTPUT_SECRET"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("thread", 1, 2, "gpt", "openai", 99, secret),
                )
                connection.execute(
                    """
                    CREATE TRIGGER block_preview_read
                    BEFORE UPDATE OF preview ON threads
                    BEGIN
                        SELECT RAISE(FAIL, 'preview accessed');
                    END
                    """
                )
                connection.commit()
            real_connect = sqlite3.connect

            def guarded_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)

                def deny_preview(action, _arg1, arg2, _database, _trigger):
                    if action == sqlite3.SQLITE_READ and arg2 == "preview":
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                connection.set_authorizer(deny_preview)
                return connection

            with patch("app.codex_state.sqlite3.connect", side_effect=guarded_connect):
                result = load_latest_thread_total(path)
        self.assertIsNotNone(result)
        self.assertNotIn(secret, repr(result))


if __name__ == "__main__":
    unittest.main()
