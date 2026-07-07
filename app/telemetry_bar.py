"""Tkinter telemetry bar for local-estimate values."""

from __future__ import annotations

import tkinter as tk

from app.metrics import (
    PricingConfig,
    RunUsage,
    average_hit,
    budget_remaining,
    context_usage,
    current_cost,
    current_hit,
    current_tokens,
    session_cost,
    session_tokens,
)


def format_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def build_telemetry_values(usages: list[RunUsage], pricing: PricingConfig) -> list[tuple[str, str]]:
    current = usages[-1]
    spent = session_cost(usages, pricing)
    current_context_tokens = session_tokens(usages)
    values = [
        ("本次命中率 / current cache hit", format_percent(current_hit(current))),
        ("平均命中率 / average cache hit", format_percent(average_hit(usages))),
        ("会话 tokens / session tokens", str(session_tokens(usages))),
        ("本次 tokens / current run tokens", str(current_tokens(current))),
        ("本次费用 / current cost", f"${current_cost(current, pricing):.6f}"),
        ("当前会话轮数 / session rounds", str(len(usages))),
        ("上下文占用 / context usage", format_percent(context_usage(current_context_tokens, pricing.configured_context_window))),
        ("压缩阈值 / compression threshold", format_percent(pricing.compression_threshold)),
        ("会话费用 / session cost", f"${spent:.6f}"),
        ("预算剩余 / budget remaining", f"${budget_remaining(pricing.configured_budget, spent):.6f}"),
    ]
    return [(label, f"{value} 本地估算 / local estimate") for label, value in values]


def create_telemetry_bar(parent: tk.Widget, usages: list[RunUsage], pricing: PricingConfig) -> tk.Frame:
    frame = tk.Frame(parent, bg="#15202b", padx=8, pady=6)
    for column, (label, value) in enumerate(build_telemetry_values(usages, pricing)):
        cell = tk.Frame(frame, bg="#15202b", padx=6)
        tk.Label(cell, text=label, fg="#c8d3df", bg="#15202b", font=("Segoe UI", 8)).pack(anchor="w")
        tk.Label(cell, text=value, fg="#ffffff", bg="#15202b", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        cell.grid(row=0, column=column, sticky="nsew")
        frame.grid_columnconfigure(column, weight=1)
    return frame

