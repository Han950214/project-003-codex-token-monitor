import inspect
import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from math import inf, nan
from unittest.mock import Mock

from app.quota import CodexQuotaSnapshot, QuotaKind, QuotaWindow
from app.quota_provider import (
    CodexAppServerQuotaProvider,
    SOURCE_LABEL,
    snapshot_from_app_server,
)


NOW = datetime(2026, 7, 12, 8, 30, tzinfo=timezone.utc)


def window(kind=QuotaKind.FIVE_HOUR, used=48, remaining=None, reset=None):
    return QuotaWindow(
        kind,
        used,
        remaining,
        reset or NOW + timedelta(hours=1),
        NOW,
        "test_source",
        True,
    )


def app_server_payload(primary_used=48, secondary_used=66):
    return {
        "rateLimits": {
            "primary": {
                "usedPercent": primary_used,
                "resetsAt": int((NOW + timedelta(hours=1)).timestamp()),
                "windowDurationMins": 300,
            },
            "secondary": {
                "usedPercent": secondary_used,
                "resetsAt": int((NOW + timedelta(days=3)).timestamp()),
                "windowDurationMins": 10080,
            },
        }
    }


class QuotaModelTests(unittest.TestCase):
    def test_used_percent_derives_remaining_percent(self):
        item = window(used=48)
        self.assertEqual((item.used_percent, item.remaining_percent), (48.0, 52.0))

    def test_remaining_percent_derives_used_percent(self):
        item = window(used=None, remaining=52)
        self.assertEqual((item.used_percent, item.remaining_percent), (48.0, 52.0))

    def test_consistent_used_and_remaining_are_available(self):
        self.assertTrue(window(used=48.2, remaining=51.8).available)

    def test_inconsistent_used_and_remaining_are_rejected(self):
        item = window(used=48, remaining=40)
        self.assertFalse(item.available)
        self.assertEqual(item.error_code, "percentage_mismatch")
        self.assertIsNone(item.used_percent)

    def test_percentages_are_clamped_to_contract_range(self):
        self.assertEqual(window(used=-3).used_percent, 0)
        self.assertEqual(window(used=103).used_percent, 100)

    def test_nan_and_infinity_are_rejected(self):
        for value in (nan, inf, -inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                window(used=value)

    def test_unknown_values_stay_unknown_and_unavailable(self):
        item = window(used=None, remaining=None)
        self.assertFalse(item.available)
        self.assertIsNone(item.used_percent)

    def test_reset_at_requires_timezone(self):
        with self.assertRaises(ValueError):
            QuotaWindow(QuotaKind.FIVE_HOUR, 1, 99, datetime(2026, 1, 1), NOW, "test", True)

    def test_reset_at_preserves_timezone(self):
        offset = timezone(timedelta(hours=8))
        reset = datetime(2026, 7, 13, 1, 0, tzinfo=offset)
        self.assertIs(window(reset=reset).reset_at.tzinfo, offset)

    def test_duration_to_reset_uses_snapshot_observed_at(self):
        item = QuotaWindow.from_reset_duration(
            QuotaKind.FIVE_HOUR,
            used_percent=10,
            remaining_percent=None,
            reset_after=timedelta(minutes=30),
            observed_at=NOW,
            source="test",
        )
        self.assertEqual(item.reset_at, NOW + timedelta(minutes=30))

    def test_expired_reset_marks_window_stale(self):
        self.assertTrue(window(reset=NOW - timedelta(seconds=1)).stale)

    def test_snapshot_rejects_mixed_window_kinds(self):
        with self.assertRaises(ValueError):
            CodexQuotaSnapshot(
                window(QuotaKind.WEEKLY),
                window(QuotaKind.FIVE_HOUR),
                NOW,
                "normal",
            )

    def test_dto_has_no_credential_or_content_fields(self):
        names = {field.name for field in fields(QuotaWindow)} | {
            field.name for field in fields(CodexQuotaSnapshot)
        }
        forbidden = {"token", "cookie", "authorization", "preview", "prompt", "response"}
        self.assertTrue(names.isdisjoint(forbidden))


class QuotaProviderTests(unittest.TestCase):
    def test_parser_maps_five_hour_and_weekly_by_explicit_duration(self):
        snapshot = snapshot_from_app_server(app_server_payload(), NOW)
        self.assertEqual(snapshot.five_hour.used_percent, 48)
        self.assertEqual(snapshot.weekly.used_percent, 66)
        self.assertEqual(snapshot.source_status, "normal")

    def test_parser_does_not_mix_unknown_window_duration(self):
        payload = app_server_payload()
        payload["rateLimits"]["primary"]["windowDurationMins"] = 301
        snapshot = snapshot_from_app_server(payload, NOW)
        self.assertFalse(snapshot.five_hour.available)
        self.assertTrue(snapshot.weekly.available)

    def test_parser_ignores_credential_preview_and_content_keys(self):
        payload = app_server_payload()
        payload.update({"authorization": "fake", "preview": "fake", "message": "fake"})
        snapshot = snapshot_from_app_server(payload, NOW)
        self.assertEqual(snapshot.five_hour.source, SOURCE_LABEL)
        self.assertFalse(hasattr(snapshot, "authorization"))

    def test_missing_source_returns_unavailable_without_zero(self):
        snapshot = snapshot_from_app_server({}, NOW)
        self.assertEqual(snapshot.source_status, "unavailable")
        self.assertIsNone(snapshot.five_hour.used_percent)

    def test_malformed_numeric_field_returns_unavailable(self):
        payload = app_server_payload()
        payload["rateLimits"]["primary"]["usedPercent"] = "48"
        snapshot = snapshot_from_app_server(payload, NOW)
        self.assertFalse(snapshot.five_hour.available)
        self.assertEqual(snapshot.five_hour.error_code, "quota_invalid")

    def test_codex_bucket_is_preferred_over_legacy_bucket(self):
        payload = app_server_payload(1, 2)
        payload["rateLimitsByLimitId"] = {
            "codex": app_server_payload(48, 66)["rateLimits"]
        }
        snapshot = snapshot_from_app_server(payload, NOW)
        self.assertEqual((snapshot.five_hour.used_percent, snapshot.weekly.used_percent), (48, 66))

    def test_two_consecutive_snapshots_keep_window_semantics(self):
        first = snapshot_from_app_server(app_server_payload(), NOW)
        second = snapshot_from_app_server(app_server_payload(), NOW + timedelta(seconds=1))
        self.assertEqual(first.five_hour.kind, second.five_hour.kind)
        self.assertEqual(first.weekly.kind, second.weekly.kind)

    def test_refresh_failure_keeps_previous_values_but_marks_stale(self):
        provider = CodexAppServerQuotaProvider("missing")
        provider._last_success = snapshot_from_app_server(app_server_payload(), NOW)
        provider._ensure_started = Mock(side_effect=TimeoutError())
        snapshot = provider.refresh()
        self.assertTrue(snapshot.five_hour.stale)
        self.assertEqual(snapshot.five_hour.used_percent, 48)
        self.assertEqual(snapshot.source_status, "stale")

    def test_provider_source_does_not_log_raw_responses(self):
        source = inspect.getsource(CodexAppServerQuotaProvider)
        self.assertNotIn("print(", source)
        self.assertNotIn("auth.json", source)


if __name__ == "__main__":
    unittest.main()
