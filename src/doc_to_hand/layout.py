"""Pure-math layout fitting engine.

This is the heart of the converter's fidelity guarantee. A source document has
already been reduced to the span/line/page model in `model.py`; every line
carries the exact horizontal extent it occupied in the original PDF. Handwriting
glyphs are wider (or narrower) than the print glyphs they replace, so if we drew
them naively the text would drift — a long line would push past the right margin,
overflow onto the next line, and shift every subsequent line down. That single
extra visual line is enough to change where page breaks fall, and once pagination
moves the output stops being a faithful copy of the original.

The fix is to treat each *original* line as an inviolable horizontal budget and
squeeze the handwriting to fit inside it. In FIT mode (the default) the engine
NEVER adds a line: it keeps every baseline where the source put it and instead
distorts the handwriting just enough to fit, using three levers in order of
increasing damage to legibility:

    1. horizontal scale (`hscale`)  -- condense glyphs sideways; cheapest.
    2. font-size reduction           -- shrink the whole line; costs vertical
                                        presence but a line stays one line.
    3. accepted overflow             -- when both floors are hit, let the line
                                        spill slightly rather than mangle it
                                        further; a production system would fall
                                        back to REFLOW here.

Each lever has a floor in `Config` so distortion is bounded and the output stays
readable. Because the levers only ever act *within* a line's own width and never
move a baseline, pagination is mathematically invariant in FIT mode.

This module is deliberately pure math. It imports no PDF library and touches no
files. Width comes in through a `measure` callback so the engine is decoupled
from the font machinery (see `fonts.FontManager.measure`), and placement goes out
as plain data (`PlacedSpan`) that the render stage turns into draw calls. That
separation is what lets us unit-test every fitting branch with a fake ruler and
no fonts at all.
"""

from __future__ import annotations

from typing import Callable

from .config import Config, LayoutMode
from .model import Document, FontStyle, Line, Page, PlacedPage, PlacedSpan

# A width oracle: given text, style and size, return rendered width in points.
# This is exactly `fonts.FontManager.measure`, but typed structurally so the
# engine never has to import that (PyMuPDF-backed) module.
Measure = Callable[[str, FontStyle, float], float]

# PlacedSpan and PlacedPage are the layout->render contract and live in model.py
# (the single source of truth render.py also imports). This module conforms to
# them rather than defining its own: a PlacedSpan carries `origin=(x, y)` and a
# `redact_bbox` (the source box render.py erases before drawing), and a
# PlacedPage holds a FLAT list of spans — no nested per-line grouping. The fitter
# still reasons per line internally; it just flattens the result at page level.
#
# The layout stage's whole product is `list[PlacedPage]` (see fit_document): the
# renderer maps each page back onto the source PDF by its `number`, and the
# source path stays on the source `Document`, so no extra wrapper is needed.


def _natural_offsets(
    line: Line, widths: list[float]
) -> tuple[list[float], float]:
    """Lay the spans out left-to-right at their handwriting widths, preserving
    the original inter-span gaps, and report each span's offset from the line
    start plus the total natural width.

    Spans within a line can be separated by whitespace (e.g. a style change
    mid-sentence leaves a gap between one span's end and the next span's
    origin). That gap is real layout information, so we keep it: the gap before
    span *i* is the original distance from span *i-1*'s right edge to span *i*'s
    origin, clamped to non-negative (extraction rounding can make it slightly
    negative). The returned offsets are in "content space" — i.e. before any
    horizontal scale is applied — so a caller can scale them uniformly.
    """
    offsets: list[float] = []
    cursor = 0.0
    spans = line.spans
    for i, w in enumerate(widths):
        if i > 0:
            gap = spans[i].origin[0] - spans[i - 1].bbox.x1
            if gap < 0.0:
                gap = 0.0
            cursor += gap
        offsets.append(cursor)
        cursor += w
    return offsets, cursor


def fit_line(line: Line, measure: Measure, cfg: Config) -> list[PlacedSpan]:
    """Fit one line's handwriting into its original horizontal extent.

    `measure(text, style, size) -> width_in_points` is the only outside
    dependency; it must reflect the width the renderer will actually draw.

    In FIT mode the algorithm is:

    1. Budget = the original line width `W_orig` (`line.bbox`). The start x is the
       original left origin of the first span, so the line begins exactly where
       it did in the source.
    2. Measure each span's handwriting width at its mapped size
       (`span.size * cfg.size_scale`) and sum them with the preserved gaps to get
       the natural handwriting width `W_hw`.
    3. Choose the least-damaging lever:
         * `W_hw <= W_orig * max_overflow_before_scale` -> no distortion. Slack
           lines keep each span at its original x; a (configurably) tolerated
           mild overflow is laid out naturally and allowed to spill.
         * otherwise condense: `hscale = W_orig / W_hw`, clamped to
           `min_horizontal_scale`.
         * if condensing bottoms out at the floor and still overflows, also
           reduce font size by `max(min_size_scale, remaining_ratio)` and
           re-measure. If even both floors together can't fit, accept the
           residual overflow (graceful degradation — REFLOW territory).

    REFLOW mode is handled separately (see `_reflow_line`); it is opt-in because
    it can add visual lines and therefore move pagination.
    """
    if cfg.mode is LayoutMode.REFLOW:
        return _reflow_line(line, measure, cfg)
    return _fit_line_fit_mode(line, measure, cfg)


def _fit_line_fit_mode(
    line: Line, measure: Measure, cfg: Config
) -> list[PlacedSpan]:
    """FIT-mode implementation of `fit_line` (see that docstring)."""
    spans = line.spans
    if not spans:
        return []

    W_orig = line.bbox.width
    start_x = spans[0].origin[0]

    mapped_sizes = [s.size * cfg.size_scale for s in spans]
    widths = [measure(s.text, s.style, ms) for s, ms in zip(spans, mapped_sizes)]
    _, W_hw = _natural_offsets(line, widths)

    # Degenerate lines (no measurable ink, or a zero-width budget) can't be
    # scaled meaningfully — place at original positions and return.
    if W_hw <= 0.0 or W_orig <= 0.0:
        return [
            PlacedSpan(
                text=s.text,
                origin=(s.origin[0], s.origin[1]),
                size=ms,
                style=s.style,
                color=s.color,
                redact_bbox=s.bbox,
                hscale=1.0,
            )
            for s, ms in zip(spans, mapped_sizes)
        ]

    # Overflow up to this width is left undistorted. With the default
    # max_overflow_before_scale == 1.0 this equals W_orig, so *any* overflow is
    # condensed; raising it lets small overflows through untouched.
    trigger = W_orig * cfg.max_overflow_before_scale

    hscale = 1.0
    size_factor = 1.0

    if W_hw > trigger:
        # Condense to the true original width (not the trigger): fitting to
        # W_orig is the invariant that keeps the right edge where it belongs.
        hscale = W_orig / W_hw
        if hscale < cfg.min_horizontal_scale:
            # Sideways squeeze has bottomed out. Pin it at the floor and buy the
            # rest of the fit with a font-size reduction. remaining_ratio is how
            # much narrower we still need to be after the hscale floor; it is
            # < 1 because W_hw * min_horizontal_scale still exceeds W_orig here.
            hscale = cfg.min_horizontal_scale
            remaining_ratio = W_orig / (W_hw * cfg.min_horizontal_scale)
            size_factor = max(cfg.min_size_scale, remaining_ratio)

    final_sizes = [ms * size_factor for ms in mapped_sizes]

    # When size changed, re-measure at the true final size so placement is
    # accurate even for fonts whose width isn't perfectly linear in size (our
    # size_factor estimate assumed linearity, which is fine as a heuristic but
    # not as final geometry).
    if size_factor != 1.0:
        widths = [
            measure(s.text, s.style, fs) for s, fs in zip(spans, final_sizes)
        ]
    offsets, _ = _natural_offsets(line, widths)

    # Placement. True slack (fits at natural width) preserves each span's exact
    # original x so an unchanged line is pixel-faithful. Anything we had to
    # touch is laid from the shared start x along the (uniformly scaled) natural
    # offsets, which keeps inter-span gaps proportional.
    pure_slack = hscale == 1.0 and size_factor == 1.0 and W_hw <= W_orig

    placed: list[PlacedSpan] = []
    for i, span in enumerate(spans):
        if pure_slack:
            x = span.origin[0]
        else:
            x = start_x + hscale * offsets[i]
        placed.append(
            PlacedSpan(
                text=span.text,
                # baseline y never moves — the fidelity anchor
                origin=(x, span.origin[1]),
                size=final_sizes[i],
                style=span.style,
                color=span.color,
                redact_bbox=span.bbox,  # renderer erases the original print box
                hscale=hscale,
            )
        )
    return placed


def _reflow_line(line: Line, measure: Measure, cfg: Config) -> list[PlacedSpan]:
    """Basic greedy word-wrap within the original line's width (REFLOW mode).

    CAVEAT: this is intentionally simple and opt-in. Unlike FIT, it may emit
    spans on *new* baselines (shifted down by the line height) when the
    handwriting won't fit on one line. That can push content down the page and
    therefore change pagination, which is exactly why REFLOW is not the default.
    It also does not attempt paragraph-aware rewrapping across lines, hyphenation,
    or justification; it wraps word-by-word within a single source line only, and
    joins words with a single measured space. Use it when squeezing in FIT mode
    would be illegible and a shifted layout is acceptable.
    """
    spans = line.spans
    if not spans:
        return []

    W_orig = line.bbox.width
    start_x = spans[0].origin[0]
    # Vertical step between wrapped lines. The source line's own height is the
    # most faithful spacing we have; fall back to a size-derived step if the
    # bbox is degenerate.
    line_height = line.bbox.height
    if line_height <= 0.0:
        line_height = max(s.size for s in spans) * cfg.size_scale * 1.2

    x = start_x
    y = spans[0].origin[1]
    right_limit = start_x + W_orig

    placed: list[PlacedSpan] = []
    for span in spans:
        size = span.size * cfg.size_scale
        space_w = measure(" ", span.style, size)
        words = span.text.split()
        for word in words:
            ww = measure(word, span.style, size)
            # Wrap if this word would cross the budget and we're not already at
            # the line start (a single over-long word is placed and allowed to
            # overflow rather than looping forever).
            if x > start_x and (x + ww) > right_limit:
                y += line_height
                x = start_x
            placed.append(
                PlacedSpan(
                    text=word,
                    origin=(x, y),
                    size=size,
                    style=span.style,
                    color=span.color,
                    redact_bbox=span.bbox,  # erase the source span's print box
                    hscale=1.0,
                )
            )
            x += ww + space_w
    return placed


def fit_page(page: Page, measure: Measure, cfg: Config) -> PlacedPage:
    """Fit every line on a page, preserving page geometry.

    Returns a `model.PlacedPage` carrying a FLAT list of `PlacedSpan`s in source
    order (the render contract keeps no line grouping — each span is a
    self-contained draw instruction). Empty lines contribute no spans.
    """
    spans: list[PlacedSpan] = []
    for line in page.lines:
        spans.extend(fit_line(line, measure, cfg))
    return PlacedPage(
        number=page.number,
        width=page.width,
        height=page.height,
        spans=spans,
    )


def fit_document(
    doc: Document, measure: Measure, cfg: Config
) -> list[PlacedPage]:
    """Fit an entire document, returning one `model.PlacedPage` per source page.

    A plain list is the whole product of the layout stage: the render stage maps
    each `PlacedPage` back onto the source PDF by its `number`, so no extra
    document wrapper is needed (source path lives on the source `Document`).
    """
    return [fit_page(p, measure, cfg) for p in doc.pages]
