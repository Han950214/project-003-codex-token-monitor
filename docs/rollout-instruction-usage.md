# Rollout 指令 Token Usage

当前 usage 数据源是 `%USERPROFILE%\.codex\sessions\YYYY\MM\DD\rollout-*.jsonl`，也可用 `CODEX_SESSIONS_DIR` 覆盖。Reader 从最多 500 条近期候选记录中发现多个近期 Thread，按最新合法事件时间排序；文件 mtime 只用于候选预排序。Reader 只读取事件名、Thread/turn 标识、时间和安全的数字 token 元数据。

- 模型调用：`last_token_usage` 是一次模型调用；累计向量增量必须与它完全对账。
- 指令：同一 `turn_id` 从 `task_started` 到 `task_complete` 聚合全部已验证调用。重复累计快照会去重；累计下降会开启新 epoch；无法对账的事件不进入精确总数。
- Thread：`total_token_usage` 与 state SQLite 只用于同一 Rollout Thread 的累计总计。
- 多会话：每个 Rollout 独立解析；相同 Thread 的多个 Rollout 只保留最新合法事件所属记录，不相加；不同 Thread 的指令和累计 Token 永不混合。
- 选择：默认自动跟随最近活动 Thread；用户选择任务或点击近期会话行后按内部 Thread ID 固定。已固定 Thread 跌出近期候选后仍通过已知 Rollout 路径定向读取，真正不可用时保留固定状态且不跳转。
- 近期会话：默认显示最近 7 天，可切换为 30 或 90 天；这仍是最多检查 500 条 Rollout 记录的近期视图，不是 500 个会话或完整历史。会话总 Tokens 来自 Thread cumulative usage；会话缓存命中率来自 Thread cumulative Cached/Input。任务是否结束和指令 Usage 是否精确是两个维度：exact 完成显示“已完成”，已结束但非 exact 显示“已完成（部分数据）”，缺少结束边界且超过 10 分钟没有合法事件时显示“数据不完整”。10 分钟规则只是本地活跃度启发式，不表示检测到窗口关闭。数据不完整的行仍可选择和查看；只有暂不可用的行不能新固定。固定任务跌出 500 条候选后仍按已知路径定向读取；未变化 Rollout 复用进程内解析缓存。
- 标题：不再读取 State `threads.title`。完整刷新通过共享的 Codex `app-server` 连接执行一次 `thread/list` 批量请求，只解码结构化 `id` 与 `name`；`preview`、turn 和正文值会被跳过而不解码。正文式或路径式名称会被拒绝，缺失时使用安全时间回退。会话点击与翻页不发标题请求。
- 快速切换与分页：完整刷新后保留各 Thread 快照；列表和下拉选择只在内存中切换，缓存未命中才执行普通完整刷新，且选择动作不刷新额度。每页 10 条，上一页/下一页只切片内存列表；固定任务在页外时仍保留在下拉框。

只有具备 turn 前 baseline、完整边界和全部累计对账时，指令才标为 `exact`。进行中的指令可显示已验证增量，但仍会增长。Reader 不保存 prompt、用户或助手消息、tool 输出、reasoning 内容或原始 JSON 行；最终可见文字的 Token 目前不可获得。

`logs_2.sqlite`、手动 Runs 存储和旧报告模块保留为 legacy compatibility，但都不在当前 Dashboard 启动、手动刷新或自动刷新链路中；旧报告入口已隐藏。
