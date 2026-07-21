# Codex Token Monitor

Codex Token Monitor 是一个本地优先的 Windows 桌面监控工具，用于只读展示 Codex 会话的安全数字元数据。它不读取或保存 Prompt、Response、消息、Tool Output 或 Reasoning 正文，也不扫描项目文件。

## 当前产品结构

主界面固定为四个一级入口：

1. 概览 / Overview
2. 会话 / Sessions
3. 用量趋势 / Usage Trends
4. 设置 / Settings

会话详情是二级页面。页面切换、会话选择、语言切换和桌面组件展开或收起只复用内存中的快照，不触发新的 Codex、Rollout 或 SQLite 读取。

概览采用单列全宽结构，顺序固定为：会话选择器、核心指标、官方额度、已观测用量、趋势预览。核心指标与额度来自同一次完整刷新；历史汇总只查询本产品自己的只读本地历史。

设置页保留通用、刷新、Windows、桌面组件、隐私和版本信息。当前版本不包含自动更新入口。

## 数据与隐私边界

- Rollout JSONL 提供会话与 Token 数字；结构化 `app-server` 接口提供官方 `Thread.name` 和额度。
- `state_5.sqlite` 只用于安全元数据；`logs_2.sqlite` 仅保留为旧版兼容适配器。
- Reasoning 只读取 Token 数量，不读取正文。
- 不读取或保存 Cookie、Token、Authorization Header、API Key 或 Session Secret。
- 本地已观测用量不等于官方账单或额度，也不会从额度反推 Token。
- 本项目不创建知识库、项目记忆或项目上下文，不依赖也不替代 AOS。

## 桌面组件与系统托盘

桌面组件提供 `compact` 和 `expanded` 两种显示状态。它只显示事实状态、所选会话的安全标题与数字、5 小时额度，以及恢复主界面、刷新、展开/收起和退出操作。系统托盘支持恢复主界面、显示组件、隐藏窗口、刷新、切换自动刷新、打开设置和退出。

## 快速验证

```powershell
python -m unittest discover -s tests
python -m compileall app scripts tests
python app\main.py --smoke
python app\main.py
```

## 从源码运行

```powershell
python -m pip install -r requirements.txt
python app\main.py
```

## Windows 构建

```powershell
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

便携版输出到 `dist\CodexTokenMonitor\CodexTokenMonitor.exe`。安装版需要 Inno Setup 6：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1
```

安装与卸载细节见 [Windows 用户级安装器](docs/windows-installer.md) 和 [Windows 便携版构建](docs/windows-portable-build.md)。产品交互见 [产品交互](docs/product-interaction.md)，桌面组件见 [桌面迷你组件](docs/desktop-mini-widget.md)，安全边界见 [AOS 边界](docs/aos-boundary.md)。

## 项目目录

```text
app/        CustomTkinter 桌面应用与数据展示逻辑
docs/       产品、UI、数据与安装说明
resources/  旧版兼容样例与构建资源
scripts/    构建、验收与性能脚本
tests/      单元、契约、GUI 与隐私验证
```

应用版本保持为 `0.1.0`。当前仍不包含自动更新、额度预测、账户切换或云端同步。
