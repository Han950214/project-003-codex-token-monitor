"""Testable single-threaded auto-refresh scheduling controller."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


DEFAULT_AUTO_REFRESH_SECONDS = 60


class AutoRefreshController:
    def __init__(
        self,
        schedule: Callable[[int, Callable[[], None]], Any],
        cancel: Callable[[Any], None],
        refresh: Callable[[], None],
        interval_seconds: int = DEFAULT_AUTO_REFRESH_SECONDS,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.schedule = schedule
        self.cancel = cancel
        self.refresh = refresh
        self.interval_seconds = max(int(interval_seconds), 15)
        self.on_error = on_error
        self.enabled = False
        self.refreshing = False
        self.closed = False
        self.pending_id: Any | None = None

    def set_enabled(self, enabled: bool) -> None:
        if self.closed:
            return
        self.enabled = bool(enabled)
        self._cancel_pending()
        if self.enabled:
            self._schedule_next()

    def manual_refresh(self) -> None:
        if self.closed:
            return
        self._cancel_pending()
        self._run_refresh()
        if self.enabled and self.pending_id is None:
            self._schedule_next()

    def close(self) -> None:
        self.enabled = False
        self.closed = True
        self._cancel_pending()

    def _scheduled_refresh(self) -> None:
        self.pending_id = None
        self._run_refresh()
        if self.enabled and not self.closed and self.pending_id is None:
            self._schedule_next()

    def _run_refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        try:
            self.refresh()
        except Exception as exc:  # Keep the Tk event loop and future schedule alive.
            if self.on_error is not None:
                self.on_error(exc)
        finally:
            self.refreshing = False

    def _schedule_next(self) -> None:
        if self.enabled and not self.closed and self.pending_id is None:
            self.pending_id = self.schedule(
                self.interval_seconds * 1000,
                self._scheduled_refresh,
            )

    def _cancel_pending(self) -> None:
        if self.pending_id is None:
            return
        pending_id = self.pending_id
        self.pending_id = None
        try:
            self.cancel(pending_id)
        except Exception:
            pass
