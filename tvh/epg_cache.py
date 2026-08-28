"""Dyskowy cache kanałów i EPG (XDG_CACHE_HOME), zeby przy starcie od razu
pokazac ostatnio znane dane zamiast czekac az caly initial sync z serwera
sie skonczy - synchronizacja z serwerem leci dalej normalnie w tle i na
biezaco nadpisuje/uzupelnia to co pokazane z cache. Wlaczane/wylaczane w
preferencjach (PlayerPreferences.epg_cache_enabled).

Nie cache'ujemy nagran (Recording) - ich stan (recording/completed/error)
musi byc zawsze swiezy z serwera, cache'owanie zwiekszaloby ryzyko
pokazania nieaktualnego stanu bez realnej korzysci w czasie startu (lista
nagran jest zazwyczaj krotsza niz pelna baza EPG).
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Tuple

from gi.repository import GLib

from tvh.models import Channel, EpgEvent

logger = logging.getLogger("tvh.epg_cache")

_CACHE_DIR = Path(GLib.get_user_cache_dir()) / "tvh-gnome-client"
_CACHE_FILE = _CACHE_DIR / "epg_cache.json"

# Cache starszy niz to nie jest uzywany do wstepnego wyswietlenia (zbyt
# nieaktualny, lepiej pokazac pusty widok podczas normalnej synchronizacji
# niz myslace dane sprzed tygodnia).
MAX_CACHE_AGE_S = 24 * 3600


def _server_key(host: str, port: int) -> str:
    return f"{host}:{port}"


def save(host: str, port: int, channels: List[Channel], events_by_channel: dict) -> None:
    """Zapisuje kanaly i EPG do cache. Best-effort - blad zapisu tylko
    logujemy, nigdy nie przerywa dzialania aplikacji."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "server": _server_key(host, port),
            "saved_at": int(time.time()),
            "channels": [asdict(ch) for ch in channels],
            "events": [
                asdict(ev)
                for evs in events_by_channel.values()
                for ev in evs
            ],
        }
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_CACHE_FILE)
        logger.info(
            "Zapisano cache EPG: %d kanałów, %d zdarzeń",
            len(payload["channels"]), len(payload["events"]),
        )
    except Exception:
        logger.exception("Nie udało się zapisać cache EPG")


def load(host: str, port: int) -> Optional[Tuple[List[Channel], List[EpgEvent]]]:
    """Wczytuje cache dla danego serwera, jesli istnieje i nie jest zbyt
    stary. Zwraca (channels, events) albo None."""
    if not _CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Nie udało się odczytać cache EPG")
        return None

    if payload.get("server") != _server_key(host, port):
        return None
    saved_at = payload.get("saved_at", 0)
    if int(time.time()) - saved_at > MAX_CACHE_AGE_S:
        logger.info("Cache EPG zbyt stary (>%ds) - pomijam", MAX_CACHE_AGE_S)
        return None

    try:
        channels = [Channel(**c) for c in payload.get("channels", [])]
        events = [EpgEvent(**e) for e in payload.get("events", [])]
    except TypeError:
        logger.exception("Cache EPG ma nieznany/niezgodny format - ignoruję")
        return None

    return channels, events


def clear() -> None:
    try:
        if _CACHE_FILE.exists():
            _CACHE_FILE.unlink()
    except Exception:
        logger.exception("Nie udało się usunąć cache EPG")
