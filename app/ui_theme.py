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
    # Surfaces: darkest window, card surface, inner raised panel.
    window: str = "#0C1018"
    surface: str = "#121826"
    raised_surface: str = "#1A2334"
    border: str = "#263147"
    border_strong: str = "#3B4A66"
    scrollbar_thumb: str = "#33415E"
    scrollbar_thumb_hover: str = "#455678"
    # Text hierarchy.
    primary_text: str = "#E6ECF5"
    on_accent: str = "#0C1018"
    secondary_text: str = "#A0AEC4"
    muted_text: str = "#7F8CA3"
    # Primary brand accent (electric blue).
    accent: str = "#4D8DFF"
    accent_hover: str = "#3A76E8"
    accent_soft: str = "#16263F"
    selection_background: str = "#22406E"
    selection_background_inactive: str = "#1E2A42"
    selection_foreground: str = "#DCE9FF"
    # Semantic status colors, brightened for dark backgrounds.
    real: str = "#3DDC84"
    real_soft: str = "#10281C"
    estimate: str = "#F5A623"
    estimate_soft: str = "#2C2310"
    stale: str = "#E8B04B"
    stale_soft: str = "#2C2314"
    error: str = "#F87171"
    error_soft: str = "#331419"
    unknown: str = "#8E9BAF"
    unknown_soft: str = "#1B2333"
    purple: str = "#A78BFA"
    purple_soft: str = "#241C3D"
    teal: str = "#2DD4BF"
    teal_soft: str = "#0F2928"
    orange: str = "#FB923C"
    orange_soft: str = "#2B1D10"
    # Telemetry sidebar (darker than the main window).
    telemetry: str = "#0A0F1A"
    telemetry_footer: str = "#080C15"
    telemetry_hover: str = "#182638"
    telemetry_border: str = "#22334C"
    telemetry_action_hover: str = "#20324E"
    telemetry_exit_hover: str = "#3A1F2C"
    telemetry_muted: str = "#7C90AD"
    telemetry_secondary: str = "#A6B8D0"
    telemetry_text: str = "#F0F5FB"
    # Compact desktop widget (kept high-contrast on dark).
    widget_purple: str = "#A884FF"
    widget_success: str = "#72D68B"
    widget_warning: str = "#F0B45C"
    widget_error: str = "#FF8178"


# Chart-specific tokens (shared by TrendCanvas / Sparkline / CircularProgress).
CHART_BACKGROUND = "#121826"
CHART_FOREGROUND = "#8E9BAF"
CHART_GRID = "#263147"
CHART_TRACK = "#263147"
CHART_RING_TEXT = "#DCE4F0"
CHART_SERIES = ("#4D8DFF", "#3DDC84", "#A78BFA", "#FB923C")

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
    "Input": ("#60A5FA", "#122441"),
    "Output": ("#3DDC84", "#10281C"),
    "Current Total": (COLORS.purple, COLORS.purple_soft),
    "Cached": (COLORS.orange, COLORS.orange_soft),
    "Reasoning": ("#8B9DF6", "#1B2140"),
    "Cache Hit": (COLORS.teal, COLORS.teal_soft),
}


def configure_view(root: ctk.CTk) -> None:
    ctk.set_appearance_mode("dark")
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
        background=[
            ("selected !focus", COLORS.selection_background_inactive),
            ("selected", COLORS.selection_background),
        ],
        foreground=[
            ("selected !focus", COLORS.selection_foreground),
            ("selected", COLORS.selection_foreground),
        ],
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
    style.map(
        "Monitor.Treeview.Heading", background=[("active", COLORS.raised_surface)]
    )
    return style
