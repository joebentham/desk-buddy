"""Lump Control: a small Windows tray app to test Lump and change his settings.

A tray icon (right-click: open, play an animation, sound on/off, restart the bridge) and a window: on the
left Lump himself, live (his mood in his colour, from the board's own frames in lump.bin), with
his counters and usage; on the right Status (sessions), Play (gags, mood previews, tap sensitivity), Sounds
and Settings (colour, Wi-Fi host, calendar, Slack), in dark or light mode. It only talks to the bridge's own
HTTP API on 127.0.0.1:47823 and edits ~/.desk-buddy/config.json and sounds.json, which the bridge re-reads by
itself, so nothing here needs the bridge restarted. With the bridge stopped it says so and offers to start it.

The window is an HTML page (tools/windows/ui/) shown in Windows' own WebView2 through pywebview; the page
asks Python for everything through class Api. Without pywebview it falls back to the older Tk window
(ControlWindow), which `--tk` also forces.

tools/windows/install.ps1 installs it as ~/.desk-buddy/control.py and starts it at logon (pythonw, no
console). Run by hand: ~/.desk-buddy/venv/Scripts/pythonw tools/windows/control.py [--show]
Needs pywebview, pystray and Pillow (installed by install.ps1).
"""
import json, os, socket, subprocess, sys, threading, time, urllib.error, urllib.request

HOME = os.environ.get("DESKBUDDY_HOME") or os.path.expanduser("~/.desk-buddy")
CONFIG_FILE = os.path.join(HOME, "config.json")
SOUNDS_FILE = os.path.join(HOME, "sounds.json")
BRIDGE_URL = os.environ.get("DESKBUDDY_URL") or "http://127.0.0.1:47823"
SHOW_PORT = 47825                      # a second launch asks the running one to show its window
# Moods the board knows (the bridge sends these with "s <mood>").
MOODS = ("idle", "awake", "working", "alert", "waiting", "done", "longhaul", "night", "letter", "meeting")
REFRESH_MS = 2000


# ---------- talking to the bridge ----------
class Bridge:
    """The bridge's HTTP API. Every call returns None when the bridge isn't running."""

    def __init__(self, url=BRIDGE_URL, timeout=2):
        self.url, self.timeout = url, timeout

    def _call(self, path, body=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else body.encode("utf-8"),
                                     method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (OSError, ValueError):
            return None

    def status(self):
        r = self._call("/status")
        return json.loads(r[1]) if r and r[0] == 200 else None

    def animations(self):
        r = self._call("/play")
        return json.loads(r[1]) if r and r[0] == 200 else []

    def play(self, name):
        r = self._call("/play", name)
        return None if r is None else r[0] == 200

    def cmd(self, text):
        r = self._call("/cmd", text)
        return None if r is None else r[0] == 200

    def resync(self):
        r = self._call("/resync", "")
        return None if r is None else r[0] == 200

    def sound(self, text):
        r = self._call("/sound", text)
        return None if r is None else r[1]

    def calendar_now(self):
        return self._call("/calendar", "") is not None


# ---------- settings files ----------
def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_json(path, changes, remove=()):
    """Merge changes into a JSON file (keeping every other key) and write it atomically.
    Keys in `remove`, or with a value of None or "", are deleted."""
    data = read_json(path)
    for k, v in changes.items():
        if v is None or v == "":
            data.pop(k, None)
        else:
            data[k] = v
    for k in remove:
        data.pop(k, None)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)
    return data


def set_moment(path, moment, on):
    """Switch one of sounds.json's moments on or off, keeping its picks."""
    moments = dict(read_json(path).get("moments") or {})
    m = dict(moments.get(moment) or {})
    m["on"] = bool(on)
    moments[moment] = m
    return update_json(path, {"moments": moments})


# Swatches for his body colour (name, RRGGBB); "Pick…" offers any colour. Tangerine is his own default.
DEFAULT_BODY_COLOUR = "FF7A45"
BODY_COLOURS = (("Tangerine", "FF7A45"), ("Lemon", "FFC93C"), ("Mint", "3DD68C"), ("Sky", "4FA8FF"),
                ("Grape", "9B6BFF"), ("Bubblegum", "FF6FB5"), ("Cherry", "E8334A"))


def current_body_colour(path=None):
    c = str(read_json(path or CONFIG_FILE).get("body_colour") or "").strip().lstrip("#").upper()
    return c if len(c) == 6 and all(ch in "0123456789ABCDEF" for ch in c) else DEFAULT_BODY_COLOUR


def set_body_colour(path, hex_):
    """Save his everyday colour to config.json (None or Tangerine removes the setting: back to his default)."""
    h = (hex_ or "").strip().lstrip("#").upper()
    if h and not (len(h) == 6 and all(ch in "0123456789ABCDEF" for ch in h)):
        raise ValueError("color must be RRGGBB")
    return update_json(path, {"body_colour": None if h in ("", DEFAULT_BODY_COLOUR) else h})


def parse_vips(text):
    return [n.strip() for n in text.replace("\n", ",").split(",") if n.strip()]


def describe(status):
    """One line for the tray tooltip / status header."""
    if not status:
        return "Bridge not running"
    link = {"usb": "USB", "wifi": "Wi-Fi"}.get(status.get("link"), status.get("link") or "")
    board = f"board on {link}" if status.get("device") else "board not connected"
    return f"{status.get('mood', '?')} · {board}"


def bridge_ctl(action):
    """Start or stop the installed bridge (tools/windows/bridge.ps1, copied next to this file)."""
    script = os.path.join(HOME, "bridge.ps1")
    if not os.path.exists(script):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bridge.ps1")
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-" + action],
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---------- one copy at a time ----------
def already_running():
    """If Lump Control is already running, ask it to show its window and say so."""
    try:
        with socket.create_connection(("127.0.0.1", SHOW_PORT), timeout=1) as s:
            s.sendall(b"show\n")
        return True
    except OSError:
        return False


def listen_for_show(on_show):
    srv = socket.socket()
    srv.bind(("127.0.0.1", SHOW_PORT))
    srv.listen(2)

    def loop():
        while True:
            conn, _ = srv.accept()
            with conn:
                if conn.recv(16).startswith(b"show"):
                    on_show()
    threading.Thread(target=loop, daemon=True).start()
    return srv


# ---------- the window ----------
class ControlWindow:
    def __init__(self, root, bridge):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root, self.bridge = tk, ttk, root, bridge
        self.status = None
        root.title("Lump Control")
        root.geometry("560x600")
        root.minsize(480, 480)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        icon = os.path.join(HOME, "lump_icon.png")
        if os.path.exists(icon):
            try:
                big = tk.PhotoImage(file=icon)
                self._icon = big.subsample(max(1, big.width() // 64))
                root.iconphoto(True, self._icon)
            except tk.TclError:
                pass
        style = ttk.Style()
        style.configure("Big.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Note.TLabel", foreground="#666")

        self.header = ttk.Label(root, text="…", style="Big.TLabel", padding=(12, 10, 12, 0))
        self.header.pack(anchor="w")
        self.notice = ttk.Label(root, text="", style="Note.TLabel", padding=(12, 0, 12, 6))
        self.notice.pack(anchor="w")
        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        nb.enable_traversal()                  # Ctrl+Tab / Ctrl+Shift+Tab switch tabs
        self.build_status(nb)
        self.build_play(nb)
        self.build_sounds(nb)
        self.build_settings(nb)
        self.refresh()

    # --- helpers
    def say(self, text):
        self.notice.config(text=text)
        self.root.after(6000, lambda: self.notice.cget("text") == text and self.notice.config(text=""))

    def run(self, what, fn, *a):
        """Call the bridge off the UI thread; report the outcome."""
        def work():
            ok = fn(*a)
            msg = (f"{what}: done" if ok else f"{what}: the board isn't connected" if ok is False else
                   f"{what}: the bridge isn't running")
            self.root.after(0, self.say, msg)
        threading.Thread(target=work, daemon=True).start()

    def frame(self, nb, title):
        f = self.ttk.Frame(nb, padding=10)
        nb.add(f, text=title)
        return f

    # --- Status
    def build_status(self, nb):
        ttk, tk = self.ttk, self.tk
        f = self.frame(nb, "Status")
        top = ttk.Frame(f); top.pack(fill="x")
        self.swatch = tk.Canvas(top, width=36, height=36, highlightthickness=0)
        self.swatch.pack(side="left", padx=(0, 10))
        self.swatch_rect = self.swatch.create_oval(2, 2, 34, 34, fill="#FF7A45", outline="")
        self.facts = ttk.Label(top, text="", justify="left")
        self.facts.pack(side="left", anchor="w")
        ttk.Label(f, text="Sessions", padding=(0, 12, 0, 4)).pack(anchor="w")
        cols = ("session", "state", "age", "folder")
        self.sessions = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (90, 80, 60, 260)):
            self.sessions.heading(c, text=c.title()); self.sessions.column(c, width=w, anchor="w")
        self.sessions.pack(fill="both", expand=True)
        row = ttk.Frame(f, padding=(0, 10, 0, 0)); row.pack(fill="x")
        self.start_btn = ttk.Button(row, text="Start bridge", command=lambda: self.ctl("Start"))
        ttk.Button(row, text="Restart bridge", command=self.restart).pack(side="left")
        ttk.Button(row, text="Open log", command=lambda: os.startfile(os.path.join(HOME, "bridge.log"))).pack(side="left", padx=6)
        ttk.Button(row, text="Open folder", command=lambda: os.startfile(HOME)).pack(side="left")

    def ctl(self, action):
        self.say(f"{action.lower()}ing the bridge…" if action != "Stop" else "stopping the bridge…")
        threading.Thread(target=lambda: (bridge_ctl(action), self.root.after(0, self.refresh)), daemon=True).start()

    def restart(self):
        self.say("restarting the bridge…")
        threading.Thread(target=lambda: (bridge_ctl("Stop"), bridge_ctl("Start"), self.root.after(0, self.refresh)), daemon=True).start()

    def show_status(self, st):
        self.status = st
        self.header.config(text="Lump: " + describe(st))
        if not st:
            self.facts.config(text="The bridge isn't running, so Lump can't hear from Claude.")
            self.swatch.itemconfig(self.swatch_rect, fill="#BBBBBB")
            self.start_btn.pack(side="left", padx=(0, 6), before=self.start_btn.master.winfo_children()[1])
            self.sessions.delete(*self.sessions.get_children())
            return
        self.start_btn.pack_forget()
        colour = "#" + (st.get("colour") or "FF7A45")
        self.swatch.itemconfig(self.swatch_rect, fill=colour)
        usage = st.get("usage") or {}
        u = lambda k: f"{usage[k]['pct']:.0f}%" if k in usage and usage[k].get("pct") is not None else "–"
        c = st.get("counts") or {}
        meeting = st.get("next_meeting")
        lines = [f"Color #{st.get('colour')}   usage: 5-hour {u('five_hour')}, week {u('seven_day')}",
                 f"Working {c.get('working', 0)} · waiting on you {c.get('alert', 0)} · finished, unseen {c.get('unseen', 0)}",
                 f"Sound {'on' if st.get('sound') else 'off'} · Claude app in front: {'yes' if (st.get('focus') or {}).get('claude_in_front') else 'no'}"]
        if meeting:
            lines.append("Next meeting: %s at %s" % (meeting.get("title") or "untitled", time.strftime("%H:%M", time.localtime(meeting["start"]))))
        self.facts.config(text="\n".join(lines))
        self.sessions.delete(*self.sessions.get_children())
        for sid, s in sorted((st.get("sessions") or {}).items(), key=lambda kv: kv[1].get("age_s", 0)):
            age = s.get("age_s", 0)
            self.sessions.insert("", "end", values=(sid, s.get("state"), f"{age // 60}m" if age >= 60 else f"{age}s",
                                                    os.path.basename((s.get("cwd") or "").rstrip("\\/")) or s.get("cwd") or ""))

    def refresh(self):
        threading.Thread(target=lambda: self.root.after(0, self.show_status, self.bridge.status()), daemon=True).start()

    def tick(self):
        if self.root.state() != "withdrawn":
            self.refresh()
        self.root.after(REFRESH_MS, self.tick)

    # --- Play
    def build_play(self, nb):
        ttk, tk = self.ttk, self.tk
        f = self.frame(nb, "Play")
        ttk.Label(f, text="Animations (play once, then he goes back to what he was doing)").pack(anchor="w")
        self.anim_box = ttk.Frame(f, padding=(0, 6)); self.anim_box.pack(fill="x")
        self.fill_animations()
        ttk.Label(f, text="Preview a mood (lasts until something changes, or Back to live)", padding=(0, 10, 0, 0)).pack(anchor="w")
        moods = ttk.Frame(f, padding=(0, 6)); moods.pack(fill="x")
        for i, m in enumerate(MOODS):
            ttk.Button(moods, text=m, width=10, command=lambda m=m: self.run(f"mood {m}", self.bridge.cmd, f"s {m}")).grid(row=i // 5, column=i % 5, padx=2, pady=2)
        row = ttk.Frame(f); row.pack(fill="x")
        ttk.Button(row, text="Showcase every mood", command=lambda: self.run("showcase", self.bridge.cmd, "s demo")).pack(side="left")
        ttk.Button(row, text="Back to live", command=lambda: self.run("back to live", self.bridge.resync)).pack(side="left", padx=6)
        ttk.Label(f, text="Tap sensitivity: how hard a tap on the glass has to be (lower = gentler)", padding=(0, 14, 0, 0)).pack(anchor="w")
        tap = ttk.Frame(f, padding=(0, 6)); tap.pack(fill="x")
        self.tap_var = tk.DoubleVar(value=float(read_json(CONFIG_FILE).get("tap_threshold") or 0.35))
        self.tap_label = ttk.Label(tap, width=6)
        ttk.Scale(tap, from_=0.1, to=1.5, variable=self.tap_var, length=260,
                  command=lambda v: self.tap_label.config(text=f"{float(v):.2f} g")).pack(side="left")
        self.tap_label.pack(side="left", padx=6)
        self.tap_label.config(text=f"{self.tap_var.get():.2f} g")
        ttk.Button(tap, text="Apply", command=self.apply_tap).pack(side="left")
        ttk.Button(tap, text="Default", command=lambda: (self.tap_var.set(0.35), self.tap_label.config(text="0.35 g"), self.apply_tap())).pack(side="left", padx=6)
        self.build_colour(f)

    def build_colour(self, f):
        """His everyday body colour. The board recolours his drawings live; the usage gauge and meeting countdown
        still blend from it, and the usage warning still ends in the same reds."""
        ttk, tk = self.ttk, self.tk
        ttk.Label(f, text="Body color (usage warnings and meetings still blend from it)", padding=(0, 14, 0, 0)).pack(anchor="w")
        row = ttk.Frame(f, padding=(0, 6)); row.pack(fill="x")
        self.colour_swatch = tk.Canvas(row, width=28, height=28, highlightthickness=0)
        self.colour_swatch.pack(side="left", padx=(0, 10))
        self.colour_dot = self.colour_swatch.create_oval(2, 2, 26, 26, fill="#" + current_body_colour(), outline="")
        for name, hex_ in BODY_COLOURS:
            b = tk.Button(row, width=2, bg="#" + hex_, activebackground="#" + hex_, relief="flat", cursor="hand2",
                          command=lambda h=hex_, n=name: self.set_colour(h, n))
            b.pack(side="left", padx=2)
        ttk.Button(row, text="Pick…", command=self.pick_colour).pack(side="left", padx=(10, 0))
        ttk.Button(row, text="Tangerine", command=lambda: self.set_colour(None, "Tangerine")).pack(side="left", padx=6)

    def pick_colour(self):
        from tkinter import colorchooser
        rgb, hex_ = colorchooser.askcolor(color="#" + current_body_colour(), title="Lump's colour", parent=self.root)
        if hex_:
            self.set_colour(hex_.lstrip("#").upper(), "#" + hex_.lstrip("#").upper())

    def set_colour(self, hex_, name):
        set_body_colour(CONFIG_FILE, hex_)
        self.colour_swatch.itemconfig(self.colour_dot, fill="#" + current_body_colour())
        self.run(f"color {name}", self.bridge.resync)          # the bridge re-reads it and sends the new colour

    def fill_animations(self):
        def work():
            names = self.bridge.animations() or []
            self.root.after(0, self._show_animations, names)
        threading.Thread(target=work, daemon=True).start()

    def _show_animations(self, names):
        for w in self.anim_box.winfo_children():
            w.destroy()
        if not names:
            self.ttk.Label(self.anim_box, text="(start the bridge to see them)", style="Note.TLabel").grid(row=0, column=0)
            self.root.after(5000, self.fill_animations)
            return
        for i, n in enumerate(names):
            self.ttk.Button(self.anim_box, text=n.replace("_", " "), width=13,
                            command=lambda n=n: self.run(f"play {n}", self.bridge.play, n)).grid(row=i // 4, column=i % 4, padx=2, pady=2)

    def apply_tap(self):
        t = round(self.tap_var.get(), 2)
        update_json(CONFIG_FILE, {"tap_threshold": None if abs(t - 0.35) < 0.005 else t})
        self.run(f"tap sensitivity {t:.2f} g", self.bridge.cmd, f"T {t:.2f}")

    # --- Sounds
    def build_sounds(self, nb):
        ttk, tk = self.ttk, self.tk
        f = self.frame(nb, "Sounds")
        cfg = read_json(SOUNDS_FILE)
        self.snd_on = tk.BooleanVar(value=bool(cfg.get("enabled")))
        ttk.Checkbutton(f, text="Lump makes sounds", variable=self.snd_on, command=self.toggle_sound).pack(anchor="w")
        self.quiet = tk.BooleanVar(value=bool(cfg.get("quiet_at_night", True)))
        ttk.Checkbutton(f, text="Quiet at night unless you've been around", variable=self.quiet,
                        command=lambda: update_json(SOUNDS_FILE, {"quiet_at_night": self.quiet.get()})).pack(anchor="w", pady=(2, 0))
        vol = ttk.Frame(f, padding=(0, 8)); vol.pack(fill="x")
        ttk.Label(vol, text="Volume").pack(side="left")
        self.vol = tk.DoubleVar(value=float(cfg.get("volume", 1.0)))
        s = ttk.Scale(vol, from_=0.1, to=2.0, variable=self.vol, length=260)
        s.pack(side="left", padx=8)
        s.bind("<ButtonRelease-1>", lambda e: update_json(SOUNDS_FILE, {"volume": round(self.vol.get(), 2)}))
        ttk.Button(vol, text="Test", command=lambda: self.run("test sound", self.bridge.sound, "play hello")).pack(side="left")
        ttk.Label(f, text="Moments (tick to hear it; ▶ plays it now)", padding=(0, 6, 0, 4)).pack(anchor="w")
        box = ttk.Frame(f); box.pack(fill="both", expand=True)
        canvas = tk.Canvas(box, highlightthickness=0)
        sb = ttk.Scrollbar(box, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")
        self.moment_vars = {}
        for i, (name, m) in enumerate(sorted((cfg.get("moments") or {}).items())):
            v = tk.BooleanVar(value=bool((m or {}).get("on")))
            self.moment_vars[name] = v
            ttk.Checkbutton(inner, text=name.replace("_", " "), variable=v,
                            command=lambda n=name, v=v: set_moment(SOUNDS_FILE, n, v.get())).grid(row=i, column=0, sticky="w")
            ttk.Button(inner, text="▶", width=3, command=lambda n=name: self.run(f"sound {n}", self.bridge.sound, f"play {n}")).grid(row=i, column=1, padx=6)

    def toggle_sound(self):
        on = self.snd_on.get()
        def work():
            if self.bridge.sound("on" if on else "off") is None:      # bridge down: write the file ourselves
                update_json(SOUNDS_FILE, {"enabled": on})
            self.root.after(0, self.say, f"sounds {'on' if on else 'off'}")
        threading.Thread(target=work, daemon=True).start()

    # --- Settings
    def build_settings(self, nb):
        ttk, tk = self.ttk, self.tk
        f = self.frame(nb, "Settings")
        cfg = read_json(CONFIG_FILE)
        self.fields = {}

        def field(row, key, label, value="", secret=False, note=""):
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=value)
            e = ttk.Entry(f, textvariable=v, width=44, show="•" if secret else "")
            e.grid(row=row, column=1, sticky="we", pady=3)
            if note:
                ttk.Label(f, text=note, style="Note.TLabel").grid(row=row + 1, column=1, sticky="w")
            self.fields[key] = v

        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Lump's connection", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
        field(1, "lump_host", "Wi-Fi host", cfg.get("lump_host", ""), note="used when he's not on USB (default lump.local)")
        ttk.Label(f, text="Wi-Fi pairing: " + ("set up" if cfg.get("lump_token") else "not set up (run tools/wifi_setup.py with him plugged in)"),
                  style="Note.TLabel").grid(row=3, column=1, sticky="w")
        ttk.Label(f, text="Calendar", font=("Segoe UI", 10, "bold")).grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))
        field(5, "calendar_ics", "Private iCal link", cfg.get("calendar_ics", ""), secret=True)
        field(6, "my_email", "Your email", cfg.get("my_email", ""), note="skips meetings you've declined")
        ttk.Label(f, text="Slack", font=("Segoe UI", 10, "bold")).grid(row=8, column=0, columnspan=2, sticky="w", pady=(12, 0))
        field(9, "slack_token", "User token (xoxp-…)", cfg.get("slack_token", ""), secret=True)
        field(10, "slack_vips", "VIPs", ", ".join(cfg.get("slack_vips") or []), note="names as Slack shows them, comma separated")
        field(12, "slack_vip_minutes", "Letter lasts (minutes)", str(cfg.get("slack_vip_minutes") or ""))
        row = ttk.Frame(f, padding=(0, 14, 0, 0)); row.grid(row=13, column=0, columnspan=2, sticky="w")
        ttk.Button(row, text="Save", command=self.save_settings).pack(side="left")
        ttk.Label(f, text="The bridge picks up changes by itself.", style="Note.TLabel").grid(row=14, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def save_settings(self):
        v = {k: var.get().strip() for k, var in self.fields.items()}
        mins = v.pop("slack_vip_minutes")
        try:
            mins = float(mins) if mins else None
        except ValueError:
            self.say("Letter minutes must be a number"); return
        vips = parse_vips(v.pop("slack_vips"))
        old = read_json(CONFIG_FILE)
        update_json(CONFIG_FILE, {**v, "slack_vips": vips or None, "slack_vip_minutes": mins})
        if v["calendar_ics"] != old.get("calendar_ics", "") or v["my_email"] != old.get("my_email", ""):
            threading.Thread(target=self.bridge.calendar_now, daemon=True).start()
        self.say("settings saved")

    # --- show / hide
    def show(self):
        self.root.deiconify(); self.root.lift(); self.root.focus_force()
        self.refresh()

    def hide(self):
        self.root.withdraw()


# ---------- the new window: an HTML page in Windows' own WebView2 (pywebview) ----------
# The page (tools/windows/ui/) draws everything; Python only answers its questions through Api below, so the
# page never touches files or the network itself, and no port is opened for it. Without pywebview (or WebView2)
# the older Tk window above is used instead.
HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIRS = (os.path.join(HOME, "ui"), os.path.join(HERE, "ui"))
# Lump's drawings: the very file the board plays (240 x 240 frames), so the window shows what's on the glass
ART_FILES = (os.path.join(HOME, "ui", "lump.bin"), os.path.join(os.path.dirname(os.path.dirname(HERE)), "firmware", "data", "lump.bin"))
GAG_NAMES = {"tap": "Random gag", "moto": "Ring of fire", "wolf": "Wolf howl", "glass": "Face on the glass",
             "banana": "Banana peel", "balloon": "Balloon pop", "moon": "Mooning", "bomb": "Cartoon bomb",
             "startle": "Late-night startle", "prep": "Meeting prep", "fidget_toss": "Fidget: toss",
             "fidget_twitch": "Fidget: twitch", "fidget_bubble": "Fidget: bubble"}


def first_existing(paths, name=""):
    for p in paths:
        if os.path.exists(os.path.join(p, name)):
            return p
    return paths[-1]


class Art:
    """Reads lump.bin, the board's own drawings (format: firmware/src/main.cpp, parseAnim): for each mood the
    frames of its loop, as the board's run-length data, which the page decodes and recolours exactly as the
    firmware does. Only the header is parsed up front; frames are read when a mood is first asked for."""
    LOOP, EVENT = 0, 2

    def __init__(self, path):
        import struct
        with open(path, "rb") as f:
            self.data = f.read()
        d, p = self.data, 0
        if d[:4] != b"LUMP" or struct.unpack_from("<H", d, 4)[0] != 3:
            raise ValueError("not a lump.bin (version 3)")
        p = 6
        self.n_frames = struct.unpack_from("<H", d, p)[0]; p += 2
        n_moods, self.fps = d[p], d[p + 1]; p += 2
        names = []
        for _ in range(n_moods):
            p += 1                                       # base mood
            n = d[p]; names.append(d[p + 1:p + 1 + n].decode("ascii", "replace")); p += 1 + n
        n_seqs = d[p]; p += 1
        self.loops, self.events = {}, {}
        for _ in range(n_seqs):
            kind, frm = d[p], d[p + 1]; p += 3
            n = d[p]; seq_name = d[p + 1:p + 1 + n].decode("ascii", "replace"); p += 1 + n
            ln = struct.unpack_from("<H", d, p)[0]; p += 2
            seq = list(struct.unpack_from("<%dH" % ln, d, p)); p += ln * 2
            if kind == self.LOOP and frm < len(names):
                self.loops[names[frm]] = seq
            elif kind == self.EVENT:
                self.events[seq_name] = seq
        self.offsets = struct.unpack_from("<%dI" % self.n_frames, d, p)
        self.base = p + self.n_frames * 4

    def frame(self, i):
        start = self.base + self.offsets[i]
        end = self.base + self.offsets[i + 1] if i + 1 < self.n_frames else len(self.data)
        return self.data[start:end]

    def mood(self, name):
        """{"fps", "ticks": indices into "frames", "frames": [base64 run-length data]} for one mood's loop."""
        return self._pack(self.loops.get(name) or self.loops.get("idle"))

    def event(self, name):
        """The same for a one-off animation (a gag, fidget, oops, hello), by its exact name; None if unknown."""
        return self._pack(self.events.get(name))

    def _pack(self, seq):
        import base64
        if not seq:
            return None
        order = list(dict.fromkeys(seq))
        index = {f: n for n, f in enumerate(order)}
        return {"fps": self.fps, "size": 240, "ticks": [index[f] for f in seq],
                "frames": [base64.b64encode(self.frame(f)).decode("ascii") for f in order]}


def system_theme():
    """Windows' app theme: "light" or "dark" (dark if it can't be read)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except Exception:
        return "dark"


def outcome(what, ok):
    """The page's message for an action: done, or why not."""
    if ok:
        return {"ok": True, "message": f"{what}: done"}
    return {"ok": False, "message": f"{what}: " + ("the board isn't connected" if ok is False else "the bridge isn't running")}


class Api:
    """Everything the page can ask for (pywebview's js_api: each public method is callable from the page and
    returns plain JSON data). Settings go to the same files the bridge re-reads, as in the Tk window."""

    def __init__(self, bridge, art_path=None, ctl=None):
        self._bridge, self._ctl = bridge, ctl or bridge_ctl
        self._art_path = art_path or next((p for p in ART_FILES if os.path.exists(p)), ART_FILES[-1])
        self._art, self._mood_cache = None, {}

    # --- what the page shows
    def state(self):
        cfg, snd = read_json(CONFIG_FILE), read_json(SOUNDS_FILE)
        moments = {k: bool((v or {}).get("on")) for k, v in sorted((snd.get("moments") or {}).items())}
        try: tap = float(cfg.get("tap_threshold") or 0.35)
        except (TypeError, ValueError): tap = 0.35
        return {"status": self._bridge.status(),
                "theme": cfg.get("ui_theme") if cfg.get("ui_theme") in ("dark", "light") else system_theme(),
                "colour": current_body_colour(), "tap": tap,
                "sounds": {"enabled": bool(snd.get("enabled")), "volume": float(snd.get("volume", 1.0)),
                           "quiet_at_night": bool(snd.get("quiet_at_night", True)), "moments": moments},
                "settings": {"lump_host": cfg.get("lump_host", ""), "lump_paired": bool(cfg.get("lump_token")),
                             "calendar_ics": cfg.get("calendar_ics", ""), "my_email": cfg.get("my_email", ""),
                             "slack_token": cfg.get("slack_token", ""), "slack_vips": ", ".join(cfg.get("slack_vips") or []),
                             "slack_vip_minutes": str(cfg.get("slack_vip_minutes") or "")}}

    def animations(self):
        names = self._bridge.animations() or []
        return [{"name": n, "label": GAG_NAMES.get(n, n.replace("_", " ").capitalize())} for n in names]

    def screen(self, mood):
        """One mood's loop as the board draws it (see class Art), or None if the drawings can't be read."""
        if mood not in self._mood_cache:
            try:
                if self._art is None:
                    self._art = Art(self._art_path)
                self._mood_cache[mood] = self._art.mood(str(mood))
            except (OSError, ValueError, IndexError) as e:
                return None
        return self._mood_cache[mood]

    def screen_event(self, name):
        """A one-off animation the board just started (status "playing"), as the board draws it."""
        key = ("event", str(name))
        if key not in self._mood_cache:
            try:
                if self._art is None:
                    self._art = Art(self._art_path)
                self._mood_cache[key] = self._art.event(str(name))
            except (OSError, ValueError, IndexError):
                return None
        return self._mood_cache[key]

    # --- actions
    def play(self, name):
        return outcome(f"play {GAG_NAMES.get(name, name)}", self._bridge.play(name))

    def preview(self, mood):
        if mood != "demo" and mood not in MOODS:
            return {"ok": False, "message": "no such mood"}
        return outcome("showcase" if mood == "demo" else f"preview {mood}", self._bridge.cmd(f"s {mood}"))

    def live(self):
        return outcome("back to live", self._bridge.resync())

    def set_tap(self, g):
        t = round(min(1.5, max(0.1, float(g))), 2)
        update_json(CONFIG_FILE, {"tap_threshold": None if abs(t - 0.35) < 0.005 else t})
        return outcome(f"tap sensitivity {t:.2f} g", self._bridge.cmd(f"T {t:.2f}"))

    def set_colour(self, hex_):
        try:
            set_body_colour(CONFIG_FILE, hex_)
        except ValueError as e:
            return {"ok": False, "message": str(e)}
        r = self._bridge.resync()
        return {"ok": True, "message": "color saved" + ("" if r else " (Lump gets it when the bridge is running)")}

    def set_sound(self, on):
        if self._bridge.sound("on" if on else "off") is None:          # bridge down: write the file ourselves
            update_json(SOUNDS_FILE, {"enabled": bool(on)})
        return {"ok": True, "message": "sounds " + ("on" if on else "off")}

    def set_volume(self, v):
        update_json(SOUNDS_FILE, {"volume": round(min(2.0, max(0.1, float(v))), 2)})
        return {"ok": True, "message": ""}

    def set_quiet(self, on):
        update_json(SOUNDS_FILE, {"quiet_at_night": bool(on)})
        return {"ok": True, "message": ""}

    def set_moment(self, name, on):
        set_moment(SOUNDS_FILE, str(name), bool(on))
        return {"ok": True, "message": ""}

    def play_sound(self, name):
        r = self._bridge.sound(f"play {name}")
        return {"ok": r == "ok", "message": f"sound {name}" + (": the bridge isn't running" if r is None else "" if r == "ok" else ": not found")}

    def save_settings(self, values):
        v = {k: str((values or {}).get(k, "")).strip() for k in ("lump_host", "calendar_ics", "my_email", "slack_token", "slack_vips", "slack_vip_minutes")}
        mins = v.pop("slack_vip_minutes")
        try:
            mins = float(mins) if mins else None
        except ValueError:
            return {"ok": False, "message": "Letter minutes must be a number"}
        old = read_json(CONFIG_FILE)
        update_json(CONFIG_FILE, {**v, "slack_vips": parse_vips(v.pop("slack_vips")) or None, "slack_vip_minutes": mins})
        if v["calendar_ics"] != old.get("calendar_ics", "") or v["my_email"] != old.get("my_email", ""):
            threading.Thread(target=self._bridge.calendar_now, daemon=True).start()
        return {"ok": True, "message": "settings saved"}

    def save_apps(self, apps):
        """Remember the Claude apps to read usage from (config.json "claude_apps": [{"name", "path"}]). The usual
        app folders are always read; listing one here only names it. The bridge picks changes up within a minute."""
        out, seen = [], set()
        for a in (apps or [])[:12]:
            name, path = str((a or {}).get("name", "")).strip()[:40], str((a or {}).get("path", "")).strip()
            if not path:
                continue
            if not os.path.isdir(path):
                return {"ok": False, "message": f"not a folder: {path}"}
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name or os.path.basename(path.rstrip("\\/")).capitalize(), "path": path})
        update_json(CONFIG_FILE, {"claude_apps": out or None})
        return {"ok": True, "message": "Claude apps saved (usage shows within a minute)"}

    def pick_folder(self):
        """A folder dialog for the data folder of a Claude app kept somewhere unusual; None if cancelled."""
        win = getattr(self, "_window", None)
        if win is None:
            return None
        import webview
        start = os.path.expanduser("~")
        got = win.create_file_dialog(webview.FOLDER_DIALOG, directory=start)
        return got[0] if got else None

    def set_theme(self, theme):
        update_json(CONFIG_FILE, {"ui_theme": theme if theme in ("dark", "light") else None})
        return {"ok": True, "message": ""}

    def bridge_ctl(self, action):
        if action == "start":
            self._ctl("Start")
        elif action == "restart":
            self._ctl("Stop"); self._ctl("Start")
        else:
            return {"ok": False, "message": "no such action"}
        return {"ok": True, "message": "bridge " + ("started" if action == "start" else "restarted")}

    def open(self, what):
        path = {"log": os.path.join(HOME, "bridge.log"), "folder": HOME}.get(what)
        if path and os.path.exists(path):
            os.startfile(path)
            return {"ok": True, "message": ""}
        return {"ok": False, "message": "not found"}


# ---------- the tray icon ----------
def start_tray(bridge, show, restart, quit_app, holder):
    """The tray icon. show/restart/quit_app: callbacks; holder["status"] is kept up to date for the menu."""
    try:
        import pystray
        from PIL import Image
    except ImportError:
        return None
    path = os.path.join(HOME, "lump_icon.png")
    if not os.path.exists(path):
        path = os.path.join(os.path.dirname(HERE), "lumpbar", "icon.png")
    image = Image.open(path) if os.path.exists(path) else Image.new("RGBA", (64, 64), (255, 122, 69, 255))
    names = bridge.animations() or ["tap", "disco", "shock", "hello"]

    def sound_on(item=None):
        return bool((holder.get("status") or {}).get("sound"))

    def toggle_sound(icon, item):
        bridge.sound("toggle") if bridge.status() else update_json(SOUNDS_FILE, {"enabled": not read_json(SOUNDS_FILE).get("enabled")})

    def player(n):                       # pystray wants actions taking exactly (icon, item)
        return lambda icon, item: bridge.play(n)

    menu = pystray.Menu(
        pystray.MenuItem("Open Lump Control", lambda icon, item: show(), default=True),
        pystray.MenuItem("Play", pystray.Menu(*[pystray.MenuItem(GAG_NAMES.get(n, n.replace("_", " ")), player(n)) for n in names])),
        pystray.MenuItem("Sound", toggle_sound, checked=sound_on),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Restart bridge", lambda icon, item: restart()),
        pystray.MenuItem("Quit Lump Control", lambda icon, item: (icon.stop(), quit_app())),
    )
    icon = pystray.Icon("lump-control", image, "Lump", menu)
    icon.run_detached()

    def keep_title():
        while True:
            st = bridge.status()
            holder["status"] = st
            try:
                icon.title = ("Lump: " + describe(st))[:120]
                icon.update_menu()
            except Exception:
                pass
            time.sleep(5)
    threading.Thread(target=keep_title, daemon=True).start()
    return icon


def set_window_icon(title):
    """Give the window Lump's icon instead of Python's (pywebview can't set one on Windows)."""
    try:
        import ctypes
        ico = os.path.join(HOME, "lump.ico")
        if not os.path.exists(ico):
            from PIL import Image
            src = os.path.join(HOME, "lump_icon.png")
            if not os.path.exists(src):
                src = os.path.join(os.path.dirname(HERE), "lumpbar", "icon.png")
            Image.open(src).save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p)
        hwnd = user32.FindWindowW(None, title)
        small, big = user32.GetSystemMetrics(49), user32.GetSystemMetrics(11)   # SM_CXSMICON, SM_CXICON (DPI-aware)
        for which, size in ((0, small or 16), (1, big or 32)):  # ICON_SMALL (title bar), ICON_BIG (taskbar, Alt+Tab)
            icon = user32.LoadImageW(None, ico, 1, size, size, 0x10)   # IMAGE_ICON, LR_LOADFROMFILE
            if hwnd and icon:
                user32.SendMessageW(hwnd, 0x80, which, icon)           # WM_SETICON
    except Exception:
        pass


def run_webview(bridge, start_hidden):
    """The new window. Returns False if pywebview isn't available (the caller falls back to Tk)."""
    try:
        import webview
    except ImportError:
        return False
    ui_dir = first_existing(UI_DIRS, "index.html")
    api = Api(bridge)
    quitting = {"now": False}
    win = api._window = webview.create_window("Lump Control", url=os.path.join(ui_dir, "index.html"), js_api=api, width=1040, height=700,
                                min_size=(900, 620), hidden=start_hidden, background_color="#141518")

    def on_closing():
        if quitting["now"]:
            return True
        win.hide()                        # closing the window keeps Lump Control in the tray
        return False
    win.events.closing += on_closing
    win.events.shown += lambda: set_window_icon("Lump Control")

    def show():
        win.show(); win.restore()

    def restart():
        threading.Thread(target=lambda: (bridge_ctl("Stop"), bridge_ctl("Start")), daemon=True).start()

    def quit_app():
        quitting["now"] = True
        win.destroy()

    holder = {}
    listen_for_show(show)
    tray = start_tray(bridge, show, restart, quit_app, holder)
    if tray is None and start_hidden:
        win.events.loaded += lambda: win.show()      # no tray to open it from: show it
    webview.start(private_mode=False, storage_path=os.path.join(HOME, "webview"))
    if tray:
        tray.stop()
    return True


def main():
    if already_running():
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)          # crisp text on high-DPI screens
        # its own taskbar identity, so the taskbar shows Lump's icon instead of grouping it under Python
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DeskBuddy.LumpControl")
    except Exception:
        pass
    bridge = Bridge()
    start_hidden = "--show" not in sys.argv                     # at logon: just the tray icon
    if "--tk" not in sys.argv and run_webview(bridge, start_hidden):
        return
    import tkinter as tk
    root = tk.Tk()
    win = ControlWindow(root, bridge)
    listen_for_show(lambda: root.after(0, win.show))
    holder = {}
    tray = start_tray(bridge, lambda: root.after(0, win.show), lambda: root.after(0, win.restart),
                      lambda: root.after(0, root.destroy), holder)
    win.status_holder = holder
    if tray and start_hidden:
        root.withdraw()
    root.after(REFRESH_MS, win.tick)
    root.mainloop()
    if tray:
        tray.stop()


if __name__ == "__main__":
    main()
