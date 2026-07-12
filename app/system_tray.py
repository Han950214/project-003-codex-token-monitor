"""Single system-tray boundary with Tk-main-thread callback dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.i18n import translate
from app.paths import resource_root


@dataclass(frozen=True)
class TrayState:
    language: str = "zh-CN"
    auto_refresh_enabled: bool = False


def load_tray_icon():
    from PIL import Image

    mask = Image.open(resource_root() / "resources" / "tray-icon.xbm").convert("L")
    image = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    image.paste((74, 125, 255, 255), mask=mask)
    return image


class SystemTrayController:
    """Own exactly one pystray icon; never touch Tk from its worker thread."""

    def __init__(
        self,
        root,
        *,
        on_restore_dashboard: Callable[[], None],
        on_show_widget: Callable[[], None],
        on_hide_to_tray: Callable[[], None],
        on_manual_refresh: Callable[[], None],
        on_toggle_auto_refresh: Callable[[], None],
        on_settings: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self.root = root
        self._callbacks = {
            "restore": on_restore_dashboard,
            "widget": on_show_widget,
            "hide": on_hide_to_tray,
            "refresh": on_manual_refresh,
            "toggle": on_toggle_auto_refresh,
            "settings": on_settings,
            "exit": on_exit,
        }
        self.state = TrayState()
        self.icon = None
        self.started = False
        self.closing = False

    def start(self) -> bool:
        if self.started:
            return True
        try:
            import pystray

            self.icon = pystray.Icon(
                "CodexTokenMonitor",
                load_tray_icon(),
                "Codex Token Monitor",
                self._build_menu(),
            )
            self.icon.run_detached()
            self.started = True
            return True
        except Exception:
            self.icon = None
            self.started = False
            return False

    def stop(self) -> None:
        if self.closing:
            return
        self.closing = True
        icon, self.icon = self.icon, None
        self.started = False
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def update(self, *, language: str, auto_refresh_enabled: bool) -> None:
        self.state = TrayState(language, auto_refresh_enabled)
        if self.icon is not None:
            try:
                self.icon.menu = self._build_menu()
                self.icon.update_menu()
            except Exception:
                pass

    def _dispatch(self, key: str) -> None:
        if self.closing:
            return
        try:
            self.root.after(0, self._callbacks[key])
        except Exception:
            self.closing = True

    def _build_menu(self):
        import pystray

        language = self.state.language
        auto_key = "tray_auto_refresh_on" if self.state.auto_refresh_enabled else "tray_auto_refresh_off"
        item = pystray.MenuItem
        return pystray.Menu(
            item(translate("open_dashboard", language), lambda _icon, _item: self._dispatch("restore"), default=True),
            item(translate("show_mini_widget", language), lambda _icon, _item: self._dispatch("widget")),
            item(translate("hide_to_tray", language), lambda _icon, _item: self._dispatch("hide")),
            pystray.Menu.SEPARATOR,
            item(translate("manual_refresh", language), lambda _icon, _item: self._dispatch("refresh")),
            item(translate(auto_key, language), lambda _icon, _item: self._dispatch("toggle")),
            item(translate("settings", language), lambda _icon, _item: self._dispatch("settings")),
            pystray.Menu.SEPARATOR,
            item(translate("exit_application", language), lambda _icon, _item: self._dispatch("exit")),
        )
