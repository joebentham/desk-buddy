"""Put Lump on your Wi-Fi.

Run it yourself with the board plugged into the computer by USB (data cable):
    ~/.desk-buddy/venv/bin/python tools/wifi_setup.py          # set up / change network
    ~/.desk-buddy/venv/bin/python tools/wifi_setup.py --forget # turn Wi-Fi off again

It asks for the network name and password (the password isn't shown, logged or saved on the host:
it goes straight over USB into the board's own flash), makes a pairing token so only your
bridge can drive him over the network, and tells him your time zone so he can keep time from
the internet. The token goes into ~/.desk-buddy/config.json for the bridge.
The ESP32 only does 2.4 GHz Wi-Fi.
PORT overrides DESKBUDDY_PORT; without a USB override, discover local USB ports.
"""
import getpass, json, os, re, secrets, subprocess, sys, time

import serial
from bridge_host import paused_bridge, probe_lump, usb_ports

HOME = os.path.expanduser("~/.desk-buddy")
CONFIG = os.path.join(HOME, "config.json")


def posix_tz():
    """Your time zone as a POSIX rule (e.g. EST5EDT,M3.2.0,M11.1.0), from the footer of
    /etc/localtime, which is what the ESP32's clock understands."""
    try:
        data = open("/etc/localtime", "rb").read()
        footer = data.rstrip(b"\n").rsplit(b"\n", 1)[-1].decode()
        return footer if re.fullmatch(r"[A-Za-z<>+\-0-9,./:]+", footer) else ""
    except Exception:
        return ""


def current_ssid():
    if sys.platform != "darwin":
        return ""                    # Linux: prompt explicitly; no dependency on NetworkManager
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


def main():
    forget = "--forget" in sys.argv
    port = os.environ.get("PORT") or os.environ.get("DESKBUDDY_PORT")
    ports = [port] if port and not port.startswith("socket://") else usb_ports()
    if not ports:
        sys.exit("Lump isn't plugged in by USB (needs a data cable).")
    cfg = {}
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
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
    with paused_bridge():
        time.sleep(1)
        for port in ports:
            ser = serial.Serial(); ser.port, ser.baudrate, ser.timeout = port, 115200, 0.3
            ser.dtr = False; ser.rts = False
            try:
                try:
                    ser.open()
                    if not probe_lump(ser):
                        continue
                except (OSError, serial.SerialException):
                    continue
                configure(ser, line, cfg, forget, None if forget else token)
                return
            finally:
                ser.close()          # release rejected ports and errors before resuming the bridge
        sys.exit("No verified Lump USB port found. Is the new firmware flashed (tools/flash.sh)?")


def configure(ser, line, cfg, forget, token):
    """Configure the USB link after main has verified the Lump state reply."""
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
    with open(CONFIG + ".tmp", "w") as f:
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
    if status and "wifi=connected" in status:
        print("\nLump is on Wi-Fi. Unplug him from the computer and power him from any USB charger:")
        print("the bridge will find him at lump.local within a few seconds.")
    else:
        print("\nHe saved the details but hasn't joined yet. Check the network name and password")
        print("(and that it's a 2.4 GHz network), then run this again.")


if __name__ == "__main__":
    main()
