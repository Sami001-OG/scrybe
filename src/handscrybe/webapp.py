"""Local Flask web UI for handscrybe.

A single-page tool: the user uploads a document (PDF / DOCX / TXT) and,
optionally, a photo of their handwriting sample sheet (A-Z, a-z, 0-9, and an
optional punctuation row). The app
runs the exact same `pipeline.convert` the CLI uses and streams back the
resulting handwriting PDF.

DESIGN
------
* Stateless per request. Each conversion gets its own temp directory that is
  cleaned up after the response is sent, so nothing accumulates on disk and two
  users never see each other's files.
* The UI is intentionally one file (HTML/CSS/JS inlined below) so the app runs
  from a single ``python -m handscrybe.webapp`` with no template/static setup.
* This is a LOCAL tool bound to 127.0.0.1 by default. There is no auth: it is
  meant for a developer running it on their own machine, not public hosting.
  Uploaded files are size-capped to avoid accidentally OOMing the box.

The heavy lifting (parsing, layout, glyph segmentation, rendering) all lives in
the library modules; this file only handles HTTP, file staging, and reporting
glyph coverage back to the user.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request

from .config import Config
from .pipeline import convert

# Accepted document extensions (mirrors normalize.detect_format's support).
_DOC_EXTS = {".pdf", ".docx", ".txt"}
# Accepted sample-sheet image extensions.
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}

# 25 MB per upload. Generous for documents/photos, small enough to bound memory.
_MAX_CONTENT_LENGTH = 25 * 1024 * 1024

# Jobs older than this (seconds) are swept even if never downloaded, so a user
# who starts a conversion and closes the tab doesn't leak a temp dir forever.
_JOB_TTL = 900


def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


class _Job:
    """One conversion's live state, shared between the worker thread and the
    polling requests. All mutation goes through a lock because the worker writes
    ``fraction``/``message``/``status`` while ``/progress`` reads them."""

    __slots__ = (
        "id", "work", "out_path", "out_name", "mimetype", "coverage",
        "fraction", "message", "status", "error", "created", "_lock",
    )

    def __init__(self, job_id: str, work: str) -> None:
        self.id = job_id
        self.work = work
        self.out_path = None
        self.out_name = "handwriting.pdf"
        self.mimetype = "application/pdf"
        self.coverage = None
        self.fraction = 0.0
        self.message = "Starting"
        self.status = "running"  # running | done | error
        self.error = None
        self.created = time.monotonic()
        self._lock = threading.Lock()

    def update(self, fraction: float, message: str) -> None:
        with self._lock:
            # Never let the reported fraction go backwards — a monotonic bar
            # reads as trustworthy; a jumpy one does not.
            self.fraction = max(self.fraction, min(1.0, max(0.0, fraction)))
            self.message = message

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "percent": int(round(self.fraction * 100)),
                "message": self.message,
                "error": self.error,
                "coverage": self.coverage,
            }


_MIMETYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
}


def create_app() -> Flask:
    """Build and return the Flask application (factory pattern so tests can get
    an isolated instance with the test client).

    Conversions run in a background thread so the browser can poll live progress
    while the work happens. The flow is:

        POST /convert       validate + stage files, start the worker -> {job_id}
        GET  /progress/<id> poll {status, percent, message} until done/error
        GET  /result/<id>   download the finished file (then the job is swept)

    A per-app registry holds live jobs. This is a single-user local tool, so an
    in-process dict guarded by a lock is the right amount of machinery.
    """
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = _MAX_CONTENT_LENGTH

    jobs: dict[str, _Job] = {}
    jobs_lock = threading.Lock()

    def _sweep_expired() -> None:
        """Drop jobs that have outlived _JOB_TTL, cleaning their temp dirs. Cheap
        enough to run opportunistically on each new conversion."""
        now = time.monotonic()
        for jid, job in list(jobs.items()):
            if now - job.created > _JOB_TTL:
                _safe_rmtree(job.work)
                jobs.pop(jid, None)

    @app.get("/")
    def index() -> Response:
        return Response(_INDEX_HTML, mimetype="text/html")

    @app.post("/convert")
    def convert_route():
        # --- Validate the document upload -------------------------------
        doc = request.files.get("document")
        if doc is None or not doc.filename:
            return jsonify(error="No document uploaded."), 400
        doc_ext = _ext(doc.filename)
        if doc_ext not in _DOC_EXTS:
            return (
                jsonify(
                    error=f"Unsupported document type {doc_ext!r}. "
                    f"Use one of: {', '.join(sorted(_DOC_EXTS))}."
                ),
                400,
            )

        # --- Optional handwriting sample image --------------------------
        sample = request.files.get("sample")
        sample_given = sample is not None and bool(sample.filename)
        if sample_given and _ext(sample.filename) not in _IMG_EXTS:
            return (
                jsonify(
                    error=f"Unsupported image type {_ext(sample.filename)!r}. "
                    f"Use one of: {', '.join(sorted(_IMG_EXTS))}."
                ),
                400,
            )

        # --- Options ----------------------------------------------------
        ink = request.form.get("ink", "original").strip() or "original"
        mode = request.form.get("mode", "fit").strip() or "fit"
        out_fmt_str = request.form.get("format", "pdf").strip().lower() or "pdf"

        from .config import LayoutMode, OutputFormat

        try:
            out_fmt = OutputFormat(out_fmt_str)
        except ValueError:
            return (
                jsonify(
                    error=f"Unsupported output format {out_fmt_str!r}. "
                    f"Use one of: {', '.join(f.value for f in OutputFormat)}."
                ),
                400,
            )

        # Each job works in its own temp dir; cleaned up on download or by sweep.
        work = tempfile.mkdtemp(prefix="handscrybe_web_")
        doc_path = os.path.join(work, "input" + doc_ext)
        doc.save(doc_path)

        cfg = Config()
        cfg.ink_color = ink
        cfg.output_format = out_fmt
        try:
            cfg.mode = LayoutMode(mode)
        except ValueError:
            cfg.mode = cfg.mode  # keep default on a bad value

        coverage = None
        if sample_given:
            sample_path = os.path.join(work, "sample" + _ext(sample.filename))
            sample.save(sample_path)
            cfg.handwriting_image = sample_path
            # Segment once up front so we can report coverage AND fail early on a
            # bad image, before starting the worker (this is a fast, synchronous
            # validation so a bad sheet is a clean 400, not a failed job).
            try:
                from .glyphs import GlyphSet

                gs = GlyphSet.from_sheet(sample_path)
                coverage = list(gs.coverage())  # [found, expected]
            except Exception as exc:  # noqa: BLE001 - surface any imaging error
                _safe_rmtree(work)
                return jsonify(error=f"Could not read handwriting sample: {exc}"), 400

        out_path = os.path.join(work, "handwriting." + out_fmt.value)

        job_id = uuid.uuid4().hex
        job = _Job(job_id, work)
        job.out_path = out_path
        job.out_name = f"handwriting.{out_fmt.value}"
        job.mimetype = _MIMETYPES[out_fmt.value]
        job.coverage = coverage

        with jobs_lock:
            _sweep_expired()
            jobs[job_id] = job

        def _worker():
            try:
                convert(doc_path, out_path, cfg, progress=job.update)
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                with job._lock:
                    job.status = "error"
                    job.error = str(exc)
                return
            except Exception as exc:  # noqa: BLE001 - last-resort guard
                with job._lock:
                    job.status = "error"
                    job.error = f"Conversion failed: {exc}"
                return
            with job._lock:
                job.fraction = 1.0
                job.message = "Done"
                job.status = "done"

        threading.Thread(target=_worker, daemon=True).start()

        resp = {"job_id": job_id}
        if coverage is not None:
            resp["coverage"] = f"{coverage[0]}/{coverage[1]}"
        return jsonify(resp), 202

    @app.get("/progress/<job_id>")
    def progress_route(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            return jsonify(error="Unknown or expired job."), 404
        return jsonify(job.snapshot())

    @app.get("/result/<job_id>")
    def result_route(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            return jsonify(error="Unknown or expired job."), 404
        snap = job.snapshot()
        if snap["status"] == "running":
            return jsonify(error="Not finished yet."), 409
        if snap["status"] == "error":
            return jsonify(error=job.error or "Conversion failed."), 500

        # Read bytes into memory, then remove the temp dir and retire the job so
        # nothing lingers (Windows won't delete a file that's still open).
        with open(job.out_path, "rb") as fh:
            data = fh.read()
        with jobs_lock:
            jobs.pop(job_id, None)
        _safe_rmtree(job.work)

        resp = Response(data, mimetype=job.mimetype)
        resp.headers["Content-Disposition"] = f'attachment; filename="{job.out_name}"'
        if job.coverage is not None:
            resp.headers["X-Glyph-Coverage"] = f"{job.coverage[0]}/{job.coverage[1]}"
        return resp

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify(error="Upload too large (25 MB max)."), 413

    return app


def _safe_rmtree(path: str) -> None:
    """Remove a temp dir, ignoring errors (a locked file must never crash the
    response). Best-effort cleanup only."""
    import shutil

    shutil.rmtree(path, ignore_errors=True)


# --- Front-end (single inlined page) -------------------------------------
# Kept inline so the app is a single importable module with no template search
# path or static-folder setup. Plain HTML/CSS/vanilla JS; no build step.
_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>handscrybe</title>
<style>
  /* --- Palette: warm paper, deep ink, a single indigo accent --------- */
  :root {
    --paper:#f4f1ea; --paper-2:#ece7dc; --card:#fffdf8;
    --ink:#20222e; --ink-soft:#5c5f6e; --ink-faint:#9a9caa;
    --accent:#3b4c9a; --accent-2:#6d7ff0; --line:#e2ddd0;
    --ok:#2b7a4b; --err:#c0392b;
    --shadow:0 1px 2px rgba(30,30,50,.04), 0 8px 30px rgba(30,30,50,.06);
    --radius:16px;
  }
  * { box-sizing:border-box; }
  html { -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility; }
  body {
    margin:0; color:var(--ink);
    font:16px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:
      radial-gradient(1200px 600px at 50% -10%, #fbf9f4 0%, rgba(251,249,244,0) 70%),
      var(--paper);
    background-attachment:fixed;
  }
  main { max-width:680px; margin:0 auto; padding:56px 22px 96px; }

  /* --- Masthead: the HS logo + wordmark ------------------------------- */
  .masthead { text-align:center; margin-bottom:40px; }
  .logo {
    display:inline-block; margin:0 0 18px;
    font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
    font-size:11px; line-height:1.05; letter-spacing:0; white-space:pre;
    color:var(--accent); text-shadow:0 1px 0 rgba(255,255,255,.7);
  }
  .masthead h1 { font-size:26px; font-weight:650; letter-spacing:.06em;
                 margin:0; color:var(--ink); }
  .masthead .sub { margin:12px auto 0; max-width:30em; color:var(--ink-soft);
                   font-size:15px; line-height:1.55; }

  /* --- Cards ----------------------------------------------------------- */
  form { display:flex; flex-direction:column; gap:20px; }
  .card {
    background:var(--card); border:1px solid var(--line);
    border-radius:var(--radius); padding:22px 24px 24px; box-shadow:var(--shadow);
  }
  label.field { display:flex; align-items:baseline; justify-content:space-between;
    gap:12px; font-weight:650; font-size:14px; margin-bottom:12px;
    color:var(--ink); letter-spacing:.01em; }
  .row label.field { display:block; font-size:13px; margin-bottom:9px;
    color:var(--ink-soft); }
  .hint { color:var(--ink-faint); font-size:13px; font-weight:400; text-align:right; }
  .sheet-hint { margin:12px 0 0; line-height:1.55; text-align:left; }
  code { background:var(--paper-2); padding:2px 6px; border-radius:6px;
         font-size:12.5px; font-family:ui-monospace,Menlo,Consolas,monospace;
         color:var(--ink-soft); }

  /* --- Dropzone (file inputs) ----------------------------------------- */
  .drop {
    position:relative; display:flex; flex-direction:column; align-items:center;
    justify-content:center; gap:6px; text-align:center;
    padding:26px 18px; border:1.5px dashed #cbc4b2; border-radius:12px;
    background:var(--paper); color:var(--ink-soft); cursor:pointer;
    transition:border-color .18s, background .18s, transform .06s;
  }
  .drop:hover { border-color:var(--accent-2); background:#f7f5ef; }
  .drop.drag { border-color:var(--accent); background:#eef0fb; }
  .drop.filled { border-style:solid; border-color:var(--accent-2);
                 background:#f6f7fe; color:var(--ink); }
  .drop input[type=file] {
    position:absolute; inset:0; width:100%; height:100%; opacity:0; cursor:pointer;
  }
  .drop-icon { font-size:24px; line-height:1; opacity:.75; }
  .drop-title { font-weight:600; color:var(--ink); }
  .drop.filled .drop-title { color:var(--accent); word-break:break-all; }
  .drop-note { font-size:12.5px; color:var(--ink-faint); }
  .drop.filled .drop-note { color:var(--ok); }

  /* --- Selects --------------------------------------------------------- */
  .row { display:flex; gap:20px; flex-wrap:wrap; }
  .row > div { flex:1 1 220px; }
  select {
    -webkit-appearance:none; appearance:none; width:100%;
    padding:11px 34px 11px 13px; border:1px solid var(--line);
    border-radius:10px; background:var(--card)
      url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='none' stroke='%239a9caa' stroke-width='2' d='M1 1l5 5 5-5'/%3E%3C/svg%3E")
      no-repeat right 13px center;
    font-size:14.5px; color:var(--ink); cursor:pointer; transition:border-color .15s;
  }
  select:hover { border-color:#cfc8b8; }
  select:focus, .drop:focus-within { outline:none; border-color:var(--accent);
    box-shadow:0 0 0 3px rgba(59,76,154,.12); }

  /* --- Submit ---------------------------------------------------------- */
  #go {
    position:relative; align-self:stretch; border:0; cursor:pointer;
    padding:16px 22px; border-radius:12px; font-size:16px; font-weight:650;
    letter-spacing:.01em; color:#fff;
    background:linear-gradient(180deg,var(--accent-2),var(--accent));
    box-shadow:0 6px 18px rgba(59,76,154,.28);
    transition:transform .06s, box-shadow .18s, opacity .18s;
  }
  #go:hover:not(:disabled) { box-shadow:0 8px 24px rgba(59,76,154,.34); }
  #go:active:not(:disabled) { transform:translateY(1px); }
  #go:disabled { opacity:.55; cursor:default; box-shadow:none; }

  /* --- Progress -------------------------------------------------------- */
  #progress { display:none; }
  #progress.show { display:block; }
  .pbar { position:relative; height:30px; background:var(--paper-2);
          border-radius:15px; overflow:hidden; border:1px solid var(--line); }
  .pfill { position:absolute; left:0; top:0; bottom:0; width:0%;
           background:linear-gradient(90deg,var(--accent),var(--accent-2));
           border-radius:15px; transition:width .35s ease; }
  .pfill::after {
    content:""; position:absolute; inset:0; border-radius:15px;
    background:linear-gradient(90deg,rgba(255,255,255,0) 0%,rgba(255,255,255,.35) 50%,rgba(255,255,255,0) 100%);
    background-size:200% 100%; animation:sheen 1.4s linear infinite;
  }
  @keyframes sheen { from{background-position:200% 0;} to{background-position:-200% 0;} }
  .pnum { position:absolute; width:100%; text-align:center; line-height:30px;
          font-size:13px; font-weight:700; color:var(--ink);
          font-variant-numeric:tabular-nums; }
  .pmsg { margin-top:10px; font-size:13px; color:var(--ink-soft); min-height:18px; }
  .pmsg .elapsed { color:var(--ink-faint); }

  /* --- Status ---------------------------------------------------------- */
  #status { min-height:0; font-size:14px; }
  #status:not(:empty) { margin-top:14px; padding:12px 14px; border-radius:10px; }
  #status.err { color:var(--err); background:rgba(192,57,43,.07);
                border:1px solid rgba(192,57,43,.2); }
  #status.ok  { color:var(--ok); background:rgba(43,122,75,.08);
                border:1px solid rgba(43,122,75,.2); }

  .foot { text-align:center; margin-top:34px; color:var(--ink-faint);
          font-size:12.5px; }

  @media (max-width:520px) {
    main { padding:36px 16px 72px; }
    .logo { font-size:9px; }
    .row { gap:14px; }
  }
  @media (prefers-reduced-motion:reduce) {
    .pfill::after { animation:none; }
    * { transition:none !important; }
  }
</style>
</head>
<body>
<main>
  <header class="masthead">
    <pre class="logo" aria-hidden="true">██╗  ██╗███████╗
██║  ██║██╔════╝
███████║███████╗
██╔══██║╚════██║
██║  ██║███████║
╚═╝  ╚═╝╚══════╝</pre>
    <h1>Handscrybe</h1>
    <p class="sub">Turn a typed document into handwriting &mdash;
       every letter aligned, the page layout kept intact.</p>
  </header>

  <form id="form">
    <div class="card">
      <label class="field" for="document">Document
        <span class="hint">PDF, DOCX, or TXT</span></label>
      <label class="drop" id="drop-document" for="document">
        <input type="file" id="document" name="document"
               accept=".pdf,.docx,.txt" required>
        <span class="drop-icon" aria-hidden="true">&#128196;</span>
        <span class="drop-title" data-empty="Drop a document here or click to browse">
          Drop a document here or click to browse</span>
        <span class="drop-note">PDF &middot; DOCX &middot; TXT</span>
      </label>
    </div>

    <div class="card">
      <label class="field" for="sample">Your handwriting
        <span class="hint">optional</span></label>
      <label class="drop" id="drop-sample" for="sample">
        <input type="file" id="sample" name="sample"
               accept="image/png,image/jpeg,image/bmp,image/tiff,image/webp">
        <span class="drop-icon" aria-hidden="true">&#9997;</span>
        <span class="drop-title" data-empty="Drop a photo of your sample sheet">
          Drop a photo of your sample sheet</span>
        <span class="drop-note">or click to browse &middot; PNG, JPG&hellip;</span>
      </label>
      <p class="hint sheet-hint">
        Write one sheet: <code>A&hellip;Z</code>, then <code>a&hellip;z</code>,
        then <code>0&hellip;9</code>, one row each, plus an optional row of
        punctuation <code>.,:;'&quot;!?()-/</code>. Leave empty to use the
        built-in hand &mdash; any letter you skip falls back to it automatically.</p>
    </div>

    <div class="card">
      <div class="row">
        <div>
          <label class="field" for="ink">Ink color</label>
          <select id="ink" name="ink">
            <option value="original">Match the document</option>
            <option value="#1a1a6e">Blue-black pen</option>
            <option value="#111111">Black pen</option>
            <option value="#1c3fa8">Blue pen</option>
          </select>
        </div>
        <div>
          <label class="field" for="mode">Layout mode</label>
          <select id="mode" name="mode">
            <option value="fit">Fit &mdash; keep pages identical</option>
            <option value="reflow">Reflow &mdash; rewrap lines</option>
          </select>
        </div>
      </div>
    </div>

    <div class="card">
      <label class="field" for="format">Deliver as
        <span class="hint">PDF &amp; DOCX carry the handwriting; TXT &amp; MD deliver the text</span>
      </label>
      <select id="format" name="format">
        <option value="pdf">PDF &mdash; handwriting, page-for-page</option>
        <option value="docx">DOCX &mdash; handwriting pages in a Word file</option>
        <option value="txt">TXT &mdash; plain text content</option>
        <option value="md">Markdown &mdash; text content with structure</option>
      </select>
    </div>

    <button type="submit" id="go">Convert to handwriting</button>

    <div id="progress">
      <div class="pbar"><div class="pfill" id="pfill"></div>
        <div class="pnum" id="pnum">0%</div></div>
      <div class="pmsg" id="pmsg">Starting&hellip;</div>
    </div>
    <div id="status"></div>
  </form>

  <footer class="foot">Runs entirely on your machine &middot; nothing is uploaded to the cloud.</footer>
</main>

<script>
const form = document.getElementById('form');
const status = document.getElementById('status');
const go = document.getElementById('go');
const progress = document.getElementById('progress');
const pfill = document.getElementById('pfill');
const pnum = document.getElementById('pnum');
const pmsg = document.getElementById('pmsg');

// --- Dropzone wiring -------------------------------------------------------
// Each dropzone is a <label> wrapping a hidden file <input>, so a click already
// opens the picker natively. We add: (1) a filename readout that swaps in once a
// file is chosen, (2) drag-over highlighting, and (3) drop-to-assign so a file
// dragged from the desktop lands on the underlying input. The input's `name`
// still drives the upload, so the backend contract is unchanged.
function wireDrop(id) {
  const zone = document.getElementById('drop-' + id);
  const input = document.getElementById(id);
  const titleEl = zone.querySelector('.drop-title');
  const emptyText = titleEl.getAttribute('data-empty');

  function refresh() {
    const f = input.files && input.files[0];
    if (f) {
      zone.classList.add('filled');
      titleEl.textContent = f.name;
    } else {
      zone.classList.remove('filled');
      titleEl.textContent = emptyText;
    }
  }
  input.addEventListener('change', refresh);

  ['dragenter', 'dragover'].forEach((ev) =>
    zone.addEventListener(ev, (e) => {
      e.preventDefault();
      zone.classList.add('drag');
    })
  );
  ['dragleave', 'dragend', 'drop'].forEach((ev) =>
    zone.addEventListener(ev, () => zone.classList.remove('drag'))
  );
  zone.addEventListener('drop', (e) => {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      refresh();
    }
  });
}
wireDrop('document');
wireDrop('sample');

// --- Progress + elapsed clock ---------------------------------------------
let startedAt = 0;
let clockTimer = null;

function elapsedText() {
  const s = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
  const m = Math.floor(s / 60);
  return m > 0 ? m + 'm ' + String(s % 60).padStart(2, '0') + 's' : s + 's';
}

function setBar(pct, msg) {
  pfill.style.width = pct + '%';
  pnum.textContent = pct + '%';
  const clock = startedAt
    ? ' <span class="elapsed">&middot; ' + elapsedText() + '</span>' : '';
  if (msg !== undefined && msg !== null) {
    pmsg.innerHTML = msg + clock;
  }
}

function stopClock() {
  if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
}

function setStatus(kind, text) {
  status.className = kind;
  status.textContent = text;
}

function fail(msg) {
  stopClock();
  progress.classList.remove('show');
  setStatus('err', msg);
  go.disabled = false;
}

// Poll /progress until the job finishes, then download from /result. The bar
// reflects the pipeline's real stages (normalize -> parse -> fit -> per-page
// render -> save), so the percentage tracks actual work, not a timer.
function poll(jobId) {
  fetch('/progress/' + jobId).then((r) => r.json()).then((s) => {
    if (s.error) { fail(s.error); return; }
    setBar(s.percent || 0, s.message || '');
    if (s.status === 'running') {
      setTimeout(() => poll(jobId), 400);
      return;
    }
    if (s.status === 'error') { fail(s.error || 'Conversion failed.'); return; }
    // status === 'done' — fetch the file.
    setBar(100, 'Done');
    download(jobId, s.coverage);
  }).catch((err) => fail('Lost contact with the server: ' + err));
}

function download(jobId, cov) {
  fetch('/result/' + jobId).then((resp) => {
    if (!resp.ok) {
      return resp.json().then((j) => fail(j.error || 'Download failed.'));
    }
    let fname = 'handwriting.' + (form.querySelector('#format').value || 'pdf');
    const cd = resp.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    if (m) fname = m[1];
    const hcov = resp.headers.get('X-Glyph-Coverage') || cov;
    resp.blob().then((blob) => {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = fname; a.click();
      URL.revokeObjectURL(url);
      stopClock();
      progress.classList.remove('show');
      setStatus('ok', 'Done. Downloaded ' + fname
        + (hcov ? ' \\u2014 used your handwriting for ' + hcov + ' characters.' : '.'));
      go.disabled = false;
    });
  }).catch((err) => fail('Download error: ' + err));
}

form.addEventListener('submit', (e) => {
  e.preventDefault();
  setStatus('', '');
  go.disabled = true;
  progress.classList.add('show');
  startedAt = Date.now();
  stopClock();
  clockTimer = setInterval(() => setBar(
    parseInt(pnum.textContent, 10) || 0, null), 1000);
  setBar(0, 'Uploading\\u2026');

  fetch('/convert', { method:'POST', body:new FormData(form) })
    .then((resp) => resp.json().then((body) => ({ ok: resp.ok, body })))
    .then(({ ok, body }) => {
      if (!ok) { fail(body.error || 'Conversion failed.'); return; }
      poll(body.job_id);
    })
    .catch((err) => fail('Network error: ' + err));
});
</script>
</body>
</html>
"""


def _find_free_port(host: str, preferred: int) -> int:
    """Return the preferred port if it's free, otherwise an OS-assigned one.

    Running `handscrybe` twice, or having another service on :5000, shouldn't be a
    hard error — we transparently fall back to a free port so the web app always
    comes up."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def main() -> int:
    """Run the local web server. Host/port overridable via env for flexibility.

    Binds to localhost only (never 0.0.0.0) — this is a personal tool meant to
    run on the user's own machine, so it is intentionally NOT exposed to the
    network and has no authentication. Opens the browser automatically for a
    zero-friction start."""
    host = os.environ.get("HANDSCRYBE_HOST", "127.0.0.1")
    preferred = int(os.environ.get("HANDSCRYBE_PORT", "5000"))
    port = _find_free_port(host, preferred)
    url = f"http://{host}:{port}"
    app = create_app()

    print()
    print(f"  Handscrybe web app is running at:  {url}")
    if port != preferred:
        print(f"  (port {preferred} was busy, so I picked {port} instead)")
    print("  Opening your browser… press Ctrl+C here to stop the server.")
    print()

    # Open the browser shortly after the server starts accepting connections.
    # A tiny delay avoids racing the bind; failures to open are non-fatal (the
    # URL is printed above regardless).
    def _open() -> None:
        import time
        import webbrowser

        time.sleep(1.0)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if os.environ.get("HANDSCRYBE_NO_BROWSER") != "1":
        import threading

        threading.Thread(target=_open, daemon=True).start()

    try:
        # threaded=True is essential: conversions run in a background thread and
        # the browser polls /progress concurrently. The default single-threaded
        # dev server would block those polls until the whole conversion finished,
        # defeating the live progress bar.
        app.run(host=host, port=port, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\n  Server stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
