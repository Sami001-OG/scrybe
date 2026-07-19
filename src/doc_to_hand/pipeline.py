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

from .config import Config, OutputFormat
from .export import deliver
from .fonts import FontManager
from .layout import fit_document
from .normalize import normalize
from .parse_pdf import parse_pdf
from .render import render_pdf


def convert(
    input_path: str,
    output_path: str,
    config: Config | None = None,
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

    The source/normalized PDF is never mutated; only ``output_path`` is written.
    """
    if config is None:
        config = Config()

    if not os.path.isfile(input_path):
        raise FileNotFoundError(f"Input document not found: {input_path}")

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

        glyphs = GlyphSet.from_sheet(config.handwriting_image)

    # DOCX conversion needs a scratch directory for the intermediate PDF (and
    # LibreOffice's throwaway user profile). A PDF input skips this entirely,
    # but we still create the dir cheaply and let it clean up on exit.
    with tempfile.TemporaryDirectory(prefix="doc_to_hand_") as work_dir:
        pdf_path, source_format = normalize(
            input_path, work_dir, soffice_cmd=config.soffice_cmd
        )

        document = parse_pdf(pdf_path)
        # fit_document returns a flat list[PlacedPage]; render maps each onto the
        # source PDF by page number.
        placed_pages = fit_document(document, fonts.measure, config)

        # The handwriting PDF is always produced first — it's the native, highest
        # fidelity artifact. When PDF output is requested we render straight to
        # output_path; otherwise we render to a temp PDF and let export.deliver
        # transform it into the requested format (DOCX via LibreOffice, or the
        # document's text for TXT/MD).
        if config.output_format is OutputFormat.PDF:
            hw_pdf = output_path
        else:
            hw_pdf = os.path.join(work_dir, "handwriting.pdf")
        render_pdf(pdf_path, placed_pages, fonts, config, hw_pdf, glyphs=glyphs)

        deliver(
            handwriting_pdf=hw_pdf,
            source_pdf=pdf_path,
            output_path=output_path,
            fmt=config.output_format,
            soffice_cmd=config.soffice_cmd,
        )

    return output_path
