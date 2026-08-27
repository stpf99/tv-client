"""Mapowanie kodow gatunkow EPG (DVB EN 300 468 "content descriptor",
pole contentType z HTSP) na czytelne nazwy PL, do uzytku w
wyszukiwarce EPG (fraza typu "film", "sport", "dokument" itp.).

Kody sa dwupoziomowe: gorny nibble = kategoria glowna, dolny = podkategoria.
Tutaj interesuje nas glownie kategoria glowna (0x1_ .. 0xA_) - wystarcza
do sensownego filtrowania "typ audycji" bez pretensji do pelnej
precyzji wszystkich podkategorii operatorow.
"""
from __future__ import annotations

# (kod_kategorii_glownej) -> (nazwa PL, lista slow kluczowych do dopasowania z wyszukiwarki)
_MAIN_CATEGORIES: dict[int, tuple[str, list[str]]] = {
    0x1: ("Film / dramat", ["film", "filmy", "dramat", "kino"]),
    0x2: ("Wiadomości / aktualności", ["wiadomości", "wiadomosci", "news", "aktualności", "aktualnosci", "informacyjny"]),
    0x3: ("Rozrywka", ["rozrywka", "show", "talk-show", "talk show"]),
    0x4: ("Sport", ["sport", "sportowy", "mecz", "sporty"]),
    0x5: ("Dla dzieci", ["dzieci", "bajka", "bajki", "dziecięcy", "dziecinny", "junior"]),
    0x6: ("Muzyka", ["muzyka", "muzyczny", "koncert"]),
    0x7: ("Kultura / sztuka", ["kultura", "sztuka", "teatr", "kulturalny"]),
    0x8: ("Nauka społeczna / polityka", ["polityka", "społeczny", "spoleczny", "publicystyka"]),
    0x9: ("Edukacja / nauka", ["edukacja", "nauka", "naukowy", "edukacyjny", "przyroda", "dokumentalny", "dokument"]),
    0xA: ("Hobby", ["hobby", "poradnik", "wnętrza", "wnetrza", "kulinarny", "gotowanie", "ogród", "ogrod"]),
}


def genre_name(content_type: int | None) -> str:
    """Zwraca czytelna nazwe glownej kategorii gatunku dla danego kodu."""
    if not content_type:
        return ""
    main = (content_type >> 4) & 0xF
    entry = _MAIN_CATEGORIES.get(main)
    return entry[0] if entry else ""


def matches_genre_keyword(content_type: int | None, keyword: str) -> bool:
    """True, jesli podane slowo kluczowe (fraza z wyszukiwarki, lowercase)
    pasuje do gatunku danego eventu."""
    if not content_type:
        return False
    main = (content_type >> 4) & 0xF
    entry = _MAIN_CATEGORIES.get(main)
    if not entry:
        return False
    _name, keywords = entry
    keyword = keyword.lower().strip()
    return any(keyword == kw or keyword in kw or kw in keyword for kw in keywords)


def all_keywords() -> set[str]:
    """Wszystkie znane slowa kluczowe gatunkow - do podpowiedzi/walidacji."""
    out: set[str] = set()
    for _name, keywords in _MAIN_CATEGORIES.values():
        out.update(keywords)
    return out
