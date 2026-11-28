"""Hook Claude Code up to Lump.

    python3 tools/install_hooks.py                      # ~/.claude/settings.json
    python3 tools/install_hooks.py --config-dir ~/.claude-work   # a second Claude Code config
    python3 tools/install_hooks.py --uninstall

Adds one small async hook per Claude Code event that matters to Lump. Each hook POSTs the event's
JSON to the bridge on 127.0.0.1:47823 and never blocks or fails Claude Code. It also installs the
status line (tools/statusline.sh, which feeds your usage limits to his colour) unless you already
have a status line of your own. Running it again changes nothing; settings.json is backed up first.

On Windows the hooks are the very same entries (Claude Code runs them in Git Bash, which has curl), so a
settings.json shared between a Mac and Windows machines stays valid on both. Only the status line differs:
tools/statusline.py, run by the Python that ran this script, written with plain absolute paths.
"""
import argparse, json, os, shutil, sys, time

IS_WIN = sys.platform == "win32"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = os.path.expanduser("~/.desk-buddy")
HOOK_CMD = ("curl -s -m 1 -o /dev/null -H 'Content-Type: application/json' "
            "--data-binary @- http://127.0.0.1:47823/hook || true")
# events without a tool name take no matcher
EVENTS = {"SessionStart": "*", "UserPromptSubmit": None, "PreToolUse": "*", "PermissionRequest": "*",
          "PostToolUse": "*", "PostToolUseFailure": "*", "Notification": "*", "Elicitation": "*",
          "Stop": None, "StopFailure": "*", "SessionEnd": "*"}
MARK = "127.0.0.1:47823/"


def ours(group):
    return any(MARK in (h.get("command") or "") or MARK in (h.get("url") or "") for h in group.get("hooks", []))


def windows_status_command(status_py):
    """The status line command on Windows: unquoted absolute paths with forward slashes, the one form that
    runs the same in Git Bash and PowerShell (either may run it). None if a path has a space in it."""
    exe, script = sys.executable.replace("\\", "/"), status_py.replace("\\", "/")
    if " " in exe or " " in script:
        return None
    return f"{exe} -I {script}"


def is_our_status(line, status_cmd):
    cmd = line.get("command") or ""
    if IS_WIN:
        return cmd.replace("\\", "/").endswith("/.desk-buddy/statusline.py")
    return cmd == status_cmd


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config-dir", default="~/.claude", help="Claude Code config folder (default ~/.claude)")
    ap.add_argument("--uninstall", action="store_true", help="remove Lump's hooks and status line")
    args = ap.parse_args()

    path = os.path.join(os.path.expanduser(args.config_dir), "settings.json")
    settings = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            settings = json.load(f)
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    hooks = settings.setdefault("hooks", {})
    status_src = "statusline.py" if IS_WIN else "statusline.sh"
    status_file = os.path.join(RUNTIME, status_src)
    status_cmd = windows_status_command(status_file) if IS_WIN else status_file

    changed = []
    for event, matcher in EVENTS.items():
        groups = hooks.get(event, [])
        if args.uninstall:
            kept = [g for g in groups if not ours(g)]
            if len(kept) != len(groups):
                changed.append(event)
                if kept: hooks[event] = kept
                else: hooks.pop(event, None)
        elif not any(ours(g) for g in groups):
            group = {"hooks": [{"type": "command", "command": HOOK_CMD, "async": True, "timeout": 5}]}
            if matcher: group = {"matcher": matcher, **group}
            hooks[event] = groups + [group]
            changed.append(event)
    if not hooks:
        settings.pop("hooks")

    line = settings.get("statusLine") or {}
    if args.uninstall:
        if line and is_our_status(line, status_cmd):
            settings.pop("statusLine"); changed.append("statusLine")
    else:
        os.makedirs(RUNTIME, exist_ok=True)
        shutil.copy2(os.path.join(ROOT, "tools", status_src), status_file)
        if not IS_WIN:
            os.chmod(status_file, 0o755)
        if IS_WIN and not status_cmd:
            print(f"No status line added: a path has a space in it. To feed Lump your usage from CLI sessions,\n"
                  f"set your status line to run {status_file} with Python (the desktop app needs none).")
        elif not line:
            settings["statusLine"] = {"type": "command", "command": status_cmd}; changed.append("statusLine")
        elif not is_our_status(line, status_cmd):
            print(f"You already have a status line ({line.get('command')}), so it was left alone.\n"
                  f"Lump's usage colour needs {status_file} to see the status line JSON: call it from yours.")

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        # Windows keeps non-ASCII text as it was (a settings.json shared through git then only shows the hooks
        # in its diff); the Mac output stays exactly as it has always been.
        json.dump(settings, f, indent=2, **({"ensure_ascii": False} if IS_WIN else {}))
        f.write("\n")
    verb = "Removed" if args.uninstall else "Added"
    print(f"{verb}: {', '.join(changed)}" if changed else "Nothing to change.")
    print(f"({path})")


if __name__ == "__main__":
    sys.exit(main())
