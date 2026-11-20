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
    try:
        schemes = await client.schemes.list()
        if "isapi" not in schemes and await _respawn_for_isapi(hass, go2rtc_cfg, client):
            schemes = await client.schemes.list()
        producers = [_door_rtsp_url(runtime), f"ffmpeg:{identifier}#audio=opus#query=log_level=debug"]
        if "isapi" in schemes:
            producers.append(_isapi_url(runtime))
        # streams.add REPLACES the producer list - always write the full set.
        # HA's own _update_stream_source then finds the rtsp url present and
        # never rewrites, so the isapi producer survives later offers.
        streams = await client.streams.list()
        existing = streams.get(identifier)
        known = {p.url for p in existing.producers} if existing else set()
        if known != set(producers):
            await client.streams.add(identifier, producers)
    except Exception:
        _LOGGER.debug("go2rtc stream setup failed", exc_info=True)
    await _apply_preload(hass, runtime, client, identifier)


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
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
            if "/bin/go2rtc" in cmd:
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
