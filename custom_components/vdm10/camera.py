"""Door camera - strictly pull-on-demand.

The entity owns no puller and never connects to the door's RTSP while idle:
- ``stream_source`` is just a URL string handed to HA's ``stream`` integration;
  the stream opens only while a viewer is attached and closes when the last
  viewer leaves (standard stream lifecycle; go2rtc when configured).
- stills are one-shot HTTP fetches from the door's ISAPI snapshot endpoint,
  so no ``use_stream_for_stills`` and no background preview pull either.

Credentials are embedded in the RTSP URL (the door requires Basic auth); they
never appear in logs or entity attributes.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import Vdm10ConfigEntry
from .const import (
    CONF_DOOR_HOST,
    CONF_DOOR_PASSWORD,
    CONF_DOOR_USER,
    DEFAULT_DOOR_USER,
    DOMAIN,
    DOOR_ISAPI_PORT,
    DOOR_RTSP_PORT,
    DOOR_STREAM_PATH,
)
from .coordinator import Vdm10Runtime

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry: Vdm10ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([Vdm10DoorCamera(entry.runtime_data)])


class Vdm10DoorCamera(Camera):
    """the outdoor station's H264 stream; two-way audio rides the same stream
    via go2rtc (ISAPI backchannel) when HA Labs WebRTC talk is enabled"""

    _attr_has_entity_name = True
    _attr_translation_key = "door"
    _attr_supported_features = CameraEntityFeature.STREAM
    # no explicit stream-type/capabilities attr: HA 2026.10 auto-negotiates
    # WebRTC from the registered webrtc providers (go2rtc incl. its ISAPI
    # two-way backchannel); the entity just exposes the RTSP source

    def __init__(self, runtime: Vdm10Runtime) -> None:
        super().__init__()
        self.runtime = runtime
        self._attr_unique_id = f"vdm10_{runtime.serial}_door_camera"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, runtime.serial)})

    async def stream_source(self) -> str:
        """credentials embedded - the door demands Basic auth on RTSP DESCRIBE"""
        cfg = self.runtime.entry.data
        user = quote(str(cfg.get(CONF_DOOR_USER, DEFAULT_DOOR_USER)), safe="")
        pwd = quote(str(cfg.get(CONF_DOOR_PASSWORD, "")), safe="")
        auth = f"{user}:{pwd}@" if pwd else ""
        return f"rtsp://{auth}{cfg[CONF_DOOR_HOST]}:{DOOR_RTSP_PORT}{DOOR_STREAM_PATH}"

    async def async_camera_image(self, width=None, height=None) -> bytes | None:
        """one-shot ISAPI snapshot (Hikvision /Streaming/channels/101/picture) -
        a fresh single HTTP fetch per request, nothing held open"""
        cfg = self.runtime.entry.data
        url = f"http://{cfg[CONF_DOOR_HOST]}:{DOOR_ISAPI_PORT}/ISAPI/Streaming/channels/101/picture"
        try:
            import aiohttp

            auth = aiohttp.BasicAuth(
                str(cfg.get(CONF_DOOR_USER, DEFAULT_DOOR_USER)),
                str(cfg.get(CONF_DOOR_PASSWORD, "")),
            )
            async with (
                aiohttp.ClientSession() as session,
                session.get(url, auth=auth, timeout=aiohttp.ClientTimeout(total=5)) as resp,
            ):
                if resp.status == 200:
                    return await resp.read()
                _LOGGER.debug("door snapshot returned HTTP %s", resp.status)
        except Exception as err:  # door offline / non-Hikvision firmware
            _LOGGER.debug("door snapshot failed: %s", err)
        return None

    @property
    def available(self) -> bool:
        # the door RTSP is independent of SIP registration - keep the camera
        # usable even while the extension is unregistered
        return self.runtime.station is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.runtime.signal, self._handle_runtime_update))

    @callback
    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()
