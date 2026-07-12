"""Single-instance startup settings dialog."""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from typing import Callable

import customtkinter as ctk

from app.i18n import translate
from app.ui_settings import load_startup_mode, save_startup_mode
from app.windows_startup import WindowsStartupAdapter


class StartupSettingsDialog:
    def __init__(
        self,
        root: ctk.CTk,
        settings_path: Path,
        *,
        startup: WindowsStartupAdapter | None = None,
        on_saved: Callable[[str], None] | None = None,
    ) -> None:
        self.root = root
        self.settings_path = settings_path
        self.startup = startup or WindowsStartupAdapter()
        self.on_saved = on_saved or (lambda _mode: None)
        self.window: ctk.CTkToplevel | None = None
        self.language = "zh-CN"

    def show(self, language: str) -> None:
        self.language = language
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
            return
        window = self.window = ctk.CTkToplevel(self.root)
        window.title(translate("startup_settings", language))
        window.geometry("430x310")
        window.resizable(False, False)
        if self.root.winfo_viewable():
            window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(window, text=translate("startup_settings", language), font=("Segoe UI", 20, "bold")).grid(row=0, column=0, sticky="w", padx=24, pady=(24, 16))
        self.startup_var = tk.BooleanVar(master=window, value=self.startup.is_enabled(sys.executable))
        self.startup_switch = ctk.CTkSwitch(window, text=translate("start_with_windows", language), variable=self.startup_var)
        self.startup_switch.grid(row=1, column=0, sticky="w", padx=24)
        if not self.startup.is_supported():
            self.startup_switch.configure(state="disabled")
            ctk.CTkLabel(window, text=translate("startup_exe_only", language), text_color="#8B93A7", wraplength=380, justify="left").grid(row=2, column=0, sticky="w", padx=24, pady=(6, 14))

        ctk.CTkLabel(window, text=translate("default_startup_mode", language)).grid(row=3, column=0, sticky="w", padx=24, pady=(8, 6))
        self.mode_labels = {
            translate("startup_dashboard", language): "dashboard",
            translate("startup_widget", language): "widget",
            translate("startup_tray", language): "tray",
        }
        current = load_startup_mode(self.settings_path)
        selected = next(label for label, mode in self.mode_labels.items() if mode == current)
        self.mode_menu = ctk.CTkOptionMenu(window, values=list(self.mode_labels), width=260)
        self.mode_menu.set(selected)
        self.mode_menu.grid(row=4, column=0, sticky="w", padx=24)
        ctk.CTkButton(window, text=translate("save_settings", language), command=self.save, width=130).grid(row=5, column=0, sticky="e", padx=24, pady=24)
        def present() -> None:
            if self.window is not window or not window.winfo_exists():
                return
            window.deiconify()
            window.lift()
            window.grab_set()
            window.focus_force()

        window.after(20, present)

    def save(self) -> None:
        mode = self.mode_labels.get(self.mode_menu.get(), "dashboard")
        save_startup_mode(mode, self.settings_path)
        if self.startup.is_supported():
            if self.startup_var.get():
                self.startup.enable(sys.executable)
            else:
                self.startup.disable()
        self.on_saved(mode)
        self.close()

    def close(self) -> None:
        if self.window is not None:
            try:
                self.window.grab_release()
                self.window.destroy()
            except tk.TclError:
                pass
        self.window = None
