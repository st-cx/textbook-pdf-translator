"""Source-preserving regressions for rc7 whole-page fallback causes."""
from pathlib import Path
from dataclasses import asdict,replace
import json,shutil,unittest,uuid
from unittest.mock import patch
import pymupdf as fitz
from textbook_pdf import layout
from textbook_pdf.models import Book,Element,Asset,file_sha256,digest
from textbook_pdf.extraction import _collect

class LayoutConvergenceTests(unittest.TestCase):
 def setUp(self):
  self.out=Path(__file__).parent/'_tmp_layout_convergence'/uuid.uuid4().hex
  self.out.mkdir(parents=True);self.addCleanup(shutil.rmtree,self.out)
 def label_book(self,baseline=108,original_text='End',font_variant='helv'):
  original=self.out/'source.pdf'
  with fitz.open()as pdf:
   page=pdf.new_page(width=340,height=300)
   page.insert_font(fontname='AuthoredOriginal',fontbuffer=fitz.Font(font_variant).buffer)
   page.insert_text((80,baseline),original_text,fontname='AuthoredOriginal',fontsize=8,color=(0,174/255,239/255))
   page.draw_line((70,75),(220,75));pdf.save(original)
   _,glyphs=_collect(page)
  background=self.out/'background.pdf'
  with fitz.open()as pdf:
   page=pdf.new_page(width=180,height=50);page.draw_line((10,15),(160,15));pdf.save(background)
  a=Asset('background','figure',str(background),1,[60,60,240,110],180,50,sha256=file_sha256(background))
  parent=Element('diagram','figure','',1,[60,60,240,110],asset_ids=[a.id],metadata={'figure_label_ids':['label']})
  child=Element('label','figure_label',original_text,1,[80,101.5,120,109.5],style={'color':'00aeef'},metadata={'parent_figure_id':parent.id,'glyph_ids':[g['id']for g in glyphs],'source_label_bbox':[80,101.5,120,109.5],'source_text':original_text,'recognition':'detached-source-label','role':'translated_figure_label'})
  book=Book(str(original),file_sha256(original),'Self-authored edge label',1,[1],pages=[{'number':1,'width':340,'height':300}],elements=[parent,child],assets={a.id:a},profile={'job_dir':str(self.out),'page_width':340,'page_height':340})
  return book
 def small_top_label_book(self,**kwargs):
  b=self.label_book(**kwargs);bb=[60,103,240,153]
  b.assets['background']=replace(b.assets['background'],bbox=bb)
  b.elements[0]=replace(b.elements[0],bbox=bb)
  return b
 def marker_book(self,text):
  original=self.out/'marker-source.pdf'
  with fitz.open()as pdf:
   page=pdf.new_page(width=340,height=300);page.insert_text((55,80),text);pdf.save(original)
  marker_path=self.out/'marker.pdf'
  with fitz.open()as pdf:
   page=pdf.new_page(width=8,height=12)
   shape=page.new_shape();shape.draw_polyline([(0,0),(8,6),(0,12),(0,0)]);shape.finish(color=(0,174/255,239/255));shape.commit();pdf.save(marker_path)
  a=Asset('marker-asset','figure',str(marker_path),1,[40,70,48,82],8,12,sha256=file_sha256(marker_path))
  marker=Element('marker','figure','',1,[40,70,48,82],asset_ids=[a.id],metadata={'role':'exercise_marker','anchor_id':'anchor'})
  anchor=Element('anchor','exercise',text,1,[55,70,290,95],style={'columns':1})
  return Book(str(original),file_sha256(original),'Self-authored marker branch',1,[1],pages=[{'number':1,'width':340,'height':300}],elements=[marker,anchor],assets={a.id:a},profile={'job_dir':str(self.out),'page_width':340,'page_height':340})
 def formula_book(self,width=105):
  original=self.out/'formula-source.pdf'
  with fitz.open()as pdf:
   page=pdf.new_page(width=340,height=300)
   page.insert_text((60,80),'21. An expression');page.insert_text((60,110),'x + y = z',fontsize=9)
   pdf.save(original)
   box=fitz.Rect(60,100,60+width,114);crop=self.out/'formula.pdf'
   with fitz.open()as isolated:
    canvas=isolated.new_page(width=box.width,height=box.height);canvas.show_pdf_page(canvas.rect,pdf,0,clip=box);isolated.save(crop)
  a=Asset('wide','formula',str(crop),1,list(box),width,14,baseline=10,sha256=file_sha256(crop))
  e=Element('exercise','exercise','21. An expression ⟦F:wide⟧',1,[60,70,173,120],asset_ids=[a.id],style={'columns':2,'column':0})
  return Book(str(original),file_sha256(original),'Self-authored numbered formula',1,[1],pages=[{'number':1,'width':340,'height':300,'columns':2}],elements=[e],assets={a.id:a},profile={'job_dir':str(self.out),'page_width':340,'page_height':340,'margins':{'left':48,'right':48}})
 def punctuation_book(self):
  original=self.out/'punctuation-source.pdf'
  with fitz.open()as pdf:
   page=pdf.new_page(width=340,height=300)
   page.insert_font(fontname='AuthoredOriginal',fontbuffer=fitz.Font('helv').buffer)
   page.insert_text((80,100),'x = 1',fontname='AuthoredOriginal',fontsize=9)
   page.insert_text((120,100),'.',fontname='AuthoredOriginal',fontsize=9);pdf.save(original)
   _,glyphs=_collect(page);period=[g['id']for g in glyphs if g['c']=='.'];formula=[g['id']for g in glyphs if g['c']!='.']
   crop=self.out/'punctuation-formula.pdf';box=fitz.Rect(78,89,119,103)
   with fitz.open()as isolated:
    canvas=isolated.new_page(width=box.width,height=box.height);canvas.show_pdf_page(canvas.rect,pdf,0,clip=box);isolated.save(crop)
  a=Asset('proof','formula',str(crop),1,list(box),41,14,baseline=11,sha256=file_sha256(crop),metadata={'glyph_ids':formula})
  e=Element('formula','formula','⟦F:proof⟧',1,list(box),asset_ids=[a.id],metadata={'glyph_ids':formula})
  dot=Element('punctuation','footnote','.',1,[120,89,124,103],metadata={'glyph_ids':period})
  return Book(str(original),file_sha256(original),'Self-authored punctuation',1,[1],pages=[{'number':1,'width':340,'height':300}],elements=[e,dot],assets={a.id:a},profile={'job_dir':str(self.out),'page_width':340,'page_height':340})
 def test_unnumbered_exercise_does_not_swallow_attached_native_marker(self):
  b=self.marker_book('An explanatory note.');before=digest(asdict(b))
  result=layout.compose(b,{'anchor':'这是一段说明。'},self.out/'rendered',allow_incomplete=True)
  m=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'))
  self.assertEqual(len(m['elements'].get('marker',[])),1,result['issues'])
  self.assertEqual(len(m['assets'].get('marker-asset',[])),1,result['issues'])
  self.assertFalse(any(i['code']in{'missing_layout_trace','missing_asset_trace'}for i in result['issues']))
  self.assertEqual(before,digest(asdict(b)))
 def test_numbered_exercise_still_renders_marker_once(self):
  b=self.marker_book('7. Explain the idea.')
  result=layout.compose(b,{'anchor':'7. 解释这个想法。'},self.out/'rendered',allow_incomplete=True)
  m=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'))
  self.assertEqual(len(m['elements'].get('marker',[])),1,result['issues']);self.assertEqual(len(m['assets'].get('marker-asset',[])),1)
 def test_real_measured_label_fits_even_when_unused_synthetic_height_does_not(self):
  b=self.label_book();before=digest(asdict(b))
  result=layout.compose(b,{'label':'终'},self.out/'rendered',allow_incomplete=True)
  m=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'))
  self.assertEqual(m['native_retained_labels'],[])
  decision=m['figure_label_layout_decisions'][0];self.assertEqual(decision['decision'],'translated')
  self.assertEqual(decision['measurement']['font_size'],8.5)
  self.assertLessEqual(decision['measurement']['measured_height'],decision['measurement']['max_height'])
  self.assertAlmostEqual(decision['measurement']['max_height'],9.5)
  self.assertEqual(before,digest(asdict(b)))
 def test_actual_chinese_overflow_still_uses_verified_native_preview_only(self):
  b=self.label_book();values={'label':'这段文字在固定字号下放不进这个标签区域。'*8}
  result=layout.compose(b,values,self.out/'preview',allow_incomplete=True)
  m=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'))
  self.assertEqual(m['not_displayed_translated_label_ids'],['label']);self.assertEqual(m['native_retained_labels'][0]['reason'],'translated_label_overflow_preview')
  self.assertTrue(m['native_retained_labels'][0]['native_proof']['actual_native_capture']['verified'])
  with self.assertRaisesRegex(layout.CompositionError,'图内标签溢出'):
   layout.compose(b,values,self.out/'strict',allow_incomplete=False)
 def test_actual_source_label_outside_parent_stays_blocked(self):
  b=self.label_book(118);b.elements[1]=replace(b.elements[1],bbox=[80,109,120,117],metadata={**b.elements[1].metadata,'source_label_bbox':[80,109,120,117]})
  with self.assertRaisesRegex(layout.CompositionError,'固定字号框超出原图'):
   layout.compose(b,{'label':'终'},self.out/'blocked',allow_incomplete=True)
 def test_nominal_font_frame_outside_with_all_native_ink_inside_uses_proved_intersection(self):
  b=self.label_book();b.elements[1]=replace(b.elements[1],bbox=[80,101.5,120,112],metadata={**b.elements[1].metadata,'source_label_bbox':[80,101.5,120,112]})
  before=digest(asdict(b))
  result=layout.compose(b,{'label':'终'},self.out/'nominal',allow_incomplete=True)
  m=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'))
  decision=m['figure_label_layout_decisions'][0]
  proof=decision['measurement']['source_frame_proof']
  self.assertEqual(proof['source_label_bbox'],[80,101.5,120,112])
  self.assertEqual(proof['render_label_bbox'],[80,101.5,120,110])
  self.assertTrue(proof['all_native_ink_inside_background'])
  self.assertEqual(before,digest(asdict(b)))
 def test_number_indent_is_included_when_choosing_formula_flow_width(self):
  b=self.formula_book();before=digest(asdict(b))
  result=layout.compose(b,{'exercise':'21. 表达式 ⟦F:wide⟧'},self.out/'wide',allow_incomplete=True)
  self.assertFalse(any(i['code']=='asset_too_wide'for i in result['issues']),result['issues'])
  m=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'))
  self.assertAlmostEqual(m['assets']['wide'][0]['bbox'][2]-m['assets']['wide'][0]['bbox'][0],105,places=3)
  traces=json.loads((self.out/'wide/typst-traces.json').read_text(encoding='utf8'))
  self.assertTrue(all(t['columns']==1 for t in traces if t.get('type')=='element'and t.get('id')=='exercise'))
  self.assertEqual(before,digest(asdict(b)))
 def test_formula_wider_than_whole_body_stays_blocked_without_scaling(self):
  b=self.formula_book(260)
  result=layout.compose(b,{'exercise':'21. 表达式 ⟦F:wide⟧'},self.out/'too-wide',allow_incomplete=True)
  self.assertTrue(any(i['code']=='asset_too_wide'for i in result['issues']))
 def test_equivalent_chinese_stop_keeps_source_native_stop_and_explicit_trace(self):
  b=self.punctuation_book();before=digest(asdict(b))
  result=layout.compose(b,{'punctuation':'。'},self.out/'punctuation',allow_incomplete=True)
  m=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'));retained=m['native_spatial_punctuation'][0]
  self.assertEqual(retained['requested_literal'],'。');self.assertEqual(retained['source_literal'],'.')
  self.assertEqual(retained['punctuation_equivalence'],'finite-ascii-cjk-prose-pairs-v1')
  self.assertEqual(before,digest(asdict(b)))
 def test_different_punctuation_meaning_stays_blocked(self):
  b=self.punctuation_book()
  with self.assertRaisesRegex(layout.CompositionError,'裸标点缺少唯一'):
   layout.compose(b,{'punctuation':'!'},self.out/'wrong-punctuation',allow_incomplete=True)
 def test_reading_draft_retains_safe_structured_composition_cause_without_private_exception(self):
  from textbook_pdf.reading_draft import compose_reading_draft
  b=self.marker_book('A source note.');secret='F:\\private\\credential.txt?api_key=secret-value'
  error=layout.CompositionError(secret,code='label_source_bounds',element_id='anchor',asset_id='marker-asset')
  with patch('textbook_pdf.layout.compose',side_effect=error):
   result=compose_reading_draft(b,{'anchor':'中文说明。'},self.out/'draft')
  self.assertEqual(result['fallback_source_pages'],[1])
  event=result['layout_failure_events'][0]
  self.assertIn('label_source_bounds',event['failure_codes'])
  self.assertEqual(event['element_ids'],['anchor']);self.assertEqual(event['asset_ids'],['marker-asset'])
  self.assertNotIn(secret,json.dumps(result));self.assertNotIn('secret-value',json.dumps(result))
  ui=Path(result['html_report_path']).read_text(encoding='utf8')
  self.assertIn('图内标签的原始位置超出图形范围',ui)
  self.assertNotIn('label_source_bounds',ui);self.assertNotIn(secret,ui)
  self.assertNotIn('credential.txt',ui);self.assertNotIn('api_key=',ui)
 def test_structured_composition_fields_reject_unapproved_codes_and_path_ids(self):
  from textbook_pdf.reading_draft import compose_reading_draft
  b=self.marker_book('A source note.');secret='F:\\private\\secret-value'
  error=layout.CompositionError(secret,code=secret,element_id=secret,asset_id=secret)
  with patch('textbook_pdf.layout.compose',side_effect=error):
   result=compose_reading_draft(b,{'anchor':'中文说明。'},self.out/'draft')
  event=result['layout_failure_events'][0]
  self.assertEqual(event['failure_codes'],[]);self.assertEqual(event['element_ids'],[])
  self.assertNotIn('secret-value',json.dumps(result))
  ui=Path(result['html_report_path']).read_text(encoding='utf8')
  self.assertIn('其他排版问题',ui);self.assertNotIn('secret-value',ui)
 def test_failure_reason_labels_are_chinese_whitelist_and_unknown_is_generic(self):
  from textbook_pdf.reading_draft import _failure_reason_labels
  self.assertEqual(_failure_reason_labels(['asset_out_of_page','rendered_object_outside_page']),['内容超出页面边界'])
  for code,word in [('native_punctuation_identity','标点'),('oversized_source_unit','版面'),
                    ('asset_too_wide','栏宽'),('missing_asset_trace','位置记录'),
                    ('missing_layout_trace','位置记录'),('detached_caption','图注'),
                    ('detached_explanation','距离')]:
   text='、'.join(_failure_reason_labels([code]))
   self.assertIn(word,text);self.assertNotIn(code,text)
  self.assertEqual(_failure_reason_labels(['F:/private/credential.txt',None]),['其他排版问题'])
 def test_small_proved_top_only_complete_word_keeps_fitted_chinese_at_legacy_anchor(self):
  b=self.small_top_label_book();before=digest(asdict(b))
  result=layout.compose(b,{'label':'结束'},self.out/'top-word',allow_incomplete=True)
  m=json.loads(Path(result['layout_map_path']).read_text(encoding='utf8'))
  self.assertEqual(m['native_retained_labels'],[])
  decision=m['figure_label_layout_decisions'][0];proof=decision['measurement']['source_frame_proof']
  self.assertEqual(decision['decision'],'translated');self.assertEqual(decision['measurement']['font_size'],8.5)
  self.assertEqual(proof['method'],'proved-small-top-only-roman-word-chinese-anchor-v1')
  self.assertEqual(proof['source_label_bbox'],b.elements[1].bbox)
  self.assertTrue(proof['requires_chinese_fit']);self.assertFalse(proof['native_fallback_allowed'])
  self.assertEqual(proof['tolerances_pt'],{'nominal_top':2.0,'actual_ink_top':1.0})
  self.assertEqual(before,digest(asdict(b)))
 def test_top_word_permission_cannot_escape_to_native_from_shared_cache(self):
  b=self.small_top_label_book();cache={}
  layout._label_source_frame(b.elements[1],b.assets['background'],b,cache,translated_text='结束')
  with self.assertRaises(layout.CompositionError):
   layout._label_source_frame(b.elements[1],b.assets['background'],b,cache)
 def test_protected_math_and_unowned_or_unknown_top_label_stay_blocked(self):
  for mode in ['math','unknown','unclassified']:
   with self.subTest(mode=mode):
    b=self.small_top_label_book();e=b.elements[1]
    if mode=='math':e=replace(e,text=e.text+' ⟦F:background⟧',asset_ids=['background'])
    elif mode=='unknown':e=replace(e,metadata={**e.metadata,'glyph_ids':e.metadata['glyph_ids']+['unknown-glyph']})
    else:e=replace(e,metadata={k:v for k,v in e.metadata.items()if k!='recognition'})
    b.elements[1]=e
    with self.assertRaises(layout.CompositionError):
     layout.compose(b,{'label':'结束'},self.out/mode,allow_incomplete=True)
 def test_italic_math_operator_and_large_top_overflow_stay_blocked(self):
  for mode in ['italic','operator','large_nominal','large_ink']:
   with self.subTest(mode=mode):
    b=self.small_top_label_book(**({'font_variant':'heit'}if mode=='italic'else{'original_text':'log'}if mode=='operator'else{'baseline':106}if mode=='large_ink'else{}))
    if mode=='large_nominal':
     e=b.elements[1];box=[80,98,120,109.5];b.elements[1]=replace(e,bbox=box,metadata={**e.metadata,'source_label_bbox':box})
    with self.assertRaises(layout.CompositionError):
     layout.compose(b,{'label':'结束'},self.out/mode,allow_incomplete=True)
 def test_top_word_with_missing_or_unfitted_chinese_never_clips_native_ink(self):
  for name,values in [('missing',{}),('long',{'label':'中文在固定字号下放不进这个标签。'*80})]:
   with self.subTest(name=name):
    b=self.small_top_label_book()
    with self.assertRaises(layout.CompositionError):
     layout.compose(b,values,self.out/name,allow_incomplete=True)

if __name__=='__main__':unittest.main(verbosity=2)
