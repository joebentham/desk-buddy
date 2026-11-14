"""Run production firmware paths with ASan/UBSan and inert SDK/graphics mocks."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("host_shrink_art", ROOT / "tools/shrink_art.py")
art = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = art
spec.loader.exec_module(art)


@pytest.fixture(scope="session")
def firmware_host(tmp_path_factory):
    binary = tmp_path_factory.mktemp("firmware-host") / "check"
    result = subprocess.run(["g++", "-std=c++17", "-O1", "-g", "-fsanitize=address,undefined",
                    "-fno-omit-frame-pointer", "-fno-pie", "-no-pie",
                    "-I", str(ROOT / "tests/firmware_host"),
                    str(ROOT / "tests/firmware_host/check.cpp"), "-o", str(binary)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


@pytest.fixture(scope="session", params=["buddy", "esp32-32e"])
def recovery_host(request, tmp_path_factory):
    binary = tmp_path_factory.mktemp("firmware-recovery") / "check"
    defines = ["-DBOARD_ESP32_32E=1"] if request.param == "esp32-32e" else []
    result = subprocess.run(["g++", "-std=c++17", "-O1", "-g", "-fsanitize=address,undefined",
                             "-fno-omit-frame-pointer", "-fno-pie", "-no-pie", *defines,
                             "-I", str(ROOT / "tests/firmware_host"),
                             str(ROOT / "tests/firmware_host/check_recovery.cpp"), "-o", str(binary)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary, request.param


@pytest.mark.parametrize("art_state", ["missing", "corrupt"])
def test_recovery_screen_and_boot_message_match_board(recovery_host, tmp_path, art_state):
    binary, board = recovery_host
    path = tmp_path / "art.bin"
    if art_state == "corrupt":
        path.write_bytes(b"not a valid art header")
    command = "tools/flash.sh esp32-32e" if board == "esp32-32e" else "tools/ota.py --art"
    result = subprocess.run([str(binary), str(path), command], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS recovery" in result.stdout


def run(firmware_host, scenario, path=None):
    result = subprocess.run([str(firmware_host), scenario, *([str(path)] if path else [])],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"PASS {scenario}" in result.stdout


def test_touch_rounded_spi_read_has_initialized_padding(firmware_host):
    run(firmware_host, "touch")


def test_panel_parser_rejects_partial_out_of_range_and_trailing_input(firmware_host):
    run(firmware_host, "panel-parse")


@pytest.mark.parametrize("scenario", ["band", "band-redraw", "offline-counts", "touch-feedback"])
def test_portrait_panel_band_tuning_and_accepted_press(firmware_host, scenario):
    run(firmware_host, scenario, ROOT / "firmware/data/lump-esp32-32e.bin")


@pytest.mark.parametrize("art_state", ["missing", "corrupt"])
def test_panel_tuning_restores_recovery_message_without_art(firmware_host, tmp_path, art_state):
    path = tmp_path / "art.bin"
    if art_state == "corrupt":
        path.write_bytes(b"not a valid art header")
    run(firmware_host, "recovery-tuning", path)


@pytest.mark.parametrize("mood", ["idle", "working", "alert", "done", "waiting", "longhaul", "night"])
def test_damaged_required_mood_disables_animation_keeps_serial(firmware_host, tmp_path, mood):
    blob = (ROOT / "firmware/data/lump-esp32-32e.bin").read_bytes()
    # Equal length: every count, index, frame offset and RLE run remains valid.
    damaged = blob.replace(bytes([len(mood)]) + mood.encode(),
                           bytes([len(mood)]) + b"v" + mood[1:].encode(), 1)
    parsed = art.read_art(damaged)  # Prove this is structurally valid art.
    assert mood not in [name for _, name in parsed.moods]
    path = tmp_path / "damaged.bin"
    path.write_bytes(damaged)
    run(firmware_host, "bad-art", path)


def test_player_rejects_invalid_targets_and_failed_internal_lookups(firmware_host):
    run(firmware_host, "bounds", ROOT / "firmware/data/lump-esp32-32e.bin")


@pytest.mark.parametrize("scenario", ["boot-off", "boot-configured", "boot-no-ntp", "boot-online"])
def test_radio_deferred_until_after_usb_discovery(firmware_host, scenario):
    run(firmware_host, scenario, ROOT / "firmware/data/lump-esp32-32e.bin")
