# Handwriting fonts

The pipeline renders text with a handwriting TTF. Bold/italic are synthesized
from the regular face at render time (stroke widening + shear), so a single
font is enough to start.

## Bundled font

- `Caveat-Regular.ttf` — Google Fonts, licensed under the SIL Open Font
  License 1.1 (see `OFL.txt`). Variable-weight file; PyMuPDF renders it at the
  default weight.

## Using your own font

Drop any `.ttf` into this directory and point the CLI at it:

```
convert input.pdf output.pdf --font fonts/MyHand.ttf
```

If `Caveat-Regular.ttf` is missing (e.g. network was blocked at setup), the
pipeline errors with instructions to place a TTF here.
