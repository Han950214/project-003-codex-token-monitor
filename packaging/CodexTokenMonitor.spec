from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


repo_root = Path(SPECPATH).resolve().parent
datas = collect_data_files("customtkinter")
datas.append((str(repo_root / "resources" / "pricing-config.sample.json"), "resources"))
datas.append((str(repo_root / "resources" / "tray-icon.xbm"), "resources"))
datas.append((str(repo_root / "resources" / "app-icon.ico"), "resources"))
hiddenimports = collect_submodules("customtkinter") + collect_submodules("pystray")

a = Analysis(
    [str(repo_root / "app" / "main.py")],
    pathex=[str(repo_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CodexTokenMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(repo_root / "resources" / "app-icon.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="CodexTokenMonitor",
)
