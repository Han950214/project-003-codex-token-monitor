# Windows 用户级安装器

Windows 安装器基于现有 PyInstaller one-folder 便携版构建。便携版可直接解压运行；安装版则提供当前用户级安装、开始菜单快捷方式、可选桌面快捷方式和标准卸载程序。两者都不要求目标电脑安装 Python。

安装器使用 Inno Setup 6，默认目录为 `%LOCALAPPDATA%\Programs\CodexTokenMonitor`，不请求管理员权限、不写 HKLM、不修改系统 PATH。安装结束页可选择运行应用；静默安装不会自动启动应用，也不会自动创建桌面快捷方式或启用“随 Windows 启动”。

用户数据仍保存在 `%LOCALAPPDATA%\CodexTokenMonitor`。卸载会删除程序文件、安装器创建的快捷方式和本项目的 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下 `CodexTokenMonitor` 值，但默认保留用户数据，也不会触碰 `.codex`、Codex SQLite、Rollout 或其他项目数据。若需彻底清理，请在卸载后手动删除 `%LOCALAPPDATA%\CodexTokenMonitor`。

应用继续使用 `Local\CodexTokenMonitor.SingleInstance` 单实例互斥量。安装版和便携版同时运行时也视为同一个应用实例。首次安装不会启用开机启动；用户在应用设置中启用后，HKCU Run 值会指向当前安装目录中的 `CodexTokenMonitor.exe`。

## 构建

自行安装 Inno Setup 6，并确保 `ISCC.exe` 位于 PATH、常见安装目录，或在当前 PowerShell 进程设置：

```powershell
$env:INNO_SETUP_COMPILER = 'E:\Inno Setup 6\ISCC.exe'
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1
```

脚本先重建并 smoke 验证便携版，再生成 `dist\installer\CodexTokenMonitor-Setup-<version>.exe` 及对应 `.sha256` 文件。可用以下命令校验：

```powershell
Get-FileHash -Algorithm SHA256 .\dist\installer\CodexTokenMonitor-Setup-<version>.exe
```

安装器当前未签名，Windows 可能显示 SmartScreen 提示。当前没有代码签名、自动更新或在线更新检查。应用只读取既有安全数字元数据，不读取或保存凭据、prompt、response、preview、message、tool output 或 reasoning 正文。
