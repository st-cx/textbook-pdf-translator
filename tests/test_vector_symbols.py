"""A native symbol drawn as curves must not disappear from prose."""
from pathlib import Path
import shutil
import unittest
import uuid

import pymupdf as fitz

from textbook_pdf.drawing_geometry import source_drawings
from textbook_pdf.extraction import _collect, _attach_inline_vector_symbols, _finalize_assets
from textbook_pdf.models import Book, Element, TOKEN_RE


class InlineVectorSymbolTests(unittest.TestCase):
    def test_unique_line_end_vector_is_bound_to_prose_without_guessing_unicode(self):
        out = Path(__file__).parent / '_tmp_vector_symbol' / uuid.uuid4().hex
        (out / 'assets').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, out)
        with fitz.open() as doc:
            page = doc.new_page(width=200, height=100)
            page.insert_text((25, 60), 'Meet at', fontsize=10)
            lines, glyphs = _collect(page)
            end = max(g['bbox'][2] for g in glyphs)
            page.draw_polyline([(end+1, 60), (end+2, 54), (end+3, 60.5)], width=.4)
            book = Book('synthetic.pdf', 'source', 'Fixture', 1, [1])
            book._active_glyphs = glyphs
            element = Element('body', 'paragraph', 'Meet at', 1, lines[0]['bbox'],
                              metadata={'glyph_ids': [g['id'] for g in glyphs]})
            for glyph in glyphs:
                glyph.update(owner=element.id, disposition='translation-source')
            _attach_inline_vector_symbols(book, doc, page, out, [element], glyphs, source_drawings(page))
            self.assertEqual(len(TOKEN_RE.findall(element.text)), 1)
            asset = book.assets[element.asset_ids[0]]
            self.assertEqual(asset.metadata['unicode_identity'], 'not_inferred')
            self.assertEqual(asset.metadata['association_proof']['unique_paragraph_tail_glyph'], glyphs[-1]['id'])
            # The same path cannot be attached twice when the stage is resumed.
            _attach_inline_vector_symbols(book, doc, page, out, [element], glyphs, source_drawings(page))
            self.assertEqual(len(element.asset_ids), 1)
            _finalize_assets(book, 1, out)
            with fitz.open(out / asset.path) as bundle:
                self.assertTrue(bundle[asset.metadata['pdf_page']].get_drawings())


if __name__ == '__main__':
    unittest.main()
