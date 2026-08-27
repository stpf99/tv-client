from __future__ import annotations

from datetime import datetime
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

from tvh.library import TvhLibrary
from ui.recent import RecentEntry, RecentStore
from ui.icon_cache import make_icon_widget


class RecentTile(Gtk.Button):
    def __init__(self, entry: RecentEntry, library: TvhLibrary, on_activate: Callable[[int], None]) -> None:
        super().__init__()
        self.add_css_class("tvh-recent-tile")
        self.add_css_class("card")
        self.set_size_request(180, 120)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)

        icon = make_icon_widget("tv-symbolic", 60, library.resolve_icon_url(entry.icon_url))
        icon.set_vexpand(True)
        box.append(icon)

        title = Gtk.Label(label=entry.name, xalign=0, ellipsize=3)
        title.add_css_class("heading")
        box.append(title)

        when = datetime.fromtimestamp(entry.played_at).strftime("%d.%m %H:%M")
        sub = Gtk.Label(label=when, xalign=0)
        sub.add_css_class("caption")
        sub.add_css_class("dim-label")
        box.append(sub)

        self.set_child(box)
        self.connect("clicked", lambda *_: on_activate(entry.channel_id))


class RecentView(Gtk.Box):
    def __init__(self, library: TvhLibrary, recent_store: RecentStore, on_play_channel_id: Callable[[int], None]) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.library = library
        self.recent_store = recent_store
        self.on_play_channel_id = on_play_channel_id

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.flow = Gtk.FlowBox()
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_max_children_per_line(8)
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_margin_top(16)
        self.flow.set_margin_bottom(16)
        self.flow.set_margin_start(16)
        self.flow.set_margin_end(16)
        self.flow.set_row_spacing(12)
        self.flow.set_column_spacing(12)
        scroller.set_child(self.flow)

        self.empty_state = Adw.StatusPage(
            icon_name="document-open-recent-symbolic",
            title="Brak historii",
            description="Odtworzone kanały pojawią się tutaj",
        )

        self.stack = Gtk.Stack()
        self.stack.add_named(self.empty_state, "empty")
        self.stack.add_named(scroller, "grid")
        self.append(self.stack)

        recent_store.connect("changed", lambda *_: self.reload())
        self.reload()

    def reload(self) -> None:
        child = self.flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.flow.remove(child)
            child = nxt
        items = self.recent_store.items
        for entry in items:
            self.flow.append(RecentTile(entry, self.library, self.on_play_channel_id))
        self.stack.set_visible_child_name("grid" if items else "empty")
