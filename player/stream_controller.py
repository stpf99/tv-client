"""
StreamController spina TvhLibrary (zrodlo pakietow muxpkt z HTSP) z
GstPlayer (appsrc per elementarny strumien). Odpowiada tez za "ostatnio
odtwarzane".

Kolejnosc zdarzen dla jednego kanalu:
    1. subscribe_channel() - Tvheadend zaczyna wysylac subscriptionStart
    2. subscriptionStart (`_on_stream_started`) - zawiera liste `streams`
       (index + typ kazdej skladowej: wideo, audio, teletekst, napisy...).
       Dopiero teraz wiemy jakie `caps` ustawic, wiec budujemy pipeline.
       To zdarzenie idzie przez GLib (rzadkie).
    3. muxpkt (`_on_muxpkt_direct`) - kazdy pakiet ma numer `stream` (index z
       kroku 2). Z watku asyncio robimy tylko put_nowait do kolejki
       GstPlayera; wlasciwy push do appsrc robi osobny watek gst-feeder.
       Watek odczytu TCP HTSP nigdy nie alokuje Gst.Buffer ani nie czeka
       na lock – sciezka tcp → gst jest maksymalnie krotka.

Thread-safety:
  - _on_muxpkt_direct jest wywolywany z watku asyncio (tylko put_nowait)
  - feeder GstPlayera i build/play/stop pipeline'u sa na osobnych watkach
  - indeksy strumieni i subscription_id sa chronione lockiem
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from gi.repository import GObject

from tvh.library import TvhLibrary
from tvh.models import Channel
from tvh.config import load_player_prefs, save_player_prefs, PlayerPreferences
from .gst_player import GstPlayer, AUDIO_CAPS, VIDEO_CAPS, AUDIO_PRIORITY

logger = logging.getLogger("tvh.stream_controller")

VIDEO_TYPES = set(VIDEO_CAPS.keys())
AUDIO_TYPES = set(AUDIO_CAPS.keys())
SUBTITLE_TYPES = {"DVBSUB", "TEXTSUB", "TELETEXT"}

# Sentinel dla "wlacz napisy, auto-wybor jezyka" - uzywany w menu UI gdy
# playbin ma TEXT flag wylaczona (bo napisy sa globalnie wylaczone w
# configu) i lista sciezek jest jeszcze pusta, wiec nie ma czego wybrac
# per-index. Odroznia sie od realnych indeksow (zawsze >= 0) i od None
# (off).
SUBTITLE_AUTO = -2


class StreamController(GObject.GObject):
    __gsignals__ = {
        "tracks-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, library: TvhLibrary, recent_store=None) -> None:
        super().__init__()
        self.library = library
        self.player = GstPlayer()
        self.recent_store = recent_store
        self.prefs: PlayerPreferences = load_player_prefs()
        self.current_subscription_id: Optional[int] = None
        self.current_channel: Optional[Channel] = None
        self._video_stream_index: Optional[int] = None
        self._audio_stream_index: Optional[int] = None
        self._subtitle_stream_index: Optional[int] = None
        # Restart-based przelaczanie napisow w trybie HTTP TS (patrz
        # select_subtitle_track/_restart_with_subtitle) - stan
        # "docelowego" indeksu napisow do zaaplikowania po ponownym
        # starcie streamu, gdy nowe sciezki sie pojawia.
        self._pending_subtitle_index: Optional[int] = None
        self._pending_subtitle_applied: bool = True
        # Chroni odczyt/zapis indeksów i sid między wątkiem asyncio a GLib
        self._lock = threading.Lock()

        # Pełna lista ścieżek z ostatniego subscriptionStart
        self._all_streams: List[Dict[str, Any]] = []
        self._audio_tracks: List[Dict[str, Any]] = []
        self._subtitle_tracks: List[Dict[str, Any]] = []

        # Rzadkie zdarzenia sterujące – nadal przez sygnały GObject (GLib)
        library.connect("stream-started", self._on_stream_started)
        library.connect("stream-stopped", self._on_stream_stopped)

        # HOT PATH: bezpośredni callback z wątku asyncio
        library.set_muxpkt_handler(self._on_muxpkt_direct)

        # Preferencje gracza → player
        self.player.set_preferences(self.prefs)

        # play_channel() idzie przez getTicket+HTTP+playbin3 (nie subscribe
        # HTSP), więc to GstPlayer zna realne ścieżki audio/napisów -
        # przekaż jego sygnał dalej, żeby UI mogło zostać podłączone tylko
        # do stream_ctrl (jak dotychczas), bez wiedzy o zmianie architektury.
        self.player.connect("tracks-changed", lambda *_: self.emit("tracks-changed"))
        self.player.connect("tracks-changed", self._maybe_apply_pending_subtitle)

    def reload_prefs(self) -> None:
        self.prefs = load_player_prefs()
        self.player.set_preferences(self.prefs)

    def play_channel(self, channel: Channel) -> None:
        """Live TV przez HTTP MPEG-TS + VAAPI. Ticket HTSP → stream URL."""
        self.stop()
        self.current_channel = channel
        self._pending_subtitle_index = None
        self._pending_subtitle_applied = True
        with self._lock:
            self.current_subscription_id = None
            self._video_stream_index = None
            self._audio_stream_index = None
            self._subtitle_stream_index = None
            self._all_streams = []
            self._audio_tracks = []
            self._subtitle_tracks = []
        if self.recent_store:
            self.recent_store.add_channel(channel)

        play_token = object()
        self._http_play_token = play_token

        def _start(uri: str) -> None:
            # Anuluj jeśli w międzyczasie wybrano inny kanał
            if getattr(self, "_http_play_token", None) is not play_token:
                return
            if self.current_channel is not channel:
                return
            safe = uri
            if "ticket=" in safe:
                # nie loguj ticketu w całości
                import re

                safe = re.sub(r"ticket=[^&]+", "ticket=***", safe)
            if "@" in safe:
                safe = "http://***@" + safe.split("@", 1)[1]
            logger.info("Odtwarzanie kanalu %s przez HTTP TS: %s", channel.name, safe)
            self.player.set_preferences(self.prefs)
            # Krótka przerwa po stop() – reset VCN przy AVC<->HEVC (AMD)
            from gi.repository import GLib
            def _go():
                if getattr(self, "_http_play_token", None) is not play_token:
                    return False
                if self.current_channel is not channel:
                    return False
                self.player.play_http_ts(uri)
                self.emit("tracks-changed")
                return False
            GLib.timeout_add(450, _go)

        def _err(exc: Exception) -> None:
            logger.error("Nie udało się pobrać ticketu HTTP: %s", exc)

        # path+ticket z getTicket; profile=pass jak w playliście M3U z TVH
        self.library.get_http_stream_url(
            channel.channel_id, _start, on_err=_err, profile="pass"
        )

    def play_url(self, url: str, title: str = "Nagranie") -> None:
        """Odtwarzanie nagrania / dowolnego HTTP MPEG-TS (DVR)."""
        self.stop()
        self.current_channel = None
        self._pending_subtitle_index = None
        self._pending_subtitle_applied = True
        with self._lock:
            self.current_subscription_id = None
            self._video_stream_index = None
            self._audio_stream_index = None
            self._subtitle_stream_index = None
            self._all_streams = []
            self._audio_tracks = []
            self._subtitle_tracks = []

        play_token = object()
        self._http_play_token = play_token

        safe = url
        if "ticket=" in safe:
            import re
            safe = re.sub(r"ticket=[^&]+", "ticket=***", safe)
        if "@" in safe:
            safe = "http://***@" + safe.split("@", 1)[1]
        logger.info("Odtwarzanie nagrania „%s” przez HTTP: %s", title, safe)
        self.player.set_preferences(self.prefs)

        from gi.repository import GLib

        def _go():
            if getattr(self, "_http_play_token", None) is not play_token:
                return False
            self.player.play_http_ts(url)
            self.emit("tracks-changed")
            return False

        GLib.timeout_add(300, _go)

    def stop(self) -> None:
        # Najpierw unieważnij sid/indeksy – concurrent muxpkt staje się no-op
        with self._lock:
            sid = self.current_subscription_id
            self.current_subscription_id = None
            self._video_stream_index = None
            self._audio_stream_index = None
            self._subtitle_stream_index = None
            self._all_streams = []
            self._audio_tracks = []
            self._subtitle_tracks = []
        if sid is not None:
            self.library.unsubscribe(sid)
        self.player.stop()
        self.current_channel = None
        self.emit("tracks-changed")

    # ------------------------------------------------------------------ tracks API
    #
    # UWAGA: play_channel() odtwarza przez getTicket+HTTP+playbin3, NIE
    # przez subscribe() HTSP - subscriptionStart (obsługiwany niżej w
    # _on_stream_started) nigdy nie nadejdzie dla tej ścieżki, więc
    # _audio_tracks/_subtitle_tracks poniżej zostają puste. Realnym
    # źródłem prawdy o ścieżkach jest playbin3 (GstPlayer), stąd delegacja.
    # _on_stream_started/_audio_tracks zostają nietknięte jako fallback,
    # gdyby kiedyś wrócił tryb subscribe+appsrc.
    def get_audio_tracks(self) -> List[Dict[str, Any]]:
        """Lista ścieżek audio: [{index, language, description}, ...]"""
        pb_tracks = self.player.get_audio_tracks()
        if pb_tracks:
            return pb_tracks
        with self._lock:
            return list(self._audio_tracks)

    def get_subtitle_tracks(self) -> List[Dict[str, Any]]:
        pb_tracks = self.player.get_subtitle_tracks()
        if pb_tracks:
            return pb_tracks
        with self._lock:
            return list(self._subtitle_tracks)

    def get_current_audio_index(self) -> Optional[int]:
        if self.player.get_audio_tracks():
            return self.player.get_current_audio_index()
        with self._lock:
            return self._audio_stream_index

    def get_current_subtitle_index(self) -> Optional[int]:
        if self.player.get_subtitle_tracks():
            return self.player.get_current_subtitle_index()
        with self._lock:
            return self._subtitle_stream_index

    def select_audio_track(self, stream_index: int) -> bool:
        """Przełącz ścieżkę audio na podany index."""
        if self.player.get_audio_tracks():
            return self.player.select_audio_track(stream_index)
        with self._lock:
            track = next((t for t in self._audio_tracks if t.get("index") == stream_index), None)
            if track is None:
                return False
            if self._audio_stream_index == stream_index:
                return True
            self._audio_stream_index = stream_index
            audio_type = track.get("type")
        logger.info("Przełączanie audio na index=%s type=%s", stream_index, audio_type)
        try:
            self.player.rebuild_audio(audio_type)
            self.emit("tracks-changed")
            return True
        except Exception:
            logger.exception("Nie udało się przełączyć ścieżki audio")
            return False

    def select_subtitle_track(self, stream_index: Optional[int]) -> bool:
        """Przełącz napisy. stream_index=None wyłącza napisy.

        Dla trybu HTTP TS (playbin) robimy pelny restart streamu.
        Sam on/off zapisujemy do GLOBALNEGO configu (prefs.subtitles_enabled)
        - play_http_ts() czyta go PRZED pojawieniem sie jakichkolwiek
        text-tags-changed i ustawia _subs_user_disabled od razu, wiec
        auto-selekcja najlepszej sciezki w ogole nie odpala. Zero wyscigu
        i zadnego trickowania current-text/flush w locie (to zacinalo A/V
        na nieseekowalnym HTTP - patrz historia zmian).
        Wybor KONKRETNEJ sciezki (index != None, gdy jest ich kilka) nie
        ma odpowiednika w globalnym configu (tylko preferred_sub_langs),
        wiec dla tego przypadku nadal aplikujemy indeks po restarcie przez
        _pending_subtitle_index/_maybe_apply_pending_subtitle.
        """
        if self.player._http_mode:
            enabled = stream_index is not None
            if self.prefs.subtitles_enabled != enabled:
                self.prefs.subtitles_enabled = enabled
                try:
                    save_player_prefs(self.prefs)
                except Exception:
                    logger.exception("nie udalo sie zapisac prefs.subtitles_enabled")
            self._restart_with_subtitle(stream_index)
            return True
        with self._lock:
            if stream_index is None:
                self._subtitle_stream_index = None
                sub_type = None
            else:
                track = next(
                    (t for t in self._subtitle_tracks if t.get("index") == stream_index), None
                )
                if track is None:
                    return False
                self._subtitle_stream_index = stream_index
                sub_type = track.get("type")
        logger.info("Przełączanie napisów na index=%s type=%s", stream_index, sub_type)
        try:
            self.player.rebuild_subtitles(sub_type)
            self.emit("tracks-changed")
            return True
        except Exception:
            logger.exception("Nie udało się przełączyć napisów")
            return False

    def _restart_with_subtitle(self, stream_index: Optional[int]) -> None:
        """Restartuje aktualny kanal. Sam on/off napisow jest juz zalatwiony
        przez prefs.subtitles_enabled (patrz select_subtitle_track) -
        play_http_ts() go odczyta przy starcie. Pending-index (nizej)
        potrzebny jest tylko gdy uzytkownik wybral KONKRETNA sciezke
        (bo tego nie ma w globalnym configu)."""
        channel = self.current_channel
        if channel is None:
            return
        logger.info("Restart streamu dla zmiany napisow: index=%s", stream_index)
        self.play_channel(channel)
        if stream_index is not None and stream_index != SUBTITLE_AUTO:
            self._pending_subtitle_index = stream_index
            self._pending_subtitle_applied = False

    def _maybe_apply_pending_subtitle(self, *_args) -> None:
        if self._pending_subtitle_applied:
            return
        idx = self._pending_subtitle_index
        if idx is None:
            self._pending_subtitle_applied = True
            return
        tracks = self.player.get_subtitle_tracks()
        if not tracks:
            return  # czekamy na kolejny tracks-changed po wykryciu sciezek
        self._pending_subtitle_applied = True
        if any(t.get("index") == idx for t in tracks):
            self.player.select_subtitle_track(idx)

    # ------------------------------------------------------------------ muxpkt
    def _on_muxpkt_direct(self, sid: int, msg: dict) -> None:
        """HOT PATH – wywoływane z wątku asyncio (odczyt TCP HTSP).

        Tylko: sprawdzenie sid + put_nowait do kolejki feedera.
        Żadnego Gst.Buffer, żadnego locka appsrc, żadnego GLib.
        Właściwy push robi wątek gst-feeder w GstPlayer.
        """
        with self._lock:
            if sid != self.current_subscription_id:
                return
            video_idx = self._video_stream_index
            audio_idx = self._audio_stream_index
            sub_idx = self._subtitle_stream_index

        payload = msg.get("payload")
        if not payload:
            return

        stream_idx = msg.get("stream")
        pts_us = msg.get("pts")
        dts_us = msg.get("dts")
        duration_us = msg.get("duration")

        if stream_idx == video_idx:
            self.player.push_video_bytes(
                payload, pts_us=pts_us, dts_us=dts_us, duration_us=duration_us
            )
        elif stream_idx == audio_idx:
            self.player.push_audio_bytes(
                payload, pts_us=pts_us, dts_us=dts_us, duration_us=duration_us
            )
        elif stream_idx is not None and stream_idx == sub_idx:
            self.player.push_subtitle_bytes(
                payload, pts_us=pts_us, dts_us=dts_us, duration_us=duration_us
            )

    def _pick_audio(self, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not candidates:
            return None
        prefs = self.prefs

        def score(s: Dict[str, Any]) -> tuple:
            lang_rank = prefs.rank_language(s.get("language"), prefs.preferred_audio_langs)
            prio = AUDIO_PRIORITY.get(s.get("type"), 0)
            return (lang_rank, -prio)

        return min(candidates, key=score)

    def _pick_subtitle(self, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not candidates or not self.prefs.subtitles_enabled:
            return None
        prefs = self.prefs

        def score(s: Dict[str, Any]) -> int:
            return prefs.rank_language(s.get("language"), prefs.preferred_sub_langs)

        return min(candidates, key=score)

    def _on_stream_started(self, _lib, sid: int, msg: dict) -> None:
        # GLib main thread
        with self._lock:
            if sid != self.current_subscription_id:
                return

        streams = msg.get("streams", [])
        types_summary = [
            (s.get("index"), s.get("type"), s.get("language")) for s in streams
        ]
        logger.info("Subskrypcja %s wystartowala, strumienie: %s", sid, types_summary)

        video = next((s for s in streams if s.get("type") in VIDEO_TYPES), None)

        audio_candidates = [s for s in streams if s.get("type") in AUDIO_TYPES]
        audio = self._pick_audio(audio_candidates)

        sub_candidates = [s for s in streams if s.get("type") in SUBTITLE_TYPES]
        dvb_subs = [s for s in sub_candidates if s.get("type") == "DVBSUB"]
        subtitle = self._pick_subtitle(dvb_subs) if dvb_subs else None

        unknown = [
            s.get("type")
            for s in streams
            if s.get("type") not in VIDEO_TYPES
            and s.get("type") not in AUDIO_TYPES
            and s.get("type") not in SUBTITLE_TYPES
            and s.get("type") not in ("CA",)
        ]
        if unknown:
            logger.warning(
                "Subskrypcja %s: nierozpoznane typy strumieni: %s",
                sid,
                unknown,
            )

        if not video and not audio:
            logger.error(
                "Subskrypcja %s: brak rozpoznanego strumienia audio/wideo (typy: %s)",
                sid,
                [s.get("type") for s in streams],
            )
            return

        with self._lock:
            if sid != self.current_subscription_id:
                return
            self._all_streams = list(streams)
            self._audio_tracks = [
                {
                    "index": s.get("index"),
                    "type": s.get("type"),
                    "language": s.get("language") or "",
                    "description": self._track_label(s),
                }
                for s in audio_candidates
            ]
            self._subtitle_tracks = [
                {
                    "index": s.get("index"),
                    "type": s.get("type"),
                    "language": s.get("language") or "",
                    "description": self._track_label(s),
                }
                for s in sub_candidates
            ]
            self._video_stream_index = video.get("index") if video else None
            self._audio_stream_index = audio.get("index") if audio else None
            self._subtitle_stream_index = subtitle.get("index") if subtitle else None

        if audio:
            logger.info(
                "Wybrano audio: index=%s type=%s lang=%s (kandydaci: %s)",
                audio.get("index"),
                audio.get("type"),
                audio.get("language"),
                [
                    (c.get("index"), c.get("type"), c.get("language"))
                    for c in audio_candidates
                ],
            )
        else:
            logger.warning("Subskrypcja %s: brak obsługiwanego strumienia audio", sid)

        if subtitle:
            logger.info(
                "Wybrano napisy: index=%s type=%s lang=%s",
                subtitle.get("index"),
                subtitle.get("type"),
                subtitle.get("language"),
            )

        try:
            self.player.build(
                video.get("type") if video else None,
                audio.get("type") if audio else None,
                subtitle.get("type") if subtitle else None,
            )
            self.player.play()
            self.emit("tracks-changed")
        except Exception:
            logger.exception("Nie udalo sie zbudowac pipeline'u odtwarzania")

    @staticmethod
    def _track_label(s: Dict[str, Any]) -> str:
        lang = (s.get("language") or "").strip()
        typ = s.get("type") or "?"
        if lang:
            return f"{lang.upper()} ({typ})"
        return typ

    def _on_stream_stopped(self, _lib, sid: int) -> None:
        with self._lock:
            if sid == self.current_subscription_id:
                logger.info("Subskrypcja %s zatrzymana przez serwer", sid)
                self._video_stream_index = None
                self._audio_stream_index = None
                self._subtitle_stream_index = None
