"""Render subtitle text into 4-colour bitmaps suitable for DVD subpictures.

DVD SPUs have 4 colour codes per pixel, mapped through a 16-entry palette.
We reserve:
    code 0 -> fully transparent (background)
    code 1 -> body text colour
    code 2 -> outline
    code 3 -> reserved / unused (kept transparent)

Anti-aliased coverage from FreeType is quantised into those buckets.  Optional
anti-aliasing spill (code 2 at partial coverage) would look nicer but many TV
firmware only handles binary alpha crisply, so it is opt-in.
"""
from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

# DVD-Video is either NTSC (720x480) or PAL (720x576)
PAL_SIZE = (720, 576)
NTSC_SIZE = (720, 480)

MIN_FONT = 12            # below this a TV subtitle is unreadable anyway
ALPHA_CUTOFF = 128       # FreeType coverage above which a pixel is "on"

#: Faces that exist almost everywhere, tried before scanning whole trees.
DEFAULT_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "arialbd.ttf",            # Windows: resolved against %WINDIR%\\Fonts
    "arial.ttf",
]

FONT_EXTS = (".ttf", ".otf", ".ttc")

#: Font file-name fragments that indicate coverage of a script.  Used only to
#: *guess* which installed face to ask for; the user can always override with
#: --font.  Windows is the reason this exists: there is no DejaVu, and the
#: Sinhala/Devanagari faces live under names like iskpota.ttf / Nirmala.ttf.
SCRIPT_FONT_HINTS = {
    "Sinhala": ("sinhala", "lklug", "iskpota", "iskoola", "akur", "hapsar",
                "gatambe", "nirmala", "nimbuslkl"),
    "Tamil": ("tamil", "nirmala", "lakshman"),
    "Telugu": ("telugu", "nirmala", "suravaram", "gautami"),
    "Kannada": ("kannada", "nirmala", "pothana2"),
    "Malayalam": ("malayalam", "nirmala", "manjari"),
    "Devanagari": ("devanagari", "mangal", "nirmala", "nakula"),
    "Bengali": ("bengali", "nirmala", "shonar"),
    "Gurmukhi": ("gurmukhi", "raavi"),
    "Gujarati": ("gujarati", "shruti"),
    "Oriya": ("oriya", "utkal"),
    "Thai": ("thai", "leelawadee"),
    "Lao": ("lao",),
    "Myanmar": ("myanmar", "myanmar3"),
    "Khmer": ("khmer", "khmemui"),
    "Tibetan": ("tibetan", "kailasa"),
    "Hebrew": ("hebrew", "arialhb"),
    "Arabic": ("arabic", "amil", "traditional"),
}


def system_font_dirs() -> tuple[str, ...]:
    """Directories holding fonts on *this* machine, most specific first."""
    out: list[str] = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        out.append(os.path.join(windir, "Fonts"))
    out.append("C:/Windows/Fonts")            # Wine/Cygwin and bare installs
    local = os.environ.get("LOCALAPPDATA")
    if local:                                 # per-user font installs (Win10+)
        out.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    home = os.path.expanduser("~")
    out.append(os.path.join(home, ".fonts"))
    out.append(os.path.join(home, ".local", "share", "fonts"))
    out += ["/usr/share/fonts", "/usr/local/share/fonts",
            "/Library/Fonts", os.path.join(home, "Library", "Fonts"),
            "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft",
                         "Shared", "Fonts")]
    seen, dirs = set(), []
    for d in out:
        d = d.strip()
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            dirs.append(d)
    return tuple(dirs)


def _norm(name: str) -> str:
    return os.path.splitext(name)[0].lower().replace("-", "").replace(
        "_", "").replace(" ", "").replace("(", "").replace(")", "")


_font_cache: dict = {}


def script_fonts(scripts, dirs=None) -> list[str]:
    """Installed font files whose *name* mentions one of these scripts."""
    dirs = tuple(dirs or system_font_dirs())
    key = (tuple(scripts), dirs)
    if key in _font_cache:
        return _font_cache[key]
    want = []
    for sc in scripts:
        want += [sc] + list(SCRIPT_FONT_HINTS.get(sc, ()))
    want = {_norm(w) for w in want}
    found: list[str] = []
    for d in dirs:
        for root, _sub, files in os.walk(d):
            for name in sorted(files):
                if not name.lower().endswith(FONT_EXTS):
                    continue
                stem = _norm(name)
                if any(w in stem for w in want):
                    found.append(os.path.join(root, name))
    # Prefer the free, well-maintained Unicode fonts when several faces match:
    # Noto and the vendor fallbacks both cover the script, but Noto's shaping
    # tables are the ones that have been tested against libass.
    def rank(path):
        stem = _norm(os.path.basename(path))
        pref = ("noto", "freesans", "sarala", "samyak", "harmony")
        return (0 if any(k in stem for k in pref) else 1, stem, path)
    found.sort(key=rank)
    _font_cache[key] = found
    return found


def _tokens(text: str) -> frozenset:
    return frozenset(w for w in re.split(r"[^a-z0-9]+", text.lower()) if w)


_family_cache: dict = {}


def family_of(path: str) -> str | None:
    """The family name inside a font file, e.g. "Iskoola Pota"."""
    if path in _family_cache:
        return _family_cache[path]
    try:
        from PIL import ImageFont
        fam = ImageFont.truetype(path, 12).getname()[0] or None
    except Exception:
        fam = None
    _family_cache[path] = fam
    return fam


def font_by_name(spec: str, dirs=None) -> str | None:
    """Resolve a font *family or file name* to a file on this machine.

    Windows users say "Nirmala UI", not "C:\\Windows\\Fonts\\Nirmala.ttf", and
    the real Sinhala face is called "Iskoola Pota" while its file is
    iskpota.ttf -- so file names are tried first (cheap), then the family name
    read out of each font.  Matching is by token set, so "Nirmala-UI",
    "nirmala ui" and "Nirmala.ttf" all hit Nirmala.ttf.
    """
    want = _tokens(os.path.basename(spec))
    if not want:
        return None
    fonts = []
    for d in (dirs or system_font_dirs()):
        for root, _sub, files in os.walk(d):
            fonts += [os.path.join(root, n) for n in sorted(files)
                      if n.lower().endswith(FONT_EXTS)]
    partial = None
    for path in fonts:
        tok = _tokens(os.path.splitext(os.path.basename(path))[0])
        if tok == want:
            return path                                   # name says it exactly
        if tok and (tok <= want or want <= tok):
            partial = partial or path
    for path in fonts:                                   # ask the font itself
        fam = _tokens(family_of(path) or "")
        if fam and (fam == want or fam <= want or want <= fam):
            return path
    return partial


def suggest_font(text: str, dirs=None) -> tuple[str | None, str | None]:
    """-> (font path or None, script it was matched for).

    Only consulted when the user did not pass --font: a Latin-only default face
    renders Sinhala as empty boxes, and on Windows the face that covers it is
    not the one a Linux machine would pick.
    """
    from .srt import scripts_in
    for sc in scripts_in(text):
        hits = script_fonts([sc], dirs)
        if hits:
            return hits[0], sc
    return None, None


@dataclass
class RenderStyle:
    font_path: str | None = None
    font_size: int = 30
    margin_x: int = 40
    margin_bottom: int = 36
    max_lines: int = 2
    line_spacing: float = 1.12
    outline: int = 2
    shadow: int = 0
    bold: bool = True
    uppercase: bool = False


def find_font(style: RenderStyle) -> str:
    """A face to draw with when the caller did not name one."""
    if style.font_path:
        for cand in (style.font_path, os.path.normpath(style.font_path)):
            if os.path.exists(cand):
                return cand
        # a family name instead of a path is how Windows describes its fonts
        hit = font_by_name(style.font_path)
        if hit:
            return hit
        raise FileNotFoundError(
            f"font not found: {style.font_path} -- pass a .ttf/.otf path, or a "
            f"family name that exists in {', '.join(system_font_dirs())}")

    env = os.environ.get("SRT2IDX_FONT")
    if env and os.path.exists(env):
        return env

    fontdirs = system_font_dirs()
    for cand in DEFAULT_FONT_CANDIDATES:
        # bare names like "arialbd.ttf" are Windows font-dir residents
        for base in (os.path.curdir, *fontdirs):
            full = cand if os.path.isabs(cand) else os.path.join(base, cand)
            if os.path.exists(full):
                return full

    for d in fontdirs:
        found = sorted(p for pat in ("*.ttf", "*.otf")
                       for p in glob.glob(os.path.join(d, "**", pat),
                                          recursive=True))
        if found:
            return found[0]
    raise FileNotFoundError(
        "No TrueType font found. Pass --font /path/to/font.ttf "
        "or set SRT2IDX_FONT."
    )


class Renderer:
    def __init__(self, style: RenderStyle, canvas: tuple[int, int]) -> None:
        self.style = style
        self.canvas_w, self.canvas_h = canvas
        self._size = style.font_size
        self.font = ImageFont.truetype(find_font(style), style.font_size)
        self._advances: dict[str, float] = {}

    def _split_word(self, word: str, max_width: float) -> list[str]:
        """Break a word that is wider than the line into fitting chunks.

        Without this, a long URL or a long agglutinated token is centred at a
        negative x and both ends are silently chopped off the canvas.
        """
        if self.font.getlength(word) <= max_width:
            return [word]
        out: list[str] = []
        cur = ""
        for ch in word:
            if cur and self.font.getlength(cur + ch) > max_width:
                out.append(cur)
                cur = ch
            else:
                cur += ch
        if cur:
            out.append(cur)
        return out

    def _wrap(self, text: str, max_width: float) -> list[str]:
        words: list[str] = []
        for w in text.split():
            words.extend(self._split_word(w, max_width))
        if not words:
            return []
        lines: list[str] = []
        cur = words[0]
        for w in words[1:]:
            trial = cur + " " + w
            if self.font.getlength(trial) <= max_width:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
        return lines

    def set_font(self, size: int) -> None:
        self._size = size
        self.font = ImageFont.truetype(find_font(self.style), size)
        self._advances.clear()

    # -- text layout ---------------------------------------------------
    def wrap(self, text_lines: list[str]) -> tuple[list[str], int]:
        """Return (lines, font_size), shrinking the font until it fits.

        Stateless apart from self.font: callers must not assume the font is
        still whatever it was, so render() re-applies the size it needs.
        """
        max_width = self.canvas_w - 2 * self.style.margin_x
        text = " ".join(text_lines)
        if self.style.uppercase:
            text = text.upper()

        size = self.style.font_size
        while True:
            if size != self._size:
                self.set_font(size)
            out = self._wrap(text, max_width)
            if len(out) <= self.style.max_lines + 1 or size <= MIN_FONT:
                return out, size
            size = max(MIN_FONT, int(size * 0.88))

    def layout(self, text_lines: list[str]) -> list[str]:
        """Wrapped lines at the current font size (kept for tests/previews)."""
        return self.wrap(text_lines)[0]

    # -- rasterisation -------------------------------------------------
    def render(self, text_lines: list[str]) -> tuple[bytes, int, int, int, int, int]:
        """Return (data, width, height, x, y, lines_used).

        ``rows`` is one byte per pixel with DVD colour codes: 0 = transparent,
        1 = body, 2 = outline/shadow.
        """
        lines, size = self.wrap(text_lines)
        if size != self._size:
            self.set_font(size)
        try:
            return self._rasterise(lines)
        finally:
            if self._size != self.style.font_size:
                self.set_font(self.style.font_size)

    def _rasterise(self, lines: list[str]) -> tuple[bytes, int, int, int, int, int]:
        if not lines:
            raise ValueError("empty cue")

        ascent, descent = self.font.getmetrics()
        line_h = int(round((ascent + descent) * self.style.line_spacing))
        pad = self.style.outline + self.style.shadow + 1
        width = int(max(self.font.getlength(ln) for ln in lines)) + 2 * pad
        width = min(width, self.canvas_w)
        height = line_h * len(lines) + 2 * pad

        body = Image.new("L", (width, height), 0)
        draw = ImageDraw.Draw(body)
        for i, ln in enumerate(lines):
            lw = self.font.getlength(ln)
            draw.text(((width - lw) / 2, pad + i * line_h), ln,
                      font=self.font, fill=255)

        outline = body
        if self.style.outline > 0:
            outline = _dilate(body, self.style.outline)
        if self.style.shadow > 0:
            shadow = _offset(_dilate(body, self.style.shadow), self.style.shadow,
                             self.style.shadow)
        else:
            shadow = None

        x = max(0, (self.canvas_w - width) // 2)
        y = max(0, self.canvas_h - self.style.margin_bottom - height)

        top = _first_row(outline)
        bottom = _last_row(outline)
        if top is None:
            raise ValueError("nothing rendered")
        height = bottom - top + 1
        y += top
        outline = outline.crop((0, top, width, bottom + 1))
        body = body.crop((0, top, width, bottom + 1))
        if shadow is not None:
            shadow = shadow.crop((0, top, width, bottom + 1))

        ob = outline.tobytes()
        bb = body.tobytes()
        sb = shadow.tobytes() if shadow is not None else None

        rows = bytearray(width * height)
        for i in range(width * height):
            if bb[i] >= ALPHA_CUTOFF:
                rows[i] = 1
            elif ob[i] >= ALPHA_CUTOFF or (sb is not None and sb[i] >= ALPHA_CUTOFF):
                rows[i] = 2
        return bytes(rows), width, height, x, y, len(lines)


def _dilate(img: Image.Image, radius: int) -> Image.Image:
    """Cheap square dilation using MaxFilter (radius in pixels)."""
    from PIL import ImageFilter
    size = 2 * radius + 1
    return img.filter(ImageFilter.MaxFilter(size))


def _offset(img: Image.Image, dx: int, dy: int) -> Image.Image:
    out = Image.new("L", img.size, 0)
    out.paste(img, (dx, dy))
    return out


def _first_row(img: Image.Image) -> int | None:
    w, h = img.size
    data = img.tobytes()
    for y in range(h):
        if max(data[y * w:(y + 1) * w]) > 0:
            return y
    return None


def _last_row(img: Image.Image) -> int | None:
    w, h = img.size
    data = img.tobytes()
    for y in range(h - 1, -1, -1):
        if max(data[y * w:(y + 1) * w]) > 0:
            return y
    return None


