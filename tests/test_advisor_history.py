from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.advisor import (
    HISTORY_MIN_DISTINCT_OBSERVATIONS,
    HISTORY_MIN_VALID_SAMPLES,
    NEW_THREAD_TURN_COUNT,
    OPTIMIZE_CACHE_HIT_PERCENT,
    OPTIMIZE_INPUT_TOKENS,
    PRIORITY,
    QUOTA_RISK_REMAINING_PERCENT,
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
    observed_at: datetime | None = None,
):
    return SimpleNamespace(
        thread_safe_id=thread,
        source_type="dashboard",
        source_observed_at=(
            observed_at
            if observed_at is not None
            else NOW - timedelta(minutes=10 - index)
        ),
        token_stale=stale,
        source_available=available,
        source_status="exact" if available else "unavailable",
        **{metric: value},
    )


def quota_sample(
    index: int,
    value: float,
    *,
    metric: str = "five_hour_remaining_percent",
    source_type: str = "global_quota",
    stale: bool = False,
    available: bool = True,
    observed_at: datetime | None = None,
):
    prefix = "five_hour" if metric.startswith("five_hour") else "weekly"
    return SimpleNamespace(
        thread_safe_id=None,
        source_type=source_type,
        quota_source_status="normal" if available else "unavailable",
        **{
            f"{prefix}_observed_at": (
                observed_at
                if observed_at is not None
                else NOW - timedelta(minutes=10 - index)
            ),
            f"{prefix}_available": available,
            f"{prefix}_stale": stale,
            metric: value,
        },
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
        source_observed_at=NOW,
        five_hour_observed_at=NOW,
        weekly_observed_at=NOW,
    )
    return replace(base, **changes)


class AdvisorHistoryTests(unittest.TestCase):
    def test_constants_define_the_required_minimum(self):
        self.assertEqual(HISTORY_MIN_VALID_SAMPLES, 5)
        self.assertEqual(HISTORY_MIN_DISTINCT_OBSERVATIONS, 3)
        self.assertEqual(QUOTA_RISK_REMAINING_PERCENT, 15.0)
        self.assertEqual(NEW_THREAD_TURN_COUNT, 30)
        self.assertEqual(OPTIMIZE_INPUT_TOKENS, 60_000)
        self.assertEqual(OPTIMIZE_CACHE_HIT_PERCENT, 20.0)
        self.assertEqual(PRIORITY, {
            "data_unavailable": 0,
            "quota_risk": 1,
            "new_thread": 2,
            "optimize": 3,
            "normal": 4,
        })

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
            current_observed_at=NOW,
        ))
        repeated = [sample(0, index) for index in range(5)]
        self.assertIsNone(build_history_evidence(
            repeated, thread_safe_id="thread-a", metric="turn_count", current_value=10,
            current_observed_at=NOW,
        ))

    def test_same_thread_fresh_available_samples_only(self):
        samples = [sample(index, 10 + index) for index in range(5)]
        samples.extend((sample(6, 999, thread="thread-b"), sample(7, 999, stale=True)))
        evidence = build_history_evidence(
            samples, thread_safe_id="thread-a", metric="turn_count", current_value=30,
            current_observed_at=NOW,
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.sample_count, 5)
        self.assertEqual(evidence.direction, "up")
        self.assertEqual(evidence.maximum_value, 14)

    def test_history_store_field_names_are_supported(self):
        samples = [
            SimpleNamespace(
                thread_safe_id="thread-a",
                source_observed_at=NOW - timedelta(minutes=10 - index),
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
            current_observed_at=NOW,
        )
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.direction, "up")

    def test_up_down_and_flat_are_deterministic(self):
        samples = [sample(index, value) for index, value in enumerate((100, 101, 99, 100, 100))]
        directions = [
            build_history_evidence(
                samples, thread_safe_id="thread-a", metric="turn_count", current_value=value,
                current_observed_at=NOW,
            ).direction
            for value in (120, 80, 103)
        ]
        self.assertEqual(directions, ["up", "down", "flat"])

    def test_metric_mismatch_and_bad_duck_type_fail_closed(self):
        samples = [sample(index, index, metric="instruction_total_tokens") for index in range(5)]
        self.assertIsNone(build_history_evidence(
            samples, thread_safe_id="thread-a", metric="turn_count", current_value=10,
            current_observed_at=NOW,
        ))
        self.assertIsNone(build_history_evidence(
            [object()] * 5, thread_safe_id="thread-a", metric="turn_count", current_value=10,
            current_observed_at=NOW,
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
        self.assertEqual(with_history.primary.source_observed_at, NOW)

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

    def test_current_token_observation_is_not_part_of_its_prior_baseline(self):
        current = sample(0, NEW_THREAD_TURN_COUNT, observed_at=NOW)
        four_prior = tuple(sample(index, 10 + index) for index in range(4))
        five_prior = tuple(sample(index, 10 + index) for index in range(5))

        insufficient = evaluate_advice(advisor_input(
            turn_count=NEW_THREAD_TURN_COUNT,
            history_samples=(*four_prior, current),
        ))
        sufficient = evaluate_advice(advisor_input(
            turn_count=NEW_THREAD_TURN_COUNT,
            history_samples=(*five_prior, current),
        ))

        insufficient_item = next(
            item for item in insufficient.recommendations if item.code == "new_thread"
        )
        sufficient_item = next(
            item for item in sufficient.recommendations if item.code == "new_thread"
        )
        self.assertIsNone(insufficient_item.history_evidence)
        self.assertEqual(sufficient_item.history_evidence.sample_count, 5)
        self.assertLess(sufficient_item.history_evidence.range_ended_at, NOW)

    def test_quota_risk_uses_global_history_without_a_thread_id(self):
        history = tuple(quota_sample(index, 30 + index) for index in range(5))

        result = evaluate_advice(advisor_input(
            thread_safe_id="unrelated-current-thread",
            five_hour_remaining_percent=4.0,
            quota_history_samples=history,
        ))

        quota_risk = next(item for item in result.recommendations if item.code == "quota_risk")
        self.assertIsNotNone(quota_risk.history_evidence)
        self.assertEqual(quota_risk.history_evidence.sample_count, 5)
        self.assertEqual(quota_risk.history_evidence.source, "global_quota_history")

    def test_weekly_quota_history_uses_its_own_reliable_observation_time(self):
        history = tuple(
            quota_sample(
                index,
                30 + index,
                metric="weekly_remaining_percent",
            )
            for index in range(5)
        )

        result = evaluate_advice(advisor_input(
            five_hour_remaining_percent=40.0,
            weekly_remaining_percent=4.0,
            weekly_observed_at=NOW,
            quota_history_samples=history,
        ))

        quota_risk = next(item for item in result.recommendations if item.code == "quota_risk")
        self.assertEqual(
            quota_risk.history_evidence.metric,
            "weekly_remaining_percent",
        )
        self.assertEqual(quota_risk.history_evidence.sample_count, 5)

    def test_quota_history_never_mixes_thread_token_samples(self):
        quota_history = tuple(quota_sample(index, 30 + index) for index in range(4))
        token_rows_with_quota_values = tuple(
            quota_sample(index, 90, source_type="dashboard") for index in range(5)
        )

        result = evaluate_advice(advisor_input(
            five_hour_remaining_percent=4.0,
            quota_history_samples=(*quota_history, *token_rows_with_quota_values),
        ))

        quota_risk = next(item for item in result.recommendations if item.code == "quota_risk")
        self.assertIsNone(quota_risk.history_evidence)

    def test_other_quota_window_changes_cannot_fill_the_five_sample_gate(self):
        history = []
        for duplicate_index, (minutes, five_value) in enumerate((
            (10, 30.0),
            (10, 30.0),
            (9, 31.0),
            (9, 31.0),
            (8, 32.0),
        )):
            item = quota_sample(
                duplicate_index,
                five_value,
                observed_at=NOW - timedelta(minutes=minutes),
            )
            item.weekly_observed_at = NOW - timedelta(minutes=10 - duplicate_index)
            item.weekly_remaining_percent = 80.0 - duplicate_index
            history.append(item)

        result = evaluate_advice(advisor_input(
            five_hour_remaining_percent=4.0,
            quota_history_samples=tuple(history),
        ))

        quota_risk = next(item for item in result.recommendations if item.code == "quota_risk")
        self.assertIsNone(quota_risk.history_evidence)

    def test_current_quota_observation_is_not_part_of_its_prior_baseline(self):
        current = quota_sample(0, 4.0, observed_at=NOW)
        four_prior = tuple(quota_sample(index, 30 + index) for index in range(4))
        five_prior = tuple(quota_sample(index, 30 + index) for index in range(5))

        insufficient = evaluate_advice(advisor_input(
            five_hour_remaining_percent=4.0,
            quota_history_samples=(*four_prior, current),
        ))
        sufficient = evaluate_advice(advisor_input(
            five_hour_remaining_percent=4.0,
            quota_history_samples=(*five_prior, current),
        ))

        insufficient_item = next(
            item for item in insufficient.recommendations if item.code == "quota_risk"
        )
        sufficient_item = next(
            item for item in sufficient.recommendations if item.code == "quota_risk"
        )
        self.assertIsNone(insufficient_item.history_evidence)
        self.assertEqual(sufficient_item.history_evidence.sample_count, 5)
        self.assertLess(sufficient_item.history_evidence.range_ended_at, NOW)

    def test_current_quota_last_seen_identity_is_excluded_even_if_value_started_earlier(self):
        earlier_duplicate = quota_sample(0, 4.0, observed_at=NOW - timedelta(minutes=20))
        earlier_duplicate.five_hour_last_seen_at = NOW - timedelta(minutes=1)
        current_state = quota_sample(0, 4.0, observed_at=NOW - timedelta(minutes=20))
        current_state.five_hour_last_seen_at = NOW
        four_prior = tuple(quota_sample(index, 30 + index) for index in range(4))

        result = evaluate_advice(advisor_input(
            five_hour_remaining_percent=4.0,
            quota_history_samples=(*four_prior, earlier_duplicate, current_state),
        ))

        quota_risk = next(item for item in result.recommendations if item.code == "quota_risk")
        self.assertIsNone(quota_risk.history_evidence)

    def test_stale_unavailable_and_unknown_time_quota_samples_are_excluded(self):
        history = (
            quota_sample(0, 30),
            quota_sample(1, 31),
            quota_sample(2, 32),
            quota_sample(3, 33, stale=True),
            quota_sample(4, 34, available=False),
            SimpleNamespace(
                source_type="global_quota",
                sampled_at=NOW - timedelta(minutes=1),
                five_hour_remaining_percent=35,
                five_hour_available=True,
                five_hour_stale=False,
                quota_source_status="normal",
            ),
        )

        result = evaluate_advice(advisor_input(
            five_hour_remaining_percent=4.0,
            quota_history_samples=history,
        ))

        quota_risk = next(item for item in result.recommendations if item.code == "quota_risk")
        self.assertIsNone(quota_risk.history_evidence)

    def test_sampled_at_never_substitutes_for_token_source_time(self):
        samples = tuple(
            SimpleNamespace(
                thread_safe_id="thread-a",
                source_type="dashboard",
                sampled_at=NOW - timedelta(minutes=10 - index),
                turn_count=10 + index,
                source_available=True,
                token_stale=False,
                source_status="exact",
            )
            for index in range(5)
        )

        evidence = build_history_evidence(
            samples,
            thread_safe_id="thread-a",
            metric="turn_count",
            current_value=NEW_THREAD_TURN_COUNT,
            current_observed_at=NOW,
        )

        self.assertIsNone(evidence)

    def test_quota_history_is_independent_of_thread_token_freshness(self):
        history = tuple(quota_sample(index, 30 + index) for index in range(5))

        result = evaluate_advice(advisor_input(
            data_available=False,
            data_age_seconds=999,
            five_hour_remaining_percent=4.0,
            quota_history_samples=history,
        ))

        quota_risk = next(item for item in result.recommendations if item.code == "quota_risk")
        self.assertIsNotNone(quota_risk.history_evidence)
        unavailable = next(
            item for item in result.recommendations if item.code == "data_unavailable"
        )
        self.assertIsNone(unavailable.history_evidence)

    def test_history_never_changes_v1_order_severity_or_actions(self):
        token_history = tuple(sample(index, 10 + index) for index in range(5))
        quota_history = tuple(quota_sample(index, 30 + index) for index in range(5))
        changes = {
            "five_hour_remaining_percent": 4.0,
            "turn_count": NEW_THREAD_TURN_COUNT,
            "instruction_input_tokens": OPTIMIZE_INPUT_TOKENS,
            "cached_input_tokens": 0,
        }

        without = evaluate_advice(advisor_input(**changes))
        with_history = evaluate_advice(advisor_input(
            **changes,
            history_samples=token_history,
            quota_history_samples=quota_history,
        ))

        def projection(result):
            return [
                (item.code, item.severity, item.primary_action)
                for item in result.recommendations
            ]

        self.assertEqual(projection(with_history), projection(without))
        self.assertEqual(
            [item.code for item in with_history.recommendations],
            ["quota_risk", "new_thread", "optimize_cache_reuse"],
        )

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
