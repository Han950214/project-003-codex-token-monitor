import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.codex_rollout import CodexSessionUsage, InstructionUsage, RolloutUsageResult, TokenUsage
from app.codex_state import CodexThreadTotal
from app.dashboard import DashboardSnapshot
from app.metrics import PricingConfig, summarize_runs
from app.ui_presenter import DataStatus, present_dashboard


def snapshot(instruction=None, state=None, observed=None, refreshed=None, reconciliation="unavailable"):
    cumulative = TokenUsage(900, 200, 99, 10, 999) if instruction else None
    rollout = RolloutUsageResult("rollout.jsonl" if instruction else None, "thread-12345678" if instruction else None, instruction, instruction is not None, cumulative, observed, refreshed or datetime(2026, 7, 11, 13, tzinfo=timezone.utc))
    return DashboardSnapshot([], summarize_runs([], PricingConfig(1, .1, 2), state.total_tokens if state else None), rollout, state, reconciliation == "reconciled", reconciliation)


class UiPresenterTests(unittest.TestCase):
    def test_exact_instruction_drives_all_six_cards(self):
        instruction = InstructionUsage("turn", "exact", TokenUsage(100, 25, 20, 5, 120), 2, 1500, 1, 0, 0, True, False)
        view = present_dashboard(snapshot(instruction, CodexThreadTotal("thread-12345678", None, None, None, None, 999)), False)
        self.assertEqual(view.data_status, DataStatus.FRESH_REAL)
        self.assertEqual([item.value for item in view.latest_usage[:5]], ["100", "20", "120", "25", "5"])
        self.assertEqual(view.latest_usage[5].value, "25.0%")
        self.assertEqual(view.telemetry_current_total, "120")
        self.assertEqual(view.telemetry_session_total, "999")
        self.assertEqual(tuple(item.label for item in view.source_details), ("Data Source", "Current Task", "Model Calls", "Task Elapsed", "Data Sync"))

    def test_unavailable_rollout_uses_dashes_without_manual_fallback(self):
        view = present_dashboard(snapshot(), False)
        self.assertEqual(view.data_status, DataStatus.NO_DATA)
        self.assertTrue(all(item.value == "—" for item in view.latest_usage))
        self.assertEqual(view.telemetry_current_total, "—")

    def test_in_progress_is_marked_and_can_show_verified_increment(self):
        instruction = InstructionUsage("turn", "in_progress", TokenUsage(3, 1, 2, 1, 5), 1, None, 0, 0, 0, False, True)
        view = present_dashboard(snapshot(instruction), True)
        self.assertEqual(view.data_status, DataStatus.RUNNING)
        self.assertEqual(view.status_message, "in_progress")

    def test_unreconciled_in_progress_is_not_fresh_real(self):
        instruction = InstructionUsage("turn", "in_progress", TokenUsage(3, 1, 2, 1, 5), 1, None, 0, 0, 1, False, True)
        view = present_dashboard(snapshot(instruction), True)
        self.assertEqual(view.data_status, DataStatus.INCOMPLETE)
        self.assertNotEqual(view.data_status, DataStatus.COMPLETED)

    def test_event_and_refresh_times_are_separate(self):
        instruction = InstructionUsage("turn", "exact", TokenUsage(3, 1, 2, 1, 5), 1, None, 0, 0, 0, True, False)
        event_time = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
        refresh_time = datetime(2026, 7, 11, 13, tzinfo=timezone.utc)
        view = present_dashboard(snapshot(instruction, observed=event_time, refreshed=refresh_time), False)
        self.assertNotEqual(view.last_event, view.last_refresh)
        self.assertNotEqual(view.last_event, "—")
        self.assertNotEqual(view.last_refresh, "—")

    def test_completed_non_exact_is_completed_partial_with_verified_usage(self):
        instruction = InstructionUsage("turn", "incomplete", TokenUsage(3, 1, 2, 1, 5), 1, 1234, 0, 0, 1, False, False)
        view = present_dashboard(snapshot(instruction), False)
        self.assertEqual(view.data_status, DataStatus.COMPLETED_PARTIAL)
        self.assertEqual(view.status_tone.value, "estimate")
        self.assertNotIn("unavailable", view.status_message.lower())
        self.assertEqual(view.latest_usage[2].value, "5")
        self.assertEqual(next(item for item in view.source_details if item.label == "Task Elapsed").value, "1s")

    def test_completed_partial_keeps_data_sync_independent(self):
        instruction = InstructionUsage("turn", "incomplete", TokenUsage(3, 1, 2, 1, 5), 1, 12000, 0, 0, 1, False, False)
        view = present_dashboard(snapshot(instruction, reconciliation="reconciled"), False)
        self.assertEqual(view.data_status, DataStatus.COMPLETED_PARTIAL)
        self.assertEqual(next(item for item in view.source_details if item.label == "Data Sync").value, "reconciled")

    def test_stale_in_progress_and_unreconciled_in_progress_are_incomplete(self):
        now = datetime(2026, 7, 12, 1, tzinfo=timezone.utc)
        active = InstructionUsage("turn", "in_progress", TokenUsage(3, 1, 2, 1, 5), 1, None, 0, 0, 0, False, True)
        stale_session = CodexSessionUsage("thread", "Session", "safe timestamp fallback", "rollout.jsonl", active, TokenUsage(9, 2, 1, 0, 10), now - timedelta(minutes=11), now, "incomplete")
        stale_snapshot = replace(snapshot(active, observed=stale_session.observed_at, refreshed=now), selected_session=stale_session, recent_sessions=(stale_session,))
        self.assertEqual(present_dashboard(stale_snapshot, False).data_status, DataStatus.INCOMPLETE)
        bad = InstructionUsage("turn", "in_progress", TokenUsage(3, 1, 2, 1, 5), 1, None, 0, 0, 1, False, True)
        self.assertEqual(present_dashboard(snapshot(bad), False).data_status, DataStatus.INCOMPLETE)


if __name__ == "__main__":
    unittest.main()
