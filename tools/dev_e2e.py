"""Local end-to-end helper for a throwaway development Home Assistant (never a household server).

Usage: python tools/dev_e2e.py setup URL USER PASSWORD RENDERER_URL
Writes a renderer token and a paired TV token to .private/dev-e2e.json (gitignored); prints no secrets.
"""
import asyncio
import json
import secrets
import time
import sys
from pathlib import Path

import aiohttp

OUT = Path(__file__).resolve().parents[1] / ".private" / "dev-e2e.json"


async def access_token(session, base, user, password):
    client = base + "/"
    async with session.post(base + "/auth/login_flow", json={"client_id": client, "handler": ["homeassistant", None], "redirect_uri": client}) as r:
        flow = await r.json()
    async with session.post(f"{base}/auth/login_flow/{flow['flow_id']}", json={"username": user, "password": password, "client_id": client}) as r:
        code = (await r.json())["result"]
    async with session.post(base + "/auth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client}) as r:
        return (await r.json())["access_token"]


async def long_lived(session, base, token):
    async with session.ws_connect(base.replace("http", "ws", 1) + "/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": token})
        assert (await ws.receive_json())["type"] == "auth_ok"
        await ws.send_json({"id": 1, "type": "auth/long_lived_access_token", "client_name": f"Foyer renderer (dev {time.time():.0f})", "lifespan": 30})
        return (await ws.receive_json())["result"]


async def setup(base, user, password, renderer):
    async with aiohttp.ClientSession() as session:
        token = await access_token(session, base, user, password)
        headers = {"Authorization": "Bearer " + token}
        renderer_token = await long_lived(session, base, token)
        renderer_secret = secrets.token_urlsafe(24)
        async with session.post(base + "/api/config/config_entries/flow", headers=headers, json={"handler": "foyer"}) as r:
            flow = await r.json()
        settings = {"title": "Dev TV", "entities": ["input_boolean.front_door_locked", "input_boolean.garage_open", "input_number.living_room_temp"],
                    "scenes": [], "attention_when_on": [], "renderer_url": renderer, "renderer_secret": renderer_secret}
        async with session.post(f"{base}/api/config/config_entries/flow/{flow['flow_id']}", headers=headers, json=settings) as r:
            step = await r.json()
        assert "description_placeholders" in step, {k: step.get(k) for k in ("type", "step_id", "errors", "message")}
        code = step["description_placeholders"]["code"]
        async with session.post(f"{base}/api/config/config_entries/flow/{flow['flow_id']}", headers=headers, json={}) as r:
            assert (await r.json())["type"] == "create_entry"
        async with session.post(base + "/api/foyer/pair", json={"code": code}) as r:
            tv_token = (await r.json())["token"]
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"base": base, "admin_token": token, "renderer_token": renderer_token, "tv_token": tv_token,
                               "renderer_secret": renderer_secret}), encoding="utf-8")
    print(f"Created and paired 'Dev TV'; tokens saved to {OUT.name} (not printed).")


if __name__ == "__main__":
    asyncio.run(setup(*sys.argv[2:6]))
