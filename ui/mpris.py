"""
Minimalna implementacja MPRIS2, dzieki ktorej GNOME Shell (quick settings /
media OSD / ekran blokady) pokazuje aktualnie odtwarzany kanal oraz obsluguje
Play/Pause/Stop/Next/Previous (Next/Previous = kanal +1/-1).

Zaimplementowane bez zewnetrznych zaleznosci (pydbus) - bezposrednio przez
Gio.DBusConnection i XML-owa definicje interfejsu.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from gi.repository import Gio, GLib

logger = logging.getLogger("tvh.mpris")

BUS_NAME = "org.mpris.MediaPlayer2.TvhGnomeClient"
OBJECT_PATH = "/org/mpris/MediaPlayer2"

INTROSPECTION_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Play"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Next"/>
    <method name="Previous"/>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


class MprisService:
    def __init__(self, on_play_pause: Callable[[], None], on_stop: Callable[[], None],
                 on_next: Callable[[], None], on_previous: Callable[[], None]) -> None:
        self._on_play_pause = on_play_pause
        self._on_stop = on_stop
        self._on_next = on_next
        self._on_previous = on_previous

        self._playback_status = "Stopped"
        self._metadata = {
            "mpris:trackid": GLib.Variant("o", "/org/mpris/MediaPlayer2/TrackList/NoTrack"),
            "xesam:title": GLib.Variant("s", ""),
        }
        self._volume = 1.0
        self._registration_ids = []
        self._conn: Optional[Gio.DBusConnection] = None
        self._owner_id = 0

    def start(self) -> None:
        self._owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            BUS_NAME,
            Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired,
            None,
            None,
        )

    def stop(self) -> None:
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)

    def _on_bus_acquired(self, connection: Gio.DBusConnection, name: str) -> None:
        self._conn = connection
        node_info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        for iface in node_info.interfaces:
            reg_id = connection.register_object(
                OBJECT_PATH,
                iface,
                self._handle_method_call,
                self._handle_get_property,
                self._handle_set_property,
            )
            self._registration_ids.append(reg_id)
        logger.info("MPRIS2 zarejestrowany na %s", BUS_NAME)

    # ------------------------------------------------------------------ #
    def _handle_method_call(self, connection, sender, object_path, interface_name,
                             method_name, params, invocation) -> None:
        if method_name in ("Play", "PlayPause"):
            self._on_play_pause()
        elif method_name == "Pause":
            self._on_play_pause()
        elif method_name == "Stop":
            self._on_stop()
        elif method_name == "Next":
            self._on_next()
        elif method_name == "Previous":
            self._on_previous()
        elif method_name in ("Raise", "Quit"):
            pass
        invocation.return_value(None)

    def _handle_get_property(self, connection, sender, object_path, interface_name, property_name):
        if interface_name == "org.mpris.MediaPlayer2":
            values = {
                "CanQuit": GLib.Variant("b", False),
                "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", "TVH GNOME Client"),
                "DesktopEntry": GLib.Variant("s", "io.github.tvh-gnome-client"),
                "SupportedUriSchemes": GLib.Variant("as", []),
                "SupportedMimeTypes": GLib.Variant("as", []),
            }
            return values.get(property_name)
        if interface_name == "org.mpris.MediaPlayer2.Player":
            if property_name == "PlaybackStatus":
                return GLib.Variant("s", self._playback_status)
            if property_name == "Metadata":
                return GLib.Variant("a{sv}", self._metadata)
            if property_name == "Volume":
                return GLib.Variant("d", self._volume)
            if property_name in ("CanGoNext", "CanGoPrevious", "CanPlay", "CanPause", "CanControl"):
                return GLib.Variant("b", True)
        return None

    def _handle_set_property(self, connection, sender, object_path, interface_name,
                              property_name, value) -> bool:
        if interface_name == "org.mpris.MediaPlayer2.Player" and property_name == "Volume":
            self._volume = value.unpack()
        return True

    # ------------------------------------------------------------------ #
    def update_now_playing(self, title: str, channel_name: str, art_url: Optional[str] = None) -> None:
        meta = {
            "mpris:trackid": GLib.Variant("o", "/org/mpris/MediaPlayer2/Track/1"),
            "xesam:title": GLib.Variant("s", title or channel_name),
            "xesam:artist": GLib.Variant("as", [channel_name]),
        }
        if art_url:
            meta["mpris:artUrl"] = GLib.Variant("s", art_url)
        self._metadata = meta
        self._playback_status = "Playing"
        self._emit_properties_changed()

    def set_playback_status(self, status: str) -> None:
        self._playback_status = status
        self._emit_properties_changed()

    def _emit_properties_changed(self) -> None:
        if not self._conn:
            return
        changed = {
            "PlaybackStatus": GLib.Variant("s", self._playback_status),
            "Metadata": GLib.Variant("a{sv}", self._metadata),
        }
        self._conn.emit_signal(
            None,
            OBJECT_PATH,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant(
                "(sa{sv}as)",
                ("org.mpris.MediaPlayer2.Player", changed, []),
            ),
        )
