import unittest
from unittest.mock import Mock

from app.auto_refresh import AutoRefreshController


class FakeScheduler:
    def __init__(self):
        self.callbacks = {}
        self.cancelled = []
        self.next_id = 1

    def after(self, delay, callback):
        callback_id = self.next_id
        self.next_id += 1
        self.callbacks[callback_id] = (delay, callback)
        return callback_id

    def cancel(self, callback_id):
        self.cancelled.append(callback_id)
        self.callbacks.pop(callback_id, None)

    def fire(self, callback_id):
        _, callback = self.callbacks.pop(callback_id)
        callback()


class AutoRefreshControllerTests(unittest.TestCase):
    def test_enable_schedules_and_disable_cancels(self):
        scheduler = FakeScheduler()
        controller = AutoRefreshController(scheduler.after, scheduler.cancel, Mock())

        self.assertFalse(controller.enabled)
        controller.set_enabled(True)
        pending = controller.pending_id
        self.assertEqual(scheduler.callbacks[pending][0], 60_000)
        controller.set_enabled(False)

        self.assertIn(pending, scheduler.cancelled)
        self.assertIsNone(controller.pending_id)

    def test_scheduled_failure_still_schedules_next_refresh(self):
        scheduler = FakeScheduler()
        errors = []
        controller = AutoRefreshController(
            scheduler.after,
            scheduler.cancel,
            Mock(side_effect=RuntimeError("safe failure")),
            on_error=errors.append,
        )
        controller.set_enabled(True)
        first = controller.pending_id

        scheduler.fire(first)

        self.assertEqual(len(errors), 1)
        self.assertIsNotNone(controller.pending_id)
        self.assertNotEqual(controller.pending_id, first)

    def test_manual_refresh_resets_schedule_and_does_not_overlap(self):
        scheduler = FakeScheduler()
        calls = []
        controller = None

        def refresh():
            calls.append("refresh")
            controller.manual_refresh()

        controller = AutoRefreshController(scheduler.after, scheduler.cancel, refresh)
        controller.set_enabled(True)
        old_pending = controller.pending_id

        controller.manual_refresh()

        self.assertEqual(calls, ["refresh"])
        self.assertIn(old_pending, scheduler.cancelled)
        self.assertEqual(len(scheduler.callbacks), 1)

    def test_close_cancels_pending_callback(self):
        scheduler = FakeScheduler()
        controller = AutoRefreshController(scheduler.after, scheduler.cancel, Mock())
        controller.set_enabled(True)
        pending = controller.pending_id

        controller.close()

        self.assertIn(pending, scheduler.cancelled)
        self.assertTrue(controller.closed)

    def test_auto_refresh_only_invokes_refresh_callback(self):
        scheduler = FakeScheduler()
        refresh = Mock()
        save_run = Mock()
        export_report = Mock()
        controller = AutoRefreshController(scheduler.after, scheduler.cancel, refresh)
        controller.set_enabled(True)

        scheduler.fire(controller.pending_id)

        refresh.assert_called_once_with()
        save_run.assert_not_called()
        export_report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
