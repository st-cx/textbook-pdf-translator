"""Dispatch explicitly opted-in cardinal counts without changing legacy schemas."""
from . import numeric_policy as legacy
from . import cardinal_counts as cardinal

def is_cardinal(element):
    p=element.metadata.get('numeric_policy')
    return isinstance(p,dict)and p.get('schema')==cardinal.SCHEMA

def numeric_policy_fields(element):
    return cardinal.policy_fields(element)if is_cardinal(element)else legacy.numeric_policy_fields(element)

def frozen_policy_matches(row,element):
    if not is_cardinal(element):return legacy.frozen_policy_matches(row,element)
    fields=numeric_policy_fields(element)
    return ('numeric_policy'in row)==bool(fields)and row.get('numeric_policy')==fields.get('numeric_policy')

def policy_number_issues(element,target):
    if not is_cardinal(element):return legacy.policy_number_issues(element,target)
    checked=cardinal.check(element,target)
    if checked['equivalent']:return []
    return [{'code':'number_mismatch','severity':'error','element_id':element.id,'page':element.page,
             'detail':'显式基数词策略或其余正文数字不可核验：'+str(checked['reason'])}]
