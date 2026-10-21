"""Independent scan adapter acceptance boundaries, no OCR model/API calls.

Synthetic PDFs exercise the real scan routing, raster extraction, assets and
checkpoint functions. Mocked OCR/detector outputs are stated source evidence,
not acceptance of OCR accuracy. All files live in a bounded workspace folder.
"""
from contextlib import ExitStack
from copy import deepcopy
import gzip
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import cv2
import numpy as np
from PIL import Image
import pymupdf

from textbook_pdf import extraction, ocr_engine, scan_adapter as scan, scan_detectors
from textbook_pdf.models import Book, Element, file_sha256, needs_translation, source_contract_issues

WORK = Path(__file__).resolve().parent / '_tmp_scan_adapter'


def row(text, bbox, i=0, confidence=.99):
    return {'id': f'l{i:04d}', 'text':text, 'bbox':list(bbox), 'confidence':confidence,
            'words':[{'id':f'l{i:04d}-w0000','text':text,'bbox':list(bbox),'confidence':confidence}]}


def region(kind, bbox, detector='formula', i=0):
    return {'id':f'{detector}-{i}', 'kind':kind, 'bbox':list(bbox),
            'detector':detector, 'confidence':.95}


class ScanAdapterTests(unittest.TestCase):
    def setUp(self):
        self.root=WORK/'adapter-tests'/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(self.cleanup)
        self.identity={'signature':'ocr-test-content-v1'}
        self.detector_identity={'layout':{'sha256':'layout-test'},'formula':{'sha256':'math-test'}}
        self.ocr_calls=[]

    def cleanup(self):
        assert self.root.resolve().is_relative_to((WORK/'adapter-tests').resolve())
        shutil.rmtree(self.root)

    def pdf(self, *, rotation=0, hidden_text=False, annotation=False, mixed=False, layers=False):
        path=self.root/'source.pdf'
        with pymupdf.open() as doc:
            page=doc.new_page(width=240,height=320)
            rgb=np.full((160,120,3),255,np.uint8)
            rgb[65:85,30:80]=[30,60,150]
            stream=io.BytesIO();Image.fromarray(rgb).save(stream,format='PNG')
            page.insert_image(page.rect,stream=stream.getvalue())
            if layers:
                rgba=np.zeros((40,40,4),np.uint8);rgba[:]=[20,180,40,160]
                stream=io.BytesIO();Image.fromarray(rgba).save(stream,format='PNG')
                page.insert_image(pymupdf.Rect(60,120,140,200),stream=stream.getvalue())
            if hidden_text:page.insert_text((20,100),'Hidden OCR regular Helvetica',render_mode=3)
            if annotation:
                annot=page.add_rect_annot(pymupdf.Rect(20,20,80,80))
                annot.set_colors(stroke=(1,0,0));annot.set_border(width=4);annot.update()
            page.set_rotation(rotation)
            if mixed:
                page=doc.new_page(width=240,height=320)
                page.insert_text((20,150),'Native digital paragraph')
            doc.save(path)
        return path

    def mocks(self, lines=None, regions=None):
        lines=lines if lines is not None else [row('Body paragraph.',[60,200,350,220])]
        regions=[] if regions is None else regions
        stack=ExitStack()
        stack.enter_context(patch.object(ocr_engine,'engine_identity',side_effect=lambda:deepcopy(self.identity)))
        stack.enter_context(patch.object(scan_detectors,'detector_identity',side_effect=lambda:deepcopy(self.detector_identity)))
        def recognize(image):
            self.ocr_calls.append(image.copy())
            return {'lines':deepcopy(lines),'engine':deepcopy(self.identity),'elapsed_seconds':0.0}
        stack.enter_context(patch.object(ocr_engine,'recognize',side_effect=recognize))
        stack.enter_context(patch.object(scan_detectors,'detect_regions',side_effect=lambda image:{'regions':deepcopy(regions),'models':deepcopy(self.detector_identity)}))
        return stack

    def run_scan(self,path,lines=None,regions=None,*,out=None,corrections=None,ocr='force'):
        with self.mocks(lines,regions):
            return scan.analyze(path,out or self.root/'job',backend='native',ocr=ocr,ocr_dpi=150,corrections=corrections)

    def test_layout_supplies_missing_formula_prefix_and_vertical_ellipsis(self):
        evidence={'regions':[region('formula_display',[40,60,240,260],'layout'),
                             region('formula_display',[105,60,240,120]),
                             region('formula_display',[105,180,240,225],i=1)]}
        protected=scan._select_regions(evidence,[],400,600)
        self.assertEqual(len(protected),1)
        box=protected[0]['bbox']
        self.assertLessEqual(box[0],40);self.assertGreaterEqual(box[3],260)
        self.assertEqual(len(protected[0]['evidence_ids']),3)

    def test_separate_equations_and_columns_do_not_union(self):
        evidence={'regions':[region('formula_display',[30,100,180,125]),
                             region('formula_display',[220,100,370,125],i=1),
                             region('formula_display',[30,170,180,195],i=2)]}
        protected=scan._select_regions(evidence,[],400,600)
        self.assertEqual(len(protected),3)

    def test_adjacent_column_questions_are_never_horizontal_sentence_fragments(self):
        lines=[]
        for i in range(3):
            for col,(x0,x1) in enumerate(((25,195),(205,375))):
                lines.append(row(f'{1+3*col+i}. Question',[x0,100+30*i,x1,120+30*i],i*2+col))
        rows=scan._text_rows(lines,[],400,600)
        self.assertEqual(len(rows),6,'A narrow gutter is not evidence that two numbered questions share one sentence')
        self.assertTrue(all(sum(str(n)+'. Question' in r['text'] for n in range(1,7))==1 for r in rows))

    def test_coarse_layout_box_cannot_override_two_numbered_column_flows(self):
        lines=[]
        for i in range(3):
            for col,(x0,x1) in enumerate(((25,195),(205,375))):
                lines.append(row(f'{1+3*col+i}. Question',[x0,100+30*i,x1,120+30*i],i*2+col))
        coarse=[region('text',[20,90,380,200],'layout')]
        rows=scan._text_rows(lines,[],400,600,coarse)
        self.assertEqual(len(rows),6,'A coarse shared detector rectangle must not join independent numbered questions')

    def test_true_inline_formula_can_bridge_masked_sentence_fragments(self):
        lines=[row('Given',[20,100,55,120]),row('we prove',[100,100,160,120],1)]
        math=region('formula_inline',[61,98,94,122]);math['asset_id']='original-math'
        rows=scan._text_rows(lines,[math],400,600)
        self.assertEqual(len(rows),1)
        self.assertIn('Given',rows[0]['text']);self.assertIn('we prove',rows[0]['text'])
        self.assertEqual(rows[0]['text'].count('⟦F:original-math⟧'),1)

    def test_raster_without_text_bookmarks_or_labels_dispatches_to_scan(self):
        path=self.pdf()
        with pymupdf.open(path) as doc:
            self.assertFalse(doc.get_toc());self.assertFalse(doc.get_page_labels());self.assertEqual(doc[0].get_text(),'')
        self.assertEqual(scan.scan_pages(path),[1])
        with patch.object(scan,'analyze',return_value='routed') as routed:
            result=extraction.analyze(path,self.root/'routed',backend='native')
        self.assertEqual(result,'routed');self.assertEqual(routed.call_args.args[2],[1])

    def test_rotated_raster_with_regular_hidden_ocr_is_still_scan(self):
        path=self.pdf(rotation=90,hidden_text=True)
        with pymupdf.open(path) as doc:
            self.assertIn('Hidden',doc[0].get_text());self.assertEqual(doc[0].rotation,90)
        self.assertEqual(scan.scan_pages(path),[1],'Image coverage must use the same coordinate frame as image bboxes')

    def test_source_sha_rotation_unknown_label_and_units_survive_real_adapter(self):
        path=self.pdf(rotation=90);before=file_sha256(path)
        book=self.run_scan(path)
        self.assertEqual(book.source_sha256,before);self.assertEqual(file_sha256(path),before)
        self.assertEqual(book.pages[0]['scan_evidence']['source_rotation'],90)
        self.assertEqual(book.pages[0]['parser_backend'],'local-scan-ocr')
        self.assertIsNone(book.pages[0]['printed_page'])
        self.assertFalse(book.pages[0]['scan_evidence']['optical_recognition_verified'])
        ledger=self.root/'job'/book.pages[0]['coverage']['ledger']
        with gzip.open(ledger,'rt',encoding='utf8') as stream:units=json.load(stream)
        self.assertTrue(units);self.assertTrue(all(u['owner'] in book.element_map() for u in units))
        self.assertEqual(source_contract_issues(book),[])

    def test_margin_body_is_not_silently_excluded_without_header_evidence(self):
        path=self.pdf()
        lines=[row('The opening body sentence.',[50,25,350,45]),
               row('Middle body.',[50,200,350,220],1),
               row('The final body sentence.',[50,630,350,650],2)]
        book=self.run_scan(path,lines)
        text=[e.text for e in book.elements if needs_translation(e)]
        self.assertTrue(any('opening body sentence' in s for s in text))
        self.assertTrue(any('final body sentence' in s for s in text))

    def test_numbered_neighbor_exercises_remain_separate(self):
        path=self.pdf()
        lines=[row('1. Prove the first claim.',[50,150,350,170]),
               row('Use the given assumption.',[70,180,350,200],1),
               row('2. Prove the next claim.',[50,220,350,240],2)]
        regions=[region('text',[40,140,370,250],'layout')]
        book=self.run_scan(path,lines,regions)
        self.assertFalse(any('1. Prove' in e.text and '2. Prove' in e.text for e in book.elements))
        self.assertEqual(sum(e.kind=='exercise' for e in book.elements),2)

    def test_original_mrc_composition_used_in_protected_asset(self):
        path=self.pdf(layers=True)
        regions=[region('formula_display',[115,245,290,425])]
        book=self.run_scan(path,regions=regions)
        self.assertEqual(len(book.assets),1)
        asset=next(iter(book.assets.values()))
        self.assertTrue(asset.metadata['original_page_composition_preserved'])
        self.assertEqual(asset.metadata['source_sha256'],book.source_sha256)
        self.assertEqual(file_sha256(self.root/'job'/asset.path),asset.sha256)
        with pymupdf.open(self.root/'job'/asset.path) as bundled:
            pix=bundled[asset.metadata['pdf_page']].get_pixmap(matrix=pymupdf.Matrix(150/72,150/72),colorspace=pymupdf.csRGB,alpha=False)
            actual=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3)
        self.assertGreater(np.count_nonzero(actual[:,:,1]>actual[:,:,0]+20),100)
        self.assertEqual(source_contract_issues(book),[])

    def test_protected_asset_preserves_visible_annotation_from_rendered_source(self):
        path=self.pdf(annotation=True)
        regions=[region('formula_display',[30,30,200,200])]
        book=self.run_scan(path,regions=regions)
        asset=next(iter(book.assets.values()))
        with pymupdf.open(self.root/'job'/asset.path) as bundle:
            pix=bundle[asset.metadata['pdf_page']].get_pixmap(matrix=pymupdf.Matrix(150/72,150/72),colorspace=pymupdf.csRGB,alpha=False)
            actual=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3)
        red=(actual[:,:,0]>180)&(actual[:,:,1]<100)&(actual[:,:,2]<100)
        self.assertGreater(np.count_nonzero(red),500,'show_pdf_page omits annotations unless flattened/preserved explicitly')

    def test_resume_uses_cache_but_model_identity_change_invalidates_source(self):
        path=self.pdf();first=self.run_scan(path);calls=len(self.ocr_calls)
        self.assertGreater(calls,0)
        second=self.run_scan(path)
        self.assertEqual(len(self.ocr_calls),calls)
        self.assertEqual(first.profile['analysis_signature'],second.profile['analysis_signature'])
        self.identity['signature']='ocr-test-content-v2'
        third=self.run_scan(path)
        self.assertGreater(len(self.ocr_calls),calls)
        self.assertNotEqual(first.profile['analysis_signature'],third.profile['analysis_signature'])

    def test_changed_scan_checkpoint_image_is_not_trusted(self):
        path=self.pdf();book=self.run_scan(path)
        image=self.root/'job'/book.pages[0]['scan_evidence']['image']
        image.write_bytes(b'corrupt source raster')
        with self.assertRaisesRegex(ValueError,'checkpoint image changed'):
            self.run_scan(path)

    def test_auto_to_force_mixed_resume_replaces_native_route(self):
        path=self.pdf(mixed=True)
        def native(pdf_path,outdir,pages,backend,ocr):
            book=Book(str(path),file_sha256(path),'mixed',2,list(pages))
            for n in pages:
                eid=f'native-{n}'
                book.elements.append(Element(eid,'paragraph','Native digital paragraph',n,[20,100,200,120]))
                book.pages.append({'number':n,'element_ids':[eid],'columns':1,'parser_backend':'native','coverage':{}})
            return book
        with patch.object(extraction,'analyze',side_effect=native):
            auto=self.run_scan(path,ocr='auto')
            forced=self.run_scan(path,ocr='force')
        self.assertEqual(auto.pages[1]['parser_backend'],'native')
        self.assertTrue(all(p['parser_backend']=='local-scan-ocr' for p in forced.pages))
        self.assertNotIn('native-2',forced.element_map())
        self.assertNotEqual(auto.profile['analysis_signature'],forced.profile['analysis_signature'])

    def test_native_dependency_hash_changes_mixed_checkpoint_signature(self):
        path=self.pdf(mixed=True)
        def native(pdf_path,outdir,pages,backend,ocr):
            book=Book(str(path),file_sha256(path),'mixed',2,list(pages))
            for n in pages:
                eid=f'native-{n}';book.elements.append(Element(eid,'paragraph','Native',n,[20,100,200,120]))
                book.pages.append({'number':n,'element_ids':[eid],'columns':1,'parser_backend':'native','coverage':{}})
            return book
        real_sha=scan.file_sha256
        def changed(p):return 'new-native-parser-content' if Path(p).name=='extraction.py' else real_sha(p)
        with patch.object(extraction,'analyze',side_effect=native):
            first=self.run_scan(path,ocr='auto')
            with patch.object(scan,'file_sha256',side_effect=changed):
                second=self.run_scan(path,ocr='auto')
        self.assertNotEqual(first.profile['analysis_signature'],second.profile['analysis_signature'])

    def correction_from_review(self,path,book):
        review=json.loads((self.root/'job/scan/p0001.review.json').read_text(encoding='utf8'))
        return {'source_sha256':book.source_sha256,'pages':{'1':{
            'reviewed':True,'reason':'original raster checked',
            'image_sha256':review['image_sha256'],
            'line_map_sha256':review['line_map_sha256'],
            'line_text':{'l0000':'Corrected body sentence.'}}}}

    def test_current_reviewed_line_map_accepts_text_correction(self):
        path=self.pdf();book=self.run_scan(path)
        correction=self.root/'valid-correction.json'
        correction.write_text(json.dumps(self.correction_from_review(path,book)),encoding='utf8')
        fixed=self.run_scan(path,corrections=correction)
        self.assertTrue(any(e.text=='Corrected body sentence.' for e in fixed.elements))
        self.assertEqual(fixed.source_sha256,book.source_sha256)
        self.assertNotEqual(fixed.profile['analysis_signature'],book.profile['analysis_signature'])

    def test_same_image_reordered_ocr_or_new_engine_rejects_old_correction(self):
        path=self.pdf()
        lines=[row('First source sentence.',[50,150,350,170]),
               row('Second source sentence.',[50,200,350,220],1)]
        book=self.run_scan(path,lines)
        correction=self.root/'frozen-line-map.json'
        correction.write_text(json.dumps(self.correction_from_review(path,book)),encoding='utf8')
        with self.assertRaisesRegex(ValueError,'line map changed'):
            self.run_scan(path,list(reversed(lines)),corrections=correction,out=self.root/'reordered')
        self.identity['signature']='new-ocr-engine'
        with self.assertRaisesRegex(ValueError,'line map changed'):
            self.run_scan(path,lines,corrections=correction,out=self.root/'new-engine')

    def test_invalid_correction_source_review_image_and_unknown_line_rejected(self):
        path=self.pdf();book=self.run_scan(path)
        image_hash=book.pages[0]['scan_evidence']['image_sha256']
        review=json.loads((self.root/'job/scan/p0001.review.json').read_text(encoding='utf8'))
        base={'source_sha256':book.source_sha256,'pages':{'1':{'reviewed':True,'reason':'source checked','image_sha256':image_hash,'line_map_sha256':review['line_map_sha256']}}}
        values=[]
        value=deepcopy(base);value['source_sha256']='other';values.append(value)
        value=deepcopy(base);value['pages']['1']['reviewed']=False;values.append(value)
        value=deepcopy(base);value['pages']['1']['image_sha256']='changed';values.append(value)
        value=deepcopy(base);value['pages']['1']['line_text']={'l9999':'fabricated'};values.append(value)
        for i,value in enumerate(values):
            correction=self.root/f'bad-{i}.json';correction.write_text(json.dumps(value),encoding='utf8')
            with self.subTest(i=i),self.assertRaises(ValueError):
                self.run_scan(path,corrections=correction,out=self.root/f'bad-job-{i}')


if __name__=='__main__':unittest.main()
