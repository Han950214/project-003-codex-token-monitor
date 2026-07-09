"""Windows Dashboard for Codex Token Monitor Skill."""

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

from app.config import load_pricing
from app.metrics import build_run_estimates, summarize_runs
from app.models import AgentRun
from app.reporting import export_report
from app.storage import DEFAULT_RUNS_PATH, append_run, load_runs
from app.telemetry_bar import build_telemetry_values_from_summary, create_telemetry_bar_from_values


ROOT = Path(__file__).resolve().parents[1]
RUNS_PATH = ROOT / DEFAULT_RUNS_PATH
PRICING_PATH = ROOT / "resources" / "pricing-config.sample.json"


class Dashboard:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.pricing = load_pricing(PRICING_PATH)
        self.started_at: datetime | None = None
        self.started_at_var = tk.StringVar(value="")
        self.ended_at_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="本地估算 / local estimate")
        self.fields: dict[str, tk.StringVar] = {}
        self.text_fields: dict[str, tk.Text] = {}
        self.telemetry_frame: tk.Frame | None = None

        self.root.title("Codex Token Monitor Skill - 本地估算 Dashboard")
        self.root.geometry("1180x760")
        self.root.minsize(980, 660)
        self._build()
        self.refresh()

    def _build(self) -> None:
        header = ttk.Frame(self.root, padding=10)
        header.pack(fill="x")
        ttk.Label(header, text="Codex Token Monitor / 本地估算 Dashboard", font=("Segoe UI", 15, "bold")).pack(side="left")
        ttk.Button(header, text="Export Report / 导出报告", command=self.export_report).pack(side="right", padx=(8, 0))
        ttk.Button(header, text="Save Run / 保存", command=self.save_run).pack(side="right", padx=(8, 0))
        ttk.Button(header, text="End Run / 结束", command=self.end_run).pack(side="right", padx=(8, 0))
        ttk.Button(header, text="Start Run / 开始", command=self.start_run).pack(side="right")

        main = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        main.pack(fill="both", expand=True)
        main.grid_columnconfigure(0, weight=3)
        main.grid_columnconfigure(1, weight=2)
        main.grid_rowconfigure(0, weight=1)

        form = ttk.Frame(main)
        form.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        form.grid_columnconfigure(1, weight=1)

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
        row = 0
        for key, label in [
            ("project", "Project"),
            ("title", "Title"),
            ("session_id", "Session ID"),
            ("model", "Model"),
            ("mode", "Mode"),
            ("input_tokens", "Input tokens"),
            ("output_tokens", "Output tokens"),
            ("cached_tokens", "Cached tokens"),
        ]:
            var = tk.StringVar(value=defaults[key])
            self.fields[key] = var
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(form, textvariable=var).grid(row=row, column=1, sticky="ew", pady=3)
            row += 1

        ttk.Label(form, text="Started").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Label(form, textvariable=self.started_at_var).grid(row=row, column=1, sticky="w", pady=3)
        row += 1
        ttk.Label(form, text="Ended").grid(row=row, column=0, sticky="w", pady=3)
        ttk.Label(form, textvariable=self.ended_at_var).grid(row=row, column=1, sticky="w", pady=3)
        row += 1

        for key, label in [
            ("prompt_summary", "Prompt summary / 用户指令摘要"),
            ("output_summary", "Output summary / 输出摘要"),
            ("note", "Note / 备注"),
        ]:
            ttk.Label(form, text=label).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 3))
            row += 1
            text = tk.Text(form, wrap="word", height=4)
            text.grid(row=row, column=0, columnspan=2, sticky="nsew")
            self.text_fields[key] = text
            row += 1

        ttk.Label(
            form,
            text="默认仅保存摘要和手动 token 数；不要保存凭据或 prompt/output 全文。",
            foreground="#555555",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))

        right = ttk.Frame(main)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        self.summary_label = ttk.Label(right, text="", justify="left", padding=10)
        self.summary_label.grid(row=0, column=0, sticky="ew")
        self.recent_label = ttk.Label(right, text="", justify="left", padding=10)
        self.recent_label.grid(row=1, column=0, sticky="ew")
        ttk.Label(right, textvariable=self.status_var, padding=10).grid(row=2, column=0, sticky="ew")

    def start_run(self) -> None:
        self.started_at = datetime.now()
        self.started_at_var.set(self.started_at.isoformat(timespec="seconds"))
        self.ended_at_var.set("")
        self.status_var.set("Run started / 已开始，本地估算 / local estimate")

    def end_run(self) -> None:
        if self.started_at is None:
            self.start_run()
        ended = datetime.now()
        self.ended_at_var.set(ended.isoformat(timespec="seconds"))
        self.status_var.set("Run ended / 已结束，本地估算 / local estimate")

    def save_run(self) -> None:
        run = self._run_from_form()
        result = append_run(run, RUNS_PATH)
        if result.error:
            messagebox.showerror("Storage error", result.error)
            self.status_var.set(f"Save failed: {result.error}")
            return
        self.status_var.set(f"Saved run {run.run_id} 本地估算 / local estimate")
        self.refresh(result.runs)

    def export_report(self) -> None:
        result = load_runs(RUNS_PATH)
        if result.error:
            messagebox.showerror("Storage error", result.error)
            return
        summary = summarize_runs(result.runs, self.pricing)
        path = export_report(result.runs, summary, ROOT / "reports" / f"token-waste-report-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md")
        self.status_var.set(f"Report exported: {path} 本地估算 / local estimate")

    def refresh(self, runs: list[AgentRun] | None = None) -> None:
        result = load_runs(RUNS_PATH) if runs is None else None
        loaded_runs = runs if runs is not None else result.runs
        if result and result.error:
            self.status_var.set(f"Storage warning: {result.error}")
        summary = summarize_runs(loaded_runs, self.pricing)
        self.summary_label.configure(
            text=(
                "Session Summary / 会话汇总\n"
                f"- Rounds: {summary.rounds}\n"
                f"- Session tokens: {summary.session_tokens} 本地估算 / local estimate\n"
                f"- Current run tokens: {summary.current_run_tokens} 本地估算 / local estimate\n"
                f"- Session cost: ${summary.session_cost:.6f} 本地估算 / local estimate\n"
                f"- Average cache hit: {summary.average_cache_hit * 100:.1f}% 本地估算 / local estimate\n"
                f"- Context usage: {summary.context_usage * 100:.1f}% 本地估算 / local estimate\n"
                f"- Budget remaining: ${summary.budget_remaining:.6f} 本地估算 / local estimate"
            )
        )
        recent = loaded_runs[-5:]
        recent_lines = ["Recent Runs / 最近记录"]
        recent_lines.extend(f"- {run.run_id}: {run.title} ({run.total_tokens} tokens local estimate)" for run in recent)
        if not recent:
            recent_lines.append("- No local runs saved.")
        self.recent_label.configure(text="\n".join(recent_lines))
        self._refresh_telemetry(summary)

    def _refresh_telemetry(self, summary) -> None:
        if self.telemetry_frame is not None:
            self.telemetry_frame.destroy()
        values = build_telemetry_values_from_summary(summary, self.pricing)
        self.telemetry_frame = create_telemetry_bar_from_values(self.root, values)
        self.telemetry_frame.pack(fill="x", side="bottom")

    def _run_from_form(self) -> AgentRun:
        if self.started_at is None:
            self.started_at = datetime.now()
            self.started_at_var.set(self.started_at.isoformat(timespec="seconds"))
        ended = datetime.now()
        if not self.ended_at_var.get():
            self.ended_at_var.set(ended.isoformat(timespec="seconds"))
        input_tokens = _parse_int(self.fields["input_tokens"].get())
        output_tokens = _parse_int(self.fields["output_tokens"].get())
        cached_tokens = _parse_int(self.fields["cached_tokens"].get())
        total_tokens, estimated_cost, cache_hit = build_run_estimates(input_tokens, output_tokens, cached_tokens, self.pricing)
        return AgentRun(
            run_id=f"run-{uuid.uuid4().hex[:8]}",
            session_id=self.fields["session_id"].get().strip() or "default-session",
            project=self.fields["project"].get().strip(),
            title=self.fields["title"].get().strip() or "Manual Codex run",
            started_at=self.started_at.isoformat(timespec="seconds"),
            ended_at=ended.isoformat(timespec="seconds"),
            elapsed_seconds=max(round((ended - self.started_at).total_seconds()), 0),
            model=self.fields["model"].get().strip(),
            mode=self.fields["mode"].get().strip(),
            prompt_summary=_text_value(self.text_fields["prompt_summary"]),
            output_summary=_text_value(self.text_fields["output_summary"]),
            note=_text_value(self.text_fields["note"]),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
            cache_hit=cache_hit,
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
    runs = load_runs(RUNS_PATH).runs
    summary = summarize_runs(runs, pricing)
    print("Codex Token Monitor smoke OK")
    print(f"rounds={summary.rounds} 本地估算 / local estimate")
    print(f"session_tokens={summary.session_tokens} 本地估算 / local estimate")


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
