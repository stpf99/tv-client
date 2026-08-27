from __future__ import annotations

import json
import locale
import uuid
from pathlib import Path
from typing import List, Optional

from gi.repository import GLib


class ServerConfig:
    def __init__(
        self,
        host: str = "",
        htsp_port: int = 9982,
        http_port: int = 9981,
        username: str = "",
        password: str = "",
        server_id: str = "",
        name: str = "",
    ):
        self.host = host
        self.htsp_port = htsp_port
        self.http_port = http_port
        self.username = username
        self.password = password
        # server_id: klucz stabilny do identyfikacji wpisu na liście
        # serwerów (patrz list_servers/save_server niżej) - niezależny od
        # host/port, żeby zmiana adresu nie tworzyła nowego wpisu.
        self.server_id = server_id or uuid.uuid4().hex[:12]
        self.name = name or host

    def to_dict(self) -> dict:
        return {
            "server_id": self.server_id,
            "name": self.name,
            "host": self.host,
            "htsp_port": self.htsp_port,
            "http_port": self.http_port,
            "username": self.username,
            "password": self.password,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ServerConfig":
        return cls(
            host=d.get("host", ""),
            htsp_port=d.get("htsp_port", 9982),
            http_port=d.get("http_port", 9981),
            username=d.get("username", ""),
            password=d.get("password", ""),
            server_id=d.get("server_id", ""),
            name=d.get("name", ""),
        )


def _system_lang_codes() -> List[str]:
    """Zwraca listę kodów językowych z locale systemowego (np. ['pl', 'en'])."""
    codes: List[str] = []
    try:
        loc = locale.getlocale(locale.LC_MESSAGES)[0] or locale.getdefaultlocale()[0]
        if loc:
            # pl_PL.UTF-8 -> pl, en_US -> en
            primary = loc.split("_")[0].lower()
            if primary and primary not in codes:
                codes.append(primary)
            # pełny kod (pl_PL) też przydatny dla dopasowań
            full = loc.split(".")[0].replace("-", "_").lower()
            if full and full not in codes:
                codes.append(full)
    except Exception:
        pass
    if not codes:
        codes = ["en"]
    return codes


class PlayerPreferences:
    """Preferencje odtwarzacza: dekodery, wyjście wideo, języki audio/napisów."""

    # decoder_pref: "auto" | "hw" | "sw"
    # video_output: "auto" | "gtk4" | "glimagesink" | "va-surface" | "vapostproc" | "gl" | "software"
    _VIDEO_OUTPUTS = (
        "auto", "gtk4", "glimagesink", "va-surface", "vapostproc", "gl", "software",
    )

    def __init__(
        self,
        decoder_pref: str = "auto",
        video_output: str = "auto",
        preferred_audio_langs: Optional[List[str]] = None,
        preferred_sub_langs: Optional[List[str]] = None,
        subtitles_enabled: bool = True,
        subtitle_font_pt: int = 20,
        osd_subtitle_font_pt: int = 16,
        osd_desc_autoscroll: bool = False,
        osd_desc_scroll_direction: str = "down",
        osd_top_bar_font_pt: int = 20,
    ):
        self.decoder_pref = decoder_pref if decoder_pref in ("auto", "hw", "sw") else "auto"
        self.video_output = (
            video_output if video_output in self._VIDEO_OUTPUTS else "auto"
        )
        sys_langs = _system_lang_codes()
        self.preferred_audio_langs = list(preferred_audio_langs) if preferred_audio_langs else list(sys_langs)
        self.preferred_sub_langs = list(preferred_sub_langs) if preferred_sub_langs else list(sys_langs)
        self.subtitles_enabled = bool(subtitles_enabled)
        # Rozmiar czcionki napisow (pt) przekazywany do playbin3
        # "subtitle-font-desc" (patrz GstPlayer.set_subtitle_font_pt).
        # Dotyczy tylko napisow tekstowych (TEXTSUB/teletekst) - DVBSUB to
        # bitmapa i nie skaluje sie przez font.
        self.subtitle_font_pt = int(subtitle_font_pt) if subtitle_font_pt else 20
        # Odrebne ustawienia dla warstwy OSD (nakladka GTK, nie dekoder):
        # rozmiar czcionki opisu audycji w dolnej belce OSD, oraz
        # automatyczne przewijanie dlugiego opisu (i jego kierunek), gdy
        # tekst nie miesci sie w widocznym obszarze.
        self.osd_subtitle_font_pt = int(osd_subtitle_font_pt) if osd_subtitle_font_pt else 16
        self.osd_desc_autoscroll = bool(osd_desc_autoscroll)
        self.osd_desc_scroll_direction = (
            osd_desc_scroll_direction if osd_desc_scroll_direction in ("up", "down") else "down"
        )
        # Rozmiar czcionki gornego paska OSD (nazwa kanalu + zegar) -
        # domyslny title-2 z libadwaita bywa za maly na duzym ekranie/TV.
        self.osd_top_bar_font_pt = int(osd_top_bar_font_pt) if osd_top_bar_font_pt else 20

    def to_dict(self) -> dict:
        return {
            "decoder_pref": self.decoder_pref,
            "video_output": self.video_output,
            "preferred_audio_langs": self.preferred_audio_langs,
            "preferred_sub_langs": self.preferred_sub_langs,
            "subtitles_enabled": self.subtitles_enabled,
            "subtitle_font_pt": self.subtitle_font_pt,
            "osd_subtitle_font_pt": self.osd_subtitle_font_pt,
            "osd_desc_autoscroll": self.osd_desc_autoscroll,
            "osd_desc_scroll_direction": self.osd_desc_scroll_direction,
            "osd_top_bar_font_pt": self.osd_top_bar_font_pt,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlayerPreferences":
        return cls(
            decoder_pref=d.get("decoder_pref", "auto"),
            video_output=d.get("video_output", "auto"),
            preferred_audio_langs=d.get("preferred_audio_langs"),
            preferred_sub_langs=d.get("preferred_sub_langs"),
            subtitles_enabled=d.get("subtitles_enabled", True),
            subtitle_font_pt=d.get("subtitle_font_pt", 20),
            osd_subtitle_font_pt=d.get("osd_subtitle_font_pt", 16),
            osd_desc_autoscroll=d.get("osd_desc_autoscroll", False),
            osd_desc_scroll_direction=d.get("osd_desc_scroll_direction", "down"),
            osd_top_bar_font_pt=d.get("osd_top_bar_font_pt", 20),
        )

    def rank_language(self, lang: Optional[str], preferred: List[str]) -> int:
        """Im niższa liczba, tym wyższa preferencja. Brak języka = najgorszy."""
        if not lang:
            return 1000
        lang = lang.lower().replace("-", "_")
        for i, pref in enumerate(preferred):
            p = pref.lower().replace("-", "_")
            if lang == p or lang.startswith(p + "_") or p.startswith(lang + "_"):
                return i
            if lang[:2] == p[:2]:
                return i + 50
        return 500


def _config_path() -> Path:
    cfg_dir = Path(GLib.get_user_config_dir()) / "tvh-gnome-client"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    return cfg_dir / "config.json"


def _read_raw() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_raw(data: dict) -> None:
    _config_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def _migrate_legacy_server(data: dict) -> dict:
    """Stare config.json (sprzed obsługi wielu serwerów) trzymało pojedyncze
    pola host/htsp_port/... na najwyższym poziomie. Migrujemy je jednorazowo
    do data['servers'] przy pierwszym odczycie, bez utraty ustawień."""
    if "servers" in data:
        return data
    if not data.get("host"):
        return data
    legacy = ServerConfig.from_dict(data)
    data["servers"] = [legacy.to_dict()]
    data["active_server_id"] = legacy.server_id
    for k in ("host", "htsp_port", "http_port", "username", "password"):
        data.pop(k, None)
    return data


# ------------------------------------------------------------------ #
# Wiele serwerów
# ------------------------------------------------------------------ #
def list_servers() -> List[ServerConfig]:
    data = _migrate_legacy_server(_read_raw())
    return [ServerConfig.from_dict(d) for d in data.get("servers", [])]


def get_active_server_id() -> Optional[str]:
    data = _migrate_legacy_server(_read_raw())
    return data.get("active_server_id")


def get_active_server() -> Optional[ServerConfig]:
    servers = list_servers()
    if not servers:
        return None
    active_id = get_active_server_id()
    for s in servers:
        if s.server_id == active_id:
            return s
    return servers[0]


def set_active_server_id(server_id: str) -> None:
    data = _migrate_legacy_server(_read_raw())
    data["active_server_id"] = server_id
    _write_raw(data)


def upsert_server(cfg: ServerConfig, make_active: bool = True) -> None:
    """Dodaje nowy serwer albo aktualizuje istniejący (po server_id)."""
    data = _migrate_legacy_server(_read_raw())
    servers = data.get("servers", [])
    for i, d in enumerate(servers):
        if d.get("server_id") == cfg.server_id:
            servers[i] = cfg.to_dict()
            break
    else:
        servers.append(cfg.to_dict())
    data["servers"] = servers
    if make_active or "active_server_id" not in data:
        data["active_server_id"] = cfg.server_id
    _write_raw(data)


def remove_server(server_id: str) -> None:
    data = _migrate_legacy_server(_read_raw())
    servers = [d for d in data.get("servers", []) if d.get("server_id") != server_id]
    data["servers"] = servers
    if data.get("active_server_id") == server_id:
        data["active_server_id"] = servers[0]["server_id"] if servers else None
    _write_raw(data)


# ------------------------------------------------------------------ #
# Kompatybilność wsteczna - jeden "aktywny" serwer, jak dawniej
# ------------------------------------------------------------------ #
def load_config() -> Optional[ServerConfig]:
    return get_active_server()


def save_config(cfg: ServerConfig) -> None:
    upsert_server(cfg, make_active=True)


# ------------------------------------------------------------------ #
# Ulubione - osobne listy dla TV i radia, kluczowane po channel_id
# ------------------------------------------------------------------ #
def load_favorites() -> dict:
    data = _read_raw()
    favs = data.get("favorites") or {}
    return {
        "tv": set(favs.get("tv", [])),
        "radio": set(favs.get("radio", [])),
    }


def _save_favorites(favs: dict) -> None:
    data = _migrate_legacy_server(_read_raw())
    data["favorites"] = {
        "tv": sorted(favs.get("tv", set())),
        "radio": sorted(favs.get("radio", set())),
    }
    _write_raw(data)


def is_favorite(channel_id: int, is_radio: bool) -> bool:
    favs = load_favorites()
    return channel_id in favs["radio" if is_radio else "tv"]


def toggle_favorite(channel_id: int, is_radio: bool) -> bool:
    """Przełącza ulubiony i zwraca nowy stan (True = jest ulubiony)."""
    favs = load_favorites()
    bucket = favs["radio" if is_radio else "tv"]
    if channel_id in bucket:
        bucket.discard(channel_id)
        new_state = False
    else:
        bucket.add(channel_id)
        new_state = True
    _save_favorites(favs)
    return new_state


def load_player_prefs() -> PlayerPreferences:
    path = _config_path()
    if not path.exists():
        return PlayerPreferences()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PlayerPreferences.from_dict(data.get("player", data))
    except Exception:
        return PlayerPreferences()


def save_player_prefs(prefs: PlayerPreferences) -> None:
    path = _config_path()
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing["player"] = prefs.to_dict()
    path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
