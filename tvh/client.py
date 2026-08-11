"""
Klient protokolu HTSP (Home Tvheadend Streaming Protocol) dzialajacy w
petli asyncio. Uruchamiany jest we wlasnym watku (patrz async_bridge.py),
zeby nie blokowac petli GLib/GTK.

Obsluguje:
  - handshake `hello` + `authenticate` (SHA1(password + challenge))
  - `enableAsyncMetadata` -> serwer sam wysyla channelAdd/tagAdd/eventAdd/...
  - subskrypcje strumienia (`subscribe`/`unsubscribe`) z odbiorem `muxpkt`
  - sterowanie DVR: addDvrEntry / cancelDvrEntry / stopDvrEntry / getDvrConfigs
  - `getTicket` (do ew. odtwarzania przez HTTP jako fallback)
  - `getEvents` / `epgQuery` do przegladania EPG poza async-metadata
"""
from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import struct
from typing import Any, Awaitable, Callable, Dict, Optional

from . import htsmsg

logger = logging.getLogger("tvh.htsp")

HTSP_CLIENT_NAME = "TVH-GNOME-Client"
HTSP_CLIENT_VERSION = "1.0"
HTSP_VERSION = 27  # deklarowana wersja protokolu ktora rozumiemy


class HtspError(Exception):
    pass


class HtspAuthError(HtspError):
    pass


class HtspClient:
    def __init__(self) -> None:
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._seq = itertools.count(1)
        self._pending: Dict[int, asyncio.Future] = {}
        self._read_task: Optional[asyncio.Task] = None
        self._connected = False
        self.server_name = ""
        self.server_version = ""
        self.htsp_version = 0
        self.challenge: bytes = b""

        # Callbacki na wiadomosci nie majace 'seq' (push z serwera)
        self.on_channel_add: Optional[Callable[[dict], None]] = None
        self.on_channel_update: Optional[Callable[[dict], None]] = None
        self.on_channel_delete: Optional[Callable[[dict], None]] = None
        self.on_tag_add: Optional[Callable[[dict], None]] = None
        self.on_tag_update: Optional[Callable[[dict], None]] = None
        self.on_event_add: Optional[Callable[[dict], None]] = None
        self.on_event_update: Optional[Callable[[dict], None]] = None
        self.on_dvr_entry_add: Optional[Callable[[dict], None]] = None
        self.on_dvr_entry_update: Optional[Callable[[dict], None]] = None
        self.on_dvr_entry_delete: Optional[Callable[[dict], None]] = None
        self.on_initial_syncdone: Optional[Callable[[], None]] = None
        self.on_muxpkt: Optional[Callable[[int, dict], None]] = None
        self.on_subscription_start: Optional[Callable[[dict], None]] = None
        self.on_subscription_stop: Optional[Callable[[dict], None]] = None
        self.on_subscription_status: Optional[Callable[[dict], None]] = None
        self.on_signal_status: Optional[Callable[[dict], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------ #
    # Polaczenie / handshake
    # ------------------------------------------------------------------ #
    async def connect(self, host: str, port: int = 9982, timeout: float = 8.0) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        self._connected = True
        self._read_task = asyncio.ensure_future(self._read_loop())
        logger.info("Polaczono z %s:%s", host, port)

    async def close(self) -> None:
        self._connected = False
        if self._read_task:
            self._read_task.cancel()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def hello(self) -> dict:
        resp = await self.send_and_wait(
            "hello",
            {
                "htspversion": HTSP_VERSION,
                "clientname": HTSP_CLIENT_NAME,
                "clientversion": HTSP_CLIENT_VERSION,
            },
        )
        self.server_name = resp.get("servername", "")
        self.server_version = resp.get("serverversion", "")
        self.htsp_version = resp.get("htspversion", 0)
        self.challenge = resp.get("challenge", b"") or b""
        return resp

    async def authenticate(self, username: str, password: str = "") -> None:
        digest = hashlib.sha1(password.encode("utf-8") + self.challenge).digest()
        resp = await self.send_and_wait(
            "authenticate",
            {"username": username, "digest": digest},
        )
        if not resp.get("noaccess", 0) == 0:
            raise HtspAuthError("Odmowa dostepu - sprawdz login/haslo/uprawnienia")

    async def enable_async_metadata(self, epg: bool = True, epg_max_time: int = 0) -> None:
        params: Dict[str, Any] = {"epg": 1 if epg else 0}
        if epg_max_time:
            params["epgMaxTime"] = epg_max_time
        await self.send_and_wait("enableAsyncMetadata", params)

    # ------------------------------------------------------------------ #
    # Niskopoziomowe wysylanie / odbior
    # ------------------------------------------------------------------ #
    async def send_and_wait(self, method: str, params: Optional[dict] = None, timeout: float = 15.0) -> dict:
        if not self._writer:
            raise HtspError("Klient nie jest polaczony")
        seq = next(self._seq)
        msg = dict(params or {})
        msg["method"] = method
        msg["seq"] = seq

        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[seq] = fut

        raw = htsmsg.serialize_message(msg)
        self._writer.write(raw)
        await self._writer.drain()

        try:
            resp = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(seq, None)

        error = resp.get("error")
        if error:
            raise HtspError(str(error))
        return resp

    async def send_no_wait(self, method: str, params: Optional[dict] = None) -> None:
        if not self._writer:
            raise HtspError("Klient nie jest polaczony")
        msg = dict(params or {})
        msg["method"] = method
        raw = htsmsg.serialize_message(msg)
        self._writer.write(raw)
        await self._writer.drain()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                header = await self._reader.readexactly(4)
                (length,) = struct.unpack(">I", header)
                body = await self._reader.readexactly(length)
                msg = htsmsg.deserialize_message(body)
                self._dispatch(msg)
        except (asyncio.CancelledError, asyncio.IncompleteReadError):
            pass
        except Exception:
            logger.exception("Blad w petli odczytu HTSP")
        finally:
            self._connected = False
            if self.on_disconnect:
                self.on_disconnect()

    def _dispatch(self, msg: dict) -> None:
        seq = msg.get("seq")
        if seq is not None and seq in self._pending:
            fut = self._pending[seq]
            if not fut.done():
                fut.set_result(msg)
            return

        method = msg.get("method")
        if method == "channelAdd" and self.on_channel_add:
            self.on_channel_add(msg)
        elif method == "channelUpdate" and self.on_channel_update:
            self.on_channel_update(msg)
        elif method == "channelDelete" and self.on_channel_delete:
            self.on_channel_delete(msg)
        elif method == "tagAdd" and self.on_tag_add:
            self.on_tag_add(msg)
        elif method == "tagUpdate" and self.on_tag_update:
            self.on_tag_update(msg)
        elif method == "eventAdd" and self.on_event_add:
            self.on_event_add(msg)
        elif method == "eventUpdate" and self.on_event_update:
            self.on_event_update(msg)
        elif method == "dvrEntryAdd" and self.on_dvr_entry_add:
            self.on_dvr_entry_add(msg)
        elif method == "dvrEntryUpdate" and self.on_dvr_entry_update:
            self.on_dvr_entry_update(msg)
        elif method == "dvrEntryDelete" and self.on_dvr_entry_delete:
            self.on_dvr_entry_delete(msg)
        elif method == "initialSyncCompleted" and self.on_initial_syncdone:
            self.on_initial_syncdone()
        elif method == "muxpkt" and self.on_muxpkt:
            self.on_muxpkt(msg.get("subscriptionId", 0), msg)
        elif method == "subscriptionStart" and self.on_subscription_start:
            self.on_subscription_start(msg)
        elif method == "subscriptionStop" and self.on_subscription_stop:
            self.on_subscription_stop(msg)
        elif method == "subscriptionStatus" and self.on_subscription_status:
            self.on_subscription_status(msg)
        elif method == "signalStatus" and self.on_signal_status:
            self.on_signal_status(msg)
        else:
            logger.debug("Nieobsluzona wiadomosc push: %s", method)

    # ------------------------------------------------------------------ #
    # Strumieniowanie
    # ------------------------------------------------------------------ #
    async def subscribe(self, channel_id: int, subscription_id: int,
                         weight: int = 150, normts: bool = True) -> None:
        await self.send_no_wait(
            "subscribe",
            {
                "channelId": channel_id,
                "subscriptionId": subscription_id,
                "weight": weight,
                "normts": 1 if normts else 0,
            },
        )

    async def unsubscribe(self, subscription_id: int) -> None:
        await self.send_no_wait("unsubscribe", {"subscriptionId": subscription_id})

    async def subscription_speed(self, subscription_id: int, speed: int) -> None:
        await self.send_no_wait(
            "subscriptionSpeed", {"subscriptionId": subscription_id, "speed": speed}
        )

    # ------------------------------------------------------------------ #
    # EPG
    # ------------------------------------------------------------------ #
    async def get_events(self, channel_id: Optional[int] = None, num_following: int = 20,
                          event_id: Optional[int] = None) -> dict:
        params: Dict[str, Any] = {"numFollowing": num_following}
        if channel_id is not None:
            params["channelId"] = channel_id
        if event_id is not None:
            params["eventId"] = event_id
        return await self.send_and_wait("getEvents", params)

    async def epg_query(self, query: str, channel_id: Optional[int] = None, limit: int = 50) -> dict:
        params: Dict[str, Any] = {"query": query, "limit": limit}
        if channel_id is not None:
            params["channelId"] = channel_id
        return await self.send_and_wait("epgQuery", params)

    # ------------------------------------------------------------------ #
    # DVR
    # ------------------------------------------------------------------ #
    async def get_dvr_configs(self) -> dict:
        return await self.send_and_wait("getDvrConfigs", {})

    async def add_dvr_entry(self, channel_id: int, event_id: Optional[int] = None,
                             title: Optional[str] = None, start: Optional[int] = None,
                             stop: Optional[int] = None, config_uuid: Optional[str] = None) -> dict:
        params: Dict[str, Any] = {"channelId": channel_id}
        if event_id is not None:
            params["eventId"] = event_id
        if title is not None:
            params["title"] = {"eng": title}
        if start is not None:
            params["start"] = start
        if stop is not None:
            params["stop"] = stop
        if config_uuid:
            params["configUUID"] = config_uuid
        return await self.send_and_wait("addDvrEntry", params)

    async def cancel_dvr_entry(self, entry_id: int) -> dict:
        return await self.send_and_wait("cancelDvrEntry", {"id": entry_id})

    async def stop_dvr_entry(self, entry_id: int) -> dict:
        return await self.send_and_wait("stopDvrEntry", {"id": entry_id})

    async def delete_dvr_entry(self, entry_id: int) -> dict:
        return await self.send_and_wait("deleteDvrEntry", {"id": entry_id})

    async def add_autorec_entry(
        self,
        title: str,
        channel_id: Optional[int] = None,
        event_id: Optional[int] = None,
        fulltext: bool = False,
        config_uuid: Optional[str] = None,
        enabled: bool = True,
    ) -> dict:
        """Zaplanuj serię (autorec) po tytule / eventId."""
        params: Dict[str, Any] = {
            "title": title,
            "enabled": 1 if enabled else 0,
            "fulltext": 1 if fulltext else 0,
        }
        if channel_id is not None:
            params["channelId"] = channel_id
        if event_id is not None:
            params["eventId"] = event_id
        if config_uuid:
            params["configName"] = config_uuid
        return await self.send_and_wait("addAutorecEntry", params)

    async def get_ticket(self, channel_id: Optional[int] = None,
                          dvr_entry_id: Optional[int] = None) -> dict:
        params: Dict[str, Any] = {}
        if channel_id is not None:
            params["channelId"] = channel_id
        if dvr_entry_id is not None:
            params["dvrId"] = dvr_entry_id
        return await self.send_and_wait("getTicket", params)
