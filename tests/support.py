"""Load the HA adapter against small fakes; NOT a full HA runtime test."""
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]


def load_modules():
    package = ModuleType("foyer_test")
    package.__path__ = [str(ROOT / "custom_components" / "foyer")]
    sys.modules.setdefault("foyer_test", package)
    for name in ["homeassistant", "homeassistant.components", "homeassistant.components.http", "homeassistant.exceptions"]:
        sys.modules.setdefault(name, ModuleType(name))
    sys.modules["homeassistant.components.http"].HomeAssistantView = type("HomeAssistantView", (), {})
    class HAError(Exception):
        pass
    sys.modules["homeassistant.exceptions"].HomeAssistantError = HAError
    return importlib.import_module("foyer_test.core"), importlib.import_module("foyer_test.api")


def state(value, name="A device", device_class=None, last_changed=None, **attrs):
    return SimpleNamespace(state=value, attributes={"friendly_name": name, "device_class": device_class, **attrs}, last_changed=last_changed)


def fake_hass(states):
    def update(entry, **changes):
        for key, value in changes.items():
            setattr(entry, key, value)
    return SimpleNamespace(states=SimpleNamespace(get=states.get, async_all=lambda domain=None: []), services=SimpleNamespace(async_call=AsyncMock()),
                           config_entries=SimpleNamespace(async_update_entry=update), data={},
                           config=SimpleNamespace(units=SimpleNamespace(temperature_unit="°F"), location_name="Home"))
