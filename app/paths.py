"""Central runtime paths for source and PyInstaller builds."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping


DATA_DIR_ENV = "CODEX_TOKEN_MONITOR_DATA_DIR"
APP_DIR_NAME = "CodexTokenMonitor"


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_root(*, frozen: bool | None = None, bundle_root: str | Path | None = None) -> Path:
    frozen = is_frozen() if frozen is None else frozen
    if not frozen:
        return repository_root()
    root = bundle_root if bundle_root is not None else getattr(sys, "_MEIPASS", None)
    if root is None:
        raise RuntimeError("PyInstaller bundle resource root is unavailable")
    return Path(root).resolve()


def writable_root(
    *,
    frozen: bool | None = None,
    environ: Mapping[str, str] | None = None,
    local_appdata: str | Path | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    override = env.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    frozen = is_frozen() if frozen is None else frozen
    if not frozen:
        return repository_root()
    base = local_appdata if local_appdata is not None else env.get("LOCALAPPDATA")
    if not base:
        base = Path.home() / "AppData" / "Local"
    return Path(base).expanduser().resolve() / APP_DIR_NAME


def user_data_root(
    *,
    frozen: bool | None = None,
    environ: Mapping[str, str] | None = None,
    local_appdata: str | Path | None = None,
) -> Path:
    """Return the per-user application data root in source and frozen builds."""

    # ``frozen`` is accepted so callers can forward the same path-test kwargs
    # used by the other helpers. History must never default to the repository.
    _ = frozen
    env = os.environ if environ is None else environ
    override = env.get(DATA_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    base = local_appdata if local_appdata is not None else env.get("LOCALAPPDATA")
    if not base:
        base = Path.home() / "AppData" / "Local"
    return Path(base).expanduser().resolve() / APP_DIR_NAME


def pricing_path(**kwargs: object) -> Path:
    return resource_root(**kwargs) / "resources" / "pricing-config.sample.json"


def runs_path(**kwargs: object) -> Path:
    root = writable_root(**kwargs)
    filename = "session-runs.json" if root == repository_root() else "runs.json"
    return root / "data" / filename


def ui_settings_path(**kwargs: object) -> Path:
    return writable_root(**kwargs) / "data" / "ui-settings.json"


def history_db_path(**kwargs: object) -> Path:
    return user_data_root(**kwargs) / "data" / "usage-history.sqlite3"


def reports_dir(**kwargs: object) -> Path:
    return writable_root(**kwargs) / "reports"
