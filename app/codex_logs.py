"""Read-only latest response.completed usage metadata from Codex logs SQLite."""

from __future__ import annotations

import os
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
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
REAL_USAGE_SOURCE = "codex_logs_sqlite / real usage"
UNKNOWN_SOURCE = "unknown"
_USAGE_OBJECT_PATTERN = re.compile(r'(?:"usage"\s*:|\busage\s*=)\s*(\{)')


@dataclass(frozen=True)
class CodexResponseUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int
    reasoning_tokens: int


class LogsAdapterStatus(str, Enum):
    CONNECTED = "connected"
    DATABASE_MISSING = "database missing"
    OPEN_FAILED = "open failed"
    NO_RESPONSE_COMPLETED = "no response.completed"
    PARSE_FAILED = "parse failed"


@dataclass(frozen=True)
class CodexLogsResult:
    usage: CodexResponseUsage | None
    source: str
    status: LogsAdapterStatus
    observed_at: datetime | None
    refreshed_at: datetime
    error_category: str | None = None


def configured_logs_path() -> Path:
    configured = os.environ.get(CODEX_LOGS_PATH_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_CODEX_LOGS_PATH


def load_latest_completed_response_usage(path: Path | None = None) -> CodexResponseUsage | None:
    return load_latest_completed_response_result(path).usage


def load_latest_completed_response_result(
    path: Path | None = None,
    now: datetime | None = None,
) -> CodexLogsResult:
    database = path or configured_logs_path()
    refreshed_at = now or datetime.now(timezone.utc)
    if not database.is_file():
        return CodexLogsResult(
            usage=None,
            source=UNKNOWN_SOURCE,
            status=LogsAdapterStatus.DATABASE_MISSING,
            observed_at=None,
            refreshed_at=refreshed_at,
            error_category="database missing",
        )

    row, open_failed = _fetch_latest_completed_response(database)
    if open_failed:
        return CodexLogsResult(
            usage=None,
            source=UNKNOWN_SOURCE,
            status=LogsAdapterStatus.OPEN_FAILED,
            observed_at=None,
            refreshed_at=refreshed_at,
            error_category="database open or query failed",
        )
    if row is None:
        return CodexLogsResult(
            usage=None,
            source=UNKNOWN_SOURCE,
            status=LogsAdapterStatus.NO_RESPONSE_COMPLETED,
            observed_at=None,
            refreshed_at=refreshed_at,
            error_category="response.completed unavailable",
        )
    if not isinstance(row[0], str):
        return _parse_failed(refreshed_at)
    values = _extract_usage_values(row[0])
    if values is None:
        return _parse_failed(refreshed_at)
    return CodexLogsResult(
        usage=CodexResponseUsage(**values),
        source=REAL_USAGE_SOURCE,
        status=LogsAdapterStatus.CONNECTED,
        observed_at=_event_time_from_nanos(row[1] if len(row) > 1 else None),
        refreshed_at=refreshed_at,
    )


def _fetch_latest_completed_response(database: Path) -> tuple[tuple[object, ...] | None, bool]:
    uri = database.resolve().as_uri() + "?mode=ro"
    for args, kwargs in [
        ((uri,), {"uri": True}),
        ((str(database),), {}),
    ]:
        try:
            with closing(sqlite3.connect(*args, **kwargs)) as connection:
                connection.execute("PRAGMA query_only=ON")
                return connection.execute(
                    """
                    SELECT feedback_log_body, ts_nanos
                    FROM logs
                    WHERE feedback_log_body LIKE '%response.completed%'
                    ORDER BY ts_nanos DESC
                    LIMIT 1
                    """
                ).fetchone(), False
        except (OSError, sqlite3.Error):
            continue
    return None, True


def _parse_failed(refreshed_at: datetime) -> CodexLogsResult:
    return CodexLogsResult(
        usage=None,
        source=UNKNOWN_SOURCE,
        status=LogsAdapterStatus.PARSE_FAILED,
        observed_at=None,
        refreshed_at=refreshed_at,
        error_category="invalid usage payload",
    )


def _event_time_from_nanos(value: object) -> datetime | None:
    try:
        nanos = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    # Accept only a plausible Unix timestamp; do not substitute rowid or file mtime.
    if not 946_684_800_000_000_000 <= nanos <= 4_102_444_800_000_000_000:
        return None
    try:
        return datetime.fromtimestamp(nanos / 1_000_000_000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _extract_usage_values(body: str) -> dict[str, int] | None:
    marker = _USAGE_OBJECT_PATTERN.search(body)
    starts = [marker.start(1)] if marker is not None else []
    first_field = body.find(f'"{USAGE_FIELDS[0]}"')
    if first_field >= 0:
        nearest_object = body.rfind("{", 0, first_field)
        if nearest_object >= 0 and nearest_object not in starts:
            starts.append(nearest_object)
    decoder = json.JSONDecoder()
    for start in starts:
        try:
            candidate, _ = decoder.raw_decode(body[start:])
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        values = _usage_values_from_mapping(candidate)
        if values is not None:
            return values
    return None


def _usage_values_from_mapping(candidate: object) -> dict[str, int] | None:
    if not isinstance(candidate, dict):
        return None
    input_details = candidate.get("input_tokens_details")
    output_details = candidate.get("output_tokens_details")
    raw_values = {
        "input_tokens": candidate.get("input_tokens"),
        "output_tokens": candidate.get("output_tokens"),
        "total_tokens": candidate.get("total_tokens"),
        "cached_tokens": candidate.get("cached_tokens")
        if "cached_tokens" in candidate
        else input_details.get("cached_tokens") if isinstance(input_details, dict) else None,
        "reasoning_tokens": candidate.get("reasoning_tokens")
        if "reasoning_tokens" in candidate
        else output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None,
    }
    values: dict[str, int] = {}
    for field in USAGE_FIELDS:
        value = raw_values[field]
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if value < 0:
            return None
        values[field] = value
    return values
