"""Doorbell press event entity - the Alexa DoorbellEventSource path.

An event entity with device_class doorbell maps to DisplayCategory.DOORBELL
in HA's alexa discovery, so it registers as a doorbell with no
entity_config - the user only needs the ``event`` domain in their
smart_home filter.
"""

from conftest import fire_indoor_ring, fire_ring

_ENTITY = "event.vdm10_test_doorbell"


def _state(hass):
    return hass.states.get(_ENTITY)


async def test_entity_registers_as_doorbell(hass, runtime):
    state = _state(hass)
    assert state is not None
    assert state.attributes.get("device_class") == "doorbell"
    assert state.state == "unavailable"  # dead registrar port -> unregistered


async def test_ring_fires_one_press(hass, runtime):
    runtime.station.station.registered.set()
    runtime._poll_registration(None)
    await fire_ring(runtime)
    await hass.async_block_till_done()
    state = _state(hass)
    assert state.state not in ("unknown", "unavailable")
    assert state.attributes["event_type"] == "press"
    assert state.attributes["caller"] == "10010100000"
    assert state.attributes["call_id"] == "doorcall1"


async def test_repeated_signal_does_not_refire(hass, runtime):
    """registration flaps during a ring must not emit a second press"""
    runtime.station.station.registered.set()
    runtime._poll_registration(None)
    await fire_ring(runtime)
    await hass.async_block_till_done()
    first = _state(hass).state
    runtime._poll_registration(None)  # signal again, same call still ringing
    await hass.async_block_till_done()
    assert _state(hass).state == first


async def test_indoor_ring_does_not_fire(hass, runtime):
    """extension calls are not doorbell presses"""
    runtime.station.station.registered.set()
    runtime._poll_registration(None)
    await fire_indoor_ring(runtime)
    await hass.async_block_till_done()
    assert _state(hass).state == "unknown"


async def test_new_ring_after_end_refires(hass, runtime):
    runtime.station.station.registered.set()
    runtime._poll_registration(None)
    await fire_ring(runtime, call_id="doorcall1")
    await hass.async_block_till_done()
    first = _state(hass).state
    call = runtime.station.station.calls["doorcall1"]
    runtime.station.station._close_call(call, "doorcall1")
    await fire_ring(runtime, call_id="doorcall2")
    await hass.async_block_till_done()
    state = _state(hass)
    assert state.state != first or state.attributes["call_id"] == "doorcall2"
    assert state.attributes["call_id"] == "doorcall2"
