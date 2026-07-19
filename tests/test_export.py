"""Output-format delivery tests.

The pipeline always renders a handwriting PDF first, then `export.deliver`
turns it into the format the user asked for. These tests cover all four:

    PDF  -> the handwriting PDF (copied to the target path).
    DOCX -> a .docx whose pages are full-page handwriting images.
    TXT  -> the document's extracted text content.
    MD   -> the same text with light Markdown structure.

We drive the whole thing through `convert` on a self-generated source PDF so
the test needs no external fixture. DOCX here is BUILT (rasterize pages into a
python-docx document), so unlike DOCX *input* it does NOT need LibreOffice and
runs everywhere.
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import fitz  # noqa: E402

from doc_to_hand.config import Config, OutputFormat  # noqa: E402
from doc_to_hand.pipeline import convert  # noqa: E402

_TEXT = "Output format test 123"


def _make_source_pdf(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), _TEXT, fontname="helv", fontsize=14)
    doc.save(path)
    doc.close()


def _convert(tmp_path, ext: str, fmt: OutputFormat | None):
    src = str(tmp_path / "src.pdf")
    out = str(tmp_path / f"out.{ext}")
    _make_source_pdf(src)
    cfg = Config(font_bold=None, output_format=fmt)
    return convert(src, out, cfg), out


def test_pdf_output(tmp_path):
    result, out = _convert(tmp_path, "pdf", OutputFormat.PDF)
    assert result == out
    assert os.path.isfile(out)
    # A real, single-page PDF whose text renders in the handwriting font.
    doc = fitz.open(out)
    try:
        assert doc.page_count == 1
        fonts = {
            span.get("font", "")
            for p in range(doc.page_count)
            for block in doc[p].get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            if span.get("text", "").strip()
        }
        assert any("caveat" in f.lower() for f in fonts)
    finally:
        doc.close()


def test_docx_output_embeds_page_images(tmp_path):
    result, out = _convert(tmp_path, "docx", OutputFormat.DOCX)
    assert os.path.isfile(out)
    # Must be a valid, openable .docx that contains one image per source page.
    from docx import Document as DocxDocument
    from docx.document import Document as _Doc  # noqa: F401

    d = DocxDocument(out)
    image_parts = [p for p in d.part.package.iter_parts() if "image" in p.content_type]
    assert image_parts, "DOCX has no embedded page image"


def test_txt_output_has_content(tmp_path):
    result, out = _convert(tmp_path, "txt", OutputFormat.TXT)
    assert os.path.isfile(out)
    with open(out, encoding="utf-8") as fh:
        content = fh.read()
    # TXT delivers the document's text content (handwriting can't live in text).
    assert _TEXT in content


def test_md_output_has_content(tmp_path):
    result, out = _convert(tmp_path, "md", OutputFormat.MD)
    assert os.path.isfile(out)
    with open(out, encoding="utf-8") as fh:
        content = fh.read()
    assert _TEXT in content


def test_format_inferred_from_extension(tmp_path):
    # output_format=None => infer from the output path's extension.
    _result, out = _convert(tmp_path, "txt", None)
    with open(out, encoding="utf-8") as fh:
        assert _TEXT in fh.read()


def test_unknown_extension_defaults_to_pdf(tmp_path):
    src = str(tmp_path / "src.pdf")
    out = str(tmp_path / "out.xyz")
    _make_source_pdf(src)
    cfg = Config(font_bold=None, output_format=None)
    convert(src, out, cfg)
    # Unknown extension with no explicit format => PDF bytes.
    with open(out, "rb") as fh:
        assert fh.read(5) == b"%PDF-"
