import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app.codex_rollout import CodexRolloutReader, TokenUsage, _event_time, configured_sessions_dir


def event(kind, turn=None, last=None, total=None, timestamp=1, duration_ms=None, thread_id="thread-12345678"):
    payload = {"type": kind}
    if turn:
        payload["turn_id"] = turn
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if last is not None:
        payload["info"] = {"last_token_usage": last, "total_token_usage": total}
    return {"type": "event_msg", "payload": payload, "timestamp": timestamp, "thread_id": thread_id}


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

    def test_task_complete_uses_final_token_count_at_eof(self):
        with tempfile.TemporaryDirectory() as directory:
            first, final = usage(10, 4, 2, 1), usage(20, 8, 4, 2)
            events = [
                event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)),
                event("task_started", "turn"),
                event("token_count", last=first, total=first),
                event("task_complete", "turn", duration_ms=321),
                event("token_count", last=final, total=usage(30, 12, 6, 3)),
            ]
            self.write(directory, events)
            result = CodexRolloutReader().refresh(Path(directory))
        self.assertTrue(result.instruction.exact)
        self.assertEqual(result.instruction.model_calls, 2)
        self.assertEqual(result.instruction.usage, TokenUsage(30, 12, 6, 3, 36))
        self.assertEqual(result.instruction.duration_ms, 321)

    def test_post_complete_duplicate_does_not_add_a_call(self):
        with tempfile.TemporaryDirectory() as directory:
            call = usage(10, 4, 2, 1)
            events = [
                event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)),
                event("task_started", "turn"),
                event("token_count", last=call, total=call),
                event("task_complete", "turn", duration_ms=321),
                event("token_count", last=call, total=call),
            ]
            self.write(directory, events)
            result = CodexRolloutReader().refresh(Path(directory))
        self.assertTrue(result.instruction.exact)
        self.assertEqual(result.instruction.model_calls, 1)
        self.assertEqual(result.instruction.duplicate_snapshots, 1)

    def test_next_turn_does_not_replace_completed_instruction_with_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            call = usage(10, 4, 2, 1)
            events = [
                event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)),
                event("task_started", "one"), event("token_count", last=call, total=call), event("task_complete", "one", duration_ms=321),
                event("task_started", "two"),
            ]
            self.write(directory, events)
            result = CodexRolloutReader().refresh(Path(directory))
        self.assertTrue(result.instruction.in_progress)
        self.assertEqual(result.instruction.turn_id, "two")

    def test_refresh_sessions_returns_threads_separately_by_event_time(self):
        with tempfile.TemporaryDirectory() as directory:
            older = [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0), timestamp=10, thread_id="thread-a"), event("task_started", "a", timestamp=11, thread_id="thread-a"), event("token_count", "a", usage(10, 2, 1, 0), usage(10, 2, 1, 0), timestamp=12, thread_id="thread-a")]
            newer = [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0), timestamp=20, thread_id="thread-b"), event("task_started", "b", timestamp=21, thread_id="thread-b"), event("token_count", "b", usage(30, 15, 5, 1), usage(30, 15, 5, 1), timestamp=22, thread_id="thread-b")]
            a = self.write(directory, older, "rollout-z.jsonl")
            b = self.write(directory, newer, "rollout-a.jsonl")
            os.utime(a, (100, 100)); os.utime(b, (50, 50))
            result = CodexRolloutReader().refresh_sessions(Path(directory))
        self.assertEqual([item.thread_id for item in result.sessions], ["thread-b", "thread-a"])
        self.assertEqual([item.thread_cumulative_usage.total_tokens for item in result.sessions], [35, 11])
        self.assertEqual(result.running_thread_count, 2)

    def test_duplicate_thread_rollouts_keep_latest_without_summing(self):
        with tempfile.TemporaryDirectory() as directory:
            first = [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0), timestamp=1, thread_id="same"), event("task_started", "a", timestamp=2, thread_id="same"), event("token_count", "a", usage(10, 0, 1, 0), usage(10, 0, 1, 0), timestamp=3, thread_id="same")]
            latest = [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0), timestamp=4, thread_id="same"), event("task_started", "b", timestamp=5, thread_id="same"), event("token_count", "b", usage(20, 5, 2, 0), usage(20, 5, 2, 0), timestamp=6, thread_id="same")]
            self.write(directory, first, "rollout-999.jsonl")
            self.write(directory, latest, "rollout-001.jsonl")
            result = CodexRolloutReader().refresh_sessions(Path(directory))
        self.assertEqual(len(result.sessions), 1)
        self.assertEqual(result.sessions[0].instruction.turn_id, "b")
        self.assertEqual(result.sessions[0].thread_cumulative_usage.total_tokens, 22)

    def test_lookback_days_filters_by_valid_event_time(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            for thread_id, when, name in (("recent", now - timedelta(days=2), "rollout-recent.jsonl"), ("old", now - timedelta(days=20), "rollout-old.jsonl")):
                stamp = when.isoformat()
                events = [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0), timestamp=stamp, thread_id=thread_id), event("task_started", thread_id, timestamp=stamp, thread_id=thread_id), event("token_count", thread_id, usage(3, 1, 1, 0), usage(3, 1, 1, 0), timestamp=stamp, thread_id=thread_id)]
                self.write(directory, events, name)
            result = CodexRolloutReader().refresh_sessions(Path(directory), lookback_days=7)
        self.assertEqual([item.thread_id for item in result.sessions], ["recent"])

    def test_unchanged_files_are_reused_without_reading_again(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write(directory, [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)), event("task_started", "t"), event("token_count", "t", usage(3, 1, 1, 0), usage(3, 1, 1, 0))])
            reader = CodexRolloutReader()
            with patch.object(reader, "_read", wraps=reader._read) as read:
                first = reader.refresh_sessions(Path(directory))
                second = reader.refresh_sessions(Path(directory))
        self.assertEqual(read.call_count, 1)
        self.assertEqual(first.files_parsed, 1)
        self.assertEqual(second.files_parsed, 0)
        self.assertEqual(second.files_reused_from_cache, 1)
        self.assertEqual(first.sessions[0].observed_at, second.sessions[0].observed_at)
        self.assertEqual(second.sessions[0].refreshed_at, second.refreshed_at)

    def test_size_or_mtime_change_reparses_cached_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)), event("task_started", "t"), event("token_count", "t", usage(3, 1, 1, 0), usage(3, 1, 1, 0))])
            reader = CodexRolloutReader()
            reader.refresh_sessions(Path(directory))
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            changed_size = reader.refresh_sessions(Path(directory))
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            changed_mtime = reader.refresh_sessions(Path(directory))
        self.assertEqual(changed_size.files_parsed, 1)
        self.assertEqual(changed_mtime.files_parsed, 1)

    def test_new_and_deleted_files_update_process_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.write(directory, [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)), event("task_started", "a"), event("token_count", "a", usage(3, 1, 1, 0), usage(3, 1, 1, 0))], "rollout-first.jsonl")
            reader = CodexRolloutReader()
            reader.refresh_sessions(Path(directory))
            self.write(directory, [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)), event("task_started", "b"), event("token_count", "b", usage(4, 1, 1, 0), usage(4, 1, 1, 0))], "rollout-second.jsonl")
            added = reader.refresh_sessions(Path(directory))
            first.unlink()
            removed = reader.refresh_sessions(Path(directory))
        self.assertEqual(added.files_parsed, 1)
        self.assertEqual(added.files_reused_from_cache, 1)
        self.assertEqual(removed.files_reused_from_cache, 1)
        self.assertEqual(len(reader._parse_cache), 1)

    def test_expanding_time_range_reuses_prior_safe_results(self):
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            reader = CodexRolloutReader()
            for days, name in ((2, "recent"), (20, "month"), (60, "quarter")):
                when = now - timedelta(days=days)
                path = self.write(directory, [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0), timestamp=when.isoformat(), thread_id=name), event("task_started", name, timestamp=when.isoformat(), thread_id=name), event("token_count", name, usage(3, 1, 1, 0), usage(3, 1, 1, 0), timestamp=when.isoformat(), thread_id=name)], f"rollout-{name}.jsonl")
                os.utime(path, (when.timestamp(), when.timestamp()))
            seven = reader.refresh_sessions(Path(directory), lookback_days=7)
            thirty = reader.refresh_sessions(Path(directory), lookback_days=30)
            ninety = reader.refresh_sessions(Path(directory), lookback_days=90)
        self.assertEqual(seven.files_parsed, 1)
        self.assertEqual(thirty.files_reused_from_cache, 1)
        self.assertEqual(ninety.files_reused_from_cache, 2)

    def test_pinned_path_uses_the_same_cache_and_reparses_when_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)), event("task_started", "t"), event("token_count", "t", usage(3, 1, 1, 0), usage(3, 1, 1, 0))])
            reader = CodexRolloutReader()
            reader.refresh_sessions(Path(directory))
            cached = reader.read_session(path)
            path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            reparsed = reader.read_session(path)
        self.assertIsNotNone(cached)
        self.assertIsNotNone(reparsed)
        self.assertEqual(len(reader._parse_cache), 1)

    def test_cache_contains_only_safe_parse_dtos_and_signatures(self):
        with tempfile.TemporaryDirectory() as directory:
            self.write(directory, [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0)), event("task_started", "t"), event("token_count", "t", usage(3, 1, 1, 0), usage(3, 1, 1, 0))])
            reader = CodexRolloutReader()
            reader.refresh_sessions(Path(directory))
        entry = next(iter(reader._parse_cache.values()))
        self.assertIsInstance(entry.file_signature, tuple)
        self.assertFalse(hasattr(entry, "raw_json"))
        self.assertFalse(hasattr(entry, "payload"))
        self.assertFalse(hasattr(entry, "preview"))

    def test_candidate_limit_and_pinned_extra_are_reported_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            pinned = None
            for index in range(501):
                name = f"rollout-{index:03}.jsonl"
                path = self.write(directory, [event("token_count", last=usage(0, 0, 0, 0), total=usage(0, 0, 0, 0), thread_id=f"thread-{index}"), event("task_started", "t", thread_id=f"thread-{index}"), event("token_count", "t", usage(1, 0, 1, 0), usage(1, 0, 1, 0), thread_id=f"thread-{index}")], name)
                os.utime(path, (index + 1, index + 1))
                if index == 0:
                    pinned = path
            result = CodexRolloutReader().refresh_sessions(Path(directory), pinned_path=pinned)
        self.assertEqual(result.candidate_limit, 500)
        self.assertEqual(result.candidates_found, 501)
        self.assertEqual(result.candidates_loaded, 500)
        self.assertTrue(result.candidate_truncated)
        self.assertEqual(result.files_parsed, 501)
        self.assertEqual(len(result.sessions), 501)


if __name__ == "__main__":
    unittest.main()
