"""Render cues to DVD subpicture bitmaps via libass (through ffmpeg).

Why this exists: Pillow here is built without Raqm, so it can place glyphs but
cannot *shape* them.  Sinhala, Devanagari, Arabic and Hebrew need vowel-sign
reordering and joining; libass (HarfBuzz + FriBiDi) does that properly, and
also gives real wrapping and font fallback.  We keep only the raster: libass
draws, we quantise into the four DVD colour codes.

The trick that keeps this cheap: libass is driven by a rewritten .srt in which
cue k is displayed on frame k only, so ONE ffmpeg pass over N frames of a flat
grey canvas yields all N bitmaps -- no per-cue spawn, and no rendering 117k
frames just to cover a 78-minute film.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from PIL import Image

CANVAS_GREY = 128      # the flat background libass draws onto
BG_LO, BG_HI = 124, 132        # grey band treated as "nothing drawn here"
BODY_MIN = 160                 # at or above this: glyph body

# libass composites white text (255) with a dark outline (0) over CANVAS_GREY,
# so a mid-grey can be either background or an anti-aliased outline pixel.  We
# call the grey band background, bright pixels body, and *everything else*
# outline -- erring towards a slightly bolder outline rather than leaving holes
# between glyph and outline.
_CODE_LUT = bytes(0 if BG_LO <= v <= BG_HI else (1 if v >= BODY_MIN else 2)
                  for v in range(256))
_MASK_LUT = bytes(0 if BG_LO <= v <= BG_HI else 255 for v in range(256))


class AssError(RuntimeError):
    pass


def _srt_time(ms: int) -> str:
    h, r = divmod(max(0, ms), 3_600_000)
    m, r = divmod(r, 60_000)
    s, milli = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def default_family() -> str:
    """A family name that exists on this OS, for when no font was chosen.

    Hard-coding "DejaVu Sans" (a Linux font) silently starves libass of a match
    on Windows and macOS: the cue renders empty and the file still validates.
    """
    if sys.platform.startswith("win"):
        return "Arial"
    if sys.platform == "darwin":
        return "Helvetica"
    return "DejaVu Sans"


def font_family(path: str | None) -> str:
    """Font *family name* libass should select, resolved from the font file."""
    if not path:
        return default_family()
    try:
        from PIL import ImageFont
        return ImageFont.truetype(path, 12).getname()[0] or default_family()
    except Exception:
        return os.path.splitext(os.path.basename(path))[0]


def ffquote(value: str) -> str | None:
    """Quote one filtergraph argument value, Windows-safely, or say we can't.

    ffmpeg parses -vf in two passes: the graph level splits on ","/";" and
    honours '...', then the filter level splits options on ":".  A Windows path
    trips both (C:\\Users\\...).  Forward slashes are accepted by every Windows
    file API, so normalise, escape the option separator, and quote.  A value
    containing "'" cannot be quoted -- ffmpeg has no way to express it inside
    '...' -- and the caller must then go relative instead.  (Checked against
    ffmpeg 9: quoting works, an apostrophe silently desynchronises the parser.)
    """
    v = value.replace("\\", "/")
    if "'" in v:
        return None
    return "'" + v.replace(":", "\\:") + "'"


def filter_src(driver_name: str, style, fontsdir: str | None,
               extra_style: str | None = None) -> str:
    """The whole -vf argument, for a driver file in the process' own cwd.

    The driver is passed as a *bare* filename and ffmpeg is spawned with
    cwd=<the temp dir>, because that is the only spelling that survives every
    temp path: on Windows TMPDIR is C:\\Users\\<name>\\... and a user called
    O'Brien -- or just the drive colon -- breaks filtergraph quoting.
    """
    src = f"subtitles=filename={driver_name}"
    if fontsdir:
        src += f":fontsdir={fontsdir}"        # already quoted, or "."
    return f"{src}:force_style='{force_style(style, extra_style)}',format=gray"


def force_style(style, extra: str | None = None) -> str:
    parts = [
        f"Fontname={font_family(style.font_path)}",
        f"FontSize={style.font_size}",
        f"Bold={1 if style.bold else 0}",
        f"Outline={style.outline}",
        f"Shadow={style.shadow}",
        "Alignment=2",                              # bottom centre
        f"MarginV={style.margin_bottom}",
        f"MarginL={style.margin_x}",
        f"MarginR={style.margin_x}",
    ]
    if extra:
        parts.append(extra)
    return ",".join(parts)


class AssRenderer:
    def __init__(self, style, canvas: tuple[int, int], ffmpeg_exe: str,
                 fps: float = 25.0, fontsdir: str | None = None,
                 extra_style: str | None = None) -> None:
        self.style = style
        self.canvas = canvas
        self.ff = ffmpeg_exe
        self.fps = fps
        # MUST stay absolute: ffmpeg is spawned with cwd=<temp dir>, so a
        # relative fontsdir would point at that temp dir and libass would find
        # no fonts at all -- the cue renders as a row of .notdef boxes that
        # every byte-level check still calls valid.
        self.fontsdir = os.path.abspath(fontsdir) if fontsdir else None
        self.extra_style = extra_style
        self.notes: list[str] = []      # things the caller should mention

    def _fontsdir_arg(self, tmp: str) -> str | None:
        """The fontsdir option value, quoted -- or a relative one.

        ffmpeg parses the filtergraph twice over, and a path containing "'"
        cannot be expressed at all (verified against ffmpeg 9: the apostrophe
        desynchronises the parser and the file simply fails to open).  Since the
        child runs with cwd=tmp, the fallback is to put the face *next to the
        driver* and pass ".", which needs no quoting whatever TMPDIR is called.
        """
        if not self.fontsdir:
            return None
        quoted = ffquote(self.fontsdir)
        if quoted is not None:
            return quoted
        import shutil
        name = os.path.basename(self.style.font_path or "")
        if name:
            shutil.copy2(os.path.join(self.fontsdir, name), tmp)
            self.notes.append("font face copied into a safe temp dir "
                              "(ffmpeg cannot quote this path)")
            return "'.'"
        self.notes.append("libass could not be told where the fonts are; "
                          "pass --font to be sure of the face")
        return None

    def render_all(self, cues) -> list[dict | None]:
        """One dict per cue (data/w/h/x/y of colour codes 0..3), or None."""
        w, h = self.canvas
        if not cues:
            return []
        frame_ms = 1000.0 / self.fps if self.fps > 0 else 40.0

        # cue k -> frame k
        order: list[int] = []
        blocks: list[str] = []
        for k, cue in enumerate(cues):
            text = "\n".join(cue.lines)
            if self.style.uppercase:
                text = text.upper()
            if not text.strip():
                continue
            blocks.append(f"{len(blocks) + 1}\n"
                          f"{_srt_time(int(round(len(blocks) * frame_ms)))} --> "
                          f"{_srt_time(int(round((len(blocks) + 1) * frame_ms)))}\n"
                          f"{text}\n")
            order.append(k)
        if not blocks:
            return [None] * len(cues)

        tmp = tempfile.mkdtemp(prefix="srt2idx-ass-")
        driver = os.path.join(tmp, "driver.srt")
        with open(driver, "w", encoding="utf-8") as fh:
            fh.write("\n".join(blocks))

        src = filter_src("driver.srt", self.style,
                         self._fontsdir_arg(tmp), self.extra_style)
        cmd = [self.ff, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "lavfi", "-i",
               f"color=c=0x{CANVAS_GREY:02x}{CANVAS_GREY:02x}{CANVAS_GREY:02x}"
               f":s={w}x{h}:r={self.fps}",
               "-frames:v", str(len(blocks)),
               "-vf", src,
               "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=tmp)
        assert proc.stdout is not None and proc.stderr is not None
        results: list[dict | None] = [None] * len(cues)
        try:
            for f in range(len(blocks)):
                blob = proc.stdout.read(w * h)
                if len(blob) < w * h:
                    break
                results[order[f]] = self._quantise(blob, w, h)
            err = proc.stderr.read().decode("utf-8", "replace").strip()
        finally:
            proc.stdout.close()
            proc.stderr.close()
            rc = proc.wait()
        _rm(tmp)
        if rc != 0 and not any(results):
            raise AssError(f"ffmpeg/libass render failed (exit {rc}): {err[:500]}")
        return results

    @staticmethod
    def _quantise(blob: bytes, w: int, h: int) -> dict | None:
        """Grey frame -> tight rectangle of DVD colour codes."""
        img = Image.frombytes("L", (w, h), blob)
        box = img.point(_MASK_LUT).getbbox()
        if not box:
            return None
        left, top, right, bottom = box
        right = max(right, left + 1)
        bottom = max(bottom, top + 2)          # a DVD rect needs height > 1
        right = min(right + 1, w)              # keep the AA edge un-clipped
        bottom = min(bottom + 1, h)
        crop = img.crop((left, top, right, bottom)).point(_CODE_LUT)
        return dict(data=crop.tobytes(), w=right - left, h=bottom - top,
                    x=left, y=top)


def _rm(d: str) -> None:
    try:
        for name in os.listdir(d):
            os.unlink(os.path.join(d, name))
        os.rmdir(d)
    except OSError:
        pass
