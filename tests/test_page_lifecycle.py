import unittest
from unittest.mock import Mock, patch

import app.main as main_module
from app.dashboard_mode import ALL_PAGES
from app.main import Dashboard


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self.visible = None
        self.children = []
        self.destroyed = False
        self.configure_calls = []

    def grid(self, *args, **kwargs):
        self.visible = True

    def grid_remove(self):
        self.visible = False

    def grid_columnconfigure(self, *args, **kwargs):
        return None

    def grid_rowconfigure(self, *args, **kwargs):
        return None

    def winfo_children(self):
        return list(self.children)

    def destroy(self):
        self.destroyed = True

    def configure(self, **kwargs):
        self.configure_calls.append(kwargs)

    def set(self, value):
        self.value = value


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeRoot:
    def __init__(self):
        self.idle_callbacks = []
        self.after_callbacks = []
        self.cancelled = set()

    def after_idle(self, callback):
        self.idle_callbacks.append(callback)
        return f"idle-{len(self.idle_callbacks)}"

    def after(self, _milliseconds, callback):
        job = f"after-{len(self.after_callbacks)}"
        self.after_callbacks.append((job, callback))
        return job

    def after_cancel(self, job):
        self.cancelled.add(job)

    def run_after_callbacks(self):
        for job, callback in list(self.after_callbacks):
            if job not in self.cancelled:
                callback()


class FakeShellState:
    def __init__(self, page):
        self.page = page

    def navigate(self, page):
        self.page = page
        return self


class FakeAutoRefresh:
    interval_seconds = 60

    def __init__(self):
        self.enabled = []

    def set_enabled(self, enabled):
        self.enabled.append(enabled)


class QueryBomb:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected product query: {name}")


class FakeScrollCanvas:
    def __init__(self):
        self.bounds = (0, 0, 100, 200)
        self.scrollregions = []

    def bbox(self, _target):
        return self.bounds

    def configure(self, **kwargs):
        self.scrollregions.append(kwargs["scrollregion"])


class FakeScrollableFrame:
    def __init__(self):
        self._parent_canvas = FakeScrollCanvas()
        self.configure_callback = None

    def bind(self, _event, callback):
        self.configure_callback = callback


class PageLifecycleTests(unittest.TestCase):
    def test_continuous_resize_uses_lightweight_placeholder_then_restores_page(self):
        dashboard = object.__new__(Dashboard)
        overview = FakeWidget()
        overview.visible = True
        dashboard.page_frames = {"overview": overview}
        dashboard.current_nav_page = "overview"
        dashboard.resize_placeholder = FakeWidget()
        dashboard.resize_placeholder.visible = False
        dashboard._resize_content_hidden = False
        dashboard._resize_suspended_page = None
        dashboard._apply_responsive_layout = Mock()

        Dashboard._suspend_content_for_resize(dashboard)

        self.assertFalse(overview.visible)
        self.assertTrue(dashboard.resize_placeholder.visible)
        self.assertTrue(dashboard._resize_content_hidden)

        Dashboard._finish_resize_layout(dashboard)

        dashboard._apply_responsive_layout.assert_called_once()
        self.assertTrue(overview.visible)
        self.assertFalse(dashboard.resize_placeholder.visible)
        self.assertFalse(dashboard._resize_content_hidden)

    def test_idle_heavy_page_prune_keeps_overview_and_most_recent_heavy_state(self):
        dashboard = object.__new__(Dashboard)
        dashboard.current_nav_page = "overview"
        dashboard._recent_heavy_page = "recommendations"
        dashboard._heavy_page_prune_job = "pending"
        dashboard._heavy_page_destroy_count = 0
        dashboard.page_frames = {
            page: FakeWidget()
            for page in ("overview", "sessions", "usage_trends", "recommendations")
        }
        dashboard.built_pages = set(dashboard.page_frames)
        dashboard.session_search_var = FakeVar(value="needle")

        Dashboard._prune_inactive_heavy_pages(dashboard)

        self.assertEqual(
            dashboard.built_pages,
            {"overview", "recommendations"},
        )
        self.assertEqual(dashboard._session_search_text, "needle")
        self.assertEqual(dashboard._heavy_page_destroy_count, 2)
        self.assertIsNone(dashboard._heavy_page_prune_job)

    def test_pruned_heavy_page_rebuilds_with_preserved_search_state(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = FakeRoot()
        dashboard.page_host = FakeWidget()
        dashboard.current_nav_page = "overview"
        dashboard._recent_heavy_page = "recommendations"
        dashboard._heavy_page_prune_job = None
        dashboard._heavy_page_destroy_count = 0
        dashboard.page_frames = {
            page: FakeWidget()
            for page in ("overview", "sessions", "recommendations")
        }
        dashboard.built_pages = set(dashboard.page_frames)
        dashboard._building_pages = set()
        dashboard._page_build_errors = {}
        dashboard.session_search_var = FakeVar(value="needle")
        dashboard.language = "zh-CN"
        dashboard.page_builders = {
            "sessions": lambda _parent: setattr(
                dashboard, "rebuilt_search_text", dashboard._session_search_text,
            ),
        }
        dashboard._apply_deferred_page_language = Mock()
        dashboard._render_visible_page = Mock()
        dashboard._apply_responsive_layout = Mock()

        Dashboard._prune_inactive_heavy_pages(dashboard)
        dashboard.current_nav_page = "sessions"
        with patch.object(main_module.ctk, "CTkFrame", FakeWidget), patch.object(
            main_module.ctk, "CTkLabel", FakeWidget,
        ):
            self.assertFalse(Dashboard.ensure_page_built(dashboard, "sessions"))
            dashboard.root.idle_callbacks.pop()()

        self.assertEqual(dashboard.rebuilt_search_text, "needle")
        self.assertIn("sessions", dashboard.built_pages)

    def test_compact_metric_text_preserves_title_value_scope_and_hint(self):
        self.assertEqual(
            Dashboard._compact_metric_text(
                "Current turn", "12.4K", scope="latest response", hint="Running",
            ),
            "Current turn · latest response\n12.4K · Running",
        )

    def test_scrollregion_is_updated_only_when_content_bounds_change(self):
        frame = FakeScrollableFrame()

        Dashboard._stabilize_scrollable_frame(frame)
        frame.configure_callback(None)
        frame.configure_callback(None)
        frame._parent_canvas.bounds = (0, 0, 100, 260)
        frame.configure_callback(None)

        self.assertEqual(
            frame._parent_canvas.scrollregions,
            [(0, 0, 100, 200), (0, 0, 100, 260)],
        )

    def test_unbuilt_or_destroyed_page_does_not_run_responsive_layout(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = FakeWidget()
        dashboard.current_nav_page = "usage_trends"
        dashboard.built_pages = {"overview"}
        dashboard.page_frames = {"overview": FakeWidget()}
        dashboard._sidebar_collapsed = False
        dashboard._header_collapsed_state = False
        dashboard._last_layout_signature = None
        dashboard._layout_apply_count = 0
        dashboard._layout_skip_count = 0
        dashboard._logical_window_width = lambda: 1280
        dashboard._dashboard_content_width = lambda _width: 1040
        dashboard._layout_trend_metrics = Mock()

        Dashboard._apply_responsive_layout(dashboard)

        dashboard._layout_trend_metrics.assert_not_called()

    def test_startup_schedules_only_overview_and_has_no_unvisited_page_frames(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = FakeRoot()
        dashboard.main_container = FakeWidget()
        dashboard.page_frames = {}
        dashboard._dirty_pages = set(ALL_PAGES)
        dashboard.current_nav_page = "overview"
        dashboard.language = "en"
        dashboard._apply_language = Mock()
        dashboard._render_visible_page = Mock()
        dashboard._apply_responsive_layout = Mock()
        builders = {
            "overview": Mock(),
            "session_detail": Mock(),
            "sessions": Mock(),
            "usage_trends": Mock(),
            "recommendations": Mock(),
            "tools": Mock(),
            "settings": Mock(),
        }
        dashboard._build_status_center = builders["overview"]
        dashboard._build_current_task_page = builders["session_detail"]
        dashboard._build_history_page = builders["sessions"]
        dashboard._build_usage_trends_page = builders["usage_trends"]
        dashboard._build_recommendations_page = builders["recommendations"]
        dashboard._build_tools_page = builders["tools"]
        dashboard._build_settings_page = builders["settings"]

        with patch.object(main_module.ctk, "CTkFrame", FakeWidget), patch.object(
            main_module.ctk, "CTkLabel", FakeWidget,
        ):
            Dashboard._build_content(dashboard)

        for builder in builders.values():
            builder.assert_not_called()
        self.assertEqual(len(dashboard.root.idle_callbacks), 1)
        dashboard.root.idle_callbacks.pop()()

        builders["overview"].assert_called_once()
        for page in set(ALL_PAGES) - {"overview"}:
            builders[page].assert_not_called()
            self.assertNotIn(page, dashboard.page_frames)
        self.assertEqual(dashboard.built_pages, {"overview"})

    def test_delayed_page_build_is_scheduled_once_and_uses_current_state(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = FakeRoot()
        dashboard.page_host = FakeWidget()
        dashboard.page_frames = {"overview": FakeWidget()}
        dashboard.built_pages = {"overview"}
        dashboard._building_pages = set()
        dashboard._page_build_errors = {}
        dashboard.current_nav_page = "sessions"
        dashboard.language = "zh-CN"
        dashboard.presentation = "before-refresh"
        observed = []
        dashboard.page_builders = {
            "sessions": lambda _parent: observed.append(
                (dashboard.language, dashboard.presentation)
            ),
        }
        dashboard._apply_deferred_page_language = Mock()
        dashboard._render_visible_page = Mock()
        dashboard._apply_responsive_layout = Mock()

        with patch.object(main_module.ctk, "CTkFrame", FakeWidget), patch.object(
            main_module.ctk, "CTkLabel", FakeWidget,
        ):
            self.assertFalse(Dashboard.ensure_page_built(dashboard, "sessions"))
            self.assertFalse(Dashboard.ensure_page_built(dashboard, "sessions"))
            self.assertEqual(len(dashboard.root.idle_callbacks), 1)
            dashboard.presentation = "latest-presentation"
            dashboard.root.idle_callbacks.pop()()

        self.assertEqual(observed, [("zh-CN", "latest-presentation")])
        self.assertEqual(dashboard.built_pages, {"overview", "sessions"})
        dashboard._apply_deferred_page_language.assert_called_once_with(
            "sessions", "zh-CN",
        )
        dashboard._render_visible_page.assert_called_once()

    def test_failed_delayed_page_build_can_be_retried(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = FakeRoot()
        dashboard.page_host = FakeWidget()
        dashboard.page_frames = {"overview": FakeWidget()}
        dashboard.built_pages = {"overview"}
        dashboard._building_pages = set()
        dashboard._page_build_errors = {}
        dashboard.current_nav_page = "tools"
        dashboard.language = "en"
        attempts = []

        def build(_parent):
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("expected failure")

        dashboard.page_builders = {"tools": build}
        dashboard._apply_deferred_page_language = Mock()
        dashboard._render_visible_page = Mock()
        dashboard._apply_responsive_layout = Mock()

        with patch.object(main_module.ctk, "CTkFrame", FakeWidget), patch.object(
            main_module.ctk, "CTkLabel", FakeWidget,
        ), patch.object(main_module.ctk, "CTkButton", FakeWidget):
            self.assertFalse(Dashboard.ensure_page_built(dashboard, "tools"))
            dashboard.root.idle_callbacks.pop()()
            self.assertNotIn("tools", dashboard.built_pages)
            self.assertNotIn("tools", dashboard._building_pages)
            self.assertIn("tools", dashboard._page_build_errors)

            self.assertFalse(Dashboard.ensure_page_built(dashboard, "tools"))
            dashboard.root.idle_callbacks.pop()()

        self.assertEqual(len(attempts), 2)
        self.assertIn("tools", dashboard.built_pages)
        self.assertNotIn("tools", dashboard._page_build_errors)

    def test_page_finishing_after_navigation_away_stays_hidden(self):
        dashboard = object.__new__(Dashboard)
        dashboard.root = FakeRoot()
        dashboard.page_host = FakeWidget()
        dashboard.page_frames = {"overview": FakeWidget()}
        dashboard.built_pages = {"overview"}
        dashboard._building_pages = set()
        dashboard._page_build_errors = {}
        dashboard.current_nav_page = "sessions"
        dashboard.language = "en"
        dashboard.page_builders = {"sessions": lambda _parent: None}
        dashboard._apply_deferred_page_language = Mock()
        dashboard._render_visible_page = Mock()
        dashboard._apply_responsive_layout = Mock()

        with patch.object(main_module.ctk, "CTkFrame", FakeWidget), patch.object(
            main_module.ctk, "CTkLabel", FakeWidget,
        ):
            Dashboard.ensure_page_built(dashboard, "sessions")
            dashboard.current_nav_page = "overview"
            dashboard.root.idle_callbacks.pop()()

        self.assertFalse(dashboard.page_frames["sessions"].visible)
        dashboard._render_visible_page.assert_not_called()

    def _heavy_navigation_dashboard(self, initial_page="sessions"):
        dashboard = object.__new__(Dashboard)
        dashboard.root = FakeRoot()
        dashboard.current_nav_page = initial_page
        dashboard._shown_page = initial_page
        dashboard.shell_state = FakeShellState(initial_page)
        dashboard.page_frames = {
            page: FakeWidget()
            for page in ("overview", "sessions", "usage_trends", "recommendations", "tools")
        }
        dashboard.built_pages = set(dashboard.page_frames)
        dashboard._building_pages = set()
        dashboard._page_build_errors = {}
        dashboard._heavy_page_prune_job = None
        dashboard._heavy_page_destroy_count = 0
        dashboard._recent_heavy_page = None
        dashboard._last_layout_signature = None
        dashboard._dirty_pages = set()
        dashboard.nav_buttons = {}
        dashboard.session_search_var = FakeVar("needle")
        dashboard._render_visible_page = Mock()
        return dashboard

    def test_heavy_page_sequences_keep_only_current_and_recent_page(self):
        cases = (
            (("sessions", "usage_trends"), {"overview", "sessions", "usage_trends", "tools"}, 1),
            (("sessions", "usage_trends", "recommendations"), {"overview", "usage_trends", "recommendations", "tools"}, 1),
            (("sessions", "usage_trends", "recommendations", "sessions"), {"overview", "sessions", "recommendations", "tools"}, 1),
            (("sessions", "overview"), {"overview", "sessions", "tools"}, 2),
            (("usage_trends", "tools"), {"overview", "usage_trends", "tools"}, 2),
        )
        for sequence, expected, destroy_count in cases:
            with self.subTest(sequence=sequence):
                dashboard = self._heavy_navigation_dashboard(sequence[0])
                for page in sequence[1:]:
                    Dashboard.show_page(dashboard, page)
                dashboard.root.run_after_callbacks()

                self.assertEqual(dashboard.built_pages, expected)
                self.assertIn(dashboard.current_nav_page, dashboard.built_pages)
                self.assertEqual(dashboard._heavy_page_destroy_count, destroy_count)
                self.assertEqual(dashboard._recent_heavy_page, sequence[-2] if sequence[-2] in main_module.HEAVY_PAGES else None)

    def test_quick_heavy_page_round_trip_cancels_old_prune_and_keeps_both_pages(self):
        dashboard = self._heavy_navigation_dashboard("sessions")

        Dashboard.show_page(dashboard, "usage_trends")
        Dashboard.show_page(dashboard, "sessions")
        dashboard.root.run_after_callbacks()

        self.assertEqual(dashboard.built_pages & main_module.HEAVY_PAGES, {"sessions", "usage_trends"})
        self.assertEqual(dashboard._heavy_page_destroy_count, 1)
        self.assertIn("after-0", dashboard.root.cancelled)
        self.assertEqual(dashboard.current_nav_page, "sessions")

    def test_rebuilt_heavy_page_preserves_state_without_product_query(self):
        dashboard = self._heavy_navigation_dashboard("sessions")
        dashboard.page_host = FakeWidget()
        dashboard.view_model = QueryBomb()
        dashboard.quota_provider = QueryBomb()
        dashboard.page_builders = {
            "sessions": lambda _parent: setattr(
                dashboard, "rebuilt_search_text", dashboard._session_search_text,
            ),
        }
        dashboard.language = "en"
        dashboard._apply_deferred_page_language = Mock()
        dashboard._apply_responsive_layout = Mock()

        Dashboard.show_page(dashboard, "usage_trends")
        Dashboard.show_page(dashboard, "recommendations")
        dashboard.root.run_after_callbacks()
        self.assertNotIn("sessions", dashboard.built_pages)

        with patch.object(main_module.ctk, "CTkFrame", FakeWidget), patch.object(
            main_module.ctk, "CTkLabel", FakeWidget,
        ):
            Dashboard.show_page(dashboard, "sessions")
            dashboard.root.idle_callbacks.pop()()

        self.assertEqual(dashboard.rebuilt_search_text, "needle")
        self.assertIn("sessions", dashboard.built_pages)

    def _auto_refresh_dashboard(self, *, settings_built=False):
        dashboard = object.__new__(Dashboard)
        dashboard.auto_refresh_var = FakeVar(True)
        dashboard.auto_refresh = FakeAutoRefresh()
        dashboard.auto_switch = FakeWidget()
        dashboard.language = "en"
        dashboard.snapshot = None
        dashboard.tray = Mock()
        dashboard.page_frames = {"overview": FakeWidget()}
        dashboard.built_pages = {"overview"}
        if settings_built:
            dashboard.page_frames["settings"] = FakeWidget()
            dashboard.built_pages.add("settings")
            dashboard.settings_auto_switch = FakeWidget()
        return dashboard

    def test_auto_refresh_is_safe_when_settings_has_not_been_built(self):
        dashboard = self._auto_refresh_dashboard()
        self.assertFalse(hasattr(dashboard, "settings_auto_switch"))

        with patch.object(main_module, "save_auto_refresh_enabled") as save:
            Dashboard._toggle_auto_refresh(dashboard)

        self.assertEqual(dashboard.auto_refresh.enabled, [True])
        save.assert_called_once_with(True, main_module.UI_SETTINGS_PATH)
        self.assertEqual(dashboard.auto_switch.configure_calls[0]["text"], "Auto Refresh: On (60s)")
        dashboard.tray.update.assert_called_once_with(language="en", auto_refresh_enabled=True)

    def test_tray_auto_refresh_is_safe_when_settings_has_not_been_built(self):
        dashboard = self._auto_refresh_dashboard()
        dashboard.auto_refresh_var.set(False)
        self.assertFalse(hasattr(dashboard, "settings_auto_switch"))

        with patch.object(main_module, "save_auto_refresh_enabled"):
            Dashboard._toggle_auto_refresh_from_tray(dashboard)

        self.assertTrue(dashboard.auto_refresh_var.get())
        self.assertEqual(dashboard.auto_refresh.enabled, [True])
        dashboard.tray.update.assert_called_once_with(language="en", auto_refresh_enabled=True)

    def test_auto_refresh_syncs_existing_settings_switch_and_deferred_settings_language(self):
        dashboard = self._auto_refresh_dashboard(settings_built=True)
        with patch.object(main_module, "save_auto_refresh_enabled"):
            Dashboard._toggle_auto_refresh(dashboard)
        self.assertEqual(
            dashboard.settings_auto_switch.configure_calls[0]["text"], "Enabled",
        )

        dashboard.settings_language_menu = FakeWidget()
        dashboard.settings_labels = {
            name: FakeWidget()
            for name in (
                "language", "startup_mode", "widget_mode", "auto_refresh",
                "exit_behavior", "widget_idle_opacity", "start_with_windows",
                "refresh_interval", "stale_status", "tray", "taskbar",
                "always_on_top", "remember_position", "privacy", "version", "updates",
            )
        }
        dashboard.settings_startup_var = FakeVar(False)
        dashboard.settings_startup_switch = FakeWidget()
        dashboard.settings_note_var = FakeVar()
        dashboard.settings_opacity_var = FakeVar(0.7)
        dashboard.settings_opacity_value = FakeWidget()
        dashboard.settings_group_titles = {}
        dashboard.settings_refresh_interval_value = FakeWidget()
        dashboard.settings_stale_value = FakeWidget()
        dashboard.settings_tray_value = FakeWidget()
        dashboard.settings_taskbar_value = FakeWidget()
        dashboard.settings_topmost_value = FakeWidget()
        dashboard.settings_position_value = FakeWidget()
        dashboard.settings_privacy_button = FakeWidget()
        dashboard.settings_version_value = FakeWidget()
        dashboard.settings_update_button = FakeWidget()
        dashboard._configure_settings_menus = Mock()
        Dashboard._apply_deferred_page_language(dashboard, "settings", "en")

        self.assertEqual(
            dashboard.settings_auto_switch.configure_calls[-1]["text"], "Enabled",
        )

    def test_auto_refresh_tolerates_destroyed_settings_widget_reference(self):
        dashboard = self._auto_refresh_dashboard()
        dashboard.built_pages.add("settings")
        self.assertFalse(hasattr(dashboard, "settings_auto_switch"))

        with patch.object(main_module, "save_auto_refresh_enabled"):
            Dashboard._toggle_auto_refresh(dashboard)

        self.assertEqual(dashboard.auto_refresh.enabled, [True])


if __name__ == "__main__":
    unittest.main()
