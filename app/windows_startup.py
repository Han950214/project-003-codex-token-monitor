"""User-only Windows startup registry adapter."""

from __future__ import annotations

import os
import sys
from pathlib import Path


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CodexTokenMonitor"


class WindowsStartupAdapter:
    def __init__(self, registry=None, *, frozen: bool | None = None) -> None:
        if registry is None and sys.platform == "win32":
            import winreg

            registry = winreg
        self.registry = registry
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen

    def is_supported(self) -> bool:
        return self.registry is not None and self.frozen

    @staticmethod
    def command_for(executable_path: str | Path) -> str:
        return f'"{Path(executable_path).resolve()}"'

    def is_enabled(self, executable_path: str | Path | None = None) -> bool:
        if not self.is_supported():
            return False
        expected = self.command_for(executable_path or sys.executable)
        try:
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _kind = self.registry.QueryValueEx(key, VALUE_NAME)
        except OSError:
            return False
        return os.path.normcase(str(value)) == os.path.normcase(expected)

    def path_status(self, executable_path: str | Path | None = None) -> str:
        """Return a path-only health code without exposing the registry value."""
        if not self.is_supported():
            return "unused"
        expected = self.command_for(executable_path or sys.executable)
        try:
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _kind = self.registry.QueryValueEx(key, VALUE_NAME)
        except FileNotFoundError:
            return "unused"
        except OSError:
            return "failure"
        raw = str(value)
        if os.path.normcase(raw) != os.path.normcase(expected):
            return "warning"
        target = raw.strip().strip('"')
        return "normal" if Path(target).is_file() else "warning"

    def enable(self, executable_path: str | Path) -> bool:
        if not self.is_supported():
            return False
        try:
            with self.registry.CreateKey(self.registry.HKEY_CURRENT_USER, RUN_KEY) as key:
                self.registry.SetValueEx(key, VALUE_NAME, 0, self.registry.REG_SZ, self.command_for(executable_path))
            return True
        except OSError:
            return False

    def disable(self) -> bool:
        if not self.is_supported():
            return False
        try:
            with self.registry.OpenKey(self.registry.HKEY_CURRENT_USER, RUN_KEY, 0, self.registry.KEY_SET_VALUE) as key:
                self.registry.DeleteValue(key, VALUE_NAME)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False
