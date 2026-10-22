"""Native asset audits compare real PDF glyph counts, never empty evidence."""
from pathlib import Path
import gzip
import json
import shutil
import unittest
import uuid
from unittest.mock import patch

import pymupdf as fitz

from textbook_pdf.models import Asset, Book, Element, file_sha256
from textbook_pdf.source_integrity import audit_binding, audit_native_assets, _native_counts


class NativeSourceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / '_tmp_source_integrity' / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)

    def fixture(self, original='x+x', actual=None, *, morph=None, font='helv', size=12):
        source = self.directory / 'source.pdf'
        crop = self.directory / 'crop.pdf'
        with fitz.open() as document:
            page = document.new_page(width=180, height=100)
            page.insert_text((25, 50), original, fontsize=size, fontname=font, morph=morph)
            document.save(source)
        with fitz.open(source) as document:
            raw = document[0].get_text('rawdict', flags=fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_PRESERVE_WHITESPACE)
            glyphs = [{'id': f'g{n}', 'c': c['c'], 'font': span['font'], 'size': span['size']}
                      for n, (span, c) in enumerate((span, c)
                          for block in raw['blocks'] if 'lines' in block
                          for line in block['lines'] for span in line['spans'] for c in span['chars'])]
        if actual is None:
            shutil.copy2(source, crop)
        else:
            with fitz.open() as document:
                page = document.new_page(width=180, height=100)
                if actual:
                    page.insert_text((25, 50), actual, fontsize=size, fontname=font, morph=morph)
                document.save(crop)
        with gzip.open(self.directory / 'ledger.json.gz', 'wt', encoding='utf8') as stream:
            json.dump(glyphs, stream)
        asset = Asset('math', 'formula', 'crop.pdf', 1, [0, 0, 180, 100], 180, 100,
                      text=original, sha256=file_sha256(crop),
                      metadata={'glyph_ids': [g['id'] for g in glyphs], 'pdf_page': 0})
        book = Book(str(source), file_sha256(source), 'Synthetic', 1, [1],
                    pages=[{'number': 1, 'coverage': {'ledger': 'ledger.json.gz'}}],
                    assets={'math': asset},
                    elements=[Element('body', 'paragraph', '⟦F:math⟧', 1, asset_ids=['math'])],
                    profile={'job_dir': str(self.directory.resolve())})
        return book

    def audit(self, book):
        report = audit_native_assets(book, self.directory, self.directory / 'audit')
        self.assertEqual(report, json.loads((self.directory / 'audit/source-native-glyph-audit.json').read_text('utf8')))
        return report

    def test_complete_real_counter_passes(self):
        report = self.audit(self.fixture())
        self.assertEqual((report['eligible_assets'], report['checked_assets'], report['passed_assets']), (1, 1, 1))
        self.assertEqual(report['selected_source_glyphs'], 3)
        self.assertTrue(report['all_eligible_glyph_counts_present'])

    def test_duplicate_character_counts_detect_one_missing_x(self):
        report = self.audit(self.fixture(actual='x+'))
        self.assertEqual(report['missing_glyph_assets'], 1)
        self.assertFalse(report['all_eligible_glyph_counts_present'])
        issue = report['issues'][0]
        self.assertEqual(issue['missing_glyph_count'], 1)
        self.assertEqual(issue['missing_identities'][0]['character'], 'x')
        self.assertEqual(issue['missing_identities'][0]['count'], 1)

    def test_qa_cannot_bypass_a_missing_native_source_glyph(self):
        from textbook_pdf.qa import verify
        book = self.fixture(actual='x+')
        report = verify(book, {'body': '⟦F:math⟧'}, self.directory / 'qa')
        self.assertFalse(report['technical_checks_passed'])
        missing = [i for i in report['issues'] if i.get('code') == 'source_asset_native_glyphs_missing']
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]['severity'], 'error')
        self.assertEqual(missing[0]['missing_glyph_count'], 1)
        self.assertEqual(report['native_source_glyph_checks']['missing_glyph_assets'], 1)

    def test_hash_change_is_unverifiable_not_passed_or_counted_checked(self):
        book = self.fixture()
        with (self.directory / 'crop.pdf').open('ab') as stream:
            stream.write(b'\n% changed after immutable descriptor\n')
        report = self.audit(book)
        self.assertEqual((report['eligible_assets'], report['checked_assets'], report['unverifiable_assets']), (1, 0, 1))
        self.assertFalse(report['all_eligible_glyph_counts_present'])
        self.assertEqual(report['issues'][0]['reason'], 'Immutable source bundle hash changed')

    def test_missing_page_ledger_stays_in_denominator(self):
        book = self.fixture()
        (self.directory / 'ledger.json.gz').unlink()
        report = self.audit(book)
        self.assertEqual((report['eligible_assets'], report['checked_assets'], report['unverifiable_assets']), (1, 0, 1))
        self.assertFalse(report['all_eligible_glyph_counts_present'])
        self.assertEqual(report['page_results'][0]['unverifiable'], 1)

    def test_whitespace_only_expected_is_not_a_pass(self):
        book = self.fixture(original='x')
        with gzip.open(self.directory / 'ledger.json.gz', 'wt', encoding='utf8') as stream:
            json.dump([{'id': 'g0', 'c': ' ', 'font': 'Helvetica', 'size': 12}], stream)
        report = self.audit(book)
        self.assertEqual(report['passed_assets'], 0)
        self.assertEqual((report['eligible_assets'], report['checked_assets'], report['unverifiable_assets']), (1, 0, 1))
        self.assertFalse(report['all_eligible_glyph_counts_present'])
        self.assertEqual(report['issues'][0]['code'], 'source_asset_expected_native_glyphs_empty')

    def test_no_eligible_assets_is_not_applicable_not_vacuously_verified(self):
        book = self.fixture()
        book.elements = []
        report = self.audit(book)
        self.assertIsNone(report['all_eligible_glyph_counts_present'])
        self.assertEqual(report['status'], 'not_applicable')

    def test_token_only_reference_is_audited_and_suppressed_owner_is_ignored(self):
        book = self.fixture(actual='x+')
        book.elements[0].asset_ids = []
        book.elements.append(Element('old', 'paragraph', '⟦F:math⟧', 1,
                                     asset_ids=['math'], metadata={'render_suppressed': True}))
        report = self.audit(book)
        self.assertEqual(report['eligible_assets'], 1)
        self.assertEqual(report['issues'][0]['owner_ids'], ['body'])

    def test_anisotropic_native_font_uses_same_rawdict_metric(self):
        book = self.fixture(original='0', morph=(fitz.Point(25, 50), fitz.Matrix(1.1, 1)))
        with fitz.open(self.directory / 'crop.pdf') as document:
            trace_size = document[0].get_texttrace()[0]['size']
            raw_size = document[0].get_text('rawdict')['blocks'][0]['lines'][0]['spans'][0]['size']
        self.assertNotEqual(round(trace_size, 2), round(raw_size, 2), 'Fixture must exercise distinct PDF size metrics')
        report = self.audit(book)
        self.assertEqual(report['passed_assets'], 1)
        self.assertEqual(report['missing_glyph_assets'], 0)

    def test_actual_font_change_is_not_accepted_by_character_only_matching(self):
        book = self.fixture(original='x')
        with fitz.open() as document:
            page = document.new_page(width=180, height=100)
            page.insert_text((25, 50), 'x', fontsize=12, fontname='cour')
            document.save(self.directory / 'different.pdf')
        shutil.copyfile(self.directory / 'different.pdf', self.directory / 'crop.pdf')
        book.assets['math'].sha256 = file_sha256(self.directory / 'crop.pdf')
        report = self.audit(book)
        self.assertEqual(report['missing_glyph_assets'], 1)
        self.assertFalse(report['all_eligible_glyph_counts_present'])

    def test_unknown_selected_id_is_not_a_pass(self):
        book = self.fixture()
        book.assets['math'].metadata['glyph_ids'].append('not-in-ledger')
        report = self.audit(book)
        self.assertEqual((report['eligible_assets'], report['checked_assets'], report['unverifiable_assets']), (1, 0, 1))
        self.assertFalse(report['all_eligible_glyph_counts_present'])

    def test_binding_covers_page_selection_formula_page_and_ledger_contents(self):
        book = self.fixture()
        original = audit_binding(book, self.directory)
        self.assertEqual(original, audit_binding(book, self.directory))
        book.assets['math'].metadata['pdf_page'] = 1
        self.assertNotEqual(original['owned_assets_sha256'], audit_binding(book, self.directory)['owned_assets_sha256'])
        book.assets['math'].metadata['pdf_page'] = 0
        book.selected_pages = [1, 2]
        self.assertNotEqual(original['source_contract_sha256'], audit_binding(book, self.directory)['source_contract_sha256'])
        book.selected_pages = [1]
        with gzip.open(self.directory / 'ledger.json.gz', 'rt', encoding='utf8') as stream:
            records = json.load(stream)
        records[0]['c'] = 'y'
        with gzip.open(self.directory / 'ledger.json.gz', 'wt', encoding='utf8') as stream:
            json.dump(records, stream)
        self.assertNotEqual(original['coverage_ledgers_sha256'], audit_binding(book, self.directory)['coverage_ledgers_sha256'])

    def test_asset_bytes_are_bound_even_when_claimed_descriptor_is_unchanged(self):
        book = self.fixture()
        before = audit_binding(book, self.directory)
        with (self.directory / 'crop.pdf').open('ab') as stream:
            stream.write(b'\n% mutation\n')
        after = audit_binding(book, self.directory)
        self.assertEqual(before['owned_assets_sha256'], after['owned_assets_sha256'])
        self.assertNotEqual(before['asset_files_sha256'], after['asset_files_sha256'])
        self.assertNotEqual(before['signature'], after['signature'])

    def test_mutation_during_counts_invalidates_otherwise_complete_report(self):
        book = self.fixture()
        def mutate_after_read(page):
            counts = _native_counts(page)
            book.assets['math'].metadata['glyph_ids'] = ['g0']
            return counts
        with patch('textbook_pdf.source_integrity._native_counts', side_effect=mutate_after_read):
            report = self.audit(book)
        self.assertEqual(report['passed_assets'], 1, 'Comparisons completed before source changed')
        self.assertFalse(report['binding_unchanged'])
        self.assertFalse(report['all_eligible_glyph_counts_present'])
        self.assertEqual(report['status'], 'blocked')
        self.assertIn('source_asset_audit_binding_changed', [i['code'] for i in report['issues']])


if __name__ == '__main__':
    unittest.main()
