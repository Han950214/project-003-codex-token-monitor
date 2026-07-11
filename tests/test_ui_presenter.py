import unittest

from app.codex_rollout import InstructionUsage, RolloutUsageResult, TokenUsage
from app.codex_state import CodexThreadTotal
from app.dashboard import DashboardSnapshot
from app.metrics import PricingConfig, summarize_runs
from app.ui_presenter import DataStatus, present_dashboard


def snapshot(instruction=None, state=None):
    return DashboardSnapshot([], summarize_runs([], PricingConfig(1, .1, 2), state.total_tokens if state else None), RolloutUsageResult("rollout.jsonl" if instruction else None, "thread-12345678" if instruction else None, instruction, instruction is not None), state, state is not None)


class UiPresenterTests(unittest.TestCase):
    def test_exact_instruction_drives_all_six_cards(self):
        instruction = InstructionUsage("turn", "exact", TokenUsage(100, 25, 20, 5, 120), 2, 1500, 1, 0, 0, True, False)
        view = present_dashboard(snapshot(instruction, CodexThreadTotal("thread-12345678", None, None, None, None, 999)), False)
        self.assertEqual(view.data_status, DataStatus.FRESH_REAL)
        self.assertEqual([item.value for item in view.latest_usage[:5]], ["100", "20", "120", "25", "5"])
        self.assertEqual(view.latest_usage[5].value, "25.0%")
        self.assertEqual(view.telemetry_current_total, "120")
        self.assertEqual(view.telemetry_session_total, "999")
        self.assertEqual(tuple(item.label for item in view.source_details), ("Rollout File", "Thread", "Instruction Status", "Model Calls", "Instruction Elapsed", "State/Rollout"))

    def test_unavailable_rollout_uses_dashes_without_manual_fallback(self):
        view = present_dashboard(snapshot(), False)
        self.assertEqual(view.data_status, DataStatus.NO_DATA)
        self.assertTrue(all(item.value == "—" for item in view.latest_usage))
        self.assertEqual(view.telemetry_current_total, "—")

    def test_in_progress_is_marked_and_can_show_verified_increment(self):
        instruction = InstructionUsage("turn", "in_progress", TokenUsage(3, 1, 2, 1, 5), 1, None, 0, 0, 0, False, True)
        view = present_dashboard(snapshot(instruction), True)
        self.assertIn("in progress", view.status_message)
        self.assertIn("still growing", view.latest_usage[0].detail)


if __name__ == "__main__":
    unittest.main()
