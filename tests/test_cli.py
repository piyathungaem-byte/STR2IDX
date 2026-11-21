"""Command-line behaviour: exit codes, file layout, timing options.

These run the module as the user would, so argparse wiring, the auto engine
choice and the on-disk contract (both files, right names, offsets valid) are
covered, not just the library internals.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from srt2idx.decode import decode_spu, parse_idx, parse_sub


def run(tmp_path, *args, expect=0):
    cmd = [sys.executable, "-m", "srt2idx", *[str(a) for a in args]]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if expect is not None:
        assert r.returncode == expect, f"rc={r.returncode}\n{r.stdout}\n{r.stderr}"
    return r


@pytest.fixture
def srt(tmp_path):
    p = tmp_path / "movie.srt"
    p.write_text(
        "1\n00:00:01,000 --> 00:00:04,000\nHello TV\n\n"
        "2\n00:00:05,000 --> 00:00:05,060\nblink and you miss it\n\n"
        "3\n00:00:07,500 --> 00:00:09,250\nthird cue, slightly longer text\n",
        encoding="utf-8")
    return p


def test_writes_both_files_next_to_the_idx(tmp_path, srt):
    out = tmp_path / "movie.idx"
    r = run(tmp_path, srt, "-o", out, "--verify")
    assert "0 problem(s)" in r.stdout
    assert out.exists() and out.with_suffix(".sub").exists()
    # the .sub must be referenced by name only: players pair them by basename
    assert out.with_suffix(".sub").stat().st_size > 100


def test_default_output_is_beside_the_input(tmp_path, srt):
    run(tmp_path, srt)
    assert (tmp_path / "movie.idx").exists() and (tmp_path / "movie.sub").exists()


def test_output_extension_is_forced(tmp_path, srt):
    run(tmp_path, srt, "-o", tmp_path / "movie")
    assert (tmp_path / "movie.idx").exists(), "a missing .idx suffix is added"


def test_short_cues_are_stretched_then_snapped_to_frames(tmp_path, srt):
    out = tmp_path / "t.idx"
    run(tmp_path, srt, "-o", out, "--fps", "25")
    idx = parse_idx(out.read_text(encoding="utf-8"))
    starts = [ms for ms, _ in idx["timestamps"]]
    grid = round(1000 / 25)
    assert all(ms % grid == 0 for ms in starts), starts
    assert starts[0] == 1000 and abs(starts[1] - 5000) < grid and \
        abs(starts[2] - 7500) < grid, "cue starts must stay within a frame"

    subs = parse_sub(out.with_suffix(".sub").read_bytes())
    durations = []
    for (pts, spu) in subs:
        d = decode_spu(spu)
        durations.append(d.end_ms)
    assert durations[1] >= 700, "the 60 ms cue must be made readable"
    assert all(d > 0 for d in durations)


def test_min_duration_and_delay(tmp_path, srt):
    out = tmp_path / "d.idx"
    run(tmp_path, srt, "-o", out, "--delay", "1.0", "--min-duration", "2.5")
    idx = parse_idx(out.read_text(encoding="utf-8"))
    starts = [ms for ms, _ in idx["timestamps"]]
    assert all(s % 40 == 0 for s in starts), starts
    assert [round(a / 40) for a in starts] == [50, 150, 212], starts
    subs = parse_sub(out.with_suffix(".sub").read_bytes())
    # the SPU date field quantises to ~11.4 ms (1024/90 ms per tick), so the
    # encoded duration can land a hair under the requested one
    assert min(decode_spu(s).end_ms for _, s in subs) >= 2500 - 12


def test_negative_delay_clamps_at_zero(tmp_path, srt):
    out = tmp_path / "n.idx"
    run(tmp_path, srt, "-o", out, "--delay", "-3")
    assert parse_idx(out.read_text(encoding="utf-8"))["timestamps"][0][0] == 0


def test_ntsc_and_pal_canvases(tmp_path, srt):
    for size in ("pal", "ntsc"):
        out = tmp_path / f"{size}.idx"
        run(tmp_path, srt, "-o", out, "--size", size, "--verify")
        idx = parse_idx(out.read_text(encoding="utf-8"))
        assert idx["size"] == (720, 576 if size == "pal" else 480)


def test_dry_run_writes_nothing(tmp_path, srt):
    out = tmp_path / "z.idx"
    r = run(tmp_path, srt, "-o", out, "--dry-run")
    assert "would write" in r.stdout and ".sub" in r.stdout
    assert not out.exists() and not out.with_suffix(".sub").exists()


def test_empty_input_fails_loudly(tmp_path):
    p = tmp_path / "empty.srt"
    p.write_text("\n\n\ngarbage with no timestamps\n", encoding="utf-8")
    r = run(tmp_path, p, "-o", tmp_path / "e.idx", expect=1)
    assert "no usable cues" in r.stderr + r.stdout
    assert not (tmp_path / "e.idx").exists()


def test_bad_language_is_rejected_before_any_work(tmp_path, srt):
    r = run(tmp_path, srt, "-o", tmp_path / "x.idx", "--lang", "english",
            expect=2)
    assert "2-3 letter ASCII" in r.stderr + r.stdout
    assert not (tmp_path / "x.idx").exists()


def test_language_is_lowercased(tmp_path, srt):
    out = tmp_path / "up.idx"
    run(tmp_path, srt, "-o", out, "--lang", "TAM")
    assert parse_idx(out.read_text(encoding="utf-8"))["streams"][0][0] == "tam"


def test_missing_input_fails(tmp_path):
    r = run(tmp_path, tmp_path / "nope.srt", expect=1)
    assert "no such file" in r.stderr + r.stdout


def test_preview_images_are_written_and_removed_beyond_the_limit(tmp_path, srt):
    r = run(tmp_path, srt, "-o", tmp_path / "p.idx", "--preview", "--preview-limit", "2")
    pngs = sorted(f for f in os.listdir(tmp_path) if f.endswith(".png"))
    assert len(pngs) == 2, pngs
    assert "previews:" in r.stdout


def test_colours_reach_the_idx(tmp_path, srt):
    out = tmp_path / "c.idx"
    run(tmp_path, srt, "-o", out, "--fg", "FFFED2", "--outline-color", "101010")
    idx = parse_idx(out.read_text(encoding="utf-8"))
    assert idx["palette"][1] == 0xFFFED2
    assert idx["palette"][2] == 0x101010


def test_language_and_title_land_in_the_idx(tmp_path, srt):
    out = tmp_path / "l.idx"
    run(tmp_path, srt, "-o", out, "--lang", "sin", "--title", "සිංහල")
    text = out.read_text(encoding="utf-8")
    idx = parse_idx(text)
    assert idx["streams"][0][0] == "sin"
    assert "alt: සිංහල" in text


def test_auto_engine_picks_libass_for_shaped_scripts(tmp_path, srt):
    si = tmp_path / "si.srt"
    si.write_text("1\n00:00:01,000 --> 00:00:04,000\n"
                  "\u0dc3\u0dd4\u0dbd\u0dd2 \u0db8\u0dd9\u0db1\u0dc3\n", encoding="utf-8")
    r = run(tmp_path, si, "-o", tmp_path / "si.idx", "--engine", "auto",
            "--verify")
    if "engine: libass" not in r.stdout:
        pytest.skip("no ffmpeg available for the libass engine")
    assert "0 problem(s)" in r.stdout


SINHALA = ("1\n00:00:01,000 --> 00:00:04,000\n"
           "\u0dc3\u0dd4\u0dbd\u0dd2 \u0db8\u0dd9\u0db1\u0dc3\n")


def test_an_unshaped_render_is_refused_before_it_wastes_a_film(tmp_path):
    """Pillow draws Sinhala in code-point order: real glyphs, wrong text.

    Every byte-level check still passes on such a file, so the only moment we
    can say "this will read wrong on your TV" is before writing it.
    """
    si = tmp_path / "si.srt"
    si.write_text(SINHALA, encoding="utf-8")
    r = run(tmp_path, si, "-o", tmp_path / "x.idx", "--engine", "pil",
            expect=2)
    assert "needs text shaping" in r.stderr
    assert "--allow-unshaped" in r.stderr          # names the way out
    assert not (tmp_path / "x.idx").exists()


def test_allow_unshaped_writes_a_valid_pair_anyway(tmp_path):
    """The override must still produce a conformant file: what it changes is
    the text order, never the container."""
    si = tmp_path / "si.srt"
    si.write_text(SINHALA, encoding="utf-8")
    out = tmp_path / "pil.idx"
    r = run(tmp_path, si, "-o", out, "--engine", "pil", "--verify",
            "--allow-unshaped")
    # warnings go to stderr, so they survive a pipe of the report
    assert "WITHOUT text shaping" in r.stderr
    subs = parse_sub(out.with_suffix(".sub").read_bytes())
    assert len(subs) == 1
    assert decode_spu(subs[0][1]).width > 0


def test_latin_text_is_never_blocked(tmp_path):
    en = tmp_path / "en.srt"
    en.write_text("1\n00:00:01,000 --> 00:00:04,000\nHello TV\n",
                  encoding="utf-8")
    run(tmp_path, en, "-o", tmp_path / "en.idx", "--engine", "pil", "--verify")


def test_list_fonts_needs_no_input_file(tmp_path):
    """The one question worth asking before a conversion: can this font shape?"""
    r = run(tmp_path, "--list-fonts")
    assert "shape" in r.stdout and "path" in r.stdout
    assert "--font" in r.stdout or "--list-fonts" in r.stdout


def test_a_command_with_no_input_is_an_error_not_a_crash(tmp_path):
    r = run(tmp_path, expect=2)
    assert "srt" in r.stderr and "--list-fonts" in r.stderr
