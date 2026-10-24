"""Cast target for one paired TV: HA media, cameras, TTS and rendered dashboards."""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import timedelta

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseMedia,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from .media import Media, NeedsConversion, NoRoom, converted_id
from .playback import DEFAULT_CAPS
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from .core import (
    DOMAIN, HA_MASTER_RE, LIBRARY_ROOT, build_view_path, camera_command, media_format, new_command, notify_command, short_title, spoken_text,
)

CAMERA_SOURCE = "media-source://camera/"
CAMERA_RE = re.compile(r"^camera\.[a-z0-9_]+$")
SCAN_INTERVAL = timedelta(seconds=10)  # Re-evaluates "TV app closed" when polls stop.
PLAYBACK = {"playing": MediaPlayerState.PLAYING, "paused": MediaPlayerState.PAUSED,
            "buffering": MediaPlayerState.BUFFERING}
NOT_PAIRED = "This TV is not paired. In Foyer's settings for it, generate a pairing code and enter it on the TV."
NOT_SELECTED = "Select this camera for the TV in Foyer's settings first."


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([FoyerPlayer(hass.data[DOMAIN], entry)])


def _is_playable(item: BrowseMedia) -> bool:
    kind = item.media_content_type or ""
    return item.can_expand or kind.startswith(("audio/", "video/", "image/")) or kind in {
        "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml"}


class FoyerPlayer(MediaPlayerEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = True
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA
        | MediaPlayerEntityFeature.MEDIA_ANNOUNCE | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PLAY)

    def __init__(self, runtime, entry):
        self.runtime = runtime
        self.entry = entry
        self.link = runtime.link(entry.entry_id)
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=entry.title,
                                            manufacturer="Foyer", model="Roku app")
        # Title, content type and app name per command; shown only once the TV confirms it is on screen.
        self._meta: OrderedDict[str, tuple] = OrderedDict()
        self._position_at = None

    async def async_added_to_hass(self):
        self.link.listeners.add(self._changed)
        self.async_on_remove(lambda: self.link.listeners.discard(self._changed))
        self.runtime.players[self.entry.entry_id] = self
        self.async_on_remove(lambda: self.runtime.players.pop(self.entry.entry_id, None))

    def _changed(self):
        self._position_at = dt_util.utcnow()
        self.async_write_ha_state()

    @property
    def state(self):
        link = self.link
        if not link.online():
            return MediaPlayerState.OFF
        if link.state in PLAYBACK and (link.view is None or link.announcing):
            return PLAYBACK[link.state]
        if link.view is not None or link.state == "view":
            return MediaPlayerState.ON
        return MediaPlayerState.IDLE

    def _shown(self):
        """(title, content type, app name) of what the TV confirmed it is showing or playing."""
        link = self.link
        state = self.state
        if state in PLAYBACK.values():
            key = link.announcement_id if link.announcing else link.media_id
        elif state == MediaPlayerState.ON and link.view is not None:
            key = link.view["id"]
        else:
            key = None
        return self._meta.get(key, (None, None, None))

    def _remember(self, command_id, title, content_type, app):
        """Titles for recent commands, never dropping one the TV is showing or about to show."""
        self._meta[command_id] = (title, content_type, app)
        excess = len(self._meta) - 16
        if excess > 0:
            link = self.link
            keep = {command_id, link.media_id, link.announcement_id}
            keep.update(item["id"] for item in (link.view, link.next_view, link.command, link.delivered) if item)
            for key in [key for key in self._meta if key not in keep][:excess]:
                del self._meta[key]

    @property
    def app_name(self):
        state = self.state
        if state == MediaPlayerState.OFF:
            return None
        if state == MediaPlayerState.ON:
            return self._shown()[2] or "Foyer"
        return "Foyer"

    @property
    def media_title(self):
        return self._shown()[0]

    @property
    def media_content_type(self):
        return self._shown()[1]

    @property
    def media_position(self):
        return self.link.position if self.state in PLAYBACK.values() else None

    @property
    def media_duration(self):
        return self.link.duration if self.state in PLAYBACK.values() else None

    @property
    def media_position_updated_at(self):
        return self._position_at if self.media_position is not None else None

    @property
    def extra_state_attributes(self):
        # "blocked" means Roku's "Control by mobile apps" setting refused the wake request.
        return {"tv_wake": self.link.wake}

    def _require_pairing(self):
        if not self.runtime.paired(self.entry.entry_id):
            raise ServiceValidationError(NOT_PAIRED)

    def _send(self, kind, meta=None, **fields):
        command = new_command(kind, **fields)
        if kind == "view":  # Frames come through the TV's own authenticated API, never the renderer.
            command.update(media="image", url=f"/api/foyer/frame?cast={command['id']}", auth=True)
        elif kind == "play":
            command["auth"] = False  # Always explicit: a TV app before 0.4 halts on a missing flag.
        if meta is not None:
            self._remember(command["id"], *meta)
        self.runtime.cast(self.entry.entry_id, command)
        return command

    async def async_play_media(self, media_type, media_id, **kwargs):
        self._require_pairing()
        extra = kwargs.get("extra") or {}
        announce = bool(kwargs.get("announce"))
        spoken = spoken_text(media_id)
        mime = hint = path = None
        source_id = media_id
        if media_source.is_media_source_id(media_id):
            try:
                resolved = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
                media_id, mime = resolved.url, resolved.mime_type
                path = getattr(resolved, "path", None)
            except media_source.Unresolvable:
                # WebRTC-only cameras have no HLS for Roku; fall back to their still image.
                camera = media_id.removeprefix(CAMERA_SOURCE)
                if camera == media_id or not CAMERA_RE.fullmatch(camera):
                    raise
                media_id = f"/api/camera_proxy_stream/{camera}"
        elif isinstance(media_type, str) and "/" in media_type:
            mime = media_type
        elif isinstance(media_type, str):
            hint = media_type
        if media_id.startswith("/api/camera_proxy_stream/"):
            # Roku cannot play MJPEG. Refreshing stills come through the TV's own token-checked
            # frame endpoint, so no long-lived signed camera URL is handed out.
            camera = media_id.removeprefix("/api/camera_proxy_stream/").split("?")[0]
            camera_state = self.hass.states.get(camera)
            title = short_title(extra.get("title") or (camera_state.name if camera_state else camera))
            self._send("view", (title, MediaType.IMAGE, "Camera"), camera=camera, title=title, refresh=2)
            return
        # Roku's player never leaves a master playlist whose only variant lacks RESOLUTION, which is
        # what HA's stream component serves. Its media playlist plays directly (LL-HLS included).
        media_id = HA_MASTER_RE.sub(r"\1playlist.m3u8", media_id)
        kind, fmt = media_format(mime, media_id, hint)
        title = short_title(extra.get("title") or spoken or media_id.split("?")[0].rsplit("/", 1)[-1] or "Media")
        content_type = {"image": MediaType.IMAGE, "audio": MediaType.MUSIC}.get(kind, MediaType.VIDEO)
        if path is not None and kind != "image" and not announce and source_id.startswith(LIBRARY_ROOT + "/"):
            # A file in My media is checked first, so formats Roku cannot open still play (see playback.py).
            media: Media = self.runtime.media
            caps = self.link.caps or DEFAULT_CAPS
            plan, summary = await media.choose(str(path), caps)
            if plan == "convert":
                raise NeedsConversion(source_id, str(path), summary)
            if plan == "no_room":
                raise NoRoom
            if plan == "converted":
                resolved = await media_source.async_resolve_media(self.hass, converted_id(source_id), self.entity_id)
                media_id, fmt = resolved.url, "mkv"
            elif plan == "repackage":
                session = await media.start(self.entry.entry_id, source_id, str(path), summary, caps)
                # The TV may restart it elsewhere (?start=), so it keeps its place by the file, not the address.
                command = self._send("play", (title, content_type, "Foyer"), media=kind, format="hls", url=media.url(session),
                                     title=title, refresh=0, key=source_id[-200:], length=int(summary["duration"]), restart=True)
                session.command_id = command["id"]
                return
        url = async_process_play_media_url(self.hass, media_id, allow_relative_url=True)
        if not url.startswith(("/", "http://", "https://")):
            raise HomeAssistantError("Foyer can only play HTTP(S) media")
        if kind == "image":
            self._send("play", (title, content_type, "Picture"), media=kind, format=fmt, url=url, title=title, refresh=0)
            return
        # An announcement plays over whatever is on screen, which the TV then shows again.
        fields = {"announce": True} if announce else {}
        self._send("play", (title, content_type, "Foyer"), media=kind, format=fmt, url=url, title=title, refresh=0, **fields)

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        return await media_source.async_browse_media(self.hass, media_content_id, content_filter=_is_playable)

    async def async_media_stop(self):
        if not self.link.online():
            self.runtime.cancel(self.entry.entry_id)  # Never open the app just to stop it.
            return
        self._send("stop")

    async def async_media_pause(self):
        if self.link.online():
            self._send("pause")

    async def async_media_play(self):
        if self.link.online():
            self._send("resume")

    async def async_show_view(self, dashboard_path="lovelace", view_path=None, refresh_seconds=3):
        self._require_pairing()
        settings = self.entry.options or self.entry.data["settings"]
        if not settings.get("renderer_url"):
            raise ServiceValidationError("Add a dashboard renderer URL in Foyer settings before casting dashboards.")
        try:
            path = build_view_path(dashboard_path, view_path)
        except ValueError as err:
            raise ServiceValidationError("Use the dashboard and view paths from the dashboard's URL.") from err
        title = short_title(path.strip("/"))
        self._send("view", (title, "dashboard", "Dashboard"), path=path, title=title, refresh=max(2, min(60, int(refresh_seconds))))

    def _settings(self):
        return self.entry.options or self.entry.data["settings"]

    async def async_show_camera(self, camera, title=None, message="", timeout=30, actions=(), return_to_app=True):
        """Doorbell takeover: the TV shows the camera, and can hand the screen back to the app it interrupted."""
        self._require_pairing()
        settings = self._settings()
        state = self.hass.states.get(camera)
        visible = {device["id"] for device in self.runtime.home(settings)["devices"]}
        try:
            command = camera_command(settings, visible, camera, title or (state.name if state else camera), message,
                                     timeout, actions)
        except ValueError as err:
            raise ServiceValidationError(NOT_SELECTED) from err
        if return_to_app:
            app = await self.runtime.active_app(self.entry.entry_id)
            if app is not None:
                self.link.remember_return(command["id"], *app)
                command["back"] = app[1]
        self.runtime.cast(self.entry.entry_id, command)

    async def async_notify(self, title, message="", camera=None, timeout=10, launch=False):
        self._require_pairing()
        try:
            command = notify_command(self._settings(), title, message, camera, timeout)
        except ValueError as err:
            raise ServiceValidationError(NOT_SELECTED if camera else "Enter a title for the notification.") from err
        if launch or self.link.online():  # A notice for a closed app is dropped, never shown late.
            self.runtime.cast(self.entry.entry_id, command, launch=launch)
