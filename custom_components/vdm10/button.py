"""Explicit call controls: answer while ringing, hang up while active.

HA buttons cannot be disabled, so out-of-window presses are a no-op + warning
(``available`` greys them in the UI).
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import Vdm10ConfigEntry
from .const import DOMAIN
from .coordinator import Vdm10Runtime


async def async_setup_entry(hass, entry: Vdm10ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = entry.runtime_data
    async_add_entities([AnswerCallButton(runtime), HangupButton(runtime)])


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
        self._attr_unique_id = f"vdm10_{runtime.serial}_answer"

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
        self._attr_unique_id = f"vdm10_{runtime.serial}_hangup"

    @property
    def available(self) -> bool:
        return self.runtime.active_call is not None

    async def async_press(self) -> None:
        await self.runtime.async_hangup_call()
