"""Doorbell ring sensor + vdm10_* bus events (US1)."""

from conftest import fire_ring
from custom_components.vdm10.const import EVENT_ANSWERED, EVENT_CALL_ENDED, EVENT_RING


def _sensor(hass):
    return hass.states.get("binary_sensor.vdm10_test_doorbell_ringing")


async def test_sensor_off_and_unavailable_until_registered(hass, runtime):
    state = _sensor(hass)
    assert state is not None
    # registrar is a dead port -> never registered -> entity unavailable
    assert state.state == "unavailable"


async def test_ring_turns_sensor_on_and_fires_event(hass, runtime):
    events = []
    hass.bus.async_listen(EVENT_RING, events.append)
    await fire_ring(runtime)
    assert runtime.ringing_call is not None
    # attributes only reach state.attributes while the entity is available -
    # simulate a completed registration so the sensor reports 'on' + attrs
    runtime.station.station.registered.set()
    runtime._poll_registration(None)
    await hass.async_block_till_done()
    state = _sensor(hass)
    assert state.state == "on"
    assert state.attributes["caller"] == "10010100000"
    assert state.attributes["call_id"] == "doorcall1"
    assert len(events) == 1 and events[0].data["call_id"] == "doorcall1"
    assert events[0].data["door"] is True


async def test_cancel_ends_ring_best_effort_answered_elsewhere(hass, runtime):
    ended = []
    hass.bus.async_listen(EVENT_CALL_ENDED, ended.append)
    await fire_ring(runtime)
    call = runtime.station.station.calls["doorcall1"]
    # registrar-style forked-leg CANCEL is indistinguishable from answer-elsewhere
    call.end_reason = "cancel"
    runtime.station.station._close_call(call, "doorcall1")
    await hass.async_block_till_done()
    assert runtime.ringing_call is None
    assert len(ended) == 1 and ended[0].data["reason"] == "answered_elsewhere"


async def test_answer_fires_answered_event_and_clears_ring(hass, runtime):
    answered, ended = [], []
    hass.bus.async_listen(EVENT_ANSWERED, answered.append)
    hass.bus.async_listen(EVENT_CALL_ENDED, ended.append)
    await fire_ring(runtime)
    assert await runtime.async_answer_call()
    await hass.async_block_till_done()
    assert runtime.ringing_call is None  # established != ringing
    assert runtime.active_call is not None
    assert answered[0].data == {"call_id": "doorcall1", "by": "ha"}
    # mic stays muted by default: the intercom media feeds silence
    call = runtime.station.station.calls["doorcall1"]
    assert call.local.mic_frame() == b"\xff" * 160


async def test_hangup_declines_and_fires_ended(hass, runtime):
    ended = []
    hass.bus.async_listen(EVENT_CALL_ENDED, ended.append)
    await fire_ring(runtime)
    assert await runtime.async_hangup_call()
    await hass.async_block_till_done()
    assert "doorcall1" not in runtime.station.station.calls
    assert ended[0].data["reason"] == "declined"
