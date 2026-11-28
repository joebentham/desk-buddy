"""The remote link: the bridge's relay listener and link choice (tools/buddy_bridge.py), the relay itself
(tools/lump_relay.py), and the installer / Lump Control pieces that set it up.

    python -m unittest discover -s tests -v

Nothing here touches the real board, port 4211, ~/.desk-buddy or the network beyond 127.0.0.1: listeners bind
ephemeral localhost ports, the relay is handed a fake resolver (Tailscale addresses that are really 127.0.0.1)
and fake boards, and timing that matters runs on a simulated clock. Test names carry the security review
finding they prove (F1 ... F14).
"""
import hmac, importlib.util, json, os, re, socket, subprocess, sys, tempfile, threading, time, unittest, wave
from unittest import mock

from test_bridge import bridge, FakeFocus, FakeSounds, NOON, Clock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
INSTALL_PS1 = os.path.join(TOOLS, "windows", "install.ps1")
ON_WINDOWS = sys.platform == "win32"
TOKEN = "0123456789abcdef0123456789abcdef"
TOKEN2 = "fedcba9876543210fedcba9876543210"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lump_relay = load("lump_relay", os.path.join(TOOLS, "lump_relay.py"))
control = load("lump_control_relay", os.path.join(TOOLS, "windows", "control.py"))


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return bool(cond())


def recv_all(sock, timeout=3.0):
    """Everything the peer sends until it closes (or timeout)."""
    sock.settimeout(timeout)
    out = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                return out, True
            out += chunk
    except socket.timeout:
        return out, False
    except OSError:
        return out, True


class FakeLink:
    """A USB serial port or Wi-Fi link to the board, for the Device."""
    def __init__(self, name):
        self.name, self.written, self.closed, self.incoming = name, [], False, []

    def read(self, n=256):
        if self.closed:
            raise OSError("closed")
        return self.incoming.pop(0) if self.incoming else b""

    def write(self, b): self.written.append(b)
    def flush(self): pass
    def close(self): self.closed = True


class ConfigMixin:
    def use_config(self, cfg):
        self.cfg_path = os.path.join(tempfile.mkdtemp(prefix="deskbuddy-relay-"), "config.json")
        self.write_config(cfg)
        p = mock.patch.object(bridge, "CONFIG_FILE", self.cfg_path)
        p.start(); self.addCleanup(p.stop)

    def write_config(self, cfg):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)


# ======================================================================================
# Bridge: lines from a relay (one RelayLink fed directly; no network, simulated time)
# ======================================================================================
class RelayLinkTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(NOON)
        p = mock.patch.object(bridge.time, "time", self.clock)
        p.start(); self.addCleanup(p.stop)
        self.boards = []
        self.lst = bridge.RelayListener(TOKEN, on_board=lambda l, prev, new: self.boards.append((prev, new)), start=False)
        self.a, self.b = socket.socketpair()
        self.addCleanup(self.b.close)
        self.link = self.lst.adopt(self.a, reader=False)
        self.addCleanup(self.link.unpair, "test over")

    def feed(self, data, at=None):
        self.link.feed(data, self.clock() if at is None else at)

    def queued(self):
        out = []
        while not self.link.q.empty():
            out.append(self.link.q.get_nowait())
        return out

    def test_status_lines_are_consumed_and_only_a_change_is_reported(self):
        self.link.active = True
        self.feed(b"relay board 1\nrelay board 1\nrelay board 0\nrelay board 1\n")
        self.assertEqual(self.queued(), [])                       # never handed on as board lines
        self.assertEqual(self.boards, [(None, 1), (1, 0), (0, 1)])
        self.assertEqual(self.link.board, 1)

    def test_F6_only_exact_lines_are_accepted(self):
        self.link.active = True
        good = [b"tap", b"gag tap_shock_idle", b"resync", b"desk-buddy lump ready: 10 moods, 40 sequences, 900 frames"]
        bad = [b"tapx", b"xtap", b"gag tap_shock_idle extra", b"gag  tap_shock_idle", b"gag tap_nope_idle",
               b"gag tap_shock_happy", b"gag tap_/../../x_idle", b"gag tap_..\\..\\x_idle", b"lump ready",
               b"xdesk-buddy lump ready: 1", b"err pairing", b"ok paired", b"k " + TOKEN.encode(), b"relay board 2"]
        for i, line in enumerate(bad + good):
            self.feed(line + b"\n", at=NOON + 3 * i)              # spaced out: no rate limit in play
        self.assertEqual(self.queued(), [g + b"\n" for g in good])

    def test_F6_tap_gag_resync_from_a_relay_are_rate_limited(self):
        self.link.active = True
        for kind in (b"tap", b"gag tap_moon_idle", b"resync"):
            self.feed(kind + b"\n", at=NOON)
            self.feed(kind + b"\n", at=NOON + 1)                 # too soon
            self.feed(kind + b"\n", at=NOON + 2.5)
        got = self.queued()
        self.assertEqual(got.count(b"tap\n"), 2)
        self.assertEqual(got.count(b"gag tap_moon_idle\n"), 2)
        self.assertEqual(got.count(b"resync\n"), 2)

    def test_inactive_relay_board_lines_are_discarded(self):
        self.feed(b"relay board 1\ntap\nresync\n")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.link.board, 1)

    def test_F3_overlong_line_is_discarded_to_its_newline_and_the_link_survives(self):
        self.link.active = True
        self.feed(b"x" * 300)                                     # no newline yet
        self.assertLessEqual(len(self.link.buf), bridge.RELAY_LINE_MAX)
        self.feed(b"still the same long line tap\ntap\n")
        self.assertEqual(self.queued(), [b"tap\n"])
        self.assertFalse(self.link.closed.is_set())
        self.feed(b"tap " + b"y" * 300 + b"\n", at=NOON + 5)       # a whole overlong line in one go
        self.assertEqual(self.queued(), [])
        self.assertFalse(self.link.closed.is_set())

    def test_F3_memory_stays_bounded(self):
        self.link.active = True
        for i in range(400):                                      # 1.6 MB with no newline, a chunk at a time
            self.feed(b"z" * 4096, at=NOON + i)
            self.assertLessEqual(len(self.link.buf), bridge.RELAY_LINE_MAX + 4096)
        self.assertFalse(self.link.closed.is_set())

    def test_F3_a_flood_beyond_the_rate_limit_unpairs_the_relay(self):
        self.link.active = True
        self.feed(b"relay board 1\n" * 1000)                      # all at the same moment
        self.assertTrue(self.link.closed.is_set())
        self.assertIsNone(self.lst.current)
        self.assertLessEqual(self.link.q.qsize(), bridge.RelayLink.QUEUE)
        self.assertLessEqual(len(self.link.drops), bridge.RELAY_FLOOD)

    def test_normal_traffic_is_well_under_the_rate_limit(self):
        self.link.active = True
        for i in range(600):                                      # ten lines a second for a minute
            self.feed(b"ok colour FF7A45\n", at=NOON + i / 10)
        self.assertFalse(self.link.closed.is_set())

    def test_F4_an_inactive_relay_that_keeps_heartbeating_is_not_unpaired(self):
        for t in range(0, 50, 10):
            self.feed(b"relay board 0\n", at=NOON + t)
            self.link.check(NOON + t + 5)
        self.link.check(NOON + 40 + 29)
        self.assertFalse(self.link.closed.is_set())

    def test_F4_a_silent_relay_is_unpaired_after_30_s(self):
        self.feed(b"relay board 1\n", at=NOON)
        self.link.check(NOON + 29)
        self.assertFalse(self.link.closed.is_set())
        self.link.check(NOON + 31)
        self.assertTrue(self.link.closed.is_set())
        self.assertIsNone(self.lst.current)

    def test_relay_lines_are_logged_truncated(self):
        self.link.active = True
        with self.assertLogs("bridge", "INFO") as logs:
            self.feed(b"ok " + b"q" * 200 + b"\n")
        self.assertTrue(all(len(line) < 140 for line in logs.output), logs.output)


# ======================================================================================
# Bridge: Device.send refuses w / CR / LF / NUL on every link (F1), gag names on every link (F6)
# ======================================================================================
F1_BYPASSES = [b"s idle\rw evil\tpw\ttok", b" w x", b"\tw x", b"wSSID\tpass\ttoken", b"w", b"W", b"X",
               b"s " + b"a" * 300, b"s idle\x00w x", b"\x0bw x"]


class DeviceSendRefuses(unittest.TestCase):
    def device(self):
        d = bridge.Device(lambda: None, start=False)
        d.ser, d.link = FakeLink("usb"), "usb"
        return d

    def test_F1_w_cr_lf_nul_never_reach_any_link(self):
        for link in ("usb", "wifi", "relay"):
            d = self.device(); d.link = link
            for cmd in ("w x", " w x", "\tw x", "\x0bw x", "wSSID\tpass\ttoken", "w", "s idle\rw evil", "s idle\nw evil",
                        "s idle\x00", "e tap\r\n"):
                with self.subTest(link=link, cmd=cmd):
                    self.assertFalse(d.send(cmd))
            self.assertEqual(d.ser.written, [])
            self.assertTrue(d.send("s idle"))
            self.assertEqual(d.ser.written, [b"s idle\n"])

    def test_F1_refused_wifi_details_never_reach_the_log(self):
        d = self.device()
        with self.assertLogs("bridge", "INFO") as logs:
            d.send("wMyNetwork\tsecret-password\ttoken123")
        self.assertNotIn("secret-password", "\n".join(logs.output))

    def test_F1_the_cmd_endpoint_cant_send_a_second_line(self):
        d = self.device()
        b = bridge.Buddy(dev=d, focus=FakeFocus(), sounds=FakeSounds(), start=False)
        self.assertFalse(b.dev.send("s idle\nw evil".strip()))
        self.assertEqual(d.ser.written, [])

    def test_F1_W_X_and_overlong_are_left_to_the_relays_allowlist(self):
        # The bridge itself only blocks w and line breaks; the relay blocks everything outside its allowlist.
        d = self.device()
        for cmd in ("W", "X", "s " + "a" * 300):
            self.assertTrue(d.send(cmd))
            self.assertIsNone(lump_relay.board_command(cmd.encode()))


class GagNames(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="deskbuddy-gags-")
        for g in ("shock", "x"):
            with wave.open(os.path.join(self.dir, f"tap_{g}.wav"), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100); w.writeframes(b"\0\0" * 100)
        cfg = os.path.join(self.dir, "sounds.json")
        with open(cfg, "w", encoding="utf-8") as f: json.dump({"enabled": True}, f)
        self.timers = []
        for p in (mock.patch.object(bridge, "SOUNDS_DIR", self.dir), mock.patch.object(bridge, "SOUNDS_FILE", cfg),
                  mock.patch.object(bridge.threading, "Timer", side_effect=lambda d, fn, args: self.timers.append(args) or mock.Mock())):
            p.start(); self.addCleanup(p.stop)

    def test_F6_unknown_or_traversal_gag_is_rejected_on_every_link(self):
        s = bridge.Sounds()
        for event in ("tap_/../../x_idle", "tap_..\\..\\x_idle", "tap_x_idle", "tap_shock_happy", "tap_shock", "x"):
            with self.subTest(event=event):
                self.assertFalse(s.play_gag(event, force=True))         # /sound play ..., forced
                self.assertFalse(s.play_gag(event))                     # the board's gag line (USB, Wi-Fi)
        self.assertEqual(self.timers, [])
        b = bridge.Buddy(dev=bridge.Device(lambda: None, start=False), focus=FakeFocus(), sounds=s, start=False)
        with mock.patch.object(bridge.time, "time", return_value=NOON):
            b.board_line("gag tap_/../../x_idle")
        self.assertEqual(self.timers, [])
        self.assertIsNone(bridge.relay_line_kind("gag tap_/../../x_idle"))    # and from a relay
        self.assertTrue(s.play_gag("tap_shock_idle"))                  # a real one still plays
        self.assertEqual(len(self.timers), 1)

    def test_every_gag_the_board_can_print_is_accepted(self):
        for g in bridge.GAGS + ("startle", "prep", "plane"):
            for m in bridge.BOARD_MOODS:
                self.assertTrue(bridge.gag_event_ok(f"tap_{g}_{m}"))


# ======================================================================================
# Bridge: the Device's choice of link (fakes for USB and Wi-Fi; the relay on a socketpair)
# ======================================================================================
class LinkSelection(ConfigMixin, unittest.TestCase):
    def setUp(self):
        self.clock = Clock(NOON)
        p = mock.patch.object(bridge.time, "time", self.clock)
        p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(bridge.RelayAdapter, "READ_TIMEOUT", 0.01)
        p.start(); self.addCleanup(p.stop)
        self.use_config({"lump_token": "abcdef123456", "relay_token": TOKEN})
        self.ready, self.lines, self.usb, self.usb_error = [], [], [], None
        self.d = d = bridge.Device(lambda: self.ready.append(d.link), on_line=self.lines.append, start=False)
        d.SETTLE, d.IDLE_WAIT, d.RELAY_PORT = 0, 0.01, 0
        d.usb_ports = lambda: list(self.usb)
        d._open_usb = self.open_usb
        d._open_net = lambda p: FakeLink("wifi")
        d.relay = bridge.RelayListener(TOKEN, on_board=d._relay_board, on_change=d._wake.set, start=False)
        self.addCleanup(lambda: d.relay and d.relay.stop("test over"))
        self.a, self.b = socket.socketpair()
        self.addCleanup(self.b.close)
        self.link = d.relay.adopt(self.a, reader=False)

    def open_usb(self, p):
        if self.usb_error:
            raise self.usb_error
        return FakeLink("usb")

    def board(self, v):
        self.link.feed(b"relay board %d\n" % v, self.clock())

    def step(self, n=1):
        for _ in range(n):
            self.d._step()

    def socket_open(self):
        return not self.link.closed.is_set() and self.a.fileno() != -1 and self.d.relay.current is self.link

    def test_link_order_usb_then_relay_then_wifi(self):
        self.board(1)
        self.usb = ["COM9"]
        self.assertEqual(self.d._targets(), ["COM9", "relay", "socket://lump.local:4210"])
        self.board(0)
        self.assertEqual(self.d._targets(), ["COM9", "socket://lump.local:4210"])

    def test_paired_relay_with_board_0_doesnt_preempt_wifi(self):
        self.board(0)
        self.step(3)
        self.assertEqual(self.d.link, "wifi")
        self.assertEqual(self.ready, ["wifi"])

    def test_relay_board_1_takes_over_from_wifi_and_board_0_falls_back_with_the_socket_open(self):
        self.step()
        self.assertEqual(self.d.link, "wifi")
        self.board(1)
        self.step()                                               # lets go of Wi-Fi
        self.step()                                               # opens the relay
        self.assertEqual(self.d.link, "relay")
        self.assertTrue(self.link.active)
        self.board(0)
        self.step()                                               # notices at once
        self.assertIsNone(self.d.link)
        self.step()
        self.assertEqual(self.d.link, "wifi")
        self.assertTrue(self.socket_open())                       # still paired, still heartbeating
        self.assertFalse(self.link.active)
        self.board(1)
        self.step(2)
        self.assertEqual(self.d.link, "relay")                    # and takes over again
        self.assertEqual(self.ready, ["wifi", "relay", "wifi", "relay"])

    def test_board_lines_from_the_active_relay_reach_the_bridge(self):
        self.board(1)
        self.step()
        self.link.feed(b"tap\ngag tap_disco_idle\n", self.clock())
        self.step(3)
        self.assertEqual(self.lines, ["tap", "gag tap_disco_idle"])

    def test_usb_on_this_pc_takes_over_from_the_relay_with_the_socket_open(self):
        self.board(1)
        self.step()
        self.assertEqual(self.d.link, "relay")
        self.usb = ["COM9"]
        self.clock.advance(self.d.USB_CHECK + 1)
        self.step()
        self.assertEqual(self.d.link, "usb")
        self.assertTrue(self.socket_open())
        self.assertFalse(self.link.active)

    def test_usb_takeover_only_when_the_port_really_opens(self):
        """Windows lists the CH343 even while another program holds it: keep the relay, say so once."""
        self.board(1)
        self.step()
        self.usb, self.usb_error = ["COM9"], bridge.serial.SerialException("could not open port 'COM9': PermissionError(13, 'Access is denied.')")
        with self.assertLogs("bridge", "INFO") as logs:
            for _ in range(5):
                self.clock.advance(self.d.USB_CHECK + 1)
                self.link.feed(b"relay board 1\n", self.clock())
                self.step()
                self.assertEqual(self.d.link, "relay")
        self.assertEqual(sum("can't open COM9" in line for line in logs.output), 1)
        self.assertFalse(any("switching" in line for line in logs.output))
        self.usb_error = None
        self.clock.advance(self.d.USB_CHECK + 1)
        self.link.feed(b"relay board 1\n", self.clock())
        self.step()
        self.assertEqual(self.d.link, "usb")

    def test_usb_takeover_from_wifi_also_waits_for_the_port_to_open(self):
        self.step()
        self.assertEqual(self.d.link, "wifi")
        self.usb, self.usb_error = ["COM9"], OSError("busy")
        for _ in range(3):
            self.clock.advance(self.d.USB_CHECK + 1)
            self.step()
        self.assertEqual(self.d.link, "wifi")

    def test_F4_a_silent_relay_is_unpaired_and_the_link_falls_back_to_wifi(self):
        self.board(1)
        self.step()
        self.assertEqual(self.d.link, "relay")
        self.clock.advance(bridge.RELAY_SILENCE + 1)
        self.link.check(self.clock())
        self.assertTrue(self.link.closed.is_set())
        self.step(2)
        self.assertEqual(self.d.link, "wifi")

    def test_resend_only_on_change_one_resend_over_a_minute_of_heartbeats(self):
        self.write_config({"relay_token": TOKEN})                 # no Wi-Fi: the relay is the first link
        b = bridge.Buddy(dev=self.d, focus=FakeFocus(), sounds=FakeSounds(), start=False)
        resends = []
        self.d.on_ready = lambda: (resends.append(self.clock()), b.resend())
        self.board(1)
        self.step()
        self.assertEqual(self.d.link, "relay")
        for _ in range(8):                                        # 80 s of "relay board 1" every 10 s
            self.clock.advance(10)
            self.board(1)
            self.step()
            self.link.check(self.clock())
        self.assertEqual(len(resends), 1)
        self.assertEqual(b.sounds.played.count("waking_up"), 1)
        self.assertTrue(self.socket_open())

    def test_resend_on_a_0_to_1_change_while_active(self):
        self.board(1)
        self.step()
        self.assertEqual(self.ready, ["relay"])
        self.board(0); self.board(1)                              # before the loop noticed the 0
        self.assertEqual(self.ready, ["relay", "relay"])

    def test_resend_on_the_firmware_boot_line_via_the_active_relay(self):
        self.board(1)
        self.step()
        self.link.feed(b"desk-buddy lump ready: 10 moods, 40 sequences, 900 frames\n", self.clock())
        self.step()
        self.assertEqual(self.ready, ["relay", "relay"])

    def test_relay_status_in_bridge_status(self):
        b = bridge.Buddy(dev=self.d, focus=FakeFocus(), sounds=FakeSounds(), start=False)
        self.board(1)
        self.step()
        st = b.status()
        self.assertEqual(st["link"], "relay")
        self.assertTrue(st["relay_board"])
        self.assertEqual({k: st["relay"][k] for k in ("paired", "board", "active", "takeovers")},
                         {"paired": True, "board": True, "active": True, "takeovers": 0})


class StalledRelay(ConfigMixin, unittest.TestCase):
    def test_F4_a_stalled_relay_write_releases_the_device_lock_within_2_s_and_unpairs(self):
        self.use_config({"relay_token": TOKEN})
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        peer = socket.socket()
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)   # the "relay": connected, never reads
        peer.connect(srv.getsockname())
        conn, _ = srv.accept(); srv.close()
        self.addCleanup(peer.close)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        d = bridge.Device(lambda: None, start=False)
        d.relay = bridge.RelayListener(TOKEN, start=False)
        link = d.relay.adopt(conn, reader=False)
        link.feed(b"relay board 1\n", time.time())
        d.ser, d.link = bridge.RelayAdapter(link), "relay"
        b = bridge.Buddy(dev=d, focus=FakeFocus(), sounds=FakeSounds(), start=False)
        t0 = time.monotonic()
        writer = threading.Thread(target=d.send, args=("c " + "A" * (32 * 1024 * 1024),), daemon=True)
        writer.start()
        self.assertTrue(wait_until(lambda: d.lock.locked(), 2))
        ticker = threading.Thread(target=b.tick, args=(time.time(),), daemon=True)
        ticker.start()                                            # wants the lock for its heartbeat
        writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertLess(time.monotonic() - t0, bridge.RELAY_SEND_TIMEOUT + 1.5)
        ticker.join(3)
        self.assertFalse(ticker.is_alive())                       # tick carried on
        self.assertTrue(link.closed.is_set())                     # and the relay was dropped
        self.assertTrue(d.lock.acquire(timeout=0)); d.lock.release()


# ======================================================================================
# Bridge: the listener itself (real sockets on an ephemeral 127.0.0.1 port)
# ======================================================================================
class Listener(unittest.TestCase):
    def setUp(self):
        self.lst = bridge.RelayListener(TOKEN, port=0)
        self.lst.FAIL_DELAY, self.lst.FAIL_DELAY_MAX = 0.05, 0.1
        self.addCleanup(self.lst.stop, "test over")

    def client(self):
        c = socket.create_connection(("127.0.0.1", self.lst.port), timeout=3)
        self.addCleanup(c.close)
        return c

    def pair(self, token=TOKEN):
        c = self.client()
        c.sendall(b"k " + token.encode() + b"\n")
        c.settimeout(3)
        self.assertEqual(c.recv(64), b"ok paired\n")
        return c

    def test_F8_bound_to_127_0_0_1_only(self):
        self.assertEqual(self.lst.srv.getsockname()[0], "127.0.0.1")

    def test_F14_no_reuseaddr_and_exclusive_on_windows(self):
        self.assertEqual(self.lst.srv.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)
        if ON_WINDOWS:
            with mock.patch.object(bridge, "IS_WIN", True):
                s = bridge.RelayListener.bind(0)
            self.addCleanup(s.close)
            self.assertEqual(s.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE), 1)
            self.assertEqual(s.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR), 0)
            self.assertEqual(s.getsockname()[0], "127.0.0.1")

    def test_pairing_accepted(self):
        c = self.pair()
        self.assertTrue(wait_until(lambda: self.lst.current is not None))
        c.sendall(b"relay board 1\n")
        self.assertTrue(wait_until(lambda: self.lst.current.board == 1))
        self.assertIs(self.lst.eligible(), self.lst.current)

    def test_F10_wrong_or_non_ascii_token_gets_err_pairing_and_a_clean_close(self):
        for line in (b"k " + TOKEN2.encode(), b"k " + TOKEN.upper().encode() + b"x", b"k \xff\xfe\xc3\xa9" + b"0" * 28,
                     TOKEN.encode(), b"hello"):
            with self.subTest(line=line):
                c = self.client()
                c.sendall(line + b"\n")
                out, closed = recv_all(c)
                self.assertEqual(out, b"err pairing\n")
                self.assertTrue(closed)
        self.assertIsNone(self.lst.current)

    def test_F8_oversized_preauth_line_is_refused(self):
        c = self.client()
        c.sendall(b"k " + b"0" * 300)                             # no newline within 256 bytes
        out, closed = recv_all(c)
        self.assertEqual(out, b"err pairing\n")
        self.assertTrue(closed)

    def test_F10_token_compared_in_constant_time_on_bytes(self):
        calls = []
        real = hmac.compare_digest
        def spy(a, b):
            calls.append((type(a), type(b))); return real(a, b)
        with mock.patch.object(bridge.hmac, "compare_digest", side_effect=spy):
            self.pair()
            c = self.client(); c.sendall(b"k " + TOKEN2.encode() + b"\n"); recv_all(c)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all(t == (bytes, bytes) for t in calls), calls)

    def test_F10_preauth_line_and_token_never_in_the_log(self):
        with self.assertLogs("bridge", "INFO") as logs:
            self.pair()
            c = self.client(); c.sendall(b"k " + TOKEN2.encode() + b"\n"); recv_all(c)
            c = self.client(); c.sendall(b"secret-guess-line\n"); recv_all(c)
        text = "\n".join(logs.output)
        for secret in (TOKEN, TOKEN2, "secret-guess-line"):
            self.assertNotIn(secret, text)

    def test_F8_fifth_concurrent_unpaired_connection_is_refused(self):
        waiting = [self.client() for _ in range(4)]
        self.assertTrue(wait_until(lambda: self.lst.unpaired == 4))
        fifth = self.client()
        out, closed = recv_all(fifth, timeout=2)
        self.assertEqual(out, b"")
        self.assertTrue(closed)
        self.assertEqual(self.lst.unpaired, 4)
        waiting[0].sendall(b"k " + TOKEN.encode() + b"\n")        # the waiting ones still get their turn
        waiting[0].settimeout(3)
        self.assertEqual(waiting[0].recv(64), b"ok paired\n")

    def test_F8_pairing_deadline_is_absolute_even_when_bytes_trickle_in(self):
        clock = Clock(NOON)
        with mock.patch.object(bridge.time, "time", clock):
            c = self.client()
            self.assertTrue(wait_until(lambda: self.lst.unpaired == 1))
            time.sleep(0.2)                                       # the pairing thread is waiting (deadline NOON + 5)
            for piece in (b"k", b" 01", b"23"):
                c.sendall(piece)
                time.sleep(0.1)
                c.settimeout(0.05)
                with self.assertRaises(socket.timeout):           # still waiting: nothing sent back yet
                    c.recv(64)
                clock.advance(2)                                  # NOON + 2, + 4, + 6
            out, closed = recv_all(c)
        self.assertEqual(out, b"err pairing\n")
        self.assertTrue(closed)

    def test_F9_an_unpaired_connection_never_displaces_a_paired_relay(self):
        a = self.pair()
        self.assertTrue(wait_until(lambda: self.lst.current is not None))
        first = self.lst.current
        idle = self.client()                                      # connects, says nothing
        wrong = self.client(); wrong.sendall(b"k " + TOKEN2.encode() + b"\n"); recv_all(wrong)
        self.assertIs(self.lst.current, first)
        a.sendall(b"relay board 1\n")
        self.assertTrue(wait_until(lambda: first.board == 1))
        self.assertEqual(self.lst.takeovers, 0)

    def test_F9_a_newer_paired_relay_takes_over_and_is_counted_in_status(self):
        a = self.pair()
        self.assertTrue(wait_until(lambda: self.lst.current is not None))
        first = self.lst.current
        self.pair()
        self.assertTrue(wait_until(lambda: self.lst.current is not first and self.lst.current is not None))
        out, closed = recv_all(a)
        self.assertTrue(closed)                                   # the old relay was dropped
        d = bridge.Device(lambda: None, start=False)
        d.relay = self.lst
        b = bridge.Buddy(dev=d, focus=FakeFocus(), sounds=FakeSounds(), start=False)
        self.assertEqual(b.status()["relay"]["takeovers"], 1)

    def test_newest_relay_wins(self):
        self.pair()
        self.assertTrue(wait_until(lambda: self.lst.current is not None))
        c2 = self.pair()
        self.assertTrue(wait_until(lambda: self.lst.takeovers == 1))
        c2.sendall(b"relay board 1\n")
        self.assertTrue(wait_until(lambda: self.lst.eligible() is not None))


class ListenerFromConfig(ConfigMixin, unittest.TestCase):
    def device(self):
        d = bridge.Device(lambda: None, start=False)
        d.RELAY_PORT = 0
        self.addCleanup(lambda: d.relay and d.relay.stop("test over"))
        return d

    def test_no_listener_without_relay_token(self):
        self.use_config({"lump_token": "abcdef123456"})
        with mock.patch.object(bridge, "RelayListener") as rl:
            d = self.device(); d._relay_config()
        rl.assert_not_called()
        self.assertIsNone(d.relay)
        self.assertIsNone(d.relay_status())

    def test_F10_malformed_token_means_no_listener(self):
        for bad in ("zz-not-hex-zz", TOKEN[:-1], TOKEN + "0", TOKEN[:-1] + "g"):
            with self.subTest(bad=bad):
                self.use_config({"relay_token": bad})
                with mock.patch.object(bridge, "RelayListener") as rl, self.assertLogs("bridge", "INFO") as logs:
                    d = self.device(); d._relay_config()
                rl.assert_not_called()
                self.assertIsNone(d.relay)
                self.assertNotIn(bad, "\n".join(logs.output))

    def test_F9_new_token_closes_the_current_relay_and_no_token_stops_listening(self):
        self.use_config({"relay_token": TOKEN})
        d = self.device(); d._relay_config()
        lst = d.relay
        self.assertIsNotNone(lst)
        self.assertEqual(lst.srv.getsockname()[0], "127.0.0.1")
        c = socket.create_connection(("127.0.0.1", lst.port), timeout=3); self.addCleanup(c.close)
        c.sendall(b"k " + TOKEN.encode() + b"\n"); c.settimeout(3)
        self.assertEqual(c.recv(64), b"ok paired\n")
        self.assertTrue(wait_until(lambda: lst.current is not None))
        d._relay_config()                                         # same token: nothing changes
        self.assertIs(d.relay, lst)
        self.write_config({"relay_token": TOKEN2})               # Lump Control's "New token"
        d._relay_config()
        out, closed = recv_all(c)
        self.assertTrue(closed)
        self.assertTrue(lst.stopped)
        self.assertIsNot(d.relay, lst)
        self.assertTrue(d.relay.has_token(TOKEN2))
        old = d.relay
        self.write_config({})                                    # install.ps1 -AllowRelay -Off
        d._relay_config()
        self.assertIsNone(d.relay)
        self.assertTrue(old.stopped)

    def test_a_relay_paired_while_on_wifi_takes_over_once_it_reports_board_1(self):
        self.use_config({"lump_token": "abcdef123456", "relay_token": TOKEN})
        ready = []
        d = self.device()
        d.on_ready = lambda: ready.append(d.link)
        d.SETTLE, d.IDLE_WAIT = 0, 0.01
        d.usb_ports = lambda: []
        d._open_net = lambda p: FakeLink("wifi")
        with mock.patch.object(bridge.RelayAdapter, "READ_TIMEOUT", 0.01):
            d._step()
            self.assertEqual(d.link, "wifi")
            c = socket.create_connection(("127.0.0.1", d.relay.port), timeout=3); self.addCleanup(c.close)
            c.sendall(b"k " + TOKEN.encode() + b"\n")
            c.settimeout(3); self.assertEqual(c.recv(64), b"ok paired\n")
            c.sendall(b"relay board 1\n")                         # read by the relay's own reader thread
            self.assertTrue(wait_until(lambda: d._relay_link() is not None))
            d._step(); d._step()
            self.assertEqual(d.link, "relay")
            d.send("s working")
            c.settimeout(3)
            self.assertEqual(c.recv(64), b"s working\n")
        self.assertEqual(ready, ["wifi", "relay"])


# ======================================================================================
# The relay (tools/lump_relay.py)
# ======================================================================================
def through_relay(data):
    """What the relay passes to the board from these bridge bytes, and how many lines it refuses."""
    lines, rest, skipping = lump_relay.take_lines(data, False)
    passed, refused = [], 0
    for raw in lines:
        cmd = lump_relay.board_command(raw)
        if cmd is not None:
            passed.append(cmd)
        elif raw is None or raw.strip(lump_relay.SPACE):
            refused += 1
    return passed, refused, rest, skipping


class RelayFilter(unittest.TestCase):
    def test_F1_each_bypass_is_dropped(self):
        for raw in F1_BYPASSES:
            with self.subTest(raw=raw):
                passed, refused, _, _ = through_relay(raw + b"\n")
                self.assertGreaterEqual(refused, 1)
                self.assertFalse(any(p[:1] in (b"w", b"W", b"X") for p in passed), passed)
        passed, _, _, _ = through_relay(b"s idle\rw evil\tpw\ttok\n")
        self.assertEqual(passed, [b"s idle"])                     # the honest half still gets through

    def test_allowlist(self):
        for ok in (b"s idle", b"e tap", b"p 1", b"o 1 2 3", b"c FF7A45", b"h 43200", b"T 0.35", b"i", b"?", b"  s idle  "):
            self.assertIsNotNone(lump_relay.board_command(ok), ok)
        for bad in (b"w", b"W", b"X", b"k " + TOKEN.encode(), b"relay board 1", b"s idle\x7f", b"s \xc3\xa9", b"", b"   "):
            self.assertIsNone(lump_relay.board_command(bad), bad)

    def test_240_character_cap(self):
        self.assertIsNotNone(lump_relay.board_command(b"c" + b"F" * 239))
        self.assertIsNone(lump_relay.board_command(b"c" + b"F" * 240))
        passed, refused, rest, skipping = through_relay(b"s " + b"a" * 300)       # overlong, no newline yet
        self.assertEqual((passed, refused, rest, skipping), ([], 1, b"", True))
        lines, rest, skipping = lump_relay.take_lines(b"aaaa\ns idle\n", True)
        self.assertEqual(lines, [b"s idle"])                      # thrown away up to its newline

    def test_F2_bridge_address_must_be_on_the_tailnet(self):
        def resolver(ip, fam=socket.AF_INET):
            return lambda host, port, *a: [(fam, socket.SOCK_STREAM, 6, "", (ip, port) if fam == socket.AF_INET else (ip, port, 0, 0))]
        for ip in ("100.64.0.1", "100.101.102.103", "100.127.255.254"):
            self.assertEqual(len(lump_relay.tailnet_addresses("pc", 4211, resolver(ip))), 1)
        self.assertEqual(len(lump_relay.tailnet_addresses("pc", 4211, resolver("fd7a:115c:a1e0::1234", socket.AF_INET6))), 1)
        for ip, fam in (("192.168.1.5", socket.AF_INET), ("10.0.0.5", socket.AF_INET), ("100.128.0.1", socket.AF_INET),
                        ("127.0.0.1", socket.AF_INET), ("fd7a:115c:a1e1::1", socket.AF_INET6), ("::ffff:100.64.0.1", socket.AF_INET6)):
            with self.subTest(ip=ip):
                connect = mock.Mock()
                r = lump_relay.Relay(settings=lambda: {"bridge_host": "pc", "port": 4211, "token": TOKEN},
                                     resolve=resolver(ip, fam), connect=connect)
                with self.assertRaises(lump_relay.RelayError):
                    r.dial()
                connect.assert_not_called()                       # nothing was sent to it, token included

    def test_settings_are_checked_and_the_token_never_reaches_an_error(self):
        d = tempfile.mkdtemp(prefix="deskbuddy-relaycfg-")
        p = os.path.join(d, "relay.json")
        with self.assertRaises(lump_relay.RelayError):
            lump_relay.read_settings(p)
        for cfg in ({"bridge_host": "pc", "token": "nothex" * 6}, {"token": TOKEN}, {"bridge_host": "pc", "token": TOKEN, "port": 0}):
            with open(p, "w", encoding="utf-8") as f: json.dump(cfg, f)
            with self.assertRaises(lump_relay.RelayError) as e:
                lump_relay.read_settings(p)
            self.assertNotIn("nothex", str(e.exception))
        with open(p, "w", encoding="utf-8") as f: json.dump({"bridge_host": "pc.tail.ts.net", "token": TOKEN}, f)
        self.assertEqual(lump_relay.read_settings(p), {"bridge_host": "pc.tail.ts.net", "port": 4211, "token": TOKEN})

    def test_F12_only_the_ch343_is_opened_on_windows(self):
        import types
        ports = [types.SimpleNamespace(device="COM1", vid=0x0403, pid=0x6001), types.SimpleNamespace(device="COM3", vid=None, pid=None),
                 types.SimpleNamespace(device="COM7", vid=0x1A86, pid=0x55D3)]
        with mock.patch.object(lump_relay, "IS_WIN", True), mock.patch("serial.tools.list_ports.comports", return_value=ports):
            self.assertEqual(lump_relay.find_board(), "COM7")
        with mock.patch.object(lump_relay, "IS_WIN", True), mock.patch("serial.tools.list_ports.comports", return_value=ports[:2]):
            self.assertIsNone(lump_relay.find_board())

    def test_the_relay_never_listens(self):
        with open(os.path.join(TOOLS, "lump_relay.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIsNone(re.search(r"\.(listen|bind|accept)\(", src))


class FakeBoard:
    """The board on the relay's USB port."""
    def __init__(self):
        self.got, self.lines, self.unplugged, self.lock = [], [], False, threading.Lock()

    def read(self, n=256):
        if self.unplugged:
            raise OSError("device unplugged")
        with self.lock:
            if self.lines:
                return self.lines.pop(0)
        time.sleep(0.01)
        return b""

    def write(self, b):
        if self.unplugged:
            raise OSError("device unplugged")
        self.got.append(b)

    def close(self): pass


class FakeBridge:
    """Stands in for the bridge's relay port: pairs with the token and records each connection's lines."""
    def __init__(self, reply=b"ok paired\n"):
        self.srv = socket.socket(); self.srv.bind(("127.0.0.1", 0)); self.srv.listen(4)
        self.port, self.reply, self.conns, self.lines = self.srv.getsockname()[1], reply, [], []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try: c, _ = self.srv.accept()
            except OSError: return
            self.conns.append(c)
            threading.Thread(target=self._read, args=(c, len(self.conns) - 1), daemon=True).start()

    def _read(self, c, i):
        buf, paired = b"", False
        while True:
            try: data = c.recv(4096)
            except OSError: return
            if not data: return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not paired:
                    paired = line == b"k " + TOKEN.encode()
                    try: c.sendall(self.reply if paired else b"err pairing\n")
                    except OSError: return
                    continue
                self.lines.append((i, line))

    def got(self, line, conn=None):
        return [x for x in self.lines if x[1] == line and (conn is None or x[0] == conn)]

    def close(self):
        self.srv.close()
        for c in self.conns:
            try: c.close()
            except OSError: pass


class RelayEndToEnd(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.addCleanup(self.bridge.close)
        self.board = FakeBoard()
        self.plugged = True
        self.opened = []
        tailnet = lambda host, port, *a: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.100.1.1", port))]
        self.r = lump_relay.Relay(settings=lambda: {"bridge_host": "pc.tail.ts.net", "port": 4211, "token": TOKEN},
                                  find=lambda: "COM7" if self.plugged else None, open_=self.open_board, resolve=tailnet,
                                  connect=lambda fam, sa: socket.create_connection(("127.0.0.1", self.bridge.port), timeout=3))
        self.r.BACKOFF, self.r.HEARTBEAT, self.r.BOARD_POLL = (0.05, 0.2), 0.2, 0.05
        self.addCleanup(self.r.stop)

    def open_board(self, port):
        self.opened.append(port)
        self.board = FakeBoard()
        return self.board

    def start(self):
        threading.Thread(target=self.r.run, daemon=True).start()
        self.assertTrue(wait_until(lambda: self.bridge.got(b"relay board 1")))

    def send(self, data, conn=-1):
        self.bridge.conns[conn].sendall(data)

    def test_pairs_and_heartbeats_relay_board_1(self):
        self.start()
        self.assertTrue(wait_until(lambda: len(self.bridge.got(b"relay board 1")) >= 3))

    def test_w_is_dropped_and_answered_and_allowed_lines_pass(self):
        self.start()
        self.send(b"w MyWifi\tpassword\t" + TOKEN.encode() + b"\ns idle\nc " + b"F" * 300 + b"\n")
        self.assertTrue(wait_until(lambda: b"s idle\n" in self.board.got))
        self.assertTrue(wait_until(lambda: len(self.bridge.got(b"err relay refused")) == 2))
        self.assertEqual(self.board.got, [b"s idle\n"])

    def test_refusals_come_back_as_err_relay_refused(self):
        # A raw bridge socket of our own, to read the relay's answers.
        a, b = socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
        self.r.board, self.r.HEARTBEAT = self.board, 60
        t = threading.Thread(target=self.r.serve, args=(b,), daemon=True); t.start()
        a.settimeout(3)
        self.assertEqual(a.recv(64), b"relay board 1\n")
        a.sendall(b"w x\n s idle \nX\n")
        got = b""
        while got.count(b"\n") < 2:
            got += a.recv(64)
        self.assertEqual(got, b"err relay refused\nerr relay refused\n")
        self.assertTrue(wait_until(lambda: self.board.got == [b"s idle\n"]))
        self.r.stop(); t.join(2)                                  # before the sockets are closed under it
        self.assertFalse(t.is_alive())

    def test_board_lines_go_to_the_bridge(self):
        self.start()
        with self.board.lock:
            self.board.lines.append(b"tap\r\ngag tap_disco_idle\r\n")
        self.assertTrue(wait_until(lambda: self.bridge.got(b"tap") and self.bridge.got(b"gag tap_disco_idle")))

    def test_reconnects_after_losing_the_bridge(self):
        self.start()
        self.bridge.conns[0].close()
        self.assertTrue(wait_until(lambda: self.bridge.got(b"relay board 1", conn=1), 5))

    def test_reconnects_after_losing_the_board(self):
        self.start()
        self.plugged = False
        self.board.unplugged = True
        self.assertTrue(wait_until(lambda: self.bridge.got(b"relay board 0")))
        time.sleep(0.3)
        n = len(self.bridge.got(b"relay board 1"))
        self.plugged = True
        self.assertTrue(wait_until(lambda: len(self.bridge.got(b"relay board 1")) > n, 3))
        self.assertEqual(self.opened, ["COM7", "COM7"])

    def test_a_refused_token_is_logged_without_the_token(self):
        self.bridge.reply = b"err pairing\n"
        with self.assertLogs("relay", "INFO") as logs:
            threading.Thread(target=self.r.run, daemon=True).start()
            self.assertTrue(wait_until(lambda: any("refused our token" in l for l in logs.output)))
            self.r.stop()
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_the_token_never_reaches_the_relay_log(self):
        with self.assertLogs("relay", "INFO") as logs:
            self.start()
            self.send(b"w MyWifi\tpassword\t" + TOKEN.encode() + b"\n")
            self.assertTrue(wait_until(lambda: any("refused" in l for l in logs.output)))
        text = "\n".join(logs.output)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("password", text)


# ======================================================================================
# Setting it up: install.ps1 (F5, F7) and Lump Control
# ======================================================================================
class Installer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(INSTALL_PS1, "rb") as f:
            cls.raw = f.read()
        cls.src = cls.raw.decode("ascii")

    def param_names(self):
        m = re.search(r"^param\((.*?)^\)", self.src, re.M | re.S)
        self.assertIsNotNone(m, "no param() block")
        return re.findall(r"\$(\w+)", m.group(1))

    def test_F5_no_token_parameter(self):
        names = self.param_names()
        self.assertIn("TokenFile", names)
        self.assertIn("Relay", names)
        self.assertNotIn("token", [n.lower() for n in names])

    def test_F5_relay_reads_the_token_only_from_a_hidden_prompt_or_token_file(self):
        reads = re.findall(r"Read-Host[^\n]*", self.src)
        self.assertTrue(reads)
        self.assertTrue(all("-AsSecureString" in r for r in reads), reads)
        fn = re.search(r"function Read-RelayToken \{(.*?)^\}", self.src, re.M | re.S).group(1)
        self.assertIn("$TokenFile", fn)
        self.assertIn("Read-Host -AsSecureString", fn)
        relay = re.search(r"^if \(\$Relay\) \{(.*?)^\}", self.src, re.M | re.S).group(1)
        self.assertIn("Read-RelayToken", relay)
        self.assertNotIn("$args", self.src.lower().replace("$arguments", ""))

    def test_F5_printed_command_for_the_relay_has_no_token(self):
        self.assertIn('"--relay-command", $name', self.src)       # -AllowRelay prints through this
        home = tempfile.mkdtemp(prefix="deskbuddy-allowrelay-")
        with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"relay_token": TOKEN}, f)
        env = dict(os.environ, DESKBUDDY_HOME=home)
        out = subprocess.run([sys.executable, os.path.join(TOOLS, "windows", "control.py"), "--relay-command", "pc.tail.ts.net"],
                             env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn(TOKEN, out.stdout + out.stderr)
        self.assertEqual(out.stdout.strip(),
                         r"powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1 -Relay -BridgeHost pc.tail.ts.net")
        for flag in ("--ensure-relay-token", "--remove-relay-token"):
            out = subprocess.run([sys.executable, os.path.join(TOOLS, "windows", "control.py"), flag],
                                 env=env, capture_output=True, text=True, timeout=30)
            self.assertNotIn(TOKEN, out.stdout + out.stderr)

    def test_F7_serve_is_tailnet_tcp_4211_to_localhost_and_off_removes_only_that(self):
        self.assertIn('@("serve", "--bg", "--tcp", "$RelayPort", "tcp://127.0.0.1:$RelayPort")', self.src)
        self.assertIn("serve --tcp $RelayPort off", self.src)
        self.assertIn("$RelayPort = 4211", self.src)
        self.assertNotRegex(self.src, r"(?i)serve\s+reset|funnel\s+(on|--bg)|tailscale\s+funnel|proxy-protocol")

    def test_relay_refuses_when_the_bridge_is_installed_unless_forced_and_uninstall_covers_it(self):
        relay = re.search(r"^if \(\$Relay\) \{(.*?)^\}", self.src, re.M | re.S).group(1)
        self.assertRegex(relay, r"if \(\$bridgeHere -and -not \$Force\) \{\s*throw")
        self.assertIn('"pyserial")', relay)
        uninstall = re.search(r"^if \(\$Uninstall\) \{(.*?)^\}", self.src, re.M | re.S).group(1)
        for part in ("Stop-Relay", "$RelayShortcut", "$RelayJson"):
            self.assertIn(part, uninstall)

    def test_powershell_is_ascii_only(self):
        for name in ("install.ps1", "bridge.ps1", "flash.ps1"):
            with open(os.path.join(TOOLS, "windows", name), "rb") as f:
                self.assertTrue(all(b < 128 for b in f.read()), name)


class ControlRelay(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="deskbuddy-ctlrelay-"), "config.json")

    def test_tokens(self):
        control.update_json(self.path, {"slack_token": "xoxp-1"})
        self.assertTrue(control.new_relay_token(self.path))
        tok = control.read_json(self.path)["relay_token"]
        self.assertRegex(tok, r"^[0-9a-f]{32}$")
        self.assertFalse(control.new_relay_token(self.path))     # kept
        self.assertTrue(control.new_relay_token(self.path, replace=True))
        self.assertNotEqual(control.read_json(self.path)["relay_token"], tok)
        self.assertTrue(control.remove_relay_token(self.path))
        self.assertEqual(control.read_json(self.path), {"slack_token": "xoxp-1"})
        with mock.patch.object(bridge, "CONFIG_FILE", self.path):
            control.new_relay_token(self.path)
            self.assertTrue(bridge.RELAY_TOKEN_RE.fullmatch(bridge.read_config()["relay_token"]))

    def test_status_text(self):
        self.assertEqual(control.describe({"mood": "working", "device": True, "link": "relay"}), "working · board on the remote link")
        self.assertIsNone(control.describe_relay({"relay": None}))
        self.assertIn("waiting for the relay", control.describe_relay({"relay": {"paired": False}}))
        self.assertIn("board plugged in there (in use)", control.describe_relay({"relay": {"paired": True, "board": True, "active": True}}))
        self.assertIn("board not plugged in there", control.describe_relay({"relay": {"paired": True, "board": False}}))


if __name__ == "__main__":
    unittest.main()


class RelayAnimationLines(unittest.TestCase):
    """The firmware's "event <name>" lines come through a relay too, but only real animation names."""
    def test_event_lines_from_a_relay(self):
        for ok in ("event fidget_toss", "event oops", "event hello", "event tap_disco_working", "event tap_startle_night"):
            self.assertEqual(bridge.relay_line_kind(ok), "event", ok)
        for bad in ("event ../../x", "event tap_disco", "event fidget_toss_extra", "event " + "a" * 300, "events hello"):
            self.assertIsNone(bridge.relay_line_kind(bad), bad)
