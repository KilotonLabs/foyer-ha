"""Protocol and state rules. No HA imports, network, or device mutations."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import re
import secrets
import time
from collections import OrderedDict, deque
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

DOMAIN = "foyer"
PROTOCOL = 2
POLL_SECONDS = 3
PAIR_TTL = 600
LIMITS = {"areas": 50, "entities": 100, "exclude": 300, "cameras": 16, "favorites": 12, "scenes": 12, "attention_when_on": 100}
COUNT_ERRORS = {"areas": "area_count", "entities": "entity_count", "exclude": "exclude_count", "cameras": "camera_count",
                "favorites": "favorite_count", "scenes": "scene_count", "attention_when_on": "invalid_attention"}
MAX_DEVICES = 300
ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
AREA_RE = re.compile(r"^[\w-]{1,64}$")
RENDERER_RE = re.compile(r"^https?://[A-Za-z0-9.\[\]:-]+(/[A-Za-z0-9._~/-]*)?$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
AREA_DOMAINS = {"light", "switch", "fan", "cover", "lock", "climate", "media_player", "input_boolean", "vacuum",
                "alarm_control_panel", "humidifier"}
AREA_CLASSES = {
    "binary_sensor": {"door", "window", "garage_door", "opening", "motion", "occupancy", "presence", "moisture", "smoke", "gas",
                      "carbon_monoxide", "safety", "problem", "lock", "tamper", "heat", "cold"},
    "sensor": {"temperature", "humidity", "power", "carbon_dioxide", "pm25", "illuminance"},
}
SUPPORTED = AREA_DOMAINS | {"sensor", "binary_sensor", "input_select", "input_number", "button", "input_button", "scene", "script"}
KINDS = {"light": "light", "switch": "switch", "input_boolean": "switch", "fan": "fan", "cover": "cover", "lock": "lock",
         "climate": "climate", "media_player": "media", "sensor": "sensor", "binary_sensor": "binary", "vacuum": "vacuum",
         "alarm_control_panel": "alarm", "humidifier": "humidifier", "input_select": "select", "input_number": "number",
         "button": "button", "input_button": "button", "scene": "scene", "script": "script"}
RUNNABLE = {"scene": ("scene", "turn_on"), "script": ("script", "turn_on"), "button": ("button", "press"),
            "input_button": ("input_button", "press")}
GUARDED_CLASSES = {"garage", "gate"}
UNAVAILABLE = {"unknown", "unavailable", "", None}
NOT_REPORTING = "Not reporting"
ATTENTION_CLASSES = {"door", "window", "opening", "garage_door", "moisture", "problem", "smoke", "gas", "safety", "battery",
                     "carbon_monoxide", "tamper", "heat", "cold", "lock"}
# Binary sensor on/off wording by device class; anything else reads On/Off.
BINARY_LABELS = {
    "door": ("Open", "Closed"), "window": ("Open", "Closed"), "opening": ("Open", "Closed"), "garage_door": ("Open", "Closed"),
    "lock": ("Unlocked", "Locked"), "moisture": ("Wet", "Dry"), "battery": ("Low", "Normal"), "problem": ("Problem", "OK"),
    "smoke": ("Smoke detected", "Clear"), "gas": ("Gas detected", "Clear"), "carbon_monoxide": ("CO detected", "Clear"),
    "safety": ("Unsafe", "Safe"), "tamper": ("Tampering", "Clear"), "heat": ("Hot", "Normal"), "cold": ("Cold", "Normal"),
    "motion": ("Detected", "Clear"), "occupancy": ("Detected", "Clear"), "presence": ("Home", "Away"),
    "connectivity": ("Connected", "Disconnected"), "plug": ("Plugged in", "Unplugged"), "power": ("Power on", "No power"),
}
BINARY_ICONS = {"door": ("door-open", "door-closed"), "opening": ("door-open", "door-closed"),
                "window": ("window-open-variant", "window-closed-variant"), "garage_door": ("garage-open-variant", "garage-variant"),
                "motion": ("motion-sensor", "motion-sensor-off"), "occupancy": ("motion-sensor", "motion-sensor-off"),
                "moisture": ("water-alert", "water-off"), "smoke": ("smoke-detector-alert", "smoke-detector")}
STATIC_ICONS = {"climate": "thermostat", "vacuum": "robot-vacuum", "alarm": "shield-home", "scene": "palette",
                "script": "script-text", "button": "gesture-tap-button"}
SENSOR_ICONS = {"temperature": "thermometer", "humidity": "water-percent", "power": "flash"}
ROUNDED_SENSORS = {"temperature", "humidity", "power", "carbon_dioxide", "pm25", "illuminance", "battery"}
MOTION_ORDER = ("motion", "occupancy", "presence")
MOTION_CLASSES = set(MOTION_ORDER)
COLOR_TEMPS = {"warm": 2700, "neutral": 4000, "daylight": 6500}
WEATHER = {  # HA condition: (text, mdi icon)
    "clear-night": ("Clear", "weather-night"), "cloudy": ("Cloudy", "weather-cloudy"),
    "exceptional": ("Exceptional", "alert-circle-outline"), "fog": ("Fog", "weather-fog"), "hail": ("Hail", "weather-hail"),
    "lightning": ("Lightning", "weather-lightning"), "lightning-rainy": ("Thunderstorms", "weather-lightning-rainy"),
    "partlycloudy": ("Partly cloudy", "weather-partly-cloudy"), "pouring": ("Pouring rain", "weather-pouring"),
    "rainy": ("Rain", "weather-rainy"), "snowy": ("Snow", "weather-snowy"), "snowy-rainy": ("Sleet", "weather-snowy-rainy"),
    "sunny": ("Sunny", "weather-sunny"), "windy": ("Windy", "weather-windy"), "windy-variant": ("Windy", "weather-windy-variant"),
}
# HA feature bits read from state attributes.
FAN_SET_SPEED, COVER_OPEN, COVER_CLOSE, COVER_POSITION, COVER_STOP = 1, 1, 2, 4, 8
CAMERA_STREAM, MEDIA_VOLUME_SET, MEDIA_NEXT = 2, 4, 32


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def matches(value: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(digest(value), expected)


def new_pairing() -> tuple[str, dict]:
    """A code to show and the hash to store. Its 10 minutes start at activation, not while it is on screen."""
    code = f"{secrets.randbelow(100_000_000):08d}"
    return code, {"pair_hash": digest(code)}


def new_match() -> str:
    return f"{secrets.randbelow(10_000):04d}"


def activate_pairing(pairing: dict, now: float | None = None) -> dict:
    return {**pairing, "pair_expires": (time.time() if now is None else now) + PAIR_TTL}


def _entity_list(values, domains=None) -> bool:
    return all(isinstance(v, str) and ENTITY_RE.fullmatch(v) and (domains is None or v.split(".")[0] in domains) for v in values)


def validate_settings(settings: dict) -> None:
    title = settings.get("title", "Home")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 40:
        raise ValueError("invalid_title")
    lists = {key: settings.get(key, []) for key in LIMITS}
    for key, values in lists.items():
        if not isinstance(values, list) or len(values) > LIMITS[key]:
            raise ValueError(COUNT_ERRORS[key])
    if not all(isinstance(a, str) and AREA_RE.fullmatch(a) for a in lists["areas"]) or len(set(lists["areas"])) != len(lists["areas"]):
        raise ValueError("invalid_area")
    for key, domains, error in [("entities", SUPPORTED, "invalid_entity"), ("exclude", None, "invalid_entity"),
                                ("favorites", None, "invalid_entity"), ("cameras", {"camera"}, "invalid_camera"),
                                ("scenes", {"scene", "script"}, "invalid_scene"),
                                ("attention_when_on", {"binary_sensor", "input_boolean"}, "invalid_attention")]:
        if not _entity_list(lists[key], domains) or len(set(lists[key])) != len(lists[key]):
            raise ValueError(error)
    weather = settings.get("weather", "")
    if not isinstance(weather, str) or (weather and not _entity_list([weather], {"weather"})):
        raise ValueError("invalid_weather")
    if not all(isinstance(settings.get(key, True), bool) for key in ("allow_control", "allow_guarded", "library", "convert")):
        raise ValueError("invalid_settings")
    renderer = settings.get("renderer_url", "")
    if not isinstance(renderer, str) or (renderer and not RENDERER_RE.fullmatch(renderer)):
        raise ValueError("invalid_renderer")
    secret = settings.get("renderer_secret", "")
    if not isinstance(secret, str) or len(secret) > 128 or (renderer and len(secret) < 16):
        raise ValueError("invalid_renderer")
    if not (lists["areas"] or lists["entities"] or lists["cameras"]):
        raise ValueError("nothing_selected")


def migrate_settings(old: dict) -> dict:
    """Protocol 1 profile to protocol 2: its checks become extra entities and favourites, its scripts scene pills."""
    entities = list(old.get("entities", []))
    return {"title": old.get("title") or "Home", "areas": [], "entities": entities[:LIMITS["entities"]], "exclude": [],
            "cameras": [], "favorites": entities[:LIMITS["favorites"]], "scenes": list(old.get("routines", []))[:LIMITS["scenes"]],
            "attention_when_on": list(old.get("attention_when_on", [])), "weather": "", "allow_control": True,
            "allow_guarded": False, "library": True, "convert": False, "renderer_url": old.get("renderer_url", ""),
            "renderer_secret": old.get("renderer_secret", "")}


def readable(raw) -> str:
    """HA state machine values (not_home, heat_cool) as display text; input_select options pass through."""
    text = str(raw)
    if re.fullmatch(r"[a-z0-9_]+", text):
        text = text.replace("_", " ").capitalize()
    return text[:120]


def clip(text, limit: int) -> str:
    """One line of text, at most `limit` characters."""
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def epoch(value) -> int | None:
    if value is None:
        return None
    try:
        return int((datetime.fromisoformat(value) if isinstance(value, str) else value).timestamp())
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def _num(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _short(number: float | None):
    """Whole numbers as ints, others to one decimal, so the JSON and the rev stay compact and stable."""
    if number is None:
        return None
    number = round(number, 1)
    return int(number) if number == int(number) else number


def _degrees(number, exact=False) -> str:
    return f"{_short(number) if exact else round(number)}°"


def clean_name(name, area_name: str = "") -> str:
    """'Living room Floor lamp' in Living room reads 'Floor lamp'; 'Lamp Lamp' reads 'Lamp'."""
    text = " ".join(str(name).split())
    size = len(area_name)
    if size and text[:size].casefold() == area_name.casefold() and text[size:size + 1] in {" ", "-", "_", ":"}:
        text = text[size:].lstrip(" -_:") or text
    words = text.split(" ")
    if len(words) > 1 and words[0] == words[1]:
        text = " ".join(words[1:])
    return clip(text[:1].upper() + text[1:], 60)


def _available(kind: str, raw) -> bool:
    # A scene or button that never ran reports "unknown"; that is normal, not a fault.
    return raw not in UNAVAILABLE or (kind in {"scene", "button"} and raw == "unknown")


def _kind(domain: str, device_class) -> str:
    if domain == "cover" and device_class in GUARDED_CLASSES:
        return "garage"
    return KINDS.get(domain, "sensor")


def _icon(domain, kind, device_class, raw, attrs) -> str:
    custom = attrs.get("icon")
    if isinstance(custom, str) and re.fullmatch(r"mdi:[a-z0-9-]{1,60}", custom):
        return custom[4:]
    on = raw in {"on", "open", "opening", "playing"}
    if kind == "light":
        return "lightbulb" if on else "lightbulb-outline"
    if kind == "switch":
        return "power-socket-us" if device_class == "outlet" else ("toggle-switch" if on else "toggle-switch-off-outline")
    if kind == "fan":
        return "fan" if on else "fan-off"
    if kind == "cover":
        return "blinds-horizontal" if on else "blinds-horizontal-closed"
    if kind == "garage":
        return "garage-open-variant" if on else "garage-variant"
    if kind == "lock":
        return "lock" if raw == "locked" else "lock-open-variant"
    if kind == "media":
        return "television" if device_class == "tv" else "speaker"
    if kind == "binary" and device_class in BINARY_ICONS:
        return BINARY_ICONS[device_class][raw != "on"]
    if kind == "sensor" and device_class in SENSOR_ICONS:
        return SENSOR_ICONS[device_class]
    return STATIC_ICONS.get(kind, "circle-medium")


def _measure(raw, attrs, device_class) -> str:
    """A sensor reading with its unit: '71°', '44%', '118 W'."""
    unit = str(attrs.get("unit_of_measurement") or "")[:12]
    number = _num(raw)
    if number is None:
        return clip(f"{raw} {unit}".strip(), 40)
    if device_class == "temperature":
        return _degrees(number)
    text = str(round(number)) if device_class in ROUNDED_SENSORS else str(_short(number))
    return f"{text}%" if unit == "%" else f"{text} {unit}".strip()


def _brightness(attrs) -> int | None:
    value = _num(attrs.get("brightness"))
    return None if value is None else max(0, min(100, round(value / 255 * 100)))


def _dimmable(attrs) -> bool:
    modes = attrs.get("supported_color_modes")
    if isinstance(modes, (list, tuple, set)) and modes:
        return bool(set(modes) - {"onoff", "unknown"})
    return attrs.get("brightness") is not None


def _climate_label(raw, attrs) -> str:
    if raw == "off":
        return "Off"
    action = attrs.get("hvac_action")
    target = _num(attrs.get("temperature"))
    if action == "heating" and (target is not None or _num(attrs.get("target_temp_low")) is not None):
        return f"Heating to {_degrees(target if target is not None else _num(attrs.get('target_temp_low')), True)}"
    if action == "cooling" and (target is not None or _num(attrs.get("target_temp_high")) is not None):
        return f"Cooling to {_degrees(target if target is not None else _num(attrs.get('target_temp_high')), True)}"
    base = readable(action) if isinstance(action, str) and action else readable(raw)
    current = _num(attrs.get("current_temperature"))
    return f"{base}, {_degrees(current)}" if current is not None else base


def _label(kind, raw, attrs, device_class) -> str:
    if not _available(kind, raw):
        return NOT_REPORTING
    if kind == "binary" and raw in ("on", "off"):
        return BINARY_LABELS.get(device_class, ("On", "Off"))[raw == "off"]
    if kind in ("sensor", "number"):
        return _measure(raw, attrs, device_class)
    if kind == "climate":
        return _climate_label(raw, attrs)
    if kind in ("cover", "garage"):
        position = _num(attrs.get("current_position"))
        if raw == "open" and position is not None and 0 < position < 100:
            return f"Open {round(position)}%"
    if kind in ("scene", "button"):
        return ""
    if kind == "script":
        return "Running" if raw == "on" else ""
    return clip(readable(raw), 40)


def _value(kind, raw, attrs, label) -> str:
    if not _available(kind, raw):
        return ""
    if kind == "light" and raw == "on" and _dimmable(attrs) and _brightness(attrs) is not None:
        return f"{_brightness(attrs)}%"
    if kind == "climate" and _num(attrs.get("current_temperature")) is not None:
        return _degrees(_num(attrs["current_temperature"]))
    if kind == "fan" and raw == "on" and _num(attrs.get("percentage")) is not None:
        return f"{round(_num(attrs['percentage']))}%"
    if kind in ("cover", "garage") and raw == "open":
        position = _num(attrs.get("current_position"))
        return f"{round(position)}%" if position is not None and 0 < position < 100 else ""
    if kind in ("sensor", "number"):
        return label if len(label) <= 12 else ""
    return ""


def _is_on(kind, raw) -> bool:
    return ((kind in ("light", "switch", "fan") and raw == "on") or (kind == "media" and raw == "playing")
            or (kind == "cover" and raw == "open") or (kind == "vacuum" and raw == "cleaning"))


def _severity(kind, raw, device_class, flagged) -> str:
    if not _available(kind, raw):
        return "unknown"
    if flagged and raw == "on":
        return "attention"
    if kind == "lock":
        return "ok" if raw == "locked" else "attention"
    if kind == "garage":
        return "ok" if raw == "closed" else "attention"
    if kind == "binary" and device_class in ATTENTION_CLASSES:
        return {"on": "attention", "off": "ok"}.get(raw, "unknown")
    if kind == "alarm" and raw == "triggered":
        return "attention"
    if kind in ("light", "switch", "cover", "fan"):
        return "ok"
    return "info"


def _control(kind, raw, attrs, settings, unit):
    if not settings.get("allow_control", True) or not _available(kind, raw):
        return None
    features = int(_num(attrs.get("supported_features")) or 0)
    guarded_ok = settings.get("allow_guarded", False) is True
    if kind == "light":
        dimmable = _dimmable(attrs)
        modes = attrs.get("supported_color_modes") or []
        temps = list(COLOR_TEMPS) if "color_temp" in modes else []
        kelvin = _num(attrs.get("color_temp_kelvin")) if raw == "on" else None
        return {"toggle": True, "brightness": ((_brightness(attrs) or 0) if raw == "on" else 0) if dimmable else None,
                "temps": temps, "temp": min(COLOR_TEMPS, key=lambda name: abs(COLOR_TEMPS[name] - kelvin))
                if temps and kelvin is not None else None}
    if kind == "switch":
        return {"toggle": True}
    if kind == "fan":
        speed = round(_num(attrs.get("percentage")) or 0) if raw == "on" else 0
        return {"toggle": True, "speed": speed if features & FAN_SET_SPEED else None}
    if kind in ("cover", "garage"):
        if kind == "garage" and not guarded_ok:
            return None
        position = _num(attrs.get("current_position"))
        if position is None:
            position = 0 if raw == "closed" else 100
        return {"open": bool(features & COVER_OPEN), "close": bool(features & COVER_CLOSE), "stop": bool(features & COVER_STOP),
                "position": round(position) if features & COVER_POSITION else None, "guarded": kind == "garage"}
    if kind == "lock":
        return {"lock": True, "unlock": guarded_ok, "guarded": True}
    if kind == "climate":
        ranged = raw == "heat_cool" or (attrs.get("temperature") is None and attrs.get("target_temp_low") is not None)
        celsius = unit == "°C"
        modes = [m for m in attrs.get("hvac_modes") or [] if isinstance(m, str) and re.fullmatch(r"[a-z_]{1,20}", m)][:8]
        action = attrs.get("hvac_action")
        return {"target": None if ranged else _short(_num(attrs.get("temperature"))),
                "step": _short(_num(attrs.get("target_temp_step")) or (0.5 if celsius else 1)),
                "min": _short(_num(attrs.get("min_temp")) or (7 if celsius else 45)),
                "max": _short(_num(attrs.get("max_temp")) or (35 if celsius else 95)),
                "current": _short(_num(attrs.get("current_temperature"))), "unit": unit, "mode": str(raw), "modes": modes,
                "action": action if isinstance(action, str) and re.fullmatch(r"[a-z_]{1,20}", action) else ""}
    if kind == "media":
        volume = _num(attrs.get("volume_level"))
        return {"playing": raw == "playing", "title": clip(attrs.get("media_title") or "", 60),
                "artist": clip(attrs.get("media_artist") or "", 60),
                "volume": round(volume * 100) if features & MEDIA_VOLUME_SET and volume is not None else None,
                "next": bool(features & MEDIA_NEXT)}
    if kind in ("scene", "script", "button"):
        return {"run": True}
    return None


def build_device(entity_id: str, state, settings: dict, area_id: str = "", area_name: str = "", device_class=None,
                 flagged: bool = False, unit: str = "°C") -> dict:
    domain = entity_id.split(".")[0]
    attrs = state.attributes if state is not None else {}
    raw = state.state if state is not None else None
    device_class = attrs.get("device_class") or device_class
    kind = _kind(domain, device_class)
    label = _label(kind, raw, attrs, device_class)
    friendly = attrs.get("friendly_name") or entity_id
    device = {"id": entity_id, "name": clean_name(friendly, area_name), "area": area_id,
              "kind": kind, "icon": _icon(domain, kind, device_class, raw, attrs), "label": label,
              "value": _value(kind, raw, attrs, label), "on": _is_on(kind, raw),
              "severity": _severity(kind, raw, device_class, flagged),
              "changed_at": epoch(getattr(state, "last_changed", None)), "control": _control(kind, raw, attrs, settings, unit)}
    if clean_name(friendly) != device["name"]:
        device["full"] = clean_name(friendly)  # With the room, for screens that show devices from several rooms.
    return device


def _pulled(entity_id: str, info: dict) -> bool:
    """Whether an area brings this entity in by itself: visible, primary and a supported kind."""
    domain = entity_id.split(".")[0]
    if info.get("disabled") or info.get("hidden") or info.get("category") or info.get("platform") == DOMAIN:
        return False
    return domain in AREA_DOMAINS or info.get("device_class") in AREA_CLASSES.get(domain, ())


def area_order(areas: dict) -> list:
    return sorted(areas, key=lambda a: (areas[a].get("level") is None, areas[a].get("level") or 0,
                                        str(areas[a].get("name", "")).casefold(), a))


def select_entities(settings: dict, registry: dict, own_tv: str | None = None) -> list:
    """Entity ids the TV may see: area members first, in area order, then extras and favorites; exclusions win; at most 300."""
    areas, entries = registry.get("areas", {}), registry.get("entities", {})
    selected, excluded = set(settings.get("areas", [])), set(settings.get("exclude", []))
    members = {}
    for entity_id, info in entries.items():
        # The Roku integration's entities for this same TV would only show the TV to itself.
        own = own_tv is not None and info.get("roku") == own_tv
        if info.get("area") in selected and entity_id not in excluded and not own and _pulled(entity_id, info):
            members.setdefault(info["area"], []).append(entity_id)
    result = [e for area in area_order(areas) for e in sorted(members.get(area, []))]
    seen = set(result)
    for entity_id in [*settings.get("entities", []), *settings.get("favorites", [])]:
        info = entries.get(entity_id, {})
        if (entity_id not in seen and entity_id not in excluded and entity_id.split(".")[0] in SUPPORTED
                and info.get("platform") != DOMAIN):
            seen.add(entity_id)
            result.append(entity_id)
    return result[:MAX_DEVICES]


def _summary(facts: list) -> str:
    """'71°, 44% humidity, 2 lights on, 1 window open' from the devices sent for one area."""
    temps = [n for kind, dc, raw, n in facts if kind == "sensor" and dc == "temperature" and n is not None]
    if not temps:  # No thermometer in the room: the thermostat's own reading.
        temps = [n for kind, dc, raw, n in facts if kind == "climate" and n is not None]
    humid = [n for kind, dc, raw, n in facts if kind == "sensor" and dc == "humidity" and n is not None]
    lights = sum(kind == "light" and raw == "on" for kind, dc, raw, n in facts)
    doors = sum(kind == "binary" and dc in {"door", "garage_door", "opening"} and raw == "on" for kind, dc, raw, n in facts)
    windows = sum(kind == "binary" and dc == "window" and raw == "on" for kind, dc, raw, n in facts)
    parts = []
    if temps:
        parts.append(_degrees(sum(temps) / len(temps)))
    if humid:
        parts.append(f"{round(sum(humid) / len(humid))}% humidity")
    for count, noun, verb in [(lights, "light", "on"), (doors, "door", "open"), (windows, "window", "open")]:
        if count:
            parts.append(f"{count} {noun}{'s' if count > 1 else ''} {verb}")
    return clip(", ".join(parts), 80)


def build_camera(entity_id: str, state, registry: dict, get_state, area_name: str = "", area_id: str = "",
                 motion_seen: dict | None = None) -> dict:
    """motion_seen: when each motion sensor last turned on, as recorded since HA started. A sensor's own last change
    cannot tell motion from a restart or a cloud reconnect, so a quiet sensor shows only motion that was seen."""
    attrs = state.attributes if state is not None else {}
    entries = registry.get("entities", {})
    device = entries.get(entity_id, {}).get("device")
    motion = None
    if device:
        # A true motion sensor first: doorbells also carry a 'ding' sensor of the occupancy class.
        sensors = sorted((MOTION_ORDER.index(info.get("device_class")), e) for e, info in entries.items()
                         if info.get("device") == device and e.startswith("binary_sensor.")
                         and info.get("device_class") in MOTION_CLASSES and not info.get("disabled"))
        sensor = get_state(sensors[0][1]) if sensors else None
        if sensor is not None and sensor.state not in UNAVAILABLE:
            changed = epoch(getattr(sensor, "last_changed", None))
            active = sensor.state == "on"
            seen = (motion_seen or {}).get(sensors[0][1])
            motion = {"active": active, "changed_at": changed if active else (int(seen) if seen else None)}
    friendly = attrs.get("friendly_name") or entity_id
    camera = {"id": entity_id, "name": clean_name(friendly, area_name), "area": area_id,
              "live": bool(int(_num(attrs.get("supported_features")) or 0) & CAMERA_STREAM), "motion": motion,
              "available": state is not None and state.state != "unavailable"}
    if clean_name(friendly) != camera["name"]:
        camera["full"] = clean_name(friendly)
    return camera


def weather_card(state, forecast=None) -> dict | None:
    if state is None or state.state in UNAVAILABLE:
        return None
    attrs = state.attributes
    text, icon = WEATHER.get(state.state, (clip(readable(state.state), 40), "weather-cloudy"))
    temp = _num(attrs.get("temperature"))
    low, high = forecast or (None, None)
    return {"temp": None if temp is None else round(temp), "unit": str(attrs.get("temperature_unit") or "")[:4],
            "condition": text, "icon": icon, "low": low, "high": high}


def forecast_range(response, entity_id: str) -> tuple:
    """(low, high) of today's entry in a weather.get_forecasts response."""
    try:
        today = response[entity_id]["forecast"][0]
        low, high = _num(today.get("templow")), _num(today.get("temperature"))
    except (KeyError, IndexError, TypeError, AttributeError):
        return None, None
    return (None if low is None else round(low)), (None if high is None else round(high))


def build_home(settings: dict, get_state, registry: dict, unit: str = "°C", forecast=None, motion_seen: dict | None = None,
               own_tv: str | None = None, restreams: dict | None = None) -> dict:
    """What one TV may see. Holds no time-relative values, so its rev changes only when something real does."""
    areas, entries = registry.get("areas", {}), registry.get("entities", {})
    flagged = set(settings.get("attention_when_on", []))
    devices, facts, watched = [], {}, set()
    for entity_id in select_entities(settings, registry, own_tv):
        info = entries.get(entity_id, {})
        area = info.get("area") if info.get("area") in areas else ""
        state = get_state(entity_id)
        device = build_device(entity_id, state, settings, area, areas.get(area, {}).get("name", ""), info.get("device_class"),
                              entity_id in flagged, unit)
        devices.append(device)
        dc = (state.attributes.get("device_class") if state is not None else None) or info.get("device_class")
        if device["kind"] in ("lock", "garage", "alarm") or entity_id in flagged or (device["kind"] == "binary" and dc in ATTENTION_CLASSES):
            watched.add(entity_id)
        if area:
            raw = state.state if state is not None else None
            number = _num(state.attributes.get("current_temperature")) if device["kind"] == "climate" and state else _num(raw)
            facts.setdefault(area, []).append((device["kind"], (state.attributes.get("device_class") if state else None)
                                               or info.get("device_class"), raw, number))
    cameras = []
    for entity_id in settings.get("cameras", []):
        info = entries.get(entity_id, {})
        area = info.get("area") if info.get("area") in areas else ""
        camera = build_camera(entity_id, get_state(entity_id), registry, get_state, areas.get(area, {}).get("name", ""), area,
                              motion_seen)
        if camera["available"] and entity_id in (restreams or {}):
            camera["live"] = True  # Plays through its companion's restream.
        cameras.append(camera)
    used = {d["area"] for d in devices} | {c["area"] for c in cameras}
    area_list = [{"id": a, "name": clip(areas[a].get("name", a), 60), "icon": str(areas[a].get("icon") or "").removeprefix("mdi:"),
                  "summary": _summary(facts.get(a, []))} for a in area_order(areas) if a in used]
    visible = {d["id"] for d in devices}
    scenes = []
    for entity_id in settings.get("scenes", []):
        state = get_state(entity_id)
        if state is not None:
            domain = entity_id.split(".")[0]
            scenes.append({"id": entity_id, "name": clip(state.attributes.get("friendly_name") or entity_id, 60),
                           "icon": _icon(domain, domain, None, state.state, state.attributes), "kind": domain})
    weather = settings.get("weather") or ""
    return {"title": settings.get("title", "Home"), "weather": weather_card(get_state(weather), forecast) if weather else None,
            "areas": area_list, "devices": devices, "cameras": cameras,
            "favorites": [f for f in settings.get("favorites", []) if f in visible], "scenes": scenes,
            "library": settings.get("library", True) is True,
            # Not reporting needs a look only where it could hide a problem; a bulb on a wall switch is often off.
            "attention": [d["id"] for d in devices if d["severity"] == "unknown" and d["id"] in watched]
            + [d["id"] for d in devices if d["severity"] == "attention"]}


def rev_of(home: dict) -> str:
    return digest(json.dumps(home, sort_keys=True, separators=(",", ":")))[:16]


def _given(value) -> float | None:
    """A JSON number from the TV; strings and booleans are never coerced."""
    return _num(value) if isinstance(value, (int, float)) else None


def _percent(value) -> int | None:
    number = _given(value)
    return int(number) if number is not None and number == int(number) and 0 <= number <= 100 else None


def control_call(home: dict, settings: dict, entity_id, action, value, confirmed, get_state):
    """The one HA service call a TV control request maps to, as (domain, service, data, guarded), or an error code.
    Only actions the entity's current control block offers are possible; nothing else is ever dispatched."""
    if not isinstance(entity_id, str) or not isinstance(action, str) or settings.get("allow_control", True) is not True:
        return "not_allowed"
    domain = entity_id.split(".")[0]
    state = get_state(entity_id)
    device = next((d for d in home["devices"] if d["id"] == entity_id), None)
    if device is None:
        if action != "run" or entity_id not in {s["id"] for s in home["scenes"]}:
            return "not_allowed"
        if state is None or not _available(KINDS[domain], state.state):
            return "unavailable"
        if domain == "script" and state.state == "on":
            return "busy"
        return (*RUNNABLE[domain], {"entity_id": entity_id}, False)
    block = device["control"]
    if block is None:
        return "not_allowed" if state is not None and _available(device["kind"], state.state) else "unavailable"
    guarded = bool(block.get("guarded")) and action != "lock"
    if guarded and (settings.get("allow_guarded") is not True or confirmed is not True):
        return "not_allowed"
    data = {"entity_id": entity_id}
    attrs = state.attributes
    if action in ("toggle", "on", "off") and block.get("toggle"):
        return domain, {"toggle": "toggle", "on": "turn_on", "off": "turn_off"}[action], data, False
    if action == "brightness" and block.get("brightness") is not None:
        pct = _percent(value)
        if pct is None:
            return "invalid_value"
        return ("light", "turn_off", data, False) if pct == 0 else ("light", "turn_on", {**data, "brightness_pct": pct}, False)
    if action == "temp" and block.get("temps"):
        if value not in COLOR_TEMPS:
            return "invalid_value"
        low, high = _num(attrs.get("min_color_temp_kelvin")) or 2000, _num(attrs.get("max_color_temp_kelvin")) or 6500
        return "light", "turn_on", {**data, "color_temp_kelvin": int(max(low, min(high, COLOR_TEMPS[value])))}, False
    if action == "speed" and block.get("speed") is not None:
        pct = _percent(value)
        return "invalid_value" if pct is None else ("fan", "set_percentage", {**data, "percentage": pct}, False)
    if action in ("open", "close", "stop") and block.get(action) is True:
        return "cover", f"{action}_cover", data, guarded
    if action == "position" and block.get("position") is not None:
        pct = _percent(value)
        return "invalid_value" if pct is None else ("cover", "set_cover_position", {**data, "position": pct}, guarded)
    if action in ("lock", "unlock") and block.get(action) is True:
        return "lock", action, data, guarded
    if action == "target" and block.get("target") is not None:
        number, step = _given(value), block["step"] or 1
        if number is None:
            return "invalid_value"
        number = _short(round(number / step) * step)
        if not block["min"] <= number <= block["max"]:
            return "invalid_value"
        return "climate", "set_temperature", {**data, "temperature": number}, False
    if action == "mode" and "modes" in block:
        return ("climate", "set_hvac_mode", {**data, "hvac_mode": value}, False) if value in block["modes"] else "invalid_value"
    if action == "play_pause" and device["kind"] == "media":
        return "media_player", "media_play_pause", data, False
    if action == "next" and block.get("next") is True:
        return "media_player", "media_next_track", data, False
    if action == "volume" and block.get("volume") is not None:
        pct = _percent(value)
        return "invalid_value" if pct is None else ("media_player", "volume_set", {**data, "volume_level": pct / 100}, False)
    if action == "run" and block.get("run") is True:
        if domain == "script" and state.state == "on":
            return "busy"
        return (*RUNNABLE[domain], data, False)
    return "not_allowed"


def clamp_width(value) -> int:
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 5:
        return max(160, min(1920, int(value)))
    return 640


class RateLimit:
    """Bounded sliding window without retaining arbitrary client identifiers."""
    def __init__(self, limit: int, seconds: int):
        self.limit = limit
        self.seconds = seconds
        self.hits: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        while self.hits and self.hits[0] <= now - self.seconds:
            self.hits.popleft()
        if len(self.hits) >= self.limit:
            return False
        self.hits.append(now)
        return True


class AddressRateLimit:
    """A window per network address under one shared cap, so one noisy device cannot lock out the others.
    Addresses live only in memory, at most `addresses` of them, least recently seen dropped first."""
    def __init__(self, per_address: int, total: int, seconds: int, addresses: int = 256):
        self.per_address = per_address
        self.seconds = seconds
        self.addresses = addresses
        self.total = RateLimit(total, seconds)
        self.windows: OrderedDict[str, RateLimit] = OrderedDict()

    def allow(self, address: str | None, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        key = str(address or "")
        window = self.windows.pop(key, None) or RateLimit(self.per_address, self.seconds)
        self.windows[key] = window
        while len(self.windows) > self.addresses:
            self.windows.popitem(last=False)
        return window.allow(now) and self.total.allow(now)


class ActionGate:
    """Record before awaiting HA, preventing duplicate dispatch; guarded actions at most one per 3 s."""
    def __init__(self):
        self.recent: dict[str, float] = {}
        self.last_guarded = -100.0

    def check(self, request_id, now: float | None = None) -> str | None:
        now = time.monotonic() if now is None else now
        self.recent = {key: at for key, at in self.recent.items() if now - at < 600}
        if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
            return "invalid_request"
        return "duplicate" if request_id in self.recent else None

    def claim(self, request_id, guarded: bool = False, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        problem = self.check(request_id, now)
        if problem:
            return problem
        if guarded and now - self.last_guarded < 3:
            return "busy"
        self.recent[request_id] = now
        if guarded:
            self.last_guarded = now
        return "accepted"


class PendingPairings:
    """Discovery pairings waiting for their TV, keyed by config flow id. Holds hashes only, at most `limit`."""
    def __init__(self, limit: int = 8):
        self.limit = limit
        self.items: OrderedDict[str, dict] = OrderedDict()

    def _prune(self, now):
        for key in [k for k, item in self.items.items() if item["expires"] <= now and item["token_hash"] is None]:
            del self.items[key]

    def add(self, key: str, code: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._prune(now)
        self.items.pop(key, None)
        self.items[key] = {"pair_hash": digest(code), "expires": now + PAIR_TTL, "token_hash": None}
        while len(self.items) > self.limit:
            self.items.popitem(last=False)

    def claim(self, code: str, token: str, now: float | None = None) -> str | None:
        """The flow whose code this is, now holding the token's hash; each code works once."""
        now = time.time() if now is None else now
        self._prune(now)
        for key, item in self.items.items():
            if item["token_hash"] is None and matches(code, item["pair_hash"]):
                item.update(pair_hash="", token_hash=digest(token))
                return key
        return None

    def token_hash(self, key: str) -> str | None:
        item = self.items.get(key)
        return item["token_hash"] if item else None

    def discard(self, key: str) -> None:
        self.items.pop(key, None)


# --- Roku ECP replies. Parsed with bounded patterns, never a general XML parser on LAN input. ---

APP_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
RETURN_APP_RE = re.compile(r"^(?=.{1,40}$)[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")  # Includes inputs such as tvinput.hdmi1.
SERIAL_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")
# The sideloaded build is titled "Foyer"; a store title may add to it ("Foyer for Home Assistant", "Foyer: ...").
def is_app_name(name: str) -> bool:
    name = name.casefold()
    return name == "foyer" or name.startswith(("foyer ", "foyer:"))


def _tag(xml: str, tag: str) -> str:
    found = re.search(rf"<{tag}>([^<]{{1,200}})</{tag}>", xml)
    return " ".join(html.unescape(found.group(1)).split()) if found else ""


def parse_device_info(xml: str) -> dict | None:
    serial = _tag(xml, "serial-number")
    if not SERIAL_RE.fullmatch(serial):
        return None
    return {"serial": serial, "name": clip(_tag(xml, "user-device-name") or _tag(xml, "friendly-model-name") or "Roku TV", 40)}


def parse_apps(xml: str) -> list:
    apps = []
    for attrs, name in re.findall(r"<app\b([^>]{0,300})>([^<]{0,200})</app>", xml):
        found = re.search(r'\bid="([^"]{1,40})"', attrs)
        if found:
            apps.append((found.group(1), " ".join(html.unescape(name).split())))
    return apps


def find_app(apps: list) -> str | None:
    """The Foyer app's id, whether installed from the store or side-loaded as 'dev'."""
    return next((app_id for app_id, name in apps if is_app_name(name) and APP_ID_RE.fullmatch(app_id)), None)


HOME_SCREEN = ("home", "Home")  # Returning to it is a Home key press, not a launch.


def parse_active_app(xml: str) -> tuple | None:
    """(id, name) of the app in front, HOME_SCREEN for Roku's home screen, None when it cannot be told.
    The home screen has no id on older Roku OS and reports itself as 'Roku Dynamic Menu' on newer ones."""
    if re.search(r"<app>\s*Roku\s*</app>", xml):
        return HOME_SCREEN
    apps = parse_apps(xml)
    if not apps or not RETURN_APP_RE.fullmatch(apps[0][0]):
        return None
    if apps[0][0] == "562859" or apps[0][1].casefold() == "roku dynamic menu":
        return HOME_SCREEN
    return apps[0][0], clip(apps[0][1] or apps[0][0], 40)


# --- Library: the TV browses Home Assistant's own media ("My media") and asks to play an item. ---

LIBRARY_ROOT = "media-source://media_source"
LIBRARY_LIMIT = 300


def library_id(value) -> str | None:
    """A media id the TV may browse or play: inside My media only, never another media source."""
    if value in (None, ""):
        return LIBRARY_ROOT
    if not isinstance(value, str) or len(value) > 500 or not value.startswith(LIBRARY_ROOT):
        return None
    rest = value[len(LIBRARY_ROOT):]
    if rest and not rest.startswith("/") or "/../" in rest + "/" or "\\" in rest or any(ord(c) < 32 for c in rest):
        return None
    return value


def library_kind(can_expand: bool, media_class, content_type) -> str:
    if can_expand:
        return "folder"
    for kind in ("video", "audio", "image"):
        if str(content_type or "").startswith(kind + "/") or media_class in ({"video": {"video", "movie", "episode", "tv_show"},
                                                                               "audio": {"music", "track", "podcast", "album"},
                                                                               "image": {"image"}}[kind]):
            return kind
    return "other"


def library_item(content_id: str, title, can_expand: bool, can_play: bool, media_class=None, content_type=None) -> dict | None:
    kind = library_kind(can_expand, media_class, content_type)
    if library_id(content_id) is None or (kind == "other") or (kind != "folder" and not can_play):
        return None
    return {"id": content_id, "title": clip(str(title or content_id.rsplit("/", 1)[-1]), 80), "kind": kind}


# --- Restreams: a camera whose companion sensor publishes a stream address (Ring-MQTT's convention:
# stream_Source, with still_Image_URL naming the camera entity) can play live even if its own entity cannot. ---

RESTREAM_SCHEMES = ("rtsp://", "rtsps://", "http://", "https://")


def restream_sources(sensors) -> dict:
    """{camera entity id: stream address} from (entity_id, attributes) pairs of sensors."""
    found = {}
    for _entity_id, attrs in sensors:
        source, still = attrs.get("stream_Source"), attrs.get("still_Image_URL")
        if not isinstance(source, str) or not isinstance(still, str) or not source.startswith(RESTREAM_SCHEMES):
            continue
        if len(source) > 500 or any(ord(c) < 33 for c in source):
            continue
        match = re.search(r"states\.(camera\.[a-z0-9_]+)\.", still)
        if match:
            found.setdefault(match.group(1), source)
    return found


# --- Casting: HA queues one command per TV; the TV collects it on its next poll. ---

PLAYER_STATES = {"idle", "playing", "paused", "buffering", "error", "view"}
COMMAND_TTL = 60  # An unopened TV must not act on a stale cast later.
COMMAND_ID_RE = re.compile(r"^c[0-9a-f]{16}$")
PATH_PART_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HA_MASTER_RE = re.compile(r"^(/api/hls/[^/?]+/)master_playlist\.m3u8")
FORMATS = (  # (MIME prefix or exact, URL suffix, kind, Roku streamFormat)
    ("application/vnd.apple.mpegurl", ".m3u8", "video", "hls"), ("application/x-mpegurl", None, "video", "hls"),
    ("audio/mpegurl", None, "video", "hls"), ("audio/x-mpegurl", None, "video", "hls"),
    ("application/dash+xml", ".mpd", "video", "dash"), ("video/mp4", ".mp4", "video", "mp4"),
    (None, ".m4v", "video", "mp4"), ("video/quicktime", ".mov", "video", "mp4"),
    ("video/x-matroska", ".mkv", "video", "mkv"), ("audio/mpeg", ".mp3", "audio", "mp3"),
    ("audio/mp4", ".m4a", "audio", "mp4"), ("audio/x-m4a", None, "audio", "mp4"), ("audio/aac", ".aac", "audio", "mp4"),
    ("audio/flac", ".flac", "audio", "flac"), ("audio/x-flac", None, "audio", "flac"),
    ("audio/wav", ".wav", "audio", "wav"), ("audio/x-wav", None, "audio", "wav"),
)


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
# HA media types (media_content_type without a "/"), used only when nothing more specific is known.
TYPE_HINTS = {"image": "image", "music": "audio", "podcast": "audio", "video": "video", "movie": "video",
              "episode": "video", "tvshow": "video", "channel": "video"}
TTS_SOURCE = "media-source://tts/"
TITLE_LIMIT = 80  # The TV rejects longer titles.


def media_format(mime: str | None, url: str, hint: str | None = None) -> tuple[str, str]:
    """Return (kind, Roku streamFormat). Explicit MIME, then file suffix, then the HA media type."""
    mime = (mime or "").split(";")[0].strip().lower()
    path = url.split("?")[0].lower()
    if mime.startswith("image/"):
        return "image", ""
    for prefix, suffix, kind, fmt in FORMATS:
        if (prefix and mime == prefix) or (suffix and path.endswith(suffix)):
            return kind, fmt
    if mime.startswith("audio/"):
        return "audio", ""
    if mime in {"", "application/octet-stream"}:
        if path.endswith(IMAGE_SUFFIXES):
            return "image", ""
        if TYPE_HINTS.get((hint or "").lower()):
            return TYPE_HINTS[hint.lower()], ""
    return "video", ""


def short_title(text) -> str:
    return clip(text, TITLE_LIMIT)


def spoken_text(media_id: str) -> str | None:
    """The message of a text-to-speech media source ID, which is far better as a title than its random file name."""
    if not media_id.startswith(TTS_SOURCE):
        return None
    message = parse_qs(urlsplit(media_id).query).get("message", [""])[0]
    return short_title(message) or None


def new_command(kind: str, **fields) -> dict:
    return {"id": "c" + secrets.token_hex(8), "kind": kind, **fields}


def view_of(command: dict) -> dict | None:
    """Pictures stay on the TV until replaced: dashboards, camera stills and cast photos."""
    if command["kind"] == "view" or (command["kind"] == "play" and command.get("media") == "image"):
        return {"id": command["id"], "path": command.get("path"), "camera": command.get("camera")}
    return None


def camera_command(settings: dict, visible, camera, title, message="", timeout=30, actions=()) -> dict:
    """A doorbell-style takeover: a selected camera plus at most three visible devices to act on."""
    if camera not in settings.get("cameras", []):
        raise ValueError("camera_not_selected")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 <= timeout <= 600:
        raise ValueError("invalid_timeout")
    actions = [a for a in dict.fromkeys(actions or []) if a in visible][:3]
    return new_command("camera", camera=camera, title=clip(title, TITLE_LIMIT), message=clip(message, 120), timeout=timeout,
                       actions=actions, back=None)


def notify_command(settings: dict, title, message="", camera=None, timeout=10) -> dict:
    if not clip(title, TITLE_LIMIT):
        raise ValueError("invalid_title")
    if camera is not None and camera not in settings.get("cameras", []):
        raise ValueError("camera_not_selected")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 <= timeout <= 600:
        raise ValueError("invalid_timeout")
    return new_command("notify", title=clip(title, TITLE_LIMIT), message=clip(message, 120), camera=camera, timeout=timeout)


def build_view_path(dashboard: str, view: str | None) -> str:
    if not PATH_PART_RE.fullmatch(dashboard or "") or (view and not PATH_PART_RE.fullmatch(view)):
        raise ValueError("invalid_view")
    return f"/{dashboard}/{view}" if view else f"/{dashboard}"


class TvLink:
    """What one paired TV last reported, plus the cast command waiting for it."""
    def __init__(self):
        self.command: dict | None = None
        self.command_at = 0.0
        self.view: dict | None = None  # {"id", "path", "camera"} of the picture the TV confirmed it is showing
        self.next_view: dict | None = None  # the picture queued in `command`, until the TV collects it
        self.delivered: dict | None = None  # the command last handed to the TV, until it acknowledges it
        self.state = "idle"
        self.announcing = False
        self.media_id: str | None = None  # last media (not a picture) the TV confirmed playing
        self.caps: dict | None = None  # what the TV reported it decodes (playback.tv_caps)
        self.announcement_id: str | None = None  # last announcement the TV confirmed playing
        self.position: int | None = None
        self.duration: int | None = None
        self.last_seen: float | None = None
        self.remote: str | None = None
        self.app_id: str | None = None
        self.wake = "unknown"
        self.listeners: set = set()
        self.returns: OrderedDict[str, tuple] = OrderedDict()  # command id: (app id, app name) it took the screen from

    def remember_return(self, command_id: str, app_id: str, name: str) -> None:
        self.returns[command_id] = (app_id, name)
        while len(self.returns) > 8:
            self.returns.popitem(last=False)

    def take_return(self, command_id) -> tuple | None:
        return self.returns.pop(command_id, None) if isinstance(command_id, str) else None

    def send(self, command: dict, now: float | None = None) -> None:
        self.command = command
        self.command_at = time.monotonic() if now is None else now
        self.next_view = view_of(command)

    def cancel(self) -> None:
        """Drop a queued command the TV has not collected."""
        self.command = None
        self.next_view = None

    def forget(self) -> None:
        """Revoked: nothing queued, nothing shown, no address to wake."""
        self.cancel()
        self.view = self.delivered = None
        self.state = "idle"
        self.announcing = False
        self.media_id = self.announcement_id = None
        self.position = self.duration = self.last_seen = None
        self.remote = self.app_id = None
        self.returns.clear()

    def frame_view(self, cast_id) -> dict | None:
        """The view a frame request may fetch: the one on screen, or one the TV is just picking up."""
        for view in (self.view, self.next_view, self.delivered and view_of(self.delivered)):
            if view is not None and view["id"] == cast_id:
                return view
        return None

    def pending(self, now: float | None = None) -> dict | None:
        now = time.monotonic() if now is None else now
        if self.command and now - self.command_at > COMMAND_TTL:
            self.cancel()  # An uncollected picture never replaces the one still on screen.
        if self.command is None:
            return None
        self.delivered = self.command
        # A view's dashboard path and camera stay in HA; its frames come through the token-checked frame endpoint.
        hidden = {"path", "camera"} if self.command["kind"] == "view" else set()
        return {k: v for k, v in self.command.items() if k not in hidden}

    def online(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return self.last_seen is not None and now - self.last_seen < 20

    def _commit(self, command: dict) -> None:
        """The TV has acted on `command`: update what HA believes is on screen."""
        view = view_of(command)
        if view is not None:
            self.view = view
        elif command["kind"] == "play" and command.get("announce"):
            self.announcement_id = command["id"]  # Plays over the picture, which the TV shows again afterwards.
        elif command["kind"] == "play":
            self.view = None
            self.media_id = command["id"]
        elif command["kind"] == "stop":
            self.view = None

    def report(self, query, remote: str | None, app_id: str | None, now: float | None = None) -> bool:
        """Record a status poll. Returns True when the TV's visible state changed."""
        now = time.monotonic() if now is None else now
        before = (self.online(now), self.state, self.announcing, self.position, self.duration)
        self.last_seen = now
        self.remote = remote or self.remote
        if isinstance(app_id, str) and APP_ID_RE.fullmatch(app_id):
            self.app_id = app_id
        # The TV repeats its last ack in every poll; each command is committed once.
        ack = query.get("ack")
        if self.command and isinstance(ack, str) and ack == self.command["id"]:
            self._commit(self.command)
            self.command = self.next_view = self.delivered = None
        elif self.delivered and isinstance(ack, str) and ack == self.delivered["id"]:
            # The TV acted on a command a newer one has since replaced in the queue: it is on screen now.
            self._commit(self.delivered)
            self.delivered = None
        state = query.get("player")
        if state in PLAYER_STATES:
            self.state = state
            # A report sent before the TV collected a pending cast says nothing about that cast.
            if state in {"idle", "error"} and self.command is None:
                self.view = None
            # A TV still showing a cast HA no longer knows (restart, reload) is told to stop.
            if state == "view" and self.view is None and self.command is None:
                self.send(new_command("stop"), now)
        self.announcing = query.get("ann") == "1"
        self.position, self.duration = _seconds(query.get("pos")), _seconds(query.get("dur"))
        return before != (self.online(now), self.state, self.announcing, self.position, self.duration)


def _seconds(value) -> int | None:
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 7:
        return int(value)
    return None
