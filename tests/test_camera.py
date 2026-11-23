"""Lazy door camera (US2): no pull while idle, URL on demand only."""

from unittest.mock import patch

from homeassistant.components.camera import CameraEntityFeature

from custom_components.metzler_vdm10.const import DOOR_STREAM_PATH


def _camera(hass):
    return hass.data["entity_components"]["camera"].get_entity("camera.vdm10_test_door_camera")


async def test_camera_exists_with_stream_feature(hass, runtime):
    state = hass.states.get("camera.vdm10_test_door_camera")
    assert state is not None
    cam = _camera(hass)
    assert cam.supported_features & CameraEntityFeature.STREAM


async def test_idle_setup_never_touches_the_door(hass, runtime):
    """laziness proof: nothing called stream_source during setup+idle, and the
    entity holds no connection/puller of its own"""
    cam = _camera(hass)
    with patch.object(cam, "stream_source", wraps=cam.stream_source) as spy:
        await hass.async_block_till_done()
        spy.assert_not_called()
    for attr in ("_stream", "_puller", "_rtsp", "_session", "_reader"):
        assert not hasattr(cam, attr), f"camera unexpectedly owns {attr}"
    assert cam._http is None  # session opens on the first still, not at setup


async def test_stream_source_url_on_demand(hass, runtime):
    cam = _camera(hass)
    url = await cam.stream_source()
    assert url == f"rtsp://admin:doorpw@127.0.0.1:554{DOOR_STREAM_PATH}"


async def test_stream_source_does_not_answer_a_ring(hass, runtime):
    """opening the camera must leave a ringing call ringing (FR: no implicit answer)"""
    from conftest import fire_ring

    await fire_ring(runtime)
    url = await _camera(hass).stream_source()
    await hass.async_block_till_done()
    assert url is not None
    call = runtime.station.station.calls["doorcall1"]
    assert call.state == "early"  # still parked - never established by viewing
    assert runtime.ringing_call is not None


async def test_stills_come_from_the_stream(hass, runtime):
    """the door's ISAPI /picture route hangs post-auth on current firmware -
    HA must fetch stills via the stream path (go2rtc frame.jpeg on demand)"""
    assert _camera(hass).use_stream_for_stills is True


async def test_snapshot_answers_the_digest_challenge(hass, runtime):
    """Hikvision ISAPI answers Basic with 401 - the snapshot must complete the
    Digest challenge and return the JPEG bytes"""
    from aiohttp import web

    seen = []

    async def picture(request):
        auth = request.headers.get("Authorization", "")
        if "Digest username=" not in auth:
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Digest realm="isapi",nonce="abc123",qop=auth'})
        seen.append(auth)
        return web.Response(body=b"\xff\xd8jpeg-bytes", content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/ISAPI/Streaming/channels/101/picture", picture)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = runner.addresses[0][1]
        with patch("custom_components.metzler_vdm10.camera.DOOR_ISAPI_PORT", port):
            image = await _camera(hass).async_camera_image()
        assert image == b"\xff\xd8jpeg-bytes"
        assert seen and 'username="admin"' in seen[0]
    finally:
        await runner.cleanup()
