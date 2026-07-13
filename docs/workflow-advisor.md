# 工作流建议 v1

Advisor v1 是独立、确定性的纯数字规则模块。输入只包含安全数字、时间和状态代码；输出包含 `code`、`severity`、翻译键、主操作、数字 evidence 和观察时间。Evidence 白名单拒绝 Prompt、Response、Preview、Message、Tool Output、Reasoning 正文或任意自由文本。

## 输入字段

- 当前指令 Input、Total、Cached；
- 当前会话累计 Total；
- Thread 的 `task_started` 唯一轮次数；
- 5 小时和每周可靠剩余百分比；
- quota/source 状态；
- 当前选择状态与完整刷新时间。

缓存复用比例按 `Cached / Input × 100` 本地推导，不是官方缓存命中率。额度剩余和重置时间仅在 provider 已验证完整窗口、且数据未陈旧时参与规则；Advisor 不预测可用时长。

## 优先级

1. `data_unavailable`
2. `quota_risk`
3. `new_thread`
4. `optimize`
5. `normal`

极简首页只显示最高优先级建议；高级模式最多显示三条当前建议。同一输入总是得到相同顺序和输出。

## 集中阈值与规则

阈值定义在 `app/advisor.py`：

| 规则 | 阈值 | 结论 |
|---|---:|---|
| 数据不可用 | 当前安全指令数字不可用 | `data_unavailable` |
| 数据陈旧 | 完整刷新时间超过 3 分钟 | `data_unavailable` |
| 额度风险 | 任一可靠额度剩余 ≤ 15% | `quota_risk` |
| 长会话 | 唯一轮次 ≥ 30 | `new_thread` |
| 缓存复用优化 | Input ≥ 60,000 且本地推导复用 < 20% | `optimize` |
| 回退 | 未触发以上规则 | `normal` |

这些阈值是产品提示规则，不是模型上下文上限、质量预测或 OpenAI 官方门槛。文案只使用“建议”“可能”“观察到”“当前数字显示”，不声称质量必然下降、精确节省 Token 或准确预测额度时长。

## 轮次来源

Rollout reader 只对已读取的 `task_started` 事件按 `turn_id` 去重计数，不读取事件正文。旧 DTO 构造保持兼容，缺失轮次时显示 `—` 并跳过长会话规则。
