"""Best-effort go2rtc wiring for the door camera stream.

Talks to whatever go2rtc instance HA is connected to - the embedded one
started by the ``go2rtc`` integration, or a self-hosted instance configured
via ``go2rtc: url:`` - through the same ``Go2RtcRestClient`` HA uses. Runs
as a background task because the go2rtc integration may finish setting up
after this entry; polling ``hass.data["go2rtc"]`` waits for it.

What it configures on the camera stream (identifier matches
``get_camera_identifier``: ``<platform_domain>_<unique_id>``):

- the RTSP producer plus HA's ffmpeg opus mirror, mirroring
  ``go2rtc.WebRTCProvider._update_stream_source`` so a later WebRTC offer
  finds the stream already complete and does not re-add it
- an ``isapi://`` producer when the go2rtc build supports the scheme -
  that is the Hikvision two-way-audio backchannel (the embedded binary
  does not load the isapi module, so this activates only on self-hosted
  instances that do)
- ``preload`` on the stream when the ``preload_stream`` option is enabled,
  keeping the door RTSP connection hot so a WebRTC offer answers instantly
  instead of racing the cold-connect (default off - it costs a constant
  media pull in standby)
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import quote

from homeassistant.core import HomeAssistant

from .const import (
    CONF_DOOR_HOST,
    CONF_DOOR_PASSWORD,
    CONF_DOOR_USER,
    CONF_PRELOAD_STREAM,
    DEFAULT_DOOR_USER,
    DOOR_RTSP_PORT,
    DOOR_STREAM_PATH,
)

_LOGGER = logging.getLogger(__name__)

_GO2RTC_HASS_KEY = "go2rtc"
_WAIT_SECONDS = 90
_POLL_INTERVAL = 2

# stream identifier -> isapi producer url, for streams whose backchannel must
# survive HA's own producer rewrites (see _patch_streams_add)
_ISAPI_STREAMS: dict[str, str] = {}
_STREAMS_ADD_PATCHED = False


def _patch_streams_add() -> None:
    """Re-append our isapi producer to HA's own stream rewrites.

    go2rtc's ``/api/streams`` masks credentials in producer urls (``***``), so
    ``_update_stream_source``'s ``stream_source == producer.url`` check never
    matches a credentialed camera - every WebRTC offer and go2rtc still then
    rewrites the producers as plain ``[rtsp, ffmpeg]``, dropping the isapi
    backchannel on the very session that needs it. Wrapping ``_StreamClient.add``
    re-appends it on every rewrite of a registered stream; a no-op for any
    stream that isn't ours."""
    global _STREAMS_ADD_PATCHED
    if _STREAMS_ADD_PATCHED:
        return
    try:
        from go2rtc_client import rest as _rest
    except ImportError:
        return
    orig_add = _rest._StreamClient.add

    async def _add(self, name, sources):
        isapi = _ISAPI_STREAMS.get(name)
        if isapi is not None:
            if isinstance(sources, str):
                sources = [sources]
            if not any(s.startswith("isapi://") for s in sources):
                sources = [*sources, isapi]
        return await orig_add(self, name, sources)

    _rest._StreamClient.add = _add
    _STREAMS_ADD_PATCHED = True


def forget_stream(runtime) -> None:
    """drop the entry's isapi registration on unload - the wrapper keyed on
    this identifier must not outlive the runtime that owns it."""
    _ISAPI_STREAMS.pop(f"vdm10_vdm10_{runtime.serial}_door_camera", None)


def _door_rtsp_url(runtime) -> str:
    cfg = {**runtime.entry.data, **runtime.entry.options}
    user = quote(str(cfg.get(CONF_DOOR_USER, DEFAULT_DOOR_USER)), safe="")
    pwd = quote(str(cfg.get(CONF_DOOR_PASSWORD, "")), safe="")
    auth = f"{user}:{pwd}@" if pwd else ""
    # plain URL only: go2rtc's provider dedupes by exact string match against
    # camera.stream_source() - any #param suffix makes HA re-add the producer
    return f"rtsp://{auth}{cfg[CONF_DOOR_HOST]}:{DOOR_RTSP_PORT}{DOOR_STREAM_PATH}"


def _isapi_url(runtime) -> str:
    cfg = {**runtime.entry.data, **runtime.entry.options}
    user = quote(str(cfg.get(CONF_DOOR_USER, DEFAULT_DOOR_USER)), safe="")
    pwd = quote(str(cfg.get(CONF_DOOR_PASSWORD, "")), safe="")
    return f"isapi://{user}:{pwd}@{cfg[CONF_DOOR_HOST]}/"


async def async_configure(hass: HomeAssistant, runtime) -> None:
    """register the door stream on HA's go2rtc and enable preload.

    Fully best-effort: any failure (no go2rtc, unsupported scheme, API
    error) is logged at debug and never affects the entry."""
    try:
        from go2rtc_client.rest import Go2RtcRestClient
    except ImportError:
        _LOGGER.debug("go2rtc_client unavailable - skipping go2rtc stream setup")
        return
    go2rtc_cfg = await _wait_for_go2rtc(hass)
    if go2rtc_cfg is None:
        _LOGGER.debug("go2rtc integration not set up - skipping stream setup")
        return
    client = Go2RtcRestClient(go2rtc_cfg.session, go2rtc_cfg.url)
    # get_camera_identifier: <platform_domain>_<entity unique_id>
    identifier = f"vdm10_vdm10_{runtime.serial}_door_camera"
    needs_isapi_retry = True
    try:
        # the entry reporting hass.data is set when its setup finishes, but the
        # managed process may still be a few seconds from listening - retry
        # briefly before falling back to the background isapi watcher.
        for attempt in range(_SCHEMES_RETRY_ATTEMPTS):
            try:
                schemes = await client.schemes.list()
                break
            except Exception:
                if attempt == _SCHEMES_RETRY_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(1)
        if "isapi" not in schemes and await _respawn_for_isapi(hass, go2rtc_cfg, client):
            schemes = await client.schemes.list()
        producers = [_door_rtsp_url(runtime), f"ffmpeg:{identifier}#audio=opus#query=log_level=debug"]
        if "isapi" in schemes:
            producers.append(_isapi_url(runtime))
            needs_isapi_retry = False
            _patch_streams_add()
            _ISAPI_STREAMS[identifier] = _isapi_url(runtime)
        # streams.add REPLACES the producer list - always write the full set.
        # HA's own _update_stream_source then finds the rtsp url present and
        # never rewrites, so the isapi producer survives later offers.
        streams = await client.streams.list()
        existing = streams.get(identifier)
        known = {p.url for p in existing.producers} if existing else set()
        _LOGGER.info(
            "go2rtc stream %s: isapi=%s in schemes, producers=%s (was %s)",
            identifier,
            "isapi" in schemes,
            [_redact(p) for p in producers],
            sorted(_redact(k) for k in known),
        )
        if known != set(producers):
            await client.streams.add(identifier, producers)
        if "isapi" not in schemes:
            _LOGGER.warning(
                "go2rtc has no isapi module yet - talkback unavailable; "
                "stream %s runs camera-only while a background retry watches "
                "for the module (~60s).",
                identifier,
            )
    except Exception:
        # e.g. the managed process was still starting and the API refused -
        # HA's lazy producer write then leaves the stream camera-only, so the
        # retry is the only path that adds the backchannel later.
        _LOGGER.warning("go2rtc stream setup failed - will retry for isapi", exc_info=True)
    if needs_isapi_retry:
        _schedule_isapi_retry(hass, runtime, client, identifier)
    await _apply_preload(hass, runtime, client, identifier)


def _redact(url: str) -> str:
    """log-safe url: strip credentials, keep scheme + host part"""
    return url.split("@", 1)[-1] if "@" in url else url.split(":", 1)[0]


_SCHEMES_RETRY_ATTEMPTS = 10  # ~10s for the managed process to start listening
_ISAPI_RECHECK_ATTEMPTS = 12  # ~60s total, covers a late watchdog respawn
_ISAPI_RECHECK_INTERVAL = 5


def _schedule_isapi_retry(hass: HomeAssistant, runtime, client, identifier: str) -> None:
    """isapi was missing at configure time - the module can still land late
    (watchdog respawn slower than our poll). Re-check once after the respawn
    window and rewrite the stream with the full producer set if it showed
    up; silent no-op otherwise."""

    async def _recheck() -> None:
        for _ in range(_ISAPI_RECHECK_ATTEMPTS):
            await asyncio.sleep(_ISAPI_RECHECK_INTERVAL)
            try:
                streams = await client.streams.list()
                existing = streams.get(identifier)
                known = {p.url for p in existing.producers} if existing else set()
                if any(p.startswith("isapi://") for p in known):
                    return  # already present - nothing to heal
                if "isapi" not in await client.schemes.list():
                    continue
                producers = [
                    _door_rtsp_url(runtime),
                    f"ffmpeg:{identifier}#audio=opus#query=log_level=debug",
                    _isapi_url(runtime),
                ]
                await client.streams.add(identifier, producers)
                _patch_streams_add()
                _ISAPI_STREAMS[identifier] = _isapi_url(runtime)
                _LOGGER.info("go2rtc stream %s: isapi module appeared - talkback enabled", identifier)
                return
            except Exception:
                continue  # transient API error - keep watching within the window

    hass.async_create_background_task(_recheck(), f"vdm10_isapi_recheck_{identifier}")


async def _apply_preload(hass: HomeAssistant, runtime, client, identifier: str) -> None:
    """sync go2rtc preload with the ``preload_stream`` option (off by
    default): a hot stream costs a constant few Mbit/s from the door plus
    its encoder running 24/7. It goes through the camera prefs so it
    survives provider rebinds; the stored pref alone does not notify the
    provider, so enable/disable directly too."""
    want = bool({**runtime.entry.data, **runtime.entry.options}.get(CONF_PRELOAD_STREAM, False))
    try:
        if entity_id := _door_camera_entity_id(hass, runtime):
            from homeassistant.components.camera import DATA_CAMERA_PREFS

            await hass.data[DATA_CAMERA_PREFS].async_update(entity_id, preload_stream=want)
        preloads = await client.preload.list()
        if want and identifier not in preloads:
            await client.preload.enable(identifier)
        elif not want and identifier in preloads:
            await client.preload.disable(identifier)
        _LOGGER.debug("go2rtc stream %s configured (preload=%s)", identifier, want)
    except Exception:
        _LOGGER.debug("go2rtc preload setup failed", exc_info=True)


async def _wait_for_go2rtc(hass: HomeAssistant):
    """the go2rtc entry may still be setting up - poll hass.data briefly"""
    for _ in range(_WAIT_SECONDS // _POLL_INTERVAL):
        if (cfg := hass.data.get(_GO2RTC_HASS_KEY)) is not None:
            return cfg
        await asyncio.sleep(_POLL_INTERVAL)
    return None


async def _respawn_for_isapi(hass: HomeAssistant, go2rtc_cfg, client) -> bool:
    """the HA-managed go2rtc started before this package imported, so our
    _APP_MODULES patch missed its generated config. HA's watchdog respawns
    the process via _start() which regenerates the yaml - killing it once
    makes the patched module set (with isapi) land. Only ever targets the
    HA-managed instance; a self-hosted url is left alone."""
    if "11984" not in (go2rtc_cfg.url or ""):
        return False
    try:
        from homeassistant.components.go2rtc import server as _g2s

        if "isapi" not in _g2s._APP_MODULES:
            return False  # patch did not land in this process - nothing to do
        if not await hass.async_add_executor_job(_kill_go2rtc):
            return False
        for _ in range(15):  # watchdog respawn takes a few seconds
            await asyncio.sleep(1)
            try:
                if "isapi" in await client.schemes.list():
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


def _kill_go2rtc() -> bool:
    """SIGTERM the HA-managed go2rtc process; the watchdog respawns it with
    a regenerated config. Runs in the executor - /proc scans + kill block."""
    pids = _go2rtc_pids()
    for pid in pids:
        os.kill(pid, 15)
    return bool(pids)


def _go2rtc_pids() -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            argv0 = argv[0].decode(errors="replace")
            if argv0 == "go2rtc" or argv0.endswith("/go2rtc"):
                out.append(int(entry.name))
        except OSError:
            pass
    return out


def _door_camera_entity_id(hass: HomeAssistant, runtime) -> str | None:
    """resolve the door camera entity_id by unique_id (user may rename it)"""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    unique = f"vdm10_{runtime.serial}_door_camera"
    return next(
        (e.entity_id for e in registry.entities.values() if e.unique_id == unique),
        None,
    )
