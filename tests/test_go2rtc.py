"""go2rtc stream registration: producers, isapi gating, preload."""

import types

import pytest

from conftest import ENTRY_DATA
from custom_components.metzler_vdm10.const import CONF_PRELOAD_STREAM


class _FakeApi:
    def __init__(self, schemes=(), streams=None, preloads=()):
        self._schemes = schemes
        self.schemes = types.SimpleNamespace(list=self.list)
        self.streams = types.SimpleNamespace(list=self._list_streams, add=self._add)
        self.preload = types.SimpleNamespace(list=self._list_preload, enable=self._enable, disable=self._disable)
        self.added = []
        self.preloaded = []
        self.disabled = []
        self._streams = streams or {}
        self._preloads = set(preloads)

    async def list(self):
        return set(self._schemes)

    async def _list_streams(self):
        return self._streams

    async def _add(self, name, sources):
        self.added.append((name, list(sources)))
        self._streams[name] = types.SimpleNamespace(producers=[types.SimpleNamespace(url=u) for u in sources])

    async def _list_preload(self):
        return dict.fromkeys(self._preloads)

    async def _enable(self, name):
        self.preloaded.append(name)
        self._preloads.add(name)

    async def _disable(self, name):
        self.disabled.append(name)
        self._preloads.discard(name)


def _rest_client(monkeypatch, api):
    import go2rtc_client.rest

    monkeypatch.setattr(go2rtc_client.rest, "Go2RtcRestClient", lambda s, u: api)


@pytest.mark.parametrize("mock_entry", [{CONF_PRELOAD_STREAM: True}], indirect=True)
async def test_registers_producers_and_preload(hass, runtime, monkeypatch):
    api = _FakeApi(schemes={"rtsp", "ffmpeg", "isapi"})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    import custom_components.metzler_vdm10.go2rtc as g

    await g.async_configure(hass, runtime)

    name, sources = api.added[0]
    assert name == "metzler_vdm10_Q12345678_door_camera"
    rtsp = f"rtsp://{ENTRY_DATA['door_user']}:{ENTRY_DATA['door_password']}@{ENTRY_DATA['door_host']}:554/ch1/main/av_stream"
    # ffmpeg-opus first: go2rtc picks the first producer able to serve a
    # consumer media - this is what makes WebRTC clients negotiate opus.
    # The mirror carries the volume boost (the door's mic feed is ~20 dB
    # under the intercom path on the RTSP track).
    assert sources == [
        f"ffmpeg:{name}#audio=opus#query=log_level=debug#raw=-af#raw=volume=8",
        rtsp,
        f"isapi://{ENTRY_DATA['door_user']}:{ENTRY_DATA['door_password']}@{ENTRY_DATA['door_host']}/",
    ]
    # preload goes through the camera prefs (survives provider rebinds)
    from homeassistant.components.camera.prefs import get_dynamic_camera_stream_settings

    prefs = await get_dynamic_camera_stream_settings(hass, "camera.vdm10_test_door_camera")
    assert prefs.preload_stream is True
    assert api.preloaded == [name]  # direct enable for this boot


async def test_preload_off_by_default(hass, runtime, monkeypatch):
    """preload is opt-in: without the option a leftover preload is disabled"""
    name = "metzler_vdm10_Q12345678_door_camera"
    api = _FakeApi(schemes={"rtsp", "ffmpeg"}, preloads={name})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    from custom_components.metzler_vdm10.go2rtc import async_configure

    await async_configure(hass, runtime)
    assert api.preloaded == []
    assert api.disabled == [name]
    from homeassistant.components.camera.prefs import get_dynamic_camera_stream_settings

    prefs = await get_dynamic_camera_stream_settings(hass, "camera.vdm10_test_door_camera")
    assert prefs.preload_stream is False


async def test_skips_isapi_when_scheme_missing(hass, runtime, monkeypatch):
    """the embedded go2rtc does not load the isapi module - no isapi producer,
    but a background recheck is armed in case a respawn lands it late"""
    api = _FakeApi(schemes={"rtsp", "ffmpeg"})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    import custom_components.metzler_vdm10.go2rtc as g

    scheduled = []
    monkeypatch.setattr(g, "_schedule_isapi_retry", lambda *a: scheduled.append(a))
    await g.async_configure(hass, runtime)
    assert all(not s.startswith("isapi") for _, sources in api.added for s in sources)
    assert len(scheduled) == 1


async def test_isapi_recheck_rewrites_stream_when_module_lands(hass, runtime, monkeypatch):
    """isapi absent at configure time but present a few seconds later (the
    watchdog respawn finishing) - the recheck rewrites the stream with the
    talkback producer."""
    calls = {"n": 0}
    api = _FakeApi(schemes={"rtsp", "ffmpeg"})
    real_list = api.list

    async def late_isapi():
        calls["n"] += 1
        if calls["n"] >= 2:
            return {"rtsp", "ffmpeg", "isapi"}
        return await real_list()

    api.schemes = types.SimpleNamespace(list=late_isapi)
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    import custom_components.metzler_vdm10.go2rtc as g

    monkeypatch.setattr(g, "_ISAPI_RECHECK_INTERVAL", 0)
    await g.async_configure(hass, runtime)
    await hass.async_block_till_done()
    name, sources = api.added[-1]
    assert name == "metzler_vdm10_Q12345678_door_camera"
    assert any(s.startswith("isapi://") for s in sources)


async def test_streams_add_wrapper_reappends_isapi(hass, runtime, monkeypatch):
    """go2rtc masks producer credentials ('***'), so HA's lazy
    _update_stream_source rewrite always fires for credentialed cameras and
    rewrites [rtsp, ffmpeg] - the Streams.add wrapper restores our canonical
    [ffmpeg+boost, rtsp, isapi] order on every rewrite of our stream (rtsp
    first would let pcm_mulaw win over opus, and HA's rewrite drops the
    volume boost) and leaves other streams untouched."""
    api = _FakeApi(schemes={"rtsp", "ffmpeg", "isapi"})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    import custom_components.metzler_vdm10.go2rtc as g

    await g.async_configure(hass, runtime)
    name = "metzler_vdm10_Q12345678_door_camera"
    isapi = g._ISAPI_STREAMS[name]

    captured = []

    class _Base:
        async def request(self, method, path, params=None):
            captured.append(params["src"])

    import go2rtc_client.rest as _rest

    client = _rest._StreamClient(_Base())
    # HA's rewrite passes its plain ffmpeg mirror - the wrapper must
    # substitute ours (volume boost) and keep canonical order
    await client.add(name, ["rtsp://u:p@door/ch", f"ffmpeg:{name}#audio=opus"])
    assert captured[-1] == [
        f"ffmpeg:{name}#audio=opus#query=log_level=debug#raw=-af#raw=volume=8",
        "rtsp://u:p@door/ch",
        isapi,
    ]
    await client.add("other_stream", ["rtsp://x"])
    assert captured[-1] == ["rtsp://x"]

    g.forget_stream(runtime)
    await client.add(name, ["rtsp://u:p@door/ch"])
    assert captured[-1] == ["rtsp://u:p@door/ch"]


async def test_no_go2rtc_is_a_noop(hass, runtime, monkeypatch):
    hass.data.pop("go2rtc", None)
    import custom_components.metzler_vdm10.go2rtc as g

    monkeypatch.setattr(g, "_WAIT_SECONDS", 0)
    await g.async_configure(hass, runtime)  # _wait_for_go2rtc returns None -> early return


async def test_api_failure_is_swallowed(hass, runtime, monkeypatch):
    """go2rtc up but the API not listening yet - the exception is swallowed
    and the background isapi watcher is armed so the backchannel can still
    land once the process is reachable."""

    class _Boom:
        async def list(self):
            raise RuntimeError("api down")

    api = types.SimpleNamespace(schemes=_Boom())
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    import custom_components.metzler_vdm10.go2rtc as g

    scheduled = []
    monkeypatch.setattr(g, "_schedule_isapi_retry", lambda *a: scheduled.append(a))
    monkeypatch.setattr(g, "_SCHEMES_RETRY_ATTEMPTS", 1)
    await g.async_configure(hass, runtime)  # must not raise
    assert len(scheduled) == 1
