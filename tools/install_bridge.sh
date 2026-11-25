#!/bin/bash
# Install (or update) the Desk Buddy bridge as a Linux user service or macOS login item.
#   com.deskbuddy.bridge    Python bridge: Claude Code hooks + Chat/Cowork state -> Lump over USB
#   com.deskbuddy.appwatch  small native helper that reads the Claude app's accessibility tree
#                           (Chat and Cowork have no hooks). Only this helper needs Accessibility.
# Runtime lives in ~/.desk-buddy (outside iCloud, so it's never evicted):
#   venv/  bridge.py  bridge_host.py  bridge.log  deskbuddy-appwatch (Mac only)
# Uninstall: tools/install_bridge.sh --uninstall
# Preview on Linux without changing anything: --dry-run, or --print-unit for the unit alone.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RT="$HOME/.desk-buddy"
OS="$(uname -s)"
MODE="${1:-}"
if [ "$#" -gt 1 ]; then echo "Usage: $0 [--dry-run|--print-unit|--uninstall]" >&2; exit 2; fi
case "$MODE" in
  ""|--dry-run|--print-unit|--uninstall) ;;
  --help|-h) echo "Usage: $0 [--dry-run|--print-unit|--uninstall]"; exit 0 ;;
  *) echo "Unknown option: $MODE" >&2; exit 2 ;;
esac

print_linux_unit() {
  cat <<'EOF'
[Unit]
Description=Desk Buddy bridge (Claude Code to Lump)
After=pipewire.service pipewire-pulse.service

[Service]
Type=simple
ExecStart="%h/.desk-buddy/venv/bin/python" -u "%h/.desk-buddy/bridge.py"
WorkingDirectory=%h/.desk-buddy
Environment="DESKBUDDY_HOME=%h/.desk-buddy"
Restart=on-failure
RestartSec=5
MemoryMax=300M
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
}

install_runtime() {
  mkdir -p "$RT"
  [ -x "$RT/venv/bin/python" ] || python3 -m venv "$RT/venv"
  "$RT/venv/bin/pip" -q install --disable-pip-version-check --upgrade pip
  "$RT/venv/bin/pip" -q install --disable-pip-version-check pyserial icalendar recurring-ical-events
  cp "$ROOT/tools/buddy_bridge.py" "$RT/bridge.py"
  cp "$ROOT/tools/bridge_host.py" "$RT/bridge_host.py"
  # Render the voice and gag WAVs; create sounds.json only on the first install.
  python3 "$ROOT/tools/make_sounds.py"
  python3 "$ROOT/tools/make_gag_sounds.py"
}

if [ "$OS" = Linux ]; then
  SERVICE="deskbuddy-bridge.service"
  UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  UNIT="$UNIT_DIR/$SERVICE"
  if [ "$MODE" = --print-unit ]; then print_linux_unit; exit 0; fi
  if [ "$MODE" = --dry-run ]; then
    echo "Would create $RT/venv and install pyserial, icalendar and recurring-ical-events."
    echo "Would copy the bridge and bridge_host.py to $RT and render sounds there."
    echo "Would write $UNIT with the following contents:"
    print_linux_unit
    echo "Would run: systemctl --user daemon-reload"
    echo "Would run: systemctl --user enable $SERVICE"
    echo "Would run: systemctl --user restart $SERVICE"
    echo "Requires dialout membership in this login session (re-login after adding it)."
    echo "Claude app watcher and the Lump menu bar app are macOS-only; skipped."
    exit 0
  fi
  if [ "$MODE" = --uninstall ]; then
    systemctl --user disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl --user daemon-reload
    echo "Bridge service removed (runtime left in $RT)."
    exit 0
  fi
  if ! id -nG | tr ' ' '\n' | grep -qx dialout; then
    echo "USB serial needs dialout membership; log out and back in after adding it." >&2
  fi
  install_runtime
  mkdir -p "$UNIT_DIR"
  print_linux_unit > "$UNIT"
  systemctl --user daemon-reload
  systemctl --user enable "$SERVICE"
  systemctl --user restart "$SERVICE"
  echo "Bridge running as $SERVICE. Logs: $RT/bridge.log or journalctl --user -u $SERVICE"
  echo "Claude app watcher and the Lump menu bar app are macOS-only; skipped."
  exit 0
fi

if [ "$OS" != Darwin ]; then echo "Unsupported host: $OS" >&2; exit 1; fi
if [ "$MODE" = --print-unit ]; then echo "--print-unit is Linux-only." >&2; exit 2; fi
if [ "$MODE" = --dry-run ]; then
  echo "Would install the runtime in $RT and the bridge/app watcher as launchd login items."
  exit 0
fi

LABEL="com.deskbuddy.bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
WLABEL="com.deskbuddy.appwatch"
WPLIST="$HOME/Library/LaunchAgents/$WLABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/$WLABEL" 2>/dev/null || true
if [ "$MODE" = --uninstall ]; then
  rm -f "$PLIST" "$WPLIST"
  echo "Bridge and app watcher removed (runtime left in $RT)."
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
install_runtime

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$RT/venv/bin/python</string><string>-u</string><string>$RT/bridge.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$RT/bridge.out</string>
  <key>StandardErrorPath</key><string>$RT/bridge.out</string>
</dict></plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# Build the app watcher. macOS ties its Accessibility permission to the signature. If Claude Switcher's
# local signing certificate exists (its build.sh makes one), sign with that: every build then keeps the
# permission. Otherwise it's an ad hoc signature, which changes with every build, so the installed
# binary is only replaced when the program changed (and you re-allow it in Accessibility).
NEW="$RT/deskbuddy-appwatch.new"
swiftc -O -swift-version 5 "$ROOT/tools/appwatch/main.swift" -o "$NEW"
SIGNING="$HOME/Library/Application Support/Claude Switcher Signing"
if [ -f "$SIGNING/signing.keychain-db" ] && [ -f "$SIGNING/password" ]; then
  KC="$SIGNING/signing.keychain-db"
  security unlock-keychain -p "$(cat "$SIGNING/password")" "$KC"
  ID="$(security find-certificate -c "Claude Switcher Local" -Z "$KC" | awk '/SHA-1/ {print $NF}')"
  # codesign only finds an identity in a keychain on the search list: add it just while signing
  OLD_LIST=()
  while IFS= read -r k; do OLD_LIST+=("$k"); done < <(security list-keychains -d user | sed -E 's/^ *"(.*)"$/\1/')
  security list-keychains -d user -s "${OLD_LIST[@]}" "$KC"
  SIGNED=0; codesign --force --sign "$ID" --identifier "$WLABEL" "$NEW" && SIGNED=1
  security list-keychains -d user -s "${OLD_LIST[@]}"
  [ "$SIGNED" = 1 ] || { echo "signing the app watcher failed"; exit 1; }
  mv "$NEW" "$RT/deskbuddy-appwatch"
elif cmp -s "$NEW" "$RT/deskbuddy-appwatch"; then rm -f "$NEW"; else mv "$NEW" "$RT/deskbuddy-appwatch"; fi
cat > "$WPLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$WLABEL</string>
  <key>ProgramArguments</key><array><string>$RT/deskbuddy-appwatch</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$RT/appwatch.out</string>
  <key>StandardErrorPath</key><string>$RT/appwatch.out</string>
</dict></plist>
EOF
launchctl bootstrap "gui/$(id -u)" "$WPLIST"

sleep 3
curl -s -m 2 http://127.0.0.1:47823/status && echo
echo "Bridge and app watcher running. If macOS asks, allow deskbuddy-appwatch under"
echo "System Settings > Privacy & Security > Accessibility."
