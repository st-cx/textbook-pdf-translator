"""Native RHS-number wraps must not consume real exercise numbers."""
from __future__ import annotations

from collections import Counter
import copy
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import unittest
from unittest.mock import patch
import uuid

import pymupdf as fitz

from textbook_pdf.extraction import _collect, _columns, _order, analyze
from textbook_pdf.models import TOKEN_RE, source_contract_issues, file_sha256
from textbook_pdf.source_flow import annotate_rhs_numeric_continuations, rhs_numeric_continuation


def row(text, y=50., block=1, line=0, x=50., size=10.):
    chars=[{'id':f'b{block}.l{line}.c{i}', 'c':c, 'font':'Helvetica',
            'flags':0, 'size':size, 'origin':[x+i*5,y],
            'bbox':[x+i*5,y-8,x+(i+1)*5,y+2], 'color':0,
            'owner':None} for i,c in enumerate(text)]
    return {'text':text,'chars':chars,'block':block,'line':line,
            'bbox':[x,y-8,x+len(chars)*5,y+2]}


COLS={'count':1,'split':300.,'start_y':float('inf')}


class NumericRhsEvidenceTests(unittest.TestCase):
    def test_same_native_flow_splits_only_number_not_period_or_sentence(self):
        a=row('Let f(e) =');b=row('18. The quotients follow.',62,line=1)
        evidence=rhs_numeric_continuation(a,b,COLS)
        self.assertEqual(evidence['number'],'18')
        self.assertEqual(evidence['glyph_ids'],[g['id'] for g in b['chars'][:2]])
        self.assertNotIn(b['chars'][2]['id'],evidence['glyph_ids'])

    def test_never_mutates_original_rows_or_glyphs(self):
        rows=[row('Let f(e) ='),row('1. The image follows.',62,line=1)]
        before=copy.deepcopy(rows)
        marked=annotate_rhs_numeric_continuations(rows,COLS)
        self.assertEqual(rows,before)
        self.assertIs(marked[0],rows[0])
        self.assertIs(marked[1]['chars'],rows[1]['chars'])

    def test_complete_previous_row_does_not_consume_real_next_question(self):
        a=row('14. Show x = 1 and finish the proof.')
        b=row('15. Prove the next claim.',62,line=1)
        self.assertIsNone(rhs_numeric_continuation(a,b,COLS))

    def test_bold_marker_vetoes_even_an_incomplete_previous_equation(self):
        a=row('Let x =');b=row('3. Is this true?',62,line=1)
        for g in b['chars'][:2]:g['flags']=16
        self.assertIsNone(rhs_numeric_continuation(a,b,COLS))

    def test_column_block_line_and_indentation_boundaries(self):
        a=row('Let x =')
        for key,value in [('block',2),('line',2),('x',100.)]:
            with self.subTest(key=key):
                kwargs={'block':1,'line':1,'x':50.};kwargs[key]=value
                self.assertIsNone(rhs_numeric_continuation(a,row('1. The result follows.',62,**kwargs),COLS))
        b=row('1. The result follows.',62,line=1)
        b['bbox']=[350,54,470,64]
        self.assertIsNone(rhs_numeric_continuation(a,b,{'count':2,'split':300,'start_y':0}))

    def test_font_size_color_and_baseline_are_not_tolerated_as_guesses(self):
        a=row('Let x =')
        for key,value in [('font','Courier'),('size',9.5),('color',0x0084aa),('origin',[50,63])]:
            with self.subTest(key=key):
                b=row('1. The result follows.',62,line=1)
                b['chars'][0][key]=value
                self.assertIsNone(rhs_numeric_continuation(a,b,COLS))

    def test_far_vertical_gap_and_scripted_or_owned_prefix_are_rejected(self):
        a=row('Let x =')
        self.assertIsNone(rhs_numeric_continuation(a,row('1. The result follows.',80,line=1),COLS))
        for key,value in [('flags',2),('owner','figure')]:
            b=row('1. The result follows.',62,line=1);b['chars'][0][key]=value
            self.assertIsNone(rhs_numeric_continuation(a,b,COLS))

    def test_only_an_unambiguous_integer_sentence_prefix_is_supported(self):
        for text in ['1.5 is a decimal.','1. x is variable.','1) The next question.',
                     '1. THE TITLE','-1. The value follows.']:
            self.assertIsNone(rhs_numeric_continuation(row('Let x ='),row(text,62,line=1),COLS))


class NumericRhsPdfTests(unittest.TestCase):
    def setUp(self):
        self.root=Path.cwd()/'.test-source-flow'/uuid.uuid4().hex
        self.root.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_real_native_pdf_keeps_glyphs_and_question_boundary(self):
        source=self.root/'source.pdf'
        doc=fitz.open();p=doc.new_page(width=300,height=200)
        p.insert_text((50,50),'Let f(e) =\n1. The image follows.\n2. Prove the next claim.',fontsize=10)
        doc.save(source);doc.close()
        book=analyze(source,self.root/'job',[1],backend='native',ocr='off')
        active=[e for e in book.elements if not e.metadata.get('render_suppressed')]
        fixed=[e for e in active if e.metadata.get('rhs_numeric_continuations')]
        self.assertEqual(len(fixed),1)
        element=fixed[0]
        expanded=TOKEN_RE.sub(lambda m:book.assets[m[1]].text,element.text)
        self.assertRegex(expanded,r'f\(e\)\s*=\s*1\. The image follows[.]')
        self.assertNotIn('2. Prove',expanded)
        self.assertTrue(any(e.kind=='exercise' and e.text.startswith('2. Prove') for e in active))
        joined=[book.assets[aid] for aid in element.asset_ids
                if book.assets[aid].metadata.get('rhs_numeric_continuation')]
        self.assertEqual(len(joined),1)
        self.assertNotIn('.',joined[0].text)
        self.assertEqual(joined[0].metadata['native_segment_geometry'],'translation-only-recorded-v1')
        self.assertTrue(all('target_bbox' in s for s in joined[0].metadata['source_segments']))
        self.assertEqual(source_contract_issues(book),[])
        with gzip.open(self.root/'job'/'coverage'/'p0001.json.gz','rt',encoding='utf8') as f:
            ledger=json.load(f)
        self.assertEqual(len({g['id'] for g in ledger}),len(ledger))
        self.assertTrue(all(g['owner'] in {e.id for e in active} or
                            (g['c'].isspace() and g['disposition']=='excluded-whitespace') for g in ledger))
        # Presence is tested using the actual saved PDF, not token strings.
        with fitz.open(self.root/'job'/joined[0].path) as d:
            chars=Counter(c['c'] for block in d[joined[0].metadata['pdf_page']].get_text('rawdict')['blocks']
                          if block['type']==0 for l in block['lines'] for s in l['spans'] for c in s['chars'])
        self.assertEqual(chars['1'],1)
        self.assertEqual(chars['='],1)
        self.assertEqual(chars['.'],0)

    def test_crop_and_cross_page_code_changes_invalidate_native_checkpoint(self):
        source=self.root/'signature-source.pdf'
        doc=fitz.open();p=doc.new_page(width=300,height=200)
        p.insert_text((50,50),'A complete source sentence.',fontsize=10)
        doc.save(source);doc.close()
        for module in ['source_flow.py','native_clipping.py','cross_page_math.py']:
            with self.subTest(module=module):
                directory=self.root/module
                first=analyze(source,directory,[1],backend='native',ocr='off')
                def altered_hash(path):
                    return 'f'*64 if Path(path).name==module else file_sha256(path)
                with patch('textbook_pdf.extraction.file_sha256',side_effect=altered_hash), \
                        patch('textbook_pdf.extraction._collect',wraps=_collect) as collect:
                    second=analyze(source,directory,[1],backend='native',ocr='off')
                self.assertNotEqual(first.profile['analysis_signature'],second.profile['analysis_signature'])
                self.assertEqual(collect.call_count,1,'Stale native checkpoint was reused')


SOURCE=Path(os.environ['TEXTBOOK_TEST_ROSEN_PDF']) if os.environ.get('TEXTBOOK_TEST_ROSEN_PDF') else None


@unittest.skipUnless(SOURCE and SOURCE.is_file(),'Set TEXTBOOK_TEST_ROSEN_PDF for private original-source checks')
class RosenNumericRhsTests(unittest.TestCase):
    def test_original_173_175_310_have_exact_three_proven_numeric_wraps(self):
        with fitz.open(SOURCE) as doc:
            for number,wanted in [(173,'1'),(175,'3'),(310,'18')]:
                with self.subTest(page=number):
                    page=doc[number-1];lines,glyphs=_collect(page)
                    marked=annotate_rhs_numeric_continuations(_order(lines,_columns(lines,page.rect.width)),_columns(lines,page.rect.width))
                    evidence=[r['rhs_numeric_continuation'] for r in marked if r.get('rhs_numeric_continuation')]
                    self.assertEqual([r['number'] for r in evidence],[wanted])
                    selected=[g for g in glyphs if g['id'] in evidence[0]['glyph_ids']]
                    self.assertEqual(''.join(g['c'] for g in selected),wanted)

    def test_original_136_real_question_15_is_never_a_numeric_rhs(self):
        with fitz.open(SOURCE) as doc:
            page=doc[135];lines,_=_collect(page);cols=_columns(lines,page.rect.width)
            marked=annotate_rhs_numeric_continuations(_order(lines,cols),cols)
            q=next(r for r in marked if r['text'].startswith('15. Prove or disprove'))
            self.assertNotIn('rhs_numeric_continuation',q)


if __name__=='__main__':
    unittest.main()
