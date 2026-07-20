"""Pure formatting and responsive-layout helpers for desktop UI surfaces."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def format_compact_token_count(value: int | None) -> str:
    """Format a token count without mutating or approximating the source value."""
    if value is None:
        return "—"
    number = int(value)
    sign = "-" if number < 0 else ""
    absolute = abs(number)
    if absolute < 1_000:
        return f"{number}"
    if absolute < 1_000_000:
        rounded = _rounded(absolute, 1_000, 1)
        if rounded >= 1_000:
            return f"{sign}{_rounded(absolute, 1_000_000, 2):.2f}M"
        return f"{sign}{rounded:.1f}K"
    if absolute < 1_000_000_000:
        rounded = _rounded(absolute, 1_000_000, 2)
        if rounded >= 1_000:
            return f"{sign}{_rounded(absolute, 1_000_000_000, 2):.2f}B"
        return f"{sign}{rounded:.2f}M"
    return f"{sign}{_rounded(absolute, 1_000_000_000, 2):.2f}B"


def format_localized_token_count(value: int | None, language: str) -> str:
    """Format tokens using the user's language without changing full values."""

    if language != "zh-CN":
        return format_compact_token_count(value)
    if value is None:
        return "—"
    number = int(value)
    sign = "-" if number < 0 else ""
    absolute = abs(number)
    if absolute < 1_000:
        return f"{number}"
    if absolute < 10_000:
        return f"{sign}{_trim_decimal(_rounded(absolute, 1_000, 1))}K"
    if absolute < 100_000_000:
        decimals = 1 if absolute < 1_000_000 else 0
        return f"{sign}{_trim_decimal(_rounded(absolute, 10_000, decimals))}万"
    decimals = 2 if absolute < 1_000_000_000 else 0
    return f"{sign}{_trim_decimal(_rounded(absolute, 100_000_000, decimals))}亿"


def format_full_token_count(value: int | None) -> str:
    return "—" if value is None else f"{int(value):,}"


def ellipsize_title(value: str | None, limit: int = 48) -> str:
    title = " ".join((value or "").split())
    if len(title) <= limit:
        return title
    return title[: max(1, limit - 1)].rstrip() + "…"


def metric_columns_for_width(width: int) -> int:
    if width >= 1_100:
        return 6
    if width >= 900:
        return 3
    if width >= 380:
        return 2
    return 1


def dashboard_layout_for_width(width: int) -> str:
    if width >= 1_200:
        return "wide"
    if width >= 900:
        return "medium"
    return "narrow"


def _rounded(value: int, divisor: int, decimals: int) -> Decimal:
    quantum = Decimal(1).scaleb(-decimals)
    return (Decimal(value) / Decimal(divisor)).quantize(quantum, rounding=ROUND_HALF_UP)


def _trim_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
