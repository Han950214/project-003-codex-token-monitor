# Codex Token Monitor Skill

Codex Token Monitor Skill 是一个独立、本地优先的 **Codex 工作流状态助手**。产品只提供一个统一“状态中心”，同时回答“当前是否正常、有什么风险、为什么、下一步做什么”，并默认展示核心用量、当前任务、额度和最近任务。Windows 主界面、桌面组件、系统托盘和诊断流程均使用同一个进程内 snapshot 与同一条刷新路径。

Current boundary: the status center discovers multiple recent Codex Threads from Rollout JSONL, keeps their token data separate, auto-follows the latest activity, and can pin one Thread for monitoring. `state_5.sqlite` is queried once per full refresh for safe metadata, while user-facing titles come from one structured Codex `app-server` `thread/list` batch. The UI trusts the official `Thread.name` without inferring content from its wording; no preview or message body is decoded.

关键边界：本项目不替代 AOS，不创建知识库、项目记忆或项目上下文，不扫描项目文件，也不读取或保存 Prompt、Response、消息、工具输出或 Reasoning 正文。“准备新线程”只解释数字风险、打开 Codex，并复制不含项目内容的通用手工交接模板。状态中心从 Rollout JSONL 读取安全数字元数据；`logs_2.sqlite` 仅保留为 legacy adapter。只读取 Reasoning Token 数量，不读取 Reasoning 内容。

## Phase 3.0 产品体验

- 左侧一级导航固定为四项：状态中心、历史记录、工具、设置；当前任务不再是一级入口，而是可从状态中心、最近任务、历史记录或 Advisor 操作进入的二级详情。
- 状态中心默认显示一个最高优先级 Advisor、五项核心指标（本轮用量、会话累计、缓存复用、模型思考消耗、额度剩余）、当前任务摘要、5 小时与每周额度、四项快捷操作和最近 5 条任务。
- 当前任务详情和指标详情按需展示 Input、Output、Total、Cached、Reasoning、Cache Hit 等完整安全数字；历史记录页保留 7/30/90 天、每页 10 条、分页、状态筛选、固定选择、自动跟随和选中高亮。
- Advisor v1 只使用本地安全数字和状态代码，阈值集中定义，缓存复用明确标为本地推导值。
- 一键诊断独立检查版本、运行模式、Codex/app-server、额度、Rollout、安全数字、SQLite、设置、启动路径、托盘和数据新鲜度；单项失败不会中断其他检查。
- 桌面组件只保留 `compact`（收起）和 `expanded`（展开）两种显示状态；它们不是主界面模式，切换状态不读取数据、不重建 Tk root。
- 设置页不提供 Dashboard 模式选项；旧配置中的 `dashboard_mode` 会被安全忽略。组件默认状态、语言、透明度、启动模式、自动刷新和退出行为仍保存在本产品 UI settings 中，损坏值使用安全默认值。

详见 [产品交互](docs/product-interaction.md)、[工作流建议规则](docs/workflow-advisor.md) 与 [AOS 边界](docs/aos-boundary.md)。

## Historical MVP scope

以下仅记录早期原型，不代表当前产品能力；当前版本已移除正文录入、手工 Run 和报告导出入口。

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

## 构建 Windows 安装版

安装 Inno Setup 6 后执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1
```

安装版按当前用户安装到 `%LOCALAPPDATA%\Programs\CodexTokenMonitor`，不需要管理员权限；默认创建开始菜单快捷方式，可选创建桌面快捷方式。卸载会清理程序文件、快捷方式和本项目开机启动值，但保留 `%LOCALAPPDATA%\CodexTokenMonitor` 用户数据。详见 [Windows 用户级安装器](docs/windows-installer.md)。

便携版目标电脑不需要安装 Python。用户数据默认保存在 `%LOCALAPPDATA%\CodexTokenMonitor`，删除便携版目录不会自动删除用户数据。本阶段同时提供 one-folder 便携版和当前用户级安装器；两者保留系统托盘、用户可选的 HKCU 开机启动和单实例保护，但仍没有代码签名或自动更新。Windows 可能对未签名的新 exe 显示安全提示，详见 [Windows 便携版构建](docs/windows-portable-build.md)。

## 桌面组件与使用限额

桌面组件默认使用明显更小的 `compact` 状态胶囊，只显示状态、5 小时额度和展开按钮；`expanded` 显示当前状态、任务、轮次、本轮用量、会话累计、5 小时额度及少量操作。两种显示状态复用同一个 `CTkToplevel` 和共享 presentation，点击展开/收起、双击恢复、拖动或透明度变化均不重新查询数据。组件保持置顶，闲置使用用户透明度，鼠标进入恢复 100% 不透明。

迷你组件显示 Codex 5 小时额度，以及最小化前选中 Thread 的“本次指令 Total”和“当前会话累计 Total”；每周额度继续在状态中心和当前任务详情中显示。两项用量范围独立，不互相回填；指令数据未知时显示 `—`。展开状态提供打开主界面、手动刷新、更多工具、收起和关闭；“更多工具”会恢复主界面并直接打开工具页。长标题单行省略，完整标题通过 Tooltip 查看。额度通过本机已安装 Codex 的官方 `app-server` 结构化只读方法 `account/rateLimits/read` 获取；本项目不读取或保存 Cookie、Token、Authorization、Session Secret，也不读取 prompt、response、preview、message、tool output 或 reasoning 正文。未知值显示 `—`，刷新失败时保留上一份值并明确标记为陈旧，不伪造数据。

系统托盘支持恢复主界面、显示迷你组件、隐藏全部窗口、手动刷新、切换自动刷新、打开设置和显式退出；恢复与窗口切换不查询数据。默认启动模式可设为主界面、迷你组件或仅托盘；“随 Windows 启动”默认关闭且仅冻结版 EXE 可用。详见 [桌面迷你组件](docs/desktop-mini-widget.md) 与 [Windows 托盘和启动设置](docs/windows-tray-and-startup.md)。当前仍不包含自动更新、额度预测、账户切换或云端同步。

## Current Status

Phase 3.0-UI-Final 将主界面统一为单一状态中心：核心安全数字默认可见，详细技术字段按需查看。导航、页面切换、任务内存切换和组件收起/展开只重绘当前内存 snapshot；只有初始加载、手动刷新、自动刷新和一键诊断会访问真实数据。应用版本仍为 `0.1.0`。

Phase 2.8-A-S 保留多 Thread 分离、500 条近期候选、已知路径回读和 Rollout 进程内缓存规则。近期列表每页 10 条，可在内存中翻页；点击近期行或下拉任务时直接切换完整刷新留下的快照，不重新读取 Rollout、SQLite、标题或额度。默认选择仍可自动跟随最近活动；主窗口最小化时会把当时实际选中的 Thread 解析为固定选择。近期范围默认 7 天，可切换 30 或 90 天。

The current Dashboard no longer loads or writes the legacy manual Runs JSON and no longer shows manual Run input, saved-Run, or report-export controls. `AgentRun`, `app/storage.py`, `app/reporting.py`, existing Runs JSON, historical reports, and their compatibility tests remain preserved.

Thread titles use the installed Codex `app-server` structured `Thread.name` field. One full refresh makes one `thread/list` batch request over the same persistent app-server connection used by quota reads; the selective parser decodes only `id` and `name`, never `preview`, turns or message content. The UI never guesses whether a name looks like a prompt or path; only missing, invalid, empty, unparsable, or absent names use `Codex 会话 · MM-DD HH:mm` / `Codex Session · MM-DD HH:mm`. Long names are safely truncated only at the display boundary. See [Rollout instruction usage](docs/rollout-instruction-usage.md).

### Historical Phase 2.4 logs adapter notes

Phase 2.4-B presented the latest `response.completed` values from `logs_2.sqlite`; this is historical behavior, not the current Dashboard usage source.

Phase 2.4-C added a stateful logs reader and optional low-frequency automatic refresh. This is historical adapter behavior; the current Dashboard uses Rollout sessions and a fixed optional 60-second refresh.

By default, usage, cost, cache, context, and budget values remain 本地估算 / local estimate. When available, the optional read-only `state_5.sqlite` adapter reads only safe fields and labels `threads.tokens_used` as `codex_state_sqlite / real total` for session total tokens only. The optional read-only `logs_2.sqlite` adapter uses SQLite JSON1 to extract only the latest `response.completed` numeric usage fields; Python receives no event body or body substring, and labels the values `codex_logs_sqlite / real usage`. It accepts only a complete JSON event with root `usage`, or the confirmed `SSE event: ` format with `response.usage`; both require a root `type` of `response.completed`, so keywords and fake usage objects inside content are ignored. Unavailable data uses `unknown`. Cache hit is derived from those numbers, not an official cache hit rate, and current/session cost remains an estimate, not billing. Set `CODEX_STATE_DB` and `CODEX_LOGS_DB` to configure database paths.

The legacy logs adapter reports `connected`, `database missing`, `open failed`, `no response.completed`, or `parse failed`, but is not on the current Dashboard usage path. A per-user Windows installer is available; automatic updates and Codex Desktop embedding are not implemented. The repository remains independent until integration is explicitly planned.
