"""go2rtc stream registration: producers, isapi gating, preload."""

import types

import pytest

from conftest import ENTRY_DATA
from custom_components.vdm10.const import CONF_PRELOAD_STREAM


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


def _isapi_probe_stub(monkeypatch, g, http):
    """swap aiohttp.ClientSession for a canned-XML fake - keeps the isapi
    channel probe off the network in tests."""
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kw: http)


class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def text(self):
        return self._body


class _FakeHttp:
    """aiohttp.ClientSession stand-in: canned GET body, records PUTs"""

    def __init__(self, xml, status=200):
        self._xml = xml
        self._status = status
        self.puts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def get(self, url, **kw):
        return _FakeResp(self._xml)

    def put(self, url, **kw):
        self.puts.append((url, kw.get("data")))
        return _FakeResp("<ok/>", self._status)


@pytest.mark.parametrize("mock_entry", [{CONF_PRELOAD_STREAM: True}], indirect=True)
async def test_registers_producers_and_preload(hass, runtime, monkeypatch):
    api = _FakeApi(schemes={"rtsp", "ffmpeg", "isapi"})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    import custom_components.vdm10.go2rtc as g

    _isapi_probe_stub(monkeypatch, g, _FakeHttp("<id>1</id><enabled>true</enabled>"))
    await g.async_configure(hass, runtime)

    name, sources = api.added[0]
    assert name == "vdm10_vdm10_Q12345678_door_camera"
    rtsp = f"rtsp://{ENTRY_DATA['door_user']}:{ENTRY_DATA['door_password']}@{ENTRY_DATA['door_host']}:554/ch1/main/av_stream"
    assert sources == [
        rtsp,
        f"ffmpeg:{name}#audio=opus#query=log_level=debug",
        f"isapi://{ENTRY_DATA['door_user']}:{ENTRY_DATA['door_password']}@{ENTRY_DATA['door_host']}/",
    ]
    # preload goes through the camera prefs (survives provider rebinds)
    from homeassistant.components.camera.prefs import get_dynamic_camera_stream_settings

    prefs = await get_dynamic_camera_stream_settings(hass, "camera.vdm10_test_door_camera")
    assert prefs.preload_stream is True
    assert api.preloaded == [name]  # direct enable for this boot


async def test_preload_off_by_default(hass, runtime, monkeypatch):
    """preload is opt-in: without the option a leftover preload is disabled"""
    name = "vdm10_vdm10_Q12345678_door_camera"
    api = _FakeApi(schemes={"rtsp", "ffmpeg"}, preloads={name})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    from custom_components.vdm10.go2rtc import async_configure

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
    import custom_components.vdm10.go2rtc as g

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
    import custom_components.vdm10.go2rtc as g

    _isapi_probe_stub(monkeypatch, g, _FakeHttp("<id>1</id><enabled>true</enabled>"))
    monkeypatch.setattr(g, "_ISAPI_RECHECK_INTERVAL", 0)
    await g.async_configure(hass, runtime)
    await hass.async_block_till_done()
    name, sources = api.added[-1]
    assert name == "vdm10_vdm10_Q12345678_door_camera"
    assert any(s.startswith("isapi://") for s in sources)


async def test_streams_add_wrapper_reappends_isapi(hass, runtime, monkeypatch):
    """go2rtc masks producer credentials ('***'), so HA's lazy
    _update_stream_source rewrite always fires for credentialed cameras and
    drops the isapi backchannel - the Streams.add wrapper re-appends it on
    every rewrite of our stream and leaves other streams untouched."""
    api = _FakeApi(schemes={"rtsp", "ffmpeg", "isapi"})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    import custom_components.vdm10.go2rtc as g

    _isapi_probe_stub(monkeypatch, g, _FakeHttp("<id>1</id><enabled>true</enabled>"))
    await g.async_configure(hass, runtime)
    name = "vdm10_vdm10_Q12345678_door_camera"
    isapi = g._ISAPI_STREAMS[name]

    captured = []

    class _Base:
        async def request(self, method, path, params=None):
            captured.append(params["src"])

    import go2rtc_client.rest as _rest

    client = _rest._StreamClient(_Base())
    await client.add(name, ["rtsp://u:p@door/ch", f"ffmpeg:{name}#audio=opus"])
    assert captured[-1] == ["rtsp://u:p@door/ch", f"ffmpeg:{name}#audio=opus", isapi]
    await client.add("other_stream", ["rtsp://x"])
    assert captured[-1] == ["rtsp://x"]

    g.forget_stream(runtime)
    await client.add(name, ["rtsp://u:p@door/ch"])
    assert captured[-1] == ["rtsp://u:p@door/ch"]


async def test_no_go2rtc_is_a_noop(hass, runtime, monkeypatch):
    hass.data.pop("go2rtc", None)
    import custom_components.vdm10.go2rtc as g

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
    import custom_components.vdm10.go2rtc as g

    scheduled = []
    monkeypatch.setattr(g, "_schedule_isapi_retry", lambda *a: scheduled.append(a))
    monkeypatch.setattr(g, "_SCHEMES_RETRY_ATTEMPTS", 1)
    await g.async_configure(hass, runtime)  # must not raise
    assert len(scheduled) == 1


_CHANNELS_OFF = """<TwoWayAudioChannelList><TwoWayAudioChannel>
<id>1</id><enabled>false</enabled>
<audioCompressionType>G.711ulaw</audioCompressionType>
</TwoWayAudioChannel></TwoWayAudioChannelList>"""

_CHANNELS_ON = _CHANNELS_OFF.replace("false", "true")


async def test_isapi_channel_disabled_gets_enabled(hass, runtime, monkeypatch):
    """a door that ships with <enabled>false</enabled> accepts open/audioData
    but stays silent - the probe enables the channel via PUT at configure."""
    api = _FakeApi(schemes={"rtsp", "ffmpeg", "isapi"})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    import custom_components.vdm10.go2rtc as g

    http = _FakeHttp(_CHANNELS_OFF)
    _isapi_probe_stub(monkeypatch, g, http)
    await g.async_configure(hass, runtime)
    assert len(http.puts) == 1
    url, data = http.puts[0]
    assert url.endswith("/TwoWayAudio/channels/1")
    assert "<enabled>true</enabled>" in data
    assert "G.711ulaw" in data


async def test_isapi_channel_enabled_is_left_alone(hass, runtime, monkeypatch):
    api = _FakeApi(schemes={"rtsp", "ffmpeg", "isapi"})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    import custom_components.vdm10.go2rtc as g

    http = _FakeHttp(_CHANNELS_ON)
    _isapi_probe_stub(monkeypatch, g, http)
    await g.async_configure(hass, runtime)
    assert http.puts == []


async def test_isapi_probe_failure_is_swallowed(hass, runtime, monkeypatch):
    """door unreachable at configure time: the stream setup must still land,
    the probe just logs and moves on."""

    class _Boom:
        def get(self, url, **kw):
            raise RuntimeError("door down")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    api = _FakeApi(schemes={"rtsp", "ffmpeg", "isapi"})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    import custom_components.vdm10.go2rtc as g

    _isapi_probe_stub(monkeypatch, g, _Boom())
    await g.async_configure(hass, runtime)  # must not raise
    _name, sources = api.added[0]
    assert any(s.startswith("isapi://") for s in sources)
