"""Command-line entry point for handscrybe.

Usage:
    python -m handscrybe.cli <input.pdf|input.docx> <output.pdf> [options]

The CLI is a thin wrapper over ``pipeline.convert``: it parses arguments into a
``Config`` and calls convert. Keeping all real work in the library modules means
the tool is equally usable as ``from handscrybe.pipeline import convert``.
"""

from __future__ import annotations

import argparse
import sys

from .config import Config, LayoutMode, OutputFormat
from .pipeline import convert


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="handscrybe",
        description=(
            "Convert a DOCX or PDF into a handwriting PDF that preserves the "
            "original layout page-for-page, replacing only the typed text."
        ),
    )
    p.add_argument("input", help="Input document (.pdf or .docx)")
    p.add_argument("output", help="Output PDF path")

    p.add_argument(
        "--font",
        dest="font_regular",
        default=None,
        help="Path to the regular-weight handwriting .ttf (overrides default).",
    )
    p.add_argument(
        "--font-bold",
        dest="font_bold",
        default=None,
        help="Path to a bold handwriting .ttf. If omitted, bold is synthesized.",
    )
    p.add_argument(
        "--no-bold-font",
        action="store_true",
        help="Ignore any default bold face and always synthesize bold from the "
        "regular face (use when only one TTF is available).",
    )
    p.add_argument(
        "--italic-font",
        dest="font_italic",
        default=None,
        help="Path to an italic handwriting .ttf. If omitted, italic is sheared.",
    )
    p.add_argument(
        "--handwriting-image",
        dest="handwriting_image",
        default=None,
        help="Path to a photo/scan of your handwriting sample sheet "
        "(A-Z, then a-z, then 0-9, one row each, plus an optional 4th row of "
        "punctuation .,:;'\"!?()-/ ). Characters found on the sheet render as "
        "your own handwriting; anything missing falls back to the TTF font.",
    )
    p.add_argument(
        "--mode",
        choices=[m.value for m in LayoutMode],
        default=LayoutMode.FIT.value,
        help="Layout mode: 'fit' (default, never changes line count) or "
        "'reflow' (word-wrap within the block; may shift pagination).",
    )
    p.add_argument(
        "--size-scale",
        type=float,
        default=None,
        help="Multiplier applied to every source font size (default 1.0).",
    )
    p.add_argument(
        "--ink",
        dest="ink_color",
        default=None,
        help="Ink color: 'original' (default) or a hex like '#1a1a6e' to force "
        "a single pen color.",
    )
    p.add_argument(
        "--to",
        dest="output_format",
        choices=[f.value for f in OutputFormat],
        default=None,
        help="Output format. 'pdf'/'docx' carry the rendered handwriting; "
        "'txt'/'md' deliver the document's text content (handwriting can't live "
        "in a text file). Default: inferred from the output path's extension, "
        "else pdf.",
    )
    p.add_argument(
        "--soffice",
        dest="soffice_cmd",
        default=None,
        help="Path to the LibreOffice 'soffice' binary (for DOCX input). "
        "Auto-detected if omitted.",
    )
    return p


def _config_from_args(args: argparse.Namespace) -> Config:
    """Build a Config, overriding only the fields the user actually set so the
    dataclass defaults (and their factories) stay intact otherwise."""
    cfg = Config()

    if args.font_regular is not None:
        cfg.font_regular = args.font_regular
    if args.no_bold_font:
        cfg.font_bold = None
    if args.font_bold is not None:
        cfg.font_bold = args.font_bold
    if args.font_italic is not None:
        cfg.font_italic = args.font_italic
    if args.handwriting_image is not None:
        cfg.handwriting_image = args.handwriting_image
    cfg.mode = LayoutMode(args.mode)
    if args.size_scale is not None:
        cfg.size_scale = args.size_scale
    if args.ink_color is not None:
        cfg.ink_color = args.ink_color
    if args.soffice_cmd is not None:
        cfg.soffice_cmd = args.soffice_cmd

    # Output format: an explicit --to wins; otherwise infer from the output
    # path's extension so `... out.docx` just works. An unrecognized extension
    # leaves it None, which the pipeline treats as PDF.
    if args.output_format is not None:
        cfg.output_format = OutputFormat(args.output_format)
    else:
        ext = args.output.rsplit(".", 1)[-1].lower() if "." in args.output else ""
        try:
            cfg.output_format = OutputFormat(ext)
        except ValueError:
            cfg.output_format = OutputFormat.PDF
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cfg = _config_from_args(args)
    # A live progress bar (percentage + stage + elapsed time) driven by the
    # pipeline's real stage callbacks. It auto-disables when stdout isn't a
    # terminal, so piped/redirected runs stay clean.
    from .progress import TerminalProgress

    bar = TerminalProgress()
    try:
        out = convert(args.input, args.output, cfg, progress=bar)
        bar.finish()
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        # These are the expected, user-actionable failures (missing input,
        # unsupported format, missing font, LibreOffice not found/failed).
        # Clear the bar first so the error prints on a clean line.
        bar.clear()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
