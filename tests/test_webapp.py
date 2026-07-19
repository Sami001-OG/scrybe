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


def test_index_loads(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"handscrybe" in r.data


def test_convert_txt_ttf_only(client):
    r = client.post(
        "/convert",
        data={
            "document": (io.BytesIO(_txt_doc()), "hello.txt"),
            "ink": "original",
            "mode": "fit",
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 200, r.data[:300]
    assert r.data[:4] == b"%PDF"  # a real PDF came back
    # No sample supplied -> no coverage header.
    assert "X-Glyph-Coverage" not in r.headers


def test_convert_with_handwriting_sample_reports_coverage(client):
    r = client.post(
        "/convert",
        data={
            "document": (io.BytesIO(_txt_doc()), "hello.txt"),
            "sample": (io.BytesIO(_sample_sheet_png()), "sheet.png"),
            "ink": "#1a1a6e",
            "mode": "fit",
        },
        content_type="multipart/form-data",
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
