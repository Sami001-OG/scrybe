"""End-to-end orchestration: input document -> handwriting PDF.

This module is the thin conductor that wires the five stages together in the
one order they can run:

    normalize -> parse_pdf -> fit_document -> render_pdf

Each stage is implemented (and tested) in its own module; the value here is
purely sequencing and passing the right objects along. The only real logic is
choosing a working directory for DOCX->PDF conversion and constructing the
`FontManager` once so the same loaded faces feed both measurement (layout) and
drawing (render) — a divergence between those two would let text overflow or
fall short of its budget.

The single measurement seam is `FontManager.measure`, handed to `fit_document`
as its `measure` callback. That keeps the layout engine free of any PDF/font
library while still budgeting against the exact widths the renderer will draw.
"""

from __future__ import annotations

import os
import tempfile
from typing import Callable

from .config import Config, OutputFormat
from .export import deliver
from .fonts import FontManager
from .layout import fit_document
from .normalize import normalize
from .parse_pdf import parse_pdf
from .render import render_pdf

# A progress reporter: called with (fraction_complete in 0..1, short_message).
# It is optional everywhere; when omitted, a no-op is used so the core stays
# unaware of any UI. The fractions below are fixed stage boundaries chosen so the
# bar advances in a way that matches where wall-clock time is actually spent:
# rendering (which draws every glyph on every page) gets the widest band and is
# the only stage that reports sub-progress, per page.
ProgressFn = Callable[[float, str], None]

# Stage boundaries as cumulative fractions. render occupies [_R0, _R1], the
# widest slice, because it dominates runtime on real documents.
_P_NORMALIZE = 0.10
_P_PARSE = 0.20
_P_FIT = 0.30
_R0 = 0.30
_R1 = 0.92
_P_DELIVER = 0.97


def _noop_progress(fraction: float, message: str) -> None:
    """Default reporter: does nothing. Keeps the pipeline UI-agnostic."""


def convert(
    input_path: str,
    output_path: str,
    config: Config | None = None,
    progress: ProgressFn | None = None,
) -> str:
    """Convert a DOCX or PDF at ``input_path`` into a handwriting PDF at
    ``output_path``. Returns ``output_path``.

    Steps:
      1. normalize  -- DOCX is rendered to PDF (LibreOffice); PDF passes through.
      2. parse_pdf  -- extract the span-centric layout model from the PDF.
      3. fit_document -- run the fitting engine so each line stays within its
         original width budget (measurement via FontManager.measure).
      4. render_pdf -- erase printed text and draw handwriting onto a copy of
         the (normalized) source PDF.

    ``progress`` is an optional ``(fraction, message)`` callback used to drive a
    progress bar in the CLI or web UI. It reports real stage transitions (and
    per-page render progress), never a synthetic timer, so the percentage always
    reflects work actually done. When omitted, conversion is silent.

    The source/normalized PDF is never mutated; only ``output_path`` is written.
    """
    if config is None:
        config = Config()
    report = progress or _noop_progress

    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input document not found: {input_path}")

    report(0.0, "Starting")

    # Resolve the output format: an explicit config value wins; otherwise infer
    # from the output path's extension; otherwise default to PDF. Storing it back
    # on the config keeps the rest of the function (and deliver) working off one
    # concrete value instead of re-deriving it.
    if config.output_format is None:
        ext = os.path.splitext(output_path)[1].lower().lstrip(".")
        try:
            config.output_format = OutputFormat(ext)
        except ValueError:
            config.output_format = OutputFormat.PDF

    # Fonts first: this validates the mandatory regular face up front, so a
    # missing-font setup error surfaces before we spend time on LibreOffice
    # conversion and parsing.
    fonts = FontManager(config)

    # If the user supplied a handwriting sample image, segment it into a glyph
    # set once, up front. Characters present in the set render as the user's own
    # handwriting; anything missing (punctuation, un-sampled glyphs) falls back
    # to the TTF at draw time. A failure to read the image is surfaced now rather
    # than mid-render. None => TTF-only (the original behavior).
    glyphs = None
    if config.handwriting_image:
        from .glyphs import GlyphSet

        report(0.03, "Reading your handwriting sample")
        glyphs = GlyphSet.from_sheet(config.handwriting_image)
        # Make width measurement glyph-aware: the fitting engine must budget the
        # REAL handwriting width of each sampled character (≈1.5x the TTF advance)
        # or the renderer's wider glyphs would overlap. This wires the same
        # advance helper render uses into fonts.measure, keeping layout and render
        # in agreement so pagination stays invariant while spacing comes out right.
        fonts.set_glyphs(glyphs)

    # DOCX conversion needs a scratch directory for the intermediate PDF (and
    # LibreOffice's throwaway user profile). A PDF input skips this entirely,
    # but we still create the dir cheaply and let it clean up on exit.
    with tempfile.TemporaryDirectory(prefix="handscrybe_") as work_dir:
        report(_P_NORMALIZE - 0.02, "Preparing the document")
        pdf_path, source_format = normalize(
            input_path, work_dir, soffice_cmd=config.soffice_cmd
        )
        report(_P_NORMALIZE, "Reading pages")

        document = parse_pdf(pdf_path)
        report(_P_PARSE, "Measuring the layout")
        # fit_document returns a flat list[PlacedPage]; render maps each onto the
        # source PDF by page number.
        placed_pages = fit_document(document, fonts.measure, config)
        report(_P_FIT, "Writing by hand")

        # Per-page render progress maps onto the [_R0, _R1] band: page k of n
        # advances the bar proportionally within that slice, so a long document
        # shows steady movement rather than one long freeze.
        total_pages = max(1, len(placed_pages))

        def _on_page(done: int, total: int) -> None:
            frac = _R0 + (_R1 - _R0) * (done / max(1, total))
            report(min(frac, _R1), f"Writing page {done} of {total}")

        # The handwriting PDF is always produced first — it's the native, highest
        # fidelity artifact. When PDF output is requested we render straight to
        # output_path; otherwise we render to a temp PDF and let export.deliver
        # transform it into the requested format (DOCX via LibreOffice, or the
        # document's text for TXT/MD).
        if config.output_format is OutputFormat.PDF:
            hw_pdf = output_path
        else:
            hw_pdf = os.path.join(work_dir, "handwriting.pdf")
        render_pdf(
            pdf_path, placed_pages, fonts, config, hw_pdf, glyphs=glyphs,
            on_page=_on_page,
        )

        report(_P_DELIVER, "Saving your file")
        deliver(
            handwriting_pdf=hw_pdf,
            source_pdf=pdf_path,
            output_path=output_path,
            fmt=config.output_format,
            soffice_cmd=config.soffice_cmd,
        )

    report(1.0, "Done")
    return output_path
