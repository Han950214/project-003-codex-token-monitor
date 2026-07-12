"""Independent local persistence for Dashboard-only preferences."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Callable

from app.i18n import DEFAULT_LANGUAGE, normalize_language
from app.paths import ui_settings_path


DEFAULT_UI_SETTINGS_PATH = ui_settings_path()


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
