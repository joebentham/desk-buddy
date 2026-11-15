"""Lump Control's window: the Python side the page talks to (class Api) and the reader for the board's drawings
(class Art). No window is opened; pywebview isn't needed.

    python -m unittest discover -s tests -v
"""
import base64, json, os, tempfile, unittest
from unittest import mock

from test_control import control

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.path.join(ROOT, "firmware", "data", "lump.bin")


class FakeBridge:
    def __init__(self, up=True, board=True):
        self.up, self.board, self.sent = up, board, []
    def status(self): return {"mood": "working", "device": self.board, "link": "usb", "colour": "FF7A45"} if self.up else None
    def animations(self): return ["tap", "moto", "fidget_toss", "disco"] if self.up else []
    def _r(self): return None if not self.up else self.board
    def play(self, n): self.sent.append(("play", n)); return self._r()
    def cmd(self, c): self.sent.append(("cmd", c)); return self._r()
    def resync(self): self.sent.append(("resync",)); return True if self.up else None
    def sound(self, t): self.sent.append(("sound", t)); return ("ok" if t.startswith("play") else "on") if self.up else None
    def calendar_now(self): self.sent.append(("calendar",)); return True


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="deskbuddy-ui-")
        self.cfg, self.snd = os.path.join(self.home, "config.json"), os.path.join(self.home, "sounds.json")
        for p in (mock.patch.object(control, "CONFIG_FILE", self.cfg), mock.patch.object(control, "SOUNDS_FILE", self.snd),
                  mock.patch.object(control, "HOME", self.home), mock.patch.object(control, "system_theme", lambda: "light")):
            p.start(); self.addCleanup(p.stop)
        self.bridge, self.ctl = FakeBridge(), []
        self.api = control.Api(self.bridge, art_path=ART, ctl=self.ctl.append)

    def test_state_has_what_the_page_shows(self):
        control.update_json(self.snd, {"enabled": True, "volume": 0.6, "moments": {"hello": {"on": True}, "fidget": {"on": False}}})
        control.update_json(self.cfg, {"lump_host": "lump.local", "lump_token": "x", "slack_vips": ["Ann", "Bo"], "tap_threshold": 0.5})
        s = self.api.state()
        self.assertEqual(s["status"]["mood"], "working")
        self.assertEqual(s["theme"], "light")                    # follows Windows until one is picked
        self.assertEqual(s["colour"], "FF7A45")
        self.assertEqual(s["tap"], 0.5)
        self.assertEqual(s["sounds"], {"enabled": True, "volume": 0.6, "quiet_at_night": True, "moments": {"fidget": False, "hello": True}})
        self.assertEqual(s["settings"]["slack_vips"], "Ann, Bo")
        self.assertTrue(s["settings"]["lump_paired"])
        self.assertNotIn("lump_token", s["settings"])              # the pairing token never goes to the page

    def test_theme_is_remembered(self):
        self.api.set_theme("dark")
        self.assertEqual(self.api.state()["theme"], "dark")
        self.api.set_theme("system")
        self.assertNotIn("ui_theme", control.read_json(self.cfg))

    def test_actions_reach_the_bridge_and_say_how_it_went(self):
        self.assertTrue(self.api.play("disco")["ok"])
        self.assertTrue(self.api.preview("alert")["ok"])
        self.assertFalse(self.api.preview("nonsense")["ok"])
        self.assertTrue(self.api.preview("demo")["ok"])
        self.assertTrue(self.api.live()["ok"])
        self.assertIn(("cmd", "s alert"), self.bridge.sent)
        self.assertIn(("cmd", "s demo"), self.bridge.sent)
        self.assertNotIn(("cmd", "s nonsense"), self.bridge.sent)
        self.bridge.board = False
        self.assertIn("isn't connected", self.api.play("disco")["message"])
        self.bridge.up = False
        self.assertIn("isn't running", self.api.play("disco")["message"])

    def test_tap_sensitivity_is_saved_clamped_and_sent(self):
        self.api.set_tap(0.5)
        self.assertEqual(control.read_json(self.cfg)["tap_threshold"], 0.5)
        self.assertIn(("cmd", "T 0.50"), self.bridge.sent)
        self.api.set_tap(9)
        self.assertIn(("cmd", "T 1.50"), self.bridge.sent)
        self.api.set_tap(0.35)                                   # the default: no setting
        self.assertNotIn("tap_threshold", control.read_json(self.cfg))

    def test_colour(self):
        self.assertTrue(self.api.set_colour("4FA8FF")["ok"])
        self.assertEqual(control.read_json(self.cfg)["body_colour"], "4FA8FF")
        self.assertIn(("resync",), self.bridge.sent)
        self.assertFalse(self.api.set_colour("nope")["ok"])
        self.api.set_colour(None)
        self.assertNotIn("body_colour", control.read_json(self.cfg))

    def test_sounds(self):
        self.api.set_sound(True)
        self.assertIn(("sound", "on"), self.bridge.sent)
        self.bridge.up = False
        self.api.set_sound(False)                                # bridge down: written to the file instead
        self.assertFalse(control.read_json(self.snd)["enabled"])
        self.api.set_volume(5); self.assertEqual(control.read_json(self.snd)["volume"], 2.0)
        self.api.set_quiet(False); self.assertFalse(control.read_json(self.snd)["quiet_at_night"])
        self.api.set_moment("hello", False); self.assertEqual(control.read_json(self.snd)["moments"]["hello"], {"on": False})

    def test_settings(self):
        r = self.api.save_settings({"lump_host": " lump.local ", "calendar_ics": "https://x/cal.ics", "my_email": "",
                                    "slack_token": "", "slack_vips": "Ann, Bo", "slack_vip_minutes": "15"})
        self.assertTrue(r["ok"])
        cfg = control.read_json(self.cfg)
        self.assertEqual((cfg["lump_host"], cfg["slack_vips"], cfg["slack_vip_minutes"]), ("lump.local", ["Ann", "Bo"], 15.0))
        self.assertNotIn("my_email", cfg)
        self.assertFalse(self.api.save_settings({"slack_vip_minutes": "soon"})["ok"])

    def test_gag_names_are_friendly(self):
        labels = {a["name"]: a["label"] for a in self.api.animations()}
        self.assertEqual(labels, {"tap": "Random gag", "moto": "Ring of fire", "fidget_toss": "Fidget: toss", "disco": "Disco"})

    def test_bridge_start_and_restart(self):
        self.api.bridge_ctl("start"); self.api.bridge_ctl("restart")
        self.assertEqual(self.ctl, ["Start", "Stop", "Start"])
        self.assertFalse(self.api.bridge_ctl("format c:")["ok"])

    def test_only_its_own_files_open(self):
        with mock.patch.object(control.os, "startfile", create=True) as sf:
            self.assertFalse(self.api.open("C:/Windows")["ok"])
            sf.assert_not_called()


class AnimationsAreMirrored(unittest.TestCase):
    """The board announces each one-off animation ("event <name>"); /status reports it as "playing" for a while."""
    def test_event_lines_become_playing(self):
        from test_bridge import bridge, FakeDevice, FakeFocus, FakeSounds, NOON
        t = [NOON]
        with mock.patch.object(bridge.time, "time", side_effect=lambda: t[0]):
            b = bridge.Buddy(dev=FakeDevice(), focus=FakeFocus(), sounds=FakeSounds(), start=False)
            self.assertIsNone(b.status()["playing"])
            b.board_line("event tap_disco_working")
            t[0] += 1.5
            self.assertEqual(b.status()["playing"], {"name": "tap_disco_working", "age": 1.5})
            b.board_line("event ../../etc")                  # not a name: ignored
            self.assertEqual(b.status()["playing"]["name"], "tap_disco_working")
            t[0] += 60
            self.assertIsNone(b.status()["playing"])        # long over

    @unittest.skipUnless(os.path.exists(ART), "needs firmware/data/lump.bin")
    def test_the_window_gets_the_same_frames(self):
        api = control.Api(FakeBridge(), art_path=ART)
        e = api.screen_event("fidget_toss")
        self.assertEqual(len(e["ticks"]), 50)
        self.assertTrue(api.screen_event("tap_disco_working"))
        self.assertIsNone(api.screen_event("no_such_thing"))


class PreviewIsWhatHeShows(unittest.TestCase):
    """A mood previewed from the Play tab goes straight to the board; /status reports it as "shown" until the
    bridge next sends his real mood."""
    def test_preview_shown_until_the_bridge_sends_his_mood(self):
        from test_bridge import bridge, FakeDevice, FakeFocus, FakeSounds, NOON
        with mock.patch.object(bridge.time, "time", return_value=NOON):
            b = bridge.Buddy(dev=FakeDevice(), focus=FakeFocus(), sounds=FakeSounds(), start=False)
            b.update()
            self.assertEqual(b.status()["shown"], b.status()["mood"])
            b.note_command("s alert")
            self.assertEqual(b.status()["shown"], "alert")
            b.update()                                       # nothing changed: the preview stays on the board
            self.assertEqual(b.status()["shown"], "alert")
            b.note_command("T 0.50")                         # not a mood
            self.assertEqual(b.status()["shown"], "alert")
            b.update(force=True)                             # Back to live (POST /resync)
            self.assertEqual(b.status()["shown"], b.status()["mood"])
class RemoteLumpInTheWindow(ApiTest):
    def test_relay_state_never_includes_the_token(self):
        self.assertFalse(self.api.state()["relay"]["set_up"])
        control.new_relay_token(self.cfg)
        s = self.api.state()
        self.assertTrue(s["relay"]["set_up"])
        self.assertNotIn(control.read_json(self.cfg)["relay_token"], json.dumps(s))

    def test_copy_goes_to_the_clipboard_from_python(self):
        self.assertFalse(self.api.copy_relay_token()["ok"])          # not set up
        control.new_relay_token(self.cfg)
        tok = control.read_json(self.cfg)["relay_token"]
        with mock.patch.object(control.subprocess, "run") as run:
            r = self.api.copy_relay_token()
        self.assertTrue(r["ok"])
        self.assertNotIn(tok, json.dumps(r))
        self.assertEqual(run.call_args[0][0], ["clip"])
        self.assertEqual(run.call_args[1]["input"], tok.encode("ascii"))

    def test_new_token_replaces_the_old_one(self):
        control.new_relay_token(self.cfg)
        old = control.read_json(self.cfg)["relay_token"]
        self.assertTrue(self.api.renew_relay_token()["ok"])
        self.assertNotEqual(control.read_json(self.cfg)["relay_token"], old)


@unittest.skipUnless(os.path.exists(ART), "needs firmware/data/lump.bin")
class BoardDrawings(unittest.TestCase):
    def test_each_mood_loop_is_whole_frames_of_the_board_data(self):
        art = control.Art(ART)
        for mood in ("idle", "working", "alert", "done", "night", "letter", "meeting"):
            m = art.mood(mood)
            self.assertEqual(m["size"], 240)
            self.assertTrue(m["ticks"] and max(m["ticks"]) < len(m["frames"]))
            px = 0
            raw = base64.b64decode(m["frames"][0])
            for p in range(0, len(raw) - 2, 3):
                px += raw[p]
            self.assertGreaterEqual(px, 240 * 240)                 # the runs cover the whole frame

    def test_unknown_mood_falls_back_to_idle_and_bad_file_is_refused(self):
        art = control.Art(ART)
        self.assertEqual(art.mood("nonsense")["ticks"], art.mood("idle")["ticks"])
        bad = os.path.join(tempfile.mkdtemp(), "x.bin")
        with open(bad, "wb") as f: f.write(b"NOPE" + b"\0" * 64)
        with self.assertRaises(ValueError):
            control.Art(bad)
        self.assertIsNone(control.Api(FakeBridge(), art_path=bad).screen("idle"))


if __name__ == "__main__":
    unittest.main()
