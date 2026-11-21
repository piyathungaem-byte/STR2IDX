"""The single source of truth for every srt2idx setting.

One table (``FIELDS``) drives three things:

  * the command line flags in :mod:`srt2idx.cli`
  * the JSON schema the web UI renders its form from (``schema()``)
  * the reconstruction of an equivalent command line (``to_args()``)

so a switch added here appears in the CLI, the browser form and the command
preview without any of the three being edited separately.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from typing import Any

GROUPS = {
    "canvas": "Canvas & layout",
    "look": "Text look",
    "colours": "Colours (baked in -- a TV cannot restyle these)",
    "timing": "Timing",
    "script": "Script & font",
    "output": "Language & files",
    "checks": "Checks & output",
}


@dataclass(frozen=True)
class Field:
    key: str
    flag: str                      # CLI flag, "" for none
    group: str
    type: str                      # int float str bool color choice path text
    default: Any
    help: str = ""
    choices: tuple = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    metavar: str | None = None
    ui: str = "auto"               # auto | hidden | file
    job: bool = False              # per-file knob rather than a style knob

    def to_json(self) -> dict:
        return dict(
            key=self.key, flag=self.flag, group=self.group, type=self.type,
            default=_json_default(self), help=self.help, choices=list(self.choices),
            minimum=self.minimum, maximum=self.maximum, step=self.step,
            metavar=self.metavar, ui=self.ui, job=self.job,
        )


def _f(key, flag, group, type_, default, help="", **kw) -> Field:
    return Field(key=key, flag=flag, group=group, type=type_, default=default,
                 help=help, **kw)


FIELDS: tuple[Field, ...] = (
    # ---- canvas & layout
    _f("size", "--size", "canvas", "choice", "pal",
       "DVD canvas. PAL is 720x576, NTSC 720x480. The TV scales it to fit, so "
       "an undersized font goes soft on big screens.", choices=("pal", "ntsc")),
    _f("max_lines", "--max-lines", "canvas", "int", 2,
       "Preferred line count. Longer cues wrap onto more lines, and the font "
       "shrinks before anything is dropped.", minimum=1, maximum=6),
    _f("margin_x", "--margin-x", "canvas", "int", 40,
       "Left/right safe margin in canvas pixels.", minimum=0, maximum=200),
    _f("margin_bottom", "--margin-bottom", "canvas", "int", 36,
       "Distance from the bottom of the frame.", minimum=0, maximum=200),

    # ---- text look
    _f("font_size", "--font-size", "look", "int", 30,
       "Glyph size in canvas pixels.", minimum=12, maximum=64),
    _f("outline", "--outline", "look", "int", 2,
       "Outline thickness. Keep at least 1: white text over a bright scene is "
       "unreadable without it.", minimum=0, maximum=8),
    _f("shadow", "--shadow", "look", "int", 0,
       "Drop shadow offset, drawn in the outline colour.",
       minimum=0, maximum=8),
    _f("bold", "--bold", "look", "bool", False,
       "Ask the font for its bold face (the outline already adds weight)."),
    _f("line_spacing", "--line-spacing", "look", "float", 1.12,
       "Line height multiplier for two-line cues.",
       minimum=0.9, maximum=2.0, step=0.01),
    _f("uppercase", "--uppercase", "look", "bool", False,
       "Force capitals -- the classic DVD look, but it hurts accented text."),

    # ---- colours
    _f("fg", "--fg", "colours", "color", 0xF0F0F0,
       "Text colour, written into the .idx palette."),
    _f("outline_color", "--outline-color", "colours", "color", 0x101010,
       "Outline and shadow colour."),

    # ---- timing
    _f("fps", "--fps", "timing", "float", 25.0,
       "Cue start/end are snapped to this frame grid, which is what players "
       "use to decide whether a subtitle should be on screen.",
       minimum=1, maximum=240, step=0.001),
    _f("delay", "--delay", "timing", "float", 0.0,
       "Shift every cue by this many seconds (negative = earlier).",
       minimum=-600, maximum=600, step=0.05),
    _f("min_duration", "--min-duration", "timing", "float", 0.8,
       "Stretch cues shorter than this so rapid-fire lines do not flash past.",
       minimum=0.2, maximum=10, step=0.05),

    # ---- script & font
    _f("engine", "--engine", "script", "choice", "auto",
       "pil = Pillow (fast, fine for Latin). ass = libass through ffmpeg, which "
       "shapes Sinhala/Tamil/Arabic/Devanagari/Thai correctly. auto picks ass "
       "when the text needs it and ffmpeg exists.",
       choices=("auto", "pil", "ass")),
    _f("font", "--font", "script", "path", None,
       "TTF/OTF face to render with. Missing glyphs are burned into the bitmap "
       "as .notdef boxes, so check a preview.", ui="file"),
    _f("force_style", "--force-style", "script", "text", None,
       "Extra raw libass overrides for the ass engine, e.g. "
       "'Spacing=2,BorderStyle=1,MarginB=10'. Anything valid in an ASS "
       "Style line works.", metavar="K=V,K=V"),

    # ---- language & files
    _f("lang", "--lang", "output", "str", "eng",
       "2-3 letter ASCII id written to the .idx (ISO 639-2): eng, sin, ta."),
    _f("title", "--title", "output", "text", None,
       "'alt:' description some TVs show in their subtitle menu. Defaults to "
       "the file stem."),

    # ---- per-file / job knobs
    _f("srt", "srt", "checks", "path", None, "input SubRip .srt file",
       job=True, ui="file"),
    _f("output", "-o", "checks", "path", None,
       "output .idx path (the .sub is written beside it); default: <input>.idx",
       job=True, ui="hidden"),
    _f("video", "--video", "checks", "path", None,
       "video to mux the subtitle into", job=True, ui="file"),
    _f("mkv", "--mkv", "checks", "bool", False,
       "Mux the result into an .mkv as a real DVD subtitle track. Video and "
       "audio are stream-copied, so this is lossless and fast.", job=True),
    _f("mkv_output", "--mkv-output", "checks", "path", None,
       "where to write the .mkv; default: <video>.vobsub.mkv",
       job=True, ui="hidden"),
    _f("verify", "--verify", "checks", "bool", True,
       "Decode the written pair again and compare it to the bitmap that was "
       "rendered. Catches a broken file before it reaches the TV.", job=True),
    _f("preview", "--preview", "checks", "bool", True,
       "Write PNGs of what a player will draw, decoded back from the files.",
       job=True),
    _f("preview_limit", "--preview-limit", "checks", "int", 8,
       "How many preview PNGs to keep (they are the first cues; the rest are "
       "deleted).", minimum=1, maximum=48, job=True),
    _f("dry_run", "--dry-run", "checks", "bool", False,
       "Count cues and report the size, write nothing.", job=True,
       ui="hidden"),   # the web UI exposes this as the "Estimate" button
    _f("absolute_dates", "--absolute-dates", "checks", "bool", False,
       "Write absolute SPU dates instead of PTS-relative ones. Only needed for "
       "players that ignore the PES timestamps.", job=True),
    _f("verbose", "-v", "checks", "bool", False, "extra chatter",
       job=True, ui="hidden"),
)

BY_KEY = {f.key: f for f in FIELDS}
SETTING_FIELDS = tuple(f for f in FIELDS if not f.job)
JOB_FIELDS = tuple(f for f in FIELDS if f.job)


class UsageError(Exception):
    """A bad value the user can fix. Exit 2 on the CLI, HTTP 400 in the web app."""

    def __init__(self, msg: str, code: int = 2) -> None:
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------- value coercion
def parse_color(value: Any) -> int:
    if isinstance(value, bool):
        raise UsageError(f"{value!r} is not a colour")
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFF:
            raise UsageError(f"colour {value} is outside 000000..FFFFFF")
        return value
    s = str(value).strip().lstrip("#").replace(" ", "")
    if len(s) == 3:                       # accept the #abc shorthand
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise UsageError(f"{value!r} is not RRGGBB")
    try:
        return int(s, 16)
    except ValueError:
        raise UsageError(f"{value!r} is not a hex colour") from None


def coerce(f: Field, value: Any) -> Any:
    if value is None or value == "":
        return None if f.type in ("path", "text", "str") else f.default
    if f.type == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if f.type == "color":
        return parse_color(value)
    if f.type == "choice":
        v = str(value).strip().lower()
        if v not in f.choices:
            raise UsageError(f"{f.key} must be one of "
                             f"{', '.join(f.choices)}, got {value!r}")
        return v
    if f.type == "int":
        try:
            v = int(round(float(value)))
        except (TypeError, ValueError):
            raise UsageError(f"{f.key} must be a whole number, got {value!r}") from None
        return _bounded(f, v)
    if f.type == "float":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise UsageError(f"{f.key} must be a number, got {value!r}") from None
        return _bounded(f, v)
    return str(value)


def _bounded(f: Field, v):
    if f.minimum is not None and v < f.minimum:
        raise UsageError(f"{f.key} must be >= {f.minimum} (got {v})")
    if f.maximum is not None and v > f.maximum:
        raise UsageError(f"{f.key} must be <= {f.maximum} (got {v})")
    return v


# ------------------------------------------------------------------- options
@dataclass
class Options:
    """Every knob, style and job. Built from CLI args or the web form."""
    size: str = "pal"
    max_lines: int = 2
    margin_x: int = 40
    margin_bottom: int = 36
    font_size: int = 30
    outline: int = 2
    shadow: int = 0
    bold: bool = False
    line_spacing: float = 1.12
    uppercase: bool = False
    fg: int = 0xF0F0F0
    outline_color: int = 0x101010
    fps: float = 25.0
    delay: float = 0.0
    min_duration: float = 0.8
    engine: str = "auto"
    font: str | None = None
    force_style: str | None = None
    lang: str = "eng"
    title: str | None = None
    # job-level
    srt: str | None = None
    output: str | None = None
    video: str | None = None
    mkv: bool = False
    mkv_output: str | None = None
    verify: bool = True
    preview: bool = True
    preview_limit: int = 8
    dry_run: bool = False
    absolute_dates: bool = False
    verbose: bool = False

    @classmethod
    def defaults(cls) -> "Options":
        return cls(**{f.key: f.default for f in FIELDS})

    @classmethod
    def from_dict(cls, d: dict, *, partial: bool = False) -> "Options":
        """Build from JSON. Unknown keys are an error: they mean a typo."""
        unknown = set(d) - set(BY_KEY)
        if unknown:
            raise UsageError("unknown setting(s): " + ", ".join(sorted(unknown)))
        base: dict = {} if partial else asdict(cls.defaults())
        for k, v in d.items():
            if v is None and not partial:
                continue
            base[k] = coerce(BY_KEY[k], v)
        opts = cls(**base)
        opts.validate()
        return opts

    def validate(self) -> None:
        lang = (self.lang or "").lower().strip()
        if not (2 <= len(lang) <= 3 and lang.isascii() and lang.isalnum()):
            raise UsageError("--lang must be a 2-3 letter ASCII code "
                             f"(ISO 639-2/639-1), got {self.lang!r}: the .idx "
                             "id: field is not free text")
        self.lang = lang
        self.max_lines = max(1, self.max_lines)
        self.outline = max(0, self.outline)
        self.shadow = max(0, self.shadow)
        if self.dry_run:                      # nothing is written, so nothing
            self.verify = self.preview = False    # can be verified
        elif self.verify or self.preview:
            self.dry_run = False
        if self.mkv and not (self.video or self.srt):
            raise UsageError("muxing needs a video, or a .srt whose basename "
                             "matches one")

    def to_dict(self) -> dict:
        return asdict(self)

    def to_args(self, *, only_changes: bool = True) -> list[str]:
        """The equivalent argv, non-default values only.

        The UI shows this string, so the web settings and the CLI can never
        disagree about which one is authoritative.
        """
        defaults = self.defaults().to_dict()
        out: list[str] = []
        for f in FIELDS:
            v = getattr(self, f.key)
            if only_changes and v == defaults[f.key]:
                continue
            if not f.flag.startswith("-"):
                continue
            if f.type == "bool":
                if v:
                    out.append(f.flag)
                elif defaults[f.key]:
                    out.append("--no-" + f.flag[2:])
                continue
            if v is None:
                continue
            if f.type == "color":
                out += [f.flag, f"{int(v):06x}"]
            elif f.type == "int":
                out += [f.flag, str(int(v))]
            elif f.type == "float":
                out += [f.flag, f"{float(v):g}"]
            else:
                out += [f.flag, str(v)]
        if self.srt:
            out.append(str(self.srt))
        return out

    def command_line(self, prog: str = "srt2idx") -> str:
        import shlex
        return f"{prog} " + " ".join(shlex.quote(a) for a in self.to_args())


# --------------------------------------------------------------------- schema
def schema() -> dict:
    """Everything the web UI needs to draw the form, including bounds."""
    groups = []
    for name, title in GROUPS.items():
        flds = [f.to_json() for f in FIELDS if f.group == name and f.ui != "hidden"]
        if flds:
            groups.append(dict(name=name, title=title, fields=flds))
    return dict(version=1, groups=groups,
                defaults={f.key: _json_default(f) for f in FIELDS},
                flags={f.key: f.flag for f in FIELDS})


def _json_default(f: Field) -> Any:
    if f.type == "color" and isinstance(f.default, int):
        return f"{f.default:06x}"
    return f.default


# ------------------------------------------------------------------ argparse
def add_to_parser(p: argparse.ArgumentParser, *, job: bool | None = None) -> None:
    """Attach every field as a flag, in table order, grouped by section.

    Booleans that default to True get an opt-out twin (``--no-verify``), so
    every switch in the web form is reachable from the terminal too.
    """
    buckets: dict[str, Any] = {"": p}
    for name, title in GROUPS.items():
        buckets[name] = p.add_argument_group(title)

    for f in FIELDS:
        if f.key == "srt":
            continue
        if job is not None and f.job != job:
            continue
        grp = buckets.get(f.group, p)
        kw: dict[str, Any] = {"dest": f.key, "help": f.help or argparse.SUPPRESS}

        if f.key == "mkv":            # --mkv [VIDEO]: bare means "guess it"
            grp.add_argument(f.flag, nargs="?", const="AUTO", default=None,
                             metavar="VIDEO",
                             help="also mux into an .mkv; pass a video file, or "
                                  "nothing to use one matching the .srt basename")
            continue
        if f.type == "bool":
            if f.default:
                grp.add_argument(f.flag, action="store_true", default=True, **{
                    k: v for k, v in kw.items() if k != "default"})
                grp.add_argument("--no-" + f.flag[2:], dest=f.key,
                                 action="store_false",
                                 help="disable " + f.flag[2:])
            else:
                kw["action"] = "store_true"
                kw["default"] = False
                grp.add_argument(f.flag, **kw)
            continue
        if f.type == "color":
            kw.update(type=_arg_color, metavar="RRGGBB")
        elif f.type == "int":
            kw.update(type=int, metavar="N")
        elif f.type == "float":
            kw.update(type=float,
                      metavar="SECONDS" if f.key in ("delay", "min_duration")
                      else "N")
        elif f.type == "choice":
            kw.update(choices=f.choices)
        else:
            kw.update(type=str, metavar=f.metavar or
                      ("PATH" if f.type == "path" else "TEXT"))
        grp.add_argument(f.flag, **kw)
        if "-" in f.flag[2:]:
            # tolerate the underscore spelling without duplicating the help text
            grp.add_argument("--" + f.flag[2:].replace("-", "_"),
                             **{**kw, "help": argparse.SUPPRESS})


def _arg_color(s: str) -> int:
    try:
        return parse_color(s)
    except UsageError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def from_args(ns: argparse.Namespace) -> Options:
    d: dict = {}
    for f in FIELDS:
        v = getattr(ns, f.key, None)
        if f.key == "mkv":
            v = bool(v)                     # None | "AUTO" | path -> switch
        if v is not None:
            d[f.key] = v
    opts = Options.from_dict(d)
    mkv = getattr(ns, "mkv", None)
    if isinstance(mkv, str) and mkv != "AUTO":
        opts.video = mkv
    return opts
