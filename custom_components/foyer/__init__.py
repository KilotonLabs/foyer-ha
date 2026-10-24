"""Foyer: expose a deliberately limited TV view of Home Assistant."""
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.helpers import config_validation as cv

from .core import DOMAIN, migrate_settings
from .api import FoyerView, Runtime
from .media import MediaView
from .services import async_setup_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [Platform.MEDIA_PLAYER]


async def async_setup(hass, config):
    runtime = Runtime(hass)
    hass.data[DOMAIN] = runtime
    runtime.watch_motion()
    hass.http.register_view(FoyerView(runtime))
    hass.http.register_view(MediaView(runtime.media))
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, runtime.media.shutdown)
    async_setup_services(hass)
    return True


async def async_migrate_entry(hass, entry):
    if entry.version > 2:
        return False  # Written by a newer Foyer; never guess at its format.
    if entry.version == 1:
        data = {**entry.data, "settings": migrate_settings(entry.data.get("settings", {}))}
        options = migrate_settings(entry.options) if entry.options else {}
        hass.config_entries.async_update_entry(entry, data=data, options=options, version=2)
    return True


async def async_setup_entry(hass, entry):
    runtime = hass.data[DOMAIN]
    runtime.entries[entry.entry_id] = entry
    link = runtime.link(entry.entry_id)
    link.remote, link.app_id = entry.data.get("tv_host"), entry.data.get("tv_app")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    runtime = hass.data[DOMAIN]
    for registry in (runtime.entries, runtime.gates, runtime.tv_limits, runtime.frame_limits, runtime.camera_limits,
                     runtime.control_limits, runtime.links):
        registry.pop(entry.entry_id, None)
    return True
