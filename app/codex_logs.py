"""Read-only latest response.completed usage metadata from Codex logs SQLite."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
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
MAX_INCREMENTAL_ROWS = 500


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
    new_event_found: bool = False
    last_scan_at: datetime | None = None
    incremental_reader_initialized: bool = False


@dataclass(frozen=True, order=True)
class LogsCursor:
    ts: int
    ts_nanos: int
    row_id: int


class CodexLogsReader:
    """Stateful read-only scanner: one initial lookup, then bounded increments."""

    def __init__(self, batch_limit: int = MAX_INCREMENTAL_ROWS) -> None:
        self.batch_limit = max(int(batch_limit), 1)
        self.cursor: LogsCursor | None = None
        self.last_successful_result: CodexLogsResult | None = None
        self._last_result: CodexLogsResult | None = None
        self._database_key: tuple[str, int, int] | None = None

    def refresh(
        self,
        path: Path | None = None,
        now: datetime | None = None,
    ) -> CodexLogsResult:
        database = path or configured_logs_path()
        refreshed_at = now or datetime.now(timezone.utc)
        try:
            database_exists = database.is_file()
        except OSError:
            database_exists = False
        if not database_exists:
            return _failure_result(
                LogsAdapterStatus.DATABASE_MISSING,
                refreshed_at,
                "database missing",
                self.cursor is not None,
            )

        try:
            database_key = _database_identity(database)
        except OSError:
            return _failure_result(
                LogsAdapterStatus.OPEN_FAILED,
                refreshed_at,
                "database open or query failed",
                self.cursor is not None,
            )
        if self._database_key is not None and database_key != self._database_key:
            self._reset()
        self._database_key = database_key

        incremental = self.cursor is not None
        row, open_failed = _fetch_scan(
            database,
            self.cursor if incremental else None,
            self.batch_limit,
        )
        if open_failed or row is None:
            return _failure_result(
                LogsAdapterStatus.OPEN_FAILED,
                refreshed_at,
                "database open or query failed",
                self.cursor is not None,
            )

        database_tail = _cursor_from_row(row, 11)
        if incremental and (database_tail is None or database_tail < self.cursor):
            self._reset()
            self._database_key = database_key
            row, open_failed = _fetch_scan(database, None, self.batch_limit)
            if open_failed or row is None:
                return _failure_result(
                    LogsAdapterStatus.OPEN_FAILED,
                    refreshed_at,
                    "database open or query failed",
                    False,
                )

        scan_tail = _cursor_from_row(row, 8)
        if scan_tail is not None:
            self.cursor = scan_tail

        if row[7] != 1:
            if self._last_result is not None:
                result = replace(
                    self._last_result,
                    refreshed_at=refreshed_at,
                    new_event_found=False,
                    last_scan_at=refreshed_at,
                    incremental_reader_initialized=self.cursor is not None,
                )
            else:
                result = CodexLogsResult(
                    usage=None,
                    source=UNKNOWN_SOURCE,
                    status=LogsAdapterStatus.NO_RESPONSE_COMPLETED,
                    observed_at=None,
                    refreshed_at=refreshed_at,
                    error_category="response.completed unavailable",
                    last_scan_at=refreshed_at,
                    incremental_reader_initialized=self.cursor is not None,
                )
            self._last_result = result
            return result

        values = _usage_values_from_row(row)
        if values is None:
            result = _parse_failed(
                refreshed_at,
                new_event_found=True,
                initialized=self.cursor is not None,
            )
            self._last_result = result
            return result

        result = CodexLogsResult(
            usage=CodexResponseUsage(**values),
            source=REAL_USAGE_SOURCE,
            status=LogsAdapterStatus.CONNECTED,
            observed_at=_event_time_from_nanos(row[5]),
            refreshed_at=refreshed_at,
            new_event_found=True,
            last_scan_at=refreshed_at,
            incremental_reader_initialized=self.cursor is not None,
        )
        self.last_successful_result = result
        self._last_result = result
        return result

    def _reset(self) -> None:
        self.cursor = None
        self.last_successful_result = None
        self._last_result = None


def configured_logs_path() -> Path:
    configured = os.environ.get(CODEX_LOGS_PATH_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_CODEX_LOGS_PATH


def load_latest_completed_response_usage(path: Path | None = None) -> CodexResponseUsage | None:
    return load_latest_completed_response_result(path).usage


def load_latest_completed_response_result(
    path: Path | None = None,
    now: datetime | None = None,
) -> CodexLogsResult:
    return CodexLogsReader().refresh(path, now)


def _fetch_scan(
    database: Path,
    cursor: LogsCursor | None,
    batch_limit: int,
) -> tuple[tuple[object, ...] | None, bool]:
    if cursor is None:
        bounded_rows = "SELECT feedback_log_body, ts, ts_nanos, id FROM logs"
        parameters: tuple[object, ...] = ()
    else:
        bounded_rows = """
            SELECT feedback_log_body, ts, ts_nanos, id
            FROM logs
            WHERE (ts, ts_nanos, id) > (?, ?, ?)
            ORDER BY ts ASC, ts_nanos ASC, id ASC
            LIMIT ?
        """
        parameters = (cursor.ts, cursor.ts_nanos, cursor.row_id, batch_limit)
    uri = database.resolve().as_uri() + "?mode=ro"
    for args, kwargs in [
        ((uri,), {"uri": True}),
        ((str(database),), {}),
    ]:
        try:
            with closing(sqlite3.connect(*args, **kwargs)) as connection:
                connection.execute("PRAGMA query_only=ON")
                return connection.execute(
                    f"""
                    WITH bounded_rows AS (
                        {bounded_rows}
                    ),
                    normalized AS (
                        SELECT
                            trim(feedback_log_body) AS full_text,
                            ltrim(feedback_log_body) AS left_trimmed_text,
                            ts,
                            ts_nanos,
                            id
                        FROM bounded_rows
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
                            ts,
                            ts_nanos,
                            id
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
                            ts,
                            ts_nanos,
                            id
                        FROM validated
                    ),
                    latest AS (
                        SELECT
                            event_json,
                            event_format,
                            ts,
                            ts_nanos
                        FROM structural_events
                        WHERE event_json IS NOT NULL
                        ORDER BY ts DESC, ts_nanos DESC, id DESC
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
                            ts,
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
                            ts,
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
                    ),
                    scan_tail AS (
                        SELECT ts, ts_nanos, id
                        FROM bounded_rows
                        ORDER BY ts DESC, ts_nanos DESC, id DESC
                        LIMIT 1
                    ),
                    database_tail AS (
                        SELECT ts, ts_nanos, id
                        FROM logs
                        ORDER BY ts DESC, ts_nanos DESC, id DESC
                        LIMIT 1
                    )
                    SELECT
                        extracted.input_tokens,
                        extracted.output_tokens,
                        extracted.total_tokens,
                        extracted.cached_tokens,
                        extracted.reasoning_tokens,
                        CASE WHEN latest.event_json IS NOT NULL
                             THEN latest.ts * 1000000000 + latest.ts_nanos END,
                        CASE
                            WHEN extracted.input_type = 'integer'
                             AND extracted.output_type = 'integer'
                             AND extracted.total_type = 'integer'
                             AND extracted.cached_type = 'integer'
                             AND extracted.reasoning_type = 'integer'
                            THEN 1 ELSE 0
                        END AS usage_valid,
                        CASE WHEN latest.event_json IS NOT NULL THEN 1 ELSE 0 END,
                        scan_tail.ts,
                        scan_tail.ts_nanos,
                        scan_tail.id,
                        database_tail.ts,
                        database_tail.ts_nanos,
                        database_tail.id
                    FROM (SELECT 1) AS seed
                    LEFT JOIN latest ON 1 = 1
                    LEFT JOIN extracted ON 1 = 1
                    LEFT JOIN scan_tail ON 1 = 1
                    LEFT JOIN database_tail ON 1 = 1
                    """,
                    parameters,
                ).fetchone(), False
        except (OSError, sqlite3.Error):
            continue
    return None, True


def _parse_failed(
    refreshed_at: datetime,
    new_event_found: bool = False,
    initialized: bool = False,
) -> CodexLogsResult:
    return CodexLogsResult(
        usage=None,
        source=UNKNOWN_SOURCE,
        status=LogsAdapterStatus.PARSE_FAILED,
        observed_at=None,
        refreshed_at=refreshed_at,
        error_category="invalid usage payload",
        new_event_found=new_event_found,
        last_scan_at=refreshed_at,
        incremental_reader_initialized=initialized,
    )


def _failure_result(
    status: LogsAdapterStatus,
    refreshed_at: datetime,
    error_category: str,
    initialized: bool,
) -> CodexLogsResult:
    return CodexLogsResult(
        usage=None,
        source=UNKNOWN_SOURCE,
        status=status,
        observed_at=None,
        refreshed_at=refreshed_at,
        error_category=error_category,
        last_scan_at=refreshed_at,
        incremental_reader_initialized=initialized,
    )


def _database_identity(database: Path) -> tuple[str, int, int]:
    stat = database.stat()
    return (str(database.resolve()), int(stat.st_dev), int(stat.st_ino))


def _cursor_from_row(row: tuple[object, ...], offset: int) -> LogsCursor | None:
    values = row[offset : offset + 3]
    if len(values) != 3 or any(type(value) is not int for value in values):
        return None
    return LogsCursor(values[0], values[1], values[2])


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
    if len(row) < 7 or row[6] != 1:
        return None
    values: dict[str, int] = {}
    for field, value in zip(USAGE_FIELDS, row[:5]):
        if type(value) is not int:
            return None
        if value < 0:
            return None
        values[field] = value
    return values
