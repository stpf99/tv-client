from __future__ import annotations

import time
from typing import Callable, Dict, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Pango  # noqa: E402

from tvh.library import TvhLibrary
from tvh.models import Channel
from tvh.config import is_favorite, toggle_favorite

FAV_TAG = "__favorites__"  # sentinel dla wirtualnego "tagu" Ulubione w chipach


class ChannelRow(Gtk.ListBoxRow):
    def __init__(self, channel: Channel, library: TvhLibrary, on_favorite_toggled=None) -> None:
        super().__init__()
        self.channel = channel
        self.on_favorite_toggled = on_favorite_toggled
        self.add_css_class("tvh-channel-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(10)
        box.set_margin_end(10)

        number_lbl = Gtk.Label(label=str(channel.number or "—"))
        number_lbl.add_css_class("dim-label")
        number_lbl.set_width_chars(4)
        box.append(number_lbl)

        icon = Gtk.Image.new_from_icon_name("tv-symbolic" if not channel.is_radio else "audio-input-microphone-symbolic")
        icon.set_pixel_size(28)
        box.append(icon)

        # Kolumna tekstowa MUSI miec ograniczona/elastyczna szerokosc i
        # etykiety z ellipsize, inaczej dlugie nazwy kanalow rozpychaja caly
        # panel (ktory jest nakladka o STALEJ szerokosci nad wideo) zamiast
        # zawijac sie/skracac w jego obrebie.
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        title_lbl = Gtk.Label(label=channel.name, xalign=0)
        title_lbl.add_css_class("title-4")
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.set_hexpand(True)
        title_lbl.set_max_width_chars(1)  # wraz z hexpand wymusza zawijanie do dostepnej szerokosci, nie do tresci
        title_lbl.set_tooltip_text(channel.name)
        text_box.append(title_lbl)

        self.program_lbl = Gtk.Label(xalign=0)
        self.program_lbl.add_css_class("dim-label")
        self.program_lbl.add_css_class("caption")
        self.program_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.program_lbl.set_hexpand(True)
        self.program_lbl.set_max_width_chars(1)
        text_box.append(self.program_lbl)

        box.append(text_box)

        self.fav_btn = Gtk.ToggleButton()
        self.fav_btn.add_css_class("flat")
        self.fav_btn.add_css_class("circular")
        self.fav_btn.set_valign(Gtk.Align.CENTER)
        self.fav_btn.connect("toggled", self._on_fav_toggled)
        self._sync_fav_icon()
        box.append(self.fav_btn)

        self.set_child(box)
        self.refresh_program(library)

    def _sync_fav_icon(self) -> None:
        active = is_favorite(self.channel.channel_id, self.channel.is_radio)
        # blokujemy sygnal zeby uniknac rekursji przy programowym ustawieniu
        self.fav_btn.handler_block_by_func(self._on_fav_toggled)
        self.fav_btn.set_active(active)
        self.fav_btn.handler_unblock_by_func(self._on_fav_toggled)
        self.fav_btn.set_icon_name("starred-symbolic" if active else "non-starred-symbolic")
        self.fav_btn.set_tooltip_text("Usuń z ulubionych" if active else "Dodaj do ulubionych")

    def _on_fav_toggled(self, _btn) -> None:
        toggle_favorite(self.channel.channel_id, self.channel.is_radio)
        self._sync_fav_icon()
        if self.on_favorite_toggled:
            self.on_favorite_toggled()

    def refresh_program(self, library: TvhLibrary) -> None:
        from datetime import datetime, date

        now = int(time.time())
        ev = library.current_event_for_channel(self.channel.channel_id, now)
        if not ev:
            self.program_lbl.set_text("Brak danych EPG")
            self.program_lbl.set_tooltip_text("Brak danych EPG")
            return
        # data + 24h gdy program nie jest „dziś” (np. poranek z jutra)
        t0 = datetime.fromtimestamp(ev.start)
        t1 = datetime.fromtimestamp(ev.stop)
        today = date.fromtimestamp(now)
        if t0.date() != today:
            when = f"{t0.strftime('%d.%m %H:%M')}–{t1.strftime('%H:%M')}"
        else:
            when = f"{t0.strftime('%H:%M')}–{t1.strftime('%H:%M')}"
        text = f"{when}  {ev.title}"
        self.program_lbl.set_text(text)
        tip = (
            f"{ev.title}\n"
            f"{t0.strftime('%Y-%m-%d %H:%M')} – {t1.strftime('%Y-%m-%d %H:%M')}"
        )
        if ev.subtitle:
            tip += f"\n{ev.subtitle}"
        self.program_lbl.set_tooltip_text(tip)


class ChannelListView(Gtk.Box):
    """Panel z lista kanalow (TV lub Radio) - klikniecie odtwarza.

    Dodatkowo pozwala filtrowac liste po tagach pobranych z serwera
    (np. "HD", "SD", "Radio", grupy tematyczne itp.) - to samo, co w
    Tvheadend nazywa sie "channel tags" i czesto sluzy jako gotowe
    playlisty/kategorie kanalow.
    """

    def __init__(self, library: TvhLibrary, radio: bool, on_play: Callable[[Channel], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(False)
        self.library = library
        self.radio = radio
        self.on_play = on_play
        self.active_tag_id: Optional[int] = None
        self._tag_buttons: Dict[Optional[int], Gtk.ToggleButton] = {}

        search = Gtk.SearchEntry(placeholder_text="Szukaj kanału…")
        search.set_margin_top(8)
        search.set_margin_start(8)
        search.set_margin_end(8)
        search.connect("search-changed", self._on_search_changed)

        # --- Pasek "playlist" z tagow (SD/HD/Radio/...) pobranych z serwera
        self.tag_chip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.tag_chip_box.set_margin_start(8)
        self.tag_chip_box.set_margin_end(8)
        tag_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        tag_scroller.set_child(self.tag_chip_box)
        tag_scroller.set_margin_top(6)
        tag_scroller.set_propagate_natural_height(True)

        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_start(8)
        self.listbox.set_margin_end(8)
        self.listbox.set_margin_top(8)
        self.listbox.set_margin_bottom(8)
        self.listbox.set_filter_func(self._filter_func)
        self.listbox.connect("row-activated", self._on_row_activated)
        scroller.set_child(self.listbox)

        self.append(search)
        self.append(tag_scroller)
        self.append(scroller)

        self._search_text = ""
        library.connect("channels-changed", lambda *_: self.reload())
        library.connect("epg-changed", lambda _lib, ch_id: self._refresh_row(ch_id))
        library.connect("tags-changed", lambda *_: self._rebuild_tag_chips())
        self._rebuild_tag_chips()
        self.reload()

    def reload(self) -> None:
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        channels = self.library.radio_channels() if self.radio else self.library.tv_channels()
        for ch in channels:
            self.listbox.append(ChannelRow(ch, self.library, on_favorite_toggled=self._on_fav_changed))

    def _on_fav_changed(self) -> None:
        # Jeśli filtr "Ulubione" jest aktywny, odznaczenie gwiazdki musi
        # natychmiast schować wiersz z widoku.
        if self.active_tag_id == FAV_TAG:
            self.listbox.invalidate_filter()

    def _refresh_row(self, channel_id: int) -> None:
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if isinstance(row, ChannelRow) and row.channel.channel_id == channel_id:
                row.refresh_program(self.library)
                break
            i += 1

    # ------------------------------------------------------------------ #
    # Filtrowanie: tekst wyszukiwania + wybrany tag (SD/HD/Radio/...)
    # ------------------------------------------------------------------ #
    def _rebuild_tag_chips(self) -> None:
        child = self.tag_chip_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.tag_chip_box.remove(child)
            child = nxt
        self._tag_buttons.clear()

        all_btn = Gtk.ToggleButton(label="Wszystkie")
        all_btn.add_css_class("tvh-tag-chip")
        all_btn.set_active(self.active_tag_id is None)
        all_btn.connect("toggled", self._on_tag_toggled, None)
        self.tag_chip_box.append(all_btn)
        self._tag_buttons[None] = all_btn

        fav_btn = Gtk.ToggleButton(label="★ Ulubione")
        fav_btn.add_css_class("tvh-tag-chip")
        fav_btn.set_active(self.active_tag_id == FAV_TAG)
        fav_btn.connect("toggled", self._on_tag_toggled, FAV_TAG)
        self.tag_chip_box.append(fav_btn)
        self._tag_buttons[FAV_TAG] = fav_btn

        for tag in sorted(self.library.tags.values(), key=lambda t: t.name.lower()):
            btn = Gtk.ToggleButton(label=tag.name)
            btn.add_css_class("tvh-tag-chip")
            btn.set_active(self.active_tag_id == tag.tag_id)
            btn.connect("toggled", self._on_tag_toggled, tag.tag_id)
            self.tag_chip_box.append(btn)
            self._tag_buttons[tag.tag_id] = btn

        # jesli aktywny tag zniknal (np. usuniety na serwerze) - wracamy do "Wszystkie"
        # (FAV_TAG to wirtualny filtr, nie pochodzi z library.tags - pomijamy go tutaj)
        if (
            self.active_tag_id is not None
            and self.active_tag_id != FAV_TAG
            and self.active_tag_id not in self.library.tags
        ):
            self.active_tag_id = None
            all_btn.set_active(True)
            self.listbox.invalidate_filter()

    def _on_tag_toggled(self, btn: Gtk.ToggleButton, tag_id: Optional[int]) -> None:
        if not btn.get_active():
            return  # reagujemy tylko na wlaczenie - wylaczanie obslugujemy nizej
        self.active_tag_id = tag_id
        for tid, b in self._tag_buttons.items():
            if tid != tag_id:
                b.set_active(False)
        self.listbox.invalidate_filter()

    def _filter_func(self, row: ChannelRow) -> bool:
        if self._search_text and self._search_text not in row.channel.name.lower():
            return False
        if self.active_tag_id == FAV_TAG:
            if not is_favorite(row.channel.channel_id, row.channel.is_radio):
                return False
        elif self.active_tag_id is not None and self.active_tag_id not in row.channel.tag_ids:
            return False
        return True

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._search_text = entry.get_text().strip().lower()
        self.listbox.invalidate_filter()

    def _on_row_activated(self, _listbox, row: ChannelRow) -> None:
        self.on_play(row.channel)
