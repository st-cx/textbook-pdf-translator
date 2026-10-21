"""Pinned, parse-only BabelDOC bridge. Never calls a translator or typesetter.

The bridge intentionally raises on missing models/assets or unsupported versions;
the caller decides whether an explicitly reported native fallback is acceptable.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from importlib.metadata import PackageNotFoundError, version
import importlib.util
import json
import os
from pathlib import Path
import sys
import types

PINNED_VERSION = "0.6.4"


class BabelUnavailable(RuntimeError):
    pass


def _configure_cache(outdir):
    """Relocate upstream's hard-coded cache without changing home or installing files.

    BabelDOC 0.6.4 exposes no cache-dir setting. Its const module creates a
    directory at import time, so override the one constant before executing
    the otherwise unmodified pinned module. Never touch ~/.cache.
    """
    if "babeldoc.const" in sys.modules:
        return
    spec = importlib.util.find_spec("babeldoc.const")
    source = spec.loader.get_source("babeldoc.const")
    needle = 'CACHE_FOLDER = Path.home() / ".cache" / "babeldoc"'
    if source.count(needle) != 1:
        raise BabelUnavailable("Pinned upstream cache declaration changed; refusing home-cache writes")
    cache = Path(os.environ.get("TEXTBOOK_BABELDOC_CACHE",str(Path(__file__).resolve().parent.parent/".cache"/"babeldoc"))).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    source = source.replace(needle, "CACHE_FOLDER = Path("+repr(str(cache))+")")
    module = types.ModuleType("babeldoc.const")
    module.__file__ = spec.origin
    module.__spec__ = spec
    module.__package__ = "babeldoc"
    sys.modules["babeldoc.const"] = module
    exec(compile(source,spec.origin,"exec"),module.__dict__)


def available() -> tuple[bool, str]:
    try:
        actual = version("babeldoc")
    except PackageNotFoundError:
        return False, "BabelDOC is not installed in this Python environment"
    if actual != PINNED_VERSION:
        return False, f"Expected BabelDOC {PINNED_VERSION}, found {actual}"
    return True, actual


def layout_model_path():
    """Resolve the configured local model without importing upstream or writing."""
    configured = sys.modules.get("babeldoc.const")
    cache = configured.CACHE_FOLDER if configured is not None else Path(os.environ.get("TEXTBOOK_BABELDOC_CACHE", str(Path(__file__).resolve().parent.parent/".cache"/"babeldoc")))
    return Path(os.environ.get("TEXTBOOK_LAYOUT_MODEL", str(cache/"models"/"doclayout_yolo_docstructbench_imgsz1024.onnx")))


def layout_model_identity():
    """Content identity for checkpoint validity; locations do not affect reuse."""
    import hashlib
    from .paragraph_adapter import MODEL_SHA3_256
    path = layout_model_path()
    if not path.is_file():
        return {"present": False, "expected_sha3_256": MODEL_SHA3_256}
    checksum = hashlib.sha3_256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            checksum.update(chunk)
    actual = checksum.hexdigest()
    return {"present": True, "sha3_256": actual, "checksum_verified": actual == MODEL_SHA3_256}


def _box(box, height, offset=(0.0,0.0)):
    """BabelDOC IL uses a bottom-left origin; the book schema is top-left."""
    if box is None:
        return None
    return [float(box.x)-offset[0],height-float(box.y2)-offset[1],float(box.x2)-offset[0],height-float(box.y)-offset[1]]


def extract(pdf_path, outdir, pages, native_lines=None) -> dict:
    """Return real upstream formula/style hints keyed by physical source page.

    A verified local layout model runs LayoutParser and ParagraphFinder on
    upstream's exact normalized input when available. Native glyph ownership,
    vector math and exercise boundaries remain canonical. Missing models produce
    explicit native-geometry fallback diagnostics; no hidden downloads occur.
    StylesAndFormulas provides separately aligned character/formula/style hints.
    """
    ok, detail = available()
    if not ok:
        raise BabelUnavailable(detail)
    _configure_cache(outdir)
    from babeldoc.format.pdf.parse_only import parse_with_new_parser_to_legacy_ir
    from babeldoc.format.pdf.parse_shared import build_parse_only_config
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import StylesAndFormulas
    from babeldoc.format.pdf.document_il.il_version_1 import PdfParagraph, PdfParagraphComposition, PdfLine, Box
    import pymupdf

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    # Upstream preparation scans every xref/page even when only three pages are
    # requested. A lossless page subset keeps each bounded batch fast; physical
    # page provenance is restored explicitly below and saved alongside the IL.
    snippet=outdir/"source-pages.pdf"
    with pymupdf.open(pdf_path) as original,pymupdf.open() as subset:
        originals={}
        for n in pages:
            source=original[n-1]
            if source.rotation:raise BabelUnavailable("Rotated source page needs an explicit parser transform")
            originals[n]={"mediabox":list(source.mediabox),"cropbox":list(source.cropbox),"rect":list(source.rect),
                          "offset":[float(source.cropbox_position.x),float(source.cropbox_position.y)]}
        for n in pages:subset.insert_pdf(original,from_page=n-1,to_page=n-1)
        subset.save(snippet,garbage=4,deflate=True)
    (outdir/"source_page_map.json").write_text(json.dumps({str(i):n for i,n in enumerate(pages)},ensure_ascii=False),encoding="utf-8")
    selection = ",".join(str(p) for p in range(1,len(pages)+1))
    document = parse_with_new_parser_to_legacy_ir(
        snippet, pages=selection, working_dir=outdir / "parse", debug=False
    )
    config = build_parse_only_config(snippet, pages=selection, working_dir=outdir / "styles")
    structure={"pages":{},"diagnostics":[]}
    model=layout_model_path()
    if model.is_file():
        from .paragraph_adapter import extract_paragraph_hints
        prepared=outdir/"parse"/snippet.stem/"input.pdf"
        with pymupdf.open(prepared) as normalized:
            structure=extract_paragraph_hints(document,normalized,config,list(pages),
                {n:{"crop_offset":originals[n]["offset"],"rotation":0} for n in pages},model,outdir/"structure")
    else:structure["diagnostics"]=[{"code":"neural_layout_model_unavailable","detail":"Local pinned layout model is absent; native geometry used without downloading"}]
    pdf = pymupdf.open(snippet)
    hints = {}
    transforms={}
    try:
        worker = StylesAndFormulas(config)
        for page in document.page:
            number = pages[int(page.page_number)]
            media=page.mediabox.box
            height=float(media.y2)-float(media.y)
            offset=originals[number]["offset"]
            transforms[number]={**originals[number],"prepared_height":height,"prepared_mediabox":[float(media.x),float(media.y),float(media.x2),float(media.y2)],"version":"prepared-crop-v2"}
            lines = (native_lines or {}).get(number, [])
            # The upstream parser exposes original glyph/font records. Assign
            # each glyph exactly once to a source line before formula analysis.
            remaining = list(page.pdf_character)
            paragraphs = []
            for line in lines:
                rect = pymupdf.Rect(line["bbox"])
                assigned, rest = [], []
                for char in remaining:
                    visual=getattr(char,"visual_bbox",None)
                    cb=_box(visual.box if visual is not None and visual.box is not None else char.box,height,offset)
                    if cb and rect.contains(pymupdf.Rect(cb).tl + (pymupdf.Rect(cb).br - pymupdf.Rect(cb).tl) * 0.5):
                        assigned.append(char)
                    else:
                        rest.append(char)
                remaining = rest
                if assigned:
                    paragraphs.append(PdfParagraph(
                        box=Box(rect.x0+offset[0],height-rect.y1-offset[1],rect.x1+offset[0],height-rect.y0-offset[1]),
                        pdf_style=assigned[0].pdf_style,
                        pdf_paragraph_composition=[PdfParagraphComposition(pdf_line=PdfLine(pdf_character=assigned))],
                        unicode="".join(c.char_unicode or "" for c in assigned),
                    ))
            page.pdf_paragraph = paragraphs
            # Do not remove unmatched glyphs from the upstream record; coverage
            # remains available for comparison and native ownership accounting.
            worker.process_page(page)
            formulas, styles = [], []
            for paragraph in page.pdf_paragraph:
                for composition in paragraph.pdf_paragraph_composition:
                    formula = composition.pdf_formula
                    if formula is not None:
                        formulas.append({
                            "bbox":list(__import__("functools").reduce(lambda a,b:a|b,[pymupdf.Rect(_box(c.visual_bbox.box if c.visual_bbox and c.visual_bbox.box else c.box,height,offset)) for c in formula.pdf_character])) if formula.pdf_character else _box(formula.box,height,offset),
                            "characters":[{"text":c.char_unicode or "","bbox":_box(c.visual_bbox.box if c.visual_bbox and c.visual_bbox.box else c.box,height,offset)} for c in formula.pdf_character],
                            "text": "".join(c.char_unicode or "" for c in formula.pdf_character),
                            "provenance": f"BabelDOC {PINNED_VERSION} StylesAndFormulas",
                        })
                if paragraph.pdf_style:
                    styles.append({"bbox": _box(paragraph.box, height, offset), "font_id": paragraph.pdf_style.font_id, "font_size": paragraph.pdf_style.font_size})
            hints[number] = {"formulas": formulas, "styles": styles,
                             "glyph_count": len(page.pdf_character),"unassigned_glyphs":len(remaining),"coordinate_transform":transforms[number],"structure":structure.get("pages",{}).get(number,structure.get("pages",{}).get(str(number),{}))}
            page.page_number=number-1
        (outdir/"coordinate_transforms.json").write_text(json.dumps(transforms,ensure_ascii=False,indent=2),encoding="utf-8")
        # Save the actual intermediate representation for audit/reproducibility.
        if is_dataclass(document):
            (outdir / "upstream_il.json").write_text(json.dumps(asdict(document), ensure_ascii=False, default=str), encoding="utf-8")
        return {"backend": "babeldoc-hybrid", "version": PINNED_VERSION, "pages": hints,
                "coordinate_transform_version":"prepared-crop-v2","structure_diagnostics":structure.get("diagnostics",[]),"structure_provenance":{k:v for k,v in structure.items() if k not in {"pages","diagnostics"}},"layout":"native geometry; upstream character parser and formula/style classifier"}
    except Exception as exc:
        raise BabelUnavailable(f"BabelDOC parse/style stage failed: {type(exc).__name__}: {exc}") from exc
    finally:
        pdf.close()


def import_il_json(path, page_heights) -> dict:
    """Import an upstream debug IL export without running translation/typesetting.

    Useful when upstream parser/model execution is isolated from this tool's
    runtime. Coordinates and metadata remain explicitly upstream-derived.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    result = {}
    for page in raw.get("page", []):
        n = int(page["page_number"]) + 1
        height = float(page_heights[n])
        formulas, styles = [], []
        for para in page.get("pdf_paragraph", []):
            for composition in para.get("pdf_paragraph_composition", []):
                formula = composition.get("pdf_formula")
                if not formula or not formula.get("box"):
                    continue
                b = formula["box"]
                formulas.append({"bbox": [b["x"], height-b["y2"], b["x2"], height-b["y"]],
                                 "text": "".join(c.get("char_unicode", "") for c in formula.get("pdf_character", [])),
                                 "provenance": "BabelDOC IL import"})
            if para.get("pdf_style"):
                styles.append(para["pdf_style"])
        result[n] = {"formulas": formulas, "styles": styles, "glyph_count": len(page.get("pdf_character", []))}
    return {"backend": "babeldoc-il-import", "pages": result, "source": str(Path(path).resolve())}
