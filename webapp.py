#!/usr/bin/env python3
"""Browser interface for srt2idx -- every setting as a control.

    python3 webapp.py                       # http://127.0.0.1:8420
    python3 webapp.py --port 8080 --host 0.0.0.0

    Windows (cmd.exe / PowerShell) -- "python", not "python3":
    python webapp.py

The form is *generated* from ``srt2idx.settings.FIELDS`` -- the same table that
builds the command line flags -- so the UI can never fall behind the CLI: add a
setting there and it appears here, in ``srt2idx --help`` and in the command
preview without touching this file.

Stdlib only (http.server).  ffmpeg, when present, powers the libass renderer
and .mkv muxing; the page shows which of those it found.

Local tool by design: it writes only inside --workdir, accepts only bare
filenames from there, and binds to 127.0.0.1 unless you pass --host 0.0.0.0
(which lets anyone on the network run conversions on this machine).
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from srt2idx import pipeline, settings
from srt2idx.render import system_font_dirs
from srt2idx.pipeline import Cancelled, UsageError
from srt2idx.settings import Options

APP = "srt2idx"
ROOT = os.path.dirname(os.path.abspath(__file__))
# one source of truth with the renderer, so the picker offers what find_font
# would find on this OS (on Windows that means %WINDIR%\Fonts)
DEFAULT_FONT_DIRS = system_font_dirs()
BADCHAR = re.compile(r"[/\\\x00-\x1f]")
SAFE = re.compile(r"[^A-Za-z0-9._ \u0080-\uFFFF-]")


# --------------------------------------------------------------------- files
def safe_name(name: str) -> str:
    """Basename it, strip separators and '..'.  Raises on anything else."""
    raw = str(name or "")
    if BADCHAR.search(raw.replace("/", "").replace("\\", "")):
        raise UsageError("filename contains control characters")
    base = os.path.basename(raw.replace("\\", "/")).strip()
    base = SAFE.sub("_", base)[:150]
    if not base or base in (".", "..") or base.startswith("."):
        raise UsageError(f"unsafe filename: {base!r}")
    return base


def disposition(name: str, inline: bool = False) -> str:
    """Content-Disposition with an ASCII fallback for non-Latin filenames."""
    ascii_name = name.encode("ascii", "ignore").decode() or "download"
    return (f'{"inline" if inline else "attachment"}; '
            f'filename="{ascii_name}"; filename*=UTF-8\'\'{quote(name)}')


def unique_path(directory: str, name: str) -> str:
    """movie.srt -> movie.srt, or movie.1.srt if that name is taken."""
    stem, ext = os.path.splitext(name)
    for i in range(1, 1000):
        cand = os.path.join(directory, name if i == 1 else f"{stem}.{i}{ext}")
        if not os.path.exists(cand):
            return cand
    raise UsageError("too many files with that name")


def within(root: str, path: str) -> str:
    """Resolve `path` under `root`, refusing to escape it."""
    root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, path))
    if full != root and not full.startswith(root + os.sep):
        raise UsageError("path escapes the work dir")
    return full


# ---------------------------------------------------------------------- jobs
@dataclass
class Job:
    id: str
    label: str
    state: str = "queued"            # queued|running|done|error|cancelled
    stage: str = ""
    done: int = 0
    total: int = 0
    note: str = ""
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    items: list[dict] = field(default_factory=list)     # one per input file
    dir: str = ""

    def to_json(self) -> dict:
        elapsed = (self.finished or time.time()) - (self.started or time.time())
        return dict(id=self.id, label=self.label, state=self.state,
                    stage=self.stage, done=self.done, total=self.total,
                    note=self.note, error=self.error, items=self.items,
                    elapsed=round(elapsed, 1),
                    progress=(round(self.done / self.total, 4)
                              if self.total else None))


class Queue:
    """A tiny serial job runner.  Conversions are CPU-bound, so one at a time
    by default (--workers to change it)."""

    def __init__(self, workers: int = 1) -> None:
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.cancel: dict[str, threading.Event] = {}
        self._pending: list[tuple[str, list, dict, str | None]] = []
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(max(1, workers))
        self.workers = workers

    def submit(self, job: Job, files: list, opts: dict, video: str | None) -> None:
        with self._lock:
            self.jobs[job.id] = job
            self.order.insert(0, job.id)
            for stale in self.order[60:]:
                self.jobs.pop(stale, None)
                self.cancel.pop(stale, None)
            self.order = self.order[:60]
            self._pending.append((job.id, files, opts, video))
            self.cancel[job.id] = threading.Event()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    return
                args = self._pending.pop(0)
            with self._sem:
                self._run(*args)

    def _run(self, job_id: str, files: list, opts: dict, video: str | None) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        ev = self.cancel[job_id]
        job.state, job.started = "running", time.time()
        try:
            for name in files:
                if ev.is_set():
                    raise Cancelled()
                run_one(job, name, opts, video, ev)
            job.state = "done"
            job.stage, job.note, job.done, job.total = "done", "", 1, 1
        except Cancelled:
            job.state = "cancelled"
            job.note = "stopped on request"
        except Exception as e:                    # surface, never 500 silently
            job.state = "error"
            job.error = str(e) or e.__class__.__name__
            if not isinstance(e, UsageError):
                traceback.print_exc()
        finally:
            job.finished = time.time()

    def stop(self, job_id: str) -> bool:
        ev = self.cancel.get(job_id)
        if not ev:
            return False
        ev.set()
        job = self.jobs[job_id]
        if job.state == "queued":
            job.state = "cancelled"
        return True

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def snapshot(self) -> list[dict]:
        return [self.jobs[i].to_json() for i in self.order if i in self.jobs]


def run_one(job: Job, srt_name: str, template: dict, video: str | None,
            ev: threading.Event) -> None:
    """One input file -> .idx/.sub (+ previews, + .mkv) inside the job dir."""
    cfg = CONFIG
    src = os.path.join(cfg.uploads, safe_name(srt_name))
    if not os.path.exists(src):
        raise UsageError(f"no such uploaded file: {srt_name}")

    stem = os.path.splitext(os.path.basename(src))[0]
    if template.get("_name_after_video") and video:
        # so the sidecar trio shares one basename, which is what TVs match on
        stem = os.path.splitext(safe_name(video))[0]

    out_dir = os.path.join(cfg.jobs_dir, job.id)
    os.makedirs(out_dir, exist_ok=True)
    job.dir = out_dir

    d = {k: v for k, v in template.items() if not k.startswith("_")}
    opts = Options.from_dict({
        **d,
        "srt": src,
        "output": os.path.join(out_dir, f"{stem}.idx"),
        "video": os.path.join(cfg.uploads, safe_name(video)) if video else None,
        # results must stay inside the job dir: that is what the download and
        # zip routes are allowed to read
        "mkv_output": os.path.join(out_dir, f"{stem}.vobsub.mkv"),
    })
    if opts.mkv and not opts.video and not opts.dry_run:
        raise UsageError("muxing needs a video: pick or upload one in the "
                         "Video card, or turn 'mux into .mkv' off")

    def progress(stage, done, total, note):
        if ev.is_set():
            raise Cancelled()
        job.stage, job.done, job.total, job.note = stage, done, total, note

    job.items.append({"file": srt_name, "stem": stem, "state": "running",
                      "options": d, "dry_run": bool(opts.dry_run)})
    entry = job.items[-1]
    try:
        rep = pipeline.run(opts, progress=progress)
    except UsageError as e:
        entry.update(state="error", error=str(e))
        raise
    except Cancelled:
        entry.update(state="cancelled", error="stopped")
        raise
    except Exception as e:
        entry.update(state="error", error=str(e))
        traceback.print_exc()
        raise UsageError(f"{srt_name}: {e}") from None

    entry.update(
        state="error" if rep.verify_problems else "done",
        cues=rep.cues, encoded=rep.encoded, skipped=rep.skipped,
        long_cues=rep.long_cues, oversized=rep.oversized,
        engine=rep.engine, shaping=rep.shaping,
        canvas=list(rep.canvas), size=opts.size,
        idx=os.path.basename(rep.idx_path) if rep.idx_path else None,
        sub=os.path.basename(rep.sub_path) if rep.sub_path else None,
        idx_size=rep.idx_size, sub_size=rep.sub_size,
        problems=rep.verify_problems, notes=rep.notes, warnings=rep.warnings,
        previews=[os.path.basename(p) for p in rep.previews],
        mkv=os.path.basename(rep.mkv_path) if rep.mkv_path else None,
        spu_kib=rep.spu_bytes // 1024,
        elapsed=round(rep.elapsed, 2),
    )


# ---------------------------------------------------------------------- HTTP
CONFIG: "Config | None" = None
QUEUE: Queue | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}-webapp/1"
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------
    def log_message(self, fmt, *a):
        if CONFIG and CONFIG.verbose:
            sys.stderr.write(f"{self.address_string()} {fmt % a}\n")

    def _send(self, code: int, body: bytes, ctype: str, extra=None) -> None:
        head = self.headers.get("Connection", "")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if "close" in head.lower():
            self.send_header("Connection", "close")
            self.close_connection = True
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_out(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def err(self, msg: str, code: int = 400) -> None:
        self.json_out({"error": str(msg)}, code)

    def body_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise UsageError("bad Content-Length") from None
        if n <= 0:
            return {}
        if n > 4_000_000:
            raise UsageError("request body too large")
        raw = self.rfile.read(n)
        try:
            d = json.loads(raw.decode("utf-8"))
        except Exception:
            raise UsageError("body must be JSON") from None
        if not isinstance(d, dict):
            raise UsageError("body must be a JSON object")
        return d

    def q(self, qs: dict, key: str, default: str = "") -> str:
        v = qs.get(key)
        return v[0] if v else default

    # -- routing --------------------------------------------------------
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_HEAD(self):
        self.dispatch("GET")

    def dispatch(self, method: str) -> None:
        try:
            self.route(method)
        except UsageError as e:
            self.err(str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            traceback.print_exc()
            try:
                self.err(f"internal error: {e}", 500)
            except Exception:
                pass

    def route(self, method: str) -> None:
        u = urlparse(self.path)
        path = u.path if u.path != "/" else "/"
        if path != "/":
            path = path.rstrip("/")
        qs = parse_qs(u.query)
        post = method == "POST"

        if path == "/":
            return self._send(200, INDEX.encode(), "text/html; charset=utf-8")
        if path == "/app.css":
            return self._send(200, CSS.encode(), "text/css; charset=utf-8")
        if path == "/app.js":
            return self._send(200, JS.encode(), "text/javascript; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path == "/healthz":
            return self.json_out({"ok": True, "app": APP})

        if path == "/api/config" and not post:
            return self.json_out(api_config())
        if path == "/api/files" and not post:
            return self.json_out(api_files())
        if path == "/api/command":
            return self.json_out(api_command(self.body_json() if post else {}))
        if path == "/api/sample" and post:
            png, warning = api_sample(self.body_json())
            headers = {"Content-Disposition": 'inline; filename="preview.png"'}
            if warning:
                headers["X-Preview-Warning"] = warning
            return self._send(200, png, "image/png", headers)
        if path == "/api/probe" and post:
            return self.json_out(api_probe(self.body_json()))

        m = re.fullmatch(r"/api/jobs/([\w-]{6,40})/stop", path)
        if m and post:
            ok = QUEUE.stop(m.group(1)) if QUEUE else False
            return self.json_out({"stopped": bool(ok)})
        m = re.fullmatch(r"/api/jobs/([\w-]{6,40})/(file|preview|text|bundle)", path)
        if m and not post:
            return self.serve_artifact(m.group(1), m.group(2), qs)
        m = re.fullmatch(r"/api/jobs/([\w-]{6,40})", path)
        if m and not post:
            job = QUEUE.get(m.group(1)) if QUEUE else None
            if not job:
                return self.err("no such job", 404)
            return self.json_out(job.to_json())

        if path == "/api/jobs":
            if post:
                return self.json_out(api_create_job(self.body_json()), 201)
            return self.json_out({"jobs": QUEUE.snapshot() if QUEUE else []})
        if path == "/api/upload" and post:
            return self.json_out(api_upload(self, qs), 201)
        if path == "/api/file/delete":
            return self.json_out(api_delete(self.body_json() if post else {}))
        if path == "/api/file/rename":
            return self.json_out(api_rename(self.body_json()))

        return self.err(f"no such endpoint: {path}", 404)

    # -- artifacts ------------------------------------------------------
    def serve_artifact(self, job_id: str, kind: str, qs: dict) -> None:
        job = QUEUE.get(job_id) if QUEUE else None
        if not job or not job.dir:
            return self.err("no such job", 404)

        if kind == "bundle":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for name in sorted(os.listdir(job.dir)):
                    p = os.path.join(job.dir, name)
                    if os.path.isfile(p):
                        z.write(p, name)
            data = buf.getvalue()
            return self._send(200, data, "application/zip", {
                "Content-Disposition": f'attachment; filename="{job_id}.zip"'})

        want = safe_name(self.q(qs, "name"))
        full = within(job.dir, want)
        if not os.path.isfile(full):
            return self.err(f"not in this job: {want}", 404)
        with open(full, "rb") as fh:
            data = fh.read()
        if kind == "text":
            if not want.lower().endswith((".idx", ".txt", ".srt")):
                return self.err("only text files can be shown inline", 400)
            return self.json_out({"name": want,
                                  "text": data.decode("utf-8", "replace")})
        ctype = mimetypes.guess_type(want)[0] or "application/octet-stream"
        if want.lower().endswith(".idx"):
            ctype = "text/plain; charset=utf-8"
        inline = kind == "preview" or want.lower().endswith(".png")
        return self._send(200, data, ctype, {
            "Content-Disposition": disposition(want, inline=inline)})


# ----------------------------------------------------------------- API impls
def api_config() -> dict:
    from srt2idx import __version__
    ff = pipeline.ffmpeg_exe()
    return dict(
        app=APP, version=__version__,
        workdir=CONFIG.workdir,
        ffmpeg=ff,
        features=dict(ffmpeg=bool(ff), libass=bool(ff), mux=bool(ff)),
        schema=settings.schema(),
        fonts=pipeline.fonts_in(list(CONFIG.font_dirs))[:400],
        limits=dict(max_upload_mb=CONFIG.max_upload_mb),
        sample_text="සුළු මොහොතක් නැවතුම්පොළේ — Hello TV, this is a test",
        canvas=dict(pal=[720, 576], ntsc=[720, 480]),
    )


SUB_EXTS = (".srt", ".ssa", ".ass", ".vtt", ".txt")


def api_files() -> dict:
    out = {"subtitle": [], "video": [], "font": []}
    d = CONFIG.uploads
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        p = os.path.join(d, name)
        if not os.path.isfile(p) or name.startswith("."):
            continue
        low = name.lower()
        kind = None
        if low.endswith(SUB_EXTS):
            kind = "subtitle"
        elif low.endswith(pipeline.VIDEO_EXTS) and not low.endswith(".vobsub.mkv"):
            kind = "video"
        elif low.endswith((".ttf", ".otf", ".ttc")):
            kind = "font"
        if not kind:
            continue
        st = os.stat(p)
        rec = dict(name=name, bytes=st.st_size, mtime=int(st.st_mtime))
        if kind == "font":
            # an uploaded font is the user's own attempt to fix garbled
            # text; say at once if it cannot shape (Iskoola Pota, for one)
            rec["shaping"] = pipeline.can_shape(p)
            rec["label"] = name
        out[kind].append(rec)
    return out


def _split_options(payload: dict) -> tuple[dict, dict]:
    """Web form -> (settings, web-only extras)."""
    opts, extras, bad = {}, {}, []
    for k, v in payload.items():
        if k in settings.BY_KEY:
            opts[k] = v
        elif k.startswith("_"):
            extras[k] = v
        else:
            bad.append(k)
    if bad:
        raise UsageError("unknown setting(s): " + ", ".join(sorted(bad)))
    return opts, extras


def api_command(payload: dict) -> dict:
    opts, _ = _split_options(payload.get("options") or payload)
    try:
        o = Options.from_dict(opts)
    except UsageError as e:
        return {"error": str(e), "invalid": True}
    return dict(command=o.command_line(), args=o.to_args(),
                canvas=list(pipeline.PAL_SIZE if o.size == "pal"
                            else pipeline.NTSC_SIZE))


def api_sample(payload: dict) -> tuple[bytes, str]:
    """Render one cue with the current settings, and say whether it was shaped.

    The warning matters more than the picture: unshaped Sinhala looks *almost*
    right -- real glyphs, plausible font, marks one space to the right -- so a
    preview that stays silent is how a whole film gets burned wrong.
    """
    text = str(payload.get("text") or "")[:400]
    opts, _ = _split_options(payload.get("options") or {})
    o = Options.from_dict(opts)
    o.verify = o.preview = o.dry_run = False
    o.mkv = False
    warning = ""
    try:
        engine = pipeline.resolve_engine(o, text, [], [])
    except UsageError as e:
        # a preview should still show *why*: render it the only way we can
        o.engine = "pil"
        engine = "pil"
        warning = str(e)
    if not warning:
        o.engine = engine
        font, _ = pipeline.resolve_font(o, text)
        shaped, why = pipeline.shaping_status(engine, font, text)
        if not shaped:
            warning = f"NOT SHAPED: {why}"
    return pipeline.sample_png(text, o), warning


def api_probe(payload: dict) -> dict:
    name = payload.get("name")
    if not name:
        raise UsageError("probe needs a file name")
    p = os.path.join(CONFIG.uploads, safe_name(name))
    if not os.path.exists(p):
        raise UsageError(f"no such uploaded file: {name}")
    try:
        info = pipeline.probe_video(p)
    except Exception as e:
        raise UsageError(f"could not probe {name}: {e}") from None
    info["path"] = os.path.basename(p)
    return info


def api_create_job(payload: dict) -> dict:
    files = payload.get("files") or []
    if isinstance(files, str):
        files = [files]
    files = [safe_name(f) for f in files]
    if not files:
        raise UsageError("pick at least one .srt file")
    video = payload.get("video")
    video = safe_name(video) if video else None
    if video and not os.path.exists(os.path.join(CONFIG.uploads, video)):
        raise UsageError(f"no such uploaded video: {video}")

    opts, extras = _split_options(payload.get("options") or {})
    opts = dict(opts)
    if extras.get("_name_after_video") and video:
        opts["_name_after_video"] = True
    # validate now: a bad colour or language should fail the click, not the job
    Options.from_dict({**{k: v for k, v in opts.items() if not k.startswith("_")},
                       "srt": files[0], "video": video})

    job = Job(id=uuid.uuid4().hex[:12],
              label=files[0] if len(files) == 1 else f"{len(files)} subtitle files")
    QUEUE.submit(job, files, opts, video)
    return dict(id=job.id, state=job.state, files=files, video=video)


def api_upload(h: Handler, qs: dict) -> dict:
    name = safe_name(h.q(qs, "name", "upload.srt"))
    kind = h.q(qs, "kind", "subtitle")
    if kind not in ("subtitle", "video", "font"):
        raise UsageError("kind must be subtitle, video or font")
    allowed = {"subtitle": SUB_EXTS, "video": pipeline.VIDEO_EXTS,
               "font": (".ttf", ".otf", ".ttc")}[kind]
    ext = os.path.splitext(name)[1].lower()
    if ext not in allowed:
        raise UsageError(f"a {kind} must be one of {', '.join(allowed)}")
    try:
        length = int(h.headers.get("Content-Length") or 0)
    except ValueError:
        raise UsageError("bad Content-Length") from None
    cap = CONFIG.max_upload_mb * 1024 * 1024
    if length <= 0:
        raise UsageError("empty upload")
    if length > cap:
        raise UsageError(f"file is {length // 1048576} MB; the cap is "
                         f"{CONFIG.max_upload_mb} MB (raise it with "
                         f"--max-upload-mb)")
    dest = unique_path(CONFIG.uploads, name)
    written = 0
    remaining = length
    try:
        with open(dest, "wb") as fh:
            while remaining > 0:
                chunk = h.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    raise UsageError("upload cut short")
                fh.write(chunk)
                remaining -= len(chunk)
                written += len(chunk)
    except Exception:
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise
    return dict(name=os.path.basename(dest), bytes=written, kind=kind)


def api_delete(payload: dict) -> dict:
    name = safe_name(payload.get("name", ""))
    p = within(CONFIG.uploads, name)
    if not os.path.isfile(p):
        raise UsageError(f"no such file: {name}")
    os.unlink(p)
    return dict(deleted=name)


def api_rename(payload: dict) -> dict:
    """Rename an upload -- sidecar playback keys off the exact basename."""
    src = safe_name(payload.get("name", ""))
    dst = safe_name(payload.get("to", ""))
    a = within(CONFIG.uploads, src)
    b = within(CONFIG.uploads, dst)
    if not os.path.isfile(a):
        raise UsageError(f"no such file: {src}")
    if os.path.exists(b):
        raise UsageError(f"{dst} already exists")
    if os.path.splitext(a)[1].lower() != os.path.splitext(b)[1].lower():
        raise UsageError("keep the same extension")
    os.rename(a, b)
    return dict(renamed=dst)


INDEX = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>srt2idx &middot; SRT to VobSub</title>
<link rel="stylesheet" href="/app.css">
</head><body>
<header>
  <div class="brand">
    <h1>srt2idx</h1>
    <p class="tag">SubRip &rarr; VobSub (<code>.idx</code> + <code>.sub</code>) for
      TVs that only play bitmap subtitles &mdash; so you convert what you have
      instead of hunting for a matching <code>.idx</code></p>
  </div>
  <div class="caps" id="caps"></div>
</header>

<main>
  <section class="col" id="col-files">
    <div class="card">
      <div class="cardhead">
        <h2>Subtitles <span class="hint">.srt</span></h2>
        <button class="ghost tiny" id="btn-pick" type="button">choose</button>
      </div>
      <label class="drop" id="drop-srt">
        <input type="file" id="in-srt" accept=".srt,.ssa,.ass,.vtt,.txt" multiple hidden>
        <span class="dropmsg">Drop files here &mdash; several at once is fine<br>
          <small>click a row to include or skip it</small></span>
      </label>
      <ul class="filelist" id="list-srt"></ul>
    </div>

    <div class="card">
      <h2>Video <span class="hint">for .mkv muxing</span></h2>
      <select id="sel-video"><option value="">none &mdash; sidecar files only</option></select>
      <p class="hint" id="mux-hint"></p>
      <label class="drop small" id="drop-video">
        <input type="file" id="in-video" hidden
               accept=".mkv,.mp4,.avi,.mov,.m4v,.wmv,.mpg,.mpeg,.ts,.m2ts,.vob">
        <span class="dropmsg">or upload a video (video/audio are stream-copied,
          never re-encoded)</span>
      </label>
      <label class="check"><input type="checkbox" id="name-after-video" checked>
        <span>name the <code>.idx</code>/<code>.sub</code> after the video</span></label>
      <pre class="probe" id="probe" hidden></pre>
    </div>

    <div class="card">
      <h2>Queue</h2>
      <ul class="jobs" id="list-jobs"><li class="empty">nothing yet</li></ul>
    </div>
  </section>

  <section class="col" id="col-settings">
    <div class="card grow">
      <div class="cardhead">
        <h2>Settings</h2>
        <div class="btns">
          <button class="ghost" id="btn-reset" type="button">reset</button>
          <button class="ghost" id="btn-estimate" type="button">estimate</button>
        </div>
      </div>
      <div id="form"></div>
      <div class="cmdline">
        <label>same run from a terminal</label>
        <code id="cmd">srt2idx</code>
        <button class="ghost tiny" id="btn-copy" type="button">copy</button>
      </div>
    </div>
  </section>

  <section class="col" id="col-out">
    <div class="card">
      <div class="cardhead">
        <h2>Live look</h2>
        <span class="hint" id="sample-note">one cue, your settings</span>
      </div>
      <textarea id="sample-text" rows="2" spellcheck="false"></textarea>
      <div class="frame"><img id="sample-img" alt="preview"></div>
    </div>
    <div class="card grow">
      <div class="cardhead"><h2>Result</h2><span class="hint" id="result-note"></span></div>
      <div id="result"><p class="empty">Upload a .srt and run it &mdash; counts,
        warnings, and previews decoded back from the finished files appear here.</p></div>
    </div>
  </section>
</main>

<div class="toast" id="toast" hidden></div>
<script src="/app.js"></script>
</body></html>
"""

CSS = """
:root{--bg:#0e1014;--panel:#161a21;--panel2:#1b2029;--line:#262c37;--ink:#e6eaf2;
--dim:#8b95a7;--acc:#5cc8ff;--good:#4ade80;--warn:#fbbf24;--bad:#f87171;
--sw:34px;--r:10px;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:18px;align-items:flex-end;justify-content:space-between;
 padding:16px 20px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap;
 background:linear-gradient(180deg,#12151b,#0e1014)}
h1{margin:0;font-size:19px;letter-spacing:.3px}
h1:after{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;
 background:var(--acc);margin-left:8px;vertical-align:middle}
.tag{margin:3px 0 0;color:var(--dim);font-size:12.5px;max-width:70ch}
code{font-family:var(--mono);font-size:12px;color:#a9c6dd}
.caps{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}
.cap{font-size:11.5px;padding:4px 9px;border-radius:99px;border:1px solid var(--line);
 color:var(--dim);background:var(--panel);white-space:nowrap}
.cap.on{color:var(--good);border-color:#24503b}
.cap.off{color:var(--warn);border-color:#4a3b12}
main{display:grid;gap:14px;padding:14px;align-items:start;
 grid-template-columns:minmax(250px,300px) minmax(370px,1fr) minmax(310px,430px)}
@media (max-width:1200px){main{grid-template-columns:1fr 1fr}
 #col-out{grid-column:1/-1}}
@media (max-width:780px){main{grid-template-columns:1fr}}
.col{display:flex;flex-direction:column;gap:14px;min-width:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:13px}
.card.grow{flex:1}
h2{margin:0 0 9px;font-size:11.5px;font-weight:650;letter-spacing:.11em;
 text-transform:uppercase;color:var(--dim)}
h2 .hint{letter-spacing:0;text-transform:none;font-weight:400;font-size:11.5px;margin-left:6px;color:#5f6a7d}
.cardhead{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:9px}
.cardhead h2{margin:0}
.cardhead+.cmdline,.cardhead+#form{margin-top:0}
.hint{color:var(--dim);font-size:11.5px}
.drop{display:block;border:1px dashed #38404f;border-radius:9px;padding:14px;
 text-align:center;color:var(--dim);cursor:pointer;transition:.15s}
.drop:hover,.drop.hot{border-color:var(--acc);color:var(--ink);background:#131a23}
.drop.small{padding:9px;font-size:12px;margin-top:8px}
.dropmsg small{color:#5f6a7d}
.filelist,.jobs{list-style:none;margin:9px 0 0;padding:0;display:flex;
 flex-direction:column;gap:6px;max-height:230px;overflow:auto}
.filelist li,.jobs li{display:flex;gap:8px;align-items:center;background:var(--panel2);
 border:1px solid var(--line);border-radius:8px;padding:6px 8px;font-size:12.5px}
.filelist li{cursor:pointer;opacity:.55}
.filelist li.on{opacity:1;border-color:#2f87b8;background:#122130}
.filelist li:before{content:"";width:7px;height:7px;border-radius:50%;background:#3a4557;flex:none}
.filelist li.on:before{background:var(--acc)}
.filelist li.empty,.jobs li.empty,#result .empty{color:#5f6a7d;justify-content:center;
 border-style:dashed;background:none;font-size:12.5px;padding:10px;opacity:1;cursor:default}
.filelist li.empty:before{display:none}
.filelist .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.filelist .sz{color:var(--dim);font-size:11px;font-family:var(--mono)}
.filelist .rm{background:none;border:0;color:#4d5666;cursor:pointer;font-size:14px;
 padding:0 2px;line-height:1}
.filelist .rm:hover{color:var(--bad)}
select,input[type=text],input[type=number],textarea{background:#0f131a;color:var(--ink);
 border:1px solid var(--line);border-radius:7px;padding:6px 8px;font:inherit;width:100%}
select{width:100%;margin-bottom:2px}
textarea{font-family:var(--mono);font-size:12.5px;resize:vertical}
.row{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:9px}
.check{display:flex;gap:7px;align-items:center;font-size:12.5px;cursor:pointer;
 margin-top:9px;color:#c3cad8}
.swrow.muted{opacity:.4}
.swrow.muted input{cursor:not-allowed}
#mux-hint{margin:6px 0 0;min-height:1em;color:var(--warn)}
.probe{margin:9px 0 0;background:#0f131a;border:1px solid var(--line);border-radius:7px;
 padding:8px;font-family:var(--mono);font-size:11.5px;color:var(--dim);white-space:pre-wrap}
.grp{border-top:1px solid var(--line);padding:11px 0 2px;margin-top:8px}
.grp:first-child{border-top:0;padding-top:0;margin-top:0}
.grp h3{margin:0 0 9px;font-size:11px;text-transform:uppercase;letter-spacing:.12em;
 color:#6d788c;font-weight:650}
.f{display:grid;grid-template-columns:118px 1fr;gap:8px;align-items:center;margin:0 0 9px}
.f>label{font-size:12.5px;color:#c3cad8;cursor:default}
.f .q{color:#4d5666;cursor:help;font-size:11px}
.f .q:hover{color:var(--acc)}
.f input[type=range]{width:150px;accent-color:var(--acc);padding:0;border:0;background:none}
.f .num{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.f .num input[type=number]{width:78px}
.f .mini{color:#5f6a7d;font-size:11px;font-family:var(--mono)}
.sw{position:relative;width:var(--sw);height:20px;flex:none;display:inline-block}
.sw input{position:absolute;inset:0;opacity:0;margin:0;cursor:pointer;width:100%;height:100%}
.sw i{display:block;width:var(--sw);height:20px;border-radius:99px;background:#2b3444;
 transition:.16s;box-shadow:inset 0 0 0 1px #39445a}
.sw i:after{content:"";position:absolute;top:3px;left:3px;width:14px;height:14px;
 border-radius:50%;background:#8b95a7;transition:.16s}
.sw input:checked+i{background:#1c5c80;box-shadow:inset 0 0 0 1px #2f87b8}
.sw input:checked+i:after{transform:translateX(14px);background:var(--acc)}
.swrow{display:flex;gap:9px;align-items:center}
.swrow b{font-weight:500;font-size:12.5px;color:#c3cad8}
.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;width:max-content}
.seg button{background:#0f131a;border:0;border-right:1px solid var(--line);color:var(--dim);
 padding:5px 12px;font:inherit;font-size:12.5px;cursor:pointer}
.seg button:last-child{border-right:0}
.seg button.on{background:#15304a;color:var(--acc)}
.seg button:disabled{opacity:.35;cursor:not-allowed}
.seg button:disabled.on{background:#2a2f1e;color:var(--warn)}
input[type=color]{width:34px;height:23px;padding:0;border:1px solid var(--line);
 border-radius:6px;background:#0f131a}
.col-row{display:flex;gap:8px;align-items:center}
.col-row input[type=text]{width:92px;font-family:var(--mono);text-transform:uppercase}
.fontpick{display:flex;gap:8px;align-items:center}
.fontpick select{flex:1;margin:0}
.frame{margin-top:9px;border:1px solid var(--line);border-radius:8px;overflow:hidden;
 background:#05070a;aspect-ratio:720/300;display:flex;align-items:center;justify-content:center}
.frame img{width:100%;display:block;image-rendering:pixelated}
.btns{display:flex;gap:7px}
button.primary{background:var(--acc);color:#062234;border:0;border-radius:8px;
 padding:10px 15px;font:inherit;font-weight:650;cursor:pointer;width:100%;margin-top:10px}
button.primary:hover{filter:brightness(1.08)}
button.primary:disabled{opacity:.45;cursor:not-allowed}
button.ghost{background:#0f131a;border:1px solid var(--line);color:var(--dim);
 border-radius:7px;padding:5px 10px;font:inherit;font-size:12px;cursor:pointer}
button.ghost:hover{color:var(--ink);border-color:#3b4557}
button.ghost.tiny{padding:2px 7px;font-size:11px}
.cmdline{margin-top:12px;border-top:1px solid var(--line);padding-top:9px;position:relative}
.cmdline label{display:block;font-size:10.5px;text-transform:uppercase;
 letter-spacing:.1em;color:#5f6a7d;margin-bottom:4px}
.cmdline code{display:block;background:#0b0e13;border-radius:7px;padding:7px 60px 7px 9px;
 word-break:break-all;color:#a9c6dd;font-size:11.5px;line-height:1.45;min-height:32px}
.cmdline button{position:absolute;right:0;top:20px}
.bar{height:3px;background:#232b38;border-radius:2px;overflow:hidden;margin-top:5px;width:100%}
.bar i{display:block;height:100%;background:var(--acc);transition:width .25s;width:0}
.jobs .jname{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.jobs .st{font-size:11px;color:var(--dim);white-space:nowrap}
.jobs li.done .st{color:var(--good)}
.jobs li.error .st,.jobs li.cancelled .st{color:var(--bad)}
.jobs li.running{border-color:#2f4a63;cursor:pointer}
.jobs li.done,.jobs li.error{cursor:pointer}
.jobs{max-height:none}
.rep{display:flex;flex-direction:column;gap:10px}
.rep .one{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px}
.rep .one.error{border-color:#5a2a2a}
.rep h4{margin:0 0 8px;font-size:12.5px;font-weight:600;word-break:break-all}
.kv{display:grid;grid-template-columns:64px 1fr;gap:2px 10px;font-size:12px;margin:0}
.kv dt{color:var(--dim)}
.kv dd{margin:0;font-family:var(--mono);font-size:11.5px;word-break:break-word}
.warn{color:var(--warn);font-size:11.5px;margin:7px 0 0}
.err{color:var(--bad);font-size:11.5px;margin:7px 0 0;font-family:var(--mono);
 word-break:break-word}
.ok{color:var(--good);font-size:11.5px;margin:7px 0 0}
.note{color:var(--dim);font-size:11.5px;margin:6px 0 0}
.acts{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
.acts a{color:var(--acc);text-decoration:none;border:1px solid #23445c;border-radius:7px;
 padding:4px 9px;font-size:11.5px;background:#0f131a;cursor:pointer}
.acts a:hover{border-color:var(--acc)}
.thumbs{display:flex;gap:6px;flex-wrap:wrap;margin-top:9px}
.thumbs img{width:calc(50% - 3px);border:1px solid var(--line);border-radius:7px;
 cursor:zoom-in;background:#000;display:block}
.modal{position:fixed;inset:0;background:rgba(6,8,12,.94);display:flex;align-items:center;
 justify-content:center;z-index:50;cursor:zoom-out;flex-direction:column;gap:10px;padding:18px}
.modal img{max-width:96vw;max-height:86vh;image-rendering:pixelated;
 border:1px solid var(--line);border-radius:6px}
.modal pre{max-width:92vw;max-height:86vh;overflow:auto;background:#0b0e13;
 border:1px solid var(--line);border-radius:8px;padding:12px;font-family:var(--mono);
 font-size:11.5px;color:#c3cad8;cursor:auto;margin:0}
.toast{position:fixed;left:50%;bottom:26px;transform:translateX(-50%);
 background:#1d2531;border:1px solid #38455c;color:var(--ink);padding:9px 15px;
 border-radius:9px;font-size:12.5px;z-index:60;box-shadow:0 8px 30px #0009;max-width:80vw}
.toast.bad{border-color:#6b3232;color:#ffd9d9}
.spin{display:inline-block;width:9px;height:9px;border:2px solid #3b4557;
 border-top-color:var(--acc);border-radius:50%;animation:s .7s linear infinite}
@keyframes s{to{transform:rotate(360deg)}}
"""

JS = """
'use strict';
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
/* paths the file cards own, and terminal-only chatter: not form controls */
const NAME_ONLY = ['srt', 'output', 'video', 'mkv_output', 'verbose'];

let CFG = null, FILES = {subtitle: [], video: [], font: []}, LAST = null;
let PICKED = new Set(), CURRENT = null, TIMERS = {};
let DEFAULTS = {}, VALUES = {};

/* ------------------------------------------------------------------- boot */
async function boot() {
  CFG = await jfetch('/api/config');
  DEFAULTS = Object.assign({}, CFG.schema.defaults);
  for (const k of NAME_ONLY) delete DEFAULTS[k];
  VALUES = Object.assign({}, DEFAULTS);
  renderCaps();
  buildForm();
  wire();
  $('#sample-text').value = CFG.sample_text || 'Hello TV';
  await refreshFiles();
  syncAll();
  showSample();
  TIMERS.poll = setInterval(pollJobs, 900);
}

function renderCaps() {
  const f = CFG.features;
  $('#caps').innerHTML = [
    `<span class="cap ${f.ffmpeg ? 'on' : 'off'}" title="${esc(CFG.ffmpeg || 'not found')}">ffmpeg ${f.ffmpeg ? 'found' : 'missing'}</span>`,
    `<span class="cap ${f.libass ? 'on' : 'off'}" title="shaped scripts: Sinhala, Tamil, Arabic, Devanagari, Thai">libass ${f.libass ? 'ready' : 'unavailable'}</span>`,
    `<span class="cap ${f.mux ? 'on' : 'off'}">.mkv mux ${f.mux ? 'ready' : 'needs ffmpeg'}</span>`,
    `<span class="cap" title="${esc(CFG.workdir)}">workdir ${esc(shorten(CFG.workdir, 30))}</span>`,
  ].join('');
}
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const shorten = (p, n) => p && p.length > n ? '\\u2026' + p.slice(-n) : (p || '');

/* ------------------------------------------------------------------- form */
function buildForm() {
  const host = $('#form');
  host.innerHTML = '';
  for (const g of CFG.schema.groups) {
    const vis = g.fields.filter(f => !NAME_ONLY.includes(f.key));
    if (!vis.length) continue;
    const box = el('div', 'grp');
    box.appendChild(el('h3', null, g.title));
    for (const f of vis) box.appendChild(control(f));
    host.appendChild(box);
  }
  const b = el('button', 'primary', 'Convert');
  b.id = 'btn-run';
  b.type = 'button';
  b.onclick = () => startJob(false);
  host.appendChild(b);
}

function el(tag, cls, txt) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
}

function fieldLabel(f) {
  const lab = el('label', null, f.key.replace(/_/g, ' '));
  if (f.help) {
    const q = el('span', 'q', ' ?');
    q.title = f.help;
    lab.appendChild(q);
  }
  lab.htmlFor = 'ctl-' + f.key;
  return lab;
}

function control(f) {
  const row = el('div', 'f');
  row.appendChild(fieldLabel(f));
  const wrap = el('div');
  row.appendChild(wrap);

  if (f.type === 'bool') {
    const sw = el('div', 'swrow');
    const s = el('label', 'sw'), i = el('i'), inp = document.createElement('input');
    inp.type = 'checkbox'; inp.id = 'ctl-' + f.key; inp.dataset.k = f.key;
    inp.onchange = () => set(f.key, inp.checked);
    s.append(inp, i);
    sw.append(s, el('b', null, switchName(f.key)));
    wrap.appendChild(sw);
    return row;
  }

  if (f.type === 'choice') {
    const seg = el('div', 'seg');
    seg.dataset.k = f.key;
    for (const c of f.choices) {
      const btn = el('button', null, c);
      btn.type = 'button'; btn.dataset.v = c;
      btn.onclick = () => set(f.key, c);
      if (f.key === 'engine' && c === 'ass' && !CFG.features.libass) {
        btn.disabled = true;
        btn.title = 'needs ffmpeg';
      }
      seg.appendChild(btn);
    }
    wrap.appendChild(seg);
    return row;
  }

  if (f.type === 'color') {
    const box = el('div', 'col-row');
    const c = document.createElement('input'); c.type = 'color'; c.dataset.k = f.key;
    const t = document.createElement('input');
    t.type = 'text'; t.maxLength = 7; t.spellcheck = false; t.id = 'ctl-' + f.key;
    t.dataset.k = f.key;
    c.oninput = () => set(f.key, c.value.slice(1));
    t.oninput = () => set(f.key, t.value.replace('#', ''));
    box.append(c, t);
    wrap.appendChild(box);
    return row;
  }

  const numeric = f.type === 'int' || f.type === 'float';
  if (numeric && f.minimum != null && f.maximum != null) {
    const box = el('div', 'num');
    const r = document.createElement('input'); r.type = 'range';
    r.min = f.minimum; r.max = f.maximum;
    r.step = f.step || (f.type === 'int' ? 1 : 0.01);
    const n = document.createElement('input');
    n.type = 'number'; n.id = 'ctl-' + f.key;
    n.min = f.minimum; n.max = f.maximum; n.step = r.step;
    n.dataset.k = f.key; n.dataset.range = '1';
    r.oninput = () => set(f.key, parseFloat(r.value));
    n.onchange = () => set(f.key, parseFloat(n.value));
    box.append(r, n);
    box.appendChild(el('span', 'mini', unitFor(f.key)));
    wrap.appendChild(box);
    return row;
  }

  if (numeric) {
    const n = document.createElement('input');
    n.type = 'number'; n.id = 'ctl-' + f.key;
    if (f.minimum != null) n.min = f.minimum;
    if (f.maximum != null) n.max = f.maximum;
    n.step = f.step || (f.type === 'int' ? 1 : 0.05);
    n.dataset.k = f.key;
    n.onchange = () => set(f.key, parseFloat(n.value));
    wrap.appendChild(n);
    return row;
  }

  if (f.key === 'font') {
    const box = el('div', 'fontpick');
    const sel = document.createElement('select');
    sel.id = 'ctl-font'; sel.dataset.k = 'font';
    sel.onchange = () => set('font', sel.value || null);
    box.appendChild(sel);
    const up = document.createElement('input');
    up.type = 'file'; up.accept = '.ttf,.otf,.ttc'; up.hidden = true;
    up.onchange = () => uploadFiles(up.files, 'font');
    const btn = el('button', 'ghost tiny', 'add');
    btn.type = 'button'; btn.onclick = () => up.click();
    box.append(up, btn);
    wrap.appendChild(box);
    return row;
  }

  const t = document.createElement('input');
  t.type = 'text'; t.spellcheck = false; t.id = 'ctl-' + f.key;
  t.placeholder = f.default == null ? '' : String(f.default);
  t.dataset.k = f.key;
  t.oninput = () => set(f.key, t.value || null);
  wrap.appendChild(t);
  return row;
}

const switchName = k => ({
  mkv: 'mux into .mkv', verify: 'self-check output', preview: 'PNG previews',
  dry_run: 'estimate only', uppercase: 'force CAPITALS', bold: 'bold face',
  absolute_dates: 'absolute SPU dates',
}[k] || k.replace(/_/g, ' '));
const unitFor = k => ({font_size: 'px', outline: 'px', shadow: 'px', margin_x: 'px',
  margin_bottom: 'px', fps: 'fps', delay: 's', min_duration: 's',
  line_spacing: '\\u00d7 line'}[k] || '');

function set(key, value) { VALUES[key] = value; mark(key); schedule(); }

function mark(key) {
  const v = VALUES[key];
  for (const n of $$('[data-k="' + key + '"]')) {
    if (n.type === 'checkbox') n.checked = !!v;
    else if (n.type === 'color') n.value = '#' + hex6(v);
    else if (n.type === 'range') n.value = v;
    else if (n.tagName === 'SELECT') n.value = v || '';
    else if (n.dataset.range) n.value = num(v);
    else n.value = v == null ? '' : (typeof v === 'number' ? num(v) : v);
  }
  const seg = $('.seg[data-k="' + key + '"]');
  if (seg) for (const b of seg.children) b.classList.toggle('on', b.dataset.v === String(v));
}
const num = v => v == null || isNaN(v) ? '' : +Number(v).toFixed(3);
function hex6(v) {
  if (typeof v === 'string') return v.replace('#', '').padEnd(6, '0').slice(0, 6);
  return (Number(v) || 0).toString(16).padStart(6, '0').slice(0, 6);
}

function styleOptions() {
  /* muxing needs a video: without one the switch is meaningless, so drop it
     here rather than letting the job fail on the server */
  const o = {};
  for (const k in VALUES) o[k] = VALUES[k];
  if (!$('#sel-video').value) o.mkv = false;
  return o;
}

function syncAll() { for (const k in VALUES) mark(k); syncMux(); showCommand(); }

/* the mux switch is inert without a video -- say so instead of silently
   ignoring it */
function syncMux() {
  const has = !!$('#sel-video').value;
  const inp = $('#ctl-mkv');
  if (inp) {
    inp.disabled = !has;
    inp.parentElement.parentElement.classList.toggle('muted', !has);
    inp.title = has ? '' : 'needs a video: pick or upload one in the Video card';
  }
  const note = $('#mux-hint');
  if (note) note.textContent = has ? '' : 'needs a video';
}

/* ------------------------------------------------- live sample + command */
function schedule() {
  clearTimeout(TIMERS.sample); clearTimeout(TIMERS.cmd);
  TIMERS.sample = setTimeout(showSample, 320);
  TIMERS.cmd = setTimeout(showCommand, 200);
}

async function showCommand() {
  try {
    const r = await jfetch('/api/command', {options: styleOptions()});
    const c = $('#cmd');
    if (r.invalid) { c.textContent = '\\u26a0 ' + r.error; c.style.color = 'var(--warn)'; }
    else { c.textContent = r.command; c.style.color = ''; }
  } catch (e) { /* keep last good command */ }
}

async function showSample() {
  const img = $('#sample-img'), note = $('#sample-note');
  try {
    const res = await fetch('/api/sample', {method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({text: $('#sample-text').value, options: styleOptions()})});
    if (!res.ok) throw new Error((await res.json()).error || ('HTTP ' + res.status));
    const w = res.headers.get('X-Preview-Warning');
    note.textContent = w || (VALUES.engine === 'ass' ? 'via libass' : 'via Pillow');
    note.style.color = w ? 'var(--warn)' : '';
    const url = URL.createObjectURL(await res.blob());
    const old = img.src;
    img.onload = () => { if (old) URL.revokeObjectURL(old); };
    img.src = url;
  } catch (e) {
    note.textContent = 'preview failed';
    note.style.color = 'var(--bad)';
    note.title = e.message;
  }
}

/* --------------------------------------------------------------- uploads */
async function uploadFiles(list, kind) {
  const files = Array.from(list || []);
  if (!files.length) return;
  let failed = 0;
  for (const f of files) {
    try {
      const res = await fetch('/api/upload?name=' + encodeURIComponent(f.name) +
        '&kind=' + kind, {method: 'POST', body: f});
      const j = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(j.error || 'upload failed');
      PICKED.add(j.name);
    } catch (e) { failed++; toast(f.name + ': ' + e.message, true); }
  }
  await refreshFiles();
  if (!failed && kind === 'font') toast('font added to the picker');
}

async function refreshFiles() {
  FILES = await jfetch('/api/files');
  for (const n of [...PICKED])
    if (!FILES.subtitle.some(f => f.name === n)) PICKED.delete(n);
  if (!PICKED.size) FILES.subtitle.slice(-4).forEach(f => PICKED.add(f.name));

  const ul = $('#list-srt');
  ul.innerHTML = '';
  if (!FILES.subtitle.length) {
    ul.appendChild(el('li', 'empty', 'no .srt uploaded yet'));
  }
  for (const f of FILES.subtitle) {
    const li = el('li');
    if (PICKED.has(f.name)) li.classList.add('on');
    li.title = f.name + ' — click to toggle';
    li.appendChild(el('span', 'nm', f.name));
    li.appendChild(el('span', 'sz', kb(f.bytes)));
    const rn = el('button', 'rm', '\\u270e');
    rn.title = 'rename (sidecar matching is by filename)';
    const rm = el('button', 'rm', '\\u00d7');
    rm.title = 'delete from workdir';
    const stop = e => { e.stopPropagation(); };
    rn.onclick = async e => {
      stop(e);
      const to = prompt('new filename (keep the extension)', f.name);
      if (!to || to === f.name) return;
      try { await jfetch('/api/file/rename', {name: f.name, to}); }
      catch (err) { return toast(err.message, true); }
      PICKED.delete(f.name); PICKED.add(to); refreshFiles();
    };
    rm.onclick = async e => {
      stop(e);
      try { await jfetch('/api/file/delete', {name: f.name}); }
      catch (err) { return toast(err.message, true); }
      PICKED.delete(f.name); refreshFiles();
    };
    li.append(rn, rm);
    li.onclick = () => {
      if (PICKED.has(f.name)) PICKED.delete(f.name); else PICKED.add(f.name);
      li.classList.toggle('on', PICKED.has(f.name));
      updateRunButton();
    };
    ul.appendChild(li);
  }

  const sv = $('#sel-video'), cur = sv.value;
  sv.innerHTML = '<option value="">none \\u2014 sidecar files only</option>';
  for (const f of FILES.video) sv.appendChild(new Option(f.name + ' (' + kb(f.bytes) + ')', f.name));
  if (FILES.video.some(f => f.name === cur)) sv.value = cur;

  const sel = $('#ctl-font');
  if (sel) {
    const want = VALUES.font || '';
    sel.innerHTML = '<option value="">(auto: a face that shapes this script)</option>';
    const seen = new Set();
    for (const f of FILES.font.concat(CFG.fonts)) {
      if (seen.has(f.path)) continue;
      seen.add(f.path);
      const bad = f.shaping === false
        ? '  \u26a0 no OpenType tables: cannot shape Sinhala/Thai/Arabic'
        : (f.scripts && f.scripts.length
            ? '  \u00b7 ' + f.scripts.slice(0, 3).join(', ') : '');
      sel.appendChild(new Option((f.label || f.name)
        + (inUploads(f) ? '  \u2191' : '') + bad, f.path));
    }
    sel.value = [...sel.options].some(o => o.value === want) ? want : '';
    VALUES.font = sel.value || null;
  }
  mark('font');
  syncMux();
  updateRunButton();
}
const inUploads = f => (f.path || '').indexOf('/uploads/') >= 0;

const kb = n => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB'
  : n > 1024 ? Math.round(n / 1024) + ' KB' : (n || 0) + ' B';

async function probeVideo() {
  const p = $('#probe'), v = $('#sel-video').value;
  if (!v) { p.hidden = true; return; }
  p.hidden = false; p.textContent = 'probing\\u2026';
  try {
    const r = await jfetch('/api/probe', {name: v});
    p.textContent = [
      (r.width ? r.width + '\\u00d7' + r.height : 'size ?') + ' \\u00b7 ' + dur(r.duration_ms) +
        ' \\u00b7 ' + (r.has_audio ? 'audio' : 'no audio') +
        (r.subtitle_count ? ' \\u00b7 ' + r.subtitle_count + ' subtitle track(s) already in it' : ''),
      r.suggested_size ? 'canvas suggested by the video: ' + r.suggested_size : '',
    ].join('\\n');
    if (r.suggested_size && r.suggested_size !== VALUES.size) {
      VALUES.size = r.suggested_size;
      mark('size'); schedule();
      toast('canvas set to ' + r.suggested_size + ' to match the video');
    }
  } catch (e) { p.textContent = 'probe failed: ' + e.message; }
}
const dur = ms => ms == null ? '?'
  : Math.floor(ms / 3600000) + ':' + String(Math.floor(ms / 60000) % 60).padStart(2, '0')
    + ':' + String(Math.floor(ms / 1000) % 60).padStart(2, '0');

/* ------------------------------------------------------------------ jobs */
function updateRunButton() {
  const b = $('#btn-run');
  if (!b) return;
  const n = PICKED.size;
  b.disabled = !n;
  b.textContent = n ? (n === 1 ? 'Convert \\u2192 .idx + .sub' : `Convert ${n} files \\u2192 .idx + .sub`)
    : 'add a .srt on the left first';
}

async function startJob(dry) {
  const files = [...PICKED];
  if (!files.length) return toast('pick at least one .srt', true);
  const opts = styleOptions();
  opts.dry_run = !!dry;
  if (dry) { opts.verify = false; opts.preview = false; opts.mkv = false; }
  try {
    await jfetch('/api/jobs', {files, options: opts,
      video: $('#sel-video').value || null,
      _name_after_video: $('#name-after-video').checked});
    toast(dry ? 'estimating\\u2026' : 'job started');
    pollJobs();
  } catch (e) { toast(e.message, true); }
}

async function pollJobs() {
  let snap;
  try { snap = (await jfetch('/api/jobs')).jobs; } catch (e) { return; }
  const ul = $('#list-jobs');
  const open = $('#list-jobs').scrollTop;
  ul.innerHTML = '';
  if (!snap.length) { ul.appendChild(el('li', 'empty', 'nothing yet')); return; }
  let active = null;
  for (const j of snap) {
    const li = el('li', j.state);
    li.appendChild(el('span', 'jname', j.label));
    const txt = j.state === 'running' && j.total > 1
      ? j.stage + ' ' + j.done + '/' + j.total : j.state;
    li.appendChild(el('span', 'st', txt));
    if (j.state === 'running' || j.state === 'queued') {
      const bar = el('div', 'bar'), i = el('i');
      i.style.width = (j.progress == null ? 4 : Math.max(4, j.progress * 100)) + '%';
      bar.appendChild(i); li.appendChild(bar);
      const stop = el('button', 'rm', '\\u00d7');
      stop.title = 'stop this job';
      stop.onclick = async e => {
        e.stopPropagation();
        try { await jfetch('/api/jobs/' + j.id + '/stop', {}); } catch (err) {}
      };
      li.appendChild(stop);
      if (!active) active = j.id;
    }
    li.onclick = () => { CURRENT = j.id; showResult(j.id); };
    ul.appendChild(li);
  }
  $('#list-jobs').scrollTop = open;
  if (active) {
    if (CURRENT !== active) { CURRENT = active; LAST = null; }
    showResult(active);
    updateRunButton();
    return;
  }
  if (CURRENT) {
    const j = snap.find(x => x.id === CURRENT);
    if (j) {
      if (signature(j) !== LAST) renderResult(j);
      if (j.state === 'done' && !$('#btn-run').dataset.notified) {
        $('#btn-run').dataset.notified = '1';
        toast('done \\u2014 ' + (j.items[0] && j.items[0].encoded) + ' bitmaps');
      }
      if (j.state === 'error') toast(j.error || 'job failed', true);
    }
  }
  updateRunButton();
}

const signature = j => j.state + '|' + j.stage + '|' + j.done + '/' + j.total
  + '|' + j.items.map(i => i.state + ':' + i.file).join(',');

async function showResult(id) {
  try {
    const j = await jfetch('/api/jobs/' + id);
    if (id !== CURRENT) return;              /* a newer job took over */
    if (j.state === 'running' || j.state === 'queued') { LAST = signature(j); return; }
    if (signature(j) !== LAST) renderResult(j);
  } catch (e) { /* gone */ }
}

function renderResult(j) {
  $('#result-note').textContent = j.state === 'running'
    ? (j.note || j.stage || 'working') : j.state + ' in ' + j.elapsed + 's';
  const host = $('#result');
  host.innerHTML = '';
  if (!j.items.length) { host.appendChild(el('p', 'empty', j.error || 'no output yet')); return; }
  const wrap = el('div', 'rep');
  for (const it of j.items) {
    const one = el('div', 'one' + (it.state === 'error' ? ' error' : ''));
    one.appendChild(el('h4', null, (it.dry_run ? 'estimate \\u00b7 ' : '') + it.file));
    if (it.error && it.state === 'error') {
      one.appendChild(el('p', 'err', it.error));
      wrap.appendChild(one);
      continue;
    }
    const dl = document.createElement('dl');
    dl.className = 'kv';
    const rows = [];
    if (it.dry_run) {
      rows.push(['would be', it.encoded + ' bitmaps, ~' + it.spu_kib + ' KiB of subpicture data']);
      rows.push(['files', it.stem + '.idx + ' + it.stem + '.sub']);
    } else {
      rows.push(['cues', it.cues + ' \\u2192 ' + it.encoded + ' bitmaps'
        + (it.skipped ? ' (' + it.skipped + ' skipped)' : '')]);
      rows.push(['fit', (it.long_cues ? it.long_cues + ' shrank the font' : 'all cues fit ' + ((it.options || {}).max_lines || 2) + ' lines')
        + (it.oversized ? ' \\u00b7 ' + it.oversized + ' over 64 KiB' : '')]);
      rows.push(['canvas', it.canvas[0] + '\\u00d7' + it.canvas[1] + ' \\u00b7 ' + (it.size || '')]);
      rows.push(['engine', it.engine === 'ass' ? 'libass' : 'Pillow']);
      rows.push(['shaping', { 'libass+harfbuzz': '\u2713 shaped by HarfBuzz',
                              'NONE': '\u2717 NOT SHAPED \u2014 marks misplaced'
                            }[it.shaping] || 'not needed for this script']);
      rows.push(['files', it.idx + ' ' + it.idx_size + ' B \\u00b7 ' + it.sub + ' ' + it.sub_size + ' B']);
      rows.push(['time', it.elapsed + 's']);
    }
    for (const [k, v] of rows) {
      const dd = el('dd', null, String(v));
      if (k === 'shaping') dd.style.color =
        String(v).indexOf('\u2717') === 0 ? 'var(--bad)'
        : String(v).indexOf('\u2713') === 0 ? 'var(--good)' : '';
      dl.appendChild(el('dt', null, k)); dl.appendChild(dd);
    }
    if (!it.dry_run) {
      const dt = el('dt', null, 'check');
      const dd = el('dd', null, it.problems.length ? '\\u2717 ' + it.problems.length + ' problem(s)' : '\\u2713 decoded back identical');
      dd.style.color = it.problems.length ? 'var(--bad)' : 'var(--good)';
      dl.append(dt, dd);
      if (it.mkv) { dl.appendChild(el('dt', null, 'muxed')); dl.appendChild(el('dd', null, it.mkv)); }
    }
    one.appendChild(dl);
    for (const n of it.notes || []) one.appendChild(el('p', 'note', n));
    for (const w of it.warnings || []) one.appendChild(el('p', 'warn', '\\u26a0 ' + w));
    for (const p of (it.problems || []).slice(0, 4)) one.appendChild(el('p', 'err', '\\u00b7 ' + p));
    if (it.previews && it.previews.length) {
      const th = el('div', 'thumbs');
      for (const p of it.previews) {
        const img = el('img');
        img.src = `/api/jobs/${j.id}/preview?name=${encodeURIComponent(p)}`;
        img.alt = p; img.loading = 'lazy';
        img.onclick = () => modal(null, img.src, 'img');
        th.appendChild(img);
      }
      one.appendChild(th);
    }
    if (!it.dry_run) {
      const acts = el('div', 'acts');
      const link = (label, url, view) => {
        const a = el('a', null, label);
        if (view) a.onclick = ev => { ev.preventDefault(); viewText(j.id, url); };
        else { a.href = url; a.download = ''; }
        acts.appendChild(a);
      };
      if (it.idx) link(it.idx, `/api/jobs/${j.id}/file?name=${encodeURIComponent(it.idx)}`);
      if (it.sub) link(it.sub, `/api/jobs/${j.id}/file?name=${encodeURIComponent(it.sub)}`);
      if (it.idx) link('inspect .idx', it.idx, true);
      if (it.mkv) link(it.mkv, `/api/jobs/${j.id}/file?name=${encodeURIComponent(it.mkv)}`);
      link('everything (.zip)', `/api/jobs/${j.id}/bundle`);
      one.appendChild(acts);
    }
    wrap.appendChild(one);
  }
  host.appendChild(wrap);
}

async function viewText(jobId, name) {
  try {
    const r = await jfetch(`/api/jobs/${jobId}/text?name=${encodeURIComponent(name)}`);
    const pre = el('pre');
    pre.textContent = r.text;
    modal(pre);
  } catch (e) { toast(e.message, true); }
}

function modal(node, src, kind) {
  const b = el('div', 'modal');
  if (kind === 'img') { const i = el('img'); i.src = src; b.appendChild(i); }
  else b.appendChild(node);
  b.appendChild(el('div', 'hint', 'click anywhere to close'));
  b.onclick = () => b.remove();
  document.body.appendChild(b);
}

/* ------------------------------------------------------------------ fetch */
async function jfetch(url, body) {
  const init = body === undefined ? undefined
    : {method: 'POST', headers: {'content-type': 'application/json'},
       body: JSON.stringify(body)};
  const res = await fetch(url, init);
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(j.error || ('HTTP ' + res.status));
  return j;
}

let TT = null;
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (bad ? ' bad' : '');
  t.hidden = false;
  clearTimeout(TT);
  TT = setTimeout(() => { t.hidden = true; }, bad ? 5200 : 2100);
}

/* ------------------------------------------------------------------- wire */
function wire() {
  dropZone($('#drop-srt'), $('#in-srt'), 'subtitle');
  dropZone($('#drop-video'), $('#in-video'), 'video');
  $('#btn-pick').onclick = () => $('#in-srt').click();
  $('#sel-video').onchange = () => { syncMux(); probeVideo(); schedule(); };
  $('#btn-reset').onclick = () => {
    VALUES = Object.assign({}, DEFAULTS);
    syncAll(); showSample();
    toast('settings reset');
  };
  $('#btn-estimate').onclick = () => startJob(true);
  $('#btn-copy').onclick = () => {
    const txt = $('#cmd').textContent;
    (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject())
      .then(() => toast('command copied')).catch(() => prompt('copy:', txt));
  };
  $('#sample-text').oninput = () => { clearTimeout(TIMERS.s); TIMERS.s = setTimeout(showSample, 400); };
  window.addEventListener('error', () => {});
}

function dropZone(zone, input, kind) {
  if (!zone) return;
  zone.addEventListener('click', e => { if (e.target !== input) input.click(); });
  input.onchange = () => uploadFiles(input.files, kind);
  ['dragenter', 'dragover'].forEach(ev => zone.addEventListener(ev, e => {
    e.preventDefault(); zone.classList.add('hot');
  }));
  ['dragleave', 'drop'].forEach(ev => zone.addEventListener(ev, e => {
    e.preventDefault(); zone.classList.remove('hot');
    if (e.type === 'drop' && e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files, kind);
  }));
}
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); startJob(false); }
});

boot().catch(e => toast('could not start: ' + e.message, true));
"""


@dataclass
class Config:
    workdir: str
    verbose: bool = False
    max_upload_mb: int = 4096
    font_dirs: tuple = DEFAULT_FONT_DIRS

    @property
    def uploads(self) -> str:
        p = os.path.join(self.workdir, "uploads")
        os.makedirs(p, exist_ok=True)
        return p

    @property
    def jobs_dir(self) -> str:
        p = os.path.join(self.workdir, "jobs")
        os.makedirs(p, exist_ok=True)
        return p


def serve(workdir: str, host: str, port: int, workers: int = 1,
          verbose: bool = False, max_upload_mb: int = 4096) -> None:
    global CONFIG, QUEUE
    CONFIG = Config(workdir=os.path.abspath(workdir), verbose=verbose,
                    max_upload_mb=max_upload_mb)
    os.makedirs(CONFIG.uploads, exist_ok=True)
    os.makedirs(CONFIG.jobs_dir, exist_ok=True)
    QUEUE = Queue(workers=workers)

    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    shown = host if host not in ("0.0.0.0", "::") else (socket.gethostname()
                                                        or "localhost")
    print(f"{APP} web app  ·  http://{shown}:{port}")
    print(f"  workdir {CONFIG.workdir}")
    print(f"  ffmpeg  {pipeline.ffmpeg_exe() or 'NOT FOUND -- libass and .mkv muxing off'}")
    print(f"  uploads {CONFIG.uploads}")
    print("  Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="srt2idx-webapp",
        description="Browser UI for srt2idx: every conversion setting as a "
                    "control, generated from the same table the CLI uses.")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address; 0.0.0.0 to reach it from a phone or TV")
    p.add_argument("--port", type=int, default=8420)
    p.add_argument("--workdir", default=os.path.join(ROOT, "webdata"),
                   help="where uploads and results live (default ./webdata)")
    p.add_argument("--workers", type=int, default=1,
                   help="concurrent conversions (CPU-bound; 1 is usually right)")
    p.add_argument("--max-upload-mb", type=int, default=4096,
                   help="size cap for uploaded video files")
    p.add_argument("-v", "--verbose", action="store_true", help="log requests")
    a = p.parse_args(argv)
    try:
        serve(a.workdir, a.host, a.port, a.workers, a.verbose, a.max_upload_mb)
    except OSError as e:
        print(f"cannot listen on {a.host}:{a.port}: {e}", file=sys.stderr)
        print("try a different --port", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
