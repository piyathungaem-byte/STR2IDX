"""srt2idx command line interface.

    python -m srt2idx input.srt -o out.idx            # write .idx + .sub
    python -m srt2idx input.srt -o out.idx --mkv       # and mux into .mkv
    python -m srt2idx --verify input.srt -o /tmp/x.idx # round-trip check
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from . import __version__
from .render import NTSC_SIZE, PAL_SIZE, RenderStyle, Renderer
from .srt import needs_shaping, parse
from .spu import build_spu
from .vobsub import DEFAULT_PALETTE, VobSubOptions, build

VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv",
              ".mpg", ".mpeg", ".ts", ".m2ts", ".vob")


def _ffmpeg_exe() -> str | None:
    """ffmpeg on PATH, or the one bundled with imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _hex_colour(s: str) -> int:
    s = s.strip().lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError(f"'{s}' is not RRGGBB")
    return int(s, 16)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="srt2idx",
        description="Convert .srt subtitles into VobSub .idx + .sub, for TVs "
                    "that only accept bitmap subtitles.",
    )
    p.add_argument("srt", help="input SubRip .srt file")
    p.add_argument("-o", "--output", default=None,
                   help="output .idx path (.sub is written beside it); "
                        "default: <input>.idx")

    lay = p.add_argument_group("canvas & layout")
    lay.add_argument("--size", choices=("pal", "ntsc"), default="pal",
                     help="DVD canvas: pal=720x576 (default), ntsc=720x480")
    lay.add_argument("--fps", type=float, default=25.0,
                     help="frame rate to snap timings to (default 25)")
    lay.add_argument("--font", default=None, help="TTF font file")
    lay.add_argument("--font-size", type=int, default=30)
    lay.add_argument("--outline", type=int, default=2, help="outline width in px")
    lay.add_argument("--shadow", type=int, default=0, help="shadow offset in px")
    lay.add_argument("--margin-bottom", type=int, default=36)
    lay.add_argument("--margin-x", type=int, default=40)
    lay.add_argument("--max-lines", type=int, default=2,
                     help="soft line count before the font shrinks")
    lay.add_argument("--uppercase", action="store_true")
    lay.add_argument("--engine", choices=("auto", "pil", "ass"), default="auto",
                     help="'pil' = Pillow (fast, Latin-friendly), 'ass' = libass "
                          "via ffmpeg (needed for Sinhala/Arabic/Devanagari and "
                          "other shaped scripts). auto picks ass when the text "
                          "needs it (default)")
    lay.add_argument("--force-style", default=None, metavar="K=V,K=V",
                     help="extra libass force_style overrides (ass engine)")

    col = p.add_argument_group("colours (baked in -- a TV cannot restyle these)")
    col.add_argument("--fg", type=_hex_colour, default=DEFAULT_PALETTE[1],
                     metavar="RRGGBB", help="text colour")
    col.add_argument("--outline-color", type=_hex_colour, default=DEFAULT_PALETTE[2],
                     metavar="RRGGBB", help="outline and shadow colour")

    out = p.add_argument_group("output")
    out.add_argument("--lang", default="eng", help="3-letter id in the .idx")
    out.add_argument("--title", default=None, help="'alt:' description")
    out.add_argument("--delay", type=float, default=0.0, metavar="SECONDS",
                     help="shift all timings (+/-)")
    out.add_argument("--min-duration", type=float, default=0.8, metavar="SEC",
                     help="stretch cues shorter than this (default 0.8)")
    out.add_argument("--absolute-dates", action="store_true",
                     help="write absolute SPU dates instead of PTS-relative")
    out.add_argument("--mkv", metavar="VIDEO", nargs="?", const="AUTO", default=None,
                     help="also mux into an .mkv; pass a video file, or nothing to "
                          "use one matching the .srt basename")
    out.add_argument("--mkv-output", default=None, metavar="PATH",
                     help="where to write the .mkv (default: <video>.vobsub.mkv)")
    out.add_argument("--preview", metavar="DIR", nargs="?", const="", default=None,
                     help="also decode the result back to PNGs (one per cue) so "
                          "you can see what the TV will show; DIR optional")
    out.add_argument("--preview-limit", type=int, default=8,
                     help="max PNGs to write with --preview (default 8)")
    out.add_argument("--verify", action="store_true",
                     help="decode the result back and compare pixels")
    out.add_argument("--dry-run", action="store_true",
                     help="report counts, write nothing")
    out.add_argument("-v", "--verbose", action="store_true")
    out.add_argument("--version", action="version", version=f"srt2idx {__version__}")
    return p


class OversizeSubtitle(Exception):
    """The cue needs more pixels than one DVD subpicture can carry."""


def encode_cue(renderer, style, cue_lines, start, end, absolute_dates):
    """Render + pack one cue, shrinking the font if the SPU will not fit.

    A DVD subpicture packet is addressed by two 16-bit fields, so a single
    rectangle cannot exceed 64 KiB once RLE'd.  Text that long would be
    unreadable on a TV anyway; we shrink the font once (some players also
    dislike very tall boxes) and otherwise report the cue as dropped.
    """
    data, w, h, x, y, used = renderer.render(cue_lines)
    try:
        spu = build_spu(data, w, h, x, y, start, end,
                        absolute_dates=absolute_dates)
    except ValueError:
        if style.font_size <= 16:
            raise OversizeSubtitle()
        data, w, h, x, y, used = renderer.render(cue_lines)
        spu = build_spu(data, w, h, x, y, start, end,
                        absolute_dates=absolute_dates)
    return dict(start=start, end=end, data=data, w=w, h=h, x=x, y=y,
                lines=used, spu=spu)


def collect(args):
    with open(args.srt, "r", encoding="utf-8", errors="replace") as fh:
        cues = parse(fh.read())
    if not cues:
        raise SystemExit(f"no usable cues found in {args.srt}")

    canvas = PAL_SIZE if args.size == "pal" else NTSC_SIZE
    style = RenderStyle(
        font_path=args.font,
        font_size=args.font_size,
        margin_x=args.margin_x,
        margin_bottom=args.margin_bottom,
        max_lines=max(1, args.max_lines),
        outline=max(0, args.outline),
        shadow=max(0, args.shadow),
        uppercase=args.uppercase,
    )
    renderer = Renderer(style, canvas)

    engine = args.engine
    if engine == "auto":
        ff = _ffmpeg_exe()
        shaped = needs_shaping("".join(ln for cue in cues for ln in cue.lines))
        if shaped and ff:
            engine = "ass"
            print("engine: libass (this script needs text shaping; Pillow "
                  "alone would mis-place vowel signs). Override with --engine.")
        else:
            engine = "pil"
            if shaped:
                print("! this script needs text shaping but ffmpeg was not "
                      "found; falling back to Pillow, which may mis-render "
                      "combining marks. Install ffmpeg for correct output.",
                      file=sys.stderr)

    shift = int(args.delay * 1000)
    frame_ms = 1000.0 / args.fps if args.fps > 0 else 40.0
    items, skipped, long_cues, oversized = [], 0, 0, 0

    prepended: list[dict] = [dict() for _ in cues]
    if engine == "ass":
        from .ass import AssRenderer
        ff = _ffmpeg_exe()
        if not ff:
            raise SystemExit("--engine ass needs ffmpeg on PATH (or "
                             "pip install imageio-ffmpeg)")
        fontsdir = os.path.dirname(os.path.abspath(style.font_path)) \
            if style.font_path else None
        ar = AssRenderer(style, canvas, ff, fps=args.fps, fontsdir=fontsdir,
                         extra_style=args.force_style)
        rendered = ar.render_all(cues)
        for i, res in enumerate(rendered):
            if res:
                prepended[i] = res

    for ci, cue in enumerate(cues):
        start = int(round((cue.start_ms + shift) / frame_ms) * frame_ms)
        end = int(round((cue.end_ms + shift) / frame_ms) * frame_ms)
        if start < 0:
            start = 0
        if end - start < args.min_duration * 1000:
            end = start + int(round(max(args.min_duration, 0.7) * 1000))
        if end <= start:
            skipped += 1
            continue
        try:
            if engine == "ass":
                res = prepended[ci]
                if not res:
                    skipped += 1
                    continue
                item = dict(res, start=start, end=end, lines=0)
                try:
                    item["spu"] = build_spu(
                        res["data"], res["w"], res["h"], res["x"], res["y"],
                        start, end, absolute_dates=args.absolute_dates)
                except ValueError:
                    raise OversizeSubtitle()
            else:
                item = encode_cue(renderer, style, cue.lines, start, end,
                                  args.absolute_dates)
                if item["lines"] > style.max_lines + 1:
                    long_cues += 1
        except OversizeSubtitle:
            oversized += 1
            continue
        except ValueError:
            skipped += 1
            continue
        items.append(item)

    if not items:
        raise SystemExit("nothing to encode -- all cues were empty or unrenderable")
    if oversized:
        print(f"  ! {oversized} cue(s) exceeded the 64 KiB DVD subpicture limit "
              f"even at the minimum font size and were dropped", file=sys.stderr)
    return canvas, renderer, items, len(cues), skipped, long_cues


def write_outputs(args, canvas, items) -> tuple[str, str]:
    palette = list(DEFAULT_PALETTE)
    palette[1] = args.fg
    palette[2] = args.outline_color
    opts = VobSubOptions(
        language=args.lang,
        title=args.title or os.path.splitext(os.path.basename(args.srt))[0],
        palette=palette,
    )
    idx_bytes, sub_bytes = build([(it["start"], it["spu"]) for it in items],
                                canvas, opts)

    out_idx = args.output or os.path.splitext(args.srt)[0] + ".idx"
    if not out_idx.lower().endswith(".idx"):
        out_idx += ".idx"
    out_sub = os.path.splitext(out_idx)[0] + ".sub"

    with open(out_idx, "wb") as fh:
        fh.write(idx_bytes)
    with open(out_sub, "wb") as fh:
        fh.write(sub_bytes)
    return out_idx, out_sub


def verify(out_idx: str, out_sub: str, items, canvas) -> int:
    """Decode the .sub we just wrote and compare it to what we rendered."""
    from .decode import decode_spu, parse_idx, parse_sub

    with open(out_idx, "r", encoding="utf-8", errors="replace") as fh:
        idx = parse_idx(fh.read())
    with open(out_sub, "rb") as fh:
        subs = parse_sub(fh.read())

    errors = 0
    if idx["size"] != tuple(canvas):
        print(f"  ! idx size {idx['size']} != canvas {canvas}")
        errors += 1
    if len(subs) != len(items):
        print(f"  ! {len(subs)} packets in .sub, expected {len(items)}")
        errors += 1
    if len(idx["timestamps"]) != len(items):
        print(f"  ! {len(idx['timestamps'])} idx timestamps, expected {len(items)}")
        errors += 1

    for (pts_ms, spu), item in zip(subs, items):
        if spu != item["spu"]:
            print(f"  ! cue at {item['start']} ms: bytes read back differ from "
                  f"bytes written")
            errors += 1
            continue
        d = decode_spu(spu)
        want_h = item["h"] + (item["h"] & 1)     # encoder pads odd rows to even
        if (d.width, d.height) != (item["w"], want_h):
            print(f"  ! cue at {item['start']} ms: rect {d.width}x{d.height} != "
                  f"{item['w']}x{want_h}")
            errors += 1
        elif (d.x1, d.y1) != (item["x"], item["y"]):
            print(f"  ! cue at {item['start']} ms: position {d.x1},{d.y1} != "
                  f"{item['x']},{item['y']}")
            errors += 1
        else:
            src = [item["data"][i * item["w"]:(i + 1) * item["w"]]
                   for i in range(item["h"])]
            if item["h"] & 1:          # encoder pads a transparent row to even it
                src.append(bytes(item["w"]))
            bad = sum(1 for a, b in zip(src, d.rows) if a != b)
            if bad:
                print(f"  ! cue at {item['start']} ms: {bad}/{len(src)} rows "
                      f"decode differently than they were rendered")
                errors += 1
        if pts_ms != item["start"]:
            print(f"  ! packet PTS {pts_ms} != cue start {item['start']}")
            errors += 1

    with open(out_sub, "rb") as fh:
        blob = fh.read()
    for (ms, pos), item in zip(idx["timestamps"], items):
        if ms != item["start"]:
            print(f"  ! idx timestamp {ms} != cue start {item['start']}")
            errors += 1
        if blob[pos:pos + 4] != b"\x00\x00\x01\xbd":
            print(f"  ! filepos {pos:#x} does not point at a PES start code")
            errors += 1

    print(f"  round-trip: {len(subs)} SPU packets decoded, {errors} problem(s)")
    return errors


def mkv_merge(args, idx_path: str, video: str) -> int:
    """Mux the VobSub pair in as a real subtitle track (video/audio untouched).

    FFmpeg's vobsub demuxer reads our .idx/.sub pair and the dvdsub packets are
    copied verbatim into Matroska -- no decoding, no re-encoding.
    """
    out = args.mkv_output or (os.path.splitext(video)[0] + ".vobsub.mkv")
    ff = _ffmpeg_exe()
    if not ff:
        print("ffmpeg not found (nor imageio-ffmpeg) -- skipping the mkv step. "
              "The .idx/.sub pair works on its own as a sidecar.", file=sys.stderr)
        return 2
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-i", video, "-i", idx_path,
           "-map", "0", "-map", "1:s",
           "-c", "copy", "-map_metadata", "-1",
           "-metadata:s:s:0", f"language={args.lang}",
           "-disposition:s:0", "default",
           out]
    print("muxing ->", out)
    rc = subprocess.call(cmd)
    if rc == 0:
        print(f"  {os.path.getsize(out)} B; subtitle is a DVD (vobsub) track, "
              f"video/audio stream-copied")
    else:
        print("  ffmpeg failed; the .idx/.sub pair is still usable as a sidecar",
              file=sys.stderr)
    return rc


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.exists(args.srt):
        raise SystemExit(f"no such file: {args.srt}")
    # fail before spending a minute rendering, not after
    args.lang = args.lang.lower()
    if not (2 <= len(args.lang) <= 3 and args.lang.isascii() and args.lang.isalnum()):
        # usage error -> exit 2, the way getopt conventions expect
        print(f"--lang must be a 2-3 letter ASCII code (ISO 639-2/639-1), got "
              f"{args.lang!r}: the .idx id: field is not free text",
              file=sys.stderr)
        return 2

    canvas, renderer, items, total, skipped, long_cues = collect(args)
    print(f"{total} cues -> {len(items)} bitmaps on a {canvas[0]}x{canvas[1]} "
          f"{args.size.upper()} canvas")
    if long_cues:
        print(f"  note: {long_cues} cue(s) needed extra lines or a smaller font")
    if skipped:
        print(f"  {skipped} cue(s) skipped (empty or unrenderable)")

    if args.dry_run:
        base = args.output or os.path.splitext(args.srt)[0]
        stem = base[:-4] if base.lower().endswith(".idx") else base
        rough = sum(len(it["spu"]) + 15 for it in items)
        print(f"would write {stem}.idx and {stem}.sub "
              f"(~{rough // 1024} KiB of subpicture data, {len(items)} cues)")
        return 0

    out_idx, out_sub = write_outputs(args, canvas, items)
    print(f"wrote {out_idx} ({os.path.getsize(out_idx)} B) and "
          f"{out_sub} ({os.path.getsize(out_sub)} B)")

    rc = 0
    if args.preview is not None:
        from .preview import write_previews
        shots = write_previews(out_idx, out_dir=args.preview or None)
        keep = shots[:args.preview_limit]
        for extra in shots[len(keep):]:
            os.unlink(extra)
        if keep:
            print(f"previews: {', '.join(os.path.basename(k) for k in keep)}"
                  + (f" (+{len(shots) - len(keep)} more written then deleted)"
                     if len(shots) > len(keep) else ""))
        else:
            print("  ! no preview could be rendered", file=sys.stderr)

    if args.verify:
        rc = verify(out_idx, out_sub, items, canvas)

    if args.mkv:
        video = args.mkv
        if video == "AUTO":
            base = os.path.splitext(args.srt)[0]
            for ext in VIDEO_EXTS:
                if os.path.exists(base + ext):
                    video = base + ext
                    break
            else:
                raise SystemExit("--mkv given, but no video matches "
                                 f"{base}.{{{','.join(VIDEO_EXTS[1:])}}}")
        rc = mkv_merge(args, out_idx, video) or rc

    if not args.mkv:
        print(f"\nUse it: keep {os.path.basename(out_idx)} and "
              f"{os.path.basename(out_sub)} together next to the video and pick "
              f"them in the TV's subtitle menu, or re-run with --mkv.")
    return rc
