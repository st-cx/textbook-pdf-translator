"""Real native-PDF matrices retain delimiters, cells and their labels."""
from collections import Counter
import gzip
import json
from pathlib import Path
import shutil
import unittest
import uuid

import pymupdf as fitz

from textbook_pdf.extraction import analyze
from textbook_pdf.models import TOKEN_RE,source_contract_issues
from textbook_pdf.native_containers import _container_pairs
from textbook_pdf.source_integrity import _native_counts,_signature


class NativeContainerTests(unittest.TestCase):
    def setUp(self):
        self.root=Path.cwd()/'.test-native-containers'/uuid.uuid4().hex
        self.root.mkdir(parents=True)
        shutil.copyfile(Path(__file__).parent/'fixtures/original-tall-containers.ttf',self.root/'own.ttf')

    def tearDown(self):
        shutil.rmtree(self.root)

    def source(self,language_inside=False):
        path=self.root/'source.pdf'
        d=fitz.open();p=d.new_page(width=300,height=200)
        p.insert_font(fontname='own',fontfile=str(self.root/'own.ttf'))
        p.insert_text((50,35),'A complete matrix follows.',fontsize=10)
        p.insert_text((69,64),'A =',fontsize=10)
        p.insert_text((90,60),'[',fontname='own',fontsize=10)
        p.insert_text((130,60),']',fontname='own',fontsize=10)
        if language_inside:p.insert_text((97,58),'cat',fontsize=10)
        else:
            for y,values in [(58,'101'),(70,'010')]:
                for x,c in zip([97,109,121],values):p.insert_text((x,y),c,fontsize=10)
        p.insert_text((50,115),'The next sentence remains prose.',fontsize=10)
        d.save(path);d.close();return path

    def test_actual_native_matrix_has_one_complete_object_and_no_duplicate_cells(self):
        source=self.source();job=self.root/'job'
        book=analyze(source,job,[1],backend='native',ocr='off')
        active=[e for e in book.elements if not e.metadata.get('render_suppressed')]
        with gzip.open(job/'coverage/p0001.json.gz','rt',encoding='utf8') as f:ledger=json.load(f)
        selected={g['id'] for g in ledger if g['c'] in '[]' or g['c'].isdigit()}
        refs={aid for e in active for aid in TOKEN_RE.findall(e.text)}
        matrices=[book.assets[aid] for aid in refs
                  if selected<=set(book.assets[aid].metadata.get('glyph_ids',[]))]
        self.assertEqual(len(matrices),1)
        matrix=matrices[0]
        self.assertIn('A',matrix.text);self.assertIn('=',matrix.text)
        self.assertEqual(matrix.text.count('0'),3)
        self.assertEqual(matrix.text.count('1'),3)
        self.assertGreater(matrix.width,55)
        self.assertGreater(matrix.height,23)
        owner=next(e for e in active if matrix.id in TOKEN_RE.findall(e.text))
        self.assertEqual({g['owner'] for g in ledger if g['id'] in selected},{owner.id})
        self.assertTrue(all(g['owner'] in {e.id for e in active} or
            (g['c'].isspace() and g['owner']=='whitespace' and g['disposition']=='excluded-whitespace')
            for g in ledger))
        self.assertEqual(sum(e.text.count(f'⟦F:{matrix.id}⟧') for e in active),1)
        self.assertTrue(any('The next sentence remains prose.' in e.text for e in active))
        self.assertFalse(any(e.text.strip() in ['101','010','1 0 1','0 1 0','A'] for e in active))
        self.assertFalse(any(i['severity']=='error' for i in book.issues),book.issues)
        self.assertEqual(source_contract_issues(book),[])
        chosen=[g for g in ledger if g['id'] in matrix.metadata['glyph_ids'] and g['c'].strip()]
        with fitz.open(job/matrix.path) as d:
            actual=_native_counts(d[matrix.metadata['pdf_page']])
            self.assertFalse(Counter(_signature(g['c'],g['font'],g['size']) for g in chosen)-actual)
            # The final native PDF itself retains two separate cell baselines.
            char_origins=[(c['c'],c['origin']) for b in d[matrix.metadata['pdf_page']].get_text('rawdict')['blocks']
                if 'lines' in b for l in b['lines'] for s in l['spans'] for c in s['chars'] if c['c'].isdigit()]
        self.assertEqual(len(char_origins),6)
        self.assertGreater(max(v[1][1] for v in char_origins)-min(v[1][1] for v in char_origins),11.9)

    def test_ordinary_language_inside_container_is_blocked_not_hidden_in_formula(self):
        book=analyze(self.source(True),self.root/'job',[1],backend='native',ocr='off')
        errors=[i for i in book.issues if i['code']=='source_math_container_unverified']
        self.assertTrue(errors,book.issues)
        self.assertTrue(all(i['severity']=='error' for i in errors))
        active=[e for e in book.elements if not e.metadata.get('render_suppressed')]
        self.assertTrue(any('cat' in e.text for e in active))
        self.assertFalse(any('cat' in book.assets[aid].text for e in active
                             for aid in TOKEN_RE.findall(e.text)))

    def test_stacked_native_delimiter_requires_complete_contiguous_caps(self):
        glyphs=[]
        for side,x,chars in [('left',0,'⎡⎢⎣'),('right',40,'⎤⎥⎦')]:
            for i,c in enumerate(chars):
                glyphs.append({'id':f'{side}{i}','c':c,'font':'STIXMathExtensions-Regul',
                    'size':10,'origin':[x,i*10+10],'bbox':[x,i*10,x+3,i*10+11],
                    'ink_geometry':{'method':'test-exact-geometric-components'}})
        pairs=_container_pairs(glyphs)
        self.assertEqual(len(pairs),1)
        self.assertEqual(pairs[0][0]['component_glyph_ids'],['left0','left1','left2'])
        self.assertEqual(pairs[0][1]['component_glyph_ids'],['right0','right1','right2'])
        self.assertEqual(pairs[0][0]['bbox'],[0,0,3,31])
        # A cap is indispensable; a code-point string alone cannot fill it in.
        self.assertEqual(_container_pairs([g for g in glyphs if g['id']!='left0']),[])
        changed=[dict(g) for g in glyphs]
        changed[1]['bbox']=[0,13,3,24]
        self.assertEqual(_container_pairs(changed),[])


if __name__=='__main__':unittest.main()
