"""Bounds use the exact embedded native glyph, not a Unicode substitute."""
import re
import unittest
from unittest.mock import patch

import pymupdf as fitz

from textbook_pdf import glyph_geometry
from textbook_pdf.extraction import _collect


class GlyphGeometryTests(unittest.TestCase):
    def test_embedded_glyph_identity_and_visible_pixel_bounds_are_retained(self):
        with fitz.open() as document:
            page = document.new_page(width=160, height=120)
            font = fitz.Font('helv')
            page.insert_font(fontname='embedded', fontbuffer=font.buffer)
            page.insert_text((30, 60), '(', fontname='embedded', fontsize=26)
            with patch.object(glyph_geometry, '_TARGET_FONTS', re.compile('.*')):
                _, glyphs = _collect(page)
            glyph = glyphs[0]
            self.assertNotIn('ink_geometry_error', glyph)
            self.assertEqual(glyph['ink_geometry']['method'], 'embedded-native-glyph-render-v1')
            self.assertNotEqual(glyph['bbox'], glyph['nominal_bbox'])
            pix = page.get_pixmap(matrix=fitz.Matrix(8, 8), colorspace=fitz.csGRAY)
            import numpy as np
            ys, xs = np.nonzero(np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width) < 240)
            visible = fitz.Rect(min(xs)/8, min(ys)/8, (max(xs)+1)/8, (max(ys)+1)/8)
            self.assertTrue(fitz.Rect(glyph['bbox']).contains(visible), (glyph, visible))
            # The saved source glyph is still the literal '(' with the same origin.
            self.assertEqual(glyph['c'], '(')
            self.assertEqual(glyph['origin'], [30, 60])

    def test_ambiguous_or_unembedded_math_font_does_not_claim_verified_bounds(self):
        with fitz.open() as document:
            page = document.new_page(width=160, height=120)
            page.insert_text((30, 60), '(', fontsize=26)
            with patch.object(glyph_geometry, '_TARGET_FONTS', re.compile('.*')):
                _, glyphs = _collect(page)
            self.assertIn('ink_geometry_error', glyphs[0])
            self.assertNotIn('ink_geometry', glyphs[0])


if __name__ == '__main__':
    unittest.main()
