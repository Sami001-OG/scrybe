"""Turn a user's handwriting sample image into a set of per-character glyphs.

WHAT THIS SOLVES
----------------
The rest of the pipeline can draw handwriting from a TTF (Caveat). This module
adds the other source the product promises: the *user's own* handwriting, given
as a photo/scan of a sample sheet laid out as

    A B C ... Z
    a b c ... z
    0 1 2 ... 9
    . , : ; ' " ! ? ( ) - /   (optional 4th row)

We segment that image into individual character images and expose them as
`GlyphSet`, a dict-like ``char -> Glyph`` where each glyph is an RGBA image (ink
opaque, paper transparent) plus a handful of baseline-relative size metrics.
render.py composites those glyphs, tinted to each run's color, into the same
advance boxes the TTF would have used — so layout math (which stays TTF-based
and already tested) is untouched and only the *appearance* of each glyph
changes. The metrics let render place each glyph with its natural relative
height and descent instead of forcing every character to one uniform height.

SEGMENTATION STRATEGY
---------------------
We use projection profiles rather than ML, because it's deterministic and
debuggable:

1.  Load, grayscale, and binarize so ink=1, paper=0. Binarization is *adaptive*
    (local block threshold via an integral-image background estimate) so ruled
    or grid paper and uneven lighting don't wreck the mask; long thin ruling
    runs are additionally suppressed.
2.  Horizontal projection (ink per row) -> split into text ROWS at the vertical
    gaps between lines.
3.  Within each row, vertical projection (ink per column) -> split into
    character BOXES at the horizontal gaps between letters. The per-row box
    count is reconciled *bidirectionally* against the expected count: too few
    boxes (touching letters) are split at ink valleys; too many boxes
    (over-segmentation, e.g. the digit row producing 11 boxes for 10 chars) are
    merged at the smallest inter-box gap. This keeps a single stray box from
    shifting every following character onto the wrong glyph.
4.  Walk the boxes in reading order (top row left-to-right, then next row) and
    zip them against the expected character sequence. Whatever we successfully
    pair becomes a glyph; anything missing simply falls back to the TTF at
    render time, so a partial or imperfect scan still produces output.

Per row we also derive a stable scale unit (the row x-height = median trimmed
box height) and a robust baseline (median of the dominant cluster of box
bottoms, so descenders don't drag it down). Every glyph then records its height,
width, ascent and descent as fractions of that x-height, which is all render
needs to place glyphs proportionally on a shared baseline.

Each glyph is trimmed to its ink, converted to RGBA with alpha = ink darkness
(so anti-aliased edges stay soft). Tinting to a target pen color happens later,
per draw, so one glyph serves any color.

Dependencies: Pillow (image IO) and numpy (projections + integral images). No
scipy/cv2, no PDF/font imports — this module is about pixels only, which keeps
it unit-testable in isolation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
from PIL import Image

# Canonical order the user is asked to write the sample sheet in. The segmenter
# pairs discovered character boxes against this sequence, in reading order.
UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWER = "abcdefghijklmnopqrstuvwxyz"
DIGITS = "0123456789"
# Fourth (optional) row: punctuation the user can add to the sample sheet so
# these marks render in their own hand instead of falling back to the TTF. The
# order is fixed so the segmenter can pair boxes positionally like the other
# rows. A sheet without this row still works — the marks just fall back.
PUNCT = ".,:;'\"!?()-/"
DEFAULT_ORDER = UPPER + LOWER + DIGITS + PUNCT

# --- Shared handwriting-advance geometry ---------------------------------
# These two constants are the single source of truth for how wide a handwriting
# glyph is on the page. BOTH layout measurement (fonts.measure, which drives the
# fitting engine) and render stepping import them, so the width the engine
# budgets is exactly the width the renderer draws. If they diverged, glyphs
# would either overlap (render wider than budgeted) or spread apart (narrower).
#
# X_HEIGHT_RATIO pins the writer's x-height — the unit all glyph metrics are
# normalized to — to a fraction of the run's cap-size `size`. Handwriting
# x-height typically runs a little over half the cap size.
X_HEIGHT_RATIO: float = 0.52

# Inter-letter gap added after each handwriting glyph, as a fraction of the
# writer's x-height. Glyph `w_frac` measures INK width only (the trimmed box),
# so without an explicit gap adjacent letters would touch. This is the breathing
# room between letters within a word; word spaces come from the space advance.
GLYPH_GAP_FRAC: float = 0.18

# Baseline-snap threshold. A letter whose measured ink-bottom sits within this
# fraction of x-height BELOW the fitted baseline is treated as resting on the
# baseline (descent snapped to 0). This flattens the small residual wobble that
# made lines look wavy, while staying well under any true descender (g/j/p/q/y
# hang ~0.4–0.75 x-height), which are left exactly as measured.
_BASELINE_SNAP_FRAC: float = 0.16

# Size multiplier for TTF FALLBACK glyphs (characters with no sampled
# handwriting — typically punctuation). Goal: match the fallback face's X-HEIGHT
# to the handwriting's, so a fallback comma/period sits proportionally beside the
# hand-lettered words instead of looking like a different font pasted in.
#
# Derived by direct ink measurement, not font metrics (which mislead here): the
# fallback face (Caveat) has an unusually small x-height, ~0.355 of its point
# size, while the handwriting's x-height letters draw at ~0.437 of the run size
# (median x-letter h_frac 0.84 * X_HEIGHT_RATIO). Matching the two =>
# 0.437 / 0.355 ≈ 1.23, i.e. the fallback must be drawn slightly LARGER than the
# run size to line its x-height up with the writing. Applied in BOTH
# glyph_advance (width) and render (draw) so measurement and drawing never
# disagree. Note: because Caveat's caps are large relative to its x-height, a
# fallback CAPITAL comes out a bit tall — an acceptable trade since caps are
# almost always present in the sample sheet and only punctuation truly falls
# back in practice.
FALLBACK_SIZE_SCALE: float = 1.23

# Vertical placement of each punctuation mark, as a signed `descent_frac` in
# x-height units: where the mark's INK BOTTOM sits relative to the writing
# baseline. Positive hangs below the baseline (comma, paren), zero rests on it
# (period, colon), negative floats above it (apostrophe up high, hyphen mid).
#
# Why a fixed table instead of measurement: for letters we fit a baseline
# through a whole row of ink-bottoms, but a punctuation row is a sparse mix of
# marks at wildly different vertical positions (a period on the baseline next to
# an apostrophe near cap height), so a fitted baseline is meaningless. We instead
# capture each mark's real INK (shape, size, width — all measured from the scan)
# and only pin WHERE it sits from this typographic table. The drawn height stays
# the mark's true ink height, so the writer's own dot/comma/paren shapes show
# through; only vertical anchoring is prescribed.
PUNCT_DESCENT: dict[str, float] = {
    ".": 0.0,    # period rests on the baseline
    ",": 0.22,   # comma tail dips below
    ":": 0.0,    # colon dots span baseline..mid, bottom on baseline
    ";": 0.22,   # semicolon has a comma tail
    "'": -0.95,  # apostrophe floats up near cap height
    '"': -0.95,  # double quote likewise
    "!": 0.0,    # exclamation rests on baseline
    "?": 0.0,    # question mark rests on baseline
    "(": 0.28,   # parenthesis dips below the baseline
    ")": 0.28,
    "-": -0.42,  # hyphen floats at mid x-height
    "/": 0.22,   # slash dips below
}


def glyph_advance(
    glyph: "Glyph | None",
    ch: str,
    size: float,
    ttf_advance: float,
) -> float:
    """Return the horizontal advance for one character, in points.

    This is the ONE place that decides how far the pen moves after a character,
    used identically by measurement (layout) and drawing (render) so the two can
    never disagree about line width.

    * If we have the user's handwriting for `ch`, the advance is the glyph's
      measured INK width (``w_frac`` in x-height units, converted to points via
      `X_HEIGHT_RATIO`) plus a fixed inter-letter gap (`GLYPH_GAP_FRAC`). This is
      what makes words space correctly: the old code stepped by the TTF advance,
      which is ~1.5x narrower than real handwriting, so glyphs piled on top of
      one another. Stepping by the true ink width + a gap spaces them naturally.
    * Otherwise (punctuation not sampled, or any missing char) we fall back to
      the TTF advance the caller already computed, so unsampled characters keep
      their previous, correct spacing.

    `ttf_advance` is the width of the character in the fallback face at the run's
    FULL size, passed in (rather than computed here) because computing it needs a
    `fitz.Font`, which this pixel-only module deliberately never imports. On the
    fallback path we scale it by `FALLBACK_SIZE_SCALE` because the renderer draws
    fallback glyphs at that reduced size (see render); scaling the advance to
    match keeps measurement and drawing in lockstep for unsampled characters too.
    """
    if glyph is not None and getattr(glyph, "w_frac", None):
        xheight_pt = size * X_HEIGHT_RATIO
        return glyph.w_frac * xheight_pt + GLYPH_GAP_FRAC * xheight_pt
    return ttf_advance * FALLBACK_SIZE_SCALE


@dataclass
class Glyph:
    """One extracted character.

    `image` is a tightly-trimmed RGBA image whose alpha channel is the ink
    coverage (opaque ink, transparent paper). `aspect` is width/height of the
    trimmed ink, used by the renderer to keep the glyph's natural proportions.

    The ``*_frac`` fields are baseline-relative metrics, all expressed as
    fractions of the glyph's ROW x-height (the median trimmed box height of that
    row — a stable per-row scale unit). They exist so render can place a glyph
    with its true relative size and descent instead of stretching every
    character to one uniform height:

    - ``h_frac``       — glyph ink height / row x-height (a cap > 1.0, an
                         x-height letter ~1.0, a period ~0.15).
    - ``w_frac``       — glyph ink width / row x-height (aspect is preserved via
                         ``w_frac / h_frac``).
    - ``ascent_frac``  — (row baseline - glyph ink top) / row x-height; how far
                         the glyph rises above the baseline.
    - ``descent_frac`` — (glyph ink bottom - row baseline) / row x-height,
                         clamped >= 0; how far a descender (g/j/p/q/y) hangs
                         below the baseline, 0 for non-descenders.

    Invariant: ``h_frac ≈ ascent_frac + descent_frac``.

    Defaults describe an old-style, uniform-height glyph sitting exactly on the
    baseline, so pre-metrics constructors and tests keep working unchanged.
    """

    char: str
    image: Image.Image  # RGBA, trimmed to ink

    # Baseline-relative metrics; see class docstring. Safe defaults = a plain
    # x-height glyph resting on the baseline with no descender.
    h_frac: float = 1.0
    w_frac: float = 1.0
    ascent_frac: float = 1.0
    descent_frac: float = 0.0

    @property
    def aspect(self) -> float:
        w, h = self.image.size
        return (w / h) if h else 1.0

    # --- Spec-named aliases -------------------------------------------------
    # The shared fidelity spec refers to these metrics as ``*_ratio``; render
    # and other consumers may use either spelling. They are read-only views onto
    # the canonical ``*_frac`` fields so there is a single source of truth.
    @property
    def height_ratio(self) -> float:
        return self.h_frac

    @property
    def ascent_ratio(self) -> float:
        return self.ascent_frac

    @property
    def descent_ratio(self) -> float:
        return self.descent_frac


def _otsu_threshold(gray: np.ndarray) -> float:
    """Return Otsu's optimal threshold for a uint8 grayscale array.

    Standard between-class variance maximization over the 256-bin histogram.
    Kept dependency-free (no skimage/cv2) so the module stays lightweight."""
    hist, _ = np.histogram(gray.ravel(), bins=256, range=(0, 256))
    hist = hist.astype(np.float64)
    total = gray.size
    sum_total = np.dot(np.arange(256), hist)
    sum_b = 0.0
    w_b = 0.0
    best_var = -1.0
    threshold = 127.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        if between > best_var:
            best_var = between
            threshold = float(t)
    return threshold


def _box_mean(a: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a ``(2*radius+1)`` square window at every pixel, edge-clamped.

    Computed from an integral image (summed-area table) so it is O(H*W)
    regardless of window size — the large windows used for background estimation
    would be far too slow as an explicit convolution, and we can't lean on
    scipy/cv2. The window is clipped at borders (count reflects the clipped
    area) so no wrap-around artifacts appear at the page edges."""
    a = a.astype(np.float64)
    H, W = a.shape
    ii = np.zeros((H + 1, W + 1), dtype=np.float64)
    ii[1:, 1:] = np.cumsum(np.cumsum(a, axis=0), axis=1)

    ys = np.arange(H)
    xs = np.arange(W)
    y0 = np.clip(ys - radius, 0, H)
    y1 = np.clip(ys + radius + 1, 0, H)
    x0 = np.clip(xs - radius, 0, W)
    x1 = np.clip(xs + radius + 1, 0, W)

    Y0 = y0[:, None]
    Y1 = y1[:, None]
    X0 = x0[None, :]
    X1 = x1[None, :]
    total = ii[Y1, X1] - ii[Y0, X1] - ii[Y1, X0] + ii[Y0, X0]
    count = (Y1 - Y0) * (X1 - X0)
    return total / count


def _win_sum(a: np.ndarray, length: int, axis: int) -> np.ndarray:
    """Centered sliding-window sum of `length` along `axis`, border-clipped.

    A tiny cumulative-sum primitive used to build 1-D morphological openings for
    ruling detection without scipy. Border windows are clipped, so the returned
    count at the edges is smaller than `length` (which conveniently means edge
    pixels can never be mistaken for the center of a full-length run)."""
    if axis == 0:
        return _win_sum(a.T, length, 1).T
    a = a.astype(np.int64)
    H, W = a.shape
    cs = np.zeros((H, W + 1), dtype=np.int64)
    cs[:, 1:] = np.cumsum(a, axis=1)
    idx = np.arange(W)
    left = length // 2
    right = length - 1 - left
    lo = np.clip(idx - left, 0, W)
    hi = np.clip(idx + right + 1, 0, W)
    return cs[:, hi] - cs[:, lo]


def _open_axis(ink: np.ndarray, length: int, axis: int) -> np.ndarray:
    """1-D morphological opening (erode then dilate) with a `length` flat SE.

    Keeps only ink runs at least `length` long along `axis`. Used both to find
    long ruling runs (open along the line's own axis with a big length) and to
    test thickness (open across the line with a small length)."""
    eroded = _win_sum(ink.astype(np.int64), length, axis) == length
    dilated = _win_sum(eroded.astype(np.int64), length, axis) >= 1
    return dilated


def _suppress_rulings(ink: np.ndarray) -> np.ndarray:
    """Remove long thin horizontal/vertical ruling runs from the ink mask.

    Grid and ruled paper add lines that span most of the page width/height at a
    small, near-constant thickness. Left in, they bridge letters and rows and
    ruin projection segmentation. We detect a ruling as ink that is (a) long
    along one axis — an opening with a length of ~half the page dimension keeps
    only runs that long — and (b) thin across the other axis — it must NOT
    survive a small opening across its width (a genuine tall/thick stroke
    would). Only pixels that are long-and-thin are removed, so real strokes,
    which are short relative to the page or thick, are preserved.

    A safety valve: if this would erase more than half the ink the detector has
    clearly misfired (e.g. a dense scan), so we return the mask untouched."""
    H, W = ink.shape
    if ink.sum() == 0:
        return ink

    long_h = max(8, int(round(W * 0.5)))
    long_v = max(8, int(round(H * 0.5)))
    max_thick = max(2, int(round(min(H, W) * 0.012)))

    remove = np.zeros_like(ink, dtype=bool)

    # Horizontal rulings: long along x, thin along y.
    h_runs = _open_axis(ink, long_h, axis=1)
    tall = _open_axis(ink, max_thick + 1, axis=0)  # vertically thick ink
    remove |= h_runs & ~tall

    # Vertical rulings: long along y, thin along x.
    v_runs = _open_axis(ink, long_v, axis=0)
    wide = _open_axis(ink, max_thick + 1, axis=1)  # horizontally thick ink
    remove |= v_runs & ~wide

    if remove.sum() > ink.sum() * 0.5:
        return ink
    return ink & ~remove


def _binarize(path: str) -> np.ndarray:
    """Load an image and return a boolean ink mask (True = ink).

    Global Otsu alone fails on the real target: grid/ruled paper and uneven
    lighting mean no single global threshold cleanly separates pen from paper.
    Instead we threshold *locally*:

    1. Normalize orientation — global Otsu tells us whether the dark class is
       the majority (a light-on-dark / inverted scan); if so we invert once so
       the rest of the routine can assume dark ink on light paper.
    2. Estimate the paper background with a large-window box mean (integral
       image, so it's cheap). Subtract it to get per-pixel "darkness relative to
       local background" — this cancels gradients and faint uniform grid tint.
    3. Otsu on that residual picks the ink/paper split adaptively; ``darkness >
       max(t, 1)`` keeps a truly blank sheet empty rather than all-ink.

    A final guard flips the result if it still somehow marks most of the page as
    ink, and long thin ruling runs are stripped (see `_suppress_rulings`)."""
    img = Image.open(path).convert("L")
    gray = np.asarray(img, dtype=np.uint8)

    # Normalize an inverted (light-on-dark) scan to dark-ink-on-light.
    tg = _otsu_threshold(gray)
    if (gray < tg).mean() > 0.5:
        gray = 255 - gray

    H, W = gray.shape
    # Background window: large enough to average paper (many stroke-widths
    # across) yet bounded so it stays a background, not a global, estimate.
    radius = max(15, min(H, W) // 8)
    background = _box_mean(gray, radius)

    darkness = np.clip(background - gray.astype(np.float64), 0, 255).astype(np.uint8)
    t = _otsu_threshold(darkness)
    ink = darkness > max(t, 1.0)

    # If "ink" covers most of the page the split inverted; flip it back.
    if ink.mean() > 0.5:
        ink = ~ink

    ink = _suppress_rulings(ink)
    return ink


def _segments_from_projection(profile: np.ndarray, min_run: int, min_gap: int) -> list[tuple[int, int]]:
    """Given a 1-D ink-count profile, return (start, end) spans of contiguous
    ink separated by gaps.

    `min_run` drops specks (spans thinner than this are noise); `min_gap` merges
    spans separated by less than this many empty slots (so a dotted 'i' or a
    broken stroke doesn't split into two characters).

    A noise floor is applied before thresholding: a slot counts as "ink" only if
    its projection exceeds a small fraction of the profile's peak. Without this,
    salt/pepper specks from a real scan put a nonzero count in every row/column
    gap, so `profile > 0` would see no gaps at all and merge every line (and
    every letter) into one blob. Keying the floor off the peak makes it scale to
    the image's ink density rather than an absolute pixel count."""
    peak = float(profile.max()) if profile.size else 0.0
    # 3% of peak: comfortably above stray specks, well below a real stroke's
    # contribution to its row/column. Zero-peak (blank) profiles yield no spans.
    floor = peak * 0.03
    active = profile > floor
    spans: list[tuple[int, int]] = []
    start = None
    for i, on in enumerate(active):
        if on and start is None:
            start = i
        elif not on and start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(active)))

    if not spans:
        return []

    # Merge spans separated by a gap smaller than min_gap.
    merged = [spans[0]]
    for s, e in spans[1:]:
        ps, pe = merged[-1]
        if s - pe < min_gap:
            merged[-1] = (ps, e)
        else:
            merged.append((s, e))

    # Drop runs shorter than min_run (noise).
    return [(s, e) for (s, e) in merged if (e - s) >= min_run]


def _split_wide_box(
    c0: int, c1: int, col_profile: np.ndarray, n_extra: int
) -> list[tuple[int, int]]:
    """Split one over-wide column span into ``n_extra + 1`` pieces.

    Used when a row found fewer boxes than expected because neighboring letters
    touched and merged into one span. We cut at the ``n_extra`` deepest interior
    valleys of the ink profile (the thinnest connections between letters), which
    recovers touching-but-distinct characters far better than cutting at equal
    widths. Valleys are chosen greedily and kept apart so we don't cut the same
    gap twice."""
    width = c1 - c0
    if n_extra <= 0 or width < (n_extra + 1) * 2:
        return [(c0, c1)]

    seg = col_profile[c0:c1].astype(np.float64)
    # Candidate cut columns: local minima of the ink profile, ranked shallowest
    # ink first. Guard the edges so a cut never lands at the very border.
    order = np.argsort(seg)
    min_sep = max(2, width // (n_extra + 1) // 2)
    cuts: list[int] = []
    for idx in order:
        col = int(idx)
        if col < min_sep or col > width - min_sep:
            continue
        if all(abs(col - k) >= min_sep for k in cuts):
            cuts.append(col)
        if len(cuts) == n_extra:
            break

    if not cuts:
        return [(c0, c1)]

    cuts.sort()
    pieces: list[tuple[int, int]] = []
    prev = 0
    for cut in cuts:
        pieces.append((c0 + prev, c0 + cut))
        prev = cut
    pieces.append((c0 + prev, c1))
    return pieces


def _reconcile_span_count(
    col_spans: list[tuple[int, int]], col_profile: np.ndarray, expected: int
) -> list[tuple[int, int]]:
    """Force the number of column spans toward ``expected``, both directions.

    Projection segmentation is never exact on a hand-written row, and the error
    goes BOTH ways:

    - Too FEW spans: neighboring letters touched and merged. We split the widest
      remaining span at its deepest ink valley, one cut at a time, re-picking the
      widest each round so cuts land wherever letters actually joined.
    - Too MANY spans: a letter over-segmented (a broken stroke, a gap inside a
      character, or — the verified digit-row bug — ``0-9`` yielding 11 spans for
      10 chars). Left alone, one surplus span shifts every following character
      onto the wrong glyph. We fix it by greedily MERGING the adjacent span pair
      with the smallest inter-span gap, fusing the pair that is most likely two
      fragments of one character, until the count matches.

    This is deliberately symmetric with the split path: both nudge the count to
    ``expected`` before assignment so a single stray box never cascades."""
    if expected <= 0 or not col_spans:
        return col_spans

    spans = list(col_spans)

    # Too few: split the widest span at its deepest valley until we catch up.
    while len(spans) < expected:
        widest_i = max(range(len(spans)), key=lambda i: spans[i][1] - spans[i][0])
        c0, c1 = spans[widest_i]
        pieces = _split_wide_box(c0, c1, col_profile, 1)
        if len(pieces) == 1:
            break  # can't split further; accept a short row
        spans[widest_i : widest_i + 1] = pieces

    # Too many: merge the closest-adjacent pair (smallest gap) until we match.
    # The gap between span i and i+1 is spans[i+1].start - spans[i].end; the
    # smallest gap is the seam most likely to be two fragments of one glyph.
    while len(spans) > expected:
        gaps = [spans[i + 1][0] - spans[i][1] for i in range(len(spans) - 1)]
        i = min(range(len(gaps)), key=lambda k: gaps[k])
        spans[i : i + 2] = [(spans[i][0], spans[i + 1][1])]

    return spans


def _connected_components(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Label 8-connected ink regions and return one bounding box per region.

    This is the letter finder. A projection profile collapses a row to a single
    ink-per-column curve, so two letters whose x-ranges overlap even slightly
    (the norm in real, slightly-slanted handwriting) share columns and read as
    ONE blob — the verified failure where 26 letters projected to 4-8 spans and
    the reconciler then chopped those blobs at arbitrary ink valleys, landing
    every box on the wrong letter. Labeling in 2-D instead keeps letters that are
    horizontally adjacent but not touching as separate regions, which is what a
    human sees.

    Implementation is a row-run union-find rather than per-pixel labeling: each
    horizontal ink run on a scanline is one node, and runs on vertically adjacent
    scanlines whose columns overlap (or touch diagonally, giving 8-connectivity)
    are merged. Cost is O(number of runs), which stays fast on the wide
    sample-sheet bands where a per-pixel loop would crawl, and needs no
    scipy/cv2. Returns half-open ``(c0, r0, c1, r1)`` boxes in arbitrary order.
    """
    H, W = mask.shape
    parent: list[int] = [0]  # 1-indexed labels; index 0 is unused

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    all_runs: list[tuple[int, int, int, int]] = []  # (label, row, start, end_incl)
    prev_runs: list[tuple[int, int, int]] = []       # (start, end_incl, label)
    next_label = 1

    for y in range(H):
        idx = np.nonzero(mask[y])[0]
        if idx.size == 0:
            prev_runs = []
            continue
        # Contiguous ink runs on this scanline (ends are inclusive).
        breaks = np.nonzero(np.diff(idx) > 1)[0]
        starts = np.concatenate(([idx[0]], idx[breaks + 1]))
        ends = np.concatenate((idx[breaks], [idx[-1]]))

        cur_runs: list[tuple[int, int, int]] = []
        for s, e in zip(starts.tolist(), ends.tolist()):
            lbl = next_label
            parent.append(lbl)
            next_label += 1
            # 8-connect to any previous-row run whose columns touch [s-1, e+1].
            for (ps, pe, plbl) in prev_runs:
                if ps <= e + 1 and pe >= s - 1:
                    union(lbl, plbl)
            cur_runs.append((s, e, lbl))
            all_runs.append((lbl, y, s, e))
        prev_runs = cur_runs

    boxes: dict[int, list[int]] = {}
    for (lbl, y, s, e) in all_runs:
        root = find(lbl)
        b = boxes.get(root)
        if b is None:
            boxes[root] = [s, y, e, y]
        else:
            if s < b[0]:
                b[0] = s
            if y < b[1]:
                b[1] = y
            if e > b[2]:
                b[2] = e
            if y > b[3]:
                b[3] = y
    return [(b[0], b[1], b[2] + 1, b[3] + 1) for b in boxes.values()]


def _row_char_boxes(
    ink: np.ndarray, r0: int, r1: int, expected: int
) -> list[tuple[int, int, int, int]]:
    """Return character boxes for a single text row, trying to hit ``expected``.

    Finds letters as 2-D connected ink regions (see `_connected_components`),
    which — unlike a 1-D column projection — keeps horizontally-adjacent letters
    apart even when their x-ranges overlap, the case that made projection paste
    whole groups of letters into one box and mis-pair the row. Components are
    then:

    1. Denoised — specks tiny in BOTH dimensions (scan salt) are dropped.
    2. Ordered left-to-right by horizontal center.
    3. Merged when their x-intervals overlap, which re-unites the parts of a
       single character that are not ink-connected: the dot over an ``i``/``j``,
       a stray accent, a broken stroke. Two distinct letters sit side by side in
       x, so this does not fuse them.
    4. Reconciled toward ``expected`` in BOTH directions (`_reconcile_span_count`)
       to absorb any residual over-/under-segmentation, then vertically tightened
       to each span's own ink.

    Hitting exactly ``expected`` boxes wherever possible is what keeps the later
    zip against the expected character sequence aligned."""
    h, w = ink.shape
    band = ink[r0:r1, :]
    band_h = r1 - r0
    col_profile = band.sum(axis=0)

    comps = _connected_components(band)
    # Drop specks that are small in BOTH dimensions (scan noise); a legitimately
    # thin mark (an apostrophe, the stem of an 'l') survives on its tall side.
    min_dim = max(2, int(round(band_h * 0.10)))
    comps = [
        c for c in comps
        if (c[2] - c[0]) >= min_dim or (c[3] - c[1]) >= min_dim
    ]

    if comps:
        comps.sort(key=lambda c: (c[0] + c[2]) / 2.0)
        # Merge components that overlap in x — parts of one character (i/j dot,
        # accent, broken stroke) — into a single span. Separate letters don't
        # overlap in x, so they stay distinct.
        merged: list[tuple[int, int, int, int]] = []
        for c in comps:
            if merged and c[0] <= merged[-1][2]:
                p = merged[-1]
                merged[-1] = (
                    min(p[0], c[0]), min(p[1], c[1]),
                    max(p[2], c[2]), max(p[3], c[3]),
                )
            else:
                merged.append(c)
        col_spans = [(c[0], c[2]) for c in merged]
    else:
        # Faint/blank row: fall back to the column-projection splitter so we
        # still emit whatever spans exist rather than nothing.
        col_min_run = max(2, int(round(w * 0.006)))
        col_min_gap = max(2, int(round(w * 0.008)))
        col_spans = _segments_from_projection(col_profile, col_min_run, col_min_gap)

    col_spans = _reconcile_span_count(col_spans, col_profile, expected)

    boxes: list[tuple[int, int, int, int]] = []
    for (c0, c1) in col_spans:
        sub = ink[r0:r1, c0:c1]
        rows_with_ink = np.where(sub.any(axis=1))[0]
        if rows_with_ink.size == 0:
            continue
        tr0 = r0 + int(rows_with_ink[0])
        tr1 = r0 + int(rows_with_ink[-1]) + 1
        boxes.append((c0, tr0, c1, tr1))
    return boxes


def _row_baseline(bottoms: list[int], ref_height: float) -> float:
    """Robustly estimate a row's baseline from its box ink-bottoms.

    Most letters — x-height letters, ascenders and caps alike — rest ON the
    baseline, so their ink-bottoms coincide. Descenders (g j p q y) hang below,
    pulling any naive mean/median of *all* bottoms downward. So instead of the
    plain median we find the dominant CLUSTER of bottoms: for each bottom, count
    how many others fall within a tight band (a fraction of the row's x-height),
    take the densest such group, and use its median. Ties in density are broken
    toward the SMALLER y (higher up the page) — i.e. the cluster near the top of
    the bottom-range — because that is the true baseline; the lower cluster is
    the descenders. Descenders, being a minority, never win the count.

    This returns a single scalar baseline (a flat line). For slanted samples use
    `_fit_baseline`, which returns a tilted line; this scalar version is kept as
    the fallback when there are too few boxes to fit a slope reliably."""
    arr = np.sort(np.asarray(bottoms, dtype=np.float64))
    if arr.size == 0:
        return 0.0
    if arr.size == 1:
        return float(arr[0])

    tol = max(1.0, 0.18 * ref_height)
    best_i = 0
    best_count = -1
    # Ascending order + strict-greater update keeps, among equal-density
    # clusters, the one with the smallest bottom (top of the bottom-range).
    for i in range(arr.size):
        count = int(np.sum(np.abs(arr - arr[i]) <= tol))
        if count > best_count:
            best_count = count
            best_i = i
    center = arr[best_i]
    cluster = arr[np.abs(arr - center) <= tol]
    return float(np.median(cluster))


def _fit_baseline(
    centers: list[float], bottoms: list[int], ref_height: float
) -> tuple[float, float]:
    """Fit a (possibly tilted) baseline line ``y = slope*x + intercept`` through
    a row's box ink-bottoms, robust to descenders.

    A single horizontal baseline is wrong for real handwriting: samples are
    almost always written with a slight upward or downward tilt, so the
    ink-bottoms of baseline-resting letters drift linearly across the row. If we
    force a flat baseline, letters on the "low" end of the tilt pick up a false
    descent (their bottom sits below the flat line) and letters on the "high"
    end lose real descent — exactly the artifact seen on a slanted sheet where
    flat glyphs like ``E`` or ``0`` wrongly appear to hang below the line.

    We fit a line instead. Descenders (a minority) would drag a plain
    least-squares fit downward, so we fit iteratively: start from an all-points
    least-squares line (which captures the tilt direction), then repeatedly drop
    points sitting more than a tolerance BELOW the current line (the descenders)
    and refit on the survivors (the true baseline letters). ``x`` is the glyph's
    horizontal center; ``y`` its ink bottom. Returns ``(slope, intercept)`` in
    pixel space. Falls back to a flat line at the robust scalar baseline when
    there are too few points to fit a slope."""
    xs = np.asarray(centers, dtype=np.float64)
    ys = np.asarray(bottoms, dtype=np.float64)
    n = xs.size
    if n == 0:
        return 0.0, 0.0
    if n < 3:
        # Too few points to trust a slope; use a flat robust baseline.
        return 0.0, _row_baseline([int(b) for b in bottoms], ref_height)

    # Descenders hang well below the baseline (~0.5–0.8 x-height in practice);
    # a tolerance around a third of the x-height cleanly separates baseline
    # letters (near/above the line) from descenders (far below) after the first
    # fit has picked up the tilt.
    tol = max(1.0, 0.30 * ref_height)

    def _lstsq(mask: np.ndarray) -> tuple[float, float]:
        A = np.vstack([xs[mask], np.ones(int(mask.sum()))]).T
        sol, *_ = np.linalg.lstsq(A, ys[mask], rcond=None)
        return float(sol[0]), float(sol[1])

    # Seed with an all-points fit so the slope reflects the row's actual tilt.
    slope, intercept = _lstsq(np.ones(n, dtype=bool))
    for _ in range(3):
        resid = ys - (slope * xs + intercept)  # >0 means below the line
        inliers = resid <= tol
        if int(inliers.sum()) < 2:
            break
        new_slope, new_intercept = _lstsq(inliers)
        # Stop once the fit settles, to avoid needless churn on clean rows.
        if abs(new_slope - slope) < 1e-4 and abs(new_intercept - intercept) < 1e-3:
            slope, intercept = new_slope, new_intercept
            break
        slope, intercept = new_slope, new_intercept
    return slope, intercept


def _row_spans(ink: np.ndarray) -> list[tuple[int, int]]:
    """Detect text-row bands via the horizontal ink projection."""
    h, _ = ink.shape
    row_profile = ink.sum(axis=1)
    # A text line must be at least ~1.5% of the page tall; lines are separated by
    # gaps of at least ~1% of page height. These are deliberately loose.
    row_min_run = max(3, int(round(h * 0.015)))
    row_min_gap = max(2, int(round(h * 0.010)))
    return _segments_from_projection(row_profile, row_min_run, row_min_gap)


def _align_rows(
    detected: list[tuple[int, int]], expected_rows: list[str]
) -> list[tuple[tuple[int, int], str]]:
    """Pair detected row bands with the expected row strings.

    The happy path is a 1:1 match (three written lines -> three expected rows).
    Reality is messier: a descender/ascender collision can split one written
    line into two bands, or a faint gap can merge two lines into one. We resolve
    this by count:

    - Equal counts: zip directly.
    - More detected than expected: the extras are almost always a single line
      that fragmented, so we greedily merge the closest-together adjacent bands
      (smallest vertical gap) until the counts match. Merging keeps reading
      order and simply widens a band, which the per-row splitter then handles.
    - Fewer detected than expected: we can't invent bands, so we pair the first
      N expected rows and drop the rest (those characters fall back to the TTF).

    Returns a list of ((r0, r1), expected_chars) in reading order.
    """
    bands = list(detected)
    n_exp = len(expected_rows)

    # Merge adjacent bands (closest first) until we have at most n_exp of them.
    while len(bands) > n_exp:
        # Find the adjacent pair with the smallest vertical gap and fuse them.
        gaps = [bands[i + 1][0] - bands[i][1] for i in range(len(bands) - 1)]
        i = min(range(len(gaps)), key=lambda k: gaps[k])
        bands[i : i + 2] = [(bands[i][0], bands[i + 1][1])]

    return [(bands[i], expected_rows[i]) for i in range(len(bands))]


def _to_rgba_glyph(gray_crop: np.ndarray, ink_crop: np.ndarray) -> Image.Image:
    """Build an RGBA glyph from a grayscale crop and its ink mask.

    Alpha is the ink darkness (255 - gray) but gated by the ink mask so paper
    stays fully transparent; RGB is left black and gets recolored at draw time.
    Using darkness for alpha preserves anti-aliased stroke edges, which keeps
    the handwriting looking natural rather than jagged/1-bit."""
    darkness = (255 - gray_crop).astype(np.uint8)
    alpha = np.where(ink_crop, darkness, 0).astype(np.uint8)
    h, w = alpha.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    # RGB stays 0 (black); tinting multiplies in a target color later.
    rgba[..., 3] = alpha
    return Image.fromarray(rgba, "RGBA")


class GlyphSet:
    """A user's handwriting as per-character RGBA glyphs.

    Dict-like: ``glyphset.get(ch)`` returns a `Glyph` or None. Built from a
    sample-sheet image via `from_sheet`. Immutable after construction; safe to
    share across a whole render.
    """

    def __init__(self, glyphs: dict[str, Glyph]) -> None:
        self._glyphs = glyphs

    def __contains__(self, ch: str) -> bool:
        return ch in self._glyphs

    def __len__(self) -> int:
        return len(self._glyphs)

    @property
    def chars(self) -> set[str]:
        return set(self._glyphs)

    def get(self, ch: str) -> Glyph | None:
        return self._glyphs.get(ch)

    @classmethod
    def from_sheet(
        cls, image_path: str, rows: list[str] | None = None
    ) -> "GlyphSet":
        """Segment a handwriting sample sheet into a GlyphSet.

        `rows` is the expected character content of each written line, in order
        — by default ``[A-Z, a-z, 0-9]``. Pairing is done ROW BY ROW rather than
        as one flat sequence: we detect the text-row bands, align them to the
        expected rows, and within each row pair the discovered character boxes
        against that row's expected characters. This containment matters — if two
        letters in the uppercase row touch and merge, only that row is affected;
        the lower-case and digit rows still pair correctly. (A single flat zip
        would let one merge shift every following character by one, corrupting
        the whole sheet.)

        Within a row the box count is reconciled bidirectionally against the
        expected count (split touching letters, merge over-segmented fragments),
        so both under- and over-segmentation are corrected before assignment.
        Whatever still can't be paired is simply left out and falls back to the
        TTF at render time, so an imperfect scan degrades gracefully.

        Per row we also compute a stable scale unit (x-height = median trimmed
        box height) and a robust baseline (dominant cluster of box bottoms), and
        record each glyph's height/width/ascent/descent as fractions of the
        x-height so render can place glyphs proportionally on a shared baseline.
        """
        if not os.path.isfile(image_path):
            raise FileNotFoundError(f"Handwriting sample image not found: {image_path}")

        if rows is None:
            rows = [UPPER, LOWER, DIGITS, PUNCT]

        ink = _binarize(image_path)
        gray = np.asarray(Image.open(image_path).convert("L"), dtype=np.uint8)

        detected_rows = _row_spans(ink)
        aligned = _align_rows(detected_rows, rows)

        # A row is "punctuation" when every expected char is a punctuation mark.
        # Punctuation is handled in a SECOND pass because it can't establish its
        # own scale or baseline: a row mixing a period, an apostrophe and a paren
        # has no meaningful median height or fitted baseline. We first process the
        # letter/digit rows (which do), collect their x-height units, and reuse a
        # global x-height to normalize punctuation so a tiny period isn't blown up
        # to letter size.
        def _is_punct_row(expected: str) -> bool:
            stripped = [c for c in expected if c != " "]
            return bool(stripped) and all(c in PUNCT for c in stripped)

        letter_rows = [(band, exp) for band, exp in aligned if not _is_punct_row(exp)]
        punct_rows = [(band, exp) for band, exp in aligned if _is_punct_row(exp)]

        glyphs: dict[str, Glyph] = {}
        ref_heights: list[float] = []

        # --- Pass 1: letter/digit rows (measured baseline + per-row x-height) ---
        for (r0, r1), expected_chars in letter_rows:
            chars = [c for c in expected_chars if c != " "]
            boxes = _row_char_boxes(ink, r0, r1, expected=len(chars))
            if not boxes:
                continue

            # Per-row scale unit and baseline. x-height = median trimmed box
            # height (robust and cheap). The baseline is fit as a (possibly
            # tilted) LINE through the box ink-bottoms rather than a single
            # scalar: real samples are written with a slight tilt, so a flat
            # baseline gives letters on the low end a false descent and robs the
            # high end of real descent. `_fit_baseline` returns slope/intercept
            # robust to descenders; each glyph is then measured against the
            # baseline evaluated at its own horizontal center. Guard against a
            # degenerate zero unit.
            heights = [br1 - br0 for (_, br0, _, br1) in boxes]
            ref_height = float(np.median(heights)) if heights else 1.0
            if ref_height <= 0:
                ref_height = 1.0
            ref_heights.append(ref_height)
            bottoms = [br1 for (_, _, _, br1) in boxes]
            centers = [(c0 + c1) / 2.0 for (c0, _, c1, _) in boxes]
            slope, intercept = _fit_baseline(centers, bottoms, ref_height)

            for ch, (c0, br0, c1, br1) in zip(chars, boxes):
                gray_crop = gray[br0:br1, c0:c1]
                ink_crop = ink[br0:br1, c0:c1]
                if gray_crop.size == 0:
                    continue

                # Baseline y at this glyph's horizontal center along the tilted
                # line. Measuring ascent/descent against the local baseline is
                # what removes the slant artifact.
                cx = (c0 + c1) / 2.0
                baseline = slope * cx + intercept

                h_frac = (br1 - br0) / ref_height
                w_frac = (c1 - c0) / ref_height
                ascent_frac = (baseline - br0) / ref_height
                descent_frac = max(0.0, (br1 - baseline) / ref_height)

                # Baseline jitter cleanup: a flat-bottomed letter (E, o, m) should
                # rest exactly on the baseline, but residual fit error leaves it a
                # few percent of x-height above/below, so lines look wavy. Snap
                # small descents to zero and fold the slack into ascent, which
                # flattens the baseline for resting glyphs while leaving true
                # descenders (g/j/p/q/y, descent >> the threshold) untouched.
                if descent_frac < _BASELINE_SNAP_FRAC:
                    ascent_frac += descent_frac
                    descent_frac = 0.0

                glyphs[ch] = Glyph(
                    char=ch,
                    image=_to_rgba_glyph(gray_crop, ink_crop),
                    h_frac=h_frac,
                    w_frac=w_frac,
                    ascent_frac=ascent_frac,
                    descent_frac=descent_frac,
                )

        # Global x-height: median of the letter/digit rows' units. Used to
        # normalize punctuation to the same scale the writing uses. If there were
        # no letter rows at all (punctuation-only sheet), fall back to each punct
        # row's own median so we still produce sensibly-scaled marks.
        global_xheight = float(np.median(ref_heights)) if ref_heights else 0.0

        # --- Pass 2: punctuation rows (global x-height + class placement) ---
        for (r0, r1), expected_chars in punct_rows:
            chars = [c for c in expected_chars if c != " "]
            boxes = _row_char_boxes(ink, r0, r1, expected=len(chars))
            if not boxes:
                continue

            unit = global_xheight
            if unit <= 0:
                heights = [br1 - br0 for (_, br0, _, br1) in boxes]
                unit = float(np.median(heights)) if heights else 1.0
                if unit <= 0:
                    unit = 1.0

            for ch, (c0, br0, c1, br1) in zip(chars, boxes):
                gray_crop = gray[br0:br1, c0:c1]
                ink_crop = ink[br0:br1, c0:c1]
                if gray_crop.size == 0:
                    continue

                # Keep the mark's REAL ink height and width (its own shape/size),
                # normalized to the global x-height so it sits proportionally next
                # to the writing. Vertical anchoring comes from the class table
                # rather than a fitted baseline, which punctuation can't provide.
                h_frac = (br1 - br0) / unit
                w_frac = (c1 - c0) / unit
                descent_frac = PUNCT_DESCENT.get(ch, 0.0)
                ascent_frac = h_frac - descent_frac

                glyphs[ch] = Glyph(
                    char=ch,
                    image=_to_rgba_glyph(gray_crop, ink_crop),
                    h_frac=h_frac,
                    w_frac=w_frac,
                    ascent_frac=ascent_frac,
                    descent_frac=descent_frac,
                )

        return cls(glyphs)

    def coverage(self, rows: list[str] | None = None) -> tuple[int, int]:
        """Return (found, expected) glyph counts, for reporting to the user."""
        if rows is None:
            rows = [UPPER, LOWER, DIGITS]
        expected = len([c for row in rows for c in row if c != " "])
        return len(self._glyphs), expected
