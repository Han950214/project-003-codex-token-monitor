import unittest
from datetime import datetime, timezone

from app.codex_logs import CodexLogsResult, CodexResponseUsage, LogsAdapterStatus
from app.codex_state import CodexThreadTotal
from app.dashboard import DashboardSnapshot
from app.metrics import PricingConfig, RunUsage, summarize_runs
from app.ui_presenter import DataStatus, UiTone, format_auto_refresh, present_dashboard
from tests.test_storage import sample_run


NOW = datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc)
PRICING = PricingConfig(1, 0.1, 2)


def snapshot(status, usage=None, runs=None, state_total=None):
    runs = list(runs or [])
    logs = CodexLogsResult(
        usage=usage,
        source="codex_logs_sqlite / real usage" if usage else "unknown",
        status=status,
        observed_at=NOW if usage else None,
        refreshed_at=NOW,
    )
    latest = None
    if usage is not None:
        latest = RunUsage(
            usage.input_tokens,
            usage.output_tokens,
            max(usage.total_tokens - usage.input_tokens - usage.output_tokens, 0),
            observed_cached_input_tokens=usage.cached_tokens,
        )
    summary = summarize_runs(
        runs,
        PRICING,
        state_total.total_tokens if state_total else None,
        latest,
    )
    return DashboardSnapshot(runs, summary, logs, state_total)


class UiPresenterTests(unittest.TestCase):
    def test_real_usage_is_fresh_and_cache_hit_is_derived(self):
        usage = CodexResponseUsage(100, 20, 125, 25, 5)
        view = present_dashboard(snapshot(LogsAdapterStatus.CONNECTED, usage), False)

        self.assertEqual(view.data_status, DataStatus.FRESH_REAL)
        self.assertEqual(view.latest_usage[2].value, "125")
        self.assertEqual(view.latest_usage[5].value, "25.0%")
        self.assertIn("Derived from real usage", view.latest_usage[5].detail)
        self.assertIn("not an official rate", view.latest_usage[5].detail)

    def test_saved_run_without_real_usage_is_local_estimate(self):
        run = sample_run()
        view = present_dashboard(snapshot(LogsAdapterStatus.NO_RESPONSE_COMPLETED, runs=[run]), False)

        self.assertEqual(view.data_status, DataStatus.LOCAL_ESTIMATE)
        self.assertEqual(view.latest_usage[2].value, str(run.total_tokens))
        self.assertEqual(view.latest_usage[4].value, "—")
        self.assertEqual(view.manual_runs[0].ended_at, run.ended_at)
        self.assertEqual(view.manual_runs[0].values()[0:3], (run.title, run.model, run.mode))

    def test_no_response_completed_is_no_data_and_never_zero(self):
        view = present_dashboard(snapshot(LogsAdapterStatus.NO_RESPONSE_COMPLETED), False)

        self.assertEqual(view.data_status, DataStatus.NO_DATA)
        self.assertTrue(all(metric.value == "—" for metric in view.latest_usage))
        self.assertEqual(view.telemetry_current_total, "—")
        self.assertIn("No response usage is available yet.", view.status_message)
        self.assertIn("Use Manual Refresh", view.status_message)

    def test_adapter_failures_are_logs_error(self):
        for status in (
            LogsAdapterStatus.DATABASE_MISSING,
            LogsAdapterStatus.OPEN_FAILED,
            LogsAdapterStatus.PARSE_FAILED,
        ):
            with self.subTest(status=status):
                view = present_dashboard(snapshot(status), False)
                self.assertEqual(view.data_status, DataStatus.LOGS_ERROR)
                self.assertEqual(view.status_tone, UiTone.ERROR)

    def test_missing_state_total_is_unknown_not_state_error(self):
        usage = CodexResponseUsage(10, 2, 12, 4, 1)
        view = present_dashboard(snapshot(LogsAdapterStatus.CONNECTED, usage), False)

        state = next(item for item in view.source_details if item.label == "State Adapter")
        self.assertEqual(state.value, "No state total available")
        self.assertNotEqual(view.data_status, DataStatus.STATE_ERROR)
        self.assertEqual(view.telemetry_session_total, "—")

    def test_real_state_total_is_used_for_session(self):
        state = CodexThreadTotal("thread", None, None, None, None, 999)
        view = present_dashboard(snapshot(LogsAdapterStatus.NO_RESPONSE_COMPLETED, state_total=state), False)
        self.assertEqual(view.telemetry_session_total, "999")
        source = next(item for item in view.source_details if item.label == "Session Source")
        self.assertEqual(source.value, "codex_state_sqlite / real total")

    def test_refreshing_retains_previous_values(self):
        usage = CodexResponseUsage(100, 20, 120, 25, 5)
        snap = snapshot(LogsAdapterStatus.CONNECTED, usage)
        previous = present_dashboard(snap, False)
        refreshing = present_dashboard(snap, True, refreshing=True, previous=previous)

        self.assertEqual(refreshing.data_status, DataStatus.REFRESHING)
        self.assertEqual(refreshing.latest_usage, previous.latest_usage)
        self.assertEqual(refreshing.manual_runs, previous.manual_runs)
        self.assertEqual(refreshing.auto_refresh, "Auto Refresh: On (60s)")
        self.assertIn("Previous values remain visible", refreshing.status_message)

    def test_auto_refresh_copy_is_fixed_to_sixty_seconds(self):
        self.assertEqual(format_auto_refresh(True), "Auto Refresh: On (60s)")
        self.assertEqual(format_auto_refresh(False), "Auto Refresh: Off (60s)")


if __name__ == "__main__":
    unittest.main()
