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
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

# DVD-Video is either NTSC (720x480) or PAL (720x576)
PAL_SIZE = (720, 576)
NTSC_SIZE = (720, 480)

MIN_FONT = 12            # below this a TV subtitle is unreadable anyway
ALPHA_CUTOFF = 128       # FreeType coverage above which a pixel is "on"

DEFAULT_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


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
    if style.font_path:
        if not os.path.exists(style.font_path):
            raise FileNotFoundError(f"font not found: {style.font_path}")
        return style.font_path

    env = os.environ.get("SRT2IDX_FONT")
    if env and os.path.exists(env):
        return env

    for cand in DEFAULT_FONT_CANDIDATES:
        if os.path.exists(cand):
            return cand

    found = sorted(glob.glob("/usr/share/fonts/**/*.ttf", recursive=True))
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


