"""Windows named-mutex single-instance guard."""

from __future__ import annotations

import ctypes
import sys


ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = r"Local\CodexTokenMonitor.SingleInstance"


class SingleInstanceGuard:
    def __init__(self, kernel32=None) -> None:
        self.kernel32 = kernel32 or (ctypes.windll.kernel32 if sys.platform == "win32" else None)
        self.handle = None

    def acquire(self) -> bool:
        if self.kernel32 is None:
            return True
        self.handle = self.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not self.handle:
            return False
        if self.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            self.release()
            return False
        return True

    def release(self) -> None:
        if self.handle and self.kernel32 is not None:
            self.kernel32.CloseHandle(self.handle)
        self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.release()
