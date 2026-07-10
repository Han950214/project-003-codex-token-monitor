import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from app.codex_logs import CodexLogsResult, CodexResponseUsage, LogsAdapterStatus
from app.codex_state import CodexThreadTotal
from app.dashboard import DashboardViewModel
from app.metrics import PricingConfig
from app.storage import LoadResult
from tests.test_storage import sample_run


class DashboardViewModelTests(unittest.TestCase):
    def test_refresh_rereads_sources_and_does_not_add_recent_runs(self):
        refreshed_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
        results = iter(
            [
                CodexLogsResult(
                    CodexResponseUsage(100, 20, 120, 25, 5),
                    "codex_logs_sqlite / real usage",
                    LogsAdapterStatus.CONNECTED,
                    None,
                    refreshed_at,
                ),
                CodexLogsResult(
                    CodexResponseUsage(200, 40, 240, 50, 10),
                    "codex_logs_sqlite / real usage",
                    LogsAdapterStatus.CONNECTED,
                    None,
                    refreshed_at,
                ),
            ]
        )
        runs_loader = Mock(return_value=LoadResult([sample_run()]))
        logs_loader = Mock(side_effect=lambda: next(results))
        state_loader = Mock(
            return_value=CodexThreadTotal("thread", None, None, None, None, 999)
        )
        view_model = DashboardViewModel(
            PricingConfig(1, 0.1, 2),
            Path("unused.json"),
            runs_loader=runs_loader,
            logs_loader=logs_loader,
            state_loader=state_loader,
        )

        first = view_model.refresh()
        second = view_model.refresh()

        self.assertEqual(first.summary.current_run_tokens, 120)
        self.assertEqual(second.summary.current_run_tokens, 240)
        self.assertEqual(second.summary.session_tokens, 999)
        self.assertEqual(second.summary.total_tokens_source, "codex_state_sqlite")
        self.assertEqual([run.run_id for run in second.runs], ["run-1"])
        self.assertEqual(runs_loader.call_count, 2)
        self.assertEqual(logs_loader.call_count, 2)
        self.assertEqual(state_loader.call_count, 2)


if __name__ == "__main__":
    unittest.main()
