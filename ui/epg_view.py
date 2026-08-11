from __future__ import annotations

import time
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

from tvh.library import TvhLibrary


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "--:--"


class EpgChannelRow(Gtk.ListBoxRow):
    def __init__(self, library: TvhLibrary, channel_id: int, channel_name: str) -> None:
        super().__init__()
        self.library = library
        self.channel_id = channel_id

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label(label=channel_name, xalign=0)
        title.add_css_class("title-4")
        box.append(title)

        self.current_row = self._make_event_row("TERAZ")
        self.next_row = self._make_event_row("NASTĘPNIE")
        box.append(self.current_row["box"])
        box.append(self.next_row["box"])

        self.set_child(box)
        self.refresh()

    def _make_event_row(self, tag: str) -> dict:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        tag_lbl = Gtk.Label(label=tag)
        tag_lbl.add_css_class("caption")
        tag_lbl.add_css_class("dim-label")
        tag_lbl.set_width_chars(10)
        time_lbl = Gtk.Label()
        time_lbl.add_css_class("caption")
        title_lbl = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        record_btn = Gtk.Button(icon_name="media-record-symbolic")
        record_btn.add_css_class("flat")
        record_btn.add_css_class("circular")
        box.append(tag_lbl)
        box.append(time_lbl)
        box.append(title_lbl)
        box.append(record_btn)
        return {"box": box, "time": time_lbl, "title": title_lbl, "record_btn": record_btn}

    def refresh(self) -> None:
        now = int(time.time())
        events = self.library.events_by_channel.get(self.channel_id, [])
        current = next((e for e in events if e.start <= now < e.stop), None)
        upcoming = next((e for e in events if e.start > now), None)

        if current:
            self.current_row["time"].set_text(f"{_fmt(current.start)}–{_fmt(current.stop)}")
            self.current_row["title"].set_text(current.title)
            self.current_row["record_btn"].set_visible(True)
            self.current_row["record_btn"].connect(
                "clicked", lambda *_: self.library.record_event(self.channel_id, current.event_id)
            )
        else:
            self.current_row["time"].set_text("")
            self.current_row["title"].set_text("Brak danych")
            self.current_row["record_btn"].set_visible(False)

        if upcoming:
            self.next_row["time"].set_text(f"{_fmt(upcoming.start)}–{_fmt(upcoming.stop)}")
            self.next_row["title"].set_text(upcoming.title)
            self.next_row["record_btn"].set_visible(True)
            self.next_row["record_btn"].connect(
                "clicked", lambda *_: self.library.record_event(self.channel_id, upcoming.event_id)
            )
        else:
            self.next_row["time"].set_text("")
            self.next_row["title"].set_text("")
            self.next_row["record_btn"].set_visible(False)


class EpgView(Gtk.Box):
    """Przewodnik: lista kanalow z 'teraz i za chwile', jak pasek EPG w Kodi."""

    def __init__(self, library: TvhLibrary) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.library = library

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_top(10)
        self.listbox.set_margin_bottom(10)
        self.listbox.set_margin_start(10)
        self.listbox.set_margin_end(10)
        scroller.set_child(self.listbox)
        self.append(scroller)

        library.connect("channels-changed", lambda *_: self.reload())
        library.connect("epg-changed", lambda _lib, ch_id: self._refresh_channel(ch_id))
        self.reload()

    def reload(self) -> None:
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        for ch in self.library.tv_channels() + self.library.radio_channels():
            self.listbox.append(EpgChannelRow(self.library, ch.channel_id, ch.name))

    def _refresh_channel(self, channel_id: int) -> None:
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if isinstance(row, EpgChannelRow) and row.channel_id == channel_id:
                row.refresh()
                break
            i += 1
