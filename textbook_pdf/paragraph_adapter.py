"""Pinned local neural structure hints; native text and vector ownership stay canonical.

The upstream paragraph stage mutates its IL, so this adapter always works on a
copy. It never translates, renders a formula, or downloads a model. Glyph
coverage must be exact before a page's hints can be consumed by extraction.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from importlib.metadata import version
import hashlib
import json
import math
from pathlib import Path
import threading
import time

BABELDOC_VERSION = "0.6.4"
MODEL_SHA3_256 = "60be061226930524958b5465c8c04af3d7c03bcb0beb66454f5da9f792e3cf2a"
MODEL_SOURCE = "https://huggingface.co/wybxc/DocLayout-YOLO-DocStructBench-onnx/resolve/main/doclayout_yolo_docstructbench_imgsz1024.onnx"
_MODELS = {}
_MODEL_LOCK = threading.RLock()
_INFERENCE_LOCK = threading.RLock()


class ParagraphHintsUnavailable(RuntimeError):
    pass


def _mapped_box(box, prepared_height, offset):
    if box is None:
        return None
    values = [float(box.x)-offset[0], prepared_height-float(box.y2)-offset[1],
              float(box.x2)-offset[0], prepared_height-float(box.y)-offset[1]]
    if not all(math.isfinite(value) for value in values) or values[2] < values[0] or values[3] < values[1]:
        raise ParagraphHintsUnavailable("Invalid upstream character/paragraph geometry")
    return values


def _signature(char):
    box = char.box
    return (char.char_unicode, getattr(char.pdf_style, "font_id", None),
            *(round(float(v), 5) for v in (box.x, box.y, box.x2, box.y2)))


def _characters(composition):
    for name in ("pdf_line", "pdf_formula", "pdf_same_style_characters"):
        part = getattr(composition, name, None)
        if part is not None:
            return list(getattr(part, "pdf_character", []))
    char = getattr(composition, "pdf_character", None)
    return [char] if char is not None else []


def _source_records(page):
    records, whitespace, unknown = [], 0, 0
    for index, char in enumerate(page.pdf_character):
        value = char.char_unicode
        if value is None or value == "":
            unknown += 1
        elif not value.strip():
            whitespace += 1
        else:
            records.append((index, char, _signature(char)))
    return records, whitespace, unknown


def _page_hints(page, records, physical_page, prepared_height, geometry):
    offset = geometry.get("crop_offset")
    if (not isinstance(offset, (list, tuple)) or len(offset) != 2
            or not all(math.isfinite(float(v)) for v in offset)):
        raise ParagraphHintsUnavailable("Original crop_offset is required for neural hints")
    if geometry.get("rotation", 0):
        raise ParagraphHintsUnavailable("Rotated source pages require an explicit verified transform")
    offset = tuple(float(v) for v in offset)
    original, whitespace, unknown = records
    index_by_identity = {id(char): index for index, char, signature in original}
    indices_by_signature = {}
    for index, char, signature in original:
        indices_by_signature.setdefault(signature, []).append(index)
    assigned, extras = Counter(), 0
    paragraphs = []
    for order, paragraph in enumerate(page.pdf_paragraph):
        indices = []
        for composition in paragraph.pdf_paragraph_composition:
            for char in _characters(composition):
                if not (char.char_unicode or "").strip():
                    continue
                index = index_by_identity.get(id(char))
                if index is None:
                    matching = indices_by_signature.get(_signature(char), [])
                    available = [value for value in matching if not assigned[value]]
                    index = available[0] if available else None
                if index is None:
                    extras += 1
                else:
                    assigned[index] += 1
                    indices.append(index)
        fingerprint = hashlib.sha256(json.dumps([physical_page, indices], separators=(",", ":")).encode()).hexdigest()[:20]
        paragraphs.append({"id": f"babel-p{physical_page:04d}-{fingerprint}",
                           "bbox": _mapped_box(paragraph.box, prepared_height, offset),
                           "text": paragraph.unicode or "", "order": order,
                           "layout_class": getattr(paragraph, "layout_label", None),
                           "layout_id": getattr(paragraph, "layout_id", None),
                           "source_glyph_indices": indices,
                           "text_is_readonly_hint": True,
                           "requires_native_exercise_boundaries": True})
    missing = [index for index, char, signature in original if not assigned[index]]
    duplicated = [index for index, count in assigned.items() if count > 1]
    coverage = {"source_nonspace_glyphs": len(original), "assigned_nonspace_glyphs": sum(assigned.values()),
                "unmatched_source_glyphs": len(missing), "duplicated_source_glyphs": len(duplicated),
                "unmatched_paragraph_glyphs": extras, "unknown_source_unicode": unknown,
                "whitespace_excluded": whitespace, "whitespace_exclusion_reason": "Native text remains canonical; paragraph dummy spaces are not source glyphs",
                "exact": not (missing or duplicated or extras or unknown)}
    diagnostics = []
    if not coverage["exact"]:
        diagnostics.append({"code": "neural_paragraph_glyph_coverage", "severity": "warning", "page": physical_page,
                            "detail": "Neural structure hints rejected because source glyph coverage is not exact",
                            "missing_indices": missing[:100], "duplicated_indices": duplicated[:100],
                            "coverage": coverage})
        return {"usable": False, "coverage": coverage, "paragraphs": [], "layouts": [],
                "source_glyphs": [], "diagnostics": diagnostics}
    source_glyphs = [{"index": index, "text": char.char_unicode,
                      "bbox": _mapped_box(char.box, prepared_height, offset),
                      "visual_bbox": _mapped_box(getattr(getattr(char, "visual_bbox", None), "box", char.box), prepared_height, offset),
                      "font_id": getattr(char.pdf_style, "font_id", None)}
                     for index, char, signature in original]
    layouts = [{"id": f"babel-p{physical_page:04d}-layout-{layout.id}", "native_id": layout.id,
                "bbox": _mapped_box(layout.box, prepared_height, offset),
                "class_name": layout.class_name, "confidence": float(layout.conf)}
               for layout in page.page_layout if layout.class_name != "fallback_line"]
    return {"usable": True, "coverage": coverage, "source_glyphs": source_glyphs,
            "paragraphs": paragraphs, "layouts": layouts, "diagnostics": diagnostics}


def _load_model(model_path):
    path = Path(model_path).resolve()
    if not path.is_file():
        raise ParagraphHintsUnavailable("Pinned local ONNX model is missing; no automatic download was attempted")
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _MODEL_LOCK:
        if key in _MODELS:
            return _MODELS[key]
        if version("babeldoc") != BABELDOC_VERSION:
            raise ParagraphHintsUnavailable("Unsupported BabelDOC version for neural structure hints")
        checksum = hashlib.sha3_256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4*1024*1024), b""):
                checksum.update(chunk)
        if checksum.hexdigest() != MODEL_SHA3_256:
            raise ParagraphHintsUnavailable("Local ONNX checksum differs from pinned upstream model")
        from babeldoc.docvision.doclayout import OnnxModel
        model = OnnxModel(str(path))
        provenance = {"babeldoc_version": BABELDOC_VERSION, "model_path": str(path),
                      "model_bytes": stat.st_size, "model_sha3_256": checksum.hexdigest(),
                      "checksum_verified": True, "official_source": MODEL_SOURCE,
                      "loader": "babeldoc.docvision.doclayout.OnnxModel (explicit local path)",
                      "onnxruntime_version": version("onnxruntime"),
                      "execution_providers": list(model.model.get_providers()),
                      "automatic_model_download": False, "translator_used": False}
        _MODELS.clear()
        _MODELS[key] = (model, provenance)
        return model, provenance


def extract_paragraph_hints(document, prepared_pdf, config, page_map, source_geometry, model_path, outdir):
    """Run local LayoutParser/ParagraphFinder on a copy of an existing parsed IL.

    ``prepared_pdf`` MUST be the normalized input PDF used by the character
    parser. ``page_map`` maps its zero-based page indices to physical page
    numbers. ``source_geometry[physical]`` supplies original ``crop_offset``
    and ``rotation``. Boxes are returned in original top-left coordinates;
    glyph indices refer to the original per-page IL character list. Paragraph
    text is read-only context, never a replacement for canonical native text.
    The caller configures BabelDOC's workspace cache before importing upstream.
    """
    started = time.perf_counter()
    page_map = list(page_map)
    model, provenance = _load_model(model_path)
    from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
    from babeldoc.const import close_process_pool
    copied = deepcopy(document)
    source = {int(page.page_number): _source_records(page) for page in copied.page}
    timings = {}
    monitor, old_model = config.progress_monitor, config.doc_layout_model
    old_disable = monitor.disable
    with _INFERENCE_LOCK:
        try:
            config.doc_layout_model = model
            monitor.disable = True
            at = time.perf_counter()
            LayoutParser(config).process(copied, prepared_pdf)
            close_process_pool()
            timings["layout_seconds"] = time.perf_counter()-at
            at = time.perf_counter()
            ParagraphFinder(config).process(copied)
            timings["paragraph_seconds"] = time.perf_counter()-at
        finally:
            config.doc_layout_model = old_model
            monitor.disable = old_disable
    pages = {}
    for page in copied.page:
        index = int(page.page_number)
        if not 0 <= index < len(page_map) or index >= len(prepared_pdf):
            raise ParagraphHintsUnavailable("IL page index is outside the prepared page map")
        physical = int(page_map[index])
        geometry = source_geometry.get(physical, source_geometry.get(str(physical)))
        if geometry is None:
            raise ParagraphHintsUnavailable("Original page geometry is missing for a neural page")
        try:
            pages[physical] = _page_hints(page, source[index], physical, float(prepared_pdf[index].rect.height), geometry)
        except ParagraphHintsUnavailable as error:
            # A malformed upstream box invalidates this page's hints only. Do
            # not normalize/invent geometry or discard verified neighboring
            # pages. Coverage counts were not fully evaluated, so do not claim
            # zero missing/duplicate glyphs for this rejected page.
            original, whitespace, unknown = source[index]
            pages[physical] = {
                "usable": False, "paragraphs": [], "layouts": [], "source_glyphs": [],
                "coverage": {"source_nonspace_glyphs": len(original), "exact": False,
                             "geometry_verified": False, "whitespace_excluded": whitespace,
                             "unknown_source_unicode": unknown,
                             "coverage_not_evaluated_reason": "upstream_geometry_unavailable"},
                "diagnostics": [{"code": "neural_paragraph_geometry_unavailable", "severity": "warning",
                                 "page": physical, "detail": str(error),
                                 "fallback": "Native source glyphs and assets remain canonical; no neural hints for this page"}],
            }
    result = {"backend": "babeldoc-neural-structure", "schema_version": 1,
              "model": provenance, "stages": ["LayoutParser", "ParagraphFinder"],
              "coordinates": "original top-left; prepared height then original crop_offset",
              "native_text_assets_and_exercises_remain_canonical": True,
              "input_il_unchanged": True, "pages": pages, "timings": timings,
              "elapsed_seconds": time.perf_counter()-started}
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    destination = outdir / "neural_structure_hints.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    result["audit_path"] = str(destination.resolve())
    return result
