from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Dict, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gio, GLib, GdkPixbuf  # noqa: E402

logger = logging.getLogger("tvh.icon_cache")

# Cache na dysku dla pobranych ikon kanalow (np. z TVH imagecache) -
# unika ponownego sciagania tej samej ikony po restarcie aplikacji.
_CACHE_DIR = Path(GLib.get_user_cache_dir()) / "tvh-gnome-client" / "icons"

# Cache w pamieci: url -> Gdk.Texture, zeby te same widgety (np. przy
# przewijaniu listy kanalow) nie odpalaly wielu rownoleglych pobran.
_texture_cache: Dict[str, "object"] = {}
_pending: Dict[str, list] = {}


def _cache_path_for(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return _CACHE_DIR / digest


def make_icon_widget(icon_name: str, size: int, url: Optional[str] = None) -> Gtk.Widget:
    """Tworzy widget ikony kanalu.

    Domyslnie pokazuje symboliczna ikone (np. "tv-symbolic"), a jesli
    podano url, probuje asynchronicznie pobrac i podmienic na wlasciwe
    logo kanalu. Zwrocony widget mozna pozniej zaktualizowac przez
    update_icon_widget().
    """
    image = Gtk.Image.new_from_icon_name(icon_name)
    image.set_pixel_size(size)
    image.add_css_class("tvh-channel-icon")
    image._tvh_icon_name = icon_name  # type: ignore[attr-defined]
    image._tvh_icon_size = size  # type: ignore[attr-defined]

    if url:
        _load_icon_async(image, url)

    return image


def update_icon_widget(image: Gtk.Image, url: Optional[str]) -> None:
    """Aktualizuje istniejacy widget ikony (np. przy zmianie kanalu w OSD).

    Gdy url jest None, przywraca domyslna ikone symboliczna.
    """
    icon_name = getattr(image, "_tvh_icon_name", "tv-symbolic")
    size = getattr(image, "_tvh_icon_size", 32)

    if not url:
        image.set_from_icon_name(icon_name)
        return

    cached = _texture_cache.get(url)
    if cached is not None:
        image.set_from_paintable(cached)
        return

    image.set_from_icon_name(icon_name)
    _load_icon_async(image, url)


def _load_icon_async(image: Gtk.Image, url: str) -> None:
    cached = _texture_cache.get(url)
    if cached is not None:
        image.set_from_paintable(cached)
        return

    cache_path = _cache_path_for(url)
    if cache_path.exists():
        try:
            texture = GdkPixbuf.Pixbuf.new_from_file(str(cache_path))
            _finish_load(image, url, texture)
            return
        except GLib.Error:
            logger.debug("Uszkodzony wpis w cache ikon, pobieram ponownie: %s", url)

    if url in _pending:
        _pending[url].append(image)
        return
    _pending[url] = [image]

    gfile = Gio.File.new_for_uri(url)
    gfile.load_contents_async(None, _on_download_finished, url)


def _on_download_finished(gfile: Gio.File, result: Gio.AsyncResult, url: str) -> None:
    widgets = _pending.pop(url, [])
    try:
        ok, contents, _etag = gfile.load_contents_finish(result)
    except GLib.Error as exc:
        logger.debug("Nie udalo sie pobrac ikony %s: %s", url, exc)
        return

    if not ok or not contents:
        return

    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(contents)
        loader.close()
        pixbuf = loader.get_pixbuf()
    except GLib.Error as exc:
        logger.debug("Nie udalo sie zdekodowac ikony %s: %s", url, exc)
        return

    if pixbuf is None:
        return

    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = _cache_path_for(url)
        pixbuf.savev(str(cache_path), "png", [], [])
    except GLib.Error as exc:
        logger.debug("Nie udalo sie zapisac ikony do cache: %s", exc)

    for image in widgets:
        _finish_load(image, url, pixbuf)


def _finish_load(image: Gtk.Image, url: str, pixbuf: "GdkPixbuf.Pixbuf") -> None:
    # Logo kanalow czesto ma proporcje szerokie (np. 200x100), a nie
    # kwadratowe - skalowanie na sztywno do size x size je splaszcza.
    # Zamiast tego skalujemy z zachowaniem proporcji tak, aby zmiescic
    # sie w kwadracie size x size (dluzszy bok = size).
    size = getattr(image, "_tvh_icon_size", 32)
    src_w, src_h = pixbuf.get_width(), pixbuf.get_height()
    scaled = pixbuf
    if src_w > 0 and src_h > 0 and (src_w != size or src_h != size):
        scale = size / max(src_w, src_h)
        new_w = max(1, round(src_w * scale))
        new_h = max(1, round(src_h * scale))
        scaled = pixbuf.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)

    from gi.repository import Gdk

    texture = Gdk.Texture.new_for_pixbuf(scaled)
    _texture_cache[url] = texture
    image.set_from_paintable(texture)
    # Gtk.Image domyslnie centruje zawartosc mniejsza niz jego rozmiar,
    # wiec box/logo pozostanie wysrodkowane w polu size x size bez
    # rozciagania.
    image.set_pixel_size(size)
