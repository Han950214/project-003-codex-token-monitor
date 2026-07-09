"""Read-only access to safe total-token metadata in Codex state SQLite."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CODEX_STATE_PATH = Path.home() / ".codex" / "state_5.sqlite"
CODEX_STATE_PATH_ENV = "CODEX_STATE_DB"


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


def load_latest_thread_total(path: Path | None = None) -> CodexThreadTotal | None:
    database = path or configured_state_path()
    if not database.is_file():
        return None

    try:
        uri = database.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            row = connection.execute(
                """
                SELECT id, created_at, updated_at, model, model_provider, tokens_used
                FROM threads
                WHERE tokens_used IS NOT NULL
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None

    if row is None:
        return None
    try:
        total_tokens = int(row[5])
    except (TypeError, ValueError, OverflowError):
        return None
    if total_tokens < 0:
        return None
    return CodexThreadTotal(
        thread_id=str(row[0]),
        created_at=_optional_int(row[1]),
        updated_at=_optional_int(row[2]),
        model=None if row[3] is None else str(row[3]),
        model_provider=None if row[4] is None else str(row[4]),
        total_tokens=total_tokens,
    )


def _optional_int(value: object) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError, OverflowError):
        return None
