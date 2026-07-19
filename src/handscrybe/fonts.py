"""Handwriting font management and text measurement.

This module owns the boundary between the layout engine and PyMuPDF's font
machinery. It loads handwriting TTFs, hands back `fitz.Font` objects, and — most
importantly — measures rendered text width. Width measurement is what *drives*
the fitting engine in layout.py: every decision it makes (scale, letter-spacing,
size reduction) is a reaction to how wide a span's handwriting actually is
versus the width budget inherited from the source document. Keeping measurement
here, isolated from any drawing or fitting logic, means the layout engine can
ask "how wide is this?" without ever touching a PDF library.

Bold and italic are frequently *synthesized* from the regular face (stroke
widening + shear) rather than loaded as separate fonts, because good matching
handwriting faces are rare. This module doesn't perform the synthesis — render.py
does — but it must account for its width impact when measuring, and it exposes
the synthesis coefficients that render.py consumes so the two stages agree.

Import-safe: no filesystem access happens at import time. Fonts may not be
downloaded yet when this module is first imported.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import fitz  # PyMuPDF

from .config import FONTS_DIR, Config
from .model import FontStyle

# --- Synthesis coefficients (consumed by render.py) ----------------------
# Synthetic bold is drawn by re-stroking each glyph outline with a pen whose
# width is a fraction of the font size. This is the coefficient: stroke width =
# BOLD_STROKE_FACTOR * fontsize. Kept small — handwriting strokes are already
# organic, so a little widening reads as bold without turning to mud.
BOLD_STROKE_FACTOR: float = 0.03

# Synthetic italic is a horizontal shear (slant). 0.25 => tan(theta) = 0.25,
# i.e. ~14 degrees of lean, which matches typical oblique type without the
# exaggerated slant that makes handwriting look like it's falling over.
ITALIC_SHEAR: float = 0.25

# Width penalty applied when bold is synthesized. Stroke widening pushes glyph
# edges outward, so a synthetically-bolded run renders slightly wider than the
# base metrics report. text_length() measures the base outline and can't see the
# extra stroke, so we approximate the growth with a flat multiplier. This keeps
# the fitting engine from under-budgeting and letting bold runs overflow.
_SYNTH_BOLD_WIDTH_FACTOR: float = 1.0 + BOLD_STROKE_FACTOR  # ~1.03


@dataclass
class ResolvedFont:
    """A font choice resolved for one style: the loaded face plus the synthesis
    flags that render.py must apply to reach the requested style. `ttf_path` is
    retained for diagnostics and cache identity."""

    font: fitz.Font
    synth_bold: bool
    synth_italic: bool
    ttf_path: str


class FontManager:
    """Loads and caches handwriting fonts, and measures text width.

    One instance per conversion is enough; `fitz.Font` objects are immutable
    once loaded, so caching by path is safe and avoids re-parsing the same TTF
    for every span.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        # Cache keyed by ttf path so a face shared across styles (e.g. regular
        # reused for synthesized bold/italic) is only loaded once.
        self._fonts: dict[str, fitz.Font] = {}

        # Validate only the regular face eagerly: it's the mandatory base every
        # style falls back to, and a missing base is a setup error worth failing
        # fast on. Bold/italic faces are optional (may be None or synthesized),
        # so checking them here would reject valid configs.
        if not os.path.isfile(config.font_regular):
            raise FileNotFoundError(
                f"Handwriting font not found: {config.font_regular}\n"
                f"Place a regular-weight .ttf in the fonts directory:\n"
                f"  {FONTS_DIR}\n"
                "See the project README for recommended handwriting fonts "
                "(e.g. Caveat-Regular.ttf)."
            )

    def _load(self, ttf_path: str) -> fitz.Font:
        """Return the cached `fitz.Font` for a path, loading on first use."""
        font = self._fonts.get(ttf_path)
        if font is None:
            font = fitz.Font(fontfile=ttf_path)
            self._fonts[ttf_path] = font
        return font

    def resolve(self, style: FontStyle) -> ResolvedFont:
        """Resolve a style to a loaded face plus synthesis flags.

        Delegates the path/flag decision to Config.font_for so font-selection
        policy lives in one place; this method only turns that decision into a
        loaded font."""
        ttf_path, synth_bold, synth_italic = self._config.font_for(style)
        return ResolvedFont(
            font=self._load(ttf_path),
            synth_bold=synth_bold,
            synth_italic=synth_italic,
            ttf_path=ttf_path,
        )

    def font(self, style: FontStyle) -> fitz.Font:
        """Return the raw `fitz.Font` for a style, ignoring synthesis flags.

        Convenience for callers that only need the face (e.g. reading ascender/
        descender metrics)."""
        return self.resolve(style).font

    def measure(self, text: str, style: FontStyle, size: float) -> float:
        """Return the rendered width of `text` in points at the given size.

        This is the primitive the fitting engine budgets against. It uses the
        face that would actually be drawn for `style`, and inflates the result
        when bold is synthesized because stroke widening makes glyphs wider than
        their base outline (which is all text_length can see). Synthetic italic
        shear doesn't change advance width, so it isn't compensated here."""
        resolved = self.resolve(style)
        width = resolved.font.text_length(text, fontsize=size)
        if resolved.synth_bold:
            width *= _SYNTH_BOLD_WIDTH_FACTOR
        return width
