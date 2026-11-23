"""Doorbell ring binary sensor + per-leg SIP registration diagnostics."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import Vdm10ConfigEntry
from .const import CONF_FRITZ_HOST, CONF_SIP_HOST, DOMAIN
from .coordinator import RingSession, Vdm10Runtime


async def async_setup_entry(hass, entry: Vdm10ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = entry.runtime_data
    cfg = {**entry.data, **entry.options}  # options-flow values land in .options
    entities: list[BinarySensorEntity] = [
        RingSensor(runtime, door=True),
        RingSensor(runtime, door=False),
        Vdm10RegistrationSensor(runtime, "intercom_registration", lambda r: r.registered),
    ]
    if cfg.get(CONF_SIP_HOST):
        entities.append(Vdm10RegistrationSensor(runtime, "provider_registration", lambda r: r.provider_registered))
    if cfg.get(CONF_FRITZ_HOST):
        entities.append(Vdm10RegistrationSensor(runtime, "fritz_registration", lambda r: r.fritz_registered))
    async_add_entities(entities)


class RingSensor(BinarySensorEntity):
    """on while a call is ringing (early); an established call is answered -
    it no longer 'rings'. door=True is the outdoor station's doorbell call,
    door=False an indoor room-station call. Unavailable while unregistered."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    def __init__(self, runtime: Vdm10Runtime, door: bool) -> None:
        self.runtime = runtime
        self._door = door
        self._attr_translation_key = "doorbell_ringing" if door else "indoor_ringing"
        self._attr_unique_id = f"{runtime.serial}_{'doorbell' if door else 'indoor_ringing'}"
        self._attr_icon = "mdi:doorbell-video" if door else "mdi:phone-ring"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, runtime.serial)},
            name=runtime.entry.title,
            manufacturer="Metzler",
            model="VDM10",
        )

    @property
    def _sess(self) -> RingSession | None:
        return self.runtime.door_ringing if self._door else self.runtime.indoor_ringing

    @property
    def is_on(self) -> bool:
        return self._sess is not None

    @property
    def available(self) -> bool:
        return self.runtime.registered

    @property
    def extra_state_attributes(self) -> dict:
        sess = self._sess
        return {"caller": sess.caller, "call_id": sess.call_id} if sess else {}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.runtime.signal, self._handle_runtime_update))

    @callback
    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()


class Vdm10RegistrationSensor(BinarySensorEntity):
    """one diagnostic entity per SIP leg: intercom registrar, provider trunk,
    fritz trunk (the latter two only exist when configured)."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: Vdm10Runtime, key: str, leg) -> None:
        self.runtime = runtime
        self._leg = leg
        self._attr_translation_key = key
        self._attr_unique_id = f"{runtime.serial}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(identifiers={(DOMAIN, self.runtime.serial)})

    @property
    def is_on(self) -> bool:
        return bool(self._leg(self.runtime))

    @property
    def extra_state_attributes(self) -> dict:
        if self._attr_translation_key != "intercom_registration":
            return {}
        return {
            "last_error": self.runtime.last_error,
            "active_calls": len(self.runtime.sessions),
        }

    @property
    def available(self) -> bool:
        return self.runtime.station is not None

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.runtime.signal, self._handle_runtime_update))

    @callback
    def _handle_runtime_update(self) -> None:
        self.async_write_ha_state()
