"""Home Assistant serves Foyer's own icons from custom_components/foyer/brand/, not the grey placeholder."""
from pathlib import Path

import pytest
from homeassistant.setup import async_setup_component

BRAND = Path(__file__).resolve().parents[1] / "custom_components" / "foyer" / "brand"


@pytest.mark.parametrize(("image", "source"), [
    ("icon.png", "icon.png"),
    ("icon@2x.png", "icon@2x.png"),
    ("dark_icon.png", "icon.png"),  # the dark theme falls back to the plain icon
])
async def test_brand_icons_are_served_from_the_integration(hass, hass_client_no_auth, image, source):
    assert await async_setup_component(hass, "brands", {})
    token = hass.data["brands"][-1]
    client = await hass_client_no_auth()
    response = await client.get(f"/api/brands/integration/foyer/{image}", params={"token": token})
    assert response.status == 200
    assert response.content_type == "image/png"
    assert await response.read() == (BRAND / source).read_bytes()
