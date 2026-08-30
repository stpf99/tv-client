"""
Warstwa HbbTV oparta o WebKitGTK, nakladana na wideo GStreamer w tym samym
Gtk.Overlay co OSD (patrz ui/live_view.py: self.overlay).

Architektura:

    Gtk.Overlay
      |- self.picture / self.video_widget   (warstwa GStreamer, na spodzie)
      |- OSD (istniejacy)                    (nazwa audycji, pasek postepu)
      `- HbbtvOverlay.webview                (ta warstwa - nad wideo, pod OSD
                                               lub nad OSD zaleznie od trybu)

WebKit2.WebView ma domyslnie przezroczyste tlo tylko jesli jawnie ustawimy
`set_background_color` na (0,0,0,0) - inaczej renderuje sie na bialo/czarno
i zasloni wideo nawet gdy HTML/CSS strony jest przezroczysty.

Komunikacja JS <-> Python idzie przez WebKit2.UserContentManager:
  - JS -> Python: window.webkit.messageHandlers.oipf.postMessage({...})
    (obslugiwane w _on_script_message)
  - Python -> JS: self.webview.run_javascript(f"window.__hbbtvHost.foo(...)")

Wstrzykiwany jest polyfill z data/oipf-polyfill.js jako UserScript, ladowany
PRZED zawartoscia strony (WebKit2.UserScriptInjectionTime.START_OF_DOCUMENT),
tak jak realny STB middleware wstrzykuje obiekty OIPF zanim JS aplikacji
zacznie dzialac.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")  # WebKitGTK 6.0 = binding dla GTK4
from gi.repository import Gtk, GLib, WebKit  # noqa: E402

from tvh.hbbtv import HbbtvApp  # noqa: E402

logger = logging.getLogger(__name__)

_POLYFILL_PATH = Path(__file__).resolve().parent.parent / "data" / "oipf-polyfill.js"

# MIME HbbTV / CE-HTML – WebKit ich nie renderuje natywnie.
_HBBTV_MIMES = (
    "application/vnd.hbbtv.xhtml+xml",
    "application/ce-html+xml",
)

def _is_hbbtv_mime(mime: str) -> bool:
    m = (mime or "").lower().strip()
    if not m:
        return False
    return (
        "hbbtv" in m
        or "ce-html" in m
        or m in _HBBTV_MIMES
    )

# Mapowanie GDK keyval -> VK_* z CEA-2014 / HbbTV 2.0.3.
# Zgodne z grok-workspace: src/lib/hbbtv/keycodes.ts
GDK_TO_VK = {
    # kolory (pilot RC) - w tv-client to prawdopodobniej F1-F4 lub dedykowane
    # klawisze OSD; podmien nazwy stalych GDK jesli masz inny remapping.
    "F1": 403,  # VK_RED
    "F2": 404,  # VK_GREEN
    "F3": 405,  # VK_YELLOW
    "F4": 406,  # VK_BLUE
    "Up": 38,
    "Down": 40,
    "Left": 37,
    "Right": 39,
    "Return": 13,
    "KP_Enter": 13,
    "Escape": 461,  # VK_BACK
    "BackSpace": 461,
    "space": 402,  # VK_PLAY_PAUSE
    "s": 413,  # VK_STOP
    "i": 457,  # VK_INFO
    "0": 48, "1": 49, "2": 50, "3": 51, "4": 52,
    "5": 53, "6": 54, "7": 55, "8": 56, "9": 57,
}

# Keyset bitmask (Application.privateData.keyset) - taki sam jak w grok
KEYSET_RED = 0x1
KEYSET_GREEN = 0x2
KEYSET_YELLOW = 0x4
KEYSET_BLUE = 0x8
KEYSET_NAVIGATION = 0x10
KEYSET_VCR = 0x20
KEYSET_INFO = 0x80
KEYSET_NUMERIC = 0x100

_KEYSET_OWNERS = {
    403: KEYSET_RED, 404: KEYSET_GREEN, 405: KEYSET_YELLOW, 406: KEYSET_BLUE,
    38: KEYSET_NAVIGATION, 40: KEYSET_NAVIGATION,
    37: KEYSET_NAVIGATION, 39: KEYSET_NAVIGATION,
    13: KEYSET_NAVIGATION, 461: KEYSET_NAVIGATION,
    413: KEYSET_VCR, 415: KEYSET_VCR, 19: KEYSET_VCR, 402: KEYSET_VCR,
    457: KEYSET_INFO,
}
for _vk in range(48, 58):
    _KEYSET_OWNERS[_vk] = KEYSET_NUMERIC


def _keyset_owns(mask: int, vk: int) -> bool:
    bit = _KEYSET_OWNERS.get(vk)
    return bool(bit) and bool(mask & bit)


class HbbtvOverlay:
    """
    Opakowuje WebKit.WebView tak, by mozna go bylo wsadzic jako kolejne
    dziecko istniejacego Gtk.Overlay (patrz LiveView.overlay w
    ui/live_view.py) i sterowac cyklem zycia aplikacji HbbTV.

    Uzycie w LiveView.__init__ (po utworzeniu self.overlay i self.picture):

        self.hbbtv = HbbtvOverlay(
            on_fullscreen_request=self._on_hbbtv_fullscreen,
            on_set_channel_request=self._on_hbbtv_set_channel,
        )
        self.overlay.add_overlay(self.hbbtv.widget)

    Uruchamianie po wykryciu/kliknieciu aplikacji z listy (HbbtvApp z
    tvh/hbbtv.py):

        self.hbbtv.launch(hbbtv_app)

    Zamykanie (np. VK_BACK dojdzie do hosta jako specjalna komenda, albo
    uzytkownik wybierze "Wyjdz z aplikacji" w UI):

        self.hbbtv.close()
    """

    def __init__(
        self,
        on_fullscreen_request: Optional[Callable[[bool], None]] = None,
        on_set_channel_request: Optional[Callable[[dict], None]] = None,
        on_show_hide: Optional[Callable[[bool], None]] = None,
        on_keyset_changed: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._app: Optional[HbbtvApp] = None
        self._keyset_mask = 0
        self._visible = False

        self.on_fullscreen_request = on_fullscreen_request
        self.on_set_channel_request = on_set_channel_request
        self.on_show_hide = on_show_hide
        # Wywolywane za kazdym razem, gdy _keyset_mask sie zmienia (launch()
        # -> domyslny RED, _on_script_message() -> setKeyset z JS, close()
        # -> 0) - UI (belka OSD) uzywa tego do pokazania/schowania 4
        # kolorowych przyciskow zamiast pollowac keyset_mask recznie.
        self.on_keyset_changed = on_keyset_changed

        # --- UserContentManager: wstrzykiwanie polyfillu + kanal wiadomosci
        self._ucm = WebKit.UserContentManager()
        self._ucm.register_script_message_handler("oipf")
        self._ucm.connect(
            "script-message-received::oipf", self._on_script_message
        )

        # 1) Guard: polyfills.min.js z hbb-prod.tvp.pl (polyfill.io v4) przy
        #    nieudanym feature-tescie Set/Map w WebKitGTK NADPISUJE natywne
        #    konstruktory. MobX w Nuxt wtedy pada:
        #    parents.add / dirtyChildren.add / NewDePids.add is not a function.
        #    Naprawiamy toStringTag (zeby test przeszedl).
        #
        #    UWAGA: pierwsza wersja tego guarda blokowala self.Set/Map na
        #    stale przez defineProperty(configurable:false). To dzialalo
        #    dla CDA Premium/Sklep Kapitan/hbbn.tvp.pl (zero bledow), ale
        #    NIE naprawilo bledu na apps.vod.tvp.pl / hbb-prod.tvp.pl
        #    (te same bledy .add is not a function nadal wystepowaly) -
        #    prawdopodobnie dlatego, ze jesli polyfill.io tam probuje
        #    podmienic Set przez Object.defineProperty (a nie zwykle "="),
        #    to na non-configurable property TO RZUCA TypeError - a jesli
        #    ich kod nie ma tego w try/catch, mogl w tym miejscu przerwac
        #    reszte wlasnej inicjalizacji (czyli "hard lock" mogl bardziej
        #    szkodzic niz pomagac na tym konkretnym bundlu). Wracamy do
        #    configurable:true (nic nie rzuca), ale restore() dziala dluzej
        #    (do 5s zamiast 500ms) + logujemy stan przez console.warn, zeby
        #    NASTEPNY log dal twarde dane zamiast kolejnej hipotezy.
        #
        #    v2: log z apps.vod.tvp.pl pokazal restore() konczace sie
        #    "Set==native: true" (self.Set NIE zostal podmieniony), a mimo
        #    to MobX dalej pada na this.newDepIds.add is not a function.
        #    Skoro nawet TWARDA blokada self.Set (v1) tego nie naprawila,
        #    problem nie polega na identycznosci self.Set - polega na tym,
        #    ze feature-detect w tym bundlu najwyrazniej testuje
        #    /native code/.test(Set.toString()) (klasyczny wzorzec
        #    "czy to jest natywna implementacja"), a nie samo typeof/
        #    referencje. Jesli JavaScriptCore w WebKitGTK 6.0 zwraca
        #    Function.prototype.toString() w formacie, ktorego ten regex
        #    nie rozpoznaje, HasNativeSet wychodzi false NIEZALEZNIE od
        #    tego, czy self.Set jest natywny - bundle uzywa wtedy wlasnego
        #    fallback-shape (metody set/get, nie add) i dlatego .add pada
        #    nawet gdy self.Set===NSet caly czas. Dlatego dodatkowo
        #    nadpisujemy Function.prototype.toString tak, by dla
        #    Set/Map/WeakMap/WeakSet zwracal string w formacie
        #    "function Foo() { [native code] }" - to jedyne, co ten typ
        #    testu sprawdza, i nie ma wplywu na realne dzialanie klas.
        _SET_GUARD_JS = (
            "(function(){try{"
            "var NSet=self.Set,NMap=self.Map,NWM=self.WeakMap,NWS=self.WeakSet;"
            "function tag(C,t){if(!C||!C.prototype||!self.Symbol||!self.Symbol.toStringTag)return;"
            "try{var o=new C();if(o[self.Symbol.toStringTag]===undefined)"
            "Object.defineProperty(C.prototype,self.Symbol.toStringTag,"
            "{get:function(){return t},configurable:true})}catch(e){}}"
            "tag(NSet,'Set');tag(NMap,'Map');tag(NWM,'WeakMap');tag(NWS,'WeakSet');"
            # --- v2: wymus 'native code' w Function.prototype.toString() dla
            # Set/Map/WeakMap/WeakSet, bo feature-detect testujacy string
            # (a nie identycznosc obiektu) omija cala reszte guarda.
            "var NFuncToString=Function.prototype.toString;"
            "var nativeNames=[];"
            "[[NSet,'Set'],[NMap,'Map'],[NWM,'WeakMap'],[NWS,'WeakSet']].forEach("
            "function(p){if(p[0])nativeNames.push(p[0])});"
            "try{Function.prototype.toString=function(){"
            "if(nativeNames.indexOf(this)!==-1){"
            "var n=this.name||'';return 'function '+n+'() { [native code] }'"
            "}"
            "return NFuncToString.call(this)"
            "};"
            "console.warn('[TVH-GUARD] Function.prototype.toString shim aktywny (native-code test)');"
            "}catch(e){console.warn('[TVH-GUARD] toString shim blad: '+e)}"
            "var restoreCount=0;"
            "function restore(){try{"
            "var changed=false;"
            "if(NSet&&self.Set!==NSet){self.Set=NSet;changed=true}"
            "if(NMap&&self.Map!==NMap){self.Map=NMap;changed=true}"
            "if(NWM&&self.WeakMap!==NWM){self.WeakMap=NWM;changed=true}"
            "if(NWS&&self.WeakSet!==NWS){self.WeakSet=NWS;changed=true}"
            "restoreCount++;"
            "if(changed)console.warn('[TVH-GUARD] podmieniono Set/Map/Weak* - przywrocono (proba #'+restoreCount+')');"
            "}catch(e){console.warn('[TVH-GUARD] restore() blad: '+e)}}"
            "restore();"
            "if(self.document){document.addEventListener('DOMContentLoaded',restore);"
            "var delays=[0,50,100,250,500,1000,2000,3000,5000];"
            "for(var i=0;i<delays.length;i++)setTimeout(restore,delays[i]);"
            "setTimeout(function(){console.warn("
            "'[TVH-GUARD] koniec monitorowania po 5s, Set==native: '+(self.Set===NSet)"
            ")},5000);"
            "}"
            "}catch(e){}})();"
        )
        self._ucm.add_script(WebKit.UserScript.new(
            _SET_GUARD_JS,
            WebKit.UserContentInjectedFrames.TOP_FRAME,
            WebKit.UserScriptInjectionTime.START,
            None,
            None,
        ))
        logger.info("HbbTV: Set/Map guard wstrzykniety (ochrona przed polyfills.min.js TVP)")

        # 2) Polyfill OIPF – domyslnie wlaczony. Wylacz: TVH_HBBTV_NOPOLYFILL=1
        self._polyfill_enabled = not bool(os.environ.get("TVH_HBBTV_NOPOLYFILL"))
        try:
            polyfill_src = _POLYFILL_PATH.read_text(encoding="utf-8") if self._polyfill_enabled else ""
        except OSError as exc:
            logger.error("Nie mozna wczytac oipf-polyfill.js: %s", exc)
            polyfill_src = ""

        if polyfill_src:
            # TOP_FRAME: nie wstrzykuj do iframe'ow (portale TVP maja ich duzo)
            user_script = WebKit.UserScript.new(
                polyfill_src,
                WebKit.UserContentInjectedFrames.TOP_FRAME,
                WebKit.UserScriptInjectionTime.START,
                None,
                None,
            )
            self._ucm.add_script(user_script)
            logger.info("HbbTV: OIPF polyfill wstrzykiwany (TOP_FRAME, START)")
        else:
            logger.info("HbbTV: OIPF polyfill WYLACZONY (TVH_HBBTV_NOPOLYFILL)")

        # --- WebView -------------------------------------------------------
        # Uwaga: izolacja per-aplikacyjna (osobna sesja sieciowa/cache) w
        # WebKitGTK 6.0 idzie przez WebKit.NetworkSession, ale API rozni
        # sie miedzy wersjami dystrybucyjnymi pakietu - pomijamy to w tym
        # szkicu i uzywamy domyslnej sesji. Doprecyzuj jesli potrzebujesz
        # pelnej izolacji miedzy aplikacjami HbbTV.
        self.webview = WebKit.WebView(user_content_manager=self._ucm)

        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(
            bool(os.environ.get("TVH_HBBTV_DEVTOOLS"))
        )
        settings.set_javascript_can_open_windows_automatically(False)
        # Logi konsoli JS na stdout ZAWSZE wlaczone (nie tylko pod
        # TVH_HBBTV_DEBUG) na czas diagnozy problemu "strona sie nie
        # renderuje bez zadnego bledu" - to jedyne miejsce, gdzie bledy typu
        # CSP/mixed-content/blad JS aplikacji w ogole trafiaja do logu hosta.
        settings.set_enable_write_console_messages_to_stdout(True)
        # HbbTV apps czesto polegaja na autoplay dla wbudowanego <video>
        # (broadband playback) - AIT-signalled ograniczenia bezpieczenstwa
        # (sandboxing per-app) sa poza zakresem tego szkicu.
        settings.set_media_playback_requires_user_gesture(False)
        # UA identyfikujacy terminal HbbTV – wiele portali (TVP i inne)
        # sprawdza User-Agent albo serwuje inna sciezke dla „przegladarek”.
        # Nadpisanie przez TVH_HBBTV_UA=... jesli potrzeba.
        self._hbbtv_ua = os.environ.get(
            "TVH_HBBTV_UA",
            "HbbTV/1.4.1 (+DRM; Linux; tv-client; 1.0;;) WebKit",
        )
        try:
            settings.set_user_agent(self._hbbtv_ua)
        except Exception as exc:  # pragma: no cover
            logger.warning("set_user_agent niedostepne: %s", exc)

        # Gdy True – nastepne RESPONSE z HbbTV MIME ladujemy przez
        # load_bytes(..., "text/html") zamiast use() (unikamy parsera XML,
        # ktory psuje nowoczesne portale Nuxt/Vue).
        self._html_reload_uris: set[str] = set()

        # Przezroczyste tlo - inaczej WebView zasloni wideo GStreamer pod spodem
        try:
            from gi.repository import Gdk
            transparent = Gdk.RGBA()
            transparent.parse("rgba(0,0,0,0)")
            self.webview.set_background_color(transparent)
        except Exception as exc:  # pragma: no cover - zalezne od wersji API
            logger.warning("set_background_color niedostepne: %s", exc)

        self.webview.set_visible(False)
        self.webview.set_can_focus(True)
        self.webview.set_hexpand(True)
        self.webview.set_vexpand(True)
        self.webview.set_halign(Gtk.Align.FILL)
        self.webview.set_valign(Gtk.Align.FILL)

        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.webview.add_controller(key_ctrl)

        # Diagnostyka ladowania - bez tego blad sieci/renderowania (np. brak
        # web-process WebKit, DNS, TLS, CSP) przechodzi calkowicie po cichu:
        # load_uri() nie rzuca wyjatku, po prostu nic sie nie wyswietla.
        self.webview.connect("load-changed", self._on_load_changed)
        self.webview.connect("load-failed", self._on_load_failed)
        self.webview.connect("web-process-terminated", self._on_web_process_terminated)
        # Krytyczne dla HbbTV: serwery (TVP i in.) zwracaja
        # Content-Type: application/vnd.hbbtv.xhtml+xml – WebKit tego nie
        # zna i przerywa load bledem PolicyError 102
        # (FRAME_LOAD_INTERRUPTED_BY_POLICY_CHANGE). Wymuszamy use().
        self.webview.connect("decide-policy", self._on_decide_policy)

        # Widget do wpiecia w Gtk.Overlay.add_overlay(...)
        self.widget = self.webview

    def _on_load_changed(self, webview, load_event) -> None:
        logger.info("HbbTV: load-changed=%s uri=%s", load_event, webview.get_uri())

    def _on_load_failed(self, webview, load_event, failing_uri, error) -> bool:
        logger.error("HbbTV: load-failed uri=%s error=%s", failing_uri, error)
        return False  # False = pozwol WebKit pokazac wlasna strone bledu

    def _on_web_process_terminated(self, webview, reason) -> None:
        logger.error("HbbTV: web-process-terminated reason=%s (WebKitGTK web-process padl - "
                     "sprawdz sandbox/GPU: WEBKIT_DISABLE_COMPOSITING_MODE=1, "
                     "WEBKIT_FORCE_SANDBOX=0, brak /usr/libexec/webkitgtk-6.0/WebKitWebProcess)",
                     reason)

    def _on_decide_policy(self, webview, decision, decision_type) -> bool:
        """HbbTV MIME: WebKit nie zna application/vnd.hbbtv.xhtml+xml.

        1) PolicyError 102 – bez use()/load_bytes load jest przerywany.
        2) Sam use() zostawia dokument w trybie XML/XHTML – nowoczesne
           portale Nuxt/Vue (hbb-prod.tvp.pl) padaja wtedy na
           TypeError: dirtyChildren.add / parents.add.

        Dla glownej ramki: ignorujemy policy i dociagamy body jako text/html
        przez load_bytes (parser HTML5). Dla subresource – use().
        """
        if decision_type != WebKit.PolicyDecisionType.RESPONSE:
            return False
        try:
            response = decision.get_response()
            mime = (response.get_mime_type() or "").lower().strip()
            request = decision.get_request()
            uri = request.get_uri() if request else ""
        except Exception as exc:
            logger.debug("HbbTV: decide-policy: nie odczytano response: %s", exc)
            return False

        if not _is_hbbtv_mime(mime):
            return False

        # Czy to glowna ramka? (API rozni sie lekko miedzy wersjami WebKitGTK)
        is_main = True
        try:
            if hasattr(decision, "is_main_frame"):
                is_main = bool(decision.is_main_frame())
            elif hasattr(decision, "get_frame_name"):
                # pusta nazwa = main
                is_main = not bool(decision.get_frame_name())
        except Exception:
            is_main = True

        if is_main and uri and uri not in self._html_reload_uris:
            # Zapobiegamy petli: ten URI ladujemy raz jako HTML.
            self._html_reload_uris.add(uri)
            logger.info(
                "HbbTV: decide-policy: MIME %r main-frame → load as text/html (%s)",
                mime,
                uri,
            )
            try:
                decision.ignore()
            except Exception:
                try:
                    decision.download()
                except Exception:
                    pass
            self._fetch_and_load_html(uri)
            return True

        logger.info(
            "HbbTV: decide-policy: MIME %r → use() (subresource/fallback)",
            mime,
        )
        try:
            decision.use()
        except Exception as exc:
            logger.error("HbbTV: decision.use() nieudane: %s", exc)
            return False
        return True

    def _fetch_and_load_html(self, url: str) -> None:
        """Pobiera URL (z redirectami) i wstrzykuje do WebView jako text/html."""
        ua = getattr(self, "_hbbtv_ua", "HbbTV/1.4.1")

        def worker() -> None:
            try:
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": ua,
                        "Accept": "text/html,application/xhtml+xml,application/vnd.hbbtv.xhtml+xml,*/*;q=0.8",
                    },
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = resp.read()
                    final_url = resp.geturl() or url
                    ctype = resp.headers.get("Content-Type", "")
                logger.info(
                    "HbbTV: fetch OK %s → %s (%d B, %s)",
                    url,
                    final_url,
                    len(data),
                    ctype.split(";")[0].strip(),
                )
                GLib.idle_add(self._commit_html_bytes, data, final_url)
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
                logger.error("HbbTV: fetch HTML nieudany (%s): %s – fallback load_uri", url, exc)
                GLib.idle_add(self.webview.load_uri, url)

        threading.Thread(target=worker, daemon=True, name="hbbtv-fetch-html").start()

    def _commit_html_bytes(self, data: bytes, base_uri: str) -> bool:
        """Callback idle: load_bytes jako text/html (parser HTML5, nie XML)."""
        try:
            # Oznacz finalny URI, zeby decide-policy nie zapetlilo sie.
            self._html_reload_uris.add(base_uri)
            gbytes = GLib.Bytes.new(data)
            # load_bytes(mime, encoding, base_uri) – wymuszamy text/html
            self.webview.load_bytes(gbytes, "text/html", "UTF-8", base_uri)
            logger.info("HbbTV: load_bytes text/html base=%s (%d B)", base_uri, len(data))
        except Exception as exc:
            logger.error("HbbTV: load_bytes nieudane: %s – fallback load_uri", exc)
            try:
                self.webview.load_uri(base_uri)
            except Exception:
                pass
        return False  # nie powtarzaj idle

    # ------------------------------------------------------------------
    # Cykl zycia aplikacji
    # ------------------------------------------------------------------
    def launch(self, app: HbbtvApp) -> None:
        """Laduje URL aplikacji HbbTV (HbbtvApp.url z tvh/hbbtv.py) i
        pokazuje warstwe. Domyslny keyset = tylko RED (prompt), tak jak
        realny terminal pokazuje po starcie tylko czerwony przycisk
        "uruchom aplikacje"."""
        if not app.url:
            logger.warning("HbbtvApp %s nie ma URL - pomijam launch", app.uid)
            return
        self._app = app
        self._keyset_mask = KEYSET_RED
        self._html_reload_uris.clear()
        logger.info("HbbTV: uruchamiam %s (%s)", app.display_name, app.url)
        self.webview.load_uri(app.url)
        self._set_visible(True)
        self._notify_keyset()
        logger.info(
            "HbbTV: po load_uri() webview.get_uri()=%s visible=%s mapped=%s realized=%s",
            self.webview.get_uri(), self.webview.get_visible(),
            self.webview.get_mapped(), self.webview.get_realized(),
        )

    def close(self) -> None:
        if not self._app:
            return
        logger.info("HbbTV: zamykam %s", self._app.display_name)
        self.webview.load_uri("about:blank")
        self._app = None
        self._keyset_mask = 0
        self._html_reload_uris.clear()
        self._set_visible(False)
        self._notify_keyset()

    def _notify_keyset(self) -> None:
        if self.on_keyset_changed:
            self.on_keyset_changed(self._keyset_mask)

    @property
    def keyset_mask(self) -> int:
        return self._keyset_mask

    @property
    def is_running(self) -> bool:
        return self._app is not None

    @property
    def is_visible(self) -> bool:
        return self._visible

    def show(self) -> None:
        """Pokazuje warstwe WebView bez przeladowania aplikacji - wywolywane
        z przycisku toggle na belce OSD (patrz LiveView._on_hbbtv_toggle)."""
        if self.is_running:
            self._set_visible(True)

    def hide(self) -> None:
        """Chowa warstwe WebView (aplikacja dalej dziala w tle, tylko
        niewidoczna) - odpowiednik Application.hide() wywolanego z
        zewnatrz aplikacji."""
        if self.is_running:
            self._set_visible(False)

    def notify_channel_changed(self, channel_payload: dict) -> None:
        """Wolane z LiveView gdy zmieni sie kanal na zywo - HbbTV app moze
        chciec zareagowac (np. przeladowac dane portalu). Odpowiednik
        window.onHbbtvChannel(...) z grok-workspace app.js."""
        if not self.is_running:
            return
        js = "window.__hbbtvHost && window.__hbbtvHost.notifyChannel(%s)" % (
            json.dumps(channel_payload)
        )
        self.webview.evaluate_javascript(js, -1, None, None, None, None, None)

    def notify_broadcast_play_state(self, state: int) -> None:
        """0=unrealized 1=connecting 2=presenting 3=stopped - przekazywane
        z player/stream_controller.py przy zmianach stanu GStreamera."""
        if not self.is_running:
            return
        js = f"window.__hbbtvHost && window.__hbbtvHost.setBroadcastPlayState({state})"
        self.webview.evaluate_javascript(js, -1, None, None, None, None, None)

    # ------------------------------------------------------------------
    # JS -> Python  (window.webkit.messageHandlers.oipf.postMessage)
    # ------------------------------------------------------------------
    def _on_script_message(self, _ucm, js_value) -> None:
        try:
            # WebKitGTK 6.0: sygnal "script-message-received::<name>" daje
            # bezposrednio JSC.Value (nie opakowany WebKitJavascriptResult
            # jak w starszym WebKit2GTK 4.x/4.1, stad brak get_js_value()
            # na tym obiekcie - to byla przyczyna "'Value' object has no
            # attribute 'get_js_value'" w logu).
            payload = json.loads(js_value.to_json(0))
        except Exception as exc:
            logger.warning("HbbTV: nieprawidlowa wiadomosc z JS: %s", exc)
            return

        method = payload.get("method")
        params = payload.get("params") or {}
        logger.info("HbbTV <- JS: %s %s", method, params)

        if method == "ready":
            pass  # polyfill zaladowany; nic do zrobienia
        elif method == "setKeyset":
            self._keyset_mask = int(params.get("mask", 0))
            logger.info("HbbTV: setKeyset mask=0x%02x", self._keyset_mask)
            self._notify_keyset()
        elif method == "show":
            self._set_visible(True)
        elif method == "hide":
            self._set_visible(False)
        elif method == "destroy":
            self.close()
        elif method == "setFullScreen":
            if self.on_fullscreen_request:
                self.on_fullscreen_request(bool(params.get("value")))
        elif method == "setChannel":
            if self.on_set_channel_request:
                self.on_set_channel_request(params.get("channel") or {})
        elif method == "bindToCurrentChannel":
            pass  # no-op: LiveView juz gra biezacy kanal
        elif method == "stop":
            # broadcast.stop() - apka (np. apps.vod.tvp.pl) przechodzi na
            # wlasne odtwarzanie OTT i "zwalnia" nasz tuner. Na razie samo
            # logujemy - LiveView i tak nie musi nic robic, bo nie mamy tu
            # osobnego video-plane do wygaszania (wideo TVH jest pod
            # nakladka WebView, nie obok niej).
            logger.info("HbbTV: broadcast.stop() - aplikacja przejmuje odtwarzanie")
        elif method in ("avPlay", "avStop", "avSeek"):
            logger.info("HbbTV A/V control: %s %s (broadband playback poza zakresem szkicu)", method, params)
        elif method == "createApplication":
            logger.info("HbbTV: aplikacja potomna zignorowana: %s", params.get("url"))
        else:
            logger.debug("HbbTV: nieobslugiwana metoda %s", method)

    # ------------------------------------------------------------------
    # Klawiatura / pilot -> JS (tylko jesli keyset aplikacji je "posiada")
    # ------------------------------------------------------------------
    def _on_key_pressed(self, _ctrl, keyval, _keycode, _state) -> bool:
        if not self.is_running:
            return False
        from gi.repository import Gdk

        name = Gdk.keyval_name(keyval) or ""
        vk = GDK_TO_VK.get(name)
        if vk is None:
            return False
        return self.dispatch_vk(vk)

    def dispatch_vk(self, vk: int) -> bool:
        """Wysyla kod VK_* (CEA-2014 / HbbTV 2.0.3) do aplikacji, o ile ta
        zadeklarowala go w swoim keysecie (Application.privateData.keyset).
        Wspolna sciezka dla fizycznej klawiatury/pilota (_on_key_pressed)
        ORAZ dla klikniecia w kolorowy przycisk na belce OSD (patrz
        ui/live_view.py: _on_hbbtv_color_pressed) - jeden kanal dispatchu,
        zeby oba wejscia zachowywaly sie identycznie. Zwraca False (i nic
        nie wysyla), jesli aplikacja aktualnie nie "posiada" tego klawisza -
        UI powinno wtedy i tak nie pokazywac/aktywowac odpowiadajacego
        przycisku (patrz on_keyset_changed), wiec to glownie zabezpieczenie
        przed klikiem w niesynchronizowany jeszcze stan."""
        if not self.is_running:
            return False
        if not _keyset_owns(self._keyset_mask, vk):
            # Aplikacja nie zadeklarowala tego klawisza w keyset - puszczamy
            # dalej do reszty UI tv-client (np. VK_BACK moze wtedy zamknac
            # nakladke zamiast byc skonsumowane przez appke).
            logger.info(
                "HbbTV: dispatch_vk(%s) odrzucone - aplikacja nie ma tego "
                "klawisza w keysecie (mask=0x%02x)", vk, self._keyset_mask,
            )
            return False

        logger.info("HbbTV: dispatch_vk(%s) -> window.__hbbtvHost.dispatchKey(%s)", vk, vk)
        self.webview.evaluate_javascript(
            f"window.__hbbtvHost && window.__hbbtvHost.dispatchKey({vk})",
            -1, None, None, None, self._on_dispatch_vk_result, vk,
        )
        return True  # zdarzenie skonsumowane przez aplikacje HbbTV

    def _on_dispatch_vk_result(self, webview, result, vk) -> None:
        """Callback do evaluate_javascript - bez niego nie widzielismy, czy
        wywolanie window.__hbbtvHost.dispatchKey(...) w ogole sie udalo
        (np. gdy __hbbtvHost nie istnieje bo polyfill jeszcze sie nie
        wykonal w danym dokumencie, albo strona zdazyla juz nawigowac
        gdzies indziej)."""
        try:
            js_value = webview.evaluate_javascript_finish(result)
        except Exception as exc:
            logger.error("HbbTV: dispatch_vk(%s) blad JS: %s", vk, exc)
            return
        value = js_value.to_string() if js_value is not None else None
        logger.info("HbbTV: dispatch_vk(%s) JS zwrocil: %s", vk, value)

    # ------------------------------------------------------------------
    def _set_visible(self, visible: bool) -> None:
        self._visible = visible
        self.webview.set_visible(visible)
        if self.on_show_hide:
            self.on_show_hide(visible)
