"""vdm10 component tests: a real IndoorStation on 127.0.0.1 ephemeral ports.

PHACC allows sockets to 127.0.0.1 (socket_allow_hosts), so the station binds
real UDP/RTSP sockets on port 0; the registrar address is a dead localhost port,
which keeps ``registered`` False and exercises the unregistered/retry paths.
Packets the station emits are real UDP datagrams to dead localhost ports -
localhost-only, nothing leaves the machine.
"""

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.vdm10.const import (
    CONF_DOOR_HOST,
    CONF_DOOR_PASSWORD,
    CONF_DOOR_USER,
    CONF_LISTEN_PORT,
    CONF_LOCAL_IP,
    CONF_PASSWORD,
    CONF_REGISTRAR_HOST,
    CONF_REGISTRAR_PORT,
    CONF_RTSP_PORT,
    CONF_SERIAL,
    CONF_USERNAME,
    DOMAIN,
)
from metzler.sip.message import Packet

pytest_plugins = "pytest_homeassistant.plugins"

ENTRY_DATA = {
    CONF_REGISTRAR_HOST: "127.0.0.1",
    CONF_REGISTRAR_PORT: 5098,  # dead registrar: registration stays pending
    CONF_USERNAME: "10000000003",
    CONF_PASSWORD: "pw",
    CONF_SERIAL: "Q12345678",
    CONF_LOCAL_IP: "127.0.0.1",
    CONF_LISTEN_PORT: 0,  # ephemeral SIP UDP bind
    CONF_RTSP_PORT: 0,  # ephemeral RTSP server bind
    CONF_DOOR_HOST: "127.0.0.1",
    CONF_DOOR_USER: "admin",
    CONF_DOOR_PASSWORD: "doorpw",
}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def door_invite(call_id="doorcall1"):
    """the door fan-out INVITE: From door devLongNum, 'call to app' Subject,
    header-only SDP with door attrs (shape copied from the library test suite)"""
    return Packet.parse(
        "INVITE sip:10000000003@127.0.0.1:5060 SIP/2.0\n"
        "Via: SIP/2.0/UDP 127.0.0.1:5065;branch=z9hG4bKdoor\n"
        "From: <sip:10010100000@127.0.0.1>;tag=1\n"
        "To: <sip:10000000003@127.0.0.1:5060>\n"
        f"Call-ID: {call_id}\n"
        "CSeq: 20 INVITE\n"
        "Subject: This is a call to app.\n"
        "Content-Type: application/sdp\n"
        "Content-Length: 95\n\n"
        "v=0\r\no=10010100000 0 0 IN IP4 127.0.0.1\r\ns=call\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        "a=doorType:1\r\na=isSpecialType:2\r\n"
    )


def indoor_invite(call_id="indoorcall1"):
    """an indoor station's extension INVITE: From a class-3 devLongNum,
    'call to extension' Subject, header-only SDP (no door attrs)"""
    return Packet.parse(
        "INVITE sip:10000000003@127.0.0.1:5060 SIP/2.0\n"
        "Via: SIP/2.0/UDP 127.0.0.1:5065;branch=z9hG4bKindoor\n"
        "From: <sip:10000000000@127.0.0.1>;tag=1\n"
        "To: <sip:10000000003@127.0.0.1:5060>\n"
        f"Call-ID: {call_id}\n"
        "CSeq: 20 INVITE\n"
        "Subject: This is a call to extension.\n"
        "Content-Type: application/sdp\n"
        "Content-Length: 62\n\n"
        "v=0\r\no=10000000000 0 0 IN IP4 127.0.0.1\r\ns=call\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
    )


def provider_invite(call_id="provcall1"):
    """an inbound SIP-side INVITE: the app dialed our provider account, the
    library bridges it to the main indoor (which then rings)"""
    return Packet.parse(
        "INVITE sip:ha@127.0.0.1:5060 SIP/2.0\n"
        "Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKprov\n"
        "From: <sip:alice@sip.linphone.org>;tag=1\n"
        "To: <sip:ha@127.0.0.1:5060>\n"
        f"Call-ID: {call_id}\n"
        "CSeq: 20 INVITE\n"
        "Contact: <sip:alice@127.0.0.1:5060>\n"
        "Content-Type: application/sdp\n"
        "Content-Length: 96\n\n"
        "v=0\r\no=alice 0 0 IN IP4 127.0.0.1\r\ns=call\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        "m=audio 40000 RTP/AVP 0 8 101\r\n"
    )


@pytest.fixture
def mock_entry(hass, request):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options=getattr(request, "param", {}),
        title="VDM10 test",
        unique_id="Q12345678@127.0.0.1",  # config flow's serial@registrar key
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def runtime(hass, mock_entry, socket_enabled):
    """a set-up config entry with a live station; torn down on test end.
    socket_enabled: pytest-socket blocks socket creation globally - the station
    needs real UDP/RTSP binds on 127.0.0.1 ephemeral ports"""
    await hass.config_entries.async_setup(mock_entry.entry_id)
    await hass.async_block_till_done()
    yield mock_entry.runtime_data
    await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()


async def fire_ring(runtime, call_id="doorcall1"):
    """inject the door fan-out INVITE straight into the station's handler"""
    runtime.station.station.on_invite(door_invite(call_id), ("127.0.0.1", 5065))
    await runtime.hass.async_block_till_done()


async def fire_indoor_ring(runtime, call_id="indoorcall1"):
    """inject an indoor extension INVITE (not a door call)"""
    runtime.station.station.on_invite(indoor_invite(call_id), ("127.0.0.1", 5065))
    await runtime.hass.async_block_till_done()


async def fire_provider_call(runtime, call_id="provcall1"):
    """inject an inbound provider INVITE (the app dials us -> we ring the
    indoor); the source addr must match the configured trunk proxy"""
    runtime.station.station.on_invite(provider_invite(call_id), ("127.0.0.1", 5060))
    await runtime.hass.async_block_till_done()
