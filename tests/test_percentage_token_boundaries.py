"""Self-authored complete-token regressions for the explicit rate policy."""
from copy import deepcopy
from pathlib import Path
from unittest import TestCase
import hashlib
import tempfile
import uuid

from textbook_pdf.models import Book, Element, validate_translation
from textbook_pdf.store import Store
from textbook_pdf.percentage_policy import (FIELD, VERSION, make_percentage_policy,
    percentage_policy_fields, check_percentage_numbers)
from textbook_pdf.translation import make_tasks
import textbook_pdf.numeric_policy as numeric


def element(text):
    return Element('signed-rate', 'paragraph', text, 1, [0, 0, 100, 10], group_id='rate')


def optin(text):
    e = element(text); e.metadata[FIELD] = make_percentage_policy(e)
    return e


def book(e):
    return Book('', 'self-authored-sign-original', 'Synthetic signs', 1, [1], elements=[e])


class PercentageTokenBoundaryTests(TestCase):
    def test_actual_repeated_mixed_sign_regressions_rejected_in_Store(self):
        for source, target in [('A rate is -1.12%.', '比率为--1.12%。'),
                               ('A rate is +1.12%.', '比率为-+1.12%。')]:
            with self.subTest(source=source):
                e = optin(source); b = book(e)
                path = Path(tempfile.gettempdir()) / ('sign-policy-fixture-' + uuid.uuid4().hex)
                path.mkdir()
                with Store(path / 'tasks.db') as s:
                    s.register(b, {})
                    result = s.put_candidate(e.id, target, 'self-authored-negative')
                    self.assertFalse(result['accepted'])
                    self.assertNotIn(e.id, s.translations())
                    self.assertIn('percentage_unit_mismatch', {i['code'] for i in result['issues']})
                    self.assertEqual(s.db.execute('SELECT count(*) FROM candidates').fetchone()[0], 1)

    def test_single_explicit_plus_minus_signs_remain_valid(self):
        for source, target in [('A rate is -1.12%.', '比率为−1.12%。'),
                               ('A rate is −1.12%.', '比率为-1.12%。'),
                               ('A rate is +1.12%.', '比率为1.12%。'),
                               ('A rate is +2.00%.', '比率为+2.00%。')]:
            with self.subTest(source=source):
                e = optin(source)
                self.assertTrue(check_percentage_numbers(e, target)['equivalent'])
                self.assertEqual(validate_translation(e, target, book(e)), [])

    def test_fraction_ratio_source_cannot_produce_bound_policy_task(self):
        for raw in ('1/2%', '1:2%', '1 / 2%', '1：2%', '1∕2%', '1⁄2%', '1\\2%', 'x/2%', 'x:2%'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                e = element('A rate is ' + raw + '.')
                e.metadata[FIELD] = make_percentage_policy(e)
                make_tasks(book(e), {})

    def test_malformed_target_signs_are_not_restarted_at_inner_decimal(self):
        e = optin('A rate is -1.12%.')
        for raw in ('--1.12%', '+-1.12%', '-+1.12%', '++1.12%', '−-1.12%',
                    '＋-1.12%', '- -1.12%', '+ +1.12%', '- 1.12%'):
            with self.subTest(raw=raw):
                result = check_percentage_numbers(e, '比率为' + raw + '。')
                self.assertFalse(result['supported'])
                self.assertFalse(result['equivalent'])
                self.assertIn('percentage_unit_mismatch', {i['code'] for i in validate_translation(e, '比率为' + raw + '。', book(e))})

    def test_other_partial_expression_heads_and_tails_stay_unverifiable(self):
        for raw in ('1−2%', '1＋2%', '1e-2%', '1,,234%', '1 2%', '1.12%/3',
                    '(1.12%)/2', '+(1.12%)', 'f(1.12%)', '1.12%+2%', '1.12%−2%',
                    '百分之1/2', '百分之1:2', '百分之1e2'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                make_percentage_policy(element('A rate is ' + raw + '.'))

    def test_ordinary_prose_punctuation_and_parentheses_remain_supported(self):
        for source, target in [('Growth: 1.12%.', '增长率：1.12%。'),
                               ('A rate (1.12%).', '比率（1.12%）。'),
                               ('Rates are 1.12%, 2.00%.', '比率为1.12%和2.00%。')]:
            with self.subTest(source=source):
                e = optin(source)
                self.assertTrue(check_percentage_numbers(e, target)['equivalent'])

    def test_v1_policy_cannot_be_reinterpreted_under_v2(self):
        e = optin('A rate is 1.12%.')
        old = deepcopy(e)
        old.metadata[FIELD]['schema'] = 'explicit-arabic-percent-units-v1'
        old.metadata[FIELD]['implementation_sha256'] = '9782e8b1d074ca2ad929b8e6204c95400e4212e03bfc881cb2eeb6c6a53e9b00'
        old.metadata[FIELD]['parser_sha256'] = '8c3ef09ef8e4376a7e5e43a2672ece5c8751d8ca1a1bc3ce434b4bcb595fcd57'
        with self.assertRaises(ValueError): percentage_policy_fields(old)
        with self.assertRaises(ValueError): make_tasks(book(old), {})
        self.assertEqual(VERSION, 'explicit-arabic-percent-units-v4')

    def test_quantity_b79_is_unchanged(self):
        self.assertEqual(hashlib.sha256(Path(numeric.__file__).read_bytes()).hexdigest(),
                         'b79efd8bf01eade62a50e10e2be7823b6d944109452e8a3bebaeb8954bb2186b')
