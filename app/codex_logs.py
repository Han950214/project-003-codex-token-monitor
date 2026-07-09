"""Read-only latest response.completed usage metadata from Codex logs SQLite."""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CODEX_LOGS_PATH = Path.home() / ".codex" / "logs_2.sqlite"
CODEX_LOGS_PATH_ENV = "CODEX_LOGS_DB"
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
)
_USAGE_PATTERNS = {
    field: re.compile(rf'"{field}"\s*:\s*(-?\d+)\b')
    for field in USAGE_FIELDS
}


@dataclass(frozen=True)
class CodexResponseUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int


def configured_logs_path() -> Path:
    configured = os.environ.get(CODEX_LOGS_PATH_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_CODEX_LOGS_PATH


def load_latest_completed_response_usage(path: Path | None = None) -> CodexResponseUsage | None:
    database = path or configured_logs_path()
    if not database.is_file():
        return None

    try:
        uri = database.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            row = connection.execute(
                """
                SELECT feedback_log_body
                FROM logs
                WHERE feedback_log_body LIKE '%"response.completed"%'
                ORDER BY ts_nanos DESC
                LIMIT 1
                """
            ).fetchone()
    except (OSError, sqlite3.Error):
        return None

    if row is None or not isinstance(row[0], str):
        return None
    values = _extract_usage_values(row[0])
    if values is None:
        return None
    return CodexResponseUsage(**values)


def _extract_usage_values(body: str) -> dict[str, int] | None:
    values: dict[str, int] = {}
    for field, pattern in _USAGE_PATTERNS.items():
        match = pattern.search(body)
        if match is None:
            return None
        try:
            value = int(match.group(1))
        except (TypeError, ValueError, OverflowError):
            return None
        if value < 0:
            return None
        values[field] = value
    return values
