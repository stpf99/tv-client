"""Lekkie modele danych odzwierciedlajace encje HTSP."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Channel:
    channel_id: int
    name: str = ""
    number: int = 0
    icon_url: Optional[str] = None
    tag_ids: List[int] = field(default_factory=list)
    is_radio: bool = False
    current_event_id: Optional[int] = None
    next_event_id: Optional[int] = None

    @classmethod
    def from_htsp(cls, m: dict) -> "Channel":
        # Tvheadend nie ma jawnej flagi "radio" - heurystyka: dopasowujemy
        # kanal do tagow typu "Radio" (patrz TvhLibrary._resolve_radio_flags).
        # "channelType" nie jest standardowym polem HTSP - zostawiamy jako
        # dodatkowa furtke na wypadek serwerow/patchy ktore je wysylaja.
        is_radio = m.get("channelType") == "radio"
        # eventId / nextEventId – serwer sam wskazuje aktualny i następny program
        cur = m.get("eventId")
        nxt = m.get("nextEventId")

        def _uid(v, default=0) -> int:
            if v is None:
                return default
            try:
                n = int(v)
            except (TypeError, ValueError):
                return default
            if n < 0:
                n = n & 0xFFFFFFFF
            return n

        # Numer LCN zwykle 1–9999; ujemne/huge = zły signed decode → spróbuj u16
        raw_num = m.get("channelNumber", 0) or 0
        try:
            number = int(raw_num)
        except (TypeError, ValueError):
            number = 0
        if number < 0:
            number = number & 0xFFFF
        if number > 99999:
            number = 0

        return cls(
            channel_id=_uid(m.get("channelId"), 0),
            name=m.get("channelName", "") or m.get("name", ""),
            number=number,
            icon_url=m.get("channelIcon"),
            # UWAGA: pole HTSP dla tagow kanalu nazywa sie "tags" (lista
            # tagId-ow), NIE "channelTags" - zla nazwa pola powodowala ze
            # tag_ids bylo zawsze puste, co psulo zarowno heurystyke radio/TV
            # jak i filtrowanie po tagach (SD/HD/Radio/...). "channelTags"
            # zostaje jako fallback dla ewentualnych starszych/patchowanych
            # serwerow.
            tag_ids=list(m.get("tags") or m.get("channelTags") or []),
            is_radio=is_radio,
            current_event_id=int(cur) if cur else None,
            next_event_id=int(nxt) if nxt else None,
        )


@dataclass
class ChannelTag:
    tag_id: int
    name: str = ""
    icon_url: Optional[str] = None

    @classmethod
    def from_htsp(cls, m: dict) -> "ChannelTag":
        return cls(
            tag_id=m.get("tagId", 0),
            name=m.get("tagName", ""),
            icon_url=m.get("tagIcon"),
        )


@dataclass
class EpgEvent:
    event_id: int
    channel_id: int
    start: int = 0
    stop: int = 0
    title: str = ""
    subtitle: str = ""
    description: str = ""
    next_event_id: Optional[int] = None
    # pola pomocnicze do PVR / serii
    series_link_id: Optional[int] = None
    episode_id: Optional[str] = None
    # DVB content type (ETSI EN 300 468 tabela 28, "content_nibble_level_1"
    # w gornym nibble'u bajtu) - kategoria gatunku audycji (Film/Serial,
    # Rozrywka, Sport, itd.) uzywana do kolorowania siatki EPG i filtra
    # gatunkow. None gdy serwer nie przyslal.
    content_type: Optional[int] = None

    @classmethod
    def from_htsp(cls, m: dict) -> "EpgEvent":
        title = m.get("title", "")
        if isinstance(title, dict):
            title = title.get("eng") or next(iter(title.values()), "") or ""
        subtitle = m.get("subtitle", "")
        if isinstance(subtitle, dict):
            subtitle = subtitle.get("eng") or next(iter(subtitle.values()), "") or ""
        description = m.get("description", "") or m.get("summary", "")
        if isinstance(description, dict):
            description = description.get("eng") or next(iter(description.values()), "") or ""

        # HTSP: start/stop = UNIX time (sekundy od epoch, UTC).
        def _ts(v) -> int:
            if v is None:
                return 0
            try:
                n = int(v)
            except (TypeError, ValueError):
                return 0
            if n < 0:
                n = n & 0xFFFFFFFFFFFFFFFF  # u64 wrap
            return n

        def _norm_unix(ts: int) -> int:
            """Sprowadź do sekund unix. Obsłuż ms / µs."""
            if ts <= 0:
                return 0
            # µs (ok. 1.7e15 dla roku 2026)
            if ts > 10_000_000_000_000:  # > ~rok 2286 w ms
                return ts // 1_000_000
            # ms (ok. 1.7e12)
            if ts > 10_000_000_000:  # > ~rok 2286 w sekundach
                return ts // 1000
            return ts

        start = _norm_unix(_ts(m.get("start")))
        stop = _norm_unix(_ts(m.get("stop")))
        # odwrócone start/stop (zepsute dane) – nie ufamy
        if start and stop and stop < start:
            start, stop = 0, 0

        nxt = m.get("nextEventId")
        ct = m.get("contentType")
        return cls(
            event_id=_ts(m.get("eventId")),
            channel_id=_ts(m.get("channelId")),
            start=start,
            stop=stop,
            title=str(title or ""),
            subtitle=str(subtitle or ""),
            description=str(description or ""),
            next_event_id=int(nxt) if nxt else None,
            series_link_id=m.get("seriesLinkId") or m.get("serieslinkId"),
            episode_id=m.get("episodeId") or m.get("episodeNumber"),
            content_type=int(ct) if ct is not None else None,
        )


@dataclass
class Recording:
    entry_id: int
    channel_id: int = 0
    channel_name: str = ""
    title: str = ""
    subtitle: str = ""
    description: str = ""
    start: int = 0
    stop: int = 0
    state: str = ""  # scheduled/recording/completed/missed/invalid
    path: str = ""  # sciezka / nazwa pliku na serwerze (jesli znana)
    filesize: int = 0
    error: str = ""
    autorec_id: Optional[int] = None
    event_id: Optional[int] = None

    @classmethod
    def from_htsp(cls, m: dict) -> "Recording":
        title = m.get("title", "")
        if isinstance(title, dict):
            title = title.get("eng") or next(iter(title.values()), "") or ""
        subtitle = m.get("subtitle", "")
        if isinstance(subtitle, dict):
            subtitle = subtitle.get("eng") or next(iter(subtitle.values()), "") or ""
        description = m.get("description", "") or m.get("summary", "")
        if isinstance(description, dict):
            description = description.get("eng") or next(iter(description.values()), "") or ""

        # path/filename – HTSP bywa niespojne (path, filename, files[].filename)
        path = m.get("path") or m.get("filename") or ""
        if not path and isinstance(m.get("files"), list) and m["files"]:
            first = m["files"][0]
            if isinstance(first, dict):
                path = first.get("filename") or first.get("path") or ""
            elif isinstance(first, str):
                path = first
        filesize = int(m.get("filesize") or m.get("size") or 0)
        if not filesize and isinstance(m.get("files"), list) and m["files"]:
            first = m["files"][0]
            if isinstance(first, dict):
                filesize = int(first.get("size") or first.get("filesize") or 0)

        return cls(
            entry_id=m.get("id", 0),
            channel_id=m.get("channel", 0) or m.get("channelId", 0),
            channel_name=m.get("channelname") or m.get("channelName") or "",
            title=str(title or ""),
            subtitle=str(subtitle or ""),
            description=str(description or ""),
            start=m.get("start", 0),
            stop=m.get("stop", 0),
            state=m.get("state", "") or "",
            path=str(path or ""),
            filesize=filesize,
            error=str(m.get("error") or m.get("errorcode") or ""),
            autorec_id=m.get("autorecId") or m.get("autorec"),
            event_id=m.get("eventId"),
        )


@dataclass
class DvrConfig:
    uuid: str
    name: str = ""
