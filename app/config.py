"""Configuration helpers for local-estimate pricing."""

from __future__ import annotations

import json
from pathlib import Path

from app.metrics import PricingConfig


DEFAULT_PRICING = PricingConfig(
    input_token_price=1.25,
    cached_input_token_price=0.125,
    output_token_price=10.0,
    unit_tokens=1_000_000,
    configured_budget=5.0,
    configured_context_window=200_000,
    compression_threshold=0.85,
)


def load_pricing(path: Path) -> PricingConfig:
    if not path.exists():
        return DEFAULT_PRICING
    data = json.loads(path.read_text(encoding="utf-8"))
    return PricingConfig(
        input_token_price=float(data.get("input_token_price", DEFAULT_PRICING.input_token_price)),
        cached_input_token_price=float(data.get("cached_input_token_price", DEFAULT_PRICING.cached_input_token_price)),
        output_token_price=float(data.get("output_token_price", DEFAULT_PRICING.output_token_price)),
        unit_tokens=int(data.get("unit_tokens", DEFAULT_PRICING.unit_tokens)),
        configured_budget=float(data.get("configured_budget", DEFAULT_PRICING.configured_budget)),
        configured_context_window=int(data.get("configured_context_window", DEFAULT_PRICING.configured_context_window)),
        compression_threshold=float(data.get("compression_threshold", DEFAULT_PRICING.compression_threshold)),
    )

