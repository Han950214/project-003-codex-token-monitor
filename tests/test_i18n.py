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
            "time_range": ("时间范围", "Time Range"),
            "last_7_days": ("最近 7 天", "Last 7 days"),
            "recent_sessions_note": ("显示最近检测到的 Codex 会话，最多检查 500 条近期记录。", "Showing recently detected Codex sessions; up to 500 recent records are checked."),
            "recent_sessions_note_truncated": ("记录较多，本次仅检查最近 500 条；可缩小时间范围以提高速度。", "Many records were found. Only the latest 500 were checked; choose a shorter time range for faster loading."),
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
        self.assertEqual(DataStatus.COMPLETED.value, "Completed")
        self.assertEqual(localize_status(DataStatus.COMPLETED, "zh-CN"), "已完成")
        self.assertEqual(localize_status(DataStatus.RUNNING, "en"), "Running")
        self.assertEqual(localize_status(DataStatus.COMPLETED_PARTIAL, "zh-CN"), "已完成（部分数据）")
        self.assertEqual(localize_status(DataStatus.COMPLETED_PARTIAL, "en"), "Completed (Partial Data)")

    def test_auto_refresh_localization_keeps_sixty_seconds(self):
        self.assertEqual(localize_auto_refresh(True, "zh-CN"), "自动刷新：开启（60 秒）")
        self.assertEqual(localize_auto_refresh(False, "zh-CN"), "自动刷新：关闭（60 秒）")
        self.assertEqual(localize_auto_refresh(True, "en"), "Auto Refresh: On (60s)")

    def test_user_facing_source_labels_hide_engineering_terms(self):
        self.assertEqual(localize_presenter_label("Data Sync", "zh-CN"), "数据同步")
        self.assertEqual(localize_presenter_label("Current Task", "en"), "Current Task")
        self.assertEqual(localize_auto_refresh(False, "en"), "Auto Refresh: Off (60s)")


if __name__ == "__main__":
    unittest.main()
