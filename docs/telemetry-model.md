# Telemetry Model

当前 Dashboard 使用只读来源和安全 DTO，字段名保持英文以便稳定集成。

## 当前响应与会话

当前响应包含 Input、Output、Total、Cached Input、Reasoning、完成状态与安全响应身份。当前会话包含 Thread 安全身份、官方 `Thread.name` 或安全回退标题、轮次、最近活动和权威累计 Token。

缺失字段保持未知，不当作零。旧 v3 Token 行没有严格响应身份，不进入规范全局总和。

## 已观测用量

本产品自己的历史库按响应安全身份归并完成观测，并支持 today、5h、7d、30d 范围。汇总只计算有效最终 Token 字段，不累加进行中快照、会话累计或额度行。每项指标同时保留有效、缺失与无效记录数，并标记覆盖与新鲜度。

## 额度

官方额度 DTO 只接受：

- `windowDurationMins`
- `usedPercent`
- `resetsAt`

300 分钟映射到 5 小时窗口，10080 分钟映射到每周窗口。未知窗口不猜测。额度和本地 Token 汇总保持独立，不相互换算。

## 历史兼容模型

仓库仍保留早期 `AgentRun`、`AgentStep`、`PromptArtifact`、`OutputArtifact`、`TokenEstimate`、`RepoSnapshot`、`WasteSignal`、`CacheRisk`、`PricingConfig` 和 `BudgetState` 类型，仅用于旧数据兼容，不是当前 Dashboard 的数据入口。

类似账单的旧字段均为本地估算，不能表述为真实账单、真实余额或保证的提供方用量。
