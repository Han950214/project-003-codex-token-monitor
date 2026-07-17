"""Synthetic-only performance verification for Phase 3.1-D2."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.codex_rollout import CodexRolloutReader
from app.dashboard import (
    BACKFILL_MAX_COMPLETED_RESPONSES,
    BACKFILL_MAX_PROCESSED_FILES,
    BACKFILL_MAX_SCAN_BYTES,
    ResponseHistoryBackfillService,
)
from app.history import UsageHistoryStore
from app.usage_summary import UsageWindowKind
from scripts.usage_summary_performance import run_benchmark


def _usage(
    input_tokens: int,
    cached_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _event(
    kind: str,
    thread_id: str,
    timestamp: str,
    *,
    turn_id: str | None = None,
    last: dict[str, int] | None = None,
    total: dict[str, int] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {"type": kind}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    if last is not None:
        payload["info"] = {
            "last_token_usage": last,
            "total_token_usage": total,
        }
    return {
        "type": "event_msg",
        "thread_id": thread_id,
        "timestamp": timestamp,
        "payload": payload,
    }


def run_backfill_benchmark(
    *,
    candidate_files: int = 500,
    completed_responses: int = 5_000,
) -> dict[str, object]:
    if candidate_files <= 0 or candidate_files > BACKFILL_MAX_PROCESSED_FILES:
        raise ValueError("candidate_files_out_of_range")
    if (
        completed_responses <= 0
        or completed_responses > BACKFILL_MAX_COMPLETED_RESPONSES
        or completed_responses % candidate_files
    ):
        raise ValueError("completed_responses_out_of_range")
    responses_per_file = completed_responses // candidate_files
    as_of = datetime.now(timezone.utc)
    fixture_started = perf_counter()
    with tempfile.TemporaryDirectory(prefix="CodexTokenMonitor-D2Perf-") as root:
        sessions = Path(root)
        total_fixture_bytes = 0
        for file_index in range(candidate_files):
            thread_id = f"thread-safe-{file_index:04d}"
            timestamp = (
                as_of - timedelta(minutes=file_index % 60)
            ).isoformat()
            zero = _usage(0, 0, 0, 0)
            cumulative = zero
            events = [
                _event(
                    "token_count",
                    thread_id,
                    timestamp,
                    last=zero,
                    total=zero,
                ),
            ]
            for response_index in range(responses_per_file):
                turn_id = f"turn-{response_index:04d}"
                call = _usage(
                    100 + response_index,
                    40 + response_index,
                    20 + response_index,
                    5 + response_index,
                )
                cumulative = tuple(
                    left + right
                    for left, right in zip(
                        (
                            cumulative["input_tokens"],
                            cumulative["cached_input_tokens"],
                            cumulative["output_tokens"],
                            cumulative["reasoning_output_tokens"],
                            cumulative["total_tokens"],
                        ),
                        (
                            call["input_tokens"],
                            call["cached_input_tokens"],
                            call["output_tokens"],
                            call["reasoning_output_tokens"],
                            call["total_tokens"],
                        ),
                    )
                )
                cumulative_usage = {
                    key: value
                    for key, value in zip(
                        (
                            "input_tokens",
                            "cached_input_tokens",
                            "output_tokens",
                            "reasoning_output_tokens",
                            "total_tokens",
                        ),
                        cumulative,
                    )
                }
                events.extend((
                    _event("task_started", thread_id, timestamp, turn_id=turn_id),
                    _event(
                        "token_count",
                        thread_id,
                        timestamp,
                        turn_id=turn_id,
                        last=call,
                        total=cumulative_usage,
                    ),
                    _event("task_complete", thread_id, timestamp, turn_id=turn_id),
                ))
                cumulative = cumulative_usage
            encoded = "\n".join(
                json.dumps(item, separators=(",", ":"), ensure_ascii=True)
                for item in events
            )
            path = sessions / f"rollout-safe-{file_index:04d}.jsonl"
            path.write_text(encoded, encoding="utf-8")
            total_fixture_bytes += path.stat().st_size
        fixture_build_ms = round((perf_counter() - fixture_started) * 1000)
        if total_fixture_bytes > BACKFILL_MAX_SCAN_BYTES:
            raise AssertionError("synthetic_fixture_exceeds_scan_limit")

        store = UsageHistoryStore(
            sessions / "usage-history.sqlite3",
            clock=lambda: as_of,
        )
        if not store.initialize():
            raise RuntimeError(store.last_error or "history_initialize_failed")
        reader = CodexRolloutReader()
        service = ResponseHistoryBackfillService(
            reader,
            store,
            sessions_dir=sessions,
            clock=lambda: as_of,
        )
        cancel = threading.Event()
        cancel.set()
        cancelled = service.run_once(cancel)
        cancel.clear()
        runs = []
        run_started = perf_counter()
        for _attempt in range(10):
            result = service.run_once(cancel)
            runs.append(result)
            if result.status == "completed":
                break
        scan_elapsed_ms = round((perf_counter() - run_started) * 1000)
        summary = store.summarize_usage(
            UsageWindowKind.ROLLING_30D,
            as_of_utc=as_of,
            local_timezone=timezone.utc,
        )
        if not runs or runs[-1].status != "completed":
            raise AssertionError("bounded_backfill_did_not_complete")
        if summary.observed_response_count != completed_responses:
            raise AssertionError("canonical_response_count_mismatch")
        scan_bytes = sum(item.scan_bytes for item in runs)
        if scan_bytes > BACKFILL_MAX_SCAN_BYTES:
            raise AssertionError("actual_scan_bytes_exceeded")
        return {
            "candidate_files": candidate_files,
            "completed_responses": completed_responses,
            "fixture_bytes": total_fixture_bytes,
            "fixture_build_ms": fixture_build_ms,
            "scan_elapsed_ms": scan_elapsed_ms,
            "scan_bytes": scan_bytes,
            "run_count": len(runs),
            "cancel_resume": (
                cancelled.status == "cancelled"
                and summary.observed_response_count == completed_responses
            ),
            "canonical_response_count": summary.observed_response_count,
        }


def run_verification(
    *,
    candidate_files: int = 500,
    completed_responses: int = 5_000,
    history_rows: int = 200_000,
    thread_count: int = 20_000,
) -> dict[str, object]:
    backfill = run_backfill_benchmark(
        candidate_files=candidate_files,
        completed_responses=completed_responses,
    )
    history = run_benchmark(rows=history_rows, threads=thread_count)
    query_plan = tuple(str(item) for item in history["summary_query_plan"])
    query_plan_indexed = bool(
        query_plan
        and any(
            "USING INDEX ix_usage_history_samples_response_observed" in item
            for item in query_plan
        )
        and all("USE TEMP B-TREE" not in item for item in query_plan)
    )
    return {
        "backfill": backfill,
        "history": {
            "thread_count": history["thread_count"],
            "history_rows": history["rows_in_30d"],
            "fixture_build_ms": round(
                float(history["fixture_build_seconds"]) * 1000,
            ),
            "selected_time_and_rankings_ms": round(
                float(history["elapsed_seconds"]) * 1000,
            ),
            "trend_ms": round(
                float(history["trend_elapsed_seconds"]) * 1000,
            ),
            "query_plan_verdict": (
                "bounded_indexed"
                if not history["unbounded_fetchall_found"]
                and not history["select_star_found"]
                and query_plan_indexed
                else "failed"
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-files", type=int, default=500)
    parser.add_argument("--completed-responses", type=int, default=5_000)
    parser.add_argument("--history-rows", type=int, default=200_000)
    parser.add_argument("--thread-count", type=int, default=20_000)
    args = parser.parse_args()
    print(json.dumps(
        run_verification(
            candidate_files=args.candidate_files,
            completed_responses=args.completed_responses,
            history_rows=args.history_rows,
            thread_count=args.thread_count,
        ),
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
