"""Real Home Assistant runtime tests (pytest-homeassistant-custom-component)."""
from pathlib import Path

import custom_components  # The plugin's testing_config package; add this repo's integration to it.
import pytest

custom_components.__path__.append(str(Path(__file__).resolve().parents[1] / "custom_components"))

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
