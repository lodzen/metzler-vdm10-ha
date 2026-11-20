"""Config flow: menu -> discover/manual, derived values, provider sections."""

from typing import Any
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
# (rtsp_port 554 is privileged and listen_port 5060 can be held by a running
# dev instance on this host - the bind check rejects both, so override)
MANUAL_MIN: dict[str, Any] = {
    CONF_REGISTRAR_HOST: "127.0.0.1",
    CONF_USERNAME: "10000000003",
    CONF_PASSWORD: "pw",
    CONF_DOOR_HOST: "127.0.0.1",
    "advanced": {"rtsp_port": 8554, "listen_port": 55060},
}


@pytest.fixture(autouse=True)
def _no_docker():
    """these tests assume host networking - pin docker_detected off; the docker
    cases patch it back to True locally."""
    with patch("custom_components.vdm10.config_flow._docker", return_value=False):
        yield


@pytest.fixture(autouse=True)
def _sip_ok():
    """config-flow tests never run real SIP probes - the registrar/provider
    status checks get a canned success; the status tests override locally."""
    with patch(
        "custom_components.vdm10.config_flow._sip_statuses",
        return_value={"registrar": 200},
    ):
        yield


@pytest.fixture(autouse=True)
def _no_setup():
    """config-flow tests never run real entry setup - CREATE_ENTRY would start
    a Vdm10Runtime (station thread + poll timer) that lingers past teardown.
    A stub runtime keeps async_unload_entry working on the mocked entry."""

    async def _setup(_hass, entry):
        entry.runtime_data = AsyncMock()
        return True

    with patch("custom_components.vdm10.async_setup_entry", side_effect=_setup):
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
                    "listen_port": 55060,
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
        patch(
            "custom_components.vdm10.config_flow._local_ip_for",
            return_value="10.0.0.96",
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
    """container with a NAT'd egress IP -> 'Search the network' bounces to
    manual with the host-IP question; the subnet scan never runs"""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with (
        patch("custom_components.vdm10.config_flow._docker", return_value=True),
        patch("custom_components.vdm10.config_flow._egress_ip", return_value="172.30.32.1"),
        patch("custom_components.vdm10.config_flow._run_discovery") as scan,
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discover"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"
    assert result["errors"]["base"] == "docker_detected"
    scan.assert_not_called()
    markers = {k.schema: type(k).__name__ for k in result["data_schema"].schema}
    assert markers.get(CONF_LOCAL_IP) == "Required"


async def test_docker_host_networked_runs_scan(hass, socket_enabled):
    """container with a real LAN egress IP (host networking, e.g. HAOS) -> the
    subnet scan still runs and local_ip stays auto-derived in Advanced."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with (
        patch("custom_components.vdm10.config_flow._docker", return_value=True),
        patch("custom_components.vdm10.config_flow._egress_ip", return_value="192.168.178.6"),
        patch(
            "custom_components.vdm10.config_flow._run_discovery",
            return_value=DISCOVERY_RESULT,
        ) as scan,
        patch(
            "custom_components.vdm10.config_flow._free_extension",
            return_value="10000000002",
        ),
        patch(
            "custom_components.vdm10.config_flow._local_ip_for",
            return_value="192.168.178.6",
        ),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "discover"})
        assert result["type"] is FlowResultType.FORM and result["step_id"] == "discover"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"registrar_pick": "10.0.0.38", "door_pick": "10.0.0.48"},
        )
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"
    scan.assert_called_once()
    markers = {k.schema: type(k).__name__ for k in result["data_schema"].schema}
    assert markers.get(CONF_LOCAL_IP) != "Required"


async def test_docker_local_ip_required_then_accepted(hass, socket_enabled):
    """docker mode: local_ip required only when auto-derive fails (unroutable
    registrar); an explicit value is then accepted as-is."""
    with (
        patch("custom_components.vdm10.config_flow._docker", return_value=True),
        patch("custom_components.vdm10.config_flow._egress_ip", return_value="172.30.32.1"),
    ):
        flow_id = await _go_manual(hass)
        with patch(
            "custom_components.vdm10.config_flow._local_ip_for",
            side_effect=OSError("no route"),
        ):
            result = await hass.config_entries.flow.async_configure(flow_id, MANUAL_MIN)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"][CONF_LOCAL_IP] == "required"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**MANUAL_MIN, CONF_LOCAL_IP: "127.0.0.1"}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_LOCAL_IP] == "127.0.0.1"


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
        "inbound_target": "10000000001",
    }
    o = build_options(data)
    assert o.sip_host == "sip.linphone.org"
    assert o.fritz and o.fritz_target
    assert o.inbound_target == "10000000001"


def test_build_options_no_provider_stays_ha_only():
    from custom_components.vdm10.station import build_options

    o = build_options({**ENTRY_DATA, "listen_port": 5060, "rtsp_port": 8554})
    assert not o.sip_host and not o.fritz


async def test_options_flow_stores_a_full_flat_entry(hass, mock_entry, socket_enabled):
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(mock_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "registrar_host": "127.0.0.1",
            "username": "10000000003",
            "password": "pw",
            "provider": {"inbound_target": "10000000001"},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_entry.options["inbound_target"] == "10000000001"
    assert mock_entry.options["registrar_host"] == "127.0.0.1"  # _flatten produced a full entry dict


def test_build_options_derives_main_station_as_inbound_default():
    from custom_components.vdm10.station import build_options

    # trunk configured, no inbound_target -> ring the main indoor directly on
    # :5060 (the registrar can't route a call to its own number)
    o = build_options({**ENTRY_DATA, "listen_port": 5060, "rtsp_port": 8554, CONF_SIP_HOST: "pbx.lan"})
    assert o.inbound_target == "10000000000@127.0.0.1"


async def test_duplicate_serial_host_aborts(hass, mock_entry, socket_enabled):
    flow_id = await _go_manual(hass)
    result = await hass.config_entries.flow.async_configure(
        flow_id,
        {
            **MANUAL_MIN,
            "advanced": {**MANUAL_MIN["advanced"], "serial": ENTRY_DATA[CONF_SERIAL]},
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_empty_fields_auto_detect(hass, socket_enabled):
    """username/door_host left empty -> the submit probes the registrar for the
    next free slot and scans the LAN for the outdoor station."""
    flow_id = await _go_manual(hass)
    submit = {**MANUAL_MIN, CONF_USERNAME: "", CONF_DOOR_HOST: ""}
    with (
        patch(
            "custom_components.vdm10.config_flow._free_extension",
            return_value="10000000002",
        ) as probe,
        patch(
            "custom_components.vdm10.config_flow._door_host",
            return_value="10.0.0.48",
        ) as door,
        patch("custom_components.vdm10.station.HaStation.async_start", new=AsyncMock()),
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, submit)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_USERNAME] == "10000000002"
    assert result["data"][CONF_DOOR_HOST] == "10.0.0.48"
    probe.assert_called_once_with("127.0.0.1")
    door.assert_called_once()


async def test_manual_extension_undetectable(hass, socket_enabled):
    flow_id = await _go_manual(hass)
    submit = {**MANUAL_MIN, CONF_USERNAME: ""}
    with patch("custom_components.vdm10.config_flow._free_extension", return_value=None):
        result = await hass.config_entries.flow.async_configure(flow_id, submit)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_USERNAME] == "cannot_detect_extension"


async def test_manual_door_undetectable(hass, socket_enabled):
    flow_id = await _go_manual(hass)
    submit = {**MANUAL_MIN, CONF_DOOR_HOST: ""}
    with patch("custom_components.vdm10.config_flow._door_host", return_value=None):
        result = await hass.config_entries.flow.async_configure(flow_id, submit)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_DOOR_HOST] == "cannot_detect_door"


async def test_sip_status_success_creates_entry(hass, socket_enabled):
    """a successful registrar probe (200) plus provider/fritz 2xx -> entry"""
    flow_id = await _go_manual(hass)
    with patch(
        "custom_components.vdm10.config_flow._sip_statuses",
        return_value={"registrar": 200, "provider": 200, "fritz": 200},
    ) as probe:
        result = await hass.config_entries.flow.async_configure(flow_id, MANUAL_MIN)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    probe.assert_called_once()


async def test_sip_status_extension_conflict(hass, socket_enabled):
    flow_id = await _go_manual(hass)
    with patch(
        "custom_components.vdm10.config_flow._sip_statuses",
        return_value={"registrar": 410},
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, MANUAL_MIN)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_USERNAME] == "extension_conflict"


async def test_sip_status_registrar_unreachable(hass, socket_enabled):
    flow_id = await _go_manual(hass)
    with patch(
        "custom_components.vdm10.config_flow._sip_statuses",
        return_value={"registrar": None},
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, MANUAL_MIN)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_REGISTRAR_HOST] == "cannot_connect"


async def test_sip_status_provider_invalid_auth(hass, socket_enabled):
    flow_id = await _go_manual(hass)
    with patch(
        "custom_components.vdm10.config_flow._sip_statuses",
        return_value={"registrar": 200, "provider": 403},
    ):
        result = await hass.config_entries.flow.async_configure(flow_id, MANUAL_MIN)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["provider"] == "invalid_auth"


async def test_dhcp_indoor_prefills_everything(hass, socket_enabled):
    """DHCP event on the registrar's MAC: the unit's role is probed, the LAN
    scan fills the outdoor station, and the next free slot is probed - the
    manual form opens with every field derived."""
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    info = DhcpServiceInfo(ip="10.0.0.38", hostname="vdm10-indoor", macaddress="bc9b5e01cab7")
    with (
        patch(
            "custom_components.vdm10.config_flow._probe_role",
            return_value="registrar",
        ) as role,
        patch(
            "custom_components.vdm10.config_flow._is_main_indoor",
            return_value=True,
        ),
        patch(
            "custom_components.vdm10.config_flow._run_discovery",
            return_value=DISCOVERY_RESULT,
        ),
        patch(
            "custom_components.vdm10.config_flow._free_extension",
            return_value="10000000002",
        ) as probe,
        patch(
            "custom_components.vdm10.config_flow._local_ip_for",
            return_value="10.0.0.96",
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "dhcp"}, data=info)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "manual"
    role.assert_called_once_with("10.0.0.38")
    probe.assert_called_once_with("10.0.0.38")
    markers = {k.schema: k for k in result["data_schema"].schema}
    assert markers[CONF_REGISTRAR_HOST].default() == "10.0.0.38"
    assert markers[CONF_USERNAME].default() == "10000000002"
    assert markers[CONF_DOOR_HOST].default() == "10.0.0.48"


async def test_dhcp_tile_aborts_when_entry_exists(hass, mock_entry, socket_enabled):
    """a DHCP event for the already-configured registrar must not recreate a
    tile under 'Entdeckt' - the entry match aborts before any probing."""
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    info = DhcpServiceInfo(ip=ENTRY_DATA[CONF_REGISTRAR_HOST], hostname="vdm10-indoor", macaddress="bc9b5e01cab7")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "dhcp"}, data=info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_dhcp_tile_dies_when_entry_is_created(hass, socket_enabled):
    """a tile opened while no entry existed must disappear once an entry is
    created - HA auto-aborts default-discovery-unique-id flows when a real
    unique id lands."""
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    info = DhcpServiceInfo(ip="10.0.0.38", hostname="vdm10-indoor", macaddress="bc9b5e01cab7")
    with (
        patch(
            "custom_components.vdm10.config_flow._probe_role",
            return_value="registrar",
        ),
        patch(
            "custom_components.vdm10.config_flow._is_main_indoor",
            return_value=True,
        ),
        patch(
            "custom_components.vdm10.config_flow._run_discovery",
            return_value=DISCOVERY_RESULT,
        ),
        patch(
            "custom_components.vdm10.config_flow._free_extension",
            return_value="10000000002",
        ),
        patch(
            "custom_components.vdm10.config_flow._local_ip_for",
            return_value="10.0.0.96",
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "dhcp"}, data=info)
    assert result["type"] is FlowResultType.FORM
    tile_flow_id = result["flow_id"]
    assert tile_flow_id in hass.config_entries.flow._progress

    manual_id = await _go_manual(hass)
    with patch("custom_components.vdm10.station.HaStation.async_start", new=AsyncMock()):
        res = await hass.config_entries.flow.async_configure(manual_id, MANUAL_MIN)
    assert res["type"] is FlowResultType.CREATE_ENTRY
    assert tile_flow_id not in hass.config_entries.flow._progress


async def test_dhcp_door_station_is_suppressed(hass, socket_enabled):
    """a DHCP match on the outdoor station (ISAPI on :80) must not create a
    discovery tile - only the main indoor can host the entry."""
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    info = DhcpServiceInfo(ip="10.0.0.48", hostname="vdm10-door", macaddress="bc9b5e01cab8")
    with patch(
        "custom_components.vdm10.config_flow._probe_role",
        return_value="door",
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "dhcp"}, data=info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_main_indoor"


async def test_dhcp_extension_indoor_is_suppressed(hass, socket_enabled):
    """an extension indoor (indoor role but silent on the :5065 registrar
    fingerprint) is suppressed the same way - it can't host the entry."""
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    info = DhcpServiceInfo(ip="10.0.0.47", hostname="vdm10-ext", macaddress="bc9b5e01cab9")
    with (
        patch(
            "custom_components.vdm10.config_flow._probe_role",
            return_value="registrar",
        ),
        patch(
            "custom_components.vdm10.config_flow._is_main_indoor",
            return_value=False,
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "dhcp"}, data=info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_main_indoor"


async def test_dhcp_suppressed_while_station_holds_probe_port(hass, mock_entry, socket_enabled):
    """the registrar answers on :5060 unconditionally, so the fingerprint can't
    run while our own station holds that port - every unit then reads as an
    indoor and non-configured tiles are suppressed (a foreign :5060 holder on
    a dev box without an entry still fails open)."""
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    info = DhcpServiceInfo(ip="10.0.0.47", hostname="vdm10-ext", macaddress="bc9b5e01cab9")
    with (
        patch(
            "custom_components.vdm10.config_flow._probe_role",
            return_value="registrar",
        ),
        patch(
            "custom_components.vdm10.config_flow._is_main_indoor",
            return_value=None,
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "dhcp"}, data=info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_main_indoor"


async def test_closed_flow_skips_remaining_probes(hass, socket_enabled):
    """closing the dialog mid-scan can't kill the running executor job, but
    the free-extension probe that would follow it is skipped."""
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

    info = DhcpServiceInfo(ip="10.0.0.38", hostname="vdm10-indoor", macaddress="bc9b5e01cab7")

    def close_and_scan():
        flow_id = next(iter(hass.config_entries.flow._progress))
        hass.config_entries.flow.async_abort(flow_id)
        return DISCOVERY_RESULT

    with (
        patch(
            "custom_components.vdm10.config_flow._probe_role",
            return_value="registrar",
        ),
        patch(
            "custom_components.vdm10.config_flow._is_main_indoor",
            return_value=True,
        ),
        patch(
            "custom_components.vdm10.config_flow._run_discovery",
            side_effect=close_and_scan,
        ),
        patch(
            "custom_components.vdm10.config_flow._free_extension",
        ) as probe,
        patch(
            "custom_components.vdm10.config_flow._local_ip_for",
            return_value="10.0.0.96",
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "dhcp"}, data=info)
    assert result["type"] is FlowResultType.ABORT
    probe.assert_not_called()
