"""Assemble native mathematical runs using source geometry, never prose guesses.

Every merged crop must contain only the selected native glyphs. Numerators,
denominators and limits can have separate PDF text owners; neither owner IDs nor
the PDF's character order establish their visual mathematical relationship.
"""
from __future__ import annotations

from collections import Counter
import re
import statistics

import pymupdf as fitz

from .models import TOKEN_RE


def _numbered_exercise_group(element):
    """Only a real exercise/list boundary, never a bare numeric denominator."""
    if element is None or element.kind!='exercise' or element.metadata.get('render_suppressed'):
        return None
    label=element.metadata.get('list_label')
    source=element.metadata.get('source_text',element.text)
    if not (label is not None and str(label).strip()) and not re.match(r'^\s*\d+[.)]\s',source):
        return None
    return element.group_id or element.id


def _absorb_literals(element, selected, book, glyph_map):
    """Remove native literal glyphs only with an exact prose-character map.

    A PDF can emit a closing delimiter or denominator in another text line,
    which is later merged into a paragraph. Never remove a matching character
    by value alone: it may be another parenthesis or a question number.
    """
    protected = set().union(*(set(book.assets[aid].metadata.get('glyph_ids', []))
                              for aid in TOKEN_RE.findall(element.text) if aid in book.assets))
    plain = [glyph_map[gid] for gid in dict.fromkeys(element.metadata.get('glyph_ids', []))
             if gid in glyph_map and gid not in protected and glyph_map[gid]['c'].strip()]
    absorbed = {g['id'] for g in plain} & selected
    if not absorbed:
        return element.text
    token_positions = {i for match in TOKEN_RE.finditer(element.text) for i in range(*match.span())}
    positions = [i for i, char in enumerate(element.text) if i not in token_positions and char.strip()]
    if ''.join(element.text[i] for i in positions) != ''.join(g['c'] for g in plain):
        return None
    removed = {position for position, glyph in zip(positions, plain) if glyph['id'] in absorbed}
    return ''.join(char for i, char in enumerate(element.text) if i not in removed)


def assemble_native_regions(book, doc, page, outdir, elements, glyphs, drawings):
    from .extraction import _asset, _rect_union, _rounded

    glyph_map = {g['id']: g for g in glyphs}
    index = {e.id: e for e in elements}
    referenced = {aid for e in elements if not e.metadata.get('render_suppressed')
                  for aid in TOKEN_RE.findall(e.text)}
    nodes = []
    for aid in sorted(referenced):
        asset = book.assets.get(aid)
        if not asset or asset.kind != 'formula':
            continue
        ids = set(asset.metadata.get('glyph_ids', [])) & glyph_map.keys()
        if ids:
            nodes.append({'assets': {aid}, 'glyphs': ids,
                          'bbox': _rect_union(glyph_map[k]['bbox'] for k in ids),
                          'baseline': asset.bbox[1] + asset.baseline,
                          'size': statistics.median(glyph_map[k].get('size', 10) for k in ids),
                          'vertical_operator': any(c in asset.text for c in '∑∏∫⋁⋀√')})
    # A lone denominator is often extracted as a prose element. Include only
    # entire literal elements; question labels and arbitrary words never qualify.
    for e in elements:
        if e.kind not in {'paragraph', 'formula', 'exercise'} or e.asset_ids or e.metadata.get('list_label'):
            continue
        if not re.fullmatch(r'[0-9 .,+−*/=()\[\]{}]+', e.text) or not e.text.strip():
            continue
        ids = set(e.metadata.get('glyph_ids', [])) & glyph_map.keys()
        if ids:
            nodes.append({'assets': set(), 'glyphs': ids,
                          'bbox': _rect_union(glyph_map[k]['bbox'] for k in ids)})
    # Literal PDF lines can already belong to a larger prose element. Keep
    # those scripts/delimiters eligible without treating the whole owner as math.
    known = set().union(*(n['glyphs'] for n in nodes)) if nodes else set()
    native_lines = {}
    for glyph in glyphs:
        native_lines.setdefault(glyph['id'].split('.s')[0], []).append(glyph)
    for line in native_lines.values():
        chars = [g for g in line if g['c'].strip() and g['id'] not in known]
        if not chars or not re.fullmatch(r'[0-9()\[\]{}]+', ''.join(g['c'] for g in chars)):
            continue
        ids = {g['id'] for g in chars} - known
        owners = [index.get(g.get('owner')) for g in chars]
        if not ids or any(e is None or e.kind not in {'paragraph', 'formula', 'exercise'} or e.metadata.get('list_label') for e in owners):
            continue
        nodes.append({'assets': set(), 'glyphs': ids,
                      'bbox': _rect_union(glyph_map[k]['bbox'] for k in ids)})
        known.update(ids)
    for drawing in drawings:
        r = fitz.Rect(drawing['rect']) & page.rect
        if .1 < r.width < page.rect.width * .45 and r.height <= 1.2:
            # Bars are geometry bridges, not textual mathematical evidence.
            nodes.append({'assets': set(), 'glyphs': set(), 'bbox': r, 'bar': True})
    # Native ascender/script boxes can overlap neighbouring numbered rows.
    # Track the *whole component's* source exercise identity, so an ungrouped
    # symbol, fraction bar or large operator cannot bridge two distinct rows.
    # Unnumbered denominators and parts of one multiline formula stay eligible.
    asset_groups={}
    for element in elements:
        group=_numbered_exercise_group(element)
        if group is not None:
            for aid in TOKEN_RE.findall(element.text):asset_groups.setdefault(aid,set()).add(group)
    for node in nodes:
        groups={_numbered_exercise_group(index.get(glyph_map[k].get('owner')))
                for k in node['glyphs']}
        groups.discard(None)
        for aid in node['assets']:groups.update(asset_groups.get(aid,()))
        node['exercise_groups']=groups
    parents = list(range(len(nodes)))
    mathematical_ids = set().union(*(n['glyphs'] for n in nodes)) if nodes else set()
    prose_bins = {}
    for g in glyphs:
        if g['c'].strip() and g['id'] not in mathematical_ids:
            y = (g['bbox'][1] + g['bbox'][3]) * .5
            prose_bins.setdefault(int(y // 12), []).append(g)
    group_boxes = [fitz.Rect(n['bbox']) for n in nodes]
    group_exercises=[set(node['exercise_groups']) for node in nodes]

    def root(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    def above_math_band(glyph, members):
        operators = [n for n in members if n.get('vertical_operator')]
        bases = [n['baseline'] for n in members if 'baseline' in n]
        sizes = [n['size'] for n in members if 'size' in n]
        # Tall native sum/radical boxes can cross the preceding prose line's
        # nominal boxes. This permits association only; _asset must still
        # remove every foreign text object safely before any source acceptance.
        return bool(operators and bases and glyph['origin'][1] < min(bases) - max(sizes) * .8)

    for i, a in enumerate(nodes):
        for j in range(i):
            b = nodes[j]
            ar, br = a['bbox'], b['bbox']
            dx = max(0, ar.x0 - br.x1, br.x0 - ar.x1)
            dy = max(0, ar.y0 - br.y1, br.y0 - ar.y1)
            oy = min(ar.y1, br.y1) - max(ar.y0, br.y0)
            ox = min(ar.x1, br.x1) - max(ar.x0, br.x0)
            horizontal = dx <= 5.0 and oy > max(.6, min(ar.height, br.height) * .12)
            bar_bridge = (a.get('bar') or b.get('bar')) and ox > .8 and dy <= 5.5
            limit_bridge = (a.get('vertical_operator') or b.get('vertical_operator')) and ox > .8 and dy <= 6
            if horizontal or bar_bridge or limit_bridge:
                left, right = root(i), root(j)
                if left == right:
                    continue
                exercises=group_exercises[left]|group_exercises[right]
                if len(exercises)>1:
                    continue
                union = group_boxes[left] | group_boxes[right]
                prose = [g for band in range(int(union.y0 // 12), int(union.y1 // 12) + 1)
                         for g in prose_bins.get(band, [])]
                members = [node for k, node in enumerate(nodes) if root(k) in {left, right}]
                if any(union.contains((fitz.Rect(g['bbox']).tl + fitz.Rect(g['bbox']).br) * .5)
                       and not above_math_band(g, members) for g in prose):
                    continue
                parents[left] = right
                group_boxes[right] = union
                group_exercises[right]=exercises
    components = {}
    for i, node in enumerate(nodes):
        components.setdefault(root(i), []).append(node)
    proofs = []
    for members in components.values():
        exercise_groups=set().union(*(n['exercise_groups'] for n in members))
        if len(exercise_groups)>1:
            continue
        old_ids = set().union(*(n['assets'] for n in members))
        selected = set().union(*(n['glyphs'] for n in members))
        old_glyphs = set().union(*(set(book.assets[aid].metadata.get('glyph_ids', [])) for aid in old_ids))
        if not old_ids or len(old_ids) < 2 and not any(n.get('bar') for n in members) and not selected - old_glyphs:
            continue
        box = _rect_union(n['bbox'] for n in members)
        foreign = [g for g in glyphs if g['c'].strip() and g['id'] not in selected
                   and box.contains((fitz.Rect(g['bbox']).tl + fitz.Rect(g['bbox']).br) * .5)]
        if any(not above_math_band(g, members) for g in foreign):
            continue
        tokens = [(e, list(TOKEN_RE.finditer(e.text))) for e in elements]
        owners = [e for e, matches in tokens if any(m[1] in old_ids for m in matches)]
        if not owners:
            continue
        keeper = owners[0]
        literal_text = {e.id: _absorb_literals(e, selected, book, glyph_map) for e in elements}
        if any(value is None for value in literal_text.values()):
            continue
        gs = [glyph_map[k] for k in selected]
        relations = [g['origin'][1] for g in gs if g['c'] in '=<>≤≥≠≈→↔']
        origins = relations or [g['origin'][1] for g in gs]
        common = Counter(round(v, 1) for v in origins).most_common(1)[0][0]
        baseline = statistics.median(v for v in origins if abs(v - common) < .11)
        ordered = sorted(gs, key=lambda g: (round(g['origin'][1], 1), g['bbox'][0], g['id']))
        text = ''.join(g['c'] for g in ordered)
        asset = _asset(book, doc, page, outdir, 'formula', box + (-.15, -.15, .15, .15),
                       text, baseline, {'detector': 'native-math-connected-region-v1',
                       'glyph_ids': sorted(selected), 'source_component_ids': sorted(old_ids),
                       'native_glyph_geometry_preserved': True,
                       'source_text_is_positioned_runs': True,
                       'fraction_bar_bboxes': [_rounded(n['bbox']) for n in members if n.get('bar')],
                       'assembly_proof': {'foreign_glyphs_in_union': len(foreign),
                                          'foreign_glyph_isolation_required': len(foreign),
                                          'numbered_exercise_groups':sorted(exercise_groups),
                                          'selected_native_glyphs': len(selected)}})
        if asset.id in old_ids or asset.metadata.get('isolation_issues') or asset.metadata.get('selected_glyphs_lost'):
            continue
        inserted = False
        for e, _ in tokens:
            original_ids = set(e.metadata.get('glyph_ids', []))
            e.text = literal_text[e.id]
            def replace(match):
                nonlocal inserted
                if match[1] not in old_ids:
                    return match[0]
                if not inserted:
                    inserted = True
                    return f'⟦F:{asset.id}⟧'
                return ''
            e.text = TOKEN_RE.sub(replace, e.text)
            e.asset_ids = [aid for aid in e.asset_ids if aid not in old_ids]
            if e is keeper:
                e.asset_ids.append(asset.id)
            owned = [g for g in gs if g.get('owner') == e.id]
            if owned:
                e.metadata['glyph_ids'] = [k for k in e.metadata.get('glyph_ids', [])
                                           if k not in selected]
            if e is not keeper and original_ids and original_ids <= selected and not TOKEN_RE.search(e.text):
                e.text = ''
            if not e.text.strip() and not e.asset_ids and e is not keeper:
                e.metadata.update(render_suppressed=True, superseded_by=keeper.id,
                                  exclusion_reason='native math glyphs transferred to complete region')
        keeper.metadata.setdefault('glyph_ids', []).extend(sorted(selected))
        if not re.search(r'[A-Za-z0-9]', TOKEN_RE.sub('', keeper.text)):
            keeper.bbox = list(asset.bbox)
            keeper.metadata['source_text'] = asset.text
        for g in gs:
            g.update(owner=keeper.id, asset_id=asset.id, disposition='preserved-original')
        for aid in old_ids:
            book.assets[aid].metadata.setdefault('combined_into', []).append(asset.id)
            book.assets[aid].metadata['exclusion_reason'] = 'complete native geometry assembled without foreign glyphs'
        resolved = [issue for issue in book.issues
                    if issue['code'] in {'formula_crop_contamination','formula_crop_selected_glyph_lost'}
                    and any(issue.get('asset_id') == aid or issue.get('detail', '').endswith('asset '+aid)
                            for aid in old_ids)]
        for issue in resolved:
            book.profile.setdefault('resolved_source_issues', []).append({
                **issue, 'resolution_reason': 'contaminated component replaced by isolated complete native region',
                'replacement_asset_id': asset.id, 'replacement_isolation_issues': []})
        book.issues = [issue for issue in book.issues if issue not in resolved]
        proofs.append({'asset_id': asset.id, 'component_ids': sorted(old_ids),
                       'owner_ids': [e.id for e in owners],
                       'glyph_ids': sorted(selected), 'bbox': asset.bbox})
    return proofs
