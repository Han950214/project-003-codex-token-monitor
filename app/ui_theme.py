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
CARD_RADIUS = 8
CONTROL_RADIUS = 4


@dataclass(frozen=True)
class Colors:
    window: str = "#F3F4F6"
    surface: str = "#FFFFFF"
    raised_surface: str = "#F8FAFC"
    border: str = "#D1D5DB"
    border_strong: str = "#94A3B8"
    scrollbar_thumb: str = "#91A4BE"
    scrollbar_thumb_hover: str = "#6F849F"
    primary_text: str = "#111827"
    on_accent: str = "#FFFFFF"
    secondary_text: str = "#4B5563"
    muted_text: str = "#6B7280"
    accent: str = "#2563EB"
    accent_hover: str = "#1D4ED8"
    accent_soft: str = "#EFF6FF"
    selection_background: str = "#D8E6FF"
    selection_background_inactive: str = "#E4ECF8"
    selection_foreground: str = "#102A56"
    real: str = "#15803D"
    real_soft: str = "#E8F6ED"
    estimate: str = "#BC4800"
    estimate_soft: str = "#FFF4DC"
    stale: str = "#9A6417"
    stale_soft: str = "#FFF1D8"
    error: str = "#BA1A1A"
    error_soft: str = "#FDECEB"
    unknown: str = "#728096"
    unknown_soft: str = "#EEF1F5"
    purple: str = "#7C3AED"
    purple_soft: str = "#F1EBFF"
    teal: str = "#258E92"
    teal_soft: str = "#E4F6F5"
    orange: str = "#C87917"
    orange_soft: str = "#FFF0D6"
    telemetry: str = "#17263A"
    telemetry_footer: str = "#132237"
    telemetry_hover: str = "#263B56"
    telemetry_border: str = "#314B69"
    telemetry_action_hover: str = "#36516F"
    telemetry_exit_hover: str = "#4A2630"
    telemetry_muted: str = "#9FB0C5"
    telemetry_secondary: str = "#B9C7D9"
    telemetry_text: str = "#F7FAFC"
    widget_purple: str = "#A884FF"
    widget_success: str = "#72D68B"
    widget_warning: str = "#F0B45C"
    widget_error: str = "#FF8178"


COLORS = Colors()
FONT_FAMILY = "Segoe UI"
PAGE_TITLE = (FONT_FAMILY, 22, "bold")
STATUS_TITLE = (FONT_FAMILY, 19, "bold")
SECTION_TITLE = (FONT_FAMILY, 15, "bold")
CARD_TITLE = (FONT_FAMILY, 13, "bold")
BODY = (FONT_FAMILY, 13)
BODY_STRONG = (FONT_FAMILY, 13, "bold")
CAPTION = (FONT_FAMILY, 11)
METRIC = (FONT_FAMILY, 20, "bold")
NAV = (FONT_FAMILY, 13)
BUTTON = (FONT_FAMILY, 13, "bold")

# Compatibility aliases for unchanged secondary surfaces.
FONT_BODY = BODY
FONT_SMALL = (FONT_FAMILY, 11)
FONT_SECTION = SECTION_TITLE
FONT_TITLE = PAGE_TITLE
FONT_METRIC = METRIC

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
        rowheight=36,
        font=(FONT_FAMILY, 11),
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
        font=(FONT_FAMILY, 11, "bold"),
        padding=(SPACE_2, SPACE_2),
    )
    style.map("Monitor.Treeview.Heading", background=[("active", COLORS.raised_surface)])
    return style
