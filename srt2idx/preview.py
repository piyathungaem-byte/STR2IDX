"""Render a written .idx/.sub pair back to PNGs, exactly as a player will see it.

This is the eyeball check: it reads the files from disk, walks the .idx
filepos offsets, decodes the bitstream and paints it onto the DVD canvas with
the palette from the .idx.  If a TV shows something different from these PNGs,
the TV is at fault -- not the encoder.
"""
from __future__ import annotations

from PIL import Image

from .decode import decode_spu, parse_idx, parse_sub


def render_pair(idx_path: str, cue_filter: list[int] | None = None,
                background: tuple[int, int, int] = (0, 0, 0)):
    """Yield (index, start_ms, PIL image) for each cue in a .idx/.sub pair."""
    with open(idx_path, "r", encoding="utf-8", errors="replace") as fh:
        idx = parse_idx(fh.read())
    with open(_sub_path(idx_path), "rb") as fh:
        packets = parse_sub(fh.read())
    canvas = idx["size"] or (720, 576)
    palette = idx["palette"]

    for i, ((pts_ms, spu), (idx_ms, _pos)) in enumerate(zip(packets,
                                                             idx["timestamps"])):
        if cue_filter is not None and i not in cue_filter:
            continue
        img = Image.new("RGB", canvas, background)
        px = img.load()
        d = decode_spu(spu)
        for r, row in enumerate(d.rows):
            y = d.y1 + r
            if not 0 <= y < canvas[1]:
                continue
            for c, code in enumerate(row):
                if d.alpha[code] == 0:
                    continue
                x = d.x1 + c
                if not 0 <= x < canvas[0]:
                    continue
                col = palette[d.cols[code]] if palette else 0xFFFFFF
                px[x, y] = (col >> 16 & 255, col >> 8 & 255, col & 255)
        yield i, idx_ms or pts_ms, img


def _sub_path(idx_path: str) -> str:
    import os
    return os.path.splitext(idx_path)[0] + ".sub"


def write_previews(idx_path: str, out_dir: str | None = None,
                   crop: bool = True, scale: int = 1) -> list[str]:
    """Write <idx>.cue<i>.png per subtitle; returns the paths written."""
    import os
    base = os.path.splitext(idx_path)[0]
    dir_ = out_dir or os.path.dirname(idx_path) or "."
    written = []
    for i, _ms, img in render_pair(idx_path):
        if crop:
            box = img.getbbox()
            if box:
                pad = 8
                box = (max(0, box[0] - pad), max(0, box[1] - pad),
                       min(img.width, box[2] + pad),
                       min(img.height, box[3] + pad))
                img = img.crop(box)
        if scale != 1:
            img = img.resize((img.width * scale, img.height * scale),
                             Image.NEAREST)
        path = os.path.join(dir_, f"{os.path.basename(base)}.cue{i}.png")
        img.save(path)
        written.append(path)
    return written
