"""Original synthetic native-font regressions, independent of any textbook."""
from copy import deepcopy
from pathlib import Path
import shutil
import re
import unittest
import uuid
from unittest.mock import patch

import cv2
import numpy as np
import pymupdf as fitz
from textbook_pdf.extraction import _asset, _collect, _finalize_assets, analyze
from textbook_pdf.models import Book, file_sha256
from textbook_pdf.native_clipping import record_native_text_operators, _embedded_font_sha
from textbook_pdf.native_ink_geometry import measure_selected_native_ink, capture_grid, verify_actual_capture


class SelectedInkPrototypeTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).parent/'_tmp_selected_ink'/uuid.uuid4().hex
        self.root.mkdir(parents=True); (self.root/'assets').mkdir()
        self.addCleanup(shutil.rmtree,self.root)
        self.font=Path(__file__).parent/'fixtures/original-ordinary-ink.ttf'

    def document(self,text='p',*,matrix=None,rotate=0,font=None,position=(70,90)):
        document=fitz.open(); self.addCleanup(document.close)
        page=document.new_page(width=240,height=200)
        kwargs={'fontname':'owned','fontfile':font or self.font,'fontsize':30,'rotate':rotate}
        if matrix is not None: kwargs['morph']=(fitz.Point(position),matrix)
        page.insert_text(position,text,**kwargs)
        return document,page

    def asset(self,doc,page,glyphs,selected=None,box=None):
        selected=selected if selected is not None else glyphs
        rect=fitz.Rect(box or selected[0]['bbox'])
        if box is None:
            for g in selected[1:]:rect|=fitz.Rect(g['bbox'])
        book=Book('owned-memory.pdf','owned-synthetic-v1','Owned',1,[1]);book._active_glyphs=glyphs
        baseline=selected[0]['origin'][1]
        a=_asset(book,doc,page,self.root,'formula',rect,''.join(g['c']for g in selected),baseline,
                 {'glyph_ids':[g['id']for g in selected]})
        _finalize_assets(book,1,self.root)
        return book,a

    def assert_native_and_pixels(self,doc,page,asset,selected,*,composed=True):
        with fitz.open(self.root/asset.path)as saved:
            target=saved[asset.metadata['pdf_page']]
            before=record_native_text_operators(page)
            after=record_native_text_operators(target)
            self.assertEqual(len(after),len([g for g in selected if g['c'].strip()]))
            for g in selected:
                if not g['c'].strip():continue
                proof=g['native_ink_geometry'];matrix=proof['effective_matrix']
                wanted=[matrix[4]-asset.bbox[0],matrix[5]-asset.bbox[1]]
                found=[r for r in after if r['character']==g['c']and max(abs(r['effective_matrix'][i+4]-wanted[i])for i in (0,1))<=.001]
                self.assertEqual(len(found),1)
                self.assertLessEqual(max(abs(found[0]['effective_matrix'][i]-matrix[i])for i in range(4)),1e-5)
                self.assertEqual(_embedded_font_sha(target,g['font']),proof['font_program_sha256'])
                raw=[(s,c)for b in target.get_text('rawdict')['blocks']for l in b.get('lines',[])for s in l['spans']for c in s['chars']
                     if c['c']==g['c']and max(abs(c['origin'][i]-wanted[i])for i in (0,1))<=.001]
                self.assertEqual(len(raw),1);self.assertLessEqual(abs(raw[0][0]['size']-g['size']),1e-4)
            self.assertAlmostEqual(asset.bbox[1]+asset.baseline,selected[0]['origin'][1],places=6)
            self.assertAlmostEqual(target.rect.width,asset.width,places=5)
            self.assertAlmostEqual(target.rect.height,asset.height,places=5)
            if composed:
                src=page.get_pixmap(matrix=fitz.Matrix(8,8),clip=fitz.Rect(asset.bbox),colorspace=fitz.csGRAY)
                dst=target.get_pixmap(matrix=fitz.Matrix(8,8),colorspace=fitz.csGRAY)
                self.assertEqual((src.width,src.height),(dst.width,dst.height))
                a=np.frombuffer(src.samples,np.uint8).reshape(src.height,src.width)
                b=np.frombuffer(dst.samples,np.uint8).reshape(dst.height,dst.width)
                dilate=lambda m:cv2.dilate(m.astype(np.uint8),np.ones((3,3),np.uint8)).astype(bool)
                self.assertEqual(int(((a<128)&~dilate(b<244)).sum()),0)
                self.assertEqual(int(((b<128)&~dilate(a<244)).sum()),0)

    def test_negative_bearing_and_descender_reproduce_nominal_failure_and_are_enclosed(self):
        doc,page=self.document();_,glyphs=_collect(page);g=glyphs[0];old=deepcopy(g)
        book,asset=self.asset(doc,page,glyphs)
        self.assertFalse(fitz.Rect(old['bbox']).contains(fitz.Rect(g['native_ink_bbox'])))
        self.assertTrue(fitz.Rect(asset.bbox).contains(fitz.Rect(g['native_ink_bbox'])))
        self.assertEqual(book.issues,[]);self.assert_native_and_pixels(doc,page,asset,glyphs)

    def test_structural_bbox_origin_size_font_flags_and_source_pdf_are_unchanged(self):
        doc,page=self.document('p\u2192x');_,glyphs=_collect(page)
        keys=('id','c','bbox','origin','size','font','flags','color','owner','disposition')
        before=[{k:g[k]for k in keys}for g in glyphs];source=doc.tobytes(no_new_id=True)
        book,asset=self.asset(doc,page,glyphs)
        self.assertEqual(before,[{k:g[k]for k in keys}for g in glyphs])
        self.assertEqual(source,doc.tobytes(no_new_id=True));self.assertEqual(book.issues,[])
        self.assert_native_and_pixels(doc,page,asset,glyphs)

    def test_negative_control_interior_ordinary_glyph_does_not_need_ink_expansion(self):
        doc,page=self.document('n');_,glyphs=_collect(page);nominal=list(glyphs[0]['bbox'])
        book,asset=self.asset(doc,page,glyphs)
        self.assertTrue(fitz.Rect(nominal).contains(fitz.Rect(glyphs[0]['native_ink_bbox'])))
        self.assertEqual(asset.bbox,nominal);self.assertEqual(book.issues,[])
        self.assert_native_and_pixels(doc,page,asset,glyphs)

    def test_nonuniform_shear_full_matrix_survives_without_scalar_approximation(self):
        for m in (fitz.Matrix(1.5,.2,.4,.7,0,0),fitz.Matrix(.6,-.3,.2,1.7,0,0)):
            with self.subTest(matrix=list(m)):
                doc,page=self.document('p',matrix=m);_,glyphs=_collect(page)
                book,asset=self.asset(doc,page,glyphs)
                proof=glyphs[0]['native_ink_geometry'];self.assertFalse(proof['scalar_size_approximation'])
                self.assertNotEqual(proof['effective_matrix'][1:3],[0,0]);self.assertEqual(book.issues,[])
                self.assert_native_and_pixels(doc,page,asset,glyphs)

    def test_all_four_text_rotations_preserve_full_matrix_size_and_ink(self):
        for angle in (0,90,180,270):
            with self.subTest(angle=angle):
                doc,page=self.document('p',rotate=angle,position=(110,100));_,glyphs=_collect(page)
                book,asset=self.asset(doc,page,glyphs);self.assertEqual(book.issues,[])
                self.assert_native_and_pixels(doc,page,asset,glyphs)

    def test_ext_family_uses_full_native_capture_without_regressing_old_corrected_box(self):
        from textbook_pdf import glyph_geometry
        doc,page=self.document('p')
        with patch.object(glyph_geometry,'_TARGET_FONTS',re.compile('OriginalOrdinary')):
            _,glyphs=_collect(page)
        self.assertIn('nominal_bbox',glyphs[0]);old=deepcopy(glyphs[0]['bbox'])
        book,asset=self.asset(doc,page,glyphs)
        self.assertEqual(old,glyphs[0]['bbox']);self.assertEqual(book.issues,[])
        self.assert_native_and_pixels(doc,page,asset,glyphs)

    def test_distinct_same_name_subset_programs_are_not_guessed(self):
        doc,page=self.document('p')
        second=Path(__file__).parent/'fixtures/original-ordinary-ink-distinct.ttf'
        page.insert_text((150,90),'p',fontname='other',fontfile=second,fontsize=30)
        _,glyphs=_collect(page);chosen=[glyphs[0]]
        proof=measure_selected_native_ink(page,glyphs,{chosen[0]['id']})
        self.assertEqual(proof['verified'],[])
        self.assertIn('distinct_same_name',proof['unverified'][0]['reason'])
        book,asset=self.asset(doc,page,glyphs,selected=chosen)
        self.assertTrue(any(i['code']=='source_native_glyph_ink_unverified'and i['severity']=='error'for i in book.issues))
        self.assertEqual(asset.metadata['native_ink_capture']['verified'],[])

    def test_nonembedded_basefont_has_explicit_failure_without_substitute(self):
        with fitz.open()as doc:
            page=doc.new_page();page.insert_text((30,70),'p',fontsize=20)
            _,glyphs=_collect(page);proof=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']})
            self.assertEqual(proof['verified'],[])
            self.assertEqual(proof['unverified'][0]['reason'],'original_embedded_font_program_unavailable')

    def test_overprinted_duplicate_identity_is_rejected(self):
        doc,page=self.document();page.insert_text((70,90),'p',fontname='owned',fontsize=30)
        _,glyphs=_collect(page);proof=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']})
        self.assertEqual(proof['verified'],[])
        self.assertEqual(proof['unverified'][0]['reason'],'native_trace_identity_ambiguous_or_absent')

    def test_stroke_or_translucent_native_text_is_explicitly_unverified(self):
        for mode,alpha in ((1,1),(0,.5)):
            with self.subTest(mode=mode,alpha=alpha),fitz.open()as doc:
                page=doc.new_page();page.insert_text((70,90),'p',fontname='owned',fontfile=self.font,fontsize=30,render_mode=mode,fill_opacity=alpha)
                _,glyphs=_collect(page);proof=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']})
                self.assertEqual(proof['verified'],[])
                self.assertIn('render_mode_or_alpha',proof['unverified'][0]['reason'])

    def test_page_rotate_is_not_confused_with_native_text_rotation(self):
        doc,page=self.document();_,glyphs=_collect(page);page.set_rotation(90)
        before=doc.tobytes(no_new_id=True)
        proof=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']})
        self.assertEqual(proof['verified'],[])
        self.assertEqual(proof['unverified'][0]['reason'],'page_rotation_coordinate_mapping_unverified')
        self.assertEqual(before,doc.tobytes(no_new_id=True))

    def test_foreign_neighbor_is_removed_without_redrawing_selected_overshoot(self):
        doc,page=self.document('p');page.insert_text((82,90),'x',fontname='owned',fontsize=30)
        _,glyphs=_collect(page);chosen=[g for g in glyphs if g['c']=='p']
        book,asset=self.asset(doc,page,glyphs,selected=chosen)
        self.assertEqual(book.issues,[])
        with fitz.open(self.root/asset.path)as saved:
            self.assertEqual(saved[0].get_text().strip(),'p')
        self.assert_native_and_pixels(doc,page,asset,chosen,composed=False)

    def test_capture_grid_only_quantizes_outward_at_measurement_resolution(self):
        source=fitz.Rect(1.011,2.111,7.999,9.001);actual=capture_grid(source)
        self.assertTrue(actual.contains(source))
        self.assertTrue(all(abs(a-b)<.125 for a,b in zip(actual,source)))

    def test_cache_key_retains_distinct_full_matrices_with_same_scalar_size(self):
        doc,page=self.document('p',matrix=fitz.Matrix(2,0,0,.5,0,0))
        page.insert_text((160,90),'p',fontname='owned',fontsize=30,
                         morph=(fitz.Point(160,90),fitz.Matrix(.5,0,0,2,0,0)))
        _,glyphs=_collect(page);self.assertEqual(len(glyphs),2)
        proof=measure_selected_native_ink(page,glyphs,{g['id']for g in glyphs})
        self.assertEqual(proof['unverified'],[])
        self.assertAlmostEqual(glyphs[0]['size'],glyphs[1]['size'],places=5)
        self.assertGreater(fitz.Rect(glyphs[0]['native_ink_bbox']).width,
                           fitz.Rect(glyphs[1]['native_ink_bbox']).width*3.5)
        self.assertNotEqual(glyphs[0]['native_ink_geometry']['effective_matrix'][:4],
                            glyphs[1]['native_ink_geometry']['effective_matrix'][:4])

    def test_page_cache_is_invalidated_when_original_content_changes(self):
        doc,page=self.document('p');_,old=_collect(page)
        first=measure_selected_native_ink(page,old,{old[0]['id']})
        self.assertEqual(len(first['verified']),1)
        page.insert_text((150,90),'x',fontname='owned',fontsize=30)
        _,current=_collect(page)
        second=measure_selected_native_ink(page,current,{g['id']for g in current})
        self.assertEqual(second['unverified'],[]);self.assertEqual(len(second['verified']),2)

    def test_same_embedded_program_resource_alias_is_not_distinct_subset_ambiguity(self):
        doc,page=self.document('p');page.insert_text((150,90),'x',fontname='alias',fontfile=self.font,fontsize=30)
        _,glyphs=_collect(page)
        proof=measure_selected_native_ink(page,glyphs,{g['id']for g in glyphs})
        self.assertEqual(proof['unverified'],[])
        self.assertEqual(len({g['native_ink_geometry']['font_program_sha256']for g in glyphs}),1)

    def test_native_geometry_helper_hash_invalidates_analysis_checkpoint_signature(self):
        doc,page=self.document('p\u2192x');source=self.root/'source.pdf';doc.save(source)
        first=analyze(source,self.root/'analysis-first',backend='native',ocr='off')
        def changed_helper(path):
            return '0'*64 if Path(path).name=='native_ink_geometry.py'else file_sha256(path)
        with patch('textbook_pdf.extraction.file_sha256',side_effect=changed_helper):
            second=analyze(source,self.root/'analysis-second',backend='native',ocr='off')
        self.assertNotEqual(first.profile['analysis_signature'],second.profile['analysis_signature'])

    def saved_document(self,*,nested=False):
        source,source_page=self.document('p')
        if nested:
            document=fitz.open();self.addCleanup(document.close)
            page=document.new_page(width=240,height=200)
            page.show_pdf_page(page.rect,source,0)
        else:document=source
        path=self.root/'immutable-source.pdf';document.save(path)
        reopened=fitz.open(path);self.addCleanup(reopened.close)
        return reopened,reopened[0],path,file_sha256(path)

    @staticmethod
    def text_form(document,page):
        found=[row[0]for row in page.get_xobjects()if b'BT' in document.xref_stream(row[0])]
        if len(found)!=1:raise AssertionError('Expected one original inner text Form')
        return found[0]

    def test_immutable_source_repeated_measurement_extracts_each_font_program_once(self):
        doc,page,path,sha=self.saved_document();_,glyphs=_collect(page)
        with patch.object(doc,'extract_font',wraps=doc.extract_font)as extract:
            for _ in range(25):
                result=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']},immutable_source_sha256=sha)
                self.assertEqual(result['unverified'],[])
            self.assertEqual(extract.call_count,len({row[0]for row in page.get_fonts(full=True)}))
        stats=doc._textbook_capture_ink_v2['stats']
        self.assertEqual(stats['immutable_source_file_hashes'],1)
        self.assertEqual(stats['native_operator_page_reads'],1)
        self.assertEqual(stats['dependency_objects_hashed'],0)

    def test_mutable_nested_form_change_invalidates_index_with_direct_contents_unchanged(self):
        source,_=self.document();doc=fitz.open();self.addCleanup(doc.close)
        page=doc.new_page(width=240,height=200);page.show_pdf_page(page.rect,source,0)
        _,glyphs=_collect(page);first=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']})
        self.assertEqual(first['unverified'],[])
        matrix=glyphs[0]['native_ink_geometry']['effective_matrix']
        direct=[doc.xref_stream(xref)for xref in page.get_contents()]
        form=self.text_form(doc,page);body=doc.xref_stream(form)
        doc.update_stream(form,b'q 1 0 0 1 12 0 cm\n'+body+b'\nQ')
        self.assertEqual(direct,[doc.xref_stream(xref)for xref in page.get_contents()])
        _,current=_collect(page);second=measure_selected_native_ink(page,current,{current[0]['id']})
        self.assertEqual(second['unverified'],[])
        self.assertAlmostEqual(current[0]['native_ink_geometry']['effective_matrix'][4]-matrix[4],12,places=4)
        self.assertEqual(doc._textbook_capture_ink_v2['stats']['native_operator_page_reads'],2)

    def test_attested_immutable_nested_form_mutation_fails_and_clears_old_capture(self):
        doc,page,path,sha=self.saved_document(nested=True);_,glyphs=_collect(page)
        first=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']},immutable_source_sha256=sha)
        self.assertEqual(first['unverified'],[]);self.assertIn('native_ink_bbox',glyphs[0])
        form=self.text_form(doc,page);doc.update_stream(form,b'q\n'+doc.xref_stream(form)+b'\nQ')
        second=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']},immutable_source_sha256=sha)
        self.assertEqual(second['verified'],[])
        self.assertEqual(second['unverified'][0]['reason'],'declared_immutable_source_document_was_mutated')
        self.assertNotIn('native_ink_bbox',glyphs[0]);self.assertNotIn('native_ink_geometry',glyphs[0])

    def test_mutable_font_program_change_cannot_reuse_old_geometry(self):
        doc,page=self.document();_,glyphs=_collect(page)
        first=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']});self.assertEqual(first['unverified'],[])
        todo=[page.get_fonts(full=True)[0][0]];seen=set();fontfile=None
        while todo and fontfile is None:
            xref=todo.pop()
            if xref in seen:continue
            seen.add(xref)
            for key in ('FontFile','FontFile2','FontFile3'):
                kind,value=doc.xref_get_key(xref,key)
                if kind=='xref':fontfile=int(value.split()[0]);break
            todo.extend(int(value)for value in re.findall(r'\b(\d+)\s+\d+\s+R\b',doc.xref_object(xref)))
        self.assertIsNotNone(fontfile)
        alternate=Path(__file__).parent/'fixtures/original-ordinary-ink-distinct.ttf'
        doc.update_stream(fontfile,alternate.read_bytes())
        second=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']})
        self.assertEqual(second['verified'],[])
        self.assertEqual(second['unverified'][0]['reason'],'mutable_embedded_font_program_changed')
        self.assertNotIn('native_ink_bbox',glyphs[0]);self.assertNotIn('native_ink_geometry',glyphs[0])

    def test_invalid_selected_ids_clear_previously_derived_capture_fields(self):
        doc,page=self.document();_,glyphs=_collect(page)
        first=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']});self.assertEqual(first['unverified'],[])
        second=measure_selected_native_ink(page,glyphs,{glyphs[0]['id'],'absent-source-id'})
        self.assertEqual(second['verified'],[])
        self.assertNotIn('native_ink_bbox',glyphs[0]);self.assertNotIn('native_ink_geometry',glyphs[0])

    def test_new_unverified_render_mapping_clears_prior_capture(self):
        doc,page=self.document();_,glyphs=_collect(page)
        measure_selected_native_ink(page,glyphs,{glyphs[0]['id']});self.assertIn('native_ink_bbox',glyphs[0])
        before=deepcopy(glyphs[0]['bbox']);page.set_rotation(90)
        result=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']})
        self.assertEqual(result['verified'],[]);self.assertNotIn('native_ink_bbox',glyphs[0])
        self.assertEqual(before,glyphs[0]['bbox'])

    def test_source_file_stat_change_invalidates_immutable_attestation(self):
        doc,page,path,sha=self.saved_document();_,glyphs=_collect(page)
        measure_selected_native_ink(page,glyphs,{glyphs[0]['id']},immutable_source_sha256=sha)
        with path.open('ab')as stream:stream.write(b'\n% changed source identity\n')
        result=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']},immutable_source_sha256=sha)
        self.assertEqual(result['verified'],[])
        self.assertEqual(result['unverified'][0]['reason'],'declared_immutable_source_identity_changed')
        self.assertNotIn('native_ink_bbox',glyphs[0])

    def test_existing_asset_cannot_hide_current_native_measurement_failure(self):
        doc,page=self.document();_,glyphs=_collect(page)
        book=Book('owned-memory.pdf','owned-synthetic-v1','Owned',1,[1]);book._active_glyphs=glyphs
        first=_asset(book,doc,page,self.root,'formula',glyphs[0]['bbox'],'p',90,{'glyph_ids':[glyphs[0]['id']]})
        page.insert_text((70,90),'p',fontname='owned',fontsize=30)
        with self.assertRaisesRegex(ValueError,'Cannot reuse accepted asset'):
            _asset(book,doc,page,self.root,'formula',first.bbox,'p',90,{'glyph_ids':[glyphs[0]['id']]})
        self.assertNotIn('native_ink_bbox',glyphs[0])

    def test_outer_form_microscale_passes_legacy_identity_but_is_blocked_by_strong_gate(self):
        doc,page=self.document();_,glyphs=_collect(page)
        original=fitz.Page.show_pdf_page
        def scaled(target,*args,**kwargs):
            xref=original(target,*args,**kwargs)
            # Scale by 0.01% about the original x70 origin: ordinary origins
            # remain within .03pt, but the native linear matrix is changed.
            target.parent.xref_set_key(xref,'Matrix','[1.0001 0 0 1 -0.007 0]')
            return xref
        with patch.object(fitz.Page,'show_pdf_page',new=scaled):
            book,asset=self.asset(doc,page,glyphs)
        proof=asset.metadata['native_ink_output_verification']
        self.assertTrue(proof['legacy_identity_layer']['verified'])
        self.assertFalse(proof['verified'])
        self.assertIn('actual_target_linear_matrix_changed',[row['reason']for row in proof['failures']])
        self.assertTrue(any(issue['code']=='source_native_ink_output_unverified'and issue['asset_id']==asset.id
                            and issue['severity']=='error'for issue in book.issues))

    def test_actual_target_ink_support_must_fit_visible_frame(self):
        doc,page=self.document();_,glyphs=_collect(page)
        book,asset=self.asset(doc,page,glyphs)
        with fitz.open(self.root/asset.path)as saved:
            target=saved[0];target.set_cropbox(fitz.Rect(0,0,1,target.rect.height))
            result=verify_actual_capture(page,target,glyphs,{glyphs[0]['id']},
                                         source_to_target=[-asset.bbox[0],-asset.bbox[1]])
        self.assertFalse(result['verified'])
        self.assertIn('actual_target_native_ink_support_outside_visible_page',[row['reason']for row in result['failures']])

    def test_changed_valid_native_gid_cannot_reuse_old_asset_with_same_text_and_box(self):
        doc,page=self.document();_,glyphs=_collect(page)
        book=Book('owned-memory.pdf','owned-synthetic-v1','Owned',1,[1]);book._active_glyphs=glyphs
        broad=fitz.Rect(40,40,110,120)
        first=_asset(book,doc,page,self.root,'formula',broad,'p',90,{'glyph_ids':[glyphs[0]['id']]})
        old_gid=glyphs[0]['native_ink_geometry']['native_glyph_id']
        fontxref=page.get_fonts(full=True)[0][0]
        unicode_xref=int(doc.xref_get_key(fontxref,'ToUnicode')[1].split()[0])
        cmap=doc.xref_stream(unicode_xref);self.assertIn(b'<0002> <0078>',cmap)
        doc.update_stream(unicode_xref,cmap.replace(b'<0002> <0078>',b'<0002> <0070>'))
        for xref in page.get_contents():
            body=doc.xref_stream(xref);doc.update_stream(xref,body.replace(b'<0001>',b'<0002>'))
        changed=self.root/'changed-mapping.pdf';doc.save(changed)
        with fitz.open(changed)as current_doc:
            current_page=current_doc[0];_,current=_collect(current_page)
            self.assertEqual(current[0]['c'],'p');book._active_glyphs=current
            measured=measure_selected_native_ink(current_page,current,{current[0]['id']})
            self.assertEqual(measured['unverified'],[])
            self.assertNotEqual(current[0]['native_ink_geometry']['native_glyph_id'],old_gid)
            with self.assertRaisesRegex(ValueError,'native capture identity changed'):
                _asset(book,current_doc,current_page,self.root,'formula',first.bbox,'p',90,{'glyph_ids':[current[0]['id']]})


if __name__=='__main__':unittest.main()
