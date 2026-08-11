from __future__ import annotations

from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

from tvh.library import TvhLibrary
from tvh.models import Recording

STATE_LABELS = {
    "scheduled": "Zaplanowane",
    "recording": "Nagrywanie…",
    "completed": "Zakończone",
    "missed": "Pominięte",
    "invalid": "Błąd",
}

STATE_ICONS = {
    "scheduled": "alarm-symbolic",
    "recording": "media-record-symbolic",
    "completed": "emblem-ok-symbolic",
    "missed": "dialog-warning-symbolic",
    "invalid": "dialog-error-symbolic",
}


class RecordingRow(Gtk.ListBoxRow):
    def __init__(self, rec: Recording, library: TvhLibrary) -> None:
        super().__init__()
        self.rec = rec

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        icon = Gtk.Image.new_from_icon_name(STATE_ICONS.get(rec.state, "media-optical-symbolic"))
        icon.set_pixel_size(24)
        box.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        title_lbl = Gtk.Label(label=rec.title or "(bez tytułu)", xalign=0)
        title_lbl.add_css_class("title-4")
        when = f"{datetime.fromtimestamp(rec.start).strftime('%d.%m %H:%M')} – {datetime.fromtimestamp(rec.stop).strftime('%H:%M')}"
        sub_lbl = Gtk.Label(label=f"{STATE_LABELS.get(rec.state, rec.state)} · {when}", xalign=0)
        sub_lbl.add_css_class("dim-label")
        sub_lbl.add_css_class("caption")
        text_box.append(title_lbl)
        text_box.append(sub_lbl)
        box.append(text_box)

        if rec.state == "recording":
            stop_btn = Gtk.Button(icon_name="media-playback-stop-symbolic")
            stop_btn.add_css_class("flat")
            stop_btn.connect("clicked", lambda *_: library.stop_recording(rec.entry_id))
            box.append(stop_btn)
        elif rec.state == "scheduled":
            cancel_btn = Gtk.Button(icon_name="edit-delete-symbolic")
            cancel_btn.add_css_class("flat")
            cancel_btn.connect("clicked", lambda *_: library.cancel_recording(rec.entry_id))
            box.append(cancel_btn)
        else:
            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.connect("clicked", lambda *_: library.delete_recording(rec.entry_id))
            box.append(del_btn)

        self.set_child(box)


class RecordingsView(Gtk.Box):
    def __init__(self, library: TvhLibrary) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.library = library

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        for m in ("top", "bottom", "start", "end"):
            getattr(self.listbox, f"set_margin_{m}")(10)
        scroller.set_child(self.listbox)

        self.empty_state = Adw.StatusPage(
            icon_name="folder-videos-symbolic",
            title="Brak nagrań",
            description="Zaplanowane i zakończone nagrania pojawią się tutaj",
        )

        self.stack = Gtk.Stack()
        self.stack.add_named(self.empty_state, "empty")
        self.stack.add_named(scroller, "list")
        self.append(self.stack)

        library.connect("recordings-changed", lambda *_: self.reload())
        self.reload()

    def reload(self) -> None:
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        recs = sorted(self.library.recordings.values(), key=lambda r: -r.start)
        for rec in recs:
            self.listbox.append(RecordingRow(rec, self.library))
        self.stack.set_visible_child_name("list" if recs else "empty")
