"""Which files a TV can play as they are, and the ffmpeg work for the rest. Pure: no Home Assistant imports.

Roku plays H.264 and HEVC in MP4, MKV and MPEG-TS, with AAC, MP3 and Dolby audio. Many files that fail hold
perfectly good video in a wrapper Roku refuses (AVI, WMV, FLV) or with audio it cannot decode (DTS, TrueHD).
Those are repackaged as they stream: the video is copied untouched and only the audio is converted, which costs
little even on a Raspberry Pi. Only files whose video itself cannot play need real conversion, which is opt-in
and runs in the background.
"""
from __future__ import annotations

import re

VIDEO_CAPS = ("h264", "hevc", "hevc10", "vp9", "av1", "mpeg2video", "mpeg4", "vp8")
AUDIO_CAPS = ("aac", "mp3", "mp2", "ac3", "eac3", "dts", "truehd", "flac", "alac", "opus", "vorbis", "pcm", "wma")
# What every current Roku decodes; used until the TV reports its own.
DEFAULT_CAPS = {"video": frozenset({"h264", "hevc"}), "audio": frozenset({"aac", "mp3", "ac3", "eac3", "flac", "alac", "pcm"}),
                "uhd": False}
VIDEO_CONTAINERS = frozenset({"mov", "mp4", "matroska", "webm", "mpegts"})  # parts of ffprobe's format_name
AUDIO_CONTAINERS = VIDEO_CONTAINERS | {"mp3", "flac", "wav", "aac"}
TS_AUDIO = frozenset({"aac", "mp3", "mp2", "ac3", "eac3"})  # copied into MPEG-TS as they are
TS_VIDEO = frozenset({"h264", "hevc"})  # what Roku plays from HLS in MPEG-TS; VP9 and AV1 cannot go there
CONVERT_FACTOR = 1.5  # Seconds of conversion per second of video per megapixel, until one has been timed.
TEXT_SUBTITLES = frozenset({"subrip", "ass", "ssa", "webvtt", "mov_text", "text", "subviewer", "microdvd"})
SEGMENT_SECONDS = 6
MAX_AUDIO = 4
MAX_SUBTITLES = 4
LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$")


def tv_caps(value) -> dict | None:
    """What a TV reported it decodes, keeping only names Foyer knows."""
    if not isinstance(value, dict):
        return None
    video, audio = value.get("video"), value.get("audio")
    if not isinstance(video, list) or not isinstance(audio, list) or len(video) > 20 or len(audio) > 30:
        return None
    return {"video": frozenset(v for v in video if v in VIDEO_CAPS), "audio": frozenset(a for a in audio if a in AUDIO_CAPS),
            "uhd": value.get("uhd") is True}


def _text(value, limit) -> str:
    return "".join(c for c in str(value or "") if c.isprintable() and c != '"')[:limit]


def _number(value, cast=int, default=0):
    try:
        return cast(value)
    except (TypeError, ValueError):
        return default


def media_summary(raw) -> dict | None:
    """The parts of ffprobe's JSON (-show_format -show_streams) that decide how a file plays."""
    if not isinstance(raw, dict) or not isinstance(raw.get("streams"), list):
        return None
    fmt = raw.get("format") if isinstance(raw.get("format"), dict) else {}
    video, audio, subtitles = None, [], []
    for stream in raw["streams"]:
        if not isinstance(stream, dict) or not isinstance(stream.get("index"), int):
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = stream.get("disposition") if isinstance(stream.get("disposition"), dict) else {}
        kind, codec = stream.get("codec_type"), _text(stream.get("codec_name"), 24)
        language = _text(tags.get("language"), 8)
        common = {"index": stream["index"], "codec": codec, "language": language if LANGUAGE_RE.fullmatch(language) else "",
                  "title": _text(tags.get("title"), 40), "default": bool(disposition.get("default"))}
        if kind == "video" and video is None and not disposition.get("attached_pic"):  # Cover art is not the video.
            video = {"index": stream["index"], "codec": codec, "pix_fmt": _text(stream.get("pix_fmt"), 24),
                     "width": _number(stream.get("width")), "height": _number(stream.get("height"))}
        elif kind == "audio" and len(audio) < 8:
            audio.append({**common, "channels": _number(stream.get("channels"), default=2)})
        elif kind == "subtitle" and len(subtitles) < 8:
            subtitles.append({**common, "forced": bool(disposition.get("forced"))})
    return {"container": _text(fmt.get("format_name"), 60), "duration": max(0.0, _number(fmt.get("duration"), float, 0.0)),
            "size": max(0, _number(fmt.get("size"))), "video": video, "audio": audio, "subtitles": subtitles}


def audio_family(codec: str) -> str:
    """ffprobe's codec name as the name a TV reports."""
    if codec.startswith("pcm_"):
        return "pcm"
    if codec.startswith("wma"):
        return "wma"
    return {"mp3float": "mp3", "mlp": "truehd", "dca": "dts"}.get(codec, codec)


def main_audio(audio: list) -> dict | None:
    """The track Roku starts with: the default one, else the first."""
    return next((track for track in audio if track["default"]), audio[0] if audio else None)


def video_playable(video: dict, caps: dict) -> bool:
    fmt = video["pix_fmt"]
    if fmt and not re.fullmatch(r"(yuvj?420p|nv12)(10le|10be)?", fmt):
        return False  # 4:2:2, 4:4:4 and RGB: no Roku decodes them.
    deep = fmt.endswith(("10le", "10be"))
    if video["codec"] == "h264" and deep:
        return False  # "Hi10" H.264, common in anime releases, plays nowhere on Roku.
    name = "hevc10" if video["codec"] == "hevc" and deep else video["codec"]
    if name not in caps["video"]:
        return False
    return video["height"] <= 1088 or caps["uhd"]


def playback_plan(summary: dict, caps: dict) -> str:
    """"direct", "repackage" (copy the video, convert the audio, as it streams) or "convert" (the video itself)."""
    video, audio = summary["video"], summary["audio"]
    if video is None and not audio:
        return "direct"  # Nothing Foyer understands; the TV says so if it cannot play it.
    if video is not None and not video_playable(video, caps):
        return "convert"
    main = main_audio(audio)
    containers = VIDEO_CONTAINERS if video is not None else AUDIO_CONTAINERS
    if (main is None or audio_family(main["codec"]) in caps["audio"]) and set(summary["container"].split(",")) & containers:
        return "direct"
    # Repackaged streams carry the video copied into MPEG-TS, which holds only H.264 and HEVC for Roku.
    return "repackage" if video is None or video["codec"] in TS_VIDEO else "convert"


def text_subtitles(summary: dict) -> list:
    if summary["video"] is None:
        return []
    return [track for track in summary["subtitles"] if track["codec"] in TEXT_SUBTITLES][:MAX_SUBTITLES]


def _audio_codec(flag: str, track: dict, caps: dict, copyable) -> list:
    family = audio_family(track["codec"])
    if family in caps["audio"] and family in copyable:
        return [flag, "copy"]
    if track["channels"] > 2 and "ac3" in caps["audio"]:
        # Surround stays surround (5.1 at most).
        return [flag, "ac3", "-ac" + flag[2:], str(min(track["channels"], 6)), "-b" + flag[2:], "448k"]
    return [flag, "aac", "-ac" + flag[2:], "2", "-b" + flag[2:], "192k"]


def audio_renditions(summary: dict) -> list:
    """Several audio tracks go out as separate renditions: Roku lists only one of those mixed into the video."""
    audio = summary["audio"][:MAX_AUDIO]
    return audio if summary["video"] is not None and len(audio) > 1 else []


def needs_master(summary: dict) -> bool:
    return bool(text_subtitles(summary) or audio_renditions(summary))


def timeline_shift(start: int) -> int:
    """Seconds taken off every timestamp of a stream started at `start`. Roku shows WebVTT cues late in streams
    whose timestamps start far from zero (3 s late at 4 minutes, 6 s at 12, seen on a real TV), so a restarted
    stream counts from near zero. The margin keeps the keyframe before `start` above zero."""
    return max(0, start - 15)


def repackage_args(path: str, summary: dict, caps: dict, start: int, out: str) -> list:
    """ffmpeg arguments: the video copied and the audio made playable, as HLS in `out`, from `start` seconds.

    Every output keeps the file's timestamps (-copyts, no mux delay) less the same timeline_shift, so audio
    renditions and subtitle cues, written alongside, line up with the video whatever point playback starts from.
    """
    shift = ["-output_ts_offset", f"-{timeline_shift(start)}"] if timeline_shift(start) else []

    def hls(segments, playlist):
        return [*shift, "-muxdelay", "0", "-muxpreload", "0", "-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_list_size", "0",
                "-hls_playlist_type", "event", "-hls_flags", "independent_segments",
                "-hls_segment_filename", f"{out}/{segments}%05d.ts", f"{out}/{playlist}.m3u8"]

    args = ["-nostdin", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        args += ["-ss", str(start)]
    args += ["-copyts", "-i", path]
    video, renditions = summary["video"], audio_renditions(summary)
    if video is not None:
        args += ["-map", f"0:{video['index']}", "-c:v", "copy"]
    for track in [] if renditions else summary["audio"][:1]:
        args += ["-map", f"0:{track['index']}", *_audio_codec("-c:a:0", track, caps, TS_AUDIO)]
    args += hls("seg", "video")
    for number, track in enumerate(renditions):
        args += ["-map", f"0:{track['index']}", *_audio_codec("-c:a:0", track, caps, TS_AUDIO), *hls(f"a{number}_", f"audio{number}")]
    for number, track in enumerate(text_subtitles(summary)):
        args += ["-map", f"0:{track['index']}", "-c:s", "webvtt", *shift, "-flush_packets", "1", "-f", "webvtt",
                 f"{out}/sub{number}.vtt"]
    return args


def convert_args(path: str, summary: dict, caps: dict, out: str) -> list:
    """ffmpeg arguments for a copy every Roku plays: H.264 video, playable audio, text subtitles, in MKV."""
    video = summary["video"]
    args = ["-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-threads", "2",
            "-i", path, "-map", f"0:{video['index']}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", "4.1"]
    if video["height"] > 1080 or video["width"] > 1920:
        args += ["-vf", "scale=w=1920:h=1080:force_original_aspect_ratio=decrease:force_divisible_by=2"]
    for number, track in enumerate(summary["audio"][:MAX_AUDIO]):
        args += ["-map", f"0:{track['index']}", *_audio_codec(f"-c:a:{number}", track, caps, AUDIO_CAPS)]
    subtitles = [track for track in summary["subtitles"] if track["codec"] in TEXT_SUBTITLES][:8]
    for track in subtitles:
        args += ["-map", f"0:{track['index']}"]
    if subtitles:
        args += ["-c:s", "srt"]
    return args + ["-f", "matroska", out]


# --- Serving a repackaged stream: playlists and subtitle segments, written by Foyer from ffmpeg's output. ---

def playlist_segments(text: str) -> list:
    """[(seconds, segment name)] of an HLS media playlist."""
    segments, length = [], None
    for line in text.splitlines():
        if line.startswith("#EXTINF:"):
            length = _number(line[8:].split(",")[0], float, 0.0)
        elif line and not line.startswith("#") and length is not None:
            segments.append((length, line.strip()))
            length = None
    return segments


def media_playlist(text: str, prefix: str, rename=None) -> str:
    """ffmpeg's playlist with each segment renamed for this session (and, for subtitles, swapped for a cue file)."""
    lines, number = [], 0
    for line in text.splitlines():
        if line and not line.startswith("#"):
            lines.append(prefix + (rename(number) if rename else line.strip()))
            number += 1
        elif line.strip():
            lines.append(line)
    return "\n".join(lines) + "\n"


def master_playlist(summary: dict, prefix: str) -> str:
    """Video plus audio and WebVTT subtitle renditions. RESOLUTION is required: Roku never leaves a master without it."""
    video = summary["video"]
    bandwidth = int(summary["size"] * 8 / summary["duration"]) if summary["duration"] > 0 and summary["size"] > 0 else 8_000_000
    lines = ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS"]
    groups = ""

    def rendition(kind, group, number, track, default, autoselect, uri):
        name = track["title"] or track["language"] or f"{kind.title()} {number + 1}"
        if name in used:  # Names must differ within a group.
            name = f"{name} {number + 1}"
        used.add(name)
        language = f',LANGUAGE="{track["language"]}"' if track["language"] else ""
        extra = f',FORCED={autoselect}' if kind == "SUBTITLES" else ""
        lines.append(f'#EXT-X-MEDIA:TYPE={kind},GROUP-ID="{group}",NAME="{name}"{language},DEFAULT={default},'
                     f'AUTOSELECT={autoselect}{extra},URI="{prefix}{uri}"')

    renditions = audio_renditions(summary)
    used: set = set()
    main = main_audio(renditions)
    for number, track in enumerate(renditions):
        rendition("AUDIO", "audio", number, track, "YES" if track is main else "NO", "YES", f"audio{number}.m3u8")
    if renditions:
        groups += ',AUDIO="audio"'
    used = set()
    for number, track in enumerate(text_subtitles(summary)):
        forced = "YES" if track["forced"] else "NO"
        rendition("SUBTITLES", "subs", number, track, "NO", forced, f"sub{number}.m3u8")
    if text_subtitles(summary):
        groups += ',SUBTITLES="subs"'
    lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={max(bandwidth, 64_000)},RESOLUTION={video["width"]}x{video["height"]}{groups}')
    lines.append(f"{prefix}video.m3u8")
    return "\n".join(lines) + "\n"


CUE_RE = re.compile(r"^(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3}) --> ")


def vtt_cues(text: str) -> list:
    """[(start seconds, cue block)] of a WebVTT file ffmpeg may still be writing; a cut-off last cue waits.

    ffmpeg writes each cue whole (-flush_packets 1) and ends it with a single newline.
    """
    text = text.replace("\r\n", "\n")
    blocks = text.split("\n\n")
    if not text.endswith("\n"):
        blocks = blocks[:-1]
    cues = []
    for block in blocks:
        lines = block.strip("\n").split("\n")
        for number, line in enumerate(lines[:2]):
            match = CUE_RE.match(line)
            if match and len(lines) > number + 1:  # Timing and at least one line of text.
                block = "\n".join(lines)
                hours, minutes, seconds, millis = (int(part or 0) for part in match.groups())
                cues.append((hours * 3600 + minutes * 60 + seconds + millis / 1000, block))
                break
    return cues


def subtitle_segment(cues: list, segments: list, number: int, start: float, ended: bool) -> str:
    """The WebVTT segment matching video segment `number`: each cue belongs to the segment its start falls in.

    Cue and video times are both the file's own (see repackage_args), so the timestamp map is the identity.
    """
    edges = [start]
    for length, _name in segments:
        edges.append(edges[-1] + length)
    low = float("-inf") if number == 0 else edges[number]
    high = float("inf") if ended and number == len(segments) - 1 else edges[number + 1]
    body = [block for begin, block in cues if low <= begin < high]
    return "WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:0,LOCAL:00:00:00.000\n\n" + "".join(block + "\n\n" for block in body)


def convert_megapixels(summary: dict) -> float:
    """The picture size a conversion encodes: the file's, at most 1080p (see convert_args)."""
    video = summary["video"] or {}
    width, height = video.get("width") or 1280, video.get("height") or 720
    if width > 1920 or height > 1080:
        scale = min(1920 / width, 1080 / height)
        width, height = width * scale, height * scale
    return width * height / 1_000_000


def estimate_minutes(summary: dict, factor: float | None = None) -> int:
    """A rough conversion time. Encoding time grows with the picture size; `factor` is what the last conversion
    on this box measured (seconds per second of video per megapixel)."""
    return max(1, round(summary["duration"] * convert_megapixels(summary) * (factor or CONVERT_FACTOR) / 60))
