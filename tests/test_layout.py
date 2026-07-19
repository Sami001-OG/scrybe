"""Unit tests for the layout fitting engine.

Everything here uses a FAKE `measure` function so the tests exercise the fitter's
branch logic without any real fonts. The fake ruler makes width strictly linear
in both text length and size (`len(text) * K * size`), which lets each test dial
`W_hw` (handwriting width) and `W_orig` (the original bbox budget) independently:
the budget comes from the span bbox, the handwriting width comes from the ruler.
"""

from __future__ import annotations

import os
import sys

import pytest

# The package lives under src/ (no installed/editable dist, no conftest), so put
# it on the path here. This keeps the exact documented pytest invocation working
# without needing PYTHONPATH set in the environment.
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from handscrybe.config import Config, LayoutMode
from handscrybe.layout import (
    fit_document,
    fit_line,
    fit_page,
)
from handscrybe.model import (
    Document,
    FontStyle,
    Line,
    Page,
    PlacedPage,
    PlacedSpan,
    Rect,
    Span,
)

K = 0.5  # points of width per character per point of font size


def measure(text: str, style: FontStyle, size: float) -> float:
    """Fake width oracle: perfectly linear so tests can predict W_hw exactly."""
    return len(text) * K * size


def make_span(
    text: str,
    x0: float,
    budget: float,
    size: float = 10.0,
    style: FontStyle = FontStyle.REGULAR,
    y: float = 200.0,
    color: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> Span:
    """Build a span whose ORIGINAL bbox width is `budget` (the fitter's per-span
    contribution to W_orig), independent of what `measure` reports for it."""
    return Span(
        text=text,
        origin=(x0, y),
        bbox=Rect(x0, y - 8.0, x0 + budget, y + 2.0),
        size=size,
        style=style,
        color=color,
    )


# --- FIT: no distortion paths -------------------------------------------------


def test_line_fits_exactly_hscale_one():
    # len 10 * K(0.5) * size 10 = 50 handwriting; budget also 50.
    span = make_span("abcdefghij", x0=100.0, budget=50.0)
    line = Line(spans=[span])
    cfg = Config()

    (placed,) = fit_line(line, measure, cfg)

    assert placed.hscale == pytest.approx(1.0)
    assert placed.size == pytest.approx(10.0)
    assert placed.origin[0] == pytest.approx(100.0)  # original left origin preserved
    assert placed.origin[1] == pytest.approx(200.0)  # baseline never moves
    assert placed.char_spacing == pytest.approx(0.0)
    # redact_bbox must carry the SOURCE span box so render.py can erase the
    # printed text underneath before drawing handwriting.
    assert placed.redact_bbox is span.bbox
    assert placed.redact_bbox.as_tuple() == span.bbox.as_tuple()
    # render.py erases this box before drawing; it must be the source span's bbox.
    assert placed.redact_bbox is span.bbox


def test_line_with_slack_places_at_original_x():
    # 50 pts of handwriting inside a 100 pt budget -> pure slack.
    span = make_span("abcdefghij", x0=100.0, budget=100.0)
    line = Line(spans=[span])
    cfg = Config()

    (placed,) = fit_line(line, measure, cfg)

    assert placed.hscale == pytest.approx(1.0)
    assert placed.size == pytest.approx(10.0)
    assert placed.origin[0] == pytest.approx(100.0)


# --- FIT: condensing paths ----------------------------------------------------


def test_mild_overflow_condenses_within_floor():
    # W_hw = 12 * 0.5 * 10 = 60; budget 54 -> hscale 0.9 (>= floor 0.8), no shrink.
    span = make_span("abcdefghijkl", x0=100.0, budget=54.0)
    line = Line(spans=[span])
    cfg = Config()

    (placed,) = fit_line(line, measure, cfg)

    assert cfg.min_horizontal_scale <= placed.hscale < 1.0
    assert placed.hscale == pytest.approx(0.9)
    assert placed.size == pytest.approx(10.0)  # size untouched
    # Resulting rendered width must fit the original budget.
    rendered_width = placed.hscale * measure(span.text, span.style, placed.size)
    assert rendered_width <= 54.0 + 1e-9
    assert placed.origin[0] == pytest.approx(100.0)
    # redact_bbox must carry the SOURCE span box so render can erase print ink.
    assert placed.redact_bbox is span.bbox


def test_severe_overflow_hits_hscale_floor_and_reduces_size():
    # W_hw = 20 * 0.5 * 10 = 100; budget 76.
    # raw hscale 0.76 < floor -> hscale=0.8; remaining_ratio = 76/(100*0.8)=0.95
    # size_factor = max(0.90, 0.95) = 0.95 -> size 9.5 (fits within both floors).
    span = make_span("a" * 20, x0=100.0, budget=76.0)
    line = Line(spans=[span])
    cfg = Config()

    (placed,) = fit_line(line, measure, cfg)

    assert placed.hscale == pytest.approx(cfg.min_horizontal_scale)  # 0.80
    assert placed.size < 10.0
    assert placed.size == pytest.approx(9.5)
    # Re-measured at reduced size, condensed width should fit the budget.
    rendered_width = placed.hscale * measure(span.text, span.style, placed.size)
    assert rendered_width <= 76.0 + 1e-6


def test_extreme_overflow_hits_both_floors_gracefully():
    # W_hw = 20 * 0.5 * 10 = 100; budget 60.
    # raw hscale 0.6 -> floor 0.8; remaining_ratio = 60/80 = 0.75 < min_size_scale
    # -> size_factor clamps to 0.90. Both floors hit; residual overflow accepted.
    span = make_span("a" * 20, x0=100.0, budget=60.0)
    line = Line(spans=[span])
    cfg = Config()

    (placed,) = fit_line(line, measure, cfg)

    assert placed.hscale == pytest.approx(cfg.min_horizontal_scale)  # 0.80 floor
    assert placed.size == pytest.approx(10.0 * cfg.min_size_scale)  # 9.0 floor
    # Graceful degradation: it is allowed to overflow rather than distort more.
    rendered_width = placed.hscale * measure(span.text, span.style, placed.size)
    assert rendered_width > 60.0


# --- FIT: multi-span ----------------------------------------------------------


def test_multi_span_preserves_style_and_first_origin():
    # Two adjacent spans, different styles, comfortably inside budget (slack).
    s1 = make_span("hello", x0=100.0, budget=60.0, style=FontStyle.REGULAR)
    # Second span begins right after the first's bbox (x1 = 160) -> zero gap.
    s2 = make_span("world", x0=160.0, budget=60.0, style=FontStyle.BOLD)
    line = Line(spans=[s1, s2])
    cfg = Config()

    placed = fit_line(line, measure, cfg)

    assert len(placed) == 2
    assert placed[0].style is FontStyle.REGULAR
    assert placed[1].style is FontStyle.BOLD
    # First span keeps the line's original left origin.
    assert placed[0].origin[0] == pytest.approx(100.0)
    # Pure slack -> each span at its own original x.
    assert placed[1].origin[0] == pytest.approx(160.0)
    # Each placed span redacts its own source box.
    assert placed[0].redact_bbox is s1.bbox
    assert placed[1].redact_bbox is s2.bbox


def test_multi_span_scaled_keeps_left_origin_and_orders_spans():
    # Force condensing: two spans totalling W_hw wider than the budget.
    s1 = make_span("aaaaaaaaaa", x0=100.0, budget=30.0)  # W_hw 50
    s2 = make_span("bbbbbbbbbb", x0=130.0, budget=30.0)  # W_hw 50, total 100
    line = Line(spans=[s1, s2])  # budget 60, W_hw 100 -> condense
    cfg = Config()

    placed = fit_line(line, measure, cfg)

    assert placed[0].origin[0] == pytest.approx(100.0)  # left origin anchored
    assert placed[1].origin[0] > placed[0].origin[0]  # order preserved, rightward
    assert placed[0].hscale == pytest.approx(placed[1].hscale)  # uniform scale


# --- page / document ----------------------------------------------------------


def test_fit_page_smoke():
    line1 = Line(spans=[make_span("abcdefghij", x0=72.0, budget=50.0, y=100.0)])
    line2 = Line(spans=[make_span("a" * 20, x0=72.0, budget=60.0, y=120.0)])
    page = Page(number=0, width=612.0, height=792.0, lines=[line1, line2])
    cfg = Config()

    placed_page = fit_page(page, measure, cfg)

    # The render contract is flat: a PlacedPage carries one list of PlacedSpans
    # in source order, with no line grouping. Two single-span lines -> 2 spans.
    assert isinstance(placed_page, PlacedPage)
    assert placed_page.number == 0
    assert placed_page.width == pytest.approx(612.0)
    assert len(placed_page.spans) == 2
    assert all(isinstance(s, PlacedSpan) for s in placed_page.spans)


def test_fit_page_drops_empty_lines():
    empty = Line(spans=[])
    real = Line(spans=[make_span("abcdefghij", x0=72.0, budget=50.0)])
    page = Page(number=1, width=612.0, height=792.0, lines=[empty, real])

    placed_page = fit_page(page, measure, Config())

    # The empty line contributes no spans; only the real line's span survives.
    assert len(placed_page.spans) == 1


def test_fit_document_returns_pages():
    line = Line(spans=[make_span("abcdefghij", x0=72.0, budget=50.0)])
    page = Page(number=0, width=612.0, height=792.0, lines=[line])
    doc = Document(pages=[page], source_pdf_path="/tmp/source.pdf")

    # fit_document returns a plain list[PlacedPage]; the source path is carried
    # by the source Document, so no wrapper object is needed.
    placed = fit_document(doc, measure, Config())

    assert isinstance(placed, list)
    assert len(placed) == 1
    assert isinstance(placed[0], PlacedPage)
    assert len(placed[0].spans) == 1


# --- degenerate input ---------------------------------------------------------


def test_empty_line_returns_no_spans():
    assert fit_line(Line(spans=[]), measure, Config()) == []


def test_zero_width_handwriting_places_at_origin():
    # Empty text -> zero handwriting width; must not divide by zero.
    span = make_span("", x0=100.0, budget=50.0)
    (placed,) = fit_line(Line(spans=[span]), measure, Config())

    assert placed.hscale == pytest.approx(1.0)
    assert placed.origin[0] == pytest.approx(100.0)


# --- REFLOW (opt-in, basic) ---------------------------------------------------


def test_reflow_wraps_to_new_baseline():
    # Long single span that cannot fit its budget on one line -> wraps down.
    text = "one two three four five six seven eight"
    span = make_span(text, x0=100.0, budget=40.0, y=200.0, size=10.0)
    line = Line(spans=[span])
    cfg = Config(mode=LayoutMode.REFLOW)

    placed = fit_line(line, measure, cfg)

    ys = {p.origin[1] for p in placed}
    assert len(ys) > 1  # baselines were added below the original
    assert min(ys) == pytest.approx(200.0)  # first word keeps original baseline
    assert all(p.origin[0] >= 100.0 - 1e-9 for p in placed)
