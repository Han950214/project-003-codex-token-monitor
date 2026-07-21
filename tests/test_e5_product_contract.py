"""Phase 3.1-E5 product-surface regression contract."""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.dashboard import MiniThreadSnapshot
from app.dashboard_mode import ALL_PAGES, NAVIGATION_ITEMS, SECONDARY_PAGES, AppShellState
from app.desktop_widget import DesktopMiniWidget
from app.i18n import TRANSLATIONS
from app.main import HEAVY_PAGES, Dashboard
from app.quota import CodexQuotaSnapshot
from app.widget_presentation import present_widget


class PhaseE5ProductContractTests(unittest.TestCase):
    def test_navigation_contains_only_retained_product_pages(self) -> None:
        self.assertEqual(
            NAVIGATION_ITEMS,
            ("overview", "sessions", "usage_trends", "settings"),
        )
        self.assertEqual(SECONDARY_PAGES, ("session_detail",))
        self.assertEqual(ALL_PAGES, NAVIGATION_ITEMS + SECONDARY_PAGES)
        self.assertNotIn("recommendations", ALL_PAGES)
        self.assertNotIn("tools", ALL_PAGES)
        self.assertEqual(AppShellState().navigate("recommendations").page, "overview")
        self.assertEqual(AppShellState().navigate("tools").page, "overview")
        self.assertEqual(AppShellState().navigate("unknown").page, "overview")
        self.assertEqual(HEAVY_PAGES, {"sessions", "usage_trends"})

    def test_removed_feature_modules_and_entry_points_are_absent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for name in (
            "advisor.py", "diagnostics.py", "diagnostics_worker.py",
            "app_actions.py", "new_thread.py",
        ):
            self.assertFalse((root / "app" / name).exists(), name)

        shell_source = inspect.getsource(Dashboard.__init__)
        widget_source = inspect.getsource(DesktopMiniWidget)
        self.assertNotIn('"recommendations":', shell_source)
        self.assertNotIn('"tools":', shell_source)
        self.assertNotIn("on_more", widget_source)
        self.assertNotIn('translate("more_tools"', widget_source)

    def test_overview_is_single_column_in_required_order(self) -> None:
        source = inspect.getsource(Dashboard._apply_status_layout)
        required = (
            "session_selector_card", "core_metrics_panel", "quota_center_card",
            "observed_usage_card", "trend_preview_card",
        )
        positions = tuple(source.index(name) for name in required)
        self.assertEqual(positions, tuple(sorted(positions)))
        self.assertIn("column=0", source)
        self.assertNotIn("column=1", source)

    def test_removed_copy_is_not_in_translation_catalog(self) -> None:
        removed = {
            "nav_recommendations", "nav_tools", "more_tools",
            "update_placeholder", "new_thread", "diagnostics_title",
        }
        for catalog in TRANSLATIONS.values():
            self.assertTrue(removed.isdisjoint(catalog))

    def test_widget_status_uses_only_session_and_quota_facts(self) -> None:
        observed_at = datetime(2026, 7, 21, tzinfo=timezone.utc)
        quota = CodexQuotaSnapshot.unavailable(observed_at=observed_at)
        thread = MiniThreadSnapshot(
            "Safe title",
            1234,
            5678,
            "available",
            observed_at,
            3,
            response_status="completed_partial",
        )

        presentation = present_widget(quota, thread, "en")

        self.assertEqual(presentation.status, "warning")
        self.assertIn("Partial", presentation.status_text)
        self.assertEqual(presentation.instruction_total, "1.2K")
        self.assertEqual(presentation.session_total, "5.7K")


if __name__ == "__main__":
    unittest.main()
