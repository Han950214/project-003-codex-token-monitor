# Codex Token Monitor 设计合同

本文件是 Phase 2.5-UI-B 的唯一设计合同（v2：炫酷化修订）。当前运行时数据合同优先；本合同只冻结视觉层级、状态表达和桌面 View 翻译边界。

## 产品定位

Codex Token Monitor 是 Windows 本地 token usage 监控工具：暗色霓虹、高能见度、氛围化，但信息密度与数据可读性优先于一切装饰。炫酷服务于"token 消耗状态一眼可见"这个产品核心，不是为炫而炫。View 层使用 CustomTkinter，表格保留 `ttk.Treeview`；它不是 SaaS 后台、不是完整 Codex 工作台，也不模仿 Reasonix。

## 冻结参考

![Dashboard reference](references/dashboard-normal-real-usage.png)

![Telemetry states](references/telemetry-bar-all-states-comparison.png)

- Dashboard 图片只作为正常 `Real Usage` 状态的视觉参考。
- telemetry 图片只作为状态栏结构和状态表达参考。
- 其他状态从下文状态矩阵在同一 Dashboard 布局中派生，不参考已丢弃页面。
- 图片中的数据完全虚构，不得进入运行时默认数据。
- 参考图中的导出按钮文案不是实现合同；现有操作固定为 `Export Report`，报告格式为 Markdown。

## Design Tokens

| 角色 | 冻结规则 |
| --- | --- |
| Primary font | Segoe UI |
| Fallback | system Tk default |
| Spacing grid | 4px |
| Common spacing | 4 / 8 / 12 / 16 / 24 |
| Visual corner radius | 8–12px |
| Border | 1px low-contrast border |
| Shadow | Canvas 叠层模拟：2–3 层深色偏移矩形；禁止依赖系统阴影或任何第三方特效库 |
| Gradient | 仅允许 tk.Canvas 离散色阶模拟（逐行/分段填色）；单张卡片渐变不超过 2 色阶过渡；禁止引入第三方渐变库 |
| Glass effect | 仅允许模拟：暗色半透明色块 + 顶部 1px 高光描边；Tk 颜色无 alpha 通道，禁止宣称真实 blur |
| Animation | 允许 `after()` 驱动的轻量动画：hover 过渡、刷新脉冲点、数字滚动、卡片光晕呼吸；帧率 ≤30fps、单段时长 ≤300ms、提供总开关 |

颜色按语义角色集中定义，而不是复制网页色值：window background、surface、raised surface、border、primary text、secondary text、accent、real/fresh、estimate/warning、stale、error、unknown/disabled。所有状态必须同时使用文字与颜色；颜色从不单独表达状态。霓虹语义色板由 `app/ui_theme.py` 中央 token 统一提供（electric blue accent、每指标独立彩色 accent、明暗双色对），组件内禁止硬编码色值。

## 炫酷护栏（酷要有边界）

- 所有特效必须由 `app/ui_theme.py` 中央 token 驱动；禁止在组件中散落硬编码色值。
- 动画不阻塞数据刷新、不改变数值显示时序：`Refreshing` 仍保留旧值、防重叠刷新合同不变。
- 状态永远"文字 + 颜色"双通道；发光、渐变、脉冲是增强表达，不能取代文字标签。
- 动画帧率 ≤30fps、单段 ≤300ms；提供 Effects 总开关，关闭后所有 Canvas 特效静默，核心布局不受影响。
- 炫酷不新增数据字段、不改变产品文案、不引入虚构数据；`Export Report` 输出内容不变。
- 所有效果必须在 CustomTkinter / ttk / tk.Canvas 能力内实现；不得新增任何第三方依赖。

## Dashboard 信息层级

1. 顶部状态与操作区：`Codex Token Monitor`、当前数据状态、Last Event、Last Refresh、Manual Refresh、`Auto Refresh: On/Off (60s)`。
2. 第一层级：Latest Response Usage；Total Tokens 适度突出；Input、Output、Total、Cached、Reasoning、Cache Hit 横向可比较；同时显示 `Derived from real usage` 或 estimate 说明。
3. 第二层级：Session Total、Usage Source、Session Source、Logs Adapter、State Adapter、时间与 freshness。
4. 第三层级：Manual Saved Runs，以及手工 Run 输入、保存和报告功能。

不采用固定宽度的大型导航区域；内容使用可扩展网格。

## 文案与 Manual Saved Runs

产品名称只能是 `Codex Token Monitor`。手工记录区标题固定为 `Manual Saved Runs`，并显示：

> Only runs explicitly saved by the user appear here.

建议列为 Title、Model、Mode、Input、Output、Cached、Total、Saved At。最终实现必须以当前 `AgentRun` 数据模型为准；不得为了匹配图片新增字段。

## Dashboard 状态矩阵

所有状态共享同一 Dashboard 布局，不能生成完全不同的页面。

| 状态 | 数据与显示合同 | 可用操作 |
| --- | --- | --- |
| Real Usage | 真实数值可用；显示来源与时间，并标记 `Real Usage`。 | Manual Refresh；Auto Refresh On/Off。 |
| Estimate | 显示 `Local Estimate`，不得伪装成真实 usage。 | Manual Refresh；Auto Refresh On/Off。 |
| No Data / Unknown | 数值显示 `—` 或 `Unknown`，绝不显示为 0。显示 `No response usage is available yet.` 与 `Use Manual Refresh or wait for Codex usage data.`；无数据不等于 adapter error。 | Manual Refresh；Auto Refresh On/Off。 |
| Refreshing | 保留旧值；顶部只显示一次 `Refreshing…`，并显示 `Previous values remain visible while new usage is loaded.`；保持防重叠刷新合同。 | 不重复触发刷新；Auto Refresh 状态可见。 |
| Stale | 保留旧值并标记 `Stale Data`；显示 Last Event 与 Manual Refresh。不定义固定 stale 时长阈值。 | Manual Refresh；Auto Refresh On/Off。 |
| Logs Adapter Error | logs usage 指标为 Unknown，或保留旧值并标记 Stale；若 state 可用，Session Total 仍可显示。 | Manual Refresh；不提供额外连接操作。 |
| State Adapter Error | 若 logs 可用，Latest Response Usage 继续显示；Session Total 为 Unknown 或当前真实存在的 estimate 回退，且明确显示 Session Source。 | Manual Refresh；不提供额外连接操作。 |
| Auto Refresh Off | 正常状态，不是错误；显示 `Auto Refresh: Off (60s)`。 | Manual Refresh 可用。 |
| Auto Refresh On | 显示 `Auto Refresh: On (60s)`；使用克制的活动状态，不改变 60 秒合同。 | Manual Refresh 可用。 |
## telemetry bar 合同

字段顺序固定为：

1. Codex Token Monitor
2. Current Total
3. Cache Hit
4. Session Total
5. Data Status
6. Auto Refresh

所有状态保持相同高度、字段顺序、基本宽度与数字对齐；只改变状态文字、缺失值和语义提示色（霓虹语义色，由 `app/ui_theme.py` token 驱动，始终与文字标签成对出现）。至少支持 `Fresh · Real`、`No Data`、`Refreshing`、`Stale`、`Logs Error`、`State Error`。不在 telemetry bar 复制 Dashboard 的全部 adapter 技术字段。

## 窗口与缩放

- Reference size：约 1180×760。
- Minimum size：约 980×660。
- 核心 token 数值在最小尺寸仍可读。
- 区域使用合理的 grid 权重扩展；Manual Saved Runs 可纵向扩展。
- telemetry bar 保持固定且紧凑的高度。
- 不依赖网页响应式布局。

## 桌面 View 翻译边界

CustomTkinter 是唯一允许的轻量第三方 View 层依赖；卡片、按钮、输入、开关、分段标签和主要容器优先使用其原生组件。`ttk.Treeview` 可继续承担表格，集中式 `ttk.Style` 只用于统一表格外观。

不得复制 HTML/CSS/JavaScript；不得改用 Electron、WebView、Qt、React 或浏览器 UI；不得引入其他第三方 UI 框架；不得以复杂 Canvas 重建整个页面布局或模拟网页浏览体验；渐变、毛玻璃模拟、阴影与动画仅允许 Design Tokens 与炫酷护栏限定的 CustomTkinter / tk.Canvas 内实现方式。数据、adapter、storage、refresh、report 与 presenter 合同保持不变，参考图中的虚构数据不得进入运行时。
