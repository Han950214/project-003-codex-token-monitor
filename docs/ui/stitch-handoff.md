# Stitch 设计交接

## 输入与采用范围

- 来源：用户提供的 `stitch_codex_token_usage_monitor.zip`，日期 2026-07-11。
- 采用：`dashboard_normal_real_usage/screen.png`，作为 Dashboard 正常 Real Usage 状态的视觉参考。
- 采用：`telemetry_bar_all_states_comparison/screen.png`，作为统一 telemetry bar 的结构和状态表达参考。
- ZIP 内的 `codex_token_monitor/DESIGN.md` 仅作风格输入；仓库中的 `docs/ui/DESIGN.md` 已按本项目真实数据与操作合同重新编写，不能视为原文副本。

## 丢弃页面

以下七个页面不进入项目设计合同：

- `dashboard_no_data`
- `dashboard_refreshing`
- `dashboard_stale_data`
- `dashboard_logs_adapter_error`
- `dashboard_state_adapter_error`
- `dashboard_auto_refresh_on`
- `dashboard_auto_refresh_off`

它们出现当前项目不存在的指标、操作或产品结构，因此属于产品语义漂移。未来状态必须在正常 Dashboard 的同一布局中，依据 `docs/ui/DESIGN.md` 的状态矩阵与当前运行时合同派生。

## 实现交接规则

- HTML、CSS、JavaScript 与 ZIP 文件均不进入项目。
- Phase 2.5-UI-A 必须先遵循当前运行时数据合同，再遵循修正后的 `docs/ui/DESIGN.md`。
- 参考图片只决定视觉层级、信息分组和状态表达；不能据此新增功能、指标或数据字段。
- `Manual Saved Runs` 只表示用户明确保存的 Run；自动刷新不会产生记录或报告。
