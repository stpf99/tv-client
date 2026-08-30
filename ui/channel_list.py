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
from tvh.hbbtv import HbbtvApp
from ui.icon_cache import make_icon_widget

FAV_TAG = "__favorites__"  # sentinel dla wirtualnego "tagu" Ulubione w chipach


class HbbtvAppRow(Gtk.ListBoxRow):
    """Pojedyncza pozycja w podliscie aplikacji HbbTV danego kanalu -
    pokazuje nazwe aplikacji i pozwala otworzyc jej URL (np. w zewnetrznej
    przegladarce/WebKit) klikniciem."""

    def __init__(self, app: HbbtvApp, on_activate=None) -> None:
        super().__init__()
        self.app = app
        self.on_activate = on_activate
        self.add_css_class("tvh-hbbtv-app-row")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(34)  # wciecie - sygnalizuje ze to sub-pozycja kanalu
        box.set_margin_end(10)

        icon = Gtk.Image.new_from_icon_name("applications-internet-symbolic")
        icon.set_pixel_size(16)
        box.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text_box.set_hexpand(True)

        name_lbl = Gtk.Label(label=app.display_name, xalign=0)
        name_lbl.add_css_class("caption-heading")
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        name_lbl.set_hexpand(True)
        name_lbl.set_max_width_chars(1)
        text_box.append(name_lbl)

        detail = f"[{app.section}]" if app.section else ""
        if app.lang:
            detail = f"{detail} {app.lang}".strip()
        if app.visibility:
            detail = f"{detail} · {app.visibility}".strip(" ·")
        if app.url:
            detail = f"{detail} · {app.url}".strip(" ·")
        detail_lbl = Gtk.Label(label=detail, xalign=0)
        detail_lbl.add_css_class("dim-label")
        detail_lbl.add_css_class("caption")
        detail_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        detail_lbl.set_hexpand(True)
        detail_lbl.set_max_width_chars(1)
        detail_lbl.set_tooltip_text(app.url or "")
        text_box.append(detail_lbl)

        box.append(text_box)
        self.set_child(box)
        self.set_activatable(bool(app.url))

    def activate_app(self) -> None:
        if self.on_activate and self.app.url:
            self.on_activate(self.app)


class ChannelRow(Gtk.ListBoxRow):
    def __init__(self, channel: Channel, library: TvhLibrary, on_favorite_toggled=None,
                 on_hbbtv_app_activate=None) -> None:
        super().__init__()
        self.channel = channel
        self.on_favorite_toggled = on_favorite_toggled
        self.on_hbbtv_app_activate = on_hbbtv_app_activate
        self._icon_url = library.resolve_icon_url(channel.icon_url)
        self.add_css_class("tvh-channel-row")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(10)
        box.set_margin_end(10)

        self.number_lbl = Gtk.Label(label=str(channel.number or "—"))
        self.number_lbl.add_css_class("dim-label")
        self.number_lbl.set_width_chars(4)
        box.append(self.number_lbl)

        self.icon_widget = make_icon_widget(
            "tv-symbolic" if not channel.is_radio else "audio-input-microphone-symbolic",
            28,
            self._icon_url,
        )
        box.append(self.icon_widget)

        # Kolumna tekstowa MUSI miec ograniczona/elastyczna szerokosc i
        # etykiety z ellipsize, inaczej dlugie nazwy kanalow rozpychaja caly
        # panel (ktory jest nakladka o STALEJ szerokosci nad wideo) zamiast
        # zawijac sie/skracac w jego obrebie.
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)

        title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.title_lbl = Gtk.Label(label=channel.name, xalign=0)
        self.title_lbl.add_css_class("title-4")
        self.title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.title_lbl.set_hexpand(True)
        self.title_lbl.set_max_width_chars(1)  # wraz z hexpand wymusza zawijanie do dostepnej szerokosci, nie do tresci
        self.title_lbl.set_tooltip_text(channel.name)
        title_row.append(self.title_lbl)

        # Znacznik "ma HbbTV" - widoczny dopiero po wykryciu AIT (patrz
        # set_hbbtv_apps/ChannelListView._on_hbbtv_changed).
        self.hbbtv_badge = Gtk.Label(label="HbbTV")
        self.hbbtv_badge.add_css_class("tvh-hbbtv-badge")
        self.hbbtv_badge.add_css_class("caption")
        self.hbbtv_badge.set_visible(False)
        title_row.append(self.hbbtv_badge)
        text_box.append(title_row)

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

        # Przycisk rozwijania sublisty aplikacji HbbTV - ukryty dopoki nie
        # wykryto zadnej aplikacji na kanale.
        self.hbbtv_expander_btn = Gtk.ToggleButton()
        self.hbbtv_expander_btn.add_css_class("flat")
        self.hbbtv_expander_btn.add_css_class("circular")
        self.hbbtv_expander_btn.set_valign(Gtk.Align.CENTER)
        self.hbbtv_expander_btn.set_icon_name("pan-down-symbolic")
        self.hbbtv_expander_btn.set_tooltip_text("Aplikacje HbbTV")
        self.hbbtv_expander_btn.set_visible(False)
        self.hbbtv_expander_btn.connect("toggled", self._on_hbbtv_expander_toggled)
        box.append(self.hbbtv_expander_btn)

        outer.append(box)

        # Sublista pozycji "tv-hbbtv"/"radio-hbbtv" - dodatkowe strumienie
        # (aplikacje HbbTV) wykryte dla TEGO kanalu, pod jego wierszem.
        self.hbbtv_list = Gtk.ListBox()
        self.hbbtv_list.add_css_class("tvh-hbbtv-sublist")
        self.hbbtv_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.hbbtv_list.set_visible(False)
        self.hbbtv_list.connect("row-activated", self._on_hbbtv_row_activated)
        outer.append(self.hbbtv_list)

        self.set_child(outer)
        self.refresh_program(library)
        self._apps: list[HbbtvApp] = []

    def set_hbbtv_apps(self, apps: list) -> None:
        """Aktualizuje znacznik 'HbbTV' i sublista pozycji pod kanalem."""
        self._apps = list(apps)
        has_apps = bool(self._apps)
        self.hbbtv_badge.set_visible(has_apps)
        self.hbbtv_expander_btn.set_visible(has_apps)
        if not has_apps:
            self.hbbtv_expander_btn.set_active(False)

        child = self.hbbtv_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.hbbtv_list.remove(child)
            child = nxt
        for app in self._apps:
            self.hbbtv_list.append(HbbtvAppRow(app, on_activate=self.on_hbbtv_app_activate))

        kind = "radio-hbbtv" if self.channel.is_radio else "tv-hbbtv"
        self.hbbtv_expander_btn.set_tooltip_text(
            f"{len(self._apps)} aplikacja(-e) HbbTV [{kind}]" if has_apps else "Aplikacje HbbTV"
        )

    def _on_hbbtv_expander_toggled(self, btn: Gtk.ToggleButton) -> None:
        expanded = btn.get_active()
        self.hbbtv_list.set_visible(expanded)
        btn.set_icon_name("pan-up-symbolic" if expanded else "pan-down-symbolic")

    def _on_hbbtv_row_activated(self, _listbox, row: "HbbtvAppRow") -> None:
        row.activate_app()

    def update_channel(self, channel: Channel, library: TvhLibrary) -> None:
        """Odswieza istniejacy wiersz nowymi danymi kanalu (numer, nazwa,
        ikona, EPG) bez niszczenia/tworzenia widgetow od nowa - wolane z
        ChannelListView.reload() dla kanalow, ktore juz maja wiersz, zeby
        uniknac kosztu rekonstrukcji calej listy przy kazdej aktualizacji."""
        old_channel = self.channel
        self.channel = channel

        if old_channel.number != channel.number:
            self.number_lbl.set_label(str(channel.number or "—"))
        if old_channel.name != channel.name:
            self.title_lbl.set_label(channel.name)
            self.title_lbl.set_tooltip_text(channel.name)

        new_icon_url = library.resolve_icon_url(channel.icon_url)
        if new_icon_url != self._icon_url:
            from ui.icon_cache import update_icon_widget
            self._icon_url = new_icon_url
            update_icon_widget(self.icon_widget, new_icon_url)

        if old_channel.current_event_id != channel.current_event_id:
            self.refresh_program(library)

        if is_favorite(channel.channel_id, channel.is_radio) != is_favorite(old_channel.channel_id, old_channel.is_radio):
            self._sync_fav_icon()

    def apply_hbbtv_state(self, library: TvhLibrary) -> None:
        state = library.get_hbbtv_state(self.channel.channel_id)
        self.set_hbbtv_apps(state.apps if state else [])

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

    def __init__(self, library: TvhLibrary, radio: bool, on_play: Callable[[Channel], None],
                 on_hbbtv_app_activate: Optional[Callable[[Channel, object], None]] = None,
                 on_close: Optional[Callable[[], None]] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.set_hexpand(False)
        self.library = library
        self.radio = radio
        self.on_play = on_play
        self._on_hbbtv_app_activate = on_hbbtv_app_activate
        # Patrz komentarze w _on_channel_hbbtv_app_activate / _on_row_activated
        # ponizej - zabezpieczenie przed podwojna aktywacja (klik na
        # HbbtvAppRow bulgoczacy do glownego listbox kanalow).
        self._suppress_next_row_activation = False
        self.active_tag_id: Optional[int] = None
        self._tag_buttons: Dict[Optional[int], Gtk.ToggleButton] = {}
        # Indeks channel_id -> ChannelRow, zeby reload() mogl diffowac
        # zamiast burzyc i budowac cala liste od zera przy kazdym
        # channels-changed, a _refresh_row() mogl znalezc wiersz w O(1)
        # zamiast liniowego przeszukiwania Gtk.ListBox.
        self._rows: Dict[int, ChannelRow] = {}

        # Naglowek z przyciskiem X - reczne ukrywanie panelu bezposrednio z
        # niego samego, niezaleznie od tego czy belka OSD (z drugim takim
        # przyciskiem, list_btn w LiveView) jest akurat widoczna czy nie -
        # belka znika po 5s bez ruchu myszy, ten przycisk zostaje zawsze
        # dostepny dopoki panel jest otwarty.
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.set_margin_top(8)
        header.set_margin_start(8)
        header.set_margin_end(8)
        title = Gtk.Label(label="Kanały radiowe" if radio else "Kanały")
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        title.set_hexpand(True)
        header.append(title)
        if on_close is not None:
            close_btn = Gtk.Button()
            close_btn.set_child(Gtk.Image.new_from_icon_name("window-close-symbolic"))
            close_btn.add_css_class("flat")
            close_btn.add_css_class("circular")
            close_btn.set_tooltip_text("Ukryj listę kanałów")
            close_btn.connect("clicked", lambda _btn: on_close())
            header.append(close_btn)

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

        self.append(header)
        self.append(search)
        self.append(tag_scroller)
        self.append(scroller)

        self._search_text = ""
        library.connect("channels-changed", lambda *_: self.reload())
        library.connect("epg-changed", lambda _lib, ch_id: self._refresh_row(ch_id))
        library.connect("tags-changed", lambda *_: self._rebuild_tag_chips())
        library.connect("hbbtv-changed", lambda _lib, ch_id: self._refresh_hbbtv_row(ch_id))
        self._rebuild_tag_chips()
        self.reload()

    def _on_channel_hbbtv_app_activate(self, channel: Channel, app) -> None:
        # UWAGA: klikniecie w HbbtvAppRow (wiersz zagniezdzonej self.hbbtv_list
        # wewnatrz ChannelRow) bulgocze w gore i aktywuje TAKZE ChannelRow jako
        # wiersz glownej self.listbox (oba to Gtk.ListBoxRow, GtkListBox
        # aktywuje wiersz na klikniecie gdziekolwiek w jego obszarze - nie
        # zatrzymuje sie na granicy zagniezdzonej listy potomnej). Bez tej
        # flagi _on_row_activated ponizej odpalal on_play() zaraz PO
        # launch_hbbtv_app(), co przez LiveView.play_channel() natychmiast
        # zamykalo dopiero co otwarta aplikacje HbbTV (patrz log: "uruchamiam"
        # i "zamykam" w tej samej milisekundzie).
        self._suppress_next_row_activation = True
        if self._on_hbbtv_app_activate:
            self._on_hbbtv_app_activate(channel, app)

    def reload(self) -> None:
        channels = self.library.radio_channels() if self.radio else self.library.tv_channels()
        new_ids = [ch.channel_id for ch in channels]
        new_id_set = set(new_ids)

        # Usun wiersze kanalow, ktorych juz nie ma (usuniete na serwerze
        # albo przelaczyly sie miedzy TV/Radio).
        for old_id in list(self._rows.keys()):
            if old_id not in new_id_set:
                row = self._rows.pop(old_id)
                self.listbox.remove(row)

        # Dodaj/zaktualizuj: kanaly nieznane dostaja nowy ChannelRow,
        # znane dostaja odswiezone dane (numer/nazwa/ikona/EPG) na
        # istniejacym widgecie zamiast rekonstrukcji.
        for ch in channels:
            row = self._rows.get(ch.channel_id)
            if row is None:
                row = ChannelRow(
                    ch, self.library,
                    on_favorite_toggled=self._on_fav_changed,
                    on_hbbtv_app_activate=lambda app, c=ch: self._on_channel_hbbtv_app_activate(c, app),
                )
                self._rows[ch.channel_id] = row
                self.listbox.append(row)
                row.apply_hbbtv_state(self.library)
                # Zapytanie JSON API w tle - nie blokuje UI, wynik przyjdzie
                # przez sygnal "hbbtv-changed" jesli kanal ma HbbTV.
                self.library.fetch_hbbtv_for_channel(ch)
            else:
                row.update_channel(ch, self.library)

        # Kolejnosc: Gtk.ListBox nie ma taniego "przesun wiersz na pozycje
        # N", wiec zamiast tego przestawiamy tylko wiersze, ktore faktycznie
        # sa nie na swoim miejscu wzgledem docelowej kolejnosci (typowo
        # zero lub kilka przy zwyklej aktualizacji EPG/nazw, nie setki).
        for target_index, ch_id in enumerate(new_ids):
            row = self._rows[ch_id]
            current_index = row.get_index()
            if current_index != target_index:
                self.listbox.remove(row)
                self.listbox.insert(row, target_index)

    def _on_fav_changed(self) -> None:
        # Jeśli filtr "Ulubione" jest aktywny, odznaczenie gwiazdki musi
        # natychmiast schować wiersz z widoku.
        if self.active_tag_id == FAV_TAG:
            self.listbox.invalidate_filter()

    def _refresh_row(self, channel_id: int) -> None:
        row = self._rows.get(channel_id)
        if row is not None:
            row.refresh_program(self.library)

    def _refresh_hbbtv_row(self, channel_id: int) -> None:
        row = self._rows.get(channel_id)
        if row is not None:
            row.apply_hbbtv_state(self.library)

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
        # Patrz komentarz w _on_channel_hbbtv_app_activate: klikniecie w
        # HbbtvAppRow potrafi wywolac TAKZE ta aktywacje glownego wiersza
        # (ta sama sekwencja zdarzen GTK). Pomijamy ja jednorazowo, zeby nie
        # zamknac aplikacji HbbTV zaraz po jej uruchomieniu.
        if self._suppress_next_row_activation:
            self._suppress_next_row_activation = False
            return
        self.on_play(row.channel)
