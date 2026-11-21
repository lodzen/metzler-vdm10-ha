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

# go2rtc compiles every module into its binary; HA's managed config only
# enables a hardcoded subset (no isapi). Adding "isapi" to the module list
# makes the Hikvision two-way-audio backchannel scheme available on the
# embedded instance. Depends on HA internals - safe if the import or the
# attribute ever moves: the integration just loses talkback again.
try:
    from homeassistant.components.go2rtc import server as _go2rtc_server

    if "isapi" not in _go2rtc_server._APP_MODULES:
        _go2rtc_server._APP_MODULES = (*_go2rtc_server._APP_MODULES, "isapi")  # ty: ignore[invalid-assignment]
        _go2rtc_server._UI_APP_MODULES = (*_go2rtc_server._APP_MODULES, "debug")
except (ImportError, AttributeError):
    pass

# Amazon links a doorbell's press to its camera only when ONE endpoint carries
# both Alexa.DoorbellEventSource and a camera stream interface, and only treats
# it as a *video* doorbell when displayCategories contains CAMERA+DOORBELL
# (what Scrypted ships). HA never yields DoorbellEventSource on cameras, and
# entity_config's display_categories accepts a single value only - so a doorbell
# entity and a camera entity can never merge natively. Three patches fix that:
# our door camera is auto-detected via its unique_id and reported as
# ["CAMERA", "DOORBELL"] with the doorbell interface appended, and a press
# coming from our doorbell event entity is redirected to the camera endpoint.
try:
    from collections.abc import Iterable

    from homeassistant.components.alexa import entities as _alexa_entities
    from homeassistant.components.alexa import state_report as _alexa_state_report
    from homeassistant.components.alexa.capabilities import (
        AlexaCapability,
        AlexaDoorbellEventSource,
        AlexaRTCSessionController,
    )
    from homeassistant.components.alexa.config import AbstractConfig
    from homeassistant.components.alexa.const import CONF_DISPLAY_CATEGORIES
    from homeassistant.components.alexa.entities import (
        ENTITY_ADAPTERS,
        AlexaEntity,
        DisplayCategory,
    )
    from homeassistant.helpers import entity_registry as _er

    def _is_vdm10_door_camera(hass: HomeAssistant, entity_id: str) -> bool:
        """auto-detect our door camera by unique_id - the user should not have
        to write entity_config YAML for the integration to discover itself"""
        entry = _er.async_get(hass).async_get(entity_id)
        return bool(entry and entry.platform == DOMAIN and entry.unique_id.endswith("_door_camera"))

    def _cam_is_doorbell(self: AlexaEntity) -> bool:
        conf = self.config.entity_config.get(self.entity.entity_id, {})
        return conf.get(CONF_DISPLAY_CATEGORIES) == DisplayCategory.DOORBELL or _is_vdm10_door_camera(
            self.hass, self.entity_id
        )

    # HA reports half-duplex for EVERY camera's RTCSessionController, so
    # Amazon never opens the mic path - no talking and no door audio on the
    # Echo either. For a camera whose resolved capabilities prove two-way
    # audio (entity flag + WebRTC provider + labs feature), swap in a
    # duplex-advertising subclass instead.
    class _DuplexRTCSessionController(AlexaRTCSessionController):
        def configuration(self):
            conf = super().configuration()
            return {**(conf or {}), "isFullDuplexAudioSupported": True}

    _orig_cam_interfaces = _alexa_entities.CameraCapabilities.interfaces
    _orig_cam_display_categories = _alexa_entities.CameraCapabilities.display_categories

    def _cam_interfaces(self) -> Iterable[AlexaCapability]:
        doorbell = _cam_is_doorbell(self)
        duplex = False
        if doorbell and (entity := _alexa_entities.async_get_camera_entity(self.hass, self.entity_id)):
            duplex = entity.camera_capabilities.two_way_audio
        for iface in _orig_cam_interfaces(self):
            if duplex and isinstance(iface, AlexaRTCSessionController):
                yield _DuplexRTCSessionController(self.entity)
            else:
                yield iface
        if doorbell:
            yield AlexaDoorbellEventSource(self.entity)

    def _cam_display_categories(self) -> list[str] | None:
        if _cam_is_doorbell(self):
            return [DisplayCategory.CAMERA, DisplayCategory.DOORBELL]
        return _orig_cam_display_categories(self)

    _alexa_entities.CameraCapabilities.interfaces = _cam_interfaces  # ty: ignore[invalid-assignment]
    _alexa_entities.CameraCapabilities.display_categories = _cam_display_categories  # ty: ignore[invalid-assignment]

    _orig_send_doorbell = _alexa_state_report.async_send_doorbell_event_message

    def _door_camera_alexa_entity(
        hass: HomeAssistant, config: AbstractConfig, event_entity_id: str
    ) -> AlexaEntity | None:
        """a DoorbellPress must carry the endpointId of the device that holds
        the video - our door camera lives on the same device entry as the
        vdm10 doorbell event entity"""
        reg = _er.async_get(hass)
        entry = reg.async_get(event_entity_id)
        if entry is None or entry.platform != DOMAIN or not entry.device_id:
            return None
        cam_id = next(
            (
                e.entity_id
                for e in reg.entities.values()
                if e.device_id == entry.device_id and e.unique_id.endswith("_door_camera")
            ),
            None,
        )
        if cam_id is None or (cam_state := hass.states.get(cam_id)) is None:
            return None
        return ENTITY_ADAPTERS["camera"](hass, config, cam_state)

    async def _send_doorbell(hass: HomeAssistant, config: AbstractConfig, alexa_entity: AlexaEntity) -> None:
        if adapter := _door_camera_alexa_entity(hass, config, alexa_entity.entity_id):
            _LOGGER.debug(
                "doorbell press from %s redirected to %s",
                alexa_entity.entity_id,
                adapter.entity_id,
            )
            alexa_entity = adapter
        assert _orig_send_doorbell is not None
        await _orig_send_doorbell(hass, config, alexa_entity)

    _alexa_state_report.async_send_doorbell_event_message = _send_doorbell  # ty: ignore[invalid-assignment]

    # the direct-send path needs the YAML AlexaConfig, which HA retains only
    # inside the SmartHomeView's route-handler closure - capture it at
    # construction time instead (covers alexa setting up AFTER us; the router
    # scan below covers the reverse order)
    from homeassistant.components.alexa.smart_home import AlexaConfig as _AlexaYamlConfig

    _orig_alexa_config_init = _AlexaYamlConfig.__init__

    def _alexa_config_init(self, hass, config):
        _orig_alexa_config_init(self, hass, config)
        _ALEXA_CONFIGS[hass] = self

    _AlexaYamlConfig.__init__ = _alexa_config_init
except (ImportError, AttributeError):
    _orig_send_doorbell = None

_ALEXA_CONFIGS: dict = {}


def _alexa_smart_home_config(hass: HomeAssistant) -> AbstractConfig | None:
    """the YAML AlexaConfig object - captured on construction, or recovered
    from request_handler_factory's closure (the route handler is the wrapped
    ``handle()``, so the view is a closure cell, not ``__self__``)"""
    if _orig_send_doorbell is None:
        return None
    if (cfg := _ALEXA_CONFIGS.get(hass)) is not None:
        return cfg
    try:
        for resource in hass.http.app.router.resources():
            for route in resource:
                handler = getattr(route, "handler", None)
                for cell in getattr(handler, "__closure__", None) or ():
                    cfg = getattr(cell.cell_contents, "smart_home_config", None)
                    if cfg is not None:
                        _ALEXA_CONFIGS[hass] = cfg
                        return cfg
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return None


async def _async_fire_doorbell_press(hass: HomeAssistant, event_entity_id: str) -> None:
    """one-endpoint mode: when the doorbell event entity is NOT Alexa-exposed
    HA's state listener skips it entirely, so the press is sent straight onto
    the door camera endpoint (which carries the video). When it IS exposed
    the patched listener path above already delivers it."""
    config = _alexa_smart_home_config(hass)
    if config is None:
        _LOGGER.debug("doorbell press %s: no Alexa smart_home config reachable", event_entity_id)
        return
    if config.should_expose(event_entity_id):
        _LOGGER.debug("doorbell press %s: entity exposed, listener path owns it", event_entity_id)
        return
    if adapter := _door_camera_alexa_entity(hass, config, event_entity_id):
        _LOGGER.debug("doorbell press %s -> %s (direct)", event_entity_id, adapter.entity_id)
        assert _orig_send_doorbell is not None
        await _orig_send_doorbell(hass, config, adapter)
    else:
        _LOGGER.debug("doorbell press %s: door camera not resolvable", event_entity_id)


from .const import (  # noqa: E402
    CONF_DOOR_HOST,
    DOMAIN,
    PLATFORMS,
    SERVICE_ANSWER_CALL,
    SERVICE_HANGUP_CALL,
    SERVICE_SIMULATE_PRESS,
)
from .coordinator import Vdm10Runtime  # noqa: E402 - imports metzler: must run after `vendor` above

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
    cfg = {**entry.data, **entry.options}
    if cfg.get(CONF_DOOR_HOST):
        from .go2rtc import async_configure

        entry.async_create_background_task(hass, async_configure(hass, runtime), "vdm10_go2rtc")
    return True


async def _update_listener(hass: HomeAssistant, entry: Vdm10ConfigEntry) -> None:
    """options changed -> reload so build_options picks them up"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: Vdm10ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
        from .go2rtc import forget_stream

        forget_stream(entry.runtime_data)
        if not hass.config_entries.async_loaded_entries(DOMAIN):
            for service in (SERVICE_ANSWER_CALL, SERVICE_HANGUP_CALL, SERVICE_SIMULATE_PRESS):
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

    async def _simulate(_call: ServiceCall) -> None:
        # lets users verify the whole doorbell -> Alexa chain from Developer
        # tools without pressing the physical button (release->install->
        # restart->ring cycle is too slow for debugging)
        from homeassistant.components import event as _event_component
        from homeassistant.helpers import entity_registry as er

        from .event import Vdm10DoorbellEvent

        reg = er.async_get(hass)
        fired = False
        for reg_entry in reg.entities.values():
            if reg_entry.platform != DOMAIN or not reg_entry.unique_id.endswith("_doorbell_press"):
                continue
            component = hass.data.get(_event_component.DATA_COMPONENT)
            entity = component.get_entity(reg_entry.entity_id) if component else None
            if isinstance(entity, Vdm10DoorbellEvent):
                entity.simulate_press()
                fired = True
        if not fired:
            _LOGGER.warning("vdm10.simulate_doorbell_press: no doorbell event entity available")

    hass.services.async_register(DOMAIN, SERVICE_ANSWER_CALL, _answer)
    hass.services.async_register(DOMAIN, SERVICE_HANGUP_CALL, _hangup)
    hass.services.async_register(DOMAIN, SERVICE_SIMULATE_PRESS, _simulate)
