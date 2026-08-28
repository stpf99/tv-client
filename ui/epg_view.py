from __future__ import annotations

import time
from datetime import datetime, date, timedelta
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Adw, Pango, GLib  # noqa: E402

from tvh.library import TvhLibrary
from tvh.models import EpgEvent
from tvh.genres import all_genres
from ui.reminders import ReminderStore
from ui.epg_grid_view import EpgGridView


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
        channel_name: str = "",
        reminder_store: Optional[ReminderStore] = None,
        show_channel_name: bool = False,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.library = library
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.event = event
        self.reminder_store = reminder_store
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
        if show_channel_name and channel_name:
            ch_lbl = Gtk.Label(label=channel_name, xalign=0)
            ch_lbl.add_css_class("dim-label")
            ch_lbl.add_css_class("caption")
            ch_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            title_box.append(ch_lbl)
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

        if self.reminder_store is not None:
            self.remind_btn = Gtk.ToggleButton()
            self.remind_btn.add_css_class("flat")
            self.remind_btn.add_css_class("circular")
            has = self.reminder_store.has_reminder(event.event_id)
            self.remind_btn.set_active(has)
            self.remind_btn.set_icon_name("alarm-symbolic")
            self.remind_btn.set_tooltip_text(
                "Usuń przypomnienie" if has else "Przypomnij przed startem"
            )
            self.remind_btn.connect("toggled", self._on_remind_toggled)
            btn_box.append(self.remind_btn)

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

    def _on_remind_toggled(self, btn: Gtk.ToggleButton) -> None:
        if self.reminder_store is None:
            return
        if btn.get_active():
            self.reminder_store.add(
                event_id=self.event.event_id,
                channel_id=self.channel_id,
                channel_name=self.channel_name,
                title=self.event.title or "(bez tytułu)",
                start=self.event.start,
                stop=self.event.stop,
            )
            btn.set_tooltip_text("Usuń przypomnienie")
        else:
            self.reminder_store.remove(self.event.event_id)
            btn.set_tooltip_text("Przypomnij przed startem")


class EpgChannelRow(Gtk.ListBoxRow):
    """Kanał + sąsiadujące programy (gazeta EPG): TERAZ + kolejne z pełnych danych."""

    MAX_EVENTS = 6  # teraz + następne

    def __init__(self, library: TvhLibrary, channel_id: int, channel_name: str,
                 reminder_store: Optional[ReminderStore] = None) -> None:
        super().__init__()
        self.library = library
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.reminder_store = reminder_store

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

    def refresh(self) -> None:
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
                return
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
                channel_name=self.channel_name,
                reminder_store=self.reminder_store,
            )
            self.events_box.append(row)


class EpgView(Gtk.Box):
    """Przewodnik: trzy tryby widoku - siatka z osią czasu (grid, jak w
    04-epg-grid.png), lista/gazeta per kanał (TERAZ/NASTĘPNIE, jak w
    05-epg-list.png) oraz wyniki wyszukiwania. Rozbudowany pasek
    wyszukiwania (tytuł/opis, kanały, zakres dat) + filtr gatunku, zgodnie
    z oryginalnym projektem.
    """

    def __init__(self, library: TvhLibrary, reminder_store: Optional[ReminderStore] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.library = library
        self.reminder_store = reminder_store
        self._search_source: Optional[int] = None
        self._active_mode = "grid"  # "grid" | "list" - tryb do ktorego wracamy po wyczyszczeniu wyszukiwania

        # --- górny pasek: grid/lista/szukaj + filtr gatunku + odśwież ---
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.set_margin_top(6)
        toolbar.set_margin_bottom(4)
        toolbar.set_margin_start(12)
        toolbar.set_margin_end(12)

        mode_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        mode_box.add_css_class("linked")
        self.grid_mode_btn = Gtk.ToggleButton(icon_name="view-grid-symbolic")
        self.grid_mode_btn.set_tooltip_text("Widok siatki (oś czasu)")
        self.grid_mode_btn.set_active(True)
        self.grid_mode_btn.connect("toggled", self._on_mode_toggled, "grid")
        mode_box.append(self.grid_mode_btn)
        self.list_mode_btn = Gtk.ToggleButton(icon_name="view-list-symbolic")
        self.list_mode_btn.set_tooltip_text("Widok listy (gazeta per kanał)")
        self.list_mode_btn.set_group(self.grid_mode_btn)
        self.list_mode_btn.connect("toggled", self._on_mode_toggled, "list")
        mode_box.append(self.list_mode_btn)
        toolbar.append(mode_box)

        self.search_toggle_btn = Gtk.ToggleButton(icon_name="edit-find-symbolic")
        self.search_toggle_btn.set_tooltip_text("Szukaj audycji")
        self.search_toggle_btn.connect("toggled", self._on_search_toggle)
        toolbar.append(self.search_toggle_btn)

        genre_names = ["Wszystkie gatunki"] + [label for _val, label in all_genres()]
        self._genre_values = [None] + [val for val, _label in all_genres()]
        self.genre_dropdown = Gtk.DropDown(model=Gtk.StringList.new(genre_names))
        self.genre_dropdown.set_tooltip_text("Filtruj wg gatunku")
        self.genre_dropdown.connect("notify::selected", self._on_genre_changed)
        toolbar.append(self.genre_dropdown)

        spacer = Gtk.Box(hexpand=True)
        toolbar.append(spacer)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text("Odśwież widok EPG")
        refresh_btn.connect("clicked", lambda *_: self.reload())
        toolbar.append(refresh_btn)
        self.append(toolbar)

        # --- rozwijany pasek wyszukiwania zaawansowanego ---
        self.search_revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_box.set_margin_start(12)
        search_box.set_margin_end(12)
        search_box.set_margin_bottom(6)

        self.search_entry = Gtk.SearchEntry(placeholder_text="Szukaj w tytule / opisie…")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("search-changed", self._on_search_changed)
        search_box.append(self.search_entry)

        self.channels_entry = Gtk.Entry(placeholder_text="Kanały: TVP1, Polsat…")
        self.channels_entry.set_width_chars(20)
        self.channels_entry.connect("changed", self._on_search_changed)
        search_box.append(self.channels_entry)

        self.date_from_entry = Gtk.Entry(placeholder_text="Od: RRRR-MM-DD")
        self.date_from_entry.set_width_chars(12)
        self.date_from_entry.connect("changed", self._on_search_changed)
        search_box.append(self.date_from_entry)

        self.date_to_entry = Gtk.Entry(placeholder_text="Do: RRRR-MM-DD")
        self.date_to_entry.set_width_chars(12)
        self.date_to_entry.connect("changed", self._on_search_changed)
        search_box.append(self.date_to_entry)

        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        clear_btn.add_css_class("flat")
        clear_btn.set_tooltip_text("Wyczyść wyszukiwanie")
        clear_btn.connect("clicked", self._on_clear_search)
        search_box.append(clear_btn)

        self.search_revealer.set_child(search_box)
        self.append(self.search_revealer)

        hint = Gtk.Label(xalign=0)
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        hint.set_margin_start(12)
        hint.set_margin_bottom(4)
        self.append(hint)
        self._hint_lbl = hint

        self.view_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.view_stack.set_vexpand(True)
        self.append(self.view_stack)

        # --- widok siatki (grid, oś czasu) ---
        self.grid_view = EpgGridView(library, self._on_grid_event_click)
        self.view_stack.add_named(self.grid_view, "grid")

        # --- widok gazetowy/listy (TERAZ/NASTĘPNIE per kanał) ---
        guide_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        self.listbox.set_margin_top(6)
        self.listbox.set_margin_bottom(10)
        self.listbox.set_margin_start(10)
        self.listbox.set_margin_end(10)
        guide_scroller.set_child(self.listbox)
        self.view_stack.add_named(guide_scroller, "list")

        # --- widok wynikow wyszukiwania ---
        search_scroller = Gtk.ScrolledWindow(vexpand=True)
        self.search_listbox = Gtk.ListBox()
        self.search_listbox.add_css_class("boxed-list")
        self.search_listbox.set_margin_top(6)
        self.search_listbox.set_margin_bottom(10)
        self.search_listbox.set_margin_start(10)
        self.search_listbox.set_margin_end(10)
        search_scroller.set_child(self.search_listbox)

        self.search_empty = Adw.StatusPage(
            icon_name="edit-find-symbolic",
            title="Brak wyników",
            description="Nie znaleziono audycji pasujących do zapytania",
        )

        self.search_stack = Gtk.Stack()
        self.search_stack.add_named(search_scroller, "results")
        self.search_stack.add_named(self.search_empty, "empty")
        self.view_stack.add_named(self.search_stack, "search")

        self.view_stack.set_visible_child_name("grid")
        self._set_hint_default()

        library.connect("channels-changed", lambda *_: self.reload())
        library.connect("epg-changed", lambda _lib, ch_id: self._refresh_channel(ch_id))
        library.connect("initial-sync-done", lambda *_: self.reload())
        self.reload()

        # okresowe odświeżenie „TERAZ” (co 60 s)
        GLib.timeout_add_seconds(60, self._tick)

    def _set_hint_default(self) -> None:
        if self._active_mode == "grid":
            self._hint_lbl.set_text("Siatka: przewiń poziomo, ◀ Teraz ▶ zmienia okno czasowe")
        else:
            self._hint_lbl.set_text("Widok listy · data przy programach spoza dziś · 24h")

    def _on_grid_event_click(self, event: EpgEvent, channel_id: int) -> None:
        ch = self.library.channels.get(channel_id)
        ch_name = ch.name if ch else f"Kanał {channel_id}"
        dialog = Adw.Window(transient_for=self.get_root(), modal=True, title=ch_name)
        dialog.set_default_size(420, -1)
        tb = Adw.ToolbarView()
        tb.add_top_bar(Adw.HeaderBar())
        row = EpgEventRow(
            self.library, channel_id, event,
            channel_name=ch_name, reminder_store=self.reminder_store,
        )
        row.set_margin_top(8)
        row.set_margin_bottom(8)
        row.set_margin_start(8)
        row.set_margin_end(8)
        tb.set_content(row)
        dialog.set_content(tb)
        dialog.present()

    def _on_mode_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if not btn.get_active():
            return
        self._active_mode = mode
        if not self.search_toggle_btn.get_active():
            self.view_stack.set_visible_child_name(mode)
            self._set_hint_default()

    def _on_genre_changed(self, *_a) -> None:
        # zmiana gatunku, gdy trwa wyszukiwanie, powtarza zapytanie
        if self.search_toggle_btn.get_active() and self.search_entry.get_text().strip():
            self._on_search_changed(self.search_entry)

    def _on_search_toggle(self, btn: Gtk.ToggleButton) -> None:
        self.search_revealer.set_reveal_child(btn.get_active())
        if btn.get_active():
            self.search_entry.grab_focus()
        else:
            self.view_stack.set_visible_child_name(self._active_mode)
            self._set_hint_default()

    def _on_clear_search(self, *_a) -> None:
        self.search_entry.set_text("")
        self.channels_entry.set_text("")
        self.date_from_entry.set_text("")
        self.date_to_entry.set_text("")
        self.genre_dropdown.set_selected(0)
        self.search_toggle_btn.set_active(False)

    def _tick(self) -> bool:
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if isinstance(row, EpgChannelRow):
                row.refresh()
            i += 1
        return True

    def reload(self) -> None:
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        for ch in self.library.tv_channels() + self.library.radio_channels():
            self.listbox.append(
                EpgChannelRow(self.library, ch.channel_id, ch.name, reminder_store=self.reminder_store)
            )
        self.grid_view.reload()

    def _refresh_channel(self, channel_id: int) -> None:
        i = 0
        while (row := self.listbox.get_row_at_index(i)) is not None:
            if isinstance(row, EpgChannelRow) and row.channel_id == channel_id:
                row.refresh()
                break
            i += 1

    # ------------------------------------------------------------------ #
    # Wyszukiwanie audycji (epgQuery na serwerze) - tytuł/opis + kanały +
    # zakres dat + gatunek
    # ------------------------------------------------------------------ #
    def _on_search_changed(self, *_a) -> None:
        if self._search_source is not None:
            GLib.source_remove(self._search_source)
            self._search_source = None
        text = self.search_entry.get_text().strip()
        channels_text = self.channels_entry.get_text().strip()
        date_from = self.date_from_entry.get_text().strip()
        date_to = self.date_to_entry.get_text().strip()
        if not text and not channels_text and not date_from and not date_to:
            self.view_stack.set_visible_child_name(self._active_mode)
            self._set_hint_default()
            return
        # debounce 400ms - nie odpalaj epgQuery na kazde nacisniecie klawisza
        self._search_source = GLib.timeout_add(400, self._run_search)

    def _resolve_channel_filter(self, channels_text: str) -> Optional[int]:
        """epgQuery filtruje po JEDNYM channelId - gdy user wpisał
        rozpoznawalną nazwę pasującą do dokładnie jednego kanału, filtrujemy
        po nim server-side; przy wielu/niejednoznacznych nazwach filtrujemy
        po stronie klienta po otrzymaniu wyników (patrz _run_search)."""
        if not channels_text:
            return None
        names = [n.strip().lower() for n in channels_text.split(",") if n.strip()]
        if len(names) != 1:
            return None
        matches = [
            ch for ch in (self.library.tv_channels() + self.library.radio_channels())
            if names[0] in ch.name.lower()
        ]
        if len(matches) == 1:
            return matches[0].channel_id
        return None

    def _run_search(self) -> bool:
        self._search_source = None
        text = self.search_entry.get_text().strip()
        channels_text = self.channels_entry.get_text().strip()
        date_from_text = self.date_from_entry.get_text().strip()
        date_to_text = self.date_to_entry.get_text().strip()
        genre_idx = self.genre_dropdown.get_selected()
        content_type = self._genre_values[genre_idx] if 0 <= genre_idx < len(self._genre_values) else None

        self._hint_lbl.set_text("Szukam…")

        channel_id = self._resolve_channel_filter(channels_text)
        channel_names_filter = None
        if channels_text and channel_id is None:
            channel_names_filter = [n.strip().lower() for n in channels_text.split(",") if n.strip()]

        min_start = max_start = None
        try:
            if date_from_text:
                min_start = int(datetime.strptime(date_from_text, "%Y-%m-%d").timestamp())
            if date_to_text:
                max_start = int((datetime.strptime(date_to_text, "%Y-%m-%d") + timedelta(days=1)).timestamp())
        except ValueError:
            self._hint_lbl.set_text("Nieprawidłowy format daty (oczekiwano RRRR-MM-DD)")

        query_text = text or "."  # epgQuery wymaga niepustego query - "." jako "dowolny tytuł" gdy filtrujemy tylko po kanale/dacie/gatunku

        def _ok(events: list) -> None:
            if (
                self.search_entry.get_text().strip() != text
                or self.channels_entry.get_text().strip() != channels_text
                or self.date_from_entry.get_text().strip() != date_from_text
                or self.date_to_entry.get_text().strip() != date_to_text
            ):
                return  # zapytanie się zmieniło zanim odpowiedź wróciła
            filtered = events
            if channel_names_filter:
                by_id = {ch.channel_id: ch.name.lower() for ch in
                         (self.library.tv_channels() + self.library.radio_channels())}
                filtered = [
                    ev for ev in filtered
                    if any(n in by_id.get(ev.channel_id, "") for n in channel_names_filter)
                ]
            if min_start is not None:
                filtered = [ev for ev in filtered if ev.start >= min_start]
            if max_start is not None:
                filtered = [ev for ev in filtered if ev.start < max_start]
            self._show_search_results(filtered)

        def _err(exc: Exception) -> None:
            self._hint_lbl.set_text(f"Błąd wyszukiwania: {exc}")
            self.search_stack.set_visible_child_name("empty")
            self.view_stack.set_visible_child_name("search")

        self.library.search_epg(
            query_text,
            _ok,
            on_err=_err,
            channel_id=channel_id,
            content_type=content_type,
            limit=150,
        )
        return False

    def _show_search_results(self, events: list) -> None:
        while (row := self.search_listbox.get_row_at_index(0)) is not None:
            self.search_listbox.remove(row)

        channels_by_id = {
            ch.channel_id: ch.name
            for ch in self.library.tv_channels() + self.library.radio_channels()
        }

        if not events:
            self._hint_lbl.set_text("Brak wyników")
            self.search_stack.set_visible_child_name("empty")
            self.view_stack.set_visible_child_name("search")
            return

        for ev in events:
            ch_name = channels_by_id.get(ev.channel_id, f"Kanał {ev.channel_id}")
            row = Gtk.ListBoxRow()
            row.set_child(
                EpgEventRow(
                    self.library,
                    ev.channel_id,
                    ev,
                    tag=_day_label(ev.start, int(time.time())),
                    channel_name=ch_name,
                    reminder_store=self.reminder_store,
                    show_channel_name=True,
                )
            )
            self.search_listbox.append(row)

        self._hint_lbl.set_text(f"Wyniki: {len(events)}")
        self.search_stack.set_visible_child_name("results")
        self.view_stack.set_visible_child_name("search")
