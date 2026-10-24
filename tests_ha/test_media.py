"""Files Roku cannot open, through a real Home Assistant and a real ffmpeg: repackaged streams and conversion."""
import asyncio
import os
import shutil
import subprocess

import pytest

from custom_components.foyer import media as media_module
from custom_components.foyer.media import converted_path
from custom_components.foyer.playback import DEFAULT_CAPS
from test_protocol2 import CODE, SETTINGS, home, setup_entry, tv  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
LIBRARY = "media-source://media_source/local/"
CAPS = {"video": ["h264", "hevc"], "audio": ["aac", "mp3", "ac3", "eac3"], "uhd": False}
SUBS = "1\n00:00:01,000 --> 00:00:03,000\nHello from the subtitle track.\n\n2\n00:00:08,000 --> 00:00:09,500\nEight seconds in.\n"


def make(folder, name, *args):
    video = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=12"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=12"]
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *video, *tone, *args, str(folder / name)],
                   check=True, timeout=60)


@pytest.fixture
def media_dir(hass, tmp_path):
    (tmp_path / "subs.srt").write_text(SUBS)
    srt = ["-i", str(tmp_path / "subs.srt")]
    make(tmp_path, "Film.mkv", *srt, "-map", "0", "-map", "1", "-map", "1", "-map", "2", "-c:v", "libx264", "-preset",
         "ultrafast", "-g", "24", "-c:a:0", "dca", "-strict", "-2", "-ac:a:0", "2", "-c:a:1", "aac", "-c:s", "srt",
         "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "title=Commentary", "-metadata:s:s:0", "language=eng")
    make(tmp_path, "Old.avi", "-c:v", "mpeg4", "-q:v", "8", "-c:a", "libmp3lame")
    make(tmp_path, "Plain.mp4", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac")
    (tmp_path / "subs.srt").unlink()
    hass.config.media_dirs["local"] = str(tmp_path)
    return tmp_path


async def test_files_are_checked_before_they_play(hass, tv, media_dir):
    media = hass.data["foyer"].media
    assert (await media.choose(str(media_dir / "Plain.mp4"), DEFAULT_CAPS))[0] == "direct"
    plan, summary = await media.choose(str(media_dir / "Film.mkv"), DEFAULT_CAPS)
    assert plan == "repackage" and summary["audio"][0]["codec"] == "dts" and summary["subtitles"][0]["codec"] == "subrip"
    assert (await media.choose(str(media_dir / "Old.avi"), DEFAULT_CAPS))[0] == "convert"
    # Without ffprobe the probe runs through PyAV, in a separate process, with the same answers.
    media.probes.clear()
    media.ffprobe = lambda: None
    assert (await media.choose(str(media_dir / "Film.mkv"), DEFAULT_CAPS))[0] == "repackage"
    assert (await media.choose(str(media_dir / "Old.avi"), DEFAULT_CAPS))[0] == "convert"
    assert await media.probe(str(media_dir / "missing.mkv")) is None


async def test_dts_film_streams_repackaged_with_subtitles(hass, tv, media_dir):
    client = tv.client
    played = await tv.post("play", {"id": LIBRARY + "Film.mkv", "caps": CAPS})
    assert played.status == 200, await played.text()
    command = (await tv(player="idle"))["command"]
    assert command["format"] == "hls" and command["restart"] is True and command["length"] == 12
    assert command["key"] == LIBRARY + "Film.mkv" and command["url"].startswith("/api/foyer/media/")
    media = hass.data["foyer"].media
    session = next(iter(media.sessions.values()))
    master = await client.get(command["url"])  # No HA login: the session token is the credential.
    text = await master.text()
    assert master.status == 200 and "RESOLUTION=320x240" in text and 'TYPE=SUBTITLES' in text and 'LANGUAGE="eng"' in text
    # Work files sit on the media's disk, never in /tmp (memory on Home Assistant OS).
    assert session.directory.startswith(str(media_dir / ".foyer-work"))
    assert 'TYPE=AUDIO,GROUP-ID="audio",NAME="eng"' in text and 'NAME="Commentary"' in text  # Each track selectable.
    video = await (await client.get(command["url"].replace("index.m3u8", "1-video.m3u8"))).text()
    assert "1-seg00000.ts" in video
    commentary = await (await client.get(command["url"].replace("index.m3u8", "1-audio1.m3u8"))).text()
    assert "1-a1_00000.ts" in commentary
    audio = await (await client.get(command["url"].replace("index.m3u8", "1-a1_00000.ts"))).read()
    assert audio[0] == 0x47 and len(audio) > 1000
    segment = await client.get(command["url"].replace("index.m3u8", "1-seg00000.ts"))
    data = await segment.read()
    assert segment.status == 200 and data[0] == 0x47 and len(data) > 10_000  # MPEG-TS
    for _ in range(40):  # The whole 12 seconds, so the last subtitle segment is final.
        if "#EXT-X-ENDLIST" in await (await client.get(command["url"].replace("index.m3u8", "1-video.m3u8"))).text():
            break
        await asyncio.sleep(0.25)
    subtitles = await (await client.get(command["url"].replace("index.m3u8", "1-sub0.m3u8"))).text()
    assert "1-sub0_00000.vtt" in subtitles and "#EXT-X-ENDLIST" in subtitles
    first = await (await client.get(command["url"].replace("index.m3u8", "1-sub0_00000.vtt"))).text()
    assert first.startswith("WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:0,LOCAL:00:00:00.000") and "Hello from the subtitle track." in first
    assert "Eight seconds in." not in first  # It belongs to the second segment, from 6 seconds.
    second = await (await client.get(command["url"].replace("index.m3u8", "1-sub0_00001.vtt"))).text()
    assert "Eight seconds in." in second

    # A jump past what is ready: the TV asks again from there. A new generation; the old one is gone.
    old = session.directory
    restarted = await client.get(command["url"] + "?start=6")
    assert restarted.status == 200 and "2-video.m3u8" in await restarted.text()
    assert session.generation == 2 and not os.path.exists(old)
    assert (await client.get(command["url"].replace("index.m3u8", "1-seg00000.ts"))).status == 404
    assert (await client.get(command["url"] + "?start=-1")).status == 400
    assert (await client.get(command["url"].replace(session.token, "x" * 24))).status == 404

    process, directory = session.process, session.directory
    await media.shutdown()
    assert process.returncode is not None and not os.path.exists(directory) and not media.sessions
    assert not (media_dir / ".foyer-work").exists()  # Nothing left in the media folder.


async def test_played_segments_are_deleted_behind_the_tv(hass, tv, media_dir, monkeypatch):
    monkeypatch.setattr(media_module, "KEEP_BEHIND", 0)  # 15 minutes in use; here nothing is kept behind.
    client = tv.client
    assert (await tv.post("play", {"id": LIBRARY + "Film.mkv", "caps": CAPS})).status == 200
    url = (await tv(player="idle"))["command"]["url"]
    session = next(iter(hass.data["foyer"].media.sessions.values()))
    assert (await client.get(url)).status == 200
    for _ in range(40):
        if "1-seg00001.ts" in await (await client.get(url.replace("index.m3u8", "1-video.m3u8"))).text():
            break
        await asyncio.sleep(0.25)
    assert (await client.get(url.replace("index.m3u8", "1-seg00001.ts"))).status == 200  # The TV has moved on.
    for _ in range(20):
        if not os.path.exists(os.path.join(session.directory, "seg00000.ts")):
            break
        await asyncio.sleep(0.25)
    assert not os.path.exists(os.path.join(session.directory, "seg00000.ts"))
    assert not os.path.exists(os.path.join(session.directory, "a1_00000.ts"))  # Audio renditions too.
    assert (await client.get(url.replace("index.m3u8", "1-seg00000.ts"))).status == 404
    assert (await client.get(url.replace("index.m3u8", "1-seg00001.ts"))).status == 200
    # Back to the very start, where this run began: its first segments are gone, so it starts afresh.
    again = await client.get(url + "?start=0")
    assert again.status == 200 and "2-video.m3u8" in await again.text() and session.generation == 2
    await hass.data["foyer"].media.shutdown()


async def test_never_repackages_into_memory_or_a_full_disk(hass, tv, media_dir, monkeypatch):
    # Home Assistant OS: /tmp is RAM. A media folder Foyer cannot write to must not send the work there.
    monkeypatch.setattr(media_module, "in_memory", lambda path: True)
    reply = await tv.post("play", {"id": LIBRARY + "Film.mkv", "caps": CAPS})
    assert reply.status == 409 and (await reply.json())["error"] == "no_room"
    assert not os.path.exists(media_dir / ".foyer-work")
    monkeypatch.setattr(media_module, "in_memory", lambda path: False)
    monkeypatch.setattr(media_module, "stream_bytes", lambda summary: 1 << 62)  # More than any disk has free.
    reply = await tv.post("play", {"id": LIBRARY + "Film.mkv", "caps": CAPS})
    assert reply.status == 409 and (await reply.json())["error"] == "no_room"
    assert (await tv.post("play", {"id": LIBRARY + "Plain.mp4", "caps": CAPS})).status == 200  # Direct files still play.
    assert not hass.data["foyer"].media.sessions


async def test_stale_converted_copies_are_cleared(hass, tv, media_dir):
    copies = media_dir / ".foyer"
    copies.mkdir()
    (copies / "Gone.avi.mkv").write_bytes(b"x")  # Its original was deleted.
    (copies / "Old.avi.mkv.part").write_bytes(b"x")  # A conversion stopped by a power cut.
    (copies / "Plain.mp4.mkv").write_bytes(b"x")
    os.utime(copies / "Plain.mp4.mkv", (1, 1))  # Older than its original, which was replaced since.
    assert (await tv.post("browse", {"id": LIBRARY + "."})).status == 200
    await hass.async_block_till_done()
    assert not copies.exists()
    (copies).mkdir()
    (copies / "Old.avi.mkv").write_bytes(b"x")  # A good copy stays, and plays.
    assert (await tv.post("browse", {"id": LIBRARY + "."})).status == 200
    await hass.async_block_till_done()
    assert (copies / "Old.avi.mkv").exists()


async def test_avi_needs_converting_which_is_opt_in(hass, tv, media_dir):
    reply = await tv.post("play", {"id": LIBRARY + "Old.avi", "caps": CAPS})
    assert reply.status == 409 and await reply.json() == {"error": "needs_conversion", "convert": False, "minutes": 1}
    assert (await tv.post("convert", {"id": LIBRARY + "Old.avi"})).status == 403  # Off unless switched on.


async def test_conversion_makes_a_copy_that_then_plays(hass, hass_client_no_auth, home, media_dir):
    entry = await setup_entry(hass, {**SETTINGS, "convert": True})
    client = await hass_client_no_auth()
    token = (await (await client.post("/api/foyer/pair", json={"code": CODE})).json())["token"]
    headers = {"Authorization": "Bearer " + token}
    media = hass.data["foyer"].media
    messages = []
    media._notify = messages.append  # What Home Assistant's notification shows.
    assert (await client.post("/api/foyer/convert", headers=headers, json={"id": LIBRARY + "Plain.mp4"})).status == 409
    queued = await client.post("/api/foyer/convert", headers=headers, json={"id": LIBRARY + "Old.avi", "caps": CAPS})
    body = await queued.json()
    assert queued.status == 200 and body["result"] in ("queued", "converting") and body["ahead"] == 0
    listing = await (await client.post("/api/foyer/browse", headers=headers, json={"id": LIBRARY + "."})).json()
    old = next(item for item in listing["items"] if item["title"] == "Old.avi")
    assert old.get("note", "").startswith(("Converting", "Waiting"))
    for _ in range(240):
        if media.converting is None:
            break
        await asyncio.sleep(0.25)
    target = converted_path(str(media_dir / "Old.avi"))
    assert target.is_file() and not target.with_name(target.name + ".part").exists()
    assert (await media.probe(str(target)))["video"]["codec"] == "h264"
    listing = await (await client.post("/api/foyer/browse", headers=headers, json={"id": LIBRARY + "."})).json()
    assert ".foyer" not in [item["title"] for item in listing["items"]]  # The copies stay out of sight.
    assert "is ready to watch on the TV" in messages[-1] and "Converting **Old.avi**" in messages[0]
    played = await client.post("/api/foyer/play", headers=headers, json={"id": LIBRARY + "Old.avi"})
    assert played.status == 200
    status = await (await client.get("/api/foyer/status", headers=headers)).json()
    assert "/.foyer/Old.avi.mkv" in status["command"]["url"] and status["command"]["format"] == "mkv"
    await media.shutdown()
    assert entry.state.value == "loaded"
