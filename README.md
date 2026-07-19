# Scrybe

**Turn typed documents into handwriting — without losing the layout.**

Scrybe takes a PDF, DOCX, or TXT file and rewrites the text in handwriting,
keeping the page exactly as it was: same pages, same positions, same tables and
borders, same colors. Only the typed text is swapped for handwriting. Bring your
own handwriting (a photo of a sample sheet) and it uses *your* letters; skip that
and it falls back to a clean built-in handwriting font.

Deliver the result as a **PDF**, a **DOCX**, or as plain **TXT** / **Markdown**
text content.

---

## Why Scrybe

Most "text to handwriting" tools throw your document on a blank ruled page and
call it done — headings, tables, columns, and spacing all gone. Scrybe is built
the other way around: the original document *is* the canvas. It renders your
source to real page coordinates, measures where every line of text sits, and
draws handwriting into that same space. The page you get back looks like the one
you started with — just handwritten.

What it preserves:

- **Pagination** — one source page in, one page out. No drift.
- **Layout** — text stays where it was; nothing reflows unless you ask.
- **Vector graphics** — table borders, rules, and boxes come through intact.
- **Color** — keeps each span's original ink, or force a single pen color.
- **Styling** — bold and italic are honored (real faces if provided, otherwise
  synthesized).

## How it works

1. **Normalize** — DOCX inputs are rendered to PDF with LibreOffice so Scrybe
   gets exact page geometry; PDF and TXT go straight in.
2. **Parse** — text spans (with position, size, style, and color) and vector
   drawings are extracted from every page.
3. **Fit** — each line is measured in the handwriting font and fitted into the
   space the original text occupied (in `fit` mode, line count never changes;
   `reflow` re-wraps within a block).
4. **Render** — handwriting is drawn onto a copy of the source page. If you
   supplied a handwriting sample, your own glyphs are composited in; anything
   missing (punctuation, accents) falls back to the font.
5. **Deliver** — the handwriting PDF is produced first, then converted to the
   format you asked for.

A note on formats, stated plainly: handwriting is *visual*, so **PDF and DOCX
carry the actual handwriting**. **TXT and Markdown are text-only formats**, so
they deliver the document's text content instead of the handwriting.

---

## Install

```bash
npm install -g handscrybe
```

That's it. The first time you run `handscrybe`, it sets itself up automatically:

- **Python** — Scrybe's engine is Python. It reuses a Python 3.12+ you already
  have, or quietly downloads a private, self-contained copy into `~/.scrybe`
  (no admin rights, nothing added to your PATH). This one-time step takes about
  a minute; every run after that is instant.
- **LibreOffice** — *optional*, and only needed to read **DOCX input**. Scrybe
  detects it and, if it's missing, tells you exactly how to add it. PDF and TXT
  input — and every output format — work without it.

  | Platform | Install LibreOffice |
  | --- | --- |
  | Windows | `winget install TheDocumentFoundation.LibreOffice` |
  | macOS | `brew install --cask libreoffice` |
  | Linux | `sudo apt install libreoffice` |

> **From source (for development):** clone the repo, then
> `pip install -e .` in a Python 3.12 virtual environment. See
> [Development](#development).

---

## Usage

### Just run `handscrybe`

The friendliest way — an interactive menu that walks you through everything:

```bash
handscrybe
```

You'll get a short menu:

```
What would you like to do?
  1) Convert a document   (guided, step by step)
  2) Open the web app     (drag and drop in your browser)
  3) Quit
```

Pick **1** and Scrybe asks a few plain questions (which file, your handwriting or
the built-in font, ink color, layout, output format, where to save) and does the
rest. Pick **2** and it launches the web app and opens it in your browser.

### Command line (scripting)

For automation, the underlying command takes flags directly:

```bash
# Simplest: PDF in, handwriting PDF out
doc-to-hand input.pdf output.pdf

# DOCX in, handwriting PDF out
doc-to-hand report.docx report_hand.pdf

# Use your own handwriting from a sample sheet
doc-to-hand input.pdf output.pdf --handwriting-image my_hand.png

# Choose the delivered format (or let the extension decide)
doc-to-hand input.pdf notes.docx --to docx
doc-to-hand input.pdf notes.txt          # inferred from ".txt"

# Force a single pen color and reflow the text
doc-to-hand input.pdf output.pdf --ink "#1a1a6e" --mode reflow
```

Key options:

| Option | What it does |
| --- | --- |
| `--to {pdf,docx,txt,md}` | Output format. Defaults to the output file's extension, else PDF. |
| `--handwriting-image PATH` | Photo/scan of your sample sheet (A–Z, a–z, 0–9, one row each). |
| `--ink {original,#hex}` | Keep original colors, or force one pen color. |
| `--mode {fit,reflow}` | `fit` keeps pages identical; `reflow` re-wraps lines. |
| `--font PATH` | Use a different regular handwriting `.ttf`. |
| `--font-bold PATH` / `--no-bold-font` | Provide a bold face, or always synthesize bold. |
| `--italic-font PATH` | Provide an italic face (otherwise italic is sheared). |
| `--size-scale N` | Multiply every font size (e.g. `1.1` for slightly larger writing). |
| `--soffice PATH` | Point at the LibreOffice binary if it isn't auto-detected. |

### Web UI

```bash
doc-to-hand-web
# prints the URL and opens your browser; picks a free port if 5000 is busy
```

Upload a document, optionally add a handwriting sample, pick your ink, layout,
and delivery format, and download the result. Host and port are overridable:

```bash
DOC_TO_HAND_HOST=0.0.0.0 DOC_TO_HAND_PORT=8080 doc-to-hand-web
```

### As a library

```python
from doc_to_hand.config import Config, OutputFormat
from doc_to_hand.pipeline import convert

cfg = Config(
    handwriting_image="my_hand.png",   # optional — omit for the built-in font
    ink_color="#1a1a6e",               # or "original"
    output_format=OutputFormat.PDF,    # or DOCX / TXT / MD; None infers from path
)
convert("input.pdf", "output.pdf", cfg)
```

---

## The handwriting sample sheet

To write in your own hand, give Scrybe a photo or scan of a sheet with three
rows of characters:

```
A B C D E F G H I J K L M N O P Q R S T U V W X Y Z
a b c d e f g h i j k l m n o p q r s t u v w x y z
0 1 2 3 4 5 6 7 8 9
```

Scrybe segments the sheet into individual glyphs and composites them into the
document. Characters you didn't write (punctuation, accented letters) fall back
to the built-in font automatically, so partial sheets still work. In the web UI
you'll see how many of the 62 expected glyphs were found.

---

## Supported formats

| | PDF | DOCX | TXT |
| --- | :---: | :---: | :---: |
| **Input** | ✓ | ✓ (needs LibreOffice) | ✓ |
| **Output** | ✓ | ✓ | ✓ (+ Markdown) |

---

## Development

Clone the repo, then work against the Python package directly:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -e .
python -m pytest -q          # Python engine + CLI/web/wizard tests
node npm/test/smoke.js        # npm launcher smoke checks (no provisioning needed)
```

Project layout:

- `src/doc_to_hand/` — the Python engine (parsing, layout, rendering, glyphs,
  CLI, wizard, web app) and the bundled handwriting font under `fonts/`.
- `npm/` — the dependency-free Node launcher that provisions Python and hands
  off to the engine. `bin/scrybe.js` is the `handscrybe` command.

Tests that require LibreOffice (DOCX input) skip automatically when it isn't
installed, so the suite is green on any machine.

---

## License

Released under the [MIT License](LICENSE).
