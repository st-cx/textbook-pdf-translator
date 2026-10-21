# Textbook PDF Translator

A Chinese reading-edition compositor for large English textbooks. Preserve source mathematics as immutable vector/raster objects, translate prose through versioned tasks, reflow at stable role-based font sizes, and label output pages with their original source pages.

**Status: 2.0.0-rc.2. Selected digital and image-only textbook samples have been reviewed; no full-book acceptance or controlled competitor benchmark has been completed.** This is an engineering integration, not a new OCR/model algorithm or a claim of worldwide novelty.

- Digital, image-only, hidden-OCR and mixed PDFs; no outline or searchable text required.
- Pinned BabelDOC parsing, local RapidOCR/DocLayout/MFD inference, custom Typst composition.
- Shared SQLite state for API and agent workers: leases, strict placeholders, content/rule identities, resume and selective redo.
- Separate source coverage, structural checks, semantic sampling, object-pixel checks and visual review.
- Layout changes reuse accepted translations; source-page labels survive pagination expansion.

Verified locally on Windows x64 / Python3.13 / Typst0.15.1. Start with [installation](docs/INSTALL.md) and [workflow](docs/WORKFLOW.md) (Chinese). Copy `config.example.json` to a local `config.json` for your provider, or use agent workers. Never commit credentials or books.

```powershell
powershell -ExecutionPolicy Bypass -File .\安装依赖.ps1
.\.venv\Scripts\python.exe translate_pdf.py "book.pdf" --stage prepare --pages 1-3 --out "sample" --no-legacy-import
```

Inspect source extraction before paid translation. See [architecture](ARCHITECTURE.md), [comparison](docs/INNOVATION.md), [validation](docs/VALIDATION.md) and [limitations](docs/LIMITATIONS.md).

Original synthetic demonstrations enter the Book/Store contract with author-known structure; they do not benchmark end-to-end OCR/translation. No textbook pages, translations, model weights, caches or fonts are bundled.

Project-authored code: AGPL-3.0-or-later. Dependencies/models retain their licenses; see [THIRD_PARTY.md](THIRD_PARTY.md), including conflicting upstream MIT/AGPL notices for the MFD artifact.
