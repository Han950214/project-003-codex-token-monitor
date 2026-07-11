"""Central Tkinter/ttk theme for the Dashboard."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont, ttk


SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_6 = 24


@dataclass(frozen=True)
class Colors:
    window: str = "#F3F5F7"
    surface: str = "#FFFFFF"
    raised_surface: str = "#F8FAFC"
    border: str = "#D8DEE6"
    primary_text: str = "#182230"
    secondary_text: str = "#5E6B7A"
    accent: str = "#2563EB"
    real: str = "#16794A"
    estimate: str = "#9A6700"
    stale: str = "#8A5A13"
    error: str = "#B42318"
    unknown: str = "#7A8696"
    telemetry: str = "#172231"
    telemetry_muted: str = "#AAB6C4"
    telemetry_text: str = "#FFFFFF"


COLORS = Colors()
FONT_FAMILY = "Segoe UI"
FALLBACK_FONT = "TkDefaultFont"

TONE_STYLES = {
    "fresh": "Fresh.TLabel",
    "estimate": "Estimate.TLabel",
    "stale": "Stale.TLabel",
    "error": "Error.TLabel",
    "unknown": "Unknown.TLabel",
    "disabled": "Unknown.TLabel",
}


def configure_theme(root: tk.Misc) -> ttk.Style:
    """Configure all Dashboard styles, with a safe system-theme fallback."""
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

    family = FONT_FAMILY
    try:
        if family not in tkfont.families(root):
            family = tkfont.nametofont(FALLBACK_FONT, root=root).actual("family")
    except tk.TclError:
        family = FALLBACK_FONT

    try:
        tkfont.nametofont(FALLBACK_FONT, root=root).configure(family=family, size=9)
    except tk.TclError:
        pass
    root.configure(background=COLORS.window)
    style.configure(".", font=(family, 9), foreground=COLORS.primary_text)
    style.configure("Window.TFrame", background=COLORS.window)
    style.configure("Surface.TFrame", background=COLORS.surface)
    style.configure("Raised.TFrame", background=COLORS.raised_surface)
    style.configure("Card.TFrame", background=COLORS.surface, relief="solid", borderwidth=1)
    style.configure("Header.TLabel", background=COLORS.window, foreground=COLORS.primary_text, font=(family, 17, "bold"))
    style.configure("Section.TLabel", background=COLORS.window, foreground=COLORS.primary_text, font=(family, 11, "bold"))
    style.configure("CardLabel.TLabel", background=COLORS.surface, foreground=COLORS.secondary_text, font=(family, 8))
    style.configure("CardValue.TLabel", background=COLORS.surface, foreground=COLORS.primary_text, font=(family, 15, "bold"))
    style.configure("TotalCardValue.TLabel", background=COLORS.surface, foreground=COLORS.accent, font=(family, 17, "bold"))
    for name, color in (
        ("Fresh", COLORS.real),
        ("Estimate", COLORS.estimate),
        ("Stale", COLORS.stale),
        ("Error", COLORS.error),
        ("Unknown", COLORS.unknown),
        ("Disabled", COLORS.unknown),
    ):
        style.configure(f"Card{name}.TLabel", background=COLORS.surface, foreground=color, font=(family, 15, "bold"))
        style.configure(f"TotalCard{name}.TLabel", background=COLORS.surface, foreground=color, font=(family, 17, "bold"))
        style.configure(f"Source{name}.TLabel", background=COLORS.surface, foreground=color, font=(family, 9, "bold"))
    style.configure("CardDetail.TLabel", background=COLORS.surface, foreground=COLORS.secondary_text, font=(family, 8))
    style.configure("Secondary.TLabel", background=COLORS.window, foreground=COLORS.secondary_text)
    style.configure("Fresh.TLabel", background=COLORS.window, foreground=COLORS.real, font=(family, 9, "bold"))
    style.configure("Estimate.TLabel", background=COLORS.window, foreground=COLORS.estimate, font=(family, 9, "bold"))
    style.configure("Stale.TLabel", background=COLORS.window, foreground=COLORS.stale, font=(family, 9, "bold"))
    style.configure("Error.TLabel", background=COLORS.window, foreground=COLORS.error, font=(family, 9, "bold"))
    style.configure("Unknown.TLabel", background=COLORS.window, foreground=COLORS.unknown, font=(family, 9, "bold"))
    style.configure("Accent.TButton", padding=(SPACE_3, SPACE_2))
    style.configure("TButton", padding=(SPACE_3, SPACE_2))
    style.configure("TEntry", padding=SPACE_1)
    style.configure("Treeview", rowheight=24, background=COLORS.surface, fieldbackground=COLORS.surface, bordercolor=COLORS.border)
    style.configure("Treeview.Heading", font=(family, 9, "bold"))
    style.configure("TNotebook", background=COLORS.window, borderwidth=0)
    style.configure("TNotebook.Tab", padding=(SPACE_4, SPACE_2))
    style.configure("Telemetry.TFrame", background=COLORS.telemetry)
    style.configure("TelemetryLabel.TLabel", background=COLORS.telemetry, foreground=COLORS.telemetry_muted, font=(family, 8))
    style.configure("TelemetryValue.TLabel", background=COLORS.telemetry, foreground=COLORS.telemetry_text, font=(family, 9, "bold"))
    return style
