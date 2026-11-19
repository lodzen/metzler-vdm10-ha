"""Doorbell ring sensor + vdm10_* bus events (US1)."""

import pytest
from pytest_homeassistant.common import MockConfigEntry

from conftest import ENTRY_DATA, fire_ring
from custom_components.vdm10.const import CONF_SIP_HOST, EVENT_ANSWERED, EVENT_CALL_ENDED, EVENT_RING


@pytest.fixture
async def provider_runtime(hass, socket_enabled):
    """entry with a provider trunk configured -> the provider registration
    entity exists (pointing at a dead localhost port, so 'off')"""
    entry = MockConfigEntry(
        domain="vdm10",
        data={**ENTRY_DATA, CONF_SIP_HOST: "127.0.0.1"},
        title="VDM10 test",
        unique_id="Q12345678@127.0.0.1",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry.runtime_data
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


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


async def test_provider_win_reports_answered_by_other(hass, runtime):
    """ha_mode + trunk race: the provider leg 200-OKs first -> the library
    fires 'answered' without async_answer_call ever running -> by=other"""
    answered = []
    hass.bus.async_listen(EVENT_ANSWERED, answered.append)
    await fire_ring(runtime)
    call = runtime.station.station.calls["doorcall1"]
    runtime.station.station._fire_event("answered", call)  # provider_answered path
    await hass.async_block_till_done()
    assert answered[0].data == {"call_id": "doorcall1", "by": "other"}
    # answering after the other side won is a no-op on the HA side
    assert not await runtime.async_answer_call()


async def test_hangup_declines_and_fires_ended(hass, runtime):
    ended = []
    hass.bus.async_listen(EVENT_CALL_ENDED, ended.append)
    await fire_ring(runtime)
    assert await runtime.async_hangup_call()
    await hass.async_block_till_done()
    assert "doorcall1" not in runtime.station.station.calls
    assert ended[0].data["reason"] == "declined"


# ------------------------------------------------------- registration sensors


async def test_intercom_registration_tracks_the_registrar(hass, runtime):
    entity = hass.states.get("binary_sensor.vdm10_test_intercom_registration")
    assert entity is not None and entity.state == "off"  # dead registrar port
    runtime.station.station.registered.set()
    runtime._poll_registration(None)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.vdm10_test_intercom_registration").state == "on"


async def test_intercom_registration_exposes_diagnostics(hass, runtime):
    """last_error + active_calls ride on the intercom leg (the removed status
    sensor used to carry them); trunk legs get no attributes"""
    entity = hass.states.get("binary_sensor.vdm10_test_intercom_registration")
    assert entity.attributes["last_error"] is None
    assert entity.attributes["active_calls"] == 0
    await fire_ring(runtime)
    runtime._poll_registration(None)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.vdm10_test_intercom_registration").attributes["active_calls"] == 1


async def test_trunk_entities_only_exist_when_configured(hass, runtime):
    # ENTRY_DATA configures no provider/fritz -> those legs get no entity
    assert hass.states.get("binary_sensor.vdm10_test_provider_registration") is None
    assert hass.states.get("binary_sensor.vdm10_test_fritz_box_registration") is None


async def test_provider_registration_entity(hass, provider_runtime):
    entity = hass.states.get("binary_sensor.vdm10_test_provider_registration")
    assert entity is not None
    assert entity.state == "off"  # trunk configured, dead port -> not registered
    assert "active_calls" not in entity.attributes  # diagnostics live on the intercom leg


async def test_fritz_registration_entity_from_options_only(hass, socket_enabled):
    """fritz set via the options flow lands in entry.options - the entity must
    still appear (regression: the gate used to read entry.data only)."""
    from custom_components.vdm10.const import CONF_FRITZ_HOST, CONF_FRITZ_PASS, CONF_FRITZ_USER

    entry = MockConfigEntry(
        domain="vdm10",
        data=ENTRY_DATA,
        options={CONF_FRITZ_HOST: "127.0.0.1", CONF_FRITZ_USER: "u", CONF_FRITZ_PASS: "p"},
        title="VDM10 test",
        unique_id="Q12345678@127.0.0.1",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity = hass.states.get("binary_sensor.vdm10_test_fritz_box_registration")
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entity is not None
    assert entity.state == "off"
