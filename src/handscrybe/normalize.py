"""Input normalization for the document-to-handwriting pipeline.

WHY THIS EXISTS
---------------
The pipeline needs real page geometry: glyph positions, line boxes, page
sizes. A PDF carries that layout baked in. A raw DOCX does NOT -- a .docx is
just zipped XML describing content and styles, with no coordinates. The actual
positions only exist after a layout engine (Word / LibreOffice) flows the text
into pages. So we cannot parse a DOCX for coordinates directly; we must first
render it to PDF.

We use LibreOffice in headless mode to do that rendering. This module is the
single shared entry point for the rest of the pipeline: everything downstream
of ``normalize`` is PDF-only and never has to care whether the user handed us a
PDF or a DOCX.

Kept dependency-free on purpose (stdlib only: os, shutil, subprocess). The
LibreOffice binary path is passed in from config via ``soffice_cmd`` rather than
imported, so this module has no project-internal imports.
"""

from __future__ import annotations

import os
import shutil
import subprocess

# Recognized input extensions -> normalized format tag.
_SUPPORTED = {".pdf": "pdf", ".docx": "docx", ".txt": "txt"}

# Common Windows install locations, checked after PATH lookup.
_WINDOWS_SOFFICE_PATHS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


def detect_format(path: str) -> str:
    """Return "pdf", "docx" or "txt" based on extension. Raise ValueError otherwise."""
    ext = os.path.splitext(path)[1].lower()
    fmt = _SUPPORTED.get(ext)
    if fmt is None:
        supported = ", ".join(sorted(_SUPPORTED))
        raise ValueError(
            f"Unsupported input {path!r}: extension {ext!r} not one of {supported}"
        )
    return fmt


def find_soffice(explicit: str | None = None) -> str | None:
    """Locate the LibreOffice binary.

    Order: explicit arg, then ``soffice``/``soffice.exe`` on PATH, then common
    Windows install locations. Return the path or None if not found.
    """
    # Explicit config value wins if it actually points at something runnable.
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        found = shutil.which(explicit)
        if found:
            return found

    # The npm launcher detects LibreOffice and passes its path via this env var,
    # so a soffice found by the Node side is honored by every Python entry point
    # (CLI, wizard, webapp) without threading it through each call site.
    env_soffice = os.environ.get("HANDSCRYBE_SOFFICE")
    if env_soffice:
        if os.path.isfile(env_soffice):
            return env_soffice
        found = shutil.which(env_soffice)
        if found:
            return found

    # PATH lookup handles the .exe suffix on Windows for us.
    for name in ("soffice", "soffice.exe"):
        found = shutil.which(name)
        if found:
            return found

    # Fall back to the default installer locations.
    for candidate in _WINDOWS_SOFFICE_PATHS:
        if os.path.isfile(candidate):
            return candidate

    return None


def _path_to_file_uri(path: str) -> str:
    """Convert a local path to a ``file:///`` URI LibreOffice accepts.

    Handles Windows paths with spaces/backslashes (e.g. "D:\\doc to hand").
    """
    abs_path = os.path.abspath(path)
    # pathname2url percent-encodes spaces and normalizes separators to "/".
    from urllib.request import pathname2url

    return "file:" + pathname2url(abs_path)


def docx_to_pdf(
    docx_path: str,
    out_dir: str,
    soffice_cmd: str | None = None,
    timeout: float = 120,
) -> str:
    """Convert a DOCX to PDF using LibreOffice headless. Return the PDF path.

    Runs::

        soffice --headless --convert-to pdf --outdir <out_dir> <docx_path>

    Raises RuntimeError if soffice is missing, if the subprocess fails, or if
    the expected output file is not produced.
    """
    soffice = find_soffice(soffice_cmd)
    if soffice is None:
        raise RuntimeError(
            "LibreOffice (soffice) was not found. Install LibreOffice or pass "
            "its path via --soffice / the soffice_cmd argument."
        )

    os.makedirs(out_dir, exist_ok=True)

    # A dedicated temp user profile avoids the "LibreOffice is already running"
    # lock that otherwise breaks headless conversion when a desktop instance is
    # open. The value must be a file URI, not a plain path.
    profile_dir = os.path.join(out_dir, ".lo_profile")
    profile_uri = _path_to_file_uri(profile_dir)

    # Command passed as a LIST (never shell=True) so paths with spaces such as
    # "D:\\doc to hand" are handled without quoting/injection issues.
    cmd = [
        soffice,
        f"-env:UserInstallation={profile_uri}",
        "--headless",
        "--convert-to",
        "pdf",
        "--outdir",
        out_dir,
        docx_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"LibreOffice conversion timed out after {timeout}s for {docx_path!r}."
        ) from exc

    if result.returncode != 0:
        raise RuntimeError(
            f"LibreOffice conversion failed (exit {result.returncode}) for "
            f"{docx_path!r}.\nstderr:\n{result.stderr.strip()}"
        )

    # LibreOffice names the output <basename>.pdf in out_dir regardless of the
    # source directory, so derive the expected path from the input basename.
    base = os.path.splitext(os.path.basename(docx_path))[0]
    pdf_path = os.path.join(out_dir, base + ".pdf")
    if not os.path.isfile(pdf_path):
        raise RuntimeError(
            f"LibreOffice reported success but the expected output {pdf_path!r} "
            f"was not produced.\nstdout:\n{result.stdout.strip()}"
        )

    return pdf_path


def txt_to_pdf(txt_path: str, out_dir: str) -> str:
    """Render a plain-text file to a simple PDF using PyMuPDF. Return the path.

    Unlike DOCX, plain text has no styling or layout to preserve, so we don't
    need LibreOffice: we lay the text onto US-Letter pages with a standard
    margin and monospaced-ish flow, letting PyMuPDF's text box handle wrapping
    and pagination. This gives the rest of the pipeline real coordinates to work
    from, exactly like any other PDF. Encoding is read as UTF-8 with a permissive
    fallback so odd files don't abort the run."""
    import fitz  # local import keeps this module import-light

    os.makedirs(out_dir, exist_ok=True)
    with open(txt_path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    # US Letter with a 72pt (1 inch) margin, 11pt text — ordinary document defaults.
    page_w, page_h = 612.0, 792.0
    margin = 72.0
    fontsize = 11.0
    line_height = fontsize * 1.4  # comfortable leading
    max_width = page_w - 2 * margin
    lines_per_page = int((page_h - 2 * margin) // line_height)

    font = fitz.Font("helv")

    # Wrap each source line to the text width, word by word, so long paragraphs
    # flow instead of running off the page. Explicit newlines are preserved as
    # hard breaks (and blank lines stay blank). This is a word-wrap by measured
    # width, which is deterministic and needs no textbox return-value guessing.
    def wrap(line: str) -> list[str]:
        if line == "":
            return [""]
        words = line.split(" ")
        out: list[str] = []
        cur = ""
        for word in words:
            trial = word if cur == "" else cur + " " + word
            if font.text_length(trial, fontsize=fontsize) <= max_width or cur == "":
                cur = trial
            else:
                out.append(cur)
                cur = word
        out.append(cur)
        return out

    wrapped: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        wrapped.extend(wrap(raw_line))

    doc = fitz.open()
    for i in range(0, max(1, len(wrapped)), lines_per_page):
        page = doc.new_page(width=page_w, height=page_h)
        chunk = wrapped[i : i + lines_per_page]
        y = margin + fontsize  # first baseline
        for ln in chunk:
            if ln:
                page.insert_text(
                    (margin, y), ln, fontsize=fontsize, fontname="helv"
                )
            y += line_height

    base = os.path.splitext(os.path.basename(txt_path))[0]
    pdf_path = os.path.join(out_dir, base + ".pdf")
    doc.save(pdf_path)
    doc.close()
    return pdf_path


def normalize(
    input_path: str,
    work_dir: str,
    soffice_cmd: str | None = None,
) -> tuple[str, str]:
    """Top-level entry. Return ``(pdf_path, source_format)``.

    - PDF input: returned as-is (downstream opens read-only, writes elsewhere).
    - DOCX input: converted into ``work_dir`` via LibreOffice.
    - TXT input: rendered to a simple PDF via PyMuPDF (no LibreOffice needed).
    """
    fmt = detect_format(input_path)
    if fmt == "pdf":
        # No copy needed -- downstream never mutates the source PDF.
        return input_path, "pdf"

    if fmt == "txt":
        pdf_path = txt_to_pdf(input_path, work_dir)
        return pdf_path, "txt"

    # DOCX: render to PDF so the rest of the pipeline has real coordinates.
    pdf_path = docx_to_pdf(input_path, work_dir, soffice_cmd=soffice_cmd)
    return pdf_path, "docx"
