from __future__ import annotations

import inspect
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.diagnostics import DIAGNOSTIC_CHECK_CODES
from app.diagnostics_worker import (
    DashboardDiagnosticsWorker,
    DiagnosticsRequest,
    run_diagnostics_request,
)


class DashboardDiagnosticsWorkerTests(unittest.TestCase):
    def test_request_uses_and_closes_its_own_quota_provider(self):
        closed = []
        executable_finder_calls = []

        class Provider:
            def refresh(self):
                return SimpleNamespace(source_status="normal")

            def close(self):
                closed.append(True)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            report = run_diagnostics_request(
                DiagnosticsRequest(
                    version="0.1.0",
                    runtime_mode="dashboard",
                    frozen=False,
                    session_count=2,
                    rollout_root=root,
                    state_path=root / "missing.sqlite",
                    settings_path=root / "missing.json",
                    startup_executable=Path("monitor.exe"),
                    tray_started=True,
                    refreshed_at=datetime.now(timezone.utc),
                ),
                provider_factory=lambda executable: (
                    executable_finder_calls.append(executable) or Provider()
                ),
                executable_finder=lambda: Path("codex.exe"),
                startup_adapter_factory=lambda: SimpleNamespace(
                    path_status=lambda _path: "unused",
                ),
            )

        self.assertEqual(
            tuple(item.code for item in report.results),
            DIAGNOSTIC_CHECK_CODES,
        )
        self.assertEqual(executable_finder_calls, [Path("codex.exe")])
        self.assertEqual(closed, [True])

    def test_diagnostics_runs_once_off_calling_thread_and_ignores_repeated_clicks(self):
        caller = threading.get_ident()
        started = threading.Event()
        release = threading.Event()
        executions: list[tuple[str, int]] = []

        def execute(value: str) -> tuple[str, int]:
            started.set()
            release.wait(1)
            item = (value, threading.get_ident())
            executions.append(item)
            return item

        worker = DashboardDiagnosticsWorker(execute)
        self.addCleanup(worker.shutdown)
        generation = worker.submit("diagnose")
        self.assertTrue(started.wait(1))
        self.assertEqual(
            [worker.submit("diagnose") for _ in range(4)],
            [generation] * 4,
        )
        release.set()
        self.assertTrue(worker.wait_until_idle(1))

        result = worker.drain_results()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].generation, generation)
        self.assertNotEqual(result[0].value[1], caller)
        self.assertEqual(executions, [("diagnose", result[0].value[1])])
        self.assertEqual(worker.metrics["submitted"], 5)
        self.assertEqual(worker.metrics["executed"], 1)
        self.assertEqual(worker.metrics["ignored"], 4)
        self.assertEqual(worker.metrics["max_parallel"], 1)

    def test_error_does_not_prevent_later_diagnostics(self):
        attempts: list[str] = []

        def execute(value: str) -> str:
            attempts.append(value)
            if value == "bad":
                raise RuntimeError("simulated")
            return value

        worker = DashboardDiagnosticsWorker(execute)
        self.addCleanup(worker.shutdown)
        worker.submit("bad")
        self.assertTrue(worker.wait_until_idle(1))
        worker.submit("good")
        self.assertTrue(worker.wait_until_idle(1))

        results = worker.drain_results()
        self.assertEqual(attempts, ["bad", "good"])
        self.assertIsInstance(results[0].error, RuntimeError)
        self.assertEqual(results[1].value, "good")
        self.assertEqual(worker.metrics["errors"], 1)

    def test_shutdown_is_nonblocking_and_suppresses_late_result(self):
        started = threading.Event()
        release = threading.Event()

        def execute(_value: str) -> str:
            started.set()
            release.wait(1)
            return "late"

        worker = DashboardDiagnosticsWorker(execute)
        worker.submit("diagnose")
        self.assertTrue(started.wait(1))
        before = time.perf_counter()
        worker.shutdown()
        self.assertLess(time.perf_counter() - before, 0.2)
        release.set()
        self.assertTrue(worker.wait_until_stopped(1))
        self.assertEqual(worker.drain_results(), ())
        self.assertEqual(worker.metrics["late_results_ignored"], 1)

    def test_worker_module_has_no_tk_access(self):
        source = inspect.getsource(__import__("app.diagnostics_worker", fromlist=["*"]))
        for forbidden in (
            "StringVar",
            ".configure(",
            "messagebox",
            "root.after(",
            "tkinter",
            "customtkinter",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn("DiagnosticsWorkerResult", source)


if __name__ == "__main__":
    unittest.main()
