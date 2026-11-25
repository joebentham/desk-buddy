#!/bin/bash
# USB recovery/install. Default buddy (S3); select esp32-32e for the classic ESP32.
# BACKUP selects a full recovery image for esp32-32e; NO_BACKUP=1 skips that check.
# ART=0 skips art, SKIP_BUILD=1 uses an already-built program, PACED=1 forces pacing.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BOARD="${1:-${BOARD:-buddy}}"
VENV="${VENV:-$ROOT/tools/.venv}"
PIO="${PIO:-$ROOT/tools/.pio-venv/bin/pio}"
[ -x "$PIO" ] || PIO=pio
OS="$(uname -s)"
HEAVY_LOCK="${HEAVY_LOCK:-/tmp/deskbuddy-heavy.lock}"
locked() {
  if command -v flock >/dev/null; then flock "$HEAVY_LOCK" "$@"; else "$@"; fi
}

case "$BOARD" in
  esp32-32e)
    CHIP=esp32; FLASH_SIZE=4MB; BOOT_ADDR=0x1000; ART_ADDR=0x180000; ART_ROOM=$((0x270000))
    ART_FILE="$ROOT/firmware/data/lump-esp32-32e.bin"
    PACED="${PACED:-0}"
    ;;
  buddy)
    CHIP=esp32s3; FLASH_SIZE=16MB; BOOT_ADDR=0x0; ART_ADDR=0x410000; ART_ROOM=$((0xBE0000))
    ART_FILE="$ROOT/firmware/data/lump.bin"; PACED="${PACED:-1}"
    ;;
  *) echo "Usage: tools/flash.sh [buddy|esp32-32e]" >&2; exit 2 ;;
esac
if [ -z "${PORT:-}" ]; then
  shopt -s nullglob
  if [ "$OS" = Darwin ]; then
    # Match bridge_host.usb_ports(): keep S3 modem ordering before USB adapters.
    PORTS=(/dev/cu.usbmodem* /dev/cu.usbserial-* /dev/cu.wchusbserial*)
  elif [ "$BOARD" = esp32-32e ]; then
    PORTS=(/dev/ttyUSB0)
  else
    PORTS=(/dev/ttyACM* /dev/ttyUSB*)
  fi
  PORT="${PORTS[0]:-}"
fi
[ -n "$PORT" ] || { echo "No USB serial port found for $BOARD; plug in a data cable or set PORT explicitly." >&2; exit 1; }
[ -e "$PORT" ] || { echo "USB serial port missing: $PORT; check the data cable and PORT setting." >&2; exit 1; }
if [ "$BOARD" = esp32-32e ]; then
  # Check before building or pausing the bridge, including with ART=0.
  if [ "${NO_BACKUP:-0}" = 1 ]; then
    echo "Skipping recovery backup check (NO_BACKUP=1)."
  elif [ -z "${BACKUP:-}" ]; then
    printf -v READ_CMD '%q ' "$VENV/bin/python" -I -m esptool --chip "$CHIP" --port "$PORT" \
      --baud "${BAUD:-921600}" read-flash 0 0x400000 esp32-32e-backup.bin
    if [ "$OS" = Linux ]; then printf -v READ_CMD 'sg dialout -c %q' "$READ_CMD"; fi
    echo "STOP: set BACKUP to a full 4 MB recovery image before flashing ESP32-32E." >&2
    echo "Install esptool in $VENV first, and close other serial tools (including the bridge)." >&2
    printf 'Read the backup:\n  %s\n' "$READ_CMD" >&2
    echo "Then set BACKUP=esp32-32e-backup.bin when running this script." >&2
    echo "NO_BACKUP=1 skips the backup check." >&2
    exit 1
  else
    [ -f "$BACKUP" ] && [ -r "$BACKUP" ] || { echo "STOP: backup is not a readable file: $BACKUP" >&2; exit 1; }
    BACKUP_BYTES="$(wc -c < "$BACKUP")"
    [ "$BACKUP_BYTES" -eq 4194304 ] || { echo "STOP: backup must be exactly 4194304 bytes (4 MB): $BACKUP" >&2; exit 1; }
    echo "Recovery backup: $BACKUP (4194304 bytes); SHA-256:"
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum -- "$BACKUP"
    else
      shasum -a 256 -- "$BACKUP"
    fi
  fi
fi
[ -x "$VENV/bin/python" ] || { /usr/bin/python3 -m venv "$VENV"; "$VENV/bin/pip" install esptool; }

# the bridge holds the serial port: pause it while flashing, restart it afterwards
if [ "$(uname -s)" = Linux ]; then
  if command -v systemctl >/dev/null 2>&1; then
    bridge_state_error() {
      echo 'Cannot safely pause deskbuddy-bridge.service: systemd state is unreadable or ambiguous; refusing to use the board.' >&2
      exit 1
    }
    for BRIDGE_ATTEMPT in 1 2; do
      BRIDGE_STATE="$(systemctl --user show --property=LoadState,ActiveState,SubState,Job --all deskbuddy-bridge.service 2>/dev/null)" || bridge_state_error
      BRIDGE_LOAD= BRIDGE_ACTIVE= BRIDGE_SUB= BRIDGE_JOB=missing BRIDGE_INTENT=
      while IFS='=' read -r key value; do
        case "$key" in
          LoadState) BRIDGE_LOAD="$value" ;;
          ActiveState) BRIDGE_ACTIVE="$value" ;;
          SubState) BRIDGE_SUB="$value" ;;
          Job) BRIDGE_JOB="${value%% *}"; BRIDGE_JOB="${BRIDGE_JOB:-0}" ;;
        esac
      done <<< "$BRIDGE_STATE"
      case "$BRIDGE_LOAD" in
        loaded|not-found) ;;
        *) bridge_state_error ;;
      esac
      [ -n "$BRIDGE_SUB" ] || bridge_state_error
      case "$BRIDGE_ACTIVE" in
        active|reloading|activating|deactivating|inactive|failed) ;;
        *) bridge_state_error ;;
      esac
      case "$BRIDGE_JOB" in
        *[!0-9]*|'') bridge_state_error ;;
      esac
      if [ "$BRIDGE_LOAD" = not-found ]; then
        [ "$BRIDGE_ACTIVE:$BRIDGE_SUB:$BRIDGE_JOB" = inactive:dead:0 ] || bridge_state_error
        break
      fi
      if [ "$BRIDGE_JOB" != 0 ]; then
        if ! BRIDGE_JOB_TYPE="$(systemctl --user show --property=JobType --value "$BRIDGE_JOB" 2>/dev/null)"; then
          # A completed job loses its object. Re-read ALL unit state once; never guess.
          [ "$BRIDGE_ATTEMPT" = 1 ] || bridge_state_error
          continue
        fi
        case "$BRIDGE_JOB_TYPE" in
          stop) BRIDGE_INTENT=stop ;;    # pending intent wins even while the unit is active
          start|restart|try-restart|reload-or-start|reload) BRIDGE_INTENT=run ;;
          *) bridge_state_error ;;
        esac
      else
        case "$BRIDGE_ACTIVE:$BRIDGE_SUB" in
          active:*|reloading:*|activating:*|*:auto-restart|*:auto-restart-queued) BRIDGE_INTENT=run ;;
          deactivating:*) BRIDGE_INTENT=stop ;;
          inactive:dead|inactive:failed|failed:dead|failed:failed) ;;
          *) bridge_state_error ;;
        esac
      fi
      break
    done
    if [ -n "$BRIDGE_INTENT" ]; then
      bridge_cleanup() {
        trap '' HUP INT TERM           # repeated termination must let restoration finish
        local status="$1" resume_status
        trap - EXIT
        if [ "$BRIDGE_INTENT" = run ]; then
          systemctl --user start deskbuddy-bridge.service || {
            resume_status=$?
            echo 'Failed to resume deskbuddy-bridge.service.' >&2
            if [ "$status" = 0 ]; then status="$resume_status"; fi
          }
        fi
        exit "$status"
      }
      # Arm cleanup BEFORE the interruptible stop, including a partially failed stop.
      trap 'bridge_cleanup "$?"' EXIT
      trap 'exit 129' HUP
      trap 'exit 130' INT
      trap 'exit 143' TERM
      # A checked synchronous stop also waits for queued stops and cancels activation.
      systemctl --user stop deskbuddy-bridge.service
      sleep 1
    fi
  fi
elif [ "$(uname -s)" = Darwin ]; then
  BRIDGE="$HOME/Library/LaunchAgents/com.deskbuddy.bridge.plist"
  if [ -f "$BRIDGE" ]; then
    launchctl bootout "gui/$(id -u)/com.deskbuddy.bridge" 2>/dev/null || true
    trap 'launchctl bootstrap "gui/$(id -u)" "$BRIDGE" 2>/dev/null || true' EXIT
    sleep 1
  fi
fi

# Build and validate the selected board images before uploading.

if [ "${ART:-1}" != 0 ] && [ "$BOARD" = esp32-32e ]; then /usr/bin/python3 "$ROOT/tools/shrink_art.py"; fi
if [ "${SKIP_BUILD:-0}" != 1 ]; then locked "$PIO" run -d "$ROOT/firmware" -e "$BOARD" -j 1; fi
B="$ROOT/firmware/.pio/build/$BOARD"
if [ "$BOARD" = esp32-32e ]; then APP_ROOM=$((0x170000)); else APP_ROOM=$((0x200000)); fi
/usr/bin/python3 - "$B/firmware.bin" "$APP_ROOM" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
if not p.is_file() or not 0 < p.stat().st_size <= int(sys.argv[2]):
    sys.exit("STOP: program missing or too big for slot")
PY
EXTRA=()
if [ "$BOARD" = buddy ]; then
  BOOT_APP0="$HOME/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin"
  EXTRA+=(0xe000 "$BOOT_APP0")
fi
if [ "${ART:-1}" != 0 ]; then
  /usr/bin/python3 - "$ART_FILE" "$ART_ROOM" <<'PY'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
if not p.is_file() or not 0 < p.stat().st_size <= int(sys.argv[2]):
    sys.exit("STOP: drawings missing or too big for partition")
PY
  EXTRA+=("$ART_ADDR" "$ART_FILE")
fi
if [ "$PACED" = 1 ]; then
  UPLOADER=("$VENV/bin/python" -I "$ROOT/tools/paced_esptool.py")
  STUB=(--no-stub)
else
  UPLOADER=("$VENV/bin/python" -I -m esptool)
  STUB=()
fi
CMD=("${UPLOADER[@]}" --chip "$CHIP" --port "$PORT" ${STUB[@]+"${STUB[@]}"} --baud "${BAUD:-921600}"
     write_flash --flash_mode dio --flash_size "$FLASH_SIZE"
     "$BOOT_ADDR" "$B/bootloader.bin" 0x8000 "$B/partitions.bin" 0x10000 "$B/firmware.bin" ${EXTRA[@]+"${EXTRA[@]}"})
if [ "$OS" = Linux ]; then
  # Bash-quote each argument for the shell sg uses. Always acquire dialout.
  printf -v SERIAL_CMD '%q ' "${CMD[@]}"
  locked sg dialout -c "$SERIAL_CMD"
else
  locked "${CMD[@]}"
fi
