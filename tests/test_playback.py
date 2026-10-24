import importlib
import unittest

from support import load_modules

load_modules()
playback = importlib.import_module("foyer_test.playback")
CAPS = playback.DEFAULT_CAPS


def probe(container="matroska,webm", video=("h264", "yuv420p", 1920, 1080), audio=(("aac", 2, True),), subtitles=(), duration="600"):
    streams, index = [], 0
    if video:
        streams.append({"index": index, "codec_type": "video", "codec_name": video[0], "pix_fmt": video[1], "width": video[2],
                        "height": video[3]})
        index += 1
    for codec, channels, default in audio:
        streams.append({"index": index, "codec_type": "audio", "codec_name": codec, "channels": channels,
                        "disposition": {"default": int(default)}, "tags": {"language": "eng", "title": "Main"}})
        index += 1
    for codec, language in subtitles:
        streams.append({"index": index, "codec_type": "subtitle", "codec_name": codec, "tags": {"language": language}})
        index += 1
    return playback.media_summary({"format": {"format_name": container, "duration": duration, "size": "750000000"},
                                   "streams": streams})


class PlanTests(unittest.TestCase):
    def test_what_plays_as_it_is_what_is_repackaged_and_what_needs_converting(self):
        for summary, expected in [
            (probe(), "direct"),
            (probe("mov,mp4,m4a,3gp,3g2,mj2", audio=(("eac3", 6, True),)), "direct"),
            (probe(audio=(("dts", 6, True), ("aac", 2, False))), "repackage"),  # The default track is what plays first.
            (probe(audio=(("aac", 2, True), ("dts", 6, False))), "direct"),
            (probe(audio=(("truehd", 8, True),)), "repackage"),
            (probe("avi", audio=(("mp3", 2, True),)), "repackage"),  # H.264 in AVI: only the wrapper is wrong.
            (probe("flv"), "repackage"),
            (probe("avi", video=("mpeg4", "yuv420p", 720, 480)), "convert"),
            (probe("asf", video=("wmv3", "yuv420p", 720, 480), audio=(("wmav2", 2, True),)), "convert"),
            (probe(video=("h264", "yuv420p10le", 1920, 1080)), "convert"),  # Hi10 anime releases.
            (probe(video=("h264", "yuv422p", 1920, 1080)), "convert"),
            (probe(video=("hevc", "yuv420p10le", 3840, 2160)), "convert"),  # Not a 4K TV, and no HEVC Main 10 reported.
            (probe("ogg", video=None, audio=(("vorbis", 2, True),)), "repackage"),
            (probe("mp3", video=None, audio=(("mp3", 2, False),)), "direct"),
            (probe("flac", video=None, audio=(("flac", 2, False),)), "direct"),
            (probe("asf", video=None, audio=(("wmav2", 2, True),)), "repackage"),
            (probe(video=None, audio=()), "direct"),
        ]:
            self.assertEqual(playback.playback_plan(summary, CAPS), expected, summary)

    def test_a_tv_reports_what_it_decodes(self):
        uhd = playback.tv_caps({"video": ["h264", "hevc", "hevc10", "vp9", "nonsense"], "audio": ["aac", "dts"], "uhd": True})
        self.assertEqual(uhd["video"], {"h264", "hevc", "hevc10", "vp9"})
        self.assertEqual(playback.playback_plan(probe(video=("hevc", "yuv420p10le", 3840, 2160)), uhd), "direct")
        self.assertEqual(playback.playback_plan(probe(audio=(("dts", 6, True),)), uhd), "direct")
        # VP9 and AV1 play from MKV and WebM, but repackaged streams are MPEG-TS, which cannot carry them.
        vp9 = playback.tv_caps({"video": ["h264", "vp9", "av1"], "audio": ["aac"], "uhd": False})
        self.assertEqual(playback.playback_plan(probe("matroska,webm", video=("vp9", "yuv420p", 1920, 1080), audio=(("opus", 2, True),)), vp9), "convert")
        self.assertEqual(playback.playback_plan(probe("matroska,webm", video=("av1", "yuv420p", 1920, 1080), audio=(("aac", 2, True),)), vp9), "direct")
        self.assertEqual(playback.playback_plan(probe("avi", video=("av1", "yuv420p", 1920, 1080)), vp9), "convert")
        self.assertEqual(playback.playback_plan(probe(video=("hevc", "yuv420p", 1920, 1080), audio=(("dts", 6, True),)), CAPS), "repackage")
        for bad in (None, [], {"video": "h264", "audio": []}, {"video": ["h264"] * 21, "audio": []}):
            self.assertIsNone(playback.tv_caps(bad))
        self.assertIs(playback.tv_caps({"video": [], "audio": [], "uhd": "yes"})["uhd"], False)

    def test_summary_skips_cover_art_and_cleans_tags(self):
        summary = playback.media_summary({"format": {"format_name": "matroska,webm", "duration": "oops"}, "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"index": 1, "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1280, "height": 720},
            {"index": 2, "codec_type": "audio", "codec_name": "aac", "tags": {"language": "en\"g", "title": 'A "b"\n'}},
            "junk", {"codec_type": "audio"}]})
        self.assertEqual(summary["video"]["index"], 1)
        self.assertEqual(summary["duration"], 0.0)
        self.assertEqual((summary["audio"][0]["language"], summary["audio"][0]["title"]), ("eng", "A b"))
        self.assertIsNone(playback.media_summary({"streams": "x"}))


class FfmpegTests(unittest.TestCase):
    def test_repackaging_copies_the_video_and_only_converts_audio_roku_cannot_play(self):
        summary = probe(audio=(("dts", 6, True), ("aac", 2, False), ("flac", 2, False)),
                        subtitles=(("subrip", "eng"), ("hdmv_pgs_subtitle", "eng"), ("ass", "spa")))
        args = playback.repackage_args("/media/Film.mkv", summary, CAPS, 0, "/tmp/g1")
        joined = " ".join(args)
        self.assertIn("-copyts -i /media/Film.mkv -map 0:0 -c:v copy -muxdelay 0 -muxpreload 0 -f hls", joined)
        self.assertIn("/tmp/g1/seg%05d.ts /tmp/g1/video.m3u8", joined)  # Video alone: each audio track is its own rendition.
        self.assertIn("-map 0:1 -c:a:0 ac3 -ac:a:0 6 -b:a:0 448k -muxdelay 0", joined)  # Surround stays surround.
        self.assertIn("/tmp/g1/a0_%05d.ts /tmp/g1/audio0.m3u8", joined)
        self.assertIn("-map 0:2 -c:a:0 copy -muxdelay 0", joined)
        self.assertIn("-map 0:3 -c:a:0 aac -ac:a:0 2", joined)  # FLAC cannot go into MPEG-TS.
        self.assertIn("/tmp/g1/audio2.m3u8", joined)
        single = " ".join(playback.repackage_args("/f.mkv", probe(audio=(("dts", 6, True),)), CAPS, 0, "/t"))
        self.assertIn("-map 0:0 -c:v copy -map 0:1 -c:a:0 ac3", single)  # One track stays with the video.
        self.assertNotIn("audio0", single)
        self.assertIn("-map 0:4 -c:s webvtt -flush_packets 1 -f webvtt /tmp/g1/sub0.vtt", joined)
        self.assertIn("-map 0:6 -c:s webvtt -flush_packets 1 -f webvtt /tmp/g1/sub1.vtt", joined)  # Picture subtitles left out.
        self.assertNotIn("-ss", args)
        later = playback.repackage_args("/media/Film.mkv", summary, CAPS, 1830, "/tmp/g2")
        self.assertEqual(later[later.index("-ss") + 1], "1830")
        self.assertLess(later.index("-ss"), later.index("-i"))
        # Every output (video, each audio, each subtitle) counts from near zero, all by the same amount.
        self.assertEqual(" ".join(later).count("-output_ts_offset -1815"), 6)
        self.assertNotIn("-output_ts_offset", args)
        self.assertEqual((playback.timeline_shift(10), playback.timeline_shift(253)), (0, 238))
        stereo = playback.repackage_args("/m.mkv", summary, {**CAPS, "audio": frozenset({"aac"})}, 0, "/t")
        self.assertIn("-c:a:0 aac -ac:a:0 2", " ".join(stereo))

    def test_conversion_makes_h264_in_mkv_and_scales_4k_down(self):
        summary = probe("avi", video=("mpeg4", "yuv420p", 3840, 2160), audio=(("mp3", 2, True),), subtitles=(("subrip", "eng"),))
        args = playback.convert_args("/media/Old.avi", summary, CAPS, "/media/.foyer/Old.avi.mkv.part")
        joined = " ".join(args)
        self.assertIn("-c:v libx264 -preset veryfast -crf 21 -pix_fmt yuv420p", joined)
        self.assertIn("-threads 2", joined)
        self.assertIn("force_original_aspect_ratio=decrease", joined)
        self.assertIn("-c:a:0 copy", joined)
        self.assertIn("-c:s srt", joined)
        self.assertEqual(args[-3:], ["-f", "matroska", "/media/.foyer/Old.avi.mkv.part"])

    def test_conversion_time_grows_with_the_picture_and_learns_this_box(self):
        hour = lambda size: probe("avi", video=("mpeg4", "yuv420p", *size), duration="3600")  # noqa: E731
        sd, hd, uhd = (playback.estimate_minutes(hour(size)) for size in ((720, 480), (1920, 1080), (3840, 2160)))
        self.assertLess(sd, 60)  # A DVD-size picture converts faster than it plays.
        self.assertGreater(hd, 120)  # 1080p takes longer than the film.
        self.assertEqual(uhd, hd)  # 4K is scaled to 1080p first.
        self.assertEqual(playback.estimate_minutes(hour((1920, 1080)), 0.5), round(60 * 2.0736 * 0.5))
        self.assertEqual(playback.estimate_minutes(probe(duration="5")), 1)


class PlaylistTests(unittest.TestCase):
    VIDEO = ("#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:6\n#EXT-X-PLAYLIST-TYPE:EVENT\n"
             "#EXTINF:6.000000,\nseg00000.ts\n#EXTINF:5.5,\nseg00001.ts\n#EXT-X-ENDLIST\n")

    def test_playlists_are_renamed_per_generation(self):
        self.assertEqual(playback.playlist_segments(self.VIDEO), [(6.0, "seg00000.ts"), (5.5, "seg00001.ts")])
        renamed = playback.media_playlist(self.VIDEO, "3-")
        self.assertIn("3-seg00000.ts\n", renamed)
        self.assertIn("#EXT-X-ENDLIST", renamed)
        subtitles = playback.media_playlist(self.VIDEO, "3-", lambda n: f"sub1_{n:05d}.vtt")
        self.assertIn("3-sub1_00001.vtt", subtitles)
        self.assertNotIn(".ts", subtitles)

    def test_master_lists_subtitles_and_a_resolution(self):
        summary = probe(subtitles=(("subrip", "eng"), ("ass", "")))
        master = playback.master_playlist(summary, "2-")
        self.assertIn('NAME="eng",LANGUAGE="eng"', master)
        self.assertIn('NAME="Subtitles 2",DEFAULT=NO', master)
        self.assertIn('URI="2-sub1.m3u8"', master)
        self.assertIn("BANDWIDTH=10000000,RESOLUTION=1920x1080,SUBTITLES=\"subs\"\n2-video.m3u8", master)
        self.assertFalse(playback.needs_master(probe()))
        both = playback.master_playlist(probe(audio=(("dts", 6, False), ("aac", 2, True))), "1-")
        self.assertIn('TYPE=AUDIO,GROUP-ID="audio",NAME="Main",LANGUAGE="eng",DEFAULT=NO,AUTOSELECT=YES,URI="1-audio0.m3u8"', both)
        self.assertIn('NAME="Main 2",LANGUAGE="eng",DEFAULT=YES', both)  # Names differ; the file's default plays first.
        self.assertIn(',AUDIO="audio"\n1-video.m3u8', both)

    def test_cues_land_in_the_segment_their_start_falls_in(self):
        text = ("WEBVTT\n\n00:01.000 --> 00:03.000\nHello\n\n00:08.000 --> 00:09.500\nEight\n\n"
                "01:00:07.250 --> 01:00:08.000\nLate\n\n00:20.000 --> 00:21")
        cues = playback.vtt_cues(text)
        self.assertEqual([start for start, _ in cues], [1.0, 8.0, 3607.25])  # The cut-off last cue waits.
        segments = [(6.0, "a"), (6.0, "b")]
        first = playback.subtitle_segment(cues, segments, 0, 0, False)
        self.assertTrue(first.startswith("WEBVTT\nX-TIMESTAMP-MAP=MPEGTS:0,LOCAL:00:00:00.000\n\n00:01.000 --> 00:03.000\nHello\n\n"))
        self.assertNotIn("Eight", first)
        self.assertIn("Eight", playback.subtitle_segment(cues, segments, 1, 0, False))
        self.assertNotIn("Late", playback.subtitle_segment(cues, segments, 1, 0, False))
        self.assertIn("Late", playback.subtitle_segment(cues, segments, 1, 0, True))  # The last segment takes the rest.
        # Restarted at 1 hour: windows count from the start point.
        self.assertIn("Late", playback.subtitle_segment(cues, segments, 1, 3600, False))
        self.assertEqual(playback.vtt_cues("WEBVTT\n\n00:01.000 --> 00:03.000\nfinal line\n")[0][0], 1.0)


if __name__ == "__main__":
    unittest.main()
