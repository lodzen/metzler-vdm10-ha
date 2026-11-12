"""Config flow for the VDM10 intercom integration.

Only the main indoor's IP + password are strictly required:

- ``async_step_discover`` runs the library's LAN probes (SADP inquiry for
  outdoor stations, the :5065 REGISTER fingerprint for the main indoor's
  registrar) and prefills what it finds.
- ``local_ip`` is derived via a UDP-connect to the registrar; ``serial`` is
  generated deterministically; the door ISAPI login defaults to ``admin`` +
  the indoor password (the units share it by default).
- ports/serial overrides live in a collapsed "Advanced" section; the optional
  SIP provider (Linphone/PBX) and Fritz trunk in collapsed sections - a call
  then races: whichever side (HA or the trunk) answers first wins.
"""

from __future__ import annotations

import hashlib
import logging
import socket

import probatio as vol  # HA's voluptuous fork; data_schema wants probatio.Schema
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import section

from .const import (
    CONF_ADVANCED,
    CONF_DOOR_HOST,
    CONF_DOOR_PASSWORD,
    CONF_DOOR_USER,
    CONF_FRITZ,
    CONF_FRITZ_DOMAIN,
    CONF_FRITZ_HOST,
    CONF_FRITZ_PASS,
    CONF_FRITZ_PORT,
    CONF_FRITZ_TARGET,
    CONF_FRITZ_USER,
    CONF_LISTEN_PORT,
    CONF_LOCAL_IP,
    CONF_PASSWORD,
    CONF_PROVIDER,
    CONF_REGISTRAR_HOST,
    CONF_REGISTRAR_PORT,
    CONF_RTSP_PORT,
    CONF_SERIAL,
    CONF_SIP_DOMAIN,
    CONF_SIP_HOST,
    CONF_SIP_PASS,
    CONF_SIP_PORT,
    CONF_SIP_SRTP,
    CONF_SIP_TARGET,
    CONF_SIP_USER,
    CONF_USERNAME,
    DEFAULT_DOOR_USER,
    DEFAULT_FRITZ_DOMAIN,
    DEFAULT_FRITZ_PORT,
    DEFAULT_LISTEN_PORT,
    DEFAULT_REGISTRAR_PORT,
    DEFAULT_RTSP_PORT,
    DEFAULT_SIP_PORT,
    DISCOVER_MANUAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
DISCOVER_TIMEOUT = 3.0


def _serial_for(registrar: str, username: str) -> str:
    """deterministic regDevSerial - unique per registrar+extension (the
    registrar's dev_reg dedup keys on serial OR ip)"""
    digest = hashlib.md5(f"{registrar}|{username}".encode()).hexdigest()[:8].upper()
    return f"VDM10HA{digest}"


def _run_discovery() -> dict:
    """executor: SADP + REGISTER fingerprint; empty dict on any failure."""
    from metzler import discovery  # vendored in release installs

    try:
        local_ip = discovery.local_ip_for("10.0.0.1", 80)
    except OSError:
        local_ip = None
    try:
        return discovery.discover(timeout=DISCOVER_TIMEOUT, local_ip=local_ip)
    except OSError as err:
        # :5060 in use (a running entry) or no route - fall back to manual
        _LOGGER.debug("VDM10 discovery failed: %s", err)
        return {"doors": [], "registrars": [], "candidates": []}


def _free_extension(registrar: str) -> str | None:
    """executor: lowest free extension slot on the registrar."""
    from metzler import discovery

    try:
        return discovery.free_extension(registrar, timeout=DISCOVER_TIMEOUT)
    except OSError:
        return None


def _check_bind(ip: str, listen_port: int, rtsp_port: int) -> str | None:
    """bind-test SIP/RTSP ports on local_ip in the executor - catches
    address-not-on-host and port-in-use before the entry is created."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((ip, listen_port))
        sock.close()
    except OSError:
        return CONF_LISTEN_PORT
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((ip, rtsp_port))
        sock.close()
    except OSError:
        return CONF_RTSP_PORT
    return None


def _manual_schema(prefill: dict) -> vol.Schema:
    """the manual/field form; ``prefill`` carries values found by discovery."""
    adv = prefill.get(CONF_ADVANCED, {})
    return vol.Schema(
        {
            vol.Required(CONF_REGISTRAR_HOST, default=prefill.get(CONF_REGISTRAR_HOST, "")): str,
            vol.Required(CONF_USERNAME, default=prefill.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD, default=""): str,
            vol.Required(CONF_DOOR_HOST, default=adv.get(CONF_DOOR_HOST, prefill.get(CONF_DOOR_HOST, ""))): str,
            vol.Optional(CONF_NAME, default=""): str,
            vol.Optional(CONF_ADVANCED): section(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_REGISTRAR_PORT, default=adv.get(CONF_REGISTRAR_PORT, DEFAULT_REGISTRAR_PORT)
                        ): PORT,
                        vol.Optional(CONF_LOCAL_IP, default=adv.get(CONF_LOCAL_IP, "")): str,
                        vol.Optional(CONF_SERIAL, default=adv.get(CONF_SERIAL, "")): str,
                        vol.Optional(CONF_LISTEN_PORT, default=adv.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT)): PORT,
                        vol.Optional(CONF_RTSP_PORT, default=adv.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)): PORT,
                        vol.Optional(CONF_DOOR_USER, default=adv.get(CONF_DOOR_USER, DEFAULT_DOOR_USER)): str,
                        vol.Optional(CONF_DOOR_PASSWORD, default=adv.get(CONF_DOOR_PASSWORD, "")): str,
                    }
                ),
                {"collapsed": True},
            ),
            vol.Optional(CONF_PROVIDER): section(
                vol.Schema(
                    {
                        vol.Optional(CONF_SIP_HOST, default=""): str,
                        vol.Optional(CONF_SIP_PORT, default=DEFAULT_SIP_PORT): PORT,
                        vol.Optional(CONF_SIP_USER, default=""): str,
                        vol.Optional(CONF_SIP_PASS, default=""): str,
                        vol.Optional(CONF_SIP_TARGET, default=""): str,
                        vol.Optional(CONF_SIP_DOMAIN, default=""): str,
                        vol.Optional(CONF_SIP_SRTP, default=False): bool,
                    }
                ),
                {"collapsed": True},
            ),
            vol.Optional(CONF_FRITZ): section(
                vol.Schema(
                    {
                        vol.Optional(CONF_FRITZ_HOST, default=""): str,
                        vol.Optional(CONF_FRITZ_PORT, default=DEFAULT_FRITZ_PORT): PORT,
                        vol.Optional(CONF_FRITZ_DOMAIN, default=DEFAULT_FRITZ_DOMAIN): str,
                        vol.Optional(CONF_FRITZ_USER, default=""): str,
                        vol.Optional(CONF_FRITZ_PASS, default=""): str,
                        vol.Optional(CONF_FRITZ_TARGET, default=""): str,
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


class Vdm10ConfigFlow(ConfigFlow, domain=DOMAIN):
    """one door intercom per entry; unique id = serial@registrar"""

    VERSION = 1

    def __init__(self) -> None:
        self._prefill: dict = {}

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["discover", "manual"])

    async def async_step_discover(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None:
            found = await self.hass.async_add_executor_job(_run_discovery)
            registrar_opts = found["registrars"] + found["candidates"] + [DISCOVER_MANUAL]
            door_ips = [d.get("IPv4Address") or d.get("_ip") for d in found["doors"]]
            door_opts = [ip for ip in door_ips if ip] + [DISCOVER_MANUAL]
            if not found["registrars"] and not found["doors"]:
                errors["base"] = "nothing_found"
            return self.async_show_form(
                step_id="discover",
                data_schema=vol.Schema(
                    {
                        vol.Required("registrar_pick", default=registrar_opts[0]): vol.In(registrar_opts),
                        vol.Required("door_pick", default=door_opts[0]): vol.In(door_opts),
                    }
                ),
                errors=errors,
            )
        registrar = user_input["registrar_pick"]
        door = user_input["door_pick"]
        if registrar != DISCOVER_MANUAL:
            self._prefill[CONF_REGISTRAR_HOST] = registrar
            ext = await self.hass.async_add_executor_job(_free_extension, registrar)
            if ext:
                self._prefill[CONF_USERNAME] = ext
        if door != DISCOVER_MANUAL:
            self._prefill[CONF_DOOR_HOST] = door
        return self.async_show_form(step_id="manual", data_schema=_manual_schema(self._prefill), errors={})

    async def async_step_manual(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            return await self._finish(user_input, errors)
        return self.async_show_form(step_id="manual", data_schema=_manual_schema(self._prefill), errors={})

    async def _finish(self, user_input: dict, errors: dict) -> ConfigFlowResult:
        data = _flatten(user_input)
        for key in (CONF_REGISTRAR_HOST, CONF_DOOR_HOST, CONF_USERNAME):
            if not data[key]:
                errors[key] = "required"
        if errors:
            return self._show_manual(user_input, errors)
        if not data[CONF_LOCAL_IP]:
            try:
                data[CONF_LOCAL_IP] = await self.hass.async_add_executor_job(
                    _local_ip_for, data[CONF_REGISTRAR_HOST], data[CONF_REGISTRAR_PORT]
                )
            except OSError:
                return self._show_manual(user_input, {"base": "cannot_reach"})
        data[CONF_SERIAL] = data[CONF_SERIAL] or _serial_for(data[CONF_REGISTRAR_HOST], data[CONF_USERNAME])
        if bad := await self.hass.async_add_executor_job(
            _check_bind, data[CONF_LOCAL_IP], data[CONF_LISTEN_PORT], data[CONF_RTSP_PORT]
        ):
            return self._show_manual(user_input, {bad: "cannot_bind"})
        await self.async_set_unique_id(f"{data[CONF_SERIAL]}@{data[CONF_REGISTRAR_HOST]}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data[CONF_NAME] or f"VDM10 {data[CONF_USERNAME]}", data=data)

    def _show_manual(self, user_input: dict, errors: dict) -> ConfigFlowResult:
        return self.async_show_form(step_id="manual", data_schema=_manual_schema(user_input), errors=errors)


def _flatten(user_input: dict) -> dict:
    """merge the advanced/provider/fritz sections into one flat data dict"""
    adv = user_input.get(CONF_ADVANCED) or {}
    data = {
        CONF_REGISTRAR_HOST: user_input[CONF_REGISTRAR_HOST].strip(),
        CONF_USERNAME: user_input[CONF_USERNAME].strip(),
        CONF_PASSWORD: user_input[CONF_PASSWORD],
        CONF_DOOR_HOST: user_input[CONF_DOOR_HOST].strip(),
        CONF_NAME: user_input.get(CONF_NAME, ""),
        CONF_REGISTRAR_PORT: adv.get(CONF_REGISTRAR_PORT, DEFAULT_REGISTRAR_PORT),
        CONF_LOCAL_IP: adv.get(CONF_LOCAL_IP),
        CONF_SERIAL: adv.get(CONF_SERIAL),
        CONF_LISTEN_PORT: adv.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT),
        CONF_RTSP_PORT: adv.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
        CONF_DOOR_USER: adv.get(CONF_DOOR_USER, DEFAULT_DOOR_USER),
        CONF_DOOR_PASSWORD: adv.get(CONF_DOOR_PASSWORD) or user_input[CONF_PASSWORD],
    }
    data.update(user_input.get(CONF_PROVIDER) or {})
    data.update(user_input.get(CONF_FRITZ) or {})
    return data


def _local_ip_for(host: str, port: int) -> str:
    """UDP-connect trick (no traffic sent) -> local IP that reaches the registrar."""
    from metzler import discovery

    return discovery.local_ip_for(host, port)
