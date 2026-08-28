"""Minimalizacja do zasobnika/tla z podgladem OSD-info.

WAZNA UWAGA TECHNICZNA: klasyczne "ikony w zasobniku systemowym" na Linuksie
(AppIndicator3/AyatanaAppIndicator3) sa zaimplementowane wylacznie jako
biblioteka GTK3 (jej menu to Gtk3.Menu, nie Gio.Menu) - PyGObject NIE
pozwala zaladowac GTK3 i GTK4 w tym samym procesie (gi.require_version
rzuca ValueError, jesli namespace "Gtk" jest juz zarejestrowany w innej
wersji), a main.py laduje GTK4 od pierwszej linii. Proba polaczenia
AppIndicator3 z ta aplikacja dawalaby wiec ikone bez dzialajacego menu -
nie ma sensu tego udawac ani tego dostarczac.

Zamiast tego: prawdziwe minimalizowanie GTK4 "do tla" (ukrycie okna zamiast
zamkniecia + Gio.Application.hold(), zeby proces zyl bez otwartego okna) w
polaczeniu z powiadomieniami systemowymi (Gio.Notification - dziala wszedzie,
bez dodatkowych zaleznosci systemowych) jako kanal informacji "co gra teraz"
zamiast statycznej ikony w zasobniku z menu.

Przywrocenie okna: klikniecie w powiadomienie (akcja app.tvh-show-window),
lub ponowne uruchomienie aplikacji - Gio.Application z unikalnym
application-id wykrywa juz dzialajaca instancje i po prostu aktywuje ja
zamiast tworzyc nowy proces (standardowe zachowanie GApplication).
"""
from __future__ import annotations

import logging
import time
from typing import List

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio  # noqa: E402

logger = logging.getLogger("ui.tray")

_STATUS_NOTIFICATION_ID = "tvh-now-playing"


class BackgroundController:
    """Zarzadza stanem 'zminimalizowane do tla': ukrywanie/przywracanie
    okna, trzymanie procesu przy zyciu (Gio.Application.hold/release),
    i powiadomienie systemowe pokazujace co aktualnie gra + najblizsze
    przypomnienie + czas ogladania, aktualizowane w miejscu (ta sama
    notification-id, wiec nie mnozy sie w centrum powiadomien)."""

    def __init__(self, application: Gio.Application, window: Gtk.Window) -> None:
        self._app = application
        self._window = window
        self._is_hidden = False
        self._held = False

        self._channel_name = ""
        self._program_title = ""
        self._reminders: List = []
        self._watch_seconds_today = 0

        window.connect("close-request", self._on_close_request)

    # ------------------------------------------------------------------ #
    @property
    def is_hidden(self) -> bool:
        return self._is_hidden

    def hide_to_background(self) -> None:
        if self._is_hidden:
            return
        self._is_hidden = True
        if not self._held:
            self._app.hold()
            self._held = True
        self._window.set_visible(False)
        self._push_status_notification(force=True)
        logger.info("Okno zminimalizowane do tła — aplikacja działa dalej w tle")

    def show_window(self) -> None:
        self._is_hidden = False
        self._window.set_visible(True)
        self._window.present()
        if self._held:
            self._app.release()
            self._held = False
        try:
            self._app.withdraw_notification(_STATUS_NOTIFICATION_ID)
        except Exception:
            pass

    def toggle(self) -> None:
        if self._is_hidden:
            self.show_window()
        else:
            self.hide_to_background()

    def _on_close_request(self, *_a) -> bool:
        # Zamkniecie okna (X) minimalizuje do tla zamiast konczyc proces.
        # Prawdziwe wyjscie: akcja app.tvh-quit (np. z menu aplikacji).
        self.hide_to_background()
        return True  # zatrzymaj domyslne niszczenie okna

    def quit(self) -> None:
        try:
            self._app.withdraw_notification(_STATUS_NOTIFICATION_ID)
        except Exception:
            pass
        if self._held:
            self._app.release()
            self._held = False
        self._app.quit()

    # ------------------------------------------------------------------ #
    # Stan "co gra teraz" pokazywany w powiadomieniu, gdy zminimalizowane
    # ------------------------------------------------------------------ #
    def set_now_playing(self, channel_name: str, program_title: str) -> None:
        self._channel_name = channel_name or ""
        self._program_title = program_title or ""
        if self._is_hidden:
            self._push_status_notification()

    def clear_now_playing(self) -> None:
        self._channel_name = ""
        self._program_title = ""
        if self._is_hidden:
            self._push_status_notification()

    def set_reminders(self, reminders: list) -> None:
        self._reminders = list(reminders)[:5]
        if self._is_hidden:
            self._push_status_notification()

    def set_watch_seconds_today(self, seconds: int) -> None:
        self._watch_seconds_today = seconds
        if self._is_hidden:
            self._push_status_notification()

    def _push_status_notification(self, force: bool = False) -> None:
        if not self._is_hidden and not force:
            return
        title = self._channel_name or "TVHeadend – zminimalizowane"
        lines = []
        if self._channel_name:
            lines.append(self._program_title or "(brak danych EPG)")
        hrs = self._watch_seconds_today // 3600
        mins = (self._watch_seconds_today % 3600) // 60
        lines.append(f"Dziś oglądano: {hrs:d}h {mins:02d}min")
        if self._reminders:
            next_r = self._reminders[0]
            when = time.strftime("%H:%M", time.localtime(next_r.start))
            lines.append(f"Następne przypomnienie: {when} {next_r.title}")
        body = "\n".join(lines)

        try:
            notif = Gio.Notification.new(title)
            notif.set_body(body)
            notif.set_priority(Gio.NotificationPriority.LOW)
            notif.set_default_action("app.tvh-show-window")
            self._app.send_notification(_STATUS_NOTIFICATION_ID, notif)
        except Exception:
            logger.exception("Nie udało się zaktualizować powiadomienia o stanie")


def install_background_support(
    application: Gio.Application, window: Gtk.Window
) -> BackgroundController:
    """Tworzy BackgroundController i podpina akcje 'app.tvh-show-window'
    (klikniecie powiadomienia przywraca okno) oraz 'app.tvh-quit'."""
    ctrl = BackgroundController(application, window)

    show_action = Gio.SimpleAction.new("tvh-show-window", None)
    show_action.connect("activate", lambda *_: ctrl.show_window())
    application.add_action(show_action)

    quit_action = Gio.SimpleAction.new("tvh-quit", None)
    quit_action.connect("activate", lambda *_: ctrl.quit())
    application.add_action(quit_action)

    return ctrl
