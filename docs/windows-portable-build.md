# Windows 便携版构建

本阶段使用 PyInstaller `one-folder` 与 `windowed` 模式生成 Windows 便携版。当前用户级安装器以该完整目录为唯一程序载荷，二者共用同一应用代码与版本来源。

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

构建成功后，双击 `dist\CodexTokenMonitor\CodexTokenMonitor.exe`。目标电脑无需安装 Python、pip 或 CustomTkinter。

额度卡依赖目标电脑已安装的 Codex Desktop / Codex CLI。程序会定位本机 Codex 自带的 `codex.exe`，通过官方本地 `app-server` 读取结构化的 5 小时与每周限额数字；不会把认证凭据复制到便携版目录。未找到兼容 Codex 命令或额度暂不可用时，卡片显示 `—`，Thread Token 监控仍可独立使用。

用户数据默认写入 `%LOCALAPPDATA%\CodexTokenMonitor`；删除便携版目录不会自动删除这些数据。可用 `CODEX_TOKEN_MONITOR_DATA_DIR` 覆盖本项目自身的可写数据根目录，此变量不会改变 Codex SQLite 路径。

构建会打包 `customtkinter`、`pystray`、`Pillow`、Tk/Tcl、项目自有托盘资源与应用图标。便携 EXE 可选择把自身的带引号绝对路径写入当前用户 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`；默认关闭，源码模式不可用。系统托盘和启动设置见 [Windows 托盘和启动设置](windows-tray-and-startup.md)，安装流程见 [Windows 用户级安装器](windows-installer.md)。当前仍没有代码签名或自动更新。构建采用 `windowed` 模式，正常运行不显示控制台窗口。
