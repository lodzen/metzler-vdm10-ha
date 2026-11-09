"""Config flow: form, entry creation, duplicate abort."""

from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType

from conftest import ENTRY_DATA
from custom_components.vdm10.const import DOMAIN

# the flow never binds sockets - any schema-valid values work (ENTRY_DATA uses
# ephemeral port 0 for the real station fixture, which the schema would reject;
# rtsp_port 554 is also privileged here - use an unprivileged stand-in)
FLOW_DATA = {**ENTRY_DATA, "listen_port": 5060, "rtsp_port": 8554}


async def test_form_creates_entry(hass, socket_enabled):  # bind-check touches sockets
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    # CREATE_ENTRY triggers async_setup_entry - stub out the station so the test
    # doesn't need real sockets/threads
    with patch("custom_components.vdm10.station.HaStation.async_start", new=AsyncMock()):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], FLOW_DATA)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"].startswith("VDM10")


async def test_duplicate_serial_host_aborts(hass, mock_entry, socket_enabled):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], FLOW_DATA)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
