"""CLI scan options and source QA integration; all recognition is mocked/local."""
from copy import deepcopy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import numpy as np
from PIL import Image

from textbook_pdf.cli import main
from textbook_pdf.models import Book, Element, file_sha256
from textbook_pdf.qa import verify
from textbook_pdf.scan_quality import audit_scan_page
from textbook_pdf.store import Store


class ScanCliQaTests(unittest.TestCase):
    def setUp(self):
        self.base=Path(__file__).resolve().parent/'_tmp_scan_cli_qa';self.base.mkdir(exist_ok=True)
        self.directory=self.base/uuid.uuid4().hex;self.directory.mkdir()
        self.count=0

    def tearDown(self):
        self.assertTrue(self.directory.resolve().is_relative_to(self.base.resolve()))
        shutil.rmtree(self.directory)

    def fixture(self, *, low=False, omitted=False):
        self.count+=1
        out=self.directory/f'job-{self.count}';out.mkdir();(out/'scan').mkdir()
        image=np.full((80,120,3),230,np.uint8);image[15:30,15:65]=20
        if omitted:image[50:65,90:110]=20
        path=out/'scan/p0001.png';Image.fromarray(image).save(path)
        words=[{'bbox':[15,15,65,30],'text':'A source','confidence':.5 if low else .99}]
        quality=audit_scan_page(image,[{'bbox':[15,15,65,30],'owner':'ocr-e1'}],words)
        page={'number':1,'printed_page':None,'coverage':{'total_glyphs':0,'total_ocr_units':1,'basis':'raster-ink-and-ocr-units','unresolved':0},
              'scan_evidence':{'image':'scan/p0001.png','image_sha256':hashlib.sha256(image.tobytes()).hexdigest(),
                               'dpi':300,'image_size':[120,80],'quality':quality,'ocr_identity':{'name':'local-test-recognizer','model_sha256':'test-model'},
                               'original_composed_page_preserved':True,'recognition_accuracy':None,'optical_recognition_verified':False}}
        book=Book('fixture.pdf','source-sha','Scan fixture',1,[1],pages=[page],profile={'job_dir':str(out)})
        return out,book,path,quality

    def analyze_fixture(self):
        source=self.directory/'source.pdf';source.write_bytes(b'PDF source identity fixture')
        book=Book(str(source),file_sha256(source),'CLI fixture',1,[1])
        return source,book

    def test_analyze_forwards_explicit_scan_parameters_without_api(self):
        source,book=self.analyze_fixture();out=self.directory/'analysis'
        corrections=self.directory/'review.json';corrections.write_text('{}',encoding='utf-8')
        with patch('textbook_pdf.extraction.analyze',return_value=book) as analyze, \
             patch('textbook_pdf.translation.run_translation',side_effect=AssertionError('No external API')), \
             contextlib.redirect_stdout(io.StringIO()):
            result=main([str(source),'--out',str(out),'--stage','analyze','--ocr','force','--ocr-dpi','450',
                         '--scan-corrections',str(corrections),'--backend','native','--no-legacy-import'])
        self.assertEqual(result,0)
        kwargs=analyze.call_args.kwargs
        self.assertEqual(kwargs['ocr'],'force');self.assertEqual(kwargs['ocr_dpi'],450)
        self.assertEqual(kwargs['corrections'],str(corrections));self.assertEqual(kwargs['backend'],'native')
        self.assertTrue((out/'source_analysis_report.json').is_file())

    def test_analyze_default_and_off_options_are_explicit(self):
        source,book=self.analyze_fixture()
        for option,expected in [([], 'auto'),(['--ocr','off'],'off')]:
            with patch('textbook_pdf.extraction.analyze',return_value=book) as analyze,contextlib.redirect_stdout(io.StringIO()):
                result=main([str(source),'--out',str(self.directory/f'analysis-{expected}'),'--stage','analyze','--no-legacy-import',*option])
            self.assertEqual(result,0);self.assertEqual(analyze.call_args.kwargs['ocr'],expected)
            self.assertEqual(analyze.call_args.kwargs['ocr_dpi'],300)
            self.assertIsNone(analyze.call_args.kwargs['corrections'])

    def test_invalid_scan_dpi_rejects_before_recognition(self):
        for value in ['149','601']:
            with patch('textbook_pdf.extraction.analyze') as analyze,contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(['fake.pdf','--out',str(self.directory/'never-created'),'--stage','analyze','--ocr-dpi',value])
            self.assertEqual(error.exception.code,2);analyze.assert_not_called()
        self.assertFalse((self.directory/'never-created').exists())

    def test_existing_translate_ignores_scan_options_and_does_not_reanalyze(self):
        out=self.directory/'existing';out.mkdir()
        book=Book('original.pdf','source-sha','Existing',1,[1],elements=[Element('e1','paragraph','Source text.',1)])
        book.save(out/'book.json');before=(out/'book.json').read_bytes()
        with patch('textbook_pdf.extraction.analyze',side_effect=AssertionError('Must not rerun OCR')) as analyze, \
             patch('textbook_pdf.translation.run_translation',return_value={'requests':0}) as translate, \
             contextlib.redirect_stdout(io.StringIO()):
            result=main(['--out',str(out),'--stage','translate','--ocr','force','--scan-corrections','nonexistent.json','--no-legacy-import'])
        self.assertEqual(result,0);analyze.assert_not_called();translate.assert_called_once()
        self.assertEqual((out/'book.json').read_bytes(),before)

    def test_existing_layout_does_not_reanalyze_or_call_provider(self):
        out=self.directory/'layout-existing';out.mkdir()
        book=Book('original.pdf','source-sha','Existing',1,[1],elements=[Element('e1','paragraph','Source text.',1)])
        book.save(out/'book.json')
        with Store(out/'tasks.db') as store:
            store.register(book,{});store.put_candidate('e1','原文。','local-agent')
        before=(out/'book.json').read_bytes()
        with patch('textbook_pdf.extraction.analyze',side_effect=AssertionError('Must not OCR')) as analyze, \
             patch('textbook_pdf.translation.run_translation',side_effect=AssertionError('Must not call API')), \
             patch('textbook_pdf.layout.compose',return_value={'pdf_path':None}) as compose, \
             contextlib.redirect_stdout(io.StringIO()):
            result=main(['--out',str(out),'--stage','layout','--ocr','force','--scan-corrections','nonexistent.json','--no-legacy-import'])
        self.assertEqual(result,0);analyze.assert_not_called();compose.assert_called_once()
        self.assertEqual((out/'book.json').read_bytes(),before)

    def test_zero_native_glyphs_and_full_ink_do_not_claim_ocr_accuracy(self):
        out,book,image,quality=self.fixture()
        report=verify(book,{},out)
        scan=report['scan_source_checks']
        self.assertEqual(scan['scanned_pages'],1);self.assertEqual(scan['total_ocr_units'],1)
        self.assertEqual(scan['ink_coverage_ratio'],1)
        self.assertEqual(scan['source_images_verified'],1)
        self.assertEqual(scan['pages'][0]['native_pdf_glyphs'],0)
        self.assertFalse(scan['optical_recognition_verified']);self.assertIsNone(scan['recognition_accuracy'])
        self.assertEqual(scan['status'],'review_required')
        html=(out/'质量报告.html').read_text(encoding='utf-8')
        self.assertIn('扫描来源检查',html);self.assertIn('墨迹归属不证明OCR准确',html)
        self.assertIn('原PDF字形为0不代表识别完整',html)
        self.assertFalse(any(x['code']=='scan_source_image_changed' for x in report['issues']))

    def test_scan_quality_error_cannot_disappear_when_not_copied_to_book_issues(self):
        out,book,_,quality=self.fixture(omitted=True)
        report=verify(book,{},out)
        self.assertFalse(report['technical_checks_passed'])
        self.assertTrue(any(x['code']=='scan_unassigned_ink' and x['severity']=='error' for x in report['issues']))
        self.assertGreater(report['scan_source_checks']['unassigned_ink_pixels'],0)

    def test_scan_summary_preserves_existing_gate_and_deduplicates_quality_issue(self):
        out,book,_,quality=self.fixture(omitted=True)
        issue=next(x for x in quality['issues'] if x['code']=='scan_unassigned_ink')
        book.issues=[{'code':'original_source_blocker','severity':'error','detail':'Original gate remains','page':1},
                     {'code':issue['code'],'severity':issue['severity'],'detail':issue['detail'],'page':1}]
        report=verify(book,{},out)
        self.assertFalse(report['technical_checks_passed'])
        self.assertEqual(sum(x['code']=='scan_unassigned_ink' for x in report['issues']),1)
        self.assertTrue(any(x['code']=='original_source_blocker' for x in report['issues']))

    def test_low_confidence_is_separate_from_pixel_coverage(self):
        out,book,_,_=self.fixture(low=True)
        report=verify(book,{},out)
        self.assertEqual(report['scan_source_checks']['ink_coverage_ratio'],1)
        self.assertEqual(report['scan_source_checks']['low_confidence_ocr_units'],1)
        self.assertTrue(any(x['code']=='scan_low_confidence_ocr' for x in report['issues']))

    def test_source_png_is_verified_by_rgb_bytes_and_changed_pixels_block(self):
        out,book,image,_=self.fixture()
        self.assertNotEqual(file_sha256(image),book.pages[0]['scan_evidence']['image_sha256'])
        good=verify(book,{},out)
        self.assertEqual(good['scan_source_checks']['source_images_verified'],1)
        altered=np.asarray(Image.open(image).convert('RGB')).copy();altered[0,0]=[0,0,0];Image.fromarray(altered).save(image)
        bad=verify(book,{},out)
        self.assertFalse(bad['technical_checks_passed'])
        self.assertEqual(bad['scan_source_checks']['source_images_verified'],0)
        self.assertTrue(any(x['code']=='scan_source_image_changed' for x in bad['issues']))

    def test_missing_quality_and_zero_glyphs_are_unknown_not_complete(self):
        out,book,_,_=self.fixture();del book.pages[0]['scan_evidence']['quality']
        report=verify(book,{},out);scan=report['scan_source_checks']
        self.assertFalse(report['technical_checks_passed'])
        self.assertEqual(scan['pages_without_quality_evidence'],1)
        self.assertIsNone(scan['ink_coverage_ratio']);self.assertFalse(scan['raster_coverage_complete'])
        self.assertTrue(any(x['code']=='scan_raster_evidence_invalid' for x in report['issues']))

    def test_unsupported_saved_accuracy_claim_is_not_adopted(self):
        out,book,_,_=self.fixture();book.pages[0]['scan_evidence'].update(optical_recognition_verified=True,recognition_accuracy=1.0)
        report=verify(book,{},out);scan=report['scan_source_checks']
        self.assertFalse(scan['optical_recognition_verified']);self.assertIsNone(scan['recognition_accuracy'])
        self.assertTrue(any(x['code']=='scan_recognition_claim_unsupported' for x in report['issues']))

    def test_mixed_book_does_not_count_native_glyphs_as_ocr_units(self):
        out,book,_,_=self.fixture();book.page_count=2;book.selected_pages=[1,2]
        book.pages.append({'number':2,'coverage':{'total_glyphs':500,'unresolved':0}})
        report=verify(book,{},out);scan=report['scan_source_checks']
        self.assertEqual(scan['scanned_pages'],1);self.assertEqual(scan['source_pages'],[1])
        self.assertEqual(scan['total_ocr_units'],1)

    def test_native_only_qa_has_no_new_scan_warnings(self):
        out=self.directory/'native';out.mkdir()
        book=Book('native.pdf','sha','Native',1,[1],pages=[{'number':1,'coverage':{'total_glyphs':0,'unresolved':0}}])
        report=verify(book,{},out)
        self.assertEqual(report['scan_source_checks']['status'],'not_applicable')
        self.assertEqual(report['errors'],0);self.assertEqual(report['warnings'],0)

    def test_image_path_cannot_escape_job_roots(self):
        out,book,_,_=self.fixture();outside=self.directory/'outside.png';Image.new('RGB',(120,80)).save(outside)
        book.pages[0]['scan_evidence']['image']='../outside.png'
        report=verify(book,{},out)
        self.assertTrue(any(x['code']=='scan_source_image_missing' for x in report['issues']))
        self.assertFalse(report['technical_checks_passed'])


if __name__=='__main__':unittest.main()
