"""Windows named-mutex single-instance guard."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = r"Local\CodexTokenMonitor.SingleInstance"


def _load_kernel32():
    """Load the Win64 API with pointer-safe ctypes signatures."""
    if sys.platform != "win32":
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


class SingleInstanceGuard:
    def __init__(self, kernel32=None) -> None:
        self._native_kernel32 = kernel32 is None
        self.kernel32 = _load_kernel32() if self._native_kernel32 else kernel32
        self.handle = None

    def acquire(self) -> bool:
        if self.kernel32 is None:
            return True
        self.handle = self.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not self.handle:
            return False
        last_error = ctypes.get_last_error() if self._native_kernel32 else self.kernel32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            self.release()
            return False
        return True

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if not handle or self.kernel32 is None:
            return
        try:
            self.kernel32.CloseHandle(handle)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.release()
