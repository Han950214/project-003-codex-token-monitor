"""Localized multi-session Windows Dashboard for Codex Token Monitor."""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import customtkinter as ctk

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auto_refresh import AutoRefreshController, DEFAULT_AUTO_REFRESH_SECONDS
from app.dashboard import DashboardViewModel, MiniThreadSnapshot
from app.desktop_widget import DesktopMiniWidget, ExitChoiceDialog
from app.i18n import (
    LANGUAGE_LABELS, language_from_label, localize_auto_refresh,
    localize_presenter_label, localize_presenter_text, localize_status, translate,
)
from app.paths import ui_settings_path
from app.quota import CodexQuotaSnapshot
from app.quota_provider import CodexAppServerQuotaProvider, QuotaProvider
from app.telemetry_bar import TelemetryBar, build_telemetry_values
from app.ui_presenter import (
    DashboardPresentation, disambiguated_session_labels, present_dashboard,
)
from app.ui_settings import LanguageController, load_exit_action_for_today
from app.ui_theme import (
    CARD_RADIUS, COLORS, CONTROL_RADIUS, FONT_BODY, FONT_FAMILY,
    FONT_SECTION, FONT_SMALL, FONT_TITLE, METRIC_ACCENTS, METRIC_ICONS,
    SPACE_1, SPACE_2, SPACE_3, SPACE_4, TONE_COLORS, configure_view,
)


UI_SETTINGS_PATH = ui_settings_path()
SESSION_COLUMNS = ("Name", "Status", "Activity", "Tokens", "Cache")
SESSION_COLUMN_KEYS = (
    "column_session_name", "column_status", "column_last_activity",
    "column_session_tokens", "column_session_cache_hit",
)


class Dashboard:
    def __init__(self, root: ctk.CTk, quota_provider: QuotaProvider | None = None) -> None:
        self.root = root
        configure_view(root)
        self.view_model = DashboardViewModel()
        self.language_controller = LanguageController(self._apply_language, UI_SETTINGS_PATH)
        self.language = self.language_controller.language
        self.snapshot = None
        self.presentation: DashboardPresentation | None = None
        self.lookback_days = 7
        self.label_to_thread: dict[str, str] = {}
        self.selectable_thread_ids: set[str] = set()
        self._rendering_sessions = False
        self._selection_refresh_pending = False
        self._widget_mode = False
        self._taskbar_mode = False
        self._taskbar_iconify_scheduled = False
        self._widget_thread_id: str | None = None
        self._last_dashboard_geometry = "1180x760"
        self._mini_thread_snapshot = MiniThreadSnapshot("", None, "no_selection", None)
        self.quota_provider = quota_provider or CodexAppServerQuotaProvider()
        self.quota_snapshot = CodexQuotaSnapshot.unavailable()

        self.auto_refresh_var = tk.BooleanVar(value=False)
        self.data_status_var = tk.StringVar(value="")
        self.status_message_var = tk.StringVar(value="")
        self.last_event_var = tk.StringVar(value="—")
        self.last_refresh_var = tk.StringVar(value="—")
        self.task_label_var = tk.StringVar(value="")
        self.metric_widgets: list[dict[str, object]] = []
        self.source_widgets: dict[str, dict[str, object]] = {}

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
            on_exit=self.request_exit,
            on_refresh=self.manual_refresh,
            settings_path=UI_SETTINGS_PATH,
        )
        self.exit_dialog = ExitChoiceDialog(root, UI_SETTINGS_PATH)
        root.protocol("WM_DELETE_WINDOW", self.request_exit)
        root.bind("<Unmap>", self._on_root_unmap, add="+")
        root.bind("<Map>", self._on_root_map, add="+")
        root.bind("<Configure>", self._on_root_configure, add="+")
        self._apply_language(self.language)
        self.refresh()

    def _build(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_rowconfigure(2, weight=0, minsize=64)
        self._build_header()
        self._build_content()
        self.telemetry = TelemetryBar(self.root, self.language)
        self.telemetry.grid(row=2, column=0, sticky="ew")

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self.root, fg_color="transparent", corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_3, SPACE_2))
        header.grid_columnconfigure(0, weight=1)
        identity = ctk.CTkFrame(header, fg_color="transparent")
        identity.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(identity, text="Codex Token Monitor", font=FONT_TITLE, text_color=COLORS.primary_text).grid(row=0, column=0, sticky="w")
        self.status_pill = ctk.CTkLabel(identity, textvariable=self.data_status_var, font=(FONT_FAMILY, 12, "bold"), corner_radius=9, height=30, padx=SPACE_3)
        self.status_pill.grid(row=0, column=1, padx=(SPACE_4, 0), sticky="w")

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.grid(row=0, column=1, sticky="e")
        self.refresh_button = ctk.CTkButton(actions, text="", command=self.manual_refresh, width=112, height=34, corner_radius=CONTROL_RADIUS, fg_color="transparent", border_width=1, border_color=COLORS.accent, text_color=COLORS.accent, hover_color=COLORS.accent_soft)
        self.refresh_button.grid(row=0, column=0, padx=(0, SPACE_2))
        self.mini_widget_button = ctk.CTkButton(actions, text="", command=self._enter_widget_mode, width=112, height=34, corner_radius=CONTROL_RADIUS, fg_color=COLORS.accent, text_color="#FFFFFF", hover_color=COLORS.accent_hover)
        self.mini_widget_button.grid(row=0, column=1, padx=(0, SPACE_2))
        self.auto_switch = ctk.CTkSwitch(actions, text="", variable=self.auto_refresh_var, command=self._toggle_auto_refresh, width=168, font=FONT_SMALL, progress_color=COLORS.real, button_color=COLORS.surface, button_hover_color=COLORS.raised_surface)
        self.auto_switch.grid(row=0, column=2, padx=(0, SPACE_2))
        self.language_menu = ctk.CTkOptionMenu(actions, values=list(LANGUAGE_LABELS.values()), command=self._change_language, width=112, height=34, corner_radius=CONTROL_RADIUS, fg_color=COLORS.surface, button_color=COLORS.raised_surface, button_hover_color=COLORS.border, text_color=COLORS.primary_text, dropdown_fg_color=COLORS.surface, dropdown_text_color=COLORS.primary_text, dropdown_hover_color=COLORS.accent_soft)
        self.language_menu.grid(row=0, column=3)

        selector = ctk.CTkFrame(header, fg_color="transparent")
        selector.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(SPACE_2, 0))
        self.task_selector_label = ctk.CTkLabel(selector, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.task_selector_label.grid(row=0, column=0, padx=(0, SPACE_2))
        self.task_menu = ctk.CTkOptionMenu(selector, values=["—"], command=self._select_task, variable=self.task_label_var, width=360, height=34, corner_radius=CONTROL_RADIUS, fg_color=COLORS.surface, button_color=COLORS.raised_surface, button_hover_color=COLORS.border, text_color=COLORS.primary_text, dropdown_fg_color=COLORS.surface, dropdown_text_color=COLORS.primary_text, dropdown_hover_color=COLORS.accent_soft)
        self.task_menu.grid(row=0, column=1, sticky="w")
        self.range_selector_label = ctk.CTkLabel(selector, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.range_selector_label.grid(row=0, column=2, padx=(SPACE_3, SPACE_2))
        self.range_menu = ctk.CTkOptionMenu(selector, values=["—"], command=self._change_time_range, width=130, height=34, corner_radius=CONTROL_RADIUS, fg_color=COLORS.surface, button_color=COLORS.raised_surface, button_hover_color=COLORS.border, text_color=COLORS.primary_text, dropdown_fg_color=COLORS.surface, dropdown_text_color=COLORS.primary_text, dropdown_hover_color=COLORS.accent_soft)
        self.range_menu.grid(row=0, column=3, sticky="w")
        self.status_message_label = ctk.CTkLabel(selector, textvariable=self.status_message_var, font=FONT_BODY, text_color=COLORS.secondary_text, anchor="w")
        self.status_message_label.grid(row=0, column=4, sticky="ew", padx=(SPACE_3, 0))
        selector.grid_columnconfigure(4, weight=1)

        meta = ctk.CTkFrame(header, fg_color="transparent")
        meta.grid(row=2, column=0, columnspan=2, sticky="w", pady=(SPACE_1, 0))
        self.last_event_title = ctk.CTkLabel(meta, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.last_event_title.grid(row=0, column=0)
        ctk.CTkLabel(meta, textvariable=self.last_event_var, font=FONT_SMALL, text_color=COLORS.primary_text).grid(row=0, column=1, padx=(SPACE_2, SPACE_4))
        self.last_refresh_title = ctk.CTkLabel(meta, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.last_refresh_title.grid(row=0, column=2)
        ctk.CTkLabel(meta, textvariable=self.last_refresh_var, font=FONT_SMALL, text_color=COLORS.primary_text).grid(row=0, column=3, padx=(SPACE_2, 0))

    def _build_content(self) -> None:
        content = ctk.CTkFrame(self.root, fg_color="transparent", corner_radius=0)
        content.grid(row=1, column=0, sticky="nsew", padx=SPACE_4, pady=(0, SPACE_2))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(5, weight=1)
        self.latest_title = ctk.CTkLabel(content, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.latest_title.grid(row=0, column=0, sticky="ew", pady=(0, SPACE_1))
        self._build_metric_cards(content)
        self.sources_title = ctk.CTkLabel(content, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.sources_title.grid(row=2, column=0, sticky="ew", pady=(SPACE_2, SPACE_1))
        self._build_source_panel(content)
        self._build_recent_sessions(content)

    def _build_metric_cards(self, parent: ctk.CTkFrame) -> None:
        cards = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        cards.grid(row=1, column=0, sticky="ew")
        for column, label in enumerate(METRIC_ICONS):
            cards.grid_columnconfigure(column, weight=1, uniform="metric")
            accent, soft = METRIC_ACCENTS[label]
            card = ctk.CTkFrame(cards, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=2 if label == "Total" else 1, border_color=accent if label == "Total" else COLORS.border)
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

    def _build_recent_sessions(self, parent: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        panel.grid(row=5, column=0, sticky="nsew", pady=(SPACE_2, 0))
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

    def _change_language(self, selected: str) -> None:
        self.language_controller.set_language(language_from_label(selected))

    def _apply_language(self, language: str) -> None:
        self.language = language
        if not hasattr(self, "refresh_button"):
            return
        self.refresh_button.configure(text=translate("manual_refresh", language))
        self.mini_widget_button.configure(text=translate("show_mini_widget", language))
        self.auto_switch.configure(text=localize_auto_refresh(bool(self.auto_refresh_var.get()), language, DEFAULT_AUTO_REFRESH_SECONDS))
        self.language_menu.set(LANGUAGE_LABELS[language])
        self.task_selector_label.configure(text=translate("monitored_task", language))
        self.range_selector_label.configure(text=translate("time_range", language))
        range_values = [translate(key, language) for key in ("last_7_days", "last_30_days", "last_90_days")]
        self.range_menu.configure(values=range_values)
        self.range_menu.set(translate(f"last_{self.lookback_days}_days", language))
        self.last_event_title.configure(text=translate("last_event", language))
        self.last_refresh_title.configure(text=translate("last_refresh", language))
        self.latest_title.configure(text=self._usage_scope_title(self.presentation, language))
        self.sources_title.configure(text=translate("session_sources", language))
        self.recent_title.configure(text=translate("recent_sessions", language))
        self.recent_note.configure(text=self._recent_sessions_note())
        for column, key in zip(SESSION_COLUMNS, SESSION_COLUMN_KEYS):
            self.sessions_tree.heading(column, text=translate(key, language))
        for widget in self.metric_widgets:
            widget["label_var"].set(localize_presenter_label(widget["semantic"], language))
        for semantic, widget in self.source_widgets.items():
            widget["label_var"].set(localize_presenter_label(semantic, language))
        if self.presentation is not None:
            self._apply_presentation(self.presentation)
        if hasattr(self, "mini_widget") and self.mini_widget.visible:
            self.mini_widget.update(
                self.quota_snapshot,
                self._mini_thread_snapshot,
                language,
            )

    def _select_task(self, label: str) -> None:
        if label == translate("auto_follow", self.language):
            self.view_model.set_auto_follow()
        else:
            thread_id = self.label_to_thread.get(label)
            if thread_id:
                self.view_model.pin_thread(thread_id)
        self.refresh()

    def _change_time_range(self, label: str) -> None:
        labels = {translate(f"last_{days}_days", self.language): days for days in (7, 30, 90)}
        days = labels.get(label)
        if days is not None and self.view_model.set_lookback_days(days):
            self.refresh()

    def _select_recent_row(self, _event: object) -> None:
        if self._rendering_sessions:
            return
        selected = self.sessions_tree.selection()
        if selected:
            thread_id = selected[0]
            if thread_id not in self.selectable_thread_ids:
                self.sessions_tree.selection_remove(thread_id)
                return
            # selection_set() during rendering must not behave like a user click.
            if self.sessions_tree.focus() != thread_id:
                return
            if self.view_model.selection_mode == "pinned" and self.view_model.selected_thread_id == thread_id:
                return
            self.view_model.pin_thread(thread_id)
            if not self._selection_refresh_pending:
                self._selection_refresh_pending = True
                self.root.after_idle(self._refresh_selected_task)

    def _refresh_selected_task(self) -> None:
        self._selection_refresh_pending = False
        self.refresh(show_refreshing=False, render_session_rows=False)

    def manual_refresh(self) -> None:
        self.auto_refresh.manual_refresh()

    def _toggle_auto_refresh(self) -> None:
        enabled = bool(self.auto_refresh_var.get())
        self.auto_refresh.set_enabled(enabled)
        self.auto_switch.configure(text=localize_auto_refresh(enabled, self.language, self.auto_refresh.interval_seconds))
        if self.snapshot is not None:
            self.presentation = present_dashboard(self.snapshot, enabled)
            self._apply_presentation(self.presentation)

    def _auto_refresh_error(self, _error: Exception) -> None:
        self.status_message_var.set(translate("auto_refresh_failed", self.language))

    def close(self) -> None:
        self.auto_refresh.close()
        self.quota_provider.close()
        self.mini_widget.destroy()
        self.root.destroy()

    def request_exit(self) -> None:
        remembered = load_exit_action_for_today(UI_SETTINGS_PATH)
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

    def refresh(self, show_refreshing: bool = True, render_session_rows: bool = True) -> None:
        if self._widget_mode:
            if show_refreshing:
                self.mini_widget.set_refreshing()
                self.root.update_idletasks()
            self.quota_snapshot = self.quota_provider.refresh()
            self._mini_thread_snapshot = self.view_model.refresh_thread(self._widget_thread_id)
            self.mini_widget.update(
                self.quota_snapshot,
                self._mini_thread_snapshot,
                self.language,
            )
            return
        if show_refreshing and self.presentation is not None and self.snapshot is not None:
            self._apply_presentation(present_dashboard(self.snapshot, bool(self.auto_refresh_var.get()), True, self.presentation))
            self.root.update_idletasks()
        self.snapshot = self.view_model.refresh()
        self.lookback_days = self.snapshot.lookback_days
        self.presentation = present_dashboard(self.snapshot, bool(self.auto_refresh_var.get()))
        self._apply_presentation(self.presentation, render_session_rows=render_session_rows)
        self.quota_snapshot = self.quota_provider.refresh()

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

    def _enter_widget_mode(self) -> None:
        if self._widget_mode:
            return
        selected = self.snapshot.selected_session if self.snapshot is not None else None
        thread_id = selected.thread_id if selected is not None else None
        if thread_id and self.snapshot is not None and self.snapshot.selection_mode == "auto":
            self.view_model.pin_thread(thread_id)
        self._widget_mode = True
        self._widget_thread_id = thread_id
        self.root.withdraw()
        self._mini_thread_snapshot = self.view_model.refresh_thread(thread_id)
        self.mini_widget.show(
            thread_id,
            self.quota_snapshot,
            self._mini_thread_snapshot,
            self.language,
        )
        self.root.after(50, self.manual_refresh)

    def restore_dashboard(self) -> None:
        if not self._widget_mode:
            return
        self.mini_widget.hide()
        self._widget_mode = False
        self.root.deiconify()
        self.root.geometry(self._last_dashboard_geometry)
        self.root.lift()

    def _minimize_to_taskbar(self) -> None:
        self.mini_widget.hide()
        self._widget_mode = False
        self._taskbar_mode = True
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

    def _apply_presentation(self, presentation: DashboardPresentation, render_session_rows: bool = True) -> None:
        foreground, background = TONE_COLORS[presentation.status_tone.value]
        self.data_status_var.set(localize_status(presentation.data_status, self.language))
        self.status_pill.configure(text_color=foreground, fg_color=background)
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

    def _render_sessions(self, presentation: DashboardPresentation, render_session_rows: bool = True) -> None:
        self._rendering_sessions = True
        try:
            self._render_sessions_inner(presentation, render_session_rows)
        finally:
            self._rendering_sessions = False

    def _render_sessions_inner(self, presentation: DashboardPresentation, render_session_rows: bool = True) -> None:
        labels = disambiguated_session_labels(presentation.recent_sessions, self.language)
        self.selectable_thread_ids = {
            row.thread_id for row in presentation.recent_sessions
            if row.status != "unavailable"
        }
        self.label_to_thread = {
            label: thread_id for thread_id, label in labels.items()
            if thread_id in self.selectable_thread_ids
        }
        auto_label = translate("auto_follow", self.language)
        values = [auto_label, *(labels[thread_id] for thread_id in labels if thread_id in self.selectable_thread_ids)]
        selected_id = self.snapshot.selected_thread_id if self.snapshot else None
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
        for row in presentation.recent_sessions:
            status = localize_presenter_text(row.status, self.language)
            activity = row.last_activity.astimezone().strftime("%m-%d %H:%M:%S") if row.last_activity else "—"
            self.sessions_tree.insert("", "end", iid=row.thread_id, values=(labels[row.thread_id], status, activity, row.thread_total, row.cache_hit))

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
    args = parser.parse_args()
    if args.smoke:
        smoke()
        return
    build_dashboard().mainloop()


if __name__ == "__main__":
    main()
