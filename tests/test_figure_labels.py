"""A diagram's translated labels must not silently become untranslated pixels."""
from pathlib import Path
import shutil
import unittest
import uuid

import pymupdf as fitz

from textbook_pdf.extraction import _asset, _collect, _detach_figure_labels, _finalize_assets
from textbook_pdf.models import Book, Element
from textbook_pdf.layout import compose, _Runtime, CompositionError
from textbook_pdf.visual_checks import compare_assets


class FigureLabelExtractionTests(unittest.TestCase):
    def test_actual_chinese_labels_are_measured_once_and_background_ink_passes(self):
        try:
            _Runtime.find()
        except CompositionError:
            self.skipTest('Pinned Typst unavailable')
        out = Path(__file__).parent / '_tmp_figure_labels' / uuid.uuid4().hex
        (out / 'assets').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, out)
        with fitz.open() as doc:
            page = doc.new_page(width=260, height=150)
            page.insert_text((35, 60), 'Start', fontsize=10, color=(0, 174 / 255, 239 / 255))
            page.insert_text((155, 60), 'Finish', fontsize=10)
            page.draw_line((80, 56), (140, 56))
            doc.save(out / 'synthetic.pdf')
            lines, glyphs = _collect(page)
            book = Book(str(out / 'synthetic.pdf'), 'source', 'Synthetic diagram', 1, [1],
                        profile={'job_dir': str(out.resolve()), 'page_width': 260, 'page_height': 220})
            book._active_glyphs = glyphs
            original = _asset(book, doc, page, out, 'figure', [25, 40, 205, 80])
            parent = Element('diagram', 'figure', '', 1, [25, 40, 205, 80],
                             asset_ids=[original.id], metadata={'glyph_ids': [g['id'] for g in glyphs]})
            for glyph in glyphs:
                glyph.update(owner=parent.id, disposition='preserved-original')
            elements = [parent]
            _detach_figure_labels(book, doc, page, out, elements, lines, glyphs)
            book.elements = elements
            _finalize_assets(book, 1, out)
        labels = [e for e in elements if e.kind == 'figure_label']
        result = compose(book, {e.id: {'Start': '开始', 'Finish': '结束'}[e.text] for e in labels}, out / 'rendered')
        import json
        mapping = json.loads(Path(result['layout_map_path']).read_text('utf8'))
        for label in labels:
            self.assertEqual(len(mapping['elements'][label.id]), 1)
        with fitz.open(result['pdf_path']) as rendered:
            text = ''.join(p.get_text() for p in rendered)
            self.assertEqual(text.count('开始'), 1)
            self.assertEqual(text.count('结束'), 1)
            self.assertNotIn('Start', text)
            chinese_colors = {
                span['text']: span['color']
                for page in rendered for block in page.get_text('dict')['blocks']
                if 'lines' in block for line in block['lines'] for span in line['spans']
                if span['text'] in {'开始', '结束'}
            }
            self.assertEqual(chinese_colors.get('开始'), 0x00AEEF)
            self.assertEqual(chinese_colors.get('结束'), 0x000000)
        checks = compare_assets(book, mapping, result['pdf_path'], out / 'checks')
        self.assertEqual(checks['checked'], 1, checks['issues'])
        self.assertEqual(checks['passed'], 1, checks['issues'])
        self.assertFalse(checks['metrics'][0]['label_semantics_certified'])

    def test_text_is_detached_while_original_arrow_is_retained(self):
        out = Path(__file__).parent / '_tmp_figure_labels' / uuid.uuid4().hex
        (out / 'assets').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, out)
        doc = fitz.open()
        self.addCleanup(doc.close)
        page = doc.new_page(width=260, height=150)
        page.insert_text((35, 60), 'Start', fontsize=10)
        page.insert_text((155, 60), 'Finish', fontsize=10)
        page.draw_line((80, 56), (140, 56))
        lines, glyphs = _collect(page)
        book = Book('synthetic.pdf', 'source', 'Synthetic diagram', 1, [1])
        book._active_glyphs = glyphs
        # Same bbox and empty text deliberately exercise exclusion-aware IDs.
        original = _asset(book, doc, page, out, 'figure', [25, 40, 205, 80])
        parent = Element('diagram', 'figure', '', 1, [25, 40, 205, 80],
                         asset_ids=[original.id], metadata={'glyph_ids': [g['id'] for g in glyphs]})
        for g in glyphs:
            g.update(owner=parent.id, disposition='preserved-original')
        elements = [parent]
        _detach_figure_labels(book, doc, page, out, elements, lines, glyphs)
        labels = [e for e in elements if e.kind == 'figure_label']
        self.assertEqual({e.text for e in labels}, {'Start', 'Finish'})
        self.assertEqual(set(parent.metadata['figure_label_ids']), {e.id for e in labels})
        self.assertTrue(all(e.metadata['parent_figure_id'] == parent.id for e in labels))
        self.assertNotEqual(parent.asset_ids, [original.id])
        self.assertNotIn(parent.id, {g['owner'] for g in glyphs})
        _finalize_assets(book, 1, out)
        background = book.assets[parent.asset_ids[0]]
        with fitz.open(out / background.path) as bundle:
            image = bundle[background.metadata['pdf_page']]
            self.assertEqual(image.get_text().strip(), '')
            self.assertTrue(any(d['rect'].width >= 60 for d in image.get_drawings()))
        self.assertEqual(book.assets[original.id].metadata['superseded_by'], background.id)


if __name__ == '__main__':
    unittest.main()
