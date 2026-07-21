import unittest
from collections import Counter

from scripts.gui_real_profile import RealDataProfiler


class FakeFrame:
    def __init__(self):
        self.callbacks = []
        self.destroyed = False

    def bind(self, event, callback, add=None):
        self.callbacks.append((event, callback, add))

    def emit_configure(self):
        for event, callback, _add in self.callbacks:
            if event == "<Configure>":
                callback(None)

    def destroy(self):
        self.destroyed = True


class FakeDashboard:
    def __init__(self):
        self.page_frames = {}
        self.current_nav_page = "overview"

    def _create_page_frame(self, page):
        frame = FakeFrame()
        self.page_frames[page] = frame
        return frame


class RealDataProfilerTests(unittest.TestCase):
    def test_new_lazy_page_frames_are_bound_once_and_rebuilds_replace_old_sources(self):
        profiler = object.__new__(RealDataProfiler)
        profiler.hidden_page_configures = Counter()
        profiler.page_configure_bind_counts = Counter()
        profiler.page_configure_event_counts = Counter()
        profiler._instrument_page_frame_creation(FakeDashboard)
        dashboard = FakeDashboard()
        profiler.dashboard = dashboard

        frames = {
            page: dashboard._create_page_frame(page)
            for page in ("overview", "sessions", "usage_trends", "settings")
        }
        profiler._bind_page_configures()
        self.assertEqual(
            [len(frame.callbacks) for frame in frames.values()], [1, 1, 1, 1],
        )
        self.assertEqual(
            profiler.page_configure_bind_counts,
            Counter({
                "overview": 1,
                "sessions": 1,
                "usage_trends": 1,
                "settings": 1,
            }),
        )

        dashboard.current_nav_page = "sessions"
        frames["overview"].emit_configure()
        dashboard.current_nav_page = "overview"
        for page in ("sessions", "usage_trends", "settings"):
            frames[page].emit_configure()
        self.assertEqual(
            profiler.hidden_page_configures,
            Counter({
                "overview": 1,
                "sessions": 1,
                "usage_trends": 1,
                "settings": 1,
            }),
        )

        old_sessions = frames["sessions"]
        dashboard.page_frames.pop("sessions")
        old_sessions.destroy()
        old_sessions.emit_configure()
        self.assertEqual(profiler.hidden_page_configures["sessions"], 1)

        rebuilt_sessions = dashboard._create_page_frame("sessions")
        profiler._bind_page_configures()
        self.assertEqual(len(rebuilt_sessions.callbacks), 1)
        rebuilt_sessions.emit_configure()
        self.assertEqual(profiler.hidden_page_configures["sessions"], 2)
        self.assertEqual(profiler.page_configure_bind_counts["sessions"], 2)


if __name__ == "__main__":
    unittest.main()
