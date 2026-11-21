"""Windows/platform behaviour of font discovery and the ffmpeg filter graph.

Every test here encodes a case that is *normal* on Windows and impossible on a
plain Linux box: a temp path that starts with a drive colon, a system font
folder under %WINDIR%, no DejaVu, and a user folder name with an apostrophe.
The filter-graph tests were written after finding that `--engine ass` could not
work at all on Windows (ffmpeg parses `subtitles=filename=C:\\Users\\...` as a
broken option list), which is exactly the engine Sinhala depends on.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from srt2idx import settings
from srt2idx.ass import (AssRenderer, default_family, ffquote, filter_src,
                         font_family)
from srt2idx.pipeline import ffmpeg_exe, resolve_font
from srt2idx.render import (RenderStyle, find_font, script_fonts, suggest_font,
                            system_font_dirs)
from srt2idx.srt import parse, scripts_in

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
HAS_FONT = os.path.exists(DEJAVU)


@pytest.fixture
def fake_windows(tmp_path, monkeypatch):
    """A %WINDIR%\\Fonts that exists, with plausible Windows faces."""
    fonts = tmp_path / "Windows" / "Fonts"
    fonts.mkdir(parents=True)
    names = ["arialbd.ttf", "arial.ttf", "Nirmala.ttf", "iskpota.ttf",
             "taLeel.ttf", "calibri.ttf"]
    for n in names:
        if HAS_FONT:
            shutil.copy(DEJAVU, fonts / n)
        else:                                    # no system font to borrow
            (fonts / n).write_bytes(b"\x00")
    monkeypatch.setenv("WINDIR", str(tmp_path / "Windows"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    import srt2idx.render as R
    monkeypatch.setattr(R, "_font_cache", {})
    return tmp_path


# ------------------------------------------------------------------- ffmpeg
def test_ffquote_normalises_and_escapes_a_windows_path():
    assert ffquote("C:\\Windows\\Fonts") == "'C\\:/Windows/Fonts'"
    assert ffquote("/usr/share/fonts") == "'/usr/share/fonts'"


def test_ffquote_refuses_a_path_it_cannot_express():
    """ffmpeg has no escape for "'" inside '...', so say so instead of
    producing a command that fails to parse."""
    assert ffquote("C:\\Users\\O'Brien\\Fonts") is None


def test_filter_src_passes_only_a_bare_name():
    src = filter_src("driver.srt", RenderStyle(), "'C\\:/Windows/Fonts'")
    assert src.startswith("subtitles=filename=driver.srt")
    assert ":fontsdir='C\\:/Windows/Fonts'" in src
    # the graph must never see an unescaped drive colon: that is what breaks it
    head = src.split("force_style")[0]
    assert "C:" not in head
    assert ffquote("C:\\Windows\\Fonts").strip("'") in head


@pytest.mark.skipif(not ffmpeg_exe(), reason="ffmpeg not available")
@pytest.mark.parametrize("tmpname", ["win", "apos"])
def test_ass_engine_survives_a_hostile_temp_dir(tmp_path, tmpname):
    """The regression that made `--engine ass` dead on arrival on Windows.

    TMPDIR=/tmp/C:/... is what every Windows temp path looks like, and
    /tmp/u'ser x: is what a Windows temp path looks like for a user whose name
    contains an apostrophe.  A bare absolute path in -vf breaks ffmpeg's
    filter-graph parser in the first case; nothing in the second can be quoted
    at all, which is why the driver is passed relative to the child's cwd.
    """
    name = {"win": "C:/Users/me/AppData/Local/Temp",
            "apos": "u'ser x: \\tmp"}[tmpname]
    hostile = tmp_path / name.replace("\\", "/")
    hostile.mkdir(parents=True)
    srt = tmp_path / "movie.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:04,000\nHello TV\n\n",
                   encoding="utf-8")
    old, tempfile.tempdir = tempfile.tempdir, str(hostile)
    try:
        from srt2idx import pipeline
        opts = settings.Options.from_dict(
            {"srt": str(srt), "output": str(tmp_path / "o.idx"),
             "engine": "ass", "verify": True, "preview": False})
        rep = pipeline.run(opts)
        assert rep.engine == "ass"
        assert rep.encoded == 1
        assert rep.verify_problems == []
    finally:
        tempfile.tempdir = old


@pytest.mark.skipif(not (ffmpeg_exe() and HAS_FONT), reason="needs ffmpeg+font")
def test_unquotable_fontsdir_falls_back_to_a_copied_face(tmp_path):
    """No quoting works -> put the face next to the driver and pass '.'."""
    odd = tmp_path / "u'ser" / "Fonts"
    odd.mkdir(parents=True)
    shutil.copy(DEJAVU, odd / "MyFont.ttf")
    style = RenderStyle(font_path=str(odd / "MyFont.ttf"), font_size=24)
    ar = AssRenderer(style, (320, 240), ffmpeg_exe(), fontsdir=str(odd))
    tmp = tempfile.mkdtemp()
    assert ar._fontsdir_arg(tmp) == "'.'"
    assert os.path.exists(os.path.join(tmp, "MyFont.ttf"))
    assert any("safe temp dir" in n for n in ar.notes)
    # and the render itself still works
    out = ar.render_all(parse("1\n00:00:00,000 --> 00:00:00,04\nHello\n\n"))
    assert out[0] and out[0]["w"] > 0


# -------------------------------------------------------------------- fonts
def test_font_dirs_follow_windir_and_skip_missing(fake_windows):
    dirs = system_font_dirs()
    assert dirs[0] == os.path.join(str(fake_windows / "Windows"), "Fonts")
    assert "C:/Windows/Fonts" not in dirs        # does not exist here -> out
    assert all(os.path.isdir(d) for d in dirs)


def test_find_font_resolves_a_bare_windows_name(fake_windows, monkeypatch):
    """DEFAULT_FONT_CANDIDATES lists "arialbd.ttf" with no directory: on
    Windows that name must be found under %WINDIR%\\Fonts, not in /usr/share."""
    import srt2idx.render as R
    monkeypatch.setattr(R, "DEFAULT_FONT_CANDIDATES", ["arialbd.ttf", "arial.ttf"])
    got = R.find_font(RenderStyle())
    assert got == os.path.join(str(fake_windows / "Windows" / "Fonts"),
                               "arialbd.ttf"), got
    assert os.path.exists(got)


def test_find_font_still_prefers_an_absolute_candidate(fake_windows, monkeypatch):
    """The bare names are a fallback, not a replacement: a Linux box keeps its
    DejaVu pick, so nothing on this side of the port changes."""
    import srt2idx.render as R
    monkeypatch.setattr(R, "DEFAULT_FONT_CANDIDATES", [DEJAVU])
    assert R.find_font(RenderStyle()) == DEJAVU


def test_find_font_reports_a_missing_user_font(tmp_path):
    with pytest.raises(FileNotFoundError):
        find_font(RenderStyle(font_path=str(tmp_path / "nope.ttf")))


def test_font_path_accepts_windows_separators(fake_windows):
    p = os.path.join(str(fake_windows / "Windows" / "Fonts"), "arial.ttf")
    assert find_font(RenderStyle(font_path=p)) == p


def test_scripts_are_named():
    assert scripts_in("සුළු මොහොතක් Hello TV") == ["Sinhala"]
    assert scripts_in("வணக்கம்") == ["Tamil"]
    assert scripts_in("你好") == []               # CJK needs no shaping
    assert scripts_in("plain") == []


def test_font_is_picked_by_script_not_by_default_name(fake_windows):
    """On Windows the Sinhala face is iskpota/Nirmala; 'DejaVu Sans' does not
    exist, and asking for it renders an empty box."""
    path, script = suggest_font("සුළු මොහොතක්")
    assert script == "Sinhala"
    assert path and os.path.basename(path) in ("Nirmala.ttf", "iskpota.ttf")
    assert suggest_font("Hello TV") == (None, None)


def test_noto_is_preferred_over_older_faces(tmp_path):
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    for n in ("lklug.ttf", "NotoSansSinhala-Regular.ttf", "iskpota.ttf"):
        (fonts / n).write_bytes(b"\x00")
    got = script_fonts(["Sinhala"], [str(fonts)])
    assert os.path.basename(got[0]) == "NotoSansSinhala-Regular.ttf"
    assert len(got) == 3                          # all three match, ordered


@pytest.mark.parametrize("plat,fam", [("win32", "Arial"), ("darwin", "Helvetica"),
                                      ("linux", "DejaVu Sans")])
def test_default_family_is_per_platform(monkeypatch, plat, fam):
    monkeypatch.setattr(sys, "platform", plat)
    assert default_family() == fam
    assert font_family(None) == fam


def test_resolve_font_prefers_the_user_then_the_system(fake_windows, tmp_path):
    # explicit --font wins and its directory becomes the fontsdir
    mine = tmp_path / "mine.ttf"
    shutil.copy(DEJAVU, mine) if HAS_FONT else mine.write_bytes(b"\x00")
    opts = settings.Options.from_dict({"srt": str(tmp_path / "a.srt"),
                                       "font": str(mine)})
    font, fontsdir = resolve_font(opts, "සුළු මොහොතක්")
    assert font == str(mine)
    assert fontsdir == str(tmp_path)

    # no --font: take the face that covers the script, and say so
    opts = settings.Options.from_dict({"srt": str(tmp_path / "a.srt")})
    notes: list[str] = []
    font, fontsdir = resolve_font(opts, "සුළු මොහොතක්", notes)
    assert font and os.path.basename(font) in ("Nirmala.ttf", "iskpota.ttf")
    assert fontsdir and fontsdir.endswith(os.path.join("Windows", "Fonts"))
    assert any("picked for Sinhala" in n for n in notes)

    # Latin text: nothing to auto-pick, but libass still gets a directory
    font, fontsdir = resolve_font(opts, "Hello TV", [])
    assert font is None and fontsdir


def test_pillow_engine_uses_the_suggested_face(fake_windows, monkeypatch):
    """The auto-pick must reach the renderer, not just the notes."""
    import srt2idx.render as R
    seen = {}
    real = R.Renderer.__init__

    def spy(self, style, canvas):
        seen["font"] = style.font_path
        real(self, style, canvas)
    monkeypatch.setattr(R.Renderer, "__init__", spy)
    from srt2idx.pipeline import build_items
    cues = parse("1\n00:00:01,000 --> 00:00:04,000\nසුළු මොහොතක්\n\n")
    opts = settings.Options.from_dict({"srt": "x.srt"})
    canvas, style, renderer, items, *_ = build_items(
        opts, cues, "pil", None, None, [])
    assert seen["font"] and os.path.basename(seen["font"]) in (
        "Nirmala.ttf", "iskpota.ttf")


# ------------------------------------------------- files written on Windows
def test_a_cp1252_subtitle_file_is_read_as_cp1252(tmp_path):
    """Old .srt files are ANSI, not UTF-8; strict+replace would print boxes."""
    from srt2idx.srt import decode, parse
    text = "1\n00:00:01,000 --> 00:00:02,000\nBjörk Ísland\n\n"
    got, enc = decode(text.encode("cp1252"))
    assert enc == "cp1252" and parse(got)[0].lines == ["Björk Ísland"]
    assert decode(text.encode("utf-8"))[1] is None          # no note needed
    assert decode(b"\xef\xbb\xbf" + text.encode("utf-8"))[1] is None  # BOM ok


def test_cli_reports_the_encoding_it_fell_back_to(tmp_path):
    srt = tmp_path / "ansi.srt"
    srt.write_bytes("1\n00:00:01,000 --> 00:00:04,000\nBjörk\n\n"
                    .encode("cp1252"))
    r = subprocess.run([sys.executable, "-m", "srt2idx", str(srt), "-o",
                        str(tmp_path / "o.idx"), "--no-verify", "--no-preview"],
                       capture_output=True, text=True, cwd=os.path.dirname(
                           os.path.dirname(os.path.abspath(__file__))))
    assert r.returncode == 0, r.stderr[-300:]
    assert "cp1252" in r.stdout, r.stdout[-300:]


def test_a_family_name_resolves_to_a_file(fake_windows, monkeypatch):
    """Windows users know "Nirmala UI", not C:\\Windows\\Fonts\\Nirmala.ttf."""
    import srt2idx.render as R
    for spec in ("Nirmala UI", "nirmala-ui", "Nirmala", "Nirmala.ttf"):
        got = R.font_by_name(spec)
        assert got and got.endswith("Nirmala.ttf"), (spec, got)
    assert R.font_by_name("Comic Sans") is None
    # find_font accepts it too, rather than erroring on "no such file"
    assert R.find_font(R.RenderStyle(font_path="Nirmala UI")).endswith(
        "Nirmala.ttf")
    with pytest.raises(FileNotFoundError, match="family name that exists"):
        R.find_font(R.RenderStyle(font_path="definitely-not-a-font"))


def test_resolve_font_turns_a_name_into_a_path(fake_windows, tmp_path):
    opts = settings.Options.from_dict({"srt": str(tmp_path / "a.srt"),
                                       "font": "Nirmala UI"})
    notes: list[str] = []
    font, fontsdir = resolve_font(opts, "Hello", notes)
    assert font and font.endswith("Nirmala.ttf")
    assert fontsdir.endswith(os.path.join("Windows", "Fonts"))
    assert any("resolved to" in n for n in notes)


def test_family_name_lookup_reads_the_font(fake_windows, monkeypatch):
    """"Iskoola Pota" is the family; the file is iskpota.ttf.  Only reading the
    font's own name table connects them, and that lookup must stay a fallback
    (it opens every file), not the first thing tried."""
    import srt2idx.render as R
    calls = []

    def fake_family(path):
        calls.append(os.path.basename(path))
        return "Iskoola Pota" if path.endswith("iskpota.ttf") else "Nirmala UI" \
            if path.endswith("Nirmala.ttf") else "Whatever"
    monkeypatch.setattr(R, "family_of", fake_family)
    got = R.font_by_name("Iskoola Pota")
    assert got and got.endswith("iskpota.ttf"), got
    assert calls                                    # the name table was read
    monkeypatch.setattr(R, "family_of", lambda p: None)
    assert R.font_by_name("Nothing Like It") is None
