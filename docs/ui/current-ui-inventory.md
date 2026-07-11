# 当前 UI 与数据能力盘点

本文只记录当前运行时代码已经具备的界面和数据能力，不包含后续 UI 改造建议。

## 窗口与区域

- 默认窗口约为 1180×760；最小尺寸约为 980×660。
- 顶部操作区：Start Run、End Run、Save Run、Manual Refresh、Auto Refresh、Export Report。
- Run 手工输入区：Project、Title、Session ID、Model、Mode、Input Tokens、Output Tokens、Cached Tokens、开始/结束时间，以及 prompt/output/note 摘要。
- Session Summary：会话轮数、Session Total、当前 Run 总量、成本、cache hit、context 与 budget 本地值。
- Latest Response Usage：最新 response 的数值 usage 与日志适配器元数据。
- Manual Saved Runs：当前显示最近保存的手工 Run。
- telemetry bar：底部横向显示汇总的本地 telemetry 值。

## 当前真实操作

| 操作 | 当前结果 |
| --- | --- |
| Start Run | 记录本次手工 Run 的开始时间。 |
| End Run | 记录本次手工 Run 的结束时间。 |
| Save Run | 将表单中的手工 Run 保存到本地 runs 数据。 |
| Manual Refresh | 刷新 runs、logs 与 state 数据快照。 |
| Auto Refresh On/Off | 默认 Off；开启后固定每 60 秒刷新。 |
| Export Report | 导出 Markdown 报告。 |

Auto Refresh 只刷新显示：不保存 Run、不增加 Recent Runs、也不导出报告。关闭窗口时会取消待执行回调，并避免重叠刷新。

## 数据来源与语义

| 来源 | 当前用途 | 语义 |
| --- | --- | --- |
| `codex_logs_sqlite / real usage` | 最新完整 response usage | real usage；只用于最新 response，不聚合 session/thread。 |
| `codex_state_sqlite / real total` | 最新 thread 的 Session Total | real total；仅适用于 Session Total。 |
| `local estimate` | 手工 Run、成本、context、budget、无 real 回退 | estimate，不是 billing。 |
| `unknown` | 缺失、无完成 response 或适配器失败 | 未知值，不能表示为真实的 0。 |

Latest Response Usage 当前可展示：Input Tokens、Output Tokens、Total Tokens、Cached Tokens、Reasoning Tokens，以及 Derived Cache Hit。Derived Cache Hit 在有 logs usage 时由 real usage 派生，但不是官方 cache hit rate；否则为本地 estimate 或 unknown。

## Session 与技术状态

- Session Total 的来源可为 `codex_state_sqlite / real total` 或 `local estimate`。
- Usage Source 标识最新 usage 的来源；Session Source 标识 Session Total 的来源。
- logs adapter 当前显示状态、Latest Event 与 Last Refresh（或刷新尝试时间）。
- state adapter 当前作为 Session Total 的读取来源参与汇总；当前界面没有独立的 State Adapter 状态行。
- 成本、context、budget 和平均 cache hit 均为 estimate；不是 billing，也不应被描述为真实 Codex 指标。

## Manual Saved Runs 边界

Manual Saved Runs 只包含用户明确执行 Save Run 后保存的 `AgentRun`。它不是自动历史，也不表示自动刷新期间出现的 usage 记录。当前 `AgentRun` 可用字段包括标题、模型、模式、输入、输出、缓存、总量及保存所需时间信息；实现层不得为匹配视觉参考而新增虚构字段。
