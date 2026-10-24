"""Immutable source-anchored argument contract; review results never live here."""
from copy import deepcopy
from dataclasses import asdict
import math,re
from .models import TOKEN_RE,digest

PROFILE_KEY='argument_units_v1'
MEMBER_KEY='argument_member_v1'
SCHEMA='source-anchored-argument-unit-v1'
JOINT_PROTOCOL='source-anchored-argument-joint-review-v1'
_KEYS={'schema','id','source_pdf_sha256','origin_book_sha256','source_facts_sha256','parent_id',
 'parent_full_source_sha256','source_page','printed_page','original_source_owner_ids','original_source_full_sha256',
 'member_ids','member_roles','member_source_full_sha256','member_source_glyph_ids','complete_immutable_asset_sha256',
 'marker','divider','layout','joint_review','authored_argument_validity'}

def _sha(v):return isinstance(v,str)and bool(re.fullmatch('[0-9a-f]{64}',v))

def registry(book):
    value=book.profile.get(PROFILE_KEY,{})
    if not isinstance(value,dict):raise ValueError('Argument registry must be an explicit dictionary')
    seen=set()
    for uid,u in value.items():
        if (not isinstance(u,dict)or set(u)!=_KEYS or u.get('id')!=uid or not re.fullmatch(r'[A-Za-z0-9_.-]+',uid)
            or u.get('schema')!=SCHEMA or u.get('source_pdf_sha256')!=book.source_sha256):
            raise ValueError('Invalid immutable argument source identity/schema')
        members=u['member_ids'];roles=u['member_roles'];layout=u['layout'];divider=u['divider'];marker=u['marker']
        if (not isinstance(members,list)or not 3<=len(members)<=128 or len(set(members))!=len(members)
            or roles!=['premise']*(len(members)-1)+['conclusion'] or u['parent_id']in members
            or seen.intersection(members+[u['parent_id']])):raise ValueError('Overlapping/unordered argument roles or parent')
        seen.update(members+[u['parent_id']])
        if (not all(_sha(u.get(k))for k in('parent_full_source_sha256','origin_book_sha256','source_facts_sha256'))
            or set(u['member_source_full_sha256'])!=set(members)or not all(_sha(v)for v in u['member_source_full_sha256'].values())
            or set(u['member_source_glyph_ids'])!=set(members)):raise ValueError('Incomplete argument source evidence')
        gids=[g for eid in members for g in u['member_source_glyph_ids'][eid]]
        if not gids or len(gids)!=len(set(gids)):raise ValueError('Source glyph partition duplicates or empties an argument')
        if (set(u['joint_review'])!={'requires_joint_review','individual_passes_imply_unit_pass','protocol','mutable_verdict_storage','legacy_semantic_unit_v1_is_applicable'}
            or u['joint_review']!={'requires_joint_review':True,'individual_passes_imply_unit_pass':False,'protocol':JOINT_PROTOCOL,
              'mutable_verdict_storage':'separate_transactional_Store_table_not_source_profile','legacy_semantic_unit_v1_is_applicable':False}
            or u['authored_argument_validity']!='not_inferred_or_corrected'):raise ValueError('Mutable verdict/legacy semantic-unit substitution forbidden')
        role=layout.get('role');size={'body':10.5,'exercise':9.5}.get(role)
        if (size is None or layout.get('fixed_text_size_pt')!=size
            or layout.get('protected_native_objects')!='original_dimensions_and_baseline_no_scaling'
            or layout.get('short_block')!='parent_and_all_members_together'
            or layout.get('long_block')!='between_complete_premise_members_only'
            or layout.get('atomic_tail_member_ids')!=[members[-2],'divider',members[-1]]
            or layout.get('overlong_premise_or_tail')!='block_no_font_object_or_line_compression'):
            raise ValueError('Unsupported argument pagination or typography policy')
        if (divider.get('primitive_schema')!='source-anchored-argument-divider-v1'
            or divider.get('semantic_role')!='argument_divider' or divider.get('page')!=u['source_page']
            or divider.get('between_member_ids')!=members[-2:]
            or divider.get('color_space')!='DeviceCMYK'or divider.get('stroke_components')!=[1,0,0,0]
            or divider.get('preserve_raw_width_pt')!=.504 or not _sha(divider.get('source_draw_state_sha256'))
            or digest(divider.get('complete_original_draw_state'))!=divider['source_draw_state_sha256']):
            raise ValueError('Argument divider source path/paint/role is unverified')
        length=divider.get('preserve_raw_length_pt');state=divider['complete_original_draw_state']
        if (not isinstance(length,(float,int))or isinstance(length,bool)or not math.isfinite(length)or length<=0
            or state.get('raw_length_pt')!=length or state.get('raw_width_pt')!=.504
            or state.get('original_color_space')!='DeviceCMYK'or state.get('original_components')!=[1,0,0,0]
            or state.get('page')!=u['source_page']or state.get('graphic_id')!=divider.get('graphic_id')):
            raise ValueError('Argument divider length/width/source space changed')
        strategy=marker.get('strategy')
        if (strategy not in('inside_protected_object','separate_original_native_asset')
            or marker.get('extra_marker_drawing_forbidden')is not True
            or marker.get('marker_is_inside_original_complete_object')is not(strategy=='inside_protected_object')
            or marker.get('conclusion_must_start_with_original_marker_token')is not(strategy=='separate_original_native_asset')
            or not marker.get('complete_original_asset_ids')):raise ValueError('Invalid original conclusion marker contract')
    return value

def checked_units(book,*,scoped=False):
    units=registry(book);lookup=book.element_map();positions={e.id:i for i,e in enumerate(book.elements)};result={}
    for uid,u in units.items():
        chain=[u['parent_id']]+u['member_ids'];present=set(chain)&lookup.keys()
        if scoped and not present:continue
        if present!=set(chain):raise ValueError('Partial argument source scope; parent/members may not be dropped')
        ordinals=[positions[e]for e in chain]
        if ordinals!=list(range(ordinals[0],ordinals[0]+len(chain))):raise ValueError('Argument parent/member source adjacency changed')
        parent=lookup[u['parent_id']]
        if digest(asdict(parent))!=u['parent_full_source_sha256']:raise ValueError('Argument parent source changed')
        refs=set()
        for ordinal,eid in enumerate(u['member_ids']):
            e=lookup[eid]
            if digest(asdict(e))!=u['member_source_full_sha256'][eid]:raise ValueError('Argument member full source changed')
            meta=e.metadata.get(MEMBER_KEY,{})
            expected={'schema':'source-anchored-argument-member-v1','unit_id':uid,'role':u['member_roles'][ordinal],
             'member_ordinal':ordinal,'parent_id':u['parent_id'],'source_page':e.page,'layout_role':u['layout']['role'],
             'source_glyph_ids':u['member_source_glyph_ids'][eid],'joint_review_protocol':JOINT_PROTOCOL}
            if any(meta.get(k)!=v for k,v in expected.items()):raise ValueError('Argument member metadata/role mismatch')
            if e.metadata.get('render_suppressed')or e.kind not in('paragraph','formula','exercise')or e.page!=u['source_page']:
                raise ValueError('Unsupported/hidden argument source member')
            if e.metadata.get('glyph_ids')!=u['member_source_glyph_ids'][eid]:raise ValueError('Argument source glyph partition mismatch')
            refs|=set(e.asset_ids)|set(TOKEN_RE.findall(e.text))
        if refs!=set(u['complete_immutable_asset_sha256']):raise ValueError('Argument protected object scope incomplete')
        for aid in refs:
            if aid not in book.assets or digest(asdict(book.assets[aid]))!=u['complete_immutable_asset_sha256'][aid]:
                raise ValueError('Argument complete immutable asset identity changed')
        c=lookup[u['member_ids'][-1]];markers=u['marker']['complete_original_asset_ids']
        if not set(markers)<=set(TOKEN_RE.findall(c.text)):raise ValueError('Conclusion lost its original complete marker asset')
        if u['marker']['conclusion_must_start_with_original_marker_token']and not c.text.startswith('⟦F:'+markers[0]+'⟧'):
            raise ValueError('Conclusion marker must start the source conclusion')
        page=next((p for p in book.pages if p.get('number',p.get('page'))==u['source_page']),None)
        if page is None:raise ValueError('Argument original page missing')
        graphics=[g for g in page.get('graphics_coverage',[])if g.get('id')==u['divider']['graphic_id']]
        if len(graphics)!=1:raise ValueError('Argument original graphic identity ambiguous')
        g=graphics[0]
        if (g.get('disposition')!='reconstructed-source-primitive'or g.get('semantic_role')!='argument_divider'
            or g.get('argument_unit_id')!=uid or g.get('primitive_schema')!='source-anchored-argument-divider-v1'
            or g.get('source_draw_state_sha256')!=u['divider']['source_draw_state_sha256']or g.get('asset_ids')!=[]
            or g.get('requires_validated_reconstruction')is not True):raise ValueError('Unbound reconstructed source graphics primitive')
        before=u['divider']['complete_original_draw_state']['complete_before_graphics_ledger']
        if g.get('source_original_graphic_before_argument_reclassification')!=before:raise ValueError('Original graphic coverage history changed')
        if any(g.get(k)!=v for k,v in before.items()if k not in('disposition','reason')):
            raise ValueError('Reconstructed primitive altered original graphic geometry/paint/clip')
        result[uid]=u
    extras={e.id for e in book.elements if MEMBER_KEY in e.metadata}-{eid for u in result.values()for eid in u['member_ids']}
    if extras:raise ValueError('Argument members have no validated source registry')
    return result

def source_issues(book):
    if not book.profile.get(PROFILE_KEY)and not any(MEMBER_KEY in e.metadata for e in book.elements):
        # A new graphics disposition must never bypass source validation alone.
        if not any(g.get('disposition')=='reconstructed-source-primitive'for p in book.pages for g in p.get('graphics_coverage',[])):return []
    try:
        units=checked_units(book,scoped=True)
        expected={(u['source_page'],u['divider']['graphic_id'])for u in units.values()}
        for p in book.pages:
            for g in p.get('graphics_coverage',[]):
                if g.get('disposition')=='reconstructed-source-primitive'and(p.get('number',p.get('page')),g.get('id'))not in expected:
                    raise ValueError('Unregistered reconstructed source primitive')
        return []
    except (ValueError,KeyError,TypeError)as exc:return [{'code':'source_argument_contract_invalid','severity':'error','detail':str(exc)}]

def argument_member_fields(element,book):
    """Opt-in identity in task/rule/review payloads; default is exactly {}."""
    units=registry(book);matching=[u for u in units.values()if element.id in u['member_ids']or element.id==u['parent_id']]
    if not matching:
        if MEMBER_KEY in element.metadata:raise ValueError('Argument member lost its explicit source unit')
        return {}
    if len(matching)!=1:raise ValueError('Ambiguous argument source relationship')
    u=matching[0];parent=element.id==u['parent_id'];expected=u['parent_full_source_sha256']if parent else u['member_source_full_sha256'][element.id]
    if digest(asdict(element))!=expected:raise ValueError('Current argument source record differs from frozen relation')
    return {'argument_policy':{'schema':'source-anchored-argument-member-binding-v1','unit_id':u['id'],
      'source_unit_sha256':digest(u),'role':'readonly_parent'if parent else u['member_roles'][u['member_ids'].index(element.id)],
      'parent_id':u['parent_id'],'ordered_member_ids':list(u['member_ids']),
      'member_source_full_sha256':deepcopy(u['member_source_full_sha256']),
      'parent_full_source_sha256':u['parent_full_source_sha256'],'joint_review_protocol':JOINT_PROTOCOL}}

def frozen_argument_matches(row,element,book):
    try:return row.get('argument_policy')==argument_member_fields(element,book).get('argument_policy')
    except (ValueError,KeyError,TypeError):return False

def task_context(book,element_ids):
    units=registry(book);wanted=set(element_ids);lookup=book.element_map();out={}
    for uid,u in units.items():
        chain=[u['parent_id']]+u['member_ids']
        if not wanted.intersection(chain):continue
        if not set(chain)<=lookup.keys():raise ValueError('Argument task lost complete read-only source context')
        out[uid]={'definition':deepcopy(u),'ordered_readonly_source':[
         {'id':eid,'text':lookup[eid].text,'source_hash':lookup[eid].source_hash,'readonly':True,
          **argument_member_fields(lookup[eid],book)}for eid in chain]}
    return out
