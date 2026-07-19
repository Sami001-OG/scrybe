"""Render placed handwriting spans onto a copy of the source PDF.

This is the final stage of the pipeline and the only one that produces output
the user sees. Its contract is narrow but strict: take the layout engine's
already-positioned `PlacedSpan`s (see model.py) and turn them into a PDF that is
visually identical to the source *except* that every run of printed text has
been erased and redrawn in handwriting. Everything the renderer is NOT told
about — images, table borders, rules, vector graphics — must survive byte-for-
page untouched. That preservation is the whole point of the product, so the two
risky operations here (erasing text, and re-stroking for synthetic bold) are
implemented to be as surgical as PyMuPDF allows.

Two design decisions dominate this module:

1.  *How we erase without collateral damage.* We use PyMuPDF redaction
    annotations, but with the image/line-art removal knobs turned OFF
    (`images=PDF_REDACT_IMAGE_NONE`, `graphics=PDF_REDACT_LINE_ART_NONE`).
    By default `apply_redactions` also strips any vector graphics or images
    that merely *touch* a redaction box, which would delete the very borders
    and pictures we must keep. With those two flags we remove only the text
    covered by each box. Redact boxes come straight from `redact_bbox` and are
    kept tight around the original glyphs, so even text removal stays local.

2.  *How we honor per-span geometry.* Layout bakes an absolute baseline
    `origin` per span and a line-level `hscale` (horizontal squeeze) plus an
    optional `char_spacing`. PyMuPDF's high-level text writers can't do
    horizontal scaling, shear, or letter-spacing at once, so we place glyphs
    ourselves: we step the pen from `origin` along the baseline, advancing by
    the font's own glyph advance *scaled by hscale* plus `char_spacing`, and we
    realize hscale + synthetic-italic shear through a single affine `morph`
    matrix pivoted at the span origin. Doing the horizontal scale in the step
    AND in the morph is consistent because we pivot the morph at the same
    origin the steps start from: a glyph the step logic places at
    `origin.x + dx` is mapped by the morph to `origin.x + hscale*dx`, i.e. the
    intra-span advances are scaled about the span's left edge — exactly the
    scaling layout applied when it computed widths. (Layout also scaled each
    span's START origin about the line's left edge, so the per-span origins are
    already correct absolute points; we do not re-scale them here, we only pivot
    each span's own morph at its own origin. This keeps render and layout using
    one coherent scaling scheme.)

Synthetic italic is a horizontal shear of tan = ITALIC_SHEAR, and synthetic
bold widens each glyph by stroking its outline with a pen of width
BOLD_STROKE_FACTOR * fontsize (render_mode=2 fills AND strokes). Both
coefficients are imported from fonts.py so measurement (which drives layout)
and drawing agree exactly — a divergence there would let text overflow or fall
short of its budget.
"""

from __future__ import annotations

import io

import fitz  # PyMuPDF

from .config import Config
from .fonts import BOLD_STROKE_FACTOR, ITALIC_SHEAR, FontManager
from .glyphs import GlyphSet
from .model import PlacedPage, PlacedSpan


def _tint_glyph_png(glyph, color: tuple[float, float, float]) -> bytes:
    """Return PNG bytes of a glyph recolored to ``color``, alpha preserved.

    The stored glyph is black ink with an alpha mask (see glyphs._to_rgba_glyph).
    We paint every pixel the target pen color and keep the original alpha, so the
    handwriting takes on each run's ink color while its soft anti-aliased edges
    survive. PNG (not JPEG) because we must carry the alpha channel through to
    ``insert_image``, which composites it over the page."""
    from PIL import Image

    img = glyph.image  # RGBA, ink=opaque, paper=transparent
    r = int(round(color[0] * 255))
    g = int(round(color[1] * 255))
    b = int(round(color[2] * 255))
    solid = Image.new("RGBA", img.size, (r, g, b, 0))
    solid.putalpha(img.getchannel("A"))
    buf = io.BytesIO()
    solid.save(buf, format="PNG")
    return buf.getvalue()


def _resolve_ink(color: tuple[float, float, float], config: Config) -> tuple[float, float, float]:
    """Pick the pen color for a span.

    ``ink_color == "original"`` keeps the span's own extracted color (the
    common case, so scanned-in colored text stays colored). Any other value is
    treated as a hex string and forces one global ink — the classic "everything
    in blue-black pen" look. We parse defensively: a malformed hex falls back to
    the span color rather than raising mid-render, because a color glitch should
    never abort an otherwise-good document."""
    spec = config.ink_color
    if not spec or spec == "original":
        return color
    s = spec.lstrip("#")
    if len(s) == 6:
        try:
            r = int(s[0:2], 16) / 255.0
            g = int(s[2:4], 16) / 255.0
            b = int(s[4:6], 16) / 255.0
            return (r, g, b)
        except ValueError:
            return color
    return color


# Characters that hang below the baseline. When we composite fixed-height glyph
# images (which have no shared baseline reference, since each was trimmed to its
# own ink), we drop these so descenders read correctly instead of floating on the
# line. The fraction is of font size.
_DESCENDERS = set("gjpqy")
_DESCENDER_DROP = 0.22
# Target glyph height as a fraction of font size for composited handwriting
# images. Uniform height keeps proportions predictable across a trimmed-glyph set
# (we can't recover the writer's own relative sizing from independent crops); the
# result reads as clean hand-lettering and sits neatly on the baseline.
_GLYPH_HEIGHT = 0.92


def _tint_glyph_png(glyph, color: tuple[float, float, float]) -> bytes:
    """Return PNG bytes of `glyph` recolored to `color` (RGB in 0..1).

    The stored glyph is black ink with alpha = ink coverage. We paint the RGB
    channels to the pen color wherever there is ink and keep the original alpha,
    so anti-aliased stroke edges stay soft and the paper stays transparent. This
    is what makes one extracted glyph usable in any color (e.g. matching the
    source run's own color)."""
    import numpy as np  # local import: only needed on the image path

    arr = np.asarray(glyph.image.convert("RGBA"), dtype=np.uint8).copy()
    r = int(round(color[0] * 255))
    g = int(round(color[1] * 255))
    b = int(round(color[2] * 255))
    arr[..., 0] = r
    arr[..., 1] = g
    arr[..., 2] = b
    from PIL import Image

    out = Image.fromarray(arr, "RGBA")
    import io

    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _draw_glyph_image(
    page: fitz.Page,
    glyph,
    x: float,
    baseline_y: float,
    advance: float,
    size: float,
    hscale: float,
    color: tuple[float, float, float],
) -> None:
    """Composite one handwriting-image glyph onto the page.

    The glyph is scaled to a uniform target height (`_GLYPH_HEIGHT * size`),
    keeps its natural aspect ratio (then squeezed by `hscale` horizontally, the
    same squeeze layout applied), and is centered within the character's TTF
    advance slot so inter-character spacing stays regular and matches what the
    layout engine measured. Descender characters are dropped below the baseline
    so they hang correctly."""
    target_h = _GLYPH_HEIGHT * size
    glyph_w = target_h * glyph.aspect * hscale
    slot_w = advance * hscale
    # Center the glyph in its advance slot; if it's wider than the slot (rare,
    # very wide chars) it simply overhangs symmetrically, which looks natural.
    gx0 = x + (slot_w - glyph_w) / 2.0
    drop = _DESCENDER_DROP * size if glyph.char in _DESCENDERS else 0.0
    # Rect is (x0, top, x1, bottom) in y-down page space. Bottom sits on the
    # baseline (plus any descender drop); top is one glyph-height above it.
    bottom = baseline_y + drop
    top = bottom - target_h
    rect = fitz.Rect(gx0, top, gx0 + glyph_w, bottom)
    if rect.is_empty or rect.is_infinite:
        return
    png = _tint_glyph_png(glyph, color)
    page.insert_image(rect, stream=png, keep_proportion=False, overlay=True)


def _draw_span(
    page: fitz.Page,
    span: PlacedSpan,
    fonts: FontManager,
    config: Config,
    glyphs=None,
) -> None:
    """Draw one placed span's handwriting onto ``page``.

    Erasure has already happened for the whole page by the time we get here, so
    this only lays down ink. We place glyph-by-glyph so ``char_spacing`` and
    ``hscale`` both apply. Two ink sources are supported per character:

    * If `glyphs` (a `GlyphSet` from the user's handwriting sample) has the
      character, we composite its tinted image into the character's advance slot.
    * Otherwise we draw the TTF glyph (Caveat), with hscale + synthetic-italic
      shear folded into a single morph matrix pivoted at the span origin.

    Crucially, horizontal stepping always uses the TTF advance regardless of
    source, so line widths match exactly what the layout engine measured — the
    image path changes only how each glyph *looks*, never how wide the line is."""
    text = span.text
    if not text:
        return

    resolved = fonts.resolve(span.style)
    font = resolved.font
    size = span.size
    ttf_path = resolved.ttf_path
    # insert_text needs a fontname handle to register the embedded face; the
    # actual bytes come from fontfile. The name only has to be unique per face
    # within the page's resources, so key it on the ttf path.
    fontname = "hw_" + str(abs(hash(ttf_path)) % (10**8))

    color = _resolve_ink(span.color, config)
    hscale = span.hscale

    # Synthetic bold: fill + stroke (render_mode=2) with a pen proportional to
    # font size. When not bold we fill only (render_mode=0) and pass a tiny
    # border_width because PyMuPDF still wants a value. Stroke color matches the
    # fill so the glyph reads as one solid, slightly heavier mark.
    if resolved.synth_bold:
        render_mode = 2
        border_width = BOLD_STROKE_FACTOR * size
        stroke_color = color
    else:
        render_mode = 0
        border_width = 0.0
        stroke_color = None

    # Build the shear/scale morph. fitz.Matrix(a, b, c, d, e, f) maps
    # (x, y) -> (a*x + c*y + e, b*x + d*y + f): 'a' is horizontal scale, 'c' is
    # horizontal shear. We verified against PyMuPDF 1.24.10 that c = +ITALIC_SHEAR
    # produces a forward (rightward-at-top) lean in the y-down PDF space.
    shear = ITALIC_SHEAR if resolved.synth_italic else 0.0
    ox, oy = span.origin
    pivot = fitz.Point(ox, oy)
    matrix = fitz.Matrix(hscale, 0.0, shear, 1.0, 0.0, 0.0)
    # A morph of (pivot, matrix) applies matrix about pivot, so scaling and
    # shear happen about the baseline start — the same anchor layout used.
    morph = (pivot, matrix)

    # Step the pen manually. dx is in the span's own (un-scaled) coordinate
    # frame; the morph then scales it by hscale about the origin, so we advance
    # by the raw glyph advance plus char_spacing and let the morph do the
    # squeeze. This keeps the step math independent of hscale while still
    # producing hscale-correct final positions.
    dx = 0.0
    for ch in text:
        advance = font.text_length(ch, fontsize=size)
        glyph = glyphs.get(ch) if glyphs is not None else None
        if glyph is not None and glyph.aspect > 0:
            # User-handwriting image path. Position uses the un-scaled dx as the
            # left of the advance slot; _draw_glyph_image applies hscale inside
            # the slot, matching the TTF morph's scaling about the origin.
            _draw_glyph_image(
                page, glyph, ox + hscale * dx, oy, advance, size, hscale, color
            )
        else:
            point = fitz.Point(ox + dx, oy)
            # insert_text draws with the embedded fontfile; render_mode/
            # border_width give us the synthetic-bold stroke. morph applies
            # scale+shear per glyph about the shared span origin.
            page.insert_text(
                point,
                ch,
                fontsize=size,
                fontname=fontname,
                fontfile=ttf_path,
                color=color,
                fill=color,
                border_width=border_width if border_width else 0.05,
                render_mode=render_mode,
                stroke_opacity=1.0,
                fill_opacity=1.0,
                morph=morph,
            )
        # Advance by this glyph's own width (unscaled) plus letter-spacing. The
        # morph / slot logic handles hscale, so we deliberately do NOT pre-scale.
        dx += advance + span.char_spacing

    # stroke_color is unused directly (insert_text ties stroke to `color`), but
    # kept above to document that bold's stroke is the same hue as the fill.
    del stroke_color


def render_pdf(
    source_pdf_path: str,
    placed_pages: list[PlacedPage],
    fonts: FontManager,
    config: Config,
    output_path: str,
    glyphs=None,
    on_page=None,
) -> str:
    """Produce the final handwriting PDF and return ``output_path``.

    Opens the source read-only-in-spirit (we never save back over it), erases
    the printed text under every span via redaction, draws the handwriting, and
    writes a cleaned/compressed copy to ``output_path``. The source file is left
    untouched because we only ever ``save`` to a different path.

    `glyphs`, when given, is a `GlyphSet` built from the user's handwriting
    sample: characters it covers are drawn from those images, and everything else
    falls back to the TTF. When None, all text is drawn from the TTF (the
    original behavior).
    """
    doc = fitz.open(source_pdf_path)
    try:
        # Map placed pages onto source pages by number so a document whose
        # layout dropped/reordered nothing still lines up 1:1.
        pages_by_number = {pp.number: pp for pp in placed_pages}

        # Per-page progress is reported against the number of source pages, so
        # the fraction the caller sees advances once per finished page. A no-op
        # default keeps render UI-agnostic when no reporter is supplied.
        total = doc.page_count
        report_page = on_page or (lambda done, tot: None)

        for pno in range(doc.page_count):
            placed = pages_by_number.get(pno)
            if placed is None:
                report_page(pno + 1, total)
                continue
            page = doc[pno]

            # --- Erase original text -------------------------------------
            # One redaction annotation per span. fill=white paints the cleared
            # region white, matching the usual paper background; if a document
            # ever needs transparent clearing we'd drop fill, but white is the
            # safe default and hides any anti-aliasing halo from the old glyphs.
            for span in placed.spans:
                rect = fitz.Rect(*span.redact_bbox.as_tuple())
                if rect.is_empty or rect.is_infinite:
                    continue
                # cross_out=False: the default draws diagonal "redacted" lines
                # over the box, which would show up as ink over our clean white
                # fill. We only want the fill, not the crossing marks.
                page.add_redact_annot(rect, fill=(1, 1, 1), cross_out=False)

            if placed.spans:
                # Apply with removal disabled for images AND line art: this is
                # what protects tables, borders, rules and pictures that touch a
                # text box. text defaults to PDF_REDACT_TEXT_REMOVE, which is the
                # one thing we DO want gone.
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                    text=fitz.PDF_REDACT_TEXT_REMOVE,
                )

            # --- Draw handwriting ---------------------------------------
            for span in placed.spans:
                _draw_span(page, span, fonts, config, glyphs)

            report_page(pno + 1, total)

        # garbage=4 fully dedups/compacts the object tree (redactions leave
        # orphaned objects behind); deflate compresses streams. Both keep the
        # output lean without touching visible content.
        doc.save(output_path, garbage=4, deflate=True)
        return output_path
    finally:
        doc.close()
