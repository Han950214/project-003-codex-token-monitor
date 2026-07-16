"""Worst-case local benchmark for the 30-day response summary.

The benchmark creates only synthetic safe identifiers and numeric usage in a
temporary app-owned SQLite database. It never reads Codex data or user content.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import tracemalloc
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.history import SCHEMA_VERSION, UsageHistoryStore
from app.usage_summary import UsageWindowKind


def run_benchmark(
    rows: int = 200_000,
    identity_mode: str = "unique_responses",
) -> dict[str, object]:
    if rows <= 0 or rows > 200_000:
        raise ValueError("rows_must_be_between_1_and_200000")
    if identity_mode not in {
        "unique_responses", "unique_threads", "single_response_snapshots",
    }:
        raise ValueError("unsupported_identity_mode")
    as_of = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    first = as_of - timedelta(seconds=rows)
    with tempfile.TemporaryDirectory(prefix="CodexTokenMonitor-UsagePerf-") as root:
        path = Path(root) / "usage-history.sqlite3"
        store = UsageHistoryStore(path, clock=lambda: as_of)
        if not store.initialize():
            raise RuntimeError(store.last_error or "history_initialize_failed")
        sql = (
            "INSERT INTO usage_history_samples("
            "schema_version, sampled_at_utc, source_observed_at_utc, "
            "thread_safe_id, response_safe_id, model_safe_id, source_type, "
            "source_status, source_available, input_tokens, output_tokens, "
            "total_tokens, cached_tokens, reasoning_tokens, "
            "quota_source_status, five_hour_observed_at_utc, "
            "five_hour_last_seen_at_utc, five_hour_used_percent, "
            "five_hour_remaining_percent, five_hour_reset_at_utc, "
            "five_hour_source, five_hour_available, five_hour_stale, "
            "sample_fingerprint"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, ?, ?)"
        )

        def values():
            for index in range(rows):
                observed = first + timedelta(seconds=index + 1)
                timestamp = observed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                safe_id = (
                    f"sha256:{index:064x}"
                    if identity_mode in {"unique_responses", "unique_threads"}
                    else f"sha256:{0:064x}"
                )
                thread_id = (
                    f"perf-thread-{index:06d}"
                    if identity_mode == "unique_threads"
                    else "perf-thread-001"
                )
                input_tokens = (
                    100
                    if identity_mode in {"unique_responses", "unique_threads"}
                    else 100 + index
                )
                yield (
                    SCHEMA_VERSION, timestamp, timestamp, thread_id,
                    safe_id, "perf-model-001", "dashboard", "exact", 1,
                    input_tokens, 20, input_tokens + 20, 40, 5,
                    "normal", timestamp, timestamp,
                    40.0 if index % 2 == 0 else 60.0,
                    60.0 if index % 2 == 0 else 40.0,
                    (observed + timedelta(hours=5)).strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"
                    ),
                    "qa_safe_numbers", 1, 0,
                    f"perf-{index:064x}",
                )

        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executemany(sql, values())
            summary_plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT id, sampled_at_utc, "
                "source_observed_at_utc, thread_safe_id, response_safe_id "
                "FROM usage_history_samples WHERE (input_tokens IS NOT NULL OR "
                "output_tokens IS NOT NULL OR total_tokens IS NOT NULL OR "
                "cached_tokens IS NOT NULL OR reasoning_tokens IS NOT NULL) "
                "ORDER BY thread_safe_id, "
                "response_safe_id, source_observed_at_utc, sampled_at_utc, id",
            ).fetchall()

        tracemalloc.start()
        started = perf_counter()
        query_started = perf_counter()
        thread_filter = (
            f"perf-thread-{rows - 1:06d}"
            if identity_mode == "unique_threads"
            else "perf-thread-001"
        )
        trend = store.query(30, thread_filter, now=as_of)
        query_elapsed = perf_counter() - query_started
        summary_started = perf_counter()
        summary = store.summarize_usage(
            UsageWindowKind.ROLLING_30D,
            as_of_utc=as_of,
            local_timezone=timezone.utc,
        )
        summary_elapsed = perf_counter() - summary_started
        elapsed = perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        if identity_mode in {"unique_responses", "unique_threads"}:
            expected = {
                "response_count": rows,
                "covered_thread_count": (
                    rows if identity_mode == "unique_threads" else 1
                ),
                "input_tokens": rows * 100,
                "output_tokens": rows * 20,
                "total_tokens": rows * 120,
                "cached_tokens": rows * 40,
                "reasoning_tokens": rows * 5,
            }
        else:
            expected = {
                "response_count": 1,
                "covered_thread_count": 1,
                "input_tokens": 100 + rows - 1,
                "output_tokens": 20,
                "total_tokens": 120 + rows - 1,
                "cached_tokens": 40,
                "reasoning_tokens": 5,
            }
        actual = {
            "response_count": summary.observed_response_count,
            "covered_thread_count": summary.covered_thread_count,
            "input_tokens": summary.input_tokens.value,
            "output_tokens": summary.output_tokens.value,
            "total_tokens": summary.total_tokens.value,
            "cached_tokens": summary.cached_tokens.value,
            "reasoning_tokens": summary.reasoning_tokens.value,
        }
        if actual != expected:
            raise AssertionError({"expected": expected, "actual": actual})
        expected_trend_count = (
            1 if identity_mode in {"unique_threads", "single_response_snapshots"}
            else min(rows, 500)
        )
        if trend.sample_count != expected_trend_count:
            raise AssertionError({
                "expected_trend_count": expected_trend_count,
                "actual_trend_count": trend.sample_count,
            })
        return {
            "rows_in_30d": rows,
            "identity_mode": identity_mode,
            "elapsed_seconds": round(elapsed, 6),
            "trend_query_seconds": round(query_elapsed, 6),
            "summary_query_seconds": round(summary_elapsed, 6),
            "peak_memory_bytes": peak,
            "result": actual,
            "trend_sample_count": trend.sample_count,
            "trend_query_limit": 500,
            "summary_query_plan": [str(tuple(row)) for row in summary_plan],
            "trend_query_strategy": (
                "canonical response winners and quota event endpoints are "
                "bounded to the latest 500 identities/transitions in SQL"
            ),
            "fetch_mode": "bounded trend projections + summary fetchmany(512)",
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument(
        "--identity-mode",
        choices=("unique_responses", "unique_threads", "single_response_snapshots"),
        default="unique_responses",
    )
    args = parser.parse_args()
    print(json.dumps(
        run_benchmark(args.rows, args.identity_mode),
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
