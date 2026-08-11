"""
TvhLibrary spina HtspClient z GObject-owym swiatem GTK: trzyma stan
(kanaly/tagi/EPG/nagrania) jako GObject z sygnalami, zeby widoki mogly sie
podlaczyc przez `connect()` tak jak do kazdego innego widgetu GTK.

Sciezka mediow (muxpkt) NIE idzie przez GLib – jest dostarczana bezposrednio
do handlera ustawionego przez StreamController, z watku asyncio. Dzieki temu
setki pakietow/s nie zapychaja petli GUI.
"""
from __future__ import annotations

import itertools
import logging
from typing import Callable, Dict, Optional

import gi

gi.require_version("GObject", "2.0")
from gi.repository import GObject, GLib  # noqa: E402

from .async_bridge import bridge
from .client import HtspClient, HtspAuthError, HtspError
from .models import Channel, ChannelTag, DvrConfig, EpgEvent, Recording

logger = logging.getLogger("tvh.library")

# Handler muxpkt: (subscription_id, msg_dict) -> None
# Wywolywany z watku asyncio – musi byc thread-safe.
MuxpktHandler = Callable[[int, dict], None]


class TvhLibrary(GObject.GObject):
    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "disconnected": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "connect-failed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "channels-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "tags-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "epg-changed": (GObject.SignalFlags.RUN_FIRST, None, (int,)),  # channel_id
        "recordings-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "initial-sync-done": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # licznik postepu podczas wstepnej synchronizacji (kanaly, zdarzenia EPG)
        "sync-progress": (GObject.SignalFlags.RUN_FIRST, None, (int, int)),
        # Rzadkie zdarzenia sterujace – nadal przez GLib (bezpieczne dla GTK)
        "stream-started": (GObject.SignalFlags.RUN_FIRST, None, (int, object)),
        "stream-stopped": (GObject.SignalFlags.RUN_FIRST, None, (int,)),
        "signal-status": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self.client = HtspClient()
        self.channels: Dict[int, Channel] = {}
        self.tags: Dict[int, ChannelTag] = {}
        self.events: Dict[int, EpgEvent] = {}
        self.events_by_channel: Dict[int, list] = {}
        self.recordings: Dict[int, Recording] = {}
        self.dvr_configs: Dict[str, DvrConfig] = {}
        self._sub_id_counter = itertools.count(1)
        self.host = ""
        self.port = 9982
        self.http_port = 9981
        self.username = ""
        self.password = ""

        # Bezposredni handler pakietow mediow (watku asyncio → appsrc).
        # NIE emitujemy sygnalu GObject na kazdy muxpkt.
        self._muxpkt_handler: Optional[MuxpktHandler] = None

        # --- Debouncing/batching sygnalow -------------------------------
        # Tvheadend podczas wstepnej synchronizacji wysyla kanaly/zdarzenia
        # EPG pojedynczo (moga byc ich tysiace) - emitowanie sygnalu GTK i
        # przebudowa calej listy przy KAZDEJ wiadomosci zapycha petle GLib
        # i GTK zglasza "nie odpowiada". Zamiast tego zbieramy zmiany i
        # emitujemy sygnal co ~150-200ms (jeden rebuild zamiast tysiecy).
        self._channels_emit_scheduled = False
        self._epg_dirty_channels: set[int] = set()
        self._epg_emit_scheduled = False
        self._sync_channel_count = 0
        self._sync_event_count = 0
        self._sync_progress_scheduled = False
        self._initial_sync_done = False

        c = self.client
        c.on_channel_add = lambda m: bridge.emit_to_gtk(self._on_channel_add, m)
        c.on_channel_update = lambda m: bridge.emit_to_gtk(self._on_channel_add, m)
        c.on_channel_delete = lambda m: bridge.emit_to_gtk(self._on_channel_delete, m)
        c.on_tag_add = lambda m: bridge.emit_to_gtk(self._on_tag_add, m)
        c.on_tag_update = lambda m: bridge.emit_to_gtk(self._on_tag_add, m)
        c.on_event_add = lambda m: bridge.emit_to_gtk(self._on_event_add, m)
        c.on_event_update = lambda m: bridge.emit_to_gtk(self._on_event_add, m)
        c.on_dvr_entry_add = lambda m: bridge.emit_to_gtk(self._on_dvr_add, m)
        c.on_dvr_entry_update = lambda m: bridge.emit_to_gtk(self._on_dvr_add, m)
        c.on_dvr_entry_delete = lambda m: bridge.emit_to_gtk(self._on_dvr_delete, m)
        c.on_initial_syncdone = lambda: bridge.emit_to_gtk(self._on_initial_sync)
        # HOT PATH: muxpkt bezposrednio z watku asyncio – zero GLib.idle_add
        c.on_muxpkt = self._on_muxpkt_direct
        c.on_subscription_start = lambda m: bridge.emit_to_gtk(self._on_sub_start, m)
        c.on_subscription_stop = lambda m: bridge.emit_to_gtk(self._on_sub_stop, m)
        c.on_signal_status = lambda m: bridge.emit_to_gtk(self._on_signal_status, m)
        c.on_disconnect = lambda: bridge.emit_to_gtk(self._on_disconnect)

    def set_muxpkt_handler(self, handler: Optional[MuxpktHandler]) -> None:
        """Ustawia (lub czyści) handler pakietów mediów.
        Handler jest wywoływany z wątku asyncio – musi być thread-safe.
        """
        self._muxpkt_handler = handler

    # ------------------------------------------------------------------ #
    # Polaczenie
    # ------------------------------------------------------------------ #
    def connect_to_server(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        http_port: int = 9981,
    ) -> None:
        self.host, self.port, self.username = host, port, username
        self.http_port = http_port or 9981
        self.password = password or ""

        async def _do():
            await self.client.connect(host, port)
            await self.client.hello()
            if username:
                await self.client.authenticate(username, password)
            await self.client.enable_async_metadata(epg=True)

        def _ok(_result):
            self.emit("connected")

        def _err(exc: Exception):
            msg = str(exc)
            if isinstance(exc, HtspAuthError):
                msg = "Blad autoryzacji - sprawdz login/haslo"
            elif isinstance(exc, HtspError):
                msg = f"Blad HTSP: {exc}"
            self.emit("connect-failed", msg)

        bridge.call_with_callback(_do(), _ok, _err)

    def disconnect_from_server(self) -> None:
        bridge.call(self.client.close())

    # ------------------------------------------------------------------ #
    # Handlery push z serwera (juz w petli GLib) – poza muxpkt
    # ------------------------------------------------------------------ #
    def _on_channel_add(self, m: dict) -> None:
        ch = Channel.from_htsp(m)
        # Heurystyka radio/TV: kanal bez skladowej wideo w EPG raczej trudno
        # wykryc z samego channelAdd; wielu userow ma tagi typu "Radio" -
        # oznaczamy dodatkowo po nazwie tagu w resolve_radio_flags(). Tagi
        # moga przyjsc przed lub po kanale, wiec probujemy od razu tutaj
        # (na wypadek gdy tag juz jest znany) - pelny re-scan i tak nastapi
        # gdy przyjdzie/zaktualizuje sie tagAdd.
        self.channels[ch.channel_id] = ch
        self._sync_channel_count += 1
        if any("radio" in self.tags[tid].name.lower() for tid in ch.tag_ids if tid in self.tags):
            ch.is_radio = True
        self._schedule_channels_changed()
        self._schedule_sync_progress()

    def _on_channel_delete(self, m: dict) -> None:
        self.channels.pop(m.get("channelId"), None)
        self._schedule_channels_changed()

    def _on_tag_add(self, m: dict) -> None:
        tag = ChannelTag.from_htsp(m)
        self.tags[tag.tag_id] = tag
        self.emit("tags-changed")
        self._resolve_radio_flags()

    # ------------------------------------------------------------------ #
    # Batching/debounce pomocnicze
    # ------------------------------------------------------------------ #
    def _schedule_channels_changed(self) -> None:
        if self._channels_emit_scheduled:
            return
        self._channels_emit_scheduled = True
        GLib.timeout_add(150, self._flush_channels_changed)

    def _flush_channels_changed(self) -> bool:
        self._channels_emit_scheduled = False
        self.emit("channels-changed")
        return False

    def _schedule_epg_changed(self, channel_id: int) -> None:
        self._epg_dirty_channels.add(channel_id)
        if self._epg_emit_scheduled:
            return
        self._epg_emit_scheduled = True
        GLib.timeout_add(200, self._flush_epg_changed)

    def _flush_epg_changed(self) -> bool:
        self._epg_emit_scheduled = False
        dirty = self._epg_dirty_channels
        self._epg_dirty_channels = set()
        for channel_id in dirty:
            self.emit("epg-changed", channel_id)
        return False

    def _schedule_sync_progress(self) -> None:
        if self._initial_sync_done or self._sync_progress_scheduled:
            return
        self._sync_progress_scheduled = True
        GLib.timeout_add(120, self._flush_sync_progress)

    def _flush_sync_progress(self) -> bool:
        self._sync_progress_scheduled = False
        if not self._initial_sync_done:
            self.emit("sync-progress", self._sync_channel_count, self._sync_event_count)
        return False

    def _resolve_radio_flags(self) -> None:
        radio_tag_ids = {
            tid for tid, t in self.tags.items() if "radio" in t.name.lower()
        }
        if not radio_tag_ids:
            return
        for ch in self.channels.values():
            if any(tid in radio_tag_ids for tid in ch.tag_ids):
                ch.is_radio = True
        self.emit("channels-changed")

    def _on_event_add(self, m: dict) -> None:
        ev = EpgEvent.from_htsp(m)
        self.events[ev.event_id] = ev
        ch_id = ev.channel_id
        if ch_id not in self.events_by_channel:
            self.events_by_channel[ch_id] = []
        lst = self.events_by_channel[ch_id]
        # aktualizacja lub dopisanie
        for i, old in enumerate(lst):
            if old.event_id == ev.event_id:
                lst[i] = ev
                break
        else:
            lst.append(ev)
        self._sync_event_count += 1
        self._schedule_epg_changed(ch_id)
        self._schedule_sync_progress()

    def _on_dvr_add(self, m: dict) -> None:
        rec = Recording.from_htsp(m)
        self.recordings[rec.entry_id] = rec
        self.emit("recordings-changed")

    def _on_dvr_delete(self, m: dict) -> None:
        self.recordings.pop(m.get("id") or m.get("entryId"), None)
        self.emit("recordings-changed")

    def _on_initial_sync(self) -> None:
        self._initial_sync_done = True
        # finalny, pelny rebuild widokow po zakonczeniu synchronizacji
        self.emit("channels-changed")
        self.emit("initial-sync-done")

    def _on_disconnect(self) -> None:
        self.emit("disconnected")

    # ------------------------------------------------------------------ #
    # HOT PATH mediów – wątek asyncio, bez GLib
    # ------------------------------------------------------------------ #
    def _on_muxpkt_direct(self, sid: int, m: dict) -> None:
        """Wywoływane bezpośrednio z wątku asyncio (HtspClient).
        Zero idle_add, zero sygnałów GObject – tylko callback do StreamController.
        """
        handler = self._muxpkt_handler
        if handler is not None:
            handler(sid, m)

    def _on_sub_start(self, m: dict) -> None:
        self.emit("stream-started", m.get("subscriptionId", 0), m)

    def _on_sub_stop(self, m: dict) -> None:
        self.emit("stream-stopped", m.get("subscriptionId", 0))

    def _on_signal_status(self, m: dict) -> None:
        self.emit("signal-status", m)

    # ------------------------------------------------------------------ #
    # Strumieniowanie
    # ------------------------------------------------------------------ #
    def new_subscription_id(self) -> int:
        return next(self._sub_id_counter)

    def subscribe_channel(self, channel_id: int, subscription_id: Optional[int] = None) -> int:
        sid = subscription_id or self.new_subscription_id()
        bridge.call(self.client.subscribe(channel_id, sid))
        return sid

    def unsubscribe(self, subscription_id: int) -> None:
        bridge.call(self.client.unsubscribe(subscription_id))

    def get_http_stream_url(
        self,
        channel_id: int,
        on_ok: Callable[[str], None],
        on_err: Optional[Callable[[Exception], None]] = None,
        profile: str = "pass",
    ) -> None:
        """URL HTTP MPEG-TS z getTicket.

        TVH zwraca gotowe `path` + `ticket` – używamy ich wprost
        (nie składamy channelid sami → unika 400 przy ujemnych ID).
        """
        from urllib.parse import quote

        host = self.host or "127.0.0.1"
        http_port = self.http_port or 9981
        user = self.username or ""
        password = self.password or ""

        def _with_ticket(resp: dict) -> None:
            path = (resp.get("path") or "").strip()
            ticket = resp.get("ticket") or ""
            if not path:
                cid = channel_id & 0xFFFFFFFF if channel_id < 0 else channel_id
                path = f"/stream/channelid/{cid}"
            if not path.startswith("/"):
                path = "/" + path
            qs = []
            if ticket:
                qs.append(f"ticket={quote(str(ticket), safe='')}")
            if profile:
                qs.append(f"profile={quote(profile, safe='')}")
            query = ("?" + "&".join(qs)) if qs else ""
            auth = ""
            if not ticket and user:
                auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
            url = f"http://{auth}{host}:{http_port}{path}{query}"
            logger.info(
                "HTTP stream: path=%s ticket=%s profile=%s",
                path,
                "yes" if ticket else "no",
                profile or "(default)",
            )
            on_ok(url)

        def _fail(exc: Exception) -> None:
            logger.warning("getTicket failed (%s) – fallback URL", exc)
            cid = channel_id & 0xFFFFFFFF if channel_id < 0 else channel_id
            path = f"/stream/channelid/{cid}"
            qs = f"?profile={quote(profile, safe='')}" if profile else ""
            auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
            on_ok(f"http://{auth}{host}:{http_port}{path}{qs}")

        bridge.call_with_callback(
            self.client.get_ticket(channel_id=channel_id),
            _with_ticket,
            _fail if on_err is None else on_err,
        )

    # ------------------------------------------------------------------ #
    # DVR
    # ------------------------------------------------------------------ #
    def refresh_dvr_configs(self, callback=None) -> None:
        def _ok(resp: dict):
            self.dvr_configs.clear()
            for e in resp.get("dvrconfigs", []):
                self.dvr_configs[e.get("uuid", "")] = DvrConfig(
                    uuid=e.get("uuid", ""), name=e.get("name", "") or "Domyslny"
                )
            if callback:
                callback()

        bridge.call_with_callback(
            self.client.get_dvr_configs(),
            _ok,
            lambda e: logger.error("getDvrConfigs: %s", e),
        )

    def record_event(self, channel_id: int, event_id: int) -> None:
        bridge.call_with_callback(
            self.client.add_dvr_entry(channel_id=channel_id, event_id=event_id),
            lambda r: logger.info("Zaplanowano nagranie: %s", r),
            lambda e: logger.error("addDvrEntry: %s", e),
        )

    def cancel_recording(self, entry_id: int) -> None:
        bridge.call_with_callback(
            self.client.cancel_dvr_entry(entry_id),
            lambda r: logger.info("Anulowano nagranie %s", entry_id),
            lambda e: logger.error("cancelDvrEntry: %s", e),
        )

    def stop_recording(self, entry_id: int) -> None:
        bridge.call_with_callback(
            self.client.stop_dvr_entry(entry_id),
            lambda r: logger.info("Zatrzymano nagranie %s", entry_id),
            lambda e: logger.error("stopDvrEntry: %s", e),
        )

    def delete_recording(self, entry_id: int) -> None:
        bridge.call_with_callback(
            self.client.delete_dvr_entry(entry_id),
            lambda r: logger.info("Usunieto nagranie %s", entry_id),
            lambda e: logger.error("deleteDvrEntry: %s", e),
        )

    # ------------------------------------------------------------------ #
    # Pomocnicze widoki danych
    # ------------------------------------------------------------------ #
    def tv_channels(self):
        return sorted(
            (c for c in self.channels.values() if not c.is_radio),
            key=lambda c: c.number or c.channel_id,
        )

    def radio_channels(self):
        return sorted(
            (c for c in self.channels.values() if c.is_radio),
            key=lambda c: c.number or c.channel_id,
        )

    def current_event_for_channel(self, channel_id: int, now_ts: int) -> Optional[EpgEvent]:
        lst = self.events_by_channel.get(channel_id, [])
        for ev in lst:
            if ev.start <= now_ts < ev.stop:
                return ev
        return None
