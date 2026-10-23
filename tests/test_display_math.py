"""Original-size display formulas and their prose must paginate together."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest
import uuid

import pymupdf as fitz

from textbook_pdf import layout
from textbook_pdf.models import Asset, Book, Element, file_sha256
from textbook_pdf.store import translation_rule_hash
from textbook_pdf.translation import _review_binding, make_tasks
from textbook_pdf.visual_checks import compare_assets


class DisplayMathTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "_tmp_display_math"
        self.directory = self.base / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.assets = {}
        equations = {
            "owned-a": ["s(n + 1) = s(n) + 2n + 1", "= (n + 1)^2"],
            "owned-b": ["u(n + 1) = 2u(n) + 1", "= 2^(n + 1) - 1."],
        }
        for aid, lines in equations.items():
            path = self.directory / (aid + ".pdf")
            with fitz.open() as document:
                page = document.new_page(width=185, height=36)
                page.insert_font(fontname="OwnEmbedded", fontbuffer=fitz.Font("tiro").buffer)
                for index, text in enumerate(lines):
                    page.insert_text((3, 12 + 17 * index), text, fontname="OwnEmbedded", fontsize=9.5)
                document.save(path)
            self.assets[aid] = Asset(
                aid, "formula", path.name, 1, [0, 0, 185, 36], 185, 36, 29,
                " / ".join(lines), file_sha256(path),
                {"format": "pdf", "vector": True, "pdf_page": 0,
                 "baseline_from_top": 29, "isolated_source_content": True, "isolation_issues": []},
            )

    def tearDown(self):
        self.assertTrue(self.directory.resolve().is_relative_to(self.base.resolve()))
        shutil.rmtree(self.directory)

    def book(self):
        filler = Element("filler", "paragraph", "这是检验分页关联的自创说明文字，公式保持原有大小。" * 6, 1)
        target = Element(
            "target", "paragraph", "应用递推关系可得下式。⟦F:owned-a⟧（由归纳假设）⟦F:owned-b⟧", 1,
            asset_ids=list(self.assets), metadata={"display_asset_ids": list(self.assets)},
        )
        return Book(
            str(self.directory / "owned-a.pdf"), self.assets["owned-a"].sha256,
            "Self-authored display equations", 1, [1],
            pages=[{"number": 1, "width": 300, "height": 260, "printed_page": "1", "columns": 1}],
            elements=[filler, target], assets=deepcopy(self.assets),
            profile={"job_dir": str(self.directory), "page_width": 300, "page_height": 260,
                     "margins": {"top": 24, "bottom": 24, "left": 24, "right": 24},
                     "body_size": 10.5, "exercise_size": 9.5, "caption_size": 8.5},
        )

    def require_typst(self):
        try:
            layout._Runtime.find()
        except layout.CompositionError:
            self.skipTest("Pinned Typst unavailable")

    def test_annotation_and_lead_stay_with_both_formulas_across_page_boundary(self):
        self.require_typst()
        book = self.book()
        result = layout.compose(book, {e.id: e.text for e in book.elements}, self.directory / "composition")
        mapping = json.loads(Path(result["layout_map_path"]).read_text("utf8"))
        first, last = (mapping["assets"][aid][0] for aid in self.assets)
        self.assertGreaterEqual(result["page_count"], 2)
        self.assertEqual(first["page"], last["page"])
        self.assertGreater(first["page"], 1)
        for aid in self.assets:
            box = mapping["assets"][aid][0]["bbox"]
            self.assertAlmostEqual(box[2] - box[0], 185, places=4)
            self.assertAlmostEqual(box[3] - box[1], 36, places=4)
        with fitz.open(result["pdf_path"]) as document:
            notes = [(i + 1, r) for i, page in enumerate(document) for r in page.search_for("（由归纳假设）")]
            leads = [(i + 1, r) for i, page in enumerate(document) for r in page.search_for("应用递推关系可得下式。")]
            self.assertEqual(len(notes), 1)
            self.assertEqual(len(leads), 1)
            self.assertEqual(notes[0][0], first["page"])
            self.assertEqual(leads[0][0], first["page"])
            self.assertGreaterEqual(notes[0][1].y0, first["bbox"][3])
            self.assertLessEqual(notes[0][1].y1, last["bbox"][1])
        pixels = compare_assets(book, mapping, result["pdf_path"], self.directory / "pixels", limit=None)
        self.assertEqual((pixels["checked"], pixels["passed"]), (2, 2))
        self.assertFalse(pixels["issues"])

    def test_optin_changes_render_binding_without_changing_translation_or_review(self):
        marked = self.book()
        plain = deepcopy(marked)
        plain.elements[1].metadata = {}
        text = marked.elements[1].text
        self.assertEqual(make_tasks(plain, {}), make_tasks(marked, {}))
        self.assertEqual(translation_rule_hash(plain, plain.elements[1], {}), translation_rule_hash(marked, marked.elements[1], {}))
        self.assertEqual(_review_binding(plain.elements[1], text, plain, {}), _review_binding(marked.elements[1], text, marked, {}))
        self.assertNotEqual(layout.rendering_binding(plain, {}), layout.rendering_binding(marked, {}))

    def test_invalid_missing_repeated_or_duplicate_display_asset_is_rejected(self):
        book = self.book()
        element = book.elements[1]
        paths = {aid: aid + ".pdf" for aid in self.assets}
        for ids in [["owned-a", "owned-a"], ["missing"], "owned-a"]:
            element.metadata = {"display_asset_ids": ids}
            with self.assertRaises(layout.CompositionError):
                layout._text_markup(element.text, element, book, paths, 260, [])
        element.metadata = {"display_asset_ids": ["owned-a"]}
        with self.assertRaises(layout.CompositionError):
            layout._text_markup(element.text + "⟦F:owned-a⟧", element, book, paths, 260, [])

    def test_oversized_display_is_rejected_without_shrinking_formula(self):
        self.require_typst()
        book = self.book()
        book.elements = book.elements[1:]
        book.profile.update(page_width=180)
        output = self.directory / "oversize.pdf"
        with self.assertRaises(layout.CompositionError):
            layout.compose(book, {book.elements[0].id: book.elements[0].text}, self.directory / "oversize", output_path=output)
        self.assertFalse(output.exists())
        mapping = json.loads((self.directory / "oversize/failed-layout-map.json").read_text("utf8"))
        self.assertTrue(any(i["code"] == "asset_too_wide" and i["severity"] == "error" for i in mapping["issues"]))
        for aid in self.assets:
            box = mapping["assets"][aid][0]["bbox"]
            self.assertAlmostEqual(box[2] - box[0], 185, places=4)
            self.assertAlmostEqual(box[3] - box[1], 36, places=4)

    def test_numbered_exercise_uses_first_prose_baseline(self):
        self.require_typst()
        book = self.book()
        element = book.elements[1]
        element.kind = "exercise"
        element.text = "27. " + element.text
        book.elements = [element]
        result = layout.compose(book, {element.id: element.text}, self.directory / "numbered")
        traces = json.loads((self.directory / "numbered/typst-traces.json").read_text("utf8"))
        number = next(t for t in traces if t["type"] == "number-baseline")
        body = next(t for t in traces if t["type"] == "exercise-baseline")
        self.assertEqual(number["page"], body["page"])
        self.assertAlmostEqual(number["y"], body["y"], places=3)
        with fitz.open(result["pdf_path"]) as document:
            spans = [s for p in document for b in p.get_text("dict")["blocks"] for line in b.get("lines", []) for s in line["spans"]]
            label = next(s for s in spans if s["text"] == "27.")
            prose = next(s for s in spans if "应用递推关系" in s["text"])
            self.assertAlmostEqual(label["origin"][1], prose["origin"][1], places=2)
            self.assertEqual(label["size"], 9.5)
            self.assertEqual(prose["size"], 9.5)

    def test_pure_math_exercise_without_prose_lead_is_explicitly_rejected(self):
        book = self.book()
        element = book.elements[1]
        element.kind, element.text = "exercise", "⟦F:owned-a⟧"
        element.asset_ids, element.metadata = ["owned-a"], {"display_asset_ids": ["owned-a"]}
        with self.assertRaises(layout.CompositionError):
            layout._text_markup(element.text, element, book, {"owned-a": "owned-a.pdf"}, 260, [])

    def test_figure_is_not_silently_reclassified_as_formula(self):
        book = self.book()
        book.assets["owned-a"].kind = "figure"
        with self.assertRaises(layout.CompositionError):
            layout._text_markup(book.elements[1].text, book.elements[1], book, {a: a + ".pdf" for a in self.assets}, 260, [])


if __name__ == "__main__":
    unittest.main()
