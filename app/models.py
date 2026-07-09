"""Data models for locally saved Codex Token Monitor runs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    session_id: str
    project: str
    title: str
    started_at: str
    ended_at: str
    elapsed_seconds: int
    model: str
    mode: str
    prompt_summary: str
    output_summary: str
    note: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    estimated_cost: float
    cache_hit: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentRun":
        return cls(
            run_id=str(data.get("run_id", "")),
            session_id=str(data.get("session_id", "")),
            project=str(data.get("project", "")),
            title=str(data.get("title", "")),
            started_at=str(data.get("started_at", "")),
            ended_at=str(data.get("ended_at", "")),
            elapsed_seconds=int(data.get("elapsed_seconds", 0) or 0),
            model=str(data.get("model", "")),
            mode=str(data.get("mode", "")),
            prompt_summary=str(data.get("prompt_summary", "")),
            output_summary=str(data.get("output_summary", "")),
            note=str(data.get("note", "")),
            input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=int(data.get("output_tokens", 0) or 0),
            cached_tokens=int(data.get("cached_tokens", 0) or 0),
            total_tokens=int(data.get("total_tokens", 0) or 0),
            estimated_cost=float(data.get("estimated_cost", 0.0) or 0.0),
            cache_hit=float(data.get("cache_hit", 0.0) or 0.0),
        )

