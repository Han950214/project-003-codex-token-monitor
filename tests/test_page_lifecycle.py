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


class FakeVar:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value


class FakeRoot:
    def __init__(self):
        self.idle_callbacks = []

    def after_idle(self, callback):
        self.idle_callbacks.append(callback)
        return f"idle-{len(self.idle_callbacks)}"


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


if __name__ == "__main__":
    unittest.main()
