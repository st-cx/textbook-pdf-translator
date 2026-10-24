"""Self-authored finite bit-string counts through the real transactional Store."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import shutil
import json
import sys
import unittest
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))

from textbook_pdf.models import (Book, Element, Asset, digest, validate_translation,
                                 source_contract_issues)
from textbook_pdf.store import Store, translation_rule_hash
from textbook_pdf.translation import (make_tasks, _current_task, _request_payload,
    _review_binding, _review_hash, review_record_matches, review_selected, review_status)
from textbook_pdf.cardinal_counts import make_policy, SCHEMA
from textbook_pdf.numeric_policy_dispatch import numeric_policy_fields

SOURCE = '15. How many bit strings of length eight do not contain six consecutive 0s?'
GOOD = '15. 长度为8且不包含6个连续0的位串有多少个？'
OLD_VALID = '15. 长度为八且不包含六个连续0的位串有多少个？'


def fixture(policy=True):
    e = Element('own-count', 'exercise', SOURCE, 1,
                group_id='own-count', chapter='own-chapter')
    if policy:
        e.metadata['numeric_policy'] = make_policy(e)
    return Book('own-cardinal.pdf', 'own-cardinal-source', 'Own fixture', 1, [1],
                elements=[e]), e


def apply_pass(store, b, e, text):
    binding = _review_binding(e, text, b, {})
    binding.pop('element_id')
    return store.apply_semantic_review(e.id, text, **binding, passed=True,
                                       problems=[], provider='synthetic-no-api')


class PipelineIdentityTests(unittest.TestCase):
    def test_new_cardinal_policy_is_source_valid_and_mandatory_review(self):
        b, e = fixture()
        self.assertEqual(source_contract_issues(b), [])
        self.assertEqual(validate_translation(e, GOOD, b), [])
        self.assertTrue(review_selected(e))

    def test_source_token_text_and_rule_are_not_confused(self):
        b, e = fixture(False)
        source = e.source_hash
        old = translation_rule_hash(b, e, {})
        before = make_tasks(b, {})[0]
        e.metadata['numeric_policy'] = make_policy(e)
        self.assertEqual(e.source_hash, source)
        self.assertNotEqual(translation_rule_hash(b, e, {}), old)
        self.assertFalse(_current_task(before, b, {}))
        self.assertTrue(_current_task(make_tasks(b, {})[0], b, {}))

    def test_policy_removed_or_schema_changed_rejects_frozen_task(self):
        b, e = fixture()
        task = make_tasks(b, {})[0]
        e.metadata.pop('numeric_policy')
        self.assertFalse(_current_task(task, b, {}))
        e.metadata['numeric_policy'] = {'schema': 'unknown'}
        with self.assertRaises(ValueError):
            numeric_policy_fields(e)
        self.assertIn('source_numeric_policy_invalid',
                      [i['code'] for i in source_contract_issues(b)])

    def test_rehashed_frozen_policy_tamper_cannot_become_current(self):
        b, _ = fixture()
        task = make_tasks(b, {})[0]
        task['payload']['elements'][0]['numeric_policy']['source_events'][0]['value'] = 6
        task['source_hash'] = digest(task['payload'])
        self.assertFalse(_current_task(task, b, {}))

    def test_old_pass_not_valid_for_new_policy(self):
        b, e = fixture(False)
        old = _review_binding(e, OLD_VALID, b, {})
        old['translation_hash'] = digest(OLD_VALID)
        e.metadata['numeric_policy'] = make_policy(e)
        self.assertFalse(review_record_matches(old, e, OLD_VALID, b, {}))

    def test_context_and_completed_context_carry_full_cardinal_binding(self):
        b, e = fixture()
        other = Element('other', 'paragraph', 'Use 7 examples.', 1,
                        group_id='other', chapter=e.chapter)
        b.elements.append(other)
        task = next(t for t in make_tasks(b, {}, target_chars=1)
                    if t['element_ids'] == ['other'])
        self.assertEqual(task['payload']['context_before'][0]['numeric_policy'],
                         e.metadata['numeric_policy'])
        task['payload']['context_before'][0].pop('numeric_policy')
        task['source_hash'] = digest(task['payload'])
        self.assertFalse(_current_task(task, b, {}))
        both = make_tasks(b, {}, target_chars=10000)[0]
        reduced = _request_payload(both, ['other'], {e.id: GOOD})
        ctx = reduced['payload']['completed_context'][0]
        self.assertTrue(ctx['readonly'])
        self.assertEqual(ctx['numeric_policy'], e.metadata['numeric_policy'])

    def test_layout_only_book_profile_or_display_association_changes_no_identity(self):
        b, e = fixture()
        before = (numeric_policy_fields(e), translation_rule_hash(b, e, {}),
                  make_tasks(b, {}), _review_hash(e, GOOD, b, {}))
        b.profile.update(body_font='OwnFont', exercise_size=11.5)
        e.metadata['display_asset_ids'] = ['own-render-only']
        self.assertEqual(before, (numeric_policy_fields(e), translation_rule_hash(b, e, {}),
                                 make_tasks(b, {}), _review_hash(e, GOOD, b, {})))

    def test_unknown_semantic_metadata_changes_invalidate_policy(self):
        _, e = fixture()
        e.metadata['semantic_unit_dependency'] = 'new-unit'
        with self.assertRaises(ValueError):
            numeric_policy_fields(e)

    def test_mixed_century_and_percentage_policies_rejected(self):
        for key in ('century_policy', 'percentage_unit_policy'):
            _, e = fixture()
            e.metadata[key] = {'schema': 'unsupported'}
            with self.subTest(key=key), self.assertRaises(ValueError):
                numeric_policy_fields(e)

    def test_unprotected_or_protected_new_math_is_not_enabled_by_cardinal_schema(self):
        b, e = fixture()
        self.assertTrue(validate_translation(e, GOOD + '⟦F:alien⟧', b))
        e.text += ' ⟦F:own-math⟧'
        e.asset_ids = ['own-math']
        b.assets['own-math'] = Asset('own-math', 'formula', 'unused.pdf', 1,
                                     [0, 0, 10, 10], 10, 10, text='x', sha256='a' * 64)
        with self.assertRaises(ValueError):
            make_policy(e)


class CardinalStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = HERE / '_tmp_cardinal' / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.store = Store(self.root / 'tasks.db')
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(self.store.close)

    def prepare(self, policy=True):
        b, e = fixture(policy)
        self.store.register(b, {})
        self.store.add_tasks(make_tasks(b, {}))
        return b, e, self.store.claim('worker')

    def test_current_claim_submit_accepts_each_equivalent_form_but_is_pending(self):
        for length, run in [('8', '6'), ('八', '六'), ('eight', 'six')]:
            with self.subTest(length=length, run=run):
                b, e = fixture()
                self.store.register(b, {})
                self.store.add_tasks(make_tasks(b, {}))
                if self.store.translations():
                    self.store.redo_element(e.id, 'synthetic equivalent form')
                task = self.store.claim('worker')
                text = f'15. 长度为{length}且不包含{run}个连续0的位串有多少个？'
                result = self.store.submit(task['id'], 'worker', task['source_hash'], {e.id: text})
                self.assertEqual(result['accepted'], [e.id])
                self.assertTrue(result['complete'])
                status = review_status(b, self.store)
                self.assertEqual((status['pending'], status['passed']), (1, 0))

    def test_wrong_values_roles_question_number_or_bit_never_accept(self):
        b, e, task = self.prepare()
        for text in [GOOD.replace('为8', '为7'), GOOD.replace('6个', '5个'),
                     GOOD.replace('为8', '为6').replace('6个', '8个'),
                     GOOD.replace('15.', '16.'), GOOD.replace('连续0', '连续1')]:
            with self.subTest(text=text):
                r = self.store.put_candidate(e.id, text, 'synthetic-invalid')
                self.assertFalse(r['accepted'])
                self.assertIn('number_mismatch', [i['code'] for i in r['issues']])
        self.assertEqual(self.store.translations(), {})

    def test_old_accepted_and_pass_are_preserved_history_but_not_current(self):
        b, e, _ = self.prepare(False)
        self.assertTrue(self.store.put_candidate(e.id, OLD_VALID, 'old-rule')['accepted'])
        self.assertTrue(apply_pass(self.store, b, e, OLD_VALID)['applied'])
        before_candidates = self.store.db.execute('SELECT count(*) FROM candidates').fetchone()[0]
        newer = deepcopy(b)
        newer.elements[0].metadata['numeric_policy'] = make_policy(newer.elements[0])
        self.store.register(newer, {})
        self.assertEqual(self.store.translations(), {})
        self.assertEqual(self.store.db.execute('SELECT status FROM translations').fetchone()[0], 'accepted')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM candidates').fetchone()[0], before_candidates)
        self.assertEqual(review_status(newer, self.store)['passed'], 0)
        self.assertTrue(any(i['code'] == 'semantic_review_passed'
                            for i in (json.loads(row[0]) for row in self.store.db.execute('SELECT json FROM issues'))))

    def test_old_claim_cannot_submit_after_policy_registration(self):
        b, e, old = self.prepare(False)
        e.metadata['numeric_policy'] = make_policy(e)
        self.store.register(b, {})
        with self.assertRaises(ValueError):
            self.store.submit(old['id'], 'worker', old['source_hash'], {e.id: GOOD})
        self.assertEqual(self.store.translations(), {})

    def test_new_claim_cannot_submit_after_policy_removed(self):
        b, e, task = self.prepare()
        e.metadata.pop('numeric_policy')
        self.store.register(b, {})
        with self.assertRaises(ValueError):
            self.store.submit(task['id'], 'worker', task['source_hash'], {e.id: GOOD})
        self.assertEqual(self.store.translations(), {})

    def test_context_policy_change_is_rejected_by_store_not_just_pure_helper(self):
        b, e = fixture(False)
        other = Element('other', 'paragraph', 'Use 7 examples.', 1,
                        group_id='other', chapter=e.chapter)
        b.elements.append(other)
        self.store.register(b, {})
        tasks = make_tasks(b, {}, target_chars=1)
        self.store.add_tasks(tasks)
        task = self.store.claim('worker', task_id=next(t['id'] for t in tasks if t['element_ids'] == ['other']))
        e.metadata['numeric_policy'] = make_policy(e)
        self.store.register(b, {})
        with self.assertRaises(ValueError):
            self.store.submit(task['id'], 'worker', task['source_hash'], {'other': '使用7个示例。'})
        self.assertEqual(self.store.translations(), {})

    def test_same_source_hash_changed_group_or_style_cannot_reuse_policy(self):
        b, e, _ = self.prepare()
        for field in ('group_id', 'style'):
            newer = deepcopy(b)
            target = newer.elements[0]
            setattr(target, field, 'different' if field == 'group_id' else {'column': 1})
            self.assertEqual(target.source_hash, e.source_hash)
            with self.subTest(field=field), self.assertRaises(ValueError):
                translation_rule_hash(newer, target, {})
        self.assertEqual(self.store.translations(), {})

    def test_stale_other_session_cannot_commit_against_rebound_policy(self):
        b, e, old = self.prepare(False)
        with Store(self.root / 'tasks.db') as other:
            new = deepcopy(b)
            new.elements[0].metadata['numeric_policy'] = make_policy(new.elements[0])
            other.register(new, {})
            with self.assertRaises(ValueError):
                self.store.submit(old['id'], 'worker', old['source_hash'], {e.id: OLD_VALID})
            self.assertEqual(other.translations(), {})

    def test_stale_review_cas_does_not_copy_a_previous_rule_pass(self):
        b, e = fixture(False)
        old = _review_binding(e, OLD_VALID, b, {})
        e.metadata['numeric_policy'] = make_policy(e)
        self.store.register(b, {})
        self.assertTrue(self.store.put_candidate(e.id, OLD_VALID, 'current-rule')['accepted'])
        old.pop('element_id')
        r = self.store.apply_semantic_review(e.id, OLD_VALID, **old, passed=True,
                                             problems=[], provider='synthetic-no-api')
        self.assertFalse(r['applied'])
        self.assertTrue(r['stale'])
        self.assertEqual(review_status(b, self.store)['passed'], 0)

    def test_normal_review_applies_only_after_new_source_acceptance(self):
        b, e, task = self.prepare()
        self.store.submit(task['id'], 'worker', task['source_hash'], {e.id: GOOD})
        self.assertTrue(apply_pass(self.store, b, e, GOOD)['applied'])
        self.assertEqual(review_status(b, self.store)['passed'], 1)

    def test_missing_policy_even_rehashed_task_is_rejected_at_submission(self):
        b, e = fixture()
        self.store.register(b, {})
        t = make_tasks(b, {})[0]
        t['payload']['elements'][0].pop('numeric_policy')
        t['source_hash'] = digest(t['payload'])
        t['id'] = 'task-' + t['source_hash'][:24]
        self.store.add_tasks([t])
        claimed = self.store.claim('worker')
        with self.assertRaises(ValueError):
            self.store.submit(claimed['id'], 'worker', claimed['source_hash'], {e.id: GOOD})
        self.assertEqual(self.store.translations(), {})

    def test_outer_transaction_failure_rolls_back_current_candidate(self):
        b, e, _ = self.prepare()
        with self.assertRaisesRegex(RuntimeError, 'synthetic outer failure'):
            with self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                self.assertTrue(self.store.put_candidate(e.id, GOOD, 'synthetic-no-api')['accepted'])
                raise RuntimeError('synthetic outer failure')
        self.assertEqual(self.store.translations(), {})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM candidates').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
