from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.advisor import (
    HISTORY_MIN_DISTINCT_OBSERVATIONS,
    HISTORY_MIN_VALID_SAMPLES,
    NEW_THREAD_TURN_COUNT,
    AdvisorInput,
    Recommendation,
    build_history_evidence,
    evaluate_advice,
)


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)


def sample(
    index: int,
    value: float,
    *,
    thread: str = "thread-a",
    stale: bool = False,
    available: bool = True,
    metric: str = "turn_count",
):
    return SimpleNamespace(
        thread_safe_id=thread,
        observed_at=NOW + timedelta(minutes=index),
        stale=stale,
        available=available,
        **{metric: value},
    )


def advisor_input(**changes) -> AdvisorInput:
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
        thread_safe_id="thread-a",
        source_observed_at=NOW - timedelta(seconds=5),
    )
    return replace(base, **changes)


class AdvisorHistoryTests(unittest.TestCase):
    def test_constants_define_the_required_minimum(self):
        self.assertEqual(HISTORY_MIN_VALID_SAMPLES, 5)
        self.assertEqual(HISTORY_MIN_DISTINCT_OBSERVATIONS, 3)

    def test_recommendation_tail_fields_are_backward_compatible(self):
        recommendation = Recommendation(
            "normal", "normal", "title", "body", "action", (), NOW,
        )
        self.assertEqual(recommendation.source, "current_snapshot")
        self.assertFalse(recommendation.derived)
        self.assertIsNone(recommendation.history_evidence)
        self.assertIsNone(recommendation.source_observed_at)

    def test_insufficient_or_fewer_than_three_times_returns_none(self):
        too_few = [sample(index, index) for index in range(4)]
        self.assertIsNone(build_history_evidence(
            too_few, thread_safe_id="thread-a", metric="turn_count", current_value=10,
        ))
        repeated = [sample(0, index) for index in range(5)]
        self.assertIsNone(build_history_evidence(
            repeated, thread_safe_id="thread-a", metric="turn_count", current_value=10,
        ))

    def test_same_thread_fresh_available_samples_only(self):
        samples = [sample(index, 10 + index) for index in range(5)]
        samples.extend((sample(6, 999, thread="thread-b"), sample(7, 999, stale=True)))
        evidence = build_history_evidence(
            samples, thread_safe_id="thread-a", metric="turn_count", current_value=30,
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.sample_count, 5)
        self.assertEqual(evidence.direction, "up")
        self.assertEqual(evidence.maximum_value, 14)

    def test_history_store_field_names_are_supported(self):
        samples = [
            SimpleNamespace(
                thread_safe_id="thread-a",
                source_observed_at=NOW + timedelta(minutes=index),
                source_available=True,
                token_stale=False,
                source_status="exact",
                input_tokens=100,
                cached_tokens=20 + index,
            )
            for index in range(5)
        ]
        evidence = build_history_evidence(
            samples,
            thread_safe_id="thread-a",
            metric="cache_hit_percent_derived",
            current_value=40,
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.direction, "up")

    def test_up_down_and_flat_are_deterministic(self):
        samples = [sample(index, value) for index, value in enumerate((100, 101, 99, 100, 100))]
        directions = [
            build_history_evidence(
                samples, thread_safe_id="thread-a", metric="turn_count", current_value=value,
            ).direction
            for value in (120, 80, 103)
        ]
        self.assertEqual(directions, ["up", "down", "flat"])

    def test_metric_mismatch_and_bad_duck_type_fail_closed(self):
        samples = [sample(index, index, metric="instruction_total_tokens") for index in range(5)]
        self.assertIsNone(build_history_evidence(
            samples, thread_safe_id="thread-a", metric="turn_count", current_value=10,
        ))
        self.assertIsNone(build_history_evidence(
            [object()] * 5, thread_safe_id="thread-a", metric="turn_count", current_value=10,
        ))

    def test_history_is_auxiliary_and_does_not_change_v1_priority_or_severity(self):
        history = tuple(sample(index, 10 + index) for index in range(5))
        without = evaluate_advice(advisor_input(turn_count=NEW_THREAD_TURN_COUNT))
        with_history = evaluate_advice(advisor_input(
            turn_count=NEW_THREAD_TURN_COUNT,
            history_samples=history,
        ))
        self.assertEqual(
            [(item.code, item.severity) for item in with_history.recommendations],
            [(item.code, item.severity) for item in without.recommendations],
        )
        self.assertIsNotNone(with_history.primary.history_evidence)
        self.assertEqual(with_history.primary.source_observed_at, NOW - timedelta(seconds=5))

    def test_unavailable_and_stale_rules_never_infer_history(self):
        history = tuple(sample(index, 10 + index) for index in range(5))
        unavailable = evaluate_advice(advisor_input(
            data_available=False, turn_count=NEW_THREAD_TURN_COUNT,
            history_samples=history,
        ))
        stale = evaluate_advice(advisor_input(
            data_age_seconds=181, turn_count=NEW_THREAD_TURN_COUNT,
            history_samples=history,
        ))
        self.assertTrue(all(
            item.history_evidence is None for item in unavailable.recommendations
        ))
        self.assertTrue(all(
            item.history_evidence is None for item in stale.recommendations
        ))

    def test_quota_risk_uses_the_triggering_quota_observation_time(self):
        five_observed = NOW - timedelta(seconds=20)
        weekly_observed = NOW - timedelta(seconds=10)

        result = evaluate_advice(advisor_input(
            five_hour_remaining_percent=4.0,
            weekly_remaining_percent=40.0,
            five_hour_observed_at=five_observed,
            weekly_observed_at=weekly_observed,
        ))

        quota_risk = next(item for item in result.recommendations if item.code == "quota_risk")
        self.assertEqual(quota_risk.source_observed_at, five_observed)


if __name__ == "__main__":
    unittest.main()
