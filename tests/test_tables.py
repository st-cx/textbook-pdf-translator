"""Native PDF table integration tests; generated fixtures stay in memory."""
from __future__ import annotations

import os
from pathlib import Path
import unittest

import pymupdf

from textbook_pdf.table_adapter import extract_table_grid, extract_table_body_grid


ROSEN = Path(os.environ["TEXTBOOK_TEST_ROSEN_PDF"]).expanduser() if os.environ.get("TEXTBOOK_TEST_ROSEN_PDF") else None


class NativeTableTests(unittest.TestCase):
    def setUp(self):
        self.docs = []

    def tearDown(self):
        for doc in self.docs:
            doc.close()

    def document(self):
        doc = pymupdf.open()
        self.docs.append(doc)
        return doc

    @staticmethod
    def draw_grid(page, values, xs=(30, 220, 370), top=30, row_height=35, bold_header=True):
        bottom = top + row_height * len(values)
        for x in xs:
            page.draw_line((x, top), (x, bottom), width=.6)
        for r in range(len(values) + 1):
            y = top + r * row_height
            page.draw_line((xs[0], y), (xs[-1], y), width=.6)
        for r, row in enumerate(values):
            for c, text in enumerate(row):
                page.insert_text((xs[c] + 5, top + 22 + r * row_height), text,
                                 fontname="hebo" if bold_header and r == 0 else "helv", fontsize=10)
        return [xs[0], top, xs[-1], bottom]

    @staticmethod
    def xref_snapshot(doc):
        return [(xref, doc.xref_object(xref), doc.xref_stream(xref) if doc.xref_is_stream(xref) else None)
                for xref in range(1, doc.xref_length())]

    def test_multi_page_headers_have_repeat_metadata_and_preserved_math_cells(self):
        doc = self.document()
        bboxes = []
        for page_number in range(2):
            page = doc.new_page(width=400, height=240)
            bboxes.append(self.draw_grid(page, [
                ["Description", "Formula"], ["A natural statement", "p => q"],
                ["Another proposition", "x^2 + 1"], ["Number of cases", str(page_number + 2)],
            ]))
        before = self.xref_snapshot(doc)
        grids = []
        for page, bbox in zip(doc, bboxes):
            diagnostics = []
            grid = extract_table_grid(page, bbox, diagnostics=diagnostics)
            self.assertIsNotNone(grid, diagnostics)
            self.assertEqual(diagnostics, [])
            self.assertEqual(grid["header_rows"], 1)
            self.assertTrue(grid["repeat_header"])
            self.assertTrue(grid["rows"][1][0]["translatable"])
            self.assertEqual(grid["rows"][1][1]["text"], "p => q")
            self.assertEqual(grid["rows"][2][1]["text"], "x^2 + 1")
            self.assertEqual(grid["rows"][1][1]["kind"], "formula")
            self.assertFalse(grid["rows"][1][1]["translatable"])
            self.assertFalse(grid["rows"][3][1]["translatable"])
            self.assertEqual(grid["evidence"]["unassigned_source_chars"], 0)
            self.assertTrue(grid["evidence"]["continuation_requires_adjacent_page_and_caption"])
            self.assertTrue(all(cell["rowspan"] == cell["colspan"] == 1 for row in grid["rows"] for cell in row))
            grids.append(grid)
        self.assertEqual(grids[0]["header_signature"], grids[1]["header_signature"])
        self.assertEqual(grids[0]["continuation_key"], grids[1]["continuation_key"])
        self.assertEqual(before, self.xref_snapshot(doc), "Native table inspection must not change PDF objects or content streams")

    def test_numeric_first_row_is_not_invented_as_a_header(self):
        doc = self.document()
        page = doc.new_page(width=400, height=200)
        bbox = self.draw_grid(page, [["1", "2"], ["3", "4"]], bold_header=False)
        diagnostics = []
        grid = extract_table_grid(page, bbox, diagnostics=diagnostics)
        self.assertIsNotNone(grid, diagnostics)
        self.assertEqual(grid["header_rows"], 0)
        self.assertFalse(grid["repeat_header"])
        self.assertIsNone(grid["continuation_key"])
        self.assertTrue(all(not cell["translatable"] for row in grid["rows"] for cell in row))

    def test_merged_cells_reject_with_a_reason_instead_of_flattening(self):
        doc = self.document()
        page = doc.new_page(width=400, height=200)
        page.draw_rect([30, 30, 370, 135])
        page.draw_line((30, 65), (370, 65))
        page.draw_line((30, 100), (370, 100))
        page.draw_line((220, 65), (220, 135))
        page.insert_text((35, 52), "Merged title", fontname="hebo", fontsize=10)
        page.insert_text((35, 87), "Left", fontsize=10)
        page.insert_text((225, 87), "Right", fontsize=10)
        page.insert_text((35, 122), "1", fontsize=10)
        page.insert_text((225, 122), "2", fontsize=10)
        diagnostics = []
        self.assertIsNone(extract_table_grid(page, [30, 30, 370, 135], diagnostics=diagnostics))
        self.assertEqual(diagnostics[0]["code"], "table_merged_or_missing_cells")
        self.assertTrue(diagnostics[0]["evidence"]["positions"])

    def test_external_headers_are_not_silently_omitted(self):
        doc = self.document()
        page = doc.new_page(width=400, height=220)
        page.insert_text((35, 70), "Name", fontname="hebo", fontsize=10)
        page.insert_text((225, 70), "Value", fontname="hebo", fontsize=10)
        bbox = self.draw_grid(page, [["1", "2"], ["3", "4"]], top=80, bold_header=False)
        diagnostics = []
        self.assertIsNone(extract_table_grid(page, bbox, diagnostics=diagnostics))
        self.assertEqual(diagnostics[0]["code"], "table_external_header_unresolved")
        self.assertIn("Name", diagnostics[0]["evidence"]["names"])

    def test_clipping_half_a_table_does_not_create_a_false_complete_grid(self):
        doc = self.document()
        page = doc.new_page(width=400, height=220)
        self.draw_grid(page, [["Name", "Value"], ["One", "1"], ["Two", "2"]])
        diagnostics = []
        self.assertIsNone(extract_table_grid(page, [30, 30, 220, 135], diagnostics=diagnostics))
        self.assertEqual(diagnostics[0]["code"], "table_grid_not_unique")
        self.assertEqual(diagnostics[0]["evidence"]["candidate_count"], 0)

    def test_multiple_native_grids_are_not_assigned_to_one_table(self):
        doc = self.document()
        page = doc.new_page(width=400, height=250)
        self.draw_grid(page, [["Name", "Value"], ["One", "1"]], xs=(20, 100, 180))
        self.draw_grid(page, [["Name", "Value"], ["Two", "2"]], xs=(210, 290, 380))
        diagnostics = []
        self.assertIsNone(extract_table_grid(page, [20, 30, 380, 100], diagnostics=diagnostics))
        self.assertEqual(diagnostics[0]["code"], "table_grid_not_unique")
        self.assertEqual(diagnostics[0]["evidence"]["candidate_count"], 2)

    def test_intersecting_drawn_grids_are_not_flattened_into_a_table(self):
        doc = self.document()
        page = doc.new_page(width=400, height=300)
        for xs, ys in [((30, 120, 210), (30, 100, 170)), ((130, 220, 310), (80, 150, 220))]:
            for x in xs:
                page.draw_line((x, ys[0]), (x, ys[-1]))
            for y in ys:
                page.draw_line((xs[0], y), (xs[-1], y))
        diagnostics = []
        self.assertIsNone(extract_table_grid(page, [30, 30, 310, 220], diagnostics=diagnostics))
        self.assertTrue(diagnostics)

    def test_rotated_page_rejects_before_pymupdf_can_insert_a_content_stream(self):
        doc = self.document()
        page = doc.new_page(width=400, height=220)
        self.draw_grid(page, [["Name", "Value"], ["One", "1"]])
        page.set_rotation(90)
        before = self.xref_snapshot(doc)
        diagnostics = []
        self.assertIsNone(extract_table_grid(page, list(page.rect), diagnostics=diagnostics))
        self.assertEqual(diagnostics[0]["code"], "table_rotated_page_unsupported")
        self.assertEqual(before, self.xref_snapshot(doc))

    def test_invalid_or_one_column_regions_are_rejected(self):
        doc = self.document()
        page = doc.new_page(width=400, height=220)
        bbox = self.draw_grid(page, [["Header"], ["Value"]], xs=(30, 200))
        diagnostics = []
        self.assertIsNone(extract_table_grid(page, bbox, diagnostics=diagnostics))
        self.assertEqual(diagnostics[0]["code"], "table_grid_not_unique")
        diagnostics.clear()
        self.assertIsNone(extract_table_grid(page, [30, 30, float("nan"), 100], diagnostics=diagnostics))
        self.assertEqual(diagnostics[0]["code"], "table_invalid_bbox")

    @unittest.skipUnless(ROSEN is not None and ROSEN.is_file(), "Optional Rosen source is not present on this machine")
    def test_actual_rosen_truth_value_table_translates_headers_only(self):
        doc = pymupdf.open(ROSEN)
        self.docs.append(doc)
        diagnostics = []
        grid = extract_table_grid(doc[34], [50.203, 102.345, 132.082, 153.457], diagnostics=diagnostics)
        self.assertIsNotNone(grid, diagnostics)
        self.assertEqual(grid["row_count"], 2)
        self.assertEqual(grid["col_count"], 2)
        self.assertEqual(grid["header_rows"], 1)
        self.assertEqual([cell["text"] for cell in grid["rows"][0]], ["Truth Value", "Bit"])
        self.assertTrue(all(cell["translatable"] for cell in grid["rows"][0]))
        self.assertEqual([cell["kind"] for cell in grid["rows"][1]], ["truth", "numeric"])
        self.assertFalse(any(cell["translatable"] for cell in grid["rows"][1]))
        self.assertFalse(doc.is_dirty)

    @unittest.skipUnless(ROSEN is not None and ROSEN.is_file(), "Optional Rosen source is not present on this machine")
    def test_actual_rosen_merged_caption_tables_use_explicit_safe_fallback(self):
        doc = pymupdf.open(ROSEN)
        self.docs.append(doc)
        for physical_page, bbox in [
            (27, [50.453, 66.957, 131.497, 167.839]),
            (34, [168.0035, 337.74, 448.485, 434.536]),
        ]:
            with self.subTest(physical_page=physical_page):
                diagnostics = []
                self.assertIsNone(extract_table_grid(doc[physical_page - 1], bbox, diagnostics=diagnostics))
                self.assertEqual(diagnostics[0]["code"], "table_merged_or_missing_cells")
        self.assertFalse(doc.is_dirty)


    def test_explicit_caption_prefix_keeps_native_body_and_repeated_header(self):
        doc = self.document()
        page = doc.new_page(width=400, height=300)
        self.draw_grid(page, [["Description", "Formula"], ["A natural statement", "p => q"],
                              ["Another statement", "x^2 + 1"]], top=100)
        page.draw_rect([30, 30, 370, 100])
        page.draw_line((30, 65), (370, 65))
        page.insert_text((35, 87), "TABLE 1 Protected formulas", fontname="hebo", fontsize=10)
        original = [30, 30, 370, 205]
        before = self.xref_snapshot(doc)
        diagnostics = []
        grid = extract_table_body_grid(page, original, 100, diagnostics=diagnostics)
        self.assertIsNotNone(grid, diagnostics)
        self.assertEqual(grid["bbox"], [30, 100, 370, 205])
        self.assertEqual(grid["original_bbox"], original)
        self.assertEqual(grid["row_count"], 3)
        self.assertEqual(grid["header_rows"], 1)
        self.assertTrue(grid["repeat_header"])
        self.assertEqual(len(grid["excluded_prefix_rows"]), 2)
        self.assertIn("TABLE 1", grid["excluded_prefix_rows"][1]["text"])
        self.assertGreater(grid["evidence"]["excluded_caption_source_chars"], 0)
        self.assertEqual(grid["evidence"]["unassigned_source_chars"], 0)
        self.assertEqual(grid["rows"][1][1]["text"], "p => q")
        self.assertFalse(grid["rows"][1][1]["translatable"])
        self.assertEqual(before, self.xref_snapshot(doc))
        for bad_top in (112, 135):
            diagnostics = []
            self.assertIsNone(extract_table_body_grid(page, original, bad_top, diagnostics=diagnostics))
            self.assertIn(diagnostics[0]["code"], {"table_body_boundary_unverified",
                                                  "table_caption_prefix_not_full_width"})

    def test_caption_helper_rejects_merged_body_and_unverified_prefix(self):
        doc = self.document()
        page = doc.new_page(width=400, height=330)
        page.draw_rect([30, 30, 370, 240])
        for y in (65, 100, 135, 170, 205):
            page.draw_line((30, y), (370, y))
        page.draw_line((220, 100), (220, 135))
        page.draw_line((220, 170), (220, 240))
        page.insert_text((35, 87), "TABLE 2 Caption", fontsize=10)
        page.insert_text((35, 122), "Description", fontname="hebo", fontsize=10)
        page.insert_text((225, 122), "Value", fontname="hebo", fontsize=10)
        page.insert_text((35, 157), "Merged data must remain unsupported", fontsize=9)
        page.insert_text((35, 192), "One", fontsize=10)
        page.insert_text((225, 192), "1", fontsize=10)
        page.insert_text((35, 227), "Two", fontsize=10)
        page.insert_text((225, 227), "2", fontsize=10)
        diagnostics = []
        self.assertIsNone(extract_table_body_grid(page, [30, 30, 370, 240], 100, diagnostics=diagnostics))
        self.assertEqual(diagnostics[0]["code"], "table_merged_or_missing_cells")
        self.assertIn((3, 1), diagnostics[0]["evidence"]["positions"])
        other = doc.new_page(width=400, height=300)
        self.draw_grid(other, [["Name", "Value"], ["One", "1"]], top=100)
        other.draw_rect([30, 30, 370, 100])
        other.draw_line((30, 65), (370, 65))
        other.insert_text((35, 87), "Merged ordinary data", fontsize=10)
        diagnostics = []
        self.assertIsNone(extract_table_body_grid(other, [30, 30, 370, 170], 100, diagnostics=diagnostics))
        self.assertEqual(diagnostics[0]["code"], "table_caption_prefix_unverified")

    @unittest.skipUnless(ROSEN is not None and ROSEN.is_file(), "Optional Rosen source is not present on this machine")
    def test_actual_rosen_equivalence_body_excludes_only_caption_bands(self):
        doc = pymupdf.open(ROSEN)
        self.docs.append(doc)
        original = [168.00325393676758, 66.95703125, 398.3044891357422, 405.1410217285156]
        before = self.xref_snapshot(doc)
        diagnostics = []
        grid = extract_table_body_grid(doc[51], original, 86.739013671875, diagnostics=diagnostics)
        self.assertIsNotNone(grid, diagnostics)
        self.assertEqual((grid["row_count"], grid["col_count"]), (11, 2))
        self.assertEqual(grid["header_rows"], 1)
        self.assertEqual([cell["text"] for cell in grid["rows"][0]], ["Equivalence", "Name"])
        self.assertEqual([row[1]["text"] for row in grid["rows"][1:]],
                         ["Identity laws", "Domination laws", "Idempotent laws", "Double negation law",
                          "Commutative laws", "Associative laws", "Distributive laws", "De Morgan’s laws",
                          "Absorption laws", "Negation laws"])
        self.assertTrue(all(not row[0]["translatable"] and row[1]["translatable"] for row in grid["rows"][1:]))
        self.assertEqual(grid["evidence"]["excluded_caption_rows"], 2)
        self.assertEqual(grid["evidence"]["source_chars"], 331)
        self.assertEqual(grid["evidence"]["excluded_caption_source_chars"], 26)
        self.assertEqual(grid["evidence"]["unassigned_source_chars"], 0)
        self.assertEqual(before, self.xref_snapshot(doc))
        self.assertFalse(doc.is_dirty)


if __name__ == "__main__":
    unittest.main()
