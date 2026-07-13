"""Central visual tokens for the CustomTkinter Dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from tkinter import ttk

import customtkinter as ctk


SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_6 = 24
CARD_RADIUS = 12
CONTROL_RADIUS = 8


@dataclass(frozen=True)
class Colors:
    window: str = "#F4F7FB"
    surface: str = "#FFFFFF"
    raised_surface: str = "#F8FAFD"
    border: str = "#DDE4EE"
    border_strong: str = "#C8D3E1"
    scrollbar_thumb: str = "#91A4BE"
    scrollbar_thumb_hover: str = "#6F849F"
    primary_text: str = "#152033"
    secondary_text: str = "#667085"
    muted_text: str = "#8A96A8"
    accent: str = "#3978F6"
    accent_hover: str = "#2D66D4"
    accent_soft: str = "#EAF1FF"
    selection_background: str = "#D8E6FF"
    selection_background_inactive: str = "#E4ECF8"
    selection_foreground: str = "#102A56"
    real: str = "#248A52"
    real_soft: str = "#E8F6ED"
    estimate: str = "#A76812"
    estimate_soft: str = "#FFF4DC"
    stale: str = "#9A6417"
    stale_soft: str = "#FFF1D8"
    error: str = "#BC3A32"
    error_soft: str = "#FDECEB"
    unknown: str = "#728096"
    unknown_soft: str = "#EEF1F5"
    purple: str = "#7A4FD1"
    purple_soft: str = "#F1EBFF"
    teal: str = "#258E92"
    teal_soft: str = "#E4F6F5"
    orange: str = "#C87917"
    orange_soft: str = "#FFF0D6"
    telemetry: str = "#17263A"
    telemetry_muted: str = "#9FB0C5"
    telemetry_text: str = "#F7FAFC"


COLORS = Colors()
FONT_FAMILY = "Segoe UI"
FONT_BODY = (FONT_FAMILY, 13)
FONT_SMALL = (FONT_FAMILY, 11)
FONT_SECTION = (FONT_FAMILY, 15, "bold")
FONT_TITLE = (FONT_FAMILY, 25, "bold")
FONT_METRIC = (FONT_FAMILY, 22, "bold")

TONE_COLORS = {
    "fresh": (COLORS.real, COLORS.real_soft),
    "estimate": (COLORS.estimate, COLORS.estimate_soft),
    "stale": (COLORS.stale, COLORS.stale_soft),
    "error": (COLORS.error, COLORS.error_soft),
    "unknown": (COLORS.unknown, COLORS.unknown_soft),
    "disabled": (COLORS.unknown, COLORS.unknown_soft),
}

METRIC_ICONS = {
    "Input": "↳",
    "Output": "↗",
    "Current Total": "◆",
    "Cached": "▣",
    "Reasoning": "◉",
    "Cache Hit": "%",
}

METRIC_ACCENTS = {
    "Input": ("#4F8FEF", "#E8F1FF"),
    "Output": ("#3B9B55", "#E9F7EC"),
    "Current Total": (COLORS.purple, COLORS.purple_soft),
    "Cached": (COLORS.orange, COLORS.orange_soft),
    "Reasoning": ("#627ED0", "#ECF0FF"),
    "Cache Hit": (COLORS.teal, COLORS.teal_soft),
}


def configure_view(root: ctk.CTk) -> None:
    ctk.set_appearance_mode("light")
    ctk.set_default_color_theme("blue")
    root.configure(fg_color=COLORS.window)
    configure_treeview(root)


def configure_treeview(root: ctk.CTk) -> ttk.Style:
    """Keep ttk styling isolated to the one allowed native table widget."""
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure(
        "Monitor.Treeview",
        background=COLORS.surface,
        fieldbackground=COLORS.surface,
        foreground=COLORS.primary_text,
        borderwidth=0,
        relief="flat",
        rowheight=28,
        font=(FONT_FAMILY, 10),
    )
    style.map(
        "Monitor.Treeview",
        background=[("selected !focus", COLORS.selection_background_inactive), ("selected", COLORS.selection_background)],
        foreground=[("selected !focus", COLORS.selection_foreground), ("selected", COLORS.selection_foreground)],
    )
    style.configure(
        "Monitor.Treeview.Heading",
        background=COLORS.raised_surface,
        foreground=COLORS.secondary_text,
        borderwidth=0,
        relief="flat",
        font=(FONT_FAMILY, 10, "bold"),
        padding=(SPACE_2, SPACE_2),
    )
    style.map("Monitor.Treeview.Heading", background=[("active", COLORS.raised_surface)])
    return style
