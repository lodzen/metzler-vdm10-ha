"""Doorbell press event entity - the Alexa DoorbellEventSource path.

The door camera is auto-detected as a video doorbell endpoint
(["CAMERA", "DOORBELL"] + DoorbellEventSource) and presses from this
entity are delivered under the camera's endpointId - one Alexa device,
no entity_config YAML needed.
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


async def test_vdm10_camera_auto_detected_as_video_doorbell(hass, runtime):
    """no entity_config at all: our door camera is detected via its unique_id
    and reported the Scrypted way - CAMERA+DOORBELL categories plus the
    DoorbellEventSource interface on the same endpoint"""
    from types import SimpleNamespace

    from homeassistant.components.alexa.entities import ENTITY_ADAPTERS

    cam_state = hass.states.get("camera.vdm10_test_door_camera")
    assert cam_state is not None
    config = SimpleNamespace(
        entity_config={},
        should_expose=lambda entity_id: True,
        generate_alexa_id=lambda entity_id: entity_id,
    )
    adapter = ENTITY_ADAPTERS["camera"](hass, config, cam_state)  # ty: ignore[invalid-argument-type]
    assert adapter.display_categories() == ["CAMERA", "DOORBELL"]
    names = {iface.name() for iface in adapter.interfaces()}
    assert "Alexa.DoorbellEventSource" in names


async def test_duplex_rtc_controller_for_two_way_camera(hass, runtime, monkeypatch):
    """HA's RTCSessionController hardcodes isFullDuplexAudioSupported=False -
    for a camera with resolved two_way_audio the integration must advertise
    duplex instead, else the Echo never opens the mic/audio path"""
    from types import SimpleNamespace

    from homeassistant.components.alexa.entities import ENTITY_ADAPTERS
    from homeassistant.components.camera import StreamType

    import custom_components.metzler_vdm10 as vdm10

    cam_state = hass.states.get("camera.vdm10_test_door_camera")
    assert cam_state is not None
    fake_entity = SimpleNamespace(
        camera_capabilities=SimpleNamespace(
            two_way_audio=True,
            frontend_stream_types={StreamType.WEB_RTC},
        )
    )
    monkeypatch.setattr(vdm10._alexa_entities, "async_get_camera_entity", lambda hass, eid: fake_entity)
    config = SimpleNamespace(
        entity_config={},
        should_expose=lambda entity_id: True,
        generate_alexa_id=lambda entity_id: entity_id,
    )
    adapter = ENTITY_ADAPTERS["camera"](hass, config, cam_state)  # ty: ignore[invalid-argument-type]
    rtc = [iface for iface in adapter.interfaces() if iface.name() == "Alexa.RTCSessionController"]
    assert len(rtc) == 1
    assert rtc[0].configuration()["isFullDuplexAudioSupported"] is True


async def test_foreign_camera_untouched(hass, runtime):
    """a camera that is not ours must keep its plain CAMERA shape"""
    from types import SimpleNamespace

    from homeassistant.components.alexa.entities import ENTITY_ADAPTERS
    from homeassistant.core import State

    fake = State("camera.someone_elses", "idle", {"supported_features": 0})
    config = SimpleNamespace(
        entity_config={},
        should_expose=lambda entity_id: True,
        generate_alexa_id=lambda entity_id: entity_id,
    )
    adapter = ENTITY_ADAPTERS["camera"](hass, config, fake)  # ty: ignore[invalid-argument-type]
    assert adapter.display_categories() == ["CAMERA"]
    names = {iface.name() for iface in adapter.interfaces()}
    assert "Alexa.DoorbellEventSource" not in names


async def test_doorbell_press_redirects_to_camera_endpoint(hass, runtime, monkeypatch):
    """the sender must hand the camera adapter, not the event adapter"""
    from types import SimpleNamespace

    from homeassistant.components.alexa.entities import ENTITY_ADAPTERS

    import custom_components.metzler_vdm10 as vdm10

    sent = []

    async def _spy(hass, config, alexa_entity):
        sent.append(alexa_entity.entity_id)

    monkeypatch.setattr(vdm10, "_orig_send_doorbell", _spy)
    config = SimpleNamespace(entity_config={})
    event_adapter = ENTITY_ADAPTERS["event"](hass, config, _state(hass))
    await vdm10._send_doorbell(hass, config, event_adapter)
    assert sent == ["camera.vdm10_test_door_camera"]


async def test_direct_press_when_event_not_exposed(hass, runtime, monkeypatch):
    """unexposed trigger: we send DoorbellPress ourselves to the camera -
    the one-endpoint setup (Scrypted-style) where the event entity does
    not appear in Alexa at all"""
    from types import SimpleNamespace

    import custom_components.metzler_vdm10 as vdm10

    sent = []

    async def _spy(hass, config, alexa_entity):
        sent.append(alexa_entity.entity_id)

    config = SimpleNamespace(
        should_expose=lambda entity_id: False,
        entity_config={},
        generate_alexa_id=lambda entity_id: entity_id,
    )
    monkeypatch.setattr(vdm10, "_alexa_smart_home_config", lambda hass: config)
    monkeypatch.setattr(vdm10, "_orig_send_doorbell", _spy)
    await vdm10._async_fire_doorbell_press(hass, _ENTITY)
    assert sent == ["camera.vdm10_test_door_camera"]


async def test_no_direct_press_when_event_exposed(hass, runtime, monkeypatch):
    """exposed trigger: HA's listener path delivers it, we stay out - no
    duplicate press"""
    from types import SimpleNamespace

    import custom_components.metzler_vdm10 as vdm10

    sent = []

    async def _spy(hass, config, alexa_entity):
        sent.append(alexa_entity.entity_id)

    config = SimpleNamespace(should_expose=lambda entity_id: True, entity_config={})
    monkeypatch.setattr(vdm10, "_alexa_smart_home_config", lambda hass: config)
    monkeypatch.setattr(vdm10, "_orig_send_doorbell", _spy)
    await vdm10._async_fire_doorbell_press(hass, _ENTITY)
    assert sent == []


async def test_simulate_service_exercises_full_chain(hass, runtime, monkeypatch):
    """the debug service must produce a press on the event entity AND run the
    direct Alexa send - the no-real-ring verification path"""
    from types import SimpleNamespace

    import custom_components.metzler_vdm10 as vdm10

    runtime.station.station.registered.set()
    runtime._poll_registration(None)
    sent = []

    async def _spy(hass, config, alexa_entity):
        sent.append(alexa_entity.entity_id)

    config = SimpleNamespace(
        should_expose=lambda entity_id: False,
        entity_config={},
        generate_alexa_id=lambda entity_id: entity_id,
    )
    monkeypatch.setattr(vdm10, "_alexa_smart_home_config", lambda hass: config)
    monkeypatch.setattr(vdm10, "_orig_send_doorbell", _spy)
    await hass.services.async_call(vdm10.DOMAIN, "simulate_doorbell_press", {}, blocking=True)
    await hass.async_block_till_done()
    assert _state(hass).attributes["event_type"] == "press"
    assert sent == ["camera.vdm10_test_door_camera"]


async def test_alexa_config_captured_on_construction(hass, runtime):
    """the __init__ patch must retain the YAML AlexaConfig for the direct
    doorbell path - on prod the SmartHomeView wraps it in a route closure"""
    from homeassistant.components.alexa.smart_home import AlexaConfig

    import custom_components.metzler_vdm10 as vdm10

    vdm10._ALEXA_CONFIGS.pop(hass, None)
    cfg = AlexaConfig(hass, {"endpoint": "http://localhost", "filter": {}})
    assert vdm10._ALEXA_CONFIGS.get(hass) is cfg
    assert vdm10._alexa_smart_home_config(hass) is cfg
