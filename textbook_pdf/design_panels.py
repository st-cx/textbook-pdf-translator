"""Explicit source-bound flat panels; never infer membership from translated prose."""
from dataclasses import asdict
from collections import Counter
from pathlib import Path
import math
import re
import uuid
import hashlib

from .models import TOKEN_RE, digest, file_sha256, protected_asset_descriptor

PROFILE_KEY = 'source_design_panels_v1'


def _number(value, low, high):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


def checked_panels(book, scoped=False):
    registry = book.profile.get(PROFILE_KEY, {})
    if not isinstance(registry, dict):
        raise ValueError('Source design panel registry must be a mapping')
    lookup = book.element_map()
    order = {e.id: n for n, e in enumerate(book.elements)}
    occupied, result = set(), {}
    for uid, panel in registry.items():
        if not isinstance(panel, dict) or panel.get('schema') != 'source-flat-panel-v1' or panel.get('id') != uid:
            raise ValueError('Unknown source panel contract')
        if panel.get('source_sha256') != book.source_sha256:
            raise ValueError('Source panel belongs to another original PDF')
        label, bodies = panel.get('label_id'), panel.get('body_ids')
        if not isinstance(label, str) or not isinstance(bodies, list) or len(bodies) != 1 or not all(isinstance(i, str) for i in bodies):
            raise ValueError('Source panel needs an explicit label and complete body')
        members = [label] + bodies
        if len(set(members)) != len(members) or occupied.intersection(members):
            raise ValueError('Source panel members repeat or overlap another panel')
        present = [eid in lookup for eid in members]
        if scoped and not any(present):
            continue
        if not all(present):
            raise ValueError('Partial source design panel selected')
        if [order[eid] for eid in members] != list(range(order[label], order[label] + len(members))):
            raise ValueError('Source panel member order changed or contains unrelated content')
        source_members = [lookup[eid] for eid in members]
        if lookup[label].kind != 'sidebar' or any(e.kind != 'paragraph' for e in source_members[1:]):
            raise ValueError('This flat panel supports one margin label and prose body only')
        if len({e.page for e in source_members}) != 1 or any(e.metadata.get('render_suppressed') for e in source_members):
            raise ValueError('Source panel cannot hide members or guess cross-page continuation')
        expected_members = {e.id: digest(asdict(e)) for e in source_members}
        if panel.get('member_record_sha256') != expected_members:
            raise ValueError('Source panel members changed since original design review')
        aids = {aid for e in source_members for aid in set(e.asset_ids) | set(TOKEN_RE.findall(e.text))}
        if aids - book.assets.keys():
            raise ValueError('Source panel refers to missing formula assets')
        descriptors = {aid: protected_asset_descriptor(book.assets[aid]) for aid in sorted(aids)}
        if panel.get('protected_asset_descriptors') != descriptors:
            raise ValueError('Source panel original formula identity changed')
        graphic = panel.get('source_graphic')
        if not isinstance(graphic, dict) or graphic.get('page') != source_members[0].page:
            raise ValueError('Source panel original graphic/page missing')
        page = next((p for p in book.pages if p['number'] == graphic['page']), None)
        originals = [g for g in (page or {}).get('graphics_coverage', []) if g['id'] == graphic.get('id')]
        if len(originals) != 1 or digest(originals[0]) != graphic.get('record_sha256'):
            raise ValueError('Source panel original graphic changed')
        paint = panel.get('paint')
        if (not isinstance(paint, dict) or set(paint) != {'space', 'components', 'opacity'}
                or paint['space'] != 'DeviceCMYK' or paint['opacity'] != 1
                or not isinstance(paint['components'], list) or len(paint['components']) != 4
                or not all(_number(v, 0, 1) for v in paint['components'])):
            raise ValueError('Source panel requires explicit opaque DeviceCMYK paint')
        padding = panel.get('insets_pt')
        if (not isinstance(padding, dict) or set(padding) != {'left', 'right', 'top', 'bottom'}
                or not all(_number(v, 0, 36) for v in padding.values())):
            raise ValueError('Invalid source-derived panel insets')
        if panel.get('oversize_policy') != 'reject-unreviewed-continuation':
            raise ValueError('Oversized source panels need reviewed continuation boundaries')
        occupied.update(members)
        result[uid] = panel
    return result


def body_panel(book, element_id):
    candidates = [p for p in book.profile.get(PROFILE_KEY, {}).values()
                  if isinstance(p, dict) and element_id in p.get('body_ids', [])]
    if not candidates:
        return None
    panels = checked_panels(book, scoped=True)
    panel = next(p for p in panels.values() if element_id in p['body_ids'])
    if len(panel['body_ids']) != 1:
        raise ValueError('Multi-paragraph panel projection has not been implemented')
    return panel


def markup(panel, element, text, book, paths, geometry, width, profile, issues,
           preview, labels, columns, texts, preview_ids, sticky_next, markers, margin_notes, flow_inset):
    from . import layout as L
    label_notes = [e.id for role, items in margin_notes for e in items if role == 'label']
    if label_notes != [panel['label_id']]:
        raise ValueError('Source panel label must appear once in its original outside rail')
    padding = panel['insets_pt']
    inner = width - padding['left'] - padding['right']
    if inner < 80:
        raise ValueError('Fixed-size source panel has insufficient body width')
    body = L._element_markup(element, text, book, paths, geometry, inner, profile, issues,
        preview, labels, columns, texts, preview_ids, sticky_next, markers, margin_notes, flow_inset,
        panel_skip=True, rail_shift=padding['left'])
    paint = ', '.join(L._number(v * 100) + '%' for v in panel['paint']['components'])
    args = [L._q(panel['id']), L._number(width) + 'pt',
            L._number(geometry['height'] - geometry['top'] - geometry['bottom']) + 'pt',
            'cmyk(' + paint + ')'] + [L._number(padding[k]) + 'pt' for k in ('left', 'right', 'top', 'bottom')]
    return '#source-design-panel(' + ', '.join(args) + ')[' + body + ']\n'


def renderer_identity():
    return {'source_design_panels_sha256': file_sha256(Path(__file__))}


_PDF_NUMBER = rb'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
# A deliberately finite grammar for the flat rectangle emitted by pinned Typst.
# Imported formula streams and arbitrary source PDF paint are never rewritten.
_RECTANGLE = re.compile(
    rb'q\s+1\s+0\s+0\s+-1\s+(?P<x>' + _PDF_NUMBER + rb')\s+(?P<y>' + _PDF_NUMBER + rb')\s+cm\s+'
    rb'(?P<paint>' + _PDF_NUMBER + rb'\s+' + _PDF_NUMBER + rb'\s+' + _PDF_NUMBER + rb'\s+' + _PDF_NUMBER + rb')\s+k\s+'
    rb'0\s+0\s+m\s+(?P<w1>' + _PDF_NUMBER + rb')\s+0\s+l\s+'
    rb'(?P<w2>' + _PDF_NUMBER + rb')\s+(?P<h1>' + _PDF_NUMBER + rb')\s+l\s+'
    rb'0\s+(?P<h2>' + _PDF_NUMBER + rb')\s+l\s+h\s+f\s+Q')


def _records(document, panels, traces):
    records = [t for t in traces if t.get('type') == 'source-panel']
    if len(records) != len(panels) or {t.get('id') for t in records} != set(panels):
        raise ValueError('Source panel trace set missing, duplicated or stale')
    for t in records:
        page = t.get('page')
        if type(page) is not int or not 1 <= page <= len(document):
            raise ValueError('Source panel page must be an in-range positive integer')
        if not all(_number(t.get(k), 0, 100000) for k in ('x', 'y', 'width', 'height')):
            raise ValueError('Source panel geometry must be finite numeric values')
        bounds = document[page - 1].rect
        if t['width'] <= 0 or t['height'] <= 0 or t['x'] + t['width'] > bounds.width + .001 or t['y'] + t['height'] > bounds.height + .001:
            raise ValueError('Source panel geometry is empty or outside the page')
    return records


_REFERENCE = re.compile(r'(?<!\w)(\d+)\s+\d+\s+R\b')


def _font_resource_signatures(document):
    """Font programs AND their actual resource bindings, independent of xref renumbering."""
    memo, active = {}, set()
    def node(xref):
        if xref in memo:
            return memo[xref]
        if xref in active or len(active) >= 256 or not 0 < xref < document.xref_length() or len(memo) > 20000:
            raise ValueError('Unverifiable or cyclic font resource dependency')
        active.add(xref)
        stream = document.xref_is_stream(xref)
        keys = document.xref_get_keys(xref)
        if not keys:
            raw = document.xref_object(xref, compressed=False)
            value = _REFERENCE.sub(lambda m: '@' + node(int(m[1])), raw)
        else:
            value = []
            for key in sorted(keys):
                if stream and key in {'Length', 'Filter', 'DecodeParms'}:
                    continue  # Encoding may change; decoded stream bytes are hashed below.
                kind, raw = document.xref_get_key(xref, key)
                value.append((key, kind, _REFERENCE.sub(lambda m: '@' + node(int(m[1])), raw)))
        fingerprint = digest({'object': value,
            'decoded_stream_sha256': hashlib.sha256(document.xref_stream(xref)).hexdigest() if stream else None})
        active.remove(xref)
        memo[xref] = fingerprint
        return fingerprint
    pages = []
    for n, page in enumerate(document, 1):
        # A large book may contain many independent imported Form resources.
        # Bound one page's reachable graph, never the accumulated whole book.
        memo.clear()
        active.clear()
        signatures = Counter()
        for font in page.get_fonts(full=True):
            xref, referencer = font[0], font[6]
            # The complete font resource graph includes FontFile/FontFile2/FontFile3,
            # CFF programs, encoding/ToUnicode, widths and Type3 CharProcs.
            binding = ('page', n) if referencer == 0 else ('form-resource', node(referencer))
            signatures[(tuple(font[1:6]), node(xref), binding)] += 1
        pages.append(signatures)
    return pages


def _paint_plan(page, trace, panel):
    expected = [float(trace[k]) for k in ('x', 'y', 'width', 'height')]
    candidates = []
    for xref in page.get_contents():
        raw = page.parent.xref_stream(xref)
        for match in _RECTANGLE.finditer(raw):
            x, y, w, w2, h, h2 = [float(match[k]) for k in ('x', 'y', 'w1', 'w2', 'h1', 'h2')]
            actual = [x, page.rect.height - y, w, h]
            if (max(abs(a - b) for a, b in zip(actual, expected)) <= .001
                    and w == w2 and h == h2):
                paint = [float(x) for x in match['paint'].split()]
                desired = panel['paint']['components']
                # Typst0.15.1 quantizes CMYK to8-bit. Restore this one reviewed
                # background's exact source components instead of accepting a seam.
                quantized = [math.floor(v * 255 + .5) / 255 for v in desired]
                if not all(min(abs(a - b), abs(a - c)) <= 2e-7 for a, b, c in zip(paint, desired, quantized)):
                    raise ValueError('Source panel geometry matched an unexpected native paint')
                replacement = ' '.join(format(v, '.12g') for v in desired).encode('ascii')
                candidates.append({'xref': xref, 'start': match.start('paint'), 'end': match.end('paint'),
                    'original': match['paint'], 'replacement': replacement, 'uid': panel['id'], 'bbox': actual})
    if len(candidates) != 1:
        raise ValueError('Need exactly one measured native flat rectangle for each source panel')
    return candidates[0]


def restore_exact_paint(book, pdf_path, traces):
    """Edit only each declared background's paint operands; all other bytes stay."""
    import pymupdf as fitz
    panels = checked_panels(book, scoped=True)
    if not panels:
        return {'opt_in': False, 'restored_panels': 0}
    path = Path(pdf_path)
    protected_paths = {Path(book.source_path).resolve()}
    for asset in book.assets.values():
        p = Path(asset.path)
        if p.is_absolute():
            protected_paths.add(p.resolve())
        for key in ('job_dir', 'asset_root'):
            if book.profile.get(key):
                protected_paths.add((Path(book.profile[key]) / p).resolve())
    if path.resolve() in protected_paths:
        raise ValueError('Source panel paint cannot overwrite original PDF or protected assets')
    before = file_sha256(path)
    applied = []
    with fitz.open(path) as pdf:
        records = _records(pdf, panels, traces)
        original_text_traces = [p.get_texttrace() for p in pdf]
        original_font_resources = _font_resource_signatures(pdf)
        def protected_streams(document):
            return {hashlib.sha256(document.xref_stream(x)).hexdigest()
                    for x in range(1, document.xref_length()) if document.xref_is_stream(x)
                    and ('/Subtype /Form' in document.xref_object(x) or '/Length1' in document.xref_object(x))}
        original_protected_streams = protected_streams(pdf)
        plans = [_paint_plan(pdf[int(t['page']) - 1], t, panels[t['id']]) for t in records]
        by_stream = {}
        for plan in plans:
            by_stream.setdefault(plan['xref'], []).append(plan)
        for xref, patches in by_stream.items():
            original = pdf.xref_stream(xref)
            raw = original
            last = len(raw) + 1
            for patch in sorted(patches, key=lambda p: p['start'], reverse=True):
                if patch['end'] >= last or raw[patch['start']:patch['end']] != patch['original']:
                    raise ValueError('Native source panel paint patches overlap or changed')
                raw = raw[:patch['start']] + patch['replacement'] + raw[patch['end']:]
                last = patch['start']
            pdf.update_stream(xref, raw)
            applied.extend({'panel_id': p['uid'], 'bbox_xywh': p['bbox'],
                'before_components': p['original'].decode('ascii'),
                'source_components': p['replacement'].decode('ascii'),
                'only_native_paint_operands_changed': True} for p in patches)
        staged = path.with_name('.panel-paint-' + uuid.uuid4().hex + '.pdf')
        pdf.save(staged, garbage=4, deflate=True)
    # A fresh actual-PDF reader checks the final operands before replacing output.
    with fitz.open(staged) as fresh:
        _records(fresh, panels, traces)
        if [p.get_texttrace() for p in fresh] != original_text_traces:
            raise ValueError('Source panel paint restoration changed actual text/glyph traces')
        if protected_streams(fresh) != original_protected_streams:
            raise ValueError('Source panel paint restoration changed protected Form/font streams')
        if _font_resource_signatures(fresh) != original_font_resources:
            raise ValueError('Source panel paint restoration changed an actual font program or resource binding')
        for t in records:
            p = _paint_plan(fresh[int(t['page']) - 1], t, panels[t['id']])
            if p['original'] != p['replacement']:
                raise ValueError('Actual saved source panel paint is not exact')
    staged.replace(path)
    return {'opt_in': True, 'restored_panels': len(applied), 'before_pdf_sha256': before,
            'after_pdf_sha256': file_sha256(path), 'panels': applied,
            'all_actual_text_traces_exact_before_after': True,
            'all_Form_and_embedded_font_stream_hashes_exact_before_after': True,
            'all_font_programs_encodings_and_resource_bindings_exact_before_after': True,
            'source_assets_or_text_matrices_modified': False}


def check_exact_paint(book, pdf_path, traces):
    """Read-only final-PDF check used independently from the paint restoration."""
    import pymupdf as fitz
    panels = checked_panels(book, scoped=True)
    with fitz.open(pdf_path) as pdf:
        records = _records(pdf, panels, traces)
        for t in records:
            paint = _paint_plan(pdf[int(t['page']) - 1], t, panels[t['id']])
            if paint['original'] != paint['replacement']:
                raise ValueError('Final PDF source panel still contains quantized/incorrect CMYK paint')
    return {'opt_in': bool(panels), 'exact_native_CMYK_panels': len(records), 'passed': True}
