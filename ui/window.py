from __future__ import annotations

import logging
import time
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio, GLib  # noqa: E402

from tvh.config import ServerConfig, load_config, save_config
from tvh.library import TvhLibrary
from tvh.models import Channel
from player.stream_controller import StreamController
from ui.connection_dialog import ConnectionDialog
from ui.channel_list import ChannelListView
from ui.live_view import LiveView
from ui.epg_view import EpgView
from ui.recordings_view import RecordingsView
from ui.recent_view import RecentView
from ui.recent import RecentStore
from ui.reminders import ReminderStore
from ui.tray import install_background_support
from ui.mpris import MprisService

logger = logging.getLogger("tvh.window")


MODES = [
    ("live_tv", "TV na żywo", "tv-symbolic"),
    ("live_radio", "Radio na żywo", "audio-input-microphone-symbolic"),
    ("epg", "Przewodnik EPG", "x-office-calendar-symbolic"),
    ("recordings", "Nagrania", "folder-videos-symbolic"),
    ("recent", "Ostatnio odtwarzane", "document-open-recent-symbolic"),
]


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="TVHeadend GNOME Client", default_width=1280, default_height=800)

        self.library = TvhLibrary()
        self.recent_store = RecentStore()
        self.reminder_store = ReminderStore(application=app)
        self.stream_ctrl = StreamController(self.library, self.recent_store)

        self.mpris = MprisService(
            on_play_pause=self._mpris_play_pause,
            on_stop=self._mpris_stop,
            on_next=self._mpris_next,
            on_previous=self._mpris_previous,
        )
        self.mpris.start()

        self._build_ui()
        self._connect_library_signals()

        self.bg_ctrl = install_background_support(app, self)
        self.reminder_store.connect("changed", self._on_reminders_changed)
        self._on_reminders_changed(self.reminder_store)
        self._watch_seconds_today = 0
        self._watch_session_start: Optional[float] = None
        GLib.timeout_add_seconds(60, self._tick_watch_time)

        cfg = load_config()
        if cfg and cfg.host:
            GLib.idle_add(self._do_connect, cfg)
        else:
            GLib.idle_add(self._show_connection_dialog)

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()

        header = Adw.HeaderBar()
        self.status_icon = Gtk.Image.new_from_icon_name("network-offline-symbolic")
        header.pack_start(self.status_icon)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("Połącz z serwerem…", "win.connect")
        menu.append("Preferencje odtwarzacza…", "win.player_prefs")
        menu.append("Odśwież EPG", "win.refresh_epg")
        menu.append("O programie", "win.about")
        menu_btn.set_menu_model(menu)
        header.pack_end(menu_btn)

        reconnect_action = Gio.SimpleAction.new("connect", None)
        reconnect_action.connect("activate", lambda *_: self._show_connection_dialog())
        self.add_action(reconnect_action)

        refresh_action = Gio.SimpleAction.new("refresh_epg", None)
        refresh_action.connect("activate", lambda *_: self.library.refresh_dvr_configs())
        self.add_action(refresh_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._show_about)
        self.add_action(about_action)

        prefs_action = Gio.SimpleAction.new("player_prefs", None)
        prefs_action.connect("activate", self._show_player_prefs)
        self.add_action(prefs_action)

        toolbar_view.add_top_bar(header)

        self.window_title = Adw.WindowTitle(title="TVHeadend GNOME Client")
        header.set_title_widget(self.window_title)

        # --- Nawigacja: waska szyna z samymi ikonami (jak w Kodi/paskach
        # bocznych odtwarzaczy multimedialnych) zamiast szerokiego panelu z
        # etykietami, ktory zabieral miejsce i nie skalowal sie z oknem.
        self.rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.rail.add_css_class("tvh-rail")
        self.rail.set_valign(Gtk.Align.START)
        self.rail.set_margin_top(6)
        self.rail.set_margin_bottom(6)
        self.rail.set_margin_start(4)
        self.rail.set_margin_end(4)

        self._rail_buttons: dict[str, Gtk.Button] = {}
        for mode_id, label, icon_name in MODES:
            btn = Gtk.Button()
            btn.set_child(Gtk.Image.new_from_icon_name(icon_name))
            btn.add_css_class("flat")
            btn.add_css_class("tvh-rail-btn")
            btn.set_tooltip_text(label)
            btn.connect("clicked", self._on_rail_clicked, mode_id, label)
            self.rail.append(btn)
            self._rail_buttons[mode_id] = btn

        rail_scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vexpand=True,
        )
        rail_scroller.set_child(self.rail)
        rail_scroller.add_css_class("tvh-rail-scroller")

        # --- Zawartosc: stos trybow, wypelnia cala pozostala przestrzen
        self.content_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.content_stack.set_hexpand(True)
        self.content_stack.set_vexpand(True)

        self.live_tv_view = self._build_live_page(radio=False)
        self.live_radio_view = self._build_live_page(radio=True)
        self.epg_view = EpgView(self.library, reminder_store=self.reminder_store)
        self.recordings_view = RecordingsView(
            self.library, on_play_url=self._play_recording_url
        )
        self.recent_view = RecentView(self.library, self.recent_store, self._play_channel_by_id)

        self.content_stack.add_named(self.live_tv_view, "live_tv")
        self.content_stack.add_named(self.live_radio_view, "live_radio")
        self.content_stack.add_named(self.epg_view, "epg")
        self.content_stack.add_named(self.recordings_view, "recordings")
        self.content_stack.add_named(self.recent_view, "recent")

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        outer.append(rail_scroller)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        outer.append(self.content_stack)
        outer.set_hexpand(True)
        outer.set_vexpand(True)

        # --- Wspoldzielona belka statusu (mini-player, widoczna w kazdym trybie)
        self.mini_bar = self._build_mini_status_bar()

        # --- Nakladka ladowania/synchronizacji (kanaly + EPG z cache serwera) ---
        self.sync_overlay = Gtk.Overlay(vexpand=True)
        self.sync_overlay.set_child(outer)

        self.sync_status_page = Adw.StatusPage(
            title="Synchronizacja z serwerem…",
            description="Pobieranie listy kanałów i danych EPG",
            icon_name="emblem-synchronizing-symbolic",
        )
        self.sync_status_page.add_css_class("tvh-sync-overlay")
        self.sync_spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        self.sync_progress_lbl = Gtk.Label()
        self.sync_progress_lbl.add_css_class("dim-label")
        sync_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.CENTER)
        sync_box.append(self.sync_spinner)
        sync_box.append(self.sync_progress_lbl)
        self.sync_status_page.set_child(sync_box)
        self.sync_status_page.set_visible(False)
        self.sync_overlay.add_overlay(self.sync_status_page)

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrapper.append(self.sync_overlay)
        wrapper.append(self.mini_bar)

        toolbar_view.set_content(wrapper)
        self.set_content(toolbar_view)

        # klawisz Escape wychodzi z fullscreen
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

        # domyslnie aktywny tryb: TV na zywo
        self._on_rail_clicked(self._rail_buttons["live_tv"], "live_tv", "TV na żywo")

    def _build_live_page(self, radio: bool) -> LiveView:
        live_view = LiveView(self.library, self.stream_ctrl, self)
        channel_list = ChannelListView(
            self.library, radio, on_play=lambda ch: self._play_channel(ch, live_view)
        )
        live_view.set_channel_list(channel_list)
        live_view.channel_list = channel_list  # type: ignore[attr-defined]
        return live_view

    def _build_mini_status_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.add_css_class("tvh-mini-bar")
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.set_margin_start(10)
        bar.set_margin_end(10)

        self.mini_icon = Gtk.Image.new_from_icon_name("media-playback-stop-symbolic")
        self.mini_channel_lbl = Gtk.Label(label="Nic nie jest odtwarzane", xalign=0, hexpand=True)
        self.mini_channel_lbl.add_css_class("dim-label")

        self.mini_stop_btn = Gtk.Button(icon_name="media-playback-stop-symbolic")
        self.mini_stop_btn.add_css_class("flat")
        self.mini_stop_btn.connect("clicked", lambda *_: self.stream_ctrl.stop())

        bar.append(self.mini_icon)
        bar.append(self.mini_channel_lbl)
        bar.append(self.mini_stop_btn)
        return bar

    # ------------------------------------------------------------------ #
    def _on_rail_clicked(self, _btn, mode_id: str, label: str) -> None:
        for mid, b in self._rail_buttons.items():
            if mid == mode_id:
                b.add_css_class("tvh-rail-active")
            else:
                b.remove_css_class("tvh-rail-active")
        self.content_stack.set_visible_child_name(mode_id)
        self.window_title.set_title(label)

    def _on_key_pressed(self, _ctrl, keyval, _keycode, _state) -> bool:
        from gi.repository import Gdk
        if keyval == Gdk.KEY_Escape and self.is_fullscreen():
            self.unfullscreen()
            return True
        if keyval == Gdk.KEY_F11:
            if self.is_fullscreen():
                self.unfullscreen()
            else:
                self.fullscreen()
            return True
        return False

    # ------------------------------------------------------------------ #
    def _play_channel(self, channel: Channel, live_view: LiveView) -> None:
        live_view.play_channel(channel)
        self.mini_channel_lbl.set_text(f"{'📻' if channel.is_radio else '📺'} {channel.name}")
        self.mini_icon.set_from_icon_name("media-playback-start-symbolic")
        self.mpris.update_now_playing(channel.name, channel.name)

    def _play_recording_url(self, url: str, title: str) -> None:
        """Odtwarzanie nagrania DVR – przełącz na TV na żywo i odtwórz URL."""
        self._on_rail_clicked(self._rail_buttons["live_tv"], "live_tv", "TV na żywo")
        self.stream_ctrl.play_url(url, title=title)
        self.mini_channel_lbl.set_text(f"🎬 {title}")
        self.mini_icon.set_from_icon_name("media-playback-start-symbolic")
        self.mpris.update_now_playing(title, "Nagranie DVR")
        # play_url() nie przechodzi przez LiveView.play_channel(), wiec
        # panel listy kanalow nie chowal sie automatycznie przy odtwarzaniu
        # nagrania (w odroznieniu od zwyklej zmiany kanalu) - ujednolicone
        # tym samym mechanizmem/preferencja co zwykle odtwarzanie.
        self.live_tv_view.maybe_hide_channel_list()

    def _play_channel_by_id(self, channel_id: int) -> None:
        ch = self.library.channels.get(channel_id)
        if not ch:
            return
        mode_id = "live_radio" if ch.is_radio else "live_tv"
        live_view = self.live_radio_view if ch.is_radio else self.live_tv_view
        btn = self._rail_buttons[mode_id]
        self._on_rail_clicked(btn, mode_id, btn.get_tooltip_text() or "")
        self._play_channel(ch, live_view)

    # ------------------------------------------------------------------ #
    def _mpris_play_pause(self) -> None:
        lv = self._active_live_view()
        if lv:
            lv._on_play_pause(None)

    def _mpris_stop(self) -> None:
        self.stream_ctrl.stop()

    def _mpris_next(self) -> None:
        self._channel_step(1)

    def _mpris_previous(self) -> None:
        self._channel_step(-1)

    def _active_live_view(self):
        name = self.content_stack.get_visible_child_name()
        if name == "live_tv":
            return self.live_tv_view
        if name == "live_radio":
            return self.live_radio_view
        return None

    def _channel_step(self, direction: int) -> None:
        current = self.stream_ctrl.current_channel
        if not current:
            return
        channels = self.library.radio_channels() if current.is_radio else self.library.tv_channels()
        ids = [c.channel_id for c in channels]
        if current.channel_id not in ids:
            return
        idx = (ids.index(current.channel_id) + direction) % len(ids)
        self._play_channel_by_id(ids[idx])

    # ------------------------------------------------------------------ #
    def _show_connection_dialog(self) -> None:
        dlg = ConnectionDialog(self, load_config(), self._on_connection_submitted)
        dlg.present()

    def _on_connection_submitted(self, cfg: ServerConfig) -> None:
        save_config(cfg)
        self._do_connect(cfg)

    def _do_connect(self, cfg: ServerConfig) -> bool:
        self.status_icon.set_from_icon_name("network-transmit-receive-symbolic")
        self.library.connect_to_server(
            cfg.host,
            cfg.htsp_port,
            cfg.username,
            cfg.password,
            http_port=getattr(cfg, "http_port", 9981) or 9981,
        )
        return False

    def _connect_library_signals(self) -> None:
        self.library.connect("connected", self._on_connected)
        self.library.connect("connect-failed", self._on_connect_failed)
        self.library.connect("disconnected", self._on_disconnected)
        self.library.connect("initial-sync-done", self._on_initial_sync_done)
        self.library.connect("sync-progress", self._on_sync_progress)
        self.reminder_store.connect("reminder-due", self._on_reminder_due)

    def _on_reminder_due(self, _store, reminder) -> None:
        # Powiadomienie systemowe wysyla juz ReminderStore._notify(); tutaj
        # tylko log - ewentualny toast w oknie wymagalby globalnego
        # Adw.ToastOverlay, ktorego ta wersja okna celowo nie ma (patrz
        # notatka przy polaczeniu z bledem nizej).
        logger.info(
            "Przypomnienie: %s na %s o %s",
            reminder.title,
            reminder.channel_name,
            time.strftime("%H:%M", time.localtime(reminder.start)),
        )

    def _on_reminders_changed(self, store) -> None:
        if getattr(self, "bg_ctrl", None) is not None:
            self.bg_ctrl.set_reminders(list(store.items))

    def _tick_watch_time(self) -> bool:
        # Prosty poll co 60s zamiast podpinania sie pod kazde play()/stop()
        # w StreamController - wystarczajaco dokladne dla informacyjnego
        # licznika "dziś oglądano" w powiadomieniu przy zminimalizowaniu.
        ch = self.stream_ctrl.current_channel
        if ch is not None:
            self._watch_seconds_today += 60
        channel_name = ch.name if ch is not None else ""
        program_title = ""
        if ch is not None:
            ev = self.library.current_event_for_channel(ch.channel_id, int(time.time()))
            if ev is not None:
                program_title = ev.title or ""
        if getattr(self, "bg_ctrl", None) is not None:
            self.bg_ctrl.set_now_playing(channel_name, program_title)
            self.bg_ctrl.set_watch_seconds_today(self._watch_seconds_today)
        return True

    def _on_connected(self, _lib) -> None:
        self.status_icon.set_from_icon_name("network-idle-symbolic")
        logger.info("Połączono z serwerem Tvheadend")
        # Serwer zaraz zacznie sypac channelAdd/eventAdd (moga byc tysiace) -
        # pokazujemy nakladke, zeby bylo widac ze aplikacja pracuje, a nie zawiesila sie
        self.sync_progress_lbl.set_text("Łączenie…")
        self.sync_status_page.set_visible(True)
        self.sync_spinner.set_spinning(True)

    def _on_sync_progress(self, _lib, channel_count: int, event_count: int) -> None:
        self.sync_progress_lbl.set_text(
            f"Kanały: {channel_count} · Pozycje EPG: {event_count}"
        )

    def _on_connect_failed(self, _lib, message: str) -> None:
        self.status_icon.set_from_icon_name("network-error-symbolic")
        toast = Adw.Toast(title=f"Nie udało się połączyć: {message}")
        # Adw.ToastOverlay nie jest tu wpiety globalnie dla prostoty - logujemy
        logger.error("Polaczenie nieudane: %s", message)

    def _on_disconnected(self, _lib) -> None:
        self.status_icon.set_from_icon_name("network-offline-symbolic")

    def _on_initial_sync_done(self, _lib) -> None:
        self.library.refresh_dvr_configs()
        self.sync_spinner.set_spinning(False)
        self.sync_status_page.set_visible(False)
        logger.info("Wstepna synchronizacja zakonczona: %d kanalow, %d tagow",
                     len(self.library.channels), len(self.library.tags))

    def _show_player_prefs(self, *_a) -> None:
        # Deleguj do aktywnego LiveView (ten sam dialog)
        lv = self._active_live_view()
        if lv is not None:
            lv._on_prefs(None)
        else:
            # Fallback: otwórz z live_tv
            self.live_tv_view._on_prefs(None)

    def _show_about(self, *_a) -> None:
        about = Adw.AboutWindow(
            transient_for=self,
            application_name="TVHeadend GNOME Client",
            application_icon="tv-symbolic",
            version="1.0",
            developer_name="Tomasz",
            license_type=Gtk.License.GPL_3_0,
            comments="Klient Tvheadend (HTSP) dla GTK4/libadwaita z GStreamer.",
        )
        about.present()
