"""Several Claude desktop apps on one computer, each its own account (class ClaudeApps in the bridge), and Lump
Control's "Claude apps" setting.

    python -m unittest discover -s tests -v
"""
import json, os, tempfile, types, unittest
from unittest import mock

from test_bridge import bridge, FakeDevice, FakeFocus, FakeSounds, NOON
from test_control import control


def usage_file(folder, t, fh, sd):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "plan-usage-history.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 2, "samples": [{"t": int(t * 1000), "org": "o", "u": {"fh": fh, "sd": sd}}]}, f)


class Apps(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="deskbuddy-apps-")
        self.support = os.path.join(self.root, "AppData")
        for app in ("Claude", "Claude-Work"):
            os.makedirs(os.path.join(self.support, app))
            open(os.path.join(self.support, app, "Local State"), "w").close()
        os.makedirs(os.path.join(self.support, "Claude-NativeHost"))      # the browser extension's helper: not an app
        self.rentals = os.path.join(self.root, ".claude-instances", "rentals")
        os.makedirs(self.rentals)
        self.cfg = os.path.join(self.root, "config.json")
        for p in (mock.patch.object(bridge, "CONFIG_FILE", self.cfg), mock.patch.object(bridge, "APP_SUPPORT", self.support),
                  mock.patch.object(bridge.time, "time", return_value=NOON)):
            p.start(); self.addCleanup(p.stop)
        self.apps = bridge.ClaudeApps()
        p = mock.patch.object(bridge, "APPS", self.apps)
        p.start(); self.addCleanup(p.stop)

    def config(self, apps):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"claude_apps": apps}, f)

    def accounts(self):
        return {a["account"]: (a["name"], a["source"]) for a in self.apps.list()}

    def test_usual_folders_alone_are_as_before(self):
        self.assertEqual(self.accounts(), {"personal": ("Main", "usual"), "work": ("Work", "usual")})

    def test_a_configured_app_is_its_own_account_and_can_name_the_usual_one(self):
        self.config([{"name": "Rentals", "path": self.rentals}, {"name": "Home", "path": os.path.join(self.support, "Claude")}])
        self.assertEqual(self.accounts(), {"personal": ("Home", "usual"), "work": ("Work", "usual"), "rentals": ("Rentals", "configured")})
        self.assertEqual(self.apps.account_for_dir(self.rentals + os.sep), "rentals")

    def test_a_running_copy_shows_up_by_itself(self):
        procs = [types.SimpleNamespace(info={"name": "Claude.exe", "exe": r"C:\Program Files\WindowsApps\Claude_5\app\Claude.exe",
                                             "cmdline": ["Claude.exe", f'--user-data-dir="{self.rentals}"']}),
                 types.SimpleNamespace(info={"name": "claude.exe", "exe": r"C:\Users\me\AppData\Roaming\Claude\claude-code\2\claude.exe",
                                             "cmdline": ["claude.exe", "--user-data-dir=C:/elsewhere"]})]     # the CLI: not an app
        with mock.patch.object(bridge, "IS_WIN", True), mock.patch("psutil.process_iter", return_value=procs):
            self.apps.detect()
        self.assertEqual(self.accounts()["rentals"], ("Rentals", "running"))
        self.assertEqual(len(self.apps.list()), 3)

    def test_accounts_never_clash_with_the_usual_ones(self):
        work = os.path.join(self.root, "other", "work"); os.makedirs(work)
        self.config([{"name": "Laptop", "path": work}])
        self.assertEqual(self.accounts()["work-2"], ("Laptop", "configured"))

    def test_renaming_keeps_the_account(self):
        self.config([{"name": "Rentals", "path": self.rentals}])
        self.assertEqual(self.accounts()["rentals"], ("Rentals", "configured"))
        self.config([{"name": "Office PC", "path": self.rentals}])
        self.assertEqual(self.accounts()["rentals"], ("Office PC", "configured"))

    def test_each_app_feeds_its_own_usage(self):
        self.config([{"name": "Rentals", "path": self.rentals}])
        usage_file(os.path.join(self.support, "Claude"), NOON - 60, 10, 20)
        usage_file(self.rentals, NOON - 60, 70, 40)
        b = bridge.Buddy(dev=FakeDevice(), focus=FakeFocus(), sounds=FakeSounds(), start=False)
        bridge.AppUsageFile(b, start=False).poll(NOON)
        self.assertEqual(b.usage["five_hour"]["pct"], 10)
        self.assertEqual(b.usage["rentals:five_hour"]["pct"], 70)
        self.assertAlmostEqual(b.usage_frac(NOON), 0.70)                   # his colour follows the busier app
        self.assertIn("rentals", [a["account"] for a in b.status()["apps"]])

    def test_the_app_in_front_and_its_sessions(self):
        self.config([{"name": "Rentals", "path": self.rentals}])
        d = os.path.join(self.rentals, "claude-code-sessions", "acct", "org"); os.makedirs(d)
        with open(os.path.join(d, "local_rrr.json"), "w", encoding="utf-8") as f:
            json.dump({"cliSessionId": "cli-rentals"}, f)
        with mock.patch.object(bridge.Focus, "SUPPORT", self.support):
            f = bridge.Focus(start=False)
            f.set_front("rentals"); f._apply("local_rrr")
            self.assertEqual(f.code_session, "cli-rentals")
            self.assertEqual(f.focused["rentals"], "cli-rentals")
        with mock.patch("psutil.Process", return_value=types.SimpleNamespace(cmdline=lambda: ["Claude.exe", f"--user-data-dir={self.rentals}"])):
            self.assertEqual(bridge.account_of_pid_win(42), "rentals")
        with mock.patch("psutil.Process", return_value=types.SimpleNamespace(cmdline=lambda: ["Claude.exe"])):
            self.assertEqual(bridge.account_of_pid_win(43), "personal")


class AppsSetting(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="deskbuddy-appsui-")
        self.cfg = os.path.join(self.root, "config.json")
        p = mock.patch.object(control, "CONFIG_FILE", self.cfg)
        p.start(); self.addCleanup(p.stop)
        self.api = control.Api(types.SimpleNamespace(status=lambda: None), art_path=os.devnull)

    def test_save_keeps_real_folders_and_names_them(self):
        a = os.path.join(self.root, "rentals"); os.makedirs(a)
        r = self.api.save_apps([{"name": "  ", "path": a}, {"name": "Dup", "path": a + os.sep}])
        self.assertTrue(r["ok"])
        self.assertEqual(control.read_json(self.cfg)["claude_apps"], [{"name": "Rentals", "path": a}])
        self.assertFalse(self.api.save_apps([{"name": "X", "path": os.path.join(self.root, "nope")}])["ok"])
        self.api.save_apps([])
        self.assertNotIn("claude_apps", control.read_json(self.cfg))

    def test_no_window_no_folder_dialog(self):
        self.assertIsNone(self.api.pick_folder())


if __name__ == "__main__":
    unittest.main()
