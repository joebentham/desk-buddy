"""Host serial discovery, protocol verification and bridge service control."""
from contextlib import contextmanager
import glob
import os
import re
import signal
import subprocess
import sys
import time

SERVICE = "deskbuddy-bridge.service"
LABEL = "com.deskbuddy.bridge"
PROBE_SECONDS = 3.0


def usb_ports():
    """S3 modem paths first on macOS; stable WCH paths first on Linux.

    pyserial reads USB metadata from sysfs, without opening the serial devices. udev's
    by-id names also identify WCH chips when USB metadata is unavailable. Linux
    aliases are deduplicated so each physical port is tried once.
    """
    if sys.platform == "darwin":
        return [path for pattern in ("/dev/cu.usbmodem*", "/dev/cu.usbserial-*", "/dev/cu.wchusbserial*")
                for path in sorted(glob.glob(pattern))]
    if not sys.platform.startswith("linux"):
        return []

    from serial.tools import list_ports
    try:
        wch = {os.path.realpath(p.device) for p in list_ports.comports()
               if p.vid == 0x1A86 or "CH343" in (p.description or "").upper()}
    except OSError:
        wch = set()
    stable = sorted(glob.glob("/dev/serial/by-id/*"))
    paths = stable + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")

    def priority(path):
        name = os.path.basename(path).lower()
        preferred = os.path.realpath(path) in wch or any(s in name for s in ("1a86", "ch340", "ch341", "ch343"))
        return (not preferred, path not in stable, path)

    seen, result = set(), []
    for path in sorted(paths, key=priority):
        device = os.path.realpath(path)
        if device not in seen:
            result.append(path)
            seen.add(device)
    return result


def usb_identity(path):
    """Use USB metadata equally for raw tty paths and udev aliases.

    CH340 adapters often have no unique serial number. Their location identifies
    the socket, not the board; with no metadata remember the resolved tty path.
    Every reopened port must still pass the protocol probe.
    """
    device = os.path.realpath(path)
    from serial.tools import list_ports
    try:
        for port in list_ports.comports():
            if os.path.realpath(port.device) == device:
                for field in ("serial_number", "location"):
                    value = getattr(port, field, None)
                    if value:
                        return (field, port.vid, port.pid, value)
    except OSError:
        pass
    return ("path", device)


def probe_lump(ser):
    """Ask for the existing '?' state reply, allowing at most 3 s for CH340 reset.

    DTR/RTS being low does not prevent every adapter's open-time reset. Retry
    through ROM/boot banners and partial lines; neither a banner nor a generic
    'ok' identifies Lump. Bound reads, writes and buffered noise. Do not flush:
    serial tcdrain can block without a timeout on a misbehaving adapter.
    """
    deadline, next_query = time.monotonic() + PROBE_SECONDS, 0.0
    timeout, write_timeout = ser.timeout, ser.write_timeout
    buf = b""
    try:
        while (remaining := deadline - time.monotonic()) > 0:
            if time.monotonic() >= next_query:
                ser.write_timeout = min(0.1, remaining)
                ser.write(b"?\n")
                next_query = time.monotonic() + 0.3
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ser.timeout = min(0.1, remaining)
            buf += ser.read(256)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                # reply() uses this exact error for both missing and invalid art.
                if line == b"err lump art missing or bad":
                    return True
                if re.fullmatch(rb"ok mode=(?:demo|offline|direct) target=\S* "
                                rb"playing=[^()\s]*\((?:loop|transition|event)\) "
                                rb"fps=\d+\.\d frames=\d+ seqs=\d+", line):
                    return True
            buf = buf[-1024:]
        return False
    finally:
        ser.timeout, ser.write_timeout = timeout, write_timeout


def linux_bridge_intent():
    """Return 'run', 'stop', or None (already stopped/absent).

    Read unit properties together. A job may complete before its type is read:
    take one fresh snapshot in that case, then abort if intent is still unclear.
    A queued stop takes precedence over the unit's current running state.
    """
    error = f"Cannot safely pause {SERVICE}: systemd state is unreadable or ambiguous; refusing to use the board."
    for attempt in range(2):
        try:
            state = subprocess.run(
                ["systemctl", "--user", "show", "--property=LoadState,ActiveState,SubState,Job", "--all", SERVICE],
                capture_output=True, text=True)
        except FileNotFoundError:
            if attempt == 0:            # no systemctl installed
                return None
            raise RuntimeError(error) from None
        except OSError as exc:
            raise RuntimeError(error) from exc
        if state.returncode:
            raise RuntimeError(error)
        props = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
        if (props.get("LoadState") not in ("loaded", "not-found") or not props.get("SubState") or "Job" not in props or
                props.get("ActiveState") not in ("active", "reloading", "activating", "deactivating", "inactive", "failed")):
            raise RuntimeError(error)
        # systemctl can render an empty Job= when no job is pending.
        job = (props["Job"].split() or ["0"])[0]
        if not job.isdecimal():
            raise RuntimeError(error)
        if props["LoadState"] == "not-found":
            if (props["ActiveState"], props["SubState"], job) == ("inactive", "dead", "0"):
                return None
            raise RuntimeError(error)
        if job != "0":
            try:
                pending = subprocess.run(["systemctl", "--user", "show", "--property=JobType", "--value", job],
                                         capture_output=True, text=True)
            except OSError:
                continue
            if pending.returncode:
                continue              # the job may have disappeared; never treat that as stopped
            job_type = pending.stdout.strip()
            if job_type == "stop":
                return "stop"
            if job_type in ("start", "restart", "try-restart", "reload-or-start", "reload"):
                return "run"
            raise RuntimeError(error)
        if (props["ActiveState"] in ("active", "reloading", "activating") or
                props["SubState"] in ("auto-restart", "auto-restart-queued")):
            return "run"
        if props["ActiveState"] == "deactivating":
            return "stop"
        if props["ActiveState"] in ("inactive", "failed") and props["SubState"] in ("dead", "failed"):
            return None
        raise RuntimeError(error)
    raise RuntimeError(error)


@contextmanager
def paused_bridge():
    """Release the board during setup/updates, restoring a running bridge on exit.

    A missing unit or stopped Linux service is left alone; unreadable state aborts.
    Linux entry points run in the main thread: TERM/HUP unwind like SystemExit,
    including during pause acquisition. Previous signal handlers are restored.
    Stop/start failures for an installed, running service are surfaced to the caller.
    OTA can use this module with only the standard library; pyserial is imported lazily.
    """
    resume, checked = None, False
    handlers, cleaning = {}, False

    def interrupted(number, frame):
        if not cleaning:                # subsequent TERM/HUP must let restoration finish
            raise SystemExit(128 + number)

    try:
        if sys.platform.startswith("linux"):
            for number in (signal.SIGTERM, signal.SIGHUP):
                handlers[number] = signal.getsignal(number)
                signal.signal(number, interrupted)
            intent = linux_bridge_intent()
            if intent is not None:
                if intent == "run":
                    # Arm restoration BEFORE a stop that may finish as we're interrupted.
                    resume, checked = ["systemctl", "--user", "start", SERVICE], True
                subprocess.run(["systemctl", "--user", "stop", SERVICE], capture_output=True, check=True)
        elif sys.platform == "darwin":
            plist = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
            if os.path.isfile(plist):
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
                resume = ["launchctl", "bootstrap", f"gui/{os.getuid()}", plist]
        yield
    finally:
        cleaning = True
        try:
            if resume:
                subprocess.run(resume, capture_output=True, check=checked)
        finally:
            for number, handler in handlers.items():
                signal.signal(number, handler)
