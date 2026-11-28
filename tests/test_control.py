"""Lump Control (tools/windows/control.py): its bridge client and settings handling. No window, no tray:
the GUI parts are imported only when the app runs.

    python -m unittest discover -s tests -v
"""
import http.server, importlib.util, json, os, tempfile, threading, unittest
from unittest import mock

from test_bridge import bridge, FakeDevice, FakeFocus, FakeSounds

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("lump_control", os.path.join(ROOT, "tools", "windows", "control.py"))
control = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(control)


class FakeBridge(http.server.BaseHTTPRequestHandler):
    """Answers like the bridge's HTTP API and remembers what it was sent."""
    got = []

    def log_message(self, *a): pass

    def reply(self, code, body):
        b = body.encode()
        self.send_response(code); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == "/status": self.reply(200, json.dumps({"mood": "working", "device": True, "link": "usb"}))
        elif self.path == "/play": self.reply(200, json.dumps(["tap", "disco"]))
        else: self.reply(404, "")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        FakeBridge.got.append((self.path, body))
        if self.path == "/play": self.reply(200 if body == "disco" else 404, "")
        elif self.path == "/cmd": self.reply(503, "board not connected") if body == "offline" else self.reply(200, "ok")
        elif self.path == "/sound": self.reply(200, "on")
        else: self.reply(200, "ok")


class BridgeClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.b = control.Bridge(f"http://127.0.0.1:{cls.srv.server_address[1]}")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        FakeBridge.got.clear()

    def test_reads_status_and_animations(self):
        self.assertEqual(self.b.status()["mood"], "working")
        self.assertEqual(self.b.animations(), ["tap", "disco"])

    def test_sends_commands(self):
        self.assertTrue(self.b.play("disco"))
        self.assertFalse(self.b.play("nope"))
        self.assertTrue(self.b.cmd("s working"))
        self.assertFalse(self.b.cmd("offline"))               # bridge up, board not connected
        self.assertTrue(self.b.resync())
        self.assertEqual(self.b.sound("on"), "on")
        self.assertIn(("/cmd", "s working"), FakeBridge.got)
        self.assertIn(("/resync", ""), FakeBridge.got)

    def test_bridge_not_running_is_none(self):
        down = control.Bridge("http://127.0.0.1:9", timeout=0.5)
        self.assertIsNone(down.status())
        self.assertIsNone(down.play("disco"))
        self.assertIsNone(down.cmd("s idle"))
        self.assertEqual(down.animations(), [])
        self.assertEqual(control.describe(None), "Bridge not running")

    def test_describe(self):
        self.assertEqual(control.describe({"mood": "alert", "device": True, "link": "wifi"}), "alert · board on Wi-Fi")
        self.assertEqual(control.describe({"mood": "idle", "device": False}), "idle · board not connected")


class SettingsFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="deskbuddy-control-")
        self.path = os.path.join(self.dir, "config.json")

    def test_update_keeps_other_keys_and_removes_blanks(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"lump_token": "abc", "slack_token": "xoxp-1", "note": "Lump’s"}, f, ensure_ascii=False)
        control.update_json(self.path, {"lump_host": "lumps-pc", "slack_token": "", "slack_vips": None})
        with open(self.path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(json.loads(text), {"lump_token": "abc", "note": "Lump’s", "lump_host": "lumps-pc"})
        self.assertIn("Lump’s", text)                          # written as is, not \u-escaped

    def test_missing_or_broken_file_reads_empty(self):
        self.assertEqual(control.read_json(self.path), {})
        with open(self.path, "w") as f: f.write("{nope")
        self.assertEqual(control.read_json(self.path), {})
        control.update_json(self.path, {"a": 1})
        self.assertEqual(control.read_json(self.path), {"a": 1})

    def test_moment_switch_keeps_its_picks(self):
        p = os.path.join(self.dir, "sounds.json")
        control.update_json(p, {"enabled": True, "moments": {"needs_you": {"on": True, "pick": ["a", "c"]}}})
        control.set_moment(p, "needs_you", False)
        control.set_moment(p, "goodnight", True)
        m = control.read_json(p)["moments"]
        self.assertEqual(m["needs_you"], {"on": False, "pick": ["a", "c"]})
        self.assertEqual(m["goodnight"], {"on": True})
        self.assertTrue(control.read_json(p)["enabled"])

    def test_vips(self):
        self.assertEqual(control.parse_vips(" Ann Lee, Bob ,,\nCara "), ["Ann Lee", "Bob", "Cara"])

    def test_written_settings_are_what_the_bridge_reads(self):
        with mock.patch.object(bridge, "CONFIG_FILE", self.path):
            control.update_json(self.path, {"slack_vips": ["Ann"], "slack_vip_minutes": 10.0, "tap_threshold": 0.5})
            self.assertEqual(bridge.read_config()["slack_vips"], ["Ann"])
            b = bridge.Buddy(dev=FakeDevice(), focus=FakeFocus(), sounds=FakeSounds(), start=False)
            self.assertEqual(b.letter_hold(), 600)


class TapSensitivity(unittest.TestCase):
    """The board forgets its tap sensitivity when it restarts; the bridge resends the saved one."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="deskbuddy-tap-"), "config.json")
        p = mock.patch.object(bridge, "CONFIG_FILE", self.path)
        p.start(); self.addCleanup(p.stop)
        self.b = bridge.Buddy(dev=FakeDevice(), focus=FakeFocus(), sounds=FakeSounds(), start=False)

    def test_saved_sensitivity_is_resent_when_the_board_comes_back(self):
        control.update_json(self.path, {"tap_threshold": 0.5})
        self.b.resend()
        self.assertIn("T 0.50", self.b.dev.sent)

    def test_nothing_sent_without_one(self):
        self.b.resend()
        self.assertFalse(any(c.startswith("T ") for c in self.b.dev.sent))


if __name__ == "__main__":
    unittest.main()
