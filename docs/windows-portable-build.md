# Windows 便携版构建

本阶段使用 PyInstaller `one-folder` 与 `windowed` 模式生成 Windows 便携版，不是安装器。

```powershell
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

构建成功后，双击 `dist\CodexTokenMonitor\CodexTokenMonitor.exe`。目标电脑无需安装 Python、pip 或 CustomTkinter。

用户数据默认写入 `%LOCALAPPDATA%\CodexTokenMonitor`；删除便携版目录不会自动删除这些数据。可用 `CODEX_TOKEN_MONITOR_DATA_DIR` 覆盖本项目自身的可写数据根目录，此变量不会改变 Codex SQLite 路径。

当前未实现安装器、代码签名、自动更新、开机启动和系统托盘。Windows 可能对未签名的新 exe 显示安全提示，留待后续发布阶段处理。
