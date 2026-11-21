"""Reference DVD-SPU decoder, mirroring FFmpeg's libavcodec/dvdsubdec.c.

Used for verification (`srt2idx verify`) and previews.  Written independently
of spu.py so an encoder bug cannot silently cancel itself out.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SPU:
    start_ms: int = 0
    end_ms: int = 0
    x1: int = 0
    x2: int = 0
    y1: int = 0
    y2: int = 0
    cols: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    alpha: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    rows: list[bytes] = field(default_factory=list)

    @property
    def width(self) -> int:
        return self.x2 - self.x1 + 1

    @property
    def height(self) -> int:
        return self.y2 - self.y1 + 1


def _rb16(b: bytes, i: int) -> int:
    return (b[i] << 8) | b[i + 1]


def _rb32(b: bytes, i: int) -> int:
    return _rb16(b, i) << 16 | _rb16(b, i + 2)


def decode_rle(buf: bytes, start: int, size: int, w: int, h: int) -> list[bytes]:
    """DVD 2-bit RLE -> h rows of `w` colour codes.  Byte-aligned per row.

    Mirrors decode_run_2bit() in FFmpeg's dvdsubdec.c: nibbles are consumed
    while the accumulated value is still below the running threshold
    (t = 1, 4, 16, 64, 256); a final value below 4 is the "fill the rest of
    the row" code.
    """
    rows = [bytearray(w) for _ in range(h)]
    bit = start * 8
    limit = size * 8
    x = y = 0

    def nib() -> int:
        nonlocal bit
        if (bit >> 3) >= len(buf):
            raise ValueError("RLE ran past the end of the packet")
        v = (buf[bit >> 3] >> (4 - (bit & 7))) & 0xF
        bit += 4
        return v

    while True:
        if bit > limit:
            raise ValueError("RLE ran past the end of the packet")
        v = 0
        t = 1
        while v < t and t <= 0x40:
            v = (v << 4) | nib()
            t <<= 2
        colour = v & 3
        length = w - x if v < 4 else min(v >> 2, w - x)
        rows[y][x:x + length] = bytes([colour]) * length
        x += length
        if x >= w:
            y += 1
            if y >= h:
                break
            x = 0
            bit = (bit + 7) & ~7        # each row is byte-aligned
    return [bytes(r) for r in rows]


def decode_spu(data: bytes) -> SPU:
    if len(data) < 10:
        raise ValueError("SPU packet too short")

    big_offsets = _rb16(data, 0) == 0     # HD/4-byte-offset variant starts with 0
    offset_size = 4 if big_offsets else 2
    get_off = _rb32 if big_offsets else _rb16
    cmd_pos = get_off(data, 2)

    spu = SPU()
    keep = None          # (offset1, offset2, x1, x2, y1, y2) of the picture seq
    for _seq in range(8):                    # bounded: malformed data loops
        if not 0 < cmd_pos < len(data) - 2 - offset_size:
            break
        date = _rb16(data, cmd_pos)
        next_pos = get_off(data, cmd_pos + 2)
        i = cmd_pos + 2 + offset_size
        offset1 = offset2 = -1
        x1 = x2 = y1 = y2 = 0
        while i < len(data):
            cmd = data[i]
            i += 1
            if cmd == 0xFF:
                break
            if cmd == 0x00:                     # menu subpicture
                continue
            if cmd == 0x01:
                spu.start_ms = (date << 10) // 90
            elif cmd == 0x02:
                spu.end_ms = (date << 10) // 90
            elif cmd == 0x03:
                b0, b1 = data[i], data[i + 1]
                spu.cols = [b1 & 0xF, b1 >> 4, b0 & 0xF, b0 >> 4]
                i += 2
            elif cmd == 0x04:
                b0, b1 = data[i], data[i + 1]
                spu.alpha = [b1 & 0xF, b1 >> 4, b0 & 0xF, b0 >> 4]
                i += 2
            elif cmd in (0x05, 0x85):
                x1 = (data[i] << 4) | (data[i + 1] >> 4)
                x2 = ((data[i + 1] & 0xF) << 8) | data[i + 2]
                y1 = (data[i + 3] << 4) | (data[i + 4] >> 4)
                y2 = ((data[i + 4] & 0xF) << 8) | data[i + 5]
                i += 6
            elif cmd == 0x06:
                offset1 = _rb16(data, i)
                offset2 = _rb16(data, i + 2)
                i += 4
            elif cmd == 0x86:
                offset1 = _rb32(data, i)
                offset2 = _rb32(data, i + 4)
                i += 8
            else:
                break
        if offset1 >= 0 and offset2 >= 0:
            # FFmpeg decodes the picture for the sequence that carries the
            # offsets and keeps scanning: the stop date lives in the next one.
            keep = (offset1, offset2, x1, x2, y1, y2)
        if next_pos <= cmd_pos:
            break
        cmd_pos = next_pos

    if keep is None:
        raise ValueError("no RLE offsets found in SPU packet")
    offset1, offset2, spu.x1, spu.x2, spu.y1, spu.y2 = keep
    if offset1 >= len(data) or offset2 >= len(data):
        raise ValueError("RLE offsets point past the end of the packet")

    w, h = spu.width, spu.height
    if w <= 0 or h <= 1:
        raise ValueError(f"degenerate SPU rectangle {w}x{h}")

    top = decode_rle(data, offset1, len(data), w, (h + 1) >> 1)
    bot = decode_rle(data, offset2, len(data), w, h >> 1)
    merged = []
    for r in range(h):
        if r & 1:
            merged.append(bot[r >> 1] if (r >> 1) < len(bot) else bytes(w))
        else:
            merged.append(top[r >> 1])
    spu.rows = merged
    # dates are relative to the packet PTS; the caller adds them
    return spu


def _pts_ms(b: bytes) -> int:
    pts = ((((b[0] >> 1) & 0x07) << 30)
           | (((b[1] << 8) | b[2]) >> 1) << 15
           | (((b[3] << 8) | b[4]) >> 1))
    return pts // 90


def parse_sub(data: bytes) -> list[tuple[int, bytes]]:
    """Split a VobSub .sub (MPEG-PS PES stream) into (pts_ms, spu_bytes).

    Mirrors how FFmpeg's vobsub demuxer walks the file: find each
    00 00 01 BD start code, parse the PES header, skip the one-byte
    substream id, and take exactly the bytes the length field promises.
    """
    out: list[tuple[int, bytes]] = []
    i = 0
    n = len(data)
    while True:
        j = data.find(b"\x00\x00\x01\xbd", i)
        if j < 0 or j + 6 > n:
            break
        nxt = data.find(b"\x00\x00\x01", j + 4)
        if nxt < 0:
            nxt = n
        plen = _rb16(data, j + 4)
        end = min(j + 6 + plen, n) if plen else nxt

        p = j + 6
        while p < n and data[p] == 0xFF:
            p += 1
        pts_ms = 0
        c = data[p]
        p += 1
        if (c & 0xC0) == 0x80:                       # MPEG-2 PES
            flags, hdr_len = data[p], data[p + 1]
            p += 2
            if flags & 0x80:
                pts_ms = _pts_ms(data[p:p + 5])
            p += hdr_len
        elif (c & 0xE0) == 0x20:                     # MPEG-1 PES
            pts_ms = _pts_ms(bytes(0x21) + data[p:p + 3] + b"\x00\x00")
            p += 3
            if (data[p - 1] & 0x0F) == 0x03:         # PTS + DTS
                p += 4
        p += 1                                       # substream id (0x20 + n)
        spu = data[p:end]
        if len(spu) >= 10:
            out.append((pts_ms, spu))
        i = end if end > j else nxt
    return out


def parse_idx(text: str) -> dict:
    """Parse the .idx fields that matter for playback and verification."""
    out: dict = {"streams": [], "timestamps": [], "palette": [], "size": None,
                 "langidx": 0}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("size:"):
            w, h = line[5:].strip().split("x")
            out["size"] = (int(w), int(h))
        elif line.startswith("palette:"):
            out["palette"] = [int(v.strip(), 16) for v in line[8:].split(",")]
        elif line.startswith("id:"):
            ident, _, idx = line[3:].partition("index:")
            out["streams"].append((ident.strip().rstrip(","),
                                   int(idx.strip()) if idx.strip() else 0))
        elif line.startswith("langidx:"):
            out["langidx"] = int(line[8:].strip() or 0)
        elif line.startswith("timestamp:"):
            ts, _, pos = line[10:].partition(", filepos:")
            hh, mm, ss, ms = ts.strip().replace(",", ":").split(":")
            ts_ms = ((int(hh) * 60 + int(mm)) * 60 + int(ss)) * 1000 + int(ms)
            out["timestamps"].append((ts_ms, int(pos.strip(), 16)))
    return out
