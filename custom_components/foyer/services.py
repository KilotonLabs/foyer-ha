"""Entity services for Foyer TVs."""
import voluptuous as vol

from homeassistant.components.media_player import DOMAIN as MEDIA_PLAYER_DOMAIN
from homeassistant.helpers import config_validation as cv, service

from .core import DOMAIN

SHOW_VIEW_SCHEMA = {
    vol.Required("dashboard_path", default="lovelace"): cv.string,
    vol.Optional("view_path"): cv.string,
    vol.Optional("refresh_seconds", default=3): vol.All(vol.Coerce(int), vol.Range(min=2, max=60)),
}
TIMEOUT = vol.All(vol.Coerce(int), vol.Range(min=0, max=600))
SHOW_CAMERA_SCHEMA = {
    vol.Required("camera"): cv.entity_domain("camera"),
    vol.Optional("title"): cv.string,
    vol.Optional("message", default=""): cv.string,
    vol.Optional("timeout", default=30): TIMEOUT,
    vol.Optional("actions", default=[]): cv.entity_ids,
    vol.Optional("return_to_app", default=True): cv.boolean,
}
NOTIFY_SCHEMA = {
    vol.Required("title"): cv.string,
    vol.Optional("message", default=""): cv.string,
    vol.Optional("camera"): cv.entity_domain("camera"),
    vol.Optional("timeout", default=10): TIMEOUT,
    vol.Optional("launch", default=False): cv.boolean,
}


def async_setup_services(hass):
    for name, schema, func in [("show_view", SHOW_VIEW_SCHEMA, "async_show_view"),
                               ("show_camera", SHOW_CAMERA_SCHEMA, "async_show_camera"),
                               ("notify", NOTIFY_SCHEMA, "async_notify")]:
        service.async_register_platform_entity_service(
            hass, DOMAIN, name, entity_domain=MEDIA_PLAYER_DOMAIN, schema=schema, func=func)
