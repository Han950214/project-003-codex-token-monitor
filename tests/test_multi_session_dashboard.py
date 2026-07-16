import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from app.codex_rollout import (
    CodexSessionUsage, InstructionUsage, RolloutSessionsResult, TokenUsage,
    make_response_safe_id,
)
from app.codex_state import CodexThreadMetadata
from app.dashboard import DashboardViewModel
from app.ui_presenter import disambiguated_session_labels, present_dashboard


NOW = datetime(2026, 7, 12, 1, 0, tzinfo=timezone.utc)


def session(thread_id, minute, title="Codex Session", status="exact", total=100, cached=20, input_tokens=80, path=None, instruction_total=None, cumulative_total=None):
    instruction_total = total if instruction_total is None else instruction_total
    cumulative_total = total if cumulative_total is None else cumulative_total
    instruction_usage = TokenUsage(input_tokens, cached, instruction_total - input_tokens, 0, instruction_total)
    usage = TokenUsage(input_tokens, cached, cumulative_total - input_tokens, 0, cumulative_total)
    instruction = InstructionUsage(f"turn-{thread_id}", status, instruction_usage, 1, 65000, 0, 0, 0, status == "exact", status == "in_progress")
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
        state = Mock(return_value={"a": CodexThreadMetadata("a", 1, 2, "gpt", "openai", 100), "b": CodexThreadMetadata("b", 1, 2, "gpt", "openai", 999)})
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((session("a", 1), session("b", 0, total=90)), "a", 0, NOW), state_batch_loader=state, title_batch_loader=lambda: {"a": "Alpha", "b": "Beta"})
        snapshot = vm.refresh()
        state.assert_called_once_with(("a", "b"))
        self.assertEqual(snapshot.selected_session.display_title, "Alpha")
        self.assertEqual(snapshot.state_reconciliation, "reconciled")

    def test_cached_selection_performs_no_loader_calls(self):
        rollout = Mock(return_value=RolloutSessionsResult((session("a", 1), session("b", 0)), "a", 0, NOW))
        state = Mock(return_value={})
        titles = Mock(return_value={"a": "Alpha", "b": "Beta"})
        vm = DashboardViewModel(rollout_sessions_loader=rollout, state_batch_loader=state, title_batch_loader=titles)
        vm.refresh()
        for _ in range(10):
            self.assertEqual(vm.select_cached_thread("b").selected_thread_id, "b")
            self.assertEqual(vm.select_cached_thread("a").selected_thread_id, "a")
        self.assertEqual(rollout.call_count, 1)
        self.assertEqual(state.call_count, 1)
        self.assertEqual(titles.call_count, 1)

    def test_missing_title_uses_safe_timestamp_fallback(self):
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((session("a", 1),), "a", 0, NOW), state_batch_loader=lambda _ids: {"a": CodexThreadMetadata("a", 1, 2, "gpt", "openai", 100)})
        selected = vm.refresh().selected_session
        self.assertEqual(selected.title_source, "safe timestamp fallback")
        self.assertTrue(selected.display_title.startswith("Codex Session ·"))

    def test_long_official_name_is_truncated_only_for_display(self):
        name = "A" * 90
        vm = DashboardViewModel(
            rollout_sessions_loader=lambda: RolloutSessionsResult((session("a", 1),), "a", 0, NOW),
            state_batch_loader=lambda _ids: {},
            title_batch_loader=lambda: {"a": name},
        )
        selected = vm.refresh().selected_session
        self.assertEqual(selected.display_title, "A" * 71 + "…")
        self.assertEqual(vm._title_cache["a"], name)

    def test_cached_title_survives_transient_title_batch_failure(self):
        title_loader = Mock(side_effect=[{"a": "Official Name"}, RuntimeError("temporary")])
        vm = DashboardViewModel(
            rollout_sessions_loader=lambda: RolloutSessionsResult((session("a", 1),), "a", 0, NOW),
            state_batch_loader=lambda _ids: {},
            title_batch_loader=title_loader,
        )
        self.assertEqual(vm.refresh().selected_session.display_title, "Official Name")
        refreshed = vm.refresh()
        self.assertEqual(refreshed.selected_session.display_title, "Official Name")
        self.assertEqual(vm.refresh_thread("a").title, "Official Name")

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

    def test_incomplete_session_can_be_newly_pinned(self):
        item = session("incomplete", 1, status="incomplete")
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((item,), item.thread_id, 0, NOW), state_batch_loader=lambda _ids: {})
        vm.refresh()
        self.assertTrue(vm.pin_thread(item.thread_id))
        self.assertEqual(vm.selection_mode, "pinned")

    def test_unavailable_session_cannot_be_newly_pinned(self):
        item = session("unavailable", 1, status="unavailable")
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((item,), item.thread_id, 0, NOW), state_batch_loader=lambda _ids: {})
        vm.refresh()
        self.assertFalse(vm.pin_thread(item.thread_id))

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
        self.assertTrue(vm.pin_thread(stale.thread_id))

    def test_recent_unfinished_session_remains_running(self):
        active = session("active", -5, status="in_progress")
        vm = DashboardViewModel(rollout_sessions_loader=lambda: RolloutSessionsResult((active,), active.thread_id, 1, NOW), state_batch_loader=lambda _ids: {})
        self.assertEqual(vm.refresh().selected_session.status, "in_progress")

    def test_mini_thread_refresh_uses_known_path_and_keeps_token_scopes_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            known_path = Path(directory) / "known.jsonl"
            known_path.write_text("", encoding="utf-8")
            initial = session("a", 1, total=100, path=known_path)
            updated = session("a", 2, path=known_path, instruction_total=75, cumulative_total=150)
            reader = Mock()
            reader.read_session.return_value = updated
            vm = DashboardViewModel(
                rollout_sessions_loader=lambda: RolloutSessionsResult((initial,), "a", 0, NOW),
                state_batch_loader=lambda _ids: {"a": CodexThreadMetadata("a", 1, 2, "gpt", "openai", 150)},
                title_batch_loader=lambda: {"a": "Pinned"},
                rollout_reader=reader,
            )
            vm.refresh()
            mini = vm.refresh_thread("a")
        self.assertEqual(mini.instruction_total_tokens, 75)
        self.assertEqual(mini.session_total_tokens, 150)
        self.assertEqual(mini.title, "Pinned")
        self.assertEqual(
            mini.response_safe_id, make_response_safe_id("a", "turn-a"),
        )
        self.assertEqual(mini.response_status, "exact")
        reader.read_session.assert_called_once_with(known_path)

    def test_mini_thread_refresh_does_not_change_dashboard_selection(self):
        first = session("a", 1)
        second = session("b", 0)
        vm = DashboardViewModel(
            rollout_sessions_loader=lambda: RolloutSessionsResult((first, second), "a", 0, NOW),
            state_batch_loader=lambda _ids: {},
        )
        vm.refresh()
        vm.pin_thread("a")
        vm.refresh_thread("b")
        self.assertEqual(vm.selected_thread_id, "a")

    def test_mini_thread_without_selection_stays_empty(self):
        vm = DashboardViewModel(
            rollout_sessions_loader=lambda: RolloutSessionsResult((), None, 0, NOW),
            state_batch_loader=lambda _ids: {},
        )
        mini = vm.refresh_thread(None)
        self.assertEqual(mini.status, "no_selection")
        self.assertIsNone(mini.instruction_total_tokens)
        self.assertIsNone(mini.session_total_tokens)

    def test_mini_thread_keeps_cumulative_total_when_instruction_usage_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            known_path = Path(directory) / "known.jsonl"
            known_path.write_text("", encoding="utf-8")
            item = session("a", 1, path=known_path, instruction_total=70, cumulative_total=150)
            item = replace(item, instruction=replace(item.instruction, usage=None))
            reader = Mock()
            reader.read_session.return_value = item
            vm = DashboardViewModel(
                rollout_sessions_loader=lambda: RolloutSessionsResult((item,), "a", 0, NOW),
                state_batch_loader=lambda _ids: {}, rollout_reader=reader,
            )
            vm.refresh()
            mini = vm.refresh_thread("a")
        self.assertIsNone(mini.instruction_total_tokens)
        self.assertEqual(mini.session_total_tokens, 150)

    def test_mini_thread_incomplete_keeps_verified_token_values(self):
        with tempfile.TemporaryDirectory() as directory:
            known_path = Path(directory) / "known.jsonl"
            known_path.write_text("", encoding="utf-8")
            item = session("a", 1, status="incomplete", path=known_path, instruction_total=70, cumulative_total=150)
            reader = Mock()
            reader.read_session.return_value = item
            vm = DashboardViewModel(
                rollout_sessions_loader=lambda: RolloutSessionsResult((item,), "a", 0, NOW),
                state_batch_loader=lambda _ids: {}, rollout_reader=reader,
            )
            vm.refresh()
            mini = vm.refresh_thread("a")
        self.assertEqual(mini.status, "completed_partial")
        self.assertEqual(mini.instruction_total_tokens, 70)
        self.assertEqual(mini.session_total_tokens, 150)

    def test_mini_thread_unavailable_keeps_both_totals_unknown(self):
        vm = DashboardViewModel(
            rollout_sessions_loader=lambda: RolloutSessionsResult((), None, 0, NOW),
            state_batch_loader=lambda _ids: {},
        )
        mini = vm.refresh_thread("missing")
        self.assertEqual(mini.status, "unavailable")
        self.assertIsNone(mini.instruction_total_tokens)
        self.assertIsNone(mini.session_total_tokens)


if __name__ == "__main__":
    unittest.main()
