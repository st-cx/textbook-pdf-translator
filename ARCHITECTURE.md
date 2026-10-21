# Shared contract

`textbook_pdf.models` owns the neutral JSON schema. A `Book` holds `Element` records and immutable `Asset` records. Physical source pages are one based. Coordinates are PDF points with top-left origin. Asset paths are relative to the book job directory. Math tokens are exactly `⟦F:asset_id⟧`. No renderer or translator may change asset contents. Translation dictionaries map stable element IDs to strings. Shared SQLite state is owned by `store.py`; agents must not create another authoritative translation store.

Parser: `extraction.analyze(pdf_path, outdir, pages=None, backend="auto") -> Book`; stores assets under outdir/assets, returns Book and records issues. BabelDOC bridge is optional but must genuinely export formulas/styles when available; no pretending that fallback used BabelDOC.

Composer: `layout.compose(book, translations, outdir, output_path=None, allow_incomplete=False) -> dict`; emits PDF plus `layout_map.json`, which maps each source element/asset ID to rendered page and rectangles. Fixed role fonts, source page labels, no text scaling. Missing translations fail unless explicit preview mode, which visibly labels source text.

Translation: `translation.make_tasks(book, glossary, target_chars=2400) -> list[dict]`; task has id, element_ids, source_hash, payload. `translation.run_translation(book, store, config_path, provider=None, workers=4, review_provider=None, max_tasks=None) -> dict`; tasks use Store methods below. Migration: `migration.import_legacy(book, store, legacy_dir) -> dict`; only exact source matching accepted, reinsert tokens by verified matching protected text, ambiguous mappings remain review issues.

Store interface: `Store(path)`; `register(book, glossary)`; `add_tasks(tasks)`; `claim(owner, lease_seconds=600, task_id=None) -> task or None`; `renew(task_id,owner,lease_seconds=600)`; `submit(task_id,owner,source_hash,translations,provider="agent",usage=None) -> dict`; `fail(task_id,owner,error,retryable=True)`; `translations(accepted_only=True) -> dict`; `status() -> dict`; `pending_tasks() -> list`; `put_candidate(element_id,text,provenance,issues=None)`; `record_issue(issue)`; context manager. Submit atomically validates exact IDs, source hashes, formulas and numbers. Failed/partial tasks never count as done. Glossary dict maps English phrases to Chinese.


Standalone semantic review: `translation.run_review(book, store, config_path, provider, max_elements=None, workers=4)` partitions disjoint bounded batches under one provider limiter/stop event. Current verdicts bind both element ID and review hash. It never registers subset books or changes source/task identity.


## Scan extension contract

A missing PDF outline/TOC is never a prerequisite failure. Source identity remains the original PDF SHA256 and one-based physical pages. Printed-page labels come from page-specific evidence; unknown labels use physical-page text.

`extraction.analyze` keeps its native route for digital pages and dispatches scan/hidden-OCR pages to `scan_adapter.analyze`. New arguments: `ocr="auto"|"off"|"force"`, `ocr_dpi=300`, optional corrections sidecar. Mixed books preserve physical page IDs and the original Book source hash. Existing accepted jobs are never silently re-analyzed by translate/layout.


OCR engine: `recognize(image_rgb, *, model_dir=None) -> {lines:[{text,confidence,bbox,words:[{text,confidence,bbox}]}], engine:{name,version,models}, elapsed_seconds}`. Boxes are finite original-input-image pixel coordinates, top-left origin. CTC word/character boxes are estimates and never native glyph evidence. Models and configuration have content hashes; inference is local. No URL image inputs, document upload or implicit cloud provider.

Detectors: `detect_regions(image_rgb, *, layout_model=None, formula_model=None) -> {regions:[{kind,bbox,confidence,detector}], models:{...},elapsed_seconds}`. `kind` is heading/text/figure/table/code/formula_inline/formula_display/page_number/caption/other. All boxes use the same original pixel frame. Use DocLayout for document regions plus Pix2Text MFD for inline/display formula detection. Preserve overlaps/conflicts as review evidence rather than silently deleting conflicting labels.

Scan adapter converts pixel boxes to original visible PDF points using actual rendered width/height. Assets retain original composed page appearance (including MRC foreground/background and alpha masks), not one image xref or OCR-transcribed math. It masks protected regions for prose OCR, then injects immutable formula tokens geometrically. `Asset`/`Element` schemas stay unchanged; scan provenance lives in metadata and `page.scan_evidence`. OCR text units have a separate ledger; `total_glyphs=0` must never imply optical recognition completeness.

Scanned-page QA separately reports OCR confidence, original-image region/ink coverage, detector ambiguity and visual review. Unrecognized text, unassigned meaningful image ink or questionable formula boundaries remain explicit issues. A scan is no longer rejected solely for being a scan. Confidence and pixel coverage alone do not certify mathematical meaning.


Scan composition is a separate `scan_layout.compose` wrapper around the frozen native `layout.compose`. It binds its own source SHA and sampled original-paper color policy in Book.profile before composition, paints only behind output pages after layout, updates output file hash, and preserves every font/object rectangle. Ordinary digital jobs keep the original renderer and template byte-for-byte. Source-associated short introductions and following display formulas use the existing `body_connective` grouping protocol. Synthetic unindexed tests retain original physical page numbers with explicit blank placeholders, selecting only filled pages; placeholders are never claimed as a translated full book.
