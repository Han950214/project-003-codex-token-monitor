from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from app.codex_rollout import (
    CodexRolloutReader,
    ResponseUsageCandidate,
    make_response_safe_id,
)
from app.history import UsageHistoryStore
from app.dashboard import (
    BACKFILL_MAX_SINGLE_FILE_BYTES,
    ResponseHistoryBackfillService,
    ResponseHistoryBackfillResult,
)
from app.main import Dashboard, _new_backfill_history_store
import app.dashboard as dashboard_module
from scripts.verify_d2_performance import run_verification


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)


def usage(input_tokens: int, cached_tokens: int, output_tokens: int, reasoning_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def event(
    kind: str,
    turn: str | None = None,
    *,
    last: dict[str, int] | None = None,
    total: dict[str, int] | None = None,
    timestamp: object = None,
    thread_id: str = "thread-safe-a",
) -> dict[str, object]:
    payload: dict[str, object] = {"type": kind}
    if turn is not None:
        payload["turn_id"] = turn
    if last is not None:
        payload["info"] = {
            "last_token_usage": last,
            "total_token_usage": total,
        }
    return {
        "type": "event_msg",
        "payload": payload,
        "timestamp": timestamp,
        "thread_id": thread_id,
    }


def write_rollout(directory: str, events: list[dict[str, object]]) -> Path:
    path = Path(directory) / "rollout-safe.jsonl"
    path.write_text(
        "\n".join(json.dumps(item) for item in events),
        encoding="utf-8",
    )
    return path


class ResponseHistoryBackfillTests(unittest.TestCase):
    @staticmethod
    def three_completed_events() -> list[dict[str, object]]:
        zero = usage(0, 0, 0, 0)
        first = usage(10, 4, 2, 1)
        second = usage(20, 8, 4, 2)
        third = usage(30, 12, 6, 3)
        return [
            event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
            event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
            event("token_count", "turn-a", last=first, total=first, timestamp="2026-07-18T10:00:02Z"),
            event("task_complete", "turn-a", timestamp="2026-07-18T10:00:03Z"),
            event("task_started", "turn-b", timestamp="2026-07-18T10:01:01Z"),
            event("token_count", "turn-b", last=second, total=usage(30, 12, 6, 3), timestamp="2026-07-18T10:01:02Z"),
            event("task_complete", "turn-b", timestamp="2026-07-18T10:01:03Z"),
            event("task_started", "turn-c", timestamp="2026-07-18T10:02:01Z"),
            event("token_count", "turn-c", last=third, total=usage(60, 24, 12, 6), timestamp="2026-07-18T10:02:02Z"),
            event("task_complete", "turn-c", timestamp="2026-07-18T10:02:03Z"),
        ]

    def test_single_rollout_returns_and_persists_three_completed_responses(self):
        events = self.three_completed_events()
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, events)
            reader = CodexRolloutReader()
            latest = reader.refresh(Path(directory))
            batch = reader.read_completed_batch(path)
            store = UsageHistoryStore(
                Path(directory) / "history.sqlite3",
                clock=lambda: NOW,
            )
            self.assertTrue(store.initialize(), store.last_error)
            result = store.record_completed_batch(batch)
            summary = store.summarize_usage(
                "rolling_7d",
                as_of_utc=NOW,
            )

        self.assertEqual(len(batch.responses), 3)
        self.assertEqual(len(latest.completed_responses), 3)
        self.assertEqual(len({item.response_safe_id for item in batch.responses}), 3)
        self.assertEqual(result.canonical_response_count, 3)
        self.assertEqual(summary.observed_response_count, 3)

    def test_duplicate_refresh_and_restart_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, self.three_completed_events())
            database = Path(directory) / "history.sqlite3"
            first_reader = CodexRolloutReader()
            first_store = UsageHistoryStore(database, clock=lambda: NOW)
            self.assertTrue(first_store.initialize(), first_store.last_error)
            batch = first_reader.read_completed_batch(path)
            first = first_store.record_completed_batch(batch)
            duplicate = first_store.record_completed_batch(
                first_reader.read_completed_batch(path),
            )

            restarted_reader = CodexRolloutReader()
            restarted_store = UsageHistoryStore(database, clock=lambda: NOW)
            self.assertTrue(restarted_store.initialize(), restarted_store.last_error)
            self.assertTrue(
                restarted_store.backfill_watermark_is_current(
                    restarted_reader.file_scan_metadata(path),
                )
            )
            restarted = restarted_store.record_completed_batch(
                restarted_reader.read_completed_batch(path),
            )
            summary = restarted_store.summarize_usage(
                "rolling_7d",
                as_of_utc=NOW,
            )

        self.assertEqual(first.inserted_count, 3)
        self.assertEqual(duplicate.inserted_count, 0)
        self.assertEqual(restarted.inserted_count, 0)
        self.assertEqual(summary.observed_response_count, 3)

    def test_in_progress_upgrades_to_one_terminal_response(self):
        zero = usage(0, 0, 0, 0)
        first = usage(10, 4, 2, 1)
        final = usage(5, 1, 1, 0)
        initial = [
            event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
            event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
            event("token_count", "turn-a", last=first, total=first, timestamp="2026-07-18T10:00:02Z"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, initial)
            database = Path(directory) / "history.sqlite3"
            store = UsageHistoryStore(database, clock=lambda: NOW)
            self.assertTrue(store.initialize(), store.last_error)
            reader = CodexRolloutReader()
            before = reader.read_completed_batch(path)
            store.record_completed_batch(before)
            self.assertEqual(
                store.summarize_usage("rolling_7d", as_of_utc=NOW).observed_response_count,
                0,
            )

            updated = initial + [
                event("task_complete", "turn-a", timestamp="2026-07-18T10:00:03Z"),
                event(
                    "token_count",
                    "turn-a",
                    last=final,
                    total=usage(15, 5, 3, 1),
                    timestamp="2026-07-18T10:00:04Z",
                ),
            ]
            write_rollout(directory, updated)
            after = reader.read_completed_batch(path)
            store.record_completed_batch(after)
            summary = store.summarize_usage("rolling_7d", as_of_utc=NOW)

        self.assertEqual(len(before.responses), 0)
        self.assertEqual(len(after.responses), 1)
        self.assertEqual(after.responses[0].status, "exact")
        self.assertEqual(summary.observed_response_count, 1)

    def test_exact_supersedes_partial_without_deleting_old_observation(self):
        zero = usage(0, 0, 0, 0)
        call = usage(10, 4, 2, 1)
        partial_events = [
            event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
            event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
            event("token_count", "turn-a", last=call, total=call, timestamp="2026-07-18T10:00:02Z"),
            event("task_complete", "turn-a", timestamp="invalid-time"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, partial_events)
            database = Path(directory) / "history.sqlite3"
            store = UsageHistoryStore(database, clock=lambda: NOW)
            self.assertTrue(store.initialize(), store.last_error)
            partial = CodexRolloutReader().read_completed_batch(path)
            store.record_completed_batch(partial)

            exact_events = partial_events + [
                event("task_complete", "turn-a", timestamp="2026-07-18T10:00:03Z"),
            ]
            write_rollout(directory, exact_events)
            exact = CodexRolloutReader().read_completed_batch(path)
            store.record_completed_batch(exact)
            trend = store.query(7, "thread-safe-a", now=NOW)
            summary = store.summarize_usage("rolling_7d", as_of_utc=NOW)

        self.assertEqual(partial.responses[0].status, "completed_partial")
        self.assertEqual(exact.responses[0].status, "exact")
        self.assertEqual(
            partial.responses[0].response_safe_id,
            exact.responses[0].response_safe_id,
        )
        self.assertEqual(trend.sample_count, 1)
        self.assertEqual(trend.samples[0].source_status, "exact")
        self.assertEqual(summary.observed_response_count, 1)

    def test_missing_completion_time_falls_back_only_to_trusted_snapshot_time(self):
        zero = usage(0, 0, 0, 0)
        call = usage(10, 4, 2, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, [
                event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
                event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
                event("token_count", "turn-a", last=call, total=call, timestamp="2026-07-18T10:00:02Z"),
                event("task_complete", "turn-a", timestamp="invalid-time"),
            ])
            fallback = CodexRolloutReader().read_completed_batch(path)

            no_time_path = write_rollout(directory, [
                event("token_count", last=zero, total=zero, timestamp=None),
                event("task_started", "turn-b", timestamp=None),
                event("token_count", "turn-b", last=call, total=call, timestamp=None),
                event("task_complete", "turn-b", timestamp=None),
            ])
            no_time = CodexRolloutReader().read_completed_batch(no_time_path)

            invalid_start_path = write_rollout(directory, [
                event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
                event("task_started", "turn-c", timestamp="invalid-time"),
                event("token_count", "turn-c", last=call, total=call, timestamp="2026-07-18T10:00:02Z"),
                event("task_complete", "turn-c", timestamp="2026-07-18T10:00:03Z"),
            ])
            invalid_start = CodexRolloutReader().read_completed_batch(
                invalid_start_path,
            )

        self.assertEqual(fallback.responses[0].status, "completed_partial")
        self.assertEqual(
            fallback.responses[0].completion_time_utc,
            datetime(2026, 7, 18, 10, 0, 2, tzinfo=timezone.utc),
        )
        self.assertEqual(no_time.responses[0].status, "completed_partial")
        self.assertIsNone(no_time.responses[0].completion_time_utc)
        self.assertEqual(
            invalid_start.responses[0].status,
            "completed_partial",
        )
        self.assertIn(
            "start_time_missing",
            invalid_start.responses[0].safe_diagnostic_codes,
        )

    def test_next_turn_blocks_unidentified_late_snapshot_from_prior_turn(self):
        zero = usage(0, 0, 0, 0)
        first = usage(10, 4, 2, 1)
        late = usage(5, 1, 1, 0)
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, [
                event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
                event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
                event("token_count", "turn-a", last=first, total=first, timestamp="2026-07-18T10:00:02Z"),
                event("task_complete", "turn-a", timestamp="2026-07-18T10:00:03Z"),
                event("task_started", "turn-b", timestamp="2026-07-18T10:00:04Z"),
                event("token_count", last=late, total=usage(15, 5, 3, 1), timestamp="2026-07-18T10:00:05Z"),
            ])
            batch = CodexRolloutReader().read_completed_batch(path)

        self.assertEqual(len(batch.responses), 1)
        self.assertEqual(batch.responses[0].status, "exact")
        self.assertEqual(batch.responses[0].total_tokens, first["total_tokens"])

    def test_negative_delta_never_produces_exact(self):
        zero = usage(0, 0, 0, 0)
        first = usage(10, 4, 2, 1)
        reset = usage(2, 0, 1, 0)
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, [
                event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
                event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
                event("token_count", "turn-a", last=first, total=first, timestamp="2026-07-18T10:00:02Z"),
                event("token_count", "turn-a", last=reset, total=reset, timestamp="2026-07-18T10:00:03Z"),
                event("task_complete", "turn-a", timestamp="2026-07-18T10:00:04Z"),
            ])
            batch = CodexRolloutReader().read_completed_batch(path)

        self.assertEqual(len(batch.responses), 1)
        self.assertEqual(batch.responses[0].status, "completed_partial")
        self.assertIn("usage_unreconciled", batch.responses[0].safe_diagnostic_codes)

    def test_missing_baseline_keeps_only_confirmed_partial_usage(self):
        call = usage(10, 4, 2, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, [
                event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
                event(
                    "token_count",
                    "turn-a",
                    last=call,
                    total=usage(110, 44, 22, 11),
                    timestamp="2026-07-18T10:00:02Z",
                ),
                event("task_complete", "turn-a", timestamp="2026-07-18T10:00:03Z"),
            ])
            batch = CodexRolloutReader().read_completed_batch(path)

        self.assertEqual(len(batch.responses), 1)
        self.assertEqual(batch.responses[0].status, "completed_partial")
        self.assertEqual(batch.responses[0].total_tokens, call["total_tokens"])
        self.assertIn(
            "baseline_missing",
            batch.responses[0].safe_diagnostic_codes,
        )

    def test_total_only_snapshot_can_establish_a_safe_baseline(self):
        zero = usage(0, 0, 0, 0)
        call = usage(10, 4, 2, 1)
        baseline = event(
            "token_count",
            last=zero,
            total=zero,
            timestamp="2026-07-18T10:00:00Z",
        )
        payload = baseline["payload"]
        assert isinstance(payload, dict)
        info = payload["info"]
        assert isinstance(info, dict)
        info.pop("last_token_usage")
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, [
                baseline,
                event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
                event("token_count", "turn-a", last=call, total=call, timestamp="2026-07-18T10:00:02Z"),
                event("task_complete", "turn-a", timestamp="2026-07-18T10:00:03Z"),
            ])
            batch = CodexRolloutReader().read_completed_batch(path)

        self.assertEqual(len(batch.responses), 1)
        self.assertEqual(batch.responses[0].status, "exact")

    def test_truncated_tail_and_thread_conflict_never_produce_exact(self):
        zero = usage(0, 0, 0, 0)
        call = usage(10, 4, 2, 1)
        complete = [
            event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
            event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
            event("token_count", "turn-a", last=call, total=call, timestamp="2026-07-18T10:00:02Z"),
            event("task_complete", "turn-a", timestamp="2026-07-18T10:00:03Z"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            truncated_path = write_rollout(directory, complete)
            with truncated_path.open("a", encoding="utf-8") as handle:
                handle.write('\n{"type":"event_msg","payload":')
            truncated = CodexRolloutReader().read_completed_batch(truncated_path)

            conflict_events = list(complete)
            conflict_events[-1] = event(
                "task_complete",
                "turn-a",
                timestamp="2026-07-18T10:00:03Z",
                thread_id="different-thread",
            )
            conflict_path = write_rollout(directory, conflict_events)
            conflict = CodexRolloutReader().read_completed_batch(conflict_path)

        self.assertEqual(truncated.responses[0].status, "completed_partial")
        self.assertIn(
            "parse_incomplete",
            truncated.responses[0].safe_diagnostic_codes,
        )
        self.assertEqual(conflict.responses, ())

    def test_rejected_or_foreign_turn_evidence_never_produces_exact(self):
        zero = usage(0, 0, 0, 0)
        first = usage(10, 4, 2, 1)
        second = usage(5, 1, 1, 0)
        with tempfile.TemporaryDirectory() as directory:
            rejected_path = write_rollout(directory, [
                event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
                event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
                event("token_count", "turn-a", last=first, total=first, timestamp="2026-07-18T10:00:02Z"),
                event(
                    "token_count",
                    "turn-a",
                    last=usage(True, 0, 1, 0),
                    total=first,
                    timestamp="2026-07-18T10:00:03Z",
                ),
                event("task_complete", "turn-a", timestamp="2026-07-18T10:00:04Z"),
            ])
            rejected = CodexRolloutReader().read_completed_batch(rejected_path)

            foreign_path = write_rollout(directory, [
                event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
                event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
                event("token_count", "turn-a", last=first, total=first, timestamp="2026-07-18T10:00:02Z"),
                event(
                    "token_count",
                    "foreign-turn",
                    last=second,
                    total=usage(15, 5, 3, 1),
                    timestamp="2026-07-18T10:00:03Z",
                ),
                event("task_complete", "turn-a", timestamp="2026-07-18T10:00:04Z"),
            ])
            foreign = CodexRolloutReader().read_completed_batch(foreign_path)

        self.assertEqual(rejected.responses[0].status, "completed_partial")
        self.assertIn(
            "usage_rejected",
            rejected.responses[0].safe_diagnostic_codes,
        )
        self.assertEqual(foreign.responses[0].status, "completed_partial")
        self.assertEqual(foreign.responses[0].total_tokens, first["total_tokens"])
        self.assertIn(
            "usage_unreconciled",
            foreign.responses[0].safe_diagnostic_codes,
        )

    def test_file_changed_during_parse_is_retryable_and_not_cached(self):
        zero = usage(0, 0, 0, 0)
        call = usage(10, 4, 2, 1)

        class AppendingReader(CodexRolloutReader):
            def _read(self, path, refreshed_at, **kwargs):
                item = super()._read(path, refreshed_at, **kwargs)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                return item

        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, [
                event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
                event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
                event("token_count", "turn-a", last=call, total=call, timestamp="2026-07-18T10:00:02Z"),
                event("task_complete", "turn-a", timestamp="2026-07-18T10:00:03Z"),
            ])
            reader = AppendingReader()
            batch = reader.read_completed_batch(path)

        self.assertEqual(batch.scan_metadata.result_status, "changed_during_scan")
        self.assertFalse(reader._parse_cache)

    def test_cross_thread_identity_is_scoped_without_exposing_raw_ids(self):
        first = make_response_safe_id("safe-thread-a", "same-turn")
        second = make_response_safe_id("safe-thread-b", "same-turn")

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)
        self.assertRegex(first or "", r"^sha256:[0-9a-f]{64}$")

    def test_privacy_sentinel_never_reaches_dto_cache_database_or_metadata(self):
        sentinel = "D2_PRIVATE_SENTINEL_7f1b"
        zero = usage(0, 0, 0, 0)
        call = usage(10, 4, 2, 1)
        forbidden = {
            "prompt", "response", "reasoning", "reasoning_text", "tool_output",
            "message", "content", "project_content", "file_content",
            "authorization", "cookie", "credential", "raw_json", "raw_line",
            "raw_payload", "payload", "raw_thread_id", "raw_turn_id",
            "rollout_path",
        }
        with tempfile.TemporaryDirectory() as directory:
            events = [
                event("token_count", last=zero, total=zero, timestamp="2026-07-18T10:00:00Z"),
                event("task_started", "turn-a", timestamp="2026-07-18T10:00:01Z"),
                event("token_count", "turn-a", last=call, total=call, timestamp="2026-07-18T10:00:02Z"),
                event("task_complete", "turn-a", timestamp="2026-07-18T10:00:03Z"),
            ]
            for item in events:
                item["content"] = sentinel
                payload = item["payload"]
                assert isinstance(payload, dict)
                for name in (
                    "prompt", "response", "reasoning", "tool_output", "message",
                    "content", "authorization", "cookie", "credential",
                    "raw_secret_marker",
                ):
                    payload[name] = sentinel
            path = write_rollout(directory, events)
            reader = CodexRolloutReader()
            batch = reader.read_completed_batch(path)
            database = Path(directory) / "history.sqlite3"
            store = UsageHistoryStore(database, clock=lambda: NOW)
            self.assertTrue(store.initialize(), store.last_error)
            store.record_completed_batch(batch)
            with closing(sqlite3.connect(database)) as connection:
                stored_text = "\n".join(
                    str(value)
                    for row in connection.execute(
                        "SELECT * FROM usage_history_samples"
                    )
                    for value in row
                )
                metadata_text = "\n".join(
                    str(value)
                    for row in connection.execute("SELECT * FROM usage_history_meta")
                    for value in row
                )

        dto_text = json.dumps(asdict(batch), default=str, sort_keys=True)
        cache_text = repr(reader._parse_cache)
        self.assertNotIn(sentinel, dto_text)
        self.assertNotIn(sentinel, cache_text)
        self.assertNotIn(sentinel, stored_text)
        self.assertNotIn(sentinel, metadata_text)
        self.assertEqual(
            forbidden.intersection(field.name for field in fields(ResponseUsageCandidate)),
            set(),
        )

    def test_parse_cache_uses_safe_identity_and_coalesces_concurrent_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, self.three_completed_events())
            reader = CodexRolloutReader()
            original = reader._read

            def slow_read(*args, **kwargs):
                time.sleep(0.05)
                return original(*args, **kwargs)

            results = []
            with patch.object(reader, "_read", side_effect=slow_read) as read:
                threads = [
                    threading.Thread(
                        target=lambda: results.append(reader.read_completed_batch(path))
                    )
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

        self.assertEqual(read.call_count, 1)
        self.assertEqual(len(results), 2)
        cache_identity = next(iter(reader._parse_cache))[0]
        self.assertRegex(cache_identity, r"^sha256:[0-9a-f]{64}$")

    def test_backfill_service_skips_unchanged_after_restart_and_reprocesses_append(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_rollout(directory, self.three_completed_events())
            database = root / "history.sqlite3"
            first_store = UsageHistoryStore(database, clock=lambda: NOW)
            self.assertTrue(first_store.initialize(), first_store.last_error)
            first = ResponseHistoryBackfillService(
                CodexRolloutReader(), first_store, sessions_dir=root, clock=lambda: NOW,
            ).run_once()

            restarted_reader = CodexRolloutReader()
            restarted_store = UsageHistoryStore(database, clock=lambda: NOW)
            self.assertTrue(restarted_store.initialize(), restarted_store.last_error)
            with patch.object(
                restarted_reader, "_read", wraps=restarted_reader._read,
            ) as read:
                unchanged = ResponseHistoryBackfillService(
                    restarted_reader,
                    restarted_store,
                    sessions_dir=root,
                    clock=lambda: NOW,
                ).run_once()
            path.write_text(
                path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
            changed = ResponseHistoryBackfillService(
                restarted_reader,
                restarted_store,
                sessions_dir=root,
                clock=lambda: NOW,
            ).run_once()
            summary = restarted_store.summarize_usage(
                "rolling_7d",
                as_of_utc=NOW,
            )

        self.assertEqual(first.processed_file_count, 1)
        self.assertEqual(first.inserted_observation_count, 3)
        self.assertEqual(unchanged.processed_file_count, 0)
        self.assertEqual(unchanged.unchanged_file_count, 1)
        self.assertEqual(read.call_count, 0)
        self.assertEqual(changed.processed_file_count, 1)
        self.assertEqual(changed.inserted_observation_count, 0)
        self.assertEqual(summary.observed_response_count, 3)

    def test_backfill_does_not_scan_beyond_the_30_day_file_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_rollout(directory, self.three_completed_events())
            too_old = (NOW - timedelta(days=30, hours=1)).timestamp()
            os.utime(path, (too_old, too_old))
            store = UsageHistoryStore(
                Path(directory) / "history.sqlite3",
                clock=lambda: NOW,
            )
            self.assertTrue(store.initialize(), store.last_error)
            result = ResponseHistoryBackfillService(
                CodexRolloutReader(),
                store,
                sessions_dir=Path(directory),
                clock=lambda: NOW,
            ).run_once()

        self.assertEqual(result.candidate_file_count, 0)
        self.assertEqual(result.processed_file_count, 0)
        self.assertEqual(result.completed_response_count, 0)

    def test_backfill_service_cancels_and_retries_oversize_files_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "rollout-safe.jsonl"
            with path.open("wb") as handle:
                handle.seek(BACKFILL_MAX_SINGLE_FILE_BYTES)
                handle.write(b"x")
            store = UsageHistoryStore(root / "history.sqlite3", clock=lambda: NOW)
            self.assertTrue(store.initialize(), store.last_error)
            service = ResponseHistoryBackfillService(
                CodexRolloutReader(), store, sessions_dir=root, clock=lambda: NOW,
            )
            first = service.run_once()
            second = service.run_once()

            cancel = threading.Event()
            cancel.set()
            cancelled = service.run_once(cancel)

        self.assertEqual(first.skipped_file_count, 1)
        self.assertEqual(second.skipped_file_count, 1)
        self.assertEqual(cancelled.status, "cancelled")

    def test_dashboard_coalesces_background_backfill_and_updates_ui_on_poll(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingService:
            def run_once(self, cancel_event):
                started.set()
                release.wait(1)
                return ResponseHistoryBackfillResult("completed")

        dashboard = object.__new__(Dashboard)
        dashboard.root = Mock()
        dashboard._closing = False
        dashboard._history_backfill_service = BlockingService()
        dashboard._history_backfill_lock = threading.Lock()
        dashboard._history_backfill_cancel = threading.Event()
        dashboard._history_backfill_thread = None
        dashboard._history_backfill_results = __import__("queue").Queue()
        dashboard._history_backfill_poll_scheduled = False
        dashboard._history_backfill_last_started = None
        dashboard.history_backfill_status = "idle"
        dashboard.status_message_var = Mock()
        dashboard.language = "en"

        self.assertTrue(Dashboard._request_history_backfill(dashboard, manual=True))
        self.assertTrue(started.wait(1))
        self.assertFalse(Dashboard._request_history_backfill(dashboard, manual=True))
        self.assertEqual(dashboard.status_message_var.set.call_count, 1)
        release.set()
        dashboard._history_backfill_thread.join(1)
        Dashboard._poll_history_backfill_results(dashboard)

        self.assertEqual(dashboard.history_backfill_status, "completed")
        self.assertEqual(dashboard.status_message_var.set.call_count, 2)

    def test_worker_history_store_has_an_independent_lock_and_connection_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            primary = UsageHistoryStore(Path(directory) / "history.sqlite3")
            worker = _new_backfill_history_store(primary)

        self.assertIsNot(worker, primary)
        self.assertIsNot(worker._lock, primary._lock)
        self.assertEqual(worker.path, primary.path)
        self.assertEqual(worker.retention_days, primary.retention_days)
        self.assertEqual(worker.max_rows, primary.max_rows)

    def test_retryable_failures_do_not_starve_older_unprocessed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for index in range(3):
                path = root / f"rollout-safe-{index}.jsonl"
                with path.open("wb") as handle:
                    handle.seek(BACKFILL_MAX_SINGLE_FILE_BYTES)
                    handle.write(b"x")
                paths.append(path)
            store = UsageHistoryStore(root / "history.sqlite3", clock=lambda: NOW)
            self.assertTrue(store.initialize(), store.last_error)
            service = ResponseHistoryBackfillService(
                CodexRolloutReader(), store, sessions_dir=root, clock=lambda: NOW,
            )
            with patch.object(
                dashboard_module, "BACKFILL_MAX_PROCESSED_FILES", 2,
            ):
                first = service.run_once()
                first_statuses = store.backfill_file_statuses(
                    service.reader.file_scan_metadata(path) for path in paths
                )
                second = service.run_once()
                second_statuses = store.backfill_file_statuses(
                    service.reader.file_scan_metadata(path) for path in paths
                )

        self.assertEqual(first.processed_file_count, 2)
        self.assertEqual(len(first_statuses), 2)
        self.assertGreaterEqual(second.processed_file_count, 1)
        self.assertEqual(len(second_statuses), 3)

    def test_small_performance_harness_covers_backfill_and_history_queries(self):
        result = run_verification(
            candidate_files=3,
            completed_responses=6,
            history_rows=200,
            thread_count=20,
        )

        self.assertEqual(result["backfill"]["candidate_files"], 3)
        self.assertEqual(result["backfill"]["canonical_response_count"], 6)
        self.assertTrue(result["backfill"]["cancel_resume"])
        self.assertEqual(result["history"]["history_rows"], 200)
        self.assertEqual(result["history"]["query_plan_verdict"], "bounded_indexed")


if __name__ == "__main__":
    unittest.main()
