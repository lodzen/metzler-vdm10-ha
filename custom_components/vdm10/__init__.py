"""VDM10 door intercom - Home Assistant integration.

Wraps the ``metzler`` library (vendored for Marketplace installs, path
dependency in development): doorbell ring binary_sensor, on-demand door camera,
explicit answer/hangup, muted-by-default two-way audio.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady

from . import vendor as _vendor_loader  # noqa: F401 - MUST run before `import metzler`
from .const import DOMAIN, PLATFORMS, SERVICE_ANSWER_CALL, SERVICE_HANGUP_CALL
from .coordinator import Vdm10Runtime

_LOGGER = logging.getLogger(__name__)

type Vdm10ConfigEntry = ConfigEntry[Vdm10Runtime]


async def async_setup_entry(hass: HomeAssistant, entry: Vdm10ConfigEntry) -> bool:
    runtime = Vdm10Runtime(hass, entry)
    try:
        await runtime.async_start()
    except (OSError, ValueError) as err:
        raise ConfigEntryNotReady(f"VDM10 station failed to start: {err}") from err
    entry.runtime_data = runtime
    entry.async_on_unload(entry.add_update_listener(_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    return True


async def _update_listener(hass: HomeAssistant, entry: Vdm10ConfigEntry) -> None:
    """options changed -> reload so build_options picks them up"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: Vdm10ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            for service in (SERVICE_ANSWER_CALL, SERVICE_HANGUP_CALL):
                hass.services.async_remove(DOMAIN, service)
    return unload_ok


def _iter_runtimes(hass: HomeAssistant):
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        yield entry.runtime_data


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_ANSWER_CALL):
        return

    async def _answer(_call: ServiceCall) -> None:
        answered = False
        for runtime in _iter_runtimes(hass):
            answered = await runtime.async_answer_call() or answered
        if not answered:
            _LOGGER.warning("vdm10.answer_call: no ringing call on any entry")

    async def _hangup(_call: ServiceCall) -> None:
        hung = False
        for runtime in _iter_runtimes(hass):
            hung = await runtime.async_hangup_call() or hung
        if not hung:
            _LOGGER.warning("vdm10.hangup_call: no active call on any entry")

    hass.services.async_register(DOMAIN, SERVICE_ANSWER_CALL, _answer)
    hass.services.async_register(DOMAIN, SERVICE_HANGUP_CALL, _hangup)
