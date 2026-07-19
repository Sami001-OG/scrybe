"""Turn a user's handwriting sample image into a set of per-character glyphs.

WHAT THIS SOLVES
----------------
The rest of the pipeline can draw handwriting from a TTF (Caveat). This module
adds the other source the product promises: the *user's own* handwriting, given
as a photo/scan of a sample sheet laid out as

    A B C ... Z
    a b c ... z
    0 1 2 ... 9

We segment that image into individual character images and expose them as
`GlyphSet`, a dict-like ``char -> RGBA PIL.Image`` where the ink is opaque and
the paper is transparent. render.py composites those glyphs, tinted to each
run's color, into the same advance boxes the TTF would have used — so layout
math (which stays TTF-based and already tested) is untouched and only the
*appearance* of each glyph changes.

SEGMENTATION STRATEGY
---------------------
We use projection profiles rather than ML, because it's deterministic and
debuggable:

1.  Load, grayscale, and binarize (Otsu threshold) so ink=1, paper=0.
2.  Horizontal projection (ink per row) -> split into text ROWS at the vertical
    gaps between lines.
3.  Within each row, vertical projection (ink per column) -> split into
    character BOXES at the horizontal gaps between letters.
4.  Walk the boxes in reading order (top row left-to-right, then next row) and
    zip them against the expected character sequence. Whatever we successfully
    pair becomes a glyph; anything missing simply falls back to the TTF at
    render time, so a partial or imperfect scan still produces output.

Each glyph is trimmed to its ink, converted to RGBA with alpha = ink darkness
(so anti-aliased edges stay soft), and stored with its aspect ratio for
placement. Tinting to a target pen color happens later, per draw, so one glyph
serves any color.

Dependencies: Pillow (image IO) and numpy (projections). No PDF/font imports —
this module is about pixels only, which keeps it unit-testable in isolation.
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
DEFAULT_ORDER = UPPER + LOWER + DIGITS


@dataclass
class Glyph:
    """One extracted character. `image` is a tightly-trimmed RGBA image whose
    alpha channel is the ink coverage (opaque ink, transparent paper). `aspect`
    is width/height of the trimmed ink, used by the renderer to keep the glyph's
    natural proportions when it scales the glyph to the font size."""

    char: str
    image: Image.Image  # RGBA, trimmed to ink

    @property
    def aspect(self) -> float:
        w, h = self.image.size
        return (w / h) if h else 1.0


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


def _binarize(path: str) -> np.ndarray:
    """Load an image and return a boolean ink mask (True = ink).

    Ink is assumed darker than paper (pen on white), so we threshold below
    Otsu. We also guard against inverted scans by flipping if the 'ink' would
    otherwise cover most of the page."""
    img = Image.open(path).convert("L")
    gray = np.asarray(img, dtype=np.uint8)
    t = _otsu_threshold(gray)
    ink = gray < t  # darker than threshold = ink
    # If more than 50% is "ink", the image was probably light-on-dark; invert.
    if ink.mean() > 0.5:
        ink = ~ink
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


def _row_char_boxes(
    ink: np.ndarray, r0: int, r1: int, expected: int
) -> list[tuple[int, int, int, int]]:
    """Return character boxes for a single text row, trying to hit ``expected``.

    Splits the row into column spans, then if it found fewer than expected
    (touching letters) it splits the widest spans at ink valleys until the count
    matches. Each box is vertically tightened to its own ink."""
    h, w = ink.shape
    band = ink[r0:r1, :]
    col_profile = band.sum(axis=0)
    col_min_run = max(2, int(round(w * 0.006)))
    col_min_gap = max(2, int(round(w * 0.008)))
    col_spans = _segments_from_projection(col_profile, col_min_run, col_min_gap)

    # Recover merged letters: while short of the expected count, split the widest
    # remaining span at its deepest ink valley. Doing it one cut at a time and
    # re-picking the widest keeps splits distributed across whichever letters
    # actually touched, rather than hammering a single span.
    if expected > 0:
        while len(col_spans) < expected:
            widest_i = max(range(len(col_spans)), key=lambda i: col_spans[i][1] - col_spans[i][0])
            c0, c1 = col_spans[widest_i]
            pieces = _split_wide_box(c0, c1, col_profile, 1)
            if len(pieces) == 1:
                break  # can't split further; accept a short row
            col_spans[widest_i : widest_i + 1] = pieces

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

        Within a row we also actively recover merged letters: when a row yields
        fewer boxes than its expected character count, the widest boxes are split
        at ink valleys (see `_row_char_boxes`) until the counts line up. Whatever
        still can't be paired is simply left out and falls back to the TTF at
        render time, so an imperfect scan degrades gracefully instead of failing.
        """
        if not os.path.isfile(image_path):
            raise FileNotFoundError(f"Handwriting sample image not found: {image_path}")

        if rows is None:
            rows = [UPPER, LOWER, DIGITS]

        ink = _binarize(image_path)
        gray = np.asarray(Image.open(image_path).convert("L"), dtype=np.uint8)

        detected_rows = _row_spans(ink)
        aligned = _align_rows(detected_rows, rows)

        glyphs: dict[str, Glyph] = {}
        for (r0, r1), expected_chars in aligned:
            chars = [c for c in expected_chars if c != " "]
            boxes = _row_char_boxes(ink, r0, r1, expected=len(chars))
            for ch, (c0, br0, c1, br1) in zip(chars, boxes):
                gray_crop = gray[br0:br1, c0:c1]
                ink_crop = ink[br0:br1, c0:c1]
                if gray_crop.size == 0:
                    continue
                glyphs[ch] = Glyph(char=ch, image=_to_rgba_glyph(gray_crop, ink_crop))

        return cls(glyphs)

    def coverage(self, rows: list[str] | None = None) -> tuple[int, int]:
        """Return (found, expected) glyph counts, for reporting to the user."""
        if rows is None:
            rows = [UPPER, LOWER, DIGITS]
        expected = len([c for row in rows for c in row if c != " "])
        return len(self._glyphs), expected
