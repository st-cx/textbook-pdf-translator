"""Meaningful source, real-pagination and no-shrink checks for flat panels."""
from pathlib import Path
from dataclasses import asdict
from copy import deepcopy
import json
import re
import sys
import unittest
import uuid
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'evidence/test-artifacts/design-panels'
BASE.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
from textbook_pdf.models import Book, Element, Asset, digest, protected_asset_descriptor
from textbook_pdf.design_panels import checked_panels, PROFILE_KEY
from textbook_pdf.layout import compose
from textbook_pdf.store import translation_rule_hash
import pymupdf as fitz


def fixture(long=False):
    folder = BASE / ('fixture-' + uuid.uuid4().hex[:10])
    folder.mkdir()
    original = folder / 'self-authored.pdf'
    doc = fitz.open()
    page = doc.new_page(width=360, height=300)
    page.insert_text((42, 30), 'Self-authored panel fixture; no textbook material.')
    page.insert_font(fontname='SelfRoman', fontbuffer=fitz.Font('tiro').buffer)
    page.insert_text((160, 130), 'x', fontname='SelfRoman', fontsize=10.5)
    formula = folder / 'self-authored-variable.pdf'
    clip = fitz.open()
    cp = clip.new_page(width=18, height=17)
    cp.show_pdf_page(cp.rect, doc, 0, clip=fitz.Rect(158, 119, 176, 136))
    for xref in range(1, clip.xref_length()):
        if clip.xref_is_stream(xref) and clip.xref_get_key(xref, 'Subtype')[1] in {'/Type1C', '/CIDFontType0C'}:
            original_program = clip.xref_stream(xref)
            original_subtype = clip.xref_get_key(xref, 'Subtype')
            dictionary, replacements = re.subn(r'/Length1\s+\d+\b', '', clip.xref_object(xref))
            assert replacements == 1
            clip.update_object(xref, dictionary)
            assert '/Length1' not in clip.xref_object(xref)
            assert clip.xref_stream(xref) == original_program
            assert clip.xref_get_key(xref, 'Subtype') == original_subtype
    clip.save(formula)
    clip.close()
    doc.save(original)
    doc.close()
    from textbook_pdf.models import file_sha256
    label = Element('label', 'sidebar', 'Definition 1', 1, printed_page='7', bbox=[20, 105, 95, 117])
    body = Element('body', 'paragraph', 'An integer is even when it is twice an integer. ⟦F:self-x⟧', 1,
                   printed_page='7', bbox=[145, 105, 320, 170], asset_ids=['self-x'])
    asset = Asset(id='self-x', kind='formula', page=1, path=str(formula), bbox=[158, 119, 176, 136],
                  width=18, height=17, baseline=11, text='x', sha256=file_sha256(formula),
                  metadata={'format': 'pdf', 'pdf_page': 0})
    graphic = {'id': 'band', 'bbox': [139, 100, 326, 180], 'disposition': 'design-reference',
               'reason': 'self-authored design fixture', 'asset_ids': []}
    book = Book(str(original), file_sha256(original), 'Self authored', 1, [1],
        elements=[label, body], assets={asset.id: asset}, pages=[{'number': 1, 'width': 360, 'height': 300,
                'printed_page': '7', 'graphics_coverage': [graphic]}],
        profile={'body_size': 10.5, 'exercise_size': 9.5, 'caption_size': 8.5,
                 'body_font': 'SimSun', 'margins': {'left': 42, 'right': 32, 'top': 40, 'bottom': 40}})
    panel = {'schema': 'source-flat-panel-v1', 'id': 'self-definition', 'source_sha256': book.source_sha256,
             'label_id': label.id, 'body_ids': [body.id],
             'member_record_sha256': {e.id: digest(asdict(e)) for e in book.elements},
             'protected_asset_descriptors': {asset.id: protected_asset_descriptor(asset)},
             'source_graphic': {'page': 1, 'id': 'band', 'record_sha256': digest(graphic)},
             'paint': {'space': 'DeviceCMYK', 'components': [.1, 0, 0, 0], 'opacity': 1},
             'insets_pt': {'left': 6, 'right': 6, 'top': 11.5, 'bottom': 10.4},
             'oversize_policy': 'reject-unreviewed-continuation'}
    book.profile[PROFILE_KEY] = {'self-definition': panel}
    texts = {'label': '定义 1', 'body': '若一个整数等于某个整数的两倍，就称它为偶数。' * (80 if long else 2) + '⟦F:self-x⟧'}
    return book, texts, folder


class PanelTests(unittest.TestCase):
    def test_real_cumulative_font_resources_do_not_inherit_one_page_limit(self):
        from textbook_pdf.design_panels import _font_resource_signatures
        target = BASE / ('large-resources-' + uuid.uuid4().hex[:10] + '.pdf')
        doc = fitz.open()
        for n in range(21):
            page = doc.new_page(width=260, height=260)
            resources, operators = [], []
            for k in range(1001):
                xref = doc.get_new_xref()
                doc.update_object(xref, f'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding /Name /OwnP{n}F{k} >>')
                resources.append(f'/F{k} {xref} 0 R')
                x, y = 8 + (k % 32) * 7.5, 248 - (k // 32) * 7.5
                operators.append(f'BT /F{k} 4 Tf 1 0 0 1 {x} {y} Tm (x) Tj ET\n')
            resource = doc.get_new_xref()
            doc.update_object(resource, '<< /Font << ' + ' '.join(resources) + ' >> >>')
            content = doc.get_new_xref()
            doc.update_object(content, '<< >>')
            doc.update_stream(content, ''.join(operators).encode('ascii'))
            doc.xref_set_key(page.xref, 'Resources', f'{resource} 0 R')
            doc.xref_set_key(page.xref, 'Contents', f'{content} 0 R')
        doc.save(target, garbage=0, deflate=True)
        doc.close()
        with fitz.open(target) as fresh:
            self.assertEqual(len(fresh), 21)
            self.assertEqual([len(p.get_fonts(full=True)) for p in fresh], [1001] * 21)
            self.assertEqual([len([c for s in p.get_texttrace() for c in s['chars']]) for p in fresh], [1001] * 21)
            signatures = _font_resource_signatures(fresh)
            self.assertEqual([sum(p.values()) for p in signatures], [1001] * 21)

    def test_valid_and_layout_only_rule_identity(self):
        book, texts, folder = fixture()
        a = translation_rule_hash(book, book.elements[-1], {})
        self.assertEqual(len(checked_panels(book)), 1)
        book.profile.pop(PROFILE_KEY)
        self.assertEqual(a, translation_rule_hash(book, book.elements[-1], {}))

    def test_stale_partial_duplicate_paint_and_graphic_rejected(self):
        book, texts, folder = fixture()
        mutations = {
            'stale_source': lambda b: setattr(b.elements[-1], 'text', 'Changed source.'),
            'partial_panel': lambda b: b.elements.pop(0),
            'duplicate_member': lambda b: b.profile[PROFILE_KEY]['self-definition']['body_ids'].append('label'),
            'wrong_pdf': lambda b: b.profile[PROFILE_KEY]['self-definition'].__setitem__('source_sha256', 'b' * 64),
            'rgb_approximation': lambda b: b.profile[PROFILE_KEY]['self-definition']['paint'].__setitem__('space', 'RGB'),
            'negative_inset': lambda b: b.profile[PROFILE_KEY]['self-definition']['insets_pt'].__setitem__('left', -1),
            'nonopaque_paint': lambda b: b.profile[PROFILE_KEY]['self-definition']['paint'].__setitem__('opacity', .5),
            'missing_graphic': lambda b: b.pages[0]['graphics_coverage'].clear(),
            'shrink_policy': lambda b: b.profile[PROFILE_KEY]['self-definition'].__setitem__('oversize_policy', 'shrink'),
        }
        for key, mutate in mutations.items():
            with self.subTest(key=key):
                bad = deepcopy(book)
                mutate(bad)
                with self.assertRaises(ValueError):
                    checked_panels(bad)

    def test_real_pdf_flat_cmyk_and_fixed_font(self):
        book, texts, folder = fixture()
        result = compose(book, texts, folder / 'render')
        with fitz.open(result['pdf_path']) as pdf:
            self.assertEqual(len(pdf), 1)
            stream = b'\n'.join(pdf.xref_stream(x) for x in pdf[0].get_contents())
            self.assertRegex(stream, rb'0\.1\s+0\s+0\s+0\s+k')
            spans = [s for b in pdf[0].get_text('dict')['blocks'] for l in b.get('lines', [])
                     for s in l['spans'] if any('\u4e00' <= c <= '\u9fff' for c in s['text'])]
            self.assertTrue(spans)
            self.assertTrue(all(abs(s['size'] - 10.5) < .001 for s in spans if '原书' not in s['text']))

    def test_oversized_unit_rejected_without_compression(self):
        book, texts, folder = fixture(long=True)
        with self.assertRaises(Exception) as raised:
            compose(book, texts, folder / 'render')
        self.assertIn('continuation boundaries', str(raised.exception))

    def test_short_panel_and_label_move_together_to_added_page(self):
        book, texts, folder = fixture()
        filler = Element('before-panel', 'paragraph', 'A preceding paragraph.', 1,
                         printed_page='7', bbox=[145, 35, 320, 95])
        book.elements.insert(0, filler)
        texts[filler.id] = '这段自创的正文用于验证自然分页和原书页码。' * 14
        result = compose(book, texts, folder / 'render')
        mapping = json.loads(Path(result['layout_map_path']).read_text('utf8'))
        label = mapping['elements']['label']
        body = mapping['elements']['body']
        self.assertEqual(len(label), 1)
        self.assertEqual(len(body), 1)
        self.assertEqual(label[0]['page'], body[0]['page'])
        self.assertEqual(body[0]['page'], 2)
        self.assertEqual(mapping['pages'][1]['source_labels'], ['原书 p.7'])

    def test_final_quantized_or_wrong_paint_is_rejected_by_read_only_check(self):
        from textbook_pdf.design_panels import check_exact_paint
        book, texts, folder = fixture()
        result = compose(book, texts, folder / 'render')
        pdfpath = Path(result['pdf_path'])
        traces = json.loads((folder / 'render/typst-traces.json').read_text('utf8'))
        self.assertTrue(check_exact_paint(book, pdfpath, traces)['passed'])
        altered = folder / 'deliberately-wrong-self-authored.pdf'
        with fitz.open(pdfpath) as pdf:
            changes = 0
            for xref in pdf[0].get_contents():
                raw = pdf.xref_stream(xref)
                if b'0.1 0 0 0 k' in raw:
                    self.assertEqual(raw.count(b'0.1 0 0 0 k'), 1)
                    pdf.update_stream(xref, raw.replace(b'0.1 0 0 0 k', b'0.2 0 0 0 k'))
                    changes += 1
            self.assertEqual(changes, 1)
            pdf.save(altered)
        with self.assertRaises(ValueError):
            check_exact_paint(book, altered, traces)

    def test_invalid_trace_page_and_geometry_are_rejected(self):
        from textbook_pdf.design_panels import check_exact_paint
        book, texts, folder = fixture()
        result = compose(book, texts, folder / 'render')
        traces = json.loads((folder / 'render/typst-traces.json').read_text('utf8'))
        for key, values in {'page': [0, -1, 1.9, True, 2],
                            'x': [float('nan'), float('inf'), -1],
                            'width': [0, -1, True, 100000]}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    bad = deepcopy(traces)
                    next(t for t in bad if t.get('type') == 'source-panel')[key] = value
                    with self.assertRaises(ValueError):
                        check_exact_paint(book, result['pdf_path'], bad)

    def test_injected_CFF_program_change_during_save_is_rejected(self):
        from textbook_pdf.design_panels import restore_exact_paint
        book, texts, folder = fixture()
        result = compose(book, texts, folder / 'render')
        output = Path(result['pdf_path'])
        traces = json.loads((folder / 'render/typst-traces.json').read_text('utf8'))
        before = output.read_bytes()
        original_save = fitz.Document.save
        injected = []
        def changed_save(pdf, path, *args, **kwargs):
            for xref in range(1, pdf.xref_length()):
                kind = pdf.xref_get_key(xref, 'Subtype')[1]
                if pdf.xref_is_stream(xref) and kind in {'/Type1C', '/CIDFontType0C'}:
                    pdf.update_stream(xref, pdf.xref_stream(xref) + b'\0')
                    injected.append(xref)
                    break
            self.assertEqual(len(injected), 1, 'Self-authored fixture must contain a real embedded CFF program')
            return original_save(pdf, path, *args, **kwargs)
        with patch.object(fitz.Document, 'save', changed_save):
            with self.assertRaisesRegex(ValueError, 'font program or resource binding'):
                restore_exact_paint(book, output, traces)
        self.assertEqual(output.read_bytes(), before, 'A rejected save must retain the existing good output')

    def test_production_QA_checks_actual_paint_even_with_updated_pdf_hash(self):
        from textbook_pdf.qa import verify
        from textbook_pdf.models import file_sha256
        book, texts, folder = fixture()
        rendered = compose(book, texts, folder / 'render')
        output = Path(rendered['pdf_path'])
        mapping = json.loads((folder / 'render/layout_map.json').read_text('utf8'))
        good = verify(book, texts, folder / 'qa-good', pdf_path=output, layout_map=mapping)
        self.assertEqual(good['source_design_panel_checks']['status'], 'passed')
        altered = folder / 'changed-paint.pdf'
        with fitz.open(output) as pdf:
            changes = 0
            for xref in pdf[0].get_contents():
                raw = pdf.xref_stream(xref)
                if b'0.1 0 0 0 k' in raw:
                    pdf.update_stream(xref, raw.replace(b'0.1 0 0 0 k', b'0.2 0 0 0 k'))
                    changes += 1
            self.assertEqual(changes, 1)
            pdf.save(altered)
        mapping['pdf_sha256'] = file_sha256(altered)
        bad = verify(book, texts, folder / 'qa-bad', pdf_path=altered, layout_map=mapping)
        self.assertEqual(bad['source_design_panel_checks']['status'], 'failed')
        self.assertIn('source_design_panel_check_failed', [i['code'] for i in bad['issues']])
        self.assertNotIn('stale_rendered_pdf', [i['code'] for i in bad['issues']])

    def test_production_QA_rejects_missing_panel_traces(self):
        from textbook_pdf.qa import verify
        book, texts, folder = fixture()
        rendered = compose(book, texts, folder / 'render')
        mapping = json.loads((folder / 'render/layout_map.json').read_text('utf8'))
        mapping.pop('source_design_panel_traces')
        result = verify(book, texts, folder / 'qa-missing-traces', pdf_path=rendered['pdf_path'], layout_map=mapping)
        self.assertEqual(result['source_design_panel_checks']['status'], 'failed')
        self.assertIn('source_design_panel_check_failed', [i['code'] for i in result['issues']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
