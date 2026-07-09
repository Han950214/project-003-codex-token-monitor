import unittest

from app.metrics import (
    PricingConfig,
    RunUsage,
    average_hit,
    budget_remaining,
    cached_input_tokens,
    context_usage,
    current_cost,
    current_hit,
    current_tokens,
    session_cost,
    session_tokens,
    summarize_runs,
    uncached_input_tokens,
)
from app.models import AgentRun


class MetricsTests(unittest.TestCase):
    def test_current_hit_uses_observed_cached_tokens_first(self):
        usage = RunUsage(
            input_tokens=1000,
            output_tokens=200,
            stable_prefix_tokens=300,
            observed_cached_input_tokens=700,
        )
        self.assertEqual(current_hit(usage), 0.7)

    def test_current_hit_falls_back_to_stable_prefix(self):
        usage = RunUsage(input_tokens=1000, output_tokens=200, stable_prefix_tokens=250)
        self.assertEqual(current_hit(usage), 0.25)

    def test_current_hit_clamps_stable_prefix_to_input_tokens(self):
        usage = RunUsage(input_tokens=1000, output_tokens=200, stable_prefix_tokens=1200)
        self.assertEqual(current_hit(usage), 1.0)

    def test_current_hit_clamps_observed_cached_tokens_to_input_tokens(self):
        usage = RunUsage(
            input_tokens=1000,
            output_tokens=200,
            stable_prefix_tokens=250,
            observed_cached_input_tokens=1400,
        )
        self.assertEqual(current_hit(usage), 1.0)

    def test_average_hit_is_weighted_by_input_tokens(self):
        usages = [
            RunUsage(input_tokens=1000, output_tokens=100, stable_prefix_tokens=500),
            RunUsage(input_tokens=3000, output_tokens=100, stable_prefix_tokens=1500),
        ]
        self.assertEqual(average_hit(usages), 0.5)

    def test_token_totals(self):
        usage = RunUsage(input_tokens=100, output_tokens=50, optional_log_tokens=20)
        self.assertEqual(current_tokens(usage), 170)
        self.assertEqual(session_tokens([usage, usage]), 340)

    def test_cached_and_uncached_tokens_are_clamped(self):
        usage = RunUsage(input_tokens=100, output_tokens=10, stable_prefix_tokens=150)
        self.assertEqual(cached_input_tokens(usage), 100)
        self.assertEqual(uncached_input_tokens(usage), 0)

    def test_current_and_session_cost(self):
        pricing = PricingConfig(
            input_token_price=0.01,
            cached_input_token_price=0.001,
            output_token_price=0.02,
            unit_tokens=1000,
            configured_budget=1,
        )
        usage = RunUsage(input_tokens=1000, output_tokens=500, stable_prefix_tokens=400)
        self.assertAlmostEqual(current_cost(usage, pricing), 0.0164)
        self.assertAlmostEqual(session_cost([usage, usage], pricing), 0.0328)
        self.assertAlmostEqual(budget_remaining(1, session_cost([usage], pricing)), 0.9836)

    def test_context_usage_handles_zero_window(self):
        self.assertEqual(context_usage(100, 0), 0.0)
        self.assertEqual(context_usage(100, 1000), 0.1)

    def test_summarize_runs_aggregates_session_values(self):
        pricing = PricingConfig(
            input_token_price=0.01,
            cached_input_token_price=0.001,
            output_token_price=0.02,
            unit_tokens=1000,
            configured_budget=1,
            configured_context_window=1000,
        )
        runs = [
            AgentRun(
                run_id="run-1",
                session_id="session-1",
                project="project",
                title="First",
                started_at="2026-07-08T09:00:00",
                ended_at="2026-07-08T09:01:00",
                elapsed_seconds=60,
                model="demo",
                mode="manual",
                prompt_summary="p1",
                output_summary="o1",
                note="n1",
                input_tokens=100,
                output_tokens=50,
                cached_tokens=20,
                total_tokens=150,
                estimated_cost=0.001,
                cache_hit=0.2,
            ),
            AgentRun(
                run_id="run-2",
                session_id="session-1",
                project="project",
                title="Second",
                started_at="2026-07-08T09:02:00",
                ended_at="2026-07-08T09:03:00",
                elapsed_seconds=60,
                model="demo",
                mode="manual",
                prompt_summary="p2",
                output_summary="o2",
                note="n2",
                input_tokens=200,
                output_tokens=100,
                cached_tokens=300,
                total_tokens=300,
                estimated_cost=0.002,
                cache_hit=1.0,
            ),
        ]
        summary = summarize_runs(runs, pricing)
        self.assertEqual(summary.rounds, 2)
        self.assertEqual(summary.session_tokens, 450)
        self.assertEqual(summary.current_run_tokens, 300)
        self.assertEqual(summary.current_cache_hit, 1.0)
        self.assertAlmostEqual(summary.average_cache_hit, (0.2 * 100 + 1.0 * 200) / 300)
        self.assertEqual(summary.context_usage, 0.45)
        self.assertAlmostEqual(summary.budget_remaining, 0.997)


if __name__ == "__main__":
    unittest.main()
