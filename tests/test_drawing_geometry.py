"""Hatching outside a source clip is not missing diagram content."""
import unittest
import pymupdf as fitz
from textbook_pdf.drawing_geometry import source_drawings


class DrawingGeometryTests(unittest.TestCase):
    def test_native_clip_stack_preserves_visible_paths_and_explains_invisible_paths(self):
        with fitz.open() as doc:
            page = doc.new_page(width=160, height=120)
            page.draw_line((1, 1), (2, 2))
            doc.update_stream(page.get_contents()[0],
                b'q 40 50 30 30 re W n 0 0 m 140 140 l S 100 100 m 120 120 l S Q 10 10 m 20 20 l S')
            drawings = source_drawings(page)
            self.assertEqual(len(drawings), 3)
            visible, hidden, outside_group = drawings
            self.assertFalse(visible['fully_source_clipped'])
            self.assertTrue(fitz.Rect(40, 40, 70, 70).contains(visible['rect']))
            self.assertTrue(hidden['fully_source_clipped'])
            self.assertFalse(outside_group['fully_source_clipped'])
            self.assertEqual(outside_group['source_clip_bbox'], [0, 0, 160, 120])
            self.assertEqual([d['source_drawing_index'] for d in drawings], [0, 1, 2])
            self.assertTrue(all(d['source_clipping_evidence'] for d in drawings))

    def test_thin_visible_rules_are_not_misclassified_as_empty(self):
        with fitz.open() as doc:
            page = doc.new_page(width=100, height=100)
            page.draw_line((10, 30), (80, 30), width=.4)
            record = source_drawings(page)[0]
            self.assertFalse(record['fully_source_clipped'])
            self.assertGreater(record['rect'].height, 0)


if __name__ == '__main__':
    unittest.main()
