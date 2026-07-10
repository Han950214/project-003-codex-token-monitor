"""Testable data refresh boundary for the Dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.codex_logs import CodexLogsReader, CodexLogsResult, CodexResponseUsage
from app.codex_state import CodexThreadTotal, load_latest_thread_total
from app.metrics import PricingConfig, RunUsage, SessionSummary, summarize_runs
from app.models import AgentRun
from app.storage import LoadResult, load_runs


@dataclass(frozen=True)
class DashboardSnapshot:
    runs: list[AgentRun]
    summary: SessionSummary
    logs: CodexLogsResult
    state_total: CodexThreadTotal | None
    storage_error: str | None = None


class DashboardViewModel:
    def __init__(
        self,
        pricing: PricingConfig,
        runs_path: Path,
        runs_loader: Callable[[Path], LoadResult] = load_runs,
        logs_loader: Callable[[], CodexLogsResult] | None = None,
        state_loader: Callable[[], CodexThreadTotal | None] = load_latest_thread_total,
    ) -> None:
        self.pricing = pricing
        self.runs_path = runs_path
        self.runs_loader = runs_loader
        self.logs_reader = CodexLogsReader() if logs_loader is None else None
        self.logs_loader = logs_loader or self.logs_reader.refresh
        self.state_loader = state_loader

    def refresh(self, runs: list[AgentRun] | None = None) -> DashboardSnapshot:
        load_result = self.runs_loader(self.runs_path) if runs is None else LoadResult(runs)
        logs = self.logs_loader()
        state_total = self.state_loader()
        summary = summarize_runs(
            load_result.runs,
            self.pricing,
            state_total.total_tokens if state_total else None,
            run_usage_from_codex_logs(logs.usage),
        )
        return DashboardSnapshot(
            runs=load_result.runs,
            summary=summary,
            logs=logs,
            state_total=state_total,
            storage_error=load_result.error,
        )


def run_usage_from_codex_logs(latest_usage: CodexResponseUsage | None) -> RunUsage | None:
    if latest_usage is None:
        return None
    return RunUsage(
        input_tokens=latest_usage.input_tokens,
        output_tokens=latest_usage.output_tokens,
        optional_log_tokens=max(latest_usage.total_tokens - latest_usage.input_tokens - latest_usage.output_tokens, 0),
        observed_cached_input_tokens=latest_usage.cached_tokens,
    )
