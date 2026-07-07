# Codex Token Monitor Skill

Codex Token Monitor Skill is an independent local-first project for estimating and visualizing token usage in Codex Desktop workflows. It is designed as a GitHub-agent-skill-style toolkit with a Windows desktop floating window / Dashboard as the first user-facing surface.

关键边界：本项目当前独立开发，不修改 AOS 主仓库，不接入云端，不读取真实凭据，不读取 Codex hidden reasoning tokens，不做真实扣费。所有 token、cache hit、cost、budget、context usage 均为“本地估算 / local estimate”，不能当作真实账单、真实余额或 provider 官方 usage。

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

This is a phase-1 scaffold and mock telemetry UI. It estimates locally from pasted prompt/output and sample data. Later versions may add optional wrappers, richer storage, and AOS adapters, but this repository must remain independent until integration is explicitly planned.

