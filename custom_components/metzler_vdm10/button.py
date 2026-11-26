"""Explicit call controls: answer while ringing, hang up while active.

HA buttons cannot be disabled, so out-of-window presses are a no-op + warning
(``available`` greys them in the UI).
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import Vdm10ConfigEntry
from .const import CONF_DOOR_HOST, CONF_DOOR_PASSWORD, CONF_DOOR_USER, DEFAULT_DOOR_USER, DOMAIN, DOOR_ISAPI_PORT
from .coordinator import Vdm10Runtime

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry: Vdm10ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = entry.runtime_data
    entities: list[ButtonEntity] = [AnswerCallButton(runtime), HangupButton(runtime)]
    if {**entry.data, **entry.options}.get(CONF_DOOR_HOST):
        entities += [UnlockDoorButton(runtime), RebootDoorButton(runtime)]
    async_add_entities(entities)


class _Vdm10Button(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, runtime: Vdm10Runtime) -> None:
        self.runtime = runtime
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, runtime.serial)})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.runtime.signal, self._handle_runtime_update))

    @callback
    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()


class AnswerCallButton(_Vdm10Button):
    _attr_translation_key = "answer_call"
    _attr_icon = "mdi:phone"

    def __init__(self, runtime: Vdm10Runtime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.serial}_answer"

    @property
    def available(self) -> bool:
        return self.runtime.ringing_call is not None

    async def async_press(self) -> None:
        await self.runtime.async_answer_call()


class HangupButton(_Vdm10Button):
    _attr_translation_key = "hangup"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, runtime: Vdm10Runtime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.serial}_hangup"

    @property
    def available(self) -> bool:
        return self.runtime.active_call is not None

    async def async_press(self) -> None:
        await self.runtime.async_hangup_call()


class RebootDoorButton(_Vdm10Button):
    """PUT /ISAPI/System/reboot - the door's web-UI restart action; the RTSP
    server wedges often enough after video-setting changes that a one-click
    reset from HA earns its place."""

    _attr_translation_key = "reboot_door"
    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, runtime: Vdm10Runtime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.serial}_door_reboot"

    @property
    def available(self) -> bool:
        return self.runtime.station is not None

    async def async_press(self) -> None:
        import aiohttp

        cfg = {**self.runtime.entry.data, **self.runtime.entry.options}
        url = f"http://{cfg[CONF_DOOR_HOST]}:{DOOR_ISAPI_PORT}/ISAPI/System/reboot"
        digest = aiohttp.DigestAuthMiddleware(
            login=str(cfg.get(CONF_DOOR_USER, DEFAULT_DOOR_USER)),
            password=str(cfg.get(CONF_DOOR_PASSWORD, "")),
        )
        try:
            async with (
                aiohttp.ClientSession(middlewares=(digest,)) as session,
                session.put(url, timeout=aiohttp.ClientTimeout(total=10)) as resp,
            ):
                if resp.status == 200:
                    _LOGGER.info("door reboot requested at %s", cfg[CONF_DOOR_HOST])
                else:
                    _LOGGER.warning("door reboot at %s returned HTTP %s", cfg[CONF_DOOR_HOST], resp.status)
        except Exception as err:  # door offline / non-Hikvision firmware
            _LOGGER.warning("door reboot at %s failed: %s", cfg[CONF_DOOR_HOST], err)


class UnlockDoorButton(_Vdm10Button):
    """momentary unlock via the door's ISAPI
    PUT /AccessControl/RemoteControl/door/1 <cmd>open</cmd> - the route
    live-verified on this outdoor station (fw V2.2.56); the library's
    isapi_open_door owns the digest/XML and logs the outcome."""

    _attr_translation_key = "unlock_door"
    _attr_icon = "mdi:door-open"

    def __init__(self, runtime: Vdm10Runtime) -> None:
        super().__init__(runtime)
        self._attr_unique_id = f"{runtime.serial}_door_unlock"

    @property
    def available(self) -> bool:
        return self.runtime.station is not None

    async def async_press(self) -> None:
        from metzler.station.door_unlock import isapi_open_door

        cfg = {**self.runtime.entry.data, **self.runtime.entry.options}
        await self.runtime.hass.async_add_executor_job(
            isapi_open_door,
            cfg[CONF_DOOR_HOST],
            str(cfg.get(CONF_DOOR_USER, DEFAULT_DOOR_USER)),
            str(cfg.get(CONF_DOOR_PASSWORD, "")),
            1,
            DOOR_ISAPI_PORT,
        )
