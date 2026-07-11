"""Fixed six-field telemetry bar for the Dashboard."""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from app.codex_logs import CodexLogsResult
from app.metrics import PricingConfig, RunUsage, SessionSummary
from app.i18n import DEFAULT_LANGUAGE, localize_auto_refresh, localize_status, translate
from app.ui_presenter import DashboardPresentation
from app.ui_theme import COLORS, FONT_FAMILY, SPACE_2, SPACE_3


TELEMETRY_FIELD_LABELS = (
    "Codex Token Monitor",
    "Current Total",
    "Cache Hit",
    "Session Total",
    "Data Status",
    "Auto Refresh",
)


TELEMETRY_FIELD_KEYS = (
    None,
    "telemetry_current_total",
    "telemetry_cache_hit",
    "telemetry_session_total",
    "telemetry_data_status",
    "telemetry_auto_refresh",
)


def telemetry_field_labels(language: str = DEFAULT_LANGUAGE) -> tuple[str, ...]:
    return tuple(
        "Codex Token Monitor" if key is None else translate(key, language)
        for key in TELEMETRY_FIELD_KEYS
    )


def build_telemetry_values(
    presentation: DashboardPresentation,
    language: str = DEFAULT_LANGUAGE,
) -> tuple[tuple[str, str], ...]:
    labels = telemetry_field_labels(language)
    auto_enabled = "On" in presentation.auto_refresh
    return (
        (labels[0], translate("telemetry_local_monitor", language)),
        (labels[1], _localize_value(presentation.telemetry_current_total, language)),
        (labels[2], _localize_value(presentation.telemetry_cache_hit, language)),
        (labels[3], _localize_value(presentation.telemetry_session_total, language)),
        (labels[4], localize_status(presentation.data_status, language)),
        (labels[5], localize_auto_refresh(auto_enabled, language).replace("自动刷新：", "").replace("Auto Refresh: ", "")),
    )


class TelemetryBar(ctk.CTkFrame):
    """A stable widget tree; refreshes only update StringVars."""

    def __init__(self, parent: tk.Misc, language: str = DEFAULT_LANGUAGE) -> None:
        # Let the two text rows determine the final height so Windows DPI scaling
        # cannot clip the value row. All six cells still share one compact row.
        super().__init__(parent, fg_color=COLORS.telemetry, corner_radius=0, height=64)
        self.label_vars = tuple(tk.StringVar(value=value) for value in telemetry_field_labels(language))
        self.value_vars = tuple(tk.StringVar(value="—") for _ in TELEMETRY_FIELD_LABELS)
        self.grid_rowconfigure(0, weight=1)
        for column, (label_var, value_var) in enumerate(zip(self.label_vars, self.value_vars)):
            cell = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
            ctk.CTkLabel(cell, textvariable=label_var, text_color=COLORS.telemetry_muted, font=(FONT_FAMILY, 10), anchor="w").grid(row=0, column=0, sticky="ew")
            ctk.CTkLabel(cell, textvariable=value_var, text_color=COLORS.telemetry_text, font=(FONT_FAMILY, 12, "bold"), anchor="w").grid(row=1, column=0, sticky="ew")
            cell.grid(row=0, column=column, sticky="nsew", padx=(SPACE_3, SPACE_2), pady=(6, 10))
            cell.grid_columnconfigure(0, weight=1)
            self.grid_columnconfigure(column, weight=1, uniform="telemetry")

    def update_values(self, values: tuple[tuple[str, str], ...]) -> None:
        if len(values) != len(TELEMETRY_FIELD_LABELS):
            raise ValueError("telemetry must contain exactly six fields")
        for label_var, value_var, (label, value) in zip(self.label_vars, self.value_vars, values):
            label_var.set(label)
            value_var.set(value)


def _localize_value(value: str, language: str) -> str:
    for suffix, key in ((" real", "value_real"), (" estimate", "value_estimate"), (" derived", "value_derived")):
        if value.endswith(suffix):
            return translate(key, language, value=value[: -len(suffix)])
    return value


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
