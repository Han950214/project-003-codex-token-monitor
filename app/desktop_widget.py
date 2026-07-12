"""Single-process always-on-top desktop mini widget."""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import customtkinter as ctk

from app.dashboard import MiniThreadSnapshot
from app.i18n import localize_presenter_text, translate
from app.quota import CodexQuotaSnapshot, QuotaWindow
from app.ui_settings import load_widget_position, save_widget_position
from app.ui_settings import save_exit_action_for_today
from app.ui_theme import (
    CARD_RADIUS,
    COLORS,
    CONTROL_RADIUS,
    FONT_BODY,
    FONT_FAMILY,
    FONT_SECTION,
    FONT_SMALL,
    SPACE_1,
    SPACE_2,
    SPACE_3,
    SPACE_4,
)


WIDGET_WIDTH = 340
WIDGET_HEIGHT = 500
WIDGET_MARGIN = 16
DEFAULT_ALPHA = 0.82
HOVER_ALPHA = 1.0


@dataclass(frozen=True)
class WorkArea:
    left: int
    top: int
    right: int
    bottom: int


def top_right_position(
    work_area: WorkArea,
    width: int,
    height: int,
    margin: int = WIDGET_MARGIN,
) -> tuple[int, int]:
    x = max(work_area.left, work_area.right - width - margin)
    y = max(work_area.top, work_area.top + margin)
    return clamp_position((x, y), work_area, width, height)


def clamp_position(
    position: tuple[int, int],
    work_area: WorkArea,
    width: int,
    height: int,
) -> tuple[int, int]:
    max_x = max(work_area.left, work_area.right - width)
    max_y = max(work_area.top, work_area.bottom - height)
    return (
        min(max(position[0], work_area.left), max_x),
        min(max(position[1], work_area.top), max_y),
    )


def monitor_work_area(root: tk.Misc) -> WorkArea:
    if sys.platform == "win32":
        try:
            return _windows_monitor_work_area(int(root.winfo_id()))
        except Exception:
            pass
    return WorkArea(0, 0, int(root.winfo_screenwidth()), int(root.winfo_screenheight()))


def _windows_monitor_work_area(hwnd: int) -> WorkArea:
    class RECT(ctypes.Structure):
        _fields_ = [(name, ctypes.c_long) for name in ("left", "top", "right", "bottom")]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    user32 = ctypes.windll.user32
    monitor = user32.MonitorFromWindow(hwnd, 2)
    if not monitor:
        raise OSError("monitor_unavailable")
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise OSError("monitor_info_unavailable")
    rect = info.rcWork
    return WorkArea(rect.left, rect.top, rect.right, rect.bottom)


class DesktopMiniWidget:
    def __init__(
        self,
        root: ctk.CTk,
        *,
        on_restore: Callable[[], None],
        on_minimize: Callable[[], None],
        on_exit: Callable[[], None],
        on_refresh: Callable[[], None],
        settings_path: Path,
    ) -> None:
        self.root = root
        self.on_restore = on_restore
        self.on_minimize = on_minimize
        self.on_exit = on_exit
        self.on_refresh = on_refresh
        self.settings_path = settings_path
        self.language = "zh-CN"
        self.visible = False
        self.thread_id: str | None = None
        self._drag_start: tuple[int, int, int, int] | None = None
        self._opacity_job: str | None = None

        self.window = ctk.CTkToplevel(root)
        self.window.withdraw()
        self.window.title("Codex Token Monitor · Desktop Widget")
        self.window.overrideredirect(True)
        self.window.resizable(False, False)
        self.window.configure(fg_color=COLORS.window)
        self.window.attributes("-topmost", True)
        self.window.geometry(f"{WIDGET_WIDTH}x{WIDGET_HEIGHT}")

        self.used_vars = [tk.StringVar(master=self.window, value="—") for _ in range(2)]
        self.remaining_vars = [tk.StringVar(master=self.window, value="—") for _ in range(2)]
        self.reset_vars = [tk.StringVar(master=self.window, value="—") for _ in range(2)]
        self.quota_title_vars = [tk.StringVar(master=self.window, value="") for _ in range(2)]
        self.instruction_total_var = tk.StringVar(master=self.window, value="—")
        self.session_total_var = tk.StringVar(master=self.window, value="—")
        self.thread_title_var = tk.StringVar(master=self.window, value="")
        self.thread_status_var = tk.StringVar(master=self.window, value="")
        self.last_updated_var = tk.StringVar(master=self.window, value="—")
        self.data_status_var = tk.StringVar(master=self.window, value="")
        self.progress_bars: list[ctk.CTkProgressBar] = []
        self._build()
        self._bind_hover_opacity(self.window)

    def _build(self) -> None:
        self.window.grid_columnconfigure(0, weight=1)
        title_bar = ctk.CTkFrame(self.window, fg_color=COLORS.surface, corner_radius=0, height=54)
        title_bar.grid(row=0, column=0, sticky="ew")
        title_bar.grid_columnconfigure(1, weight=1)
        icon = ctk.CTkLabel(title_bar, text="◆", text_color=COLORS.accent, font=(FONT_FAMILY, 18, "bold"))
        icon.grid(row=0, column=0, padx=(SPACE_4, SPACE_2), pady=SPACE_3)
        self.title_label = ctk.CTkLabel(title_bar, text="Codex Token Monitor", font=(FONT_FAMILY, 13, "bold"), text_color=COLORS.primary_text, anchor="w")
        self.title_label.grid(row=0, column=1, sticky="ew", pady=SPACE_3)
        self.restore_button = ctk.CTkButton(title_bar, text="", command=self.on_restore, width=58, height=30, corner_radius=CONTROL_RADIUS, fg_color=COLORS.accent, hover_color=COLORS.accent_hover)
        self.restore_button.grid(row=0, column=2, padx=SPACE_1, pady=SPACE_3)
        self.minimize_button = ctk.CTkButton(title_bar, text="", command=self.on_minimize, width=64, height=30, corner_radius=CONTROL_RADIUS, fg_color="transparent", text_color=COLORS.secondary_text, hover_color=COLORS.accent_soft)
        self.minimize_button.grid(row=0, column=3, padx=SPACE_1, pady=SPACE_3)
        self.exit_button = ctk.CTkButton(title_bar, text="×", command=self.on_exit, width=44, height=30, corner_radius=CONTROL_RADIUS, fg_color="transparent", text_color=COLORS.secondary_text, hover_color=COLORS.error_soft)
        self.exit_button.grid(row=0, column=4, padx=(SPACE_1, SPACE_3), pady=SPACE_3)
        for widget in (title_bar, icon, self.title_label):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_drag)
            widget.bind("<Double-Button-1>", lambda _event: self.on_restore())

        body = ctk.CTkFrame(self.window, fg_color="transparent", corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew", padx=SPACE_3, pady=(SPACE_2, SPACE_2))
        body.grid_columnconfigure(0, weight=1)
        accents = ((COLORS.accent, COLORS.accent_soft), (COLORS.real, COLORS.real_soft))
        for row, (accent, soft) in enumerate(accents):
            self._build_quota_card(body, row, accent, soft)
        self._build_thread_card(body, 2)

        footer = ctk.CTkFrame(self.window, fg_color=COLORS.surface, corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.footer_label = ctk.CTkLabel(footer, textvariable=self.last_updated_var, font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
        self.footer_label.grid(row=0, column=0, sticky="ew", padx=(SPACE_4, SPACE_2), pady=SPACE_2)
        self.status_label = ctk.CTkLabel(footer, textvariable=self.data_status_var, font=(FONT_FAMILY, 11, "bold"), text_color=COLORS.real)
        self.status_label.grid(row=0, column=1, padx=SPACE_2, pady=SPACE_2)
        self.refresh_button = ctk.CTkButton(footer, text="↻", command=self.on_refresh, width=30, height=28, corner_radius=CONTROL_RADIUS, fg_color="transparent", text_color=COLORS.accent, hover_color=COLORS.accent_soft)
        self.refresh_button.grid(row=0, column=2, padx=(SPACE_1, SPACE_3), pady=SPACE_2)

    def _build_quota_card(self, parent: ctk.CTkFrame, row: int, accent: str, soft: str) -> None:
        card = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        card.grid(row=row, column=0, sticky="ew", pady=(0, SPACE_2))
        card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(card, textvariable=self.quota_title_vars[row], font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w").grid(row=0, column=0, sticky="ew", padx=SPACE_3, pady=(SPACE_2, 0))
        ctk.CTkLabel(card, textvariable=self.used_vars[row], font=(FONT_FAMILY, 18, "bold"), text_color=accent, anchor="w").grid(row=1, column=0, sticky="ew", padx=SPACE_3)
        ctk.CTkLabel(card, textvariable=self.remaining_vars[row], font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="e").grid(row=1, column=1, padx=SPACE_3)
        progress = ctk.CTkProgressBar(card, height=7, corner_radius=4, fg_color=soft, progress_color=accent)
        progress.grid(row=2, column=0, columnspan=2, sticky="ew", padx=SPACE_3, pady=(SPACE_1, SPACE_1))
        progress.set(0)
        ctk.CTkLabel(card, textvariable=self.reset_vars[row], font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w").grid(row=3, column=0, columnspan=2, sticky="ew", padx=SPACE_3, pady=(0, SPACE_2))
        self.progress_bars.append(progress)

    def _build_thread_card(self, parent: ctk.CTkFrame, row: int) -> None:
        card = ctk.CTkFrame(parent, fg_color=COLORS.surface, corner_radius=CARD_RADIUS, border_width=1, border_color=COLORS.border)
        card.grid(row=row, column=0, sticky="ew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_columnconfigure(2, weight=1)
        self.thread_heading = ctk.CTkLabel(card, text="", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
        self.thread_heading.grid(row=0, column=0, columnspan=3, sticky="ew", padx=SPACE_3, pady=(SPACE_2, 0))
        self.instruction_label = ctk.CTkLabel(card, text="", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
        self.instruction_label.grid(row=1, column=0, sticky="ew", padx=(SPACE_3, SPACE_1))
        self.session_label = ctk.CTkLabel(card, text="", font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w")
        self.session_label.grid(row=1, column=2, sticky="ew", padx=(SPACE_2, SPACE_3))
        ctk.CTkFrame(card, width=1, fg_color=COLORS.border, corner_radius=0).grid(row=1, column=1, rowspan=2, sticky="ns", pady=(SPACE_1, SPACE_1))
        ctk.CTkLabel(card, textvariable=self.instruction_total_var, font=(FONT_FAMILY, 17, "bold"), text_color=COLORS.purple, anchor="w").grid(row=2, column=0, sticky="ew", padx=(SPACE_3, SPACE_1))
        ctk.CTkLabel(card, textvariable=self.session_total_var, font=(FONT_FAMILY, 17, "bold"), text_color=COLORS.purple, anchor="w").grid(row=2, column=2, sticky="ew", padx=(SPACE_2, SPACE_3))
        ctk.CTkLabel(card, textvariable=self.thread_title_var, font=FONT_BODY, text_color=COLORS.primary_text, anchor="w", justify="left", wraplength=290, height=36).grid(row=3, column=0, columnspan=3, sticky="ew", padx=SPACE_3)
        ctk.CTkLabel(card, textvariable=self.thread_status_var, font=FONT_SMALL, text_color=COLORS.secondary_text, anchor="w").grid(row=4, column=0, columnspan=3, sticky="ew", padx=SPACE_3, pady=(0, SPACE_2))

    def show(
        self,
        thread_id: str | None,
        quota: CodexQuotaSnapshot,
        thread: MiniThreadSnapshot,
        language: str,
    ) -> None:
        self.thread_id = thread_id
        self.language = language
        self.update(quota, thread, language)
        self.root.update_idletasks()
        area = monitor_work_area(self.root)
        saved = load_widget_position(self.settings_path)
        position = clamp_position(saved, area, WIDGET_WIDTH, WIDGET_HEIGHT) if saved else top_right_position(area, WIDGET_WIDTH, WIDGET_HEIGHT)
        self.window.geometry(f"{WIDGET_WIDTH}x{WIDGET_HEIGHT}+{position[0]}+{position[1]}")
        self.window.deiconify()
        self.window.lift()
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", DEFAULT_ALPHA)
        self.visible = True

    def hide(self) -> None:
        self.visible = False
        self.window.withdraw()

    def destroy(self) -> None:
        self.visible = False
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def _bind_hover_opacity(self, widget: tk.Misc) -> None:
        widget.bind("<Enter>", self._show_background, add="+")
        widget.bind("<Leave>", self._schedule_transparent_background, add="+")
        for child in widget.winfo_children():
            self._bind_hover_opacity(child)

    def _show_background(self, _event: object = None) -> None:
        if self._opacity_job is not None:
            try:
                self.window.after_cancel(self._opacity_job)
            except tk.TclError:
                pass
            self._opacity_job = None
        if self.visible:
            self.window.attributes("-alpha", HOVER_ALPHA)

    def _schedule_transparent_background(self, _event: object = None) -> None:
        if self._opacity_job is not None:
            try:
                self.window.after_cancel(self._opacity_job)
            except tk.TclError:
                pass
        self._opacity_job = self.window.after(30, self._refresh_pointer_opacity)

    def _refresh_pointer_opacity(self) -> None:
        self._opacity_job = None
        if not self.visible:
            return
        pointer_x, pointer_y = self.window.winfo_pointerxy()
        left, top = self.window.winfo_rootx(), self.window.winfo_rooty()
        inside = (
            left <= pointer_x < left + self.window.winfo_width()
            and top <= pointer_y < top + self.window.winfo_height()
        )
        self.window.attributes("-alpha", HOVER_ALPHA if inside else DEFAULT_ALPHA)

    def set_refreshing(self) -> None:
        self.data_status_var.set(translate("quota_refreshing", self.language))
        self.status_label.configure(text_color=COLORS.accent)

    def update(
        self,
        quota: CodexQuotaSnapshot,
        thread: MiniThreadSnapshot,
        language: str,
    ) -> None:
        self.language = language
        self.restore_button.configure(text=translate("restore_widget", language))
        self.minimize_button.configure(text=translate("minimize_widget", language))
        self.exit_button.configure(text=translate("exit_application_short", language))
        self.quota_title_vars[0].set(translate("five_hour_limit", language))
        self.quota_title_vars[1].set(translate("weekly_limit", language))
        self.thread_heading.configure(text=translate("token_usage", language))
        self.instruction_label.configure(text=translate("instruction_total", language))
        self.session_label.configure(text=translate("session_total_short", language))
        for index, window in enumerate((quota.five_hour, quota.weekly)):
            self._update_window(index, window)
        if thread.status == "no_selection":
            self.thread_title_var.set(translate("no_selected_thread", language))
            self.instruction_total_var.set("—")
            self.session_total_var.set("—")
            self.thread_status_var.set(translate("quota_unavailable", language))
        else:
            self.thread_title_var.set(_bounded_title(thread.title) or translate("no_selected_thread", language))
            self.instruction_total_var.set(format_token_total(thread.instruction_total_tokens))
            self.session_total_var.set(format_token_total(thread.session_total_tokens))
            self.thread_status_var.set(localize_presenter_text(thread.status, language))
        local_time = quota.refreshed_at.astimezone().strftime("%H:%M:%S")
        self.last_updated_var.set(f"{translate('last_updated', language)}：{local_time}" if language == "zh-CN" else f"{translate('last_updated', language)}: {local_time}")
        status_key = {
            "normal": "quota_normal",
            "stale": "quota_stale",
            "invalid": "quota_invalid",
        }.get(quota.source_status, "quota_unavailable")
        self.data_status_var.set(translate(status_key, language))
        color = {
            "normal": COLORS.real,
            "stale": COLORS.stale,
            "invalid": COLORS.error,
        }.get(quota.source_status, COLORS.unknown)
        self.status_label.configure(text_color=color)

    def _update_window(self, index: int, window: QuotaWindow) -> None:
        used = format_percent(window.used_percent) if window.available else "—"
        remaining = format_percent(window.remaining_percent) if window.available else "—"
        if self.language == "zh-CN":
            self.used_vars[index].set(f"{translate('used_percent', self.language)} {used}")
            self.remaining_vars[index].set(f"{translate('remaining_percent', self.language)} {remaining}")
            self.reset_vars[index].set(f"{translate('reset_time', self.language)}：{format_reset_time(window.reset_at, self.language, window.observed_at)}")
        else:
            self.used_vars[index].set(f"{used} {translate('used_percent', self.language)}")
            self.remaining_vars[index].set(f"{remaining} {translate('remaining_percent', self.language)}")
            self.reset_vars[index].set(f"{translate('reset_time', self.language)}: {format_reset_time(window.reset_at, self.language, window.observed_at)}")
        if window.available and window.used_percent is not None:
            self.progress_bars[index].configure(progress_color=(COLORS.accent, COLORS.real)[index])
            self.progress_bars[index].set(window.used_percent / 100.0)
        else:
            self.progress_bars[index].configure(progress_color=COLORS.border)
            self.progress_bars[index].set(0)

    def _start_drag(self, event: tk.Event) -> None:
        self._drag_start = (event.x_root, event.y_root, self.window.winfo_x(), self.window.winfo_y())

    def _drag(self, event: tk.Event) -> None:
        if self._drag_start is None:
            return
        start_x, start_y, window_x, window_y = self._drag_start
        self.window.geometry(f"+{window_x + event.x_root - start_x}+{window_y + event.y_root - start_y}")

    def _end_drag(self, _event: tk.Event) -> None:
        self._drag_start = None
        area = monitor_work_area(self.root)
        x, y = clamp_position((self.window.winfo_x(), self.window.winfo_y()), area, WIDGET_WIDTH, WIDGET_HEIGHT)
        self.window.geometry(f"+{x}+{y}")
        save_widget_position(x, y, self.settings_path)


def format_percent(value: float | None) -> str:
    if value is None:
        return "—"
    value = min(100.0, max(0.0, float(value)))
    return f"{int(value)}%" if value.is_integer() else f"{value:.1f}%"


def format_token_total(value: int | None) -> str:
    return f"{value:,}" if value is not None else "—"


def format_reset_time(
    reset_at: datetime | None,
    language: str,
    observed_at: datetime | None = None,
) -> str:
    if reset_at is None:
        return "—"
    local = reset_at.astimezone()
    now = (observed_at or datetime.now().astimezone()).astimezone(local.tzinfo)
    if local.date() == now.date():
        return f"{translate('today', language)} {local:%H:%M}"
    if local.date() == (now + timedelta(days=1)).date():
        return f"{translate('tomorrow', language)} {local:%H:%M}"
    return local.strftime("%m月%d日 %H:%M") if language == "zh-CN" else local.strftime("%b %d, %H:%M")


def _bounded_title(value: str, limit: int = 76) -> str:
    title = " ".join(value.split())
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


class ExitChoiceDialog:
    """One reusable decision surface with a safe, local day-only preference."""

    def __init__(self, root: ctk.CTk, settings_path: Path) -> None:
        self.root = root
        self.settings_path = settings_path
        self.window: ctk.CTkToplevel | None = None
        self.remember_var: tk.BooleanVar | None = None
        self._on_choice: Callable[[str], None] | None = None

    def show(
        self,
        *,
        owner: tk.Misc,
        language: str,
        on_choice: Callable[[str], None],
    ) -> None:
        if self.window is not None and self.window.winfo_exists():
            self.window.lift()
            return
        self._on_choice = on_choice
        window = self.window = ctk.CTkToplevel(self.root)
        window.title(translate("exit_prompt_title", language))
        window.geometry("390x210")
        window.resizable(False, False)
        window.configure(fg_color=COLORS.window)
        window.transient(owner)
        window.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            window,
            text=translate("exit_prompt_title", language),
            font=FONT_SECTION,
            text_color=COLORS.primary_text,
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=SPACE_4, pady=(SPACE_4, SPACE_2))
        ctk.CTkLabel(
            window,
            text=translate("exit_prompt_message", language),
            font=FONT_BODY,
            text_color=COLORS.secondary_text,
            anchor="w",
            justify="left",
            wraplength=350,
        ).grid(row=1, column=0, sticky="ew", padx=SPACE_4)
        self.remember_var = tk.BooleanVar(master=window, value=False)
        ctk.CTkCheckBox(
            window,
            text=translate("dont_ask_today", language),
            variable=self.remember_var,
            font=FONT_SMALL,
            text_color=COLORS.secondary_text,
            width=180,
        ).grid(row=2, column=0, sticky="w", padx=SPACE_4, pady=SPACE_3)
        actions = ctk.CTkFrame(window, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="e", padx=SPACE_4, pady=(0, SPACE_4))
        exit_button = ctk.CTkButton(
            actions,
            text=translate("exit_application", language),
            command=lambda: self._choose("exit"),
            width=120,
            height=34,
            fg_color="transparent",
            border_width=1,
            border_color=COLORS.border_strong,
            text_color=COLORS.error,
            hover_color=COLORS.error_soft,
        )
        exit_button.grid(row=0, column=0, padx=(0, SPACE_2))
        minimize_button = ctk.CTkButton(
            actions,
            text=translate("minimize_application", language),
            command=lambda: self._choose("minimize"),
            width=130,
            height=34,
            fg_color=COLORS.accent,
            hover_color=COLORS.accent_hover,
        )
        minimize_button.grid(row=0, column=1)
        window.protocol("WM_DELETE_WINDOW", lambda: self._choose("minimize"))
        window.bind("<Return>", lambda _event: self._choose("minimize"))
        window.bind("<Escape>", lambda _event: self._choose("minimize"))
        window.update_idletasks()
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - 390) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - 210) // 2)
        window.geometry(f"390x210+{x}+{y}")
        window.deiconify()
        window.lift()
        window.grab_set()
        minimize_button.focus_set()

    def _choose(self, action: str) -> None:
        if self.window is None:
            return
        remember = bool(self.remember_var and self.remember_var.get())
        callback = self._on_choice
        if remember:
            save_exit_action_for_today(action, self.settings_path)
        try:
            self.window.grab_release()
            self.window.destroy()
        except tk.TclError:
            pass
        self.window = None
        self.remember_var = None
        self._on_choice = None
        if callback is not None:
            callback(action)
