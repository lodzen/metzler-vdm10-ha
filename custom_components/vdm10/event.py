"""Doorbell press event entity - one event per outdoor-station ring.

The binary_sensor tracks ring *state* (on while ringing); this entity emits
the *edge*. It should stay Alexa-unexposed: the integration forwards the
press under the door camera's endpoint, where Amazon finds one device with
video + doorbell ("CAMERA", "DOORBELL"). If it *is* exposed (e.g. Nabu Casa,
where the direct path can't reach the config), the patched state listener
delivers the press instead - same camera endpoint, one extra inert entry in
the Alexa app.
"""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import Vdm10ConfigEntry, _async_fire_doorbell_press
from .const import DOMAIN
from .coordinator import Vdm10Runtime

EVENT_PRESS = "press"


async def async_setup_entry(hass, entry: Vdm10ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([Vdm10DoorbellEvent(entry.runtime_data)])


class Vdm10DoorbellEvent(EventEntity):
    """one ``press`` per ring, deduped by Call-ID: the runtime signal also
    fires for registration flaps and other sessions, so the rising edge is
    detected here instead of trusting each dispatch."""

    _attr_has_entity_name = True
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_translation_key = "doorbell"
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, runtime: Vdm10Runtime) -> None:
        self.runtime = runtime
        self._attr_event_types = [EVENT_PRESS]
        self._attr_unique_id = f"vdm10_{runtime.serial}_doorbell_press"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, runtime.serial)})
        self._seen_call_id: str | None = None

    @property
    def available(self) -> bool:
        return self.runtime.registered

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.runtime.signal, self._handle_runtime_update))

    @callback
    def _handle_runtime_update(self) -> None:
        fired = False
        sess = self.runtime.door_ringing
        if sess is None:
            self._seen_call_id = None
        elif sess.call_id != self._seen_call_id:
            self._seen_call_id = sess.call_id
            self._trigger_event(EVENT_PRESS, {"caller": sess.caller, "call_id": sess.call_id})
            fired = True
        self.async_write_ha_state()  # also tracks availability flips
        if fired:
            # when this entity is Alexa-exposed the (patched) state listener
            # delivers the press - this task only covers the unexposed case
            self.hass.async_create_task(_async_fire_doorbell_press(self.hass, self.entity_id))

    @callback
    def simulate_press(self) -> None:
        """fires the same edge a real ring would - called by the
        ``vdm10.simulate_doorbell_press`` service so the full Alexa delivery
        chain can be tested without pressing the physical button"""
        self._trigger_event(EVENT_PRESS, {"caller": "simulated", "call_id": f"simulated-{id(self)}"})
        self.async_write_ha_state()
        self.hass.async_create_task(_async_fire_doorbell_press(self.hass, self.entity_id))
