# Codex Token Monitor Skill

Codex Token Monitor Skill 是一个独立、本地优先的 **Codex 工作流状态助手**。产品只提供一个统一“状态中心”，同时回答“当前是否正常、有什么风险、为什么、下一步做什么”，并默认展示核心用量、当前任务、额度和最近任务。Windows 主界面、桌面组件、系统托盘和诊断流程均使用同一个进程内 snapshot 与同一条刷新路径。

Current boundary: the status center discovers multiple recent Codex Threads from Rollout JSONL, keeps their token data separate, auto-follows the latest activity, and can pin one Thread for monitoring. `state_5.sqlite` is queried once per full refresh for safe metadata, while user-facing titles come from one structured Codex `app-server` `thread/list` batch. The UI trusts the official `Thread.name` without inferring content from its wording; no preview or message body is decoded.

关键边界：本项目不替代 AOS，不创建知识库、项目记忆或项目上下文，不扫描项目文件，也不读取或保存 Prompt、Response、消息、工具输出或 Reasoning 正文。“准备新线程”只解释数字风险、打开 Codex，并复制不含项目内容的通用手工交接模板。状态中心从 Rollout JSONL 读取安全数字元数据；`logs_2.sqlite` 仅保留为 legacy adapter。只读取 Reasoning Token 数量，不读取 Reasoning 内容。

## Phase 3.1-D 高消耗定位与简明洞察

- “用量趋势 / Usage Trends”复用 Phase 3.1-C 的范围与规范响应归并，在同一次后台历史查询中提供高消耗会话、高消耗响应和低缓存复用会话，不新增数据源、页面或主线程读取。
- 高消耗会话与响应默认显示前三项，可在本地展开到前五项；低缓存复用固定显示前三项。排序结果只展示安全短标签和数值，不展示完整 Thread ID、响应身份、Prompt、标题、项目名或路径。
- 缓存复用按会话有效 Input 与 Cached 数字加权计算；缺失 Cached 或 Input 为零的响应不参与比率。完整、有限、部分、无观测和不可用状态沿用已观测用量的覆盖说明，旧 v3 Token 行仍不进入严格排名。
- 30 天 200,000 条规范响应、20,000 个会话的单次查询与聚合保持有界字段投影、`fetchmany` 和流式归并；界面只消费结果 DTO，不在 Tk 主线程重新查询、排序或去重。应用版本仍为 `0.1.0`。

## Phase 3.1-C 全局真实用量汇总与范围澄清

- “当前指令 / Current response”继续使用最新可靠响应观测；“当前会话 / Current session”继续使用当前 Thread 权威累计数字，二者不从历史窗口反推。
- “已观测用量 / Observed usage”按今日、最近 5 小时、最近 7 天和最近 30 天汇总本机历史库中的规范响应观测。今日使用系统本地日历边界；滚动窗口使用实际经过时间，边界两端均包含。
- 汇总层只计算带安全响应身份的终态 Token 字段，不累计进行中快照或 `session_total_tokens`，不把 Quota 行加入 Token，不把缺失或无效字段当作零。Mini、Dashboard、状态转换和 post-complete 更新按同一响应归并；不同响应不会因数字相同而合并。
- 每项 Token 指标保留有效、缺失和无效记录计数；界面区分完整本地历史、有限历史、部分覆盖、无观测、未知和不可用，并单独显示 fresh、stale 或 unavailable。
- 官方 5 小时与每周 Quota 保持独立区域和百分比语义；本地 Token 汇总不等同于 Codex 官方账单或额度，也不从 Quota 反推 Token。
- 历史 Schema 为 v4：只新增由 `Thread ID + turn_id` 域分隔 SHA-256 得到的 `response_safe_id`，不保存原始 `turn_id`。旧 v3 Token 行保持身份未知，不猜测回填、不进入严格总和，并明确降低覆盖；Quota 历史保持可用。30 天汇总使用有界字段投影、`fetchmany` 和逐响应流式归并，不新增历史数据库、网络请求、遥测、导出或云能力。应用版本仍为 `0.1.0`。

## Phase 3.1-B2 真实趋势与优化建议

- 手动刷新和 60 秒自动刷新复用同一个安全数字归一化与历史写入入口；迷你组件模式也复用该入口。
- 真实历史保存在 Token Monitor 自有用户数据目录的 `data/usage-history.sqlite3`，不写 Codex SQLite、Rollout、`.codex` 或项目仓库。
- SQLite v1 迁移可重复执行；稳定 SHA-256 指纹与数据库唯一约束防止同一观测重复写入。额度值或可靠重置身份可独立触发新样本，本地刷新时间不参与指纹。
- 历史默认保留 90 天且最多 200,000 行；自动清理只作用于趋势样本表，不提供手动清理按钮。
- 概览与完整趋势页共用本地查询层，支持 7/30/90 天、当前 Thread 数字与明确标记的全局额度。主图使用响应式 Tk Canvas、峰谷保留降采样和完整数值 Tooltip，不生成假曲线。
- Advisor v1 阈值、优先级和 severity 不变；仅在同一 Thread 至少 5 个有效样本、3 个可靠观测时间时显示上升、下降或大致持平的辅助证据。

## Phase 3.1-B1 产品体验

- 左侧一级导航固定为六项：概览、会话、用量趋势、建议、工具、设置；会话详情作为二级页面或宽屏侧栏呈现。
- 概览默认显示六项核心指标、当前会话、额度状态、趋势预览、四项快捷操作和最近 5 条会话。
- 会话页保留 7/30/90 天、每页 10 条、分页、状态筛选和安全标题搜索；行切换只读取完整刷新留下的进程内 snapshot。
- 用量趋势只展示真实样本，并明确区分 `available`、`insufficient`、`unavailable` 与 `stale`；样本不足时不绘制伪造曲线。
- 建议页只展示 Advisor v1 已生成的建议与安全数字证据；工具页按诊断、数据、工作流、帮助分组，未实现的数据操作统一标记为 Coming soon。
- 设置页按通用、刷新与通知、Windows、桌面组件、隐私与关于分组；刷新周期固定为 60 秒，不新增无效阈值或伪配置。
- 桌面组件继续只保留 `compact` 与 `expanded` 两种显示状态；切换导航、语言、会话和组件状态均不触发数据读取或重建 Tk root。

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

Phase 3.1-C 的历史层已升级为 v4，并用安全 `response_safe_id` 提供全局响应用量窗口汇总与覆盖说明；旧 v3 Token 行保留但不进入严格总和。范围切换在后台只读查询本地历史，不刷新 Codex 来源、不写历史。Phase 3.1-B2 的趋势、Advisor、额度事件序列与 Mini/Dashboard 去重语义保持不变。应用版本仍为 `0.1.0`。

Phase 2.8-A-S 保留多 Thread 分离、500 条近期候选、已知路径回读和 Rollout 进程内缓存规则。近期列表每页 10 条，可在内存中翻页；点击近期行或下拉任务时直接切换完整刷新留下的快照，不重新读取 Rollout、SQLite、标题或额度。默认选择仍可自动跟随最近活动；主窗口最小化时会把当时实际选中的 Thread 解析为固定选择。近期范围默认 7 天，可切换 30 或 90 天。

The current Dashboard no longer loads or writes the legacy manual Runs JSON and no longer shows manual Run input, saved-Run, or report-export controls. `AgentRun`, `app/storage.py`, `app/reporting.py`, existing Runs JSON, historical reports, and their compatibility tests remain preserved.

Thread titles use the installed Codex `app-server` structured `Thread.name` field. One full refresh makes one `thread/list` batch request over the same persistent app-server connection used by quota reads; the selective parser decodes only `id` and `name`, never `preview`, turns or message content. The UI never guesses whether a name looks like a prompt or path; only missing, invalid, empty, unparsable, or absent names use `Codex 会话 · MM-DD HH:mm` / `Codex Session · MM-DD HH:mm`. Long names are safely truncated only at the display boundary. See [Rollout instruction usage](docs/rollout-instruction-usage.md).

### Historical Phase 2.4 logs adapter notes

Phase 2.4-B presented the latest `response.completed` values from `logs_2.sqlite`; this is historical behavior, not the current Dashboard usage source.

Phase 2.4-C added a stateful logs reader and optional low-frequency automatic refresh. This is historical adapter behavior; the current Dashboard uses Rollout sessions and a fixed optional 60-second refresh.

By default, usage, cost, cache, context, and budget values remain 本地估算 / local estimate. When available, the optional read-only `state_5.sqlite` adapter reads only safe fields and labels `threads.tokens_used` as `codex_state_sqlite / real total` for session total tokens only. The optional read-only `logs_2.sqlite` adapter uses SQLite JSON1 to extract only the latest `response.completed` numeric usage fields; Python receives no event body or body substring, and labels the values `codex_logs_sqlite / real usage`. It accepts only a complete JSON event with root `usage`, or the confirmed `SSE event: ` format with `response.usage`; both require a root `type` of `response.completed`, so keywords and fake usage objects inside content are ignored. Unavailable data uses `unknown`. Cache hit is derived from those numbers, not an official cache hit rate, and current/session cost remains an estimate, not billing. Set `CODEX_STATE_DB` and `CODEX_LOGS_DB` to configure database paths.

The legacy logs adapter reports `connected`, `database missing`, `open failed`, `no response.completed`, or `parse failed`, but is not on the current Dashboard usage path. A per-user Windows installer is available; automatic updates and Codex Desktop embedding are not implemented. The repository remains independent until integration is explicitly planned.
