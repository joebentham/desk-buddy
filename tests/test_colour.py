"""His everyday body colour: config.json "body_colour", set from Lump Control's colour picker.

    python -m unittest discover -s tests -v
"""
import json, os, tempfile, unittest
from unittest import mock

from test_bridge import bridge, FakeDevice, FakeFocus, FakeSounds, NOON
from test_control import control


class BodyColour(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="deskbuddy-colour-"), "config.json")
        for p in (mock.patch.object(bridge, "CONFIG_FILE", self.path), mock.patch.object(bridge.time, "time", return_value=NOON),
                  mock.patch.object(bridge, "MEETINGS_FILE", os.path.join(os.path.dirname(self.path), "meetings.json"))):
            p.start(); self.addCleanup(p.stop)
        self.b = bridge.Buddy(dev=FakeDevice(), focus=FakeFocus(), sounds=FakeSounds(), start=False)

    def test_tangerine_unless_one_is_picked(self):
        self.assertEqual(self.b.colour(NOON), "FF7A45")
        control.set_body_colour(self.path, "4fa8ff")
        self.assertEqual(self.b.colour(NOON), "4FA8FF")

    def test_a_bad_setting_is_ignored(self):
        for bad in ("blue", "12345", "GGGGGG", 7):
            with open(self.path, "w", encoding="utf-8") as f: json.dump({"body_colour": bad}, f)
            self.assertEqual(self.b.colour(NOON), "FF7A45", bad)

    def test_the_usage_warning_starts_from_his_colour_and_ends_in_the_same_reds(self):
        for frac in (0.0, 0.5, 0.7, 0.8, 0.97, 1.0):            # default colour: exactly as before
            self.assertEqual(bridge.usage_colour(frac), bridge.usage_colour(frac, "FF7A45"))
        self.assertEqual(bridge.usage_colour(0.3, "4FA8FF"), "4FA8FF")
        self.assertEqual(bridge.usage_colour(0.8, "4FA8FF"), "FF4B2B")
        self.assertEqual(bridge.usage_colour(0.97, "4FA8FF"), "C8102E")
        self.assertNotIn(bridge.usage_colour(0.65, "4FA8FF"), ("4FA8FF", "FF4B2B"))   # on its way
        control.set_body_colour(self.path, "4FA8FF")
        self.b.set_usage({"five_hour": {"used_percentage": 85, "resets_at": NOON + 3600}})
        self.assertEqual(self.b.colour(NOON), bridge.usage_colour(round(0.85 * 50) / 50, "4FA8FF"))   # the bridge's 2% steps

    def test_the_new_colour_reaches_the_board(self):
        self.b.update()
        control.set_body_colour(self.path, "3DD68C")
        self.b.update(force=True)                                 # what POST /resync does
        self.assertIn("c 3DD68C", self.b.dev.sent)

    def test_picker_helpers(self):
        control.set_body_colour(self.path, "#9b6bff")
        self.assertEqual(control.current_body_colour(self.path), "9B6BFF")
        control.set_body_colour(self.path, "FF7A45")             # Tangerine: back to no setting
        self.assertNotIn("body_colour", control.read_json(self.path))
        control.set_body_colour(self.path, None)
        self.assertEqual(control.current_body_colour(self.path), "FF7A45")
        with self.assertRaises(ValueError):
            control.set_body_colour(self.path, "nope")


if __name__ == "__main__":
    unittest.main()
