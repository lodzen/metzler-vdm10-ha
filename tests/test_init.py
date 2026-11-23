"""entry lifecycle: unload must unregister at the indoor station."""

import asyncio
import contextlib
import socket
import threading

from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from conftest import ENTRY_DATA
from custom_components.metzler_vdm10.const import CONF_REGISTRAR_PORT


@contextlib.contextmanager
def registrar_collector(port):
    """capture raw datagrams on the fake registrar's port in a thread"""
    received = []
    stop = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", port))
    listener.settimeout(0.1)

    def serve():
        while not stop.is_set():
            try:
                data, _addr = listener.recvfrom(65535)
            except TimeoutError:
                continue
            received.append(data.decode())

    collector = threading.Thread(target=serve, daemon=True)
    collector.start()
    try:
        yield received
    finally:
        stop.set()
        collector.join(timeout=2)
        listener.close()


def _expires_zero(received):
    return [d for d in received if "REGISTER" in d and "Expires: 0" in d]


async def test_unload_entry_sends_expires_zero(hass, mock_entry, socket_enabled):
    """the library's shutdown() sends REGISTER Expires: 0 - capture the real
    datagram on the registrar port to prove the unload path triggers it"""
    with registrar_collector(ENTRY_DATA[CONF_REGISTRAR_PORT]) as received:
        await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()
        await asyncio.sleep(0.5)  # datagram is sent synchronously; grace for recv

    unregs = _expires_zero(received)
    assert unregs, f"no Expires: 0 REGISTER among {len(received)} datagrams"
    # regXML rides along: serial_no match cleans the row regardless of source IP
    assert any("<regDevSerial>Q12345678</regDevSerial>" in d for d in unregs)


async def test_ha_stop_sends_expires_zero(hass, mock_entry, socket_enabled):
    """HA shutdown does not unload entries - the stop listener must trigger the
    unregister or the registrar row lingers until its TTL (observed bug)"""
    with registrar_collector(ENTRY_DATA[CONF_REGISTRAR_PORT]) as received:
        await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()
        await asyncio.sleep(0.5)

    assert _expires_zero(received), f"no Expires: 0 REGISTER among {len(received)} datagrams on HA stop"
