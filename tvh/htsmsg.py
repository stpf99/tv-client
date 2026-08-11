"""
Implementacja binarnego formatu HTSMSG używanego przez protokół HTSP
(HTS = HTS Tvheadend Streaming Protocol).

Format ramki na poziomie TCP:
    [4 bajty: dlugosc body, big-endian uint32] [body...]

Body to konkatenacja pól. Każde pole:
    [1 bajt: typ] [1 bajt: dlugosc nazwy] [4 bajty: dlugosc danych, BE]
    [nazwa (bytes)] [dane (bytes)]

Typy pól (HMF_*):
    MAP  = 1   -- zagnieżdżona mapa (rekurencyjnie taki sam format body)
    S64  = 2   -- liczba całkowita ze znakiem, big-endian, zmienna długość
    STR  = 3   -- tekst UTF-8
    BIN  = 4   -- surowe bajty
    LIST = 5   -- lista pól (każdy element to pole z pusta nazwa)
    DBL  = 6   -- double, 8 bajtów big-endian
    BOOL = 7   -- 1 bajt: 0/1
    UUID = 8   -- traktowane jak BIN
"""
from __future__ import annotations

import struct
from typing import Any, Dict, List, Union

HMF_MAP = 1
HMF_S64 = 2
HMF_STR = 3
HMF_BIN = 4
HMF_LIST = 5
HMF_DBL = 6
HMF_BOOL = 7
HMF_UUID = 8

HtsValue = Union[None, bool, int, float, str, bytes, dict, list]


def _encode_s64(value: int) -> bytes:
    """Koduje liczbę całkowitą ze znakiem do minimalnej liczby bajtów big-endian."""
    if value == 0:
        return b"\x00"
    length = (value.bit_length() // 8) + 1
    return value.to_bytes(length, byteorder="big", signed=True)


def _decode_s64(data: bytes) -> int:
    if not data:
        return 0
    return int.from_bytes(data, byteorder="big", signed=True)


def _serialize_field(name: bytes, value: HtsValue) -> bytes:
    if isinstance(value, bool):
        htype = HMF_BOOL
        payload = b"\x01" if value else b"\x00"
    elif isinstance(value, int):
        htype = HMF_S64
        payload = _encode_s64(value)
    elif isinstance(value, float):
        htype = HMF_DBL
        payload = struct.pack(">d", value)
    elif isinstance(value, str):
        htype = HMF_STR
        payload = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        htype = HMF_BIN
        payload = bytes(value)
    elif isinstance(value, dict):
        htype = HMF_MAP
        payload = serialize_body(value)
    elif isinstance(value, (list, tuple)):
        htype = HMF_LIST
        payload = b"".join(_serialize_field(b"", item) for item in value)
    elif value is None:
        # Puste pole traktujemy jako pusty string - HTSP nie ma "null"
        htype = HMF_STR
        payload = b""
    else:
        raise TypeError(f"Nieobslugiwany typ pola HTSMSG: {type(value)!r}")

    header = struct.pack(">BB I", htype, len(name), len(payload))
    return header + name + payload


def serialize_body(msg: Dict[str, HtsValue]) -> bytes:
    return b"".join(_serialize_field(k.encode("utf-8"), v) for k, v in msg.items())


def serialize_message(msg: Dict[str, HtsValue]) -> bytes:
    """Serializuje pelna wiadomosc HTSP wraz z 4-bajtowym naglowkiem dlugosci."""
    body = serialize_body(msg)
    return struct.pack(">I", len(body)) + body


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        if len(chunk) != n:
            raise ValueError("Nieoczekiwany koniec danych HTSMSG")
        self.pos += n
        return chunk

    def eof(self) -> bool:
        return self.pos >= len(self.data)


def _deserialize_field(reader: "_Reader"):
    htype, name_len = struct.unpack(">BB", reader.read(2))
    (data_len,) = struct.unpack(">I", reader.read(4))
    name = reader.read(name_len).decode("utf-8", errors="replace")
    raw = reader.read(data_len)

    if htype == HMF_MAP:
        value = deserialize_body(raw)
    elif htype == HMF_S64:
        value = _decode_s64(raw)
    elif htype == HMF_STR:
        value = raw.decode("utf-8", errors="replace")
    elif htype == HMF_BIN:
        value = raw
    elif htype == HMF_UUID:
        value = raw
    elif htype == HMF_LIST:
        value = _deserialize_list(raw)
    elif htype == HMF_DBL:
        value = struct.unpack(">d", raw)[0]
    elif htype == HMF_BOOL:
        value = bool(raw[0]) if raw else False
    else:
        value = raw
    return name, value


def _deserialize_list(raw: bytes) -> List[Any]:
    reader = _Reader(raw)
    items = []
    while not reader.eof():
        _, value = _deserialize_field(reader)
        items.append(value)
    return items


def deserialize_body(raw: bytes) -> Dict[str, Any]:
    reader = _Reader(raw)
    result: Dict[str, Any] = {}
    while not reader.eof():
        name, value = _deserialize_field(reader)
        result[name] = value
    return result


def deserialize_message(body: bytes) -> Dict[str, Any]:
    """Deserializuje body wiadomosci (bez 4-bajtowego naglowka dlugosci)."""
    return deserialize_body(body)
