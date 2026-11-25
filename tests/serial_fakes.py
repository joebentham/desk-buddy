"""In-memory USB and clock doubles; these never open a device or wait in real time."""
from pathlib import Path
import re

STATE = b"ok mode=direct target=idle playing=idle(loop) fps=24.0 frames=300 seqs=20\r\n"
# Use the production reply and boot banner for both missing and invalid art.
_MAIN_CPP = (Path(__file__).resolve().parents[1] / "firmware/src/main.cpp").read_bytes()
MISSING_ART_STATE = re.search(
    rb'static void reply\(\)\s*\{\s*if \(!A\.ok\) \{ say\("([^"]+)"\);', _MAIN_CPP
)[1] + b"\r\n"
_RECOVERY_COMMAND = re.search(rb'ART_RECOVERY_COMMAND = "([^"]+)";', _MAIN_CPP)[1]
MISSING_ART_BOOT = re.search(
    rb'if \(!mapArt\(\) \|\| !parseAnim\(\)\) \{\s*A\.ok = false;\s*'
    rb'Serial\.printf\("([^"]+)", ART_RECOVERY_COMMAND\);', _MAIN_CPP
)[1].replace(b"%s", _RECOVERY_COMMAND).replace(rb"\n", b"\r\n")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class USB:
    def __init__(self, clock, *, boot=0.0, response=STATE, noise=False):
        self.clock, self.boot, self.response, self.noise = clock, boot, response, noise
        self.timeout, self.write_timeout = 0.3, None
        self.port, self.baudrate, self.dtr, self.rts = None, None, True, True
        self.writes, self.pending = [], [b"ets Jun  8 2016\r\nrst:0x1\r\n", b"desk-buddy lump ready\n"]
        self.opened = self.closed = False

    def open(self):
        self.opened = True

    def close(self):
        self.closed = True

    def write(self, data):
        self.writes.append(data)
        if data == b"?\n" and self.clock() >= self.boot:
            self.pending.extend([self.response[:17], self.response[17:]])
        return len(data)

    def read(self, size):
        self.clock.now += min(self.timeout, 0.1)
        if self.pending:
            return self.pending.pop(0)
        return b"x" * size if self.noise else b""

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.pending.clear()
