# Codex Token Monitor Skill

Codex Token Monitor Skill is an independent local-first project for estimating and visualizing token usage in Codex Desktop workflows. It is designed as a GitHub-agent-skill-style toolkit with a Windows desktop floating window / Dashboard as the first user-facing surface.

Current boundary: the Dashboard reads privacy-safe numeric metadata from Rollout JSONL and displays a current or latest instruction aggregate. `logs_2.sqlite` is a legacy adapter, not the current usage source. State Thread Total is reconciled only when the same Thread's cumulative total matches exactly. Cache hit is derived from usage numbers, not an official rate; cost, budget, and context remain estimates.

关键边界：本项目当前独立开发，不修改 AOS 主仓库，不接入云端，不读取真实凭据，不做真实扣费。当前 Dashboard 从 Rollout JSONL 读取安全的数字元数据，展示当前或最近的单指令聚合；`logs_2.sqlite` 仅保留为 legacy adapter。只读取 Reasoning Token 数量，不读取 Reasoning 内容。State Thread Total 仅在同一 Thread 的累计数值完全一致时标记为已对账。

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
app/        CustomTkinter Dashboard and metric logic.
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

## 从源码运行

```powershell
python -m pip install -r requirements.txt
python app\main.py
```

## 构建 Windows 便携版

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

## 启动便携版

```text
dist\CodexTokenMonitor\CodexTokenMonitor.exe
```

便携版目标电脑不需要安装 Python。用户数据默认保存在 `%LOCALAPPDATA%\CodexTokenMonitor`，删除便携版目录不会自动删除用户数据。本阶段只提供 one-folder 便携版，不是安装器；尚未实现代码签名、自动更新、开机启动和系统托盘。Windows 可能对未签名的新 exe 显示安全提示，详见 [Windows 便携版构建](docs/windows-portable-build.md)。

## Current Status

Phase 2.7-A reads privacy-safe numeric metadata from the active Codex rollout and shows exact per-instruction usage only after cumulative-token reconciliation. State is marked reconciled only when its total exactly matches the selected Rollout Thread cumulative total. `logs_2.sqlite` is retained as a legacy adapter but is no longer the Dashboard current-usage source; see [Rollout instruction usage](docs/rollout-instruction-usage.md).

Phase 2-MVP adds manual local run records, JSON persistence, session summaries, and Markdown report export. It saves prompt/output summaries and manual token counts only by default; do not store credentials or private prompt/output full text.

### Historical Phase 2.4 logs adapter notes

Phase 2.4-B presented the latest `response.completed` values from `logs_2.sqlite`; this is historical behavior, not the current Dashboard usage source.

Phase 2.4-C adds a stateful logs reader and optional low-frequency automatic refresh. The first read performs one initial lookup; later refreshes use the indexed `(ts, ts_nanos, id)` cursor and process at most 500 new rows per scan instead of revalidating the full log. Auto Refresh defaults to Off with a fixed 60-second interval, can be enabled or disabled in the Dashboard, and never saves a Run, exports a report, or writes to Codex SQLite. Recent Runs still comes only from explicit `Save Run / 保存` actions.

By default, usage, cost, cache, context, and budget values remain 本地估算 / local estimate. When available, the optional read-only `state_5.sqlite` adapter reads only safe fields and labels `threads.tokens_used` as `codex_state_sqlite / real total` for session total tokens only. The optional read-only `logs_2.sqlite` adapter uses SQLite JSON1 to extract only the latest `response.completed` numeric usage fields; Python receives no event body or body substring, and labels the values `codex_logs_sqlite / real usage`. It accepts only a complete JSON event with root `usage`, or the confirmed `SSE event: ` format with `response.usage`; both require a root `type` of `response.completed`, so keywords and fake usage objects inside content are ignored. Unavailable data uses `unknown`. Cache hit is derived from those numbers, not an official cache hit rate, and current/session cost remains an estimate, not billing. Set `CODEX_STATE_DB` and `CODEX_LOGS_DB` to configure database paths.

The logs adapter reports `connected`, `database missing`, `open failed`, `no response.completed`, or `parse failed`. `Refresh / 刷新` manually rereads logs and state data and updates values, sources, status, and time without saving a Run or adding to Recent Runs. Cache hit remains derived rather than an official rate, and cost remains an estimate rather than billing. Historical session/thread aggregation, system tray support, startup integration, installers, and Codex Desktop embedding are not implemented. The repository remains independent until integration is explicitly planned.
