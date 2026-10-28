"""Fixed-size Chinese book composition through the pinned Typst engine.

Every visible source element is tracked in Typst itself.  ``layout_map.json``
contains block fragments (not guessed glyph boxes), while protected objects
have measured rectangles.  No fit-to-box text or mathematical-object scaling
is used.  The source document and extracted assets are never modified.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from .models import Book, Element, TOKEN_RE, TRANSLATABLE, file_sha256, source_label, safe_filename, needs_translation, digest
from .exercise_flow_adapter import PLAN_KEY

TYPST_VERSION = "0.15.1"
_TEMPLATE = Path(__file__).with_name("templates") / "book.typ"
_FONT_SIZES = {"body_size": 10.5, "exercise_size": 9.5, "caption_size": 8.5}
_PERIPHERAL = {"running_header", "running_footer", "page_number"}
_EXERCISE_LABEL_RE = re.compile(r"^(\s*(?:[∗*†‡]{0,3}\s*\d+[.)]|\([a-zA-Z0-9]+\)|[a-zA-Z][.)]))\s*")


class CompositionError(RuntimeError):
    """Composition could not produce a faithful, auditable document."""


def rendering_binding(book: Book, translations: dict) -> dict:
    """Identity of document inputs and compositor files, for stale-output QA."""
    profile = {key: value for key, value in book.profile.items() if key not in {"job_dir", "asset_root"}}
    structure = {
        "pages": book.pages,
        "elements": [{"id": e.id, "bbox": e.bbox, "printed_page": e.printed_page,
                      "style": e.style, "group_id": e.group_id, "chapter": e.chapter,
                      "metadata": e.metadata} for e in book.elements],
        "assets": {aid: {"kind": a.kind, "page": a.page, "bbox": a.bbox, "width": a.width,
                         "height": a.height, "baseline": a.baseline, "metadata": a.metadata}
                   for aid, a in book.assets.items()},
    }
    adapter_identity = {}
    if book.profile.get(PLAN_KEY):
        from . import exercise_flow_adapter
        adapter_identity["exercise_flow_adapter_sha256"] = file_sha256(Path(exercise_flow_adapter.__file__).resolve())
    if book.profile.get("argument_units_v1"):
        from .argument_layout import renderer_identity
        adapter_identity.update(renderer_identity())
    if book.profile.get("source_design_panels_v1"):
        from .design_panels import renderer_identity as panel_identity
        adapter_identity.update(panel_identity())
    return {"schema_version": 1, "source_sha256": book.source_sha256,
            "renderer_identity": {"layout_sha256": file_sha256(Path(__file__).resolve()),
                                  "template_sha256": file_sha256(_TEMPLATE),
                                  "typst_version": TYPST_VERSION, **adapter_identity},
            "selected_pages": book.selected_pages,
            "elements": {e.id: e.source_hash for e in book.elements},
            "translations": {e.id: digest(translations.get(e.id)) for e in book.elements if needs_translation(e)},
            "assets": {aid: a.sha256 for aid, a in book.assets.items()},
            "profile_digest": digest(profile), "structure_digest": digest(structure)}


def _q(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _positive(value, default):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else default
    except (TypeError, ValueError):
        return default


def _number(value) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".") or "0"


@dataclass
class _Runtime:
    binary: Path | None = None
    module: object | None = None

    @classmethod
    def find(cls):
        requested = os.environ.get("TEXTBOOK_TYPST")
        candidates = [Path(requested)] if requested else []
        located = shutil.which("typst")
        if located:
            candidates.append(Path(located))
        for parent in Path(__file__).resolve().parents:
            candidates.extend((parent / "tools" / "typst.exe", parent / "tools" / "typst",
                               parent / "tools" / "typst-x86_64-pc-windows-msvc" / "typst.exe"))
        checked = set()
        for candidate in candidates:
            if candidate in checked or not candidate.is_file():
                continue
            checked.add(candidate)
            result = subprocess.run([str(candidate), "--version"], capture_output=True, text=True, check=False)
            if result.returncode == 0 and re.search(r"\b" + re.escape(TYPST_VERSION) + r"\b", result.stdout):
                return cls(binary=candidate)
            if requested and candidate == Path(requested):
                raise CompositionError(f"TEXTBOOK_TYPST 必须指向 Typst {TYPST_VERSION}：{result.stdout.strip()}")
        try:
            import typst
            version = str(getattr(typst, "__typst_version__", getattr(typst, "__version__", "")))
            if version == TYPST_VERSION:
                return cls(module=typst)
        except ImportError:
            pass
        raise CompositionError(f"未找到 Typst {TYPST_VERSION}。请设置 TEXTBOOK_TYPST 为该版本可执行文件的绝对路径。")

    def compile(self, source: Path, output: Path, root: Path):
        if self.binary:
            result = subprocess.run([str(self.binary), "compile", "--root", str(root),
                                     str(source), str(output)], capture_output=True, text=True, encoding="utf-8", check=False)
            if result.returncode:
                raise CompositionError("Typst 排版失败：\n" + result.stderr[-12000:])
            return result.stderr.strip()
        try:
            self.module.compile(str(source), output=str(output), root=str(root))
            return ""
        except Exception as exc:
            raise CompositionError(f"Typst 排版失败：{exc}") from exc

    def query(self, source: Path, root: Path):
        if self.binary:
            result = subprocess.run([str(self.binary), "query", "--root", str(root),
                                     "--format", "json", str(source), "<tb-trace>", "--field", "value"],
                                    capture_output=True, text=True, encoding="utf-8", check=False)
            if result.returncode:
                raise CompositionError("Typst 位置查询失败：\n" + result.stderr[-12000:])
            return json.loads(result.stdout)
        result = self.module.query(str(source), "<tb-trace>", field="value", root=str(root))
        return json.loads(result) if isinstance(result, str) else result


def _page_geometry(book):
    first = book.pages[0] if book.pages else {}
    width = _positive(book.profile.get("page_width", first.get("width")), 612)
    height = _positive(book.profile.get("page_height", first.get("height")), 792)
    margins = book.profile.get("margins", {})
    if not isinstance(margins, dict):
        margins = {}
    left = _positive(margins.get("left"), min(48, width * .09))
    right = _positive(margins.get("right"), min(48, width * .09))
    top = _positive(margins.get("top"), 45)
    bottom = _positive(margins.get("bottom"), 45)
    if left + right >= width - 100 or top + bottom >= height - 100:
        raise CompositionError("书籍页边距未留下足够的正文区域")
    return {"width": width, "height": height, "left": left, "right": right,
            "top": top, "bottom": bottom, "gutter": _positive(book.profile.get("column_gutter"), 18)}


def _source_roots(book, outdir):
    roots = [outdir, outdir.parent]
    for value in (book.profile.get("job_dir"), book.profile.get("asset_root"),
                  Path(book.source_path).parent):
        if value:
            roots.append(Path(value))
    return list(dict.fromkeys(Path(p).resolve() for p in roots))


def _find_asset(asset, roots):
    path = Path(asset.path)
    if path.is_absolute():
        candidates = [path]
    else:
        candidates = [root / path for root in roots]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise CompositionError(f"受保护对象文件不存在：{asset.id} ({asset.path})")


def _prepare_assets(book, outdir):
    roots = _source_roots(book, outdir)
    directory = outdir / "assets"
    directory.mkdir(exist_ok=True)
    paths = {}
    by_digest = {}
    source_hashes = {}
    bundle_counts = {}
    original_paths = {}
    for asset_id, asset in book.assets.items():
        if asset.path not in original_paths:
            original_paths[asset.path] = _find_asset(asset, roots)
        original = original_paths[asset.path]
        if original not in source_hashes:
            source_hashes[original] = file_sha256(original)
        sha = source_hashes[original]
        if asset.sha256 and asset.sha256 != sha:
            raise CompositionError(f"受保护对象内容已经变更：{asset_id}")
        page_index = asset.metadata.get("pdf_page")
        if page_index is not None:
            if original.suffix.lower() != ".pdf":
                raise CompositionError(f"非 PDF 对象 {asset_id} 不能包含 pdf_page")
            try:
                import pymupdf
                if original not in bundle_counts:
                    with pymupdf.open(original) as src:
                        bundle_counts[original] = len(src)
                index = int(page_index)
                if index < 0 or index >= bundle_counts[original]:
                    raise CompositionError(f"对象 {asset_id} 的 PDF 页索引无效")
            except ImportError as exc:
                raise CompositionError("多页 PDF 对象需要 PyMuPDF") from exc
        key = (sha, "original")
        if key in by_digest:
            paths[asset_id] = by_digest[key]
            continue
        suffix = original.suffix.lower()
        target = directory / (sha[:24] + suffix)
        # The selected bundle page is passed to Typst's image(page:) directly.
        if target.resolve() != original:
            if not target.is_file() or file_sha256(target) != sha:
                shutil.copyfile(original, target)
        relative = target.relative_to(outdir).as_posix()
        paths[asset_id] = relative
        by_digest[key] = relative
    return paths


def _prepare_native_label_previews(book, translations, outdir, *, overflow_measurements=None):
    """Keep missing diagram labels as original native ink in previews only.

    These are source-region copies, never accepted translations or replacement
    identities for the smaller protected objects contained inside the label.
    """
    explicit = overflow_measurements is not None
    labels = [e for e in book.elements if e.metadata.get("parent_figure_id")
              and _requires_translation(e) and
              (e.id in overflow_measurements if explicit else
               not isinstance(translations.get(e.id), str) or not translations[e.id].strip())]
    if explicit and {e.id for e in labels} != set(overflow_measurements):
        raise CompositionError("溢出标签的原生回退缺少实际图内标签来源")
    if not labels:
        return {}
    source = Path(book.source_path).resolve()
    if not source.is_file() or file_sha256(source) != book.source_sha256:
        raise CompositionError("保留未译图内标签需要同一份可验证的原书 PDF")
    import pymupdf
    from .native_clipping import clip_native_content
    directory = outdir / "native-labels"
    directory.mkdir(exist_ok=True)
    records = {}
    with pymupdf.open(source) as original:
        glyph_pages = {}
        for label in labels:
            covered = Counter(TOKEN_RE.findall(label.text))
            covered.update({aid: max(0, label.asset_ids.count(aid) - covered[aid])
                            for aid in set(label.asset_ids)})
            bbox = pymupdf.Rect(label.metadata.get("source_label_bbox", label.bbox))
            for aid in covered:
                asset = book.assets.get(aid)
                if asset is None or asset.page != label.page:
                    raise CompositionError(f"未译标签 {label.id} 的公式来源不完整")
                bbox |= pymupdf.Rect(asset.bbox)
            native_proof = {}
            selected = None
            if explicit:
                from .extraction import _collect
                from .native_ink_geometry import measure_selected_native_ink, capture_grid, verify_actual_capture
                if label.page not in glyph_pages:
                    glyph_pages[label.page] = _collect(original[label.page - 1])[1]
                glyphs = glyph_pages[label.page]
                owned = set(label.metadata.get("glyph_ids", []))
                owned.update(gid for aid in covered for gid in book.assets[aid].metadata.get("glyph_ids", []))
                if not owned or owned - {g["id"] for g in glyphs}:
                    raise CompositionError(f"溢出标签 {label.id} 的原生字形身份不完整")
                selected = [g for g in glyphs if g["id"] in owned and g["c"].strip()]
                if not selected:
                    raise CompositionError(f"溢出标签 {label.id} 没有可验证的可见字形")
                measured = measure_selected_native_ink(original[label.page - 1], glyphs,
                    {g["id"] for g in selected}, immutable_source_sha256=book.source_sha256)
                if measured["unverified"] or len(measured["verified"]) != len(selected):
                    raise CompositionError(f"溢出标签 {label.id} 的完整原生墨迹范围无法验证")
                for glyph in selected:
                    bbox |= pymupdf.Rect(glyph["native_ink_bbox"])
                bbox = capture_grid(bbox)
                native_proof = {"source_ink": measured, "source_glyph_ids": [g["id"] for g in selected]}
            if (not 1 <= label.page <= len(original) or bbox.is_empty or bbox.is_infinite
                    or not original[label.page - 1].rect.contains(bbox)):
                raise CompositionError(f"未译标签 {label.id} 的原书裁片区域无效")
            identity = digest({"source": book.source_sha256, "page": label.page,
                               "bbox": list(bbox), "element": label.id})
            target = directory / (identity[:24] + ".pdf")
            with pymupdf.open() as isolated, pymupdf.open() as crop:
                isolated.insert_pdf(original, from_page=label.page - 1, to_page=label.page - 1)
                if explicit:
                    selection = clip_native_content(isolated[0], bbox, selected_glyphs=selected, source_glyphs=glyphs)
                    if not selection.get("native_text_selection", {}).get("verified"):
                        raise CompositionError(f"溢出标签 {label.id} 的原生字形不能唯一选择")
                    native_proof["selection"] = selection
                else:
                    clip_native_content(isolated[0], bbox)
                page = crop.new_page(width=bbox.width, height=bbox.height)
                page.show_pdf_page(page.rect, isolated, 0, clip=bbox)
                if explicit:
                    actual = verify_actual_capture(original[label.page - 1], page, glyphs,
                        {g["id"] for g in selected}, source_to_target=[-bbox.x0, -bbox.y0],
                        expected_dimensions=[bbox.width, bbox.height])
                    if not actual["verified"]:
                        raise CompositionError(f"溢出标签 {label.id} 的原生字体、字形、颜色或位置验证失败")
                    native_proof["actual_native_capture"] = actual
                crop.save(target, garbage=4, deflate=True)
            records[label.id] = {"element_id": label.id,
                "parent_id": label.metadata["parent_figure_id"], "source_page": label.page,
                "source_bbox": list(bbox), "source_pdf_sha256": book.source_sha256,
                "clip_path": target.relative_to(outdir).as_posix(), "clip_sha256": file_sha256(target),
                "covered_asset_ids": list(covered), "covered_asset_occurrences": dict(covered),
                "precision": "measured-native-label", "reason": "untranslated_preview"}
            if explicit:
                records[label.id].update(reason="translated_label_overflow_preview",
                    translation_hash=digest(translations[label.id]), translation_displayed=False,
                    overflow_measurement=overflow_measurements[label.id], native_proof=native_proof)
    return records


def _columns(element, page_info):
    value = element.style.get("columns", element.metadata.get("columns", page_info.get(element.page, {}).get("columns", 1)))
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = 1
    if element.style.get("span_columns") or element.metadata.get("span_columns"):
        return 1
    page_width = float(page_info.get(element.page, {}).get("width", 612))
    if count > 1 and element.bbox and element.bbox[2] - element.bbox[0] > page_width * .52:
        return 1
    return min(2, max(1, count))


def _font_size(element, profile):
    if element.metadata.get("layout_runin_heading"):
        return 11.0
    if element.metadata.get("render_as") == "paragraph":
        return profile["body_size"]
    if element.metadata.get("role") == "frontmatter" and element.metadata.get("frontmatter_kind") == "colophon":
        return profile["caption_size"]
    if element.kind == "heading":
        level = int(element.style.get("level", element.metadata.get("heading_level", element.metadata.get("level", 2))))
        return {1: 20.0, 2: 15.0, 3: 12.0}.get(level, 11.0)
    if element.kind in {"caption", "footnote", "figure_label"}:
        return profile["caption_size"]
    if element.kind in {"exercise", "subpart"}:
        return profile["exercise_size"]
    return profile["body_size"]


def _requires_translation(element):
    return needs_translation(element)


def _book_page_labels(book):
    """A verified printed label belongs to the page, including its graphics."""
    printed = {int(p.get("number", p.get("page", i + 1))): str(p["printed_page"])
               for i, p in enumerate(book.pages) if p.get("printed_page")}
    for element in book.elements:
        if element.printed_page:
            printed.setdefault(element.page, str(element.printed_page))
    return {page: f"原书 p.{printed[page]}" if page in printed else f"原 PDF 第{page}页"
            for page in set(book.selected_pages) | {e.page for e in book.elements}}


def _table_column_minima(element, book, profile, inset=4):
    rows = element.metadata.get("table_grid", {}).get("rows", [])
    columns = max((len(row) for row in rows), default=0)
    minima = [profile["body_size"] + 2 * inset] * columns
    lookup = book.element_map()
    for row in rows:
        for column, cell_id in enumerate(row):
            cell = lookup.get(cell_id)
            if not cell:
                continue
            for aid in set(cell.asset_ids) | set(TOKEN_RE.findall(cell.text)):
                if aid in book.assets:
                    minima[column] = max(minima[column], book.assets[aid].width + 2 * inset)
    return minima


def _estimate_height(element, text, book, width, profile):
    size = _font_size(element, profile)
    if element.kind == "table" and isinstance(element.metadata.get("table_grid"), dict):
        grid = element.metadata["table_grid"]
        rows = grid.get("rows", [])
        lookup = book.element_map()
        columns = max((len(row) for row in rows), default=1)
        cell_width = width / columns - 8
        return sum(max((_estimate_height(lookup[cell], lookup[cell].text, book, cell_width, profile)
                        for cell in row if cell in lookup), default=size * 1.55) + 8 for row in rows)
    if element.kind in {"figure", "table", "formula", "equation", "image"} and element.asset_ids:
        return max((book.assets[a].height for a in element.asset_ids), default=size * 2) + 8
    plain = TOKEN_RE.sub("田田", text)
    units = sum(1 if ord(char) > 0x2e80 else .53 for char in plain)
    return max(1, math.ceil(units * size / max(1, width))) * size * 1.55 + 6


def _inline_key(element):
    if element.metadata.get("heading_join") == "inline" and element.metadata.get("heading_group"):
        return ("heading", element.metadata["heading_group"])
    if element.metadata.get("inline_group"):
        return ("text", element.metadata["inline_group"])
    return None


def _explicit_graphic_order(elements):
    """Move only proved caption/graphic components, never unrelated prose.

    Native extraction can list several images before listing their captions.
    The explicit anchor/caption IDs, rather than proximity or a six-item scan,
    establish the indivisible source unit. Stored source records stay intact.
    """
    lookup = {e.id: e for e in elements}
    parent = {e.id: e.id for e in elements}
    def root(eid):
        while parent[eid] != eid:
            parent[eid] = parent[parent[eid]]
            eid = parent[eid]
        return eid
    def link(graphic, caption):
        if (graphic is not None and caption is not None and graphic.page == caption.page
                and graphic.kind in {"figure", "table", "image"} and caption.kind == "caption"
                and caption.metadata.get("render_as") != "paragraph"):
            parent[root(caption.id)] = root(graphic.id)
    for element in elements:
        if element.kind == "caption":
            anchor = element.metadata.get("figure_id", element.metadata.get("anchor_id")) or element.group_id
            link(lookup.get(anchor), element)
        elif element.kind in {"figure", "table", "image"}:
            link(element, lookup.get(element.metadata.get("caption_id")))
    components = defaultdict(list)
    for element in elements:
        components[root(element.id)].append(element)
    emitted, ordered = set(), []
    for element in elements:
        key = root(element.id)
        if key in emitted:
            continue
        emitted.add(key)
        members = components[key]
        if len(members) == 1:
            ordered.append(element)
        else:
            ordered.extend(replace(e, metadata={**e.metadata, "layout_graphic_bundle": key}) for e in members)
    return ordered


def _native_spatial_annotations(elements, book):
    """Retain native geometry for adjacent image pieces and display fragments.

    Only immutable objects with a direct source-rectangle mapping participate.
    Prose, code, translated labels, structured tables and cross-page objects
    end a run. No formula is reconstructed from text or scaled to fit.
    """
    graphic_kinds = {"figure", "image", "table"}
    math_kinds = {"formula", "equation", "footnote", "sidebar"}
    result, index = [], 0
    while index < len(elements):
        first = elements[index]
        graphic = first.kind in graphic_kinds
        if not graphic and first.kind not in math_kinds:
            result.append(first)
            index += 1
            continue
        run, boxes, captions = [], [], []
        cursor = index
        while cursor < len(elements):
            item = elements[cursor]
            if (item.page != first.page or len(item.bbox) != 4
                    or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in item.bbox)):
                break
            if (item.metadata.get("figure_label_ids") or item.metadata.get("table_grid")
                    or item.metadata.get("cross_page_math") or item.metadata.get("role") in
                    {"exercise_marker", "resource_pointer", "end_marker"}):
                break
            if item.kind == "caption" and graphic:
                captions.append(item)
                run.append(item)
                cursor += 1
                continue
            if item.kind not in (graphic_kinds | math_kinds if graphic else math_kinds):
                break
            literal = TOKEN_RE.sub("", item.text).strip()
            if literal and not re.fullmatch(r"[.,;:，。；：!?！？]{1,3}", literal):
                break
            ids = set(item.asset_ids) | set(TOKEN_RE.findall(item.text))
            if not ids and literal and item.kind in math_kinds:
                boxes.append(item.bbox)
                run.append(item)
                cursor += 1
                continue
            if (not ids or any(TOKEN_RE.findall(item.text).count(aid) > 1 or item.asset_ids.count(aid) > 1
                               for aid in ids)):
                break
            assets = [book.assets.get(aid) for aid in ids]
            if any(a is None or a.page != item.page or len(a.bbox) != 4 or
                   not all(isinstance(value, (int, float)) and math.isfinite(value) for value in a.bbox) or
                   abs(a.width - (a.bbox[2] - a.bbox[0])) > .05 or
                   abs(a.height - (a.bbox[3] - a.bbox[1])) > .05 or
                   a.metadata.get("cross_page_math") for a in assets):
                break
            if boxes and item.bbox[1] > max(b[3] for b in boxes) + 36:
                break
            boxes.extend([item.bbox] + [a.bbox for a in assets])
            run.append(item)
            cursor += 1
        native = [e for e in run if e.kind != "caption"]
        graphic_ids = {e.id for e in native if e.kind in graphic_kinds}
        valid = (len(graphic_ids) >= 2 if graphic else len(native) >= 2)
        if captions and any((e.metadata.get("anchor_id", e.metadata.get("figure_id")) or e.group_id)
                            not in graphic_ids for e in captions):
            valid = False
        if valid:
            bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes)]
            key = "native-spatial-" + first.id
            result.extend(replace(e, metadata={**e.metadata, "layout_spatial_unit": key,
                "layout_spatial_bbox": bbox, "layout_spatial_graphic": graphic}) for e in run)
            index = cursor
        else:
            result.append(first)
            index += 1
    return result


def _associated_groups(elements, texts, book, geometry, profile):
    """Group real figures with their caption and directly associated prose."""
    elements = _native_spatial_annotations(_explicit_graphic_order(elements), book)
    groups = []
    index = 0
    available = geometry["height"] - geometry["top"] - geometry["bottom"]
    page_info = {int(p.get("number", p.get("page", i + 1))): p for i, p in enumerate(book.pages)}
    graphic_group_ids = {e.group_id for e in elements if e.group_id and e.kind in {"figure", "table", "image"}}
    # A detached caption and its proved structured table need the same actual
    # frame. Otherwise the caption centers over the full page while a narrow
    # table remains at the page's left margin.
    captioned_tables = {e.id: e for e in elements if e.kind == "table"
                        and e.metadata.get("table_grid") and e.metadata.get("caption_id")}
    connective_group_ids = {e.group_id for e in elements if e.group_id and e.metadata.get("role") == "body_connective"}
    frames = {}
    for e in elements:
        page_width = float(page_info.get(e.page, {}).get("width", geometry["width"]))
        if e.kind == "paragraph" and e.bbox and e.bbox[2] - e.bbox[0] > page_width * .52:
            lo, hi = frames.get(e.page, (e.bbox[0], e.bbox[2]))
            frames[e.page] = (min(lo, e.bbox[0]), max(hi, e.bbox[2]))
    while index < len(elements):
        element = elements[index]
        page_width = float(page_info.get(element.page, {}).get("width", geometry["width"]))
        render_kind = element.metadata.get("render_as", element.kind)
        group = [element]
        exercise_group = element.kind == "exercise" and bool(element.group_id)
        inline_group = bool(_inline_key(element))
        connective_group = element.group_id in connective_group_ids
        if element.metadata.get("layout_spatial_unit"):
            cursor = index + 1
            while (cursor < len(elements) and elements[cursor].metadata.get("layout_spatial_unit") ==
                   element.metadata["layout_spatial_unit"]):
                group.append(elements[cursor])
                cursor += 1
        elif element.metadata.get("layout_graphic_bundle"):
            cursor = index + 1
            while (cursor < len(elements) and elements[cursor].metadata.get("layout_graphic_bundle") ==
                   element.metadata["layout_graphic_bundle"]):
                group.append(elements[cursor])
                cursor += 1
        elif inline_group:
            cursor = index + 1
            while (cursor < len(elements) and (elements[cursor].page == element.page or
                   element.metadata.get('cross_page_math') == elements[cursor].metadata.get('cross_page_math')
                   and bool(element.metadata.get('cross_page_math'))) and
                   _inline_key(elements[cursor]) == _inline_key(element)):
                group.append(elements[cursor])
                cursor += 1
        elif exercise_group:
            cursor = index + 1
            while cursor < len(elements) and elements[cursor].kind == "exercise" and elements[cursor].group_id == element.group_id:
                group.append(elements[cursor])
                cursor += 1
        elif connective_group:
            cursor = index + 1
            while (cursor < len(elements) and elements[cursor].page == element.page and
                   elements[cursor].group_id == element.group_id):
                group.append(elements[cursor])
                cursor += 1
        elif element.kind in {"figure", "table", "image"} or element.group_id in graphic_group_ids:
            cursor = index + 1
            while cursor < len(elements) and len(group) < 6:
                candidate = elements[cursor]
                linked = bool(element.group_id and element.group_id == candidate.group_id)
                anchor = candidate.metadata.get("figure_id", candidate.metadata.get("anchor_id"))
                linked = linked or anchor == element.id
                immediate_caption = (candidate.kind == "caption" and candidate.metadata.get("render_as") != "paragraph"
                                     and candidate.page == element.page and not anchor
                                     and not candidate.group_id)
                near_explanation = (len(group) > 1 and group[-1].kind == "caption" and candidate.kind == "paragraph"
                                    and candidate.page == element.page and candidate.bbox and group[-1].bbox
                                    and 0 <= candidate.bbox[1] - group[-1].bbox[3] <= 28)
                if not (linked or immediate_caption or near_explanation):
                    break
                group.append(candidate)
                cursor += 1
        columns = _columns(element, page_info)
        exercise_heading = element.kind == "heading" and element.text.strip().casefold() in {
            "exercises", "review exercises", "problems", "习题", "练习"}
        if render_kind == "heading" and not exercise_heading:
            following = next((e for e in elements[index + 1:index + 5] if e.kind != "heading"), None)
            if following is not None and following.page == element.page:
                columns = _columns(following, page_info)
            if int(element.style.get("level", element.metadata.get("heading_level", 2))) <= 2:
                columns = 1
        inset = 0.0
        frame = frames.get(element.page)
        proof_grid = (element.kind == "table" and
                      (element.metadata.get("proof_grid") is True or
                       element.metadata.get("table_grid", {}).get("proof_grid") is True))
        frame_role = render_kind in {"paragraph", "heading", "code", "formula", "equation", "footnote"} or proof_grid
        use_body_frame = (frame and element.bbox and frame_role
                          and element.bbox[0] >= frame[0] - 20 and not exercise_heading)
        if (inline_group and frame and element.metadata.get("frontmatter_kind") != "contents"
                and any(e.kind == "paragraph" and e.bbox and e.bbox[2] - e.bbox[0] > page_width * .52 for e in group)):
            use_body_frame = True
        if use_body_frame:
            columns = 1
        if exercise_heading or element.metadata.get("banner_background_asset_ids"):
            columns = 1
        width = (geometry["width"] - geometry["left"] - geometry["right"] - (columns - 1) * geometry["gutter"]) / columns
        # A wide protected expression gets a full-width flow region rather
        # than being squeezed into a source column. Its natural dimensions
        # remain unchanged, including all mathematical superscripts.
        full_width = geometry["width"] - geometry["left"] - geometry["right"]
        if element.metadata.get("role") == "frontmatter" and element.metadata.get("frontmatter_kind") == "contents" and element.bbox:
            columns = 1
            inset = max(0, element.bbox[0] - geometry["left"])
            width = full_width - inset
        if use_body_frame and profile.get("preserve_body_indent", True):
            source_page_width = float(page_info.get(element.page, {}).get("width", geometry["width"]))
            factor = geometry["width"] / source_page_width
            inset = max(0, frame[0] * factor - geometry["left"])
            width = min(frame[1] * factor - max(frame[0] * factor, geometry["left"]), full_width - inset)
        if element.metadata.get("banner_background_asset_ids"):
            columns, width, inset = 1, full_width, 0
        table_frame = captioned_tables.get(element.id) or captioned_tables.get(element.metadata.get("anchor_id"))
        if (table_frame and (element.id == table_frame.id or
                            element.id == table_frame.metadata.get("caption_id"))
                and table_frame.bbox and len(table_frame.bbox) == 4):
            grid = table_frame.metadata["table_grid"]
            rows = grid.get("rows", [])
            raw_widths = grid.get("col_widths", [])
            if (rows and isinstance(raw_widths, list) and len(raw_widths) == len(rows[0])
                    and all(isinstance(w, (int, float)) and not isinstance(w, bool)
                            and math.isfinite(w) and w > 0 for w in raw_widths)):
                table_width = max(sum(raw_widths), sum(_table_column_minima(table_frame, book, profile)))
                source_width = float(page_info.get(table_frame.page, {}).get("width", geometry["width"]))
                table_inset = max(0, table_frame.bbox[0] * geometry["width"] / source_width - geometry["left"])
                if table_width <= full_width - table_inset:
                    columns, width, inset = 1, table_width, table_inset
        group_assets = {asset_id for e in group for asset_id in e.asset_ids + TOKEN_RE.findall(texts[e.id])}
        natural_width = max((book.assets[a].width for a in group_assets if a in book.assets), default=0)
        if element.metadata.get("layout_spatial_unit"):
            spatial_bbox = element.metadata["layout_spatial_bbox"]
            natural_width = max(natural_width, spatial_bbox[2] - spatial_bbox[0])
        for e in group:
            if e.kind == "table" and e.metadata.get("table_grid"):
                natural_width = max(natural_width, sum(_table_column_minima(e, book, profile)))
        if width < natural_width <= full_width:
            columns, width = 1, full_width
            inset = 0
        estimated = sum(_estimate_height(e, texts[e.id], book, width, profile) for e in group)
        if element.metadata.get("layout_spatial_unit"):
            estimated = spatial_bbox[3] - spatial_bbox[1] + sum(
                _estimate_height(e, texts[e.id], book, width, profile) for e in group if e.kind == "caption")
        keep = len(group) > 1 and any(e.kind in {"figure", "table", "image"} for e in group) and estimated <= available * .82
        if exercise_group:
            keep = len(group) > 1 and estimated <= available * .48
        if inline_group:
            keep = len(group) > 1 and estimated <= available * .3
        if connective_group:
            keep = len(group) > 1 and estimated <= available * .48
        if element.metadata.get("layout_spatial_unit"):
            if estimated > available:
                raise CompositionError("原尺寸图形/公式空间单元连同图注超过整页，需要明确续排边界")
            keep = True
        if any(e.kind == "table" and e.metadata.get("table_grid") for e in group):
            # The final translated row heights can exceed a source-based
            # estimate. Never enclose a flowing table in an unbreakable box.
            keep = False
            group = [element]
        if len(group) > 1 and not keep and not exercise_group and not inline_group and not connective_group:
            # Never make a page-sized unbreakable box. Keep figure + caption;
            # its following anchor prose is tracked and checked for distance.
            captions = [element]
            while len(captions) < len(group) and group[len(captions)].kind == "caption":
                captions.append(group[len(captions)])
            caption_height = sum(_estimate_height(e, texts[e.id], book, width, profile) for e in captions)
            if len(captions) > 1 and caption_height <= available * .9:
                group = captions
                keep = True
            else:
                group = [element]
        groups.append((group, keep, columns, width, inset))
        index += len(group)
    return groups



def _exercise_flow_annotations(elements, page_info):
    """Recover narrow questions in a measured two-column exercise region.

    A prematurely ended source column band must not turn its remaining narrow
    questions into page-wide paragraphs. Both geometric column clusters must
    contain multiple real exercise elements before this correction is allowed.
    """
    clusters = defaultdict(lambda: [0, 0])
    choices = {}
    for element in elements:
        page = page_info.get(element.page, {})
        if (element.kind != "exercise" or element.metadata.get("render_as") == "paragraph"
                or element.style.get("span_columns") or element.metadata.get("span_columns")
                or page.get("columns") != 2 or len(element.bbox) != 4):
            continue
        split = page.get("column_split")
        if not isinstance(split, (int, float)):
            continue
        x0, y0, x1, _ = element.bbox
        if x1 - x0 > float(page.get("width", 612)) * .44:
            continue
        if y0 < float(page.get("column_start_y") or 0) - 16:
            continue
        column = 0 if x1 <= split + 8 else 1 if x0 >= split - 8 else None
        if column is not None:
            choices[element.id] = column
            clusters[element.page][column] += 1
    return [replace(e, style={**e.style, "columns": 2, "column": choices[e.id]})
            if e.id in choices and min(clusters[e.page]) >= 2 else e for e in elements]


def _same_line_subpart(label, body):
    return (label.kind == body.kind == "exercise" and label.page == body.page and
            bool(label.group_id) and label.group_id == body.group_id and
            bool(re.fullmatch(r"[a-zA-Z][.)]", label.text.strip())) and
            not re.match(r"^(?:[a-zA-Z][.)]|\d+[.)])\s", body.text.strip()) and
            len(label.bbox) == len(body.bbox) == 4 and
            abs(label.bbox[1] - body.bbox[1]) <= 3 and body.bbox[0] >= label.bbox[2] + 2)


def _body_formula_flow_annotations(elements):
    """Project proved source paragraph continuations into the reading flow.

    Stored kinds, groups and text stay unchanged. Same neural/source-line
    evidence is required before a tiny in-body sidebar becomes run-in prose.
    A nearby standalone formula keeps the last line of its unfinished lead.
    """
    result = list(elements)
    for index, element in enumerate(result):
        if (element.kind != "sidebar" or element.metadata.get("role") or element.asset_ids
                or len(element.bbox) != 4 or not element.metadata.get("structure_id")
                or len(element.text) > 100 or index == 0 or _inline_key(element)):
            continue
        previous = result[index - 1]
        keys = ("structure_id", "source_block")
        if (previous.kind != "paragraph" or previous.page != element.page
                or any(previous.metadata.get(k) != element.metadata.get(k) for k in keys)
                or previous.metadata.get("source_block") is None or len(previous.bbox) != 4
                or not isinstance(element.metadata.get("source_line"), int)
                or not isinstance(previous.metadata.get("source_line"), int)
                or element.metadata["source_line"] <= previous.metadata["source_line"]
                or not -3 <= element.bbox[1] - previous.bbox[3] <= 18
                ):
            continue
        start = index - 1
        while start and result[start - 1].kind == "paragraph" and result[start - 1].page == element.page:
            candidate = result[start - 1]
            if any(candidate.metadata.get(k) != element.metadata.get(k) for k in keys) or _inline_key(candidate):
                break
            start -= 1
        if (any(_inline_key(e) for e in result[start:index + 1]) or
                not min(e.bbox[0] for e in result[start:index]) - 2 <= element.bbox[0] <
                    max(e.bbox[2] for e in result[start:index])):
            continue
        group = "layout-source-continuation-" + result[start].id
        for offset in range(start, index + 1):
            item = result[offset]
            result[offset] = replace(item, metadata={**item.metadata, "inline_group": group,
                "inline_join": "space", "render_as": "paragraph", "layout_body_fragment": True})
    for index, element in enumerate(result[:-1]):
        following = result[index + 1]
        if (element.kind == "paragraph" and following.kind in {"formula", "equation"}
                and element.page == following.page and len(element.bbox) == len(following.bbox) == 4
                and following.asset_ids and not TOKEN_RE.sub("", following.text).strip(" .,:;。；，：")
                and not re.search(r"[.!?;。！？；]\s*$", element.text)
                and 0 <= following.bbox[1] - element.bbox[3] <= 22
                and min(element.bbox[2], following.bbox[2]) > max(element.bbox[0], following.bbox[0])):
            result[index] = replace(element, metadata={**element.metadata, "layout_sticky_next": True})
    return result


def _flow_annotations(elements):
    """Recognize source run-in titles without changing stored source identities."""
    elements = _body_formula_flow_annotations(elements)
    result = []
    for index, element in enumerate(elements):
        following = elements[index + 1] if index + 1 < len(elements) else None
        title = element.text.strip()
        if (element.kind == "paragraph" and 3 < len(title) < 90 and title.isupper()
                and not TOKEN_RE.search(title) and "bold" in str(element.style.get("font", "")).lower()
                and following is not None and following.kind == "paragraph" and following.page == element.page
                and len(element.bbox) == len(following.bbox) == 4
                and abs(element.bbox[0] - following.bbox[0]) <= 3
                and abs(element.bbox[1] - following.bbox[1]) <= 4):
            element = replace(element, metadata={**element.metadata, "render_as": "heading", "layout_runin_heading": True})
        result.append(element)
    return result


def _margin_attachments(elements, geometry):
    """Keep real source margin labels/resources beside their related first line.

    Selection requires an explicit resource pointer or a numbered semantic label,
    a same-page body to its right, and measured vertical adjacency. General notes,
    chapter rails and frontmatter are deliberately left in their own flow.
    """
    attachments, attached = defaultdict(list), set()
    bodies = [e for e in elements if e.kind in {"paragraph", "exercise"} and e.text.strip()
              and len(e.bbox) == 4 and e.metadata.get("role") not in {"frontmatter", "chapter_contents"}]
    def target(note, label=False):
        choices = []
        for body in bodies:
            if (body.page != note.page or body.id == note.id or body.bbox[0] < note.bbox[2] + 3
                    or body.bbox[0] - geometry["left"] < 48):
                continue
            overlap = min(body.bbox[3], note.bbox[3]) - max(body.bbox[1], note.bbox[1])
            distance = abs(body.bbox[1] - note.bbox[1])
            if label and distance > 14:
                continue
            if not label and overlap < -24:
                continue
            choices.append((0 if overlap >= -2 else 1, distance, body.bbox[0] - note.bbox[2], body.id))
        return min(choices)[-1] if choices else None
    resource_words = {"extra", "examples", "links", "link", "assessment", "demo"}
    for pointer in elements:
        if (pointer.metadata.get("role") != "resource_pointer" or len(pointer.bbox) != 4
                or pointer.bbox[2] - pointer.bbox[0] > 32 or pointer.bbox[3] - pointer.bbox[1] > 42):
            continue
        anchor = target(pointer)
        if not anchor:
            continue
        words = [e for e in elements if e.kind in {"sidebar", "footnote"} and e.page == pointer.page
                 and e.id not in attached and e.text.strip().casefold() in resource_words and len(e.bbox) == 4
                 and e.bbox[2] <= pointer.bbox[0] + 3 and pointer.bbox[0] - e.bbox[2] < 65
                 and e.bbox[1] >= pointer.bbox[1] - 5 and e.bbox[3] <= pointer.bbox[3] + 5]
        words.sort(key=lambda e: (e.bbox[1], e.bbox[0]))
        items = words + [pointer]
        attachments[anchor].append(("resource", items))
        attached.update(e.id for e in items)
    numbered = re.compile(r"^(?:example|definition|theorem|lemma|corollary|proposition)\s+\d+\b", re.I)
    for label in elements:
        if (label.kind != "sidebar" or label.id in attached or len(label.bbox) != 4
                or label.metadata.get("role") in {"frontmatter", "chapter_contents"}
                or not numbered.match(label.text.strip())):
            continue
        anchor = target(label, label=True)
        if anchor:
            attachments[anchor].insert(0, ("label", [label]))
            attached.add(label.id)
    return dict(attachments), attached


def _margin_markup(notes, element, texts, book, paths, geometry, width, profile, issues, labels, preview_ids, flow_inset=0, rail_shift=0):
    # Source x is not the rendered x: a source list can reflow at full width.
    # Reserve a physical 6pt safety margin even when the body has no inset.
    margin_width = min(88.0, geometry["left"] + max(0.0, flow_inset) - 14.0)
    if margin_width < 24:
        raise CompositionError(f"元素 {element.id} 的侧边没有足够空间放置原尺寸资源标识")
    rows = []
    for role, items in notes:
        identities = ["(id: " + _q(e.id) + ", source-page: " + str(e.page) + ", source-label: " + _q(labels[e.page]) + ")" for e in items]
        text_items = [e for e in items if e.kind in {"sidebar", "footnote"}]
        pointers = [e for e in items if e.kind not in {"sidebar", "footnote"}]
        strings = []
        for note in text_items:
            body = _text_markup(texts[note.id], note, book, paths, margin_width, issues)
            if note.id in preview_ids:
                body = '#text(size: 7pt, fill: rgb("b83a24"), "【未译】")' + body
            strings.append(body)
        content = "#h(.1em)".join(strings)
        if pointers:
            pointer_markup = "".join(_asset_markup(a, e.id, book, paths, margin_width, issues, inline=False)
                                     for e in pointers for a in e.asset_ids)
            content = "#grid(columns: (1fr, auto), column-gutter: 3pt, align: right + top, [" + content + "], [" + pointer_markup + "])"
        size = profile["body_size"] if role == "label" else profile["caption_size"]
        rows.append("[#margin-source((" + ", ".join(identities) + ",), " + _number(margin_width) + "pt, " +
                    _number(size) + "pt)[" + content + "]]")
    return "#place(top + left, dx: -" + _number(margin_width + 8 + rail_shift) + "pt)[#stack(dir: ttb, spacing: 4pt, " + ", ".join(rows) + ")]"


def _asset_markup(asset_id, element_id, book, paths, width, issues, inline=True):
    asset = book.assets.get(asset_id)
    if asset is None:
        raise CompositionError(f"未知受保护对象：{asset_id}")
    aw, ah = _positive(asset.width, 1), _positive(asset.height, 1)
    baseline = asset.metadata.get("baseline_from_top", asset.baseline)
    try:
        baseline = float(baseline)
    except (TypeError, ValueError):
        baseline = ah
    if not math.isfinite(baseline) or baseline <= 0:
        baseline = ah
    baseline = min(ah, baseline)
    if aw > width + .5:
        issues.append({"code": "asset_too_wide", "severity": "error", "element_id": element_id,
                       "asset_id": asset_id, "page": asset.page,
                       "detail": f"原始对象宽 {aw:.1f}pt，所在栏宽 {width:.1f}pt；保留原始尺寸并要求修复版面"})
    image_page = int(asset.metadata.get("pdf_page", 0)) + 1
    return "#protected(" + ", ".join((_q(asset_id), _q(element_id), _q(paths[asset_id]),
           _number(aw) + "pt", _number(ah) + "pt", _number(ah - baseline) + "pt",
           "pdf-page: " + str(image_page))) + ")"


def _display_text_markup(text, element, book, paths, width, issues):
    """Explicit layout-only displays; source text/token/asset identity is unchanged."""
    selected = element.metadata['display_asset_ids']
    if (not isinstance(selected, list) or not selected or
            any(not isinstance(aid, str) for aid in selected) or len(set(selected)) != len(selected)):
        raise CompositionError('display_asset_ids must be a nonempty list of distinct protected asset ids')
    counts = Counter(TOKEN_RE.findall(text))
    for aid in selected:
        asset = book.assets.get(aid)
        if (aid not in element.asset_ids or counts[aid] != 1 or asset is None or
                asset.kind not in {'formula', 'equation'} or asset.metadata.get('cross_page_math')):
            raise CompositionError('A display asset must occur exactly once as one complete protected formula: ' + aid)
    inline_element = replace(element, metadata={k:v for k,v in element.metadata.items() if k != 'display_asset_ids'})
    matches = [m for m in TOKEN_RE.finditer(text) if m[1] in selected]
    if element.kind == 'exercise' and not text[:matches[0].start()].strip():
        raise CompositionError('An opt-in display exercise needs leading prose for its first-line number baseline')
    parts = []; position = 0
    for index, match in enumerate(matches):
        prefix = text[position:match.start()]
        if prefix.strip():
            probe = ('#baseline-probe("exercise-baseline", ' + _q(element.id) + ')'
                     if index == 0 and element.kind == 'exercise' and
                     re.match(r'^(\s*(?:\d+[.)]|\([a-zA-Z0-9]+\)|[a-zA-Z][.)]))\s*', element.text) else '')
            parts.append('#block(above: 0pt, below: 2pt, sticky: true)[' +
                         probe + _text_markup(prefix, inline_element, book, paths, width, issues) + ']')
        tail = text[match.end():] if index == len(matches)-1 else ''
        terminal = tail.strip() if re.fullmatch(r'\s*[。；，、：！？,.!?;:]\s*', tail) else ''
        body = _asset_markup(match[1], element.id, book, paths, width, issues, inline=False)
        if terminal:
            body += '#text(' + _q(terminal) + ')'
        following = text[match.end():matches[index+1].start()].strip() if index+1 < len(matches) else ''
        annotation = bool(len(following) <= 64 and re.fullmatch(r'[（(].+[)）]', following, flags=re.DOTALL))
        parts.append('#block(above: 2pt, below: 3pt, breakable: false, sticky: ' +
                     str(annotation).lower() + ')[' + body + ']')
        position = len(text) if terminal else match.end()
    if position < len(text):
        parts.append('#block(above: 0pt, below: 0pt)[' +
                     _text_markup(text[position:], inline_element, book, paths, width, issues) + ']')
    return '#block(above: 0pt, below: 0pt)[#set par(justify: false, hanging-indent: 0pt)\n' + ''.join(parts) + ']'


def _literal_text_markup(value):
    # A Chinese Landau name is one reading unit. Keep the original characters,
    # font size and spaces while preventing a break inside the short name.
    matches = list(re.finditer(r'(?:大[ \u00a0\u3000]{0,2}[OΩΘ]|小[ \u00a0\u3000]{0,2}[oω])(?=$|[^A-Za-z])', value))
    if not matches:
        return '#text(' + _q(value) + ')'
    parts = []
    position = 0
    for match in matches:
        if match.start() > position:
            parts.append('#text(' + _q(value[position:match.start()]) + ')')
        parts.append('#box[#text(' + _q(match.group()) + ')]')
        position = match.end()
    if position < len(value):
        parts.append('#text(' + _q(value[position:]) + ')')
    return ''.join(parts)


def _text_markup(text, element, book, paths, width, issues):
    if element.metadata.get('display_asset_ids'):
        return _display_text_markup(text, element, book, paths, width, issues)
    parts = []
    position = 0
    matches = list(TOKEN_RE.finditer(text))
    skip = set()
    for index, match in enumerate(matches):
        if index in skip:
            continue
        if match.start() > position:
            parts.append(_literal_text_markup(text[position:match.start()]))
        proof = book.assets[match[1]].metadata.get('cross_page_math') if match[1] in book.assets else None
        if proof and index+1 < len(matches):
            following = matches[index+1]
            segments = proof['segments']
            if (match[1] == segments[0]['asset_id'] and following[1] == segments[1]['asset_id']
                    and not text[match.end():following.start()].strip()):
                if sum(book.assets[s['asset_id']].width for s in segments) > width+.1:
                    raise CompositionError('跨原页公式原尺寸超过当前栏宽，需要修正版面：'+proof['id'])
                parts.append('#box['+''.join(_asset_markup(s['asset_id'], s['element_id'], book, paths, width, issues)
                                             for s in segments)+']')
                skip.add(index+1)
                position = following.end()
                continue
        # A near-full-line native object cannot share a line with its leading
        # connective. Give that complete object a fresh line; its own contents
        # and native dimensions remain indivisible.
        asset = book.assets.get(match[1])
        if asset is not None and asset.width > width * .85 and match.start() > 0:
            parts.append('#linebreak(justify: false)')
        parts.append(_asset_markup(match[1], element.id, book, paths, width, issues))
        position = match.end()
    if position < len(text):
        parts.append(_literal_text_markup(text[position:]))
    return "".join(parts)


def _table_markup(element, texts, book, paths, geometry, width, profile, issues, labels, preview_ids):
    grid = element.metadata.get("table_grid")
    rows = grid.get("rows") if isinstance(grid, dict) else None
    if not isinstance(rows, list) or not rows or not all(isinstance(row, list) for row in rows):
        raise CompositionError(f"表格 {element.id} 的单元格结构无效")
    count = len(rows[0])
    if not count or any(len(row) != count for row in rows):
        raise CompositionError(f"表格 {element.id} 有未处理的跨行或跨列单元格，拒绝猜测")
    lookup = book.element_map()
    ids = [cell for row in rows for cell in row]
    if len(ids) != len(set(ids)) or any(not isinstance(cell, str) or cell not in lookup for cell in ids):
        raise CompositionError(f"表格 {element.id} 包含重复或未知单元格")
    try:
        headers = int(grid.get("header_rows", 1))
    except (TypeError, ValueError):
        headers = 1
    headers = max(0, min(len(rows), headers))
    raw_widths = grid.get("col_widths", [])
    if not isinstance(raw_widths, list) or len(raw_widths) != count:
        raw_widths = [width / count] * count
    widths = [_positive(w, width / count) for w in raw_widths]
    inset = _positive(grid.get("cell_inset"), 4)
    minima = _table_column_minima(element, book, profile, inset)
    if sum(minima) > width + .1:
        raise CompositionError(f"表格 {element.id} 的原尺寸公式与固定字号所需最小列宽合计 {sum(minima):.1f}pt，超过可用宽度 {width:.1f}pt")
    widths = [max(w, minima[i]) for i, w in enumerate(widths)]
    total = sum(widths)
    if total > width:
        extra = width - sum(minima)
        weights = [max(0, w - minima[i]) for i, w in enumerate(widths)]
        weight_sum = sum(weights)
        widths = [minima[i] + (extra * weights[i] / weight_sum if weight_sum else extra / count)
                  for i in range(count)]
    if any(w <= inset * 2 + 5 for w in widths):
        raise CompositionError(f"表格 {element.id} 的列宽不足以容纳固定字号文字")
    proof_borderless = (grid.get("proof_grid") is True or element.metadata.get("proof_grid") is True) and grid.get("stroke") in (0, 0.0, "0")
    borderless = grid.get("borderless") is True or proof_borderless
    header_fill_value = grid.get("header_fill", grid.get("heading_fill", "edf6fa"))
    no_header_fill = header_fill_value is None or proof_borderless
    stroke_color = str(grid.get("stroke_color", grid.get("stroke", grid.get("color", "8aa9b8")))).lstrip("#")
    header_fill = str(header_fill_value).lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", stroke_color):
        stroke_color = "8aa9b8"
    if not re.fullmatch(r"[0-9a-fA-F]{6}", header_fill):
        header_fill = "edf6fa"
    stroke_width = _positive(grid.get("stroke_width"), .45)
    cells = []
    available = geometry["height"] - geometry["top"] - geometry["bottom"]
    for row_index, row in enumerate(rows):
        height = max(_estimate_height(lookup[cell], texts[cell], book, widths[col] - 2 * inset, profile)
                     for col, cell in enumerate(row)) + 2 * inset
        split = height > available * .85
        if split:
            issues.append({"code": "table_row_too_tall", "severity": "error", "element_id": element.id,
                           "page": element.page, "row": row_index,
                           "detail": "单行表格接近或超过整页高度；允许行内续页以保持内容，但需要人工复核"})
        for column, cell_id in enumerate(row):
            cell = lookup[cell_id]
            markup = _element_markup(cell, texts[cell_id], book, paths, geometry,
                                     widths[column] - 2 * inset, profile, issues,
                                     cell_id in preview_ids, labels, 1, texts, preview_ids)
            weight = '#set text(weight: "bold")\n' if row_index < headers else ""
            cells.append("table.cell(breakable: " + str(split).lower() + ")[" + weight + markup + "]")
    children = []
    header_count = headers * count
    if header_count:
        children.append("table.header(repeat: true, " + ", ".join(cells[:header_count]) + ")")
    children.extend(cells[header_count:])
    stroke = "none" if borderless else _number(stroke_width) + "pt + rgb(" + _q(stroke_color) + ")"
    fill = "none" if no_header_fill else "(x, y) => if y < " + str(headers) + " { rgb(" + _q(header_fill) + ") } else { none }"
    params = ["columns: (" + ", ".join(_number(w) + "pt" for w in widths) + ",)",
              "inset: " + _number(inset) + "pt", "stroke: " + stroke, "fill: " + fill]
    return "#table(" + ", ".join(params + children) + ")"


def _banner_markup(element, text, book, paths, geometry, profile, issues, preview, labels):
    """Rebuild a chapter's clean full-bleed graphic and translated title."""
    backgrounds = list(dict.fromkeys(element.metadata.get("banner_background_asset_ids", [])))
    if not backgrounds or any(a not in book.assets for a in backgrounds):
        raise CompositionError(f"章节 {element.id} 的横幅背景不存在")
    width = geometry["width"] - geometry["left"] - geometry["right"]
    related = [e for e in book.elements if e.metadata.get("rendered_by_heading") == element.id]
    label = labels.get(element.page, source_label(element)) if labels else source_label(element)
    placements, source_bottom = [], 0.0
    for aid in backgrounds:
        asset = book.assets[aid]
        owner = next((e for e in related if aid in e.asset_ids), None)
        owner_id = owner.id if owner else element.id
        bbox = asset.bbox if len(asset.bbox) == 4 else [0, 0, asset.width, asset.height]
        dx, dy = float(bbox[0]) - geometry["left"], float(bbox[1]) - geometry["top"]
        image = _asset_markup(aid, owner_id, book, paths, asset.width, issues, inline=False)
        if owner:
            image = "#banner-source(" + ", ".join((_q(owner_id), str(owner.page), _q(label),
                     _number(asset.width) + "pt", _number(asset.height) + "pt")) + ")[" + image + "]"
        placements.append("#place(top + left, dx: " + _number(dx) + "pt, dy: " + _number(dy) + "pt)[" + image + "]")
        source_bottom = max(source_bottom, float(bbox[1]) + asset.height)
    rect = element.metadata.get("banner_text_bbox")
    if not isinstance(rect, (list, tuple)) or len(rect) != 4 or rect[2] <= rect[0] or rect[3] <= rect[1]:
        rect = [geometry["left"], source_bottom + 12, geometry["width"] - geometry["right"], source_bottom + 55]
    rect = list(map(float, rect))
    if rect[0] < 0 or rect[2] > geometry["width"] or rect[1] < 0:
        raise CompositionError(f"章节 {element.id} 的横幅标题区域超出页面")
    title = text
    number = element.metadata.get("chapter_number")
    if number is not None:
        prefix = r"^\s*(?:第\s*" + re.escape(str(number)) + r"\s*章|chapter\s*" + re.escape(str(number)) + r")\s*[:：.、\-]?\s*"
        title = re.sub(prefix, "", title, count=1, flags=re.I)
    color = str(element.metadata.get("banner_text_color", "231f20")).lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        color = "231f20"
    size = _font_size(element, profile)
    title_body = _text_markup(title, element, book, paths, rect[2] - rect[0], issues)
    if preview:
        title_body = '#text(size: 8pt, fill: rgb("b83a24"), "【未翻译原文】")' + title_body
    title_body = "#set par(justify: false)\n#text(size: " + _number(size) + "pt, fill: rgb(" + _q(color) + "))[" + title_body + "]"
    placements.append("#place(top + left, dx: " + _number(rect[0] - geometry["left"]) + "pt, dy: " +
                      _number(rect[1] - geometry["top"]) + "pt)[#block(width: " + _number(rect[2] - rect[0]) + "pt)[" + title_body + "]]")
    for key, content, font_size, fill in (
        ("banner_number_bbox", str(number) if number is not None else "", 48, "ffffff"),
        ("banner_label_bbox", "第 " + str(number) + " 章" if number is not None else "", 9, color),
    ):
        area = element.metadata.get(key)
        if content and isinstance(area, (list, tuple)) and len(area) == 4 and area[2] > area[0]:
            placements.append("#place(top + left, dx: " + _number(area[0] - geometry["left"]) + "pt, dy: " +
                              _number(area[1] - geometry["top"]) + "pt)[#block(width: " + _number(area[2] - area[0]) +
                              "pt)[#align(center)[#text(size: " + _number(font_size) + "pt, fill: rgb(" + _q(fill) + "), " + _q(content) + ")]]]")
    placements.append('#if settings.preview { place(top + right, dy: ' + _number(8 - geometry["top"]) +
                      'pt)[#block(width: ' + _number(width) + 'pt)[#align(right)[#page-preview-status(settings)]]] }')
    title_height = _estimate_height(element, title, book, rect[2] - rect[0], profile)
    height = max(source_bottom, rect[1] + title_height) - geometry["top"] + 10
    body = "#block(width: " + _number(width) + "pt, height: " + _number(height) + "pt)[" + "".join(placements) + "]"
    args = [_q(element.id), _q(element.kind), str(element.page), _q(label), _number(width) + "pt", _number(size) + "pt",
            "heading: true", "banner: true", "heading-font: " + _q(profile.get("heading_font", "SimHei"))]
    return "#tracked(" + ", ".join(args) + ")[" + body + "]\n"


def _blank_label_width(child, background, children, paths, x, y, width, height, profile):
    """Bound rightward growth by actual background ink and other label frames.

    Only white/transparent native background pixels are eligible. Colored fills,
    neighboring labels, unverified dimensions and missing pixels stay strict.
    This offers extra space; Typst uses it only if the original box overflows.
    """
    root = paths.get("__output_root__")
    if root is None or not paths[background.id].lower().endswith('.pdf'):
        return width, None
    import numpy as np
    import pymupdf
    cache = paths.setdefault("__label_background_pixels__", {})
    key = (background.id, background.metadata.get("pdf_page", 0))
    if key not in cache:
        with pymupdf.open(Path(root) / paths[background.id]) as pdf:
            page = pdf[int(background.metadata.get("pdf_page", 0))]
            if abs(page.rect.width - background.width) > .05 or abs(page.rect.height - background.height) > .05:
                return width, None
            if background.width * background.height * 64 > 32_000_000:
                return width, None
            pix = page.get_pixmap(matrix=pymupdf.Matrix(8, 8), alpha=True)
            cache[key] = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 4).copy()
    pixels = cache[key]
    limit = min(background.width, x + width + 64)
    for other in children:
        if other.id == child.id:
            continue
        ox = other.bbox[0] - background.bbox[0]
        oy = max(0, other.bbox[1] - background.bbox[1] - 1)
        oh = max(other.bbox[3] - other.bbox[1] + 2, profile['caption_size'] * 1.4)
        if oy < y + height and oy + oh > y:
            if ox >= x + width - .1:
                limit = min(limit, ox - .5)
            elif other.bbox[2] - background.bbox[0] > x + width:
                limit = min(limit, x + width)
    start = max(0, math.floor((x + width) * 8))
    stop = min(pixels.shape[1], math.ceil(limit * 8))
    top, bottom = max(0, math.floor(y * 8)), min(pixels.shape[0], math.ceil((y + height) * 8))
    if stop <= start or bottom <= top:
        return width, None
    strip = pixels[top:bottom, start:stop]
    ink = (strip[:, :, 3] > 2) & np.any(strip[:, :, :3] < 250, axis=2)
    occupied = np.flatnonzero(np.any(ink, axis=0))
    if occupied.size:
        limit = min(limit, (start + int(occupied[0])) / 8 - .25)
    safe = max(width, limit - x)
    if safe <= width + .25:
        return width, None
    safe_strip = pixels[top:bottom, start:min(stop, math.floor((x + safe) * 8))]
    return safe, {"element_id": child.id, "parent_background_id": background.id,
        "background_sha256": background.sha256, "pdf_page": int(background.metadata.get("pdf_page", 0)),
        "original_width_pt": width, "safe_width_pt": safe, "height_pt": height,
        "background_local_bbox": [x, y, x + safe, y + height],
        "method": "rightward-native-background-blank-strip-v1", "raster_dpi": 576,
        "pymupdf_version": pymupdf.VersionBind, "extra_pixels_checked": int(safe_strip.shape[0] * safe_strip.shape[1]),
        "extra_pixel_window_sha256": digest(safe_strip.tobytes()), "foreground_pixels_in_extra": 0,
        "blank_rule": "alpha<=2 or all RGB>=250; occupied column stops growth with 0.25pt guard",
        "other_label_source_rectangles_excluded": True, "font_size_pt": profile['caption_size']}


def _figure_labels_markup(element, texts, book, paths, width, profile, issues, labels, preview_ids):
    children = [book.element_map().get(eid) for eid in element.metadata['figure_label_ids']]
    if any(child is None for child in children) or len(element.asset_ids) != 1:
        raise CompositionError(f'图内标签 {element.id} 的来源或背景不完整')
    background = book.assets[element.asset_ids[0]]
    if background.width > width + .5:
        raise CompositionError(f'图 {element.id} 及其标签需要完整栏宽，原尺寸不能压缩')
    aw, ah = background.width, background.height
    content = ['#place(top + left)[' + _asset_markup(background.id, element.id, book, paths, aw, issues) + ']']
    for child in children:
        retained = paths.get("__native_retained_labels__", {}).get(child.id)
        if retained is not None:
            source_bbox = retained["source_bbox"]
            x, y = source_bbox[0] - background.bbox[0], source_bbox[1] - background.bbox[1]
            w, h = source_bbox[2] - source_bbox[0], source_bbox[3] - source_bbox[1]
            if x < -.5 or y < -.5 or x + w > aw + .5 or y + h > ah + .5:
                raise CompositionError(f"未译标签 {child.id} 的原尺寸裁片超出图形区域")
            label = labels.get(child.page, source_label(child)) if labels else source_label(child)
            markup = '#retained-native-label(' + ', '.join((_q(child.id), str(child.page), _q(label),
                _q(retained["clip_path"]), _number(w) + 'pt', _number(h) + 'pt',
                'json(bytes(' + _q(_q(retained)) + '))')) + ')'
            content.append('#place(top + left, dx: ' + _number(x) + 'pt, dy: ' + _number(y) + 'pt)[' + markup + ']')
            overflow = retained.get("reason") == "translated_label_overflow_preview"
            issues.append({"code": "translated_figure_label_retained_native" if overflow else "untranslated_figure_label_retained",
                           "severity": "warning", "element_id": child.id, "page": child.page,
                           "translation_hash": retained.get("translation_hash"),
                           "detail": ("固定字号译文实测超出安全标签框；本阅读稿仅此标签保留原尺寸原生文字，已存译文未显示。"
                                      if overflow else "预览保留原尺寸原生图内标签；仍属未译内容，不计中文完成。")})
            continue
        value = texts.get(child.id, '')
        if not value and child.id not in preview_ids:
            raise CompositionError(f'图内标签 {child.id} 尚未翻译')
        w = child.bbox[2] - child.bbox[0]
        h = max(child.bbox[3] - child.bbox[1] + 2, profile['caption_size'] * 1.4)
        x = child.bbox[0] - background.bbox[0]
        y = max(0, child.bbox[1] - background.bbox[1] - 1)
        if x < -.5 or x + w > aw + .5 or y + h > ah + .5:
            raise CompositionError(f'图内标签 {child.id} 的固定字号框超出原图，需要修正版面')
        body = _text_markup(value or child.text, child, book, paths, w, issues)
        label = labels.get(child.page, source_label(child)) if labels else source_label(child)
        color = str(child.style.get('color', '202730')).lstrip('#')
        if not re.fullmatch(r'[0-9a-fA-F]{6}', color):
            raise CompositionError(f'图内标签 {child.id} 的来源颜色无效')
        safe_width, proof = _blank_label_width(child, background, children, paths, x, y, w, h, profile)
        width_choice = ('let original = measure(block(width: ' + _number(w) + 'pt, above: 0pt, below: 0pt)[#label-body]); '
                        'let label-width = if original.height > ' + _number(h) + 'pt { ' +
                        _number(safe_width) + 'pt } else { ' + _number(w) + 'pt }; ')
        proof_markup = ('if label-width > ' + _number(w) + 'pt { label-blank-expansion(' + _q(child.id) +
                        ', json(bytes(' + _q(_q(proof)) + '))) }; ') if proof else ''
        measurement = '(source_page: ' + str(child.page) + ', original_width: ' + _number(w) + ', safe_width: ' + _number(safe_width) + \
                      ', chosen_width: label-width / 1pt, measured_height: measured.height / 1pt, max_height: ' + _number(h) + \
                      ', font_size: ' + _number(profile['caption_size']) + ', translation_hash: ' + _q(digest(value)) + ')'
        if paths.get("__allow_label_overflow_preview__"):
            failed = ('figure-label-decision(' + _q(child.id) + ', "probe", ' + measurement + '); '
                      'box(width: label-width, height: ' + _number(h) + 'pt)[]')
        else:
            failed = 'panic(' + _q('图内标签溢出：' + child.id) + ')'
        fitted = (proof_markup + 'figure-label-decision(' + _q(child.id) + ', "translated", ' + measurement + '); '
                  'tracked(' + ', '.join((_q(child.id), _q('figure_label'), str(child.page), _q(label),
                  'label-width', _number(profile['caption_size']) + 'pt', 'keep: true')) + ')[#label-body]')
        markup = ('#context { let label-body = [#set par(justify: false, leading: .1em)\n'
                  '#set text(size: ' + _number(profile['caption_size']) + 'pt, fill: rgb(' + _q(color) + '))\n' + body + ']; '
                  + width_choice + 'let measured = measure(block(width: label-width, above: 0pt, below: 0pt)[#label-body]); '
                  'if measured.height > ' + _number(h) + 'pt { ' + failed + ' } else { ' + fitted + ' } }')
        content.append('#place(top + left, dx: ' + _number(x) + 'pt, dy: ' + _number(y) + 'pt)[' + markup + ']')
    # Retain at most one diagram's high-resolution evidence raster at a time.
    paths.get("__label_background_pixels__", {}).pop((background.id, background.metadata.get("pdf_page", 0)), None)
    return '#align(center)[#box(width: ' + _number(aw) + 'pt, height: ' + _number(ah) + 'pt)[' + ''.join(content) + ']]'


def _native_spatial_punctuation(element, literal, book, paths):
    """Copy only the proved, unprotected source punctuation at its own origin."""
    import pymupdf
    from .extraction import _collect
    from .native_clipping import clip_native_content, verify_selected_native_glyphs
    key = (element.id, literal)
    cache = paths.setdefault("__native_spatial_punctuation__", {})
    if key in cache:
        return cache[key]
    source = Path(book.source_path).resolve()
    if not paths.get("__native_spatial_source_verified__"):
        if not source.is_file() or file_sha256(source) != book.source_sha256:
            raise CompositionError("原生公式标点需要同一份可验证的原书 PDF")
        paths["__native_spatial_source_verified__"] = True
    covered_ids = {gid for aid in set(element.asset_ids) | set(TOKEN_RE.findall(element.text))
                   for gid in book.assets[aid].metadata.get("glyph_ids", [])}
    owned_ids = set(element.metadata.get("glyph_ids", [])) - covered_ids
    with pymupdf.open(source) as original:
        page = original[element.page - 1]
        _, glyphs = _collect(page)
        selected = [g for g in glyphs if g["id"] in owned_ids and g["c"] in literal]
        if ''.join(g["c"] for g in selected) != literal:
            raise CompositionError(f"公式 {element.id} 的裸标点缺少唯一的原书字形位置证据")
        bbox = pymupdf.Rect(selected[0]["bbox"])
        for glyph in selected[1:]:
            bbox |= pymupdf.Rect(glyph["bbox"])
        bbox = (bbox + (-.15, -.15, .15, .15)) & page.rect
        root = Path(paths["__output_root__"])
        directory = root / "native-punctuation"
        directory.mkdir(exist_ok=True)
        identity = digest({"source": book.source_sha256, "page": element.page,
                           "ids": [g['id'] for g in selected], "bbox": list(bbox)})
        target = directory / (identity[:24] + '.pdf')
        with pymupdf.open() as isolated, pymupdf.open() as crop:
            isolated.insert_pdf(original, from_page=element.page - 1, to_page=element.page - 1)
            selection = clip_native_content(isolated[0], bbox, selected_glyphs=selected, source_glyphs=glyphs)
            native = verify_selected_native_glyphs(page, isolated[0], selected)
            if not selection.get('native_text_selection', {}).get('verified') or native.get('missing_ids'):
                raise CompositionError(f"公式 {element.id} 的原生标点不能完整保留")
            canvas = crop.new_page(width=bbox.width, height=bbox.height)
            canvas.show_pdf_page(canvas.rect, isolated, 0, clip=bbox)
            crop.save(target, garbage=4, deflate=True)
    record = {"element_id": element.id, "source_page": element.page, "source_bbox": list(bbox),
        "source_pdf_sha256": book.source_sha256, "glyph_ids": [g['id'] for g in selected],
        "clip_path": target.relative_to(root).as_posix(), "clip_sha256": file_sha256(target),
        "native_selection": selection, "native_identity": native,
        "precision": "measured-native-punctuation"}
    cache[key] = record
    return record


def _spatial_native_markup(group, texts, book, paths, geometry, width, profile, issues, labels, preview_ids, columns):
    """One measured canvas preserves the input's native relative coordinates."""
    bbox = group[0].metadata["layout_spatial_bbox"]
    canvas_width, canvas_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if (canvas_width > width + .1 or
            canvas_height > geometry["height"] - geometry["top"] - geometry["bottom"]):
        raise CompositionError("原尺寸图形/公式空间单元超过当前正文区域，需要源关系或纸型修复")
    content = []
    for element in group:
        if element.kind == "caption":
            continue
        ex, ey = element.bbox[0] - bbox[0], element.bbox[1] - bbox[1]
        ew, eh = element.bbox[2] - element.bbox[0], element.bbox[3] - element.bbox[1]
        label = labels.get(element.page, source_label(element))
        identity = ', '.join((_q(element.id), str(element.page), _q(label), _number(ew) + 'pt', _number(eh) + 'pt'))
        content.append('#place(top + left, dx: ' + _number(ex) + 'pt, dy: ' + _number(ey) +
                       'pt)[#native-source-frame(' + identity + ')]')
        ids = list(dict.fromkeys(TOKEN_RE.findall(element.text) + element.asset_ids))
        for aid in ids:
            asset = book.assets[aid]
            image = _asset_markup(aid, element.id, book, paths, asset.width, issues, inline=False)
            content.append('#place(top + left, dx: ' + _number(asset.bbox[0] - bbox[0]) +
                           'pt, dy: ' + _number(asset.bbox[1] - bbox[1]) + 'pt)[' + image + ']')
        literal = TOKEN_RE.sub('', texts[element.id]).strip()
        if literal:
            retained = _native_spatial_punctuation(element, literal, book, paths)
            rect = retained['source_bbox']
            content.append('#place(top + left, dx: ' + _number(rect[0] - bbox[0]) +
                'pt, dy: ' + _number(rect[1] - bbox[1]) + 'pt)[#native-spatial-punctuation(' +
                _q(retained['clip_path']) + ', ' + _number(rect[2]-rect[0]) + 'pt, ' +
                _number(rect[3]-rect[1]) + 'pt, json(bytes(' + _q(_q(retained)) + ')))]')
    lines = ['#block(above: 0pt, below: 5pt, breakable: false)[#box(width: ' +
             _number(canvas_width) + 'pt, height: ' + _number(canvas_height) + 'pt)[' + ''.join(content) + ']]']
    captions = sorted((e for e in group if e.kind == 'caption'), key=lambda e: (e.bbox[1], e.bbox[0]))
    if len(captions) > 1 and max(e.bbox[1] for e in captions) - min(e.bbox[1] for e in captions) <= 3:
        lines.append('#align(center)[' + _inline_group_markup(captions, texts, book, paths, geometry,
            width, profile, issues, labels, preview_ids, columns) + ']')
    else:
        lines.extend(_element_markup(e, texts[e.id], book, paths, geometry, width, profile, issues,
            e.id in preview_ids, labels, columns, texts, preview_ids) for e in captions)
    return '\n'.join(lines)


def _element_markup(element, text, book, paths, geometry, width, profile, issues, preview=False, labels=None, columns=1,
                    texts=None, preview_ids=None, sticky_next=False, markers=(), margin_notes=(), flow_inset=0,
                    panel_skip=False, rail_shift=0):
    if not panel_skip and book.profile.get("source_design_panels_v1"):
        from .design_panels import body_panel, markup as panel_markup
        panel = body_panel(book, element.id)
        if panel:
            return panel_markup(panel, element, text, book, paths, geometry, width, profile, issues,
                preview, labels, columns, texts, preview_ids, sticky_next, markers, margin_notes, flow_inset)
    if element.metadata.get("banner_background_asset_ids"):
        return _banner_markup(element, text, book, paths, geometry, profile, issues, preview, labels)
    if element.kind == "paragraph" and not element.metadata.get("display_asset_ids"):
        # Wrapped native objects already contain their source line layout.
        # Separate their prose flow, rather than forcing the short lead line
        # to justify across the canvas's mostly empty first row.
        display = [aid for aid in TOKEN_RE.findall(text) if aid in book.assets
                   and book.assets[aid].metadata.get("detector") == "whole-native-formula-chain"
                   and len(book.assets[aid].metadata.get("source_segments", [])) > 1
                   and book.assets[aid].width > width * .7]
        if display:
            element = replace(element, metadata={**element.metadata, "display_asset_ids": display})
    has_labels = bool(element.metadata.get('figure_label_ids'))
    body = (_figure_labels_markup(element, texts or {}, book, paths, width, profile, issues,
                                 labels, preview_ids or set()) if has_labels else
            _text_markup(text, element, book, paths, width, issues))
    in_text = Counter(TOKEN_RE.findall(text))
    additional = [a for a in element.asset_ids if not in_text[a]] if not has_labels else []
    if additional:
        assets = "#v(3pt)".join(_asset_markup(a, element.id, book, paths, width, issues, inline=False) for a in additional)
        body = "#align(center)[" + assets + "]" + body if element.kind in {"figure", "formula", "equation", "table", "image"} else body + assets
    size = _font_size(element, profile)
    render_kind = element.metadata.get("render_as", element.kind)
    is_heading = render_kind == "heading"
    label = labels.get(element.page, source_label(element)) if labels else source_label(element)
    args = [_q(element.id), _q(render_kind), str(element.page), _q(label),
            _number(width) + "pt", _number(size) + "pt"]
    color = str(profile.get("heading_color", "00a9df")).lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        color = "00a9df"
    options = ["heading: " + str(is_heading).lower(), "preview: " + str(preview).lower(),
               "heading-font: " + _q(profile.get("heading_font", "SimHei")), "heading-color: " + _q(color),
               "column-count: " + str(columns), "sticky-next: " + str(sticky_next or element.metadata.get("layout_sticky_next", False)).lower()]
    if is_heading and element.text.strip().casefold() in {"exercises", "review exercises", "problems", "习题", "练习"}:
        options.append("rule-below: true")
    if render_kind == "caption":
        options.append("caption-center: " + str(element.style.get("alignment", profile.get("caption_align", "center")) == "center").lower())
    available_height = geometry["height"] - geometry["top"] - geometry["bottom"]
    if element.kind in {"exercise", "code", "formula", "equation", "figure", "image"}:
        keep = _estimate_height(element, text, book, width, profile) <= available_height * .3
        options.append("keep: " + str(keep).lower())
    if element.kind == "code":
        # Code is a protected source object. Its spaces and line breaks are
        # preserved, not passed through translation or prose line wrapping.
        body = ("#raw(" + _q(text) + ", block: true)" if not element.asset_ids else
                "#align(left)[" + "#v(3pt)".join(_asset_markup(a, element.id, book, paths, width, issues, inline=False)
                                                   for a in element.asset_ids) + "]")
        if not element.asset_ids and max((len(line.expandtabs(4)) for line in text.splitlines()), default=0) * size * .6 > width:
            issues.append({"code": "code_too_wide", "severity": "error", "element_id": element.id,
                           "page": element.page, "detail": "代码按原行保留，存在超过当前栏宽的行"})
    if render_kind == "sidebar" and element.metadata.get("role") not in {"frontmatter", "chapter_contents"}:
        body = "#block(inset: 8pt, stroke: .5pt + rgb(\"d5dbe0\"), radius: 3pt, fill: rgb(\"f5f9fc\"))[" + body + "]"
    if element.kind == "table" and element.metadata.get("table_grid"):
        table = _table_markup(element, texts or {}, book, paths, geometry, width, profile, issues, labels,
                              preview_ids or set())
        body = body + table if text.strip() else table
    if element.kind == "exercise":
        options.append("subpart: " + str(bool(element.group_id) and element.id != element.group_id).lower())
        number = _EXERCISE_LABEL_RE.match(text)
        if number and not TOKEN_RE.search(number[0]):
            label = number[1].strip()
            label_width = max(size * 1.6, len(label) * size * .53 + size * .35)
            marker_markup = ""
            marker_width = sum(max((book.assets[a].width for a in marker.asset_ids), default=0) + 2 for marker in markers)
            outside = marker_width <= geometry["gutter"]
            shift = 0.0
            for marker in markers:
                natural = max((book.assets[a].width for a in marker.asset_ids), default=0)
                rendered = _element_markup(marker, (texts or {}).get(marker.id, marker.text), book, paths,
                                           geometry, natural, profile, issues, False, labels, 1, texts, preview_ids)
                shift += natural + 2
                dx = -shift if outside else marker_width - shift
                marker_markup += "#place(top + left, dx: " + _number(dx) + "pt)[" + rendered + "]"
            if not outside:
                label_width += marker_width
                marker_markup += "#h(" + _number(marker_width) + "pt)"
            # A real label/body grid gives every continuation line the same
            # body indent. A paragraph-indent setting alone does not align
            # independently generated inline text/asset fragments reliably.
            offset = "settings.number_offsets.at(" + _q(element.id) + ", default: 0) * 1pt"
            number_probe = "#baseline-probe(\"number-baseline\", " + _q(element.id) + ")"
            body_probe = ("" if element.metadata.get('display_asset_ids') else
                          "#baseline-probe(\"exercise-baseline\", " + _q(element.id) + ")")
            body = "#grid(columns: (" + _number(label_width) + "pt, 1fr), column-gutter: 0pt, " + \
                   "[#pad(top: " + offset + ")[" + marker_markup + number_probe + "#text(weight: \"bold\", " + _q(label) + ")]], [" + \
                   body_probe + _text_markup(text[number.end():], element, book, paths, width - label_width, issues) + "])"

        options.append("exercise: true")
    if margin_notes:
        body = _margin_markup(margin_notes, element, texts or {}, book, paths, geometry, width, profile, issues, labels, preview_ids or set(), flow_inset, rail_shift) + body
    return "#tracked(" + ", ".join(args + options) + ")[" + body + "]\n"


def _inline_group_markup(group, texts, book, paths, geometry, width, profile, issues, labels, preview_ids, columns):
    """Join explicitly related source fragments without merging their IDs."""
    identities, bodies = [], []
    texts = dict(texts)
    proof = group[0].metadata.get('cross_page_math')
    if proof:
        first, second = proof['segments']
        a, b = ('⟦F:'+s['asset_id']+'⟧' for s in (first, second))
        if texts[first['element_id']].count(a) != 1 or texts[second['element_id']].count(b) != 1:
            raise CompositionError('跨原页公式标记身份或数量不一致：'+proof['id'])
        texts[first['element_id']] = texts[first['element_id']].replace(a, a+b)
        texts[second['element_id']] = texts[second['element_id']].replace(b, '')
    heading = any(e.kind == "heading" for e in group)
    contents = all(e.metadata.get("frontmatter_kind") == "contents" for e in group)
    word_group = any(e.metadata.get("inline_group") for e in group)
    color = str(profile.get("heading_color", "00a9df")).lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        color = "00a9df"
    pending, rows = "", []
    translated_opening = any(not e.metadata.get("drop_cap") and e.id not in preview_ids and
                             re.search(r"[\u3400-\u9fff]", texts[e.id]) for e in group)
    leaders = re.compile(r"(?:[.·•…]\s*){3,}\s*(\d+|[ivxlcdm]+)(?=\s|$)", re.I)
    for index, element in enumerate(group):
        identities.append("(id: " + _q(element.id) + ", kind: " + _q(element.kind) +
                          ", source-page: " + str(element.page) + ", source-label: " + _q(labels[element.page]) + ")")
        text = texts[element.id]
        if element.metadata.get("drop_cap"):
            if translated_opening:
                issues.append({"code": "drop_cap_absorbed", "severity": "info", "element_id": element.id,
                               "page": element.page, "detail": "原文首字下沉的拉丁字母并入中文开头；源编号保留在整段位置记录中"})
                continue
            text = element.text
        style = "size: " + _number(_font_size(element, profile)) + "pt"
        if element.kind == "heading":
            style += ", font: " + _q(profile.get("heading_font", "SimHei")) + ", weight: \"bold\", fill: rgb(" + _q(color) + ")"
        if contents:
            # Dotted leaders are graphic spacing, not text to justify. A
            # translated source block can contain multiple Contents rows.
            position = 0
            for match in leaders.finditer(text):
                fragment = text[position:match.start()].strip()
                if fragment:
                    pending += ("#h(.6em)" if pending else "") + "#text(" + style + ")[" + _text_markup(fragment, element, book, paths, width, issues) + "]"
                rows.append("#contents-row(" + _q(match[1]) + ")[" + pending + "]")
                pending, position = "", match.end()
            remainder = text[position:].strip()
            if remainder:
                pending += ("#h(.6em)" if pending else "") + "#text(" + style + ")[" + _text_markup(remainder, element, book, paths, width, issues) + "]"
            continue
        body = _text_markup(text, element, book, paths, width, issues)
        tokens = set(TOKEN_RE.findall(text))
        for aid in element.asset_ids:
            if aid not in tokens and not (proof and aid in {s['asset_id'] for s in proof['segments']}):
                body += _asset_markup(aid, element.id, book, paths, width, issues)
        if element.id in preview_ids and not word_group:
            body = '#text(size: 8pt, fill: rgb("b83a24"), "【未翻译原文】")' + body
        join = element.metadata.get("inline_join")
        separator = "" if join == "word" else "#h(.25em)" if join == "space" else "#h(.6em)"
        bodies.append((separator if bodies else "") + "#text(" + style + ")[" + body + "]")
    if contents:
        if pending:
            rows.append("#contents-row(\"\")[" + pending + "]")
        bodies = rows
        if any(e.id in preview_ids for e in group):
            bodies.insert(0, '#text(size: 8pt, fill: rgb("b83a24"), "【未翻译原文】")')
    if not contents and word_group and any(e.id in preview_ids for e in group):
        bodies.insert(0, '#text(size: 8pt, fill: rgb("b83a24"), "【未翻译原文】")')
    return "#inline-source-group((" + ", ".join(identities) + ",), " + _number(width) + "pt, " + \
           "heading: " + str(heading).lower() + ", contents: " + str(contents).lower() + ", column-count: " + str(columns) + ")[" + "".join(bodies) + "]\n"


def _rail_markup(rail, book, texts, paths, geometry, profile, issues, labels, preview_ids):
    """Compose an explicit chapter mini-Contents beside its opening prose."""
    lookup = book.element_map()
    sidebar = [lookup[i] for i in rail["sidebar_ids"]]
    body = [lookup[i] for i in rail["body_ids"]]
    left, right = rail.get("bbox"), rail.get("body_bbox")
    if (not left or not right or len(left) != 4 or len(right) != 4 or
            left[2] <= left[0] or right[2] <= right[0] or right[0] <= left[2]):
        raise CompositionError("章节侧目录的区域关系无效，拒绝把左右栏混排")
    left_width, right_width, gutter = left[2] - left[0], right[2] - right[0], right[0] - left[2]
    inset = max(0, left[0] - geometry["left"])
    available = geometry["width"] - geometry["left"] - geometry["right"] - inset
    if left_width + right_width + gutter > available + .5:
        raise CompositionError("章节侧目录与正文区域超出当前页面宽度；不缩小文字")
    def render(items, width, style):
        output = []
        groups = _associated_groups(items, texts, book, geometry, style)
        for group, keep, _, _, _ in groups:
            if len(group) > 1 and _inline_key(group[0]):
                output.append(_inline_group_markup(group, texts, book, paths, geometry, width, style,
                                                   issues, labels, preview_ids, 1))
            else:
                output.extend(_element_markup(e, texts[e.id], book, paths, geometry, width, style, issues,
                                              e.id in preview_ids, labels, 1, texts, preview_ids) for e in group)
        return "".join(output)
    sidebar_style = {**profile, "body_size": profile["caption_size"]}
    left_body = render(sidebar, left_width, sidebar_style)
    right_body = render(body, right_width, profile)
    return "#block(inset: (left: " + _number(inset) + "pt), below: 8pt)[#grid(columns: (" + \
           _number(left_width) + "pt, " + _number(right_width) + "pt), column-gutter: " + _number(gutter) + \
           "pt, grid.cell(breakable: true)[" + left_body + "], grid.cell(breakable: true)[" + right_body + "])]\n"


def _build_source(book, translations, outdir, geometry, paths, allow_incomplete,
                  settings_name="composition-settings.json", document_preview=None):
    if book.profile.get("source_design_panels_v1"):
        from .design_panels import checked_panels
        checked_panels(book, scoped=True)
    profile = dict(book.profile)
    profile.update({key: _positive(profile.get(key), value) for key, value in _FONT_SIZES.items()})
    texts, preview_ids, issues = {}, set(), []
    seen_ids = set()
    for element in book.elements:
        if element.id in seen_ids:
            raise CompositionError(f"源元素编号重复：{element.id}")
        seen_ids.add(element.id)
        if _requires_translation(element):
            translated = translations.get(element.id)
            if not isinstance(translated, str) or not translated.strip():
                if not allow_incomplete:
                    raise CompositionError(f"译文缺失：{element.id} ({source_label(element)})；仅预览可使用 allow_incomplete")
                translated = element.text
                preview_ids.add(element.id)
                issues.append({"code": "untranslated_preview", "severity": "error", "element_id": element.id,
                               "page": element.page, "detail": "预览中以醒目标记显示未翻译原文"})
            expected, actual = Counter(TOKEN_RE.findall(element.text)), Counter(TOKEN_RE.findall(translated))
            if expected != actual:
                raise CompositionError(f"公式标记不匹配，拒绝排版：{element.id}；应有 {dict(expected)}，实有 {dict(actual)}")
            if "⟦" in TOKEN_RE.sub("", translated) or "⟧" in TOKEN_RE.sub("", translated):
                raise CompositionError(f"公式标记损坏，拒绝排版：{element.id}")
            texts[element.id] = translated
        else:
            texts[element.id] = element.text
    settings = dict(geometry)
    settings.update({k: profile.get(k, default) for k, default in {
        "body_font": "SimSun", "heading_font": "SimHei", "heading_color": "00a9df", **_FONT_SIZES}.items()})
    settings["preview"] = bool(preview_ids) if document_preview is None else bool(document_preview)
    settings["column_breaks"] = {}
    settings["number_offsets"] = {}
    source_errors = [i for i in book.issues if i.get("severity") == "error"]
    # Raw diagnostics remain in the map/report. A reader sees only the status
    # of source elements actually present on this output page, not chunk-wide
    # codes copied onto unrelated translated pages.
    settings["preview_source_diagnostics"] = [
        {"page": issue.get("page"), "element_id": issue.get("element_id")}
        for issue in source_errors]
    settings["preview_untranslated_ids"] = sorted(preview_ids)
    (outdir / settings_name).write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ['#import "textbook-template.typ": *', '#let settings = json(' + _q(settings_name) + ')', '#show: book-style.with(settings)']
    table_children = {cell for e in book.elements if e.kind == "table" and isinstance(e.metadata.get("table_grid"), dict)
                      and not e.metadata.get("render_suppressed") and not e.metadata.get("rendered_by_heading")
                      and not e.metadata.get("parent_figure_id")
                      for row in e.metadata["table_grid"].get("rows", []) for cell in row}
    lookup = book.element_map()
    markers = defaultdict(list)
    for element in book.elements:
        anchor = element.metadata.get("anchor_id")
        if element.metadata.get("role") == "exercise_marker" and anchor in lookup and lookup[anchor].kind == "exercise":
            markers[anchor].append(element)
    attached_markers = {e.id for group in markers.values() for e in group}
    rail_anchors, rail_ids = {}, set()
    for index, page in enumerate(book.pages):
        rail = page.get("chapter_rail", page.get("metadata", {}).get("chapter_rail"))
        if not isinstance(rail, dict):
            continue
        number = int(page.get("number", page.get("page", index + 1)))
        ids = rail.get("sidebar_ids", []) + rail.get("body_ids", [])
        if (not ids or len(ids) != len(set(ids)) or any(i not in lookup or lookup[i].page != number for i in ids)):
            raise CompositionError(f"原页 {number} 的章节侧目录包含重复或未知源元素")
        anchor = next((e for e in book.elements if e.page == number and e.metadata.get("banner_background_asset_ids")), None)
        if not anchor:
            raise CompositionError(f"原页 {number} 的章节侧目录缺少章节标题关联")
        rail_anchors[anchor.id], rail_ids = rail, rail_ids | set(ids)
    visible = [e for e in book.elements if e.kind not in _PERIPHERAL and e.id not in table_children
               and e.id not in attached_markers and not e.metadata.get("rendered_by_heading")
               and not e.metadata.get("render_suppressed") and not e.metadata.get('parent_figure_id') and e.id not in rail_ids]
    if not visible:
        raise CompositionError("所选页面只有页眉或页码，没有正文可排版")
    margin_notes, margin_ids = _margin_attachments(visible, geometry)
    visible = _flow_annotations([e for e in visible if e.id not in margin_ids])
    page_info = {int(p.get("number", p.get("page", i + 1))): p for i, p in enumerate(book.pages)}
    from .exercise_flow_adapter import annotate, PLAN_KEY
    visible = annotate(visible, book.profile.get(PLAN_KEY), page_info)
    visible = _exercise_flow_annotations(visible, page_info)
    groups = _associated_groups(visible, texts, book, geometry, profile)
    argument_anchors={}
    if book.profile.get("argument_units_v1"):
        from .argument_layout import projected_groups
        groups,argument_anchors=projected_groups(book,groups,geometry)
    page_labels = _book_page_labels(book)
    current_columns, current_chapter, in_columns = 1, "", False
    previous_source_column = None
    previous_source_page = None
    previous_exercise_flow = False
    exercise_flow_id = None
    for group, keep, columns, width, inset in groups:
        # A sample may deliberately omit whole stretches of the source book.
        # Never let the next selected page look like a continuation of a
        # question from before that gap. Ordinary consecutive pages still flow.
        source_jump = previous_source_page is not None and group[0].page > previous_source_page + 1
        if source_jump:
            if in_columns:
                lines.append("]")
                in_columns = False
            lines.append("#pagebreak(weak: true)")
            if not group[0].metadata.get("banner_background_asset_ids"):
                lines.append("#source-segment-start(" + _q(page_labels[group[0].page]) + ")")
            current_columns = 1
            previous_source_column = None
            previous_exercise_flow = False
            exercise_flow_id = None
        chapter = group[0].chapter
        major_chapter_start = (group[0].metadata.get("chapter_start") is True or
                               (group[0].kind == "heading" and group[0].style.get("level") == 1))
        if group[0].metadata.get("banner_background_asset_ids") or (major_chapter_start and current_chapter and chapter != current_chapter):
            if in_columns:
                lines.append("]")
                in_columns = False
            lines.append("#pagebreak(weak: true)")
            current_columns = 1
            previous_source_column = None
        if chapter:
            current_chapter = chapter
        if columns != current_columns:
            if in_columns:
                lines.append("]")
                in_columns = False
            if columns > 1:
                lines.append(f"#columns({columns}, gutter: {_number(geometry['gutter'])}pt)[")
                in_columns = True
            current_columns = columns
            previous_source_column = None
        source_column = group[0].style.get("column", group[0].metadata.get("column"))
        if (columns > 1 and source_column == 1 and previous_source_column == 0 and
                group[0].page == previous_source_page and
                not (previous_exercise_flow and group[0].kind == "exercise")):
            boundary_id = _q(group[0].id)
            lines.append("#source-column-boundary(" + boundary_id + ", break-before: settings.column_breaks.at(" +
                         boundary_id + ", default: false))")
        exercise_group = columns > 1 and all(e.kind == "exercise" for e in group)
        if exercise_group:
            if not previous_exercise_flow:
                exercise_flow_id = group[0].id
            boundary_id = _q(group[0].id)
            lines.append("#exercise-column-candidate(" + boundary_id + ", " + _q(exercise_flow_id) +
                         ", break-before: settings.column_breaks.at(" + boundary_id + ", default: false))")
        previous_source_column = source_column if columns > 1 else None
        previous_source_page = group[0].page
        previous_exercise_flow = exercise_group
        if inset:
            # Inner block margins collapse at a containing block boundary.
            # Restore the role's outside spacing on this inset wrapper.
            heading_role = group[0].metadata.get("render_as", group[0].kind) == "heading"
            above = 10 if heading_role else 0
            sticky = str(heading_role or group[0].metadata.get("layout_sticky_next", False)).lower()
            lines.append(f"#block(inset: (left: {_number(inset)}pt), above: {above}pt, below: 5pt, sticky: {sticky})[")
        if keep:
            lines.append("#block(breakable: false, below: 6pt)[")
        if group[0].metadata.get("layout_spatial_unit"):
            lines.append(_spatial_native_markup(group, texts, book, paths, geometry, width, profile,
                                                issues, page_labels, preview_ids, columns))
        elif group[0].id in argument_anchors:
            from .argument_layout import markup as argument_markup
            lines.append(argument_markup(argument_anchors[group[0].id],texts,book,paths,geometry,width,profile,issues,page_labels,preview_ids,columns))
        elif len(group) > 1 and _inline_key(group[0]):
            lines.append(_inline_group_markup(group, texts, book, paths, geometry, width, profile, issues,
                                               page_labels, preview_ids, columns))
        else:
            element_index = 0
            while element_index < len(group):
                element = group[element_index]
                if element.metadata.get("layout_only_flow_unit") and _inline_key(element):
                    stop = element_index + 1
                    while stop < len(group) and _inline_key(group[stop]) == _inline_key(element):
                        stop += 1
                    if stop > element_index + 1:
                        lines.append(_inline_group_markup(group[element_index:stop], texts, book, paths, geometry, width, profile, issues, page_labels, preview_ids, columns))
                        element_index = stop
                        continue
                if element_index + 1 < len(group) and _same_line_subpart(element, group[element_index + 1]):
                    following = replace(group[element_index + 1], metadata={**group[element_index + 1].metadata, "inline_join": "space"})
                    lines.append(_inline_group_markup([element, following], texts, book, paths, geometry, width,
                                                       profile, issues, page_labels, preview_ids, columns))
                    element_index += 2
                    continue
                lines.append(_element_markup(element, texts[element.id], book, paths, geometry, width, profile,
                                             issues, element.id in preview_ids, page_labels, columns, texts, preview_ids,
                                             sticky_next=element.kind == "exercise" and element_index == 0 and len(group) > 1 and not keep,
                                             markers=markers.get(element.id, ()),
                                             margin_notes=margin_notes.get(element.id, ()), flow_inset=inset))
                element_index += 1
        if keep:
            lines.append("]")
        if inset:
            lines.append("]")
        for element in group:
            if element.id in rail_anchors:
                lines.append(_rail_markup(rail_anchors[element.id], book, texts, paths, geometry, profile,
                                          issues, page_labels, preview_ids))
    if in_columns:
        lines.append("]")
    return "\n".join(lines) + "\n", issues, preview_ids


def _fragments(start, end, geometry):
    """Convert actual Typst boundary positions into allocated block fragments."""
    first, last = int(start["page"]), int(end["page"])
    top, bottom = geometry["top"], geometry["height"] - geometry["bottom"]
    width = float(start["width"])
    left, right = geometry["left"], geometry["width"] - geometry["right"]
    two_columns = int(start.get("columns", 1)) > 1
    col_width = width
    starts_x = float(start["x"])
    ends_x = float(end["x"])
    split_x = left + col_width + geometry["gutter"]
    result = []
    for page in range(first, last + 1):
        if not two_columns:
            y0 = float(start["y"]) if page == first else top
            y1 = float(end["y"]) if page == last else bottom
            result.append({"page": page, "bbox": [starts_x, y0, starts_x + width, max(y0 + .01, y1)]})
            continue
        begin_col = 0 if starts_x < split_x - geometry["gutter"] / 2 else 1
        end_col = 0 if ends_x < split_x - geometry["gutter"] / 2 else 1
        lo = begin_col if page == first else 0
        hi = end_col if page == last else 1
        for column in range(lo, hi + 1):
            x = left if column == 0 else split_x
            y0 = float(start["y"]) if page == first and column == begin_col else top
            y1 = float(end["y"]) if page == last and column == end_col else bottom
            result.append({"page": page, "bbox": [x, y0, x + col_width, max(y0 + .01, y1)], "column": column})
    return result


def _layout_map(book, traces, geometry, output, issues):
    elements, assets, starts, ends = {}, defaultdict(list), defaultdict(list), defaultdict(list)
    excluded = {}
    page_sources = defaultdict(dict)
    source_labels = _book_page_labels(book)
    tables, table_header_ids = {}, set()
    for element in book.elements:
        grid = element.metadata.get("table_grid")
        if element.kind == "table" and isinstance(grid, dict):
            rows = grid.get("rows", [])
            try:
                header_count = max(0, int(grid.get("header_rows", 1)))
            except (TypeError, ValueError):
                header_count = 1
            header_ids = [cell for row in rows[:header_count] for cell in row]
            table_header_ids.update(header_ids)
            tables[element.id] = {"rows": rows, "header_element_ids": header_ids,
                                  "source_page": element.page, "repeated_header": True}
    expected_assets = Counter()
    retained_labels = []
    nested_assets = defaultdict(list)
    for trace in traces:
        if trace.get("type") != "native-retained-label":
            continue
        record = dict(trace["retained"])
        x, y, width, height = (float(trace[k]) for k in ("x", "y", "width", "height"))
        record.update({"rendered_page": int(trace["page"]), "bbox": [x, y, x + width, y + height]})
        retained_labels.append(record)
        for aid in record["covered_asset_ids"]:
            nested_assets[aid].append(record)
    for element in book.elements:
        if element.metadata.get("render_suppressed"):
            continue
        in_tokens, declared = Counter(TOKEN_RE.findall(element.text)), Counter(element.asset_ids)
        expected_assets.update({asset_id: max(in_tokens[asset_id], declared[asset_id])
                                for asset_id in in_tokens.keys() | declared.keys()})
    for record in retained_labels:
        expected_assets.subtract(record["covered_asset_occurrences"])
        if record["covered_asset_ids"]:
            issues.append({"code": "protected_assets_retained_with_native_label", "severity": "warning",
                "element_id": record["element_id"], "page": record["source_page"],
                "asset_ids": record["covered_asset_ids"],
                "detail": "公式随原生标签裁片保留，未冒充独立资产像素通过；需要校核裁片内原公式。"})
    max_page = 0
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        max_page = max(max_page, int(trace.get("page", 1)))
        if trace.get("type") == "element":
            (starts if trace.get("phase") == "start" else ends)[trace["id"]].append(trace)
        elif trace.get("type") == "asset":
            x, y, w, h = (float(trace[k]) for k in ("x", "y", "width", "height"))
            assets[trace["id"]].append({"page": int(trace["page"]), "bbox": [x, y, x + w, y + h],
                                        "element_id": trace["element_id"], "precision": "measured",
                                        "render_format": trace.get("render_format", "pdf")})
    for element in book.elements:
        if element.kind in _PERIPHERAL:
            elements[element.id] = []
            excluded[element.id] = {"kind": element.kind, "source_page": element.page,
                                    "reason": "原页眉、页脚和原页码由每页生成的来源页码标记替代"}
            continue
        if element.metadata.get("render_suppressed"):
            elements[element.id] = []
            excluded[element.id] = {"kind": element.kind, "source_page": element.page,
                                    "reason": element.metadata.get("exclusion_reason", "重复源图层由合并对象呈现"),
                                    "superseded_by": element.metadata.get("superseded_by")}
            continue
        if element.id not in starts or element.id not in ends:
            issues.append({"code": "missing_layout_trace", "severity": "error", "element_id": element.id,
                           "page": element.page, "detail": "排版引擎未返回源元素的位置"})
            continue
        beginnings, endings = starts[element.id], ends[element.id]
        if len(beginnings) != len(endings):
            issues.append({"code": "unpaired_layout_trace", "severity": "error", "element_id": element.id,
                           "page": element.page, "detail": "排版位置的起止记录数量不一致"})
        fragments = [fragment for start, end in zip(beginnings, endings)
                     for fragment in _fragments(start, end, geometry)]
        for fragment in fragments:
            fragment.update({"kind": element.kind, "source_page": element.page,
                             "source_column": element.style.get("column", element.metadata.get("column")),
                             "precision": "block-fragment"})
            if _inline_key(element):
                fragment["precision"] = "shared-inline-group"
            if element.id in table_header_ids:
                fragment["role"] = "table_header"
                fragment["repeated_header"] = len(beginnings) > 1
            page_sources[fragment["page"]][element.page] = source_labels[element.page]
        elements[element.id] = fragments
    for element in book.elements:
        target = element.metadata.get("superseded_by") if element.metadata.get("render_suppressed") else None
        if target and elements.get(target):
            elements[element.id] = [{**record, "kind": element.kind, "source_page": element.page,
                                      "precision": "shared-superseded", "superseded_by": target}
                                     for record in elements[target]]
    for asset_id, asset in book.assets.items():
        occurrences = assets.get(asset_id, [])
        expected = expected_assets[asset_id]
        if expected and len(occurrences) < expected:
            issues.append({"code": "missing_asset_trace", "severity": "error", "asset_id": asset_id,
                           "page": asset.page, "detail": f"对象预期出现 {expected} 次，实际记录 {len(occurrences)} 次"})
        for occurrence in occurrences:
            owner = book.element_map().get(occurrence.get("element_id"))
            if owner and owner.metadata.get('figure_label_ids') and asset.metadata.get('detector')=='translated-figure-background':
                retained_ids = {item["element_id"] for item in retained_labels}
                label_ids=[eid for eid in owner.metadata['figure_label_ids'] if eid not in retained_ids]
                regions=[r['bbox'] for eid in label_ids for r in elements.get(eid,[])
                         if r.get('page')==occurrence['page']]
                if len(regions)!=len(label_ids):
                    issues.append({'code':'figure_label_trace_missing','severity':'error','element_id':owner.id,
                                   'page':owner.page,'detail':'图内译文没有与背景同页的完整位置证据'})
                occurrence['translated_label_regions']=regions
                occurrence['translated_label_ids']=list(label_ids)
                occurrence['translated_label_regions_precision']='measured-figure-labels'
            heading_id = owner.metadata.get("rendered_by_heading") if owner else None
            heading = book.element_map().get(heading_id)
            if heading and asset_id in heading.metadata.get("banner_background_asset_ids", []):
                dx, dy = occurrence["bbox"][0] - asset.bbox[0], occurrence["bbox"][1] - asset.bbox[1]
                regions = [heading.metadata.get(key) for key in
                           ("banner_text_bbox", "banner_label_bbox", "banner_number_bbox")]
                occurrence["translated_label_regions"] = [[r[0] + dx, r[1] + dy, r[2] + dx, r[3] + dy]
                    for r in regions if isinstance(r, (list, tuple)) and len(r) == 4 and r[2] > r[0] and r[3] > r[1]]
                occurrence["translated_label_regions_precision"] = "source-design-region"
            if occurrence["element_id"] in table_header_ids:
                occurrence["repeated_header"] = len(occurrences) > 1
            x0, y0, x1, y1 = occurrence["bbox"]
            if x0 < -.5 or y0 < -.5 or x1 > geometry["width"] + .5 or y1 > geometry["height"] + .5:
                issues.append({"code": "asset_out_of_page", "severity": "error", "asset_id": asset_id,
                               "page": asset.page, "rendered_page": occurrence["page"], "detail": "原尺寸对象超出页面边界"})
    # Check known graphic/anchor links using actual rendered positions.
    by_id = book.element_map()
    graphic_groups = {e.group_id: e.id for e in book.elements
                      if e.group_id and e.kind in {"figure", "table", "image"}}
    for element in book.elements:
        render_kind = element.metadata.get("render_as", element.kind)
        if render_kind not in {"caption", "paragraph"}:
            continue
        anchor_id = element.metadata.get("figure_id", element.metadata.get("anchor_id"))
        if not anchor_id and element.group_id:
            anchor_id = graphic_groups.get(element.group_id)
        if anchor_id in by_id and anchor_id in elements and element.id in elements:
            anchor_pages = [p["page"] for p in elements[anchor_id]]
            current_pages = [p["page"] for p in elements[element.id]]
            distance = min(abs(a - b) for a in anchor_pages for b in current_pages)
            limit = 0 if render_kind == "caption" else 1
            if distance > limit:
                issues.append({"code": "detached_caption" if not limit else "detached_explanation", "severity": "error",
                               "element_id": element.id, "anchor_id": anchor_id, "page": element.page,
                               "detail": f"图表与关联内容相距 {distance} 张译文页"})
    from .cross_page_math import check_joined_math_geometry
    issues.extend(check_joined_math_geometry(book, assets))
    pages = [{"page": page, "width": geometry["width"], "height": geometry["height"],
              "source_pages": sorted(page_sources[page]),
              "source_labels": [page_sources[page][p] for p in sorted(page_sources[page])]}
             for page in range(1, max_page + 1)]
    return {"schema_version": 1, "pdf_path": str(output), "page_count": max_page,
            "geometry": geometry, "pages": pages, "elements": elements, "assets": dict(assets),
            "tables": tables, "excluded_elements": excluded, "issues": issues,
            "native_retained_labels": retained_labels,
            "figure_label_layout_decisions": [{"element_id": trace["id"], "decision": trace["decision"],
                "measurement": trace["measurement"], "rendered_page": trace["page"],
                "x": trace["x"], "y": trace["y"]}
                for trace in traces if trace.get("type") == "figure-label-decision"],
            "not_displayed_translated_label_ids": sorted({row["element_id"] for row in retained_labels
                if row.get("reason") == "translated_label_overflow_preview"}),
            "native_spatial_punctuation": [{**trace['retained'], 'rendered_page': trace['page'],
                'bbox': [trace['x'], trace['y'], trace['x']+trace['width'], trace['y']+trace['height']]}
                for trace in traces if trace.get('type') == 'native-spatial-punctuation'],
            "figure_label_blank_expansions": [{**trace["proof"], "rendered_page": trace["page"],
                "x": trace["x"], "y": trace["y"]} for trace in traces if trace.get("type") == "label-blank-expansion"],
            "assets_retained_inside_native_labels": dict(nested_assets)}


def _partition_book(book):
    """Bound each engine invocation without splitting a source page/group."""
    try:
        limit = max(1, min(500, int(book.profile.get("layout_chunk_source_pages", 80))))
    except (TypeError, ValueError):
        limit = 80
    pages = list(dict.fromkeys(e.page for e in book.elements))
    page_index = {page: i for i, page in enumerate(pages)}
    flow_spans = {}
    plan = book.profile.get(PLAN_KEY)
    if plan:
        # Validate the exact current source records and visible adjacency before
        # treating any flow as atomic. Pages come only from those actual
        # members; a plan's claimed page range never controls partitioning.
        from .exercise_flow_adapter import annotate
        page_info = {int(p.get("number", p.get("page", i + 1))): p
                     for i, p in enumerate(book.pages)}
        visible = [e for e in book.elements if e.kind not in _PERIPHERAL
                   and not e.metadata.get("render_suppressed")]
        checked = {e.id: e for e in annotate(visible, plan, page_info)}
        for unit in plan["units"]:
            members = [checked[m["id"]] for m in unit["members"] if m["id"] in checked]
            if not members:
                continue
            positions = [page_index[e.page] for e in members]
            flow_spans[("verified-exercise-flow", unit["anchor_id"])] = (min(positions), max(positions))
    if book.profile.get("argument_units_v1"):
        from .argument_layout import page_spans
        flow_spans.update(page_spans(book,page_index))
    if len(pages) <= limit:
        return [book]
    graphic_groups = {e.group_id for e in book.elements
                      if e.group_id and e.kind in {"figure", "image", "table"}}
    spans = dict(flow_spans)
    for element in book.elements:
        if element.group_id in graphic_groups or element.metadata.get('cross_page_math'):
            position = page_index[element.page]
            lo, hi = spans.get(element.group_id, (position, position))
            spans[element.group_id] = (min(lo, position), max(hi, position))
    result = []
    start = 0
    while start < len(pages):
        end = min(len(pages), start + limit)
        changed = True
        while changed:
            changed = False
            for lo, hi in spans.values():
                if lo < end <= hi:
                    end, changed = hi + 1, True
        selected = pages[start:end]
        selected_set = set(selected)
        result.append(replace(book, selected_pages=selected,
                              pages=[p for i, p in enumerate(book.pages)
                                     if int(p.get("number", p.get("page", i + 1))) in selected_set],
                              elements=[e for e in book.elements if e.page in selected_set]))
        start = end
    return result



def _exercise_column_breaks(traces, geometry, book=None):
    """Balance only the last page of each measured exercise flow.

    This chooses one literal break at a complete question boundary. Positions
    from the first pass never participate in the final Typst evaluation, so no
    layout state depends on the result of its own break decision.
    """
    flows = defaultdict(list)
    for item in traces:
        if item.get("type") == "exercise-boundary":
            flows[item.get("flow", item["id"])].append(item)
    ends = {item["id"]: item for item in traces
            if item.get("type") == "element" and item.get("phase") == "end"}
    # The next boundary measures all subparts as well as the question stem.
    decisions = {}
    bottom = geometry["height"] - geometry["bottom"]
    midpoint = geometry["left"] + (geometry["width"] - geometry["left"] - geometry["right"]) / 2
    for flow in flows.values():
        decisions.update({item["id"]: False for item in flow})
        last = flow[-1]
        ending = ends.get(last["id"])
        if ending is None:
            continue
        if book is not None:
            stem = book.element_map().get(last["id"])
            if stem and stem.group_id:
                members = [ends[e.id] for e in book.elements if e.kind == "exercise" and
                           e.group_id == stem.group_id and e.id in ends]
                if members:
                    ending = max(members, key=lambda item: (int(item["page"]),
                                 float(item["x"]) >= midpoint, float(item["y"])))
        last_page = int(ending["page"])
        candidates = [item for item in flow if int(item["page"]) == last_page]
        if len(candidates) < 2:
            continue
        start_y = float(flow[0]["y"]) if int(flow[0]["page"]) == last_page else geometry["top"]
        height = bottom - start_y
        if height <= 0:
            continue
        def distance(item):
            column = 0 if float(item["x"]) < midpoint else 1
            return column * height + float(item["y"]) - start_y
        endpoint = distance(ending)
        if endpoint <= 0 or endpoint > 2 * height + 2:
            continue
        eligible = [item for item in candidates[1:] if float(item["x"]) < midpoint
                    and distance(item) <= height and endpoint - distance(item) <= height]
        if not eligible:
            continue
        chosen = min(eligible, key=lambda item: abs(2 * distance(item) - endpoint))
        decisions[chosen["id"]] = True
    return decisions



def _number_baseline_offsets(traces):
    """Measure first-line baseline differences, independent of glyph guesses."""
    labels = {item["id"]: item for item in traces if item.get("type") == "number-baseline"}
    bodies = {item["id"]: item for item in traces if item.get("type") == "exercise-baseline"}
    offsets = {}
    for identity, label in labels.items():
        body = bodies.get(identity)
        if body is None or int(body["page"]) != int(label["page"]):
            continue
        difference = float(body["y"]) - float(label["y"])
        if abs(difference) > .05:
            offsets[identity] = round(difference, 5)
    return offsets


def compose(book: Book, translations: dict, outdir, output_path=None, allow_incomplete=False) -> dict:
    """Compose a book without changing source text or protected asset bytes.

    ``allow_incomplete`` is an explicit preview only: every untranslated block
    and every page are visibly marked, and the result cannot pass final QA.
    Diagnostics are retained beside the PDF, including the executable Typst
    source and its queried positions. A successful compile is not a QA pass.
    """
    started = time.perf_counter()
    source_errors = [dict(i) for i in book.issues if i.get("severity") == "error"]
    if source_errors and not allow_incomplete:
        summary = "; ".join(f"原页{i.get('page', '全书')} {i.get('code', 'source_error')}" for i in source_errors[:8])
        raise CompositionError(f"原书解析尚有 {len(source_errors)} 项错误，拒绝生成正式译本：{summary}。修复后重试；仅显式 allow_incomplete 可生成带警示的预览。")
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    runtime = _Runtime.find()
    if not book.elements:
        raise CompositionError("书籍没有可排版的源元素")
    source_pdf = Path(book.source_path).resolve()
    if output_path:
        output = Path(output_path).resolve()
    else:
        title = safe_filename(book.title)
        output = outdir / (title + (".preview.zh.pdf" if allow_incomplete else ".zh.pdf"))
    if output == source_pdf:
        raise CompositionError("输出路径不能覆盖原书")
    if output.suffix.lower() != ".pdf":
        raise CompositionError("排版输出必须为 .pdf")
    output.parent.mkdir(parents=True, exist_ok=True)
    geometry = _page_geometry(book)
    roots = _source_roots(book, outdir)
    unique_assets = {asset.path: asset for asset in book.assets.values()}
    protected_paths = {_find_asset(asset, roots) for asset in unique_assets.values()}
    if output in protected_paths:
        raise CompositionError("输出路径不能覆盖不可变的原书对象文件")
    paths = _prepare_assets(book, outdir)
    paths["__output_root__"] = outdir
    paths["__allow_label_overflow_preview__"] = bool(allow_incomplete)
    if allow_incomplete:
        paths["__native_retained_labels__"] = _prepare_native_label_previews(book, translations, outdir)
    shutil.copyfile(_TEMPLATE, outdir / "textbook-template.typ")
    parts = _partition_book(book)
    issues, preview_ids, sources = list(source_errors), set(), []
    document_preview = bool(allow_incomplete) or any(_requires_translation(e) and
                           (not isinstance(translations.get(e.id), str) or not translations[e.id].strip())
                           for e in book.elements) or bool(source_errors)
    for index, part in enumerate(parts, 1):
        stem = "composition" if len(parts) == 1 else f"composition-{index:04d}"
        source, part_issues, part_preview = _build_source(
            part, translations, outdir, geometry, paths, allow_incomplete,
            settings_name=stem + "-settings.json", document_preview=document_preview)
        typst_path = outdir / (stem + ".typ")
        typst_path.write_text(source, encoding="utf-8")
        part_output = outdir / (stem + ".part.pdf")
        if part_output == source_pdf or part_output in protected_paths:
            raise CompositionError("排版中间文件路径与原书或不可变对象冲突，请更换输出目录")
        sources.append((part, typst_path, part_output))
        issues.extend(part_issues)
        preview_ids.update(part_preview)
    temporary = outdir / (".composition-" + uuid.uuid4().hex + ".pending.pdf")
    traces, diagnostic_messages, part_manifest = [], [], []
    offset = 0
    for index, (part, typst_source, part_output) in enumerate(sources, 1):
        diagnostics = runtime.compile(typst_source, part_output, outdir)
        part_traces = runtime.query(typst_source, outdir)
        overflow = {trace["id"]: trace["measurement"] for trace in part_traces
                    if trace.get("type") == "figure-label-overflow-probe"}
        if overflow:
            if not allow_incomplete:
                raise CompositionError("正式译本不能保留溢出标签的测量占位件")
            # The first PDF only measures fixed-size Chinese. Native copies
            # are made exclusively for actual overflows, then the source is
            # rebuilt and compiled again; no measurement placeholder ships.
            recovered = _prepare_native_label_previews(part, translations, outdir,
                                                       overflow_measurements=overflow)
            paths["__native_retained_labels__"].update(recovered)
            source, recovery_issues, recovery_preview = _build_source(
                part, translations, outdir, geometry, paths, allow_incomplete,
                settings_name=typst_source.stem + "-settings.json", document_preview=document_preview)
            typst_source.write_text(source, encoding="utf-8")
            existing = {digest(issue) for issue in issues}
            issues.extend(issue for issue in recovery_issues if digest(issue) not in existing)
            preview_ids.update(recovery_preview)
            diagnostics = runtime.compile(typst_source, part_output, outdir)
            part_traces = runtime.query(typst_source, outdir)
        if any(trace.get("type") == "figure-label-overflow-probe" for trace in part_traces):
            raise CompositionError("标签回退后仍有测量占位件，拒绝发布阅读稿")
        boundary_x = {trace["id"]: float(trace["x"]) for trace in part_traces
                      if trace.get("type") == "column-boundary"}
        midpoint = geometry["left"] + (geometry["width"] - geometry["left"] - geometry["right"]) / 2
        fixed_breaks = {identity: x < midpoint for identity, x in boundary_x.items()}
        fixed_breaks.update(_exercise_column_breaks(part_traces, geometry, part))
        number_offsets = _number_baseline_offsets(part_traces)
        settings_path = typst_source.with_name(typst_source.stem + "-settings.json")
        if fixed_breaks or number_offsets:
            fixed_settings = json.loads(settings_path.read_text(encoding="utf-8"))
            fixed_settings["column_breaks"] = fixed_breaks
            fixed_settings["number_offsets"] = number_offsets
            settings_path.write_text(json.dumps(fixed_settings, ensure_ascii=False, indent=2), encoding="utf-8")
        if any(fixed_breaks.values()) or number_offsets:
            # A separate compile sees literal break choices. No branch in the
            # final layout depends on a position that its output can change.
            diagnostics = runtime.compile(typst_source, part_output, outdir)
            part_traces = runtime.query(typst_source, outdir)
        if any(trace.get("type") == "figure-label-overflow-probe" for trace in part_traces):
            raise CompositionError("最终排版仍有未替换的标签测量占位件")
        if diagnostics:
            diagnostic_messages.append(f"Part {index}: {diagnostics}")
        try:
            import pymupdf
            with pymupdf.open(part_output) as pdf:
                count = len(pdf)
        except ImportError:
            count = max((int(t.get("page", 1)) for t in part_traces), default=0)
        for trace in part_traces:
            trace["page"] = int(trace.get("page", 1)) + offset
        traces.extend(part_traces)
        part_manifest.append({"index": index, "source_pages": part.selected_pages,
                              "typst_source": str(typst_source), "pdf_part": str(part_output),
                              "rendered_start_page": offset + 1, "page_count": count,
                              "column_boundary_strategy": "measured-two-pass",
                              "column_breaks": fixed_breaks, "number_label_offsets": number_offsets})
        offset += count
    if len(sources) == 1:
        shutil.copyfile(sources[0][2], temporary)
    else:
        try:
            import pymupdf
        except ImportError as exc:
            raise CompositionError("分段排版的矢量 PDF 合并需要 PyMuPDF") from exc
        with pymupdf.open() as merged:
            for _, _, part_output in sources:
                with pymupdf.open(part_output) as part_pdf:
                    merged.insert_pdf(part_pdf)
            merged.set_metadata({"title": book.title, "producer": f"Textbook PDF / Typst {TYPST_VERSION}"})
            merged.save(temporary, garbage=4, deflate=True)
    panel_paint_check = None
    if book.profile.get("source_design_panels_v1"):
        from .design_panels import restore_exact_paint
        panel_paint_check = restore_exact_paint(book, temporary, traces)
    diagnostics = "\n".join(diagnostic_messages)
    mapping = _layout_map(book, traces, geometry, output, issues)
    if panel_paint_check:
        mapping["source_design_panel_paint_restoration"] = panel_paint_check
        mapping["source_design_panel_traces"] = [t for t in traces if t.get("type") == "source-panel"]
    mapping["parts"] = part_manifest
    # A second PDF reader verifies that metadata coverage describes the actual
    # compiled document and supplies the physical count even for blank pages.
    try:
        import pymupdf
        with pymupdf.open(temporary) as pdf:
            actual_pages = len(pdf)
            mapping["page_count"] = actual_pages
            for i, page in enumerate(pdf):
                if i >= len(mapping["pages"]):
                    mapping["pages"].append({"page": i + 1, "width": page.rect.width, "height": page.rect.height,
                                              "source_pages": [], "source_labels": []})
    except ImportError:
        actual_pages = mapping["page_count"]
    if diagnostics:
        typography_error = any(token in diagnostics.lower() for token in ("unknown font family", "missing glyph", "could not find a font"))
        issues.append({"code": "typst_diagnostic", "severity": "error" if typography_error else "warning", "detail": diagnostics})
    if "did not converge" in diagnostics.lower():
        issues.append({"code": "layout_not_converged", "severity": "error", "detail": diagnostics})
    is_preview = bool(allow_incomplete or preview_ids or source_errors)
    mapping["preview"] = is_preview
    mapping["source_errors"] = source_errors
    mapping["engine"] = {"name": "Typst", "version": TYPST_VERSION,
                         "binary": str(runtime.binary) if runtime.binary else "python:typst"}
    mapping["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    mapping["limitations"] = ["元素位置为实际边界推导的段落片段矩形，非逐字字形框。",
                              "排版成功不等于翻译、公式或视觉检查通过；请运行独立 QA。"]
    if book.profile.get(PLAN_KEY):
        from .exercise_flow_adapter import check_reading_order
        measured_flow_issues = check_reading_order(book.profile[PLAN_KEY], traces, geometry, visible_ids={e.id for e in book.elements})
        issues.extend(measured_flow_issues)
        mapping["exercise_flow_check"] = {"opt_in": True, "issues": measured_flow_issues, "passed": not measured_flow_issues}
    if book.profile.get("argument_units_v1"):
        from .argument_qa import check_argument_layout
        argument_check=check_argument_layout(book,traces,geometry,temporary)
        issues.extend(argument_check["issues"])
        mapping["argument_check"]=argument_check
        argument_ids={eid for u in book.profile["argument_units_v1"].values()for eid in[u['parent_id']]+u['member_ids']}
        mapping["argument_traces"]=[t for t in traces if str(t.get("type", "")).startswith("argument-")or(t.get('type')=='element'and t.get('id')in argument_ids)]
    blocking = [issue for issue in issues if issue.get("severity") == "error"]
    if blocking and not allow_incomplete:
        # Keep an earlier valid output/map together. Failed measured geometry
        # remains inspectable, but must never acquire the formal filename.
        mapping["pdf_path"] = str(temporary)
        mapping["failed_formal_composition"] = True
        mapping["render_binding"] = rendering_binding(book, translations)
        mapping["pdf_sha256"] = file_sha256(temporary)
        failed_map = outdir / "failed-layout-map.json"
        failed_map.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
        detail = "; ".join(str(issue.get("code", "layout_error")) for issue in blocking[:8])
        raise CompositionError(f"测量排版存在 {len(blocking)} 项错误，未更新正式译本：{detail}。详情：{failed_map}")
    if temporary.drive.lower() == output.drive.lower():
        temporary.replace(output)
    else:
        staged_output = output.with_name("." + output.name + "." + uuid.uuid4().hex + ".pending")
        shutil.copyfile(temporary, staged_output)
        staged_output.replace(output)
        temporary.unlink()
    mapping["render_binding"] = rendering_binding(book, translations)
    mapping["pdf_sha256"] = file_sha256(output)
    map_path = outdir / "layout_map.json"
    map_path.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "typst-traces.json").write_text(json.dumps(traces, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "composition-index.json").write_text(json.dumps(part_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"pdf_path": str(output), "layout_map_path": str(map_path), "page_count": actual_pages,
            "engine": mapping["engine"], "preview": is_preview, "untranslated_count": len(preview_ids), "source_error_count": len(source_errors),
            "issues": issues, "elapsed_seconds": mapping["elapsed_seconds"], "composition_parts": len(parts),
            "passed": not is_preview and not any(i.get("severity") == "error" for i in issues)}
