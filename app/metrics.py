"""Local-estimate telemetry calculations for Codex Token Monitor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


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


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def current_hit(usage: RunUsage) -> float:
    cached = (
        usage.observed_cached_input_tokens
        if usage.observed_cached_input_tokens is not None
        else usage.stable_prefix_tokens
    )
    return _safe_ratio(max(cached, 0), usage.input_tokens)


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

