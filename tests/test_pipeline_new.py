"""End-to-end tests for the new input/rendering paths:

  1. txt input -> handwriting PDF (normalize builds the PDF via PyMuPDF, no
     LibreOffice needed), and
  2. rendering from a user handwriting-sample image (glyph images composited
     instead of TTF glyphs), tinted to the source text color.

Both build their own fixtures (a .txt file, and a synthetic sample sheet) so the
suite stays self-contained. The image-render assertions check that raster glyph
images were actually placed on the page and that the original print text is
gone, which is the observable contract of the feature.
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import fitz  # noqa: E402

from doc_to_hand.config import Config  # noqa: E402
from doc_to_hand.pipeline import convert  # noqa: E402

# Reuse the sample-sheet generator from the glyph tests to avoid duplication.
from test_glyphs import _make_sample_sheet  # noqa: E402


def _count_placed_images(path: str) -> int:
    """Count image placements across all pages (each composited glyph is one)."""
    doc = fitz.open(path)
    try:
        return sum(len(doc[p].get_images(full=True)) for p in range(doc.page_count))
    finally:
        doc.close()


def _rendered_span_fonts(path: str) -> set[str]:
    doc = fitz.open(path)
    try:
        names: set[str] = set()
        for p in range(doc.page_count):
            for block in doc[p].get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("text", "").strip():
                            names.add(span.get("font", ""))
        return names
    finally:
        doc.close()


def test_txt_input_produces_handwriting_pdf(tmp_path):
    src = str(tmp_path / "note.txt")
    out = str(tmp_path / "note_hand.pdf")
    with open(src, "w", encoding="utf-8") as fh:
        fh.write("Hello from a text file.\nSecond line here.\n")

    cfg = Config(font_bold=None)
    result = convert(src, out, cfg)

    assert result == out
    assert os.path.isfile(out)

    doc = fitz.open(out)
    try:
        assert doc.page_count >= 1
        text = "\n".join(doc[p].get_text() for p in range(doc.page_count))
    finally:
        doc.close()

    # Content preserved; rendered in the handwriting TTF (no glyph image given).
    assert "Hello from a text file." in text
    fonts = _rendered_span_fonts(out)
    assert any("caveat" in f.lower() for f in fonts)


def test_render_from_handwriting_image(tmp_path):
    """A PDF converted with a handwriting-sample image should composite glyph
    images (raster placements) and leave no original print-font text spans."""
    # Source PDF with plain ASCII text fully covered by the sample sheet.
    src = str(tmp_path / "src.pdf")
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 200), "Hello World", fontname="helv", fontsize=24)
    doc.save(src)
    doc.close()

    sheet = str(tmp_path / "hand.png")
    _make_sample_sheet(sheet)

    out = str(tmp_path / "src_hand.pdf")
    cfg = Config(font_bold=None, handwriting_image=sheet)
    convert(src, out, cfg)

    # Glyph images were composited: "HelloWorld" is 10 non-space letters, all
    # present on the sheet, so we expect ~10 image placements (>=8 tolerates any
    # single-char fallback).
    assert _count_placed_images(out) >= 8

    # The original Helvetica text must be gone (redacted); any remaining text
    # spans must not be a built-in print face.
    fonts = _rendered_span_fonts(out)
    assert not any(
        base in f.lower()
        for f in fonts
        for base in ("helvetica", "times", "courier")
    ), f"a print font survived: {fonts}"


def test_render_from_image_falls_back_for_missing_chars(tmp_path):
    """Characters absent from the sample sheet (punctuation) must still render
    via the TTF fallback rather than vanishing."""
    src = str(tmp_path / "src.pdf")
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    # Ampersand and punctuation are NOT on the A-Z/a-z/0-9 sheet.
    page.insert_text((72, 200), "AB & CD!", fontname="helv", fontsize=24)
    doc.save(src)
    doc.close()

    sheet = str(tmp_path / "hand.png")
    _make_sample_sheet(sheet)

    out = str(tmp_path / "src_hand.pdf")
    cfg = Config(font_bold=None, handwriting_image=sheet)
    convert(src, out, cfg)

    doc = fitz.open(out)
    try:
        text = "\n".join(doc[p].get_text() for p in range(doc.page_count))
    finally:
        doc.close()
    # The punctuation that fell back to the TTF should still be in the text layer
    # (glyph-image letters won't be, since they're raster). Presence of '&'/'!'
    # proves the fallback drew them rather than dropping them.
    assert "&" in text
    assert "!" in text
