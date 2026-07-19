"""Configuration for the conversion pipeline.

Defaults are chosen so a bare `convert(in, out)` call produces sane output;
every knob is overridable via the CLI. The values that matter most for
fidelity are the layout caps — they bound how far the fitting engine may
distort handwriting to keep a line inside its original width.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

# The bundled handwriting fonts live INSIDE the package (doc_to_hand/fonts/),
# not at the repo root. Resolving relative to this file means the path is
# correct both in a source checkout and after `pip install` relocates the
# package into site-packages — the previous "../../fonts" scheme broke the
# instant a normal (non-editable) install was done, since the repo root isn't
# copied. `PROJECT_ROOT` is retained for any tooling that wants the package dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = _HERE
FONTS_DIR = os.path.join(_HERE, "fonts")


class LayoutMode(Enum):
    """FIT: never change line count; squeeze each line to its original width
    (scale / letter-spacing / mild size reduction). Guarantees pagination is
    invariant. REFLOW: allow lines to rewrap within the original text block —
    opt-in, may shift content, used when squeezing would be illegible."""

    FIT = "fit"
    REFLOW = "reflow"


class OutputFormat(Enum):
    """What the user wants delivered.

    Handwriting is inherently VISUAL, so the two format families differ in
    kind, not just container:

    * PDF / DOCX are *visual* outputs — they carry the rendered handwriting
      page-for-page (DOCX is produced by converting the handwriting PDF back
      through LibreOffice, so each page becomes a full-page handwriting image).
    * TXT / MD are *text* outputs — plain text can't contain handwriting, so
      these deliver the document's extracted text content instead (MD adds
      light structure: blank-line-separated paragraphs). This is surfaced to
      the user so the distinction is never a surprise.
    """

    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    MD = "md"

    @property
    def is_visual(self) -> bool:
        """True for formats that carry the actual handwriting (PDF/DOCX)."""
        return self in (OutputFormat.PDF, OutputFormat.DOCX)

    @classmethod
    def from_path(cls, path: str) -> "OutputFormat":
        """Infer the desired format from an output path's extension.

        Defaults to PDF for an unknown/absent extension, since PDF is the
        native, highest-fidelity output."""
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        try:
            return cls(ext)
        except ValueError:
            return cls.PDF


@dataclass
class Config:
    # --- Fonts -----------------------------------------------------------
    # Base handwriting font (regular). Bold/italic are synthesized from it
    # unless explicit style faces are supplied.
    font_regular: str = field(default_factory=lambda: os.path.join(FONTS_DIR, "Caveat-Regular.ttf"))
    font_bold: str | None = field(default_factory=lambda: os.path.join(FONTS_DIR, "Caveat-Bold.ttf"))
    font_italic: str | None = None
    font_bold_italic: str | None = None

    # Multiplier applied to every source font size. Handwriting fonts tend to
    # have a smaller visual x-height than print fonts; ~1.0 keeps baselines
    # aligned. Exposed for taste.
    size_scale: float = 1.0

    # --- Layout fitting --------------------------------------------------
    mode: LayoutMode = LayoutMode.FIT

    # A line whose handwriting is wider than the original by up to this factor
    # is squeezed via horizontal scaling. Below 1.0 would never trigger.
    # 1.0 means "any overflow gets scaled back to fit".
    max_overflow_before_scale: float = 1.0

    # Horizontal scale is clamped to this floor; below it text becomes
    # unreadable and we prefer to reduce font size instead.
    min_horizontal_scale: float = 0.80

    # If horizontal scaling bottoms out, font size may be reduced by at most
    # this fraction (e.g. 0.90 = shrink to 90%) as a last resort in FIT mode.
    min_size_scale: float = 0.90

    # When a line has slack (handwriting narrower than original), spread the
    # extra space between words up to this factor of a normal space before
    # leaving the line left-aligned. Prevents rivers while keeping right edges
    # near the original.
    max_space_stretch: float = 3.0

    # --- Handwriting source ---------------------------------------------
    # Optional path to a user's handwriting SAMPLE SHEET image (rows of
    # A-Z / a-z / 0-9). When set, render.py composites the user's own glyphs
    # instead of drawing the TTF; any character not present on the sheet
    # (punctuation, accents) falls back to the TTF. When None, the TTF is the
    # sole source. Layout/measurement stay TTF-based either way, so glyph
    # source never affects pagination.
    handwriting_image: str | None = None

    # --- Rendering -------------------------------------------------------
    # Ink color source: "original" keeps each span's extracted color; a hex
    # string like "#1a1a6e" forces a single pen color (classic blue-black).
    ink_color: str = "original"

    # --- Output ----------------------------------------------------------
    # What to deliver. PDF/DOCX carry the rendered handwriting; TXT/MD deliver
    # the document's text content (handwriting can't live in a text file). When
    # None, the format is inferred from the output path's extension, defaulting
    # to PDF if the extension is unrecognized.
    output_format: "OutputFormat | None" = None

    # --- Tooling ---------------------------------------------------------
    # Command used to drive DOCX -> PDF. Overridable for non-standard installs.
    soffice_cmd: str | None = None  # None => auto-detect

    def font_for(self, style) -> tuple[str, bool, bool]:
        """Return (ttf_path, synth_bold, synth_italic) for a FontStyle.

        Falls back to the regular face and flags the missing attributes for
        synthesis at render time. Importing FontStyle lazily avoids a circular
        import between config and model."""
        from .model import FontStyle

        want_bold = style.bold
        want_italic = style.italic

        # A configured face path is only usable if the file actually exists;
        # otherwise we must fall back to the regular face and synthesize the
        # missing style. Without this guard a stale/default path (e.g. a bundled
        # bold face that was never downloaded) would crash font loading the
        # first time any bold/italic text is encountered.
        def _usable(path: str | None) -> str | None:
            return path if path and os.path.isfile(path) else None

        bold = _usable(self.font_bold)
        italic = _usable(self.font_italic)
        bold_italic = _usable(self.font_bold_italic)

        # Prefer an explicit face when present.
        if style is FontStyle.BOLD_ITALIC and bold_italic:
            return bold_italic, False, False
        if style is FontStyle.BOLD and bold:
            return bold, False, False
        if style is FontStyle.ITALIC and italic:
            return italic, False, False

        # Otherwise use bold face for bold-italic if available (synth italic),
        # else fall all the way back to regular and synthesize what's missing.
        if want_bold and bold:
            return bold, False, want_italic
        return self.font_regular, want_bold, want_italic
