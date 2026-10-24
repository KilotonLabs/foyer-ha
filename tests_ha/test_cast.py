"""Casting through a real Home Assistant runtime: entity, services, media source and API."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components import media_source
from homeassistant.components.media_source import PlayMedia
from homeassistant.components.media_player import DOMAIN as MP
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.foyer.core import digest

ENTITY = "media_player.living_room"
CODE = "00123456"
SETTINGS = {"title": "Living room", "entities": ["light.lamp"], "scenes": [], "attention_when_on": []}


@pytest.fixture
async def setup(hass, tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"\x00" * 32)
    hass.config.media_dirs = {"local": str(tmp_path)}
    assert await async_setup_component(hass, "media_source", {})
    entry = MockConfigEntry(domain="foyer", title="Living room", version=2, data={
        "settings": SETTINGS, "token_hash": "", "pair_hash": digest(CODE), "pair_expires": time.time() + 600})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


WAKE = "http://127.0.0.1:8060/input?contentId=cast&mediaType=special"
CLIP = "media-source://media_source/local/clip.mp4"
RENDERER = {"renderer_url": "http://renderer.local:8099", "renderer_secret": "renderer-secret-0123"}


@pytest.fixture
async def tv(hass, hass_client_no_auth, setup, aioclient_mock):
    aioclient_mock.post(WAKE, status=200)
    client = await hass_client_no_auth()
    response = await client.post("/api/foyer/pair", json={"code": CODE})
    assert response.status == 200
    headers = {"Authorization": "Bearer " + (await response.json())["token"], "X-Foyer-App": "dev"}

    async def poll(**query):
        result = await client.get("/api/foyer/status", headers=headers, params=query)
        assert result.status == 200
        return await result.json()
    poll.client, poll.headers = client, headers
    return poll


async def test_tv_shows_off_until_it_polls(hass, tv):
    assert hass.states.get(ENTITY).state == "off"
    data = await tv(player="idle")
    assert data["command"] is None
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "idle"
    assert state.attributes["app_name"] == "Foyer"


async def test_play_local_media_delivers_signed_relative_url_once(hass, tv):
    await tv(player="idle")
    await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_id": "media-source://media_source/local/clip.mp4",
                                                      "media_content_type": "video/mp4"}, blocking=True)
    await hass.async_block_till_done()
    command = (await tv(player="idle"))["command"]
    assert command["kind"] == "play" and command["media"] == "video" and command["format"] == "mp4"
    assert command["auth"] is False  # Explicit, so no TV app ever compares a missing flag.
    assert command["url"].startswith("/media/local/clip.mp4?authSig=")
    assert "path" not in command
    assert hass.states.get(ENTITY).attributes["tv_wake"] == "ok"
    # Ack plus playback report clears the command and updates the entity.
    data = await tv(player="playing", ack=command["id"], pos="3", dur="10")
    assert data["command"] is None
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "playing"
    assert state.attributes["media_position"] == 3
    assert state.attributes["media_title"] == "clip.mp4"
    await hass.services.async_call(MP, "media_stop", {"entity_id": ENTITY}, blocking=True)
    assert (await tv(player="playing"))["command"]["kind"] == "stop"


async def test_stale_command_expires(hass, tv):
    await tv(player="playing")
    await hass.services.async_call(MP, "media_pause", {"entity_id": ENTITY}, blocking=True)
    link = hass.data["foyer"].link(next(iter(hass.data["foyer"].entries)))
    assert link.command["kind"] == "pause"
    link.command_at -= 61
    assert (await tv())["command"] is None


async def test_blocked_wake_is_reported(hass, tv, aioclient_mock):
    aioclient_mock.clear_requests()
    aioclient_mock.post(WAKE, status=403)
    await tv(player="idle")
    await hass.services.async_call(MP, "media_pause", {"entity_id": ENTITY}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["tv_wake"] == "blocked"


async def test_dashboard_needs_renderer(hass, tv):
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "lovelace"}, blocking=True)


async def test_dashboard_frames_proxied_with_tv_token_only(hass, tv, setup, aioclient_mock):
    hass.config_entries.async_update_entry(setup, options={**SETTINGS, "renderer_url": "http://renderer.local:8099", "renderer_secret": "renderer-secret-0123"})
    aioclient_mock.get("http://renderer.local:8099/frame", content=b"\xff\xd8jpeg", headers={"Content-Type": "image/jpeg"})
    await tv(player="idle")
    await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "dashboard-tv",
                                                                "view_path": "cards", "refresh_seconds": 5}, blocking=True)
    command = (await tv(player="idle"))["command"]
    assert command["kind"] == "view" and command["auth"] is True and command["refresh"] == 5
    assert "dashboard-tv" not in command["url"]
    frame = await tv.client.get(command["url"], headers=tv.headers)
    assert frame.status == 200 and frame.content_type == "image/jpeg"
    assert await frame.read() == b"\xff\xd8jpeg"
    _, url, _, headers = aioclient_mock.mock_calls[-1]
    assert url.query["path"] == "/dashboard-tv/cards"
    assert headers["X-Foyer-Secret"] == "renderer-secret-0123"
    assert (await tv.client.get(command["url"])).status == 401
    assert (await tv.client.get("/api/foyer/frame?cast=c0000000000000000", headers=tv.headers)).status == 404
    await tv(player="view", ack=command["id"])
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "on"
    assert hass.states.get(ENTITY).attributes["app_name"] == "Dashboard"


async def test_renderer_failure_is_opaque(hass, tv, setup, aioclient_mock):
    hass.config_entries.async_update_entry(setup, options={**SETTINGS, "renderer_url": "http://renderer.local:8099"})
    aioclient_mock.get("http://renderer.local:8099/frame", status=500, text="secret stack trace")
    await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "lovelace"}, blocking=True)
    command = (await tv())["command"]
    frame = await tv.client.get(command["url"], headers=tv.headers)
    assert frame.status == 502
    assert "secret" not in await frame.text()


async def test_invalid_view_path_rejected(hass, tv, setup):
    hass.config_entries.async_update_entry(setup, options={**SETTINGS, "renderer_url": "http://renderer.local:8099"})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "../config"}, blocking=True)


async def test_browse_offers_local_media(hass, tv, hass_ws_client):
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "media_player/browse_media", "entity_id": ENTITY, "media_content_type": "",
                            "media_content_id": "media-source://media_source/local/."})
    result = await client.receive_json()
    assert result["success"], result
    assert [child["title"] for child in result["result"]["children"]] == ["clip.mp4"]


async def test_unload_removes_entity_and_access(hass, tv, setup):
    assert await hass.config_entries.async_unload(setup.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state == "unavailable"
    response = await tv.client.get("/api/foyer/status", headers=tv.headers)
    assert response.status == 401


@pytest.mark.parametrize("resolved", [PlayMedia("/api/camera_proxy_stream/camera.door", "image/jpeg"),
                                      media_source.Unresolvable("Camera does not support MJPEG or HLS streaming.")])
async def test_camera_without_hls_becomes_refreshing_still(hass, tv, resolved):
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(side_effect=resolved) if isinstance(resolved, Exception) else AsyncMock(return_value=resolved)):
        await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "",
                                                          "media_content_id": "media-source://camera/camera.door"}, blocking=True)
    command = (await tv())["command"]
    assert command["kind"] == "view" and command["media"] == "image" and command["refresh"] == 2 and command["auth"] is True
    assert "camera" not in command and "authSig" not in command["url"]
    image = SimpleNamespace(content=b"jpeg-camera-still", content_type="image/jpeg")
    with patch("homeassistant.components.camera.async_get_image", AsyncMock(return_value=image)) as get_image:
        frame = await tv.client.get(command["url"] + "&t=3", headers=tv.headers)
        assert frame.status == 200 and await frame.read() == b"jpeg-camera-still"
    assert get_image.await_args.args[1] == "camera.door"


async def test_hls_camera_plays_as_video(hass, tv):
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=PlayMedia("/api/hls/abc/master_playlist.m3u8", "application/vnd.apple.mpegurl"))):
        await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "",
                                                          "media_content_id": "media-source://camera/camera.door"}, blocking=True)
    command = (await tv())["command"]
    assert (command["media"], command["format"]) == ("video", "hls")
    assert command["url"].startswith("/api/hls/abc/playlist.m3u8")  # Roku rejects HA's master playlist.


LAUNCH = "http://127.0.0.1:8060/launch/dev?contentId=cast&mediaType=special"


async def test_closed_app_is_launched_and_address_survives_restart(hass, tv, setup, aioclient_mock):
    aioclient_mock.post(LAUNCH, status=200)
    await tv(player="idle")
    assert setup.data["tv_host"] == "127.0.0.1" and setup.data["tv_app"] == "dev"
    link = hass.data["foyer"].link(setup.entry_id)
    link.last_seen -= 30  # The TV app was closed.
    await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "video/mp4",
                                                      "media_content_id": CLIP}, blocking=True)
    await hass.async_block_till_done()
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [LAUNCH]
    assert await hass.config_entries.async_reload(setup.entry_id)
    link = hass.data["foyer"].link(setup.entry_id)
    assert (link.remote, link.app_id) == ("127.0.0.1", "dev")


async def test_unanswered_input_event_falls_back_to_launch(hass, tv, aioclient_mock):
    aioclient_mock.post(LAUNCH, status=200)
    await tv(player="idle")
    with patch("custom_components.foyer.api.WAKE_GRACE", 0):
        await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "video/mp4",
                                                          "media_content_id": CLIP}, blocking=True)
        await hass.async_block_till_done()
    assert [str(call[1]) for call in aioclient_mock.mock_calls] == [WAKE, LAUNCH]



async def test_large_frame_arrives_complete(hass, tv, setup, aioclient_mock):
    big = bytes(range(256)) * 2400  # 600 KB, many network chunks.
    hass.config_entries.async_update_entry(setup, options={**SETTINGS, "renderer_url": "http://renderer.local:8099", "renderer_secret": "renderer-secret-0123"})
    aioclient_mock.get("http://renderer.local:8099/frame", content=big, headers={"Content-Type": "image/jpeg"})
    await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "lovelace"}, blocking=True)
    command = (await tv())["command"]
    frame = await tv.client.get(command["url"], headers=tv.headers)
    assert await frame.read() == big


async def test_tv_showing_forgotten_cast_is_told_to_stop(hass, tv):
    command = (await tv(player="view"))["command"]
    assert command["kind"] == "stop"
    assert (await tv(player="idle", ack=command["id"]))["command"] is None


def ecp_calls(aioclient_mock):
    return [str(call[1]) for call in aioclient_mock.mock_calls if ":8060/" in str(call[1])]


async def test_announcement_title_is_the_spoken_message(hass, tv):
    await tv(player="idle")
    spoken = "media-source://tts/demo?message=The%20washer%20has%20finished&language=en"
    with patch("homeassistant.components.media_source.async_resolve_media",
               AsyncMock(return_value=PlayMedia("/api/tts_proxy/Xk3_9fQ2LmA0bC7dE1fG2h.mp3", "audio/mpeg"))):
        await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "music",
                                                          "media_content_id": spoken, "announce": True}, blocking=True)
    command = (await tv(player="idle"))["command"]
    assert command["title"] == "The washer has finished" and command["announce"] is True
    assert (command["media"], command["format"]) == ("audio", "mp3")
    await tv(player="playing", ack=command["id"], ann="1", pos="1", dur="3")
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert state.state == "playing" and state.attributes["media_title"] == "The washer has finished"


async def test_announcement_over_a_dashboard_returns_to_it(hass, tv, setup, aioclient_mock):
    hass.config_entries.async_update_entry(setup, options={**SETTINGS, **RENDERER})
    aioclient_mock.get("http://renderer.local:8099/frame", content=b"\xff\xd8jpeg", headers={"Content-Type": "image/jpeg"})
    await tv(player="idle")
    await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "dashboard-tv"}, blocking=True)
    view = (await tv(player="idle"))["command"]
    await tv(player="view", ack=view["id"])
    await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "audio/mpeg", "announce": True,
                                                      "media_content_id": "https://example.com/doorbell.mp3"}, blocking=True)
    announcement = (await tv(player="view", ack=view["id"]))["command"]
    await tv(player="playing", ack=announcement["id"], ann="1")
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["media_title"] == "doorbell.mp3"
    data = await tv(player="view", ack=announcement["id"])  # The TV is showing the dashboard again.
    assert data["command"] is None  # Not told to stop it.
    assert (await tv.client.get(view["url"] + "&t=9", headers=tv.headers)).status == 200
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert (state.state, state.attributes["app_name"], state.attributes["media_title"]) == ("on", "Dashboard", "dashboard-tv")


async def test_picture_collected_just_before_an_announcement_stays_on_screen(hass, tv, setup, aioclient_mock):
    hass.config_entries.async_update_entry(setup, options={**SETTINGS, **RENDERER})
    aioclient_mock.get("http://renderer.local:8099/frame", content=b"\xff\xd8jpeg", headers={"Content-Type": "image/jpeg"})
    await tv(player="idle")
    await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "dashboard-tv"}, blocking=True)
    first = (await tv(player="idle"))["command"]
    await tv(player="view", ack=first["id"])
    await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "dashboard-tv",
                                                               "view_path": "cameras"}, blocking=True)
    second = (await tv(player="view", ack=first["id"]))["command"]  # Collected; its ack comes with the next poll.
    await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "audio/mpeg", "announce": True,
                                                      "media_content_id": "https://example.com/doorbell.mp3"}, blocking=True)
    assert (await tv.client.get(second["url"] + "&t=1", headers=tv.headers)).status == 200
    announcement = (await tv(player="view", ack=second["id"]))["command"]
    assert announcement["announce"] is True
    await tv(player="playing", ack=announcement["id"], ann="1")
    assert (await tv(player="view", ack=announcement["id"]))["command"] is None  # Not told to stop.
    assert (await tv.client.get(second["url"] + "&t=2", headers=tv.headers)).status == 200
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert (state.state, state.attributes["media_title"]) == ("on", "dashboard-tv/cameras")


async def test_dashboard_title_survives_many_announcements(hass, tv, setup):
    hass.config_entries.async_update_entry(setup, options={**SETTINGS, **RENDERER})
    await tv(player="idle")
    await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "dashboard-tv"}, blocking=True)
    view = (await tv(player="idle"))["command"]
    last = view["id"]
    for number in range(17):  # More than the 16 recent titles kept.
        await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "audio/mpeg", "announce": True,
                                                          "media_content_id": f"https://example.com/chime-{number}.mp3"}, blocking=True)
        announcement = (await tv(player="view", ack=last))["command"]
        await tv(player="playing", ack=announcement["id"], ann="1")
        last = announcement["id"]
    await tv(player="view", ack=last)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY)
    assert (state.state, state.attributes["app_name"], state.attributes["media_title"]) == ("on", "Dashboard", "dashboard-tv")


async def test_cast_photo_and_plain_image_links_stay_on_screen(hass, tv):
    await tv(player="idle")
    for media_type in ("image", "image/jpeg"):
        await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": media_type,
                                                          "media_content_id": "https://example.com/doorbell.jpg"}, blocking=True)
        command = (await tv(player="idle"))["command"]
        assert (command["kind"], command["media"], command["refresh"], command["auth"]) == ("play", "image", 0, False)
        assert (await tv(player="view", ack=command["id"]))["command"] is None  # No stop.
        await hass.async_block_till_done()
        state = hass.states.get(ENTITY)
        assert (state.state, state.attributes["app_name"], state.attributes["media_title"]) == ("on", "Picture", "doorbell.jpg")


async def test_stop_pause_and_play_never_open_a_closed_app(hass, tv, setup, aioclient_mock):
    await tv(player="idle")
    link = hass.data["foyer"].link(setup.entry_id)
    link.last_seen -= 30  # The TV app was closed.
    aioclient_mock.clear_requests()
    aioclient_mock.post(LAUNCH, status=200)
    for service in ("media_stop", "media_pause", "media_play", "media_play_pause"):
        await hass.services.async_call(MP, service, {"entity_id": ENTITY}, blocking=True)
        await hass.async_block_till_done()
    assert ecp_calls(aioclient_mock) == []
    # Stopping a cast the closed app has not collected yet cancels it.
    await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "video/mp4",
                                                      "media_content_id": CLIP}, blocking=True)
    await hass.services.async_call(MP, "media_stop", {"entity_id": ENTITY}, blocking=True)
    await hass.async_block_till_done()
    assert link.command is None


async def test_revoked_tv_can_no_longer_be_cast_to_or_launched(hass, tv, setup, aioclient_mock):
    await tv(player="idle")
    assert setup.data["tv_host"] == "127.0.0.1"
    flow = await hass.config_entries.options.async_init(setup.entry_id)
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"next_step_id": "revoke"})
    await hass.config_entries.options.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    assert "tv_host" not in setup.data and "tv_app" not in setup.data
    response = await tv.client.get("/api/foyer/status", headers=tv.headers)
    assert response.status == 401
    aioclient_mock.clear_requests()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "video/mp4",
                                                          "media_content_id": CLIP}, blocking=True)
    await hass.async_block_till_done()
    assert ecp_calls(aioclient_mock) == []
    assert hass.states.get(ENTITY).state == "off"


async def test_expired_view_keeps_the_one_on_screen(hass, tv, setup, aioclient_mock):
    hass.config_entries.async_update_entry(setup, options={**SETTINGS, **RENDERER})
    aioclient_mock.get("http://renderer.local:8099/frame", content=b"\xff\xd8jpeg", headers={"Content-Type": "image/jpeg"})
    await tv(player="idle")
    await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "lovelace"}, blocking=True)
    first = (await tv(player="idle"))["command"]
    await tv(player="view", ack=first["id"])
    await hass.services.async_call("foyer", "show_view", {"entity_id": ENTITY, "dashboard_path": "dashboard-tv"}, blocking=True)
    hass.data["foyer"].link(setup.entry_id).command_at -= 61  # The TV missed it.
    for _ in range(3):
        assert (await tv(player="view", ack=first["id"]))["command"] is None
    assert (await tv.client.get(first["url"] + "&t=2", headers=tv.headers)).status == 200
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).attributes["media_title"] == "lovelace"


async def test_unpaired_tv_rejects_casts(hass, setup):
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(MP, "play_media", {"entity_id": ENTITY, "media_content_type": "video/mp4",
                                                          "media_content_id": CLIP}, blocking=True)


async def test_late_wake_after_unload_does_not_recreate_tv(hass, tv, setup):
    runtime = hass.data["foyer"]
    assert await hass.config_entries.async_unload(setup.entry_id)
    await runtime.wake(setup.entry_id)
    runtime.notify(setup.entry_id)
    assert setup.entry_id not in runtime.links
