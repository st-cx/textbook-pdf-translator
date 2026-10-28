from pathlib import Path
import sys,os,json,shutil,uuid
from dataclasses import asdict,replace
import unittest
from unittest.mock import patch
import pymupdf as fitz
HERE=Path(__file__).resolve().parent
PACKAGE=HERE/'textbook_pdf' if (HERE/'textbook_pdf').is_dir() else HERE.parent/'textbook_pdf'
sys.path.insert(0,str(PACKAGE.parent))
for parent in [PACKAGE.parent,*HERE.parents]:
 binary=parent/'tools/typst.exe'
 if binary.is_file():
  os.environ.setdefault('TEXTBOOK_TYPST',str(binary));break
else:
 for parent in HERE.parents:
  binary=parent/'public-repo/tools/typst.exe'
  if binary.is_file():os.environ.setdefault('TEXTBOOK_TYPST',str(binary));break
from textbook_pdf import layout,reading_draft
from textbook_pdf.models import Book,Element,Asset,file_sha256,digest,TOKEN_RE
from textbook_pdf.extraction import _asset,_collect,_detach_figure_labels,_finalize_assets

class LocalLabelFallbackTests(unittest.TestCase):
 def setUp(self):
  self.out=HERE/'_testdata'/uuid.uuid4().hex;self.out.mkdir(parents=True);self.addCleanup(shutil.rmtree,self.out)
 def fixture(self):
  directory=self.out;(directory/'assets').mkdir()
  source=directory/'original.pdf'
  with fitz.open()as doc:
   page=doc.new_page(width=340,height=280)
   page.insert_font(fontname='OriginalRoman',fontbuffer=fitz.Font('helv').buffer)
   page.insert_font(fontname='OriginalItalic',fontbuffer=fitz.Font('heit').buffer)
   page.insert_text((60,80),'Rate ',fontname='OriginalRoman',fontsize=9,color=(0,174/255,239/255))
   page.insert_text((80,80),'x',fontname='OriginalItalic',fontsize=9,color=(0,174/255,239/255))
   page.insert_text((190,80),'End',fontname='OriginalRoman',fontsize=9)
   page.draw_line((100,78),(180,78))
   doc.save(source)
   lines,glyphs=_collect(page)
   book=Book(str(source),file_sha256(source),'Self-authored local fallback',1,[1],pages=[{'number':1,'width':340,'height':280}],profile={'job_dir':str(directory),'page_width':340,'page_height':340})
   book._active_glyphs=glyphs
   a=_asset(book,doc,page,directory,'figure',[50,60,250,110])
   e=Element('diagram','figure','',1,[50,60,250,110],asset_ids=[a.id],metadata={'glyph_ids':[g['id']for g in glyphs]})
   for g in glyphs:g.update(owner=e.id,disposition='preserved-original')
   elements=[e];_detach_figure_labels(book,doc,page,directory,elements,lines,glyphs)
   book.elements=elements+[Element('body','paragraph','An original explanation.',1,[50,120,280,150])]
   _finalize_assets(book,1,directory)
  labels=[e for e in book.elements if e.kind=='figure_label'];self.assertEqual(len(labels),2)
  overflow=next(e for e in labels if 'Rate'in e.text);fit=next(e for e in labels if 'End'in e.text)
  formulas=''.join('⟦F:'+a+'⟧'for a in TOKEN_RE.findall(overflow.text))
  values={overflow.id:'这个标签需要完整地保留在原图的安全区域内，不能通过缩小字体来强塞。'*5+formulas,fit.id:'终','body':'这段中文仍须显示。'}
  return book,values,overflow,fit
 def test_actual_measurement_localizes_recovery_and_leaves_fit_chinese(self):
  b,t,overflow,fit=self.fixture();before=digest(asdict(b));text_before=digest(t)
  result=layout.compose(b,t,self.out/'composed',allow_incomplete=True)
  m=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'))
  self.assertEqual(m['not_displayed_translated_label_ids'],[overflow.id])
  self.assertEqual(result['untranslated_count'],0)
  record=m['native_retained_labels'][0];self.assertEqual(record['translation_hash'],digest(t[overflow.id]))
  self.assertFalse(record['translation_displayed']);self.assertTrue(record['native_proof']['actual_native_capture']['verified'])
  self.assertFalse(record['native_proof']['actual_native_capture']['legacy_identity_layer']['unselected_target_glyph_count'])
  measured=record['overflow_measurement'];self.assertGreater(measured['measured_height'],measured['max_height']);self.assertEqual(measured['font_size'],8.5)
  decisions={r['element_id']:r['decision']for r in m['figure_label_layout_decisions']}
  self.assertEqual(decisions,{overflow.id:'retained_native_overflow',fit.id:'translated'})
  traces=json.loads((self.out/'composed/typst-traces.json').read_text(encoding='utf8'))
  self.assertFalse(any(x.get('type')=='figure-label-overflow-probe'for x in traces))
  with fitz.open(result['pdf_path'])as pdf:
   text=''.join(p.get_text()for p in pdf)
   self.assertIn('Rate',text);self.assertIn('终',text);self.assertIn('这段中文仍须显示',text)
   self.assertNotIn('这个标签需要',text);self.assertNotIn('End',text)
   spans=[s for p in pdf for block in p.get_text('dict')['blocks']for line in block.get('lines',[])for s in line['spans']if 'Rate'in s['text']]
   self.assertEqual(len(spans),1);self.assertEqual(spans[0]['color'],0x00aeef);self.assertAlmostEqual(spans[0]['size'],9,places=3)
  for aid in record['covered_asset_ids']:
   self.assertEqual(m['assets'].get(aid,[]),[])
   self.assertEqual(m['assets_retained_inside_native_labels'][aid][0]['element_id'],overflow.id)
  self.assertEqual(before,digest(asdict(b)));self.assertEqual(text_before,digest(t))
 def test_formal_overflow_keeps_strict_refusal(self):
  b,t,_,_=self.fixture()
  # Isolate the strict layout gate from unrelated extraction fixture diagnostics.
  b=replace(b,issues=[])
  with self.assertRaisesRegex(layout.CompositionError,'图内标签溢出'):
   layout.compose(b,t,self.out/'formal',allow_incomplete=False)
  self.assertFalse((self.out/'formal'/(b.title+'.zh.pdf')).exists())
 def test_reading_draft_counts_saved_but_undisplayed_label_and_reuses_parts(self):
  b,t,overflow,fit=self.fixture()
  report=reading_draft.compose_reading_draft(b,t,self.out/'draft',chunk_pages=1)
  self.assertEqual(report['translated_elements'],3);self.assertEqual(report['displayed_translated_elements'],2)
  self.assertEqual(report['untranslated_elements'],0);self.assertEqual(report['fallback_source_pages'],[])
  self.assertEqual(report['not_displayed_translated_label_ids'],[overflow.id])
  html=Path(report['html_report_path']).read_text(encoding='utf8')
  self.assertIn('2 / 3',html);self.assertIn('已存图内译文未显示',html);self.assertNotIn('当前已存译文均已显示',html)
  second=reading_draft.compose_reading_draft(b,t,self.out/'draft',chunk_pages=1)
  self.assertEqual(second['displayed_translated_elements'],2);self.assertEqual(second['parts_reused'],1)
 def test_native_shared_asset_elsewhere_still_requires_its_own_trace(self):
  a=Asset('formula','formula','formula.pdf',1,[60,70,70,80],10,10)
  label=Element('label','figure_label','Text ⟦F:formula⟧',1,[50,60,120,90],asset_ids=['formula'])
  body=Element('body','paragraph','Again ⟦F:formula⟧',1,[50,100,300,120],asset_ids=['formula'])
  b=Book('self-authored.pdf','source','Shared asset',1,[1],elements=[label,body],assets={'formula':a})
  retained={'element_id':'label','parent_id':'figure','source_page':1,'reason':'translated_label_overflow_preview','covered_asset_ids':['formula'],'covered_asset_occurrences':{'formula':1}}
  traces=[{'type':'native-retained-label','retained':retained,'page':1,'x':50,'y':60,'width':70,'height':30}]
  for eid,y in [('label',60),('body',100)]:
   for phase,dy in [('start',0),('end',10)]:traces.append({'type':'element','id':eid,'phase':phase,'page':1,'x':50,'y':y+dy,'width':250})
  issues=[];layout._layout_map(b,traces,layout._page_geometry(b),self.out/'never.pdf',issues)
  self.assertTrue(any(i['code']=='missing_asset_trace'for i in issues))
  traces.append({'type':'asset','id':'formula','element_id':'body','page':1,'x':80,'y':100,'width':10,'height':10})
  issues=[];m=layout._layout_map(b,traces,layout._page_geometry(b),self.out/'never.pdf',issues)
  self.assertFalse(any(i['code']=='missing_asset_trace'for i in issues));self.assertEqual(len(m['assets']['formula']),1)
 def test_unique_path_protection_is_identical_with_duplicate_bundles_and_alias(self):
  b,t,_,_=self.fixture();a=next(iter(b.assets.values()))
  b.assets['same-bundle']=replace(a,id='same-bundle')
  b.assets['alias']=replace(a,id='alias',path=str(Path(a.path).parent/'.'/Path(a.path).name))
  roots=layout._source_roots(b,self.out/'destination')
  old={str((root/a.path).resolve())for root in roots for a in b.assets.values()}
  paths={a.path for a in b.assets.values()};new={str((root/path).resolve())for root in roots for path in paths}
  self.assertEqual(old,new)
  b.assets['not-created']=replace(a,id='not-created',path='assets/not-created.pdf')
  protected=self.out/'assets/not-created.pdf'
  with self.assertRaisesRegex(ValueError,'保护对象'):
   reading_draft.compose_reading_draft(b,t,self.out/'draft-protect',output_path=protected)
 def test_unverifiable_native_source_remains_error(self):
  b,t,_,_=self.fixture()
  with patch('textbook_pdf.native_ink_geometry.measure_selected_native_ink',return_value={'verified':[],'unverified':[{'reason':'test_unproven_native_identity'}]}):
   with self.assertRaisesRegex(layout.CompositionError,'完整原生墨迹范围无法验证'):
    layout.compose(b,t,self.out/'unverified',allow_incomplete=True)

if __name__=='__main__':unittest.main(verbosity=2)
