# Textbook PDF Translator

The Chinese desktop launcher `开始翻译.bat` now supports automatic bounded batches,
resume, safe stop, optional review samples and a reading draft. Failed pages are
explicitly retained in the original language, with a source-page report. This is
separate from formal full-book acceptance. [Guide (Chinese)](docs/AUTOMATIC.md)

Keep original mathematical objects and reflow translated Chinese at stable role-based font sizes. Extra pages retain source-page labels.

**2.0.0-rc.5 is a candidate release. No large textbook has completed full-book translation and final visual acceptance.** The 2026-10-24 internal Rosen snapshot (source-v69) covers 1,118 original pages: 28,582 elements require translation and 13,196 have current source/rule-bound accepted translations. Accepted does not mean independently reviewed. See [current progress](docs/ROSEN_PROGRESS.md) and the [aggregate](evidence/rosen-progress-20261022.json).

This release adds native ink envelopes measured from the original embedded font program, glyph ID and full 2×2 text matrix, followed by actual saved-object identity checks. It preserves italic overhangs and terminal strokes without changing the structural box or shrinking text. Unknown identity, unsupported rotation and missing embedded fonts remain explicit source issues. Source, ink, mathematical relationships, semantics and composed-output checks remain separate.

Translation requests contain at most six complete element IDs and preserve indivisible source groups, canonical task identity and read-only context. Truncation splits eligible packets; a truncated singleton remains pending. Successful partial results survive interruption, with a per-task limit of 128 requests including inline review. Optional provider thinking/effort fields are omitted by default; no global reasoning mode is disabled.

- Explicit source-bound definition backgrounds follow translated paragraphs; final QA checks exact native CMYK and complete font resource bindings. Exercise short lines keep natural spacing. [Scope](docs/DESIGN_PANELS.md)
- Complete premise/divider/conclusion units have joint source-bound review. [Protocol](docs/ARGUMENTS.md)
- Digital, image-only, hidden-OCR and mixed PDFs; no outline or searchable text required.
- Pinned BabelDOC parsing, local RapidOCR/DocLayout/MFD inference and custom Typst composition.
- Shared SQLite state for API and agent workers: leases, strict placeholders, source/rule identities, resume and selective redo.
- Fixed role fonts, natural pagination, associated figures/captions and per-page source labels.
- Literal Chinese Landau names stay together during line wrapping; five actual PDF regressions verify their characters, baseline and 10.5pt role size.
- Development main adds opt-in original-size display equations, linked short notes and first-prose exercise baselines. [Validation scope](evidence/display-math-layout-20261023.json)

Verified locally on Windows x64 / Python3.13 / Typst0.15.1. Start with [installation](docs/INSTALL.md) and [workflow](docs/WORKFLOW.md). Copy `config.example.json` to a private local `config.json`.

```powershell
powershell -ExecutionPolicy Bypass -File .\安装依赖.ps1
.\.venv\Scripts\python.exe translate_pdf.py "book.pdf" --stage prepare --pages 1-3 --out "sample" --no-legacy-import
```

Current local source regressions: **1,046 tests, 1,029 passed, 17 optional skips, zero failures/errors**, with matching pre/post source and fixture hashes. This is local validation, not hosted GitHub CI or full-book acceptance. [Bound receipt](evidence/source-regressions-20261024-desktop-auto-v2.json)

The earlier 24-page/7-page digital and 3-page unindexed scan samples are historical rc.2 results. Their 796/796 and 421/421 pixel passes compare composed output with the source crops then used, not those crops with the original book. Newly discovered source-crop risks have not been repaired in those historical outputs. The [original authored demo](examples/original-demo) proves known Book/Store structure to composition, not OCR, automatic extraction or translation-model quality.

No textbook pages or translations, credentials, private jobs, model weights or runtime Chinese fonts are bundled. Four tiny self-authored geometric test fonts are included. Project-authored code is AGPL-3.0-or-later; dependencies/models retain their licenses, including the upstream MFD license discrepancy recorded in [THIRD_PARTY.md](THIRD_PARTY.md).

[Architecture](ARCHITECTURE.md) · [comparison and provenance](docs/INNOVATION.md) · [validation](docs/VALIDATION.md) · [limits](docs/LIMITATIONS.md) · [中文](README.md)
