import inspect
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.advisor import (
    ADVISOR_RULE_CODES,
    DATA_STALE_AFTER,
    NEW_THREAD_TURN_COUNT,
    OPTIMIZE_CACHE_HIT_PERCENT,
    OPTIMIZE_INPUT_TOKENS,
    QUOTA_RISK_REMAINING_PERCENT,
    AdvisorInput,
    Recommendation,
    evaluate_advice,
)
from app.dashboard_mode import AppShellState, NAVIGATION_ITEMS
from app.desktop_widget import DesktopMiniWidget, HOVER_ALPHA, format_percent
from app.diagnostics import (
    DIAGNOSTIC_CHECK_CODES,
    DiagnosticContext,
    inspect_settings_file,
    run_diagnostics,
)
from app.i18n import TRANSLATIONS, translate
from app.main import Dashboard
from app.new_thread import generic_handoff_template
from app.ui_presenter import _latest_metrics
from app.ui_settings import (
    load_auto_refresh_enabled,
    load_dashboard_mode,
    load_exit_behavior,
    load_language,
    load_startup_mode,
    load_widget_idle_opacity,
    load_widget_mode,
    save_auto_refresh_enabled,
    save_dashboard_mode,
    save_exit_behavior,
    save_language,
    save_startup_mode,
    save_widget_idle_opacity,
    save_widget_mode,
    validate_ui_settings,
)


NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


def advisor_input(**changes):
    base = AdvisorInput(
        data_available=True,
        data_age_seconds=10,
        source_status="normal",
        five_hour_remaining_percent=75.0,
        weekly_remaining_percent=80.0,
        turn_count=4,
        instruction_input_tokens=20_000,
        instruction_total_tokens=22_000,
        cached_input_tokens=10_000,
        session_total_tokens=100_000,
        session_status="in_progress",
        observed_at=NOW,
    )
    return replace(base, **changes)


class Phase3ModeTests(unittest.TestCase):
    def test_dashboard_mode_defaults_to_simple(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_dashboard_mode(Path(directory) / "missing.json"), "simple")

    def test_widget_mode_defaults_to_compact(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_widget_mode(Path(directory) / "missing.json"), "compact")

    def test_dashboard_and_widget_modes_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertTrue(save_dashboard_mode("advanced", path))
            self.assertTrue(save_widget_mode("expanded", path))
            self.assertEqual((load_dashboard_mode(path), load_widget_mode(path)), ("advanced", "expanded"))

    def test_corrupt_and_unknown_modes_use_safe_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("broken", encoding="utf-8")
            self.assertEqual((load_dashboard_mode(path), load_widget_mode(path)), ("simple", "compact"))
            path.write_text('{"dashboard_mode":"x","widget_mode":"y"}', encoding="utf-8")
            self.assertEqual((load_dashboard_mode(path), load_widget_mode(path)), ("simple", "compact"))

    def test_mode_transition_preserves_selection_pagination_and_auto_refresh(self):
        state = AppShellState(
            page="history", selected_thread_id="thread-1", history_page=3,
            auto_refresh_enabled=True,
        )
        changed = state.with_dashboard_mode("advanced")
        self.assertEqual(changed.selected_thread_id, "thread-1")
        self.assertEqual(changed.history_page, 3)
        self.assertTrue(changed.auto_refresh_enabled)
        self.assertEqual(changed.page, "history")

    def test_invalid_mode_transition_returns_simple(self):
        self.assertEqual(AppShellState(dashboard_mode="advanced").with_dashboard_mode("bad").dashboard_mode, "simple")

    def test_navigation_has_exactly_five_product_entries(self):
        self.assertEqual(NAVIGATION_ITEMS, ("status_center", "current_task", "history", "tools", "settings"))

    def test_navigation_transition_is_query_free(self):
        source = inspect.getsource(Dashboard.show_page)
        for forbidden in ("refresh(", "view_model", "quota_provider", "title_loader"):
            self.assertNotIn(forbidden, source)

    def test_dashboard_mode_transition_is_query_free(self):
        source = inspect.getsource(Dashboard.set_dashboard_mode)
        for forbidden in ("refresh(", "view_model", "quota_provider", "rollout", "sqlite"):
            self.assertNotIn(forbidden, source.lower())

    def test_auto_refresh_and_exit_behavior_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_auto_refresh_enabled(True, path)
            save_exit_behavior("minimize", path)
            self.assertTrue(load_auto_refresh_enabled(path))
            self.assertEqual(load_exit_behavior(path), "minimize")


class Phase3AdvisorTests(unittest.TestCase):
    def test_rule_codes_and_thresholds_are_centralized(self):
        self.assertEqual(len(ADVISOR_RULE_CODES), 6)
        self.assertGreater(NEW_THREAD_TURN_COUNT, 0)
        self.assertGreater(OPTIMIZE_INPUT_TOKENS, 0)
        self.assertGreater(OPTIMIZE_CACHE_HIT_PERCENT, 0)
        self.assertGreater(QUOTA_RISK_REMAINING_PERCENT, 0)
        self.assertGreater(DATA_STALE_AFTER.total_seconds(), 0)

    def test_data_unavailable_has_highest_priority(self):
        result = evaluate_advice(advisor_input(
            data_available=False, five_hour_remaining_percent=1,
            turn_count=NEW_THREAD_TURN_COUNT,
            instruction_input_tokens=OPTIMIZE_INPUT_TOKENS,
            cached_input_tokens=0,
        ))
        self.assertEqual(result.primary.status, "data_unavailable")

    def test_stale_data_is_data_unavailable_status(self):
        result = evaluate_advice(advisor_input(data_age_seconds=round(DATA_STALE_AFTER.total_seconds()) + 1))
        self.assertEqual((result.primary.code, result.primary.status), ("data_stale", "data_unavailable"))

    def test_quota_risk_precedes_optimize(self):
        result = evaluate_advice(advisor_input(
            five_hour_remaining_percent=QUOTA_RISK_REMAINING_PERCENT,
            instruction_input_tokens=OPTIMIZE_INPUT_TOKENS,
            cached_input_tokens=0,
        ))
        self.assertEqual(result.primary.status, "quota_risk")

    def test_new_thread_precedes_optimize(self):
        result = evaluate_advice(advisor_input(
            turn_count=NEW_THREAD_TURN_COUNT,
            instruction_input_tokens=OPTIMIZE_INPUT_TOKENS,
            cached_input_tokens=0,
        ))
        self.assertEqual(result.primary.status, "new_thread")

    def test_low_cache_reuse_on_large_input_suggests_optimize(self):
        result = evaluate_advice(advisor_input(
            instruction_input_tokens=OPTIMIZE_INPUT_TOKENS,
            cached_input_tokens=0,
        ))
        self.assertEqual(result.primary.status, "optimize")
        self.assertIn(("cache_hit_percent_derived", 0.0), result.primary.evidence)

    def test_normal_state_is_stable(self):
        self.assertEqual(evaluate_advice(advisor_input()).primary.code, "normal")

    def test_missing_numeric_fields_do_not_crash(self):
        result = evaluate_advice(advisor_input(
            turn_count=None, instruction_input_tokens=None,
            instruction_total_tokens=None, cached_input_tokens=None,
            session_total_tokens=None, five_hour_remaining_percent=None,
            weekly_remaining_percent=None,
        ))
        self.assertEqual(result.primary.status, "normal")

    def test_same_input_has_deterministic_output(self):
        data = advisor_input(turn_count=NEW_THREAD_TURN_COUNT)
        self.assertEqual(evaluate_advice(data), evaluate_advice(data))

    def test_evidence_rejects_content_fields_and_free_text(self):
        with self.assertRaises(ValueError):
            Recommendation("normal", "normal", "x", "y", "z", (("prompt", 1),), NOW)
        with self.assertRaises(ValueError):
            Recommendation("normal", "normal", "x", "y", "z", (("session_status", "project text"),), NOW)

    def test_advice_wording_marks_cache_rate_as_non_official(self):
        self.assertIn("不是官方", translate("advisor_optimize_body", "zh-CN"))
        self.assertIn("not an official", translate("advisor_optimize_body", "en"))


class Phase3DiagnosticsTests(unittest.TestCase):
    def context(self, root: Path, **changes):
        base = DiagnosticContext(
            version="0.1.0",
            runtime_mode="dashboard",
            frozen=False,
            codex_executable_found=True,
            quota_probe=lambda: "normal",
            rollout_root=root,
            rollout_probe=lambda: 2,
            state_path=root / "missing.sqlite",
            settings_path=root / "missing.json",
            startup_status=lambda: "unused",
            tray_started=True,
            refreshed_at=NOW,
        )
        return replace(base, **changes)

    def test_diagnostics_run_all_thirteen_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_diagnostics(self.context(Path(directory)), now=NOW)
        self.assertEqual(tuple(item.code for item in report.results), DIAGNOSTIC_CHECK_CODES)

    def test_one_failure_does_not_abort_other_checks(self):
        def fail():
            raise RuntimeError("connection")
        with tempfile.TemporaryDirectory() as directory:
            report = run_diagnostics(self.context(Path(directory), quota_probe=fail), now=NOW)
        self.assertEqual(len(report.results), 13)
        self.assertEqual(report.results[4].status, "failure")
        self.assertEqual(report.results[-1].status, "normal")

    def test_numeric_probe_failure_is_isolated(self):
        def fail():
            raise OSError("read")
        with tempfile.TemporaryDirectory() as directory:
            report = run_diagnostics(self.context(Path(directory), rollout_probe=fail), now=NOW)
        numeric = next(item for item in report.results if item.code == "safe_numeric_data")
        self.assertEqual(numeric.status, "failure")

    def test_invalid_settings_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(inspect_settings_file(path), "failure")
            self.assertEqual(validate_ui_settings(path), (False, "invalid_json"))

    def test_invalid_sqlite_schema_is_detected_without_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite"
            sqlite3.connect(path).close()
            report = run_diagnostics(self.context(Path(directory), state_path=path), now=NOW)
        sqlite_result = next(item for item in report.results if item.code == "sqlite_adapter")
        self.assertEqual(sqlite_result.status, "failure")

    def test_stale_data_is_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            report = run_diagnostics(
                self.context(Path(directory), refreshed_at=NOW - timedelta(minutes=4)), now=NOW,
            )
        self.assertEqual(report.results[-1].status, "warning")

    def test_diagnostics_can_repeat_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self.context(Path(directory))
            self.assertEqual(run_diagnostics(context, now=NOW), run_diagnostics(context, now=NOW))

    def test_diagnostic_contract_contains_no_content_or_credentials(self):
        fields = set(DiagnosticContext.__dataclass_fields__)
        forbidden = {"prompt", "response", "message", "reasoning", "authorization", "cookie", "secret"}
        self.assertTrue(fields.isdisjoint(forbidden))

    def test_diagnostic_translations_exist_in_both_languages(self):
        for code in DIAGNOSTIC_CHECK_CODES:
            self.assertIn(f"diagnostic_name_{code}", TRANSLATIONS["zh-CN"])
            self.assertIn(f"diagnostic_name_{code}", TRANSLATIONS["en"])


class Phase3WidgetAndSettingsTests(unittest.TestCase):
    def test_all_settings_round_trip_without_losing_existing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_language("en", path)
            save_startup_mode("tray", path)
            save_widget_idle_opacity(0.7, path)
            save_dashboard_mode("advanced", path)
            save_widget_mode("expanded", path)
            save_auto_refresh_enabled(True, path)
            save_exit_behavior("minimize", path)
            self.assertEqual(load_language(path), "en")
            self.assertEqual(load_startup_mode(path), "tray")
            self.assertEqual(load_widget_idle_opacity(path), 0.7)
            self.assertEqual(load_dashboard_mode(path), "advanced")
            self.assertEqual(load_widget_mode(path), "expanded")
            self.assertTrue(load_auto_refresh_enabled(path))
            self.assertEqual(load_exit_behavior(path), "minimize")

    def test_settings_validation_rejects_invalid_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"dashboard_mode": "broken"}), encoding="utf-8")
            self.assertFalse(validate_ui_settings(path)[0])

    def test_widget_mode_transition_is_query_free(self):
        source = inspect.getsource(DesktopMiniWidget.set_mode)
        for forbidden in ("refresh(", "rollout", "sqlite", "quota_provider", "mainloop"):
            self.assertNotIn(forbidden, source.lower())

    def test_widget_build_contains_compact_and_expanded_surfaces(self):
        source = inspect.getsource(DesktopMiniWidget)
        self.assertIn("compact_frame", source)
        self.assertIn("expanded_frame", source)
        self.assertEqual(source.count("ctk.CTkToplevel("), 2)  # widget plus opacity popover

    def test_widget_hover_opacity_remains_full(self):
        self.assertEqual(HOVER_ALPHA, 1.0)

    def test_unknown_widget_quota_is_dash(self):
        self.assertEqual(format_percent(None), "—")

    def test_widget_switch_does_not_rebuild_root(self):
        source = inspect.getsource(DesktopMiniWidget.set_mode)
        self.assertNotIn("CTk(", source)
        self.assertNotIn("CTkToplevel(", source)

    def test_settings_callbacks_do_not_read_product_data(self):
        source = "".join(inspect.getsource(method) for method in (
            Dashboard._settings_startup_changed,
            Dashboard._settings_widget_changed,
            Dashboard._settings_exit_changed,
            Dashboard._settings_opacity_changed,
        ))
        for forbidden in ("view_model", "quota_provider", "refresh("):
            self.assertNotIn(forbidden, source)


class Phase3ProductBoundaryTests(unittest.TestCase):
    def test_simple_home_has_one_primary_action_control(self):
        source = inspect.getsource(Dashboard._build_simple_status_center)
        self.assertEqual(source.count("self.primary_action_button ="), 1)

    def test_simple_home_has_three_quick_primary_entries(self):
        source = inspect.getsource(Dashboard._build_simple_status_center)
        for name in ("quick_diagnose", "quick_codex", "quick_history"):
            self.assertIn(name, source)

    def test_advanced_metrics_are_six_required_fields(self):
        metrics = _latest_metrics(None, "unavailable")
        self.assertEqual(
            [item.label for item in metrics],
            ["Input", "Output", "Total", "Cached", "Reasoning", "Cache Hit"],
        )

    def test_generic_handoff_template_contains_only_manual_placeholder(self):
        chinese = generic_handoff_template("zh-CN")
        english = generic_handoff_template("en")
        self.assertIn("手动整理", chinese)
        self.assertIn("请在这里", chinese)
        self.assertIn("organized manually", english)
        for value in (chinese.lower(), english.lower()):
            self.assertNotIn("response", value)
            self.assertNotIn("reasoning", value)
            self.assertNotIn("tool output", value)

    def test_new_thread_dialog_uses_one_toplevel_and_no_mainloop(self):
        source = inspect.getsource(Dashboard._show_new_thread_dialog)
        self.assertEqual(source.count("ctk.CTkToplevel("), 1)
        self.assertNotIn("mainloop", source)
        self.assertNotIn("view_model", source)

    def test_no_aos_runtime_dependency_is_added(self):
        requirements = Path("requirements.txt").read_text(encoding="utf-8").lower()
        build_requirements = Path("requirements-build.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("aos", requirements + build_requirements)

    def test_product_actions_do_not_offer_knowledge_features(self):
        source = inspect.getsource(Dashboard._build_tools_page)
        for forbidden in ("knowledge", "project_export", "context_restore", "scan_project"):
            self.assertNotIn(forbidden, source.lower())

    def test_all_new_product_keys_are_bilingual(self):
        keys = {
            "nav_status_center", "nav_current_task", "nav_history", "nav_tools",
            "nav_settings", "mode_simple", "mode_advanced", "prepare_new_thread",
            "diagnostics_title", "widget_compact", "widget_expanded",
        }
        self.assertTrue(keys.issubset(TRANSLATIONS["zh-CN"]))
        self.assertTrue(keys.issubset(TRANSLATIONS["en"]))


if __name__ == "__main__":
    unittest.main()
