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
    uncached_input_tokens,
)


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


if __name__ == "__main__":
    unittest.main()
