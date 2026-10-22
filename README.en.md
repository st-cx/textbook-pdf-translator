# Textbook PDF Translator

A Chinese reading-edition compositor for large English textbooks. Preserve source mathematics as immutable vector/raster objects, translate prose through versioned tasks, reflow at stable role-based font sizes, and label output pages with their original source pages.

**Status: 2.0.0-rc.3. Full-book Rosen translation and final acceptance remain incomplete.** This is an engineering integration, not a new OCR/model algorithm or a claim of worldwide novelty. No controlled competitor benchmark has been completed.

The **2026-10-22 release snapshot**, frozen at 11:33:42 UTC with registered source **v41**, has **10,143 / 32,219 current-contract valid translations and 903 source blockers**. Its selected, current-hash-bound semantic-review scope is 24,905 entries: **112 passed, 6,640 pending, 3 failed and 18,150 awaiting translation**. Valid translations are not all independently reviewed. Later private batches remain in progress and are excluded from this release snapshot.

The current extension-font source audit targets 3,177 objects: 3,171 flat crops were checked without a detected target-ink enclosure risk; **6 assembled objects remain outside that audit scope**. Six earlier operator/limit-splitting findings were repaired with complete per-object source evidence. The current eligible-object guard reports zero findings; this does not certify every formula, ordinary math fonts, semantics or final PDF pixels.

**New unresolved finding on the same v41 source:** a later read-only audit of ordinary mathematical fonts found crop boxes cutting italic overhangs or terminal strokes. Two examples were directly confirmed against the originals at 12x magnification; they remain unfixed. This is separate from the extension-font audit and does not change the frozen task counts above.

The audit covered 1,080 eligible-formula pages, 52,662 active formula assets and 275,899 glyph occurrences; 272,251 occurrences were rasterized, with 3,648 not verifiable at screening. The 13,736 crop-screen findings split into 11,790 core-ink and 1,946 antialias-fringe-only cases. Local actual-asset checks left **9,324 glyph occurrences across 9,242 assets as visible-core-loss candidates**; 581 core cases did not confirm local loss, and 1,885 lacked verifiable target identity or matrix evidence. **The 9,242 assets have not all been manually confirmed damaged; these figures are not a whole-book missing-glyph rate.** Character presence, ink completeness and mathematical correctness remain distinct. Later v42/v43 changes cannot inherit this v41 audit as acceptance. See the [aggregate audit](evidence/ordinary-math-ink-risk-20261022.json), [risk scope](docs/ROSEN_PROGRESS.md) and [validation record](docs/VALIDATION.md).

Batch 01 is explicitly **v39 execution history**: 16 tasks, 14 completed and 2 still requiring completion; 60 inline reviews passed, while standalone review recorded 30 passes and 1 failure. It made 58 book-model requests, including 12 failed requests. These historical results are not current-v41 acceptance counts. Provider pricing is unknown; a zero estimate is not evidence of free service.

This candidate improves native tall-delimiter boundaries, equation continuation across adjacent source pages, translated figure labels, and checks against PDF clipping regions. Authentication and quota failures stop the translation batch. See [Rosen progress and remaining blockers](docs/ROSEN_PROGRESS.md) for measured scopes and version boundaries.

- Digital, image-only, hidden-OCR and mixed PDFs; no outline or searchable text required.
- Pinned BabelDOC parsing, local RapidOCR/DocLayout/MFD inference, custom Typst composition.
- Shared SQLite state for API and agent workers: leases, strict placeholders, content/rule identities, resume and selective redo.
- Separate source coverage, structural checks, semantic sampling, object-pixel checks and visual review.
- Pure layout changes reuse accepted translations while current source and rule identities still match; source-page labels survive pagination expansion.

Verified locally on Windows x64 / Python3.13 / Typst0.15.1. Start with [installation](docs/INSTALL.md) and [workflow](docs/WORKFLOW.md) (Chinese). Copy `config.example.json` to a local `config.json` for your provider, or use agent workers. Never commit credentials or books.

```powershell
powershell -ExecutionPolicy Bypass -File .\安装依赖.ps1
.\.venv\Scripts\python.exe translate_pdf.py "book.pdf" --stage prepare --pages 1-3 --out "sample" --no-legacy-import
```

Inspect source extraction before paid translation. See [architecture](ARCHITECTURE.md), [comparison](docs/INNOVATION.md), [validation](docs/VALIDATION.md), [full-book progress](docs/ROSEN_PROGRESS.md) and [limitations](docs/LIMITATIONS.md). Earlier internal sample reviews do not certify a later parser or renderer revision without a matching rerender and review. The earlier 796/796 and 421/421 object-pixel passes compare composed output with the source crops used at that time, not those crops with the original book. Those passes remain valid within that scope; the newly discovered source-crop risks have not yet been repaired in those historical outputs.

A synthetic red/green regression now checks source-color propagation into translated figure labels. A separate Pascal figure-only probe passed **1/1 background-object pixel checks**, with only the measured translated-label region masked. It retained 18 existing source errors and two caption-design differences; this is not full-page or whole-book acceptance. See the [validation scope](docs/VALIDATION.md).

The rc.3 authored-demo record includes direct visual review of the source and both one-page digital/image-only outputs, using 9 authored translations and 4/4 protected-object pixel checks per variant, with no QA errors or warnings. It certifies only its bound files and renderer; later renderer changes do not inherit that acceptance. Author-known structure enters Book/Store directly, so this is not an end-to-end OCR, automatic-parsing or translation-model benchmark. See the [bound demo record](examples/original-demo/demo-summary.json).

No copyrighted textbook pages or translations, model weights, or private caches are bundled. Runtime and third-party font files are not distributed; tests include two self-authored tiny test fonts.

Project-authored code: AGPL-3.0-or-later. Dependencies/models retain their licenses; see [THIRD_PARTY.md](THIRD_PARTY.md), including conflicting upstream MIT/AGPL notices for the MFD artifact.
