"""Raster evidence for protected PDF objects at their measured output locations.

This compares visible pixels, including the source PDF page's real clipping.
It never infers formula correctness from text extraction and never modifies the
source document.  A pass concerns image-box-relative ink geometry only.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from collections import Counter

import numpy as np
import pymupdf

from .models import file_sha256, TOKEN_RE

SCALE = 4
AA_TOLERANCE_PIXELS = 1
INK_CONTRAST = 32
AA_SUPPORT_CONTRAST = 24
PROTECTED_ASSET_KINDS = frozenset({"formula", "equation", "math", "code", "figure", "table", "image", "drawing", "diagram"})
_MATH_CODE_KINDS = frozenset({"formula", "equation", "math", "code"})
_MAX_RENDER_PIXELS = 80_000_000
_MAX_EVIDENCE_PAIRS = 40


def _rectangle(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Expected four measured rectangle coordinates")
    coords = [float(v) for v in value]
    if not all(math.isfinite(v) for v in coords):
        raise ValueError("Rectangle coordinates must be finite")
    rect = pymupdf.Rect(coords)
    if rect.is_empty or rect.is_infinite:
        raise ValueError("Rectangle has no finite positive area")
    return rect


def _bitmap(page):
    if page.rect.width * page.rect.height * SCALE * SCALE > _MAX_RENDER_PIXELS:
        raise ValueError("Page exceeds the bounded raster memory budget")
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(SCALE, SCALE), colorspace=pymupdf.csGRAY,
                            alpha=False, annots=False)
    # samples owns immutable bytes; this array does not depend on a live Pixmap.
    pixels = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width)
    return pixels, pixmap.x, pixmap.y


def _mask_figure_labels(source, target, bbox, record, asset):
    """Compare preserved diagram geometry outside measured translated labels.

    This does not certify label text or ink under a label. Child formula objects
    receive independent checks; label language and placement require review.
    """
    if (asset.metadata.get('detector') != 'translated-figure-background'
            or record.get('translated_label_regions_precision') != 'measured-figure-labels'
            or not asset.metadata.get('label_removal_proof', {}).get('native_glyph_ids')
            or set(record.get('translated_label_ids', [])) != set(asset.metadata.get('detached_label_ids', []))):
        raise ValueError('Translated-label comparison lacks bound source and measured child evidence')
    regions = record.get('translated_label_regions', [])
    if not regions:
        raise ValueError('Translated-label comparison has no measured regions')
    result = []
    fractions = []
    for pixels in (source, target):
        copy = pixels.copy()
        mask = np.zeros(copy.shape, dtype=bool)
        for value in regions:
            rect = _rectangle(value)
            if not (bbox + (-.3, -.3, .3, .3)).contains(rect):
                raise ValueError('Translated label leaves its source diagram')
            x0 = max(0, math.floor((rect.x0 - bbox.x0) / bbox.width * copy.shape[1]) - 1)
            x1 = min(copy.shape[1], math.ceil((rect.x1 - bbox.x0) / bbox.width * copy.shape[1]) + 1)
            y0 = max(0, math.floor((rect.y0 - bbox.y0) / bbox.height * copy.shape[0]) - 1)
            y1 = min(copy.shape[0], math.ceil((rect.y1 - bbox.y0) / bbox.height * copy.shape[0]) + 1)
            mask[y0:y1, x0:x1] = True
        fraction = float(mask.mean())
        if fraction > .6:
            raise ValueError('Too little unmasked diagram remains for a background integrity pass')
        copy[mask] = 255
        result.append(copy)
        fractions.append(fraction)
    return (*result, {'comparison_scope': 'diagram background outside measured translated-label regions',
                     'label_semantics_certified': False,
                     'masked_fraction': max(fractions), 'masked_regions': regions})


def _ink(pixels):
    border = np.concatenate((pixels[0], pixels[-1], pixels[:, 0], pixels[:, -1]))
    background = float(np.median(border))
    contrast = np.abs(pixels.astype(np.int16) - background)
    return contrast >= INK_CONTRAST, background


def _dilate(mask):
    padded = np.pad(mask, AA_TOLERANCE_PIXELS)
    result = np.zeros_like(mask)
    for y in range(3):
        for x in range(3):
            result |= padded[y:y + mask.shape[0], x:x + mask.shape[1]]
    return result


def _largest_component(mask):
    if not np.any(mask):
        return 0
    remaining = mask.copy()
    height, width = remaining.shape
    largest = 0
    for y, x in zip(*np.nonzero(mask)):
        if not remaining[y, x]:
            continue
        stack, count = [(int(y), int(x))], 0
        remaining[y, x] = False
        while stack:
            yy, xx = stack.pop()
            count += 1
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = yy + dy, xx + dx
                    if 0 <= ny < height and 0 <= nx < width and remaining[ny, nx]:
                        remaining[ny, nx] = False
                        stack.append((ny, nx))
        largest = max(largest, count)
    return largest


def _longest_run(mask):
    best = 0
    for row in mask:
        padded = np.pad(row.astype(np.int8), 1)
        transitions = np.diff(padded)
        starts, ends = np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)
        if len(starts):
            best = max(best, int(np.max(ends - starts)))
    return best


def _compare(source, target):
    source_mask, source_background = _ink(source)
    target_mask, target_background = _ink(target)
    height, width = max(source.shape[0], target.shape[0]), max(source.shape[1], target.shape[1])
    expected, actual = np.zeros((height, width), bool), np.zeros((height, width), bool)
    expected_support, actual_support = np.zeros((height, width), bool), np.zeros((height, width), bool)
    expected[:source.shape[0], :source.shape[1]] = source_mask
    actual[:target.shape[0], :target.shape[1]] = target_mask
    # Two real Typst renders differed by a single grayscale level on a serif
    # edge (source=223, output=224).  Require weaker visible support for an AA
    # edge while retaining the same strict one-pixel spatial tolerance.
    expected_support[:source.shape[0], :source.shape[1]] = np.abs(source.astype(np.int16) - source_background) >= AA_SUPPORT_CONTRAST
    actual_support[:target.shape[0], :target.shape[1]] = np.abs(target.astype(np.int16) - target_background) >= AA_SUPPORT_CONTRAST
    missing = expected & ~_dilate(actual_support)
    extra = actual & ~_dilate(expected_support)
    source_ink, target_ink = int(expected.sum()), int(actual.sum())
    absent, added = int(missing.sum()), int(extra.sum())
    missing_component, extra_component = _largest_component(missing), _largest_component(extra)
    missing_run = max(_longest_run(missing), _longest_run(missing.T)) if absent else 0
    missing_ratio, extra_ratio = absent / max(1, source_ink), added / max(1, source_ink)
    if source_ink and target_ink:
        source_y, source_x = np.nonzero(expected)
        target_y, target_x = np.nonzero(actual)
        centroid = [float(target_x.mean() - source_x.mean()), float(target_y.mean() - source_y.mean())]
    else:
        centroid = None
    metrics = {"source_ink_pixels": source_ink, "target_ink_pixels": target_ink,
               "missing_pixels_outside_tolerance": absent, "extra_pixels_outside_tolerance": added,
               "missing_fraction": missing_ratio, "extra_fraction_of_source": extra_ratio,
               "largest_missing_component": missing_component, "largest_extra_component": extra_component,
               "longest_missing_stroke_run": missing_run, "centroid_delta_pixels": centroid,
               "source_background_gray": source_background, "target_background_gray": target_background,
               "source_visible_support_pixels": int(expected_support.sum()),
               "target_visible_support_pixels": int(actual_support.sum()),
               "source_pixel_size": [int(source.shape[1]), int(source.shape[0])],
               "target_pixel_size": [int(target.shape[1]), int(target.shape[0])]}
    if source_ink == 0:
        return "unverifiable", "visual_source_has_no_visible_ink", metrics
    if target_ink == 0:
        if np.any(actual_support):
            return "unverifiable", "visual_target_ink_too_faint", metrics
        return "error", "visual_asset_blank_or_missing", metrics
    if source_ink >= 6 and (target_ink / source_ink < .08 or missing_ratio >= .8):
        return "error", "visual_asset_severely_incomplete", metrics
    # Actual Typst embedding showed occasional isolated one- or two-pixel
    # differences after 1px dilation, including tiny 30-pixel source symbols.
    # Absolute/component thresholds avoid flagging those as 3%-6% damage.
    missing_shape = absent >= 3 and (missing_ratio > .01 or missing_component >= 4 or missing_run >= 5)
    extra_shape = added >= 3 and (extra_ratio > .01 or extra_component >= 4)
    if missing_shape or extra_shape:
        return "warning", "visual_asset_ink_mismatch", metrics
    return "passed", None, metrics


def resolve_asset_path(book, asset, outdir, pdf_path=None):
    outdir = Path(outdir)
    pdf_path = Path(pdf_path) if pdf_path else outdir / "output.pdf"
    value = Path(asset.path)
    if value.is_absolute():
        candidates = [value]
    else:
        roots = [outdir, outdir.parent, pdf_path.parent, pdf_path.parent.parent]
        roots += [Path(path) for path in (book.profile.get("job_dir"), book.profile.get("asset_root")) if path]
        roots.append(Path(book.source_path).parent)
        candidates = [root / value for root in roots]
        if asset.sha256:
            candidates += [root / "assets" / (asset.sha256[:24] + ".pdf") for root in roots]
    return next((path.resolve() for path in candidates if path.is_file()), None)


def _evidence_png(pixels, path):
    # A diagnostic rendering, not an edit to an input image or PDF.
    pixmap = pymupdf.Pixmap(pymupdf.csGRAY, int(pixels.shape[1]), int(pixels.shape[0]),
                            pixels.astype(np.uint8, copy=False).tobytes(), False)
    pixmap.save(path)


def archived_asset_dispositions(book):
    """Trace archived originals to active vectors or a verified translated grid."""
    active={aid for element in book.elements for aid in element.asset_ids}
    active.update(aid for element in book.elements for aid in TOKEN_RE.findall(element.text))
    elements=book.element_map()
    def resolve(target,seen):
        if target in seen:return None
        if target in active and target in book.assets:return ({target},set())
        element=elements.get(target)
        if element is not None and element.kind=='table':
            grid=element.metadata.get('table_grid',{})
            evidence=grid.get('evidence',{})
            rows=grid.get('rows',[])
            if (not evidence.get('complete_rectangular_grid') or evidence.get('unassigned_source_chars',1)!=0
                or not rows or any(not isinstance(row,list) or not row for row in rows)):
                return None
            cells=[eid for row in rows for eid in row]
            if (len(cells)!=len(set(cells)) or any(eid not in elements or elements[eid].kind!='table_cell'
                or elements[eid].metadata.get('parent_table_id')!=target for eid in cells)):
                return None
            assets={aid for eid in cells for aid in elements[eid].asset_ids}
            return (assets,{target,*cells}) if assets<=book.assets.keys() else None
        asset=book.assets.get(target)
        if asset is None or not asset.metadata.get('exclusion_reason'):return None
        targets=asset.metadata.get('combined_into') or asset.metadata.get('superseded_by') or []
        if isinstance(targets,str):targets=[targets]
        if not isinstance(targets,list) or not targets:return None
        proofs=[resolve(value,seen|{target}) for value in targets if isinstance(value,str)]
        if len(proofs)!=len(targets) or any(proof is None for proof in proofs):return None
        return (set().union(*(proof[0] for proof in proofs)),set().union(*(proof[1] for proof in proofs)))
    archived={}
    for aid,asset in book.assets.items():
        if aid in active or not asset.metadata.get('exclusion_reason'):continue
        proof=resolve(aid,set())
        if proof is not None:
            archived[aid]={'asset_id':aid,'kind':asset.kind,'page':asset.page,
                           'reason':asset.metadata['exclusion_reason'],
                           'replacement_ids':sorted(proof[0]),'replacement_element_ids':sorted(proof[1]),
                           'replacement_is_translated_grid':bool(proof[1])}
    return archived


def compare_assets(book, layout_map, pdf_path, outdir, limit=None) -> dict:
    """Check immutable protected occurrences using *measured* image boxes.

    checked/passed count distinct rendered occurrences, not source symbols or
    all assets in a book.  Skipped, limited, blank-source and uncertain geometry
    never become passes.  Pixel disagreement is a manual-review warning unless
    the target is blank or severely missing.  Only one output-page raster and
    one open source bundle are cached; arrays are discarded between objects.
    """
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("limit must be a nonnegative integer")
    pdf_path, outdir = Path(pdf_path).resolve(), Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    if isinstance(layout_map, (str, Path)):
        layout_map = json.loads(Path(layout_map).read_text(encoding="utf-8"))
    if not isinstance(layout_map, dict) or not isinstance(layout_map.get("assets"), dict):
        raise ValueError("layout_map must contain an assets mapping")
    report = {"checked": 0, "passed": 0, "issues": [], "metrics": [], "skipped": 0,
              "deduplicated": 0, "limited": 0, "eligible_occurrences": 0,
              "protected_source_assets": 0, "evidence_pairs_saved": 0,
              "total_book_assets": len(book.assets), "eligible_source_assets_by_kind": {},
              "excluded_source_assets_by_kind": {},
              "unit": "distinct measured protected-asset occurrences",
              "parameters": {"scale": SCALE, "dpi": SCALE * 72, "antialias_tolerance_pixels": AA_TOLERANCE_PIXELS,
                             "ink_contrast_threshold": INK_CONTRAST, "antialias_support_contrast": AA_SUPPORT_CONTRAST,
                             "isolated_noise_pixels_allowed": 2},
              "limitations": ["Passes cover image-box-relative visible ink geometry, not mathematical meaning.",
                              "The surrounding-prose baseline cannot be proved from an image box alone.",
                              "Background tone is estimated separately; this check does not certify color fidelity."]}

    def issue(code, detail, asset_id=None, record=None, severity="warning", **extra):
        value = {"code": code, "severity": severity, "detail": detail, **extra}
        if asset_id:
            value.update(asset_id=asset_id, page=book.assets[asset_id].page)
        if record:
            value.update(output_page=record.get("page"), output_bbox=record.get("bbox"),
                         element_id=record.get("element_id"))
        report["issues"].append(value)
        return value

    archived=archived_asset_dispositions(book)
    report['archived_asset_dispositions']=list(archived.values())
    report['archived_source_assets_by_kind']=dict(Counter(book.assets[aid].kind for aid in archived))
    jobs, seen = [], set()
    eligible_kinds, excluded_kinds = Counter(), Counter()
    for aid, asset in book.assets.items():
        if aid in archived:continue
        if asset.kind not in PROTECTED_ASSET_KINDS:
            excluded_kinds[asset.kind] += 1
            continue
        eligible_kinds[asset.kind] += 1
        report["protected_source_assets"] += 1
        records = layout_map["assets"].get(aid)
        if not isinstance(records, list) or not records:
            report["skipped"] += 1
            issue("visual_asset_trace_missing", "No measured output rectangle; visual integrity was not checked", aid)
            continue
        for record in records:
            reason = asset.metadata.get("visual_check_exclusion_reason")
            if isinstance(record, dict):
                reason = record.get("visual_check_exclusion_reason") or reason
                has_overlays = record.get("translated_label_regions") or asset.metadata.get("translated_label_regions")
                region_check = (has_overlays and asset.metadata.get('detector')=='translated-figure-background'
                                and record.get('translated_label_regions_precision')=='measured-figure-labels')
                if has_overlays and not reason and not region_check:
                    reason = "Translated-label overlays change visible pixels; region-aware comparison has not been supplied"
            if reason:
                report["skipped"] += 1
                issue("visual_asset_intentional_change_unverified", str(reason), aid,
                      record if isinstance(record, dict) else None)
                continue
            if not isinstance(record, dict) or record.get("precision") != "measured":
                report["skipped"] += 1
                issue("visual_asset_trace_imprecise", "An approximate layout record cannot establish visual integrity", aid,
                      record if isinstance(record, dict) else None)
                continue
            try:
                bbox = _rectangle(record.get("bbox"))
                page_number = record.get("page")
                if type(page_number) is not int or page_number < 1:
                    raise ValueError("Invalid one-based output page")
            except (TypeError, ValueError, OverflowError):
                report["skipped"] += 1
                issue("visual_asset_trace_invalid", "Measured page/rectangle is invalid; comparison skipped", aid, record)
                continue
            key = (aid, page_number, tuple(round(v, 6) for v in bbox))
            if key in seen:
                report["deduplicated"] += 1
                continue
            seen.add(key)
            jobs.append((page_number, aid, record, bbox))
    report["eligible_source_assets_by_kind"] = dict(eligible_kinds)
    report["excluded_source_assets_by_kind"] = dict(excluded_kinds)
    jobs.sort(key=lambda job: (job[0], job[1], tuple(job[3])))
    report["eligible_occurrences"] = len(jobs)
    if limit is not None:
        report["limited"] = max(0, len(jobs) - limit)
        jobs = jobs[:limit]
    source_doc, source_doc_path, source_bitmap_key, source_pixels = None, None, None, None
    output_page_number, output_pixels, output_offsets = None, None, None
    hashes = {}
    try:
        with pymupdf.open(pdf_path) as output:
            for page_number, aid, record, bbox in jobs:
                asset = book.assets[aid]
                if page_number > len(output):
                    report["skipped"] += 1
                    issue("visual_asset_page_missing", "Measured output page does not exist", aid, record, severity="error")
                    continue
                output_page = output[page_number - 1]
                page_box = output_page.rect
                if bbox.x0 < page_box.x0 - .01 or bbox.y0 < page_box.y0 - .01 or bbox.x1 > page_box.x1 + .01 or bbox.y1 > page_box.y1 + .01:
                    report["skipped"] += 1
                    issue("visual_asset_outside_page", "Measured protected object extends outside the output page", aid, record,
                          severity="error")
                    continue
                source = resolve_asset_path(book, asset, outdir, pdf_path)
                if source is None or source.suffix.lower() != ".pdf":
                    report["skipped"] += 1
                    issue("visual_asset_source_unavailable", "Original protected PDF asset could not be opened; no integrity pass", aid, record)
                    continue
                if source not in hashes:
                    hashes[source] = file_sha256(source)
                if asset.sha256 and hashes[source] != asset.sha256:
                    report["skipped"] += 1
                    issue("visual_asset_source_changed", "Protected source bundle hash changed; comparison rejected", aid, record,
                          severity="error")
                    continue
                try:
                    source_index = asset.metadata.get("pdf_page", 0)
                    if type(source_index) is not int or source_index < 0:
                        raise ValueError("Invalid protected bundle page index")
                    if source_doc_path != source:
                        if source_doc is not None and not source_doc.is_closed:
                            source_doc.close()
                        source_doc, source_doc_path, source_bitmap_key, source_pixels = None, None, None, None
                        source_doc = pymupdf.open(source)
                        source_doc_path, source_bitmap_key, source_pixels = source, None, None
                    if source_index >= len(source_doc):
                        raise ValueError("Protected bundle page does not exist")
                    original_page = source_doc[source_index]
                    delta = [bbox.width - original_page.rect.width, bbox.height - original_page.rect.height]
                    declared_delta = [original_page.rect.width - float(asset.width), original_page.rect.height - float(asset.height)]
                    dimensions_ok = max(abs(v) for v in delta + declared_delta) <= .26
                    if asset.kind not in _MATH_CODE_KINDS and not dimensions_ok:
                        report["skipped"] += 1
                        issue("visual_asset_geometry_changed_unverified", "Figure/table geometry changed; unchanged-geometry pixel comparison does not apply",
                              aid, record, dimension_delta_points=delta, source_dimension_delta_points=declared_delta)
                        continue
                    bitmap_key = (source, source_index)
                    if source_bitmap_key != bitmap_key:
                        source_pixels, _, _ = _bitmap(original_page)
                        source_bitmap_key = bitmap_key
                    if output_page_number != page_number:
                        output_pixels, px, py = _bitmap(output_page)
                        output_offsets, output_page_number = (px, py), page_number
                    px, py = output_offsets
                    left, top = math.floor(bbox.x0 * SCALE) - px, math.floor(bbox.y0 * SCALE) - py
                    right, bottom = math.ceil(bbox.x1 * SCALE) - px, math.ceil(bbox.y1 * SCALE) - py
                    if min(left, top) < 0 or right > output_pixels.shape[1] or bottom > output_pixels.shape[0]:
                        raise ValueError("Raster bounds do not contain measured box")
                    # Detach this small crop so replacing the page cache cannot
                    # keep an old full-page raster alive through a numpy view.
                    target = output_pixels[top:bottom, left:right].copy()
                    if not source_pixels.size or not target.size:
                        raise ValueError("Protected object has an empty raster")
                    comparison_source = source_pixels
                    label_metrics = {}
                    if record.get('translated_label_regions'):
                        comparison_source, target, label_metrics = _mask_figure_labels(source_pixels, target, bbox, record, asset)
                    status, code, metrics = _compare(comparison_source, target)
                    metrics.update(label_metrics)
                    metrics.update({"asset_id": aid, "source_page": asset.page, "source_bundle": str(source),
                                    "source_bundle_page": source_index + 1, "output_page": page_number,
                                    "output_bbox": list(bbox), "element_id": record.get("element_id"),
                                    "dimension_delta_points": delta, "source_dimension_delta_points": declared_delta,
                                    "status": status})
                    if status == "passed" and not dimensions_ok:
                        status, code = "warning", "visual_asset_dimension_mismatch"
                        metrics["status"] = status
                    report["checked"] += 1
                    if status == "passed":
                        report["passed"] += 1
                    else:
                        problem = issue(code, "Protected visible ink requires review; inspect raster metrics and source/output crops",
                                        aid, record, severity="error" if status == "error" else "warning",
                                        missing_fraction=metrics["missing_fraction"],
                                        extra_fraction_of_source=metrics["extra_fraction_of_source"])
                        if report["evidence_pairs_saved"] < _MAX_EVIDENCE_PAIRS:
                            evidence = outdir / "visual-evidence"
                            evidence.mkdir(exist_ok=True)
                            label = re.sub(r"[^A-Za-z0-9_.-]", "_", aid)[:90]
                            suffix = hashlib.sha256(json.dumps([page_number, list(bbox)]).encode()).hexdigest()[:10]
                            source_png, target_png = evidence / f"{label}-{suffix}-source.png", evidence / f"{label}-{suffix}-target.png"
                            _evidence_png(source_pixels, str(source_png))
                            _evidence_png(target, str(target_png))
                            metrics["evidence"] = problem["evidence"] = {"source_png": str(source_png), "target_png": str(target_png)}
                            report["evidence_pairs_saved"] += 1
                    report["metrics"].append(metrics)
                except (ValueError, RuntimeError, OSError, TypeError, OverflowError) as error:
                    report["skipped"] += 1
                    issue("visual_asset_comparison_unavailable", "Asset could not be rendered or compared; no integrity pass", aid, record,
                          error_type=type(error).__name__)
    finally:
        if source_doc is not None and not source_doc.is_closed:
            source_doc.close()
    report["not_passed"] = report["checked"] - report["passed"]
    report["all_eligible_occurrences_checked"] = report["checked"] == report["eligible_occurrences"] and report["skipped"] == 0 and report["limited"] == 0
    report["output_pdf"] = str(pdf_path)
    report_path = outdir / "visual_asset_report.json"
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
