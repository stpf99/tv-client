"""Lista ostatnio odtwarzanych kanalow - trwala miedzy sesjami (JSON w XDG_DATA_HOME)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import List

from gi.repository import GObject, GLib

from tvh.models import Channel

logger = logging.getLogger("tvh.recent")

MAX_ITEMS = 20


class RecentEntry:
    def __init__(self, channel_id: int, name: str, icon_url: str | None, played_at: float):
        self.channel_id = channel_id
        self.name = name
        self.icon_url = icon_url
        self.played_at = played_at

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "name": self.name,
            "icon_url": self.icon_url,
            "played_at": self.played_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RecentEntry":
        return cls(d.get("channel_id", 0), d.get("name", ""), d.get("icon_url"), d.get("played_at", 0.0))


class RecentStore(GObject.GObject):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self) -> None:
        super().__init__()
        data_dir = Path(GLib.get_user_data_dir()) / "tvh-gnome-client"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._path = data_dir / "recent.json"
        self.items: List[RecentEntry] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self.items = [RecentEntry.from_dict(d) for d in raw]
        except Exception:
            logger.exception("Nie udalo sie wczytac listy ostatnio odtwarzanych")

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps([e.to_dict() for e in self.items], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Nie udalo sie zapisac listy ostatnio odtwarzanych")

    def add_channel(self, channel: Channel) -> None:
        self.items = [e for e in self.items if e.channel_id != channel.channel_id]
        self.items.insert(0, RecentEntry(channel.channel_id, channel.name, channel.icon_url, time.time()))
        self.items = self.items[:MAX_ITEMS]
        self._save()
        self.emit("changed")

    def clear(self) -> None:
        self.items = []
        self._save()
        self.emit("changed")
