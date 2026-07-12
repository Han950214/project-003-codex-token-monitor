import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from app.codex_rollout import CodexSessionUsage, InstructionUsage, RolloutSessionsResult, TokenUsage
from app.codex_state import CodexThreadMetadata
from app.dashboard import DashboardViewModel
from app.ui_presenter import disambiguated_session_labels, present_dashboard


NOW = datetime(2026, 7, 12, 1, 0, tzinfo=timezone.utc)


def session(thread_id, minute, title="Codex Session", status="exact", total=100, cached=20, input_tokens=80, path=None):
    usage = TokenUsage(input_tokens, cached, total - input_tokens, 0, total)
    instruction = InstructionUsage(f"turn-{thread_id}", status, usage, 1, 65000, 0, 0, 0, status == "exact", status == "in_progress")
    observed = NOW + timedelta(minutes=minute)
    return CodexSessionUsage(thread_id, title, "safe timestamp fallback", f"rollout-{thread_id}.jsonl", instruction, usage, observed, NOW, status, path)


class MultiSessionDashboardTests(unittest.TestCase):
    def test_auto_follow_switches_to_latest_activity(self):
        results = [RolloutSessionsResult((session("a", 1), session("b", 0)), "a", 0, NOW), RolloutSessionsResult((session("b", 2), session("a", 1)), "b", 0, NOW)]
        loader = Mock(side_effect=results)
        vm = DashboardViewModel(rollout_sessions_loader=loader, state_batch_loader=lambda _ids: {})
        self.assertEqual(vm.refresh().selected_thread_id, "a")
        self.assertEqual(vm.refresh().selected_thread_id, "b")

    def test_pinned_selection_does_not_jump_when_another_thread_updates(self):
        results = [RolloutSessionsResult((session("a", 1), session("b", 0)), "a", 0, NOW), RolloutSessionsResult((session("b", 5), session("a", 1)), "b", 0, NOW)]
        vm = DashboardViewModel(rollout_sessions_loader=Mock(side_effect=results), state_batch_loader=lambda _ids: {})
        vm.refresh(); vm.pin_thread("a")
        snapshot = vm.refresh()
        self.assertEqual(snapshot.selection_mode, "pinned")
        self.assertEqual(snapshot.selected_thread_id, "a")

    def test_batch_state_loader_is_called_once_and_aligned_to_selected_thread(self):
        state = Mock(return_value={"a": CodexThreadMetadata("a", 1, 2, "gpt", "openai", 100, "Alpha"), "b": CodexThreadMetadata("b", 1, 2, "gpt", "openai", 999, "Beta")})
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((session("a", 1), session("b", 0, total=90)), "a", 0, NOW), state_batch_loader=state)
        snapshot = vm.refresh()
        state.assert_called_once_with(("a", "b"))
        self.assertEqual(snapshot.selected_session.display_title, "Alpha")
        self.assertEqual(snapshot.state_reconciliation, "reconciled")

    def test_missing_title_uses_safe_timestamp_fallback(self):
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((session("a", 1),), "a", 0, NOW), state_batch_loader=lambda _ids: {"a": CodexThreadMetadata("a", 1, 2, "gpt", "openai", 100)})
        selected = vm.refresh().selected_session
        self.assertEqual(selected.title_source, "safe timestamp fallback")
        self.assertTrue(selected.display_title.startswith("Codex Session ·"))

    def test_same_titles_are_disambiguated_without_thread_ids(self):
        rows = present_dashboard(DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((session("thread-secret-a", 1, "Same"), session("thread-secret-b", 1, "Same")), "thread-secret-a", 0, NOW), state_batch_loader=lambda _ids: {}).refresh(), False).recent_sessions
        labels = disambiguated_session_labels(rows, "en")
        self.assertEqual(len(set(labels.values())), 2)
        self.assertTrue(all("thread-secret" not in label for label in labels.values()))

    def test_recent_row_uses_thread_cumulative_total_and_cache_hit(self):
        snapshot = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((session("a", 1, total=200, cached=40, input_tokens=100),), "a", 0, NOW), state_batch_loader=lambda _ids: {}).refresh()
        row = present_dashboard(snapshot, False).recent_sessions[0]
        self.assertEqual(row.thread_total, "200")
        self.assertEqual(row.cache_hit, "40.0%")
        self.assertEqual(present_dashboard(snapshot, False).telemetry_session_total, "200")

    def test_zero_cumulative_input_uses_dash_cache_hit(self):
        item = session("a", 1, total=10, cached=0, input_tokens=0)
        snapshot = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((item,), "a", 0, NOW), state_batch_loader=lambda _ids: {}).refresh()
        self.assertEqual(present_dashboard(snapshot, False).recent_sessions[0].cache_hit, "—")

    def test_pinned_unavailable_keeps_identifier_without_switching(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "gone.jsonl"
            first = session("a", 1, path=missing)
            loader = Mock(side_effect=[RolloutSessionsResult((first,), "a", 0, NOW), RolloutSessionsResult((session("b", 5),), "b", 0, NOW)])
            vm = DashboardViewModel(rollout_sessions_loader=loader, state_batch_loader=lambda _ids: {})
            vm.refresh(); vm.pin_thread("a")
            snapshot = vm.refresh()
        self.assertEqual(snapshot.selected_thread_id, "a")
        self.assertEqual(snapshot.selected_session.status, "unavailable")

    def test_pinned_thread_outside_recent_candidates_uses_known_path(self):
        with tempfile.TemporaryDirectory() as directory:
            known_path = Path(directory) / "known.jsonl"
            known_path.write_text("", encoding="utf-8")
            first = session("a", 1, path=known_path)
            reader = Mock()
            reader.read_session.return_value = first
            loader = Mock(side_effect=[RolloutSessionsResult((first,), "a", 0, NOW), RolloutSessionsResult((session("b", 5),), "b", 0, NOW)])
            vm = DashboardViewModel(rollout_sessions_loader=loader, state_batch_loader=lambda _ids: {}, rollout_reader=reader)
            vm.refresh(); vm.pin_thread("a")
            snapshot = vm.refresh()
        reader.read_session.assert_called_once_with(known_path)
        self.assertEqual(snapshot.selected_thread_id, "a")
        self.assertNotEqual(snapshot.selected_session.status, "unavailable")

    def test_incomplete_session_cannot_be_newly_pinned(self):
        item = session("incomplete", 1, status="incomplete")
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((item,), item.thread_id, 0, NOW), state_batch_loader=lambda _ids: {})
        vm.refresh()
        self.assertFalse(vm.pin_thread(item.thread_id))
        self.assertEqual(vm.selection_mode, "auto")

    def test_time_range_defaults_to_seven_days_and_accepts_larger_ranges(self):
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((), None, 0, NOW), state_batch_loader=lambda _ids: {})
        self.assertEqual(vm.lookback_days, 7)
        self.assertTrue(vm.set_lookback_days(30))
        self.assertEqual(vm.refresh().lookback_days, 30)
        self.assertTrue(vm.set_lookback_days(90))
        self.assertFalse(vm.set_lookback_days(365))

    def test_stale_unfinished_session_is_not_shown_as_running(self):
        stale = session("stale", -60, status="in_progress")
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((stale,), stale.thread_id, 1, NOW), state_batch_loader=lambda _ids: {})
        snapshot = vm.refresh()
        self.assertEqual(snapshot.selected_session.status, "incomplete")
        self.assertFalse(vm.pin_thread(stale.thread_id))

    def test_recent_unfinished_session_remains_running(self):
        active = session("active", -5, status="in_progress")
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((active,), active.thread_id, 1, NOW), state_batch_loader=lambda _ids: {})
        self.assertEqual(vm.refresh().selected_session.status, "in_progress")


if __name__ == "__main__":
    unittest.main()
