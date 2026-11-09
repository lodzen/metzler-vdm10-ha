"""Diagnostic sensor: registration state + last error of the VDM10 station."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import Vdm10ConfigEntry
from .const import DOMAIN
from .coordinator import Vdm10Runtime

STATUS_OPTIONS = ["registered", "connecting", "unavailable"]


async def async_setup_entry(hass, entry: Vdm10ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([Vdm10StatusSensor(entry.runtime_data)])


class Vdm10StatusSensor(SensorEntity):
    """registered | connecting | unavailable, with last_error as attribute"""

    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lan-connect"

    def __init__(self, runtime: Vdm10Runtime) -> None:
        self.runtime = runtime
        self._attr_unique_id = f"vdm10_{runtime.serial}_status"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, runtime.serial)})

    @property
    def native_value(self) -> str:
        if self.runtime.registered:
            return "registered"
        return "connecting" if self.runtime.station is not None else "unavailable"

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "last_error": self.runtime.last_error,
            "active_calls": len(self.runtime.sessions),
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.runtime.signal, self._handle_runtime_update))

    @callback
    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()
