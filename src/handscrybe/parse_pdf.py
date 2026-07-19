"""Extract a `Document` model from a PDF using PyMuPDF.

PyMuPDF's ``page.get_text("dict")`` already returns the structure we need:
blocks -> lines -> spans, each span carrying text, bbox, baseline origin,
font size, font name, color and style flags. We translate that directly into
our span-centric model. Non-text content (images, drawings, table borders) is
intentionally ignored here — it stays in the source PDF untouched, which is
exactly what preserves it.
"""

from __future__ import annotations

import fitz  # PyMuPDF

from .model import Document, FontStyle, Line, Page, Rect, Span

# PyMuPDF span flag bits (from the "dict" extractor).
_FLAG_ITALIC = 1 << 1
_FLAG_BOLD = 1 << 4


def _int_color_to_rgb(color: int) -> tuple[float, float, float]:
    """PyMuPDF encodes span color as a packed sRGB int (0xRRGGBB)."""
    r = (color >> 16) & 0xFF
    g = (color >> 8) & 0xFF
    b = color & 0xFF
    return (r / 255.0, g / 255.0, b / 255.0)


def _style_from_span(span: dict) -> FontStyle:
    """Derive style from the flag bits, with a font-name fallback since some
    producers set the name (e.g. 'Arial-BoldItalic') but not the flags."""
    flags = span.get("flags", 0)
    bold = bool(flags & _FLAG_BOLD)
    italic = bool(flags & _FLAG_ITALIC)

    name = span.get("font", "").lower()
    if "bold" in name or "black" in name or "heavy" in name or "semibold" in name:
        bold = True
    if "italic" in name or "oblique" in name:
        italic = True

    return FontStyle.from_flags(bold, italic)


def parse_pdf(path: str) -> Document:
    doc = fitz.open(path)
    try:
        model = Document(source_pdf_path=path)
        for pno in range(doc.page_count):
            page = doc[pno]
            rect = page.rect
            mpage = Page(number=pno, width=rect.width, height=rect.height)

            data = page.get_text("dict")
            for block in data.get("blocks", []):
                # type 0 == text block; type 1 == image (skip, stays in PDF).
                if block.get("type", 0) != 0:
                    continue
                for line in block.get("lines", []):
                    mline = Line()
                    for span in line.get("spans", []):
                        text = span.get("text", "")
                        if text == "":
                            continue
                        bbox = span["bbox"]  # (x0, y0, x1, y1)
                        origin = span.get("origin", (bbox[0], bbox[3]))
                        mspan = Span(
                            text=text,
                            origin=(origin[0], origin[1]),
                            bbox=Rect(*bbox),
                            size=span.get("size", bbox[3] - bbox[1]),
                            style=_style_from_span(span),
                            color=_int_color_to_rgb(span.get("color", 0)),
                            ascender=span.get("ascender", 0.8),
                            descender=span.get("descender", -0.2),
                        )
                        mline.spans.append(mspan)
                    if mline.spans:
                        mpage.lines.append(mline)
            model.pages.append(mpage)
        return model
    finally:
        doc.close()
