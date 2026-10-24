"""Configure each TV from HA's UI, including pairing and immediate revocation."""
import asyncio
import logging
from urllib.parse import urlencode, urlsplit

try:
    import probatio as vol
except ImportError:  # HA versions before the schema-library transition.
    import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .core import (
    DOMAIN, LIMITS, PAIR_TTL, SUPPORTED, activate_pairing, find_app, new_match, new_pairing, parse_apps, parse_device_info,
    validate_settings,
)

_LOGGER = logging.getLogger(__name__)
LISTS = ["areas", "entities", "exclude", "cameras", "favorites", "scenes", "attention_when_on"]


def _entities(domain, multiple=True):
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain, multiple=multiple))


def form_defaults(hass):
    """A first form that shows the whole home: every area and every camera, up to the limits."""
    from homeassistant.helpers import area_registry as ar
    areas = [area.id for area in ar.async_get(hass).async_list_areas()][:LIMITS["areas"]]
    return {"title": hass.config.location_name or "Home", "areas": areas,
            "cameras": sorted(hass.states.async_entity_ids("camera"))[:LIMITS["cameras"]]}


def settings_schema(defaults, typed_secret=None):
    """Renderer and weather fields are suggestions, never defaults, so clearing them sticks. The saved secret is never
    sent back to the browser; only a secret typed into this same form is offered again after an error."""
    supported = sorted(SUPPORTED)
    return vol.Schema({
        vol.Required("title", default=defaults.get("title", "Home")): str,
        vol.Optional("areas", default=defaults.get("areas", [])): selector.AreaSelector(selector.AreaSelectorConfig(multiple=True)),
        vol.Optional("entities", default=defaults.get("entities", [])): _entities(supported),
        vol.Optional("exclude", default=defaults.get("exclude", [])): _entities(supported),
        vol.Optional("cameras", default=defaults.get("cameras", [])): _entities("camera"),
        vol.Optional("favorites", default=defaults.get("favorites", [])): _entities(supported),
        vol.Optional("scenes", default=defaults.get("scenes", [])): _entities(["scene", "script"]),
        vol.Optional("attention_when_on", default=defaults.get("attention_when_on", [])): _entities(["binary_sensor", "input_boolean"]),
        vol.Optional("weather", description={"suggested_value": defaults.get("weather") or None}): _entities("weather", False),
        vol.Required("allow_control", default=defaults.get("allow_control", True)): bool,
        vol.Required("allow_guarded", default=defaults.get("allow_guarded", False)): bool,
        vol.Required("library", default=defaults.get("library", True)): bool,
        vol.Required("convert", default=defaults.get("convert", False)): bool,
        vol.Optional("renderer_url", description={"suggested_value": defaults.get("renderer_url") or None}): str,
        vol.Optional("renderer_secret", description={"suggested_value": typed_secret or None}): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
    })


def clean_settings(data, current=None):
    renderer = str(data.get("renderer_url") or "").strip().rstrip("/")
    secret = str(data.get("renderer_secret") or "")
    if renderer and not secret and current and renderer == current.get("renderer_url"):
        secret = current.get("renderer_secret", "")  # A blank secret keeps the saved one for the same renderer.
    result = {"title": str(data.get("title", "")).strip(), **{key: list(data.get(key) or []) for key in LISTS},
              "weather": str(data.get("weather") or ""), "allow_control": data.get("allow_control", True),
              "allow_guarded": data.get("allow_guarded", False), "library": data.get("library", True),
              "convert": data.get("convert", False), "renderer_url": renderer,
              "renderer_secret": secret if renderer else ""}
    validate_settings(result)
    return result


class FoyerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    _discovered = None
    _link_task = None

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                self._settings = clean_settings(user_input)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                if self._discovered:
                    return await self.async_step_link()
                self._code, self._pairing = new_pairing()
                return await self.async_step_pair()
        typed = (user_input or {}).get("renderer_secret")
        return self.async_show_form(step_id="user", data_schema=settings_schema(user_input or form_defaults(self.hass), typed),
                                    errors=errors)

    async def async_step_pair(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title=self._settings["title"], data={
                "settings": self._settings, "token_hash": "", **activate_pairing(self._pairing)})
        return self.async_show_form(step_id="pair", data_schema=vol.Schema({}),
                                    description_placeholders={"code": self._code})

    async def async_step_ssdp(self, discovery_info):
        from .api import ecp, lan_host
        host = lan_host(urlsplit(discovery_info.ssdp_location or "").hostname, loopback=False)
        if host is None:
            return self.async_abort(reason="cannot_connect")
        info_status, info = await ecp(self.hass, host, "/query/device-info", "get")
        apps_status, apps = await ecp(self.hass, host, "/query/apps", "get")
        device = parse_device_info(info.decode("utf-8", "replace")) if info_status == 200 else None
        if device is None or apps_status != 200:
            return self.async_abort(reason="cannot_connect")
        await self.async_set_unique_id(device["serial"])
        self._abort_if_unique_id_configured()
        app_id = find_app(parse_apps(apps.decode("utf-8", "replace")))
        if app_id is None:
            return self.async_abort(reason="app_not_installed")
        self._discovered = {"host": host, "app": app_id, "name": device["name"]}
        self.context["title_placeholders"] = {"name": device["name"]}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input=None):
        if user_input is not None:
            return await self.async_step_user()
        return self.async_show_form(step_id="discovery_confirm", data_schema=vol.Schema({}),
                                    description_placeholders={"name": self._discovered["name"]})

    async def _launch(self):
        """Open the app on the TV with a one-time code; the TV pairs by itself once its owner confirms the number."""
        from homeassistant.helpers.network import NoURLAvailableError, get_url
        from homeassistant.setup import async_setup_component
        from .api import ecp
        try:
            server = get_url(self.hass, allow_external=False, allow_cloud=False, prefer_external=False)
        except NoURLAvailableError:
            return "no_url"
        # The first TV of a fresh install: the pairing endpoint must exist before the TV calls it.
        if not await async_setup_component(self.hass, DOMAIN, {}):
            return "launch_failed"
        runtime = self.hass.data[DOMAIN]
        code, self._match = new_pairing()[0], new_match()
        runtime.pending.add(self.flow_id, code)
        query = urlencode({"contentId": "pair", "mediaType": "special", "server": server, "code": code, "match": self._match})
        status, _ = await ecp(self.hass, self._discovered["host"], f"/launch/{self._discovered['app']}?{query}")
        if status not in (200, 204):
            runtime.pending.discard(self.flow_id)
            _LOGGER.warning("Could not open Foyer on %s: the TV answered %s", self._discovered["name"], status or "nothing")
            return "launch_failed"
        return None

    async def _wait_for_tv(self):
        runtime = self.hass.data[DOMAIN]
        event = runtime.pair_events.setdefault(self.flow_id, asyncio.Event())
        try:
            async with asyncio.timeout(PAIR_TTL):
                await event.wait()
        except TimeoutError:
            return None
        return runtime.pending.token_hash(self.flow_id)

    async def async_step_link(self, user_input=None):
        if self._link_task is None:
            error = await self._launch()
            if error:
                return self.async_show_form(step_id="link", data_schema=vol.Schema({}), errors={"base": error},
                                            description_placeholders={"name": self._discovered["name"]})
            self._link_task = self.hass.async_create_task(self._wait_for_tv())
        if not self._link_task.done():
            return self.async_show_progress(step_id="link", progress_action="wait_for_tv", progress_task=self._link_task,
                                            description_placeholders={"match": self._match, "name": self._discovered["name"]})
        self._token_hash = self._link_task.result()
        return self.async_show_progress_done(next_step_id="finish" if self._token_hash else "pair_timeout")

    async def async_step_finish(self, user_input=None):
        self._abort_if_unique_id_configured()
        found = self._discovered
        return self.async_create_entry(title=found["name"], data={
            "settings": self._settings, "token_hash": self._token_hash, "pair_hash": "", "pair_expires": 0,
            "tv_host": found["host"], "tv_app": found["app"]})

    async def async_step_pair_timeout(self, user_input=None):
        return self.async_abort(reason="pair_timeout")

    @callback
    def async_remove(self):
        runtime = self.hass.data.get(DOMAIN)
        if runtime is not None:
            runtime.pending.discard(self.flow_id)
            runtime.pair_events.pop(self.flow_id, None)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return FoyerOptionsFlow()


class FoyerOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["settings", "pair", "revoke"])

    def _current(self):
        return self.config_entry.options or self.config_entry.data["settings"]

    async def async_step_settings(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                settings = clean_settings(user_input, self._current())
            except ValueError as err:
                errors["base"] = str(err)
            else:
                return self.async_create_entry(title="", data=settings)
        typed = (user_input or {}).get("renderer_secret")
        return self.async_show_form(step_id="settings", data_schema=settings_schema(user_input or self._current(), typed),
                                    errors=errors)

    async def async_step_pair(self, user_input=None):
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, **activate_pairing(self._pairing)})
            return self.async_create_entry(title="", data=dict(self._current()))
        self._code, self._pairing = new_pairing()
        return self.async_show_form(step_id="pair", data_schema=vol.Schema({}), description_placeholders={"code": self._code})

    async def async_step_revoke(self, user_input=None):
        if user_input is not None:
            # The TV's address goes too, so nothing can launch the app on a TV that no longer has access.
            data = {key: value for key, value in self.config_entry.data.items() if key not in {"tv_host", "tv_app"}}
            self.hass.config_entries.async_update_entry(self.config_entry, data={
                **data, "token_hash": "", "pair_hash": "", "pair_expires": 0})
            runtime = self.hass.data.get(DOMAIN)
            if runtime is not None:
                runtime.unpair(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=dict(self._current()))
        return self.async_show_form(step_id="revoke", data_schema=vol.Schema({}))
