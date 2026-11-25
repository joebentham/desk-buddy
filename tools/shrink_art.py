#!/usr/bin/env python3
"""Convert existing Lump art offline, without a browser or colour interpolation.

Default: v3 240x240/24 fps -> v4 120x120/8 fps; retain every mood/sequence.
Usage: python3 tools/shrink_art.py

v4 is v3 with three fields inserted after fps (byte offset 10):
  u16 width, u16 height, u32 file_bytes (including header), little endian.
Everything else is unchanged: sequence indices, offsets relative to frame data,
and [u8 count, u16 RGB565 big endian] runs. Each frame expands to width*height
pixels. The player doubles each pixel on both axes. RGB565 0xF81F (the exported
FF00FF marker) is copied exactly, along with every other sampled colour.

Sample source ticks at floor(t*source_fps/fps), rounding durations up to one
output tick; preserve the last pose of transitions/events. Deduplicate sampled
frames after scaling. This never overwrites the source file.
"""
import argparse
from array import array
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Sequence:
    kind: int
    start: int
    end: int
    name: str
    ticks: tuple


@dataclass
class Art:
    width: int
    height: int
    fps: int
    moods: list
    sequences: list
    frames: list


def read_art(blob):
    """Strict parser: reject incomplete metadata and corrupt RLE before converting."""
    pos = 0

    def take(n):
        nonlocal pos
        if n < 0 or pos + n > len(blob):
            raise ValueError("truncated art")
        part = blob[pos:pos + n]
        pos += n
        return part

    def unpack(fmt):
        return struct.unpack(fmt, take(struct.calcsize(fmt)))

    def string():
        return take(take(1)[0]).decode("utf-8")

    if take(4) != b"LUMP":
        raise ValueError("not LUMP art")
    version, nframes, nmoods, fps = unpack("<HHBB")
    width = height = 240
    if version == 4:
        width, height, length = unpack("<HHI")
        if length != len(blob):
            raise ValueError("art size mismatch")
    elif version != 3:
        raise ValueError("unsupported art version")
    if not fps or not nframes or not 0 < nmoods <= 12 or width != height or width not in (120, 240):
        raise ValueError("invalid art geometry/counts")
    moods = [(take(1)[0], string()) for _ in range(nmoods)]
    if any(base >= nmoods for base, _ in moods):
        raise ValueError("invalid base mood")
    sequences = []
    for _ in range(take(1)[0]):
        kind, start, end = unpack("<BBB")
        name = string()
        count, = unpack("<H")
        ticks = unpack(f"<{count}H")
        if kind > 2 or start >= nmoods or end >= nmoods or not count or max(ticks) >= nframes:
            raise ValueError("invalid sequence")
        sequences.append(Sequence(kind, start, end, name, ticks))
    if {s.start for s in sequences if s.kind == 0} != set(range(nmoods)):
        raise ValueError("missing mood loop")
    offsets = unpack(f"<{nframes}I")
    data = blob[pos:]
    frames = []
    for i, offset in enumerate(offsets):
        stop = offsets[i + 1] if i + 1 < nframes else len(data)
        if not 0 <= offset < stop <= len(data) or (stop - offset) % 3:
            raise ValueError("invalid frame offset")
        frame = data[offset:stop]
        if any(frame[j] == 0 for j in range(0, len(frame), 3)) or sum(frame[::3]) != width * height:
            raise ValueError("invalid RLE frame")
        frames.append(frame)
    return Art(width, height, fps, moods, sequences, frames)


def decode(frame):
    pixels = array("H")
    for count, colour in struct.iter_unpack(">BH", frame):
        pixels.extend(array("H", [colour]) * count)
    return pixels


def encode(pixels):
    out = bytearray()
    for colour, run in groupby(pixels):
        count = sum(1 for _ in run)
        while count:
            n = min(255, count)
            out.extend(struct.pack(">BH", n, colour))
            count -= n
    return bytes(out)


def convert(source, size=120, fps=8):
    if size not in (120, 240) or source.width % size or not 0 < fps <= source.fps:
        raise ValueError("size must divide source width; fps must be 1..source fps")
    scale = source.width // size
    converted, unique, frames, sequences = {}, {}, [], []
    for s in source.sequences:
        count = (len(s.ticks) * fps + source.fps - 1) // source.fps
        times = [min(len(s.ticks) - 1, t * source.fps // fps) for t in range(count)]
        if s.kind != 0:
            times[-1] = len(s.ticks) - 1
        ticks = []
        for t in times:
            index = s.ticks[t]
            if index not in converted:
                px = decode(source.frames[index])
                small = array("H")
                for y in range(0, source.height, scale):
                    small.extend(px[y * source.width:(y + 1) * source.width:scale])
                frame = encode(small)
                if frame not in unique:
                    unique[frame] = len(frames)
                    frames.append(frame)
                converted[index] = unique[frame]
            ticks.append(converted[index])
        sequences.append(Sequence(s.kind, s.start, s.end, s.name, tuple(ticks)))
    return Art(size, size, fps, source.moods, sequences, frames)


def write_art(art):
    def string(value):
        b = value.encode("utf-8")
        return bytes([len(b)]) + b

    out = bytearray(b"LUMP" + struct.pack("<HHBBHHI", 4, len(art.frames), len(art.moods), art.fps,
                                        art.width, art.height, 0))
    for base, name in art.moods:
        out.extend(bytes([base]) + string(name))
    out.append(len(art.sequences))
    for s in art.sequences:
        out.extend(bytes([s.kind, s.start, s.end]) + string(s.name))
        out.extend(struct.pack(f"<H{len(s.ticks)}H", len(s.ticks), *s.ticks))
    offset = 0
    for frame in art.frames:
        out.extend(struct.pack("<I", offset))
        offset += len(frame)
    out.extend(b"".join(art.frames))
    struct.pack_into("<I", out, 14, len(out))
    return bytes(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", type=Path, default=ROOT / "firmware/data/lump.bin")
    p.add_argument("--output", type=Path, default=ROOT / "firmware/data/lump-esp32-32e.bin")
    p.add_argument("--size", type=int, default=120)
    p.add_argument("--fps", type=int, default=8)
    p.add_argument("--budget", type=lambda s: int(s, 0), default=0x270000)
    args = p.parse_args()
    if args.source.resolve() == args.output.resolve():
        p.error("source and output must differ")
    source = read_art(args.source.read_bytes())
    art = convert(source, args.size, args.fps)
    blob = write_art(art)
    read_art(blob)  # verify the entire output before saving
    if len(blob) > args.budget:
        p.error(f"art needs {len(blob)} bytes, budget is {args.budget}")
    args.output.write_bytes(blob)
    print(f"{args.output}: {len(blob):,} bytes / {args.budget:,}; {len(art.frames)} unique frames, "
          f"{len(art.moods)} moods, {len(art.sequences)} sequences, {art.width}x{art.height} @ {art.fps} fps")


if __name__ == "__main__":
    main()
