# Windows 系统托盘与启动设置

Phase 2.8-B 为同一个 Dashboard 进程增加一个系统托盘图标。左键图标恢复并置前已有主界面；右键菜单提供打开主界面、显示迷你组件、隐藏到托盘、手动刷新、切换自动刷新、设置和退出。打开、隐藏、恢复、切换语言或打开设置只改变窗口状态，不读取 Rollout、SQLite、标题或额度；手动刷新和固定 60 秒自动刷新继续使用原路径。

“最小化到任务栏”会保留 Windows 任务栏图标；“隐藏到系统托盘”会同时隐藏主界面与迷你组件，只留下托盘图标。两者使用独立的 `taskbar` / `tray` 状态，不改变选中 Thread、7/30/90 天范围、语言或自动刷新开关。托盘“退出应用”是直接退出路径，不受当天记住的最小化选择影响，并会停止自动刷新、关闭 app-server、移除托盘图标和销毁 Tk 窗口。

设置窗口可选择下次启动模式：`dashboard`（默认）、`widget` 或 `tray`。每次启动仍只创建一个 Tk root、一个 Dashboard 和一个托盘控制器，只执行一次初始数据与额度读取；之后按设置显示对应窗口。没有可用 Thread 时，迷你组件的两项 Token 显示 `—`。

“随 Windows 启动”默认关闭，只支持 PyInstaller 便携 EXE。启用时仅写当前用户 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 下名为 `CodexTokenMonitor` 的值，内容是当前 EXE 的带引号绝对路径；不请求管理员权限、不写 HKLM、不触碰其他启动项。源码模式会禁用开关并明确提示不支持。移动便携目录后，旧路径不会被误报为已启用。

Windows named mutex 保证单实例。第二次启动不会创建 Dashboard 或托盘，也不会终止、扫描或读取已有进程；检测到首实例后安全退出。本阶段不实现跨进程唤醒。

系统托盘和启动设置不读取或保存账号、凭据、Token 数据、Thread ID、prompt、response、preview、message、tool output 或 reasoning 正文。当前仍没有安装器、自动更新、代码签名、云同步或系统通知推送。
