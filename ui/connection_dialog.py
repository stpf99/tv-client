from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk  # noqa: E402

from tvh.config import ServerConfig, list_servers, remove_server

NEW_SERVER_SENTINEL = "__new__"


class ConnectionDialog(Adw.Window):
    def __init__(self, parent: Gtk.Window, existing: ServerConfig | None,
                 on_connect: Callable[[ServerConfig], None]) -> None:
        super().__init__(transient_for=parent, modal=True, default_width=420, default_height=-1)
        self.set_title("Połącz z serwerem Tvheadend")
        self._on_connect = on_connect
        # existing.server_id (jeśli podane) == aktualnie aktywny serwer,
        # zaznaczany domyślnie na liście zapisanych.
        self._active_id = existing.server_id if existing else None
        self._saved_servers: list[ServerConfig] = list_servers()

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()

        # --- Zapisane serwery: przełączanie / dodawanie kolejnych --------
        group_saved = Adw.PreferencesGroup(
            title="Zapisane serwery",
            description="Wybierz serwer albo dodaj nowy — możesz mieć ich dowolnie wiele.",
        )
        page.add(group_saved)

        self.server_row = Adw.ComboRow(title="Serwer")
        labels = [f"{s.name} ({s.host})" for s in self._saved_servers] + ["+ Nowy serwer…"]
        self.server_row.set_model(Gtk.StringList.new(labels))
        default_idx = len(self._saved_servers)  # "+ Nowy serwer…" domyślnie gdy brak dopasowania
        for i, s in enumerate(self._saved_servers):
            if s.server_id == self._active_id:
                default_idx = i
                break
        self.server_row.set_selected(default_idx)
        self.server_row.connect("notify::selected", self._on_server_picked)
        group_saved.add(self.server_row)

        self.delete_btn = Gtk.Button(label="Usuń wybrany serwer z listy")
        self.delete_btn.add_css_class("destructive-action")
        self.delete_btn.set_halign(Gtk.Align.START)
        self.delete_btn.connect("clicked", self._on_delete_clicked)
        group_saved.add(self.delete_btn)

        # --- Dane wybranego/nowego serwera --------------------------------
        group = Adw.PreferencesGroup(title="Dane serwera")
        page.add(group)

        self.name_row = Adw.EntryRow(title="Nazwa (dowolna, do rozpoznania na liście)")
        self.host_row = Adw.EntryRow(title="Adres serwera (host)")
        self.htsp_port_row = Adw.EntryRow(title="Port HTSP")
        self.http_port_row = Adw.EntryRow(title="Port HTTP (API/nagrania)")
        self.user_row = Adw.EntryRow(title="Użytkownik")
        self.pass_row = Adw.PasswordEntryRow(title="Hasło")

        for row in (
            self.name_row, self.host_row, self.htsp_port_row,
            self.http_port_row, self.user_row, self.pass_row,
        ):
            group.add(row)

        self._current_server_id = ""
        if default_idx < len(self._saved_servers):
            self._fill_form(self._saved_servers[default_idx])
        else:
            self._fill_form(None)

        connect_btn = Gtk.Button(label="Połącz")
        connect_btn.add_css_class("suggested-action")
        connect_btn.set_halign(Gtk.Align.END)
        connect_btn.set_margin_top(12)
        connect_btn.set_margin_end(12)
        connect_btn.set_margin_bottom(12)
        connect_btn.connect("clicked", self._on_connect_clicked)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(page)
        box.append(connect_btn)

        toolbar.set_content(box)
        self.set_content(toolbar)

    def _fill_form(self, cfg: Optional[ServerConfig]) -> None:
        if cfg:
            self._current_server_id = cfg.server_id
            self.name_row.set_text(cfg.name)
            self.host_row.set_text(cfg.host)
            self.htsp_port_row.set_text(str(cfg.htsp_port))
            self.http_port_row.set_text(str(cfg.http_port))
            self.user_row.set_text(cfg.username)
            self.pass_row.set_text(cfg.password)
            self.delete_btn.set_sensitive(True)
        else:
            self._current_server_id = ""
            self.name_row.set_text("")
            self.host_row.set_text("")
            self.htsp_port_row.set_text("9982")
            self.http_port_row.set_text("9981")
            self.user_row.set_text("")
            self.pass_row.set_text("")
            self.delete_btn.set_sensitive(False)

    def _on_server_picked(self, *_a) -> None:
        idx = self.server_row.get_selected()
        if idx < len(self._saved_servers):
            self._fill_form(self._saved_servers[idx])
        else:
            self._fill_form(None)

    def _on_delete_clicked(self, _btn) -> None:
        if not self._current_server_id:
            return
        remove_server(self._current_server_id)
        self._saved_servers = list_servers()
        labels = [f"{s.name} ({s.host})" for s in self._saved_servers] + ["+ Nowy serwer…"]
        self.server_row.set_model(Gtk.StringList.new(labels))
        self.server_row.set_selected(len(self._saved_servers))
        self._fill_form(None)

    def _on_connect_clicked(self, _btn) -> None:
        try:
            htsp_port = int(self.htsp_port_row.get_text() or "9982")
            http_port = int(self.http_port_row.get_text() or "9981")
        except ValueError:
            htsp_port, http_port = 9982, 9981
        host = self.host_row.get_text().strip()
        cfg = ServerConfig(
            host=host,
            htsp_port=htsp_port,
            http_port=http_port,
            username=self.user_row.get_text().strip(),
            password=self.pass_row.get_text(),
            server_id=self._current_server_id,
            name=self.name_row.get_text().strip() or host,
        )
        self._on_connect(cfg)
        self.close()
