"""Keeps the Desk Buddy bridge running on Windows: what launchd's KeepAlive does on the Mac.

tools/windows/install.ps1 copies this to ~/.desk-buddy/keepalive.py and starts it at logon (a shortcut in
your Startup folder) with the runtime's pythonw.exe, so there's no console window. It runs bridge.py and
starts it again if it ever exits. Crash output goes to ~/.desk-buddy/bridge.out; the bridge's own log is
bridge.log. If something already answers on the bridge's port (another copy, or one you started by hand),
it waits instead of starting a second one.
"""
import os, socket, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "bridge.py")
OUT = os.path.join(HERE, "bridge.out")
PORT = 47823
NO_WINDOW = 0x08000000                      # CREATE_NO_WINDOW


def bridge_answering():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            return True
    except OSError:
        return False


def main():
    while True:
        if bridge_answering():
            time.sleep(30); continue
        try:
            if os.path.getsize(OUT) > 1_000_000:
                os.replace(OUT, OUT + ".1")
        except OSError:
            pass
        started = time.time()
        with open(OUT, "ab") as out:
            out.write(time.strftime("\n--- %Y-%m-%d %H:%M:%S starting the bridge\n").encode())
            out.flush()
            subprocess.call([sys.executable, "-u", BRIDGE], cwd=HERE, stdout=out, stderr=out, creationflags=NO_WINDOW)
        time.sleep(5 if time.time() - started > 60 else 30)     # crashing straight away: back off


if __name__ == "__main__":
    main()
