import inspect
import unittest
from ctypes import wintypes
from unittest.mock import patch

from app.main import main
import app.single_instance as single_instance
from app.single_instance import ERROR_ALREADY_EXISTS, MUTEX_NAME, SingleInstanceGuard


class FakeKernel32:
    def __init__(self, error=0, handle=41, close_result=True, close_error=None):
        self.error, self.handle = error, handle
        self.close_result, self.close_error = close_result, close_error
        self.created = []
        self.closed = []

    def CreateMutexW(self, security, owned, name):
        self.created.append((security, owned, name))
        return self.handle

    def GetLastError(self):
        return self.error

    def CloseHandle(self, handle):
        self.closed.append(handle)
        if self.close_error is not None:
            raise self.close_error
        return self.close_result


class FakeFunction:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class NativeKernel32:
    def __init__(self, handle=41):
        self.CreateMutexW = FakeFunction(handle)
        self.CloseHandle = FakeFunction(True)


class SingleInstanceTests(unittest.TestCase):
    def test_first_instance_acquires_project_local_mutex(self):
        kernel = FakeKernel32()
        guard = SingleInstanceGuard(kernel)
        self.assertTrue(guard.acquire())
        self.assertEqual(kernel.created, [(None, False, MUTEX_NAME)])

    def test_second_instance_releases_new_handle_and_exits(self):
        kernel = FakeKernel32(ERROR_ALREADY_EXISTS)
        guard = SingleInstanceGuard(kernel)
        self.assertFalse(guard.acquire())
        self.assertEqual(kernel.closed, [41])
        self.assertIsNone(guard.handle)

    def test_failed_mutex_creation_is_safe(self):
        self.assertFalse(SingleInstanceGuard(FakeKernel32(handle=0)).acquire())

    def test_release_is_idempotent(self):
        kernel = FakeKernel32()
        guard = SingleInstanceGuard(kernel)
        guard.acquire()
        guard.release()
        guard.release()
        self.assertEqual(kernel.closed, [41])

    def test_release_clears_handle_before_closehandle(self):
        class ObservingKernel(FakeKernel32):
            def CloseHandle(inner_self, handle):
                self.assertIsNone(guard.handle)
                return super().CloseHandle(handle)

        kernel = ObservingKernel()
        guard = SingleInstanceGuard(kernel)
        guard.acquire()
        guard.release()
        self.assertIsNone(guard.handle)

    def test_closehandle_false_keeps_handle_cleared(self):
        kernel = FakeKernel32(close_result=False)
        guard = SingleInstanceGuard(kernel)
        guard.acquire()
        guard.release()
        guard.release()
        self.assertIsNone(guard.handle)
        self.assertEqual(kernel.closed, [41])

    def test_closehandle_exception_is_safe_and_not_repeated(self):
        kernel = FakeKernel32(close_error=RuntimeError("close failed"))
        guard = SingleInstanceGuard(kernel)
        guard.acquire()
        guard.release()
        guard.release()
        self.assertIsNone(guard.handle)
        self.assertEqual(kernel.closed, [41])

    def test_large_handle_is_preserved_when_released(self):
        large_handle = 0x123456789ABCDEF
        kernel = FakeKernel32(handle=large_handle)
        guard = SingleInstanceGuard(kernel)
        self.assertTrue(guard.acquire())
        guard.release()
        self.assertEqual(kernel.closed, [large_handle])

    def test_production_loader_declares_win64_signatures(self):
        kernel = NativeKernel32()
        with patch.object(single_instance.sys, "platform", "win32"), patch.object(single_instance.ctypes, "WinDLL", return_value=kernel) as loader:
            self.assertIs(single_instance._load_kernel32(), kernel)
        loader.assert_called_once_with("kernel32", use_last_error=True)
        self.assertEqual(kernel.CreateMutexW.argtypes, (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR))
        self.assertIs(kernel.CreateMutexW.restype, wintypes.HANDLE)
        self.assertEqual(kernel.CloseHandle.argtypes, (wintypes.HANDLE,))
        self.assertIs(kernel.CloseHandle.restype, wintypes.BOOL)

    def test_native_path_uses_ctypes_last_error_without_fake_method(self):
        kernel = NativeKernel32()
        with patch.object(single_instance, "_load_kernel32", return_value=kernel), patch.object(single_instance.ctypes, "get_last_error", return_value=ERROR_ALREADY_EXISTS):
            guard = SingleInstanceGuard()
            self.assertFalse(guard.acquire())
        self.assertEqual(kernel.CloseHandle.calls, [(41,)])

    def test_fake_path_keeps_fake_get_last_error(self):
        kernel = FakeKernel32(ERROR_ALREADY_EXISTS)
        with patch.object(single_instance.ctypes, "get_last_error", side_effect=AssertionError("native path only")):
            self.assertFalse(SingleInstanceGuard(kernel).acquire())
        self.assertEqual(kernel.closed, [41])

    def test_second_instance_check_precedes_window_creation(self):
        source = inspect.getsource(main)
        self.assertLess(source.index("instance.acquire()"), source.index("build_dashboard()"))

    def test_single_instance_does_not_scan_or_terminate_processes(self):
        source = inspect.getsource(SingleInstanceGuard)
        for forbidden in ("psutil", "TerminateProcess", "OpenProcess", "process_iter"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
