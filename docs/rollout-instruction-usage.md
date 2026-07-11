# Rollout 指令 Token Usage

当前 usage 数据源是 `%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl`，也可用 `CODEX_SESSIONS_DIR` 覆盖。Reader 只读取事件名、Thread/turn 标识和安全的数字 token 元数据。

- 模型调用：`last_token_usage` 是一次模型调用；累计向量增量必须与它完全对账。
- 指令：同一 `turn_id` 从 `task_started` 到 `task_complete` 聚合全部已验证调用。重复累计快照会去重；累计下降会开启新 epoch；无法对账的事件不进入精确总数。
- Thread：`total_token_usage` 与 state SQLite 只用于同一 Rollout Thread 的累计总计。

只有具备 turn 前 baseline、完整边界和全部累计对账时，指令才标为 `exact`。进行中的指令可显示已验证增量，但仍会增长。Reader 不保存 prompt、用户或助手消息、tool 输出、reasoning 内容或原始 JSON 行；最终可见文字的 Token 目前不可获得。

`logs_2.sqlite` 保留为旧适配器，但不再是 Dashboard 当前 usage 主来源。
