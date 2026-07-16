---
name: token-monitor-reference-ui-implementation
description: "当 Codex Token Monitor 任务包含 Stitch 图、桌面 UI 截图或已批准参考设计，且用户要求按参考图实现时，在当前 Python Desktop 技术栈中执行高保真实现与真实运行截图对比。没有可用参考图或仅请求自由设计时不要调用。"
---

# Token Monitor 参考图实现

独立分析当前任务提供的参考图，不复制无明确许可证仓库的 Skill、脚本、参考文本或实现。

## 流程

1. 确认可访问参考图、主参考窗口尺寸、DPI 和已批准范围；缺少必要图片时请求重新提供。
2. 盘点当前 Tkinter/CustomTkinter 组件、Dashboard、presenter、状态与可实现范围。
3. 提取布局、字体、卡片、颜色、间距、对齐、圆角、描边、阴影、图标及空、加载、错误和不可用状态。
4. 将不可直接实现的视觉效果转换成当前技术栈可维护的等价方案；不通过大量 Canvas 硬编码伪造整张 UI。
5. 保持 presenter 与数据语义不变，接入现有 snapshot 和刷新路径。
6. 真实运行桌面应用，按参考尺寸截图，列出差异并修正。
7. 再次截图，检查长数字、窗口缩放以及可实现范围内的 100%、125%、150% DPI 表现。

## 边界

- 不迁移到 PySide6、Electron、WebView 或其他框架，不新增依赖。
- 不复制第三方 Logo、品牌图标、文案和受保护素材。
- 不修改 usage 解析、Rollout、SQLite、日志读取、归并、额度、隐私或数据口径。
- 不自动安装 Pillow、Playwright、浏览器、系统软件或其他依赖。
