# Wayland zero-copy: gtk4paintablesink + glimagesink

## Problem, który to naprawia

Poprzednia ścieżka playbin owijała `gtk4paintablesink` w:

```
queue ! videoconvert ! video/x-raw,format=NV12 ! gtk4paintablesink
```

To **zawsze** zrywało DMABuf: dekoder VA-API zrzucał powierzchnię do RAM
(`system-memory`), a GTK kopiował ją z powrotem na GPU. Zero-copy nie działało
nawet gdy w preferencjach było „VA surface / DMABuf” albo „sprzętowy”.

## Zalecane metody (GStreamer ≥ 1.22)

### 1. gtk4paintablesink + glsinkbin  (osadzone w GTK4)

```
playbin video-sink="glsinkbin sink=gtk4paintablesink"
```

`glsinkbin` to ten sam bin, którego używa `glimagesink`. Wstawia `glupload`,
który na Wayland/EGL importuje DMA-BUF:

```
vaapih264dec / vah264dec
    → video/x-raw(memory:VAMemory)  lub  memory:DMABuf
glupload
    → eglCreateImageKHR(EGL_LINUX_DMA_BUF_EXT)   # GPU→GPU, bez CPU
    → video/x-raw(memory:GLMemory)
gtk4paintablesink
    → GdkPaintable → Gtk.Picture
Gtk.GraphicsOffload (GTK ≥ 4.14)
    → compositor skanuje bufor (Mutter / KWin)
```

### 2. glimagesink  (GstGLSinkBin)

```
playbin video-sink="glimagesink"
```

`glimagesink` **jest** `GstGLSinkBin` i akceptuje `memory:DMABuf` oraz
`memory:GLMemory`. Ta sama ścieżka GL. W kliencie GTK4 używamy jej jako
`glsinkbin sink=gtk4paintablesink`, żeby obraz został w oknie aplikacji.
Gdy `gtk4paintablesink` nie jest zainstalowany, pada na prawdziwy
`glimagesink` (osobne okno GL).

### 3. gtk4paintablesink bezpośrednio (VA surface)

```
playbin video-sink="gtk4paintablesink"
```

GDK 4 importuje DMABuf jako `GdkDmabufTexture`. Działa, gdy dekoder eksportuje
`memory:DMABuf`. `VAMemory` z `vaapi*dec` bywa nieakceptowane – wtedy playbin
mógłby wstawić `videoconvert`. Dlatego **auto** i **gtk4** owijają sink w
`glsinkbin`.

## Kiedy która opcja

W Preferencjach odtwarzacza:

| Wybór | Co się dzieje |
|---|---|
| Dekoder = **sprzętowy VA-API** | Rank `PRIMARY+256` dla wszystkich `va*dec` / `vaapi*dec`, w tym HEVC, JPEG, MPEG-4:2, VP8, VC-1, WMV3. Soft-decody schodzą do `MARGINAL`. |
| Wyjście = **gtk4paintablesink + glsinkbin** | metoda 1 |
| Wyjście = **glimagesink / glsinkbin** | metoda 2 |
| Wyjście = **auto** | na Waylandzie metoda 1 |
| Wyjście = **programowe** | jedyny przypadek z `videoconvert` (kopia CPU) |

## Kodeki HW

`vaapi<CODEC>dec` (plugin `vaapi`) i `va<CODEC>dec` (plugin `va`):

JPEG, MPEG-2, MPEG-4 Part 2, H.264 AVC, H.264 MVC, VP8, VP9, VC-1, WMV3, HEVC, AV1
– w zależności od `vainfo` i GPU.

## Weryfikacja

W logu (`G_MESSAGES_DEBUG=` niepotrzebne, wystarczy INFO):

```
VA-API (hw, wayland=True, hevc_hw=True): vah264dec, vaapih264dec, …
glsinkbin wrap: DMABuf/VAMemory → glupload → gtk4paintablesink (wayland=True, zero-copy)
Pipeline path: hw_dec=vaapih264dec … videoconvert=False zerocopy=True wayland=True
```

`zerocopy=False` + `videoconvert=True` oznacza, że klatki idą przez RAM –
sprawdź, czy na pewno wybrałeś dekoder sprzętowy i wyjście inne niż „programowe”.

## Zmienne środowiskowe (ustawiane automatycznie przed Gst.init)

| Zmienna | Wartość | Po co |
|---|---|---|
| `GST_GL_WINDOW` | `wayland` | GstGL Display Wayland |
| `GST_GL_PLATFORM` | `egl` | import DMABuf przez EGL |
| `GST_VAAPI_ALL_DRIVERS` | `1` | legacy vaapi na AMD/NVIDIA |
| `TVH_ENABLE_HW_HEVC` | `0`/`1` | HW HEVC przy dekoderze=auto |

## Zależności

```
gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad
gst-libav gst-plugin-gtk4
# VA:
gst-plugin-va          # va*dec (nowoczesne)
gstreamer-vaapi        # vaapi*dec (legacy) – opcjonalnie
libva / vainfo
```
