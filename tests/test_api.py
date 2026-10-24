"""Real aiohttp HTTP tests with fake HA state/service/config-entry adapters."""
import asyncio
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from support import load_modules, fake_hass, state

core, api = load_modules()
HAError = sys.modules["homeassistant.exceptions"].HomeAssistantError
EMPTY_REGISTRY = {"areas": {}, "entities": {}}


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.states = {"light.lounge": state("on", supported_color_modes=["brightness"], brightness=255), "script.test": state("off"),
                       "lock.front": state("locked"), "camera.door": state("idle", supported_features=2),
                       "camera.still": state("idle", supported_features=0)}
        self.hass = fake_hass(self.states)
        self.runtime = api.Runtime(self.hass)
        self.runtime.registry_view = lambda: EMPTY_REGISTRY
        self.entry = SimpleNamespace(entry_id="room", options={}, data={
            "settings": {"title": "Home", "entities": ["light.lounge", "lock.front"], "scenes": ["script.test"],
                         "cameras": ["camera.door", "camera.still"], "allow_control": True, "allow_guarded": False},
            "pair_hash": core.digest("00123456"), "pair_expires": time.time() + 60, "token_hash": ""})
        self.runtime.entries["room"] = self.entry
        view = api.FoyerView(self.runtime)
        async def get(request):
            return await view.get(request, request.match_info["operation"])
        async def post(request):
            return await view.post(request, request.match_info["operation"])
        app = web.Application()
        app.router.add_get(view.url, get)
        app.router.add_post(view.url, post)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def pair(self):
        response = await self.client.post("/api/foyer/pair", json={"code": "00123456"})
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["protocol"], 2)
        self.assertNotIn(data["token"], str(self.entry.data))
        return {"Authorization": "Bearer " + data["token"]}

    async def control(self, headers, **overrides):
        data = {"entity": "script.test", "action": "run", "request_id": "request_123"}
        return await self.client.post("/api/foyer/control", headers=headers, json={**data, **overrides})

    async def test_pair_then_status_and_single_use_code(self):
        headers = await self.pair()
        response = await self.client.get("/api/foyer/status", headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        data = await response.json()
        self.assertEqual((data["protocol"], data["poll_seconds"], data["command"]), (2, 3, None))
        self.assertAlmostEqual(data["now"], time.time(), delta=5)
        self.assertEqual([d["id"] for d in data["home"]["devices"]], ["light.lounge", "lock.front"])
        self.assertEqual(data["home"]["scenes"][0]["id"], "script.test")
        retry = await self.client.post("/api/foyer/pair", json={"code": "00123456"})
        self.assertEqual(retry.status, 401)

    async def test_home_is_omitted_when_the_tv_has_its_rev(self):
        headers = await self.pair()
        rev = (await (await self.client.get("/api/foyer/status", headers=headers)).json())["rev"]
        data = await (await self.client.get("/api/foyer/status", headers=headers, params={"rev": rev})).json()
        self.assertEqual(data["rev"], rev)
        self.assertNotIn("home", data)
        self.states["lock.front"] = state("unlocked")
        data = await (await self.client.get("/api/foyer/status", headers=headers, params={"rev": rev})).json()
        self.assertNotEqual(data["rev"], rev)
        self.assertEqual(data["home"]["attention"], ["lock.front"])

    async def test_expired_code_and_leading_zero_preserved(self):
        self.entry.data["pair_expires"] = time.time() - 1
        expired = await self.client.post("/api/foyer/pair", json={"code": "00123456"})
        self.assertEqual(expired.status, 401)
        numeric = await self.client.post("/api/foyer/pair", json={"code": 123456})
        self.assertEqual(numeric.status, 400)

    async def test_discovery_pairing_through_the_pending_registry(self):
        self.runtime.pending.add("flow1", "87654321")
        class CreatesEntry:  # Stands in for the config flow finishing once the TV has paired.
            def set(inner):
                self.runtime.entries["tv2"] = SimpleNamespace(entry_id="tv2", options={}, data={
                    "settings": {"entities": ["sensor.x"]}, "token_hash": self.runtime.pending.token_hash("flow1")})
        self.runtime.pair_events["flow1"] = CreatesEntry()
        response = await self.client.post("/api/foyer/pair", json={"code": "87654321"})
        self.assertEqual(response.status, 200)
        token = (await response.json())["token"]
        status = await self.client.get("/api/foyer/status", headers={"Authorization": "Bearer " + token})
        self.assertEqual([d["id"] for d in (await status.json())["home"]["devices"]], ["sensor.x"])
        again = await self.client.post("/api/foyer/pair", json={"code": "87654321"})
        self.assertEqual(again.status, 401)

    async def test_no_auth_and_invalid_auth(self):
        for headers in [{}, {"Authorization": "Bearer wrong"}]:
            self.assertEqual((await self.client.get("/api/foyer/status", headers=headers)).status, 401)
            self.assertEqual((await self.client.get("/api/foyer/camera?entity=camera.door", headers=headers)).status, 401)
            self.assertEqual((await self.control(headers)).status, 401)
            for operation in ("stream", "return"):
                self.assertEqual((await self.client.post(f"/api/foyer/{operation}", headers=headers, json={})).status, 401)
        self.hass.services.async_call.assert_not_awaited()

    async def test_deeply_nested_json_rejected_without_dispatch(self):
        response = await self.client.post("/api/foyer/pair", data="[" * 1500 + "0" + "]" * 1500,
                                          headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        self.assertEqual((await response.json())["error"], "invalid_request")
        self.hass.services.async_call.assert_not_awaited()

    async def test_revocation_and_unload_immediate(self):
        headers = await self.pair()
        self.entry.data["token_hash"] = ""
        self.assertEqual((await self.client.get("/api/foyer/status", headers=headers)).status, 401)
        self.runtime.entries.clear()
        self.assertEqual((await self.control(headers)).status, 401)

    async def test_revocation_during_body_read_prevents_dispatch(self):
        headers = await self.pair()
        original = api.body
        async def revoke_during_read(request):
            value = await original(request)
            self.entry.data["token_hash"] = ""
            return value
        with patch.object(api, "body", revoke_during_read):
            self.assertEqual((await self.control(headers)).status, 401)
        self.hass.services.async_call.assert_not_awaited()

    async def test_control_scope(self):
        headers = await self.pair()
        for data in [{"entity": "script.unselected"}, {"entity": "lock.front", "action": "unlock", "confirmed": True},
                     {"entity": "light.lounge", "action": "turn_on"}, {"entity": ["light.lounge"]}, {"action": None}]:
            response = await self.control(headers, **data)
            self.assertEqual((response.status, (await response.json())["error"]), (403, "not_allowed"))
        self.hass.services.async_call.assert_not_awaited()

    async def test_request_id_required(self):
        headers = await self.pair()
        for request_id in [None, "short", "x" * 81, "bad id!!", 12345678]:
            self.assertEqual((await self.control(headers, request_id=request_id)).status, 400)
        self.hass.services.async_call.assert_not_awaited()

    async def test_read_only_configuration_applies_without_restart(self):
        headers = await self.pair()
        self.entry.options = {"entities": ["sensor.new"], "scenes": ["script.test"], "allow_control": False}
        response = await self.client.get("/api/foyer/status", headers=headers)
        self.assertEqual((await response.json())["home"]["devices"][0]["id"], "sensor.new")
        self.assertEqual((await self.control(headers)).status, 403)

    async def test_exact_dispatch_and_duplicate_prevention(self):
        headers = await self.pair()
        first = await self.control(headers, entity="light.lounge", action="brightness", value=40)
        self.assertEqual(await first.json(), {"protocol": 2, "result": "accepted"})
        second = await self.control(headers, entity="light.lounge", action="brightness", value=40)
        self.assertEqual((await second.json())["result"], "already_requested")
        self.hass.services.async_call.assert_awaited_once_with(
            "light", "turn_on", {"entity_id": "light.lounge", "brightness_pct": 40}, blocking=True)

    async def test_concurrent_same_request_dispatches_once(self):
        headers = await self.pair()
        async def slow(*args, **kwargs):
            await asyncio.sleep(0.02)
        self.hass.services.async_call.side_effect = slow
        await asyncio.gather(self.control(headers), self.control(headers))
        self.assertEqual(self.hass.services.async_call.await_count, 1)

    async def test_running_or_missing_script_not_dispatched(self):
        headers = await self.pair()
        self.states["script.test"] = state("on")
        response = await self.control(headers)
        self.assertEqual((response.status, (await response.json())["error"]), (409, "busy"))
        del self.states["script.test"]
        self.assertEqual((await self.control(headers, request_id="request_456")).status, 403)  # No longer a scene pill.
        self.hass.services.async_call.assert_not_awaited()

    async def test_failures_never_report_success_or_leak_exception(self):
        headers = await self.pair()
        for number, failure in enumerate([HAError("private details"), ValueError("private schema details")]):
            self.hass.services.async_call.side_effect = failure
            response = await self.control(headers, request_id=f"request_{number}00")
            self.assertEqual(response.status, 502)
            self.assertNotIn("private", await response.text())
        await self.control(headers, request_id="request_000")  # Retry is acknowledged, never re-executed.
        self.assertEqual(self.hass.services.async_call.await_count, 2)

    async def test_timeout_outcome_unknown(self):
        headers = await self.pair()
        self.hass.services.async_call.side_effect = TimeoutError()
        response = await self.control(headers)
        self.assertEqual(response.status, 504)
        self.assertEqual((await response.json())["error"], "outcome_unknown")

    async def test_browser_cross_origin_and_large_body_rejected(self):
        response = await self.client.post("/api/foyer/pair", headers={"Origin": "https://untrusted.example"}, json={"code": "00123456"})
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/foyer/pair", json={"code": "x" * 5000})
        self.assertEqual(response.status, 400)

    async def test_bad_json_and_unknown_operation(self):
        for data in ["{", "[]", "null"]:
            response = await self.client.post("/api/foyer/pair", data=data, headers={"Content-Type": "application/json"})
            self.assertEqual(response.status, 400)
        self.assertEqual((await self.client.get("/api/foyer/not-an-api")).status, 404)
        self.assertEqual((await self.client.post("/api/foyer/action", json={})).status, 403)  # Protocol 1's endpoint is gone.

    async def test_pair_rate_limit_and_unknown_codes(self):
        for _ in range(20):
            await self.client.post("/api/foyer/pair", json={"code": "99999999"})
        self.assertEqual((await self.client.post("/api/foyer/pair", json={"code": "00123456"})).status, 429)

    async def test_unpaired_flood_does_not_lock_out_paired_tv(self):
        headers = await self.pair()
        for _ in range(80):
            await self.client.get("/api/foyer/status", headers={"Authorization": "Bearer wrong"})
        self.assertEqual((await self.client.get("/api/foyer/status")).status, 429)
        self.assertEqual((await self.client.get("/api/foyer/status", headers=headers)).status, 200)

    async def test_each_tv_is_rate_limited(self):
        headers = await self.pair()
        statuses = [(await self.client.get("/api/foyer/status", headers=headers)).status for _ in range(61)]
        self.assertEqual(statuses[:60], [200] * 60)
        self.assertEqual(statuses[60], 429)

    async def test_controls_are_rate_limited_per_tv(self):
        headers = await self.pair()
        statuses = [(await self.control(headers, entity="light.lounge", action="toggle", request_id=f"request_{n:04d}")).status
                    for n in range(41)]
        self.assertEqual(statuses[:40], [200] * 40)
        self.assertEqual(statuses[40], 429)

    async def test_guarded_actions_are_spaced_out(self):
        headers = await self.pair()
        self.entry.data["settings"]["allow_guarded"] = True
        first = await self.control(headers, entity="lock.front", action="unlock", confirmed=True, request_id="request_g1")
        self.assertEqual(first.status, 200)
        second = await self.control(headers, entity="lock.front", action="unlock", confirmed=True, request_id="request_g2")
        self.assertEqual((second.status, (await second.json())["error"]), (409, "busy"))
        self.assertEqual(self.hass.services.async_call.await_count, 1)

    async def test_token_cannot_access_another_tv_profile(self):
        headers = await self.pair()
        self.runtime.entries["other"] = SimpleNamespace(entry_id="other", options={}, data={
            "token_hash": core.digest("other-secret"),
            "settings": {"entities": ["sensor.private"], "scenes": ["script.private"], "cameras": ["camera.private"]}})
        self.states["script.private"] = state("off")
        response = await self.client.get("/api/foyer/status", headers=headers)
        self.assertNotIn("private", await response.text())
        self.assertEqual((await self.control(headers, entity="script.private")).status, 403)
        self.assertEqual((await self.client.get("/api/foyer/camera?entity=camera.private", headers=headers)).status, 404)

    async def test_camera_stills(self):
        headers = await self.pair()
        self.runtime.camera_frame = AsyncMock(return_value=(b"jpeg", "image/jpeg"))
        response = await self.client.get("/api/foyer/camera", headers=headers,
                                         params={"entity": "camera.door", "width": "4000", "t": "7"})
        self.assertEqual((response.status, await response.read()), (200, b"jpeg"))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.runtime.camera_frame.assert_awaited_once_with("camera.door", 1920)
        for entity in ["camera.other", "light.lounge", None]:
            missing = await self.client.get("/api/foyer/camera", headers=headers, params={"entity": entity} if entity else {})
            self.assertEqual(missing.status, 404)
        self.runtime.camera_frame.side_effect = HAError("private camera details")
        failed = await self.client.get("/api/foyer/camera", headers=headers, params={"entity": "camera.door"})
        self.assertEqual(failed.status, 502)
        self.assertNotIn("private", await failed.text())

    async def test_live_stream(self):
        headers = await self.pair()
        self.runtime.camera_stream = AsyncMock(return_value="/api/hls/abc/master_playlist.m3u8")
        response = await self.client.post("/api/foyer/stream", headers=headers, json={"camera": "camera.door"})
        self.assertEqual(await response.json(), {"protocol": 2, "url": "/api/hls/abc/playlist.m3u8", "format": "hls"})
        still = await self.client.post("/api/foyer/stream", headers=headers, json={"camera": "camera.still"})
        self.assertEqual((still.status, (await still.json())["error"]), (409, "no_stream"))
        self.assertEqual((await self.client.post("/api/foyer/stream", headers=headers, json={"camera": "camera.x"})).status, 404)
        self.runtime.camera_stream.side_effect = HAError("private")
        failed = await self.client.post("/api/foyer/stream", headers=headers, json={"camera": "camera.door"})
        self.assertEqual((failed.status, (await failed.json())["error"]), (409, "no_stream"))

    async def test_return_relaunches_only_a_remembered_app(self):
        headers = await self.pair()
        link = self.runtime.link("room")
        link.remote = "192.168.1.30"
        link.remember_return("c0123456789abcdef", "12", "Netflix")
        self.runtime._ecp = AsyncMock(return_value="ok")
        response = await self.client.post("/api/foyer/return", headers=headers, json={"command": "c0123456789abcdef"})
        self.assertEqual(await response.json(), {"protocol": 2, "result": "accepted"})
        self.runtime._ecp.assert_awaited_once_with("192.168.1.30", "/launch/12")
        again = await self.client.post("/api/foyer/return", headers=headers, json={"command": "c0123456789abcdef"})
        self.assertEqual(again.status, 404)
        bad = await self.client.post("/api/foyer/return", headers=headers, json={"command": "../launch"})
        self.assertEqual(bad.status, 400)
        self.assertEqual(self.runtime._ecp.await_count, 1)
        link.remember_return("c0123456789abcdee", *core.HOME_SCREEN)
        await self.client.post("/api/foyer/return", headers=headers, json={"command": "c0123456789abcdee"})
        self.runtime._ecp.assert_awaited_with("192.168.1.30", "/keypress/Home")

    async def test_ecp_never_targets_public_addresses(self):
        for address, expected in [("192.168.1.5", "192.168.1.5"), ("127.0.0.1", "127.0.0.1"), ("fd00::5", "[fd00::5]"),
                                  ("8.8.8.8", None), ("169.254.1.1", None), ("tv.local", None), (None, None)]:
            self.assertEqual(api.lan_host(address), expected)
        self.assertIsNone(api.lan_host("127.0.0.1", loopback=False))  # Discovery only follows real LAN devices.
        self.assertEqual(api.lan_host("10.1.2.3", loopback=False), "10.1.2.3")
