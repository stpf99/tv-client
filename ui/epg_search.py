"""Parser zapytan wyszukiwania w widoku EPG.

Obsluguje pojedyncze pole tekstowe, z ktorego wyciagane sa:
  - zakresy/punkty czasowe: "20:00-22:00", "po 20", "przed 18:30",
    "dziś", "jutro", "wczoraj", "12.08", "12.08-14.08"
  - gatunek/typ audycji: rozpoznawany po slowach kluczowych z
    tvh.epg_genres (np. "film", "sport", "dokument")
  - reszta frazy: dopasowywana pelnotekstowo (bez rozroznienia
    wielkosci liter, bez polskich znakow diakrytycznych) do tytulu,
    podtytulu, opisu programu ORAZ nazwy kanalu.

Wynikiem jest EpgQuery, ktore udostepnia matches(event, channel_name, now)
tak, aby EpgChannelRow/EpgView mogly filtrowac liste programow.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from tvh.epg_genres import matches_genre_keyword, all_keywords
from tvh.models import EpgEvent

_DAY_NAMES = {
    "poniedzialek": 0, "wtorek": 1, "sroda": 2, "czwartek": 3,
    "piatek": 4, "sobota": 5, "niedziela": 6,
}

_TIME_RANGE_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?\b")
_TIME_AFTER_RE = re.compile(r"\bpo\s+(\d{1,2})(?::(\d{2}))?\b")
_TIME_BEFORE_RE = re.compile(r"\bprzed\s+(\d{1,2})(?::(\d{2}))?\b")
_TIME_SINGLE_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_DATE_DMY_RANGE_RE = re.compile(
    r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s*-\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b"
)
_DATE_DMY_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\b")


def _strip_diacritics(s: str) -> str:
    norm = unicodedata.normalize("NFKD", s)
    return "".join(c for c in norm if not unicodedata.combining(c))


def _norm(s: str) -> str:
    return _strip_diacritics(s).lower().strip()


@dataclass
class EpgQuery:
    raw: str = ""
    text_terms: list[str] = field(default_factory=list)
    genre_keywords: list[str] = field(default_factory=list)
    date_start: Optional[date] = None
    date_end: Optional[date] = None
    time_from_minutes: Optional[int] = None  # minuty od poczatku doby, wg czasu startu programu
    time_to_minutes: Optional[int] = None
    weekday: Optional[int] = None  # 0=poniedzialek .. 6=niedziela, gdy podano nazwe dnia tygodnia

    @property
    def is_empty(self) -> bool:
        return not (
            self.text_terms
            or self.genre_keywords
            or self.date_start
            or self.date_end
            or self.time_from_minutes is not None
            or self.time_to_minutes is not None
            or self.weekday is not None
        )

    def matches(self, event: EpgEvent, channel_name: str = "", now: Optional[int] = None) -> bool:
        if self.is_empty:
            return True
        if not event.start:
            return False

        ev_dt = datetime.fromtimestamp(event.start)
        ev_date = ev_dt.date()

        if self.date_start and ev_date < self.date_start:
            return False
        if self.date_end and ev_date > self.date_end:
            return False

        if self.weekday is not None and ev_dt.weekday() != self.weekday:
            return False

        minute_of_day = ev_dt.hour * 60 + ev_dt.minute
        if self.time_from_minutes is not None and minute_of_day < self.time_from_minutes:
            return False
        if self.time_to_minutes is not None and minute_of_day > self.time_to_minutes:
            return False

        for kw in self.genre_keywords:
            if not matches_genre_keyword(event.content_type, kw):
                return False

        if self.text_terms:
            haystack = _norm(
                " ".join(
                    filter(
                        None,
                        [event.title, event.subtitle, event.description, channel_name],
                    )
                )
            )
            for term in self.text_terms:
                if term not in haystack:
                    return False

        return True


def parse_query(raw: str, now: Optional[int] = None) -> EpgQuery:
    """Parsuje pojedyncze pole wyszukiwania na EpgQuery.

    Rozpoznane fragmenty sa usuwane z tekstu, reszta trafia do
    text_terms jako dopasowanie pelnotekstowe.
    """
    now_ts = now or datetime.now().timestamp()
    today = datetime.fromtimestamp(now_ts).date()

    q = EpgQuery(raw=raw)
    working = " " + _norm(raw) + " "

    # --- zakres dat dd.mm[.rrrr]-dd.mm[.rrrr] ---------------------------
    m = _DATE_DMY_RANGE_RE.search(working)
    if m:
        d1, mo1, y1, d2, mo2, y2 = m.groups()
        try:
            year1 = int(y1) if y1 else today.year
            year2 = int(y2) if y2 else today.year
            q.date_start = date(year1, int(mo1), int(d1))
            q.date_end = date(year2, int(mo2), int(d2))
        except ValueError:
            pass
        working = working[: m.start()] + " " + working[m.end():]
    else:
        # --- pojedyncza data dd.mm[.rrrr] -------------------------------
        m = _DATE_DMY_RE.search(working)
        if m:
            d1, mo1, y1 = m.groups()
            try:
                year1 = int(y1) if y1 else today.year
                d = date(year1, int(mo1), int(d1))
                q.date_start = d
                q.date_end = d
            except ValueError:
                pass
            working = working[: m.start()] + " " + working[m.end():]

    # --- slowa "dzis"/"jutro"/"wczoraj" ---------------------------------
    if re.search(r"\bdzis\b", working):
        q.date_start = q.date_start or today
        q.date_end = q.date_end or today
        working = re.sub(r"\bdzis\b", " ", working)
    if re.search(r"\bjutro\b", working):
        d = today + timedelta(days=1)
        q.date_start = q.date_start or d
        q.date_end = q.date_end or d
        working = re.sub(r"\bjutro\b", " ", working)
    if re.search(r"\bwczoraj\b", working):
        d = today - timedelta(days=1)
        q.date_start = q.date_start or d
        q.date_end = q.date_end or d
        working = re.sub(r"\bwczoraj\b", " ", working)

    # --- nazwa dnia tygodnia --------------------------------------------
    for name, idx in _DAY_NAMES.items():
        if re.search(rf"\b{name}\w*\b", working):
            q.weekday = idx
            working = re.sub(rf"\b{name}\w*\b", " ", working)
            break

    # --- zakres godzin hh[:mm]-hh[:mm] -----------------------------------
    m = _TIME_RANGE_RE.search(working)
    if m:
        h1, m1, h2, m2 = m.groups()
        q.time_from_minutes = int(h1) * 60 + int(m1 or 0)
        q.time_to_minutes = int(h2) * 60 + int(m2 or 0)
        working = working[: m.start()] + " " + working[m.end():]
    else:
        m = _TIME_AFTER_RE.search(working)
        if m:
            h, mi = m.groups()
            q.time_from_minutes = int(h) * 60 + int(mi or 0)
            working = working[: m.start()] + " " + working[m.end():]
        m = _TIME_BEFORE_RE.search(working)
        if m:
            h, mi = m.groups()
            q.time_to_minutes = int(h) * 60 + int(mi or 0)
            working = working[: m.start()] + " " + working[m.end():]
        if q.time_from_minutes is None and q.time_to_minutes is None:
            # pojedyncza godzina "20:00" -> traktuj jako "od tej godziny"
            m = _TIME_SINGLE_RE.search(working)
            if m:
                h, mi = m.groups()
                q.time_from_minutes = int(h) * 60 + int(mi)
                working = working[: m.start()] + " " + working[m.end():]

    # --- gatunek / typ audycji -------------------------------------------
    remaining_words = working.split()
    genre_kws = all_keywords()
    kept_words = []
    matched_any_genre = False
    for word in remaining_words:
        stripped = word.strip(",.;:!?")
        if stripped in genre_kws:
            q.genre_keywords.append(stripped)
            matched_any_genre = True
        else:
            kept_words.append(word)
    if not matched_any_genre:
        # sprobuj dopasowac frazy wielowyrazowe typu "talk show", "talk-show"
        joined = " ".join(kept_words)
        for kw in sorted(genre_kws, key=len, reverse=True):
            if " " in kw and kw in joined:
                q.genre_keywords.append(kw)
                joined = joined.replace(kw, " ")
        kept_words = joined.split()

    # --- reszta = dopasowanie pelnotekstowe -------------------------------
    q.text_terms = [w for w in kept_words if w]

    return q
