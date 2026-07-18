"""Versioned local persistence for privacy-safe usage history samples."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterable

from app.codex_rollout import (
    CompletedResponseUsageBatch,
    ResponseUsageCandidate,
    SafeRolloutScanMetadata,
    make_response_safe_id,
    make_thread_safe_id,
)
from app.paths import history_db_path
from app.usage_summary import (
    ObservedUsageRecord,
    ObservedUsageSummary,
    UsageWindowKind,
    aggregate_observed_usage,
    unavailable_usage_summary,
    usage_window_bounds,
)

if TYPE_CHECKING:
    from app.dashboard import DashboardSnapshot, MiniThreadSnapshot
    from app.quota import CodexQuotaSnapshot, QuotaWindow


SCHEMA_VERSION = 4
RETENTION_DAYS = 90
MAX_HISTORY_ROWS = 200_000
MAX_TREND_QUERY_ROWS = 500
HISTORY_STALE_AFTER = timedelta(minutes=3)
SUPPORTED_RANGES = (7, 30, 90)

_TABLE = "usage_history_samples"
_META_TABLE = "usage_history_meta"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RESPONSE_SAFE_IDENTIFIER = re.compile(r"^sha256:[0-9a-f]{64}$")
_CODE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")
_RESPONSE_PAYLOAD_PREDICATE = "(" + " OR ".join(
    f"{name} IS NOT NULL"
    for name in (
        "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
        "reasoning_tokens",
    )
) + ")"
_QUOTA_PAYLOAD_PREDICATE = "(" + " OR ".join(
    (
        name if name.endswith(("_available", "_stale"))
        else f"{name} IS NOT NULL"
    )
    for name in (
        "five_hour_observed_at_utc", "five_hour_last_seen_at_utc",
        "five_hour_used_percent", "five_hour_remaining_percent",
        "five_hour_reset_at_utc", "five_hour_available", "five_hour_stale",
        "five_hour_error_code", "weekly_observed_at_utc",
        "weekly_last_seen_at_utc", "weekly_used_percent",
        "weekly_remaining_percent", "weekly_reset_at_utc",
        "weekly_available", "weekly_stale", "weekly_error_code",
    )
) + ")"
_UTC_TEXT_GLOB = "????-??-??T??:??:??.??????Z"
_USAGE_SUMMARY_COLUMNS = (
    "id", "sampled_at_utc", "source_observed_at_utc", "thread_safe_id",
    "response_safe_id", "model_safe_id", "source_type", "source_status",
    "source_available", "token_stale", "input_tokens",
    "output_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
    "is_derived", "legacy_unknown_time",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _history_instruction_status(instruction: object) -> str:
    if instruction is None:
        return "unavailable"
    if bool(getattr(instruction, "in_progress", False)):
        return "in_progress"
    return "exact" if bool(getattr(instruction, "exact", False)) else "completed_partial"


@dataclass(frozen=True)
class HistoryObservation:
    """One normalized safe observation ready for deterministic persistence."""

    sampled_at: datetime = field(default_factory=_utc_now)
    source_observed_at: datetime | None = None
    quota_observed_at: datetime | None = None
    thread_safe_id: str | None = None
    response_safe_id: str | None = None
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
    five_hour_observed_at: datetime | None = None
    five_hour_last_seen_at: datetime | None = None
    five_hour_event_seq: int = 0
    five_hour_used_percent: float | None = None
    five_hour_remaining_percent: float | None = None
    five_hour_reset_at: datetime | None = None
    five_hour_source: str = "unknown"
    five_hour_available: bool = False
    five_hour_stale: bool = False
    five_hour_error_code: str | None = None
    weekly_observed_at: datetime | None = None
    weekly_last_seen_at: datetime | None = None
    weekly_event_seq: int = 0
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
            "five_hour_observed_at", "five_hour_last_seen_at",
            "five_hour_reset_at", "weekly_observed_at",
            "weekly_last_seen_at", "weekly_reset_at",
        ):
            object.__setattr__(self, name, _aware_utc(getattr(self, name), name))
        object.__setattr__(
            self,
            "thread_safe_id",
            (
                _safe_identifier(self.thread_safe_id)
                if isinstance(self, HistorySample)
                else _persisted_thread_safe_identifier(self.thread_safe_id)
            ),
        )
        object.__setattr__(
            self, "response_safe_id", _safe_response_identifier(self.response_safe_id),
        )
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
            "five_hour_event_seq", "weekly_event_seq",
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
        for prefix in ("five_hour", "weekly"):
            if (
                not getattr(self, f"{prefix}_available")
                or getattr(self, f"{prefix}_stale")
            ):
                object.__setattr__(self, f"{prefix}_last_seen_at", None)

    @classmethod
    def from_dashboard(
        cls,
        snapshot: "DashboardSnapshot",
        quota: "CodexQuotaSnapshot",
        *,
        sampled_at: datetime | None = None,
    ) -> "HistoryObservation":
        source_session = (
            getattr(snapshot, "current_session", None) or snapshot.selected_session
        )
        instruction = (
            source_session.instruction
            if source_session is not None else snapshot.rollout.instruction
        )
        usage = instruction.usage if instruction is not None else None
        cumulative = (
            source_session.thread_cumulative_usage
            if source_session is not None
            else snapshot.rollout.thread_cumulative_usage
        )
        thread_id = (
            source_session.thread_id if source_session is not None
            else snapshot.selected_thread_id or snapshot.rollout.thread_id
        )
        state = snapshot.state_metadata.get(thread_id) if thread_id else None
        model = state.model if state is not None else None
        if (
            model is None
            and snapshot.state_total is not None
            and getattr(snapshot.state_total, "thread_id", None) == thread_id
        ):
            model = snapshot.state_total.model
        source_status = _history_instruction_status(instruction)
        if source_session is not None and source_session.status == "unavailable":
            source_status = "unavailable"
        source_available = bool(
            source_status not in {"unavailable", "no_selection"}
            and (usage is not None or cumulative is not None)
        )
        return cls(
            sampled_at=sampled_at or _utc_now(),
            source_observed_at=(
                source_session.observed_at
                if source_session is not None else snapshot.rollout.observed_at
            ),
            quota_observed_at=quota.refreshed_at,
            thread_safe_id=_persisted_thread_safe_identifier(thread_id),
            response_safe_id=make_response_safe_id(
                thread_id, instruction.turn_id if instruction is not None else None,
            ),
            model_safe_id=model,
            source_type="dashboard",
            source_status=source_status,
            source_available=source_available,
            token_stale=bool(
                instruction is not None
                and instruction.in_progress
                and source_session is not None
                and source_session.status == "incomplete"
            ),
            token_stale_reason=(
                "source_stale_or_unreconciled"
                if instruction is not None
                and instruction.in_progress
                and source_session is not None
                and source_session.status == "incomplete"
                else None
            ),
            input_tokens=usage.input_tokens if usage is not None else None,
            output_tokens=usage.output_tokens if usage is not None else None,
            total_tokens=usage.total_tokens if usage is not None else None,
            cached_tokens=usage.cached_input_tokens if usage is not None else None,
            reasoning_tokens=usage.reasoning_output_tokens if usage is not None else None,
            session_total_tokens=cumulative.total_tokens if cumulative is not None else None,
            turn_count=(
                source_session.turn_count
                if source_session is not None else snapshot.rollout.turn_count
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
        source_status = mini.response_status or (
            "in_progress" if mini.status == "incomplete" else mini.status
        )
        available = bool(
            source_status not in {"unavailable", "no_selection"}
            and (
                mini.instruction_total_tokens is not None
                or mini.session_total_tokens is not None
            )
        )
        return cls(
            sampled_at=sampled_at or _utc_now(),
            source_observed_at=mini.observed_at,
            quota_observed_at=quota.refreshed_at,
            thread_safe_id=_persisted_thread_safe_identifier(thread_safe_id),
            response_safe_id=mini.response_safe_id,
            model_safe_id=model_safe_id,
            source_type="mini",
            source_status=source_status,
            source_available=available,
            token_stale=mini.status in {"stale", "incomplete"},
            token_stale_reason=(
                "source_stale_or_unreconciled"
                if mini.status == "incomplete" else
                "source_stale" if mini.status == "stale" else None
            ),
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
            "response_safe_id": self.response_safe_id,
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
    queried_at: datetime | None = None
    token_start_at: datetime | None = None
    token_end_at: datetime | None = None
    quota_start_at: datetime | None = None
    quota_end_at: datetime | None = None
    five_hour_last_seen_at: datetime | None = None
    weekly_last_seen_at: datetime | None = None
    five_hour_available: bool | None = None
    five_hour_stale: bool = False
    weekly_available: bool | None = None
    weekly_stale: bool = False


@dataclass(frozen=True)
class HistoryBatchWriteResult:
    inserted_count: int
    canonical_response_count: int


_COLUMN_DEFINITIONS = {
    "schema_version": "INTEGER NOT NULL DEFAULT 1",
    "sampled_at_utc": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00.000000Z'",
    "source_observed_at_utc": "TEXT",
    "quota_observed_at_utc": "TEXT",
    "thread_safe_id": "TEXT",
    "response_safe_id": "TEXT",
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
    "five_hour_observed_at_utc": "TEXT",
    "five_hour_last_seen_at_utc": "TEXT",
    "five_hour_event_seq": "INTEGER NOT NULL DEFAULT 0",
    "five_hour_used_percent": "REAL",
    "five_hour_remaining_percent": "REAL",
    "five_hour_reset_at_utc": "TEXT",
    "five_hour_source": "TEXT NOT NULL DEFAULT 'unknown'",
    "five_hour_available": "INTEGER NOT NULL DEFAULT 0",
    "five_hour_stale": "INTEGER NOT NULL DEFAULT 0",
    "five_hour_error_code": "TEXT",
    "weekly_observed_at_utc": "TEXT",
    "weekly_last_seen_at_utc": "TEXT",
    "weekly_event_seq": "INTEGER NOT NULL DEFAULT 0",
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
                    try:
                        event_sequences = self._allocate_quota_event_sequences(
                            connection, observation,
                        )
                        storage_fingerprint = _storage_fingerprint(
                            observation, event_sequences,
                        )
                        values = _observation_row(
                            observation,
                            event_sequences=event_sequences,
                            storage_fingerprint=storage_fingerprint,
                        )
                        placeholders = ", ".join("?" for _ in _INSERT_COLUMNS)
                        columns = ", ".join(_INSERT_COLUMNS)
                        cursor = connection.execute(
                            f"INSERT INTO {_TABLE} ({columns}) VALUES ({placeholders}) "
                            "ON CONFLICT(sample_fingerprint) DO NOTHING",
                            values,
                        )
                        inserted = cursor.rowcount == 1
                        if not inserted:
                            self._refresh_duplicate_quota_last_seen(
                                connection, observation, storage_fingerprint,
                            )
                        if inserted:
                            self._prune_capacity(connection)
                            self._prune_if_due(
                                connection, self.clock(), in_transaction=True,
                            )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                self.last_error = None
                return inserted
            except (OSError, sqlite3.Error, ValueError) as exc:
                self.last_error = _storage_error_code(exc)
                return False

    def record_completed_batch(
        self,
        batch: CompletedResponseUsageBatch,
        *,
        mark_success: bool = True,
    ) -> HistoryBatchWriteResult:
        """Atomically persist one safe Rollout batch and its success watermark."""

        if not isinstance(batch, CompletedResponseUsageBatch):
            raise TypeError("completed_response_batch_required")
        if len(batch.responses) > 100:
            raise ValueError("completed_response_batch_too_large")
        observations = tuple(
            _history_observation_from_candidate(candidate, sampled_at=self.clock())
            for candidate in batch.responses
        )
        with self._lock:
            if not self._initialized and not self.initialize():
                return HistoryBatchWriteResult(0, 0)
            try:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    inserted = 0
                    try:
                        for observation in observations:
                            storage_fingerprint = _storage_fingerprint(
                                observation,
                                {"five_hour": 0, "weekly": 0},
                            )
                            values = _observation_row(
                                observation,
                                event_sequences={"five_hour": 0, "weekly": 0},
                                storage_fingerprint=storage_fingerprint,
                            )
                            cursor = connection.execute(
                                f"INSERT INTO {_TABLE} ({', '.join(_INSERT_COLUMNS)}) "
                                f"VALUES ({', '.join('?' for _ in _INSERT_COLUMNS)}) "
                                "ON CONFLICT(sample_fingerprint) DO NOTHING",
                                values,
                            )
                            inserted += int(cursor.rowcount == 1)
                        if mark_success:
                            _set_meta_value(
                                connection,
                                _backfill_meta_key(batch.scan_metadata.safe_file_hash),
                                _backfill_meta_value(batch, self.clock()),
                            )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                        raise
                self.last_error = None
                canonical_count = len({
                    candidate.response_safe_id for candidate in batch.responses
                })
                return HistoryBatchWriteResult(inserted, canonical_count)
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                self.last_error = _storage_error_code(exc)
                return HistoryBatchWriteResult(0, 0)

    def record_backfill_failure(
        self,
        metadata: SafeRolloutScanMetadata,
        error_code: str,
    ) -> bool:
        """Persist a retryable safe status without marking a file complete."""

        if not isinstance(metadata, SafeRolloutScanMetadata):
            raise TypeError("safe_rollout_scan_metadata_required")
        safe_error = _safe_code(error_code)
        with self._lock:
            if not self._initialized and not self.initialize():
                return False
            try:
                with closing(self._connect()) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    _set_meta_value(
                        connection,
                        _backfill_meta_key(metadata.safe_file_hash),
                        json.dumps(
                            {
                                "size": metadata.size,
                                "mtime_ns": metadata.mtime_ns,
                                "processed_completed_count": 0,
                                "backfill_version": 1,
                                "result_status": "failed",
                                "retry_status": safe_error,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=True,
                        ),
                    )
                    connection.commit()
                self.last_error = None
                return True
            except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
                self.last_error = _storage_error_code(exc)
                return False

    def backfill_watermark_is_current(
        self,
        metadata: SafeRolloutScanMetadata,
    ) -> bool:
        """Return whether a file signature has a committed successful batch."""

        if not isinstance(metadata, SafeRolloutScanMetadata):
            return False
        return metadata.safe_file_hash in self.current_backfill_file_hashes(
            (metadata,),
        )

    def current_backfill_file_hashes(
        self,
        metadata_items: Iterable[SafeRolloutScanMetadata],
    ) -> set[str]:
        """Load successful matching watermarks without scanning history rows."""

        return {
            safe_file_hash
            for safe_file_hash, status in self.backfill_file_statuses(
                metadata_items,
            ).items()
            if status == "success"
        }

    def backfill_file_statuses(
        self,
        metadata_items: Iterable[SafeRolloutScanMetadata],
    ) -> dict[str, str]:
        """Load matching safe statuses so new files outrank retryable failures."""

        expected = {
            metadata.safe_file_hash: metadata
            for metadata in metadata_items
            if isinstance(metadata, SafeRolloutScanMetadata)
        }
        if not expected:
            return {}
        with self._lock:
            if not self._initialized and not self.initialize():
                return {}
            try:
                rows: list[sqlite3.Row] = []
                with closing(self._connect()) as connection:
                    keys = [
                        _backfill_meta_key(safe_file_hash)
                        for safe_file_hash in expected
                    ]
                    for offset in range(0, len(keys), 500):
                        chunk = keys[offset:offset + 500]
                        rows.extend(connection.execute(
                            f"SELECT key, value FROM {_META_TABLE} "
                            f"WHERE key IN ({', '.join('?' for _ in chunk)})",
                            chunk,
                        ).fetchall())
                statuses: dict[str, str] = {}
                prefix = "response_backfill_v1:"
                for row in rows:
                    key = str(row["key"])
                    if not key.startswith(prefix):
                        continue
                    safe_file_hash = key[len(prefix):]
                    metadata = expected.get(safe_file_hash)
                    if metadata is None:
                        continue
                    value = json.loads(str(row["value"]))
                    if (
                        isinstance(value, dict)
                        and value.get("size") == metadata.size
                        and value.get("mtime_ns") == metadata.mtime_ns
                    ):
                        status = value.get("result_status")
                        if status in {"success", "failed"}:
                            statuses[safe_file_hash] = status
                return statuses
            except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
                return {}

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
        cutoff_at = now - timedelta(days=range_days)
        cutoff = _iso_utc(cutoff_at)
        try:
            with closing(self._connect()) as connection:
                invalid_row = connection.execute(
                    f"SELECT 1 FROM (SELECT sampled_at_utc FROM {_TABLE} "
                    "ORDER BY sampled_at_utc DESC, id DESC LIMIT ?) "
                    "WHERE sampled_at_utc IS NULL OR sampled_at_utc NOT GLOB ? LIMIT 1",
                    (MAX_TREND_QUERY_ROWS, _UTC_TEXT_GLOB),
                ).fetchone()
                if invalid_row is not None:
                    raise ValueError("history_sample_time_invalid")
                safe_thread = (
                    None if thread_safe_id is None
                    else _safe_identifier(thread_safe_id)
                )
                thread_aliases: tuple[str, ...] | None = None
                if safe_thread is not None:
                    hashed_thread = (
                        safe_thread
                        if _RESPONSE_SAFE_IDENTIFIER.fullmatch(safe_thread)
                        else make_thread_safe_id(safe_thread)
                    )
                    thread_aliases = tuple(dict.fromkeys(
                        item
                        for item in (safe_thread, hashed_thread)
                        if item is not None
                    ))
                rows = _bounded_token_rows(
                    connection,
                    cutoff,
                    thread_aliases,
                )
                quota_rows = _bounded_quota_rows(connection)
            samples = _thread_token_samples(
                rows,
                canonical_thread_safe_id=safe_thread,
            )[-MAX_TREND_QUERY_ROWS:]
            quota = _global_quota_projection(quota_rows, cutoff_at)
            self.last_error = None
            return _query_result(range_days, samples, quota, now)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.last_error = _storage_error_code(exc)
            return HistoryQueryResult(
                range_days, "unavailable", error_code=self.last_error,
            )

    def summarize_usage(
        self,
        scope: UsageWindowKind | str,
        *,
        as_of_utc: datetime | None = None,
        local_timezone: tzinfo | None = None,
    ) -> ObservedUsageSummary:
        """Aggregate one global response window without exposing SQLite to UI."""

        as_of = _aware_utc(as_of_utc or self.clock(), "as_of_utc")
        assert as_of is not None
        bounds = usage_window_bounds(
            scope,
            as_of_utc=as_of,
            local_timezone=local_timezone,
        )
        if not self._initialized and not self.initialize():
            return unavailable_usage_summary(
                bounds.scope,
                as_of_utc=as_of,
                local_timezone=local_timezone,
                error_code=self.last_error or "history_storage_error",
            )
        try:
            with closing(self._connect()) as connection:
                connection.row_factory = None
                cursor = connection.execute(
                    f"SELECT {', '.join(_USAGE_SUMMARY_COLUMNS)} FROM {_TABLE} "
                    f"WHERE {_RESPONSE_PAYLOAD_PREDICATE} "
                    "ORDER BY response_safe_id, thread_safe_id, "
                    "source_observed_at_utc, sampled_at_utc, id",
                )

                def records() -> Iterable[ObservedUsageRecord]:
                    while True:
                        batch = cursor.fetchmany(512)
                        if not batch:
                            return
                        for row in batch:
                            record = _observed_usage_record_from_row(row)
                            if (
                                record.thread_safe_id is not None
                                and not _RESPONSE_SAFE_IDENTIFIER.fullmatch(
                                    record.thread_safe_id
                                )
                            ):
                                record = replace(
                                    record,
                                    thread_safe_id=make_thread_safe_id(
                                        record.thread_safe_id
                                    ),
                                )
                            yield record

                summary = aggregate_observed_usage(
                    records(),
                    bounds.scope,
                    as_of_utc=as_of,
                    local_timezone=local_timezone,
                    records_grouped_by_response=True,
                )
            self.last_error = None
            return summary
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            self.last_error = _storage_error_code(exc)
            return unavailable_usage_summary(
                bounds.scope,
                as_of_utc=as_of,
                local_timezone=local_timezone,
                error_code=self.last_error,
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

    @staticmethod
    def _refresh_duplicate_quota_last_seen(
        connection: sqlite3.Connection,
        observation: HistoryObservation,
        storage_fingerprint: str,
    ) -> None:
        updates: dict[str, str] = {}
        parameters: list[object] = []
        for prefix in ("five_hour", "weekly"):
            last_seen = getattr(observation, f"{prefix}_last_seen_at")
            if last_seen is None:
                continue
            column = f"{prefix}_last_seen_at_utc"
            updates[column] = (
                f"CASE WHEN {column} IS NULL OR {column} < ? THEN ? ELSE {column} END"
            )
            value = _iso_utc(last_seen)
            parameters.extend((value, value))
        if observation.quota_observed_at is not None:
            updates["quota_observed_at_utc"] = (
                "CASE WHEN quota_observed_at_utc IS NULL OR quota_observed_at_utc < ? "
                "THEN ? ELSE quota_observed_at_utc END"
            )
            value = _iso_utc(observation.quota_observed_at)
            parameters.extend((value, value))
        if not updates:
            return
        parameters.append(storage_fingerprint)
        connection.execute(
            f"UPDATE {_TABLE} SET "
            + ", ".join(f"{column} = {expression}" for column, expression in updates.items())
            + " WHERE sample_fingerprint = ?",
            tuple(parameters),
        )

    def _allocate_quota_event_sequences(
        self,
        connection: sqlite3.Connection,
        observation: HistoryObservation,
    ) -> dict[str, int]:
        sequences: dict[str, int] = {}
        for prefix in ("five_hour", "weekly"):
            active_identity, active_seq = _load_or_recover_active_quota_event(
                connection, prefix,
            )
            identity = _quota_window_identity_key(observation, prefix)
            if identity is not None and identity != active_identity:
                active_seq += 1
                active_identity = identity
                _set_active_quota_event(
                    connection, prefix, active_identity, active_seq,
                )
            sequences[prefix] = active_seq
        return sequences

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
            for prefix in ("five_hour", "weekly"):
                observed_column = f"{prefix}_observed_at_utc"
                last_seen_column = f"{prefix}_last_seen_at_utc"
                connection.execute(
                    f"UPDATE {_TABLE} SET {observed_column} = quota_observed_at_utc "
                    f"WHERE {observed_column} IS NULL "
                    f"AND {prefix}_available = 1 AND {prefix}_stale = 0 "
                    "AND quota_observed_at_utc IS NOT NULL"
                )
                connection.execute(
                    f"UPDATE {_TABLE} SET {last_seen_column} = {observed_column} "
                    f"WHERE {last_seen_column} IS NULL "
                    f"AND {prefix}_available = 1 AND {prefix}_stale = 0 "
                    f"AND {observed_column} IS NOT NULL"
                )
            if version < 3:
                _backfill_quota_event_sequences(connection)
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
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_thread_source_observed "
                f"ON {_TABLE}(thread_safe_id, source_observed_at_utc, id)"
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_source_observed "
                f"ON {_TABLE}(source_observed_at_utc, id)"
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_response_observed "
                f"ON {_TABLE}(thread_safe_id, response_safe_id, "
                "source_observed_at_utc, sampled_at_utc, id)"
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_response_identity "
                f"ON {_TABLE}(response_safe_id, thread_safe_id, "
                "source_observed_at_utc, sampled_at_utc, id)"
            )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_response_summary_v4 "
                f"ON {_TABLE}(response_safe_id, thread_safe_id, "
                "source_observed_at_utc, sampled_at_utc, id, model_safe_id, "
                "source_type, source_status, source_available, token_stale, "
                "input_tokens, output_tokens, total_tokens, cached_tokens, "
                "reasoning_tokens, is_derived, legacy_unknown_time)"
            )
            for prefix in ("five_hour", "weekly"):
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_{prefix}_event "
                    f"ON {_TABLE}({prefix}_event_seq DESC, "
                    f"{prefix}_observed_at_utc, id)"
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


def _history_observation_from_candidate(
    candidate: ResponseUsageCandidate,
    *,
    sampled_at: datetime,
) -> HistoryObservation:
    if not isinstance(candidate, ResponseUsageCandidate):
        raise TypeError("response_usage_candidate_required")
    if not _RESPONSE_SAFE_IDENTIFIER.fullmatch(candidate.thread_safe_id):
        raise ValueError("thread_safe_id_invalid")
    if not _RESPONSE_SAFE_IDENTIFIER.fullmatch(candidate.response_safe_id):
        raise ValueError("response_safe_id_invalid")
    return HistoryObservation(
        sampled_at=sampled_at,
        source_observed_at=candidate.completion_time_utc,
        thread_safe_id=candidate.thread_safe_id,
        response_safe_id=candidate.response_safe_id,
        model_safe_id=candidate.safe_model_id,
        source_type="rollout_backfill",
        source_status=candidate.status,
        source_available=True,
        input_tokens=candidate.input_tokens,
        output_tokens=candidate.output_tokens,
        total_tokens=candidate.total_tokens,
        cached_tokens=candidate.cached_input_tokens,
        reasoning_tokens=candidate.reasoning_tokens,
    )


def _backfill_meta_key(safe_file_hash: str) -> str:
    return f"response_backfill_v1:{safe_file_hash}"


def _backfill_meta_value(
    batch: CompletedResponseUsageBatch,
    observed_at: datetime,
) -> str:
    metadata = batch.scan_metadata
    return json.dumps(
        {
            "size": metadata.size,
            "mtime_ns": metadata.mtime_ns,
            "processed_completed_count": metadata.completed_response_count,
            "backfill_version": 1,
            "result_status": "success",
            "last_success_utc": _iso_utc(observed_at),
            "retry_status": "none",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _window_values(prefix: str, window: "QuotaWindow") -> dict[str, object]:
    return {
        f"{prefix}_observed_at": window.observed_at,
        f"{prefix}_last_seen_at": (
            window.observed_at if window.available and not window.stale else None
        ),
        f"{prefix}_used_percent": window.used_percent,
        f"{prefix}_remaining_percent": window.remaining_percent,
        f"{prefix}_reset_at": window.reset_at,
        f"{prefix}_source": window.source,
        f"{prefix}_available": window.available,
        f"{prefix}_stale": window.stale,
        f"{prefix}_error_code": window.error_code,
    }


def _observation_row(
    observation: HistoryObservation,
    *,
    event_sequences: dict[str, int] | None = None,
    storage_fingerprint: str | None = None,
) -> tuple[object, ...]:
    event_sequences = event_sequences or {
        "five_hour": observation.five_hour_event_seq,
        "weekly": observation.weekly_event_seq,
    }
    values = {
        "schema_version": SCHEMA_VERSION,
        "sampled_at_utc": _iso_utc(observation.sampled_at),
        "source_observed_at_utc": _iso_utc(observation.source_observed_at),
        "quota_observed_at_utc": _iso_utc(observation.quota_observed_at),
        "thread_safe_id": observation.thread_safe_id,
        "response_safe_id": observation.response_safe_id,
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
        "five_hour_observed_at_utc": _iso_utc(observation.five_hour_observed_at),
        "five_hour_last_seen_at_utc": _iso_utc(observation.five_hour_last_seen_at),
        "five_hour_event_seq": event_sequences["five_hour"],
        "five_hour_used_percent": observation.five_hour_used_percent,
        "five_hour_remaining_percent": observation.five_hour_remaining_percent,
        "five_hour_reset_at_utc": _iso_utc(observation.five_hour_reset_at),
        "five_hour_source": observation.five_hour_source,
        "five_hour_available": int(observation.five_hour_available),
        "five_hour_stale": int(observation.five_hour_stale),
        "five_hour_error_code": observation.five_hour_error_code,
        "weekly_observed_at_utc": _iso_utc(observation.weekly_observed_at),
        "weekly_last_seen_at_utc": _iso_utc(observation.weekly_last_seen_at),
        "weekly_event_seq": event_sequences["weekly"],
        "weekly_used_percent": observation.weekly_used_percent,
        "weekly_remaining_percent": observation.weekly_remaining_percent,
        "weekly_reset_at_utc": _iso_utc(observation.weekly_reset_at),
        "weekly_source": observation.weekly_source,
        "weekly_available": int(observation.weekly_available),
        "weekly_stale": int(observation.weekly_stale),
        "weekly_error_code": observation.weekly_error_code,
        "is_derived": int(observation.is_derived),
        "legacy_unknown_time": int(observation.legacy_unknown_time),
        "sample_fingerprint": storage_fingerprint or observation.sample_fingerprint,
    }
    return tuple(values[column] for column in _INSERT_COLUMNS)


def _sample_from_row(row: sqlite3.Row) -> HistorySample:
    return HistorySample(
        sampled_at=_parse_utc(row["sampled_at_utc"]),
        source_observed_at=_parse_utc(row["source_observed_at_utc"]),
        quota_observed_at=_parse_utc(row["quota_observed_at_utc"]),
        thread_safe_id=row["thread_safe_id"],
        response_safe_id=row["response_safe_id"],
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
        five_hour_observed_at=_parse_utc(row["five_hour_observed_at_utc"]),
        five_hour_last_seen_at=_parse_utc(row["five_hour_last_seen_at_utc"]),
        five_hour_event_seq=int(row["five_hour_event_seq"]),
        five_hour_used_percent=row["five_hour_used_percent"],
        five_hour_remaining_percent=row["five_hour_remaining_percent"],
        five_hour_reset_at=_parse_utc(row["five_hour_reset_at_utc"]),
        five_hour_source=row["five_hour_source"],
        five_hour_available=bool(row["five_hour_available"]),
        five_hour_stale=bool(row["five_hour_stale"]),
        five_hour_error_code=row["five_hour_error_code"],
        weekly_observed_at=_parse_utc(row["weekly_observed_at_utc"]),
        weekly_last_seen_at=_parse_utc(row["weekly_last_seen_at_utc"]),
        weekly_event_seq=int(row["weekly_event_seq"]),
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


def _observed_usage_record_from_row(row: tuple[object, ...]) -> ObservedUsageRecord:
    observed_at, observed_invalid = _parse_utc_safely(row[2])
    recorded_at = _parse_utc_safely(row[1])[0]
    return ObservedUsageRecord(
        source_observed_at=observed_at,
        recorded_at=recorded_at,
        thread_safe_id=(
            row[3] if isinstance(row[3], str) else None
        ),
        response_safe_id=(
            row[4] if isinstance(row[4], str) else None
        ),
        model_safe_id=(
            row[5] if isinstance(row[5], str) else None
        ),
        source_type=str(row[6] or "unknown"),
        source_status=str(row[7] or "unavailable"),
        source_available=bool(row[8]),
        token_stale=bool(row[9]),
        token_stale_reason=None,
        input_tokens=row[10],
        output_tokens=row[11],
        total_tokens=row[12],
        cached_tokens=row[13],
        reasoning_tokens=row[14],
        session_total_tokens=None,
        is_derived=bool(row[15]),
        legacy_unknown_time=bool(row[16]),
        observed_time_invalid=observed_invalid,
        stored_fingerprint="",
        sample_id=int(row[0]),
    )


_TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_tokens",
    "reasoning_tokens", "session_total_tokens", "turn_count",
)


def _thread_token_samples(
    rows: list[sqlite3.Row],
    *,
    canonical_thread_safe_id: str | None = None,
) -> tuple[HistorySample, ...]:
    """Project combined rows into deterministic Thread Token observations."""

    groups: dict[tuple[object, ...], list[HistorySample]] = {}
    for row in rows:
        sample = _sample_from_row(row)
        if sample.legacy_unknown_time or sample.source_observed_at is None:
            continue
        token = _token_only(sample)
        if canonical_thread_safe_id is not None:
            token = replace(
                token,
                thread_safe_id=canonical_thread_safe_id,
            )
        key = (
            token.thread_safe_id,
            token.response_safe_id
            if token.response_safe_id is not None
            else ("legacy_source_time", token.source_observed_at),
        )
        candidates = groups.setdefault(key, [])
        if token.response_safe_id is not None and candidates:
            candidates[0] = _merge_token_samples(candidates[0], token)
        else:
            for index, existing in enumerate(candidates):
                if _token_values_compatible(existing, token):
                    candidates[index] = _merge_token_samples(existing, token)
                    break
            else:
                candidates.append(token)
    projected = [sample for candidates in groups.values() for sample in candidates]
    projected.sort(key=lambda sample: (
        sample.source_observed_at,
        sample.sampled_at,
        sample.sample_id,
    ))
    return tuple(projected)


def _bounded_token_rows(
    connection: sqlite3.Connection,
    cutoff: str,
    thread_safe_ids: tuple[str, ...] | None,
) -> list[sqlite3.Row]:
    """Return bounded canonical v4 winners plus bounded legacy candidates."""

    thread_clause = (
        ""
        if thread_safe_ids is None
        else "AND thread_safe_id IN ("
        + ", ".join("?" for _item in thread_safe_ids)
        + ") "
    )
    common_parameters: tuple[object, ...] = (
        (cutoff,)
        if thread_safe_ids is None
        else (cutoff, *thread_safe_ids)
    )
    lifecycle_rank = (
        "CASE source_status WHEN 'exact' THEN 3 "
        "WHEN 'completed_partial' THEN 2 WHEN 'in_progress' THEN 1 ELSE 0 END"
    )
    completeness = " + ".join(
        f"CASE WHEN {name} IS NULL THEN 0 ELSE 1 END" for name in _TOKEN_FIELDS
    )
    source_rank = "CASE source_type WHEN 'dashboard' THEN 2 WHEN 'mini' THEN 1 ELSE 0 END"
    stable_rows = connection.execute(
        "WITH ranked AS ("
        f"SELECT id, source_observed_at_utc, ROW_NUMBER() OVER (PARTITION BY "
        "thread_safe_id, response_safe_id ORDER BY "
        f"{lifecycle_rank} DESC, source_observed_at_utc DESC, "
        f"({completeness}) DESC, {source_rank} DESC, source_available DESC, "
        "token_stale ASC, sampled_at_utc DESC, id DESC) AS candidate_rank "
        f"FROM {_TABLE} WHERE response_safe_id IS NOT NULL "
        "AND source_status IN ('exact', 'completed_partial') "
        f"AND source_observed_at_utc >= ? {thread_clause}"
        "), winners AS ("
        "SELECT id, source_observed_at_utc FROM ranked WHERE candidate_rank = 1 "
        "ORDER BY source_observed_at_utc DESC, id DESC LIMIT ?) "
        f"SELECT sample.* FROM {_TABLE} AS sample JOIN winners USING(id) "
        "ORDER BY sample.source_observed_at_utc, sample.sampled_at_utc, sample.id",
        (*common_parameters, MAX_TREND_QUERY_ROWS),
    ).fetchall()
    legacy_rows = connection.execute(
        f"SELECT * FROM (SELECT * FROM {_TABLE} WHERE response_safe_id IS NULL "
        "AND source_status IN ('exact', 'completed_partial') "
        f"AND source_observed_at_utc >= ? {thread_clause}"
        "ORDER BY source_observed_at_utc DESC, sampled_at_utc DESC, id DESC LIMIT ?) "
        "ORDER BY source_observed_at_utc, sampled_at_utc, id",
        (*common_parameters, MAX_TREND_QUERY_ROWS),
    ).fetchall()
    return [*stable_rows, *legacy_rows]


def _bounded_quota_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return first/last rows for the latest quota transitions per window."""

    selected: dict[int, sqlite3.Row] = {}
    for prefix in ("five_hour", "weekly"):
        observed = f"{prefix}_observed_at_utc"
        last_seen = f"{prefix}_last_seen_at_utc"
        event_seq = f"{prefix}_event_seq"
        available = f"{prefix}_available"
        stale = f"{prefix}_stale"
        valid_event = (
            f"{observed} IS NOT NULL AND {available} = 1 AND {stale} = 0 "
            f"AND ({prefix}_used_percent IS NOT NULL OR "
            f"{prefix}_remaining_percent IS NOT NULL OR "
            f"{prefix}_reset_at_utc IS NOT NULL)"
        )
        status_time = (
            f"CASE WHEN {last_seen} IS NOT NULL THEN {last_seen} "
            f"WHEN quota_observed_at_utc IS NOT NULL THEN quota_observed_at_utc "
            "ELSE sampled_at_utc END"
        )
        rows = connection.execute(
            "WITH recent_events AS ("
            f"SELECT {event_seq} AS seq FROM {_TABLE} WHERE {valid_event} "
            f"GROUP BY {event_seq} ORDER BY {event_seq} DESC LIMIT ?"
            "), ranked AS ("
            f"SELECT id, ROW_NUMBER() OVER (PARTITION BY {event_seq} "
            f"ORDER BY {observed}, id) AS first_rank, "
            f"ROW_NUMBER() OVER (PARTITION BY {event_seq} "
            f"ORDER BY {status_time} DESC, id DESC) AS last_rank "
            f"FROM {_TABLE} JOIN recent_events ON {event_seq} = recent_events.seq "
            f"WHERE {valid_event}) "
            f"SELECT sample.* FROM {_TABLE} AS sample JOIN ranked USING(id) "
            "WHERE first_rank = 1 OR last_rank = 1",
            (MAX_TREND_QUERY_ROWS,),
        ).fetchall()
        for row in rows:
            selected[int(row["id"])] = row
        status_row = connection.execute(
            f"SELECT * FROM {_TABLE} WHERE {observed} IS NOT NULL "
            f"OR {last_seen} IS NOT NULL OR {available} = 1 OR {stale} = 1 "
            f"OR {prefix}_error_code IS NOT NULL "
            f"ORDER BY {status_time} DESC, id DESC LIMIT 1"
        ).fetchone()
        if status_row is not None:
            selected[int(status_row["id"])] = status_row
    return sorted(selected.values(), key=lambda row: (row["sampled_at_utc"], row["id"]))


def _token_only(sample: HistorySample) -> HistorySample:
    return replace(
        sample,
        quota_observed_at=None,
        quota_source_status="unavailable",
        five_hour_observed_at=None,
        five_hour_last_seen_at=None,
        five_hour_event_seq=0,
        five_hour_used_percent=None,
        five_hour_remaining_percent=None,
        five_hour_reset_at=None,
        five_hour_source="unknown",
        five_hour_available=False,
        five_hour_stale=False,
        five_hour_error_code=None,
        weekly_observed_at=None,
        weekly_last_seen_at=None,
        weekly_event_seq=0,
        weekly_used_percent=None,
        weekly_remaining_percent=None,
        weekly_reset_at=None,
        weekly_source="unknown",
        weekly_available=False,
        weekly_stale=False,
        weekly_error_code=None,
    )


def _token_values_compatible(first: HistorySample, second: HistorySample) -> bool:
    return all(
        getattr(first, name) is None
        or getattr(second, name) is None
        or getattr(first, name) == getattr(second, name)
        for name in _TOKEN_FIELDS
    )


def _merge_token_samples(first: HistorySample, second: HistorySample) -> HistorySample:
    def rank(sample: HistorySample) -> tuple[object, ...]:
        lifecycle_rank = {
            "exact": 3,
            "completed_partial": 2,
            "in_progress": 1,
        }.get(sample.source_status, 0)
        source_rank = {"dashboard": 2, "mini": 1}.get(sample.source_type, 0)
        completeness = sum(getattr(sample, name) is not None for name in _TOKEN_FIELDS)
        return (
            lifecycle_rank,
            sample.source_observed_at or datetime.min.replace(tzinfo=timezone.utc),
            completeness,
            source_rank,
            int(sample.source_available),
            int(not sample.token_stale),
            sample.sampled_at,
            sample.sample_id,
        )

    preferred, other = (first, second) if rank(first) >= rank(second) else (second, first)
    # A candidate is one coherent source snapshot. Filling its missing fields
    # from another observation can create an impossible mixture (for example,
    # a newer Mini Total paired with older Dashboard Input and Output).
    return preferred


@dataclass(frozen=True)
class _QuotaProjection:
    samples: tuple[HistorySample, ...] = ()
    start_at: datetime | None = None
    end_at: datetime | None = None
    five_hour_last_seen_at: datetime | None = None
    weekly_last_seen_at: datetime | None = None
    five_hour_available: bool | None = None
    five_hour_stale: bool = False
    weekly_available: bool | None = None
    weekly_stale: bool = False


def _global_quota_projection(
    rows: list[sqlite3.Row], cutoff: datetime,
) -> _QuotaProjection:
    raw = [
        _quota_only(_sample_from_row(row))
        for row in rows
        if not bool(row["legacy_unknown_time"])
    ]
    raw.sort(key=lambda sample: (sample.sampled_at, sample.sample_id))
    state_sample: dict[str, HistorySample | None] = {
        "five_hour": None, "weekly": None,
    }
    state_observed: dict[str, datetime | None] = {
        "five_hour": None, "weekly": None,
    }
    state_last_seen: dict[str, datetime | None] = {
        "five_hour": None, "weekly": None,
    }
    window_events: dict[str, tuple[HistorySample, ...]] = {}
    current_available: dict[str, bool | None] = {}
    current_stale: dict[str, bool] = {}
    for prefix in ("five_hour", "weekly"):
        events, available, stale = _quota_window_events(raw, prefix)
        window_events[prefix] = events
        current_available[prefix] = available
        current_stale[prefix] = stale
        if events:
            state_last_seen[prefix] = getattr(
                events[-1], f"{prefix}_last_seen_at",
            )

    timeline: dict[datetime, dict[str, list[HistorySample]]] = {}
    for prefix, events in window_events.items():
        for event in events:
            observed_at = getattr(event, f"{prefix}_observed_at")
            if observed_at is not None:
                timeline.setdefault(observed_at, {}).setdefault(prefix, []).append(event)

    events: list[HistorySample] = []
    for observed_at in sorted(timeline):
        changes = timeline[observed_at]
        rounds = max(len(items) for items in changes.values())
        for index in range(rounds):
            current_changes: list[HistorySample] = []
            for prefix in ("five_hour", "weekly"):
                prefix_events = changes.get(prefix, [])
                if index >= len(prefix_events):
                    continue
                event = prefix_events[index]
                current_changes.append(event)
                state_sample[prefix] = event
                state_observed[prefix] = getattr(event, f"{prefix}_observed_at")
                state_last_seen[prefix] = getattr(event, f"{prefix}_last_seen_at")
            current = max(current_changes, key=lambda sample: sample.sample_id)
            events.append(_compose_quota_state(
                current, state_sample, state_observed, state_last_seen,
            ))

    visible: list[HistorySample] = []
    observed_times: list[datetime] = []
    for sample in events:
        masked = sample
        visible_window = False
        for prefix in ("five_hour", "weekly"):
            observed_at = getattr(masked, f"{prefix}_observed_at")
            if observed_at is None or observed_at < cutoff:
                masked = _mask_quota_window(masked, prefix)
            else:
                visible_window = True
                observed_times.append(observed_at)
        if visible_window:
            visible.append(masked)
    return _QuotaProjection(
        samples=tuple(visible),
        start_at=min(observed_times) if observed_times else None,
        end_at=max(observed_times) if observed_times else None,
        five_hour_last_seen_at=state_last_seen["five_hour"],
        weekly_last_seen_at=state_last_seen["weekly"],
        five_hour_available=current_available["five_hour"],
        five_hour_stale=current_stale["five_hour"],
        weekly_available=current_available["weekly"],
        weekly_stale=current_stale["weekly"],
    )


def _quota_window_events(
    raw: list[HistorySample], prefix: str,
) -> tuple[tuple[HistorySample, ...], bool | None, bool]:
    """Build one window's value stream using only that window's reliable time."""

    status_sample: HistorySample | None = None
    status_key: tuple[datetime, int] | None = None
    candidates: list[HistorySample] = []
    for sample in raw:
        status_at = _window_status_time(sample, prefix)
        if status_at is not None:
            key = status_at, sample.sample_id
            if status_key is None or key >= status_key:
                status_key = key
                status_sample = sample
        if _quota_window_identity(sample, prefix) is not None:
            candidates.append(sample)
    candidates.sort(key=lambda sample: (
        getattr(sample, f"{prefix}_event_seq"),
        getattr(sample, f"{prefix}_observed_at"),
        sample.sample_id,
    ))

    events: list[HistorySample] = []
    identity: tuple[object, ...] | None = None
    prototype: HistorySample | None = None
    observed_at: datetime | None = None
    last_seen_at: datetime | None = None
    for sample in candidates:
        sample_identity = _quota_window_identity(sample, prefix)
        sample_seq = getattr(sample, f"{prefix}_event_seq")
        current_seq = (
            None if not events else getattr(events[-1], f"{prefix}_event_seq")
        )
        if sample_identity != identity or sample_seq != current_seq:
            identity = sample_identity
            prototype = sample
            observed_at = getattr(sample, f"{prefix}_observed_at")
            last_seen_at = getattr(sample, f"{prefix}_last_seen_at")
            events.append(_quota_window_event(
                sample, prototype, prefix, observed_at, last_seen_at,
            ))
        else:
            last_seen_at = _latest_time(
                last_seen_at, getattr(sample, f"{prefix}_last_seen_at"),
            )
            assert prototype is not None
            events[-1] = _quota_window_event(
                sample, prototype, prefix, observed_at, last_seen_at,
            )
    available = (
        None if status_sample is None
        else bool(getattr(status_sample, f"{prefix}_available"))
    )
    stale = bool(
        status_sample is not None
        and getattr(status_sample, f"{prefix}_stale")
    )
    if events and status_sample is not None and stale:
        events[-1] = replace(events[-1], **{
            f"{prefix}_available": bool(
                getattr(status_sample, f"{prefix}_available")
            ),
            f"{prefix}_stale": True,
            f"{prefix}_error_code": getattr(
                status_sample, f"{prefix}_error_code",
            ),
        })
    return tuple(events), available, stale


def _quota_window_event(
    current: HistorySample,
    prototype: HistorySample,
    prefix: str,
    observed_at: datetime | None,
    last_seen_at: datetime | None,
) -> HistorySample:
    other = "weekly" if prefix == "five_hour" else "five_hour"
    event = _mask_quota_window(_quota_only(current), other)
    values = {
        f"{prefix}_{suffix}": getattr(prototype, f"{prefix}_{suffix}")
        for suffix in ("used_percent", "remaining_percent", "reset_at", "source")
    }
    values[f"{prefix}_observed_at"] = observed_at
    values[f"{prefix}_last_seen_at"] = last_seen_at
    values[f"{prefix}_event_seq"] = getattr(prototype, f"{prefix}_event_seq")
    return replace(event, **values)


def _quota_only(sample: HistorySample) -> HistorySample:
    return replace(
        sample,
        source_observed_at=None,
        thread_safe_id=None,
        response_safe_id=None,
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


def _quota_window_identity(
    sample: HistoryObservation, prefix: str,
) -> tuple[object, ...] | None:
    observed_at = getattr(sample, f"{prefix}_observed_at")
    used = getattr(sample, f"{prefix}_used_percent")
    remaining = getattr(sample, f"{prefix}_remaining_percent")
    reset_at = getattr(sample, f"{prefix}_reset_at")
    if (
        observed_at is None
        or not getattr(sample, f"{prefix}_available")
        or getattr(sample, f"{prefix}_stale")
        or (used is None and remaining is None and reset_at is None)
    ):
        return None
    return used, remaining, reset_at, getattr(sample, f"{prefix}_source")


def _quota_window_identity_key(
    sample: HistoryObservation, prefix: str,
) -> str | None:
    identity = _quota_window_identity(sample, prefix)
    if identity is None:
        return None
    normalized = [
        _iso_utc(value) if isinstance(value, datetime) else value
        for value in identity
    ]
    encoded = json.dumps(
        normalized, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _storage_fingerprint(
    observation: HistoryObservation,
    event_sequences: dict[str, int],
) -> str:
    payload = {
        "base": observation.sample_fingerprint,
        "five_hour_event_seq": event_sequences["five_hour"],
        "weekly_event_seq": event_sequences["weekly"],
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _active_meta_key(prefix: str, suffix: str) -> str:
    return f"quota_{prefix}_active_{suffix}_v3"


def _meta_value(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        f"SELECT value FROM {_META_TABLE} WHERE key = ?", (key,),
    ).fetchone()
    return None if row is None else str(row[0])


def _set_meta_value(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        f"INSERT INTO {_META_TABLE}(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _set_active_quota_event(
    connection: sqlite3.Connection,
    prefix: str,
    identity: str | None,
    sequence: int,
) -> None:
    _set_meta_value(
        connection, _active_meta_key(prefix, "identity"), identity or "none",
    )
    _set_meta_value(
        connection, _active_meta_key(prefix, "seq"), str(max(0, sequence)),
    )


def _load_or_recover_active_quota_event(
    connection: sqlite3.Connection, prefix: str,
) -> tuple[str | None, int]:
    identity_text = _meta_value(connection, _active_meta_key(prefix, "identity"))
    sequence_text = _meta_value(connection, _active_meta_key(prefix, "seq"))
    try:
        sequence = int(sequence_text) if sequence_text is not None else -1
    except ValueError:
        sequence = -1
    identity_valid = bool(
        identity_text == "none"
        or (
            identity_text is not None
            and re.fullmatch(r"[0-9a-f]{64}", identity_text)
        )
    )
    if identity_valid and sequence >= 0:
        return (None if identity_text == "none" else identity_text), sequence
    return _recover_active_quota_event(connection, prefix)


def _recover_active_quota_event(
    connection: sqlite3.Connection, prefix: str,
) -> tuple[str | None, int]:
    rows = connection.execute(f"SELECT * FROM {_TABLE}").fetchall()
    samples = [
        _sample_from_row(row) for row in rows if not bool(row["legacy_unknown_time"])
    ]
    candidates = [
        sample for sample in samples
        if _quota_window_identity(sample, prefix) is not None
    ]
    max_sequence = max(
        (getattr(sample, f"{prefix}_event_seq") for sample in candidates),
        default=0,
    )
    if not candidates:
        _set_active_quota_event(connection, prefix, None, max_sequence)
        return None, max_sequence
    latest = max(candidates, key=lambda sample: (
        _window_status_time(sample, prefix) or sample.sampled_at, sample.sample_id,
    ))
    last_event = max(candidates, key=lambda sample: (
        getattr(sample, f"{prefix}_event_seq"),
        getattr(sample, f"{prefix}_observed_at"),
        sample.sample_id,
    ))
    identity = _quota_window_identity_key(latest, prefix)
    last_identity = _quota_window_identity_key(last_event, prefix)
    sequence = max_sequence + int(identity != last_identity)
    _set_active_quota_event(connection, prefix, identity, sequence)
    return identity, sequence


def _backfill_quota_event_sequences(connection: sqlite3.Connection) -> None:
    rows = connection.execute(f"SELECT * FROM {_TABLE}").fetchall()
    samples = [
        _sample_from_row(row) for row in rows if not bool(row["legacy_unknown_time"])
    ]
    for prefix in ("five_hour", "weekly"):
        candidates = [
            sample for sample in samples
            if _quota_window_identity(sample, prefix) is not None
        ]
        candidates.sort(key=lambda sample: (
            getattr(sample, f"{prefix}_observed_at"), sample.sample_id,
        ))
        sequence = 0
        previous: str | None = None
        for sample in candidates:
            identity = _quota_window_identity_key(sample, prefix)
            if identity != previous:
                sequence += 1
                previous = identity
            connection.execute(
                f"UPDATE {_TABLE} SET {prefix}_event_seq = ? WHERE id = ?",
                (sequence, sample.sample_id),
            )
        if not candidates:
            _set_active_quota_event(connection, prefix, None, 0)
            continue
        latest = max(candidates, key=lambda sample: (
            _window_status_time(sample, prefix) or sample.sampled_at,
            sample.sample_id,
        ))
        active_identity = _quota_window_identity_key(latest, prefix)
        active_sequence = sequence + int(active_identity != previous)
        _set_active_quota_event(
            connection, prefix, active_identity, active_sequence,
        )


def _window_status_time(sample: HistorySample, prefix: str) -> datetime | None:
    last_seen = getattr(sample, f"{prefix}_last_seen_at")
    if last_seen is not None:
        return last_seen
    return sample.quota_observed_at or sample.sampled_at


def _compose_quota_state(
    current: HistorySample,
    state_sample: dict[str, HistorySample | None],
    state_observed: dict[str, datetime | None],
    state_last_seen: dict[str, datetime | None],
) -> HistorySample:
    values: dict[str, object] = {}
    for prefix in ("five_hour", "weekly"):
        prototype = state_sample[prefix]
        if prototype is None:
            continue
        for suffix in (
            "used_percent", "remaining_percent", "reset_at", "source",
            "available", "stale", "error_code", "event_seq",
        ):
            values[f"{prefix}_{suffix}"] = getattr(prototype, f"{prefix}_{suffix}")
        values[f"{prefix}_observed_at"] = state_observed[prefix]
        values[f"{prefix}_last_seen_at"] = state_last_seen[prefix]
    return replace(current, **values)


def _mask_quota_window(sample: HistorySample, prefix: str) -> HistorySample:
    return replace(sample, **{
        f"{prefix}_observed_at": None,
        f"{prefix}_last_seen_at": None,
        f"{prefix}_event_seq": 0,
        f"{prefix}_used_percent": None,
        f"{prefix}_remaining_percent": None,
        f"{prefix}_reset_at": None,
        f"{prefix}_source": "unknown",
        f"{prefix}_available": False,
        f"{prefix}_stale": False,
        f"{prefix}_error_code": None,
    })


def _latest_time(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _query_result(
    range_days: int,
    samples: tuple[HistorySample, ...],
    quota: _QuotaProjection,
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
            or latest.source_observed_at is None
            or now - latest.source_observed_at > HISTORY_STALE_AFTER
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
        if any(getattr(sample, name) is not None for sample in quota.samples):
            available.add(name)
    token_start_at = samples[0].source_observed_at if samples else None
    token_end_at = samples[-1].source_observed_at if samples else None
    return HistoryQueryResult(
        range_days=range_days,
        status=status,
        samples=samples,
        quota_samples=quota.samples,
        sample_count=len(samples),
        start_at=token_start_at,
        end_at=token_end_at,
        stale=status == "stale",
        metrics_available=tuple(sorted(available)),
        queried_at=now,
        token_start_at=token_start_at,
        token_end_at=token_end_at,
        quota_start_at=quota.start_at,
        quota_end_at=quota.end_at,
        five_hour_last_seen_at=quota.five_hour_last_seen_at,
        weekly_last_seen_at=quota.weekly_last_seen_at,
        five_hour_available=quota.five_hour_available,
        five_hour_stale=quota.five_hour_stale,
        weekly_available=quota.weekly_available,
        weekly_stale=quota.weekly_stale,
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
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is timezone.utc:
        return parsed
    return _aware_utc(parsed, "stored_datetime")


def _parse_utc_safely(value: object) -> tuple[datetime | None, bool]:
    try:
        return _parse_utc(value), False
    except (TypeError, ValueError):
        return None, value is not None


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


def _persisted_thread_safe_identifier(value: str | None) -> str | None:
    normalized = _safe_identifier(value)
    if normalized is None or _RESPONSE_SAFE_IDENTIFIER.fullmatch(normalized):
        return normalized
    return make_thread_safe_id(normalized)


def _safe_response_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not _RESPONSE_SAFE_IDENTIFIER.fullmatch(normalized):
        raise ValueError("response_safe_id_invalid")
    return normalized


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
