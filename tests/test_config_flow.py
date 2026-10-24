"""Flow lifecycle tests with real schema validation and fake HA flow plumbing.

These cannot establish frontend rendering or compatibility with a running HA.
"""
import importlib
import sys
import time
import unittest
from types import ModuleType, SimpleNamespace

from support import fake_hass, load_modules

core, _ = load_modules()


class FlowBase:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def async_show_menu(self, **kwargs):
        return {"type": "menu", **kwargs}


class EntitySelector:
    def __init__(self, config):
        self.config = config

    def __call__(self, value):
        return value


for name in ["homeassistant.config_entries", "homeassistant.core", "homeassistant.helpers", "homeassistant.helpers.selector",
             "homeassistant.helpers.area_registry"]:
    sys.modules.setdefault(name, ModuleType(name))
AREAS = SimpleNamespace(async_list_areas=lambda: [SimpleNamespace(id="kitchen"), SimpleNamespace(id="den")])
sys.modules["homeassistant.helpers.area_registry"].async_get = lambda hass: AREAS
sys.modules["homeassistant.helpers"].area_registry = sys.modules["homeassistant.helpers.area_registry"]
sys.modules["homeassistant.config_entries"].ConfigFlow = FlowBase
sys.modules["homeassistant.config_entries"].OptionsFlow = FlowBase
sys.modules["homeassistant.core"].callback = lambda fn: fn
sys.modules["homeassistant.helpers.selector"].EntitySelector = EntitySelector
sys.modules["homeassistant.helpers.selector"].EntitySelectorConfig = dict
sys.modules["homeassistant.helpers.selector"].AreaSelector = EntitySelector
sys.modules["homeassistant.helpers.selector"].AreaSelectorConfig = dict
sys.modules["homeassistant.helpers.selector"].TextSelector = EntitySelector
sys.modules["homeassistant.helpers.selector"].TextSelectorConfig = dict
sys.modules["homeassistant.helpers.selector"].TextSelectorType = SimpleNamespace(PASSWORD="password")
flow = importlib.import_module("foyer_test.config_flow")


def new_flow():
    instance = flow.FoyerConfigFlow()
    instance.hass = fake_hass({})
    instance.hass.states.async_entity_ids = lambda domain: ["camera.b", "camera.a"]
    return instance


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_form_shows_the_whole_home(self):
        form = await new_flow().async_step_user()
        defaults = {str(key): key.default() for key in form["data_schema"].schema if callable(getattr(key, "default", None))}
        self.assertEqual((defaults["title"], defaults["areas"], defaults["cameras"]), ("Home", ["kitchen", "den"], ["camera.a", "camera.b"]))
        self.assertEqual((defaults["allow_control"], defaults["allow_guarded"]), (True, False))

    async def test_initial_pair_code_is_only_committed_on_submit(self):
        instance = new_flow()
        result = await instance.async_step_user({"title": " Lounge ", "entities": ["sensor.room"]})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "pair")
        code = result["description_placeholders"]["code"]
        created = await instance.async_step_pair({})
        self.assertEqual(created["title"], "Lounge")
        self.assertTrue(core.matches(code, created["data"]["pair_hash"]))
        self.assertNotIn(code, str(created))
        self.assertEqual(created["data"]["settings"]["scenes"], [])
        self.assertEqual(created["data"]["settings"]["allow_guarded"], False)
        self.assertEqual(created["data"]["token_hash"], "")

    async def test_form_validation_errors_and_defaults(self):
        instance = new_flow()
        for settings, error in [
            ({"title": "Room", "entities": []}, "nothing_selected"),
            ({"title": "Room", "cameras": ["camera.a"], "scenes": ["light.a"]}, "invalid_scene"),
            ({"title": " ", "entities": ["sensor.room"]}, "invalid_title"),
            ({"title": "Room", "entities": ["light.room"], "attention_when_on": ["light.room"]}, "invalid_attention"),
        ]:
            result = await instance.async_step_user(settings)
            self.assertEqual(result["errors"]["base"], error)
        schema = flow.settings_schema({})
        values = schema({"entities": ["sensor.room"]})
        self.assertEqual(values["title"], "Home")
        self.assertEqual(values["allow_guarded"], False)

    def options(self):
        instance = flow.FoyerOptionsFlow()
        instance.config_entry = SimpleNamespace(options={}, data={
            "settings": {"title": "Room", "entities": ["sensor.room"], "scenes": []},
            "token_hash": "existing-hash", "pair_hash": "", "pair_expires": 0})
        instance.hass = fake_hass({})
        return instance

    async def test_options_update_preserves_pairing(self):
        instance = self.options()
        result = await instance.async_step_settings({"title": "Updated", "entities": ["sensor.new"]})
        self.assertEqual(result["data"]["entities"], ["sensor.new"])
        self.assertEqual(instance.config_entry.data["token_hash"], "existing-hash")

    async def test_pair_options_do_not_invalidate_old_tv_before_success(self):
        instance = self.options()
        result = await instance.async_step_pair()
        code = result["description_placeholders"]["code"]
        self.assertEqual(instance.config_entry.data["pair_hash"], "")
        await instance.async_step_pair({})
        self.assertTrue(core.matches(code, instance.config_entry.data["pair_hash"]))
        self.assertEqual(instance.config_entry.data["token_hash"], "existing-hash")
        instance.hass.services.async_call.assert_not_awaited()

    def renderer_options(self):
        instance = self.options()
        instance.config_entry.options = {"title": "Room", "entities": ["sensor.room"], "scenes": [], "attention_when_on": [],
                                         "renderer_url": "http://10.0.0.2:8099", "renderer_secret": "s" * 20}
        return instance

    async def test_renderer_can_be_cleared(self):
        instance = self.renderer_options()
        # HA's frontend leaves emptied text fields out of the submission entirely.
        result = await instance.async_step_settings({"title": "Room", "entities": ["sensor.room"]})
        self.assertEqual((result["data"]["renderer_url"], result["data"]["renderer_secret"]), ("", ""))

    async def test_blank_secret_keeps_the_saved_one_only_for_the_same_renderer(self):
        instance = self.renderer_options()
        kept = await instance.async_step_settings({"title": "Room", "entities": ["sensor.room"], "renderer_url": "http://10.0.0.2:8099/"})
        self.assertEqual(kept["data"]["renderer_secret"], "s" * 20)
        moved = await instance.async_step_settings({"title": "Room", "entities": ["sensor.room"], "renderer_url": "http://10.0.0.3:8099"})
        self.assertEqual(moved["errors"]["base"], "invalid_renderer")

    async def test_saved_secret_is_never_sent_back_to_the_browser(self):
        instance = self.renderer_options()
        form = await instance.async_step_settings()
        suggested = {str(key): (key.description or {}).get("suggested_value") for key in form["data_schema"].schema}
        self.assertEqual(suggested["renderer_url"], "http://10.0.0.2:8099")
        self.assertIsNone(suggested["renderer_secret"])
        defaults = [key.default() for key in form["data_schema"].schema if callable(getattr(key, "default", None))]
        self.assertTrue(defaults)
        self.assertNotIn("s" * 20, str(defaults))

    async def test_pairing_window_starts_when_the_code_is_activated(self):
        instance = self.options()
        await instance.async_step_pair()
        before = time.time()
        await instance.async_step_pair({})
        self.assertGreaterEqual(instance.config_entry.data["pair_expires"], before + core.PAIR_TTL)

    async def test_revoke_forgets_the_tv_address(self):
        instance = self.options()
        instance.config_entry.entry_id = "room"
        instance.config_entry.data.update(tv_host="10.0.0.5", tv_app="dev")
        unpaired = []
        instance.hass.data["foyer"] = SimpleNamespace(unpair=unpaired.append)
        await instance.async_step_revoke({})
        self.assertNotIn("tv_host", instance.config_entry.data)
        self.assertNotIn("tv_app", instance.config_entry.data)
        self.assertEqual(unpaired, ["room"])

    async def test_revoke_requires_submit_and_preserves_checks(self):
        instance = self.options()
        await instance.async_step_revoke()
        self.assertEqual(instance.config_entry.data["token_hash"], "existing-hash")
        result = await instance.async_step_revoke({})
        self.assertEqual(instance.config_entry.data["token_hash"], "")
        self.assertEqual(instance.config_entry.data["pair_expires"], 0)
        self.assertEqual(result["data"]["entities"], ["sensor.room"])
        instance.hass.services.async_call.assert_not_awaited()
