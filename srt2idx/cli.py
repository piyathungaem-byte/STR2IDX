"""srt2idx command line interface.

Thin: it turns argv into an :class:`~srt2idx.settings.Options`, runs
:func:`srt2idx.pipeline.run`, and prints the report.  All the work lives in the
pipeline, which the web app calls in exactly the same way.

    srt2idx movie.srt                     # -> movie.idx + movie.sub
    srt2idx movie.srt --mkv               # -> movie.vobsub.mkv
    srt2idx movie.srt --font-size 40 --no-preview
    srt2idx --help                        # every switch, generated from one table
"""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, pipeline, settings
from .pipeline import Cancelled, UsageError


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="srt2idx",
        description="Convert .srt subtitles into VobSub .idx + .sub, for TVs "
                    "that only accept bitmap subtitles.",
        epilog="Every setting here also drives the web UI: run "
               "`python webapp.py` and the same options appear as a form.",
    )
    p.add_argument("srt", help="input SubRip .srt file")
    settings.add_to_parser(p)
    p.add_argument("--version", action="version", version=f"srt2idx {__version__}")
    return p


def main(argv=None) -> int:
    parser = build_parser()
    try:
        ns = parser.parse_args(argv)
        opts = settings.from_args(ns)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    except SystemExit as e:                      # --help / --version / argparse
        return e.code if isinstance(e.code, int) else 2

    last = [None]

    def progress(stage, done, total, note):
        if not opts.verbose or not sys.stdout.isatty():
            return
        if stage == "encode":
            if done % 100 and done != total:
                return
            sys.stdout.write(f"\r  encoding {done}/{total}")
        else:
            sys.stdout.write(f"\r  {stage} {done}/{total} {note}")
        sys.stdout.flush()
        last[0] = stage

    try:
        rep = pipeline.run(opts, progress=progress)
    except UsageError as e:
        if last[0] and sys.stdout.isatty():
            sys.stdout.write("\n")
        print(f"error: {e}", file=sys.stderr)
        return e.code
    except Cancelled:
        print("cancelled", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    if last[0] and sys.stdout.isatty():
        sys.stdout.write("\n")

    canvas = rep.canvas
    print(f"{rep.cues} cues -> {rep.encoded} bitmaps on a {canvas[0]}x"
          f"{canvas[1]} {opts.size.upper()} canvas")
    for n in rep.notes:
        print(n)
    if rep.long_cues:
        print(f"  note: {rep.long_cues} cue(s) needed extra lines or a smaller "
              f"font")
    if rep.skipped:
        print(f"  {rep.skipped} cue(s) skipped (empty or unrenderable)")
    if rep.oversized:
        print(f"  ! {rep.oversized} cue(s) exceeded the 64 KiB DVD subpicture "
              f"limit even at the minimum font size and were dropped",
              file=sys.stderr)
    for warn in rep.warnings:
        print(f"! {warn}", file=sys.stderr)

    if opts.dry_run:
        return 0

    print(f"wrote {rep.idx_path} ({rep.idx_size} B) and {rep.sub_path} "
          f"({rep.sub_size} B)")
    if rep.previews:
        print(f"previews: {', '.join(os.path.basename(p) for p in rep.previews)}")
    if opts.verify:
        print(f"  round-trip: {rep.encoded} SPU packets decoded, "
              f"{len(rep.verify_problems)} problem(s)")
        for prob in rep.verify_problems:
            print(f"  ! {prob}", file=sys.stderr)
    if rep.mkv_path:
        print(f"muxed -> {rep.mkv_path}")
    elif not opts.mkv:
        print(f"\nUse it: keep {os.path.basename(rep.idx_path)} and "
              f"{os.path.basename(rep.sub_path)} together next to the video and "
              f"pick them in the TV's subtitle menu, or re-run with --mkv.")
    if opts.verbose:
        print(f"  {rep.elapsed:.1f}s, {rep.spu_bytes // 1024} KiB of subpicture "
              f"data, engine={rep.engine}")

    if rep.verify_problems:
        return 1
    if rep.mkv_rc not in (None, 0):
        return rep.mkv_rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
