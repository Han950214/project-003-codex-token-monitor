"""Fixed six-field telemetry bar for the Dashboard."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from app.codex_logs import CodexLogsResult
from app.metrics import PricingConfig, RunUsage, SessionSummary
from app.ui_presenter import DashboardPresentation
from app.ui_theme import SPACE_2, SPACE_3


TELEMETRY_FIELD_LABELS = (
    "Codex Token Monitor",
    "Current Total",
    "Cache Hit",
    "Session Total",
    "Data Status",
    "Auto Refresh",
)


def build_telemetry_values(presentation: DashboardPresentation) -> tuple[tuple[str, str], ...]:
    return (
        (TELEMETRY_FIELD_LABELS[0], "Local monitor"),
        (TELEMETRY_FIELD_LABELS[1], presentation.telemetry_current_total),
        (TELEMETRY_FIELD_LABELS[2], presentation.telemetry_cache_hit),
        (TELEMETRY_FIELD_LABELS[3], presentation.telemetry_session_total),
        (TELEMETRY_FIELD_LABELS[4], presentation.data_status.value),
        (TELEMETRY_FIELD_LABELS[5], presentation.auto_refresh.removeprefix("Auto Refresh: ")),
    )


class TelemetryBar(ttk.Frame):
    """A stable widget tree; refreshes only update StringVars."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, style="Telemetry.TFrame", padding=(SPACE_3, SPACE_2))
        self.value_vars = tuple(tk.StringVar(value="—") for _ in TELEMETRY_FIELD_LABELS)
        for column, (label, variable) in enumerate(zip(TELEMETRY_FIELD_LABELS, self.value_vars)):
            cell = ttk.Frame(self, style="Telemetry.TFrame", padding=(SPACE_2, 0))
            ttk.Label(cell, text=label, style="TelemetryLabel.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Label(cell, textvariable=variable, style="TelemetryValue.TLabel").grid(row=1, column=0, sticky="w")
            cell.grid(row=0, column=column, sticky="nsew")
            self.grid_columnconfigure(column, weight=1, uniform="telemetry")

    def update_values(self, values: tuple[tuple[str, str], ...]) -> None:
        if tuple(label for label, _ in values) != TELEMETRY_FIELD_LABELS:
            raise ValueError("telemetry fields must use the fixed six-field order")
        for variable, (_, value) in zip(self.value_vars, values):
            variable.set(value)


# Compatibility formatters retained for existing non-GUI callers and reports tests.
def format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def build_latest_response_values(result: CodexLogsResult) -> list[tuple[str, str]]:
    usage = result.usage
    source = result.source if usage is not None else "unknown"
    values = [
        ("Input tokens", _known(usage.input_tokens if usage else None, source)),
        ("Output tokens", _known(usage.output_tokens if usage else None, source)),
        ("Total tokens", _known(usage.total_tokens if usage else None, source)),
        ("Cached tokens", _known(usage.cached_tokens if usage else None, source)),
        ("Reasoning tokens", _known(usage.reasoning_tokens if usage else None, source)),
    ]
    if usage is not None and usage.input_tokens > 0:
        hit = min(max(usage.cached_tokens, 0), usage.input_tokens) / usage.input_tokens
        cache = f"{format_percent(hit)} derived from codex_logs_sqlite / real usage; not official cache hit rate"
    else:
        cache = "unknown / source: unknown"
    values.append(("Derived cache hit", cache))
    return values


def build_logs_adapter_metadata(result: CodexLogsResult) -> list[tuple[str, str]]:
    values = [("Logs adapter", result.status.value)]
    if result.observed_at is not None:
        values.append(("Latest response at", result.observed_at.astimezone().isoformat(timespec="seconds")))
    label = "Refreshed at" if result.status.value in {"connected", "no response.completed"} else "Refresh attempted at"
    values.append((label, result.refreshed_at.astimezone().isoformat(timespec="seconds")))
    return values


def build_telemetry_values_from_summary(summary: SessionSummary, _pricing: PricingConfig) -> list[tuple[str, str]]:
    cache = f"{format_percent(summary.current_cache_hit)} local estimate, not real Codex cache"
    current = f"{summary.current_run_tokens} local estimate"
    session = f"{summary.session_tokens} local estimate"
    if summary.current_usage_source == "codex_logs_sqlite":
        current = f"{summary.current_run_tokens} codex_logs_sqlite / real usage"
    if summary.current_cache_hit_source == "codex_logs_sqlite":
        cache = f"{format_percent(summary.current_cache_hit)} derived from codex_logs_sqlite / real usage, not official cache hit rate"
    if summary.total_tokens_source == "codex_state_sqlite":
        session = f"{summary.session_tokens} codex_state_sqlite / real total"
    return [
        ("本次命中率 / current cache hit", cache),
        ("平均命中率 / average cache hit", f"{format_percent(summary.average_cache_hit)} local estimate, not real Codex cache"),
        ("会话 tokens / session tokens", session),
        ("本次 tokens / current run tokens", current),
        ("本次费用 / current cost", f"${summary.current_cost:.6f} local estimate, not billing"),
        ("当前会话轮数 / session rounds", f"{summary.rounds} local estimate"),
        ("上下文占用 / context usage", f"{format_percent(summary.context_usage)} local estimate"),
        ("压缩阈值 / compression threshold", "local estimate"),
        ("会话费用 / session cost", f"${summary.session_cost:.6f} local estimate, not billing"),
        ("预算剩余 / budget remaining", f"${summary.budget_remaining:.6f} local estimate"),
    ]


def build_legacy_telemetry_values(usages: list[RunUsage], pricing: PricingConfig) -> list[tuple[str, str]]:
    from app.metrics import summarize_runs

    return build_telemetry_values_from_summary(summarize_runs([], pricing, latest_response_usage=usages[-1] if usages else None), pricing)


def _known(value: int | None, source: str) -> str:
    return "unknown / source: unknown" if value is None else f"{value} {source}"
