"""Interactive Handscrybe menu — the mode selector shown after `handscrybe` starts.

The npm ``handscrybe`` command provisions Python and a venv, then hands off to THIS
module. Keeping the menu (and every prompt) in a single Python process avoids a
subtle but fatal bug: if Node reads stdin to show the menu and then spawns
Python for the wizard, Node's line-buffered reader swallows the wizard's
answers and Python sees EOF. One process, one stdin reader — no lost input.

Responsibilities:
  * Show a friendly menu: convert a document (guided wizard) or open the web app.
  * Report whether LibreOffice was detected (DOCX *input* depends on it) and, if
    not, print the platform-specific install hint — without blocking anything
    else.
  * Dispatch to :func:`handscrybe.wizard.run_wizard` or the Flask web server.

Like the wizard, all I/O is injectable so the whole loop is testable with
scripted answers and fakes.
"""

from __future__ import annotations

import sys
from typing import Callable

from .normalize import find_soffice

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]

# Platform-specific install hint for LibreOffice (needed for DOCX *input* only).
_LIBREOFFICE_HINTS = {
    "win32": "winget install TheDocumentFoundation.LibreOffice",
    "darwin": "brew install --cask libreoffice",
    "linux": "sudo apt install libreoffice",
}


def _hint_for(platform: str) -> str:
    for key, hint in _LIBREOFFICE_HINTS.items():
        if platform.startswith(key):
            return hint
    return _LIBREOFFICE_HINTS["linux"]


def _banner(output_fn: OutputFn) -> None:
    output_fn("=" * 60)
    output_fn("  Handscrybe — turn typed documents into handwriting")
    output_fn("=" * 60)


def run_menu(
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
    *,
    run_wizard_fn: Callable[..., int] | None = None,
    run_web_fn: Callable[..., int] | None = None,
    soffice_finder: Callable[[], "str | None"] = find_soffice,
    platform: str | None = None,
) -> int:
    """Show the mode menu and dispatch. Returns a process exit code.

    ``run_wizard_fn`` / ``run_web_fn`` are injected so tests can drive the menu
    without launching a real conversion or web server. In normal use they
    default to the real wizard and web server (imported lazily so a test never
    triggers Flask import).
    """
    plat = platform if platform is not None else sys.platform

    if run_wizard_fn is None:
        from .wizard import run_wizard as run_wizard_fn  # type: ignore[assignment]

    # LibreOffice status is reported once up front so the user knows what's
    # available before choosing. Absence is informational, never blocking.
    soffice = soffice_finder()

    try:
        _banner(output_fn)
        if soffice:
            output_fn("LibreOffice detected — DOCX input is available.")
        else:
            output_fn("Note: LibreOffice was not found, so DOCX *input* is off.")
            output_fn("      PDF and TXT input work, and every output format works.")
            output_fn(f"      To enable DOCX input: {_hint_for(plat)}")
        output_fn("")

        while True:
            output_fn("What would you like to do?")
            output_fn("  1) Convert a document   (guided, step by step)")
            output_fn("  2) Open the web app     (drag and drop in your browser)")
            output_fn("  3) Quit")
            output_fn("")

            try:
                choice = input_fn("Choose 1, 2, or 3: ").strip()
            except EOFError:
                output_fn("")
                return 0
            except KeyboardInterrupt:
                output_fn("")
                output_fn("Bye!")
                return 0

            if choice in ("1", "convert"):
                rc = run_wizard_fn(input_fn=input_fn, output_fn=output_fn)
                output_fn("")
                # Loop back so the user can convert again or switch modes.
                continue
            if choice in ("2", "web"):
                if run_web_fn is None:
                    from .webapp import main as run_web_fn  # lazy: avoid Flask import in tests
                output_fn("Starting the web app — open the printed URL in your browser.")
                output_fn("Press Ctrl-C to stop the server and return here.")
                output_fn("")
                try:
                    run_web_fn()
                except KeyboardInterrupt:
                    pass
                output_fn("")
                continue
            if choice in ("3", "quit", "q", "exit"):
                output_fn("Bye!")
                return 0

            output_fn("Please enter 1, 2, or 3.")
            output_fn("")
    except KeyboardInterrupt:
        output_fn("")
        output_fn("Bye!")
        return 0


def main() -> int:
    """Console entry point: run the menu with real I/O."""
    return run_menu()


if __name__ == "__main__":
    raise SystemExit(main())
