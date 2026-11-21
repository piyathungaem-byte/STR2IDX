"""Minimal, forgiving SubRip (.srt) parser."""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

_TS = re.compile(
    r"(\d+)\s*:\s*(\d{1,2})\s*:\s*(\d{1,2})\s*[,.]\s*(\d{1,3})"
    r"\s*-->\s*"
    r"(\d+)\s*:\s*(\d{1,2})\s*:\s*(\d{1,2})\s*[,.]\s*(\d{1,3})"
)

# tags that carry no text and can simply be dropped before rendering
_TAG = re.compile(r"</?[a-zA-Z][^>]*>|\{\\[^}]*\}")


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    lines: list[str] = field(default_factory=list)

    @property
    def duration(self) -> int:
        return self.end_ms - self.start_ms


def _ms(h: str, m: str, s: str, frac: str) -> int:
    f = (frac + "00")[:3]
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(f)


#: Encodings tried for a subtitle file that is not UTF-8.  Windows-1252 is the
#: single most common one in the wild for older .srt files (it is the default
#: ANSI codepage for Western European Windows), and latin-1 accepts any byte so
#: it never fails -- better a wrong accent than U+FFFD everywhere.
FALLBACK_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


def decode(raw: bytes) -> tuple[str, str | None]:
    """-> (text, encoding) where encoding is None when it was plain UTF-8."""
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        pass
    for enc in FALLBACK_ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace"), "utf-8 (invalid bytes replaced)"


def clean_text(line: str) -> str:
    """Strip markup, decode entities, normalise spaces."""
    line = _TAG.sub("", line)
    line = html.unescape(line)
    line = line.replace("\u00a0", " ").replace("\u200b", "")
    return " ".join(line.split())


def parse(text: str) -> list[Cue]:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text)

    for block in blocks:
        lines = [ln for ln in block.split("\n")]
        idx = None
        for i, ln in enumerate(lines):
            if _TS.search(ln):
                idx = i
                break
        if idx is None:
            continue

        m = _TS.search(lines[idx])
        start = _ms(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _ms(m.group(5), m.group(6), m.group(7), m.group(8))
        if end <= start:
            continue

        body = []
        for ln in lines[idx + 1:]:
            txt = clean_text(ln)
            if txt:
                body.append(txt)
        if not body:
            continue

        # merge overlapping/duplicate cues so nothing is dropped
        if cues and start < cues[-1].end_ms:
            cues[-1].end_ms = max(cues[-1].end_ms, end)
            cues[-1].lines.extend(l for l in body if l not in cues[-1].lines)
        else:
            cues.append(Cue(start, end, body))

    cues.sort(key=lambda c: c.start_ms)
    return cues


# Scripts whose rendering needs shaping (reordering, joining, vowel-sign
# placement) that a plain glyph-placing rasteriser gets wrong.
_COMPLEX_BLOCKS = (
    (0x0590, 0x08FF),   # Hebrew, Arabic, Syriac, Thaana
    (0x0900, 0x0DFF),   # Devanagari, Bengali, Gurmukhi, Gujarati, Oriya,
                        # Tamil, Telugu, Kannada, Malayalam, Sinhala
    (0x0E00, 0x10FF),   # Thai, Lao, Tibetan, Myanmar, Georgian, Armenian
    (0x1780, 0x17FF),   # Khmer
    (0x1800, 0x18AF),   # Mongolian (vertical, needs rotation)
    (0x1A00, 0x1BFF),   # Tai Tham, Balinese, Sundanese
    (0xA980, 0xA9DF),   # Javanese
    (0xFB1E, 0xFEFF),   # presentation forms
)


# Named ranges, so a font can be picked by name.  _COMPLEX_BLOCKS above is the
# "must shape" test; this is the "which face has these glyphs" test.
_SCRIPTS = (
    ("Sinhala", 0x0D80, 0x0DFF),
    ("Tamil", 0x0B80, 0x0BFF),
    ("Telugu", 0x0C00, 0x0C7F),
    ("Kannada", 0x0C80, 0x0CFF),
    ("Malayalam", 0x0D00, 0x0D7F),
    ("Devanagari", 0x0900, 0x097F),
    ("Bengali", 0x0980, 0x09FF),
    ("Gurmukhi", 0x0A00, 0x0A7F),
    ("Gujarati", 0x0A80, 0x0AFF),
    ("Oriya", 0x0B00, 0x0B7F),
    ("Thai", 0x0E00, 0x0E7F),
    ("Lao", 0x0E80, 0x0EFF),
    ("Myanmar", 0x1000, 0x109F),
    ("Khmer", 0x1780, 0x17FF),
    ("Tibetan", 0x0F00, 0x0FFF),
    ("Georgian", 0x10A0, 0x10FF),
    ("Armenian", 0x0530, 0x058F),
    ("Hebrew", 0x0590, 0x05FF),
    ("Arabic", 0x0600, 0x06FF),
)


def scripts_in(text: str) -> list[str]:
    """Script names present in *text*, first-seen order, no repeats."""
    seen = []
    for ch in text:
        if ch.isspace() or ord(ch) < 0x80:
            continue
        for name, lo, hi in _SCRIPTS:
            if lo <= ord(ch) <= hi and name not in seen:
                seen.append(name)
    return seen


def needs_shaping(text: str) -> bool:
    """True when a plain per-codepoint renderer would mis-draw this text.

    Latin (including accents and smart quotes) and CJK are fine -- those are
    either side-by-side glyphs or context-free.  Everything in an indic/RTL
    block is not: vowel signs move around the consonant and letters join.
    """
    for ch in text:
        o = ord(ch)
        if o < 0x80 or o <= 0x24F or 0x2000 <= o <= 0x20CF:
            continue
        if unicodedata.combining(ch):
            return True
        if any(a <= o <= b for a, b in _COMPLEX_BLOCKS):
            return True
    return False
