"""Linux host integration, using temp homes, fake USB paths and fake service commands."""
from contextlib import contextmanager, nullcontext
import configparser
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from serial.tools import list_ports

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
import bridge_host as host
import ota
import wifi_setup as wifi
from serial_fakes import Clock as USBClock, MISSING_ART_BOOT, MISSING_ART_STATE, STATE, USB


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")


def systemctl_reply(cmd, *, load="loaded", active="active", sub="running", job="0", job_type="start"):
    """Match systemctl show's property/value formatting and is-active exit status."""
    if "show" in cmd:
        values = {"LoadState": load, "ActiveState": active, "SubState": sub, "Job": job, "JobType": job_type}
        fields = next(arg.split("=", 1)[1].split(",") for arg in cmd if arg.startswith("--property="))
        output = "\n".join(values[key] if "--value" in cmd else f"{key}={values[key]}" for key in fields)
        return subprocess.CompletedProcess(cmd, 0, output + "\n")
    return subprocess.CompletedProcess(cmd, 3 if "is-active" in cmd and active != "active" else 0, "")


@pytest.mark.parametrize("active,sub,job,job_type", [
    ("activating", "auto-restart", "0", "start"),
    ("activating", "auto-restart-queued", "0", "start"),
    ("inactive", "dead", "73", "start"),
    ("inactive", "dead", "73", "restart"),
])
def test_linux_pending_activation_is_stopped_and_resumed(linux, active, sub, job, job_type):
    def run(cmd, **kwargs):
        return systemctl_reply(cmd, active=active, sub=sub, job=job, job_type=job_type)

    with mock.patch.object(host.subprocess, "run", side_effect=run) as command:
        with host.paused_bridge():
            assert command.call_args.args[0][2] == "stop"
    assert [c.args[0][2] for c in command.call_args_list if c.args[0][2] in ("stop", "start")] == ["stop", "start"]


@pytest.mark.parametrize("reread", ["active", "unavailable", "pending", "malformed"])
def test_linux_completed_activation_is_rechecked_before_upload(linux, reread):
    running, completed, entered = False, False, False
    actions = []

    def run(cmd, **kwargs):
        nonlocal running, completed
        action = "job" if "--property=JobType" in cmd else cmd[2]
        actions.append(action)
        if action == "job":
            # Complete activation exactly between the unit snapshot and the job lookup.
            running, completed = True, True
            return subprocess.CompletedProcess(cmd, 1, "")  # the job object disappeared
        if action == "show":
            if not completed or reread == "pending":
                return systemctl_reply(cmd, active="inactive", sub="dead", job="73")
            if reread == "unavailable":
                return subprocess.CompletedProcess(cmd, 1, "")
            if reread == "malformed":
                return subprocess.CompletedProcess(cmd, 0, "LoadState=loaded\nActiveState=active\n")
            return systemctl_reply(cmd)
        if action == "stop":
            running = False
        if action == "start":
            running = True
        return subprocess.CompletedProcess(cmd, 0, "")

    with mock.patch.object(host.subprocess, "run", side_effect=run):
        if reread == "active":
            with host.paused_bridge():
                entered = True
                assert not running, "activation escaped the pause"
        else:
            with pytest.raises(RuntimeError, match="Cannot safely pause"):
                with host.paused_bridge():
                    pytest.fail("ambiguous state must prevent uploading")
    assert completed and running
    assert entered == (reread == "active")
    assert actions[:3] == ["show", "job", "show"]
    assert actions.count("show") == 2  # a changing job cannot cause an unbounded retry
    assert actions[3:] == (["stop", "start"] if entered else ["job"] if reread == "pending" else [])


def test_inactive_pending_stop_is_completed_without_resuming(linux):
    with mock.patch.object(host.subprocess, "run", side_effect=lambda cmd, **kwargs:
                           systemctl_reply(cmd, active="inactive", sub="dead", job="73", job_type="stop")) as command:
        with host.paused_bridge():
            pass
    assert [c.args[0][2] for c in command.call_args_list] == ["show", "show", "stop"]


@pytest.mark.parametrize("active,sub", [("active", "running"), ("activating", "auto-restart"),
                                        ("deactivating", "stop-sigterm")])
def test_linux_queued_stop_releases_port_without_resuming(linux, active, sub):
    holding_port = True
    actions = []

    def run(cmd, **kwargs):
        nonlocal holding_port
        action = "job" if "--property=JobType" in cmd else cmd[2]
        actions.append(action)
        if action == "stop":
            assert kwargs.get("check"), "stop completion must be checked"
            holding_port = False
        elif action == "start":
            holding_port = True
        return systemctl_reply(cmd, active=active, sub=sub, job="73", job_type="stop")

    with mock.patch.object(host.subprocess, "run", side_effect=run):
        with host.paused_bridge():
            assert not holding_port, "queued stop has not released the port"
    assert not holding_port
    assert actions == ["show", "job", "stop"]


@pytest.mark.parametrize("tool", ["wifi", "ota"])
@pytest.mark.parametrize("stage", ["stop", "body"])
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP])
def test_linux_entrypoint_signal_restores_bridge_and_handlers(tmp_path, isolated_home, tool, stage, signum):
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    executable(bin_dir / "systemctl", '''printf '%s\n' "$2" >> "$TEST_COMMAND_LOG"
case "$2" in
  show) printf 'LoadState=loaded\nActiveState=active\nSubState=running\nJob=0\n' ;;
  stop) if [ "$TEST_SIGNAL_STAGE" = stop ]; then kill -"$TEST_SIGNAL" "$PPID"; fi ;;
  start) kill -"$TEST_SIGNAL" "$PPID" ;; # a second signal must not interrupt restoration
esac
''')
    log = tmp_path / "commands.log"
    env = {**isolated_home, "PATH": f"{bin_dir}:/usr/bin:/bin", "PYTHONPATH": str(TOOLS),
           "TEST_COMMAND_LOG": str(log), "TEST_SIGNAL_STAGE": stage, "TEST_SIGNAL": str(signum.value)}
    script = '''
import json, os, signal, sys
from pathlib import Path
import ota, wifi_setup as wifi

tool, stage, number, root = sys.argv[1:]
root, number = Path(root), int(number)
log = root / "commands.log"
before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}

def event(name):
    with log.open("a") as f:
        f.write(name + "\\n")

def upload(*args):
    event("body")
    os.kill(os.getpid(), number)

if tool == "wifi":
    class USB:
        def open(self): pass
        def close(self): event("close")
    wifi.CONFIG = str(root / "absent-config.json")
    wifi.usb_ports = lambda: ["/fake/lump"]
    wifi.serial.Serial = USB
    wifi.probe_lump = lambda ser: True
    wifi.time.sleep = lambda secs: None
    wifi.configure = upload
    os.environ.pop("DESKBUDDY_PORT", None)
    sys.argv = ["wifi_setup.py", "--forget"]
    main = wifi.main
else:
    art = root / "art.bin"
    art.write_bytes(b"fake art")
    config = root / "test-config.json"
    config.write_text(json.dumps({"lump_token": "fake-token"}))
    ota.CONFIG, ota.ART = str(config), str(art)
    ota.resolve = lambda host: "127.0.0.1"
    ota.info = lambda *args, **kwargs: {"art_room": 1024, "art_sha": "old"}
    ota.send = upload
    sys.argv = ["ota.py", "--art"]
    main = ota.main

try:
    main()
finally:
    assert all(signal.getsignal(s) == handler for s, handler in before.items()), "handlers not restored"
'''
    result = subprocess.run([sys.executable, "-c", script, tool, stage, str(signum.value), str(tmp_path)],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 128 + signum.value, result.stdout + result.stderr
    expected = ["show", "stop"]
    if stage == "body":
        expected += ["body"] + (["close"] if tool == "wifi" else [])
    assert log.read_text().splitlines() == expected + ["start"]


@pytest.mark.parametrize("failure", [None, ValueError, SystemExit, KeyboardInterrupt])
def test_linux_pause_restores_custom_signal_handlers(monkeypatch, linux, failure):
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    custom = lambda number, frame: None
    for s in saved:
        signal.signal(s, custom)
    try:
        with mock.patch.object(host.subprocess, "run", side_effect=lambda cmd, **kwargs: systemctl_reply(cmd)):
            try:
                with host.paused_bridge():
                    assert all(signal.getsignal(s) is not custom for s in saved)
                    if failure:
                        raise failure()
            except BaseException as exc:
                assert failure is not None and isinstance(exc, failure)
        assert all(signal.getsignal(s) is custom for s in saved)
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)


@pytest.mark.parametrize("failed_action", ["stop", "start"])
def test_linux_pause_restores_handlers_when_service_commands_fail(linux, failed_action):
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    entered = False

    def run(cmd, **kwargs):
        if cmd[2] == failed_action and kwargs.get("check"):
            raise subprocess.CalledProcessError(1, cmd)
        return systemctl_reply(cmd)

    with mock.patch.object(host.subprocess, "run", side_effect=run) as command:
        with pytest.raises(subprocess.CalledProcessError) as failure:
            with host.paused_bridge():
                entered = True
    assert failure.value.cmd[2] == failed_action
    assert entered == (failed_action == "start")
    assert [c.args[0][2] for c in command.call_args_list] == ["show", "stop", "start"]
    assert all(signal.getsignal(s) == handler for s, handler in before.items())


def test_linux_discovery_prefers_wch_by_id_and_deduplicates_aliases(tmp_path, monkeypatch, linux):
    dev = tmp_path / "dev"
    by_id = dev / "serial" / "by-id"
    by_id.mkdir(parents=True)
    usb, acm, other = [dev / name for name in ("ttyUSB0", "ttyACM0", "ttyUSB1")]
    for path in (usb, acm, other):
        path.touch()
    ch340, ch343, generic = [by_id / name for name in ("usb-WCH_CH340", "usb-WCH_CH343", "usb-AAA-other")]
    for alias, target in ((ch340, usb), (ch343, acm), (generic, other)):
        alias.symlink_to(target)
    monkeypatch.setattr(host.glob, "glob", {
        "/dev/serial/by-id/*": list(map(str, (generic, ch343, ch340))),
        "/dev/ttyUSB*": list(map(str, (usb, other))), "/dev/ttyACM*": [str(acm)],
    }.__getitem__)
    monkeypatch.setattr(list_ports, "comports", lambda: [
        SimpleNamespace(device=str(usb), vid=0x1A86, pid=0x7523, description="USB Serial"),
        SimpleNamespace(device=str(acm), vid=0x1A86, pid=None, description="CH343"),
        SimpleNamespace(device=str(other), vid=0x1234, pid=1, description="Other")])
    assert host.usb_ports() == list(map(str, (ch340, ch343, generic)))


def test_linux_discovery_falls_back_to_ttyusb_and_ttyacm(monkeypatch, linux):
    monkeypatch.setattr(host.glob, "glob", {
        "/dev/serial/by-id/*": [], "/dev/ttyUSB*": ["/fake/ttyUSB2", "/fake/ttyUSB0"],
        "/dev/ttyACM*": ["/fake/ttyACM0"],
    }.__getitem__)
    monkeypatch.setattr(list_ports, "comports", lambda: [
        SimpleNamespace(device="/fake/ttyUSB2", vid=0x1A86, description="USB Serial")])
    assert host.usb_ports() == ["/fake/ttyUSB2", "/fake/ttyACM0", "/fake/ttyUSB0"]


def test_linux_by_id_preference_survives_missing_sysfs_metadata(monkeypatch, linux):
    monkeypatch.setattr(host.glob, "glob", lambda p: ["/fake/usb-AAA", "/fake/usb-1a86_USB_Serial"]
                        if p == "/dev/serial/by-id/*" else [])
    with mock.patch.object(list_ports, "comports", side_effect=OSError("sysfs unavailable")):
        assert host.usb_ports() == ["/fake/usb-1a86_USB_Serial", "/fake/usb-AAA"]


@pytest.mark.parametrize("adapters", [False, True])
def test_mac_discovery_keeps_s3_first_and_includes_usb_adapters(monkeypatch, adapters):
    monkeypatch.setattr(sys, "platform", "darwin")
    paths = {"/dev/cu.usbmodem*": ["/dev/cu.usbmodem2", "/dev/cu.usbmodem1"],
             "/dev/cu.usbserial-*": ["/dev/cu.usbserial-2", "/dev/cu.usbserial-1"] if adapters else [],
             "/dev/cu.wchusbserial*": ["/dev/cu.wchusbserial2", "/dev/cu.wchusbserial1"] if adapters else []}
    monkeypatch.setattr(host.glob, "glob", paths.__getitem__)
    ports = host.usb_ports()
    assert ports[:2] == ["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"]
    assert ports[2:] == (["/dev/cu.usbserial-1", "/dev/cu.usbserial-2",
                          "/dev/cu.wchusbserial1", "/dev/cu.wchusbserial2"] if adapters else [])


def test_probe_retries_through_ch340_reset_and_fragmented_boot_output(monkeypatch):
    clock = USBClock()
    ser = USB(clock, boot=1.9)
    monkeypatch.setattr(time, "monotonic", clock)
    assert host.probe_lump(ser)
    assert 1.9 < clock() <= 3.0
    assert len(ser.writes) > 1 and set(ser.writes) == {b"?\n"}
    assert (ser.timeout, ser.write_timeout) == (0.3, None)


@pytest.mark.parametrize("mode", [b"demo", b"offline", b"direct"])
def test_probe_accepts_complete_state(monkeypatch, mode):
    clock = USBClock()
    ser = USB(clock, response=STATE.replace(b"direct", mode))
    monkeypatch.setattr(time, "monotonic", clock)
    assert host.probe_lump(ser)
    assert (ser.timeout, ser.write_timeout) == (0.3, None)
    assert ser.writes and set(ser.writes) == {b"?\n"}


@pytest.mark.parametrize("response", [MISSING_ART_STATE, MISSING_ART_STATE.replace(b"\r\n", b"\n")],
                         ids=["crlf", "lf"])
def test_probe_accepts_firmware_art_error_after_reset(monkeypatch, response):
    clock = USBClock()
    ser = USB(clock, boot=1.9, response=response)
    ser.pending = [b"ets Jun  8 2016\r\nrst:0x1\r\n", MISSING_ART_BOOT]
    monkeypatch.setattr(time, "monotonic", clock)
    assert host.probe_lump(ser)
    assert 1.9 < clock() <= 3.0  # the boot banner alone must not identify the board
    assert len(ser.writes) > 1 and set(ser.writes) == {b"?\n"}
    assert (ser.timeout, ser.write_timeout) == (0.3, None)


@pytest.mark.parametrize("response", [
    b"ok mode=direct target= playing=(loop)\n",  # incomplete state
    STATE.replace(b"playing=idle(loop)", b"playing="),
    STATE.replace(b"(loop)", b"(unknown)"),
    STATE.replace(b"fps=24.0", b"fps=."),
    STATE.replace(b"fps=24.0", b"fps=1.2.3"),
    STATE.replace(b"seqs=20", b"seqs="),
    STATE.replace(b"direct", b"unknown"),
    STATE.rstrip() + b" extra=yes\n",
])
def test_probe_rejects_incomplete_or_malformed_state(monkeypatch, response):
    clock = USBClock()
    ser = USB(clock, response=response)
    monkeypatch.setattr(time, "monotonic", clock)
    assert not host.probe_lump(ser)
    assert 2.9 <= clock() <= 3.01
    assert (ser.timeout, ser.write_timeout) == (0.3, None)


@pytest.mark.parametrize("response", [
    MISSING_ART_BOOT,
    MISSING_ART_STATE.replace(b"lump", b"other"),
    MISSING_ART_STATE.replace(b"lump ", b""),
    MISSING_ART_STATE.replace(b"err", b"ok"),
    MISSING_ART_STATE.replace(b"bad", b"broken"),
    b"unrelated " + MISSING_ART_STATE,
    MISSING_ART_STATE.rstrip() + b" extra=yes\n",
    MISSING_ART_STATE.rstrip(),  # no complete line
])
def test_probe_rejects_incomplete_or_malformed_recovery_state(monkeypatch, response):
    clock = USBClock()
    ser = USB(clock, response=response)
    monkeypatch.setattr(time, "monotonic", clock)
    assert not host.probe_lump(ser)
    assert 2.9 <= clock() <= 3.01
    assert (ser.timeout, ser.write_timeout) == (0.3, None)


@pytest.mark.parametrize("response,noise", [(b"ok unrelated\n", False), (b"", True)])
def test_probe_rejects_banner_generic_ok_and_continuous_noise_within_deadline(monkeypatch, response, noise):
    clock = USBClock()
    ser = USB(clock, response=response, noise=noise)
    monkeypatch.setattr(time, "monotonic", clock)
    assert not host.probe_lump(ser)
    assert 2.9 <= clock() <= 3.01
    assert set(ser.writes) == {b"?\n"}
    assert (ser.timeout, ser.write_timeout) == (0.3, None)


@pytest.mark.parametrize("field", ["serial_number", "location"])
def test_linux_usb_identity_survives_tty_renumbering(monkeypatch, linux, field):
    port = SimpleNamespace(device="/fake/ttyUSB0", vid=0x1A86, pid=0x7523,
                           serial_number="intended-board" if field == "serial_number" else None, location="1-2")
    monkeypatch.setattr(list_ports, "comports", lambda: [port])
    identity = host.usb_identity(port.device)
    port.device = "/fake/ttyUSB3"
    assert host.usb_identity(port.device) == identity
    setattr(port, field, "another-board-or-socket")
    assert host.usb_identity(port.device) != identity


@pytest.mark.parametrize("fail_in_body", [False, True])
def test_running_linux_bridge_resumes_even_on_failure(linux, fail_in_body):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return systemctl_reply(cmd)

    with mock.patch.object(host.subprocess, "run", side_effect=run), \
         mock.patch.object(host.os.path, "isfile", side_effect=AssertionError("Library access")):
        try:
            with host.paused_bridge():
                assert calls[-1] == ["systemctl", "--user", "stop", host.SERVICE]
                if fail_in_body:
                    raise ValueError("upload failed")
        except ValueError:
            assert fail_in_body
    assert [c[2] for c in calls] == ["show", "stop", "start"]


@pytest.mark.parametrize("load,active", [("not-found", "inactive"), ("loaded", "inactive")])
def test_missing_unit_or_inactive_bridge_is_left_alone(linux, load, active):
    def run(cmd, **kwargs):
        return systemctl_reply(cmd, load=load, active=active, sub="dead")

    with mock.patch.object(host.subprocess, "run", side_effect=run) as command:
        with host.paused_bridge():
            pass
    assert not any(c.args[0][2] in ("stop", "start") for c in command.call_args_list)


def test_linux_without_systemctl_is_a_clean_skip(linux):
    with mock.patch.object(host.subprocess, "run", side_effect=FileNotFoundError) as command:
        with host.paused_bridge():
            pass
    assert command.call_count == 1


def test_stop_failure_prevents_upload_and_is_not_silenced(linux):
    def run(cmd, **kwargs):
        code = 1 if "stop" in cmd else 0
        if code and kwargs.get("check"):
            raise subprocess.CalledProcessError(1, cmd)
        result = systemctl_reply(cmd)
        result.returncode = code
        return result

    with mock.patch.object(host.subprocess, "run", side_effect=run) as command:
        with pytest.raises(subprocess.CalledProcessError):
            with host.paused_bridge():
                pytest.fail("must release USB before uploading")
    assert [c.args[0][2] for c in command.call_args_list] == ["show", "stop", "start"]


def test_stop_failure_regression_detects_unchecked_stop(monkeypatch, linux):
    # Exercise the regression test itself: an unchecked failed stop must reach its
    # forbidden upload body, rather than being hidden by an over-helpful fake.
    source = inspect.getsource(host.paused_bridge.__wrapped__)
    checked = '["systemctl", "--user", "stop", SERVICE], capture_output=True, check=True'
    assert checked in source
    namespace = vars(host).copy()
    exec(source.replace(checked, checked.replace("check=True", "check=False")), namespace)
    monkeypatch.setattr(host, "paused_bridge", namespace["paused_bridge"])
    with pytest.raises(pytest.fail.Exception, match="must release USB before uploading"):
        test_stop_failure_prevents_upload_and_is_not_silenced(linux)


def test_mac_service_still_uses_launchd(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with mock.patch.object(host.os.path, "isfile", return_value=True), \
         mock.patch.object(host.signal, "signal", side_effect=AssertionError("Linux-only")), \
         mock.patch.object(host.subprocess, "run") as command:
        with host.paused_bridge():
            pass
    assert [c.args[0][:2] for c in command.call_args_list] == [["launchctl", "bootout"], ["launchctl", "bootstrap"]]


def executable(path, text):
    path.write_text("#!/bin/sh\n" + text)
    path.chmod(0o755)


def run_flash_pause(tmp_path, isolated_home, systemctl, body="exit 17\n", **variables):
    """Run only flash.sh's guard against fake commands; never build or flash."""
    text = (TOOLS / "flash.sh").read_text()
    block = text.split("# the bridge holds the serial port:", 1)[1].split(
        "\n# Build and validate the selected board images before uploading.", 1)[0]
    block = block.split("\n", 1)[1]
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    executable(bin_dir / "uname", "echo Linux\n")
    executable(bin_dir / "sleep", "exit 0\n")
    executable(bin_dir / "launchctl", "echo unexpected-launchctl >&2; exit 99\n")
    executable(bin_dir / "systemctl", systemctl)
    log = tmp_path / "commands.log"
    env = {**isolated_home, "PATH": f"{bin_dir}:/usr/bin:/bin", "TEST_COMMAND_LOG": str(log), **variables}
    result = subprocess.run(["bash", "-c", "set -euo pipefail\n" + block + "\n" + body],
                            env=env, capture_output=True, text=True, timeout=10)
    assert not (Path(env["HOME"]) / "Library").exists()
    return result, log.read_text().splitlines()


@pytest.mark.parametrize("reread", ["active", "unavailable", "pending", "malformed"])
def test_flash_completed_activation_is_rechecked_before_upload(tmp_path, isolated_home, reread):
    systemctl = '''case "$2:$3" in
  show:--property=JobType)
    echo job >> "$TEST_COMMAND_LOG"
    touch "$TEST_RUNNING" "$TEST_COMPLETED"
    exit 1 ;;
  show:*)
    echo show >> "$TEST_COMMAND_LOG"
    if [ ! -f "$TEST_COMPLETED" ] || [ "$TEST_REREAD" = pending ]; then
      printf 'LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob=73\n'
    elif [ "$TEST_REREAD" = unavailable ]; then exit 1
    elif [ "$TEST_REREAD" = malformed ]; then printf 'LoadState=loaded\nActiveState=active\n'
    else printf 'LoadState=loaded\nActiveState=active\nSubState=running\nJob=0\n'
    fi ;;
  stop:*) echo stop >> "$TEST_COMMAND_LOG"; rm -f "$TEST_RUNNING" ;;
  start:*) echo start >> "$TEST_COMMAND_LOG"; touch "$TEST_RUNNING" ;;
esac
'''
    body = '''if [ -f "$TEST_RUNNING" ]; then echo unsafe-upload >&2; exit 91; fi
echo body >> "$TEST_COMMAND_LOG"
exit 17
'''
    result, actions = run_flash_pause(tmp_path, isolated_home, systemctl, body,
                                      TEST_RUNNING=str(tmp_path / "running"),
                                      TEST_COMPLETED=str(tmp_path / "completed"), TEST_REREAD=reread)
    if reread == "active":
        assert result.returncode == 17, result.stderr
        assert actions == ["show", "job", "show", "stop", "body", "start"]
    else:
        assert result.returncode != 0
        assert "Cannot safely pause" in result.stderr
        assert actions == ["show", "job", "show"] + (["job"] if reread == "pending" else [])
    assert (tmp_path / "running").exists()


@pytest.mark.parametrize("active,sub", [("active", "running"), ("activating", "auto-restart"),
                                        ("deactivating", "stop-sigterm")])
def test_flash_queued_stop_releases_port_without_resuming(tmp_path, isolated_home, active, sub):
    running = tmp_path / "running"
    running.touch()
    systemctl = '''case "$2:$3" in
  show:--property=JobType) echo job >> "$TEST_COMMAND_LOG"; echo stop ;;
  show:*) echo show >> "$TEST_COMMAND_LOG"
    printf 'LoadState=loaded\nActiveState=%s\nSubState=%s\nJob=73\n' "$TEST_ACTIVE" "$TEST_SUB" ;;
  stop:*) echo stop >> "$TEST_COMMAND_LOG"; rm -f "$TEST_RUNNING" ;;
  start:*) echo start >> "$TEST_COMMAND_LOG"; touch "$TEST_RUNNING" ;;
esac
'''
    body = '''if [ -f "$TEST_RUNNING" ]; then echo unsafe-upload >&2; exit 91; fi
echo body >> "$TEST_COMMAND_LOG"
exit 17
'''
    result, actions = run_flash_pause(tmp_path, isolated_home, systemctl, body,
                                      TEST_RUNNING=str(running), TEST_ACTIVE=active, TEST_SUB=sub)
    assert result.returncode == 17, result.stderr
    assert actions == ["show", "job", "stop", "body"]
    assert not running.exists()


@pytest.mark.parametrize("guard", ["python", "flash"])
@pytest.mark.parametrize("state,job_type,code", [
    ("", "", 1),  # unreachable user manager
    ("LoadState=loaded\nActiveState=active\nSubState=running\n", "", 0),  # missing Job
    ("LoadState=loaded\nActiveState=unknown\nSubState=unknown\nJob=0\n", "", 0),
    ("LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob=invalid\n", "", 0),
    ("LoadState=error\nActiveState=active\nSubState=running\nJob=0\n", "", 0),
    ("LoadState=not-found\n", "", 0),  # absence must also have a complete stopped snapshot
    ("LoadState=not-found\nActiveState=active\nSubState=running\nJob=0\n", "", 0),
    ("LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob=73\n", "unknown", 0),
])
def test_ambiguous_systemd_state_prevents_upload(tmp_path, isolated_home, linux, guard, state, job_type, code):
    if guard == "python":
        def run(cmd, **kwargs):
            output = job_type if "--property=JobType" in cmd else state
            return subprocess.CompletedProcess(cmd, code, output)

        with mock.patch.object(host.subprocess, "run", side_effect=run) as command:
            with pytest.raises(RuntimeError, match="Cannot safely pause"):
                with host.paused_bridge():
                    pytest.fail("ambiguous state must prevent uploading")
        assert not any(c.args[0][2] in ("stop", "start") for c in command.call_args_list)
    else:
        systemctl = '''echo "$2" >> "$TEST_COMMAND_LOG"
case "$2:$3" in
  show:--property=JobType) printf '%s\n' "$TEST_JOB_TYPE" ;;
  show:*) printf '%s' "$TEST_STATE"; exit "$TEST_CODE" ;;
esac
'''
        result, actions = run_flash_pause(tmp_path, isolated_home, systemctl,
                                          "echo unsafe-upload >&2\nexit 91\n", TEST_STATE=state,
                                          TEST_JOB_TYPE=job_type, TEST_CODE=str(code))
        assert result.returncode != 0
        assert "Cannot safely pause" in result.stderr
        assert "unsafe-upload" not in result.stderr
        assert all(action == "show" for action in actions)


@pytest.mark.parametrize("active,sub,expected", [("inactive", "dead", ["show"]),
                                                 ("active", "running", ["show", "stop", "start"]),
                                                 ("deactivating", "stop-sigterm", ["show", "stop"])])
@pytest.mark.parametrize("job", ["", "0"])
def test_linux_no_job_representation_preserves_intent(linux, active, sub, expected, job):
    with mock.patch.object(host.subprocess, "run", side_effect=lambda cmd, **kwargs:
                           systemctl_reply(cmd, active=active, sub=sub, job=job)) as command:
        with host.paused_bridge():
            pass
    assert [c.args[0][2] for c in command.call_args_list] == expected


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
@pytest.mark.parametrize("stage", ["stop", "body"])
@pytest.mark.parametrize("intent", ["run", "stop"])
def test_flash_interruption_restores_only_running_intent(tmp_path, isolated_home, signum, stage, intent):
    running = tmp_path / "running"
    running.touch()
    systemctl = '''case "$2:$3" in
  show:--property=JobType) echo job >> "$TEST_COMMAND_LOG"; echo stop ;;
  show:*) echo show >> "$TEST_COMMAND_LOG"
    printf 'LoadState=loaded\nActiveState=active\nSubState=running\nJob=%s\n' "$TEST_JOB" ;;
  stop:*) echo stop >> "$TEST_COMMAND_LOG"
    rm -f "$TEST_RUNNING"
    if [ "$TEST_STAGE" = stop ]; then kill -"$TEST_SIGNAL" "$PPID"; fi ;;
  start:*) echo start >> "$TEST_COMMAND_LOG"
    touch "$TEST_RUNNING"
    kill -"$TEST_SIGNAL" "$PPID" ;; # repeated signals must let cleanup finish
esac
'''
    body = '''echo body >> "$TEST_COMMAND_LOG"
kill -"$TEST_SIGNAL" "$$"
echo forbidden-body >&2
exit 91
'''
    result, actions = run_flash_pause(tmp_path, isolated_home, systemctl, body,
                                      TEST_RUNNING=str(running), TEST_STAGE=stage,
                                      TEST_SIGNAL=str(signum.value), TEST_JOB="0" if intent == "run" else "73")
    assert result.returncode == 128 + signum.value, result.stderr
    expected = ["show"] + (["job"] if intent == "stop" else []) + ["stop"]
    if stage == "body":
        expected += ["body"]
    assert actions == expected + (["start"] if intent == "run" else [])
    assert running.exists() == (intent == "run")
    assert "forbidden-body" not in result.stderr


@pytest.mark.parametrize("body_exit", [0, 17])
@pytest.mark.parametrize("resume_exit", [0, 9])
def test_flash_cleanup_preserves_failure_status(tmp_path, isolated_home, body_exit, resume_exit):
    systemctl = '''echo "$2" >> "$TEST_COMMAND_LOG"
case "$2" in
  show) printf 'LoadState=loaded\nActiveState=active\nSubState=running\nJob=0\n' ;;
  start) exit "$TEST_RESUME_EXIT" ;;
esac
'''
    result, actions = run_flash_pause(tmp_path, isolated_home, systemctl, f"exit {body_exit}\n",
                                      TEST_RESUME_EXIT=str(resume_exit))
    assert result.returncode == (body_exit or resume_exit), result.stderr
    assert actions == ["show", "stop", "start"]
    if resume_exit:
        assert "Failed to resume" in result.stderr


@pytest.fixture
def isolated_home(tmp_path):
    home = tmp_path / "home with spaces"
    home.mkdir()
    return {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share")}


def test_installer_prints_a_valid_unit_without_side_effects(tmp_path, isolated_home):
    # Even if a preview accidentally tries an installer/system command, it cannot reach the real one.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable(bin_dir / "uname", "echo Linux\n")
    for name in ("systemctl", "launchctl", "swiftc", "python3", "pip", "cp", "mkdir"):
        executable(bin_dir / name, "echo unexpected-command >&2; exit 90\n")
    env = {**isolated_home, "PATH": f"{bin_dir}:/usr/bin:/bin"}
    unit = subprocess.run(["bash", str(TOOLS / "install_bridge.sh"), "--print-unit"],
                          env=env, capture_output=True, text=True, check=True).stdout
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(unit)
    assert config.sections() == ["Unit", "Service", "Install"]
    assert config["Service"]["ExecStart"] == '"%h/.desk-buddy/venv/bin/python" -u "%h/.desk-buddy/bridge.py"'
    assert config["Service"]["Restart"] == "on-failure"
    assert config["Service"]["RestartSec"] == "5"
    assert config["Service"]["MemoryMax"] == "300M"
    assert config["Service"]["StandardError"] == "journal"
    assert config["Install"]["WantedBy"] == "default.target"
    preview = subprocess.run(["bash", str(TOOLS / "install_bridge.sh"), "--dry-run"],
                             env=env, capture_output=True, text=True, check=True)
    assert unit in preview.stdout
    assert "systemctl --user" in preview.stdout
    assert "macOS-only; skipped" in preview.stdout
    assert not list(Path(env["HOME"]).iterdir())


@pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("systemd-analyze"),
                    reason="systemd-analyze needs a Linux systemd installation")
def test_generated_unit_passes_systemd_verification(tmp_path, isolated_home):
    unit = subprocess.run(["bash", str(TOOLS / "install_bridge.sh"), "--print-unit"],
                          env=isolated_home, capture_output=True, text=True, check=True).stdout
    runtime = Path(isolated_home["HOME"]) / ".desk-buddy"
    (runtime / "venv/bin").mkdir(parents=True)
    (runtime / "venv/bin/python").symlink_to(sys.executable)
    (runtime / "bridge.py").touch()
    path = tmp_path / host.SERVICE
    path.write_text(unit)
    # Verification parses the real generated unit; it never installs it or starts the service.
    result = subprocess.run(["systemd-analyze", "--user", "--man=no", "verify", str(path)],
                            env=isolated_home, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (Path(isolated_home["XDG_CONFIG_HOME"]) / "systemd").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux journal logging")
def test_linux_journal_receives_activity_without_starting_bridge(tmp_path, isolated_home):
    env = {**isolated_home, "PYTHONPATH": str(TOOLS), "DESKBUDDY_HOME": str(tmp_path / "runtime"),
           "JOURNAL_STREAM": "1:1"}
    result = subprocess.run([sys.executable, "-c", "import buddy_bridge; buddy_bridge.log.info('journal marker')"],
                            env=env, capture_output=True, text=True, check=True)
    assert "journal marker" in result.stderr
    assert "journal marker" in (tmp_path / "runtime/bridge.log").read_text()


def test_hooks_install_idempotently_and_uninstall_in_temporary_config(tmp_path, isolated_home):
    config = tmp_path / "claude-test-config"
    config.mkdir()
    path = config / "settings.json"
    original = {"env": {"EXAMPLE": "kept"}, "hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": "echo existing-hook"}]}]}}
    path.write_text(json.dumps(original))
    cmd = [sys.executable, str(TOOLS / "install_hooks.py"), "--config-dir", str(config)]
    subprocess.run(cmd, env=isolated_home, capture_output=True, text=True, check=True)
    installed = json.loads(path.read_text())
    assert installed["env"] == original["env"]
    assert installed["hooks"]["Stop"][0] == original["hooks"]["Stop"][0]
    assert len(installed["hooks"]) == 11
    assert installed["hooks"]["Stop"][1]["hooks"][0]["async"] is True
    status = Path(isolated_home["HOME"]) / ".desk-buddy/statusline.sh"
    assert installed["statusLine"]["command"] == str(status)
    assert os.access(status, os.X_OK)
    assert list(config.glob("settings.json.bak-*"))
    subprocess.run(cmd, env=isolated_home, capture_output=True, text=True, check=True)
    assert json.loads(path.read_text()) == installed
    subprocess.run(cmd + ["--uninstall"], env=isolated_home, capture_output=True, text=True, check=True)
    assert json.loads(path.read_text()) == original
    assert not (Path(isolated_home["HOME"]) / ".claude").exists()
    assert not (Path(isolated_home["HOME"]) / "Library").exists()


def test_hooks_preserve_a_custom_statusline(tmp_path, isolated_home):
    config = tmp_path / "claude-custom-status"
    config.mkdir()
    line = {"type": "command", "command": "my-statusline"}
    (config / "settings.json").write_text(json.dumps({"statusLine": line}))
    subprocess.run([sys.executable, str(TOOLS / "install_hooks.py"), "--config-dir", str(config)],
                   env=isolated_home, capture_output=True, text=True, check=True)
    assert json.loads((config / "settings.json").read_text())["statusLine"] == line


@pytest.mark.parametrize("fail", [False, True])
def test_wifi_releases_fake_usb_before_resuming_bridge(tmp_path, monkeypatch, linux, fail):
    events = []

    @contextmanager
    def paused():
        events.append("pause")
        try:
            yield
        finally:
            events.append("resume")

    ser = mock.Mock()
    ser.close.side_effect = lambda: events.append("close")
    if fail:
        ser.open.side_effect = OSError("fake USB failure")
    monkeypatch.setattr(wifi, "HOME", str(tmp_path))
    monkeypatch.setattr(wifi, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(wifi, "usb_ports", lambda: ["/fake/by-id/buddy"])
    monkeypatch.setattr(wifi, "paused_bridge", paused)
    monkeypatch.setattr(wifi.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(wifi.serial, "Serial", lambda: ser)
    monkeypatch.setattr(wifi, "probe_lump", lambda port: True)
    monkeypatch.setattr(wifi, "read_lines", lambda *args: "restarting")
    monkeypatch.setattr(sys, "argv", ["wifi_setup.py", "--forget"])
    monkeypatch.delenv("DESKBUDDY_PORT", raising=False)
    with mock.patch.object(wifi.subprocess, "run", side_effect=AssertionError("Mac SSID command")):
        assert wifi.current_ssid() == ""
        if fail:
            with pytest.raises(SystemExit, match="verified Lump"):
                wifi.main()
        else:
            wifi.main()
            ser.write.assert_called_once_with(b"w\n")
    assert (ser.port, ser.baudrate, ser.dtr, ser.rts) == ("/fake/by-id/buddy", 115200, False, False)
    assert events == ["pause", "close", "resume"]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_wifi_never_sends_credentials_to_unverified_usb(tmp_path, monkeypatch, platform, explicit):
    monkeypatch.setattr(sys, "platform", platform)
    clock = USBClock()
    alien = USB(clock, response=b"ok generic\n")
    monkeypatch.setattr(time, "monotonic", clock)
    monkeypatch.setattr(wifi, "HOME", str(tmp_path))
    monkeypatch.setattr(wifi, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(wifi, "usb_ports", lambda: ["/fake/unrelated"])
    monkeypatch.setattr(wifi, "paused_bridge", nullcontext)
    monkeypatch.setattr(wifi.serial, "Serial", lambda: alien)
    monkeypatch.setattr(wifi.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(wifi, "current_ssid", lambda: "")
    monkeypatch.setattr("builtins.input", lambda prompt: "test-network")
    monkeypatch.setattr(wifi.getpass, "getpass", lambda prompt: "test-password")
    monkeypatch.setattr(wifi, "posix_tz", lambda: "UTC0")
    monkeypatch.setattr(wifi, "read_lines", lambda *args: "restarting wifi=connected")
    monkeypatch.setattr(sys, "argv", ["wifi_setup.py"])
    if explicit:
        monkeypatch.setenv("DESKBUDDY_PORT", "/fake/unrelated")
    else:
        monkeypatch.delenv("DESKBUDDY_PORT", raising=False)
    with pytest.raises(SystemExit, match="verified Lump"):
        wifi.main()
    assert alien.closed
    assert alien.writes and set(alien.writes) == {b"?\n"}
    assert not (tmp_path / "config.json").exists()


@pytest.mark.parametrize("platform,port", [
    ("linux", "/fake/lump"), ("darwin", "/dev/cu.usbserial-0001"), ("darwin", "/dev/cu.wchusbserial0001")])
def test_wifi_skips_rejected_candidate_then_configures_booting_lump(tmp_path, monkeypatch, platform, port):
    monkeypatch.setattr(sys, "platform", platform)
    clock = USBClock()
    alien, lump = USB(clock, response=b"ok generic\n"), USB(clock, boot=4.8)
    monkeypatch.setattr(time, "monotonic", clock)
    monkeypatch.setattr(wifi, "HOME", str(tmp_path))
    monkeypatch.setattr(wifi, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(wifi, "usb_ports", lambda: ["/fake/unrelated", port])
    monkeypatch.setattr(wifi, "paused_bridge", nullcontext)
    monkeypatch.setattr(wifi.serial, "Serial", mock.Mock(side_effect=[alien, lump]))
    monkeypatch.setattr(wifi.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(wifi, "read_lines", lambda *args: "restarting")
    monkeypatch.setattr(sys, "argv", ["wifi_setup.py", "--forget"])
    monkeypatch.delenv("DESKBUDDY_PORT", raising=False)
    wifi.main()
    assert alien.closed and lump.closed
    assert lump.port == port
    assert set(alien.writes) == {b"?\n"}
    assert lump.writes[-1] == b"w\n" and set(lump.writes[:-1]) == {b"?\n"}


@pytest.mark.parametrize("port,desk_port,expected", [
    ("/fake/legacy-port", None, "/fake/legacy-port"),
    (None, "/fake/bridge-port", "/fake/bridge-port"),
    ("/fake/legacy-port", "/fake/bridge-port", "/fake/legacy-port"),
    ("", "/fake/bridge-port", "/fake/bridge-port"),
    (None, None, "/fake/discovered-ttyUSB0"),
    (None, "socket://lump.local:4210", "/fake/discovered-ttyUSB0"),
])
def test_wifi_port_environment_precedence_keeps_verification(tmp_path, monkeypatch, linux, port, desk_port, expected):
    for name, value in (("PORT", port), ("DESKBUDDY_PORT", desk_port)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.setattr(wifi, "CONFIG", str(tmp_path / "absent-config.json"))
    monkeypatch.setattr(wifi, "usb_ports", lambda: ["/fake/discovered-ttyUSB0"])
    monkeypatch.setattr(wifi, "paused_bridge", nullcontext)
    monkeypatch.setattr(wifi.time, "sleep", lambda seconds: None)
    ser = mock.Mock()
    monkeypatch.setattr(wifi.serial, "Serial", lambda: ser)
    verified = []

    def probe(candidate):
        assert candidate is ser
        verified.append(candidate.port)
        return True

    def configure(candidate, *args):
        assert verified == [expected], "configuration must follow the identity probe"
        assert candidate.port == expected

    monkeypatch.setattr(wifi, "probe_lump", probe)
    monkeypatch.setattr(wifi, "configure", configure)
    monkeypatch.setattr(sys, "argv", ["wifi_setup.py", "--forget"])
    wifi.main()
    assert ser.port == expected
    ser.close.assert_called_once_with()


@pytest.mark.parametrize("forget", [False, True], ids=["configure", "forget"])
def test_linux_wifi_configures_lump_with_missing_art(tmp_path, monkeypatch, linux, forget):
    clock = USBClock()

    class RecoveryUSB(USB):
        def write(self, data):
            result = super().write(data)
            if data.startswith(b"w "):
                self.pending.extend([b"ok wifi saved, restarting\n", MISSING_ART_BOOT])
            elif data == b"w\n":
                self.pending.extend([b"ok wifi forgotten, restarting\n", MISSING_ART_BOOT])
            elif data == b"W\n":
                self.pending.append(b"ok wifi=connected ip=192.0.2.1\n")
            return result

    lump = RecoveryUSB(clock, boot=1.9, response=MISSING_ART_STATE)
    lump.pending = [b"ets Jun  8 2016\n", MISSING_ART_BOOT]
    config = tmp_path / "config.json"
    if forget:
        config.write_text(json.dumps({"lump_token": "fake-pairing-token", "lump_host": "lump.local"}))
    monkeypatch.setattr(time, "monotonic", clock)
    monkeypatch.setattr(time, "time", clock)
    monkeypatch.setattr(wifi.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(wifi, "HOME", str(tmp_path))
    monkeypatch.setattr(wifi, "CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(wifi, "usb_ports", lambda: ["/fake/lump"])
    monkeypatch.setattr(wifi, "paused_bridge", nullcontext)
    monkeypatch.setattr(wifi.serial, "Serial", lambda: lump)
    monkeypatch.setattr(wifi, "current_ssid", lambda: "")
    monkeypatch.setattr(wifi, "posix_tz", lambda: "UTC0")
    monkeypatch.setattr(wifi.secrets, "token_hex", lambda size: "fake-pairing-token")
    monkeypatch.setattr("builtins.input", lambda prompt: "test-network")
    monkeypatch.setattr(wifi.getpass, "getpass", lambda prompt: "test-password")
    monkeypatch.setattr(sys, "argv", ["wifi_setup.py", "--forget"] if forget else ["wifi_setup.py"])
    monkeypatch.delenv("DESKBUDDY_PORT", raising=False)
    wifi.main()  # real configure()/read_lines(), including no ready banner after reboot
    command = b"w\n" if forget else b"w test-network\ttest-password\tfake-pairing-token\tUTC0\n"
    index = lump.writes.index(command)
    assert index > 0 and set(lump.writes[:index]) == {b"?\n"}
    assert lump.writes[index:] == ([command] if forget else [command, b"W\n"])
    assert lump.closed and (lump.baudrate, lump.dtr, lump.rts) == (115200, False, False)
    expected = {"lump_host": "lump.local"}
    if not forget:
        expected["lump_token"] = "fake-pairing-token"
    assert json.loads(config.read_text()) == expected


@pytest.mark.parametrize("changed,fail", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_ota_pauses_only_for_upload_and_resumes_on_failure(tmp_path, monkeypatch, changed, fail, platform):
    events = []
    monkeypatch.setattr(sys, "platform", platform)

    @contextmanager
    def paused():
        events.append("pause")
        try:
            yield
        finally:
            events.append("resume")

    art = tmp_path / "lump.bin"
    art.write_bytes(b"fake art")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"lump_token": "fake-pairing-token"}))
    monkeypatch.setattr(ota, "CONFIG", str(config))
    monkeypatch.setattr(ota, "ART", str(art))
    monkeypatch.setattr(ota, "resolve", lambda host: "127.0.0.1")
    monkeypatch.setattr(ota, "info", lambda *args, **kwargs: {
        "art_room": 1024, "art_sha": "old" if changed else hashlib.sha256(art.read_bytes()).hexdigest()})
    monkeypatch.setattr(ota, "paused_bridge", paused)
    monkeypatch.setattr(ota, "wait_back", lambda *args: {})
    monkeypatch.setattr(sys, "argv", ["ota.py", "--art"])

    def send(*args):
        events.append("send")
        if fail:
            raise RuntimeError("fake update failure")

    monkeypatch.setattr(ota, "send", send)
    if fail:
        with pytest.raises(RuntimeError):
            ota.main()
    else:
        ota.main()
    assert events == ((["pause", "send", "resume"] if platform == "linux" else ["send"]) if changed else [])


@pytest.mark.parametrize("load,active,fail_stop,expected", [
    ("loaded", True, False, ["show", "stop", "start"]),
    ("not-found", False, False, ["show"]),
    ("loaded", False, False, ["show"]),
    ("loaded", True, True, ["show", "stop", "start"]),
    ("loaded", "auto-restart", False, ["show", "stop", "start"]),
    ("loaded", "pending-start", False, ["show", "show", "stop", "start"]),
    ("loaded", "pending-stop", False, ["show", "show", "stop"]),
])
def test_flash_pause_block_in_isolation(tmp_path, isolated_home, load, active, fail_stop, expected):
    # Execute only the bridge block. Nothing can build firmware, flash or open a serial device.
    text = (TOOLS / "flash.sh").read_text()
    block = text.split("# the bridge holds the serial port:", 1)[1].split(
        "\n# Build and validate the selected board images before uploading.", 1)[0]
    block = block.split("\n", 1)[1]
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    executable(bin_dir / "uname", "echo Linux\n")
    executable(bin_dir / "sleep", "exit 0\n")
    executable(bin_dir / "launchctl", "echo unexpected-launchctl >&2; exit 99\n")
    executable(bin_dir / "systemctl", '''printf '%s\n' "$2" >> "$TEST_COMMAND_LOG"
case "$2" in
  show)
    case "$3" in
      --property=LoadState) printf '%s\n' "$TEST_LOAD_STATE" ;;
      --property=JobType) printf '%s\n' "$TEST_JOB_TYPE" ;;
      *) printf 'LoadState=%s\nActiveState=%s\nSubState=%s\nJob=%s\n' \\
           "$TEST_LOAD_STATE" "$TEST_ACTIVE_STATE" "$TEST_SUB_STATE" "$TEST_JOB" ;;
    esac ;;
  is-active) exit "$TEST_ACTIVE_EXIT" ;;
  stop) exit "$TEST_STOP_EXIT" ;;
esac
''')
    log = tmp_path / "commands.log"
    env = {**isolated_home, "PATH": f"{bin_dir}:/usr/bin:/bin", "TEST_COMMAND_LOG": str(log),
           "TEST_LOAD_STATE": load, "TEST_ACTIVE_EXIT": "0" if active is True else "3",
           "TEST_ACTIVE_STATE": "active" if active is True else "activating" if active == "auto-restart" else "inactive",
           "TEST_SUB_STATE": "auto-restart" if active == "auto-restart" else "running" if active is True else "dead",
           "TEST_JOB": "73" if str(active).startswith("pending-") else "0",
           "TEST_JOB_TYPE": "stop" if active == "pending-stop" else "start",
           "TEST_STOP_EXIT": "1" if fail_stop else "0"}
    result = subprocess.run(["bash", "-c", "set -eu\n" + block + "\nexit 17\n"],
                            env=env, capture_output=True, text=True)
    assert result.returncode == (1 if fail_stop else 17), result.stderr
    assert log.read_text().splitlines() == expected
    assert not (Path(env["HOME"]) / "Library").exists()
