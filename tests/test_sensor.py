"""Diagnostic status sensor (T038)."""

import threading
import types

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


async def test_status_omits_provider_attribute_without_trunk(hass, runtime):
    """ENTRY_DATA has no sip_host - the attribute stays away entirely"""
    state = hass.states.get("sensor.vdm10_test_intercom_status")
    assert "provider_registered" not in state.attributes
    assert "fritz_registered" not in state.attributes


async def test_status_exposes_provider_registration(hass, runtime, monkeypatch):
    """a configured provider leg surfaces as provider_registered attribute -
    the poller diffs all three legs so a trunk flip alone triggers an update"""
    st = runtime.station.station
    # ENTRY_DATA has no sip_host -> st.provider is None; inject a trunk-like stub
    # (unregister() because shutdown calls it when needs_register() is true)
    monkeypatch.setattr(
        st,
        "provider",
        types.SimpleNamespace(
            needs_register=lambda: True,
            registered=threading.Event(),
            unregister=lambda: None,
        ),
    )
    runtime._poll_registration(None)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.vdm10_test_intercom_status")
    assert state.attributes["provider_registered"] is False
    st.provider.registered.set()  # what the register_loop does on a 200
    runtime._poll_registration(None)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.vdm10_test_intercom_status")
    assert state.attributes["provider_registered"] is True
