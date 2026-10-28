"""Bounded request regression tests; synthetic fixtures, no credentials or network."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from textbook_pdf import translation as tr
from textbook_pdf.models import Asset, Book, Element
from textbook_pdf.store import Store


class RequestChunks(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / '_tmp_request_chunks'
        self.base.mkdir(exist_ok=True)
        self.directory = self.base / uuid.uuid4().hex
        self.directory.mkdir()
        self.config = self.directory / 'unused-mock-config.json'
        self.config.write_text(json.dumps({'providers': {'mock': {
            'base_url': 'http://never-contact.invalid/v1', 'api_key': 'unused-fixture',
            'model': 'mock', 'requests_per_minute': 600000, 'max_attempts': 1,
            'max_tokens': 8192}}}), encoding='utf8')
        self.stores = []

    def tearDown(self):
        for store in reversed(self.stores):
            store.close()
        self.assertTrue(self.directory.resolve().is_relative_to(self.base.resolve()))
        shutil.rmtree(self.directory)

    def config_values(self, **values):
        d = json.loads(self.config.read_text('utf8'))
        d['providers']['mock'].update(values)
        self.config.write_text(json.dumps(d), encoding='utf8')

    def fixture(self, count=7, group=None):
        f = Asset('f1', 'formula', 'unused.pdf', 1, [10, 10, 20, 20], 10, 10,
                  text='x', sha256='synthetic-bytes', metadata={'pdf_page': 0})
        b = Book('not-a-private-book.pdf', 'synthetic-source', 'Synthetic', 1, [1],
                 assets={'f1': f}, elements=[Element(f'e{i}', 'exercise',
                     f'There are {i} cases ⟦F:f1⟧.', 1, asset_ids=['f1'],
                     group_id=group(i) if group else '') for i in range(count)])
        s = Store(self.directory / f'state-{len(self.stores)}.db')
        self.stores.append(s)
        s.register(b, {})
        tasks = tr.make_tasks(b, {}, target_chars=100000)
        self.assertEqual(len(tasks), 1)
        s.add_tasks(tasks)
        return b, s, tasks[0]

    @staticmethod
    def packet(messages):
        return json.loads(messages[-1]['content'])

    @staticmethod
    def good(packet, ids=None):
        ids = packet['requested_element_ids'] if ids is None else ids
        return json.dumps({'task_id': packet['task_id'], 'source_hash': packet['source_hash'],
                           'translations': {eid: f'共有 {eid[1:]} 种情况 ⟦F:f1⟧。' for eid in ids}},
                          ensure_ascii=False), {'prompt_tokens': 100, 'completion_tokens': 50}

    def run_api(self, b, s, api, **kwargs):
        with patch.object(tr, '_chat', side_effect=api):
            return tr.run_translation(b, s, self.config, provider='mock', workers=1, **kwargs)

    def test_large_task_same_envelope_disjoint_ids_and_existing_neighbours(self):
        b, s, task = self.fixture(31)
        for i in range(3):
            s.put_candidate(f'e{i}', f'已有 {i} 种情况 ⟦F:f1⟧。', 'synthetic')
        calls = []
        def api(provider, messages):
            p = self.packet(messages)
            self.assertEqual(p['task_id'], task['id'])
            self.assertEqual(p['source_hash'], task['source_hash'])
            self.assertLessEqual(len(p['requested_element_ids']), 6)
            self.assertNotIn('protected_asset_descriptors', p['payload'])
            for e in p['payload']['elements']:
                frozen = next(e0 for e0 in task['payload']['elements'] if e0['id'] == e['id'])
                self.assertEqual(e, frozen)
            for e in p['payload']['context_before'] + p['payload']['context_after']:
                self.assertTrue(e['readonly'])
                self.assertEqual(e['source_hash'], b.element_map()[e['id']].source_hash)
            calls.append(p)
            return self.good(p)
        r = self.run_api(b, s, api)
        requested = [eid for p in calls for eid in p['requested_element_ids']]
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(requested), len(set(requested)))
        self.assertEqual(set(requested), {f'e{i}' for i in range(3, 31)})
        self.assertEqual(len(s.translations()), 31)
        self.assertEqual(r['completed'], 1)
        self.assertEqual(s.db.execute('SELECT state FROM tasks').fetchone()[0], 'done')

    def test_truncated_multi_group_splits_without_same_body_retry(self):
        self.config_values(max_attempts=4)
        b, s, task = self.fixture(7)
        calls = []
        def api(provider, messages):
            p = self.packet(messages); calls.append(p)
            if len(calls) == 1:
                return '{', {'prompt_tokens': 100, 'completion_tokens': 8192, '_truncated': True}
            return self.good(p)
        r = self.run_api(b, s, api)
        requests = [tuple(p['requested_element_ids']) for p in calls]
        self.assertEqual([len(x) for x in requests], [6, 3, 3, 1])
        self.assertEqual(requests.count(requests[0]), 1)
        self.assertEqual(r['completed'], 1)
        self.assertEqual(r['translation_truncated_calls'], 1)
        self.assertEqual(r['translation_split_packets'], 1)
        self.assertEqual(s.db.execute('SELECT count(*) FROM usage').fetchone()[0], 4)

    def test_singleton_truncates_once_and_stays_pending(self):
        self.config_values(max_attempts=4)
        b, s, _ = self.fixture(1)
        calls = []
        def api(provider, messages):
            calls.append(self.packet(messages))
            return '{', {'prompt_tokens': 100, 'completion_tokens': 8192, '_truncated': True}
        r = self.run_api(b, s, api)
        self.assertEqual(len(calls), 1)
        self.assertEqual(s.translations(), {})
        self.assertEqual(r['completed'], 0)
        self.assertEqual(r['translation_deferred_groups'], 1)
        self.assertEqual(s.db.execute('SELECT state FROM tasks').fetchone()[0], 'pending')

    def test_successful_partial_replies_do_not_consume_failure_retry_quota(self):
        b, s, _ = self.fixture(6)
        calls = []
        def api(provider, messages):
            p = self.packet(messages); calls.append(p)
            return self.good(p, p['requested_element_ids'][:1])
        r = self.run_api(b, s, api)
        self.assertEqual([len(p['requested_element_ids']) for p in calls], [6, 5, 4, 3, 2, 1])
        self.assertEqual(r['completed'], 1)
        self.assertEqual(len(s.translations()), 6)

    def test_indivisible_group_stays_together_and_truncation_does_not_split_it(self):
        self.config_values(max_tokens=512, max_attempts=4)
        b, s, _ = self.fixture(3, group=lambda i: 'one-definition' if i < 2 else '')
        calls = []
        def api(provider, messages):
            p = self.packet(messages); calls.append(p['requested_element_ids'])
            if p['requested_element_ids'] == ['e0', 'e1']:
                return '{', {'prompt_tokens': 100, 'completion_tokens': 512, '_truncated': True}
            return self.good(p)
        r = self.run_api(b, s, api)
        self.assertEqual(calls, [['e0', 'e1'], ['e2']])
        self.assertEqual(set(s.translations()), {'e2'})
        self.assertEqual(r['completed'], 0)

    def test_oversize_source_group_uses_bounded_requests_with_complete_readonly_context(self):
        b, s, task = self.fixture(7, group=lambda i: 'one-large-definition')
        calls = []
        def api(provider, messages):
            p = self.packet(messages)
            calls.append(p['requested_element_ids'])
            self.assertEqual(p['source_hash'], task['source_hash'])
            context = p['payload']['readonly_source_groups'][0]
            self.assertEqual(context['member_ids'], task['element_ids'])
            self.assertTrue(context['readonly'])
            for row, frozen in zip(context['elements'], task['payload']['elements']):
                self.assertTrue(row['readonly'])
                self.assertEqual({k: v for k, v in row.items() if k != 'readonly'}, frozen)
            return self.good(p)
        r = self.run_api(b, s, api)
        self.assertEqual(calls, [[f'e{i}' for i in range(6)], ['e6']])
        self.assertEqual(r['translation_deferred_groups'], 0)
        self.assertEqual(r['completed'], 1)
        self.assertEqual(set(s.translations()), set(task['element_ids']))

    def test_provider_budget_changes_packet_size_without_slicing_source(self):
        b, s, task = self.fixture(8)
        a, _ = tr._translation_packets(task, task['element_ids'], 8192)
        z, _ = tr._translation_packets(task, task['element_ids'], 512)
        self.assertEqual([sum(map(len, x)) for x in a], [6, 2])
        self.assertEqual([sum(map(len, x)) for x in z], [1] * 8)

    def test_request_hard_limit_preserves_completed_packets(self):
        b, s, _ = self.fixture(25)
        calls = []
        def api(provider, messages):
            p = self.packet(messages); calls.append(p)
            return self.good(p)
        with patch.object(tr, 'TRANSLATION_TASK_MAX_REQUESTS', 3):
            r = self.run_api(b, s, api)
        self.assertEqual(len(calls), 3)
        self.assertEqual(r['requests'], 3)
        self.assertEqual(len(s.translations()), 18)
        self.assertEqual(r['completed'], 0)
        self.assertTrue(any(i['code'] == 'translation_request_limit' for i in s.issues()))

    def test_same_text_changed_asset_from_second_connection_cannot_be_overwritten(self):
        b, s, _ = self.fixture(7)
        other = Store(s.db.execute('PRAGMA database_list').fetchone()[2])
        self.stores.append(other)
        calls = []
        def api(provider, messages):
            p = self.packet(messages); calls.append(p)
            newer = deepcopy(b); newer.assets['f1'].metadata['pdf_page'] = 1
            other.register(newer, {})
            other.add_tasks(tr.make_tasks(newer, {}, target_chars=100000))
            other.put_candidate('e0', '当前 0 种情况 ⟦F:f1⟧。', 'synthetic-new-revision')
            return self.good(p)
        r = self.run_api(b, s, api)
        self.assertEqual(len(calls), 1)
        self.assertEqual(other.translations(), {'e0': '当前 0 种情况 ⟦F:f1⟧。'})
        self.assertEqual(r['completed'], 0)

    def test_revoked_lease_does_not_steal_new_owner_or_accept_old_reply(self):
        b, s, task = self.fixture(7)
        other = Store(s.db.execute('PRAGMA database_list').fetchone()[2])
        self.stores.append(other)
        def api(provider, messages):
            p = self.packet(messages)
            other.db.execute('UPDATE tasks SET owner=? WHERE id=?', ('new-owner', task['id']))
            other.db.commit()
            return self.good(p)
        r = self.run_api(b, s, api)
        self.assertEqual(s.translations(), {})
        self.assertEqual(other.db.execute('SELECT owner FROM tasks WHERE id=?', (task['id'],)).fetchone()[0], 'new-owner')
        self.assertEqual(r['completed'], 0)

    def test_cancel_during_review_preserves_returned_packet_with_pending_proofs(self):
        b, s, _ = self.fixture(7)
        calls = []
        def api(provider, messages):
            p = self.packet(messages); calls.append(p)
            if 'requested_element_ids' in p:
                return self.good(p)
            raise tr._Cancelled()
        r = self.run_api(b, s, api, review_provider='mock')
        self.assertEqual(len(calls), 2)
        self.assertTrue(r['cancelled'])
        self.assertEqual(set(s.translations()), {f'e{i}' for i in range(6)})
        self.assertEqual(r['interrupted_saved_elements'], 6)
        self.assertEqual(tr.review_status(b, s)['pending'], 6)

    def test_provider_block_stops_following_packets(self):
        b, s, _ = self.fixture(13)
        calls = []
        def api(provider, messages):
            calls.append(self.packet(messages))
            raise tr._HTTPFailure(402, 0)
        r = self.run_api(b, s, api)
        self.assertEqual(len(calls), 1)
        self.assertEqual(r['provider_blocked']['http_status'], 402)
        self.assertEqual(s.translations(), {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
