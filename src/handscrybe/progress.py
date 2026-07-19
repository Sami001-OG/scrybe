"""A tiny, dependency-free terminal progress bar for the CLI and wizard.

The bar is driven by the pipeline's ``(fraction, message)`` progress callback, so
the percentage it shows always reflects real work done — never a synthetic timer.
It redraws in place with a carriage return and only when the visible state
actually changes, so it stays smooth without spamming the terminal, and it
degrades to nothing when stdout is not a TTY (piped/redirected output stays
clean, which matters for scripts and tests).

Usage::

    with TerminalProgress() as bar:
        convert(inp, out, cfg, progress=bar)

``bar`` is itself the ``(fraction, message)`` callable the pipeline expects.
"""

from __future__ import annotations

import sys
import time


class TerminalProgress:
    """A carriage-return progress bar usable as a ``(fraction, message)`` callback.

    Construct it, pass the instance straight to ``convert(..., progress=bar)``,
    and use it as a context manager so the line is finished cleanly (newline on
    success, or left in place on error) when the block exits.
    """

    def __init__(self, *, width: int = 28, stream=None, enabled: bool | None = None) -> None:
        self._width = width
        self._stream = stream if stream is not None else sys.stdout
        # Auto-detect: only animate on a real terminal. Callers can force it
        # (e.g. tests) via ``enabled``.
        if enabled is None:
            enabled = bool(getattr(self._stream, "isatty", lambda: False)())
        self._enabled = enabled
        self._start = time.monotonic()
        self._last_line = ""
        self._closed = False

    def __call__(self, fraction: float, message: str) -> None:
        """Render the bar for ``fraction`` in 0..1 with a trailing ``message``."""
        if not self._enabled or self._closed:
            return
        frac = 0.0 if fraction < 0 else 1.0 if fraction > 1 else fraction
        filled = int(round(frac * self._width))
        bar = "#" * filled + "-" * (self._width - filled)
        pct = int(round(frac * 100))
        elapsed = time.monotonic() - self._start
        line = f"  [{bar}] {pct:3d}%  {message}  ({elapsed:4.1f}s)"
        # Only repaint when the text changes, and pad to erase any leftover from
        # a previously longer line.
        if line != self._last_line:
            pad = max(0, len(self._last_line) - len(line))
            self._stream.write("\r" + line + " " * pad)
            self._stream.flush()
            self._last_line = line

    def finish(self, message: str = "Done") -> None:
        """Complete the bar at 100% and drop to a new line."""
        if self._closed:
            return
        if self._enabled:
            self(1.0, message)
            self._stream.write("\n")
            self._stream.flush()
        self._closed = True

    def clear(self) -> None:
        """Erase the current bar line without printing a newline.

        Used when an error interrupts progress so the caller can print its own
        message on a clean line."""
        if self._closed:
            return
        if self._enabled and self._last_line:
            self._stream.write("\r" + " " * len(self._last_line) + "\r")
            self._stream.flush()
        self._closed = True

    def __enter__(self) -> "TerminalProgress":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # On success, finish the line at 100%; on error, clear it so the caller's
        # error message isn't tangled with a half-drawn bar.
        if exc_type is None:
            self.finish()
        else:
            self.clear()
