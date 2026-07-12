"""Read-only access to privacy-safe Codex thread metadata."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_CODEX_STATE_PATH = Path.home() / ".codex" / "state_5.sqlite"
CODEX_STATE_PATH_ENV = "CODEX_STATE_DB"
SAFE_BASE_COLUMNS = ("id", "created_at", "updated_at", "model", "model_provider", "tokens_used")


@dataclass(frozen=True)
class CodexThreadMetadata:
    thread_id: str
    created_at: int | None
    updated_at: int | None
    model: str | None
    model_provider: str | None
    total_tokens: int | None


@dataclass(frozen=True)
class CodexThreadTotal:
    thread_id: str
    created_at: int | None
    updated_at: int | None
    model: str | None
    model_provider: str | None
    total_tokens: int


def configured_state_path() -> Path:
    configured = os.environ.get(CODEX_STATE_PATH_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_CODEX_STATE_PATH


def load_thread_metadata(
    thread_ids: Sequence[str], path: Path | None = None
) -> dict[str, CodexThreadMetadata]:
    """Load safe metadata for all requested identifiers in one parameterized SELECT."""
    identifiers = tuple(dict.fromkeys(value for value in thread_ids if value))
    database = path or configured_state_path()
    if not identifiers or not database.is_file():
        return {}
    rows = _fetch_threads(database, identifiers)
    result: dict[str, CodexThreadMetadata] = {}
    for row in rows:
        total = _optional_int(row[5])
        if total is not None and total < 0:
            total = None
        item = CodexThreadMetadata(
            str(row[0]), _optional_int(row[1]), _optional_int(row[2]),
            _optional_text(row[3]), _optional_text(row[4]), total,
        )
        result[item.thread_id] = item
    return result


def load_latest_thread_total(path: Path | None = None) -> CodexThreadTotal | None:
    database = path or configured_state_path()
    if not database.is_file():
        return None
    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT id, created_at, updated_at, model, model_provider, tokens_used "
                "FROM threads WHERE tokens_used IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
            return _total_from_row(row)
    except (OSError, sqlite3.Error):
        return None


def load_thread_total(thread_id: str, path: Path | None = None) -> CodexThreadTotal | None:
    item = load_thread_metadata((thread_id,), path).get(thread_id)
    if item is None or item.total_tokens is None:
        return None
    return CodexThreadTotal(
        item.thread_id, item.created_at, item.updated_at, item.model,
        item.model_provider, item.total_tokens,
    )


def _fetch_threads(
    database: Path, thread_ids: tuple[str, ...]
) -> list[tuple[object, ...]]:
    uri = database.resolve().as_uri() + "?mode=ro"
    placeholders = ",".join("?" for _ in thread_ids)
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            schema = {str(row[1]) for row in connection.execute("PRAGMA table_info(threads)")}
            if not set(SAFE_BASE_COLUMNS).issubset(schema):
                return []
            columns = ", ".join(SAFE_BASE_COLUMNS)
            rows = connection.execute(
                f"SELECT {columns} FROM threads WHERE id IN ({placeholders})",
                thread_ids,
            ).fetchall()
            return rows
    except (OSError, sqlite3.Error):
        return []


def _total_from_row(row: tuple[object, ...] | None) -> CodexThreadTotal | None:
    if row is None:
        return None
    total = _optional_int(row[5])
    if total is None or total < 0:
        return None
    return CodexThreadTotal(
        str(row[0]), _optional_int(row[1]), _optional_int(row[2]),
        _optional_text(row[3]), _optional_text(row[4]), total,
    )


def _optional_int(value: object) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
