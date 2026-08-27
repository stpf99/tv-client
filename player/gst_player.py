"""
Odtwarzacz oparty o GStreamer, zasilany surowymi pakietami elementarnych
strumieni (ES) otrzymywanymi z HTSP (wiadomości `muxpkt`).

Ścieżka mediów (tcp → gst):
  wątek asyncio (odczyt TCP)  →  push_*_bytes (tylko put_nowait)
  →  SimpleQueue  →  wątek gst-feeder  →  _push (Gst.Buffer + appsrc)

Dzięki temu wątek odczytu HTSP nigdy nie alokuje buforów GStreamer ani nie
czeka na lock – recv() wraca natychmiast nawet przy burstach 1080i/HEVC.

Każdy elementarny strumień (wideo, audio, napisy DVBSUB) dostaje własny appsrc
z jawnymi caps na podstawie typu z subscriptionStart.

Audio: jawny łańcuch parser + dekoder (bez decodebin), z listą fallbacków.
Wideo: jawny łańcuch parse+dekoder (VA/SW) pod live HTSP ES; decodebin tylko fallback.
  Post-decode Wayland ZERO-COPY (zalecane):
    gtk4paintablesink  – GdkPaintable w Gtk.Picture (DMABuf / GLMemory)
    glimagesink        – GstGLSinkBin: glupload importuje DMABuf przez EGL
    glsinkbin sink=gtk4paintablesink
        va*dec/vaapi*dec → VAMemory|DMABuf → glupload → gtk4paintablesink
  videoconvert TYLKO przy video_output=software (kopia CPU zrywa DMABuf).
  HW: JPEG, MPEG-2, MPEG-4:2, H.264 AVC/MVC, VP8, VP9, VC-1, WMV3, HEVC, AV1.
Napisy: appsrc → dvbsuboverlay (nakładka na wideo).

Preferencje (PlayerPreferences):
  - decoder_pref: auto | hw | sw  – wpływ na ranking dekoderów VA-API
  - video_output: auto | gtk4 | glimagesink | va-surface | vapostproc | gl | software
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import List, Optional, TYPE_CHECKING

import gi

# EGL + Wayland MUSZĄ być ustawione zanim Gst.init() stworzy GstGLDisplay,
# inaczej glupload nie zaimportuje DMABuf (zero-copy pada do kopii CPU).
def _prepare_wayland_gl_env() -> None:
    gdk_backend = (os.environ.get("GDK_BACKEND") or "").lower()
    wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )
    if gdk_backend == "x11":
        wayland = False
    if wayland or gdk_backend == "wayland":
        os.environ.setdefault("GST_GL_WINDOW", "wayland")
        os.environ.setdefault("GST_GL_PLATFORM", "egl")
    # Legacy gstreamer-vaapi na AMD/NVIDIA (Intel działa bez tego).
    os.environ.setdefault("GST_VAAPI_ALL_DRIVERS", "1")


_prepare_wayland_gl_env()

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GstApp, GObject, GLib  # noqa: E402

if TYPE_CHECKING:
    from tvh.config import PlayerPreferences

logger = logging.getLogger("tvh.player")

Gst.init(None)

VIDEO_CAPS = {
    "H264": "video/x-h264,stream-format=byte-stream,alignment=nal",
    "H264MVC": "video/x-h264,stream-format=byte-stream,alignment=nal",
    "H.264": "video/x-h264,stream-format=byte-stream,alignment=nal",
    "HEVC": "video/x-h265,stream-format=byte-stream,alignment=au",
    "H265": "video/x-h265,stream-format=byte-stream,alignment=au",
    "MPEG2VIDEO": "video/mpeg,mpegversion=2,systemstream=false",
    "MPEG2": "video/mpeg,mpegversion=2,systemstream=false",
    "MPEG4VIDEO": "video/mpeg,mpegversion=4,systemstream=false",
    "MPEG4": "video/mpeg,mpegversion=4,systemstream=false",
    "VP8": "video/x-vp8",
    "VP9": "video/x-vp9",
    "AV1": "video/x-av1",
    "VC1": "video/x-wmv,wmvversion=3,format=WVC1",
    "WMV3": "video/x-wmv,wmvversion=3",
    "JPEG": "image/jpeg",
    "MJPEG": "image/jpeg",
}

# Jawne łańcuchy wideo (parser, lista dekoderów HW→SW) – bez decodebin = niższa latencja live
VIDEO_DECODER_CHAIN = {
    "H264": {
        "parser": "h264parse",
        "hw": ["vah264dec", "vaapih264dec"],
        "sw": ["avdec_h264", "openh264dec"],
    },
    "H264MVC": {
        "parser": "h264parse",
        "hw": ["vah264dec", "vaapih264dec"],
        "sw": ["avdec_h264"],
    },
    "H.264": {
        "parser": "h264parse",
        "hw": ["vah264dec", "vaapih264dec"],
        "sw": ["avdec_h264", "openh264dec"],
    },
    "HEVC": {
        "parser": "h265parse",
        "hw": ["vah265dec", "vaapih265dec"],
        "sw": ["avdec_h265", "libde265dec"],
    },
    "H265": {
        "parser": "h265parse",
        "hw": ["vah265dec", "vaapih265dec"],
        "sw": ["avdec_h265", "libde265dec"],
    },
    "MPEG2VIDEO": {
        "parser": "mpegvideoparse",
        "hw": ["vampeg2dec", "vaapimpeg2dec"],
        "sw": ["avdec_mpeg2video", "mpeg2dec"],
    },
    "MPEG2": {
        "parser": "mpegvideoparse",
        "hw": ["vampeg2dec", "vaapimpeg2dec"],
        "sw": ["avdec_mpeg2video", "mpeg2dec"],
    },
    "MPEG4VIDEO": {
        "parser": "mpeg4videoparse",
        "hw": ["vampeg4dec", "vaapimpeg4dec"],
        "sw": ["avdec_mpeg4"],
    },
    "MPEG4": {
        "parser": "mpeg4videoparse",
        "hw": ["vampeg4dec", "vaapimpeg4dec"],
        "sw": ["avdec_mpeg4"],
    },
    "VP9": {
        "parser": "vp9parse",
        "hw": ["vavp9dec", "vaapivp9dec"],
        "sw": ["avdec_vp9", "vp9dec"],
    },
    "AV1": {
        "parser": "av1parse",
        "hw": ["vaav1dec", "vaapiav1dec"],
        "sw": ["avdec_av1", "dav1ddec"],
    },
    "VP8": {
        "parser": "vp8parse",
        "hw": ["vavp8dec", "vaapivp8dec"],
        "sw": ["avdec_vp8", "vp8dec"],
    },
    "VC1": {
        "parser": None,
        "hw": ["vavc1dec", "vaapivc1dec"],
        "sw": ["avdec_vc1", "avdec_wmv3"],
    },
    "WMV3": {
        "parser": None,
        "hw": ["vavc1dec", "vaapivc1dec"],
        "sw": ["avdec_wmv3", "avdec_vc1"],
    },
    "JPEG": {
        "parser": "jpegparse",
        "hw": ["vajpegdec", "vaapijpegdec"],
        "sw": ["jpegdec"],
    },
    "MJPEG": {
        "parser": "jpegparse",
        "hw": ["vajpegdec", "vaapijpegdec"],
        "sw": ["jpegdec"],
    },
}

AUDIO_CAPS = {
    "MPEG2AUDIO": "audio/mpeg,mpegversion=1,layer=2,parsed=false",
    "MPEGAUDIO": "audio/mpeg,mpegversion=1,layer=2,parsed=false",
    "MP2": "audio/mpeg,mpegversion=1,layer=2,parsed=false",
    "MP3": "audio/mpeg,mpegversion=1,layer=3,parsed=false",
    "AC3": "audio/x-ac3,framed=false",
    "AC3+": "audio/x-eac3,framed=false",
    "EAC3": "audio/x-eac3,framed=false",
    "E-AC-3": "audio/x-eac3,framed=false",
    "AAC": "audio/mpeg,mpegversion=4,stream-format=adts",
    "HEAAC": "audio/mpeg,mpegversion=4,stream-format=adts",
    "AAC-ADTS": "audio/mpeg,mpegversion=4,stream-format=adts",
    "AAC-LATM": "audio/mpeg,mpegversion=4,stream-format=loas",
    "MP4A-LATM": "audio/mpeg,mpegversion=4,stream-format=loas",
    "MPEG4AUDIO": "audio/mpeg,mpegversion=4,stream-format=adts",
    "AAC-HE": "audio/mpeg,mpegversion=4,stream-format=adts",
    "AAC-LC": "audio/mpeg,mpegversion=4,stream-format=adts",
    "DTS": "audio/x-dts,framed=false",
    "TRUEHD": "audio/x-true-hd,framed=false",
    "VORBIS": "audio/x-vorbis",
    "OPUS": "audio/x-opus",
    "FLAC": "audio/x-flac",
    "PCM": "audio/x-raw",
}

# DVB subtitles (bitmap) – typowy caps z HTSP
SUBTITLE_CAPS = {
    "DVBSUB": "subpicture/x-dvd",  # lub application/x-dvb – zależnie od demuxera
}

AUDIO_PRIORITY = {
    "TRUEHD": 100, "DTS": 90,
    "EAC3": 80, "AC3+": 80, "E-AC-3": 80,
    "AC3": 70,
    "AAC-LATM": 60, "MP4A-LATM": 60, "AAC": 55, "HEAAC": 55,
    "AAC-ADTS": 55, "MPEG4AUDIO": 55, "AAC-HE": 55, "AAC-LC": 55,
    "OPUS": 50, "VORBIS": 45, "FLAC": 40,
    "MPEG2AUDIO": 30, "MPEGAUDIO": 30, "MP2": 30, "MP3": 25,
    "PCM": 10,
}

# (parser|None, decoder) – pierwszy dostępny wygrywa
AUDIO_DECODER_CHAIN = {
    "AC3": [
        ("ac3parse", "avdec_ac3"),
        (None, "avdec_ac3"),
    ],
    "AC3+": [
        ("ac3parse", "avdec_eac3"),
        (None, "avdec_eac3"),
    ],
    "EAC3": [
        ("ac3parse", "avdec_eac3"),
        (None, "avdec_eac3"),
    ],
    "E-AC-3": [
        ("ac3parse", "avdec_eac3"),
        (None, "avdec_eac3"),
    ],
    "MPEG2AUDIO": [
        # avdec łagodniej znosi urwany początek PES z HTSP niż mpg123
        ("mpegaudioparse", "avdec_mp2"),
        ("mpegaudioparse", "avdec_mpa"),
        ("mpegaudioparse", "avdec_mp3"),
        ("mpegaudioparse", "mpg123audiodec"),
        (None, "avdec_mp2"),
        (None, "avdec_mpa"),
        (None, "avdec_mp3"),
        (None, "mpg123audiodec"),
    ],
    "MPEGAUDIO": [
        ("mpegaudioparse", "avdec_mp2"),
        ("mpegaudioparse", "avdec_mpa"),
        ("mpegaudioparse", "avdec_mp3"),
        ("mpegaudioparse", "mpg123audiodec"),
        (None, "avdec_mpa"),
        (None, "avdec_mp3"),
    ],
    "MP2": [
        ("mpegaudioparse", "avdec_mp2"),
        ("mpegaudioparse", "avdec_mpa"),
        ("mpegaudioparse", "mpg123audiodec"),
        (None, "avdec_mp2"),
        (None, "avdec_mpa"),
    ],
    "MP3": [
        ("mpegaudioparse", "avdec_mp3"),
        ("mpegaudioparse", "mpg123audiodec"),
        (None, "avdec_mp3"),
    ],
    "AAC": [
        ("aacparse", "avdec_aac"),
        ("aacparse", "faad"),
        (None, "avdec_aac"),
    ],
    "HEAAC": [
        ("aacparse", "avdec_aac"),
        (None, "avdec_aac"),
    ],
    "AAC-ADTS": [
        ("aacparse", "avdec_aac"),
        (None, "avdec_aac"),
    ],
    "AAC-LATM": [
        ("aacparse", "avdec_aac"),
        (None, "avdec_aac"),
    ],
    "MP4A-LATM": [
        ("aacparse", "avdec_aac"),
        (None, "avdec_aac"),
    ],
    "MPEG4AUDIO": [
        ("aacparse", "avdec_aac"),
        (None, "avdec_aac"),
    ],
    "AAC-HE": [
        ("aacparse", "avdec_aac"),
        (None, "avdec_aac"),
    ],
    "AAC-LC": [
        ("aacparse", "avdec_aac"),
        (None, "avdec_aac"),
    ],
    "DTS": [
        (None, "avdec_dca"),
        (None, "dca"),
    ],
    "OPUS": [
        (None, "avdec_opus"),
        (None, "opusdec"),
    ],
    "VORBIS": [
        (None, "avdec_vorbis"),
        (None, "vorbisdec"),
    ],
    "FLAC": [
        (None, "avdec_flac"),
        (None, "flacdec"),
    ],
}

_HW_DECODER_FACTORIES = [
    # gstreamer-va (gst-plugins-bad, zalecane)
    "vah264dec", "vah265dec", "vavp8dec", "vavp9dec", "vaav1dec",
    "vampeg2dec", "vampeg4dec", "vavc1dec", "vajpegdec",
    # gstreamer-vaapi (legacy): JPEG, MPEG-2, MPEG-4:2, H.264 AVC/MVC,
    # VP8, VP9, VC-1, WMV3, HEVC → VA surfaces / DMABuf
    "vaapih264dec", "vaapih265dec", "vaapivp8dec", "vaapivp9dec",
    "vaapimpeg2dec", "vaapimpeg4dec", "vaapivc1dec", "vaapijpegdec",
    "vaapiav1dec", "vaapidecodebin",
]

_HEVC_HW_FACTORIES = ("vah265dec", "vaapih265dec")
_AVC_HW_FACTORIES = ("vah264dec", "vaapih264dec")
# HEVC VA na AMD (VCN) + playbin/gtk4 bywał niestabilny przy wymuszonej
# kopii DMA→RAM (amdgpu CS -22). Przy prawdziwym zero-copy (glsinkbin /
# gtk4paintablesink) HW HEVC jest bezpieczny gdy user wybierze "hw".
# auto: HW HEVC tylko z TVH_ENABLE_HW_HEVC=1.
_ENABLE_HW_HEVC = os.environ.get("TVH_ENABLE_HW_HEVC", "0") == "1"
_HW_PROFILE_SWITCH_DELAY_S = float(os.environ.get("TVH_HW_PROFILE_SWITCH_DELAY", "0.4"))

# --- Stabilnosc odbioru live -------------------------------------------------
_BUFFER_MS = int(os.environ.get("TVH_BUFFER_MS", "1500"))
_STALL_TIMEOUT_MS = int(os.environ.get("TVH_STALL_TIMEOUT_MS", "700"))

_ZEROCOPY_OUTPUTS = frozenset(
    ("auto", "gtk4", "glimagesink", "va-surface", "vapostproc", "gl")
)


def _is_wayland() -> bool:
    gdk_backend = (os.environ.get("GDK_BACKEND") or "").lower()
    if gdk_backend == "x11":
        return False
    if gdk_backend == "wayland":
        return True
    return bool(os.environ.get("WAYLAND_DISPLAY")) or (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
    )


def _hw_hevc_allowed(decoder_pref: str) -> bool:
    """HW HEVC: zawsze przy jawnym 'hw'; przy 'auto' tylko z env."""
    if decoder_pref == "sw":
        return False
    if decoder_pref == "hw":
        return True
    return _ENABLE_HW_HEVC


def _hw_video_profile(video_type: Optional[str]) -> Optional[str]:
    if video_type is None:
        return None
    if video_type in ("HEVC", "H265"):
        return "hevc"
    if video_type in ("H264", "H.264", "H264MVC"):
        return "avc"
    return video_type


def _make(factory_name: str, name: Optional[str] = None):
    try:
        return Gst.ElementFactory.make(factory_name, name)
    except Exception:
        return None


def _boost_decoder_ranks(decoder_pref: str = "auto") -> None:
    """Ustaw ranking dekoderów wg preferencji użytkownika.

    decoder_pref=hw → WSZYSTKIE va*/vaapi*dec (w tym HEVC/JPEG/VC1/VP8/MPEG4)
    dostają PRIMARY+256, żeby playbin/decodebin zawsze wybrał HW, a sink
    zero-copy (gtk4paintablesink / glimagesink) dostał VAMemory/DMABuf.
    """
    registry = Gst.Registry.get()
    hevc_ok = _hw_hevc_allowed(decoder_pref)
    hw = []
    for name in _HW_DECODER_FACTORIES:
        feature = registry.find_feature(name, Gst.ElementFactory)
        if feature is None:
            continue
        if name in _HEVC_HW_FACTORIES and not hevc_ok:
            feature.set_rank(Gst.Rank.NONE)
            continue
        if decoder_pref == "sw":
            feature.set_rank(Gst.Rank.MARGINAL)
        elif decoder_pref == "hw":
            feature.set_rank(Gst.Rank.PRIMARY + 256)
        else:  # auto
            if name in _HEVC_HW_FACTORIES:
                feature.set_rank(Gst.Rank.SECONDARY)
            else:
                feature.set_rank(Gst.Rank.PRIMARY + 128)
        hw.append(name)
    if hw:
        logger.info(
            "VA-API (%s, wayland=%s, hevc_hw=%s): %s",
            decoder_pref, _is_wayland(), hevc_ok, ", ".join(hw),
        )
    else:
        logger.info("VA-API niedostępne – dekodowanie wideo programowe")

    if hevc_ok:
        logger.info(
            "Sprzętowy HEVC WŁĄCZONY (pref=%s, TVH_ENABLE_HW_HEVC=%s) – "
            "odstęp %.2fs przy zmianie AVC<->HEVC.",
            decoder_pref, int(_ENABLE_HW_HEVC), _HW_PROFILE_SWITCH_DELAY_S,
        )
    else:
        logger.info(
            "Sprzętowy HEVC wyłączony – HEVC idzie avdec_h265/libde265. "
            "Włączenie: dekoder=sprzętowy w preferencjach albo TVH_ENABLE_HW_HEVC=1"
        )

    libav = []
    for name in (
        "avdec_ac3", "avdec_eac3", "avdec_aac", "avdec_mp2", "avdec_mpa",
        "avdec_mp3", "avdec_dca", "avdec_truehd", "avdec_opus", "avdec_vorbis",
        "avdec_flac", "mpg123audiodec", "avdec_h265", "avdec_h264",
        "avdec_mpeg2video", "avdec_mpeg4", "avdec_vp8", "avdec_vp9",
        "avdec_vc1", "avdec_wmv3", "avdec_av1", "jpegdec",
    ):
        feature = registry.find_feature(name, Gst.ElementFactory)
        if feature is not None:
            if name in ("avdec_h265",) and not hevc_ok:
                feature.set_rank(Gst.Rank.PRIMARY + 200)
            elif decoder_pref == "hw":
                feature.set_rank(Gst.Rank.MARGINAL)
            else:
                feature.set_rank(Gst.Rank.PRIMARY + 96)
            libav.append(name)
    de265 = registry.find_feature("libde265dec", Gst.ElementFactory)
    if de265 is not None:
        if not hevc_ok:
            de265.set_rank(Gst.Rank.PRIMARY + 150)
        elif decoder_pref == "hw":
            de265.set_rank(Gst.Rank.MARGINAL)
        libav.append("libde265dec")
    if libav:
        logger.info("gst-libav / audio: %s", ", ".join(libav))
    else:
        logger.error(
            "Brak dekoderów avdec_* – zainstaluj: sudo moss install gstreamer-plugin-libav"
        )


_boost_decoder_ranks("auto")


class GstPlayer(GObject.GObject):
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "eos": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "paintable-ready": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "decoder-chosen": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # Emitowany gdy zmieni sie lista/aktualny wybor sciezek audio/napisow
        # (tryb playbin3/HTTP-TS - patrz _on_pb_*_tags_changed) lub gdy
        # zmienia sie info o strumieniu (kodek/bitrate/rozdzielczosc, patrz
        # _on_pb_video_tags_changed) uzywane przez OSD.
        "tracks-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "stream-info-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        # Generacja pipeline'u - inkrementowana przy kazdym stop()/nowym
        # starcie. Bus starego pipeline'u nie jest natychmiast odlaczany
        # (add_signal_watch/connect bez pary remove_signal_watch/disconnect),
        # wiec przy szybkiej zmianie kanalu potrafi jeszcze dostarczyc
        # spozniony EOS/ERROR/STATE_CHANGED z NIEISTNIEJACEGO juz pipeline'u
        # DLUGO PO tym jak nowy kanal juz gra. _on_bus_message dostaje wlasna
        # generacje w domknieciu i odrzuca wiadomosci nie pasujace do
        # aktualnej - to jest zrodlo losowo znikajacej nazwy kanalu/OSD
        # (spozniony EOS/ERROR nadpisywal etykiete tytulu nowego kanalu).
        self._generation: int = 0
        self.pipeline: Optional[Gst.Pipeline] = None
        self.appsrc_video: Optional[GstApp.AppSrc] = None
        self.appsrc_audio: Optional[GstApp.AppSrc] = None
        self.appsrc_subtitle: Optional[GstApp.AppSrc] = None
        self.video_sink = None
        self._paintable = None
        self._muted = False
        self._volume = 1.0
        self._base_pts_us: Optional[int] = None
        self._pts_warmup_deadline: Optional[float] = None
        self._pts_warmup_seen: dict = {True: None, False: None}
        self._pts_warmup_buf: list = []
        self._last_hw_video_profile: Optional[str] = None
        self._push_lock = threading.Lock()
        self._prefs: Optional["PlayerPreferences"] = None
        self._current_video_type: Optional[str] = None
        self._current_audio_type: Optional[str] = None
        self._current_sub_type: Optional[str] = None
        self._audio_elements: List[Gst.Element] = []
        self._sub_elements: List[Gst.Element] = []
        self._preroll_us = int(os.environ.get("TVH_PREROLL_MS", "1200")) * 1000
        self._rebuffer_us = int(os.environ.get("TVH_REBUFFER_MS", "600")) * 1000
        self._active_preroll_us = self._preroll_us
        self._preroll_first_pts_us: Optional[int] = None
        self._preroll_wall_start: Optional[float] = None
        self._preroll_done = False
        self._want_playing = False
        self._watchdog_id: Optional[int] = None
        self._last_data_ts: float = 0.0
        self._http_mode: bool = False

        # Sciezki audio/napisow raportowane przez playbin3 (tryb HTTP-TS).
        # Wypelniane w _on_pb_audio_tags_changed / _on_pb_text_tags_changed.
        self._pb_audio_tracks: List[dict] = []
        self._pb_sub_tracks: List[dict] = []
        # Info o strumieniu do ikon OSD (kodek/rozdzielczosc/bitrate).
        self._pb_video_info: dict = {}
        self._pb_audio_info: dict = {}
        # Czy uzytkownik jawnie wylaczyl napisy z menu (rozroznia to od
        # "brak sciezek napisow") - patrz select_subtitle_track.
        self._subs_user_disabled: bool = False

        self._pkt_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._feeder_stop = threading.Event()
        self._feeder_thread = threading.Thread(
            target=self._feeder_loop,
            name="gst-feeder",
            daemon=True,
        )
        self._feeder_thread.start()

    def set_preferences(self, prefs: "PlayerPreferences") -> None:
        self._prefs = prefs
        _boost_decoder_ranks(prefs.decoder_pref if prefs else "auto")
        self._apply_subtitle_font_pt()

    def _apply_subtitle_font_pt(self) -> None:
        if not (self._http_mode and self.pipeline and self._prefs):
            return
        pt = getattr(self._prefs, "subtitle_font_pt", 20) or 20
        try:
            self.pipeline.set_property("subtitle-font-desc", f"Sans {pt}")
        except Exception:
            logger.exception("subtitle-font-desc")

    def build(
        self,
        video_type: Optional[str],
        audio_type: Optional[str],
        subtitle_type: Optional[str] = None,
    ) -> None:
        prev_profile = self._last_hw_video_profile
        if self.pipeline:
            self.stop()

        new_profile = _hw_video_profile(video_type)
        enable_hw = True
        if self._prefs and self._prefs.decoder_pref == "sw":
            enable_hw = False
        dec_pref = (self._prefs.decoder_pref if self._prefs else "auto") or "auto"
        if (
            enable_hw
            and _hw_hevc_allowed(dec_pref)
            and prev_profile is not None
            and new_profile is not None
            and prev_profile != new_profile
            and _HW_PROFILE_SWITCH_DELAY_S > 0
        ):
            logger.info(
                "Zmiana profilu sprzetowego dekodera: %s -> %s – odczekuje %.2fs "
                "na reset VCN",
                prev_profile, new_profile, _HW_PROFILE_SWITCH_DELAY_S,
            )
            time.sleep(_HW_PROFILE_SWITCH_DELAY_S)

        self._last_hw_video_profile = new_profile if enable_hw else None
        self._current_video_type = video_type
        self._current_audio_type = audio_type
        self._current_sub_type = subtitle_type

        pipeline = Gst.Pipeline.new("tvh-player")
        self.pipeline = pipeline
        self._http_mode = False
        self._generation += 1
        gen = self._generation
        bus = pipeline.get_bus()
        self._bus = bus
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message, gen)

        if video_type:
            caps_str = VIDEO_CAPS.get(video_type)
            if caps_str is None:
                logger.warning("Nieznany typ wideo %s – bez jawnych caps", video_type)
            self.appsrc_video = self._build_video_branch(video_type, caps_str)
        else:
            self.appsrc_video = None

        if audio_type:
            caps_str = AUDIO_CAPS.get(audio_type)
            if caps_str is None:
                logger.warning(
                    "Nieznany typ audio '%s' – brak mapowania w AUDIO_CAPS", audio_type
                )
            else:
                logger.info("Audio branch: type=%s caps=%s", audio_type, caps_str)
            self.appsrc_audio = self._build_audio_explicit(audio_type, caps_str)
            if self.appsrc_audio is None:
                logger.warning(
                    "Brak jawnego łańcucha dla %s – fallback decodebin", audio_type
                )
                self.appsrc_audio = self._build_audio_decodebin(caps_str)
        else:
            self.appsrc_audio = None

        if subtitle_type == "DVBSUB":
            self.appsrc_subtitle = self._build_subtitle_branch(subtitle_type)
        else:
            self.appsrc_subtitle = None

        if not self.appsrc_video and not self.appsrc_audio:
            raise RuntimeError("Brak rozpoznanego strumienia audio/wideo")

        self._base_pts_us = None
        self._reset_pts_warmup()
        self._preroll_first_pts_us = None
        self._preroll_wall_start = None
        self._preroll_done = False
        self._want_playing = False

    def _teardown_elements(self, elements: List[Gst.Element]) -> None:
        for el in elements:
            try:
                el.set_state(Gst.State.NULL)
                el.get_state(int(0.5 * Gst.SECOND))
            except Exception:
                logger.debug("teardown: NULL state failed dla %s", el.get_name())
        for el in elements:
            try:
                self.pipeline.remove(el)
            except Exception:
                logger.debug("teardown: remove failed dla %s", el.get_name())

    def rebuild_audio(self, audio_type: Optional[str]) -> None:
        if not self.pipeline:
            return
        with self._push_lock:
            self.appsrc_audio = None
        old_elements = self._audio_elements
        self._audio_elements = []
        self._teardown_elements(old_elements)
        self._current_audio_type = audio_type
        if not audio_type:
            return
        caps_str = AUDIO_CAPS.get(audio_type)
        appsrc = self._build_audio_explicit(audio_type, caps_str)
        if appsrc is None:
            appsrc = self._build_audio_decodebin(caps_str)
        with self._push_lock:
            self.appsrc_audio = appsrc
        if appsrc:
            appsrc.sync_state_with_parent()

    def rebuild_subtitles(self, subtitle_type: Optional[str]) -> None:
        if not self.pipeline:
            return
        with self._push_lock:
            self.appsrc_subtitle = None
        old_elements = self._sub_elements
        self._sub_elements = []
        self._teardown_elements(old_elements)
        self._current_sub_type = subtitle_type
        if subtitle_type == "DVBSUB":
            appsrc = self._build_subtitle_branch(subtitle_type)
            with self._push_lock:
                self.appsrc_subtitle = appsrc
            if appsrc:
                appsrc.sync_state_with_parent()

    _LIVE_QUEUE_NS = int(_BUFFER_MS * Gst.MSECOND)
    _LIVE_QUEUE_BUFFERS = 0
    _PTS_WARMUP_S = 0.3

    @staticmethod
    def _configure_appsrc(appsrc, caps_str: Optional[str]) -> None:
        appsrc.set_property("is-live", True)
        appsrc.set_property("format", Gst.Format.TIME)
        appsrc.set_property("do-timestamp", False)
        appsrc.set_property("min-latency", 0)
        appsrc.set_property("max-bytes", 8 * 1024 * 1024)
        appsrc.set_property("block", False)
        try:
            appsrc.set_property("max-time", int(_BUFFER_MS * Gst.MSECOND))
        except Exception:
            pass
        try:
            appsrc.set_property("emit-signals", False)
        except Exception:
            pass
        if caps_str:
            appsrc.set_property("caps", Gst.Caps.from_string(caps_str))

    @classmethod
    def _configure_live_queue(cls, queue: Gst.Element, preroll: bool = False) -> None:
        queue.set_property("max-size-time", cls._LIVE_QUEUE_NS)
        queue.set_property("max-size-buffers", cls._LIVE_QUEUE_BUFFERS)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("leaky", 2)
        try:
            queue.set_property("silent", True)
        except Exception:
            pass

    def _tighten_queues_after_preroll(self) -> None:
        if not self.pipeline:
            return
        it = self.pipeline.iterate_elements()
        while True:
            ok, el = it.next()
            if ok != Gst.IteratorResult.OK:
                break
            if el.get_factory() and el.get_factory().get_name() == "queue":
                try:
                    self._configure_live_queue(el)
                except Exception:
                    pass

    @staticmethod
    def _configure_live_sink(el: Gst.Element, is_video: bool = False) -> None:
        want_sync = os.environ.get("TVH_SINK_SYNC", "1") != "0"
        if el.find_property("sync") is not None:
            try:
                el.set_property("sync", want_sync)
            except Exception:
                pass
        if el.find_property("qos") is not None:
            try:
                el.set_property("qos", bool(is_video and want_sync))
            except Exception:
                pass
        if el.find_property("max-lateness") is not None:
            try:
                if want_sync:
                    el.set_property("max-lateness", int(150 * Gst.MSECOND))
                else:
                    el.set_property("max-lateness", int(0.8 * Gst.SECOND))
            except Exception:
                pass
        if el.find_property("processing-deadline") is not None:
            try:
                el.set_property("processing-deadline", 40 * Gst.MSECOND)
            except Exception:
                pass
        if is_video and el.find_property("enable-last-sample") is not None:
            try:
                el.set_property("enable-last-sample", False)
            except Exception:
                pass
        try:
            if el.find_property("drift-tolerance") is not None:
                el.set_property("drift-tolerance", 40 * Gst.MSECOND)
            if el.find_property("alignment-threshold") is not None:
                el.set_property("alignment-threshold", 20 * Gst.MSECOND)
            if el.find_property("slave-method") is not None:
                try:
                    el.set_property("slave-method", 1)
                except Exception:
                    pass
        except Exception:
            pass

    # ------------------------------------------------------------------ video
    def _pick_video_decoder(self, video_type: str):
        chain = VIDEO_DECODER_CHAIN.get(video_type)
        if not chain:
            return None
        pref = (self._prefs.decoder_pref if self._prefs else "auto") or "auto"
        order = []
        if pref == "sw":
            order = list(chain["sw"])
        elif pref == "hw":
            order = list(chain["hw"]) + list(chain["sw"])
        else:
            order = list(chain["hw"]) + list(chain["sw"])
        if not _hw_hevc_allowed(pref) and video_type in ("HEVC", "H265"):
            order = [n for n in order if n not in _HEVC_HW_FACTORIES]
        parser_name = chain.get("parser")
        for dec_name in order:
            dec = _make(dec_name)
            if dec is None:
                continue
            parser = None
            used_parser = parser_name
            if parser_name:
                parser = _make(parser_name)
                if parser is None:
                    logger.debug(
                        "Brak parsera %s dla %s – próbuję dekoder bez parsera",
                        parser_name, dec_name,
                    )
                    used_parser = None
            kind = "vaapi" if dec_name.startswith(("va", "vaapi")) else "software"
            return parser, dec, dec_name, kind, used_parser
        return None

    def _build_video_branch(
        self, video_type: str, caps_str: Optional[str]
    ) -> Optional[GstApp.AppSrc]:
        assert self.pipeline is not None
        picked = self._pick_video_decoder(video_type)
        if picked is None:
            logger.warning(
                "Brak jawnego dekodera dla %s – fallback decodebin", video_type
            )
            return self._build_video_decodebin(video_type, caps_str)

        parser, decoder, dec_name, kind, parser_name = picked
        logger.info(
            "Video explicit live: %s → %s%s (%s)",
            video_type,
            f"{parser_name} ! " if parser_name else "",
            dec_name,
            kind,
        )
        self.emit("decoder-chosen", dec_name, kind)

        appsrc = _make("appsrc", "src_video")
        if appsrc is None:
            return None
        self._configure_appsrc(appsrc, caps_str)

        queue_in = _make("queue", "queue_in_video")
        queue_out = _make("queue", "queue_out_video")
        if queue_in is None or queue_out is None:
            return None
        self._configure_live_queue(queue_in)
        self._configure_live_queue(queue_out)

        video_sink = self._make_inner_video_sink()
        if video_sink is None:
            logger.error("brak video sink")
            return None

        if parser is not None:
            for prop, val in (
                ("config-interval", -1),
                ("update-timecode", False),
            ):
                if parser.find_property(prop) is not None:
                    try:
                        parser.set_property(prop, val)
                    except Exception:
                        pass
        for prop, val in (
            ("output-corrupt", False),
            ("discard-corrupted-frames", True),
        ):
            if decoder.find_property(prop) is not None:
                try:
                    decoder.set_property(prop, val)
                except Exception:
                    pass

        pref = (self._prefs.video_output if self._prefs else "auto") or "auto"

        candidates: List[tuple] = []

        def _add_candidate(name: str, factories: list) -> None:
            for f in factories:
                if _make(f) is None:
                    return
            candidates.append((name, factories))

        # Zero-copy: dekoder VA → DMABuf/VAMemory → sink (gtk4 / glupload).
        # videoconvert TYLKO jako ostatni fallback albo przy pref=software.
        if kind == "vaapi":
            if pref == "software":
                _add_candidate("videoconvert", ["videoconvert"])
                _add_candidate("deinterlace+videoconvert", ["deinterlace", "videoconvert"])
                _add_candidate("direct", [])
            elif pref in ("gl", "glimagesink"):
                _add_candidate("glupload+glcolorconvert", ["glupload", "glcolorconvert"])
                _add_candidate("vapostproc+glupload+glcolorconvert", ["vapostproc", "glupload", "glcolorconvert"])
                _add_candidate("vaapipostproc+glupload+glcolorconvert", ["vaapipostproc", "glupload", "glcolorconvert"])
                _add_candidate("glupload", ["glupload"])
                _add_candidate("vapostproc", ["vapostproc"])
                _add_candidate("direct", [])
            elif pref == "vapostproc":
                _add_candidate("vapostproc", ["vapostproc"])
                _add_candidate("vaapipostproc", ["vaapipostproc"])
                _add_candidate("vadeinterlace+vapostproc", ["vadeinterlace", "vapostproc"])
                _add_candidate("direct", [])
                _add_candidate("glupload+glcolorconvert", ["glupload", "glcolorconvert"])
            else:
                # auto / gtk4 / va-surface: najpierw direct DMABuf, potem GPU convert
                _add_candidate("direct", [])
                _add_candidate("vapostproc", ["vapostproc"])
                _add_candidate("vaapipostproc", ["vaapipostproc"])
                _add_candidate("glupload+glcolorconvert", ["glupload", "glcolorconvert"])
                _add_candidate("vapostproc+glupload+glcolorconvert", ["vapostproc", "glupload", "glcolorconvert"])
                _add_candidate("vadeinterlace+vapostproc", ["vadeinterlace", "vapostproc"])
                _add_candidate("videoconvert", ["videoconvert"])
        else:
            if pref in ("gl", "glimagesink"):
                _add_candidate("glupload+glcolorconvert", ["glupload", "glcolorconvert"])
                _add_candidate("glupload", ["glupload"])
            if pref != "software":
                _add_candidate("direct", [])
            _add_candidate("deinterlace+videoconvert", ["deinterlace", "videoconvert"])
            _add_candidate("videoconvert", ["videoconvert"])
            _add_candidate("direct", [])

        if not candidates:
            _add_candidate("direct", [])
            _add_candidate("videoconvert", ["videoconvert"])

        head = [appsrc, queue_in]
        if parser is not None:
            head.append(parser)
        head.extend([decoder, queue_out])

        for el in head:
            self.pipeline.add(el)

        prev = appsrc
        for el in head[1:]:
            if not prev.link(el):
                logger.error(
                    "Video head link failed: %s -> %s",
                    prev.get_name(),
                    el.get_name(),
                )
                return None
            prev = el

        used_path = None
        linked_mids: list = []
        for path_name, factories in candidates:
            mids = []
            ok = True
            for i, fname in enumerate(factories):
                el = _make(fname, f"vpost_{fname}_{i}")
                if el is None:
                    ok = False
                    break
                if fname == "vapostproc" and el.find_property("deinterlace-mode") is not None:
                    try:
                        el.set_property("deinterlace-mode", "auto")
                    except Exception:
                        pass
                mids.append(el)
            if not ok:
                continue

            for el in mids + [video_sink]:
                parent = el.get_parent()
                if parent is not None:
                    try:
                        parent.remove(el)
                    except Exception:
                        pass
                self.pipeline.add(el)

            chain_ok = True
            cur = queue_out
            for el in mids + [video_sink]:
                if not cur.link(el):
                    chain_ok = False
                    logger.debug(
                        "explicit post-decode link fail [%s]: %s -> %s",
                        path_name, cur.get_name(), el.get_name(),
                    )
                    break
                cur = el

            if not chain_ok:
                for el in mids:
                    try:
                        self.pipeline.remove(el)
                    except Exception:
                        pass
                if video_sink.get_parent() is not None:
                    try:
                        self.pipeline.remove(video_sink)
                    except Exception:
                        pass
                continue

            used_path = path_name
            linked_mids = mids
            break

        if used_path is None:
            logger.error(
                "Video explicit: żadna ścieżka post-decode nie zlinkowała się "
                "(candidates=%s)",
                [c[0] for c in candidates],
            )
            return None

        mid_names = " ! ".join(
            (el.get_factory().get_name() if el.get_factory() else "?") for el in linked_mids
        ) if linked_mids else "(direct)"
        sink_name = (
            video_sink.get_factory().get_name() if video_sink.get_factory() else "vsink"
        )
        logger.info(
            "Video live chain: appsrc ! … ! %s ! %s ! %s  [path=%s zerocopy=%s]",
            dec_name,
            mid_names,
            sink_name,
            used_path,
            used_path not in ("videoconvert", "deinterlace+videoconvert"),
        )
        return appsrc

    def _build_video_decodebin(
        self, video_type: str, caps_str: Optional[str]
    ) -> Optional[GstApp.AppSrc]:
        assert self.pipeline is not None
        appsrc = _make("appsrc", "src_video")
        if appsrc is None:
            return None
        self._configure_appsrc(appsrc, caps_str)
        queue_in = _make("queue", "queue_in_video")
        decodebin = _make("decodebin", "decode_video")
        if queue_in is None or decodebin is None:
            return None
        self._configure_live_queue(queue_in)
        if self._prefs and self._prefs.decoder_pref == "sw":
            try:
                decodebin.set_property("force-sw-decoders", True)
            except Exception:
                pass
        owner_pipeline = self.pipeline
        decodebin.connect(
            "pad-added",
            lambda db, pad: GLib.idle_add(self._idle_link_video, pad, owner_pipeline),
        )
        decodebin.connect(
            "element-added",
            lambda db, el: GLib.idle_add(self._idle_decoder_added, el, owner_pipeline),
        )
        for el in (appsrc, queue_in, decodebin):
            self.pipeline.add(el)
        if not appsrc.link(queue_in) or not queue_in.link(decodebin):
            logger.error("video decodebin branch link failed")
            return None
        return appsrc

    def _idle_link_video(self, pad: Gst.Pad, owner_pipeline: Gst.Pipeline) -> bool:
        if owner_pipeline is not self.pipeline:
            return False
        try:
            self._link_video_branch(pad)
        except Exception:
            logger.exception("link video")
        return False

    def _caps_features(self, caps: Optional[Gst.Caps]) -> str:
        if not caps or caps.get_size() == 0:
            return "?"
        feat = caps.get_features(0)
        if feat is None:
            return "system-memory"
        return feat.to_string() if hasattr(feat, "to_string") else str(feat)

    def _feature_kind(self, feat: str) -> str:
        f = (feat or "").lower()
        if "vamemory" in f or "vaapi" in f:
            return "va"
        if "dmabuf" in f or "dma_buf" in f:
            return "dmabuf"
        if "glmemory" in f or "memory:gl" in f:
            return "gl"
        if "systemmemory" in f or f in ("?", "system-memory", ""):
            return "system"
        return "other"

    def _caps_is_interlaced(self, caps: Optional[Gst.Caps]) -> bool:
        if not caps or caps.get_size() == 0:
            return False
        try:
            st = caps.get_structure(0)
            mode = st.get_string("interlace-mode")
            if mode in ("interleaved", "mixed", "fields", "alternate"):
                return True
            fo = st.get_string("field-order")
            if fo and fo not in ("progressive", "unknown", ""):
                return True
        except Exception:
            pass
        return False

    def _build_path_candidates(
        self, feat_kind: str, interlaced: bool
    ) -> List[tuple]:
        pref = (self._prefs.video_output if self._prefs else "auto") or "auto"
        has_vapost = _make("vapostproc") is not None
        has_vaapipost = _make("vaapipostproc") is not None
        has_vadeint = _make("vadeinterlace") is not None
        has_glupload = _make("glupload") is not None
        has_glcolor = _make("glcolorconvert") is not None
        has_videoconvert = _make("videoconvert") is not None
        has_deinterlace = _make("deinterlace") is not None

        paths: List[tuple] = []

        def add(name: str, mids: list) -> None:
            for n in mids:
                if _make(n) is None:
                    return
            paths.append((name, mids))

        if feat_kind in ("va", "dmabuf"):
            if interlaced and has_vadeint:
                if has_vapost:
                    add("vadeinterlace+vapostproc", ["vadeinterlace", "vapostproc"])
                add("vadeinterlace", ["vadeinterlace"])
            add("direct-va/dmabuf", [])
            if has_vapost:
                add("vapostproc", ["vapostproc"])
            if has_vaapipost:
                add("vaapipostproc", ["vaapipostproc"])
            if has_glupload and has_glcolor:
                add("glupload+glcolorconvert", ["glupload", "glcolorconvert"])
            elif has_glupload:
                add("glupload", ["glupload"])
            if has_vapost and has_glupload and has_glcolor:
                add("vapostproc+glupload+glcolorconvert", ["vapostproc", "glupload", "glcolorconvert"])
            if interlaced and has_vadeint and has_videoconvert:
                add("vadeinterlace+videoconvert", ["vadeinterlace", "videoconvert"])
            if has_videoconvert:
                add("videoconvert", ["videoconvert"])

        elif feat_kind == "gl":
            if interlaced and has_deinterlace:
                add("deinterlace+glcolorconvert", ["deinterlace", "glcolorconvert"] if has_glcolor else ["deinterlace"])
            add("direct-gl", [])
            if has_glcolor:
                add("glcolorconvert", ["glcolorconvert"])
            if has_videoconvert:
                add("videoconvert", ["videoconvert"])

        else:
            if pref in ("gl", "glimagesink", "gtk4", "auto") and has_glupload:
                if has_glcolor:
                    add("glupload+glcolorconvert", ["glupload", "glcolorconvert"])
                add("glupload", ["glupload"])
            if interlaced:
                if has_deinterlace and has_videoconvert:
                    add("deinterlace+videoconvert", ["deinterlace", "videoconvert"])
                elif has_deinterlace:
                    add("deinterlace", ["deinterlace"])
            add("direct-system", [])
            if has_videoconvert:
                add("videoconvert", ["videoconvert"])

        if pref == "auto":
            return paths

        preferred_names = {
            "va-surface": (
                "vadeinterlace+vapostproc",
                "vadeinterlace",
                "direct-va/dmabuf",
                "vapostproc",
                "vaapipostproc",
                "direct-gl",
                "direct-system",
            ),
            "gtk4": (
                "direct-va/dmabuf",
                "vapostproc",
                "vaapipostproc",
                "glupload+glcolorconvert",
                "glupload",
                "direct-gl",
                "vadeinterlace+vapostproc",
            ),
            "vapostproc": (
                "vadeinterlace+vapostproc",
                "vapostproc",
                "vaapipostproc",
                "vadeinterlace",
                "direct-va/dmabuf",
                "glupload+glcolorconvert",
            ),
            "gl": (
                "glupload+glcolorconvert",
                "vapostproc+glupload+glcolorconvert",
                "glupload",
                "direct-gl",
                "glcolorconvert",
                "deinterlace+glcolorconvert",
                "direct-va/dmabuf",
            ),
            "glimagesink": (
                "glupload+glcolorconvert",
                "vapostproc+glupload+glcolorconvert",
                "glupload",
                "direct-gl",
                "glcolorconvert",
                "direct-va/dmabuf",
            ),
            "software": (
                "deinterlace+videoconvert",
                "deinterlace",
                "vadeinterlace+videoconvert",
                "videoconvert",
                "direct-system",
            ),
        }.get(pref, ())

        ordered: List[tuple] = []
        used = set()
        for name in preferred_names:
            for p in paths:
                if p[0] == name and p[0] not in used:
                    ordered.append(p)
                    used.add(p[0])
        for p in paths:
            if p[0] not in used:
                ordered.append(p)
        return ordered if ordered else paths

    def _link_video_branch(self, src_pad: Gst.Pad) -> None:
        caps = src_pad.get_current_caps() or src_pad.query_caps(None)
        if caps and not caps.get_structure(0).get_name().startswith("video/"):
            return
        assert self.pipeline is not None

        feat = self._caps_features(caps)
        feat_kind = self._feature_kind(feat)
        interlaced = self._caps_is_interlaced(caps)
        logger.info(
            "Video pad caps: %s  features=%s  kind=%s  interlaced=%s",
            caps.to_string() if caps else "?",
            feat,
            feat_kind,
            interlaced,
        )

        video_sink = self._make_inner_video_sink()
        if video_sink is None:
            logger.error("brak video sink")
            return

        candidates = self._build_path_candidates(feat_kind, interlaced)
        if not candidates:
            logger.error("Brak dostępnych ścieżek wyjścia wideo")
            return

        logger.info(
            "Kandydaci ścieżek wideo (pref=%s, kind=%s, interlaced=%s): %s",
            self._prefs.video_output if self._prefs else "auto",
            feat_kind,
            interlaced,
            [c[0] for c in candidates],
        )

        linked = False
        used_path = None
        for path_name, mid_names in candidates:
            mids = []
            ok = True
            for n in mid_names:
                el = _make(n, None)
                if el is None:
                    ok = False
                    break
                mids.append(el)
            if not ok:
                continue

            queue = _make("queue", None)
            if queue is None:
                continue
            self._configure_live_queue(queue)

            chain = [queue] + mids + [video_sink]
            for el in chain:
                parent = el.get_parent()
                if parent is not None:
                    parent.remove(el)
                self.pipeline.add(el)

            prev = queue
            chain_ok = True
            for el in mids + [video_sink]:
                if not prev.link(el):
                    chain_ok = False
                    logger.debug(
                        "link fail w ścieżce %s: %s -> %s",
                        path_name, prev.get_name(), el.get_name(),
                    )
                    break
                prev = el

            if not chain_ok:
                for el in [queue] + mids:
                    try:
                        self.pipeline.remove(el)
                    except Exception:
                        pass
                if video_sink.get_parent() is not None:
                    try:
                        self.pipeline.remove(video_sink)
                    except Exception:
                        pass
                continue

            for el in chain:
                el.sync_state_with_parent()

            sink_pad = queue.get_static_pad("sink")
            link_ret = src_pad.link(sink_pad)
            if link_ret != Gst.PadLinkReturn.OK:
                logger.debug(
                    "src_pad.link nieudany dla %s: %s", path_name, link_ret
                )
                for el in [queue] + mids:
                    try:
                        self.pipeline.remove(el)
                    except Exception:
                        pass
                if video_sink.get_parent() is not None:
                    try:
                        self.pipeline.remove(video_sink)
                    except Exception:
                        pass
                continue

            linked = True
            used_path = path_name
            break

        if not linked:
            logger.error("Nie udało się podpiąć video sink (żadna ścieżka)")
            return

        logger.info(
            "Video sink podpięty: path=%s features=%s kind=%s interlaced=%s (pref=%s)",
            used_path, feat, feat_kind, interlaced,
            self._prefs.video_output if self._prefs else "auto",
        )


    # ------------------------------------------------------------------ subtitles
    def _build_subtitle_branch(self, sub_type: str) -> Optional[GstApp.AppSrc]:
        assert self.pipeline is not None
        appsrc = _make("appsrc", "src_sub")
        if appsrc is None:
            return None
        caps_str = SUBTITLE_CAPS.get(sub_type, "subpicture/x-dvd")
        self._configure_appsrc(appsrc, caps_str)

        queue = _make("queue", "queue_sub")
        overlay = _make("dvbsuboverlay", "sub_overlay")
        if overlay is None:
            overlay = _make("dvdsuboverlay", "sub_overlay")
        if overlay is None:
            logger.warning(
                "Brak dvbsuboverlay/dvdsuboverlay – napisy DVBSUB niedostępne "
                "(zainstaluj gst-plugins-bad)"
            )
            return None

        if queue is None:
            return None

        elements = [appsrc, queue, overlay]
        for el in elements:
            self.pipeline.add(el)
            self._sub_elements.append(el)

        if not appsrc.link(queue) or not queue.link(overlay):
            logger.error("subtitle branch link failed")
            return None

        logger.info("Gałąź napisów DVBSUB zbudowana (overlay=%s)", overlay.get_factory().get_name())
        for el in elements:
            el.sync_state_with_parent()
        return appsrc

    # ------------------------------------------------------------------ audio explicit
    def _build_audio_explicit(
        self, audio_type: str, caps_str: Optional[str]
    ) -> Optional[GstApp.AppSrc]:
        assert self.pipeline is not None
        chains = AUDIO_DECODER_CHAIN.get(audio_type) or []
        chosen = None
        for parser_name, decoder_name in chains:
            decoder = _make(decoder_name)
            if decoder is None:
                continue
            parser = None
            if parser_name:
                parser = _make(parser_name)
                if parser is None:
                    continue
            chosen = (parser, decoder, parser_name, decoder_name)
            break

        if chosen is None:
            return None

        parser, decoder, parser_name, decoder_name = chosen
        logger.info(
            "Audio explicit: type=%s → %s%s",
            audio_type,
            f"{parser_name} ! " if parser_name else "",
            decoder_name,
        )
        if parser is not None and parser_name == "mpegaudioparse":
            for prop, val in (
                ("disable-passthrough", True),
            ):
                if parser.find_property(prop) is not None:
                    try:
                        parser.set_property(prop, val)
                    except Exception:
                        pass
        self.emit("decoder-chosen", decoder_name, "software")

        appsrc = _make("appsrc", "src_audio")
        if appsrc is None:
            return None
        self._configure_appsrc(appsrc, caps_str)

        queue_in = _make("queue", "queue_in_audio")
        queue_out = _make("queue", None)
        convert = _make("audioconvert", None)
        resample = _make("audioresample", None)
        volume = _make("volume", "audio_volume")
        audio_sink = _make("autoaudiosink", None)

        if None in (queue_in, queue_out, convert, resample, volume, audio_sink):
            logger.error("brak elementu w łańcuchu audio sink")
            return None

        self._configure_live_queue(queue_in)
        if queue_out is not None:
            self._configure_live_queue(queue_out)
        volume.set_property("volume", self._volume)
        volume.set_property("mute", self._muted)
        self._configure_live_sink(audio_sink, is_video=False)

        elements: List = [appsrc, queue_in]
        if parser is not None:
            elements.append(parser)
        elements.extend([decoder, queue_out, convert, resample, volume, audio_sink])

        for el in elements:
            self.pipeline.add(el)
            self._audio_elements.append(el)

        prev = appsrc
        for el in elements[1:]:
            if not prev.link(el):
                logger.error(
                    "Audio link failed: %s -> %s (type=%s)",
                    prev.get_name(), el.get_name(), audio_type,
                )
                return None
            prev = el

        for el in elements:
            el.sync_state_with_parent()

        return appsrc

    def _build_audio_decodebin(self, caps_str: Optional[str]) -> Optional[GstApp.AppSrc]:
        assert self.pipeline is not None
        appsrc = _make("appsrc", "src_audio")
        if appsrc is None:
            return None
        self._configure_appsrc(appsrc, caps_str)

        queue_in = _make("queue", "queue_in_audio")
        decodebin = _make("decodebin", "decode_audio")
        if queue_in is None or decodebin is None:
            return None
        self._configure_live_queue(queue_in)
        owner_pipeline = self.pipeline
        decodebin.connect(
            "pad-added",
            lambda db, pad: GLib.idle_add(self._idle_link_audio, pad, owner_pipeline),
        )
        decodebin.connect(
            "element-added",
            lambda db, el: GLib.idle_add(self._idle_decoder_added, el, owner_pipeline),
        )
        for el in (appsrc, queue_in, decodebin):
            self.pipeline.add(el)
            self._audio_elements.append(el)
        if not appsrc.link(queue_in) or not queue_in.link(decodebin):
            return None
        for el in (appsrc, queue_in, decodebin):
            el.sync_state_with_parent()
        return appsrc

    def _idle_link_audio(self, pad: Gst.Pad, owner_pipeline: Gst.Pipeline) -> bool:
        if owner_pipeline is not self.pipeline:
            return False
        try:
            self._link_audio_decodebin_pad(pad)
        except Exception:
            logger.exception("link audio decodebin")
        return False

    def _link_audio_decodebin_pad(self, src_pad: Gst.Pad) -> None:
        caps = src_pad.get_current_caps() or src_pad.query_caps(None)
        if caps and not caps.get_structure(0).get_name().startswith("audio/"):
            return
        assert self.pipeline is not None
        queue = _make("queue", None)
        convert = _make("audioconvert", None)
        resample = _make("audioresample", None)
        volume = _make("volume", "audio_volume")
        audio_sink = _make("autoaudiosink", None)
        if None in (queue, convert, resample, volume, audio_sink):
            return
        volume.set_property("volume", self._volume)
        volume.set_property("mute", self._muted)
        self._configure_live_queue(queue)
        self._configure_live_sink(audio_sink, is_video=False)
        for el in (queue, convert, resample, volume, audio_sink):
            self.pipeline.add(el)
            self._audio_elements.append(el)
            el.sync_state_with_parent()
        queue.link(convert)
        convert.link(resample)
        resample.link(volume)
        volume.link(audio_sink)
        sink_pad = queue.get_static_pad("sink")
        src_pad.link(sink_pad)
        logger.info("Audio sink (decodebin) podpięty")

    def _idle_decoder_added(self, element: Gst.Element, owner_pipeline: Gst.Pipeline) -> bool:
        if owner_pipeline is not self.pipeline:
            return False
        factory = element.get_factory()
        if not factory:
            return False
        fname = factory.get_name()
        if fname.startswith(("va", "vaapi")) and "dec" in fname:
            logger.info("Dekoder sprzętowy (VA-API): %s", fname)
            self.emit("decoder-chosen", fname, "vaapi")
        elif fname.startswith("avdec_") or fname in (
            "faad", "mad", "mpg123audiodec"
        ):
            logger.info("Dekoder: %s", fname)
            self.emit("decoder-chosen", fname, "software")
        return False

    def _start_watchdog(self) -> None:
        self._stop_watchdog()
        self._last_data_ts = time.monotonic()
        self._watchdog_id = GLib.timeout_add(200, self._watchdog_tick)

    def _stop_watchdog(self) -> None:
        if self._watchdog_id is not None:
            try:
                GLib.source_remove(self._watchdog_id)
            except Exception:
                pass
            self._watchdog_id = None

    def _watchdog_tick(self) -> bool:
        if self._http_mode:
            return True
        if not self.pipeline or not self._want_playing or not self._preroll_done:
            return True
        gap_s = time.monotonic() - self._last_data_ts
        if gap_s * 1000.0 >= _STALL_TIMEOUT_MS:
            self._start_rebuffer(gap_s)
        return True

    def _start_rebuffer(self, gap_s: float) -> None:
        if not self.pipeline or not self._want_playing:
            return
        logger.warning(
            "Brak danych ze strumienia przez %.2fs – ponowne buforowanie", gap_s
        )
        self._preroll_done = False
        self._preroll_first_pts_us = None
        self._preroll_wall_start = time.monotonic()
        self._active_preroll_us = self._rebuffer_us
        try:
            self.pipeline.set_state(Gst.State.PAUSED)
        except Exception:
            pass
        self.emit("state-changed", "buffering")

    def play(self) -> None:
        if not self.pipeline:
            return
        self._want_playing = True
        self._active_preroll_us = self._preroll_us
        try:
            self.pipeline.set_property("latency", int(_BUFFER_MS * Gst.MSECOND))
        except Exception:
            pass
        self._start_watchdog()
        if self._preroll_done:
            self._go_playing()
        else:
            self._preroll_wall_start = time.monotonic()
            self.pipeline.set_state(Gst.State.PAUSED)
            logger.info(
                "Preroll: zbieram dane przez ~%.1f s zanim wystartuje obraz",
                self._active_preroll_us / 1_000_000.0,
            )
            self.emit("state-changed", "buffering")

    def _go_playing(self) -> None:
        if not self.pipeline or not self._want_playing:
            return
        self._preroll_done = True
        self._tighten_queues_after_preroll()
        try:
            self.pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
        except Exception:
            pass
        try:
            self.pipeline.set_base_time(0)
        except Exception:
            pass
        self.pipeline.set_state(Gst.State.PLAYING)
        try:
            self.pipeline.set_latency(int(_BUFFER_MS * Gst.MSECOND))
        except Exception:
            pass
        logger.info("Preroll zakończony – PLAYING")
        self.emit("state-changed", "playing")
        return False

    def pause(self) -> None:
        if self.pipeline:
            self.pipeline.set_state(Gst.State.PAUSED)
            self.emit("state-changed", "paused")

    def _emit_paintable_from(self, gtk_sink: Gst.Element) -> None:
        self.video_sink = gtk_sink
        try:
            if gtk_sink.find_property("paintable") is None:
                return
            paintable = gtk_sink.get_property("paintable")
            self._paintable = paintable
            self.emit("paintable-ready", paintable)
        except Exception:
            logger.exception("paintable")

    def _make_inner_video_sink(self) -> Optional[Gst.Element]:
        """Wewnętrzny sink do osadzenia w Gtk.Picture.

        Zalecane: gtk4paintablesink (GdkPaintable, DMABuf/GLMemory).
        Fallback: glimagesink (osobne okno GL – bez paintable).
        """
        pref = (self._prefs.video_output if self._prefs else "auto") or "auto"
        gtk_sink = _make("gtk4paintablesink", "vsink")
        if gtk_sink is not None:
            self._configure_live_sink(gtk_sink, is_video=True)
            try:
                if gtk_sink.find_property("force-aspect-ratio") is not None:
                    gtk_sink.set_property("force-aspect-ratio", False)
            except Exception:
                pass
            self._emit_paintable_from(gtk_sink)
            logger.info(
                "Video sink inner: gtk4paintablesink (wayland=%s pref=%s)",
                _is_wayland(), pref,
            )
            return gtk_sink

        if pref in ("gl", "glimagesink", "auto"):
            glimg = _make("glimagesink", "vsink")
            if glimg is not None:
                self._configure_live_sink(glimg, is_video=True)
                try:
                    if glimg.find_property("force-aspect-ratio") is not None:
                        glimg.set_property("force-aspect-ratio", True)
                except Exception:
                    pass
                self.video_sink = glimg
                logger.warning(
                    "Brak gtk4paintablesink – glimagesink (osobne okno GL, "
                    "DMABuf zero-copy przez GstGLSinkBin)"
                )
                return glimg

        logger.warning("Brak gtk4paintablesink i glimagesink – autovideosink")
        auto = _make("autovideosink", "vsink") or _make("fakesink", "vsink")
        if auto is not None:
            self._configure_live_sink(auto, is_video=True)
            self.video_sink = auto
        return auto

    def _wrap_glsinkbin(self, inner: Gst.Element) -> Gst.Element:
        """glsinkbin wstawia glupload: DMABuf/VAMemory → GLMemory (EGL, zero-copy).

        To jest ta sama ścieżka, której używa glimagesink (glimagesink IS
        GstGLSinkBin). Na Waylandzie glupload importuje DMA-BUF przez
        eglCreateImageKHR(EGL_LINUX_DMA_BUF_EXT) – bez kopii przez CPU.
        """
        glbin = _make("glsinkbin", "glsinkbin")
        if glbin is None:
            logger.warning("Brak glsinkbin – sink bez automatycznego glupload")
            return inner
        try:
            glbin.set_property("sink", inner)
        except Exception:
            logger.exception("glsinkbin.sink")
            return inner
        try:
            if glbin.find_property("sync") is not None:
                glbin.set_property("sync", True)
            if glbin.find_property("qos") is not None:
                glbin.set_property("qos", True)
        except Exception:
            pass
        inner_name = inner.get_factory().get_name() if inner.get_factory() else "?"
        logger.info(
            "glsinkbin wrap: DMABuf/VAMemory → glupload → %s (wayland=%s, zero-copy)",
            inner_name, _is_wayland(),
        )
        return glbin

    def _make_bin_with_filters(
        self, name: str, filters: List[str], inner: Gst.Element
    ) -> Optional[Gst.Element]:
        """Bin: ghost_sink → filter… → inner. Filtry GPU (vapostproc/glupload)
        zachowują zero-copy; videoconvert – kopia CPU."""
        elements = []
        for i, fname in enumerate(filters):
            el = _make(fname, f"{name}_{fname}_{i}")
            if el is None:
                return None
            if fname == "vapostproc" and el.find_property("deinterlace-mode") is not None:
                try:
                    el.set_property("deinterlace-mode", "auto")
                except Exception:
                    pass
            elements.append(el)
        bin_el = Gst.Bin.new(name)
        for el in elements:
            bin_el.add(el)
        parent = inner.get_parent()
        if parent is not None:
            try:
                parent.remove(inner)
            except Exception:
                pass
        bin_el.add(inner)
        prev = elements[0]
        for el in elements[1:] + [inner]:
            if not prev.link(el):
                logger.warning("sink bin link fail: %s -> %s", prev.get_name(), el.get_name())
                return None
            prev = el
        sink_pad = elements[0].get_static_pad("sink")
        if sink_pad is None:
            return None
        ghost = Gst.GhostPad.new("sink", sink_pad)
        bin_el.add_pad(ghost)
        return bin_el

    def _make_playbin_video_sink(self) -> Optional[Gst.Element]:
        """Video-sink dla playbin – zero-copy na Wayland gdy wybrane z opcji.

        auto / gtk4 (zalecane):
            glsinkbin sink=gtk4paintablesink
            va*dec → DMABuf → glupload (EGL) → gtk4paintablesink → Gtk.Picture
        gl / glimagesink (zalecane):
            ta sama ścieżka GL (glimagesink to GstGLSinkBin); osadzamy
            gtk4paintablesink, żeby obraz został w oknie GTK4.
            Gdy brak gtk4paintablesink → prawdziwy glimagesink.
        va-surface:
            gtk4paintablesink bezpośrednio (GDK importuje DMABuf).
        vapostproc:
            vapostproc ! gtk4paintablesink  (konwersja po stronie GPU).
        software:
            videoconvert ! gtk4paintablesink  (kopia CPU, ostatnia deska).
        """
        pref = (self._prefs.video_output if self._prefs else "auto") or "auto"
        inner = self._make_inner_video_sink()
        if inner is None:
            return None

        inner_name = inner.get_factory().get_name() if inner.get_factory() else "?"
        wayland = _is_wayland()

        if pref == "software":
            bin_el = self._make_bin_with_filters(
                "vsink_copy", ["videoconvert"], inner
            )
            logger.info("video-sink: videoconvert ! %s (kopia CPU)", inner_name)
            return bin_el or inner

        if pref == "vapostproc":
            for filt in (["vapostproc"], ["vaapipostproc"]):
                bin_el = self._make_bin_with_filters("vsink_vapost", filt, inner)
                if bin_el is not None:
                    logger.info(
                        "video-sink: %s ! %s (GPU, zero-copy)", filt[0], inner_name
                    )
                    return bin_el
            logger.warning("Brak vapostproc/vaapipostproc – fallback glsinkbin")
            return self._wrap_glsinkbin(inner)

        if pref == "va-surface":
            logger.info(
                "video-sink: %s direct (VA/DMABuf → GDK, wayland=%s)",
                inner_name, wayland,
            )
            return inner

        if pref in ("gl", "glimagesink"):
            # glimagesink sam jest GstGLSinkBin. Gdy mamy gtk4paintablesink,
            # owijamy go w glsinkbin – identyczna ścieżka GL, obraz w GTK4.
            if inner_name == "glimagesink":
                logger.info("video-sink: glimagesink (GstGLSinkBin, DMABuf zero-copy)")
                return inner
            return self._wrap_glsinkbin(inner)

        # auto / gtk4: na Waylandzie glsinkbin, żeby VA DMABuf zawsze
        # wszedł przez EGL import. gtk4paintablesink sam też umie DMABuf,
        # ale glupload jest pewniejszy wobec VAMemory z vaapi*dec.
        if wayland or pref == "gtk4":
            return self._wrap_glsinkbin(inner)
        return inner

    def play_http_ts(self, uri: str) -> None:
        self.stop()
        pref = self._prefs.decoder_pref if self._prefs else "auto"
        _boost_decoder_ranks(pref)

        # UWAGA: cala logika scieżek audio/napisow ponizej (n-audio, n-text,
        # current-audio, current-text, get-*-tags, sygnaly *-tags-changed)
        # jest napisana pod klasyczne API playbin (playbin2). playbin3 tych
        # property/sygnalow w ogole nie ma (uzywa GstStreamCollection +
        # eventow select-streams), wiec przy playbin3 przelaczanie
        # audio/napisow po cichu nie dzialaloby (n-audio zawsze 0).
        # Dlatego celowo wymuszamy klasyczny playbin, a nie playbin3.
        playbin = _make("playbin", "tvh-playbin") or _make("playbin3", "tvh-playbin")
        if playbin is None:
            logger.error("Brak playbin/playbin3")
            return

        video_sink = self._make_playbin_video_sink()
        audio_sink = _make("autoaudiosink", "asink") or _make("fakesink", "asink")

        if video_sink is not None:
            try:
                playbin.set_property("video-sink", video_sink)
            except Exception:
                logger.exception("video-sink")
            try:
                if self.video_sink is not None and self.video_sink.find_property("paintable"):
                    paintable = self.video_sink.get_property("paintable")
                    self._paintable = paintable
                    self.emit("paintable-ready", paintable)
            except Exception:
                logger.exception("paintable")

        if audio_sink is not None:
            if audio_sink.find_property("sync") is not None:
                try:
                    audio_sink.set_property("sync", True)
                except Exception:
                    pass
            try:
                playbin.set_property("audio-sink", audio_sink)
            except Exception:
                pass

        playbin.set_property("uri", uri)
        try:
            subs_enabled = bool(self._prefs.subtitles_enabled) if self._prefs else True
        except Exception:
            subs_enabled = True
        self._subs_user_disabled = not subs_enabled
        try:
            flags = int(playbin.get_property("flags"))
            # VIDEO|AUDIO zawsze. TEXT (0x4) TYLKO gdy napisy sa wlaczone w
            # configu - kazda zmiana prefs.subtitles_enabled i tak robi
            # pelny restart streamu (patrz StreamController.select_subtitle_
            # track), wiec nie ma potrzeby trzymac TEXT wlaczonego "na
            # zapas" gdy user ma napisy wylaczone: bez tej flagi playbin w
            # ogole nie demuksuje/dekoduje sciezki napisow (oszczedza
            # CPU/pasmo), zamiast dekodowac i tylko chowac wynik przez
            # current-text=-1.
            new_flags = (flags | 0x1 | 0x2 | 0x4) if subs_enabled else ((flags | 0x1 | 0x2) & ~0x4)
            playbin.set_property("flags", new_flags)
        except Exception:
            logger.exception("nie udalo sie ustawic flags VIDEO|AUDIO|TEXT")

        self._pb_audio_tracks = []
        self._pb_sub_tracks = []
        self._pb_video_info = {}
        self._pb_audio_info = {}
        for sig, handler in (
            ("audio-tags-changed", self._on_pb_audio_tags_changed),
            ("text-tags-changed", self._on_pb_text_tags_changed),
            ("video-tags-changed", self._on_pb_video_tags_changed),
        ):
            try:
                playbin.connect(sig, handler)
            except Exception:
                logger.exception("nie udalo sie podpiac sygnalu %s", sig)
        try:
            if playbin.find_property("buffer-size") is not None:
                playbin.set_property("buffer-size", 2 * 1024 * 1024)
            if playbin.find_property("buffer-duration") is not None:
                playbin.set_property("buffer-duration", int(1.5 * Gst.SECOND))
        except Exception:
            pass

        self.pipeline = playbin
        self.appsrc_video = None
        self.appsrc_audio = None
        self.appsrc_subtitle = None
        self._http_mode = True
        self._want_playing = True
        self._preroll_done = True
        self._audio_auto_selected = False
        self._sub_auto_selected = False
        try:
            # Nic nie pokazuj dopoki nie podejmiemy swiadomej decyzji w
            # _rebuild_pb_text_tracks (wedlug prefs.preferred_sub_langs).
            playbin.set_property("current-text", -1)
        except Exception:
            pass
        self._apply_subtitle_font_pt()

        self._generation += 1
        gen = self._generation
        bus = playbin.get_bus()
        self._bus = bus
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message, gen)

        try:
            playbin.set_start_time(Gst.CLOCK_TIME_NONE)
        except Exception:
            pass

        ret = playbin.set_state(Gst.State.PLAYING)
        vout = (self._prefs.video_output if self._prefs else "auto") or "auto"
        logger.info(
            "HTTP TS playbin: uri=%s state=%s decoder=%s vsink=%s wayland=%s zerocopy=%s",
            uri.split("?")[0],
            ret,
            pref,
            vout,
            _is_wayland(),
            vout in _ZEROCOPY_OUTPUTS,
        )
        self.emit("state-changed", "playing")
        self._start_watchdog()
        GLib.timeout_add(1200, lambda: (self._log_pipeline_elements() or False))

    def stop(self) -> None:
        self._stop_watchdog()
        # Odetnij bus PRZED zatrzymaniem pipeline'u, zeby zaden spozniony
        # EOS/ERROR/STATE_CHANGED z tego pipeline'u nie mogl juz dotrzec do
        # _on_bus_message (dodatkowo strzezone generacja, ale to eliminuje
        # zrodlo calkowicie zamiast tylko je filtrowac).
        old_bus = getattr(self, "_bus", None)
        if old_bus is not None:
            try:
                old_bus.remove_signal_watch()
            except Exception:
                pass
            self._bus = None
        self._generation += 1
        pipeline = None
        with self._push_lock:
            if self.pipeline:
                pipeline = self.pipeline
                self.pipeline = None
                self.appsrc_video = None
                self.appsrc_audio = None
                self.appsrc_subtitle = None
                self.video_sink = None
                self._paintable = None
                self._base_pts_us = None
                self._reset_pts_warmup()
                self._preroll_first_pts_us = None
                self._preroll_wall_start = None
                self._preroll_done = False
                self._want_playing = False
                self._audio_elements = []
                self._sub_elements = []
        self._drain_pkt_queue()
        if pipeline is not None:
            self.emit("paintable-ready", None)
            pipeline.set_state(Gst.State.NULL)
            ret, _state, _pending = pipeline.get_state(2 * Gst.SECOND)
            if ret != Gst.StateChangeReturn.SUCCESS:
                logger.warning(
                    "Zatrzymanie pipeline'u nie zakonczylo sie w 2s (ret=%s)",
                    ret,
                )
        self.emit("state-changed", "stopped")

    def push_video_bytes(
        self,
        data: bytes,
        pts_us: Optional[int] = None,
        dts_us: Optional[int] = None,
        duration_us: Optional[int] = None,
    ) -> None:
        try:
            self._pkt_queue.put_nowait(("v", data, pts_us, dts_us, duration_us))
        except Exception:
            pass

    def push_audio_bytes(
        self,
        data: bytes,
        pts_us: Optional[int] = None,
        dts_us: Optional[int] = None,
        duration_us: Optional[int] = None,
    ) -> None:
        try:
            self._pkt_queue.put_nowait(("a", data, pts_us, dts_us, duration_us))
        except Exception:
            pass

    def push_subtitle_bytes(
        self,
        data: bytes,
        pts_us: Optional[int] = None,
        dts_us: Optional[int] = None,
        duration_us: Optional[int] = None,
    ) -> None:
        try:
            self._pkt_queue.put_nowait(("s", data, pts_us, dts_us, duration_us))
        except Exception:
            pass

    def _drain_pkt_queue(self) -> None:
        while True:
            try:
                self._pkt_queue.get_nowait()
            except queue.Empty:
                break

    def _feeder_loop(self) -> None:
        while not self._feeder_stop.is_set():
            try:
                item = self._pkt_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                break
            kind, data, pts_us, dts_us, duration_us = item
            if kind == "v":
                self._push(True, data, pts_us, dts_us, duration_us)
            elif kind == "a":
                self._push(False, data, pts_us, dts_us, duration_us)
            elif kind == "s":
                self._push_subtitle(data, pts_us, dts_us, duration_us)

    def _push_subtitle(
        self,
        data: bytes,
        pts_us: Optional[int] = None,
        dts_us: Optional[int] = None,
        duration_us: Optional[int] = None,
    ) -> None:
        with self._push_lock:
            appsrc = self.appsrc_subtitle
            if not appsrc:
                return
            buf = Gst.Buffer.new_wrapped(data)
            if pts_us is not None:
                if self._base_pts_us is None:
                    self._base_pts_us = pts_us
                base = self._base_pts_us
                rel = max(0, pts_us - base)
                buf.pts = rel * 1000
                if dts_us is not None:
                    buf.dts = max(0, dts_us - base) * 1000
                if duration_us is not None and duration_us > 0:
                    buf.duration = duration_us * 1000
            ret = appsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                logger.debug("push-buffer sub: %s", ret)

    def _reset_pts_warmup(self) -> None:
        self._pts_warmup_deadline = None
        self._pts_warmup_seen = {True: None, False: None}
        self._pts_warmup_buf = []

    def _push(
        self,
        is_video: bool,
        data: bytes,
        pts_us: Optional[int] = None,
        dts_us: Optional[int] = None,
        duration_us: Optional[int] = None,
    ) -> None:
        with self._push_lock:
            appsrc = self.appsrc_video if is_video else self.appsrc_audio
            if not appsrc:
                return
            self._last_data_ts = time.monotonic()

            if self._base_pts_us is None and pts_us is not None:
                now = time.monotonic()
                if self._pts_warmup_deadline is None:
                    self._pts_warmup_deadline = now + self._PTS_WARMUP_S
                if self._pts_warmup_seen.get(is_video) is None:
                    self._pts_warmup_seen[is_video] = pts_us
                self._pts_warmup_buf.append((is_video, data, pts_us, dts_us, duration_us))
                have_both = (
                    self._pts_warmup_seen[True] is not None
                    and self._pts_warmup_seen[False] is not None
                )
                if not (have_both or now >= self._pts_warmup_deadline):
                    return
                candidates = [v for v in self._pts_warmup_seen.values() if v is not None]
                self._base_pts_us = min(candidates)
                pending = self._pts_warmup_buf
                self._pts_warmup_buf = []
                self._pts_warmup_deadline = None
                for p_is_video, p_data, p_pts, p_dts, p_dur in pending:
                    self._push_buffer_now(p_is_video, p_data, p_pts, p_dts, p_dur)
                return

            self._push_buffer_now(is_video, data, pts_us, dts_us, duration_us)

    def _push_buffer_now(
        self,
        is_video: bool,
        data: bytes,
        pts_us: Optional[int],
        dts_us: Optional[int],
        duration_us: Optional[int],
    ) -> None:
        appsrc = self.appsrc_video if is_video else self.appsrc_audio
        if not appsrc:
            return
        buf = Gst.Buffer.new_wrapped(data)
        if pts_us is not None and self._base_pts_us is not None:
            base = self._base_pts_us
            gap_us = pts_us - base
            if gap_us > 2_000_000:
                self._base_pts_us = pts_us
                base = pts_us
                try:
                    buf.set_flags(Gst.BufferFlags.DISCONT)
                except Exception:
                    pass
            elif gap_us < -200_000:
                try:
                    buf.set_flags(Gst.BufferFlags.DISCONT)
                except Exception:
                    pass
            rel = max(0, pts_us - base)
            buf.pts = rel * 1000
            if dts_us is not None:
                buf.dts = max(0, dts_us - base) * 1000
            if duration_us is not None and duration_us > 0:
                buf.duration = duration_us * 1000
            if not self._preroll_done and self._want_playing:
                if self._preroll_first_pts_us is None:
                    self._preroll_first_pts_us = pts_us
                span = pts_us - self._preroll_first_pts_us
                wall_ok = False
                if self._preroll_wall_start is not None:
                    wall_ok = (time.monotonic() - self._preroll_wall_start) * 1_000_000 >= self._active_preroll_us
                if span >= self._active_preroll_us or wall_ok:
                    self._preroll_done = True
                    GLib.idle_add(self._go_playing)
        elif not self._preroll_done and self._want_playing and self._preroll_wall_start is not None:
            if (time.monotonic() - self._preroll_wall_start) * 1_000_000 >= self._active_preroll_us:
                self._preroll_done = True
                GLib.idle_add(self._go_playing)
        ret = appsrc.emit("push-buffer", buf)
        if ret != Gst.FlowReturn.OK:
            logger.debug("push-buffer: %s", ret)

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, value))
        if not self.pipeline:
            return
        if self._http_mode:
            # playbin3 ma wlasne wlasciwosci volume/mute - element
            # "audio_volume" istnieje tylko w starym pipeline appsrc.
            try:
                self.pipeline.set_property("volume", self._volume)
            except Exception:
                logger.exception("playbin volume")
            return
        vol = self.pipeline.get_by_name("audio_volume")
        if vol:
            vol.set_property("volume", self._volume)

    def set_mute(self, muted: bool) -> None:
        self._muted = muted
        if not self.pipeline:
            return
        if self._http_mode:
            try:
                self.pipeline.set_property("mute", muted)
            except Exception:
                logger.exception("playbin mute")
            return
        vol = self.pipeline.get_by_name("audio_volume")
        if vol:
            vol.set_property("mute", muted)

    @property
    def paintable(self):
        return self._paintable

    # ------------------------------------------------------------------ #
    # Sciezki audio/napisy - tryb playbin3/HTTP-TS
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pb_track_label(index: int, lang: str, codec: str) -> str:
        lang = (lang or "").strip()
        codec = (codec or "").strip()
        if lang and codec:
            return f"{lang.upper()} ({codec})"
        if lang:
            return lang.upper()
        if codec:
            return codec
        return f"#{index}"

    def _on_pb_audio_tags_changed(self, playbin, _stream_id) -> None:
        GLib.idle_add(self._rebuild_pb_audio_tracks, playbin)

    def _on_pb_text_tags_changed(self, playbin, _stream_id) -> None:
        GLib.idle_add(self._rebuild_pb_text_tracks, playbin)

    def _on_pb_video_tags_changed(self, playbin, _stream_id) -> None:
        GLib.idle_add(self._rebuild_pb_video_info, playbin)

    def _rebuild_pb_audio_tracks(self, playbin) -> bool:
        if playbin is not self.pipeline:
            return False
        try:
            n = int(playbin.get_property("n-audio"))
        except Exception:
            n = 0
        tracks = []
        for i in range(n):
            lang, codec = "", ""
            try:
                tags = playbin.emit("get-audio-tags", i)
            except Exception:
                tags = None
            if tags:
                ok, val = tags.get_string(Gst.TAG_LANGUAGE_CODE)
                if ok:
                    lang = val
                ok, val = tags.get_string(Gst.TAG_AUDIO_CODEC)
                if ok:
                    codec = val
            tracks.append(
                {
                    "index": i,
                    "language": lang,
                    "codec": codec,
                    "description": self._pb_track_label(i, lang, codec),
                }
            )
        self._pb_audio_tracks = tracks

        if not self._audio_auto_selected and tracks and self._prefs:
            best = min(
                tracks,
                key=lambda t: self._prefs.rank_language(
                    t.get("language"), self._prefs.preferred_audio_langs
                ),
            )
            self._audio_auto_selected = True
            try:
                cur = int(playbin.get_property("current-audio"))
            except Exception:
                cur = -1
            if cur != best["index"]:
                try:
                    playbin.set_property("current-audio", best["index"])
                except Exception:
                    logger.exception("current-audio")

        self.emit("tracks-changed")
        return False

    def _rebuild_pb_text_tracks(self, playbin) -> bool:
        if playbin is not self.pipeline:
            return False
        try:
            n = int(playbin.get_property("n-text"))
        except Exception:
            n = 0
        tracks = []
        for i in range(n):
            lang, codec = "", ""
            try:
                tags = playbin.emit("get-text-tags", i)
            except Exception:
                tags = None
            if tags:
                ok, val = tags.get_string(Gst.TAG_LANGUAGE_CODE)
                if ok:
                    lang = val
                ok, val = tags.get_string(Gst.TAG_SUBTITLE_CODEC)
                if ok:
                    codec = val
            tracks.append(
                {
                    "index": i,
                    "language": lang,
                    "codec": codec,
                    "description": self._pb_track_label(i, lang, codec),
                }
            )
        self._pb_sub_tracks = tracks

        if not self._sub_auto_selected and tracks and not self._subs_user_disabled:
            enabled = bool(self._prefs.subtitles_enabled) if self._prefs else True
            if enabled:
                prefs = self._prefs
                best = min(
                    tracks,
                    key=lambda t: prefs.rank_language(t.get("language"), prefs.preferred_sub_langs),
                )
                # rank_language zwraca 500 gdy jezyk nie pasuje do zadnej
                # preferencji - w takim wypadku nie wlaczamy napisow
                # automatycznie, uzytkownik wybierze recznie z menu.
                if prefs.rank_language(best.get("language"), prefs.preferred_sub_langs) < 500:
                    try:
                        playbin.set_property("current-text", best["index"])
                    except Exception:
                        logger.exception("current-text (auto)")
            self._sub_auto_selected = True

        self.emit("tracks-changed")
        return False

    def _rebuild_pb_video_info(self, playbin) -> bool:
        if playbin is not self.pipeline:
            return False
        info: dict = {}
        try:
            tags = playbin.emit("get-video-tags", 0)
        except Exception:
            tags = None
        if tags:
            ok, val = tags.get_string(Gst.TAG_VIDEO_CODEC)
            if ok:
                info["codec"] = val
            ok, val = tags.get_uint(Gst.TAG_BITRATE)
            if ok:
                info["bitrate"] = val
        self._pb_video_info = info
        self.emit("stream-info-changed")
        return False

    def get_audio_tracks(self) -> List[dict]:
        if self._http_mode:
            return list(self._pb_audio_tracks)
        return []

    def get_subtitle_tracks(self) -> List[dict]:
        if self._http_mode:
            return list(self._pb_sub_tracks)
        return []

    def get_current_audio_index(self) -> Optional[int]:
        if self._http_mode and self.pipeline:
            try:
                idx = int(self.pipeline.get_property("current-audio"))
            except Exception:
                return None
            return idx if idx >= 0 else None
        return None

    def get_current_subtitle_index(self) -> Optional[int]:
        if self._http_mode and self.pipeline:
            try:
                idx = int(self.pipeline.get_property("current-text"))
            except Exception:
                return None
            return idx if idx >= 0 else None
        return None

    def select_audio_track(self, index: int) -> bool:
        if not (self._http_mode and self.pipeline):
            return False
        self._audio_auto_selected = True
        try:
            before = int(self.pipeline.get_property("current-audio"))
        except Exception:
            before = None
        try:
            self.pipeline.set_property("current-audio", index)
        except Exception:
            logger.exception("select_audio_track")
            return False
        try:
            after = int(self.pipeline.get_property("current-audio"))
        except Exception:
            after = None
        logger.info(
            "select_audio_track: index=%s current-audio przed=%s po=%s (n-audio=%s)",
            index, before, after,
            self._try_get_int(self.pipeline, "n-audio"),
        )
        # UWAGA: wczesniej byl tu dodatkowy _nudge_stream_resync() (flushing
        # seek na calym pipeline) "na wszelki wypadek", zeby wymusic
        # odswiezenie po zmianie current-audio. Na zywym, nieseekowalnym
        # zrodle HTTP TS taki globalny flush okazal sie NIEBEZPIECZNY -
        # dokladnie ten sam mechanizm psul wczesniej przelaczanie napisow
        # (zacinal/zatrzymywal A/V), a teraz - gdy galaz napisow bywa
        # calkowicie wypieta z grafu (brak dvbsuboverlay/text-pada przy
        # wylaczonych napisach) - zaczal psuc rowniez przelaczanie audio.
        # Samo ustawienie current-audio na klasycznym playbin dziala live
        # bez potrzeby seeka, wiec usunieto to wywolanie.
        self.emit("tracks-changed")
        return True

    @staticmethod
    def _try_get_int(element, prop: str) -> Optional[int]:
        try:
            return int(element.get_property(prop))
        except Exception:
            return None

    def select_subtitle_track(self, index: Optional[int]) -> bool:
        if not (self._http_mode and self.pipeline):
            return False
        self._sub_auto_selected = True
        self._subs_user_disabled = index is None
        try:
            before = int(self.pipeline.get_property("current-text"))
        except Exception:
            before = None
        try:
            self.pipeline.set_property("current-text", -1 if index is None else index)
        except Exception:
            logger.exception("select_subtitle_track")
            return False
        try:
            after = int(self.pipeline.get_property("current-text"))
        except Exception:
            after = None
        logger.info(
            "select_subtitle_track: index=%s current-text przed=%s po=%s (n-text=%s)",
            index, before, after,
            self._try_get_int(self.pipeline, "n-text"),
        )
        if index is None:
            # current-text=-1 nie czysci ostatnio wyrenderowanej bitmapy
            # w dvbsuboverlay (DVB wysyla napisy tylko przy zmianie tresci).
            # NIE robimy tu globalnego flush/seek calego playbin - na
            # zywym, nieseekowalnym zrodle HTTP TS to zacinalo/zatrzymywalo
            # A/V. Zamiast tego czyscimy lokalnie tylko sam element overlay.
            self._clear_subtitle_overlay()
        self.emit("tracks-changed")
        return True

    def _clear_subtitle_overlay(self) -> None:
        """Lokalny flush sink-padu dvbsuboverlay/subtitleoverlay, zeby
        skasowac ostatnio narysowana bitmape napisow DVB - bez dotykania
        reszty pipeline'u (audio/wideo/pozycja), co na zywym HTTP TS
        potrafi zaciac odtwarzanie (patrz select_subtitle_track)."""
        if not self.pipeline:
            return
        try:
            it = self.pipeline.iterate_recurse()
        except Exception:
            return
        elements = []
        while True:
            res, el = it.next()
            if res == Gst.IteratorResult.DONE:
                break
            if res == Gst.IteratorResult.ERROR:
                break
            if res == Gst.IteratorResult.RESYNC:
                it.resync()
                elements = []
                continue
            if el is not None:
                elements.append(el)
        for el in elements:
            try:
                factory = el.get_factory()
                fname = factory.get_name() if factory else ""
            except Exception:
                fname = ""
            if fname not in ("dvbsuboverlay", "dvdsuboverlay", "subtitleoverlay"):
                continue
            for pad in el.sinkpads:
                try:
                    pad.send_event(Gst.Event.new_flush_start())
                    pad.send_event(Gst.Event.new_flush_stop(True))
                except Exception:
                    logger.exception("nie udalo sie wyczyscic %s", fname)

    def _log_pipeline_elements(self) -> None:
        if not self.pipeline:
            return
        found = []
        hw_dec = None
        sw_dec = None
        has_videoconvert = False
        has_glupload = False
        has_vapost = False
        has_gtk4 = False
        has_glimg = False
        has_glsinkbin = False

        def _walk(el, prefix=""):
            nonlocal hw_dec, sw_dec, has_videoconvert, has_glupload
            nonlocal has_vapost, has_gtk4, has_glimg, has_glsinkbin
            name = el.get_name()
            factory = el.get_factory()
            fname = factory.get_name() if factory else type(el).__name__
            low = (fname + " " + name).lower()
            interesting = any(
                k in low
                for k in (
                    "dec", "parse", "demux", "sink", "src", "convert",
                    "va", "avdec", "tsdemux", "soup", "queue", "paint",
                    "glupload", "glcolor", "glimage", "glsink",
                )
            )
            if interesting:
                found.append(f"{prefix}{fname}:{name}")
            fl = fname.lower()
            if fl.startswith(("va", "vaapi")) and "dec" in fl:
                hw_dec = fname
            elif fl.startswith("avdec_") or fl in ("openh264dec", "libde265dec", "jpegdec"):
                if "h264" in fl or "h265" in fl or "mpeg" in fl or "vp" in fl or "jpeg" in fl or "vc1" in fl or "wmv" in fl or "av1" in fl:
                    sw_dec = fname
            if fl == "videoconvert":
                has_videoconvert = True
            if fl == "glupload":
                has_glupload = True
            if fl in ("vapostproc", "vaapipostproc"):
                has_vapost = True
            if fl == "gtk4paintablesink":
                has_gtk4 = True
            if fl == "glimagesink":
                has_glimg = True
            if fl == "glsinkbin":
                has_glsinkbin = True
            if isinstance(el, Gst.Bin):
                it = el.iterate_elements()
                while True:
                    ok, child = it.next()
                    if ok != Gst.IteratorResult.OK:
                        break
                    _walk(child, prefix + "  ")

        try:
            _walk(self.pipeline)
        except Exception:
            logger.exception("pipeline walk")
        if found:
            logger.info("Pipeline elements:\n  %s", "\n  ".join(found))
        else:
            logger.info("Pipeline elements: (brak / jeszcze nie zlinkowane)")

        zerocopy = bool(hw_dec) and not has_videoconvert and (
            has_glupload or has_vapost or has_glsinkbin or has_gtk4 or has_glimg
        )
        logger.info(
            "Pipeline path: hw_dec=%s sw_dec=%s gtk4=%s glimagesink=%s "
            "glsinkbin=%s glupload=%s vapostproc=%s videoconvert=%s "
            "zerocopy=%s wayland=%s",
            hw_dec, sw_dec, has_gtk4, has_glimg, has_glsinkbin,
            has_glupload, has_vapost, has_videoconvert,
            zerocopy, _is_wayland(),
        )
        if hw_dec:
            self.emit("decoder-chosen", hw_dec, "vaapi")
        elif sw_dec:
            self.emit("decoder-chosen", sw_dec, "software")

    def _on_bus_message(self, _bus, message: Gst.Message, gen: int) -> None:
        # Bus starego pipeline'u nie jest odlaczany synchronicznie ze stop()
        # (patrz komentarz w __init__), wiec przy szybkiej zmianie kanalu
        # potrafi jeszcze wyemitowac spozniony EOS/ERROR/STATE_CHANGED z
        # nieaktualnej juz generacji - to nadpisywalo etykiety nowego
        # kanalu (losowo znikajaca nazwa/OSD). Odrzucamy wszystko co nie
        # nalezy do biezacej generacji, zanim zrobimy cokolwiek innego.
        if gen != self._generation:
            return
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error("GStreamer error: %s (%s)", err, debug)
            self.emit("error", str(err))
        elif t == Gst.MessageType.EOS:
            self.emit("eos")
        elif t == Gst.MessageType.ASYNC_DONE:
            logger.info("GStreamer ASYNC_DONE – pipeline gotowy")
            try:
                self._log_pipeline_elements()
            except Exception:
                logger.exception("log elements")
            try:
                if self.video_sink is not None and self.video_sink.find_property("paintable"):
                    paintable = self.video_sink.get_property("paintable")
                    if paintable is not None:
                        self._paintable = paintable
                        self.emit("paintable-ready", paintable)
                        logger.info("paintable re-emitted after ASYNC_DONE")
            except Exception:
                logger.exception("paintable re-emit")
        elif t == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                _old, new, _pend = message.parse_state_changed()
                if new == Gst.State.PLAYING and getattr(self, "_http_mode", False):
                    GLib.timeout_add(800, lambda: (self._log_pipeline_elements() or False))
        elif t == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            msg = str(err)
            if "more decoded frames" in msg or "CONTINUITY" in msg:
                logger.debug("GStreamer warning: %s", err)
            else:
                logger.warning("GStreamer warning: %s (%s)", err, debug)
