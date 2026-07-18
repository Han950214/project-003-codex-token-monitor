import threading
import time
import unittest

from app.main import Dashboard
from app.refresh_worker import DashboardRefreshWorker, RefreshWorkerResult


class DashboardRefreshWorkerTests(unittest.TestCase):
    def test_execute_runs_off_calling_thread(self):
        caller = threading.get_ident()
        worker = DashboardRefreshWorker(lambda _value: threading.get_ident())
        self.addCleanup(worker.shutdown)
        worker.submit("request")
        self.assertTrue(worker.wait_until_idle(1))
        result = worker.drain_results()[0]
        self.assertNotEqual(result.value, caller)

    def test_slow_execution_is_serial_and_keeps_only_latest_pending_request(self):
        started = threading.Event()
        release = threading.Event()
        executed = []

        def execute(value):
            executed.append(value)
            if value == 1:
                started.set()
                release.wait(2)
            return value * 10

        worker = DashboardRefreshWorker(execute)
        self.addCleanup(worker.shutdown)
        first = worker.submit(1)
        self.assertTrue(started.wait(1))
        generations = [worker.submit(value) for value in range(2, 6)]

        self.assertEqual(first, 1)
        self.assertEqual(generations, [2, 3, 4, 5])
        self.assertEqual(executed, [1])
        release.set()
        self.assertTrue(worker.wait_until_idle(2))

        results = worker.drain_results()
        self.assertEqual(executed, [1, 5])
        self.assertEqual([(item.generation, item.value) for item in results], [(1, 10), (5, 50)])
        self.assertEqual(worker.metrics["submitted"], 5)
        self.assertEqual(worker.metrics["executed"], 2)
        self.assertEqual(worker.metrics["coalesced"], 3)
        self.assertEqual(worker.metrics["max_parallel"], 1)

    def test_worker_error_does_not_prevent_later_refresh(self):
        attempts = []

        def execute(value):
            attempts.append(value)
            if value == "bad":
                raise RuntimeError("simulated")
            return value

        worker = DashboardRefreshWorker(execute)
        self.addCleanup(worker.shutdown)
        worker.submit("bad")
        self.assertTrue(worker.wait_until_idle(1))
        worker.submit("good")
        self.assertTrue(worker.wait_until_idle(1))

        results = worker.drain_results()
        self.assertEqual(attempts, ["bad", "good"])
        self.assertIsInstance(results[0].error, RuntimeError)
        self.assertEqual(results[1].value, "good")
        self.assertEqual(worker.metrics["errors"], 1)

    def test_shutdown_returns_without_waiting_and_suppresses_late_result(self):
        started = threading.Event()
        release = threading.Event()

        def execute(value):
            started.set()
            release.wait(2)
            return value

        worker = DashboardRefreshWorker(execute)
        worker.submit("late")
        self.assertTrue(started.wait(1))
        before = time.perf_counter()
        worker.shutdown()
        self.assertLess(time.perf_counter() - before, 0.2)
        release.set()
        self.assertTrue(worker.wait_until_stopped(2))
        self.assertEqual(worker.drain_results(), ())
        self.assertEqual(worker.metrics["late_results_ignored"], 1)

    def test_dashboard_discards_old_generation_and_applies_latest_only(self):
        class FakeWorker:
            busy = False

            def __init__(self):
                self.discarded = 0

            @staticmethod
            def drain_results():
                return (
                    RefreshWorkerResult(1, value="old"),
                    RefreshWorkerResult(2, value="latest"),
                )

            def mark_discarded(self):
                self.discarded += 1

        dashboard = object.__new__(Dashboard)
        dashboard._closing = False
        dashboard._latest_refresh_generation = 2
        dashboard._refresh_poll_scheduled = True
        dashboard.refresh_worker = FakeWorker()
        applied = []
        dashboard._apply_refresh_payload = applied.append

        Dashboard._poll_refresh_results(dashboard)

        self.assertEqual(applied, ["latest"])
        self.assertEqual(dashboard.refresh_worker.discarded, 1)
        self.assertFalse(dashboard._refresh_poll_scheduled)


if __name__ == "__main__":
    unittest.main()
