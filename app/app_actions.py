"""Small, explicit Windows actions used by the product UI."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.paths import writable_root
from app.quota_provider import find_codex_executable


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    error_code: str | None = None


def open_codex() -> ActionResult:
    executable = find_codex_executable()
    if executable is None:
        return ActionResult(False, "codex_not_found")
    try:
        subprocess.Popen([str(executable)], shell=False, close_fds=True)
    except OSError:
        return ActionResult(False, "codex_open_failed")
    return ActionResult(True)


def open_data_directory(path: Path | None = None) -> ActionResult:
    directory = (path or writable_root()).resolve()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(directory)  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return ActionResult(False, "data_directory_open_failed")
    return ActionResult(True)
