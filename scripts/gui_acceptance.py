"""Launch the real dashboard at an exact QA geometry and CTk scale.

This helper is intentionally separate from the production entry point.  It
requires an isolated temporary data directory so GUI acceptance never writes
fictional QA observations into the user's normal application data.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import customtkinter as ctk


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.paths import DATA_DIR_ENV, ui_settings_path  # noqa: E402
from app.i18n import translate  # noqa: E402
from app.ui_settings import save_language  # noqa: E402


GEOMETRIES = ("980x660", "1440x900")
SCALES = (1.0, 1.25, 1.5)
PAGES = ("overview", "usage_trends", "recommendations")
RANGES = (7, 30, 90)


def _isolated_data_root() -> Path:
    raw = os.environ.get(DATA_DIR_ENV)
    if not raw:
        raise RuntimeError(f"{DATA_DIR_ENV} is required for GUI acceptance")
    root = Path(raw).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if root == temp_root or temp_root not in root.parents:
        raise RuntimeError("GUI acceptance data directory must be under the system temp directory")
    return root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--geometry", choices=GEOMETRIES, required=True)
    parser.add_argument("--scale", choices=SCALES, type=float, required=True)
    parser.add_argument("--language", choices=("zh-CN", "en"), required=True)
    parser.add_argument("--page", choices=PAGES, default="usage_trends")
    parser.add_argument("--range", choices=RANGES, type=int, default=7)
    parser.add_argument("--auto-close-ms", type=int, default=0)
    args = parser.parse_args()

    data_root = _isolated_data_root()
    data_root.mkdir(parents=True, exist_ok=True)
    if not save_language(args.language, ui_settings_path()):
        raise RuntimeError("Unable to save isolated GUI acceptance language")

    ctk.set_widget_scaling(args.scale)
    ctk.set_window_scaling(args.scale)

    from app.main import Dashboard

    root = ctk.CTk()
    dashboard = Dashboard(root)
    percent = round(args.scale * 100)
    root.title(
        f"Codex Token Monitor QA - {percent}% - {args.geometry} - {args.language}"
    )

    def apply_case() -> None:
        root.geometry(args.geometry)
        if args.page == "usage_trends":
            range_label = translate(f"last_{args.range}_days", dashboard.language)
            dashboard.trend_range_menu.set(range_label)
            dashboard._change_trend_range(range_label)  # noqa: SLF001 - QA launcher
        dashboard.show_page(args.page)
        if args.auto_close_ms > 0:
            root.after(args.auto_close_ms, dashboard.close)

    root.after_idle(apply_case)
    root.mainloop()


if __name__ == "__main__":
    main()
