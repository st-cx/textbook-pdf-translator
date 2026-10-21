"""Conservative, read-only adapter for native ruled PDF table grids.

The caller retains responsibility for formula glyph crops and book-level table
continuations.  No files, assets, translations or PDF objects are modified here.
Merged, clipped and overlapping grids are rejected rather than flattened.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from bisect import bisect_right

import pymupdf

_TOLERANCE = .8
_MATH_WORDS = {"sin", "cos", "tan", "cot", "sec", "csc", "log", "ln", "exp", "lim",
               "mod", "gcd", "lcm", "max", "min", "det", "xor", "and", "or", "not", "iff"}
_TRUTH_WORDS = {"T", "F", "TRUE", "FALSE", "⊤", "⊥"}


def _reject(diagnostics, code, detail, **evidence):
    if diagnostics is not None:
        diagnostics.append({"code": code, "detail": detail, "evidence": evidence})
    return None


def _rect(value):
    values = list(value)
    if len(values) != 4 or not all(math.isfinite(float(v)) for v in values):
        raise ValueError("bbox must contain four finite coordinates")
    result = pymupdf.Rect(values)
    if result.is_empty or result.is_infinite or result.width <= 0 or result.height <= 0:
        raise ValueError("bbox must have positive width and height")
    return result


def _inside(inner, outer, tolerance=_TOLERANCE):
    return (inner.x0 >= outer.x0 - tolerance and inner.y0 >= outer.y0 - tolerance
            and inner.x1 <= outer.x1 + tolerance and inner.y1 <= outer.y1 + tolerance)


def _kind(text):
    """Classify literal cells without rewriting any mathematical source text."""
    if not text.strip():
        return "empty", False
    tokens = [t for t in re.split(r"[\s,;|]+", text.strip()) if t]
    if tokens and all(token.upper() in _TRUTH_WORDS for token in tokens):
        return "truth", False
    if re.fullmatch(r"[\d\s.,+\-−±/%‰:;()\[\]{}×·*^Ee]+", text) and re.search(r"\d", text):
        return "numeric", False
    words = re.findall(r"[A-Za-z][A-Za-z'’\-]{2,}", text)
    natural = [word for word in words if word.lower() not in _MATH_WORDS]
    chinese = bool(re.search(r"[\u3400-\u9fff]", text))
    if natural or chinese:
        return "natural_language", bool(natural)
    return "formula", False


def _spans(page, bbox):
    content = page.get_text("dict", clip=bbox, flags=pymupdf.TEXTFLAGS_TEXT)
    return [span for block in content.get("blocks", []) for line in block.get("lines", [])
            for span in line.get("spans", []) if span.get("text", "").strip()]


def _bold_share(spans):
    total = sum(len(span.get("text", "").strip()) for span in spans)
    if not total:
        return 0.0
    bold = sum(len(span.get("text", "").strip()) for span in spans
               if span.get("flags", 0) & pymupdf.TEXT_FONT_BOLD
               or re.search(r"bold|demi|black|heavy|semibold", span.get("font", ""), re.I))
    return bold / total


def _header_rows(page, rows):
    first, second = rows[0], rows[1]
    top_bbox = pymupdf.Rect(first[0]["bbox"][0], first[0]["bbox"][1], first[-1]["bbox"][2], first[0]["bbox"][3])
    next_bbox = pymupdf.Rect(second[0]["bbox"][0], second[0]["bbox"][1], second[-1]["bbox"][2], second[0]["bbox"][3])
    first_nonempty = [cell for cell in first if cell["text"].strip()]
    body_nonempty = [cell for row in rows[1:] for cell in row if cell["text"].strip()]
    if not first_nonempty:
        return 0, "empty_first_row"
    if _bold_share(_spans(page, top_bbox)) >= .5 and _bold_share(_spans(page, next_bbox)) < .25:
        return 1, "bold_header_contrasts_with_body"
    if (all(cell["kind"] == "natural_language" for cell in first_nonempty) and body_nonempty
            and all(cell["kind"] in {"numeric", "truth", "formula"} for cell in body_nonempty)):
        return 1, "text_column_labels_above_literal_values"
    if (all(cell["kind"] == "formula" for cell in first_nonempty) and body_nonempty
            and all(cell["kind"] == "truth" for cell in body_nonempty)):
        return 1, "symbol_column_labels_above_truth_values"
    # Native Table.header defaults to the first row even for headerless data.
    # Repeating that row would duplicate data, so uncertain headers stay zero.
    return 0, "header_not_verified_do_not_repeat_first_data_row"


def extract_table_grid(page, bbox, *, diagnostics=None) -> dict | None:
    """Inspect one entire native grid; merged cells always remain unsupported."""
    return _extract_table_grid(page, bbox, diagnostics=diagnostics)


def extract_table_body_grid(page, original_bbox, body_top, *, diagnostics=None) -> dict | None:
    """Resolve a rectangular body beneath explicitly bounded caption bands.

    Detection always inspects the full original grid. Only consecutive merged
    full-width prefix rows containing a TABLE/表 caption may be excluded. Their
    original rectangles, text and glyph counts are returned for the caller to
    preserve separately. The supplied body_top must match a native boundary;
    no cell may be split or any ordinary data/header row silently discarded.
    """
    return _extract_table_grid(page, original_bbox, diagnostics=diagnostics, body_top=body_top)


def _extract_table_grid(page, bbox, *, diagnostics=None, body_top=None) -> dict | None:
    """Return one confident rectangular native grid, or None with diagnostics.

    ``rows`` is a rectangular matrix of cell dictionaries.  Every accepted cell
    has explicit row/column, bbox, unchanged text, kind, translatable, rowspan=1
    and colspan=1.  V1 rejects merged/null cells with a reason.  ``header_rows``
    is verified independently of PyMuPDF's default first-row header assumption.
    ``continuation_key`` is a *candidate* identity only: joining across pages
    additionally requires adjacent pages and matching table/caption context.
    """
    try:
        requested = _rect(bbox)
    except (TypeError, ValueError, OverflowError):
        return _reject(diagnostics, "table_invalid_bbox", "Table region is not a finite positive rectangle")
    bounds = pymupdf.Rect(page.rect)
    if not _inside(requested, bounds, tolerance=2):
        return _reject(diagnostics, "table_bbox_outside_page", "Table region extends outside its source page")
    if page.rotation:
        # PyMuPDF's native finder temporarily inserts a content stream when
        # de-rotating pages.  Reject here to keep the source document immutable.
        return _reject(diagnostics, "table_rotated_page_unsupported",
                       "Native detection would modify a rotated page; preserve its source table instead")
    try:
        # Inspect the full page, then select the requested region.  Clipping
        # detection can turn half of a larger table into an apparent whole grid.
        try:
            finder = page.find_tables(strategy="lines", use_layout=False)
        except TypeError as error:
            if "use_layout" not in str(error):
                raise
            finder = page.find_tables(strategy="lines")
        tables = [table for table in finder.tables if table.row_count >= 2 and table.col_count >= 2]
    except Exception as error:
        return _reject(diagnostics, "table_native_detection_failed", "Native table detection failed",
                       error_type=type(error).__name__)
    contained = [table for table in tables if _inside(_rect(table.bbox), requested, tolerance=2)]
    if len(contained) != 1:
        return _reject(diagnostics, "table_grid_not_unique", "Expected one complete native grid in the requested region",
                       candidate_count=len(contained))
    table = contained[0]
    table_bbox = _rect(table.bbox)
    for other in tables:
        if other is not table and (table_bbox & _rect(other.bbox)).get_area() > .5:
            return _reject(diagnostics, "table_overlapping_grids", "Native table grids overlap; ownership is ambiguous")
    native_rows = table.rows
    if len(native_rows) != table.row_count or any(len(row.cells) != table.col_count for row in native_rows):
        return _reject(diagnostics, "table_ragged_grid", "Native row/column dimensions are inconsistent")
    original_bbox, prefix_rows, split = pymupdf.Rect(table_bbox), [], 0
    if body_top is not None:
        try:
            top = float(body_top)
            if not math.isfinite(top):
                raise ValueError("nonfinite body boundary")
            starts = [r for r, row in enumerate(native_rows)
                      if row.cells[0] is not None and abs(_rect(row.cells[0]).y0 - top) <= _TOLERANCE]
            if len(starts) != 1 or starts[0] < 1 or table.row_count - starts[0] < 2:
                return _reject(diagnostics, "table_body_boundary_unverified",
                               "Body start must be one native row boundary after a caption and leave at least two rows")
            split = starts[0]
            previous_bottom = table_bbox.y0
            for r, row in enumerate(native_rows[:split]):
                present = [(c, cell) for c, cell in enumerate(row.cells) if cell is not None]
                if len(present) != 1 or present[0][0] != 0:
                    return _reject(diagnostics, "table_caption_prefix_not_full_width",
                                   "Only single full-width merged caption bands may precede the body", row=r)
                rect = _rect(present[0][1])
                if (abs(rect.x0-table_bbox.x0) > _TOLERANCE
                        or abs(rect.x1-table_bbox.x1) > _TOLERANCE
                        or abs(rect.y0-previous_bottom) > _TOLERANCE
                        or rect.y1 > top + _TOLERANCE or rect.height <= 2):
                    return _reject(diagnostics, "table_caption_prefix_not_full_width",
                                   "Caption bands must cover the original width without gaps or body overlap", row=r)
                prefix_rows.append({"native_row": r, "bbox": list(rect)})
                previous_bottom = rect.y1
            actual_top = _rect(native_rows[split].cells[0]).y0
            if abs(previous_bottom-actual_top) > _TOLERANCE:
                return _reject(diagnostics, "table_caption_body_gap_or_overlap",
                               "Caption and body do not share a native boundary")
            table_bbox = pymupdf.Rect(table_bbox.x0, actual_top, table_bbox.x1, table_bbox.y1)
            native_rows = native_rows[split:]
        except (TypeError, ValueError, OverflowError):
            return _reject(diagnostics, "table_body_boundary_unverified", "Body boundary or caption cell is invalid")
    missing = [(r+split, c) for r, row in enumerate(native_rows) for c, cell in enumerate(row.cells) if cell is None]
    if missing:
        return _reject(diagnostics, "table_merged_or_missing_cells",
                       "Merged or absent native cells are unsupported; preserve the original table instead",
                       positions=missing, rows=table.row_count, columns=table.col_count)
    try:
        text_rows = table.extract()
        if len(text_rows) != table.row_count or any(len(row) != table.col_count for row in text_rows):
            return _reject(diagnostics, "table_text_grid_mismatch", "Extracted text does not match the native cell matrix")
        if prefix_rows:
            for entry, values in zip(prefix_rows, text_rows[:split]):
                if not isinstance(values[0], str) or any(value not in (None, "") for value in values[1:]):
                    return _reject(diagnostics, "table_caption_text_ambiguous", "Caption text does not belong to its full-width cell")
                entry["text"] = values[0]
            caption = " ".join(entry["text"] for entry in prefix_rows)
            if not re.search(r"(?:^|\s)(?:TABLE\s+\w|表\s*[0-9一二三四五六七八九十])", caption, re.I):
                return _reject(diagnostics, "table_caption_prefix_unverified", "Excluded merged bands lack an explicit numbered table caption")
            text_rows = text_rows[split:]
        rectangles = [[_rect(cell) for cell in row.cells] for row in native_rows]
    except (TypeError, ValueError, RuntimeError):
        return _reject(diagnostics, "table_invalid_native_cells", "Native cells or their text cannot be read reliably")
    first = rectangles[0]
    for r, row in enumerate(rectangles):
        for c, cell in enumerate(row):
            if (not _inside(cell, table_bbox) or cell.width <= 2 or cell.height <= 2
                    or abs(cell.x0 - first[c].x0) > _TOLERANCE or abs(cell.x1 - first[c].x1) > _TOLERANCE
                    or abs(cell.y0 - row[0].y0) > _TOLERANCE or abs(cell.y1 - row[0].y1) > _TOLERANCE):
                return _reject(diagnostics, "table_unaligned_or_spanning_cells", "Cell boundaries do not form one rectangular grid",
                               row=r, column=c)
            if c and abs(row[c - 1].x1 - cell.x0) > _TOLERANCE:
                return _reject(diagnostics, "table_column_gap_or_overlap", "Adjacent columns do not share a boundary")
        if r and abs(rectangles[r - 1][0].y1 - row[0].y0) > _TOLERANCE:
            return _reject(diagnostics, "table_row_gap_or_overlap", "Adjacent rows do not share a boundary")
    if (abs(first[0].x0 - table_bbox.x0) > _TOLERANCE or abs(first[-1].x1 - table_bbox.x1) > _TOLERANCE
            or abs(first[0].y0 - table_bbox.y0) > _TOLERANCE
            or abs(rectangles[-1][0].y1 - table_bbox.y1) > _TOLERANCE):
        return _reject(diagnostics, "table_incomplete_grid_coverage", "Cell matrix does not cover the native table region")
    header = getattr(table, "header", None)
    if header is not None and getattr(header, "external", False):
        return _reject(diagnostics, "table_external_header_unresolved",
                       "Detected column labels lie outside the ruled grid; caller must preserve or explicitly resolve them",
                       header_bbox=list(header.bbox), names=list(header.names))
    rows = []
    for r, native_row in enumerate(rectangles):
        cells = []
        for c, cell_bbox in enumerate(native_row):
            text = text_rows[r][c]
            if not isinstance(text, str):
                return _reject(diagnostics, "table_missing_cell_text", "A native cell has an ambiguous text value", row=r, column=c)
            kind, translatable = _kind(text)
            cells.append({"row": r, "column": c, "bbox": list(cell_bbox), "text": text,
                          "kind": kind, "translatable": translatable, "rowspan": 1, "colspan": 1})
        rows.append(cells)
    # Audit source character centres against cells.  Formula glyphs remain
    # source objects; no text synthesis or mathematical conversion occurs.
    source_chars, unassigned = 0, 0
    row_starts = [row[0].y0 for row in rectangles]
    column_starts = [[cell.x0 for cell in row] for row in rectangles]
    try:
        raw = page.get_text("rawdict", clip=table_bbox, flags=pymupdf.TEXTFLAGS_RAWDICT)
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        if not char.get("c", "").strip():
                            continue
                        source_chars += 1
                        char_bbox = char["bbox"]
                        x, y = (char_bbox[0] + char_bbox[2]) / 2, (char_bbox[1] + char_bbox[3]) / 2
                        owner_count = 0
                        row_index = bisect_right(row_starts, y) - 1
                        # The preceding row/column also participates when
                        # native floating boundaries overlap slightly.
                        for r in (row_index, row_index - 1):
                            if not 0 <= r < len(rectangles):
                                continue
                            column = bisect_right(column_starts[r], x) - 1
                            for c in (column, column - 1):
                                if 0 <= c < len(rectangles[r]):
                                    cell = rectangles[r][c]
                                    owner_count += int(cell.x0 <= x < cell.x1 and cell.y0 <= y < cell.y1)
                        if owner_count != 1:
                            unassigned += 1
        if unassigned:
            return _reject(diagnostics, "table_glyph_ownership_ambiguous", "Source glyphs cannot be assigned to exactly one cell",
                           unassigned=unassigned, source_chars=source_chars)
    except (TypeError, KeyError, RuntimeError):
        return _reject(diagnostics, "table_glyph_audit_failed", "Could not verify source glyph ownership inside the grid")
    prefix_chars = 0
    if prefix_rows:
        # A source glyph must remain in exactly one excluded caption band or
        # the body. A tall glyph crossing the native boundary is ambiguous.
        try:
            raw = page.get_text("rawdict", clip=original_bbox, flags=pymupdf.TEXTFLAGS_RAWDICT)
            prefix_rectangles = [_rect(entry["bbox"]) for entry in prefix_rows]
            for block in raw.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        for char in span.get("chars", []):
                            if not char.get("c", "").strip():
                                continue
                            rect = _rect(char["bbox"])
                            x, y = (rect.x0+rect.x1)/2, (rect.y0+rect.y1)/2
                            if y >= table_bbox.y0:
                                if rect.y0 < table_bbox.y0 - _TOLERANCE:
                                    return _reject(diagnostics, "table_caption_body_glyph_overlap",
                                                   "A body glyph crosses the caption boundary")
                                continue
                            owners = [r for r, band in enumerate(prefix_rectangles)
                                      if band.x0 <= x < band.x1 and band.y0 <= y < band.y1]
                            if len(owners) != 1 or rect.y1 > table_bbox.y0 + _TOLERANCE:
                                return _reject(diagnostics, "table_caption_glyph_ownership_ambiguous",
                                               "An excluded source glyph cannot be assigned uniquely to its caption band")
                            prefix_chars += 1
        except (TypeError, KeyError, ValueError, RuntimeError):
            return _reject(diagnostics, "table_caption_glyph_audit_failed", "Could not verify excluded caption glyph ownership")
    header_rows, header_evidence = _header_rows(page, rows)
    widths = [cell.width for cell in first]
    labels = [[re.sub(r"\s+", " ", cell["text"]).strip().casefold() for cell in row]
              for row in rows[:header_rows]]
    header_signature = hashlib.sha256(json.dumps(labels, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() if labels else None
    normalized_widths = [round(width / sum(widths), 3) for width in widths]
    continuation = hashlib.sha256(json.dumps({"header": labels, "columns": normalized_widths},
                                             ensure_ascii=False, sort_keys=True).encode()).hexdigest() if labels else None
    return {"bbox": list(table_bbox), "original_bbox": list(original_bbox),
            "excluded_prefix_rows": prefix_rows, "rows": rows, "row_count": len(rows), "col_count": len(widths),
            "col_widths": widths, "header_rows": header_rows, "header_signature": header_signature,
            "continuation_key": continuation, "confidence": .98 if header_rows else .95,
            "repeat_header": bool(header_rows),
            "evidence": {"engine": "PyMuPDF " + pymupdf.VersionBind, "strategy": "native_ruled_lines",
                         "complete_rectangular_grid": True, "merged_cells": False,
                         "source_chars": source_chars, "unassigned_source_chars": unassigned,
                         "excluded_caption_rows": len(prefix_rows), "excluded_caption_source_chars": prefix_chars,
                         "caption_preservation_required": bool(prefix_rows),
                         "body_uses_original_native_boundaries": True,
                         "header": header_evidence, "native_header_external": False,
                         "continuation_requires_adjacent_page_and_caption": True,
                         "full_page_detection_prevents_clipped_grids": True,
                         "text_line_structure_not_inferred": True}}
