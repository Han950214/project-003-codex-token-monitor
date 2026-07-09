"""Local-estimate telemetry calculations for Codex Token Monitor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.models import AgentRun


@dataclass(frozen=True)
class PricingConfig:
    input_token_price: float
    cached_input_token_price: float
    output_token_price: float
    unit_tokens: int = 1_000_000
    configured_budget: float = 0.0
    configured_context_window: int = 200_000
    compression_threshold: float = 0.85


@dataclass(frozen=True)
class RunUsage:
    input_tokens: int
    output_tokens: int
    optional_log_tokens: int = 0
    stable_prefix_tokens: int = 0
    observed_cached_input_tokens: int | None = None


@dataclass(frozen=True)
class SessionSummary:
    rounds: int
    session_tokens: int
    current_run_tokens: int
    current_cost: float
    session_cost: float
    current_cache_hit: float
    average_cache_hit: float
    context_usage: float
    budget_remaining: float


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def current_hit(usage: RunUsage) -> float:
    return _safe_ratio(cached_input_tokens(usage), usage.input_tokens)


def average_hit(usages: Iterable[RunUsage]) -> float:
    total_weight = 0
    weighted_hits = 0.0
    for usage in usages:
        weight = max(usage.input_tokens, 0)
        total_weight += weight
        weighted_hits += current_hit(usage) * weight
    return _safe_ratio(weighted_hits, total_weight)


def current_tokens(usage: RunUsage) -> int:
    return max(usage.input_tokens, 0) + max(usage.output_tokens, 0) + max(usage.optional_log_tokens, 0)


def session_tokens(usages: Iterable[RunUsage]) -> int:
    return sum(current_tokens(usage) for usage in usages)


def cached_input_tokens(usage: RunUsage) -> int:
    cached = (
        usage.observed_cached_input_tokens
        if usage.observed_cached_input_tokens is not None
        else usage.stable_prefix_tokens
    )
    return min(max(cached, 0), max(usage.input_tokens, 0))


def uncached_input_tokens(usage: RunUsage) -> int:
    return max(max(usage.input_tokens, 0) - cached_input_tokens(usage), 0)


def current_cost(usage: RunUsage, pricing: PricingConfig) -> float:
    unit = pricing.unit_tokens if pricing.unit_tokens > 0 else 1
    input_rate = pricing.input_token_price / unit
    cached_rate = pricing.cached_input_token_price / unit
    output_rate = pricing.output_token_price / unit
    return (
        uncached_input_tokens(usage) * input_rate
        + cached_input_tokens(usage) * cached_rate
        + max(usage.output_tokens, 0) * output_rate
    )


def session_cost(usages: Iterable[RunUsage], pricing: PricingConfig) -> float:
    return sum(current_cost(usage, pricing) for usage in usages)


def context_usage(current_context_tokens: int, configured_context_window: int) -> float:
    return _safe_ratio(max(current_context_tokens, 0), configured_context_window)


def budget_remaining(configured_budget: float, spent: float) -> float:
    return configured_budget - spent


def estimate_tokens_from_text(text: str) -> int:
    # Conservative local estimate for mixed English/Chinese text.
    if not text:
        return 0
    return max(1, round(len(text) / 4))


def usage_from_run(run: AgentRun) -> RunUsage:
    return RunUsage(
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        stable_prefix_tokens=run.cached_tokens,
        observed_cached_input_tokens=run.cached_tokens,
    )


def build_run_estimates(
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    pricing: PricingConfig,
) -> tuple[int, float, float]:
    usage = RunUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        observed_cached_input_tokens=cached_tokens,
    )
    return current_tokens(usage), current_cost(usage, pricing), current_hit(usage)


def summarize_runs(runs: list[AgentRun], pricing: PricingConfig) -> SessionSummary:
    usages = [usage_from_run(run) for run in runs]
    current_usage = usages[-1] if usages else RunUsage(input_tokens=0, output_tokens=0)
    spent = sum(max(run.estimated_cost, 0.0) for run in runs)
    total_tokens = sum(max(run.total_tokens, 0) for run in runs)
    return SessionSummary(
        rounds=len(runs),
        session_tokens=total_tokens,
        current_run_tokens=current_tokens(current_usage),
        current_cost=current_cost(current_usage, pricing),
        session_cost=spent,
        current_cache_hit=current_hit(current_usage),
        average_cache_hit=average_hit(usages),
        context_usage=context_usage(total_tokens, pricing.configured_context_window),
        budget_remaining=budget_remaining(pricing.configured_budget, spent),
    )
