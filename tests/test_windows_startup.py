import sys
import unittest
from pathlib import Path

from app.windows_startup import RUN_KEY, VALUE_NAME, WindowsStartupAdapter


class _Key:
    def __init__(self, registry):
        self.registry = registry

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.values = {}
        self.calls = []

    def CreateKey(self, hive, path):
        self.calls.append(("create", hive, path))
        return _Key(self)

    def OpenKey(self, hive, path, *args):
        self.calls.append(("open", hive, path, *args))
        return _Key(self)

    def SetValueEx(self, _key, name, _reserved, kind, value):
        self.values[name] = (value, kind)

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name]

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError
        del self.values[name]


class WindowsStartupTests(unittest.TestCase):
    def test_source_mode_is_unsupported_and_never_writes(self):
        registry = FakeRegistry()
        adapter = WindowsStartupAdapter(registry, frozen=False)
        self.assertFalse(adapter.is_supported())
        self.assertFalse(adapter.enable("C:/App/app.exe"))
        self.assertEqual(registry.calls, [])

    def test_portable_command_quotes_resolved_executable(self):
        command = WindowsStartupAdapter.command_for(Path("dist/CodexTokenMonitor/CodexTokenMonitor.exe"))
        self.assertTrue(command.startswith('"'))
        self.assertTrue(command.endswith('CodexTokenMonitor.exe"'))

    def test_enable_writes_only_named_hkcu_run_value(self):
        registry = FakeRegistry()
        registry.values["OtherApp"] = ("other.exe", registry.REG_SZ)
        adapter = WindowsStartupAdapter(registry, frozen=True)
        self.assertTrue(adapter.enable("C:/Portable/CodexTokenMonitor.exe"))
        self.assertIn(VALUE_NAME, registry.values)
        self.assertIn("OtherApp", registry.values)
        self.assertEqual(registry.calls[0], ("create", registry.HKEY_CURRENT_USER, RUN_KEY))

    def test_disable_removes_only_project_value(self):
        registry = FakeRegistry()
        registry.values = {VALUE_NAME: ('"C:\\App.exe"', 1), "OtherApp": ("other.exe", 1)}
        adapter = WindowsStartupAdapter(registry, frozen=True)
        self.assertTrue(adapter.disable())
        self.assertNotIn(VALUE_NAME, registry.values)
        self.assertIn("OtherApp", registry.values)

    def test_disable_missing_value_is_safe(self):
        self.assertTrue(WindowsStartupAdapter(FakeRegistry(), frozen=True).disable())

    def test_enabled_requires_current_executable_path(self):
        registry = FakeRegistry()
        adapter = WindowsStartupAdapter(registry, frozen=True)
        adapter.enable("C:/Old/CodexTokenMonitor.exe")
        self.assertFalse(adapter.is_enabled("C:/New/CodexTokenMonitor.exe"))
        self.assertTrue(adapter.is_enabled("C:/Old/CodexTokenMonitor.exe"))

    def test_adapter_never_mentions_hklm(self):
        self.assertNotIn("HKEY_LOCAL_MACHINE", Path("app/windows_startup.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
