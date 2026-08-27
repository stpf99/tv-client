from __future__ import annotations

import os
import time
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, GObject, Pango  # noqa: E402

from tvh.library import TvhLibrary
from tvh.models import Channel, EpgEvent
from player.stream_controller import StreamController, SUBTITLE_AUTO
from ui.icon_cache import make_icon_widget, update_icon_widget

# OSD znika po 5 s bez ruchu myszy / kliknięcia
OSD_HIDE_DELAY_MS = 5000
# Odświeżanie paska postępu audycji
PROGRESS_TICK_MS = 2000


def _font_size_attrs(pt: int, bold: bool = False) -> Pango.AttrList:
    """Buduje Pango.AttrList wymuszajaca konkretny rozmiar (i opcjonalnie
    pogrubienie) niezaleznie od klasy CSS libadwaita (title-2 itp. maja
    stale, male rozmiary nieodpowiednie dla OSD ogladanego z odleglosci)."""
    attrs = Pango.AttrList()
    attrs.insert(Pango.attr_size_new(int(pt * Pango.SCALE)))
    if bold:
        attrs.insert(Pango.attr_weight_new(Pango.Weight.BOLD))
    return attrs


class LiveView(Gtk.Box):
    """
    Panel odtwarzania: obszar wideo z nakładką OSD (nie wpływa na layout
    wideo – Gtk.Overlay). Po zmianie kanału / ruchu myszy pokazuje nazwę
    i opis audycji, czas trwania oraz pasek pozycji; po 5 s znika.
    """

    def __init__(
        self,
        library: TvhLibrary,
        stream_ctrl: StreamController,
        window: Adw.ApplicationWindow,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.library = library
        self.stream_ctrl = stream_ctrl
        self.window = window
        self._osd_hide_source: Optional[int] = None
        self._last_motion_xy: Optional[tuple] = None
        self._clock_source: Optional[int] = None
        self._progress_source: Optional[int] = None
        self._current_event: Optional[EpgEvent] = None

        self.overlay = Gtk.Overlay(vexpand=True, hexpand=True)
        self.overlay.add_css_class("tvh-video-area")

        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)

        # Gtk.GraphicsOffload (GTK >=4.14): compositor Wayland skanuje klatkę
        # wideo bezpośrednio z DMABuf (gtk4paintablesink → GdkDmabufTexture),
        # z pominięciem GSK. Działa gdy sink dostarcza DMABuf/GL – czyli przy
        # zero-copy (gtk4paintablesink / glsinkbin). Przy software/videoconvert
        # Offload po cichu wraca do zwykłej kompozycji.
        offload_cls = getattr(Gtk, "GraphicsOffload", None)
        wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or (
            os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        )
        if offload_cls is not None and wayland:
            self.video_widget = offload_cls()
            self.video_widget.set_child(self.picture)
            try:
                self.video_widget.set_enabled(True)
            except Exception:
                pass
            try:
                self.video_widget.set_black_background(True)
            except Exception:
                pass
        else:
            self.video_widget = self.picture

        self.placeholder = Adw.StatusPage(
            icon_name="tv-symbolic",
            title="Brak odtwarzania",
            description="Wybierz kanał z listy po lewej stronie",
        )
        self.placeholder.add_css_class("tvh-placeholder")

        self.video_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.video_stack.add_named(self.placeholder, "placeholder")
        self.video_stack.add_named(self.video_widget, "video")
        self.video_stack.set_visible_child_name("placeholder")
        self.overlay.set_child(self.video_stack)

        # Nakładka buforowania – pokazywana zarówno przy pierwszym starcie
        # kanału, jak i przy re-bufferingu w trakcie odtwarzania (watchdog
        # w GstPlayer wykrywa ciszę w danych HTSP i wraca tutaj).
        self.buffering_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        self.buffering_box.add_css_class("tvh-buffering")
        self._buffering_spinner = Gtk.Spinner()
        self._buffering_spinner.set_size_request(32, 32)
        buffering_lbl = Gtk.Label(label="Buforowanie…")
        buffering_lbl.add_css_class("dim-label")
        self.buffering_box.append(self._buffering_spinner)
        self.buffering_box.append(buffering_lbl)
        self.buffering_box.set_visible(False)
        self.overlay.add_overlay(self.buffering_box)

        self._build_osd()

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.overlay.add_controller(motion)

        click = Gtk.GestureClick()
        click.connect("released", self._on_click)
        self.overlay.add_controller(click)

        self.append(self.overlay)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self.channel_list_revealer: Optional[Gtk.Revealer] = None

        self.stream_ctrl.player.connect("paintable-ready", self._on_paintable_ready)
        self.stream_ctrl.player.connect("state-changed", self._on_player_state)
        self.stream_ctrl.player.connect("error", self._on_player_error)
        self.stream_ctrl.player.connect("decoder-chosen", self._on_decoder_chosen)
        self.stream_ctrl.player.connect("stream-info-changed", self._on_stream_info_changed)
        self.stream_ctrl.connect("tracks-changed", self._on_tracks_changed)
        self.stream_ctrl.connect("tracks-changed", self._on_stream_info_changed)

        library.connect("epg-changed", self._on_epg_changed)
        library.connect("recordings-changed", self._on_stream_info_changed)

        self._desc_scroll_source: Optional[int] = None
        self._desc_scroll_adj: Optional[Gtk.Adjustment] = None
        self.apply_osd_prefs()

    # ------------------------------------------------------------------ #
    def _build_osd(self) -> None:
        # --- Górna belka: kanał + zegar --------------------------------
        self.top_osd = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.top_osd.add_css_class("tvh-osd-top")
        self.top_osd.set_valign(Gtk.Align.START)
        self.top_osd.set_halign(Gtk.Align.FILL)
        self.top_osd.set_hexpand(True)

        self.list_btn = self._osd_button("view-list-symbolic", self._on_toggle_channel_list)
        self.list_btn.set_tooltip_text("Lista kanałów")

        self.osd_channel_icon = make_icon_widget("tv-symbolic", 48, None)
        self.osd_channel_icon.set_valign(Gtk.Align.CENTER)

        self.channel_lbl = Gtk.Label(xalign=0, hexpand=True)
        self.channel_lbl.add_css_class("title-2")
        self.channel_lbl.set_ellipsize(Pango.EllipsizeMode.END)

        self.decoder_lbl = Gtk.Label(xalign=0)
        self.decoder_lbl.add_css_class("caption")
        self.decoder_lbl.add_css_class("dim-label")
        self.decoder_lbl.set_visible(False)

        # Pasek ikon/tekstu: kodek wideo/audio, status PVR. Siła
        # sygnału/health tunera DVB celowo pominięte - wymagałyby
        # równoległej subskrypcji HTSP obok streamu HTTP (podwójny
        # transfer u serwera) - do decyzji, patrz notatka w README/PR.
        self.stream_info_lbl = Gtk.Label(xalign=0)
        self.stream_info_lbl.add_css_class("caption")
        self.stream_info_lbl.add_css_class("dim-label")
        self.stream_info_lbl.set_visible(False)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.append(self.channel_lbl)
        info_box.append(self.decoder_lbl)
        info_box.append(self.stream_info_lbl)

        self.clock_lbl = Gtk.Label()
        self.clock_lbl.add_css_class("title-4")

        self.top_osd.append(self.list_btn)
        self.top_osd.append(self.osd_channel_icon)
        self.top_osd.append(info_box)
        self.top_osd.append(self.clock_lbl)
        self.top_osd.set_margin_top(12)
        self.top_osd.set_margin_start(16)
        self.top_osd.set_margin_end(16)

        # --- Dolna belka: audycja + pasek postępu + kontrolki -----------
        self.bottom_osd = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.bottom_osd.add_css_class("tvh-osd-bottom")
        self.bottom_osd.set_valign(Gtk.Align.END)
        self.bottom_osd.set_halign(Gtk.Align.FILL)
        self.bottom_osd.set_hexpand(True)
        self.bottom_osd.set_margin_bottom(12)
        self.bottom_osd.set_margin_start(16)
        self.bottom_osd.set_margin_end(16)

        # Blok informacji o audycji
        self.program_title_lbl = Gtk.Label(xalign=0)
        self.program_title_lbl.add_css_class("title-3")
        self.program_title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.program_title_lbl.set_wrap(False)

        self.program_desc_lbl = Gtk.Label(xalign=0, yalign=0)
        self.program_desc_lbl.add_css_class("caption")
        self.program_desc_lbl.add_css_class("dim-label")
        self.program_desc_lbl.set_wrap(True)
        self.program_desc_lbl.set_max_width_chars(80)

        # Owinięte w ScrolledWindow o ustalonej wysokości - gdy opis nie
        # mieści się, i autoscroll jest włączony w preferencjach, animujemy
        # Gtk.Adjustment (góra→dół lub dół→góra). Gdy autoscroll wyłączony,
        # zachowuje się jak zwykły label z ellipsize/limitem linii.
        self.program_desc_scroller = Gtk.ScrolledWindow()
        self.program_desc_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
        self.program_desc_scroller.set_child(self.program_desc_lbl)
        self.program_desc_scroller.set_propagate_natural_height(False)

        prog_info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        prog_info.append(self.program_title_lbl)
        prog_info.append(self.program_desc_scroller)

        # Czas + pasek postępu audycji (EPG: start → stop)
        self.time_start_lbl = Gtk.Label(label="--:--")
        self.time_start_lbl.add_css_class("caption")
        self.time_end_lbl = Gtk.Label(label="--:--")
        self.time_end_lbl.add_css_class("caption")
        self.time_remain_lbl = Gtk.Label(label="")
        self.time_remain_lbl.add_css_class("caption")
        self.time_remain_lbl.add_css_class("dim-label")

        self.progress = Gtk.ProgressBar()
        self.progress.set_hexpand(True)
        self.progress.set_valign(Gtk.Align.CENTER)
        self.progress.set_fraction(0.0)
        self.progress.add_css_class("tvh-osd-progress")
        self.progress.set_show_text(False)

        progress_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        progress_row.append(self.time_start_lbl)
        progress_row.append(self.progress)
        progress_row.append(self.time_end_lbl)
        progress_row.append(self.time_remain_lbl)

        # Kontrolki odtwarzania
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.play_btn = self._osd_button("media-playback-pause-symbolic", self._on_play_pause)
        self.stop_btn = self._osd_button("media-playback-stop-symbolic", self._on_stop)
        self.mute_btn = self._osd_button("audio-volume-high-symbolic", self._on_mute)

        self.volume_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.volume_scale.set_value(100)
        self.volume_scale.set_size_request(120, -1)
        self.volume_scale.set_draw_value(False)
        self.volume_scale.connect("value-changed", self._on_volume_changed)

        self.audio_btn = self._osd_button("audio-speakers-symbolic", self._on_audio_menu)
        self.audio_btn.set_tooltip_text("Ścieżka audio")
        self.sub_btn = self._osd_button("media-view-subtitles-symbolic", self._on_sub_menu)
        self.sub_btn.set_tooltip_text("Napisy")
        self.prefs_btn = self._osd_button("preferences-system-symbolic", self._on_prefs)
        self.prefs_btn.set_tooltip_text("Preferencje odtwarzacza")

        self.record_btn = self._osd_button("media-record-symbolic", self._on_record)
        self.fullscreen_btn = self._osd_button(
            "view-fullscreen-symbolic", self._on_fullscreen_toggle
        )

        spacer = Gtk.Box(hexpand=True)

        for w in (self.play_btn, self.stop_btn, self.mute_btn, self.volume_scale,
                  self.audio_btn, self.sub_btn):
            controls.append(w)
        controls.append(spacer)
        for w in (self.prefs_btn, self.record_btn, self.fullscreen_btn):
            controls.append(w)

        self.bottom_osd.append(prog_info)
        self.bottom_osd.append(progress_row)
        self.bottom_osd.append(controls)

        # Overlay – nie zajmuje miejsca w layoucie wideo
        self.overlay.add_overlay(self.top_osd)
        self.overlay.add_overlay(self.bottom_osd)

        # Na starcie ukryte (brak odtwarzania)
        self.top_osd.set_opacity(0)
        self.bottom_osd.set_opacity(0)
        self.top_osd.set_sensitive(False)
        self.bottom_osd.set_sensitive(False)

        self._start_clock()

    def _osd_button(self, icon_name: str, handler) -> Gtk.Button:
        btn = Gtk.Button()
        btn.set_child(Gtk.Image.new_from_icon_name(icon_name))
        btn.add_css_class("circular")
        btn.add_css_class("osd")
        btn.connect("clicked", handler)
        return btn

    def _start_clock(self) -> None:
        def _tick():
            self.clock_lbl.set_text(time.strftime("%H:%M"))
            return True

        _tick()
        self._clock_source = GLib.timeout_add_seconds(15, _tick)

    # ------------------------------------------------------------------ #
    def set_channel_list(self, channel_list: Gtk.Widget) -> None:
        """Lista kanałów jako wysuwana nakładka NAD obszarem wideo."""
        channel_list.add_css_class("tvh-channel-overlay")
        channel_list.set_size_request(320, -1)
        channel_list.set_halign(Gtk.Align.FILL)
        channel_list.set_valign(Gtk.Align.FILL)
        channel_list.set_vexpand(True)

        revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.SLIDE_RIGHT,
            transition_duration=200,
        )
        revealer.set_halign(Gtk.Align.START)
        revealer.set_valign(Gtk.Align.FILL)
        revealer.set_vexpand(True)
        revealer.set_child(channel_list)
        revealer.set_reveal_child(True)

        self.overlay.add_overlay(revealer)
        self.channel_list_revealer = revealer

    def _on_toggle_channel_list(self, _btn) -> None:
        if self.channel_list_revealer is None:
            return
        shown = self.channel_list_revealer.get_reveal_child()
        self.channel_list_revealer.set_reveal_child(not shown)
        self._show_osd_temporarily()

    def _on_decoder_chosen(self, _player, element_name: str, kind: str) -> None:
        if kind == "vaapi":
            label = f"Dekodowanie: VA-API · {element_name} (zero-copy)"
        else:
            label = f"Dekodowanie: programowe ({element_name})"
        self.decoder_lbl.set_text(label)
        self.decoder_lbl.set_visible(True)

    def _on_stream_info_changed(self, *_a) -> None:
        parts: list[str] = []

        vinfo = getattr(self.stream_ctrl.player, "_pb_video_info", {}) or {}
        vcodec = vinfo.get("codec")
        bitrate = vinfo.get("bitrate")
        if vcodec:
            parts.append(vcodec)
        if bitrate:
            parts.append(f"{int(bitrate) // 1000} kb/s")

        cur_a = self.stream_ctrl.get_current_audio_index()
        if cur_a is not None:
            for t in self.stream_ctrl.get_audio_tracks():
                if t.get("index") == cur_a and t.get("codec"):
                    parts.append(t["codec"])
                    break

        ch = self.stream_ctrl.current_channel
        if ch is not None:
            rec = next(
                (
                    r
                    for r in self.library.recordings.values()
                    if r.channel_id == ch.channel_id and r.state == "recording"
                ),
                None,
            )
            if rec is not None:
                parts.append("● PVR: nagrywanie")

        if parts:
            self.stream_info_lbl.set_text(" · ".join(parts))
            self.stream_info_lbl.set_visible(True)
        else:
            self.stream_info_lbl.set_visible(False)

    def play_channel(self, channel: Channel) -> None:
        self.decoder_lbl.set_visible(False)
        self.stream_info_lbl.set_visible(False)
        self.stream_ctrl.play_channel(channel)
        self.channel_lbl.set_text(f"{channel.number or ''} {channel.name}".strip())
        update_icon_widget(self.osd_channel_icon, self.library.resolve_icon_url(channel.icon_url))
        self._update_program_info(channel)
        self.video_stack.set_visible_child_name("video")
        self.play_btn.set_child(
            Gtk.Image.new_from_icon_name("media-playback-pause-symbolic")
        )
        if self.channel_list_revealer is not None:
            self.channel_list_revealer.set_reveal_child(False)
        self._show_osd_temporarily()
        self._ensure_progress_timer()

    def _update_program_info(self, channel: Optional[Channel] = None) -> None:
        ch = channel or self.stream_ctrl.current_channel
        if not ch:
            self._clear_program_info()
            return
        ev = self.library.current_event_for_channel(ch.channel_id, int(time.time()))
        self._current_event = ev
        if not ev:
            self.program_title_lbl.set_text("Brak danych EPG")
            self.program_desc_lbl.set_text("")
            self.program_desc_scroller.set_visible(False)
            self.time_start_lbl.set_text("--:--")
            self.time_end_lbl.set_text("--:--")
            self.time_remain_lbl.set_text("")
            self.progress.set_fraction(0.0)
            self._stop_desc_scroll()
            return

        title = ev.title or "Bez tytułu"
        if ev.subtitle:
            title = f"{title} — {ev.subtitle}"
        self.program_title_lbl.set_text(title)

        desc = (ev.description or "").strip()
        if desc:
            self.program_desc_lbl.set_text(desc)
            self.program_desc_lbl.set_tooltip_text(desc)
            self.program_desc_scroller.set_visible(True)
            self._restart_desc_scroll()
        else:
            self.program_desc_lbl.set_text("")
            self.program_desc_scroller.set_visible(False)
            self._stop_desc_scroll()

        self.time_start_lbl.set_text(time.strftime("%H:%M", time.localtime(ev.start)))
        self.time_end_lbl.set_text(time.strftime("%H:%M", time.localtime(ev.stop)))
        self._refresh_progress()

    def _clear_program_info(self) -> None:
        self._current_event = None
        self.program_title_lbl.set_text("")
        self.program_desc_lbl.set_text("")
        self.program_desc_scroller.set_visible(False)
        self._stop_desc_scroll()
        self.time_start_lbl.set_text("--:--")
        self.time_end_lbl.set_text("--:--")
        self.time_remain_lbl.set_text("")
        self.progress.set_fraction(0.0)

    # ------------------------------------------------------------------ #
    # Preferencje OSD: rozmiar czcionki gornego paska, rozmiar czcionki
    # opisu audycji, automatyczne przewijanie dlugiego opisu i kierunek.
    # ------------------------------------------------------------------ #
    def apply_osd_prefs(self) -> None:
        from tvh.config import load_player_prefs
        prefs = load_player_prefs()
        self._osd_prefs = prefs

        top_pt = getattr(prefs, "osd_top_bar_font_pt", 20) or 20
        self.channel_lbl.set_attributes(_font_size_attrs(top_pt, bold=True))
        self.clock_lbl.set_attributes(_font_size_attrs(top_pt))

        desc_pt = getattr(prefs, "osd_subtitle_font_pt", 16) or 16
        self.program_desc_lbl.set_attributes(_font_size_attrs(desc_pt))

        # Wysokosc widocznego okna opisu ~ 3 linie przy biezacym rozmiarze
        # czcionki, zeby autoscroll mial co przewijac, a bez autoscrolla
        # tekst po prostu przycina sie do tej wysokosci.
        self.program_desc_scroller.set_min_content_height(int(desc_pt * 1.4 * 3))
        self.program_desc_scroller.set_max_content_height(int(desc_pt * 1.4 * 3))

        if self._current_event is not None:
            self._restart_desc_scroll()

    def _stop_desc_scroll(self) -> None:
        if self._desc_scroll_source is not None:
            GLib.source_remove(self._desc_scroll_source)
            self._desc_scroll_source = None
        self._desc_scroll_adj = None

    def _restart_desc_scroll(self) -> None:
        self._stop_desc_scroll()
        prefs = getattr(self, "_osd_prefs", None)
        if prefs is None or not getattr(prefs, "osd_desc_autoscroll", False):
            return
        direction = getattr(prefs, "osd_desc_scroll_direction", "down")
        adj = self.program_desc_scroller.get_vadjustment()
        self._desc_scroll_adj = adj

        # Czekamy jedna klatke, zeby label zdazyl przeliczyc swoja
        # naturalna wysokosc po set_text() - inaczej get_upper() bywa 0.
        def _prime() -> bool:
            upper = adj.get_upper() - adj.get_page_size()
            if upper <= 1:
                # Tekst miesci sie w calosci - nic do przewijania.
                return False
            adj.set_value(upper if direction == "up" else 0)
            self._desc_scroll_source = GLib.timeout_add(60, self._tick_desc_scroll, direction)
            return False

        GLib.idle_add(_prime)

    def _tick_desc_scroll(self, direction: str) -> bool:
        adj = self._desc_scroll_adj
        if adj is None:
            self._desc_scroll_source = None
            return False
        upper = adj.get_upper() - adj.get_page_size()
        if upper <= 1:
            self._desc_scroll_source = None
            return False
        step = upper / 240.0  # pelny przejazd w ok. 240 * 60ms = 14.4s
        value = adj.get_value()
        if direction == "up":
            value -= step
            if value <= 0:
                value = upper  # zawin z powrotem na dol (gora -> dol -> gora)
        else:
            value += step
            if value >= upper:
                value = 0  # zawin z powrotem na gore (dol -> gora -> dol)
        adj.set_value(value)
        return True

    def _refresh_progress(self) -> None:
        ev = self._current_event
        if not ev or ev.stop <= ev.start:
            self.progress.set_fraction(0.0)
            self.time_remain_lbl.set_text("")
            return
        now = time.time()
        duration = float(ev.stop - ev.start)
        elapsed = max(0.0, min(duration, now - ev.start))
        frac = elapsed / duration
        self.progress.set_fraction(frac)

        remain = max(0, int(ev.stop - now))
        if remain >= 3600:
            self.time_remain_lbl.set_text(
                f"−{remain // 3600}:{(remain % 3600) // 60:02d}:{remain % 60:02d}"
            )
        else:
            self.time_remain_lbl.set_text(f"−{remain // 60}:{remain % 60:02d}")

        # Koniec audycji → odśwież EPG (tylko jesli biblioteka faktycznie
        # zwrocila NOWE wydarzenie - w przeciwnym razie petla
        # _update_program_info -> _refresh_progress -> _update_program_info
        # odpalalaby sie w nieskonczonosc, gdy EPG jeszcze nie zdazyl
        # zaktualizowac danych po zakonczeniu audycji).
        if now >= ev.stop:
            ch = self.stream_ctrl.current_channel
            if ch:
                next_ev = self.library.current_event_for_channel(ch.channel_id, int(now))
                if next_ev is not None and next_ev is not ev:
                    self._update_program_info(ch)

    def _ensure_progress_timer(self) -> None:
        if self._progress_source is not None:
            return
        self._progress_source = GLib.timeout_add(PROGRESS_TICK_MS, self._on_progress_tick)

    def _stop_progress_timer(self) -> None:
        if self._progress_source is not None:
            GLib.source_remove(self._progress_source)
            self._progress_source = None

    def _on_progress_tick(self) -> bool:
        if self.video_stack.get_visible_child_name() != "video":
            self._progress_source = None
            return False
        self._refresh_progress()
        return True

    def _on_epg_changed(self, _lib, channel_id: int) -> None:
        ch = self.stream_ctrl.current_channel
        if ch and ch.channel_id == channel_id:
            self._update_program_info(ch)

    def _on_paintable_ready(self, _player, paintable) -> None:
        if paintable is None:
            self.picture.set_paintable(None)
            return
        self.picture.set_paintable(paintable)
        # Wymuś widok wideo (placeholder "Brak odtwarzania" znika)
        self.video_stack.set_visible_child_name("video")
        self.picture.queue_draw()
        if self.video_widget is not self.picture:
            try:
                self.video_widget.queue_draw()
            except Exception:
                pass

    def _on_player_state(self, _player, state: str) -> None:
        if state == "buffering":
            self._buffering_spinner.start()
            self.buffering_box.set_visible(True)
        elif state == "playing":
            self._buffering_spinner.stop()
            self.buffering_box.set_visible(False)
        if state == "stopped":
            self._buffering_spinner.stop()
            self.buffering_box.set_visible(False)
            # Odepnij natychmiast stara klatke/paintable od widgetu - inaczej
            # GtkPicture trzyma referencje do bufora poprzedniego dekodera
            # (DMABuf/VA-surface) az do nastepnego "paintable-ready", co przy
            # szybkim przelaczaniu kanalow (zwlaszcza zmiana kodeka wideo,
            # np. H264 -> HEVC) potrafi kolidowac z nowym kontekstem VA-API
            # na tym samym GPU.
            self.picture.set_paintable(None)
            self.video_stack.set_visible_child_name("placeholder")
            self._stop_progress_timer()
            self._hide_osd()

    def _on_player_error(self, _player, message: str) -> None:
        self.program_title_lbl.set_text(f"Błąd odtwarzania: {message}")
        self.program_desc_lbl.set_visible(False)
        self._show_osd_temporarily()

    # ------------------------------------------------------------------ #
    def _on_play_pause(self, _btn) -> None:
        player = self.stream_ctrl.player
        if not player.pipeline:
            return
        state = player.pipeline.get_state(0)[1]
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if state == Gst.State.PLAYING:
            player.pause()
            self.play_btn.set_child(
                Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
            )
        else:
            player.play()
            self.play_btn.set_child(
                Gtk.Image.new_from_icon_name("media-playback-pause-symbolic")
            )
        self._show_osd_temporarily()

    def _on_stop(self, _btn) -> None:
        self.stream_ctrl.stop()
        self.video_stack.set_visible_child_name("placeholder")
        self.channel_lbl.set_text("")
        update_icon_widget(self.osd_channel_icon, None)
        self._clear_program_info()
        self._stop_progress_timer()
        self._hide_osd()

    def _on_mute(self, _btn) -> None:
        player = self.stream_ctrl.player
        muted = not player._muted
        player.set_mute(muted)
        icon = "audio-volume-muted-symbolic" if muted else "audio-volume-high-symbolic"
        self.mute_btn.set_child(Gtk.Image.new_from_icon_name(icon))
        self._show_osd_temporarily()

    def _on_volume_changed(self, scale: Gtk.Scale) -> None:
        self.stream_ctrl.player.set_volume(scale.get_value() / 100.0)

    def _on_record(self, _btn) -> None:
        ch = self.stream_ctrl.current_channel
        if not ch:
            return
        ev = self.library.current_event_for_channel(ch.channel_id, int(time.time()))
        if ev:
            self.library.record_event(ch.channel_id, ev.event_id)
        self._show_osd_temporarily()

    def _on_fullscreen_toggle(self, _btn) -> None:
        if self.window.is_fullscreen():
            self.window.unfullscreen()
        else:
            self.window.fullscreen()
        self._show_osd_temporarily()

    # ------------------------------------------------------------------ #
    # OSD: półprzezroczysta nakładka, auto-hide 5 s
    # ------------------------------------------------------------------ #
    def _show_osd_temporarily(self) -> None:
        self.top_osd.set_opacity(1.0)
        self.bottom_osd.set_opacity(1.0)
        self.top_osd.set_sensitive(True)
        self.bottom_osd.set_sensitive(True)
        # Odśwież info przy każdym pokazaniu
        if self.stream_ctrl.current_channel:
            self._update_program_info()
        if self._osd_hide_source:
            GLib.source_remove(self._osd_hide_source)
        self._osd_hide_source = GLib.timeout_add(OSD_HIDE_DELAY_MS, self._hide_osd)

    def _hide_osd(self) -> bool:
        if self.video_stack.get_visible_child_name() == "video":
            self.top_osd.set_opacity(0)
            self.bottom_osd.set_opacity(0)
            self.top_osd.set_sensitive(False)
            self.bottom_osd.set_sensitive(False)
        self._osd_hide_source = None
        return False

    def _on_motion(self, _ctrl, x: float, y: float) -> None:
        # Bug od poczatku projektu: EventControllerMotion jest podpiety do
        # self.overlay, ktory zawiera tez top_osd/bottom_osd. _hide_osd()
        # zmienia im set_sensitive/opacity pod kursorem, a GTK4 przy takiej
        # zmianie potrafi przeliczyc "pick" pod wskaznikiem i wygenerowac
        # syntetyczne zdarzenie motion w TYCH SAMYCH wspolrzednych (bez
        # realnego ruchu myszy). To odpalalo _show_osd_temporarily() od razu
        # po kazdym _hide_osd(), wiec przy nieruchomej myszy OSD nigdy
        # faktycznie nie znikal (petla hide -> synthetic motion -> show ->
        # po 5s znow hide -> ...). Odrzucamy zdarzenia o tych samych (lub
        # prawie tych samych) wspolrzednych co poprzednie - prawdziwy ruch
        # myszy zawsze zmienia x/y.
        last = self._last_motion_xy
        self._last_motion_xy = (x, y)
        if last is not None and abs(x - last[0]) < 0.5 and abs(y - last[1]) < 0.5:
            return
        self._show_osd_temporarily()

    def _on_click(self, _gesture, _n_press, _x, _y) -> None:
        self._show_osd_temporarily()

    def _on_tracks_changed(self, _ctrl) -> None:
        audio_tracks = self.stream_ctrl.get_audio_tracks()
        sub_tracks = self.stream_ctrl.get_subtitle_tracks()
        self.audio_btn.set_sensitive(len(audio_tracks) > 1)
        self.sub_btn.set_sensitive(len(sub_tracks) > 0)
        # Zaktualizuj tooltip aktualnej ścieżki
        cur_a = self.stream_ctrl.get_current_audio_index()
        for t in audio_tracks:
            if t.get("index") == cur_a:
                self.audio_btn.set_tooltip_text(f"Audio: {t.get('description')}")
                break
        cur_s = self.stream_ctrl.get_current_subtitle_index()
        if cur_s is None:
            self.sub_btn.set_tooltip_text("Napisy: wyłączone")
        else:
            for t in sub_tracks:
                if t.get("index") == cur_s:
                    self.sub_btn.set_tooltip_text(f"Napisy: {t.get('description')}")
                    break

    def _on_audio_menu(self, btn) -> None:
        tracks = self.stream_ctrl.get_audio_tracks()
        if not tracks:
            return
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        cur = self.stream_ctrl.get_current_audio_index()
        for t in tracks:
            label = t.get("description") or f"#{t.get('index')}"
            row = Gtk.CheckButton(label=label)
            row.set_active(t.get("index") == cur)
            row.connect("toggled", self._on_audio_track_toggled, t.get("index"), popover)
            box.append(row)
        popover.set_child(box)
        popover.set_parent(btn)
        popover.popup()
        self._show_osd_temporarily()

    def _on_audio_track_toggled(self, check: Gtk.CheckButton, index: int, popover: Gtk.Popover) -> None:
        if not check.get_active():
            return
        self.stream_ctrl.select_audio_track(index)
        popover.popdown()
        self._show_osd_temporarily()

    def _on_sub_menu(self, btn) -> None:
        tracks = self.stream_ctrl.get_subtitle_tracks()
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        cur = self.stream_ctrl.get_current_subtitle_index()
        # Opcja wyłączenia
        off = Gtk.CheckButton(label="Wyłączone")
        off.set_active(cur is None)
        off.connect("toggled", self._on_sub_track_toggled, None, popover)
        box.append(off)
        if not tracks:
            # Napisy sa globalnie wylaczone w configu -> playbin ma
            # wylaczona flage TEXT i nie demuksuje/dekoduje sciezki, wiec
            # lista jest pusta dopoki nie zrestartujemy z wlaczonymi
            # napisami. Dajemy wpis "auto", zeby dalo sie w ogole wrocic
            # do wlaczonego stanu bez znajomosci konkretnych indeksow.
            auto = Gtk.CheckButton(label="Włączone (auto)")
            auto.set_active(False)
            auto.connect("toggled", self._on_sub_track_toggled, SUBTITLE_AUTO, popover)
            box.append(auto)
        for t in tracks:
            label = t.get("description") or f"#{t.get('index')}"
            row = Gtk.CheckButton(label=label)
            row.set_active(t.get("index") == cur)
            row.connect("toggled", self._on_sub_track_toggled, t.get("index"), popover)
            box.append(row)
        popover.set_child(box)
        popover.set_parent(btn)
        popover.popup()
        self._show_osd_temporarily()

    def _on_sub_track_toggled(self, check: Gtk.CheckButton, index, popover: Gtk.Popover) -> None:
        if not check.get_active():
            return
        self.stream_ctrl.select_subtitle_track(index)
        popover.popdown()
        self._show_osd_temporarily()

    def _on_prefs(self, _btn) -> None:
        from tvh.config import load_player_prefs, save_player_prefs, PlayerPreferences
        prefs = load_player_prefs()

        dialog = Adw.PreferencesWindow(transient_for=self.window, title="Preferencje odtwarzacza")
        dialog.set_default_size(560, 520)

        page = Adw.PreferencesPage(title="Odtwarzacz", icon_name="multimedia-player-symbolic")
        dialog.add(page)

        # --- Dekoder ---
        group_dec = Adw.PreferencesGroup(
            title="Dekoder wideo",
            description="Sprzętowy (VA-API) wymusza va*/vaapi*dec dla H.264, HEVC, "
            "MPEG-2, MPEG-4, VP8/VP9, VC-1/WMV3, JPEG, AV1. Klatki zostają w "
            "DMABuf – zero-copy do gtk4paintablesink / glimagesink.",
        )
        page.add(group_dec)
        decoder_row = Adw.ComboRow(title="Preferencja dekodera")
        decoder_row.set_model(Gtk.StringList.new([
            "auto (HW tam gdzie stabilne)",
            "sprzętowy VA-API (wszystkie kodeki, zero-copy)",
            "programowy (CPU)",
        ]))
        idx = {"auto": 0, "hw": 1, "sw": 2}.get(prefs.decoder_pref, 0)
        decoder_row.set_selected(idx)
        group_dec.add(decoder_row)

        # --- Wyjście wideo ---
        group_out = Adw.PreferencesGroup(
            title="Wyjście wideo (Wayland zero-copy)",
            description="Zalecane: gtk4paintablesink albo glimagesink/glsinkbin. "
            "Oba importują DMABuf przez EGL – bez kopii przez RAM.",
        )
        page.add(group_out)
        output_row = Adw.ComboRow(title="Ścieżka wyjścia")
        output_row.set_model(Gtk.StringList.new([
            "auto (Wayland: gtk4paintablesink + glsinkbin)",
            "gtk4paintablesink + glsinkbin (zalecane, zero-copy)",
            "glimagesink / glsinkbin (OpenGL Wayland, zero-copy)",
            "VA surface / DMABuf bezpośrednio (GDK)",
            "vapostproc (konwersja GPU, zero-copy)",
            "programowe (videoconvert, kopia CPU)",
        ]))
        oidx = {
            "auto": 0,
            "gtk4": 1,
            "gl": 2,
            "glimagesink": 2,
            "va-surface": 3,
            "vapostproc": 4,
            "software": 5,
        }.get(prefs.video_output, 0)
        output_row.set_selected(oidx)
        group_out.add(output_row)

        # --- Języki ---
        group_lang = Adw.PreferencesGroup(title="Języki")
        page.add(group_lang)

        audio_entry = Adw.EntryRow(title="Preferowane języki audio")
        audio_entry.set_text(", ".join(prefs.preferred_audio_langs))
        audio_entry.set_tooltip_text("Kody oddzielone przecinkiem, np. pl, en, de (kolejność = priorytet)")
        group_lang.add(audio_entry)

        sub_entry = Adw.EntryRow(title="Preferowane języki napisów")
        sub_entry.set_text(", ".join(prefs.preferred_sub_langs))
        sub_entry.set_tooltip_text("Kody oddzielone przecinkiem, np. pl, en")
        group_lang.add(sub_entry)

        sub_en = Adw.SwitchRow(title="Napisy domyślnie włączone")
        sub_en.set_active(prefs.subtitles_enabled)
        group_lang.add(sub_en)

        font_row = Adw.SpinRow.new_with_range(10, 48, 1)
        font_row.set_title("Rozmiar czcionki napisów (pt)")
        font_row.set_subtitle("Dotyczy napisów tekstowych/teletekstu — DVBSUB to bitmapa")
        font_row.set_value(prefs.subtitle_font_pt)
        group_lang.add(font_row)

        # --- Napisy OSD (nakladka GTK, nie dekoder) ---
        group_osd = Adw.PreferencesGroup(
            title="Napisy OSD",
            description="Rozmiar czcionki i przewijanie opisu audycji w dolnej belce OSD "
            "oraz rozmiar czcionki górnego paska (nazwa kanału, zegar).",
        )
        page.add(group_osd)

        top_font_row = Adw.SpinRow.new_with_range(12, 48, 1)
        top_font_row.set_title("Rozmiar czcionki górnego paska OSD (pt)")
        top_font_row.set_value(getattr(prefs, "osd_top_bar_font_pt", 20))
        group_osd.add(top_font_row)

        osd_font_row = Adw.SpinRow.new_with_range(10, 40, 1)
        osd_font_row.set_title("Rozmiar czcionki opisu audycji OSD (pt)")
        osd_font_row.set_value(getattr(prefs, "osd_subtitle_font_pt", 16))
        group_osd.add(osd_font_row)

        autoscroll_row = Adw.SwitchRow(title="Automatyczne przewijanie opisu")
        autoscroll_row.set_subtitle("Gdy opis nie mieści się w widocznym obszarze")
        autoscroll_row.set_active(getattr(prefs, "osd_desc_autoscroll", False))
        group_osd.add(autoscroll_row)

        direction_row = Adw.ComboRow(title="Kierunek przewijania")
        direction_row.set_model(Gtk.StringList.new(["Góra → dół", "Dół → góra"]))
        direction_row.set_selected(0 if getattr(prefs, "osd_desc_scroll_direction", "down") == "down" else 1)
        group_osd.add(direction_row)

        def _save(*_a):
            dmap = {0: "auto", 1: "hw", 2: "sw"}
            omap = {
                0: "auto",
                1: "gtk4",
                2: "glimagesink",
                3: "va-surface",
                4: "vapostproc",
                5: "software",
            }
            new_prefs = PlayerPreferences(
                decoder_pref=dmap.get(decoder_row.get_selected(), "auto"),
                video_output=omap.get(output_row.get_selected(), "auto"),
                preferred_audio_langs=[x.strip() for x in audio_entry.get_text().split(",") if x.strip()],
                preferred_sub_langs=[x.strip() for x in sub_entry.get_text().split(",") if x.strip()],
                subtitles_enabled=sub_en.get_active(),
                subtitle_font_pt=int(font_row.get_value()),
                osd_top_bar_font_pt=int(top_font_row.get_value()),
                osd_subtitle_font_pt=int(osd_font_row.get_value()),
                osd_desc_autoscroll=autoscroll_row.get_active(),
                osd_desc_scroll_direction="down" if direction_row.get_selected() == 0 else "up",
            )
            save_player_prefs(new_prefs)
            self.stream_ctrl.reload_prefs()
            self.apply_osd_prefs()
            dialog.close()

        # Przycisk zapisu w nagłówku
        save_btn = Gtk.Button(label="Zapisz")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", _save)
        # Adw.PreferencesWindow nie ma prostego pack_end – używamy close-request lub extra
        # Alternatywa: auto-save przy zamknięciu
        dialog.connect("close-request", lambda *_: (_save() or False))

        dialog.present()
        self._show_osd_temporarily()

