"""Create an original bilingual PDF demo using only local public contracts.

This is a synthetic compositor demonstration, not an OCR/parser benchmark.
No textbook content, font files, model weights, credentials or API calls.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True

TITLE = "A small counting experiment"
SOURCE = {
    "title": TITLE,
    "intro": "A walking robot leaves one marker at its first stop, two markers at its next stop, and one more at each later stop. We want a quick way to count the markers without drawing every stop.",
    "definition": "Let ⟦F:demo-n⟧ be the number of stops, and let ⟦F:demo-s⟧ be the total marker count. The following rule gives the answer.",
    "reason": "Read the product before dividing: pair the first short row with a reversed copy of the last long row. Every pair has the same length, so the rearranged picture forms a rectangle. Each original picture occupies half of that rectangle.",
    "code-title": "Check the idea with a loop",
    "caption": "Listing 1. A direct count used to check the rule.",
    "conclusion": "The formula and the loop count the same arrangement in different ways. The formula avoids visiting every stop; the loop makes the counting process easy to inspect.",
    "exercise": "Try it: use four stops, and explain why two copies of the pattern always make a rectangle.",
    "note": "This page was written from scratch for a layout demonstration.",
}
ZH = {
    "title": "一个计数小实验",
    "intro": "一个行走机器人在首次停靠时留下一个标记，在下一次停靠时留下两个标记，此后每次都比上次多留一个。我们希望找到一种快速计数的方法，不必把每次停靠都画出来。",
    "definition": "用 ⟦F:demo-n⟧ 表示停靠次数，用 ⟦F:demo-s⟧ 表示标记总数。下面的规则给出答案。",
    "reason": "先看乘积，再做除法：把最短的一行与另一份反向排列图案中最长的一行配对。每一对都一样长，因此重新组合后形成矩形。每份原图恰好占这个矩形的一半。",
    "code-title": "用循环检验这个想法",
    "caption": "代码1：通过直接计数检验规则。",
    "conclusion": "公式和循环用不同的方法计算同一个排列中的标记数。公式无需逐次访问所有停靠点；循环则让计数过程便于检查。",
    "exercise": "试一试：采用四次停靠，解释为什么两份图案总能拼成一个矩形。",
    "note": "本页为排版演示全新编写，未摘录教材。",
}
CODE = "def triangular(stops):\n    total = 0\n    for step in range(1, stops + 1):\n        total += step\n    return total"
W, H = 176 / 25.4 * 72, 250 / 25.4 * 72
LEFT, RIGHT = 44.0, W - 44.0


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def original_pdf(path, fitz):
    """Draw only our own prose, formula strokes and code; core PDF fonts."""
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    blue, ink = (0.04, 0.37, 0.49), (0.10, 0.14, 0.18)
    page.draw_rect(fitz.Rect(0, 0, W, 10), color=None, fill=blue)
    page.insert_text((LEFT, 34), "ORIGINAL SYNTHETIC DEMO / CC0", fontname="helv", fontsize=8, color=blue)
    boxes = {}
    assets = {}
    def block(key, y, size=10.5, font="helv", pad=3):
        text = SOURCE[key]
        width = RIGHT - LEFT
        words = text.split()
        lines, current = [], ""
        for word in words:
            trial = current + (" " if current else "") + word
            if fitz.get_text_length(trial, fontname=font, fontsize=size) > width:
                lines.append(current); current = word
            else: current = trial
        if current: lines.append(current)
        leading = size * 1.42
        for i, line in enumerate(lines):
            page.insert_text((LEFT, y + i * leading), line, fontname=font, fontsize=size, color=ink)
        boxes[key] = [LEFT, y - size * 1.05, RIGHT, y + (len(lines) - 1) * leading + pad]
        return boxes[key][3]
    block("title", 72, 21, "hebo")
    block("intro", 108)
    y = 179
    chunks = [("Let ", "helv", None), ("n", "tiit", "demo-n"),
              (" be the number of stops, and let ", "helv", None),
              ("S(n)", "tiit", "demo-s"), (" be the total marker count.", "helv", None)]
    x = LEFT
    for text, font, aid in chunks:
        size = 10.5; width = fitz.get_text_length(text, fontname=font, fontsize=size)
        page.insert_text((x, y), text, fontname=font, fontsize=size, color=ink)
        if aid: assets[aid] = {"kind": "formula", "bbox": [x - .6, y - 10.4, x + width + .6, y + 2.8], "baseline": 10.4, "text": text}
        x += width
    page.insert_text((LEFT, y + 15), "The following rule gives the answer.", fontname="helv", fontsize=10.5, color=ink)
    boxes["definition"] = [LEFT, y - 11, RIGHT, y + 18]
    # The full fraction stays one immutable vector object.
    fx, fy = 184, 238
    page.insert_text((fx, fy + 10), "S(n) =", fontname="tiit", fontsize=16, color=ink)
    nx = fx + 58
    page.insert_text((nx, fy - 1), "n(n + 1)", fontname="tiit", fontsize=14, color=ink)
    page.draw_line((nx - 2, fy + 5), (nx + 52, fy + 5), color=ink, width=.7)
    page.insert_text((nx + 21, fy + 23), "2", fontname="tiro", fontsize=14, color=ink)
    assets["demo-rule"] = {"kind": "formula", "bbox": [fx - 2, fy - 16, nx + 55, fy + 28], "baseline": 26, "text": "S(n) = n(n + 1) / 2"}
    block("reason", 296)
    block("code-title", 377, 14, "hebo")
    code_box = [LEFT + 10, 395, RIGHT - 10, 486]
    page.draw_rect(fitz.Rect(code_box), color=(.79, .85, .88), fill=(.97, .98, .99), width=.6)
    for i, line in enumerate(CODE.splitlines()):
        page.insert_text((code_box[0] + 12, code_box[1] + 18 + 14 * i), line, fontname="cour", fontsize=9.3, color=ink)
    assets["demo-code"] = {"kind": "code", "bbox": code_box, "baseline": code_box[3] - code_box[1], "text": CODE}
    block("caption", 505, 8.5)
    block("conclusion", 535)
    block("exercise", 600, 9.5)
    block("note", 650, 8.5)
    page.insert_text((W - 87, H - 22), "Demo page 1", fontsize=8, fontname="helv", color=blue)
    doc.set_metadata({"title": TITLE, "author": "Original synthetic demonstration", "subject": "CC0 original demo; no textbook content"})
    doc.save(path, garbage=4, deflate=True)
    doc.close()
    return boxes, assets


def image_only_pdf(native, output, png, fitz):
    with fitz.open(native) as doc:
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(300/72, 300/72), alpha=False)
        pix.save(png)
        raw = pix.tobytes("png")
    with fitz.open() as doc:
        p = doc.new_page(width=W, height=H)
        p.insert_image(p.rect, stream=raw)
        doc.set_metadata({"title": TITLE + " / image-only test", "subject": "Self-authored synthetic raster, not a scanned textbook"})
        doc.save(output, garbage=4, deflate=True)
    with fitz.open(output) as doc:
        assert doc[0].get_text() == "" and not doc.get_toc()


def run_variant(kind, source, out, boxes, originals, fitz, api):
    Asset, Element, Book, Store, compose, verify, file_sha256 = api
    out.mkdir(parents=True, exist_ok=True)
    asset_dir = out / "assets"; asset_dir.mkdir(exist_ok=True)
    assets = {}
    with fitz.open(source) as source_doc:
        for aid, spec in originals.items():
            bbox = fitz.Rect(spec["bbox"])
            # Permanently remove all outside content before importing the crop.
            with fitz.open() as isolated:
                isolated.insert_pdf(source_doc, from_page=0, to_page=0)
                isolated[0].clip_to_rect(bbox)
                with fitz.open() as crop:
                    target = crop.new_page(width=bbox.width, height=bbox.height)
                    target.show_pdf_page(target.rect, isolated, 0, clip=bbox)
                    path = asset_dir / (aid + ".pdf")
                    crop.save(path, garbage=4, deflate=True)
            assets[aid] = Asset(aid, spec["kind"], "assets/" + path.name, 1, list(bbox), bbox.width, bbox.height,
                baseline=spec["baseline"], text=spec["text"], sha256=file_sha256(path),
                metadata={"format": "pdf", "vector": kind == "native", "source_is_scan": kind == "raster",
                          "pdf_page": 0, "baseline_from_top": spec["baseline"], "synthetic_author_bbox": True})
    elements = []
    for key in SOURCE:
        ekind = "heading" if key in {"title", "code-title"} else "caption" if key == "caption" else "exercise" if key == "exercise" else "footnote" if key == "note" else "paragraph"
        role = {"source_is_synthetic": True, "authoring_contract": "direct-book-v1"}
        group = "rule-group" if key == "definition" else "code-group" if key == "caption" else key
        if key == "definition": role.update(role="body_connective", anchor_id="rule")
        if key == "caption": role["anchor_id"] = "code"
        elements.append(Element(key, ekind, SOURCE[key], 1, boxes[key], None, style={"columns":1,"column":0,"heading_level":1 if key=="title" else 2}, group_id=group, metadata=role))
        if key == "definition":
            elements.append(Element("rule", "formula", "", 1, originals["demo-rule"]["bbox"], None, asset_ids=["demo-rule"], group_id="rule-group"))
        if key == "code-title":
            elements.append(Element("code", "code", "", 1, originals["demo-code"]["bbox"], None, asset_ids=["demo-code"], group_id="code-group"))
    book = Book(str(source.resolve()), file_sha256(source), TITLE + " (original synthetic " + kind + " demo)", 1, [1],
        pages=[{"number":1,"width":W,"height":H,"printed_page":None,"label_source":"physical-fallback","columns":1,
                "parser_backend":"direct-synthetic-authoring-contract","element_ids":[e.id for e in elements],
                "coverage":{"basis":"synthetic-authoring-manifest; not parser/OCR coverage"}}],
        elements=elements, assets=assets,
        profile={"body_size":10.5,"exercise_size":9.5,"caption_size":8.5,"heading_color":"135c72",
                 "body_font":"Noto Serif SC","heading_font":"Noto Sans SC","columns":1,
                 "page_width":W,"page_height":H,"job_dir":str(out.resolve()),
                 "synthetic_demo":True,"source_mode":"synthetic-authoring-manifest"})
    book.save(out / "book.json")
    with Store(out / "tasks.db") as store:
        store.register(book, {"marker":"标记","stop":"停靠点"})
        for eid, text in ZH.items():
            result = store.put_candidate(eid, text, "original-bilingual-demo-author")
            if not result["accepted"]: raise RuntimeError(result)
        accepted = store.translations()
        assert accepted == ZH
        assert store.db.execute("SELECT COUNT(*) FROM usage").fetchone()[0] == 0
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    dump(out / "translations.json", accepted)
    result = compose(book, accepted, out, output_path=out / ("counting-" + kind + "-zh.pdf"))
    report = verify(book, accepted, out, pdf_path=result["pdf_path"], layout_map=result["layout_map_path"])
    with fitz.open(result["pdf_path"]) as pdf:
        font_names = sorted({f[3] for p in pdf for f in p.get_fonts()})
        sizes = sorted({round(s["size"],2) for p in pdf for b in p.get_text("dict")["blocks"] for l in b.get("lines",[]) for s in l["spans"] if any("\u4e00"<=c<="\u9fff" for c in s["text"])})
        for i, page in enumerate(pdf,1):
            page.get_pixmap(matrix=fitz.Matrix(1.6,1.6),alpha=False).save(out / f"preview-{i:03}.png")
    return {"variant":kind,"source_pdf":str(source.resolve()),"source_sha256":file_sha256(source),
            "output_pdf":result["pdf_path"],"output_sha256":file_sha256(result["pdf_path"]),
            "output_pages":result["page_count"],"source_elements":len(elements),"accepted_authored_translations":len(accepted),
            "protected_assets":len(assets),"qa_errors":report["errors"],"qa_warnings":report["warnings"],
            "asset_pixel_summary":report["visual_asset_checks"],"embedded_font_names":font_names,
            "observed_chinese_font_sizes_pt":sizes,"external_api_calls":0,"OCR_or_layout_model_calls":0}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--implementation",type=Path,default=Path(__file__).resolve().parents[2])
    args=parser.parse_args()
    sys.path.insert(0,str(args.implementation.resolve()))
    import pymupdf as fitz
    from textbook_pdf.models import Asset,Element,Book,file_sha256
    from textbook_pdf.store import Store
    from textbook_pdf.layout import compose
    from textbook_pdf.qa import verify
    base=Path(__file__).resolve().parent
    generated=base/"generated";generated.mkdir(exist_ok=True)
    start=time.perf_counter()
    native=generated/"original-counting-en.pdf"
    boxes,spec=original_pdf(native,fitz)
    raster=generated/"original-counting-image-only.pdf"
    image_only_pdf(native,raster,generated/"original-counting-raster-300dpi.png",fitz)
    with fitz.open(native) as doc:doc[0].get_pixmap(matrix=fitz.Matrix(1.6,1.6),alpha=False).save(generated/"source-preview.png")
    api=(Asset,Element,Book,Store,compose,verify,file_sha256)
    variants=[run_variant(kind,source,generated/kind,boxes,spec,fitz,api)for kind,source in [("native",native),("raster",raster)]]
    dump(base/"original_content.json",{"license":"CC0-1.0","origin":"new original synthetic demo; no textbook source", "english":SOURCE,"chinese":ZH,"preserved_code":CODE,"formula_asset_specs":spec})
    report={"scope":"Synthetic Book/Store-to-compositor demo; not an end-to-end extraction/OCR/translation benchmark",
            "content_license":"CC0-1.0; original text and simple mathematical drawing only",
            "generated_font_files":[],"model_weights_copied":[],"textbook_content_copied":False,
            "body_font":"Noto Serif SC (locally installed, not copied)","heading_font":"Noto Sans SC (locally installed, not copied)",
            "PDF_font_subsets":"PDFs may embed needed open-font glyph subsets; no standalone font files are included.",
            "external_api_calls":0,"OCR_or_layout_model_calls":0,"variants":variants,
            "elapsed_seconds":round(time.perf_counter()-start,3),"visual_review":"pending_direct_review_of_all_previews"}
    dump(generated/"demo_report.json",report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 2 if any(v["qa_errors"] for v in variants) else 0

if __name__=="__main__":raise SystemExit(main())
