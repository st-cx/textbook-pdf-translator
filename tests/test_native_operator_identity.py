"""Operator identity for context-dependent combining origins, using our TTF."""
from pathlib import Path
import shutil
import struct
import unittest
import uuid

import pymupdf as fitz

from textbook_pdf.extraction import _collect
from textbook_pdf.native_clipping import clip_native_content, verify_selected_native_glyphs


class NativeOperatorIdentityTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).parent/'_tmp_native_operator_identity'/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,self.root)
        self.font=Path(__file__).parent/'fixtures/original-zero-advance.ttf'

    def source(self,second_mark=False,font=None):
        pdf=fitz.open();self.addCleanup(pdf.close)
        page=pdf.new_page(width=240,height=200)
        page.insert_text((30,70),'a|',fontname='original',fontfile=font or self.font,fontsize=30)
        # MuPDF's logical extractor attaches this zero-advance mark to x54,
        # while the unchanged native operator and trace place its origin at55.
        page.insert_text((55,70),'\u0338b',fontname='original',fontsize=30)
        if second_mark:page.insert_text((150,120),'\u0338',fontname='original',fontsize=30)
        _,glyphs=_collect(page)
        selected=[g for g in glyphs if g['c']=='\u0338' and g['origin'][1]<100 or g['c']=='b']
        return pdf,page,selected

    def target(self,source):
        target=fitz.open();self.addCleanup(target.close);target.insert_pdf(source)
        clip_native_content(target[0],[54.5,40,72,80])
        return target[0]

    def independent_target(self,x=55,*,font=None,duplicate=False,body_x=None,morph=None):
        pdf=fitz.open();self.addCleanup(pdf.close);page=pdf.new_page(width=240,height=200)
        page.insert_text((x,70),'\u0338',fontname='original',fontfile=font or self.font,fontsize=30,morph=morph)
        if duplicate:page.insert_text((x,70),'\u0338',fontname='original',fontsize=30)
        page.insert_text((x if body_x is None else body_x,70),'b',fontname='original',fontsize=30)
        return page

    def test_real_context_change_uses_unique_native_operator_without_mutating_input(self):
        source,page,selected=self.source();target=self.target(source)
        before_source=source.tobytes(no_new_id=True);before_target=target.parent.tobytes(no_new_id=True)
        report=verify_selected_native_glyphs(page,target,selected)
        self.assertTrue(report['verified'],report)
        self.assertEqual(report['matched_by_rawdict'],1)
        self.assertEqual(report['matched_by_native_operator'],1)
        evidence=report['native_operator_evidence'][0]
        self.assertEqual(evidence['classification'],'verified_zero_advance_combining_operator')
        self.assertGreater(evidence['rawdict_origin_delta_pt'],.9)
        self.assertLessEqual(evidence['operator_origin_delta_pt'],.001)
        self.assertEqual(evidence['source_font_sha256'],evidence['target_font_sha256'])
        self.assertEqual(report['unselected_target_glyph_count'],0)
        self.assertEqual(source.tobytes(no_new_id=True),before_source)
        self.assertEqual(target.parent.tobytes(no_new_id=True),before_target)

    def test_actual_operator_displacement_over_one_millipoint_is_rejected(self):
        _,source,selected=self.source()
        report=verify_selected_native_glyphs(source,self.independent_target(55.002),selected)
        self.assertFalse(report['verified'])
        self.assertEqual(report['matched_by_native_operator'],0)
        self.assertIn('operator_position_changed',{r['reason'] for r in report['native_operator_evidence']})

    def test_normal_glyph_keeps_original_point_zero_three_limit(self):
        _,source,selected=self.source()
        report=verify_selected_native_glyphs(source,self.independent_target(body_x=55.04),selected)
        normal=next(g['id'] for g in selected if g['c']=='b')
        self.assertIn(normal,report['missing_ids'])
        self.assertFalse(report['verified'])

    def test_duplicate_target_operator_is_ambiguous_even_if_rawdict_deduplicates_it(self):
        _,source,selected=self.source()
        report=verify_selected_native_glyphs(source,self.independent_target(duplicate=True),selected)
        self.assertFalse(report['verified'])
        self.assertEqual(report['matched_by_native_operator'],0)

    def test_repeated_source_combining_identity_is_not_guessed(self):
        source,page,selected=self.source(second_mark=True)
        report=verify_selected_native_glyphs(page,self.target(source),selected)
        self.assertFalse(report['verified'])
        self.assertEqual(report['matched_by_native_operator'],0)

    def test_same_font_name_with_different_embedded_program_is_rejected(self):
        data=bytearray(self.font.read_bytes());tables=struct.unpack_from('>H',data,4)[0]
        for n in range(tables):
            tag,_,offset,_=struct.unpack_from('>4sIII',data,12+16*n)
            if tag==b'head':data[offset+27]^=1;break
        else:self.fail('Project font head table unavailable')
        alternate=self.root/'same-name-different-program.ttf';alternate.write_bytes(data)
        _,source,selected=self.source()
        report=verify_selected_native_glyphs(source,self.independent_target(font=alternate),selected)
        self.assertFalse(report['verified'])
        self.assertIn('font_program_changed',{r['reason'] for r in report['native_operator_evidence']})

    def test_combining_category_without_real_zero_advance_gets_no_exception(self):
        data=bytearray(self.font.read_bytes());tables=struct.unpack_from('>H',data,4)[0]
        for n in range(tables):
            tag,_,offset,_=struct.unpack_from('>4sIII',data,12+16*n)
            if tag==b'hmtx':struct.pack_into('>H',data,offset+3*4,400);break
        else:self.fail('Project font hmtx table unavailable')
        alternate=self.root/'nonzero-advance-combining.ttf';alternate.write_bytes(data)
        source,page,selected=self.source(font=alternate);target=self.target(source)
        mark=next(g for g in selected if g['c']=='\u0338')
        native=next(c for span in page.get_texttrace() for c in span['chars'] if c[0]==0x338)
        self.assertGreater(fitz.Rect(native[3]).width,1)
        report=verify_selected_native_glyphs(page,target,selected)
        self.assertIn(mark['id'],report['missing_ids'])
        self.assertFalse(report['verified'])
        self.assertEqual(report['matched_by_native_operator'],0)

    def test_linear_matrix_change_is_rejected(self):
        _,source,selected=self.source()
        # Reflect about the *same* origin, preserving size and zero advance.
        # This reaches the full-matrix check instead of merely losing a box.
        target=self.independent_target(morph=(fitz.Point(55,70),fitz.Matrix(1,0,0,-1,0,0)))
        report=verify_selected_native_glyphs(source,target,selected)
        self.assertFalse(report['verified'])
        self.assertEqual(report['matched_by_native_operator'],0)
        self.assertIn('operator_linear_matrix_changed',
                      {r['reason'] for r in report['native_operator_evidence']})

    def test_crop_translation_is_bound_in_effective_matrix(self):
        source,page,selected=self.source();isolated=self.target(source)
        target=fitz.open();self.addCleanup(target.close)
        crop=fitz.Rect(54.5,40,72,80);output=target.new_page(width=crop.width,height=crop.height)
        output.show_pdf_page(output.rect,isolated.parent,0,clip=crop)
        good=verify_selected_native_glyphs(page,output,selected,target_offset=(crop.x0,crop.y0))
        self.assertTrue(good['verified'],good)
        bad=verify_selected_native_glyphs(page,output,selected,target_offset=(crop.x0+.002,crop.y0))
        self.assertFalse(bad['verified'])


if __name__=='__main__':unittest.main()
