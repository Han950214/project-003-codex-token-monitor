import unittest

from app.metrics import PricingConfig, SessionSummary
from app.telemetry_bar import build_telemetry_values_from_summary


class TelemetryBarTests(unittest.TestCase):
    def test_real_session_total_has_distinct_source_label(self):
        summary = SessionSummary(
            rounds=1,
            session_tokens=999,
            current_run_tokens=150,
            current_cost=0.01,
            session_cost=0.01,
            current_cache_hit=0.2,
            average_cache_hit=0.2,
            context_usage=0.1,
            budget_remaining=1.0,
            total_tokens_source="codex_state_sqlite",
        )
        values = build_telemetry_values_from_summary(summary, PricingConfig(1, 0.1, 2))
        self.assertEqual(values[2][1], "999 codex_state_sqlite / real total")
        self.assertIn("local estimate, not real Codex cache", values[0][1])
        self.assertIn("local estimate", values[3][1])
        self.assertIn("local estimate, not billing", values[4][1])
        self.assertNotIn("real total", values[0][1])
        self.assertNotIn("real total", values[4][1])


if __name__ == "__main__":
    unittest.main()
