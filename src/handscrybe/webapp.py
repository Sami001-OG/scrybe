"""Local Flask web UI for handscrybe.

A single-page tool: the user uploads a document (PDF / DOCX / TXT) and,
optionally, a photo of their handwriting sample sheet (A-Z, a-z, 0-9). The app
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
import uuid

from flask import Flask, Response, jsonify, request, send_file

from .config import Config
from .pipeline import convert

# Accepted document extensions (mirrors normalize.detect_format's support).
_DOC_EXTS = {".pdf", ".docx", ".txt"}
# Accepted sample-sheet image extensions.
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}

# 25 MB per upload. Generous for documents/photos, small enough to bound memory.
_MAX_CONTENT_LENGTH = 25 * 1024 * 1024


def _ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def create_app() -> Flask:
    """Build and return the Flask application (factory pattern so tests can get
    an isolated instance with the test client)."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = _MAX_CONTENT_LENGTH

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

        # Each request works in its own temp dir; cleaned up after send.
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
            # bad image, before spending time on document conversion.
            try:
                from .glyphs import GlyphSet

                gs = GlyphSet.from_sheet(sample_path)
                coverage = list(gs.coverage())  # [found, expected]
            except Exception as exc:  # noqa: BLE001 - surface any imaging error
                _safe_rmtree(work)
                return jsonify(error=f"Could not read handwriting sample: {exc}"), 400

        out_path = os.path.join(work, "handwriting." + out_fmt.value)
        try:
            convert(doc_path, out_path, cfg)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            _safe_rmtree(work)
            return jsonify(error=str(exc)), 400
        except Exception as exc:  # noqa: BLE001 - last-resort guard for the UI
            _safe_rmtree(work)
            return jsonify(error=f"Conversion failed: {exc}"), 500

        # Stream the result back, then remove the temp dir. Read the bytes into
        # memory first so we can delete the file before returning (Windows won't
        # remove a file that's still open).
        with open(out_path, "rb") as fh:
            data = fh.read()
        _safe_rmtree(work)

        mimetypes = {
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "txt": "text/plain; charset=utf-8",
            "md": "text/markdown; charset=utf-8",
        }
        resp = Response(data, mimetype=mimetypes[out_fmt.value])
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="handwriting.{out_fmt.value}"'
        )
        if coverage is not None:
            # Non-standard header the front-end reads to show "X/Y glyphs used".
            resp.headers["X-Glyph-Coverage"] = f"{coverage[0]}/{coverage[1]}"
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
  :root { --bg:#faf9f6; --ink:#1a1a2e; --accent:#3b5bdb; --line:#e3e0d8; }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
         background:var(--bg); color:var(--ink); }
  main { max-width:640px; margin:0 auto; padding:40px 20px 80px; }
  h1 { font-size:28px; margin:0 0 4px; }
  p.sub { margin:0 0 28px; color:#666; }
  .card { background:#fff; border:1px solid var(--line); border-radius:12px;
          padding:22px; margin-bottom:18px; }
  label.field { display:block; font-weight:600; margin-bottom:8px; }
  .hint { font-weight:400; color:#888; font-size:13px; }
  input[type=file] { width:100%; padding:10px; border:1px dashed #bbb;
                     border-radius:8px; background:#fafafa; }
  .row { display:flex; gap:16px; flex-wrap:wrap; }
  .row > div { flex:1 1 200px; }
  select, input[type=text] { width:100%; padding:9px; border:1px solid #ccc;
                             border-radius:8px; font-size:15px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px;
           padding:13px 22px; font-size:16px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  #status { margin-top:16px; min-height:22px; font-size:14px; }
  .err { color:#c92a2a; } .ok { color:#2b8a3e; }
  .spin { display:inline-block; width:14px; height:14px; border:2px solid #ccc;
          border-top-color:var(--accent); border-radius:50%;
          animation:spin .7s linear infinite; vertical-align:-2px; margin-right:6px; }
  @keyframes spin { to { transform:rotate(360deg); } }
  code { background:#f0eee8; padding:1px 5px; border-radius:4px; font-size:13px; }
</style>
</head>
<body>
<main>
  <h1>handscrybe</h1>
  <p class="sub">Turn a document into handwriting while keeping its layout.</p>

  <form id="form">
    <div class="card">
      <label class="field" for="document">Document
        <span class="hint">PDF, DOCX, or TXT</span></label>
      <input type="file" id="document" name="document"
             accept=".pdf,.docx,.txt" required>
    </div>

    <div class="card">
      <label class="field" for="sample">Your handwriting sample
        <span class="hint">optional &mdash; a photo of one sheet written
        <code>A&hellip;Z</code> then <code>a&hellip;z</code> then
        <code>0&hellip;9</code>, one row each</span></label>
      <input type="file" id="sample" name="sample"
             accept="image/png,image/jpeg,image/bmp,image/tiff,image/webp">
      <p class="hint" style="margin:8px 0 0">
        Leave empty to use the built-in handwriting font. Characters you don't
        write (punctuation, etc.) fall back to it automatically.</p>
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
            <option value="fit">Fit (keep pages identical)</option>
            <option value="reflow">Reflow (rewrap lines)</option>
          </select>
        </div>
      </div>
    </div>

    <div class="card">
      <label class="field" for="format">Deliver as
        <span class="hint">PDF &amp; DOCX carry the handwriting; TXT &amp; MD deliver the text content</span>
      </label>
      <select id="format" name="format">
        <option value="pdf">PDF — handwriting, page-for-page</option>
        <option value="docx">DOCX — handwriting pages in a Word file</option>
        <option value="txt">TXT — plain text content</option>
        <option value="md">Markdown — text content with structure</option>
      </select>
    </div>

    <button type="submit" id="go">Convert to handwriting</button>
    <div id="status"></div>
  </form>
</main>

<script>
const form = document.getElementById('form');
const status = document.getElementById('status');
const go = document.getElementById('go');

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  status.className = '';
  status.innerHTML = '<span class="spin"></span>Converting&hellip;';
  go.disabled = true;

  try {
    const resp = await fetch('/convert', { method:'POST', body:new FormData(form) });
    if (!resp.ok) {
      let msg = 'Conversion failed.';
      try { msg = (await resp.json()).error || msg; } catch (_) {}
      status.className = 'err';
      status.textContent = msg;
      return;
    }
    const cov = resp.headers.get('X-Glyph-Coverage');
    // Derive the download filename from the server's Content-Disposition so the
    // extension always matches the format actually produced.
    let fname = 'handwriting.' + (form.querySelector('#format').value || 'pdf');
    const cd = resp.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename="([^"]+)"/);
    if (m) fname = m[1];
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = fname; a.click();
    URL.revokeObjectURL(url);
    status.className = 'ok';
    status.textContent = 'Done. Downloaded ' + fname
      + (cov ? ' \\u2014 used your handwriting for ' + cov + ' characters.' : '.');
  } catch (err) {
    status.className = 'err';
    status.textContent = 'Network or server error: ' + err;
  } finally {
    go.disabled = false;
  }
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
        app.run(host=host, port=port, debug=False)
    except KeyboardInterrupt:
        print("\n  Server stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
