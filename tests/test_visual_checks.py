"""Protected-object visual checks against deliberately damaged native PDF forms."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import numpy as np
import pymupdf

from textbook_pdf.models import Asset, Book, Element, file_sha256
from textbook_pdf.qa import verify
from textbook_pdf import visual_checks


# Actual-book calibration is opt-in; synthetic tests never inspect private jobs.
REAL_JOB = Path(os.environ["TEXTBOOK_TEST_RENDERED_JOB"]).expanduser() if os.environ.get("TEXTBOOK_TEST_RENDERED_JOB") else None
REAL_MAP = (Path(os.environ["TEXTBOOK_TEST_LAYOUT_MAP"]).expanduser() if os.environ.get("TEXTBOOK_TEST_LAYOUT_MAP") else REAL_JOB / "layout_map.json") if REAL_JOB else None


class VisualAssetTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "_tmp_visual"
        self.base.mkdir(exist_ok=True)
        self.directory = self.base / uuid.uuid4().hex
        self.directory.mkdir()
        self.case_number = 0

    def tearDown(self):
        resolved, boundary = self.directory.resolve(), self.base.resolve()
        self.assertTrue(resolved.is_relative_to(boundary))
        self.assertNotEqual(resolved, boundary)
        shutil.rmtree(resolved)

    @staticmethod
    def paint_formula(page, *, bar=True, radical=True, superscript=True, dy=0, foreign=False):
        page.insert_text((11, 32 + dy), "x", fontname="tiit", fontsize=18)
        page.insert_text((36, 15 + dy), "a", fontname="tiit", fontsize=12)
        page.insert_text((36, 35 + dy), "b", fontname="tiit", fontsize=12)
        if bar:
            page.draw_line((29, 21 + dy), (57, 21 + dy), width=.7)
        if radical:
            shape = page.new_shape()
            shape.draw_polyline([(3, 23 + dy), (6, 26 + dy), (9, 13 + dy), (23, 13 + dy)])
            shape.finish(width=.7)
            shape.commit()
        if superscript:
            page.insert_text((22, 17 + dy), "2", fontname="tiro", fontsize=8)
        if foreign:
            page.insert_text((60, 17), "BAD", fontname="helv", fontsize=8)

    def fixture(self, *, mutation=None, pale_background=False, tiny=False, blank_source=False):
        self.case_number += 1
        folder = self.directory / str(self.case_number)
        folder.mkdir()
        source_path, target_path = folder / "source-bundle.pdf", folder / "target.pdf"
        width, height = (3, 3) if tiny else (80, 42)
        with pymupdf.open() as source:
            decoy = source.new_page(width=width, height=height)
            decoy.draw_rect([0, 0, width, height], fill=(0, 0, 0))
            page = source.new_page(width=width, height=height)
            if tiny and not blank_source:
                page.draw_circle((1.5, 1.5), .35, color=(0, 0, 0), fill=(0, 0, 0), width=.2)
            elif not blank_source:
                self.paint_formula(page)
                # Hidden off-page source objects must not become expected ink.
                page.insert_text((3, -4), "HIDDEN", fontsize=10)
            source.save(source_path)
        bbox = [40.13, 60.27, 40.13 + width, 60.27 + height]
        with pymupdf.open() as target, pymupdf.open(source_path) as source:
            page = target.new_page(width=220, height=180)
            if pale_background:
                page.draw_rect(bbox, color=None, fill=(.88, .93, 1))
            if mutation == "blank":
                pass
            elif mutation:
                with pymupdf.open() as modified:
                    modified_page = modified.new_page(width=width, height=height)
                    self.paint_formula(modified_page, **mutation)
                    page.show_pdf_page(pymupdf.Rect(bbox), modified, 0)
            else:
                page.show_pdf_page(pymupdf.Rect(bbox), source, 1)
            target.save(target_path)
        asset = Asset("math", "formula", str(source_path), 1, [0, 0, width, height], width, height,
                      text="sqrt(x)^2 + a/b", sha256=file_sha256(source_path), metadata={"pdf_page": 1})
        book = Book("original.pdf", "fixture-book", "Fixture", 1, [1], assets={asset.id: asset})
        mapping = {"assets": {"math": [{"page": 1, "bbox": bbox, "element_id": "e1", "precision": "measured"}]}}
        return book, mapping, target_path, folder / "checks"

    @staticmethod
    def qa_inputs(book, mapping):
        book.elements = [Element("e1", "exercise", "Prove ⟦F:math⟧ has 2 cases.", 1, asset_ids=["math"])]
        mapping["elements"] = {"e1": [{"page": 1, "bbox": mapping["assets"]["math"][0]["bbox"]}]}
        mapping["pages"] = [{"page": 1, "source_pages": [1]}]
        return {"e1": "证明 ⟦F:math⟧ 有 2 种情况。"}

    def test_assembled_source_label_counts_formula_once_with_verified_glyph_transfer(self):
        book,mapping,pdf,out=self.fixture();translations=self.qa_inputs(book,mapping)
        parent=book.elements[0];parent.metadata['glyph_ids']=['label-glyph','body-glyph']
        child=Element('source-label','sidebar','⟦F:math⟧:',1,asset_ids=['math'],metadata={
            'render_suppressed':True,'preserve_only':True,'translatable':False,'superseded_by':parent.id,
            'glyph_ids':[],'source_glyph_ids':['label-glyph'],'exclusion_reason':'Label assembled with its statement'})
        book.elements.insert(0,child);mapping['elements'][child.id]=mapping['elements'][parent.id]
        report=verify(book,translations,out,pdf_path=pdf,layout_map=mapping)
        self.assertFalse(any(i['code']=='formula_not_rendered' for i in report['issues']))
        self.assertEqual(report['assembled_source_elements'][0]['replacement_element_id'],parent.id)
        child.metadata['source_glyph_ids']=['untransferred-original-glyph']
        report=verify(book,translations,out/'unproven',pdf_path=pdf,layout_map=mapping)
        self.assertTrue(any(i['code']=='formula_not_rendered' for i in report['issues']))
        self.assertEqual(report['assembled_source_elements'],[])

    def test_source_glyph_ids_from_different_pages_do_not_prove_label_transfer(self):
        from textbook_pdf.qa import assembled_source_dispositions
        parent=Element('parent','paragraph','⟦F:f⟧ statement.',2,metadata={'glyph_ids':['b0.l0.s0.c0']})
        child=Element('label','sidebar','⟦F:f⟧:',1,metadata={'render_suppressed':True,'preserve_only':True,
            'exclusion_reason':'label assembled','superseded_by':'parent','glyph_ids':[],
            'source_glyph_ids':['b0.l0.s0.c0']})
        book=Book('original.pdf','same','Test',2,[1,2],elements=[child,parent])
        self.assertEqual(assembled_source_dispositions(book),{})

    def test_measured_margin_annotation_clipped_by_page_is_blocking(self):
        book,mapping,pdf,out=self.fixture();translations=self.qa_inputs(book,mapping)
        mapping['elements']['margin-note']=[{'page':1,'bbox':[-48,50,40,70]}]
        # The clipped word may disappear from PDF text extraction; its measured
        # placement still proves that the actual annotation leaves the sheet.
        report=verify(book,translations,out,pdf_path=pdf,layout_map=mapping)
        found=[i for i in report['issues'] if i['code']=='rendered_object_outside_page']
        self.assertEqual(len(found),1);self.assertEqual(found[0]['object_id'],'margin-note')
        self.assertFalse(report['technical_checks_passed'])

    def test_intact_native_form_passes_with_exact_bundle_page_and_no_source_edits(self):
        book, mapping, pdf, out = self.fixture()
        before = file_sha256(book.assets["math"].path)
        with patch.object(pymupdf.Page, "get_text", side_effect=AssertionError("Use visible pixels, not extracted text")):
            report = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["metrics"][0]["source_bundle_page"], 2)
        self.assertTrue(report["all_eligible_occurrences_checked"])
        self.assertEqual(file_sha256(book.assets["math"].path), before)

    def test_archived_components_require_active_replacement_and_never_count_as_passes(self):
        book,mapping,pdf,out=self.fixture()
        translations=self.qa_inputs(book,mapping)
        archived=deepcopy(book.assets['math']);archived.id='old-component'
        archived.metadata.update(combined_into=['math'],exclusion_reason='Original component is included in the active assembled vector')
        book.assets[archived.id]=archived
        report=visual_checks.compare_assets(book,mapping,pdf,out)
        self.assertEqual((report['checked'],report['passed'],report['skipped']),(1,1,0))
        self.assertEqual(len(report['archived_asset_dispositions']),1)
        self.assertEqual(report['issues'],[])
        summary=verify(book,translations,out/'qa',pdf_path=pdf,layout_map=mapping)['visual_asset_checks']
        self.assertEqual(summary['total_book_assets'],2)
        self.assertEqual(summary['eligible_source_assets'],1)
        self.assertEqual(summary['archived_source_assets'],1)
        self.assertFalse(summary['all_book_assets_passed'])
        archived.metadata['combined_into']=['missing-replacement']
        failed=visual_checks.compare_assets(book,mapping,pdf,out/'invalid-replacement')
        self.assertEqual(failed['archived_asset_dispositions'],[])
        self.assertEqual(failed['skipped'],1)
        self.assertTrue(any(i['code']=='visual_asset_trace_missing' for i in failed['issues']))

    def test_archived_table_chain_requires_complete_current_grid_cells(self):
        book,mapping,pdf,out=self.fixture()
        self.qa_inputs(book,mapping)
        grid={'rows':[['label']],'evidence':{'complete_rectangular_grid':True,'unassigned_source_chars':0}}
        book.elements.extend([Element('grid','table','',1,metadata={'table_grid':grid}),
                              Element('label','table_cell','Name',1,metadata={'parent_table_id':'grid'})])
        old=deepcopy(book.assets['math']);old.id='old-table';old.kind='table'
        old.metadata.update(superseded_by='intermediate-table',exclusion_reason='Source title detached')
        middle=deepcopy(old);middle.id='intermediate-table';middle.metadata.update(superseded_by='grid',exclusion_reason='Verified body rebuilt from translated cells')
        book.assets[old.id]=old;book.assets[middle.id]=middle
        result=visual_checks.compare_assets(book,mapping,pdf,out)
        self.assertEqual(len(result['archived_asset_dispositions']),2)
        self.assertTrue(all(e['replacement_is_translated_grid'] for e in result['archived_asset_dispositions']))
        self.assertEqual((result['checked'],result['passed'],result['skipped']),(1,1,0))
        book.elements.pop()
        broken=visual_checks.compare_assets(book,mapping,pdf,out/'missing-cell')
        self.assertEqual(broken['archived_asset_dispositions'],[])
        self.assertEqual(broken['skipped'],2)

    def test_missing_fraction_bar_radical_and_superscript_are_flagged(self):
        for mutation in ({"bar": False}, {"radical": False}, {"superscript": False}):
            with self.subTest(mutation=mutation):
                book, mapping, pdf, out = self.fixture(mutation=mutation)
                report = visual_checks.compare_assets(book, mapping, pdf, out)
                self.assertEqual(report["checked"], 1)
                self.assertEqual(report["passed"], 0)
                metric = report["metrics"][0]
                self.assertGreater(metric["missing_pixels_outside_tolerance"], 2)
                self.assertTrue(any(i["code"] in {"visual_asset_ink_mismatch", "visual_asset_severely_incomplete"}
                                    for i in report["issues"]))
                self.assertTrue(Path(metric["evidence"]["source_png"]).is_file())
                self.assertTrue(Path(metric["evidence"]["target_png"]).is_file())

    def test_content_baseline_shift_inside_measured_box_is_flagged(self):
        book, mapping, pdf, out = self.fixture(mutation={"dy": 2})
        report = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(report["passed"], 0)
        self.assertGreater(abs(report["metrics"][0]["centroid_delta_pixels"][1]), 1)
        self.assertTrue(report["issues"])

    def test_foreign_text_contamination_is_flagged(self):
        book, mapping, pdf, out = self.fixture(mutation={"foreign": True})
        report = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(report["passed"], 0)
        self.assertGreater(report["metrics"][0]["extra_pixels_outside_tolerance"], 2)
        self.assertEqual(report["issues"][0]["code"], "visual_asset_ink_mismatch")

    def test_pale_background_changes_do_not_masquerade_as_formula_damage(self):
        book, mapping, pdf, out = self.fixture(pale_background=True)
        report = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["passed"], 1, report["issues"])
        self.assertNotEqual(report["metrics"][0]["source_background_gray"], report["metrics"][0]["target_background_gray"])

    def test_one_grayscale_level_on_a_serif_edge_is_antialias_not_damage(self):
        source = np.full((40, 40), 255, np.uint8)
        target = source.copy()
        source[10:20, 5:10] = target[10:20, 5:10] = 0
        source[20:30, 25] = 223
        target[20:30, 26] = 224
        status, code, metrics = visual_checks._compare(source, target)
        self.assertEqual(status, "passed")
        self.assertIsNone(code)
        self.assertEqual(metrics["missing_pixels_outside_tolerance"], 0)

    def test_tiny_missing_asset_is_an_error_and_blank_source_never_passes(self):
        book, mapping, pdf, out = self.fixture(tiny=True, mutation="blank")
        report = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["passed"], 0)
        self.assertGreater(report["metrics"][0]["source_ink_pixels"], 0)
        self.assertEqual(report["issues"][0]["code"], "visual_asset_blank_or_missing")
        self.assertEqual(report["issues"][0]["severity"], "error")
        book, mapping, pdf, out = self.fixture(tiny=True, blank_source=True, mutation="blank")
        report = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(report["passed"], 0)
        self.assertEqual(report["issues"][0]["code"], "visual_source_has_no_visible_ink")
        self.assertEqual(report["metrics"][0]["status"], "unverifiable")

    def test_imprecise_or_missing_measurements_never_receive_a_pass(self):
        book, mapping, pdf, out = self.fixture()
        mapping["assets"]["math"][0]["precision"] = "block-fragment"
        report = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(report["checked"], 0)
        self.assertEqual(report["passed"], 0)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["issues"][0]["code"], "visual_asset_trace_imprecise")
        self.assertFalse(report["all_eligible_occurrences_checked"])
        mapping["assets"].clear()
        report = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(report["passed"], 0)
        self.assertEqual(report["issues"][0]["code"], "visual_asset_trace_missing")

    def test_duplicate_trace_is_checked_once_and_limits_are_not_global_passes(self):
        book, mapping, pdf, out = self.fixture()
        mapping["assets"]["math"].append(deepcopy(mapping["assets"]["math"][0]))
        with patch.object(visual_checks, "_bitmap", wraps=visual_checks._bitmap) as raster:
            report = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(report["deduplicated"], 1)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["passed"], 1)
        self.assertEqual(raster.call_count, 2)  # One source object and one output page.
        limited = visual_checks.compare_assets(book, mapping, pdf, out, limit=0)
        self.assertEqual(limited["checked"], 0)
        self.assertEqual(limited["passed"], 0)
        self.assertEqual(limited["limited"], 1)
        self.assertFalse(limited["all_eligible_occurrences_checked"])

    def test_changed_source_bundle_is_rejected_before_comparison(self):
        book, mapping, pdf, out = self.fixture()
        path = Path(book.assets["math"].path)
        with path.open("ab") as stream:
            stream.write(b"\n% Deliberately changed test fixture\n")
        report = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(report["checked"], 0)
        self.assertEqual(report["passed"], 0)
        self.assertEqual(report["issues"][0]["code"], "visual_asset_source_changed")
        self.assertEqual(report["issues"][0]["severity"], "error")

    def test_figures_compare_only_with_unchanged_geometry_and_explicit_overlay_exclusions(self):
        book, mapping, pdf, out = self.fixture()
        book.assets["math"].kind = "figure"
        original = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(original["passed"], 1)
        self.assertEqual(original["eligible_source_assets_by_kind"], {"figure": 1})
        mapping["assets"]["math"][0]["bbox"][2] += 2
        resized = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(resized["checked"], 0)
        self.assertEqual(resized["passed"], 0)
        self.assertEqual(resized["issues"][0]["code"], "visual_asset_geometry_changed_unverified")
        mapping["assets"]["math"][0]["bbox"][2] -= 2
        book.assets["math"].metadata["visual_check_exclusion_reason"] = "Translated diagram labels are intentional overlays"
        overlay = visual_checks.compare_assets(book, mapping, pdf, out)
        self.assertEqual(overlay["passed"], 0)
        self.assertEqual(overlay["skipped"], 1)
        self.assertEqual(overlay["issues"][0]["code"], "visual_asset_intentional_change_unverified")

    def test_qa_json_html_explain_formula_and_figure_coverage(self):
        book, mapping, pdf, out = self.fixture()
        translations = self.qa_inputs(book, mapping)
        figure = deepcopy(book.assets["math"])
        figure.id, figure.kind = "figure", "figure"
        book.assets["figure"] = figure
        figure_bbox = [130.13, 60.27, 210.13, 102.27]
        new_pdf = pdf.with_name("target-with-figure.pdf")
        with pymupdf.open(pdf) as output, pymupdf.open(figure.path) as source:
            output[0].show_pdf_page(pymupdf.Rect(figure_bbox), source, 1)
            output.save(new_pdf)
        book.elements.append(Element("e2", "figure", "", 1, asset_ids=["figure"]))
        mapping["elements"]["e2"] = [{"page": 1, "bbox": figure_bbox}]
        mapping["assets"]["figure"] = [{"page": 1, "bbox": figure_bbox, "element_id": "e2", "precision": "measured"}]
        report = verify(book, translations, out, pdf_path=new_pdf, layout_map=mapping)
        summary = report["visual_asset_checks"]
        self.assertEqual(summary["total_book_assets"], 2)
        self.assertEqual(summary["eligible_source_assets_by_kind"], {"formula": 1, "figure": 1})
        self.assertEqual(summary["checked_occurrences"], 2)
        self.assertEqual(summary["passed_occurrences"], 2)
        self.assertTrue(summary["all_book_assets_passed"])
        self.assertEqual(report["manual_visual_review"], "not_claimed")
        saved = json.loads((out / "quality_report.json").read_text("utf-8"))
        self.assertEqual(saved["visual_asset_checks"], summary)
        html = (out / "质量报告.html").read_text("utf-8")
        self.assertIn("本书资产共 2 个", html)
        self.assertIn("公式 1", html)
        self.assertIn("图形 1", html)
        self.assertIn("适用输出位置 2 次", html)
        self.assertIn("不能代替整页观感", html)

    def test_qa_distinguishes_review_warnings_from_missing_ink_errors(self):
        book, mapping, pdf, out = self.fixture(mutation={"bar": False})
        translations = self.qa_inputs(book, mapping)
        report = verify(book, translations, out, pdf_path=pdf, layout_map=mapping)
        self.assertEqual(report["visual_asset_checks"]["errors"], 0)
        self.assertEqual(report["visual_asset_checks"]["review_warnings"], 1)
        self.assertEqual(report["visual_asset_checks"]["status"], "review_required")
        self.assertFalse(report["visual_asset_checks"]["all_book_assets_passed"])
        book, mapping, pdf, out = self.fixture(tiny=True, mutation="blank")
        mapping["elements"], mapping["pages"] = {}, [{"page": 1, "source_pages": [1]}]
        blank = verify(book, {}, out, pdf_path=pdf, layout_map=mapping)
        self.assertEqual(blank["visual_asset_checks"]["errors"], 1)
        self.assertFalse(blank["technical_checks_passed"])
        self.assertEqual(blank["acceptance_status"], "incomplete")

    def test_qa_explicitly_reports_unsupported_asset_kinds_as_unverified(self):
        book, mapping, pdf, out = self.fixture()
        book.assets["math"].kind = "custom-asset"
        mapping["elements"], mapping["pages"] = {}, [{"page": 1, "source_pages": [1]}]
        report = verify(book, {}, out, pdf_path=pdf, layout_map=mapping)
        summary = report["visual_asset_checks"]
        self.assertEqual(summary["excluded_source_assets"], 1)
        self.assertEqual(summary["excluded_source_assets_by_kind"], {"custom-asset": 1})
        self.assertEqual(summary["passed_occurrences"], 0)
        self.assertFalse(summary["all_book_assets_passed"])
        self.assertTrue(any(i["code"] == "visual_asset_scope_excluded" for i in report["issues"]))

    @unittest.skipUnless(REAL_JOB is not None and REAL_MAP is not None and REAL_MAP.is_file() and (REAL_JOB / "book.json").is_file(),
                         "Optional locally rendered Typst preview is not present")
    def test_actual_typst_embedding_calibrates_one_pixel_antialias_tolerance(self):
        book = Book.load(REAL_JOB / "book.json")
        mapping = json.loads(REAL_MAP.read_text(encoding="utf-8"))
        report = visual_checks.compare_assets(book, mapping, mapping["pdf_path"], self.directory / "real-calibration", limit=50)
        self.assertEqual(report["checked"], 50)
        self.assertGreaterEqual(report["passed"], 49)
        self.assertFalse(any(i["severity"] == "error" for i in report["issues"]))
        self.assertEqual(report["parameters"]["antialias_tolerance_pixels"], 1)
        self.assertFalse(report["all_eligible_occurrences_checked"])


if __name__ == "__main__":
    unittest.main()
