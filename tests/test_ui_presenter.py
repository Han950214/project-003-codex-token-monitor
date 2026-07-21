import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.codex_rollout import CodexSessionUsage, InstructionUsage, RolloutUsageResult, TokenUsage
from app.codex_state import CodexThreadTotal
from app.dashboard import DashboardSnapshot
from app.metrics import PricingConfig, summarize_runs
from app.ui_presenter import (
    DataStatus,
    HistoryEmptyState,
    QuotaAvailability,
    UiDataScope,
    build_history_state_view,
    build_quota_state_view,
    build_ui_scope_contract,
    classify_history_empty_state,
    classify_quota_availability,
    disambiguated_session_labels,
    present_dashboard,
    safe_session_primary_label,
)


def snapshot(instruction=None, state=None, observed=None, refreshed=None, reconciliation="unavailable"):
    cumulative = TokenUsage(900, 200, 99, 10, 999) if instruction else None
    rollout = RolloutUsageResult("rollout.jsonl" if instruction else None, "thread-12345678" if instruction else None, instruction, instruction is not None, cumulative, observed, refreshed or datetime(2026, 7, 11, 13, tzinfo=timezone.utc))
    return DashboardSnapshot([], summarize_runs([], PricingConfig(1, .1, 2), state.total_tokens if state else None), rollout, state, reconciliation == "reconciled", reconciliation)


class UiPresenterTests(unittest.TestCase):
    @staticmethod
    def _fallback_view():
        instruction = InstructionUsage("turn", "incomplete", None, 0, 12000, 0, 0, 1, False, False)
        return present_dashboard(snapshot(instruction), False)

    def test_exact_instruction_drives_all_six_cards(self):
        instruction = InstructionUsage("turn", "exact", TokenUsage(100, 25, 20, 5, 120), 2, 1500, 1, 0, 0, True, False)
        view = present_dashboard(snapshot(instruction, CodexThreadTotal("thread-12345678", None, None, None, None, 999)), False)
        self.assertEqual(view.data_status, DataStatus.FRESH_REAL)
        self.assertEqual([item.value for item in view.latest_usage[:5]], ["100", "20", "120", "25", "5"])
        self.assertEqual(view.latest_usage[5].value, "25.0%")
        self.assertEqual(view.telemetry_current_total, "120")
        self.assertEqual(view.telemetry_session_total, "999")
        self.assertEqual([item.label for item in view.latest_usage], ["Input", "Output", "Total", "Cached", "Reasoning", "Cache Hit"])
        self.assertIn("including Reasoning", view.latest_usage[1].detail)
        self.assertEqual(view.usage_scope, "instruction")
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

    def test_missing_instruction_increment_falls_back_to_thread_cumulative_usage(self):
        instruction = InstructionUsage("turn", "incomplete", None, 0, 12000, 0, 0, 1, False, False)
        view = present_dashboard(snapshot(instruction), False)
        self.assertEqual([item.value for item in view.latest_usage[:5]], ["900", "99", "999", "200", "10"])
        self.assertEqual(view.latest_usage[5].value, "22.2%")
        self.assertEqual(view.usage_scope, "thread_cumulative")
        self.assertEqual(view.telemetry_current_total, "999")
        self.assertEqual(view.telemetry_cache_hit, "22.2% derived")
        self.assertEqual(view.telemetry_session_total, "999")
        self.assertEqual([item.label for item in view.latest_usage], ["Input", "Output", "Total", "Cached", "Reasoning", "Cache Hit"])
        self.assertEqual(next(item for item in view.source_details if item.label == "Model Calls").value, "—")
        self.assertTrue(all(item.tone.value == "stale" for item in view.latest_usage))
        self.assertTrue(all(item.detail == "Thread cumulative usage; latest instruction unavailable" for item in view.latest_usage))

    def test_stale_in_progress_and_unreconciled_in_progress_are_incomplete(self):
        now = datetime(2026, 7, 12, 1, tzinfo=timezone.utc)
        active = InstructionUsage("turn", "in_progress", TokenUsage(3, 1, 2, 1, 5), 1, None, 0, 0, 0, False, True)
        stale_session = CodexSessionUsage("thread", "Session", "safe timestamp fallback", "rollout.jsonl", active, TokenUsage(9, 2, 1, 0, 10), now - timedelta(minutes=11), now, "incomplete")
        stale_snapshot = replace(snapshot(active, observed=stale_session.observed_at, refreshed=now), selected_session=stale_session, recent_sessions=(stale_session,))
        self.assertEqual(present_dashboard(stale_snapshot, False).data_status, DataStatus.INCOMPLETE)
        bad = InstructionUsage("turn", "in_progress", TokenUsage(3, 1, 2, 1, 5), 1, None, 0, 0, 1, False, True)
        self.assertEqual(present_dashboard(snapshot(bad), False).data_status, DataStatus.INCOMPLETE)

    def test_fallback_scope_is_explicit(self):
        self.assertEqual(self._fallback_view().usage_scope, "thread_cumulative")

    def test_fallback_cards_keep_all_cumulative_values(self):
        view = self._fallback_view()
        self.assertEqual([metric.value for metric in view.latest_usage], ["900", "99", "999", "200", "10", "22.2%"])

    def test_fallback_card_details_repeat_cumulative_boundary(self):
        view = self._fallback_view()
        self.assertTrue(all(metric.detail == "Thread cumulative usage; latest instruction unavailable" for metric in view.latest_usage))

    def test_fallback_does_not_overwrite_current_telemetry(self):
        view = self._fallback_view()
        self.assertEqual(view.telemetry_current_total, "999")

    def test_fallback_does_not_overwrite_current_cache_telemetry(self):
        view = self._fallback_view()
        self.assertEqual(view.telemetry_cache_hit, "22.2% derived")

    def test_fallback_keeps_session_total(self):
        self.assertEqual(self._fallback_view().telemetry_session_total, "999")

    def test_zero_verified_calls_is_a_dash(self):
        view = self._fallback_view()
        self.assertEqual(next(item for item in view.source_details if item.label == "Model Calls").value, "—")

    def test_instruction_scope_is_not_cumulative(self):
        instruction = InstructionUsage("turn", "exact", TokenUsage(1, 0, 1, 0, 2), 1, 1000, 0, 0, 0, True, False)
        self.assertEqual(present_dashboard(snapshot(instruction), False).usage_scope, "instruction")

    def test_completed_partial_fallback_uses_estimate_tone(self):
        view = self._fallback_view()
        self.assertEqual(view.status_tone.value, "estimate")

    def test_no_usage_has_unavailable_scope(self):
        self.assertEqual(present_dashboard(snapshot(), False).usage_scope, "unavailable")


class UiScopeContractTests(unittest.TestCase):
    @staticmethod
    def _session(thread_id: str, status: str, *, in_progress: bool) -> CodexSessionUsage:
        observed = datetime(2026, 7, 18, 8, tzinfo=timezone.utc)
        instruction = InstructionUsage(
            "turn", status, TokenUsage(3, 1, 2, 1, 5), 1, None,
            0, 0, 0, not in_progress, in_progress,
        )
        return CodexSessionUsage(
            thread_id, f"Session {thread_id}", "safe timestamp fallback",
            "rollout.jsonl", instruction, TokenUsage(9, 2, 1, 0, 10),
            observed, observed, status, turn_count=2,
        )

    def test_only_explicit_running_session_uses_current_activity_scope(self):
        running = self._session("running", "in_progress", in_progress=True)
        historical = self._session("historical", "exact", in_progress=False)
        item = replace(
            snapshot(), current_session=running, current_thread_id=running.thread_id,
            selected_session=historical, selected_thread_id=historical.thread_id,
            recent_sessions=(running, historical), selection_mode="pinned",
        )

        contract = build_ui_scope_contract(item)

        self.assertEqual(contract.activity_scope, UiDataScope.CURRENT_ACTIVITY)
        self.assertEqual(contract.activity_thread_id, "running")
        self.assertEqual(contract.selected_scope, UiDataScope.SELECTED_SESSION)
        self.assertEqual(contract.selected_thread_id, "historical")
        self.assertFalse(contract.selected_is_activity)
        self.assertEqual(contract.global_scope, UiDataScope.GLOBAL_SUMMARY)
        self.assertEqual(contract.live_quota_scope, UiDataScope.LIVE_QUOTA)
        self.assertEqual(
            contract.local_quota_history_scope, UiDataScope.LOCAL_QUOTA_HISTORY,
        )

    def test_terminal_or_unreconciled_activity_is_recent_not_running(self):
        terminal = self._session("recent", "exact", in_progress=False)
        stale_running = replace(
            self._session("stale", "in_progress", in_progress=True),
            status="incomplete",
        )
        for candidate in (terminal, stale_running):
            with self.subTest(thread_id=candidate.thread_id):
                item = replace(
                    snapshot(), current_session=candidate,
                    current_thread_id=candidate.thread_id,
                    selected_session=candidate,
                    selected_thread_id=candidate.thread_id,
                    recent_sessions=(candidate,), selection_mode="pinned",
                )
                contract = build_ui_scope_contract(item)
                self.assertEqual(
                    contract.activity_scope, UiDataScope.RECENT_ACTIVITY,
                )
                self.assertTrue(contract.selected_is_activity)

    def test_explicit_running_recent_session_wins_over_newer_completed_fallback(self):
        completed = self._session("completed", "exact", in_progress=False)
        running = self._session("running", "in_progress", in_progress=True)
        item = replace(
            snapshot(), current_session=completed,
            current_thread_id=completed.thread_id,
            selected_session=completed, selected_thread_id=completed.thread_id,
            recent_sessions=(completed, running),
        )

        contract = build_ui_scope_contract(item)

        self.assertEqual(contract.activity_scope, UiDataScope.CURRENT_ACTIVITY)
        self.assertEqual(contract.activity_thread_id, "running")
        self.assertFalse(contract.selected_is_activity)

    def test_auto_follow_does_not_expose_a_selected_viewing_scope(self):
        recent = self._session("recent", "exact", in_progress=False)
        item = replace(
            snapshot(), current_session=recent, current_thread_id=recent.thread_id,
            selected_session=recent, selected_thread_id=recent.thread_id,
            recent_sessions=(recent,), selection_mode="auto",
        )

        contract = build_ui_scope_contract(item)

        self.assertIsNone(contract.selected_scope)
        self.assertIsNone(contract.selected_thread_id)
        self.assertFalse(contract.selected_is_pinned)
        self.assertFalse(contract.selected_is_activity)

    def test_missing_pinned_session_does_not_claim_a_viewing_scope(self):
        item = replace(
            snapshot(), selected_session=None, selected_thread_id="missing",
            selection_mode="pinned",
        )

        contract = build_ui_scope_contract(item)

        self.assertIsNone(contract.selected_scope)
        self.assertIsNone(contract.selected_thread_id)
        self.assertFalse(contract.selected_is_pinned)

    def test_safe_session_labels_use_metadata_fallback_without_anonymous_code(self):
        session = self._session("raw-thread-must-not-render", "exact", in_progress=False)

        label = safe_session_primary_label(
            session,
            "en",
            role_key="historical_session_role",
            viewing=True,
        )

        self.assertIn("Historical session", label)
        self.assertIn(
            session.observed_at.astimezone().strftime("%m-%d %H:%M"),
            label,
        )
        self.assertIn("2 turns", label)
        self.assertNotIn("Viewing", label)
        self.assertNotIn("Anonymous", label)
        self.assertNotIn(session.thread_id, label)
        self.assertNotIn(session.display_title, label)

    def test_history_labels_never_promote_structured_title_to_primary(self):
        session = self._session("safe-thread", "exact", in_progress=False)
        row = present_dashboard(replace(
            snapshot(), current_session=session,
            recent_sessions=(session,),
        ), False).recent_sessions[0]

        labels = disambiguated_session_labels(
            (replace(row, display_title="Private-looking title"),),
            "en",
            activity_thread_id=session.thread_id,
            activity_is_running=False,
            selected_thread_id=session.thread_id,
        )

        label = labels[session.thread_id]
        self.assertIn("Recent activity", label)
        self.assertNotIn("Viewing", label)
        self.assertNotIn("Anonymous", label)
        self.assertNotIn("Private-looking title", label)

    def test_presenter_uses_codex_app_server_titles_in_recent_rows(self):
        safe_names = (
            "整理本周项目进展",
            "Review release checklist",
        )
        sessions = tuple(
            replace(
                self._session(f"safe-thread-{index}", "exact", in_progress=False),
                display_title=name,
                title_source="codex_app_server.thread_display_title",
                full_title=name,
            )
            for index, name in enumerate(safe_names)
        )

        presentation = present_dashboard(replace(
            snapshot(), current_session=sessions[0], recent_sessions=sessions,
        ), False)

        rendered = repr(presentation.recent_sessions)
        for fragment in safe_names:
            self.assertIn(fragment, rendered)

    def test_unreconciled_usage_does_not_hide_explicit_running_lifecycle(self):
        running = self._session("running", "in_progress", in_progress=True)
        running = replace(
            running,
            instruction=replace(running.instruction, unreconciled_events=1),
        )
        item = replace(
            snapshot(), current_session=running, current_thread_id=running.thread_id,
            selected_session=running, selected_thread_id=running.thread_id,
            recent_sessions=(running,), selection_mode="auto",
        )

        self.assertEqual(
            build_ui_scope_contract(item).activity_scope,
            UiDataScope.CURRENT_ACTIVITY,
        )

    def test_history_empty_states_are_distinct_and_deterministic(self):
        cases = {
            HistoryEmptyState.UNAVAILABLE: dict(source_available=False),
            HistoryEmptyState.MAPPING_FAILED: dict(mapping_failed=True),
            HistoryEmptyState.SELECTED_NO_HISTORY: dict(
                selected_session_without_history=True,
            ),
            HistoryEmptyState.NO_SELECTION: dict(selection_required=True),
            HistoryEmptyState.FIRST_USE: dict(has_any_history=False),
            HistoryEmptyState.IN_PROGRESS_ONLY: dict(
                has_range_rows=False, in_progress_observation_count=1,
            ),
            HistoryEmptyState.RANGE_EMPTY: dict(has_range_rows=False),
            HistoryEmptyState.BACKFILL_INCOMPLETE: dict(backfill_incomplete=True),
            HistoryEmptyState.STALE: dict(stale=True),
            HistoryEmptyState.PARTIAL: dict(coverage_state="partial"),
            HistoryEmptyState.AVAILABLE: {},
        }
        for expected, overrides in cases.items():
            values = dict(
                source_available=True, has_any_history=True,
                has_range_rows=True, in_progress_observation_count=0,
                selection_required=False, mapping_failed=False,
                coverage_state="complete_for_local_history", stale=False,
                backfill_incomplete=False,
                selected_session_without_history=False,
            )
            values.update(overrides)
            with self.subTest(expected=expected):
                self.assertEqual(classify_history_empty_state(**values), expected)

        self.assertEqual(
            classify_history_empty_state(
                source_available=True, has_any_history=False,
                has_range_rows=False, in_progress_observation_count=2,
                selection_required=False, mapping_failed=False,
                coverage_state="no_observations", stale=False,
                backfill_incomplete=False,
                selected_session_without_history=False,
            ),
            HistoryEmptyState.IN_PROGRESS_ONLY,
        )
        self.assertEqual(
            classify_history_empty_state(
                source_available=True, has_any_history=False,
                has_range_rows=False, in_progress_observation_count=0,
                selection_required=True, mapping_failed=False,
                coverage_state="no_observations", stale=False,
                backfill_incomplete=False,
                selected_session_without_history=False,
            ),
            HistoryEmptyState.FIRST_USE,
        )
        self.assertEqual(
            classify_history_empty_state(
                source_available=True, has_any_history=False,
                has_range_rows=False, in_progress_observation_count=0,
                selection_required=True, mapping_failed=False,
                coverage_state="no_observations", stale=False,
                backfill_incomplete=True,
                selected_session_without_history=False,
            ),
            HistoryEmptyState.BACKFILL_INCOMPLETE,
        )
        self.assertEqual(
            classify_history_empty_state(
                source_available=True, has_any_history=True,
                has_range_rows=False, in_progress_observation_count=1,
                selection_required=False, mapping_failed=False,
                coverage_state="no_observations", stale=False,
                backfill_incomplete=False,
                selected_session_without_history=True,
            ),
            HistoryEmptyState.IN_PROGRESS_ONLY,
        )

    def test_each_history_state_has_an_actionable_ui_contract(self):
        states = tuple(HistoryEmptyState)
        for state in states:
            with self.subTest(state=state):
                view = build_history_state_view(state)
                self.assertEqual(view.kind, state)
                self.assertTrue(view.title_key)
                self.assertTrue(view.reason_key)
                self.assertTrue(view.realtime_impact_key)
                if state is not HistoryEmptyState.AVAILABLE:
                    self.assertIsNotNone(view.primary_action)
                    self.assertTrue(view.primary_action.label_key)

    def test_required_history_state_matrix_has_exact_primary_and_fallback(self):
        expected = {
            HistoryEmptyState.FIRST_USE: ("refresh", None),
            HistoryEmptyState.SELECTED_NO_HISTORY: (
                "choose_session", "expand_range",
            ),
            HistoryEmptyState.RANGE_EMPTY: ("expand_range", "view_all"),
            HistoryEmptyState.IN_PROGRESS_ONLY: ("view_activity", "refresh"),
            HistoryEmptyState.PARTIAL: ("view_coverage", "use_current"),
            HistoryEmptyState.NO_SELECTION: ("choose_session", "view_all"),
            HistoryEmptyState.MAPPING_FAILED: (
                "expand_range", "keep_ranking",
            ),
            HistoryEmptyState.STALE: ("refresh", None),
            HistoryEmptyState.BACKFILL_INCOMPLETE: (
                "use_current", "retry",
            ),
            HistoryEmptyState.UNAVAILABLE: ("refresh", None),
        }
        for state, actions in expected.items():
            with self.subTest(state=state):
                view = build_history_state_view(state)
                self.assertEqual(view.primary_action.kind, actions[0])
                self.assertEqual(
                    view.fallback_action.kind if view.fallback_action else None,
                    actions[1],
                )
                self.assertTrue(view.title_key.endswith("_title"))
                self.assertTrue(view.reason_key.endswith("_reason"))
                self.assertTrue(view.realtime_impact_key.endswith("_impact"))

    def test_quota_combinations_keep_live_and_local_history_scopes_separate(self):
        cases = (
            ((True, True, True, False), QuotaAvailability.LIVE_AND_HISTORY),
            ((True, True, False, False), QuotaAvailability.LIVE_ONLY),
            ((False, False, True, False), QuotaAvailability.HISTORY_ONLY),
            ((False, True, True, False), QuotaAvailability.WEEKLY_ONLY),
            ((True, True, True, True), QuotaAvailability.STALE_LIVE),
            ((False, False, False, False), QuotaAvailability.UNAVAILABLE),
        )
        for arguments, expected in cases:
            with self.subTest(expected=expected):
                contract = classify_quota_availability(*arguments)
                self.assertEqual(contract.state, expected)
                self.assertEqual(contract.five_hour_live_available, arguments[0])
                self.assertEqual(contract.weekly_live_available, arguments[1])
                self.assertEqual(contract.local_history_available, arguments[2])
                self.assertTrue(contract.local_history_source_available)
                self.assertEqual(contract.live_stale, arguments[3])

        weekly_with_history = classify_quota_availability(False, True, True, False)
        weekly_without_history = classify_quota_availability(False, True, False, False)
        self.assertEqual(weekly_with_history.state, QuotaAvailability.WEEKLY_ONLY)
        self.assertEqual(weekly_without_history.state, QuotaAvailability.WEEKLY_ONLY)
        self.assertNotEqual(
            weekly_with_history.local_history_available,
            weekly_without_history.local_history_available,
        )

    def test_quota_empty_and_query_failure_have_distinct_actionable_views(self):
        empty = classify_quota_availability(
            False, False, False, False, history_source_available=True,
        )
        failed = classify_quota_availability(
            False, False, False, False, history_source_available=False,
        )

        empty_view = build_quota_state_view(empty)
        failed_view = build_quota_state_view(failed)

        self.assertNotEqual(empty_view.title_key, failed_view.title_key)
        self.assertNotEqual(empty_view.reason_key, failed_view.reason_key)
        self.assertIsNotNone(empty_view.primary_action)
        self.assertIsNotNone(failed_view.primary_action)

    def test_every_nonhealthy_quota_state_has_reason_actions_and_impact(self):
        contracts = (
            classify_quota_availability(True, True, False, False),
            classify_quota_availability(False, False, True, False),
            classify_quota_availability(False, True, False, False),
            classify_quota_availability(True, True, True, True),
            classify_quota_availability(False, False, False, False),
            classify_quota_availability(
                False, False, False, False,
                history_source_available=False,
            ),
        )
        for contract in contracts:
            with self.subTest(contract=contract):
                view = build_quota_state_view(contract)
                self.assertTrue(view.title_key)
                self.assertTrue(view.reason_key)
                self.assertTrue(view.realtime_impact_key)
                self.assertIsNotNone(view.primary_action)

    def test_required_quota_matrix_has_exact_variants_and_action_scopes(self):
        cases = (
            ((True, True, False, False, True), "live_only", "refresh_quota", None),
            ((False, False, True, False, True), "history_only", "refresh_quota", "view_quota_history"),
            ((True, True, True, False, True), "live_and_history", "view_quota_history", None),
            ((False, False, False, False, False), "sources_unavailable", "refresh_quota", None),
            ((False, True, False, False, True), "weekly_only", "refresh_quota", None),
            ((True, True, True, True, True), "stale_live", "refresh_quota", "view_quota_history"),
        )
        for arguments, variant, primary, fallback in cases:
            with self.subTest(variant=variant):
                five, weekly, history, stale, source = arguments
                contract = classify_quota_availability(
                    five, weekly, history, stale,
                    history_source_available=source,
                )
                view = build_quota_state_view(contract)
                self.assertIn(f"quota_state_{variant}_", view.title_key)
                self.assertEqual(view.primary_action.kind, primary)
                self.assertEqual(
                    view.fallback_action.kind if view.fallback_action else None,
                    fallback,
                )
                if primary == "refresh_quota":
                    self.assertEqual(
                        view.primary_action.target_scope,
                        UiDataScope.LIVE_QUOTA,
                    )
                if fallback == "view_quota_history":
                    self.assertEqual(
                        view.fallback_action.target_scope,
                        UiDataScope.LOCAL_QUOTA_HISTORY,
                    )

    def test_token_history_expand_range_targets_selected_session_scope(self):
        view = build_history_state_view(HistoryEmptyState.RANGE_EMPTY)

        self.assertEqual(
            view.primary_action.target_scope,
            UiDataScope.SELECTED_SESSION,
        )


if __name__ == "__main__":
    unittest.main()
