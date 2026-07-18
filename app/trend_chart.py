"""Dependency-free Tk trend chart primitives for safe numeric history."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
import re
import tkinter as tk


_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/ -]{0,79}$")
_SAFE_UNIT = re.compile(r"^[A-Za-z0-9%][A-Za-z0-9_.:/% -]{0,31}$")


@dataclass(frozen=True)
class TrendPoint:
    """One safe numeric observation; content-bearing fields are intentionally absent."""

    observed_at: datetime
    metric: str
    value: int | float | None
    source: str
    stale: bool = False
    unit: str = "tokens"
    derived: bool = False

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("trend_point_requires_timezone")
        for name, value in (("metric", self.metric), ("source", self.source)):
            if not isinstance(value, str) or not _SAFE_KEY.fullmatch(value):
                raise ValueError(f"unsafe_trend_{name}")
        if not isinstance(self.unit, str) or not _SAFE_UNIT.fullmatch(self.unit):
            raise ValueError("unsafe_trend_unit")
        if self.value is not None:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise ValueError("invalid_trend_value")
            if not isfinite(float(self.value)):
                raise ValueError("invalid_trend_value")


@dataclass(frozen=True)
class TrendTooltipLabels:
    time: str = "Time"
    metric: str = "Metric"
    value: str = "Value"
    source: str = "Source"
    freshness: str = "Freshness"
    stale: str = "Stale"
    fresh: str = "Fresh"
    derived: str = "Derived"
    derived_yes: str = "Yes"
    derived_no: str = "No"


def downsample_peak_valley(
    points: Iterable[TrendPoint], max_points: int,
) -> tuple[TrendPoint, ...]:
    """Deterministically retain first/last and bucket extrema.

    Missing values are ignored rather than converted to zero. Equal timestamps
    retain their original relative order.
    """

    indexed = [
        (index, point) for index, point in enumerate(points)
        if point.value is not None
    ]
    indexed.sort(key=lambda item: (item[1].observed_at.astimezone(timezone.utc), item[0]))
    if max_points <= 0 or not indexed:
        return ()
    if len(indexed) <= max_points:
        return tuple(point for _, point in indexed)
    if max_points == 1:
        return (indexed[0][1],)
    if max_points == 2:
        return indexed[0][1], indexed[-1][1]

    first, last = indexed[0], indexed[-1]
    interior = indexed[1:-1]
    bucket_count = max(1, (max_points - 2) // 2)
    selected: set[int] = {first[0], last[0]}
    for bucket_index in range(bucket_count):
        start = len(interior) * bucket_index // bucket_count
        end = len(interior) * (bucket_index + 1) // bucket_count
        bucket = interior[start:end]
        if not bucket:
            continue
        selected.add(min(bucket, key=lambda item: (float(item[1].value), item[0]))[0])
        selected.add(max(bucket, key=lambda item: (float(item[1].value), -item[0]))[0])

    candidates = [item for item in indexed if item[0] in selected]
    if len(candidates) > max_points:
        mandatory = {first[0], last[0]}
        ranked = sorted(
            (item for item in candidates if item[0] not in mandatory),
            key=lambda item: (-_extreme_score(indexed, item), item[0]),
        )[: max_points - 2]
        keep = mandatory | {item[0] for item in ranked}
        candidates = [item for item in indexed if item[0] in keep]
    return tuple(point for _, point in candidates)


def nearest_trend_point(
    points: Iterable[TrendPoint], target_time: datetime,
) -> TrendPoint | None:
    """Select the temporally nearest valid point with stable tie-breaking."""

    if target_time.tzinfo is None:
        raise ValueError("target_time_requires_timezone")
    target = target_time.astimezone(timezone.utc)
    candidates = [point for point in points if point.value is not None]
    if not candidates:
        return None
    return min(
        enumerate(candidates),
        key=lambda item: (
            abs((item[1].observed_at.astimezone(timezone.utc) - target).total_seconds()),
            item[0],
        ),
    )[1]


def _extreme_score(
    ordered: list[tuple[int, TrendPoint]], candidate: tuple[int, TrendPoint],
) -> float:
    position = ordered.index(candidate)
    if position <= 0 or position >= len(ordered) - 1:
        return float("inf")
    previous = float(ordered[position - 1][1].value)
    current = float(candidate[1].value)
    following = float(ordered[position + 1][1].value)
    return abs(current - ((previous + following) / 2.0))


class TrendCanvas(tk.Canvas):
    """Responsive vector chart with a local, full-value hover tooltip."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        width: int = 760,
        height: int = 180,
        background: str = "#F8FAFD",
        foreground: str = "#152033",
        grid_color: str = "#DDE4EE",
        series_colors: Mapping[str, str] | None = None,
        metric_labels: Mapping[str, str] | None = None,
        source_labels: Mapping[str, str] | None = None,
        tooltip_labels: TrendTooltipLabels | None = None,
        value_formatter: Callable[[TrendPoint], str] | None = None,
    ) -> None:
        super().__init__(
            master, width=width, height=height, bg=background,
            highlightthickness=0, bd=0,
        )
        self._foreground = foreground
        self._grid_color = grid_color
        self._series_colors = dict(series_colors or {})
        self._metric_labels = dict(metric_labels or {})
        self._source_labels = dict(source_labels or {})
        self._tooltip_labels = tooltip_labels or TrendTooltipLabels()
        self._value_formatter = value_formatter or _format_full_value
        self._points: tuple[TrendPoint, ...] = ()
        self._rendered_points: list[tuple[float, float, TrendPoint]] = []
        self._tooltip: tk.Toplevel | None = None
        self._tooltip_label: tk.Label | None = None
        self._redraw_after_id: str | None = None
        self._render_signature: object | None = None
        self._needs_redraw = True
        self._disposed = False
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Map>", self._on_map, add="+")
        self.bind("<Motion>", self._on_motion, add="+")
        self.bind("<Button-1>", self._on_click, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")

    def set_points(self, points: Iterable[TrendPoint]) -> None:
        updated = tuple(points)
        if updated == self._points:
            return
        self._hide_tooltip()
        self._points = updated
        self._schedule_redraw()

    def set_labels(
        self,
        *,
        metric_labels: Mapping[str, str] | None = None,
        source_labels: Mapping[str, str] | None = None,
        tooltip_labels: TrendTooltipLabels | None = None,
    ) -> None:
        updated_metrics = (
            dict(metric_labels) if metric_labels is not None else self._metric_labels
        )
        updated_sources = (
            dict(source_labels) if source_labels is not None else self._source_labels
        )
        updated_tooltips = tooltip_labels or self._tooltip_labels
        if (
            updated_metrics == self._metric_labels
            and updated_sources == self._source_labels
            and updated_tooltips == self._tooltip_labels
        ):
            return
        self._hide_tooltip()
        self._metric_labels = updated_metrics
        self._source_labels = updated_sources
        self._tooltip_labels = updated_tooltips
        self._schedule_redraw()

    def destroy(self) -> None:
        self._dispose()
        super().destroy()

    def _on_configure(self, _event: tk.Event) -> None:
        self._schedule_redraw()

    def _on_map(self, _event: tk.Event | None) -> None:
        if self._needs_redraw:
            self._schedule_redraw()

    def _schedule_redraw(self) -> None:
        self._needs_redraw = True
        if self._disposed or self._redraw_after_id is not None:
            return
        try:
            if not self.winfo_viewable():
                return
        except tk.TclError:
            return
        self._redraw_after_id = self.after_idle(self._redraw)

    def _redraw(self) -> None:
        self._redraw_after_id = None
        if self._disposed:
            return
        try:
            if not self.winfo_viewable():
                self._needs_redraw = True
                return
        except tk.TclError:
            return
        self._needs_redraw = False
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        signature = (
            self._points, width, height, self._foreground, self._grid_color,
            tuple(sorted(self._series_colors.items())),
            tuple(sorted(self._metric_labels.items())),
            tuple(sorted(self._source_labels.items())), self._tooltip_labels,
        )
        if signature == self._render_signature:
            return
        self._render_signature = signature
        self.delete("all")
        self._rendered_points.clear()
        grouped: dict[str, list[TrendPoint]] = defaultdict(list)
        for point in self._points:
            if point.value is not None:
                grouped[point.metric].append(point)
        all_valid = [point for values in grouped.values() for point in values]
        if not all_valid:
            return
        left, right, top, bottom = 42, max(43, width - 12), 10, max(11, height - 24)
        self.create_line(left, bottom, right, bottom, fill=self._grid_color)
        times = [point.observed_at.timestamp() for point in all_valid]
        values = [float(point.value) for point in all_valid]
        time_min, time_max = min(times), max(times)
        value_min, value_max = min(values), max(values)
        self.create_text(
            left - 6, top, text=_format_axis(value_max), anchor="e",
            fill=self._foreground, font=("Segoe UI", 8),
        )
        self.create_text(
            left - 6, bottom, text=_format_axis(value_min), anchor="e",
            fill=self._foreground, font=("Segoe UI", 8),
        )
        max_points = max(2, int((right - left) // 4))
        for series_index, (metric, series) in enumerate(sorted(grouped.items())):
            sampled = downsample_peak_valley(series, max_points)
            color = self._series_colors.get(metric, _DEFAULT_COLORS[series_index % len(_DEFAULT_COLORS)])
            coords: list[float] = []
            for point_index, point in enumerate(sampled):
                x = _scale(
                    point.observed_at.timestamp(), time_min, time_max,
                    left, right, point_index, len(sampled),
                )
                y = _scale(float(point.value), value_min, value_max, bottom, top)
                coords.extend((x, y))
                self._rendered_points.append((x, y, point))
            if len(coords) >= 4:
                self.create_line(*coords, fill=color, width=2, smooth=False)
            for index in range(0, len(coords), 2):
                x, y = coords[index:index + 2]
                self.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline="")

    def _on_motion(self, event: tk.Event) -> None:
        if self._disposed or not self._rendered_points:
            return
        x, y, point = min(
            self._rendered_points,
            key=lambda item: ((item[0] - event.x) ** 2 + (item[1] - event.y) ** 2),
        )
        if (x - event.x) ** 2 + (y - event.y) ** 2 > 18 ** 2:
            self._hide_tooltip()
            return
        self._show_tooltip(event, point)

    def _on_click(self, event: tk.Event) -> None:
        if self._disposed or not self._rendered_points:
            return
        x, y, point = min(
            self._rendered_points,
            key=lambda item: ((item[0] - event.x) ** 2 + (item[1] - event.y) ** 2),
        )
        if (x - event.x) ** 2 + (y - event.y) ** 2 <= 24 ** 2:
            self._show_tooltip(event, point)

    def _show_tooltip(self, event: tk.Event, point: TrendPoint) -> None:
        if self._tooltip is None:
            self._tooltip = tk.Toplevel(self)
            self._tooltip.overrideredirect(True)
            self._tooltip_label = tk.Label(
                self._tooltip, justify="left", relief="solid", borderwidth=1,
                background="#FFFFFF", foreground=self._foreground,
                font=("Segoe UI", 9), padx=7, pady=5,
            )
            self._tooltip_label.pack()
        assert self._tooltip_label is not None
        labels = self._tooltip_labels
        local_time = point.observed_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        metric = self._metric_labels.get(point.metric, point.metric)
        source = self._source_labels.get(point.source, point.source)
        freshness = labels.stale if point.stale else labels.fresh
        derived = labels.derived_yes if point.derived else labels.derived_no
        self._tooltip_label.configure(text=(
            f"{labels.time}: {local_time}\n"
            f"{labels.metric}: {metric}\n"
            f"{labels.value}: {self._value_formatter(point)}\n"
            f"{labels.source}: {source}\n"
            f"{labels.freshness}: {freshness}\n"
            f"{labels.derived}: {derived}"
        ))
        self._tooltip.deiconify()
        self._tooltip.update_idletasks()
        x = self.winfo_rootx() + event.x + 12
        y = self.winfo_rooty() + event.y + 12
        x = max(0, min(x, self.winfo_screenwidth() - self._tooltip.winfo_reqwidth() - 4))
        y = max(0, min(y, self.winfo_screenheight() - self._tooltip.winfo_reqheight() - 4))
        self._tooltip.geometry(f"+{x}+{y}")

    def _on_leave(self, _event: tk.Event) -> None:
        self._hide_tooltip()

    def _hide_tooltip(self) -> None:
        if self._tooltip is not None:
            self._tooltip.withdraw()

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self:
            self._dispose()

    def _dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        if self._redraw_after_id is not None:
            try:
                self.after_cancel(self._redraw_after_id)
            except tk.TclError:
                pass
            self._redraw_after_id = None
        if self._tooltip is not None:
            try:
                self._tooltip.destroy()
            except tk.TclError:
                pass
            self._tooltip = None
            self._tooltip_label = None
        self._rendered_points.clear()


_DEFAULT_COLORS = ("#3978F6", "#248A52", "#8B5CF6", "#D97706")


def _scale(
    value: float,
    source_min: float,
    source_max: float,
    target_min: float,
    target_max: float,
    index: int = 0,
    count: int = 1,
) -> float:
    if source_max == source_min:
        if count > 1:
            return target_min + ((target_max - target_min) * index / (count - 1))
        return (target_min + target_max) / 2.0
    return target_min + ((value - source_min) / (source_max - source_min) * (target_max - target_min))


def _format_full_value(point: TrendPoint) -> str:
    assert point.value is not None
    if isinstance(point.value, int) or float(point.value).is_integer():
        value = f"{int(point.value):,}"
    else:
        value = f"{float(point.value):,.2f}".rstrip("0").rstrip(".")
    return f"{value} {point.unit}" if point.unit else value


def _format_axis(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if magnitude >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"
