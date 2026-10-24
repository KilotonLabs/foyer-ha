"""Small authenticated API for paired TVs; never a generic service-call proxy."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
import time

from aiohttp import ClientError, ClientTimeout, web
from yarl import URL
from homeassistant.components.http import HomeAssistantView

from .core import (
    MOTION_CLASSES, LIBRARY_LIMIT, library_id, library_item, restream_sources, short_title,
    COMMAND_ID_RE, HA_MASTER_RE, POLL_SECONDS, PROTOCOL, ActionGate, AddressRateLimit, PendingPairings, RateLimit,
    HOME_SCREEN, TvLink, build_home, clamp_width, control_call, digest, forecast_range, matches, parse_active_app, rev_of,
)

from .media import ConvertError, Media, NeedsConversion, NoRoom
from .playback import DEFAULT_CAPS, estimate_minutes, playback_plan, tv_caps

_LOGGER = logging.getLogger(__name__)
MAX_FRAME = 4_000_000
MAX_ECP_REPLY = 262_144
# A cold dashboard can take ~40 s to render, but an unreachable renderer must fail fast so the TV says so.
RENDERER_TIMEOUT = ClientTimeout(total=45, sock_connect=5)
WAKE_GRACE = 4  # Seconds for a foreground TV to poll after an input event.
FORECAST_AGE = 1800
STREAM_FEATURE = 2


def lan_host(value, loopback=True):
    """An ECP host for a LAN address, else None: HA must never be steered at anything beyond the local network."""
    try:
        address = ipaddress.ip_address(value or "")
    except ValueError:
        return None
    if not (address.is_private or address.is_loopback) or address.is_link_local or (address.is_loopback and not loopback):
        return None
    return f"[{address}]" if address.version == 6 else str(address)


async def read_capped(response, limit):
    data = bytearray()  # read(n) returns only what has arrived; collect the whole body.
    async for chunk in response.content.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("reply_too_large")
    return bytes(data)


async def ecp(hass, host, path, method="post"):
    """(status, body) of a Roku ECP request, or (None, b"") when the TV did not answer."""
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    try:
        async with asyncio.timeout(4):
            # Already encoded: re-quoting would turn server=http%3A%2F%2F... into a raw "http://", which the TV rejects (404).
            url = URL(f"http://{host}:8060{path}", encoded=True)
            async with async_get_clientsession(hass).request(method, url) as response:
                return response.status, (await read_capped(response, MAX_ECP_REPLY) if method == "get" else b"")
    except (ClientError, OSError, TimeoutError, ValueError) as err:
        _LOGGER.debug("Roku ECP request failed: %s", type(err).__name__)
        return None, b""


class Runtime:
    def __init__(self, hass):
        self.hass = hass
        self.motion_seen = {}  # entity_id -> when a motion sensor last turned on (see watch_motion)
        self.players = {}  # entry_id -> the TV's media player, which plays library items
        self.restreams = {}  # camera entity id -> (source, Stream)
        self.media = Media(hass, self.still_playing)
        self.entries = {}
        self.gates = {}
        # Per network address, so one device sending bad codes or tokens cannot lock out the others.
        self.pair_limit = AddressRateLimit(10, 60, 60)
        self.reject_limit = AddressRateLimit(30, 240, 60)
        self.tv_limits = {}
        self.frame_limits = {}
        self.camera_limits = {}
        self.control_limits = {}
        self.links = {}
        self.pending = PendingPairings()
        self.pair_events = {}
        self.forecasts = {}
        self.forecast_tasks = set()

    def link(self, entry_id):
        return self.links.setdefault(entry_id, TvLink())

    def still_playing(self, session) -> bool:
        """Whether a TV is still on a repackaged stream (playing, paused or buffering it)."""
        link = self.links.get(session.entry_id)
        return (link is not None and link.online() and link.media_id == session.command_id
                and link.state in ("playing", "paused", "buffering"))

    def notify(self, entry_id):
        if entry_id in self.links:  # A late wake must not recreate state for an unloaded TV.
            for listener in list(self.links[entry_id].listeners):
                listener()

    def paired(self, entry_id):
        entry = self.entries.get(entry_id)
        return entry is not None and bool(entry.data.get("token_hash"))

    def cast(self, entry_id, command, launch=None):
        """Queue a command for the TV and nudge it over Roku ECP so it acts now."""
        link = self.link(entry_id)
        link.send(command)
        self.notify(entry_id)
        # Only something to show may open a closed app; stop, pause, resume and plain notices never do.
        if launch is None:
            launch = command["kind"] in {"play", "view", "camera"}
        self.hass.async_create_background_task(self.wake(entry_id, launch), "foyer_wake")
        entry = self.entries.get(entry_id)
        if command["kind"] == "view" and command.get("path") and entry is not None:
            # Opening a view can take longer than one frame request; start it now.
            settings = entry.options or entry.data["settings"]
            self.hass.async_create_background_task(self.warm(settings, command["path"]), "foyer_warm")

    def cancel(self, entry_id):
        """Drop whatever is queued for a TV that is not polling, without waking it."""
        if entry_id in self.links:
            self.links[entry_id].cancel()
            self.notify(entry_id)

    def unpair(self, entry_id):
        """Revoked: forget the TV's address so nothing can launch the app on it any more."""
        if entry_id in self.links:
            self.links[entry_id].forget()
            self.notify(entry_id)

    async def warm(self, settings, path):
        try:
            await self.fetch_frame(settings, path)
        except Exception as err:  # The TV's own frame requests report failures.
            _LOGGER.debug("Dashboard warm-up failed: %s", type(err).__name__)

    def host(self, entry_id):
        link = self.links.get(entry_id)
        if link is None or not self.paired(entry_id):
            return None
        return lan_host(link.remote)

    async def wake(self, entry_id, launch=True):
        link = self.links.get(entry_id)
        host = self.host(entry_id)
        if host is None:
            return
        sent = time.monotonic()
        # A polling TV is already in the foreground: deliver an input event, never relaunch.
        if link.online():
            link.wake = await self._ecp(host, "/input?contentId=cast&mediaType=special")
            await asyncio.sleep(WAKE_GRACE)
            if (link.last_seen or 0) >= sent or link.command is None or not launch:
                self.notify(entry_id)
                return
        elif not launch:
            return
        # The app closed (or never answered): launch it, which also delivers the cast.
        if link.app_id and self.paired(entry_id):
            link.wake = await self._ecp(host, f"/launch/{link.app_id}?contentId=cast&mediaType=special")
        self.notify(entry_id)

    async def _ecp(self, host, path):
        status, _ = await ecp(self.hass, host, path)
        # "blocked" is Roku's "Control by mobile apps" setting refusing network control.
        return "unreachable" if status is None else {200: "ok", 204: "ok", 403: "blocked"}.get(status, "error")

    async def active_app(self, entry_id):
        """(id, name) of another app in front on the TV, to hand the screen back to later; None otherwise."""
        host = self.host(entry_id)
        if host is None:
            return None
        status, data = await ecp(self.hass, host, "/query/active-app", "get")
        app = parse_active_app(data.decode("utf-8", "replace")) if status == 200 else None
        link = self.links.get(entry_id)
        if app is None or link is None or app[0] == link.app_id or app[1].casefold() == "foyer":
            return None
        return app

    async def return_to(self, entry_id, command_id):
        """Relaunch the app a camera takeover interrupted. None when nothing is remembered for that command."""
        link = self.links.get(entry_id)
        app = link.take_return(command_id) if link else None
        if app is None:
            return None
        host = self.host(entry_id)
        if host is None:
            return "unreachable"
        return await self._ecp(host, "/keypress/Home" if app == HOME_SCREEN else f"/launch/{app[0]}")

    def remember_tv(self, entry):
        """Keep the TV's address across HA restarts so a cast can launch a closed app."""
        link = self.link(entry.entry_id)
        if link.remote and (link.remote, link.app_id) != (entry.data.get("tv_host"), entry.data.get("tv_app")):
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, "tv_host": link.remote, "tv_app": link.app_id})

    async def camera_frame(self, entity_id, width=None):
        from homeassistant.components.camera import async_get_image
        image = await async_get_image(self.hass, entity_id, timeout=10, width=width)
        return image.content, image.content_type

    async def camera_stream(self, entity_id):
        from homeassistant.components.camera import async_request_stream
        async with asyncio.timeout(15):
            return await async_request_stream(self.hass, entity_id, "hls")

    async def fetch_frame(self, settings, path):
        """Ask the dashboard renderer for the current picture of a view."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        base = settings.get("renderer_url", "").rstrip("/")
        if not base:
            raise ValueError("no_renderer")
        headers = {"X-Foyer-Secret": settings.get("renderer_secret", "")}
        async with asyncio.timeout(46):
            async with async_get_clientsession(self.hass).get(base + "/frame", params={"path": path}, headers=headers,
                                                              timeout=RENDERER_TIMEOUT) as response:
                if response.status != 200 or response.content_type not in {"image/jpeg", "image/png"}:
                    raise ValueError("renderer_failed")
                return await read_capped(response, MAX_FRAME), response.content_type

    def registry_view(self):
        """Areas and entity registry facts core.py needs, as plain data."""
        from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, floor_registry as fr
        floors = fr.async_get(self.hass)
        devices = dr.async_get(self.hass)
        areas = {}
        for area in ar.async_get(self.hass).async_list_areas():
            floor = floors.async_get_floor(area.floor_id) if area.floor_id else None
            areas[area.id] = {"name": area.name, "icon": area.icon, "level": floor.level if floor else None}
        entities = {}
        for item in er.async_get(self.hass).entities.values():
            device = devices.async_get(item.device_id) if item.device_id else None
            roku = next((ident for domain, ident in device.identifiers if domain == "roku"), None) if device else None
            entities[item.entity_id] = {"roku": roku,
                "area": item.area_id or (device.area_id if device else None), "device": item.device_id,
                "device_class": item.device_class or item.original_device_class, "hidden": item.hidden_by is not None,
                "disabled": item.disabled_by is not None, "category": item.entity_category is not None, "platform": item.platform}
        return {"areas": areas, "entities": entities}

    def temperature_unit(self):
        return getattr(getattr(self.hass.config, "units", None), "temperature_unit", "°C")

    def forecast(self, entity_id):
        """Today's (low, high), refreshed in the background at most every 30 min and never awaited by a TV request."""
        if not entity_id:
            return None
        cached = self.forecasts.get(entity_id)
        if (cached is None or time.monotonic() - cached["at"] > FORECAST_AGE) and entity_id not in self.forecast_tasks:
            self.forecast_tasks.add(entity_id)
            self.hass.async_create_background_task(self.refresh_forecast(entity_id), "foyer_forecast")
        return (cached["low"], cached["high"]) if cached else None

    async def refresh_forecast(self, entity_id):
        low = high = None
        try:
            async with asyncio.timeout(20):
                response = await self.hass.services.async_call("weather", "get_forecasts", {"entity_id": entity_id, "type": "daily"},
                                                               blocking=True, return_response=True)
            low, high = forecast_range(response, entity_id)
        except Exception as err:  # A weather service without daily forecasts still shows current conditions.
            _LOGGER.debug("Weather forecast unavailable: %s", type(err).__name__)
        finally:
            self.forecasts[entity_id] = {"at": time.monotonic(), "low": low, "high": high}
            self.forecast_tasks.discard(entity_id)

    def watch_motion(self):
        """Record when motion sensors turn on; cheap, and filtered before any work is scheduled."""
        from homeassistant.const import EVENT_STATE_CHANGED
        from homeassistant.core import callback

        @callback
        def is_motion(data) -> bool:
            new = data.get("new_state")
            return (new is not None and new.state == "on" and new.domain == "binary_sensor"
                    and new.attributes.get("device_class") in MOTION_CLASSES)

        @callback
        def seen(event) -> None:
            self.motion_seen[event.data["entity_id"]] = time.time()

        return self.hass.bus.async_listen(EVENT_STATE_CHANGED, seen, event_filter=is_motion)

    def restream_map(self):
        return restream_sources((s.entity_id, s.attributes) for s in self.hass.states.async_all("sensor"))

    async def restream(self, camera, source):
        """HLS for a camera through its companion's stream address; HA's stream stops it once nobody watches."""
        from homeassistant.components.camera import DynamicStreamSettings
        from homeassistant.components.stream import create_stream
        known = self.restreams.get(camera)
        if known is None or known[0] != source:
            if known is not None:
                await known[1].stop()
            options = {"rtsp_transport": "tcp"} if source.startswith(("rtsp://", "rtsps://")) else {}
            known = (source, create_stream(self.hass, source, options, DynamicStreamSettings(), stream_label=camera))
            self.restreams[camera] = known
        stream = known[1]
        stream.add_provider("hls")
        await stream.start()
        return stream.endpoint_url("hls")

    def home(self, settings, own_tv=None):
        return build_home(settings, self.hass.states.get, self.registry_view(), self.temperature_unit(),
                          self.forecast(settings.get("weather") or ""), self.motion_seen, own_tv, self.restream_map())

    async def entry_ready(self, token_hash, timeout=10):
        """Wait for the entry a discovery pairing creates, so the TV's first status poll already succeeds."""
        async with asyncio.timeout(timeout):
            while not any(entry.data.get("token_hash") == token_hash for entry in self.entries.values()):
                await asyncio.sleep(0.05)

    def allow(self, entry):
        return self.tv_limits.setdefault(entry.entry_id, RateLimit(60, 60)).allow()

    def allow_control(self, entry):
        return self.control_limits.setdefault(entry.entry_id, RateLimit(40, 10)).allow()

    def authenticate(self, header):
        if not isinstance(header, str) or not header.startswith("Bearer "):
            return None
        token = header[7:]
        if len(token) > 128:
            return None
        for entry in self.entries.values():
            if matches(token, entry.data.get("token_hash", "")):
                return entry
        return None


def reply(data, status=200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def error(code, status):
    return reply({"error": code}, status)


async def body(request):
    if request.content_type != "application/json":
        raise ValueError("json_required")
    data = bytearray()
    async with asyncio.timeout(5):
        async for chunk in request.content.iter_chunked(1024):
            data.extend(chunk)
            if len(data) > 4096:
                raise ValueError("body_too_large")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("object_required")
    return value


class FoyerView(HomeAssistantView):
    url = "/api/foyer/{operation}"
    name = "api:foyer"
    requires_auth = False  # Our revocable TV token is NOT an HA bearer token.

    def __init__(self, runtime):
        self.runtime = runtime

    def _rejected(self, request):
        if not self.runtime.reject_limit.allow(request.remote):
            return error("rate_limited", 429)
        return error("pairing_required", 401)

    async def get(self, request, operation):
        if operation not in {"status", "frame", "camera"}:
            return error("not_found", 404)
        runtime = self.runtime
        entry = runtime.authenticate(request.headers.get("Authorization"))
        if entry is None:
            return self._rejected(request)
        settings = entry.options or entry.data["settings"]
        link = runtime.link(entry.entry_id)
        if operation == "frame":
            return await self.frame(request, entry, settings, link)
        if operation == "camera":
            return await self.camera(request, entry, settings)
        if not runtime.allow(entry):
            return error("rate_limited", 429)
        if link.report(request.query, request.remote, request.headers.get("X-Foyer-App")):
            runtime.notify(entry.entry_id)
        runtime.remember_tv(entry)
        home = runtime.home(settings, getattr(entry, "unique_id", None))
        data = {"protocol": PROTOCOL, "now": int(time.time()), "rev": rev_of(home), "poll_seconds": POLL_SECONDS,
                "command": link.pending()}
        if request.query.get("rev") != data["rev"]:
            data["home"] = home
        return reply(data)

    async def frame(self, request, entry, settings, link):
        limiter = self.runtime.frame_limits.setdefault(entry.entry_id, RateLimit(40, 60))
        view = link.frame_view(request.query.get("cast"))
        if view is None or not (view.get("camera") or view.get("path")):
            return error("not_casting", 404)
        if not limiter.allow():
            return error("rate_limited", 429)
        try:
            if view.get("camera"):
                data, content_type = await self.runtime.camera_frame(view["camera"])
            else:
                data, content_type = await self.runtime.fetch_frame(settings, view["path"])
        except Exception as err:  # Renderer faults must never surface details to the TV.
            _LOGGER.debug("Dashboard frame unavailable: %s", type(err).__name__)
            return error("frame_unavailable", 502)
        return web.Response(body=data, content_type=content_type, headers={"Cache-Control": "no-store"})

    async def camera(self, request, entry, settings):
        if not self.runtime.camera_limits.setdefault(entry.entry_id, RateLimit(1200, 60)).allow():
            return error("rate_limited", 429)
        camera = request.query.get("entity")
        if camera not in settings.get("cameras", []):
            return error("not_found", 404)
        try:
            data, content_type = await self.runtime.camera_frame(camera, clamp_width(request.query.get("width")))
        except Exception as err:  # Camera faults must never surface details to the TV.
            _LOGGER.debug("Camera still unavailable: %s", type(err).__name__)
            return error("frame_unavailable", 502)
        return web.Response(body=data, content_type=content_type, headers={"Cache-Control": "no-store"})

    async def post(self, request, operation):
        runtime = self.runtime
        # TV requests do not carry browser Origin headers. No cross-origin API.
        if request.headers.get("Origin") or operation not in {"pair", "control", "stream", "return", "browse", "play", "convert"}:
            return error("forbidden", 403)
        if operation == "pair":
            if not runtime.pair_limit.allow(request.remote):
                return error("rate_limited", 429)
        else:
            entry = runtime.authenticate(request.headers.get("Authorization"))
            if entry is None:
                return self._rejected(request)
            if not (runtime.allow(entry) if operation in ("stream", "browse") else runtime.allow_control(entry)):
                return error("rate_limited", 429)
        try:
            data = await body(request)
        except (ValueError, UnicodeError, TimeoutError, RecursionError):
            return error("invalid_request", 400)
        if operation == "pair":
            return await self.pair(data)
        # Reading a slow request body yields to revocation, re-pairing or unload.
        entry = runtime.authenticate(request.headers.get("Authorization"))
        if entry is None:
            return error("pairing_required", 401)
        settings = entry.options or entry.data["settings"]
        caps = tv_caps(data.get("caps"))
        link = runtime.link(entry.entry_id)
        if caps is not None and caps != link.caps:  # What this TV decodes, sent with library requests; casts use it too.
            _LOGGER.debug("%s decodes video %s, audio %s%s", entry.title, sorted(caps["video"]), sorted(caps["audio"]),
                          ", 4K" if caps["uhd"] else "")
            link.caps = caps
        handler = {"control": self.control, "stream": self.stream, "return": self.go_back, "browse": self.browse,
                   "play": self.play, "convert": self.convert}[operation]
        return await handler(entry, settings, data)

    async def pair(self, data):
        runtime = self.runtime
        code = data.get("code")
        if not isinstance(code, str) or len(code) != 8 or not code.isascii() or not code.isdigit():
            return error("invalid_code", 400)
        token = secrets.token_urlsafe(32)
        for entry in runtime.entries.values():
            if entry.data.get("pair_expires", 0) > time.time() and matches(code, entry.data.get("pair_hash", "")):
                updated = {**entry.data, "token_hash": digest(token), "pair_hash": "", "pair_expires": 0}
                runtime.hass.config_entries.async_update_entry(entry, data=updated)
                runtime.gates.pop(entry.entry_id, None)
                return reply({"protocol": PROTOCOL, "token": token})
        flow_id = runtime.pending.claim(code, token)
        if flow_id is None:
            return error("invalid_or_expired_code", 401)
        if flow_id in runtime.pair_events:
            runtime.pair_events[flow_id].set()
        try:
            await runtime.entry_ready(digest(token))
        except TimeoutError:
            _LOGGER.debug("Discovery pairing entry not ready yet")
        return reply({"protocol": PROTOCOL, "token": token})

    async def control(self, entry, settings, data):
        runtime = self.runtime
        gate = runtime.gates.setdefault(entry.entry_id, ActionGate())
        request_id = data.get("request_id")
        seen = gate.check(request_id)
        if seen == "invalid_request":
            return error("invalid_request", 400)
        if seen == "duplicate":
            return reply({"protocol": PROTOCOL, "result": "already_requested"})
        call = control_call(runtime.home(settings, getattr(entry, "unique_id", None)), settings, data.get("entity"), data.get("action"), data.get("value"),
                            data.get("confirmed"), runtime.hass.states.get)
        if isinstance(call, str):
            return error(call, 403 if call == "not_allowed" else 409)
        domain, service, service_data, guarded = call
        claimed = gate.claim(request_id, guarded)  # Before awaiting HA, so a concurrent duplicate is never dispatched.
        if claimed == "duplicate":
            return reply({"protocol": PROTOCOL, "result": "already_requested"})
        if claimed != "accepted":
            return error(claimed, 409)
        try:
            # 'accepted' means HA ran the service call; it does not claim the device reached its target.
            async with asyncio.timeout(8):
                await runtime.hass.services.async_call(domain, service, service_data, blocking=True)
        except TimeoutError:
            return error("outcome_unknown", 504)
        except Exception as err:  # Never echo HA's error text to the TV.
            _LOGGER.debug("Foyer control failed: %s", type(err).__name__)
            return error("action_failed", 502)
        return reply({"protocol": PROTOCOL, "result": "accepted"})

    async def stream(self, entry, settings, data):
        camera = data.get("camera")
        if not isinstance(camera, str) or camera not in settings.get("cameras", []):
            return error("not_found", 404)
        state = self.runtime.hass.states.get(camera)
        if state is None or state.state == "unavailable":
            return error("no_stream", 409)
        source = self.runtime.restream_map().get(camera)
        if source is None and not int(state.attributes.get("supported_features") or 0) & STREAM_FEATURE:
            return error("no_stream", 409)
        try:
            url = await (self.runtime.restream(camera, source) if source else self.runtime.camera_stream(camera))
        except Exception as err:  # The TV keeps refreshing stills instead.
            if source:  # A companion stream was found, so a failure here is worth seeing.
                _LOGGER.warning("Could not start the live stream for %s: %s", camera, type(err).__name__)
            _LOGGER.debug("Camera stream unavailable: %s", type(err).__name__)
            return error("no_stream", 409)
        # Roku's player never leaves a master playlist whose only variant lacks RESOLUTION; the media playlist plays.
        return reply({"protocol": PROTOCOL, "url": HA_MASTER_RE.sub(r"\1playlist.m3u8", url), "format": "hls"})

    async def browse(self, entry, settings, data):
        """One folder of My media: its folders and the items the TV can play."""
        from homeassistant.components import media_source
        from .media_player import _is_playable
        media_id = library_id(data.get("id"))
        if settings.get("library", True) is not True or media_id is None:
            return error("not_found", 404)
        try:
            folder = await media_source.async_browse_media(self.runtime.hass, media_id, content_filter=_is_playable)
        except Exception as err:  # A missing or unreadable folder is just not found.
            _LOGGER.debug("Library browse failed: %s", type(err).__name__)
            return error("not_found", 404)
        path = self.runtime.media.folder(media_id)
        if path is not None:  # Converted copies of files since deleted or replaced go too.
            self.runtime.hass.async_add_executor_job(self.runtime.media.tidy, path)
        items = []
        for child in folder.children or []:
            item = library_item(child.media_content_id, child.title, child.can_expand, child.can_play, child.media_class,
                                child.media_content_type)
            if item is not None:
                note = self.runtime.media.note(child.media_content_id)
                if note:
                    item["note"] = note
                items.append(item)
            if len(items) >= LIBRARY_LIMIT:
                break
        return reply({"protocol": PROTOCOL, "id": media_id, "title": str(folder.title or "My media")[:80], "items": items})

    async def play(self, entry, settings, data):
        """Play a My media item on this TV, through its own media player (the same path as casting)."""
        media_id = library_id(data.get("id"))
        player = self.runtime.players.get(entry.entry_id)
        if settings.get("library", True) is not True or media_id is None or media_id == library_id(None) or player is None:
            return error("not_found", 404)
        try:
            await player.async_play_media(None, media_id)
        except NeedsConversion as needed:
            job = self.runtime.media.job(media_id)
            if job is not None:
                return reply({"error": "converting", "note": self.runtime.media.note(media_id)}, 409)
            return reply({"error": "needs_conversion", "convert": settings.get("convert") is True,
                          "minutes": estimate_minutes(needed.summary, self.runtime.media.factor)}, 409)
        except NoRoom:
            return error("no_room", 409)
        except Exception as err:
            _LOGGER.debug("Library play failed: %s", type(err).__name__)
            return error("unplayable", 409)
        return reply({"protocol": PROTOCOL, "result": "accepted"})

    async def convert(self, entry, settings, data):
        """Convert a My media file whose video the TV cannot play (opt-in, see the "convert" setting)."""
        from homeassistant.components import media_source
        media_id = library_id(data.get("id"))
        if settings.get("library", True) is not True or media_id is None or media_id == library_id(None):
            return error("not_found", 404)
        if settings.get("convert") is not True:
            return error("not_allowed", 403)
        media = self.runtime.media
        try:
            resolved = await media_source.async_resolve_media(self.runtime.hass, media_id, None)
        except Exception as err:
            _LOGGER.debug("Convert: cannot resolve: %s", type(err).__name__)
            return error("not_found", 404)
        path = getattr(resolved, "path", None)
        summary = await media.probe(str(path)) if path is not None else None
        caps = self.runtime.link(entry.entry_id).caps or DEFAULT_CAPS
        if summary is None or playback_plan(summary, caps) != "convert":
            return error("not_needed", 409)
        title = short_title(media_id.rsplit("/", 1)[-1])
        try:
            result = await media.convert(media_id, str(path), title, summary, caps)
        except ConvertError as err:
            return error(str(err), 409)
        return reply({"protocol": PROTOCOL, **result})

    async def go_back(self, entry, settings, data):
        command = data.get("command")
        if not isinstance(command, str) or not COMMAND_ID_RE.fullmatch(command):
            return error("invalid_request", 400)
        result = await self.runtime.return_to(entry.entry_id, command)
        if result is None:
            return error("not_found", 404)
        if result != "ok":
            return error("action_failed", 502)
        return reply({"protocol": PROTOCOL, "result": "accepted"})
