from dataclasses import asdict,replace
from copy import deepcopy
import sys,unittest
sys.dont_write_bytecode=True
from textbook_pdf.models import Element,Book,digest
from textbook_pdf.exercise_flow_adapter import PLAN_KEY
from textbook_pdf.layout import _partition_book
PROOF={k:True for k in('native_fields_match_current_ledger','unique_owner_and_complete_glyph_span','geometric_source_column_coherent_per_original_page','source_font_size_coherent','plain_text_structure_match','no_original_container_drawing','original_context_visually_approved')}
def elem(eid,kind,text,page,col=0,group=None):
 x=50 if col==0 else 330
 return Element(eid,kind,text,page,[x,65,x+220,77],style={'source_size':9.5,'columns':2},group_id=group or eid,chapter='chapter-1',metadata={'structure_class':'plain text'})
def unit(es,identity):
 return {'id':identity,'anchor_id':es[0].id,'anchor_role':'numbered_exercise_stem','source_proof':dict(PROOF),
  'members':[{'id':e.id,'original_record_sha256':digest(asdict(e)),'geometric_source_column':0 if e.bbox[0]<300 else 1}for e in es]}
def fixture(limit=80,optin=True):
 pages=[{'number':n,'width':612,'height':756,'columns':2,'column_split':306}for n in range(1,84)]
 es=[elem('before-'+str(n),'paragraph','Prelude.',n)for n in range(1,79)]
 q9=[elem('q9','exercise','9. State the result.',79,1),elem('q9a','sidebar','a) First.',80),elem('q9b','paragraph','b) Second.',80)]
 q25=[elem('q25','exercise','25. State the result.',80,1),elem('q25a','sidebar','a) Third.',81),elem('q25b','paragraph','b) Fourth.',81)]
 es+=q9+q25+[elem('after-'+str(n),'paragraph','Following.',n)for n in(82,83)]
 plan={'schema':'verified-original-exercise-flow-layout-plan-v2','units':[unit(q9,'Q9'),unit(q25,'Q25')]}
 profile={'layout_chunk_source_pages':limit}
 if optin:profile[PLAN_KEY]=plan
 return Book('synthetic.pdf','source','Synthetic',83,list(range(1,84)),pages=pages,elements=es,profile=profile),plan
def refresh(book,plan):
 lookup={e.id:e for e in book.elements}
 for u in plan['units']:
  for m in u['members']:
   if m['id']in lookup:m['original_record_sha256']=digest(asdict(lookup[m['id']]))
class PartitionGuards(unittest.TestCase):
 def test_default80_transitively_keeps_79_80_and80_81_chains(self):
  b,p=fixture();parts=_partition_book(b)
  self.assertEqual(parts[0].selected_pages,list(range(1,82)));self.assertEqual(parts[1].selected_pages,[82,83])
 def test_source_ids_and_all_source_records_unchanged(self):
  b,p=fixture();old=[asdict(e)for e in b.elements];parts=_partition_book(b)
  self.assertEqual([asdict(e)for part in parts for e in part.elements],old);self.assertEqual([asdict(e)for e in b.elements],old)
 def test_one_page_limit_expands_only_proven_overlap(self):
  b,p=fixture(1);parts=_partition_book(b)
  chainpart=next(part for part in parts if any(e.id=='q9'for e in part.elements))
  self.assertEqual(chainpart.selected_pages,[79,80,81]);self.assertEqual(len(parts),81)
 def test_no_plan_exact_default80_behavior(self):
  b,p=fixture(optin=False);parts=_partition_book(b)
  self.assertEqual(parts[0].selected_pages,list(range(1,81)));self.assertEqual(parts[1].selected_pages,[81,82,83])
 def test_no_plan_single_part_preserves_same_book_object(self):
  b,p=fixture(500,optin=False);self.assertIs(_partition_book(b)[0],b)
 def test_own_page_range_claims_do_not_control_partition(self):
  b,p=fixture();p['units'][0]['source_pages']=[1,500];p['units'][1]['page_span']=[1,1118]
  self.assertEqual(_partition_book(b)[0].selected_pages,list(range(1,82)))
 def test_wholly_unrelated_unit_skips_for_a_source_selection(self):
  b,p=fixture();es=[e for e in b.elements if e.page in(82,83)];b=replace(b,elements=es,pages=[x for x in b.pages if x['number']in(82,83)],selected_pages=[82,83])
  self.assertIs(_partition_book(b)[0],b)
 def test_partial_source_selection_cannot_silently_drop_parent(self):
  b,p=fixture();b=replace(b,elements=[e for e in b.elements if e.page>=80])
  with self.assertRaisesRegex(ValueError,'only some members'):_partition_book(b)
 def test_unknown_member_in_present_chain_rejected(self):
  b,p=fixture();p['units'][0]['members'].append({'id':'unknown','original_record_sha256':'x','geometric_source_column':0})
  with self.assertRaisesRegex(ValueError,'only some members'):_partition_book(b)
 def test_stale_member_record_rejected(self):
  b,p=fixture();b.elements[78].text+=' Changed.'
  with self.assertRaisesRegex(ValueError,'stale'):_partition_book(b)
 def test_missing_anchor_rejected(self):
  b,p=fixture();p['units'][0]['anchor_id']='invented'
  with self.assertRaisesRegex(ValueError,'real source anchor'):_partition_book(b)
 def test_noncontiguous_members_cannot_skip_a_true_figure(self):
  b,p=fixture();b.elements.insert(80,elem('figure','figure','',80))
  with self.assertRaisesRegex(ValueError,'contiguous'):_partition_book(b)
 def test_false_native_proof_rejected_before_atomic_span(self):
  b,p=fixture();p['units'][0]['source_proof']['native_fields_match_current_ledger']=False
  with self.assertRaisesRegex(ValueError,'source proof'):_partition_book(b)
 def test_existing_graphic_and_crosspage_spans_share_transitive_closure(self):
  b,p=fixture();qend=next(i for i,e in enumerate(b.elements)if e.id=='q25b')
  fig=elem('figure81','figure','',81,group='graphic81-82');cap=elem('caption82','caption','Caption.',82,group='graphic81-82')
  math82=elem('math82','formula','',82,group='math82-83');math83=elem('math83','formula','',83,group='math82-83')
  math82.metadata['cross_page_math']={'id':'math82-83'};math83.metadata['cross_page_math']={'id':'math82-83'}
  b.elements=b.elements[:qend+1]+[fig,cap,math82,math83]
  parts=_partition_book(b);self.assertEqual(len(parts),1);self.assertEqual(parts[0].selected_pages,list(range(1,84)))
if __name__=='__main__':unittest.main()
