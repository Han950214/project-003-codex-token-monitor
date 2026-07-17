"""Synthetic 30-day benchmark for Phase 3.1-D usage insights."""

from __future__ import annotations

import argparse
import inspect
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
    threads: int = 20_000,
) -> dict[str, object]:
    if rows <= 0 or rows > 200_000:
        raise ValueError("rows_must_be_between_1_and_200000")
    if threads <= 0 or threads > rows:
        raise ValueError("threads_must_be_between_1_and_rows")
    as_of = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    first = as_of - timedelta(seconds=rows)
    expected_totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }
    expected_threads: dict[str, dict[str, object]] = {}
    expected_responses: list[tuple[int, datetime, str, str]] = []
    fixture_started = perf_counter()

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
            "quota_source_status, sample_fingerprint"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )

        def values():
            for index in range(rows):
                thread_index = index % threads
                observed = first + timedelta(seconds=index + 1)
                timestamp = observed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                thread_id = f"sha256:{thread_index + 1:064x}"
                response_id = f"sha256:{index + 1:064x}"
                input_tokens = 100 + index % 97
                output_tokens = 20 + index % 13
                total_tokens = input_tokens + output_tokens
                cached_tokens = thread_index % 31
                reasoning_tokens = index % 11
                for name, value in (
                    ("input_tokens", input_tokens),
                    ("output_tokens", output_tokens),
                    ("total_tokens", total_tokens),
                    ("cached_tokens", cached_tokens),
                    ("reasoning_tokens", reasoning_tokens),
                ):
                    expected_totals[name] += value
                aggregate = expected_threads.setdefault(thread_id, {
                    "total": 0,
                    "input": 0,
                    "cached": 0,
                    "count": 0,
                    "last": observed,
                })
                aggregate["total"] += total_tokens
                aggregate["input"] += input_tokens
                aggregate["cached"] += cached_tokens
                aggregate["count"] += 1
                aggregate["last"] = max(aggregate["last"], observed)
                expected_responses.append(
                    (total_tokens, observed, response_id, thread_id),
                )
                expected_responses.sort(
                    key=lambda item: (-item[0], -item[1].timestamp(), item[2]),
                )
                del expected_responses[5:]
                yield (
                    SCHEMA_VERSION,
                    timestamp,
                    timestamp,
                    thread_id,
                    response_id,
                    "perf-model-001",
                    "dashboard",
                    "exact",
                    1,
                    input_tokens,
                    output_tokens,
                    total_tokens,
                    cached_tokens,
                    reasoning_tokens,
                    "unavailable",
                    f"perf-{index:064x}",
                )

        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executemany(sql, values())
            summary_plan = connection.execute(
                "EXPLAIN QUERY PLAN SELECT id, sampled_at_utc, "
                "source_observed_at_utc, thread_safe_id, response_safe_id, "
                "source_status, source_available, input_tokens, output_tokens, "
                "total_tokens, cached_tokens, reasoning_tokens "
                "FROM usage_history_samples WHERE (input_tokens IS NOT NULL OR "
                "output_tokens IS NOT NULL OR total_tokens IS NOT NULL OR "
                "cached_tokens IS NOT NULL OR reasoning_tokens IS NOT NULL) "
                "ORDER BY thread_safe_id, response_safe_id, "
                "source_observed_at_utc, sampled_at_utc, id",
            ).fetchall()
        fixture_build_elapsed = perf_counter() - fixture_started

        tracemalloc.start()
        started = perf_counter()
        summary = store.summarize_usage(
            UsageWindowKind.ROLLING_30D,
            as_of_utc=as_of,
            local_timezone=timezone.utc,
        )
        elapsed = perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        trend_started = perf_counter()
        trend = store.query(
            30,
            "sha256:" + f"{1:064x}",
            now=as_of,
        )
        trend_elapsed = perf_counter() - trend_started

    actual_totals = {
        "input_tokens": summary.input_tokens.value,
        "output_tokens": summary.output_tokens.value,
        "total_tokens": summary.total_tokens.value,
        "cached_tokens": summary.cached_tokens.value,
        "reasoning_tokens": summary.reasoning_tokens.value,
    }
    if summary.observed_response_count != rows or actual_totals != expected_totals:
        raise AssertionError({
            "expected_count": rows,
            "actual_count": summary.observed_response_count,
            "expected_totals": expected_totals,
            "actual_totals": actual_totals,
        })
    if summary.covered_thread_count != threads:
        raise AssertionError({
            "expected_threads": threads,
            "actual_threads": summary.covered_thread_count,
        })

    expected_high_threads = sorted(
        expected_threads.items(),
        key=lambda item: (
            -item[1]["total"],
            -item[1]["last"].timestamp(),
            item[0],
        ),
    )[:5]
    expected_low_cache = sorted(
        expected_threads.items(),
        key=lambda item: (
            item[1]["cached"] / item[1]["input"],
            -item[1]["input"],
            item[0],
        ),
    )[:3]
    insights = summary.insights
    actual_high_threads = [
        (item.thread_safe_id, item.total_tokens)
        for item in insights.high_usage_threads
    ]
    actual_high_responses = [
        (item.total_tokens, item.observed_at, item.response_safe_id, item.thread_safe_id)
        for item in insights.high_usage_responses
    ]
    actual_low_cache = [
        (
            item.thread_safe_id,
            item.valid_input_tokens,
            item.valid_cached_tokens,
        )
        for item in insights.low_cache_reuse_threads
    ]
    expected_high_thread_pairs = [
        (thread_id, aggregate["total"])
        for thread_id, aggregate in expected_high_threads
    ]
    expected_low_cache_pairs = [
        (thread_id, aggregate["input"], aggregate["cached"])
        for thread_id, aggregate in expected_low_cache
    ]
    top5_threads_verified = actual_high_threads == expected_high_thread_pairs
    top5_responses_verified = actual_high_responses == expected_responses
    top3_low_cache_verified = actual_low_cache == expected_low_cache_pairs
    if not all((
        top5_threads_verified,
        top5_responses_verified,
        top3_low_cache_verified,
    )):
        raise AssertionError({
            "top5_threads_verified": top5_threads_verified,
            "top5_responses_verified": top5_responses_verified,
            "top3_low_cache_verified": top3_low_cache_verified,
        })

    source = inspect.getsource(UsageHistoryStore.summarize_usage)
    peak_mib = peak / (1024 * 1024)
    stable_sort_verified = (
        list(insights.high_usage_threads) == sorted(
            insights.high_usage_threads,
            key=lambda item: (
                -item.total_tokens,
                -item.last_observed_at.timestamp(),
                item.thread_safe_id,
            ),
        )
        and list(insights.high_usage_responses) == sorted(
            insights.high_usage_responses,
            key=lambda item: (
                -item.total_tokens,
                -item.observed_at.timestamp(),
                item.response_safe_id,
            ),
        )
        and list(insights.low_cache_reuse_threads) == sorted(
            insights.low_cache_reuse_threads,
            key=lambda item: (
                item.cache_reuse,
                -item.valid_input_tokens,
                item.thread_safe_id,
            ),
        )
    )
    if elapsed > 30 or peak_mib > 512:
        raise AssertionError({
            "elapsed_seconds": elapsed,
            "peak_memory_mib": peak_mib,
        })
    return {
        "rows_in_30d": rows,
        "thread_count": threads,
        "range": UsageWindowKind.ROLLING_30D.value,
        "elapsed_seconds": round(elapsed, 6),
        "fixture_build_seconds": round(fixture_build_elapsed, 6),
        "trend_elapsed_seconds": round(trend_elapsed, 6),
        "trend_sample_count": trend.sample_count,
        "peak_memory_bytes": peak,
        "peak_memory_mib": round(peak_mib, 6),
        "coverage_status": insights.coverage_status.value,
        "observed_response_count": summary.observed_response_count,
        "covered_thread_count": summary.covered_thread_count,
        "top5_threads_verified": top5_threads_verified,
        "top5_responses_verified": top5_responses_verified,
        "top3_low_cache_verified": top3_low_cache_verified,
        "stable_sort_verified": stable_sort_verified,
        "select_star_found": "SELECT *" in source,
        "unbounded_fetchall_found": ".fetchall()" in source,
        "fetch_mode": "strict projection + fetchmany(512) + one canonical pass",
        "ui_thread_path": "trend-query-worker -> Tk root.after result polling",
        "summary_query_plan": [str(tuple(row)) for row in summary_plan],
        "top_threads": [
            {"label": item.safe_thread_label, "total_tokens": item.total_tokens}
            for item in insights.high_usage_threads
        ],
        "top_responses": [
            {"rank": index, "total_tokens": item.total_tokens}
            for index, item in enumerate(insights.high_usage_responses, 1)
        ],
        "low_cache_threads": [
            {"label": item.safe_thread_label, "cache_reuse": item.cache_reuse}
            for item in insights.low_cache_reuse_threads
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--threads", type=int, default=20_000)
    args = parser.parse_args()
    print(json.dumps(
        run_benchmark(args.rows, args.threads),
        indent=2,
        sort_keys=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
