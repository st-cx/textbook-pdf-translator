"""Real native PDF evidence must gate every CLI paid boundary."""
from contextlib import redirect_stdout
import gzip
from io import StringIO
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import pymupdf as fitz

from textbook_pdf import cli, source_integrity
from textbook_pdf.models import Asset, Book, Element, file_sha256
from textbook_pdf.store import Store


class SourcePreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parent / '_tmp_source_preflight' / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)

    def fixture(self, actual='x+x'):
        source, crop = self.directory / 'source.pdf', self.directory / 'crop.pdf'
        for path, text in ((source, 'x+x'), (crop, actual)):
            with fitz.open() as document:
                page = document.new_page(width=180, height=100)
                page.insert_text((25, 50), text, fontsize=12, fontname='helv')
                document.save(path)
        with fitz.open(source) as document:
            raw = document[0].get_text('rawdict')
            glyphs = [{'id': f'g{n}', 'c': char['c'], 'font': span['font'], 'size': span['size']}
                      for n, (span, char) in enumerate((span, char)
                          for block in raw['blocks'] if 'lines' in block
                          for line in block['lines'] for span in line['spans'] for char in span['chars'])]
        with gzip.open(self.directory / 'ledger.json.gz', 'wt', encoding='utf8') as stream:
            json.dump(glyphs, stream)
        asset = Asset('math', 'formula', 'crop.pdf', 1, [0, 0, 180, 100], 180, 100,
                      text='x+x', sha256=file_sha256(crop),
                      metadata={'pdf_page': 0, 'glyph_ids': [g['id'] for g in glyphs]})
        book = Book(str(source), file_sha256(source), 'Native source preflight', 1, [1],
                    pages=[{'number': 1, 'printed_page': None, 'coverage': {'ledger': 'ledger.json.gz'}}],
                    elements=[Element('body', 'paragraph', 'Use the original ⟦F:math⟧ expression.', 1, asset_ids=['math'])],
                    assets={'math': asset}, profile={'job_dir': str(self.directory.resolve())})
        book.save(self.directory / 'book.json')
        return book

    def invoke(self, stage):
        with redirect_stdout(StringIO()), \
             patch('textbook_pdf.translation.run_translation', return_value={}) as translate, \
             patch('textbook_pdf.translation.run_review', return_value={}) as review, \
             patch('textbook_pdf.layout.compose', side_effect=RuntimeError('Synthetic layout intentionally not run')):
            result = cli.main(['--out', str(self.directory), '--stage', stage,
                               '--review-provider', 'synthetic-review', '--no-legacy-import'])
        return result, translate.call_count, review.call_count

    def test_real_missing_crop_glyph_stops_default_all_before_any_paid_boundary(self):
        self.fixture(actual='x+')
        result, translation_requests, review_requests = self.invoke('all')
        self.assertEqual((translation_requests, review_requests), (0, 0))
        self.assertEqual(result, 2)
        book = Book.load(self.directory / 'book.json')
        self.assertTrue(any(i['code'] == 'source_asset_native_glyphs_missing' for i in book.issues))
        with Store(self.directory / 'tasks.db') as store:
            self.assertTrue(any(i['code'] == 'source_asset_native_glyphs_missing' for i in store.book.issues))
            self.assertEqual(store.db.execute("SELECT count(*) FROM tasks WHERE state!='superseded'").fetchone()[0], 0)

    def test_explicit_translate_and_review_cannot_send_known_bad_native_source(self):
        self.fixture(actual='x+')
        for stage in ('translate', 'review'):
            with self.subTest(stage=stage):
                result, translation_requests, review_requests = self.invoke(stage)
                self.assertEqual((translation_requests, review_requests), (0, 0))
                self.assertEqual(result, 2)

    def test_exact_unchanged_passing_audit_is_reused_without_rerunning_pdf_comparison(self):
        book = self.fixture()
        first = cli._source_preflight(book, self.directory)
        self.assertFalse(first['cache_reused'])
        self.assertFalse(first['blocked'])
        with patch.object(source_integrity, 'audit_native_assets', side_effect=AssertionError('Must reuse exact audit')):
            second = cli._source_preflight(book, self.directory)
        self.assertTrue(second['cache_reused'])
        self.assertFalse(second['blocked'])

    def test_actual_asset_byte_change_invalidates_cache_even_when_descriptor_is_unchanged(self):
        book = self.fixture()
        cli._source_preflight(book, self.directory)
        with (self.directory / 'crop.pdf').open('ab') as stream:
            stream.write(b'\n% immutable source changed\n')
        with patch.object(source_integrity, 'audit_native_assets', wraps=source_integrity.audit_native_assets) as audit:
            result = cli._source_preflight(book, self.directory)
        self.assertEqual(audit.call_count, 1)
        self.assertFalse(result['cache_reused'])
        self.assertTrue(result['blocked'])
        self.assertTrue(any(i['code'] == 'source_asset_native_audit_unavailable' for i in book.issues))

    def test_missing_ledger_invalidates_cache_and_fails_closed(self):
        book = self.fixture()
        cli._source_preflight(book, self.directory)
        (self.directory / 'ledger.json.gz').unlink()
        result = cli._source_preflight(book, self.directory)
        self.assertFalse(result['cache_reused'])
        self.assertTrue(result['blocked'])
        self.assertTrue(any(i['code'] == 'source_asset_ledger_unavailable' for i in book.issues))

    def test_asset_pdf_subpage_change_invalidates_cache(self):
        book = self.fixture()
        cli._source_preflight(book, self.directory)
        book.assets['math'].metadata['pdf_page'] = 1
        result = cli._source_preflight(book, self.directory)
        self.assertFalse(result['cache_reused'])
        self.assertTrue(result['blocked'])

    def test_unbound_apparently_passing_report_is_not_evidence(self):
        book = self.fixture()
        report = {'schema_version': 2, 'read_only': True, 'source_sha256': book.source_sha256,
                  'binding_unchanged': True, 'eligible_assets': 1, 'checked_assets': 1,
                  'passed_assets': 1, 'missing_glyph_assets': 0, 'unverifiable_assets': 0,
                  'all_eligible_glyph_counts_present': True, 'issues': []}
        with patch.object(source_integrity, 'audit_native_assets', return_value=report):
            result = cli._source_preflight(book, self.directory)
        self.assertTrue(result['blocked'])
        self.assertTrue(any(i['code'] == 'source_preflight_unavailable' for i in book.issues))

    def test_zero_count_cache_cannot_turn_real_native_assets_into_not_applicable(self):
        book = self.fixture()
        cli._source_preflight(book, self.directory)
        path = self.directory / 'source_integrity/source-native-glyph-audit.json'
        cached = json.loads(path.read_text('utf8'))
        cached.update(eligible_assets=0, checked_assets=0, passed_assets=0,
                      all_eligible_glyph_counts_present=None, status='not_applicable')
        path.write_text(json.dumps(cached), encoding='utf8')
        with patch.object(source_integrity, 'audit_native_assets', wraps=source_integrity.audit_native_assets) as audit:
            result = cli._source_preflight(book, self.directory)
        self.assertEqual(audit.call_count, 1)
        self.assertFalse(result['cache_reused'])
        self.assertFalse(result['blocked'])

    def test_unstable_audit_retains_original_diagnostic_alongside_fail_closed_error(self):
        book = self.fixture()
        report = source_integrity.audit_native_assets(book, self.directory, self.directory / 'separate-audit')
        report.update(binding_unchanged=False, all_eligible_glyph_counts_present=False)
        report['issues'] = [{'code': 'source_asset_audit_binding_changed', 'severity': 'error',
                             'detail': 'Synthetic concurrent source change'}]
        with patch.object(source_integrity, 'audit_native_assets', return_value=report):
            result = cli._source_preflight(book, self.directory)
        self.assertTrue(result['blocked'])
        self.assertTrue({'source_asset_audit_binding_changed', 'source_preflight_unavailable'}
                        <= {i['code'] for i in book.issues})

    def test_repaired_source_clears_only_previous_preflight_diagnostics(self):
        book = self.fixture(actual='x+')
        book.issues.append({'code': 'independent_review', 'severity': 'warning', 'detail': 'Keep independent history'})
        self.assertTrue(cli._source_preflight(book, self.directory)['blocked'])
        shutil.copyfile(self.directory / 'source.pdf', self.directory / 'crop.pdf')
        book.assets['math'].sha256 = file_sha256(self.directory / 'crop.pdf')
        result = cli._source_preflight(book, self.directory)
        self.assertFalse(result['blocked'])
        self.assertEqual([i['code'] for i in book.issues], ['independent_review'])

    def test_unavailable_auditor_never_opens_paid_boundary(self):
        self.fixture()
        with patch.object(source_integrity, 'audit_native_assets', side_effect=PermissionError('Synthetic unavailable evidence')):
            result, translation_requests, review_requests = self.invoke('all')
        self.assertEqual((translation_requests, review_requests), (0, 0))
        self.assertEqual(result, 2)
        book = Book.load(self.directory / 'book.json')
        self.assertTrue(any(i['code'] == 'source_preflight_unavailable' for i in book.issues))

    def test_no_owned_native_assets_does_not_invoke_auditor_or_download_models(self):
        book = Book('synthetic.pdf', 'sha', 'No assets', 1, [1],
                    elements=[Element('body', 'paragraph', 'Pure synthetic prose.', 1)])
        with patch.object(source_integrity, 'audit_native_assets', side_effect=AssertionError('No native assets')), \
             patch.object(source_integrity, 'audit_binding', side_effect=AssertionError('No native assets')):
            result = cli._source_preflight(book, self.directory)
        self.assertEqual(result['status'], 'not_applicable')
        self.assertFalse(result['blocked'])


if __name__ == '__main__':
    unittest.main()
