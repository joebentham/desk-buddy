"""The baked Mac frames (tools/lumpbar/frames) match what the app reads.

Run: python3 -m unittest tests.test_lumpbar_frames
"""
import json, os, re, struct, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAMES = os.path.join(ROOT, "tools", "lumpbar", "frames")
LUMP = os.path.join(ROOT, "design", "lump.html")


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    return struct.unpack(">II", head[16:24])


@unittest.skipUnless(os.path.exists(os.path.join(FRAMES, "manifest.json")), "frames not baked (python3 tools/lumpbar/bake.py)")
class Frames(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(FRAMES, "manifest.json")) as f:
            self.m = json.load(f)

    def test_every_mood_the_bridge_can_send_is_baked(self):
        src = open(LUMP).read()
        moments = re.findall(r"'(\w+)'", re.search(r"const MOMENTS = \[(.*?)\]", src).group(1))
        self.assertEqual(sorted(self.m["moods"]), sorted(moments))

    def test_strips_hold_every_frame_the_ticks_point_at(self):
        for mood, info in {**self.m["moods"], **(self.m.get("events") or {})}.items():
            self.assertTrue(info["ticks"], mood)
            self.assertEqual(max(info["ticks"]) + 1, info["frames"], mood)
            for size, (fw, fh) in self.m["sizes"].items():
                for ink in ("glow", "paper"):
                    for layer in ("k", "w"):
                        name = f"{mood}-{size}-{ink}-{layer}"
                        self.assertEqual(png_size(os.path.join(FRAMES, name + ".png")), (fw * info["frames"], fh), name)

    def test_both_slots_are_baked_wide_and_at_retina_size(self):
        # the app picks "wing" beside the notch and "item" in the menu bar; both share one aspect
        wing, item = self.m["sizes"]["wing"], self.m["sizes"]["item"]
        self.assertAlmostEqual(wing[0] / wing[1], item[0] / item[1], places=1)
        self.assertGreater(wing[0], wing[1])
        self.assertGreaterEqual(item[1], 40)     # 20 pt menu bar item at 2x

    def test_the_slack_plane_is_baked(self):
        self.assertIn("tap_plane", self.m.get("events") or {})


if __name__ == "__main__":
    unittest.main()
