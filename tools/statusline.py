"""Claude Code status line for Desk Buddy (Windows; the Mac uses statusline.sh, which does the same).

Claude Code runs this after each reply with session JSON on stdin; it includes your plan's usage limits
(rate_limits.five_hour / seven_day: used_percentage, resets_at). This forwards that JSON to the bridge
(which turns it into Lump's usage-gauge colour) and prints a short status line. Standard library only,
so any Python runs it. Installed copy: ~/.desk-buddy/statusline.py (referenced from settings.json).
"""
import json, math, os, sys, threading, urllib.request

URL = os.environ.get("DESKBUDDY_USAGE_URL") or "http://127.0.0.1:47823/usage"     # tests point it elsewhere


def forward(raw):
    try:
        req = urllib.request.Request(URL, data=raw, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=1).close()
    except Exception:
        pass                                  # bridge not running: the status line still prints


def line(raw):
    """The same "model · 5h N% · week N%" line statusline.sh prints."""
    try:
        d = json.loads(raw.decode("utf-8"))
    except Exception:
        return ""
    parts = []
    name = (d.get("model") or {}).get("display_name") if isinstance(d.get("model"), dict) else None
    if name:
        parts.append(str(name))
    rl = d.get("rate_limits") if isinstance(d.get("rate_limits"), dict) else {}
    for key, label in (("five_hour", "5h"), ("seven_day", "week")):
        w = rl.get(key)
        pct = w.get("used_percentage") if isinstance(w, dict) else None
        if isinstance(pct, (int, float)):
            parts.append(f"{label} {math.floor(pct)}%")
    return " · ".join(parts)


def main():
    raw = sys.stdin.buffer.read()
    t = threading.Thread(target=forward, args=(raw,), daemon=True)
    t.start()
    sys.stdout.buffer.write((line(raw) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    t.join(1.5)


if __name__ == "__main__":
    main()
