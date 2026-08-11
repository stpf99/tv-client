# TVHeadend GNOME Client

Klient Tvheadend dla GTK4 + libadwaita + GStreamer, mówiący natywnym
binarnym protokołem **HTSP** (nie REST) — z osobnymi trybami *TV na żywo*,
*Radio na żywo*, *Przewodnik EPG*, *Nagrania* oraz *Ostatnio odtwarzane*
(jak sekcje w Kodi/skórce Estuary), OSD z auto-hide, trybem pełnoekranowym,
wspólną belką statusu (mini-player) i integracją z appletem multimediów
GNOME przez MPRIS2.

## Zależności systemowe (Fedora/Arch/Debian — nazwy pakietów mogą się różnić)

- GTK4 (>= 4.12) + libadwaita (>= 1.5)
- PyGObject (`python3-gobject` / `python-gobject`)
- GStreamer 1.22+ wraz z:
  - `gst-plugins-base`, `gst-plugins-good`, `gst-plugins-bad`, `gst-plugins-ugly`
  - `gst-plugin-gtk4` (dostarcza `gtk4paintablesink` — kluczowe dla renderu
    wideo bezpośrednio w drzewie widgetów GTK4/GSK, zero-copy na Waylandzie)
  - `gst-libav` (dekodery H.264/AAC/MPEG2 przez FFmpeg, jeśli nie masz
    sprzętowych/VAAPI)

Na ChimeraOS/Arch-based (AerynOS/Solus podobnie, sprawdź nazwy w repo):

```bash
sudo pacman -S gtk4 libadwaita python-gobject \
    gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad \
    gst-plugins-ugly gst-libav gst-plugin-gtk4
```

Jeśli `gst-plugin-gtk4` nie jest spakietowany w Twojej dystrybucji, trzeba
zbudować go z `https://gitlab.freedesktop.org/gstreamer/gst-plugins-rs`
(pakiet `gtk4` w `video/gtk4`, Rust + `cargo-c`).

## Uruchomienie

```bash
pip install -r requirements.txt --break-system-packages
python3 main.py
```

Przy pierwszym uruchomieniu pojawi się okno połączenia — podaj adres
serwera Tvheadend, port HTSP (domyślnie 9982), login/hasło. Konfiguracja
zapisywana jest w `~/.config/tvh-gnome-client/config.json`.

## Architektura

```
main.py
tvh/
  htsmsg.py         # binarna (de)serializacja HTSMSG
  client.py         # asyncio HtspClient: hello/auth/subscribe/DVR/EPG
  async_bridge.py    # most asyncio (HTSP) <-> GLib (GTK), watek w tle
  library.py          # TvhLibrary(GObject) - stan: kanaly/tagi/EPG/nagrania
  models.py             # dataclasses: Channel, ChannelTag, EpgEvent, Recording
  config.py              # zapis/odczyt konfiguracji polaczenia
player/
  gst_player.py       # appsrc -> decodebin -> gtk4paintablesink/autoaudiosink
  stream_controller.py # spina subskrypcje HTSP (muxpkt) z GstPlayer
ui/
  app.py               # Adw.Application, ladowanie CSS
  window.py             # Adw.ApplicationWindow, nawigacja, mini-player
  connection_dialog.py   # dialog polaczenia z serwerem
  channel_list.py          # lista kanalow TV/Radio z aktualnym programem
  live_view.py               # obszar wideo + OSD (auto-hide) + fullscreen
  epg_view.py                  # przewodnik: "teraz i za chwile" per kanal
  recordings_view.py            # lista nagran DVR (stop/anuluj/usun)
  recent_view.py                  # kafelki "ostatnio odtwarzane"
  recent.py                        # trwaly magazyn historii (JSON)
  mpris.py                          # serwis MPRIS2 dla appletu GNOME
  style.css                          # akcenty na bazie zmiennych libadwaita
```

### Dlaczego appsrc, a nie zwykłe `uri=http://.../stream/channel/...`?

Zadanie było jednoznaczne: "korzysta z HTSP z pełnią dostępnych funkcji".
HTSP dostarcza surowe pakiety (`muxpkt`) przez to samo połączenie TCP na
którym leci sterowanie — nie ma tu adresu URL do podania `playbin`. Dlatego
pipeline to `appsrc` karmiony bajtami z `HtspClient.on_muxpkt`, a dalej
`decodebin` (autodetekcja kontenera/kodeków) rozdzielający się na gałąź
wideo (`gtk4paintablesink`) i audio (`autoaudiosink`).

Zaletą tego podejścia względem podejścia REST+HTTP (jak we `tvhplayer`)
jest dostęp do pełnego zestawu funkcji HTSP: `subscriptionStatus`,
`signalStatus` (siła/jakość sygnału tunera), `getTicket`, granularna kontrola
DVR (`addDvrEntry` z `configUUID`, `cancelDvrEntry` vs `stopDvrEntry` vs
`deleteDvrEntry`) i async EPG push (`eventAdd`/`eventUpdate` bez pollingu).

## Stabilność odbioru / strojenie buforowania

Odtwarzacz (`player/gst_player.py`) domyślnie priorytetyzuje stabilność nad
latencją: ~1.5 s bufora w kolejkach GStreamera, ~1.2 s prerollu przed startem
obrazu, renderowanie zsynchronizowane z zegarem pipeline'u (płynny pacing
klatek/audio) oraz watchdog, który wykrywa ciszę w danych z HTSP (zator
sieciowy) i automatycznie wraca do buforowania zamiast pozwolić obrazowi
szarpać. Da się to przestroić zmiennymi środowiskowymi, jeśli wolisz niższą
latencję kosztem odporności na jitter (albo odwrotnie, przy bardzo
niestabilnej sieci):

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `TVH_BUFFER_MS` | `1500` | Rozmiar kolejek live (appsrc + queue) w ms |
| `TVH_PREROLL_MS` | `1200` | Ile danych (wg PTS) zebrać przed pierwszym PLAYING |
| `TVH_REBUFFER_MS` | `600` | Preroll po wykryciu zerwania strumienia (szybszy powrót) |
| `TVH_STALL_TIMEOUT_MS` | `700` | Po ilu ms ciszy w danych watchdog uzna strumień za "zerwany" |
| `TVH_DISABLE_HW_HEVC` | `0` | `1` wyłącza sprzętowy dekoder HEVC (obejście buggy VA-API) |
| `TVH_HW_PROFILE_SWITCH_DELAY` | `0.4` | Opóźnienie (s) przy zmianie profilu sprzętowego dekodera |

Jeśli mimo większego bufora nadal widać zacięcia, warto sprawdzić logi pod
kątem `Brak danych ze strumienia przez` (watchdog re-bufferingu) — to znak,
że problem leży po stronie sieci/Tvheadend, a nie samego odtwarzacza.

## Znane ograniczenia / do dopracowania

- Detekcja "kanał radiowy vs TV" opiera się o obecność tagu z nazwą
  zawierającą "radio" — jeśli Twój serwer nie ma takiego tagu, warto dodać
  heurystykę po obecności/braku strumienia wideo w `subscriptionStart`
  (pole `streams[].type`).
- Widok EPG to lista "teraz / za chwilę" per kanał (jak pasek info w Kodi),
  nie siatka czasowa. Siatka czasowa (kanały × oś czasu) to sensowny
  następny krok — dobry kandydat na `Gtk.ColumnView` z customowym rysowaniem
  bloków programów przez `Gtk.DrawingArea`/GSK.
- Tray icon (StatusNotifierItem) celowo pominięty w tej wersji — na czystym
  GNOME i tak wymaga rozszerzenia powłoki; zamiast tego zaimplementowano
  MPRIS2, które działa natywnie w quick settings/OSD GNOME bez dodatków.
- `gst4paintablesink` bywa nazwany `gtk4paintablesink` w zależności od
  wersji `gst-plugins-rs` — jeśli `Gst.ElementFactory.make` zwraca `None`,
  sprawdź `gst-inspect-1.0 | grep -i gtk4`.
