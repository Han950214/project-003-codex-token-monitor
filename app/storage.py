"""Local JSON persistence for Codex Token Monitor runs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.models import AgentRun
from app.paths import runs_path


DEFAULT_RUNS_PATH = runs_path()


@dataclass(frozen=True)
class LoadResult:
    runs: list[AgentRun]
    error: str | None = None


def load_runs(path: Path | None = None) -> LoadResult:
    path = path or runs_path()
    if not path.exists() or path.stat().st_size == 0:
        return LoadResult([])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return LoadResult([], f"Invalid JSON: {exc.msg}")
    if not isinstance(raw, list):
        return LoadResult([], "Invalid JSON: expected a list")
    try:
        return LoadResult([AgentRun.from_dict(item) for item in raw if isinstance(item, dict)])
    except (TypeError, ValueError) as exc:
        return LoadResult([], f"Invalid run data: {exc}")


def save_runs(runs: list[AgentRun], path: Path | None = None) -> None:
    path = path or runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    data = [run.to_dict() for run in runs]
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def append_run(run: AgentRun, path: Path | None = None) -> LoadResult:
    path = path or runs_path()
    result = load_runs(path)
    if result.error:
        return result
    runs = [*result.runs, run]
    save_runs(runs, path)
    return LoadResult(runs)
