"""Diagnostics download through a real config entry: plain JSON, useful fields present, nothing identifying."""
import json

from homeassistant.data_entry_flow import FlowResultType

from custom_components.foyer.core import digest
from custom_components.foyer.diagnostics import async_get_config_entry_diagnostics
from custom_components.foyer.playback import tv_caps

SECRET = "renderer-secret-0123"
SETTINGS = {"title": "14 Elm St", "entities": ["light.elm_st_lamp"], "scenes": [], "attention_when_on": [],
            "renderer_url": "http://192.168.1.20:8099/", "renderer_secret": SECRET}


async def test_diagnostics_hold_no_secrets_addresses_or_names(hass, hass_client_no_auth):
    result = await hass.config_entries.flow.async_init("foyer", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    code = result["description_placeholders"]["code"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    token = (await (await client.post("/api/foyer/pair", json={"code": code})).json())["token"]
    assert (await client.get("/api/foyer/status", headers={"Authorization": "Bearer " + token})).status == 200
    entry = hass.config_entries.async_entries("foyer")[0]
    link = hass.data["foyer"].links[entry.entry_id]
    link.caps = tv_caps({"video": ["hevc", "h264"], "audio": ["aac", "ac3"], "uhd": True})  # frozensets, as a TV reports them
    assert entry.data.get("tv_host")  # the status poll recorded the TV's address

    found = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(found)  # strict: fails on sets, frozensets or any other non-JSON value

    assert found["foyer"] == "1.0.1" and found["home_assistant"]
    assert found["entry"]["paired"] is True and found["entry"]["version"] == 2
    assert found["entry"]["settings"]["entities"] == 1 and found["entry"]["settings"]["renderer"] is True
    assert found["tv"]["online"] is True and found["tv"]["caps"] == {"video": ["h264", "hevc"], "audio": ["aac", "ac3"], "uhd": True}
    for leak in (SECRET, "192.168.1.20", entry.data["tv_host"], token, digest(token), code, digest(code), "Elm", "light."):
        assert leak not in text, leak
