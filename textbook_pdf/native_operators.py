"""Detect a proven native operator limit split across independently flowed assets.

This is a page-local source guard, not an equation recognizer or a completeness
certificate. It reads original glyph identities, visible geometry, font size and
baseline. Without current, verified native glyph ownership it makes no finding.
It never inspects an asset's extracted text or its assembled union rectangle.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math

from .models import TOKEN_RE

ISSUE_CODE = 'source_vertical_operator_limits_split'


def _valid_glyph(glyph):
    try:
        box = tuple(float(v) for v in glyph['bbox'])
        origin = tuple(float(v) for v in glyph['origin'])
        size = float(glyph['size'])
        return (len(box) == 4 and len(origin) == 2 and size > 0
                and all(math.isfinite(v) for v in (*box, *origin, size))
                and box[0] < box[2] and box[1] < box[3])
    except (KeyError, TypeError, ValueError):
        return False


def _box(glyphs):
    return [min(g['bbox'][0] for g in glyphs), min(g['bbox'][1] for g in glyphs),
            max(g['bbox'][2] for g in glyphs), max(g['bbox'][3] for g in glyphs)]


def _script_runs(glyphs, size):
    """Small original glyphs on one baseline, with a bounded interglyph gap."""
    small = [g for g in glyphs if 0 < g['size'] <= size * .82]
    rows = []
    for glyph in sorted(small, key=lambda g: (g['origin'][1], g['bbox'][0], g['id'])):
        matches = [row for row in rows if abs(row[0]['origin'][1] - glyph['origin'][1]) <= size * .10]
        if len(matches) == 1:
            matches[0].append(glyph)
        else:
            rows.append([glyph])
    for row in rows:
        run = []
        for glyph in sorted(row, key=lambda g: (g['bbox'][0], g['id'])):
            if run and glyph['bbox'][0] - run[-1]['bbox'][2] > size * .60:
                yield run
                run = []
            run.append(glyph)
        if run:
            yield run


def _position(operator, run, selected):
    size = operator['size']
    ob, rb = operator['bbox'], _box(run)
    oy, ry = (ob[1] + ob[3]) / 2, (rb[1] + rb[3]) / 2
    delta = ry - oy
    # Both stacked limits and scripts just to the right of an operator qualify.
    # All thresholds describe source mathematical adjacency, never glyph-match
    # tolerances or permission to move/resize a protected source object.
    if not size * .20 <= abs(delta) <= size * 1.05:
        return None
    right_script = ob[0] - size * .35 <= rb[0] <= ob[2] + size * .50
    centered_script = (rb[0] < ob[2] and rb[2] > ob[0]
                       and abs((rb[0] + rb[2] - ob[0] - ob[2]) / 2) <= size * .80)
    if not (right_script or centered_script):
        return None
    # An exponent/subscript whose own normal-size base precedes it is not an
    # operator limit. The source base must actually touch that script's run.
    for base in selected:
        bb = base['bbox']
        if (base['size'] >= size * .90 and base['c'] not in '∑∏∫'
                and 0 <= rb[0] - bb[2] <= size * .60
                and min(abs(base['origin'][1] - g['origin'][1]) for g in run) <= size * .65):
            return None
    return 'lower' if delta > 0 else 'upper'


def check_vertical_operator_limits(book, elements, glyphs=None):
    """Return page-local blocking issues; do not mutate Book, glyphs or assets.

    Only current formula tokens sharing a rendered source element are related.
    A missing/duplicate/stale glyph identity or unverified crop is insufficient
    evidence here; other source guards remain responsible for those failures.
    Empty output therefore never certifies all operators as structurally valid.
    """
    glyphs = getattr(book, '_active_glyphs', None) if glyphs is None else glyphs
    if not glyphs:
        return []
    elements = [e for e in elements if not e.metadata.get('render_suppressed')]
    pages = {e.page for e in elements}
    if len(pages) != 1:
        return []
    page = next(iter(pages))
    counts = Counter(g.get('id') for g in glyphs)
    by_id = {g['id']: g for g in glyphs if counts[g.get('id')] == 1 and _valid_glyph(g)}
    refs = defaultdict(set)
    for element in elements:
        for aid in TOKEN_RE.findall(element.text):
            if aid in element.asset_ids:
                refs[aid].add(element.id)
    selected, operators = {}, []
    for aid, owners in refs.items():
        asset = book.assets.get(aid)
        if (asset is None or asset.page != page or asset.kind != 'formula'
                or asset.metadata.get('selected_native_glyphs_verified') is not True):
            continue
        ids = asset.metadata.get('glyph_ids', [])
        if not ids or len(ids) != len(set(ids)):
            continue
        current = [by_id[k] for k in ids if k in by_id and by_id[k].get('asset_id') == aid
                   and by_id[k].get('owner') in owners]
        if len(current) != len(ids):
            continue
        current = [g for g in current if g['c'].strip()]
        selected[aid] = current
        operators.extend((aid, g) for g in current if g['c'] in {'∑', '∏', '∫'})
    findings = []
    seen = set()
    def flow_glyphs(shared):
        return [g for aid, owned in selected.items() if refs[aid] & shared for g in owned]
    for operator_aid, operator in operators:
        for script_aid, current in selected.items():
            shared = refs[operator_aid] & refs[script_aid]
            if script_aid == operator_aid or not shared:
                continue
            for run in _script_runs(current, operator['size']):
                position = _position(operator, run, flow_glyphs(shared))
                if position is None:
                    continue
                # A limit belonging to another complete operator object must
                # not be stolen by a neighbouring equation in the same prose.
                possible = []
                rb = _box(run)
                for other_aid, other in operators:
                    if not (refs[other_aid] & refs[script_aid]):
                        continue
                    if _position(other, run, flow_glyphs(refs[other_aid] & refs[script_aid])):
                        ob = other['bbox']
                        dx = max(ob[0] - rb[2], rb[0] - ob[2], 0)
                        dy = abs((rb[1] + rb[3] - ob[1] - ob[3]) / 2)
                        possible.append(((dx + dy) / other['size'], other_aid, other['id']))
                possible.sort()
                if (not possible or possible[0][1:] != (operator_aid, operator['id'])
                        or (len(possible) > 1 and abs(possible[0][0] - possible[1][0]) < .10)):
                    continue
                key = (operator['id'], script_aid, tuple(sorted(g['id'] for g in run)))
                if key in seen:
                    continue
                seen.add(key)
                proof = {
                    'schema': 'native-operator-limit-flow-v1',
                    'operator': {'glyph_id': operator['id'], 'char': operator['c'],
                                 'bbox': operator['bbox'], 'origin': operator['origin'],
                                 'size': operator['size'], 'asset_id': operator_aid},
                    'script_asset_id': script_aid, 'script_glyph_ids': [g['id'] for g in run],
                    'script_bbox': rb, 'script_baselines': [g['origin'][1] for g in run],
                    'script_sizes': [g['size'] for g in run], 'position': position,
                    'shared_flow_elements': sorted(shared),
                    'basis': 'exact-current-native-glyph-ids-visible-bbox-size-baseline',
                }
                for eid in sorted(shared):
                    findings.append({'code': ISSUE_CODE, 'severity': 'error', 'page': page,
                                     'element_id': eid, 'asset_id': operator_aid,
                                     'asset_ids': [operator_aid, script_aid],
                                     'detail': 'Verified source operator and its small-font limit are in separate independently flowed formula assets; preserve the original two-dimensional object before translation.',
                                     'source_proof': proof})
    return findings
