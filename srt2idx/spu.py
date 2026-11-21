"""DVD SPU (subpicture unit) bitstream writer.

Layout (DVD-Video spec, as implemented by FFmpeg's dvdsubenc.c/dvdsubdec.c --
cross-checked byte-for-byte against libavcodec/dvdsubenc.c):

    +0   u16  total SPU packet size
    +2   u16  offset of the first control sequence   (0 = "HD" 4-byte form)
    +4   RLE of the top field    (even bitmap rows)   <- offset1, always 4
         RLE of the bottom field (odd  bitmap rows)   <- offset2
         control sequence 1: date, next_pos, 0x03 colormap, 0x04 alpha,
                             0x05 rect, 0x06 offsets, 0x01 start, 0xff
         control sequence 2: date, next_pos(=self),  0x02 end,   0xff

``colormap[code]`` maps RLE colour code -> palette index; ``alpha[code]`` is a
4-bit per-code alpha where 0xF = opaque and 0 = transparent.
"""
from __future__ import annotations

MAX_PACKET = 0xFFFF
HEADER_SIZE = 4


class NibbleWriter:
    """Packs 4-bit codes MSB-first, byte-aligning on flush()."""

    __slots__ = ("buf", "_high")

    def __init__(self) -> None:
        self.buf = bytearray()
        self._high: int | None = None

    def put(self, val: int) -> None:
        val &= 0x0F
        if self._high is None:
            self._high = val
        else:
            self.buf.append((self._high << 4) | val)
            self._high = None

    def flush(self) -> bytes:
        if self._high is not None:
            self.buf.append(self._high << 4)
            self._high = None
        return bytes(self.buf)


def encode_rle(rows: list[bytes]) -> bytes:
    """Run-length encode field rows into the DVD 2-bit RLE bitstream."""
    nw = NibbleWriter()
    for line in rows:
        w = len(line)
        x = 0
        while x < w:
            colour = line[x]
            run = 1
            while x + run < w and line[x + run] == colour:
                run += 1
            if run < 0x04:
                nw.put((run << 2) | colour)
            elif run < 0x10:
                nw.put(run >> 2)
                nw.put((run << 2) | colour)
            elif run < 0x40:
                nw.put(0)
                nw.put(run >> 2)
                nw.put((run << 2) | colour)
            elif x + run == w:
                # "fill the rest of the line with this colour"
                nw.put(0)
                nw.put(0)
                nw.put(0)
                nw.put(colour)
                break
            else:
                run = min(run, 0xFF)
                nw.put(0)
                nw.put(run >> 6)
                nw.put(run >> 2)
                nw.put((run << 2) | colour)
            x += run
        nw.flush()  # every row ends byte-aligned
    return bytes(nw.buf)


def _date(ms: int) -> int:
    """Milliseconds -> the SPU control-sequence date field.

    The DVD subpicture date is NOT a 90 kHz PTS: it is 1024/90 ms per tick
    (~11.4 ms), hence the >>10.  So a cue's *duration* as stored in the
    subpicture is quantised to that, while the .idx timestamp and the PES PTS
    carry exact milliseconds.  Worst case a subtitle disappears ~11 ms early,
    which is below one frame at 25/30 fps and is what real DVDs do too.
    """
    return (max(0, ms) * 90) >> 10


def build_spu(
    data: bytes,
    width: int,
    height: int,
    x: int,
    y: int,
    start_ms: int,
    end_ms: int,
    absolute_dates: bool = False,
    colormap: tuple[int, int, int, int] = (0, 1, 2, 3),
    alpha: tuple[int, int, int, int] = (0x0, 0xF, 0xF, 0xF),
    even_rows_fix: bool = True,
) -> bytes:
    """Assemble a complete SPU packet.

    ``data`` is one byte per pixel (values 0..3) in row-major order for a
    ``width`` x ``height`` rectangle placed at ``(x, y)`` on the DVD canvas.

    The control-sequence dates are written *relative to the packet PTS*, which
    is how players interpret them (MPlayer, libavcodec, VLC all add them to
    the packet timestamp).  Pass ``absolute_dates=True`` for tools that read a
    bare .sub without usable PES timestamps.
    """
    if absolute_dates:
        start_date, end_date = _date(start_ms), _date(end_ms)
    else:
        start_date = 0
        end_date = _date(max(0, end_ms - start_ms))
    if width <= 0 or height <= 1:
        raise ValueError("SPU rectangle must be at least 1x2 pixels")
    rows = [data[i * width:(i + 1) * width] for i in range(height)]

    if even_rows_fix and height & 1:
        # some players require an even height; pad one transparent row
        rows.append(bytes(width))
        height += 1

    top = encode_rle([rows[i] for i in range(0, height, 2)])
    bottom = encode_rle([rows[i] for i in range(1, height, 2)])

    # offsets and the size field are all 16-bit, so check before writing them
    if HEADER_SIZE + len(top) + len(bottom) + 24 > MAX_PACKET:
        raise ValueError(
            f"subpicture does not fit the DVD format ({len(top) + len(bottom)} "
            f"rle bytes for a {width}x{height} box); reduce --font-size or "
            f"--max-lines")

    out = bytearray(b"\x00\x00" * 2)
    out += top
    offset1 = HEADER_SIZE
    offset2 = len(out)
    out += bottom

    x2 = x + width - 1
    y2 = y + height - 1

    ctrl = bytearray()
    ctrl += start_date.to_bytes(2, "big")
    ctrl += b"\x00\x00"                                  # patched below
    ctrl += bytes((0x03,
                   ((colormap[3] & 0xF) << 4) | (colormap[2] & 0xF),
                   ((colormap[1] & 0xF) << 4) | (colormap[0] & 0xF)))
    ctrl += bytes((0x04,
                   ((alpha[3] & 0xF) << 4) | (alpha[2] & 0xF),
                   ((alpha[1] & 0xF) << 4) | (alpha[0] & 0xF)))
    ctrl += bytes((0x05,
                   (x >> 4) & 0xFF,
                   ((x << 4) | ((x2 >> 8) & 0x0F)) & 0xFF,
                   x2 & 0xFF,
                   (y >> 4) & 0xFF,
                   ((y << 4) | ((y2 >> 8) & 0x0F)) & 0xFF,
                   y2 & 0xFF))
    ctrl += bytes((0x06,))
    ctrl += offset1.to_bytes(2, "big")
    ctrl += offset2.to_bytes(2, "big")
    ctrl += b"\x01\xFF"

    cmd_pos = len(out)
    stop_pos = cmd_pos + len(ctrl)
    ctrl[2:4] = stop_pos.to_bytes(2, "big")
    out += ctrl

    out += end_date.to_bytes(2, "big")
    out += stop_pos.to_bytes(2, "big")   # self-reference: ends the command loop
    out += b"\x02\xFF"

    if len(out) > MAX_PACKET:
        raise ValueError(f"SPU packet too large for the DVD format ({len(out)} B)")

    out[0:2] = len(out).to_bytes(2, "big")
    out[2:4] = cmd_pos.to_bytes(2, "big")
    return bytes(out)


def build_sub_stream(packets: list[tuple[int, bytes]],
                     substream_id: int = 0x20) -> tuple[bytes, list[int]]:
    """Wrap SPU packets in MPEG-2 PES packets, as a VobSub .sub file stores them.

    Each entry is:  00 00 01 BD <len16> 80 80 05 <PTS:5> <id> <SPU>
    where id is 0x20 + the ``index:`` number from the .idx.

    Returns the stream bytes and the byte offset of every entry, which the
    .idx ``filepos:`` values must point at (start of the 00 00 01 BD code).
    """
    out = bytearray()
    offsets: list[int] = []
    for start_ms, spu in packets:
        pts = max(0, int(round(start_ms * 90)))
        body = bytes((substream_id,)) + spu
        length = 8 + len(body)          # marker + flags + hdr_len + PTS + body
        if length > 0xFFFF:
            raise ValueError("PES packet too large")
        pes = bytearray(b"\x00\x00\x01\xbd")
        pes += length.to_bytes(2, "big")
        pes += b"\x80\x80\x05"          # MPEG-2 PES, PTS present, 5 bytes follow
        pes += bytes((
            0x21 | (((pts >> 30) & 0x07) << 1),
            (pts >> 22) & 0xFF,
            0x01 | (((pts >> 15) & 0x7F) << 1),
            (pts >> 7) & 0xFF,
            0x01 | ((pts & 0x7F) << 1),
        ))
        pes += body
        offsets.append(len(out))
        out += pes
    return bytes(out), offsets
