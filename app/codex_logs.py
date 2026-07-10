"""Read-only latest response.completed usage metadata from Codex logs SQLite."""

from __future__ import annotations

import os
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
    values = _usage_values_from_row(row)
    if values is None:
        return _parse_failed(refreshed_at)
    return CodexLogsResult(
        usage=CodexResponseUsage(**values),
        source=REAL_USAGE_SOURCE,
        status=LogsAdapterStatus.CONNECTED,
        observed_at=_event_time_from_nanos(row[5]),
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
                    WITH normalized AS (
                        SELECT
                            trim(feedback_log_body) AS full_text,
                            ltrim(feedback_log_body) AS left_trimmed_text,
                            ts_nanos
                        FROM logs
                    ),
                    validated AS (
                        SELECT
                            CASE WHEN json_valid(full_text) THEN full_text END AS full_json,
                            CASE
                                WHEN substr(left_trimmed_text, 1, length('SSE event: ')) = 'SSE event: '
                                THEN CASE
                                    WHEN json_valid(trim(substr(left_trimmed_text, length('SSE event: ') + 1)))
                                    THEN trim(substr(left_trimmed_text, length('SSE event: ') + 1))
                                END
                            END AS anchored_json,
                            ts_nanos
                        FROM normalized
                    ),
                    structural_events AS (
                        SELECT
                            CASE
                                WHEN json_type(full_json, '$') = 'object'
                                 AND json_extract(full_json, '$.type') = 'response.completed'
                                THEN full_json
                                WHEN json_type(anchored_json, '$') = 'object'
                                 AND json_extract(anchored_json, '$.type') = 'response.completed'
                                THEN anchored_json
                            END AS event_json,
                            CASE
                                WHEN json_type(full_json, '$') = 'object'
                                 AND json_extract(full_json, '$.type') = 'response.completed'
                                THEN 1
                                WHEN json_type(anchored_json, '$') = 'object'
                                 AND json_extract(anchored_json, '$.type') = 'response.completed'
                                THEN 2
                            END AS event_format,
                            ts_nanos
                        FROM validated
                    ),
                    latest AS (
                        SELECT
                            event_json,
                            event_format,
                            ts_nanos
                        FROM structural_events
                        WHERE event_json IS NOT NULL
                        ORDER BY ts_nanos DESC
                        LIMIT 1
                    ),
                    usage_document AS (
                        SELECT
                            CASE
                                WHEN event_format = 1
                                 AND json_type(event_json, '$.usage') = 'object'
                                THEN json_extract(event_json, '$.usage')
                                WHEN event_format = 2
                                 AND json_type(event_json, '$.response.usage') = 'object'
                                THEN json_extract(event_json, '$.response.usage')
                            END AS usage_json,
                            ts_nanos
                        FROM latest
                    ),
                    extracted AS (
                        SELECT
                            json_extract(usage_json, '$.input_tokens') AS input_tokens,
                            json_extract(usage_json, '$.output_tokens') AS output_tokens,
                            json_extract(usage_json, '$.total_tokens') AS total_tokens,
                            CASE
                                WHEN json_type(usage_json, '$.cached_tokens') IS NOT NULL
                                THEN json_extract(usage_json, '$.cached_tokens')
                                ELSE json_extract(usage_json, '$.input_tokens_details.cached_tokens')
                            END AS cached_tokens,
                            CASE
                                WHEN json_type(usage_json, '$.reasoning_tokens') IS NOT NULL
                                THEN json_extract(usage_json, '$.reasoning_tokens')
                                ELSE json_extract(usage_json, '$.output_tokens_details.reasoning_tokens')
                            END AS reasoning_tokens,
                            ts_nanos,
                            json_type(usage_json, '$.input_tokens') AS input_type,
                            json_type(usage_json, '$.output_tokens') AS output_type,
                            json_type(usage_json, '$.total_tokens') AS total_type,
                            CASE
                                WHEN json_type(usage_json, '$.cached_tokens') IS NOT NULL
                                THEN json_type(usage_json, '$.cached_tokens')
                                ELSE json_type(usage_json, '$.input_tokens_details.cached_tokens')
                            END AS cached_type,
                            CASE
                                WHEN json_type(usage_json, '$.reasoning_tokens') IS NOT NULL
                                THEN json_type(usage_json, '$.reasoning_tokens')
                                ELSE json_type(usage_json, '$.output_tokens_details.reasoning_tokens')
                            END AS reasoning_type
                        FROM usage_document
                    )
                    SELECT
                        input_tokens,
                        output_tokens,
                        total_tokens,
                        cached_tokens,
                        reasoning_tokens,
                        ts_nanos,
                        CASE
                            WHEN input_type = 'integer'
                             AND output_type = 'integer'
                             AND total_type = 'integer'
                             AND cached_type = 'integer'
                             AND reasoning_type = 'integer'
                            THEN 1 ELSE 0
                        END AS usage_valid
                    FROM extracted
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


def _usage_values_from_row(row: tuple[object, ...]) -> dict[str, int] | None:
    if len(row) != 7 or row[6] != 1:
        return None
    values: dict[str, int] = {}
    for field, value in zip(USAGE_FIELDS, row[:5]):
        if type(value) is not int:
            return None
        if value < 0:
            return None
        values[field] = value
    return values
