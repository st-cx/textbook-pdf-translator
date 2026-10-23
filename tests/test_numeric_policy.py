from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import json,shutil,sys,unittest,uuid
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path[:0]=[str(HERE),str(ROOT)]
from textbook_pdf.models import Book,Element,digest,validate_translation,source_contract_issues,RULES_VERSION
from textbook_pdf.store import Store,translation_rule_hash
from textbook_pdf.translation import make_tasks,_current_task,_review_hash,review_record_matches,_review_binding,review_status,review_selected,_request_payload
from textbook_pdf.numeric_policy import make_policy,numeric_policy_fields,check_policy_numbers,normalize_explicit_amounts,canonical

SOURCE='Archive 2.9 million sensor events during 2018; inspect batch 10.4.'
GOOD='2018年归档超过290万个传感事件；核对10.4号批次。'
OLD_FORM='归档超过2.9百万个传感事件（2018年）；核对10.4号批次。'


def fixture(policy=True,text=SOURCE):
    e=Element('e1','paragraph',text,1,group_id='e1',chapter='c1')
    if policy:e.metadata['numeric_policy']=make_policy(e)
    b=Book('own-synthetic.pdf','own-source','Own test',1,[1],elements=[e])
    return b,e


class NumericPolicyTests(unittest.TestCase):
    def test_p700_equivalent_natural_reordering_warning(self):
        b,e=fixture();r=check_policy_numbers(e,GOOD)
        self.assertTrue(r['equivalent']);self.assertTrue(r['order_review_required'])
        issues=validate_translation(e,GOOD,b);self.assertFalse(any(i['severity']=='error'for i in issues))
        self.assertIn('numeric_order_changed',[i['code']for i in issues])
    def test_million_billion_is_hard_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('290万','2.9 billion'))['equivalent'])
    def test_million_wan_wrong_magnitude_is_hard_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('290万','2.9万'))['equivalent'])
    def test_missing_magnitude_is_hard_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('290万','2.9'))['equivalent'])
    def test_wanyi_yi_boundary_rejected(self):
        b,e=fixture(text='There are 1 trillion vertices.')
        self.assertTrue(check_policy_numbers(e,'有1万亿个顶点。')['equivalent'])
        self.assertFalse(check_policy_numbers(e,'有1亿个顶点。')['equivalent'])
    def test_grouped_plain_number_quantity_can_be_used(self):
        b,e=fixture();self.assertTrue(check_policy_numbers(e,GOOD.replace('290万','2,900,000'))['equivalent'])
    def test_sign_flip_explicit_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('290万','-290万'))['equivalent'])
    def test_sign_flip_bare_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('290万','-2900000'))['equivalent'])
    def test_lexical_chinese_words_not_units(self):
        for text in('841 万能地址系统','721 万维网','772 万维网图'):
            with self.subTest(text=text):self.assertEqual(normalize_explicit_amounts(text,['个']),(text,[]))
    def test_Mega_Millions_has_no_numeric_policy(self):
        b,e=fixture(False,'Mega Millions lottery in 2018.')
        self.assertEqual(numeric_policy_fields(e),{})
        with self.assertRaises(ValueError):make_policy(e)
    def test_stacked_units_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('290万','2.9 million billion'))['equivalent'])
    def test_quantity_percentage_corruption_rejected(self):
        b,e=fixture()
        for value in('290万%','2900000%','百分之290万','2900000 percent','290万个%'):
            with self.subTest(value=value):self.assertFalse(check_policy_numbers(e,GOOD.replace('290万',value))['equivalent'])
    def test_unrelated_list_syntax_remains_legacy(self):
        b,e=fixture(text='2.9 million vertices; labels 1,2.')
        self.assertTrue(check_policy_numbers(e,'290万个顶点；标号1,2。')['equivalent'])
    def test_dates_centuries_rest_stays_legacy(self):
        b,e=fixture(text='2.9 million vertices in January 2018, in the twentieth century.')
        self.assertTrue(check_policy_numbers(e,'2018年1月有290万个顶点，在20世纪。')['equivalent'])
    def test_decimal_exact_beyond_float_precision(self):
        self.assertEqual(canonical('9007199254740993',6),'9007199254740993000000')
    def test_decimal_precision_setting_does_not_round(self):
        from decimal import localcontext
        with localcontext()as c:c.prec=2;self.assertEqual(canonical('9007199254740993.29',6),'9007199254740993290000')
    def test_bad_explicit_grouping_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('290万','29,00万'))['equivalent'])
    def test_source_policy_unknown_version_rejected(self):
        b,e=fixture();e.metadata['numeric_policy']['schema']='unknown'
        self.assertTrue(any(i['code']=='source_numeric_policy_invalid'for i in source_contract_issues(b)))
        self.assertFalse(check_policy_numbers(e,GOOD)['equivalent'])
    def test_source_policy_wrong_implementation_rejected(self):
        b,e=fixture();e.metadata['numeric_policy']['implementation_sha256']='0'*64
        with self.assertRaises(ValueError):numeric_policy_fields(e)
    def test_source_policy_exact_source_binding(self):
        b,e=fixture();e.text=e.text.replace('2.9','3.9')
        self.assertFalse(check_policy_numbers(e,GOOD)['equivalent'])
    def test_source_policy_amount_spec_tamper(self):
        b,e=fixture();e.metadata['numeric_policy']['source_amounts'][0]['value']='29000'
        with self.assertRaises(ValueError):numeric_policy_fields(e)
    def test_policy_optin_changes_rule_not_source_hash(self):
        b,e=fixture(False);old=e.source_hash;before=translation_rule_hash(b,e,{})
        e.metadata['numeric_policy']=make_policy(e)
        self.assertEqual(e.source_hash,old);self.assertNotEqual(translation_rule_hash(b,e,{}),before)
    def test_old_policy_task_is_not_current(self):
        b,e=fixture(False);task=make_tasks(b,{})[0];e.metadata['numeric_policy']=make_policy(e)
        self.assertFalse(_current_task(task,b,{}));self.assertTrue(_current_task(make_tasks(b,{})[0],b,{}))
    def test_new_task_cannot_run_in_unmarked_book(self):
        b,e=fixture();task=make_tasks(b,{})[0];e.metadata.pop('numeric_policy')
        self.assertFalse(_current_task(task,b,{}))
    def test_frozen_policy_tamper_even_rehashed_rejected(self):
        b,e=fixture();t=make_tasks(b,{})[0];t['payload']['elements'][0]['numeric_policy']['source_amounts'][0]['value']='1';t['source_hash']=digest(t['payload'])
        self.assertFalse(_current_task(t,b,{}))
    def test_context_policy_bound_conditionally(self):
        b,e=fixture();other=Element('other','paragraph','Use 7.',1,group_id='other',chapter='c1');b.elements.append(other)
        tasks=make_tasks(b,{},target_chars=1);t=next(t for t in tasks if t['element_ids']==['other'])
        self.assertIn('numeric_policy',t['payload']['context_before'][0]);self.assertNotIn('numeric_policy',t['payload']['elements'][0])
        t['payload']['context_before'][0].pop('numeric_policy');t['source_hash']=digest(t['payload'])
        self.assertFalse(_current_task(t,b,{}))
    def test_completed_context_carries_policy_only_for_marked_rows(self):
        b,e=fixture();other=Element('other','paragraph','Use 7.',1,group_id='other',chapter='c1');b.elements.append(other)
        task=make_tasks(b,{},target_chars=10000)[0]
        payload=_request_payload(task,['other'],{'e1':GOOD})
        completed=next(row for row in payload['payload']['completed_context']if row['id']=='e1')
        self.assertEqual(completed['numeric_policy'],e.metadata['numeric_policy']);self.assertTrue(completed['readonly'])
    def test_old_pass_never_matches_new_policy_even_legacy_fallback(self):
        b,e=fixture(False);entry={'element_id':e.id,'source_hash':e.source_hash,'translation_hash':digest(OLD_FORM),'review_hash':_review_hash(e,OLD_FORM,b,{})}
        e.metadata['numeric_policy']=make_policy(e)
        self.assertFalse(review_record_matches(entry,e,OLD_FORM,b,{}));self.assertTrue(review_selected(e))


class PolicyStoreTests(unittest.TestCase):
    def setUp(self):
        self.root=(HERE/('synthetic-'+uuid.uuid4().hex)).resolve();self.root.mkdir();self.store=Store(self.root/'tasks.db')
    def tearDown(self):
        self.store.close();assert self.root.is_relative_to(HERE);shutil.rmtree(self.root)
    def test_old_accepted_not_visible_after_policy_registration(self):
        b,e=fixture(False);self.store.register(b,{})
        self.assertTrue(self.store.put_candidate('e1',OLD_FORM,'synthetic')['accepted'])
        entry={**_review_binding(e,OLD_FORM,b,{}),'passed':True,'problems':[],'provider':'synthetic'};entry.pop('element_id')
        self.assertTrue(self.store.apply_semantic_review('e1',OLD_FORM,**entry)['applied'])
        b2=deepcopy(b);b2.elements[0].metadata['numeric_policy']=make_policy(b2.elements[0]);self.store.register(b2,{})
        self.assertEqual(self.store.translations(),{})
        self.assertEqual(self.store.db.execute('SELECT status FROM translations').fetchone()[0],'accepted')
        self.assertEqual(review_status(b2,self.store)['passed'],0)
    def test_new_acceptance_is_pending_review_not_old_pass(self):
        b,e=fixture();self.store.register(b,{})
        self.assertTrue(self.store.put_candidate('e1',GOOD,'synthetic')['accepted'])
        status=review_status(b,self.store);self.assertEqual(status['pending'],1);self.assertEqual(status['passed'],0)
    def test_old_task_submission_rejected_when_local_store_has_new_policy(self):
        b,e=fixture(False);self.store.register(b,{});old=make_tasks(b,{})[0];self.store.add_tasks([old]);t=self.store.claim('w')
        e.metadata['numeric_policy']=make_policy(e);self.store.register(b,{})
        with self.assertRaises(ValueError):self.store.submit(t['id'],'w',t['source_hash'],{'e1':GOOD})
        self.assertEqual(self.store.translations(),{})
    def test_stale_other_store_cannot_commit_old_rule(self):
        b,e=fixture(False);self.store.register(b,{});self.store.add_tasks(make_tasks(b,{}));t=self.store.claim('w')
        other=Store(self.root/'tasks.db')
        try:
            b2=deepcopy(b);b2.elements[0].metadata['numeric_policy']=make_policy(b2.elements[0]);other.register(b2,{})
            with self.assertRaises(ValueError):self.store.submit(t['id'],'w',t['source_hash'],{'e1':OLD_FORM})
            self.assertEqual(other.translations(),{})
        finally:other.close()
    def test_current_policy_task_submission_accepted_but_review_pending(self):
        b,e=fixture();self.store.register(b,{});self.store.add_tasks(make_tasks(b,{}));t=self.store.claim('w')
        r=self.store.submit(t['id'],'w',t['source_hash'],{'e1':GOOD});self.assertTrue(r['complete']);self.assertEqual(review_status(b,self.store)['passed'],0)
    def test_current_task_policy_frozen_field_removed_rejected_by_store(self):
        b,e=fixture();self.store.register(b,{});task=make_tasks(b,{})[0]
        task['payload']['elements'][0].pop('numeric_policy');task['source_hash']=digest(task['payload']);task['id']='task-'+task['source_hash'][:24]
        self.store.add_tasks([task]);t=self.store.claim('w')
        with self.assertRaises(ValueError):self.store.submit(t['id'],'w',t['source_hash'],{'e1':GOOD})
        self.assertEqual(self.store.translations(),{})
    def test_old_review_cas_cannot_apply_to_new_accepted(self):
        b,e=fixture(False);old=_review_binding(e,OLD_FORM,b,{})
        e.metadata['numeric_policy']=make_policy(e);self.store.register(b,{});self.store.put_candidate('e1',OLD_FORM,'synthetic')
        r=self.store.apply_semantic_review('e1',OLD_FORM,source_hash=old['source_hash'],rule_hash=old['rule_hash'],source_sha256=old['source_sha256'],review_hash=old['review_hash'],passed=True,problems=[],provider='synthetic')
        self.assertFalse(r['applied']);self.assertTrue(r['stale']);self.assertEqual(review_status(b,self.store)['passed'],0)


if __name__=='__main__':unittest.main()
