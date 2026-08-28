"""Wspolny loader/cache ikon stacji (channelIcon z TVH) dla wszystkich
widokow (lista kanalow, ostatnio ogladane, OSD).

Pobieranie idzie w watku roboczym (urllib), dekodowanie GdkPixbuf i
dostarczenie do widgetu przez GLib.idle_add - bezpieczne dla GTK.
Cache dwupoziomowy:
  - w pamieci (Gdk.Texture) na czas zycia procesu,
  - na dysku (surowe bajty, XDG_CACHE_HOME) miedzy sesjami.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Callable, Dict, Optional, Set
from urllib.request import Request, urlopen

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, GObject  # noqa: E402

logger = logging.getLogger("ui.icon_cache")

_CACHE_DIR = Path(GLib.get_user_cache_dir()) / "tvh-gnome-client" / "icons"
_FETCH_TIMEOUT_S = 6.0
_MAX_BYTES = 4 * 1024 * 1024  # 4 MB - twardy limit, ikony stacji sa male


def _cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return _CACHE_DIR / digest


class IconCache(GObject.GObject):
    """Singleton-owy cache tekstur ikon, dzielony miedzy widokami."""

    _instance: Optional["IconCache"] = None

    def __init__(self) -> None:
        super().__init__()
        self._mem: Dict[str, Gdk.Texture] = {}
        self._failed: Set[str] = set()
        self._inflight: Set[str] = set()
        self._waiters: Dict[str, list] = {}

    @classmethod
    def get(cls) -> "IconCache":
        if cls._instance is None:
            cls._instance = IconCache()
        return cls._instance

    def get_texture(self, url: Optional[str]) -> Optional[Gdk.Texture]:
        """Zwraca teksture jesli juz jest w pamieci (nieblokujace)."""
        if not url:
            return None
        return self._mem.get(url)

    def request(self, url: Optional[str], on_ready: Callable[[Optional[Gdk.Texture]], None]) -> None:
        """Asynchronicznie dostarcza teksture (lub None przy bledzie/braku).

        on_ready jest wywolywane na petli glownej GLib. Bezpieczne wywolanie
        wielokrotne dla tego samego URL (dedupe w locie) - kazdy caller
        dostaje wlasne wywolanie zwrotne gdy dane sa gotowe.
        """
        if not url:
            GLib.idle_add(on_ready, None)
            return
        cached = self._mem.get(url)
        if cached is not None:
            GLib.idle_add(on_ready, cached)
            return
        if url in self._failed:
            GLib.idle_add(on_ready, None)
            return
        # Szybka sciezka: dane juz sa na dysku (najczestszy przypadek przy
        # starcie, gdy poprzednia sesja zapelnila cache). Odczyt lokalnego
        # pliku jest tani, WIEC UNIKAMY watku (threading.Thread ma wiekszy
        # narzut niz sam odczyt kilku KB) - ale odczyt+dekodowanie nadal
        # oddajemy do GLib.idle_add, a nie robimy inline w wywolaniu
        # request(). Przy budowaniu listy setek wierszy na starcie,
        # request() jest wolane bezposrednio z konstruktora kazdego wiersza
        # - inline'owy odczyt+dekodowanie zablokowalby glowna petle na czas
        # zbudowania calej listy (stad "okno nie odpowiada"), zamiast
        # oddawac sterowanie petli zdarzen miedzy kolejnymi ikonami.
        cpath = _cache_path(url)
        if cpath.exists():
            self._waiters.setdefault(url, []).append(on_ready)
            GLib.idle_add(self._load_from_disk_cache, url, cpath)
            return

        self._waiters.setdefault(url, []).append(on_ready)
        if url in self._inflight:
            return
        self._inflight.add(url)
        threading.Thread(target=self._fetch_worker, args=(url,), daemon=True).start()

    def _load_from_disk_cache(self, url: str, cpath: Path) -> bool:
        """Wywolywane z GLib.idle_add - odczyt lokalnego pliku i dekodowanie
        dzieje sie w tym momencie, nie w request(), zeby kazda ikona byla
        osobnym "krokiem" petli zdarzen zamiast jednego dlugiego bloku."""
        try:
            data = cpath.read_bytes()
        except Exception:
            logger.debug("nie udalo sie odczytac ikony z cache dysku: %s", url)
            data = None
        self._on_fetched(url, data)
        return False

    # ------------------------------------------------------------------ #
    def _fetch_worker(self, url: str) -> None:
        data: Optional[bytes] = None
        try:
            cpath = _cache_path(url)
            if cpath.exists():
                data = cpath.read_bytes()
            else:
                req = Request(url, headers={"User-Agent": "tvh-gnome-client"})
                with urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
                    data = resp.read(_MAX_BYTES + 1)
                if data and len(data) <= _MAX_BYTES:
                    try:
                        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                        cpath.write_bytes(data)
                    except Exception:
                        logger.debug("nie udalo sie zapisac ikony do cache: %s", url)
                elif data and len(data) > _MAX_BYTES:
                    logger.warning("ikona przekracza limit rozmiaru, pomijam: %s", url)
                    data = None
        except Exception as exc:
            logger.debug("nie udalo sie pobrac ikony %s: %s", url, exc)
            data = None
        GLib.idle_add(self._on_fetched, url, data)

    def _on_fetched(self, url: str, data: Optional[bytes]) -> bool:
        self._inflight.discard(url)
        texture: Optional[Gdk.Texture] = None
        if data:
            try:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(data)
                loader.close()
                pixbuf = loader.get_pixbuf()
                if pixbuf is not None:
                    texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            except Exception as exc:
                logger.debug("nie udalo sie zdekodowac ikony %s: %s", url, exc)
                texture = None
        if texture is not None:
            self._mem[url] = texture
        else:
            self._failed.add(url)
        for cb in self._waiters.pop(url, []):
            try:
                cb(texture)
            except Exception:
                logger.exception("blad callbacku ikony")
        return False


def make_icon_widget(
    fallback_icon_name: str,
    pixel_size: int,
    url: Optional[str] = None,
) -> Gtk.Widget:
    """Buduje Gtk.Picture/Gtk.Image z fallbackiem na symboliczna ikone,
    ktora automatycznie podmienia sie na logo stacji gdy zostanie pobrane.
    """
    stack = Gtk.Stack()
    stack.set_size_request(pixel_size, pixel_size)

    fallback = Gtk.Image.new_from_icon_name(fallback_icon_name)
    fallback.set_pixel_size(pixel_size)
    stack.add_named(fallback, "fallback")

    picture = Gtk.Picture()
    picture.set_can_shrink(True)
    picture.set_content_fit(Gtk.ContentFit.CONTAIN)
    picture.set_size_request(pixel_size, pixel_size)
    stack.add_named(picture, "logo")
    stack.set_visible_child_name("fallback")

    def _apply(texture: Optional[Gdk.Texture]) -> None:
        if texture is None:
            return
        picture.set_paintable(texture)
        stack.set_visible_child_name("logo")

    if url:
        cached = IconCache.get().get_texture(url)
        if cached is not None:
            _apply(cached)
        else:
            IconCache.get().request(url, _apply)

    return stack


def update_icon_widget(stack: Gtk.Widget, url: Optional[str]) -> None:
    """Aktualizuje widget stworzony przez make_icon_widget() dla nowego URL
    (np. przy zmianie kanalu w OSD - ten sam widget, inna stacja)."""
    if not isinstance(stack, Gtk.Stack):
        return
    picture = stack.get_child_by_name("logo")
    if picture is None:
        return

    def _apply(texture: Optional[Gdk.Texture]) -> None:
        if texture is None:
            stack.set_visible_child_name("fallback")
            return
        picture.set_paintable(texture)
        stack.set_visible_child_name("logo")

    stack.set_visible_child_name("fallback")
    if url:
        cached = IconCache.get().get_texture(url)
        if cached is not None:
            _apply(cached)
        else:
            IconCache.get().request(url, _apply)
