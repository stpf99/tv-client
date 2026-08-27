from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from urllib.request import urlretrieve, Request, urlopen

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Adw, Pango, GLib, Gio  # noqa: E402

from tvh.library import TvhLibrary
from tvh.models import Recording

logger = logging.getLogger("tvh.recordings_view")

STATE_LABELS = {
    "scheduled": "Zaplanowane",
    "recording": "Nagrywanie…",
    "completed": "Zakończone",
    "completedError": "Zakończone z błędem",
    "missed": "Pominięte",
    "invalid": "Błąd",
    "running": "Nagrywanie…",
}

STATE_ICONS = {
    "scheduled": "alarm-symbolic",
    "recording": "media-record-symbolic",
    "running": "media-record-symbolic",
    "completed": "emblem-ok-symbolic",
    "completedError": "dialog-warning-symbolic",
    "missed": "dialog-warning-symbolic",
    "invalid": "dialog-error-symbolic",
}

# katalog lokalny na pobrane nagrania (w katalogu domowym użytkownika)
LOCAL_ARCHIVE_DIR = Path.home() / "Videos" / "TVH-Nagrania"


def _fmt_when(rec: Recording) -> str:
    """Co i kiedy – pełna data + 24h."""
    if not rec.start:
        return "—"
    start_dt = datetime.fromtimestamp(rec.start)
    stop_dt = datetime.fromtimestamp(rec.stop) if rec.stop else None
    day = start_dt.strftime("%d.%m.%Y")
    t0 = start_dt.strftime("%H:%M")
    t1 = stop_dt.strftime("%H:%M") if stop_dt else "--:--"
    return f"{day}  {t0}–{t1}"


def _fmt_size(n: int) -> str:
    if n <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class RecordingRow(Gtk.ListBoxRow):
    """Karta nagrania: co i kiedy + Play / Archiwizuj / Pobierz / Usuń z serwera."""

    def __init__(self, rec: Recording, library: TvhLibrary, parent_view: "RecordingsView") -> None:
        super().__init__()
        self.rec = rec
        self.library = library
        self.parent_view = parent_view

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        outer.set_margin_top(10)
        outer.set_margin_bottom(10)
        outer.set_margin_start(14)
        outer.set_margin_end(14)

        # --- nagłówek: ikona stanu + tytuł + kanał --------------------
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        icon = Gtk.Image.new_from_icon_name(
            STATE_ICONS.get(rec.state, "media-optical-symbolic")
        )
        icon.set_pixel_size(28)
        icon.set_valign(Gtk.Align.START)
        head.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)

        title_lbl = Gtk.Label(label=rec.title or "(bez tytułu)", xalign=0)
        title_lbl.add_css_class("title-4")
        title_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        title_lbl.set_hexpand(True)
        text_box.append(title_lbl)

        if rec.subtitle:
            sub = Gtk.Label(label=rec.subtitle, xalign=0)
            sub.add_css_class("dim-label")
            sub.add_css_class("caption")
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            text_box.append(sub)

        meta_parts = [
            STATE_LABELS.get(rec.state, rec.state or "?"),
            _fmt_when(rec),
        ]
        if rec.channel_name:
            meta_parts.append(rec.channel_name)
        size_s = _fmt_size(rec.filesize)
        if size_s:
            meta_parts.append(size_s)

        meta_lbl = Gtk.Label(label=" · ".join(meta_parts), xalign=0)
        meta_lbl.add_css_class("dim-label")
        meta_lbl.add_css_class("caption")
        meta_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        text_box.append(meta_lbl)

        if rec.path:
            path_lbl = Gtk.Label(label=rec.path, xalign=0)
            path_lbl.add_css_class("dim-label")
            path_lbl.add_css_class("caption")
            path_lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            path_lbl.set_tooltip_text(rec.path)
            text_box.append(path_lbl)

        head.append(text_box)
        outer.append(head)

        # --- przyciski akcji ------------------------------------------
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions.set_halign(Gtk.Align.END)

        state = (rec.state or "").lower()

        if state in ("recording", "running"):
            stop_btn = Gtk.Button(label="Zatrzymaj")
            stop_btn.set_icon_name("media-playback-stop-symbolic")
            stop_btn.connect("clicked", lambda *_: library.stop_recording(rec.entry_id))
            actions.append(stop_btn)

        if state == "scheduled":
            cancel_btn = Gtk.Button(label="Anuluj")
            cancel_btn.set_icon_name("edit-delete-symbolic")
            cancel_btn.add_css_class("destructive-action")
            cancel_btn.connect("clicked", lambda *_: library.cancel_recording(rec.entry_id))
            actions.append(cancel_btn)

        # Play – dla zakończonych i trwających
        if state in ("completed", "completederror", "recording", "running"):
            play_btn = Gtk.Button(label="Odtwórz")
            play_btn.set_icon_name("media-playback-start-symbolic")
            play_btn.add_css_class("suggested-action")
            play_btn.connect("clicked", self._on_play)
            actions.append(play_btn)

            # Pobierz na komputer
            dl_btn = Gtk.Button(label="Pobierz")
            dl_btn.set_icon_name("folder-download-symbolic")
            dl_btn.set_tooltip_text("Pobierz plik nagrania na ten komputer")
            dl_btn.connect("clicked", self._on_download)
            actions.append(dl_btn)

            # Archiwizuj lokalnie (pobierz do ~/Videos/TVH-Nagrania)
            arch_btn = Gtk.Button(label="Archiwizuj")
            arch_btn.set_icon_name("document-save-symbolic")
            arch_btn.set_tooltip_text(
                f"Pobierz do archiwum lokalnego:\n{LOCAL_ARCHIVE_DIR}"
            )
            arch_btn.connect("clicked", self._on_archive)
            actions.append(arch_btn)

        # Usuń z serwera (zawsze oprócz samego „scheduled” – tam jest Anuluj)
        if state != "scheduled":
            del_btn = Gtk.Button(label="Usuń z serwera")
            del_btn.set_icon_name("user-trash-symbolic")
            del_btn.add_css_class("destructive-action")
            del_btn.set_tooltip_text("Usuń wpis i plik nagrania z serwera Tvheadend")
            del_btn.connect("clicked", self._on_delete)
            actions.append(del_btn)

        outer.append(actions)
        self.set_child(outer)

    # ------------------------------------------------------------------ #
    def _on_play(self, *_a) -> None:
        self.parent_view.play_recording(self.rec)

    def _on_download(self, *_a) -> None:
        self.parent_view.download_recording(self.rec, archive=False)

    def _on_archive(self, *_a) -> None:
        self.parent_view.download_recording(self.rec, archive=True)

    def _on_delete(self, *_a) -> None:
        dialog = Adw.MessageDialog(
            transient_for=self.get_root(),
            heading="Usunąć nagranie z serwera?",
            body=f"„{self.rec.title or '(bez tytułu)'}” zostanie trwale usunięte z serwera Tvheadend (plik + wpis DVR).",
        )
        dialog.add_response("cancel", "Anuluj")
        dialog.add_response("delete", "Usuń z serwera")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._confirm_delete)
        dialog.present()

    def _confirm_delete(self, dialog, response: str) -> None:
        if response == "delete":
            self.library.delete_recording(self.rec.entry_id)


class NewRecordingDialog(Adw.Window):
    """Zaplanuj nowe nagranie: wybor kanalu Z LISTY (nie recznie wpisywana
    nazwa), tytul i okno czasowe. Adw.ComboRow z duza lista kanalow ma
    wbudowane wyszukiwanie typeahead (GTK >= 4.10) - wpisanie liter
    filtruje/skacze do pasujacej pozycji tak jak w innych ComboRow tej
    aplikacji (patrz connection_dialog.py, live_view.py prefs)."""

    def __init__(self, parent: Gtk.Window, library: TvhLibrary) -> None:
        super().__init__(transient_for=parent, modal=True, title="Nowe nagranie")
        self.library = library
        self.saved = False
        self.set_default_size(440, 420)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel_btn = Gtk.Button(label="Anuluj")
        cancel_btn.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_btn)
        self.save_btn = Gtk.Button(label="Zaplanuj")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.connect("clicked", self._on_save)
        self.save_btn.set_sensitive(False)
        header.pack_end(self.save_btn)
        toolbar_view.add_top_bar(header)

        page = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(title="Kanał i tytuł")
        page.add(group)

        self._channels = library.tv_channels() + library.radio_channels()
        names = [f"{c.number or '—'}  {c.name}" for c in self._channels]
        self.channel_row = Adw.ComboRow(title="Kanał")
        self.channel_row.set_model(Gtk.StringList.new(names))
        # Wyszukiwanie w ComboRow wymaga ustawienia 'expression' (od czego
        # brac tekst do filtrowania) - bez tego enable_search nic nie
        # zrobi. Wymaga libadwaita >= 1.4; na starszych wersjach po prostu
        # nie bedzie pola wyszukiwania w popupie, reszta dziala normalnie.
        try:
            self.channel_row.set_expression(
                Gtk.PropertyExpression.new(Gtk.StringObject, None, "string")
            )
            self.channel_row.set_enable_search(True)
        except (AttributeError, TypeError):
            logger.debug("Adw.ComboRow.set_enable_search niedostępne w tej wersji libadwaita")
        self.channel_row.connect("notify::selected", self._on_channel_changed)
        group.add(self.channel_row)

        self.title_entry = Adw.EntryRow(title="Tytuł nagrania")
        group.add(self.title_entry)

        group_time = Adw.PreferencesGroup(title="Okno czasowe")
        page.add(group_time)

        now = int(time.time())
        default_start = now + 60
        default_stop = now + 3600

        self.date_row = Adw.EntryRow(title="Data (RRRR-MM-DD)")
        self.date_row.set_text(datetime.fromtimestamp(default_start).strftime("%Y-%m-%d"))
        group_time.add(self.date_row)

        self.start_row = Adw.EntryRow(title="Początek (GG:MM)")
        self.start_row.set_text(datetime.fromtimestamp(default_start).strftime("%H:%M"))
        group_time.add(self.start_row)

        self.stop_row = Adw.EntryRow(title="Koniec (GG:MM)")
        self.stop_row.set_text(datetime.fromtimestamp(default_stop).strftime("%H:%M"))
        group_time.add(self.stop_row)

        self.error_lbl = Gtk.Label(xalign=0, wrap=True)
        self.error_lbl.add_css_class("error")
        self.error_lbl.set_visible(False)
        self.error_lbl.set_margin_start(12)
        self.error_lbl.set_margin_end(12)
        page.add(self._wrap_error())

        toolbar_view.set_content(page)
        self.set_content(toolbar_view)

        if self._channels:
            self.channel_row.set_selected(0)

    def _wrap_error(self) -> Adw.PreferencesGroup:
        g = Adw.PreferencesGroup()
        g.add(self.error_lbl)
        return g

    def _on_channel_changed(self, *_a) -> None:
        idx = self.channel_row.get_selected()
        if 0 <= idx < len(self._channels) and not self.title_entry.get_text():
            ch = self._channels[idx]
            ev = self.library.current_event_for_channel(ch.channel_id, int(time.time()))
            if ev and ev.title:
                self.title_entry.set_text(ev.title)
        self.save_btn.set_sensitive(bool(self._channels))

    def _parse_window(self) -> tuple[int, int] | None:
        try:
            d = datetime.strptime(self.date_row.get_text().strip(), "%Y-%m-%d")
            t0 = datetime.strptime(self.start_row.get_text().strip(), "%H:%M")
            t1 = datetime.strptime(self.stop_row.get_text().strip(), "%H:%M")
        except ValueError:
            self.error_lbl.set_text("Nieprawidłowy format daty lub godziny.")
            self.error_lbl.set_visible(True)
            return None
        start_dt = d.replace(hour=t0.hour, minute=t0.minute)
        stop_dt = d.replace(hour=t1.hour, minute=t1.minute)
        if stop_dt <= start_dt:
            # okno przechodzace przez polnoc (np. 23:50 -> 00:40)
            from datetime import timedelta
            stop_dt += timedelta(days=1)
        start = int(start_dt.timestamp())
        stop = int(stop_dt.timestamp())
        if stop <= start:
            self.error_lbl.set_text("Koniec musi być późniejszy niż początek.")
            self.error_lbl.set_visible(True)
            return None
        self.error_lbl.set_visible(False)
        return start, stop

    def _on_save(self, *_a) -> None:
        idx = self.channel_row.get_selected()
        if not (0 <= idx < len(self._channels)):
            return
        window = self._parse_window()
        if window is None:
            return
        start, stop = window
        ch = self._channels[idx]
        title = self.title_entry.get_text().strip() or ch.name
        self.library.record_manual(channel_id=ch.channel_id, title=title, start=start, stop=stop)
        self.saved = True
        self.close()


class RecordingsView(Gtk.Box):
    """Osobna karta/index nagrań: co i kiedy, play, archiwizuj, pobierz, usuń z serwera."""

    def __init__(self, library: TvhLibrary, on_play_url=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.library = library
        # callback(url, title) – podłączony z MainWindow do odtwarzacza
        self.on_play_url = on_play_url

        # nagłówek sekcji
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_top(8)
        header.set_margin_bottom(4)
        header.set_margin_start(14)
        header.set_margin_end(14)
        title = Gtk.Label(label="Nagrania DVR", xalign=0, hexpand=True)
        title.add_css_class("title-3")
        header.append(title)
        new_rec_btn = Gtk.Button(label="Nowe nagranie")
        new_rec_btn.set_icon_name("list-add-symbolic")
        new_rec_btn.add_css_class("suggested-action")
        new_rec_btn.set_tooltip_text("Zaplanuj nagranie wybierając kanał z listy")
        new_rec_btn.connect("clicked", self._on_new_recording)
        header.append(new_rec_btn)
        open_local = Gtk.Button(label="Folder lokalny")
        open_local.set_icon_name("folder-symbolic")
        open_local.set_tooltip_text(str(LOCAL_ARCHIVE_DIR))
        open_local.connect("clicked", self._open_local_folder)
        header.append(open_local)
        self.append(header)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("boxed-list")
        for m in ("top", "bottom", "start", "end"):
            getattr(self.listbox, f"set_margin_{m}")(10)
        scroller.set_child(self.listbox)

        self.empty_state = Adw.StatusPage(
            icon_name="folder-videos-symbolic",
            title="Brak nagrań",
            description="Zaplanowane i zakończone nagrania pojawią się tutaj.\n"
            "Z EPG: jednorazowo / seria / ręcznie.",
        )

        self.stack = Gtk.Stack()
        self.stack.add_named(self.empty_state, "empty")
        self.stack.add_named(scroller, "list")
        self.append(self.stack)

        self._toast_overlay = None  # ustawiane z zewnątrz jeśli jest

        library.connect("recordings-changed", lambda *_: self.reload())
        self.reload()

    def set_toast_overlay(self, overlay: Adw.ToastOverlay) -> None:
        self._toast_overlay = overlay

    def _toast(self, msg: str) -> None:
        if self._toast_overlay is not None:
            self._toast_overlay.add_toast(Adw.Toast(title=msg, timeout=4))
        else:
            logger.info("%s", msg)

    def _open_local_folder(self, *_a) -> None:
        LOCAL_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(LOCAL_ARCHIVE_DIR.as_uri(), None)

    def _on_new_recording(self, *_a) -> None:
        if not self.library.tv_channels() and not self.library.radio_channels():
            self._toast("Lista kanałów jeszcze się nie załadowała — spróbuj ponownie za chwilę.")
            return
        dialog = NewRecordingDialog(self.get_root(), self.library)
        dialog.connect("close-request", lambda *_: self._toast("Zaplanowano nagranie.") if dialog.saved else None)
        dialog.present()

    def reload(self) -> None:
        while (row := self.listbox.get_row_at_index(0)) is not None:
            self.listbox.remove(row)
        recs = sorted(self.library.recordings.values(), key=lambda r: -r.start)
        for rec in recs:
            self.listbox.append(RecordingRow(rec, self.library, self))
        self.stack.set_visible_child_name("list" if recs else "empty")

    # ------------------------------------------------------------------ #
    # Odtwarzanie
    # ------------------------------------------------------------------ #
    def play_recording(self, rec: Recording) -> None:
        def _ok(url: str) -> None:
            self._toast(f"Odtwarzanie: {rec.title or 'nagranie'}")
            if self.on_play_url:
                self.on_play_url(url, rec.title or "Nagranie")
            else:
                # fallback – otwórz w zewnętrznym playerze
                Gio.AppInfo.launch_default_for_uri(url, None)

        def _err(exc: Exception) -> None:
            self._toast(f"Nie udało się uzyskać URL nagrania: {exc}")
            logger.error("play recording: %s", exc)

        self.library.get_recording_url(rec.entry_id, on_ok=_ok, on_err=_err)

    # ------------------------------------------------------------------ #
    # Pobieranie / archiwizacja lokalna
    # ------------------------------------------------------------------ #
    def download_recording(self, rec: Recording, archive: bool = False) -> None:
        def _ok(url: str) -> None:
            if archive:
                dest_dir = LOCAL_ARCHIVE_DIR
                dest_dir.mkdir(parents=True, exist_ok=True)
                safe_title = "".join(
                    c if c.isalnum() or c in " ._-" else "_"
                    for c in (rec.title or f"nagranie-{rec.entry_id}")
                )[:80]
                stamp = datetime.fromtimestamp(rec.start).strftime("%Y%m%d_%H%M") if rec.start else ""
                ext = Path(rec.path).suffix if rec.path else ".mkv"
                if not ext or len(ext) > 5:
                    ext = ".mkv"
                dest = dest_dir / f"{stamp}_{safe_title}{ext}"
            else:
                # dialog wyboru lokalizacji
                dialog = Gtk.FileDialog(title="Zapisz nagranie jako…")
                name = Path(rec.path).name if rec.path else f"nagranie-{rec.entry_id}.mkv"
                dialog.set_initial_name(name)

                def _on_save(dlg, result):
                    try:
                        file = dlg.save_finish(result)
                    except GLib.Error:
                        return
                    if file is None:
                        return
                    path = file.get_path()
                    if path:
                        self._start_download(url, Path(path), rec.title or "nagranie")

                dialog.save(self.get_root(), None, _on_save)
                return

            self._start_download(url, dest, rec.title or "nagranie")

        def _err(exc: Exception) -> None:
            self._toast(f"Błąd URL nagrania: {exc}")

        self.library.get_recording_url(rec.entry_id, on_ok=_ok, on_err=_err)

    def _start_download(self, url: str, dest: Path, title: str) -> None:
        self._toast(f"Pobieranie: {title} → {dest.name}")

        def _worker():
            try:
                req = Request(url, headers={"User-Agent": "tvh-gnome-client/1.0"})
                with urlopen(req, timeout=30) as resp, open(dest, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        out.write(chunk)
                GLib.idle_add(self._toast, f"Zapisano: {dest}")
            except Exception as exc:
                logger.exception("download failed")
                GLib.idle_add(self._toast, f"Błąd pobierania: {exc}")

        import threading

        threading.Thread(target=_worker, daemon=True).start()
