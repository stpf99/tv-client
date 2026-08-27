"""Minimalizacja do zasobnika systemowego (tray) z podglądem OSD-info w
menu: aktualny kanał + audycja, lista zaplanowanych przypomnień, czas
oglądania w tej sesji.

GTK4 nie ma wlasnego API zasobnika (Gtk.StatusIcon usuniete w GTK3->4).
Standardowe podejscie to AyatanaAppIndicator3 (pakiet systemowy, NIE
biblioteka Python z pip) - dziala natywnie na KDE/XFCE/MATE, na czystym
GNOME Shell wymaga rozszerzenia "AppIndicator and KStatusNotifierItem
Support". Jesli pakiet nie jest zainstalowany w systemie, aplikacja ma
dzialac normalnie po prostu bez ikony w zasobniku (nie crashowac, nie
blokowac startu) - stad ostrozny import z fallbackiem.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gio  # noqa: E402

logger = logging.getLogger("ui.tray")

_INDICATOR_NS = None
AppIndicator3 = None
for _ns, _ver in (("AyatanaAppIndicator3", "0.1"), ("AppIndicator3", "0.1")):
    try:
        gi.require_version(_ns, _ver)
        from gi.repository import AyatanaAppIndicator3 as _mod  # type: ignore
        AppIndicator3 = _mod
        _INDICATOR_NS = _ns
        break
    except (ValueError, ImportError):
        continue

TRAY_AVAILABLE = AppIndicator3 is not None
if not TRAY_AVAILABLE:
    logger.info(
        "AyatanaAppIndicator3/AppIndicator3 niedostępne w systemie - "
        "zasobnik systemowy będzie wyłączony (aplikacja działa normalnie "
        "bez niego). Na Debian/Ubuntu: apt install gir1.2-ayatanaappindicator3-0.1"
    )
else:
    logger.info("Zasobnik systemowy: użyję %s", _INDICATOR_NS)


class TrayController:
    """Zarządza ikoną w zasobniku i jej menu. Bezpieczne do stworzenia
    nawet gdy TRAY_AVAILABLE jest False - staje się wtedy no-opem."""

    def __init__(
        self,
        app_id: str,
        on_toggle_window: Callable[[], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._on_toggle_window = on_toggle_window
        self._on_quit = on_quit
        self._indicator = None

        self._channel_name = ""
        self._program_title = ""
        self._reminders: list = []
        self._watch_seconds_today = 0

        if not TRAY_AVAILABLE:
            return

        try:
            self._indicator = AppIndicator3.Indicator.new(
                app_id,
                "tv-symbolic",
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            self._indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
            self._rebuild_menu()
        except Exception:
            logger.exception("Nie udało się utworzyć ikony zasobnika")
            self._indicator = None

    @property
    def available(self) -> bool:
        return self._indicator is not None

    # ------------------------------------------------------------------ #
    # Stan pokazywany w menu
    # ------------------------------------------------------------------ #
    def set_now_playing(self, channel_name: str, program_title: str) -> None:
        self._channel_name = channel_name or ""
        self._program_title = program_title or ""
        self._rebuild_menu()

    def clear_now_playing(self) -> None:
        self._channel_name = ""
        self._program_title = ""
        self._rebuild_menu()

    def set_reminders(self, reminders: list) -> None:
        """reminders: lista obiektów Reminder (ui/reminders.py), posortowana
        po czasie startu - pokazujemy najblizsze."""
        self._reminders = list(reminders)[:5]
        self._rebuild_menu()

    def set_watch_seconds_today(self, seconds: int) -> None:
        self._watch_seconds_today = seconds
        self._rebuild_menu()

    # ------------------------------------------------------------------ #
    def _rebuild_menu(self) -> None:
        if self._indicator is None:
            return
        try:
            import gi as _gi
            _gi.require_version("Gtk", "3.0")
        except ValueError:
            logger.warning(
                "Nie można załadować GTK 3.0 obok GTK 4.0 w tym samym procesie - "
                "menu zasobnika będzie niedostępne. AppIndicator3 wymaga GTK3."
            )
            return
        from gi.repository import Gtk as Gtk3  # noqa: E402

        menu = Gtk3.Menu()

        if self._channel_name:
            label = self._channel_name
            if self._program_title:
                label = f"{self._channel_name} — {self._program_title}"
        else:
            label = "Nic nie jest odtwarzane"
        now_item = Gtk3.MenuItem(label=label)
        now_item.set_sensitive(False)
        now_item.show()
        menu.append(now_item)

        hrs = self._watch_seconds_today // 3600
        mins = (self._watch_seconds_today % 3600) // 60
        watch_item = Gtk3.MenuItem(label=f"Dziś oglądano: {hrs:d}h {mins:02d}min")
        watch_item.set_sensitive(False)
        watch_item.show()
        menu.append(watch_item)

        if self._reminders:
            sep = Gtk3.SeparatorMenuItem()
            sep.show()
            menu.append(sep)
            header = Gtk3.MenuItem(label="Zaplanowane przypomnienia:")
            header.set_sensitive(False)
            header.show()
            menu.append(header)
            for r in self._reminders:
                when = time.strftime("%H:%M", time.localtime(r.start))
                item = Gtk3.MenuItem(label=f"⏰ {when}  {r.title} ({r.channel_name})")
                item.set_sensitive(False)
                item.show()
                menu.append(item)

        sep2 = Gtk3.SeparatorMenuItem()
        sep2.show()
        menu.append(sep2)

        toggle_item = Gtk3.MenuItem(label="Pokaż/ukryj okno")
        toggle_item.connect("activate", lambda *_: self._on_toggle_window())
        toggle_item.show()
        menu.append(toggle_item)

        quit_item = Gtk3.MenuItem(label="Zakończ")
        quit_item.connect("activate", lambda *_: self._on_quit())
        quit_item.show()
        menu.append(quit_item)

        try:
            self._indicator.set_menu(menu)
        except Exception:
            logger.exception("Nie udało się zaktualizować menu zasobnika")
