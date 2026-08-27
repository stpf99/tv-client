from __future__ import annotations

import time
from datetime import datetime, date
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Adw, Pango, GLib  # noqa: E402

from tvh.library import TvhLibrary
from tvh.models import EpgEvent
from ui.epg_search import EpgQuery, parse_query


def _fmt_range(start: int, stop: int, now_ts: int | None = None) -> str:
    """Pełny 24h zegar + data gdy program nie jest „dziś”.

    Przykłady:
      14:30–15:00          (dziś)
      23:50–00:40          (dziś → jutro, bez daty przy starcie)
      12.08 06:00–07:00    (jutro / inny dzień)
      12.08 23:50–13.08 00:40
    """
    if not start:
        return "--:--"
    now_ts = now_ts or int(time.time())
    today = date.fromtimestamp(now_ts)
    d_start = date.fromtimestamp(start)
    d_stop = date.fromtimestamp(stop) if stop else d_start

    t_start = datetime.fromtimestamp(start).strftime("%H:%M")
    t_stop = datetime.fromtimestamp(stop).strftime("%H:%M") if stop else "--:--"

    if d_start == today and d_stop == today:
        return f"{t_start}–{t_stop}"
    if d_start == today and d_stop != today:
        # start dziś, koniec jutro – data tylko przy końcu
        return f"{t_start}–{d_stop.strftime('%d.%m')} {t_stop}"
    if d_start == d_stop:
        return f"{d_start.strftime('%d.%m')} {t_start}–{t_stop}"
    return f"{d_start.strftime('%d.%m')} {t_start}–{d_stop.strftime('%d.%m')} {t_stop}"


def _day_label(ts: int, now_ts: int) -> str:
    """Etykieta dnia: Dziś / Jutro / Wtorek 12.08 …"""
    d = date.fromtimestamp(ts)
    today = date.fromtimestamp(now_ts)
    delta = (d - today).days
    if delta == 0:
        return "Dziś"
    if delta == 1:
        return "Jutro"
    if delta == -1:
        return "Wczoraj"
    # lokalna nazwa dnia tygodnia
    names = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
    return f"{names[d.weekday()]} {d.strftime('%d.%m')}"


class EpgEventRow(Gtk.Box):
    """Jeden program w „gazecie” EPG: czas + tytuł + ikony PVR."""

    def __init__(
        self,
        library: TvhLibrary,
        channel_id: int,
        event: EpgEvent,
        tag: str = "",
        is_current: bool = False,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.library = library
        self.channel_id = channel_id
        self.event = event
        self.set_margin_start(4)
        self.set_margin_end(4)
        self.set_margin_top(2)
        self.set_margin_bottom(2)

        if tag:
            tag_lbl = Gtk.Label(label=tag)
            tag_lbl.add_css_class("caption")
            tag_lbl.add_css_class("dim-label")
            tag_lbl.set_width_chars(10)
            tag_lbl.set_xalign(0)
            self.append(tag_lbl)

        time_lbl = Gtk.Label(label=_fmt_range(event.start, event.stop))
        time_lbl.add_css_class("caption")
        time_lbl.set_width_chars(18)
        time_lbl.set_xalign(0)
        time_lbl.set_tooltip_text(
            f"{datetime.fromtimestamp(event.start).strftime('%Y-%m-%d %H:%M')} – "
            f"{datetime.fromtimestamp(event.stop).strftime('%Y-%m-%d %H:%M')}"
        )
        self.append(time_lbl)

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0, hexpand=True)
        title_lbl = Gtk.Label(label=event.title or "(bez tytułu)", xalign=0)
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.set_hexpand(True)
        if is_current:
            title_lbl.add_css_class("heading")
        else:
            title_lbl.add_css_class("caption")
        title_box.append(title_lbl)
        if event.subtitle:
            sub = Gtk.Label(label=event.subtitle, xalign=0)
            sub.add_css_class("dim-label")
            sub.add_css_class("caption")
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            title_box.append(sub)
        self.append(title_box)

        # --- Ikony PVR: jednorazowo / seria / ręcznie -----------------
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)

        rec_once = Gtk.Button(icon_name="media-record-symbolic")
        rec_once.add_css_class("flat")
        rec_once.add_css_class("circular")
        rec_once.set_tooltip_text("Nagraj jednorazowo")
        rec_once.connect("clicked", self._on_record_once)
        btn_box.append(rec_once)

        rec_series = Gtk.Button(icon_name="view-continuous-symbolic")
        rec_series.add_css_class("flat")
        rec_series.add_css_class("circular")
        rec_series.set_tooltip_text("Zaplanuj serię (autorec)")
        rec_series.connect("clicked", self._on_record_series)
        btn_box.append(rec_series)

        rec_manual = Gtk.Button(icon_name="appointment-new-symbolic")
        rec_manual.add_css_class("flat")
        rec_manual.add_css_class("circular")
        rec_manual.set_tooltip_text("Ręczne nagranie (okno czasowe tego programu)")
        rec_manual.connect("clicked", self._on_record_manual)
        btn_box.append(rec_manual)

        self.append(btn_box)

    def _on_record_once(self, *_a) -> None:
        self.library.record_event(self.channel_id, self.event.event_id)

    def _on_record_series(self, *_a) -> None:
        title = self.event.title or "Seria"
        self.library.record_series(
            title=title,
            channel_id=self.channel_id,
            event_id=self.event.event_id,
        )

    def _on_record_manual(self, *_a) -> None:
        self.library.record_manual(
            channel_id=self.channel_id,
            title=self.event.title or "Ręczne nagranie",
            start=self.event.start,
            stop=self.event.stop,
        )


class EpgChannelRow(Gtk.ListBoxRow):
    """Kanał + sąsiadujące programy (gazeta EPG): TERAZ + kolejne z pełnych danych."""

    MAX_EVENTS = 6  # teraz + następne

    def __init__(self, library: TvhLibrary, channel_id: int, channel_name: str) -> None:
        super().__init__()
        self.library = library
        self.channel_id = channel_id
        self.channel_name = channel_name
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=channel_name, xalign=0, hexpand=True)
        title.add_css_class("title-4")
        header.append(title)
        self.day_hint = Gtk.Label(xalign=1)
        self.day_hint.add_css_class("caption")
        self.day_hint.add_css_class("dim-label")
        header.append(self.day_hint)
        box.append(header)

        self.events_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(self.events_box)

        self.set_child(box)
        self.refresh()

    def refresh(self, query: Optional[EpgQuery] = None) -> bool:
        """Odswieza wiersz kanalu. Zwraca True, jesli kanal ma cokolwiek
        do pokazania (przy aktywnym query - czy jakikolwiek program
        pasuje do wyszukiwania), False gdy wiersz nalezy ukryc."""
        # wyczyść poprzednie wiersze programów
        while (child := self.events_box.get_first_child()) is not None:
            self.events_box.remove(child)

        now = int(time.time())

        def _sane(e) -> bool:
            if not e.start or not e.stop or e.stop <= e.start:
                return False
            # max 12 h – odrzuć zepsute wpisy (wcześniej: programy po kilka miesięcy)
            if (e.stop - e.start) > 12 * 3600:
                return False
            return True

        events = [
            e for e in self.library.events_by_channel.get(self.channel_id, []) if _sane(e)
        ]

        has_query = query is not None and not query.is_empty
        if has_query:
            events = [e for e in events if query.matches(e, self.channel_name, now)]
            if not events:
                return False
            # przy aktywnym wyszukiwaniu pokazujemy dopasowane programy
            # chronologicznie, bez sztucznego dzielenia na TERAZ/NASTĘPNIE
            events.sort(key=lambda e: e.start)
            shown = events[: self.MAX_EVENTS]
            tags = [""] * len(shown)
            is_curr = [
                e.start <= now < e.stop for e in shown
            ]
            if shown:
                self.day_hint.set_text(_day_label(shown[0].start, now))
            else:
                self.day_hint.set_text("")
            for i, ev in enumerate(shown):
                row = EpgEventRow(
                    self.library,
                    self.channel_id,
                    ev,
                    tag=tags[i] if i < len(tags) else "",
                    is_current=is_curr[i] if i < len(is_curr) else False,
                )
                self.events_box.append(row)
            return True

        # Aktualny program: eventId + weryfikacja start/stop vs time.time()
        current = self.library.current_event_for_channel(self.channel_id, now)
        current_idx = None
        if current is not None and _sane(current):
            for i, e in enumerate(events):
                if e.event_id == current.event_id:
                    current_idx = i
                    break
            if current_idx is None:
                events = [current] + [e for e in events if e.event_id != current.event_id]
                events.sort(key=lambda e: e.start)
                current_idx = next(
                    (i for i, e in enumerate(events) if e.event_id == current.event_id),
                    0,
                )

        if current_idx is None:
            # brak „teraz” – pokaż pierwsze przyszłe (sąsiedzi)
            upcoming = [e for e in events if e.start > now][: self.MAX_EVENTS]
            if not upcoming:
                empty = Gtk.Label(label="Brak danych EPG", xalign=0)
                empty.add_css_class("dim-label")
                empty.add_css_class("caption")
                self.events_box.append(empty)
                self.day_hint.set_text("")
                return True
            shown = upcoming
            tags = ["NASTĘPNIE"] + [""] * (len(shown) - 1)
            is_curr = [False] * len(shown)
        else:
            shown = events[current_idx : current_idx + self.MAX_EVENTS]
            tags = ["TERAZ"] + (["NASTĘPNIE"] if len(shown) > 1 else []) + [""] * max(
                0, len(shown) - 2
            )
            is_curr = [i == 0 for i in range(len(shown))]

        # podpowiedź dnia z pierwszego pokazanego programu (np. „Jutro” gdy poranek z jutra)
        if shown:
            self.day_hint.set_text(_day_label(shown[0].start, now))
        else:
            self.day_hint.set_text("")

        for i, ev in enumerate(shown):
            row = EpgEventRow(
                self.library,
                self.channel_id,
                ev,
                tag=tags[i] if i < len(tags) else "",
                is_current=is_curr[i] if i < len(is_curr) else False,
            )
            self.events_box.append(row)
        return True

class EpgView(Gtk.Box):
    """Przewodnik: lista kanałów z sąsiadującymi programami (gazeta EPG),
    pełne dane + data + 24h zegar + ikony PVR (jednorazowo / seria / ręcznie).
    """

    def __init__(self, library: TvhLibrary) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.library = library
        self._query: Optional[EpgQuery] = None

        # pasek narzędzi: wyszukiwarka / odśwież
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(12)
        toolbar.set_margin_end(12)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(True)
        self.search_entry.set_placeholder_text(
            "Szukaj: tytuł, opis, kanał, gatunek (film, sport…), data (12.08, jutro), godzina (20:00-22:00, po 20)"
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        toolbar.append(self.search_entry)

        self.results_hint = Gtk.Label(xalign=1)
        self.results_hint.add_css_class("caption")
        self.results_hint.add_css_class("dim-label")
        toolbar.append(self.results_hint)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text("Odśwież widok EPG")
        refresh_btn.connect("clicked", lambda *_: self.reload())
        toolbar.append(refresh_btn)
        self.append(toolbar)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_top(6)
        self.listbox.set_margin_bottom(10)
        self.listbox.set_margin_start(10)
        self.listbox.set_margin_end(10)
        scroller.set_child(self.listbox)
        self.append(scroller)

        library.connect("channels-changed", lambda *_: self.reload())
        library.connect("epg-changed", lambda _lib, ch_id: self._refresh_channel(ch_id))
        library.connect("initial-sync-done", lambda *_: self.reload())
        self.reload()

        # okresowe odświeżenie „TERAZ” (co 60 s)
        GLib.timeout_add_seconds(60, self._tick)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        text = (entry.get_text() or "").strip()
        self._query = parse_query(text) if text else None
        self.reload()

    def _tick(self) -> bool:
        query = self._query
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if isinstance(row, EpgChannelRow):
                row.refresh(query)
            i += 1
        return True

    def reload(self) -> None:
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)

        query = self._query
        has_query = query is not None and not query.is_empty
        shown = 0

        for ch in self.library.tv_channels() + self.library.radio_channels():
            row = EpgChannelRow(self.library, ch.channel_id, ch.name)
            if has_query:
                if not row.refresh(query):
                    continue
            self.listbox.append(row)
            shown += 1

        if has_query:
            self.results_hint.set_text(
                f"{shown} kanał" if shown == 1 else f"{shown} kanałów" if shown else "Brak wyników"
            )
        else:
            self.results_hint.set_text("")

    def _refresh_channel(self, channel_id: int) -> None:
        query = self._query
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if isinstance(row, EpgChannelRow) and row.channel_id == channel_id:
                visible = row.refresh(query)
                if query is not None and not query.is_empty and not visible:
                    self.listbox.remove(row)
                break
            i += 1
