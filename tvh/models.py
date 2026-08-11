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
        return cls(
            channel_id=m.get("channelId", 0),
            name=m.get("channelName", "") or m.get("name", ""),
            number=m.get("channelNumber", 0),
            icon_url=m.get("channelIcon"),
            # UWAGA: pole HTSP dla tagow kanalu nazywa sie "tags" (lista
            # tagId-ow), NIE "channelTags" - zla nazwa pola powodowala ze
            # tag_ids bylo zawsze puste, co psulo zarowno heurystyke radio/TV
            # jak i filtrowanie po tagach (SD/HD/Radio/...). "channelTags"
            # zostaje jako fallback dla ewentualnych starszych/patchowanych
            # serwerow.
            tag_ids=list(m.get("tags") or m.get("channelTags") or []),
            is_radio=is_radio,
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

    @classmethod
    def from_htsp(cls, m: dict) -> "EpgEvent":
        return cls(
            event_id=m.get("eventId", 0),
            channel_id=m.get("channelId", 0),
            start=m.get("start", 0),
            stop=m.get("stop", 0),
            title=m.get("title", ""),
            subtitle=m.get("subtitle", ""),
            description=m.get("description", "") or m.get("summary", ""),
            next_event_id=m.get("nextEventId"),
        )


@dataclass
class Recording:
    entry_id: int
    channel_id: int = 0
    title: str = ""
    start: int = 0
    stop: int = 0
    state: str = ""  # scheduled/recording/completed/missed/invalid

    @classmethod
    def from_htsp(cls, m: dict) -> "Recording":
        return cls(
            entry_id=m.get("id", 0),
            channel_id=m.get("channel", 0),
            title=m.get("title", ""),
            start=m.get("start", 0),
            stop=m.get("stop", 0),
            state=m.get("state", ""),
        )


@dataclass
class DvrConfig:
    uuid: str
    name: str = ""
