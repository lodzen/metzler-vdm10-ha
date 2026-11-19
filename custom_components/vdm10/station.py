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
    CONF_FRITZ_DOMAIN,
    CONF_FRITZ_HOST,
    CONF_FRITZ_PASS,
    CONF_FRITZ_PORT,
    CONF_FRITZ_TARGET,
    CONF_FRITZ_USER,
    CONF_INBOUND_TARGET,
    CONF_LISTEN_PORT,
    CONF_LOCAL_IP,
    CONF_PASSWORD,
    CONF_REGISTRAR_HOST,
    CONF_REGISTRAR_PORT,
    CONF_RTSP_PORT,
    CONF_SERIAL,
    CONF_SIP_DOMAIN,
    CONF_SIP_HOST,
    CONF_SIP_PASS,
    CONF_SIP_PORT,
    CONF_SIP_SRTP,
    CONF_SIP_TARGET,
    CONF_SIP_USER,
    CONF_USERNAME,
    DEFAULT_DOOR_USER,
    DEFAULT_FRITZ_DOMAIN,
    DEFAULT_FRITZ_PORT,
    DEFAULT_LISTEN_PORT,
    DEFAULT_REGISTRAR_PORT,
    DEFAULT_RTSP_PORT,
    DEFAULT_SIP_PORT,
)

if TYPE_CHECKING:
    from metzler.station.station import IndoorStation

_LOGGER = logging.getLogger(__name__)

_COMPONENT_LOGGER_NAME = __name__.rsplit(".", 1)[0]  # custom_components.vdm10


class _LibraryLogForward(logging.Handler):
    """Re-emit library records under this integration's logger name.

    The library logs everything on one flat logger ("indoor") - forwarding
    (rather than propagation) renames each record to
    ``custom_components.vdm10.indoor`` so HA's log output and the ``logs:``
    level control attribute them to the component, not the library.
    """

    def emit(self, record: logging.LogRecord) -> None:
        record.name = f"{_COMPONENT_LOGGER_NAME}.{record.name}"
        target = logging.getLogger(record.name)
        if target.isEnabledFor(record.levelno):
            target.handle(record)


# re-parent under the integration so `logs: custom_components.vdm10` gates
# record creation on the library logger; propagate stays off - the handler
# above re-names each record to the component instead of letting it bubble
# up under the original "indoor" name
_library_log = logging.getLogger("indoor")
_library_log.parent = logging.getLogger(_COMPONENT_LOGGER_NAME)
_library_log.propagate = False
_library_log.addHandler(_LibraryLogForward())


def build_options(entry_data: dict):
    """argparse namespace for ``IndoorStation`` - built through the library's own
    parser+finalize so every library default (RTP range, expires, heartbeat,
    talkback port, SRTP handling) stays library-owned."""
    from metzler.cli import build_parser, finalize_options

    ap = build_parser({})
    argv = [
        "--server",
        entry_data[CONF_REGISTRAR_HOST],
        "--server-port",
        str(entry_data.get(CONF_REGISTRAR_PORT, DEFAULT_REGISTRAR_PORT)),
        "--username",
        entry_data[CONF_USERNAME],
        "--password",
        entry_data.get(CONF_PASSWORD, ""),
        "--name",
        "Home Assistant",
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
    argv += _provider_argv(entry_data)
    o = ap.parse_args(argv)
    o.ha_mode = True  # programmatic flag only - NOT a cli.py option
    finalize_options(o, ap)
    o.call_events = None  # wired by async_start (needs the HA loop callback)
    o.ha_mic_feed = None  # wired by async_start
    o.ha_on_audio = None  # v1: no consumer for door-audio frames
    return o


def _provider_argv(entry_data: dict) -> list[str]:
    """optional SIP provider (Linphone/PBX) + Fritz!Box trunk args.

    Empty host = disabled. When either trunk is configured, an incoming door
    call is parked for HA *and* rings the trunk - first answer wins (library
    race semantics); the HA-side win CANCELs the trunk legs.
    """
    argv: list[str] = []
    if entry_data.get(CONF_SIP_HOST):
        argv += [
            "--sip-host",
            entry_data[CONF_SIP_HOST],
            "--sip-port",
            str(entry_data.get(CONF_SIP_PORT, DEFAULT_SIP_PORT)),
            "--sip-user",
            entry_data.get(CONF_SIP_USER, ""),
            "--sip-pass",
            entry_data.get(CONF_SIP_PASS, ""),
        ]
        if entry_data.get(CONF_SIP_TARGET):
            argv += ["--sip-target", entry_data[CONF_SIP_TARGET]]
        if entry_data.get(CONF_SIP_DOMAIN):
            argv += ["--sip-domain", entry_data[CONF_SIP_DOMAIN]]
        argv += ["--sip-srtp" if entry_data.get(CONF_SIP_SRTP, True) else "--no-sip-srtp"]
    if entry_data.get(CONF_FRITZ_HOST):
        argv += [
            "--fritz",
            "--fritz-host",
            entry_data[CONF_FRITZ_HOST],
            "--fritz-port",
            str(entry_data.get(CONF_FRITZ_PORT, DEFAULT_FRITZ_PORT)),
            "--fritz-domain",
            entry_data.get(CONF_FRITZ_DOMAIN, DEFAULT_FRITZ_DOMAIN),
            "--fritz-user",
            entry_data.get(CONF_FRITZ_USER, ""),
            "--fritz-pass",
            entry_data.get(CONF_FRITZ_PASS, ""),
        ]
        if entry_data.get(CONF_FRITZ_TARGET):
            argv += ["--fritz-call-button", entry_data[CONF_FRITZ_TARGET]]
    if argv:  # a trunk exists -> inbound SIP calls ring the intercom
        argv += ["--inbound-target", entry_data.get(CONF_INBOUND_TARGET) or _main_indoor_ext(entry_data)]
    return argv


def _main_indoor_ext(entry_data: dict) -> str:
    """the registrar IS the main indoor: devLongNum `1000000000N` with N=0
    (intercom-registration.md room-class table; seen live: .38 INVITEs as
    sip:10000000000@...). It can't be reached VIA the registrar - a call to
    its own number is silently dropped - so the default dials it directly on
    :5060 (`num@ip`, the firmware's netAltRoute path). A nonstandard username
    scheme gets no guess - the field stays editable."""
    u = entry_data.get(CONF_USERNAME, "")
    host = entry_data.get(CONF_REGISTRAR_HOST, "")
    if u.startswith("1000000000") and len(u) == 11 and host:
        return f"{u[:10]}0@{host}"
    return ""


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
        opts = await self._hass.async_add_executor_job(
            build_options,
            {**self._entry.data, **self._entry.options},  # options flow overrides data
        )
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
