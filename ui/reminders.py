"""Przypomnienia o audycjach (watch-only, bez nagrywania) - trwale miedzy
sesjami (JSON w XDG_DATA_HOME), z powiadomieniem systemowym (Gio.Notification)
kilka minut przed startem.

To jest odrebna rzecz od DVR/autorec (ktore faktycznie nagrywaja przez
serwer TVH) - przypomnienie tylko odpala powiadomienie na tym komputerze
kilka minut przed audycja, zeby uzytkownik zdazyl przelaczyc kanal.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from gi.repository import GObject, GLib, Gio

logger = logging.getLogger("tvh.reminders")

REMINDER_LEAD_S = 5 * 60  # powiadomienie 5 min przed startem
CHECK_INTERVAL_S = 30


class Reminder:
    def __init__(
        self,
        event_id: int,
        channel_id: int,
        channel_name: str,
        title: str,
        start: int,
        stop: int,
        notified: bool = False,
    ) -> None:
        self.event_id = event_id
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.title = title
        self.start = start
        self.stop = stop
        self.notified = notified

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "title": self.title,
            "start": self.start,
            "stop": self.stop,
            "notified": self.notified,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Reminder":
        return cls(
            d.get("event_id", 0),
            d.get("channel_id", 0),
            d.get("channel_name", ""),
            d.get("title", ""),
            d.get("start", 0),
            d.get("stop", 0),
            d.get("notified", False),
        )


class ReminderStore(GObject.GObject):
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # emitowany tuz przed startem audycji, zeby UI mogl np. zaproponowac przelaczenie kanalu
        "reminder-due": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, application: Optional[Gio.Application] = None) -> None:
        super().__init__()
        self._application = application
        data_dir = Path(GLib.get_user_data_dir()) / "tvh-gnome-client"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._path = data_dir / "reminders.json"
        self.items: List[Reminder] = []
        self._load()
        GLib.timeout_add_seconds(CHECK_INTERVAL_S, self._tick)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self.items = [Reminder.from_dict(d) for d in raw]
        except Exception:
            logger.exception("nie udalo sie wczytac przypomnien")

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps([r.to_dict() for r in self.items], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("nie udalo sie zapisac przypomnien")

    def has_reminder(self, event_id: int) -> bool:
        return any(r.event_id == event_id for r in self.items)

    def add(self, event_id: int, channel_id: int, channel_name: str, title: str, start: int, stop: int) -> None:
        if self.has_reminder(event_id):
            return
        # nie dodawaj przypomnien dla audycji ktore juz sie skonczyly
        if stop and stop < int(time.time()):
            return
        self.items.append(Reminder(event_id, channel_id, channel_name, title, start, stop))
        self.items.sort(key=lambda r: r.start)
        self._save()
        self.emit("changed")

    def remove(self, event_id: int) -> None:
        before = len(self.items)
        self.items = [r for r in self.items if r.event_id != event_id]
        if len(self.items) != before:
            self._save()
            self.emit("changed")

    def _tick(self) -> bool:
        now = int(time.time())
        changed = False
        expired = []
        for r in self.items:
            if not r.notified and r.start - now <= REMINDER_LEAD_S and r.start > now:
                self._notify(r)
                r.notified = True
                changed = True
            if r.stop and r.stop < now:
                expired.append(r)
        if expired:
            self.items = [r for r in self.items if r not in expired]
            changed = True
        if changed:
            self._save()
            self.emit("changed")
        return True

    def _notify(self, r: Reminder) -> None:
        when = time.strftime("%H:%M", time.localtime(r.start))
        body = f"{r.channel_name} · {when}"
        logger.info("Przypomnienie: %s (%s)", r.title, body)
        self.emit("reminder-due", r)
        if self._application is None:
            return
        try:
            notif = Gio.Notification.new(r.title or "Audycja")
            notif.set_body(body)
            notif.set_priority(Gio.NotificationPriority.NORMAL)
            self._application.send_notification(f"reminder-{r.event_id}", notif)
        except Exception:
            logger.exception("nie udalo sie wyslac powiadomienia systemowego")
