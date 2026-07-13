"""Deterministic workflow advice based only on safe numeric metadata."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.dashboard import DashboardSnapshot
    from app.quota import CodexQuotaSnapshot


QUOTA_RISK_REMAINING_PERCENT = 15.0
NEW_THREAD_TURN_COUNT = 30
OPTIMIZE_INPUT_TOKENS = 60_000
OPTIMIZE_CACHE_HIT_PERCENT = 20.0
DATA_STALE_AFTER = timedelta(minutes=3)

ADVISOR_RULE_CODES = (
    "data_unavailable",
    "data_stale",
    "quota_risk",
    "new_thread",
    "optimize_cache_reuse",
    "normal",
)

PRIORITY = {
    "data_unavailable": 0,
    "quota_risk": 1,
    "new_thread": 2,
    "optimize": 3,
    "normal": 4,
}

_SAFE_EVIDENCE_KEYS = {
    "data_available",
    "data_age_seconds",
    "source_status",
    "five_hour_remaining_percent",
    "weekly_remaining_percent",
    "turn_count",
    "instruction_input_tokens",
    "instruction_total_tokens",
    "cached_input_tokens",
    "cache_hit_percent_derived",
    "session_total_tokens",
    "session_status",
}
_SAFE_STRING_VALUES = {
    "normal", "stale", "invalid", "unavailable", "in_progress", "exact",
    "completed_partial", "incomplete", "completed", "no_selection",
}


EvidenceValue = int | float | bool | str | None


@dataclass(frozen=True)
class AdvisorInput:
    data_available: bool
    data_age_seconds: int | None
    source_status: str
    five_hour_remaining_percent: float | None
    weekly_remaining_percent: float | None
    turn_count: int | None
    instruction_input_tokens: int | None
    instruction_total_tokens: int | None
    cached_input_tokens: int | None
    session_total_tokens: int | None
    session_status: str
    observed_at: datetime


@dataclass(frozen=True)
class Recommendation:
    code: str
    severity: str
    title_key: str
    body_key: str
    primary_action: str
    evidence: tuple[tuple[str, EvidenceValue], ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        for key, value in self.evidence:
            if key not in _SAFE_EVIDENCE_KEYS:
                raise ValueError(f"unsafe_evidence_key:{key}")
            if isinstance(value, str) and value not in _SAFE_STRING_VALUES:
                raise ValueError(f"unsafe_evidence_value:{key}")

    @property
    def status(self) -> str:
        if self.code in {"data_unavailable", "data_stale"}:
            return "data_unavailable"
        if self.code == "quota_risk":
            return "quota_risk"
        if self.code == "new_thread":
            return "new_thread"
        if self.code.startswith("optimize"):
            return "optimize"
        return "normal"


@dataclass(frozen=True)
class AdvisorResult:
    recommendations: tuple[Recommendation, ...]

    @property
    def primary(self) -> Recommendation:
        return self.recommendations[0]


def build_advisor_input(
    snapshot: "DashboardSnapshot | None",
    quota: "CodexQuotaSnapshot",
    *,
    now: datetime | None = None,
) -> AdvisorInput:
    now = now or datetime.now(timezone.utc)
    selected = snapshot.selected_session if snapshot is not None else None
    instruction = selected.instruction if selected is not None else None
    usage = instruction.usage if instruction is not None else None
    cumulative = selected.thread_cumulative_usage if selected is not None else None
    refreshed_at = snapshot.sessions_result.refreshed_at if snapshot is not None else None
    age = None if refreshed_at is None else max(0, round((now - refreshed_at).total_seconds()))
    data_available = bool(selected is not None and selected.status != "unavailable" and usage is not None)
    five_hour = quota.five_hour
    weekly = quota.weekly
    return AdvisorInput(
        data_available=data_available,
        data_age_seconds=age,
        source_status=quota.source_status,
        five_hour_remaining_percent=(
            five_hour.remaining_percent if five_hour.available and not five_hour.stale else None
        ),
        weekly_remaining_percent=(
            weekly.remaining_percent if weekly.available and not weekly.stale else None
        ),
        turn_count=getattr(selected, "turn_count", None) if selected is not None else None,
        instruction_input_tokens=usage.input_tokens if usage is not None else None,
        instruction_total_tokens=usage.total_tokens if usage is not None else None,
        cached_input_tokens=usage.cached_input_tokens if usage is not None else None,
        session_total_tokens=cumulative.total_tokens if cumulative is not None else None,
        session_status=("unavailable" if selected is None else selected.status),
        observed_at=now,
    )


def evaluate_advice(data: AdvisorInput) -> AdvisorResult:
    recommendations: list[Recommendation] = []
    if not data.data_available:
        recommendations.append(_make(
            "data_unavailable", "failure", "advisor_data_unavailable_title",
            "advisor_data_unavailable_body", "diagnose",
            (("data_available", False), ("session_status", data.session_status)), data.observed_at,
        ))
    elif data.data_age_seconds is not None and data.data_age_seconds > DATA_STALE_AFTER.total_seconds():
        recommendations.append(_make(
            "data_stale", "failure", "advisor_data_unavailable_title",
            "advisor_data_stale_body", "diagnose",
            (("data_age_seconds", data.data_age_seconds),), data.observed_at,
        ))

    risky = [
        value for value in (
            data.five_hour_remaining_percent,
            data.weekly_remaining_percent,
        )
        if value is not None and value <= QUOTA_RISK_REMAINING_PERCENT
    ]
    if risky:
        recommendations.append(_make(
            "quota_risk", "warning", "advisor_quota_risk_title", "advisor_quota_risk_body",
            "view_quota",
            (
                ("five_hour_remaining_percent", data.five_hour_remaining_percent),
                ("weekly_remaining_percent", data.weekly_remaining_percent),
                ("source_status", data.source_status),
            ),
            data.observed_at,
        ))

    if data.turn_count is not None and data.turn_count >= NEW_THREAD_TURN_COUNT:
        recommendations.append(_make(
            "new_thread", "warning", "advisor_new_thread_title", "advisor_new_thread_body",
            "prepare_new_thread",
            (("turn_count", data.turn_count), ("session_total_tokens", data.session_total_tokens)),
            data.observed_at,
        ))

    cache_hit = _cache_hit(data.instruction_input_tokens, data.cached_input_tokens)
    if (
        data.instruction_input_tokens is not None
        and data.instruction_input_tokens >= OPTIMIZE_INPUT_TOKENS
        and cache_hit is not None
        and cache_hit < OPTIMIZE_CACHE_HIT_PERCENT
    ):
        recommendations.append(_make(
            "optimize_cache_reuse", "notice", "advisor_optimize_title", "advisor_optimize_body",
            "view_advice",
            (
                ("instruction_input_tokens", data.instruction_input_tokens),
                ("cached_input_tokens", data.cached_input_tokens),
                ("cache_hit_percent_derived", round(cache_hit, 1)),
            ),
            data.observed_at,
        ))

    if not recommendations:
        recommendations.append(_make(
            "normal", "normal", "advisor_normal_title", "advisor_normal_body", "view_current_task",
            (
                ("turn_count", data.turn_count),
                ("instruction_total_tokens", data.instruction_total_tokens),
                ("session_status", data.session_status),
            ),
            data.observed_at,
        ))
    recommendations.sort(key=lambda item: (PRIORITY[item.status], ADVISOR_RULE_CODES.index(item.code)))
    return AdvisorResult(tuple(recommendations))


def _make(
    code: str,
    severity: str,
    title_key: str,
    body_key: str,
    primary_action: str,
    evidence: tuple[tuple[str, EvidenceValue], ...],
    observed_at: datetime,
) -> Recommendation:
    return Recommendation(code, severity, title_key, body_key, primary_action, evidence, observed_at)


def _cache_hit(input_tokens: int | None, cached_tokens: int | None) -> float | None:
    if input_tokens is None or cached_tokens is None or input_tokens <= 0:
        return None
    return cached_tokens / input_tokens * 100.0
