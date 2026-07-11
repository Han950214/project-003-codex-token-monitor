import unittest

from app.i18n import (
    DEFAULT_LANGUAGE,
    TRANSLATIONS,
    localize_auto_refresh,
    localize_presenter_label,
    localize_status,
    translate,
)
from app.ui_presenter import DataStatus


class I18nTests(unittest.TestCase):
    def test_default_language_is_simplified_chinese(self):
        self.assertEqual(DEFAULT_LANGUAGE, "zh-CN")
        self.assertEqual(translate("manual_refresh"), "手动刷新")

    def test_key_translations_are_complete_in_both_languages(self):
        self.assertEqual(set(TRANSLATIONS["zh-CN"]), set(TRANSLATIONS["en"]))
        expected = {
            "latest_usage": ("当前 / 最近指令 Usage", "Current / Latest Instruction Usage"),
            "manual_saved_runs": ("手动保存记录", "Manual Saved Runs"),
            "manual_run_input": ("手动 Run 输入", "Manual Run Input"),
            "export_report": ("导出报告", "Export Report"),
        }
        for key, (zh, en) in expected.items():
            self.assertEqual(translate(key, "zh-CN"), zh)
            self.assertEqual(translate(key, "en"), en)
        self.assertNotIn("最新响应", translate("latest_usage", "zh-CN"))
        self.assertNotIn("Latest Response", translate("latest_usage", "en"))

    def test_unknown_language_and_missing_key_fall_back_safely(self):
        self.assertEqual(translate("manual_refresh", "xx"), "手动刷新")
        self.assertEqual(translate("missing_key", "en"), "missing_key")

    def test_data_status_internal_semantics_do_not_change(self):
        self.assertEqual(DataStatus.FRESH_REAL.value, "Fresh · Real")
        self.assertEqual(localize_status(DataStatus.FRESH_REAL, "zh-CN"), "实时 · 真实数据")
        self.assertEqual(localize_status(DataStatus.FRESH_REAL, "en"), "Fresh · Real")
        self.assertEqual(DataStatus.FRESH_REAL.value, "Fresh · Real")

    def test_auto_refresh_localization_keeps_sixty_seconds(self):
        self.assertEqual(localize_auto_refresh(True, "zh-CN"), "自动刷新：开启（60 秒）")
        self.assertEqual(localize_auto_refresh(False, "zh-CN"), "自动刷新：关闭（60 秒）")
        self.assertEqual(localize_auto_refresh(True, "en"), "Auto Refresh: On (60s)")

    def test_thread_total_reconciliation_label_is_explicit(self):
        self.assertEqual(localize_presenter_label("Thread Total Reconciliation", "zh-CN"), "Thread 总计对账")
        self.assertEqual(localize_presenter_label("Thread Total Reconciliation", "en"), "Thread Total Reconciliation")
        self.assertEqual(localize_auto_refresh(False, "en"), "Auto Refresh: Off (60s)")


if __name__ == "__main__":
    unittest.main()
