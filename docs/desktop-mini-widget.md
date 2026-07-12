# 桌面迷你组件

## 行为

用户最小化主 Dashboard，或点击主界面的“显示迷你组件”按钮后，主窗口会隐藏，并显示一个单进程 `CTkToplevel` 迷你组件。组件默认位于主窗口所在显示器的工作区右上角，保留 16 px 边距并避开任务栏；Windows 工作区 API 不可用时回退到 Tk 主屏幕尺寸。

迷你组件默认置顶，并以 82% 不透明度显示；文字和数值始终可读，鼠标进入组件后恢复为 100% 不透明，离开后重新变为半透明。它不使用全屏覆盖、透明点击穿透或第二个 Tk root。标题栏可拖动；位置仅以安全的 x/y 坐标保存在现有 UI settings 中，失效时回退右上角。恢复按钮或双击标题栏会隐藏组件并恢复原 Dashboard；恢复操作只切换窗口，不触发 Rollout、SQLite 或额度数据重新查询。

点击主窗口关闭按钮或迷你组件退出按钮时，会显示“最小化到任务栏 / 退出应用”选择框。默认焦点和 Enter、Escape、关闭对话框都选择最小化到任务栏；此时迷你组件隐藏，主窗口以标准 Windows 任务栏图标保持最小化，点击任务栏图标即可恢复。勾选“今天不再提示”后，仅在本机 UI settings 中保存当天日期与所选动作；次日自动恢复询问，不保存任何账号或认证信息。

## 显示内容

- 5 小时窗口：已使用百分比、剩余百分比、进度条和重置时间。
- 每周窗口：已使用百分比、剩余百分比、进度条和重置时间。
- 选中 Thread：最小化前固定 Thread 的累计 Total、短名称和状态。
- 底部状态：最后更新时间、额度状态和手动刷新。

自动跟随模式会在最小化瞬间解析为具体 Thread。组件期间只刷新该 Thread；其他 Thread 的新事件不会触发跳转。固定 Thread 跌出 500 条近期候选后，仍使用已知 Rollout 路径读取。没有可用选择时显示“暂无选中线程”和 `—`，不会选择其他 Thread 冒充。

百分比默认表示“已使用”。内部值为 `float | None`，范围钳制到 0–100；NaN、Infinity 和 used/remaining 不一致会标记为异常。未知值保持 `None`，UI 显示 `—`。重置时间内部使用带时区的 `datetime`，显示时转换到用户本地时区。

## 额度来源与安全边界

额度来源是本机已安装 Codex 提供的官方结构化 `app-server` 方法 `account/rateLimits/read`。Provider 只接收以下安全字段：

- `windowDurationMins`
- `usedPercent`
- `resetsAt`

`300` 分钟映射到 5 小时窗口，`10080` 分钟映射到每周窗口。窗口时长不明确时不会猜测映射。来源响应由 Codex 自己处理认证；本项目不读取、保存或输出 Cookie、Access Token、Refresh Token、Authorization Header、API Key 或 Session Secret，也不打印原始响应。

本项目仍不读取 prompt、response、preview、user/assistant message、tool output 或 reasoning 正文。额度 DTO 不包含凭据字段，也不创建持久化额度快照。刷新失败时仅保留进程内上一份数据并标记为陈旧。

## 当前不包含

系统托盘、开机启动、自动更新、额度预测、Credits、账户切换、多账户合并和云端同步均不在本阶段范围内。
