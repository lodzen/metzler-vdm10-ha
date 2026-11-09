"""Runtime model: the async side of the library station.

Owns the HaStation thread bridge, tracks active calls (a dict keyed by Call-ID -
the library's ``self.calls`` mirrors concurrent calls, so no single
``current_call``), fires the ``vdm10_*`` bus events, and signals entities via
dispatcher. ``answer_call``/``hangup_call`` go through the executor - both send
SIP packets synchronously.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_SERIAL,
    EVENT_ANSWERED,
    EVENT_CALL_ENDED,
    EVENT_RING,
    SIGNAL_VDM10_UPDATED,
)
from .station import HaStation

_LOGGER = logging.getLogger(__name__)

STATUS_POLL_INTERVAL = datetime.timedelta(seconds=5)


@dataclass
class RingSession:
    """one SIP dialog while HA cares about it (early + established)"""

    call_id: str
    caller: str
    door: bool
    state: str = "early"  # early -> established (removed on ended)
    answered_by_us: bool = False


class Vdm10Runtime:
    """per-config-entry runtime, stored on ``entry.runtime_data``"""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        self.hass = hass
        self.entry = entry
        self.station: HaStation | None = None
        self.sessions: dict[str, RingSession] = {}
        self.last_error: str | None = None
        self._registered = False
        self._remove_status_timer = None

    @property
    def signal(self) -> str:
        return f"{SIGNAL_VDM10_UPDATED}_{self.entry.entry_id}"

    @property
    def serial(self) -> str:
        return self.entry.data[CONF_SERIAL]

    @property
    def registered(self) -> bool:
        return bool(self.station and self.station.registered)

    @property
    def ringing_call(self) -> RingSession | None:
        return next((s for s in self.sessions.values() if s.state == "early"), None)

    @property
    def active_call(self) -> RingSession | None:
        return self.ringing_call or next(iter(self.sessions.values()), None)

    async def async_start(self) -> None:
        self.station = HaStation(self.hass, self.entry)
        self.station.on_call_event = self._on_call_event
        await self.station.async_start()
        self._remove_status_timer = async_track_time_interval(self.hass, self._poll_registration, STATUS_POLL_INTERVAL)

    async def async_stop(self) -> None:
        if self._remove_status_timer:
            self._remove_status_timer()
            self._remove_status_timer = None
        if self.station is not None:
            station, self.station = self.station, None
            await self.hass.async_add_executor_job(station.shutdown_sync)
        self.sessions.clear()
        self._registered = False
        async_dispatcher_send(self.hass, self.signal)  # entities go unavailable

    @callback
    def _poll_registration(self, _now) -> None:
        """registration is a threading.Event in the library - poll + diff"""
        if self.registered != self._registered:
            self._registered = self.registered
            async_dispatcher_send(self.hass, self.signal)

    # ---------------------------------------------------------------- events

    @callback
    def _on_call_event(self, name: str, call) -> None:
        """library ``call_events`` hook, now on the HA loop (marshalled by HaStation)"""
        if name == "ring":
            sess = RingSession(call.id, _caller_of(call), bool(call.door))
            self.sessions[call.id] = sess
            self.hass.bus.async_fire(EVENT_RING, {"call_id": call.id, "caller": sess.caller, "door": sess.door})
        elif name == "answered":
            sess = self.sessions.get(call.id)
            if sess is not None:
                sess.state = "established"
                self.hass.bus.async_fire(
                    EVENT_ANSWERED,
                    {"call_id": call.id, "by": "ha" if sess.answered_by_us else "other"},
                )
        elif name == "ended":
            sess = self.sessions.pop(call.id, None)
            reason = getattr(call, "end_reason", None) or "error"
            # the registrar's forked-leg CANCEL is identical for answer-elsewhere,
            # caller cancel and caller timeout -> best-effort label (contract)
            if sess is not None and sess.state == "early" and reason == "cancel":
                reason = "answered_elsewhere"
            self.hass.bus.async_fire(EVENT_CALL_ENDED, {"call_id": call.id, "reason": reason})
        async_dispatcher_send(self.hass, self.signal)

    # ---------------------------------------------------------------- actions

    async def async_answer_call(self) -> bool:
        """answer the ringing call via the library's 200-OK path (executor)"""
        sess = self.ringing_call
        if sess is None or self.station is None or self.station.station is None:
            _LOGGER.warning("vdm10: answer requested but no call is ringing")
            return False
        sess.answered_by_us = True
        ok = await self.hass.async_add_executor_job(self.station.station.answer_call, sess.call_id)
        if not ok:
            sess.answered_by_us = False
            _LOGGER.warning("vdm10: answer failed - call %s no longer ringing", sess.call_id)
        return ok

    async def async_hangup_call(self) -> bool:
        """decline (early) or BYE (established) the active call (executor)"""
        sess = self.active_call
        if sess is None or self.station is None or self.station.station is None:
            _LOGGER.warning("vdm10: hangup requested but no call is active")
            return False
        return await self.hass.async_add_executor_job(self.station.station.hangup_call, sess.call_id)

    def feed_mic(self, frame: bytes) -> None:
        """queue a PCMU frame for the caller's RTSP mic pull (talk control)"""
        if self.station is not None:
            self.station.push_mic_frame(frame)


def _caller_of(call) -> str:
    """``<sip:10010100000@10.0.0.1>;tag=x`` -> ``10010100000``"""
    frm = call.invite.get("From") or ""
    if "sip:" in frm:
        return frm.split("sip:", 1)[1].split("@", 1)[0].split(";", 1)[0]
    return frm or "unknown"
