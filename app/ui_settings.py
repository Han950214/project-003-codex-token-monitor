"""Independent local persistence for Dashboard-only preferences."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from app.i18n import DEFAULT_LANGUAGE, normalize_language
from app.paths import ui_settings_path


DEFAULT_UI_SETTINGS_PATH = ui_settings_path()


def load_language(path: Path | None = None) -> str:
    path = path or ui_settings_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return DEFAULT_LANGUAGE
    if not isinstance(payload, dict):
        return DEFAULT_LANGUAGE
    return normalize_language(payload.get("language"))


def save_language(language: str, path: Path | None = None) -> bool:
    path = path or ui_settings_path()
    language = normalize_language(language)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"language": language}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        return False
    return True


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
