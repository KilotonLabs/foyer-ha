import json
import unittest
from datetime import datetime, timezone
from support import load_modules, state

core, _ = load_modules()
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
CONTROL = {"allow_control": True, "allow_guarded": False}
GUARDED = {"allow_control": True, "allow_guarded": True}


def device(entity_id, st, settings=CONTROL, **kwargs):
    return core.build_device(entity_id, st, settings, **kwargs)


def entry(area=None, device_class=None, **flags):
    return {"area": area, "device": flags.pop("device", None), "device_class": device_class, "hidden": False, "disabled": False,
            "category": False, "platform": "test", **flags}


AREAS = {"living_room": {"name": "Living room", "icon": "mdi:sofa", "level": 0},
         "bedroom": {"name": "Bedroom", "icon": None, "level": 1}, "attic": {"name": "Attic", "icon": None, "level": None},
         "kitchen": {"name": "Kitchen", "icon": None, "level": 0}}


class DeviceModelTests(unittest.TestCase):
    def test_names_drop_the_area_and_repeated_words(self):
        for name, area, expected in [("Living room Floor lamp", "Living room", "Floor lamp"),
                                     ("living room: floor lamp", "Living room", "Floor lamp"),
                                     ("Kitchenette lamp", "Kitchen", "Kitchenette lamp"), ("Living room", "Living room", "Living room"),
                                     ("Lamp Lamp", "", "Lamp"), ("Lamp lamp", "", "Lamp lamp"), ("x" * 90, "", "X" + "x" * 58 + "…")]:
            with self.subTest(name=name):
                self.assertEqual(core.clean_name(name, area), expected)
        shortened = device("light.a", state("on", "Living room Floor lamp"), area_name="Living room")
        self.assertEqual((shortened["name"], shortened["full"]), ("Floor lamp", "Living room Floor lamp"))
        self.assertNotIn("full", device("light.a", state("on", "Floor lamp"), area_name="Living room"))

    def test_kinds(self):
        for entity_id, klass, kind in [("light.a", None, "light"), ("input_boolean.a", None, "switch"), ("cover.a", "garage", "garage"),
                                       ("cover.a", "gate", "garage"), ("cover.a", "blind", "cover"), ("media_player.a", None, "media"),
                                       ("binary_sensor.a", "door", "binary"), ("alarm_control_panel.a", None, "alarm"),
                                       ("input_select.a", None, "select"), ("input_number.a", None, "number"),
                                       ("input_button.a", None, "button"), ("script.a", None, "script")]:
            with self.subTest(entity_id=entity_id, klass=klass):
                self.assertEqual(device(entity_id, state("on", device_class=klass))["kind"], kind)
        self.assertEqual(device("cover.a", state("open"), device_class="garage")["kind"], "garage")  # Class from the registry.

    def test_labels_and_values(self):
        for entity_id, st, label, value in [
            ("sensor.t", state("71.4", device_class="temperature", unit_of_measurement="°F"), "71°", "71°"),
            ("sensor.h", state("44.2", device_class="humidity", unit_of_measurement="%"), "44%", "44%"),
            ("sensor.p", state("118.4", device_class="power", unit_of_measurement="W"), "118 W", "118 W"),
            ("sensor.mode", state("eco"), "eco", "eco"), ("sensor.v", state("1.25", unit_of_measurement="V"), "1.2 V", "1.2 V"),
            ("light.a", state("on", brightness=204, supported_color_modes=["brightness"]), "On", "80%"),
            ("light.a", state("on", supported_color_modes=["onoff"]), "On", ""),
            ("light.a", state("off", supported_color_modes=["brightness"]), "Off", ""),
            ("fan.a", state("on", percentage=50), "On", "50%"),
            ("cover.a", state("open", current_position=40), "Open 40%", "40%"),
            ("cover.a", state("open", current_position=100), "Open", ""), ("cover.a", state("closing"), "Closing", ""),
            ("climate.a", state("heat", hvac_action="heating", temperature=72, current_temperature=70.2), "Heating to 72°", "70°"),
            ("climate.a", state("cool", hvac_action="cooling", temperature=68), "Cooling to 68°", ""),
            ("climate.a", state("heat", hvac_action="idle", temperature=72, current_temperature=70), "Idle, 70°", "70°"),
            ("climate.a", state("heat_cool", hvac_action="heating", target_temp_low=20.5, target_temp_high=24), "Heating to 20.5°", ""),
            ("climate.a", state("off", current_temperature=70), "Off", "70°"),
            ("media_player.a", state("playing"), "Playing", ""), ("media_player.a", state("paused"), "Paused", ""),
            ("lock.a", state("unlocked"), "Unlocked", ""), ("binary_sensor.d", state("on", device_class="door"), "Open", ""),
            ("binary_sensor.x", state("off"), "Off", ""), ("input_select.a", state("Spin cycle"), "Spin cycle", ""),
            ("alarm_control_panel.a", state("armed_away"), "Armed away", ""), ("scene.a", state("unknown"), "", ""),
            ("script.a", state("on"), "Running", ""), ("light.a", state("unavailable"), "Not reporting", ""),
            ("lock.a", None, "Not reporting", "")]:
            with self.subTest(entity_id=entity_id, raw=getattr(st, "state", None)):
                result = device(entity_id, st)
                self.assertEqual((result["label"], result["value"]), (label, value))
                self.assertLessEqual(len(result["label"]), 40)
                self.assertLessEqual(len(result["value"]), 12)

    def test_icons(self):
        for entity_id, st, icon in [
            ("light.a", state("on"), "lightbulb"), ("light.a", state("off"), "lightbulb-outline"),
            ("light.a", state("on", icon="mdi:floor-lamp"), "floor-lamp"), ("light.a", state("on", icon="hass:x"), "lightbulb"),
            ("switch.a", state("on", device_class="outlet"), "power-socket-us"), ("switch.a", state("off"), "toggle-switch-off-outline"),
            ("fan.a", state("off"), "fan-off"), ("cover.a", state("closed"), "blinds-horizontal-closed"),
            ("cover.a", state("open", device_class="garage"), "garage-open-variant"), ("lock.a", state("locked"), "lock"),
            ("lock.a", state("unlocked"), "lock-open-variant"), ("climate.a", state("heat"), "thermostat"),
            ("media_player.a", state("on", device_class="tv"), "television"), ("media_player.a", state("on"), "speaker"),
            ("binary_sensor.a", state("on", device_class="window"), "window-open-variant"),
            ("binary_sensor.a", state("off", device_class="motion"), "motion-sensor-off"),
            ("binary_sensor.a", state("on", device_class="smoke"), "smoke-detector-alert"),
            ("sensor.a", state("20", device_class="humidity"), "water-percent"), ("vacuum.a", state("docked"), "robot-vacuum"),
            ("scene.a", state("unknown"), "palette"), ("input_button.a", state("unknown"), "gesture-tap-button"),
            ("input_select.a", state("x"), "circle-medium")]:
            with self.subTest(entity_id=entity_id, st=st.attributes):
                self.assertEqual(device(entity_id, st)["icon"], icon)

    def test_severity_and_on(self):
        for entity_id, st, severity, on in [
            ("light.a", state("on"), "ok", True), ("switch.a", state("off"), "ok", False),
            ("lock.a", state("locked"), "ok", False), ("lock.a", state("jammed"), "attention", False),
            ("cover.a", state("open"), "ok", True), ("cover.a", state("open", device_class="garage"), "attention", False),
            ("cover.a", state("closed", device_class="garage"), "ok", False),
            ("binary_sensor.a", state("on", device_class="moisture"), "attention", False),
            ("binary_sensor.a", state("on", device_class="motion"), "info", False), ("sensor.a", state("23"), "info", False),
            ("media_player.a", state("playing"), "info", True), ("vacuum.a", state("cleaning"), "info", True),
            ("alarm_control_panel.a", state("triggered"), "attention", False), ("scene.a", state("unknown"), "info", False),
            ("scene.a", state("unavailable"), "unknown", False), ("lock.a", None, "unknown", False)]:
            with self.subTest(entity_id=entity_id, raw=getattr(st, "state", None)):
                result = device(entity_id, st)
                self.assertEqual((result["severity"], result["on"]), (severity, on))
        self.assertEqual(device("binary_sensor.laundry", state("on"), flagged=True)["severity"], "attention")

    def test_changed_at_is_epoch_seconds(self):
        self.assertEqual(device("lock.a", state("locked", last_changed=NOW))["changed_at"], int(NOW.timestamp()))
        self.assertEqual(device("lock.a", state("locked", last_changed=NOW.isoformat()))["changed_at"], int(NOW.timestamp()))
        self.assertIsNone(device("lock.a", None)["changed_at"])

    def test_control_blocks(self):
        for entity_id, st, settings, block in [
            ("light.a", state("on", brightness=204, supported_color_modes=["color_temp"], color_temp_kelvin=3000), CONTROL,
             {"toggle": True, "brightness": 80, "temps": ["warm", "neutral", "daylight"], "temp": "warm"}),
            ("light.a", state("off", supported_color_modes=["brightness"]), CONTROL,
             {"toggle": True, "brightness": 0, "temps": [], "temp": None}),
            ("light.a", state("on", supported_color_modes=["onoff"]), CONTROL, {"toggle": True, "brightness": None, "temps": [], "temp": None}),
            ("switch.a", state("on"), CONTROL, {"toggle": True}), ("input_boolean.a", state("off"), CONTROL, {"toggle": True}),
            ("fan.a", state("on", percentage=50, supported_features=1), CONTROL, {"toggle": True, "speed": 50}),
            ("fan.a", state("on", supported_features=0), CONTROL, {"toggle": True, "speed": None}),
            ("cover.a", state("open", current_position=40, supported_features=15), CONTROL,
             {"open": True, "close": True, "stop": True, "position": 40, "guarded": False}),
            ("cover.a", state("closed", supported_features=3), CONTROL,
             {"open": True, "close": True, "stop": False, "position": None, "guarded": False}),
            ("cover.a", state("closed", device_class="garage", supported_features=3), CONTROL, None),
            ("cover.a", state("closed", device_class="garage", supported_features=3), GUARDED,
             {"open": True, "close": True, "stop": False, "position": None, "guarded": True}),
            ("lock.a", state("locked"), CONTROL, {"lock": True, "unlock": False, "guarded": True}),
            ("lock.a", state("locked"), GUARDED, {"lock": True, "unlock": True, "guarded": True}),
            ("climate.a", state("heat", temperature=72, current_temperature=70, min_temp=45, max_temp=95, target_temp_step=1,
                                hvac_modes=["heat", "cool", "heat_cool", "off"], hvac_action="heating"), CONTROL,
             {"target": 72, "step": 1, "min": 45, "max": 95, "current": 70, "unit": "°F", "mode": "heat",
              "modes": ["heat", "cool", "heat_cool", "off"], "action": "heating"}),
            ("climate.a", state("heat_cool", target_temp_low=68, target_temp_high=75, hvac_modes=["heat_cool"]), CONTROL,
             {"target": None, "step": 1, "min": 45, "max": 95, "current": None, "unit": "°F", "mode": "heat_cool",
              "modes": ["heat_cool"], "action": ""}),
            ("media_player.a", state("playing", media_title="Song", media_artist="Band", volume_level=0.35, supported_features=36),
             CONTROL, {"playing": True, "title": "Song", "artist": "Band", "volume": 35, "next": True}),
            ("media_player.a", state("off", supported_features=0), CONTROL,
             {"playing": False, "title": "", "artist": "", "volume": None, "next": False}),
            ("scene.a", state("unknown"), CONTROL, {"run": True}), ("script.a", state("off"), CONTROL, {"run": True}),
            ("button.a", state("2026-01-01T00:00:00+00:00"), CONTROL, {"run": True}), ("input_button.a", state("unknown"), CONTROL, {"run": True}),
            ("sensor.a", state("20"), CONTROL, None), ("binary_sensor.a", state("on"), CONTROL, None),
            ("vacuum.a", state("docked"), CONTROL, None), ("alarm_control_panel.a", state("disarmed"), CONTROL, None),
            ("humidifier.a", state("on"), CONTROL, None), ("input_select.a", state("x"), CONTROL, None),
            ("input_number.a", state("4"), CONTROL, None),
            ("light.a", state("unavailable"), CONTROL, None), ("light.a", state("on"), {"allow_control": False}, None)]:
            with self.subTest(entity_id=entity_id, raw=st.state, settings=settings):
                self.assertEqual(device(entity_id, st, settings, unit="°F")["control"], block)

    def test_device_never_exposes_other_attributes(self):
        result = device("light.a", state("on", private_token="never expose", entity_picture="/api/x?token=y"))
        self.assertNotIn("never", json.dumps(result))
        self.assertNotIn("token", json.dumps(result))


class HomeTests(unittest.TestCase):
    def registry(self):
        return {"areas": AREAS, "entities": {
            "light.lamp": entry("living_room"), "sensor.temp": entry("living_room", "temperature"),
            "sensor.hum": entry("living_room", "humidity"), "sensor.energy": entry("living_room", "energy"),
            "binary_sensor.window": entry("living_room", "window"), "binary_sensor.update": entry("living_room", None),
            "light.hidden": entry("living_room", hidden=True), "light.disabled": entry("living_room", disabled=True),
            "switch.diagnostic": entry("living_room", category=True), "media_player.tv": entry("living_room", platform="foyer"),
            "light.bed": entry("bedroom"), "lock.attic": entry("attic"), "light.kitchen": entry("kitchen"),
            "camera.door": entry("living_room", device="dev1"), "binary_sensor.door_motion": entry(None, "motion", device="dev1"),
            "input_select.mode": entry(None)}}

    def states(self):
        return {"light.lamp": state("on", "Living room Lamp", last_changed=NOW), "sensor.temp": state("71.4", device_class="temperature"),
                "sensor.hum": state("44", device_class="humidity", unit_of_measurement="%"),
                "binary_sensor.window": state("on", device_class="window"), "light.bed": state("off", "Bed light"),
                "lock.attic": state("unlocked"), "light.kitchen": state("on"), "input_select.mode": state("Eco"),
                "camera.door": state("idle", "Living room Door", supported_features=2),
                "binary_sensor.door_motion": state("on", device_class="motion", last_changed=NOW),
                "scene.movie": state("unknown", "Movie night"), "weather.home": state("partlycloudy", temperature=61.4, temperature_unit="°F")}

    def settings(self, **overrides):
        return {"title": "Maple Street", "areas": ["living_room", "bedroom", "attic"], "entities": ["input_select.mode", "light.lamp"],
                "exclude": [], "cameras": ["camera.door"], "favorites": ["light.lamp", "light.kitchen"], "scenes": ["scene.movie"],
                "attention_when_on": [], "weather": "weather.home", **CONTROL, **overrides}

    def home(self, **overrides):
        return core.build_home(self.settings(**overrides), self.states().get, self.registry(), "°F", (52, 70))

    def test_area_selection_skips_hidden_disabled_diagnostic_and_own_players(self):
        ids = [d["id"] for d in self.home()["devices"]]
        self.assertEqual(ids, ["binary_sensor.window", "light.lamp", "sensor.hum", "sensor.temp", "light.bed", "lock.attic",
                               "input_select.mode", "light.kitchen"])  # A favorite shows even when its room is not chosen.
        self.assertNotIn("light.kitchen", [d["id"] for d in self.home(favorites=[])["devices"]])  # Area not selected.
        self.assertNotIn("light.kitchen", [d["id"] for d in self.home(exclude=["light.kitchen"])["devices"]])  # Hidden wins.

    def test_exclude_wins_and_extras_keep_their_area(self):
        home = self.home(exclude=["light.lamp"], entities=["light.kitchen", "media_player.tv", "person.me"])
        ids = [d["id"] for d in home["devices"]]
        self.assertNotIn("light.lamp", ids)
        self.assertNotIn("media_player.tv", ids)
        self.assertNotIn("person.me", ids)
        self.assertEqual(ids[-1], "light.kitchen")
        self.assertIn("kitchen", [a["id"] for a in home["areas"]])

    def test_device_cap(self):
        registry = {"areas": {"big": {"name": "Big", "level": 0}},
                    "entities": {f"light.l{n:03d}": entry("big") for n in range(320)}}
        home = core.build_home({"areas": ["big"], "entities": ["switch.extra"]}, {}.get, registry)
        self.assertEqual(len(home["devices"]), core.MAX_DEVICES)
        self.assertNotIn("switch.extra", [d["id"] for d in home["devices"]])

    def test_areas_ordered_by_floor_then_name_with_summaries(self):
        areas = self.home(favorites=["light.lamp"])["areas"]
        self.assertEqual([a["id"] for a in areas], ["living_room", "bedroom", "attic"])
        # A favorite from a room that was not chosen brings its room along, as extra devices do.
        self.assertEqual([a["id"] for a in self.home()["areas"]], ["kitchen", "living_room", "bedroom", "attic"])
        self.assertEqual(areas[0], {"id": "living_room", "name": "Living room", "icon": "sofa",
                                    "summary": "71°, 44% humidity, 1 light on, 1 window open"})
        self.assertEqual(areas[1]["summary"], "")

    def test_summary_counts(self):
        facts = [("light", None, "on", None)] * 2 + [("binary", "door", "on", None), ("binary", "garage_door", "on", None),
                                                     ("sensor", "temperature", "20", 20.0), ("sensor", "temperature", "23", 23.0)]
        self.assertEqual(core._summary(facts), "22°, 2 lights on, 2 doors open")
        self.assertEqual(core._summary([("climate", None, "heat", 77.0), ("light", None, "off", None)]), "77°")
        self.assertEqual(core._summary([("climate", None, "heat", 77.0), ("sensor", "temperature", "21", 21.0)]), "21°")

    def test_home_payload(self):
        home = self.home()
        self.assertEqual(home["title"], "Maple Street")
        self.assertEqual(home["weather"], {"temp": 61, "unit": "°F", "condition": "Partly cloudy", "icon": "weather-partly-cloudy",
                                           "low": 52, "high": 70})
        self.assertEqual(home["favorites"], ["light.lamp", "light.kitchen"])
        self.assertEqual(home["scenes"], [{"id": "scene.movie", "name": "Movie night", "icon": "palette", "kind": "scene"}])
        self.assertEqual(home["attention"], ["binary_sensor.window", "lock.attic"])
        self.assertEqual(home["cameras"], [{"id": "camera.door", "name": "Door", "area": "living_room", "live": True,
                                            "motion": {"active": True, "changed_at": int(NOW.timestamp())}, "available": True,
                                            "full": "Living room Door"}])
        lamp = home["devices"][1]
        self.assertEqual((lamp["name"], lamp["area"], lamp["changed_at"]), ("Lamp", "living_room", int(NOW.timestamp())))

    def test_attention_lists_unknown_first(self):
        states = self.states()
        states["light.bed"] = state("unavailable")
        states["binary_sensor.window"] = state("unavailable", device_class="window")
        home = core.build_home(self.settings(), states.get, self.registry())
        # A window that stops reporting needs a look; a light that does not is only marked on its own tile.
        self.assertEqual(home["attention"], ["binary_sensor.window", "lock.attic"])
        self.assertEqual(next(d for d in home["devices"] if d["id"] == "light.bed")["severity"], "unknown")

    def test_own_tv_is_not_shown_to_itself(self):
        registry = self.registry()
        registry["entities"]["media_player.tv"] = entry("living_room", roku="X0SERIAL")
        states = {**self.states(), "media_player.tv": state("idle", "Bedroom TV")}
        ids = lambda own: [d["id"] for d in core.build_home(self.settings(), states.get, registry, own_tv=own)["devices"]]
        self.assertIn("media_player.tv", ids("X0OTHER"))
        self.assertNotIn("media_player.tv", ids("X0SERIAL"))

    def test_library_ids_stay_inside_my_media(self):
        root = core.LIBRARY_ROOT
        self.assertEqual(core.library_id(None), root)
        self.assertEqual(core.library_id(""), root)
        self.assertEqual(core.library_id(root + "/local/Movies/a.mp4"), root + "/local/Movies/a.mp4")
        for bad in ["media-source://camera/camera.door", root + "x", root + "/local/../secrets.yaml", 42,
                    root + "/local/a" + chr(10) + "b", root + "/" + "a" * 600]:
            self.assertIsNone(core.library_id(bad), bad)

    def test_library_items(self):
        root = core.LIBRARY_ROOT + "/local"
        self.assertEqual(core.library_item(root + "/Movies", "Movies", True, False, "directory", "directory"),
                         {"id": root + "/Movies", "title": "Movies", "kind": "folder"})
        self.assertEqual(core.library_item(root + "/a.mp4", "a.mp4", False, True, "video", "video/mp4")["kind"], "video")
        self.assertEqual(core.library_item(root + "/b.mp3", "b.mp3", False, True, "music", "audio/mpeg")["kind"], "audio")
        self.assertEqual(core.library_item(root + "/c.jpg", "c.jpg", False, True, "image", "image/jpeg")["kind"], "image")
        self.assertIsNone(core.library_item(root + "/d.txt", "d.txt", False, True, "app", "text/plain"))
        self.assertIsNone(core.library_item(root + "/e.mp4", "e.mp4", False, False, "video", "video/mp4"))
        self.assertIsNone(core.library_item("media-source://camera/camera.door", "Door", False, True, "video", "video/mp4"))

    def test_restream_sources_follow_ring_mqtt_companions(self):
        sensors = [("sensor.front_door_info", {"stream_Source": "rtsp://ring-mqtt:8554/abc_live",
                                               "still_Image_URL": "https://h:8123{{ states.camera.front_door_snapshot.attributes.entity_picture }}"}),
                   ("sensor.weird", {"stream_Source": "file:///etc/passwd", "still_Image_URL": "{{ states.camera.x.attributes.y }}"}),
                   ("sensor.other", {"stream_Source": "rtsp://a/b"}),
                   ("sensor.temp", {"unit_of_measurement": "°F"})]
        self.assertEqual(core.restream_sources(sensors), {"camera.front_door_snapshot": "rtsp://ring-mqtt:8554/abc_live"})
        home = core.build_home(self.settings(), self.states().get, self.registry(), restreams={"camera.door": "rtsp://a/b"})
        self.assertTrue(home["cameras"][0]["live"])
        self.assertTrue(home["library"])
        self.assertFalse(core.build_home(self.settings(library=False), self.states().get, self.registry())["library"])

    def test_camera_motion_prefers_motion_sensor_and_ignores_restart(self):
        registry = {"areas": {}, "entities": {"camera.ring": {"device": "d1"}, "binary_sensor.ring_ding": {"device": "d1", "device_class": "occupancy"},
                                              "binary_sensor.ring_motion": {"device": "d1", "device_class": "motion"}}}
        quiet = {"camera.ring": state("idle"), "binary_sensor.ring_ding": state("on", last_changed=NOW),
                 "binary_sensor.ring_motion": state("off", last_changed=NOW)}
        # Quiet, and no motion seen since HA started: a restart or reconnect is not shown as motion.
        camera = core.build_camera("camera.ring", quiet["camera.ring"], registry, quiet.get, motion_seen={})
        self.assertEqual(camera["motion"], {"active": False, "changed_at": None})
        seen = {"binary_sensor.ring_motion": NOW.timestamp() - 300, "binary_sensor.ring_ding": NOW.timestamp()}
        camera = core.build_camera("camera.ring", quiet["camera.ring"], registry, quiet.get, motion_seen=seen)
        self.assertEqual(camera["motion"], {"active": False, "changed_at": int(NOW.timestamp()) - 300})

    def test_missing_camera_weather_and_scene(self):
        home = core.build_home(self.settings(cameras=["camera.gone"], scenes=["scene.gone"], weather="weather.gone"),
                               self.states().get, self.registry())
        self.assertEqual(home["cameras"][0]["available"], False)
        self.assertIsNone(home["cameras"][0]["motion"])
        self.assertEqual(home["scenes"], [])
        self.assertIsNone(home["weather"])

    def test_rev_is_stable_and_tracks_real_changes(self):
        first, second = self.home(), self.home()
        self.assertEqual(core.rev_of(first), core.rev_of(second))
        self.assertRegex(core.rev_of(first), r"^[0-9a-f]{16}$")
        self.assertNotIn("seconds", json.dumps(first))
        states = self.states()
        states["light.bed"] = state("on", "Bed light")
        changed = core.build_home(self.settings(), states.get, self.registry(), "°F", (52, 70))
        self.assertNotEqual(core.rev_of(changed), core.rev_of(first))

    def test_weather(self):
        self.assertEqual(core.weather_card(state("lightning-rainy", temperature=20, temperature_unit="°C"))["condition"], "Thunderstorms")
        self.assertEqual(core.weather_card(state("clear-night"))["condition"], "Clear")
        self.assertEqual(core.weather_card(state("some_new_state"))["condition"], "Some new state")
        self.assertIsNone(core.weather_card(state("unavailable")))
        self.assertEqual(core.forecast_range({"weather.a": {"forecast": [{"temperature": 70.4, "templow": 51.6}]}}, "weather.a"), (52, 70))
        for response in [None, {}, {"weather.a": {"forecast": []}}, {"weather.a": {"forecast": [{"temperature": "x"}]}}]:
            self.assertEqual(core.forecast_range(response, "weather.a"), (None, None))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.states = {
            "light.lamp": state("on", brightness=128, supported_color_modes=["color_temp"], color_temp_kelvin=2700,
                                min_color_temp_kelvin=2200, max_color_temp_kelvin=5000),
            "switch.fan": state("off"), "fan.ceiling": state("on", percentage=30, supported_features=1),
            "cover.blind": state("open", current_position=50, supported_features=15),
            "cover.garage": state("closed", device_class="garage", supported_features=3), "lock.front": state("locked"),
            "climate.hall": state("heat", temperature=70, min_temp=45, max_temp=95, target_temp_step=0.5, hvac_modes=["heat", "off"]),
            "media_player.speaker": state("playing", volume_level=0.2, supported_features=36),
            "scene.movie": state("unknown"), "script.bedtime": state("off"), "button.doorbell": state("unknown"),
            "sensor.temp": state("20"), "light.dead": state("unavailable"), "script.pill": state("off")}
        self.settings = {"entities": [e for e in self.states if e != "script.pill"], "scenes": ["script.pill"], **CONTROL}

    def call(self, entity, action, value=None, confirmed=False, **settings):
        merged = {**self.settings, **settings}
        home = core.build_home(merged, self.states.get, {"areas": {}, "entities": {}}, "°F")
        return core.control_call(home, merged, entity, action, value, confirmed, self.states.get)

    def test_allowed_calls(self):
        for args, expected in [
            (("light.lamp", "toggle"), ("light", "toggle", {"entity_id": "light.lamp"}, False)),
            (("switch.fan", "on"), ("switch", "turn_on", {"entity_id": "switch.fan"}, False)),
            (("light.lamp", "brightness", 60), ("light", "turn_on", {"entity_id": "light.lamp", "brightness_pct": 60}, False)),
            (("light.lamp", "brightness", 0), ("light", "turn_off", {"entity_id": "light.lamp"}, False)),
            (("light.lamp", "temp", "daylight"), ("light", "turn_on", {"entity_id": "light.lamp", "color_temp_kelvin": 5000}, False)),
            (("light.lamp", "temp", "warm"), ("light", "turn_on", {"entity_id": "light.lamp", "color_temp_kelvin": 2700}, False)),
            (("fan.ceiling", "speed", 75), ("fan", "set_percentage", {"entity_id": "fan.ceiling", "percentage": 75}, False)),
            (("cover.blind", "stop"), ("cover", "stop_cover", {"entity_id": "cover.blind"}, False)),
            (("cover.blind", "position", 25), ("cover", "set_cover_position", {"entity_id": "cover.blind", "position": 25}, False)),
            (("lock.front", "lock"), ("lock", "lock", {"entity_id": "lock.front"}, False)),
            (("climate.hall", "target", 71.3), ("climate", "set_temperature", {"entity_id": "climate.hall", "temperature": 71.5}, False)),
            (("climate.hall", "mode", "off"), ("climate", "set_hvac_mode", {"entity_id": "climate.hall", "hvac_mode": "off"}, False)),
            (("media_player.speaker", "play_pause"), ("media_player", "media_play_pause", {"entity_id": "media_player.speaker"}, False)),
            (("media_player.speaker", "next"), ("media_player", "media_next_track", {"entity_id": "media_player.speaker"}, False)),
            (("media_player.speaker", "volume", 40),
             ("media_player", "volume_set", {"entity_id": "media_player.speaker", "volume_level": 0.4}, False)),
            (("scene.movie", "run"), ("scene", "turn_on", {"entity_id": "scene.movie"}, False)),
            (("script.bedtime", "run"), ("script", "turn_on", {"entity_id": "script.bedtime"}, False)),
            (("button.doorbell", "run"), ("button", "press", {"entity_id": "button.doorbell"}, False)),
            (("script.pill", "run"), ("script", "turn_on", {"entity_id": "script.pill"}, False))]:
            with self.subTest(args=args):
                self.assertEqual(self.call(*args), expected)

    def test_denied(self):
        for args, settings, expected in [
            (("light.other", "toggle"), {}, "not_allowed"), (("sensor.temp", "toggle"), {}, "not_allowed"),
            (("light.lamp", "unlock"), {}, "not_allowed"), (("light.lamp", "turn_on"), {}, "not_allowed"),
            (("lock.front", "toggle"), {}, "not_allowed"), (("light.lamp", "toggle"), {"allow_control": False}, "not_allowed"),
            (("script.pill", "toggle"), {}, "not_allowed"), (("script.other", "run"), {}, "not_allowed"),
            ((["light.lamp"], "toggle"), {}, "not_allowed"), (("light.lamp", {"x": 1}), {}, "not_allowed"),
            (("media_player.speaker", "volume_mute"), {}, "not_allowed"), (("light.dead", "toggle"), {}, "unavailable")]:
            with self.subTest(args=args, settings=settings):
                self.assertEqual(self.call(*args, **settings), expected)

    def test_guarded_actions(self):
        self.assertEqual(self.call("lock.front", "unlock", confirmed=True), "not_allowed")  # Needs allow_guarded.
        self.assertEqual(self.call("lock.front", "unlock", allow_guarded=True), "not_allowed")  # Needs confirmed.
        self.assertEqual(self.call("lock.front", "unlock", confirmed="true", allow_guarded=True), "not_allowed")
        self.assertEqual(self.call("lock.front", "unlock", confirmed=True, allow_guarded=True),
                         ("lock", "unlock", {"entity_id": "lock.front"}, True))
        self.assertEqual(self.call("cover.garage", "open", confirmed=True), "not_allowed")
        self.assertEqual(self.call("cover.garage", "open", allow_guarded=True), "not_allowed")
        self.assertEqual(self.call("cover.garage", "open", confirmed=True, allow_guarded=True),
                         ("cover", "open_cover", {"entity_id": "cover.garage"}, True))
        self.assertEqual(self.call("cover.garage", "stop", confirmed=True, allow_guarded=True), "not_allowed")  # Not supported.
        self.assertEqual(self.call("cover.blind", "open")[3], False)

    def test_value_ranges(self):
        for args in [("light.lamp", "brightness", 101), ("light.lamp", "brightness", -1), ("light.lamp", "brightness", "50"),
                     ("light.lamp", "brightness", True), ("light.lamp", "brightness", 50.5), ("light.lamp", "temp", "blue"),
                     ("fan.ceiling", "speed", None), ("cover.blind", "position", 150), ("climate.hall", "target", 96),
                     ("climate.hall", "target", "72"), ("climate.hall", "target", float("nan")), ("climate.hall", "mode", "cool"),
                     ("media_player.speaker", "volume", 200)]:
            with self.subTest(args=args):
                self.assertEqual(self.call(*args), "invalid_value")
        self.assertEqual(self.call("light.lamp", "brightness", 50.0)[2]["brightness_pct"], 50)

    def test_running_script_is_busy(self):
        self.states["script.pill"] = state("on")
        self.states["script.bedtime"] = state("on")
        self.assertEqual(self.call("script.pill", "run"), "busy")
        self.assertEqual(self.call("script.bedtime", "run"), "busy")
        self.states["script.pill"] = state("unavailable")
        self.assertEqual(self.call("script.pill", "run"), "unavailable")


BASE = {"title": "Home", "areas": ["living_room"]}


class ValidationTests(unittest.TestCase):
    def test_valid_profiles(self):
        core.validate_settings(BASE)
        core.validate_settings({"entities": ["sensor.a"]})
        core.validate_settings({"cameras": ["camera.a"], "weather": "weather.home", "scenes": ["scene.a", "script.b"],
                                "favorites": ["light.a"], "exclude": ["light.b"], "attention_when_on": ["input_boolean.x"],
                                "allow_control": False, "allow_guarded": True})

    def test_settings_reject_invalid_scope_and_counts(self):
        for data, code in [({}, "nothing_selected"), ({"areas": [], "entities": [], "cameras": []}, "nothing_selected"),
                           ({**BASE, "title": " "}, "invalid_title"), ({**BASE, "title": "x" * 41}, "invalid_title"),
                           ({"areas": [f"a{n}" for n in range(51)]}, "area_count"), ({"areas": ["a", "a"]}, "invalid_area"),
                           ({"areas": ["bad/area"]}, "invalid_area"),
                           ({"entities": [f"sensor.s{n}" for n in range(101)]}, "entity_count"),
                           ({"entities": ["person.someone"]}, "invalid_entity"), ({"entities": ["sensor.a", "sensor.a"]}, "invalid_entity"),
                           ({**BASE, "exclude": [f"light.l{n}" for n in range(301)]}, "exclude_count"),
                           ({"cameras": [f"camera.c{n}" for n in range(17)]}, "camera_count"),
                           ({"cameras": ["light.a"]}, "invalid_camera"),
                           ({**BASE, "favorites": [f"light.l{n}" for n in range(13)]}, "favorite_count"),
                           ({**BASE, "scenes": [f"scene.s{n}" for n in range(13)]}, "scene_count"),
                           ({**BASE, "scenes": ["lock.front"]}, "invalid_scene"),
                           ({**BASE, "attention_when_on": ["sensor.b"]}, "invalid_attention"),
                           ({**BASE, "weather": "sensor.x"}, "invalid_weather"),
                           ({**BASE, "allow_control": "yes"}, "invalid_settings"),
                           ({**BASE, "entities": "sensor.a"}, "entity_count")]:
            with self.subTest(data=data), self.assertRaises(ValueError) as caught:
                core.validate_settings(data)
            self.assertEqual(str(caught.exception), code)

    def test_migration_from_protocol_1(self):
        old = {"title": "Den", "entities": [f"sensor.s{n}" for n in range(15)], "routines": ["script.a"],
               "attention_when_on": ["binary_sensor.x"], "renderer_url": "http://10.0.0.2:8099", "renderer_secret": "s" * 20}
        new = core.migrate_settings(old)
        self.assertEqual(new["entities"], old["entities"])
        self.assertEqual(new["favorites"], old["entities"][:12])
        self.assertEqual((new["scenes"], new["areas"], new["cameras"], new["exclude"], new["weather"]), (["script.a"], [], [], [], ""))
        self.assertEqual((new["allow_control"], new["allow_guarded"]), (True, False))
        self.assertEqual((new["renderer_url"], new["renderer_secret"], new["title"]), (old["renderer_url"], "s" * 20, "Den"))
        core.validate_settings(new)

    def test_pairing_hash_and_expiry(self):
        code, data = core.new_pairing()
        self.assertEqual(len(code), 8)
        self.assertTrue(core.matches(code, data["pair_hash"]))
        self.assertFalse(core.matches(code + "1", data["pair_hash"]))
        self.assertNotIn(code, str(data))
        self.assertRegex(core.new_match(), r"^\d{4}$")

    def test_pairing_window_starts_at_activation(self):
        code, data = core.new_pairing()
        self.assertNotIn("pair_expires", data)
        active = core.activate_pairing(data, now=1000)
        self.assertEqual(active["pair_expires"], 1000 + core.PAIR_TTL)
        self.assertEqual(active["pair_hash"], data["pair_hash"])

    def test_pending_pairings(self):
        pending = core.PendingPairings(limit=2)
        pending.add("flow1", "12345678", now=0)
        self.assertNotIn("12345678", str(pending.items))
        self.assertIsNone(pending.claim("87654321", "token", now=1))
        self.assertIsNone(pending.token_hash("flow1"))
        self.assertEqual(pending.claim("12345678", "token", now=1), "flow1")
        self.assertEqual(pending.token_hash("flow1"), core.digest("token"))
        self.assertIsNone(pending.claim("12345678", "again", now=2))  # Single use.
        pending.add("flow2", "11111111", now=0)
        self.assertIsNone(pending.claim("11111111", "late", now=core.PAIR_TTL + 1))  # Expired.
        self.assertNotIn("flow2", pending.items)
        for n in range(3, 6):
            pending.add(f"flow{n}", f"{n:08d}", now=10)
        self.assertEqual(len(pending.items), 2)  # Bounded.
        pending.discard("flow5")
        self.assertNotIn("flow5", pending.items)

    def test_one_address_cannot_exhaust_another(self):
        limit = core.AddressRateLimit(per_address=3, total=5, seconds=60, addresses=2)
        self.assertEqual([limit.allow("10.0.0.9", now=0) for _ in range(5)], [True, True, True, False, False])
        self.assertTrue(limit.allow("10.0.0.5", now=1))
        self.assertTrue(limit.allow("10.0.0.5", now=1))
        self.assertFalse(limit.allow("10.0.0.6", now=1))  # The shared cap still holds.
        self.assertTrue(limit.allow("10.0.0.6", now=61))
        self.assertLessEqual(len(limit.windows), 2)  # Memory stays bounded.

    def test_rate_limit_recovers_and_memory_is_bounded(self):
        limit = core.RateLimit(2, 60)
        self.assertTrue(limit.allow(0))
        self.assertTrue(limit.allow(1))
        for _ in range(100):
            self.assertFalse(limit.allow(2))
        self.assertEqual(len(limit.hits), 2)
        self.assertTrue(limit.allow(61))

    def test_duplicate_and_guarded_bursts(self):
        gate = core.ActionGate()
        self.assertEqual(gate.claim("request_123", now=0), "accepted")
        self.assertEqual(gate.check("request_123", now=5), "duplicate")
        self.assertEqual(gate.claim("request_456", now=0.5), "accepted")  # Ordinary actions are not spaced out.
        self.assertEqual(gate.claim("request_g1", True, now=1), "accepted")
        self.assertEqual(gate.claim("request_g2", True, now=2), "busy")
        self.assertEqual(gate.claim("request_g2", True, now=4.5), "accepted")
        self.assertEqual(gate.claim(None, now=10), "invalid_request")
        self.assertEqual(gate.check("short", now=10), "invalid_request")
        self.assertIsNone(gate.check("request_123", now=601))  # Remembered for ten minutes.

    def test_camera_width(self):
        for value, expected in [(None, 640), ("1280", 1280), ("10", 160), ("99999", 1920), ("abc", 640), ("²", 640), ("-5", 640)]:
            self.assertEqual(core.clamp_width(value), expected)


class CommandTests(unittest.TestCase):
    SETTINGS = {"cameras": ["camera.door"]}

    def test_camera_command(self):
        command = core.camera_command(self.SETTINGS, {"lock.front", "light.porch"}, "camera.door", "Someone is at the door " * 9,
                                      "Doorbell rang", 30, ["lock.front", "lock.private", "light.porch", "lock.front"])
        self.assertEqual((command["kind"], command["camera"], command["timeout"], command["back"]), ("camera", "camera.door", 30, None))
        self.assertEqual(command["actions"], ["lock.front", "light.porch"])
        self.assertLessEqual(len(command["title"]), 80)
        self.assertRegex(command["id"], core.COMMAND_ID_RE)
        many = core.camera_command(self.SETTINGS, {f"light.l{n}" for n in range(5)}, "camera.door", "T", "", 0,
                                   [f"light.l{n}" for n in range(5)])
        self.assertEqual(len(many["actions"]), 3)
        for camera, timeout in [("camera.other", 30), ("camera.door", 601), ("camera.door", -1), ("camera.door", True)]:
            with self.subTest(camera=camera, timeout=timeout), self.assertRaises(ValueError):
                core.camera_command(self.SETTINGS, set(), camera, "T", "", timeout)

    def test_notify_command(self):
        command = core.notify_command(self.SETTINGS, "Washer finished", "x" * 200)
        self.assertEqual((command["kind"], command["camera"], command["timeout"]), ("notify", None, 10))
        self.assertLessEqual(len(command["message"]), 120)
        self.assertEqual(core.notify_command(self.SETTINGS, "T", camera="camera.door")["camera"], "camera.door")
        for kwargs in [{"title": " "}, {"title": "T", "camera": "camera.other"}, {"title": "T", "timeout": 700}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                core.notify_command(self.SETTINGS, **kwargs)

    def test_camera_and_notify_are_delivered_whole_and_are_not_views(self):
        link = core.TvLink()
        view = core.new_command("view", camera="camera.door")
        link.send(view, now=0)
        link.report({"player": "view", "ack": view["id"]}, None, None, now=1)
        command = core.camera_command(self.SETTINGS, set(), "camera.door", "Door")
        link.send(command, now=2)
        self.assertEqual(link.pending(now=2)["camera"], "camera.door")
        link.report({"player": "view", "ack": command["id"]}, None, None, now=3)
        self.assertEqual(link.view["id"], view["id"])  # The picture behind the takeover stays.

    def test_return_apps_are_bounded_and_single_use(self):
        link = core.TvLink()
        for n in range(10):
            link.remember_return(f"c{n:016x}", "12", "Netflix")
        self.assertEqual(len(link.returns), 8)
        self.assertEqual(link.take_return("c0000000000000009"), ("12", "Netflix"))
        self.assertIsNone(link.take_return("c0000000000000009"))
        self.assertIsNone(link.take_return(["x"]))
        link.forget()
        self.assertEqual(len(link.returns), 0)


class EcpTests(unittest.TestCase):
    def test_device_info(self):
        xml = ("<device-info><serial-number>X00400ABCDEF</serial-number><friendly-model-name>Roku Ultra</friendly-model-name>"
               "<user-device-name>Living &amp; Room</user-device-name></device-info>")
        self.assertEqual(core.parse_device_info(xml), {"serial": "X00400ABCDEF", "name": "Living & Room"})
        self.assertEqual(core.parse_device_info(xml.replace("<user-device-name>Living &amp; Room</user-device-name>", ""))["name"],
                         "Roku Ultra")
        self.assertIsNone(core.parse_device_info("<device-info><serial-number>../x</serial-number></device-info>"))
        self.assertIsNone(core.parse_device_info("garbage"))

    def test_apps(self):
        xml = ('<apps><app id="12" type="appl" version="5.1">Netflix</app><app id="dev" type="appl" version="1.0">Foyer</app>'
               '<app id="tvinput.hdmi1" type="tvin" version="1">Game console</app></apps>')
        self.assertEqual(core.find_app(core.parse_apps(xml)), "dev")
        self.assertEqual(core.find_app(core.parse_apps('<app id="555">Foyer  for Home  Assistant</app>')), "555")
        self.assertIsNone(core.find_app(core.parse_apps('<app id="12">Netflix</app><app id="../x">Foyer</app>')))
        self.assertEqual(core.find_app(core.parse_apps('<app id="777">Foyer: Cameras &amp; Home Control</app>')), "777")
        self.assertIsNone(core.find_app(core.parse_apps('<app id="778">Foyerlike</app><app id="779">My Foyer</app>')))

    def test_active_app(self):
        self.assertEqual(core.parse_active_app('<active-app><app id="12" type="appl" version="5">Netflix</app></active-app>'),
                         ("12", "Netflix"))
        self.assertEqual(core.parse_active_app('<active-app><app id="tvinput.hdmi1" type="tvin">Game console</app></active-app>'),
                         ("tvinput.hdmi1", "Game console"))
        self.assertEqual(core.parse_active_app("<active-app><app>Roku</app></active-app>"), core.HOME_SCREEN)  # Older OS.
        self.assertEqual(core.parse_active_app('<active-app><app id="562859" type="appl" version="1.0.0">Roku Dynamic Menu</app>'
                                               '</active-app>'), core.HOME_SCREEN)
        self.assertIsNone(core.parse_active_app('<active-app><app id="../../x">Evil</app></active-app>'))
        self.assertIsNone(core.parse_active_app('<active-app><app id="a..b">Evil</app></active-app>'))


class CastTests(unittest.TestCase):
    def test_media_formats(self):
        for mime, url, expected in [
            ("application/vnd.apple.mpegurl", "/api/hls/x/master_playlist.m3u8", ("video", "hls")),
            (None, "https://radio.example/stream.mp3", ("audio", "mp3")), ("audio/mpeg", "/api/tts_proxy/a", ("audio", "mp3")),
            ("video/mp4", "/media/local/a", ("video", "mp4")), ("image/jpeg", "/api/camera_proxy/camera.door", ("image", "")),
            ("audio/ogg", "/a.ogg", ("audio", "")), (None, "https://x/unknown", ("video", ""))]:
            with self.subTest(mime=mime, url=url):
                self.assertEqual(core.media_format(mime, url), expected)

    def test_images_and_ha_media_types(self):
        for mime, url, hint, expected in [
            (None, "https://example.com/doorbell.jpg", "image", ("image", "")),
            (None, "https://example.com/snapshot.JPEG?size=large", None, ("image", "")),
            (None, "https://example.com/snapshot", "image", ("image", "")),
            (None, "https://radio.example/live", "music", ("audio", "")),
            ("application/octet-stream", "https://x/photo.png", None, ("image", "")),
            ("video/mp4", "https://x/clip.jpg", "image", ("video", "mp4")),  # An explicit MIME type wins.
            (None, "https://x/track.mp3", "image", ("audio", "mp3")),  # So does a known file suffix.
            (None, "https://x/unknown", "url", ("video", ""))]:
            with self.subTest(url=url, hint=hint):
                self.assertEqual(core.media_format(mime, url, hint), expected)

    def test_titles(self):
        self.assertEqual(core.spoken_text("media-source://tts/cloud?message=The%20washer%20has%20finished&language=en-US"),
                         "The washer has finished")
        self.assertEqual(core.spoken_text("media-source://tts/piper?message=Front+door%0Ais+open"), "Front door is open")
        self.assertIsNone(core.spoken_text("media-source://media_source/local/a.mp3"))
        self.assertIsNone(core.spoken_text("media-source://tts/cloud?language=en"))
        long = core.short_title("word " * 40)
        self.assertEqual(len(long), core.TITLE_LIMIT)
        self.assertTrue(long.endswith("\u2026"))

    def test_view_paths_cannot_escape_dashboards(self):
        self.assertEqual(core.build_view_path("dashboard-tv", "cards"), "/dashboard-tv/cards")
        self.assertEqual(core.build_view_path("lovelace", None), "/lovelace")
        for dashboard, view in [("../config", None), ("lovelace", "a/b"), ("", None), ("lovelace", "x?y")]:
            with self.subTest(dashboard=dashboard, view=view), self.assertRaises(ValueError):
                core.build_view_path(dashboard, view)

    def test_command_delivered_until_acked_then_expires(self):
        link = core.TvLink()
        command = core.new_command("play", url="/a.mp4")
        link.send(command, now=0)
        self.assertEqual(link.pending(now=10)["id"], command["id"])
        link.report({"ack": "c0000000000000000"}, "10.0.0.5", "dev", now=11)
        self.assertIsNotNone(link.pending(now=11))
        link.report({"ack": command["id"], "player": "playing"}, "10.0.0.5", "dev", now=12)
        self.assertIsNone(link.pending(now=12))
        link.send(core.new_command("pause"), now=20)
        self.assertIsNone(link.pending(now=81))

    def test_view_survives_reports_sent_before_collection(self):
        link = core.TvLink()
        command = core.new_command("view", path="/lovelace")
        link.send(command, now=0)
        link.report({"player": "idle"}, None, None, now=1)
        self.assertIsNone(link.view)  # Not on screen until the TV says so...
        self.assertEqual(link.frame_view(command["id"])["path"], "/lovelace")  # ...but its frames can be fetched.
        self.assertNotIn("path", link.pending(now=1))
        link.report({"player": "view", "ack": command["id"]}, None, None, now=2)
        self.assertEqual(link.view["path"], "/lovelace")
        link.report({"player": "idle"}, None, None, now=30)
        self.assertIsNone(link.view)

    def test_uncollected_view_never_replaces_the_one_on_screen(self):
        link = core.TvLink()
        first = core.new_command("view", path="/lovelace")
        link.send(first, now=0)
        link.report({"player": "view", "ack": first["id"]}, None, None, now=1)
        second = core.new_command("view", path="/dashboard-tv")
        link.send(second, now=2)
        self.assertIsNone(link.pending(now=63))  # The TV was offline; the second view expired.
        link.report({"player": "view", "ack": first["id"]}, None, None, now=64)
        self.assertEqual(link.view["id"], first["id"])
        self.assertIsNotNone(link.frame_view(first["id"]))
        self.assertIsNone(link.frame_view(second["id"]))
        self.assertIsNone(link.pending(now=64))  # And no stop is sent for the view still showing.

    def test_cast_photo_stays_on_screen(self):
        link = core.TvLink()
        photo = core.new_command("play", media="image", url="/media/local/a.jpg?authSig=x")
        link.send(photo, now=0)
        link.report({"player": "view", "ack": photo["id"]}, None, None, now=1)
        self.assertEqual(link.view["id"], photo["id"])
        self.assertIsNone(link.pending(now=2))  # No stop: HA knows the picture is meant to be there.
        self.assertIsNone(link.frame_view(photo["id"])["path"])

    def test_announcement_keeps_the_picture_and_reports_itself(self):
        link = core.TvLink()
        view = core.new_command("view", path="/lovelace")
        link.send(view, now=0)
        link.report({"player": "view", "ack": view["id"]}, None, None, now=1)
        announcement = core.new_command("play", media="audio", url="/api/tts_proxy/x.mp3", announce=True)
        link.send(announcement, now=2)
        link.report({"player": "playing", "ack": announcement["id"], "ann": "1"}, None, None, now=3)
        self.assertTrue(link.announcing)
        self.assertEqual(link.view["id"], view["id"])
        link.report({"player": "view", "ack": announcement["id"]}, None, None, now=9)
        self.assertFalse(link.announcing)
        self.assertEqual(link.view["id"], view["id"])
        self.assertIsNone(link.pending(now=9))

    def test_picture_acked_after_a_newer_command_is_queued_stays_on_screen(self):
        # A doorbell camera replaces a dashboard, and its announcement is queued before the TV acks the camera.
        link = core.TvLink()
        dashboard = core.new_command("view", path="/lovelace")
        link.send(dashboard, now=0)
        link.pending(now=0)
        link.report({"player": "view", "ack": dashboard["id"]}, None, None, now=1)
        camera = core.new_command("view", camera="camera.door")
        link.send(camera, now=2)
        link.pending(now=2)
        announcement = core.new_command("play", media="audio", url="/api/tts_proxy/x.mp3", announce=True)
        link.send(announcement, now=3)
        self.assertEqual(link.frame_view(camera["id"])["camera"], "camera.door")  # Its frames keep coming.
        link.report({"player": "view", "ack": camera["id"]}, None, None, now=4)
        self.assertEqual(link.view["id"], camera["id"])
        self.assertEqual(link.pending(now=4)["id"], announcement["id"])  # The announcement still goes out.
        link.report({"player": "playing", "ack": announcement["id"], "ann": "1"}, None, None, now=5)
        link.report({"player": "view", "ack": announcement["id"]}, None, None, now=12)
        self.assertEqual(link.view["id"], camera["id"])
        self.assertIsNotNone(link.frame_view(camera["id"]))
        self.assertIsNone(link.frame_view(dashboard["id"]))
        self.assertIsNone(link.pending(now=12))  # No stop for the camera on screen.

    def test_repeated_ack_never_brings_back_a_picture_the_tv_left(self):
        link = core.TvLink()
        photo = core.new_command("play", media="image", url="/media/local/a.jpg?authSig=x")
        link.send(photo, now=0)
        link.pending(now=0)
        stop = core.new_command("stop")
        link.send(stop, now=1)
        link.report({"player": "view", "ack": photo["id"]}, None, None, now=2)  # Acted on before the stop arrived.
        self.assertEqual(link.view["id"], photo["id"])
        link.pending(now=2)
        link.report({"player": "idle", "ack": stop["id"]}, None, None, now=3)
        for now in (8, 13):  # The TV repeats its last ack in every poll.
            link.report({"player": "idle", "ack": stop["id"]}, None, None, now=now)
        self.assertIsNone(link.view)
        self.assertIsNone(link.frame_view(photo["id"]))
        link.report({"player": "idle", "ack": photo["id"]}, None, None, now=18)  # A stale ack changes nothing.
        self.assertIsNone(link.view)

    def test_superseded_picture_is_committed_once(self):
        link = core.TvLink()
        photo = core.new_command("play", media="image", url="/media/local/a.jpg?authSig=x")
        link.send(photo, now=0)
        link.pending(now=0)
        link.send(core.new_command("pause"), now=1)  # Replaces the photo in the queue, then expires unsent.
        link.report({"player": "view", "ack": photo["id"]}, None, None, now=70)
        self.assertEqual(link.view["id"], photo["id"])
        self.assertIsNone(link.pending(now=70))
        for now in (75, 80):  # Back pressed on the TV, which keeps repeating the photo's ack.
            link.report({"player": "idle", "ack": photo["id"]}, None, None, now=now)
            self.assertIsNone(link.pending(now=now))
        self.assertIsNone(link.view)
        self.assertIsNone(link.frame_view(photo["id"]))

    def test_media_and_stop_clear_the_picture_once_acted_on(self):
        for kind, fields in [("play", {"media": "video", "url": "/a.mp4"}), ("stop", {})]:
            with self.subTest(kind=kind):
                link = core.TvLink()
                view = core.new_command("view", path="/lovelace")
                link.send(view, now=0)
                link.report({"player": "view", "ack": view["id"]}, None, None, now=1)
                command = core.new_command(kind, **fields)
                link.send(command, now=2)
                self.assertEqual(link.view["id"], view["id"])  # Still on screen until the TV acts.
                link.report({"player": "playing" if kind == "play" else "idle", "ack": command["id"]}, None, None, now=3)
                self.assertIsNone(link.view)

    def test_cancel_and_forget(self):
        link = core.TvLink()
        link.report({"player": "idle"}, "10.0.0.5", "dev", now=0)
        link.send(core.new_command("view", path="/lovelace"), now=1)
        link.cancel()
        self.assertIsNone(link.pending(now=2))
        self.assertIsNone(link.next_view)
        link.forget()
        self.assertEqual((link.remote, link.app_id, link.last_seen), (None, None, None))
        self.assertFalse(link.online(now=2))

    def test_report_ignores_malformed_values(self):
        link = core.TvLink()
        link.report({"player": "hacked", "pos": "-1", "dur": "9" * 20}, "10.0.0.5", "bad id!", now=0)
        self.assertEqual((link.state, link.position, link.duration, link.app_id), ("idle", None, None, None))
        self.assertTrue(link.online(now=19))
        self.assertFalse(link.online(now=21))


class CastSafetyTests(unittest.TestCase):
    def test_non_ascii_digits_ignored(self):
        link = core.TvLink()
        link.report({"pos": "²", "dur": "٣"}, None, None, now=0)
        self.assertEqual((link.position, link.duration), (None, None))

    def test_renderer_requires_secret(self):
        base = {"entities": ["sensor.a"], "renderer_url": "http://10.0.0.2:8099"}
        with self.assertRaises(ValueError):
            core.validate_settings(base)
        with self.assertRaises(ValueError):
            core.validate_settings({**base, "renderer_secret": "short"})
        core.validate_settings({**base, "renderer_secret": "x" * 16})
