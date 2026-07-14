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
from app.windows_startup import WindowsStartupAdapter


UI_SETTINGS_PATH = ui_settings_path()
SESSION_COLUMNS = ("Name", "Status", "Activity", "Tokens", "Cache")
SESSION_COLUMN_KEYS = (
    "column_session_name", "column_status", "column_last_activity",
    "column_session_tokens", "column_session_cache_hit",
)
CORE_METRICS = (
    "current_turn", "session_total", "cache_reuse", "reasoning", "quota_remaining",
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
        self.widget_display_mode = load_widget_mode(UI_SETTINGS_PATH)
        self.current_nav_page = "status_center"
        self.shell_state = AppShellState(
            widget_mode=self.widget_display_mode,
            auto_refresh_enabled=load_auto_refresh_enabled(UI_SETTINGS_PATH),
        )
        self.snapshot = None
        self.presentation: DashboardPresentation | None = None
        self.advisor_result: AdvisorResult | None = None
        self.diagnostic_report: DiagnosticReport | None = None
        self.lookback_days = 7
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
        self.show_page("status_center")

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
            "status_center": "home", "history": "history",
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
        self._build_status_center(self.page_frames["status_center"])
        self._build_current_task_page(self.page_frames["current_task"])
        self._build_history_page(self.page_frames["history"])
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
        self.task_summary_card = self._build_task_summary_card(page)
        self.quota_center_card = self._build_quota_center_card(page)
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
            COLORS.accent, COLORS.purple, COLORS.real, COLORS.orange, COLORS.teal,
        )
        softs = (
            COLORS.accent_soft, COLORS.purple_soft, COLORS.real_soft,
            COLORS.orange_soft, COLORS.teal_soft,
        )
        for semantic, accent, soft in zip(CORE_METRICS, accents, softs):
            card = ctk.CTkFrame(
                self.core_cards_frame, fg_color=COLORS.raised_surface,
                corner_radius=CONTROL_RADIUS, border_width=1,
                border_color=COLORS.border, width=96, height=180,
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
            elif semantic == "quota_remaining":
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
            actions, text="", command=lambda: self.show_page("history"), width=96,
            height=30, fg_color="transparent", border_width=1,
            border_color=COLORS.border, text_color=COLORS.primary_text,
            hover_color=COLORS.accent_soft,
        )
        self.task_switch_button_home.grid(row=0, column=0, padx=(0, SPACE_2))
        self.task_detail_button_home = ctk.CTkButton(
            actions, text="", command=lambda: self.show_page("current_task"),
            width=104, height=30, fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
        )
        self.task_detail_button_home.grid(row=0, column=1)
        for widget in (card, title, self.simple_task_title):
            widget.bind("<Button-1>", lambda _event: self.show_page("current_task"))
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
            header, text="", command=lambda: self.show_page("current_task"),
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
            compound="left", command=lambda: self.show_page("history"),
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
            header, text="", command=lambda: self.show_page("history"), width=110,
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
            self.task_summary_card, self.quota_center_card,
            self.quick_actions_card, self.status_recent_card,
        )
        for card in cards:
            card.grid_forget()
        mode = dashboard_layout_for_width(content_width)
        for column in range(12):
            self.status_page.grid_columnconfigure(
                column, weight=0, uniform="", minsize=0,
            )
        if mode == "wide":
            half_width = max(320, (content_width - SPACE_2) // 2)
            for column in (0, 1):
                self.status_page.grid_columnconfigure(
                    column, weight=1, uniform="dashboard", minsize=half_width,
                )
            # Tk's uniform sizing does not constrain widgets that span several
            # columns. Real two-column placement plus fixed section requests
            # keeps both halves equal even when child widgets request more.
            for card, height in zip(cards, (260, 260, 260, 260, 360, 360)):
                card.configure(width=half_width, height=height)
                card.grid_propagate(False)
            for card, row, column in (
                (self.status_advice_card, 0, 0),
                (self.core_metrics_panel, 0, 1),
                (self.task_summary_card, 1, 0),
                (self.quota_center_card, 1, 1),
                (self.quick_actions_card, 2, 0),
                (self.status_recent_card, 2, 1),
            ):
                card.grid(
                    row=row, column=column, sticky="nsew",
                    padx=(0 if column == 0 else SPACE_2, 0),
                    pady=(0, SPACE_3),
                )
        elif mode == "medium":
            for card in cards:
                card.grid_propagate(True)
            self.status_page.grid_columnconfigure(0, weight=1, uniform="dashboard")
            self.status_page.grid_columnconfigure(1, weight=1, uniform="dashboard")
            self.status_advice_card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, SPACE_3))
            self.core_metrics_panel.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, SPACE_3))
            for card, row, column in (
                (self.task_summary_card, 2, 0), (self.quota_center_card, 2, 1),
                (self.quick_actions_card, 3, 0), (self.status_recent_card, 3, 1),
            ):
                card.grid(
                    row=row, column=column, sticky="nsew",
                    padx=(0 if column == 0 else SPACE_2, 0), pady=(0, SPACE_3),
                )
        else:
            for card in cards:
                card.grid_propagate(True)
            self.status_page.grid_columnconfigure(0, weight=1, uniform="")
            for row, card in enumerate(cards):
                card.grid(row=row, column=0, sticky="ew", pady=(0, SPACE_3))
        core_width = (
            max(320, int(content_width / 2) - SPACE_2)
            if mode == "wide" else content_width
        )
        self._layout_core_metrics(core_width)

    def _layout_core_metrics(self, width: int) -> None:
        columns = (
            5 if getattr(self, "language", "en") == "zh-CN" and width >= 580
            else metric_columns_for_width(width)
        )
        for column in range(5):
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

    def _build_current_task_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkScrollableFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        self.task_back_button = ctk.CTkButton(
            page, text="", command=lambda: self.show_page("status_center"),
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
        self.task_switch_button = ctk.CTkButton(actions, text="", command=lambda: self.show_page("history"), fg_color="transparent", border_width=1, border_color=COLORS.border, text_color=COLORS.primary_text)
        self.task_new_thread_button = ctk.CTkButton(actions, text="", command=self._show_new_thread_dialog, fg_color=COLORS.orange, hover_color=COLORS.estimate)
        for column, button in enumerate((self.task_refresh_button, self.task_switch_button, self.task_new_thread_button)):
            button.grid(row=0, column=column, padx=(0, SPACE_2))

    def _build_history_page(self, parent: ctk.CTkFrame) -> None:
        page = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        page.grid(row=0, column=0, sticky="nsew")
        page.grid_columnconfigure(0, weight=1)
        page.grid_rowconfigure(1, weight=1)
        selector = self.history_selector = ctk.CTkFrame(page, fg_color="transparent")
        selector.grid(row=0, column=0, sticky="ew", pady=(0, SPACE_2))
        self.task_selector_label = ctk.CTkLabel(selector, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.task_selector_label.grid(row=0, column=0, padx=(0, SPACE_2))
        self.task_menu = ctk.CTkOptionMenu(selector, values=["—"], command=self._select_task, variable=self.task_label_var, width=360)
        self.task_menu.grid(row=0, column=1, sticky="w")
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
            selector, text="", command=lambda: self.show_page("current_task"),
            width=100, height=30, fg_color="transparent", border_width=1,
            border_color=COLORS.border, text_color=COLORS.primary_text,
        )
        self.history_detail_button.grid(row=0, column=6, padx=(SPACE_3, 0))
        self._build_recent_sessions(page, row=1)
        self._layout_history_controls(1000)

    def _layout_history_controls(self, content_width: int) -> None:
        """Keep history filters usable without a horizontal scrollbar."""
        controls = (
            self.task_selector_label, self.task_menu,
            self.range_selector_label, self.range_menu,
            self.status_filter_label, self.status_filter_menu,
            self.history_detail_button,
        )
        for control in controls:
            control.grid_forget()
        selector = self.history_selector
        for column in range(7):
            selector.grid_columnconfigure(column, weight=0, uniform="")
        if content_width >= 1040:
            self.task_selector_label.grid(row=0, column=0, padx=(0, SPACE_2))
            self.task_menu.configure(width=320)
            self.task_menu.grid(row=0, column=1, sticky="w")
            self.range_selector_label.grid(row=0, column=2, padx=(SPACE_3, SPACE_2))
            self.range_menu.grid(row=0, column=3, sticky="w")
            self.status_filter_label.grid(row=0, column=4, padx=(SPACE_3, SPACE_2))
            self.status_filter_menu.grid(row=0, column=5, sticky="w")
            self.history_detail_button.grid(row=0, column=6, padx=(SPACE_3, 0))
        else:
            selector.grid_columnconfigure(1, weight=1)
            selector.grid_columnconfigure(3, weight=1)
            self.task_selector_label.grid(row=0, column=0, padx=(0, SPACE_2), pady=(0, SPACE_2))
            self.task_menu.configure(width=360)
            self.task_menu.grid(
                row=0, column=1, columnspan=3, sticky="ew",
                pady=(0, SPACE_2),
            )
            self.history_detail_button.grid(
                row=0, column=4, padx=(SPACE_3, 0), pady=(0, SPACE_2),
            )
            self.range_selector_label.grid(row=1, column=0, padx=(0, SPACE_2))
            self.range_menu.grid(row=1, column=1, sticky="w")
            self.status_filter_label.grid(row=1, column=2, padx=(SPACE_3, SPACE_2))
            self.status_filter_menu.grid(row=1, column=3, sticky="w")

    def _layout_history_columns(self, content_width: int) -> None:
        available = max(560, content_width - 58)
        ratios = (0.40, 0.13, 0.17, 0.15, 0.15)
        for column, ratio in zip(SESSION_COLUMNS, ratios):
            self.sessions_tree.column(
                column, width=max(76, int(available * ratio)),
                minwidth=70,
                anchor="e" if column in {"Tokens", "Cache"} else "w",
            )

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
        fields = (
            "language", "startup_mode", "widget_mode", "auto_refresh",
            "exit_behavior", "widget_idle_opacity", "start_with_windows",
        )
        for row, name in enumerate(fields):
            label = ctk.CTkLabel(page, text="", font=FONT_BODY, text_color=COLORS.primary_text, anchor="w")
            label.grid(row=row, column=0, sticky="w", padx=(0, SPACE_4), pady=SPACE_2)
            self.settings_labels[name] = label
        self.settings_language_menu = ctk.CTkOptionMenu(page, values=list(LANGUAGE_LABELS.values()), command=self._change_language, width=260)
        self.settings_language_menu.grid(row=0, column=1, sticky="w", pady=SPACE_2)
        self.settings_startup_menu = ctk.CTkOptionMenu(page, values=["—"], command=self._settings_startup_changed, width=260)
        self.settings_startup_menu.grid(row=1, column=1, sticky="w", pady=SPACE_2)
        self.settings_widget_menu = ctk.CTkOptionMenu(page, values=["—"], command=self._settings_widget_changed, width=260)
        self.settings_widget_menu.grid(row=2, column=1, sticky="w", pady=SPACE_2)
        self.settings_auto_switch = ctk.CTkSwitch(page, text="", variable=self.auto_refresh_var, command=self._toggle_auto_refresh)
        self.settings_auto_switch.grid(row=3, column=1, sticky="w", pady=SPACE_2)
        self.settings_exit_menu = ctk.CTkOptionMenu(page, values=["—"], command=self._settings_exit_changed, width=260)
        self.settings_exit_menu.grid(row=4, column=1, sticky="w", pady=SPACE_2)
        self.settings_opacity_var = tk.DoubleVar(master=self.root, value=load_widget_idle_opacity(UI_SETTINGS_PATH))
        opacity = ctk.CTkFrame(page, fg_color="transparent")
        opacity.grid(row=5, column=1, sticky="ew", pady=SPACE_2)
        self.settings_opacity_value = ctk.CTkLabel(opacity, text="", width=50)
        self.settings_opacity_value.grid(row=0, column=0, padx=(0, SPACE_2))
        ctk.CTkSlider(opacity, from_=0.30, to=0.95, number_of_steps=13, variable=self.settings_opacity_var, command=self._settings_opacity_changed, width=260).grid(row=0, column=1)
        self.settings_startup_var = tk.BooleanVar(master=self.root, value=self.startup_adapter.is_enabled(sys.executable))
        self.settings_startup_switch = ctk.CTkSwitch(page, text="", variable=self.settings_startup_var, command=self._settings_windows_startup_changed)
        self.settings_startup_switch.grid(row=6, column=1, sticky="w", pady=SPACE_2)
        if not self.startup_adapter.is_supported():
            self.settings_startup_switch.configure(state="disabled")
        self.settings_note_var = tk.StringVar(master=self.root, value="")
        ctk.CTkLabel(page, textvariable=self.settings_note_var, font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w", justify="left", wraplength=620).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(SPACE_3, 0))

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
        self.sessions_tree.bind(
            "<Double-Button-1>", lambda _event: self.show_page("current_task"),
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

        self.simple_task_title.configure(text=translate("current_task_card_title", language))
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
            "one_click_diagnostics", "open_codex", "view_history", "more_tools",
        )):
            button.configure(text=translate(key, language))
        self.status_recent_title.configure(text=translate("recent_tasks_title", language))
        self.status_recent_all.configure(text=translate("view_all_tasks", language))

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
        self.task_back_button.configure(text=translate("back_status_center", language))
        self.task_refresh_button.configure(text=translate("manual_refresh", language))
        self.task_switch_button.configure(text=translate("switch_task", language))
        self.task_new_thread_button.configure(text=translate("prepare_new_thread", language))

        self.task_selector_label.configure(text=translate("monitored_task", language))
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
            "widget_mode": "widget_default_mode", "auto_refresh": "auto_refresh_setting",
            "exit_behavior": "exit_behavior",
            "widget_idle_opacity": "widget_idle_opacity",
            "start_with_windows": "start_with_windows",
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

        if self.presentation is not None:
            self._apply_presentation(self.presentation)
        else:
            self._render_advisor()
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
        target = page if page in ALL_PAGES else "status_center"
        self.shell_state = self.shell_state.navigate(target)
        self.current_nav_page = target
        for item, frame in self.page_frames.items():
            if item == target:
                frame.grid()
            else:
                frame.grid_remove()
        nav_target = "status_center" if target == "current_task" else target
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
            if self._layout_job is not None:
                try:
                    self.root.after_cancel(self._layout_job)
                except tk.TclError:
                    pass
            self._layout_job = self.root.after(90, self._apply_responsive_layout)

    def _apply_responsive_layout(self) -> None:
        self._layout_job = None
        try:
            window_width = max(1, self.root.winfo_width())
        except tk.TclError:
            return
        collapsed = window_width < 1080
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
        layout = dashboard_layout_for_width(content_width)
        reason_width = (
            int(content_width / 2) - 100
            if layout == "wide" else content_width - 64
        )
        self.status_reason_label.configure(wraplength=max(180, reason_width))

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
        self._render_safe_overview()
        self._render_status_recent(presentation)

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
            "quota_remaining": (
                self.simple_quota_vars["five_remaining"].get(),
                self._format_quota_summary(quota.five_hour),
                self.quota_window_widgets["five"]["state"].get(),
                None if quota.five_hour.remaining_percent is None else quota.five_hour.remaining_percent / 100.0,
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
                widget["ring"].set(
                    None if progress_value is None else progress_value * 100.0,
                    color=COLORS.stale if quota.five_hour.stale else COLORS.teal,
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
            self.show_page("current_task")
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
            self.sessions_tree.insert("", "end", iid=row.thread_id, values=(labels[row.thread_id], status, activity, row.thread_total, row.cache_hit))
        if selected_id in page_ids:
            self.sessions_tree.selection_set(selected_id)
            self.sessions_tree.focus(selected_id)
        self.page_status_var.set(translate("page_status", self.language, current=self.current_page, total=page_count, count=len(all_rows)))
        self.previous_page_button.configure(state="normal" if self.current_page > 1 else "disabled")
        self.next_page_button.configure(state="normal" if self.current_page < page_count else "disabled")

    def _filtered_history_rows(
        self, presentation: DashboardPresentation,
    ) -> tuple:
        if self.status_filter == "all":
            return presentation.recent_sessions
        groups = {
            "running": {"in_progress"},
            "completed": {"exact", "completed_partial"},
            "attention": {"incomplete", "unavailable"},
        }
        allowed = groups.get(self.status_filter, set())
        return tuple(row for row in presentation.recent_sessions if row.status in allowed)

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
