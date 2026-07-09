import tempfile
import unittest
from pathlib import Path

from app.models import AgentRun
from app.storage import append_run, load_runs, save_runs


def sample_run(run_id: str = "run-1") -> AgentRun:
    return AgentRun(
        run_id=run_id,
        session_id="session-1",
        project="project_003_codex_token_monitor",
        title="Manual run",
        started_at="2026-07-08T09:00:00",
        ended_at="2026-07-08T09:01:00",
        elapsed_seconds=60,
        model="local-estimate-demo",
        mode="manual",
        prompt_summary="Short prompt summary",
        output_summary="Short output summary",
        note="Local note",
        input_tokens=100,
        output_tokens=50,
        cached_tokens=40,
        total_tokens=150,
        estimated_cost=0.001,
        cache_hit=0.4,
    )


class StorageTests(unittest.TestCase):
    def test_missing_json_loads_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            result = load_runs(Path(temp_dir) / "missing.json")
        self.assertEqual(result.runs, [])
        self.assertIsNone(result.error)

    def test_empty_json_loads_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runs.json"
            path.write_text("", encoding="utf-8")
            result = load_runs(path)
        self.assertEqual(result.runs, [])
        self.assertIsNone(result.error)

    def test_invalid_json_returns_error_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runs.json"
            path.write_text("{not-json", encoding="utf-8")
            result = append_run(sample_run(), path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")
        self.assertEqual(result.runs, [])
        self.assertIsNotNone(result.error)

    def test_save_and_load_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "runs.json"
            save_runs([sample_run()], path)
            result = load_runs(path)
        self.assertIsNone(result.error)
        self.assertEqual(len(result.runs), 1)
        self.assertEqual(result.runs[0].run_id, "run-1")


if __name__ == "__main__":
    unittest.main()
