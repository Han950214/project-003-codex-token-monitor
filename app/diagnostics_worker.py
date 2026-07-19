"""Bounded background execution for one-click diagnostics."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import SimpleQueue
from typing import Callable, Generic, TypeVar

from app.diagnostics import DiagnosticContext, DiagnosticReport, run_diagnostics
from app.quota_provider import CodexAppServerQuotaProvider, QuotaProvider, find_codex_executable
from app.windows_startup import WindowsStartupAdapter

RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class DiagnosticsWorkerResult(Generic[ResultT]):
    generation: int
    value: ResultT | None = None
    error: Exception | None = None


@dataclass(frozen=True)
class DiagnosticsRequest:
    """Plain UI snapshot passed to the diagnostic thread."""

    version: str
    runtime_mode: str
    frozen: bool
    session_count: int
    rollout_root: Path
    state_path: Path
    settings_path: Path
    startup_executable: Path
    tray_started: bool
    refreshed_at: datetime | None


def run_diagnostics_request(
    request: DiagnosticsRequest,
    *,
    provider_factory: Callable[[Path | None], QuotaProvider] = CodexAppServerQuotaProvider,
    executable_finder: Callable[[], Path | None] = find_codex_executable,
    startup_adapter_factory: Callable[[], WindowsStartupAdapter] = WindowsStartupAdapter,
) -> DiagnosticReport:
    """Run all blocking diagnostic probes with an owned quota provider."""

    executable = executable_finder()
    provider = provider_factory(executable)
    try:
        context = DiagnosticContext(
            version=request.version,
            runtime_mode=request.runtime_mode,
            frozen=request.frozen,
            codex_executable_found=executable is not None,
            quota_probe=lambda: provider.refresh().source_status,
            rollout_root=request.rollout_root,
            rollout_probe=lambda: request.session_count,
            state_path=request.state_path,
            settings_path=request.settings_path,
            startup_status=lambda: startup_adapter_factory().path_status(
                request.startup_executable,
            ),
            tray_started=request.tray_started,
            refreshed_at=request.refreshed_at,
        )
        return run_diagnostics(context)
    finally:
        provider.close()


class DashboardDiagnosticsWorker(Generic[RequestT, ResultT]):
    """Run one diagnostic at a time and ignore duplicate requests while busy."""

    def __init__(self, execute: Callable[[RequestT], ResultT]) -> None:
        self._execute = execute
        self._condition = threading.Condition()
        self._pending: tuple[int, RequestT] | None = None
        self._active = False
        self._closed = False
        self._stopped = threading.Event()
        self._results: SimpleQueue[DiagnosticsWorkerResult[ResultT]] = SimpleQueue()
        self._generation = 0
        self._metrics = {
            "submitted": 0,
            "executed": 0,
            "ignored": 0,
            "errors": 0,
            "max_parallel": 0,
            "late_results_ignored": 0,
            "discarded_results": 0,
        }
        self._thread = threading.Thread(
            target=self._run,
            name="dashboard-diagnostics-worker",
            daemon=True,
        )
        self._thread.start()

    def submit(self, request: RequestT) -> int:
        with self._condition:
            self._metrics["submitted"] += 1
            if self._closed:
                return self._generation
            if self._active or self._pending is not None:
                self._metrics["ignored"] += 1
                return self._generation
            self._generation += 1
            self._pending = (self._generation, request)
            self._condition.notify()
            return self._generation

    def drain_results(self) -> tuple[DiagnosticsWorkerResult[ResultT], ...]:
        results = []
        while not self._results.empty():
            results.append(self._results.get())
        return tuple(results)

    def mark_discarded(self) -> None:
        with self._condition:
            self._metrics["discarded_results"] += 1

    @property
    def metrics(self) -> dict[str, int]:
        with self._condition:
            return dict(self._metrics)

    @property
    def busy(self) -> bool:
        with self._condition:
            return self._active or self._pending is not None

    def shutdown(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._pending = None
            self._condition.notify_all()

    def wait_until_idle(self, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._active and self._pending is None,
                timeout=timeout,
            )

    def wait_until_stopped(self, timeout: float) -> bool:
        return self._stopped.wait(timeout)

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._closed or self._pending is not None,
                    )
                    if self._closed:
                        return
                    generation, request = self._pending
                    self._pending = None
                    self._active = True
                    self._metrics["executed"] += 1
                    self._metrics["max_parallel"] = 1
                try:
                    result = DiagnosticsWorkerResult(
                        generation=generation,
                        value=self._execute(request),
                    )
                except Exception as error:
                    with self._condition:
                        self._metrics["errors"] += 1
                    result = DiagnosticsWorkerResult(generation=generation, error=error)
                with self._condition:
                    self._active = False
                    if self._closed:
                        self._metrics["late_results_ignored"] += 1
                    else:
                        self._results.put(result)
                    self._condition.notify_all()
        finally:
            self._stopped.set()
