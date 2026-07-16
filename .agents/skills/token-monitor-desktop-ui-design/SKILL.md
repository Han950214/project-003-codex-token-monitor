---
name: token-monitor-desktop-ui-design
description: "当 Codex Token Monitor 任务涉及 Dashboard、telemetry bar、数据源状态、最近响应、线程累计、时间范围汇总、设置、导出、首次启动界面，或用户要求 UI 现代化、认为现有界面丑时，在当前 Python Desktop 技术栈内制定界面设计。纯 usage 解析、SQLite、日志读取或数据口径任务不要调用。"
---

# Token Monitor Desktop UI 设计

先检查当前 CustomTkinter/Tkinter 组件、presenter、view model、snapshot 与刷新路径。保持当前 Python Desktop 技术栈，不迁移框架。

## 设计流程

1. 明确当前视图要回答的问题、数据来源、时间范围、更新时间和数据质量。
2. 盘点现有组件、颜色、字体、间距、状态与可复用 presenter 输出。
3. 制定紧凑设计计划：指标层级、布局、颜色、字体、间距和一个服务于状态判断的克制识别元素。
4. 使用真实口径文案审查计划，移除让估算看似官方、让累计看似单次响应或让旧数据看似实时的视觉表达。
5. 覆盖首次启动、正常、空数据、数据源不可用、刷新中、自动刷新、部分未知和错误状态。

## 数据语义边界

- 明确区分单条响应、当前线程累计、时间范围汇总、官方额度与本地统计。
- 来源、观测时间、重置时间以及真实、估算、过期、未知状态必须清晰。
- 不使用视觉设计掩盖数据来源或可靠性不确定性。
- UI 任务不得修改 usage 解析、Rollout、Codex SQLite、历史 SQLite、日志读取、归并、额度或成本口径。
- 不读取或展示 Prompt、Response、消息、工具输出或 Reasoning 正文。
- 不迁移到 PySide6、Electron、WebView 或其他框架，不新增依赖；适配 Windows 常见 DPI 和窗口尺寸。
