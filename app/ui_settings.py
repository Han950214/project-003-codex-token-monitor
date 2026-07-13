"""Independent local persistence for Dashboard-only preferences."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Callable

from app.dashboard_mode import normalize_dashboard_mode, normalize_widget_mode
from app.i18n import DEFAULT_LANGUAGE, normalize_language
from app.paths import ui_settings_path


DEFAULT_UI_SETTINGS_PATH = ui_settings_path()
STARTUP_MODES = {"dashboard", "widget", "tray"}
DEFAULT_WIDGET_IDLE_OPACITY = 0.82
MIN_WIDGET_IDLE_OPACITY = 0.30
MAX_WIDGET_IDLE_OPACITY = 0.95
EXIT_BEHAVIORS = {"ask", "minimize", "exit"}


def _load_payload(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_payload(payload: dict[str, object], path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        return False
    return True


def load_language(path: Path | None = None) -> str:
    path = path or ui_settings_path()
    payload = _load_payload(path)
    return normalize_language(payload.get("language"))


def save_language(language: str, path: Path | None = None) -> bool:
    path = path or ui_settings_path()
    language = normalize_language(language)
    payload = _load_payload(path)
    payload["language"] = language
    return _save_payload(payload, path)


def load_startup_mode(path: Path | None = None) -> str:
    value = _load_payload(path or ui_settings_path()).get("startup_mode")
    return value if value in STARTUP_MODES else "dashboard"


def save_startup_mode(mode: str, path: Path | None = None) -> bool:
    if mode not in STARTUP_MODES:
        mode = "dashboard"
    path = path or ui_settings_path()
    payload = _load_payload(path)
    payload["startup_mode"] = mode
    return _save_payload(payload, path)


def load_dashboard_mode(path: Path | None = None) -> str:
    return normalize_dashboard_mode(_load_payload(path or ui_settings_path()).get("dashboard_mode"))


def save_dashboard_mode(mode: str, path: Path | None = None) -> bool:
    path = path or ui_settings_path()
    payload = _load_payload(path)
    payload["dashboard_mode"] = normalize_dashboard_mode(mode)
    return _save_payload(payload, path)


def load_widget_mode(path: Path | None = None) -> str:
    return normalize_widget_mode(_load_payload(path or ui_settings_path()).get("widget_mode"))


def save_widget_mode(mode: str, path: Path | None = None) -> bool:
    path = path or ui_settings_path()
    payload = _load_payload(path)
    payload["widget_mode"] = normalize_widget_mode(mode)
    return _save_payload(payload, path)


def load_auto_refresh_enabled(path: Path | None = None) -> bool:
    value = _load_payload(path or ui_settings_path()).get("auto_refresh_enabled")
    return value if isinstance(value, bool) else False


def save_auto_refresh_enabled(enabled: object, path: Path | None = None) -> bool:
    path = path or ui_settings_path()
    payload = _load_payload(path)
    payload["auto_refresh_enabled"] = bool(enabled)
    return _save_payload(payload, path)


def load_exit_behavior(path: Path | None = None) -> str:
    value = _load_payload(path or ui_settings_path()).get("exit_behavior")
    return value if value in EXIT_BEHAVIORS else "ask"


def save_exit_behavior(value: str, path: Path | None = None) -> bool:
    path = path or ui_settings_path()
    payload = _load_payload(path)
    payload["exit_behavior"] = value if value in EXIT_BEHAVIORS else "ask"
    return _save_payload(payload, path)


def validate_ui_settings(path: Path | None = None) -> tuple[bool, str]:
    path = path or ui_settings_path()
    if not path.exists():
        return True, "defaults"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False, "invalid_json"
    if not isinstance(payload, dict):
        return False, "invalid_root"
    validators = {
        "language": lambda value: value in {"zh-CN", "en"},
        "startup_mode": lambda value: value in STARTUP_MODES,
        "dashboard_mode": lambda value: value in {"simple", "advanced"},
        "widget_mode": lambda value: value in {"compact", "expanded"},
        "auto_refresh_enabled": lambda value: isinstance(value, bool),
        "exit_behavior": lambda value: value in EXIT_BEHAVIORS,
        "widget_idle_opacity": lambda value: normalize_widget_idle_opacity(value) == value,
    }
    for key, validator in validators.items():
        if key in payload and not validator(payload[key]):
            return False, f"invalid_{key}"
    return True, "valid"


def clear_widget_position(path: Path | None = None) -> bool:
    path = path or ui_settings_path()
    payload = _load_payload(path)
    payload.pop("widget_position", None)
    return _save_payload(payload, path)




def normalize_widget_idle_opacity(value: object) -> float:
    if isinstance(value, bool):
        return DEFAULT_WIDGET_IDLE_OPACITY
    try:
        opacity = float(value)
    except (TypeError, ValueError):
        return DEFAULT_WIDGET_IDLE_OPACITY
    if not math.isfinite(opacity):
        return DEFAULT_WIDGET_IDLE_OPACITY
    return min(MAX_WIDGET_IDLE_OPACITY, max(MIN_WIDGET_IDLE_OPACITY, opacity))


def load_widget_idle_opacity(path: Path | None = None) -> float:
    value = _load_payload(path or ui_settings_path()).get("widget_idle_opacity")
    return normalize_widget_idle_opacity(value)


def save_widget_idle_opacity(value: object, path: Path | None = None) -> bool:
    path = path or ui_settings_path()
    payload = _load_payload(path)
    payload["widget_idle_opacity"] = normalize_widget_idle_opacity(value)
    return _save_payload(payload, path)


def load_widget_position(path: Path | None = None) -> tuple[int, int] | None:
    path = path or ui_settings_path()
    value = _load_payload(path).get("widget_position")
    if not isinstance(value, dict):
        return None
    x, y = value.get("x"), value.get("y")
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    return (x, y) if isinstance(x, int) and isinstance(y, int) else None


def save_widget_position(x: int, y: int, path: Path | None = None) -> bool:
    path = path or ui_settings_path()
    payload = _load_payload(path)
    payload["widget_position"] = {"x": int(x), "y": int(y)}
    return _save_payload(payload, path)


def load_exit_action_for_today(
    path: Path | None = None,
    *,
    today: date | None = None,
) -> str | None:
    path = path or ui_settings_path()
    value = _load_payload(path).get("exit_prompt")
    if not isinstance(value, dict) or value.get("date") != (today or date.today()).isoformat():
        return None
    action = value.get("action")
    return action if action in {"minimize", "exit"} else None


def save_exit_action_for_today(
    action: str,
    path: Path | None = None,
    *,
    today: date | None = None,
) -> bool:
    if action not in {"minimize", "exit"}:
        return False
    path = path or ui_settings_path()
    payload = _load_payload(path)
    payload["exit_prompt"] = {
        "date": (today or date.today()).isoformat(),
        "action": action,
    }
    return _save_payload(payload, path)


class LanguageController:
    """Updates and persists language without knowing about data loaders."""

    def __init__(
        self,
        on_change: Callable[[str], None],
        path: Path | None = None,
    ) -> None:
        self.path = path or ui_settings_path()
        self.on_change = on_change
        self.language = load_language(self.path)

    def set_language(self, language: str) -> str:
        self.language = normalize_language(language)
        save_language(self.language, self.path)
        self.on_change(self.language)
        return self.language
