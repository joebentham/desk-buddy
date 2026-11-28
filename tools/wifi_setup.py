"""Put Lump on your Wi-Fi.

Run it yourself with the board plugged into the Mac by USB (data cable):
    ~/.desk-buddy/venv/bin/python tools/wifi_setup.py          # set up / change network
    ~/.desk-buddy/venv/bin/python tools/wifi_setup.py --forget # turn Wi-Fi off again
On Windows: ~/.desk-buddy/venv/Scripts/python tools/wifi_setup.py (same options).

It asks for the network name and password (the password isn't shown, logged or saved on the Mac:
it goes straight over USB into the board's own flash), makes a pairing token so only your
bridge can drive him over the network, and tells him your time zone so he can keep time from
the internet. The token goes into ~/.desk-buddy/config.json for the bridge.
The ESP32 only does 2.4 GHz Wi-Fi.
"""
import getpass, glob, json, os, re, secrets, subprocess, sys, time

import serial

IS_WIN = sys.platform == "win32"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~/.desk-buddy")
CONFIG = os.path.join(HOME, "config.json")
BRIDGE = "com.deskbuddy.bridge"
BRIDGE_PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{BRIDGE}.plist")


def _posix_offset(minutes_west):
    """POSIX offset text: hours west of UTC, with :mm when needed (e.g. 300 -> "5", -330 -> "-5:30")."""
    sign = "-" if minutes_west < 0 else ""
    h, m = divmod(abs(int(minutes_west)), 60)
    return f"{sign}{h}" + (f":{m:02d}" if m else "")


def posix_tz_from_windows(bias, std_bias, dst_bias, std_date, dst_date):
    """A POSIX rule from Windows' time zone details (TIME_ZONE_INFORMATION): biases are minutes west of
    UTC, dates are (month, week 1-5 where 5 = last, weekday 0 = Sunday, hour); month 0 means no DST."""
    rule = "STD" + _posix_offset(bias + std_bias)
    if dst_date[0] == 0 or std_date[0] == 0:
        return rule
    when = lambda d: f"M{d[0]}.{d[1]}.{d[2]}/{d[3]}"
    return rule + "DST" + _posix_offset(bias + dst_bias) + "," + when(dst_date) + "," + when(std_date)


def windows_posix_tz():
    """Windows: your time zone as a POSIX rule, from GetTimeZoneInformation."""
    try:
        import ctypes
        from ctypes import wintypes

        class SYSTEMTIME(ctypes.Structure):
            _fields_ = [(n, wintypes.WORD) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond", "wMilliseconds")]

        class TZI(ctypes.Structure):
            _fields_ = [("Bias", wintypes.LONG), ("StandardName", wintypes.WCHAR * 32), ("StandardDate", SYSTEMTIME),
                        ("StandardBias", wintypes.LONG), ("DaylightName", wintypes.WCHAR * 32), ("DaylightDate", SYSTEMTIME),
                        ("DaylightBias", wintypes.LONG)]

        tzi = TZI()
        if ctypes.windll.kernel32.GetTimeZoneInformation(ctypes.byref(tzi)) == 0xFFFFFFFF:
            return ""
        d = lambda t: (t.wMonth, t.wDay, t.wDayOfWeek, t.wHour)
        return posix_tz_from_windows(tzi.Bias, tzi.StandardBias, tzi.DaylightBias, d(tzi.StandardDate), d(tzi.DaylightDate))
    except Exception:
        return ""


def posix_tz():
    """Your time zone as a POSIX rule (e.g. EST5EDT,M3.2.0,M11.1.0), from the footer of
    /etc/localtime, which is what the ESP32's clock understands."""
    if IS_WIN:
        return windows_posix_tz()
    try:
        data = open("/etc/localtime", "rb").read()
        footer = data.rstrip(b"\n").rsplit(b"\n", 1)[-1].decode()
        return footer if re.fullmatch(r"[A-Za-z<>+\-0-9,./:]+", footer) else ""
    except Exception:
        return ""


def current_ssid():
    if IS_WIN:
        try:
            out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"^\s*SSID\s*: (.+)$", out, re.M)
            return m.group(1).strip() if m else ""
        except Exception:
            return ""
    try:
        out = subprocess.run(["ipconfig", "getsummary", "en0"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"^\s*SSID : (.+)$", out, re.M)
        return m.group(1).strip() if m and "redacted" not in m.group(1) else ""
    except Exception:
        return ""


def read_lines(ser, until, secs):
    """Print the board's lines until one contains `until` (or time runs out); return it."""
    end = time.time() + secs
    buf = b""
    while time.time() < end:
        buf += ser.read(256)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode(errors="replace").strip()
            if text and not text.startswith(("ok mode", "ESP-ROM", "Build:", "rst:", "SPIWP", "mode:", "load:", "entry")):
                print("  board:", text)
            if until in text:
                return text
    return None


def usb_ports():
    if IS_WIN:                                  # the board's USB-serial chip: WCH CH343
        from serial.tools import list_ports
        return sorted(p.device for p in list_ports.comports() if (p.vid, p.pid) == (0x1A86, 0x55D3))
    return sorted(glob.glob("/dev/cu.usbmodem*"))


def bridge_ctl(start):
    """Pause the bridge (it holds the USB port) or start it again."""
    if IS_WIN:
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        os.path.join(ROOT, "tools", "windows", "bridge.ps1"), "-Start" if start else "-Stop"], capture_output=True)
    elif start:
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", BRIDGE_PLIST], capture_output=True)
    else:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{BRIDGE}"], capture_output=True)


def main():
    forget = "--forget" in sys.argv
    ports = usb_ports()
    if not ports:
        sys.exit("Lump isn't plugged in by USB (needs a data cable).")
    cfg = {}
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            cfg = json.load(f)

    if forget:
        line = "w"
    else:
        default = current_ssid()
        ssid = input(f"Wi-Fi network name{f' [{default}]' if default else ''}: ").strip() or default
        if not ssid:
            sys.exit("No network name given.")
        password = getpass.getpass(f"Password for {ssid} (not shown): ")
        if "\t" in ssid + password or "\n" in ssid + password:
            sys.exit("Tabs and newlines can't be sent; sorry.")
        token = cfg.get("lump_token") or secrets.token_hex(12)
        tz = posix_tz()
        line = f"w {ssid}\t{password}\t{token}\t{tz}"

    # the bridge holds the USB port: pause it while we talk to the board
    bridge_ctl(start=False)
    time.sleep(1)
    try:
        ser = serial.Serial(); ser.port, ser.baudrate, ser.timeout = ports[0], 115200, 0.3
        ser.dtr = False; ser.rts = False
        ser.open()
        read_lines(ser, "lump ready", 4)          # opening the port may restart him: let him boot
        ser.reset_input_buffer()
        ser.write((line + "\n").encode()); ser.flush()
        del line                                    # don't keep the password around
        if not read_lines(ser, "restarting", 5):
            sys.exit("The board didn't confirm. Is the new firmware flashed (tools/flash.sh)?")
        if forget:
            cfg.pop("lump_token", None)
        else:
            cfg["lump_token"] = token
            cfg.setdefault("lump_host", "lump.local")
            os.makedirs(HOME, exist_ok=True)
        with open(CONFIG + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(CONFIG + ".tmp", CONFIG)
        if forget:
            print("Wi-Fi forgotten.")
            return
        read_lines(ser, "lump ready", 8)
        print("Joining the network...")
        status = None
        for _ in range(20):
            ser.write(b"W\n"); ser.flush()
            status = read_lines(ser, "ok wifi=", 2)
            if status and "wifi=connected" in status:
                break
            time.sleep(1)
        ser.close()
        if status and "wifi=connected" in status:
            print(f"\nLump is on Wi-Fi. Unplug him from the {'PC' if IS_WIN else 'Mac'} and power him from any USB charger:")
            print("the bridge will find him at lump.local within a few seconds.")
        else:
            print("\nHe saved the details but hasn't joined yet. Check the network name and password")
            print("(and that it's a 2.4 GHz network), then run this again.")
    finally:
        bridge_ctl(start=True)


if __name__ == "__main__":
    main()
