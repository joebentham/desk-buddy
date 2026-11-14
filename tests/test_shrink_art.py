"""Offline art fidelity, sequence timing and corrupt-file rejection."""
import importlib.util
from pathlib import Path
import struct
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("shrink_art", ROOT / "tools/shrink_art.py")
art = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = art
spec.loader.exec_module(art)


def fixture_art():
    # Every sampled point is magenta, every unsampled point is another colour.
    pixels = [0xF81F if x % 2 == y % 2 == 0 else 0x1234 for y in range(240) for x in range(240)]
    return art.Art(240, 240, 24, [(0, "idle")], [
        art.Sequence(0, 0, 0, "idle", (0, 1, 0, 1, 0, 1, 0)),
        art.Sequence(1, 0, 0, "finish", (0, 0, 0, 0, 1)),
    ], [art.encode(pixels), art.encode([0x07E0] * 57600)])


def test_nearest_neighbour_preserves_marker_exactly():
    small = art.convert(fixture_art())
    frame = art.decode(small.frames[small.sequences[0].ticks[0]])
    assert len(frame) == 14400
    assert set(frame) == {0xF81F}


def test_duration_and_terminal_pose_are_retained():
    src = fixture_art()
    small = art.convert(src)
    for before, after in zip(src.sequences, small.sequences):
        assert 0 <= len(after.ticks) / small.fps - len(before.ticks) / src.fps < 1 / small.fps
    assert set(art.decode(small.frames[small.sequences[1].ticks[-1]])) == {0x07E0}


def test_v4_round_trip_and_file_length():
    small = art.convert(fixture_art())
    blob = art.write_art(small)
    assert struct.unpack_from("<I", blob, 14)[0] == len(blob)
    assert art.read_art(blob) == small


@pytest.mark.parametrize("damage", ["truncated", "zero_run", "overflow", "offset"])
def test_rejects_corrupt_art(damage):
    blob = bytearray(art.write_art(art.convert(fixture_art())))
    if damage == "truncated":
        blob.pop()
    elif damage == "zero_run":
        blob[-3] = 0
    elif damage == "overflow":
        blob[-3] = 255
    else:
        # Locate the offset table by subtracting the known frame lengths.
        small = art.convert(fixture_art())
        table = len(blob) - sum(map(len, small.frames)) - len(small.frames) * 4
        struct.pack_into("<I", blob, table, len(blob))
    with pytest.raises(ValueError):
        art.read_art(blob)


def test_board_art_is_reproducible_and_retains_every_sequence():
    source = art.read_art((ROOT / "firmware/data/lump.bin").read_bytes())
    blob = (ROOT / "firmware/data/lump-esp32-32e.bin").read_bytes()
    small = art.read_art(blob)
    assert small.moods == source.moods
    assert [(s.kind, s.start, s.end, s.name) for s in small.sequences] == [
        (s.kind, s.start, s.end, s.name) for s in source.sequences]
    assert len(blob) <= 0x270000
    assert blob == art.write_art(art.convert(source))
