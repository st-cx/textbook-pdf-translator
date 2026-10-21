"""Neural-hint boundary tests use no ONNX inference or external resources."""
from pathlib import Path
import shutil
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch
import uuid

from textbook_pdf import paragraph_adapter as adapter


class ParagraphAdapterTests(unittest.TestCase):
    @staticmethod
    def box(x=338.6998, y=231.1411, x2=345.17354, y2=240.1075):
        return N(x=x, y=y, x2=x2, y2=y2)

    def char(self, text="G", shift=0):
        box=self.box(x=338.6998+shift,x2=345.17354+shift)
        return N(char_unicode=text,box=box,pdf_style=N(font_id="F1"),
                 visual_bbox=N(box=self.box(x=338.9867248+shift,y=231.0155704,
                                           x2=345.0569776+shift,y2=237.2023864)))

    def page(self, chars, output=None):
        output=chars if output is None else output
        para=N(box=self.box(),unicode="".join(c.char_unicode or "" for c in output),
               layout_label="plain text",layout_id=1,
               pdf_paragraph_composition=[N(pdf_line=N(pdf_character=output))])
        return N(pdf_character=chars,pdf_paragraph=[para],
                 page_layout=[N(id=1,box=self.box(),class_name="plain text",conf=.99)])

    def test_nonzero_media_origin_maps_glyph_to_original_visible_page(self):
        page=self.page([self.char()])
        result=adapter._page_hints(page,adapter._source_records(page),28,799.52001953125,
                                   {"crop_offset":[42,0],"rotation":0})
        self.assertTrue(result["usable"])
        glyph=result["source_glyphs"][0]
        self.assertAlmostEqual(glyph["bbox"][0],296.6998)
        self.assertAlmostEqual(glyph["bbox"][1],559.41251953125)
        self.assertAlmostEqual(glyph["visual_bbox"][1],562.31763313125)
        self.assertEqual(result["paragraphs"][0]["source_glyph_indices"],[0])
        self.assertEqual(result["layouts"][0]["class_name"],"plain text")
        self.assertTrue(result["coverage"]["exact"])

    def test_missing_duplicate_and_foreign_glyphs_reject_hints(self):
        first,second=self.char(),self.char("A",shift=10)
        for label,output,metric in [
            ("missing",[first],"unmatched_source_glyphs"),
            ("duplicate",[first,first,second],"duplicated_source_glyphs"),
            ("foreign",[first,second,self.char("X",shift=40)],"unmatched_paragraph_glyphs"),
        ]:
            with self.subTest(label=label):
                page=self.page([first,second],output)
                result=adapter._page_hints(page,adapter._source_records(page),1,799.52,{"crop_offset":[42,0]})
                self.assertFalse(result["usable"])
                self.assertGreater(result["coverage"][metric],0)
                self.assertEqual(result["paragraphs"],[])
                self.assertEqual(result["layouts"],[])
                self.assertEqual(result["source_glyphs"],[])
                self.assertEqual(result["diagnostics"][0]["code"],"neural_paragraph_glyph_coverage")

    def test_unknown_unicode_is_unverified_and_spaces_are_explicitly_excluded(self):
        char,space,unknown=self.char(),self.char(" "),self.char(None)
        page=self.page([char,space], [char,self.char(" ")])
        result=adapter._page_hints(page,adapter._source_records(page),1,799.52,{"crop_offset":[42,0]})
        self.assertTrue(result["usable"])
        self.assertEqual(result["coverage"]["whitespace_excluded"],1)
        page=self.page([char,unknown],[char])
        result=adapter._page_hints(page,adapter._source_records(page),1,799.52,{"crop_offset":[42,0]})
        self.assertFalse(result["usable"])
        self.assertEqual(result["coverage"]["unknown_source_unicode"],1)

    def test_unverified_rotation_or_missing_crop_transform_reject(self):
        page=self.page([self.char()])
        for geometry in ({"crop_offset":[42,0],"rotation":90},{}, {"crop_offset":[float("nan"),0]}):
            with self.subTest(geometry=geometry),self.assertRaises(adapter.ParagraphHintsUnavailable):
                adapter._page_hints(page,adapter._source_records(page),1,799.52,geometry)

    def test_one_invalid_upstream_page_keeps_good_page_and_coverage_guards(self):
        good = self.page([self.char()])
        invalid = self.page([self.char("B")])
        invalid.pdf_paragraph[0].box = self.box(y=240.1075, y2=231.1411)
        first, second = self.char("C"), self.char("D", shift=10)
        missing = self.page([first, second], [first])
        for number, page in enumerate((good, invalid, missing)):
            page.page_number = number
        document = N(page=[good, invalid, missing])
        model = object()
        config = N(progress_monitor=N(disable=False), doc_layout_model=model)
        class NoopStage:
            def __init__(self, config):
                pass
            def process(self, *args):
                pass
        boundary = Path(__file__).resolve().parent / "_tmp_paragraphs"
        boundary.mkdir(exist_ok=True)
        directory = boundary / uuid.uuid4().hex
        directory.mkdir()
        modules = {
            "babeldoc.format.pdf.document_il.midend.layout_parser": N(LayoutParser=NoopStage),
            "babeldoc.format.pdf.document_il.midend.paragraph_finder": N(ParagraphFinder=NoopStage),
            "babeldoc.const": N(close_process_pool=lambda: None),
        }
        try:
            with patch.object(adapter, "_load_model", return_value=(object(), {"checksum_verified": True})), \
                    patch.dict("sys.modules", modules):
                result = adapter.extract_paragraph_hints(document, [N(rect=N(height=799.52))] * 3,
                    config, [281, 282, 283], {page: {"crop_offset": [42, 0]} for page in (281, 282, 283)},
                    directory / "unused-model.onnx", directory)
            self.assertTrue(result["pages"][281]["usable"])
            self.assertTrue(result["pages"][281]["coverage"]["exact"])
            bad = result["pages"][282]
            self.assertFalse(bad["usable"])
            self.assertEqual((bad["paragraphs"], bad["layouts"], bad["source_glyphs"]), ([], [], []))
            self.assertEqual(bad["diagnostics"][0]["code"], "neural_paragraph_geometry_unavailable")
            self.assertEqual(bad["diagnostics"][0]["page"], 282)
            self.assertFalse(bad["coverage"]["geometry_verified"])
            self.assertFalse(result["pages"][283]["usable"])
            self.assertEqual(result["pages"][283]["coverage"]["unmatched_source_glyphs"], 1)
            self.assertEqual(result["pages"][283]["diagnostics"][0]["code"], "neural_paragraph_glyph_coverage")
            self.assertGreater(invalid.pdf_paragraph[0].box.y, invalid.pdf_paragraph[0].box.y2)
            self.assertIs(config.doc_layout_model, model)
            self.assertFalse(config.progress_monitor.disable)
            self.assertTrue((directory / "neural_structure_hints.json").is_file())
        finally:
            self.assertTrue(directory.resolve().is_relative_to(boundary.resolve()))
            shutil.rmtree(directory)

    def test_corrupt_local_model_is_never_loaded_or_downloaded(self):
        boundary=Path(__file__).resolve().parent/"_tmp_paragraphs"
        boundary.mkdir(exist_ok=True)
        directory=boundary/uuid.uuid4().hex
        directory.mkdir()
        try:
            model=directory/"wrong.onnx"
            model.write_bytes(b"not the pinned upstream model")
            with patch.object(adapter,"version",return_value="0.6.4"):
                with self.assertRaisesRegex(adapter.ParagraphHintsUnavailable,"checksum"):
                    adapter._load_model(model)
            with self.assertRaisesRegex(adapter.ParagraphHintsUnavailable,"missing"):
                adapter._load_model(directory/"missing.onnx")
        finally:
            self.assertTrue(directory.resolve().is_relative_to(boundary.resolve()))
            shutil.rmtree(directory)


if __name__=="__main__":
    unittest.main()
