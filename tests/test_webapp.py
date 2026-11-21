"""Tests for the browser interface: schema/CLI parity, routes, jobs, safety.

The server is exercised in-process on an ephemeral port so these run in CI with
no browser and no fixture files beyond a generated .srt.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import webapp
from srt2idx import settings
from srt2idx.settings import FIELDS, Options

SRT = """1
00:00:01,000 --> 00:00:04,000
Hello TV from the web app

2
00:00:05,000 --> 00:00:08,000
second cue with a longer body
that needs to wrap
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    work = tmp_path_factory.mktemp("webdata")
    cfg = webapp.Config(workdir=str(work))
    os.makedirs(cfg.uploads, exist_ok=True)
    os.makedirs(cfg.jobs_dir, exist_ok=True)
    webapp.CONFIG = cfg
    webapp.QUEUE = webapp.Queue(workers=1)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, cfg
    httpd.shutdown()
    httpd.server_close()
    t.join(timeout=3)


def get(base, path):
    try:
        with urllib.request.urlopen(base + path, timeout=30) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def post(base, path, obj=None, raw=None, headers=None):
    if raw is not None:
        data = raw
        h = {"content-type": "application/octet-stream"}
    else:
        data = json.dumps(obj or {}).encode()
        h = {"content-type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(base + path, data=data, method="POST", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def jpost(base, path, obj=None, raw=None):
    code, body, hdr = post(base, path, obj, raw)
    return code, json.loads(body or b"{}"), hdr


# --------------------------------------------------------- schema/CLI parity
def test_every_setting_is_reachable_from_the_ui():
    """The UI is generated from FIELDS, so parity is structural.  What must be
    asserted is that nothing is silently hidden from *both* front ends."""
    sch = settings.schema()
    shown = {f["key"] for g in sch["groups"] for f in g["fields"]}
    hidden = {f.key for f in FIELDS if f.ui == "hidden"}
    # only path/terminal plumbing may be hidden, never a real knob
    assert hidden <= {"output", "mkv_output", "verbose", "dry_run", "srt"}, hidden
    not_a_control = {"srt", "output", "video", "mkv_output", "verbose",
                     "dry_run"}     # paths + terminal-only flags
    for f in FIELDS:
        if f.key in not_a_control:
            continue
        assert f.key in shown, f"{f.key} is in no form group"


def test_schema_types_are_all_renderable():
    known = {"int", "float", "str", "bool", "color", "choice", "path", "text"}
    for f in FIELDS:
        assert f.type in known, (f.key, f.type)


def test_defaults_in_schema_match_the_dataclass():
    sch = settings.schema()
    d = Options.defaults().to_dict()
    for k, v in sch["defaults"].items():
        if isinstance(d[k], int) and not isinstance(d[k], bool):
            assert v == f"{d[k]:06x}" or v == d[k], (k, v, d[k])
        else:
            assert v == d[k], k


def test_command_endpoint_mirrors_cli_flags(server):
    base, _ = server
    code, r, _ = jpost(base, "/api/command", {"options": {
        "font_size": 40, "outline": 1, "fg": "223344", "uppercase": True,
        "size": "ntsc", "verify": False, "lang": "tam"}})
    assert code == 200
    args = r["args"]
    # pairs must be present in the reconstructed command line (table order wins)
    for flag, value in (("--font-size", "40"), ("--outline", "1"),
                        ("--fg", "223344"), ("--size", "ntsc"), ("--lang", "tam")):
        assert flag in args, (flag, args)
        assert args[args.index(flag) + 1] == value, (flag, args)
    assert "--no-verify" in args and "--uppercase" in args
    assert r["canvas"] == [720, 480]
    # the returned string must actually parse back to the same settings
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("srt", nargs="?", default=None)
    settings.add_to_parser(p)
    ns = p.parse_args(args)
    back = settings.from_args(ns)
    assert (back.font_size, back.uppercase, back.fg, back.size) == (40, True,
                                                                    0x223344, "ntsc")


def test_command_endpoint_reports_invalid_settings(server):
    base, _ = server
    _, r, _ = jpost(base, "/api/command", {"options": {"fg": "not-a-colour"}})
    assert r.get("invalid")
    assert "RRGGBB" in r["error"]


def test_unknown_key_is_rejected(server):
    base, _ = server
    code, r, _ = jpost(base, "/api/command", {"options": {"colour": "ff0000"}})
    assert code == 400 and "unknown setting" in r["error"]


# -------------------------------------------------------------------- upload
def test_upload_kinds_and_extensions(server):
    base, cfg = server
    code, r, _ = jpost(base, "/api/upload?name=a.srt&kind=subtitle",
                       raw=SRT.encode())
    assert code == 201 and r["name"] == "a.srt"
    assert os.path.isfile(os.path.join(cfg.uploads, "a.srt"))
    for name, kind in [("x.sh", "subtitle"), ("y.mp3", "video"),
                       ("z.txt", "font")]:
        code, r, _ = jpost(base, f"/api/upload?name={name}&kind={kind}", raw=b"x")
        assert code == 400, (name, kind, r)


def test_upload_rejects_path_traversal(server):
    base, cfg = server
    code, r, _ = jpost(base, "/api/upload?name=../../escape.srt&kind=subtitle",
                       raw=b"1\n00:00:01,000 --> 00:00:02,000\nx\n")
    assert code == 201                          # sanitised to a bare basename
    assert r["name"] == "escape.srt"
    assert not os.path.exists(os.path.join(os.path.dirname(cfg.workdir),
                                           "escape.srt"))


def test_upload_respects_size_cap(server, tmp_path):
    base, cfg = server
    cfg.max_upload_mb = 0                        # anything is over the cap
    try:
        code, r, _ = jpost(base, "/api/upload?name=big.srt&kind=subtitle",
                           raw=b"x" * 5000)
        assert code == 400 and "cap" in r["error"]
    finally:
        cfg.max_upload_mb = 4096


def test_files_listing_buckets_by_kind(server):
    base, _ = server
    jpost(base, "/api/upload?name=buck.srt&kind=subtitle", raw=SRT.encode())
    jpost(base, "/api/upload?name=buck.ttf&kind=font", raw=b"\x00\x01")
    _, files, _ = get(base, "/api/files")
    f = json.loads(files)
    names = {k: [x["name"] for x in v] for k, v in f.items()}
    assert "buck.srt" in names["subtitle"] and "buck.ttf" in names["font"]
    assert all(not n.endswith(".srt") for n in names["video"])
    assert all(set(x) >= {"name", "bytes", "mtime"} for v in f.values() for x in v)


def test_rename_and_delete(server):
    base, cfg = server
    jpost(base, "/api/upload?name=before.srt&kind=subtitle", raw=SRT.encode())
    code, r, _ = jpost(base, "/api/file/rename",
                       {"name": "before.srt", "to": "after.srt"})
    assert code == 200 and os.path.isfile(os.path.join(cfg.uploads, "after.srt"))
    code, r, _ = jpost(base, "/api/file/rename",
                       {"name": "after.srt", "to": "other.txt"})
    assert code == 400 and "extension" in r["error"]     # keep sidecar matching
    code, r, _ = jpost(base, "/api/file/delete", {"name": "after.srt"})
    assert code == 200 and not os.path.exists(os.path.join(cfg.uploads,
                                                            "after.srt"))


# ------------------------------------------------------------------ rendering
def test_sample_endpoint_returns_png(server):
    base, _ = server
    code, body, hdr = post(base, "/api/sample", {"text": "Hello TV",
                                                 "options": {"font_size": 36,
                                                             "fg": "ffe082"}})
    assert code == 200 and hdr.get("Content-Type") == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(body) > 500


def test_sample_endpoint_accepts_sinhala_text(server):
    base, _ = server
    code, body, _ = post(base, "/api/sample",
                         {"text": "සුළු මොහොතක්", "options": {"engine": "pil"}})
    assert code == 200 and body[:4] == b"\x89PNG"


# ----------------------------------------------------------------- job flow
def _wait(base, jid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, body, _ = get(base, f"/api/jobs/{jid}")
        j = json.loads(body)
        if j["state"] in ("done", "error", "cancelled"):
            return j
        time.sleep(0.2)
    raise AssertionError(f"job {jid} never finished: {j}")


def test_job_end_to_end_writes_pair_and_artifacts(server):
    base, cfg = server
    jpost(base, "/api/upload?name=movie.srt&kind=subtitle", raw=SRT.encode())
    code, r, _ = jpost(base, "/api/jobs", {
        "files": ["movie.srt"],
        "options": {"verify": True, "preview": True, "font_size": 30}})
    assert code == 201
    j = _wait(base, r["id"])
    assert j["state"] == "done", j
    item = j["items"][0]
    assert item["encoded"] == 2 and item["problems"] == []
    assert item["idx"] == "movie.idx"

    # the files exist and are downloadable from the job dir
    for name in (item["idx"], item["sub"]):
        c, body, hdr = get(base, f"/api/jobs/{r['id']}/file?name={name}")
        assert c == 200, (c, body[:200])
        assert len(body) == item[f"{name.rsplit('.', 1)[1]}_size"], name
        assert os.path.isfile(os.path.join(cfg.jobs_dir, r["id"], name))

    # previews are inline images, and the .idx can be read back as text
    c, body, hdr = get(base, f"/api/jobs/{r['id']}/preview?name={item['previews'][0]}")
    assert c == 200 and body[:4] == b"\x89PNG"
    c, body, _ = get(base, f"/api/jobs/{r['id']}/text?name={item['idx']}")
    assert c == 200
    text = json.loads(body)["text"]
    assert text.startswith("# VobSub index file,")
    assert "timestamp: 00:00:01:000" in text


def test_job_dry_run_writes_nothing(server):
    base, cfg = server
    jpost(base, "/api/upload?name=estimate.srt&kind=subtitle", raw=SRT.encode())
    _, r, _ = jpost(base, "/api/jobs", {"files": ["estimate.srt"],
                                        "options": {"dry_run": True}})
    j = _wait(base, r["id"])
    assert j["state"] == "done"
    item = j["items"][0]
    assert item["dry_run"] and item["encoded"] == 2
    assert item["idx"] is None
    d = os.path.join(cfg.jobs_dir, r["id"])
    assert not os.path.exists(d) or not os.listdir(d)


def test_job_reports_a_bad_file_instead_of_hanging(server):
    base, _ = server
    jpost(base, "/api/upload?name=garbage.srt&kind=subtitle",
          raw=b"this is not a subtitle file\n")
    _, r, _ = jpost(base, "/api/jobs", {"files": ["garbage.srt"], "options": {}})
    j = _wait(base, r["id"])
    assert j["state"] == "error"
    assert "no usable cues" in j["error"]


def test_batch_job_covers_every_file(server):
    base, _ = server
    for i, extra in enumerate(["b", "c"]):
        jpost(base, f"/api/upload?name={extra}.srt&kind=subtitle",
              raw=SRT.encode())
    _, r, _ = jpost(base, "/api/jobs", {"files": ["b.srt", "c.srt"],
                                        "options": {"verify": True}})
    j = _wait(base, r["id"], timeout=240)
    assert j["state"] == "done", j
    assert [it["file"] for it in j["items"]] == ["b.srt", "c.srt"]
    assert all(it["problems"] == [] for it in j["items"])


def test_bundle_zip_contains_the_pair(server):
    import io
    import zipfile
    base, _ = server
    jpost(base, "/api/upload?name=zipped.srt&kind=subtitle", raw=SRT.encode())
    _, r, _ = jpost(base, "/api/jobs", {"files": ["zipped.srt"],
                                        "options": {"preview": False,
                                                    "verify": True}})
    j = _wait(base, r["id"])
    assert j["state"] == "done"
    code, body, hdr = get(base, f"/api/jobs/{r['id']}/bundle")
    assert code == 200 and hdr.get("Content-Type") == "application/zip"
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = z.namelist()
    assert "zipped.idx" in names and "zipped.sub" in names
    assert z is not None


def test_job_list_and_unknown_job(server):
    base, _ = server
    _, body, _ = get(base, "/api/jobs")
    assert any(j["id"] for j in json.loads(body)["jobs"])
    code, body, _ = get(base, "/api/jobs/deadbeef0000")
    assert code == 404 or "no such job" in body.decode()


def test_artifact_route_refuses_to_leave_the_job_dir(server):
    base, cfg = server
    _, jobs, _ = get(base, "/api/jobs")
    jid = json.loads(jobs)["jobs"][0]["id"]
    # a traversal is flattened to a bare name, which then is not in this job
    for probe in ("../../../etc/passwd", "..%2F..%2Fetc%2Fpasswd",
                  "%2e%2e%2fpasswd"):
        code, body, _ = get(base, f"/api/jobs/{jid}/file?name={probe}")
        assert code in (400, 404), (probe, code, body[:120])
        assert b"root:" not in body
    assert not os.path.exists(os.path.join(cfg.jobs_dir, jid, "passwd"))
    # control characters in a name are refused outright
    code, body, _ = get(base, f"/api/jobs/{jid}/file?name=x%00y")
    assert code == 400 and "control" in body.decode()


def test_endpoint_404s_rather_than_hanging(server):
    base, _ = server
    code, body, _ = get(base, "/api/nonsense")
    assert code == 404


def test_index_and_assets_served(server):
    base, _ = server
    code, body, hdr = get(base, "/")
    html = body.decode()
    assert code == 200 and "<!doctype html>" in html
    assert "/app.js" in html and "/app.css" in html
    # the form must be built at runtime from the schema, not hard-coded
    assert "CFG.schema.groups" in get(base, "/app.js")[1].decode()


def test_stop_route_exists(server):
    base, _ = server
    jpost(base, "/api/upload?name=stopme.srt&kind=subtitle", raw=SRT.encode())
    _, r, _ = jpost(base, "/api/jobs", {"files": ["stopme.srt"], "options": {}})
    code, body, _ = jpost(base, f"/api/jobs/{r['id']}/stop", {})
    assert code == 200 and "stopped" in body
