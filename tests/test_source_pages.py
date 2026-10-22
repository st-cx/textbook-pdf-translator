"""Pagination evidence must follow each actual page, including added pages."""
from copy import deepcopy
from pathlib import Path
import shutil
import unittest
import uuid

import pymupdf as fitz

from textbook_pdf.models import Book, Element, file_sha256
from textbook_pdf.source_pages import audit_source_pages, check_output_source_labels, footer_label


class SourcePageTests(unittest.TestCase):
    def setUp(self):
        self.boundary = Path(__file__).resolve().parent/'_tmp_source_pages'
        self.root = self.boundary/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.source = self.root/'source.pdf'
        with fitz.open() as pdf:
            for label in ('iv', '82', ''):
                page = pdf.new_page(width=300, height=400)
                page.insert_text((30, 90), 'Original body.', fontsize=11)
                if label:
                    page.insert_text((270, 25), label, fontsize=9)
            pdf.save(self.source)
        self.book = Book(str(self.source), file_sha256(self.source), 'Fixture', 3, [1, 2, 3],
                         pages=[{'number': i, 'printed_page': label} for i, label in enumerate(('iv', '82', None), 1)],
                         elements=[Element('e1', 'paragraph', 'First', 1, printed_page='iv'),
                                   Element('e2', 'paragraph', 'Second', 2, printed_page='82'),
                                   Element('e3', 'paragraph', 'Unnumbered', 3)])

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.boundary.resolve()))
        self.assertNotEqual(self.root.resolve(), self.boundary.resolve())
        shutil.rmtree(self.root)

    def test_actual_pages_support_roman_arabic_and_explicit_physical_fallback(self):
        report = audit_source_pages(self.book)
        self.assertEqual(report['issues'], [])
        self.assertEqual(report['printed_labels_supported'], 2)
        self.assertEqual(report['physical_fallback_pages'], 1)
        self.assertEqual(report['pages'][2]['display_label'], '原 PDF 第3页')

    def test_constant_offset_guess_and_conflicting_element_label_are_rejected(self):
        self.book.pages[1]['printed_page'] = 'iv'
        report = audit_source_pages(self.book)
        self.assertIn('printed_page_evidence_mismatch', {issue['code'] for issue in report['issues']})
        self.assertIn('element_printed_page_mismatch', {issue['code'] for issue in report['issues']})

    def test_changed_original_cannot_certify_old_pagination(self):
        self.book.source_sha256 = '0'*64
        report = audit_source_pages(self.book)
        self.assertEqual(report['checked_pages'], 0)
        self.assertEqual(report['issues'][0]['code'], 'source_pagination_identity_unverified')

    def test_scan_pagination_is_separate_visual_evidence_not_a_native_mismatch(self):
        self.book.pages[1].update(label_source='scan-ocr-margin',
            coverage={'basis':'raster-ink-and-ocr-units'},scan_evidence={'image_sha256':'fixture-image'})
        report=audit_source_pages(self.book)
        page=report['pages'][1]
        self.assertEqual(page['status'],'scan_label_requires_visual_review')
        self.assertFalse(page['native_margin_glyphs_certified'])
        self.assertEqual(report['scan_labels_requiring_visual_review'],1)
        self.assertFalse(any(i['severity']=='error' for i in report['issues']))
        self.assertEqual(report['printed_labels_supported'],1)

    def test_explicit_pdf_labels_are_distinct_evidence(self):
        with fitz.open(self.source) as pdf:
            pdf.set_page_labels([{'startpage': 2, 'prefix': 'A-', 'style': 'D', 'firstpagenum': 1}])
            pdf.save(self.root/'labels.pdf')
        self.book.source_path = str(self.root/'labels.pdf')
        self.book.source_sha256 = file_sha256(self.book.source_path)
        self.book.pages[2]['printed_page'] = 'A-1'
        report = audit_source_pages(self.book)
        self.assertEqual(report['issues'], [])
        self.assertEqual(report['pages'][2]['status'], 'explicit_pdf_page_label_supported')

    def test_added_pages_repeat_their_actual_original_page(self):
        mapping = {'elements': {'e2': [{'page': 1}, {'page': 2}], 'e3': [{'page': 3}]},
                   'pages': [{'page': 1, 'source_pages': [2], 'source_labels': ['原书 p.82']},
                             {'page': 2, 'source_pages': [2], 'source_labels': ['原书 p.82']},
                             {'page': 3, 'source_pages': [3], 'source_labels': ['原 PDF 第3页']}]}
        self.assertEqual(check_output_source_labels(self.book, mapping), [])
        wrong = deepcopy(mapping)
        wrong['pages'][1]['source_labels'] = ['原书 p.83']
        self.assertEqual(check_output_source_labels(self.book, wrong)[0]['code'], 'output_source_label_mismatch')
        wrong = deepcopy(mapping)
        wrong['pages'][1]['source_pages'] = [1]
        self.assertIn('output_source_page_mapping_mismatch', {issue['code'] for issue in check_output_source_labels(self.book, wrong)})

    def test_range_only_combines_contiguous_arabic_labels(self):
        self.assertEqual(footer_label(['原书 p.82', '原书 p.83']), '原书 pp.82–83')
        self.assertEqual(footer_label(['原书 p.82', '原书 p.84']), '原书 p.82 · 原书 p.84')
        self.assertEqual(footer_label(['原书 p.iv', '原书 p.v']), '原书 p.iv · 原书 p.v')

    def test_missing_or_duplicate_added_page_records_cannot_pass(self):
        mapping={'elements':{'e2':[{'page':1},{'page':2}]},
                 'pages':[{'page':1,'source_pages':[2],'source_labels':['原书 p.82']}]}
        issues=check_output_source_labels(self.book,mapping)
        self.assertEqual(issues,[dict(code='output_source_page_record_missing',severity='error',
            detail='本页有实际源元素，但缺少该成品页的来源记录',output_page=2)])
        mapping['pages'].append(deepcopy(mapping['pages'][0]))
        self.assertIn('output_source_page_record_duplicate',{i['code']for i in check_output_source_labels(self.book,mapping)})

    def test_prefixed_page_wins_over_bottom_math_subscript(self):
        from textbook_pdf.extraction import _collect, _printed_page
        with fitz.open() as pdf:
            page=pdf.new_page(width=600,height=756)
            page.insert_text((50,44),'S-44',fontsize=9)
            page.insert_text((99,712),'99',fontname='tiit',fontsize=6.6)
            lines,_=_collect(page)
            self.assertEqual(_printed_page(page,lines)[0],'S-44')

    def test_bottom_math_number_alone_is_not_a_printed_page(self):
        from textbook_pdf.extraction import _collect, _printed_page
        with fitz.open() as pdf:
            page=pdf.new_page(width=600,height=756)
            page.insert_text((99,712),'99',fontname='tiit',fontsize=6.6)
            lines,_=_collect(page)
            self.assertIsNone(_printed_page(page,lines)[0])

    def test_correct_manifest_does_not_hide_incorrect_visible_footer(self):
        output = self.root/'output.pdf'
        with fitz.open() as pdf:
            page = pdf.new_page(width=300, height=400)
            # CJK built-in font is a synthetic fixture, not a shipped font.
            page.insert_text((30, 375), '原书 p.83', fontname='china-s', fontsize=8)
            pdf.save(output)
        mapping = {'elements': {'e2': [{'page': 1}]},
                   'pages': [{'page': 1, 'source_pages': [2], 'source_labels': ['原书 p.82']}]}
        self.assertEqual(check_output_source_labels(self.book, mapping, output)[0]['code'], 'output_source_footer_missing_or_wrong')
        with fitz.open() as pdf:
            page=pdf.new_page(width=300,height=400)
            page.insert_text((30,375),'原书 p.82',fontname='china-s',fontsize=8)
            pdf.save(self.root/'correct.pdf')
        self.assertEqual(check_output_source_labels(self.book,mapping,self.root/'correct.pdf'),[])


if __name__ == '__main__':
    unittest.main()
