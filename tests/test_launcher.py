"""Tests for the interactive mode menu (`launcher.py`).

The launcher is the first thing a user sees after `scrybe` provisions itself: a
menu that dispatches to the guided wizard or the web app. Like the wizard, every
side effect is injectable, so these tests drive the menu with scripted answers
and fakes — no real stdin, no real conversion, no Flask server.

The one behavior worth guarding hardest is that a single Python process owns all
of stdin: the menu reads a choice, then hands the SAME ``input_fn`` to the
wizard. These tests assert that hand-off happens (the wizard receives the
injected I/O) because the whole module exists to avoid the dual-reader EOF bug.
"""

from __future__ import annotations

import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from doc_to_hand.launcher import run_menu  # noqa: E402


class ScriptedInput:
    """Fake ``input_fn`` returning queued answers; raises EOFError when spent."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self._answers:
            raise EOFError
        return self._answers.pop(0)


def collect_output():
    lines = []
    return lines, lambda msg="": lines.append(str(msg))


# ---------------------------------------------------------------------------
# Menu dispatch
# ---------------------------------------------------------------------------
def test_quit_returns_zero_and_says_bye():
    out, out_fn = collect_output()
    rc = run_menu(
        ScriptedInput(["3"]),
        out_fn,
        run_wizard_fn=lambda **_: 0,
        run_web_fn=lambda **_: 0,
        soffice_finder=lambda: "soffice",
        platform="linux",
    )
    assert rc == 0
    assert any("Bye" in line for line in out)


def test_choice_1_runs_wizard_and_loops_back():
    calls = []

    def fake_wizard(**kwargs):
        calls.append(kwargs)
        return 0

    out, out_fn = collect_output()
    # Convert once, then quit.
    rc = run_menu(
        ScriptedInput(["1", "3"]),
        out_fn,
        run_wizard_fn=fake_wizard,
        run_web_fn=lambda **_: 0,
        soffice_finder=lambda: None,
        platform="linux",
    )
    assert rc == 0
    assert len(calls) == 1  # wizard ran exactly once
    # The wizard must receive the SAME injected I/O (single-stdin-owner contract).
    assert "input_fn" in calls[0] and "output_fn" in calls[0]


def test_choice_2_starts_web_then_loops_back():
    web_calls = []

    out, out_fn = collect_output()
    rc = run_menu(
        ScriptedInput(["2", "3"]),
        out_fn,
        run_wizard_fn=lambda **_: 0,
        run_web_fn=lambda: web_calls.append(True),
        soffice_finder=lambda: "soffice",
        platform="linux",
    )
    assert rc == 0
    assert web_calls == [True]


def test_invalid_choice_reprompts():
    out, out_fn = collect_output()
    rc = run_menu(
        ScriptedInput(["banana", "3"]),
        out_fn,
        run_wizard_fn=lambda **_: 0,
        run_web_fn=lambda **_: 0,
        soffice_finder=lambda: "soffice",
        platform="linux",
    )
    assert rc == 0
    assert any("enter 1, 2, or 3" in line.lower() for line in out)


def test_eof_exits_cleanly():
    # Empty script -> input_fn raises EOFError immediately (closed stdin).
    out, out_fn = collect_output()
    rc = run_menu(
        ScriptedInput([]),
        out_fn,
        run_wizard_fn=lambda **_: 0,
        run_web_fn=lambda **_: 0,
        soffice_finder=lambda: "soffice",
        platform="linux",
    )
    assert rc == 0  # EOF is a clean exit, never a traceback


# ---------------------------------------------------------------------------
# LibreOffice reporting (informational, never blocking)
# ---------------------------------------------------------------------------
def test_reports_libreoffice_present():
    out, out_fn = collect_output()
    run_menu(
        ScriptedInput(["3"]),
        out_fn,
        run_wizard_fn=lambda **_: 0,
        soffice_finder=lambda: "/usr/bin/soffice",
        platform="linux",
    )
    assert any("DOCX input is available" in line for line in out)


def test_reports_libreoffice_missing_with_platform_hint():
    out, out_fn = collect_output()
    run_menu(
        ScriptedInput(["3"]),
        out_fn,
        run_wizard_fn=lambda **_: 0,
        soffice_finder=lambda: None,
        platform="win32",
    )
    text = "\n".join(out)
    assert "DOCX" in text
    # Windows hint is surfaced so the user can enable DOCX input.
    assert "winget install TheDocumentFoundation.LibreOffice" in text


def test_missing_libreoffice_does_not_block_conversion():
    ran = []
    out, out_fn = collect_output()
    rc = run_menu(
        ScriptedInput(["1", "3"]),
        out_fn,
        run_wizard_fn=lambda **_: ran.append(True) or 0,
        soffice_finder=lambda: None,
        platform="darwin",
    )
    assert rc == 0
    assert ran == [True]  # conversion still offered without LibreOffice
