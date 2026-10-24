"""Self-authored narrow paragraphs check the final PDF's glyph placement."""
from pathlib import Path
from collections import Counter
import shutil
import unittest
import uuid

import pymupdf
from textbook_pdf import layout
from textbook_pdf.models import Book, Element


class ActualMathTermWrapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            layout._Runtime.find()
        except layout.CompositionError:
            raise unittest.SkipTest("Pinned Typst runtime is optional")

    def check_term(self, term):
        root = Path(__file__).resolve().parent / "_tmp_math_term_wrap"
        directory = root / uuid.uuid4().hex
        directory.mkdir(parents=True)
        try:
            text = "一二三四五六七八九十" + term + "，这是定义。"
            element = Element("body", "paragraph", "State an asymptotic growth name.",
                              1, [10, 50, 130, 100], printed_page="1")
            book = Book("self-authored.pdf", "self-authored", "Word wrapping fixture", 1, [1],
                        elements=[element],
                        pages=[{"number": 1, "width": 140, "height": 240, "printed_page": "1"}],
                        profile={"page_width": 140, "page_height": 240,
                                 "margins": {"left": 10, "right": 10, "top": 45, "bottom": 45}})
            result = layout.compose(book, {"body": text}, directory / "rendered")
            with pymupdf.open(result["pdf_path"]) as pdf:
                self.assertEqual(len(pdf), 1)
                chars = [(chr(code), list(origin), span["size"])
                         for span in pdf[0].get_texttrace()
                         if abs(span["size"] - 10.5) < .0001
                         for code, gid, origin, bbox in span["chars"]]
            # A line-placement improvement must retain all text at the role size.
            self.assertEqual(Counter(c for c, _, _ in chars if not c.isspace()), Counter(text))
            prefix = [origin for c, origin, _ in chars if c == term[0]]
            symbol = [origin for c, origin, _ in chars if c == term[1]]
            self.assertEqual(len(prefix), 1)
            self.assertEqual(len(symbol), 1)
            self.assertAlmostEqual(prefix[0][1], symbol[0][1], delta=.01)
            self.assertLess(prefix[0][0], symbol[0][0])
        finally:
            self.assertTrue(directory.resolve().is_relative_to(root.resolve()))
            shutil.rmtree(directory)

    def test_big_theta_name_stays_on_one_line(self):
        self.check_term("大Θ")

    def test_big_omega_name_stays_on_one_line(self):
        self.check_term("大Ω")

    def test_big_O_name_stays_on_one_line(self):
        self.check_term("大O")

    def test_little_o_name_stays_on_one_line(self):
        self.check_term("小o")

    def test_little_omega_name_stays_on_one_line(self):
        self.check_term("小ω")


if __name__ == "__main__":
    unittest.main()
