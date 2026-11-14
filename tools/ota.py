"""Update Lump over Wi-Fi: his program, his drawings, or both. Sends only what changed.

    ~/.desk-buddy/venv/bin/python tools/ota.py            # build, then send whatever differs
    ~/.desk-buddy/venv/bin/python tools/ota.py --art      # just the drawings (firmware/data/lump.bin)
    ~/.desk-buddy/venv/bin/python tools/ota.py --app      # just the program (builds it first)
    options: --force (send even if unchanged)  --no-build  --host <name or IP>

It asks the board for SHA-256 hashes of what it's running (GET /info), compares them with the
local files, and POSTs the ones that differ; the board checks each upload's hash, shows a progress
bar, and restarts. Uses the pairing token and host from ~/.desk-buddy/config.json (written by
tools/wifi_setup.py). A cable is only needed for a new partition layout or recovery: tools/flash.sh.
"""
from contextlib import nullcontext
import argparse, hashlib, json, os, socket, subprocess, sys, time, urllib.error, urllib.request
from bridge_host import paused_bridge

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "firmware", ".pio", "build", "buddy", "firmware.bin")
ART = os.path.join(ROOT, "firmware", "data", "lump.bin")
CONFIG = os.path.expanduser("~/.desk-buddy/config.json")


def resolve(host):
    # IPv4 only: a general lookup of a .local name waits 5 s for an IPv6 answer the board never gives
    return socket.getaddrinfo(host, 80, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]


def request(ip, token, path, data=None, headers=None, timeout=10):
    h = {"X-Lump-Token": token, **(headers or {})}
    req = urllib.request.Request(f"http://{ip}{path}", data=data, headers=h, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def info(ip, token, app_len, art_len, timeout=20):
    st, body = request(ip, token, f"/info?app_len={app_len}&art_len={art_len}", timeout=timeout)
    if st == 401:
        sys.exit("Lump refused the pairing token: run tools/wifi_setup.py again.")
    if st != 200:
        sys.exit(f"/info failed ({st}): {body.strip()}")
    return json.loads(body)


def wait_back(ip, token, secs=60):
    """After a restart: wait until he answers again (and has rejoined Wi-Fi)."""
    end = time.time() + secs
    time.sleep(3)
    while time.time() < end:
        try:
            return info(ip, token, 0, 0, timeout=3)
        except (OSError, urllib.error.URLError):
            time.sleep(1.5)
    sys.exit("He didn't come back on Wi-Fi within a minute. Check the glass; a cable flash (tools/flash.sh) can recover him.")


def send(ip, token, kind, path, extra):
    data = open(path, "rb").read()
    mb = len(data) / 1e6
    print(f"sending {kind}: {os.path.relpath(path, ROOT)} ({mb:.1f} MB)...", flush=True)
    t = time.time()
    st, body = request(ip, token, f"/update/{kind}", data=data, timeout=600,
                       headers={"Content-Type": "application/octet-stream", "X-Lump-Size": str(len(data)), **extra})
    if st != 200:
        sys.exit(f"{kind} update failed ({st}): {body.strip()}")
    print(f"  done in {time.time() - t:.0f} s ({mb * 8 / max(time.time() - t, 0.1):.1f} Mbit/s); he's restarting", flush=True)


def update():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", action="store_true", help="only the program")
    ap.add_argument("--art", action="store_true", help="only the drawings")
    ap.add_argument("--force", action="store_true", help="send even if the board already has it")
    ap.add_argument("--no-build", action="store_true", help="don't run pio before sending the program")
    ap.add_argument("--host", help="board address (default: lump_host from config.json, else lump.local)")
    ap.add_argument("--env", choices=("buddy", "esp32-32e"), default="buddy", help="PlatformIO board environment")
    a = ap.parse_args()
    if a.env == "esp32-32e":
        sys.exit("ESP32-32E uses a single program slot and USB updates: tools/flash.sh esp32-32e")
    want_app, want_art = (a.app or not a.art), (a.art or not a.app)

    cfg = json.load(open(CONFIG)) if os.path.exists(CONFIG) else {}
    token = cfg.get("lump_token")
    if not token:
        sys.exit("No pairing token in ~/.desk-buddy/config.json: run tools/wifi_setup.py first (needs the USB cable once).")
    host = a.host or cfg.get("lump_host") or "lump.local"

    if want_app and not a.no_build:
        print("building...", flush=True)
        r = subprocess.run(["pio", "run", "-e", a.env], cwd=os.path.join(ROOT, "firmware"), capture_output=True, text=True)
        if r.returncode:
            sys.exit(r.stdout[-3000:] + r.stderr[-2000:] + "\nbuild failed")
    for kind, path, want in (("app", APP, want_app), ("art", ART, want_art)):
        if want and not os.path.exists(path):
            sys.exit(f"missing {path}")

    ip = resolve(host)
    app = open(APP, "rb").read() if want_app else b""
    art = open(ART, "rb").read() if want_art else b""
    have = info(ip, token, len(app), len(art), timeout=30)       # hashing ~10 MB of flash takes a moment
    print(f"Lump at {ip}: running {have.get('running')}, drawings {'ok' if have.get('art_ok') else 'MISSING/BAD'}")

    todo = []
    if want_art:
        if len(art) > have.get("art_room", 0):
            sys.exit(f"lump.bin is {len(art)} bytes but the art partition holds {have.get('art_room')}")
        if a.force or hashlib.sha256(art).hexdigest() != have.get("art_sha"):
            todo.append(("art", ART, {"X-Lump-Sha256": hashlib.sha256(art).hexdigest()}))
        else:
            print("drawings: already up to date")
    if want_app:
        if len(app) > have.get("app_room", 0):
            sys.exit(f"firmware.bin is {len(app)} bytes but a program slot holds {have.get('app_room')}")
        if a.force or hashlib.sha256(app).hexdigest() != have.get("app_sha"):
            todo.append(("app", APP, {"X-Lump-MD5": hashlib.md5(app).hexdigest()}))
        else:
            print("program: already up to date")

    if todo:
        # macOS OTA historically leaves launchd alone; only Linux needs the new service guard.
        guard = paused_bridge() if sys.platform.startswith("linux") else nullcontext()
        with guard:
            for kind, path, extra in todo:
                send(ip, token, kind, path, extra)
                back = wait_back(ip, token)
                print(f"  back: running {back.get('running')}, drawings {'ok' if back.get('art_ok') else 'MISSING/BAD'}")
        print("Lump is up to date.")


def main():
    try:
        update()
    except (OSError, urllib.error.URLError) as e:
        sys.exit(f"OTA connection failed: {e}. Check the board address and Wi-Fi. "
                 "ESP32-32E has USB updates only: tools/flash.sh esp32-32e")


if __name__ == "__main__":
    main()
