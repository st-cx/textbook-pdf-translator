"""Self-authored strict month-association and isolated synthetic Store tests."""
from copy import deepcopy
from pathlib import Path
from dataclasses import asdict
import json,sys,shutil,uuid,unittest
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path[:0]=[str(HERE),str(ROOT)]
from textbook_pdf.models import Book,Element,digest,validate_translation,source_contract_issues
from textbook_pdf.store import Store,translation_rule_hash
from textbook_pdf.translation import make_tasks,_current_task,_review_hash,review_record_matches,_review_binding,review_status,review_selected,_request_payload
from textbook_pdf.numeric_policy import make_named_month_policy,numeric_policy_fields,check_policy_numbers,make_policy,make_bibliography_date_policy
from textbook_pdf.models import validate_translation as old_validate

SOURCE='25. Compare the sensor logs recorded during the month of January and during the month of February.'
GOOD='25. 比较一月份与二月份记录的传感器日志。'
def fixture(policy=True,source=SOURCE):
    e=Element('months','exercise',source,707,group_id='months',chapter='c10')
    if policy:e.metadata['numeric_policy']=make_named_month_policy(e)
    b=Book('self-authored-month-fixture.pdf','synthetic-month-source','Synthetic month test',707,[707],elements=[e])
    return b,e

class NamedMonthPolicyTests(unittest.TestCase):
    def test_original_false_positive_reproduced(self):
        b,e=fixture(False);self.assertIn('number_mismatch',[i['code']for i in old_validate(e,GOOD)])
    def test_noun_anchors_to_chinese_months(self):
        b,e=fixture();r=check_policy_numbers(e,GOOD)
        self.assertTrue(r['equivalent']);self.assertTrue(r['independent_semantic_review_required']);self.assertTrue(review_selected(e))
        self.assertEqual(validate_translation(e,GOOD,b),[])
    def test_chinese_month_without_fen(self):
        b,e=fixture();self.assertTrue(check_policy_numbers(e,GOOD.replace('月份','月'))['equivalent'])
    def test_arabic_months(self):
        b,e=fixture();self.assertTrue(check_policy_numbers(e,GOOD.replace('一月份','1月份').replace('二月份','2月份'))['equivalent'])
    def test_month_swap_is_hard_failure(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('一月份','临时').replace('二月份','一月份').replace('临时','二月份'))['equivalent'])
    def test_month_value_change(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('二月份','三月份'))['equivalent'])
    def test_month_missing(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('二月份','当时'))['equivalent'])
    def test_duplicate_month_added(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD+'一月份。')['equivalent'])
    def test_question_number_change(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('25.','26.'))['equivalent'])
    def test_arabic_day_added(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('一月份','一月1日'))['equivalent'])
    def test_chinese_day_added(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('一月份','一月一号'))['equivalent'])
    def test_arabic_year_added(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('一月份','2018年一月份'))['equivalent'])
    def test_chinese_year_added(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('一月份','二〇一八年一月份'))['equivalent'])
    def test_invalid_chinese_month(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('二月份','十三月份'))['equivalent'])
    def test_unanchored_May_March_verbs_not_opted(self):
        for source in ('May people march forward.','We may march 25 steps.','The students March and May sing.'):
            with self.subTest(source=source):
                b,e=fixture(False,source)
                with self.assertRaises(ValueError):make_named_month_policy(e)
    def test_real_month_of_May_is_noun(self):
        b,e=fixture(source='People march during the month of May.')
        self.assertTrue(check_policy_numbers(e,'人们在五月份行进。')['equivalent'])
    def test_real_month_of_March_is_noun(self):
        b,e=fixture(source='People may sing during the month of March.')
        self.assertTrue(check_policy_numbers(e,'人们可以在三月份唱歌。')['equivalent'])
    def test_bare_month_source_conservatively_unsupported(self):
        b,e=fixture(False,'People march in January.')
        with self.assertRaises(ValueError):make_named_month_policy(e)
    def test_explicit_month_year_requires_other_policy(self):
        for src in ('During the month of January 2018.','During the month of January, 2018.','During the month of January 1st.'):
            with self.subTest(src=src):
                b,e=fixture(False,src)
                with self.assertRaises(ValueError):make_named_month_policy(e)
    def test_mixed_magnitude_rejected(self):
        b,e=fixture(False,'2.9 million calls during the month of January.')
        with self.assertRaises(ValueError):make_named_month_policy(e)
    def test_exact_source_tamper_rejected(self):
        b,e=fixture();e.text=e.text.replace('January','March');self.assertFalse(check_policy_numbers(e,GOOD)['equivalent'])
    def test_source_month_policy_value_tamper_rejected(self):
        b,e=fixture();e.metadata['numeric_policy']['source_months'][0]['month']=3
        self.assertTrue(source_contract_issues(b));self.assertFalse(check_policy_numbers(e,GOOD)['equivalent'])
    def test_unmarked_stays_exact_legacy(self):
        b,e=fixture(False);self.assertEqual(numeric_policy_fields(e),{});self.assertEqual(validate_translation(e,GOOD,b),old_validate(e,GOOD))
    def test_rules_review_and_frozen_task_change(self):
        b,e=fixture(False);sourcehash=e.source_hash;rule=translation_rule_hash(b,e,{});rh=_review_hash(e,GOOD,b,{});t=make_tasks(b,{})[0]
        e.metadata['numeric_policy']=make_named_month_policy(e)
        self.assertEqual(e.source_hash,sourcehash);self.assertNotEqual(rule,translation_rule_hash(b,e,{}));self.assertNotEqual(rh,_review_hash(e,GOOD,b,{}));self.assertFalse(_current_task(t,b,{}))
    def test_context_frozen_policy_cannot_be_removed_even_rehashed(self):
        b,e=fixture();b.elements.append(Element('other','paragraph','Continue.',707,group_id='other',chapter='c10'))
        t=next(t for t in make_tasks(b,{},1)if t['element_ids']==['other']);self.assertIn('numeric_policy',t['payload']['context_before'][0])
        t['payload']['context_before'][0].pop('numeric_policy');t['source_hash']=digest(t['payload']);self.assertFalse(_current_task(t,b,{}))
    def test_completed_context_carries_month_policy(self):
        b,e=fixture();b.elements.append(Element('other','paragraph','Continue.',707,group_id='other',chapter='c10'))
        t=make_tasks(b,{},10000)[0];p=_request_payload(t,['other'],{'months':GOOD})
        self.assertEqual(p['payload']['completed_context'][0]['numeric_policy'],e.metadata['numeric_policy'])
    def test_old_pass_not_inherited(self):
        b,e=fixture(False);entry={'element_id':e.id,'source_hash':e.source_hash,'translation_hash':digest(GOOD),'review_hash':_review_hash(e,GOOD,b,{})}
        e.metadata['numeric_policy']=make_named_month_policy(e);self.assertFalse(review_record_matches(entry,e,GOOD,b,{}))
    def test_same_text_other_element_id_cannot_copy_policy(self):
        b,e=fixture();other=deepcopy(e);other.id='different-id'
        self.assertEqual(other.source_hash,e.source_hash)
        self.assertFalse(check_policy_numbers(other,GOOD)['equivalent'])
    def test_same_text_other_page_cannot_copy_policy(self):
        b,e=fixture();e.page=708;self.assertFalse(check_policy_numbers(e,GOOD)['equivalent'])
    def test_same_source_hash_group_change_invalidates_policy(self):
        b,e=fixture();old=e.source_hash;e.group_id='different-group';self.assertEqual(e.source_hash,old)
        self.assertFalse(check_policy_numbers(e,GOOD)['equivalent'])
    def test_same_source_hash_style_change_invalidates_policy(self):
        b,e=fixture();old=e.source_hash;e.style['role']='caption';self.assertEqual(e.source_hash,old)
        self.assertFalse(check_policy_numbers(e,GOOD)['equivalent'])


class LayoutOnlyIdentityTests(unittest.TestCase):
    @staticmethod
    def identity(book,element):
        return {'policy':numeric_policy_fields(element),
            'rule':translation_rule_hash(book,element,{}),
            'review':_review_hash(element,GOOD,book,{}),
            'tasks':make_tasks(book,{})}
    def test_display_assets_change_no_numeric_or_rule_task_review_identity(self):
        b,e=fixture();before=self.identity(b,e);e.metadata['display_asset_ids']=['layout-only-display-object']
        self.assertEqual(before,self.identity(b,e));self.assertTrue(check_policy_numbers(e,GOOD)['equivalent'])
    def test_book_profile_font_and_size_change_no_numeric_or_rule_task_review_identity(self):
        b,e=fixture();before=self.identity(b,e)
        b.profile.update(body_font='Different body font',heading_font='Different heading font',body_size=11,exercise_size=10,caption_size=9)
        self.assertEqual(before,self.identity(b,e))
    def test_old_frozen_task_stays_current_after_display_and_profile_changes(self):
        b,e=fixture();t=make_tasks(b,{})[0]
        e.metadata['display_asset_ids']=['render-only'];b.profile.update(body_font='Other font',body_size=12)
        self.assertTrue(_current_task(t,b,{}));self.assertEqual(t,make_tasks(b,{})[0])
    def test_unknown_semantic_unit_metadata_stays_strongly_bound(self):
        b,e=fixture();oldhash=e.source_hash;oldpolicy=deepcopy(e.metadata['numeric_policy'])
        e.metadata['semantic_unit_ids']=['cross-page-unit']
        self.assertEqual(oldhash,e.source_hash);self.assertFalse(check_policy_numbers(e,GOOD)['equivalent'])
        with self.assertRaises(ValueError):numeric_policy_fields(e)
        e.metadata['numeric_policy']=make_named_month_policy(e)
        self.assertNotEqual(oldpolicy,e.metadata['numeric_policy'])


class NamedMonthSyntheticStoreTests(unittest.TestCase):
    def setUp(self):
        self.root=(HERE/('synthetic-month-'+uuid.uuid4().hex)).resolve();self.root.mkdir();self.store=Store(self.root/'tasks.db')
    def tearDown(self):
        self.store.close();assert self.root.is_relative_to(HERE.resolve());shutil.rmtree(self.root)
    def test_new_accepted_still_pending_semantic_review(self):
        b,e=fixture();self.store.register(b,{});self.assertTrue(self.store.put_candidate(e.id,GOOD,'synthetic')['accepted'])
        self.assertEqual(review_status(b,self.store)['passed'],0);self.assertEqual(review_status(b,self.store)['pending'],1)
    def test_old_accepted_and_pass_invalid_after_policy(self):
        b,e=fixture(False);old='25. 说明 January 和 February 两个电话呼叫图的用途。'
        self.store.register(b,{});self.assertTrue(self.store.put_candidate(e.id,old,'synthetic')['accepted'])
        binding=_review_binding(e,old,b,{});binding.pop('element_id');self.assertTrue(self.store.apply_semantic_review(e.id,old,passed=True,problems=[],provider='synthetic',**binding)['applied'])
        n=deepcopy(b);n.elements[0].metadata['numeric_policy']=make_named_month_policy(n.elements[0]);self.store.register(n,{})
        self.assertEqual(self.store.translations(),{});self.assertEqual(review_status(n,self.store)['passed'],0)
    def test_old_lease_cannot_submit_after_month_policy(self):
        b,e=fixture(False);self.store.register(b,{});self.store.add_tasks(make_tasks(b,{}));t=self.store.claim('w')
        e.metadata['numeric_policy']=make_named_month_policy(e);self.store.register(b,{})
        with self.assertRaises(ValueError):self.store.submit(t['id'],'w',t['source_hash'],{e.id:GOOD})
    def test_old_review_cas_cannot_apply_new_policy(self):
        b,e=fixture(False);old_binding=_review_binding(e,GOOD,b,{})
        e.metadata['numeric_policy']=make_named_month_policy(e);self.store.register(b,{});self.store.put_candidate(e.id,GOOD,'synthetic')
        r=self.store.apply_semantic_review(e.id,GOOD,source_hash=old_binding['source_hash'],rule_hash=old_binding['rule_hash'],source_sha256=old_binding['source_sha256'],review_hash=old_binding['review_hash'],passed=True,problems=[],provider='synthetic')
        self.assertFalse(r['applied']);self.assertEqual(review_status(b,self.store)['passed'],0)

if __name__=='__main__':unittest.main()
