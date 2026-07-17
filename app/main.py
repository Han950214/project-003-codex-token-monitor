"""Localized multi-session Windows Dashboard for Codex Token Monitor."""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auto_refresh import AutoRefreshController, DEFAULT_AUTO_REFRESH_SECONDS
from app.advisor import AdvisorResult, Recommendation, build_advisor_input, evaluate_advice
from app.analytics_ui import (
    TREND_STALE_AFTER, TrendView, metric_observed_at, metric_samples, summarize_metric,
    trend_view_from_query,
)
from app.app_actions import open_codex, open_data_directory
from app.codex_rollout import configured_sessions_dir, make_response_safe_id
from app.codex_state import configured_state_path
from app.dashboard import DashboardViewModel, MiniThreadSnapshot, display_session_status
from app.dashboard_mode import ALL_PAGES, AppShellState, NAVIGATION_ITEMS
from app.desktop_widget import (
    DesktopMiniWidget, ExitChoiceDialog, WidgetTooltip, format_percent,
    format_reset_time,
)
from app.diagnostics import (
    DIAGNOSTIC_CHECK_CODES, DiagnosticContext, DiagnosticReport, run_diagnostics,
)
from app.i18n import (
    LANGUAGE_LABELS, language_from_label, localize_auto_refresh,
    localize_presenter_text, localize_status, translate,
)
from app.history import HistoryObservation, UsageHistoryStore
from app.paths import ui_settings_path
from app.quota import CodexQuotaSnapshot
from app.quota_provider import CodexAppServerQuotaProvider, QuotaProvider, find_codex_executable
from app.single_instance import SingleInstanceGuard
from app.version import __version__
from app.new_thread import generic_handoff_template
from app.startup_settings import StartupSettingsDialog
from app.system_tray import SystemTrayController
from app.ui_presenter import (
    DashboardPresentation, disambiguated_session_labels, present_dashboard,
)
from app.ui_format import (
    dashboard_layout_for_width, ellipsize_title, format_compact_token_count,
    format_full_token_count, metric_columns_for_width,
)
from app.trend_chart import TrendCanvas, TrendPoint, TrendTooltipLabels
from app.usage_insights_ui import build_usage_insights_view
from app.ui_icons import CircularProgress, Sparkline, create_icon
from app.ui_settings import (
    LanguageController, load_auto_refresh_enabled,
    load_exit_action_for_today, load_exit_behavior, load_startup_mode,
    load_widget_idle_opacity, load_widget_mode, save_auto_refresh_enabled,
    save_exit_behavior, save_startup_mode, save_widget_idle_opacity,
    save_widget_mode,
)
from app.ui_theme import (
    BODY, BODY_STRONG, BUTTON, CAPTION, CARD_RADIUS, CARD_TITLE, COLORS,
    CONTROL_RADIUS, FONT_BODY, FONT_FAMILY, FONT_SECTION, FONT_SMALL,
    METRIC, NAV, PAGE_TITLE, SECTION_TITLE, SPACE_1, SPACE_2, SPACE_3,
    SPACE_4, SPACE_6, STATUS_TITLE, configure_view,
)
from app.usage_summary import (
    ObservedUsageSummary,
    UsageWindowKind,
    unavailable_usage_summary,
)
from app.windows_startup import WindowsStartupAdapter


UI_SETTINGS_PATH = ui_settings_path()
SESSION_COLUMNS = ("Name", "Status", "Activity", "Turns", "Tokens", "Cache")
SESSION_COLUMN_KEYS = (
    "column_session_name", "column_status", "column_last_activity",
    "column_turns", "column_session_tokens", "column_session_cache_hit",
)
CORE_METRICS = (
    "current_turn", "session_total", "cache_reuse", "reasoning",
    "five_hour_quota", "weekly_quota",
)
TREND_GROUP_METRICS = {
    "tokens": ("input", "output", "total"),
    "cache": ("cached", "cache_reuse", "reasoning"),
    "workflow": ("session_total", "turn_count"),
    "quota": ("five_hour", "weekly"),
}
TREND_GROUP_LABEL_KEYS = {
    key: f"trend_group_{key}" for key in TREND_GROUP_METRICS
}
TREND_METRIC_LABEL_KEYS = {
    "input": "metric_input",
    "output": "metric_output",
    "total": "metric_total",
    "cached": "metric_cached",
    "cache_reuse": "trend_metric_cache_reuse",
    "reasoning": "metric_reasoning",
    "session_total": "trend_metric_session_total",
    "turn_count": "trend_metric_turn_count",
    "five_hour": "trend_metric_five_hour",
    "weekly": "trend_metric_weekly",
}
USAGE_WINDOW_LABEL_KEYS = {
    UsageWindowKind.TODAY: "observed_usage_today",
    UsageWindowKind.ROLLING_5H: "observed_usage_rolling_5h",
    UsageWindowKind.ROLLING_7D: "observed_usage_rolling_7d",
    UsageWindowKind.ROLLING_30D: "observed_usage_rolling_30d",
}


def pagination_bounds(item_count: int, current_page: int, page_size: int = 10) -> tuple[int, int, int, int]:
    page_count = max(1, (max(0, item_count) + page_size - 1) // page_size)
    page = min(max(1, current_page), page_count)
    start = (page - 1) * page_size
    return page, page_count, start, min(start + page_size, max(0, item_count))


class Dashboard:
    def __init__(
        self,
        root: ctk.CTk,
        quota_provider: QuotaProvider | None = None,
        history_store: UsageHistoryStore | None = None,
    ) -> None:
        self.root = root
        configure_view(root)
        self.quota_provider = quota_provider or CodexAppServerQuotaProvider()
        title_loader = getattr(self.quota_provider, "refresh_thread_titles", lambda: {})
        self.view_model = DashboardViewModel(title_batch_loader=title_loader)
        self.history_store = history_store or UsageHistoryStore()
        self.history_store.initialize()
        self.history_error = self.history_store.last_error
        self.language_controller = LanguageController(self._apply_language, UI_SETTINGS_PATH)
        self.language = self.language_controller.language
        self.widget_display_mode = load_widget_mode(UI_SETTINGS_PATH)
        self.current_nav_page = "overview"
        self.shell_state = AppShellState(
            widget_mode=self.widget_display_mode,
            auto_refresh_enabled=load_auto_refresh_enabled(UI_SETTINGS_PATH),
        )
        self.snapshot = None
        self.presentation: DashboardPresentation | None = None
        self.advisor_result: AdvisorResult | None = None
        self.diagnostic_report: DiagnosticReport | None = None
        self.lookback_days = 7
        self.trend_range_days = 7
        self.trend_view = TrendView(7, "empty", (), None)
        self.trend_group = "tokens"
        self.trend_metric = "total"
        self.usage_window_kind = UsageWindowKind.TODAY
        self.usage_insights_expanded = {"threads": False, "responses": False}
        self.observed_usage_summary: ObservedUsageSummary = unavailable_usage_summary(
            self.usage_window_kind,
            as_of_utc=datetime.now(timezone.utc),
            local_timezone=None,
            error_code="history_not_loaded",
        )
        self.status_filter = "all"
        self.label_to_thread: dict[str, str] = {}
        self.selectable_thread_ids: set[str] = set()
        self._rendering_sessions = False
        self.current_page = 1
        self.page_size = 10
        self._widget_mode = False
        self._taskbar_mode = False
        self._tray_mode = False
        self.window_mode = "dashboard"
        self._closing = False
        self._trend_query_generation = 0
        self._trend_query_requests: queue.Queue[
            tuple[int, int, str, UsageWindowKind, datetime]
        ] = queue.Queue(maxsize=1)
        self._trend_query_results: queue.Queue[
            tuple[int, TrendView, ObservedUsageSummary, str | None]
        ] = queue.Queue()
        self._trend_query_stop = threading.Event()
        self._trend_query_poll_scheduled = False
        self._trend_query_worker = threading.Thread(
            target=self._trend_query_worker_loop,
            name="trend-query-worker",
            daemon=True,
        )
        self._trend_query_worker.start()
        self._taskbar_iconify_scheduled = False
        self._layout_job: str | None = None
        self._sidebar_collapsed = False
        self._widget_thread_id: str | None = None
        self._last_dashboard_geometry = "1280x800"
        self._mini_thread_snapshot = MiniThreadSnapshot("", None, None, "no_selection", None)
        self.quota_snapshot = CodexQuotaSnapshot.unavailable()

        self.auto_refresh_var = tk.BooleanVar(master=root, value=self.shell_state.auto_refresh_enabled)
        self.data_status_var = tk.StringVar(value="")
        self.status_message_var = tk.StringVar(value="")
        self.last_event_var = tk.StringVar(value="—")
        self.last_refresh_var = tk.StringVar(value="—")
        self.task_label_var = tk.StringVar(value="")
        self.core_metric_widgets: list[dict[str, object]] = []
        self.source_widgets: dict[str, dict[str, object]] = {}
        self.page_frames: dict[str, ctk.CTkFrame] = {}
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self.ui_icons: dict[str, ctk.CTkImage] = {}
        self.status_recent_rows: list[dict[str, object]] = []
        self.diagnostic_rows: list[tuple[ctk.CTkLabel, ctk.CTkLabel]] = []
        self.startup_adapter = WindowsStartupAdapter()

        root.title("Codex Token Monitor")
        root.geometry("1280x800")
        root.minsize(980, 660)
        self._build()
        self.auto_refresh = AutoRefreshController(
            root.after, root.after_cancel, self.refresh, on_error=self._auto_refresh_error,
        )
        self.mini_widget = DesktopMiniWidget(
            root,
            on_restore=self.restore_dashboard,
            on_minimize=self._minimize_to_taskbar,
            on_hide_to_tray=self.hide_to_tray,
            on_exit=self.request_exit,
            on_refresh=self.manual_refresh,
            settings_path=UI_SETTINGS_PATH,
            on_more=self._show_widget_more,
        )
        self.exit_dialog = ExitChoiceDialog(root, UI_SETTINGS_PATH)
        self.settings_dialog = StartupSettingsDialog(root, UI_SETTINGS_PATH, on_idle_opacity_saved=self.mini_widget.set_idle_opacity)
        self.tray = SystemTrayController(
            root,
            on_restore_dashboard=self.restore_dashboard,
            on_show_widget=self._enter_widget_mode,
            on_hide_to_tray=self.hide_to_tray,
            on_manual_refresh=self.manual_refresh,
            on_toggle_auto_refresh=self._toggle_auto_refresh_from_tray,
            on_settings=self.show_settings,
            on_exit=self.close,
        )
        self.tray.start()
        root.protocol("WM_DELETE_WINDOW", self.request_exit)
        root.bind("<Unmap>", self._on_root_unmap, add="+")
        root.bind("<Map>", self._on_root_map, add="+")
        root.bind("<Configure>", self._on_root_configure, add="+")
        self._apply_language(self.language)
        self.refresh()
        self.auto_refresh.set_enabled(bool(self.auto_refresh_var.get()))
        self._apply_startup_mode(load_startup_mode(UI_SETTINGS_PATH))

    def _build(self) -> None:
        self.root.grid_columnconfigure(0, minsize=184)
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        main = ctk.CTkFrame(self.root, fg_color="transparent", corner_radius=0)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=1)
        self.main_container = main
        self._build_header()
        self._build_content()
        self.show_page("overview")

    def _build_sidebar(self) -> None:
        sidebar = self.sidebar = ctk.CTkFrame(
            self.root, width=184, corner_radius=0,
            fg_color=COLORS.telemetry,
        )
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(1, weight=1)
        brand = ctk.CTkFrame(sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=SPACE_3, pady=(SPACE_4, SPACE_6))
        self.ui_icons["brand"] = create_icon("pulse", size=18, color=COLORS.on_accent)
        self.brand_icon = ctk.CTkLabel(
            brand, text="", image=self.ui_icons["brand"],
            width=30, height=30, corner_radius=7,
            fg_color=COLORS.accent, font=(FONT_FAMILY, 15, "bold"),
            text_color=COLORS.on_accent,
        )
        self.brand_icon.grid(row=0, column=0, padx=(0, SPACE_2))
        self.brand_name = ctk.CTkLabel(
            brand, text="Codex Token\nMonitor", font=(FONT_FAMILY, 13, "bold"),
            justify="left", anchor="w", text_color=COLORS.telemetry_text,
        )
        self.brand_name.grid(row=0, column=1, sticky="ew")
        nav = ctk.CTkFrame(sidebar, fg_color="transparent")
        nav.grid(row=1, column=0, sticky="new", padx=SPACE_2)
        nav.grid_columnconfigure(0, weight=1)
        nav_icon_kinds = {
            "overview": "home", "sessions": "history",
            "usage_trends": "trend", "recommendations": "recommendation",
            "tools": "tools", "settings": "settings",
        }
        for row, page in enumerate(NAVIGATION_ITEMS):
            self.ui_icons[f"nav_{page}"] = create_icon(
                nav_icon_kinds[page], size=19, color=COLORS.telemetry_text,
            )
            button = ctk.CTkButton(
                nav, text="", image=self.ui_icons[f"nav_{page}"],
                compound="left", command=lambda target=page: self.show_page(target),
                height=44, anchor="w", corner_radius=CONTROL_RADIUS,
                fg_color="transparent", hover_color=COLORS.telemetry_hover,
                text_color=COLORS.telemetry_text, font=NAV,
            )
            button.grid(row=row, column=0, sticky="ew", pady=2)
            self.nav_buttons[page] = button
            WidgetTooltip(button, lambda target=page: translate(f"nav_{target}", self.language))
        footer = ctk.CTkFrame(sidebar, fg_color=COLORS.telemetry_footer, corner_radius=0)
        footer.grid(row=2, column=0, sticky="sew")
        footer.grid_columnconfigure(0, weight=1)
        self.nav_connection_var = tk.StringVar(master=self.root, value="—")
        self.nav_version_var = tk.StringVar(master=self.root, value=f"v{__version__}")
        ctk.CTkLabel(footer, textvariable=self.nav_connection_var, font=FONT_SMALL, text_color=COLORS.telemetry_secondary, anchor="w").grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_1))
        ctk.CTkLabel(footer, textvariable=self.nav_version_var, font=FONT_SMALL, text_color=COLORS.telemetry_muted, anchor="w").grid(row=1, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_3))

    def _build_header(self) -> None:
        header = ctk.CTkFrame(
            self.main_container, fg_color=COLORS.surface, corner_radius=0,
            border_width=0,
        )
        header.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        header.grid_columnconfigure(0, weight=1)
        title_group = ctk.CTkFrame(header, fg_color="transparent")
        title_group.grid(row=0, column=0, sticky="w", padx=SPACE_4, pady=SPACE_3)
        self.page_title_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(
            title_group, textvariable=self.page_title_var, font=PAGE_TITLE,
            text_color=COLORS.primary_text, anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.header_status_var = tk.StringVar(master=self.root, value="—")
        self.header_status_badge = ctk.CTkLabel(
            title_group, textvariable=self.header_status_var, font=CAPTION,
            text_color=COLORS.real, fg_color=COLORS.real_soft,
            corner_radius=10, padx=10, height=24,
        )
        self.header_status_badge.grid(row=0, column=1, padx=(SPACE_3, 0))

        primary_actions = ctk.CTkFrame(header, fg_color="transparent")
        primary_actions.grid(row=0, column=1, sticky="e", padx=SPACE_4, pady=SPACE_3)
        self.auto_switch = ctk.CTkSwitch(
            primary_actions, text="", variable=self.auto_refresh_var,
            command=self._toggle_auto_refresh, width=150, font=CAPTION,
            progress_color=COLORS.real,
        )
        self.auto_switch.grid(row=0, column=0, padx=(0, SPACE_2))
        self.ui_icons["header_refresh"] = create_icon(
            "refresh", size=19, color=COLORS.primary_text,
        )
        self.ui_icons["header_widget"] = create_icon(
            "widget", size=19, color=COLORS.primary_text,
        )
        self.ui_icons["header_settings"] = create_icon(
            "settings", size=19, color=COLORS.primary_text,
        )
        self.refresh_button = ctk.CTkButton(
            primary_actions, text="", image=self.ui_icons["header_refresh"],
            command=self.manual_refresh, width=34,
            height=34, corner_radius=CONTROL_RADIUS, fg_color="transparent",
            text_color=COLORS.primary_text, hover_color=COLORS.accent_soft,
            font=(FONT_FAMILY, 18),
        )
        self.refresh_button.grid(row=0, column=1, padx=SPACE_1)
        self.mini_widget_button = ctk.CTkButton(
            primary_actions, text="", image=self.ui_icons["header_widget"],
            command=self._enter_widget_mode, width=34,
            height=34, corner_radius=CONTROL_RADIUS, fg_color="transparent",
            text_color=COLORS.primary_text, hover_color=COLORS.accent_soft,
        )
        self.mini_widget_button.grid(row=0, column=2, padx=SPACE_1)
        self.header_settings_button = ctk.CTkButton(
            primary_actions, text="", image=self.ui_icons["header_settings"],
            command=lambda: self.show_page("settings"),
            width=34, height=34, corner_radius=CONTROL_RADIUS,
            fg_color="transparent", text_color=COLORS.primary_text,
            hover_color=COLORS.accent_soft,
        )
        self.header_settings_button.grid(row=0, column=3, padx=SPACE_1)
        self.language_menu = ctk.CTkOptionMenu(
            primary_actions, values=list(LANGUAGE_LABELS.values()),
            command=self._change_language, width=104, height=34,
            corner_radius=CONTROL_RADIUS, fg_color=COLORS.surface,
            button_color=COLORS.raised_surface,
            button_hover_color=COLORS.border, text_color=COLORS.primary_text,
            dropdown_fg_color=COLORS.surface,
            dropdown_text_color=COLORS.primary_text,
            dropdown_hover_color=COLORS.accent_soft,
        )
        self.language_menu.grid(row=0, column=4, padx=(SPACE_2, 0))
        self.header_message_label = ctk.CTkLabel(
            title_group, textvariable=self.status_message_var, font=CAPTION,
            text_color=COLORS.secondary_text, anchor="w", width=170,
        )
        self.header_message_label.grid(
            row=0, column=2, sticky="w", padx=(SPACE_3, 0),
        )
        WidgetTooltip(self.refresh_button, lambda: translate("manual_refresh", self.language))
        WidgetTooltip(self.mini_widget_button, lambda: translate("show_mini_widget", self.language))
        WidgetTooltip(self.header_settings_button, lambda: translate("nav_settings", self.language))

    def _build_content(self) -> None:
        host = ctk.CTkFrame(self.main_container, fg_color="transparent", corner_radius=0)
        host.grid(row=1, column=0, sticky="nsew", padx=SPACE_4, pady=(0, SPACE_3))
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)
        self.page_host = host
        for page in ALL_PAGES:
            frame = ctk.CTkFrame(host, fg_color="transparent", corner_radius=0)
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(0, weight=1)
            self.page_frames[page] = frame
        self._build_status_center(self.page_frames["overview"])
        self._build_current_task_page(self.page_frames["session_detail"])
        self._build_history_page(self.page_frames["sessions"])
        self._build_usage_trends_page(self.page_frames["usage_trends"])
        self._build_recommendations_page(self.page_frames["recommendations"])
        self._build_tools_page(self.page_frames["tools"])
        self._build_settings_page(self.page_frames["settings"])

    def _build_status_center(self, parent: ctk.CTkFrame) -> None:
        """Build the one product dashboard from shared in-memory presentation state."""
        page = self.status_page = ctk.CTkScrollableFrame(
            parent, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=COLORS.scrollbar_thumb,
            scrollbar_button_hover_color=COLORS.scrollbar_thumb_hover,
        )
        page.grid(row=0, column=0, sticky="nsew")
        self.status_advice_card = self._build_status_advice(page)
        self.core_metrics_panel = self._build_core_metrics_panel(page)
        self.observed_usage_card = self._build_observed_usage_card(page)
        self.task_summary_card = self._build_task_summary_card(page)
        self.quota_center_card = self._build_quota_center_card(page)
        self.trend_preview_card = self._build_trend_preview_card(page)
        self.quick_actions_card = self._build_quick_actions_card(page)
        self.status_recent_card = self._build_status_recent_card(page)
        self._apply_status_layout(1000)

    @staticmethod
    def _section_card(parent: ctk.CTkFrame) -> ctk.CTkFrame:
        return ctk.CTkFrame(
            parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS,
            border_width=1, border_color=COLORS.border,
        )

    def _build_status_advice(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        card = self._section_card(parent)
        card.grid_columnconfigure(1, weight=1)
        self.status_section_title = ctk.CTkLabel(
            card, text="", font=SECTION_TITLE, text_color=COLORS.primary_text,
            anchor="w",
        )
        self.status_section_title.grid(
            row=0, column=0, columnspan=2, sticky="ew", padx=SPACE_4,
            pady=(SPACE_3, SPACE_2),
        )
        self.ui_icons["status_shield"] = create_icon(
            "shield", size=24, color=COLORS.on_accent,
        )
        self.simple_status_accent = ctk.CTkLabel(
            card, text="", image=self.ui_icons["status_shield"],
            width=38, height=38, corner_radius=10,
            font=(FONT_FAMILY, 19, "bold"), text_color=COLORS.on_accent,
            fg_color=COLORS.real,
        )
        self.simple_status_accent.grid(
            row=1, column=0, sticky="nw", padx=(SPACE_4, SPACE_3), pady=SPACE_1,
        )
        self.simple_status_title_var = tk.StringVar(master=self.root, value="—")
        self.simple_reason_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(
            card, textvariable=self.simple_status_title_var, font=STATUS_TITLE,
            text_color=COLORS.primary_text, anchor="w",
        ).grid(row=1, column=1, sticky="ew", padx=(0, SPACE_4), pady=SPACE_1)
        self.status_reason_label = ctk.CTkLabel(
            card, textvariable=self.simple_reason_var, font=BODY,
            text_color=COLORS.secondary_text, anchor="w", justify="left",
            wraplength=420, height=40,
        )
        self.status_reason_label.grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=SPACE_4,
            pady=(SPACE_2, SPACE_3),
        )
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.grid(
            row=3, column=0, columnspan=2, sticky="w", padx=SPACE_4,
            pady=(0, SPACE_4),
        )
        self.primary_action_button = ctk.CTkButton(
            actions, text="", command=self._execute_primary_action, width=150,
            height=38, fg_color=COLORS.accent, hover_color=COLORS.accent_hover,
            font=BUTTON,
        )
        self.primary_action_button.grid(row=0, column=0, padx=(0, SPACE_2))
        self.reason_button = ctk.CTkButton(
            actions, text="", command=self._show_reason, width=104, height=38,
            fg_color="transparent", border_width=1, border_color=COLORS.border,
            text_color=COLORS.primary_text, hover_color=COLORS.accent_soft,
            font=BUTTON,
        )
        self.reason_button.grid(row=0, column=1)
        return card

    def _build_core_metrics_panel(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        panel = self._section_card(parent)
        panel.grid_columnconfigure(0, weight=1)
        self.core_metrics_title = ctk.CTkLabel(
            panel, text="", font=SECTION_TITLE, text_color=COLORS.primary_text,
            anchor="w",
        )
        self.core_metrics_title.grid(
            row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2),
        )
        self.core_cards_frame = ctk.CTkFrame(panel, fg_color="transparent")
        self.core_cards_frame.grid(
            row=1, column=0, sticky="ew", padx=SPACE_3, pady=(0, SPACE_3),
        )
        accents = (
            COLORS.accent, COLORS.purple, COLORS.real, COLORS.orange,
            COLORS.teal, COLORS.accent,
        )
        softs = (
            COLORS.accent_soft, COLORS.purple_soft, COLORS.real_soft,
            COLORS.orange_soft, COLORS.teal_soft, COLORS.accent_soft,
        )
        for semantic, accent, soft in zip(CORE_METRICS, accents, softs):
            card = ctk.CTkFrame(
                self.core_cards_frame, fg_color=COLORS.raised_surface,
                corner_radius=CONTROL_RADIUS, border_width=1,
                border_color=COLORS.border, width=128, height=144,
            )
            card.grid_propagate(False)
            card.grid_columnconfigure(0, weight=1)
            title_var = tk.StringVar(master=self.root, value="")
            scope_var = tk.StringVar(master=self.root, value="")
            value_var = tk.StringVar(master=self.root, value="—")
            hint_var = tk.StringVar(master=self.root, value="")
            full_var = tk.StringVar(master=self.root, value="—")
            ctk.CTkLabel(
                card, textvariable=title_var, font=CARD_TITLE,
                text_color=COLORS.primary_text, anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=SPACE_2, pady=(SPACE_3, 0))
            ctk.CTkLabel(
                card, textvariable=scope_var, font=CAPTION,
                text_color=COLORS.muted_text, anchor="w",
            ).grid(row=1, column=0, sticky="ew", padx=SPACE_2, pady=(0, SPACE_2))
            value_label = ctk.CTkLabel(
                card, textvariable=value_var, font=METRIC, text_color=accent,
                anchor="w",
            )
            value_label.grid(row=2, column=0, sticky="ew", padx=SPACE_2)
            ctk.CTkLabel(
                card, textvariable=hint_var, font=CAPTION,
                text_color=COLORS.secondary_text, anchor="w", justify="left",
                wraplength=80,
            ).grid(row=3, column=0, sticky="ew", padx=SPACE_2, pady=(SPACE_1, SPACE_3))
            progress = None
            sparkline = None
            ring = None
            if semantic == "cache_reuse":
                progress = ctk.CTkProgressBar(
                    card, height=6, corner_radius=3, fg_color=soft,
                    progress_color=accent,
                )
                progress.grid(
                    row=4, column=0, sticky="ew", padx=SPACE_2,
                    pady=(0, SPACE_3),
                )
                progress.set(0)
            elif semantic in {"five_hour_quota", "weekly_quota"}:
                ring = CircularProgress(
                    card, size=48, background=COLORS.raised_surface,
                    track=soft, color=accent,
                )
                ring.grid(row=4, column=0, sticky="w", padx=SPACE_2, pady=(0, SPACE_3))
            else:
                sparkline = Sparkline(
                    card, width=72, height=28,
                    background=COLORS.raised_surface, color=accent,
                )
                sparkline.grid(
                    row=4, column=0, sticky="ew", padx=SPACE_2,
                    pady=(0, SPACE_3),
                )
                sparkline.grid_remove()
            WidgetTooltip(value_label, lambda variable=full_var: variable.get())
            self.core_metric_widgets.append({
                "semantic": semantic, "card": card, "title": title_var,
                "scope": scope_var, "value": value_var, "hint": hint_var,
                "full": full_var, "progress": progress,
                "sparkline": sparkline, "ring": ring,
            })
        return panel

    def _build_observed_usage_card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        card = self._section_card(parent)
        card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, 0))
        header.grid_columnconfigure(0, weight=1)
        self.observed_usage_title = ctk.CTkLabel(
            header,
            text="",
            font=SECTION_TITLE,
            text_color=COLORS.primary_text,
            anchor="w",
        )
        self.observed_usage_title.grid(row=0, column=0, sticky="ew")
        self.observed_usage_window_menu = ctk.CTkOptionMenu(
            header,
            values=[""],
            command=self._change_usage_window,
            width=142,
            height=30,
            fg_color=COLORS.raised_surface,
            button_color=COLORS.accent,
            button_hover_color=COLORS.accent_hover,
            text_color=COLORS.primary_text,
        )
        self.observed_usage_window_menu.grid(row=0, column=1, padx=(SPACE_3, 0))
        self.observed_usage_disclaimer = ctk.CTkLabel(
            card,
            text="",
            font=CAPTION,
            text_color=COLORS.secondary_text,
            anchor="w",
            justify="left",
            wraplength=620,
        )
        self.observed_usage_disclaimer.grid(
            row=1,
            column=0,
            sticky="ew",
            padx=SPACE_4,
            pady=(SPACE_1, SPACE_2),
        )

        self.observed_usage_metrics_host = ctk.CTkFrame(card, fg_color="transparent")
        self.observed_usage_metrics_host.grid(
            row=2,
            column=0,
            sticky="ew",
            padx=SPACE_3,
        )
        self.observed_usage_metric_widgets: dict[str, dict[str, object]] = {}
        for name in ("total", "input", "output", "cached", "reasoning"):
            metric_card = ctk.CTkFrame(
                self.observed_usage_metrics_host,
                fg_color=COLORS.raised_surface,
                corner_radius=CONTROL_RADIUS,
                border_width=1,
                border_color=COLORS.border,
                height=86,
            )
            metric_card.grid_propagate(False)
            metric_card.grid_columnconfigure(0, weight=1)
            title_var = tk.StringVar(master=self.root, value="")
            value_var = tk.StringVar(master=self.root, value="—")
            full_var = tk.StringVar(master=self.root, value="—")
            ctk.CTkLabel(
                metric_card,
                textvariable=title_var,
                font=CAPTION,
                text_color=COLORS.secondary_text,
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=SPACE_2, pady=(SPACE_2, 0))
            value_label = ctk.CTkLabel(
                metric_card,
                textvariable=value_var,
                font=METRIC if name == "total" else BODY_STRONG,
                text_color=COLORS.accent if name == "total" else COLORS.primary_text,
                anchor="w",
            )
            value_label.grid(
                row=1,
                column=0,
                sticky="ew",
                padx=SPACE_2,
                pady=(SPACE_1, SPACE_2),
            )
            WidgetTooltip(value_label, lambda variable=full_var: variable.get())
            self.observed_usage_metric_widgets[name] = {
                "card": metric_card,
                "title": title_var,
                "value": value_var,
                "full": full_var,
            }

        self.observed_usage_aux_host = ctk.CTkFrame(card, fg_color="transparent")
        self.observed_usage_aux_host.grid(
            row=3,
            column=0,
            sticky="ew",
            padx=SPACE_3,
            pady=(SPACE_2, 0),
        )
        self.observed_usage_aux_widgets: dict[str, dict[str, object]] = {}
        for name in ("responses", "sessions", "average", "cache_reuse"):
            cell = ctk.CTkFrame(
                self.observed_usage_aux_host,
                fg_color=COLORS.raised_surface,
                corner_radius=CONTROL_RADIUS,
            )
            cell.grid_columnconfigure(0, weight=1)
            title_var = tk.StringVar(master=self.root, value="")
            value_var = tk.StringVar(master=self.root, value="—")
            ctk.CTkLabel(
                cell,
                textvariable=title_var,
                font=CAPTION,
                text_color=COLORS.secondary_text,
                anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=SPACE_2, pady=(SPACE_2, 0))
            ctk.CTkLabel(
                cell,
                textvariable=value_var,
                font=BODY_STRONG,
                text_color=COLORS.primary_text,
                anchor="w",
            ).grid(row=1, column=0, sticky="ew", padx=SPACE_2, pady=(0, SPACE_2))
            self.observed_usage_aux_widgets[name] = {
                "cell": cell,
                "title": title_var,
                "value": value_var,
            }

        self.observed_usage_coverage_var = tk.StringVar(master=self.root, value="")
        self.observed_usage_coverage_label = ctk.CTkLabel(
            card,
            textvariable=self.observed_usage_coverage_var,
            font=CAPTION,
            text_color=COLORS.secondary_text,
            anchor="w",
            justify="left",
            wraplength=680,
        )
        self.observed_usage_coverage_label.grid(
            row=4,
            column=0,
            sticky="ew",
            padx=SPACE_4,
            pady=(SPACE_2, SPACE_3),
        )
        return card

    def _build_task_summary_card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        card = self._section_card(parent)
        card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_1))
        header.grid_columnconfigure(0, weight=1)
        self.simple_task_title = ctk.CTkLabel(
            header, text="", font=SECTION_TITLE, text_color=COLORS.primary_text,
            anchor="w",
        )
        self.simple_task_title.grid(row=0, column=0, sticky="ew")
        self.task_summary_status_var = tk.StringVar(master=self.root, value="—")
        self.task_summary_status = ctk.CTkLabel(
            header, textvariable=self.task_summary_status_var, font=CAPTION,
            corner_radius=8, padx=8, text_color=COLORS.real,
            fg_color=COLORS.real_soft,
        )
        self.task_summary_status.grid(row=0, column=1, padx=(SPACE_2, 0))
        self.simple_task_vars = {
            name: tk.StringVar(master=self.root, value="—")
            for name in ("title", "status", "turns", "instruction", "session", "activity")
        }
        self.task_full_title_var = tk.StringVar(master=self.root, value="—")
        title = ctk.CTkLabel(
            card, textvariable=self.simple_task_vars["title"], font=BODY_STRONG,
            text_color=COLORS.primary_text, anchor="w",
        )
        title.grid(row=1, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_2, SPACE_2))
        WidgetTooltip(title, lambda: self.task_full_title_var.get())
        stats = ctk.CTkFrame(card, fg_color="transparent")
        stats.grid(row=2, column=0, sticky="ew", padx=SPACE_4)
        stats.grid_columnconfigure((0, 1, 2), weight=1, uniform="task_stat")
        self.simple_task_labels = {}
        for column, name in enumerate(("turns", "instruction", "session")):
            cell = ctk.CTkFrame(
                stats, fg_color=COLORS.raised_surface, corner_radius=CONTROL_RADIUS,
            )
            cell.grid(
                row=0, column=column, sticky="nsew",
                padx=(0 if column == 0 else SPACE_1, 0),
            )
            label = ctk.CTkLabel(
                cell, text="", font=CAPTION, text_color=COLORS.secondary_text,
                anchor="w",
            )
            label.grid(row=0, column=0, sticky="ew", padx=SPACE_2, pady=(SPACE_2, 0))
            self.simple_task_labels[name] = label
            ctk.CTkLabel(
                cell, textvariable=self.simple_task_vars[name], font=BODY_STRONG,
                text_color=COLORS.primary_text, anchor="w",
            ).grid(row=1, column=0, sticky="ew", padx=SPACE_2, pady=(0, SPACE_2))
        self.task_activity_label = ctk.CTkLabel(
            card, textvariable=self.simple_task_vars["activity"], font=CAPTION,
            text_color=COLORS.secondary_text, anchor="w",
        )
        self.task_activity_label.grid(
            row=3, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_2, SPACE_2),
        )
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="e", padx=SPACE_4, pady=(0, SPACE_3))
        self.task_switch_button_home = ctk.CTkButton(
            actions, text="", command=lambda: self.show_page("sessions"), width=96,
            height=30, fg_color="transparent", border_width=1,
            border_color=COLORS.border, text_color=COLORS.primary_text,
            hover_color=COLORS.accent_soft,
        )
        self.task_switch_button_home.grid(row=0, column=0, padx=(0, SPACE_2))
        self.task_detail_button_home = ctk.CTkButton(
            actions, text="", command=lambda: self.show_page("session_detail"),
            width=104, height=30, fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
        )
        self.task_detail_button_home.grid(row=0, column=1)
        for widget in (card, title, self.simple_task_title):
            widget.bind("<Button-1>", lambda _event: self.show_page("session_detail"))
        return card

    def _build_quota_center_card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        card = self._section_card(parent)
        card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
        header.grid_columnconfigure(0, weight=1)
        self.simple_quota_title = ctk.CTkLabel(
            header, text="", font=SECTION_TITLE, text_color=COLORS.primary_text,
            anchor="w",
        )
        self.simple_quota_title.grid(row=0, column=0, sticky="ew")
        self.quota_detail_button = ctk.CTkButton(
            header, text="", command=lambda: self.show_page("session_detail"),
            width=84, height=28, fg_color="transparent", border_width=1,
            border_color=COLORS.border, text_color=COLORS.primary_text,
            hover_color=COLORS.accent_soft,
        )
        self.quota_detail_button.grid(row=0, column=1)
        self.quota_cards_host = ctk.CTkFrame(card, fg_color="transparent")
        self.quota_cards_host.grid(
            row=1, column=0, sticky="ew", padx=SPACE_3, pady=(0, SPACE_3),
        )
        self.quota_cards_host.grid_columnconfigure((0, 1), weight=1, uniform="quota")
        self.simple_quota_vars = {}
        self.quota_window_widgets: dict[str, dict[str, object]] = {}
        for column, (prefix, accent, soft) in enumerate((
            ("five", COLORS.real, COLORS.real_soft),
            ("week", COLORS.accent, COLORS.accent_soft),
        )):
            window_card = ctk.CTkFrame(
                self.quota_cards_host, fg_color=COLORS.raised_surface,
                corner_radius=CONTROL_RADIUS, border_width=1,
                border_color=COLORS.border,
            )
            window_card.grid(
                row=0, column=column, sticky="nsew",
                padx=(0 if column == 0 else SPACE_1, 0),
            )
            window_card.grid_columnconfigure(1, weight=1)
            title_var = tk.StringVar(master=self.root, value="")
            remaining_var = tk.StringVar(master=self.root, value="—")
            used_var = tk.StringVar(master=self.root, value="—")
            reset_var = tk.StringVar(master=self.root, value="—")
            state_var = tk.StringVar(master=self.root, value="")
            self.simple_quota_vars[f"{prefix}_remaining"] = remaining_var
            self.simple_quota_vars[f"{prefix}_used"] = used_var
            self.simple_quota_vars[f"{prefix}_reset"] = reset_var
            ctk.CTkLabel(
                window_card, textvariable=title_var, font=CARD_TITLE,
                text_color=COLORS.primary_text, anchor="w",
            ).grid(
                row=0, column=0, columnspan=2, sticky="ew",
                padx=SPACE_3, pady=(SPACE_2, 0),
            )
            ring = CircularProgress(
                window_card, size=58, background=COLORS.raised_surface,
                track=soft, color=accent,
            )
            ring.grid(
                row=1, column=0, rowspan=5, sticky="w",
                padx=(SPACE_3, SPACE_2), pady=(SPACE_1, SPACE_2),
            )
            ctk.CTkLabel(
                window_card, textvariable=remaining_var, font=METRIC,
                text_color=accent, anchor="w",
            ).grid(row=1, column=1, sticky="ew", padx=(0, SPACE_3))
            ctk.CTkLabel(
                window_card, textvariable=used_var, font=CAPTION,
                text_color=COLORS.secondary_text, anchor="w",
            ).grid(row=2, column=1, sticky="ew", padx=(0, SPACE_3))
            progress = ctk.CTkProgressBar(
                window_card, height=7, corner_radius=4, fg_color=soft,
                progress_color=accent,
            )
            progress.grid(
                row=3, column=1, sticky="ew", padx=(0, SPACE_3), pady=SPACE_2,
            )
            progress.set(0)
            ctk.CTkLabel(
                window_card, textvariable=reset_var, font=CAPTION,
                text_color=COLORS.secondary_text, anchor="w",
            ).grid(row=4, column=1, sticky="ew", padx=(0, SPACE_3))
            state_label = ctk.CTkLabel(
                window_card, textvariable=state_var, font=CAPTION,
                text_color=COLORS.unknown, anchor="w",
            )
            state_label.grid(
                row=5, column=1, sticky="ew", padx=(0, SPACE_3), pady=(0, SPACE_2),
            )
            self.quota_window_widgets[prefix] = {
                "title": title_var, "state": state_var,
                "state_label": state_label, "progress": progress, "ring": ring,
            }
        return card

    def _build_quick_actions_card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        card = self._section_card(parent)
        card.grid_columnconfigure(0, weight=1)
        self.quick_title = ctk.CTkLabel(
            card, text="", font=SECTION_TITLE, text_color=COLORS.primary_text,
            anchor="w",
        )
        self.quick_title.grid(
            row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2),
        )
        self.quick_host = ctk.CTkFrame(card, fg_color="transparent")
        self.quick_host.grid(
            row=1, column=0, sticky="ew", padx=SPACE_3, pady=(0, SPACE_3),
        )
        self.ui_icons["quick_diagnose"] = create_icon(
            "pulse", size=20, color=COLORS.on_accent,
        )
        for name, kind in (
            ("quick_codex", "open"), ("quick_history", "history"),
            ("quick_more", "more"),
        ):
            self.ui_icons[name] = create_icon(kind, size=20, color=COLORS.accent)
        self.quick_diagnose = ctk.CTkButton(
            self.quick_host, text="", image=self.ui_icons["quick_diagnose"],
            compound="left", command=self.start_diagnostics,
            fg_color=COLORS.accent, hover_color=COLORS.accent_hover,
        )
        self.quick_codex = ctk.CTkButton(
            self.quick_host, text="", image=self.ui_icons["quick_codex"],
            compound="left", command=self._open_codex,
            fg_color=COLORS.raised_surface, text_color=COLORS.primary_text,
            hover_color=COLORS.accent_soft,
        )
        self.quick_history = ctk.CTkButton(
            self.quick_host, text="", image=self.ui_icons["quick_history"],
            compound="left", command=self._show_new_thread_dialog,
            fg_color=COLORS.raised_surface, text_color=COLORS.primary_text,
            hover_color=COLORS.accent_soft,
        )
        self.quick_more = ctk.CTkButton(
            self.quick_host, text="", image=self.ui_icons["quick_more"],
            compound="left", command=lambda: self.show_page("tools"),
            fg_color=COLORS.raised_surface, text_color=COLORS.primary_text,
            hover_color=COLORS.accent_soft,
        )
        self.quick_action_buttons = (
            self.quick_diagnose, self.quick_codex, self.quick_history, self.quick_more,
        )
        for index, button in enumerate(self.quick_action_buttons):
            row, column = divmod(index, 2)
            self.quick_host.grid_columnconfigure(column, weight=1, uniform="quick")
            button.grid(
                row=row, column=column, sticky="ew", padx=SPACE_1, pady=SPACE_1,
            )
        return card

    def _build_status_recent_card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        card = self._section_card(parent)
        card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
        header.grid_columnconfigure(0, weight=1)
        self.status_recent_title = ctk.CTkLabel(
            header, text="", font=SECTION_TITLE, text_color=COLORS.primary_text,
            anchor="w",
        )
        self.status_recent_title.grid(row=0, column=0, sticky="ew")
        self.status_recent_all = ctk.CTkButton(
            header, text="", command=lambda: self.show_page("sessions"), width=110,
            height=28, fg_color="transparent", text_color=COLORS.accent,
            hover_color=COLORS.accent_soft,
        )
        self.status_recent_all.grid(row=0, column=1)
        for index in range(5):
            full_title = tk.StringVar(master=self.root, value="—")
            title_var = tk.StringVar(master=self.root, value="—")
            detail_var = tk.StringVar(master=self.root, value="")
            current_var = tk.StringVar(master=self.root, value="")
            row = ctk.CTkButton(
                card, text="", command=lambda item=index: self._select_status_recent(item),
                height=48, corner_radius=CONTROL_RADIUS,
                fg_color=COLORS.raised_surface, hover_color=COLORS.accent_soft,
                border_width=1, border_color=COLORS.border,
            )
            row.grid(
                row=index + 1, column=0, sticky="ew", padx=SPACE_3,
                pady=(0, SPACE_2 if index < 4 else SPACE_3),
            )
            row.grid_columnconfigure(0, weight=1)
            title = ctk.CTkLabel(
                row, textvariable=title_var, font=BODY_STRONG,
                text_color=COLORS.primary_text, anchor="w",
            )
            title.grid(row=0, column=0, sticky="ew", padx=SPACE_3, pady=(SPACE_1, 0))
            ctk.CTkLabel(
                row, textvariable=current_var, font=CAPTION,
                text_color=COLORS.real, anchor="e",
            ).grid(row=0, column=1, padx=SPACE_3, pady=(SPACE_1, 0))
            ctk.CTkLabel(
                row, textvariable=detail_var, font=CAPTION,
                text_color=COLORS.secondary_text, anchor="w",
            ).grid(
                row=1, column=0, columnspan=2, sticky="ew", padx=SPACE_3,
                pady=(0, SPACE_1),
            )
            WidgetTooltip(title, lambda variable=full_title: variable.get())
            self.status_recent_rows.append({
                "button": row, "title": title_var, "detail": detail_var,
                "current": current_var, "full_title": full_title,
                "thread_id": None,
            })
        return card

    def _build_trend_preview_card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        card = self._section_card(parent)
        card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
        header.grid_columnconfigure(0, weight=1)
        self.trend_preview_title = ctk.CTkLabel(
            header, text="", font=SECTION_TITLE,
            text_color=COLORS.primary_text, anchor="w",
        )
        self.trend_preview_title.grid(row=0, column=0, sticky="ew")
        self.trend_preview_scope_var = tk.StringVar(master=self.root, value="")
        self.trend_preview_scope = ctk.CTkLabel(
            header, textvariable=self.trend_preview_scope_var, font=CAPTION,
            text_color=COLORS.accent, anchor="e",
        )
        self.trend_preview_scope.grid(row=0, column=1, padx=SPACE_2)
        self.trend_preview_open = ctk.CTkButton(
            header, text="", command=lambda: self.show_page("usage_trends"),
            width=96, height=28, fg_color="transparent",
            text_color=COLORS.accent, hover_color=COLORS.accent_soft,
        )
        self.trend_preview_open.grid(row=0, column=2)
        self.trend_preview_state_var = tk.StringVar(master=self.root, value="—")
        self.trend_preview_message_var = tk.StringVar(master=self.root, value="")
        self.trend_preview_state = ctk.CTkLabel(
            card, textvariable=self.trend_preview_state_var, font=BODY_STRONG,
            text_color=COLORS.unknown, anchor="w",
        )
        self.trend_preview_state.grid(
            row=1, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_1, 0),
        )
        self.trend_preview_message = ctk.CTkLabel(
            card, textvariable=self.trend_preview_message_var, font=BODY,
            text_color=COLORS.secondary_text, anchor="w", justify="left",
            wraplength=420,
        )
        self.trend_preview_message.grid(
            row=2, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_1, SPACE_2),
        )
        self.trend_preview_plot = Sparkline(
            card, width=360, height=56,
            background=COLORS.surface, color=COLORS.accent,
        )
        self.trend_preview_plot.grid(
            row=3, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_4),
        )
        self.trend_preview_plot.grid_remove()
        return card

    def _select_status_recent(self, index: int) -> None:
        if index >= len(self.status_recent_rows):
            return
        thread_id = self.status_recent_rows[index].get("thread_id")
        if not isinstance(thread_id, str):
            return
        self.status_filter = "all"
        if hasattr(self, "status_filter_menu"):
            self.status_filter_menu.set(translate("filter_all", self.language))
        presentation = getattr(self, "presentation", None)
        if presentation is not None:
            for row_index, row in enumerate(presentation.recent_sessions):
                if row.thread_id == thread_id:
                    self.current_page = row_index // self.page_size + 1
                    break
        snapshot = self.view_model.select_cached_thread(thread_id)
        if snapshot is not None:
            self._apply_cached_snapshot(snapshot)

    def _apply_status_layout(self, content_width: int) -> None:
        cards = (
            self.status_advice_card, self.core_metrics_panel,
            self.observed_usage_card,
            self.task_summary_card, self.quota_center_card,
            self.trend_preview_card, self.status_recent_card,
            self.quick_actions_card,
        )
        for card in cards:
            card.grid_forget()
            card.grid_propagate(True)
        mode = dashboard_layout_for_width(content_width)
        for column in range(6):
            self.status_page.grid_columnconfigure(
                column, weight=0, uniform="", minsize=0,
            )
        if mode in {"wide", "medium"}:
            for column in (0, 1):
                self.status_page.grid_columnconfigure(
                    column, weight=1, uniform="dashboard",
                )
            self.status_advice_card.grid(
                row=0, column=0, columnspan=2, sticky="ew", pady=(0, SPACE_3),
            )
            self.core_metrics_panel.grid(
                row=1, column=0, columnspan=2, sticky="ew", pady=(0, SPACE_3),
            )
            self.observed_usage_card.grid(
                row=2, column=0, columnspan=2, sticky="ew", pady=(0, SPACE_3),
            )
            for card, row, column in (
                (self.task_summary_card, 3, 0),
                (self.quota_center_card, 3, 1),
                (self.trend_preview_card, 4, 0),
                (self.status_recent_card, 4, 1),
            ):
                card.grid(
                    row=row, column=column, sticky="nsew",
                    padx=(0 if column == 0 else SPACE_2, 0),
                    pady=(0, SPACE_3),
                )
            self.quick_actions_card.grid(
                row=5, column=0, columnspan=2, sticky="ew", pady=(0, SPACE_3),
            )
        else:
            self.status_page.grid_columnconfigure(0, weight=1, uniform="")
            for row, card in enumerate(cards):
                card.grid(row=row, column=0, sticky="ew", pady=(0, SPACE_3))
        self._layout_core_metrics(content_width)
        self._layout_observed_usage(content_width)

    def _layout_core_metrics(self, width: int) -> None:
        columns = metric_columns_for_width(width)
        for column in range(6):
            self.core_cards_frame.grid_columnconfigure(
                column, weight=1 if column < columns else 0,
                uniform="core_metric" if column < columns else "",
            )
        for index, widget in enumerate(self.core_metric_widgets):
            card = widget["card"]
            card.grid_forget()
            row, column = divmod(index, columns)
            card.grid(
                row=row, column=column, sticky="nsew", padx=SPACE_1,
                pady=SPACE_1,
            )

    def _layout_observed_usage(self, width: int) -> None:
        metric_columns = min(5, metric_columns_for_width(width))
        for column in range(5):
            self.observed_usage_metrics_host.grid_columnconfigure(
                column,
                weight=1 if column < metric_columns else 0,
                uniform="observed_usage" if column < metric_columns else "",
            )
        for index, widget in enumerate(self.observed_usage_metric_widgets.values()):
            card = widget["card"]
            card.grid_forget()
            row, column = divmod(index, metric_columns)
            card.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=SPACE_1,
                pady=SPACE_1,
            )
        aux_columns = 4 if width >= 900 else 2
        for column in range(4):
            self.observed_usage_aux_host.grid_columnconfigure(
                column,
                weight=1 if column < aux_columns else 0,
                uniform="observed_usage_aux" if column < aux_columns else "",
            )
        for index, widget in enumerate(self.observed_usage_aux_widgets.values()):
            cell = widget["cell"]
            cell.grid_forget()
            row, column = divmod(index, aux_columns)
            cell.grid(
                row=row,
                column=column,
                sticky="nsew",
                padx=SPACE_1,
                pady=SPACE_1,
            )

    def _build_current_task_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        self.task_back_button = ctk.CTkButton(
            page, text="", command=lambda: self.show_page("overview"),
            width=142, height=32, fg_color="transparent", border_width=1,
            border_color=COLORS.border, text_color=COLORS.primary_text,
            hover_color=COLORS.accent_soft,
        )
        self.task_back_button.grid(row=0, column=0, sticky="w", pady=(0, SPACE_2))
        self.task_detail_vars = {
            name: tk.StringVar(master=self.root, value="—")
            for name in (
                "title", "status", "activity", "turns", "input", "output",
                "total", "cached", "reasoning", "cache", "session",
                "quota_five", "quota_weekly", "advice",
            )
        }
        overview = self._section_card(page)
        overview.grid(row=1, column=0, sticky="ew")
        overview.grid_columnconfigure(1, weight=1)
        self.task_detail_labels = {}
        for row, name in enumerate(("title", "status", "activity", "turns")):
            label = ctk.CTkLabel(
                overview, text="", font=CAPTION,
                text_color=COLORS.secondary_text, anchor="w",
            )
            label.grid(
                row=row, column=0, sticky="w", padx=SPACE_4,
                pady=(SPACE_3 if row == 0 else SPACE_1),
            )
            self.task_detail_labels[name] = label
            ctk.CTkLabel(
                overview, textvariable=self.task_detail_vars[name],
                font=BODY_STRONG if name in {"title", "status"} else BODY,
                text_color=COLORS.primary_text, anchor="w", justify="left",
                wraplength=820,
            ).grid(
                row=row, column=1, sticky="ew", padx=SPACE_4,
                pady=(SPACE_3 if row == 0 else SPACE_1),
            )
        metrics = ctk.CTkFrame(page, fg_color="transparent")
        metrics.grid(row=2, column=0, sticky="ew", pady=(SPACE_3, 0))
        metric_names = ("input", "output", "total", "cached", "reasoning", "cache")
        for index, name in enumerate(metric_names):
            row, column = divmod(index, 3)
            metrics.grid_columnconfigure(column, weight=1, uniform="detail_metric")
            cell = self._section_card(metrics)
            cell.grid(
                row=row, column=column, sticky="nsew",
                padx=(0 if column == 0 else SPACE_2, 0), pady=(0, SPACE_2),
            )
            label = ctk.CTkLabel(
                cell, text="", font=CAPTION, text_color=COLORS.secondary_text,
                anchor="w",
            )
            label.grid(row=0, column=0, sticky="ew", padx=SPACE_3, pady=(SPACE_2, 0))
            self.task_detail_labels[name] = label
            ctk.CTkLabel(
                cell, textvariable=self.task_detail_vars[name], font=METRIC,
                text_color=COLORS.purple if name != "cache" else COLORS.real,
                anchor="w",
            ).grid(row=1, column=0, sticky="ew", padx=SPACE_3, pady=(0, SPACE_2))
        context = self._section_card(page)
        context.grid(row=3, column=0, sticky="ew", pady=(SPACE_1, 0))
        context.grid_columnconfigure(1, weight=1)
        for row, name in enumerate(("session", "quota_five", "quota_weekly", "advice")):
            label = ctk.CTkLabel(
                context, text="", font=CAPTION,
                text_color=COLORS.secondary_text, anchor="w",
            )
            label.grid(row=row, column=0, sticky="w", padx=SPACE_4, pady=SPACE_2)
            self.task_detail_labels[name] = label
            ctk.CTkLabel(
                context, textvariable=self.task_detail_vars[name],
                font=BODY_STRONG if name in {"session", "advice"} else BODY,
                text_color=COLORS.primary_text, anchor="w", justify="left",
                wraplength=760,
            ).grid(row=row, column=1, sticky="ew", padx=SPACE_4, pady=SPACE_2)
        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="ew", pady=SPACE_3)
        self.task_refresh_button = ctk.CTkButton(actions, text="", command=self.manual_refresh)
        self.task_switch_button = ctk.CTkButton(actions, text="", command=lambda: self.show_page("sessions"), fg_color="transparent", border_width=1, border_color=COLORS.border, text_color=COLORS.primary_text)
        self.task_new_thread_button = ctk.CTkButton(actions, text="", command=self._show_new_thread_dialog, fg_color=COLORS.orange, hover_color=COLORS.estimate)
        for column, button in enumerate((self.task_refresh_button, self.task_switch_button, self.task_new_thread_button)):
            button.grid(row=0, column=column, padx=(0, SPACE_2))

    def _build_history_page(self, parent: ctk.CTkFrame) -> None:
        page = self.sessions_page = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        selector = self.history_selector = ctk.CTkFrame(page, fg_color="transparent")
        selector.grid(row=0, column=0, sticky="ew", pady=(0, SPACE_2))
        self.task_selector_label = ctk.CTkLabel(selector, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.task_selector_label.grid(row=0, column=0, padx=(0, SPACE_2))
        self.task_menu = ctk.CTkOptionMenu(selector, values=["—"], command=self._select_task, variable=self.task_label_var, width=360)
        self.task_menu.grid(row=0, column=1, sticky="w")
        self.session_search_label = ctk.CTkLabel(
            selector, text="", font=FONT_SMALL, text_color=COLORS.secondary_text,
        )
        self.session_search_var = tk.StringVar(master=self.root, value="")
        self.session_search_entry = ctk.CTkEntry(
            selector, textvariable=self.session_search_var, width=210, height=32,
        )
        self.session_search_entry.bind("<KeyRelease>", self._change_session_search)
        self.range_selector_label = ctk.CTkLabel(selector, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.range_selector_label.grid(row=0, column=2, padx=(SPACE_3, SPACE_2))
        self.range_menu = ctk.CTkOptionMenu(selector, values=["—"], command=self._change_time_range, width=130)
        self.range_menu.grid(row=0, column=3, sticky="w")
        self.status_filter_label = ctk.CTkLabel(
            selector, text="", font=FONT_SMALL, text_color=COLORS.secondary_text,
        )
        self.status_filter_label.grid(row=0, column=4, padx=(SPACE_3, SPACE_2))
        self.status_filter_menu = ctk.CTkOptionMenu(
            selector, values=["—"], command=self._change_status_filter, width=140,
        )
        self.status_filter_menu.grid(row=0, column=5, sticky="w")
        self.history_detail_button = ctk.CTkButton(
            selector, text="", command=lambda: self.show_page("session_detail"),
            width=100, height=30, fg_color="transparent", border_width=1,
            border_color=COLORS.border, text_color=COLORS.primary_text,
        )
        self.history_detail_button.grid(row=0, column=6, padx=(SPACE_3, 0))
        self._build_recent_sessions(page, row=1)
        self.sessions_side_panel = self._section_card(page)
        self.sessions_side_panel.grid(row=1, column=1, sticky="nsew", padx=(SPACE_3, 0), pady=(SPACE_2, 0))
        self.sessions_side_panel.grid_columnconfigure(0, weight=1)
        self.sessions_side_title = ctk.CTkLabel(
            self.sessions_side_panel, text="", font=SECTION_TITLE,
            text_color=COLORS.primary_text, anchor="w",
        )
        self.sessions_side_title.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
        self.sessions_side_labels = {}
        for row_index, name in enumerate(("title", "status", "activity", "turns", "session", "cache"), start=1):
            label = ctk.CTkLabel(
                self.sessions_side_panel, text="", font=CAPTION,
                text_color=COLORS.secondary_text, anchor="w",
            )
            label.grid(row=row_index * 2 - 1, column=0, sticky="ew", padx=SPACE_4)
            self.sessions_side_labels[name] = label
            ctk.CTkLabel(
                self.sessions_side_panel, textvariable=self.task_detail_vars[name],
                font=BODY_STRONG if name in {"title", "status"} else BODY,
                text_color=COLORS.primary_text, anchor="w", justify="left",
                wraplength=280,
            ).grid(row=row_index * 2, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_2))
        self.sessions_side_open = ctk.CTkButton(
            self.sessions_side_panel, text="", command=lambda: self.show_page("session_detail"),
            height=32,
        )
        self.sessions_side_open.grid(row=13, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_2, SPACE_4))
        self._layout_history_controls(1000)
        self._layout_sessions_page(1000)

    def _layout_history_controls(self, content_width: int) -> None:
        """Keep history filters usable without a horizontal scrollbar."""
        controls = (
            self.task_selector_label, self.task_menu,
            self.session_search_label, self.session_search_entry,
            self.range_selector_label, self.range_menu,
            self.status_filter_label, self.status_filter_menu,
            self.history_detail_button,
        )
        for control in controls:
            control.grid_forget()
        selector = self.history_selector
        for column in range(9):
            selector.grid_columnconfigure(column, weight=0, uniform="")
        if content_width >= 1150:
            self.task_selector_label.grid(row=0, column=0, padx=(0, SPACE_2))
            self.task_menu.configure(width=250)
            self.task_menu.grid(row=0, column=1, sticky="w")
            self.session_search_label.grid(row=0, column=2, padx=(SPACE_3, SPACE_2))
            self.session_search_entry.grid(row=0, column=3, sticky="w")
            self.range_selector_label.grid(row=0, column=4, padx=(SPACE_3, SPACE_2))
            self.range_menu.grid(row=0, column=5, sticky="w")
            self.status_filter_label.grid(row=0, column=6, padx=(SPACE_3, SPACE_2))
            self.status_filter_menu.grid(row=0, column=7, sticky="w")
            self.history_detail_button.grid(row=0, column=8, padx=(SPACE_3, 0))
        else:
            selector.grid_columnconfigure(1, weight=1)
            selector.grid_columnconfigure(3, weight=1)
            self.session_search_label.grid(row=0, column=0, padx=(0, SPACE_2), pady=(0, SPACE_2))
            self.session_search_entry.grid(row=0, column=1, sticky="ew", pady=(0, SPACE_2))
            self.task_selector_label.grid(row=0, column=2, padx=(SPACE_3, SPACE_2), pady=(0, SPACE_2))
            self.task_menu.configure(width=280)
            self.task_menu.grid(row=0, column=3, sticky="ew", pady=(0, SPACE_2))
            self.history_detail_button.grid(
                row=0, column=4, padx=(SPACE_3, 0), pady=(0, SPACE_2),
            )
            self.range_selector_label.grid(row=1, column=0, padx=(0, SPACE_2))
            self.range_menu.grid(row=1, column=1, sticky="w")
            self.status_filter_label.grid(row=1, column=2, padx=(SPACE_3, SPACE_2))
            self.status_filter_menu.grid(row=1, column=3, sticky="w")

    def _layout_sessions_page(self, content_width: int) -> None:
        show_detail = content_width >= 1_000
        self.sessions_page.grid_columnconfigure(0, weight=3)
        self.sessions_page.grid_columnconfigure(1, weight=1 if show_detail else 0)
        if show_detail:
            self.sessions_side_panel.grid()
        else:
            self.sessions_side_panel.grid_remove()

    def _layout_history_columns(self, content_width: int) -> None:
        available = max(560, content_width - 58)
        ratios = (0.34, 0.12, 0.16, 0.10, 0.14, 0.14)
        for column, ratio in zip(SESSION_COLUMNS, ratios):
            self.sessions_tree.column(
                column, width=max(76, int(available * ratio)),
                minwidth=70,
                anchor="e" if column in {"Tokens", "Cache"} else "w",
            )
        self.sessions_tree.configure(
            displaycolumns=(
                ("Name", "Status", "Activity", "Tokens")
                if content_width < 900 else SESSION_COLUMNS
            )
        )

    def _build_usage_trends_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)

        controls = self._section_card(page)
        controls.grid(row=0, column=0, sticky="ew", pady=(0, SPACE_3))
        controls.grid_columnconfigure(0, weight=1)
        self.trend_page_description = ctk.CTkLabel(
            controls, text="", font=BODY, text_color=COLORS.secondary_text,
            anchor="w", justify="left", wraplength=720,
        )
        self.trend_page_description.grid(
            row=0, column=0, sticky="ew", padx=SPACE_4, pady=SPACE_3,
        )
        self.trend_range_menu = ctk.CTkOptionMenu(
            controls, values=["—"], command=self._change_trend_range,
            width=148, height=34,
        )
        self.trend_range_menu.grid(row=0, column=1, padx=SPACE_4, pady=SPACE_3)
        selectors = ctk.CTkFrame(controls, fg_color="transparent")
        selectors.grid(
            row=1, column=0, columnspan=2, sticky="ew",
            padx=SPACE_4, pady=(0, SPACE_3),
        )
        selectors.grid_columnconfigure((1, 3), weight=1)
        self.trend_group_label = ctk.CTkLabel(
            selectors, text="", font=CAPTION, text_color=COLORS.secondary_text,
        )
        self.trend_group_label.grid(row=0, column=0, padx=(0, SPACE_2))
        self.trend_group_menu = ctk.CTkOptionMenu(
            selectors, values=["—"], command=self._change_trend_group,
            width=190, height=34,
        )
        self.trend_group_menu.grid(row=0, column=1, sticky="w", padx=(0, SPACE_4))
        self.trend_metric_label = ctk.CTkLabel(
            selectors, text="", font=CAPTION, text_color=COLORS.secondary_text,
        )
        self.trend_metric_label.grid(row=0, column=2, padx=(0, SPACE_2))
        self.trend_metric_menu = ctk.CTkOptionMenu(
            selectors, values=["—"], command=self._change_trend_metric,
            width=190, height=34,
        )
        self.trend_metric_menu.grid(row=0, column=3, sticky="w")
        self.trend_scope_var = tk.StringVar(master=self.root, value="")
        self.trend_scope_label = ctk.CTkLabel(
            selectors, textvariable=self.trend_scope_var, font=CAPTION,
            text_color=COLORS.accent, fg_color=COLORS.accent_soft,
            corner_radius=8, padx=8,
        )
        self.trend_scope_label.grid(row=0, column=4, padx=(SPACE_3, 0))
        self.trend_group_labels: dict[str, str] = {}
        self.trend_metric_labels: dict[str, str] = {}

        summary = self._section_card(page)
        summary.grid(row=1, column=0, sticky="ew", pady=(0, SPACE_3))
        summary.grid_columnconfigure((0, 1, 2), weight=1, uniform="trend_summary")
        self.trend_summary_vars = {
            key: tk.StringVar(master=self.root, value="—")
            for key in ("range", "samples", "updated")
        }
        self.trend_summary_labels = {}
        for column, key in enumerate(("range", "samples", "updated")):
            cell = ctk.CTkFrame(summary, fg_color=COLORS.raised_surface, corner_radius=CONTROL_RADIUS)
            cell.grid(
                row=0, column=column, sticky="nsew", padx=SPACE_2,
                pady=SPACE_2,
            )
            label = ctk.CTkLabel(
                cell, text="", font=CAPTION, text_color=COLORS.secondary_text,
                anchor="w",
            )
            label.grid(row=0, column=0, sticky="ew", padx=SPACE_3, pady=(SPACE_2, 0))
            self.trend_summary_labels[key] = label
            ctk.CTkLabel(
                cell, textvariable=self.trend_summary_vars[key], font=BODY_STRONG,
                text_color=COLORS.primary_text, anchor="w",
            ).grid(row=1, column=0, sticky="ew", padx=SPACE_3, pady=(0, SPACE_2))

        quality = self._section_card(page)
        quality.grid(row=2, column=0, sticky="ew", pady=(0, SPACE_3))
        quality.grid_columnconfigure(0, weight=1)
        self.trend_quality_var = tk.StringVar(master=self.root, value="—")
        self.trend_quality_message_var = tk.StringVar(master=self.root, value="")
        self.trend_quality_label = ctk.CTkLabel(
            quality, textvariable=self.trend_quality_var, font=SECTION_TITLE,
            text_color=COLORS.unknown, anchor="w",
        )
        self.trend_quality_label.grid(
            row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_1),
        )
        ctk.CTkLabel(
            quality, textvariable=self.trend_quality_message_var, font=BODY,
            text_color=COLORS.secondary_text, anchor="w", justify="left",
            wraplength=760,
        ).grid(row=1, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_3))
        self.trend_chart = TrendCanvas(
            quality, width=760, height=190,
            background=COLORS.surface,
            foreground=COLORS.primary_text,
            grid_color=COLORS.border,
            series_colors={
                key: color for key, color in zip(
                    TREND_METRIC_LABEL_KEYS,
                    (
                        COLORS.accent, COLORS.real, COLORS.purple, COLORS.orange,
                        COLORS.teal, COLORS.purple, COLORS.accent, COLORS.orange,
                        COLORS.teal, COLORS.purple,
                    ),
                )
            },
            value_formatter=self._format_trend_tooltip_value,
        )
        self.trend_chart.grid(
            row=2, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_4),
        )
        self.trend_chart.grid_remove()

        metrics = self._section_card(page)
        metrics.grid(row=3, column=0, sticky="ew", pady=(0, SPACE_3))
        metrics.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="trend_metric")
        self.trend_metric_vars = {
            key: tk.StringVar(master=self.root, value="—")
            for key in ("current", "minimum", "maximum", "change")
        }
        self.trend_summary_metric_labels = {}
        self.trend_metric_cells = []
        for column, key in enumerate(("current", "minimum", "maximum", "change")):
            cell = ctk.CTkFrame(metrics, fg_color=COLORS.raised_surface, corner_radius=CONTROL_RADIUS)
            cell.grid(row=0, column=column, sticky="nsew", padx=SPACE_1, pady=SPACE_2)
            label = ctk.CTkLabel(cell, text="", font=CAPTION, text_color=COLORS.secondary_text)
            label.grid(row=0, column=0, padx=SPACE_2, pady=(SPACE_2, 0))
            self.trend_summary_metric_labels[key] = label
            self.trend_metric_cells.append(cell)
            ctk.CTkLabel(
                cell, textvariable=self.trend_metric_vars[key], font=METRIC,
                text_color=COLORS.primary_text,
            ).grid(row=1, column=0, padx=SPACE_2, pady=(0, SPACE_2))
        self.trend_metrics_host = metrics
        self._layout_trend_metrics(1000)

        self.usage_insights_card = self._build_usage_insights_card(page)
        self.usage_insights_card.grid(row=4, column=0, sticky="ew")

    def _build_usage_insights_card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        card = self._section_card(parent)
        card.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_1))
        header.grid_columnconfigure(0, weight=1)
        self.usage_insights_title_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(
            header,
            textvariable=self.usage_insights_title_var,
            font=SECTION_TITLE,
            text_color=COLORS.primary_text,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")
        self.usage_insights_range_var = tk.StringVar(master=self.root, value="")
        self.usage_insights_range_label = ctk.CTkLabel(
            header,
            textvariable=self.usage_insights_range_var,
            font=CAPTION,
            text_color=COLORS.accent,
            fg_color=COLORS.accent_soft,
            corner_radius=8,
            padx=8,
        )
        self.usage_insights_range_label.grid(row=0, column=1, padx=(SPACE_3, 0))
        self.usage_insights_state_var = tk.StringVar(master=self.root, value="")
        self.usage_insights_state_label = ctk.CTkLabel(
            card,
            textvariable=self.usage_insights_state_var,
            font=CAPTION,
            text_color=COLORS.secondary_text,
            anchor="w",
            justify="left",
        )
        self.usage_insights_state_label.grid(
            row=1, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_2),
        )

        self.usage_insights_sections: dict[str, dict[str, object]] = {}
        for section_row, key in enumerate(("threads", "responses", "cache"), start=2):
            group = ctk.CTkFrame(
                card,
                fg_color=COLORS.raised_surface,
                corner_radius=CONTROL_RADIUS,
                border_width=1,
                border_color=COLORS.border,
            )
            group.grid(
                row=section_row, column=0, sticky="ew",
                padx=SPACE_3, pady=(0, SPACE_2),
            )
            group.grid_columnconfigure(0, weight=1)
            section_header = ctk.CTkFrame(group, fg_color="transparent")
            section_header.grid(
                row=0, column=0, sticky="ew", padx=SPACE_3, pady=(SPACE_2, SPACE_1),
            )
            section_header.grid_columnconfigure(0, weight=1)
            title_var = tk.StringVar(master=self.root, value="")
            ctk.CTkLabel(
                section_header,
                textvariable=title_var,
                font=BODY_STRONG,
                text_color=COLORS.primary_text,
                anchor="w",
            ).grid(row=0, column=0, sticky="ew")
            toggle = ctk.CTkButton(
                section_header,
                text="",
                width=92,
                height=26,
                fg_color="transparent",
                hover_color=COLORS.accent_soft,
                text_color=COLORS.accent,
                font=CAPTION,
                command=lambda group_key=key: self._toggle_usage_insights_group(group_key),
            )
            toggle.grid(row=0, column=1, padx=(SPACE_2, 0))
            rows: list[dict[str, object]] = []
            for index in range(5 if key != "cache" else 3):
                row_frame = ctk.CTkFrame(group, fg_color="transparent")
                row_frame.grid(
                    row=index + 1, column=0, sticky="ew",
                    padx=SPACE_3, pady=(SPACE_1, SPACE_2),
                )
                row_frame.grid_columnconfigure(0, weight=1)
                title = tk.StringVar(master=self.root, value="")
                primary = tk.StringVar(master=self.root, value="")
                details = tk.StringVar(master=self.root, value="")
                coverage = tk.StringVar(master=self.root, value="")
                ctk.CTkLabel(
                    row_frame,
                    textvariable=title,
                    font=BODY_STRONG,
                    text_color=COLORS.primary_text,
                    anchor="w",
                ).grid(row=0, column=0, sticky="ew")
                ctk.CTkLabel(
                    row_frame,
                    textvariable=primary,
                    font=BODY_STRONG,
                    text_color=COLORS.accent,
                    anchor="e",
                ).grid(row=0, column=1, sticky="e", padx=(SPACE_3, 0))
                details_label = ctk.CTkLabel(
                    row_frame,
                    textvariable=details,
                    font=CAPTION,
                    text_color=COLORS.secondary_text,
                    anchor="w",
                    justify="left",
                )
                details_label.grid(row=1, column=0, sticky="ew", pady=(SPACE_1, 0))
                coverage_label = ctk.CTkLabel(
                    row_frame,
                    textvariable=coverage,
                    font=CAPTION,
                    text_color=COLORS.real,
                    anchor="e",
                )
                coverage_label.grid(
                    row=1, column=1, sticky="e", padx=(SPACE_3, 0), pady=(SPACE_1, 0),
                )
                rows.append({
                    "frame": row_frame,
                    "title": title,
                    "primary": primary,
                    "details": details,
                    "details_label": details_label,
                    "coverage": coverage,
                    "coverage_label": coverage_label,
                })
            self.usage_insights_sections[key] = {
                "frame": group,
                "title": title_var,
                "toggle": toggle,
                "rows": rows,
            }
        return card

    def _layout_trend_metrics(self, content_width: int) -> None:
        columns = 4 if content_width >= 1_000 else 2
        for column in range(4):
            self.trend_metrics_host.grid_columnconfigure(
                column, weight=1 if column < columns else 0,
                uniform="trend_metric" if column < columns else "",
            )
        for index, cell in enumerate(self.trend_metric_cells):
            cell.grid_forget()
            row, column = divmod(index, columns)
            cell.grid(row=row, column=column, sticky="nsew", padx=SPACE_1, pady=SPACE_2)

    def _build_recommendations_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        self.recommendations_description = ctk.CTkLabel(
            page, text="", font=BODY, text_color=COLORS.secondary_text,
            anchor="w", justify="left", wraplength=780,
        )
        self.recommendations_description.grid(row=0, column=0, sticky="ew", pady=(0, SPACE_2))
        self.recommendation_cards: list[dict[str, object]] = []
        for index in range(5):
            card = self._section_card(page)
            card.grid(row=index + 1, column=0, sticky="ew", pady=(0, SPACE_3))
            card.grid_columnconfigure(0, weight=1)
            title_var = tk.StringVar(master=self.root, value="")
            severity_var = tk.StringVar(master=self.root, value="")
            body_var = tk.StringVar(master=self.root, value="")
            evidence_var = tk.StringVar(master=self.root, value="")
            metadata_var = tk.StringVar(master=self.root, value="")
            history_var = tk.StringVar(master=self.root, value="")
            observed_var = tk.StringVar(master=self.root, value="")
            ctk.CTkLabel(
                card, textvariable=title_var,
                font=STATUS_TITLE if index == 0 else SECTION_TITLE,
                text_color=COLORS.primary_text, anchor="w",
            ).grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_1))
            severity = ctk.CTkLabel(
                card, textvariable=severity_var, font=CAPTION,
                text_color=COLORS.unknown, fg_color=COLORS.unknown_soft,
                corner_radius=8, padx=8,
            )
            severity.grid(row=0, column=1, padx=SPACE_4, pady=(SPACE_3, SPACE_1))
            ctk.CTkLabel(
                card, textvariable=body_var, font=BODY,
                text_color=COLORS.secondary_text, anchor="w", justify="left",
                wraplength=760,
            ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=SPACE_4, pady=SPACE_1)
            ctk.CTkLabel(
                card, textvariable=evidence_var, font=BODY_STRONG,
                text_color=COLORS.primary_text, anchor="w", justify="left",
                wraplength=760,
            ).grid(row=2, column=0, columnspan=2, sticky="ew", padx=SPACE_4, pady=SPACE_1)
            ctk.CTkLabel(
                card, textvariable=metadata_var, font=CAPTION,
                text_color=COLORS.secondary_text, anchor="w", justify="left",
                wraplength=760,
            ).grid(row=3, column=0, columnspan=2, sticky="ew", padx=SPACE_4, pady=SPACE_1)
            ctk.CTkLabel(
                card, textvariable=history_var, font=BODY,
                text_color=COLORS.secondary_text, anchor="w", justify="left",
                wraplength=760,
            ).grid(row=4, column=0, columnspan=2, sticky="ew", padx=SPACE_4, pady=SPACE_1)
            ctk.CTkLabel(
                card, textvariable=observed_var, font=CAPTION,
                text_color=COLORS.muted_text, anchor="w",
            ).grid(row=5, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_1, SPACE_3))
            action = ctk.CTkButton(
                card, text="", command=lambda item=index: self._execute_recommendation(item),
                width=138, height=32,
            )
            action.grid(row=5, column=1, padx=SPACE_4, pady=(SPACE_1, SPACE_3))
            self.recommendation_cards.append({
                "card": card, "title": title_var, "severity": severity_var,
                "severity_label": severity, "body": body_var,
                "evidence": evidence_var, "metadata": metadata_var,
                "history": history_var, "observed": observed_var,
                "action": action, "recommendation": None,
            })
        self.recommendations_rules_button = ctk.CTkButton(
            page, text="", command=self._show_advisor_rules,
            fg_color="transparent", border_width=1, border_color=COLORS.border,
            text_color=COLORS.primary_text, width=150,
        )
        self.recommendations_rules_button.grid(row=6, column=0, sticky="w", pady=(0, SPACE_3))

    def _build_tools_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        self.tools_page = page
        page.grid_columnconfigure((0, 1), weight=1, uniform="tool_group")
        self.tool_group_cards = []
        self.tool_group_titles = {}

        def group(key: str) -> ctk.CTkFrame:
            card = self._section_card(page)
            card.grid_columnconfigure(0, weight=1)
            title = ctk.CTkLabel(
                card, text="", font=SECTION_TITLE,
                text_color=COLORS.primary_text, anchor="w",
            )
            title.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
            self.tool_group_cards.append(card)
            self.tool_group_titles[key] = title
            return card

        diagnostic = group("diagnostics")
        self.diagnostic_title = self.tool_group_titles["diagnostics"]
        self.diagnostic_summary_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(
            diagnostic, textvariable=self.diagnostic_summary_var, font=BODY_STRONG,
            text_color=COLORS.primary_text, anchor="w", justify="left",
            wraplength=420,
        ).grid(row=1, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_2))
        self.diagnostic_run_button = ctk.CTkButton(
            diagnostic, text="", command=self.start_diagnostics, height=34,
        )
        self.diagnostic_view_button = ctk.CTkButton(
            diagnostic, text="", command=self._show_diagnostic_dialog,
            state="disabled", height=34, fg_color="transparent",
            border_width=1, border_color=COLORS.border,
            text_color=COLORS.primary_text,
        )
        self.diagnostic_export_button = ctk.CTkButton(
            diagnostic, text="", command=lambda: None, state="disabled", height=34,
            fg_color="transparent", border_width=1, border_color=COLORS.border,
            text_color=COLORS.muted_text,
        )
        for row, button in enumerate((
            self.diagnostic_run_button, self.diagnostic_view_button,
            self.diagnostic_export_button,
        ), start=2):
            button.grid(row=row, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_2 if row < 4 else SPACE_4))

        data = group("data")
        self.tool_open_data = ctk.CTkButton(data, text="", command=self._open_data_directory)
        self.tool_backup = ctk.CTkButton(data, text="", command=lambda: None, state="disabled")
        self.tool_restore = ctk.CTkButton(data, text="", command=lambda: None, state="disabled")
        self.tool_cache_cleanup = ctk.CTkButton(data, text="", command=lambda: None, state="disabled")
        for row, button in enumerate((
            self.tool_open_data, self.tool_backup, self.tool_restore,
            self.tool_cache_cleanup,
        ), start=1):
            button.grid(row=row, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_2 if row < 4 else SPACE_4))

        workflow = group("workflow")
        self.tool_open_codex = ctk.CTkButton(workflow, text="", command=self._open_codex)
        self.tool_new_thread = ctk.CTkButton(workflow, text="", command=self._show_new_thread_dialog)
        self.tool_redetect = ctk.CTkButton(workflow, text="", command=self.manual_refresh)
        for row, button in enumerate((self.tool_open_codex, self.tool_new_thread, self.tool_redetect), start=1):
            button.grid(row=row, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_2 if row < 3 else SPACE_4))

        help_card = group("help")
        self.tool_guide = ctk.CTkButton(help_card, text="", command=self._show_user_guide)
        self.tool_privacy = ctk.CTkButton(help_card, text="", command=self._show_privacy_boundary)
        self.tool_update = ctk.CTkButton(help_card, text="", command=lambda: None, state="disabled")
        self.tool_about = ctk.CTkButton(help_card, text="", command=self._show_about)
        for row, button in enumerate((self.tool_guide, self.tool_privacy, self.tool_update, self.tool_about), start=1):
            button.grid(row=row, column=0, sticky="ew", padx=SPACE_4, pady=(0, SPACE_2 if row < 4 else SPACE_4))

        self.coming_soon_buttons = (
            self.diagnostic_export_button, self.tool_backup,
            self.tool_restore, self.tool_cache_cleanup, self.tool_update,
        )
        self._layout_tool_groups(1000)

    def _layout_tool_groups(self, content_width: int) -> None:
        columns = 2 if content_width >= 900 else 1
        for column in range(2):
            self.tools_page.grid_columnconfigure(
                column, weight=1 if column < columns else 0,
                uniform="tool_group" if column < columns else "",
            )
        for index, card in enumerate(self.tool_group_cards):
            card.grid_forget()
            row, column = divmod(index, columns)
            card.grid(
                row=row, column=column, sticky="nsew",
                padx=(0 if column == 0 else SPACE_3, 0), pady=(0, SPACE_3),
            )

    def _build_settings_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        self.settings_page = page
        page.grid_columnconfigure((0, 1), weight=1, uniform="settings_group")
        self.settings_labels: dict[str, ctk.CTkLabel] = {}
        self.settings_group_cards = []
        self.settings_group_titles = {}

        def group(key: str) -> ctk.CTkFrame:
            card = self._section_card(page)
            card.grid_columnconfigure(1, weight=1)
            title = ctk.CTkLabel(
                card, text="", font=SECTION_TITLE,
                text_color=COLORS.primary_text, anchor="w",
            )
            title.grid(row=0, column=0, columnspan=2, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
            self.settings_group_cards.append(card)
            self.settings_group_titles[key] = title
            return card

        def field(card: ctk.CTkFrame, row: int, name: str, control: object) -> None:
            label = ctk.CTkLabel(
                card, text="", font=BODY, text_color=COLORS.primary_text,
                anchor="w", justify="left", wraplength=220,
            )
            label.grid(row=row, column=0, sticky="w", padx=SPACE_4, pady=SPACE_2)
            self.settings_labels[name] = label
            control.grid(row=row, column=1, sticky="e", padx=SPACE_4, pady=SPACE_2)

        general = group("general")
        self.settings_language_menu = ctk.CTkOptionMenu(general, values=list(LANGUAGE_LABELS.values()), command=self._change_language, width=220)
        self.settings_startup_menu = ctk.CTkOptionMenu(general, values=["—"], command=self._settings_startup_changed, width=220)
        self.settings_exit_menu = ctk.CTkOptionMenu(general, values=["—"], command=self._settings_exit_changed, width=220)
        field(general, 1, "language", self.settings_language_menu)
        field(general, 2, "startup_mode", self.settings_startup_menu)
        field(general, 3, "exit_behavior", self.settings_exit_menu)

        refresh = group("refresh")
        self.settings_auto_switch = ctk.CTkSwitch(refresh, text="", variable=self.auto_refresh_var, command=self._toggle_auto_refresh)
        self.settings_refresh_interval_value = ctk.CTkLabel(refresh, text="", font=BODY_STRONG, text_color=COLORS.primary_text)
        self.settings_stale_value = ctk.CTkLabel(refresh, text="", font=BODY_STRONG, text_color=COLORS.stale)
        field(refresh, 1, "auto_refresh", self.settings_auto_switch)
        field(refresh, 2, "refresh_interval", self.settings_refresh_interval_value)
        field(refresh, 3, "stale_status", self.settings_stale_value)

        windows = group("windows")
        self.settings_startup_var = tk.BooleanVar(master=self.root, value=self.startup_adapter.is_enabled(sys.executable))
        self.settings_startup_switch = ctk.CTkSwitch(windows, text="", variable=self.settings_startup_var, command=self._settings_windows_startup_changed)
        self.settings_tray_value = ctk.CTkLabel(windows, text="", font=BODY_STRONG, text_color=COLORS.real)
        self.settings_taskbar_value = ctk.CTkLabel(windows, text="", font=BODY_STRONG, text_color=COLORS.primary_text)
        field(windows, 1, "start_with_windows", self.settings_startup_switch)
        field(windows, 2, "tray", self.settings_tray_value)
        field(windows, 3, "taskbar", self.settings_taskbar_value)
        if not self.startup_adapter.is_supported():
            self.settings_startup_switch.configure(state="disabled")

        widget = group("widget")
        self.settings_widget_menu = ctk.CTkOptionMenu(widget, values=["—"], command=self._settings_widget_changed, width=220)
        self.settings_opacity_var = tk.DoubleVar(master=self.root, value=load_widget_idle_opacity(UI_SETTINGS_PATH))
        opacity = ctk.CTkFrame(widget, fg_color="transparent")
        self.settings_opacity_value = ctk.CTkLabel(opacity, text="", width=50)
        self.settings_opacity_value.grid(row=0, column=0, padx=(0, SPACE_2))
        ctk.CTkSlider(opacity, from_=0.30, to=0.95, number_of_steps=13, variable=self.settings_opacity_var, command=self._settings_opacity_changed, width=180).grid(row=0, column=1)
        self.settings_topmost_value = ctk.CTkLabel(widget, text="", font=BODY_STRONG, text_color=COLORS.real)
        self.settings_position_value = ctk.CTkLabel(widget, text="", font=BODY_STRONG, text_color=COLORS.real)
        field(widget, 1, "widget_mode", self.settings_widget_menu)
        field(widget, 2, "widget_idle_opacity", opacity)
        field(widget, 3, "always_on_top", self.settings_topmost_value)
        field(widget, 4, "remember_position", self.settings_position_value)

        privacy = group("privacy")
        self.settings_privacy_button = ctk.CTkButton(privacy, text="", command=self._show_privacy_boundary, width=180)
        self.settings_version_value = ctk.CTkLabel(privacy, text=f"0.1.0", font=BODY_STRONG, text_color=COLORS.primary_text)
        self.settings_update_button = ctk.CTkButton(privacy, text="", command=lambda: None, state="disabled", width=180)
        field(privacy, 1, "privacy", self.settings_privacy_button)
        field(privacy, 2, "version", self.settings_version_value)
        field(privacy, 3, "updates", self.settings_update_button)
        self.settings_note_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(
            privacy, textvariable=self.settings_note_var, font=FONT_SMALL,
            text_color=COLORS.secondary_text, anchor="w", justify="left",
            wraplength=420,
        ).grid(row=4, column=0, columnspan=2, sticky="ew", padx=SPACE_4, pady=(SPACE_2, SPACE_4))
        self._layout_settings_groups(1000)

    def _layout_settings_groups(self, content_width: int) -> None:
        columns = 2 if content_width >= 900 else 1
        for column in range(2):
            self.settings_page.grid_columnconfigure(
                column, weight=1 if column < columns else 0,
                uniform="settings_group" if column < columns else "",
            )
        for index, card in enumerate(self.settings_group_cards):
            card.grid_forget()
            row, column = divmod(index, columns)
            card.grid(
                row=row, column=column, sticky="nsew",
                padx=(0 if column == 0 else SPACE_3, 0), pady=(0, SPACE_3),
            )

    def _build_recent_sessions(self, parent: ctk.CTkFrame, row: int = 5) -> None:
        panel = self.sessions_panel = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        panel.grid(row=row, column=0, sticky="nsew", pady=(SPACE_2, 0))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)
        self.recent_title = ctk.CTkLabel(panel, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.recent_title.grid(row=0, column=0, sticky="ew", padx=SPACE_3, pady=(SPACE_2, 0))
        self.recent_note = ctk.CTkLabel(panel, text="", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
        self.recent_note.grid(row=1, column=0, sticky="ew", padx=SPACE_3, pady=(0, SPACE_1))
        frame = ctk.CTkFrame(panel, fg_color=COLORS.surface)
        frame.grid(row=2, column=0, sticky="nsew", padx=SPACE_3, pady=(0, SPACE_3))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        self.sessions_tree = ttk.Treeview(frame, columns=SESSION_COLUMNS, show="headings", selectmode="browse", style="Monitor.Treeview", height=5)
        widths = (360, 100, 145, 70, 120, 120)
        for column, width in zip(SESSION_COLUMNS, widths):
            self.sessions_tree.heading(column, text=column)
            self.sessions_tree.column(column, width=width, minwidth=80, anchor="e" if column in {"Tokens", "Cache"} else "w")
        self.sessions_tree.bind("<<TreeviewSelect>>", self._select_recent_row)
        self.sessions_tree.bind(
            "<Double-Button-1>", lambda _event: self.show_page("session_detail"),
        )
        scrollbar = ctk.CTkScrollbar(frame, command=self.sessions_tree.yview, width=12)
        self.sessions_tree.configure(yscrollcommand=scrollbar.set)
        self.sessions_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(SPACE_1, 0))
        pager = ctk.CTkFrame(panel, fg_color="transparent")
        pager.grid(row=3, column=0, sticky="ew", padx=SPACE_3, pady=(0, SPACE_2))
        pager.grid_columnconfigure(1, weight=1)
        self.previous_page_button = ctk.CTkButton(pager, text="", width=72, command=self._previous_page)
        self.previous_page_button.grid(row=0, column=0)
        self.page_status_var = tk.StringVar(value="")
        ctk.CTkLabel(pager, textvariable=self.page_status_var, text_color=COLORS.secondary_text).grid(row=0, column=1)
        self.next_page_button = ctk.CTkButton(pager, text="", width=72, command=self._next_page)
        self.next_page_button.grid(row=0, column=2)

    def _change_language(self, selected: str) -> None:
        self.language_controller.set_language(language_from_label(selected))


    def _apply_language(self, language: str) -> None:
        self.language = language
        if not hasattr(self, "refresh_button"):
            return
        self.auto_switch.configure(
            text=localize_auto_refresh(
                bool(self.auto_refresh_var.get()), language,
                DEFAULT_AUTO_REFRESH_SECONDS,
            )
        )
        self.language_menu.set(LANGUAGE_LABELS[language])
        self.settings_language_menu.set(LANGUAGE_LABELS[language])
        self._apply_sidebar_labels()
        self.nav_version_var.set(translate("app_version_value", language, version=__version__))
        self._update_page_title()

        self.status_section_title.configure(text=translate("status_advice_title", language))
        self.core_metrics_title.configure(text=translate("core_metrics_title", language))
        self.reason_button.configure(text=translate("view_reason", language))
        for widget in self.core_metric_widgets:
            semantic = widget["semantic"]
            widget["title"].set(translate(f"core_metric_{semantic}", language))
            widget["scope"].set(translate(f"core_metric_{semantic}_scope", language))

        self.observed_usage_title.configure(
            text=translate("observed_usage_title", language),
        )
        self.observed_usage_disclaimer.configure(
            text=translate("observed_usage_disclaimer", language),
        )
        self.usage_window_labels = {
            translate(label_key, language): kind
            for kind, label_key in USAGE_WINDOW_LABEL_KEYS.items()
        }
        self.observed_usage_window_menu.configure(
            values=list(self.usage_window_labels),
        )
        self.observed_usage_window_menu.set(
            translate(USAGE_WINDOW_LABEL_KEYS[self.usage_window_kind], language),
        )
        for name, key in {
            "total": "metric_total",
            "input": "metric_input",
            "output": "metric_output",
            "cached": "metric_cached",
            "reasoning": "metric_reasoning",
        }.items():
            self.observed_usage_metric_widgets[name]["title"].set(
                translate(key, language),
            )
        for name, key in {
            "responses": "observed_usage_responses",
            "sessions": "observed_usage_sessions",
            "average": "observed_usage_average",
            "cache_reuse": "observed_usage_cache_reuse",
        }.items():
            self.observed_usage_aux_widgets[name]["title"].set(
                translate(key, language),
            )

        self.simple_task_title.configure(text=translate("session_detail_title", language))
        for name, key in {
            "turns": "task_turns", "instruction": "instruction_usage",
            "session": "session_usage",
        }.items():
            self.simple_task_labels[name].configure(text=translate(key, language))
        self.task_switch_button_home.configure(text=translate("switch_task", language))
        self.task_detail_button_home.configure(text=translate("view_details", language))
        self.simple_quota_title.configure(text=translate("quota_center_title", language))
        self.quota_detail_button.configure(text=translate("view_details", language))
        self.quota_window_widgets["five"]["title"].set(translate("five_hour_limit", language))
        self.quota_window_widgets["week"]["title"].set(translate("weekly_limit", language))
        self.quick_title.configure(text=translate("quick_actions_title", language))
        for button, key in zip(self.quick_action_buttons, (
            "one_click_diagnostics", "open_codex", "prepare_new_thread", "more_tools",
        )):
            button.configure(text=translate(key, language))
        self.status_recent_title.configure(text=translate("recent_sessions", language))
        self.status_recent_all.configure(text=translate("nav_sessions", language))
        self.trend_preview_title.configure(text=translate("trend_preview_title", language))
        self.trend_preview_open.configure(text=translate("view_trends", language))

        task_detail_keys = {
            "title": "task_title", "status": "task_status",
            "activity": "recent_activity", "turns": "task_turns",
            "input": "metric_input", "output": "metric_output",
            "total": "metric_total", "cached": "metric_cached",
            "reasoning": "metric_reasoning", "cache": "cache_reuse",
            "session": "session_usage", "quota_five": "five_hour_limit",
            "quota_weekly": "weekly_limit", "advice": "current_advice",
        }
        for name, key in task_detail_keys.items():
            self.task_detail_labels[name].configure(text=translate(key, language))
        self.task_back_button.configure(text=translate("nav_sessions", language))
        self.task_refresh_button.configure(text=translate("manual_refresh", language))
        self.task_switch_button.configure(text=translate("switch_task", language))
        self.task_new_thread_button.configure(text=translate("prepare_new_thread", language))

        self.task_selector_label.configure(text=translate("monitored_task", language))
        self.session_search_label.configure(text=translate("search_sessions", language))
        self.session_search_entry.configure(
            placeholder_text=translate("search_sessions_placeholder", language),
        )
        self.range_selector_label.configure(text=translate("time_range", language))
        range_values = [translate(key, language) for key in (
            "last_7_days", "last_30_days", "last_90_days",
        )]
        self.range_menu.configure(values=range_values)
        self.range_menu.set(translate(f"last_{self.lookback_days}_days", language))
        self.status_filter_label.configure(text=translate("status_filter", language))
        self.history_detail_button.configure(text=translate("view_details", language))
        self._configure_history_filter_menu()
        self.recent_title.configure(text=translate("recent_sessions", language))
        self.recent_note.configure(text=self._recent_sessions_note())
        self.previous_page_button.configure(text=translate("previous_page", language))
        self.next_page_button.configure(text=translate("next_page", language))
        for column, key in zip(SESSION_COLUMNS, SESSION_COLUMN_KEYS):
            self.sessions_tree.heading(column, text=translate(key, language))
        self.sessions_side_title.configure(text=translate("session_detail_title", language))
        for name, key in {
            "title": "task_title", "status": "task_status",
            "activity": "recent_activity", "turns": "task_turns",
            "session": "session_usage", "cache": "cache_reuse",
        }.items():
            self.sessions_side_labels[name].configure(text=translate(key, language))
        self.sessions_side_open.configure(text=translate("view_details", language))

        self.trend_page_description.configure(text=translate("trend_page_description", language))
        trend_ranges = [translate(f"last_{days}_days", language) for days in (7, 30, 90)]
        self.trend_range_menu.configure(values=trend_ranges)
        self.trend_range_menu.set(translate(f"last_{self.trend_range_days}_days", language))
        self.trend_group_label.configure(text=translate("trend_group_label", language))
        self.trend_metric_label.configure(text=translate("trend_metric_label", language))
        self.trend_group_labels = {
            translate(label_key, language): key
            for key, label_key in TREND_GROUP_LABEL_KEYS.items()
        }
        self.trend_group_menu.configure(values=list(self.trend_group_labels))
        self.trend_group_menu.set(translate(TREND_GROUP_LABEL_KEYS[self.trend_group], language))
        self._configure_trend_metric_menu()
        for key, label_key in {
            "range": "trend_summary_range", "samples": "trend_summary_samples",
            "updated": "trend_summary_updated",
        }.items():
            self.trend_summary_labels[key].configure(text=translate(label_key, language))
        for key, label_key in {
            "current": "trend_summary_current",
            "minimum": "trend_summary_minimum",
            "maximum": "trend_summary_maximum",
            "change": "trend_summary_change",
        }.items():
            self.trend_summary_metric_labels[key].configure(text=translate(label_key, language))
        self.trend_chart.set_labels(
            metric_labels={
                key: translate(label_key, language)
                for key, label_key in TREND_METRIC_LABEL_KEYS.items()
            },
            source_labels={
                "dashboard": translate("trend_source_local_history", language),
                "mini": translate("trend_source_local_history", language),
                "token_monitor_history": translate("trend_source_local_history", language),
                "global_quota": translate("trend_source_global_quota", language),
                "codex_app_server": translate("trend_source_global_quota", language),
                "unknown": translate("trend_source_unknown", language),
            },
            tooltip_labels=TrendTooltipLabels(
                time=translate("trend_tooltip_time", language),
                metric=translate("trend_tooltip_metric", language),
                value=translate("trend_tooltip_value", language),
                source=translate("trend_tooltip_source", language),
                freshness=translate("trend_tooltip_freshness", language),
                stale=translate("trend_tooltip_stale", language),
                fresh=translate("trend_tooltip_fresh", language),
                derived=translate("trend_tooltip_derived", language),
                derived_yes=translate("recommendation_derived_yes", language),
                derived_no=translate("recommendation_derived_no", language),
            ),
        )
        self.recommendations_description.configure(text=translate("recommendations_description", language))
        self.recommendations_rules_button.configure(text=translate("recommendation_rules", language))

        for key, title in self.tool_group_titles.items():
            title.configure(text=translate(f"tools_group_{key}", language))
        self.diagnostic_run_button.configure(text=translate("run_diagnostics", language))
        self.diagnostic_view_button.configure(text=translate("view_diagnostic_result", language))
        self.diagnostic_export_button.configure(
            text=f"{translate('export_diagnostic_package', language)} · {translate('coming_soon', language)}",
        )
        for button, key in (
            (self.tool_open_codex, "open_codex"),
            (self.tool_open_data, "open_data_directory"),
            (self.tool_new_thread, "prepare_new_thread"),
            (self.tool_redetect, "redetect_sources"),
            (self.tool_privacy, "privacy_boundary"),
            (self.tool_guide, "user_guide"),
            (self.tool_about, "about_and_version"),
        ):
            button.configure(text=translate(key, language))
        for button, key in (
            (self.tool_backup, "backup_monitor_data"),
            (self.tool_restore, "restore_monitor_data"),
            (self.tool_cache_cleanup, "clear_monitor_cache"),
            (self.tool_update, "check_updates"),
        ):
            button.configure(
                text=f"{translate(key, language)} · {translate('coming_soon', language)}",
            )

        settings_keys = {
            "language": "language", "startup_mode": "default_startup_mode",
            "widget_mode": "widget_default_mode", "auto_refresh": "auto_refresh_setting",
            "exit_behavior": "exit_behavior",
            "widget_idle_opacity": "widget_idle_opacity",
            "start_with_windows": "start_with_windows",
            "refresh_interval": "refresh_interval", "stale_status": "stale_status",
            "tray": "tray_setting", "taskbar": "taskbar_setting",
            "always_on_top": "always_on_top", "remember_position": "remember_position",
            "privacy": "privacy_setting", "version": "version_setting",
            "updates": "updates_setting",
        }
        for name, key in settings_keys.items():
            self.settings_labels[name].configure(text=translate(key, language))
        self.settings_auto_switch.configure(
            text=translate("enabled" if self.auto_refresh_var.get() else "disabled", language)
        )
        self.settings_startup_switch.configure(
            text=translate("enabled" if self.settings_startup_var.get() else "disabled", language)
        )
        self.settings_note_var.set(translate("settings_no_refresh_note", language))
        self.settings_opacity_value.configure(
            text=f"{round(self.settings_opacity_var.get() * 100):.0f}%"
        )
        self._configure_settings_menus()
        for key, title in self.settings_group_titles.items():
            title.configure(text=translate(f"settings_group_{key}", language))
        self.settings_refresh_interval_value.configure(text=translate("refresh_interval_value", language))
        self.settings_stale_value.configure(text=translate("stale_status_value", language))
        self.settings_tray_value.configure(text=translate("available_value", language))
        self.settings_taskbar_value.configure(text=translate("taskbar_value", language))
        self.settings_topmost_value.configure(text=translate("enabled_fixed_value", language))
        self.settings_position_value.configure(text=translate("enabled_fixed_value", language))
        self.settings_privacy_button.configure(text=translate("privacy_boundary", language))
        self.settings_version_value.configure(text=__version__)
        self.settings_update_button.configure(
            text=f"{translate('check_updates', language)} · {translate('coming_soon', language)}",
        )

        if self.presentation is not None:
            self._apply_presentation(self.presentation)
        else:
            self._render_advisor()
            self._render_observed_usage()
            self._render_trends()
            self._render_recommendations()
        self._render_usage_insights()
        self._render_diagnostics()
        if hasattr(self, "mini_widget") and self.mini_widget.visible:
            self.mini_widget.update(
                self.quota_snapshot, self._mini_thread_snapshot, language,
                self.advisor_result.primary if self.advisor_result is not None else None,
            )
        if hasattr(self, "tray"):
            self.tray.update(
                language=language,
                auto_refresh_enabled=bool(self.auto_refresh_var.get()),
            )

    def _apply_sidebar_labels(self) -> None:
        for page in NAVIGATION_ITEMS:
            label = translate(f"nav_{page}", self.language)
            self.nav_buttons[page].configure(
                text="" if self._sidebar_collapsed else label,
                anchor="center" if self._sidebar_collapsed else "w",
            )

    def _configure_settings_menus(self) -> None:
        language = self.language
        self.startup_labels = {
            translate("startup_dashboard", language): "dashboard",
            translate("startup_widget", language): "widget",
            translate("startup_tray", language): "tray",
        }
        self.widget_mode_labels = {
            translate("widget_compact", language): "compact",
            translate("widget_expanded", language): "expanded",
        }
        self.exit_behavior_labels = {
            translate("exit_ask", language): "ask",
            translate("exit_minimize", language): "minimize",
            translate("exit_now", language): "exit",
        }
        self.settings_startup_menu.configure(values=list(self.startup_labels))
        self.settings_widget_menu.configure(values=list(self.widget_mode_labels))
        self.settings_exit_menu.configure(values=list(self.exit_behavior_labels))
        self.settings_startup_menu.set(next(
            label for label, value in self.startup_labels.items()
            if value == load_startup_mode(UI_SETTINGS_PATH)
        ))
        self.settings_widget_menu.set(next(
            label for label, value in self.widget_mode_labels.items()
            if value == self.widget_display_mode
        ))
        exit_behavior = load_exit_behavior(UI_SETTINGS_PATH)
        self.settings_exit_menu.set(next(
            label for label, value in self.exit_behavior_labels.items()
            if value == exit_behavior
        ))

    def _configure_history_filter_menu(self) -> None:
        self.status_filter_labels = {
            translate("filter_all", self.language): "all",
            translate("filter_running", self.language): "running",
            translate("filter_completed", self.language): "completed",
            translate("filter_attention", self.language): "attention",
        }
        self.status_filter_menu.configure(values=list(self.status_filter_labels))
        self.status_filter_menu.set(next(
            label for label, value in self.status_filter_labels.items()
            if value == self.status_filter
        ))

    def show_page(self, page: str) -> None:
        target = page if page in ALL_PAGES else "overview"
        self.shell_state = self.shell_state.navigate(target)
        self.current_nav_page = target
        for item, frame in self.page_frames.items():
            if item == target:
                frame.grid()
            else:
                frame.grid_remove()
        nav_target = "sessions" if target == "session_detail" else target
        for item, button in self.nav_buttons.items():
            button.configure(
                fg_color=COLORS.accent if item == nav_target else "transparent",
                text_color=COLORS.telemetry_text,
            )
        self._update_page_title()

    def _update_page_title(self) -> None:
        if hasattr(self, "page_title_var"):
            self.page_title_var.set(translate(f"nav_{self.current_nav_page}", self.language))

    def _settings_startup_changed(self, selected: str) -> None:
        save_startup_mode(self.startup_labels.get(selected, "dashboard"), UI_SETTINGS_PATH)

    def _settings_widget_changed(self, selected: str) -> None:
        self.widget_display_mode = self.widget_mode_labels.get(selected, "compact")
        self.shell_state = self.shell_state.with_widget_mode(self.widget_display_mode)
        save_widget_mode(self.widget_display_mode, UI_SETTINGS_PATH)
        if hasattr(self, "mini_widget"):
            self.mini_widget.set_mode(self.widget_display_mode)

    def _settings_exit_changed(self, selected: str) -> None:
        save_exit_behavior(self.exit_behavior_labels.get(selected, "ask"), UI_SETTINGS_PATH)

    def _settings_opacity_changed(self, value: float) -> None:
        save_widget_idle_opacity(value, UI_SETTINGS_PATH)
        self.settings_opacity_value.configure(text=f"{round(float(value) * 100):.0f}%")
        if hasattr(self, "mini_widget"):
            self.mini_widget.set_idle_opacity(value)

    def _settings_windows_startup_changed(self) -> None:
        if not self.startup_adapter.is_supported():
            return
        if self.settings_startup_var.get():
            self.startup_adapter.enable(sys.executable)
        else:
            self.startup_adapter.disable()
        self.settings_startup_switch.configure(text=translate("enabled" if self.settings_startup_var.get() else "disabled", self.language))

    def _select_task(self, label: str) -> None:
        if label == translate("auto_follow", self.language):
            snapshot = self.view_model.set_auto_follow()
        else:
            thread_id = self.label_to_thread.get(label)
            snapshot = self.view_model.select_cached_thread(thread_id) if thread_id else None
        if snapshot is not None:
            self._apply_cached_snapshot(snapshot)

    def _change_time_range(self, label: str) -> None:
        labels = {translate(f"last_{days}_days", self.language): days for days in (7, 30, 90)}
        days = labels.get(label)
        if days is not None and self.view_model.set_lookback_days(days):
            self.refresh(refresh_quota=False)

    def _change_status_filter(self, label: str) -> None:
        selected = self.status_filter_labels.get(label)
        if selected is None or selected == self.status_filter:
            return
        self.status_filter = selected
        self.current_page = 1
        if self.presentation is not None:
            self._render_sessions(self.presentation)

    def _change_session_search(self, _event: object = None) -> None:
        """Filter only the current in-memory safe-title rows."""
        self.current_page = 1
        if self.presentation is not None:
            self._render_sessions(self.presentation)

    def _select_recent_row(self, _event: object) -> None:
        if self._rendering_sessions:
            return
        selected = self.sessions_tree.selection()
        if selected:
            thread_id = selected[0]
            if thread_id not in self.selectable_thread_ids:
                self.sessions_tree.selection_remove(thread_id)
                return
            if self.view_model.selection_mode == "pinned" and self.view_model.selected_thread_id == thread_id:
                return
            snapshot = self.view_model.select_cached_thread(thread_id)
            if snapshot is not None:
                self._apply_cached_snapshot(snapshot)

    def manual_refresh(self) -> None:
        self.auto_refresh.manual_refresh()

    def _toggle_auto_refresh(self) -> None:
        enabled = bool(self.auto_refresh_var.get())
        self.auto_refresh.set_enabled(enabled)
        save_auto_refresh_enabled(enabled, UI_SETTINGS_PATH)
        self.auto_switch.configure(text=localize_auto_refresh(enabled, self.language, self.auto_refresh.interval_seconds))
        self.settings_auto_switch.configure(text=translate("enabled" if enabled else "disabled", self.language))
        if self.snapshot is not None:
            self.presentation = present_dashboard(self.snapshot, enabled)
            self._apply_presentation(self.presentation)
        self.tray.update(language=self.language, auto_refresh_enabled=enabled)

    def _toggle_auto_refresh_from_tray(self) -> None:
        self.auto_refresh_var.set(not bool(self.auto_refresh_var.get()))
        self._toggle_auto_refresh()

    def show_settings(self) -> None:
        self.restore_dashboard()
        self.show_page("settings")

    def _auto_refresh_error(self, _error: Exception) -> None:
        self.status_message_var.set(translate("auto_refresh_failed", self.language))
        self.header_message_label.configure(text_color=COLORS.error)

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._trend_query_generation += 1
        self._trend_query_poll_scheduled = False
        self._trend_query_stop.set()
        self.auto_refresh.close()
        self.quota_provider.close()
        self.tray.stop()
        self.settings_dialog.close()
        self.mini_widget.destroy()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def request_exit(self) -> None:
        behavior = load_exit_behavior(UI_SETTINGS_PATH)
        if behavior in {"minimize", "exit"}:
            self._apply_exit_choice(behavior)
            return
        remembered = None if self.mini_widget.visible else load_exit_action_for_today(UI_SETTINGS_PATH)
        if remembered is not None:
            self._apply_exit_choice(remembered)
            return
        owner = self.mini_widget.window if self.mini_widget.visible else self.root
        self.exit_dialog.show(
            owner=owner,
            language=self.language,
            on_choice=self._apply_exit_choice,
        )

    def _apply_exit_choice(self, action: str) -> None:
        if action == "exit":
            self.close()
        elif action == "minimize":
            self._minimize_to_taskbar()

    def refresh(self, show_refreshing: bool = True, render_session_rows: bool = True, refresh_quota: bool = True) -> None:
        if self._widget_mode:
            if show_refreshing:
                self.mini_widget.set_refreshing()
                self.root.update_idletasks()
            if refresh_quota:
                self.quota_snapshot = self.quota_provider.refresh()
            self._mini_thread_snapshot = self.view_model.refresh_thread(self._widget_thread_id)
            try:
                observation = HistoryObservation.from_mini(
                    self._mini_thread_snapshot,
                    self.quota_snapshot,
                    self._widget_thread_id,
                )
            except (TypeError, ValueError):
                self.history_error = "history_observation_invalid"
            else:
                self._record_history(observation)
            self.mini_widget.update(
                self.quota_snapshot,
                self._mini_thread_snapshot,
                self.language,
                self.advisor_result.primary if self.advisor_result is not None else None,
            )
            return
        if show_refreshing and self.presentation is not None and self.snapshot is not None:
            self._apply_presentation(present_dashboard(self.snapshot, bool(self.auto_refresh_var.get()), True, self.presentation))
            self.root.update_idletasks()
        self.snapshot = self.view_model.refresh()
        if self.snapshot.selection_mode == "auto":
            self.current_page = 1
        self.lookback_days = self.snapshot.lookback_days
        if refresh_quota:
            self.quota_snapshot = self.quota_provider.refresh()
        try:
            observation = HistoryObservation.from_dashboard(
                self.snapshot, self.quota_snapshot,
            )
        except (TypeError, ValueError):
            self.history_error = "history_observation_invalid"
        else:
            self._record_history(observation)
        self._refresh_trend_query()
        self.advisor_result = self._evaluate_advisor()
        self.presentation = present_dashboard(self.snapshot, bool(self.auto_refresh_var.get()))
        self._apply_presentation(self.presentation, render_session_rows=render_session_rows)

    def _record_history(self, observation: HistoryObservation) -> None:
        """Persist one normalized observation without blocking current UI data."""

        try:
            self.history_store.record(observation)
            self.history_error = self.history_store.last_error
        except (OSError, RuntimeError, TypeError, ValueError):
            self.history_error = "history_write_failed"

    def _refresh_trend_query(self) -> None:
        """Read only the app-owned store; never refresh a Codex source here."""

        self._invalidate_pending_trend_queries()
        self._schedule_trend_query()

    def _query_trend_view(
        self, range_days: int, thread_filter: str,
    ) -> tuple[TrendView, str | None]:
        try:
            result = self.history_store.query(range_days, thread_filter)
            return trend_view_from_query(result), result.error_code
        except (OSError, RuntimeError, ValueError):
            return TrendView(
                range_days,
                "unavailable",
                (),
                None,
                error_code="history_query_failed",
            ), "history_query_failed"

    def _query_observed_usage(
        self,
        scope: UsageWindowKind,
        as_of_utc: datetime,
    ) -> tuple[ObservedUsageSummary, str | None]:
        try:
            summary = self.history_store.summarize_usage(
                scope,
                as_of_utc=as_of_utc,
                local_timezone=None,
            )
            return summary, summary.error_code
        except (OSError, RuntimeError, TypeError, ValueError):
            return unavailable_usage_summary(
                scope,
                as_of_utc=as_of_utc,
                local_timezone=None,
                error_code="history_query_failed",
            ), "history_query_failed"

    def _schedule_trend_query(self) -> None:
        """Coalesce local-history requests off Tk and discard stale results."""

        self._trend_query_generation += 1
        generation = self._trend_query_generation
        range_days = self.trend_range_days
        selected_id = self.snapshot.selected_thread_id if self.snapshot is not None else None
        thread_filter = selected_id or "no_selection"
        self.trend_view = TrendView(range_days, "unavailable", (), None)
        as_of_utc = datetime.now(timezone.utc)
        self.observed_usage_summary = unavailable_usage_summary(
            self.usage_window_kind,
            as_of_utc=as_of_utc,
            local_timezone=None,
            error_code="history_query_pending",
        )
        self._render_usage_insights()
        self.history_error = None
        request = (
            generation,
            range_days,
            thread_filter,
            self.usage_window_kind,
            as_of_utc,
        )
        try:
            self._trend_query_requests.put_nowait(request)
        except queue.Full:
            try:
                self._trend_query_requests.get_nowait()
            except queue.Empty:
                pass
            self._trend_query_requests.put_nowait(request)
        if not self._trend_query_poll_scheduled:
            self._trend_query_poll_scheduled = True
            self.root.after(25, self._poll_trend_query_results)

    def _trend_query_worker_loop(self) -> None:
        while not self._trend_query_stop.is_set():
            try:
                request = self._trend_query_requests.get(timeout=0.1)
            except queue.Empty:
                continue
            while True:
                try:
                    request = self._trend_query_requests.get_nowait()
                except queue.Empty:
                    break
            generation, range_days, thread_filter, usage_scope, as_of_utc = request
            view, trend_error = self._query_trend_view(range_days, thread_filter)
            usage_summary, usage_error = self._query_observed_usage(
                usage_scope,
                as_of_utc,
            )
            self._trend_query_results.put((
                generation,
                view,
                usage_summary,
                trend_error or usage_error,
            ))

    def _poll_trend_query_results(self) -> None:
        if self._closing or not self._trend_query_poll_scheduled:
            return
        candidate: tuple[TrendView, ObservedUsageSummary, str | None] | None = None
        while True:
            try:
                generation, view, usage_summary, error = (
                    self._trend_query_results.get_nowait()
                )
            except queue.Empty:
                break
            if generation == self._trend_query_generation:
                candidate = (view, usage_summary, error)
        if candidate is None:
            self.root.after(25, self._poll_trend_query_results)
            return
        self._trend_query_poll_scheduled = False
        self.trend_view, self.observed_usage_summary, self.history_error = candidate
        if self.snapshot is not None:
            self.advisor_result = self._evaluate_advisor()
        self._render_observed_usage()
        self._render_usage_insights()
        self._render_trends()
        self._render_advisor()
        self._render_recommendations()

    def _invalidate_pending_trend_queries(self) -> None:
        self._trend_query_generation += 1
        self._trend_query_poll_scheduled = False
        for pending in (self._trend_query_requests, self._trend_query_results):
            while True:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    break

    def _evaluate_advisor(self) -> AdvisorResult:
        return evaluate_advice(build_advisor_input(
            self.snapshot,
            self.quota_snapshot,
            history_samples=self.trend_view.samples,
            quota_history_samples=self.trend_view.quota_samples,
        ))

    def _apply_cached_snapshot(self, snapshot) -> None:
        self.snapshot = snapshot
        self._schedule_trend_query()
        self.advisor_result = self._evaluate_advisor()
        self.presentation = present_dashboard(snapshot, bool(self.auto_refresh_var.get()))
        self._apply_presentation(self.presentation)

    def _on_root_configure(self, event: object) -> None:
        if getattr(event, "widget", None) is self.root and not self._widget_mode:
            try:
                if self.root.state() == "normal":
                    self._last_dashboard_geometry = self.root.geometry()
            except tk.TclError:
                pass
            if self._layout_job is not None:
                try:
                    self.root.after_cancel(self._layout_job)
                except tk.TclError:
                    pass
            self._layout_job = self.root.after(90, self._apply_responsive_layout)

    def _apply_responsive_layout(self) -> None:
        self._layout_job = None
        try:
            raw_width = self.root.winfo_width()
            reverse_scaling = getattr(self.root, "_reverse_window_scaling", None)
            window_width = max(
                1,
                int(reverse_scaling(raw_width)) if callable(reverse_scaling) else raw_width,
            )
        except tk.TclError:
            return
        collapsed = window_width < 1040
        if collapsed != self._sidebar_collapsed:
            self._sidebar_collapsed = collapsed
            sidebar_width = 64 if collapsed else 184
            self.root.grid_columnconfigure(0, minsize=sidebar_width)
            self.sidebar.configure(width=sidebar_width)
            if collapsed:
                self.brand_name.grid_remove()
                self.brand_icon.grid_configure(padx=4)
            else:
                self.brand_name.grid()
                self.brand_icon.grid_configure(padx=(0, SPACE_2))
            self._apply_sidebar_labels()
        sidebar_width = 64 if self._sidebar_collapsed else 184
        content_width = max(320, window_width - sidebar_width - (SPACE_4 * 2))
        self._apply_status_layout(content_width)
        if hasattr(self, "history_selector"):
            self._layout_history_controls(content_width)
            self._layout_history_columns(content_width)
            self._layout_sessions_page(content_width)
        if hasattr(self, "tool_group_cards"):
            self._layout_tool_groups(content_width)
        if hasattr(self, "settings_group_cards"):
            self._layout_settings_groups(content_width)
        if hasattr(self, "trend_metric_cells"):
            self._layout_trend_metrics(content_width)
        secondary_header = (
            self.auto_switch, self.mini_widget_button,
            self.header_settings_button, self.language_menu,
            self.header_message_label,
        )
        if window_width < 1040:
            for widget in secondary_header:
                widget.grid_remove()
        else:
            for widget in secondary_header:
                widget.grid()
        layout = dashboard_layout_for_width(content_width)
        reason_width = (
            int(content_width / 2) - 100
            if layout == "wide" else content_width - 64
        )
        self.status_reason_label.configure(wraplength=max(180, reason_width))
        if hasattr(self, "observed_usage_coverage_label"):
            wrap = max(220, content_width - 64)
            self.observed_usage_coverage_label.configure(wraplength=wrap)
            self.observed_usage_disclaimer.configure(wraplength=wrap)
        if hasattr(self, "usage_insights_sections"):
            wrap = max(220, content_width - 280)
            for section in self.usage_insights_sections.values():
                for row in section["rows"]:
                    row["details_label"].configure(wraplength=wrap)

    def _on_root_unmap(self, event: object) -> None:
        if (
            getattr(event, "widget", None) is not self.root
            or self._widget_mode
            or self._taskbar_mode
            or self._tray_mode
        ):
            return
        try:
            minimized = self.root.state() == "iconic"
        except tk.TclError:
            minimized = False
        if minimized:
            self.root.after_idle(self._enter_widget_mode)

    def _on_root_map(self, event: object) -> None:
        if (
            getattr(event, "widget", None) is not self.root
            or not self._taskbar_mode
            or self._taskbar_iconify_scheduled
        ):
            return
        self.root.after_idle(self._finish_taskbar_restore)

    def _finish_taskbar_restore(self) -> None:
        try:
            restored = self.root.state() == "normal"
        except tk.TclError:
            restored = False
        if restored:
            self._taskbar_mode = False
            self.window_mode = "dashboard"

    def _enter_widget_mode(self) -> None:
        if self._widget_mode:
            self.mini_widget.window.lift()
            return
        selected = self.snapshot.selected_session if self.snapshot is not None else None
        thread_id = selected.thread_id if selected is not None else None
        if thread_id and self.snapshot is not None and self.snapshot.selection_mode == "auto":
            self.view_model.pin_thread(thread_id)
        self._widget_mode = True
        self._taskbar_mode = False
        self._tray_mode = False
        self.window_mode = "widget"
        self._widget_thread_id = thread_id
        self.root.withdraw()
        self._mini_thread_snapshot = self._cached_mini_snapshot(selected)
        self.mini_widget.show(
            thread_id,
            self.quota_snapshot,
            self._mini_thread_snapshot,
            self.language,
            self.advisor_result.primary if self.advisor_result is not None else None,
        )

    def _show_widget_more(self) -> None:
        self.restore_dashboard()
        self.show_page("tools")

    def restore_dashboard(self) -> None:
        if self.window_mode == "dashboard":
            self.root.lift()
            return
        self.mini_widget.hide()
        self._widget_mode = False
        self._taskbar_mode = False
        self._tray_mode = False
        self.window_mode = "dashboard"
        self.root.deiconify()
        self.root.geometry(self._last_dashboard_geometry)
        self.root.lift()

    def _minimize_to_taskbar(self) -> None:
        self.mini_widget.hide()
        self._widget_mode = False
        self._taskbar_mode = True
        self._tray_mode = False
        self.window_mode = "taskbar"
        try:
            if self.root.state() == "withdrawn" or not self.root.winfo_viewable():
                self._taskbar_iconify_scheduled = True
                self.root.deiconify()
                self.root.after_idle(self._complete_taskbar_minimize)
            else:
                self.root.iconify()
        except tk.TclError:
            self._taskbar_mode = False

    def _complete_taskbar_minimize(self) -> None:
        self._taskbar_iconify_scheduled = False
        try:
            self.root.iconify()
        except tk.TclError:
            self._taskbar_mode = False

    def hide_to_tray(self) -> None:
        self.settings_dialog.close()
        if not self.tray.started:
            return
        self.mini_widget.hide()
        self._widget_mode = False
        self._taskbar_mode = False
        self._tray_mode = True
        self.window_mode = "tray"
        try:
            self.root.withdraw()
        except tk.TclError:
            self._tray_mode = False

    def _apply_startup_mode(self, mode: str) -> None:
        if mode == "widget":
            self._enter_widget_mode()
        elif mode == "tray":
            self.hide_to_tray()

    @staticmethod
    def _cached_mini_snapshot(selected) -> MiniThreadSnapshot:
        if selected is None:
            return MiniThreadSnapshot("", None, None, "no_selection", None, None)
        instruction = selected.instruction
        status = display_session_status(selected, instruction)
        instruction_total = instruction.usage.total_tokens if instruction is not None and instruction.usage is not None else None
        cumulative = selected.thread_cumulative_usage
        session_total = cumulative.total_tokens if cumulative is not None else None
        if status == "unavailable":
            instruction_total = session_total = None
        return MiniThreadSnapshot(
            selected.display_title, instruction_total, session_total, status,
            selected.observed_at, getattr(selected, "turn_count", None),
            getattr(selected, "full_title", None) or selected.display_title,
            make_response_safe_id(selected.thread_id, instruction.turn_id)
            if instruction is not None else None,
            (
                "in_progress" if instruction is not None and instruction.in_progress
                else "exact" if instruction is not None and instruction.exact
                else "completed_partial" if instruction is not None
                else "unavailable"
            ),
        )

    def _apply_presentation(self, presentation: DashboardPresentation, render_session_rows: bool = True) -> None:
        self.data_status_var.set(localize_status(presentation.data_status, self.language))
        self.status_message_var.set(localize_presenter_text(presentation.status_message, self.language))
        self.header_message_label.configure(
            text_color=COLORS.error
            if presentation.data_status.value == "unavailable"
            else COLORS.secondary_text,
        )
        self.last_event_var.set(presentation.last_event)
        self.last_refresh_var.set(presentation.last_refresh)
        self.recent_note.configure(text=self._recent_sessions_note())
        self._render_sessions(presentation, render_session_rows=render_session_rows)
        self._render_advisor()
        self._render_observed_usage()
        self._render_usage_insights()
        self._render_safe_overview()
        self._render_status_recent(presentation)
        self._render_trends()
        self._render_recommendations()

    def _render_advisor(self) -> None:
        if self.advisor_result is None:
            return
        recommendation = self.advisor_result.primary
        title = translate(recommendation.title_key, self.language)
        body = translate(recommendation.body_key, self.language)
        self.simple_status_title_var.set(
            translate("current_status_value", self.language, status=title)
        )
        self.simple_reason_var.set(
            ellipsize_title(body, 38 if self.language == "zh-CN" else 62)
        )
        action_keys = {
            "view_current_task": "view_current_task",
            "view_advice": "view_advice",
            "prepare_new_thread": "prepare_new_thread",
            "view_quota": "view_quota",
            "diagnose": "one_click_diagnostics",
        }
        self.primary_action_button.configure(
            text=translate(action_keys.get(recommendation.primary_action, "view_current_task"), self.language)
        )
        color = {
            "normal": COLORS.real,
            "optimize": COLORS.orange,
            "new_thread": COLORS.orange,
            "quota_risk": COLORS.orange,
            "data_unavailable": COLORS.error,
        }[recommendation.status]
        soft = {
            "normal": COLORS.real_soft,
            "optimize": COLORS.orange_soft,
            "new_thread": COLORS.orange_soft,
            "quota_risk": COLORS.orange_soft,
            "data_unavailable": COLORS.error_soft,
        }[recommendation.status]
        self.simple_status_accent.configure(fg_color=color, text="")
        self.header_status_var.set(title)
        self.header_status_badge.configure(
            text_color=color, fg_color=soft,
        )
        connected = recommendation.status != "data_unavailable"
        self.nav_connection_var.set(translate(
            "connection_normal" if connected else "connection_abnormal", self.language
        ))

    def _configure_trend_metric_menu(self) -> None:
        metrics = TREND_GROUP_METRICS[self.trend_group]
        if self.trend_metric not in metrics:
            self.trend_metric = metrics[0]
        self.trend_metric_labels = {
            translate(TREND_METRIC_LABEL_KEYS[key], self.language): key
            for key in metrics
        }
        self.trend_metric_menu.configure(values=list(self.trend_metric_labels))
        self.trend_metric_menu.set(
            translate(TREND_METRIC_LABEL_KEYS[self.trend_metric], self.language)
        )

    def _change_trend_group(self, label: str) -> None:
        group = self.trend_group_labels.get(label)
        if group is None or group == self.trend_group:
            return
        self.trend_group = group
        self.trend_metric = TREND_GROUP_METRICS[group][0]
        self._configure_trend_metric_menu()
        self._render_trends()

    def _change_usage_window(self, label: str) -> None:
        scope = self.usage_window_labels.get(label)
        if scope is None or scope == self.usage_window_kind:
            return
        self.usage_window_kind = scope
        self._schedule_trend_query()
        self._render_observed_usage()
        self._render_usage_insights()

    def _toggle_usage_insights_group(self, group: str) -> None:
        if group not in self.usage_insights_expanded:
            return
        self.usage_insights_expanded[group] = not self.usage_insights_expanded[group]
        self._render_usage_insights()

    def _change_trend_metric(self, label: str) -> None:
        metric = self.trend_metric_labels.get(label)
        if metric is None or metric == self.trend_metric:
            return
        self.trend_metric = metric
        self._render_trends()

    def _change_trend_range(self, label: str) -> None:
        labels = {
            translate(f"last_{days}_days", self.language): days
            for days in (7, 30, 90)
        }
        days = labels.get(label)
        if days is None:
            return
        self.trend_range_days = days
        self._schedule_trend_query()
        if self.snapshot is not None:
            self.advisor_result = self._evaluate_advisor()
        self._render_trends()
        self._render_advisor()
        self._render_recommendations()

    def _render_trends(self) -> None:
        if not hasattr(self, "trend_quality_var"):
            return
        view = self.trend_view
        summary = summarize_metric(view, self.trend_metric)
        scope_key = self._trend_scope_key(self.trend_metric)
        self.trend_scope_var.set(translate(scope_key, self.language))
        self.trend_preview_scope_var.set(translate("trend_scope_thread", self.language))
        quality = self._trend_metric_quality(view, self.trend_metric, summary.sample_count)
        tone, soft = {
            "empty": (COLORS.unknown, COLORS.unknown_soft),
            "available": (COLORS.real, COLORS.real_soft),
            "insufficient": (COLORS.orange, COLORS.orange_soft),
            "unavailable": (COLORS.unknown, COLORS.unknown_soft),
            "stale": (COLORS.stale, COLORS.stale_soft),
        }[quality]
        title = translate(f"trend_quality_{quality}_title", self.language)
        message = translate(
            f"trend_quality_{quality}_message", self.language,
            count=summary.sample_count,
        )
        self.trend_quality_var.set(title)
        self.trend_quality_message_var.set(message)
        self.trend_quality_label.configure(text_color=tone, fg_color=soft)
        actual_start = (
            summary.start_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if summary.start_at is not None else "—"
        )
        actual_end = (
            summary.end_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if summary.end_at is not None else "—"
        )
        self.trend_summary_vars["range"].set(translate(
            "trend_actual_range_value", self.language,
            days=view.range_days, start=actual_start, end=actual_end,
        ))
        self.trend_summary_vars["samples"].set(
            translate("trend_sample_count", self.language, count=summary.sample_count)
        )
        updated_at = summary.end_at
        if self.trend_metric == "five_hour":
            updated_at = view.five_hour_last_seen_at
        elif self.trend_metric == "weekly":
            updated_at = view.weekly_last_seen_at
        self.trend_summary_vars["updated"].set(
            updated_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if updated_at is not None else "—"
        )
        for key, value in {
            "current": summary.current,
            "minimum": summary.minimum,
            "maximum": summary.maximum,
            "change": summary.change,
        }.items():
            self.trend_metric_vars[key].set(
                self._format_trend_value(self.trend_metric, value, signed=key == "change")
            )
        points = self._trend_points(view, self.trend_metric)
        self.trend_chart.set_points(points)
        show_chart = quality in {"available", "stale"} and len(points) >= 2
        if show_chart:
            self.trend_chart.grid()
        else:
            self.trend_chart.grid_remove()

        preview = summarize_metric(view, "total")
        preview_quality = self._trend_metric_quality(view, "total", preview.sample_count)
        preview_tone = {
            "empty": COLORS.unknown,
            "available": COLORS.real,
            "insufficient": COLORS.orange,
            "unavailable": COLORS.unknown,
            "stale": COLORS.stale,
        }[preview_quality]
        preview_title = translate(f"trend_quality_{preview_quality}_title", self.language)
        preview_message = translate(
            f"trend_quality_{preview_quality}_message",
            self.language,
            count=preview.sample_count,
        )
        self.trend_preview_state_var.set(preview_title)
        self.trend_preview_state.configure(text_color=preview_tone)
        preview_start = (
            preview.start_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if preview.start_at is not None else "—"
        )
        preview_end = (
            preview.end_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if preview.end_at is not None else "—"
        )
        preview_details = translate(
            "trend_preview_details", self.language,
            current=self._format_trend_value("total", preview.current),
            start=preview_start,
            end=preview_end,
            updated=preview_end,
        )
        self.trend_preview_message_var.set(f"{preview_message}\n{preview_details}")
        preview_values = tuple(value for _, value in metric_samples(view, "total"))
        preview_visible = (
            preview_quality in {"available", "stale"}
            and self.trend_preview_plot.set_samples(preview_values)
        )
        if preview_visible:
            self.trend_preview_plot.grid()
        else:
            self.trend_preview_plot.grid_remove()

    @staticmethod
    def _trend_metric_quality(view: TrendView, metric: str, sample_count: int) -> str:
        if view.error_code:
            return "unavailable"
        if metric in {"five_hour", "weekly"}:
            prefix = "five_hour" if metric == "five_hour" else "weekly"
            relevant_samples = tuple(
                sample for sample in view.quota_samples
                if metric_observed_at(sample, metric) is not None
            )
            available = getattr(view, f"{prefix}_available", None)
            stale = bool(getattr(view, f"{prefix}_stale", False))
            last_seen = getattr(view, f"{prefix}_last_seen_at", None)
            if available is False:
                return "unavailable"
            if stale:
                return "stale"
            if (
                last_seen is not None
                and datetime.now(timezone.utc) - last_seen > TREND_STALE_AFTER
            ):
                return "stale"
        elif view.quality in {"unavailable", "stale"}:
            return view.quality
        else:
            relevant_samples = view.samples
        if sample_count == 0:
            return "unavailable" if relevant_samples else "empty"
        if sample_count < 2:
            return "insufficient"
        return "available"

    @staticmethod
    def _trend_scope_key(metric: str) -> str:
        return "trend_scope_global" if metric in {"five_hour", "weekly"} else "trend_scope_thread"

    @staticmethod
    def _history_scope_key(source: str) -> str:
        return (
            "trend_scope_global"
            if source == "global_quota_history"
            else "trend_scope_thread"
        )

    def _format_trend_value(
        self, metric: str, value: float | None, *, signed: bool = False,
    ) -> str:
        if value is None:
            return "—"
        sign = "+" if signed and value > 0 else ""
        if signed and metric in {"five_hour", "weekly"}:
            return translate(
                "trend_change_percentage_points",
                self.language,
                value=f"{sign}{value:.1f}",
            )
        if metric in {"cache_reuse", "five_hour", "weekly"}:
            return f"{sign}{value:.1f}%"
        if metric == "turn_count":
            return f"{sign}{round(value):,}"
        compact = format_compact_token_count(round(value))
        return f"{sign}{compact}"

    def _trend_points(self, view: TrendView, metric: str) -> tuple[TrendPoint, ...]:
        points: list[TrendPoint] = []
        for sample, value in metric_samples(view, metric):
            observed_at = metric_observed_at(sample, metric)
            if observed_at is None:
                continue
            if metric == "five_hour":
                source = getattr(sample, "five_hour_source", "global_quota")
                stale = bool(getattr(sample, "five_hour_stale", False))
            elif metric == "weekly":
                source = getattr(sample, "weekly_source", "global_quota")
                stale = bool(getattr(sample, "weekly_stale", False))
            else:
                source = getattr(sample, "source_type", "token_monitor_history")
                stale = bool(getattr(sample, "token_stale", False))
            unit = "percent" if metric in {"cache_reuse", "five_hour", "weekly"} else (
                "turn" if metric == "turn_count" else "token"
            )
            try:
                points.append(TrendPoint(
                    observed_at=observed_at,
                    metric=metric,
                    value=value,
                    source=source,
                    stale=stale,
                    unit=unit,
                    derived=metric == "cache_reuse",
                ))
            except ValueError:
                continue
        return tuple(points)

    def _format_trend_tooltip_value(self, point: TrendPoint) -> str:
        if point.value is None:
            return "—"
        number = float(point.value)
        value = (
            f"{int(number):,}"
            if number.is_integer()
            else f"{number:,.2f}".rstrip("0").rstrip(".")
        )
        unit_key = {
            "percent": "trend_unit_percent",
            "turn": "trend_unit_turn",
            "token": "trend_unit_token",
        }.get(point.unit)
        return value if unit_key is None else f"{value} {translate(unit_key, self.language)}"

    def _render_recommendations(self) -> None:
        if not hasattr(self, "recommendation_cards"):
            return
        recommendations = (
            self.advisor_result.recommendations
            if self.advisor_result is not None else ()
        )
        action_keys = {
            "view_current_task": "view_current_task",
            "view_advice": "view_advice",
            "prepare_new_thread": "prepare_new_thread",
            "view_quota": "view_quota",
            "diagnose": "one_click_diagnostics",
        }
        for index, widget in enumerate(self.recommendation_cards):
            card = widget["card"]
            if index >= len(recommendations):
                widget["recommendation"] = None
                card.grid_remove()
                continue
            recommendation = recommendations[index]
            widget["recommendation"] = recommendation
            card.grid()
            widget["title"].set(translate(recommendation.title_key, self.language))
            widget["body"].set(translate(recommendation.body_key, self.language))
            widget["severity"].set(
                translate(f"severity_{recommendation.severity}", self.language)
            )
            evidence = " · ".join(
                f"{translate(f'evidence_{key}', self.language)}: {self._format_advisor_evidence(key, value)}"
                for key, value in recommendation.evidence
            ) or "—"
            widget["evidence"].set(
                translate("recommendation_evidence", self.language, value=evidence)
            )
            source = translate("recommendation_source_safe_numeric", self.language)
            widget["metadata"].set(
                f"{translate('recommendation_source', self.language, value=source)} · "
                f"{translate('recommendation_derived', self.language, value=translate('recommendation_derived_yes' if recommendation.derived else 'recommendation_derived_no', self.language))}"
            )
            history = recommendation.history_evidence
            if history is None:
                history_text = translate("recommendation_history_insufficient", self.language)
            else:
                direction_key = {
                    "up": "history_direction_up",
                    "down": "history_direction_down",
                    "flat": "history_direction_stable",
                }[history.direction]
                history_text = translate(
                    "advisor_history_summary",
                    self.language,
                    metric=translate(f"evidence_{history.metric}", self.language),
                    direction=translate(direction_key, self.language),
                    count=history.sample_count,
                )
                history_text = translate(
                    "recommendation_history_scope",
                    self.language,
                    scope=translate(
                        self._history_scope_key(history.source),
                        self.language,
                    ),
                    value=history_text,
                )
            widget["history"].set(
                translate("recommendation_history", self.language, value=history_text)
            )
            observed_at = recommendation.source_observed_at or recommendation.observed_at
            widget["observed"].set(translate(
                "recommendation_observed", self.language,
                value=observed_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            ))
            widget["action"].configure(
                text=translate(
                    action_keys.get(recommendation.primary_action, "view_current_task"),
                    self.language,
                )
            )
            tone, soft = {
                "normal": (COLORS.real, COLORS.real_soft),
                "notice": (COLORS.orange, COLORS.orange_soft),
                "warning": (COLORS.orange, COLORS.orange_soft),
                "failure": (COLORS.error, COLORS.error_soft),
            }[recommendation.severity]
            widget["severity_label"].configure(text_color=tone, fg_color=soft)

    def _format_advisor_evidence(self, key: str, value: object) -> str:
        if value is None:
            return "—"
        if isinstance(value, bool):
            return translate("available_value" if value else "unavailable", self.language)
        if key.endswith("_percent") or key == "cache_hit_percent_derived":
            return f"{float(value):.1f}%"
        if key.endswith("_tokens"):
            return format_full_token_count(int(value))
        if isinstance(value, str):
            return localize_presenter_text(value, self.language)
        return str(value)

    def _execute_recommendation(self, index: int) -> None:
        if index >= len(self.recommendation_cards):
            return
        recommendation = self.recommendation_cards[index].get("recommendation")
        if not isinstance(recommendation, Recommendation):
            return
        action = recommendation.primary_action
        if action == "prepare_new_thread":
            self._show_new_thread_dialog()
        elif action == "diagnose":
            self.start_diagnostics()
        elif action == "view_advice":
            self._show_advisor_rules()
        elif action == "view_quota":
            self.show_page("overview")
        else:
            self.show_page("session_detail")

    def _show_advisor_rules(self) -> None:
        messagebox.showinfo(
            translate("recommendation_rules", self.language),
            translate("advisor_rules_body", self.language),
            parent=self.root,
        )

    def _render_usage_insights(self) -> None:
        if not hasattr(self, "usage_insights_sections"):
            return
        view = build_usage_insights_view(
            self.observed_usage_summary.insights,
            self.language,
            expanded_threads=self.usage_insights_expanded["threads"],
            expanded_responses=self.usage_insights_expanded["responses"],
        )
        self.usage_insights_title_var.set(view.title)
        self.usage_insights_range_var.set(view.range_label)
        self.usage_insights_state_var.set(view.state_text)
        state_colors = {
            "available": COLORS.real,
            "partial": COLORS.orange,
            "empty": COLORS.unknown,
            "unavailable": COLORS.error,
        }
        self.usage_insights_state_label.configure(
            text_color=state_colors[view.state_kind],
        )
        if view.state_text:
            self.usage_insights_state_label.grid()
        else:
            self.usage_insights_state_label.grid_remove()

        partial_text = translate("usage_insights_row_partial", self.language)
        for section_view in view.sections:
            section = self.usage_insights_sections[section_view.key]
            section["title"].set(section_view.title)
            section["toggle"].configure(text=section_view.toggle_text)
            if section_view.can_expand:
                section["toggle"].grid()
            else:
                section["toggle"].grid_remove()
            for row_widget in section["rows"]:
                for key in ("title", "primary", "details", "coverage"):
                    row_widget[key].set("")
                row_widget["frame"].grid_remove()
            for row_widget, row_view in zip(section["rows"], section_view.rows):
                row_widget["title"].set(row_view.title)
                row_widget["primary"].set(row_view.primary)
                row_widget["details"].set(row_view.details)
                row_widget["coverage"].set(row_view.coverage)
                row_widget["coverage_label"].configure(
                    text_color=(
                        COLORS.orange
                        if row_view.coverage == partial_text else COLORS.real
                    ),
                )
                row_widget["frame"].grid()
            if section_view.rows:
                section["frame"].grid()
            else:
                section["frame"].grid_remove()

    def _render_observed_usage(self) -> None:
        if not hasattr(self, "observed_usage_metric_widgets"):
            return
        summary = self.observed_usage_summary
        metric_values = {
            "total": summary.total_tokens,
            "input": summary.input_tokens,
            "output": summary.output_tokens,
            "cached": summary.cached_tokens,
            "reasoning": summary.reasoning_tokens,
        }
        for name, aggregate in metric_values.items():
            value = format_compact_token_count(aggregate.value)
            widget = self.observed_usage_metric_widgets[name]
            widget["value"].set(value)
            widget["full"].set(self._full_token_tooltip(aggregate.value))

        has_observations = summary.observed_response_count > 0
        average = summary.average_total_tokens_per_response
        cache_reuse = summary.cache_reuse.value
        self.observed_usage_aux_widgets["responses"]["value"].set(
            str(summary.observed_response_count) if has_observations else "—"
        )
        self.observed_usage_aux_widgets["sessions"]["value"].set(
            str(summary.covered_thread_count) if has_observations else "—"
        )
        self.observed_usage_aux_widgets["average"]["value"].set(
            format_compact_token_count(round(average)) if average is not None else "—"
        )
        self.observed_usage_aux_widgets["cache_reuse"]["value"].set(
            f"{cache_reuse * 100:.1f}%" if cache_reuse is not None else "—"
        )

        details: list[str] = []
        if summary.scope is UsageWindowKind.ROLLING_5H:
            details.append(translate("observed_usage_rolling_5h_label", self.language))
        if has_observations:
            details.append(translate(
                "observed_usage_summary",
                self.language,
                responses=summary.observed_response_count,
                sessions=summary.covered_thread_count,
            ))
        state_key = {
            "complete_for_local_history": "observed_usage_all_retained",
            "limited_history": "observed_usage_limited",
            "partial": "observed_usage_partial",
            "no_observations": "observed_usage_no_data",
            "unknown": "observed_usage_unknown",
            "unavailable": "observed_usage_unavailable",
        }[summary.coverage_state]
        if not has_observations and summary.in_progress_observation_count:
            state_key = "observed_usage_no_completed"
        details.append(translate(state_key, self.language))

        history_started = summary.coverage.history_started_at
        if (
            history_started is not None
            and history_started > summary.window_start_utc
        ):
            details.append(translate(
                "observed_usage_data_since",
                self.language,
                time=history_started.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            ))
        metric_labels = {
            "input_tokens": "metric_input",
            "output_tokens": "metric_output",
            "total_tokens": "metric_total",
            "cached_tokens": "metric_cached",
            "reasoning_tokens": "metric_reasoning",
        }
        for message in summary.coverage_messages:
            if message.code == "metric_coverage" and message.metric in metric_labels:
                details.append(translate(
                    "observed_usage_field_coverage",
                    self.language,
                    metric=translate(metric_labels[message.metric], self.language),
                    eligible=message.eligible_count,
                    total=message.total_count,
                ))
            elif message.code == "thread_coverage":
                details.append(translate(
                    "observed_usage_thread_coverage",
                    self.language,
                    eligible=message.eligible_count,
                    total=message.total_count,
                ))
            elif message.code == "in_progress_excluded":
                details.append(translate(
                    "observed_usage_in_progress_excluded", self.language,
                ))
                details.append(translate(
                    "observed_usage_in_progress_pending", self.language,
                ))
            elif message.code == "missing_response_identity":
                details.append(translate(
                    "observed_usage_missing_identity", self.language,
                ))
        if summary.freshness_state == "stale":
            details.append(translate("observed_usage_stale", self.language))
        if summary.last_reliable_observed_at is not None:
            details.append(translate(
                "observed_usage_last_observed",
                self.language,
                time=summary.last_reliable_observed_at.astimezone().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
            ))
        self.observed_usage_coverage_var.set(" · ".join(details))
        coverage_color = {
            "complete_for_local_history": COLORS.real,
            "limited_history": COLORS.orange,
            "partial": COLORS.orange,
            "no_observations": COLORS.unknown,
            "unknown": COLORS.unknown,
            "unavailable": COLORS.error,
        }[summary.coverage_state]
        if summary.freshness_state == "stale":
            coverage_color = COLORS.stale
        self.observed_usage_coverage_label.configure(text_color=coverage_color)

    def _render_safe_overview(self) -> None:
        quota = self.quota_snapshot
        for prefix, window in (("five", quota.five_hour), ("week", quota.weekly)):
            remaining = format_percent(window.remaining_percent)
            used = format_percent(window.used_percent)
            reset = (
                format_reset_time(window.reset_at, self.language, window.observed_at)
                if window.reset_at is not None else "—"
            )
            self.simple_quota_vars[f"{prefix}_remaining"].set(remaining)
            self.simple_quota_vars[f"{prefix}_used"].set(
                translate("quota_used_value", self.language, value=used)
            )
            self.simple_quota_vars[f"{prefix}_reset"].set(reset)
            state_key = "quota_stale" if window.stale else (
                "quota_normal" if window.available else "quota_unavailable"
            )
            state_color = COLORS.stale if window.stale else (
                COLORS.real if window.available else COLORS.unknown
            )
            widgets = self.quota_window_widgets[prefix]
            widgets["state"].set(translate(state_key, self.language))
            widgets["state_label"].configure(text_color=state_color)
            widgets["ring"].set(
                window.remaining_percent,
                color=COLORS.stale if window.stale else (
                    COLORS.real if prefix == "five" else COLORS.accent
                ),
            )
            progress = widgets["progress"]
            if window.used_percent is None:
                progress.grid_remove()
            else:
                progress.grid()
                progress.set(window.used_percent / 100.0)
                progress.configure(
                    progress_color=COLORS.stale if window.stale else (
                        COLORS.real if prefix == "five" else COLORS.accent
                    )
                )

        selected = self.snapshot.selected_session if self.snapshot is not None else None
        if selected is None:
            full_title = translate("no_selected_thread", self.language)
            usage = cumulative = None
            values = {
                "title": full_title,
                "status": translate("quota_unavailable", self.language),
                "turns": "—", "instruction": "—", "session": "—", "activity": "—",
            }
            cache = "—"
        else:
            instruction = selected.instruction
            usage = instruction.usage if instruction is not None else None
            cumulative = selected.thread_cumulative_usage
            status = display_session_status(selected, instruction)
            cache = "—" if usage is None or usage.input_tokens <= 0 else f"{usage.cached_input_tokens / usage.input_tokens * 100:.1f}%"
            full_title = getattr(selected, "full_title", None) or selected.display_title
            values = {
                "title": ellipsize_title(full_title, 52),
                "status": localize_presenter_text(status, self.language),
                "turns": str(getattr(selected, "turn_count", 0)) if getattr(selected, "turn_count", 0) else "—",
                "instruction": format_compact_token_count(usage.total_tokens if usage is not None else None),
                "session": format_compact_token_count(cumulative.total_tokens if cumulative is not None else None),
                "activity": selected.observed_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            }
        for name, value in values.items():
            self.simple_task_vars[name].set(value)
        self.task_full_title_var.set(full_title)
        self.task_summary_status_var.set(values["status"])
        status_code = getattr(selected, "status", "unavailable") if selected is not None else "unavailable"
        tone, tone_soft = {
            "in_progress": (COLORS.real, COLORS.real_soft),
            "exact": (COLORS.real, COLORS.real_soft),
            "completed_partial": (COLORS.stale, COLORS.stale_soft),
            "incomplete": (COLORS.orange, COLORS.orange_soft),
            "unavailable": (COLORS.unknown, COLORS.unknown_soft),
        }.get(status_code, (COLORS.unknown, COLORS.unknown_soft))
        self.task_summary_status.configure(
            text_color=tone,
            fg_color=tone_soft,
        )

        instruction_total = usage.total_tokens if usage is not None else None
        session_total = cumulative.total_tokens if cumulative is not None else None
        reasoning = usage.reasoning_output_tokens if usage is not None else None
        metric_values = {
            "current_turn": (
                format_compact_token_count(instruction_total),
                self._full_token_tooltip(instruction_total),
                values["status"], None,
            ),
            "session_total": (
                format_compact_token_count(session_total),
                self._full_token_tooltip(session_total),
                translate("task_turns_value", self.language, value=values["turns"]), None,
            ),
            "cache_reuse": (
                cache,
                translate("derived_percent_value", self.language, value=cache) if cache != "—" else "—",
                translate("locally_derived", self.language),
                None if cache == "—" else float(cache.rstrip("%")) / 100.0,
            ),
            "reasoning": (
                format_compact_token_count(reasoning),
                self._full_token_tooltip(reasoning),
                translate("current_turn_scope", self.language), None,
            ),
            "five_hour_quota": (
                self.simple_quota_vars["five_remaining"].get(),
                self._format_quota_summary(quota.five_hour),
                self.quota_window_widgets["five"]["state"].get(),
                None if quota.five_hour.remaining_percent is None else quota.five_hour.remaining_percent / 100.0,
            ),
            "weekly_quota": (
                self.simple_quota_vars["week_remaining"].get(),
                self._format_quota_summary(quota.weekly),
                self.quota_window_widgets["week"]["state"].get(),
                None if quota.weekly.remaining_percent is None else quota.weekly.remaining_percent / 100.0,
            ),
        }
        for widget in self.core_metric_widgets:
            value, full, hint, progress_value = metric_values[widget["semantic"]]
            widget["value"].set(value)
            widget["full"].set(full)
            widget["hint"].set(hint)
            if widget["progress"] is not None:
                if progress_value is None:
                    widget["progress"].grid_remove()
                else:
                    widget["progress"].grid()
                    widget["progress"].set(progress_value)
            if widget["ring"] is not None:
                window = (
                    quota.weekly
                    if widget["semantic"] == "weekly_quota"
                    else quota.five_hour
                )
                widget["ring"].set(
                    None if progress_value is None else progress_value * 100.0,
                    color=COLORS.stale if window.stale else (
                        COLORS.accent if widget["semantic"] == "weekly_quota" else COLORS.teal
                    ),
                )
            if widget["sparkline"] is not None:
                has_samples = widget["sparkline"].set_samples(
                    self._metric_trend_samples(widget["semantic"]),
                )
                if has_samples:
                    widget["sparkline"].grid()
                else:
                    widget["sparkline"].grid_remove()

        self.task_detail_vars["title"].set(full_title)
        self.task_detail_vars["status"].set(values["status"])
        self.task_detail_vars["activity"].set(values["activity"])
        self.task_detail_vars["turns"].set(values["turns"])
        for name, raw in (
            ("input", usage.input_tokens if usage is not None else None),
            ("output", usage.output_tokens if usage is not None else None),
            ("total", instruction_total),
            ("cached", usage.cached_input_tokens if usage is not None else None),
            ("reasoning", reasoning),
            ("session", session_total),
        ):
            self.task_detail_vars[name].set(format_full_token_count(raw))
        self.task_detail_vars["cache"].set(
            translate("derived_percent_value", self.language, value=cache) if cache != "—" else "—"
        )
        self.task_detail_vars["quota_five"].set(self._format_quota_summary(quota.five_hour))
        self.task_detail_vars["quota_weekly"].set(self._format_quota_summary(quota.weekly))
        if self.advisor_result is not None:
            primary = self.advisor_result.primary
            self.task_detail_vars["advice"].set(
                f"{translate(primary.title_key, self.language)} · "
                f"{translate(primary.body_key, self.language)}"
            )

    def _metric_trend_samples(self, semantic: str) -> tuple[int, ...]:
        """Return chronological real samples; never synthesize chart points."""
        if self.snapshot is None:
            return ()
        values: list[int] = []
        for session in reversed(self.snapshot.recent_sessions):
            usage = session.instruction.usage if session.instruction is not None else None
            cumulative = session.thread_cumulative_usage
            value = None
            if semantic == "current_turn" and usage is not None:
                value = usage.total_tokens
            elif semantic == "session_total" and cumulative is not None:
                value = cumulative.total_tokens
            elif semantic == "reasoning" and usage is not None:
                value = usage.reasoning_output_tokens
            if value is not None:
                values.append(value)
        return tuple(values)

    def _full_token_tooltip(self, value: int | None) -> str:
        if value is None:
            return "—"
        return translate(
            "full_token_value", self.language,
            value=format_full_token_count(value),
        )

    def _format_quota_summary(self, window) -> str:
        remaining = format_percent(window.remaining_percent)
        used = format_percent(window.used_percent)
        reset = format_reset_time(window.reset_at, self.language, window.observed_at)
        state_key = "quota_stale" if window.stale else (
            "quota_normal" if window.available else "quota_unavailable"
        )
        return translate(
            "quota_summary", self.language, remaining=remaining, used=used,
            reset=reset, state=translate(state_key, self.language),
        )

    def _render_status_recent(self, presentation: DashboardPresentation) -> None:
        selected_id = self.snapshot.selected_thread_id if self.snapshot else None
        for index, widget in enumerate(self.status_recent_rows):
            if index >= len(presentation.recent_sessions):
                widget["thread_id"] = None
                widget["title"].set(translate("no_recent_task", self.language))
                widget["full_title"].set("—")
                widget["detail"].set("")
                widget["current"].set("")
                widget["button"].configure(state="disabled", fg_color=COLORS.raised_surface)
                continue
            row = presentation.recent_sessions[index]
            full_title = row.full_title or row.display_title
            activity = (
                row.last_activity.astimezone().strftime("%m-%d %H:%M")
                if row.last_activity else "—"
            )
            total_value = getattr(row, "thread_total_tokens", None)
            total = format_compact_token_count(total_value)
            widget["thread_id"] = row.thread_id
            widget["title"].set(ellipsize_title(full_title, 42))
            widget["full_title"].set(full_title)
            widget["detail"].set(translate(
                "recent_task_detail", self.language,
                status=localize_presenter_text(row.status, self.language),
                turns=row.turn_count or "—", total=total, activity=activity,
            ))
            current = row.thread_id == selected_id
            widget["current"].set(translate("current_task_badge", self.language) if current else "")
            widget["button"].configure(
                state="normal",
                fg_color=COLORS.accent_soft if current else COLORS.raised_surface,
                border_color=COLORS.accent if current else COLORS.border,
            )

    def _execute_primary_action(self) -> None:
        if self.advisor_result is None:
            self.start_diagnostics()
            return
        action = self.advisor_result.primary.primary_action
        if action == "prepare_new_thread":
            self._show_new_thread_dialog()
        elif action == "diagnose":
            self.start_diagnostics()
        elif action == "view_advice":
            self._show_reason()
        elif action == "view_quota":
            self.show_page("session_detail")
        else:
            self.show_page("session_detail")

    def _show_reason(self) -> None:
        if self.advisor_result is None:
            return
        recommendation = self.advisor_result.primary
        messagebox.showinfo(
            translate(recommendation.title_key, self.language),
            translate(recommendation.body_key, self.language),
            parent=self.root,
        )

    def _show_new_thread_dialog(self) -> None:
        existing = getattr(self, "new_thread_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        window = self.new_thread_window = ctk.CTkToplevel(self.root)
        window.title(translate("prepare_new_thread", self.language))
        window.geometry("560x430")
        window.resizable(False, False)
        if self.root.winfo_viewable():
            window.transient(self.root)
        window.grid_columnconfigure(0, weight=1)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        recommendation = self.advisor_result.primary if self.advisor_result is not None else None
        reason = translate(recommendation.body_key, self.language) if recommendation is not None else translate("new_thread_generic_reason", self.language)
        ctk.CTkLabel(window, text=translate("prepare_new_thread", self.language), font=(FONT_FAMILY, 21, "bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=SPACE_6, pady=(SPACE_6, SPACE_2))
        ctk.CTkLabel(window, text=reason, font=FONT_BODY, text_color=COLORS.secondary_text, justify="left", anchor="w", wraplength=500).grid(row=1, column=0, sticky="ew", padx=SPACE_6)
        ctk.CTkLabel(window, text=translate("new_thread_steps", self.language), font=FONT_BODY, justify="left", anchor="w", wraplength=500).grid(row=2, column=0, sticky="ew", padx=SPACE_6, pady=SPACE_3)
        ctk.CTkLabel(window, text=translate("new_thread_aos_note", self.language), font=FONT_SMALL, text_color=COLORS.secondary_text, justify="left", anchor="w", wraplength=500).grid(row=3, column=0, sticky="ew", padx=SPACE_6, pady=SPACE_2)
        actions = ctk.CTkFrame(window, fg_color="transparent")
        actions.grid(row=4, column=0, sticky="ew", padx=SPACE_6, pady=SPACE_4)
        ctk.CTkButton(actions, text=translate("open_codex", self.language), command=self._open_codex).grid(row=0, column=0, padx=(0, SPACE_2))
        ctk.CTkButton(actions, text=translate("copy_generic_template", self.language), command=self._copy_generic_handoff, fg_color="transparent", border_width=1, border_color=COLORS.border, text_color=COLORS.primary_text).grid(row=0, column=1)

    def _copy_generic_handoff(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(generic_handoff_template(self.language))
        self.status_message_var.set(translate("generic_template_copied", self.language))
        self.header_message_label.configure(text_color=COLORS.real)

    def _open_codex(self) -> None:
        result = open_codex()
        self.status_message_var.set(translate("codex_opened" if result.ok else "codex_open_failed", self.language))
        self.header_message_label.configure(
            text_color=COLORS.real if result.ok else COLORS.error,
        )

    def _open_data_directory(self) -> None:
        result = open_data_directory()
        self.status_message_var.set(translate("data_directory_opened" if result.ok else "data_directory_open_failed", self.language))
        self.header_message_label.configure(
            text_color=COLORS.real if result.ok else COLORS.error,
        )

    def _show_privacy_boundary(self) -> None:
        messagebox.showinfo(
            translate("privacy_boundary", self.language),
            translate("privacy_boundary_body", self.language),
            parent=self.root,
        )

    def _show_user_guide(self) -> None:
        messagebox.showinfo(
            translate("user_guide", self.language),
            translate("user_guide_body", self.language),
            parent=self.root,
        )

    def _show_about(self) -> None:
        messagebox.showinfo(
            translate("about_and_version", self.language),
            translate("about_body", self.language),
            parent=self.root,
        )

    def start_diagnostics(self) -> None:
        self.show_page("tools")
        session_count = len(self.snapshot.recent_sessions) if self.snapshot is not None else 0
        context = DiagnosticContext(
            version=__version__,
            runtime_mode=self.window_mode,
            frozen=bool(getattr(sys, "frozen", False)),
            codex_executable_found=find_codex_executable() is not None,
            quota_probe=lambda: self.quota_provider.refresh().source_status,
            rollout_root=configured_sessions_dir(),
            rollout_probe=lambda: session_count,
            state_path=configured_state_path(),
            settings_path=UI_SETTINGS_PATH,
            startup_status=lambda: self.startup_adapter.path_status(sys.executable),
            tray_started=self.tray.started,
            refreshed_at=(self.snapshot.sessions_result.refreshed_at if self.snapshot is not None else None),
        )
        self.diagnostic_report = run_diagnostics(context)
        self._render_diagnostics()
        self._show_diagnostic_dialog()

    def _render_diagnostics(self) -> None:
        if not hasattr(self, "diagnostic_summary_var"):
            return
        report = self.diagnostic_report
        if report is None:
            self.diagnostic_summary_var.set(translate("diagnostics_not_run", self.language))
        else:
            self.diagnostic_summary_var.set(
                translate("diagnostics_all_normal", self.language)
                if report.problem_count == 0
                else translate("diagnostics_problem_count", self.language, count=report.problem_count)
            )
        self.diagnostic_view_button.configure(
            state="normal" if report is not None else "disabled",
        )
        if getattr(self, "diagnostic_window", None) is not None:
            self._render_diagnostic_dialog()

    def _show_diagnostic_dialog(self) -> None:
        if self.diagnostic_report is None:
            return
        existing = getattr(self, "diagnostic_window", None)
        if existing is not None and existing.winfo_exists():
            self._render_diagnostic_dialog()
            existing.lift()
            return
        window = self.diagnostic_window = ctk.CTkToplevel(self.root)
        window.title(translate("diagnostics_title", self.language))
        window.geometry("680x600")
        window.minsize(560, 460)
        if self.root.winfo_viewable():
            window.transient(self.root)
        window.grab_set()
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(2, weight=1)
        self.diagnostic_dialog_time_var = tk.StringVar(master=window, value="")
        self.diagnostic_dialog_counts_var = tk.StringVar(master=window, value="")
        ctk.CTkLabel(
            window, textvariable=self.diagnostic_dialog_time_var,
            font=SECTION_TITLE, text_color=COLORS.primary_text, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=SPACE_6, pady=(SPACE_6, SPACE_1))
        ctk.CTkLabel(
            window, textvariable=self.diagnostic_dialog_counts_var,
            font=BODY, text_color=COLORS.secondary_text, anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=SPACE_6, pady=(0, SPACE_3))
        results = ctk.CTkScrollableFrame(
            window, fg_color=COLORS.raised_surface, corner_radius=CARD_RADIUS,
        )
        results.grid(row=2, column=0, sticky="nsew", padx=SPACE_6)
        results.grid_columnconfigure(1, weight=1)
        self.diagnostic_dialog_rows = []
        for row in range(len(DIAGNOSTIC_CHECK_CODES)):
            name = ctk.CTkLabel(
                results, text="", font=BODY_STRONG,
                text_color=COLORS.primary_text, anchor="w",
            )
            detail = ctk.CTkLabel(
                results, text="", font=BODY,
                text_color=COLORS.secondary_text, anchor="w", justify="left",
                wraplength=390,
            )
            name.grid(row=row, column=0, sticky="nw", padx=SPACE_3, pady=SPACE_2)
            detail.grid(row=row, column=1, sticky="ew", padx=SPACE_3, pady=SPACE_2)
            self.diagnostic_dialog_rows.append((name, detail))
        actions = ctk.CTkFrame(window, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="e", padx=SPACE_6, pady=SPACE_4)
        self.diagnostic_rerun_button = ctk.CTkButton(
            actions, text="", command=self.start_diagnostics, width=140,
        )
        self.diagnostic_rerun_button.grid(row=0, column=0, padx=(0, SPACE_2))
        self.diagnostic_close_button = ctk.CTkButton(
            actions, text="", command=self._close_diagnostic_dialog,
            width=100, fg_color="transparent", border_width=1,
            border_color=COLORS.border, text_color=COLORS.primary_text,
        )
        self.diagnostic_close_button.grid(row=0, column=1)
        window.protocol("WM_DELETE_WINDOW", self._close_diagnostic_dialog)
        self._render_diagnostic_dialog()

    def _render_diagnostic_dialog(self) -> None:
        report = self.diagnostic_report
        rows = getattr(self, "diagnostic_dialog_rows", None)
        if report is None or rows is None:
            return
        self.diagnostic_window.title(translate("diagnostics_title", self.language))
        self.diagnostic_dialog_time_var.set(translate(
            "diagnostic_runtime", self.language,
            value=report.observed_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        counts = {
            status: sum(item.status == status for item in report.results)
            for status in ("normal", "warning", "failure")
        }
        self.diagnostic_dialog_counts_var.set(translate(
            "diagnostic_counts", self.language, **counts,
        ))
        for (name, detail), result in zip(rows, report.results):
            name.configure(text=translate(f"diagnostic_name_{result.code}", self.language))
            detail.configure(
                text=f"{translate(f'diagnostic_status_{result.status}', self.language)} · {translate(result.detail_key, self.language)}",
                text_color={
                    "normal": COLORS.real, "warning": COLORS.orange,
                    "failure": COLORS.error, "unused": COLORS.unknown,
                }[result.status],
            )
        self.diagnostic_rerun_button.configure(text=translate("rerun_diagnostics", self.language))
        self.diagnostic_close_button.configure(text=translate("close", self.language))

    def _close_diagnostic_dialog(self) -> None:
        window = getattr(self, "diagnostic_window", None)
        if window is None:
            return
        try:
            window.grab_release()
            window.destroy()
        except tk.TclError:
            pass
        self.diagnostic_window = None

    def _render_sessions(self, presentation: DashboardPresentation, render_session_rows: bool = True) -> None:
        self._rendering_sessions = True
        try:
            self._render_sessions_inner(presentation, render_session_rows)
        finally:
            self._rendering_sessions = False

    def _render_sessions_inner(self, presentation: DashboardPresentation, render_session_rows: bool = True) -> None:
        all_rows = self._filtered_history_rows(presentation)
        self.current_page, page_count, start, end = pagination_bounds(len(all_rows), self.current_page, self.page_size)
        page_rows = all_rows[start:end]
        labels = disambiguated_session_labels(all_rows, self.language)
        self.selectable_thread_ids = {
            row.thread_id for row in presentation.recent_sessions
            if row.status != "unavailable"
        }
        page_ids = {row.thread_id for row in page_rows}
        selected_id = self.snapshot.selected_thread_id if self.snapshot else None
        menu_ids = page_ids | ({selected_id} if selected_id else set())
        self.label_to_thread = {labels[thread_id]: thread_id for thread_id in labels if thread_id in self.selectable_thread_ids and thread_id in menu_ids}
        auto_label = translate("auto_follow", self.language)
        values = [auto_label, *(labels[thread_id] for thread_id in labels if thread_id in self.selectable_thread_ids and thread_id in menu_ids)]
        if selected_id and selected_id not in labels and self.snapshot.selected_session:
            session = self.snapshot.selected_session
            if session.title_source == "safe timestamp fallback":
                stamp = session.observed_at.astimezone().strftime("%m-%d %H:%M")
                label = f"Codex 会话 · {stamp}" if self.language == "zh-CN" else f"Codex Session · {stamp}"
            else:
                label = f"{session.display_title} · {session.observed_at.astimezone().strftime('%H:%M')}"
            labels[selected_id] = label
            self.label_to_thread[label] = selected_id
            values.append(label)
        self.task_menu.configure(values=values)
        self.task_menu.set(auto_label if not self.snapshot or self.snapshot.selection_mode == "auto" else labels.get(selected_id, "—"))
        if not render_session_rows:
            return
        for item in self.sessions_tree.get_children():
            self.sessions_tree.delete(item)
        for row in page_rows:
            status = localize_presenter_text(row.status, self.language)
            activity = row.last_activity.astimezone().strftime("%m-%d %H:%M:%S") if row.last_activity else "—"
            self.sessions_tree.insert(
                "", "end", iid=row.thread_id,
                values=(
                    labels[row.thread_id], status, activity,
                    row.turn_count or "—", row.thread_total, row.cache_hit,
                ),
            )
        if selected_id in page_ids:
            self.sessions_tree.selection_set(selected_id)
            self.sessions_tree.focus(selected_id)
        self.page_status_var.set(translate("page_status", self.language, current=self.current_page, total=page_count, count=len(all_rows)))
        self.previous_page_button.configure(state="normal" if self.current_page > 1 else "disabled")
        self.next_page_button.configure(state="normal" if self.current_page < page_count else "disabled")

    def _filtered_history_rows(
        self, presentation: DashboardPresentation,
    ) -> tuple:
        rows = presentation.recent_sessions
        query = self.session_search_var.get().strip().casefold() if hasattr(self, "session_search_var") else ""
        if query:
            rows = tuple(
                row for row in rows
                if query in (row.full_title or row.display_title).casefold()
            )
        if self.status_filter == "all":
            return rows
        groups = {
            "running": {"in_progress"},
            "completed": {"exact", "completed_partial"},
            "attention": {"incomplete", "unavailable"},
        }
        allowed = groups.get(self.status_filter, set())
        return tuple(row for row in rows if row.status in allowed)

    def _previous_page(self) -> None:
        if self.presentation is not None and self.current_page > 1:
            self.current_page -= 1
            self._render_sessions(self.presentation)

    def _next_page(self) -> None:
        if self.presentation is not None:
            _, count, _, _ = pagination_bounds(
                len(self._filtered_history_rows(self.presentation)),
                self.current_page, self.page_size,
            )
            if self.current_page < count:
                self.current_page += 1
                self._render_sessions(self.presentation)

    def _recent_sessions_note(self) -> str:
        truncated = bool(self.snapshot and self.snapshot.sessions_result.candidate_truncated)
        return translate("recent_sessions_note_truncated" if truncated else "recent_sessions_note", self.language)

def build_dashboard() -> ctk.CTk:
    root = ctk.CTk()
    Dashboard(root)
    return root


def smoke() -> None:
    history = UsageHistoryStore()
    if not history.initialize():
        raise RuntimeError(history.last_error or "history_initialize_failed")
    snapshot = DashboardViewModel().refresh()
    presentation = present_dashboard(snapshot, False)
    _safe_print("Codex Token Monitor smoke OK")
    _safe_print(f"data_status={presentation.data_status.value}")
    _safe_print(f"session_total={presentation.telemetry_session_total}")
    _safe_print(f"current_total={presentation.telemetry_current_total}")
    _safe_print(f"sessions={len(snapshot.recent_sessions)}")
    _safe_print(f"rollout_available={snapshot.rollout.available}")


def _safe_print(message: str) -> None:
    if sys.stdout is not None:
        print(message)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="Run a non-GUI smoke check.")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()
    if args.smoke:
        smoke()
        return
    with SingleInstanceGuard() as instance:
        if not instance.acquire():
            _safe_print(translate("already_running"))
            return
        build_dashboard().mainloop()


if __name__ == "__main__":
    main()
