"""Private, explicit layout-plan overlay; source and translation objects stay intact."""
from dataclasses import asdict, replace
import re
from .models import digest

PLAN_KEY = 'layout_only_exercise_flow_v1'

def annotate(elements, plan, page_info=None):
    if plan is None:
        return elements
    if plan.get('schema') != 'verified-original-exercise-flow-layout-plan-v2':
        raise ValueError('Unknown layout flow protocol')
    lookup = {e.id: e for e in elements}
    if len(lookup) != len(elements):
        raise ValueError('Duplicate source element ID')
    overrides = {}
    positions = {e.id: i for i,e in enumerate(elements)}
    for unit in plan.get('units', []):
        proof = unit['source_proof']
        if not all(proof.get(k) is True for k in ('native_fields_match_current_ledger', 'unique_owner_and_complete_glyph_span',
                   'geometric_source_column_coherent_per_original_page', 'source_font_size_coherent', 'plain_text_structure_match',
                   'no_original_container_drawing', 'original_context_visually_approved')):
            raise ValueError('A narrow original exercise chain needs all source proof guards')
        if unit.get('intentional_source_sidebar') or not unit['members']:
            raise ValueError('A real sidebar or empty source chain cannot be converted')
        mids=[m['id'] for m in unit['members']]
        present=[eid for eid in mids if eid in lookup]
        if not present:
            continue
        if len(present)!=len(mids):
            raise ValueError('A source part contains only some members; parent/children cannot be silently dropped')
        anchor=unit.get('anchor_id')
        if not anchor or anchor not in mids or anchor!=mids[0] or anchor not in lookup:
            raise ValueError('The real source anchor must exist and be the first member')
        if len(mids)!=len(set(mids)) or any(eid not in lookup for eid in mids):
            raise ValueError('Unknown or repeated source member')
        indices=[positions[eid]for eid in mids]
        if indices!=list(range(indices[0],indices[0]+len(indices))):
            raise ValueError('Source members must be contiguous; no sidebar/figure/prose may be skipped')
        a=lookup[anchor];role=unit.get('anchor_role')
        if role=='numbered_exercise_stem':
            if a.kind!='exercise' or not re.match(r'^\d+[.)]\s',a.text):
                raise ValueError('Numbered anchor is not a source exercise stem')
        elif role=='lettered_continuation_prefix':
            if indices[0]!=0 or not unit.get('original_prefix_parent_outside_selection') or not re.match(r'^a\s*[.)]\s',a.text):
                raise ValueError('Lettered prefix needs a proven first-visible continuation role')
        else:raise ValueError('Explicit numbered-stem or lettered-continuation anchor role required')
        childlabels=[m[1]for eid in mids if (m:=re.match(r'^([a-z])\s*[.)]\s',lookup[eid].text))]
        if len(childlabels)<2 or childlabels!=list('abcdefghijklmnopqrstuvwxyz'[:len(childlabels)]):
            raise ValueError('Source option labels must form one complete contiguous a/b/... sequence')
        joined={};joins=unit.get('inline_joins',[])
        for join in joins:
            left,right=join['left_id'],join['right_id'];mode=join['mode']
            if left not in mids or right not in mids or positions[right]!=positions[left]+1 or right in joined:
                raise ValueError('Inline join must bind two unique adjacent current members')
            if mode not in {'space','word'} or join.get('lossless_original_boundary_verified')is not True:
                raise ValueError('Unknown or unverified inline join mode')
            if join.get('left_record_sha256')!=digest(asdict(lookup[left])) or join.get('right_record_sha256')!=digest(asdict(lookup[right])):
                raise ValueError('Inline boundary identity is stale')
            if mode=='word' and not(lookup[left].metadata.get('inline_group') and lookup[left].metadata.get('inline_group')==lookup[right].metadata.get('inline_group') and lookup[right].metadata.get('inline_join')=='word'):
                raise ValueError('New word joins need an existing source word-boundary proof')
            if mode=='space' and (lookup[left].text.rstrip().endswith(('-', '\u00ad')) or join.get('evidence_kind')!='unhyphenated_source_line_boundary'):
                raise ValueError('Space join cannot conceal an unresolved source hyphen')
            group=join['inline_group'];joined[left]=(group,mode);joined[right]=(group,mode)
        previous = -1
        last=None;sizes=[]
        for member in unit['members']:
            eid = member['id']
            if eid not in lookup or eid in overrides:
                raise ValueError('Unknown or conflicting layout chain member')
            e = lookup[eid]
            if digest(asdict(e)) != member['original_record_sha256']:
                raise ValueError('Layout chain source record is stale')
            index = next(i for i, item in enumerate(elements) if item.id == eid)
            if index <= previous:
                raise ValueError('Layout overlay cannot reorder source elements')
            previous = index
            if e.kind not in {'paragraph', 'exercise', 'subpart', 'sidebar'} or e.metadata.get('role') in {'frontmatter', 'chapter_contents', 'resource_pointer'}:
                raise ValueError('Unsupported genuine source role')
            if e.metadata.get('banner_background_asset_ids') or e.metadata.get('span_columns') or e.style.get('span_columns') or e.metadata.get('figure_label_ids') or e.metadata.get('parent_figure_id'):
                raise ValueError('Full-width banners, graphic labels and true spanning content stay outside exercises')
            if e.kind=='sidebar' and e.metadata.get('structure_class')!='plain text':
                raise ValueError('A genuine or unknown sidebar cannot be recovered as a plain exercise')
            if member['geometric_source_column'] not in (0, 1):
                raise ValueError('No uniquely proven source column')
            if page_info is None or e.page not in page_info:raise ValueError('Source page geometry required')
            page=page_info[e.page];split=page.get('column_split');box=e.bbox
            if page.get('columns')!=2 or not isinstance(split,(int,float)) or len(box)!=4 or box[2]-box[0]>float(page['width'])*.44:
                raise ValueError('Source region is not a narrow original two-column exercise')
            col=0 if box[2]<=split+8 else 1 if box[0]>=split-8 else None
            if col!=member['geometric_source_column']:raise ValueError('Declared source column contradicts native geometry')
            if last and (e.chapter!=last.chapter or e.page-last.page not in (0,1)):
                raise ValueError('Unknown source chapter/page gap')
            if last and e.page==last.page and col!=previous_column:raise ValueError('Original columns changed inside one narrow source chain')
            size=e.style.get('source_size')
            if not isinstance(size,(int,float)):raise ValueError('Original source body size required')
            sizes.append(float(size))
            if max(sizes)-min(sizes)>.001:raise ValueError('Source font sizes are not coherent')
            last=e;previous_column=col
            metadata = {**e.metadata, 'layout_only_original_kind': e.kind, 'layout_only_flow_unit': unit['id']}
            if member.get('inline_group'):
                if eid not in joined or joined[eid]!=(member['inline_group'],member.get('inline_join')):
                    raise ValueError('Inline member lacks the exact lossless adjacency proof')
                metadata['inline_group'] = member['inline_group']
                metadata['inline_join'] = member['inline_join']
            elif member.get('inline_join') or eid in joined:raise ValueError('Inline mode/group scope differs')
            overrides[eid] = replace(e, kind='exercise', group_id=unit['anchor_id'],
                style={**e.style, 'columns': 2, 'column': member['geometric_source_column']}, metadata=metadata)
    return [overrides.get(e.id, e) for e in elements]

def check_reading_order(plan, traces, geometry, visible_ids=None):
    """Fail a proven narrow exercise chain that resets its output column flow.

    This check intentionally needs an explicit source-chain plan. It does not
    impose one column-major order on genuine full-width banners or sidebars.
    """
    starts = {}
    for trace in traces:
        if trace.get('type') == 'element' and trace.get('phase') == 'start':
            starts.setdefault(trace['id'], []).append(trace)
    midpoint = geometry['left'] + (geometry['width'] - geometry['left'] - geometry['right']) / 2
    issues = []
    for unit in plan['units']:
        if visible_ids is not None:
            member_ids={m['id']for m in unit['members']};present=member_ids&set(visible_ids)
            if not present:continue
            if present!=member_ids:
                issues.append({'code':'exercise_chain_partial_source_scope','unit_id':unit['id'],'severity':'error'})
                continue
        previous = None
        for member in unit['members']:
            eid = member['id']; rows = starts.get(eid, [])
            if not rows:
                issues.append({'code': 'exercise_chain_missing_trace', 'unit_id': unit['id'], 'element_id': eid, 'severity': 'error'})
                continue
            row = rows[0]
            rank = (int(row['page']), 0 if float(row['x']) < midpoint else 1, float(row['y']))
            if int(row.get('columns', 1)) != 2:
                issues.append({'code': 'exercise_chain_column_region_reset', 'unit_id': unit['id'], 'element_id': eid,
                    'severity': 'error', 'actual_columns': row.get('columns'), 'expected_proven_narrow_flow_columns': 2, 'actual_reading_rank': list(rank)})
            if previous and rank < previous[1]:
                issues.append({'code': 'exercise_chain_visual_reading_order_reversed', 'unit_id': unit['id'], 'element_id': eid,
                    'predecessor_id': previous[0], 'predecessor_reading_rank': list(previous[1]), 'actual_reading_rank': list(rank), 'severity': 'error'})
            previous = (eid, rank)
    return issues
