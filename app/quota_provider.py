"""Read Codex quota numbers through the installed official app-server command."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from app.quota import CodexQuotaSnapshot, QuotaKind, QuotaWindow


SOURCE_LABEL = "codex_app_server"
FIVE_HOUR_MINUTES = 300
WEEKLY_MINUTES = 7 * 24 * 60


class QuotaProvider(Protocol):
    def refresh(self) -> CodexQuotaSnapshot: ...

    def close(self) -> None: ...


class CodexAppServerQuotaProvider:
    """Keeps credentials inside Codex and receives only structured quota fields."""

    def __init__(
        self,
        executable: str | Path | None = None,
        *,
        timeout_seconds: float = 12.0,
    ) -> None:
        self.executable = Path(executable) if executable else find_codex_executable()
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, object]] = queue.Queue()
        self._request_id = 0
        self._last_success: CodexQuotaSnapshot | None = None
        self._lock = threading.Lock()

    def refresh(self) -> CodexQuotaSnapshot:
        observed_at = datetime.now(timezone.utc)
        with self._lock:
            try:
                self._ensure_started()
                result = self._request("account/rateLimits/read", None)
                observed_at = datetime.now(timezone.utc)
                snapshot = snapshot_from_app_server(result, observed_at)
                if snapshot.five_hour.available and snapshot.weekly.available:
                    self._last_success = snapshot
                return snapshot
            except Exception as exc:
                error_code = _safe_error_code(exc)
                self._stop()
                if self._last_success is not None:
                    return self._last_success.as_stale(observed_at, error_code)
                return CodexQuotaSnapshot.unavailable(
                    observed_at=observed_at,
                    source=SOURCE_LABEL,
                    error_code=error_code,
                )

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if self.executable is None or not self.executable.is_file():
            raise FileNotFoundError("codex_cli_missing")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            [str(self.executable), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=creationflags,
        )
        self._messages = queue.Queue()
        threading.Thread(target=self._read_messages, daemon=True).start()
        response = self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex-token-monitor",
                    "title": "Codex Token Monitor",
                    "version": "0.1.0",
                },
                "capabilities": None,
            },
        )
        if not isinstance(response, Mapping):
            raise RuntimeError("app_server_initialize_invalid")
        self._notify("initialized", {})

    def _read_messages(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict):
                self._messages.put(message)

    def _request(self, method: str, params: object) -> object:
        self._request_id += 1
        request_id = self._request_id
        self._write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                message = self._messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self._process is None or self._process.poll() is not None:
                    raise RuntimeError("app_server_stopped")
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError("app_server_response_error")
            return message.get("result")
        raise TimeoutError("app_server_timeout")

    def _notify(self, method: str, params: object) -> None:
        self._write({"method": method, "params": params})

    def _write(self, message: Mapping[str, object]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError("app_server_unavailable")
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _stop(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def find_codex_executable(
    *,
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    env = os.environ if environ is None else environ
    override = env.get("CODEX_QUOTA_CLI")
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    local = env.get("LOCALAPPDATA")
    if local:
        bin_root = Path(local) / "OpenAI" / "Codex" / "bin"
        try:
            candidates = sorted(
                bin_root.glob("*/codex.exe"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            candidates = []
        if candidates:
            return candidates[0]
    command = shutil.which("codex")
    return Path(command) if command else None


def snapshot_from_app_server(result: object, observed_at: datetime) -> CodexQuotaSnapshot:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at_timezone_required")
    payload = result if isinstance(result, Mapping) else {}
    rate_limits = payload.get("rateLimits")
    buckets = payload.get("rateLimitsByLimitId")
    if isinstance(buckets, Mapping) and isinstance(buckets.get("codex"), Mapping):
        rate_limits = buckets["codex"]
    snapshot = rate_limits if isinstance(rate_limits, Mapping) else {}
    by_duration: dict[int, Mapping[str, object]] = {}
    for name in ("primary", "secondary"):
        value = snapshot.get(name)
        if not isinstance(value, Mapping):
            continue
        duration = _integer(value.get("windowDurationMins"))
        if duration is not None:
            by_duration[duration] = value
    five_hour = _window_from_payload(
        QuotaKind.FIVE_HOUR,
        by_duration.get(FIVE_HOUR_MINUTES),
        observed_at,
    )
    weekly = _window_from_payload(
        QuotaKind.WEEKLY,
        by_duration.get(WEEKLY_MINUTES),
        observed_at,
    )
    if five_hour.stale or weekly.stale:
        status = "stale"
    elif any(window.error_code == "percentage_mismatch" for window in (five_hour, weekly)):
        status = "invalid"
    elif five_hour.available and weekly.available:
        status = "normal"
    else:
        status = "unavailable"
    return CodexQuotaSnapshot(five_hour, weekly, observed_at, status)


def _window_from_payload(
    kind: QuotaKind,
    payload: Mapping[str, object] | None,
    observed_at: datetime,
) -> QuotaWindow:
    if payload is None:
        return QuotaWindow.unavailable(kind, observed_at, SOURCE_LABEL, "window_unavailable")
    reset_seconds = _integer(payload.get("resetsAt"))
    try:
        reset_at = datetime.fromtimestamp(reset_seconds, timezone.utc) if reset_seconds is not None else None
    except (OSError, OverflowError, ValueError):
        reset_at = None
    try:
        return QuotaWindow(
            kind,
            _number(payload.get("usedPercent")),
            _number(payload.get("remainingPercent")),
            reset_at,
            observed_at,
            SOURCE_LABEL,
            True,
        )
    except (TypeError, ValueError):
        return QuotaWindow.unavailable(kind, observed_at, SOURCE_LABEL, "quota_invalid")


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("quota_number_invalid")
    return float(value)


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "app_server_timeout"
    if isinstance(error, FileNotFoundError):
        return "codex_cli_missing"
    message = str(error)
    allowed = {
        "app_server_initialize_invalid",
        "app_server_response_error",
        "app_server_stopped",
        "app_server_unavailable",
    }
    return message if message in allowed else "quota_refresh_failed"
