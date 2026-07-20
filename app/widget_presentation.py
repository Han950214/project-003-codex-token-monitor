"""Pure compact/expanded widget presentation from the shared snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.dashboard import MiniThreadSnapshot
from app.i18n import translate
from app.quota import CodexQuotaSnapshot
from app.ui_format import format_localized_token_count


@dataclass(frozen=True)
class WidgetPresentation:
    status: str
    status_text: str
    quota_text: str
    task_title: str
    turn_count_text: str
    instruction_total: str
    session_total: str


def present_widget(
    quota: CodexQuotaSnapshot,
    thread: MiniThreadSnapshot,
    recommendation: object | None,
    language: str,
    *,
    turn_count: int | None = None,
) -> WidgetPresentation:
    window = quota.five_hour
    if window.available and not window.stale:
        quota_text = translate(
            "widget_five_hour_remaining", language,
            value=_format_percent(window.remaining_percent),
        )
    elif window.reset_at is not None:
        quota_text = translate(
            "widget_five_hour_reset", language,
            value=_format_reset_time(window.reset_at, language, window.observed_at),
        )
    else:
        quota_text = translate("widget_five_hour_unknown", language)
    if recommendation is not None and isinstance(getattr(recommendation, "title_key", None), str):
        status = getattr(recommendation, "status", "normal")
        status_text = translate(recommendation.title_key, language)
    elif thread.status in {"no_selection", "unavailable"}:
        status, status_text = "data_unavailable", translate("advisor_data_unavailable_title", language)
    else:
        status, status_text = "normal", translate("advisor_normal_title", language)
    return WidgetPresentation(
        status,
        status_text,
        quota_text,
        thread.full_title or thread.title or translate("no_selected_thread", language),
        str(turn_count if turn_count is not None else thread.turn_count) if (turn_count is not None or thread.turn_count is not None) else "—",
        _format_token_total(thread.instruction_total_tokens, language),
        _format_token_total(thread.session_total_tokens, language),
    )


def _format_percent(value: float | None) -> str:
    if value is None:
        return "—"
    number = min(100.0, max(0.0, float(value)))
    return f"{int(number)}%" if number.is_integer() else f"{number:.1f}%"


def _format_token_total(value: int | None, language: str) -> str:
    return format_localized_token_count(value, language)


def _format_reset_time(
    reset_at: datetime | None,
    language: str,
    observed_at: datetime | None = None,
) -> str:
    if reset_at is None:
        return "—"
    local = reset_at.astimezone()
    now = (observed_at or datetime.now().astimezone()).astimezone(local.tzinfo)
    if local.date() == now.date():
        return f"{translate('today', language)} {local:%H:%M}"
    if local.date() == (now + timedelta(days=1)).date():
        return f"{translate('tomorrow', language)} {local:%H:%M}"
    return local.strftime("%m月%d日 %H:%M") if language == "zh-CN" else local.strftime("%b %d, %H:%M")
