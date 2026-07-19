"""Tests for the Flask web UI.

These exercise the real routes via Flask's test client (no live socket needed):
the index page renders, a document converts through the exact same pipeline the
CLI uses, an uploaded handwriting sample is segmented and its coverage reported,
and malformed requests are rejected with 4xx rather than crashing.

A synthetic sample sheet is generated in-process (same approach as
test_glyphs.py) so the tests need no binary fixtures.
"""

from __future__ import annotations

import io
import os
import sys

import pytest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import fitz  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from handscrybe.webapp import create_app  # noqa: E402

_ROWS = ["ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz", "0123456789"]


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def _sample_sheet_png() -> bytes:
    """Render an A-Z / a-z / 0-9 sheet and return PNG bytes."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=300)
    for ri, row in enumerate(_ROWS):
        x = 40.0
        for ch in row:
            page.insert_text((x, 60 + ri * 70), ch, fontname="helv", fontsize=28)
            x += 28 * 0.6 + 20
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    doc.close()
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _txt_doc() -> bytes:
    return "Hello handwriting world.\nSecond line here.".encode("utf-8")


def _run_conversion(client, data, *, timeout_s: float = 30.0):
    """Drive the full job flow: POST /convert, poll /progress to completion, then
    GET /result. Returns the final Flask response from /result.

    Conversion now runs in a background thread and reports live progress, so a
    test must start the job, wait for it to finish, and then download — exactly
    what the browser does. Progress percentages are asserted to be monotonic and
    to reach 100, which is the whole point of the feature."""
    import time

    start = client.post("/convert", data=data, content_type="multipart/form-data")
    assert start.status_code == 202, start.data[:300]
    job_id = start.get_json()["job_id"]

    last_pct = -1
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = client.get(f"/progress/{job_id}").get_json()
        pct = snap["percent"]
        # Percentage must never go backwards — a monotonic bar is trustworthy.
        assert pct >= last_pct, f"progress went backwards: {last_pct} -> {pct}"
        last_pct = pct
        if snap["status"] == "done":
            assert pct == 100
            break
        if snap["status"] == "error":
            raise AssertionError(f"job errored: {snap.get('error')}")
        time.sleep(0.02)
    else:
        raise AssertionError("conversion did not finish within timeout")

    return client.get(f"/result/{job_id}")


def test_index_loads(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"handscrybe" in r.data


def test_convert_txt_ttf_only(client):
    r = _run_conversion(
        client,
        {
            "document": (io.BytesIO(_txt_doc()), "hello.txt"),
            "ink": "original",
            "mode": "fit",
        },
    )
    assert r.status_code == 200, r.data[:300]
    assert r.data[:4] == b"%PDF"  # a real PDF came back
    # No sample supplied -> no coverage header.
    assert "X-Glyph-Coverage" not in r.headers


def test_convert_with_handwriting_sample_reports_coverage(client):
    # Coverage is reported synchronously in the /convert response (the sheet is
    # segmented up front), and echoed on the final /result download.
    start = client.post(
        "/convert",
        data={
            "document": (io.BytesIO(_txt_doc()), "hello.txt"),
            "sample": (io.BytesIO(_sample_sheet_png()), "sheet.png"),
            "ink": "#1a1a6e",
            "mode": "fit",
        },
        content_type="multipart/form-data",
    )
    assert start.status_code == 202, start.data[:300]
    assert start.get_json()["coverage"] == "62/62"

    r = _run_conversion(
        client,
        {
            "document": (io.BytesIO(_txt_doc()), "hello.txt"),
            "sample": (io.BytesIO(_sample_sheet_png()), "sheet.png"),
            "ink": "#1a1a6e",
            "mode": "fit",
        },
    )
    assert r.status_code == 200, r.data[:300]
    assert r.data[:4] == b"%PDF"
    # The full A-Z/a-z/0-9 sheet should be fully recovered.
    assert r.headers.get("X-Glyph-Coverage") == "62/62"


def test_rejects_unsupported_document(client):
    r = client.post(
        "/convert",
        data={"document": (io.BytesIO(b"nope"), "bad.exe")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
    assert b"Unsupported" in r.data


def test_rejects_missing_document(client):
    r = client.post("/convert", data={}, content_type="multipart/form-data")
    assert r.status_code == 400


def test_rejects_bad_sample_image_type(client):
    r = client.post(
        "/convert",
        data={
            "document": (io.BytesIO(_txt_doc()), "hello.txt"),
            "sample": (io.BytesIO(b"nope"), "sample.exe"),
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 400
    assert b"image type" in r.data


def test_progress_unknown_job_is_404(client):
    r = client.get("/progress/does-not-exist")
    assert r.status_code == 404


def test_result_unknown_job_is_404(client):
    r = client.get("/result/does-not-exist")
    assert r.status_code == 404


def test_result_before_finish_is_conflict(client):
    # Start a job but ask for the result immediately; until it's done, /result
    # should say "not finished" (409) rather than hand back a partial file.
    start = client.post(
        "/convert",
        data={"document": (io.BytesIO(_txt_doc()), "hello.txt")},
        content_type="multipart/form-data",
    )
    assert start.status_code == 202
    job_id = start.get_json()["job_id"]
    r = client.get(f"/result/{job_id}")
    # Either it's still running (409) or it already finished (200) on a fast
    # machine; both are correct, but it must never be a 5xx or partial.
    assert r.status_code in (200, 409)
