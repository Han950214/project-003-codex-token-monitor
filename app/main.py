"""Localized multi-session Windows Dashboard for Codex Token Monitor."""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auto_refresh import AutoRefreshController, DEFAULT_AUTO_REFRESH_SECONDS
from app.advisor import AdvisorResult, build_advisor_input, evaluate_advice
from app.app_actions import open_codex, open_data_directory
from app.codex_rollout import configured_sessions_dir
from app.codex_state import configured_state_path
from app.dashboard import DashboardViewModel, MiniThreadSnapshot, display_session_status
from app.dashboard_mode import AppShellState, NAVIGATION_ITEMS, normalize_dashboard_mode
from app.desktop_widget import (
    DesktopMiniWidget, ExitChoiceDialog, format_percent, format_reset_time,
    format_token_total,
)
from app.diagnostics import (
    DIAGNOSTIC_CHECK_CODES, DiagnosticContext, DiagnosticReport, run_diagnostics,
)
from app.i18n import (
    LANGUAGE_LABELS, language_from_label, localize_auto_refresh,
    localize_presenter_label, localize_presenter_text, localize_status, translate,
)
from app.paths import ui_settings_path
from app.quota import CodexQuotaSnapshot
from app.quota_provider import CodexAppServerQuotaProvider, QuotaProvider, find_codex_executable
from app.single_instance import SingleInstanceGuard
from app.version import __version__
from app.new_thread import generic_handoff_template
from app.startup_settings import StartupSettingsDialog
from app.system_tray import SystemTrayController
from app.telemetry_bar import TelemetryBar, build_telemetry_values
from app.ui_presenter import (
    DashboardPresentation, disambiguated_session_labels, present_dashboard,
)
from app.ui_settings import (
    LanguageController, clear_widget_position, load_auto_refresh_enabled,
    load_dashboard_mode, load_exit_action_for_today, load_exit_behavior,
    load_startup_mode, load_widget_idle_opacity, load_widget_mode,
    save_auto_refresh_enabled, save_dashboard_mode, save_exit_behavior,
    save_startup_mode, save_widget_idle_opacity, save_widget_mode,
)
from app.ui_theme import (
    CARD_RADIUS, COLORS, CONTROL_RADIUS, FONT_BODY, FONT_FAMILY,
    FONT_SECTION, FONT_SMALL, FONT_TITLE, METRIC_ACCENTS, METRIC_ICONS,
    SPACE_1, SPACE_2, SPACE_3, SPACE_4, SPACE_6, TONE_COLORS, configure_view,
)
from app.windows_startup import WindowsStartupAdapter


UI_SETTINGS_PATH = ui_settings_path()
SESSION_COLUMNS = ("Name", "Status", "Activity", "Tokens", "Cache")
SESSION_COLUMN_KEYS = (
    "column_session_name", "column_status", "column_last_activity",
    "column_session_tokens", "column_session_cache_hit",
)


def pagination_bounds(item_count: int, current_page: int, page_size: int = 10) -> tuple[int, int, int, int]:
    page_count = max(1, (max(0, item_count) + page_size - 1) // page_size)
    page = min(max(1, current_page), page_count)
    start = (page - 1) * page_size
    return page, page_count, start, min(start + page_size, max(0, item_count))


class Dashboard:
    def __init__(self, root: ctk.CTk, quota_provider: QuotaProvider | None = None) -> None:
        self.root = root
        configure_view(root)
        self.quota_provider = quota_provider or CodexAppServerQuotaProvider()
        title_loader = getattr(self.quota_provider, "refresh_thread_titles", lambda: {})
        self.view_model = DashboardViewModel(title_batch_loader=title_loader)
        self.language_controller = LanguageController(self._apply_language, UI_SETTINGS_PATH)
        self.language = self.language_controller.language
        self.dashboard_mode = load_dashboard_mode(UI_SETTINGS_PATH)
        self.widget_display_mode = load_widget_mode(UI_SETTINGS_PATH)
        self.current_nav_page = "status_center"
        self.shell_state = AppShellState(
            dashboard_mode=self.dashboard_mode,
            widget_mode=self.widget_display_mode,
            auto_refresh_enabled=load_auto_refresh_enabled(UI_SETTINGS_PATH),
        )
        self.snapshot = None
        self.presentation: DashboardPresentation | None = None
        self.advisor_result: AdvisorResult | None = None
        self.diagnostic_report: DiagnosticReport | None = None
        self.lookback_days = 7
        self.label_to_thread: dict[str, str] = {}
        self.selectable_thread_ids: set[str] = set()
        self._rendering_sessions = False
        self._selection_refresh_pending = False
        self.current_page = 1
        self.page_size = 10
        self._widget_mode = False
        self._taskbar_mode = False
        self._tray_mode = False
        self.window_mode = "dashboard"
        self._closing = False
        self._taskbar_iconify_scheduled = False
        self._widget_thread_id: str | None = None
        self._last_dashboard_geometry = "1180x760"
        self._mini_thread_snapshot = MiniThreadSnapshot("", None, None, "no_selection", None)
        self.quota_snapshot = CodexQuotaSnapshot.unavailable()

        self.auto_refresh_var = tk.BooleanVar(master=root, value=self.shell_state.auto_refresh_enabled)
        self.data_status_var = tk.StringVar(value="")
        self.status_message_var = tk.StringVar(value="")
        self.last_event_var = tk.StringVar(value="—")
        self.last_refresh_var = tk.StringVar(value="—")
        self.task_label_var = tk.StringVar(value="")
        self.metric_widgets: list[dict[str, object]] = []
        self.source_widgets: dict[str, dict[str, object]] = {}
        self.page_frames: dict[str, ctk.CTkFrame] = {}
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        self.advanced_recent_vars: list[tk.StringVar] = []
        self.diagnostic_rows: list[tuple[ctk.CTkLabel, ctk.CTkLabel]] = []
        self.startup_adapter = WindowsStartupAdapter()

        root.title("Codex Token Monitor")
        root.geometry("1180x760")
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
        self.root.grid_columnconfigure(0, minsize=190)
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
        self.show_page("status_center")

    def _build_sidebar(self) -> None:
        sidebar = ctk.CTkFrame(
            self.root, width=190, corner_radius=0,
            fg_color=COLORS.telemetry,
        )
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_columnconfigure(0, weight=1)
        sidebar.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(
            sidebar, text="◆", font=(FONT_FAMILY, 24, "bold"),
            text_color="#7AA5FF",
        ).grid(row=0, column=0, sticky="w", padx=SPACE_4, pady=(SPACE_6, 0))
        ctk.CTkLabel(
            sidebar, text="Codex Token\nMonitor", font=(FONT_FAMILY, 17, "bold"),
            justify="left", anchor="w", text_color=COLORS.telemetry_text,
        ).grid(row=1, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_1, SPACE_6))
        nav = ctk.CTkFrame(sidebar, fg_color="transparent")
        nav.grid(row=2, column=0, sticky="new", padx=SPACE_2)
        nav.grid_columnconfigure(0, weight=1)
        for row, page in enumerate(NAVIGATION_ITEMS):
            button = ctk.CTkButton(
                nav, text="", command=lambda target=page: self.show_page(target),
                height=40, anchor="w", corner_radius=CONTROL_RADIUS,
                fg_color="transparent", hover_color="#263B56",
                text_color=COLORS.telemetry_text, font=FONT_BODY,
            )
            button.grid(row=row, column=0, sticky="ew", pady=2)
            self.nav_buttons[page] = button
        footer = ctk.CTkFrame(sidebar, fg_color="#132237", corner_radius=0)
        footer.grid(row=3, column=0, sticky="sew")
        footer.grid_columnconfigure(0, weight=1)
        self.nav_connection_var = tk.StringVar(master=self.root, value="—")
        self.nav_version_var = tk.StringVar(master=self.root, value=f"v{__version__}")
        self.nav_mode_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(footer, textvariable=self.nav_connection_var, font=FONT_SMALL, text_color="#B9C7D9", anchor="w").grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_1))
        ctk.CTkLabel(footer, textvariable=self.nav_version_var, font=FONT_SMALL, text_color=COLORS.telemetry_muted, anchor="w").grid(row=1, column=0, sticky="ew", padx=SPACE_4)
        ctk.CTkLabel(footer, textvariable=self.nav_mode_var, font=FONT_SMALL, text_color=COLORS.telemetry_muted, anchor="w").grid(row=2, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_1, SPACE_3))

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self.main_container, fg_color="transparent", corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
        header.grid_columnconfigure(0, weight=1)
        self.page_title_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(
            header, textvariable=self.page_title_var, font=FONT_TITLE,
            text_color=COLORS.primary_text, anchor="w",
        ).grid(row=0, column=0, sticky="w")

        primary_actions = ctk.CTkFrame(header, fg_color="transparent")
        primary_actions.grid(row=0, column=1, sticky="e")
        self.mode_switch = ctk.CTkSegmentedButton(
            primary_actions, values=["Simple", "Advanced"], command=self._change_dashboard_mode,
            width=144, height=32, selected_color=COLORS.accent,
            selected_hover_color=COLORS.accent_hover,
            unselected_color=COLORS.surface,
            unselected_hover_color=COLORS.accent_soft,
            text_color=COLORS.primary_text,
        )
        self.mode_switch.grid(row=0, column=0, padx=(0, SPACE_1))
        self.refresh_button = ctk.CTkButton(primary_actions, text="", command=self.manual_refresh, width=100, height=34, corner_radius=CONTROL_RADIUS, fg_color="transparent", border_width=1, border_color=COLORS.accent, text_color=COLORS.accent, hover_color=COLORS.accent_soft)
        self.refresh_button.grid(row=0, column=1, padx=(0, SPACE_1))
        self.mini_widget_button = ctk.CTkButton(primary_actions, text="▣", command=self._enter_widget_mode, width=34, height=34, corner_radius=CONTROL_RADIUS, fg_color=COLORS.accent, text_color="#FFFFFF", hover_color=COLORS.accent_hover)
        self.mini_widget_button.grid(row=0, column=2)

        secondary_actions = ctk.CTkFrame(header, fg_color="transparent")
        secondary_actions.grid(row=1, column=0, columnspan=2, sticky="e", pady=(SPACE_1, 0))
        self.auto_switch = ctk.CTkSwitch(secondary_actions, text="", variable=self.auto_refresh_var, command=self._toggle_auto_refresh, width=148, font=FONT_SMALL, progress_color=COLORS.real, button_color=COLORS.surface, button_hover_color=COLORS.raised_surface)
        self.auto_switch.grid(row=0, column=0, padx=(0, SPACE_1))
        self.language_menu = ctk.CTkOptionMenu(secondary_actions, values=list(LANGUAGE_LABELS.values()), command=self._change_language, width=100, height=34, corner_radius=CONTROL_RADIUS, fg_color=COLORS.surface, button_color=COLORS.raised_surface, button_hover_color=COLORS.border, text_color=COLORS.primary_text, dropdown_fg_color=COLORS.surface, dropdown_text_color=COLORS.primary_text, dropdown_hover_color=COLORS.accent_soft)
        self.language_menu.grid(row=0, column=1)

    def _build_content(self) -> None:
        host = ctk.CTkFrame(self.main_container, fg_color="transparent", corner_radius=0)
        host.grid(row=1, column=0, sticky="nsew", padx=SPACE_4, pady=(0, SPACE_3))
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)
        self.page_host = host
        for page in NAVIGATION_ITEMS:
            frame = ctk.CTkFrame(host, fg_color="transparent", corner_radius=0)
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_columnconfigure(0, weight=1)
            frame.grid_rowconfigure(0, weight=1)
            self.page_frames[page] = frame
        self._build_status_center(self.page_frames["status_center"])
        self._build_current_task_page(self.page_frames["current_task"])
        self._build_history_page(self.page_frames["history"])
        self._build_tools_page(self.page_frames["tools"])
        self._build_settings_page(self.page_frames["settings"])

    def _build_status_center(self, parent: ctk.CTkFrame) -> None:
        self.simple_page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        self.simple_page.grid(row=0, column=0, sticky="nsew")
        self.simple_page.grid_columnconfigure((0, 1), weight=1, uniform="simple")
        self._build_simple_status_center(self.simple_page)

        self.advanced_page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        self.advanced_page.grid(row=0, column=0, sticky="nsew")
        self.advanced_page.grid_columnconfigure(0, weight=1)
        self.latest_title = ctk.CTkLabel(self.advanced_page, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w", height=30)
        self.latest_title.grid(row=0, column=0, sticky="ew", pady=(0, SPACE_2))
        self._build_metric_cards(self.advanced_page)
        self.sources_title = ctk.CTkLabel(self.advanced_page, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.sources_title.grid(row=2, column=0, sticky="ew", pady=(SPACE_3, SPACE_2))
        self._build_source_panel(self.advanced_page)
        self._build_advanced_advice(self.advanced_page)
        self._build_advanced_recent(self.advanced_page)
        self.telemetry = TelemetryBar(self.advanced_page, self.language)
        self.telemetry.grid(row=6, column=0, sticky="ew", pady=(SPACE_2, 0))

    def _build_simple_status_center(self, parent: ctk.CTkScrollableFrame) -> None:
        self.simple_status_title_var = tk.StringVar(master=self.root, value="")
        self.simple_reason_var = tk.StringVar(master=self.root, value="")
        status = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        status.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, SPACE_3))
        status.grid_columnconfigure(0, weight=1)
        self.simple_status_accent = ctk.CTkFrame(status, width=6, fg_color=COLORS.real, corner_radius=3)
        self.simple_status_accent.grid(row=0, column=0, rowspan=3, sticky="nsw")
        ctk.CTkLabel(status, textvariable=self.simple_status_title_var, font=(FONT_FAMILY, 23, "bold"), text_color=COLORS.primary_text, anchor="w").grid(row=0, column=0, sticky="ew", padx=SPACE_6, pady=(SPACE_4, SPACE_1))
        ctk.CTkLabel(status, textvariable=self.simple_reason_var, font=FONT_BODY, text_color=COLORS.secondary_text, anchor="w", justify="left", wraplength=680).grid(row=1, column=0, sticky="ew", padx=SPACE_6)
        actions = ctk.CTkFrame(status, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="w", padx=SPACE_6, pady=(SPACE_3, SPACE_4))
        self.primary_action_button = ctk.CTkButton(actions, text="", command=self._execute_primary_action, width=148, height=38, fg_color=COLORS.accent, hover_color=COLORS.accent_hover)
        self.primary_action_button.grid(row=0, column=0, padx=(0, SPACE_2))
        self.reason_button = ctk.CTkButton(actions, text="", command=self._show_reason, width=100, height=38, fg_color="transparent", border_width=1, border_color=COLORS.border, text_color=COLORS.primary_text, hover_color=COLORS.accent_soft)
        self.reason_button.grid(row=0, column=1)

        quota = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        quota.grid(row=1, column=0, sticky="nsew", padx=(0, SPACE_2))
        quota.grid_columnconfigure(1, weight=1)
        self.simple_quota_title = ctk.CTkLabel(quota, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.simple_quota_title.grid(row=0, column=0, columnspan=2, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
        self.simple_quota_vars = {name: tk.StringVar(master=self.root, value="—") for name in ("five_remaining", "five_reset", "week_remaining", "week_reset")}
        self.simple_quota_labels: dict[str, ctk.CTkLabel] = {}
        for row, name in enumerate(self.simple_quota_vars, start=1):
            label = ctk.CTkLabel(quota, text="", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
            label.grid(row=row, column=0, sticky="w", padx=(SPACE_4, SPACE_2), pady=SPACE_1)
            self.simple_quota_labels[name] = label
            ctk.CTkLabel(quota, textvariable=self.simple_quota_vars[name], font=(FONT_FAMILY, 12, "bold"), text_color=COLORS.primary_text, anchor="e").grid(row=row, column=1, sticky="e", padx=SPACE_4, pady=SPACE_1)

        task = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border, cursor="hand2")
        task.grid(row=1, column=1, sticky="nsew", padx=(SPACE_2, 0))
        task.grid_columnconfigure(1, weight=1)
        self.simple_task_title = ctk.CTkLabel(task, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.simple_task_title.grid(row=0, column=0, columnspan=2, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
        self.simple_task_vars = {name: tk.StringVar(master=self.root, value="—") for name in ("title", "status", "turns", "instruction", "session", "activity")}
        self.simple_task_labels: dict[str, ctk.CTkLabel] = {}
        for row, name in enumerate(self.simple_task_vars, start=1):
            label = ctk.CTkLabel(task, text="", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
            label.grid(row=row, column=0, sticky="w", padx=(SPACE_4, SPACE_2), pady=2)
            self.simple_task_labels[name] = label
            value = ctk.CTkLabel(task, textvariable=self.simple_task_vars[name], font=FONT_SMALL, text_color=COLORS.primary_text, anchor="e", wraplength=240)
            value.grid(row=row, column=1, sticky="e", padx=SPACE_4, pady=2)
            value.bind("<Button-1>", lambda _event: self.show_page("current_task"))
        for widget in (task, self.simple_task_title):
            widget.bind("<Button-1>", lambda _event: self.show_page("current_task"))

        quick = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        quick.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(SPACE_3, 0))
        quick.grid_columnconfigure((0, 1, 2), weight=1, uniform="quick")
        self.quick_title = ctk.CTkLabel(quick, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.quick_title.grid(row=0, column=0, columnspan=3, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
        self.quick_diagnose = ctk.CTkButton(quick, text="", command=self.start_diagnostics, fg_color=COLORS.accent, hover_color=COLORS.accent_hover)
        self.quick_codex = ctk.CTkButton(quick, text="", command=self._open_codex, fg_color=COLORS.raised_surface, text_color=COLORS.primary_text, hover_color=COLORS.accent_soft)
        self.quick_history = ctk.CTkButton(quick, text="", command=lambda: self.show_page("history"), fg_color=COLORS.raised_surface, text_color=COLORS.primary_text, hover_color=COLORS.accent_soft)
        for column, button in enumerate((self.quick_diagnose, self.quick_codex, self.quick_history)):
            button.grid(row=1, column=column, sticky="ew", padx=(SPACE_4 if column == 0 else SPACE_1, SPACE_4 if column == 2 else SPACE_1), pady=(0, SPACE_2))
        self.quick_more = ctk.CTkButton(quick, text="", command=lambda: self.show_page("tools"), height=26, fg_color="transparent", text_color=COLORS.accent, hover_color=COLORS.accent_soft)
        self.quick_more.grid(row=2, column=2, sticky="e", padx=SPACE_4, pady=(0, SPACE_2))

    def _build_advanced_advice(self, parent: ctk.CTkScrollableFrame) -> None:
        panel = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        panel.grid(row=4, column=0, sticky="ew", pady=(SPACE_2, 0))
        panel.grid_columnconfigure(0, weight=1)
        self.advanced_advice_title = ctk.CTkLabel(panel, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.advanced_advice_title.grid(row=0, column=0, sticky="ew", padx=SPACE_3, pady=(SPACE_2, SPACE_1))
        self.advanced_advice_vars: list[tk.StringVar] = []
        for row in range(3):
            value = tk.StringVar(master=self.root, value="—")
            self.advanced_advice_vars.append(value)
            ctk.CTkLabel(panel, textvariable=value, font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w", justify="left", wraplength=760).grid(row=row + 1, column=0, sticky="ew", padx=SPACE_3, pady=2)

    def _build_advanced_recent(self, parent: ctk.CTkScrollableFrame) -> None:
        panel = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        panel.grid(row=5, column=0, sticky="ew", pady=(SPACE_2, 0))
        panel.grid_columnconfigure(0, weight=1)
        self.advanced_recent_title = ctk.CTkLabel(panel, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.advanced_recent_title.grid(row=0, column=0, sticky="ew", padx=SPACE_3, pady=(SPACE_2, SPACE_1))
        for row in range(3):
            value = tk.StringVar(master=self.root, value="—")
            self.advanced_recent_vars.append(value)
            ctk.CTkLabel(panel, textvariable=value, font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w").grid(row=row + 1, column=0, sticky="ew", padx=SPACE_3, pady=2)
        self.advanced_history_button = ctk.CTkButton(panel, text="", command=lambda: self.show_page("history"), width=120, height=28, fg_color="transparent", border_width=1, border_color=COLORS.border, text_color=COLORS.accent)
        self.advanced_history_button.grid(row=4, column=0, sticky="e", padx=SPACE_3, pady=(SPACE_1, SPACE_2))

    def _build_current_task_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        self.task_detail_vars = {name: tk.StringVar(master=self.root, value="—") for name in (
            "title", "status", "activity", "turns", "instruction", "session",
            "cache", "quota", "advice",
        )}
        card = ctk.CTkFrame(page, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        card.grid(row=0, column=0, sticky="ew")
        card.grid_columnconfigure(1, weight=1)
        self.task_detail_labels: dict[str, ctk.CTkLabel] = {}
        for row, name in enumerate(self.task_detail_vars):
            label = ctk.CTkLabel(card, text="", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
            label.grid(row=row, column=0, sticky="w", padx=SPACE_4, pady=(SPACE_3 if row == 0 else SPACE_1))
            self.task_detail_labels[name] = label
            ctk.CTkLabel(card, textvariable=self.task_detail_vars[name], font=(FONT_FAMILY, 13, "bold" if name in {"title", "advice"} else "normal"), text_color=COLORS.primary_text, anchor="w", justify="left", wraplength=600).grid(row=row, column=1, sticky="ew", padx=SPACE_4, pady=(SPACE_3 if row == 0 else SPACE_1))
        actions = ctk.CTkFrame(page, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", pady=SPACE_3)
        self.task_refresh_button = ctk.CTkButton(actions, text="", command=self.manual_refresh)
        self.task_back_button = ctk.CTkButton(actions, text="", command=lambda: self.show_page("status_center"), fg_color="transparent", border_width=1, border_color=COLORS.border, text_color=COLORS.primary_text)
        self.task_switch_button = ctk.CTkButton(actions, text="", command=lambda: self.show_page("history"), fg_color="transparent", border_width=1, border_color=COLORS.border, text_color=COLORS.primary_text)
        self.task_new_thread_button = ctk.CTkButton(actions, text="", command=self._show_new_thread_dialog, fg_color=COLORS.orange, hover_color=COLORS.estimate)
        self.task_advanced_button = ctk.CTkButton(actions, text="", command=self._show_advanced_numbers, fg_color="transparent", border_width=1, border_color=COLORS.accent, text_color=COLORS.accent)
        for column, button in enumerate((self.task_refresh_button, self.task_back_button, self.task_switch_button, self.task_new_thread_button, self.task_advanced_button)):
            button.grid(row=0, column=column, padx=(0, SPACE_2))

    def _build_history_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        selector = ctk.CTkFrame(page, fg_color="transparent")
        selector.grid(row=0, column=0, sticky="ew", pady=(0, SPACE_2))
        self.task_selector_label = ctk.CTkLabel(selector, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.task_selector_label.grid(row=0, column=0, padx=(0, SPACE_2))
        self.task_menu = ctk.CTkOptionMenu(selector, values=["—"], command=self._select_task, variable=self.task_label_var, width=360)
        self.task_menu.grid(row=0, column=1, sticky="w")
        self.range_selector_label = ctk.CTkLabel(selector, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.range_selector_label.grid(row=0, column=2, padx=(SPACE_3, SPACE_2))
        self.range_menu = ctk.CTkOptionMenu(selector, values=["—"], command=self._change_time_range, width=130)
        self.range_menu.grid(row=0, column=3, sticky="w")
        self._build_recent_sessions(page, row=1)

    def _build_tools_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        card = ctk.CTkFrame(page, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        card.grid(row=0, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        self.diagnostic_title = ctk.CTkLabel(card, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.diagnostic_title.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, 0))
        self.diagnostic_summary_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(card, textvariable=self.diagnostic_summary_var, font=(FONT_FAMILY, 17, "bold"), text_color=COLORS.primary_text, anchor="w").grid(row=1, column=0, sticky="ew", padx=SPACE_4, pady=SPACE_2)
        self.diagnostic_run_button = ctk.CTkButton(card, text="", command=self.start_diagnostics, width=140)
        self.diagnostic_run_button.grid(row=1, column=1, padx=SPACE_4, pady=SPACE_2)
        details = ctk.CTkFrame(card, fg_color=COLORS.raised_surface, corner_radius=CONTROL_RADIUS)
        details.grid(row=2, column=0, columnspan=2, sticky="ew", padx=SPACE_4, pady=(0, SPACE_4))
        details.grid_columnconfigure(0, minsize=210)
        details.grid_columnconfigure(1, weight=1)
        for row in range(13):
            name = ctk.CTkLabel(details, text="—", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
            status = ctk.CTkLabel(details, text="—", font=FONT_SMALL, text_color=COLORS.unknown, anchor="w")
            name.grid(row=row, column=0, sticky="ew", padx=SPACE_3, pady=2)
            status.grid(row=row, column=1, sticky="ew", padx=SPACE_3, pady=2)
            self.diagnostic_rows.append((name, status))
        tools = ctk.CTkFrame(page, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        tools.grid(row=1, column=0, sticky="ew", pady=(SPACE_3, 0))
        tools.grid_columnconfigure((0, 1, 2), weight=1, uniform="tools")
        self.tools_title = ctk.CTkLabel(tools, text="", font=FONT_SECTION, anchor="w")
        self.tools_title.grid(row=0, column=0, columnspan=3, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
        self.tool_open_codex = ctk.CTkButton(tools, text="", command=self._open_codex)
        self.tool_open_data = ctk.CTkButton(tools, text="", command=self._open_data_directory)
        self.tool_new_thread = ctk.CTkButton(tools, text="", command=self._show_new_thread_dialog)
        self.tool_redetect = ctk.CTkButton(tools, text="", command=self.manual_refresh)
        self.tool_privacy = ctk.CTkButton(tools, text="", command=self._show_privacy_boundary)
        self.tool_update = ctk.CTkButton(tools, text="", command=lambda: None, state="disabled")
        for index, button in enumerate((self.tool_open_codex, self.tool_open_data, self.tool_new_thread, self.tool_redetect, self.tool_privacy, self.tool_update)):
            button.grid(row=1 + index // 3, column=index % 3, sticky="ew", padx=SPACE_2, pady=SPACE_2)
        self.update_note_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(tools, textvariable=self.update_note_var, font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w").grid(row=3, column=0, columnspan=3, sticky="ew", padx=SPACE_4, pady=(SPACE_1, SPACE_3))

    def _build_settings_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(1, weight=1)
        self.settings_labels: dict[str, ctk.CTkLabel] = {}
        fields = ("language", "startup_mode", "dashboard_mode", "widget_mode", "auto_refresh", "exit_behavior", "widget_idle_opacity", "start_with_windows")
        for row, name in enumerate(fields):
            label = ctk.CTkLabel(page, text="", font=FONT_BODY, text_color=COLORS.primary_text, anchor="w")
            label.grid(row=row, column=0, sticky="w", padx=(0, SPACE_4), pady=SPACE_2)
            self.settings_labels[name] = label
        self.settings_language_menu = ctk.CTkOptionMenu(page, values=list(LANGUAGE_LABELS.values()), command=self._change_language, width=260)
        self.settings_language_menu.grid(row=0, column=1, sticky="w", pady=SPACE_2)
        self.settings_startup_menu = ctk.CTkOptionMenu(page, values=["—"], command=self._settings_startup_changed, width=260)
        self.settings_startup_menu.grid(row=1, column=1, sticky="w", pady=SPACE_2)
        self.settings_dashboard_menu = ctk.CTkOptionMenu(page, values=["—"], command=self._change_dashboard_mode, width=260)
        self.settings_dashboard_menu.grid(row=2, column=1, sticky="w", pady=SPACE_2)
        self.settings_widget_menu = ctk.CTkOptionMenu(page, values=["—"], command=self._settings_widget_changed, width=260)
        self.settings_widget_menu.grid(row=3, column=1, sticky="w", pady=SPACE_2)
        self.settings_auto_switch = ctk.CTkSwitch(page, text="", variable=self.auto_refresh_var, command=self._toggle_auto_refresh)
        self.settings_auto_switch.grid(row=4, column=1, sticky="w", pady=SPACE_2)
        self.settings_exit_menu = ctk.CTkOptionMenu(page, values=["—"], command=self._settings_exit_changed, width=260)
        self.settings_exit_menu.grid(row=5, column=1, sticky="w", pady=SPACE_2)
        self.settings_opacity_var = tk.DoubleVar(master=self.root, value=load_widget_idle_opacity(UI_SETTINGS_PATH))
        opacity = ctk.CTkFrame(page, fg_color="transparent")
        opacity.grid(row=6, column=1, sticky="ew", pady=SPACE_2)
        self.settings_opacity_value = ctk.CTkLabel(opacity, text="", width=50)
        self.settings_opacity_value.grid(row=0, column=0, padx=(0, SPACE_2))
        ctk.CTkSlider(opacity, from_=0.30, to=0.95, number_of_steps=13, variable=self.settings_opacity_var, command=self._settings_opacity_changed, width=260).grid(row=0, column=1)
        self.settings_startup_var = tk.BooleanVar(master=self.root, value=self.startup_adapter.is_enabled(sys.executable))
        self.settings_startup_switch = ctk.CTkSwitch(page, text="", variable=self.settings_startup_var, command=self._settings_windows_startup_changed)
        self.settings_startup_switch.grid(row=7, column=1, sticky="w", pady=SPACE_2)
        if not self.startup_adapter.is_supported():
            self.settings_startup_switch.configure(state="disabled")
        self.settings_note_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(page, textvariable=self.settings_note_var, font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w", justify="left", wraplength=620).grid(row=8, column=0, columnspan=2, sticky="ew", pady=(SPACE_3, 0))

    def _build_metric_cards(self, parent: ctk.CTkFrame) -> None:
        cards = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        cards.grid(row=1, column=0, sticky="ew", pady=(0, SPACE_2))
        for column, label in enumerate(METRIC_ICONS):
            cards.grid_columnconfigure(column, weight=1, uniform="metric")
            accent, soft = METRIC_ACCENTS[label]
            card = ctk.CTkFrame(cards, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=2 if label == "Current Total" else 1, border_color=accent if label == "Current Total" else COLORS.border)
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else SPACE_1, 0), pady=1)
            card.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(card, text=METRIC_ICONS[label], width=38, height=38, corner_radius=19, fg_color=soft, text_color=accent, font=(FONT_FAMILY, 19, "bold")).grid(row=0, column=0, rowspan=2, padx=(SPACE_3, SPACE_2), pady=(SPACE_3, SPACE_1))
            label_var, value_var, detail_var = tk.StringVar(value=label), tk.StringVar(value="—"), tk.StringVar(value="")
            ctk.CTkLabel(card, textvariable=label_var, font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w").grid(row=0, column=1, sticky="sw", padx=(0, SPACE_2), pady=(SPACE_2, 0))
            value_label = ctk.CTkLabel(card, textvariable=value_var, font=(FONT_FAMILY, 18, "bold"), text_color=accent, anchor="w")
            value_label.grid(row=1, column=1, sticky="nw", padx=(0, SPACE_2))
            ctk.CTkLabel(card, textvariable=detail_var, font=(FONT_FAMILY, 10), text_color=COLORS.secondary_text, anchor="w", justify="left", wraplength=120).grid(row=2, column=0, columnspan=2, sticky="ew", padx=SPACE_3, pady=(SPACE_1, SPACE_2))
            self.metric_widgets.append({"semantic": label, "label_var": label_var, "value_var": value_var, "detail_var": detail_var, "value_label": value_label, "accent": accent})

    def _build_source_panel(self, parent: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        panel.grid(row=3, column=0, sticky="ew")
        labels = ("Data Source", "Current Task", "Model Calls", "Task Elapsed", "Data Sync")
        for column, label in enumerate(labels):
            panel.grid_columnconfigure(column, weight=1, uniform="source")
            cell = ctk.CTkFrame(panel, fg_color="transparent")
            cell.grid(row=0, column=column, sticky="nsew", padx=SPACE_3, pady=SPACE_2)
            label_var, value_var = tk.StringVar(value=label), tk.StringVar(value="—")
            ctk.CTkLabel(cell, textvariable=label_var, font=(FONT_FAMILY, 10), text_color=COLORS.secondary_text, anchor="w").grid(row=0, column=0, sticky="ew")
            value_label = ctk.CTkLabel(cell, textvariable=value_var, font=(FONT_FAMILY, 11, "bold"), text_color=COLORS.unknown, anchor="w")
            value_label.grid(row=1, column=0, sticky="ew")
            self.source_widgets[label] = {"label_var": label_var, "value_var": value_var, "value_label": value_label}

    def _build_recent_sessions(self, parent: ctk.CTkFrame, row: int = 5) -> None:
        panel = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
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
        widths = (420, 100, 150, 130, 130)
        for column, width in zip(SESSION_COLUMNS, widths):
            self.sessions_tree.heading(column, text=column)
            self.sessions_tree.column(column, width=width, minwidth=80, anchor="e" if column in {"Tokens", "Cache"} else "w")
        self.sessions_tree.bind("<<TreeviewSelect>>", self._select_recent_row)
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
        self.refresh_button.configure(text=translate("manual_refresh", language))
        self.auto_switch.configure(text=localize_auto_refresh(bool(self.auto_refresh_var.get()), language, DEFAULT_AUTO_REFRESH_SECONDS))
        self.language_menu.set(LANGUAGE_LABELS[language])
        self.settings_language_menu.set(LANGUAGE_LABELS[language])
        for page in NAVIGATION_ITEMS:
            self.nav_buttons[page].configure(text=translate(f"nav_{page}", language))
        self.nav_version_var.set(translate("app_version_value", language, version=__version__))
        self.nav_mode_var.set(translate("footer_mode_value", language, mode=translate(f"mode_{self.dashboard_mode}", language)))
        mode_values = [translate("mode_simple", language), translate("mode_advanced", language)]
        self.mode_switch.configure(values=mode_values)
        self.mode_switch.set(translate(f"mode_{self.dashboard_mode}", language))
        self._update_page_title()

        self.simple_quota_title.configure(text=translate("quota_card_title", language))
        self.simple_task_title.configure(text=translate("current_task_card_title", language))
        self.quick_title.configure(text=translate("quick_actions_title", language))
        self.reason_button.configure(text=translate("view_reason", language))
        self.quick_diagnose.configure(text=translate("one_click_diagnostics", language))
        self.quick_codex.configure(text=translate("open_codex", language))
        self.quick_history.configure(text=translate("view_history", language))
        self.quick_more.configure(text=translate("more", language))
        simple_quota_keys = {
            "five_remaining": "five_hour_remaining",
            "five_reset": "five_hour_reset",
            "week_remaining": "weekly_remaining",
            "week_reset": "weekly_reset",
        }
        for name, key in simple_quota_keys.items():
            self.simple_quota_labels[name].configure(text=translate(key, language))
        simple_task_keys = {
            "title": "task_title", "status": "task_status", "turns": "task_turns",
            "instruction": "instruction_usage_simple", "session": "session_usage_simple",
            "activity": "recent_activity",
        }
        for name, key in simple_task_keys.items():
            self.simple_task_labels[name].configure(text=translate(key, language))

        self.task_selector_label.configure(text=translate("monitored_task", language))
        self.range_selector_label.configure(text=translate("time_range", language))
        range_values = [translate(key, language) for key in ("last_7_days", "last_30_days", "last_90_days")]
        self.range_menu.configure(values=range_values)
        self.range_menu.set(translate(f"last_{self.lookback_days}_days", language))
        self.latest_title.configure(text=self._usage_scope_title(self.presentation, language))
        self.sources_title.configure(text=translate("session_sources", language))
        self.advanced_recent_title.configure(text=translate("recent_sessions", language))
        self.advanced_advice_title.configure(text=translate("workflow_advice", language))
        self.advanced_history_button.configure(text=translate("view_history", language))
        self.recent_title.configure(text=translate("recent_sessions", language))
        self.recent_note.configure(text=self._recent_sessions_note())
        self.previous_page_button.configure(text=translate("previous_page", language))
        self.next_page_button.configure(text=translate("next_page", language))
        for column, key in zip(SESSION_COLUMNS, SESSION_COLUMN_KEYS):
            self.sessions_tree.heading(column, text=translate(key, language))
        for widget in self.metric_widgets:
            widget["label_var"].set(localize_presenter_label(widget["semantic"], language))
        for semantic, widget in self.source_widgets.items():
            widget["label_var"].set(localize_presenter_label(semantic, language))

        task_detail_keys = {
            "title": "task_title", "status": "task_status", "activity": "recent_activity",
            "turns": "task_turns", "instruction": "instruction_usage_simple",
            "session": "session_usage_simple", "cache": "cache_reuse_simple",
            "quota": "quota_status", "advice": "current_advice",
        }
        for name, key in task_detail_keys.items():
            self.task_detail_labels[name].configure(text=translate(key, language))
        for button, key in (
            (self.task_refresh_button, "manual_refresh"),
            (self.task_back_button, "back_status_center"),
            (self.task_switch_button, "switch_task"),
            (self.task_new_thread_button, "prepare_new_thread"),
            (self.task_advanced_button, "view_advanced_numbers"),
        ):
            button.configure(text=translate(key, language))

        self.diagnostic_title.configure(text=translate("diagnostics_title", language))
        self.diagnostic_run_button.configure(text=translate("run_diagnostics", language))
        self.tools_title.configure(text=translate("tools_actions_title", language))
        for button, key in (
            (self.tool_open_codex, "open_codex"),
            (self.tool_open_data, "open_data_directory"),
            (self.tool_new_thread, "prepare_new_thread"),
            (self.tool_redetect, "redetect_sources"),
            (self.tool_privacy, "privacy_boundary"),
            (self.tool_update, "check_updates"),
        ):
            button.configure(text=translate(key, language))
        self.update_note_var.set(translate("update_placeholder", language, version=__version__))

        settings_keys = {
            "language": "language", "startup_mode": "default_startup_mode",
            "dashboard_mode": "dashboard_default_mode", "widget_mode": "widget_default_mode",
            "auto_refresh": "auto_refresh_setting", "exit_behavior": "exit_behavior",
            "widget_idle_opacity": "widget_idle_opacity", "start_with_windows": "start_with_windows",
        }
        for name, key in settings_keys.items():
            self.settings_labels[name].configure(text=translate(key, language))
        self.settings_auto_switch.configure(text=translate("enabled" if self.auto_refresh_var.get() else "disabled", language))
        self.settings_startup_switch.configure(text=translate("enabled" if self.settings_startup_var.get() else "disabled", language))
        self.settings_note_var.set(translate("settings_no_refresh_note", language))
        self.settings_opacity_value.configure(text=f"{round(self.settings_opacity_var.get() * 100):.0f}%")
        self._configure_settings_menus()

        if self.presentation is not None:
            self._apply_presentation(self.presentation)
        else:
            self._render_advisor()
        self._render_diagnostics()
        if hasattr(self, "mini_widget") and self.mini_widget.visible:
            self.mini_widget.update(
                self.quota_snapshot,
                self._mini_thread_snapshot,
                language,
                self.advisor_result.primary if self.advisor_result is not None else None,
            )
        if hasattr(self, "tray"):
            self.tray.update(language=language, auto_refresh_enabled=bool(self.auto_refresh_var.get()))

    def _configure_settings_menus(self) -> None:
        language = self.language
        self.startup_labels = {
            translate("startup_dashboard", language): "dashboard",
            translate("startup_widget", language): "widget",
            translate("startup_tray", language): "tray",
        }
        self.dashboard_mode_labels = {
            translate("mode_simple", language): "simple",
            translate("mode_advanced", language): "advanced",
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
        self.settings_dashboard_menu.configure(values=list(self.dashboard_mode_labels))
        self.settings_widget_menu.configure(values=list(self.widget_mode_labels))
        self.settings_exit_menu.configure(values=list(self.exit_behavior_labels))
        self.settings_startup_menu.set(next(label for label, value in self.startup_labels.items() if value == load_startup_mode(UI_SETTINGS_PATH)))
        self.settings_dashboard_menu.set(next(label for label, value in self.dashboard_mode_labels.items() if value == self.dashboard_mode))
        self.settings_widget_menu.set(next(label for label, value in self.widget_mode_labels.items() if value == self.widget_display_mode))
        exit_behavior = load_exit_behavior(UI_SETTINGS_PATH)
        self.settings_exit_menu.set(next(label for label, value in self.exit_behavior_labels.items() if value == exit_behavior))

    def show_page(self, page: str) -> None:
        target = page if page in NAVIGATION_ITEMS else "status_center"
        self.shell_state = self.shell_state.navigate(target)
        self.current_nav_page = target
        for item, frame in self.page_frames.items():
            if item == target:
                frame.grid()
            else:
                frame.grid_remove()
            self.nav_buttons[item].configure(
                fg_color="#284664" if item == target else "transparent",
                text_color=COLORS.telemetry_text,
            )
        if target == "status_center":
            self._render_dashboard_mode()
        self._update_page_title()

    def _update_page_title(self) -> None:
        if hasattr(self, "page_title_var"):
            self.page_title_var.set(translate(f"nav_{self.current_nav_page}", self.language))

    def _change_dashboard_mode(self, selected: str) -> None:
        mode = getattr(self, "dashboard_mode_labels", {}).get(selected, selected)
        if selected == translate("mode_simple", self.language):
            mode = "simple"
        elif selected == translate("mode_advanced", self.language):
            mode = "advanced"
        self.set_dashboard_mode(mode)

    def set_dashboard_mode(self, mode: str) -> None:
        self.dashboard_mode = normalize_dashboard_mode(mode)
        self.shell_state = self.shell_state.with_dashboard_mode(self.dashboard_mode)
        save_dashboard_mode(self.dashboard_mode, UI_SETTINGS_PATH)
        self.mode_switch.set(translate(f"mode_{self.dashboard_mode}", self.language))
        self.nav_mode_var.set(translate("footer_mode_value", self.language, mode=translate(f"mode_{self.dashboard_mode}", self.language)))
        if hasattr(self, "settings_dashboard_menu") and hasattr(self, "dashboard_mode_labels"):
            self.settings_dashboard_menu.set(next(label for label, value in self.dashboard_mode_labels.items() if value == self.dashboard_mode))
        self._render_dashboard_mode()

    def _show_advanced_numbers(self) -> None:
        self.set_dashboard_mode("advanced")
        self.show_page("status_center")

    def _render_dashboard_mode(self) -> None:
        if not hasattr(self, "simple_page"):
            return
        if self.dashboard_mode == "advanced":
            self.simple_page.grid_remove()
            self.advanced_page.grid()
        else:
            self.advanced_page.grid_remove()
            self.simple_page.grid()

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
        if snapshot is None:
            self.refresh(refresh_quota=False)
        else:
            self._apply_cached_snapshot(snapshot)

    def _change_time_range(self, label: str) -> None:
        labels = {translate(f"last_{days}_days", self.language): days for days in (7, 30, 90)}
        days = labels.get(label)
        if days is not None and self.view_model.set_lookback_days(days):
            self.refresh(refresh_quota=False)

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
            if snapshot is None:
                self.refresh(show_refreshing=False, refresh_quota=False)
            else:
                self._apply_cached_snapshot(snapshot)

    def _refresh_selected_task(self) -> None:
        self._selection_refresh_pending = False
        self.refresh(show_refreshing=False, render_session_rows=False)

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

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
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
        self.advisor_result = evaluate_advice(build_advisor_input(self.snapshot, self.quota_snapshot))
        self.presentation = present_dashboard(self.snapshot, bool(self.auto_refresh_var.get()))
        self._apply_presentation(self.presentation, render_session_rows=render_session_rows)

    def _apply_cached_snapshot(self, snapshot) -> None:
        self.snapshot = snapshot
        self.advisor_result = evaluate_advice(build_advisor_input(self.snapshot, self.quota_snapshot))
        self.presentation = present_dashboard(snapshot, bool(self.auto_refresh_var.get()))
        self._apply_presentation(self.presentation)

    def _on_root_configure(self, event: object) -> None:
        if getattr(event, "widget", None) is self.root and not self._widget_mode:
            try:
                if self.root.state() == "normal":
                    self._last_dashboard_geometry = self.root.geometry()
            except tk.TclError:
                pass

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
        )

    def _apply_presentation(self, presentation: DashboardPresentation, render_session_rows: bool = True) -> None:
        self.data_status_var.set(localize_status(presentation.data_status, self.language))
        self.status_message_var.set(localize_presenter_text(presentation.status_message, self.language))
        self.last_event_var.set(presentation.last_event)
        self.last_refresh_var.set(presentation.last_refresh)
        self.latest_title.configure(text=self._usage_scope_title(presentation, self.language))
        self.recent_note.configure(text=self._recent_sessions_note())
        for widget, metric in zip(self.metric_widgets, presentation.latest_usage):
            widget["label_var"].set(localize_presenter_label(metric.label, self.language))
            widget["value_var"].set(metric.value)
            widget["detail_var"].set(localize_presenter_text(metric.detail, self.language))
            color = TONE_COLORS[metric.tone.value][0] if metric.tone.value in {"error", "unknown"} else widget["accent"]
            widget["value_label"].configure(text_color=color)
        for source in presentation.source_details:
            widget = self.source_widgets[source.label]
            value = localize_presenter_text(source.value, self.language)
            if source.label == "Model Calls" and value != "—":
                value = f"{value} 次" if self.language == "zh-CN" else f"{value} calls"
            if source.label == "Task Elapsed":
                value = self._localized_duration(source.value)
            widget["value_var"].set(value)
            widget["value_label"].configure(text_color=TONE_COLORS[source.tone.value][0])
        self._render_sessions(presentation, render_session_rows=render_session_rows)
        self.telemetry.update_values(build_telemetry_values(presentation, self.language))
        self._render_advisor()
        self._render_safe_overview()
        self._render_advanced_recent(presentation)

    def _render_advisor(self) -> None:
        if self.advisor_result is None:
            return
        recommendation = self.advisor_result.primary
        title = translate(recommendation.title_key, self.language)
        body = translate(recommendation.body_key, self.language)
        self.simple_status_title_var.set(
            translate("current_status_value", self.language, status=title)
        )
        self.simple_reason_var.set(body)
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
        self.simple_status_accent.configure(fg_color=color)
        for index, variable in enumerate(self.advanced_advice_vars):
            if index >= len(self.advisor_result.recommendations):
                variable.set(translate("no_additional_advice", self.language) if index == 0 else "")
                continue
            item = self.advisor_result.recommendations[index]
            variable.set(f"• {translate(item.title_key, self.language)} — {translate(item.body_key, self.language)}")
        connected = recommendation.status != "data_unavailable"
        self.nav_connection_var.set(translate(
            "connection_normal" if connected else "connection_abnormal", self.language
        ))

    def _render_safe_overview(self) -> None:
        quota = self.quota_snapshot
        for prefix, window in (("five", quota.five_hour), ("week", quota.weekly)):
            remaining = format_percent(window.remaining_percent) if window.available and not window.stale else "—"
            reset = format_reset_time(window.reset_at, self.language, window.observed_at) if window.reset_at is not None else "—"
            self.simple_quota_vars[f"{prefix}_remaining"].set(remaining)
            self.simple_quota_vars[f"{prefix}_reset"].set(reset)

        selected = self.snapshot.selected_session if self.snapshot is not None else None
        if selected is None:
            values = {
                "title": translate("no_selected_thread", self.language),
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
            values = {
                "title": selected.display_title or translate("no_selected_thread", self.language),
                "status": localize_presenter_text(status, self.language),
                "turns": str(getattr(selected, "turn_count", 0)) if getattr(selected, "turn_count", 0) else "—",
                "instruction": format_token_total(usage.total_tokens if usage is not None else None),
                "session": format_token_total(cumulative.total_tokens if cumulative is not None else None),
                "activity": selected.observed_at.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            }
        for name, value in values.items():
            self.simple_task_vars[name].set(value)
        self.task_detail_vars["title"].set(values["title"])
        self.task_detail_vars["status"].set(values["status"])
        self.task_detail_vars["activity"].set(values["activity"])
        self.task_detail_vars["turns"].set(values["turns"])
        self.task_detail_vars["instruction"].set(values["instruction"])
        self.task_detail_vars["session"].set(values["session"])
        self.task_detail_vars["cache"].set(
            translate("derived_percent_value", self.language, value=cache) if cache != "—" else "—"
        )
        quota_value = f"{self.simple_quota_vars['five_remaining'].get()} / {self.simple_quota_vars['week_remaining'].get()}"
        self.task_detail_vars["quota"].set(quota_value)
        if self.advisor_result is not None:
            self.task_detail_vars["advice"].set(translate(self.advisor_result.primary.title_key, self.language))

    def _render_advanced_recent(self, presentation: DashboardPresentation) -> None:
        labels = disambiguated_session_labels(presentation.recent_sessions, self.language)
        for index, variable in enumerate(self.advanced_recent_vars):
            if index >= len(presentation.recent_sessions):
                variable.set("—")
                continue
            row = presentation.recent_sessions[index]
            activity = row.last_activity.astimezone().strftime("%m-%d %H:%M") if row.last_activity else "—"
            variable.set(f"{labels[row.thread_id]}  ·  {localize_presenter_text(row.status, self.language)}  ·  {activity}")

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
            self.show_page("status_center")
        else:
            self.show_page("current_task")

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

    def _open_codex(self) -> None:
        result = open_codex()
        self.status_message_var.set(translate("codex_opened" if result.ok else "codex_open_failed", self.language))

    def _open_data_directory(self) -> None:
        result = open_data_directory()
        self.status_message_var.set(translate("data_directory_opened" if result.ok else "data_directory_open_failed", self.language))

    def _show_privacy_boundary(self) -> None:
        messagebox.showinfo(
            translate("privacy_boundary", self.language),
            translate("privacy_boundary_body", self.language),
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

    def _render_diagnostics(self) -> None:
        if not hasattr(self, "diagnostic_summary_var"):
            return
        report = self.diagnostic_report
        if report is None:
            self.diagnostic_summary_var.set(translate("diagnostics_not_run", self.language))
            results = ()
        else:
            self.diagnostic_summary_var.set(
                translate("diagnostics_all_normal", self.language)
                if report.problem_count == 0
                else translate("diagnostics_problem_count", self.language, count=report.problem_count)
            )
            results = report.results
        for index, (name_label, status_label) in enumerate(self.diagnostic_rows):
            if index >= len(results):
                name_label.configure(text=translate(f"diagnostic_name_{DIAGNOSTIC_CHECK_CODES[index]}", self.language))
                status_label.configure(text="—", text_color=COLORS.unknown)
                continue
            result = results[index]
            name_label.configure(text=translate(f"diagnostic_name_{result.code}", self.language))
            status_label.configure(
                text=f"{translate(f'diagnostic_status_{result.status}', self.language)} · {translate(result.detail_key, self.language)}",
                text_color={
                    "normal": COLORS.real, "warning": COLORS.orange,
                    "failure": COLORS.error, "unused": COLORS.unknown,
                }[result.status],
            )

    def _render_sessions(self, presentation: DashboardPresentation, render_session_rows: bool = True) -> None:
        self._rendering_sessions = True
        try:
            self._render_sessions_inner(presentation, render_session_rows)
        finally:
            self._rendering_sessions = False

    def _render_sessions_inner(self, presentation: DashboardPresentation, render_session_rows: bool = True) -> None:
        all_rows = presentation.recent_sessions
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
            self.sessions_tree.insert("", "end", iid=row.thread_id, values=(labels[row.thread_id], status, activity, row.thread_total, row.cache_hit))
        if selected_id in page_ids:
            self.sessions_tree.selection_set(selected_id)
            self.sessions_tree.focus(selected_id)
        self.page_status_var.set(translate("page_status", self.language, current=self.current_page, total=page_count, count=len(all_rows)))
        self.previous_page_button.configure(state="normal" if self.current_page > 1 else "disabled")
        self.next_page_button.configure(state="normal" if self.current_page < page_count else "disabled")

    def _previous_page(self) -> None:
        if self.presentation is not None and self.current_page > 1:
            self.current_page -= 1
            self._render_sessions(self.presentation)

    def _next_page(self) -> None:
        if self.presentation is not None:
            _, count, _, _ = pagination_bounds(len(self.presentation.recent_sessions), self.current_page, self.page_size)
            if self.current_page < count:
                self.current_page += 1
                self._render_sessions(self.presentation)

    def _localized_duration(self, value: str) -> str:
        if value in {"—", "Calculating"}:
            return localize_presenter_text(value, self.language)
        if self.language != "zh-CN":
            return value
        if "m " in value:
            minutes, seconds = value.replace("s", "").split("m ")
            return f"{minutes}分{seconds}秒"
        return value.replace("s", "秒")

    def _recent_sessions_note(self) -> str:
        truncated = bool(self.snapshot and self.snapshot.sessions_result.candidate_truncated)
        return translate("recent_sessions_note_truncated" if truncated else "recent_sessions_note", self.language)

    @staticmethod
    def _usage_scope_title(presentation: DashboardPresentation | None, language: str) -> str:
        scope = presentation.usage_scope if presentation is not None else "instruction"
        key = "thread_cumulative_usage_title" if scope == "thread_cumulative" else "latest_usage"
        return translate(key, language)


def build_dashboard() -> ctk.CTk:
    root = ctk.CTk()
    Dashboard(root)
    return root


def smoke() -> None:
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
