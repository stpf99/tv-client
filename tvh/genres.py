"""Mapowanie DVB content_type (ETSI EN 300 468, tabela 28,
"content_nibble_level_1" w gornym nibble'u bajtu, zgodnie z HTSP v6+ -
patrz komentarz w tvh/models.py EpgEvent.content_type) na czytelne etykiety
gatunkow (PL) i kolory uzywane do kolorowania siatki EPG.

Uzywane w: filtr gatunku w widoku EPG (ComboRow), kolorowanie blokow w
siatce czasowej, etykieta gatunku w widoku listy/gazety.
"""
from __future__ import annotations

from typing import Optional

# (etykieta PL, kolor tla bloku w siatce - hex, dopasowany do palety z
# oryginalnego makiety: fiolet dla filmow/seriali, oliwka dla dzieci,
# petrol dla rozrywki, pomaranczowy dla sportu/rekreacji, niebieski dla
# edukacji/nauki, szary dla pozostalych/nieznanych)
_GENRES: dict[int, tuple[str, str]] = {
    0x1: ("Film/Serial", "#7c5cbf"),
    0x2: ("Wiadomości/Publicystyka", "#3d7a99"),
    0x3: ("Rozrywka", "#2f9e8f"),
    0x4: ("Sport", "#c9752e"),
    0x5: ("Dla dzieci", "#b8a83a"),
    0x6: ("Muzyka/Balet/Taniec", "#a15fb0"),
    0x7: ("Sztuka/Kultura", "#5f8fbf"),
    0x8: ("Społeczne/Polityczne", "#8a6a4f"),
    0x9: ("Edukacja/Nauka", "#4a7fb5"),
    0xA: ("Hobby/Rekreacja", "#4f9e5f"),
    0xB: ("Cechy specjalne", "#6b6b6b"),
}
_UNKNOWN = ("Inne", "#555555")


def genre_major(content_type: Optional[int]) -> int:
    """Wyciaga gorny nibble (major category) z content_type - dziala
    zarowno dla wartosci juz przesunietej (0x1-0xB) jak i pelnego bajtu
    (0x10-0xB0), bo epgQuery przyjmuje wartosc do filtrowania w tej samej
    postaci co eventy - zeby uniknac niezgodnosci, normalizujemy zawsze
    do gornego nibble'u przy odczycie z eventu."""
    if content_type is None:
        return 0
    return (content_type >> 4) if content_type > 0xF else content_type


def genre_label(content_type: Optional[int]) -> str:
    major = genre_major(content_type)
    return _GENRES.get(major, _UNKNOWN)[0]


def genre_color(content_type: Optional[int]) -> str:
    major = genre_major(content_type)
    return _GENRES.get(major, _UNKNOWN)[1]


def all_genres() -> list[tuple[int, str]]:
    """Lista (major_nibble, etykieta) posortowana po etykiecie - do
    wypelnienia ComboRow filtra gatunku."""
    return sorted(((k, v[0]) for k, v in _GENRES.items()), key=lambda t: t[1])
