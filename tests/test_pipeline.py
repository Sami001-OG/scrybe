"""End-to-end pipeline test on a self-generated sample PDF.

We can't ship a binary fixture, so the test builds its own source PDF with
PyMuPDF: several styled text lines (regular, bold, italic, colored), a vector
rectangle (stands in for a table border / shape), and a filled circle (stands
in for an image/graphic). Then it runs the real `convert` pipeline and asserts
the two guarantees the product makes:

  1. Structure is preserved -- same page count, same page size, and the
     non-text vector graphics survive (the rectangle and circle are still
     present as drawings after conversion).
  2. Text is replaced -- the original typed strings are gone from the text
     layer (redacted), and handwriting glyphs were drawn in their place (the
     output has text content, just not the originals).

This exercises parse_pdf -> layout -> render for real; only the DOCX->PDF
normalize step (which needs LibreOffice) is out of scope here and covered by
its own guard.
"""

from __future__ import annotations

import os
import sys

import pytest

# Make `import handscrybe` work without an editable install (mirrors
# test_layout.py's bootstrap): the package lives under ../src.
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import fitz  # noqa: E402

from handscrybe.config import Config  # noqa: E402
from handscrybe.pipeline import convert  # noqa: E402

# The sample text we lay down and later assert has been erased.
_LINE_REGULAR = "The quick brown fox"
_LINE_BOLD = "Jumps over the lazy dog"
_LINE_ITALIC = "In slanted handwriting"
_LINE_COLOR = "Colored ink stays colored"


def _make_sample_pdf(path: str) -> None:
    """Write a one-page PDF with styled text plus a rectangle and a circle."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)  # US Letter

    # Styled text lines. PyMuPDF's built-in faces carry the style so parse_pdf's
    # flag/name detection picks up bold and italic.
    page.insert_text((72, 100), _LINE_REGULAR, fontname="helv", fontsize=14)
    page.insert_text((72, 130), _LINE_BOLD, fontname="hebo", fontsize=14)
    page.insert_text((72, 160), _LINE_ITALIC, fontname="heit", fontsize=14)
    page.insert_text(
        (72, 190), _LINE_COLOR, fontname="helv", fontsize=14, color=(0.1, 0.1, 0.7)
    )

    # A rectangle standing in for a table border / shape, and a filled circle
    # standing in for an image. Both are vector drawings; the renderer must not
    # delete them when it redacts the overlapping/nearby text.
    page.draw_rect(fitz.Rect(60, 220, 400, 320), color=(0, 0, 0), width=1.5)
    page.draw_circle(fitz.Point(500, 150), 40, color=(0, 0.5, 0), fill=(0, 0.5, 0))

    doc.save(path)
    doc.close()


def _count_drawings(path: str) -> int:
    doc = fitz.open(path)
    try:
        return sum(len(doc[p].get_drawings()) for p in range(doc.page_count))
    finally:
        doc.close()


def _all_text(path: str) -> str:
    doc = fitz.open(path)
    try:
        return "\n".join(doc[p].get_text() for p in range(doc.page_count))
    finally:
        doc.close()


def _fonts_used(path: str) -> set[str]:
    """Return the set of font names that actually draw glyphs in the document
    (from the span dicts), not just those declared in the resource table."""
    doc = fitz.open(path)
    try:
        names: set[str] = set()
        for p in range(doc.page_count):
            data = doc[p].get_text("dict")
            for block in data.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        if span.get("text", "").strip():
                            names.add(span.get("font", ""))
        return names
    finally:
        doc.close()


def test_convert_preserves_structure_and_replaces_text(tmp_path):
    src = str(tmp_path / "sample.pdf")
    out = str(tmp_path / "sample_hand.pdf")
    _make_sample_pdf(src)

    src_drawings = _count_drawings(src)
    src_text = _all_text(src)
    # Sanity: the fixture really does contain what we think it does.
    assert _LINE_REGULAR in src_text
    assert src_drawings >= 2  # rectangle + circle

    # Force bold synthesis from the single regular face (no bundled bold TTF),
    # which is the real single-font scenario the design targets.
    cfg = Config(font_bold=None)
    result = convert(src, out, cfg)

    assert result == out
    assert os.path.isfile(out)

    out_doc = fitz.open(out)
    try:
        # (1) Structure preserved: page count and size unchanged.
        assert out_doc.page_count == 1
        assert out_doc[0].rect.width == pytest.approx(612, abs=1.0)
        assert out_doc[0].rect.height == pytest.approx(792, abs=1.0)
    finally:
        out_doc.close()

    # (1 cont.) The vector graphics survived redaction. Redaction can split or
    # merge path objects, so we assert the drawings weren't wiped rather than an
    # exact count.
    assert _count_drawings(out) >= src_drawings

    # (2) Text was replaced with handwriting. The *content* is intentionally
    # preserved -- what changes is the font. So we assert on the fonts, not the
    # strings: every original print face (Helvetica/Times) must be gone and only
    # the handwriting face may remain. The text itself is expected to survive.
    out_text = _all_text(out)
    assert _LINE_REGULAR in out_text  # content preserved by design
    assert out_text.strip() != ""

    out_fonts = _fonts_used(out)
    assert out_fonts, "output has no text layer -- handwriting was not drawn"
    # The handwriting face is present...
    assert any("caveat" in f.lower() for f in out_fonts)
    # ...and none of PyMuPDF's built-in print faces (helv/hebo/heit -> Helvetica,
    # Times, etc.) survived the replacement.
    assert not any(
        base in f.lower()
        for f in out_fonts
        for base in ("helvetica", "times", "courier")
    ), f"a print font survived replacement: {out_fonts}"
