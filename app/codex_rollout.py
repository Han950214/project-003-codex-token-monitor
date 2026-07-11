"""Privacy-preserving reader for numeric instruction usage in Codex rollouts."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CODEX_SESSIONS_DIR_ENV = "CODEX_SESSIONS_DIR"
DEFAULT_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int

    @property
    def non_cached_input_tokens(self) -> int:
        return self.input_tokens - self.cached_input_tokens

    @property
    def non_reasoning_output_tokens(self) -> int:
        return self.output_tokens - self.reasoning_output_tokens

    def minus(self, previous: "TokenUsage") -> "TokenUsage":
        return TokenUsage(*(a - b for a, b in zip(self.values(), previous.values())))

    def values(self) -> tuple[int, int, int, int, int]:
        return (self.input_tokens, self.cached_input_tokens, self.output_tokens, self.reasoning_output_tokens, self.total_tokens)


@dataclass(frozen=True)
class ModelCallUsage:
    usage: TokenUsage
    epoch: int


@dataclass(frozen=True)
class InstructionUsage:
    turn_id: str
    status: str
    usage: TokenUsage | None
    model_calls: int
    duration_ms: int | None
    duplicate_snapshots: int
    rejected_events: int
    unreconciled_events: int
    exact: bool
    in_progress: bool


@dataclass(frozen=True)
class RolloutUsageResult:
    rollout_filename: str | None
    thread_id: str | None
    instruction: InstructionUsage | None
    available: bool

    @property
    def thread_suffix(self) -> str | None:
        return self.thread_id[-8:] if self.thread_id else None


def configured_sessions_dir() -> Path:
    configured = os.environ.get(CODEX_SESSIONS_DIR_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_SESSIONS_DIR


class CodexRolloutReader:
    """Reads event names and numeric metadata only; text fields are never retained."""

    def __init__(self, candidate_limit: int = 30) -> None:
        self.candidate_limit = candidate_limit

    def refresh(self, sessions_dir: Path | None = None) -> RolloutUsageResult:
        root = sessions_dir or configured_sessions_dir()
        if not root.is_dir():
            return RolloutUsageResult(None, None, None, False)
        candidates = sorted(root.rglob("rollout-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)[: self.candidate_limit]
        parsed = [self._read(path) for path in candidates]
        valid = [item for item in parsed if item[1].available]
        if not valid:
            return RolloutUsageResult(None, None, None, False)
        # Event time, rather than file name or mtime, chooses the active compatible rollout.
        return max(valid, key=lambda item: item[0])[1]

    def _read(self, path: Path) -> tuple[float, RolloutUsageResult]:
        events: list[tuple[str, dict[str, Any], float]] = []
        newest_token_time = -1.0
        thread_id: str | None = None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload")
                    payload = payload if isinstance(payload, dict) else {}
                    # Rollouts store the thread identifier in session metadata or
                    # later thread-goal events, not in every token snapshot.
                    event_thread = (
                        _string(record.get("thread_id"))
                        or _string(payload.get("thread_id"))
                        or _string(payload.get("threadId"))
                        or (_string(payload.get("id")) if not _string(payload.get("type")) else None)
                    )
                    if event_thread:
                        thread_id = thread_id or event_thread
                    name = _event_name(record, payload)
                    if name not in {"task_started", "task_complete", "token_count"}:
                        continue
                    when = _event_time(record, payload)
                    events.append((name, payload, when))
                    if name == "token_count" and _usage_from_payload(payload) is not None:
                        newest_token_time = max(newest_token_time, when)
        except OSError:
            return -1.0, RolloutUsageResult(None, None, None, False)
        if newest_token_time < 0:
            return -1.0, RolloutUsageResult(None, None, None, False)
        instruction = _instruction_from_events(events)
        if instruction is None:
            return -1.0, RolloutUsageResult(None, thread_id, None, False)
        return newest_token_time, RolloutUsageResult(path.name, thread_id, instruction, True)


def _instruction_from_events(events: list[tuple[str, dict[str, Any], float]]) -> InstructionUsage | None:
    cumulative: TokenUsage | None = None
    epoch = 0
    turns: dict[str, dict[str, Any]] = {}
    last_turn: str | None = None
    for name, payload, when in events:
        turn_id = _string(payload.get("turn_id"))
        if name == "task_started" and turn_id:
            turns[turn_id] = {"started": True, "completed": False, "baseline": cumulative, "end": None, "calls": [], "duplicates": 0, "rejected": 0, "unreconciled": 0, "when": when, "duration": None}
            last_turn = turn_id
            continue
        if name == "task_complete" and turn_id in turns:
            turn = turns[turn_id]
            turn["completed"] = True
            turn["end"] = cumulative
            duration = payload.get("duration_ms")
            turn["duration"] = duration if _integer(duration) is not None and _integer(duration) >= 0 else _duration_from_times(turn["when"], when)
            last_turn = turn_id
            continue
        if name != "token_count":
            continue
        active_id = turn_id if turn_id in turns else last_turn
        current = _usage_from_payload(payload)
        if current is None:
            if active_id in turns:
                turns[active_id]["rejected"] += 1
            continue
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        total = _usage_from_mapping(info.get("total_token_usage"))
        if total is None:
            if active_id in turns:
                turns[active_id]["rejected"] += 1
            continue
        if cumulative is not None:
            delta = total.minus(cumulative)
            if total.values() == cumulative.values():
                if active_id in turns:
                    turns[active_id]["duplicates"] += 1
                continue
            if any(value < 0 for value in delta.values()):
                epoch += 1
                cumulative = total
                continue
            if delta == current:
                if active_id in turns:
                    turns[active_id]["calls"].append(ModelCallUsage(current, epoch))
            elif active_id in turns:
                turns[active_id]["unreconciled"] += 1
        cumulative = total
    if not turns:
        return None
    candidates = list(turns.items())
    complete = [(turn_id, turn) for turn_id, turn in candidates if turn["completed"]]
    turn_id, turn = (next(((key, value) for key, value in reversed(candidates) if not value["completed"]), None) or (complete[-1] if complete else candidates[-1]))
    calls = tuple(turn["calls"])
    usage = _sum_usage(call.usage for call in calls) if calls else None
    baseline, end = turn["baseline"], turn["end"] if turn["completed"] else cumulative
    exact = bool(turn["completed"] and baseline is not None and end is not None and not turn["unreconciled"] and usage is not None and usage == end.minus(baseline))
    status = "exact" if exact else ("in_progress" if not turn["completed"] else "incomplete")
    return InstructionUsage(turn_id, status, usage, len(calls), turn["duration"], turn["duplicates"], turn["rejected"], turn["unreconciled"], exact, not turn["completed"])


def _usage_from_payload(payload: dict[str, Any]) -> TokenUsage | None:
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    return _usage_from_mapping(info.get("last_token_usage"))


def _usage_from_mapping(value: Any) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    numbers = tuple(_integer(value.get(key)) for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"))
    if any(number is None for number in numbers):
        return None
    usage = TokenUsage(*numbers)  # type: ignore[arg-type]
    if any(number < 0 for number in usage.values()) or usage.cached_input_tokens > usage.input_tokens or usage.reasoning_output_tokens > usage.output_tokens or usage.total_tokens != usage.input_tokens + usage.output_tokens:
        return None
    return usage


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _event_name(record: dict[str, Any], payload: dict[str, Any]) -> str:
    return _string(payload.get("type")) or _string(record.get("type")) or ""


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _event_time(record: dict[str, Any], payload: dict[str, Any]) -> float:
    value = record.get("timestamp", record.get("ts", payload.get("timestamp")))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=timezone.utc if "+" not in value and "Z" not in value else None).timestamp()
        except ValueError:
            pass
    return 0.0


def _duration_from_times(start: float, end: float) -> int | None:
    return max(round((end - start) * 1000), 0) if start and end else None


def _sum_usage(usages: Any) -> TokenUsage:
    values = [0, 0, 0, 0, 0]
    for usage in usages:
        for index, value in enumerate(usage.values()):
            values[index] += value
    return TokenUsage(*values)
