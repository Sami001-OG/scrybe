"""Intermediate layout model.

Both PDF and (normalized) DOCX inputs are reduced to this model, expressed in
PDF coordinate space (points, origin at top-left, y increasing downward — the
convention PyMuPDF uses for its "dict" extraction and drawing APIs).

The model is deliberately span-centric: a `Span` is the atomic unit of text
that shares a single style and sits on one baseline. Everything the layout and
render stages need is captured here so those stages never touch a PDF library
directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class FontStyle(Enum):
    """Style to synthesize for a span. A single handwriting TTF is used as the
    base; bold/italic are produced by stroke widening and shear at render time,
    so we only need to record the intent here."""

    REGULAR = "regular"
    BOLD = "bold"
    ITALIC = "italic"
    BOLD_ITALIC = "bold_italic"

    @classmethod
    def from_flags(cls, bold: bool, italic: bool) -> "FontStyle":
        if bold and italic:
            return cls.BOLD_ITALIC
        if bold:
            return cls.BOLD
        if italic:
            return cls.ITALIC
        return cls.REGULAR

    @property
    def bold(self) -> bool:
        return self in (FontStyle.BOLD, FontStyle.BOLD_ITALIC)

    @property
    def italic(self) -> bool:
        return self in (FontStyle.ITALIC, FontStyle.BOLD_ITALIC)


@dataclass
class Rect:
    """Axis-aligned rectangle in PDF points (top-left origin)."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class Span:
    """A run of text on a single baseline with one style.

    `origin` is the baseline start point (x, y) as reported by the PDF text
    extractor — this is where glyph drawing begins. `bbox` is the span's
    bounding box, used for redaction and as the width budget for fitting.
    `color` is an (r, g, b) triple in 0..1.
    """

    text: str
    origin: tuple[float, float]
    bbox: Rect
    size: float
    style: FontStyle
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    # Ascender/descender as fractions of font size, from the source font, so
    # the layout engine can reason about vertical fit if it ever needs to.
    ascender: float = 0.8
    descender: float = -0.2

    @property
    def width(self) -> float:
        return self.bbox.width


@dataclass
class Line:
    """One visual line: spans sharing a baseline. Kept as a group because the
    fitting engine operates per-line (its whole reason for existing is to keep
    each original line within its original horizontal extent)."""

    spans: list[Span] = field(default_factory=list)

    @property
    def bbox(self) -> Rect:
        xs0 = min(s.bbox.x0 for s in self.spans)
        ys0 = min(s.bbox.y0 for s in self.spans)
        xs1 = max(s.bbox.x1 for s in self.spans)
        ys1 = max(s.bbox.y1 for s in self.spans)
        return Rect(xs0, ys0, xs1, ys1)


@dataclass
class Page:
    """A single page. `width`/`height` are the MediaBox dimensions in points.
    `number` is 0-based. Lines carry the text; non-text content (images,
    vectors, tables, borders) is left untouched in the source PDF and never
    enters this model — that is what preserves it."""

    number: int
    width: float
    height: float
    lines: list[Line] = field(default_factory=list)


@dataclass
class Document:
    """The full intermediate model. `source_pdf_path` points at the PDF that
    the render stage will edit in place (a copy) — for DOCX inputs this is the
    LibreOffice-normalized PDF, so coordinates always match a real PDF."""

    pages: list[Page] = field(default_factory=list)
    source_pdf_path: str | None = None


# --- Layout -> Render contract -------------------------------------------
# The layout engine consumes `Line`s and emits `PlacedSpan`s: fully resolved
# draw instructions in PDF coordinates. Render.py must be able to draw a
# PlacedSpan with no further layout reasoning — every distortion decision
# (scale, size reduction, x/y placement) is already baked in here. This is the
# single seam between the two stages; keep it dumb and complete.


@dataclass
class PlacedSpan:
    """A ready-to-draw handwriting span.

    `text`, `style`, `color` come straight from the source span. The geometry
    is what layout computed:

    - `origin` (x, y): baseline start where glyph drawing begins, in PDF points
      (top-left origin, y down — matching parse_pdf and PyMuPDF drawing).
    - `size`: final font size to draw at, after any FIT-mode size reduction.
    - `hscale`: horizontal scale factor applied about `origin.x` (1.0 = none).
      render.py realizes this via a horizontal-scaling matrix so a squeezed
      line stays within its original width budget.
    - `char_spacing`: extra points added between characters (letter-spacing);
      may be negative to tighten. Applied on top of the font's advances.
    - `synth_bold` / `synth_italic`: whether render.py must synthesize the
      style (stroke widening / shear) because no dedicated face was used. These
      mirror ResolvedFont so layout and render never disagree on what's drawn.

    `redact_bbox` is the original span box the renderer must erase before
    drawing, so the printed text underneath disappears while surrounding
    images/graphics survive."""

    text: str
    origin: tuple[float, float]
    size: float
    style: FontStyle
    color: tuple[float, float, float]
    redact_bbox: Rect
    hscale: float = 1.0
    char_spacing: float = 0.0
    synth_bold: bool = False
    synth_italic: bool = False

    @property
    def x(self) -> float:
        """Baseline start x. Convenience accessor over `origin[0]`."""
        return self.origin[0]

    @property
    def y(self) -> float:
        """Baseline y. Convenience accessor over `origin[1]`."""
        return self.origin[1]


@dataclass
class PlacedPage:
    """A page's worth of placed spans plus its geometry, so render.py can map
    each page 1:1 onto the source PDF page by `number`."""

    number: int
    width: float
    height: float
    spans: list[PlacedSpan] = field(default_factory=list)
