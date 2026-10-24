"""Protocol 2 through a real Home Assistant runtime: registries, controls, cameras, takeovers, migration and discovery."""
import asyncio
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import EntityCategory
from homeassistant.core import SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, floor_registry as fr
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.foyer.core import digest

ENTITY = "media_player.living_room"
CODE = "00123456"
ECP = "http://127.0.0.1:8060"
SETTINGS = {"title": "Maple Street", "areas": ["living_room"], "entities": ["climate.hall", "lock.front", "light.kitchen"],
            "exclude": [], "cameras": ["camera.door"], "favorites": ["light.floor"], "scenes": ["scene.movie"],
            "attention_when_on": [], "weather": "", "allow_control": True, "allow_guarded": False,
            "renderer_url": "", "renderer_secret": ""}


def registry_entity(hass, entity_id, area=None, **kwargs):
    domain, object_id = entity_id.split(".")
    entry = er.async_get(hass).async_get_or_create(domain, "test", object_id, suggested_object_id=object_id, **kwargs)
    assert entry.entity_id == entity_id
    if area:
        er.async_get(hass).async_update_entity(entity_id, area_id=area)
    return entry


@pytest.fixture
async def home(hass):
    floor = fr.async_get(hass).async_create("Ground", level=0)
    ar.async_get(hass).async_create("Living room", floor_id=floor.floor_id, icon="mdi:sofa")
    ar.async_get(hass).async_create("Porch")
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    camera_device = dr.async_get(hass).async_get_or_create(config_entry_id=source.entry_id, identifiers={("test", "doorbell")})
    dr.async_get(hass).async_update_device(camera_device.id, area_id="porch")
    registry_entity(hass, "light.floor", "living_room")
    registry_entity(hass, "light.hidden", "living_room", hidden_by=er.RegistryEntryHider.USER)
    registry_entity(hass, "light.disabled", "living_room", disabled_by=er.RegistryEntryDisabler.USER)
    registry_entity(hass, "switch.firmware", "living_room", entity_category=EntityCategory.CONFIG)
    registry_entity(hass, "sensor.temperature", "living_room", original_device_class="temperature")
    registry_entity(hass, "sensor.energy", "living_room", original_device_class="energy")
    registry_entity(hass, "switch.outlet", "living_room")
    registry_entity(hass, "camera.door", device_id=camera_device.id)
    registry_entity(hass, "binary_sensor.door_motion", device_id=camera_device.id, original_device_class="motion")
    hass.states.async_set("light.floor", "on", {"friendly_name": "Living room Floor lamp", "brightness": 204,
                                                "supported_color_modes": ["brightness"]})
    hass.states.async_set("light.hidden", "on")
    hass.states.async_set("switch.firmware", "on")
    hass.states.async_set("sensor.temperature", "71.4", {"device_class": "temperature", "unit_of_measurement": "°F"})
    hass.states.async_set("sensor.energy", "12", {"device_class": "energy"})
    hass.states.async_set("switch.outlet", "off", {"device_class": "outlet"})
    hass.states.async_set("light.kitchen", "off", {"friendly_name": "Kitchen light"})
    hass.states.async_set("lock.front", "locked", {"friendly_name": "Front door"})
    hass.states.async_set("climate.hall", "heat", {"temperature": 70, "current_temperature": 69, "min_temp": 45, "max_temp": 95,
                                                   "target_temp_step": 1, "hvac_modes": ["heat", "off"]})
    hass.states.async_set("camera.door", "idle", {"friendly_name": "Porch Door", "supported_features": 0})
    hass.states.async_set("binary_sensor.door_motion", "off", {"device_class": "motion"})
    hass.states.async_set("scene.movie", "unknown", {"friendly_name": "Movie night"})


async def setup_entry(hass, settings=SETTINGS, version=2, options=None):
    entry = MockConfigEntry(domain="foyer", title="Living room", version=version, options=options or {}, data={
        "settings": settings, "token_hash": "", "pair_hash": digest(CODE), "pair_expires": time.time() + 600})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def tv(hass, hass_client_no_auth, home):
    entry = await setup_entry(hass)
    client = await hass_client_no_auth()
    response = await client.post("/api/foyer/pair", json={"code": CODE})
    headers = {"Authorization": "Bearer " + (await response.json())["token"], "X-Foyer-App": "dev"}

    async def poll(**query):
        result = await client.get("/api/foyer/status", headers=headers, params=query)
        assert result.status == 200
        return await result.json()

    async def post(operation, data):
        return await client.post(f"/api/foyer/{operation}", headers=headers, json=data)

    poll.client, poll.headers, poll.post, poll.entry = client, headers, post, entry
    return poll


async def test_status_home_payload_and_rev_omission(hass, tv):
    data = await tv(player="idle")
    assert data["protocol"] == 2 and data["poll_seconds"] == 3 and abs(data["now"] - time.time()) < 5
    home = data["home"]
    assert home["title"] == "Maple Street" and home["weather"] is None
    floor = next(device for device in home["devices"] if device["id"] == "light.floor")
    assert (floor["name"], floor["area"], floor["value"], floor["control"]["brightness"]) == ("Floor lamp", "living_room", "80%", 80)
    assert home["areas"][0] == {"id": "living_room", "name": "Living room", "icon": "sofa", "summary": "71°, 1 light on"}
    assert home["favorites"] == ["light.floor"]
    assert home["scenes"] == [{"id": "scene.movie", "name": "Movie night", "icon": "palette", "kind": "scene"}]
    assert home["cameras"] == [{"id": "camera.door", "name": "Door", "full": "Porch Door", "area": "porch", "live": False,
                                "available": True, "motion": {"active": False, "changed_at": None}}]  # Quiet since HA started.
    assert "porch" in [area["id"] for area in home["areas"]]
    again = await tv(player="idle", rev=data["rev"])
    assert again["rev"] == data["rev"] and "home" not in again
    hass.states.async_set("lock.front", "unlocked", {"friendly_name": "Front door"})
    changed = await tv(player="idle", rev=data["rev"])
    assert changed["rev"] != data["rev"] and changed["home"]["attention"] == ["lock.front"]
    # Motion that really happened is remembered after the sensor goes quiet again.
    hass.states.async_set("binary_sensor.door_motion", "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.door_motion", "off", {"device_class": "motion"})
    await hass.async_block_till_done()
    camera = (await tv(player="idle"))["home"]["cameras"][0]
    assert camera["motion"]["active"] is False and abs(camera["motion"]["changed_at"] - time.time()) < 5


async def test_area_inclusion_follows_the_registry(hass, tv):
    er.async_get(hass).async_update_entity(ENTITY, area_id="living_room")  # The TV's own player never shows on itself.
    ids = [device["id"] for device in (await tv())["home"]["devices"]]
    assert ids[:3] == ["light.floor", "sensor.temperature", "switch.outlet"]
    for left_out in ("light.hidden", "light.disabled", "switch.firmware", "sensor.energy", ENTITY):
        assert left_out not in ids
    assert ids[3:] == ["climate.hall", "lock.front", "light.kitchen"]
    hass.config_entries.async_update_entry(tv.entry, options={**SETTINGS, "exclude": ["switch.outlet"]})
    assert "switch.outlet" not in [device["id"] for device in (await tv())["home"]["devices"]]


async def test_weather_forecast_is_cached_in_the_background(hass, tv):
    hass.states.async_set("weather.home", "partlycloudy", {"temperature": 61.4, "temperature_unit": "°F"})
    calls = async_mock_service(hass, "weather", "get_forecasts", response={"weather.home": {"forecast": [
        {"temperature": 70, "templow": 52}]}}, supports_response=SupportsResponse.ONLY)
    hass.config_entries.async_update_entry(tv.entry, options={**SETTINGS, "weather": "weather.home"})
    first = (await tv())["home"]["weather"]
    assert (first["condition"], first["temp"], first["low"]) == ("Partly cloudy", 61, None)  # Never awaited in the request.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert (await tv())["home"]["weather"]["high"] == 70
    await tv()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls) == 1  # At most every 30 minutes.


async def test_camera_stills(hass, tv):
    assert (await tv.client.get("/api/foyer/camera", params={"entity": "camera.door"})).status == 401
    missing = await tv.client.get("/api/foyer/camera", headers=tv.headers, params={"entity": "camera.other"})
    assert missing.status == 404 and (await missing.json())["error"] == "not_found"
    image = SimpleNamespace(content=b"jpeg-still", content_type="image/jpeg")
    with patch("homeassistant.components.camera.async_get_image", AsyncMock(return_value=image)) as get_image:
        response = await tv.client.get("/api/foyer/camera", headers=tv.headers,
                                       params={"entity": "camera.door", "width": "800", "t": "5"})
        assert response.status == 200 and await response.read() == b"jpeg-still"
        assert response.headers["Cache-Control"] == "no-store"
    assert get_image.await_args.args[1] == "camera.door" and get_image.await_args.kwargs == {"timeout": 10, "width": 800}
    failed = await tv.client.get("/api/foyer/camera", headers=tv.headers, params={"entity": "camera.door"})
    assert failed.status == 502  # No camera integration here: HA's error stays in HA.
    assert (await failed.json()) == {"error": "frame_unavailable"}


async def test_stream_needs_a_streaming_camera(hass, tv):
    response = await tv.post("stream", {"camera": "camera.door"})
    assert response.status == 409 and (await response.json())["error"] == "no_stream"
    hass.states.async_set("camera.door", "idle", {"supported_features": 2})
    response = await tv.post("stream", {"camera": "camera.door"})
    assert response.status == 409  # Camera integration not set up: the stream cannot start.
    with patch("homeassistant.components.camera.async_request_stream",
               AsyncMock(return_value="/api/hls/abc/master_playlist.m3u8")) as request_stream:
        response = await tv.post("stream", {"camera": "camera.door"})
    assert await response.json() == {"protocol": 2, "url": "/api/hls/abc/playlist.m3u8", "format": "hls"}
    assert request_stream.await_args.args[1:] == ("camera.door", "hls")
    assert (await tv.post("stream", {"camera": "camera.other"})).status == 404


async def control(tv, entity, action, value=None, confirmed=False, request_id=None):
    data = {"entity": entity, "action": action, "value": value, "confirmed": confirmed,
            "request_id": request_id or f"req-{time.monotonic_ns()}"}
    response = await tv.post("control", data)
    return response.status, await response.json()


async def test_controls(hass, tv):
    light = async_mock_service(hass, "light", "turn_on")
    toggle = async_mock_service(hass, "switch", "toggle")
    climate = async_mock_service(hass, "climate", "set_temperature")
    scene = async_mock_service(hass, "scene", "turn_on")
    assert await control(tv, "light.floor", "brightness", 60) == (200, {"protocol": 2, "result": "accepted"})
    assert light[0].data == {"entity_id": "light.floor", "brightness_pct": 60}
    assert (await control(tv, "switch.outlet", "toggle"))[0] == 200 and toggle[0].data == {"entity_id": "switch.outlet"}
    assert (await control(tv, "climate.hall", "target", 72))[0] == 200 and climate[0].data["temperature"] == 72
    assert (await control(tv, "climate.hall", "target", 120)) == (409, {"error": "invalid_value"})
    assert (await control(tv, "scene.movie", "run"))[0] == 200 and scene[0].data == {"entity_id": "scene.movie"}
    for entity, action in [("light.hidden", "toggle"), ("switch.firmware", "toggle"), ("sensor.temperature", "toggle"),
                           (ENTITY, "play_pause"), ("light.floor", "unlock")]:
        assert await control(tv, entity, action) == (403, {"error": "not_allowed"})
    assert len(light) == len(toggle) == 1


async def test_unlock_needs_permission_and_confirmation(hass, tv):
    unlock = async_mock_service(hass, "lock", "unlock")
    assert await control(tv, "lock.front", "unlock", confirmed=True) == (403, {"error": "not_allowed"})
    hass.config_entries.async_update_entry(tv.entry, options={**SETTINGS, "allow_guarded": True})
    assert await control(tv, "lock.front", "unlock") == (403, {"error": "not_allowed"})
    assert not unlock
    assert (await control(tv, "lock.front", "unlock", confirmed=True))[0] == 200
    assert len(unlock) == 1


async def test_duplicate_request_runs_once(hass, tv):
    calls = async_mock_service(hass, "light", "toggle")
    assert (await control(tv, "light.floor", "toggle", request_id="same-request-1"))[1]["result"] == "accepted"
    assert (await control(tv, "light.floor", "toggle", request_id="same-request-1"))[1]["result"] == "already_requested"
    assert len(calls) == 1


async def test_service_failure_is_opaque(hass, tv):
    assert await control(tv, "light.floor", "toggle") == (502, {"error": "action_failed"})  # No light integration loaded.


ACTIVE = f"{ECP}/query/active-app"
NETFLIX = '<active-app><app id="12" type="appl" version="5.1">Netflix</app></active-app>'


async def test_show_camera_takes_over_and_returns_to_the_app(hass, tv, aioclient_mock):
    aioclient_mock.get(ACTIVE, text=NETFLIX)
    aioclient_mock.post(f"{ECP}/launch/dev", status=200)
    aioclient_mock.post(f"{ECP}/launch/12", status=200)
    await tv(player="idle")
    link = hass.data["foyer"].link(tv.entry.entry_id)
    link.last_seen -= 30  # Netflix is in front; the app is closed.
    await hass.services.async_call("foyer", "show_camera", {
        "entity_id": ENTITY, "camera": "camera.door", "message": "Doorbell rang",
        "actions": ["lock.front", "light.hidden", "light.floor"]}, blocking=True)
    await hass.async_block_till_done()
    command = (await tv(player="idle"))["command"]
    assert command["kind"] == "camera" and command["camera"] == "camera.door"
    assert (command["title"], command["message"], command["timeout"], command["back"]) == ("Porch Door", "Doorbell rang", 30, "Netflix")
    assert command["actions"] == ["lock.front", "light.floor"]
    assert f"{ECP}/launch/dev?contentId=cast&mediaType=special" in [str(call[1]) for call in aioclient_mock.mock_calls]
    await tv(player="idle", ack=command["id"])
    response = await tv.post("return", {"command": command["id"]})
    assert await response.json() == {"protocol": 2, "result": "accepted"}
    assert str(aioclient_mock.mock_calls[-1][1]) == f"{ECP}/launch/12"
    assert (await tv.post("return", {"command": command["id"]})).status == 404


async def test_show_camera_rules(hass, tv, aioclient_mock):
    aioclient_mock.post(f"{ECP}/input", status=200)
    aioclient_mock.post(f"{ECP}/launch/dev", status=200)
    await tv(player="idle")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("foyer", "show_camera", {"entity_id": ENTITY, "camera": "camera.other"}, blocking=True)
    with patch("custom_components.foyer.api.WAKE_GRACE", 0):
        await hass.services.async_call("foyer", "show_camera", {"entity_id": ENTITY, "camera": "camera.door",
                                                                     "return_to_app": False, "timeout": 0}, blocking=True)
        await hass.async_block_till_done()
    command = (await tv(player="idle"))["command"]
    assert (command["back"], command["timeout"]) == (None, 0)
    assert ACTIVE not in [str(call[1]) for call in aioclient_mock.mock_calls]


async def test_foyer_in_front_is_not_remembered(hass, tv, aioclient_mock):
    aioclient_mock.get(ACTIVE, text='<active-app><app id="dev" type="appl">Foyer</app></active-app>')
    aioclient_mock.post(f"{ECP}/input", status=200)
    aioclient_mock.post(f"{ECP}/launch/dev", status=200)
    await tv(player="idle")
    with patch("custom_components.foyer.api.WAKE_GRACE", 0):
        await hass.services.async_call("foyer", "show_camera", {"entity_id": ENTITY, "camera": "camera.door"}, blocking=True)
        await hass.async_block_till_done()
    command = (await tv(player="idle"))["command"]
    assert command["back"] is None
    assert (await tv.post("return", {"command": command["id"]})).status == 404


async def test_notify(hass, tv, aioclient_mock):
    aioclient_mock.post(f"{ECP}/input", status=200)
    await tv(player="idle")
    with patch("custom_components.foyer.api.WAKE_GRACE", 0):
        await hass.services.async_call("foyer", "notify", {"entity_id": ENTITY, "title": "Washer finished",
                                                                "message": "Load is ready"}, blocking=True)
        await hass.async_block_till_done()
    command = (await tv(player="idle"))["command"]
    assert {k: command[k] for k in ("kind", "title", "message", "camera", "timeout")} == {
        "kind": "notify", "title": "Washer finished", "message": "Load is ready", "camera": None, "timeout": 10}
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [f"{ECP}/input?contentId=cast&mediaType=special"]
    await tv(player="idle", ack=command["id"])
    assert hass.states.get(ENTITY).state == "idle"  # A notice is not a view.
    link = hass.data["foyer"].link(tv.entry.entry_id)
    link.last_seen -= 30
    await hass.services.async_call("foyer", "notify", {"entity_id": ENTITY, "title": "Later"}, blocking=True)
    assert link.command is None  # A closed app is never opened for a notice unless asked.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("foyer", "notify", {"entity_id": ENTITY, "title": "x", "camera": "camera.other"},
                                       blocking=True)


async def test_v1_entry_is_migrated(hass, home):
    v1 = {"title": "Den", "entities": ["light.floor", "lock.front"], "routines": ["script.night"], "attention_when_on": [],
          "renderer_url": "", "renderer_secret": ""}
    entry = await setup_entry(hass, v1, version=1, options={**v1, "entities": ["lock.front"]})
    assert entry.version == 2
    settings = entry.data["settings"]
    assert (settings["entities"], settings["favorites"], settings["scenes"]) == (["light.floor", "lock.front"],
                                                                                ["light.floor", "lock.front"], ["script.night"])
    assert (settings["areas"], settings["cameras"], settings["allow_control"], settings["allow_guarded"]) == ([], [], True, False)
    assert entry.options["entities"] == ["lock.front"] and entry.options["scenes"] == ["script.night"]
    assert "routines" not in entry.options


DEVICE_INFO = ("<device-info><serial-number>X00400ABCDEF</serial-number><friendly-model-name>Roku Ultra</friendly-model-name>"
               "<user-device-name>Den TV</user-device-name></device-info>")
APPS = '<apps><app id="12" type="appl" version="5">Netflix</app><app id="dev" type="appl" version="1.0">Foyer</app></apps>'
TV = "http://192.168.1.50:8060"
DISCOVERY = SsdpServiceInfo(ssdp_usn="uuid:roku:ecp:X00400ABCDEF", ssdp_st="roku:ecp", upnp={}, ssdp_location=f"{TV}/")
FORM = {"title": "Maple Street", "areas": [], "entities": ["light.floor"], "cameras": []}


def roku(aioclient_mock, apps=APPS, launch=200):
    aioclient_mock.get(f"{TV}/query/device-info", text=DEVICE_INFO)
    aioclient_mock.get(f"{TV}/query/apps", text=apps)
    aioclient_mock.post(re.compile(r"^http://192\.168\.1\.50:8060/launch/dev\?"), status=launch)


async def discover(hass):
    result = await hass.config_entries.flow.async_init("foyer", context={"source": config_entries.SOURCE_SSDP}, data=DISCOVERY)
    assert result["step_id"] == "discovery_confirm" and result["description_placeholders"] == {"name": "Den TV"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], FORM)


async def test_discovery_pairs_without_typing(hass, home, hass_client_no_auth, aioclient_mock):
    hass.config.internal_url = "http://192.168.1.2:8123"
    assert await setup_component(hass)
    roku(aioclient_mock)
    result = await discover(hass)
    assert result["type"] is FlowResultType.SHOW_PROGRESS and result["progress_action"] == "wait_for_tv"
    match = result["description_placeholders"]["match"]
    launch = aioclient_mock.mock_calls[-1][1]
    assert launch.path == "/launch/dev"
    # Sent exactly as encoded: the TV answers 404 to a raw "http://" inside the query.
    assert "server=http%3A%2F%2F192.168.1.2%3A8123" in launch.raw_query_string
    assert dict(launch.query) | {"code": "x"} == {"contentId": "pair", "mediaType": "special", "server": "http://192.168.1.2:8123",
                                                  "code": "x", "match": match}
    code = launch.query["code"]
    runtime = hass.data["foyer"]
    assert code not in str(runtime.pending.items)
    client = await hass_client_no_auth()
    assert (await client.post("/api/foyer/pair", json={"code": "99999999"})).status == 401
    pairing = asyncio.ensure_future(client.post("/api/foyer/pair", json={"code": code}))
    flow_id = result["flow_id"]
    for _ in range(100):  # The TV paired; HA's dialog then advances the flow, as the frontend does.
        await asyncio.sleep(0.02)
        if hass.config_entries.flow.async_get(flow_id)["step_id"] == "finish":
            break
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert (result["data"]["tv_host"], result["data"]["tv_app"], result["data"]["pair_hash"]) == ("192.168.1.50", "dev", "")
    response = await pairing
    assert response.status == 200
    token = (await response.json())["token"]
    status = await client.get("/api/foyer/status", headers={"Authorization": "Bearer " + token})
    assert status.status == 200  # The entry exists by the time the TV has its token.
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries("foyer")[0]
    assert (entry.unique_id, entry.title, entry.version) == ("X00400ABCDEF", "Den TV", 2)
    assert entry.data["token_hash"] == digest(token) and code not in str(entry.data) and token not in str(entry.data)
    assert runtime.pending.items == {} and runtime.pair_events == {}
    assert not hass.config_entries.flow.async_progress()
    assert (await client.post("/api/foyer/pair", json={"code": code})).status == 401
    again = await hass.config_entries.flow.async_init("foyer", context={"source": config_entries.SOURCE_SSDP}, data=DISCOVERY)
    assert again["type"] is FlowResultType.ABORT and again["reason"] == "already_configured"


async def setup_component(hass):
    from homeassistant.setup import async_setup_component
    return await async_setup_component(hass, "foyer", {})


async def test_discovery_aborts_without_the_app(hass, aioclient_mock):
    roku(aioclient_mock, apps='<apps><app id="12">Netflix</app></apps>')
    result = await hass.config_entries.flow.async_init("foyer", context={"source": config_entries.SOURCE_SSDP}, data=DISCOVERY)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "app_not_installed"


async def test_discovery_ignores_public_and_loopback_addresses(hass, aioclient_mock):
    for location in ("http://8.8.8.8:8060/", "http://127.0.0.1:8060/", "http://tv.example:8060/"):
        info = SsdpServiceInfo(ssdp_usn="x", ssdp_st="roku:ecp", upnp={}, ssdp_location=location)
        result = await hass.config_entries.flow.async_init("foyer", context={"source": config_entries.SOURCE_SSDP}, data=info)
        assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"
    assert not aioclient_mock.mock_calls


async def test_blocked_launch_offers_a_retry(hass, home, aioclient_mock):
    hass.config.internal_url = "http://192.168.1.2:8123"
    assert await setup_component(hass)
    roku(aioclient_mock, launch=403)
    result = await discover(hass)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "launch_failed"}
    assert hass.data["foyer"].pending.items == {}
    aioclient_mock.clear_requests()
    roku(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.SHOW_PROGRESS
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert hass.data["foyer"].pending.items == {}  # A closed dialog leaves no code behind.


async def test_library_browse_and_play(hass, tv, tmp_path):
    (tmp_path / "Movies").mkdir()
    (tmp_path / "Movies" / "clip.mp4").write_bytes(b"\x00" * 64)
    (tmp_path / "song.mp3").write_bytes(b"\x00" * 64)
    (tmp_path / "notes.txt").write_text("not media")
    hass.config.media_dirs["local"] = str(tmp_path)  # Mutated: the local media source holds this same dict.
    assert (await tv(player="idle"))["home"]["library"] is True
    response = await tv.post("browse", {"id": "media-source://media_source/local/."})
    folder = await response.json()
    assert response.status == 200, folder
    kinds = {item["title"]: item["kind"] for item in folder["items"]}
    assert kinds == {"Movies": "folder", "song.mp3": "audio"}  # Text files are not offered.
    song = next(item["id"] for item in folder["items"] if item["kind"] == "audio")
    assert (await tv.post("browse", {"id": "media-source://camera/camera.door"})).status == 404
    assert (await tv.post("play", {"id": "media-source://media_source/local/../secrets.yaml"})).status == 404
    played = await tv.post("play", {"id": song})
    assert played.status == 200 and (await played.json())["result"] == "accepted"
    command = (await tv(player="idle"))["command"]
    assert command["kind"] == "play" and command["media"] == "audio" and "/media/local/song.mp3" in command["url"]
    assert "authSig=" in command["url"]  # Signed, so the TV needs no HA login to fetch it.


async def test_library_can_be_switched_off(hass, hass_client_no_auth, home):
    entry = await setup_entry(hass, {**SETTINGS, "library": False})
    client = await hass_client_no_auth()
    token = (await (await client.post("/api/foyer/pair", json={"code": CODE})).json())["token"]
    headers = {"Authorization": "Bearer " + token}
    assert (await client.post("/api/foyer/browse", headers=headers, json={})).status == 404
    status = await (await client.get("/api/foyer/status", headers=headers)).json()
    assert status["home"]["library"] is False and entry.state.value == "loaded"


async def test_camera_plays_through_its_ring_mqtt_restream(hass, tv):
    hass.states.async_set("sensor.door_info", "ok", {"stream_Source": "rtsp://ring-mqtt:8554/door_live",
                                                     "still_Image_URL": "{{ states.camera.door.attributes.entity_picture }}"})
    camera = (await tv(player="idle"))["home"]["cameras"][0]
    assert camera["id"] == "camera.door" and camera["live"] is True  # The entity itself has no stream feature.
    runtime = hass.data["foyer"]
    with patch.object(type(runtime), "restream", AsyncMock(return_value="/api/hls/abc/master_playlist.m3u8")) as restream:
        reply = await (await tv.post("stream", {"camera": "camera.door"})).json()
    restream.assert_awaited_once_with("camera.door", "rtsp://ring-mqtt:8554/door_live")
    assert reply["url"] == "/api/hls/abc/playlist.m3u8" and reply["format"] == "hls"


async def test_restream_starts_a_real_hls_stream(hass, tv):
    """No stand-ins: HA's own stream engine creates the HLS endpoint the TV is given."""
    from homeassistant.setup import async_setup_component
    assert await async_setup_component(hass, "stream", {})
    runtime = hass.data["foyer"]
    url = await runtime.restream("camera.door", "rtsp://127.0.0.1:9/nothing_here")
    assert url.startswith("/api/hls/") and url.endswith("/master_playlist.m3u8")
    assert await runtime.restream("camera.door", "rtsp://127.0.0.1:9/nothing_here") == url  # Reused while watched.
    await runtime.restreams["camera.door"][1].stop()
