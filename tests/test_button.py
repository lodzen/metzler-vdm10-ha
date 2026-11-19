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


async def test_reboot_door_button_puts_isapi_reboot(hass, runtime):
    from unittest.mock import MagicMock, patch

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _Session:
        put = None

        def __init__(self, *a, **kw):
            self.put = MagicMock(return_value=_Resp())
            _Session.put = self.put

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    entity = hass.states.get("button.vdm10_test_reboot_door_station")
    assert entity is not None and entity.state != "unavailable"
    with patch("aiohttp.ClientSession", _Session):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.vdm10_test_reboot_door_station"}, blocking=True
        )
    assert _Session.put is not None
    assert _Session.put.call_args.args[0] == "http://127.0.0.1:80/ISAPI/System/reboot"


async def test_unlock_door_button_calls_isapi_open(hass, runtime):
    from unittest.mock import MagicMock, patch

    opener = MagicMock(return_value=True)
    entity = hass.states.get("button.vdm10_test_unlock_door")
    assert entity is not None and entity.state != "unavailable"
    with patch("metzler.station.door_unlock.isapi_open_door", opener):
        await hass.services.async_call("button", "press", {"entity_id": "button.vdm10_test_unlock_door"}, blocking=True)
    assert opener.call_args.args[:4] == ("127.0.0.1", "admin", "doorpw", 1)


async def test_mic_feed_reaches_the_rtsp_queue(hass, runtime):
    """talk-path plumbing: frames pushed by HA surface in the library's mic feed"""
    await fire_ring(runtime)
    await runtime.async_answer_call()
    call = runtime.station.station.calls["doorcall1"]
    runtime.feed_mic(b"\x01" * 160)
    assert call.local.mic_frame() == b"\x01" * 160
    assert call.local.mic_frame() == b"\xff" * 160  # drains to silence again
