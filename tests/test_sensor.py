"""Diagnostic status sensor (T038)."""

from conftest import fire_ring


async def test_status_connecting_while_unregistered(hass, runtime):
    state = hass.states.get("sensor.vdm10_test_intercom_status")
    assert state is not None
    assert state.state == "connecting"  # station up, registrar dead
    assert state.attributes["last_error"] is None
    assert state.attributes["active_calls"] == 0


async def test_status_counts_active_calls(hass, runtime):
    await fire_ring(runtime)
    state = hass.states.get("sensor.vdm10_test_intercom_status")
    # active_calls updates on the next dispatcher signal; force one
    runtime._poll_registration(None)
    state = hass.states.get("sensor.vdm10_test_intercom_status")
    assert state.attributes["active_calls"] == 1


async def test_status_unavailable_without_station(hass, runtime):
    await runtime.async_stop()
    state = hass.states.get("sensor.vdm10_test_intercom_status")
    runtime._poll_registration(None)
    state = hass.states.get("sensor.vdm10_test_intercom_status")
    assert state.state == "unavailable"
