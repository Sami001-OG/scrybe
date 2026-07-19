"""Interactive command-line wizard for Handscrybe.

Instead of remembering CLI flags, a user can run this wizard and answer a short
series of questions: which document to convert, whether to use their own
handwriting, ink color, layout mode, output format, and where to write the
result. The wizard builds a :class:`~handscrybe.config.Config` and hands it to
``pipeline.convert``.

DESIGN: EVERY SIDE EFFECT IS INJECTABLE
---------------------------------------
The wizard never touches ``input``/``print``/``convert`` directly in its logic.
All I/O goes through ``input_fn``/``output_fn`` and the heavy work through
``convert_fn`` / ``glyph_loader`` / ``soffice_finder``. This keeps ``run_wizard``
a pure orchestration function that tests can drive with scripted answers and
fakes — no real stdin, no real conversion, no external tools.

The small ``ask*`` helpers each take the same ``input_fn``/``output_fn`` pair so
they can be unit-tested in isolation, and so re-prompt / EOF handling lives in
one place instead of being scattered through the flow.
"""

from __future__ import annotations

import os
from typing import Callable, Iterable

from .config import Config, LayoutMode, OutputFormat
from .glyphs import GlyphSet
from .normalize import find_soffice
from .pipeline import convert

# Type aliases for the injectable primitives.
InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]

SUPPORTED_INPUT_EXTS = (".pdf", ".docx", ".txt")

# Platform-specific install hint for LibreOffice (needed for DOCX *input*).
_LIBREOFFICE_HINTS = {
    "win32": "winget install TheDocumentFoundation.LibreOffice",
    "darwin": "brew install --cask libreoffice",
    "linux": "sudo apt install libreoffice",
}


class WizardExit(Exception):
    """Raised internally to unwind the flow when the user asks to quit or the
    input stream ends (EOF). ``run_wizard`` catches it and returns cleanly so a
    Ctrl-D / Ctrl-Z never surfaces as a traceback."""


# ---------------------------------------------------------------------------
# Prompting primitives
# ---------------------------------------------------------------------------
def _read(prompt: str, *, input_fn: InputFn, output_fn: OutputFn) -> str:
    """Read one line, translating EOF into a clean wizard exit.

    A bare ``input()`` raises ``EOFError`` when stdin closes; we convert that to
    ``WizardExit`` so the flow unwinds through the same path as an explicit
    quit, instead of crashing."""
    try:
        return input_fn(prompt)
    except EOFError:
        output_fn("")
        output_fn("Input closed - exiting.")
        raise WizardExit from None


def ask(
    prompt: str,
    *,
    input_fn: InputFn,
    output_fn: OutputFn,
    default: str | None = None,
) -> str:
    """Ask a free-text question. Returns the stripped answer.

    If ``default`` is given, an empty answer (just pressing Enter) yields it and
    the default is shown in brackets. Without a default, empty answers re-prompt
    until the user types something."""
    suffix = f" [{default}]" if default is not None else ""
    while True:
        answer = _read(f"{prompt}{suffix}: ", input_fn=input_fn, output_fn=output_fn).strip()
        if answer:
            return answer
        if default is not None:
            return default
        output_fn("Please enter a value.")


def ask_yes_no(
    prompt: str,
    *,
    input_fn: InputFn,
    output_fn: OutputFn,
    default: bool = True,
) -> bool:
    """Ask a yes/no question. ``default`` decides what Enter means and which
    letter is capitalized in the ``[Y/n]`` / ``[y/N]`` hint."""
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = _read(f"{prompt} {hint}: ", input_fn=input_fn, output_fn=output_fn).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        output_fn("Please answer y or n.")


def ask_choice(
    prompt: str,
    options: list[str],
    *,
    input_fn: InputFn,
    output_fn: OutputFn,
    default: int = 0,
) -> int:
    """Show a numbered menu and return the chosen zero-based index.

    ``default`` is a zero-based index; it's shown as ``[N]`` (one-based) and
    selected on empty input. Non-numbers and out-of-range numbers re-prompt
    rather than raising."""
    output_fn(prompt)
    for i, opt in enumerate(options, start=1):
        output_fn(f"  {i}. {opt}")
    while True:
        raw = _read(
            f"Choose 1-{len(options)} [{default + 1}]: ",
            input_fn=input_fn,
            output_fn=output_fn,
        ).strip()
        if not raw:
            return default
        try:
            choice = int(raw)
        except ValueError:
            output_fn("Please enter a number from the list.")
            continue
        if 1 <= choice <= len(options):
            return choice - 1
        output_fn(f"Please choose a number between 1 and {len(options)}.")


def ask_path(
    prompt: str,
    *,
    input_fn: InputFn,
    output_fn: OutputFn,
    must_exist: bool = True,
    allowed_exts: Iterable[str] | None = None,
    default: str | None = None,
) -> str:
    """Ask for a filesystem path with optional existence / extension checks.

    Re-prompts on a missing file or an unsupported extension so the caller
    always gets back a usable path (or a ``WizardExit`` if the stream ends)."""
    exts = tuple(e.lower() for e in allowed_exts) if allowed_exts else None
    while True:
        raw = ask(prompt, input_fn=input_fn, output_fn=output_fn, default=default)
        # Strip surrounding quotes that shells / drag-and-drop often add.
        path = raw.strip().strip('"').strip("'")
        if exts is not None:
            ext = os.path.splitext(path)[1].lower()
            if ext not in exts:
                pretty = ", ".join(exts)
                output_fn(f"That's a '{ext or 'no'}' file; please choose one of: {pretty}.")
                continue
        if must_exist and not os.path.isfile(path):
            output_fn(f"No file found at: {path}")
            continue
        return path


# ---------------------------------------------------------------------------
# Individual wizard steps (each returns the piece of Config it owns)
# ---------------------------------------------------------------------------
def _prompt_input_path(
    *,
    input_fn: InputFn,
    output_fn: OutputFn,
    soffice_finder: Callable[[], str | None],
    platform: str,
) -> str:
    """Ask for the input document, looping until we get a supported, readable
    file. A .docx with no LibreOffice available is refused with an install hint
    (DOCX input is normalized through LibreOffice), letting the user pick
    another file instead of failing deep in the pipeline."""
    while True:
        path = ask_path(
            "Path to the document you want to convert (.pdf, .docx, .txt)",
            input_fn=input_fn,
            output_fn=output_fn,
            allowed_exts=SUPPORTED_INPUT_EXTS,
        )
        if path.lower().endswith(".docx") and soffice_finder() is None:
            hint = _LIBREOFFICE_HINTS.get(platform, _LIBREOFFICE_HINTS["linux"])
            output_fn("")
            output_fn("DOCX input needs LibreOffice, which I can't find on this system.")
            output_fn(f"  Install it with:  {hint}")
            output_fn("Then restart the wizard, or choose a .pdf / .txt file instead.")
            output_fn("")
            continue
        return path


def _prompt_handwriting(
    *,
    input_fn: InputFn,
    output_fn: OutputFn,
    glyph_loader: Callable[[str], GlyphSet],
) -> str | None:
    """Optionally load the user's own handwriting sample sheet.

    Returns the sample-sheet path to store in ``Config.handwriting_image`` or
    ``None`` to use the built-in font. A load/coverage failure is reported and
    the user is offered a graceful fall back to the built-in font rather than
    being blocked."""
    if not ask_yes_no(
        "Use your own handwriting from a sample sheet?",
        input_fn=input_fn,
        output_fn=output_fn,
        default=False,
    ):
        return None

    while True:
        path = ask_path(
            "Path to your handwriting sample-sheet image",
            input_fn=input_fn,
            output_fn=output_fn,
        )
        try:
            glyphs = glyph_loader(path)
            found, expected = glyphs.coverage()
        except Exception as exc:  # noqa: BLE001 - any decode/segmentation error
            output_fn(f"Couldn't read that sample sheet: {exc}")
            if ask_yes_no(
                "Try a different image?",
                input_fn=input_fn,
                output_fn=output_fn,
                default=True,
            ):
                continue
            output_fn("Continuing with the built-in font.")
            return None
        output_fn(
            f"Found {found}/{expected} characters; the rest use the built-in font."
        )
        return path


def _prompt_ink_color(*, input_fn: InputFn, output_fn: OutputFn) -> str:
    """Return an ink-color value for Config: either "original" or a hex string."""
    labels = [
        "Match original document colors",
        "Blue-black ink (classic pen)",
        "Black ink",
        "Blue ink",
        "Custom hex color",
    ]
    values = ["original", "#1a1a6e", "#1a1a1a", "#1a3a8e", None]
    idx = ask_choice(
        "Ink color:",
        labels,
        input_fn=input_fn,
        output_fn=output_fn,
        default=0,
    )
    if values[idx] is not None:
        return values[idx]
    # Custom hex: keep asking until it looks like a #rrggbb value.
    while True:
        hex_val = ask(
            "Enter a hex color like #1a1a6e",
            input_fn=input_fn,
            output_fn=output_fn,
        )
        if not hex_val.startswith("#"):
            hex_val = "#" + hex_val
        body = hex_val[1:]
        if len(body) == 6 and all(c in "0123456789abcdefABCDEF" for c in body):
            return hex_val.lower()
        output_fn("That doesn't look like a 6-digit hex color. Try again.")


def _prompt_mode(*, input_fn: InputFn, output_fn: OutputFn) -> LayoutMode:
    """Return the chosen LayoutMode (FIT is the recommended default)."""
    idx = ask_choice(
        "Layout mode:",
        [
            "Fit - keep pages identical to the original (recommended)",
            "Reflow - let lines rewrap within their text block",
        ],
        input_fn=input_fn,
        output_fn=output_fn,
        default=0,
    )
    return LayoutMode.FIT if idx == 0 else LayoutMode.REFLOW


def _prompt_output_format(*, input_fn: InputFn, output_fn: OutputFn) -> OutputFormat:
    """Return the chosen OutputFormat, noting that TXT/MD are text, not
    handwriting."""
    output_fn("Note: TXT and MD deliver the document's text content, not handwriting.")
    formats = [OutputFormat.PDF, OutputFormat.DOCX, OutputFormat.TXT, OutputFormat.MD]
    idx = ask_choice(
        "Output format:",
        ["PDF (handwriting)", "DOCX (handwriting)", "TXT (text only)", "MD (text only)"],
        input_fn=input_fn,
        output_fn=output_fn,
        default=0,
    )
    return formats[idx]


def _default_output_path(input_path: str, fmt: OutputFormat) -> str:
    """Suggest ``<input-stem>-handwritten.<ext>`` next to the input file."""
    stem = os.path.splitext(input_path)[0]
    return f"{stem}-handwritten.{fmt.value}"


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_wizard(
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
    convert_fn: Callable[[str, str, Config], str] = convert,
    *,
    glyph_loader: Callable[[str], GlyphSet] = GlyphSet.from_sheet,
    soffice_finder: Callable[[], str | None] = find_soffice,
    platform: str | None = None,
) -> int:
    """Run the interactive conversion wizard.

    All external effects are injected so the whole flow is testable:
    ``input_fn``/``output_fn`` for I/O, ``convert_fn`` for the actual
    conversion, ``glyph_loader`` for the handwriting sample, and
    ``soffice_finder`` for DOCX-input detection.

    Returns a process-style exit code: 0 on a successful (or user-cancelled)
    run, 1 if a conversion attempt failed and the user chose not to retry.
    """
    import sys

    plat = platform if platform is not None else sys.platform

    try:
        output_fn("=" * 60)
        output_fn("  Handscrybe - turn your document into handwriting")
        output_fn("=" * 60)
        output_fn("Answer a few questions and I'll do the rest. Press Ctrl-C to quit.")
        output_fn("")

        input_path = _prompt_input_path(
            input_fn=input_fn,
            output_fn=output_fn,
            soffice_finder=soffice_finder,
            platform=plat,
        )
        handwriting = _prompt_handwriting(
            input_fn=input_fn, output_fn=output_fn, glyph_loader=glyph_loader
        )
        ink_color = _prompt_ink_color(input_fn=input_fn, output_fn=output_fn)
        mode = _prompt_mode(input_fn=input_fn, output_fn=output_fn)
        fmt = _prompt_output_format(input_fn=input_fn, output_fn=output_fn)
        output_path = ask_path(
            "Where should I write the result?",
            input_fn=input_fn,
            output_fn=output_fn,
            must_exist=False,
            default=_default_output_path(input_path, fmt),
        )

        cfg = Config(
            handwriting_image=handwriting,
            ink_color=ink_color,
            mode=mode,
            output_format=fmt,
        )

        # Summary + confirmation before doing any real work.
        output_fn("")
        output_fn("Here's what I'll do:")
        output_fn(f"  Input:    {input_path}")
        output_fn(f"  Output:   {output_path}  ({fmt.value})")
        output_fn(f"  Ink:      {ink_color}")
        output_fn(f"  Layout:   {mode.value}")
        hw_label = handwriting if handwriting else "built-in font"
        output_fn(f"  Handwriting: {hw_label}")
        output_fn("")
        if not ask_yes_no(
            "Convert now?", input_fn=input_fn, output_fn=output_fn, default=True
        ):
            output_fn("No problem - nothing was written. Bye!")
            return 0

        # Conversion loop: on an expected failure, offer a retry.
        while True:
            output_fn("Converting...")
            try:
                result = convert_fn(input_path, output_path, cfg)
            except (FileNotFoundError, ValueError, RuntimeError) as exc:
                output_fn(f"Conversion failed: {exc}")
                if ask_yes_no(
                    "Try again?", input_fn=input_fn, output_fn=output_fn, default=False
                ):
                    continue
                return 1
            output_fn(f"Done! Your handwriting is at: {result}")
            return 0

    except WizardExit:
        return 0
    except KeyboardInterrupt:
        output_fn("")
        output_fn("Cancelled - nothing was written.")
        return 0


def main() -> int:
    """Console entry point: run the wizard with real I/O."""
    return run_wizard()


if __name__ == "__main__":
    raise SystemExit(main())
