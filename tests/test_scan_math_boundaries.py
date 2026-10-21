"""Pixel-exact scan math boundaries, independent of the frozen 19 tests.

Black OpenCV glyphs supply known connected source ink. These cases never
invoke a model, external API, or mutate original PDFs/production source.
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

from textbook_pdf import ocr_engine, scan_adapter as scan, scan_detectors
from textbook_pdf.models import TOKEN_RE


WORK=Path(__file__).resolve().parent/'_tmp_scan_math_boundaries'


def ink_box(mask):
    yy,xx=np.nonzero(mask)
    return [int(xx.min()),int(yy.min()),int(xx.max()+1),int(yy.max()+1)]


def glyph(text,origin=(45,100),scale=2.,shape=(160,260),thickness=3):
    image=np.full((*shape,3),255,np.uint8)
    cv2.putText(image,text,origin,cv2.FONT_HERSHEY_SIMPLEX,scale,(0,0,0),thickness,cv2.LINE_8)
    return image,image[:,:,0]<128


def combine(*images):return np.minimum.reduce(images)


def math_region(box,detector='formula',kind='formula_inline'):
    return {'id':'math-0','kind':kind,'bbox':list(map(float,box)),
            'detector':detector,'confidence':.99}


def raw_word(text,box):
    return [{'id':'raw-line-0','text':text,'bbox':list(box),'confidence':.99,
             'words':[{'id':'raw-word-0','text':text,'bbox':list(box),'confidence':.99}]}]


class ScanMathBoundaryTests(unittest.TestCase):
    def assert_contains_all_ink(self,box,mask):
        owned=np.zeros(mask.shape,bool)
        x0,y0,x1,y1=box
        owned[max(0,int(np.floor(y0))):min(mask.shape[0],int(np.ceil(y1))),
              max(0,int(np.floor(x0))):min(mask.shape[1],int(np.ceil(x1)))]=True
        self.assertEqual(np.count_nonzero(mask&~owned),0,'Original connected glyph pixels fall outside the recorded box')

    def scene(self,mark=','):
        letter,letter_mask=glyph('N')
        punctuation,punctuation_mask=glyph(mark,(115,100),1.2,thickness=2)
        image=combine(letter,punctuation)
        box=ink_box(letter_mask|punctuation_mask)
        box=[box[0]-3,box[1]-3,box[2]+3,box[3]+3]
        return image,letter_mask,punctuation_mask,box

    def test_half_width_ctc_box_expands_to_every_connected_letter_pixel(self):
        image,mask=glyph('N');x0,y0,x1,y1=ink_box(mask)
        seed=[(x0+x1)//2,y0-1,x1+1,y1+1]
        region=math_region(seed,'ocr-single-variable-review-required')
        result=scan._refine_variable_regions([region],image)[0]
        self.assert_contains_all_ink(result['bbox'],mask)
        self.assertIn('ctc_bbox_before_refinement',result)

    def test_half_height_ctc_box_cannot_clip_the_global_connected_letter(self):
        image,mask=glyph('N');x0,y0,x1,y1=ink_box(mask)
        seed=[x0-1,(y0+y1)//2,x1+1,y1+1]
        result=scan._refine_variable_regions([math_region(seed,'ocr-single-variable-review-required')],image)[0]
        self.assert_contains_all_ink(result['bbox'],mask)

    def test_refinement_does_not_swallow_a_neighboring_letter(self):
        variable,variable_mask=glyph('N')
        neighbor,neighbor_mask=glyph('q',(150,100))
        image=combine(variable,neighbor);x0,y0,x1,y1=ink_box(variable_mask)
        result=scan._refine_variable_regions([math_region([x0+8,y0-1,x1+1,y1+1],'ocr-single-variable-review-required')],image)[0]
        self.assert_contains_all_ink(result['bbox'],variable_mask)
        self.assertLess(result['bbox'][2],ink_box(neighbor_mask)[0])

    def test_suffix_comma_and_period_move_out_without_cutting_variable(self):
        for mark in (',','.'):
            with self.subTest(mark=mark):
                image,letter,punctuation,box=self.scene(mark)
                result=scan._detach_prose_punctuation([math_region(box)],raw_word('N'+mark,box),image)[0]
                self.assertEqual(result['detached_prose_punctuation']['text'],mark)
                self.assert_contains_all_ink(result['bbox'],letter)
                self.assert_contains_all_ink(result['detached_prose_punctuation']['bbox'],punctuation)
                self.assertLessEqual(result['bbox'][2],ink_box(punctuation)[0])

    def test_detached_comma_owns_full_source_component_beyond_detector_crop(self):
        letter_image,letter=glyph('N')
        comma_image,punctuation=glyph(',',(115,100),2.2,thickness=2)
        image=combine(letter_image,comma_image);b=ink_box(letter|punctuation)
        box=[b[0]-3,b[1]-3,b[2]+3,b[3]+3]
        # Only the comma tail is clipped; the original letter is fully inside.
        # More than five pixels of comma continue below the detector crop.
        clipped=box[:];clipped[3]=ink_box(letter)[3]+1
        result=scan._detach_prose_punctuation([math_region(clipped)],raw_word('N,',box),image)[0]
        self.assertIn('detached_prose_punctuation',result)
        self.assert_contains_all_ink(result['detached_prose_punctuation']['bbox'],punctuation)
        self.assert_contains_all_ink(result['bbox'],letter)

    def test_small_rightmost_ink_without_ocr_suffix_is_never_deleted(self):
        image,letter,punctuation,box=self.scene(',')
        for raw in ([],raw_word('N',box)):
            with self.subTest(raw_present=bool(raw)):
                result=scan._detach_prose_punctuation([math_region(box)],raw,image)[0]
                self.assertEqual(result['bbox'],box)
                self.assertNotIn('detached_prose_punctuation',result)
                self.assert_contains_all_ink(result['bbox'],letter|punctuation)

    def test_tuple_internal_comma_and_math_right_parenthesis_are_preserved(self):
        image,mask=glyph('(x,y)',(25,100),1.5,thickness=2)
        b=ink_box(mask);box=[b[0]-3,b[1]-3,b[2]+3,b[3]+3]
        # Deliberately generous estimated CTC first-word box: suffix comma
        # alone cannot authorize deleting the rightmost mathematical bracket.
        raw=raw_word('(x,y)',box)
        raw[0]['words']=[{'text':'(x,','bbox':box[:],'confidence':.99},
                         {'text':'y)','bbox':box[:],'confidence':.99}]
        result=scan._detach_prose_punctuation([math_region(box)],raw,image)[0]
        self.assertEqual(result['bbox'],box)
        self.assertNotIn('detached_prose_punctuation',result)
        self.assert_contains_all_ink(result['bbox'],mask)

    def test_tuple_external_comma_detaches_but_entire_tuple_remains(self):
        tuple_image,tuple_mask=glyph('(x,y)',(25,100),1.5,thickness=2)
        right=ink_box(tuple_mask)[2]
        comma_image,comma_mask=glyph(',',(right+12,100),1.2,thickness=2)
        image=combine(tuple_image,comma_image);b=ink_box(tuple_mask|comma_mask)
        box=[b[0]-3,b[1]-3,b[2]+3,b[3]+3]
        result=scan._detach_prose_punctuation([math_region(box)],raw_word('(x,y),',box),image)[0]
        self.assertEqual(result['detached_prose_punctuation']['text'],',')
        self.assert_contains_all_ink(result['bbox'],tuple_mask)
        self.assert_contains_all_ink(result['detached_prose_punctuation']['bbox'],comma_mask)

    def test_display_formula_is_not_trimmed_by_inline_punctuation_rule(self):
        image,letter,punctuation,box=self.scene('.')
        result=scan._detach_prose_punctuation([math_region(box,kind='formula_display')],raw_word('N.',box),image)[0]
        self.assertEqual(result['bbox'],box)
        self.assert_contains_all_ink(result['bbox'],letter|punctuation)

    def test_detached_punctuation_is_in_body_and_ledger_with_full_original_ink(self):
        root=WORK/'math-boundary-tests'/uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(lambda:shutil.rmtree(root))
        # Source image pixel dimensions map exactly to a 150dpi PDF raster.
        shape=(400,300)
        letter,letter_mask=glyph('N',(100,200),1.3,shape,2)
        right=ink_box(letter_mask)[2]
        comma,comma_mask=glyph(',',(right+10,200),1.0,shape,2)
        given,_=glyph('Given',(10,200),.65,shape,1)
        prove,_=glyph('we prove',(180,200),.65,shape,1)
        image=combine(letter,comma,given,prove)
        b=ink_box(letter_mask|comma_mask);box=[b[0]-3,b[1]-3,b[2]+3,b[3]+3]
        path=root/'synthetic-original.pdf';stream=io.BytesIO()
        Image.fromarray(image).save(stream,format='PNG')
        with pymupdf.open() as doc:
            page=doc.new_page(width=144,height=192)
            page.insert_image(page.rect,stream=stream.getvalue());doc.save(path)
        def line(text,rect,i=0):
            return {'text':text,'bbox':rect,'confidence':.99,
                    'words':[{'text':text,'bbox':rect,'confidence':.99}]}
        raw={'lines':[line('N,',box)],'engine':{'signature':'fixed-local-test'}}
        prose={'lines':[line('Given',[10,184,76,202]),line('we prove',[180,184,285,202],1)],
               'engine':{'signature':'fixed-local-test'}}
        detection={'regions':[math_region(box)],'models':{'test':'fixed-local'}}
        with ExitStack() as stack:
            stack.enter_context(patch.object(ocr_engine,'engine_identity',return_value={'signature':'fixed-local-test'}))
            stack.enter_context(patch.object(scan_detectors,'detector_identity',return_value={'test':'fixed-local'}))
            stack.enter_context(patch.object(ocr_engine,'recognize',side_effect=[deepcopy(raw),deepcopy(prose)]))
            stack.enter_context(patch.object(scan_detectors,'detect_regions',return_value=detection))
            book=scan.analyze(path,root/'job',backend='native',ocr='force',ocr_dpi=150)
        texts=' '.join(e.text for e in book.elements)
        self.assertEqual(texts.count(','),1,'Punctuation detached from the image must occur once in translatable body text')
        self.assertRegex(texts,r'⟦F:[^⟧]+⟧\s*,')
        ledger=root/'job'/book.pages[0]['coverage']['ledger']
        with gzip.open(ledger,'rt',encoding='utf8') as source:units=json.load(source)
        punctuation_units=[unit for unit in units if ',' in unit['text']]
        self.assertTrue(punctuation_units,'Detached original punctuation needs an explicit current text owner')
        self.assertTrue(any(unit['owner'] in book.element_map() for unit in punctuation_units))
        self.assert_contains_all_ink(punctuation_units[0]['bbox'],comma_mask)
        self.assertEqual(book.pages[0]['scan_evidence']['quality']['thresholds']['source_bbox_tolerance_pixels'],2)


if __name__=='__main__':unittest.main()
