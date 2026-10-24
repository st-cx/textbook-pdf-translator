"""Pure immutable whole-argument semantic review protocol; never edits prose."""
from copy import deepcopy
from pathlib import Path
from .models import digest,file_sha256,RULES_VERSION
from .argument_units import JOINT_PROTOCOL

def build_packet(unit,snapshot):
    rows=snapshot['elements'];members=unit['member_ids'];parent=unit['parent_id']
    for eid in members+[parent]:
        row=rows[eid]
        if row['needs_translation']and row['translation']is None:raise ValueError('awaiting_argument_translation:'+eid)
    payload={'schema':JOINT_PROTOCOL,'implementation_sha256':file_sha256(Path(__file__)),'unit_id':unit['id'],
      'definition':deepcopy(unit),'source_pdf_sha256':snapshot['source_pdf_sha256'],
      'ordered_member_ids':list(members),'member_roles':list(unit['member_roles']),
      'readonly_parent_id':parent,'readonly_context_ids':list(snapshot['context_ids']),
      'source_ordered_ordinals':deepcopy(snapshot['source_ordered_ordinals']),
      'elements':deepcopy(rows),'glossary_hash':snapshot['glossary_hash'],'style_hash':snapshot['style_hash'],
      'rules_version':RULES_VERSION,'individual_passes_imply_unit_pass':False,
      'instructions':'Review the complete ordered argument together. Preserve each premise, conclusion, negation, quantifier, implication direction and relation to its readonly parent/context exactly once. Do not assess or correct the author\'s intended argument validity. Return no translation/patch or per-element pass.'}
    binding=digest(payload)
    coverage={'ordered_member_ids':list(members),'entire_argument_reviewed':True,'premises_conclusion_direction_preserved':True,
      'negation_quantifiers_preserved':True,'no_omission_or_duplicate':True,'authored_argument_not_corrected':True}
    return {'unit_id':unit['id'],'argument_review_hash':binding,'payload':payload,
      'required_response':{'unit_id':unit['id'],'argument_review_hash':binding,'passed':True,'issues':[],'argument_coverage':coverage}}

def validate_response(packet,response):
    if not isinstance(response,dict)or set(response)!={'unit_id','argument_review_hash','passed','issues','argument_coverage'}:
        raise ValueError('Argument review-only envelope required; patches/individual results forbidden')
    if response['unit_id']!=packet['unit_id']or response['argument_review_hash']!=packet['argument_review_hash']:
        raise ValueError('Different or stale argument review identity')
    if type(response['passed'])is not bool or not isinstance(response['issues'],list)or any(not isinstance(v,str)or not v.strip()for v in response['issues']):
        raise ValueError('Invalid argument verdict')
    required=packet['required_response']['argument_coverage'];coverage=response['argument_coverage']
    if not isinstance(coverage,dict)or set(coverage)!=set(required)or coverage['ordered_member_ids']!=required['ordered_member_ids']:
        raise ValueError('Complete ordered argument coverage required')
    keys=set(required)-{'ordered_member_ids'}
    if any(type(coverage[k])is not bool for k in keys)or coverage['entire_argument_reviewed']is not True:
        raise ValueError('Explicit joint argument review required')
    if response['passed']and(response['issues']or any(not coverage[k]for k in keys)):
        raise ValueError('Passing argument verdict contradicts coverage')
    if not response['passed']and not response['issues']:raise ValueError('Failed argument review requires concrete issues')
    return deepcopy(response)
