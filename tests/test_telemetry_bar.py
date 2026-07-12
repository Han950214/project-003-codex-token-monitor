import unittest
from app.codex_rollout import RolloutUsageResult
from app.dashboard import DashboardSnapshot
from app.metrics import PricingConfig, SessionSummary
from app.telemetry_bar import (
    TELEMETRY_FIELD_LABELS,
    build_telemetry_values,
    build_telemetry_values_from_summary,
    telemetry_field_labels,
)
from app.ui_presenter import present_dashboard


class TelemetryBarTests(unittest.TestCase):
    def test_field_count_and_order_are_fixed(self):
        self.assertEqual(
            TELEMETRY_FIELD_LABELS,
            (
                "Codex Token Monitor",
                "Current Total",
                "Cache Hit",
                "Session Total",
                "Data Status",
                "Auto Refresh",
            ),
        )

    def test_no_data_telemetry_uses_dashes_not_zero(self):
        summary = SessionSummary(0, 0, 0, 0, 0, 0, 0, 0, 0)
        presentation = present_dashboard(DashboardSnapshot([], summary, RolloutUsageResult(None, None, None, False), None, False), False)
        values = build_telemetry_values(presentation, "zh-CN")
        self.assertEqual(tuple(label for label, _ in values), telemetry_field_labels("zh-CN"))
        self.assertEqual(values[1][1], "—")
        self.assertEqual(values[2][1], "—")
        self.assertEqual(values[3][1], "—")
        self.assertEqual(values[4][1], "暂不可用")
        self.assertEqual(values[5][1], "关闭（60 秒）")

    def test_chinese_and_english_labels_preserve_six_field_order(self):
        self.assertEqual(
            telemetry_field_labels("zh-CN"),
            ("Codex Token Monitor", "当前总计", "缓存命中率", "会话总计", "数据状态", "自动刷新"),
        )
        self.assertEqual(telemetry_field_labels("en"), TELEMETRY_FIELD_LABELS)

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
