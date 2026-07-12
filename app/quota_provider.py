"""Read Codex quota numbers through the installed official app-server command."""

from __future__ import annotations

import json
import os
import queue
import re
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
        self._messages: queue.Queue[tuple[int, str]] = queue.Queue()
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

    def refresh_thread_titles(self) -> dict[str, str]:
        """Return one safe, structured title batch over the shared connection."""
        with self._lock:
            try:
                self._ensure_started()
                result = self._request(
                    "thread/list",
                    {"limit": 500, "archived": False, "sortKey": "updated_at",
                     "sortDirection": "desc", "sourceKinds": [
                         "cli", "vscode", "exec", "appServer", "subAgent",
                         "subAgentReview", "subAgentCompact", "subAgentThreadSpawn",
                         "subAgentOther", "unknown",
                     ], "useStateDbOnly": False},
                    parser=_parse_thread_titles_response,
                )
                return result if isinstance(result, dict) else {}
            except Exception:
                self._stop()
                return {}

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if self.executable is None or not self.executable.is_file():
            raise FileNotFoundError("codex_cli_missing")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        child_env = os.environ.copy()
        child_env.pop("CODEX_THREAD_ID", None)
        child_env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
        self._process = subprocess.Popen(
            [str(self.executable), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=creationflags,
            env=child_env,
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
                "capabilities": {"experimentalApi": True},
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
            match = re.search(r'"id"\s*:\s*(\d+)', line)
            if match:
                self._messages.put((int(match.group(1)), line))

    def _request(self, method: str, params: object, *, parser=json.loads) -> object:
        self._request_id += 1
        request_id = self._request_id
        self._write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            remaining = max(0.01, deadline - time.monotonic())
            try:
                message_id, raw = self._messages.get(timeout=min(0.25, remaining))
            except queue.Empty:
                if self._process is None or self._process.poll() is not None:
                    raise RuntimeError("app_server_stopped")
                continue
            if message_id != request_id:
                continue
            message = parser(raw)
            if not isinstance(message, Mapping):
                raise RuntimeError("app_server_response_error")
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


class _JsonCursor:
    """Small selective JSON reader; skipped values are never decoded."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.pos = 0

    def ws(self) -> None:
        while self.pos < len(self.raw) and self.raw[self.pos].isspace():
            self.pos += 1

    def string(self, decode: bool = True) -> str:
        self.ws()
        start = self.pos
        if self.raw[self.pos] != '"':
            raise ValueError("json_string_expected")
        self.pos += 1
        escaped = False
        while self.pos < len(self.raw):
            char = self.raw[self.pos]
            self.pos += 1
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                token = self.raw[start:self.pos]
                return json.loads(token) if decode else ""
        raise ValueError("json_string_unterminated")

    def skip(self) -> None:
        self.ws()
        char = self.raw[self.pos]
        if char == '"':
            self.string(False)
            return
        if char in "[{":
            stack = ["]" if char == "[" else "}"]
            self.pos += 1
            in_string = escaped = False
            while self.pos < len(self.raw) and stack:
                char = self.raw[self.pos]
                self.pos += 1
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                elif char == '"':
                    in_string = True
                elif char == "{":
                    stack.append("}")
                elif char == "[":
                    stack.append("]")
                elif char == stack[-1]:
                    stack.pop()
            return
        while self.pos < len(self.raw) and self.raw[self.pos] not in ",}]":
            self.pos += 1

    def expect(self, char: str) -> None:
        self.ws()
        if self.pos >= len(self.raw) or self.raw[self.pos] != char:
            raise ValueError("json_shape_invalid")
        self.pos += 1


def _parse_thread_titles_response(raw: str) -> dict[str, object]:
    cursor = _JsonCursor(raw)
    titles: dict[str, str] = {}

    def thread() -> None:
        cursor.expect("{")
        thread_id = name = None
        while True:
            cursor.ws()
            if cursor.raw[cursor.pos] == "}":
                cursor.pos += 1
                break
            key = cursor.string()
            cursor.expect(":")
            if key in {"id", "name"} and cursor.raw[cursor.pos:].lstrip().startswith('"'):
                cursor.ws()
                value = cursor.string()
                if key == "id": thread_id = value
                else: name = value
            else:
                cursor.skip()
            cursor.ws()
            if cursor.raw[cursor.pos] == ",": cursor.pos += 1
        safe = _safe_thread_title(name)
        if thread_id and safe:
            titles[thread_id] = safe

    def data_array() -> None:
        cursor.expect("[")
        while True:
            cursor.ws()
            if cursor.raw[cursor.pos] == "]": cursor.pos += 1; return
            thread()
            cursor.ws()
            if cursor.raw[cursor.pos] == ",": cursor.pos += 1

    def object_with(target: str, handler) -> None:
        cursor.expect("{")
        while True:
            cursor.ws()
            if cursor.raw[cursor.pos] == "}": cursor.pos += 1; return
            key = cursor.string(); cursor.expect(":")
            handler() if key == target else cursor.skip()
            cursor.ws()
            if cursor.raw[cursor.pos] == ",": cursor.pos += 1

    object_with("result", lambda: object_with("data", data_array))
    return {"result": titles}


def _safe_thread_title(value: object, limit: int = 72) -> str | None:
    if not isinstance(value, str):
        return None
    title = " ".join(value.split())
    lowered = title.lower()
    rejected = (
        not title or lowered.startswith("/goal") or
        "referenced pasted text files" in lowered or
        lowered.startswith("the following is the codex agent history") or
        "c:\\users\\" in lowered or "file:" in lowered
    )
    if rejected:
        return None
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"
