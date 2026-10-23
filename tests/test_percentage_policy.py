"""Self-authored opt-in percentage rule, task and CAS regressions."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch
import hashlib
import json
import tempfile
import uuid
from textbook_pdf.models import Book, Element, digest, source_contract_issues, validate_translation
from textbook_pdf.store import Store, translation_rule_hash
from textbook_pdf.numeric_policy import make_policy, numeric_policy_fields
from textbook_pdf.percentage_policy import (FIELD, make_percentage_policy, percentage_policy_fields,
    frozen_percentage_policy_matches, check_percentage_numbers, percentage_unit_issues)
from textbook_pdf.translation import (make_tasks, _current_task, _review_hash, _review_binding,
    review_record_matches, review_selected, _run_review_sequential, Provider)
import textbook_pdf.percentage_policy as percentage
import textbook_pdf.numeric_policy as numeric
OUT = Path(tempfile.gettempdir()) / ('percentage-policy-fixtures-' + uuid.uuid4().hex)
OUT.mkdir()

def book(*elements):
    return Book('', 'synthetic-original-pdf', 'Self-authored rates', 1, [1], elements=list(elements))

def rate(text='Population is 7.6 billion with growth 1.12% a year.', **kw):
    return Element('rate', 'paragraph', text, 1, [0, 0, 200, 20], group_id='rate', chapter='one', style={'source_size': 10.5}, **kw)

def dual(element):
    e = deepcopy(element)
    e.metadata[FIELD] = make_percentage_policy(e)
    if 'numeric_policy' in e.metadata:
        e.metadata['numeric_policy'] = make_policy(e, chinese_classifiers=tuple(e.metadata['numeric_policy']['chinese_classifiers']))
    return e

def quantity_rate():
    e = rate()
    e.metadata['numeric_policy'] = make_policy(e, chinese_classifiers=('人', '个'))
    return e

fixture = rate('In 2017, a model contains 7.6 billion records and grows by 1.12% annually.')
SOURCE = asdict(fixture)
fixture.metadata['numeric_policy'] = make_policy(fixture, chinese_classifiers=('条',))
PROPOSAL = {'element_before': SOURCE, 'element_after': asdict(fixture)}
TARGET = '2017年，模型含有76亿条记录，每年增长1.12%。'

class PercentageOverlayTests(TestCase):
    def setUp(self):
        self.net = patch('textbook_pdf.translation._chat', side_effect=AssertionError('API forbidden'))
        self.mock_chat = self.net.start()
        self.addCleanup(self.net.stop)
        self.addCleanup(lambda: self.assertEqual(self.mock_chat.call_count, 0))

    def store(self):
        path = OUT / 'synthetic-databases' / uuid.uuid4().hex
        path.mkdir(parents=True)
        s = Store(path / 'tasks.db')
        self.addCleanup(s.close)
        return s


    def test_self_authored_quantity_rate_policy_and_natural_translation(self):
        e = dual(Element(**deepcopy(PROPOSAL['element_after'])))
        self.assertEqual(e.source_hash, Element(**SOURCE).source_hash)
        self.assertEqual(source_contract_issues(book(e)), [])
        self.assertEqual(validate_translation(e, TARGET, book(e)), [])
        self.assertTrue(check_percentage_numbers(e, TARGET)['equivalent'])
        numeric_policy_fields(e)
        percentage_policy_fields(e)

    def test_self_authored_lost_percent_and_wrong_dimension_fail(self):
        e = dual(Element(**deepcopy(PROPOSAL['element_after'])))
        for changed in ('1.12', '1.12个百分点', '1.21%', '1.12%万亿', '1.12万亿%', '1.12% 1.12%'):
            with self.subTest(changed=changed):
                text = TARGET.replace('1.12%', changed)
                self.assertTrue(any(i['code'] == 'percentage_unit_mismatch' for i in validate_translation(e, text, book(e))))

    def test_quantity_wrong_magnitude_still_fails_independently(self):
        e = dual(Element(**deepcopy(PROPOSAL['element_after'])))
        for target in (TARGET.replace('76亿', '7.6亿'), TARGET.replace('76亿', '76万亿')):
            with self.subTest(target=target):
                self.assertTrue(any(i['code'] == 'number_mismatch' for i in validate_translation(e, target, book(e))))

    def test_percentage_guard_does_not_replace_year_or_token_validation(self):
        e = dual(Element(**deepcopy(PROPOSAL['element_after'])))
        self.assertTrue(any(i['code'] == 'number_mismatch' for i in validate_translation(e, TARGET.replace('2017', '2018'), book(e))))
        f = rate('Rate 1.12% and ⟦F:formula-a⟧.')
        f = dual(f)
        self.assertTrue(any(i['code'] == 'protected_token_mismatch' for i in validate_translation(f, '比率1.12%。')))

    def test_order_of_two_policy_construction_is_explicit(self):
        e = quantity_rate()
        old = deepcopy(e.metadata['numeric_policy'])
        e.metadata[FIELD] = make_percentage_policy(e)
        with self.assertRaises(ValueError):
            numeric_policy_fields(e)
        e.metadata['numeric_policy'] = make_policy(e, chinese_classifiers=('人', '个'))
        self.assertNotEqual(old['source_record_sha256'], e.metadata['numeric_policy']['source_record_sha256'])
        percentage_policy_fields(e)
        numeric_policy_fields(e)
        self.assertEqual(e.metadata['numeric_policy']['implementation_sha256'], 'b79efd8bf01eade62a50e10e2be7823b6d944109452e8a3bebaeb8954bb2186b')

    def test_policy_key_completeness_and_strict_boolean(self):
        e = dual(rate('Growth is 1.12%.'))
        for change in ('missing', 'extra', 'bool'):
            changed = deepcopy(e)
            if change == 'missing': changed.metadata[FIELD].pop('parser_sha256')
            elif change == 'extra': changed.metadata[FIELD]['ignored'] = 'x'
            else: changed.metadata[FIELD]['independent_semantic_review_required'] = 1
            with self.subTest(change=change), self.assertRaises(ValueError):
                percentage_policy_fields(changed)

    def test_adapter_and_parser_sha_drift_reject_old_policy(self):
        e = dual(rate('Growth is 1.12%.'))
        for method in ('implementation_sha256', 'parser_sha256'):
            with self.subTest(method=method), patch.object(percentage, method, return_value='0' * 64):
                with self.assertRaises(ValueError): percentage_policy_fields(e)

    def test_source_identity_geometry_role_group_and_unknown_metadata_bound(self):
        e = dual(rate('Growth is 1.12%.'))
        mutations = [lambda x: setattr(x, 'id', 'different'), lambda x: setattr(x, 'page', 2),
                     lambda x: setattr(x, 'group_id', 'other'), lambda x: setattr(x, 'kind', 'exercise'),
                     lambda x: x.bbox.__setitem__(0, 1), lambda x: x.style.__setitem__('source_size', 9.5),
                     lambda x: x.metadata.__setitem__('semantic_unit_marker', 'new')]
        for mutate in mutations:
            changed = deepcopy(e); mutate(changed)
            with self.subTest(record=asdict(changed)), self.assertRaises(ValueError):
                percentage_policy_fields(changed)

    def test_display_association_and_book_font_do_not_change_semantic_identity(self):
        e = dual(quantity_rate()); old_book = book(e)
        old = (percentage_policy_fields(e), numeric_policy_fields(e), translation_rule_hash(old_book, e, {}), _review_hash(e, '增长率1.12%。', old_book, {}))
        e.metadata['display_asset_ids'] = ['render-only']
        new_book = book(e); new_book.profile.update(body_font='Different', body_size=12)
        new = (percentage_policy_fields(e), numeric_policy_fields(e), translation_rule_hash(new_book, e, {}), _review_hash(e, '增长率1.12%。', new_book, {}))
        self.assertEqual(old, new)

    def test_unparsed_source_markers_do_not_get_a_policy(self):
        for text in ('n % 2', 'Growth is 百分之五.', 'x percent', '20 million percent', '12,34%', 'No numerical rate here.'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                make_percentage_policy(rate(text))

    def test_nonopted_legacy_behavior_is_preserved_with_known_gap(self):
        e = rate('Growth is 1.12%.')
        self.assertEqual(percentage_policy_fields(e), {})
        self.assertEqual(validate_translation(e, '增长率为1.12。'), [])
        # This remains a disclosed legacy gap outside explicit opt-in scope.

    def test_explicit_equivalent_syntax_and_rate_point_dimension(self):
        for source, target in [('Growth is 1.12%.', '增长率为百分之1.12。'),
                               ('Growth is 1.12 per cent.', '增长率为1.12％。'),
                               ('It increased by 5 percentage points.', '增加了5个百分点。')]:
            e = dual(rate(source))
            with self.subTest(source=source):
                self.assertTrue(check_percentage_numbers(e, target)['equivalent'])

    def test_multiple_rate_reordering_requires_independent_review(self):
        e = dual(rate('A is 5% and B is 7%.'))
        issues = percentage_unit_issues(e, 'B为7%，A为5%。')
        self.assertEqual([i['code'] for i in issues], ['percentage_order_changed'])
        self.assertEqual(issues[0]['severity'], 'warning')
        self.assertTrue(review_selected(e))

    def test_body_rule_review_and_task_identity_are_policy_bound(self):
        old = quantity_rate(); new = dual(old)
        a, b = book(old), book(new)
        self.assertEqual(old.source_hash, new.source_hash)
        self.assertNotEqual(translation_rule_hash(a, old, {}), translation_rule_hash(b, new, {}))
        self.assertNotEqual(_review_hash(old, '比率1.12%。', a, {}), _review_hash(new, '比率1.12%。', b, {}))
        ta, tb = make_tasks(a, {}), make_tasks(b, {})
        self.assertNotEqual(ta[0]['id'], tb[0]['id'])
        self.assertIn(FIELD, tb[0]['payload']['elements'][0])
        self.assertFalse(_current_task(ta[0], b, {}))

    def test_frozen_body_policy_cannot_be_stripped_or_forged(self):
        e = dual(quantity_rate()); current = book(e); task = make_tasks(current, {})[0]
        for mode in ('strip', 'wrong'):
            changed = deepcopy(task)
            if mode == 'strip': changed['payload']['elements'][0].pop(FIELD)
            else: changed['payload']['elements'][0][FIELD]['parser_sha256'] = '0' * 64
            changed['source_hash'] = digest(changed['payload'])
            with self.subTest(mode=mode): self.assertFalse(_current_task(changed, current, {}))

    def test_neighbor_context_policy_updates_task_but_not_neighbor_rule(self):
        old = quantity_rate(); neighbor = Element('neighbor', 'paragraph', 'A following explanation.', 1, group_id='neighbor', chapter='one')
        a = book(old, neighbor); b = book(dual(old), deepcopy(neighbor))
        ta = make_tasks(a, {}, target_chars=1)[1]; tb = make_tasks(b, {}, target_chars=1)[1]
        self.assertEqual(translation_rule_hash(a, neighbor, {}), translation_rule_hash(b, neighbor, {}))
        self.assertNotEqual(ta['id'], tb['id'])
        self.assertTrue(tb['payload']['context_before'][0]['readonly'])
        self.assertIn(FIELD, tb['payload']['context_before'][0])
        self.assertFalse(_current_task(ta, b, {}))
        self.assertTrue(_current_task(tb, b, {}))

    def test_source_contract_reports_invalid_policy_before_work(self):
        e = dual(quantity_rate()); e.metadata[FIELD]['source_events'][0]['value'] = '100'
        codes = {v['code'] for v in source_contract_issues(book(e))}
        self.assertIn('source_percentage_policy_invalid', codes)
        self.assertIn('source_numeric_policy_invalid', codes)

    def test_stale_claim_submit_rejected_after_percentage_rule_change(self):
        old = quantity_rate(); old_book = book(old); s = self.store()
        s.register(old_book, {}); s.add_tasks(make_tasks(old_book, {})); task = s.claim('old-worker')
        other = Store(s.path); self.addCleanup(other.close)
        other.register(book(dual(old)), {})
        with self.assertRaises(ValueError):
            s.submit(task['id'], 'old-worker', task['source_hash'], {old.id: '人口为76亿，年增长率为1.12%。'})
        self.assertEqual(other.translations(), {})
        self.assertEqual(other.db.execute('SELECT count(*) FROM candidates').fetchone()[0], 0)

    def test_context_only_rule_change_rejects_stale_neighbor_submission(self):
        old = quantity_rate(); neighbor = Element('neighbor', 'paragraph', 'A following explanation.', 1, group_id='neighbor', chapter='one')
        old_book = book(old, neighbor); s = self.store(); s.register(old_book, {})
        tasks = make_tasks(old_book, {}, target_chars=1); s.add_tasks(tasks); task = s.claim('context-worker', task_id=tasks[1]['id'])
        other = Store(s.path); self.addCleanup(other.close); other.register(book(dual(old), deepcopy(neighbor)), {})
        with self.assertRaises(ValueError): s.submit(task['id'], 'context-worker', task['source_hash'], {neighbor.id: '后续说明。'})
        self.assertNotIn(neighbor.id, other.translations())

    def test_old_accepted_and_pass_are_not_current_after_optin(self):
        old = quantity_rate(); a = book(old); s = self.store(); s.register(a, {})
        translated = '人口为76亿，年增长率为1.12%。'
        self.assertTrue(s.put_candidate(old.id, translated, 'synthetic')['accepted'])
        binding = _review_binding(old, translated, a, {})
        applied = s.apply_semantic_review(old.id, translated, **{k: v for k, v in binding.items() if k != 'element_id'}, passed=True, problems=[], provider='synthetic-review')
        self.assertTrue(applied['applied'])
        old_pass = next(v for v in s.issues() if v['code'] == 'semantic_review_passed')
        new = dual(old); b = book(new); s.register(b, {})
        self.assertNotIn(old.id, s.translations())
        self.assertFalse(review_record_matches(old_pass, new, translated, b, {}))
        self.assertTrue(s.put_candidate(new.id, translated, 'synthetic-new-policy')['accepted'])
        self.assertFalse(review_record_matches(old_pass, new, translated, b, {}))
        stale = s.apply_semantic_review(old.id, translated, **{k: v for k, v in binding.items() if k != 'element_id'}, passed=False, problems=['stale result'], provider='old-review')
        self.assertFalse(stale['applied'])
        self.assertEqual(s.translations()[old.id], translated)

    def test_invalid_percentage_candidate_retained_but_not_accepted(self):
        e = dual(quantity_rate()); s = self.store(); s.register(book(e), {})
        result = s.put_candidate(e.id, '人口为76亿，年增长率为1.12。', 'synthetic-invalid')
        self.assertFalse(result['accepted'])
        self.assertNotIn(e.id, s.translations())
        self.assertEqual(s.db.execute('SELECT count(*) FROM candidates').fetchone()[0], 1)
        self.assertTrue(any(v['code'] == 'percentage_unit_mismatch' for v in s.issues()))

    def test_no_policy_metadata_returns_empty_without_hash_io(self):
        e = rate('It has 20 items.')
        with patch.object(percentage, 'implementation_sha256', side_effect=AssertionError('not required')), patch.object(percentage, 'parser_sha256', side_effect=AssertionError('not required')):
            self.assertEqual(percentage_policy_fields(e), {})
            self.assertTrue(frozen_percentage_policy_matches({'id': e.id}, e))

    def test_frozen_policy_fields_deep_copy_does_not_mutate_source(self):
        e = dual(quantity_rate()); before = asdict(e)
        fields = percentage_policy_fields(e)
        fields[FIELD]['source_events'][0]['value'] = '999'
        self.assertEqual(asdict(e), before)
        percentage_policy_fields(e)

    def test_mock_review_receives_bound_percentage_policy(self):
        e = dual(quantity_rate()); current = book(e); s = self.store(); s.register(current, {})
        self.assertTrue(s.put_candidate(e.id, '人口为76亿，年增长率为1.12%。', 'synthetic')['accepted'])
        captured = []
        def fake_chat(provider, messages):
            packet = json.loads(messages[-1]['content']); captured.append(packet)
            self.assertEqual(packet['payload']['elements'][0][FIELD], e.metadata[FIELD])
            self.assertEqual(packet['payload']['elements'][0]['numeric_policy'], e.metadata['numeric_policy'])
            return json.dumps({'task_id': packet['task_id'], 'source_hash': packet['source_hash'],
                               'reviews': {e.id: {'passed': True, 'issues': []}}}), {'prompt_tokens': 1, 'completion_tokens': 1}
        with patch('textbook_pdf.translation._chat', side_effect=fake_chat):
            result = _run_review_sequential(current, s, None, 'synthetic', _provider=Provider('synthetic', 'http://invalid', 'dummy', 'mock', attempts=1))
        self.assertEqual(len(captured), 1)
        self.assertEqual(result['review_passed'], 1)

    def test_mock_review_result_is_stale_after_concurrent_policy_source_change(self):
        e = dual(quantity_rate()); current = book(e); s = self.store(); s.register(current, {})
        translated = '人口为76亿，年增长率为1.12%。'
        self.assertTrue(s.put_candidate(e.id, translated, 'synthetic')['accepted'])
        other = Store(s.path); self.addCleanup(other.close)
        def fake_chat(provider, messages):
            packet = json.loads(messages[-1]['content'])
            revised = deepcopy(e); revised.metadata['new_semantic_source_context'] = 'v2'
            revised.metadata[FIELD] = make_percentage_policy(revised)
            revised.metadata['numeric_policy'] = make_policy(revised, chinese_classifiers=('人', '个'))
            other.register(book(revised), {})
            return json.dumps({'task_id': packet['task_id'], 'source_hash': packet['source_hash'],
                               'reviews': {e.id: {'passed': True, 'issues': []}}}), {'prompt_tokens': 1, 'completion_tokens': 1}
        with patch('textbook_pdf.translation._chat', side_effect=fake_chat):
            result = _run_review_sequential(current, s, None, 'synthetic', _provider=Provider('synthetic', 'http://invalid', 'dummy', 'mock', attempts=1))
        self.assertEqual(result['review_passed'], 0)
        self.assertEqual(result['stale_results'], 1)
        self.assertEqual(other.translations(), {})


if __name__ == "__main__":
    main(verbosity=2)
