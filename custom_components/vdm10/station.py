"""Thread bridge around the blocking ``IndoorStation``.

The station owns a synchronous UDP SIP listener, register/heartbeat/reaper
threads, and an RTSP talkback server - none of it is asyncio. HA drives it from
the executor; library callbacks are marshalled onto the HA loop via
``call_soon_threadsafe``.
"""

from __future__ import annotations

import logging
import queue
from typing import TYPE_CHECKING

from . import vendor as _vendor_loader  # noqa: F401 - vendored metzler on sys.path (release installs)
from .const import (
    CONF_DOOR_PASSWORD,
    CONF_DOOR_USER,
    CONF_LISTEN_PORT,
    CONF_LOCAL_IP,
    CONF_PASSWORD,
    CONF_REGISTRAR_HOST,
    CONF_REGISTRAR_PORT,
    CONF_RTSP_PORT,
    CONF_SERIAL,
    CONF_USERNAME,
    DEFAULT_DOOR_USER,
    DEFAULT_LISTEN_PORT,
    DEFAULT_REGISTRAR_PORT,
    DEFAULT_RTSP_PORT,
)

if TYPE_CHECKING:
    from metzler.station.station import IndoorStation

_LOGGER = logging.getLogger(__name__)


def build_options(entry_data: dict):
    """argparse namespace for ``IndoorStation`` - built through the library's own
    parser+finalize so every library default (RTP range, expires, heartbeat,
    talkback port, SRTP handling) stays library-owned."""
    from metzler.cli import build_parser, finalize_options

    ap = build_parser({})
    o = ap.parse_args(
        [
            "--server",
            entry_data[CONF_REGISTRAR_HOST],
            "--server-port",
            str(entry_data.get(CONF_REGISTRAR_PORT, DEFAULT_REGISTRAR_PORT)),
            "--username",
            entry_data[CONF_USERNAME],
            "--password",
            entry_data.get(CONF_PASSWORD, ""),
            "--name",
            "vdm10-ha",
            "--serial",
            entry_data[CONF_SERIAL],
            "--local-ip",
            entry_data[CONF_LOCAL_IP],
            "--listen-port",
            str(entry_data.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT)),
            "--rtsp-port",
            str(entry_data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)),
            "--door-isapi-user",
            entry_data.get(CONF_DOOR_USER, DEFAULT_DOOR_USER),
            "--door-isapi-pass",
            entry_data.get(CONF_DOOR_PASSWORD, ""),
            "--auto-answer",
            "off",  # belt and suspenders: ha_mode already parks calls
        ]
    )
    o.ha_mode = True  # programmatic flag only - NOT a cli.py option
    finalize_options(o, ap)
    o.call_events = None  # wired by async_start (needs the HA loop callback)
    o.ha_mic_feed = None  # wired by async_start
    o.ha_on_audio = None  # v1: no consumer for door-audio frames
    return o


class HaStation:
    """Owns the library station + the mic queue; never blocks the HA loop.

    The mic queue is the fallback talk path (the door/intercom caller pulls our
    :<rtsp_port> RTSP for mic audio): ``feed_mic`` receives PCMU frames from the
    RTSP-server side and returns ``None`` while muted -> the library sends
    silence, so the mic is muted by default.
    """

    def __init__(self, hass, entry) -> None:
        self._hass = hass
        self._entry = entry
        self.station: IndoorStation | None = None
        self.mic_queue: queue.Queue[bytes] = queue.Queue(maxsize=50)
        self.on_call_event = None  # (name: str, call) -> None, invoked on the HA loop

    def _mic_frame(self) -> bytes | None:
        """drain one queued mic frame for the library's RTSP tx; None = muted"""
        try:
            return self.mic_queue.get_nowait()
        except queue.Empty:
            return None

    def push_mic_frame(self, frame: bytes) -> None:
        """enqueue a PCMU frame for talkback (drops oldest first on overflow)"""
        try:
            self.mic_queue.put_nowait(frame)
        except queue.Full:
            try:
                self.mic_queue.get_nowait()
                self.mic_queue.put_nowait(frame)
            except (queue.Empty, queue.Full):
                pass

    def _forward_call_event(self, name: str, call) -> None:
        """runs on a library listener thread - marshal onto the HA loop"""
        if self.on_call_event:
            self._hass.loop.call_soon_threadsafe(self.on_call_event, name, call)

    async def async_start(self) -> None:
        """construct + start the station off the event loop; raises OSError on bind failure"""
        opts = await self._hass.async_add_executor_job(build_options, self._entry.data)
        opts.call_events = self._forward_call_event
        opts.ha_mic_feed = self._mic_frame
        from metzler.station.station import IndoorStation

        self.station = await self._hass.async_add_executor_job(IndoorStation, opts)
        # daemon listener/register/heartbeat/reaper threads; NOT run() - that
        # installs a SIGTERM handler (main thread only) and blocks
        self.station.start_threads()
        _LOGGER.info(
            "VDM10 station up: ext %s@%s:%s (sip :%s, rtsp :%s)",
            opts.username,
            opts.server,
            opts.server_port,
            self.station.bind_port,
            opts.rtsp_port,
        )

    def shutdown_sync(self) -> None:
        """blocking unregister + call teardown - call via async_add_executor_job"""
        if self.station is not None:
            self.station.shutdown()
            self.station = None

    @property
    def registered(self) -> bool:
        return bool(self.station and self.station.registered.is_set())
