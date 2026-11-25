"""Exercise discovery deadlines without serial access or real sleeps."""
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("esp32_checker", ROOT / "tools/check_esp32_32e.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Serial:
    def __init__(self, clock, ready):
        self.clock, self.ready = clock, ready
        self.writes, self.events = [], []
        self.timeout = 0.2
        self.opened, self._rts, self.resets = False, False, 0
        self.available_at = ready
        self.demo = False
        self.wifi_status = "off"

    def open(self):
        self.opened = True

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        pulse = self._rts and not value and self.opened
        self._rts = value
        if not pulse:
            return
        self.resets += 1
        self.demo = False
        self.available_at = self.clock.now + self.ready
        boot = ["rst:0x1 (POWERON_RESET)", "panel readback PASS", "MADCTL=00000020 rotation=0 240x320",
                "panel native mapping PASS", "panel full clear PASS",
                "scaled sprite readback PASS (16/16 pixels)", "psram=0",
                "desk-buddy lump ready: 10 moods, 177 sequences, 764 frames"]
        self.events.clear()
        self.events.extend((self.available_at, line) for line in boot)

    def close(self):
        assert not self.dtr and not self.rts
        self.opened = False

    def flush(self):
        pass

    def write(self, data):
        cmd = data.decode().strip()
        self.writes.append((self.clock.now, cmd))
        if self.clock.now < self.available_at:
            return
        if cmd == "s demo":
            self.demo = True
        replies = {"?": "ok mode=direct target=idle playing=idle(loop)", "s working": "ok target=working",
                   "s done": "ok target=done", "o 2 1 0": "ok overlay working=2 waiting=1 unseen=0",
                   "c 3366FF": "ok colour 3366FF", "i": "ok touch=xpt2046 threshold=600",
                   "T 650": "ok threshold=650 pressure", "T 600": "ok threshold=600 pressure",
                   "W": f"ok wifi={self.wifi_status}", "X": "tap", "s demo": "ok mode=demo",
                   "P": "ok panel invert=1 bgr=0 rotation=0 width=240 height=320",
                   "P 1 1 1": "ok panel invert=1 bgr=1 rotation=1 width=320 height=240",
                   "o 2 1 3": "ok overlay working=2 waiting=1 unseen=3",
                   "P 1 0 0": "ok panel invert=1 bgr=0 rotation=0 width=240 height=320"}
        self.events.append((self.clock.now, replies[cmd]))

    def readline(self):
        end = self.clock.now + self.timeout
        if self.events and self.events[0][0] <= end:
            at, line = self.events.pop(0)
            self.clock.now = max(self.clock.now, at)
            return (line + "\n").encode()
        self.clock.now = end
        return b""


def check(monkeypatch, ready):
    clock = Clock()
    ser = Serial(clock, ready)
    monkeypatch.setattr(checker.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(checker.time, "sleep", clock.sleep)
    monkeypatch.setattr(checker.serial, "Serial", lambda: ser)
    monkeypatch.setattr(sys, "argv", ["check", "--port", "/fake/esp32", "--seconds", "0"])
    return ser


def test_checker_sends_query_during_startup(monkeypatch):
    ser = check(monkeypatch, 1.8)
    checker.main()
    assert ser.port == "/fake/esp32"
    assert ser.writes[0][1] == "?"
    assert ser.writes[0][0] < 0.5
    assert ser.resets == 3
    assert not ser.demo
    # Final reset is after all writes, including the normal-mode diagnostic.
    assert ser.writes[-1][1] == "i"
    assert ser.writes[-1][0] < ser.available_at - ser.ready
    assert all(not cmd.startswith("P 0") for _, cmd in ser.writes)


@pytest.mark.parametrize("state", ["off", "joining", "connected"])
@pytest.mark.parametrize("explicit", [False, True])
def test_checker_accepts_existing_wifi_state_without_configuring_it(monkeypatch, state, explicit):
    ser = check(monkeypatch, 1.8)
    ser.wifi_status = state
    if explicit:
        monkeypatch.setattr(sys, "argv", [*sys.argv, "--wifi", state])
    checker.main()
    assert ser.resets == 3 and not ser.opened
    assert all(not cmd.startswith("w") for _, cmd in ser.writes)


def test_checker_rejects_an_unexpected_wifi_state(monkeypatch):
    ser = check(monkeypatch, 1.8)
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--wifi", "connected"])
    with pytest.raises(RuntimeError, match="'W' failed"):
        checker.main()
    assert ser.resets == 2 and not ser.opened


def test_checker_leaves_counts_in_final_session_without_reset_on_close(monkeypatch):
    ser = check(monkeypatch, 1.8)
    monkeypatch.setattr(sys, "argv", ["check", "--port", "/fake/esp32", "--seconds", "0", "--leave-counts", "2", "1", "3"])
    checker.main()
    assert ser.resets == 3 and not ser.demo and not ser.opened
    assert ser.writes[-1][1] == "o 2 1 3"
    assert ser.writes[-1][0] > ser.available_at
    assert ser.clock.now - ser.writes[-1][0] > 60


def test_checker_rejects_reboot_after_final_counts(monkeypatch):
    ser = check(monkeypatch, 1.8)
    monkeypatch.setattr(sys, "argv", ["check", "--port", "/fake/esp32", "--seconds", "0", "--leave-counts", "2", "1", "3"])
    write = ser.write

    def reboot_after_counts(data):
        write(data)
        if data == b"o 2 1 3\n":
            ser.events.append((ser.clock.now + 61, "rst:0x1 (POWERON_RESET)"))

    ser.write = reboot_after_counts
    with pytest.raises(RuntimeError, match="after final counter command"):
        checker.main()
    assert ser.resets == 4 and not ser.opened


@pytest.mark.parametrize("session,delay", [(2, 2.8), (3, 8.8)])
@pytest.mark.parametrize("marker", ["rst:0xc (SW_CPU_RESET)", "Guru Meditation Error", "Brownout detector was triggered",
                                  "Backtrace: simulated crash", "abort() was called"])
def test_checker_rejects_late_reset_or_crash_in_post_reset_capture(monkeypatch, capsys, session, delay, marker):
    ser = check(monkeypatch, 1.8)
    readline = ser.readline
    injected = False

    def late_failure():
        nonlocal injected
        if ser.resets == session and not injected:
            # One complete ready banner, then a failure near the capture end.
            ser.events.append((ser.available_at - ser.ready + delay, marker))
            injected = True
        return readline()

    ser.readline = late_failure
    with pytest.raises(RuntimeError, match="crash or reboot during boot"):
        checker.main()
    assert injected and ser.resets == session + 1 and not ser.opened
    assert "PASS: normal-mode ? verified" not in capsys.readouterr().out


def test_checker_requires_exactly_one_reset_per_boot(monkeypatch):
    ser = check(monkeypatch, 1.8)
    ser.open()
    ser.rts = True; ser.rts = False
    boot = [line for _, line in ser.events if not line.startswith("rst:")]
    with pytest.raises(RuntimeError, match="crash or reboot during boot"):
        checker.validate_boot(boot)


def test_checker_rejects_boot_framebuffer_allocation_failure():
    with pytest.raises(RuntimeError, match="boot reported an error"):
        checker.validate_boot(["err status framebuffer allocation failed"])


@pytest.mark.parametrize("missing", ["panel native mapping PASS", "panel full clear PASS"])
def test_checker_requires_native_mapping_and_clear_evidence(monkeypatch, missing):
    ser = check(monkeypatch, 1.8)
    ser.open()
    ser.rts = True; ser.rts = False
    boot = [line for _, line in ser.events]
    with pytest.raises(RuntimeError, match="boot check missing"):
        checker.validate_boot([line for line in boot if missing not in line])


@pytest.mark.parametrize("ready", [3.01, 4.0])
def test_checker_rejects_ok_after_three_seconds(monkeypatch, ready):
    ser = check(monkeypatch, ready)
    with pytest.raises(RuntimeError, match="3.*s"):
        checker.main()
    assert ser.resets == 2


def test_checker_resets_out_of_demo_even_if_soak_fails(monkeypatch):
    ser = check(monkeypatch, 1.0)
    write = ser.write

    def crash_during_demo(data):
        write(data)
        if data == b"s demo\n":
            ser.events.append((ser.clock.now, "Backtrace: simulated crash"))

    ser.write = crash_during_demo
    with pytest.raises(RuntimeError, match="crash or reboot during soak"):
        checker.main()
    assert ser.resets == 2
    assert not ser.demo
