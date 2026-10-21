"""Geometry regressions for immutable textbook source extraction; never calls APIs."""
from __future__ import annotations
import gzip
import json
import os
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
import unittest
import uuid

from textbook_pdf.babel_adapter import _box
from textbook_pdf.extraction import (_annotate_page_structure, _math_spans,
    _validate_source_references, _asset, _collect, _combine_wrapped_math, _repair_hyphenated_flow, _visual_regions, _merge_body_continuations, _merge_sidebar_prose, _check_atomic_formulas, analyze, reanchor_existing)
from textbook_pdf.models import Asset, Book, Element, TOKEN_RE, file_sha256, plain_text

SOURCE = Path(os.environ["TEXTBOOK_TEST_ROSEN_PDF"]).expanduser() if os.environ.get("TEXTBOOK_TEST_ROSEN_PDF") else None


def line(text,font="STIXGeneral-Regular",flags=0):
    gs=[{"id":f"g{i}","c":c,"bbox":[10+i*5,30,15+i*5,40],"origin":[10+i*5,38],
         "font":font,"flags":flags,"size":10,"color":0,"owner":None} for i,c in enumerate(text)]
    return {"text":text,"chars":gs,"bbox":[10,30,10+len(text)*5,40],"block":0,"line":0}


class ExtractionUnitTests(unittest.TestCase):
    def test_three_wrapped_segments_have_one_exact_asset_owner(self):
        import pymupdf
        doc=pymupdf.open();page=doc.new_page(width=300,height=200)
        for i,text in enumerate(("a+","b+","c")):page.insert_text((50,50+i*14),text,fontsize=10)
        lines,gs=_collect(page)
        book=Book("fixture","sha","Fixture",1,[1]);book._active_glyphs=gs
        assets=[]
        for row in lines:
            asset=_asset(book,doc,page,Path('.'),"formula",row['bbox'],row['text'],row['chars'][0]['origin'][1],{"glyph_ids":[g['id'] for g in row['chars']]})
            assets.append(asset)
        element=Element("e","formula"," ".join(f"⟦F:{a.id}⟧" for a in assets),1,[50,40,70,80],asset_ids=[a.id for a in assets])
        _combine_wrapped_math(book,[element],1,doc=doc,page=page)
        refs=TOKEN_RE.findall(element.text)
        self.assertEqual(len(refs),1)
        self.assertEqual(set(refs),set(element.asset_ids))
        joined=book.assets[refs[0]]
        self.assertEqual(joined.text,'a+b+c')
        self.assertLess(joined.width,25)
        self.assertGreater(joined.height,30)
        self.assertTrue(joined.metadata['native_glyph_geometry_preserved'])
        self.assertEqual(len(joined.metadata['glyph_ids']),sum(len(a.metadata['glyph_ids']) for a in assets))
        for bundle in book._asset_bundles.values():bundle.close()
        doc.close()

    def test_hyphenated_exercise_continues_past_another_column(self):
        a=Element("a","exercise","40. Increasing dead-",271,[43,65,280,77],style={"source_size":9.5,"column":0},metadata={"source_text":"40. Increasing dead-","glyph_ids":[]})
        unrelated=Element("right","exercise","2. Another question",271,[330,70,540,81],style={"source_size":9.5,"column":0})
        b=Element("b","paragraph","lines always work.",271,[66,78,280,95],style={"source_size":9.5,"column":0},metadata={"source_text":"lines always work.","glyph_ids":[]})
        book=Book("s","sha","Fixture",271,[271]);es=[a,unrelated,b]
        _repair_hyphenated_flow(book,es,[])
        self.assertEqual(a.text,"40. Increasing deadlines always work.")
        self.assertEqual([e.id for e in es],["a","right"])
        self.assertIn("b",a.metadata["merged_source_ids"])

    def test_hyphenated_flow_refuses_a_new_question_and_ambiguous_candidates(self):
        a=Element("a","exercise","a wit-",251,[50,100,280,110],style={"source_size":10,"column":0})
        b=Element("b","exercise","2. nesses from another column",251,[54,111,524,125],style={"source_size":10,"column":0})
        book=Book("s","sha","Fixture",251,[251]);es=[a,b]
        _repair_hyphenated_flow(book,es,[])
        self.assertEqual(len(es),2)
        self.assertEqual(a.text,"a wit-")

    def test_inline_bold_option_markers_stay_outside_protected_math(self):
        text="21. a) x + 1 b) x2 + 2 c) 17"
        row=line(text)
        for g in row['chars']:
            if g['c']=='x':g.update(font='STIXGeneral-Italic',flags=2)
        for m in re.finditer(r'[abc]\)',text):
            for g in row['chars'][m.start():m.end()]:g.update(font='STIXGeneral-Bold',flags=16)
        spans=[text[a:b] for a,b in _math_spans(row)]
        self.assertFalse(any(')' in t for t in spans))
        tuple_row=line('P(x,y)',flags=2)
        self.assertEqual(_math_spans(tuple_row),[(0,len(tuple_row['chars']))])

    def test_mathjax_roman_diagram_language_is_translatable(self):
        for text in ("guaranteed to be in correct order","Reproducing pairs","shown with dotted lines and with dashed lines, respectively"):
            self.assertEqual(_math_spans(line(text,'STIXMathJax_Main-Regular',4)),[])
        text='f(x) < Cg(x) is shown in color.'
        row=line(text,'STIXMathJax_Main-Regular',4)
        for g in row['chars'][:12]:
            if g['c'].isalpha():g.update(font='STIXMathJax_Main-Italic',flags=6)
        spans=[text[a:b] for a,b in _math_spans(row)]
        self.assertFalse(any('shown' in value or 'color' in value for value in spans))
        self.assertTrue(any('Cg(x)' in value for value in spans))
        call=line('P(New York, New York)','STIXMathJax_Main-Regular',4)
        call['chars'][0].update(font='STIXMathJax_Main-Italic',flags=6)
        self.assertEqual(_math_spans(call),[(0,len(call['chars']))])

    def test_simple_paths_and_partial_grid_do_not_delete_diagram(self):
        import pymupdf as f
        outer=f.Rect(20,20,140,140)
        drawings=[{"rect":outer,"items":[('re',outer,1)],"fill":None,"color":(0,0,0)}]
        table=SimpleNamespace(row_count=4,col_count=3,bbox=[80,30,130,130])
        page=SimpleNamespace(rect=f.Rect(0,0,300,300),get_image_info=lambda:[],
            cluster_drawings=lambda **kw:[outer],find_tables=lambda:SimpleNamespace(tables=[table]))
        regions=_visual_regions(page,drawings*5,[])
        self.assertTrue(any(r['kind']=='figure' and f.Rect(r['bbox']).contains(outer) for r in regions))
        self.assertFalse(any(r['kind']=='table' for r in regions))
        # A single diagonal is semantic data too; path count is no rejection.
        page.cluster_drawings=lambda **kw:[f.Rect(20,20,80,60)]
        regions=_visual_regions(page,[{'rect':f.Rect(20,20,80,60),'items':[('l',(20,20),(80,60))],'fill':None,'color':(0,0,0)}],[])
        self.assertEqual(len(regions),1)

    def test_italic_terminology_is_not_a_function(self):
        for text in ("hypothesis (or antecedent or premise)","conclusion (or consequence)"):
            row=line(text,"STIXGeneral-Italic",2)
            self.assertEqual(_math_spans(row),[])

    def test_copyright_credit_is_not_math(self):
        for text in ("©2019","⃝Alfred Eisenstaedt/", "Source: Bettmann/Getty Images"):
            self.assertEqual(_math_spans(line(text,"STIXMath-Regular",2)),[])

    def test_predicate_keeps_regular_font_argument_atomic(self):
        row=line("P(orange)")
        row["chars"][0].update(font="STIXGeneral-Italic",flags=2)
        self.assertEqual(_math_spans(row),[(0,len(row["chars"]))])

    def test_babel_prepared_crop_transform(self):
        b=SimpleNamespace(x=338.98672,y=231.01557,x2=345.05698,y2=237.20239)
        actual=_box(b,799.52001953125,(42,0))
        expected=[296.98672,562.31762953125,303.05698,568.50444953125]
        for a,e in zip(actual,expected):self.assertAlmostEqual(a,e,places=5)

    def test_drop_cap_preserves_hashes_and_precedes_its_body(self):
        cap=Element("cap","sidebar","K",7,[145,131,173,168],metadata={"drop_cap":True,"source_text":"K","source_block":1})
        body=Element("body","paragraph","enneth H. Rosen",7,[174,139,540,162],metadata={"source_text":"enneth H. Rosen","source_block":2})
        elements=[body,cap];before={e.id:e.source_hash for e in elements}
        _annotate_page_structure(elements)
        self.assertEqual([e.id for e in elements],["cap","body"])
        self.assertEqual({e.id:e.source_hash for e in elements},before)
        self.assertEqual(cap.metadata["inline_group"],body.metadata["inline_group"])
        self.assertEqual(body.metadata["inline_join"],"word")

    def test_marker_uses_nearest_horizontal_question(self):
        marker=Element("marker","figure","",118,[317,462,333,471],metadata={"role":"exercise_marker"})
        left=Element("left","exercise","4. Question",118,[78,457,302,477],style={"column":0},group_id="left")
        right=Element("right","exercise","18. Question",118,[333,464,562,485],style={"column":1},group_id="right")
        book=Book("source","sha","Fixture",118,[118],elements=[left,marker,right],pages=[{"number":118}])
        fixed=reanchor_existing(book)
        self.assertEqual(fixed.elements[1].id,"marker")
        self.assertEqual(fixed.elements[1].metadata["anchor_id"],"right")
        self.assertEqual(marker.metadata.get("anchor_id"),None)

    def test_hyphen_continuation_survives_math_font_and_block_change(self):
        first=Element("first","paragraph","the do-",139,[50,100,283,122],style={"font":"STIXMath-Bold","source_size":9.5,"column":0},metadata={"source_text":"the do-","source_block":2,"glyph_ids":[]})
        second=Element("second","sidebar","main",139,[62,123.5,81,133],style={"font":"STIXGeneral-Regular","source_size":9.5,"column":0},metadata={"source_text":"main","source_block":3,"glyph_ids":[]})
        elements=[first,second];book=Book("source","sha","Fixture",139,[139],elements=elements)
        _merge_body_continuations(book,elements,[],612)
        self.assertEqual(len(elements),1)
        self.assertEqual(elements[0].text,"the domain")

    def test_portrait_wrap_uses_first_line_not_union_left_edge(self):
        first=Element("first","paragraph","His father",25,[145,100,540,109],style={"source_size":9,"font":"Regular","column":0},metadata={"source_text":"His father","source_block":1,"glyph_ids":[]})
        second=Element("second","paragraph","was a physician.",25,[72,110,540,160],style={"source_size":9,"font":"Regular","column":0},metadata={"source_text":"was a physician.","source_block":2,"glyph_ids":["g1","g2"]})
        gs=[{"id":"g1","c":"w","size":9,"font":"Regular","bbox":[145,110,150,119],"origin":[145,118],"owner":"second"},{"id":"g2","c":"n","size":9,"font":"Regular","bbox":[72,150,77,159],"origin":[72,158],"owner":"second"}]
        es=[first,second];book=Book("source","sha","Fixture",25,[25],elements=es)
        _merge_body_continuations(book,es,gs,612)
        self.assertEqual([e.text for e in es],["His father was a physician."])
        self.assertTrue(all(g["owner"]=="first" for g in gs))

    def test_shared_neural_paragraph_accepts_normal_style_continuation(self):
        a=Element("a","paragraph","in the domain",139,[50,100,283,109],style={"source_size":9.5,"font":"STIXMath-Bold","column":0},metadata={"source_text":"in the domain","source_block":1,"structure_id":"same","glyph_ids":[]})
        b=Element("b","sidebar","such that P(x) is true",139,[62,110,283,119],style={"source_size":9.5,"font":"STIXGeneral-Regular","column":0},metadata={"source_text":"such that P(x) is true","source_block":2,"structure_id":"same","glyph_ids":[]})
        es=[a,b];book=Book("source","sha","Fixture",139,[139],elements=es)
        _merge_body_continuations(book,es,[],612)
        self.assertEqual([e.text for e in es],["in the domain such that P(x) is true"])

    def test_quoted_same_line_flow_and_narrow_note_are_complete(self):
        a=Element("a","paragraph","when p is the statement",31,[145,100,540,110],style={"source_size":10,"font":"Regular","column":0},metadata={"source_text":"when p is the statement","source_block":1,"glyph_ids":[]})
        b=Element("b","paragraph","“Maria learns mathematics”",31,[145,111,540,121],style={"source_size":10,"font":"Regular","column":0},metadata={"source_text":"“Maria learns mathematics”","source_block":2,"glyph_ids":[]})
        es=[a,b];book=Book("source","sha","Fixture",31,[31],elements=es)
        _merge_body_continuations(book,es,[],612)
        self.assertEqual(len(es),1)
        notes=[Element(str(i),"footnote",t,30,[73,640+i*10,148,648+i*10],style={"source_size":8,"font":"Regular"},metadata={"source_text":t,"source_block":12,"glyph_ids":[]}) for i,t in enumerate(["You might have trouble","understanding how","unless is used."])]
        notes.insert(1,Element("body","paragraph","Unrelated main text",30,[167,650,562,690]))
        _merge_sidebar_prose(book,notes,[])
        notes=[e for e in notes if e.kind=="footnote"]
        self.assertEqual([e.text for e in notes],["You might have trouble understanding how unless is used."])

    def test_clause_commas_and_step_separators_are_outside_math(self):
        row=line("p →q, p is called the hypothesis")
        for index in (0,3,6):row["chars"][index].update(font="STIXGeneral-Italic",flags=2)
        protected=[row["text"][a:b] for a,b in _math_spans(row)]
        self.assertIn("p →q",protected);self.assertIn("p",protected)
        self.assertNotIn("p →q, p",protected)
        for text in ("(2); (4) (x −1)(x +1) = 0, obtained", "x2 −1; (5) x = 1 or x = −1"):
            row=line(text,"STIXMath-Regular")
            spans=[text[a:b] for a,b in _math_spans(row)]
            self.assertFalse(any(";" in value for value in spans))
        tuple_row=line("P(x,y)","STIXMath-Regular")
        self.assertEqual(_math_spans(tuple_row),[(0,len(tuple_row["chars"]))])

    def test_indexed_coordinated_subject_is_not_part_of_its_range(self):
        text="1 ≤j ≤n, pi and pj are equivalent"
        row=line(text)
        for a,b in ((3,4),(6,7),(9,11),(16,18)):
            for i in range(a,b):row["chars"][i].update(font="STIXGeneral-Italic",flags=2)
        spans=[text[a:b] for a,b in _math_spans(row)]
        self.assertIn("1 ≤j ≤n",spans)
        self.assertIn("pi",spans);self.assertIn("pj",spans)
        self.assertFalse(any("," in part for part in spans))
        values="p1, p2, pn are equivalent"
        row=line(values)
        for match in re.finditer(r"p[12n]",values):
            for i in range(*match.span()):row["chars"][i].update(font="STIXMath-Regular",flags=2)
        self.assertEqual([values[a:b] for a,b in _math_spans(row)],["p1, p2, pn"])
        values="p1 →p2, p2 →p3, and p3 →p1 are true"
        row=line(values)
        for match in re.finditer(r"p[123]",values):
            for i in range(*match.span()):row["chars"][i].update(font="STIXMath-Regular",flags=2)
        self.assertEqual([values[a:b] for a,b in _math_spans(row)],["p1 →p2, p2 →p3","p3 →p1"])

    def test_unresolved_step_fusion_blocks_but_matrix_separator_does_not(self):
        for text,blocked in (("(2); (4) x=0",True),("{(1);(2)}",False)):
            asset=Asset("a","formula","unused.pdf",1,[0,0,10,10],10,10,text=text)
            element=Element("e","paragraph","⟦F:a⟧",1,asset_ids=["a"])
            book=Book("source","sha","Fixture",1,[1],elements=[element],assets={"a":asset})
            _check_atomic_formulas(book,[element])
            self.assertEqual(any(i["code"]=="source_formula_step_reference_mixed" for i in book.issues),blocked)

    def test_missing_formula_reference_blocks_source(self):
        element=Element("bad","paragraph","Text ⟦F:missing⟧",1)
        book=Book("source","sha","Fixture",1,[1],elements=[element])
        _validate_source_references(book,[element],[],1)
        self.assertIn("source_asset_reference_missing",{i["code"] for i in book.issues})
        self.assertTrue(all(i["severity"]=="error" for i in book.issues))


@unittest.skipUnless(SOURCE is not None and SOURCE.is_file(),"Local Rosen source is not available")
class RealSourceExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=Path(__file__).resolve().parent/"_tmp_extraction"
        cls.base.mkdir(exist_ok=True);cls.directory=cls.base/uuid.uuid4().hex;cls.directory.mkdir()
        cls.before=file_sha256(SOURCE)
        cls.book=analyze(SOURCE,cls.directory,[7,23,24,25,28,29,30,35,56,79,92,105,112,113,114,115,116,118,119,139],backend="native")

    @classmethod
    def tearDownClass(cls):
        assert file_sha256(SOURCE)==cls.before
        target=cls.directory.resolve();assert target.is_relative_to(cls.base.resolve()) and target!=cls.base.resolve()
        shutil.rmtree(target)

    def test_all_source_glyphs_and_references_have_destinations(self):
        elements={e.id for e in self.book.elements}
        for element in self.book.elements:
            self.assertTrue(set(TOKEN_RE.findall(element.text))<=self.book.assets.keys())
            self.assertTrue(set(element.asset_ids)<=self.book.assets.keys())
        for page in self.book.pages:
            with gzip.open(self.directory/page["coverage"]["ledger"],"rt",encoding="utf8") as stream:gs=json.load(stream)
            self.assertEqual(len(gs),page["coverage"]["total_glyphs"])
            self.assertFalse(any(g["disposition"]=="unresolved" for g in gs))
            self.assertTrue(all(g["owner"] in elements|{"whitespace"} for g in gs))

    def test_actual_math_words_and_copyright_do_not_become_formula_assets(self):
        texts=[a.text for a in self.book.assets.values() if a.kind=="formula"]
        self.assertFalse(any("hypothesis" in text or "conclusion" in text or "⃝Alfred" in text for text in texts))
        self.assertFalse(any(i["code"]=="formula_crop_contamination" and i["page"]==35 for i in self.book.issues))

    def test_justified_words_and_column_labels_do_not_fragment_body(self):
        p56=[e for e in self.book.elements if e.page==56]
        self.assertFalse(any(e.text.strip() in {"of","the","compound","propositions"} for e in p56))
        p92="\n".join(plain_text(e,self.book) for e in self.book.elements if e.page==92)
        self.assertIsNone(re.search(r"c\)\s*testants|l\)\s*main|b\)\s*icate",p92))
        self.assertEqual(next(p for p in self.book.pages if p["number"]==30)["columns"],1)

    def test_glossary_words_and_entry_boundaries_are_complete(self):
        entries=[e for e in self.book.elements if e.page==139 and not e.metadata.get("render_suppressed")]
        self.assertFalse(any(e.text.endswith(("do-","quan-")) for e in entries))
        for entry in entries:
            self.assertLessEqual(sum(term in entry.text for term in ("free variable:","bound variable:","scope of a quantifier:")),1)

    def test_biography_and_short_if_radical_are_complete(self):
        p25=[plain_text(e,self.book) for e in self.book.elements if e.page==25]
        self.assertTrue(any("His father was the personal physician" in text for text in p25))
        p28=[plain_text(e,self.book) for e in self.book.elements if e.page==28]
        self.assertTrue(any("England, in November 1815" in text for text in p28))
        self.assertTrue(any("In this book, Boole introduced" in text for text in p28))
        notes=[plain_text(e,self.book) for e in self.book.elements if e.page==30 and e.kind=="footnote"]
        self.assertTrue(any("You might have trouble understanding how" in text and "paragraph carefully." in text for text in notes))
        referenced={aid for e in self.book.elements if e.page==114 for aid in e.asset_ids}
        self.assertFalse(any(self.book.assets[aid].text.strip().endswith("√") for aid in referenced))
        self.assertTrue(any("(p∧¬q)→F" in self.book.assets[aid].text.replace(" ","") for aid in referenced))

    def test_radical_ascenders_do_not_skip_a_prose_line(self):
        texts=[plain_text(e,self.book) for e in self.book.elements if e.page==113]
        joined="\n".join(texts)
        self.assertNotIn("supsays",joined)
        self.assertTrue(any("we suppose that" in text and text.index("pose that")<text.index("says that") for text in texts))

    def test_display_fractions_keep_the_whole_original_region(self):
        for number,minimum_bars in ((112,3),(114,1)):
            displays=[a for a in self.book.assets.values() if a.page==number and a.metadata.get("display_math")]
            self.assertTrue(displays,f"No complete native display on source{number}")
            target=max(displays,key=lambda a:len(a.metadata.get("fraction_bar_bboxes",[])))
            self.assertGreaterEqual(len(target.metadata["fraction_bar_bboxes"]),minimum_bars)
            import pymupdf
            with pymupdf.open(self.directory/target.path) as bundle:
                crop=bundle[target.metadata["pdf_page"]]
                lines=[d["rect"] for d in crop.get_drawings() if d["rect"].height<1 and d["rect"].width>2]
                self.assertGreaterEqual(len(lines),minimum_bars)
                self.assertAlmostEqual(crop.rect.width,target.width,places=3)
                self.assertAlmostEqual(crop.rect.height,target.height,places=3)
            self.assertTrue(target.metadata["native_glyph_geometry_preserved"])

    def test_statement_labels_and_proof_reasons_keep_rows(self):
        p115=[plain_text(e,self.book) for e in self.book.elements if e.page==115 and not e.metadata.get("render_suppressed")]
        self.assertTrue(any("p1:" in t and "n is even." in t for t in p115))
        self.assertTrue(any("p2:" in t and "n −1 is odd." in t for t in p115))
        self.assertTrue(any("p3:" in t and "n2 is even." in t for t in p115))
        tables=[e for e in self.book.elements if e.page==116 and e.metadata.get("proof_grid")]
        self.assertEqual(len(tables),1)
        grid=tables[0].metadata["table_grid"]
        self.assertEqual(len(grid["rows"]),8)
        row6=grid["rows"][6]
        self.assertIn("and simplify",plain_text(self.book.element_map()[row6[1]],self.book))

    def test_actual_clauses_and_numbered_steps_are_not_fused(self):
        assets=[a for e in self.book.elements if e.page in {29,119} for aid in e.asset_ids for a in [self.book.assets[aid]]]
        self.assertFalse(any(re.search(r";\s*\(\d+\)",a.text) for a in assets))
        definition=next(e for e in self.book.elements if e.page==29 and "hypothesis" in e.text)
        self.assertFalse(any(re.search(r"→.*?,\s*p$",self.book.assets[aid].text) for aid in definition.asset_ids))
        indexed=next(e for e in self.book.elements if e.page==115 and "for all" in e.text and "propositions" in e.text)
        protected=[self.book.assets[aid].text for aid in indexed.asset_ids]
        self.assertFalse(any("≤" in text and "," in text and "pi" in text for text in protected))
        self.assertIn("pi",protected)


    def test_radical_and_problem_page_marker_are_complete(self):
        self.assertFalse(any(a.text.strip().endswith("√") for a in self.book.assets.values() if a.page==113 and a.kind=="formula"))
        marker=next(e for e in self.book.elements if e.page==118 and e.metadata.get("role")=="exercise_marker")
        anchor=self.book.element_map()[marker.metadata["anchor_id"]]
        self.assertTrue(anchor.text.startswith("18."))
        self.assertEqual(self.book.elements.index(marker)+1,self.book.elements.index(anchor))
        blank=next(p for p in self.book.pages if p["number"]==23)
        self.assertEqual(blank["page_role"],"blank")


if __name__=="__main__":unittest.main()
