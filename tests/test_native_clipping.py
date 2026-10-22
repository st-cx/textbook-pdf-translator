"""Real zero-advance native glyphs and outside-content removal, using our font."""
from pathlib import Path
from copy import deepcopy
import shutil
import unittest
from unittest.mock import patch
import uuid

import numpy as np
import pymupdf as fitz

from textbook_pdf.extraction import _asset, _collect, _finalize_assets
from textbook_pdf.models import Book, file_sha256
from textbook_pdf.native_clipping import clip_native_content
from textbook_pdf.source_integrity import _native_counts


class NativeClippingTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).parent/'_tmp_native_clipping'/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,self.root)
        self.source=self.root/'source.pdf'
        self.font=Path(__file__).parent/'fixtures/original-zero-advance.ttf'
        with fitz.open()as pdf:
            page=pdf.new_page(width=240,height=200)
            page.insert_text((30,70),'a|\u0338b',fontname='original',fontfile=self.font,fontsize=30)
            page.insert_text((180,160),'a|\u0338b',fontname='original',fontsize=30)
            pdf.save(self.source)
        self.clip=fitz.Rect(25,38,80,80)

    @staticmethod
    def count_mark(page):
        return sum(n for key,n in _native_counts(page).items() if key[0]=='\u0338')

    def test_stock_clip_reproduces_missing_negation_and_adapter_preserves_native_mark(self):
        with fitz.open(self.source)as stock:
            self.assertEqual(self.count_mark(stock[0]),2)
            stock[0].clip_to_rect(self.clip)
            self.assertEqual(self.count_mark(stock[0]),0)
        with fitz.open(self.source)as repaired:
            proof=clip_native_content(repaired[0],self.clip)
            self.assertEqual(self.count_mark(repaired[0]),1)
            self.assertEqual(proof['kept_zero_advance_glyphs'],1)
            self.assertEqual(proof['removed_outside_zero_advance_glyphs'],1)
            self.assertEqual(sum(_native_counts(repaired[0]).values()),4)

    def test_saved_asset_matches_original_pixels_and_removes_outside_searchable_text(self):
        before=file_sha256(self.source)
        with fitz.open(self.source)as source,fitz.open()as isolated:
            isolated.insert_pdf(source)
            clip_native_content(isolated[0],self.clip)
            with fitz.open()as target:
                page=target.new_page(width=self.clip.width,height=self.clip.height)
                page.show_pdf_page(page.rect,isolated,0,clip=self.clip)
                target.save(self.root/'asset.pdf')
            expected=source[0].get_pixmap(clip=self.clip,matrix=fitz.Matrix(6,6),alpha=False)
        with fitz.open(self.root/'asset.pdf')as target:
            actual=target[0].get_pixmap(matrix=fitz.Matrix(6,6),alpha=False)
            self.assertEqual((expected.width,expected.height),(actual.width,actual.height))
            self.assertTrue(np.array_equal(np.frombuffer(expected.samples,dtype=np.uint8),np.frombuffer(actual.samples,dtype=np.uint8)))
            self.assertEqual(sum(_native_counts(target[0]).values()),4)
        self.assertEqual(file_sha256(self.source),before)

    def test_asset_pipeline_keeps_zero_advance_character_and_original_object_geometry(self):
        (self.root/'assets').mkdir()
        with fitz.open(self.source)as pdf:
            _,glyphs=_collect(pdf[0])
            selected=[g for g in glyphs if g['origin'][1]<100]
            book=Book(str(self.source),file_sha256(self.source),'Original',1,[1])
            book._active_glyphs=glyphs
            asset=_asset(book,pdf,pdf[0],self.root,'formula',self.clip,'a|\u0338b',70,
                         {'glyph_ids':[g['id']for g in selected]})
            self.assertEqual(book.issues,[])
            self.assertTrue(asset.metadata['selected_native_glyphs_verified'])
            self.assertEqual(asset.metadata['selected_glyphs_lost'],[])
            self.assertEqual(asset.width,self.clip.width)
            self.assertEqual(asset.height,self.clip.height)
            _finalize_assets(book,1,self.root)
        with fitz.open(self.root/asset.path)as bundle:
            self.assertEqual(self.count_mark(bundle[asset.metadata['pdf_page']]),1)

    def test_zero_advance_mark_survives_all_four_native_text_directions(self):
        for angle in (0,90,180,270):
            with self.subTest(angle=angle),fitz.open()as pdf:
                page=pdf.new_page(width=240,height=200)
                page.insert_text((115,100),'a|\u0338b',fontname='original',fontfile=self.font,fontsize=25,rotate=angle)
                _,glyphs=_collect(page)
                crop=fitz.Rect(glyphs[0]['bbox'])
                for glyph in glyphs[1:]:crop|=fitz.Rect(glyph['bbox'])
                crop+=(-10,-10,10,10)
                proof=clip_native_content(page,crop)
                self.assertEqual(self.count_mark(page),1)
                self.assertEqual(sum(_native_counts(page).values()),4)
                self.assertEqual(proof['kept_zero_advance_glyphs'],1)

    def test_exact_native_operator_filter_removes_foreign_text_without_redrawing(self):
        with fitz.open(self.source)as pdf:
            _,glyphs=_collect(pdf[0]);selected=[g for g in glyphs if g['origin'][1]<100]
            proof=clip_native_content(pdf[0],self.clip,selected_glyphs=selected,source_glyphs=glyphs)
            self.assertTrue(proof['native_text_selection']['verified'])
            self.assertEqual(proof['native_text_selection']['selected_operator_seen_once'],4)
            self.assertEqual(sum(_native_counts(pdf[0]).values()),4)
            self.assertEqual(self.count_mark(pdf[0]),1)

    def test_exact_native_operator_filter_rejects_ambiguous_overlapping_source_ids(self):
        with fitz.open()as pdf:
            page=pdf.new_page(width=240,height=200)
            page.insert_text((30,70),'a',fontname='original',fontfile=self.font,fontsize=30)
            page.insert_text((30,70),'a',fontname='original',fontsize=30)
            _,glyphs=_collect(page)
            # _collect intentionally de-duplicates identical overprinted runs;
            # retain their two original source identities for this ambiguity
            # fixture instead of guessing which overlapping operator was meant.
            duplicate=deepcopy(glyphs[0]);duplicate['id']='second-overprinted-source-glyph'
            glyphs=[glyphs[0],duplicate]
            proof=clip_native_content(page,self.clip,selected_glyphs=glyphs[:1],source_glyphs=glyphs)
            self.assertFalse(proof['native_text_selection']['verified'])
            self.assertGreater(proof['native_text_selection']['ambiguous_selected_matches'],0)

    def test_native_pdf_page_rotation_fails_closed_before_mutation(self):
        for angle in (90,180,270):
            with self.subTest(angle=angle),fitz.open(self.source)as pdf:
                page=pdf[0];page.set_rotation(angle)
                before=pdf.tobytes(no_new_id=True)
                with self.assertRaisesRegex(ValueError,'coordinate normalization'):
                    clip_native_content(page,self.clip)
                self.assertEqual(pdf.tobytes(no_new_id=True),before)

    def test_final_visible_box_contains_selected_ink_instead_of_only_retaining_text(self):
        with fitz.open(self.source)as pdf:
            _,glyphs=_collect(pdf[0]);selected=[g for g in glyphs if g['origin'][1]<100]
            book=Book(str(self.source),file_sha256(self.source),'Original',1,[1]);book._active_glyphs=glyphs
            # Intentionally cut through the native word. The final, measured
            # object must grow to enclose the source glyphs at unchanged size.
            asset=_asset(book,pdf,pdf[0],self.root,'formula',[30,60,40,72],'a|\u0338b',70,
                         {'glyph_ids':[g['id']for g in selected]})
            for glyph in selected:
                box=fitz.Rect(glyph['bbox'])
                self.assertLessEqual(asset.bbox[0],box.x0)
                self.assertGreaterEqual(asset.bbox[2],box.x1)
                self.assertLessEqual(asset.bbox[1],box.y0)
                self.assertGreaterEqual(asset.bbox[3],box.y1)
            self.assertTrue(asset.metadata['selected_native_glyphs_verified'])
            for bundle in book._asset_bundles.values():bundle.close()

    def test_generated_pdf_file_alone_cannot_hide_a_missing_selected_glyph(self):
        from textbook_pdf import native_clipping
        def broken_clip(page,rectangle,**kwargs):
            # Reproduce the underlying clipping failure while still producing
            # a structurally valid PDF asset.
            page.clip_to_rect(rectangle)
            return {'method':'intentional-regression'}
        with fitz.open(self.source)as pdf:
            _,glyphs=_collect(pdf[0]);selected=[g for g in glyphs if g['origin'][1]<100]
            book=Book(str(self.source),file_sha256(self.source),'Original',1,[1]);book._active_glyphs=glyphs
            with patch.object(native_clipping,'clip_native_content',broken_clip):
                asset=_asset(book,pdf,pdf[0],self.root,'formula',self.clip,'a|\u0338b',70,
                    {'glyph_ids':[g['id']for g in selected],'selected_native_glyphs_verified':True})
            self.assertFalse(asset.metadata['selected_native_glyphs_verified'])
            self.assertTrue(asset.metadata['selected_glyphs_lost'])
            self.assertIn('formula_crop_selected_glyph_lost',{issue['code']for issue in book.issues})
            for bundle in book._asset_bundles.values():bundle.close()


if __name__=='__main__':unittest.main()
