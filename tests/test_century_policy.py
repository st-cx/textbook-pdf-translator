"""Self-authored explicit-century, task identity and synthetic Store regressions."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import json,shutil,tempfile,unittest,uuid
from unittest.mock import patch
from textbook_pdf.models import Book,Element,Asset,TOKEN_RE,digest,validate_translation,source_contract_issues
from textbook_pdf.numbers import numeric_fingerprint
from textbook_pdf.century_policy import (FIELD,make_century_policy,century_policy_fields,
 frozen_century_policy_matches,check_century_numbers,event)
from textbook_pdf.store import Store,translation_rule_hash
from textbook_pdf.translation import (make_tasks,_current_task,_request_payload,_review_hash,
 _review_binding,review_record_matches,review_selected,review_status,_run_review_sequential,Provider,run_translation)

SOURCE='In the twenty-first century, 24 valves were tested.'
GOOD='二十一世纪，测试了24个阀门。'
def fixture(policy=True,text=SOURCE):
 e=Element('century-case','paragraph',text,1,[10,20,300,50],style={'source_size':10.5},group_id='century',chapter='c1')
 if policy:e.metadata[FIELD]=make_century_policy(e)
 return Book('', 'synthetic-century-source','Self-authored century',1,[1],elements=[e]),e

class CenturyLexicalTests(unittest.TestCase):
 def test_original_legacy_bug_remains_and_explicit_policy_fixes_value(self):
  self.assertNotEqual(numeric_fingerprint('twenty-first century'),numeric_fingerprint('二十一世纪'))
  b,e=fixture(False);self.assertIn('number_mismatch',[x['code']for x in validate_translation(e,GOOD,b)])
  e.metadata[FIELD]=make_century_policy(e);self.assertEqual(validate_translation(e,GOOD,b),[])
 def test_all_supported_single_centuries(self):
  chinese='一 二 三 四 五 六 七 八 九 十 十一 十二 十三 十四 十五 十六 十七 十八 十九 二十 二十一'.split()
  from textbook_pdf.century_policy import ORDINALS
  for n,(word,cn)in enumerate(zip(ORDINALS,chinese),1):
   with self.subTest(n=n):
    b,e=fixture(text='In the '+word+' century, 24 valves were tested.')
    for phrase in(cn+'世纪','第'+cn+'世纪',str(n)+'世纪','第'+str(n)+'世纪'):
     self.assertEqual(validate_translation(e,phrase+'，测试24个阀门。',b),[])
 def test_correct_arabic_ordinal_suffixes(self):
  for raw in('1st','2nd','3rd','4th','11th','12th','13th','20th','21st'):
   with self.subTest(raw=raw):self.assertEqual(event(raw+' century.')[1]['value'],int(raw[:-2]))
 def test_sentence_and_editorial_parentheses_are_legal(self):
  b,e=fixture()
  for text in('二十一世纪，测试24个阀门。','（二十一世纪），测试24个阀门。','在“二十一世纪”，测试24个阀门。','In the twenty-first century,测试24个阀门。'):
   with self.subTest(text=text):self.assertEqual(validate_translation(e,text,b),[])
 def test_changed_lost_or_repeated_century(self):
  b,e=fixture()
  for text in('第一世纪，测试24个阀门。','二十世纪，测试24个阀门。','测试24个阀门。','二十一世纪二十一世纪，24个阀门。','二十一世纪，25个阀门。'):
   with self.subTest(text=text):self.assertTrue(validate_translation(e,text,b))
 def test_unsupported_eras_ranges_or_multi_centuries(self):
  for text in('twenty-second century','twenty-first centuries','21th century','nineteenth and twentieth century','twenty-first century B.C.','first century and second century','twenty first century','twenty-first century. In another century.','21st-century and 22nd-century'):
   with self.subTest(text=text),self.assertRaises(ValueError):make_century_policy(Element('x','paragraph',text,1))
 def test_original_nine_independent_boundary_findings_close(self):
  for target in('二十一世纪²。','二十一世纪₂。','−（二十一世纪）。'):
   b,e=fixture(text='In the twenty-first century.');self.assertTrue(validate_translation(e,target,b))
  for target in('twenty–first century。','thirty—first century。'):
   b,e=fixture(text='In the first century.');self.assertTrue(validate_translation(e,target,b))
  for text in('In the twenty–first century.','In the −21st century.','A date of 1/21st century.','In the 21.5th century.'):
   with self.subTest(text=text),self.assertRaises(ValueError):make_century_policy(Element('x','paragraph',text,1))
 def test_bounded_arithmetic_and_scripts_do_not_parse_inner_substrings(self):
  b,e=fixture(text='In the twenty-first century.')
  for target in('二十一世纪½。','二十一世纪x。','二十一世纪.5。','二十一世纪（2）。','- ((二十一世纪))。','+二十一世纪。','1/二十一世纪。','x:（二十一世纪）。','二十一世纪×2。','（二十一世纪）²。'):
   with self.subTest(target=target):self.assertTrue(validate_translation(e,target,b))
 def test_normal_narrative_colon_does_not_become_ratio(self):
  b,e=fixture(text='In the twenty-first century.');self.assertEqual(validate_translation(e,'时代：（二十一世纪）。',b),[])
 def test_unicode_operators_dashes_and_rate_symbols_never_form_a_century(self):
  b,e=fixture()
  for symbol in('＋','﹣','±','−','–','—','⁺','∓','×','∕','／','＼','％','%','‰','‱','٪'):
   for phrase in(symbol+'21世纪','21世纪'+symbol,symbol+'〚（21世纪）〛','〚（21世纪）〛'+symbol):
    with self.subTest(phrase=phrase):self.assertTrue(validate_translation(e,phrase+'，24个阀门。',b))
 def test_unicode_symbol_boundary_is_enforced_for_source_compilation(self):
  for symbol in('＋','﹣','±','−','–','—','∓','∕','／','％','‰','‱','٪'):
   for phrase in(symbol+'21st century','21st century'+symbol,symbol+'〚21st century〛','〚21st century〛'+symbol):
    with self.subTest(phrase=phrase),self.assertRaises(ValueError):make_century_policy(Element('x','paragraph',phrase+', 24 valves.',1))
 def test_unicode_boundary_failures_are_rejected_by_the_full_store_path(self):
  # A parser-only assertion cannot establish Store acceptance behaviour.
  root=Path(tempfile.gettempdir())/('synthetic-century-boundary-'+uuid.uuid4().hex);root.mkdir()
  try:
   with Store(root/'tasks.db')as s:
    b,e=fixture();s.register(b,{})
    for bad in('＋21世纪，24个阀门。','﹣21世纪，24个阀门。','±21世纪，24个阀门。','21世纪%，24个阀门。','21世纪‰，24个阀门。','−〚（21世纪）〛，24个阀门。'):
     with self.subTest(bad=bad):self.assertFalse(s.put_candidate(e.id,bad,'synthetic')['accepted'])
    self.assertEqual(s.translations(),{})
  finally:shutil.rmtree(root)
 def test_same_value_no_float_rounding_or_broad_legacy_rewrite(self):
  b,e=fixture(text='In the twenty-first century, 1.25 valves and 2017 records were tested.')
  self.assertEqual(validate_translation(e,'二十一世纪，测试了1.25个阀门与2017条记录。',b),[])
  self.assertTrue(validate_translation(e,'二十一世纪，测试了1.2501个阀门与2017条记录。',b))
 def test_mutated_policy_closed(self):
  for key,value in(('implementation_sha256','0'*64),('schema','other'),('source_hash','stale'),('independent_semantic_review_required',False)):
   b,e=fixture();e.metadata[FIELD][key]=value
   with self.subTest(key=key):self.assertTrue(source_contract_issues(b));self.assertTrue(validate_translation(e,GOOD,b))
 def test_complete_source_identity_bound_even_when_source_hash_same(self):
  for key,value in(('id','other'),('page',2),('group_id','other'),('chapter','other'),('kind','caption'),('style',{'source_size':9.5}),('bbox',[1,2,3,4])):
   b,e=fixture();setattr(e,key,value)
   with self.subTest(key=key),self.assertRaises(ValueError):century_policy_fields(e)
 def test_unknown_semantic_metadata_bound(self):
  b,e=fixture();e.metadata['semantic_unit_ids']=['new-unit']
  with self.assertRaises(ValueError):century_policy_fields(e)
 def test_display_and_book_font_changes_are_layout_only(self):
  b,e=fixture();before=(century_policy_fields(e),translation_rule_hash(b,e,{}),_review_hash(e,GOOD,b,{}),make_tasks(b,{}))
  e.metadata['display_asset_ids']=['layout-only'];b.profile.update(body_size=12,body_font='Another')
  self.assertEqual(before,(century_policy_fields(e),translation_rule_hash(b,e,{}),_review_hash(e,GOOD,b,{}),make_tasks(b,{})))
 def test_default_never_reads_implementation_digest(self):
  b,e=fixture(False)
  with patch('textbook_pdf.century_policy.implementation_sha256',side_effect=AssertionError('not required')):
   self.assertEqual(century_policy_fields(e),{});self.assertTrue(frozen_century_policy_matches({'id':e.id},e))
 def test_policy_fields_are_deep_copied(self):
  b,e=fixture();before=asdict(e);x=century_policy_fields(e);x[FIELD]['source_event']['value']=1;self.assertEqual(asdict(e),before)
 def test_combined_quantity_or_percentage_policies_rejected(self):
  for key in('numeric_policy','percentage_unit_policy'):
   b,e=fixture(False);e.metadata[key]={}
   with self.subTest(key=key),self.assertRaises(ValueError):make_century_policy(e)

class CenturyTaskTests(unittest.TestCase):
 def test_optin_changes_rule_task_and_review_but_not_source_hash(self):
  b,e=fixture(False);old=(e.source_hash,translation_rule_hash(b,e,{}),_review_hash(e,GOOD,b,{}),make_tasks(b,{}))
  e.metadata[FIELD]=make_century_policy(e)
  self.assertEqual(e.source_hash,old[0]);self.assertNotEqual(translation_rule_hash(b,e,{}),old[1]);self.assertNotEqual(_review_hash(e,GOOD,b,{}),old[2]);self.assertNotEqual(make_tasks(b,{}),old[3]);self.assertFalse(_current_task(old[3][0],b,{}))
 def test_explicit_policy_mandates_semantic_review(self):
  b,e=fixture();self.assertTrue(review_selected(e))
 def test_policy_in_frozen_task_and_actual_request(self):
  b,e=fixture();t=make_tasks(b,{})[0];self.assertEqual(t['payload']['elements'][0][FIELD],e.metadata[FIELD]);self.assertEqual(_request_payload(t,[e.id],{})['payload']['elements'][0][FIELD],e.metadata[FIELD])
 def test_removed_body_policy_is_stale_even_if_task_rehashed(self):
  b,e=fixture();t=make_tasks(b,{})[0];t['payload']['elements'][0].pop(FIELD);t['source_hash']=digest(t['payload']);self.assertFalse(_current_task(t,b,{}))
 def test_frozen_readonly_context_binds_policy(self):
  b,e=fixture();b.elements.append(Element('next','paragraph','Continue.',1,group_id='next',chapter='c1'))
  t=next(x for x in make_tasks(b,{},1)if x['element_ids']==['next']);self.assertIn(FIELD,t['payload']['context_before'][0]);t['payload']['context_before'][0].pop(FIELD);t['source_hash']=digest(t['payload']);self.assertFalse(_current_task(t,b,{}))
 def test_context_policy_addition_makes_pre_policy_task_stale(self):
  b,e=fixture(False);b.elements.append(Element('next','paragraph','Continue.',1,group_id='next',chapter='c1'));t=next(x for x in make_tasks(b,{},1)if x['element_ids']==['next']);e.metadata[FIELD]=make_century_policy(e);self.assertFalse(_current_task(t,b,{}))
 def test_completed_context_keeps_policy(self):
  b,e=fixture();b.elements.append(Element('next','paragraph','Continue.',1,group_id='next',chapter='c1'));t=make_tasks(b,{},10000)[0]
  self.assertEqual(_request_payload(t,['next'],{e.id:GOOD})['payload']['completed_context'][0][FIELD],e.metadata[FIELD])
 def test_old_review_does_not_match_new_optin_revision(self):
  b,e=fixture(False);old={'element_id':e.id,'source_hash':e.source_hash,'translation_hash':digest(GOOD),'review_hash':_review_hash(e,GOOD,b,{})};e.metadata[FIELD]=make_century_policy(e);self.assertFalse(review_record_matches(old,e,GOOD,b,{}))
 def test_marker_mismatch_still_fails(self):
  b,e=fixture(False,'In the twenty-first century, 24 valves form ⟦F:eq-2⟧.');e.asset_ids=['eq-2'];e.metadata[FIELD]=make_century_policy(e)
  self.assertIn('protected_token_mismatch',[i['code']for i in validate_translation(e,GOOD,b)])

class CenturySyntheticStoreTests(unittest.TestCase):
 def setUp(self):
  self.root=Path(tempfile.gettempdir())/('synthetic-century-'+uuid.uuid4().hex);self.root.mkdir();self.store=Store(self.root/'tasks.db')
 def tearDown(self):
  self.store.close();shutil.rmtree(self.root)
 def test_normal_submit_accepts_equivalent_century_and_requires_review(self):
  b,e=fixture();self.store.register(b,{});self.store.add_tasks(make_tasks(b,{}));t=self.store.claim('synthetic');r=self.store.submit(t['id'],'synthetic',t['source_hash'],{e.id:GOOD});self.assertEqual(r['accepted'],[e.id]);self.assertEqual(review_status(b,self.store)['passed'],0);self.assertEqual(review_status(b,self.store)['pending'],1)
 def test_wrong_century_and_remaining_number_do_not_become_accepted(self):
  b,e=fixture();self.store.register(b,{})
  for bad in(GOOD.replace('二十一','一'),GOOD.replace('24','25'),GOOD.replace('世纪','世纪²')):
   with self.subTest(bad=bad):self.assertFalse(self.store.put_candidate(e.id,bad,'synthetic')['accepted'])
  self.assertEqual(self.store.translations(),{})
 def test_old_accepted_and_pass_invalid_after_optin(self):
  b,e=fixture(False);old=GOOD.replace('二十一','一');self.store.register(b,{});self.assertTrue(self.store.put_candidate(e.id,old,'synthetic')['accepted'])
  binding=_review_binding(e,old,b,{});binding.pop('element_id');self.assertTrue(self.store.apply_semantic_review(e.id,old,passed=True,problems=[],provider='synthetic',**binding)['applied'])
  e.metadata[FIELD]=make_century_policy(e);self.store.register(b,{});self.assertEqual(self.store.translations(),{});self.assertEqual(review_status(b,self.store)['passed'],0)
 def test_old_lease_cannot_submit_after_optin(self):
  b,e=fixture(False);self.store.register(b,{});self.store.add_tasks(make_tasks(b,{}));t=self.store.claim('synthetic');e.metadata[FIELD]=make_century_policy(e);self.store.register(b,{})
  with self.assertRaises(ValueError):self.store.submit(t['id'],'synthetic',t['source_hash'],{e.id:GOOD})
 def test_frozen_context_policy_cannot_be_removed_at_submit(self):
  b,e=fixture();b.elements.append(Element('next','paragraph','Continue.',1,group_id='next',chapter='c1'));self.store.register(b,{});t=next(t for t in make_tasks(b,{},1)if t['element_ids']==['next']);t['payload']['context_before'][0].pop(FIELD);t['source_hash']=digest(t['payload']);t['id']='task-'+t['source_hash'][:24];self.store.add_tasks([t]);claimed=self.store.claim('synthetic')
  with self.assertRaises(ValueError):self.store.submit(claimed['id'],'synthetic',claimed['source_hash'],{'next':'继续。'})
 def test_old_review_CAS_rejected_after_optin(self):
  b,e=fixture(False);binding=_review_binding(e,GOOD,b,{});binding.pop('element_id');e.metadata[FIELD]=make_century_policy(e);self.store.register(b,{});self.store.put_candidate(e.id,GOOD,'synthetic');r=self.store.apply_semantic_review(e.id,GOOD,passed=True,problems=[],provider='synthetic',**binding);self.assertFalse(r['applied'])
 def test_independent_review_packet_carries_century_policy(self):
  b,e=fixture();self.store.register(b,{});self.store.put_candidate(e.id,GOOD,'synthetic');packets=[]
  def chat(provider,messages):
   p=json.loads(messages[-1]['content']);packets.append(p);self.assertEqual(p['payload']['elements'][0][FIELD],e.metadata[FIELD]);return json.dumps({'task_id':p['task_id'],'source_hash':p['source_hash'],'reviews':{e.id:{'passed':True,'issues':[]}}}),{'prompt_tokens':1,'completion_tokens':1}
  with patch('textbook_pdf.translation._chat',side_effect=chat):r=_run_review_sequential(b,self.store,None,'synthetic',_provider=Provider('synthetic','http://invalid','dummy','mock',attempts=1))
  self.assertEqual(len(packets),1);self.assertEqual(r['review_passed'],1)
 def test_concurrent_policy_source_change_rejects_returned_review(self):
  b,e=fixture();self.store.register(b,{});self.store.put_candidate(e.id,GOOD,'synthetic');other=Store(self.store.path)
  def chat(provider,messages):
   p=json.loads(messages[-1]['content']);new=deepcopy(b);new.elements[0].metadata['new_semantic_context']='v2';new.elements[0].metadata[FIELD]=make_century_policy(new.elements[0]);other.register(new,{})
   return json.dumps({'task_id':p['task_id'],'source_hash':p['source_hash'],'reviews':{e.id:{'passed':True,'issues':[]}}}),{'prompt_tokens':1,'completion_tokens':1}
  try:
   with patch('textbook_pdf.translation._chat',side_effect=chat):r=_run_review_sequential(b,self.store,None,'synthetic',_provider=Provider('synthetic','http://invalid','dummy','mock',attempts=1))
   self.assertEqual(r['review_passed'],0);self.assertEqual(r['stale_results'],1)
  finally:other.close()
 def test_inline_translation_review_receives_same_century_policy(self):
  b,e=fixture();self.store.register(b,{});self.store.add_tasks(make_tasks(b,{}));cfg=self.root/'config.json';cfg.write_text(json.dumps({'providers':{'translate':{'base_url':'http://invalid','api_key':'dummy','model':'mock','attempts':1},'review':{'base_url':'http://invalid','api_key':'dummy','model':'mock','attempts':1}}}),encoding='utf8');seen=[]
  def chat(provider,messages):
   p=json.loads(messages[-1]['content']);seen.append((provider.name,p))
   if provider.name=='translate':return json.dumps({'task_id':p['task_id'],'source_hash':p['source_hash'],'translations':{e.id:GOOD},'proposed_terms':{}}),{'prompt_tokens':1,'completion_tokens':1}
   self.assertEqual(p['elements'][0][FIELD],e.metadata[FIELD]);return json.dumps({'task_id':p['task_id'],'source_hash':p['source_hash'],'reviews':{e.id:{'passed':True,'issues':[]}}}),{'prompt_tokens':1,'completion_tokens':1}
  with patch('textbook_pdf.translation._chat',side_effect=chat):r=run_translation(b,self.store,cfg,'translate',workers=1,review_provider='review',max_tasks=1)
  self.assertEqual(len(seen),2);self.assertEqual(r['review_passed'],1);self.assertEqual(self.store.translations()[e.id],GOOD)

if __name__=='__main__':unittest.main()
