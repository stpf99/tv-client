"""Siatka EPG z osią czasu (widok "grid"): kolumna z nazwami kanałów +
poziomo przewijalna siatka bloków audycji ułożonych wg czasu, kolorowanych
wg gatunku (DVB content_type), z czerwoną linią "teraz" i nawigacją
dzień/godzina - odpowiednik zrzutu ekranu 04-epg-grid.png z oryginalnego
projektu, którego w kodzie nie było zaimplementowane (tylko widok gazetowy).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Pango  # noqa: E402

from tvh.library import TvhLibrary
from tvh.models import Channel, EpgEvent
from tvh.genres import genre_color, genre_label

PX_PER_MIN = 3.2          # szerokość siatki: 1 minuta = 3.2 px (~192px/h)
ROW_HEIGHT = 56
HEADER_HEIGHT = 32
CHANNEL_COL_WIDTH = 160
VISIBLE_WINDOW_MIN = 6 * 60  # ile minut od lewej krawędzi ładujemy na raz


class EpgBlock(Gtk.Button):
    """Pojedynczy blok audycji w siatce - pozycjonowany i rozmiarowany
    ręcznie przez rodzica (Gtk.Fixed), kolor wg gatunku."""

    def __init__(self, event: EpgEvent, on_click: Callable[[EpgEvent, int], None], channel_id: int) -> None:
        super().__init__()
        self.event = event
        self.add_css_class("flat")
        self.add_css_class("tvh-epg-block")
        self.set_has_frame(False)

        lbl = Gtk.Label(xalign=0)
        title = event.title or "(bez tytułu)"
        lbl.set_text(title)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_margin_start(6)
        lbl.set_margin_end(4)
        lbl.add_css_class("caption")
        self.set_child(lbl)

        color = genre_color(event.content_type)
        css = Gtk.CssProvider()
        css.load_from_data(
            f".tvh-epg-block {{ background-color: {color}; color: white; "
            f"border-radius: 3px; }}".encode("utf-8")
        )
        self.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        when = f"{time.strftime('%H:%M', time.localtime(event.start))}–{time.strftime('%H:%M', time.localtime(event.stop))}"
        tip = f"{title}\n{when} · {genre_label(event.content_type)}"
        if event.subtitle:
            tip += f"\n{event.subtitle}"
        self.set_tooltip_text(tip)

        self.connect("clicked", lambda *_: on_click(event, channel_id))


class EpgGridView(Gtk.Box):
    """Siatka: lewa zamrożona kolumna nazw kanałów (przewija się pionowo
    razem z siatką) + prawa część z osią czasu u góry i blokami audycji,
    przewijalna w obu osiach. Synchronizacja przewijania pionowego między
    kolumną nazw a siatką przez współdzielony Gtk.Adjustment.
    """

    def __init__(
        self,
        library: TvhLibrary,
        on_event_click: Callable[[EpgEvent, int], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.library = library
        self.on_event_click = on_event_click
        self.channels: List[Channel] = []

        # dzień aktualnie wyświetlany - północ lokalnego czasu
        now = datetime.now()
        self.day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        # punkt startowy widocznego okna siatki (godzina) - domyślnie teraz
        self.window_start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) - timedelta(minutes=30)

        # --- nawigacja dnia ---
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        nav.set_margin_start(12)
        nav.set_margin_end(12)
        nav.set_margin_top(4)
        nav.set_margin_bottom(4)

        prev_btn = Gtk.Button(icon_name="go-previous-symbolic")
        prev_btn.add_css_class("flat")
        prev_btn.connect("clicked", lambda *_: self._shift_window(-120))
        nav.append(prev_btn)

        today_btn = Gtk.Button(label="Teraz")
        today_btn.connect("clicked", lambda *_: self._jump_to_now())
        nav.append(today_btn)

        next_btn = Gtk.Button(icon_name="go-next-symbolic")
        next_btn.add_css_class("flat")
        next_btn.connect("clicked", lambda *_: self._shift_window(120))
        nav.append(next_btn)

        self.date_lbl = Gtk.Label(xalign=0)
        self.date_lbl.add_css_class("dim-label")
        self.date_lbl.set_margin_start(8)
        nav.append(self.date_lbl)
        self.append(nav)

        # --- obszar siatki: kolumna nazw (statyczna szerokość) + reszta ---
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        body.set_vexpand(True)
        self.append(body)

        # kolumna nazw kanałów - własny pionowy scroller, bez poziomego
        names_scroller = Gtk.ScrolledWindow()
        names_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        names_scroller.set_size_request(CHANNEL_COL_WIDTH, -1)
        self.names_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        spacer = Gtk.Box()
        spacer.set_size_request(-1, HEADER_HEIGHT)
        self.names_box.append(spacer)
        names_scroller.set_child(self.names_box)
        body.append(names_scroller)

        # siatka: przewijalna w obu kierunkach; header godzin + wiersze
        self.grid_scroller = Gtk.ScrolledWindow()
        self.grid_scroller.set_hexpand(True)
        grid_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self.hours_fixed = Gtk.Fixed()
        self.hours_fixed.set_size_request(VISIBLE_WINDOW_MIN * PX_PER_MIN, HEADER_HEIGHT)
        grid_outer.append(self.hours_fixed)

        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        grid_outer.append(self.rows_box)

        self.grid_scroller.set_child(grid_outer)
        body.append(self.grid_scroller)

        # zsynchronizuj przewijanie pionowe: kolumna nazw <-> siatka
        self._syncing = False
        names_vadj = names_scroller.get_vadjustment()
        grid_vadj = self.grid_scroller.get_vadjustment()

        def _sync(source_adj, target_adj):
            if self._syncing:
                return
            self._syncing = True
            target_adj.set_value(source_adj.get_value())
            self._syncing = False

        names_vadj.connect("value-changed", lambda a: _sync(a, grid_vadj))
        grid_vadj.connect("value-changed", lambda a: _sync(a, names_vadj))

        library.connect("channels-changed", lambda *_: self.reload())
        library.connect("epg-changed", lambda *_a: self._redraw_events())
        library.connect("initial-sync-done", lambda *_: self.reload())

        self.reload()
        GLib.timeout_add_seconds(60, self._tick_now_line)

    # ------------------------------------------------------------------ #
    def _shift_window(self, minutes: int) -> None:
        self.window_start += timedelta(minutes=minutes)
        self._redraw_events()

    def _jump_to_now(self) -> None:
        now = datetime.now()
        self.window_start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) - timedelta(minutes=30)
        self._redraw_events()

    def _tick_now_line(self) -> bool:
        self._redraw_events()
        return True

    # ------------------------------------------------------------------ #
    def reload(self) -> None:
        children = []
        c = self.names_box.get_first_child()
        while c is not None:
            children.append(c)
            c = c.get_next_sibling()
        for c in children[1:]:
            self.names_box.remove(c)

        rc = self.rows_box.get_first_child()
        while rc is not None:
            nxt = rc.get_next_sibling()
            self.rows_box.remove(rc)
            rc = nxt

        self.channels = self.library.tv_channels() + self.library.radio_channels()
        for ch in self.channels:
            name_lbl = Gtk.Label(label=f"{ch.number or ''} {ch.name}".strip(), xalign=0)
            name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            name_lbl.set_margin_start(8)
            name_lbl.set_margin_end(4)
            name_lbl.set_size_request(CHANNEL_COL_WIDTH, ROW_HEIGHT)
            name_lbl.add_css_class("caption-heading")
            self.names_box.append(name_lbl)

            row_fixed = Gtk.Fixed()
            row_fixed.set_size_request(VISIBLE_WINDOW_MIN * PX_PER_MIN, ROW_HEIGHT)
            row_fixed.add_css_class("tvh-epg-row")
            self.rows_box.append(row_fixed)

        self._redraw_events()

    def _redraw_events(self) -> None:
        # header godzin
        c = self.hours_fixed.get_first_child()
        while c is not None:
            nxt = c.get_next_sibling()
            self.hours_fixed.remove(c)
            c = nxt

        win_start_ts = int(self.window_start.timestamp())
        win_end_ts = win_start_ts + VISIBLE_WINDOW_MIN * 60

        t = self.window_start
        x = 0.0
        while t.timestamp() < win_end_ts:
            lbl = Gtk.Label(label=t.strftime("%H:%M"))
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            self.hours_fixed.put(lbl, x + 4, 8)
            sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            sep.set_size_request(1, HEADER_HEIGHT)
            self.hours_fixed.put(sep, x, 0)
            t += timedelta(minutes=30)
            x += 30 * PX_PER_MIN

        self.date_lbl.set_text(
            self.window_start.strftime("%A %d.%m.%Y, od %H:%M").capitalize()
        )

        row_fixed = self.rows_box.get_first_child()
        idx = 0
        now_ts = int(time.time())
        for ch in self.channels:
            if row_fixed is None:
                break
            child = row_fixed.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                row_fixed.remove(child)
                child = nxt

            events = self.library.events_by_channel.get(ch.channel_id, [])
            for ev in events:
                if not ev.start or not ev.stop or ev.stop <= ev.start:
                    continue
                if ev.stop <= win_start_ts or ev.start >= win_end_ts:
                    continue
                block_start = max(ev.start, win_start_ts)
                block_stop = min(ev.stop, win_end_ts)
                x = (block_start - win_start_ts) / 60.0 * PX_PER_MIN
                w = max(20.0, (block_stop - block_start) / 60.0 * PX_PER_MIN - 2)
                block = EpgBlock(ev, self.on_event_click, ch.channel_id)
                block.set_size_request(int(w), ROW_HEIGHT - 4)
                row_fixed.put(block, x + 1, 2)

            if win_start_ts <= now_ts < win_end_ts:
                line = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
                line.add_css_class("tvh-epg-now-line")
                line.set_size_request(2, ROW_HEIGHT)
                x_now = (now_ts - win_start_ts) / 60.0 * PX_PER_MIN
                row_fixed.put(line, x_now, 0)

            row_fixed = row_fixed.get_next_sibling()
            idx += 1
