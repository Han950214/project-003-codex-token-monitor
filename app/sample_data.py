"""Sample telemetry data for the mock Dashboard."""

from __future__ import annotations

from app.metrics import RunUsage


SAMPLE_PROMPT = """低 token 模式。请检查当前 Codex 任务的 prompt 和 output，估算 token waste，并给出下一轮 cache-friendly prompt。"""

SAMPLE_OUTPUT = """已生成本地估算报告：重复背景约 600 tokens，动态前缀存在时间戳风险。建议下一轮只描述增量目标。"""

SAMPLE_USAGES = [
    RunUsage(input_tokens=2800, output_tokens=700, optional_log_tokens=80, stable_prefix_tokens=1600),
    RunUsage(input_tokens=3200, output_tokens=950, optional_log_tokens=120, stable_prefix_tokens=2100),
]

SAMPLE_CHANGED_FILES = [
    "README.md",
    "SKILL.md",
    "app/metrics.py",
]

SAMPLE_DIFF_STAT = "3 files changed, 220 insertions"

