"""Testable Rollout-to-Dashboard data refresh boundary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.codex_rollout import CodexRolloutReader, InstructionUsage, RolloutUsageResult
from app.codex_state import CodexThreadTotal, load_thread_total
from app.metrics import PricingConfig, SessionSummary, summarize_runs
from app.models import AgentRun
from app.storage import LoadResult, load_runs


@dataclass(frozen=True)
class DashboardSnapshot:
    runs: list[AgentRun]
    summary: SessionSummary
    rollout: RolloutUsageResult
    state_total: CodexThreadTotal | None
    state_reconciled: bool
    storage_error: str | None = None


class DashboardViewModel:
    def __init__(
        self,
        pricing: PricingConfig,
        runs_path: Path,
        runs_loader: Callable[[Path], LoadResult] = load_runs,
        rollout_loader: Callable[[], RolloutUsageResult] | None = None,
        state_loader: Callable[[str], CodexThreadTotal | None] = load_thread_total,
    ) -> None:
        self.pricing = pricing
        self.runs_path = runs_path
        self.runs_loader = runs_loader
        self.rollout_reader = CodexRolloutReader() if rollout_loader is None else None
        self.rollout_loader = rollout_loader or self.rollout_reader.refresh
        self.state_loader = state_loader

    def refresh(self, runs: list[AgentRun] | None = None) -> DashboardSnapshot:
        load_result = self.runs_loader(self.runs_path) if runs is None else LoadResult(runs)
        rollout = self.rollout_loader()
        state_total = self.state_loader(rollout.thread_id) if rollout.thread_id else None
        usage = rollout.instruction.usage if rollout.instruction and rollout.instruction.exact else None
        summary = summarize_runs(load_result.runs, self.pricing, state_total.total_tokens if state_total else None)
        state_reconciled = bool(state_total and rollout.instruction and usage and state_total.thread_id == rollout.thread_id)
        return DashboardSnapshot(load_result.runs, summary, rollout, state_total, state_reconciled, load_result.error)


def instruction_usage(snapshot: DashboardSnapshot) -> InstructionUsage | None:
    instruction = snapshot.rollout.instruction
    return instruction if instruction and (instruction.exact or instruction.in_progress) else None
