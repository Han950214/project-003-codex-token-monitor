"""Mock Windows Dashboard for Codex Token Monitor Skill."""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_pricing
from app.metrics import average_hit, current_hit, current_tokens, session_tokens
from app.sample_data import SAMPLE_CHANGED_FILES, SAMPLE_DIFF_STAT, SAMPLE_OUTPUT, SAMPLE_PROMPT, SAMPLE_USAGES
from app.telemetry_bar import create_telemetry_bar, format_percent


ROOT = Path(__file__).resolve().parents[1]


def build_dashboard() -> tk.Tk:
    pricing = load_pricing(ROOT / "resources" / "pricing-config.sample.json")
    current = SAMPLE_USAGES[-1]

    root = tk.Tk()
    root.title("Codex Token Monitor Skill - 本地估算 Dashboard")
    root.geometry("1180x720")
    root.minsize(980, 620)

    header = ttk.Frame(root, padding=10)
    header.pack(fill="x")
    ttk.Label(header, text="Codex Token Monitor / 本地估算 Dashboard", font=("Segoe UI", 15, "bold")).pack(side="left")
    ttk.Button(header, text="开始监控 / Start Run").pack(side="right", padx=(8, 0))
    ttk.Button(header, text="结束监控 / End Run").pack(side="right")

    main = ttk.Frame(root, padding=(10, 0, 10, 10))
    main.pack(fill="both", expand=True)
    main.grid_columnconfigure(0, weight=2)
    main.grid_columnconfigure(1, weight=1)
    main.grid_rowconfigure(0, weight=1)

    left = ttk.Frame(main)
    left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
    left.grid_rowconfigure(0, weight=1)
    left.grid_rowconfigure(1, weight=1)
    left.grid_columnconfigure(0, weight=1)

    prompt_box = _text_panel(left, "Prompt / 用户指令（本地估算 / local estimate）", SAMPLE_PROMPT)
    prompt_box.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
    output_box = _text_panel(left, "Output / Codex 输出（本地估算 / local estimate）", SAMPLE_OUTPUT)
    output_box.grid(row=1, column=0, sticky="nsew")

    right = ttk.Frame(main)
    right.grid(row=0, column=1, sticky="nsew")
    right.grid_columnconfigure(0, weight=1)

    report = (
        "Token Waste Report / 本地估算\n"
        f"- 本次 tokens: {current_tokens(current)} local estimate\n"
        f"- 会话 tokens: {session_tokens(SAMPLE_USAGES)} local estimate\n"
        f"- 本次命中率: {format_percent(current_hit(current))} local estimate\n"
        f"- 平均命中率: {format_percent(average_hit(SAMPLE_USAGES))} local estimate\n"
        f"- Changed files: {', '.join(SAMPLE_CHANGED_FILES)}\n"
        f"- Diff stat: {SAMPLE_DIFF_STAT}\n"
    )
    ttk.Label(right, text=report, justify="left", padding=10).grid(row=0, column=0, sticky="ew", pady=(0, 8))

    advisor = (
        "Cache Hit Advisor / 本地估算\n"
        "- 稳定前缀约 2100 tokens，可作为下一轮 cache-friendly prompt 的前段。\n"
        "- 检测到重复背景风险：避免重复粘贴完整项目说明。\n"
        "- 动态前缀风险：时间戳、git status、日志应放在稳定规则之后。\n\n"
        "下一轮建议 prompt:\n"
        "低 token 模式。继续当前 token monitor scaffold，只检查测试和最终状态需要的文件。"
    )
    ttk.Label(right, text=advisor, justify="left", padding=10).grid(row=1, column=0, sticky="ew")

    create_telemetry_bar(root, SAMPLE_USAGES, pricing).pack(fill="x", side="bottom")
    return root


def _text_panel(parent: tk.Widget, title: str, content: str) -> ttk.Frame:
    frame = ttk.Frame(parent)
    ttk.Label(frame, text=title, font=("Segoe UI", 10, "bold")).pack(anchor="w")
    text = tk.Text(frame, wrap="word", height=8)
    text.insert("1.0", content)
    text.pack(fill="both", expand=True)
    return frame


def smoke() -> None:
    print("Codex Token Monitor smoke OK")
    print(f"current_tokens={current_tokens(SAMPLE_USAGES[-1])} local_estimate")
    print(f"session_tokens={session_tokens(SAMPLE_USAGES)} local_estimate")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="Run a non-GUI smoke check.")
    args = parser.parse_args()
    if args.smoke:
        smoke()
        return
    build_dashboard().mainloop()


if __name__ == "__main__":
    main()
