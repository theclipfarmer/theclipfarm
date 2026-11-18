"""Offline tests: highlight picking, captions, scheduling, title templates and a real ffmpeg render.
Run:  python -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

from worker import highlights as hl
from worker import media, titles
from worker.tick import due_slot
from worker.youtube import parse_creator_input, parse_feed


class TestHighlights(unittest.TestCase):
    def test_heatmap_picks_peaks(self):
        duration = 600
        heat = []
        for i in range(100):
            s, e = i * 6, (i + 1) * 6
            v = 0.1
            if 300 <= s < 330: v = 1.0      # big spike around 5:00
            if 480 <= s < 500: v = 0.7      # second spike around 8:00
            heat.append({"start_time": s, "end_time": e, "value": v})
        picks = hl.pick_clips(duration=duration, k=2, target=40, min_len=20, max_len=58, heatmap=heat)
        self.assertEqual(len(picks), 2)
        self.assertTrue(all(p.method == "heatmap" for p in picks))
        self.assertTrue(any(p.start <= 310 <= p.end for p in picks))
        self.assertTrue(any(p.start <= 490 <= p.end for p in picks))
        a, b = sorted(picks, key=lambda p: p.start)
        self.assertLessEqual(a.end, b.start)

    def test_caption_signal_finds_reaction(self):
        words = [(t, "and") for t in range(0, 900, 2)]
        words += [(400.0, "NO"), (400.3, "WAY"), (401, "oh"), (401.2, "my"), (401.4, "god"),
                  (402, "[Laughter]"), (403, "clip"), (403.2, "that!")]
        words.sort()
        picks = hl.pick_clips(duration=900, k=1, target=40, min_len=20, max_len=58, words=words)
        self.assertEqual(picks[0].method, "captions")
        self.assertTrue(picks[0].start <= 401 <= picks[0].end, picks)

    def test_fallback_spread(self):
        picks = hl.pick_clips(duration=1000, k=3, target=40, min_len=20, max_len=58)
        self.assertEqual(len(picks), 3)
        self.assertTrue(all(p.method == "spread" for p in picks))

    def test_audio_signal(self):
        import array, math
        sr = 8000
        a = array.array("h")
        for sec in range(200):
            amp = 20000 if 120 <= sec < 125 else 1500
            a.extend(int(amp * math.sin(i / 3)) for i in range(sr))
        sig = hl.audio_signal(a.tobytes(), sr)
        self.assertEqual(max(range(len(sig)), key=sig.__getitem__) // 5, 24)

    def test_json3_parse(self):
        data = {"events": [{"tStartMs": 1000, "segs": [{"utf8": "hello"}, {"utf8": " world", "tOffsetMs": 400}]},
                           {"tStartMs": 2000, "segs": [{"utf8": "\n"}]}]}
        self.assertEqual(hl.parse_json3(json.dumps(data)), [(1.0, "hello"), (1.4, "world")])


class TestMisc(unittest.TestCase):
    def test_creator_input(self):
        self.assertEqual(parse_creator_input("https://www.youtube.com/@SomeGuy/videos"), (None, "@SomeGuy"))
        self.assertEqual(parse_creator_input("UC" + "a" * 22), ("UC" + "a" * 22, None))
        self.assertEqual(parse_creator_input("youtube.com/channel/UC" + "b" * 22)[0], "UC" + "b" * 22)

    def test_feed(self):
        xml = """<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
        xmlns="http://www.w3.org/2005/Atom"><entry><yt:videoId>abc123</yt:videoId><title>GTA 6 is wild</title>
        <link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/><published>2026-11-18T10:00:00+00:00</published>
        </entry><entry><yt:videoId>s1</yt:videoId><title>short</title>
        <link rel="alternate" href="https://www.youtube.com/shorts/s1"/><published>2026-11-18T10:00:00+00:00</published></entry></feed>"""
        v = parse_feed(xml)
        self.assertEqual(v[0]["id"], "abc123")
        self.assertFalse(v[0]["is_short"])
        self.assertTrue(v[1]["is_short"])

    def test_due_slot(self):
        st = {"timezone": "UTC", "post_times": ["09:00", "13:00", "18:00"], "last_slot_posted": None}
        now = datetime(2026, 11, 18, 13, 30, tzinfo=timezone.utc)
        slot = due_slot(st, now)
        self.assertEqual(slot.hour, 13)
        st["last_slot_posted"] = slot.astimezone(timezone.utc).isoformat()
        self.assertIsNone(due_slot(st, now))           # already served
        late = datetime(2026, 11, 18, 17, 0, tzinfo=timezone.utc)  # 4h after 13:00, 18:00 not yet
        self.assertIsNone(due_slot({**st, "last_slot_posted": None}, late))
        catchup = datetime(2026, 11, 18, 15, 40, tzinfo=timezone.utc)  # cron ran late → still posts
        self.assertEqual(due_slot({**st, "last_slot_posted": None}, catchup).hour, 13)

    def test_templates(self):
        t = titles.fill("{hook} #GTA6 #Shorts {unknown}", hook="He jumped the bridge")
        self.assertEqual(t, "He jumped the bridge #GTA6 #Shorts")
        self.assertEqual(titles.hashtags(["GTA 6", "Grand Theft Auto VI", "GTA6"]), "#GTA6 #GrandTheftAutoVI")
        h = titles._template_hook("Trying the new heist | GTA 6 Part 2", "", 2)
        self.assertEqual(h, "Trying the new heist (Part 2)")


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class TestRender(unittest.TestCase):
    def test_render_vertical_with_captions(self):
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, "raw_1.mp4")
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
                            "-f", "lavfi", "-i", "sine=frequency=440", "-t", "12", "-c:v", "libx264",
                            "-preset", "ultrafast", "-c:a", "aac", "-shortest", src], check=True)
            words = [(100 + i * 0.4, w) for i, w in enumerate("no way he actually jumped the bridge bro what".split())]
            ass = os.path.join(tmp, "subs_1.ass")
            with open(ass, "w") as f:
                f.write(media.build_ass(clip_start=100, clip_len=10, words=words,
                                        hook="He jumped the bridge {in a car}", credit="via TestCreator"))
            out = os.path.join(tmp, "clip_1.mp4")
            media.render_vertical(src, out, length=10, ass_path=ass, offset=0.5)
            probe = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", out],
                capture_output=True, text=True, check=True).stdout)
            v = [s for s in probe["streams"] if s["codec_type"] == "video"][0]
            self.assertEqual((v["width"], v["height"]), (1080, 1920))
            self.assertAlmostEqual(float(probe["format"]["duration"]), 10, delta=0.3)
            self.assertTrue(any(s["codec_type"] == "audio" for s in probe["streams"]))
            if os.environ.get("KEEP_RENDER"):
                shutil.copy(out, os.environ["KEEP_RENDER"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
