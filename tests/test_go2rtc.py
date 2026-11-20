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


@pytest.mark.parametrize("mock_entry", [{CONF_PRELOAD_STREAM: True}], indirect=True)
async def test_registers_producers_and_preload(hass, runtime, monkeypatch):
    api = _FakeApi(schemes={"rtsp", "ffmpeg", "isapi"})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    from custom_components.vdm10.go2rtc import async_configure

    await async_configure(hass, runtime)
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
    """the embedded go2rtc does not load the isapi module - no isapi producer"""
    api = _FakeApi(schemes={"rtsp", "ffmpeg"})
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    from custom_components.vdm10.go2rtc import async_configure

    await async_configure(hass, runtime)
    assert all(not s.startswith("isapi") for _, sources in api.added for s in sources)


async def test_no_go2rtc_is_a_noop(hass, runtime, monkeypatch):
    hass.data.pop("go2rtc", None)
    import custom_components.vdm10.go2rtc as g

    monkeypatch.setattr(g, "_WAIT_SECONDS", 0)
    await g.async_configure(hass, runtime)  # _wait_for_go2rtc returns None -> early return


async def test_api_failure_is_swallowed(hass, runtime, monkeypatch):
    class _Boom:
        async def list(self):
            raise RuntimeError("api down")

    api = types.SimpleNamespace(schemes=_Boom())
    _rest_client(monkeypatch, api)
    hass.data["go2rtc"] = types.SimpleNamespace(url="http://localhost:11984", session=None)
    from custom_components.vdm10.go2rtc import async_configure

    await async_configure(hass, runtime)  # must not raise
