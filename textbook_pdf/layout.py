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

TYPST_VERSION = "0.15.1"
_TEMPLATE = Path(__file__).with_name("templates") / "book.typ"
_FONT_SIZES = {"body_size": 10.5, "exercise_size": 9.5, "caption_size": 8.5}
_PERIPHERAL = {"running_header", "running_footer", "page_number"}


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
    return {"schema_version": 1, "source_sha256": book.source_sha256,
            "renderer_identity": {"layout_sha256": file_sha256(Path(__file__).resolve()),
                                  "template_sha256": file_sha256(_TEMPLATE),
                                  "typst_version": TYPST_VERSION},
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


def _associated_groups(elements, texts, book, geometry, profile):
    """Group real figures with their caption and directly associated prose."""
    groups = []
    index = 0
    available = geometry["height"] - geometry["top"] - geometry["bottom"]
    page_info = {int(p.get("number", p.get("page", i + 1))): p for i, p in enumerate(book.pages)}
    graphic_group_ids = {e.group_id for e in elements if e.group_id and e.kind in {"figure", "table", "image"}}
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
        if inline_group:
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
                                     and candidate.page == element.page)
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
        group_assets = {asset_id for e in group for asset_id in e.asset_ids + TOKEN_RE.findall(texts[e.id])}
        natural_width = max((book.assets[a].width for a in group_assets if a in book.assets), default=0)
        for e in group:
            if e.kind == "table" and e.metadata.get("table_grid"):
                natural_width = max(natural_width, sum(_table_column_minima(e, book, profile)))
        if width < natural_width <= full_width:
            columns, width = 1, full_width
            inset = 0
        estimated = sum(_estimate_height(e, texts[e.id], book, width, profile) for e in group)
        keep = len(group) > 1 and any(e.kind in {"figure", "table", "image"} for e in group) and estimated <= available * .82
        if exercise_group:
            keep = len(group) > 1 and estimated <= available * .48
        if inline_group:
            keep = len(group) > 1 and estimated <= available * .3
        if connective_group:
            keep = len(group) > 1 and estimated <= available * .48
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


def _flow_annotations(elements):
    """Recognize source run-in titles without changing stored source identities."""
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


def _margin_markup(notes, element, texts, book, paths, geometry, width, profile, issues, labels, preview_ids, flow_inset=0):
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
    return "#place(top + left, dx: -" + _number(margin_width + 8) + "pt)[#stack(dir: ttb, spacing: 4pt, " + ", ".join(rows) + ")]"


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


def _text_markup(text, element, book, paths, width, issues):
    parts = []
    position = 0
    matches = list(TOKEN_RE.finditer(text))
    skip = set()
    for index, match in enumerate(matches):
        if index in skip:
            continue
        if match.start() > position:
            parts.append("#text(" + _q(text[position:match.start()]) + ")")
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
        parts.append(_asset_markup(match[1], element.id, book, paths, width, issues))
        position = match.end()
    if position < len(text):
        parts.append("#text(" + _q(text[position:]) + ")")
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
                      'pt)[#block(width: 240pt)[#align(right)[#text(size: 8pt, fill: rgb("b83a24"), "未完成翻译或解析预览")' +
                      '#if settings.source_error_summary != "" { linebreak(); text(size: 7.5pt, fill: rgb("b83a24"), settings.source_error_summary) }]]] }')
    title_height = _estimate_height(element, title, book, rect[2] - rect[0], profile)
    height = max(source_bottom, rect[1] + title_height) - geometry["top"] + 10
    body = "#block(width: " + _number(width) + "pt, height: " + _number(height) + "pt)[" + "".join(placements) + "]"
    args = [_q(element.id), _q(element.kind), str(element.page), _q(label), _number(width) + "pt", _number(size) + "pt",
            "heading: true", "banner: true", "heading-font: " + _q(profile.get("heading_font", "SimHei"))]
    return "#tracked(" + ", ".join(args) + ")[" + body + "]\n"


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
        markup = ('#context { let label-body = [#set par(justify: false, leading: .1em)\n'
                  '#set text(size: ' + _number(profile['caption_size']) + 'pt, fill: rgb(' + _q(color) + '))\n' + body + ']; '
                  'let measured = measure(block(width: ' + _number(w) + 'pt, above: 0pt, below: 0pt)[#label-body]); '
                  'if measured.height > ' + _number(h) + 'pt { panic(' + _q('图内标签溢出：' + child.id) + ') }; '
                  'tracked(' + ', '.join((_q(child.id), _q('figure_label'), str(child.page), _q(label),
                  _number(w) + 'pt', _number(profile['caption_size']) + 'pt', 'keep: true')) + ')[#label-body] }')
        content.append('#place(top + left, dx: ' + _number(x) + 'pt, dy: ' + _number(y) + 'pt)[' + markup + ']')
    return '#align(center)[#box(width: ' + _number(aw) + 'pt, height: ' + _number(ah) + 'pt)[' + ''.join(content) + ']]'


def _element_markup(element, text, book, paths, geometry, width, profile, issues, preview=False, labels=None, columns=1,
                    texts=None, preview_ids=None, sticky_next=False, markers=(), margin_notes=(), flow_inset=0):
    if element.metadata.get("banner_background_asset_ids"):
        return _banner_markup(element, text, book, paths, geometry, profile, issues, preview, labels)
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
               "column-count: " + str(columns), "sticky-next: " + str(sticky_next).lower()]
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
        number = re.match(r"^(\s*(?:\d+[.)]|\([a-zA-Z0-9]+\)|[a-zA-Z][.)]))\s*", text)
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
            body_probe = "#baseline-probe(\"exercise-baseline\", " + _q(element.id) + ")"
            body = "#grid(columns: (" + _number(label_width) + "pt, 1fr), column-gutter: 0pt, " + \
                   "[#pad(top: " + offset + ")[" + marker_markup + number_probe + "#text(weight: \"bold\", " + _q(label) + ")]], [" + \
                   body_probe + _text_markup(text[number.end():], element, book, paths, width - label_width, issues) + "])"

        options.append("exercise: true")
    if margin_notes:
        body = _margin_markup(margin_notes, element, texts or {}, book, paths, geometry, width, profile, issues, labels, preview_ids or set(), flow_inset) + body
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
    summary = [f"原页 {i.get('page', '全书')}：{i.get('code', 'source_error')}" for i in source_errors[:3]]
    settings["source_error_summary"] = ("原书解析待修复：" + "；".join(summary) +
                                          (f"；另有 {len(source_errors) - 3} 项，详见质量报告" if len(source_errors) > 3 else "")) if summary else ""
    (outdir / settings_name).write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ['#import "textbook-template.typ": *', '#let settings = json(' + _q(settings_name) + ')', '#show: book-style.with(settings)']
    table_children = {cell for e in book.elements if e.kind == "table" and isinstance(e.metadata.get("table_grid"), dict)
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
    visible = _exercise_flow_annotations(visible, page_info)
    groups = _associated_groups(visible, texts, book, geometry, profile)
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
            sticky = str(heading_role).lower()
            lines.append(f"#block(inset: (left: {_number(inset)}pt), above: {above}pt, below: 5pt, sticky: {sticky})[")
        if keep:
            lines.append("#block(breakable: false, below: 6pt)[")
        if len(group) > 1 and _inline_key(group[0]):
            lines.append(_inline_group_markup(group, texts, book, paths, geometry, width, profile, issues,
                                               page_labels, preview_ids, columns))
        else:
            element_index = 0
            while element_index < len(group):
                element = group[element_index]
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
    for element in book.elements:
        if element.metadata.get("render_suppressed"):
            continue
        in_tokens, declared = Counter(TOKEN_RE.findall(element.text)), Counter(element.asset_ids)
        expected_assets.update({asset_id: max(in_tokens[asset_id], declared[asset_id])
                                for asset_id in in_tokens.keys() | declared.keys()})
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
                label_ids=owner.metadata['figure_label_ids']
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
            "tables": tables, "excluded_elements": excluded, "issues": issues}


def _partition_book(book):
    """Bound each engine invocation without splitting a source page/group."""
    try:
        limit = max(1, min(500, int(book.profile.get("layout_chunk_source_pages", 80))))
    except (TypeError, ValueError):
        limit = 80
    pages = list(dict.fromkeys(e.page for e in book.elements))
    if len(pages) <= limit:
        return [book]
    page_index = {page: i for i, page in enumerate(pages)}
    graphic_groups = {e.group_id for e in book.elements
                      if e.group_id and e.kind in {"figure", "image", "table"}}
    spans = {}
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
    if source_errors and allow_incomplete:
        # A source-error preview needs room for its page/code warning as well
        # as the preview label. Keep the warning inside the physical page.
        geometry["top"] = max(64, geometry["top"])
    roots = _source_roots(book, outdir)
    unique_assets = {asset.path: asset for asset in book.assets.values()}
    protected_paths = {_find_asset(asset, roots) for asset in unique_assets.values()}
    if output in protected_paths:
        raise CompositionError("输出路径不能覆盖不可变的原书对象文件")
    paths = _prepare_assets(book, outdir)
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
    diagnostics = "\n".join(diagnostic_messages)
    mapping = _layout_map(book, traces, geometry, output, issues)
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
