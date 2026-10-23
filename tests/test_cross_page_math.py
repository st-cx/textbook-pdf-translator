"""Source continuation is explicit and output segments remain atomic."""
from copy import deepcopy
from pathlib import Path
import json
import shutil
import unittest
import uuid

import pymupdf as fitz

from textbook_pdf.cross_page_math import link_cross_page_formulas, check_joined_math_geometry
from textbook_pdf.extraction import _asset, _collect, _finalize_assets
from textbook_pdf.models import Book, Element
from textbook_pdf.layout import compose, _partition_book, _Runtime, CompositionError


class CrossPageMathTests(unittest.TestCase):
    def fixture(self):
        out = Path(__file__).parent / '_tmp_cross_page_math' / uuid.uuid4().hex
        (out / 'assets').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, out)
        book = Book(str((out / 'source.pdf').resolve()), 'fixture', 'Fixture', 2, [1, 2],
                    profile={'job_dir': str(out.resolve()), 'layout_chunk_source_pages': 1})
        with fitz.open() as doc:
            for n, text, y in ((1, '(x +', 250), (2, 'y)', 50)):
                page = doc.new_page(width=260, height=300)
                # Synthetic source owns embedded bytes; production has no Base14 fallback.
                page.insert_font(fontname='fixture-sans',fontbuffer=fitz.Font('helv').buffer)
                page.insert_text((40, y), text, fontsize=12,fontname='fixture-sans')
                lines, glyphs = _collect(page)
                book._active_glyphs = glyphs
                asset = _asset(book, doc, page, out, 'formula', lines[0]['bbox'], text, y,
                               {'glyph_ids': [g['id'] for g in glyphs]})
                element = Element('e'+str(n), 'paragraph', ('Use ' if n == 1 else '') +
                                  '⟦F:'+asset.id+'⟧' + (' to count.' if n == 2 else ''), n,
                                  [40, y-12, 200, y+4], printed_page=str(30+n), asset_ids=[asset.id])
                book.elements.append(element)
                book.pages.append({'number': n, 'width': 260, 'height': 300, 'printed_page': str(30+n)})
                book.issues.append({'code': 'formula_boundary_review', 'severity': 'error',
                                    'element_id': element.id, 'page': n, 'detail': 'Source boundary: '+text})
                _finalize_assets(book, n, out)
            doc.save(out / 'source.pdf')
        return book, out

    def test_adjacent_pages_keep_all_source_identities_and_floating_table_does_not_interrupt(self):
        book, _ = self.fixture()
        original = {e.id: (e.text, e.asset_ids[:]) for e in book.elements}
        floating = Element('table', 'table', '', 2, [40, 10, 200, 30])
        book.elements.insert(1, floating)
        groups = link_cross_page_formulas(book)
        self.assertEqual(len(groups), 1)
        self.assertEqual([e.id for e in book.elements], ['e1', 'e2', 'table'])
        self.assertEqual(original, {e.id: (e.text, e.asset_ids) for e in book.elements if e.id != 'table'})
        self.assertEqual(book.issues, [])
        self.assertEqual(len(book.profile['resolved_source_issues']), 2)
        self.assertEqual(len(_partition_book(book)), 1)
        book.profile.pop('cross_page_math_groups')
        self.assertEqual(len(link_cross_page_formulas(book)), 1)

    def test_unbalanced_or_nonadjacent_fragments_remain_blocking(self):
        book, _ = self.fixture()
        right = book.assets[book.elements[1].asset_ids[0]]
        right.text = 'y'
        self.assertEqual(link_cross_page_formulas(book), [])
        self.assertEqual(len(book.issues), 2)
        right.text = 'y)'
        book.pages[1]['number'] = 3
        self.assertEqual(link_cross_page_formulas(book), [])

    def test_actual_typst_joins_segments_once_at_original_size_and_labels_both_pages(self):
        try:
            _Runtime.find()
        except CompositionError:
            self.skipTest('Pinned Typst unavailable')
        book, out = self.fixture()
        link_cross_page_formulas(book)
        first, second = book.elements
        result = compose(book, {first.id: '使用 ⟦F:'+first.asset_ids[0]+'⟧',
                                second.id: '⟦F:'+second.asset_ids[0]+'⟧ 进行计数。'}, out / 'rendered')
        mapping = json.loads(Path(result['layout_map_path']).read_text('utf8'))
        self.assertEqual(check_joined_math_geometry(book, mapping['assets']), [])
        for aid in book.assets:
            self.assertEqual(len(mapping['assets'][aid]), 1)
        with fitz.open(result['pdf_path']) as doc:
            self.assertIn('原书 pp.31–32', ''.join(p.get_text() for p in doc))
        broken = deepcopy(mapping['assets'])
        broken[second.asset_ids[0]][0]['page'] += 1
        self.assertEqual(check_joined_math_geometry(book, broken)[0]['code'], 'cross_page_formula_not_atomic')


if __name__ == '__main__':
    unittest.main()
