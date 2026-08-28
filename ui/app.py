from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gio, Gtk, Gdk  # noqa: E402

from tvh.async_bridge import bridge
from ui.window import MainWindow

logger = logging.getLogger("tvh.app")

APP_ID = "io.github.tvh_gnome_client"


class TvhGnomeApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window: MainWindow | None = None

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        bridge.start()  # uruchamiamy petle asyncio dla HTSP w tle
        self._load_css()

    def do_activate(self) -> None:
        if not self.window:
            self.window = MainWindow(self)
        self.window.present()
        if self.window.bg_ctrl is not None and self.window.bg_ctrl.is_hidden:
            self.window.bg_ctrl.show_window()

    def _load_css(self) -> None:
        import os
        css_path = os.path.join(os.path.dirname(__file__), "style.css")
        if not os.path.exists(css_path):
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(css_path)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
