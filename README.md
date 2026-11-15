# Lump, a desk buddy for Claude

<img src="design/media/lump-dance.gif" alt="Lump, an orange blob with a curly cowlick, dancing in sunglasses under a disco ball inside the glass cube display" width="240" align="right">

Lump is a little blob who lives in a tiny glass prism display on your desk and shows
you what your Claude sessions are up to, so you can look away from the screen while Claude works.

- **Working** while Claude Code is busy, **waving for attention** when a session needs you
  (a permission prompt, a question, a plan to approve), and **celebrating** when it's done.
- Corner counters show how many sessions are working, waiting on you, or finished and unread.
- His colour warms from tangerine to red as you get close to your Claude usage limits.
- **Tap the glass** and he plays one of a dozen gags: electric shock, ring of fire, wolf howl,
  banana peel, cartoon bomb and more.
- Optional extras: turns blue before calendar meetings, holds up a letter when a Slack VIP
  messages you, a tiny ocarina voice and sound effects for the gags, sleeps at night, and runs over Wi-Fi with over-the-air updates.

Lump was built with Claude Code, drawings included: Claude draws every frame in code, in a scratchy
hand-drawn style, in one web page (`design/lump.html`), and a script bakes it into a file the ESP32
plays back at 24 fps.

## What you need

- **The display:** Waveshare / SpotPear **ESP32-S3-LCD-1.3** "prism" (ESP32-S3R8, 16 MB flash,
  8 MB PSRAM, 240×240 ST7789 behind a glass cube, motion sensor for taps). Lump was built on
  [this AliExpress listing](https://www.aliexpress.com/item/1005009887388696.html), colour option
  **ESP32-S3-LCD-1.3-C** (about $20). Keep its stock case: the mount is sized for it.
- **A USB-C data cable** for the first flash. After Wi-Fi setup he only needs power.
- **A Mac or a Windows PC** for the bridge that connects Claude to Lump. The firmware itself doesn't
  care. Windows has its own setup: see [Setup on Windows](#setup-on-windows).
- **Claude Code**, plus optionally the Claude desktop app (for Chat and Cowork states, Mac only).
- On a Mac: [PlatformIO](https://platformio.org/) (`brew install platformio`), Python 3, and the Xcode
  command line tools (`xcode-select --install`).
- Optional: a 3D printer for the [monitor mount](enclosure/print/), which holds him on the back of your monitor, peeking over the top.

## Setup

```bash
git clone https://github.com/joebentham/desk-buddy.git
cd desk-buddy
```

**1. Flash the board.** Plug it in with a data cable, then:

```bash
tools/flash.sh
```

This builds the firmware and writes it plus Lump's drawings (~11 MB; the drawings take about
5 minutes). It uses a paced esptool upload because macOS drops bytes from big bursts to
this board's USB-serial chip, so plain `pio run -t upload` fails.

**2. Install the bridge.** A small Python service that listens to Claude and drives Lump, plus
a helper that reads the Claude app's window (Chat and Cowork have no hooks). Both run as login items.

```bash
tools/install_bridge.sh
```

When macOS asks, allow `deskbuddy-appwatch` under System Settings → Privacy & Security →
Accessibility. Everything it installs lives in `~/.desk-buddy`.

**3. Hook up Claude Code.** Adds small async hooks to `~/.claude/settings.json` that tell the
bridge what each session is doing, and a status line that feeds him your usage limits.
It backs up your settings first and keeps any hooks you already have.

```bash
python3 tools/install_hooks.py
```

Start a Claude Code session and Lump should wake up.

**4. Optional: Wi-Fi.** With the cable still plugged in (2.4 GHz networks only):

```bash
~/.desk-buddy/venv/bin/python tools/wifi_setup.py
```

Your Wi-Fi password goes straight into the board over USB and is never saved on the Mac. After
this he joins your network as `lump.local`, keeps time from the internet, and can live on any USB
charger, such as a monitor's power-only port. Updates then go over the air:

```bash
python3 tools/ota.py
```

### Setup on Windows

Same steps, Windows tools. You need [Python 3](https://www.python.org/downloads/) and
[Git for Windows](https://git-scm.com/download/win) (Claude Code runs Lump's hooks in its Git Bash).
Run these in PowerShell from the repo folder:

**1. Flash the board.** Installs PlatformIO and esptool into `tools\.venv` the first time, builds, and
writes the firmware and drawings in about a minute (`-NoArt` skips the drawings):

```powershell
powershell -ExecutionPolicy Bypass -File tools\windows\flash.ps1
```

**2. Install the bridge.** Sets up `~\.desk-buddy`, makes his sounds, and starts the bridge at logon from a
shortcut in your Startup folder (no admin rights needed; it shows under Task Manager › Startup apps).
`-Uninstall` removes it again.

```powershell
powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1
```

**3. Hook up Claude Code.** The same hooks as on the Mac, so a `settings.json` you share between machines
stays valid on all of them. Works for Claude Code in the terminal and in the Claude desktop app's Code tab.

```powershell
python tools\install_hooks.py
```

**4. Optional: Wi-Fi** (cable still plugged in, 2.4 GHz only), then over-the-air updates:

```powershell
~\.desk-buddy\venv\Scripts\python tools\wifi_setup.py
~\.desk-buddy\venv\Scripts\python tools\ota.py
```

**Lump Control.** The installer also puts a small Lump in your system tray (it starts at logon too). Click
him for a window with Lump himself on the left, live: the board's own frames in his current mood and colour,
with his counters and usage. On the right: Status (sessions), Play (every gag, mood previews, tap
sensitivity), Sounds (on/off, volume, each moment) and Settings (body colour, Wi-Fi host, calendar, Slack),
in dark or light mode. Right-click for quick play, sound on/off and restarting the bridge. The window is a
small HTML page in Windows' own WebView2 (pywebview); it only talks to the bridge's local API and its
settings files, so changes apply straight away.

How it differs from the Mac: his usage colour also follows the desktop app (which has no status line) by
reading its own usage history, sounds play through Windows, and Chat and Cowork states aren't tracked
yet (Code sessions are). `tools\windows\bridge.ps1 -Status | -Stop | -Start` checks or restarts the bridge;
its log is `~\.desk-buddy\bridge.log`.

### Lump on another computer (remote link)

For when Claude runs on one Windows PC (the **Claude PC**, with the bridge) and you work from another one,
over Remote Desktop say, with Lump plugged into that one (**Lump's PC**). A small relay on Lump's PC dials
out to the bridge over [Tailscale](https://tailscale.com) and passes Lump's lines back and forth, so he
follows your Claude sessions whether or not the Remote Desktop session is connected, and finds his way back
by himself after restarts, sleep and unplugging. Both PCs need Tailscale on the same tailnet with MagicDNS
on. Lump's PC needs Python 3 and a copy of this repo, but no admin rights, no open port and no firewall change.

**1. On the Claude PC** (bridge already installed):

```powershell
powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1 -AllowRelay
```

This makes a relay token (`relay_token` in `~\.desk-buddy\config.json`), and runs
`tailscale serve --bg --tcp 4211 tcp://127.0.0.1:4211`, which passes port 4211 from your tailnet (and only your
tailnet: this isn't Funnel) to the bridge, which listens on 127.0.0.1 only. It then prints the command for
Lump's PC, with this PC's Tailscale name filled in and without the token.

**2. On Lump's PC**, in the repo folder, run the printed command. It looks like:

```powershell
powershell -ExecutionPolicy Bypass -File tools\windows\install.ps1 -Relay -BridgeHost claude-pc.your-tailnet.ts.net
```

It asks for the token at a hidden prompt: in Lump Control on the Claude PC, Settings › Remote Lump › **Copy
token**, then paste (Remote Desktop shares the clipboard). `-TokenFile <path>` reads it from a file instead. The
token is never a command-line argument, so it doesn't end up in a shell history or the process list. The
installer refuses if the bridge is installed on Lump's PC too, since both would want his USB port; `-Force`
stops that bridge and takes it out of startup.

Lump Control's Status tab then shows him on the remote link, and whether his board is plugged in over there.
**New token** (Settings › Remote Lump) disconnects the relay until you run step 2 again with the new one.

**What runs on Lump's PC:** a visible `pythonw.exe` process running `~\.desk-buddy\lump_relay.py`, started at
logon by a **Lump relay** shortcut in your Startup folder, with a small Python environment (pyserial only) in
`~\.desk-buddy\venv`, its settings and token in `~\.desk-buddy\relay.json`, and its log in
`~\.desk-buddy\relay.log` (never the token). It only opens Lump's own USB-serial chip and only makes one outgoing
connection, over Tailscale to the Claude PC's port 4211; it checks the name resolves to a Tailscale address
before sending anything.

**What flows each way:** to Lump, the bridge's display commands: his mood, colour, the three corner counts, the
meeting-countdown flag and a heartbeat carrying the time of day. The relay passes only those kinds of line and
refuses everything else, including the command that changes his Wi-Fi settings (the bridge never sends that
either). From Lump: taps, which gag he's playing, his start-up line, and every 10 s whether he's plugged in. No
prompts, code, file names or session titles cross the link, only moods and counts. Sounds still play on the
Claude PC (you hear them through Remote Desktop while it's connected).

**What the administrators of Lump's PC can learn:** they can see the relay process and its connection to your
Claude PC, read the token in `relay.json` and the log, and watch the lines above (when you're busy, when
Claude is waiting on you, how close to your usage limits you are, and the time of day). Someone with the token
and a way onto your tailnet could pretend to be the relay, so the bridge treats the relay as untrusted: it
takes nothing from it but taps, known gag names and the start-up line (rate-limited, short lines only), sends
it nothing but the display commands, and drops it after 30 s of silence. **New token** shuts out an old copy.

**Recommended: limit port 4211 to Lump's PC** with a Tailscale access rule (admin console › Access controls).
Tailscale's default policy lets every device on your tailnet reach every port; rules only ever add access, so
replace the allow-all rule with ones like these (use each PC's `tailscale ip -4`):

```jsonc
{
  "hosts": {
    "claude-pc": "100.x.y.z",
    "lump-pc":   "100.a.b.c"
  },
  "acls": [
    // only Lump's PC reaches the relay port on the Claude PC
    {"action": "accept", "src": ["lump-pc"], "dst": ["claude-pc:4211"]},
    // and whatever else you use, e.g. Remote Desktop from Lump's PC
    {"action": "accept", "src": ["lump-pc"], "dst": ["claude-pc:3389"]}
  ]
}
```

**If Lump's PC is a work computer,** check that your employer allows Tailscale and USB devices on it first. The
relay isn't hidden: it shows in Task Manager, the Startup folder and the network connections.

**To remove it:** on Lump's PC, `install.ps1 -Uninstall` stops the relay and deletes its shortcut and
`relay.json` (delete `~\.desk-buddy` too if nothing else lives there). On the Claude PC,
`install.ps1 -AllowRelay -Off` removes only the Tailscale serve entry for port 4211 and the token; the bridge
then drops the relay and stops listening within a few seconds.

### Optional extras

Put these in `~/.desk-buddy/config.json`. The bridge re-reads it, so no restart is needed.

| Key | What it does |
|---|---|
| `calendar_ics` | Your calendar's private iCal link. He fades to blue over the 10 minutes before a meeting and plays a "meeting time" alert. |
| `my_email` | With `calendar_ics`: skip meetings you've declined. |
| `slack_token` | A Slack **user** token (`xoxp-…`) from your own private Slack app with read-only scopes `im:read im:history mpim:read mpim:history users:read search:read`. DMs and @-mentions send a paper plane round him. The token is never logged. |
| `slack_vips` | Names as Slack shows them. An unread DM from one of them makes him hold up a letter until you've read it. |

Sounds: `~/.desk-buddy/sounds.json` picks which moments make a noise (`"enabled": false` mutes him).
A second Claude Code config (such as `~/.claude-work`) can drive him too: run
`python3 tools/install_hooks.py --config-dir ~/.claude-work`.

### Lump on your Mac (for when you're away from the desk)

A little Lump for your screen that mirrors the desk one. On a MacBook with a notch he sits in a small
black wing beside the camera; otherwise he's a menu bar item. There's no pop-out and no text. He's
just a reminder of what the desk Lump is doing.

```bash
python3 tools/lumpbar/bake.py      # shrink his drawings (needs Google Chrome; re-run after editing lump.html)
zsh tools/lumpbar/build.sh         # builds ~/Applications/Lump.app and starts it
```

The Mac can't tell how far away you are, so it uses your monitor as a stand-in. While your **desk
monitor** is plugged in and the desk Lump is online, you're "at your desk" and the Mac one hides. Right-click him to:

- pick **My Desk Monitor**: the screen the desk Lump sits by (the default is any external display), or
- show him **All the Time** (handy if you don't use a monitor, or if the desk Lump is by your laptop).

Can't see him? Open Lump (Spotlight works) and he appears with that menu open, even at your desk. Once you
close it he hides again if you're at your desk. The same goes for the very first launch. To start him at login, add Lump.app under System Settings › General › Login Items.

## Make him your own

All of Lump is drawn in code in **`design/lump.html`**: his body, faces, moods, transitions and
every tap gag. To preview and edit:

```bash
python3 tools/export_frames.py 8767
```

Open <http://localhost:8767/lump.html#gags> in a browser. The page reloads when you save, and the
"Tap gags" gallery lets you pause and step through frames. When you're happy, run the `EXPORT_JS`
snippet from `tools/export_frames.py` in the page's console. It bakes every frame into
`firmware/data/lump.bin`, then `python3 tools/ota.py --art` (Wi-Fi) or `tools/flash.sh` (cable) puts it on the board.

Two tips: keep repeating effects on a 12-tick beat, so frames are shared between moods and the file
stays small (it has a 12 MB budget). Keep faces simple: dot eyes, small mouths, and let props sell the gag.

## How it fits together

```
Claude Code hooks ─┐
status line ───────┤                           USB serial or Wi-Fi
Claude app window ─┼─▶ bridge (Mac, :47823) ─────────────────────▶ ESP32 prism: Lump
calendar / Slack ──┘   one mood for all sessions                   plays baked frames, senses taps
```

| Path | What's there |
|---|---|
| `firmware/src/main.cpp` | The frame player: moods, transitions, taps, offline mode, Wi-Fi, OTA. The serial protocol is documented at the top. |
| `firmware/data/lump.bin` | Lump's baked frames (written to their own flash partition) |
| `design/lump.html` | Lump himself: every drawing and animation |
| `tools/buddy_bridge.py` | The bridge: Claude hooks, app state, usage, calendar, Slack, sounds → one mood |
| `tools/appwatch/` | Swift helper that reads Chat/Cowork state from the Claude app's accessibility tree |
| `tools/lumpbar/` | Lump on your Mac: frame baker (`bake.py`) and the notch/menu bar app (`main.swift`) |
| `tools/flash.sh`, `tools/ota.py`, `tools/wifi_setup.py` | Getting firmware and drawings onto the board |
| `tools/windows/` | Windows: `flash.ps1`, `install.ps1` (bridge at logon, `-AllowRelay` / `-Relay` for the remote link), `bridge.ps1` (start/stop/status), `keepalive.py`, `control.py` (Lump Control tray app) |
| `tools/lump_relay.py` | The remote link's relay, on the computer Lump is plugged into when Claude runs on another |
| `enclosure/` | OpenSCAD source for the adjustable monitor mount, with [ready-to-print STLs](enclosure/print/) |
| `tests/` | Bridge tests (no board needed): `~/.desk-buddy/venv/bin/python -m unittest discover -s tests` (Windows: `venv\Scripts\python`) |

The bridge's debug endpoints help when you're poking at him: `curl 127.0.0.1:47823/status`, or
`curl -X POST --data "e tap" 127.0.0.1:47823/cmd` to send a raw command. To play an animation on
demand (from a hotkey, a menu bar app or Shortcuts), `curl -X POST --data disco 127.0.0.1:47823/play`;
`curl 127.0.0.1:47823/play` lists the names.

## License

Code and 3D models: [MIT](LICENSE). Lump himself (the character, his drawings, animations and
sounds): [CC BY-NC 4.0](LICENSE-ART.md). Build one, remix him, share it; just don't sell him.
