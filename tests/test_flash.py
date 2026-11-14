"""Run shell routing and backup checks with synthetic files, never a serial port."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

MOCK = r'''#!/usr/bin/python3
import json, os, pathlib, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
with open(os.environ["CALL_LOG"], "a") as f:
    f.write(json.dumps([name, *sys.argv[1:]]) + "\n")
if name == "uname":
    print(os.environ.get("MOCK_OS", "Linux"))
elif name in ("sha256sum", "shasum"):
    sys.exit(subprocess.call([os.environ["TEST_HASH_TOOL"], *sys.argv[1:]]))
elif name == "systemctl":
    action = sys.argv[2]
    if action == "show":
        if "--property=JobType" in sys.argv:
            print(os.environ.get("MOCK_JOB_TYPE", "start"))
        else:
            print(os.environ.get("MOCK_BRIDGE_STATE", "LoadState=not-found\nActiveState=inactive\nSubState=dead\nJob=0"))
    elif os.environ.get("MOCK_PAUSE_FILE"):
        paused = pathlib.Path(os.environ["MOCK_PAUSE_FILE"])
        if action == "stop":
            paused.touch()
        elif action == "start":
            paused.unlink()
elif name == "flock":
    sys.exit(subprocess.call(sys.argv[2:]))
elif name == "sg":
    assert sys.argv[1:3] == ["dialout", "-c"]
    sys.exit(subprocess.call(["/bin/bash", "-c", sys.argv[3]]))
elif name == "python":
    if os.environ.get("MOCK_PAUSE_FILE"):
        assert pathlib.Path(os.environ["MOCK_PAUSE_FILE"]).exists(), "upload before bridge pause"
    sys.exit(int(os.environ.get("MOCK_UPLOAD_EXIT", "0")))
'''


@pytest.fixture
def flash(tmp_path):
    scripts, commands = tmp_path / "tools", tmp_path / "commands"
    scripts.mkdir(); commands.mkdir()
    backup = tmp_path / "synthetic-recovery.bin"
    with backup.open("wb") as f:
        f.truncate(4194304)  # Sparse synthetic image; no hardware backup is read.
    source = (ROOT / "tools/flash.sh").read_text()
    (scripts / "flash.sh").write_text(source)
    log = tmp_path / "calls.jsonl"
    hash_tool = shutil.which("sha256sum") or shutil.which("shasum")
    assert hash_tool, "backup tests require sha256sum or shasum"
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
           "HOME": str(tmp_path / "home"), "BACKUP": str(backup),
           "VENV": str(tmp_path / "venv"), "PIO": str(commands / "pio"),
           "HEAVY_LOCK": str(tmp_path / "heavy.lock"), "CALL_LOG": str(log), "TEST_HASH_TOOL": hash_tool,
           "PORT": str(tmp_path / "serial-mock")}
    # Keep developer shell settings from changing the script's defaults.
    for key in ("BOARD", "ART", "SKIP_BUILD", "PACED", "BAUD", "DESKBUDDY_PORT", "NO_BACKUP",
                "MOCK_OS", "MOCK_BRIDGE_STATE", "MOCK_JOB_TYPE",
                "MOCK_PAUSE_FILE", "MOCK_UPLOAD_EXIT"):
        env.pop(key, None)
    Path(env["PORT"]).touch()
    for name in ("uname", Path(hash_tool).name, "systemctl", "launchctl", "flock", "sg", "pio", "sleep"):
        p = commands / name
        p.write_text(MOCK); p.chmod(0o755)
    uploader = tmp_path / "venv/bin/python"
    uploader.parent.mkdir(parents=True)
    uploader.write_text(MOCK); uploader.chmod(0o755)
    # shrink_art is an external command here; routing tests don't regenerate art.
    (scripts / "shrink_art.py").write_text(MOCK)
    for board in ("buddy", "esp32-32e"):
        build = tmp_path / "firmware/.pio/build" / board
        build.mkdir(parents=True)
        for name in ("bootloader.bin", "partitions.bin", "firmware.bin"):
            (build / name).write_bytes(b"mock-image")
    data = tmp_path / "firmware/data"
    data.mkdir()
    for name in ("lump.bin", "lump-esp32-32e.bin"):
        (data / name).write_bytes(b"mock-art")

    def run(board=None, **overrides):
        result = subprocess.run(["bash", str(scripts / "flash.sh"), *([board] if board else [])],
                                env={k: v for k, v in {**env, **overrides}.items() if v is not None},
                                capture_output=True, text=True, timeout=10)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls
    return run, backup, tmp_path


def uploader_call(calls):
    return next(c for c in calls if c[0] == "python")


@pytest.mark.parametrize("board,chip,size,boot,art_addr,art_name", [
    ("buddy", "esp32s3", "16MB", "0x0", "0x410000", "lump.bin"),
    ("esp32-32e", "esp32", "4MB", "0x1000", "0x180000", "lump-esp32-32e.bin"),
])
def test_flash_routes_build_and_partitions_per_board(flash, board, chip, size, boot, art_addr, art_name):
    run, _, root = flash
    result, calls = run(board)
    assert result.returncode == 0, result.stderr
    assert ["pio", "run", "-d", str(root / "firmware"), "-e", board, "-j", "1"] in calls
    args = uploader_call(calls)
    assert args[args.index("--chip") + 1] == chip
    assert args[args.index("--flash_size") + 1] == size
    assert args[args.index(boot) + 1] == str(root / f"firmware/.pio/build/{board}/bootloader.bin")
    assert args[args.index(art_addr) + 1] == str(root / "firmware/data" / art_name)
    assert ("0xe000" in args) == (board == "buddy")
    assert any(c[0] == "sg" for c in calls)
    if board == "esp32-32e":
        assert "Recovery backup:" in result.stdout
    assert sum(c[0] == "flock" for c in calls) == 2


@pytest.mark.parametrize("damage", ["missing", "directory", "short", "long"])
@pytest.mark.parametrize("art", ["0", "1"])
def test_classic_backup_rejected_before_build_or_upload(flash, damage, art):
    run, backup, _ = flash
    if damage == "missing":
        backup.unlink()
    elif damage == "directory":
        backup.unlink()
        backup.mkdir()
    else:
        with backup.open("r+b") as f:
            f.truncate(4194303 if damage == "short" else 4194305)
    result, calls = run("esp32-32e", ART=art)
    assert result.returncode != 0
    assert ("backup is not a readable file" if damage in ("missing", "directory") else
            "backup must be exactly 4194304 bytes") in result.stderr
    assert not any(c[0] in ("systemctl", "pio", "python", "sg", "shrink_art.py") for c in calls)


@pytest.mark.parametrize("os_name", ["Linux", "Darwin"])
@pytest.mark.parametrize("art", ["0", "1"])
def test_classic_without_backup_explains_exact_read_command(flash, os_name, art):
    run, _, root = flash
    port = root / "serial port; $example"
    port.touch()
    result, calls = run("esp32-32e", BACKUP=None, PORT=str(port), MOCK_OS=os_name, ART=art)
    assert result.returncode == 1
    assert "set BACKUP" in result.stderr and "NO_BACKUP=1 skips" in result.stderr
    command = result.stderr.split("Read the backup:\n  ", 1)[1].splitlines()[0]
    args = shlex.split(command)
    if os_name == "Linux":
        assert args[:3] == ["sg", "dialout", "-c"]
        args = shlex.split(args[3])
    assert args == [str(root / "venv/bin/python"), "-I", "-m", "esptool", "--chip", "esp32",
                    "--port", str(port), "--baud", "921600", "read-flash", "0", "0x400000",
                    "esp32-32e-backup.bin"]
    assert not any(c[0] in ("systemctl", "pio", "python", "sg", "shrink_art.py") for c in calls)


def test_classic_accepts_owner_backup_and_prints_its_digest(flash):
    run, backup, root = flash
    with backup.open("r+b") as f:
        f.write(b"owner recovery image")
    chosen = root / "backup with spaces.bin"
    backup.rename(chosen)
    result, calls = run("esp32-32e", BACKUP=str(chosen))
    assert result.returncode == 0, result.stderr
    assert str(chosen) in result.stdout
    assert hashlib.sha256(chosen.read_bytes()).hexdigest() in result.stdout
    assert uploader_call(calls)


@pytest.mark.parametrize("backup", [None, "missing.bin"])
def test_classic_backup_check_has_an_explicit_bypass(flash, backup):
    result, calls = flash[0]("esp32-32e", BACKUP=backup, NO_BACKUP="1", ART="0")
    assert result.returncode == 0, result.stderr
    assert "Skipping recovery backup check (NO_BACKUP=1)" in result.stdout
    assert not any(c[0] in ("sha256sum", "shasum") for c in calls)
    assert uploader_call(calls)


@pytest.mark.parametrize("board", ["buddy", "esp32-32e"])
def test_art_zero_and_skip_build_omit_art_and_build(flash, board):
    run, _, root = flash
    for p in (root / "firmware/data").iterdir():
        p.unlink()  # ART=0 must also work without art files.
    result, calls = run(board, ART="0", SKIP_BUILD="1")
    assert result.returncode == 0, result.stderr
    assert not any(c[0] in ("pio", "shrink_art.py") for c in calls)
    args = uploader_call(calls)
    assert not any("lump" in arg or arg in ("0x180000", "0x410000") for arg in args)
    if board == "esp32-32e":
        assert "Recovery backup:" in result.stdout
    assert sum(c[0] == "flock" for c in calls) == 1


@pytest.mark.parametrize("board,paced", [("buddy", True), ("esp32-32e", False)])
def test_default_uploader_selection(flash, board, paced):
    result, calls = flash[0](board, SKIP_BUILD="1")
    assert result.returncode == 0, result.stderr
    args = uploader_call(calls)
    assert ("--no-stub" in args) == paced
    assert any(arg.endswith("paced_esptool.py") for arg in args) == paced
    assert ("-m" in args and "esptool" in args) == (not paced)


@pytest.mark.parametrize("board,paced", [("buddy", False), ("esp32-32e", True)])
def test_uploader_override(flash, board, paced):
    result, calls = flash[0](board, PACED=str(int(paced)), SKIP_BUILD="1")
    assert result.returncode == 0, result.stderr
    args = uploader_call(calls)
    assert ("--no-stub" in args) == paced
    assert any(arg.endswith("paced_esptool.py") for arg in args) == paced


def test_macos_buddy_uses_paced_uploader_directly(flash):
    result, calls = flash[0]("buddy", MOCK_OS="Darwin", SKIP_BUILD="1")
    assert result.returncode == 0, result.stderr
    args = uploader_call(calls)
    assert "--no-stub" in args and any(arg.endswith("paced_esptool.py") for arg in args)
    assert not any(c[0] == "sg" for c in calls)


def test_classic_accepts_an_explicit_serial_path(flash):
    run, _, root = flash
    port = root / "serial by-id alias"
    port.touch()
    result, calls = run("esp32-32e", PORT=str(port))
    assert result.returncode == 0, result.stderr
    args = uploader_call(calls)
    assert args[args.index("--port") + 1] == str(port)


def test_default_board_is_buddy(flash):
    result, calls = flash[0](ART="0", SKIP_BUILD="1")
    assert result.returncode == 0, result.stderr
    args = uploader_call(calls)
    assert args[args.index("--chip") + 1] == "esp32s3"


def test_unknown_board_rejected(flash):
    result, calls = flash[0]("unknown")
    assert result.returncode == 2
    assert not any(c[0] in ("pio", "python", "sg") for c in calls)


@pytest.fixture
def full_flash(flash):
    """Run every shell step with fake devices and build/upload/service commands.

    Backup checks still read only the fixture's synthetic image.
    """
    run, _, root = flash
    dev = root / "fake-dev"
    dev.mkdir()
    (dev / "ttyUSB0").touch()
    script = root / "tools/flash.sh"
    source = script.read_text()
    for prefix in ("ttyUSB", "ttyACM", "cu.usbmodem", "cu.usbserial-", "cu.wchusbserial"):
        source = source.replace("/dev/" + prefix, str(dev / prefix))
    python = root / "commands/python3"
    source = source.replace("/usr/bin/python3", shlex.quote(str(python)))
    script.write_text(source)
    python.write_text(r'''#!/usr/bin/python3
import json, os, pathlib, subprocess, sys
with open(os.environ["CALL_LOG"], "a") as log:
    if sys.argv[1] == "-":
        source = sys.stdin.read()
        # Keep the real program/art size checks on synthetic files.
        log.write(json.dumps(["size-check", *sys.argv[2:]]) + "\n")
        log.flush()
        sys.exit(subprocess.run(["/usr/bin/python3", *sys.argv[1:]], input=source, text=True).returncode)
    assert pathlib.Path(sys.argv[1]).name == "shrink_art.py"
    log.write(json.dumps(["shrink_art.py"]) + "\n")
''')
    python.chmod(0o755)

    def run_full(board="esp32-32e", **overrides):
        variables = {"PORT": None,
                     "MOCK_BRIDGE_STATE": "LoadState=loaded\nActiveState=active\nSubState=running\nJob=0",
                     "MOCK_PAUSE_FILE": str(root / "paused"), **overrides}
        return run(board, **variables)
    return run_full, root, dev


def test_full_classic_discovers_default_and_pauses_around_correct_write(full_flash):
    run, root, dev = full_flash
    result, calls = run()
    assert result.returncode == 0, result.stdout + result.stderr
    args = uploader_call(calls)
    assert args[1:4] == ["-I", "-m", "esptool"]
    assert args[args.index("--port") + 1] == str(dev / "ttyUSB0")
    assert args[args.index("--chip") + 1] == "esp32"
    assert args[args.index("--flash_size") + 1] == "4MB"
    for offset, name in (("0x1000", "bootloader.bin"), ("0x8000", "partitions.bin"),
                         ("0x10000", "firmware.bin")):
        assert args[args.index(offset) + 1] == str(root / "firmware/.pio/build/esp32-32e" / name)
    assert args[args.index("0x180000") + 1] == str(root / "firmware/data/lump-esp32-32e.bin")
    assert "0xe000" not in args and "--no-stub" not in args
    build = ["pio", "run", "-d", str(root / "firmware"), "-e", "esp32-32e", "-j", "1"]
    assert build in calls
    assert sum(c[0] == "flock" for c in calls) == 2
    sg = next(c for c in calls if c[0] == "sg")
    assert sg[1:3] == ["dialout", "-c"]
    actions = [c[2] for c in calls if c[0] == "systemctl"]
    assert actions == ["show", "stop", "start"]
    backup = next(c for c in calls if c[0] in ("sha256sum", "shasum"))
    stop = ["systemctl", "--user", "stop", "deskbuddy-bridge.service"]
    start = ["systemctl", "--user", "start", "deskbuddy-bridge.service"]
    assert calls.index(backup) < calls.index(stop) < calls.index(build) < calls.index(args) < calls.index(start)
    assert not (root / "paused").exists()


@pytest.mark.parametrize("device", ["ttyUSB2", "ttyACM1"])
def test_full_buddy_discovers_linux_ports(full_flash, device):
    run, _, dev = full_flash
    (dev / "ttyUSB0").unlink()
    (dev / device).touch()
    result, calls = run("buddy")
    assert result.returncode == 0, result.stderr
    args = uploader_call(calls)
    assert args[args.index("--port") + 1] == str(dev / device)
    assert args[args.index("--chip") + 1] == "esp32s3"


@pytest.mark.parametrize("board", ["buddy", "esp32-32e"])
@pytest.mark.parametrize("device", ["cu.usbmodem1", "cu.usbserial-0001", "cu.wchusbserial0001"])
def test_full_flash_discovers_macos_modems_and_adapters(full_flash, board, device):
    run, _, dev = full_flash
    (dev / device).touch()
    result, calls = run(board, MOCK_OS="Darwin", MOCK_PAUSE_FILE=None)
    assert result.returncode == 0, result.stderr
    args = uploader_call(calls)
    assert args[args.index("--port") + 1] == str(dev / device)
    assert args[args.index("--chip") + 1] == ("esp32s3" if board == "buddy" else "esp32")
    assert not any(c[0] == "sg" for c in calls)


@pytest.mark.parametrize("board", ["buddy", "esp32-32e"])
def test_full_macos_discovery_keeps_first_s3_modem_before_adapters(full_flash, board):
    run, _, dev = full_flash
    for device in ("cu.usbmodem2", "cu.usbmodem1", "cu.usbserial-0001", "cu.wchusbserial0001"):
        (dev / device).touch()
    result, calls = run(board, MOCK_OS="Darwin", MOCK_PAUSE_FILE=None)
    assert result.returncode == 0, result.stderr
    args = uploader_call(calls)
    assert args[args.index("--port") + 1] == str(dev / "cu.usbmodem1")


@pytest.mark.parametrize("board,os_name", [("esp32-32e", "Linux"), ("esp32-32e", "Darwin"),
                                         ("buddy", "Linux"), ("buddy", "Darwin")])
def test_full_missing_port_has_diagnostic_before_any_side_effect(full_flash, board, os_name):
    run, _, dev = full_flash
    (dev / "ttyUSB0").unlink()
    result, calls = run(board, MOCK_OS=os_name)
    assert result.returncode == 1
    assert "USB serial port" in result.stderr and "data cable" in result.stderr
    assert not any(c[0] in ("sha256sum", "shasum", "systemctl", "pio", "sg", "python") for c in calls)


def test_full_upload_failure_restores_bridge_and_status(full_flash):
    run, root, _ = full_flash
    result, calls = run(MOCK_UPLOAD_EXIT="23")
    assert result.returncode == 23, result.stderr
    assert [c[2] for c in calls if c[0] == "systemctl"] == ["show", "stop", "start"]
    assert not (root / "paused").exists()


def test_full_queued_stop_is_completed_without_resuming(full_flash):
    run, root, _ = full_flash
    result, calls = run(MOCK_BRIDGE_STATE="LoadState=loaded\nActiveState=active\nSubState=running\nJob=73",
                        MOCK_JOB_TYPE="stop")
    assert result.returncode == 0, result.stderr
    assert [c[2] for c in calls if c[0] == "systemctl"] == ["show", "show", "stop"]
    assert uploader_call(calls)
    assert (root / "paused").exists()


def test_full_ambiguous_service_state_prevents_build_or_upload(full_flash):
    result, calls = full_flash[0](MOCK_BRIDGE_STATE="LoadState=loaded\nActiveState=active")
    assert result.returncode != 0 and "Cannot safely pause" in result.stderr
    assert not any(c[0] in ("pio", "sg", "python") for c in calls)


@pytest.mark.parametrize("board", ["esp32-32e", "buddy"])
@pytest.mark.parametrize("kind", ["program", "drawings"])
def test_oversized_images_prevent_upload(flash, board, kind):
    run, _, root = flash
    if kind == "program":
        path = root / f"firmware/.pio/build/{board}/firmware.bin"
        limit = 0x170000 if board == "esp32-32e" else 0x200000
    else:
        path = root / "firmware/data" / ("lump-esp32-32e.bin" if board == "esp32-32e" else "lump.bin")
        limit = 0x270000 if board == "esp32-32e" else 0xBE0000
    with path.open("r+b") as f:
        f.truncate(limit + 1)
    result, calls = run(board)
    assert result.returncode != 0 and f"{kind} missing or too big" in result.stderr
    assert not any(c[0] in ("sg", "python") for c in calls)
