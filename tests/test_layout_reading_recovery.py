"""Real compositor regressions for partial-book reading drafts."""
from pathlib import Path
import json
import shutil
import uuid
import unittest

import pymupdf as fitz

from textbook_pdf import layout
from textbook_pdf.extraction import _asset, _collect, _detach_figure_labels, _finalize_assets
from textbook_pdf.models import Book, Element, file_sha256


class ReadingRecoveryTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[2] / "work" / "quality-repair-20261028" / "layout-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.directory = base / uuid.uuid4().hex
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)

    def test_untranslated_tight_label_keeps_native_ink_and_other_translation(self):
        directory = self.directory
        (directory / "assets").mkdir()
        with fitz.open() as source:
            page = source.new_page(width=300, height=240)
            page.insert_text((60, 80), "Chapter 1", fontsize=8, color=(0, 174/255, 239/255))
            page.draw_line((60, 100), (200, 100))
            source.save(directory / "source.pdf")
            lines, glyphs = _collect(page)
            book = Book(str(directory / "source.pdf"), file_sha256(directory / "source.pdf"),
                        "Original figure label", 1, [1],
                        pages=[{"number": 1, "width": 300, "height": 240}],
                        profile={"job_dir": str(directory), "page_width": 300, "page_height": 300})
            book._active_glyphs = glyphs
            original = _asset(book, source, page, directory, "figure", [50, 65, 210, 110])
            parent = Element("diagram", "figure", "", 1, [50,65,210,110], asset_ids=[original.id],
                             metadata={"glyph_ids": [g["id"] for g in glyphs]})
            for glyph in glyphs:
                glyph.update(owner=parent.id, disposition="preserved-original")
            elements = [parent]
            _detach_figure_labels(book, source, page, directory, elements, lines, glyphs)
            book.elements = elements + [Element("body", "paragraph", "An original explanation.", 1, [50,120,250,150])]
            _finalize_assets(book, 1, directory)
        before = json.dumps(book.elements, default=lambda obj: obj.__dict__, sort_keys=True)
        result = layout.compose(book, {"body": "这段中文仍须保留。"}, directory / "rendered", allow_incomplete=True)
        mapping = json.loads(Path(result["layout_map_path"]).read_text(encoding="utf8"))
        retained = mapping.get("native_retained_labels", [])
        self.assertEqual(len(retained), 1, result["issues"])
        label = next(e for e in book.elements if e.kind == "figure_label")
        self.assertEqual(retained[0]["element_id"], label.id)
        self.assertEqual(retained[0]["source_pdf_sha256"], book.source_sha256)
        self.assertEqual(retained[0]["reason"], "untranslated_preview")
        self.assertEqual(retained[0]["precision"], "measured-native-label")
        self.assertEqual(result["untranslated_count"], 1)
        self.assertFalse(any(i["code"] in {"missing_layout_trace", "asset_out_of_page"} for i in result["issues"]))
        with fitz.open(result["pdf_path"]) as pdf:
            text = "".join(page.get_text() for page in pdf)
            self.assertIn("Chapter 1", text)
            self.assertIn("这段中文仍须保留", text)
            native = [s for p in pdf for b in p.get_text("dict")["blocks"] for line in b.get("lines", [])
                      for s in line["spans"] if "Chapter 1" in s["text"]]
            self.assertEqual(len(native), 1)
            self.assertAlmostEqual(native[0]["size"], 8, places=3)
            self.assertEqual(native[0]["color"], 0x00AEEF)
        self.assertEqual(json.dumps(book.elements, default=lambda obj: obj.__dict__, sort_keys=True), before)

    def test_explicit_next_caption_is_not_stolen_by_previous_table(self):
        first = Element("first", "table", "", 1, [100,100,200,150], group_id="first")
        caption = Element("caption", "caption", "Next table", 1, [100,160,200,170],
                          group_id="second", metadata={"anchor_id": "second"})
        second = Element("second", "table", "", 1, [100,175,200,230], group_id="second",
                         metadata={"caption_id": "caption"})
        book = Book("self-authored.pdf", "source", "Two tables", 1, [1], elements=[first,caption,second])
        texts = {e.id: e.text for e in book.elements}
        groups = layout._associated_groups(book.elements, texts, book, layout._page_geometry(book),
                                           {**book.profile, **layout._FONT_SIZES})
        membership = [[e.id for e in g] for g, *_ in groups]
        self.assertEqual(membership, [["first"], ["caption", "second"]])

    def test_native_label_subtracts_only_its_own_shared_formula_occurrence(self):
        from textbook_pdf.models import Asset
        asset = Asset("formula", "formula", "f.pdf", 1, [60,70,70,80], 10, 10)
        label = Element("label", "figure_label", "Unknown ⟦F:formula⟧", 1, [50,60,120,90], asset_ids=["formula"])
        body = Element("body", "paragraph", "Again ⟦F:formula⟧", 1, [50,100,300,120], asset_ids=["formula"])
        book = Book("self-authored.pdf", "source", "Shared source asset", 1, [1], elements=[label,body], assets={"formula":asset})
        retained = {"element_id":"label", "parent_id":"figure", "source_page":1,
                    "covered_asset_ids":["formula"], "covered_asset_occurrences":{"formula":1}}
        traces = [{"type":"native-retained-label", "retained":retained,"page":1,"x":50,"y":60,"width":70,"height":30}]
        for eid, y in [("label",60),("body",100)]:
            for phase, dy in [("start",0),("end",10)]:
                traces.append({"type":"element","id":eid,"phase":phase,"page":1,"x":50,"y":y+dy,"width":250})
        issues=[]
        mapping=layout._layout_map(book,traces,layout._page_geometry(book),self.directory/"not-created.pdf",issues)
        self.assertTrue(any(i["code"]=="missing_asset_trace" for i in issues))
        traces.append({"type":"asset","id":"formula","element_id":"body","page":1,"x":80,"y":100,"width":10,"height":10})
        issues=[]
        mapping=layout._layout_map(book,traces,layout._page_geometry(book),self.directory/"not-created.pdf",issues)
        self.assertFalse(any(i["code"]=="missing_asset_trace" for i in issues))
        self.assertEqual(len(mapping["assets"]["formula"]),1)
        self.assertEqual(mapping["assets"]["formula"][0]["element_id"],"body")
        self.assertEqual(len(mapping["assets_retained_inside_native_labels"]["formula"]),1)

    def test_preview_warning_only_describes_current_output_sources(self):
        first = Element("one", "paragraph", "First original paragraph.", 1, [48,80,540,100])
        third = Element("three", "paragraph", "A separate original page.", 3, [48,80,540,100])
        book = Book("self-authored.pdf", "source", "Scoped warnings", 3, [1,3],
                    pages=[{"number":n,"width":612,"height":792} for n in [1,3]],
                    elements=[first,third], issues=[{"code":"private_source_code_"+str(n),
                        "page":1,"element_id":"one","severity":"error"} for n in range(4)])
        result=layout.compose(book,{"one":"第一页的完整译文。","three":"第三页的完整译文。"},
                              self.directory/"rendered",allow_incomplete=True)
        with fitz.open(result["pdf_path"]) as pdf:
            self.assertEqual(len(pdf),2)
            self.assertIn("4",pdf[0].get_text())
            self.assertIn("来源待核",pdf[0].get_text())
            self.assertNotIn("来源待核",pdf[1].get_text())
            self.assertNotIn("private_source_code", "".join(page.get_text() for page in pdf))
            self.assertNotIn("未译原文",pdf[0].get_text())
        mapping=json.loads(Path(result["layout_map_path"]).read_text(encoding="utf8"))
        self.assertEqual(len(mapping["source_errors"]),4)
        self.assertEqual(mapping["geometry"]["top"],45)

    def test_nonadjacent_explicit_captions_follow_their_real_graphics(self):
        a=Element("a","figure","",1,[50,50,150,100],metadata={"caption_id":"caption-a"})
        b=Element("b","figure","",1,[200,50,300,100],metadata={"caption_id":"caption-b"})
        prose=Element("prose","paragraph","Unrelated body",1,[50,150,300,180])
        ca=Element("caption-a","caption","Figure A",1,[50,105,150,120],metadata={"anchor_id":"a"})
        cb=Element("caption-b","caption","Figure B",1,[200,105,300,120],metadata={"anchor_id":"b"})
        original=[a,b,prose,cb,ca]
        before=json.dumps(original,default=lambda obj:obj.__dict__,sort_keys=True)
        book=Book("self-authored.pdf","source","Explicit anchors",1,[1],elements=original)
        groups=layout._associated_groups(original,{e.id:e.text for e in original},book,
                                         layout._page_geometry(book),{**book.profile,**layout._FONT_SIZES})
        self.assertEqual([[e.id for e in group]for group,*_ in groups],[["a","caption-a"],["b","caption-b"],["prose"]])
        self.assertEqual(json.dumps(original,default=lambda obj:obj.__dict__,sort_keys=True),before)

    def test_starred_exercise_keeps_its_attached_native_marker(self):
        from textbook_pdf.models import Asset
        asset=Asset("mark","figure","mark.pdf",1,[30,80,40,90],10,10)
        marker=Element("marker","figure","",1,[30,80,40,90],asset_ids=["mark"],
                       metadata={"role":"exercise_marker","anchor_id":"exercise"})
        exercise=Element("exercise","exercise","∗37. Prove the statement.",1,[45,80,280,130])
        book=Book("self-authored.pdf","source","Starred exercise",1,[1],elements=[marker,exercise],assets={"mark":asset})
        markup=layout._element_markup(exercise,exercise.text,book,{"mark":"mark.pdf"},layout._page_geometry(book),
            250,{**book.profile,**layout._FONT_SIZES},[],markers=[marker])
        self.assertIn('#protected("mark", "marker"',markup)
        self.assertIn('"∗37."',markup)

    def test_two_wide_native_formulas_wrap_instead_of_running_off_page(self):
        from textbook_pdf.models import Asset
        source=self.directory/"formula.pdf"
        with fitz.open() as pdf:
            page=pdf.new_page(width=220,height=18)
            page.insert_text((3,12),"a + b + c = d",fontsize=10)
            pdf.save(source)
        asset=Asset("wide","formula",str(source),1,[0,0,220,18],220,18,baseline=12,sha256=file_sha256(source))
        exercise=Element("exercise","exercise","25. ⟦F:wide⟧ ⟦F:wide⟧",1,[48,80,282,120],
                         style={"columns":2},asset_ids=["wide"])
        book=Book("self-authored.pdf","source","Wide exercise",1,[1],elements=[exercise],assets={"wide":asset})
        result=layout.compose(book,{"exercise":exercise.text},self.directory/"rendered",allow_incomplete=True)
        self.assertFalse(any(i["code"]=="asset_out_of_page" for i in result["issues"]),result["issues"])
        mapping=json.loads(Path(result["layout_map_path"]).read_text(encoding="utf8"))
        rectangles=[r["bbox"] for r in mapping["assets"]["wide"]]
        self.assertEqual(len(rectangles),2)
        self.assertGreater(rectangles[1][1],rectangles[0][1]+10)
        self.assertTrue(all(abs((r[2]-r[0])-220)<.001 for r in rectangles))

    def test_near_full_width_formula_breaks_after_chinese_connective(self):
        from textbook_pdf.models import Asset
        assets={}
        for name,width,height in [("first",45.1637,10.8662),("second",134.875,14.5),("long",394.8186,26.3804)]:
            path=self.directory/(name+".pdf")
            with fitz.open() as pdf:
                page=pdf.new_page(width=width,height=height)
                page.insert_text((1,8),"x = 1",fontsize=8)
                pdf.save(path)
            assets[name]=Asset(name,"formula",str(path),1,[145.5941,100,145.5941+width,100+height],width,height,
                               baseline=height-2.6,sha256=file_sha256(path))
        text="假设 ⟦F:first⟧。于是有 ⟦F:second⟧ 和 ⟦F:long⟧。"
        paragraph=Element("body","paragraph",text,1,[145.5941,80,540.6138,130],asset_ids=list(assets))
        book=Book("self-authored.pdf","source","Wide paragraph",1,[1],
                  pages=[{"number":1,"width":612.197,"height":756.22}],elements=[paragraph],assets=assets)
        result=layout.compose(book,{"body":text},self.directory/"rendered",allow_incomplete=True)
        self.assertFalse(any(i["code"]=="asset_out_of_page" for i in result["issues"]),result["issues"])

    def test_active_cell_of_suppressed_table_is_rendered_once(self):
        parent=Element("table","table","",1,[48,80,400,180],metadata={
            "render_suppressed":True,"superseded_by":"other","table_grid":{"rows":[["cell"]]}})
        cell=Element("cell","table_cell","A preserved original cell",1,[48,80,400,180],
                     metadata={"parent_table_id":"table","literal":True,"translatable":False})
        other=Element("other","paragraph","Another source paragraph.",1,[48,190,400,220])
        book=Book("self-authored.pdf","source","Independent live cell",1,[1],elements=[parent,cell,other])
        result=layout.compose(book,{"other":"另一段完整译文。"},self.directory/"rendered",allow_incomplete=True)
        self.assertFalse(any(i["code"]=="missing_layout_trace" for i in result["issues"]),result["issues"])
        with fitz.open(result["pdf_path"])as pdf:
            text="".join(page.get_text()for page in pdf)
            self.assertEqual(text.count("A preserved original cell"),1)

    def test_disjoint_native_math_fragments_keep_their_source_positions(self):
        from textbook_pdf.models import Asset
        assets={};elements=[]
        for name,bbox,kind in [("left",[145,100,185,112],"formula"),
                                ("right",[200,100,240,112],"formula"),
                                ("lower",[180,115,205,123],"footnote")]:
            width,height=bbox[2]-bbox[0],bbox[3]-bbox[1]
            path=self.directory/(name+".pdf")
            with fitz.open()as pdf:
                page=pdf.new_page(width=width,height=height)
                page.insert_text((1,height-2),name,fontsize=6)
                pdf.save(path)
            assets[name]=Asset(name,"formula",str(path),1,bbox,width,height,baseline=height-2,sha256=file_sha256(path))
            elements.append(Element("e-"+name,kind,"⟦F:"+name+"⟧",1,bbox,asset_ids=[name]))
        book=Book("self-authored.pdf","source","Native display",1,[1],elements=elements,assets=assets)
        result=layout.compose(book,{},self.directory/"rendered",allow_incomplete=True)
        mapping=json.loads(Path(result["layout_map_path"]).read_text(encoding="utf8"))
        boxes={name:mapping["assets"][name][0]["bbox"]for name in assets}
        self.assertAlmostEqual(boxes["right"][0]-boxes["left"][0],55,places=3)
        self.assertAlmostEqual(boxes["right"][1]-boxes["left"][1],0,places=3)
        self.assertAlmostEqual(boxes["lower"][0]-boxes["left"][0],35,places=3)
        self.assertAlmostEqual(boxes["lower"][1]-boxes["left"][1],15,places=3)
        for name,asset in assets.items():
            self.assertAlmostEqual(boxes[name][2]-boxes[name][0],asset.width,places=3)
            self.assertAlmostEqual(boxes[name][3]-boxes[name][1],asset.height,places=3)
        self.assertFalse(any(i["code"]in {"missing_asset_trace","missing_layout_trace","asset_out_of_page"}for i in result["issues"]))

    def test_translated_label_may_use_only_proved_blank_background_to_its_right(self):
        from textbook_pdf.models import Asset
        background_path=self.directory/"background.pdf"
        with fitz.open()as pdf:
            page=pdf.new_page(width=160,height=70)
            page.draw_line((55,10),(55,30),color=(0,0,0))
            pdf.save(background_path)
        background=Asset("background","figure",str(background_path),1,[30,50,190,120],160,70,
                         sha256=file_sha256(background_path),metadata={"detector":"translated-figure-background"})
        parent=Element("figure","figure","",1,[30,50,190,120],asset_ids=["background"],metadata={"figure_label_ids":["label"]})
        child=Element("label","figure_label","Step 1",1,[40,62,60,70],style={"color":"00aeef"},
                      metadata={"parent_figure_id":"figure"})
        book=Book("self-authored.pdf","source","Safe label growth",1,[1],elements=[parent,child],assets={"background":background})
        result=layout.compose(book,{"label":"第 1 步"},self.directory/"rendered",allow_incomplete=True)
        mapping=json.loads(Path(result["layout_map_path"]).read_text(encoding="utf8"))
        proof=mapping.get("figure_label_blank_expansions",[])
        self.assertEqual(len(proof),1)
        self.assertGreater(proof[0]["safe_width_pt"],20)
        self.assertLess(proof[0]["safe_width_pt"],45)
        self.assertEqual(proof[0]["background_sha256"],background.sha256)
        with fitz.open(result["pdf_path"])as pdf:
            spans=[span for page in pdf for block in page.get_text("dict")["blocks"]
                   for line in block.get("lines",[])for span in line["spans"]if "步" in span["text"]]
            self.assertTrue(spans)
            self.assertTrue(all(abs(s["size"]-8.5)<.001 for s in spans))

    def test_blank_expansion_stops_at_nonlabel_ink_and_other_label_bounds(self):
        from textbook_pdf.models import Asset
        path=self.directory/"background.pdf"
        with fitz.open()as pdf:
            page=pdf.new_page(width=160,height=70)
            page.draw_line((31,10),(31,30),color=(0,0,0),width=1)
            pdf.save(path)
        background=Asset("bg","figure",str(path),1,[30,50,190,120],160,70,sha256=file_sha256(path))
        child=Element("one","figure_label","Step 1",1,[40,62,60,70])
        paths={"bg":str(path),"__output_root__":self.directory}
        profile={"caption_size":8.5}
        width,proof=layout._blank_label_width(child,background,[child],paths,10,11,20,11.9,profile)
        self.assertLessEqual(width,20.5)
        # The original neighboring text is absent from a detached background;
        # its source frame still limits expansion even when pixels are blank.
        with fitz.open()as pdf:
            pdf.new_page(width=160,height=70)
            pdf.save(self.directory/"blank.pdf")
        background.path=str(self.directory/"blank.pdf")
        paths={"bg":background.path,"__output_root__":self.directory}
        other=Element("two","figure_label","Other",1,[65,62,80,70])
        width,proof=layout._blank_label_width(child,background,[child,other],paths,10,11,20,11.9,profile)
        self.assertLessEqual(10+width,34.5)

    def test_spatial_punctuation_uses_its_real_native_source_position(self):
        from textbook_pdf.models import Asset
        source_path=self.directory/"source.pdf"
        with fitz.open()as pdf:
            page=pdf.new_page(width=300,height=200)
            page.insert_text((170,100),".",fontsize=10)
            _,glyphs=_collect(page)
            punctuation=next(g for g in glyphs if g["c"]==".")
            pdf.save(source_path)
        assets={}
        for aid,bbox in [("math",[100,80,130,90]),("lower",[150,112,160,120])]:
            path=self.directory/(aid+".pdf")
            with fitz.open()as pdf:
                page=pdf.new_page(width=bbox[2]-bbox[0],height=bbox[3]-bbox[1])
                page.insert_text((1,7),"x",fontsize=6)
                pdf.save(path)
            assets[aid]=Asset(aid,"formula",str(path),1,bbox,bbox[2]-bbox[0],bbox[3]-bbox[1],sha256=file_sha256(path))
        first=Element("first","formula","⟦F:math⟧.",1,[100,80,210,110],asset_ids=["math"],
                      metadata={"glyph_ids":[punctuation["id"]]})
        lower=Element("lower","footnote","⟦F:lower⟧",1,[150,112,160,120],asset_ids=["lower"],metadata={"translatable":False})
        book=Book(str(source_path),file_sha256(source_path),"Native punctuation",1,[1],elements=[first,lower],assets=assets)
        result=layout.compose(book,{},self.directory/"rendered",allow_incomplete=True)
        mapping=json.loads(Path(result["layout_map_path"]).read_text(encoding="utf8"))
        with fitz.open(result["pdf_path"])as pdf:
            spans=[s for page in pdf for b in page.get_text("dict")["blocks"]for line in b.get("lines",[])for s in line["spans"]if s["text"]=="."]
            self.assertEqual(len(spans),1)
            math_box=mapping["assets"]["math"][0]["bbox"]
            self.assertAlmostEqual(spans[0]["origin"][0]-math_box[0],70,places=3)
            self.assertAlmostEqual(spans[0]["origin"][1]-math_box[1],20,places=3)
        self.assertEqual(len(mapping.get("native_spatial_punctuation",[])),1)

    def test_formula_lead_stays_with_formula_across_inset_wrapper(self):
        from textbook_pdf.models import Asset
        assets = {}
        for aid, width, height in [("equation", 75, 19), ("spacer", 100, 175)]:
            path = self.directory / (aid + ".pdf")
            with fitz.open() as pdf:
                page = pdf.new_page(width=width, height=height)
                page.insert_text((1, 13), "x = y + 1" if aid == "equation" else "Earlier diagram", fontsize=10)
                pdf.save(path)
            assets[aid] = Asset(aid, "formula" if aid == "equation" else "figure", str(path), 1,
                [60, 50, 60 + width, 50 + height], width, height, sha256=file_sha256(path))
        elements = [Element("filler", "figure", "⟦F:spacer⟧", 1, [60, 50, 160, 225], asset_ids=["spacer"]),
                    Element("lead", "paragraph", "We obtain", 1, [60, 230, 250, 241]),
                    Element("formula", "formula", "⟦F:equation⟧", 1, [60, 250, 135, 269], asset_ids=["equation"])]
        book = Book("", "synthetic", "Formula lead", 1, [1], elements=elements, assets=assets,
                    pages=[{"number": 1, "width": 300, "height": 300}],
                    profile={"page_width": 300, "page_height": 300})
        result = layout.compose(book, {"lead": "我们得到"}, self.directory / "lead-render", allow_incomplete=True)
        mapping = json.loads(Path(result["layout_map_path"]).read_text(encoding="utf8"))
        with fitz.open(result["pdf_path"]) as pdf:
            lead_pages = [index + 1 for index, page in enumerate(pdf) if "我们得到" in page.get_text()]
        self.assertEqual(lead_pages, [mapping["assets"]["equation"][0]["page"]])

    def test_wrapped_native_display_does_not_stretch_short_chinese_lead(self):
        from textbook_pdf.models import Asset
        path = self.directory / "wrapped.pdf"
        with fitz.open() as pdf:
            page = pdf.new_page(width=150, height=24)
            page.insert_text((110, 9), "x +", fontsize=8)
            page.insert_text((1, 21), "y = z", fontsize=8)
            pdf.save(path)
        asset = Asset("wrapped", "formula", str(path), 1, [60, 60, 210, 84], 150, 24,
                      sha256=file_sha256(path), metadata={"detector": "whole-native-formula-chain",
                          "source_segments": [{"asset_id": "left"}, {"asset_id": "right"}]})
        element = Element("body", "paragraph", "We consider ⟦F:wrapped⟧ and continue.", 1,
                          [60, 50, 250, 100], asset_ids=["wrapped"])
        book = Book("", "synthetic", "Chinese lead", 1, [1], elements=[element], assets={"wrapped": asset},
                    pages=[{"number":1,"width":300,"height":300}], profile={"page_width": 300, "page_height": 300})
        result = layout.compose(book, {"body": "我们考虑这个关系 ⟦F:wrapped⟧，并继续。"},
                                self.directory / "wrapped-render", allow_incomplete=True)
        with fitz.open(result["pdf_path"]) as pdf:
            chars = [c for p in pdf for block in p.get_text("rawdict")["blocks"]
                     for line in block.get("lines", []) for s in line["spans"] for c in s["chars"] if c["c"].strip()]
            first = next(c for c in chars if c["c"] == "我")
            row = sorted((c for c in chars if abs(c["origin"][1] - first["origin"][1]) < .01
                          and c["origin"][0] >= first["origin"][0] - .01), key=lambda c: c["origin"][0])
            self.assertEqual(''.join(c["c"] for c in row[:8]), "我们考虑这个关系")
            gaps = [b["origin"][0] - a["origin"][0] for a, b in zip(row[:7], row[1:8])]
            self.assertLessEqual(max(gaps), 11.5)

    def test_short_inbody_sidebar_with_same_source_structure_is_prose(self):
        metadata = {"structure_id": "source-unit", "source_block": 2}
        paragraph = Element("lead", "paragraph", "[Hint: The number", 1, [60, 50, 250, 62],
                            metadata={**metadata, "source_line": 0})
        tail = Element("tail", "sidebar", "is odd.]", 1, [60, 64, 105, 76],
                       metadata={**metadata, "source_line": 1})
        book = Book("", "synthetic", "In-body continuation", 1, [1], elements=[paragraph, tail],
                    profile={"page_width": 300, "page_height": 300})
        result = layout.compose(book, {"lead": "[提示：这个数", "tail": "为奇数。]"},
                                self.directory / "tail-render", allow_incomplete=True)
        with fitz.open(result["pdf_path"]) as pdf:
            spans = [s for p in pdf for block in p.get_text("dict")["blocks"]
                     for line in block.get("lines", []) for s in line["spans"]]
            a = next(s for s in spans if "这个数" in s["text"])
            b = next(s for s in spans if "为奇数" in s["text"])
            self.assertAlmostEqual(a["origin"][1], b["origin"][1], places=3)
        self.assertEqual(tail.kind, "sidebar")
        self.assertNotIn("inline_group", tail.metadata)


if __name__ == "__main__":
    unittest.main()
