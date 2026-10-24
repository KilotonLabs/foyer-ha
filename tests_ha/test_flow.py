"""Config and options flows through Home Assistant's real flow manager."""
from homeassistant.data_entry_flow import FlowResultType

BASE = {"title": "Den", "entities": ["light.lamp"], "scenes": [], "attention_when_on": []}


async def test_create_entry_then_pair_code(hass, hass_client_no_auth):
    result = await hass.config_entries.flow.async_init("foyer", context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**BASE, "renderer_url": "ftp://x"})
    assert result["errors"] == {"base": "invalid_renderer"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**BASE, "renderer_url": "http://192.168.1.20:8099/"})
    assert result["errors"] == {"base": "invalid_renderer"}  # A renderer needs its secret.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**BASE, "renderer_url": "http://192.168.1.20:8099/",
                                                                                  "renderer_secret": "renderer-secret-0123"})
    assert result["step_id"] == "pair"
    code = result["description_placeholders"]["code"]
    assert len(code) == 8 and code.isdigit()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["settings"]["renderer_url"] == "http://192.168.1.20:8099"
    assert code not in str(result["data"])
    await hass.async_block_till_done()
    assert hass.states.get("media_player.den") is not None
    assert result["result"].version == 2
    # Manual pairing: the code shown in HA, typed on the TV.
    client = await hass_client_no_auth()
    response = await client.post("/api/foyer/pair", json={"code": code})
    assert response.status == 200
    token = (await response.json())["token"]
    status = await client.get("/api/foyer/status", headers={"Authorization": "Bearer " + token})
    assert [device["id"] for device in (await status.json())["home"]["devices"]] == ["light.lamp"]


async def test_renderer_can_be_cleared_and_its_secret_is_never_sent_back(hass):
    result = await hass.config_entries.flow.async_init("foyer", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        **BASE, "renderer_url": "http://192.168.1.20:8099", "renderer_secret": "renderer-secret-0123"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    entry = result["result"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert "renderer-secret-0123" not in str(result["data_schema"].schema)
    for key in result["data_schema"].schema:
        if callable(getattr(key, "default", None)):
            assert key.default() != "renderer-secret-0123"
    # Blank secret, same renderer: the saved secret stays.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**BASE, "renderer_url": "http://192.168.1.20:8099"})
    assert entry.options["renderer_secret"] == "renderer-secret-0123"
    # The frontend leaves emptied text fields out of the submission: the renderer is removed.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], BASE)
    assert (entry.options["renderer_url"], entry.options["renderer_secret"]) == ("", "")


async def test_options_menu_settings_and_revoke(hass):
    result = await hass.config_entries.flow.async_init("foyer", context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], BASE)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    entry = result["result"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**BASE, "entities": ["sensor.t"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["entities"] == ["sensor.t"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "revoke"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert entry.data["token_hash"] == "" and entry.data["pair_hash"] == ""
