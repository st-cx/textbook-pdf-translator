"""Native fraction assembly must keep ink and remove duplicate literal runs."""
from pathlib import Path
import shutil
import unittest
import uuid
from unittest.mock import patch

import pymupdf as fitz

from textbook_pdf.extraction import _asset, _collect, _finalize_assets, _promote_formula_prose
from textbook_pdf.math_regions import assemble_native_regions
from textbook_pdf.models import Asset, Book, Element, TOKEN_RE


class NativeMathRegionTests(unittest.TestCase):
    def boundary_fixture(self,*,bridge=None,same_group=False,vertical_operator=False):
        book=Book('synthetic.pdf','source','Fixture',1,[1])
        coordinates=([40,20,50,34],[40,32,50,46]) if bridge is None else ([40,20,50,30],[40,38,50,48])
        if bridge=='neutral':coordinates=([40,20,50,34],[40,38,50,52])
        if bridge=='limit':coordinates=([40,20,50,30],[40,35,50,45])
        glyphs=[];elements=[]
        for n,box in enumerate(coordinates,1):
            gid=f'g{n}';eid=f'question{n}';aid=f'asset{n}'
            char='∑'if vertical_operator and n==1 else ('x'if n==1 else 'y')
            glyphs.append({'id':gid,'c':char,'font':'fixture','size':12,'bbox':box,
                           'origin':[box[0],box[3]-2],'owner':eid})
            book.assets[aid]=Asset(aid,'formula','not-opened.pdf',1,box,10,box[3]-box[1],
                text=char,metadata={'glyph_ids':[gid]})
            elements.append(Element(eid,'exercise',f'{n}. ⟦F:{aid}⟧',1,[10,box[1],box[2],box[3]],
                asset_ids=[aid],group_id='same-question'if same_group else eid,
                metadata={'glyph_ids':[gid],'list_label':str(n),'source_text':f'{n}. {char}'}))
        drawings=[]
        if bridge=='neutral':
            box=[40,29,50,43];gid='middle';aid='middle-asset';eid='ungrouped'
            glyphs.append({'id':gid,'c':'=','font':'fixture','size':12,'bbox':box,'origin':[40,41],'owner':eid})
            book.assets[aid]=Asset(aid,'formula','not-opened.pdf',1,box,10,14,text='=',metadata={'glyph_ids':[gid]})
            elements.insert(1,Element(eid,'formula',f'⟦F:{aid}⟧',1,box,asset_ids=[aid],metadata={'glyph_ids':[gid]}))
        if bridge=='bar':drawings=[{'rect':fitz.Rect(39,34,51,34)}]
        page=type('Page',(),{'rect':fitz.Rect(0,0,120,100)})()
        created=[]
        def asset(*args,**kwargs):
            _,_,_,_,kind,box,text,baseline,metadata=args
            aid='merged-'+str(len(created));r=fitz.Rect(box)
            result=Asset(aid,kind,'not-opened.pdf',1,list(r),r.width,r.height,text=text,metadata=metadata)
            book.assets[aid]=result;created.append(result);return result
        with patch('textbook_pdf.extraction._asset',side_effect=asset):
            proofs=assemble_native_regions(book,None,page,None,elements,glyphs,drawings)
        return book,elements,created,proofs

    def test_different_numbered_exercises_cannot_merge_from_overlapping_font_boxes(self):
        _,elements,_,proofs=self.boundary_fixture()
        self.assertEqual(proofs,[])
        self.assertTrue(all(TOKEN_RE.search(e.text)for e in elements))

    def test_ungrouped_middle_node_cannot_bridge_two_numbered_exercises(self):
        _,elements,created,_=self.boundary_fixture(bridge='neutral')
        self.assertFalse(any({'g1','g2'}<=set(a.metadata['glyph_ids'])for a in created))
        self.assertTrue(all(TOKEN_RE.search(e.text)for e in elements if e.kind=='exercise'))

    def test_fraction_bar_cannot_bridge_different_numbered_exercises(self):
        _,elements,created,_=self.boundary_fixture(bridge='bar')
        self.assertFalse(any({'g1','g2'}<=set(a.metadata['glyph_ids'])for a in created))
        self.assertTrue(all(TOKEN_RE.search(e.text)for e in elements if e.kind=='exercise'))

    def test_one_exercise_group_still_allows_an_original_multiline_region(self):
        _,_,created,proofs=self.boundary_fixture(same_group=True)
        self.assertEqual(len(proofs),1)
        self.assertTrue({'g1','g2'}<=set(created[0].metadata['glyph_ids']))

    def test_vertical_operator_is_not_an_escape_from_numbered_group_boundary(self):
        _,_,created,_=self.boundary_fixture(bridge='bar',vertical_operator=True)
        self.assertFalse(any({'g1','g2'}<=set(a.metadata['glyph_ids'])for a in created))

    def test_upper_or_lower_limit_cannot_bridge_different_numbered_groups(self):
        _,_,created,_=self.boundary_fixture(bridge='limit',vertical_operator=True)
        self.assertFalse(any({'g1','g2'}<=set(a.metadata['glyph_ids'])for a in created))

    def test_original_vertical_operator_and_limit_within_one_group_still_join(self):
        _,_,created,proofs=self.boundary_fixture(bridge='limit',vertical_operator=True,same_group=True)
        self.assertEqual(len(proofs),1)
        self.assertTrue({'g1','g2'}<=set(created[0].metadata['glyph_ids']))

    def test_nonembedded_base14_stays_an_explicit_source_ink_error(self):
        directory = Path(__file__).parent / '_tmp_math_regions' / uuid.uuid4().hex
        (directory / 'assets').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, directory)
        with fitz.open() as document:
            page = document.new_page(width=160, height=100)
            page.insert_text((30, 50), 'x', fontsize=12)
            self.assertTrue(all(not document.extract_font(row[0])[3]
                                for row in page.get_fonts(full=True)))
            lines, glyphs = _collect(page)
            book = Book('synthetic.pdf', 'source', 'Base14 negative', 1, [1])
            book._active_glyphs = glyphs
            asset = _asset(book, document, page, directory, 'formula', lines[0]['bbox'], 'x', 50,
                           {'glyph_ids': [g['id'] for g in glyphs]})
            capture = asset.metadata['native_ink_capture']
            self.assertEqual(capture['verified'], [])
            self.assertEqual({item['reason'] for item in capture['unverified']},
                             {'original_embedded_font_program_unavailable'})
            errors = [issue for issue in book.issues if issue['severity'] == 'error']
            self.assertTrue(any(issue['code'] == 'source_native_glyph_ink_unverified'
                                and issue['asset_id'] == asset.id for issue in errors), errors)
            self.assertFalse(asset.metadata['native_ink_output_verification']['verified'])
            _finalize_assets(book, 1, directory)

    def test_missing_selected_native_glyph_is_blocking_even_if_asset_pdf_is_generated(self):
        directory = Path(__file__).parent / '_tmp_math_regions' / uuid.uuid4().hex
        (directory / 'assets').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, directory)
        with fitz.open() as document:
            page = document.new_page(width=160, height=100)
            page.insert_font(fontname='fixture-sans',fontbuffer=fitz.Font('helv').buffer)
            page.insert_text((30, 50), 'x+1', fontsize=12,fontname='fixture-sans')
            lines, glyphs = _collect(page)
            book = Book('synthetic.pdf', 'source', 'Fixture', 1, [1])
            book._active_glyphs = glyphs
            def delete_selected_text(copy, rectangle, **kwargs):
                copy.add_redact_annot(rectangle,fill=False,cross_out=False)
                copy.apply_redactions(images=0,graphics=0,text=0)
                copy.draw_line((30,45),(40,45),width=.2)
                return {'method':'intentional-real-glyph-loss'}
            with patch('textbook_pdf.native_clipping.clip_native_content', side_effect=delete_selected_text):
                asset = _asset(book, document, page, directory, 'formula', lines[0]['bbox'], 'x+1', 50,
                               {'glyph_ids': [g['id'] for g in glyphs]})
            self.assertFalse(asset.metadata['selected_native_glyphs_verified'])
            self.assertEqual(len(asset.metadata['selected_glyphs_lost']), 3)
            self.assertEqual(book.issues[0]['code'], 'formula_crop_selected_glyph_lost')
            self.assertEqual(book.issues[0]['severity'], 'error')
            _finalize_assets(book, 1, directory)

    def test_delimiter_in_separate_pdf_line_is_absorbed_at_its_exact_prose_position(self):
        directory = Path(__file__).parent / '_tmp_math_regions' / uuid.uuid4().hex
        (directory / 'assets').mkdir(parents=True)
        self.addCleanup(shutil.rmtree, directory)
        with fitz.open() as document:
            page = document.new_page(width=240, height=180)
            page.insert_font(fontname='fixture-italic',fontbuffer=fitz.Font('tiit').buffer)
            page.insert_text((70, 60), '(x', fontsize=12, fontname='fixture-italic')
            page.insert_text((81, 60), ')', fontsize=12, fontname='fixture-italic')
            _, glyphs = _collect(page)
            chosen = [g for g in glyphs if g['c'] != ')']
            book = Book('synthetic.pdf', 'synthetic-source', 'Synthetic', 1, [1])
            book._active_glyphs = glyphs
            original = _asset(book, document, page, directory, 'formula',
                              fitz.Rect(chosen[0]['bbox']) | fitz.Rect(chosen[1]['bbox']),
                              '(x', 60, {'glyph_ids': [g['id'] for g in chosen]})
            element = Element('body', 'paragraph', f'⟦F:{original.id}⟧ )', 1,
                              asset_ids=[original.id], metadata={'glyph_ids': [g['id'] for g in glyphs]})
            for glyph in glyphs:
                glyph['owner'] = element.id
            proofs = assemble_native_regions(book, document, page, directory, [element], glyphs, [])
            self.assertEqual(len(proofs), 1)
            self.assertNotIn(')', TOKEN_RE.sub('', element.text))
            self.assertEqual(book.assets[TOKEN_RE.findall(element.text)[0]].text, '(x)')
            self.assertEqual({g['owner'] for g in glyphs}, {'body'})
            for bundle in getattr(book, '_asset_bundles', {}).values():
                bundle.close()

    def test_math_heavy_line_with_unprotected_words_is_translatable_prose(self):
        elements = [Element('body', 'formula', '⟦F:math⟧, because ⟦F:other⟧.', 1),
                    Element('equation', 'formula', '⟦F:math⟧', 1)]
        _promote_formula_prose(elements)
        self.assertEqual([e.kind for e in elements], ['paragraph', 'formula'])

    def test_fraction_keeps_bar_and_denominator_once(self):
        directory = Path(__file__).parent / '_tmp_math_regions' / uuid.uuid4().hex
        directory.mkdir(parents=True)
        (directory / 'assets').mkdir()
        self.addCleanup(shutil.rmtree, directory)
        document = fitz.open()
        self.addCleanup(document.close)
        page = document.new_page(width=240, height=180)
        page.insert_font(fontname='fixture-italic',fontbuffer=fitz.Font('tiit').buffer)
        page.insert_font(fontname='fixture-sans',fontbuffer=fitz.Font('helv').buffer)
        page.insert_text((70, 60), '(x + 1)', fontsize=12, fontname='fixture-italic')
        page.insert_text((82, 84), '2', fontsize=12,fontname='fixture-sans')
        page.draw_line((69, 66), (110, 66))
        lines, glyphs = _collect(page)
        numerator = next(line for line in lines if 'x' in line['text'])
        denominator = next(line for line in lines if line['text'] == '2')
        book = Book('synthetic.pdf', 'synthetic-source', 'Synthetic', 1, [1])
        book._active_glyphs = glyphs
        original = _asset(book, document, page, directory, 'formula', numerator['bbox'],
                          numerator['text'], 60, {'glyph_ids': [g['id'] for g in numerator['chars']]})
        first = Element('top', 'formula', f'⟦F:{original.id}⟧', 1, numerator['bbox'],
                        asset_ids=[original.id], metadata={'glyph_ids': [g['id'] for g in numerator['chars']]})
        second = Element('bottom', 'exercise', '2', 1, denominator['bbox'],
                         metadata={'glyph_ids': [g['id'] for g in denominator['chars']]})
        book.issues = [{'code': 'formula_crop_contamination', 'severity': 'error', 'page': 1,
                        'detail': 'Cannot safely isolate 1 overlapping unrelated glyphs in asset '+original.id},
                       {'code': 'formula_crop_contamination', 'severity': 'error', 'page': 1,
                        'detail': 'unrelated unresolved component'}]
        for g in numerator['chars']:
            g.update(owner='top', asset_id=original.id)
        for g in denominator['chars']:
            g['owner'] = 'bottom'
        proofs = assemble_native_regions(book, document, page, directory,
                                         [first, second], glyphs, page.get_drawings())
        self.assertEqual(len(proofs), 1)
        result = book.assets[TOKEN_RE.findall(first.text)[0]]
        self.assertGreater(result.height, 25)
        self.assertTrue(result.metadata['fraction_bar_bboxes'])
        self.assertEqual(second.text, '')
        self.assertTrue(second.metadata['render_suppressed'])
        self.assertEqual({g['owner'] for g in glyphs}, {'top'})
        self.assertEqual(set(result.metadata['glyph_ids']), {g['id'] for g in glyphs})
        self.assertEqual(len(book.issues), 1)
        self.assertEqual(book.profile['resolved_source_issues'][0]['replacement_asset_id'], result.id)
        _finalize_assets(book, 1, directory)
        with fitz.open(directory / result.path) as bundle:
            crop = bundle[result.metadata['pdf_page']]
            self.assertIn('2', crop.get_text())
            self.assertTrue(any(d['rect'].width > 35 for d in crop.get_drawings()))

    def test_foreign_prose_inside_union_prevents_assembly(self):
        book = Book('synthetic.pdf', 'source', 'Synthetic', 1, [1])
        nodes = [
            {'id': 'x', 'c': 'x', 'bbox': [10, 10, 20, 20], 'origin': [10, 18]},
            {'id': 'y', 'c': 'y', 'bbox': [22, 10, 32, 20], 'origin': [22, 18]},
            {'id': 'prose', 'c': 'W', 'bbox': [19, 10, 23, 20], 'origin': [19, 18]},
        ]
        elements = []
        for g in nodes[:2]:
            aid = 'a-' + g['id']
            book.assets[aid] = Asset(aid, 'formula', 'not-opened.pdf', 1, g['bbox'], 10, 10,
                                     text=g['c'], metadata={'glyph_ids': [g['id']]})
            elements.append(Element(g['id'], 'formula', f'⟦F:{aid}⟧', 1, g['bbox'], asset_ids=[aid]))
        page = type('Page', (), {'rect': fitz.Rect(0, 0, 100, 100)})()
        result = assemble_native_regions(book, None, page, None, elements, nodes, [])
        self.assertEqual(result, [])
        self.assertEqual(len(book.assets), 2)


if __name__ == '__main__':
    unittest.main()
