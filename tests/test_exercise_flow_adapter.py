from pathlib import Path
from dataclasses import asdict
from copy import deepcopy
import json,sys,unittest,uuid,shutil
from unittest.mock import patch
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent
from textbook_pdf.models import Element,Book,digest,file_sha256
from textbook_pdf.exercise_flow_adapter import annotate,check_reading_order,PLAN_KEY
from textbook_pdf.layout import compose,CompositionError,rendering_binding
import pymupdf as fitz
PROOF={k:True for k in ('native_fields_match_current_ledger','unique_owner_and_complete_glyph_span',
 'geometric_source_column_coherent_per_original_page','source_font_size_coherent','plain_text_structure_match',
 'no_original_container_drawing','original_context_visually_approved')}
PG={n:{'number':n,'width':612,'height':756,'columns':2,'column_split':306}for n in (1,2)}
GEOM={'left':48,'right':48,'width':612}
def fixture(inline=False):
 style={'source_size':9.5,'columns':2};meta={'structure_class':'plain text'}
 es=[Element('q','exercise','25. State the result.',1,[320,680,560,710],style=dict(style),group_id='q'),
     Element('a','sidebar','a) A proposition.',2,[67,67,160,77],style=dict(style),metadata=dict(meta)),
     Element('b','paragraph','b) Another proposition.',2,[67,95,185,105],style=dict(style),metadata=dict(meta))]
 if inline:es.insert(2,Element('tail','paragraph','continues.',2,[80,79,130,89],style=dict(style),metadata=dict(meta)))
 unit={'id':'u','anchor_id':'q','anchor_role':'numbered_exercise_stem','source_proof':dict(PROOF),
   'members':[{'id':e.id,'original_record_sha256':digest(asdict(e)),'geometric_source_column':1 if e.id=='q'else 0}for e in es]}
 if inline:
  unit['members'][1].update(inline_group='g',inline_join='space');unit['members'][2].update(inline_group='g',inline_join='space')
  unit['inline_joins']=[{'left_id':'a','right_id':'tail','mode':'space','inline_group':'g','lossless_original_boundary_verified':True,
   'left_record_sha256':digest(asdict(es[1])),'right_record_sha256':digest(asdict(es[2])),'evidence_kind':'unhyphenated_source_line_boundary'}]
 return es,{'schema':'verified-original-exercise-flow-layout-plan-v2','units':[unit]}
def rehash(es,p):
 d={e.id:e for e in es}
 for m in p['units'][0]['members']:m['original_record_sha256']=digest(asdict(d[m['id']]))
def trace(eid,x,y,phase='start',columns=2):
 return {'id':eid,'type':'element','phase':phase,'page':1,'x':x,'y':y,'columns':columns,'width':249,'height':0,'source_page':1,'source_label':'original1'}
class Guards(unittest.TestCase):
 def test_wholly_unrelated_part_skips_unit_deterministically(self):
  _,p=fixture();es=[Element('unrelated','paragraph','Unrelated',1,[48,45,250,60])]
  self.assertEqual(annotate(es,p,PG),es)
  self.assertEqual(check_reading_order(p,[],GEOM,visible_ids={'unrelated'}),[])
 def test_partial_source_part_cannot_silently_drop_anchor(self):
  es,p=fixture()
  with self.assertRaisesRegex(ValueError,'only some members'):annotate(es[1:],p,PG)
  self.assertEqual(check_reading_order(p,[],GEOM,visible_ids={'a','b'})[0]['code'],'exercise_chain_partial_source_scope')
 def test_optin_binding_tracks_actual_adapter_bytes(self):
  es,p=fixture();b=Book('unused.pdf','source','Synthetic',2,[1,2],pages=list(PG.values()),elements=es,profile={PLAN_KEY:p})
  binding=rendering_binding(b,{})
  self.assertEqual(binding['renderer_identity']['exercise_flow_adapter_sha256'],file_sha256(__import__('textbook_pdf.exercise_flow_adapter',fromlist=['__file__']).__file__))
  b.profile={};self.assertNotIn('exercise_flow_adapter_sha256',rendering_binding(b,{})['renderer_identity'])
 def test_no_optin_is_object_identical(self):
  es,_=fixture();self.assertIs(annotate(es,None),es)
 def test_valid_proof_only_clones_layout(self):
  es,p=fixture();old=deepcopy([asdict(e)for e in es]);after=annotate(es,p,PG)
  self.assertEqual([asdict(e)for e in es],old);self.assertEqual(after[1].kind,'exercise')
 def test_anchor_must_be_present_in_members(self):
  es,p=fixture();p['units'][0]['anchor_id']='external'
  with self.assertRaisesRegex(ValueError,'anchor'):annotate(es,p,PG)
 def test_anchor_role_cannot_be_implicit(self):
  es,p=fixture();p['units'][0].pop('anchor_role')
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_numeric_anchor_cannot_be_arbitrary_paragraph(self):
  es,p=fixture();es[0].kind='paragraph';rehash(es,p)
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_valid_letter_prefix_has_real_member_anchor(self):
  es,p=fixture();es=es[1:];u=p['units'][0];u['members']=u['members'][1:];u.update(anchor_id='a',anchor_role='lettered_continuation_prefix',original_prefix_parent_outside_selection=True)
  self.assertEqual(annotate(es,p,PG)[0].id,'a')
 def test_letter_prefix_cannot_skip_prior_visible_body(self):
  es,p=fixture();u=p['units'][0];u['members']=u['members'][1:];u.update(anchor_id='a',anchor_role='lettered_continuation_prefix',original_prefix_parent_outside_selection=True)
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_member_order_must_be_contiguous_not_only_increasing(self):
  es,p=fixture();es.insert(1,Element('figure','figure','Diagram',1,[320,720,560,740]))
  with self.assertRaisesRegex(ValueError,'contiguous'):annotate(es,p,PG)
 def test_included_figure_is_rejected(self):
  es,p=fixture();es[1].kind='figure';rehash(es,p)
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_wide_banner_rejected_even_when_proof_claims_plain_text(self):
  es,p=fixture();es[1].metadata['banner_background_asset_ids']=['bg'];rehash(es,p)
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_true_sidebar_class_rejected(self):
  es,p=fixture();es[1].metadata['structure_class']='margin note';rehash(es,p)
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_real_container_evidence_rejected(self):
  es,p=fixture();p['units'][0]['source_proof']['no_original_container_drawing']=False
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_wrong_column_and_full_width_rejected(self):
  for box in ([320,67,400,77],[40,67,560,77]):
   es,p=fixture();es[1].bbox=box;rehash(es,p)
   with self.subTest(box=box),self.assertRaises(ValueError):annotate(es,p,PG)
 def test_font_size_change_rejected(self):
  es,p=fixture();es[1].style['source_size']=7;rehash(es,p)
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_stale_source_rejected(self):
  es,p=fixture();es[1].text+=' change'
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_duplicate_member_conflict_rejected(self):
  es,p=fixture();p['units'].append(deepcopy(p['units'][0]))
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_missing_glyph_proof_rejected(self):
  es,p=fixture();p['units'][0]['source_proof']['unique_owner_and_complete_glyph_span']=False
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_options_must_be_in_sequence(self):
  es,p=fixture();es[2].text='d) Wrong sequence.';rehash(es,p)
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_valid_space_join_requires_bound_adjacent_source_records(self):
  es,p=fixture(True);self.assertEqual(annotate(es,p,PG)[2].metadata['inline_join'],'space')
 def test_arbitrary_or_unproven_inline_modes_rejected(self):
  for mode,proof in [('delete',True),('word',True),('space',False)]:
   es,p=fixture(True);j=p['units'][0]['inline_joins'][0];j.update(mode=mode,lossless_original_boundary_verified=proof)
   with self.subTest(mode=mode,proof=proof),self.assertRaises(ValueError):annotate(es,p,PG)
 def test_unresolved_source_hyphen_cannot_be_hidden_by_space(self):
  es,p=fixture(True);es[1].text+='-';rehash(es,p);j=p['units'][0]['inline_joins'][0];j['left_record_sha256']=digest(asdict(es[1]))
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_join_cannot_skip_unrelated_text(self):
  es,p=fixture(True);j=p['units'][0]['inline_joins'][0];j['right_id']='b'
  with self.assertRaises(ValueError):annotate(es,p,PG)
 def test_external_engine_reading_reversal_blocks_formal_pdf_replacement(self):
  es,p=fixture();tmp=HERE/'_tmp_exercise_flow'/uuid.uuid4().hex;tmp.mkdir(parents=True)
  self.addCleanup(shutil.rmtree,tmp)
  source=tmp/'source.pdf';prior=tmp/'formal.pdf'
  with fitz.open()as doc:doc.new_page(width=612,height=756);doc.new_page(width=612,height=756);doc.save(source)
  with fitz.open()as doc:doc.new_page(width=612,height=756);doc.save(prior)
  before=file_sha256(prior)
  book=Book(str(source),file_sha256(source),'Synthetic',2,[1,2],pages=list(PG.values()),elements=es,profile={'job_dir':str(tmp),PLAN_KEY:p})
  traces=[trace('q',315,45),trace('q',315,65,'end'),trace('a',48,100),trace('a',48,115,'end'),trace('b',48,130),trace('b',48,145,'end')]
  class Runtime:
   binary=None
   def compile(self,src,dst,root):
    with fitz.open()as pdf:pdf.new_page(width=612,height=756);pdf.save(dst)
    return ''
   def query(self,src,root):return deepcopy(traces)
  with patch('textbook_pdf.layout._Runtime.find',return_value=Runtime()):
   with self.assertRaises(CompositionError):compose(book,{'q':'State the result.','a':'First result.','b':'Second result.'},tmp,output_path=prior)
  self.assertEqual(file_sha256(prior),before)
  failed=json.loads((tmp/'failed-layout-map.json').read_text('utf8'))
  self.assertTrue(failed['failed_formal_composition']);self.assertFalse(failed['exercise_flow_check']['passed'])
  self.assertIn('exercise_chain_visual_reading_order_reversed',{i['code']for i in failed['issues']})
if __name__=='__main__':unittest.main(verbosity=2)
