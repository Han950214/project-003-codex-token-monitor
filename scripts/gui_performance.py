"""Deterministic, isolated GUI responsiveness benchmark for Phase 3.1-E2."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from collections import Counter, defaultdict
from pathlib import Path
from types import MethodType
from typing import Any, Callable


HEARTBEAT_MS = 16
PAGES = (
    "overview",
    "sessions",
    "usage_trends",
    "recommendations",
    "tools",
    "settings",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = index - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def _stats(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50_ms": round(_percentile(values, 0.50), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "p99_ms": round(_percentile(values, 0.99), 3),
        "max_ms": round(max(values, default=0.0), 3),
    }


class _DelayedQuotaProvider:
    def __init__(self, delegate: object) -> None:
        self.delegate = delegate
        self.delay_seconds = 0.0
        self.refresh_calls = 0

    def refresh(self):
        self.refresh_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return self.delegate.refresh()

    def refresh_thread_titles(self):
        return self.delegate.refresh_thread_titles()

    def close(self) -> None:
        self.delegate.close()


class Benchmark:
    def __init__(self, run_id: str, output: Path, *, quiet: bool = False) -> None:
        data_root = Path(tempfile.mkdtemp(prefix="codex-token-monitor-e2-perf-"))
        os.environ["CODEX_TOKEN_MONITOR_DATA_DIR"] = str(data_root)
        sessions = data_root / "empty-codex-sessions"
        sessions.mkdir()
        os.environ["CODEX_SESSIONS_DIR"] = str(sessions)

        import customtkinter as ctk
        import tkinter as tk
        from app.main import Dashboard
        from scripts.gui_acceptance import (
            _SafeQaQuotaProvider,
            _apply_scenario,
            build_scenario,
        )

        self.ctk = ctk
        self.tk = tk
        self.run_id = run_id
        self.quiet = quiet
        self.output = output.resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.callback_errors: list[str] = []
        self.worker_errors: list[str] = []
        self.heartbeats: dict[str, list[float]] = defaultdict(list)
        self.phase = "startup"
        self.phase_started = time.perf_counter()
        self.phase_durations: dict[str, float] = {}
        self.phase_counters: dict[str, dict[str, int]] = {}
        self.callback_times: dict[str, list[float]] = defaultdict(list)
        self.render_counts: Counter[str] = Counter()
        self.hidden_render_counts: Counter[str] = Counter()
        self.layout_counts: Counter[str] = Counter()
        self.canvas_redraw_counts: Counter[str] = Counter()
        self.configure_events = 0
        self._last_heartbeat: float | None = None

        original_delete = tk.Canvas.delete

        def counted_delete(canvas, *args):
            if args and args[0] == "all":
                self.canvas_redraw_counts[type(canvas).__name__] += 1
            return original_delete(canvas, *args)

        tk.Canvas.delete = counted_delete
        self._restore_canvas_delete = lambda: setattr(tk.Canvas, "delete", original_delete)

        self.root = ctk.CTk()
        self.root.report_callback_exception = self._report_callback_exception
        scenario = build_scenario("semantic_current_selected_history", data_root, range_days=7)
        self.provider = _DelayedQuotaProvider(_SafeQaQuotaProvider(scenario.current_observed_at))
        started = time.perf_counter()
        self.dashboard = Dashboard(
            self.root,
            quota_provider=self.provider,
            history_store=scenario.store,
        )
        self.root.update_idletasks()
        self.root.update()
        self.initial_display_ms = (time.perf_counter() - started) * 1000
        self.dashboard.auto_refresh.set_enabled(False)
        self.dashboard.auto_refresh_var.set(False)
        self.dashboard._request_history_backfill = lambda *args, **kwargs: None
        self.scenario = scenario
        self._apply_scenario = _apply_scenario
        self._instrument_dashboard()
        self.root.bind("<Configure>", self._count_configure, add="+")

    def _report_callback_exception(self, kind, error, error_traceback) -> None:
        self.callback_errors.append(f"{kind.__name__}: {error}")
        traceback.print_exception(kind, error, error_traceback)

    def _count_configure(self, event: object) -> None:
        if getattr(event, "widget", None) is self.root:
            self.configure_events += 1

    def _instrument_dashboard(self) -> None:
        render_pages = {
            "_render_sessions": {"sessions"},
            "_render_advisor": {"overview"},
            "_render_observed_usage": {"overview"},
            "_render_usage_insights": {"usage_trends"},
            "_render_safe_overview": {"overview", "session_detail"},
            "_render_status_recent": {"overview"},
            "_render_trends": {"overview", "usage_trends"},
            "_render_recommendations": {"recommendations"},
            "_render_diagnostics": {"tools"},
        }
        for name, pages in render_pages.items():
            if not hasattr(self.dashboard, name):
                continue
            original = getattr(self.dashboard, name)

            def wrapper(instance, *args, _name=name, _pages=pages, _original=original, **kwargs):
                started = time.perf_counter()
                self.render_counts[_name] += 1
                if instance.current_nav_page not in _pages:
                    self.hidden_render_counts[_name] += 1
                try:
                    return _original(*args, **kwargs)
                finally:
                    self.callback_times[_name].append(
                        (time.perf_counter() - started) * 1000,
                    )

            setattr(self.dashboard, name, MethodType(wrapper, self.dashboard))

        for name in (
            "_apply_responsive_layout",
            "_poll_refresh_results",
            "_apply_refresh_payload",
            "_apply_presentation",
            "_schedule_trend_query",
            "_apply_status_layout",
            "_layout_history_controls",
            "_layout_history_columns",
            "_layout_sessions_page",
            "_layout_tool_groups",
            "_layout_settings_groups",
            "_layout_trend_metrics",
        ):
            if not hasattr(self.dashboard, name):
                continue
            original = getattr(self.dashboard, name)

            def wrapper(instance, *args, _name=name, _original=original, **kwargs):
                started = time.perf_counter()
                try:
                    return _original(*args, **kwargs)
                finally:
                    self.layout_counts[_name] += 1
                    self.callback_times[_name].append((time.perf_counter() - started) * 1000)

            setattr(self.dashboard, name, MethodType(wrapper, self.dashboard))

        original_show_page = self.dashboard.show_page

        def timed_show_page(instance, page: str):
            started = time.perf_counter()
            try:
                return original_show_page(page)
            finally:
                self.callback_times["show_page"].append((time.perf_counter() - started) * 1000)

        self.dashboard.show_page = MethodType(timed_show_page, self.dashboard)

    def _heartbeat(self) -> None:
        now = time.perf_counter()
        if self._last_heartbeat is not None:
            self.heartbeats[self.phase].append((now - self._last_heartbeat) * 1000)
        self._last_heartbeat = now
        if not self.dashboard._closing:
            self.root.after(HEARTBEAT_MS, self._heartbeat)

    def _begin(self, phase: str) -> None:
        if self.phase_started:
            self.phase_durations[self.phase] = (time.perf_counter() - self.phase_started) * 1000
            self._capture_phase_counters(self.phase)
        self.phase = phase
        self.phase_started = time.perf_counter()
        self._last_heartbeat = self.phase_started
        self._phase_counter_start = self._counter_snapshot()

    def _counter_snapshot(self) -> dict[str, int]:
        return {
            "layout_applied": getattr(self.dashboard, "_layout_apply_count", 0),
            "layout_skipped": getattr(self.dashboard, "_layout_skip_count", 0),
            "layout_debounced": getattr(self.dashboard, "_layout_debounce_count", 0),
            "renders": sum(self.render_counts.values()),
            "hidden_renders": sum(self.hidden_render_counts.values()),
            "canvas_redraws": sum(self.canvas_redraw_counts.values()),
            "configure_events": self.configure_events,
        }

    def _capture_phase_counters(self, phase: str) -> None:
        start = getattr(self, "_phase_counter_start", self._counter_snapshot())
        end = self._counter_snapshot()
        self.phase_counters[phase] = {
            key: end[key] - start[key] for key in end
        }

    def _scroll(self, page: str, attribute: str, done: Callable[[], None]) -> None:
        self.dashboard.show_page(page)
        frame = getattr(
            self.dashboard,
            attribute,
            self.dashboard.page_frames.get(page),
        )
        canvas = getattr(frame, "_parent_canvas", None)
        pending = list(frame.winfo_children()) if frame is not None else []
        while canvas is None and pending:
            child = pending.pop(0)
            canvas = getattr(child, "_parent_canvas", None)
            pending.extend(child.winfo_children())
        if canvas is None:
            self.callback_errors.append(f"missing_scroll_canvas:{attribute}")
            done()
            return
        steps = [(5 if index % 2 == 0 else -5) for index in range(40)]

        def step(index: int = 0) -> None:
            if index >= len(steps):
                done()
                return
            canvas.yview_scroll(steps[index], "units")
            self.root.after(20, lambda: step(index + 1))

        step()

    def run(self) -> None:
        self.root.geometry("1280x800+20+20")
        self.root.after(16, self._heartbeat)

        def setup() -> None:
            self._apply_scenario(self.dashboard, self.scenario, "overview")
            self.root.update_idletasks()
            self.render_counts.clear()
            self.hidden_render_counts.clear()
            self.layout_counts.clear()
            self.canvas_redraw_counts.clear()
            self.configure_events = 0
            self._begin("slow_refresh")
            self.provider.delay_seconds = 1.5
            self.root.after(100, lambda: self.dashboard.show_page("tools"))
            self.root.after(10, self.dashboard.manual_refresh)
            self.root.after(1900, maximize_restore)

        wait_started = time.perf_counter()

        def wait_for_startup_refresh() -> None:
            worker = getattr(self.dashboard, "refresh_worker", None)
            if (
                worker is not None
                and worker.busy
                and time.perf_counter() - wait_started < 10
            ):
                self.root.after(50, wait_for_startup_refresh)
                return
            setup()

        def maximize_restore() -> None:
            self.dashboard.show_page("sessions")
            self._begin("maximize_restore")
            self.provider.delay_seconds = 0.0

            def cycle(index: int = 0) -> None:
                if index >= 20:
                    self.root.after(300, resize)
                    return
                try:
                    self.root.state("zoomed" if index % 2 == 0 else "normal")
                except self.tk.TclError as error:
                    self.callback_errors.append(f"maximize_restore:{error}")
                self.root.after(140, lambda: cycle(index + 1))

            cycle()

        def resize() -> None:
            self._begin("resize")
            sizes = []
            for group in range(25):
                width = 1000 + ((group % 5) * 70)
                for offset in (0, 8, 16, 24):
                    sizes.append((width, 700 + offset))

            def step(index: int = 0) -> None:
                if index >= len(sizes):
                    self.root.after(300, overview_scroll)
                    return
                width, height = sizes[index]
                self.root.geometry(f"{width}x{height}+20+20")
                self.root.after(150, lambda: step(index + 1))

            step()

        def overview_scroll() -> None:
            self._begin("overview_scroll")
            self._scroll("overview", "status_page", trends_scroll)

        def trends_scroll() -> None:
            self._begin("trends_scroll")
            self._scroll("usage_trends", "trends_page", page_switch)

        def page_switch() -> None:
            self._begin("page_switch")

            def step(index: int = 0) -> None:
                if index >= 30:
                    self.root.after(250, repeated_refresh)
                    return
                self.dashboard.show_page(PAGES[index % len(PAGES)])
                self.root.after(35, lambda: step(index + 1))

            step()

        def repeated_refresh() -> None:
            self._begin("repeated_refresh")
            self.provider.delay_seconds = 1.5
            for _ in range(5):
                self.dashboard.manual_refresh()
            self.root.after(100, lambda: self.dashboard.show_page("usage_trends"))
            self.root.after(180, lambda: self.root.geometry("1180x720+20+20"))
            self.root.after(3500, close_while_refreshing)

        def close_while_refreshing() -> None:
            self._begin("close_while_refreshing")
            self.provider.delay_seconds = 1.5
            self.dashboard.manual_refresh()
            close_requested = time.perf_counter()

            def close() -> None:
                self.close_latency_ms = (time.perf_counter() - close_requested) * 1000
                self.dashboard.close()

            self.root.after(100, close)

        self.root.after(1200, wait_for_startup_refresh)
        self.root.mainloop()
        self.phase_durations[self.phase] = (time.perf_counter() - self.phase_started) * 1000
        self._capture_phase_counters(self.phase)
        worker = getattr(self.dashboard, "refresh_worker", None)
        if worker is not None:
            worker.wait_until_stopped(2)
        self._restore_canvas_delete()
        self._write_result()

    def _write_result(self) -> None:
        worker = getattr(self.dashboard, "refresh_worker", None)
        worker_metrics: dict[str, Any] = {}
        if worker is not None:
            metrics = getattr(worker, "metrics", None)
            if metrics is not None:
                worker_metrics = (
                    dict(metrics)
                    if isinstance(metrics, dict)
                    else vars(metrics).copy()
                )
            errors = getattr(worker, "errors", ())
            self.worker_errors.extend(str(error) for error in errors)
        result = {
            "run_id": self.run_id,
            "isolated_data": True,
            "initial_display_ms": round(self.initial_display_ms, 3),
            "phase_duration_ms": {key: round(value, 3) for key, value in self.phase_durations.items()},
            "phase_counters": self.phase_counters,
            "heartbeat": {key: _stats(value) for key, value in self.heartbeats.items()},
            "callbacks": {key: _stats(value) for key, value in self.callback_times.items()},
            "render_counts": dict(self.render_counts),
            "hidden_render_counts": dict(self.hidden_render_counts),
            "layout_counts": dict(self.layout_counts),
            "layout_apply_count": getattr(self.dashboard, "_layout_apply_count", 0),
            "layout_skip_count": getattr(self.dashboard, "_layout_skip_count", 0),
            "layout_debounce_count": getattr(self.dashboard, "_layout_debounce_count", 0),
            "canvas_redraw_counts": dict(self.canvas_redraw_counts),
            "configure_events": self.configure_events,
            "quota_refresh_calls": self.provider.refresh_calls,
            "worker_metrics": worker_metrics,
            "close_latency_ms": round(getattr(self, "close_latency_ms", 0.0), 3),
            "callback_errors": self.callback_errors,
            "worker_errors": self.worker_errors,
        }
        self.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if not self.quiet:
            print(json.dumps(result, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    Benchmark(args.run_id, args.output, quiet=args.quiet).run()


if __name__ == "__main__":
    main()
