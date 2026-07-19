"""Privacy-safe real-data GUI profiler for Phase 3.1-E3.

The profiler uses the normal Dashboard and local data sources, but opens the
history database read-only and records only timings, counts, widget types, page
names, callback counts, and anonymous aggregate statistics.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import json
import sqlite3
import sys
import time
import traceback
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HEARTBEAT_MS = 16
PAGES = (
    "overview",
    "sessions",
    "usage_trends",
    "recommendations",
    "tools",
    "settings",
)
BUILD_METHODS = (
    "_build_content",
    "_build_status_center",
    "_build_current_task_page",
    "_build_history_page",
    "_build_usage_trends_page",
    "_build_recommendations_page",
    "_build_tools_page",
    "_build_settings_page",
)
PROFILE_METHODS = BUILD_METHODS + (
    "show_page",
    "_render_visible_page",
    "_apply_responsive_layout",
    "_apply_status_layout",
    "_layout_core_metrics",
    "_layout_observed_usage",
    "_layout_history_controls",
    "_layout_history_columns",
    "_layout_sessions_page",
    "_layout_tool_groups",
    "_layout_settings_groups",
    "_layout_trend_metrics",
    "_render_safe_overview",
    "_render_sessions",
    "_render_trends",
    "_render_usage_insights",
    "_render_recommendations",
)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _stats(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values),
        "p50_ms": round(_percentile(values, 0.50), 3),
        "p95_ms": round(_percentile(values, 0.95), 3),
        "p99_ms": round(_percentile(values, 0.99), 3),
        "max_ms": round(max(values, default=0.0), 3),
        "total_ms": round(sum(values), 3),
    }


class RealDataProfiler:
    def __init__(
        self,
        run_id: str,
        output: Path,
        *,
        quiet: bool = False,
        exercise_e3_f1: bool = False,
        language: str | None = None,
        geometry: str = "1280x800",
    ) -> None:
        import customtkinter as ctk
        import tkinter as tk
        from app.history import UsageHistoryStore
        import app.main as main_module

        self.ctk = ctk
        self.tk = tk
        self.run_id = run_id
        self.output = output.resolve()
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.quiet = quiet
        self.exercise_e3_f1 = exercise_e3_f1
        self.geometry = geometry
        self.e3_f1_checks: dict[str, bool] = {}
        self.e3_f1_persistence_calls: list[bool] = []
        self._restore_auto_refresh_persistence: Callable[[], None] | None = None
        self.callback_errors: list[str] = []
        self.worker_errors: list[str] = []
        self.phase = "startup"
        self.phase_started = time.perf_counter()
        self.phase_durations: dict[str, float] = {}
        self.heartbeats: dict[str, list[float]] = defaultdict(list)
        self.method_times: dict[str, list[float]] = defaultdict(list)
        self.method_counts: Counter[str] = Counter()
        self.phase_method_counts: dict[str, Counter[str]] = defaultdict(Counter)
        self.scrollregion_updates: Counter[str] = Counter()
        self.hidden_page_configures: Counter[str] = Counter()
        self.page_configure_bind_counts: Counter[str] = Counter()
        self.page_configure_event_counts: Counter[str] = Counter()
        self.widget_counts: dict[str, dict[str, int]] = {}
        self.page_first_layout_ms: dict[str, float] = {}
        self.page_first_render_ms: dict[str, float] = {}
        self._phase_started_at = time.perf_counter()
        self._last_heartbeat: float | None = None
        self._finished = False

        class ReadOnlyHistoryStore(UsageHistoryStore):
            def initialize(self) -> bool:
                if not self.path.is_file():
                    self.last_error = "history_storage_unavailable"
                    self._initialized = False
                    return False
                try:
                    with closing(self._connect()) as connection:
                        connection.execute("SELECT 1").fetchone()
                    self._initialized = True
                    self.last_error = None
                    return True
                except (OSError, sqlite3.Error):
                    self._initialized = False
                    self.last_error = "history_storage_unavailable"
                    return False

            def _connect(self) -> sqlite3.Connection:
                uri = self.path.resolve().as_uri() + "?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=2.0)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA busy_timeout=2000")
                return connection

            def record(self, observation: object) -> bool:
                self.last_error = None
                return True

        self.history_store = ReadOnlyHistoryStore()
        main_module._new_backfill_history_store = lambda primary: primary
        self._main_module = main_module
        Dashboard = main_module.Dashboard
        self._instrument_dashboard_class(Dashboard)
        self._instrument_page_frame_creation(Dashboard)
        self._instrument_tk(tk)

        self.root = ctk.CTk()
        self.root.report_callback_exception = self._report_callback_exception
        self.root.geometry(f"{self.geometry}+20+20")
        started = time.perf_counter()
        self.dashboard = Dashboard(self.root, history_store=self.history_store)
        self.constructor_ms = (time.perf_counter() - started) * 1000
        self.dashboard._request_history_backfill = lambda *args, **kwargs: False
        self.dashboard.auto_refresh.set_enabled(False)
        self.dashboard.auto_refresh_var.set(False)
        if language is not None:
            self.dashboard.language = language
            self.dashboard._apply_language(language)
        self._bind_page_configures()

    def _instrument_dashboard_class(self, dashboard_type: type) -> None:
        for name in PROFILE_METHODS:
            original = getattr(dashboard_type, name, None)
            if original is None or getattr(original, "_e3_profiled", False):
                continue

            def wrapper(instance, *args, _name=name, _original=original, **kwargs):
                started = time.perf_counter()
                try:
                    return _original(instance, *args, **kwargs)
                finally:
                    elapsed = (time.perf_counter() - started) * 1000
                    self.method_times[_name].append(elapsed)
                    self.method_counts[_name] += 1
                    self.phase_method_counts[self.phase][_name] += 1

            wrapper._e3_profiled = True
            setattr(dashboard_type, name, wrapper)

    def _instrument_page_frame_creation(self, dashboard_type: type) -> None:
        original = getattr(dashboard_type, "_create_page_frame", None)
        if original is None or getattr(original, "_e3_profiled", False):
            return

        def create_page_frame(instance, page: str, *args, **kwargs):
            frame = original(instance, page, *args, **kwargs)
            self._bind_page_configure(page, frame)
            return frame

        create_page_frame._e3_profiled = True
        setattr(dashboard_type, "_create_page_frame", create_page_frame)

    def _instrument_tk(self, tk: Any) -> None:
        original_configure = tk.Canvas.configure

        def configure(canvas, *args, **kwargs):
            options = kwargs
            if args and isinstance(args[-1], dict):
                options = {**args[-1], **kwargs}
            if "scrollregion" in options:
                self.scrollregion_updates[self.phase] += 1
            return original_configure(canvas, *args, **kwargs)

        tk.Canvas.configure = configure
        tk.Canvas.config = configure
        self._restore_canvas_configure = lambda: (
            setattr(tk.Canvas, "configure", original_configure),
            setattr(tk.Canvas, "config", original_configure),
        )

    def _bind_page_configures(self) -> None:
        for page, frame in self.dashboard.page_frames.items():
            self._bind_page_configure(page, frame)

    def _bind_page_configure(self, page: str, frame: Any) -> None:
        if getattr(frame, "_e3_profile_configure_bound", False):
            return
        frame._e3_profile_configure_bound = True
        self.page_configure_bind_counts[page] += 1
        frame.bind(
            "<Configure>",
            lambda _event, target=page, source=frame: self._count_page_configure(
                target, source,
            ),
            add="+",
        )

    def _count_page_configure(self, page: str, source: Any) -> None:
        if self.dashboard.page_frames.get(page) is not source:
            return
        self.page_configure_event_counts[page] += 1
        if page != self.dashboard.current_nav_page:
            self.hidden_page_configures[page] += 1

    def _wait_for_page_built(
        self, page: str, done: Callable[[], None], *, timeout: float = 15.0,
    ) -> None:
        started = time.perf_counter()

        def poll() -> None:
            if page in getattr(self.dashboard, "built_pages", set()):
                done()
                return
            if time.perf_counter() - started >= timeout:
                self.callback_errors.append(f"page_build_timeout:{page}")
                done()
                return
            self.root.after(25, poll)

        poll()

    def _exercise_e3_f1_gui_flow(self, finish: Callable[[], None]) -> None:
        original_save = self._main_module.save_auto_refresh_enabled

        def record_persistence(enabled: bool, _path: Path) -> bool:
            self.e3_f1_persistence_calls.append(enabled)
            return True

        self._main_module.save_auto_refresh_enabled = record_persistence
        self._restore_auto_refresh_persistence = lambda: setattr(
            self._main_module, "save_auto_refresh_enabled", original_save,
        )
        dashboard = self.dashboard
        self.e3_f1_checks["settings_unbuilt_on_start"] = (
            not dashboard._page_is_built("settings")
            and not hasattr(dashboard, "settings_auto_switch")
        )
        dashboard.auto_refresh_var.set(True)
        dashboard._toggle_auto_refresh()
        self.e3_f1_checks["header_auto_refresh"] = bool(
            dashboard.auto_refresh_var.get()
            and getattr(dashboard.auto_refresh, "enabled", False)
        )
        dashboard._toggle_auto_refresh_from_tray()
        self.e3_f1_checks["tray_auto_refresh"] = not bool(
            dashboard.auto_refresh_var.get()
        )
        dashboard.show_page("settings")

        def settings_ready() -> None:
            self.e3_f1_checks["settings_reflects_latest_state"] = bool(
                dashboard._page_is_built("settings")
                and dashboard.settings_auto_switch.cget("text")
                == self._main_module.translate("disabled", dashboard.language)
            )
            dashboard.show_page("sessions")
            self._wait_for_page_built("sessions", sessions_ready)

        def sessions_ready() -> None:
            dashboard.session_search_var.set("E3-F1 lifecycle")
            dashboard.show_page("usage_trends")
            self._wait_for_page_built("usage_trends", trends_ready)

        def trends_ready() -> None:
            dashboard.show_page("recommendations")
            self._wait_for_page_built("recommendations", recommendations_ready)

        def recommendations_ready() -> None:
            self.root.after(2300, prune_ready)

        def prune_ready() -> None:
            built = getattr(dashboard, "built_pages", set())
            self.e3_f1_checks["heavy_pages_pruned"] = bool(
                dashboard.current_nav_page == "recommendations"
                and "recommendations" in built
                and "usage_trends" in built
                and "sessions" not in built
            )
            dashboard.show_page("sessions")
            self._wait_for_page_built("sessions", rebuilt_sessions_ready)

        def rebuilt_sessions_ready() -> None:
            self.e3_f1_checks["session_state_restored"] = (
                dashboard.session_search_var.get() == "E3-F1 lifecycle"
            )
            self._begin("e3_f1_resize")
            self._exercise_e3_f1_resize(finish)

        self._wait_for_page_built("settings", settings_ready)

    def _exercise_e3_f1_resize(self, finish: Callable[[], None]) -> None:
        started = time.perf_counter()
        index = [0]

        def step() -> None:
            if time.perf_counter() - started >= 3.0:
                self.e3_f1_checks["callback_errors_empty"] = not self.callback_errors
                if self._restore_auto_refresh_persistence is not None:
                    self._restore_auto_refresh_persistence()
                    self._restore_auto_refresh_persistence = None
                finish()
                return
            self.root.geometry(
                f"{980 + (index[0] % 8) * 50}x{660 + (index[0] % 5) * 30}+20+20"
            )
            index[0] += 1
            self.root.after(50, step)

        step()

    def _report_callback_exception(self, kind, error, error_traceback) -> None:
        self.callback_errors.append(kind.__name__)
        self.output.with_suffix(".error").write_text(
            "".join(traceback.format_list(traceback.extract_tb(error_traceback))),
            encoding="utf-8",
        )
        print(f"GUI callback failed: {kind.__name__}", file=sys.stderr)

    def _heartbeat(self) -> None:
        now = time.perf_counter()
        if self._last_heartbeat is not None:
            self.heartbeats[self.phase].append((now - self._last_heartbeat) * 1000)
        self._last_heartbeat = now
        if not self._finished:
            self.root.after(HEARTBEAT_MS, self._heartbeat)

    def _begin(self, phase: str) -> None:
        now = time.perf_counter()
        self.phase_durations[self.phase] = (now - self._phase_started_at) * 1000
        self.phase = phase
        self._phase_started_at = now
        self._last_heartbeat = now
        self.output.with_suffix(".phase").write_text(phase, encoding="utf-8")
        if not self.quiet:
            print(f"phase={phase}", flush=True)

    @staticmethod
    def _walk_widgets(widget: Any) -> list[Any]:
        result: list[Any] = []
        pending = [widget]
        while pending:
            current = pending.pop()
            result.append(current)
            try:
                pending.extend(current.winfo_children())
            except Exception:
                continue
        return result

    def _count_widgets(self, page: str, widget: Any) -> dict[str, int]:
        widgets = self._walk_widgets(widget)
        classes = Counter(type(item).__name__ for item in widgets)
        variables: set[str] = set()
        for item in widgets:
            for option in ("textvariable", "variable"):
                try:
                    value = item.cget(option)
                except Exception:
                    continue
                if value:
                    variables.add(str(value))
        result = {
            "CTkFrame": classes["CTkFrame"],
            "CTkLabel": classes["CTkLabel"],
            "CTkButton": classes["CTkButton"],
            "CTkScrollableFrame": classes["CTkScrollableFrame"],
            "Canvas": classes["Canvas"],
            "Treeview": classes["Treeview"],
            "StringVar": len(variables),
            "total": len(widgets),
        }
        self.widget_counts[page] = result
        return result

    def _capture_widget_counts(self) -> None:
        self._count_widgets("application", self.root)
        for page, frame in self.dashboard.page_frames.items():
            self._count_widgets(page, frame)
        mini = getattr(self.dashboard, "mini_widget", None)
        mini_window = getattr(mini, "window", None)
        if mini_window is not None:
            self._count_widgets("mini_widget", mini_window)

    def _find_scroll_target(self, page: str) -> Any | None:
        if page == "sessions":
            return getattr(self.dashboard, "sessions_tree", None)
        frame = self.dashboard.page_frames.get(page)
        for widget in self._walk_widgets(frame) if frame is not None else ():
            canvas = getattr(widget, "_parent_canvas", None)
            if canvas is not None:
                return canvas
        return None

    def _wait_for_refresh(self, done: Callable[[], None], *, timeout: float = 45.0) -> None:
        started = time.perf_counter()

        def poll() -> None:
            worker = self.dashboard.refresh_worker
            if worker.busy and time.perf_counter() - started < timeout:
                self.root.after(50, poll)
                return
            self.root.after(250, done)

        poll()

    def _scroll_page(self, page: str, done: Callable[[], None]) -> None:
        self.dashboard.show_page(page)
        wait_started = time.perf_counter()

        def wait_for_page() -> None:
            built_pages = getattr(
                self.dashboard, "built_pages", self.dashboard.page_frames,
            )
            if page not in built_pages:
                if time.perf_counter() - wait_started < 15:
                    self.root.after(25, wait_for_page)
                    return
                self.callback_errors.append(f"page_build_timeout:{page}")
                done()
                return
            target = self._find_scroll_target(page)
            if target is None:
                self.callback_errors.append(f"missing_scroll_target:{page}")
                done()
                return
            phase = {
                "overview": "overview_scroll",
                "sessions": "sessions_scroll",
                "usage_trends": "trends_scroll",
            }[page]

            def start_scroll() -> None:
                self._begin(phase)
                run_scroll(target, phase)

            self.root.after(500, start_scroll)

        def run_scroll(target: Any, phase: str) -> None:
            before_render = sum(
                count for name, count in self.method_counts.items() if name.startswith("_render")
            )
            before_build = sum(
                count for name, count in self.method_counts.items() if name.startswith("_build")
            )

            def step(index: int = 0) -> None:
                if index >= 40:
                    after_render = sum(
                        count for name, count in self.method_counts.items() if name.startswith("_render")
                    )
                    after_build = sum(
                        count for name, count in self.method_counts.items() if name.startswith("_build")
                    )
                    self.phase_method_counts[self.phase]["scroll_render_delta"] = after_render - before_render
                    self.phase_method_counts[self.phase]["scroll_build_delta"] = after_build - before_build
                    self._begin(f"{phase}_complete")
                    self.root.after(250, done)
                    return
                target.yview_scroll(8 if index % 2 == 0 else -8, "units")
                self.root.after(20, lambda: step(index + 1))

            step()

        wait_for_page()

    def run(self) -> None:
        self.root.after(HEARTBEAT_MS, self._heartbeat)
        startup_started = time.perf_counter()

        def wait_for_startup() -> None:
            if (
                (self.dashboard.presentation is None or self.dashboard.refresh_worker.busy)
                and time.perf_counter() - startup_started < 60
            ):
                self.root.after(50, wait_for_startup)
                return
            self.startup_ready_ms = (time.perf_counter() - startup_started) * 1000
            self._capture_widget_counts()
            self.startup_widget_counts = {
                key: dict(value) for key, value in self.widget_counts.items()
            }
            if self.exercise_e3_f1:
                self._exercise_e3_f1_gui_flow(finish)
                return
            self._begin("maximize_restore")
            maximize_restore()

        def maximize_restore(index: int = 0) -> None:
            if index >= 20:
                self.root.after(300, lambda: (self._begin("resize"), resize()))
                return
            try:
                self.root.state("zoomed" if index % 2 == 0 else "normal")
            except self.tk.TclError as error:
                self.callback_errors.append(f"maximize_restore:{error}")
            self.root.after(150, lambda: maximize_restore(index + 1))

        resize_started = [0.0]
        resize_index = [0]

        def resize() -> None:
            if not resize_started[0]:
                resize_started[0] = time.perf_counter()
            if time.perf_counter() - resize_started[0] >= 15.0:
                self.root.after(300, lambda: self._scroll_page("overview", sessions_scroll))
                return
            index = resize_index[0]
            width = 980 + (index % 11) * 42
            height = 660 + (index % 7) * 28
            self.root.geometry(f"{width}x{height}+20+20")
            resize_index[0] += 1
            self.root.after(50, resize)

        def sessions_scroll() -> None:
            self._scroll_page("sessions", trends_scroll)

        def trends_scroll() -> None:
            self._scroll_page("usage_trends", page_switch)

        def page_switch() -> None:
            self._begin("page_switch")

            def step(index: int = 0) -> None:
                if index >= 30:
                    self.root.after(250, warm_page_switch)
                    return
                self.dashboard.show_page(PAGES[index % len(PAGES)])
                self.root.after(60, lambda: step(index + 1))

            step()

        def warm_page_switch() -> None:
            self._begin("warm_page_switch")

            def step(index: int = 0) -> None:
                if index >= 30:
                    self._capture_widget_counts()
                    self.dashboard.show_page("overview")
                    self.root.after(2500, postvisit_resize)
                    return
                self.dashboard.show_page(PAGES[index % len(PAGES)])
                self.root.after(60, lambda: step(index + 1))

            step()

        postvisit_started = [0.0]
        postvisit_index = [0]

        def postvisit_resize() -> None:
            if not postvisit_started[0]:
                self._begin("postvisit_resize")
                postvisit_started[0] = time.perf_counter()
            if time.perf_counter() - postvisit_started[0] >= 5.0:
                self.root.after(250, manual_refresh)
                return
            index = postvisit_index[0]
            self.root.geometry(
                f"{1000 + (index % 9) * 45}x{680 + (index % 5) * 30}+20+20"
            )
            postvisit_index[0] += 1
            self.root.after(50, postvisit_resize)

        def manual_refresh() -> None:
            self._begin("manual_refresh")
            self.dashboard.manual_refresh()
            self._wait_for_refresh(auto_refresh)

        def auto_refresh() -> None:
            self._begin("auto_refresh")
            self.dashboard.refresh(show_refreshing=False)
            self._wait_for_refresh(diagnostics)

        def diagnostics() -> None:
            self._begin("diagnostics")
            self.dashboard.show_page("tools")
            self.dashboard.start_diagnostics()
            started = time.perf_counter()

            def wait() -> None:
                if self.dashboard.diagnostics_worker.busy and time.perf_counter() - started < 30:
                    self.root.after(50, wait)
                    return
                self.root.after(300, mini_widget)

            wait()

        def mini_widget() -> None:
            self._begin("mini_widget")
            self.dashboard._enter_widget_mode()
            self.root.after(600, self.dashboard.restore_dashboard)
            self.root.after(1200, finish)

        def finish() -> None:
            self._begin("close")
            self._capture_widget_counts()
            close_started = time.perf_counter()
            self._finished = True
            self.dashboard.close()
            self.close_latency_ms = (time.perf_counter() - close_started) * 1000

        self.root.after(50, wait_for_startup)
        self.root.mainloop()
        if self._restore_auto_refresh_persistence is not None:
            self._restore_auto_refresh_persistence()
            self._restore_auto_refresh_persistence = None
        self.phase_durations[self.phase] = (time.perf_counter() - self._phase_started_at) * 1000
        self._restore_canvas_configure()
        self._write_result()

    def _write_result(self) -> None:
        worker = self.dashboard.refresh_worker
        diagnostics_worker = self.dashboard.diagnostics_worker
        self.worker_errors.extend(
            type(error).__name__ for error in getattr(worker, "errors", ())
        )
        worker_metrics = worker.metrics
        if isinstance(worker_metrics, dict):
            serialized_worker_metrics = dict(worker_metrics)
        elif is_dataclass(worker_metrics):
            serialized_worker_metrics = asdict(worker_metrics)
        else:
            serialized_worker_metrics = {
                name: getattr(worker_metrics, name)
                for name in dir(worker_metrics)
                if not name.startswith("_")
                and isinstance(getattr(worker_metrics, name), (int, float, str, bool))
            }
        result = {
            "run_id": self.run_id,
            "real_data": True,
            "history_database_mode": "read_only",
            "content_fields_recorded": False,
            "raw_thread_ids_recorded": False,
            "thread_names_recorded": False,
            "constructor_ms": round(self.constructor_ms, 3),
            "startup_ready_ms": round(getattr(self, "startup_ready_ms", 0.0), 3),
            "phase_duration_ms": {key: round(value, 3) for key, value in self.phase_durations.items()},
            "heartbeat": {key: _stats(value) for key, value in self.heartbeats.items()},
            "methods": {key: _stats(value) for key, value in self.method_times.items()},
            "method_counts": dict(self.method_counts),
            "phase_method_counts": {key: dict(value) for key, value in self.phase_method_counts.items()},
            "scrollregion_updates": dict(self.scrollregion_updates),
            "hidden_page_configures": dict(self.hidden_page_configures),
            "page_configure_bind_counts": dict(self.page_configure_bind_counts),
            "page_configure_event_counts": dict(self.page_configure_event_counts),
            "e3_f1_checks": self.e3_f1_checks,
            "e3_f1_persistence_calls": self.e3_f1_persistence_calls,
            "widget_counts": self.widget_counts,
            "startup_widget_counts": self.startup_widget_counts,
            "layout_apply_count": getattr(self.dashboard, "_layout_apply_count", 0),
            "layout_skip_count": getattr(self.dashboard, "_layout_skip_count", 0),
            "layout_debounce_count": getattr(self.dashboard, "_layout_debounce_count", 0),
            "built_pages": sorted(getattr(
                self.dashboard, "built_pages", self.dashboard.page_frames,
            )),
            "page_build_errors": dict(getattr(
                self.dashboard, "_page_build_errors", {},
            )),
            "heavy_page_destroy_count": getattr(
                self.dashboard, "_heavy_page_destroy_count", 0,
            ),
            "refresh_worker_metrics": serialized_worker_metrics,
            "diagnostics_worker_metrics": dict(diagnostics_worker.metrics),
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
    parser.add_argument("--assert-e3-f1", action="store_true")
    parser.add_argument("--language", choices=("zh-CN", "en"))
    parser.add_argument("--geometry", default="1280x800")
    args = parser.parse_args()
    RealDataProfiler(
        args.run_id,
        args.output,
        quiet=args.quiet,
        exercise_e3_f1=args.assert_e3_f1,
        language=args.language,
        geometry=args.geometry,
    ).run()


if __name__ == "__main__":
    main()
