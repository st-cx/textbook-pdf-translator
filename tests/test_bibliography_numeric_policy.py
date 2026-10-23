from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import json,shutil,sys,unittest,uuid
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path[:0]=[str(HERE),str(ROOT)]
from textbook_pdf.models import Book,Element,digest,validate_translation,source_contract_issues
from textbook_pdf.store import Store,translation_rule_hash
from textbook_pdf.translation import make_tasks,_current_task,_review_hash,_review_binding,review_record_matches,review_selected,review_status,_request_payload
from textbook_pdf.numeric_policy import make_bibliography_date_policy,numeric_policy_fields,check_policy_numbers
from textbook_pdf.models import validate_translation as legacy_validate

SOURCE='[Ax10] A. Example, “A Sensor Diary,” November, 2010, available at https://example.invalid/own.pdf [By82] B. Example, Notes on Signals, Example Press, 1982.'
GOOD='[Ax10] A. Example，《传感器日记》，2010年11月，可从 https://example.invalid/own.pdf 获取 [By82] B. Example，《信号札记》，Example Press，1982。'
OLD_FORM=GOOD.replace('2010年11月','November, 2010')

def fixture(policy=True,source=SOURCE):
    e=Element('bib','paragraph',source,986,group_id='bib',chapter='biblio')
    if policy:e.metadata['numeric_policy']=make_bibliography_date_policy(e)
    b=Book('self-authored-fixture.pdf','biblio-fixture','Synthetic bibliography',986,[986],elements=[e])
    return b,e

class BibliographyDateNumericTests(unittest.TestCase):
    def test_actual_comma_month_year_false_positive_reproduced(self):
        b,e=fixture(False);self.assertIn('number_mismatch',[x['code']for x in legacy_validate(e,GOOD)])
    def test_comma_month_year_natural_chinese_passes(self):
        b,e=fixture();self.assertTrue(check_policy_numbers(e,GOOD)['equivalent']);self.assertFalse(any(x['severity']=='error'for x in validate_translation(e,GOOD,b)))
    def test_chinese_month_spelling_passes(self):
        b,e=fixture();self.assertTrue(check_policy_numbers(e,GOOD.replace('11月','十一月'))['equivalent'])
    def test_wrong_month_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('11月','12月'))['equivalent'])
    def test_wrong_year_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('2010年','2011年'))['equivalent'])
    def test_missing_month_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('2010年11月','2010年'))['equivalent'])
    def test_extra_day_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('11月','11月2日'))['equivalent'])
    def test_date_moved_to_other_entry_rejected(self):
        b,e=fixture();target=GOOD.replace('2010年11月，','').replace('[By82]','[By82] 2010年11月，')
        self.assertFalse(check_policy_numbers(e,target)['equivalent'])
    def test_reference_key_digit_changed_rejected_despite_legacy_blindspot(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('[Ax10]','[Ax11]'))['equivalent'])
    def test_reference_key_order_changed_rejected(self):
        b,e=fixture();target=GOOD.replace('[Ax10]','[Tmp99]').replace('[By82]','[Ax10]').replace('[Tmp99]','[By82]')
        self.assertFalse(check_policy_numbers(e,target)['equivalent'])
    def test_remaining_publication_year_change_rejected(self):
        b,e=fixture();self.assertFalse(check_policy_numbers(e,GOOD.replace('1982','1983'))['equivalent'])
    def test_unmarked_comma_date_remains_legacy(self):
        b,e=fixture(False);self.assertEqual(numeric_policy_fields(e),{});self.assertEqual(validate_translation(e,GOOD,b),legacy_validate(e,GOOD))
    def test_month_verb_without_bound_date_not_opted_in(self):
        for source in ('[Ma10] People may 2 things.','[Ma10] We march 2010 steps.','November, 2010 without citation.'):
            with self.subTest(source=source):
                _,e=fixture(False,source)
                with self.assertRaises(ValueError):make_bibliography_date_policy(e)
    def test_mixed_magnitude_not_silently_reinterpreted(self):
        _,e=fixture(False,'[Ax10] 2.9 million records in November, 2010.')
        with self.assertRaises(ValueError):make_bibliography_date_policy(e)
    def test_duplicate_citation_key_requires_stronger_policy(self):
        _,e=fixture(False,'[Ax10] November, 2010. [Ax10] January, 2012.')
        with self.assertRaises(ValueError):make_bibliography_date_policy(e)
    def test_source_date_anchor_tamper_rejected(self):
        b,e=fixture();e.metadata['numeric_policy']['source_dates'][0]['month']=12
        self.assertTrue(source_contract_issues(b));self.assertFalse(check_policy_numbers(e,GOOD)['equivalent'])
    def test_policy_rule_review_changed_but_source_hash_not(self):
        b,e=fixture(False);source=e.source_hash;rule=translation_rule_hash(b,e,{});rh=_review_hash(e,OLD_FORM,b,{})
        e.metadata['numeric_policy']=make_bibliography_date_policy(e)
        self.assertEqual(e.source_hash,source);self.assertNotEqual(translation_rule_hash(b,e,{}),rule);self.assertNotEqual(_review_hash(e,OLD_FORM,b,{}),rh);self.assertTrue(review_selected(e))
    def test_context_only_old_task_stale(self):
        b,e=fixture(False);b.elements.append(Element('next','paragraph','Continue here.',986,group_id='next',chapter='biblio'))
        old=next(t for t in make_tasks(b,{},1)if t['element_ids']==['next']);e.metadata['numeric_policy']=make_bibliography_date_policy(e)
        self.assertFalse(_current_task(old,b,{}));new=next(t for t in make_tasks(b,{},1)if t['element_ids']==['next'])
        self.assertEqual(new['payload']['context_before'][0]['numeric_policy'],e.metadata['numeric_policy']);self.assertTrue(_current_task(new,b,{}))
    def test_changed_date_policy_frozen_payload_even_rehashed_rejected(self):
        b,e=fixture();t=make_tasks(b,{})[0];t['payload']['elements'][0]['numeric_policy']['source_dates'][0]['year']=2011;t['source_hash']=digest(t['payload'])
        self.assertFalse(_current_task(t,b,{}))
    def test_old_semantic_pass_not_matching(self):
        b,e=fixture(False);entry={'element_id':e.id,'source_hash':e.source_hash,'translation_hash':digest(OLD_FORM),'review_hash':_review_hash(e,OLD_FORM,b,{})}
        e.metadata['numeric_policy']=make_bibliography_date_policy(e);self.assertFalse(review_record_matches(entry,e,OLD_FORM,b,{}))

class BibliographyPolicyStoreTests(unittest.TestCase):
    def setUp(self):
        self.root=HERE/('synthetic-date-'+uuid.uuid4().hex);self.root.mkdir();self.store=Store(self.root/'tasks.db')
    def tearDown(self):
        self.store.close();assert self.root.resolve().is_relative_to(HERE.resolve());shutil.rmtree(self.root)
    def test_old_accepted_and_pass_not_visible_under_new_policy(self):
        b,e=fixture(False);self.store.register(b,{});self.assertTrue(self.store.put_candidate(e.id,OLD_FORM,'synthetic')['accepted'])
        binding=_review_binding(e,OLD_FORM,b,{});binding.pop('element_id');self.assertTrue(self.store.apply_semantic_review(e.id,OLD_FORM,passed=True,problems=[],provider='synthetic',**binding)['applied'])
        n=deepcopy(b);n.elements[0].metadata['numeric_policy']=make_bibliography_date_policy(n.elements[0]);self.store.register(n,{})
        self.assertEqual(self.store.translations(),{});self.assertEqual(review_status(n,self.store)['passed'],0)
    def test_new_candidate_accepts_pending_semantic_review(self):
        b,e=fixture();self.store.register(b,{});self.assertTrue(self.store.put_candidate(e.id,GOOD,'synthetic')['accepted']);self.assertEqual(review_status(b,self.store)['pending'],1)
    def test_old_lease_result_cannot_submit_after_policy_change(self):
        b,e=fixture(False);self.store.register(b,{});self.store.add_tasks(make_tasks(b,{}));t=self.store.claim('w')
        e.metadata['numeric_policy']=make_bibliography_date_policy(e);self.store.register(b,{})
        with self.assertRaises(ValueError):self.store.submit(t['id'],'w',t['source_hash'],{e.id:GOOD})
    def test_removed_context_policy_store_rejects_rehashed_task(self):
        b,e=fixture();other=Element('next','paragraph','Continue.',986,group_id='next',chapter='biblio');b.elements.append(other);self.store.register(b,{})
        t=next(t for t in make_tasks(b,{},1)if t['element_ids']==['next']);t['payload']['context_before'][0].pop('numeric_policy');t['source_hash']=digest(t['payload']);t['id']='task-'+t['source_hash'][:24]
        self.store.add_tasks([t]);t=self.store.claim('w')
        with self.assertRaises(ValueError):self.store.submit(t['id'],'w',t['source_hash'],{'next':'继续。'})
if __name__=='__main__':unittest.main()
