"""A source hold must cover the complete unit, including cross-page members."""
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
import shutil
import uuid
from dataclasses import asdict

from textbook_pdf.models import Book, Element, source_error_scope, digest
from textbook_pdf.translation import make_tasks, _current_task, run_translation


class SourceErrorScopeTests(unittest.TestCase):
    def book(self, issues):
        return Book('synthetic.pdf', 'synthetic-sha', 'Synthetic', 2, [1, 2], elements=[
            Element('parent', 'exercise', '1. State the condition.', 1, group_id='question'),
            Element('tail', 'exercise', 'Then apply the condition.', 2, group_id='question'),
            Element('next', 'exercise', '2. Give another example.', 2, group_id='next'),
        ], issues=issues)

    def test_plural_hold_blocks_all_body_and_readonly_members(self):
        book = self.book([{'code': 'source_boundary_hold', 'severity': 'error',
            'page': 1, 'element_id': 'parent', 'element_ids': ['parent', 'tail']}])
        tasks = make_tasks(book, {})
        self.assertEqual([eid for task in tasks for eid in task['element_ids']], ['next'])
        for task in tasks:
            payload = task['payload']
            self.assertFalse({'parent', 'tail'} & {row['id'] for row in
                payload['elements'] + payload['context_before'] + payload['context_after']})

    def test_plural_scope_without_anchor_keeps_unrelated_same_page(self):
        book = self.book([{'severity': 'error', 'page': 2, 'element_ids': ['tail']}])
        body = {eid for task in make_tasks(book, {}) for eid in task['element_ids']}
        self.assertEqual(body, {'parent', 'next'})

    def test_singular_and_page_only_legacy_scopes(self):
        ids, pages = source_error_scope(self.book([
            {'severity': 'error', 'element_id': 'parent', 'page': 1},
            {'severity': 'error', 'page': 2}]))
        self.assertEqual(ids, {'parent'})
        self.assertEqual(pages, {2})

    def test_unknown_owner_falls_back_to_source_page(self):
        book = self.book([{'severity': 'error', 'element_ids': ['missing'], 'page': 2}])
        self.assertEqual(source_error_scope(book), ({'missing'}, {2}))
        self.assertEqual([eid for t in make_tasks(book, {}) for eid in t['element_ids']], ['parent'])

    def test_global_error_blocks_all_selected_pages(self):
        self.assertEqual(make_tasks(self.book([{'severity': 'error'}]), {}), [])

    def test_malformed_member_scope_is_rejected_before_request_creation(self):
        for members in ('parent', [''], [None], [True]):
            with self.subTest(members=members):
                with self.assertRaisesRegex(ValueError, 'Malformed source issue'):
                    make_tasks(self.book([{'severity': 'error', 'element_ids': members}]), {})

    def test_invalid_page_is_rejected(self):
        for page in (True, 0, 3, '2'):
            with self.subTest(page=page):
                with self.assertRaisesRegex(ValueError, 'Malformed source issue page'):
                    make_tasks(self.book([{'severity': 'error', 'page': page}]), {})

    def test_warning_does_not_block_translation(self):
        book = self.book([{'severity': 'warning', 'element_ids': ['parent', 'tail'], 'page': 1}])
        self.assertEqual({eid for t in make_tasks(book, {}) for eid in t['element_ids']}, {'parent', 'tail', 'next'})

    def archived(self, element, target=None):
        element.text = ''
        element.metadata.update(render_suppressed=True, translatable=False)
        if target is not None:
            element.metadata['superseded_by'] = target

    def test_archived_owner_redirect_blocks_current_keeper_and_readonly(self):
        book = self.book([{'severity': 'error', 'element_id': 'parent', 'page': 1}])
        self.archived(book.elements[0], 'tail')
        self.assertEqual(source_error_scope(book), ({'parent', 'tail'}, set()))
        tasks = make_tasks(book, {})
        self.assertEqual([eid for t in tasks for eid in t['element_ids']], ['next'])
        self.assertTrue(all('tail' not in {r['id'] for r in t['payload']['context_before']} for t in tasks))

    def test_archived_crosspage_chain_follows_live_owner(self):
        book = self.book([{'severity': 'error', 'element_id': 'parent', 'page': 1}])
        middle = Element('middle', 'paragraph', '', 1)
        self.archived(book.elements[0], 'middle')
        self.archived(middle, 'tail')
        book.elements.insert(1, middle)
        self.assertEqual(source_error_scope(book), ({'parent', 'middle', 'tail'}, set()))

    def test_unknown_archive_destination_falls_back_to_its_actual_page(self):
        book = self.book([{'severity': 'error', 'element_id': 'parent'}])
        self.archived(book.elements[0])
        self.assertEqual(source_error_scope(book), ({'parent'}, {1}))

    def test_declared_complete_source_archive_contracts_follow_the_keeper(self):
        for field, target_key in [('exact_original_source_fragment_archive_v1', 'current_unit_owner'),
                                  ('complete_math_source_owner_archive_v1', 'moved_to_owner')]:
            with self.subTest(field=field):
                book = self.book([{'severity': 'error', 'element_id': 'parent', 'page': 1}])
                self.archived(book.elements[0])
                book.elements[0].metadata[field] = {target_key: 'tail'}
                self.assertEqual(source_error_scope(book), ({'parent', 'tail'}, set()))

    def test_malformed_or_cyclic_archive_destinations_fail_before_task_creation(self):
        for target in ('missing', 'parent', ['tail'], True):
            with self.subTest(target=target):
                book = self.book([{'severity': 'error', 'element_id': 'parent', 'page': 1}])
                self.archived(book.elements[0], target)
                with self.assertRaisesRegex(ValueError, 'Malformed source archived'):
                    make_tasks(book, {})

    def test_new_source_hold_invalidates_an_existing_body_packet(self):
        book = self.book([])
        task = make_tasks(book, {})[0]
        self.assertTrue(_current_task(task, book, {}))
        book.issues.append({'severity': 'error', 'element_id': 'tail', 'page': 2})
        self.assertFalse(_current_task(task, book, {}))

    def test_new_source_hold_invalidates_existing_readonly_context(self):
        book = self.book([])
        task = next(t for t in make_tasks(book, {}, target_chars=1) if t['element_ids'] == ['next'])
        self.assertIn('parent', {r['id'] for r in task['payload']['context_before']})
        book.issues.append({'severity': 'error', 'element_id': 'parent', 'page': 1})
        self.assertFalse(_current_task(task, book, {}))

    def test_distant_readonly_argument_parent_source_hold_blocks_its_packets(self):
        from argument_fixture import make_fixture
        root = Path(__file__).resolve().parent / '_tmp_source_error_scope'
        directory = root / uuid.uuid4().hex
        try:
            book, _ = make_fixture(directory)
            for e in book.elements:
                e.group_id = e.id
            unit = book.profile['argument_units_v1']['self-argument']
            unit['parent_full_source_sha256'] = digest(asdict(book.elements[0]))
            unit['member_source_full_sha256'] = {
                e.id: digest(asdict(e)) for e in book.elements if e.id in unit['member_ids']}
            task = next(t for t in make_tasks(book, {}, target_chars=1) if t['element_ids'] == ['c'])
            self.assertNotIn('parent', {r['id'] for r in task['payload']['context_before']})
            self.assertTrue(_current_task(task, book, {}))
            book.issues.append({'severity': 'error', 'element_id': 'parent', 'page': 1})
            self.assertFalse(_current_task(task, book, {}))
            self.assertEqual(make_tasks(book, {}, target_chars=1), [])
        finally:
            if directory.exists():
                self.assertTrue(directory.resolve().is_relative_to(root.resolve()))
                shutil.rmtree(directory)

    def test_explicit_stale_source_task_is_rejected_before_config_claim_or_network(self):
        book = self.book([])
        task = make_tasks(book, {})[0]
        book.issues.append({'severity': 'error', 'element_id': 'tail', 'page': 2})
        store = Mock()
        store.glossary = {}
        store.pending_tasks.return_value = [task]
        store.translations.return_value = {}
        with patch('textbook_pdf.translation._chat') as chat:
            with self.assertRaisesRegex(ValueError, 'not current pending work'):
                run_translation(book, store, 'unused-synthetic-config.json', task_ids=[task['id']])
        store.claim.assert_not_called()
        chat.assert_not_called()


if __name__ == '__main__':
    unittest.main()
