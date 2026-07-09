# Codex Token Monitor Skill

Codex Token Monitor Skill is an independent local-first project for estimating and visualizing token usage in Codex Desktop workflows. It is designed as a GitHub-agent-skill-style toolkit with a Windows desktop floating window / Dashboard as the first user-facing surface.

关键边界：本项目当前独立开发，不修改 AOS 主仓库，不接入云端，不读取真实凭据，不读取 Codex hidden reasoning tokens，不做真实扣费。除明确标注为 `codex_state_sqlite / real total` 的 session total tokens 外，input/output/cache hit/cost/budget/context usage 仍为“本地估算 / local estimate”或 unknown，不能当作真实账单、真实余额或 provider 官方 usage。

## MVP

- Manually start/end a Codex task monitoring run.
- Paste or record the current Codex prompt and output.
- Estimate prompt tokens, output tokens, cache hit, context usage, cost, budget remaining, and token waste.
- Capture task time range, elapsed time, git before/after status, changed files, and diff stat.
- Show a Windows desktop Dashboard with a bottom telemetry status bar.
- Produce a local token waste report and Cache Hit Advisor suggestions.
- Save locally only; no cloud sync.
- Keep an explicit future AOS integration boundary.

## Project Layout

```text
app/        Minimal Python stdlib mock Dashboard and metric logic.
docs/       Product, UI, telemetry model, advisor, and AOS integration notes.
resources/  Sample run data, report template, and pricing config sample.
tests/      Metric tests and validation notes.
```

## Quick Validation

```powershell
python -m unittest discover -s tests
python app\main.py --smoke
python app\main.py
```

`python app\main.py` opens the local Dashboard if Tkinter is available.

## Current Status

Phase 2-MVP adds manual local run records, JSON persistence, session summaries, and Markdown report export. It saves prompt/output summaries and manual token counts only by default; do not store credentials or private prompt/output full text.

By default, usage, cost, cache, context, and budget values remain 本地估算 / local estimate. When available, the optional read-only `state_5.sqlite` adapter reads only safe fields and labels `threads.tokens_used` as `codex_state_sqlite / real total` for session total tokens only; input/output/cache/reasoning/cost/context/budget remain estimates or unknown, cache hit is not real Codex cache hit, and `logs_2.sqlite` is not connected. Set `CODEX_STATE_DB` to configure the database path. The repository remains independent until integration is explicitly planned.
