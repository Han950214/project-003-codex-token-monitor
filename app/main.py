"""Windows Dashboard for Codex Token Monitor."""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auto_refresh import AutoRefreshController, DEFAULT_AUTO_REFRESH_SECONDS
from app.config import load_pricing
from app.dashboard import DashboardViewModel
from app.metrics import build_run_estimates
from app.models import AgentRun
from app.reporting import export_report
from app.storage import DEFAULT_RUNS_PATH, append_run
from app.telemetry_bar import TelemetryBar, build_telemetry_values
from app.ui_presenter import DashboardPresentation, present_dashboard
from app.ui_theme import (
    COLORS,
    SPACE_1,
    SPACE_2,
    SPACE_3,
    SPACE_4,
    TONE_STYLES,
    configure_theme,
)


ROOT = Path(__file__).resolve().parents[1]
RUNS_PATH = ROOT / DEFAULT_RUNS_PATH
PRICING_PATH = ROOT / "resources" / "pricing-config.sample.json"
MANUAL_RUN_COLUMNS = ("Title", "Model", "Mode", "Input", "Output", "Cached", "Total", "Ended At")


class Dashboard:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        configure_theme(root)
        self.pricing = load_pricing(PRICING_PATH)
        self.view_model = DashboardViewModel(self.pricing, RUNS_PATH)
        self.started_at: datetime | None = None
        self.started_at_var = tk.StringVar(value="—")
        self.ended_at_var = tk.StringVar(value="—")
        self.auto_refresh_var = tk.BooleanVar(value=False)
        self.data_status_var = tk.StringVar(value="No Data")
        self.status_message_var = tk.StringVar(value="No response usage is available yet.")
        self.last_event_var = tk.StringVar(value="—")
        self.last_refresh_var = tk.StringVar(value="—")
        self.auto_refresh_status_var = tk.StringVar(value=f"Auto Refresh: Off ({DEFAULT_AUTO_REFRESH_SECONDS}s)")
        self.fields: dict[str, tk.StringVar] = {}
        self.text_fields: dict[str, tk.Text] = {}
        self.metric_vars: list[tuple[tk.StringVar, tk.StringVar]] = []
        self.metric_value_labels: list[ttk.Label] = []
        self.source_vars: dict[str, tk.StringVar] = {}
        self.source_value_labels: dict[str, ttk.Label] = {}
        self.snapshot = None
        self.presentation: DashboardPresentation | None = None

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
        self.refresh()

    def _build(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._build_content()
        self.telemetry = TelemetryBar(self.root)
        self.telemetry.grid(row=2, column=0, sticky="ew")

    def _build_header(self) -> None:
        header = ttk.Frame(self.root, style="Window.TFrame", padding=(SPACE_4, SPACE_3))
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        title_row = ttk.Frame(header, style="Window.TFrame")
        title_row.grid(row=0, column=0, sticky="w")
        ttk.Label(title_row, text="Codex Token Monitor", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        self.data_status_label = ttk.Label(title_row, textvariable=self.data_status_var, style="Unknown.TLabel")
        self.data_status_label.grid(row=0, column=1, padx=(SPACE_4, 0), sticky="w")
        ttk.Label(header, textvariable=self.status_message_var, style="Secondary.TLabel").grid(row=1, column=0, sticky="w", pady=(SPACE_1, 0))

        meta = ttk.Frame(header, style="Window.TFrame")
        meta.grid(row=2, column=0, columnspan=3, sticky="w", pady=(SPACE_2, 0))
        ttk.Label(meta, text="Last Event", style="Secondary.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(meta, textvariable=self.last_event_var).grid(row=0, column=1, padx=(SPACE_2, SPACE_4), sticky="w")
        ttk.Label(meta, text="Last Refresh", style="Secondary.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Label(meta, textvariable=self.last_refresh_var).grid(row=0, column=3, padx=(SPACE_2, SPACE_4), sticky="w")

        actions = ttk.Frame(header, style="Window.TFrame")
        actions.grid(row=0, column=2, rowspan=2, sticky="e")
        ttk.Button(actions, text="Manual Refresh", command=self.manual_refresh, style="Accent.TButton").grid(row=0, column=0, rowspan=2, padx=(0, SPACE_2))
        ttk.Checkbutton(actions, variable=self.auto_refresh_var, command=self._toggle_auto_refresh).grid(row=0, column=1, rowspan=2)
        self.auto_refresh_label = ttk.Label(actions, textvariable=self.auto_refresh_status_var, style="Unknown.TLabel")
        self.auto_refresh_label.grid(row=0, column=2, rowspan=2, padx=(SPACE_1, SPACE_2))
        ttk.Button(actions, text="Export Report", command=self.export_report).grid(row=0, column=3, rowspan=2)

    def _build_content(self) -> None:
        content = ttk.Frame(self.root, style="Window.TFrame", padding=(SPACE_4, 0, SPACE_4, SPACE_3))
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(4, weight=1)

        ttk.Label(content, text="Latest Response Usage", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, SPACE_2))
        cards = ttk.Frame(content, style="Window.TFrame")
        cards.grid(row=1, column=0, sticky="ew")
        for column, label in enumerate(("Input", "Output", "Total", "Cached", "Reasoning", "Cache Hit")):
            cards.grid_columnconfigure(column, weight=1, uniform="metric")
            card = ttk.Frame(cards, style="Card.TFrame", padding=(SPACE_3, SPACE_2))
            card.grid(row=0, column=column, padx=(0 if column == 0 else SPACE_1, 0), sticky="nsew")
            value_var = tk.StringVar(value="—")
            detail_var = tk.StringVar(value="Unknown")
            ttk.Label(card, text=label, style="CardLabel.TLabel").grid(row=0, column=0, sticky="w")
            value_style = "TotalCardValue.TLabel" if label == "Total" else "CardValue.TLabel"
            value_label = ttk.Label(card, textvariable=value_var, style=value_style, anchor="e")
            value_label.grid(row=1, column=0, sticky="ew", pady=(SPACE_1, 0))
            ttk.Label(card, textvariable=detail_var, style="CardDetail.TLabel", wraplength=155).grid(row=2, column=0, sticky="w", pady=(SPACE_1, 0))
            card.grid_columnconfigure(0, weight=1)
            self.metric_vars.append((value_var, detail_var))
            self.metric_value_labels.append(value_label)

        ttk.Label(content, text="Session & Source Status", style="Section.TLabel").grid(row=2, column=0, sticky="w", pady=(SPACE_3, SPACE_2))
        sources = ttk.Frame(content, style="Surface.TFrame", padding=(SPACE_3, SPACE_2))
        sources.grid(row=3, column=0, sticky="ew")
        for column, label in enumerate(("Session Total", "Usage Source", "Session Source", "Logs Adapter", "State Adapter", "Freshness / Time")):
            sources.grid_columnconfigure(column, weight=1, uniform="sources")
            ttk.Label(sources, text=label, style="CardLabel.TLabel").grid(row=0, column=column, padx=SPACE_2, sticky="w")
            variable = tk.StringVar(value="—")
            value_label = ttk.Label(sources, textvariable=variable, style="Unknown.TLabel", wraplength=180)
            value_label.grid(row=1, column=column, padx=SPACE_2, sticky="w")
            self.source_vars[label] = variable
            self.source_value_labels[label] = value_label

        notebook = ttk.Notebook(content)
        notebook.grid(row=4, column=0, sticky="nsew", pady=(SPACE_3, 0))
        self._build_saved_runs_tab(notebook)
        self._build_manual_input_tab(notebook)

    def _build_saved_runs_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, style="Window.TFrame", padding=SPACE_3)
        notebook.add(tab, text="Manual Saved Runs")
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)
        ttk.Label(tab, text="Manual Saved Runs", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(tab, text="Only runs explicitly saved by the user appear here.", style="Secondary.TLabel").grid(row=1, column=0, sticky="w", pady=(SPACE_1, SPACE_2))
        tree_frame = ttk.Frame(tab, style="Window.TFrame")
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)
        self.runs_tree = ttk.Treeview(tree_frame, columns=MANUAL_RUN_COLUMNS, show="headings", selectmode="browse")
        widths = (180, 120, 85, 80, 80, 80, 80, 160)
        for column, width in zip(MANUAL_RUN_COLUMNS, widths):
            self.runs_tree.heading(column, text=column)
            self.runs_tree.column(column, width=width, minwidth=60, anchor="e" if column in {"Input", "Output", "Cached", "Total"} else "w")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.runs_tree.yview)
        self.runs_tree.configure(yscrollcommand=scrollbar.set)
        self.runs_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

    def _build_manual_input_tab(self, notebook: ttk.Notebook) -> None:
        form = ttk.Frame(notebook, style="Window.TFrame", padding=SPACE_3)
        notebook.add(form, text="Manual Run Input")
        for column in (1, 3):
            form.grid_columnconfigure(column, weight=1)
        defaults = {
            "project": "project_003_codex_token_monitor",
            "title": "Manual Codex run",
            "session_id": "default-session",
            "model": "local-estimate-demo",
            "mode": "manual",
            "input_tokens": "0",
            "output_tokens": "0",
            "cached_tokens": "0",
        }
        fields = (
            ("project", "Project"), ("title", "Title"),
            ("session_id", "Session ID"), ("model", "Model"),
            ("mode", "Mode"), ("input_tokens", "Input Tokens"),
            ("output_tokens", "Output Tokens"), ("cached_tokens", "Cached Tokens"),
        )
        for index, (key, label) in enumerate(fields):
            row, pair = divmod(index, 2)
            label_column = pair * 2
            variable = tk.StringVar(value=defaults[key])
            self.fields[key] = variable
            ttk.Label(form, text=label, style="Secondary.TLabel").grid(row=row, column=label_column, sticky="w", padx=(0, SPACE_2), pady=SPACE_1)
            ttk.Entry(form, textvariable=variable).grid(row=row, column=label_column + 1, sticky="ew", padx=(0, SPACE_4), pady=SPACE_1)

        ttk.Label(form, text="Started", style="Secondary.TLabel").grid(row=4, column=0, sticky="w", pady=SPACE_1)
        ttk.Label(form, textvariable=self.started_at_var).grid(row=4, column=1, sticky="w")
        ttk.Label(form, text="Ended", style="Secondary.TLabel").grid(row=4, column=2, sticky="w", pady=SPACE_1)
        ttk.Label(form, textvariable=self.ended_at_var).grid(row=4, column=3, sticky="w")

        summaries = (("prompt_summary", "Prompt summary"), ("output_summary", "Output summary"), ("note", "Note"))
        summaries_frame = ttk.Frame(form, style="Window.TFrame")
        summaries_frame.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=(SPACE_2, 0))
        for column, (key, label) in enumerate(summaries):
            summaries_frame.grid_columnconfigure(column, weight=1, uniform="summaries")
            cell = ttk.Frame(summaries_frame, style="Window.TFrame")
            cell.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else SPACE_2, 0))
            ttk.Label(cell, text=label, style="Secondary.TLabel").grid(row=0, column=0, sticky="w")
            text = tk.Text(cell, wrap="word", height=4, relief="solid", borderwidth=1, background=COLORS.surface, foreground=COLORS.primary_text)
            text.grid(row=1, column=0, sticky="nsew", pady=(SPACE_1, 0))
            cell.grid_columnconfigure(0, weight=1)
            self.text_fields[key] = text

        controls = ttk.Frame(form, style="Window.TFrame")
        controls.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(SPACE_2, 0))
        ttk.Button(controls, text="Start Run", command=self.start_run).pack(side="left")
        ttk.Button(controls, text="End Run", command=self.end_run).pack(side="left", padx=SPACE_2)
        ttk.Button(controls, text="Save Run", command=self.save_run, style="Accent.TButton").pack(side="left")
        ttk.Label(controls, text="默认仅保存摘要和手动 token 数；不要保存凭据或 prompt/output 全文。", style="Secondary.TLabel").pack(side="right")

    def start_run(self) -> None:
        self.started_at = datetime.now()
        self.started_at_var.set(self.started_at.isoformat(timespec="seconds"))
        self.ended_at_var.set("—")
        self.status_message_var.set("Manual Run started. Values remain local estimates until explicitly saved.")

    def end_run(self) -> None:
        if self.started_at is None:
            self.start_run()
        self.ended_at_var.set(datetime.now().isoformat(timespec="seconds"))
        self.status_message_var.set("Manual Run ended. Use Save Run to store it locally.")

    def save_run(self) -> None:
        run = self._run_from_form()
        result = append_run(run, RUNS_PATH)
        if result.error:
            messagebox.showerror("Storage error", result.error)
            self.status_message_var.set(f"Save failed: {result.error}")
            return
        self.refresh(result.runs)

    def export_report(self) -> None:
        snapshot = self.view_model.refresh()
        if snapshot.storage_error:
            messagebox.showerror("Storage error", snapshot.storage_error)
            return
        path = export_report(snapshot.runs, snapshot.summary, ROOT / "reports" / f"token-waste-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md")
        self.status_message_var.set(f"Report exported: {path}")

    def manual_refresh(self) -> None:
        self.auto_refresh.manual_refresh()

    def _toggle_auto_refresh(self) -> None:
        enabled = bool(self.auto_refresh_var.get())
        self.auto_refresh.set_enabled(enabled)
        self.auto_refresh_status_var.set(f"Auto Refresh: {'On' if enabled else 'Off'} ({self.auto_refresh.interval_seconds}s)")
        self.auto_refresh_label.configure(style="Fresh.TLabel" if enabled else "Unknown.TLabel")
        if self.snapshot is not None:
            self.presentation = present_dashboard(self.snapshot, enabled)
            self._apply_presentation(self.presentation)

    def _auto_refresh_error(self, _error: Exception) -> None:
        self.status_message_var.set("Auto refresh failed; the next refresh remains scheduled.")

    def close(self) -> None:
        self.auto_refresh.close()
        self.root.destroy()

    def refresh(self, runs: list[AgentRun] | None = None) -> None:
        if self.presentation is not None and self.snapshot is not None:
            refreshing = present_dashboard(
                self.snapshot,
                bool(self.auto_refresh_var.get()),
                refreshing=True,
                previous=self.presentation,
            )
            self._apply_presentation(refreshing)
            self.root.update_idletasks()
        self.snapshot = self.view_model.refresh(runs)
        self.presentation = present_dashboard(self.snapshot, bool(self.auto_refresh_var.get()))
        self._apply_presentation(self.presentation)

    def _apply_presentation(self, presentation: DashboardPresentation) -> None:
        top_status = "Refreshing…" if presentation.data_status.value == "Refreshing" else presentation.data_status.value
        self.data_status_var.set(top_status)
        self.data_status_label.configure(style=TONE_STYLES[presentation.status_tone.value])
        self.status_message_var.set(presentation.status_message)
        self.last_event_var.set(presentation.last_event)
        self.last_refresh_var.set(presentation.last_refresh)
        self.auto_refresh_status_var.set(presentation.auto_refresh)
        for (value_var, detail_var), value_label, metric in zip(self.metric_vars, self.metric_value_labels, presentation.latest_usage):
            value_var.set(metric.value)
            detail_var.set(metric.detail)
            tone = metric.tone.value.title()
            prefix = "TotalCard" if metric.label == "Total" else "Card"
            value_label.configure(style=f"{prefix}{tone}.TLabel")
        for source in presentation.source_details:
            self.source_vars[source.label].set(source.value)
            self.source_value_labels[source.label].configure(style=f"Source{source.tone.value.title()}.TLabel")
        for item in self.runs_tree.get_children():
            self.runs_tree.delete(item)
        for row in presentation.manual_runs:
            self.runs_tree.insert("", "end", values=row.values())
        self.telemetry.update_values(build_telemetry_values(presentation))

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
            input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens, total_tokens=total_tokens,
            estimated_cost=estimated_cost, cache_hit=cache_hit,
        )


def _parse_int(value: str) -> int:
    try:
        return max(int(value.strip()), 0)
    except ValueError:
        return 0


def _text_value(widget: tk.Text) -> str:
    return widget.get("1.0", "end").strip()


def build_dashboard() -> tk.Tk:
    root = tk.Tk()
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
    print("cache_hit=derived from real usage when available; not an official rate")
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
