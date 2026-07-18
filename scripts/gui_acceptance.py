"""Launch deterministic safe-number scenarios in the real Dashboard UI.

The launcher requires an isolated directory below the system Temp directory.
Every scenario uses the production history store, projection, trend DTOs, and
Advisor rules, but never reads or stores prompt, response, title, path, or
other content fields.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import traceback
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import customtkinter as ctk
from PIL import ImageGrab


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.advisor import AdvisorInput, AdvisorResult, evaluate_advice  # noqa: E402
from app.analytics_ui import TrendView, trend_view_from_query  # noqa: E402
from app.codex_rollout import (  # noqa: E402
    CodexSessionUsage, InstructionUsage, RolloutSessionsResult,
    RolloutUsageResult, TokenUsage, make_response_safe_id,
    make_thread_safe_id,
)
from app.dashboard import DashboardSnapshot  # noqa: E402
from app.history import (  # noqa: E402
    HistoryObservation,
    HistoryQueryResult,
    UsageHistoryStore,
)
from app.i18n import translate  # noqa: E402
from app.metrics import SessionSummary  # noqa: E402
from app.paths import DATA_DIR_ENV, ui_settings_path  # noqa: E402
from app.quota import CodexQuotaSnapshot, QuotaKind, QuotaWindow  # noqa: E402
from app.ui_settings import save_language  # noqa: E402
from app.usage_summary import (  # noqa: E402
    ObservedUsageSummary,
    UsageWindowKind,
    unavailable_usage_summary,
)
from app.widget_presentation import present_widget  # noqa: E402
from app.ui_presenter import (  # noqa: E402
    HistoryEmptyState, build_history_state_view, build_quota_state_view,
    build_ui_scope_contract, classify_quota_availability, present_dashboard,
)


GEOMETRIES = ("980x660", "1440x900")
SCALES = (1.0, 1.25, 1.5)
PAGES = ("overview", "session_detail", "usage_trends", "recommendations")
RANGES = (7, 30, 90)
SCENARIOS = (
    "token_quota_independence",
    "quota_heartbeat",
    "quota_round_trip",
    "advisor_quota_sufficient",
    "advisor_quota_insufficient",
    "mini_dashboard_dedup",
    "observed_usage_complete",
    "observed_usage_partial",
    "observed_usage_resolved",
    "observed_usage_in_progress",
    "observed_usage_empty",
    "observed_usage_unavailable",
    "semantic_current_selected_same",
    "semantic_current_selected_history",
    "semantic_selected_one_saved",
)
QA_THREAD_ID = "qa-thread-001"
QA_PRIVATE_THREAD_NAMES = (
    "用户Prompt：分析内部财务数据和客户名单",
    "User prompt: analyze confidential customer records",
)
QA_PRIVATE_FRAGMENTS = (
    "内部财务数据", "客户名单", "confidential customer records",
)
QA_INSIGHT_THREAD_IDS = (
    "sha256:" + "1" * 56 + "aaabcdef",
    "sha256:" + "2" * 56 + "bbabcdef",
    "sha256:" + "3" * 64,
    "sha256:" + "4" * 64,
    "sha256:" + "5" * 64,
)


@dataclass(frozen=True)
class ScenarioResult:
    """Deterministic production-DTO output ready for real Dashboard rendering."""

    name: str
    store: UsageHistoryStore
    before: HistoryQueryResult
    after: HistoryQueryResult
    trend_view: TrendView
    advisor_result: AdvisorResult
    trend_group: str
    trend_metric: str
    default_page: str
    record_results: tuple[bool, ...]
    current_observed_at: datetime
    usage_summary: ObservedUsageSummary
    selected_history_view: TrendView
    global_history_view: TrendView
    selected_session: object | None = None
    current_session: object | None = None
    recent_sessions: tuple[object, ...] = ()


class _SafeQaQuotaProvider:
    """Deterministic provider that prevents GUI QA from contacting Codex."""

    def __init__(self, observed_at: datetime) -> None:
        self.snapshot = CodexQuotaSnapshot(
            QuotaWindow.from_reset_duration(
                QuotaKind.FIVE_HOUR, used_percent=40.0,
                remaining_percent=60.0, reset_after=timedelta(hours=4),
                observed_at=observed_at, source="qa_safe_numbers",
            ),
            QuotaWindow.from_reset_duration(
                QuotaKind.WEEKLY, used_percent=35.0,
                remaining_percent=65.0, reset_after=timedelta(days=5),
                observed_at=observed_at, source="qa_safe_numbers",
            ),
            observed_at,
            "normal",
        )

    def refresh(self) -> CodexQuotaSnapshot:
        return self.snapshot

    @staticmethod
    def refresh_thread_titles() -> dict[str, str]:
        return {}

    @staticmethod
    def close() -> None:
        return None


def _validated_temp_root(root: Path) -> Path:
    resolved = Path(root).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if resolved == temp_root or temp_root not in resolved.parents:
        raise RuntimeError("GUI acceptance data directory must be under the system temp directory")
    return resolved


def _isolated_data_root() -> Path:
    raw = os.environ.get(DATA_DIR_ENV)
    if not raw:
        raise RuntimeError(f"{DATA_DIR_ENV} is required for GUI acceptance")
    return _validated_temp_root(Path(raw))


def _validated_screenshot_path(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root not in resolved.parents or resolved.suffix.lower() != ".png":
        raise RuntimeError("GUI acceptance screenshot must be a PNG below system Temp")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _capture_window(root: ctk.CTk, target: Path) -> None:
    root.attributes("-topmost", True)
    root.lift()
    root.focus_force()
    root.event_generate("<Escape>")
    root.event_generate("<Escape>")
    root.update_idletasks()
    left = root.winfo_rootx()
    top = root.winfo_rooty()
    right = left + root.winfo_width()
    bottom = top + root.winfo_height()
    if right <= left or bottom <= top:
        raise RuntimeError("GUI acceptance window has invalid screenshot bounds")
    image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    image.save(target, format="PNG")
    root.attributes("-topmost", False)


def _geometry_for_scale(geometry: str, scale: float) -> str:
    """Keep the requested logical viewport stable while CTk scales the window."""
    width_text, height_text = geometry.split("x", maxsplit=1)
    return f"{round(int(width_text) / scale)}x{round(int(height_text) / scale)}"


def _token_observation(
    *,
    sampled_at: datetime,
    source_observed_at: datetime,
    source_type: str = "dashboard",
    source_status: str = "exact",
    thread_safe_id: str = QA_THREAD_ID,
    response_safe_id: str | None = None,
    stale: bool = False,
    input_tokens: int | None = 1_200,
    output_tokens: int | None = 300,
    total_tokens: int | None = 1_500,
    cached_tokens: int | None = 600,
    reasoning_tokens: int | None = 100,
    session_total_tokens: int | None = 8_000,
    turn_count: int | None = 12,
    five_hour_remaining: float | None = None,
    five_hour_observed_at: datetime | None = None,
    five_hour_last_seen_at: datetime | None = None,
) -> HistoryObservation:
    quota_available = five_hour_remaining is not None
    return HistoryObservation(
        sampled_at=sampled_at,
        source_observed_at=source_observed_at,
        quota_observed_at=five_hour_last_seen_at,
        thread_safe_id=thread_safe_id,
        response_safe_id=make_response_safe_id(
            thread_safe_id,
            response_safe_id or (
                f"qa-response-{int(source_observed_at.timestamp() * 1_000_000)}"
            ),
        ),
        model_safe_id="qa-model-001",
        source_type=source_type,
        source_status=source_status,
        source_available=True,
        token_stale=stale,
        token_stale_reason="source_stale" if stale else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        session_total_tokens=session_total_tokens,
        turn_count=turn_count,
        quota_source_status="normal" if quota_available else "unavailable",
        five_hour_observed_at=five_hour_observed_at,
        five_hour_last_seen_at=five_hour_last_seen_at,
        five_hour_used_percent=(
            None if five_hour_remaining is None else 100.0 - five_hour_remaining
        ),
        five_hour_remaining_percent=five_hour_remaining,
        five_hour_reset_at=(
            None if five_hour_remaining is None else sampled_at + timedelta(hours=5)
        ),
        five_hour_source="codex_app_server" if quota_available else "unknown",
        five_hour_available=quota_available,
        five_hour_stale=False,
    )


def _semantic_session(
    thread_id: str,
    label: str,
    observed_at: datetime,
    *,
    status: str,
    total_tokens: int,
    session_total_tokens: int,
    turn_count: int,
) -> CodexSessionUsage:
    input_tokens = total_tokens * 3 // 5
    output_tokens = total_tokens - input_tokens
    usage = TokenUsage(
        input_tokens, input_tokens // 2, output_tokens,
        output_tokens // 3, total_tokens,
    )
    cumulative_input = session_total_tokens * 3 // 5
    cumulative_output = session_total_tokens - cumulative_input
    cumulative = TokenUsage(
        cumulative_input, cumulative_input // 2, cumulative_output,
        cumulative_output // 3, session_total_tokens,
    )
    instruction = InstructionUsage(
        f"qa-turn-{thread_id}", status, usage, 1, None, 0, 0, 0,
        status == "exact", status == "in_progress",
    )
    return CodexSessionUsage(
        thread_id, label, "qa_safe_label", f"qa-{thread_id}.jsonl",
        instruction, cumulative, observed_at, observed_at, status,
        turn_count=turn_count, full_title=label,
    )


def _quota_observation(
    *,
    observed_at: datetime,
    remaining_percent: float,
    reset_at: datetime,
    last_seen_at: datetime | None = None,
    weekly_remaining_percent: float | None = None,
    weekly_reset_at: datetime | None = None,
) -> HistoryObservation:
    last_seen = last_seen_at or observed_at
    return HistoryObservation(
        sampled_at=last_seen,
        quota_observed_at=last_seen,
        source_type="dashboard",
        source_status="unavailable",
        source_available=False,
        quota_source_status="normal",
        five_hour_observed_at=observed_at,
        five_hour_last_seen_at=last_seen,
        five_hour_used_percent=100.0 - remaining_percent,
        five_hour_remaining_percent=remaining_percent,
        five_hour_reset_at=reset_at,
        five_hour_source="codex_app_server",
        five_hour_available=True,
        five_hour_stale=False,
        weekly_observed_at=(observed_at if weekly_remaining_percent is not None else None),
        weekly_last_seen_at=(last_seen if weekly_remaining_percent is not None else None),
        weekly_used_percent=(
            None if weekly_remaining_percent is None
            else 100.0 - weekly_remaining_percent
        ),
        weekly_remaining_percent=weekly_remaining_percent,
        weekly_reset_at=weekly_reset_at,
        weekly_source=(
            "codex_app_server" if weekly_remaining_percent is not None else "unknown"
        ),
        weekly_available=weekly_remaining_percent is not None,
        weekly_stale=False,
    )


def _advisor_result(
    query: HistoryQueryResult,
    *,
    now: datetime,
    five_hour_remaining: float,
) -> AdvisorResult:
    return evaluate_advice(AdvisorInput(
        data_available=True,
        data_age_seconds=0,
        source_status="normal",
        five_hour_remaining_percent=five_hour_remaining,
        weekly_remaining_percent=60.0,
        turn_count=12,
        instruction_input_tokens=1_200,
        instruction_total_tokens=1_500,
        cached_input_tokens=600,
        session_total_tokens=8_000,
        session_status="exact",
        observed_at=now,
        thread_safe_id=QA_THREAD_ID,
        history_samples=query.samples,
        source_observed_at=now,
        five_hour_observed_at=now,
        weekly_observed_at=now,
        quota_history_samples=query.quota_samples,
    ))


def build_scenario(
    name: str,
    data_root: Path,
    *,
    range_days: int = 7,
    now: datetime | None = None,
) -> ScenarioResult:
    """Build one deterministic scenario in an app-owned DB below system Temp."""

    if name not in SCENARIOS:
        raise ValueError("unsupported_gui_acceptance_scenario")
    if range_days not in RANGES:
        raise ValueError("unsupported_gui_acceptance_range")
    current = now or datetime.now(timezone.utc).replace(microsecond=0)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("gui_acceptance_now_timezone_required")
    current = current.astimezone(timezone.utc)
    root = _validated_temp_root(data_root)
    scenarios_root = root / "qa-scenarios"
    scenarios_root.mkdir(parents=True, exist_ok=True)
    scenario_dir = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=scenarios_root))
    store = UsageHistoryStore(
        scenario_dir / "usage-history.sqlite3",
        clock=lambda: current,
    )
    if not store.initialize():
        raise RuntimeError(store.last_error or "gui_acceptance_history_initialize_failed")
    selected_session: object | None = None
    current_session: object | None = None
    recent_sessions: tuple[object, ...] = ()

    if name == "token_quota_independence":
        token_at = current - timedelta(minutes=10)
        reset_at = current + timedelta(hours=4)
        first = _token_observation(
            sampled_at=current - timedelta(minutes=2),
            source_observed_at=token_at,
            stale=True,
            five_hour_remaining=40.0,
            five_hour_observed_at=current - timedelta(minutes=2),
            five_hour_last_seen_at=current - timedelta(minutes=2),
        )
        first = replace(first, five_hour_reset_at=reset_at)
        inserted_first = store.record(first)
        before = store.query(range_days, QA_THREAD_ID, now=current)
        second = replace(
            first,
            sampled_at=current - timedelta(minutes=1),
            quota_observed_at=current - timedelta(minutes=1),
            five_hour_observed_at=current - timedelta(minutes=1),
            five_hour_last_seen_at=current - timedelta(minutes=1),
            five_hour_used_percent=65.0,
            five_hour_remaining_percent=35.0,
        )
        inserted_second = store.record(second)
        after = store.query(range_days, QA_THREAD_ID, now=current)
        advisor = _advisor_result(after, now=current, five_hour_remaining=35.0)
        group, metric, page = "tokens", "total", "usage_trends"
        outcomes = (inserted_first, inserted_second)

    elif name == "quota_heartbeat":
        token_at = current - timedelta(minutes=10)
        quota_value_at = current - timedelta(minutes=8)
        reset_at = current + timedelta(hours=4)
        first = _token_observation(
            sampled_at=quota_value_at,
            source_observed_at=token_at,
            stale=True,
            five_hour_remaining=40.0,
            five_hour_observed_at=quota_value_at,
            five_hour_last_seen_at=quota_value_at,
        )
        first = replace(first, five_hour_reset_at=reset_at)
        inserted_first = store.record(first)
        before = store.query(range_days, QA_THREAD_ID, now=current)
        heartbeat = replace(
            first,
            sampled_at=current,
            quota_observed_at=current,
            five_hour_observed_at=current,
            five_hour_last_seen_at=current,
        )
        inserted_heartbeat = store.record(heartbeat)
        after = store.query(range_days, QA_THREAD_ID, now=current)
        advisor = _advisor_result(after, now=current, five_hour_remaining=40.0)
        group, metric, page = "quota", "five_hour", "usage_trends"
        outcomes = (inserted_first, inserted_heartbeat)

    elif name == "quota_round_trip":
        reset_at = current + timedelta(hours=4)
        weekly_reset_at = current + timedelta(days=5)
        observations = tuple(
            _quota_observation(
                observed_at=current - timedelta(minutes=2 - index),
                remaining_percent=remaining,
                reset_at=reset_at,
                weekly_remaining_percent=60.0,
                weekly_reset_at=weekly_reset_at,
            )
            for index, remaining in enumerate((80.0, 70.0, 80.0))
        )
        inserted_first = store.record(observations[0])
        inserted_second = store.record(observations[1])
        before = store.query(range_days, QA_THREAD_ID, now=current)
        inserted_third = store.record(observations[2])
        after = store.query(range_days, QA_THREAD_ID, now=current)
        advisor = _advisor_result(after, now=current, five_hour_remaining=80.0)
        group, metric, page = "quota", "five_hour", "usage_trends"
        outcomes = (inserted_first, inserted_second, inserted_third)

    elif name in {"advisor_quota_sufficient", "advisor_quota_insufficient"}:
        prior_count = 5 if name == "advisor_quota_sufficient" else 4
        reset_at = current + timedelta(hours=4)
        outcomes_list: list[bool] = []
        for index in range(prior_count):
            observed = current - timedelta(minutes=prior_count - index)
            outcomes_list.append(store.record(_quota_observation(
                observed_at=observed,
                remaining_percent=40.0 - index * 5.0,
                reset_at=reset_at,
            )))
        before = store.query(range_days, QA_THREAD_ID, now=current)
        outcomes_list.append(store.record(_quota_observation(
            observed_at=current,
            remaining_percent=10.0,
            reset_at=reset_at,
        )))
        after = store.query(range_days, QA_THREAD_ID, now=current)
        advisor = _advisor_result(after, now=current, five_hour_remaining=10.0)
        group, metric, page = "quota", "five_hour", "recommendations"
        outcomes = tuple(outcomes_list)

    elif name == "mini_dashboard_dedup":
        observed = current - timedelta(minutes=1)
        mini = _token_observation(
            sampled_at=current - timedelta(seconds=30),
            source_observed_at=observed,
            source_type="mini",
            input_tokens=None,
            output_tokens=None,
            cached_tokens=None,
            reasoning_tokens=None,
            total_tokens=1_500,
            session_total_tokens=8_000,
            turn_count=12,
        )
        inserted_mini = store.record(mini)
        before = store.query(range_days, QA_THREAD_ID, now=current)
        dashboard = _token_observation(
            sampled_at=current,
            source_observed_at=observed,
            source_type="dashboard",
        )
        inserted_dashboard = store.record(dashboard)
        after = store.query(range_days, QA_THREAD_ID, now=current)
        advisor = _advisor_result(after, now=current, five_hour_remaining=60.0)
        group, metric, page = "tokens", "total", "usage_trends"
        outcomes = (inserted_mini, inserted_dashboard)

    elif name == "observed_usage_complete":
        retained_before_window = store.record(_token_observation(
            sampled_at=current - timedelta(hours=6),
            source_observed_at=current - timedelta(hours=6),
            thread_safe_id=QA_THREAD_ID,
            response_safe_id="qa-before-window",
            input_tokens=80,
            output_tokens=20,
            total_tokens=100,
            cached_tokens=40,
            reasoning_tokens=10,
        ))
        outcomes = (retained_before_window,) + tuple(store.record(_token_observation(
            sampled_at=observed,
            source_observed_at=observed,
            thread_safe_id=QA_INSIGHT_THREAD_IDS[index],
            response_safe_id=f"qa-insight-response-{index}",
            input_tokens=1_000 + index * 100,
            output_tokens=200 + index * 50,
            total_tokens=1_200 + index * 150,
            cached_tokens=100 + index * 100,
            reasoning_tokens=80 + index * 10,
        )) for index, observed in enumerate(
            (current - timedelta(minutes=5 + index) for index in range(5))
        ))
        before = after = store.query(range_days, QA_THREAD_ID, now=current)
        advisor = _advisor_result(after, now=current, five_hour_remaining=60.0)
        group, metric, page = "tokens", "total", "usage_trends"

    elif name == "observed_usage_partial":
        partial = _token_observation(
            sampled_at=current - timedelta(minutes=1),
            source_observed_at=current - timedelta(minutes=1),
            input_tokens=None,
            output_tokens=None,
            total_tokens=1_500,
            cached_tokens=None,
            reasoning_tokens=None,
        )
        outcomes = (store.record(partial),)
        before = after = store.query(range_days, QA_THREAD_ID, now=current)
        advisor = _advisor_result(after, now=current, five_hour_remaining=60.0)
        group, metric, page = "tokens", "total", "usage_trends"

    elif name in {"observed_usage_resolved", "observed_usage_in_progress"}:
        response = "qa-response-in-progress"
        lifecycle_rows = (
            (current - timedelta(seconds=3), "in_progress", 100),
            (current - timedelta(seconds=2), "in_progress", 200),
        ) + (() if name == "observed_usage_in_progress" else (
            (current - timedelta(seconds=1), "exact", 300),
        ))
        outcomes = tuple(store.record(_token_observation(
            sampled_at=observed,
            source_observed_at=observed,
            source_status=status,
            response_safe_id=response,
            input_tokens=total * 3 // 5,
            output_tokens=total * 2 // 5,
            total_tokens=total,
            cached_tokens=total * 3 // 10,
            reasoning_tokens=total // 5,
        )) for observed, status, total in lifecycle_rows)
        before = after = store.query(range_days, QA_THREAD_ID, now=current)
        advisor = _advisor_result(after, now=current, five_hour_remaining=60.0)
        group, metric, page = "tokens", "total", "usage_trends"
        resolved = name == "observed_usage_resolved"
        instruction = InstructionUsage(
            response,
            "exact" if resolved else "in_progress",
            TokenUsage(180, 90, 120, 60, 300) if resolved
            else TokenUsage(120, 60, 80, 40, 200),
            3 if resolved else 2, None, 0, 0, 0, resolved, not resolved,
        )
        selected_session = SimpleNamespace(
            thread_id=QA_THREAD_ID,
            instruction=instruction,
            thread_cumulative_usage=TokenUsage(600, 240, 200, 40, 800),
            status="exact" if resolved else "in_progress",
            observed_at=current - timedelta(seconds=1),
            display_title="Codex Session · 07-16 12:00",
            full_title="Codex Session · 07-16 12:00",
            turn_count=8,
        )

    elif name in {
        "semantic_current_selected_same",
        "semantic_current_selected_history",
        "semantic_selected_one_saved",
    }:
        current_session = _semantic_session(
            "qa-current-A", QA_PRIVATE_THREAD_NAMES[0], current,
            status="in_progress", total_tokens=1_200,
            session_total_tokens=9_000, turn_count=12,
        )
        current_session = replace(
            current_session,
            title_source="codex_app_server.thread_display_title",
            full_title=QA_PRIVATE_THREAD_NAMES[0],
        )
        history_session = _semantic_session(
            "qa-history-B", QA_PRIVATE_THREAD_NAMES[1],
            current - timedelta(minutes=8),
            status="exact", total_tokens=2_400,
            session_total_tokens=18_000, turn_count=18,
        )
        history_session = replace(
            history_session,
            title_source="codex_app_server.thread_display_title",
            full_title=QA_PRIVATE_THREAD_NAMES[1],
        )
        recent_sessions = (current_session, history_session)
        selected_session = (
            current_session
            if name == "semantic_current_selected_same" else history_session
        )
        completed_count = (
            0 if name == "semantic_current_selected_same"
            else 1 if name == "semantic_selected_one_saved" else 2
        )
        outcomes = tuple(store.record(_token_observation(
            sampled_at=(observed := current - timedelta(
                minutes=completed_count - index,
            )),
            source_observed_at=observed,
            thread_safe_id=make_thread_safe_id(history_session.thread_id),
            response_safe_id=make_response_safe_id(
                history_session.thread_id, f"qa-history-response-{index}",
            ),
            input_tokens=1_200 + index * 300,
            output_tokens=400 + index * 100,
            total_tokens=1_600 + index * 400,
            cached_tokens=600 + index * 150,
            reasoning_tokens=120 + index * 30,
            session_total_tokens=18_000,
            turn_count=18,
            five_hour_remaining=60.0 - index,
            five_hour_observed_at=observed,
            five_hour_last_seen_at=observed,
        )) for index in range(completed_count))
        before = after = store.query(
            range_days, selected_session.thread_id, now=current,
        )
        advisor = _advisor_result(after, now=current, five_hour_remaining=60.0)
        group, metric, page = "tokens", "total", "overview"

    else:  # observed_usage_empty / observed_usage_unavailable
        before = after = store.query(range_days, QA_THREAD_ID, now=current)
        advisor = _advisor_result(after, now=current, five_hour_remaining=60.0)
        group, metric, page = "tokens", "total", "usage_trends"
        outcomes = ()

    usage_summary = store.summarize_usage(
        UsageWindowKind.ROLLING_5H,
        as_of_utc=current,
        local_timezone=timezone.utc,
    )
    if name == "observed_usage_unavailable":
        usage_summary = unavailable_usage_summary(
            UsageWindowKind.ROLLING_5H,
            as_of_utc=current,
            local_timezone=timezone.utc,
            error_code="qa_history_unavailable",
        )

    selected_history_filter = getattr(
        selected_session, "thread_id", QA_THREAD_ID,
    )
    selected_history_view = trend_view_from_query(store.query(
        90, selected_history_filter, now=current,
    ))
    global_history_view = trend_view_from_query(store.query(
        90, None, now=current,
    ))

    return ScenarioResult(
        name=name,
        store=store,
        before=before,
        after=after,
        trend_view=trend_view_from_query(after),
        advisor_result=advisor,
        trend_group=group,
        trend_metric=metric,
        default_page=page,
        record_results=outcomes,
        current_observed_at=current,
        usage_summary=usage_summary,
        selected_history_view=selected_history_view,
        global_history_view=global_history_view,
        selected_session=selected_session,
        current_session=current_session,
        recent_sessions=recent_sessions,
    )


def _apply_scenario(dashboard: object, scenario: ScenarioResult, page: str) -> None:
    """Apply production DTOs to already-built real Dashboard widgets."""

    dashboard.trend_range_days = scenario.after.range_days
    dashboard.trend_view = scenario.trend_view
    dashboard.selected_history_view = scenario.selected_history_view
    dashboard.global_history_view = scenario.global_history_view
    dashboard.history_error = scenario.after.error_code
    dashboard.trend_group = scenario.trend_group
    dashboard.trend_metric = scenario.trend_metric
    dashboard.usage_window_kind = UsageWindowKind.ROLLING_5H
    dashboard.observed_usage_summary = scenario.usage_summary
    group_label = next(
        label
        for label, value in dashboard.trend_group_labels.items()
        if value == scenario.trend_group
    )
    dashboard.trend_group_menu.set(group_label)
    dashboard._configure_trend_metric_menu()  # noqa: SLF001 - QA launcher
    dashboard.trend_range_menu.set(
        translate(f"last_{scenario.after.range_days}_days", dashboard.language)
    )
    dashboard.advisor_result = scenario.advisor_result
    if hasattr(dashboard, "observed_usage_window_menu"):
        dashboard.observed_usage_window_menu.set(
            translate("observed_usage_rolling_5h", dashboard.language)
        )
    dashboard._render_observed_usage()  # noqa: SLF001 - QA launcher
    if hasattr(dashboard, "_render_usage_insights"):
        dashboard._render_usage_insights()  # noqa: SLF001 - QA launcher
    dashboard._render_trends()  # noqa: SLF001 - QA launcher
    dashboard._render_advisor()  # noqa: SLF001 - QA launcher
    dashboard._render_recommendations()  # noqa: SLF001 - QA launcher
    if hasattr(dashboard, "status_recent_rows"):
        five = QuotaWindow.from_reset_duration(
            QuotaKind.FIVE_HOUR,
            used_percent=40.0,
            remaining_percent=60.0,
            reset_after=timedelta(hours=4),
            observed_at=scenario.current_observed_at,
            source="qa_safe_numbers",
        )
        weekly = QuotaWindow.from_reset_duration(
            QuotaKind.WEEKLY,
            used_percent=35.0,
            remaining_percent=65.0,
            reset_after=timedelta(days=5),
            observed_at=scenario.current_observed_at,
            source="qa_safe_numbers",
        )
        if scenario.current_session is not None and scenario.selected_session is not None:
            selected = scenario.selected_session
            current_session = scenario.current_session
            recent_sessions = tuple(scenario.recent_sessions)
            sessions_result = RolloutSessionsResult(
                recent_sessions, current_session.thread_id,
                sum(item.status == "in_progress" for item in recent_sessions),
                scenario.current_observed_at,
            )
            rollout = RolloutUsageResult(
                selected.rollout_filename, selected.thread_id,
                selected.instruction, True, selected.thread_cumulative_usage,
                selected.observed_at, scenario.current_observed_at,
                selected.turn_count,
            )
            selection_mode = (
                "auto" if selected.thread_id == current_session.thread_id
                else "pinned"
            )
            dashboard.snapshot = DashboardSnapshot(
                (), SessionSummary(0, 0, 0, 0, 0, 0, 0, 0, 0),
                rollout, None, False,
                sessions_result=sessions_result,
                recent_sessions=recent_sessions,
                selected_session=selected,
                selection_mode=selection_mode,
                selected_thread_id=selected.thread_id,
                current_session=current_session,
                current_thread_id=current_session.thread_id,
            )
            dashboard.view_model._last_snapshot = dashboard.snapshot
            dashboard.view_model.current_thread_id = current_session.thread_id
            dashboard.view_model.selection_mode = selection_mode
            dashboard.view_model.selected_thread_id = selected.thread_id
            dashboard.presentation = present_dashboard(dashboard.snapshot, False)
        else:
            dashboard.snapshot = (
                SimpleNamespace(
                    selected_session=scenario.selected_session,
                    current_session=scenario.selected_session,
                    recent_sessions=(scenario.selected_session,),
                    selected_thread_id=scenario.selected_session.thread_id,
                    current_thread_id=scenario.selected_session.thread_id,
                    sessions_result=SimpleNamespace(
                        refreshed_at=scenario.current_observed_at,
                    ),
                )
                if scenario.selected_session is not None else None
            )
        dashboard.quota_snapshot = CodexQuotaSnapshot(
            five,
            weekly,
            scenario.current_observed_at,
            "normal",
        )
        if scenario.current_session is not None:
            dashboard._apply_presentation(dashboard.presentation)  # noqa: SLF001
        dashboard._render_safe_overview()  # noqa: SLF001 - production QA path
        if scenario.current_session is not None:
            dashboard._render_status_recent(dashboard.presentation)  # noqa: SLF001
            dashboard._render_sessions(dashboard.presentation)  # noqa: SLF001
            dashboard._render_trends()  # noqa: SLF001
    dashboard.show_page(page)


def _assert_e1_state_widgets(
    dashboard: object, scenario: ScenarioResult,
) -> dict[str, bool]:
    """Exercise actionable empty/quota contracts through real widgets."""

    checks: dict[str, bool] = {}
    saved_trend = dashboard.trend_view
    saved_selected_history = dashboard.selected_history_view
    saved_global_history = dashboard.global_history_view
    saved_group = dashboard.trend_group
    saved_metric = dashboard.trend_metric
    saved_backfill = dashboard.history_backfill_status
    saved_quota = dashboard.quota_snapshot

    dashboard.show_page("usage_trends")
    dashboard.trend_group = "tokens"
    dashboard.trend_metric = "total"
    dashboard.history_backfill_status = "idle"
    dashboard.trend_view = TrendView(
        7, "empty", (), scenario.current_observed_at,
        queried_at=scenario.current_observed_at,
    )
    dashboard.selected_history_view = TrendView(
        90, "empty", (), scenario.current_observed_at,
        queried_at=scenario.current_observed_at,
    )
    dashboard.global_history_view = saved_global_history
    dashboard._render_trends()  # noqa: SLF001 - real-widget state contract
    history_view = build_history_state_view(
        HistoryEmptyState.SELECTED_NO_HISTORY,
    )
    history_message = dashboard.trend_quality_message_var.get()
    checks["history_empty_widget_reason_and_actions"] = bool(
        dashboard.trend_quality_var.get()
        == translate(history_view.title_key, dashboard.language)
        and translate(history_view.reason_key, dashboard.language)
        in history_message
        and translate(history_view.realtime_impact_key, dashboard.language)
        in history_message
        and dashboard.trend_empty_action_kind
        == history_view.primary_action.kind
        and dashboard.trend_empty_fallback_kind
        == history_view.fallback_action.kind
        and dashboard.trend_metric_vars["current"].get() == "—"
    )

    available_quota = saved_quota
    unavailable_five = QuotaWindow.unavailable(
        QuotaKind.FIVE_HOUR,
        scenario.current_observed_at,
        "qa_safe_numbers",
    )
    unavailable_weekly = QuotaWindow.unavailable(
        QuotaKind.WEEKLY,
        scenario.current_observed_at,
        "qa_safe_numbers",
    )
    no_live = CodexQuotaSnapshot(
        unavailable_five,
        unavailable_weekly,
        scenario.current_observed_at,
        "unavailable",
    )
    weekly_only = CodexQuotaSnapshot(
        unavailable_five,
        available_quota.weekly,
        scenario.current_observed_at,
        "partial",
    )
    stale_live = CodexQuotaSnapshot(
        available_quota.five_hour.as_stale("qa_acceptance_stale"),
        available_quota.weekly,
        scenario.current_observed_at,
        "stale",
    )
    no_history = TrendView(
        90, "empty", (), scenario.current_observed_at,
        queried_at=scenario.current_observed_at,
    )
    failed_history = TrendView(
        90, "unavailable", (), None,
        error_code="qa_history_unavailable",
        queried_at=scenario.current_observed_at,
    )
    quota_cases = (
        ("live_and_history", available_quota, saved_global_history, True),
        ("live_only", available_quota, no_history, True),
        ("history_only", no_live, saved_global_history, True),
        ("weekly_only", weekly_only, no_history, True),
        ("stale_live", stale_live, no_history, True),
        ("unavailable", no_live, no_history, True),
        ("unavailable", no_live, failed_history, False),
    )
    for index, (expected_state, quota, history, source_available) in enumerate(
        quota_cases,
    ):
        dashboard.quota_snapshot = quota
        dashboard.global_history_view = history
        dashboard._render_safe_overview()  # noqa: SLF001 - real-widget matrix
        contract = classify_quota_availability(
            quota.five_hour.available,
            quota.weekly.available,
            bool(history.quota_samples),
            quota.five_hour.stale or quota.weekly.stale,
            history_source_available=source_available,
        )
        state_view = build_quota_state_view(contract)
        message = dashboard.quota_availability_var.get()
        checks[f"quota_widget_case_{index}_{expected_state}"] = bool(
            contract.state.value == expected_state
            and dashboard.quota_state_title_var.get()
            == translate(state_view.title_key, dashboard.language)
            and translate(state_view.reason_key, dashboard.language) in message
            and translate(state_view.realtime_impact_key, dashboard.language)
            in message
            and dashboard.quota_primary_action_kind
            == state_view.primary_action.kind
            and dashboard.quota_fallback_action_kind
            == (state_view.fallback_action.kind if state_view.fallback_action else "")
        )

    dashboard.trend_view = saved_trend
    dashboard.selected_history_view = saved_selected_history
    dashboard.global_history_view = saved_global_history
    dashboard.trend_group = saved_group
    dashboard.trend_metric = saved_metric
    dashboard.history_backfill_status = saved_backfill
    dashboard.quota_snapshot = saved_quota
    dashboard._render_safe_overview()  # noqa: SLF001 - restore acceptance state
    dashboard._render_trends()  # noqa: SLF001 - restore acceptance state
    return checks


def _assert_e1_dashboard(dashboard: object, scenario: ScenarioResult) -> None:
    """Verify E1 user-visible semantics against the real widget tree."""

    if scenario.name != "semantic_current_selected_history":
        raise RuntimeError("E1 assertions require semantic_current_selected_history")
    dashboard.root.update_idletasks()
    checks: dict[str, bool] = {}
    contract = build_ui_scope_contract(dashboard.snapshot)
    checks["explicit_current_activity"] = (
        contract.activity_scope.value == "current_activity"
        and contract.activity_thread_id == scenario.current_session.thread_id
    )
    checks["pinned_selection_separate"] = (
        contract.selected_is_pinned
        and not contract.selected_is_activity
        and contract.selected_thread_id == scenario.selected_session.thread_id
    )
    checks["advisor_scope_visible"] = translate(
        "ui_scope_current_activity", dashboard.language,
    ) in dashboard.status_meta_var.get()
    semantics = tuple(
        item["semantic"] for item in dashboard.core_metric_widgets
    )
    checks["four_non_quota_metrics"] = (
        len(semantics) == 4
        and "five_hour_quota" not in semantics
        and "weekly_quota" not in semantics
    )
    checks["current_numbers_not_selected"] = (
        dashboard.core_metric_widgets[0]["value"].get()
        != dashboard.simple_task_vars["instruction"].get()
    )
    checks["single_live_quota_region"] = (
        dashboard.quota_snapshot.five_hour.available
        and dashboard.quota_snapshot.weekly.available
        and bool(dashboard.quota_availability_var.get())
    )
    checks["quota_history_action"] = (
        dashboard.quota_detail_button.cget("text")
        == translate("view_local_quota_history", dashboard.language)
    )
    checks["global_summary_scope"] = translate(
        "all_sessions_scope", dashboard.language,
    ) in dashboard.observed_usage_coverage_var.get()
    checks["global_ranking_scope"] = translate(
        "all_sessions_scope", dashboard.language,
    ) in dashboard.usage_insights_range_var.get()
    checks["selected_card_visible"] = dashboard.task_summary_card.winfo_ismapped()
    visible_advice_children = tuple(
        child for child in dashboard.status_advice_card.winfo_children()
        if child.winfo_ismapped()
    )
    advice_height = dashboard.status_advice_card.winfo_height()
    advice_content_bottom = max(
        child.winfo_y() + child.winfo_height()
        for child in visible_advice_children
    )
    advice_blank_tail = advice_height - advice_content_bottom
    checks["compact_advice_card"] = advice_blank_tail <= 48
    visible_core_cards = tuple(
        item["card"] for item in dashboard.core_metric_widgets
        if item["card"].winfo_ismapped()
    )
    core_right = max(
        card.winfo_x() + card.winfo_width() for card in visible_core_cards
    )
    checks["core_metrics_fill_row"] = (
        dashboard.core_cards_frame.winfo_width() - core_right <= 16
    )
    checks["six_primary_navigation_items"] = tuple(
        dashboard.nav_buttons
    ) == (
        "overview", "sessions", "usage_trends",
        "recommendations", "tools", "settings",
    )
    checks["recommendations_page_preserved"] = len(
        dashboard.recommendation_cards,
    ) == 5
    visible_values: list[str] = []
    for variable in (
        dashboard.simple_status_title_var,
        dashboard.simple_reason_var,
        dashboard.status_meta_var,
        dashboard.task_full_title_var,
        dashboard.task_detail_viewing_var,
    ):
        visible_values.append(str(variable.get()))
    visible_values.extend(
        str(variable.get()) for variable in dashboard.simple_task_vars.values()
    )
    visible_values.extend(
        str(variable.get()) for variable in dashboard.task_detail_vars.values()
    )
    for row in dashboard.status_recent_rows:
        visible_values.extend(
            str(row[key].get())
            for key in ("title", "full_title", "detail", "current")
        )
    for item_id in dashboard.sessions_tree.get_children():
        visible_values.extend(
            str(value) for value in dashboard.sessions_tree.item(item_id, "values")
        )
    for section in dashboard.usage_insights_sections.values():
        for row in section["rows"]:
            visible_values.extend((
                str(row["title"].get()), str(row["details"].get()),
            ))
    for card in dashboard.recommendation_cards:
        visible_values.extend(
            str(card[key].get())
            for key in (
                "title", "severity", "body", "evidence", "metadata",
                "history", "observed",
            )
        )
    saved_widget_thread_id = dashboard._widget_thread_id  # noqa: SLF001 - QA path
    dashboard._widget_thread_id = scenario.selected_session.thread_id  # noqa: SLF001
    mini_snapshot = dashboard._safe_mini_snapshot(  # noqa: SLF001 - QA path
        dashboard._cached_mini_snapshot(scenario.selected_session),
    )
    dashboard._widget_thread_id = saved_widget_thread_id  # noqa: SLF001
    mini_presentation = present_widget(
        dashboard.quota_snapshot, mini_snapshot,
        dashboard.advisor_result.primary, dashboard.language,
    )
    tooltip_values = (
        dashboard.task_full_title_var.get(),
        *(row["full_title"].get() for row in dashboard.status_recent_rows),
        mini_presentation.task_title,
    )
    visible_values.extend(str(value) for value in tooltip_values)
    visible_text = "\n".join(visible_values)
    checks["thread_name_privacy_sentinel"] = all(
        fragment not in visible_text for fragment in QA_PRIVATE_FRAGMENTS
    )
    checks["tooltip_privacy_sentinel"] = all(
        fragment not in "\n".join(str(value) for value in tooltip_values)
        for fragment in QA_PRIVATE_FRAGMENTS
    )
    safe_selected_label = dashboard.simple_task_vars["title"].get()
    checks["safe_session_metadata_preserved"] = bool(
        str(scenario.selected_session.turn_count) in safe_selected_label
        and translate("historical_session_role", dashboard.language)
        in safe_selected_label
        and translate("anonymous_session_code", dashboard.language, code="")
        .split(":", 1)[0].split("：", 1)[0]
        in safe_selected_label
    )
    checks.update(_assert_e1_state_widgets(dashboard, scenario))
    ranking_rows = dashboard.usage_insights_sections["threads"]["rows"]
    visible_ranking = next((
        row for row in ranking_rows if row["thread_safe_id"] is not None
    ), None)
    ranking_text = "" if visible_ranking is None else " ".join((
        visible_ranking["title"].get(), visible_ranking["details"].get(),
    ))
    checks["ranking_is_explicit_and_safe"] = bool(
        visible_ranking is not None
        and "#1" not in ranking_text
        and translate("locate_session", dashboard.language)
        == visible_ranking["locate_button"].cget("text")
        and scenario.current_session.thread_id not in ranking_text
        and scenario.selected_session.thread_id not in ranking_text
    )
    if visible_ranking is not None:
        dashboard.show_page("usage_trends")
        visible_ranking["locate_button"].invoke()
        dashboard.root.update_idletasks()
        expected_origin = translate(
            "ranking_session_origin",
            dashboard.language,
            kind=translate("ranking_session_kind", dashboard.language),
            rank=visible_ranking["rank"] or "—",
        )
        checks["ranking_visible_action_locates_session"] = bool(
            dashboard.current_nav_page == "session_detail"
            and dashboard.snapshot.selected_thread_id
            == scenario.selected_session.thread_id
            and dashboard.task_detail_viewing_var.get() == expected_origin
        )
    else:
        checks["ranking_visible_action_locates_session"] = False
    dashboard.show_page("overview")
    dashboard.quota_detail_button.invoke()
    dashboard.root.update_idletasks()
    checks["quota_visible_action_routes_history"] = bool(
        dashboard.current_nav_page == "usage_trends"
        and dashboard.trend_group == "quota"
    )
    dashboard.show_page("recommendations")
    dashboard._execute_ui_action("view_quota")  # noqa: SLF001 - QA action path
    checks["quota_action_routes_overview"] = dashboard.current_nav_page == "overview"
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise RuntimeError(f"E1 GUI acceptance failed: {', '.join(failed)}")
    print(
        "E1_GUI_ACCEPTANCE_OK "
        f"checks={len(checks)} scenario={scenario.name} language={dashboard.language} "
        f"advice_height={advice_height} advice_blank_tail={advice_blank_tail}",
        flush=True,
    )


def _show_trend_tooltip(dashboard: object, point_index: int) -> None:
    """Open one real chart tooltip without taking over the user's mouse."""
    chart = dashboard.trend_chart
    chart.update_idletasks()
    chart._redraw()  # noqa: SLF001 - deterministic QA-only rendering hook
    rendered = chart._rendered_points  # noqa: SLF001 - deterministic QA-only inspection
    if not 0 <= point_index < len(rendered):
        raise RuntimeError(
            f"Tooltip point {point_index} is unavailable; rendered points={len(rendered)}"
        )
    x, y, _point = rendered[point_index]
    chart.event_generate("<Motion>", x=round(x), y=round(y))
    tooltip = chart._tooltip  # noqa: SLF001 - deterministic QA-only inspection
    if tooltip is not None:
        tooltip.lift()
        tooltip.update_idletasks()


def _click_recent_title(dashboard: object, index: int) -> None:
    """Exercise the visible title region of a recent-session card."""
    row = dashboard.status_recent_rows[index]
    thread_id = row["thread_id"]
    title_label = row["title_label"]
    visible_target = getattr(title_label, "_label", title_label)
    visible_target.event_generate("<Button-1>")
    if getattr(dashboard.snapshot, "selected_thread_id", None) != thread_id:
        raise RuntimeError(
            "Recent-session title click did not change the selection: "
            f"expected={thread_id!r}, selected="
            f"{getattr(dashboard.snapshot, 'selected_thread_id', None)!r}, "
            f"cached={getattr(dashboard.view_model, 'selected_thread_id', None)!r}"
        )


def _scroll_trends_to_end(dashboard: object) -> None:
    """Expose the final summary row in the real 980x660 trends page."""
    page = dashboard.trend_chart
    while page is not None and not hasattr(page, "_parent_canvas"):
        page = getattr(page, "master", None)
    if page is None:
        raise RuntimeError("Usage trends scroll container is unavailable")
    page._parent_canvas.yview_moveto(1.0)  # noqa: SLF001 - QA-only scroll hook


def _scroll_trends_to_insights(dashboard: object) -> None:
    """Align the usage-insights card with the top of the visible scroll area."""
    card = dashboard.usage_insights_card
    page = getattr(card, "master", None)
    if page is None or not hasattr(page, "_parent_canvas"):
        raise RuntimeError("Usage insights scroll container is unavailable")
    canvas = page._parent_canvas  # noqa: SLF001 - QA-only scroll hook
    canvas.update_idletasks()
    scroll_region = canvas.bbox("all")
    if scroll_region is None:
        raise RuntimeError("Usage insights scroll region is unavailable")
    total_height = max(1, scroll_region[3] - scroll_region[1])
    viewport_height = max(1, canvas.winfo_height())
    denominator = max(1, total_height - viewport_height)
    current_offset = canvas.canvasy(0)
    scaling_getter = getattr(card, "_get_widget_scaling", None)
    widget_scaling = scaling_getter() if callable(scaling_getter) else 1.0
    relative_card_y = (
        card.winfo_rooty() - canvas.winfo_rooty()
    ) / max(0.1, float(widget_scaling))
    target_offset = current_offset + relative_card_y - 8
    canvas.yview_moveto(min(1.0, max(0.0, target_offset / denominator)))


def _scroll_overview_to_usage(dashboard: object) -> None:
    """Center the observed-usage card for deterministic overview screenshots."""
    page = dashboard.status_page
    if not hasattr(page, "_parent_canvas"):
        raise RuntimeError("Overview scroll container is unavailable")
    page._parent_canvas.yview_moveto(0.36)  # noqa: SLF001 - QA-only scroll hook


def _scroll_overview_to_quota(dashboard: object) -> None:
    """Expose the independent quota card for deterministic evidence."""
    page = dashboard.status_page
    if not hasattr(page, "_parent_canvas"):
        raise RuntimeError("Overview scroll container is unavailable")
    page._parent_canvas.yview_moveto(0.58)  # noqa: SLF001 - QA-only scroll hook


def _change_observed_window(dashboard: object, value: str) -> None:
    """Exercise the real overview range-change callback with a safe QA store."""
    target = UsageWindowKind(value)
    label = next(label for label, kind in dashboard.usage_window_labels.items()
                 if kind == target)
    dashboard.observed_usage_window_menu.set(label)
    dashboard._change_usage_window(label)  # noqa: SLF001 - QA-only interaction hook


def _cycle_observed_windows(dashboard: object) -> None:
    for kind in (
        UsageWindowKind.TODAY,
        UsageWindowKind.ROLLING_5H,
        UsageWindowKind.ROLLING_7D,
        UsageWindowKind.ROLLING_30D,
    ):
        _change_observed_window(dashboard, kind.value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--geometry", choices=GEOMETRIES, required=True)
    parser.add_argument("--scale", choices=SCALES, type=float, required=True)
    parser.add_argument("--language", choices=("zh-CN", "en"), required=True)
    parser.add_argument("--scenario", choices=SCENARIOS, required=True)
    parser.add_argument("--page", choices=PAGES)
    parser.add_argument("--range", choices=RANGES, type=int, default=7)
    parser.add_argument("--metric", choices=("five_hour", "weekly"))
    parser.add_argument("--tooltip-index", choices=(0, 1, 2), type=int)
    parser.add_argument("--scroll-end", action="store_true")
    parser.add_argument("--scroll-insights", action="store_true")
    parser.add_argument("--scroll-overview-usage", action="store_true")
    parser.add_argument("--scroll-overview-quota", action="store_true")
    parser.add_argument(
        "--observed-window",
        choices=tuple(kind.value for kind in UsageWindowKind),
    )
    parser.add_argument("--cycle-observed-windows", action="store_true")
    parser.add_argument("--click-recent-index", choices=range(3), type=int)
    parser.add_argument("--assert-e1", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--auto-close-ms", type=int, default=0)
    args = parser.parse_args()
    data_root = _isolated_data_root()
    data_root.mkdir(parents=True, exist_ok=True)
    empty_sessions = data_root / "empty-codex-sessions"
    empty_sessions.mkdir(parents=True, exist_ok=True)
    os.environ["CODEX_SESSIONS_DIR"] = str(empty_sessions)
    if not save_language(args.language, ui_settings_path()):
        raise RuntimeError("Unable to save isolated GUI acceptance language")
    scenario = build_scenario(args.scenario, data_root, range_days=args.range)
    screenshot_path = (
        _validated_screenshot_path(args.screenshot)
        if args.screenshot is not None else None
    )
    if args.metric is not None:
        scenario = replace(scenario, trend_metric=args.metric)

    ctk.set_widget_scaling(args.scale)
    ctk.set_window_scaling(args.scale)

    from app.main import Dashboard

    root = ctk.CTk()
    callback_errors: list[BaseException] = []

    def report_callback_exception(
        exception_type: type[BaseException],
        exception: BaseException,
        exception_traceback: object,
    ) -> None:
        callback_errors.append(exception)
        traceback.print_exception(
            exception_type, exception, exception_traceback,
        )
        dashboard.close()

    root.report_callback_exception = report_callback_exception
    dashboard = Dashboard(
        root,
        quota_provider=_SafeQaQuotaProvider(scenario.current_observed_at),
        history_store=scenario.store,
    )
    dashboard.auto_refresh.set_enabled(False)
    dashboard.auto_refresh_var.set(False)
    percent = round(args.scale * 100)
    root.title(
        "Codex Token Monitor QA - "
        f"{scenario.name} - {percent}% - {args.geometry} - {args.language}"
    )

    def apply_case() -> None:
        root.minsize(1, 1)
        root.geometry(f"{_geometry_for_scale(args.geometry, args.scale)}+0+0")
        _apply_scenario(dashboard, scenario, args.page or scenario.default_page)

        def stabilize_case() -> None:
            _apply_scenario(dashboard, scenario, args.page or scenario.default_page)
            root.update_idletasks()
            if args.assert_e1:
                _assert_e1_dashboard(dashboard, scenario)
            if args.tooltip_index is not None:
                root.after(250, lambda: _show_trend_tooltip(dashboard, args.tooltip_index))
            if args.scroll_end:
                root.after(250, lambda: _scroll_trends_to_end(dashboard))
            if args.scroll_insights:
                root.after(250, lambda: _scroll_trends_to_insights(dashboard))
            if args.scroll_overview_usage:
                root.after(250, lambda: _scroll_overview_to_usage(dashboard))
            if args.scroll_overview_quota:
                root.after(250, lambda: _scroll_overview_to_quota(dashboard))
            if args.observed_window is not None:
                root.after(
                    250,
                    lambda: _change_observed_window(dashboard, args.observed_window),
                )
            if args.cycle_observed_windows:
                root.after(250, lambda: _cycle_observed_windows(dashboard))
            if args.click_recent_index is not None:
                root.after(
                    250,
                    lambda: _click_recent_title(
                        dashboard, args.click_recent_index,
                    ),
                )
            if screenshot_path is not None:
                delay = (
                    1000
                    if args.cycle_observed_windows or args.scroll_insights
                    else 500
                )
                root.after(delay, lambda: _capture_window(root, screenshot_path))

        root.after(1200, stabilize_case)
    if args.auto_close_ms > 0:
        def close_case() -> None:
            # Prevent the real Dashboard's Unmap handler from entering widget
            # mode while this deterministic QA process is being destroyed.
            dashboard._widget_mode = True  # noqa: SLF001 - QA-only shutdown guard
            dashboard.close()
        root.after(args.auto_close_ms, close_case)

    root.after_idle(apply_case)
    root.mainloop()
    if callback_errors:
        raise RuntimeError("GUI acceptance callback failed") from callback_errors[0]


if __name__ == "__main__":
    main()
