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
import ipaddress
import logging
import socket

import probatio as vol  # HA's voluptuous fork; data_schema wants probatio.Schema
from homeassistant.config_entries import (
    DEFAULT_DISCOVERY_UNIQUE_ID,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

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
    CONF_INBOUND_TARGET,
    CONF_LISTEN_PORT,
    CONF_LOCAL_IP,
    CONF_PASSWORD,
    CONF_PRELOAD_STREAM,
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
    DOOR_ISAPI_PORT,
)

_LOGGER = logging.getLogger(__name__)

PORT = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
DISCOVER_TIMEOUT = 3.0
PROBE_TIMEOUT = 6.0  # free_extension sweeps slot-by-slot; 410s are fast but a hit adds unreg retries


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
        return {"doors": [], "registrars": [], "candidates": [], "vdm10_macs": {}}


def _free_extension(registrar: str) -> str | None:
    """executor: lowest free extension slot on the registrar."""
    from metzler import discovery

    try:
        return discovery.free_extension(registrar, timeout=PROBE_TIMEOUT)
    except OSError:
        return None


def _door_host() -> str | None:
    """executor: first outdoor station found by the SADP scan, else None."""
    try:
        doors = _run_discovery()["doors"]
    except OSError:
        return None
    return (doors[0].get("IPv4Address") or doors[0].get("_ip")) if doors else None


def _sip_statuses(data: dict) -> dict:
    """executor: REGISTER status code per configured leg - the intercom
    registrar probe runs the same dialect as free_extension (200 -> the row
    is removed again); provider/fritz get a standard digest REGISTER when
    credentials exist (trunk mode without them can't be verified)."""
    from metzler import discovery

    results = {
        "registrar": discovery.register_status(
            data[CONF_REGISTRAR_HOST],
            data[CONF_USERNAME],
            data[CONF_SERIAL],
            data[CONF_LOCAL_IP],
            data[CONF_REGISTRAR_PORT],
        )
    }
    if data.get(CONF_SIP_HOST) and data.get(CONF_SIP_USER):
        results[CONF_PROVIDER] = discovery.provider_status(
            data[CONF_SIP_HOST],
            data.get(CONF_SIP_PORT, DEFAULT_SIP_PORT),
            data[CONF_SIP_USER],
            data.get(CONF_SIP_PASS, ""),
            data.get(CONF_SIP_DOMAIN) or data[CONF_SIP_HOST],
            data[CONF_LOCAL_IP],
        )
    if data.get(CONF_FRITZ_HOST) and data.get(CONF_FRITZ_USER):
        results[CONF_FRITZ] = discovery.provider_status(
            data[CONF_FRITZ_HOST],
            data.get(CONF_FRITZ_PORT, DEFAULT_FRITZ_PORT),
            data[CONF_FRITZ_USER],
            data.get(CONF_FRITZ_PASS, ""),
            data.get(CONF_FRITZ_DOMAIN) or data[CONF_FRITZ_HOST],
            data[CONF_LOCAL_IP],
        )
    return results


def _probe_role(ip: str) -> str:
    """executor: door stations serve ISAPI HTTP on :80; indoor units don't
    run HTTP at all (observed: main indoor has :554/:8000/:8102, no :80)."""
    try:
        socket.create_connection((ip, DOOR_ISAPI_PORT), timeout=1.0).close()
        return "door"
    except OSError:
        return "registrar"


def _is_main_indoor(ip: str) -> bool | None:
    """executor: the registrar fingerprint - only the main indoor answers
    :5065 REGISTERs; extension indoors stay silent. Probing the last slot
    avoids touching the …00 AOR (the main's own number); any reply means a
    registrar is present. None when the probe socket can't bind :5060 (the
    registrar replies there unconditionally) - the caller decides."""
    from metzler import discovery

    try:
        return discovery.registrar_probe(ip, ext=f"{discovery.EXT_BASE}5", timeout=2.0) is not None
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


def _looks_natted(ip: str) -> bool:
    """172.16.0.0/12 is docker's default bridge space — a local_ip derived
    there is the container's NAT address, not a usable host LAN IP."""
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("172.16.0.0/12")
    except ValueError:
        return True


def _docker() -> bool:
    """running inside a container runtime (docker, containerd, podman, ...)."""
    from metzler import discovery

    return discovery.docker_detected()


def _egress_ip() -> str:
    """executor: the source IP a LAN scan/derive would use - the NAT'd bridge
    address in bridge-mode containers, the real LAN IP with host networking
    or on bare metal; '' when there's no route at all."""
    try:
        return _local_ip_for("10.0.0.1", 80)
    except OSError:
        return ""


def _manual_schema(prefill: dict, ask_local_ip: bool = False) -> vol.Schema:
    """the manual/field form; ``prefill`` carries values found by discovery.

    ``local_ip`` is auto-derived when the egress IP is usable (hidden in
    Advanced) but becomes a required top-level field when it isn't (NAT'd
    container bridges).
    """
    adv = prefill.get(CONF_ADVANCED, {})
    fields = {
        vol.Required(CONF_REGISTRAR_HOST, default=prefill.get(CONF_REGISTRAR_HOST, "")): str,
        vol.Optional(CONF_USERNAME, default=prefill.get(CONF_USERNAME, "")): str,
        vol.Required(CONF_PASSWORD, default=""): str,
        vol.Optional(CONF_DOOR_HOST, default=adv.get(CONF_DOOR_HOST, prefill.get(CONF_DOOR_HOST, ""))): str,
        vol.Optional(CONF_NAME, default=""): str,
    }
    if ask_local_ip:
        fields[vol.Required(CONF_LOCAL_IP, default=prefill.get(CONF_LOCAL_IP, ""))] = str
    advanced = {
        vol.Optional(CONF_REGISTRAR_PORT, default=adv.get(CONF_REGISTRAR_PORT, DEFAULT_REGISTRAR_PORT)): PORT,
        vol.Optional(CONF_SERIAL, default=adv.get(CONF_SERIAL, "")): str,
        vol.Optional(CONF_LISTEN_PORT, default=adv.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT)): PORT,
        vol.Optional(CONF_RTSP_PORT, default=adv.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)): PORT,
        vol.Optional(CONF_DOOR_USER, default=adv.get(CONF_DOOR_USER, DEFAULT_DOOR_USER)): str,
        vol.Optional(CONF_DOOR_PASSWORD, default=adv.get(CONF_DOOR_PASSWORD, "")): str,
        vol.Optional(CONF_PRELOAD_STREAM, default=adv.get(CONF_PRELOAD_STREAM, False)): bool,
    }
    if not ask_local_ip:
        advanced[vol.Optional(CONF_LOCAL_IP, default=adv.get(CONF_LOCAL_IP, ""))] = str
    fields.update(
        {
            vol.Optional(CONF_ADVANCED): section(vol.Schema(advanced), {"collapsed": True}),
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
                        vol.Optional(CONF_INBOUND_TARGET, default=""): str,
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
    return vol.Schema(fields)


class Vdm10ConfigFlow(ConfigFlow, domain=DOMAIN):
    """one door intercom per entry; unique id = serial@registrar"""

    VERSION = 1

    _scan_fut = None  # executor Future shared across concurrent flow instances

    def __init__(self) -> None:
        self._prefill: dict = {}
        self._docker_env: bool | None = None
        self._natted: bool | None = None

    async def _scan(self) -> dict:
        """one in-flight LAN scan shared across concurrent flow instances -
        a DHCP event per unit (door + indoors fire together) or a manual
        retry must not each stampede the subnet."""
        cls = type(self)
        if cls._scan_fut is None or cls._scan_fut.done():
            cls._scan_fut = self.hass.async_add_executor_job(_run_discovery)
        return await cls._scan_fut

    async def _is_docker(self) -> bool:
        """cached; the filesystem probe runs in the executor."""
        if self._docker_env is None:
            self._docker_env = await self.hass.async_add_executor_job(_docker)
        return self._docker_env

    async def _container_natted(self) -> bool:
        """container AND the egress IP is the NAT'd bridge address -> a subnet
        scan is useless and local_ip can't be derived, so the host's LAN IP
        must be asked for. Host-networked containers (HAOS) scan fine."""
        if self._natted is None:
            self._natted = await self._is_docker() and _looks_natted(await self.hass.async_add_executor_job(_egress_ip))
        return self._natted

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["discover", "manual"])

    def _flow_alive(self) -> bool:
        """the user can close the dialog mid-scan - a running executor job
        can't be killed, but remaining probe stages can still be skipped.
        (``async_get`` serializes the flow and raises on a mid-step one, so
        check the manager's progress map directly.)"""
        flow_id = getattr(self, "flow_id", None)
        progress = getattr(self.hass.config_entries.flow, "_progress", None)
        if flow_id is None or progress is None:
            return True
        return flow_id in progress

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> ConfigFlowResult:
        """DHCP discovery (Hikvision OUI match): every VDM10 unit shares the
        OUI, so the tile is only kept for the main indoor - door stations
        (ISAPI on :80) and extension indoors (no :5065 registrar) are
        suppressed. Only cheap single-host probes run here; the LAN-wide
        scan and the extension-slot probing wait for user confirmation
        (async_step_dhcp_confirm)."""
        self._async_abort_entries_match({CONF_REGISTRAR_HOST: discovery_info.ip})
        self.context["title_placeholders"] = {"host": discovery_info.ip}
        # sentinel unique id: repeated dhcp events collapse into one tile, and
        # the tile is auto-aborted the moment any entry lands a real unique id
        await self.async_set_unique_id(DEFAULT_DISCOVERY_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        role = await self.hass.async_add_executor_job(_probe_role, discovery_info.ip)
        if role == "door":
            return self.async_abort(reason="not_main_indoor")
        main = await self.hass.async_add_executor_job(_is_main_indoor, discovery_info.ip)
        if main is None:
            # :5060 is held - on a real install by our own running station
            # (every unit then reads as indoor); a foreign holder is the
            # dev-box edge case where failing open keeps the tile alive
            main = not any(getattr(e, "runtime_data", None) for e in self._async_current_entries())
        if not main:
            return self.async_abort(reason="not_main_indoor")
        self._prefill[CONF_REGISTRAR_HOST] = discovery_info.ip
        try:
            self._prefill[CONF_LOCAL_IP] = await self.hass.async_add_executor_job(
                _local_ip_for, discovery_info.ip, DEFAULT_REGISTRAR_PORT
            )
        except OSError:
            pass
        return await self.async_step_dhcp_confirm()

    async def async_step_dhcp_confirm(self, user_input: dict | None = None) -> ConfigFlowResult:
        """confirmation gate behind the dhcp tile: the LAN-wide scan (SADP
        multicast + subnet sweep) and the REGISTER slot probing against the
        registrar only run once the user accepts the discovered device."""
        if user_input is None:
            return self.async_show_form(
                step_id="dhcp_confirm",
                description_placeholders={"host": self._prefill.get(CONF_REGISTRAR_HOST, "")},
            )
        found = await self._scan()
        if not self._flow_alive():
            return self.async_abort(reason="closed")
        if found["doors"]:
            door = found["doors"][0]
            self._prefill[CONF_DOOR_HOST] = door.get("IPv4Address") or door.get("_ip")
        ext = await self.hass.async_add_executor_job(_free_extension, self._prefill[CONF_REGISTRAR_HOST])
        if not self._flow_alive():
            return self.async_abort(reason="closed")
        if ext:
            self._prefill[CONF_USERNAME] = ext
        return await self.async_step_manual()

    async def async_step_discover(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is None and await self._container_natted():
            # NAT'd bridge IPs are useless for a subnet scan - ask the host IP
            return await self._show_manual(self._prefill, {"base": "docker_detected"})
        if user_input is None:
            found = await self._scan()
            if not self._flow_alive():
                return self.async_abort(reason="closed")
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
            if not self._flow_alive():
                return self.async_abort(reason="closed")
            if ext:
                self._prefill[CONF_USERNAME] = ext
            try:
                self._prefill[CONF_LOCAL_IP] = await self.hass.async_add_executor_job(
                    _local_ip_for, registrar, DEFAULT_REGISTRAR_PORT
                )
            except OSError:
                pass
        if door != DISCOVER_MANUAL:
            self._prefill[CONF_DOOR_HOST] = door
        return await self._show_manual(self._prefill, {})

    async def async_step_manual(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if await self._is_docker() and not str(user_input.get(CONF_LOCAL_IP) or "").strip():
                # host-networked containers derive fine; only NAT'd bridges
                # (or no route) still need the host's LAN IP typed in
                try:
                    user_input[CONF_LOCAL_IP] = await self.hass.async_add_executor_job(
                        _local_ip_for,
                        str(user_input.get(CONF_REGISTRAR_HOST, "")).strip(),
                        DEFAULT_REGISTRAR_PORT,
                    )
                except OSError:
                    user_input[CONF_LOCAL_IP] = ""
                if _looks_natted(user_input[CONF_LOCAL_IP]):
                    user_input[CONF_LOCAL_IP] = ""
                if not user_input[CONF_LOCAL_IP]:
                    errors[CONF_LOCAL_IP] = "required"
                    return await self._show_manual(user_input, errors)
            return await self._finish(user_input, errors)
        return await self._show_manual(self._prefill, errors)

    async def _finish(self, user_input: dict, errors: dict) -> ConfigFlowResult:
        data = _flatten(user_input)
        if not data[CONF_REGISTRAR_HOST]:
            errors[CONF_REGISTRAR_HOST] = "required"
            return await self._show_manual(user_input, errors)
        if not data[CONF_LOCAL_IP]:
            try:
                data[CONF_LOCAL_IP] = await self.hass.async_add_executor_job(
                    _local_ip_for, data[CONF_REGISTRAR_HOST], data[CONF_REGISTRAR_PORT]
                )
            except OSError:
                return await self._show_manual(user_input, {"base": "cannot_reach"})
        if not data[CONF_USERNAME]:
            if ext := await self.hass.async_add_executor_job(_free_extension, data[CONF_REGISTRAR_HOST]):
                data[CONF_USERNAME] = ext
            else:
                errors[CONF_USERNAME] = "cannot_detect_extension"
        if not self._flow_alive():
            return self.async_abort(reason="closed")
        if not data[CONF_DOOR_HOST]:
            if door := await self.hass.async_add_executor_job(_door_host):
                data[CONF_DOOR_HOST] = door
            else:
                errors[CONF_DOOR_HOST] = "cannot_detect_door"
        if errors:
            return await self._show_manual(user_input, errors)
        data[CONF_SERIAL] = data[CONF_SERIAL] or _serial_for(data[CONF_REGISTRAR_HOST], data[CONF_USERNAME])
        if bad := await self.hass.async_add_executor_job(
            _check_bind, data[CONF_LOCAL_IP], data[CONF_LISTEN_PORT], data[CONF_RTSP_PORT]
        ):
            return await self._show_manual(user_input, {bad: "cannot_bind"})
        if not self._flow_alive():
            return self.async_abort(reason="closed")
        statuses = await self.hass.async_add_executor_job(_sip_statuses, data)
        reg = statuses["registrar"]
        if reg is None:
            errors[CONF_REGISTRAR_HOST] = "cannot_connect"
        elif reg == 410:
            errors[CONF_USERNAME] = "extension_conflict"
        elif reg in (401, 403):
            errors[CONF_PASSWORD] = "invalid_auth"
        elif reg != 200:
            errors[CONF_REGISTRAR_HOST] = "register_rejected"
        for leg in (CONF_PROVIDER, CONF_FRITZ):
            code = statuses.get(leg, 200)
            if code is None:
                errors[leg] = "cannot_connect"
            elif code in (401, 403, 404, 407):
                errors[leg] = "invalid_auth"
            elif not 200 <= code < 300:
                errors[leg] = "register_rejected"
        if errors:
            return await self._show_manual(user_input, errors)
        await self.async_set_unique_id(f"{data[CONF_SERIAL]}@{data[CONF_REGISTRAR_HOST]}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=data[CONF_NAME] or f"VDM10 {data[CONF_USERNAME]}", data=data)

    async def _show_manual(self, user_input: dict, errors: dict) -> ConfigFlowResult:
        schema = _manual_schema(user_input, ask_local_ip=await self._container_natted())
        return self.async_show_form(step_id="manual", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return Vdm10OptionsFlow()


class Vdm10OptionsFlow(OptionsFlow):
    """re-edit every entry parameter without re-adding; saving reloads it."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(data=_flatten(user_input))
        return self.async_show_form(
            step_id="init", data_schema=_options_schema({**self.config_entry.data, **self.config_entry.options})
        )


def _options_schema(flat: dict) -> vol.Schema:
    """same field set as the manual form, defaults from the flat entry dict.
    No live probes here - the running station already holds :5060."""
    return vol.Schema(
        {
            vol.Required(CONF_REGISTRAR_HOST, default=flat.get(CONF_REGISTRAR_HOST, "")): str,
            vol.Optional(CONF_USERNAME, default=flat.get(CONF_USERNAME, "")): str,
            vol.Required(CONF_PASSWORD, default=flat.get(CONF_PASSWORD, "")): str,
            vol.Optional(CONF_DOOR_HOST, default=flat.get(CONF_DOOR_HOST, "")): str,
            vol.Optional(CONF_NAME, default=flat.get(CONF_NAME, "")): str,
            vol.Optional(CONF_ADVANCED): section(
                vol.Schema(
                    {
                        vol.Optional(
                            CONF_REGISTRAR_PORT, default=flat.get(CONF_REGISTRAR_PORT, DEFAULT_REGISTRAR_PORT)
                        ): PORT,
                        vol.Optional(CONF_LOCAL_IP, default=flat.get(CONF_LOCAL_IP, "")): str,
                        vol.Optional(CONF_SERIAL, default=flat.get(CONF_SERIAL, "")): str,
                        vol.Optional(CONF_LISTEN_PORT, default=flat.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT)): PORT,
                        vol.Optional(CONF_RTSP_PORT, default=flat.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)): PORT,
                        vol.Optional(CONF_DOOR_USER, default=flat.get(CONF_DOOR_USER, DEFAULT_DOOR_USER)): str,
                        vol.Optional(CONF_DOOR_PASSWORD, default=flat.get(CONF_DOOR_PASSWORD, "")): str,
                        vol.Optional(CONF_PRELOAD_STREAM, default=flat.get(CONF_PRELOAD_STREAM, False)): bool,
                    }
                ),
                {"collapsed": True},
            ),
            vol.Optional(CONF_PROVIDER): section(
                vol.Schema(
                    {
                        vol.Optional(CONF_SIP_HOST, default=flat.get(CONF_SIP_HOST, "")): str,
                        vol.Optional(CONF_SIP_PORT, default=flat.get(CONF_SIP_PORT, DEFAULT_SIP_PORT)): PORT,
                        vol.Optional(CONF_SIP_USER, default=flat.get(CONF_SIP_USER, "")): str,
                        vol.Optional(CONF_SIP_PASS, default=flat.get(CONF_SIP_PASS, "")): str,
                        vol.Optional(CONF_SIP_TARGET, default=flat.get(CONF_SIP_TARGET, "")): str,
                        vol.Optional(CONF_SIP_DOMAIN, default=flat.get(CONF_SIP_DOMAIN, "")): str,
                        vol.Optional(CONF_SIP_SRTP, default=flat.get(CONF_SIP_SRTP, False)): bool,
                        vol.Optional(CONF_INBOUND_TARGET, default=flat.get(CONF_INBOUND_TARGET, "")): str,
                    }
                ),
                {"collapsed": True},
            ),
            vol.Optional(CONF_FRITZ): section(
                vol.Schema(
                    {
                        vol.Optional(CONF_FRITZ_HOST, default=flat.get(CONF_FRITZ_HOST, "")): str,
                        vol.Optional(CONF_FRITZ_PORT, default=flat.get(CONF_FRITZ_PORT, DEFAULT_FRITZ_PORT)): PORT,
                        vol.Optional(CONF_FRITZ_DOMAIN, default=flat.get(CONF_FRITZ_DOMAIN, DEFAULT_FRITZ_DOMAIN)): str,
                        vol.Optional(CONF_FRITZ_USER, default=flat.get(CONF_FRITZ_USER, "")): str,
                        vol.Optional(CONF_FRITZ_PASS, default=flat.get(CONF_FRITZ_PASS, "")): str,
                        vol.Optional(CONF_FRITZ_TARGET, default=flat.get(CONF_FRITZ_TARGET, "")): str,
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


def _flatten(user_input: dict) -> dict:
    """merge the advanced/provider/fritz sections into one flat data dict"""
    adv = user_input.get(CONF_ADVANCED) or {}
    data = {
        CONF_REGISTRAR_HOST: user_input[CONF_REGISTRAR_HOST].strip(),
        CONF_USERNAME: user_input.get(CONF_USERNAME, "").strip(),
        CONF_PASSWORD: user_input[CONF_PASSWORD],
        CONF_DOOR_HOST: user_input.get(CONF_DOOR_HOST, "").strip(),
        CONF_NAME: user_input.get(CONF_NAME, ""),
        CONF_REGISTRAR_PORT: adv.get(CONF_REGISTRAR_PORT, DEFAULT_REGISTRAR_PORT),
        CONF_LOCAL_IP: user_input.get(CONF_LOCAL_IP) or adv.get(CONF_LOCAL_IP),
        CONF_SERIAL: adv.get(CONF_SERIAL),
        CONF_LISTEN_PORT: adv.get(CONF_LISTEN_PORT, DEFAULT_LISTEN_PORT),
        CONF_RTSP_PORT: adv.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
        CONF_DOOR_USER: adv.get(CONF_DOOR_USER, DEFAULT_DOOR_USER),
        CONF_DOOR_PASSWORD: adv.get(CONF_DOOR_PASSWORD) or user_input[CONF_PASSWORD],
        CONF_PRELOAD_STREAM: bool(adv.get(CONF_PRELOAD_STREAM, False)),
    }
    data.update(user_input.get(CONF_PROVIDER) or {})
    data.update(user_input.get(CONF_FRITZ) or {})
    return data


def _local_ip_for(host: str, port: int) -> str:
    """UDP-connect trick (no traffic sent) -> local IP that reaches the registrar."""
    from metzler import discovery

    return discovery.local_ip_for(host, port)
