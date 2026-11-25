#!/usr/bin/env python3
"""USB boot/protocol/soak check; close the bridge before running via sg dialout.
Never configures Wi-Fi or writes flash.
Resets after the soak and leaves the board offline/idle with boot defaults.
--leave-counts W A U sets a visible snapshot in the final session, then waits
for offline mode and closes without another reset (DTR/RTS false).

  sg dialout -c 'tools/.venv/bin/python tools/check_esp32_32e.py --port /dev/ttyUSB0 --seconds 120'
"""
import argparse
from datetime import datetime, timezone
import time
import serial

CRASH_MARKERS = ("Guru Meditation", "Brownout", "Backtrace:", "abort()")


def validate_single_boot(lines, context):
    if (sum("desk-buddy lump ready:" in s for s in lines) != 1 or
            sum(s.startswith("rst:") for s in lines) != 1 or
            any(marker in s for s in lines for marker in CRASH_MARKERS)):
        raise RuntimeError(f"crash or reboot during {context}")


def discover(ser, started, receive):
    """Probe throughout reset/startup; measure the bridge deadline from open."""
    deadline, next_query = started + 3, started
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now >= next_query:
            print(f"{now - started:7.2f} > ? (discovery)", flush=True)
            ser.write(b"?\n"); ser.flush()
            next_query = now + 0.2
        ser.timeout = min(0.05, max(0, deadline - time.monotonic()))
        line = ser.readline().decode(errors="replace").strip()
        at = time.monotonic()
        if line:
            receive(line, at)
            if at <= deadline and line.startswith("ok mode="):
                print(f"PASS: discovery ok at {at - started:.3f}s (limit 3.000s from port open)", flush=True)
                return at - started
    raise RuntimeError("no discovery ok within 3.000s of port open")


def validate_boot(lines):
    if any(line.startswith("err ") for line in lines):
        raise RuntimeError("boot reported an error")
    validate_single_boot(lines, "boot")
    for required in ("panel readback PASS", "scaled sprite readback PASS (16/16 pixels)",
                     "MADCTL=00000020 rotation=0 240x320", "psram=0",
                     "panel native mapping PASS", "panel full clear PASS"):
        if not any(required in line for line in lines):
            raise RuntimeError(f"boot check missing: {required}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", required=True, help="USB serial path (a stable by-id path is also accepted)")
    p.add_argument("--seconds", type=int, default=120)
    p.add_argument("--leave-counts", type=int, nargs=3, metavar=("WORK", "WAIT", "DONE"))
    p.add_argument("--wifi", choices=("any", "off", "joining", "connected"), default="any",
                   help="expected Wi-Fi status; default accepts any state without changing it")
    a = p.parse_args()
    if a.leave_counts is not None and min(a.leave_counts) < 0:
        p.error("--leave-counts must be non-negative")
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = a.port, 115200, 0.2
    ser.dtr = False; ser.rts = False
    started = time.monotonic()
    ser.open()
    print(f"USB opened {datetime.now(timezone.utc).isoformat()} expected_wifi={a.wifi}", flush=True)
    lines = []

    def receive(line, at):
        print(f"{at - started:7.2f} < {line}", flush=True)
        lines.append(line)

    def read_for(seconds):
        end = time.monotonic() + seconds
        received = []
        while time.monotonic() < end:
            line = ser.readline().decode(errors="replace").strip()
            if line:
                receive(line, time.monotonic()); received.append(line)
        return received

    def command(cmd, expect="ok", seconds=1):
        print(f"{time.monotonic() - started:7.2f} > {cmd}", flush=True)
        ser.write((cmd + "\n").encode()); ser.flush()
        got = read_for(seconds)
        if not any(expect in line for line in got) or any(line.startswith("err ") for line in got):
            raise RuntimeError(f"{cmd!r} failed: {got}")

    normal_reset = False

    def reset():
        # EN pulse, leaving GPIO0 high. No flash or preferences write.
        ser.rts = True; time.sleep(0.1); ser.rts = False

    try:
        # EN pulse, leaving GPIO0 high; include it in the port-open deadline.
        reset()
        discover(ser, started, receive)
        ser.timeout = 0.2
        validate_boot(lines)
        command("?", "ok mode=")
        command("P", "ok panel invert=1 bgr=0 rotation=0 width=240 height=320")
        command("P 1 1 1", "ok panel invert=1 bgr=1 rotation=1 width=320 height=240")
        command("P 1 0 0", "ok panel invert=1 bgr=0 rotation=0 width=240 height=320")
        command("s working", "target=working")
        command("s done", "target=done")
        command("o 2 1 0", "ok overlay working=2 waiting=1 unseen=0")
        command("c 3366FF", "ok colour 3366FF")
        command("i", "ok touch=xpt2046")
        command("T 650", "ok threshold=650 pressure")
        command("T 600", "ok threshold=600 pressure")
        expected_wifi = "ok wifi=" if a.wifi == "any" else f"ok wifi={a.wifi}"
        command("W", expected_wifi)
        command("X", "tap", seconds=4)
        command("s demo", "ok mode=demo")
        # Keep heartbeats flowing; ? does not reset a transition or its timer.
        deadline = time.monotonic() + a.seconds
        while time.monotonic() < deadline:
            command("?", "ok mode=", seconds=min(10, max(0.2, deadline - time.monotonic())))
        validate_single_boot(lines, "soak")
        command("i", "ok touch=xpt2046")
        print(f"PASS: protocol and {a.seconds}s soak; one boot, no crash/reboot.", flush=True)
        # Leave nondefault settings immediately before reset, so the following
        # P/i queries also check that tuning and sensitivity did not persist.
        command("P 1 1 1", "ok panel invert=1 bgr=1 rotation=1 width=320 height=240")
        command("T 650", "ok threshold=650 pressure")
        # Reset clears demo, tint and counters too. The normal-mode query counts
        # as a host command, so reset once more afterwards. Optional final
        # counts are sent only in that last session, without another reset.
        start = len(lines)
        reset(); read_for(3)
        validate_boot(lines[start:])
        command("?", "ok mode=direct target=idle playing=idle(loop)")
        command("P", "ok panel invert=1 bgr=0 rotation=0 width=240 height=320")
        command("i", "threshold=600")
        start = len(lines)
        reset(); read_for(9)
        validate_boot(lines[start:])
        if a.leave_counts is not None:
            w, wait, done = a.leave_counts
            command(f"o {w} {wait} {done}", f"ok overlay working={w} waiting={wait} unseen={done}")
            final = read_for(62)  # exercise host timeout; OFF marks the retained snapshot
            if any(line.startswith(("rst:", "err ")) or any(marker in line for marker in
                   ("desk-buddy lump ready:", *CRASH_MARKERS)) for line in final):
                raise RuntimeError("reboot or error after final counter command")
            print(f"PASS: final counters WORK={w} WAIT={wait} DONE={done}; "
                  "idle/offline snapshot, Tangerine, default panel; closing without reset.", flush=True)
        else:
            print("PASS: normal-mode ? verified; final reset boot validated, no commands since reset. "
                  "Board left offline/idle, Tangerine, zero counters, default panel; offline plug visible after 8s.", flush=True)
        normal_reset = True
    finally:
        if not normal_reset:
            reset()  # clear demo/tuning on failures too, including a failed soak
        ser.dtr = False; ser.rts = False
        ser.close()


if __name__ == "__main__":
    main()
