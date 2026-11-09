"""Doorbell ring binary sensor: on only while a call sits in `early` state."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import Vdm10ConfigEntry
from .const import DOMAIN
from .coordinator import Vdm10Runtime


async def async_setup_entry(hass, entry: Vdm10ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([DoorbellRingSensor(entry.runtime_data)])


class DoorbellRingSensor(BinarySensorEntity):
    """on while the door/intercom call is ringing (early); an established call
    is answered - it no longer 'rings'. Unavailable while unregistered."""

    _attr_has_entity_name = True
    _attr_translation_key = "doorbell_ringing"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, runtime: Vdm10Runtime) -> None:
        self.runtime = runtime
        self._attr_unique_id = f"vdm10_{runtime.serial}_doorbell"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.serial)},
            name=runtime.entry.title,
            manufacturer="Metzler",
            model="VDM10",
        )

    @property
    def is_on(self) -> bool:
        return self.runtime.ringing_call is not None

    @property
    def available(self) -> bool:
        return self.runtime.registered

    @property
    def extra_state_attributes(self) -> dict:
        sess = self.runtime.ringing_call
        return {"caller": sess.caller, "call_id": sess.call_id} if sess else {}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.runtime.signal, self._handle_runtime_update))

    @callback
    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()
