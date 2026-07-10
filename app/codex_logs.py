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
                    WITH RECURSIVE latest AS (
                        SELECT feedback_log_body AS internal_body, ts_nanos
                        FROM logs
                        WHERE feedback_log_body LIKE '%response.completed%'
                        ORDER BY ts_nanos DESC
                        LIMIT 1
                    ),
                    candidate_starts AS (
                        SELECT
                            instr(internal_body, '{') AS first_object_start,
                            CASE
                                WHEN instr(internal_body, '"usage"') > 0
                                THEN instr(internal_body, '"usage"')
                                WHEN instr(internal_body, 'usage=') > 0
                                THEN instr(internal_body, 'usage=')
                                ELSE 0
                            END AS usage_marker_start,
                            internal_body,
                            ts_nanos
                        FROM latest
                    ),
                    candidates(priority, candidate_text, ts_nanos) AS (
                        SELECT
                            1,
                            substr(
                                internal_body,
                                usage_marker_start
                                + instr(substr(internal_body, usage_marker_start), '{')
                                - 1
                            ),
                            ts_nanos
                        FROM candidate_starts
                        WHERE usage_marker_start > 0
                          AND instr(substr(internal_body, usage_marker_start), '{') > 0
                        UNION ALL
                        SELECT 2, substr(internal_body, first_object_start), ts_nanos
                        FROM candidate_starts
                        WHERE first_object_start > 0
                    ),
                    closing_positions(priority, candidate_text, ts_nanos, closing_pos) AS (
                        SELECT priority, candidate_text, ts_nanos, instr(candidate_text, '}')
                        FROM candidates
                        WHERE instr(candidate_text, '}') > 0
                        UNION ALL
                        SELECT
                            priority,
                            candidate_text,
                            ts_nanos,
                            closing_pos + instr(substr(candidate_text, closing_pos + 1), '}')
                        FROM closing_positions
                        WHERE instr(substr(candidate_text, closing_pos + 1), '}') > 0
                    ),
                    valid_document AS (
                        SELECT
                            substr(candidate_text, 1, closing_pos) AS json_document
                        FROM closing_positions
                        WHERE json_valid(substr(candidate_text, 1, closing_pos))
                        ORDER BY priority, closing_pos
                        LIMIT 1
                    ),
                    document AS (
                        SELECT valid_document.json_document, latest.ts_nanos
                        FROM latest
                        LEFT JOIN valid_document ON 1 = 1
                    ),
                    usage_document AS (
                        SELECT
                            CASE
                                WHEN json_type(json_document, '$.usage') = 'object'
                                THEN json_extract(json_document, '$.usage')
                                WHEN json_type(json_document, '$') = 'object'
                                THEN json_document
                            END AS usage_json,
                            ts_nanos
                        FROM document
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
