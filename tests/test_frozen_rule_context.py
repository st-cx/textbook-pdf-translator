"""Self-authored Store fixtures / mocked model transport only."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]
import test_unit_scheduler as base
from textbook_pdf import semantic_unit_runner as runner
from textbook_pdf import translation as transport
from textbook_pdf.models import digest


class FrozenRuleTests(unittest.TestCase):
    setUp = base.UnitSchedulerTests.setUp
    tearDown = base.UnitSchedulerTests.tearDown
    has_table = base.UnitSchedulerTests.has_table
    run_fake = base.UnitSchedulerTests.run_fake

    def plan(self):
        return runner.unit_queue_snapshot(self.b, self.s)[0]

    def run_planned(self, planned, chat=None):
        config = json.loads(self.config.read_text(encoding='utf-8'))
        provider = transport._load_provider(config, 'mock')
        stop = threading.Event()
        limiter = transport._Limiter(1e9, stop)
        with patch.object(transport, '_chat', side_effect=chat) as mock:
            result = runner._run_one_unit(self.s, planned, provider, limiter, stop)
        return result, mock

    def test_actual_full_glossary_and_six_style_rules_reach_model(self):
        glossary = {'direct proof': '直接证明', 'odd integer': '奇整数'}
        self.s.register(self.b, glossary)
        result = self.run_fake()
        request = self.requests[0]
        self.assertEqual(request['frozen_rules_readonly']['glossary'], glossary)
        self.assertEqual(request['frozen_rules_readonly']['style_rules'], list(transport.STYLE_RULES))
        self.assertEqual(len(request['frozen_rules_readonly']['style_rules']), 6)
        self.assertEqual(digest(glossary), request['payload']['glossary_hash'])
        self.assertEqual(digest(list(transport.STYLE_RULES)), request['payload']['style_hash'])
        self.assertEqual(result['unit_review_passed'], 1)

    def test_wrong_glossary_preimage_rejected_before_api_or_usage(self):
        planned = self.plan(); planned['_frozen_rules_readonly']['glossary']['extra'] = '篡改'
        result, chat = self.run_planned(planned)
        chat.assert_not_called()
        self.assertEqual(result['requests'], 0)
        self.assertEqual(result['unit_rejected_results'], 1)
        self.assertEqual(result['unit_review_pending'], 1)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM usage').fetchone()[0], 0)

    def test_wrong_style_preimage_rejected_before_api(self):
        planned = self.plan(); planned['_frozen_rules_readonly']['style_rules'][0] = '篡改文体'
        result, chat = self.run_planned(planned)
        chat.assert_not_called(); self.assertEqual(result['requests'], 0)
        self.assertEqual(result['unit_rejected_results'], 1)

    def test_missing_rules_never_falls_back_to_empty_rules(self):
        planned = self.plan(); planned.pop('_frozen_rules_readonly')
        result, chat = self.run_planned(planned)
        chat.assert_not_called(); self.assertEqual(result['requests'], 0)

    def test_request_is_deep_copy_and_packet_hash_unchanged(self):
        planned = self.plan(); original = deepcopy(planned)
        request = runner._unit_request_packet(planned)
        self.assertEqual(planned, original)
        self.assertEqual(request['unit_review_hash'], planned['packet']['unit_review_hash'])
        request['frozen_rules_readonly']['glossary']['outside'] = 'request-only'
        request['payload']['elements']['left']['element']['text'] = 'changed transport object'
        self.assertEqual(planned, original)

    def test_frozen_rules_do_not_leak_into_public_status(self):
        status = runner.public_unit_status([self.plan()])
        self.assertNotIn('_frozen_rules_readonly', status['units'][0])
        self.assertNotIn('packet', status['units'][0])

    def test_glossary_change_after_queue_stales_before_request(self):
        planned = self.plan()
        with self.s.db:
            self.s.db.execute("UPDATE metadata SET value=? WHERE key='glossary'", (json.dumps({'new': '新值'}),))
        result, chat = self.run_planned(planned)
        chat.assert_not_called(); self.assertEqual(result['unit_stale_results'], 1)
        self.assertEqual(result['requests'], 0)

    def test_wrong_packet_hash_is_not_authorized_by_matching_rule_content(self):
        planned = self.plan(); planned['packet']['unit_review_hash'] = '0' * 64
        with self.assertRaises(runner._FrozenRulesError): runner._unit_request_packet(planned)

    def test_response_commit_still_rejects_inflight_member_change(self):
        def handler(packet):
            self.assertIn('frozen_rules_readonly', packet)
            self.s.put_candidate('left', '请求之后更新的成员译文', 'synthetic')
            return packet['required_response']
        result = self.run_fake(handler)
        self.assertEqual(result['unit_stale_results'], 1)
        self.assertFalse(self.has_table())

    def test_no_optin_does_not_freeze_or_read_extra_rule_context(self):
        self.b.profile.pop('semantic_review_units')
        self.s.register(self.b, {})
        with patch.object(runner, '_freeze_unit_rules', side_effect=AssertionError('must not be called')):
            self.assertEqual(runner.unit_queue_snapshot(self.b, self.s), [])


if __name__ == '__main__':
    unittest.main()
