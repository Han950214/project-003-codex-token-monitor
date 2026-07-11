import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.codex_rollout import CodexRolloutReader, TokenUsage, _event_time, configured_sessions_dir


def event(kind, turn=None, last=None, total=None, timestamp=1, duration_ms=None):
    payload = {"type": kind}
    if turn:
        payload["turn_id"] = turn
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if last is not None:
        payload["info"] = {"last_token_usage": last, "total_token_usage": total}
    return {"type": "event_msg", "payload": payload, "timestamp": timestamp, "thread_id": "thread-12345678"}


def usage(i, c, o, r):
    return {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o, "reasoning_output_tokens": r, "total_tokens": i + o}


class RolloutReaderTests(unittest.TestCase):
    def write(self, directory, events, name="rollout-a.jsonl"):
        path = Path(directory) / name
        path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
        return path

    def test_token_usage_rejects_bool_and_invariant_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            events = [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)), event("task_started", "t"), event("token_count", "t", usage(True, 0, 1, 0), usage(True, 0, 1, 0)), event("task_complete", "t")]
            self.write(directory, events)
            result = CodexRolloutReader().refresh(Path(directory))
        self.assertEqual(result.instruction.rejected_events, 1)
        self.assertFalse(result.instruction.exact)

    def test_exact_deduplicates_and_aggregates_multiple_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = usage(10, 4, 2, 1), usage(20, 8, 4, 2)
            events = [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)), event("task_started", "t"), event("token_count", "t", first, first), event("token_count", "t", first, first), event("token_count", "t", second, usage(30, 12, 6, 3)), event("task_complete", "t", duration_ms=2000)]
            self.write(directory, events)
            result = CodexRolloutReader().refresh(Path(directory))
        instruction = result.instruction
        self.assertTrue(instruction.exact)
        self.assertEqual(instruction.model_calls, 2)
        self.assertEqual(instruction.usage, TokenUsage(30, 12, 6, 3, 36))
        self.assertEqual(instruction.duplicate_snapshots, 1)
        self.assertEqual(instruction.duration_ms, 2000)

    def test_unreconciled_and_epoch_do_not_make_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            events = [event("token_count", last=usage(0, 0, 0, 0), total=usage(10, 0, 1, 0)), event("task_started", "t"), event("token_count", "t", usage(2, 1, 1, 0), usage(11, 0, 2, 0)), event("token_count", "t", usage(1, 0, 1, 0), usage(1, 0, 1, 0)), event("task_complete", "t")]
            self.write(directory, events)
            result = CodexRolloutReader().refresh(Path(directory))
        self.assertFalse(result.instruction.exact)
        self.assertGreaterEqual(result.instruction.unreconciled_events, 1)

    def test_in_progress_and_environment_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"CODEX_SESSIONS_DIR": directory}):
            self.write(directory, [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)), event("task_started", "t"), event("token_count", "t", usage(3, 1, 2, 1), usage(3, 1, 2, 1))])
            result = CodexRolloutReader().refresh()
            self.assertEqual(configured_sessions_dir(), Path(directory))
        self.assertTrue(result.instruction.in_progress)
        self.assertEqual(result.thread_suffix, "12345678")

    def test_partial_tail_is_ignored_until_next_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)), event("task_started", "t")])
            with path.open("a", encoding="utf-8") as handle:
                handle.write("\n{\"bad\"")
            result = CodexRolloutReader().refresh(Path(directory))
        self.assertTrue(result.available)

    def test_iso_z_and_offset_describe_the_same_utc_time(self):
        zulu = _event_time({"timestamp": "2026-07-11T12:48:34.418Z"}, {})
        offset = _event_time({"timestamp": "2026-07-11T20:48:34.418+08:00"}, {})
        self.assertEqual(zulu, offset)
        self.assertEqual(zulu.tzinfo.utcoffset(zulu).total_seconds(), 0)

    def test_result_retains_latest_cumulative_usage_and_event_time(self):
        with tempfile.TemporaryDirectory() as directory:
            events = [event("token_count", last=usage(0, 0, 0, 0), total=usage(3, 1, 1, 0), timestamp="2026-07-11T12:00:00Z"), event("task_started", "t"), event("token_count", "t", usage(2, 1, 1, 0), usage(5, 2, 2, 0), timestamp="2026-07-11T12:01:00Z")]
            self.write(directory, events)
            result = CodexRolloutReader().refresh(Path(directory))
        self.assertEqual(result.thread_cumulative_usage, TokenUsage(5, 2, 2, 0, 7))
        self.assertIsNotNone(result.observed_at)
        self.assertIsNotNone(result.refreshed_at)


if __name__ == "__main__":
    unittest.main()
