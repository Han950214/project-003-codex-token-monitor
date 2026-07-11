"""Modern localized Windows Dashboard for Codex Token Monitor."""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auto_refresh import AutoRefreshController, DEFAULT_AUTO_REFRESH_SECONDS
from app.config import load_pricing
from app.dashboard import DashboardViewModel
from app.i18n import (
    LANGUAGE_LABELS,
    language_from_label,
    localize_auto_refresh,
    localize_presenter_label,
    localize_presenter_text,
    localize_status,
    localize_status_message,
    translate,
)
from app.metrics import build_run_estimates
from app.models import AgentRun
from app.reporting import export_report
from app.storage import DEFAULT_RUNS_PATH, append_run
from app.telemetry_bar import TelemetryBar, build_telemetry_values
from app.ui_presenter import DashboardPresentation, present_dashboard
from app.ui_settings import LanguageController
from app.ui_theme import (
    CARD_RADIUS,
    COLORS,
    CONTROL_RADIUS,
    FONT_BODY,
    FONT_FAMILY,
    FONT_METRIC,
    FONT_SECTION,
    FONT_SMALL,
    FONT_TITLE,
    METRIC_ACCENTS,
    METRIC_ICONS,
    SPACE_1,
    SPACE_2,
    SPACE_3,
    SPACE_4,
    TONE_COLORS,
    configure_view,
)


ROOT = Path(__file__).resolve().parents[1]
RUNS_PATH = ROOT / DEFAULT_RUNS_PATH
PRICING_PATH = ROOT / "resources" / "pricing-config.sample.json"
MANUAL_RUN_COLUMNS = ("Title", "Model", "Mode", "Input", "Output", "Cached", "Total", "Ended At")
MANUAL_RUN_COLUMN_KEYS = (
    "column_title", "column_model", "column_mode", "column_input",
    "column_output", "column_cached", "column_total", "column_ended_at",
)
MANUAL_FORM_FIELDS = (
    "project", "title", "session_id", "model",
    "mode", "input_tokens", "output_tokens", "cached_tokens",
)


def manual_form_position(index: int) -> tuple[int, int]:
    """Return the two-field-row position for a manual Run form field."""
    if not 0 <= index < len(MANUAL_FORM_FIELDS):
        raise IndexError("manual form field index is out of range")
    return divmod(index, 2)


class Dashboard:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        configure_view(root)
        self.pricing = load_pricing(PRICING_PATH)
        self.view_model = DashboardViewModel(self.pricing, RUNS_PATH)
        self.language_controller = LanguageController(self._apply_language)
        self.language = self.language_controller.language
        self.started_at: datetime | None = None
        self.snapshot = None
        self.presentation: DashboardPresentation | None = None
        self.active_page = "saved"

        self.auto_refresh_var = tk.BooleanVar(value=False)
        self.data_status_var = tk.StringVar(value="")
        self.status_message_var = tk.StringVar(value="")
        self.last_event_var = tk.StringVar(value="—")
        self.last_refresh_var = tk.StringVar(value="—")
        self.started_at_var = tk.StringVar(value="—")
        self.ended_at_var = tk.StringVar(value="—")
        self.fields: dict[str, tk.StringVar] = {}
        self.text_fields: dict[str, ctk.CTkTextbox] = {}
        self.form_labels: dict[str, ctk.CTkLabel] = {}
        self.metric_widgets: list[dict[str, object]] = []
        self.source_widgets: dict[str, dict[str, object]] = {}

        self.root.title("Codex Token Monitor")
        self.root.geometry("1180x760")
        self.root.minsize(980, 660)
        self._build()
        self.auto_refresh = AutoRefreshController(
            self.root.after,
            self.root.after_cancel,
            self.refresh,
            on_error=self._auto_refresh_error,
        )
        self.root.protocol("WM_DELETE_WINDOW", self.close)
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
        self.status_pill = ctk.CTkLabel(
            identity, textvariable=self.data_status_var, font=(FONT_FAMILY, 12, "bold"),
            corner_radius=9, height=30, padx=SPACE_3,
        )
        self.status_pill.grid(row=0, column=1, padx=(SPACE_4, 0), sticky="w")

        actions = ctk.CTkFrame(header, fg_color="transparent")
        actions.grid(row=0, column=1, sticky="e")
        self.refresh_button = ctk.CTkButton(
            actions, text="", command=self.manual_refresh, width=112, height=34,
            corner_radius=CONTROL_RADIUS, fg_color="transparent", border_width=1,
            border_color=COLORS.accent, text_color=COLORS.accent, hover_color=COLORS.accent_soft,
        )
        self.refresh_button.grid(row=0, column=0, padx=(0, SPACE_2))
        self.auto_switch = ctk.CTkSwitch(
            actions, text="", variable=self.auto_refresh_var, command=self._toggle_auto_refresh,
            width=168, font=FONT_SMALL, progress_color=COLORS.real, button_color=COLORS.surface,
            button_hover_color=COLORS.raised_surface,
        )
        self.auto_switch.grid(row=0, column=1, padx=(0, SPACE_2))
        self.export_button = ctk.CTkButton(
            actions, text="", command=self.export_report, width=104, height=34,
            corner_radius=CONTROL_RADIUS, fg_color=COLORS.surface, border_width=1,
            border_color=COLORS.border, text_color=COLORS.primary_text, hover_color=COLORS.raised_surface,
        )
        self.export_button.grid(row=0, column=2, padx=(0, SPACE_2))
        self.language_menu = ctk.CTkOptionMenu(
            actions, values=list(LANGUAGE_LABELS.values()), command=self._change_language,
            width=112, height=34, corner_radius=CONTROL_RADIUS, fg_color=COLORS.surface,
            button_color=COLORS.raised_surface, button_hover_color=COLORS.border,
            text_color=COLORS.primary_text, dropdown_fg_color=COLORS.surface,
            dropdown_text_color=COLORS.primary_text, dropdown_hover_color=COLORS.accent_soft,
        )
        self.language_menu.grid(row=0, column=3)

        self.status_message_label = ctk.CTkLabel(
            header, textvariable=self.status_message_var, font=FONT_BODY,
            text_color=COLORS.secondary_text, anchor="w",
        )
        self.status_message_label.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(SPACE_1, 0))
        meta = ctk.CTkFrame(header, fg_color="transparent")
        meta.grid(row=2, column=0, columnspan=2, sticky="w", pady=(SPACE_1, 0))
        self.last_event_title = ctk.CTkLabel(meta, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.last_event_title.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(meta, textvariable=self.last_event_var, font=FONT_SMALL, text_color=COLORS.primary_text).grid(row=0, column=1, padx=(SPACE_2, SPACE_4), sticky="w")
        self.last_refresh_title = ctk.CTkLabel(meta, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.last_refresh_title.grid(row=0, column=2, sticky="w")
        ctk.CTkLabel(meta, textvariable=self.last_refresh_var, font=FONT_SMALL, text_color=COLORS.primary_text).grid(row=0, column=3, padx=(SPACE_2, 0), sticky="w")

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
        self._build_segmented_pages(content)

    def _build_metric_cards(self, parent: ctk.CTkFrame) -> None:
        cards = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        cards.grid(row=1, column=0, sticky="ew")
        for column, label in enumerate(METRIC_ICONS):
            cards.grid_columnconfigure(column, weight=1, uniform="metric")
            accent, soft = METRIC_ACCENTS[label]
            card = ctk.CTkFrame(
                cards, fg_color=COLORS.surface, corner_radius=CARD_RADIUS,
                border_width=2 if label == "Total" else 1,
                border_color=accent if label == "Total" else COLORS.border,
            )
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else SPACE_1, 0), pady=1)
            card.grid_columnconfigure(1, weight=1)
            icon = ctk.CTkLabel(
                card, text=METRIC_ICONS[label], width=38, height=38, corner_radius=19,
                fg_color=soft, text_color=accent, font=(FONT_FAMILY, 19, "bold"),
            )
            icon.grid(row=0, column=0, rowspan=2, padx=(SPACE_3, SPACE_2), pady=(SPACE_3, SPACE_1))
            label_var = tk.StringVar(value=label)
            value_var = tk.StringVar(value="—")
            detail_var = tk.StringVar(value="")
            ctk.CTkLabel(card, textvariable=label_var, font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w").grid(row=0, column=1, sticky="sw", padx=(0, SPACE_2), pady=(SPACE_2, 0))
            value_label = ctk.CTkLabel(card, textvariable=value_var, font=FONT_METRIC, text_color=accent, anchor="w")
            value_label.grid(row=1, column=1, sticky="nw", padx=(0, SPACE_2))
            detail_label = ctk.CTkLabel(
                card, textvariable=detail_var, font=(FONT_FAMILY, 10), text_color=COLORS.secondary_text,
                anchor="w", justify="left", wraplength=120,
            )
            detail_label.grid(row=2, column=0, columnspan=2, sticky="ew", padx=SPACE_3, pady=(SPACE_1, SPACE_2))
            self.metric_widgets.append({
                "semantic": label, "label_var": label_var, "value_var": value_var,
                "detail_var": detail_var, "value_label": value_label, "accent": accent,
            })

    def _build_source_panel(self, parent: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(
            parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS,
            border_width=1, border_color=COLORS.border,
        )
        panel.grid(row=3, column=0, sticky="ew")
        icons = ("◆", "▤", "▣", "⛓", "◇", "◷")
        labels = ("Session Total", "Usage Source", "Session Source", "Logs Adapter", "State Adapter", "Freshness / Time")
        for column, (icon, label) in enumerate(zip(icons, labels)):
            panel.grid_columnconfigure(column, weight=1, uniform="source")
            cell = ctk.CTkFrame(panel, fg_color="transparent", corner_radius=0)
            cell.grid(row=0, column=column, sticky="nsew", padx=SPACE_2, pady=SPACE_2)
            icon_label = ctk.CTkLabel(cell, text=icon, width=24, font=(FONT_FAMILY, 16), text_color=COLORS.secondary_text)
            icon_label.grid(row=0, column=0, rowspan=2, padx=(0, SPACE_1), sticky="n")
            label_var = tk.StringVar(value=label)
            value_var = tk.StringVar(value="—")
            ctk.CTkLabel(cell, textvariable=label_var, font=(FONT_FAMILY, 10), text_color=COLORS.secondary_text, anchor="w").grid(row=0, column=1, sticky="ew")
            value_label = ctk.CTkLabel(
                cell, textvariable=value_var, font=(FONT_FAMILY, 10, "bold"),
                text_color=COLORS.unknown, anchor="w", justify="left", wraplength=116,
            )
            value_label.grid(row=1, column=1, sticky="ew")
            cell.grid_columnconfigure(1, weight=1)
            self.source_widgets[label] = {"label_var": label_var, "value_var": value_var, "value_label": value_label}

    def _build_segmented_pages(self, parent: ctk.CTkFrame) -> None:
        self.segmented = ctk.CTkSegmentedButton(
            parent, values=["saved", "input"], command=self._show_page,
            height=34, corner_radius=CONTROL_RADIUS, fg_color=COLORS.raised_surface,
            selected_color=COLORS.accent, selected_hover_color=COLORS.accent_hover,
            unselected_color=COLORS.raised_surface, unselected_hover_color=COLORS.accent_soft,
            text_color=COLORS.primary_text,
        )
        self.segmented.grid(row=4, column=0, sticky="w", pady=(SPACE_2, SPACE_1))
        self.pages = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        self.pages.grid(row=5, column=0, sticky="nsew")
        self.pages.grid_columnconfigure(0, weight=1)
        self.pages.grid_rowconfigure(0, weight=1)
        self._build_saved_runs_page()
        self._build_manual_input_page()
        self.saved_page.grid(row=0, column=0, sticky="nsew")

    def _build_saved_runs_page(self) -> None:
        self.saved_page = ctk.CTkFrame(
            self.pages, fg_color=COLORS.surface, corner_radius=CARD_RADIUS,
            border_width=1, border_color=COLORS.border,
        )
        self.saved_page.grid_columnconfigure(0, weight=1)
        self.saved_page.grid_rowconfigure(2, weight=1)
        self.saved_page_title = ctk.CTkLabel(self.saved_page, text="", font=FONT_SECTION, text_color=COLORS.primary_text, anchor="w")
        self.saved_page_title.grid(row=0, column=0, sticky="ew", padx=SPACE_3, pady=(SPACE_2, 0))
        self.saved_note = ctk.CTkLabel(self.saved_page, text="", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
        self.saved_note.grid(row=1, column=0, sticky="ew", padx=SPACE_3, pady=(0, SPACE_1))
        tree_frame = ctk.CTkFrame(self.saved_page, fg_color=COLORS.surface, corner_radius=0)
        tree_frame.grid(row=2, column=0, sticky="nsew", padx=SPACE_3, pady=(0, SPACE_3))
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)
        self.runs_tree = ttk.Treeview(
            tree_frame, columns=MANUAL_RUN_COLUMNS, show="headings", selectmode="browse",
            style="Monitor.Treeview", height=5,
        )
        widths = (160, 100, 70, 64, 64, 64, 64, 140)
        for column, width in zip(MANUAL_RUN_COLUMNS, widths):
            self.runs_tree.heading(column, text=column)
            self.runs_tree.column(column, width=width, minwidth=58, anchor="e" if column in {"Input", "Output", "Cached", "Total"} else "w")
        scrollbar = ctk.CTkScrollbar(tree_frame, command=self.runs_tree.yview, width=12)
        self.runs_tree.configure(yscrollcommand=scrollbar.set)
        self.runs_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(SPACE_1, 0))

    def _build_manual_input_page(self) -> None:
        self.input_page = ctk.CTkFrame(
            self.pages, fg_color=COLORS.surface, corner_radius=CARD_RADIUS,
            border_width=1, border_color=COLORS.border,
        )
        self.input_page.grid_columnconfigure(0, weight=1)
        self.input_page.grid_rowconfigure(0, weight=1)
        self.manual_input_scroll = ctk.CTkScrollableFrame(
            self.input_page, fg_color="transparent", corner_radius=0,
            scrollbar_fg_color=COLORS.raised_surface,
            scrollbar_button_color=COLORS.scrollbar_thumb,
            scrollbar_button_hover_color=COLORS.scrollbar_thumb_hover,
        )
        self.manual_input_scroll.grid(row=0, column=0, sticky="nsew", padx=SPACE_2, pady=SPACE_2)
        form = self.manual_input_scroll
        for column in (1, 3):
            form.grid_columnconfigure(column, weight=1)
        defaults = {
            "project": "project_003_codex_token_monitor", "title": "Manual Codex run",
            "session_id": "default-session", "model": "local-estimate-demo", "mode": "manual",
            "input_tokens": "0", "output_tokens": "0", "cached_tokens": "0",
        }
        for index, key in enumerate(MANUAL_FORM_FIELDS):
            row, group = manual_form_position(index)
            label_column = group * 2
            label = ctk.CTkLabel(form, text="", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
            label.grid(row=row, column=label_column, sticky="w", padx=(SPACE_3 if group == 0 else SPACE_4, SPACE_1), pady=(SPACE_2, SPACE_1))
            variable = tk.StringVar(value=defaults[key])
            entry = ctk.CTkEntry(
                form, textvariable=variable, height=32, corner_radius=CONTROL_RADIUS,
                border_width=1, border_color=COLORS.border, fg_color=COLORS.raised_surface,
                text_color=COLORS.primary_text,
            )
            entry.grid(row=row, column=label_column + 1, sticky="ew", padx=(0, SPACE_3), pady=(SPACE_2, SPACE_1))
            self.form_labels[key] = label
            self.fields[key] = variable

        self.form_labels["started"] = ctk.CTkLabel(form, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.form_labels["started"].grid(row=4, column=0, sticky="w", padx=(SPACE_3, SPACE_1), pady=SPACE_1)
        ctk.CTkLabel(form, textvariable=self.started_at_var, font=FONT_SMALL, text_color=COLORS.primary_text).grid(row=4, column=1, sticky="w")
        self.form_labels["ended"] = ctk.CTkLabel(form, text="", font=FONT_SMALL, text_color=COLORS.secondary_text)
        self.form_labels["ended"].grid(row=4, column=2, sticky="w", padx=(SPACE_4, SPACE_1), pady=SPACE_1)
        ctk.CTkLabel(form, textvariable=self.ended_at_var, font=FONT_SMALL, text_color=COLORS.primary_text).grid(row=4, column=3, sticky="w")

        summaries = ("prompt_summary", "output_summary", "note")
        summary_frame = ctk.CTkFrame(form, fg_color="transparent", corner_radius=0)
        summary_frame.grid(row=5, column=0, columnspan=4, sticky="nsew", padx=SPACE_3, pady=(SPACE_2, 0))
        for column, key in enumerate(summaries):
            summary_frame.grid_columnconfigure(column, weight=1, uniform="summary")
            cell = ctk.CTkFrame(summary_frame, fg_color="transparent", corner_radius=0)
            cell.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else SPACE_2, 0))
            label = ctk.CTkLabel(cell, text="", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
            label.grid(row=0, column=0, sticky="ew")
            text = ctk.CTkTextbox(
                cell, height=66, corner_radius=CONTROL_RADIUS, border_width=1,
                border_color=COLORS.border, fg_color=COLORS.raised_surface,
                text_color=COLORS.primary_text, font=FONT_SMALL,
            )
            text.grid(row=1, column=0, sticky="nsew")
            cell.grid_columnconfigure(0, weight=1)
            self.form_labels[key] = label
            self.text_fields[key] = text

        controls = ctk.CTkFrame(form, fg_color="transparent", corner_radius=0)
        controls.grid(row=6, column=0, columnspan=4, sticky="ew", padx=SPACE_3, pady=(SPACE_3, SPACE_3))
        controls.grid_columnconfigure(3, weight=1)
        self.start_button = ctk.CTkButton(controls, text="", command=self.start_run, width=100, height=32, corner_radius=CONTROL_RADIUS, fg_color=COLORS.teal, hover_color="#1E7679")
        self.start_button.grid(row=0, column=0)
        self.end_button = ctk.CTkButton(controls, text="", command=self.end_run, width=100, height=32, corner_radius=CONTROL_RADIUS, fg_color="transparent", border_width=1, border_color=COLORS.orange, text_color=COLORS.orange, hover_color=COLORS.orange_soft)
        self.end_button.grid(row=0, column=1, padx=SPACE_2)
        self.save_button = ctk.CTkButton(controls, text="", command=self.save_run, width=100, height=32, corner_radius=CONTROL_RADIUS, fg_color="transparent", border_width=1, border_color=COLORS.accent, text_color=COLORS.accent, hover_color=COLORS.accent_soft)
        self.save_button.grid(row=0, column=2)
        self.privacy_label = ctk.CTkLabel(
            controls, text="", font=(FONT_FAMILY, 10), text_color=COLORS.secondary_text,
            anchor="e", justify="right", wraplength=500,
        )
        self.privacy_label.grid(row=0, column=3, sticky="e", padx=(SPACE_3, 0))

    def _show_page(self, selected: str) -> None:
        saved_label = translate("manual_saved_runs", self.language)
        self.active_page = "saved" if selected == saved_label else "input"
        if self.active_page == "saved":
            self.input_page.grid_remove()
            self.saved_page.grid(row=0, column=0, sticky="nsew")
        else:
            self.saved_page.grid_remove()
            self.input_page.grid(row=0, column=0, sticky="nsew")

    def _change_language(self, selected: str) -> None:
        self.language_controller.set_language(language_from_label(selected))

    def _apply_language(self, language: str) -> None:
        self.language = language
        if not hasattr(self, "refresh_button"):
            return
        self.refresh_button.configure(text=translate("manual_refresh", language))
        self.export_button.configure(text=translate("export_report", language))
        self.auto_switch.configure(text=localize_auto_refresh(bool(self.auto_refresh_var.get()), language, DEFAULT_AUTO_REFRESH_SECONDS))
        self.language_menu.set(LANGUAGE_LABELS[language])
        self.last_event_title.configure(text=translate("last_event", language))
        self.last_refresh_title.configure(text=translate("last_refresh", language))
        self.latest_title.configure(text=translate("latest_usage", language))
        self.sources_title.configure(text=translate("session_sources", language))
        saved = translate("manual_saved_runs", language)
        manual_input = translate("manual_run_input", language)
        self.segmented.configure(values=[saved, manual_input])
        self.segmented.set(saved if self.active_page == "saved" else manual_input)
        self.saved_page_title.configure(text=saved)
        self.saved_note.configure(text=translate("saved_runs_note", language))
        for widget in self.metric_widgets:
            widget["label_var"].set(localize_presenter_label(widget["semantic"], language))
        for semantic, widget in self.source_widgets.items():
            widget["label_var"].set(localize_presenter_label(semantic, language))
        for key, label in self.form_labels.items():
            label.configure(text=translate(key, language))
        self.start_button.configure(text=translate("start_run", language))
        self.end_button.configure(text=translate("end_run", language))
        self.save_button.configure(text=translate("save_run", language))
        self.privacy_label.configure(text=translate("privacy_note", language))
        for column, key in zip(MANUAL_RUN_COLUMNS, MANUAL_RUN_COLUMN_KEYS):
            self.runs_tree.heading(column, text=translate(key, language))
        if self.presentation is not None:
            self._apply_presentation(self.presentation)

    def start_run(self) -> None:
        self.started_at = datetime.now()
        self.started_at_var.set(self.started_at.isoformat(timespec="seconds"))
        self.ended_at_var.set("—")
        self.status_message_var.set(translate("run_started", self.language))

    def end_run(self) -> None:
        if self.started_at is None:
            self.start_run()
        self.ended_at_var.set(datetime.now().isoformat(timespec="seconds"))
        self.status_message_var.set(translate("run_ended", self.language))

    def save_run(self) -> None:
        run = self._run_from_form()
        result = append_run(run, RUNS_PATH)
        if result.error:
            messagebox.showerror(translate("storage_error", self.language), result.error)
            self.status_message_var.set(translate("save_failed", self.language, error=result.error))
            return
        self.refresh(result.runs)

    def export_report(self) -> None:
        snapshot = self.view_model.refresh()
        if snapshot.storage_error:
            messagebox.showerror(translate("storage_error", self.language), snapshot.storage_error)
            return
        path = export_report(snapshot.runs, snapshot.summary, ROOT / "reports" / f"token-waste-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md")
        self.status_message_var.set(translate("report_exported", self.language, path=path))

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
        self.root.destroy()

    def refresh(self, runs: list[AgentRun] | None = None) -> None:
        if self.presentation is not None and self.snapshot is not None:
            refreshing = present_dashboard(
                self.snapshot, bool(self.auto_refresh_var.get()), refreshing=True, previous=self.presentation,
            )
            self._apply_presentation(refreshing)
            self.root.update_idletasks()
        self.snapshot = self.view_model.refresh(runs)
        self.presentation = present_dashboard(self.snapshot, bool(self.auto_refresh_var.get()))
        self._apply_presentation(self.presentation)

    def _apply_presentation(self, presentation: DashboardPresentation) -> None:
        foreground, background = TONE_COLORS[presentation.status_tone.value]
        self.data_status_var.set(localize_status(presentation.data_status, self.language))
        self.status_pill.configure(text_color=foreground, fg_color=background)
        self.status_message_var.set(localize_status_message(presentation.data_status, self.language))
        self.last_event_var.set(presentation.last_event)
        self.last_refresh_var.set(presentation.last_refresh)
        self.auto_switch.configure(text=localize_auto_refresh(bool(self.auto_refresh_var.get()), self.language, DEFAULT_AUTO_REFRESH_SECONDS))
        for widget, metric in zip(self.metric_widgets, presentation.latest_usage):
            widget["label_var"].set(localize_presenter_label(metric.label, self.language))
            widget["value_var"].set(metric.value)
            widget["detail_var"].set(localize_presenter_text(metric.detail, self.language))
            color = TONE_COLORS[metric.tone.value][0] if metric.tone.value in {"error", "unknown"} else widget["accent"]
            widget["value_label"].configure(text_color=color)
        for source in presentation.source_details:
            widget = self.source_widgets[source.label]
            widget["label_var"].set(localize_presenter_label(source.label, self.language))
            widget["value_var"].set(localize_presenter_text(source.value, self.language))
            widget["value_label"].configure(text_color=TONE_COLORS[source.tone.value][0])
        for item in self.runs_tree.get_children():
            self.runs_tree.delete(item)
        for row in presentation.manual_runs:
            self.runs_tree.insert("", "end", values=row.values())
        self.telemetry.update_values(build_telemetry_values(presentation, self.language))

    def _run_from_form(self) -> AgentRun:
        if self.started_at is None:
            self.started_at = datetime.now()
            self.started_at_var.set(self.started_at.isoformat(timespec="seconds"))
        ended = datetime.now()
        if self.ended_at_var.get() == "—":
            self.ended_at_var.set(ended.isoformat(timespec="seconds"))
        input_tokens = _parse_int(self.fields["input_tokens"].get())
        output_tokens = _parse_int(self.fields["output_tokens"].get())
        cached_tokens = _parse_int(self.fields["cached_tokens"].get())
        total_tokens, estimated_cost, cache_hit = build_run_estimates(input_tokens, output_tokens, cached_tokens, self.pricing)
        return AgentRun(
            run_id=f"run-{uuid.uuid4().hex[:8]}", session_id=self.fields["session_id"].get().strip() or "default-session",
            project=self.fields["project"].get().strip(), title=self.fields["title"].get().strip() or "Manual Codex run",
            started_at=self.started_at.isoformat(timespec="seconds"), ended_at=ended.isoformat(timespec="seconds"),
            elapsed_seconds=max(round((ended - self.started_at).total_seconds()), 0), model=self.fields["model"].get().strip(),
            mode=self.fields["mode"].get().strip(), prompt_summary=_text_value(self.text_fields["prompt_summary"]),
            output_summary=_text_value(self.text_fields["output_summary"]), note=_text_value(self.text_fields["note"]),
            input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens,
            total_tokens=total_tokens, estimated_cost=estimated_cost, cache_hit=cache_hit,
        )


def _parse_int(value: str) -> int:
    try:
        return max(int(value.strip()), 0)
    except ValueError:
        return 0


def _text_value(widget: ctk.CTkTextbox) -> str:
    return widget.get("1.0", "end").strip()


def build_dashboard() -> ctk.CTk:
    root = ctk.CTk()
    Dashboard(root)
    return root


def smoke() -> None:
    pricing = load_pricing(PRICING_PATH)
    snapshot = DashboardViewModel(pricing, RUNS_PATH).refresh()
    presentation = present_dashboard(snapshot, False)
    print("Codex Token Monitor smoke OK")
    print(f"data_status={presentation.data_status.value}")
    print(f"session_total={presentation.telemetry_session_total}")
    print(f"current_total={presentation.telemetry_current_total}")
    print("view=CustomTkinter; language=zh-CN default with runtime switch")
    print(f"logs_adapter={snapshot.logs.status.value}")


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
