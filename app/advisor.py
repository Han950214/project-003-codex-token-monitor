"""Deterministic workflow advice based only on safe numeric metadata."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.dashboard import DashboardSnapshot
    from app.quota import CodexQuotaSnapshot


QUOTA_RISK_REMAINING_PERCENT = 15.0
NEW_THREAD_TURN_COUNT = 30
OPTIMIZE_INPUT_TOKENS = 60_000
OPTIMIZE_CACHE_HIT_PERCENT = 20.0
DATA_STALE_AFTER = timedelta(minutes=3)
HISTORY_MIN_VALID_SAMPLES = 5
HISTORY_MIN_DISTINCT_OBSERVATIONS = 3
HISTORY_FLAT_RELATIVE_TOLERANCE = 0.05
HISTORY_FLAT_ABSOLUTE_TOLERANCE = 1.0

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
class AdvisorHistoryEvidence:
    """Derived comparison over one explicit safe numeric history scope."""

    metric: str
    direction: str
    current_value: float
    baseline_value: float
    minimum_value: float
    maximum_value: float
    sample_count: int
    distinct_observation_count: int
    range_started_at: datetime
    range_ended_at: datetime
    source: str = "token_monitor_history"
    derived: bool = True


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
    thread_safe_id: str | None = None
    history_samples: tuple[object, ...] = ()
    source_observed_at: datetime | None = None
    five_hour_observed_at: datetime | None = None
    weekly_observed_at: datetime | None = None
    quota_history_samples: tuple[object, ...] = ()


@dataclass(frozen=True)
class Recommendation:
    code: str
    severity: str
    title_key: str
    body_key: str
    primary_action: str
    evidence: tuple[tuple[str, EvidenceValue], ...]
    observed_at: datetime
    source: str = "current_snapshot"
    derived: bool = False
    history_evidence: AdvisorHistoryEvidence | None = None
    source_observed_at: datetime | None = None

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
    history_samples: Iterable[object] = (),
    quota_history_samples: Iterable[object] = (),
) -> AdvisorInput:
    now = now or datetime.now(timezone.utc)
    current = (
        getattr(snapshot, "current_session", None) or snapshot.selected_session
        if snapshot is not None else None
    )
    instruction = current.instruction if current is not None else None
    usage = instruction.usage if instruction is not None else None
    cumulative = current.thread_cumulative_usage if current is not None else None
    refreshed_at = snapshot.sessions_result.refreshed_at if snapshot is not None else None
    age = None if refreshed_at is None else max(0, round((now - refreshed_at).total_seconds()))
    data_available = bool(current is not None and current.status != "unavailable" and usage is not None)
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
        turn_count=getattr(current, "turn_count", None) if current is not None else None,
        instruction_input_tokens=usage.input_tokens if usage is not None else None,
        instruction_total_tokens=usage.total_tokens if usage is not None else None,
        cached_input_tokens=usage.cached_input_tokens if usage is not None else None,
        session_total_tokens=cumulative.total_tokens if cumulative is not None else None,
        session_status=("unavailable" if current is None else current.status),
        observed_at=now,
        thread_safe_id=(getattr(current, "thread_id", None) if current is not None else None),
        history_samples=tuple(history_samples),
        source_observed_at=(getattr(current, "observed_at", None) if current is not None else refreshed_at),
        five_hour_observed_at=five_hour.observed_at,
        weekly_observed_at=weekly.observed_at,
        quota_history_samples=tuple(quota_history_samples),
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
    recommendations = [
        replace(
            item,
            derived=item.code == "optimize_cache_reuse",
            history_evidence=_history_for_recommendation(item, data),
            source_observed_at=_recommendation_observed_at(item, data),
        )
        for item in recommendations
    ]
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


_HISTORY_FIELDS = {
    "instruction_input_tokens": ("instruction_input_tokens", "input_tokens"),
    "instruction_total_tokens": ("instruction_total_tokens", "total_tokens"),
    "cache_hit_percent_derived": (
        "cache_hit_percent_derived", "cache_reuse_percent", "cache_reuse",
    ),
    "session_total_tokens": ("session_total_tokens",),
    "turn_count": ("turn_count",),
    "five_hour_remaining_percent": (
        "five_hour_remaining_percent", "five_hour_quota_value",
    ),
    "weekly_remaining_percent": (
        "weekly_remaining_percent", "weekly_quota_value",
    ),
}
_QUOTA_HISTORY_METRICS = {
    "five_hour_remaining_percent",
    "weekly_remaining_percent",
}


def build_history_evidence(
    samples: Iterable[object],
    *,
    thread_safe_id: str | None,
    metric: str,
    current_value: int | float | None,
    current_observed_at: datetime | None = None,
) -> AdvisorHistoryEvidence | None:
    """Return a deterministic comparison or ``None`` when evidence is unsafe/weak."""

    current = _finite_number(current_value)
    current_time = _history_datetime(current_observed_at)
    quota_scope = metric in _QUOTA_HISTORY_METRICS
    if (
        metric not in _HISTORY_FIELDS
        or current is None
        or current_time is None
        or (not quota_scope and not thread_safe_id)
    ):
        return None
    valid: list[tuple[datetime, float]] = []
    quota_candidates: dict[
        tuple[object, ...], tuple[datetime, float, datetime]
    ] = {}
    try:
        for sample in samples:
            if quota_scope:
                if not _sample_is_global_quota(sample):
                    continue
            elif (
                _sample_is_global_quota(sample)
                or _sample_thread_id(sample) != thread_safe_id
            ):
                continue
            if not _sample_is_usable(sample, metric):
                continue
            observed_at = _sample_observed_at(sample, metric)
            identity_at = _sample_identity_at(sample, metric) or observed_at
            value = _sample_metric(sample, metric)
            if observed_at is None or identity_at is None or value is None:
                continue
            if quota_scope:
                identity = _quota_metric_identity(sample, metric, observed_at, value)
                previous = quota_candidates.get(identity)
                if previous is None or identity_at > previous[2]:
                    quota_candidates[identity] = (observed_at, value, identity_at)
            elif observed_at < current_time and identity_at < current_time:
                valid.append((observed_at, value))
        if quota_scope:
            valid.extend(
                (observed_at, value)
                for observed_at, value, identity_at in quota_candidates.values()
                if observed_at < current_time and identity_at < current_time
            )
    except Exception:
        return None
    valid.sort(key=lambda item: item[0])
    distinct = {item[0].astimezone(timezone.utc) for item in valid}
    if (
        len(valid) < HISTORY_MIN_VALID_SAMPLES
        or len(distinct) < HISTORY_MIN_DISTINCT_OBSERVATIONS
    ):
        return None
    values = [value for _, value in valid]
    baseline = float(median(values))
    tolerance = max(
        HISTORY_FLAT_ABSOLUTE_TOLERANCE,
        abs(baseline) * HISTORY_FLAT_RELATIVE_TOLERANCE,
    )
    if current > baseline + tolerance:
        direction = "up"
    elif current < baseline - tolerance:
        direction = "down"
    else:
        direction = "flat"
    return AdvisorHistoryEvidence(
        metric=metric,
        direction=direction,
        current_value=current,
        baseline_value=baseline,
        minimum_value=min(values),
        maximum_value=max(values),
        sample_count=len(valid),
        distinct_observation_count=len(distinct),
        range_started_at=valid[0][0],
        range_ended_at=valid[-1][0],
        source="global_quota_history" if quota_scope else "token_monitor_history",
    )


def _history_for_recommendation(
    recommendation: Recommendation, data: AdvisorInput,
) -> AdvisorHistoryEvidence | None:
    if recommendation.code in {"data_unavailable", "data_stale"}:
        return None
    metric: str | None = None
    current: int | float | None = None
    current_observed_at: datetime | None = None
    samples = data.history_samples
    thread_safe_id = data.thread_safe_id
    if recommendation.code == "quota_risk":
        quota_values = (
            (
                "five_hour_remaining_percent",
                data.five_hour_remaining_percent,
                data.five_hour_observed_at,
            ),
            (
                "weekly_remaining_percent",
                data.weekly_remaining_percent,
                data.weekly_observed_at,
            ),
        )
        available = [item for item in quota_values if item[1] is not None]
        if available:
            metric, current, current_observed_at = min(
                available, key=lambda item: float(item[1])
            )
            samples = data.quota_history_samples
            thread_safe_id = None
    elif recommendation.code == "new_thread":
        metric, current = "turn_count", data.turn_count
    elif recommendation.code == "optimize_cache_reuse":
        metric = "cache_hit_percent_derived"
        current = _cache_hit(data.instruction_input_tokens, data.cached_input_tokens)
    elif recommendation.code == "normal":
        metric, current = "instruction_total_tokens", data.instruction_total_tokens
    if metric is None:
        return None
    if metric not in _QUOTA_HISTORY_METRICS:
        if (
            not data.data_available
            or (
                data.data_age_seconds is not None
                and data.data_age_seconds > DATA_STALE_AFTER.total_seconds()
            )
        ):
            return None
        current_observed_at = data.source_observed_at
    return build_history_evidence(
        samples,
        thread_safe_id=thread_safe_id,
        metric=metric,
        current_value=current,
        current_observed_at=current_observed_at,
    )


def _recommendation_observed_at(
    recommendation: Recommendation, data: AdvisorInput,
) -> datetime:
    if recommendation.code == "quota_risk":
        candidates = (
            (data.five_hour_remaining_percent, data.five_hour_observed_at),
            (data.weekly_remaining_percent, data.weekly_observed_at),
        )
        available = [
            (float(value), observed_at)
            for value, observed_at in candidates
            if value is not None and observed_at is not None
        ]
        if available:
            return min(available, key=lambda item: item[0])[1]
    return data.source_observed_at or data.observed_at


def _sample_thread_id(sample: object) -> str | None:
    for name in ("thread_safe_id", "thread_id"):
        value = getattr(sample, name, None)
        if isinstance(value, str) and value:
            return value
    return None


def _sample_is_global_quota(sample: object) -> bool:
    return getattr(sample, "source_type", None) == "global_quota"


def _sample_is_usable(sample: object, metric: str) -> bool:
    if getattr(sample, "legacy_unknown_time", False):
        return False
    if metric == "five_hour_remaining_percent":
        available_names = ("five_hour_available", "available", "is_available")
        stale_names = ("five_hour_stale", "stale", "is_stale")
    elif metric == "weekly_remaining_percent":
        available_names = ("weekly_available", "available", "is_available")
        stale_names = ("weekly_stale", "stale", "is_stale")
    else:
        available_names = ("source_available", "available", "is_available")
        stale_names = ("token_stale", "stale", "is_stale")
    if not any(getattr(sample, name, None) is True for name in available_names):
        return False
    for name in stale_names:
        if getattr(sample, name, False) is True:
            return False
    stale_status = getattr(sample, "stale_status", None)
    if isinstance(stale_status, str) and stale_status.casefold() in {
        "stale", "data_stale", "unavailable", "invalid",
    }:
        return False
    status_name = (
        "quota_source_status"
        if metric in {"five_hour_remaining_percent", "weekly_remaining_percent"}
        else "source_status"
    )
    source_status = getattr(sample, status_name, None)
    if isinstance(source_status, str) and source_status.casefold() in {
        "unavailable", "invalid", "error", "failed",
    }:
        return False
    return True


def _sample_observed_at(sample: object, metric: str) -> datetime | None:
    if metric == "five_hour_remaining_percent":
        names = ("five_hour_observed_at",)
    elif metric == "weekly_remaining_percent":
        names = ("weekly_observed_at",)
    else:
        names = ("source_observed_at",)
    for name in names:
        parsed = _history_datetime(getattr(sample, name, None))
        if parsed is not None:
            return parsed
    return None


def _sample_identity_at(sample: object, metric: str) -> datetime | None:
    if metric == "five_hour_remaining_percent":
        return _history_datetime(getattr(sample, "five_hour_last_seen_at", None))
    if metric == "weekly_remaining_percent":
        return _history_datetime(getattr(sample, "weekly_last_seen_at", None))
    return _sample_observed_at(sample, metric)


def _quota_metric_identity(
    sample: object,
    metric: str,
    observed_at: datetime,
    value: float,
) -> tuple[object, ...]:
    prefix = "five_hour" if metric == "five_hour_remaining_percent" else "weekly"
    return (
        prefix,
        observed_at,
        value,
        _finite_number(getattr(sample, f"{prefix}_used_percent", None)),
        _history_datetime(getattr(sample, f"{prefix}_reset_at", None)),
        getattr(sample, f"{prefix}_source", None),
        getattr(sample, f"{prefix}_available", None),
        getattr(sample, f"{prefix}_stale", None),
        getattr(sample, f"{prefix}_error_code", None),
    )


def _history_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    return None


def _sample_metric(sample: object, metric: str) -> float | None:
    for name in _HISTORY_FIELDS[metric]:
        value = _finite_number(getattr(sample, name, None))
        if value is not None:
            return value
    if metric == "cache_hit_percent_derived":
        input_tokens = _finite_number(getattr(sample, "input_tokens", None))
        cached_tokens = _finite_number(getattr(sample, "cached_input_tokens", None))
        if cached_tokens is None:
            cached_tokens = _finite_number(getattr(sample, "cached_tokens", None))
        if input_tokens is not None and input_tokens > 0 and cached_tokens is not None:
            return cached_tokens / input_tokens * 100.0
    return None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if isfinite(number) else None
