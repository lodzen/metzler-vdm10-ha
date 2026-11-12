"""Config flow: menu -> discover/manual, derived values, provider sections."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from conftest import ENTRY_DATA
from custom_components.vdm10.config_flow import _serial_for
from custom_components.vdm10.const import (
    CONF_DOOR_HOST,
    CONF_DOOR_PASSWORD,
    CONF_DOOR_USER,
    CONF_LOCAL_IP,
    CONF_PASSWORD,
    CONF_REGISTRAR_HOST,
    CONF_SERIAL,
    CONF_SIP_HOST,
    CONF_SIP_TARGET,
    CONF_USERNAME,
    DISCOVER_MANUAL,
    DOMAIN,
)

# minimal submit on the manual step - everything else is derived
# (rtsp_port 554 is privileged in this env - override like the old tests did)
MANUAL_MIN = {
    CONF_REGISTRAR_HOST: "127.0.0.1",
    CONF_USERNAME: "10000000003",
    CONF_PASSWORD: "pw",
    CONF_DOOR_HOST: "127.0.0.1",
    "advanced": {"rtsp_port": 8554},
}


@pytest.fixture(autouse=True)
def _no_docker():
    """these tests assume host networking - pin docker_detected off; the docker
    cases patch it back to True locally."""
    with patch("custom_components.vdm10.config_flow._docker", return_value=False):
        yield


DISCOVERY_RESULT = {
    "doors": [
        {
            "DeviceType": "57619",
            "DeviceDescription": "VDM10-VM-POE",
            "DeviceSN": "VDM10-VM-POE0120220607RRK08753130",
            "MAC": "bc-9b-5e-01-a1-b3",
            "IPv4Address": "10.0.0.48",
            "_ip": "10.0.0.48",
        }
    ],
    "registrars": ["10.0.0.38"],
    "candidates": ["10.0.0.47"],
}


async def _go_manual(hass):
    """open the flow and select the manual path on the menu"""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "manual"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"
    return result["flow_id"]


async def test_menu_then_manual_creates_entry(hass, socket_enabled):
    flow_id = await _go_manual(hass)
    # CREATE_ENTRY triggers async_setup_entry - stub out the station
    with patch("custom_components.vdm10.station.HaStation.async_start", new=AsyncMock()):
        result = await hass.config_entries.flow.async_configure(flow_id, MANUAL_MIN)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"].startswith("VDM10")
    data = result["data"]
    # derived values: local_ip via UDP-connect, serial generated, door pw = ext pw
    assert data[CONF_LOCAL_IP] == "127.0.0.1"
    assert data[CONF_SERIAL] == _serial_for("127.0.0.1", "10000000003")
    assert data[CONF_DOOR_USER] == "admin"
    assert data[CONF_DOOR_PASSWORD] == "pw"  # defaults to the extension password


async def test_advanced_overrides_win(hass, socket_enabled):
    flow_id = await _go_manual(hass)
    with patch("custom_components.vdm10.station.HaStation.async_start", new=AsyncMock()):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                **MANUAL_MIN,
                "advanced": {
                    "local_ip": "127.0.0.1",
                    "serial": "OVERRIDE1",
                    "listen_port": 5060,
                    "rtsp_port": 8554,
                    "door_password": "doorsecret",
                },
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SERIAL] == "OVERRIDE1"
    assert result["data"][CONF_DOOR_PASSWORD] == "doorsecret"


async def test_discover_prefills_manual_form(hass, socket_enabled):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with (
        patch(
            "custom_components.vdm10.config_flow._run_discovery",
            return_value=DISCOVERY_RESULT,
        ),
        patch(
            "custom_components.vdm10.config_flow._free_extension",
            return_value="10000000002",
        ),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discover"})
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "discover"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"registrar_pick": "10.0.0.38", "door_pick": "10.0.0.48"},
        )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"
    # prefill lands in the manual schema defaults
    markers = {k.schema: k for k in result["data_schema"].schema}
    assert markers[CONF_REGISTRAR_HOST].default() == "10.0.0.38"
    assert markers[CONF_USERNAME].default() == "10000000002"
    assert markers[CONF_DOOR_HOST].default() == "10.0.0.48"


async def test_discover_manual_choice_shows_empty_form(hass, socket_enabled):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch("custom_components.vdm10.config_flow._run_discovery", return_value=DISCOVERY_RESULT):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discover"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"registrar_pick": DISCOVER_MANUAL, "door_pick": DISCOVER_MANUAL}
        )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"
    markers = {k.schema: k for k in result["data_schema"].schema}
    assert markers[CONF_REGISTRAR_HOST].default() == ""


async def test_discover_nothing_found_still_shows_form(hass, socket_enabled):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with patch(
        "custom_components.vdm10.config_flow._run_discovery",
        return_value={"doors": [], "registrars": [], "candidates": []},
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discover"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "discover"
    assert result["errors"]["base"] == "nothing_found"


async def test_docker_skips_scan_and_asks_local_ip(hass, socket_enabled):
    """container detected -> 'Search the network' bounces to manual with the
    host-IP question; the subnet scan never runs"""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with (
        patch("custom_components.vdm10.config_flow._docker", return_value=True),
        patch("custom_components.vdm10.config_flow._run_discovery") as scan,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discover"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"
    assert result["errors"]["base"] == "docker_detected"
    scan.assert_not_called()
    markers = {k.schema: type(k).__name__ for k in result["data_schema"].schema}
    assert markers.get(CONF_LOCAL_IP) == "Required"


async def test_docker_local_ip_required_then_accepted(hass, socket_enabled):
    with patch("custom_components.vdm10.config_flow._docker", return_value=True):
        flow_id = await _go_manual(hass)
        result = await hass.config_entries.flow.async_configure(flow_id, MANUAL_MIN)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"][CONF_LOCAL_IP] == "required"
        with patch("custom_components.vdm10.station.HaStation.async_start", new=AsyncMock()):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {**MANUAL_MIN, CONF_LOCAL_IP: "10.0.0.123"}
            )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_LOCAL_IP] == "10.0.0.123"


async def test_provider_fields_landed_in_entry(hass, socket_enabled):
    flow_id = await _go_manual(hass)
    with patch("custom_components.vdm10.station.HaStation.async_start", new=AsyncMock()):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                **MANUAL_MIN,
                "provider": {
                    "sip_host": "sip.linphone.org",
                    "sip_port": 5060,
                    "sip_user": "alice",
                    "sip_pass": "secret",
                    "sip_target": "",
                    "sip_domain": "",
                    "sip_srtp": True,
                },
                "fritz": {
                    "fritz_host": "fritz.box",
                    "fritz_port": 5060,
                    "fritz_domain": "fritz.box",
                    "fritz_user": "620",
                    "fritz_pass": "fpw",
                    "fritz_target": "**9",
                },
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data[CONF_SIP_HOST] == "sip.linphone.org"
    assert data["fritz_target"] == "**9"


def test_build_options_maps_provider_and_fritz():
    from custom_components.vdm10.station import build_options

    data = {
        **ENTRY_DATA,
        "listen_port": 5060,
        "rtsp_port": 8554,
        CONF_SIP_HOST: "sip.linphone.org",
        CONF_SIP_TARGET: "sip:6000@sip.linphone.org",
        "fritz_host": "fritz.box",
        "fritz_user": "620",
        "fritz_pass": "fpw",
        "fritz_target": "**9",
    }
    o = build_options(data)
    assert o.sip_host == "sip.linphone.org"
    assert o.fritz and o.fritz_target


def test_build_options_no_provider_stays_ha_only():
    from custom_components.vdm10.station import build_options

    o = build_options({**ENTRY_DATA, "listen_port": 5060, "rtsp_port": 8554})
    assert not o.sip_host and not o.fritz


async def test_duplicate_serial_host_aborts(hass, mock_entry, socket_enabled):
    flow_id = await _go_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {**MANUAL_MIN, "advanced": {"rtsp_port": 8554, "serial": ENTRY_DATA[CONF_SERIAL]}},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
