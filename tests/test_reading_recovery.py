"""A renderer failure must not split a proved source unit or hide its cause."""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import pymupdf as fitz

from textbook_pdf.models import Asset, Book, Element, file_sha256
from textbook_pdf.reading_draft import compose_reading_draft, _atomic_source_cuts


class ReadingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).parent/('_tmp_recovery_'+uuid.uuid4().hex)
        self.root.mkdir();self.addCleanup(shutil.rmtree,self.root)
        source=self.root/'original.pdf'
        with fitz.open() as pdf:
            for n in range(1,5):
                page=pdf.new_page(width=300,height=400)
                page.insert_text((30,80),'Source page '+str(n))
            pdf.save(source)
        self.book=Book(str(source.resolve()),file_sha256(source),'Recovery demo',4,[1,2,3,4],
            pages=[{'number':n,'width':300,'height':400} for n in range(1,5)],
            elements=[Element('e'+str(n),'paragraph','Source paragraph.',n) for n in range(1,5)])
        for e in self.book.elements[1:3]:
            e.group_id='complete-cross-page-equation'
            e.metadata['cross_page_math']={'id':e.group_id,'source_pages':[2,3]}
        self.translations={e.id:'完整中文段落。' for e in self.book.elements}

    def compiler(self,book,translations,outdir,output_path=None,**kw):
        pages=set(book.selected_pages)
        if 1 in pages:
            raise RuntimeError('One unrelated page cannot render')
        if (2 in pages)!=(3 in pages):
            raise ValueError('The proved cross-page unit was cut in half')
        with fitz.open() as pdf:
            for n in book.selected_pages:
                page=pdf.new_page(width=300,height=400)
                page.insert_text((30,80),'Rendered source page '+str(n))
            pdf.save(output_path)
        target=Path(outdir)/'layout_map.json'
        target.write_text(json.dumps({'pages':[{'page':i+1,'source_pages':[n]}
            for i,n in enumerate(book.selected_pages)]}),encoding='utf8')
        return {'page_count':len(pages),'layout_map_path':str(target),'issues':[]}

    def run_draft(self,**kw):
        with patch('textbook_pdf.layout._Runtime.find'), \
                patch('textbook_pdf.layout.compose',side_effect=self.compiler):
            return compose_reading_draft(self.book,self.translations,self.root,**kw)

    def test_error_isolation_keeps_cross_page_unit_together(self):
        result=self.run_draft(chunk_pages=4)
        self.assertEqual(result['fallback_source_pages'],[1])
        self.assertEqual(result['displayed_translated_elements'],3)
        self.assertEqual(result['source_to_output'],{'1':[1],'2':[2],'3':[3],'4':[4]})
        self.assertTrue(any({2,3}<=set(part['source_pages']) for part in result['parts']))

    def test_partition_fallback_does_not_reintroduce_an_atomic_split(self):
        with patch('textbook_pdf.layout._partition_book',side_effect=ValueError('stale optional plan')):
            result=self.run_draft(chunk_pages=2)
        self.assertEqual(result['fallback_source_pages'],[1])
        self.assertTrue(any({2,3}<=set(part['source_pages']) for part in result['parts']))

    def test_measured_failure_retains_actionable_code_and_owner(self):
        self.book.elements[1].group_id=''
        self.book.elements[2].group_id=''
        for e in self.book.elements:e.metadata.clear()
        def measured(book,translations,outdir,output_path=None,**kw):
            # No deliberate page-one failure in this case.
            with fitz.open() as pdf:
                for n in book.selected_pages:pdf.new_page(width=300,height=400)
                pdf.save(output_path)
            path=Path(outdir)/'layout_map.json'
            path.write_text(json.dumps({'pages':[{'page':i+1,'source_pages':[n]}
                for i,n in enumerate(book.selected_pages)]}),encoding='utf8')
            issues=[{'code':'asset_too_wide','severity':'error','element_id':'e2','asset_id':'formula-two','page':2}] if 2 in book.selected_pages else []
            return {'page_count':len(book.selected_pages),'layout_map_path':str(path),'issues':issues}
        with patch('textbook_pdf.layout._Runtime.find'),patch('textbook_pdf.layout.compose',side_effect=measured):
            result=compose_reading_draft(self.book,self.translations,self.root)
        issue=next(i for i in result['issues'] if i.get('page')==2 and i['code']=='retained_untranslated_original_page')
        self.assertIn('asset_too_wide',issue.get('failure_codes',[]))
        self.assertIn('e2',issue.get('element_ids',[]))
        self.assertTrue(result.get('layout_failure_events'))

    def test_unused_archived_cross_page_asset_does_not_join_unrelated_pages(self):
        for e in self.book.elements:
            e.group_id=''
            e.metadata.clear()
        self.book.assets['archive']=Asset('archive','formula','unused.pdf',1,[0,0,10,10],10,10,
            metadata={'cross_page_math':{'source_pages':[1,4]}})
        self.assertEqual(_atomic_source_cuts(self.book),set())
        self.book.elements[0].asset_ids=['archive']
        self.assertEqual(_atomic_source_cuts(self.book),{1,2,3})

    def test_retained_native_label_report_uses_final_output_page(self):
        base=self.compiler
        def with_label(book,translations,outdir,output_path=None,**kw):
            result=base(book,translations,outdir,output_path,**kw)
            path=Path(result['layout_map_path'])
            mapping=json.loads(path.read_text(encoding='utf8'))
            mapping['native_retained_labels']=[{'element_id':'e'+str(n),'source_page':n,
                'rendered_page':i+1,'bbox':[10,10,20,20],'reason':'untranslated_preview'}
                for i,n in enumerate(book.selected_pages)]
            mapping['assets_retained_inside_native_labels']={}
            path.write_text(json.dumps(mapping),encoding='utf8')
            result['issues']=[{'code':'native_label_preview','severity':'warning','page':n}
                              for n in book.selected_pages]
            return result
        with patch('textbook_pdf.layout._Runtime.find'),patch('textbook_pdf.layout.compose',side_effect=with_label):
            result=compose_reading_draft(self.book,self.translations,self.root,chunk_pages=1)
        labels=result.get('native_retained_labels',[])
        self.assertEqual([(v['source_page'],v['rendered_page'])for v in labels],[(2,2),(3,3),(4,4)])
        self.assertEqual(result.get('native_retained_label_count'),3)
        self.assertEqual(len([i for i in result['issues']if i['code']=='native_label_preview']),3)

    def test_original_blank_page_is_not_reported_as_missing_translation(self):
        original=Path(self.book.source_path)
        blank=self.root/'with-blank.pdf'
        with fitz.open(original) as source,fitz.open() as target:
            target.insert_pdf(source,from_page=0,to_page=2)
            target.new_page(width=300,height=400)
            target.save(blank)
        self.book.source_path=str(blank)
        self.book.source_sha256=file_sha256(blank)
        self.book.elements=self.book.elements[:3]
        base=self.compiler
        def no_visible(book,*args,**kw):
            if book.selected_pages==[4]:raise ValueError('No source elements')
            return base(book,*args,**kw)
        with patch('textbook_pdf.layout._Runtime.find'),patch('textbook_pdf.layout.compose',side_effect=no_visible):
            result=compose_reading_draft(self.book,self.translations,self.root,chunk_pages=1)
        self.assertEqual(result['fallback_source_pages'],[1])
        self.assertEqual(result.get('blank_source_pages'),[4])
        with fitz.open(result['pdf_path']) as actual:
            self.assertNotIn('尚未译成中文',actual[-1].get_text())
        self.assertEqual(result['source_to_output']['4'],[4])


if __name__=='__main__':unittest.main()
