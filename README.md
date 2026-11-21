# srt2idx

> Tool in the STR2IDX repo. `.srt` in, VobSub out.

Convert `.srt` subtitles into **VobSub** (`.idx` + `.sub`), the bitmap subtitle
format older and budget TVs actually accept — so you don't have to hunt for a
pre-made `.idx` that matches your file.

```
srt2idx movie.srt            # -> movie.idx + movie.sub
srt2idx movie.srt --mkv      # -> movie.mkv with the subtitle muxed in
```

## Is this feasible? Yes — and this repo is the proof

The short version of why it isn't a plain "conversion":

| | what it is |
|---|---|
| `.srt` | plain text + timestamps. Self-contained, restylable, size-independent. |
| `.idx` | a **text index**: timestamps, byte offsets, palette, canvas size. |
| `.sub` | the **images** the `.idx` points at (MPEG-PS stream of DVD subpictures). |

So `.idx` alone is never enough — it is the table of contents for `.sub`. Going
from SRT to VobSub means *rendering your text into pictures* and writing the
DVD subpicture bitstream yourself, then building the index on top. That is why
`ffmpeg -i in.srt -c:s dvdsub` refuses:

```
Subtitle encoding currently only possible from text to text or bitmap to bitmap
```

FFmpeg has a DVD-subtitle *encoder* but nothing feeds text into it. Writing
that missing step is the whole job, and it is what this tool does:

1. **parse** the SRT (tolerant of BOM, CRLF, `<i>`/`<b>`/`<br>` tags, ASS
   override blocks, missing counters, overlapping cues);
2. **render** each cue to a bitmap — either Pillow directly, or **libass**
   through ffmpeg when the script needs shaping (Sinhala, Tamil, Arabic,
   Devanagari, Thai — anywhere vowel signs move around a consonant);
3. **quantise** to the 4 colour codes a DVD subpicture allows
   (transparent / body / outline / spare) and map them to a 16-entry palette;
4. **encode** the DVD RLE bitstream + control commands (`0x03` colour map,
   `0x04` alpha, `0x05` rectangle, `0x06` data offsets, `0x01`/`0x02`
   start/stop dates);
5. **wrap** each subpicture in an MPEG-2 PES packet (`00 00 01 BD`, PTS,
   substream id `0x20`) to make the `.sub`, and record every packet's byte
   offset plus timestamp in the `.idx`.

### What "verified" means here

`tests/` asserts the round trip (`pytest`), and with ffmpeg installed it adds
the conformance checks that matter:

* `ffmpeg -i out.idx` identifies the stream as
  `Subtitle: dvd_subtitle, 720x576` with the language and title from the `.idx`;
* forcing ffmpeg to **decode** every packet (`-c:s dvdsub`) reports
  `1500 packets read; 1500 frames decoded; 0 decode errors` on a
  1500-cue movie-length file;
* the bitmap ffmpeg's decoder recovers is **pixel-identical** to the bitmap we
  rendered, so a player reading our file sees exactly what we drew;
* muxing into `.mkv` with `-c copy` leaves the video bitstream byte-identical.

A 1500-cue PAL file converts in ~10 s (Pillow) / ~30 s (libass) and produces a
~3–20 MB `.sub`.

## Install

Run the install from **the folder that contains `pyproject.toml`** (the repo
root — *not* the `srt2idx/` subfolder inside it). Then everything below is run
from that same folder.

```bash
# Linux / macOS
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                # needs Pillow
pip install -e '.[ass]'         # + imageio-ffmpeg: libass rendering and .mkv muxing
```

```bat
:: Windows (cmd.exe).  Use "py -3" or "python" -- there is no "python3".
py -3 -m venv .venv && .venv\Scripts\activate
pip install -e .
pip install -e ".[ass]"
```

```powershell
# Windows (PowerShell).  The quotes matter: [ass] is a glob otherwise.
py -3 -m venv .venv ; .venv\Scripts\Activate.ps1
pip install -e ".[ass]"
```

Notes for Windows:

* activate is `.venv\Scripts\activate` — there is no `.venv/bin/`.
* `python` and `py -3` exist; `python3` usually does not.
* `pip install -e "<something>[extra]"` **needs the quotes** in PowerShell, or
  the extra is eaten as a wildcard.
* A venv is per-project and cannot be copied between machines; if you made one
  elsewhere, make a new one here.
* Sinhala, Tamil and Thai render through libass, which needs ffmpeg — install
  `".[ass]"` (or put an `ffmpeg.exe` on `PATH`). Pillow alone cannot shape
  those scripts, and the wheel here is built without Raqm.

`ffmpeg` on `PATH` is used if present; otherwise the `imageio-ffmpeg` wheel's
binary is used. Without any ffmpeg you still get `.idx`/`.sub` output — you
just lose `--mkv` and `--engine ass`.

You do **not** have to install anything to run the web UI: `python webapp.py`
works from a plain checkout, because it puts its own folder on `sys.path`.
`pip install -e .` is only needed for the `srt2idx` command.

### If the install complains

| message | cause |
|---|---|
| `'.[ass]' is not a valid editable requirement` / `does not appear to be a Python project` | you are in `srt2idx/` instead of the repo root — `cd ..`, or `pip install -e "..[ass]"` |
| `can't open file '...\.venv\bin\activate'` | Windows uses `.venv\Scripts\activate` |
| `'python3' is not recognized` | use `python` or `py -3` |
| `pip is not recognized` after activating | activate again; the prompt must show `(.venv)` |
| `engine 'ass' needs ffmpeg on PATH` | install `".[ass]"`, or add an ffmpeg build to `PATH` |
| subtitles render as empty boxes | the chosen font lacks those glyphs — see *Fonts* below |

### Fonts

A font is picked automatically, in this order: `--font`, then `$SRT2IDX_FONT`,
then an installed face whose name matches the script of your file (`Nirmala`,
`iskpota` for Sinhala on Windows; `Noto*` anywhere), then the platform default.
Windows has no DejaVu, which is why the search is by name rather than a fixed
path. The chosen face is reported in the conversion notes, and the font
directories scanned are `%WINDIR%\Fonts`, `%LOCALAPPDATA%\Microsoft\Windows\Fonts`,
`/usr/share/fonts`, `/usr/local/share/fonts`, `/Library/Fonts`,
`/System/Library/Fonts` and `~/.fonts`.

If your script is not covered by any installed face, name one you have or drop
a file in:

* `--font "Nirmala UI"` — a *family name* is accepted and looked up for you
  (file names first, then each font's own name table, so "Iskoola Pota" finds
  `iskpota.ttf`);
* `--font "C:\Users\you\Downloads\NotoSansSinhala.ttf"` — a path, any font
  file, wherever it lives;
* or upload the `.ttf` in the web UI's Fonts card and pick it from the list.

A face without the right glyphs burns empty boxes into the bitmap — the file
still validates perfectly and looks fine to every checker, so **look at a
preview** before trusting a 2-hour film.


### Sinhala, Thai, Arabic: when the glyphs look right but read wrong

If the marks sit *beside* the consonant instead of above or before it — a small
detached circle where `ේ` should be — the text was drawn **one glyph per code
point**, with no shaper in the loop. The glyphs are fine; only the placement is
wrong, which is why it looks "almost right" and why a font swap appears to do
nothing. Two causes, opposite fixes:

| cause | what to do |
|---|---|
| the **Pillow** engine was used (no ffmpeg, or `--engine pil`) | use `--engine ass`. Pillow's FreeType has no HarfBuzz/Raqm, so *no font* will ever shape correctly with it. |
| the **font** has no `GSUB`/`GPOS` tables | libass is shaping, but the face carries no rules. **Iskoola Pota is this case** — a pre-OpenType Sinhala font, correct only under Windows GDI/Uniscribe. Use `Nirmala UI`, or Noto Sans Sinhala. Clearing the font box is usually enough: the picker then skips faces without layout tables. |

`srt2idx` refuses to write such a file rather than let you discover it on the TV,
because every byte-level check passes on mis-ordered text:

```
error: this text needs text shaping, but it would not be shaped: IskoolaPota.ttf
has no OpenType layout tables (GSUB/GPOS), so libass has nothing to shape with.
clear the font setting to use Nirmala.ttf; --allow-unshaped writes the file anyway
```

Check what you have before converting a film — this lists every installed face,
whether it can shape, and which scripts its name suggests:

```bash
srt2idx --list-fonts          # or: python -m srt2idx --list-fonts
```

```
  shape  script    family              path
  yes    -         Arial               C:\Windows\Fonts\arial.ttf
  yes    Sinhala   Nirmala UI          C:\Windows\Fonts\Nirmala.ttf
  NO     Sinhala   Iskoola Pota        C:\Windows\Fonts\iskpota.ttf
```

That `NO` row is the trap: the same script in the name, and nothing to say it
is unusable unless something checks. The web UI's font list marks it the same
way.

The conversion report prints `shaping: libass + HarfBuzz with <font>`, the web UI
has a green/red **shaping** row and says it in the live preview's warning, and
`--allow-unshaped` overrides everything if you need a file today and accept the
placement.

## Usage

```bash
# sidecar pair next to the video (what most TVs want)
srt2idx movie.srt -o movie.idx

# pick the look (baked in: a TV cannot restyle a bitmap)
srt2idx movie.srt --font-size 34 --outline 3 --fg FFFED2 --outline-color 000000

# NTSC canvas, 4 extra frames of delay, forced caps
srt2idx movie.srt --size ntsc --delay 0.5 --uppercase

# Sinhala / Tamil / Arabic: let libass shape the text
srt2idx movie.si.srt --font ~/.fonts/NotoSansSinhala.ttf --lang sin

# mux straight into a playable .mkv (video/audio are stream-copied)
srt2idx movie.srt --mkv movie.mkv

# see exactly what the TV will draw, as PNGs, and self-check the bitstream
srt2idx movie.srt --verify --preview
```

Useful flags: `--fps` (snap to frames, default 25), `--min-duration` (rescue
flickering sub-second cues), `--max-lines`, `--margin-bottom`, `--shadow`,
`--size pal|ntsc`, `--lang`, `--title`, `--force-style` (raw libass overrides),
`--engine pil|ass|auto`, `--allow-unshaped`.

## Getting it onto the TV

1. **Sidecar first.** Rename so the trio shares a basename
   (`movie.mkv`, `movie.idx`, `movie.sub`), keep all three together, and select
   the subtitle in the TV's menu. Most TVs that claim "DVD subtitle"/"VobSub"
   support want exactly this.
2. **If it isn't listed, use `--mkv`.** Some sets only look *inside* the
   container. The muxed track is a real DVD subtitle stream, so it behaves like
   a ripped `.idx/.sub` pair.
3. **Filenames matter** on cheap firmware: avoid spaces, keep `.idx`/`.sub`
   lowercase-and-uppercase consistent with what the TV already handles.
4. **USB, not NAS.** Several TVs only index sidecar subtitles from USB storage.

## Browser interface

Everything above is also a web UI — same options, plus drag-and-drop, a live
render of your settings, and downloads.

```bash
python3 webapp.py                      # http://127.0.0.1:8420
python3 webapp.py --port 8080 --host 0.0.0.0 --max-upload-mb 8192
```

```bat
:: Windows: "python", not "python3"
python webapp.py
```

What it does:

* **the whole settings table as controls** — canvas, margins, font, size,
  outline, shadow, bold, line spacing, capitals, both colours, fps, delay,
  minimum duration, engine, `force_style`, language, title, mux, self-check,
  previews. The form is *generated* from `srt2idx/settings.py:FIELDS`, the same
  table that builds the CLI flags, so it cannot drift behind the terminal.
* **a live command readout** — the panel under the form shows the exact
  `srt2idx ...` line your settings correspond to. It is produced by the same
  serialiser, so what you see is what the CLI would run.
* **Live look** — type one cue, and it is rendered through the chosen engine
  and quantised the way a real conversion is, so font choice, outline and
  colours are visible before you commit to a 2-hour film.
* **batch** — drop several `.srt` files, click rows to include or skip them;
  one job converts each.
* **uploads stay on disk, you keep the files** — `.idx`, `.sub`, the muxed
  `.mkv`, the previews, or a `.zip` of all of it. Nothing is sent anywhere.
* **rename/delete/rename** for sidecar matching, and a video probe that reads
  the frame size and suggests `pal` or `ntsc` from it.
* **self-check by default** — every job decodes its own output and compares
  pixels; a red result means the file would have failed on the TV.

Routes are a small JSON API if you would rather script it than click it:
`GET /api/config` (the schema), `POST /api/upload?name=&kind=`,
`POST /api/jobs`, `GET /api/jobs/<id>`, `GET /api/jobs/<id>/file?name=`,
`/preview`, `/text`, `/bundle`, `POST /api/sample`, `POST /api/probe`.

Local tool by design: writes only inside `--workdir` (default `./webdata`),
accepts only bare filenames from there, binds to `127.0.0.1` unless you pass
`--host 0.0.0.0`.

## Limitations (the honest list)

* **No restyling.** Size, colour and font are decided at conversion time. There
  is no "make the text bigger" on the TV — re-run with different flags instead.
* **Fixed canvas:** 720×576 (PAL) or 720×480 (NTSC). The bitmap is scaled to
  your screen by the TV, so an undersized font looks soft on a big panel.
* **One rectangle per cue** — the DVD subpicture format allows a single box, so
  karaoke-style multi-region subs collapse into one.
* **64 KiB per cue.** Subpictures are addressed with 16-bit offsets; a cue that
  still does not fit after font shrinking is reported and dropped rather than
  silently truncated.
* **One `.idx` holds one `id:`/`index:` stream per run.** Two languages means
  two conversions; the container merge is one command (verified here):

  ```bash
  srt2idx movie.en.srt -o movie.en.idx --lang eng
  srt2idx movie.si.srt -o movie.si.idx --lang sin --font NotoSansSinhala.ttf
  ffmpeg -i movie.mkv -i movie.en.idx -i movie.si.idx \
         -map 0 -map 1:s -map 2:s -c copy \
         -metadata:s:s:0 language=eng -metadata:s:s:1 language=sin out.mkv
  ```

  Sidecar `.idx` files cannot be merged this way — pick one language, or use
  the `.mkv` route.
* **Text quality depends on the font you give it.** Missing glyphs become
  `.notdef` boxes *in the bitmap*, permanently — check `--preview` before
  committing to a conversion of a whole film.
* If your TV *does* accept `.ssa`/`.ass`, that is the better route for the same
  complaint: it is text (tiny files, restylable) and firmware that rejects SRT
  often still plays SSA. `ffmpeg -i in.srt out.ass` is enough.

## Layout

```
srt2idx/srt.py      SubRip parser (forgiving)
srt2idx/render.py   Pillow text -> 4-colour bitmap, wrapping, canvas fitting
srt2idx/ass.py      libass/ffmpeg batch render (shaped scripts)
srt2idx/spu.py      DVD subpicture encoder: RLE, control commands, PES framing
srt2idx/vobsub.py   .idx writer
srt2idx/decode.py   reference decoder (round-trip checks, previews)
srt2idx/preview.py  decode a pair back to PNGs
srt2idx/cli.py      command line, self-check, mkv muxing
```

`spu.py` and `decode.py` were written against the bitstream layout in FFmpeg's
`libavcodec/dvdsubenc.c` / `dvdsubdec.c` and cross-checked against both.
