<div align="center">

# ✍️ Handscrybe

### Turn any typed document into handwriting — without losing the layout.

*Your PDF, DOCX, or TXT comes back handwritten, on the same pages, in the same places — tables, borders, colors and all.*

[![npm version](https://img.shields.io/npm/v/handscrybe.svg?color=cyan&label=npm)](https://www.npmjs.com/package/handscrybe)
[![license](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![node](https://img.shields.io/badge/node-%E2%89%A516-brightgreen.svg)](https://nodejs.org)
[![python](https://img.shields.io/badge/python-%E2%89%A53.12-blue.svg)](https://www.python.org)

```bash
npm install -g handscrybe
handscrybe
```

</div>

---

```
        Typed input                        Handwritten output
   ┌────────────────────┐             ┌────────────────────┐
   │  Quarterly Report  │             │  Quarterly Report  │  ← same text, handwritten
   │  ┌──────┬───────┐   │   ──────▶   │  ┌──────┬───────┐   │
   │  │ Item │ Total │   │             │  │ Item │ Total │   │  ← tables & borders kept
   │  └──────┴───────┘   │             │  └──────┴───────┘   │
   └────────────────────┘             └────────────────────┘
          page 1                             page 1
       (typed font)                    (same spot, same size)
```

---

## ✨ Why Handscrybe

Most "text to handwriting" tools dump your words onto a blank ruled page — headings, tables, columns and spacing all gone. **Handscrybe works the other way around: your original document *is* the canvas.** It reads your source to real page coordinates, measures where every line sits, and draws handwriting into that exact space.

The page you get back looks like the one you started with. Just handwritten.

|  | What you get |
| :---: | :--- |
| 📄 | **Pixel-faithful layout** — text stays exactly where it was; nothing reflows unless you ask |
| 📐 | **Same pagination** — one page in, one page out, no drift |
| 🧩 | **Graphics survive** — table borders, rules, boxes and shapes come through untouched |
| 🎨 | **Real colors** — keeps each line's original ink, or force one pen color |
| ✒️ | **Your handwriting** — snap a photo of a sample sheet and it writes in *your* letters |
| 🔤 | **Bold & italic** — honored automatically |

---

## 🚀 Quick start

```bash
npm install -g handscrybe   # one command, no Python setup needed
handscrybe                  # launches a friendly interactive menu
```

The **first** run sets everything up for you:

- **🐍 Python** — Handscrybe's engine. It reuses a Python 3.12+ you already have, or quietly downloads a private, self-contained copy into `~/.handscrybe` (no admin rights, nothing touches your PATH). ~1 minute, once. Every run after is instant.
- **📝 LibreOffice** — *optional*, only needed to read **DOCX input**. Handscrybe detects it and tells you exactly how to add it if it's missing. PDF/TXT input and every output format work without it.

  | Platform | Install LibreOffice |
  | --- | --- |
  | Windows | `winget install TheDocumentFoundation.LibreOffice` |
  | macOS | `brew install --cask libreoffice` |
  | Linux | `sudo apt install libreoffice` |

> 💡 **Developing from source?** Clone the repo and `pip install -e .` in a Python 3.12 venv — see [Development](#-development).

---

## 🎯 Usage

### The easy way — just run `handscrybe`

An interactive menu walks you through everything:

```
   ┌─────────────────────────────────────────┐
   │   What would you like to do?             │
   │                                          │
   │     1) Convert a document                │
   │     2) Open the web app                  │
   │     3) Quit                              │
   └─────────────────────────────────────────┘
```

Pick **1** and it asks a few plain questions — which file, your handwriting or the built-in font, ink color, layout, output format, where to save — then does the rest. Pick **2** and it opens the drag-and-drop web app in your browser.

### The command line — for scripts & automation

```bash
# Simplest: PDF in, handwriting PDF out
handscrybe input.pdf output.pdf

# DOCX in → handwriting PDF out
handscrybe report.docx report_hand.pdf

# Write it in YOUR handwriting
handscrybe input.pdf output.pdf --handwriting-image my_hand.png

# Choose the delivered format (or just let the extension decide)
handscrybe input.pdf notes.docx --to docx
handscrybe input.pdf notes.txt            # inferred from ".txt"

# Force a single pen color and let text reflow
handscrybe input.pdf output.pdf --ink "#1a1a6e" --mode reflow
```

<details>
<summary><b>All command-line options</b></summary>

<br>

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

</details>

### The web app

```bash
handscrybe-web    # prints the URL, opens your browser, picks a free port if 5000 is busy
```

Drag in a document, optionally add a handwriting sample, pick ink / layout / format, and download. Host and port are overridable:

```bash
HANDSCRYBE_HOST=0.0.0.0 HANDSCRYBE_PORT=8080 handscrybe-web
```

### As a Python library

```python
from handscrybe.config import Config, OutputFormat
from handscrybe.pipeline import convert

cfg = Config(
    handwriting_image="my_hand.png",   # optional — omit for the built-in font
    ink_color="#1a1a6e",               # or "original"
    output_format=OutputFormat.PDF,    # or DOCX / TXT / MD; None infers from path
)
convert("input.pdf", "output.pdf", cfg)
```

---

## ✒️ Use your own handwriting

Give Handscrybe a photo or scan of a sheet with three rows of characters:

```
A B C D E F G H I J K L M N O P Q R S T U V W X Y Z
a b c d e f g h i j k l m n o p q r s t u v w x y z
0 1 2 3 4 5 6 7 8 9
```

It segments the sheet into individual glyphs and composites them into your document. Any character you didn't write (punctuation, accented letters) falls back to the built-in font automatically — so even a **partial** sheet works. The web app shows you how many of the 62 glyphs it found.

---

## 📦 What goes in, what comes out

| | PDF | DOCX | TXT |
| --- | :---: | :---: | :---: |
| **Input** | ✅ | ✅ *(needs LibreOffice)* | ✅ |
| **Output** | ✅ | ✅ | ✅ *(+ Markdown)* |

> **Heads-up on text formats:** handwriting is *visual*, so **PDF and DOCX carry the actual handwriting**. **TXT and Markdown are text-only** — they deliver the document's text *content*, not the handwriting.

---

## 🔧 How it works

```
  ┌──────────┐   ┌────────┐   ┌───────┐   ┌────────┐   ┌─────────┐
  │Normalize │──▶│ Parse  │──▶│  Fit  │──▶│ Render │──▶│ Deliver │
  └──────────┘   └────────┘   └───────┘   └────────┘   └─────────┘
   DOCX→PDF via   text spans   size each   draw hand-   PDF, DOCX,
   LibreOffice;   + graphics   line into   writing on   TXT or MD
   PDF/TXT as-is  per page     its slot    the page
```

1. **Normalize** — DOCX is rendered to PDF (LibreOffice) so Handscrybe has exact geometry; PDF and TXT go straight in.
2. **Parse** — every text span (position, size, style, color) and vector drawing is extracted per page.
3. **Fit** — each line is measured in the handwriting font and fitted into the space the original occupied (`fit` never changes line count; `reflow` re-wraps within a block).
4. **Render** — handwriting is drawn onto a copy of the source page; your own glyphs are composited in where available, else the built-in font.
5. **Deliver** — the handwriting PDF is produced first, then converted to the format you asked for.

---

## 🛠️ Development

```bash
git clone https://github.com/Sami001-OG/scrybe.git
cd scrybe
python -m venv .venv
# Windows: .venv\Scripts\activate   |   macOS/Linux: source .venv/bin/activate
pip install -e .

python -m pytest -q       # Python engine + CLI / web / wizard tests
node npm/test/smoke.js     # npm launcher smoke checks (no provisioning needed)
```

**Project layout**

- `src/handscrybe/` — the Python engine: parsing, layout, rendering, glyph extraction, CLI, wizard, web app (bundled handwriting font under `fonts/`).
- `npm/` — the dependency-free Node launcher that provisions Python and hands off to the engine. `bin/handscrybe.js` is the `handscrybe` command.

Tests that need LibreOffice (DOCX input) skip automatically when it isn't installed, so the suite stays green on any machine.

---

## 📄 License

[MIT](LICENSE) — free to use, modify, and distribute.

<div align="center">
<sub>Built for anyone who needs the look of handwriting with the precision of a document.</sub>
</div>
