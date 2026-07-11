import unittest
from pathlib import Path
from unittest.mock import Mock

from app.codex_rollout import InstructionUsage, RolloutUsageResult, TokenUsage
from app.codex_state import CodexThreadTotal
from app.dashboard import DashboardViewModel
from app.metrics import PricingConfig
from app.storage import LoadResult
from tests.test_storage import sample_run


class DashboardViewModelTests(unittest.TestCase):
    def test_refresh_aligns_state_to_rollout_thread(self):
        instruction = InstructionUsage("turn", "exact", TokenUsage(100, 25, 20, 5, 120), 1, 1000, 0, 0, 0, True, False)
        rollout_loader = Mock(return_value=RolloutUsageResult("rollout.jsonl", "thread", instruction, True, TokenUsage(900, 20, 99, 5, 999)))
        state_loader = Mock(return_value=CodexThreadTotal("thread", None, None, None, None, 999))
        view_model = DashboardViewModel(PricingConfig(1, .1, 2), Path("unused"), runs_loader=Mock(return_value=LoadResult([sample_run()])), rollout_loader=rollout_loader, state_loader=state_loader)
        snapshot = view_model.refresh()
        self.assertTrue(snapshot.state_reconciled)
        self.assertEqual(snapshot.state_reconciliation, "reconciled")
        state_loader.assert_called_once_with("thread")
        self.assertEqual(snapshot.rollout.instruction.usage.total_tokens, 120)

    def test_missing_rollout_does_not_load_global_state_or_manual_usage(self):
        state_loader = Mock()
        view_model = DashboardViewModel(PricingConfig(1, .1, 2), Path("unused"), runs_loader=Mock(return_value=LoadResult([sample_run()])), rollout_loader=lambda: RolloutUsageResult(None, None, None, False), state_loader=state_loader)
        snapshot = view_model.refresh()
        self.assertIsNone(snapshot.state_total)
        state_loader.assert_not_called()

    def test_same_thread_with_different_cumulative_total_is_mismatch(self):
        rollout = RolloutUsageResult("rollout.jsonl", "thread", None, True, TokenUsage(90, 10, 10, 2, 100))
        view_model = DashboardViewModel(PricingConfig(1, .1, 2), Path("unused"), runs_loader=Mock(return_value=LoadResult([])), rollout_loader=lambda: rollout, state_loader=lambda _thread: CodexThreadTotal("thread", None, None, None, None, 101))
        snapshot = view_model.refresh()
        self.assertFalse(snapshot.state_reconciled)
        self.assertEqual(snapshot.state_reconciliation, "mismatch")

    def test_different_thread_or_missing_cumulative_is_unavailable(self):
        for rollout in (
            RolloutUsageResult("rollout.jsonl", "thread", None, True, TokenUsage(90, 10, 10, 2, 100)),
            RolloutUsageResult("rollout.jsonl", "thread", None, True),
        ):
            with self.subTest(rollout=rollout):
                view_model = DashboardViewModel(PricingConfig(1, .1, 2), Path("unused"), runs_loader=Mock(return_value=LoadResult([])), rollout_loader=lambda: rollout, state_loader=lambda _thread: CodexThreadTotal("other", None, None, None, None, 100))
                self.assertEqual(view_model.refresh().state_reconciliation, "unavailable")


if __name__ == "__main__":
    unittest.main()
