"""Single-worker, latest-request refresh execution for the desktop UI."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from queue import SimpleQueue
from typing import Callable, Generic, TypeVar


RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class RefreshWorkerResult(Generic[ResultT]):
    generation: int
    value: ResultT | None = None
    error: Exception | None = None


class DashboardRefreshWorker(Generic[RequestT, ResultT]):
    """Own one daemon thread and at most one latest pending request."""

    def __init__(
        self,
        execute: Callable[[RequestT], ResultT],
        *,
        cleanup: Callable[[], None] | None = None,
    ) -> None:
        self._execute = execute
        self._cleanup = cleanup
        self._condition = threading.Condition()
        self._pending: tuple[int, RequestT] | None = None
        self._active = False
        self._closed = False
        self._stopped = threading.Event()
        self._results: SimpleQueue[RefreshWorkerResult[ResultT]] = SimpleQueue()
        self._generation = 0
        self._metrics = {
            "submitted": 0,
            "executed": 0,
            "coalesced": 0,
            "errors": 0,
            "max_parallel": 0,
            "late_results_ignored": 0,
            "discarded_results": 0,
        }
        self._thread = threading.Thread(
            target=self._run,
            name="dashboard-refresh-worker",
            daemon=True,
        )
        self._thread.start()

    def submit(self, request: RequestT) -> int:
        with self._condition:
            if self._closed:
                return self._generation
            self._generation += 1
            generation = self._generation
            self._metrics["submitted"] += 1
            if self._pending is not None:
                self._metrics["coalesced"] += 1
            self._pending = (generation, request)
            self._condition.notify()
            return generation

    def drain_results(self) -> tuple[RefreshWorkerResult[ResultT], ...]:
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
                    self._metrics["max_parallel"] = max(
                        self._metrics["max_parallel"], 1,
                    )
                try:
                    result = RefreshWorkerResult(
                        generation,
                        value=self._execute(request),
                    )
                except Exception as error:  # Keep later refreshes operational.
                    with self._condition:
                        self._metrics["errors"] += 1
                    result = RefreshWorkerResult(generation, error=error)
                with self._condition:
                    self._active = False
                    if self._closed:
                        self._metrics["late_results_ignored"] += 1
                    else:
                        self._results.put(result)
                    self._condition.notify_all()
        finally:
            if self._cleanup is not None:
                try:
                    self._cleanup()
                except Exception:
                    pass
            self._stopped.set()
