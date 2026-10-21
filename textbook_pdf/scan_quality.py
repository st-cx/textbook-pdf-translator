"""Independent, bounded raster ownership checks for local scanned pages.

Boxes are in the original RGB image's pixel frame, with exclusive right/bottom
edges. Pixel ownership is evidence of region coverage, never OCR correctness.
No PDF text layer, recognition service, or source document is accessed here.
"""
from __future__ import annotations

import math
from numbers import Real

import cv2
import numpy as np

BBOX_TOLERANCE_PIXELS = 2
LOCAL_BACKGROUND_KERNEL = 41
LOCAL_INK_CONTRAST = 14
CHROMA_CONTRAST = 36
MAX_IMAGE_PIXELS = 40_000_000
MAX_REPORTED_COMPONENTS = 20_000
TINY_NOISE_MAX_AREA = 2
TINY_NOISE_MAX_SIDE = 2
TINY_NOISE_ISOLATION_PIXELS = 4


def _box(value, width, height):
    if isinstance(value, (str, bytes)):
        raise ValueError("bbox must have four finite numeric coordinates")
    try:
        if len(value) != 4:
            raise ValueError("bbox must have four coordinates")
        box = [float(v) for v in value]
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("bbox must have four finite numeric coordinates") from error
    if not all(math.isfinite(v) for v in box):
        raise ValueError("bbox must be finite")
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        raise ValueError("bbox has inverted or empty geometry")
    clipped = [max(0.0, x0), max(0.0, y0), min(float(width), x1), min(float(height), y1)]
    if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
        raise ValueError("bbox is outside the image")
    return box, clipped


def _integer_box(box, width, height, tolerance=0):
    return [max(0, math.floor(box[0]) - tolerance),
            max(0, math.floor(box[1]) - tolerance),
            min(width, math.ceil(box[2]) + tolerance),
            min(height, math.ceil(box[3]) + tolerance)]


def _foreground(rgb):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    minimum_side = min(gray.shape)
    size = min(LOCAL_BACKGROUND_KERNEL, minimum_side if minimum_side % 2 else minimum_side - 1)
    size = max(1, size)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    low, reference = (float(v) for v in np.percentile(background, [10, 90]))
    spread = reference - low
    # Black-hat finds dark strokes on gradual gray backgrounds. An explicit
    # dark anchor also retains interiors of large filled figures; the stricter
    # limit on uneven pages avoids classifying a gray gradient as a whole block.
    dark_limit = max(0.0, reference - 42) if spread <= 40 else min(64.0, max(0.0, reference - 100))
    local = background.astype(np.int16) - gray.astype(np.int16) >= LOCAL_INK_CONTRAST
    dark = (gray < dark_limit) | (gray <= 32)
    chroma = rgb.max(axis=2).astype(np.int16) - rgb.min(axis=2).astype(np.int16)
    colored = chroma >= CHROMA_CONTRAST
    return np.ascontiguousarray(local | dark | colored), {
        "method": "local gray closing contrast, explicit dark anchor, and RGB chroma",
        "local_background_kernel_pixels": size,
        "minimum_local_dark_contrast": LOCAL_INK_CONTRAST,
        "minimum_rgb_chroma": CHROMA_CONTRAST,
        "background_p10": low, "background_p90": reference,
        "background_spread": spread, "dark_anchor_gray_below": dark_limit,
        "absolute_very_dark_gray_at_most": 32,
    }


def audit_scan_page(image_rgb, regions, words, *, min_confidence=0.75):
    """Audit visible raster ink against owned processed regions and OCR units.

    ``regions`` contain ``bbox`` and a nonempty string ``owner`` (``owner_id``
    is an explicit alias). Only these regions establish ownership. ``words``
    contain ``bbox``, ``text`` and ``confidence`` in [0, 1]; no PDF glyph count
    is required or inferred. Passing 75 is an error, not an implicit percent.
    Inputs are not changed. Invalid individual records become diagnostics.
    """
    rgb = np.asarray(image_rgb)
    if rgb.ndim != 3 or rgb.shape[2] != 3 or rgb.dtype != np.uint8:
        raise ValueError("image_rgb must be a nonempty HxWx3 uint8 RGB image")
    height, width = rgb.shape[:2]
    if not height or not width:
        raise ValueError("image_rgb must be nonempty")
    if height * width > MAX_IMAGE_PIXELS:
        raise ValueError("scan raster exceeds the bounded 40-million-pixel budget")
    if isinstance(min_confidence, bool) or not isinstance(min_confidence, Real) or not math.isfinite(min_confidence) or not 0 <= min_confidence <= 1:
        raise ValueError("min_confidence must be a finite value in [0, 1]")
    if not isinstance(regions, (list, tuple)) or not isinstance(words, (list, tuple)):
        raise ValueError("regions and words must be lists or tuples")
    rgb = np.ascontiguousarray(rgb)
    foreground, method = _foreground(rgb)
    owned = np.zeros((height, width), dtype=bool)
    issues, valid_regions, invalid_regions = [], 0, 0

    def issue(code, detail, severity="warning", **evidence):
        issues.append({"code": code, "severity": severity, "detail": detail, **evidence})

    for index, region in enumerate(regions):
        owner = region.get("owner", region.get("owner_id")) if isinstance(region, dict) else None
        if not isinstance(owner, str) or not owner.strip():
            invalid_regions += 1
            issue("scan_region_owner_missing", "An unowned region cannot establish source coverage", "error", region_index=index)
            continue
        try:
            original, box = _box(region.get("bbox"), width, height)
        except ValueError as error:
            invalid_regions += 1
            issue("scan_region_bbox_invalid", str(error), "error", region_index=index, owner=owner)
            continue
        if any(abs(a - b) > BBOX_TOLERANCE_PIXELS for a, b in zip(original, box)):
            issue("scan_region_clipped", "Region extended outside the original image and was explicitly clipped", region_index=index, owner=owner, bbox=original, clipped_bbox=box)
        x0, y0, x1, y1 = _integer_box(box, width, height, BBOX_TOLERANCE_PIXELS)
        owned[y0:y1, x0:x1] = True
        valid_regions += 1

    unassigned = foreground & ~owned
    foreground_pixels = int(np.count_nonzero(foreground))
    unassigned_pixels = int(np.count_nonzero(unassigned))
    covered_pixels = foreground_pixels - unassigned_pixels
    count, labels, stats, _ = cv2.connectedComponentsWithStats(unassigned.astype(np.uint8), connectivity=8)
    del labels  # Full-resolution label raster is unnecessary after statistics.
    component_count = count - 1
    indices = np.argsort(-stats[1:, cv2.CC_STAT_AREA], kind="stable")[:MAX_REPORTED_COMPONENTS] + 1
    components, noise_pixels, noise_count = [], 0, 0
    for label in indices:
        x, y, w, h, area = (int(v) for v in stats[label])
        tiny = area <= TINY_NOISE_MAX_AREA and w <= TINY_NOISE_MAX_SIDE and h <= TINY_NOISE_MAX_SIDE
        isolated = False
        if tiny:
            radius = TINY_NOISE_ISOLATION_PIXELS
            vicinity = foreground[max(0, y-radius):min(height, y+h+radius), max(0, x-radius):min(width, x+w+radius)]
            isolated = int(np.count_nonzero(vicinity)) == area
        noise = tiny and isolated
        components.append({"id": f"ink-component-{int(label)}", "bbox": [x, y, x+w, y+h], "area": area,
                           "meaningful": not noise, "tiny_isolated_noise": noise})
        if noise:
            noise_count += 1
            noise_pixels += area
    omitted_components = component_count - len(components)
    # Any unreported components are conservatively meaningful, never noise.
    meaningful_count = component_count - noise_count
    if meaningful_count:
        issue("scan_unassigned_ink", "Visible meaningful raster components have no processed region owner", "error",
              unassigned_ink_pixels=unassigned_pixels, meaningful_component_count=meaningful_count,
              components=[c for c in components if c["meaningful"]][:24])
    if omitted_components:
        issue("scan_component_evidence_truncated", "Component evidence exceeded the explicit reporting cap; omitted components remain unresolved", "error",
              total_components=component_count, reported_components=len(components), omitted_components=omitted_components)
    if noise_count:
        issue("scan_tiny_unassigned_noise", "Tiny isolated components were downgraded as possible noise; pixels remain in unassigned totals",
              component_count=noise_count, pixels=noise_pixels)

    low_units, unknown_confidence, unowned_units, no_ink_units, invalid_units = [], [], [], [], []
    nonempty_words = 0
    for index, word in enumerate(words):
        if not isinstance(word, dict):
            invalid_units.append({"index": index, "reason": "OCR unit is not a mapping"})
            continue
        text = word.get("text")
        if not isinstance(text, str) or not text.strip():
            invalid_units.append({"index": index, "reason": "OCR unit has no nonempty text"})
            continue
        nonempty_words += 1
        try:
            original, box = _box(word.get("bbox"), width, height)
        except ValueError as error:
            invalid_units.append({"index": index, "reason": str(error)})
            continue
        unit = {"index": index, "bbox": original, "text": text}
        confidence = word.get("confidence")
        valid_confidence = (not isinstance(confidence, bool) and isinstance(confidence, Real)
                            and math.isfinite(confidence) and 0 <= confidence <= 1)
        if not valid_confidence:
            unknown_confidence.append({**unit, "confidence": None, "reason": "confidence must be finite in [0, 1]; no percent conversion inferred"})
        elif confidence < min_confidence:
            low_units.append({**unit, "confidence": float(confidence)})
        if any(abs(a - b) > BBOX_TOLERANCE_PIXELS for a, b in zip(original, box)):
            issue("scan_ocr_unit_clipped", "OCR unit extended outside the source image", unit_index=index, bbox=original, clipped_bbox=box)
        x0, y0, x1, y1 = _integer_box(box, width, height)
        ink_here = int(np.count_nonzero(foreground[y0:y1, x0:x1]))
        unowned_here = int(np.count_nonzero(unassigned[y0:y1, x0:x1]))
        if not ink_here:
            no_ink_units.append(unit)
        elif unowned_here:
            unowned_units.append({**unit, "ink_pixels": ink_here, "unassigned_ink_pixels": unowned_here})
    if low_units:
        issue("scan_low_confidence_ocr", "OCR units below the configured confidence threshold require review", unit_count=len(low_units), min_confidence=float(min_confidence), units=low_units[:24])
    if unknown_confidence:
        issue("scan_ocr_confidence_unavailable", "OCR confidence is missing or outside the declared [0, 1] scale", unit_count=len(unknown_confidence), units=unknown_confidence[:24])
    if invalid_units:
        issue("scan_ocr_unit_invalid", "Invalid OCR units are not recognition evidence", "error", unit_count=len(invalid_units), units=invalid_units[:24])
    if unowned_units:
        issue("scan_ocr_units_unowned", "Visible ink inside OCR-unit boxes is not fully owned", unit_count=len(unowned_units), units=unowned_units[:24])
    if no_ink_units:
        issue("scan_ocr_units_without_visible_ink", "OCR-unit boxes have no detected foreground; boxes may be estimated or text hallucinated", unit_count=len(no_ink_units), units=no_ink_units[:24])
    if foreground_pixels and not nonempty_words:
        issue("scan_no_ocr_units", "No OCR language units were supplied. Owned image protection may be appropriate, but optical recognition completeness remains unverified")
    if not foreground_pixels:
        issue("scan_no_visible_ink", "No foreground was detected; this may be a blank page or low-contrast content. It is not 100% recognition evidence")
    if method["background_spread"] > 48:
        issue("scan_nonuniform_background", "Strongly varying page background makes foreground segmentation uncertain; manual review is required")

    return {
        "schema_version": 1, "coverage_basis": "raster-ink-and-ocr-units",
        "coordinate_basis": "original-image-pixels, top-left origin, exclusive right/bottom",
        "image_size": [width, height], "foreground_pixels": foreground_pixels,
        "covered_ink_pixels": covered_pixels, "unassigned_ink_pixels": unassigned_pixels,
        "ink_coverage_ratio": covered_pixels / foreground_pixels if foreground_pixels else None,
        "unassigned_components": components, "unassigned_component_count": component_count,
        "unassigned_meaningful_component_count": meaningful_count,
        "unassigned_meaningful_ink_pixels": unassigned_pixels - noise_pixels,
        "unassigned_noise_component_count": noise_count, "unassigned_noise_pixels": noise_pixels,
        "component_evidence_truncated": bool(omitted_components), "omitted_component_count": omitted_components,
        "regions": {"supplied": len(regions), "valid_owned": valid_regions, "invalid": invalid_regions},
        "ocr_units": {"supplied": len(words), "nonempty_text": nonempty_words,
                      "invalid": len(invalid_units), "low_confidence": len(low_units),
                      "unknown_confidence": len(unknown_confidence), "unowned": len(unowned_units),
                      "without_visible_ink": len(no_ink_units)},
        "low_confidence_units": low_units, "unknown_confidence_units": unknown_confidence,
        "raster_coverage_complete": bool(foreground_pixels and not meaningful_count and not invalid_regions),
        "optical_recognition_verified": False, "recognition_accuracy": None,
        "status": "needs_review" if issues else "raster_regions_covered",
        "thresholds": {"source_bbox_tolerance_pixels": BBOX_TOLERANCE_PIXELS,
                       "min_confidence": float(min_confidence), "tiny_noise_max_area": TINY_NOISE_MAX_AREA,
                       "tiny_noise_max_side": TINY_NOISE_MAX_SIDE,
                       "tiny_noise_isolation_pixels": TINY_NOISE_ISOLATION_PIXELS,
                       "max_reported_components": MAX_REPORTED_COMPONENTS},
        "foreground_method": method, "issues": issues,
        "limitations": [
            "CTC word/character boxes are estimates, not native source glyph boundaries.",
            "Owned region coverage cannot prove that OCR omitted no characters or recognized words, formulas or code correctly.",
            "Confidence values are uncalibrated model scores, not measured recognition accuracy; high confidence is not semantic proof.",
            "Foreground segmentation is a documented heuristic; faint low-contrast marks, colored backgrounds, photographs and uneven illumination require visual review.",
            "Tiny isolated noise is explicitly reported and retained in raw unassigned pixel totals; small punctuation may still need inspection.",
            "Zero PDF glyphs or absent OCR units never imply complete optical recognition. No native text layer is required by this check.",
        ],
    }
