"""Tests for the interactive conversion wizard (`wizard.py`).

The wizard is written so every side effect is injectable, so these tests never
touch real stdin, never run a real conversion, and never call external tools.

- ``ScriptedInput`` feeds canned answers in order (simulating a user typing).
- ``collect_output`` gathers everything the wizard "prints".
- ``FakeConvert`` records the ``(input_path, output_path, config)`` it was
  handed and returns the output path, standing in for ``pipeline.convert``.
- ``FakeGlyphs`` / a fake ``glyph_loader`` stand in for handwriting-sheet
  segmentation so the handwriting path is fast and deterministic.
"""

from __future__ import annotations

import os
import sys

import pytest

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from doc_to_hand.config import Config, LayoutMode, OutputFormat  # noqa: E402
from doc_to_hand.wizard import (  # noqa: E402
    ask_choice,
    ask_yes_no,
    run_wizard,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class ScriptedInput:
    """A fake ``input_fn`` that returns queued answers in order.

    Raises ``EOFError`` when exhausted, mirroring what a closed stdin does, so a
    test that under-scripts fails loudly instead of hanging."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.prompts = []

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if not self._answers:
            raise EOFError("no more scripted answers")
        return self._answers.pop(0)


class Collector:
    """A fake ``output_fn`` that records every printed line."""

    def __init__(self):
        self.lines = []

    def __call__(self, text=""):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)


class FakeConvert:
    """Records the arguments of each call and returns the output path."""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self._result = result
        self._raises = raises

    def __call__(self, input_path, output_path, config):
        self.calls.append((input_path, output_path, config))
        if self._raises is not None:
            raise self._raises
        return self._result if self._result is not None else output_path


class FakeGlyphs:
    """Stand-in for a segmented GlyphSet with a fixed coverage."""

    def __init__(self, found=60, expected=62):
        self._cov = (found, expected)

    def coverage(self, rows=None):
        return self._cov


def _existing_file(tmp_path, name):
    p = tmp_path / name
    p.write_text("hello", encoding="utf-8")
    return str(p)


def _no_soffice():
    return None


def _has_soffice():
    return "/usr/bin/soffice"


# ---------------------------------------------------------------------------
# Primitive-level tests
# ---------------------------------------------------------------------------
def test_ask_choice_reprompts_on_invalid_then_accepts():
    inp = ScriptedInput(["9", "abc", "2"])
    out = Collector()
    idx = ask_choice("Pick:", ["a", "b", "c"], input_fn=inp, output_fn=out)
    assert idx == 1  # zero-based for the "2" choice
    assert any("between 1 and 3" in ln for ln in out.lines)


def test_ask_choice_empty_uses_default():
    inp = ScriptedInput([""])
    out = Collector()
    assert ask_choice("Pick:", ["a", "b"], input_fn=inp, output_fn=out, default=1) == 1


def test_ask_yes_no_variants():
    out = Collector()
    assert ask_yes_no("q", input_fn=ScriptedInput(["y"]), output_fn=out) is True
    assert ask_yes_no("q", input_fn=ScriptedInput(["no"]), output_fn=out) is False
    assert ask_yes_no("q", input_fn=ScriptedInput([""]), output_fn=out, default=False) is False


# ---------------------------------------------------------------------------
# Full-flow tests
# ---------------------------------------------------------------------------
def test_happy_path_pdf_to_pdf(tmp_path):
    """(a) A full PDF -> PDF run builds the right Config and converts once."""
    src = _existing_file(tmp_path, "doc.pdf")
    out_path = str(tmp_path / "out.pdf")
    convert = FakeConvert()
    answers = [
        src,        # input path
        "n",        # use own handwriting? no
        "2",        # ink color -> blue-black (#1a1a6e)
        "1",        # layout -> Fit
        "1",        # output format -> PDF
        out_path,   # output path
        "y",        # confirm
    ]
    code = run_wizard(
        input_fn=ScriptedInput(answers),
        output_fn=Collector(),
        convert_fn=convert,
        soffice_finder=_no_soffice,
        platform="linux",
    )
    assert code == 0
    assert len(convert.calls) == 1
    in_path, written, cfg = convert.calls[0]
    assert in_path == src
    assert written == out_path
    assert isinstance(cfg, Config)
    assert cfg.output_format is OutputFormat.PDF
    assert cfg.mode is LayoutMode.FIT
    assert cfg.ink_color == "#1a1a6e"
    assert cfg.handwriting_image is None


def test_output_default_and_docx_format(tmp_path):
    """(b) Choosing DOCX output sets OutputFormat.DOCX; Enter accepts the
    suggested default output path."""
    src = _existing_file(tmp_path, "report.txt")
    convert = FakeConvert()
    answers = [
        src,   # input
        "n",   # handwriting? no
        "3",   # ink -> black
        "1",   # layout -> Fit
        "2",   # output -> DOCX
        "",    # accept default output path
        "y",   # confirm
    ]
    code = run_wizard(
        input_fn=ScriptedInput(answers),
        output_fn=Collector(),
        convert_fn=convert,
        soffice_finder=_has_soffice,
        platform="linux",
    )
    assert code == 0
    in_path, written, cfg = convert.calls[0]
    assert cfg.output_format is OutputFormat.DOCX
    # Default path is <stem>-handwritten.docx next to the input.
    assert written.endswith(os.path.join("", "report-handwritten.docx").lstrip(os.sep))
    assert written.endswith("report-handwritten.docx")


def test_use_my_handwriting_sets_config(tmp_path):
    """(c) Choosing "use my handwriting" with a valid sample sets
    config.handwriting_image and reports coverage."""
    src = _existing_file(tmp_path, "doc.pdf")
    sheet = _existing_file(tmp_path, "sample.png")
    convert = FakeConvert()
    out = Collector()

    def fake_loader(path):
        assert path == sheet
        return FakeGlyphs(found=60, expected=62)

    answers = [
        src,     # input
        "y",     # use own handwriting? yes
        sheet,   # sample sheet path
        "1",     # ink -> original
        "1",     # layout -> Fit
        "1",     # output -> PDF
        "",      # default output
        "y",     # confirm
    ]
    code = run_wizard(
        input_fn=ScriptedInput(answers),
        output_fn=out,
        convert_fn=convert,
        glyph_loader=fake_loader,
        soffice_finder=_no_soffice,
        platform="linux",
    )
    assert code == 0
    _, _, cfg = convert.calls[0]
    assert cfg.handwriting_image == sheet
    assert cfg.ink_color == "original"
    assert any("Found 60/62 characters" in ln for ln in out.lines)


def test_handwriting_load_failure_falls_back(tmp_path):
    """A bad sample sheet is reported and the user can continue with the
    built-in font instead of being blocked."""
    src = _existing_file(tmp_path, "doc.pdf")
    sheet = _existing_file(tmp_path, "bad.png")
    convert = FakeConvert()
    out = Collector()

    def failing_loader(path):
        raise ValueError("cannot decode image")

    answers = [
        src,     # input
        "y",     # use own handwriting? yes
        sheet,   # sample sheet path
        "n",     # try a different image? no -> fall back
        "1",     # ink -> original
        "1",     # layout -> Fit
        "1",     # output -> PDF
        "",      # default output
        "y",     # confirm
    ]
    code = run_wizard(
        input_fn=ScriptedInput(answers),
        output_fn=out,
        convert_fn=convert,
        glyph_loader=failing_loader,
        soffice_finder=_no_soffice,
        platform="linux",
    )
    assert code == 0
    _, _, cfg = convert.calls[0]
    assert cfg.handwriting_image is None
    assert any("Couldn't read" in ln for ln in out.lines)


def test_invalid_input_path_reprompts(tmp_path):
    """(d) An unsupported/missing input path re-prompts, then accepts a valid
    one, alongside an invalid menu choice that also re-prompts."""
    src = _existing_file(tmp_path, "doc.pdf")
    missing = str(tmp_path / "nope.pdf")
    bad_ext = _existing_file(tmp_path, "notes.rtf")
    convert = FakeConvert()
    out = Collector()
    answers = [
        bad_ext,   # wrong extension -> reprompt
        missing,   # right ext but missing -> reprompt
        src,       # good
        "n",       # handwriting? no
        "99",      # invalid ink choice -> reprompt
        "1",       # ink -> original
        "1",       # layout -> Fit
        "1",       # output -> PDF
        "",        # default output
        "y",       # confirm
    ]
    code = run_wizard(
        input_fn=ScriptedInput(answers),
        output_fn=out,
        convert_fn=convert,
        soffice_finder=_no_soffice,
        platform="linux",
    )
    assert code == 0
    assert len(convert.calls) == 1
    assert convert.calls[0][0] == src


def test_docx_input_without_soffice_warns_then_switches(tmp_path):
    """A .docx input with no LibreOffice warns (with install hint) and lets the
    user pick a different file."""
    docx = _existing_file(tmp_path, "memo.docx")
    pdf = _existing_file(tmp_path, "memo.pdf")
    convert = FakeConvert()
    out = Collector()
    answers = [
        docx,  # docx, no soffice -> warn + reprompt
        pdf,   # switch to pdf
        "n",   # handwriting? no
        "1",   # ink
        "1",   # layout
        "1",   # output
        "",    # default output
        "y",   # confirm
    ]
    code = run_wizard(
        input_fn=ScriptedInput(answers),
        output_fn=out,
        convert_fn=convert,
        soffice_finder=_no_soffice,
        platform="win32",
    )
    assert code == 0
    assert convert.calls[0][0] == pdf
    assert any("LibreOffice" in ln for ln in out.lines)
    assert any("winget install" in ln for ln in out.lines)


def test_convert_failure_is_caught(tmp_path):
    """(e) A convert failure (RuntimeError) is caught and reported without
    raising out of run_wizard; declining a retry returns exit code 1."""
    src = _existing_file(tmp_path, "doc.pdf")
    convert = FakeConvert(raises=RuntimeError("render blew up"))
    out = Collector()
    answers = [
        src,   # input
        "n",   # handwriting? no
        "1",   # ink
        "1",   # layout
        "1",   # output
        "",    # default output
        "y",   # confirm
        "n",   # retry? no
    ]
    code = run_wizard(
        input_fn=ScriptedInput(answers),
        output_fn=out,
        convert_fn=convert,
        soffice_finder=_no_soffice,
        platform="linux",
    )
    assert code == 1
    assert any("Conversion failed" in ln for ln in out.lines)
    assert any("render blew up" in ln for ln in out.lines)


def test_decline_confirmation_skips_convert(tmp_path):
    """Answering "no" at the confirmation prompt writes nothing."""
    src = _existing_file(tmp_path, "doc.pdf")
    convert = FakeConvert()
    answers = [src, "n", "1", "1", "1", "", "n"]
    code = run_wizard(
        input_fn=ScriptedInput(answers),
        output_fn=Collector(),
        convert_fn=convert,
        soffice_finder=_no_soffice,
        platform="linux",
    )
    assert code == 0
    assert convert.calls == []


def test_eof_exits_cleanly(tmp_path):
    """Running out of input (EOF) unwinds without a traceback and returns 0."""
    convert = FakeConvert()
    code = run_wizard(
        input_fn=ScriptedInput([]),  # empty -> EOFError on first prompt
        output_fn=Collector(),
        convert_fn=convert,
        soffice_finder=_no_soffice,
        platform="linux",
    )
    assert code == 0
    assert convert.calls == []
