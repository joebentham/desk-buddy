"""Windows support, and proof that the macOS paths didn't change along the way.

    python -m unittest discover -s tests -v

MacPaths pins the Mac-only calls (afplay, lsappinfo, the /dev/cu.usbmodem* glob), loads the bridge as a Mac
would (no psutil, no winsound) and checks install_hooks.py still writes exactly what it wrote before Windows
support (tests/golden/). WindowsPaths covers the Windows stand-ins; the few that need a real Windows box
(Windows path rules, the Windows install) are skipped elsewhere.
"""
import importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time, types, unittest, wave
from unittest import mock

from test_bridge import bridge, FakeDevice, FakeFocus, FakeSounds, NOON

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
ON_WINDOWS = sys.platform == "win32"


def write_wav(path, value=1000, frames=4410):
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(int(value).to_bytes(2, "little", signed=True) * frames)


def run_install_hooks(home, *args, platform=None):
    """Run tools/install_hooks.py with HOME pointing at a temp folder (optionally pretending to be another platform)."""
    env = dict(os.environ, HOME=home, USERPROFILE=home)
    script = os.path.join(TOOLS, "install_hooks.py")
    if platform:
        code = (f"import sys, runpy; sys.platform = {platform!r}; sys.argv = [{script!r}] + {list(args)!r}; "
                f"runpy.run_path({script!r}, run_name='__main__')")
        cmd = [sys.executable, "-I", "-c", code]
    else:
        cmd = [sys.executable, "-I", script, *args]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, encoding="utf-8", check=True).stdout


class MacPaths(unittest.TestCase):
    """The Mac-only calls, pinned (IS_WIN is off in this module, as test_bridge sets it)."""

    def test_usb_ports_glob(self):
        with mock.patch.object(bridge.glob, "glob", return_value=["/dev/cu.usbmodem2", "/dev/cu.usbmodem1"]) as g:
            self.assertEqual(bridge.Device.usb_ports(), ["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"])
        g.assert_called_once_with("/dev/cu.usbmodem*")

    def test_front_app_from_lsappinfo(self):
        calls = []
        def run(argv, **kw):
            calls.append(argv)
            out = "ASN:0x0-0x1:" if argv == ["lsappinfo", "front"] else '"CFBundleIdentifier"="com.apple.Terminal"'
            return types.SimpleNamespace(stdout=out)
        f = bridge.Focus(start=False)
        with mock.patch.object(bridge.subprocess, "run", side_effect=run):
            self.assertIsNone(f.front_now())
        self.assertEqual(calls, [["lsappinfo", "front"], ["lsappinfo", "info", "-only", "bundleid", "-only", "pid", "ASN:0x0-0x1:"]])

    def test_slack_in_front_from_lsappinfo(self):
        outs = iter(["ASN:0x0-0x2:", '"CFBundleIdentifier"="com.tinyspeck.slackmacgap"'])
        with mock.patch.object(bridge.subprocess, "run", side_effect=lambda argv, **kw: types.SimpleNamespace(stdout=next(outs))) as run:
            self.assertTrue(bridge.SlackWatch.slack_in_front())
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["lsappinfo", "front"], ["lsappinfo", "info", "-only", "bundleID", "ASN:0x0-0x2:"]])

    def test_sounds_go_through_afplay(self):
        with mock.patch.object(bridge.subprocess, "Popen") as popen:
            bridge.start_sound("/x/hello_a.wav", 0.4)
        popen.assert_called_once_with(["afplay", "-v", "0.4", "/x/hello_a.wav"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_loads_as_on_a_mac_without_windows_modules(self):
        """No psutil or winsound (a Mac's venv has neither): the bridge still loads and builds its Mac parts."""
        home = tempfile.mkdtemp(prefix="deskbuddy-mac-")
        spec = importlib.util.spec_from_file_location("bridge_as_mac", os.path.join(TOOLS, "buddy_bridge.py"))
        mod = importlib.util.module_from_spec(spec)
        with mock.patch.object(sys, "platform", "darwin"), \
             mock.patch.dict(sys.modules, {"psutil": None, "winsound": None}), \
             mock.patch.dict(os.environ, {"DESKBUDDY_HOME": home}):
            spec.loader.exec_module(mod)
            try:
                self.assertFalse(mod.IS_WIN)
                self.assertTrue(mod.Focus.LOGS.replace("\\", "/").endswith("Library/Logs/Claude"))
                mod.Focus(start=False); mod.Sounds()
                with mock.patch.object(mod.glob, "glob", return_value=[]) as g:
                    mod.Device.usb_ports()
                g.assert_called_once_with("/dev/cu.usbmodem*")
            finally:
                for h in list(mod.log.handlers):
                    if getattr(h, "baseFilename", "").startswith(home):
                        mod.log.removeHandler(h); h.close()
        shutil.rmtree(home, ignore_errors=True)

    def test_no_windows_module_imported_at_top_level(self):
        with open(os.path.join(TOOLS, "buddy_bridge.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIsNone(re.search(r"^(import|from) .*(psutil|winsound|wintypes|list_ports)", src, re.M))

    def test_install_hooks_writes_what_it_always_did(self):
        """install_hooks.py as a Mac runs it gives byte-for-byte the settings.json of the pre-Windows script
        (tests/golden, captured from it; line endings aside, which only the Windows capture changed)."""
        inputs = {"empty": {}, "existing": {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
                                           "statusLine": {"type": "command", "command": "my-status"}}}
        for name, data in inputs.items():
            with self.subTest(name):
                home = tempfile.mkdtemp(); cfg = os.path.join(home, ".claude"); os.makedirs(cfg)
                with open(os.path.join(cfg, "settings.json"), "w", encoding="utf-8") as f: json.dump(data, f)
                run_install_hooks(home, "--config-dir", cfg, platform="darwin")
                with open(os.path.join(cfg, "settings.json"), "rb") as f:
                    got = f.read().decode("utf-8").replace(json.dumps(home)[1:-1], "<HOME>")
                with open(os.path.join(ROOT, "tests", "golden", f"install_hooks_{name}.json"), "rb") as f:
                    want = f.read().decode("utf-8")
                self.assertEqual(got.replace("\r\n", "\n"), want.replace("\r\n", "\n"))
                shutil.rmtree(home, ignore_errors=True)


class WindowsPaths(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(bridge, "IS_WIN", True)
        p.start(); self.addCleanup(p.stop)

    def test_board_found_by_its_usb_chip(self):
        ports = [types.SimpleNamespace(device="COM3", vid=0x07FD, pid=0x000B),
                 types.SimpleNamespace(device="COM7", vid=0x1A86, pid=0x55D3),
                 types.SimpleNamespace(device="COM4", vid=0x1A86, pid=0x55D3),
                 types.SimpleNamespace(device="COM9", vid=None, pid=None)]
        with mock.patch("serial.tools.list_ports.comports", return_value=ports):
            self.assertEqual(bridge.Device.usb_ports(), ["COM4", "COM7"])

    def test_accounts_from_windows_paths(self):
        self.assertEqual(bridge.account_of_config(r"C:\Users\me\.claude-work\projects\x\a.jsonl"), "work")
        self.assertEqual(bridge.account_of_config(r"C:\Users\me\.claude\projects\x\a.jsonl"), "personal")

    @unittest.skipUnless(ON_WINDOWS, "Windows path rules")
    def test_account_of_windows_data_dir(self):
        self.assertEqual(bridge.account_of_data_dir("C:\\Users\\me\\AppData\\Roaming\\Claude-Work\\"), "work")
        self.assertEqual(bridge.account_of_data_dir(r"C:\Users\me\AppData\Roaming\Claude"), "personal")

    def test_claude_app_versus_claude_code(self):
        self.assertTrue(bridge.is_claude_app(r"C:\Program Files\WindowsApps\Claude_5.1_x64__abc\app\Claude.exe"))
        self.assertFalse(bridge.is_claude_app(r"C:\Users\me\AppData\Roaming\Claude\claude-code\2.1\x\claude.exe"))
        self.assertFalse(bridge.is_claude_app(r"C:\Windows\explorer.exe"))
        self.assertFalse(bridge.is_claude_app(""))

    def test_claude_copy_in_front(self):
        f = bridge.Focus(start=False)
        app = r"C:\Program Files\WindowsApps\Claude_5\app\Claude.exe"
        with mock.patch.object(bridge, "account_of_pid_win", lambda pid: "work" if pid == 7 else "personal"):
            with mock.patch.object(bridge, "win_foreground", return_value=(7, app)):
                self.assertEqual(f.front_now(), "work")
            with mock.patch.object(bridge, "win_foreground", return_value=(8, app)):
                self.assertEqual(f.front_now(), "personal")
            with mock.patch.object(bridge, "win_foreground", return_value=(9, r"C:\Windows\explorer.exe")):
                self.assertIsNone(f.front_now())
            with mock.patch.object(bridge, "win_foreground", return_value=(None, "")):
                self.assertIsNone(f.front_now())

    def test_session_file_with_curly_quotes_still_maps(self):
        support = tempfile.mkdtemp(prefix="deskbuddy-support-")
        d = os.path.join(support, "Claude", "claude-code-sessions", "acct", "org"); os.makedirs(d)
        with open(os.path.join(d, "local_ccc.json"), "w", encoding="utf-8") as f:
            json.dump({"title": "Lump’s “fix” 🎉", "cliSessionId": "cli-1"}, f, ensure_ascii=False)
        with mock.patch.object(bridge.Focus, "SUPPORT", support):
            f = bridge.Focus(start=False)
            f.set_front("personal"); f._apply("local_ccc")
            self.assertEqual(f.code_session, "cli-1")

    def test_sounds_go_through_winsound(self):
        with mock.patch.object(bridge, "WinSound") as ws, mock.patch.object(bridge.subprocess, "Popen") as popen:
            bridge.start_sound(r"C:\x\hello_a.wav", 0.4)
        ws.assert_called_once_with(r"C:\x\hello_a.wav", 0.4)
        popen.assert_not_called()

    def test_volume_is_a_scaled_copy(self):
        d = tempfile.mkdtemp(prefix="deskbuddy-vol-")
        src = os.path.join(d, "hello_a.wav"); write_wav(src, 1000)
        with mock.patch.object(bridge, "HOME", d):
            self.assertEqual(bridge.WinSound.scaled(src, 1.0), src)
            out = bridge.WinSound.scaled(src, 0.5)
            self.assertNotEqual(out, src)
            with wave.open(out) as w:
                self.assertEqual(int.from_bytes(w.readframes(1), "little", signed=True), 500)
            self.assertEqual(bridge.WinSound.scaled(src, 0.5), out)           # cached

    def test_gag_sound_lead_uses_winsounds_lag(self):
        d = tempfile.mkdtemp(prefix="deskbuddy-gag-")
        write_wav(os.path.join(d, "tap_shock.wav"))
        cfg = os.path.join(d, "sounds.json")
        with open(cfg, "w", encoding="utf-8") as f: json.dump({"enabled": True}, f)
        timers = []
        with mock.patch.object(bridge, "SOUNDS_DIR", d), mock.patch.object(bridge, "SOUNDS_FILE", cfg), \
             mock.patch.object(bridge.threading, "Timer", side_effect=lambda dl, fn, args: timers.append(dl) or mock.Mock()):
            self.assertTrue(bridge.Sounds().play_gag("tap_shock_idle"))
        self.assertAlmostEqual(timers[-1], bridge.GAG_LEAD - bridge.WINSOUND_LAG)

    def test_slack_in_front_from_the_foreground_window(self):
        with mock.patch.object(bridge, "win_foreground", return_value=(5, r"C:\Users\me\AppData\Local\slack\app-4\slack.exe")):
            self.assertTrue(bridge.SlackWatch.slack_in_front())
        with mock.patch.object(bridge, "win_foreground", return_value=(5, r"C:\Windows\explorer.exe")):
            self.assertFalse(bridge.SlackWatch.slack_in_front())


class DesktopAppUsage(unittest.TestCase):
    """plan-usage-history.json from the Claude desktop app (no status line there)."""

    def setUp(self):
        self.support = tempfile.mkdtemp(prefix="deskbuddy-usage-")
        os.makedirs(os.path.join(self.support, "Claude"))
        p = mock.patch.object(bridge.time, "time", return_value=NOON)
        p.start(); self.addCleanup(p.stop)
        self.b = bridge.Buddy(dev=FakeDevice(), focus=FakeFocus(), sounds=FakeSounds(), start=False)
        self.w = bridge.AppUsageFile(self.b, support=self.support, start=False)

    def samples(self, *pairs, folder="Claude"):
        with open(os.path.join(self.support, folder, "plan-usage-history.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 2, "samples": [{"t": int(t * 1000), "org": "o", "u": {"fh": fh, "sd": sd}} for t, fh, sd in pairs]}, f)

    def test_newest_fresh_sample_sets_usage(self):
        self.samples((NOON - 1800, 10, 1), (NOON - 300, 40, 12))
        self.w.poll(NOON)
        self.assertEqual(self.b.usage["five_hour"]["pct"], 40)
        self.assertEqual(self.b.usage["seven_day"]["pct"], 12)
        self.assertEqual(self.b.usage["five_hour"]["source"], "app")
        self.assertEqual(self.b.usage["five_hour"]["t"], NOON - 300)

    def test_stale_or_missing_history_is_ignored(self):
        self.w.poll(NOON)                                           # no file yet
        self.samples((NOON - 3 * 3600, 90, 50))
        self.w.poll(NOON)
        self.assertEqual(self.b.usage, {})

    def test_a_status_line_reading_is_not_overwritten_by_an_older_sample(self):
        self.samples((NOON - 300, 40, 12))
        self.b.set_usage({"five_hour": {"used_percentage": 55, "resets_at": NOON + 3600}})     # CLI status line, now
        self.w.poll(NOON)
        self.w.poll(NOON + 60)                                     # the same sample again
        self.assertEqual(self.b.usage["five_hour"]["pct"], 55)
        self.assertEqual(self.b.usage["five_hour"]["source"], "statusline")
        self.assertEqual(self.b.usage["seven_day"]["pct"], 12)     # nothing fresher for this one

    def test_a_newer_sample_does_replace_an_older_reading(self):
        self.samples((NOON - 300, 40, 12))
        self.w.poll(NOON)
        self.samples((NOON - 300, 40, 12), (NOON - 10, 45, 13))
        self.w.poll(NOON)
        self.assertEqual(self.b.usage["five_hour"]["pct"], 45)

    def test_a_second_account_has_its_own_history(self):
        os.makedirs(os.path.join(self.support, "Claude-Work"))
        self.samples((NOON - 60, 70, 20), folder="Claude-Work")
        self.w.poll(NOON)
        self.assertEqual(self.b.usage["work:five_hour"]["pct"], 70)


class StatusLineScript(unittest.TestCase):
    def run_it(self, payload):
        env = dict(os.environ, DESKBUDDY_USAGE_URL="http://127.0.0.1:9/usage")   # never the real bridge
        return subprocess.run([sys.executable, "-I", os.path.join(TOOLS, "statusline.py")], input=payload,
                              capture_output=True, timeout=10, env=env).stdout

    def test_prints_the_same_line_as_statusline_sh_in_utf8(self):
        out = self.run_it(json.dumps({"model": {"display_name": "Opus ’5’"},
                                      "rate_limits": {"five_hour": {"used_percentage": 12.7}, "seven_day": {"used_percentage": 3}}},
                                     ensure_ascii=False).encode("utf-8"))
        self.assertEqual(out.decode("utf-8").strip(), "Opus ’5’ · 5h 12% · week 3%")

    def test_missing_parts_are_left_out(self):
        self.assertEqual(self.run_it(b'{"model": {"display_name": "Sonnet"}}').decode("utf-8").strip(), "Sonnet")
        self.assertEqual(self.run_it(b"not json").decode("utf-8").strip(), "")


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WifiSetup(unittest.TestCase):
    """tools/wifi_setup.py: the Mac path pinned, the Windows one checked."""

    def setUp(self):
        self.w = load_tool("wifi_setup")

    def test_mac_finds_the_board_and_pauses_the_bridge_as_before(self):
        with mock.patch.object(self.w, "IS_WIN", False):
            with mock.patch.object(self.w.glob, "glob", return_value=["/dev/cu.usbmodem1"]) as g:
                self.assertEqual(self.w.usb_ports(), ["/dev/cu.usbmodem1"])
            g.assert_called_once_with("/dev/cu.usbmodem*")
            with mock.patch.object(self.w.os, "getuid", create=True, return_value=501), \
                 mock.patch.object(self.w.subprocess, "run") as run:
                self.w.bridge_ctl(start=False)
                self.w.bridge_ctl(start=True)
            self.assertEqual(run.call_args_list, [
                mock.call(["launchctl", "bootout", "gui/501/com.deskbuddy.bridge"], capture_output=True),
                mock.call(["launchctl", "bootstrap", "gui/501", self.w.BRIDGE_PLIST], capture_output=True)])

    def test_windows_finds_the_board_by_its_usb_chip_and_pauses_the_installed_bridge(self):
        ports = [types.SimpleNamespace(device="COM3", vid=0x07FD, pid=0x000B), types.SimpleNamespace(device="COM4", vid=0x1A86, pid=0x55D3)]
        with mock.patch.object(self.w, "IS_WIN", True):
            with mock.patch("serial.tools.list_ports.comports", return_value=ports):
                self.assertEqual(self.w.usb_ports(), ["COM4"])
            with mock.patch.object(self.w.subprocess, "run") as run:
                self.w.bridge_ctl(start=False)
            argv = run.call_args[0][0]
            self.assertEqual(argv[0], "powershell")
            self.assertTrue(argv[-2].endswith(os.path.join("tools", "windows", "bridge.ps1")))
            self.assertEqual(argv[-1], "-Stop")

    def test_windows_time_zone_as_a_posix_rule(self):
        tz = self.w.posix_tz_from_windows
        self.assertEqual(tz(360, 0, -60, (11, 1, 0, 2), (3, 2, 0, 2)), "STD6DST5,M3.2.0/2,M11.1.0/2")      # US Central
        self.assertEqual(tz(0, 0, -60, (10, 5, 0, 2), (3, 5, 0, 1)), "STD0DST-1,M3.5.0/1,M10.5.0/2")      # London
        self.assertEqual(tz(-330, 0, 0, (0, 0, 0, 0), (0, 0, 0, 0)), "STD-5:30")                          # India, no DST

    @unittest.skipUnless(ON_WINDOWS, "reads this PC's time zone")
    def test_this_pcs_time_zone_reads(self):
        self.assertRegex(self.w.windows_posix_tz(), r"^STD-?\d")


@unittest.skipUnless(ON_WINDOWS, "the Windows install")
class WindowsInstallHooks(unittest.TestCase):
    OTHER = {"model": "opus", "note": "Lump’s settings 🎉", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="deskbuddy-hooks-")
        self.cfg = os.path.join(self.home, ".claude"); os.makedirs(self.cfg)
        self.path = os.path.join(self.cfg, "settings.json")
        self.addCleanup(shutil.rmtree, self.home, True)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False)

    def read(self):
        with open(self.path, encoding="utf-8") as f: return json.load(f)

    def test_install_twice_then_uninstall(self):
        self.write(self.OTHER)
        run_install_hooks(self.home, "--config-dir", self.cfg)
        s = self.read()
        self.assertEqual(len(s["hooks"]["Stop"]), 2)
        self.assertEqual(s["hooks"]["Stop"][1]["hooks"][0]["command"], self.mac_hook())
        self.assertRegex(s["statusLine"]["command"], r"^\S+\.exe -I \S+/\.desk-buddy/statusline\.py$")
        self.assertNotIn("\\", s["statusLine"]["command"])
        self.assertTrue(os.path.exists(os.path.join(self.home, ".desk-buddy", "statusline.py")))
        self.assertIn("Nothing to change", run_install_hooks(self.home, "--config-dir", self.cfg))
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("Lump’s settings 🎉", f.read())                # kept as written, not \u-escaped
        run_install_hooks(self.home, "--config-dir", self.cfg, "--uninstall")
        self.assertEqual(self.read(), self.OTHER)

    def test_existing_status_line_is_left_alone(self):
        self.write({"statusLine": {"type": "command", "command": 'bash "$HOME/.claude/coralline/statusline.sh"'}})
        out = run_install_hooks(self.home, "--config-dir", self.cfg)
        self.assertIn("left alone", out)
        self.assertEqual(self.read()["statusLine"]["command"], 'bash "$HOME/.claude/coralline/statusline.sh"')
        run_install_hooks(self.home, "--config-dir", self.cfg, "--uninstall")
        self.assertEqual(self.read()["statusLine"]["command"], 'bash "$HOME/.claude/coralline/statusline.sh"')

    def test_a_settings_file_shared_with_a_mac_needs_no_changes(self):
        with open(os.path.join(ROOT, "tests", "golden", "install_hooks_existing.json"), encoding="utf-8") as f:
            mac = json.load(f)
        self.write(mac)
        self.assertIn("Nothing to change", run_install_hooks(self.home, "--config-dir", self.cfg))
        self.assertEqual(self.read(), mac)

    @staticmethod
    def mac_hook():
        with open(os.path.join(ROOT, "tests", "golden", "install_hooks_empty.json"), encoding="utf-8") as f:
            return json.load(f)["hooks"]["Stop"][0]["hooks"][0]["command"]


if __name__ == "__main__":
    unittest.main()
