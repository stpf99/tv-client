/**
 * OIPF DAE polyfill dla tv-client (WebKitGTK host).
 *
 * Wersja portowana z grok-workspace (.vercel/output/static/hbbtv/oipf-polyfill.js).
 * Oryginal komunikowal sie z hostem przez `parent.postMessage` (bo host byl
 * inna ramka w tej samej przegladarce - React/iframe). Tutaj hosta nie ma
 * w DOM-ie (to proces Python/GTK), wiec komunikacja idzie przez WebKit
 * UserContentManager:
 *
 *   JS -> Python:  window.webkit.messageHandlers.oipf.postMessage({...})
 *   Python -> JS:  webview.run_javascript("window.__hbbtvHost.<fn>(...)")
 *
 * Implementuje minimalne OIPF DAE potrzebne dla prostych aplikacji AIT:
 * ApplicationManager, <object type="application/oipfApplicationManager">,
 * video/broadcast (<object type="video/broadcast">), A/V control
 * (<object type="video/mp4"> itp.), KeyEvent, Configuration.
 */
(function () {
  "use strict";

  function send(method, params) {
    try {
      window.webkit.messageHandlers.oipf.postMessage({
        method: method,
        params: params || {},
      });
    } catch (e) {
      // WebKit messageHandlers niedostepne (np. podglad w zwyklej przegladarce
      // podczas developmentu) - po cichu ignorujemy.
    }
  }

  // --- KeyEvent / VK_* --------------------------------------------------
  var KeyEvent = {
    VK_RED: 403,
    VK_GREEN: 404,
    VK_YELLOW: 405,
    VK_BLUE: 406,
    VK_UP: 38,
    VK_DOWN: 40,
    VK_LEFT: 37,
    VK_RIGHT: 39,
    VK_ENTER: 13,
    VK_BACK: 461,
    VK_STOP: 413,
    VK_PLAY: 415,
    VK_PAUSE: 19,
    VK_PLAY_PAUSE: 402,
    VK_INFO: 457,
  };
  window.KeyEvent = KeyEvent;
  for (var k in KeyEvent) window[k] = KeyEvent[k];

  // --- Keyset -------------------------------------------------------------
  var keysetObj = {
    value: 0,
    setValue: function (v) {
      this.value = v | 0;
      send("setKeyset", { mask: this.value });
      return this.value;
    },
    getValue: function () {
      return this.value;
    },
  };

  // --- ApplicationManager / Application ------------------------------------
  var application = {
    privateData: { keyset: keysetObj },
    show: function () {
      send("show");
    },
    hide: function () {
      send("hide");
    },
    destroyApplication: function () {
      send("destroy");
    },
    createApplication: function (uri /*, createChild, showOnCreate */) {
      send("createApplication", { url: uri });
      return null; // aplikacje potomne poza zakresem tego polyfillu
    },
  };

  var appManager = {
    getOwnerApplication: function (/* doc */) {
      return application;
    },
  };

  // --- video/broadcast ------------------------------------------------------
  // playState: 0=unrealized 1=connecting 2=presenting 3=stopped
  var broadcast = {
    playState: 0,
    onFullScreenChange: null,
    onChannelChangeSucceeded: null,
    onChannelChangeError: null,
    bindToCurrentChannel: function () {
      send("bindToCurrentChannel");
      return null;
    },
    stop: function () {
      // apps.vod.tvp.pl woła to przy przejsciu na wlasne odtwarzanie OTT
      // (zeby "zwolnic" tuner/video-plane) - brak tej metody dawal
      // "e.broadcastNode.stop is not a function".
      send("stop");
      this.playState = 3; // stopped
    },
    setFullScreen: function (val) {
      send("setFullScreen", { value: !!val });
    },
    setChannel: function (ch) {
      send("setChannel", { channel: ch });
    },
  };

  // --- A/V control (<object type="video/mp4"> itp. - broadband playback) ---
  function makeAvControl() {
    var el = {
      playState: 0,
      onPlayStateChange: null,
      data: "",
      play: function (speed) {
        send("avPlay", { speed: typeof speed === "number" ? speed : 1 });
      },
      stop: function () {
        send("avStop");
      },
      seek: function (ms) {
        send("avSeek", { position: ms });
      },
    };
    return el;
  }

  // --- Configuration --------------------------------------------------------
  var configuration = {
    configuration: {
      preferredAudioLanguage: "pol",
      preferredSubtitleLanguage: "pol",
      preferredUILanguage: "pol",
      countryId: "PL",
    },
    localSystem: { deviceID: "tv-client" },
  };

  // --- Rejestracja obiektow OIPF wstawionych jako <object> w HTML ----------
  // Prawdziwe przegladarki STB podmieniaja <object type="application/oipf..">
  // na natywny obiekt. Tutaj upraszczamy: po zaladowaniu strony podmieniamy
  // atrybuty/wlasciwosci elementow o znanych `type`, tak by kod aplikacji
  // (np. document.getElementById("appmgr").getOwnerApplication(...)) dzialal
  // bez zmian.
  function wireObjectElements() {
    var objs = document.querySelectorAll("object");
    for (var i = 0; i < objs.length; i++) {
      var o = objs[i];
      var type = o.getAttribute("type") || "";
      if (type.indexOf("application/oipfApplicationManager") !== -1) {
        o.getOwnerApplication = appManager.getOwnerApplication;
      } else if (type.indexOf("video/broadcast") !== -1) {
        for (var kb in broadcast) o[kb] = broadcast[kb];
      } else if (type.indexOf("video/mp4") !== -1 || type.indexOf("video/mpeg") !== -1) {
        var av = makeAvControl();
        for (var ka in av) o[ka] = av[ka];
      } else if (type.indexOf("application/oipfConfiguration") !== -1) {
        for (var kc in configuration) o[kc] = configuration[kc];
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireObjectElements);
  } else {
    wireObjectElements();
  }

  // --- Wejscie zdarzen od hosta (Python -> JS) ------------------------------
  // Wolane przez webview.run_javascript(...) z Pythona.
  window.__hbbtvHost = {
    // synteyczny KeyboardEvent, tak jak realny pilot
    dispatchKey: function (vk) {
      var ev = new KeyboardEvent("keydown", { keyCode: vk, which: vk, bubbles: true });
      Object.defineProperty(ev, "keyCode", { get: function () { return vk; } });
      window.dispatchEvent(ev);
      document.dispatchEvent(ev);
    },
    setBroadcastPlayState: function (state) {
      broadcast.playState = state;
      if (typeof broadcast.onChannelChangeSucceeded === "function" && state === 2) {
        broadcast.onChannelChangeSucceeded();
      }
    },
    notifyChannel: function (channelJson) {
      window.__HBBTV_CHANNEL__ = channelJson;
      if (typeof window.onHbbtvChannel === "function") {
        window.onHbbtvChannel(channelJson);
      }
    },
  };

  send("ready");
})();
