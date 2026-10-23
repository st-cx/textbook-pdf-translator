"""Synthetic transport/Store integration; no real provider or job database."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path[:0] = [str(HERE), str(ROOT)]
import test_semantic_units as fixtures
from textbook_pdf import translation as t
from textbook_pdf.models import Element, Book, digest
from textbook_pdf.store import Store
from textbook_pdf.semantic_units import UnitReviewStore, PROFILE_KEY
from textbook_pdf.semantic_unit_runner import unit_queue_snapshot, _run_one_unit, _apply_current_result
from textbook_pdf.numeric_policy import make_policy
from textbook_pdf.cli import main as cli_main, _review_run_options


class UnitSchedulerTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnitStoreTests.setUp(self)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'default_provider': 'mock', 'providers': {'mock': {
            'base_url': 'https://invalid.test/v1', 'api_key': 'synthetic-not-a-secret', 'model': 'mock-model',
            'max_attempts': 1, 'requests_per_minute': 1e9, 'price_in': 1, 'price_out': 2}}}), encoding='utf8')
        self.requests = []

    def tearDown(self):
        fixtures.UnitStoreTests.tearDown(self)

    def has_table(self):
        return fixtures.UnitStoreTests.has_table(self)

    def run_fake(self, handler=None, **kwargs):
        kwargs.setdefault('max_elements',0)  # Unit-focused cases leave ordinary risk review for its own tests.
        def chat(provider, messages):
            packet = json.loads(messages[-1]['content'])
            self.requests.append(packet)
            response = handler(packet) if handler else packet['required_response']
            return json.dumps(response, ensure_ascii=False), {'prompt_tokens': 20, 'completion_tokens': 10}
        with patch.object(t, '_chat', side_effect=chat):
            return t.run_review(self.b, self.s, self.config, 'mock', workers=1, **kwargs)

    def add_ordinary(self):
        e = Element('ordinary', 'exercise', 'Show 7.', 3)
        self.b.elements.append(e)
        self.s.register(self.b, {})
        self.assertTrue(self.s.put_candidate(e.id, '说明7。', 'synthetic')['accepted'])

    def verdict(self, packet, passed=False):
        response = deepcopy(packet['required_response'])
        response.update(passed=passed, issues=[] if passed else ['Whole-unit meaning is incorrect'])
        response['boundary_coverage']['no_omission_or_duplicate'] = passed
        return response

    def test_unit_is_dispatched_and_applied_with_no_individual_pass(self):
        result = self.run_fake()
        self.assertEqual(result['unit_review_passed'], 1)
        self.assertEqual(result['review_passed'], 0)
        self.assertEqual(result['semantic_units_after']['counts']['passed'], 1)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM issues WHERE json_extract(json,'$.code')='semantic_review_passed'").fetchone()[0], 0)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM semantic_unit_reviews').fetchone()[0], 1)
        self.assertEqual(self.requests[0]['unit_id'], self.u['id'])

    def test_complete_packet_keeps_both_original_pages_and_readonly_context(self):
        self.run_fake()
        p = self.requests[0]['payload']
        self.assertEqual([f['physical_page'] for f in p['source_fragments']], [1, 2])
        self.assertIn('before', p['elements'])
        self.assertIn('after', p['elements'])
        self.assertEqual(p['ordered_member_ids'], ['left', 'right'])

    def test_failed_current_unit_is_visible_but_not_automatically_recharged(self):
        first = self.run_fake(lambda p: self.verdict(p))
        self.assertEqual(first['unit_review_failed'], 1)
        before = len(self.requests)
        second = self.run_fake()
        self.assertEqual(len(self.requests), before)
        self.assertEqual(second['unit_scheduled'], 0)
        self.assertEqual(second['semantic_units_after']['counts']['failed'], 1)

    def test_explicit_id_can_reassess_current_failed_unit(self):
        self.run_fake(lambda p: self.verdict(p))
        result = self.run_fake(unit_ids=[self.u['id']])
        self.assertEqual(result['unit_review_passed'], 1)
        self.assertEqual(result['semantic_units_after']['counts']['passed'], 1)

    def test_new_member_revision_becomes_pending_after_old_failed_unit(self):
        self.run_fake(lambda p: self.verdict(p))
        self.s.put_candidate('left', '更新的把一种语言译成另一种语言的研究', 'synthetic')
        result = self.run_fake()
        self.assertEqual(result['unit_review_passed'], 1)
        self.assertEqual(len(self.requests), 2)

    def test_explicit_id_never_runs_unrelated_ordinary_paid_queue(self):
        self.add_ordinary()
        result = self.run_fake(unit_ids=[self.u['id']])
        self.assertEqual(len(self.requests), 1)
        self.assertIn('unit_id', self.requests[0])
        self.assertEqual(result['review_passed'], 0)
        self.assertTrue(result['ordinary_review_suppressed_for_explicit_unit_ids'])

    def test_unknown_unit_rejected_before_provider_or_request(self):
        with patch.object(t, '_chat') as chat:
            with self.assertRaises(ValueError):
                t.run_review(self.b, self.s, 'no-config-file', 'mock', unit_ids=['unknown'])
            chat.assert_not_called()

    def test_missing_translation_visible_as_awaiting_not_fake_pending_complete(self):
        self.s.redo_element('left')
        result = self.run_fake()
        self.assertEqual(result['unit_scheduled'], 0)
        self.assertEqual(result['semantic_units_after']['counts']['awaiting_translation'], 1)
        self.assertEqual(self.requests, [])
        status = t.review_status(self.b, self.s)
        self.assertTrue({'left', 'right'} <= set(status['ids']['awaiting_translation']))
        self.assertFalse(status['coverage_complete'])

    def test_actual_asset_change_visible_unverifiable_without_requests(self):
        (self.root / 'native.bin').write_bytes(b'tampered')
        result = self.run_fake()
        self.assertEqual(result['unit_scheduled'], 0)
        self.assertEqual(result['semantic_units_after']['counts']['unverifiable'], 1)
        self.assertEqual(self.requests, [])

    def test_max_units_zero_leaves_unit_pending_without_loading_provider(self):
        result = t.run_review(self.b, self.s, 'no-config-file', 'mock', max_units=0, max_elements=0, workers=1)
        self.assertEqual(result['unit_scheduled'], 0)
        self.assertEqual(result['semantic_units_after']['counts']['pending'], 1)

    def test_max_units_invalid_rejected(self):
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                t.run_review(self.b, self.s, 'no-config-file', 'mock', max_units=value)

    def test_unit_timeout_keeps_pending_without_fabricated_failed_verdict(self):
        with patch.object(t, '_chat', side_effect=ConnectionError('synthetic timeout')):
            result = t.run_review(self.b, self.s, self.config, 'mock', max_elements=0, workers=1)
        self.assertEqual(result['requests'], 1)
        self.assertEqual(result['failed_requests'], 1)
        self.assertEqual(result['unit_review_pending'], 1)
        self.assertFalse(self.has_table())
        self.assertEqual(result['semantic_units_after']['counts']['pending'], 1)

    def test_truncation_does_not_split_or_identically_retry_complete_unit(self):
        config = json.loads(self.config.read_text('utf8'))
        config['providers']['mock']['max_attempts'] = 4
        self.config.write_text(json.dumps(config), encoding='utf8')
        with patch.object(t, '_chat', return_value=('{}', {'prompt_tokens': 20, 'completion_tokens': 10, '_truncated': True})) as chat:
            result = t.run_review(self.b, self.s, self.config, 'mock', max_elements=0, workers=1)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(result['unit_review_truncated_calls'], 1)
        self.assertEqual(result['unit_review_pending'], 1)
        self.assertFalse(self.has_table())

    def test_reply_cannot_smuggle_translation_patch(self):
        def handler(p):
            response = deepcopy(p['required_response'])
            response['translations'] = {'right': '错误补译'}
            return response
        result = self.run_fake(handler)
        self.assertEqual(result['failed_requests'], 1)
        self.assertFalse(self.has_table())
        self.assertEqual(self.s.translations(accepted_only=False)['right'], self.trans['right'])

    def test_wrong_whole_unit_hash_is_rejected(self):
        def handler(p):
            response = deepcopy(p['required_response'])
            response['unit_review_hash'] = '0' * 64
            return response
        result = self.run_fake(handler)
        self.assertEqual(result['failed_requests'], 1)
        self.assertFalse(self.has_table())

    def test_duplicate_json_keys_rejected(self):
        with patch.object(t, '_chat', return_value=('{"unit_id":"wrong","unit_id":"same"}', {'prompt_tokens': 2, 'completion_tokens': 2})):
            result = t.run_review(self.b, self.s, self.config, 'mock', max_elements=0, workers=1)
        self.assertEqual(result['failed_requests'], 1)
        self.assertFalse(self.has_table())

    def test_402_stops_units_and_ordinary_before_more_requests(self):
        self.add_ordinary()
        with patch.object(t, '_chat', side_effect=t._HTTPFailure(402)) as chat:
            result = t.run_review(self.b, self.s, self.config, 'mock', workers=1)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(result['provider_blocked']['http_status'], 402)
        self.assertEqual(result['unit_review_pending'], 1)
        self.assertGreaterEqual(result['review_pending'], 1)
        self.assertFalse(self.has_table())

    def test_401_and_403_also_stop_shared_queue(self):
        for status in (401, 403):
            with self.subTest(status=status), patch.object(t, '_chat', side_effect=t._HTTPFailure(status)) as chat:
                result = t.run_review(self.b, self.s, self.config, 'mock', max_elements=0, workers=1)
                self.assertEqual(chat.call_count, 1)
                self.assertEqual(result['provider_blocked']['http_status'], status)

    def test_mixed_review_reuses_one_provider_limiter_and_accounts_usage(self):
        self.add_ordinary()
        real_limiter = t._Limiter
        seen = []
        def construct(*args):
            limiter = real_limiter(*args)
            seen.append(limiter)
            return limiter
        def chat(provider, messages):
            packet = json.loads(messages[-1]['content'])
            self.requests.append(packet)
            return json.dumps(packet['required_response'], ensure_ascii=False), {'prompt_tokens': 20, 'completion_tokens': 10}
        with patch.object(t, '_Limiter', side_effect=construct), patch.object(t, '_chat', side_effect=chat):
            result = t.run_review(self.b, self.s, self.config, 'mock', workers=2)
        self.assertEqual(len(seen), 1)
        self.assertEqual(result['unit_review_passed'], 1)
        self.assertEqual(result['review_passed'], 2)  # Explicit ordinary exercise plus original formula context.
        self.assertEqual(result['requests'], 2)
        self.assertEqual((result['input_tokens'], result['output_tokens']), (40, 20))
        self.assertAlmostEqual(result['estimated_cost'], 0.00008)
        usage = [json.loads(row[0]) for row in self.s.db.execute('SELECT json FROM usage')]
        self.assertEqual({u['stage'] for u in usage}, {'review', 'review_unit'})

    def test_translation_changes_inflight_unit_result_is_stale(self):
        def handler(p):
            self.s.put_candidate('left', '请求后更新的译文', 'synthetic')
            return p['required_response']
        result = self.run_fake(handler)
        self.assertEqual(result['unit_stale_results'], 1)
        self.assertFalse(self.has_table())
        self.assertEqual(self.s.translations()['left'], '请求后更新的译文')

    def test_context_changes_inflight_unit_result_is_stale(self):
        def handler(p):
            self.s.put_candidate('after', '请求后更新的上下文译文。', 'synthetic')
            return p['required_response']
        result = self.run_fake(handler)
        self.assertEqual(result['unit_stale_results'], 1)
        self.assertFalse(self.has_table())

    def test_queued_dependency_change_is_rejected_before_chat(self):
        planned = unit_queue_snapshot(self.b, self.s)[0]
        self.s.put_candidate('left', '排队期间更新', 'synthetic')
        provider = t._load_provider(json.loads(self.config.read_text('utf8')), 'mock')
        stop = threading.Event()
        with patch.object(t, '_chat') as chat:
            result = _run_one_unit(self.s, planned, provider, t._Limiter(1e9, stop), stop)
            chat.assert_not_called()
        self.assertEqual(result['unit_stale_results'], 1)

    def test_queued_asset_tamper_costs_no_api_and_no_failure_usage(self):
        planned = unit_queue_snapshot(self.b, self.s)[0]
        (self.root / 'native.bin').write_bytes(b'changed while queued')
        provider = t._load_provider(json.loads(self.config.read_text('utf8')), 'mock')
        stop = threading.Event()
        with patch.object(t, '_chat') as chat:
            result = _run_one_unit(self.s, planned, provider, t._Limiter(1e9, stop), stop)
            chat.assert_not_called()
        self.assertEqual((result['requests'], result['failed_requests'], result['usage_missing_calls']), (0, 0, 0))
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM usage').fetchone()[0], 0)

    def test_dependency_change_during_limiter_wait_rechecked_before_chat(self):
        planned = unit_queue_snapshot(self.b, self.s)[0]
        provider = t._load_provider(json.loads(self.config.read_text('utf8')), 'mock')
        stop = threading.Event()
        limiter = t._Limiter(1e9, stop)
        with patch.object(limiter, 'acquire', side_effect=lambda: self.s.put_candidate('after', '等待限流时改变上下文', 'synthetic')), patch.object(t, '_chat') as chat:
            result = _run_one_unit(self.s, planned, provider, limiter, stop)
            chat.assert_not_called()
        self.assertEqual(result['unit_stale_results'], 1)
        self.assertEqual(result['requests'], 0)

    def test_asset_file_changes_inflight_stales_whole_unit(self):
        def handler(p):
            (self.root / 'native.bin').write_bytes(b'changed after request')
            return p['required_response']
        result = self.run_fake(handler)
        self.assertEqual(result['unit_stale_results'], 1)
        self.assertFalse(self.has_table())

    def test_cancelled_queue_never_calls_model_or_writes_verdict(self):
        planned = unit_queue_snapshot(self.b, self.s)[0]
        provider = t._load_provider(json.loads(self.config.read_text('utf8')), 'mock')
        stop = threading.Event()
        stop.set()
        with patch.object(t, '_chat') as chat:
            result = _run_one_unit(self.s, planned, provider, t._Limiter(1e9, stop), stop)
            chat.assert_not_called()
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['unit_review_pending'], 1)
        self.assertFalse(self.has_table())

    def test_timeout_retries_within_existing_attempt_budget_then_saves_current_unit(self):
        config = json.loads(self.config.read_text('utf8'))
        config['providers']['mock']['max_attempts'] = 2
        self.config.write_text(json.dumps(config), encoding='utf8')
        packets = []
        def chat(provider, messages):
            packet = json.loads(messages[-1]['content'])
            packets.append(packet)
            if len(packets) == 1:
                raise ConnectionError('synthetic timeout')
            return json.dumps(packet['required_response']), {'prompt_tokens': 20, 'completion_tokens': 10}
        with patch.object(t, '_chat', side_effect=chat):
            result = t.run_review(self.b, self.s, self.config, 'mock', max_elements=0, workers=1)
        self.assertEqual(result['requests'], 2)
        self.assertEqual(result['failed_requests'], 1)
        self.assertEqual(result['unit_review_passed'], 1)
        self.assertFalse(result['cost_estimate_complete'])
        self.assertEqual(packets[0], packets[1])

    def test_retryable_http_429_throttles_existing_shared_limiter(self):
        planned = unit_queue_snapshot(self.b, self.s)[0]
        config = json.loads(self.config.read_text('utf8'))
        config['providers']['mock']['max_attempts'] = 2
        provider = t._load_provider(config, 'mock')
        stop = threading.Event()
        limiter = t._Limiter(1e9, stop)
        success = (json.dumps(planned['packet']['required_response']), {'prompt_tokens': 20, 'completion_tokens': 10})
        with patch.object(t, '_chat', side_effect=[t._HTTPFailure(429, 0), success]), patch.object(limiter, 'throttle', wraps=limiter.throttle) as throttle:
            result = _run_one_unit(self.s, planned, provider, limiter, stop)
        throttle.assert_called_once_with(0)
        self.assertEqual(result['unit_review_passed'], 1)
        self.assertEqual(result['requests'], 2)

    def test_unknown_usage_is_reported_as_unknown_cost_not_free(self):
        def chat(provider, messages):
            packet = json.loads(messages[-1]['content'])
            return json.dumps(packet['required_response']), {}
        with patch.object(t, '_chat', side_effect=chat):
            result = t.run_review(self.b, self.s, self.config, 'mock', max_elements=0, workers=1)
        self.assertEqual(result['unit_review_passed'], 1)
        self.assertEqual(result['usage_missing_calls'], 1)
        self.assertFalse(result['cost_estimate_complete'])

    def test_new_unauthorized_failure_rejects_result_separately_from_stale(self):
        self.s.record_issue({'code': 'semantic_review_failed', 'severity': 'error', 'element_id': 'right', 'page': 2,
            'source_hash': self.packet['payload']['elements']['right']['source_hash'],
            'translation_hash': digest(self.trans['right']), 'detail': 'Additional current failure not authorized by the unit declaration'})
        result = self.run_fake()
        self.assertEqual(result['unit_rejected_results'], 1)
        self.assertEqual(result['unit_stale_results'], 0)
        self.assertEqual(result['unit_review_pending'], 1)
        self.assertFalse(self.has_table())

    def test_current_passed_unit_does_not_automatically_request_again(self):
        self.run_fake()
        count = len(self.requests)
        result = self.run_fake()
        self.assertEqual(len(self.requests), count)
        self.assertEqual(result['unit_scheduled'], 0)
        self.assertEqual(result['semantic_units_after']['counts']['passed'], 1)

    def test_other_committed_result_cannot_be_overwritten_by_late_response(self):
        def handler(p):
            original_packet = deepcopy(p)
            original_packet.pop('frozen_rules_readonly', None)  # Transport-only preimages are not a Store packet edit.
            self.assertTrue(self.facade.apply(original_packet, p['required_response'], provider='other')['applied'])
            return self.verdict(p, False)
        result = self.run_fake(handler)
        self.assertEqual(result['unit_stale_results'], 1)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM semantic_unit_reviews').fetchone()[0], 1)
        self.assertEqual(self.facade.status(self.u['id'])['state'], 'passed')

    def test_duplicate_scheduler_submission_is_idempotent_at_result_version_cas(self):
        planned = unit_queue_snapshot(self.b, self.s)[0]
        one = _apply_current_result(self.s, planned, planned['packet']['required_response'], 'first')
        two = _apply_current_result(self.s, planned, planned['packet']['required_response'], 'late-duplicate')
        self.assertTrue(one['applied'])
        self.assertEqual(one['commit_state'], 'committed_when_returned')
        self.assertFalse(two['applied'])
        self.assertEqual(two['reason'], 'another_unit_result_committed')
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM semantic_unit_reviews').fetchone()[0], 1)

    def test_two_concurrent_schedulers_save_only_one_unit_result(self):
        barrier = threading.Barrier(2)
        def chat(provider, messages):
            packet = json.loads(messages[-1]['content'])
            barrier.wait(timeout=5)
            return json.dumps(packet['required_response']), {'prompt_tokens': 20, 'completion_tokens': 10}
        with patch.object(t, '_chat', side_effect=chat), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(t.run_review, self.b, self.s, self.config, 'mock', max_elements=0, workers=1) for _ in range(2)]
            reports = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(r['unit_review_passed'] for r in reports), 1)
        self.assertEqual(sum(r['unit_stale_results'] for r in reports), 1)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM semantic_unit_reviews').fetchone()[0], 1)

    def test_api_scheduler_rejects_caller_owned_outer_transaction(self):
        with self.s._atomic_write(), patch.object(t, '_chat') as chat:
            with self.assertRaisesRegex(ValueError, 'caller transaction'):
                t.run_review(self.b, self.s, self.config, 'mock', max_elements=0, workers=1)
            chat.assert_not_called()

    def test_unit_application_fault_does_not_create_success_or_result(self):
        with patch.object(UnitReviewStore, 'apply', side_effect=RuntimeError('synthetic apply fault')):
            result = self.run_fake()
        self.assertEqual(result['unit_review_passed'], 0)
        self.assertEqual(result['worker_failures'], 1)
        self.assertFalse(self.has_table())

    def test_optin_numeric_policy_is_in_actual_joint_request_and_review_identity(self):
        left, right = self.b.element_map()['left'], self.b.element_map()['right']
        left.text = 'More than 2.9 million vertices are linked'
        right.text = 'in this network.'
        left.metadata['numeric_policy'] = make_policy(left)
        self.u['boundary'].update(left_suffix='are linked', right_prefix='in this network')
        self.u['member_source_full_sha256'] = {e.id: digest(asdict(e)) for e in (left, right)}
        self.s.register(self.b, {})
        self.s.put_candidate('left', '超过290万个顶点相互连接', 'synthetic')
        self.s.put_candidate('right', '在这个网络中。', 'synthetic')
        result = self.run_fake()
        self.assertEqual(result['unit_review_passed'], 1)
        request = self.requests[0]
        self.assertIn('numeric_policy', request['payload']['elements']['left']['element']['metadata'])
        self.assertNotEqual(request['unit_review_hash'], self.packet['unit_review_hash'])
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM issues WHERE json_extract(json,'$.code')='semantic_review_passed'").fetchone()[0], 0)

    def test_old_whole_unit_verdict_cannot_survive_new_numeric_policy(self):
        self.facade.apply(self.packet, self.response)
        left = self.b.element_map()['left']
        left.text = 'More than 2.9 million vertices are linked'
        left.metadata['numeric_policy'] = make_policy(left)
        self.s.register(self.b, {})
        self.assertNotEqual(self.facade.status(self.u['id'])['state'], 'passed')
        self.assertFalse(self.facade.apply(self.packet, self.response)['applied'])

    def test_same_page_theorem_is_not_silently_supported(self):
        self.b.element_map()['right'].page = 1
        self.u['member_source_full_sha256']['right'] = digest(asdict(self.b.element_map()['right']))
        self.s.register(self.b, {})
        result = self.run_fake()
        self.assertEqual(result['unit_scheduled'], 0)
        self.assertEqual(result['semantic_units_after']['counts']['unverifiable'], 1)


class DefaultAndCliCompatibilityTests(unittest.TestCase):
    def test_default_cli_dispatch_adds_no_arguments(self):
        self.assertEqual(_review_run_options(SimpleNamespace(review_unit_id=None, max_review_units=None)), {})

    def test_explicit_cli_dispatch_names_match_scheduler(self):
        self.assertEqual(_review_run_options(SimpleNamespace(review_unit_id=['unit'], max_review_units=1)), {'unit_ids': ['unit'], 'max_units': 1})

    def test_cli_help_exposes_joint_status_recovery_controls(self):
        output = io.StringIO()
        with patch('sys.stdout', output), self.assertRaises(SystemExit) as end:
            cli_main(['--help'])
        self.assertEqual(end.exception.code, 0)
        self.assertIn('--review-unit-id', output.getvalue())
        self.assertIn('--max-review-units', output.getvalue())

    def test_joint_options_cannot_silently_trigger_translate_stage(self):
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as end:
            cli_main(['--stage', 'translate', '--review-unit-id', 'unit'])
        self.assertEqual(end.exception.code, 2)



if __name__ == '__main__':
    unittest.main()
