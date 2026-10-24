"""A source hold must cover the complete unit, including cross-page members."""
import unittest

from textbook_pdf.models import Book, Element, source_error_scope
from textbook_pdf.translation import make_tasks


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


if __name__ == '__main__':
    unittest.main()
