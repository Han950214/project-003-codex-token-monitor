from __future__ import annotations

import unittest

from scripts.usage_summary_performance import run_benchmark


class UsageSummaryPerformanceHarnessTests(unittest.TestCase):
    def test_small_synthetic_run_verifies_all_three_rankings(self):
        result = run_benchmark(rows=200, threads=20)

        self.assertEqual(result["rows_in_30d"], 200)
        self.assertEqual(result["thread_count"], 20)
        self.assertEqual(result["top5_threads_verified"], True)
        self.assertEqual(result["top5_responses_verified"], True)
        self.assertEqual(result["top3_low_cache_verified"], True)
        self.assertEqual(result["stable_sort_verified"], True)
        self.assertEqual(result["select_star_found"], False)
        self.assertEqual(result["unbounded_fetchall_found"], False)
        self.assertLessEqual(len(result["top_threads"]), 5)
        self.assertLessEqual(len(result["top_responses"]), 5)
        self.assertLessEqual(len(result["low_cache_threads"]), 3)


if __name__ == "__main__":
    unittest.main()
