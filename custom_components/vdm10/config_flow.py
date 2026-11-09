"""Config flow for the VDM10 intercom integration."""

from __future__ import annotations

import logging
import socket

import probatio as vol  # HA's voluptuous fork; data_schema wants probatio.Schema
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME

from .const import (
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
    DEFAULT_DOOR_USER,
    DEFAULT_LISTEN_PORT,
    DEFAULT_REGISTRAR_PORT,
    DEFAULT_RTSP_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REGISTRAR_HOST): str,
        vol.Optional(CONF_REGISTRAR_PORT, default=DEFAULT_REGISTRAR_PORT): PORT,
        vol.Required(CONF_USERNAME): str,
        vol.Optional(CONF_PASSWORD, default=""): str,
        vol.Required(CONF_SERIAL): str,
        vol.Required(CONF_LOCAL_IP): str,
        vol.Optional(CONF_LISTEN_PORT, default=DEFAULT_LISTEN_PORT): PORT,
        vol.Optional(CONF_RTSP_PORT, default=DEFAULT_RTSP_PORT): PORT,
        vol.Required(CONF_DOOR_HOST): str,
        vol.Optional(CONF_DOOR_USER, default=DEFAULT_DOOR_USER): str,
        vol.Optional(CONF_DOOR_PASSWORD, default=""): str,
        vol.Optional(CONF_NAME, default=""): str,
    }
)


def _check_bind(user_input: dict) -> str | None:
    """bind-test the SIP/RTSP ports on local_ip in the executor - catches
    address-not-on-host and port-in-use before the entry is created. Returns the
    field key that failed, or None."""
    ip = user_input[CONF_LOCAL_IP]
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((ip, user_input[CONF_LISTEN_PORT]))
        sock.close()
    except OSError:
        return CONF_LISTEN_PORT
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((ip, user_input[CONF_RTSP_PORT]))
        sock.close()
    except OSError:
        return CONF_RTSP_PORT
    return None


class Vdm10ConfigFlow(ConfigFlow, domain=DOMAIN):
    """one door intercom per entry; unique id = serial@registrar"""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input[CONF_REGISTRAR_HOST].strip():
                errors[CONF_REGISTRAR_HOST] = "required"
            if not user_input[CONF_LOCAL_IP].strip():
                errors[CONF_LOCAL_IP] = "required"
            if not user_input[CONF_DOOR_HOST].strip():
                errors[CONF_DOOR_HOST] = "required"
            if not errors:
                if bad := await self.hass.async_add_executor_job(_check_bind, user_input):
                    errors[bad] = "cannot_bind"
            if not errors:
                unique = f"{user_input[CONF_SERIAL]}@{user_input[CONF_REGISTRAR_HOST]}"
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                title = user_input.get(CONF_NAME) or f"VDM10 {user_input[CONF_USERNAME]}"
                return self.async_create_entry(title=title, data=user_input)
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)
