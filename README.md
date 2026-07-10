# Codex Token Monitor Skill

Codex Token Monitor Skill is an independent local-first project for estimating and visualizing token usage in Codex Desktop workflows. It is designed as a GitHub-agent-skill-style toolkit with a Windows desktop floating window / Dashboard as the first user-facing surface.

Boundary: session total tokens may be labeled `codex_state_sqlite / real total`, and latest `response.completed` numeric usage may be labeled `codex_logs_sqlite / real usage`. Cache hit is derived from usage numbers, not an official cache hit rate; cost, budget, and context remain estimates, not real billing.

关键边界：本项目当前独立开发，不修改 AOS 主仓库，不接入云端，不读取真实凭据，不读取 Codex hidden reasoning tokens，不做真实扣费。session total tokens 可明确标注为 `codex_state_sqlite / real total`；latest `response.completed` numeric usage 可明确标注为 `codex_logs_sqlite / real usage`。cache hit 只是由 usage 数字推导，不是官方命中率；cost/budget/context 仍不能当作真实账单、真实余额或 provider 官方 usage。

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

Phase 2.4-B presents the latest `response.completed` input, output, total, cached, and reasoning token values independently in the Dashboard. It also shows a cache hit estimate derived from real cached/input values, the logs adapter status, and either the reliable event time (`Latest response at`) or the successful refresh time (`Refreshed at`). Missing values are shown as `unknown`, never invented as zero.

By default, usage, cost, cache, context, and budget values remain 本地估算 / local estimate. When available, the optional read-only `state_5.sqlite` adapter reads only safe fields and labels `threads.tokens_used` as `codex_state_sqlite / real total` for session total tokens only. The optional read-only `logs_2.sqlite` adapter uses SQLite JSON1 to extract only the latest `response.completed` numeric usage fields; Python receives no event body or body substring, and labels the values `codex_logs_sqlite / real usage`. Unavailable data uses `unknown`. Cache hit is derived from those numbers, not an official cache hit rate, and current/session cost remains an estimate, not billing. Set `CODEX_STATE_DB` and `CODEX_LOGS_DB` to configure database paths.

The logs adapter reports `connected`, `database missing`, `open failed`, `no response.completed`, or `parse failed`. `Refresh / 刷新` manually rereads logs and state data and updates values, sources, status, and time without saving a Run or adding to Recent Runs. Recent Runs still contains only explicit `Save Run / 保存` records. Automatic refresh, historical session/thread aggregation, installers, and Codex Desktop embedding are not implemented. The repository remains independent until integration is explicitly planned.
