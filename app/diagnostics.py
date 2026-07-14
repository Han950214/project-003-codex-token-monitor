"""Privacy-safe, read-only diagnostics for the Token Monitor runtime."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.codex_state import SAFE_BASE_COLUMNS


DIAGNOSTIC_STATUSES = {"normal", "warning", "failure", "unused"}
DIAGNOSTIC_CHECK_CODES = (
    "app_version",
    "runtime_mode",
    "frozen_build",
    "codex_executable",
    "app_server",
    "quota_interface",
    "rollout_root",
    "safe_numeric_data",
    "sqlite_adapter",
    "ui_settings",
    "startup_path",
    "tray_controller",
    "data_freshness",
)
DIAGNOSTIC_STALE_AFTER = timedelta(minutes=3)


@dataclass(frozen=True)
class DiagnosticResult:
    code: str
    status: str
    detail_key: str

    def __post_init__(self) -> None:
        if self.code not in DIAGNOSTIC_CHECK_CODES:
            raise ValueError(f"unknown_diagnostic:{self.code}")
        if self.status not in DIAGNOSTIC_STATUSES:
            raise ValueError(f"unknown_diagnostic_status:{self.status}")


@dataclass(frozen=True)
class DiagnosticReport:
    results: tuple[DiagnosticResult, ...]
    observed_at: datetime

    @property
    def problem_count(self) -> int:
        return sum(item.status in {"warning", "failure"} for item in self.results)


@dataclass(frozen=True)
class DiagnosticContext:
    version: str
    runtime_mode: str
    frozen: bool
    codex_executable_found: bool
    quota_probe: Callable[[], str]
    rollout_root: Path
    rollout_probe: Callable[[], int]
    state_path: Path
    settings_path: Path
    startup_status: Callable[[], str]
    tray_started: bool
    refreshed_at: datetime | None


def run_diagnostics(
    context: DiagnosticContext,
    *,
    now: datetime | None = None,
) -> DiagnosticReport:
    """Run every check independently; no result includes paths, content, or secrets."""

    now = now or datetime.now(timezone.utc)
    results = [
        DiagnosticResult("app_version", "normal", "diagnostic_version_ok"),
        DiagnosticResult("runtime_mode", "normal", "diagnostic_runtime_mode_ok"),
        DiagnosticResult(
            "frozen_build", "normal" if context.frozen else "unused",
            "diagnostic_frozen_yes" if context.frozen else "diagnostic_frozen_no",
        ),
        DiagnosticResult(
            "codex_executable", "normal" if context.codex_executable_found else "failure",
            "diagnostic_codex_found" if context.codex_executable_found else "diagnostic_codex_missing",
        ),
    ]

    quota_status: str | None = None
    try:
        quota_status = context.quota_probe()
        results.append(DiagnosticResult("app_server", "normal", "diagnostic_app_server_ok"))
    except Exception:
        results.append(DiagnosticResult("app_server", "failure", "diagnostic_app_server_failed"))
    if quota_status is None:
        results.append(DiagnosticResult("quota_interface", "failure", "diagnostic_quota_failed"))
    elif quota_status == "normal":
        results.append(DiagnosticResult("quota_interface", "normal", "diagnostic_quota_ok"))
    else:
        results.append(DiagnosticResult("quota_interface", "warning", "diagnostic_quota_limited"))

    root_ok = context.rollout_root.is_dir()
    results.append(DiagnosticResult(
        "rollout_root", "normal" if root_ok else "failure",
        "diagnostic_rollout_root_ok" if root_ok else "diagnostic_rollout_root_failed",
    ))
    try:
        session_count = context.rollout_probe()
        results.append(DiagnosticResult(
            "safe_numeric_data", "normal" if session_count > 0 else "warning",
            "diagnostic_numeric_ok" if session_count > 0 else "diagnostic_numeric_empty",
        ))
    except Exception:
        results.append(DiagnosticResult("safe_numeric_data", "failure", "diagnostic_numeric_failed"))

    sqlite_status = _safe_check(lambda: inspect_sqlite_adapter(context.state_path))
    results.append(DiagnosticResult(
        "sqlite_adapter", sqlite_status,
        f"diagnostic_sqlite_{sqlite_status}",
    ))
    settings_status = _safe_check(lambda: inspect_settings_file(context.settings_path))
    results.append(DiagnosticResult(
        "ui_settings", settings_status,
        f"diagnostic_settings_{settings_status}",
    ))
    startup_status = _safe_check(context.startup_status)
    results.append(DiagnosticResult(
        "startup_path", startup_status,
        f"diagnostic_startup_{startup_status}",
    ))
    results.append(DiagnosticResult(
        "tray_controller", "normal" if context.tray_started else "warning",
        "diagnostic_tray_normal" if context.tray_started else "diagnostic_tray_warning",
    ))
    if context.refreshed_at is None:
        freshness_status, freshness_key = "warning", "diagnostic_freshness_unknown"
    elif now - context.refreshed_at > DIAGNOSTIC_STALE_AFTER:
        freshness_status, freshness_key = "warning", "diagnostic_freshness_stale"
    else:
        freshness_status, freshness_key = "normal", "diagnostic_freshness_normal"
    results.append(DiagnosticResult("data_freshness", freshness_status, freshness_key))
    return DiagnosticReport(tuple(results), now)


def inspect_sqlite_adapter(path: Path) -> str:
    if not path.is_file():
        return "unused"
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(threads)")}
    except (OSError, sqlite3.Error):
        return "failure"
    return "normal" if set(SAFE_BASE_COLUMNS).issubset(columns) else "failure"


def inspect_settings_file(path: Path) -> str:
    if not path.exists():
        return "unused"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "failure"
    if not isinstance(payload, dict):
        return "failure"
    modes = {
        "widget_mode": {"compact", "expanded"},
        "startup_mode": {"dashboard", "widget", "tray"},
        "exit_behavior": {"ask", "minimize", "exit"},
    }
    return "normal" if all(key not in payload or payload[key] in values for key, values in modes.items()) else "failure"


def _safe_check(check: Callable[[], str]) -> str:
    try:
        status = check()
    except Exception:
        return "failure"
    return status if status in DIAGNOSTIC_STATUSES else "failure"
