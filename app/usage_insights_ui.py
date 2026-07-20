"""Pure presentation mapping for safe response-level usage insights."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.codex_rollout import make_thread_safe_id
from app.i18n import translate
from app.ui_format import format_localized_token_count
from app.usage_summary import CoverageState, UsageInsightsResult, UsageWindowKind


_RANGE_LABEL_KEYS = {
    UsageWindowKind.TODAY: "observed_usage_today",
    UsageWindowKind.ROLLING_5H: "observed_usage_rolling_5h",
    UsageWindowKind.ROLLING_7D: "observed_usage_rolling_7d",
    UsageWindowKind.ROLLING_30D: "observed_usage_rolling_30d",
}


@dataclass(frozen=True)
class UsageInsightRowView:
    title: str
    primary: str
    details: str
    coverage: str
    kind: str = ""
    rank: int = 0
    thread_safe_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class UsageInsightSectionView:
    key: str
    title: str
    rows: tuple[UsageInsightRowView, ...]
    can_expand: bool
    expanded: bool
    toggle_text: str


@dataclass(frozen=True)
class UsageInsightsView:
    title: str
    scope_label: str
    range_label: str
    state_kind: str
    state_text: str
    sections: tuple[UsageInsightSectionView, ...]


def build_usage_insights_view(
    result: UsageInsightsResult,
    language: str,
    *,
    expanded_threads: bool,
    expanded_responses: bool,
) -> UsageInsightsView:
    """Format pre-ranked DTOs without reading, deduplicating, or sorting data."""

    available = result.source_available and result.coverage_status is not CoverageState.UNAVAILABLE
    has_rows = bool(
        result.high_usage_threads
        or result.high_usage_responses
        or result.low_cache_reuse_threads
    )
    if not available:
        state_kind = "unavailable"
        state_text = translate("usage_insights_unavailable", language)
    elif not has_rows:
        state_kind = "empty"
        state_text = translate("usage_insights_empty", language)
    elif result.coverage_status in {
        CoverageState.LIMITED_HISTORY,
        CoverageState.PARTIAL,
        CoverageState.UNKNOWN,
    }:
        state_kind = "partial"
        state_text = translate("usage_insights_partial", language)
    else:
        state_kind = "available"
        state_text = ""

    thread_limit = 5 if expanded_threads else 3
    response_limit = 5 if expanded_responses else 3
    thread_rows = tuple(
        _thread_row(item, rank, language)
        for rank, item in enumerate(result.high_usage_threads[:thread_limit], 1)
    ) if available and has_rows else ()
    response_rows = tuple(
        _response_row(item, rank, language)
        for rank, item in enumerate(result.high_usage_responses[:response_limit], 1)
    ) if available and has_rows else ()
    cache_rows = tuple(
        _cache_row(item, rank, language)
        for rank, item in enumerate(result.low_cache_reuse_threads[:3], 1)
    ) if available and has_rows else ()
    sections = (
        _section(
            "threads", "usage_insights_high_threads", thread_rows,
            len(result.high_usage_threads) > 3, expanded_threads, language,
        ),
        _section(
            "responses", "usage_insights_high_responses", response_rows,
            len(result.high_usage_responses) > 3, expanded_responses, language,
        ),
        _section(
            "cache", "usage_insights_low_cache", cache_rows,
            False, False, language,
        ),
    )
    return UsageInsightsView(
        title=translate("usage_insights_title", language),
        scope_label=translate("all_sessions_scope", language),
        range_label=translate(_RANGE_LABEL_KEYS[result.range_id], language),
        state_kind=state_kind,
        state_text=state_text,
        sections=sections,
    )


def _section(
    key: str,
    title_key: str,
    rows: tuple[UsageInsightRowView, ...],
    can_expand: bool,
    expanded: bool,
    language: str,
) -> UsageInsightSectionView:
    return UsageInsightSectionView(
        key=key,
        title=translate(title_key, language),
        rows=rows,
        can_expand=can_expand,
        expanded=expanded,
        toggle_text=translate(
            "usage_insights_show_less" if expanded else "usage_insights_show_more",
            language,
        ),
    )


def _thread_row(item: object, rank: int, language: str) -> UsageInsightRowView:
    metrics = _metrics(item, language)
    return UsageInsightRowView(
        title=translate(
            "usage_insights_thread_rank_label", language,
            rank=rank, time=_time(item.last_observed_at),
        ),
        primary=translate(
            "usage_insights_total_cache", language,
            total=_tokens(item.total_tokens, language), cache=_percent(item.cache_reuse),
        ),
        details=translate(
            "usage_insights_thread_summary", language,
            metrics=metrics, count=item.completed_response_count,
            label=_safe_session_label(item, language, turns=item.completed_response_count),
        ),
        coverage=_coverage(item.coverage_status, language),
        kind="thread",
        rank=rank,
        thread_safe_id=item.thread_safe_id,
    )


def _response_row(item: object, rank: int, language: str) -> UsageInsightRowView:
    metrics = _metrics(item, language)
    return UsageInsightRowView(
        title=translate(
            "usage_insights_response_rank_label", language,
            time=_time(item.observed_at), rank=rank,
        ),
        primary=translate(
            "usage_insights_total_cache", language,
            total=_tokens(item.total_tokens, language), cache=_percent(item.cache_reuse),
        ),
        details=translate(
            "usage_insights_response_session", language,
            metrics=metrics, label=_safe_session_label(item, language),
        ),
        coverage=_coverage(item.coverage_status, language),
        kind="response",
        rank=rank,
        thread_safe_id=item.thread_safe_id,
    )


def _cache_row(item: object, rank: int, language: str) -> UsageInsightRowView:
    return UsageInsightRowView(
        title=translate(
            "usage_insights_cache_rank_label", language,
            rank=rank, time=_time(item.last_observed_at),
        ),
        primary=translate(
            "usage_insights_cache_only", language,
            cache=_percent(item.cache_reuse),
        ),
        details=translate(
            "usage_insights_cache_summary", language,
            input=_tokens(item.valid_input_tokens, language),
            cached=_tokens(item.valid_cached_tokens, language),
            count=item.valid_response_count,
            label=_safe_session_label(item, language, turns=item.valid_response_count),
        ),
        coverage=_coverage(item.coverage_status, language),
        kind="cache",
        rank=rank,
        thread_safe_id=item.thread_safe_id,
    )


def _metrics(item: object, language: str) -> str:
    return translate(
        "usage_insights_metric_summary", language,
        input=_tokens(item.input_tokens, language),
        output=_tokens(item.output_tokens, language),
        cached=_tokens(item.cached_tokens, language),
        reasoning=_tokens(item.reasoning_tokens, language),
    )


def _tokens(value: int | None, language: str) -> str:
    return format_localized_token_count(value, language)


def _safe_session_label(item: object, language: str, turns: int | None = None) -> str:
    observed_at = getattr(item, "last_observed_at", None) or getattr(item, "observed_at", None)
    time = _time(observed_at) if observed_at is not None else "—"
    turn_text = (
        translate("task_turns_value", language, value=turns)
        if isinstance(turns, int) and turns > 0
        else translate("session_turn_unknown", language)
    )
    return translate(
        "ranking_session_fallback", language,
        time=time, turns=turn_text,
    )


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _time(value: object) -> str:
    return value.astimezone().strftime("%m-%d %H:%M")


def _coverage(value: str, language: str) -> str:
    return translate(
        "usage_insights_row_partial"
        if value == CoverageState.PARTIAL.value
        else "usage_insights_row_complete",
        language,
    )


def find_session_thread_id(
    thread_safe_id: str | None,
    sessions: object,
) -> str | None:
    """Resolve one safe ranking identity against the current in-memory sessions."""

    if not isinstance(thread_safe_id, str) or not thread_safe_id:
        return None
    for session in sessions:
        raw_thread_id = getattr(session, "thread_id", None)
        if not isinstance(raw_thread_id, str) or not raw_thread_id:
            continue
        if make_thread_safe_id(raw_thread_id) == thread_safe_id:
            return raw_thread_id
    return None
