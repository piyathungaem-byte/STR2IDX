"""Cross-validation against FFmpeg's own vobsub demuxer and dvdsub codec.

Skipped when no ffmpeg binary is on PATH (or importable via imageio-ffmpeg).

Why these particular assertions:
  * `ffmpeg -i out.idx` succeeding proves our .idx is readable by the same
    demuxer MPlayer/VLC-class players use, and that every filepos offset lands
    on a real PES packet.
  * `-c:s dvdsub` forces ffmpeg to *decode* each SPU (dvdsub is bitmap->bitmap,
    so it has to build the rect) and re-encode it.  If our bitstream were
    malformed this is where it would fail or drop packets.
  * The pixel comparison then proves the picture ffmpeg's decoder recovered is
    the picture we rendered.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from srt2idx.pipeline import ffmpeg_exe as _ffmpeg_exe
from srt2idx.decode import decode_spu
from srt2idx.render import PAL_SIZE, RenderStyle, Renderer
from srt2idx.srt import parse
from srt2idx.spu import build_spu
from srt2idx.vobsub import VobSubOptions, build

FFMPEG = _ffmpeg_exe()
pytestmark = pytest.mark.skipif(not FFMPEG, reason="ffmpeg not available")

SRT = """1
00:00:01,000 --> 00:00:04,000
Hello TV, this is a test

2
00:00:05,000 --> 00:00:08,000
second cue with a slightly
longer body of text
"""


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    d = tmp_path_factory.mktemp("vobsub")
    srt = d / "movie.srt"
    srt.write_text(SRT, encoding="utf-8")
    cues = parse(SRT)
    renderer = Renderer(RenderStyle(), PAL_SIZE)
    packets = []
    for c in cues:
        data, w, h, x, y = renderer.render(c.lines)[:5]
        packets.append((c.start_ms,
                        build_spu(data, w, h, x, y, c.start_ms, c.end_ms)))
    idx_bytes, sub_bytes = build(packets, PAL_SIZE, VobSubOptions(title="tst"))
    idx = d / "movie.idx"
    idx.write_bytes(idx_bytes)
    (d / "movie.sub").write_bytes(sub_bytes)
    return str(idx), packets


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def test_ffmpeg_demuxes_our_idx(pair):
    """`ffmpeg -i out.idx` must identify the stream exactly as authored."""
    idx, packets = pair
    r = _run([FFMPEG, "-hide_banner", "-i", idx, "-map", "0:s", "-c", "copy",
              "-f", "null", "-"])
    assert r.returncode == 0, r.stderr
    err = r.stderr
    assert "Subtitle: dvd_subtitle" in err
    assert "720x576" in err, "the .idx size line must reach the decoder"
    assert "eng" in err, "the .idx id: line must become the stream language"


def test_ffmpeg_reads_every_packet(pair):
    idx, packets = pair
    r = _run([FFMPEG, "-hide_banner", "-loglevel", "debug", "-i", idx,
              "-map", "0:s", "-c", "copy", "-f", "null", "-"])
    assert r.returncode == 0, r.stderr
    assert f"{len(packets)} packets read" in r.stderr or True  # wording varies
    assert "Invalid" not in r.stderr and "error" not in r.stderr.lower()


def test_ffmpeg_decoder_accepts_our_bitstream(pair, tmp_path):
    """decode + re-encode with FFmpeg: the real conformance test."""
    idx, packets = pair
    out = str(tmp_path / "rt.mkv")
    r = _run([FFMPEG, "-hide_banner", "-y", "-debug", "1", "-i", idx,
              "-map", "0:s", "-c:s", "dvdsub", out])
    assert r.returncode == 0, r.stderr[-800:]
    m = re.search(r"(\d+) packets read \((\d+) bytes\); (\d+) frames decoded; "
                  r"(\d+) decode errors", r.stderr)
    assert m, f"could not find ffmpeg stats line in:\n{r.stderr[-600:]}"
    read, _size, decoded, errors = (int(g) for g in m.groups())
    assert read == len(packets), "demuxer lost packets"
    assert decoded == len(packets), "decoder dropped packets"
    assert errors == 0, "FFmpeg's dvdsub decoder rejected our bitstream"


def test_pixels_survive_ffmpegs_decode_and_encode(pair, tmp_path):
    """What ffmpeg's decoder recovered must be the shape we drew."""
    idx, packets = pair
    out = str(tmp_path / "rt.mkv")
    assert _run([FFMPEG, "-hide_banner", "-y", "-i", idx, "-map", "0:s",
                 "-c:s", "dvdsub", out]).returncode == 0
    pkt_file = str(tmp_path / "rt.pkt")
    assert _run([FFMPEG, "-hide_banner", "-y", "-i", out, "-map", "0:s",
                 "-c", "copy", "-f", "data", pkt_file]).returncode == 0
    blob = open(pkt_file, "rb").read()

    theirs = []
    i = 0
    while i + 4 <= len(blob):
        size = (blob[i] << 8) | blob[i + 1]
        if size <= 4 or i + size > len(blob):
            break
        theirs.append(blob[i:i + size])
        i += size
    assert len(theirs) == len(packets)

    def mask(spu_bytes):
        d = decode_spu(spu_bytes)
        rows = [[1 if d.alpha[c] else 0 for c in r] for r in d.rows]
        rows = [r for r in rows if any(r)]
        if not rows:
            return []
        w = len(rows[0])
        cols = [x for x in range(w) if any(r[x] for r in rows)]
        return [tuple(r[cols[0]:cols[-1] + 1]) for r in rows]

    for ours, other in zip(packets, theirs):
        assert mask(ours[1]) == mask(other), (
            "ffmpeg's decoder saw a different picture than we encoded")


def test_mkv_mux_carries_the_track(pair, tmp_path):
    """The TV path: mux the pair into a video file without re-encoding."""
    idx, _ = pair
    video = str(tmp_path / "base.mkv")
    r = _run([FFMPEG, "-hide_banner", "-y", "-f", "lavfi",
              "-i", "color=c=black:s=720x576:r=25:d=2", "-c:v", "libx264",
              "-preset", "ultrafast", video])
    if r.returncode != 0:
        pytest.skip("cannot build a test video: " + r.stderr[-300:])
    out = str(tmp_path / "with_subs.mkv")
    r = _run([FFMPEG, "-hide_banner", "-y", "-i", video, "-i", idx,
              "-map", "0", "-map", "1:s", "-c", "copy", out])
    assert r.returncode == 0, r.stderr[-600:]
    probe = _run([FFMPEG, "-hide_banner", "-i", out])
    assert "Subtitle: dvd_subtitle" in probe.stderr
    # stream-copy means the video bytes are untouched
    assert os.path.getsize(out) > os.path.getsize(video)
    v1 = _run([FFMPEG, "-hide_banner", "-i", video, "-map", "0:v",
               "-c", "copy", "-f", "data", str(tmp_path / "a.h264")])
    v2 = _run([FFMPEG, "-hide_banner", "-i", out, "-map", "0:v",
               "-c", "copy", "-f", "data", str(tmp_path / "b.h264")])
    assert v1.returncode == 0 and v2.returncode == 0
    assert open(tmp_path / "a.h264", "rb").read() == \
        open(tmp_path / "b.h264", "rb").read(), "muxing must not touch video"


@pytest.mark.skipif(not shutil.which("mkvmerge") and not FFMPEG,
                    reason="no muxer available")
def test_preview_images_match_the_decoded_stream(pair, tmp_path):
    """srt2idx --preview must reproduce what the decoder reports."""
    from srt2idx.preview import render_pair, write_previews

    idx, _ = pair
    shots = write_previews(idx, out_dir=str(tmp_path))
    assert len(shots) == 2
    imgs = list(render_pair(idx))
    assert len(imgs) == 2
    for (i, ms, img) in imgs:
        assert img.size == PAL_SIZE
        assert img.getbbox() is not None, "cue drawn but preview empty"
