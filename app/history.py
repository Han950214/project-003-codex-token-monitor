"""Versioned local persistence for privacy-safe usage history samples."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from app.paths import history_db_path

if TYPE_CHECKING:
    from app.dashboard import DashboardSnapshot, MiniThreadSnapshot
    from app.quota import CodexQuotaSnapshot, QuotaWindow


SCHEMA_VERSION = 1
RETENTION_DAYS = 90
MAX_HISTORY_ROWS = 200_000
HISTORY_STALE_AFTER = timedelta(minutes=3)
SUPPORTED_RANGES = (7, 30, 90)

_TABLE = "usage_history_samples"
_META_TABLE = "usage_history_meta"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class HistoryObservation:
    """One normalized safe observation ready for deterministic persistence."""

    sampled_at: datetime = field(default_factory=_utc_now)
    source_observed_at: datetime | None = None
    quota_observed_at: datetime | None = None
    thread_safe_id: str | None = None
    model_safe_id: str | None = None
    source_type: str = "dashboard"
    source_status: str = "unavailable"
    source_available: bool = False
    token_stale: bool = False
    token_stale_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    session_total_tokens: int | None = None
    turn_count: int | None = None
    quota_source_status: str = "unavailable"
    five_hour_used_percent: float | None = None
    five_hour_remaining_percent: float | None = None
    five_hour_reset_at: datetime | None = None
    five_hour_source: str = "unknown"
    five_hour_available: bool = False
    five_hour_stale: bool = False
    five_hour_error_code: str | None = None
    weekly_used_percent: float | None = None
    weekly_remaining_percent: float | None = None
    weekly_reset_at: datetime | None = None
    weekly_source: str = "unknown"
    weekly_available: bool = False
    weekly_stale: bool = False
    weekly_error_code: str | None = None
    is_derived: bool = False
    legacy_unknown_time: bool = False

    def __post_init__(self) -> None:
        for name in (
            "sampled_at", "source_observed_at", "quota_observed_at",
            "five_hour_reset_at", "weekly_reset_at",
        ):
            object.__setattr__(self, name, _aware_utc(getattr(self, name), name))
        object.__setattr__(self, "thread_safe_id", _safe_identifier(self.thread_safe_id))
        object.__setattr__(self, "model_safe_id", _safe_identifier(self.model_safe_id))
        for name in (
            "source_type", "source_status", "quota_source_status",
            "five_hour_source", "weekly_source",
        ):
            object.__setattr__(self, name, _safe_code(getattr(self, name)))
        for name in (
            "token_stale_reason", "five_hour_error_code", "weekly_error_code",
        ):
            value = getattr(self, name)
            object.__setattr__(self, name, None if value is None else _safe_code(value))
        for name in (
            "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
            "reasoning_tokens", "session_total_tokens", "turn_count",
        ):
            _validate_nonnegative(getattr(self, name), name)
        if (
            self.input_tokens is not None and self.cached_tokens is not None
            and self.cached_tokens > self.input_tokens
        ):
            raise ValueError("cached_tokens_exceed_input")
        if (
            self.output_tokens is not None and self.reasoning_tokens is not None
            and self.reasoning_tokens > self.output_tokens
        ):
            raise ValueError("reasoning_tokens_exceed_output")
        for name in (
            "five_hour_used_percent", "five_hour_remaining_percent",
            "weekly_used_percent", "weekly_remaining_percent",
        ):
            _validate_percent(getattr(self, name), name)

    @classmethod
    def from_dashboard(
        cls,
        snapshot: "DashboardSnapshot",
        quota: "CodexQuotaSnapshot",
        *,
        sampled_at: datetime | None = None,
    ) -> "HistoryObservation":
        selected = snapshot.selected_session
        instruction = selected.instruction if selected is not None else snapshot.rollout.instruction
        usage = instruction.usage if instruction is not None else None
        cumulative = (
            selected.thread_cumulative_usage
            if selected is not None else snapshot.rollout.thread_cumulative_usage
        )
        thread_id = (
            selected.thread_id if selected is not None
            else snapshot.selected_thread_id or snapshot.rollout.thread_id
        )
        state = snapshot.state_metadata.get(thread_id) if thread_id else None
        model = state.model if state is not None else None
        if model is None and snapshot.state_total is not None:
            model = snapshot.state_total.model
        source_status = (
            selected.status if selected is not None
            else instruction.status if instruction is not None else "unavailable"
        )
        source_available = bool(
            source_status not in {"unavailable", "no_selection"}
            and (usage is not None or cumulative is not None)
        )
        return cls(
            sampled_at=sampled_at or _utc_now(),
            source_observed_at=(
                selected.observed_at if selected is not None else snapshot.rollout.observed_at
            ),
            quota_observed_at=quota.refreshed_at,
            thread_safe_id=thread_id,
            model_safe_id=model,
            source_type="dashboard",
            source_status=source_status,
            source_available=source_available,
            token_stale=source_status == "stale",
            token_stale_reason="source_stale" if source_status == "stale" else None,
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            total_tokens=usage.total_tokens if usage is not None else None,
            cached_tokens=usage.cached_input_tokens if usage is not None else None,
            reasoning_tokens=usage.reasoning_output_tokens if usage is not None else None,
            session_total_tokens=cumulative.total_tokens if cumulative is not None else None,
            turn_count=(
                selected.turn_count if selected is not None else snapshot.rollout.turn_count
            ),
            **_quota_values(quota),
        )

    @classmethod
    def from_mini(
        cls,
        mini: "MiniThreadSnapshot",
        quota: "CodexQuotaSnapshot",
        thread_safe_id: str | None = None,
        *,
        model_safe_id: str | None = None,
        sampled_at: datetime | None = None,
    ) -> "HistoryObservation":
        available = bool(
            mini.status not in {"unavailable", "no_selection"}
            and (
                mini.instruction_total_tokens is not None
                or mini.session_total_tokens is not None
            )
        )
        return cls(
            sampled_at=sampled_at or _utc_now(),
            source_observed_at=mini.observed_at,
            quota_observed_at=quota.refreshed_at,
            thread_safe_id=thread_safe_id,
            model_safe_id=model_safe_id,
            source_type="mini",
            source_status=mini.status,
            source_available=available,
            token_stale=mini.status == "stale",
            token_stale_reason="source_stale" if mini.status == "stale" else None,
            total_tokens=mini.instruction_total_tokens,
            session_total_tokens=mini.session_total_tokens,
            turn_count=mini.turn_count,
            **_quota_values(quota),
        )

    @property
    def sample_fingerprint(self) -> str:
        """Stable identity excluding local capture and quota observation times."""

        payload = {
            "schema_version": SCHEMA_VERSION,
            "source_observed_at": _iso_utc(self.source_observed_at),
            "thread_safe_id": self.thread_safe_id,
            "model_safe_id": self.model_safe_id,
            "source_type": self.source_type,
            "source_status": self.source_status,
            "source_available": self.source_available,
            "token_stale": self.token_stale,
            "token_stale_reason": self.token_stale_reason,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "session_total_tokens": self.session_total_tokens,
            "turn_count": self.turn_count,
            "quota_source_status": self.quota_source_status,
            "five_hour_used_percent": self.five_hour_used_percent,
            "five_hour_remaining_percent": self.five_hour_remaining_percent,
            "five_hour_reset_at": _iso_utc(self.five_hour_reset_at),
            "five_hour_source": self.five_hour_source,
            "five_hour_available": self.five_hour_available,
            "five_hour_stale": self.five_hour_stale,
            "five_hour_error_code": self.five_hour_error_code,
            "weekly_used_percent": self.weekly_used_percent,
            "weekly_remaining_percent": self.weekly_remaining_percent,
            "weekly_reset_at": _iso_utc(self.weekly_reset_at),
            "weekly_source": self.weekly_source,
            "weekly_available": self.weekly_available,
            "weekly_stale": self.weekly_stale,
            "weekly_error_code": self.weekly_error_code,
            "is_derived": self.is_derived,
            "legacy_unknown_time": self.legacy_unknown_time,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def cache_reuse_ratio(self) -> float | None:
        if not self.input_tokens:
            return None
        if self.cached_tokens is None:
            return None
        return self.cached_tokens / self.input_tokens


@dataclass(frozen=True)
class HistorySample(HistoryObservation):
    sample_id: int = 0
    schema_version: int = SCHEMA_VERSION
    stored_fingerprint: str = ""


@dataclass(frozen=True)
class HistoryQueryResult:
    range_days: int
    status: str
    samples: tuple[HistorySample, ...] = ()
    quota_samples: tuple[HistorySample, ...] = ()
    sample_count: int = 0
    start_at: datetime | None = None
    end_at: datetime | None = None
    stale: bool = False
    metrics_available: tuple[str, ...] = ()
    error_code: str | None = None


_COLUMN_DEFINITIONS = {
    "schema_version": "INTEGER NOT NULL DEFAULT 1",
    "sampled_at_utc": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00.000000Z'",
    "source_observed_at_utc": "TEXT",
    "quota_observed_at_utc": "TEXT",
    "thread_safe_id": "TEXT",
    "model_safe_id": "TEXT",
    "source_type": "TEXT NOT NULL DEFAULT 'unknown'",
    "source_status": "TEXT NOT NULL DEFAULT 'unavailable'",
    "source_available": "INTEGER NOT NULL DEFAULT 0",
    "token_stale": "INTEGER NOT NULL DEFAULT 0",
    "token_stale_reason": "TEXT",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "total_tokens": "INTEGER",
    "cached_tokens": "INTEGER",
    "reasoning_tokens": "INTEGER",
    "session_total_tokens": "INTEGER",
    "turn_count": "INTEGER",
    "quota_source_status": "TEXT NOT NULL DEFAULT 'unavailable'",
    "five_hour_used_percent": "REAL",
    "five_hour_remaining_percent": "REAL",
    "five_hour_reset_at_utc": "TEXT",
    "five_hour_source": "TEXT NOT NULL DEFAULT 'unknown'",
    "five_hour_available": "INTEGER NOT NULL DEFAULT 0",
    "five_hour_stale": "INTEGER NOT NULL DEFAULT 0",
    "five_hour_error_code": "TEXT",
    "weekly_used_percent": "REAL",
    "weekly_remaining_percent": "REAL",
    "weekly_reset_at_utc": "TEXT",
    "weekly_source": "TEXT NOT NULL DEFAULT 'unknown'",
    "weekly_available": "INTEGER NOT NULL DEFAULT 0",
    "weekly_stale": "INTEGER NOT NULL DEFAULT 0",
    "weekly_error_code": "TEXT",
    "is_derived": "INTEGER NOT NULL DEFAULT 0",
    "legacy_unknown_time": "INTEGER NOT NULL DEFAULT 0",
    "sample_fingerprint": "TEXT",
}

_INSERT_COLUMNS = tuple(_COLUMN_DEFINITIONS)


class UsageHistoryStore:
    """Single SQLite boundary for migrations, writes, queries, and retention."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        retention_days: int = RETENTION_DAYS,
        max_rows: int = MAX_HISTORY_ROWS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.path = Path(path) if path is not None else history_db_path()
        self.retention_days = max(1, int(retention_days))
        self.max_rows = max(1, int(max_rows))
        self.clock = clock
        self.last_error: str | None = None
        self._initialized = False
        self._lock = threading.RLock()

    def initialize(self) -> bool:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(self._connect()) as connection:
                    connection.execute("PRAGMA journal_mode=WAL").fetchone()
                    self._migrate(connection)
                    self._prune_if_due(connection, self.clock())
                self._initialized = True
                self.last_error = None
                return True
            except (OSError, sqlite3.Error, ValueError) as exc:
                self._initialized = False
                self.last_error = _storage_error_code(exc, migration=True)
                return False

    def record(self, observation: HistoryObservation) -> bool:
        if not isinstance(observation, HistoryObservation):
            raise TypeError("history_observation_required")
        with self._lock:
            if not self._initialized and not self.initialize():
                return False
            try:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    values = _observation_row(observation)
                    placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
                    columns = ", ".join(_INSERT_COLUMNS)
                    cursor = connection.execute(
                        f"INSERT OR IGNORE INTO {_TABLE} ({columns}) VALUES ({placeholders})",
                        values,
                    )
                    inserted = cursor.rowcount == 1
                    if inserted:
                        self._prune_capacity(connection)
                        self._prune_if_due(connection, self.clock(), in_transaction=True)
                    connection.commit()
                self.last_error = None
                return inserted
            except (OSError, sqlite3.Error, ValueError) as exc:
                self.last_error = _storage_error_code(exc)
                return False

    def query(
        self,
        range_days: int,
        thread_safe_id: str | None = None,
        *,
        now: datetime | None = None,
    ) -> HistoryQueryResult:
        if range_days not in SUPPORTED_RANGES:
            raise ValueError("unsupported_history_range")
        now = _aware_utc(now or self.clock(), "now")
        assert now is not None
        if not self._initialized and not self.initialize():
            return HistoryQueryResult(
                range_days, "unavailable", error_code=self.last_error,
            )
        cutoff = _iso_utc(now - timedelta(days=range_days))
        try:
            with closing(self._connect()) as connection:
                if thread_safe_id is None:
                    rows = connection.execute(
                        f"SELECT * FROM {_TABLE} WHERE sampled_at_utc >= ? "
                        "ORDER BY sampled_at_utc, id",
                        (cutoff,),
                    ).fetchall()
                else:
                    safe_thread = _safe_identifier(thread_safe_id)
                    rows = connection.execute(
                        f"SELECT * FROM {_TABLE} WHERE sampled_at_utc >= ? "
                        "AND thread_safe_id IS ? ORDER BY sampled_at_utc, id",
                        (cutoff, safe_thread),
                    ).fetchall()
                quota_rows = connection.execute(
                    f"SELECT * FROM {_TABLE} WHERE sampled_at_utc >= ? "
                    "ORDER BY sampled_at_utc, id",
                    (cutoff,),
                ).fetchall()
            samples = tuple(_sample_from_row(row) for row in rows)
            quota_samples = _global_quota_samples(quota_rows)
            self.last_error = None
            return _query_result(range_days, samples, quota_samples, now)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.last_error = _storage_error_code(exc)
            return HistoryQueryResult(
                range_days, "unavailable", error_code=self.last_error,
            )

    def prune(self, *, now: datetime | None = None) -> int:
        now = _aware_utc(now or self.clock(), "now")
        assert now is not None
        with self._lock:
            if not self._initialized and not self.initialize():
                return 0
            try:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    deleted = self._prune(connection, now)
                    self._set_last_pruned(connection, now)
                    connection.commit()
                self.last_error = None
                return deleted
            except (OSError, sqlite3.Error, ValueError) as exc:
                self.last_error = _storage_error_code(exc)
                return 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=2000")
        return connection

    def _migrate(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise ValueError("history_schema_newer_than_application")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {_META_TABLE} ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                + ", ".join(
                    f"{name} {definition}"
                    for name, definition in _COLUMN_DEFINITIONS.items()
                )
                + ")"
            )
            columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({_TABLE})")
            }
            if "id" not in columns:
                raise ValueError("history_schema_missing_primary_key")
            sampled_at_missing = "sampled_at_utc" not in columns
            for name, definition in _COLUMN_DEFINITIONS.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE {_TABLE} ADD COLUMN {name} {definition}"
                    )
            migration_time = _iso_utc(self.clock())
            if sampled_at_missing:
                # Preserve pre-v1 rows during the first retention pass. They
                # remain unavailable (the migrated default) and cannot create
                # a synthetic trend, but their original numeric columns survive.
                connection.execute(
                    f"UPDATE {_TABLE} SET sampled_at_utc = ?, legacy_unknown_time = 1 "
                    "WHERE sampled_at_utc = '1970-01-01T00:00:00.000000Z'",
                    (migration_time,),
                )
            else:
                connection.execute(
                    f"UPDATE {_TABLE} SET sampled_at_utc = ?, legacy_unknown_time = 1 "
                    "WHERE sampled_at_utc IS NULL OR sampled_at_utc = ''",
                    (migration_time,),
                )
            connection.execute(
                f"UPDATE {_TABLE} SET sample_fingerprint = "
                "'legacy-' || printf('%016x', id) "
                "WHERE sample_fingerprint IS NULL OR sample_fingerprint = ''"
            )
            connection.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS ux_{_TABLE}_fingerprint "
                f"ON {_TABLE}(sample_fingerprint)"
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_sampled_at "
                f"ON {_TABLE}(sampled_at_utc, id)"
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_thread_sampled_at "
                f"ON {_TABLE}(thread_safe_id, sampled_at_utc, id)"
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _prune_if_due(
        self,
        connection: sqlite3.Connection,
        now: datetime,
        *,
        in_transaction: bool = False,
    ) -> int:
        today = now.astimezone(timezone.utc).date().isoformat()
        row = connection.execute(
            f"SELECT value FROM {_META_TABLE} WHERE key='last_pruned_utc_date'"
        ).fetchone()
        if row is not None and row[0] == today:
            return 0
        if not in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        try:
            deleted = self._prune(connection, now)
            self._set_last_pruned(connection, now)
            if not in_transaction:
                connection.commit()
            return deleted
        except Exception:
            if not in_transaction:
                connection.rollback()
            raise

    def _prune(self, connection: sqlite3.Connection, now: datetime) -> int:
        before = connection.total_changes
        cutoff = _iso_utc(now - timedelta(days=self.retention_days))
        connection.execute(
            f"DELETE FROM {_TABLE} WHERE sampled_at_utc < ?",
            (cutoff,),
        )
        self._prune_capacity(connection)
        return connection.total_changes - before

    def _prune_capacity(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            f"DELETE FROM {_TABLE} WHERE id IN ("
            f"SELECT id FROM {_TABLE} ORDER BY sampled_at_utc DESC, id DESC "
            "LIMIT -1 OFFSET ?)",
            (self.max_rows,),
        )

    @staticmethod
    def _set_last_pruned(connection: sqlite3.Connection, now: datetime) -> None:
        value = now.astimezone(timezone.utc).date().isoformat()
        connection.execute(
            f"INSERT INTO {_META_TABLE}(key, value) VALUES('last_pruned_utc_date', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (value,),
        )


def _quota_values(quota: "CodexQuotaSnapshot") -> dict[str, object]:
    return {
        "quota_source_status": quota.source_status,
        **_window_values("five_hour", quota.five_hour),
        **_window_values("weekly", quota.weekly),
    }


def _window_values(prefix: str, window: "QuotaWindow") -> dict[str, object]:
    return {
        f"{prefix}_used_percent": window.used_percent,
        f"{prefix}_remaining_percent": window.remaining_percent,
        f"{prefix}_reset_at": window.reset_at,
        f"{prefix}_source": window.source,
        f"{prefix}_available": window.available,
        f"{prefix}_stale": window.stale,
        f"{prefix}_error_code": window.error_code,
    }


def _observation_row(observation: HistoryObservation) -> tuple[object, ...]:
    values = {
        "schema_version": SCHEMA_VERSION,
        "sampled_at_utc": _iso_utc(observation.sampled_at),
        "source_observed_at_utc": _iso_utc(observation.source_observed_at),
        "quota_observed_at_utc": _iso_utc(observation.quota_observed_at),
        "thread_safe_id": observation.thread_safe_id,
        "model_safe_id": observation.model_safe_id,
        "source_type": observation.source_type,
        "source_status": observation.source_status,
        "source_available": int(observation.source_available),
        "token_stale": int(observation.token_stale),
        "token_stale_reason": observation.token_stale_reason,
        "input_tokens": observation.input_tokens,
        "output_tokens": observation.output_tokens,
        "total_tokens": observation.total_tokens,
        "cached_tokens": observation.cached_tokens,
        "reasoning_tokens": observation.reasoning_tokens,
        "session_total_tokens": observation.session_total_tokens,
        "turn_count": observation.turn_count,
        "quota_source_status": observation.quota_source_status,
        "five_hour_used_percent": observation.five_hour_used_percent,
        "five_hour_remaining_percent": observation.five_hour_remaining_percent,
        "five_hour_reset_at_utc": _iso_utc(observation.five_hour_reset_at),
        "five_hour_source": observation.five_hour_source,
        "five_hour_available": int(observation.five_hour_available),
        "five_hour_stale": int(observation.five_hour_stale),
        "five_hour_error_code": observation.five_hour_error_code,
        "weekly_used_percent": observation.weekly_used_percent,
        "weekly_remaining_percent": observation.weekly_remaining_percent,
        "weekly_reset_at_utc": _iso_utc(observation.weekly_reset_at),
        "weekly_source": observation.weekly_source,
        "weekly_available": int(observation.weekly_available),
        "weekly_stale": int(observation.weekly_stale),
        "weekly_error_code": observation.weekly_error_code,
        "is_derived": int(observation.is_derived),
        "legacy_unknown_time": int(observation.legacy_unknown_time),
        "sample_fingerprint": observation.sample_fingerprint,
    }
    return tuple(values[column] for column in _INSERT_COLUMNS)


def _sample_from_row(row: sqlite3.Row) -> HistorySample:
    return HistorySample(
        sampled_at=_parse_utc(row["sampled_at_utc"]),
        source_observed_at=_parse_utc(row["source_observed_at_utc"]),
        quota_observed_at=_parse_utc(row["quota_observed_at_utc"]),
        thread_safe_id=row["thread_safe_id"],
        model_safe_id=row["model_safe_id"],
        source_type=row["source_type"],
        source_status=row["source_status"],
        source_available=bool(row["source_available"]),
        token_stale=bool(row["token_stale"]),
        token_stale_reason=row["token_stale_reason"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        total_tokens=row["total_tokens"],
        cached_tokens=row["cached_tokens"],
        reasoning_tokens=row["reasoning_tokens"],
        session_total_tokens=row["session_total_tokens"],
        turn_count=row["turn_count"],
        quota_source_status=row["quota_source_status"],
        five_hour_used_percent=row["five_hour_used_percent"],
        five_hour_remaining_percent=row["five_hour_remaining_percent"],
        five_hour_reset_at=_parse_utc(row["five_hour_reset_at_utc"]),
        five_hour_source=row["five_hour_source"],
        five_hour_available=bool(row["five_hour_available"]),
        five_hour_stale=bool(row["five_hour_stale"]),
        five_hour_error_code=row["five_hour_error_code"],
        weekly_used_percent=row["weekly_used_percent"],
        weekly_remaining_percent=row["weekly_remaining_percent"],
        weekly_reset_at=_parse_utc(row["weekly_reset_at_utc"]),
        weekly_source=row["weekly_source"],
        weekly_available=bool(row["weekly_available"]),
        weekly_stale=bool(row["weekly_stale"]),
        weekly_error_code=row["weekly_error_code"],
        is_derived=bool(row["is_derived"]),
        legacy_unknown_time=bool(row["legacy_unknown_time"]),
        sample_id=int(row["id"]),
        schema_version=int(row["schema_version"]),
        stored_fingerprint=str(row["sample_fingerprint"]),
    )


def _global_quota_samples(rows: list[sqlite3.Row]) -> tuple[HistorySample, ...]:
    result: list[HistorySample] = []
    previous: tuple[object, ...] | None = None
    for row in rows:
        sample = _sample_from_row(row)
        identity = (
            sample.quota_source_status,
            sample.five_hour_used_percent, sample.five_hour_remaining_percent,
            sample.five_hour_reset_at, sample.five_hour_source,
            sample.five_hour_available, sample.five_hour_stale,
            sample.five_hour_error_code,
            sample.weekly_used_percent, sample.weekly_remaining_percent,
            sample.weekly_reset_at, sample.weekly_source,
            sample.weekly_available, sample.weekly_stale,
            sample.weekly_error_code,
        )
        quota_only = replace(
            sample,
            source_observed_at=None,
            thread_safe_id=None,
            model_safe_id=None,
            source_type="global_quota",
            source_status="global_quota",
            source_available=False,
            token_stale=False,
            token_stale_reason=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cached_tokens=None,
            reasoning_tokens=None,
            session_total_tokens=None,
            turn_count=None,
        )
        if identity == previous and result:
            result[-1] = quota_only
        else:
            result.append(quota_only)
            previous = identity
    return tuple(result)


def _query_result(
    range_days: int,
    samples: tuple[HistorySample, ...],
    quota_samples: tuple[HistorySample, ...],
    now: datetime,
) -> HistoryQueryResult:
    if not samples:
        status = "empty"
    else:
        latest = samples[-1]
        if not latest.source_available:
            status = "unavailable"
        elif (
            latest.token_stale
            or now - latest.sampled_at > HISTORY_STALE_AFTER
        ):
            status = "stale"
        elif len(samples) < 2:
            status = "insufficient"
        else:
            status = "available"
    names = (
        "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
        "reasoning_tokens", "session_total_tokens", "turn_count",
    )
    available = {
        name for name in names
        if any(getattr(sample, name) is not None for sample in samples)
    }
    for name in (
        "five_hour_remaining_percent", "weekly_remaining_percent",
    ):
        if any(getattr(sample, name) is not None for sample in quota_samples):
            available.add(name)
    return HistoryQueryResult(
        range_days=range_days,
        status=status,
        samples=samples,
        quota_samples=quota_samples,
        sample_count=len(samples),
        start_at=samples[0].sampled_at if samples else None,
        end_at=samples[-1].sampled_at if samples else None,
        stale=status == "stale",
        metrics_available=tuple(sorted(available)),
    )


def _aware_utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}_timezone_required")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    utc = _aware_utc(value, "datetime")
    assert utc is not None
    return utc.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("stored_datetime_invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _aware_utc(parsed, "stored_datetime")


def _safe_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if _IDENTIFIER.fullmatch(normalized):
        return normalized
    digest = hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest}"


def _safe_code(value: object) -> str:
    normalized = str(value or "unknown").strip().lower()
    return normalized if _CODE.fullmatch(normalized) else "invalid"


def _validate_nonnegative(value: int | None, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name}_invalid")


def _validate_percent(value: float | None, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name}_invalid")
    if not 0.0 <= float(value) <= 100.0:
        raise ValueError(f"{field_name}_invalid")


def _storage_error_code(error: Exception, *, migration: bool = False) -> str:
    if isinstance(error, sqlite3.OperationalError):
        message = str(error).lower()
        if "locked" in message or "busy" in message:
            return "history_storage_locked"
        if "unable to open" in message or "readonly" in message:
            return "history_storage_open_failed"
    if migration:
        return "history_migration_failed"
    if isinstance(error, ValueError):
        return "history_storage_invalid"
    return "history_storage_error"
