"""Markdown report export for local-estimate token telemetry."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.metrics import SessionSummary
from app.models import AgentRun
from app.paths import reports_dir


DEFAULT_REPORTS_DIR = reports_dir()
LOCAL_ESTIMATE = "本地估算 / local estimate"
REAL_TOTAL = "codex_state_sqlite / real total"
REAL_USAGE = "codex_logs_sqlite / real usage"


def default_report_path(now: datetime | None = None, output_dir: Path | None = None) -> Path:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return (output_dir or reports_dir()) / f"token-waste-report-{stamp}.md"


def render_report(runs: list[AgentRun], summary: SessionSummary, generated_at: datetime | None = None) -> str:
    now = generated_at or datetime.now()
    recent = runs[-5:]
    lines = [
        "# Codex Token Waste Report",
        "",
        f"Generated: {now.isoformat(timespec='seconds')}",
        "",
        f"> Real total applies only to session total tokens when the source is {REAL_TOTAL}. Input/output/cache/reasoning/cost/context/budget remain {LOCAL_ESTIMATE} or unknown unless latest response.completed numeric usage is labeled {REAL_USAGE}; no session/thread aggregation is performed.",
        "> Cache hit may be derived from real usage, not official cache hit rate; cost remains an estimate, not billing.",
        "",
        "## Session Summary",
        "",
        f"- Run count: {summary.rounds}",
        f"- Session tokens: {summary.session_tokens} {_total_tokens_label(summary)}",
        f"- Current run tokens: {summary.current_run_tokens} {_current_usage_label(summary)}",
        f"- Current estimated cost: ${summary.current_cost:.6f} local estimate, not billing",
        f"- Session estimated cost: ${summary.session_cost:.6f} local estimate, not billing",
        f"- Current cache hit: {summary.current_cache_hit * 100:.1f}% {_cache_hit_label(summary)}",
        f"- Average cache hit: {summary.average_cache_hit * 100:.1f}% local estimate, not real Codex cache",
        f"- Context usage: {summary.context_usage * 100:.1f}% {LOCAL_ESTIMATE}",
        f"- Budget remaining: ${summary.budget_remaining:.6f} {LOCAL_ESTIMATE}",
        "",
        "## Recent Runs",
        "",
    ]
    if not recent:
        lines.append("- No local runs saved.")
    for run in recent:
        lines.extend(
            [
                f"- `{run.run_id}` {run.title}",
                f"  - Project: {run.project}",
                f"  - Prompt summary: {run.prompt_summary}",
                f"  - Output summary: {run.output_summary}",
                f"  - Note: {run.note}",
                f"  - Tokens: {run.total_tokens} {LOCAL_ESTIMATE}",
                f"  - Cost: ${run.estimated_cost:.6f} {LOCAL_ESTIMATE}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _total_tokens_label(summary: SessionSummary) -> str:
    return REAL_TOTAL if summary.total_tokens_source == "codex_state_sqlite" else LOCAL_ESTIMATE


def _current_usage_label(summary: SessionSummary) -> str:
    return REAL_USAGE if summary.current_usage_source == "codex_logs_sqlite" else LOCAL_ESTIMATE


def _cache_hit_label(summary: SessionSummary) -> str:
    if summary.current_cache_hit_source == "codex_logs_sqlite":
        return "derived from codex_logs_sqlite / real usage, not official cache hit rate"
    return "local estimate, not real Codex cache"


def export_report(
    runs: list[AgentRun],
    summary: SessionSummary,
    path: Path | None = None,
    generated_at: datetime | None = None,
) -> Path:
    report_path = path or default_report_path(generated_at)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(runs, summary, generated_at), encoding="utf-8")
    return report_path
