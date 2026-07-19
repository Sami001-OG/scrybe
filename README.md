<div align="center">

# Handscrybe

**Turn any typed document into handwriting — without losing the layout.**

Your PDF, DOCX, or TXT comes back handwritten, on the same pages and in the same
places. Tables, borders, and colors stay exactly where they were.

[![npm](https://img.shields.io/npm/v/handscrybe?label=npm&color=0a7)](https://www.npmjs.com/package/handscrybe)
&nbsp;[![license](https://img.shields.io/badge/license-MIT-0a7)](LICENSE)
&nbsp;[![node](https://img.shields.io/badge/node-%E2%89%A5%2016-0a7)](https://nodejs.org)
&nbsp;[![python](https://img.shields.io/badge/python-%E2%89%A5%203.12-0a7)](https://www.python.org)

```bash
npm install -g handscrybe
handscrybe
```

</div>

<br>

## Overview

Most "text to handwriting" tools drop your words onto a blank ruled page, and the
headings, tables, columns, and spacing are gone. Handscrybe works the other way
around: your original document *is* the canvas.

It reads your source at real page coordinates, measures where every line of text
sits, and draws handwriting into that exact space. The page you get back looks
like the one you started with — only handwritten.

You can write with the clean built-in handwriting font, or supply a photo of your
own sample sheet and Handscrybe will write in your letters.

<br>

## What it preserves

| | |
| --- | --- |
| **Layout** | Text stays exactly where it was. Nothing reflows unless you ask. |
| **Pagination** | One source page in, one page out. No drift. |
| **Graphics** | Table borders, rules, boxes, and shapes come through intact. |
| **Color** | Keeps each line's original ink, or force a single pen color. |
| **Styling** | Bold and italic are honored — real faces if provided, else synthesized. |
| **Your hand** | Add a photo of a sample sheet and it composites in your own glyphs. |

<br>

## Getting started

```bash
npm install -g handscrybe
```

That is the only setup step. The first time you run `handscrybe`, it prepares
everything on its own:

- **Python** — the engine that powers Handscrybe. It reuses a Python 3.12 or newer
  that you already have, or quietly downloads a private, self-contained copy into
  `~/.handscrybe`. No administrator rights are needed, and nothing is added to your
  PATH. This happens once and takes about a minute; every run afterward is instant.

- **LibreOffice** — optional, and needed only to read **DOCX input**. Handscrybe
  detects it and, if it is missing, tells you exactly how to add it. PDF and TXT
  input — and every output format — work without it.

  | Platform | Install LibreOffice |
  | --- | --- |
  | Windows | `winget install TheDocumentFoundation.LibreOffice` |
  | macOS | `brew install --cask libreoffice` |
  | Linux | `sudo apt install libreoffice` |

<br>

## Usage

### Interactive menu

Run the command with no arguments and Handscrybe guides you through the whole
process — which file to convert, your handwriting or the built-in font, ink color,
layout, output format, and where to save.

```bash
handscrybe
```

It offers three choices: convert a document, open the web app, or quit. Converting
asks a few plain questions and does the rest.

### Command line

For scripts and automation, pass the input and output paths directly.

```bash
# Simplest: PDF in, handwriting PDF out
handscrybe input.pdf output.pdf

# DOCX in, handwriting PDF out
handscrybe report.docx report_hand.pdf

# Write it in your own handwriting
handscrybe input.pdf output.pdf --handwriting-image my_hand.png

# Choose the delivered format, or let the extension decide
handscrybe input.pdf notes.docx --to docx
handscrybe input.pdf notes.txt

# Force a single pen color and let text reflow
handscrybe input.pdf output.pdf --ink "#1a1a6e" --mode reflow
```

<details>
<summary><b>All command-line options</b></summary>

<br>

| Option | What it does |
| --- | --- |
| `--to {pdf,docx,txt,md}` | Output format. Defaults to the output file's extension, otherwise PDF. |
| `--handwriting-image PATH` | Photo or scan of your sample sheet (A–Z, a–z, 0–9, one row each). |
| `--ink {original,#hex}` | Keep original colors, or force one pen color. |
| `--mode {fit,reflow}` | `fit` keeps pages identical; `reflow` re-wraps lines. |
| `--font PATH` | Use a different regular handwriting `.ttf`. |
| `--font-bold PATH` / `--no-bold-font` | Provide a bold face, or always synthesize bold. |
| `--italic-font PATH` | Provide an italic face (otherwise italic is sheared). |
| `--size-scale N` | Multiply every font size (for example, `1.1` for slightly larger writing). |
| `--soffice PATH` | Point at the LibreOffice binary if it is not detected automatically. |

</details>

### Web app

```bash
handscrybe-web
```

This prints a local URL, opens your browser, and picks a free port if 5000 is
busy. Drag in a document, optionally add a handwriting sample, choose ink, layout,
and format, then download the result. The host and port are overridable:

```bash
HANDSCRYBE_HOST=0.0.0.0 HANDSCRYBE_PORT=8080 handscrybe-web
```

The web app runs locally on your own machine and is not exposed to the internet by
default. Binding it to `0.0.0.0` makes it reachable from your network — only do
that on a network you trust.

### Python library

```python
from handscrybe.config import Config, OutputFormat
from handscrybe.pipeline import convert

cfg = Config(
    handwriting_image="my_hand.png",   # optional; omit for the built-in font
    ink_color="#1a1a6e",               # or "original"
    output_format=OutputFormat.PDF,    # or DOCX / TXT / MD; None infers from path
)
convert("input.pdf", "output.pdf", cfg)
```

<br>

## Using your own handwriting

Give Handscrybe a photo or scan of a sheet with three rows of characters:

```
A B C D E F G H I J K L M N O P Q R S T U V W X Y Z
a b c d e f g h i j k l m n o p q r s t u v w x y z
0 1 2 3 4 5 6 7 8 9
```

It segments the sheet into individual glyphs and composites them into your
document. Any character you did not write — punctuation or accented letters —
falls back to the built-in font automatically, so even a partial sheet works. The
web app shows how many of the 62 glyphs it found.

<br>

## Supported formats

| | PDF | DOCX | TXT |
| --- | :---: | :---: | :---: |
| **Input** | Yes | Yes (needs LibreOffice) | Yes |
| **Output** | Yes | Yes | Yes (plus Markdown) |

Handwriting is visual, so **PDF and DOCX carry the actual handwriting**. TXT and
Markdown are text-only formats — they deliver the document's text content rather
than the handwriting itself.

<br>

## How it works

1. **Normalize** — DOCX is rendered to PDF with LibreOffice so Handscrybe has exact
   page geometry. PDF and TXT pass straight through.
2. **Parse** — every text span (position, size, style, color) and vector drawing is
   extracted from each page.
3. **Fit** — each line is measured in the handwriting font and fitted into the space
   the original text occupied. In `fit` mode the line count never changes; `reflow`
   re-wraps within a block.
4. **Render** — handwriting is drawn onto a copy of the source page. Your own glyphs
   are composited in where available, otherwise the built-in font is used.
5. **Deliver** — the handwriting PDF is produced first, then converted to the format
   you asked for.

The source and normalized files are never modified. Only your chosen output file is
written.

<br>

## Development

```bash
git clone https://github.com/Sami001-OG/scrybe.git
cd scrybe
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS / Linux: source .venv/bin/activate
pip install -e .

python -m pytest -q        # Python engine plus CLI, web, and wizard tests
node npm/test/smoke.js     # npm launcher smoke checks (no provisioning needed)
```

**Project layout**

- `src/handscrybe/` — the Python engine: parsing, layout, rendering, glyph
  extraction, CLI, wizard, and web app. The bundled handwriting font lives under
  `fonts/`.
- `npm/` — the dependency-free Node launcher that provisions Python and hands off to
  the engine. `npm/bin/handscrybe.js` is the `handscrybe` command.

Tests that require LibreOffice (DOCX input) skip automatically when it is not
installed, so the suite stays green on any machine.

<br>

## License

Released under the [MIT License](LICENSE).
