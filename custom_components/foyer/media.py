"""The ffmpeg work behind playback.py: repackaged streams for files the TV cannot open, and opt-in conversion.

A repackaged stream is HLS written by ffmpeg into a hidden work folder on the media's disk and served from it. ffmpeg works only a
couple of minutes ahead of the TV, then waits, so a film costs no more disk or processing than the part watched.
A jump past what is ready restarts it from there (the TV asks with ?start=). Conversion runs one file at a time
at low priority and saves the copy in a hidden ".foyer" folder beside the original, which the library hides.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
from collections import OrderedDict
from pathlib import Path

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.exceptions import HomeAssistantError

from .playback import (
    MAX_AUDIO, SEGMENT_SECONDS, convert_args, convert_megapixels, estimate_minutes, master_playlist, media_playlist,
    media_summary, needs_master, playback_plan, playlist_segments, repackage_args, subtitle_segment, text_subtitles,
    timeline_shift, vtt_cues,
)

_LOGGER = logging.getLogger(__name__)
WORK_PREFIX = "foyer-media-"
PROBE_SECONDS = 30
FIRST_SEGMENT_SECONDS = 25
AHEAD_PAUSE = 20  # Segments (two minutes) past the last one the TV asked for: ffmpeg waits there.
AHEAD_RESUME = 10
# Segments (15 minutes) kept behind the last one the TV asked for; older ones are deleted, so a long film never
# fills the disk. The TV jumps back natively only 10 minutes (see CastLayer's jumpTo); further back restarts.
KEEP_BEHIND = 150
SPARE_BYTES = 512 * 1024 * 1024
IDLE_SECONDS = 120  # A stream the TV is no longer playing ends after this long without requests.
MAX_START = 86400
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,40}$")
NAME_RE = re.compile(r"^(\d{1,4})-(?:(video\.m3u8)|(seg\d{5}\.ts)|sub(\d)\.m3u8|sub(\d)_(\d{5})\.vtt|(audio\d\.m3u8)|(a\d_\d{5}\.ts))$")
PLAYLIST = "application/vnd.apple.mpegurl"
NOTIFICATION = "foyer_convert"
# Used when ffprobe is missing: PyAV ships with Home Assistant's stream component. In a child process, so a
# damaged file can never take Home Assistant down with it.
PYAV_PROBE = r"""
import json, sys
import av
streams = []
with av.open(sys.argv[1]) as container:
    for stream in container.streams:
        context = stream.codec_context
        item = {"index": stream.index, "codec_type": stream.type, "codec_name": getattr(context, "name", None),
                "tags": dict(stream.metadata), "disposition": {}}
        try:
            flags, kinds = stream.disposition, av.stream.Disposition
            item["disposition"] = {"default": bool(flags & kinds.default), "forced": bool(flags & kinds.forced),
                                   "attached_pic": bool(flags & kinds.attached_pic)}
        except Exception:
            pass
        if stream.type == "video":
            item.update(width=context.width, height=context.height, pix_fmt=getattr(context, "pix_fmt", None))
        elif stream.type == "audio":
            layout = getattr(context, "layout", None)
            item["channels"] = getattr(layout, "nb_channels", None) or getattr(context, "channels", 2)
        streams.append(item)
    duration = container.duration / 1000000 if container.duration else 0
    print(json.dumps({"format": {"format_name": container.format.name, "duration": duration,
                                 "size": getattr(container, "size", 0)}, "streams": streams}))
"""


class NeedsConversion(HomeAssistantError):
    """The file's video cannot play on the TV; only a converted copy will."""

    def __init__(self, media_id: str, path: str, summary: dict):
        super().__init__("Roku can't play this file's video. Open it in Foyer on the TV to convert it.")
        self.media_id, self.path, self.summary = media_id, path, summary


class NoRoom(HomeAssistantError):
    """A file that needs repackaging, with no disk (not memory) to repackage it on."""

    def __init__(self):
        super().__init__("Not enough free space on the media drive to prepare this file for the TV.")


class ConvertError(Exception):
    """A conversion that cannot start: "no_space", "read_only" or "no_ffmpeg"."""


def converted_path(path: str) -> Path:
    original = Path(path)
    return original.parent / ".foyer" / (original.name + ".mkv")


def converted_id(media_id: str) -> str:
    folder, _, name = media_id.rpartition("/")
    return f"{folder}/.foyer/{name}.mkv"


def _signal(process, which) -> None:
    number = getattr(signal, which, None)
    if number is not None and process is not None and process.returncode is None:
        try:
            os.kill(process.pid, number)
        except OSError:
            pass


async def _tail(stream) -> list:
    """The last lines a process wrote, reading to the end so it never blocks on a full pipe."""
    kept = b""
    while chunk := await stream.read(65536):
        kept = (kept + chunk)[-4096:]
    return kept.decode(errors="replace").strip().splitlines()


def _nice() -> list:
    found = shutil.which("nice")
    return [found, "-n", "15"] if found else []


def in_memory(path: str) -> bool:
    """Whether a folder is on a RAM file system (Home Assistant OS mounts /tmp in the Core container that way)."""
    try:
        with open("/proc/mounts", encoding="utf-8") as handle:
            mounts = [line.split()[1:3] for line in handle if len(line.split()) > 2]
    except OSError:
        return False  # Not Linux: no tmpfs to fear.
    real, best, kind = os.path.realpath(path), "", ""
    for point, fstype in mounts:
        point = point.replace("\\040", " ")
        if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) > len(best):
            best, kind = point, fstype
    return kind in ("tmpfs", "ramfs")


def stream_bytes(summary: dict) -> int:
    """Disk a repackaged stream holds at most: the segments kept behind the TV and prepared ahead, plus margin."""
    seconds = (KEEP_BEHIND + AHEAD_PAUSE + 10) * SEGMENT_SECONDS
    duration, size = summary["duration"], summary["size"]
    held = size if duration <= 0 else min(size, size * seconds / duration)
    return int(held * 1.2) + SPARE_BYTES


def tidy_copies(folder: str, busy: str | None = None) -> None:
    """Deletes converted copies whose original is gone or newer, and leftovers of an interrupted conversion."""
    copies = os.path.join(folder, ".foyer")
    try:
        names = os.listdir(copies)
    except OSError:
        return
    for name in names:
        path = os.path.join(copies, name)
        if path == busy:
            continue
        original = os.path.join(folder, name.removesuffix(".part").removesuffix(".mkv"))
        try:
            stale = name.endswith(".part") or not name.endswith(".mkv") or not os.path.isfile(original) or (
                os.stat(original).st_mtime > os.stat(path).st_mtime)
            if stale and os.path.isfile(path):
                os.remove(path)
        except OSError:
            continue
    if busy is not None and os.path.dirname(busy) == copies:
        return  # A conversion is about to write here.
    try:
        os.rmdir(copies)  # Only when empty.
    except OSError:
        pass


class Session:
    """One TV's repackaged stream. A new start point is a new generation, in a new folder."""

    def __init__(self, token, entry_id, media_id, path, summary, caps):
        self.token, self.entry_id, self.media_id, self.path = token, entry_id, media_id, path
        self.summary, self.caps = summary, caps
        self.command_id: str | None = None
        self.generation = 0
        self.start = -1
        self.process = None
        self.directory: str | None = None
        self.requested = -1
        self.trimmed = 0  # Segments before this one are deleted.
        self.paused = False
        self.used = time.monotonic()
        self.subtitles = len(text_subtitles(summary))
        self.master = needs_master(summary)
        self.lock = asyncio.Lock()


class Media:
    def __init__(self, hass, alive=lambda session: False):
        self.hass = hass
        self.alive = alive  # Whether the TV still plays a session's stream.
        self.roots: dict[str, str] = {}  # work folder per media folder
        self.sessions: dict[str, Session] = {}
        self.by_tv: dict[str, str] = {}
        self.probes: OrderedDict = OrderedDict()
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.converting = None
        self.factor: float | None = None  # Conversion speed measured on this box (see estimate_minutes).
        self.sweeper = None

    # --- Tools ---

    def ffmpeg(self) -> str | None:
        manager = self.hass.data.get("ffmpeg")  # HA's ffmpeg integration, when it names a binary.
        return shutil.which(getattr(manager, "binary", None) or "ffmpeg")

    def ffprobe(self) -> str | None:
        binary = self.ffmpeg()
        if binary:
            sibling = Path(binary).with_name("ffprobe" + Path(binary).suffix)
            if sibling.is_file():
                return str(sibling)
        return shutil.which("ffprobe")

    async def _json(self, args) -> dict | None:
        try:
            process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                           stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        except OSError:
            return None
        try:
            async with asyncio.timeout(PROBE_SECONDS):
                output, _ = await process.communicate()
        except TimeoutError:
            process.kill()
            await process.wait()
            return None
        if process.returncode != 0 or len(output) > 4_000_000:
            return None
        try:
            return json.loads(output)
        except ValueError:
            return None

    async def probe(self, path: str) -> dict | None:
        """What a file holds, or None when it cannot be read. Remembered while the file is unchanged."""
        try:
            info = await self.hass.async_add_executor_job(os.stat, path)
        except OSError:
            return None
        key = (path, info.st_mtime_ns, info.st_size)
        if key in self.probes:
            self.probes.move_to_end(key)
            return self.probes[key]
        raw = None
        ffprobe = await self.hass.async_add_executor_job(self.ffprobe)
        if ffprobe:
            raw = await self._json([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path])
        if raw is None:
            raw = await self._json([sys.executable, "-I", "-c", PYAV_PROBE, path])
        summary = media_summary(raw)
        if summary is not None:
            summary["size"] = summary["size"] or info.st_size
            self.probes[key] = summary
            while len(self.probes) > 64:
                self.probes.popitem(last=False)
        return summary

    async def choose(self, path: str, caps: dict) -> tuple[str, dict | None]:
        """("direct" | "repackage" | "no_room" | "converted" | "convert", what the file holds)."""
        summary = await self.probe(path)
        if summary is None:
            return "direct", None  # Unreadable here: the TV tries it as it is and explains a failure.
        plan = playback_plan(summary, caps)
        if plan == "repackage" and not await self.hass.async_add_executor_job(self.ffmpeg):
            plan = "direct"
        if plan == "repackage" and not await self.hass.async_add_executor_job(self._room, path, summary):
            plan = "no_room"
        if plan == "convert":
            await self.hass.async_add_executor_job(self.tidy, os.path.dirname(path))
            if await self.hass.async_add_executor_job(converted_path(path).is_file):
                plan = "converted"
        return plan, summary

    def tidy(self, folder: str) -> None:
        """tidy_copies, sparing the conversion in progress."""
        busy = None
        if self.current is not None:
            target = converted_path(self.current["path"])
            busy = str(target.with_name(target.name + ".part"))
        tidy_copies(folder, busy)

    def folder(self, media_id: str) -> str | None:
        """The folder on disk of a My media folder id (media-source://media_source/<media dir>/<path>)."""
        rest = media_id.removeprefix("media-source://media_source/")
        if rest == media_id:
            return None
        name, _, sub = rest.partition("/")
        base = self.hass.config.media_dirs.get(name)
        if base is None:
            return None
        base = os.path.realpath(base)
        folder = os.path.realpath(os.path.join(base, sub))
        return folder if folder == base or folder.startswith(base + os.sep) else None

    # --- Repackaged streams ---

    def _work_base(self, path: str) -> str | None:
        """A hidden folder in the media folder holding the file, on its disk. Never memory: on Home Assistant OS /tmp
        is RAM, which the segments of a film would fill. None when there is nowhere suitable."""
        real = os.path.realpath(path)
        for folder in self.hass.config.media_dirs.values():
            folder = os.path.realpath(folder)
            try:
                inside = os.path.commonpath([real, folder]) == folder
            except ValueError:
                continue
            if inside and os.access(folder, os.W_OK) and not in_memory(folder):
                return os.path.join(folder, ".foyer-work")
        temp = tempfile.gettempdir()
        return None if in_memory(temp) else temp

    def _room(self, path: str, summary: dict) -> bool:
        base = self._work_base(path)
        if base is None:
            return False
        try:
            free = shutil.disk_usage(base if os.path.isdir(base) else os.path.dirname(base)).free
        except OSError:
            return False
        return free >= stream_bytes(summary)

    def _make_root(self, path: str) -> str:
        base = self._work_base(path)
        if base is None:
            raise OSError("no work folder")
        root = self.roots.get(base)
        if root is None or not os.path.isdir(root):
            os.makedirs(base, exist_ok=True)
            for name in os.listdir(base):  # Left by a Home Assistant that stopped abruptly.
                if name.startswith(WORK_PREFIX):
                    shutil.rmtree(os.path.join(base, name), ignore_errors=True)
            root = self.roots[base] = tempfile.mkdtemp(prefix=WORK_PREFIX, dir=base)
        return root

    async def start(self, entry_id, media_id, path, summary, caps) -> Session:
        await self.end_tv(entry_id)
        token = secrets.token_urlsafe(18)
        session = Session(token, entry_id, media_id, path, summary, caps)
        self.sessions[token] = session
        self.by_tv[entry_id] = token
        # ffmpeg starts when the TV first asks, from where it asks (a resumed film starts mid-way).
        if self.sweeper is None:
            from homeassistant.helpers.event import async_track_time_interval
            from datetime import timedelta
            self.sweeper = async_track_time_interval(self.hass, self._sweep, timedelta(seconds=30))
        return session

    @staticmethod
    def url(session: Session) -> str:
        return f"/api/foyer/media/{session.token}/index.m3u8"

    async def _launch(self, session: Session, start: int) -> None:
        await self._stop(session)
        session.generation += 1
        session.start, session.requested, session.trimmed, session.paused = start, -1, 0, False
        try:
            root = await self.hass.async_add_executor_job(self._make_root, session.path)
            session.directory = await self.hass.async_add_executor_job(
                lambda: tempfile.mkdtemp(prefix=f"g{session.generation}-", dir=root))
        except OSError as err:
            _LOGGER.warning("No folder to repackage a file for the TV in: %s", type(err).__name__)
            return
        args = repackage_args(session.path, session.summary, session.caps, start, session.directory)
        binary = await self.hass.async_add_executor_job(self.ffmpeg)
        try:
            session.process = await asyncio.create_subprocess_exec(
                *_nice(), binary, *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
        except (OSError, TypeError) as err:
            _LOGGER.warning("Could not start ffmpeg to repackage a file for the TV: %s", type(err).__name__)
            session.process = None
            return
        self.hass.async_create_background_task(self._watch(session, session.process), "foyer_repackage")
        self.hass.async_create_background_task(self._pacer(session, session.process), "foyer_repackage_pace")

    async def _pacer(self, session: Session, process) -> None:
        """Holds ffmpeg a couple of minutes ahead of the TV. Checked twice a second: between the TV's requests a
        fast box would otherwise run far ahead."""
        while process.returncode is None and session.process is process:
            directory = session.directory
            text = await self.hass.async_add_executor_job(self._read, f"{directory}/video.m3u8") if directory else None
            if text and session.process is process:
                self._pace(session, len(playlist_segments(text)))
                await self._trim(session, directory)
                if "#EXT-X-ENDLIST" in text:
                    return
            await asyncio.sleep(0.5)

    async def _trim(self, session: Session, directory: str) -> None:
        """Deletes segments the TV has long played (KEEP_BEHIND), video and audio alike."""
        upto = session.requested - KEEP_BEHIND
        if upto <= session.trimmed:
            return
        first, session.trimmed = session.trimmed, upto

        def remove():
            names = [f"seg{n:05d}.ts" for n in range(first, upto)]
            names += [f"a{track}_{n:05d}.ts" for track in range(len(session.summary["audio"][:MAX_AUDIO])) for n in range(first, upto)]
            for name in names:
                try:
                    os.remove(os.path.join(directory, name))
                except OSError:
                    pass
        await self.hass.async_add_executor_job(remove)

    async def _watch(self, session: Session, process) -> None:
        lines = await _tail(process.stderr)
        code = await process.wait()
        if code not in (0, -9, -15) and session.process is process:
            _LOGGER.warning("Repackaging %s for the TV failed: %s", Path(session.path).name, lines[-1][:200] if lines else code)

    async def _stop(self, session: Session) -> None:
        process, directory = session.process, session.directory
        session.process = session.directory = None
        if process is not None and process.returncode is None:
            _signal(process, "SIGCONT")  # A paused process cannot act on being asked to stop.
            process.kill()
            await process.wait()
        if directory:
            await self.hass.async_add_executor_job(lambda: shutil.rmtree(directory, ignore_errors=True))

    async def end(self, token: str) -> None:
        session = self.sessions.pop(token, None)
        if session is None:
            return
        if self.by_tv.get(session.entry_id) == token:
            del self.by_tv[session.entry_id]
        async with session.lock:
            await self._stop(session)
        if not self.sessions:  # Nothing streams: no work folder stays in the media folder.
            await self.hass.async_add_executor_job(self._clear_roots)

    def _clear_roots(self) -> None:
        for base, root in list(self.roots.items()):
            shutil.rmtree(root, ignore_errors=True)
            if os.path.basename(base) == ".foyer-work":  # Never a shared temp folder.
                try:
                    os.rmdir(base)
                except OSError:
                    pass
        self.roots.clear()

    async def end_tv(self, entry_id: str) -> None:
        if entry_id in self.by_tv:
            await self.end(self.by_tv[entry_id])

    async def _sweep(self, _now=None) -> None:
        now = time.monotonic()
        for token, session in list(self.sessions.items()):
            if self.alive(session):
                session.used = now
            elif now - session.used > IDLE_SECONDS:
                await self.end(token)
        if not self.sessions and self.sweeper is not None:
            self.sweeper()
            self.sweeper = None

    def _read(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read(2_000_000)
        except OSError:
            return None

    async def _playlist(self, session: Session, wait: bool) -> str | None:
        """ffmpeg's playlist once it lists a segment; waits for the first one after a (re)start."""
        deadline = time.monotonic() + (FIRST_SEGMENT_SECONDS if wait else 0)
        while True:
            directory = session.directory
            text = await self.hass.async_add_executor_job(self._read, f"{directory}/video.m3u8") if directory else None
            if text and playlist_segments(text):
                return text
            if time.monotonic() >= deadline or session.process is None or session.process.returncode is not None:
                return None
            await asyncio.sleep(0.25)

    def _pace(self, session: Session, produced: int) -> None:
        ahead = produced - 1 - session.requested
        if ahead > AHEAD_PAUSE and not session.paused:
            _signal(session.process, "SIGSTOP")
            session.paused = True
        elif ahead < AHEAD_RESUME and session.paused:
            _signal(session.process, "SIGCONT")
            session.paused = False

    async def serve(self, token: str, name: str, query) -> web.StreamResponse:
        session = self.sessions.get(token) if TOKEN_RE.fullmatch(token) else None
        if session is None:
            raise web.HTTPNotFound
        session.used = time.monotonic()
        if name == "index.m3u8":
            try:
                start = int(query.get("start", "0"))
            except ValueError:
                raise web.HTTPBadRequest from None
            if not 0 <= start <= MAX_START:
                raise web.HTTPBadRequest
            async with session.lock:
                # The same start point again needs a fresh run too once its first segments are deleted (KEEP_BEHIND).
                if start != session.start or session.trimmed > 0:
                    await self._launch(session, start)
                text = await self._playlist(session, True)
            if text is None:
                raise web.HTTPServiceUnavailable
            prefix = f"{session.generation}-"
            body = master_playlist(session.summary, prefix) if session.master else media_playlist(text, prefix)
            return self._text(body, PLAYLIST)
        match = NAME_RE.fullmatch(name)
        _LOGGER.debug("Repackaged stream: the TV asks for %s (generation %s from %s s)", name, session.generation, session.start)
        if match is None or int(match.group(1)) != session.generation or session.directory is None:
            raise web.HTTPNotFound
        prefix = f"{session.generation}-"
        directory = session.directory
        text = await self._playlist(session, False)
        if text is None:
            raise web.HTTPNotFound
        segments = playlist_segments(text)
        if match.group(2):
            self._pace(session, len(segments))
            return self._text(media_playlist(text, prefix), PLAYLIST)
        if match.group(3) or match.group(8):
            if match.group(3):  # The video's segments say how far the TV has got.
                session.requested = max(session.requested, int(match.group(3)[3:8]))
                self._pace(session, len(segments))
                await self._trim(session, directory)
            file = Path(directory) / (match.group(3) or match.group(8))
            if not await self.hass.async_add_executor_job(file.is_file):
                raise web.HTTPNotFound
            return web.FileResponse(file, headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"})
        if match.group(7):
            for _ in range(20):  # Written moments after the video's playlist.
                audio = await self.hass.async_add_executor_job(self._read, f"{directory}/{match.group(7)}")
                if audio and playlist_segments(audio):
                    return self._text(media_playlist(audio, prefix), PLAYLIST)
                await asyncio.sleep(0.25)
            raise web.HTTPNotFound
        track = int(match.group(4) if match.group(4) is not None else match.group(5))
        if track >= session.subtitles:
            raise web.HTTPNotFound
        if match.group(4) is not None:
            return self._text(media_playlist(text, prefix, lambda number: f"sub{track}_{number:05d}.vtt"), PLAYLIST)
        number = int(match.group(6))
        if number >= len(segments):
            raise web.HTTPNotFound
        cues = vtt_cues(await self.hass.async_add_executor_job(self._read, f"{directory}/sub{track}.vtt") or "")
        ended = "#EXT-X-ENDLIST" in text
        return self._text(subtitle_segment(cues, segments, number, session.start - timeline_shift(session.start), ended), "text/vtt")

    @staticmethod
    def _text(body: str, content_type: str) -> web.Response:
        return web.Response(text=body, content_type=content_type, headers={"Cache-Control": "no-store"})

    # --- Conversion (opt-in) ---

    def job(self, media_id: str) -> dict | None:
        if self.current is not None and self.current["media_id"] == media_id:
            return self.current
        return next((job for job in self.queue if job["media_id"] == media_id), None)

    def note(self, media_id: str) -> str | None:
        """What the library shows under a file being converted."""
        job = self.job(media_id)
        if job is None:
            return None
        return f"Converting {job['percent']}%" if job is self.current else "Waiting to convert"

    def _space(self, path: str, size: int) -> str | None:
        target = converted_path(path)
        try:
            target.parent.mkdir(exist_ok=True)
        except OSError:
            return "read_only"
        if not os.access(target.parent, os.W_OK):
            return "read_only"
        if shutil.disk_usage(target.parent).free < size + 1_073_741_824:  # The copy, plus 1 GB to spare.
            return "no_space"
        return None

    async def convert(self, media_id: str, path: str, title: str, summary: dict, caps: dict) -> dict:
        """Queue a conversion; {"result": "converting" | "queued", "ahead": jobs before it, "minutes"}."""
        job = self.job(media_id)
        if job is None:
            if not await self.hass.async_add_executor_job(self.ffmpeg):
                raise ConvertError("no_ffmpeg")
            problem = await self.hass.async_add_executor_job(self._space, path, summary["size"])
            if problem:
                raise ConvertError(problem)
            job = {"media_id": media_id, "path": path, "title": title, "summary": summary, "caps": caps, "percent": 0}
            self.queue.append(job)
            if self.converting is None:
                self.converting = self.hass.async_create_background_task(self._convert_all(), "foyer_convert")
        ahead = (1 if self.current is not None and job is not self.current else 0) + (
            self.queue.index(job) if job in self.queue else 0)
        return {"result": "converting" if job is self.current else "queued", "ahead": ahead,
                "minutes": estimate_minutes(summary, self.factor)}

    def _notify(self, message: str) -> None:
        from homeassistant.components import persistent_notification
        persistent_notification.async_create(self.hass, message, title="Foyer: preparing videos for TV",
                                             notification_id=NOTIFICATION)

    async def _convert_all(self) -> None:
        try:
            while self.queue:
                self.current = self.queue.pop(0)
                await self._convert(self.current)
        finally:
            self.current = None
            self.converting = None

    async def _convert(self, job: dict) -> None:
        target = converted_path(job["path"])
        part = target.with_name(target.name + ".part")
        await self.hass.async_add_executor_job(self.tidy, str(target.parent.parent))  # Leftovers of a stopped one.
        await self.hass.async_add_executor_job(lambda: target.parent.mkdir(exist_ok=True))
        binary = await self.hass.async_add_executor_job(self.ffmpeg)
        began = time.monotonic()
        name = job["title"]
        waiting = f" {len(self.queue)} more waiting." if self.queue else ""
        self._notify(f"Converting **{name}** so Roku can play it.{waiting}")
        try:
            process = await asyncio.create_subprocess_exec(
                *_nice(), binary, "-y", *convert_args(job["path"], job["summary"], job["caps"], str(part)),
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except (OSError, TypeError):
            self._notify(f"Couldn't convert **{name}**: ffmpeg could not start.")
            return
        job["process"] = process
        errors = self.hass.async_create_background_task(_tail(process.stderr), "foyer_convert_errors")
        duration = job["summary"]["duration"] or 0
        shown = 0
        async for raw in process.stdout:
            line = raw.decode(errors="replace").strip()
            if line.startswith("out_time_us=") and duration > 0:
                try:
                    done = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                job["percent"] = max(0, min(99, int(done * 100 / duration)))
                if job["percent"] >= shown + 10:
                    shown = job["percent"] - job["percent"] % 10
                    waiting = f" {len(self.queue)} more waiting." if self.queue else ""
                    self._notify(f"Converting **{name}** so Roku can play it: {job['percent']}%.{waiting}")
        code = await process.wait()
        message = await errors
        if code == 0:
            await self.hass.async_add_executor_job(os.replace, part, target)
            job["percent"] = 100
            work = duration * convert_megapixels(job["summary"])
            if work > 60:  # Long enough to say how fast this box converts.
                self.factor = (time.monotonic() - began) / work
            self._notify(f"**{name}** is ready to watch on the TV.")
            return
        await self.hass.async_add_executor_job(lambda: part.unlink(missing_ok=True))
        if code in (-9, -15):
            return  # Stopped with Home Assistant.
        reason = message[-1][:160] if message else f"ffmpeg exited with {code}"
        _LOGGER.warning("Converting %s for the TV failed: %s", Path(job["path"]).name, reason)
        self._notify(f"Couldn't convert **{name}**: {reason}")

    # --- Shutdown ---

    async def shutdown(self, _event=None) -> None:
        for token in list(self.sessions):
            await self.end(token)
        if self.sweeper is not None:
            self.sweeper()
            self.sweeper = None
        self.queue.clear()
        job = self.current
        if job is not None and job.get("process") is not None and job["process"].returncode is None:
            job["process"].kill()
            await job["process"].wait()
        await self.hass.async_add_executor_job(self._clear_roots)


class MediaView(HomeAssistantView):
    url = "/api/foyer/media/{token}/{name}"
    name = "api:foyer:media"
    requires_auth = False  # The unguessable session token in the address is the credential, as with HA's signed paths.

    def __init__(self, media: Media):
        self.media = media

    async def get(self, request, token, name):
        return await self.media.serve(token, name, request.query)
