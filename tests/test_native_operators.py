"""A native operator's limit cannot flow as a separate inline image."""
from copy import deepcopy
import unittest

from textbook_pdf.models import Asset, Book, Element
from textbook_pdf.native_operators import check_vertical_operator_limits


class NativeOperatorTests(unittest.TestCase):
    def fixture(self, *, operator='∑', complete=False, upper=False):
        book = Book('synthetic.pdf', 'source', 'Fixture', 1, [1])
        glyphs = [
            {'id': 'op', 'c': operator, 'bbox': [40, 30, 49, 42],
             'origin': [40, 31], 'size': 10, 'asset_id': 'operator', 'owner': 'body'},
            {'id': 'limit', 'c': 'n' if upper else 'i',
             'bbox': [49.4, 27 if upper else 36, 53, 34 if upper else 43],
             'origin': [49.4, 32 if upper else 41], 'size': 7,
             'asset_id': 'operator' if complete else 'operand', 'owner': 'body'},
            {'id': 'operand', 'c': 'x', 'bbox': [65, 30, 71, 40],
             'origin': [65, 38], 'size': 10, 'asset_id': 'operand', 'owner': 'body'},
        ]
        for aid, ids in [('operator', ['op', 'limit'] if complete else ['op']),
                         ('operand', ['operand'] if complete else ['limit', 'operand'])]:
            book.assets[aid] = Asset(aid, 'formula', 'unopened.pdf', 1, [40, 27, 71, 43], 31, 16,
                                    text='not used for detection', metadata={
                                        'glyph_ids': ids, 'selected_native_glyphs_verified': True})
        element = Element('body', 'paragraph', '⟦F:operator⟧ ⟦F:operand⟧', 1,
                          asset_ids=['operator', 'operand'])
        book.elements = [element]
        book._active_glyphs = glyphs
        return book, [element], glyphs

    def test_lower_limit_is_blocked_for_sum_product_and_integral(self):
        for symbol in '∑∏∫':
            with self.subTest(symbol=symbol):
                book, elements, glyphs = self.fixture(operator=symbol)
                issues = check_vertical_operator_limits(book, elements, glyphs)
                self.assertEqual(len(issues), 1)
                self.assertEqual(issues[0]['severity'], 'error')
                self.assertEqual(issues[0]['source_proof']['operator']['glyph_id'], 'op')
                self.assertEqual(issues[0]['source_proof']['script_glyph_ids'], ['limit'])
                self.assertEqual(issues[0]['source_proof']['position'], 'lower')

    def test_upper_limit_in_another_flow_asset_is_blocked(self):
        book, elements, _ = self.fixture(upper=True)
        self.assertEqual(check_vertical_operator_limits(book, elements)[0]['source_proof']['position'], 'upper')

    def test_complete_original_object_is_not_flagged(self):
        book, elements, glyphs = self.fixture(complete=True)
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])

    def test_nearby_complete_summations_are_not_conflated(self):
        book, elements, glyphs = self.fixture(complete=True)
        for g in deepcopy(glyphs[:2]):
            g['id'] += '2'; g['asset_id'] = 'second'
            g['bbox'] = [x + 24 if i % 2 == 0 else x for i, x in enumerate(g['bbox'])]
            g['origin'][0] += 24
            glyphs.append(g)
        book.assets['second'] = Asset('second', 'formula', 'unopened.pdf', 1, [64, 27, 80, 43], 16, 16,
            metadata={'glyph_ids': ['op2', 'limit2'], 'selected_native_glyphs_verified': True})
        elements[0].text += ' ⟦F:second⟧'; elements[0].asset_ids.append('second')
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])

    def test_ordinary_superscript_after_its_base_is_not_an_operator_limit(self):
        book, elements, glyphs = self.fixture(upper=True)
        glyphs[2]['bbox'] = [49.2, 30, 55, 40]; glyphs[2]['origin'][0] = 49.2
        glyphs[1]['bbox'] = [55.4, 27, 59, 34]; glyphs[1]['origin'][0] = 55.4
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])

    def test_script_base_in_another_current_formula_asset_is_still_respected(self):
        book, elements, glyphs = self.fixture(upper=True)
        glyphs[2]['bbox'] = [43.4, 18, 49.3, 28]
        glyphs[2]['origin'] = [43.4, 25]
        glyphs[2]['asset_id'] = 'base'
        glyphs[1]['bbox'] = [49.4, 22, 53, 29]
        glyphs[1]['origin'] = [49.4, 27]
        book.assets['operand'].metadata['glyph_ids'] = ['limit']
        book.assets['base'] = Asset('base', 'formula', 'unopened.pdf', 1, [43.4, 18, 49.3, 28], 5.9, 10,
            metadata={'glyph_ids': ['operand'], 'selected_native_glyphs_verified': True})
        elements[0].text += ' ⟦F:base⟧'; elements[0].asset_ids.append('base')
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])

    def test_same_size_neighbour_is_not_a_script(self):
        book, elements, glyphs = self.fixture()
        glyphs[1]['size'] = 10
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])

    def test_previous_source_line_subscript_is_not_stolen_by_next_operator(self):
        book, elements, glyphs = self.fixture(complete=True)
        glyphs.extend([
            {'id': 'previous-base', 'c': 'X', 'bbox': [43.4, 18, 49.3, 28],
             'origin': [43.4, 25], 'size': 10, 'asset_id': 'previous', 'owner': 'body'},
            {'id': 'previous-index', 'c': 'n', 'bbox': [49.4, 22, 53, 29],
             'origin': [49.4, 27], 'size': 7, 'asset_id': 'previous', 'owner': 'body'},
        ])
        book.assets['previous'] = Asset('previous', 'formula', 'unopened.pdf', 1, [43.4, 18, 53, 29], 9.6, 11,
            metadata={'glyph_ids': ['previous-base', 'previous-index'], 'selected_native_glyphs_verified': True})
        elements[0].text = '⟦F:previous⟧ ' + elements[0].text
        elements[0].asset_ids.append('previous')
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])

    def test_different_equation_element_is_not_conflated(self):
        book, elements, glyphs = self.fixture()
        elements[0].text = '⟦F:operator⟧'; elements[0].asset_ids = ['operator']
        elements.append(Element('other', 'paragraph', '⟦F:operand⟧', 1, asset_ids=['operand']))
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])

    def test_missing_native_or_verified_identity_does_not_infer_from_asset_text(self):
        book, elements, glyphs = self.fixture()
        del book._active_glyphs
        self.assertEqual(check_vertical_operator_limits(book, elements), [])
        book.assets['operator'].metadata['selected_native_glyphs_verified'] = False
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])

    def test_duplicate_or_stale_glyph_owner_cannot_establish_evidence(self):
        book, elements, glyphs = self.fixture()
        glyphs[1]['asset_id'] = 'old-replaced'
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])
        glyphs[1]['asset_id'] = 'operand'
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs + [deepcopy(glyphs[1])]), [])
        glyphs[1]['owner'] = 'old-source-element'
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])

    def test_measured_wrapped_source_relations_use_glyphs_not_union_boxes(self):
        # Original-native measurements from two independently verified wrapped
        # equations; only geometry/IDs are fixtures, no textbook prose or PDFs.
        measurements = [
            ([168.09731445, 570.56044922, 176.89731445, 581.61044922],
             [167.74731445, 571.46044922], 10.46080017,
             [177.31199646, 576.77984619, 179.34765625, 584.10235596],
             [177.31199646, 582.40753174], 7.32250023),
            ([310.30132446, 274.79909668, 318.22632446, 284.84909668],
             [309.95132446, 275.57409668], 9.46450043,
             [318.63000488, 279.90432739, 321.20718384, 286.52941895],
             [318.63000488, 284.99603271], 6.62510014),
        ]
        for ob, oo, os, sb, so, ss in measurements:
            with self.subTest(operator_bbox=ob):
                book, elements, glyphs = self.fixture()
                glyphs[0].update(bbox=ob, origin=oo, size=os)
                glyphs[1].update(bbox=sb, origin=so, size=ss)
                glyphs[2].update(bbox=[sb[0]+30, sb[1]-3, sb[0]+36, sb[3]-3],
                                 origin=[sb[0]+30, so[1]-3], size=os)
                book.assets['operator'].bbox = [ob[0], ob[1]-15, 562.4, ob[3]]
                issues = check_vertical_operator_limits(book, elements, glyphs)
                self.assertEqual(len(issues), 1)
                self.assertEqual(issues[0]['source_proof']['operator']['bbox'], ob)
                self.assertEqual(issues[0]['source_proof']['script_glyph_ids'], ['limit'])

    def test_only_current_formula_tokens_are_inspected(self):
        book, elements, glyphs = self.fixture()
        elements[0].text = 'ordinary text'
        self.assertEqual(check_vertical_operator_limits(book, elements, glyphs), [])


if __name__ == '__main__':
    unittest.main()
