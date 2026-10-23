# Textbook PDF Translator

Keep original mathematical objects and reflow translated Chinese at stable role-based font sizes. Extra pages retain source-page labels.

**2.0.0-rc.4 is a candidate release. No large textbook has completed full-book translation and final visual acceptance.** The 2026-10-23 internal Rosen snapshot (source-v58) covers 1,118 original pages: 28,658 elements require translation, 10,814 have current accepted translations, and 3,523 exact-whitelisted mathematical/numbering units are preserved separately. Accepted does not mean independently reviewed. See [current progress](docs/ROSEN_PROGRESS.md) and its [sanitized aggregate](evidence/rosen-progress-20261022.json).

This release adds native ink envelopes measured from the original embedded font program, glyph ID and full 2×2 text matrix, followed by actual saved-object identity checks. It preserves italic overhangs and terminal strokes without changing the structural box or shrinking text. Unknown identity, unsupported rotation and missing embedded fonts remain explicit source issues. The registered source has 9,323 cumulative local-object restorations; these are not 9,323 certified complete formulas. Source, ink, mathematical relationships, semantics and composed-output checks remain separate.

Translation requests contain at most six complete element IDs and preserve indivisible source groups, canonical task identity and read-only context. Truncation splits eligible packets; a truncated singleton remains pending. Successful partial results survive interruption, with a per-task limit of 128 requests including inline review. Optional provider thinking/effort fields are omitted by default; no global reasoning mode is disabled.

- Digital, image-only, hidden-OCR and mixed PDFs; no outline or searchable text required.
- Pinned BabelDOC parsing, local RapidOCR/DocLayout/MFD inference and custom Typst composition.
- Shared SQLite state for API and agent workers: leases, strict placeholders, source/rule identities, resume and selective redo.
- Fixed role fonts, natural pagination, associated figures/captions and per-page source labels.
- Development main adds opt-in original-size display equations, linked short notes and first-prose exercise baselines. [Validation scope](evidence/display-math-layout-20261023.json)

Verified locally on Windows x64 / Python3.13 / Typst0.15.1. Start with [installation](docs/INSTALL.md) and [workflow](docs/WORKFLOW.md). Copy `config.example.json` to a private local `config.json`.

```powershell
powershell -ExecutionPolicy Bypass -File .\安装依赖.ps1
.\.venv\Scripts\python.exe translate_pdf.py "book.pdf" --stage prepare --pages 1-3 --out "sample" --no-legacy-import
```

Current local source regressions: **718 tests, 701 passed, 17 optional skips, zero failures/errors**, with matching pre/post source and fixture hashes. This is local validation, not hosted GitHub CI or full-book acceptance. [Bound receipt](evidence/source-regressions-20261023-explicit-review.json)

The earlier 24-page/7-page digital and 3-page unindexed scan samples are historical rc.2 results. Their 796/796 and 421/421 pixel passes compare composed output with the source crops then used, not those crops with the original book. Newly discovered source-crop risks have not been repaired in those historical outputs. The [original authored demo](examples/original-demo) proves known Book/Store structure to composition, not OCR, automatic extraction or translation-model quality.

No textbook pages or translations, credentials, private jobs, model weights or runtime Chinese fonts are bundled. Four tiny self-authored geometric test fonts are included. Project-authored code is AGPL-3.0-or-later; dependencies/models retain their licenses, including the upstream MFD license discrepancy recorded in [THIRD_PARTY.md](THIRD_PARTY.md).

[Architecture](ARCHITECTURE.md) · [comparison and provenance](docs/INNOVATION.md) · [validation](docs/VALIDATION.md) · [limits](docs/LIMITATIONS.md) · [中文](README.md)
