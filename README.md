# Codex Token Monitor Skill

Codex Token Monitor Skill is an independent local-first project for estimating and visualizing token usage in Codex Desktop workflows. It is designed as a GitHub-agent-skill-style toolkit with a Windows desktop floating window / Dashboard as the first user-facing surface.

Current boundary: the Dashboard discovers multiple recent Codex Threads from Rollout JSONL, keeps their token data separate, auto-follows the latest activity, and can pin one Thread for monitoring. `state_5.sqlite` is queried once per full refresh for safe metadata, while user-facing titles come from one structured Codex `app-server` `thread/list` batch. The UI trusts the official `Thread.name` without inferring content from its wording; no preview or message body is decoded.

关键边界：本项目当前独立开发，不修改 AOS 主仓库，不接入云端，不读取真实凭据，不做真实扣费。当前 Dashboard 从 Rollout JSONL 读取安全的数字元数据，展示当前或最近的单指令聚合；`logs_2.sqlite` 仅保留为 legacy adapter。只读取 Reasoning Token 数量，不读取 Reasoning 内容。State Thread Total 仅在同一 Thread 的累计数值完全一致时标记为已对账。

## Historical MVP scope

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

便携版目标电脑不需要安装 Python。用户数据默认保存在 `%LOCALAPPDATA%\CodexTokenMonitor`，删除便携版目录不会自动删除用户数据。本阶段提供 one-folder 便携版、系统托盘、用户可选的 HKCU 开机启动和单实例保护，但仍不是安装器，也没有代码签名或自动更新。Windows 可能对未签名的新 exe 显示安全提示，详见 [Windows 便携版构建](docs/windows-portable-build.md)。

## 桌面迷你组件与使用限额

Phase 2.8-A 在主 Dashboard 最小化时隐藏主窗口，并显示一个置顶的桌面迷你组件；也可点击主界面的“显示迷你组件”按钮主动切换。组件默认位于主窗口所在显示器工作区右上角，以数据可读的半透明状态展示，鼠标悬停时恢复完整背景；支持拖动、手动刷新和恢复主界面。点击主窗口关闭或组件退出时会询问“最小化到任务栏 / 退出程序”，默认最小化到任务栏，并可勾选“今天不再提示”。恢复主界面只切换窗口，不重新查询数据，并保留最小化前固定的 Thread、时间范围、语言和自动刷新状态。

迷你组件显示 Codex 5 小时与每周窗口的已使用百分比、剩余百分比、重置时间，以及最小化前选中 Thread 的“本次指令 Total”和“当前会话累计 Total”。两项数值范围独立，不互相回填；指令数据未知时显示 `—`。组件标题栏提供恢复、直接最小化和退出：直接最小化会隐藏主界面与组件并保留任务栏图标，不弹退出询问；点击任务栏图标恢复主 Dashboard，不触发数据查询。额度通过本机已安装 Codex 的官方 `app-server` 结构化只读方法 `account/rateLimits/read` 获取；本项目不读取或保存 Cookie、Token、Authorization、Session Secret，也不读取 prompt、response、preview、message、tool output 或 reasoning 正文。未知值显示 `—`，刷新失败时保留上一份值并明确标记为陈旧，不伪造数据。

系统托盘支持恢复主界面、显示迷你组件、隐藏全部窗口、手动刷新、切换自动刷新、打开设置和显式退出；恢复与窗口切换不查询数据。默认启动模式可设为主界面、迷你组件或仅托盘；“随 Windows 启动”默认关闭且仅便携版可用。详见 [桌面迷你组件](docs/desktop-mini-widget.md) 与 [Windows 托盘和启动设置](docs/windows-tray-and-startup.md)。当前仍不包含安装器、自动更新、额度预测、账户切换或云端同步。

## Current Status

Phase 2.8-A-S 保留多 Thread 分离、500 条近期候选、已知路径回读和 Rollout 进程内缓存规则。近期列表每页 10 条，可在内存中翻页；点击近期行或下拉任务时直接切换完整刷新留下的快照，不重新读取 Rollout、SQLite、标题或额度。默认选择仍可自动跟随最近活动；主窗口最小化时会把当时实际选中的 Thread 解析为固定选择。近期范围默认 7 天，可切换 30 或 90 天。

The current Dashboard no longer loads or writes the legacy manual Runs JSON and no longer shows manual Run input, saved-Run, or report-export controls. `AgentRun`, `app/storage.py`, `app/reporting.py`, existing Runs JSON, historical reports, and their compatibility tests remain preserved.

Thread titles use the installed Codex `app-server` structured `Thread.name` field. One full refresh makes one `thread/list` batch request over the same persistent app-server connection used by quota reads; the selective parser decodes only `id` and `name`, never `preview`, turns or message content. The UI never guesses whether a name looks like a prompt or path; only missing, invalid, empty, unparsable, or absent names use `Codex 会话 · MM-DD HH:mm` / `Codex Session · MM-DD HH:mm`. Long names are safely truncated only at the display boundary. See [Rollout instruction usage](docs/rollout-instruction-usage.md).

### Historical Phase 2.4 logs adapter notes

Phase 2.4-B presented the latest `response.completed` values from `logs_2.sqlite`; this is historical behavior, not the current Dashboard usage source.

Phase 2.4-C added a stateful logs reader and optional low-frequency automatic refresh. This is historical adapter behavior; the current Dashboard uses Rollout sessions and a fixed optional 60-second refresh.

By default, usage, cost, cache, context, and budget values remain 本地估算 / local estimate. When available, the optional read-only `state_5.sqlite` adapter reads only safe fields and labels `threads.tokens_used` as `codex_state_sqlite / real total` for session total tokens only. The optional read-only `logs_2.sqlite` adapter uses SQLite JSON1 to extract only the latest `response.completed` numeric usage fields; Python receives no event body or body substring, and labels the values `codex_logs_sqlite / real usage`. It accepts only a complete JSON event with root `usage`, or the confirmed `SSE event: ` format with `response.usage`; both require a root `type` of `response.completed`, so keywords and fake usage objects inside content are ignored. Unavailable data uses `unknown`. Cache hit is derived from those numbers, not an official cache hit rate, and current/session cost remains an estimate, not billing. Set `CODEX_STATE_DB` and `CODEX_LOGS_DB` to configure database paths.

The legacy logs adapter reports `connected`, `database missing`, `open failed`, `no response.completed`, or `parse failed`, but is not on the current Dashboard usage path. Installers, automatic updates, and Codex Desktop embedding are not implemented. The repository remains independent until integration is explicitly planned.
