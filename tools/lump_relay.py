"""Lump relay: Lump plugged into this computer, showing the Claude sessions of another one.

The bridge (tools/buddy_bridge.py) runs on the computer where Claude runs. This relay runs on the computer Lump
is plugged into by USB, dials out to that bridge over Tailscale, and passes lines between the two:

  bridge -> board   the bridge's commands (mood, colour, counters, heartbeat ...), but only the kinds the
                    bridge needs: one line each, starting with s e p o c h T i or ?, at most 240 characters,
                    no control characters. Anything else (above all "w", which would rewrite the board's Wi-Fi
                    details) is dropped and answered with "err relay refused".
  board -> bridge   what the board prints (taps, gags, animations, its boot line), plus "relay board 1" or "relay board 0"
                    every 10 s and whenever he's plugged in or unplugged, so the bridge knows he's here and
                    that this computer is awake.

It never listens on any port: it only connects out, to the bridge's Tailscale address (it checks the name
resolves to one, 100.64.0.0/10 or fd7a:115c:a1e0::/48, before sending anything), and pairs with the bridge's
relay token. It finds the board by its USB-serial chip (WCH CH343) on Windows, /dev/cu.usbmodem* elsewhere,
opens it without resetting it, and reconnects to the board and to the bridge on its own, each independently,
waiting 3 s and then longer (up to 30 s) while it keeps failing.

Settings: ~/.desk-buddy/relay.json  {"bridge_host": "<the bridge PC's MagicDNS name>", "port": 4211,
"token": "<the bridge's relay_token>"}, written by tools/windows/install.ps1 -Relay. Log: ~/.desk-buddy/relay.log
(the token is never written to it). Needs pyserial.
"""
import ipaddress, json, logging, logging.handlers, os, re, select, socket, sys, threading, time

IS_WIN = sys.platform == "win32"
HOME = os.environ.get("DESKBUDDY_HOME") or os.path.expanduser("~/.desk-buddy")   # tests point this elsewhere
CONFIG_FILE = os.path.join(HOME, "relay.json")
LOG_FILE = os.path.join(HOME, "relay.log")
DEFAULT_PORT = 4211
USB_ID = (0x1A86, 0x55D3)               # the board's WCH CH343 USB-serial chip
LINE_MAX = 240                          # the firmware's own line limit
TO_BRIDGE_MAX = 250                     # board lines passed on to the bridge (which takes at most 256)
ALLOWED = frozenset(b"sepochTi?")       # first characters of the commands the bridge may send through
SPACE = b" \t\n\v\f\r"                  # what the firmware trims off a line (Arduino String::trim)
TAILNET = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
TOKEN_RE = re.compile(r"[0-9a-fA-F]{32}")

log = logging.getLogger("relay")
log.setLevel(logging.INFO)


class RelayError(Exception):
    pass


# ---------- what may pass ----------
def board_command(raw):
    """A line from the bridge as the board would take it (trimmed the way the firmware trims), if it's one the
    relay lets through; otherwise None. raw is one line, already split on CR and LF."""
    if raw is None or len(raw) > LINE_MAX:
        return None
    line = raw.strip(SPACE)
    if not line or line[0] not in ALLOWED:
        return None
    if any(c < 0x20 or c > 0x7E for c in line):          # control characters (tab, NUL, DEL ...) or non-ASCII
        return None
    return line


def take_lines(buf, skipping, cap=LINE_MAX):
    """Split buffered bytes into lines on CR or LF (as the firmware does). Returns (lines, rest, skipping); a
    line that grows past cap without ending is thrown away up to its end and shows up as None."""
    out = []
    while True:
        ends = [i for i in (buf.find(b"\r"), buf.find(b"\n")) if i >= 0]
        cut = min(ends) if ends else -1
        if skipping:
            if cut < 0:
                return out, b"", True
            buf, skipping = buf[cut + 1:], False
            continue
        if cut < 0:
            if len(buf) > cap:
                out.append(None)
                return out, b"", True
            return out, buf, False
        out.append(buf[:cut])
        buf = buf[cut + 1:]


def tailnet_addresses(host, port, resolve=socket.getaddrinfo):
    """The Tailscale addresses host resolves to, as (family, sockaddr). Nothing else is ever connected to:
    the token must only travel inside the tailnet."""
    out = []
    for fam, _, _, _, sa in resolve(host, port, 0, socket.SOCK_STREAM):
        try:
            ip = ipaddress.ip_address(str(sa[0]).split("%")[0])
        except ValueError:
            continue
        if any(ip.version == net.version and ip in net for net in TAILNET):
            out.append((fam, sa))
    if not out:
        raise RelayError(f"{host} isn't a Tailscale address (100.64.0.0/10 or fd7a:115c:a1e0::/48): not connecting")
    return out


# ---------- settings and the board ----------
def read_settings(path=None):
    path = path or CONFIG_FILE
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise RelayError(f"no {path}: set the relay up with tools/windows/install.ps1 -Relay")
    except ValueError as e:
        raise RelayError(f"{path} isn't valid JSON: {e}")
    if not isinstance(cfg, dict):
        raise RelayError(f"{path} isn't a JSON object")
    host = str(cfg.get("bridge_host") or "").strip()
    token = str(cfg.get("token") or "").strip()
    try:
        port = int(cfg.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        port = 0
    if not host:
        raise RelayError(f"no bridge_host in {path}")
    if not 0 < port < 65536:
        raise RelayError(f"bad port in {path}")
    if not TOKEN_RE.fullmatch(token):
        raise RelayError(f"the token in {path} is missing or isn't 32 hex characters")
    return {"bridge_host": host, "port": port, "token": token}


def find_board():
    """The board's serial port: on Windows only the CH343 (never just the first COM port)."""
    if IS_WIN:
        from serial.tools import list_ports
        ports = sorted(p.device for p in list_ports.comports() if (p.vid, p.pid) == USB_ID)
    else:
        import glob
        ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


def open_board(port):
    import serial
    s = serial.Serial(); s.port, s.baudrate, s.timeout = port, 115200, 0.3
    s.dtr = False; s.rts = False          # don't reset the board when opening
    s.open()
    return s


def connect_tcp(family, sockaddr):
    s = socket.socket(family, socket.SOCK_STREAM)
    try:
        s.settimeout(10)
        s.connect(sockaddr)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        s.close(); raise
    return s


# ---------- the relay ----------
class Relay:
    """Two loops, each reconnecting on its own: the board (a thread) and the bridge (the caller's thread)."""

    HEARTBEAT = 10                      # "relay board 0|1" this often (the bridge drops a relay silent for 30 s)
    BACKOFF = (3, 30)                   # first wait after a failure, and the longest
    BOARD_POLL = 3                      # no board plugged in: look again this often
    PAIR_SECS = 5
    SEND_TIMEOUT = 2

    def __init__(self, settings=read_settings, find=find_board, open_=open_board, resolve=socket.getaddrinfo,
                 connect=connect_tcp):
        self.settings, self.find, self.open, self.resolve, self.connect = settings, find, open_, resolve, connect
        self.board, self.sock = None, None
        self.send_lock = threading.Lock()
        self.stopping = threading.Event()
        self._said, self._refused_at, self._refused_quiet = {}, 0.0, 0

    def run(self):
        threading.Thread(target=self.board_loop, daemon=True).start()
        self.bridge_loop()

    def stop(self):
        self.stopping.set()
        for thing in (self.sock, self.board):
            try: thing.close()
            except Exception: pass

    def _once(self, key, msg):
        """Log msg unless it's what was last said under key (a missing board or bridge is retried for hours)."""
        if msg is not None and self._said.get(key) != msg:
            log.info("%s", msg)
        self._said[key] = msg

    # -- the board
    def board_loop(self):
        delay = self.BACKOFF[0]
        while not self.stopping.is_set():
            try:
                port = self.find()
            except Exception as e:
                port = None
                self._once("board", f"can't list serial ports: {e}")
            if not port:
                self._once("board", "no board plugged in")
                self.stopping.wait(self.BOARD_POLL); continue
            try:
                b = self.open(port)
            except Exception as e:
                self._once("board", f"can't open {port}: {e}")
                self.stopping.wait(delay); delay = min(delay * 2, self.BACKOFF[1]); continue
            delay = self.BACKOFF[0]
            self._once("board", f"board connected on {port}")
            self._set_board(b)
            try:
                self._pump_board(b)
            except Exception as e:
                self._once("board", f"board lost: {e}")
            finally:
                self._set_board(None)
                try: b.close()
                except Exception: pass
            self.stopping.wait(self.BACKOFF[0])

    def _pump_board(self, b):
        buf = b""
        while not self.stopping.is_set():
            buf += b.read(256)
            lines, buf, _ = take_lines(buf, False, cap=4096)
            for line in lines:
                line = (line or b"").strip(SPACE)
                if line and len(line) <= TO_BRIDGE_MAX:
                    self._to_bridge(line + b"\n")

    def _set_board(self, b):
        self.board = b
        self._status()                   # tell the bridge straight away

    def _to_board(self, line):
        b = self.board
        if b is None:
            return
        try:
            b.write(line + b"\n")
        except Exception:
            pass                         # the board loop notices it's gone

    # -- the bridge
    def _status(self):
        self._to_bridge(b"relay board %d\n" % (1 if self.board is not None else 0))

    def _to_bridge(self, data):
        sock = self.sock
        if sock is None:
            return False
        with self.send_lock:
            try:
                sock.sendall(data)
                return True
            except OSError:
                try: sock.shutdown(socket.SHUT_RDWR)       # the bridge loop then sees it's gone
                except OSError: pass
                return False

    def dial(self):
        """Connect to the bridge (Tailscale addresses only) and pair. Returns (socket, bytes after "ok paired")."""
        cfg = self.settings()
        addrs = tailnet_addresses(cfg["bridge_host"], cfg["port"], self.resolve)
        sock, last = None, None
        for fam, sa in addrs:
            try:
                sock = self.connect(fam, sa); break
            except OSError as e:
                last = e
        if sock is None:
            raise RelayError(f"can't connect to {cfg['bridge_host']}:{cfg['port']}: {last}")
        try:
            sock.settimeout(self.PAIR_SECS)
            sock.sendall(b"k " + cfg["token"].encode("ascii") + b"\n")
            deadline, buf = time.monotonic() + self.PAIR_SECS, b""
            while b"\n" not in buf:
                left = deadline - time.monotonic()
                if left <= 0 or len(buf) > 256:
                    raise RelayError("the bridge didn't answer the pairing")
                ready, _, _ = select.select([sock], [], [], min(left, 0.5))
                if ready:
                    chunk = sock.recv(256)
                    if not chunk:
                        raise RelayError("the bridge closed the connection while pairing (wrong token?)")
                    buf += chunk
            reply, rest = buf.split(b"\n", 1)
            reply = reply.strip(SPACE)
            if reply == b"err pairing":
                raise RelayError("the bridge refused our token: put the current one in relay.json (install.ps1 -Relay)")
            if reply != b"ok paired":
                raise RelayError("the bridge didn't answer the pairing")
            return sock, rest
        except Exception:
            try: sock.close()
            except OSError: pass
            raise

    def bridge_loop(self):
        delay = self.BACKOFF[0]
        while not self.stopping.is_set():
            try:
                sock, rest = self.dial()
            except Exception as e:
                self._once("bridge", f"can't reach the bridge: {e}")
                self.stopping.wait(delay); delay = min(delay * 2, self.BACKOFF[1]); continue
            delay = self.BACKOFF[0]
            self._once("bridge", "paired with the bridge")
            try:
                self.serve(sock, rest)
            except Exception as e:
                self._once("bridge", f"lost the bridge: {e}")
            finally:
                self.sock = None
                try: sock.close()
                except OSError: pass
            self.stopping.wait(self.BACKOFF[0])

    def serve(self, sock, rest=b""):
        """Pump lines from the bridge to the board (allowed ones only) until the connection goes."""
        sock.settimeout(self.SEND_TIMEOUT)
        self.sock = sock
        self._status()
        next_beat = time.monotonic() + self.HEARTBEAT
        buf, skipping = b"", False
        pending = rest
        while not self.stopping.is_set():
            if pending:
                data, pending = pending, b""
            else:
                try:
                    ready, _, _ = select.select([sock], [], [], min(0.5, self.HEARTBEAT))
                    data = sock.recv(4096) if ready else None
                except (OSError, ValueError):
                    if self.stopping.is_set():
                        return                           # stop() closed it
                    raise
                if data == b"":
                    raise RelayError("the bridge closed the connection")
            if data:
                buf += data
                lines, buf, skipping = take_lines(buf, skipping)
                for raw in lines:
                    self._from_bridge(raw)
            if time.monotonic() >= next_beat:
                next_beat = time.monotonic() + self.HEARTBEAT
                self._status()
            if self.sock is not sock:
                return

    def _from_bridge(self, raw):
        cmd = board_command(raw)
        if cmd is not None:
            self._to_board(cmd); return
        if raw is not None and not raw.strip(SPACE):
            return                                       # an empty line: the board ignores those too
        self._to_bridge(b"err relay refused\n")
        now = time.monotonic()                           # log it (never its content), at most once a second
        if now - self._refused_at < 1:
            self._refused_quiet += 1; return
        first = "an overlong line" if raw is None or len(raw) > LINE_MAX else "a line starting %r" % chr(raw.strip(SPACE)[0])
        log.info("refused %s from the bridge%s", first, " (and %d more)" % self._refused_quiet if self._refused_quiet else "")
        self._refused_at, self._refused_quiet = now, 0


def setup_logging():
    os.makedirs(HOME, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=200_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(h)
    if sys.stderr and sys.stderr.isatty():         # under pythonw there's no stderr
        log.addHandler(logging.StreamHandler())


def main():
    setup_logging()
    log.info("lump relay starting")
    while True:
        try:
            Relay().run()
        except Exception:
            log.exception("relay failed: starting again")
            time.sleep(5)


if __name__ == "__main__":
    main()
