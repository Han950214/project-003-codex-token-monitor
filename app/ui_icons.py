"""Small locally drawn UI visuals; no icon pack or chart framework required."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Iterable

import customtkinter as ctk
from PIL import Image, ImageDraw


def create_icon(
    kind: str,
    *,
    size: int = 20,
    color: str = "#3978F6",
) -> ctk.CTkImage:
    """Create an antialiased local icon without an external icon package."""
    render_size = size * 4
    scale = render_size / 24
    image = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def points(values: Iterable[tuple[float, float]]) -> list[tuple[int, int]]:
        return [(round(x * scale), round(y * scale)) for x, y in values]

    width = max(1, round(1.8 * scale))
    if kind == "shield":
        draw.polygon(points(((12, 2), (20, 5), (19, 14), (16, 19), (12, 22), (8, 19), (5, 14), (4, 5))), fill=color)
        draw.line(points(((8, 12), (11, 15), (16, 9))), fill="#FFFFFF", width=max(2, width), joint="curve")
    elif kind == "home":
        draw.line(points(((3, 11), (12, 3), (21, 11))), fill=color, width=width, joint="curve")
        draw.rounded_rectangle((*points(((6, 10), (18, 21)))[0], *points(((6, 10), (18, 21)))[1]), radius=max(1, round(2 * scale)), outline=color, width=width)
        draw.line(points(((12, 21), (12, 15))), fill=color, width=width)
    elif kind == "history":
        draw.ellipse((*points(((4, 4), (20, 20)))[0], *points(((4, 4), (20, 20)))[1]), outline=color, width=width)
        draw.line(points(((12, 7), (12, 12), (16, 14))), fill=color, width=width, joint="curve")
    elif kind == "tools":
        draw.rounded_rectangle((*points(((3, 8), (21, 20)))[0], *points(((3, 8), (21, 20)))[1]), radius=max(1, round(2 * scale)), outline=color, width=width)
        draw.rounded_rectangle((*points(((8, 4), (16, 10)))[0], *points(((8, 4), (16, 10)))[1]), radius=max(1, round(2 * scale)), outline=color, width=width)
        draw.line(points(((3, 13), (21, 13))), fill=color, width=width)
    elif kind == "settings":
        draw.ellipse((*points(((8, 8), (16, 16)))[0], *points(((8, 8), (16, 16)))[1]), outline=color, width=width)
        for x1, y1, x2, y2 in ((12, 2, 12, 6), (12, 18, 12, 22), (2, 12, 6, 12), (18, 12, 22, 12), (5, 5, 8, 8), (16, 16, 19, 19), (19, 5, 16, 8), (8, 16, 5, 19)):
            draw.line(points(((x1, y1), (x2, y2))), fill=color, width=width)
    elif kind == "pulse":
        draw.line(points(((2, 13), (6, 13), (9, 6), (12, 19), (15, 10), (18, 13), (22, 13))), fill=color, width=width, joint="curve")
    elif kind == "open":
        draw.rounded_rectangle((*points(((3, 6), (18, 21)))[0], *points(((3, 6), (18, 21)))[1]), radius=max(1, round(2 * scale)), outline=color, width=width)
        draw.line(points(((11, 13), (21, 3), (21, 10))), fill=color, width=width, joint="curve")
        draw.line(points(((14, 3), (21, 3))), fill=color, width=width)
    elif kind == "refresh":
        draw.arc((*points(((3, 3), (21, 21)))[0], *points(((3, 3), (21, 21)))[1]), 35, 315, fill=color, width=width)
        draw.polygon(points(((19, 3), (22, 8), (16, 8))), fill=color)
    elif kind == "widget":
        draw.rounded_rectangle((*points(((3, 4), (21, 20)))[0], *points(((3, 4), (21, 20)))[1]), radius=max(1, round(2 * scale)), outline=color, width=width)
        draw.line(points(((8, 4), (8, 20))), fill=color, width=width)
        draw.line(points(((8, 10), (21, 10))), fill=color, width=width)
    elif kind == "more":
        radius = max(1, round(2 * scale))
        for x in (6, 12, 18):
            draw.ellipse((round(x * scale) - radius, round(12 * scale) - radius, round(x * scale) + radius, round(12 * scale) + radius), fill=color)
    else:
        draw.ellipse((*points(((5, 5), (19, 19)))[0], *points(((5, 5), (19, 19)))[1]), outline=color, width=width)
    # Keep a 2x source so CTkImage can render cleanly at 125%/150% DPI.
    image = image.resize((size * 2, size * 2), Image.Resampling.LANCZOS)
    return ctk.CTkImage(light_image=image, dark_image=image, size=(size, size))


class CircularProgress(tk.Canvas):
    """Tiny deterministic ring for a real percentage value."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        size: int = 58,
        background: str = "#F8FAFD",
        track: str = "#DDE4EE",
        color: str = "#248A52",
    ) -> None:
        super().__init__(
            master, width=size, height=size, bg=background,
            highlightthickness=0, bd=0,
        )
        self.size = size
        self.track = track
        self.color = color
        self.set(None)

    def set(self, value: float | None, *, color: str | None = None) -> None:
        self.delete("all")
        inset = 5
        width = max(4, round(self.size / 11))
        self.create_arc(
            inset, inset, self.size - inset, self.size - inset,
            start=90, extent=-359.9, style="arc", outline=self.track,
            width=width,
        )
        if value is not None:
            bounded = min(100.0, max(0.0, float(value)))
            self.create_arc(
                inset, inset, self.size - inset, self.size - inset,
                start=90, extent=-(bounded * 3.6), style="arc",
                outline=color or self.color, width=width,
            )
            text = f"{bounded:.0f}%"
        else:
            text = "—"
        self.create_text(
            self.size / 2, self.size / 2, text=text,
            fill="#152033", font=("Segoe UI", max(8, round(self.size / 5)), "bold"),
        )


class Sparkline(tk.Canvas):
    """A small line chart that renders only when at least two real samples exist."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        width: int = 112,
        height: int = 28,
        background: str = "#F8FAFD",
        color: str = "#3978F6",
    ) -> None:
        super().__init__(
            master, width=width, height=height, bg=background,
            highlightthickness=0, bd=0,
        )
        self.chart_width = width
        self.chart_height = height
        self.color = color

    def set_samples(self, values: Iterable[float | int | None]) -> bool:
        samples = [float(value) for value in values if value is not None]
        samples = samples[-8:]
        self.delete("all")
        if len(samples) < 2:
            return False
        low, high = min(samples), max(samples)
        spread = high - low
        left, right, top, bottom = 3, self.chart_width - 3, 3, self.chart_height - 3
        points: list[float] = []
        for index, value in enumerate(samples):
            x = left + ((right - left) * index / (len(samples) - 1))
            y = (top + bottom) / 2 if spread == 0 else bottom - ((value - low) / spread * (bottom - top))
            points.extend((x, y))
        self.create_line(*points, fill=self.color, width=2, smooth=True, splinesteps=12)
        return True
