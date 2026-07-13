import re
import subprocess
import sys
import unittest
from pathlib import Path

from PIL import Image

from app.version import __version__


ROOT = Path(__file__).resolve().parents[1]
ISS = (ROOT / "installer" / "CodexTokenMonitor.iss").read_text(encoding="utf-8")
BUILD = (ROOT / "scripts" / "build_installer.ps1").read_text(encoding="utf-8")
HELPERS = (ROOT / "scripts" / "installer_helpers.ps1").read_text(encoding="utf-8")


class InstallerContractTests(unittest.TestCase):
    def test_version_is_valid_and_cli_uses_it_without_breaking_smoke(self):
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")
        result = subprocess.run(
            [sys.executable, str(ROOT / "app" / "main.py"), "--version"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), __version__)
        main_source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('parser.add_argument("--smoke"', main_source)

    def test_build_and_installer_share_single_version_source(self):
        self.assertIn('app\\version.py', HELPERS)
        self.assertIn('/DAppVersion=$version', BUILD)
        self.assertIn('AppVersion={#AppVersion}', ISS)
        self.assertNotIn(__version__, BUILD)
        self.assertNotIn(__version__, ISS)

    def test_per_user_x64_install_contract(self):
        self.assertIn("PrivilegesRequired=lowest", ISS)
        self.assertIn(r"DefaultDirName={localappdata}\Programs\CodexTokenMonitor", ISS)
        self.assertNotIn("Program Files", ISS)
        self.assertNotIn("HKLM", ISS)
        self.assertIn("ArchitecturesAllowed=x64compatible", ISS)
        self.assertIn("ArchitecturesInstallIn64BitMode=x64compatible", ISS)
        self.assertRegex(ISS, r"AppId=\{\{[0-9A-F-]{36}\}")

    def test_payload_shortcuts_launch_and_icons(self):
        self.assertIn(r'Source: "{#PortableSourceDir}\*"', ISS)
        self.assertIn(r'Name: "{group}\Codex Token Monitor"', ISS)
        self.assertIn(r'Name: "{autodesktop}\Codex Token Monitor"', ISS)
        self.assertIn('Tasks: desktopicon', ISS)
        self.assertIn('创建桌面快捷方式 / Create a desktop shortcut', ISS)
        self.assertIn('Flags: unchecked', ISS)
        self.assertIn('postinstall skipifsilent', ISS)
        self.assertIn(r"SetupIconFile=..\resources\app-icon.ico", ISS)
        self.assertIn(r"UninstallDisplayIcon={app}\{#AppExeName}", ISS)

    def test_uninstall_removes_only_own_startup_value_and_preserves_data(self):
        self.assertIn("[Registry]", ISS)
        self.assertIn("Root: HKCU64", ISS)
        self.assertIn('ValueType: none; ValueName: "CodexTokenMonitor"; Flags: uninsdeletevalue', ISS)
        self.assertNotIn("ValueData:", ISS)
        self.assertNotIn("CodexTokenMonitor\\data", ISS)
        for forbidden in (".codex", "TerminateProcess", "OpenProcess", "HKLM"):
            self.assertNotIn(forbidden, ISS)

    def test_build_script_is_offline_fail_fast_and_hashes_output(self):
        self.assertIn('$ErrorActionPreference = "Stop"', BUILD)
        self.assertIn('scripts\\build_windows.ps1', BUILD)
        self.assertIn('& $portableExe --smoke', BUILD)
        self.assertIn('Get-FileHash -Algorithm SHA256', BUILD)
        self.assertIn('dist\\installer', BUILD)
        self.assertIn('issue=inno_setup_compiler_missing', HELPERS)
        self.assertIn('$env:INNO_SETUP_COMPILER', HELPERS)
        self.assertIn('Get-Command ISCC.exe', HELPERS)
        combined = BUILD + HELPERS
        for forbidden in ("winget", "choco", "scoop", "Invoke-WebRequest", "git ", "[Environment]::SetEnvironmentVariable"):
            self.assertNotIn(forbidden, combined)

    def test_icon_contains_required_transparent_sizes(self):
        path = ROOT / "resources" / "app-icon.ico"
        with Image.open(path) as icon:
            self.assertEqual(
                icon.ico.sizes(),
                {(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)},
            )
            self.assertEqual(icon.convert("RGBA").getpixel((0, 0))[3], 0)

    def test_installer_has_no_update_download_or_credentials_logic(self):
        combined = ISS + BUILD + HELPERS
        for forbidden in ("Download", "WinHttp", "Authorization", "credential", "auto-update", "AutoUpdate"):
            self.assertNotIn(forbidden, combined)


if __name__ == "__main__":
    unittest.main()
