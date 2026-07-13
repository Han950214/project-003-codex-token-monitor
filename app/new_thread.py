"""Content-free handoff templates for the optional new-thread workflow."""

from __future__ import annotations


GENERIC_HANDOFF_TEMPLATES = {
    "zh-CN": (
        "请继续处理当前任务。\n"
        "下面是我手动整理的任务目标、已完成内容、当前状态和下一步：\n"
        "【请在这里粘贴或填写内容】"
    ),
    "en": (
        "Please continue working on the current task.\n"
        "Below are the goal, completed work, current state, and next step that I organized manually:\n"
        "[Paste or enter the details here]"
    ),
}


def generic_handoff_template(language: str) -> str:
    return GENERIC_HANDOFF_TEMPLATES.get(language, GENERIC_HANDOFF_TEMPLATES["zh-CN"])
