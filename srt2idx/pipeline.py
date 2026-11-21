"""The conversion itself, as a callable with progress.

No printing in here: :mod:`srt2idx.cli` renders the report as text and
``webapp.py`` serialises it to JSON.  Both drive the exact same code path, so
what the browser produces is what the command line produces.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable

from .render import NTSC_SIZE, PAL_SIZE, RenderStyle, Renderer
from .srt import needs_shaping, parse
from .settings import Options, UsageError
from .spu import build_spu
from .vobsub import DEFAULT_PALETTE, VobSubOptions, build

VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv",
              ".mpg", ".mpeg", ".ts", ".m2ts", ".vob")


def ffmpeg_exe() -> str | None:
    """ffmpeg on PATH, else the one bundled with imageio-ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


class OversizeSubtitle(Exception):
    """The cue needs more pixels than one DVD subpicture can carry."""


class Cancelled(Exception):
    """Raised inside the loop when the caller asks us to stop."""


Progress = Callable[[str, int, int, str], None]


@dataclass
class Report:
    """Everything a caller (terminal or browser) needs to know."""
    options: Options
    canvas: tuple[int, int] = PAL_SIZE
    engine: str = "pil"
    cues: int = 0
    encoded: int = 0
    skipped: int = 0
    long_cues: int = 0
    oversized: int = 0
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    idx_path: str | None = None
    sub_path: str | None = None
    idx_size: int = 0
    sub_size: int = 0
    spu_bytes: int = 0
    verify_problems: list[str] = field(default_factory=list)
    previews: list[str] = field(default_factory=list)
    mkv_path: str | None = None
    mkv_rc: int | None = None
    elapsed: float = 0.0

    @property
    def ok(self) -> bool:
        return (self.encoded > 0 and not self.verify_problems
                and (self.mkv_rc in (None, 0)))

    def to_json(self) -> dict:
        import json
        return json.loads(json.dumps(_asdict(self), default=str))


def _asdict(r: Report) -> dict:
    from dataclasses import asdict
    d = asdict(r)
    d["ok"] = r.ok
    d["canvas"] = list(r.canvas)
    d.pop("options", None)
    return d


# --------------------------------------------------------------------- steps
def resolve_engine(opts: Options, text: str, notes: list[str],
                   warnings: list[str]) -> str:
    if opts.engine != "auto":
        if opts.engine == "ass" and not ffmpeg_exe():
            raise UsageError("engine 'ass' needs ffmpeg on PATH "
                             "(or pip install imageio-ffmpeg)")
        return opts.engine
    ff = ffmpeg_exe()
    if needs_shaping(text):
        if ff:
            notes.append("engine: libass (this script needs text shaping; "
                         "Pillow alone would mis-place vowel signs)")
            return "ass"
        warnings.append("this script needs text shaping but ffmpeg was not "
                        "found; falling back to Pillow, which may mis-render "
                        "combining marks. Install ffmpeg for correct output.")
        return "pil"
    return "pil"


def resolve_font(opts: Options, text: str, notes: list[str] | None = None):
    """-> (font path or None, directory libass should index).

    The user's --font always wins.  Otherwise: a script the platform default
    face cannot show is picked from what is actually installed, because the
    default is platform-specific (there is no DejaVu on Windows, and its
    Sinhala/Devanagari faces answer to "iskpota"/"Nirmala"), and libass in a
    wheel-built ffmpeg often has no fontconfig at all -- then it can only use
    the fontsdir it is told about, so a bare --engine ass would draw nothing.
    """
    from .render import suggest_font, system_font_dirs

    font = opts.font
    if font and not os.path.exists(font):
        # a family name ("Nirmala UI") rather than a path: both engines are
        # happier with a file, and the ass fontsdir must be a directory
        from .render import font_by_name
        hit = font_by_name(font)
        if hit:
            if notes is not None:
                notes.append(f'font "{font}" resolved to {hit}')
            font = hit
    if not font:
        guess, script = suggest_font(text)
        if guess:
            font = guess
            if notes is not None:
                from .ass import font_family
                notes.append(f"font: {font_family(guess)} picked for {script} "
                             f"({os.path.basename(guess)}; --font overrides)")
    if font:
        return font, os.path.dirname(os.path.abspath(font))
    dirs = system_font_dirs()
    return None, (dirs[0] if dirs else None)


def timed(options: Options, cues, shift_ms: int, frame_ms: float):
    """Snap cue boundaries to the frame grid and rescue sub-minimum ones.

    Yields (original index, cue, start, end): the index is needed to find the
    pre-rendered bitmap again, because dropped cues must not shift it.
    """
    out = []
    for i, cue in enumerate(cues):
        start = int(round((cue.start_ms + shift_ms) / frame_ms) * frame_ms)
        end = int(round((cue.end_ms + shift_ms) / frame_ms) * frame_ms)
        if start < 0:
            start = 0
        if end - start < options.min_duration * 1000:
            end = start + int(round(max(options.min_duration, 0.7) * 1000))
        if end > start:
            out.append((i, cue, start, end))
    return out


def encode_cue(renderer: Renderer, style: RenderStyle, cue_lines, start, end,
               absolute_dates: bool) -> dict:
    """Render + pack one cue, shrinking the font if the SPU will not fit.

    A DVD subpicture is addressed with 16-bit offsets, so one rectangle cannot
    exceed 64 KiB once RLE'd.  Text that long is unreadable on a TV anyway; we
    shrink the font once and otherwise report the cue as dropped.
    """
    data, w, h, x, y, used = renderer.render(cue_lines)
    try:
        spu = build_spu(data, w, h, x, y, start, end,
                        absolute_dates=absolute_dates)
    except ValueError:
        if style.font_size <= 16:
            raise OversizeSubtitle()
        style.font_size = max(16, int(style.font_size * 0.8))
        renderer.set_font(style.font_size)
        data, w, h, x, y, used = renderer.render(cue_lines)
        spu = build_spu(data, w, h, x, y, start, end,
                        absolute_dates=absolute_dates)
    return dict(start=start, end=end, data=data, w=w, h=h, x=x, y=y,
                lines=used, spu=spu)


def build_items(opts: Options, cues, engine: str, progress: Progress | None,
                cancelled: Callable[[], bool] | None,
                notes: list[str] | None = None):
    """-> (items, skipped, long_cues, oversized)"""
    canvas = PAL_SIZE if opts.size == "pal" else NTSC_SIZE
    body = "".join(ln for c in cues for ln in c.lines)
    font, fontsdir = resolve_font(opts, body, notes)
    style = RenderStyle(
        font_path=font,
        font_size=opts.font_size,
        margin_x=opts.margin_x,
        margin_bottom=opts.margin_bottom,
        max_lines=max(1, opts.max_lines),
        outline=max(0, opts.outline),
        shadow=max(0, opts.shadow),
        bold=opts.bold,
        line_spacing=opts.line_spacing,
        uppercase=opts.uppercase,
    )
    renderer = Renderer(style, canvas)
    shift = int(opts.delay * 1000)
    frame_ms = 1000.0 / opts.fps if opts.fps > 0 else 40.0
    planned = timed(opts, cues, shift, frame_ms)

    prebaked: list[dict] = [dict() for _ in cues]
    if engine == "ass":
        from .ass import AssRenderer
        ff = ffmpeg_exe()
        assert ff, "resolve_engine() already checked"
        ar = AssRenderer(style, canvas, ff, fps=opts.fps, fontsdir=fontsdir,
                         extra_style=opts.force_style)
        if progress:
            progress("render", 0, len(planned), "libass: one pass for all cues")
        for i, res in enumerate(ar.render_all(cues)):
            if res:
                prebaked[i] = res
        if notes is not None:
            notes.extend(ar.notes)

    items, skipped, long_cues, oversized = [], 0, 0, 0
    for k, (ci, cue, start, end) in enumerate(planned):
        if cancelled and cancelled():
            raise Cancelled()
        try:
            if engine == "ass":
                res = prebaked[ci]
                if not res:
                    skipped += 1
                    continue
                item = dict(res, start=start, end=end, lines=0)
                try:
                    item["spu"] = build_spu(res["data"], res["w"], res["h"],
                                            res["x"], res["y"], start, end,
                                            absolute_dates=opts.absolute_dates)
                except ValueError:
                    raise OversizeSubtitle()
            else:
                item = encode_cue(renderer, style, cue.lines, start, end,
                                  opts.absolute_dates)
                if item["lines"] > style.max_lines + 1:
                    long_cues += 1
        except OversizeSubtitle:
            oversized += 1
            continue
        except ValueError:
            skipped += 1
            continue
        items.append(item)
        if progress and (k % 25 == 0 or k == len(planned) - 1):
            progress("encode", k + 1, len(planned), "")
    return canvas, style, renderer, items, skipped, long_cues, oversized


def write_pair(opts: Options, canvas, items) -> tuple[str, str]:
    palette = list(DEFAULT_PALETTE)
    palette[1] = opts.fg
    palette[2] = opts.outline_color
    vopts = VobSubOptions(
        language=opts.lang,
        title=opts.title or os.path.splitext(os.path.basename(opts.srt))[0],
        palette=palette,
    )
    idx_bytes, sub_bytes = build([(it["start"], it["spu"]) for it in items],
                                 canvas, vopts)
    out_idx = opts.output or os.path.splitext(opts.srt)[0] + ".idx"
    if not out_idx.lower().endswith(".idx"):
        out_idx += ".idx"
    out_sub = os.path.splitext(out_idx)[0] + ".sub"
    os.makedirs(os.path.dirname(os.path.abspath(out_idx)) or ".", exist_ok=True)
    with open(out_idx, "wb") as fh:
        fh.write(idx_bytes)
    with open(out_sub, "wb") as fh:
        fh.write(sub_bytes)
    return out_idx, out_sub


def verify(out_idx: str, out_sub: str, items, canvas) -> list[str]:
    """Decode the written pair and compare it to what we rendered."""
    from .decode import decode_spu, parse_idx, parse_sub

    problems: list[str] = []
    with open(out_idx, "r", encoding="utf-8", errors="replace") as fh:
        idx = parse_idx(fh.read())
    with open(out_sub, "rb") as fh:
        blob = fh.read()
    subs = parse_sub(blob)

    if idx["size"] != tuple(canvas):
        problems.append(f"idx size {idx['size']} != canvas {tuple(canvas)}")
    if len(subs) != len(items):
        problems.append(f"{len(subs)} packets in .sub, expected {len(items)}")
    if len(idx["timestamps"]) != len(items):
        problems.append(f"{len(idx['timestamps'])} idx timestamps, "
                        f"expected {len(items)}")

    for (pts_ms, spu), item in zip(subs, items):
        if spu != item["spu"]:
            problems.append(f"cue at {item['start']} ms: bytes read back differ "
                            f"from bytes written")
            continue
        d = decode_spu(spu)
        want_h = item["h"] + (item["h"] & 1)   # odd heights are padded to even
        if (d.width, d.height) != (item["w"], want_h):
            problems.append(f"cue at {item['start']} ms: rect {d.width}x"
                            f"{d.height} != {item['w']}x{want_h}")
        elif (d.x1, d.y1) != (item["x"], item["y"]):
            problems.append(f"cue at {item['start']} ms: position {d.x1},{d.y1}"
                            f" != {item['x']},{item['y']}")
        else:
            src = [item["data"][i * item["w"]:(i + 1) * item["w"]]
                   for i in range(item["h"])]
            if item["h"] & 1:
                src.append(bytes(item["w"]))
            bad = sum(1 for a, b in zip(src, d.rows) if a != b)
            if bad:
                problems.append(f"cue at {item['start']} ms: {bad}/{len(src)} "
                                f"rows decode differently than rendered")
        if pts_ms != item["start"]:
            problems.append(f"packet PTS {pts_ms} != cue start {item['start']}")

    for (ms, pos), item in zip(idx["timestamps"], items):
        if ms != item["start"]:
            problems.append(f"idx timestamp {ms} != cue start {item['start']}")
        if blob[pos:pos + 4] != b"\x00\x00\x01\xbd":
            problems.append(f"filepos {pos:#x} does not point at a PES start code")
    return problems


def mkv_merge(opts: Options, idx_path: str, video: str | None,
              notes: list[str], warnings: list[str]) -> tuple[str | None, int]:
    """Mux the pair in as a real subtitle track (video/audio stream-copied).

    FFmpeg's vobsub demuxer reads our .idx/.sub and the dvdsub packets are
    copied verbatim into Matroska -- no decoding, no re-encoding.
    """
    if video is None or video == "AUTO":
        base = os.path.splitext(opts.srt)[0]
        for ext in VIDEO_EXTS:
            if os.path.exists(base + ext):
                video = base + ext
                break
        else:
            raise UsageError(f"mux requested but no video matches {base}"
                             f".{{{','.join(e[1:] for e in VIDEO_EXTS)}}}")
    if not os.path.exists(video):
        raise UsageError(f"no such video: {video}")

    ff = ffmpeg_exe()
    if not ff:
        raise UsageError("muxing needs ffmpeg on PATH (or pip install "
                         "imageio-ffmpeg); the .idx/.sub pair works on its own "
                         "as a sidecar")
    out = opts.mkv_output or (os.path.splitext(video)[0] + ".vobsub.mkv")
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-i", video, "-i", idx_path,
           "-map", "0", "-map", "1:s",
           "-c", "copy", "-map_metadata", "-1",
           "-metadata:s:s:0", f"language={opts.lang}",
           "-disposition:s:0", "default", out]
    rc = subprocess.call(cmd, stdout=subprocess.DEVNULL)
    if rc == 0:
        notes.append(f"muxed {out} ({os.path.getsize(out)} B); subtitle is a "
                     f"DVD (vobsub) track, video/audio stream-copied")
    else:
        warnings.append("ffmpeg failed to mux; the .idx/.sub pair is still "
                        "usable as a sidecar")
    return (out if rc == 0 else None), rc


# ------------------------------------------------------------------ the run
def run(opts: Options, *, progress: Progress | None = None,
        cancelled: Callable[[], bool] | None = None) -> Report:
    """Full conversion. Raises UsageError for anything the user must fix."""
    import time
    t0 = time.time()
    if not opts.srt:
        raise UsageError("no input .srt given")
    if not os.path.exists(opts.srt):
        raise UsageError(f"no such file: {opts.srt}", 1)
    rep = Report(options=opts)
    notes, warnings = rep.notes, rep.warnings

    if progress:
        progress("read", 0, 1, opts.srt)
    from .srt import decode
    with open(opts.srt, "rb") as fh:
        raw = fh.read()
    text, enc = decode(raw)
    if enc:
        notes.append(f"read {opts.srt} as {enc} -- it is not valid UTF-8; "
                     f"re-save as UTF-8 if any accents look wrong")
    cues = parse(text)
    if not cues:
        raise UsageError(f"no usable cues found in {opts.srt}", 1)
    rep.cues = len(cues)

    engine = resolve_engine(opts, "".join(ln for c in cues for ln in c.lines),
                            notes, warnings)
    rep.engine = engine

    if progress:
        progress("analyse", 0, 1, "")
    canvas, style, renderer, items, skipped, long_cues, oversized = build_items(
        opts, cues, engine, progress, cancelled, notes)

    rep.canvas = tuple(canvas)
    rep.encoded = len(items)
    rep.skipped = skipped
    rep.long_cues = long_cues
    rep.oversized = oversized
    rep.spu_bytes = sum(len(it["spu"]) for it in items)
    if oversized:
        warnings.append(f"{oversized} cue(s) exceeded the 64 KiB DVD subpicture "
                        f"limit even at the minimum font size and were dropped")
    if not items:
        raise UsageError("nothing to encode -- all cues were empty or "
                         "unrenderable", 1)

    stem = (os.path.splitext(opts.output)[0] if opts.output
            else os.path.splitext(opts.srt)[0])
    if stem.lower().endswith(".idx"):
        stem = stem[:-4]
    if opts.dry_run:
        notes.append(f"would write {stem}.idx and {stem}.sub "
                     f"(~{rep.spu_bytes // 1024} KiB of subpicture data, "
                     f"{len(items)} cues)")
        rep.elapsed = time.time() - t0
        return rep

    if cancelled and cancelled():
        raise Cancelled()
    out_idx, out_sub = write_pair(opts, canvas, items)
    rep.idx_path, rep.sub_path = out_idx, out_sub
    rep.idx_size, rep.sub_size = os.path.getsize(out_idx), os.path.getsize(out_sub)

    if opts.preview:
        if progress:
            progress("preview", 0, 1, "")
        from .preview import write_previews
        shots = write_previews(out_idx)
        keep = shots[:max(1, opts.preview_limit)]
        for extra in shots[len(keep):]:
            try:
                os.unlink(extra)
            except OSError:
                pass
        rep.previews = keep

    if opts.verify:
        if progress:
            progress("verify", 0, 1, "")
        rep.verify_problems = verify(out_idx, out_sub, items, canvas)

    if opts.mkv:
        if progress:
            progress("mux", 0, 1, "")
        rep.mkv_path, rep.mkv_rc = mkv_merge(opts, out_idx, opts.video,
                                            notes, warnings)

    rep.elapsed = time.time() - t0
    return rep


# ------------------------------------------------------- live sample render
def sample_png(text: str, opts: Options) -> bytes:
    """Render one cue with the current settings and return a PNG.

    Used by the web UI's live preview: it goes through the same renderer and
    the same quantiser as a real conversion, so what you see is what the TV
    will show -- including whether your font actually has these glyphs.
    """
    import io
    from PIL import Image

    from .srt import Cue
    canvas = PAL_SIZE if opts.size == "pal" else NTSC_SIZE
    img = Image.new("RGB", canvas, (8, 8, 8))

    font, fontsdir = resolve_font(opts, text)
    style = RenderStyle(
        font_path=font, font_size=opts.font_size, margin_x=opts.margin_x,
        margin_bottom=opts.margin_bottom, max_lines=max(1, opts.max_lines),
        outline=max(0, opts.outline), shadow=max(0, opts.shadow),
        bold=opts.bold, line_spacing=opts.line_spacing,
        uppercase=opts.uppercase,
    )
    engine = opts.engine
    if engine == "auto":
        engine = "ass" if (needs_shaping(text) and ffmpeg_exe()) else "pil"

    res = None
    if engine == "ass" and text.strip():
        from .ass import AssRenderer
        ff = ffmpeg_exe()
        ar = AssRenderer(style, canvas, ff, fps=opts.fps, fontsdir=fontsdir,
                         extra_style=opts.force_style)
        out = ar.render_all([Cue(0, 2000, text.split("\n") or [""])])
        res = out[0] if out else None
    elif text.strip():
        renderer = Renderer(style, canvas)
        data, w, h, x, y = renderer.render(text.split("\n"))[:5]
        res = dict(data=data, w=w, h=h, x=x, y=y)

    if res:
        cols = {1: opts.fg, 2: opts.outline_color}
        px = img.load()
        data, w, h, x, y = res["data"], res["w"], res["h"], res["x"], res["y"]
        for r in range(h):
            yy = y + r
            if not 0 <= yy < canvas[1]:
                continue
            for c in range(w):
                code = data[r * w + c]
                if not code:
                    continue
                xx = x + c
                if 0 <= xx < canvas[0]:
                    v = cols.get(code, 0)
                    px[xx, yy] = (v >> 16 & 255, v >> 8 & 255, v & 255)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ------------------------------------------------------------------- probing
def probe_video(path: str) -> dict:
    """Stream info for the mux panel: size, duration, whether it has audio."""
    ff = ffmpeg_exe()
    if not ff:
        raise UsageError("probing needs ffmpeg")
    r = subprocess.run([ff, "-hide_banner", "-i", path],
                       capture_output=True, text=True)
    err = r.stderr
    info: dict = {"path": path, "streams": []}
    import re
    for m in re.finditer(r"Stream #\d+:\d+[^:]*: (\w+): ([^\n]+)", err):
        info["streams"].append({"type": m.group(1), "detail": m.group(2).strip()})
    m = re.search(r"Stream #\d+:\d+.*?Video:.*?(\d{3,5})x(\d{3,5})", err)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
        # a source that is 480-line-or-less high suggests NTSC-era material
        info["suggested_size"] = "ntsc" if 0 < info["height"] <= 486 else "pal"
    m = re.search(r"Duration: (\d+):(\d+):(\d+)\.(\d+)", err)
    if m:
        h, mi, s, cs = (int(g) for g in m.groups())
        info["duration_ms"] = ((h * 60 + mi) * 60 + s) * 1000 + cs * 10
    info["has_audio"] = any(st["type"] == "Audio" for st in info["streams"])
    info["subtitle_count"] = sum(st["type"] == "Subtitle"
                                for st in info["streams"])
    return info


def fonts_in(dirs: list[str]) -> list[dict]:
    """Usable faces on this machine, for the font picker."""
    out, seen = [], set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for name in sorted(files):
                if not name.lower().endswith((".ttf", ".otf", ".ttc")):
                    continue
                path = os.path.join(root, name)
                if path in seen:
                    continue
                seen.add(path)
                label = name
                try:
                    from PIL import ImageFont
                    fam = ImageFont.truetype(path, 12).getname()
                    label = f"{fam[0]} {fam[1]}".strip()
                except Exception:
                    pass
                out.append(dict(path=path, name=name, label=label))
    out.sort(key=lambda f: f["label"].lower())
    return out
