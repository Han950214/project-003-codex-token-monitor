import inspect
import unittest

from app.main import main
from app.single_instance import ERROR_ALREADY_EXISTS, MUTEX_NAME, SingleInstanceGuard


class FakeKernel32:
    def __init__(self, error=0, handle=41):
        self.error, self.handle = error, handle
        self.created = []
        self.closed = []

    def CreateMutexW(self, security, owned, name):
        self.created.append((security, owned, name))
        return self.handle

    def GetLastError(self):
        return self.error

    def CloseHandle(self, handle):
        self.closed.append(handle)


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

    def test_second_instance_check_precedes_window_creation(self):
        source = inspect.getsource(main)
        self.assertLess(source.index("instance.acquire()"), source.index("build_dashboard()"))

    def test_single_instance_does_not_scan_or_terminate_processes(self):
        source = inspect.getsource(SingleInstanceGuard)
        for forbidden in ("psutil", "TerminateProcess", "OpenProcess", "process_iter"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
