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
        rollout_loader = Mock(return_value=RolloutUsageResult("rollout.jsonl", "thread", instruction, True))
        state_loader = Mock(return_value=CodexThreadTotal("thread", None, None, None, None, 999))
        view_model = DashboardViewModel(PricingConfig(1, .1, 2), Path("unused"), runs_loader=Mock(return_value=LoadResult([sample_run()])), rollout_loader=rollout_loader, state_loader=state_loader)
        snapshot = view_model.refresh()
        self.assertTrue(snapshot.state_reconciled)
        state_loader.assert_called_once_with("thread")
        self.assertEqual(snapshot.rollout.instruction.usage.total_tokens, 120)

    def test_missing_rollout_does_not_load_global_state_or_manual_usage(self):
        state_loader = Mock()
        view_model = DashboardViewModel(PricingConfig(1, .1, 2), Path("unused"), runs_loader=Mock(return_value=LoadResult([sample_run()])), rollout_loader=lambda: RolloutUsageResult(None, None, None, False), state_loader=state_loader)
        snapshot = view_model.refresh()
        self.assertIsNone(snapshot.state_total)
        state_loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
