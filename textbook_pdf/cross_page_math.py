"""Evidence-bound continuation of an inline expression across source pages."""
from __future__ import annotations

import re

from .models import TOKEN_RE, digest


def _balanced(text):
    stack = []
    pairs = {')': '(', ']': '[', '}': '{'}
    for char in text:
        if char in '([{':
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                return False
    return not stack


def link_cross_page_formulas(book):
    """Link only a unique bottom/top prose pair with balanced combined math.

    Native assets, text, glyph ownership and physical-page identities remain
    intact. Only the continuation's reading position changes: a top-of-page
    floating table cannot interrupt the sentence it originally split.
    """
    records = {p['number']: p for p in book.pages}
    proof_list = book.profile.setdefault('cross_page_math_groups', [])
    # Page snapshots can be merged into another Book profile. Recover their
    # complete bound groups; never silently drop the independent output check.
    seen = {proof['id'] for proof in proof_list}
    element_ids = book.element_map()
    for element in book.elements:
        proof = element.metadata.get('cross_page_math')
        if proof and proof['id'] not in seen and all(s['element_id'] in element_ids for s in proof['segments']):
            proof_list.append(proof)
            seen.add(proof['id'])
    for page in sorted(records):
        if page+1 not in records:
            continue
        left_candidates = [e for e in book.elements if e.page == page and e.kind == 'paragraph'
                           and not e.metadata.get('render_suppressed')]
        right_candidates = [e for e in book.elements if e.page == page+1 and e.kind == 'paragraph'
                            and not e.metadata.get('render_suppressed')]
        if not left_candidates or not right_candidates:
            continue
        left, right = left_candidates[-1], right_candidates[0]
        if left.metadata.get('cross_page_math') or right.metadata.get('cross_page_math'):
            continue
        lm = list(TOKEN_RE.finditer(left.text))
        rm = list(TOKEN_RE.finditer(right.text))
        if not lm or not rm or left.text[lm[-1].end():].strip() or right.text[:rm[0].start()].strip():
            continue
        la, ra = book.assets.get(lm[-1][1]), book.assets.get(rm[0][1])
        if not la or not ra or la.kind != 'formula' or ra.kind != 'formula':
            continue
        if _balanced(la.text) or _balanced(ra.text) or not _balanced(la.text+ra.text):
            continue
        if not re.search(r'[+−×÷∧∨→↔=]\s*$', la.text) or not re.match(r'\s*[A-Za-zΑ-ω0-9¬(]', ra.text):
            continue
        if (left.bbox[3] < records[page]['height']-105
                or right.bbox[1] > records[page+1]['height']*.4
                or abs(left.bbox[0]-right.bbox[0]) > 36
                or left.style.get('columns', 1) != right.style.get('columns', 1)):
            continue
        segments = [{'asset_id': a.id, 'element_id': e.id, 'page': e.page,
                     'bbox': list(a.bbox), 'sha256': a.sha256}
                    for a, e in ((la, left), (ra, right))]
        gid = 'cross-page-math-'+digest(segments)[:20]
        proof = {'id': gid, 'segments': segments, 'source_pages': [page, page+1],
                 'combined_delimiters_balanced': True,
                 'source_order_evidence': 'last bottom-page prose and first next-page prose; unique token-boundary continuation'}
        for element, asset in ((left, la), (right, ra)):
            element.metadata.update(cross_page_math=proof, inline_group=gid, inline_join='space')
            asset.metadata['cross_page_math'] = proof
            element.group_id = gid
        # Keep each physical-page owner, including its complete source ledger.
        book.elements.remove(right)
        book.elements.insert(book.elements.index(left)+1, right)
        resolved = [issue for issue in book.issues if issue.get('element_id') in {left.id, right.id}
                    and issue['code'] in {'formula_boundary_review', 'formula_atomic_boundary_review'}
                    and any(issue.get('detail', '').endswith(a.text) for a in (la, ra))]
        for issue in resolved:
            book.profile.setdefault('resolved_source_issues', []).append({
                **issue, 'resolution_reason': 'adjacent-page expression linked; original vector segments retained',
                'cross_page_math_group': gid})
        book.issues = [issue for issue in book.issues if issue not in resolved]
        proof_list.append(proof)
    return proof_list


def check_joined_math_geometry(book, assets):
    """Independent output evidence: both segments must share one baseline."""
    issues = []
    active_elements = book.element_map()
    for proof in book.profile.get('cross_page_math_groups', []):
        segments = proof['segments']
        if not any(s['element_id'] in active_elements for s in segments):
            continue
        occurrences = [assets.get(s['asset_id'], []) for s in segments]
        reason = None
        if any(len(rows) != 1 for rows in occurrences):
            reason = '跨原页公式缺少唯一的成品对象位置'
        else:
            a, b = (rows[0] for rows in occurrences)
            aa, ba = (book.assets[s['asset_id']] for s in segments)
            baselines = (a['bbox'][1]+aa.baseline, b['bbox'][1]+ba.baseline)
            gap = b['bbox'][0]-a['bbox'][2]
            if a['page'] != b['page'] or abs(baselines[0]-baselines[1]) > .3 or not -.1 <= gap <= .5:
                reason = '跨原页公式未在同页按原尺寸、相同基线连续排入'
        if reason:
            issues.append({'code': 'cross_page_formula_not_atomic', 'severity': 'error', 'detail': reason,
                           'source_pages': proof['source_pages'], 'group_id': proof['id']})
    return issues
