"""Scan paper is a background-only pass over a real, fixed PDF.

The legacy renderer is replaced by a deterministic PDF-producing fixture;
text extraction, images, vector ink and positions are checked independently
with PyMuPDF and pixels. No Typst process, OCR model or API is invoked.
"""
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import numpy as np
from PIL import Image
import pymupdf

from textbook_pdf import layout, qa, scan_layout
from textbook_pdf.models import Asset, Book, Element, digest, file_sha256

WORK=Path(__file__).resolve().parent/'_tmp_scan_layout'


def pdf_snapshot(path):
    result=[]
    with pymupdf.open(path) as doc:
        for page in doc:
            chars=[]
            for block in page.get_text('rawdict')['blocks']:
                for line in block.get('lines',[]):
                    for span in line.get('spans',[]):
                        for char in span['chars']:
                            chars.append((char['c'],tuple(char['origin']),tuple(char['bbox']),span['font'],span['size']))
            pix=page.get_pixmap(matrix=pymupdf.Matrix(2,2),colorspace=pymupdf.csRGB,alpha=False)
            rgb=np.frombuffer(pix.samples,np.uint8).reshape(pix.height,pix.width,3).copy()
            images=[(tuple(im['bbox']),im['width'],im['height'],im.get('digest')) for im in page.get_image_info(hashes=True)]
            result.append({'size':tuple(page.rect),'chars':chars,'images':images,'rgb':rgb})
    return result


class ScanPaperLayoutTests(unittest.TestCase):
    def setUp(self):
        self.root=WORK/'scan-layout-tests'/uuid.uuid4().hex
        self.job=self.root/'source-job';self.job.mkdir(parents=True)
        (self.job/'scan').mkdir();(self.job/'assets').mkdir()
        self.addCleanup(self.cleanup)
        self.translations={'body':'固定文字 0123。'}
        self.before=None;self.mapping_before=None;self.before_sha=None

    def cleanup(self):
        assert self.root.resolve().is_relative_to((WORK/'scan-layout-tests').resolve())
        shutil.rmtree(self.root)

    def book(self,scan=True,tone=(246,239,220)):
        rgb=np.full((180,240,3),tone,np.uint8)
        rgb[40:58,70:110]=0
        image=self.job/'scan/p0001.png';Image.fromarray(rgb).save(image)
        source=self.job/'original.pdf'
        with pymupdf.open() as doc:
            page=doc.new_page(width=240,height=180);page.insert_image(page.rect,filename=str(image));doc.save(source)
        asset_path=self.job/'assets/formula.pdf'
        with pymupdf.open() as doc:
            page=doc.new_page(width=60,height=26);page.insert_text((5,18),'x + 1',fontsize=12)
            page.draw_line((4,22),(55,22),color=(0,0,0),width=.7);doc.save(asset_path)
        elements=[Element('body','paragraph','Original paragraph 0123.',1,[26,30,210,55]),
                  Element('equation','formula','',1,[30,112,90,138],asset_ids=['math-1'])]
        asset=Asset('math-1','formula','assets/formula.pdf',1,[30,112,90,138],60,26,18,'x + 1',file_sha256(asset_path),{'format':'pdf','pdf_page':0})
        page={'number':1,'width':240,'height':180,'element_ids':['body','equation'],'columns':1}
        if scan:
            quality={'coverage_basis':'raster-ink-and-ocr-units','foreground_pixels':720,'covered_ink_pixels':720,
                     'unassigned_ink_pixels':0,'unassigned_meaningful_component_count':0,
                     'raster_coverage_complete':True,'issues':[],
                     'ocr_units':{'low_confidence':0,'unknown_confidence':0},
                     'thresholds':{'source_bbox_tolerance_pixels':2}}
            page.update(coverage={'basis':'raster-ink-and-ocr-units','total_glyphs':0,'total_ocr_units':1},
                        scan_evidence={'image':'scan/p0001.png','image_file_sha256':file_sha256(image),
                        'image_sha256':digest(rgb.tobytes()),'image_size':[240,180],'dpi':150,'quality':quality,
                        'original_composed_page_preserved':True,'optical_recognition_verified':False,'recognition_accuracy':None})
        book=Book(str(source),file_sha256(source),'Paper fixture',1,[1],pages=[page],elements=elements,assets={'math-1':asset})
        book.profile.update(job_dir=str(self.job),margins={'top':15,'bottom':15})
        return book

    def legacy_pdf(self,book,translations,outdir,output_path=None,allow_incomplete=False):
        outdir=Path(outdir);outdir.mkdir(parents=True,exist_ok=True)
        output=Path(output_path) if output_path else outdir/'fixed-output.pdf'
        source_page_sets=getattr(self,'page_sources',[[1]])
        with pymupdf.open() as doc:
            for sources in source_page_sets:
                page=doc.new_page(width=240,height=180)
                page.insert_text((26,48),'Fixed letters 0123',fontsize=12,color=(0,0,0))
                page.draw_rect(pymupdf.Rect(32,72,52,96),color=None,fill=(0,0,0))
                page.draw_line((115,80),(185,103),color=(0,0,0),width=1.2)
                tile=np.full((16,16,3),255,np.uint8);tile[:8,:8]=[250,20,30];tile[8:,8:]=[20,40,220]
                stream=io.BytesIO();Image.fromarray(tile).save(stream,format='PNG')
                page.insert_image(pymupdf.Rect(150,112,190,142),stream=stream.getvalue())
                with pymupdf.open(self.job/'assets/formula.pdf') as asset:
                    page.show_pdf_page(pymupdf.Rect(30,112,90,138),asset,0)
            doc.save(output)
        pages=[{'page':i+1,'width':240,'height':180,'source_pages':v} for i,v in enumerate(source_page_sets)]
        mapping={'pages':pages,'elements':{'body':[{'page':1,'bbox':[26,30,210,55]}],
                 'equation':[{'page':1,'bbox':[30,112,90,138]}]},
                 'assets':{'math-1':[{'page':1,'bbox':[30,112,90,138]}]},'issues':[],
                 'render_binding':layout.rendering_binding(book,translations),'pdf_sha256':file_sha256(output)}
        target=outdir/'layout_map.json';target.write_text(json.dumps(mapping),encoding='utf8')
        self.before=pdf_snapshot(output);self.mapping_before=deepcopy(mapping);self.before_sha=file_sha256(output)
        return {'pdf_path':str(output),'layout_map_path':str(target),'page_count':len(pages),'passed':True}

    def compose_fixture(self,book):
        with patch.object(scan_layout,'_compose',side_effect=self.legacy_pdf):
            return scan_layout.compose(book,self.translations,self.root/'output')

    def test_digital_book_delegates_to_legacy_compose_without_scan_binding(self):
        book=self.book(scan=False);before=deepcopy(book.profile);sentinel={'original-renderer':True}
        output=self.root/'output';requested=output/'requested.pdf'
        with patch.object(scan_layout,'_compose',return_value=sentinel) as legacy:
            actual=scan_layout.compose(book,self.translations,output,output_path=requested,allow_incomplete=True)
        self.assertIs(actual,sentinel);self.assertEqual(book.profile,before)
        legacy.assert_called_once_with(book,self.translations,output.resolve(),output_path=requested,allow_incomplete=True)
        self.assertFalse((output/'book.json').exists())

    def test_sampled_paper_is_behind_fixed_black_text_vectors_and_opaque_image(self):
        tone=(246,239,220);book=self.book(tone=tone)
        source_before=file_sha256(book.source_path);asset_before=file_sha256(self.job/'assets/formula.pdf')
        image_before=file_sha256(self.job/'scan/p0001.png')
        result=self.compose_fixture(book);after=pdf_snapshot(result['pdf_path'])
        self.assertEqual(len(after),1)
        self.assertEqual(after[0]['size'],self.before[0]['size'])
        self.assertEqual(after[0]['chars'],self.before[0]['chars'],'Glyph contents, origins, font, size and bboxes must be unchanged')
        self.assertEqual(after[0]['images'],self.before[0]['images'],'Opaque image content digest and bbox must be unchanged')
        before_rgb=self.before[0]['rgb'];after_rgb=after[0]['rgb']
        np.testing.assert_allclose(after_rgb[10,10],tone,atol=1)
        np.testing.assert_array_equal(before_rgb[10,10],[255,255,255])
        # Deep interior black vector ink cannot be tinted/covered by paper.
        black=np.all(before_rgb==0,axis=2)
        self.assertGreater(np.count_nonzero(black),1500)
        np.testing.assert_array_equal(after_rgb[black],before_rgb[black])
        # The entire opaque raster (including its white pixels) stays intact.
        np.testing.assert_array_equal(after_rgb[224:284,300:380],before_rgb[224:284,300:380])
        self.assertEqual(file_sha256(book.source_path),source_before)
        self.assertEqual(file_sha256(self.job/'assets/formula.pdf'),asset_before)
        self.assertEqual(file_sha256(self.job/'scan/p0001.png'),image_before)
        self.assertEqual(book.profile['scan_composition']['source_paper_rgb']['1'],list(tone))

    def test_layout_map_preserves_measured_boxes_and_binds_changed_pdf_bytes(self):
        book=self.book();result=self.compose_fixture(book)
        mapping=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'))
        for field in ('elements','assets','pages','render_binding'):
            self.assertEqual(mapping[field],self.mapping_before[field])
        self.assertNotEqual(mapping['pdf_sha256'],self.before_sha)
        self.assertEqual(mapping['pdf_sha256'],file_sha256(result['pdf_path']))
        self.assertEqual(mapping['scan_composition']['pdf_before_sha256'],self.before_sha)
        self.assertEqual(mapping['scan_composition']['pdf_after_sha256'],mapping['pdf_sha256'])
        self.assertEqual(mapping['render_binding'],layout.rendering_binding(book,self.translations))
        saved=json.loads((self.root/'output/book.json').read_text(encoding='utf8'))
        self.assertEqual(saved['profile']['scan_composition'],book.profile['scan_composition'])

    def test_mixed_output_page_without_scan_source_keeps_its_white_background(self):
        book=self.book();book.pages.append({'number':2,'element_ids':[],'width':240,'height':180})
        book.selected_pages.append(2);book.page_count=2;self.page_sources=[[1],[2]]
        result=self.compose_fixture(book);after=pdf_snapshot(result['pdf_path'])
        np.testing.assert_allclose(after[0]['rgb'][10,10],[246,239,220],atol=1)
        np.testing.assert_array_equal(after[1]['rgb'],self.before[1]['rgb'])
        self.assertEqual(after[0]['chars'],self.before[0]['chars'])

    def test_tampered_original_scan_image_is_rejected_before_legacy_render(self):
        book=self.book();path=self.job/'scan/p0001.png'
        with Image.open(path) as im:changed=np.array(im.convert('RGB'))
        changed[0,0]=[200,200,200];Image.fromarray(changed).save(path)
        with patch.object(scan_layout,'_compose') as legacy:
            with self.assertRaisesRegex(ValueError,'source image changed'):
                scan_layout.compose(book,self.translations,self.root/'output')
            legacy.assert_not_called()

    def test_dark_paper_fails_explicitly_without_guessing_or_rendering(self):
        book=self.book(tone=(175,160,140))
        with patch.object(scan_layout,'_compose') as legacy:
            with self.assertRaisesRegex(ValueError,'dark or uncertain'):
                scan_layout.compose(book,self.translations,self.root/'output')
            legacy.assert_not_called()
        self.assertNotIn('scan_composition',book.profile)

    def test_qa_rejects_old_paper_binding_after_isolated_wrapper_source_edit(self):
        book=self.book();result=self.compose_fixture(book)
        mapping=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'))
        alias=self.root/'isolated-current-code';alias.mkdir()
        wrapper=alias/'scan_layout.py';wrapper.write_bytes(Path(scan_layout.__file__).read_bytes())
        # QA resolves its current scan compositor beside its own module. This
        # isolated copy simulates a future code version without production edits.
        with patch.object(qa,'__file__',str(alias/'qa.py')):
            current=qa.verify(book,self.translations,self.root/'qa-current',pdf_path=result['pdf_path'],layout_map=mapping)
            self.assertNotIn('scan_compositor_changed',{i['code'] for i in current['issues']})
            self.assertEqual(current['render_revision'],'current')
            wrapper.write_text(wrapper.read_text(encoding='utf8')+'\n# isolated next paper algorithm\n',encoding='utf8')
            stale=qa.verify(book,self.translations,self.root/'qa-stale',pdf_path=result['pdf_path'],layout_map=mapping)
        self.assertIn('scan_compositor_changed',{i['code'] for i in stale['issues']})
        self.assertFalse(stale['technical_checks_passed'])
        self.assertEqual(file_sha256(result['pdf_path']),mapping['pdf_sha256'])


if __name__=='__main__':unittest.main()
