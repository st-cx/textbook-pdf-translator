"""A bad page must not lose its neighbours or masquerade as translated."""
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import pymupdf as fitz

from textbook_pdf.models import Book, Element, digest, file_sha256
from textbook_pdf.reading_draft import compose_reading_draft, ReadingDraftCancelled


class ReadingDraftTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / '_tmp_reading_draft' / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.source = self.root / 'original.pdf'
        with fitz.open() as pdf:
            for index in range(3):
                page = pdf.new_page(width=300, height=400)
                page.insert_text((30, 80), f'Original source page {index + 1}')
            pdf.save(self.source)
        self.book = Book(str(self.source.resolve()), file_sha256(self.source), 'Original demo', 3, [1, 2, 3],
                         pages=[{'number': n, 'width': 300, 'height': 400, 'printed_page': str(n + 20)} for n in (1, 2, 3)],
                         elements=[Element(f'e{n}', 'paragraph', f'Original page {n}.', n, [30, 50, 270, 80]) for n in (1, 2, 3)])

    def fake_compose(self, book, translations, outdir, output_path=None, **kwargs):
        outdir = Path(outdir)
        if 2 in book.selected_pages:
            raise RuntimeError('A deliberately unrenderable source page')
        with fitz.open() as pdf:
            for n in book.selected_pages:
                page = pdf.new_page(width=300, height=400)
                page.insert_text((30, 80), f'Translated draft source {n}')
            pdf.save(output_path)
        mapping = {'pages': [{'page': i + 1, 'source_pages': [n]} for i, n in enumerate(book.selected_pages)]}
        (outdir / 'layout_map.json').write_text(json.dumps(mapping), encoding='utf-8')
        return {'pdf_path': str(output_path), 'layout_map_path': str(outdir / 'layout_map.json'),
                'page_count': len(book.selected_pages), 'issues': []}

    def run_fake(self, translations=None, **kwargs):
        with patch('textbook_pdf.layout._Runtime.find'), patch('textbook_pdf.layout.compose', side_effect=self.fake_compose):
            return compose_reading_draft(self.book, translations or {'e1': '中文一', 'e2': '中文二'}, self.root, **kwargs)

    def test_bad_page_is_retained_in_order_and_not_counted_as_chinese(self):
        before = digest(asdict(self.book))
        result = self.run_fake()
        self.assertEqual(result['source_to_output'], {'1': [1], '2': [2], '3': [3]})
        self.assertEqual(result['fallback_source_pages'], [2])
        self.assertEqual(result['translated_elements'], 2)
        self.assertEqual(result['displayed_translated_elements'], 1)
        self.assertFalse(result['accepted'])
        self.assertFalse(result['protected_asset_pixels_checked'])
        self.assertEqual(digest(asdict(self.book)), before)
        with fitz.open(result['pdf_path']) as pdf:
            self.assertEqual(len(pdf), 3)
            self.assertIn('Original source page 2', pdf[1].get_text())
            self.assertIn('原书 p.22', pdf[1].get_text())
            self.assertAlmostEqual(pdf[1].rect.height, 444)
        self.assertEqual(file_sha256(self.source), self.book.source_sha256)

    def test_unchanged_successful_parts_reused_translation_change_invalidates(self):
        self.run_fake()
        second = self.run_fake()
        self.assertEqual(second['parts_reused'], 2)
        third = self.run_fake({'e1': '另一译文', 'e2': '中文二'})
        # Page 1 changes; page 3 remains an untranslated, cached draft.
        self.assertEqual(third['parts_reused'], 1)

    def test_changed_source_and_source_overwrite_rejected(self):
        with self.assertRaises(ValueError):
            self.run_fake(output_path=self.source)
        with fitz.open(self.source) as pdf:
            pdf.set_metadata({'title': 'changed'})
            pdf.save(self.root / 'changed.pdf')
        shutil.copyfile(self.root / 'changed.pdf', self.source)
        with self.assertRaises(ValueError):
            self.run_fake()

    def test_corrupted_cached_pdf_is_rebuilt(self):
        result = self.run_fake()
        first = next(p for p in result['parts'] if p['source_pages'] == [1])
        Path(first['pdf_path']).write_bytes(b'not a PDF')
        rebuilt = self.run_fake()
        self.assertEqual(rebuilt['page_count'], 3)
        with fitz.open(first['pdf_path']) as pdf:
            self.assertEqual(len(pdf), 1)

    def test_deleted_cached_mapping_rebuilds_the_affected_part(self):
        result = self.run_fake()
        first = next(p for p in result['parts'] if p['source_pages'] == [1])
        Path(first['layout_map_path']).unlink()
        rebuilt = self.run_fake()
        self.assertEqual(rebuilt['page_count'], 3)
        self.assertTrue(Path(first['layout_map_path']).is_file())

    def test_missing_mapping_is_not_silently_omitted(self):
        def incomplete(book, translations, outdir, output_path=None, **kwargs):
            result = self.fake_compose(book, translations, outdir, output_path, **kwargs)
            Path(result['layout_map_path']).write_text('{"pages": []}', encoding='utf-8')
            return result
        with patch('textbook_pdf.layout._Runtime.find'), patch('textbook_pdf.layout.compose', side_effect=incomplete):
            result = compose_reading_draft(self.book, {}, self.root)
        self.assertEqual(result['fallback_source_pages'], [1, 2, 3])
        self.assertEqual(set(result['source_to_output']), {'1', '2', '3'})

    def test_missing_engine_is_one_actionable_failure_not_three_original_pages(self):
        with patch('textbook_pdf.layout._Runtime.find', side_effect=RuntimeError('engine missing')):
            with self.assertRaisesRegex(RuntimeError, 'engine missing'):
                compose_reading_draft(self.book, {}, self.root)
        self.assertFalse((self.root / 'reading_draft_report.json').exists())

    def test_measured_layout_failure_is_isolated(self):
        def measured(book, translations, outdir, output_path=None, **kwargs):
            result = self.fake_compose(book, translations, outdir, output_path, **kwargs)
            if 1 in book.selected_pages:
                result['issues'] = [{'code': 'asset_out_of_page', 'severity': 'error'}]
            return result
        with patch('textbook_pdf.layout._Runtime.find'), patch('textbook_pdf.layout.compose', side_effect=measured):
            result = compose_reading_draft(self.book, {}, self.root)
        self.assertEqual(result['fallback_source_pages'], [1, 2])

    def test_duplicate_selected_pages_rejected(self):
        self.book.selected_pages = [1, 1]
        with self.assertRaises(ValueError):
            self.run_fake()

    def test_existing_formal_pdf_is_never_overwritten(self):
        target = self.root / 'existing.zh.pdf'
        shutil.copyfile(self.source, target)
        before = file_sha256(target)
        with self.assertRaises(ValueError):
            self.run_fake(output_path=target)
        self.assertEqual(file_sha256(target), before)

    def test_out_of_range_mapping_falls_back_instead_of_false_page_labels(self):
        def wrong(book, translations, outdir, output_path=None, **kwargs):
            result = self.fake_compose(book, translations, outdir, output_path, **kwargs)
            Path(result['layout_map_path']).write_text(json.dumps({'pages': [
                {'page': 99, 'source_pages': book.selected_pages}]}), encoding='utf-8')
            return result
        with patch('textbook_pdf.layout._Runtime.find'), patch('textbook_pdf.layout.compose', side_effect=wrong):
            result = compose_reading_draft(self.book, {}, self.root)
        self.assertEqual(result['fallback_source_pages'], [1, 2, 3])
        self.assertEqual(result['source_to_output'], {'1': [1], '2': [2], '3': [3]})

    def test_changed_scan_image_cannot_reuse_a_cached_translation(self):
        from PIL import Image
        image = self.root / 'scan.png'
        Image.new('RGB', (20, 20), 'white').save(image)
        self.book.profile['job_dir'] = str(self.root.resolve())
        self.book.pages[0]['scan_evidence'] = {'image': 'scan.png', 'image_file_sha256': file_sha256(image)}
        with patch('textbook_pdf.layout._Runtime.find'), patch('textbook_pdf.layout.compose', side_effect=self.fake_compose), \
                patch('textbook_pdf.scan_layout.compose', side_effect=self.fake_compose):
            compose_reading_draft(self.book, {}, self.root)
            Image.new('RGB', (20, 20), 'gray').save(image)
            result = compose_reading_draft(self.book, {}, self.root)
        self.assertIn(1, result['fallback_source_pages'])
        self.assertEqual(result['parts_reused'], 1)

    def test_cancel_after_one_saved_part_preserves_previous_merged_output(self):
        result = self.run_fake()
        before = file_sha256(result['pdf_path'])
        with self.assertRaises(ReadingDraftCancelled):
            self.run_fake(event_callback=lambda event: False)
        self.assertEqual(file_sha256(result['pdf_path']), before)


if __name__ == '__main__':
    unittest.main()
