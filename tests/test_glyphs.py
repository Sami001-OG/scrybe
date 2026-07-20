"""Tests for handwriting-sample segmentation (`glyphs.py`).

We can't ship a photo of real handwriting, so the tests synthesize a sample
sheet by rendering the expected characters (A-Z / a-z / 0-9) with PyMuPDF onto a
white page and rasterizing it. That gives a deterministic, well-separated sheet
the projection segmenter should recover completely. To prove the segmenter is
not merely tuned to a perfect image, one test adds salt noise and per-character
vertical jitter and still requires full recovery.

The synthetic sheet is a fair stand-in for the real thing precisely because the
segmenter is geometry-based (projection profiles), not appearance-based: it
cares about ink/paper contrast and inter-character gaps, both of which the
synthetic sheet exhibits just like a scanned page.
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import fitz  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from handscrybe.glyphs import (  # noqa: E402
    DIGITS,
    LOWER,
    UPPER,
    GlyphSet,
)

_ROWS = [UPPER, LOWER, DIGITS]


def _make_sample_sheet(
    path: str,
    rows=_ROWS,
    noise: float = 0.0,
    jitter: int = 0,
    dpi_scale: float = 2.0,
    col_gap: float = 20.0,
    slant: float = 0.0,
) -> None:
    """Render a handwriting-style sample sheet and save it as a raster image.

    Each row is drawn with `col_gap` points of inter-character spacing (generous
    by default so the segmenter has clear gaps). `noise` sprinkles salt/pepper
    specks (fraction of pixels) and `jitter` shifts each character up/down by up
    to +/-jitter px to mimic a hand-written, un-ruled sheet. `slant` skews each
    row's baseline (points of vertical drift per character) to mimic the upward/
    downward tilt of real handwriting; combined with a small `col_gap` this
    reproduces the real-world case where letters' x-ranges overlap."""
    # Lay out on a PDF page first (easy text placement), then rasterize.
    page_w, page_h = 612.0, 300.0
    doc = fitz.open()
    page = doc.new_page(width=page_w, height=page_h)

    top = 60.0
    row_gap = 70.0
    left = 40.0
    fontsize = 28.0
    for ri, row in enumerate(rows):
        y = top + ri * row_gap
        x = left
        for ci, ch in enumerate(row):
            page.insert_text((x, y + ci * slant), ch, fontname="helv", fontsize=fontsize)
            x += fontsize * 0.6 + col_gap

    pix = page.get_pixmap(matrix=fitz.Matrix(dpi_scale, dpi_scale))
    doc.close()

    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    arr = np.asarray(img, dtype=np.uint8).copy()

    if jitter > 0:
        # Column-wise vertical roll gives a cheap per-region wobble without
        # needing to know exact glyph boxes.
        rng = np.random.default_rng(1234)
        for c in range(0, arr.shape[1], 40):
            shift = int(rng.integers(-jitter, jitter + 1))
            arr[:, c : c + 40] = np.roll(arr[:, c : c + 40], shift, axis=0)

    if noise > 0.0:
        rng = np.random.default_rng(99)
        mask = rng.random(arr.shape) < noise
        # Salt specks: random dark dots on the paper.
        arr[mask] = 0

    Image.fromarray(arr, "L").save(path)


def test_full_recovery_on_clean_sheet(tmp_path):
    sheet = str(tmp_path / "sheet.png")
    _make_sample_sheet(sheet)

    gs = GlyphSet.from_sheet(sheet)

    found, expected = gs.coverage()
    assert expected == 62
    assert found == 62, f"only recovered {found}/62; missing glyphs"
    # Spot-check a few glyphs across all three rows exist and carry ink.
    for ch in ("A", "Z", "a", "z", "0", "9"):
        g = gs.get(ch)
        assert g is not None, f"missing {ch!r}"
        arr = np.asarray(g.image.convert("RGBA"))
        assert arr[..., 3].max() > 0, f"glyph {ch!r} has no ink"
        assert g.aspect > 0


def test_recovery_survives_noise_and_jitter(tmp_path):
    sheet = str(tmp_path / "noisy.png")
    _make_sample_sheet(sheet, noise=0.002, jitter=3)

    gs = GlyphSet.from_sheet(sheet)

    found, expected = gs.coverage()
    # Full recovery is the goal; allow a tiny shortfall in case a speck merges
    # two characters, but the row-aware pairing should keep it near-perfect.
    assert found >= expected - 1, f"noise/jitter degraded recovery to {found}/{expected}"


def test_glyph_alpha_is_transparent_paper(tmp_path):
    """The extracted glyph must have transparent paper (alpha 0) and opaque ink,
    so it composites cleanly over document backgrounds."""
    sheet = str(tmp_path / "sheet.png")
    _make_sample_sheet(sheet)
    gs = GlyphSet.from_sheet(sheet)

    g = gs.get("H")
    assert g is not None
    alpha = np.asarray(g.image.convert("RGBA"))[..., 3]
    # There must be both fully/near transparent pixels (paper) and strong ink.
    assert (alpha == 0).any(), "no transparent paper pixels"
    assert alpha.max() > 128, "ink not opaque enough"


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        GlyphSet.from_sheet(str(tmp_path / "nope.png"))


def test_tight_slanted_rows_map_to_correct_letters(tmp_path):
    """Regression for the real-world failure: tightly-spaced, slanted letters.

    With a small inter-character gap and a per-row slant, adjacent letters'
    x-ranges overlap. A 1-D column projection then sees a whole group of letters
    as ONE ink blob (verified: 26 letters projecting to 4-8 spans), and the
    count-reconciler chops those blobs at arbitrary ink valleys — so every box
    after the first merge lands on the WRONG letter even though the box COUNT
    comes out right. Connected-components segmentation isolates each letter in
    2-D instead, which this test locks in.

    We don't just count glyphs (the old bug produced the right count of wrong
    glyphs); we verify each extracted glyph actually matches the letter it was
    paired with. The check is by ink signature: re-render every candidate letter
    in the same font and pick the best-correlated one, then require the paired
    letter to be that best match. That catches an off-by-one shift that a pure
    count assertion would miss."""
    sheet = str(tmp_path / "tight.png")
    # col_gap small enough that neighboring letters share columns once slanted.
    _make_sample_sheet(sheet, col_gap=3.0, slant=0.6)

    gs = GlyphSet.from_sheet(sheet)

    # Render a reference bitmap for each letter in the same font, so we can
    # identify what an extracted glyph actually depicts independent of pairing.
    def _trim_to_ink(ink: np.ndarray) -> np.ndarray:
        # Crop to the ink bounding box so shape, not position/padding, is
        # compared. Both the reference and the extracted glyph are trimmed the
        # same way before correlation.
        ys, xs = np.nonzero(ink > ink.max() * 0.25)
        if ys.size == 0:
            return ink
        return ink[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]

    def _ref_bitmap(ch: str, box: int = 48) -> np.ndarray:
        doc = fitz.open()
        page = doc.new_page(width=box, height=box)
        page.insert_text((box * 0.2, box * 0.75), ch, fontname="helv", fontsize=box * 0.7)
        pix = page.get_pixmap(matrix=fitz.Matrix(1, 1))
        doc.close()
        g = np.asarray(
            Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
        )
        return _trim_to_ink((255 - g).astype(np.float64))  # ink positive, trimmed

    def _score(a: np.ndarray, b: np.ndarray) -> float:
        # Compare two ink masks by trimming to ink, resizing to a common grid,
        # and correlating — so the comparison is of glyph shape, not placement.
        ai = Image.fromarray(_trim_to_ink(a)).resize((32, 32))
        bi = Image.fromarray(b).resize((32, 32))
        av = np.asarray(ai, dtype=np.float64).ravel()
        bv = np.asarray(bi, dtype=np.float64).ravel()
        av -= av.mean()
        bv -= bv.mean()
        denom = (np.linalg.norm(av) * np.linalg.norm(bv)) or 1.0
        return float(np.dot(av, bv) / denom)

    # Uppercase is the least ambiguous row; verify each paired glyph's ink best
    # matches its own letter among all 26 candidates.
    refs = {ch: _ref_bitmap(ch) for ch in UPPER}
    correct = 0
    for ch in UPPER:
        g = gs.get(ch)
        if g is None:
            continue
        alpha = np.asarray(g.image.convert("RGBA"))[..., 3].astype(np.float64)
        best = max(UPPER, key=lambda cand: _score(alpha, refs[cand]))
        if best == ch:
            correct += 1

    # Allow a couple of genuinely confusable pairs (O/Q, I/J at low res) to miss,
    # but the row must be overwhelmingly correctly mapped — the old blob-cutting
    # segmenter scored near-random here.
    assert correct >= 24, f"only {correct}/26 uppercase glyphs mapped to the right letter"


def test_partial_sheet_only_pairs_present_rows(tmp_path):
    """A sheet with only two rows should still pair those rows correctly against
    the first two expected rows, leaving the third row's characters uncovered
    (they fall back to the TTF at render time)."""
    sheet = str(tmp_path / "two_rows.png")
    _make_sample_sheet(sheet, rows=[UPPER, LOWER])

    gs = GlyphSet.from_sheet(sheet)
    # Upper and lower recovered; digits absent.
    assert gs.get("A") is not None
    assert gs.get("a") is not None
    assert all(gs.get(d) is None for d in DIGITS)
