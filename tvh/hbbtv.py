"""
Wykrywanie aplikacji HbbTV na kanale.

Tvheadend PARSUJE AIT (Application Information Table, table_id 0x74) SAM,
po stronie serwera (src/input/mpegts/dvb_psi_hbbtv.c) - wymaga to
zaznaczonej opcji "Parse HbbTV data" (per-muxer/DVB input). Wynik jest
trzymany w polu `service_t.s_hbbtv` i wystawiany przez JSON API pod
`service/streams` jako dodatkowe pole `"hbbtv"`:

    {
      "name": "...",
      "streams": [...],
      "fstreams": [...],
      "hbbtv": {
        "0": [                      # klucz = numer sekcji AIT
          {
            "title": [{"name": "TVP Portal", "lang": "pol"}, ...],
            "url": "http://hbbtest.v3.tvp.pl/hub/index_edu_2.php",
            "visibility": "all"     # none/apps/reserved/all (application_control_code bity 5-6)
          }
        ]
      }
    }

Dokladnie to widac w oknie "Service details" webui Tvheadend (sekcja
"HbbTv": Section/Language/Name/Link).

Ten modul NIE parsuje juz surowych sekcji binarnych - to Tvheadend robi
za nas. Tutaj tylko mapujemy JSON zwrocony przez HTSP (metoda "api",
patrz tvh/client.py: HtspClient.api()) na wygodny model Python.

UWAGA (fix): poprzednia wersja tego modulu dochodzila do pola "hbbtv" przez
dwa dodatkowe zapytania JSON API po HTSP (api("channel/grid", {"uuid":...})
+ api("service/streams", {"uuid":...})). To bylo zle z dwoch powodow:
  1. "channel/grid" NIE ma parametru "uuid" (tylko start/limit/filter/sort/
     dir/all - patrz docs.tvheadend.org/.../common-parameters). Nieznany
     klucz jest po prostu ignorowany (src/api/api_idnode.c:
     api_idnode_grid_conf() go nie czyta), wiec zapytanie zawsze zwracalo
     domyslna, nieprzefiltrowana pierwsza strone kanalow - entries[0] byl
     zawsze TYM SAMYM (przypadkowym) kanalem, niezaleznie od zadanego uuid.
  2. Wyciagniety stad services[0] (uuid serwisu INNEGO kanalu) szedl do
     "service/streams", ktore przy nieznanym/nieistniejacym uuid zwraca
     EINVAL (src/api/api_service.c: api_service_streams). htsp_method_api()
     (src/htsp_server.c) mapuje KAZDY blad rozny od EPERM/EACCES/ENOENT/
     ENOSYS na string "Bad request" - stad identyczny blad dla wszystkich
     kanalow (zawsze to samo, zle zapytanie).

Poprawka: Tvheadend i tak dolacza "hbbtv" do KAZDEGO serwisu w normalnej,
asynchronicznej wiadomosci channelAdd/channelUpdate (src/htsp_server.c:
htsp_build_channel() -> pole "services", patrz tez tvh/library.py:
TvhLibrary._on_channel_add). Nie trzeba wiec żadnego dodatkowego zapytania
JSON API - dane sa juz w tym, co i tak dostajemy przy synchronizacji, nie
wymagaja uprawnien ADMIN (ktorych wymaga "service/streams") i odswiezaja
sie same przy kolejnym channelUpdate. Patrz
parse_hbbtv_apps_from_channel_services() nizej.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class HbbtvApp:
    """Pojedyncza aplikacja HbbTV zasygnalizowana w AIT, juz sparsowana
    przez Tvheadend."""

    section: str = ""
    name: str = ""
    lang: str = ""
    url: Optional[str] = None
    visibility: str = ""  # none/apps/reserved/all

    @property
    def display_name(self) -> str:
        return self.name or "Aplikacja HbbTV"

    @property
    def uid(self) -> str:
        return f"{self.section}:{self.url or self.name}"


def parse_hbbtv_json(hbbtv_field: Any) -> List[HbbtvApp]:
    """Parsuje pole `hbbtv` zwrocone przez `service/streams` (patrz
    docstring modulu). Zwraca liste HbbtvApp - tolerancyjnie na braki
    pol, bo to zewnetrzny JSON serwera."""
    apps: List[HbbtvApp] = []
    if not isinstance(hbbtv_field, dict):
        return apps

    for section, entries in hbbtv_field.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url") or None
            visibility = str(entry.get("visibility") or "")

            titles = entry.get("title")
            if isinstance(titles, list) and titles:
                chosen = next(
                    (t for t in titles if isinstance(t, dict) and t.get("lang") == "pol"),
                    titles[0] if isinstance(titles[0], dict) else None,
                )
                name = (chosen or {}).get("name", "") if chosen else ""
                lang = (chosen or {}).get("lang", "") if chosen else ""
            else:
                name = ""
                lang = ""

            apps.append(
                HbbtvApp(
                    section=str(section),
                    name=name,
                    lang=lang,
                    url=url,
                    visibility=visibility,
                )
            )
    return apps


def parse_hbbtv_apps_from_channel_services(services: Any) -> List[HbbtvApp]:
    """Parsuje liste `services` z surowej wiadomosci HTSP channelAdd/
    channelUpdate (patrz tvh/library.py: TvhLibrary._on_channel_add).

    Serwer juz dolacza pole "hbbtv" do kazdego serwisu w tej liscie, gdy
    tylko ma sparsowane AIT (service_t.s_hbbtv, wymaga "Parse HbbTV data"
    per-muxer w Tvheadend) - dokladnie te same dane, ktore zwracaloby
    zapytanie JSON API service/streams, tylko bez dodatkowego zapytania i
    bez wymogu uprawnien ADMIN. Kanal moze miec kilka serwisow (np. SD+HD
    simulcast na tym samym multipleksie) - laczymy aplikacje ze wszystkich,
    odrzucajac duplikaty po HbbtvApp.uid."""
    apps: List[HbbtvApp] = []
    if not isinstance(services, list):
        return apps
    seen: set = set()
    for svc in services:
        if not isinstance(svc, dict):
            continue
        for app in parse_hbbtv_json(svc.get("hbbtv")):
            if app.uid in seen:
                continue
            seen.add(app.uid)
            apps.append(app)
    return apps


@dataclass
class ChannelHbbtvState:
    """Wynik ostatniego zapytania o HbbTV dla jednego kanalu - trzymany w
    cache biblioteki (patrz TvhLibrary.get_hbbtv_state)."""

    channel_id: int
    apps: List[HbbtvApp] = field(default_factory=list)
    service_uuid: Optional[str] = None
    fetched: bool = False
    error: Optional[str] = None

    @property
    def has_hbbtv(self) -> bool:
        return bool(self.apps)
