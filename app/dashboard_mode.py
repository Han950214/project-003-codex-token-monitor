"""Query-free application shell state for Dashboard and widget modes."""

from __future__ import annotations

from dataclasses import dataclass, replace


DASHBOARD_MODES = ("simple", "advanced")
WIDGET_MODES = ("compact", "expanded")
NAVIGATION_ITEMS = ("status_center", "current_task", "history", "tools", "settings")


@dataclass(frozen=True)
class AppShellState:
    """UI-only state; transitions never access Rollout, SQLite, titles, or quota."""

    page: str = "status_center"
    dashboard_mode: str = "simple"
    widget_mode: str = "compact"
    selected_thread_id: str | None = None
    history_page: int = 1
    auto_refresh_enabled: bool = False

    def with_dashboard_mode(self, mode: str) -> "AppShellState":
        return replace(self, dashboard_mode=mode if mode in DASHBOARD_MODES else "simple")

    def with_widget_mode(self, mode: str) -> "AppShellState":
        return replace(self, widget_mode=mode if mode in WIDGET_MODES else "compact")

    def navigate(self, page: str) -> "AppShellState":
        return replace(self, page=page if page in NAVIGATION_ITEMS else "status_center")


def normalize_dashboard_mode(value: object) -> str:
    return value if value in DASHBOARD_MODES else "simple"


def normalize_widget_mode(value: object) -> str:
    return value if value in WIDGET_MODES else "compact"
