"""Tests for the SRT -> VobSub conversion.

Everything here is self-contained (no ffmpeg needed): the assertions that
matter are that the .idx index is well formed and that our bitstream decodes
back to the exact pixels we rendered.  tests/test_with_ffmpeg.py adds the
cross-check against FFmpeg's own decoder when it is available.
"""
from __future__ import annotations

import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from srt2idx import spu
from srt2idx.decode import decode_rle, decode_spu, parse_idx, parse_sub
from srt2idx.render import NTSC_SIZE, PAL_SIZE, RenderStyle, Renderer
from srt2idx.srt import needs_shaping, parse
from srt2idx.vobsub import DEFAULT_PALETTE, VobSubOptions, build

SRCS = {
    "simple": """1
00:00:01,000 --> 00:00:04,000
Hello TV

2
00:00:05,000 --> 00:00:08,500
Second cue, a bit longer
so that it wraps a little
""",
    "crlf_bom_tags": ("\ufeff1\n00:00:00,500 --> 00:00:02,000\r\n"
                      "<i>Italic</i> and <b>bold</b> tags<br />gone\r\n\r\n"
                      "2\n00:00:02,000 --> 00:00:03,000\r\n&nbsp;only spaces"
                      " after cleaning\r\n"),
    "overlapping": """1
00:00:01,000 --> 00:00:03,000
first

2
00:00:02,000 --> 00:00:04,000
overlaps the first
""",
    "no_index_numbers": """00:00:01,000 --> 00:00:02,000
bare cue without a counter
""",
}


# ---------------------------------------------------------------- SRT parsing
@pytest.mark.parametrize("name", list(SRCS))
def test_srt_parses(name):
    cues = parse(SRCS[name])
    assert cues, name
    for c in cues:
        assert c.end_ms > c.start_ms
        assert c.lines and any(ln.strip() for ln in c.lines)


def test_srt_timestamps_and_markup():
    cues = parse(SRCS["simple"])
    assert [(c.start_ms, c.end_ms) for c in cues] == [(1000, 4000), (5000, 8500)]
    cues = parse(SRCS["crlf_bom_tags"])
    assert cues[0].lines == ["Italic and bold tagsgone"]
    # overlapping cues merge rather than being dropped
    cues = parse(SRCS["overlapping"])
    assert len(cues) == 1 and cues[0].end_ms == 4000
    # a cue with no counter line still parses
    assert parse(SRCS["no_index_numbers"])[0].lines[0].startswith("bare cue")


def test_needs_shaping():
    assert needs_shaping("සුළු මොහොතක්")            # Sinhala
    assert needs_shaping("السلام عليكم")            # Arabic
    assert needs_shaping("देवनागरी")                  # Devanagari
    assert not needs_shaping("Hello, caf\u00e9 \u201cquotes\u201d")
    assert not needs_shaping("你好世界")              # CJK: no shaping needed


# ------------------------------------------------------------ RLE bitstream
@pytest.mark.parametrize("w", [1, 2, 7, 16, 63, 64, 200, 257])
@pytest.mark.parametrize("pattern", ["zero", "ones", "alt", "runs", "random"])
def test_rle_roundtrip(w, pattern):
    """encode_rle -> decode_rle must be exact for every run-length shape,
    including the boundaries of the 1/2/3/4-nibble encodings."""
    if pattern == "zero":
        row = bytes(w)
    elif pattern == "ones":
        row = bytes([1]) * w
    elif pattern == "alt":
        row = bytes((x % 2) + 2 for x in range(w))
    elif pattern == "runs":
        # one run per RLE length class: 1-3, 4-15, 16-63, "fill to EOL", >255
        packed = b"".join(bytes([c]) * n for n, c in
                          [(1, 1), (2, 2), (3, 1), (4, 0), (15, 3), (16, 1),
                           (63, 2), (64, 1), (250, 0), (300, 3), (2, 2)])
        row = (packed + bytes(w))[:w]
    else:
        import random
        rnd = random.Random(w)
        row = bytes(rnd.randrange(4) for _ in range(w))
    encoded = spu.encode_rle([row])
    decoded = decode_rle(encoded, 0, len(encoded), w, 1)
    assert decoded == [row]


def test_rle_byte_aligns_each_row():
    row = bytes([1, 1, 2])           # odd nibble count -> must be padded
    enc = spu.encode_rle([row, row])
    assert len(enc) == 2            # 3 nibbles -> 2 bytes, twice
    assert decode_rle(enc, 0, len(enc), 3, 2) == [row, row]


# ---------------------------------------------------------------- SPU packet
def _rect(w, h, kind="text") -> bytes:
    """Clipped to the box: callers pass oversized data on purpose."""
    """A plausible subpicture: transparent border, outline ring, glyph strokes.

    ``kind="flat"`` is a single solid run, which stresses the "fill the rest of
    the row" code path instead of the mixed-run one.
    """
    if kind == "flat":
        return bytes([1]) * (w * h)
    rows = []
    for y in range(min(h, 512)):
        if y == 0 or y == min(h, 512) - 1:
            rows.append(bytes(w))                       # transparent border
        else:
            rows.append(bytes([0, 2]
                              + [1 if (x % 5) else 2 for x in range(w - 4)]
                              + [2, 0]))
    if h > 512:                                   # still too big: keep it flat
        return bytes([1]) * (w * h)
    return b"".join(rows)


def test_spu_packet_self_describes_size_and_offsets():
    w, h = 40, 12
    data = _rect(w, h)
    pkt = spu.build_spu(data, w, h, 10, 20, 1000, 3000)
    size, cmd_pos = struct.unpack(">HH", pkt[:4])
    assert size == len(pkt), "the 2-byte size field must cover the whole packet"
    assert 4 < cmd_pos < len(pkt)
    assert pkt[4:6] != b"\x00\x00", "RLE data starts at offset 4"


def test_spu_decodes_to_the_same_pixels():
    for w, h in [(37, 11), (37, 12), (640, 2), (300, 45)]:
        data = _rect(w, h)
        pkt = spu.build_spu(data, w, h, 5, 400, 0, 2000)
        d = decode_spu(pkt)
        assert (d.width, d.height) == (w, h + (h & 1))
        src = [data[i * w:(i + 1) * w] for i in range(h)]
        if h & 1:
            src.append(bytes(w))
        assert d.rows == src, f"{w}x{h}"


def test_spu_dates_are_pts_relative():
    pkt = spu.build_spu(_rect(30, 10), 30, 10, 0, 500, 45_000, 48_000)
    d = decode_spu(pkt)
    assert d.start_ms == 0, "start date must be relative to the packet PTS"
    assert abs(d.end_ms - 3000) <= 12, "end date carries the duration"


def test_oversize_spu_is_refused_not_truncated():
    w, h = 700, 200
    data = bytes([x % 4 for x in range(w * h)])
    with pytest.raises(ValueError):
        spu.build_spu(data, w, h, 0, 300, 0, 1000)


# ---------------------------------------------------------------- .sub stream
def test_pes_framing_matches_the_length_field():
    pkt = spu.build_spu(_rect(30, 10), 30, 10, 0, 100, 2000, 4000)
    stream, offsets = spu.build_sub_stream([(2000, pkt)])
    assert stream[:4] == b"\x00\x00\x01\xbd"
    (length,) = struct.unpack(">H", stream[4:6])
    assert stream[6:9] == b"\x80\x80\x05"
    assert stream[14] == 0x20, "substream id byte follows the PES header"
    assert length == 8 + 1 + len(pkt)
    assert offsets == [0]
    assert len(stream) == 6 + length


def test_pts_in_the_sub_matches_the_cue_start():
    pkt = spu.build_spu(_rect(30, 10), 30, 10, 0, 100, 73_000, 75_000)
    stream, _ = spu.build_sub_stream([(73_000, pkt)])
    (pts_ms, _), = parse_sub(stream)
    assert pts_ms == 73_000


def test_sub_file_survives_a_start_code_lookalike_in_the_rle():
    """\\x00\\x00\\x01 inside subpicture data must not truncate the packet."""
    w, h = 24, 10
    data = bytes(w * h)                      # long zero runs -> 00 00 bytes
    pkt = spu.build_spu(data, w, h, 0, 100, 0, 1000)
    stream, offsets = spu.build_sub_stream([(0, pkt), (5000, pkt)])
    parsed = parse_sub(stream)
    assert [len(p) for _, p in parsed] == [len(pkt), len(pkt)]
    assert [o for o in offsets] == [p for p, _ in [(o, 0) for o in offsets]]


# --------------------------------------------------------------- .idx + pair
def _pair(canvas=PAL_SIZE, style=None):
    renderer = Renderer(style or RenderStyle(), canvas)
    cues = parse(SRCS["simple"])
    packets = []
    for c in cues:
        data, w, h, x, y = renderer.render(c.lines)[:5]
        packets.append((c.start_ms,
                        spu.build_spu(data, w, h, x, y, c.start_ms, c.end_ms)))
    idx, sub = build(packets, canvas, VobSubOptions(title="tst"))
    return idx.decode(), sub, packets, renderer, cues


def test_idx_is_parseable_and_offsets_are_right():
    idx_text, sub, packets, _, _ = _pair()
    idx = parse_idx(idx_text)
    assert idx_text.startswith("# VobSub index file,")
    assert idx["size"] == PAL_SIZE
    assert len(idx["palette"]) == 16
    assert idx["streams"][0] == ("eng", 0)
    assert len(idx["timestamps"]) == len(packets)
    for (ms, pos), (start, _spu) in zip(idx["timestamps"], packets):
        assert ms == start
        assert sub[pos:pos + 4] == b"\x00\x00\x01\xbd", "filepos -> PES start code"
    # timestamps must be ascending: players binary-search on them
    ts = [ms for ms, _ in idx["timestamps"]]
    assert ts == sorted(ts)
    # filepos must be ascending too, and the last entry ends the file
    pos = [p for _, p in idx["timestamps"]]
    assert pos == sorted(pos)
    assert pos[-1] < len(sub)


def test_idx_palette_follows_the_requested_colours():
    style = RenderStyle()
    idx_text, *_ = _pair(NTSC_SIZE, style)
    idx = parse_idx(idx_text)
    assert idx["size"] == NTSC_SIZE
    assert idx["palette"][1] == DEFAULT_PALETTE[1]
    assert "\r" not in idx_text and idx_text.endswith("\n")


def test_every_cue_lands_in_the_pair():
    idx_text, sub, packets, _, cues = _pair()
    parsed = parse_sub(sub)
    assert len(parsed) == len(packets) == len(cues)
    for (pts, pkt), (start, expect) in zip(parsed, packets):
        assert pts == start
        assert pkt == expect, "bytes read back must equal bytes written"


# ------------------------------------------------------------------- renderer
@pytest.mark.parametrize("canvas", [PAL_SIZE, NTSC_SIZE])
def test_boxes_stay_inside_the_dvd_canvas(canvas):
    renderer = Renderer(RenderStyle(font_size=34, outline=3), canvas)
    for text in ["short", "a rather long subtitle line that needs wrapping",
                 "x" * 90]:
        data, w, h, x, y, _n = renderer.render([text])
        assert x >= 0 and y >= 0
        assert x + w <= canvas[0], f"box wider than canvas: {x}+{w}"
        assert y + h <= canvas[1], f"box below canvas: {y}+{h}"
        assert w > 0 and h > 1


def test_font_size_is_restored_between_cues():
    """A cue long enough to need shrinking must not shrink the next one."""
    renderer = Renderer(RenderStyle(font_size=30), PAL_SIZE)
    small = renderer.render(["word " * 40])[0]
    normal = renderer.render(["Hello TV"])[0]
    again = renderer.render(["Hello TV"])[0]
    assert normal == again
    assert len(small) > len(normal)


def test_render_is_deterministic():
    r1 = Renderer(RenderStyle(), PAL_SIZE)
    r2 = Renderer(RenderStyle(), PAL_SIZE)
    assert r1.render(["Hello TV"]) == r2.render(["Hello TV"])


# ------------------------------------------------------------------ full loop
def test_end_to_end_pixels_survive_encode_and_decode():
    """The strongest pure-Python check: what we drew is what a player reads."""
    renderer = Renderer(RenderStyle(font_size=30, outline=2), PAL_SIZE)
    for cue in parse(SRCS["simple"]) + parse(SRCS["crlf_bom_tags"]):
        data, w, h, x, y = renderer.render(cue.lines)[:5]
        pkt = spu.build_spu(data, w, h, x, y, cue.start_ms, cue.end_ms)
        d = decode_spu(pkt)
        src = [data[i * w:(i + 1) * w] for i in range(h)]
        if h & 1:
            src.append(bytes(w))
        assert d.x1 == x and d.y1 == y
        assert d.rows == src
