"""Answer/hangup buttons + services (US3)."""

from conftest import fire_ring
from custom_components.vdm10.const import DOMAIN, SERVICE_ANSWER_CALL, SERVICE_HANGUP_CALL


async def test_buttons_unavailable_without_a_call(hass, runtime):
    assert hass.states.get("button.vdm10_test_answer_call").state == "unavailable"
    assert hass.states.get("button.vdm10_test_hang_up").state == "unavailable"


async def test_answer_button_only_available_while_ringing(hass, runtime):
    await fire_ring(runtime)
    assert hass.states.get("button.vdm10_test_answer_call").state != "unavailable"
    assert hass.states.get("button.vdm10_test_hang_up").state != "unavailable"


async def test_answer_button_press_establishes_call(hass, runtime):
    await fire_ring(runtime)
    await hass.services.async_call("button", "press", {"entity_id": "button.vdm10_test_answer_call"}, blocking=True)
    await hass.async_block_till_done()
    call = runtime.station.station.calls["doorcall1"]
    assert call.state == "established"
    # pressed again outside the window -> no-op, no exception
    await hass.services.async_call("button", "press", {"entity_id": "button.vdm10_test_answer_call"}, blocking=True)
    assert hass.states.get("button.vdm10_test_answer_call").state == "unavailable"


async def test_hangup_button_press_ends_call(hass, runtime):
    await fire_ring(runtime)
    await hass.services.async_call("button", "press", {"entity_id": "button.vdm10_test_hang_up"}, blocking=True)
    await hass.async_block_till_done()
    assert "doorcall1" not in runtime.station.station.calls


async def test_answer_service(hass, runtime):
    await fire_ring(runtime)
    await hass.services.async_call(DOMAIN, SERVICE_ANSWER_CALL, {}, blocking=True)
    assert runtime.station.station.calls["doorcall1"].state == "established"


async def test_hangup_service(hass, runtime):
    await fire_ring(runtime)
    await hass.services.async_call(DOMAIN, SERVICE_HANGUP_CALL, {}, blocking=True)
    assert "doorcall1" not in runtime.station.station.calls


async def test_services_are_safe_noops_without_calls(hass, runtime):
    await hass.services.async_call(DOMAIN, SERVICE_ANSWER_CALL, {}, blocking=True)
    await hass.services.async_call(DOMAIN, SERVICE_HANGUP_CALL, {}, blocking=True)


async def test_mic_feed_reaches_the_rtsp_queue(hass, runtime):
    """talk-path plumbing: frames pushed by HA surface in the library's mic feed"""
    await fire_ring(runtime)
    await runtime.async_answer_call()
    call = runtime.station.station.calls["doorcall1"]
    runtime.feed_mic(b"\x01" * 160)
    assert call.local.mic_frame() == b"\x01" * 160
    assert call.local.mic_frame() == b"\xff" * 160  # drains to silence again
