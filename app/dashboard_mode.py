"""Query-free application shell state for Dashboard and widget modes."""

from __future__ import annotations

from dataclasses import dataclass, replace


DASHBOARD_MODES = ("simple", "advanced")  # Legacy settings compatibility only.
WIDGET_MODES = ("compact", "expanded")
NAVIGATION_ITEMS = (
    "overview",
    "sessions",
    "usage_trends",
    "settings",
)
SECONDARY_PAGES = ("session_detail",)
ALL_PAGES = NAVIGATION_ITEMS + SECONDARY_PAGES


@dataclass(frozen=True)
class AppShellState:
    """UI-only state; transitions never access Rollout, SQLite, titles, or quota."""

    page: str = "overview"
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
        return replace(self, page=page if page in ALL_PAGES else "overview")


def normalize_dashboard_mode(value: object) -> str:
    return value if value in DASHBOARD_MODES else "simple"


def normalize_widget_mode(value: object) -> str:
    return value if value in WIDGET_MODES else "compact"
