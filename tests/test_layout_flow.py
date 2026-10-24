"""Regression checks for margin anchoring and measured exercise balance."""
from pathlib import Path
import shutil
import unittest
import uuid
from textbook_pdf import layout
from textbook_pdf.models import Book, Element, Asset, file_sha256
import json
import pymupdf

class LayoutFlowTests(unittest.TestCase):
    geometry = {"width": 612, "height": 792, "left": 48, "right": 48, "top": 45, "bottom": 45, "gutter": 18}

    def test_actual_preview_source_warning_summary_keeps_readable_font(self):
        try:
            layout._Runtime.find()
        except layout.CompositionError:
            self.skipTest("Pinned Typst runtime is optional")
        directory = Path(__file__).resolve().parent / "_tmp_layout_flow" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        try:
            body = Element("body", "paragraph", "A complete paragraph.", 1, [48, 80, 540, 110])
            book = Book("self-authored.pdf", "self-authored", "Source warning fixture", 1, [1],
                        pages=[{"number": 1, "width": 612, "height": 792, "printed_page": "1"}],
                        elements=[body], issues=[{"code": "source_hold_" + str(n), "severity": "error",
                            "page": 1, "element_id": "body"} for n in range(4)])
            result = layout.compose(book, {"body": "这是一段完整的正文。"}, directory / "rendered", allow_incomplete=True)
            with pymupdf.open(result["pdf_path"]) as doc:
                spans = [span for page in doc for block in page.get_text("dict")["blocks"]
                         for line in block.get("lines", []) for span in line["spans"]]
                warning = [span for span in spans if "另有" in span["text"]]
                self.assertTrue(warning)
                self.assertTrue(all(abs(span["size"] - 8.5) < .01 for span in warning))
                body_spans = [span for span in spans if "完整的正文" in span["text"]]
                self.assertTrue(body_spans)
                self.assertTrue(all(abs(span["size"] - 10.5) < .01 for span in body_spans))
        finally:
            self.assertTrue(directory.resolve().is_relative_to((Path(__file__).resolve().parent / "_tmp_layout_flow").resolve()))
            shutil.rmtree(directory)

    def test_actual_caption_is_centered_over_its_narrow_table_frame(self):
        try:
            layout._Runtime.find()
        except layout.CompositionError:
            self.skipTest("Pinned Typst runtime is optional")
        directory = Path(__file__).resolve().parent / "_tmp_layout_flow" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        try:
            caption = Element("caption", "caption", "Table title", 1, [155, 80, 340, 92],
                              group_id="table", metadata={"anchor_id": "table"})
            table = Element("table", "table", "", 1, [145, 95, 395, 160], group_id="table",
                            metadata={"caption_id": "caption", "table_grid": {
                                "rows": [["c1", "c2"], ["c3", "c4"]], "header_rows": 1,
                                "col_widths": [125, 125]}})
            cells = [Element("c" + str(n), "table_cell", "Cell", 1, [145, 95, 270, 110])
                     for n in range(1, 5)]
            book = Book("self-authored.pdf", "self-authored", "Table frame", 1, [1],
                        pages=[{"number": 1, "width": 612, "height": 792, "printed_page": "1"}],
                        elements=[caption, table] + cells)
            translations = {"caption": "表格标题", **{c.id: "单元格" for c in cells}}
            before = json.dumps([e.__dict__ for e in book.elements], sort_keys=True)
            result = layout.compose(book, translations, directory / "rendered")
            with pymupdf.open(result["pdf_path"]) as doc:
                self.assertEqual(len(doc), 1)
                page = doc[0]
                rect = page.search_for("表格标题")[0]
                # Read the actual drawn table envelope, not just the planner's estimate.
                rules = [d["rect"] for d in page.get_drawings() if d.get("type") in {"s", "fs"}]
                self.assertTrue(rules)
                left, right = min(r.x0 for r in rules), max(r.x1 for r in rules)
                self.assertAlmostEqual((rect.x0 + rect.x1) / 2, (left + right) / 2, places=2)
                # Table rules protrude by half a stroke outside the cell frame.
                self.assertLess(abs(left - 145), .3)
                self.assertLess(abs(right - left - 250), .6)
                sizes = [s["size"] for b in page.get_text("dict")["blocks"]
                         for line in b.get("lines", []) for s in line["spans"] if "单元格" in s["text"]]
                self.assertTrue(sizes)
                self.assertTrue(all(abs(size - 10.5) < .01 for size in sizes))
            self.assertEqual(json.dumps([e.__dict__ for e in book.elements], sort_keys=True), before)
        finally:
            self.assertTrue(directory.resolve().is_relative_to((Path(__file__).resolve().parent / "_tmp_layout_flow").resolve()))
            shutil.rmtree(directory)

    def test_related_label_and_resource_share_anchor_without_mutating_source(self):
        label = Element("label", "sidebar", "EXAMPLE 8", 1, [70,100,133,112])
        body = Element("body", "paragraph", "Prove a proposition.", 1, [145,100,540,136])
        pointer = Element("pointer", "figure", "", 1, [123,117,134,138], metadata={"role":"resource_pointer"})
        extra = Element("extra", "sidebar", "Extra", 1, [81,118,105,127])
        examples = Element("examples", "sidebar", "Examples", 1, [81,129,121,138])
        items = [label,body,pointer,extra,examples]
        before = [dict(e.metadata) for e in items]
        notes, attached = layout._margin_attachments(items,self.geometry)
        self.assertEqual(set(notes),{"body"})
        self.assertEqual(attached,{"label","pointer","extra","examples"})
        self.assertEqual([role for role,_ in notes["body"]],["label","resource"])
        self.assertEqual([dict(e.metadata) for e in items],before)

    def test_resource_words_misclassified_as_footnotes_stay_with_pointer(self):
        body=Element("b","paragraph","A related statement.",1,[145,100,540,140])
        pointer=Element("p","figure","",1,[123,110,134,131],metadata={"role":"resource_pointer"})
        note=Element("n","footnote","Examples",1,[81,120,121,129])
        notes,attached=layout._margin_attachments([body,pointer,note],self.geometry)
        self.assertEqual(attached,{"p","n"})
        self.assertEqual([e.id for e in notes["b"][0][1]],["n","p"])

    def test_margin_uses_rendered_flow_inset_and_stays_inside_physical_page(self):
        anchor=Element("a","paragraph","Body.",1,[145,100,540,140])
        note=Element("n","sidebar","EXAMPLE 1",1,[70,100,133,112])
        b=Book("s.pdf","s","Book",1,[1],elements=[anchor,note])
        markup=layout._margin_markup([("label",[note])],anchor,{"n":"例1"},b,{},self.geometry,516,
                                     layout._FONT_SIZES,[],{1:"原书 p.1"},set(),flow_inset=0)
        self.assertIn("dx: -42pt",markup)
        self.assertIn("34pt",markup)
        self.assertGreaterEqual(self.geometry["left"]-42,6)
        inset_markup=layout._margin_markup([("label",[note])],anchor,{"n":"例1"},b,{},self.geometry,394,
                                           layout._FONT_SIZES,[],{1:"原书 p.1"},set(),flow_inset=97)
        self.assertIn("dx: -96pt",inset_markup)

    def test_unrelated_sidebar_and_distant_label_are_not_moved(self):
        body=Element("body","paragraph","Body.",1,[145,100,540,136])
        note=Element("note","sidebar","A mathematical explanatory note.",1,[70,100,133,130])
        far=Element("far","sidebar","EXAMPLE 9",1,[70,200,133,212])
        self.assertEqual(layout._margin_attachments([body,note,far],self.geometry),({},set()))

    def test_runin_heading_changes_only_ephemeral_layout_role(self):
        heading=Element("h","paragraph","COUNTEREXAMPLES",1,[145,100,240,112],style={"font":"STIXGeneral-Bold"})
        body=Element("b","paragraph","In Section 1.4 ...",1,[145,99.7,540,160])
        original=heading.source_hash
        result=layout._flow_annotations([heading,body])
        self.assertEqual(result[0].metadata["render_as"],"heading")
        self.assertEqual(layout._font_size(result[0],layout._FONT_SIZES),11)
        self.assertEqual(heading.metadata,{})
        self.assertEqual(result[0].source_hash,original)

    def traces(self):
        items=[{"type":"exercise-boundary","id":"q"+str(i),"flow":"q1","page":1,"x":x,"y":y}
               for i,x,y in [(1,48,200),(2,48,340),(3,48,490),(4,315,210)]]
        return items+[{"type":"element","phase":"end","id":"q4","page":1,"x":315,"y":320}]

    def test_balance_is_literal_and_uses_whole_question_boundary(self):
        t=self.traces();choices=layout._exercise_column_breaks(t,self.geometry)
        self.assertEqual([k for k,v in choices.items() if v],["q3"])
        self.assertEqual(choices,layout._exercise_column_breaks(t,self.geometry))

    def test_final_subpart_contributes_to_balance_height(self):
        t=self.traces();t[-1]["y"]=220
        t.append({"type":"element","phase":"end","id":"q4b","page":1,"x":315,"y":400})
        b=Book("s.pdf","s","Book",1,[1],elements=[Element("q4","exercise","4. Stem.",1,group_id="q4"),Element("q4b","exercise","b) Final subpart.",1,group_id="q4")])
        self.assertEqual([k for k,v in layout._exercise_column_breaks(t,self.geometry,b).items() if v],["q3"])

    def test_narrow_question_after_early_band_end_recovers_two_columns(self):
        es=[Element("l1","exercise","1. Left.",1,[73,100,302,140],style={"columns":2,"column":0}),
            Element("l2","exercise","2. Left below early band.",1,[73,633,302,686],style={"columns":1,"column":0}),
            Element("r1","exercise","3. Right.",1,[333,100,562,140],style={"columns":2,"column":1}),
            Element("r2","exercise","4. Right.",1,[349,640,562,680],style={"columns":1,"column":0}),
            Element("wide","exercise","5. Full width task.",1,[73,700,562,720],style={"columns":1})]
        pages={1:{"columns":2,"column_split":325,"column_start_y":67,"column_end_y":623,"width":612}}
        result=layout._exercise_flow_annotations(es,pages)
        self.assertEqual(result[1].style["columns"],2)
        self.assertEqual(result[3].style["column"],1)
        self.assertEqual(result[4].style["columns"],1)
        self.assertEqual(es[1].style["columns"],1)

    def test_isolated_subpart_label_joins_only_same_question_same_source_line(self):
        label=Element("label","exercise","c)",1,[349,657.65,356.5,667.1],group_id="q13")
        body=Element("body","exercise","Jose never received a message.",1,[365,657.59,562,678],group_id="q13")
        other=Element("other","exercise","Another question.",1,[365,657.59,562,678],group_id="q14")
        self.assertTrue(layout._same_line_subpart(label,body))
        self.assertFalse(layout._same_line_subpart(label,other))

    def test_short_left_only_flow_is_balanced_before_following_full_width_body(self):
        t=[{"type":"exercise-boundary","id":"q"+str(i),"flow":"q1","page":1,"x":48,"y":y}
           for i,y in [(1,200),(2,300),(3,400),(4,500)]]
        t.append({"type":"element","phase":"end","id":"q4","page":1,"x":48,"y":580})
        self.assertEqual([k for k,v in layout._exercise_column_breaks(t,self.geometry).items() if v],["q3"])

    def test_nonconsecutive_source_page_starts_new_marked_segment(self):
        directory=Path(__file__).resolve().parent/"_tmp_layout_flow"/uuid.uuid4().hex
        directory.mkdir(parents=True)
        try:
            es=[Element("q1","exercise","9. Earlier question.",1,[60,100,300,120],printed_page="56",style={"columns":2,"column":0}),
                Element("later","exercise","b) Later-page subpart.",14,[330,100,560,120],printed_page="69",style={"columns":2,"column":1})]
            b=Book("s.pdf","s","Book",14,[1,14],elements=es)
            source,_,_=layout._build_source(b,{"q1":"9. 前段题干。","later":"b) 后段小问。"},directory,self.geometry,{},False)
            marker='#source-segment-start("原书 p.69")'
            self.assertEqual(source.count(marker),1)
            self.assertLess(source.index('"q1", "exercise"'),source.index("#pagebreak(weak: true)"))
            self.assertLess(source.index(marker),source.index('"later", "exercise"'))
            es[1].page=2
            b.selected_pages=[1,2]
            contiguous,_,_=layout._build_source(b,{"q1":"9. 前段题干。","later":"b) 后段小问。"},directory,self.geometry,{},False)
            self.assertNotIn("#source-segment-start(",contiguous)
            self.assertNotIn("#pagebreak(weak: true)",contiguous)
        finally:
            self.assertTrue(directory.resolve().is_relative_to((Path(__file__).resolve().parent/"_tmp_layout_flow").resolve()))
            shutil.rmtree(directory)

    def test_baseline_offsets_require_measured_same_page_pair(self):
        traces=[{"type":"number-baseline","id":"q","page":1,"y":50},
                {"type":"exercise-baseline","id":"q","page":1,"y":57.47},
                {"type":"number-baseline","id":"short","page":1,"y":50},
                {"type":"exercise-baseline","id":"short","page":1,"y":49.26},
                {"type":"number-baseline","id":"other","page":1,"y":50},
                {"type":"exercise-baseline","id":"other","page":2,"y":57.47}]
        self.assertEqual(layout._number_baseline_offsets(traces),{"q":7.47,"short":-.74})

    def test_actual_tall_inline_vector_keeps_number_and_first_text_on_same_baseline(self):
        try:layout._Runtime.find()
        except layout.CompositionError:self.skipTest("Pinned Typst unavailable")
        directory=Path(__file__).resolve().parent/"_tmp_layout_flow"/uuid.uuid4().hex
        directory.mkdir(parents=True)
        try:
            vector=directory/"original-vector.pdf"
            with pymupdf.open() as doc:
                page=doc.new_page(width=16,height=18)
                page.insert_text((8,15.63),"x",fontsize=9)
                page.draw_polyline([(1,9),(3,10),(5,2),(16,2)],color=(0,0,0),width=.5)
                short=doc.new_page(width=5,height=10)
                short.insert_text((.2,7.42),"x",fontsize=9.4645)
                doc.save(vector)
            asset=Asset("radical","formula",str(vector.resolve()),1,[0,0,16,18],16,18,15.63,"sqrt(x)",file_sha256(vector),{"baseline_from_top":15.63})
            short_asset=Asset("short","formula",str(vector.resolve()),1,[0,0,5,10],5,10,7.42,"x",file_sha256(vector),{"baseline_from_top":7.42,"pdf_page":1})
            e=Element("q15","exercise","15. Prove ⟦F:radical⟧ is irrational.",1,[35,45,200,65],group_id="q15")
            book=Book(str(vector.resolve()),file_sha256(vector),"Tall formula fixture",1,[1],elements=[e,Element("q16","exercise","16. ⟦F:short⟧",1,[35,80,200,95],group_id="q16")],assets={"radical":asset,"short":short_asset},profile={"page_width":240,"page_height":240,"margins":{"left":35,"right":35,"top":30,"bottom":30}})
            out=directory/"rendered";result=layout.compose(book,{"q15":"15. 证明：若 ⟦F:radical⟧ 是无理数。","q16":"16. ⟦F:short⟧"},out)
            mapping=json.loads((out/"layout_map.json").read_text(encoding="utf8"))
            traces=json.loads((out/"typst-traces.json").read_text(encoding="utf8"))
            self.assertGreater(mapping["parts"][0]["number_label_offsets"]["q15"],5)
            self.assertLess(mapping["parts"][0]["number_label_offsets"]["q16"],0)
            self.assertEqual(layout._number_baseline_offsets(traces),{})
            number=next(t for t in traces if t["type"]=="number-baseline")
            body=next(t for t in traces if t["type"]=="exercise-baseline")
            self.assertEqual(number["page"],body["page"])
            self.assertAlmostEqual(number["y"],body["y"],places=3)
            position=mapping["assets"]["radical"][0]["bbox"]
            self.assertAlmostEqual(position[2]-position[0],16,places=3)
            self.assertAlmostEqual(position[3]-position[1],18,places=3)
            with pymupdf.open(result["pdf_path"]) as doc:
                self.assertEqual(len(doc),1)
                spans=[s for b in doc[0].get_text("dict")["blocks"] for line in b.get("lines",[]) for s in line["spans"]]
                label=next(s for s in spans if s["text"]=="15.")
                prose=next(s for s in spans if "证明" in s["text"])
                self.assertAlmostEqual(label["origin"][1],prose["origin"][1],places=2)
                self.assertEqual(label["size"],9.5);self.assertEqual(prose["size"],9.5)
        finally:
            self.assertTrue(directory.resolve().is_relative_to((Path(__file__).resolve().parent/"_tmp_layout_flow").resolve()))
            shutil.rmtree(directory)

    def test_exercise_source_column_does_not_force_break(self):
        directory=Path(__file__).resolve().parent/"_tmp_layout_flow"/uuid.uuid4().hex
        directory.mkdir(parents=True)
        try:
            es=[Element("q1","exercise","1. First.",1,[60,100,300,120],style={"columns":2,"column":0}),Element("q2","exercise","2. Second.",1,[330,100,560,120],style={"columns":2,"column":1})]
            b=Book("s.pdf","s","Book",1,[1],pages=[{"number":1,"width":612,"height":792}],elements=es)
            source,issues,previews=layout._build_source(b,{"q1":"1. 第一题。","q2":"2. 第二题。"},directory,self.geometry,{},False)
            self.assertNotIn("#source-column-boundary(",source)
            self.assertEqual(source.count("#exercise-column-candidate("),2)
            self.assertLess(source.index('"q1", "exercise"'),source.index('"q2", "exercise"'))
            self.assertIn("#columns(2",source)
            self.assertEqual(issues,[]);self.assertEqual(previews,set())
        finally:
            self.assertTrue(directory.resolve().is_relative_to((Path(__file__).resolve().parent/"_tmp_layout_flow").resolve()))
            shutil.rmtree(directory)

if __name__=="__main__":unittest.main()
