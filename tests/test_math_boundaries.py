"""Native evidence gates for math/prose boundaries; no model or API calls."""
import unittest
import re

from textbook_pdf.extraction import _math_spans, _check_atomic_formulas
from textbook_pdf.models import Asset, Book, Element


def native_line(text, italic_words=()):
    chars = []
    for i, char in enumerate(text):
        italic = any(match.start() <= i < match.end()
                     for word in italic_words
                     for match in re.finditer(r'\b' + re.escape(word) + r'\b', text))
        chars.append({'id': f'b0.l0.s0.c{i}', 'c': char,
                      'bbox': [i * 5.0, 10.0, i * 5.0 + 5.0, 20.0],
                      'origin': [i * 5.0, 18.0], 'font': 'STIXGeneral-Italic' if italic else 'STIXGeneral-Regular',
                      'size': 10.0, 'flags': 6 if italic else 4, 'block': 0, 'line': 0,
                      'owner': 'e'})
    return {'text': text, 'chars': chars, 'bbox': [0, 10, len(text) * 5, 20]}


def spans(line):
    return [line['text'][a:b] for a, b in _math_spans(line)]


def case(text, math_text, prefix='', suffix=''):
    line = native_line(text, ('v', 'N', 'x'))
    a = text.index(math_text); b = a + len(math_text)
    gs = line['chars'][a:b]
    asset = Asset('m', 'formula', 'unused.pdf', 1, [a * 5, 10, b * 5, 20],
                  (b-a)*5, 10, 8, math_text,
                  metadata={'glyph_ids': [g['id'] for g in gs]})
    element = Element('e', 'paragraph', prefix+'⟦F:m⟧'+suffix, 1,
                      line['bbox'], asset_ids=['m'])
    book = Book('nonexistent-synthetic-source.pdf', 'source', 'test', 1, [1],
                elements=[element], assets={'m': asset})
    book._active_glyphs = line['chars']
    return book, element, asset


class MathBoundaryTests(unittest.TestCase):
    def test_short_word_prose_parenthesis_stays_outside_math(self):
        for text in ['y (it is an identity', 'x (an ordinary explanation', 'x (a useful fact']:
            line = native_line(text, ('x', 'y'))
            self.assertEqual(spans(line), [text[0]])

    def test_tuple_and_function_arguments_remain_protected(self):
        self.assertEqual(spans(native_line('P(x, y)', ('P', 'x', 'y'))), ['P(x, y)'])
        self.assertEqual(spans(native_line('x (y + 1)', ('x', 'y'))), ['x (y + 1)'])

    def test_upright_interval_endpoint_is_not_an_english_clause(self):
        self.assertEqual(spans(native_line('x = (i, ∞)',('x',))), ['x = (i, ∞)'])

    def test_upright_half_open_interval_is_one_complete_native_run(self):
        self.assertEqual(spans(native_line('Ai = [i, ∞)',('Ai',))), ['Ai = [i, ∞)'])

    def test_assigned_mixed_interval_uses_the_same_source_proof(self):
        book,e,asset=case('Ai = [i, ∞)','Ai = [i, ∞)')
        book.issues=[{'code':'formula_boundary_review','element_id':'e','page':1,'severity':'error',
                      'detail':'Formula parentheses cross a source line or heuristic boundary: Ai = [i, ∞)'}]
        _check_atomic_formulas(book,[e])
        self.assertFalse(book.issues)

    def test_upright_bold_matrix_operand_is_not_an_english_clause(self):
        line=native_line('A + (B + C) = (A + B) + C.')
        for glyph in line['chars']:glyph.update(font='STIXGeneral-Bold',flags=20)
        result=spans(line)
        self.assertTrue(any('(B + C)'in item for item in result),result)
        self.assertTrue(any('(A + B)'in item for item in result),result)

    def test_null_is_source_identifier_after_relation(self):
        self.assertEqual(spans(native_line('v = null.', ('v', 'null'))), ['v = null'])
        self.assertEqual(spans(native_line('A null value is common.', ('null',))), [])

    def test_null_nil_words_do_not_split_a_function_or_larger_identifier(self):
        self.assertEqual(spans(native_line('null(nil)',('null','nil'))),['null(nil)'])
        self.assertEqual(spans(native_line('v = nil',('v','nil'))),['v = nil'])
        self.assertEqual(spans(native_line('v = nullx',('v','nullx'))),['v ='])
        self.assertEqual(spans(native_line('annulling a factor',('annulling',))),[])

    def test_upright_truth_constant_at_wrap_start_is_protected(self):
        self.assertEqual(spans(native_line('F, both of which are true.')), ['F'])

    def test_mathjax_apostrophe_inside_native_prose_word_stays_prose(self):
        for text in ['Hilbert’s', "don't"]:
            line=native_line(text)
            for glyph in line['chars']:
                glyph.update(font='STIXMathJax_Main-Bold',flags=20)
            self.assertEqual(spans(line),[])

    def test_mathematical_primes_are_not_prose_apostrophes(self):
        self.assertEqual(spans(native_line("f'(x)",('f','x'))),["f'(x)"])
        line=native_line('x′')
        for glyph in line['chars']:glyph['font']='STIXMath-Regular'
        self.assertEqual(spans(line),['x′'])

    def test_native_noun_rhs_is_not_a_missing_operand(self):
        book, e, asset = case('v := root of T', 'v :=', suffix=' root of T')
        _check_atomic_formulas(book, [e])
        self.assertFalse(book.issues)
        proof = book.profile['resolved_source_issues'][0]['source_proof']
        self.assertEqual(proof['rhs_prefix'], 'root of')
        self.assertTrue(proof['native_glyph_ids'])

    def test_explicit_count_definition_has_translatable_rhs(self):
        book, e, asset = case('N = total number of solutions', 'N =', suffix=' total number of solutions')
        _check_atomic_formulas(book, [e])
        self.assertFalse(book.issues)

    def test_algorithm_child_description_keeps_english_translatable(self):
        book,e,asset=case('v := left child of v','v :=',suffix=' left child of v')
        _check_atomic_formulas(book,[e])
        self.assertFalse(book.issues)

    def test_missing_or_unrelated_rhs_remains_blocking(self):
        for native, suffix in [('v :=', ''), ('v := because it fails', ' because it fails'),
                               ('v := root of T', ' an unrelated paragraph')]:
            book, e, asset = case(native, 'v :=', suffix=suffix)
            _check_atomic_formulas(book, [e])
            self.assertEqual(book.issues[0]['code'], 'formula_atomic_boundary_review')

    def test_incomplete_noun_rhs_still_needs_its_source_object(self):
        book,e,asset=case('N = number of','N =',suffix=' number of')
        _check_atomic_formulas(book,[e])
        self.assertEqual(book.issues[0]['code'],'formula_atomic_boundary_review')

    def test_rhs_on_other_baseline_or_missing_source_glyph_stays_blocking(self):
        for missing in [False, True]:
            book, e, asset = case('v := root of T', 'v :=', suffix=' root of T')
            if missing:
                book._active_glyphs = book._active_glyphs[1:]
            else:
                for glyph in book._active_glyphs[len('v :='):]:
                    glyph['origin'][1] += 20
                    glyph['bbox'][1] += 20; glyph['bbox'][3] += 20
            _check_atomic_formulas(book, [e])
            self.assertEqual(book.issues[0]['code'], 'formula_atomic_boundary_review')

    def test_source_complete_mixed_interval_resolves_only_its_own_diagnostic(self):
        book, e, asset = case('[0, 1)', '[0, 1)')
        book.issues = [{'code':'formula_boundary_review', 'element_id':'e', 'page':1,
                        'severity':'error', 'detail':'Formula parentheses cross a source line or heuristic boundary: [0, 1)'},
                       {'code':'formula_boundary_review', 'element_id':'e', 'page':1,
                        'severity':'error', 'detail':'Formula parentheses cross a source line or heuristic boundary: (x'}]
        _check_atomic_formulas(book, [e])
        self.assertEqual(len(book.issues), 1)
        self.assertTrue(book.issues[0]['detail'].endswith('(x'))

    def test_incomplete_or_unproven_interval_diagnostic_is_not_removed(self):
        for text, lost in [('[0, 1', False), ('[0, 1)', True)]:
            book, e, asset = case(text, text)
            if lost:asset.metadata['selected_glyphs_lost'] = ['lost-source-character']
            issue={'code':'formula_boundary_review','element_id':'e','page':1,'severity':'error',
                   'detail':'Formula parentheses cross a source line or heuristic boundary: '+text}
            book.issues=[issue]
            _check_atomic_formulas(book,[e])
            self.assertIn(issue,book.issues)


if __name__ == '__main__':
    unittest.main()
