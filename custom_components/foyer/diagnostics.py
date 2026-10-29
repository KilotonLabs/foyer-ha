"""Download diagnostics for one paired TV. Users attach this to public issues, so it is an allowlist: switches,
counts and versions only. No secrets, tokens, addresses, titles, area names or entity IDs."""
import time

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.loader import async_get_integration

from .core import DOMAIN, LIMITS

SWITCHES = ("allow_control", "allow_guarded", "library", "convert")


def _settings(settings: dict) -> dict:
    found = {key: settings.get(key) for key in SWITCHES if isinstance(settings.get(key), bool)}
    found.update({key: len(settings[key]) for key in LIMITS if isinstance(settings.get(key), list)})
    found.update(weather=bool(settings.get("weather")), renderer=bool(settings.get("renderer_url")),
                 renderer_secret=bool(settings.get("renderer_secret")))
    return found


async def async_get_config_entry_diagnostics(hass, entry):
    runtime = hass.data[DOMAIN]
    integration = await async_get_integration(hass, DOMAIN)
    media = runtime.media
    ffmpeg = await hass.async_add_executor_job(media.ffmpeg)
    ffprobe = await hass.async_add_executor_job(media.ffprobe)
    link = runtime.links.get(entry.entry_id)
    tv = None
    if link is not None:
        caps = link.caps
        tv = {"online": link.online(), "state": link.state, "wake": link.wake, "app_id": link.app_id,
              "seconds_since_seen": None if link.last_seen is None else round(time.monotonic() - link.last_seen),
              "caps": None if caps is None else {"video": sorted(caps["video"]), "audio": sorted(caps["audio"]), "uhd": caps["uhd"]},
              "command_waiting": link.command is not None, "announcing": link.announcing,
              "showing_camera": bool(link.view and link.view.get("camera"))}
    settings = entry.options or entry.data.get("settings") or {}
    return {
        "foyer": str(integration.version),
        "home_assistant": HA_VERSION,
        "entry": {"version": entry.version, "paired": bool(entry.data.get("token_hash")),
                  "pairing_open": entry.data.get("pair_expires", 0) > time.time(),
                  "options_saved": bool(entry.options), "settings": _settings(settings)},
        "tv": tv,
        "media": {"ffmpeg": ffmpeg is not None, "ffprobe": ffprobe is not None, "sessions": len(media.sessions),
                  "queued": len(media.queue), "converting": media.converting is not None},
    }
