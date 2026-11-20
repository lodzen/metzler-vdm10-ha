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


async def test_camera_doorbell_config_yields_doorbell_interface(hass, runtime):
    """a camera with display_categories DOORBELL gets DoorbellEventSource"""
    from types import SimpleNamespace

    from homeassistant.components.alexa.entities import ENTITY_ADAPTERS

    cam_state = hass.states.get("camera.vdm10_test_door_camera")
    assert cam_state is not None
    config = SimpleNamespace(
        entity_config={cam_state.entity_id: {"display_categories": "DOORBELL"}},
        should_expose=lambda entity_id: True,
        generate_alexa_id=lambda entity_id: entity_id,
    )
    names = {iface.name() for iface in ENTITY_ADAPTERS["camera"](hass, config, cam_state).interfaces()}
    assert "Alexa.DoorbellEventSource" in names


async def test_doorbell_press_redirects_to_camera_endpoint(hass, runtime, monkeypatch):
    """the sender must hand the camera adapter, not the event adapter"""
    from types import SimpleNamespace

    from homeassistant.components.alexa.entities import ENTITY_ADAPTERS

    import custom_components.vdm10 as vdm10

    sent = []

    async def _spy(hass, config, alexa_entity):
        sent.append(alexa_entity.entity_id)

    monkeypatch.setattr(vdm10, "_orig_send_doorbell", _spy)
    config = SimpleNamespace(entity_config={})
    event_adapter = ENTITY_ADAPTERS["event"](hass, config, _state(hass))
    await vdm10._send_doorbell(hass, config, event_adapter)
    assert sent == ["camera.vdm10_test_door_camera"]
