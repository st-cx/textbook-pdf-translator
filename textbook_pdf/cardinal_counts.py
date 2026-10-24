"""Explicit opt-in finite role-bound counts for bit-string questions.

All other numeric schemas delegate to the unchanged numeric_policy module.
This new schema binds its complete source and both implementation dependencies;
independent semantic review remains mandatory.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import hashlib,json,re
from pathlib import Path
from .numeric_policy import _source_binding as legacy_source_binding
from .numeric_policy import implementation_sha256 as legacy_numeric_sha256
from .numbers import numeric_fingerprint

SCHEMA='explicit-bitstring-cardinal-counts-v1'
WORDS='zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
CHINESE=['零','一','二','三','四','五','六','七','八','九','十','十一','十二','十三','十四','十五','十六','十七','十八','十九','二十']
VALUES={**{w:i for i,w in enumerate(WORDS)},**{w:i for i,w in enumerate(CHINESE)}}
CARD=r'(?:[0-9]+|'+ '|'.join(sorted(VALUES,key=len,reverse=True))+r')'
EN_LENGTH=re.compile(r'\bbit strings of length\s+(?P<n>'+CARD+r')(?=\s+do not contain\b)',re.I)
EN_RUN=re.compile(r'\bdo not contain\s+(?P<n>'+CARD+r')\s+consecutive\s+(?P<bit>[01])s(?=[?.!,;:]|\s|$)',re.I)
CN_LENGTH=re.compile(r'长度(?:为|是|等于)?\s*(?P<n>'+CARD+r')(?=\s*(?:且|并|的(?:位串|比特串)|[，,]))',re.I)
CN_RUN1=re.compile(r'(?:不包含|不含|没有)\s*(?P<n>'+CARD+r')\s*个?连续(?:的)?\s*(?P<bit>[01])(?=的?(?:位串|比特串)|[？?。，,]|$)',re.I)
CN_RUN2=re.compile(r'(?:不包含|不含|没有)\s*连续(?:的)?\s*(?P<n>'+CARD+r')\s*个\s*(?P<bit>[01])(?=的?(?:位串|比特串)|[？?。，,]|$)',re.I)
KEYS={'schema','implementation_sha256','schema_dispatch_sha256','legacy_numeric_policy_sha256','source_hash','source_element_id','source_record_sha256','source_events','independent_semantic_review_required'}

def implementation_sha256():return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
def value(raw):
    raw=raw.lower()
    if raw.isascii()and raw.isdecimal():
        if not re.fullmatch(r'0|[1-9][0-9]?',raw):raise ValueError('Unsupported cardinal token')
        n=int(raw)
    elif raw in VALUES:n=VALUES[raw]
    else:raise ValueError('Unrecognized cardinal token')
    if not 0<=n<=20:raise ValueError('Cardinal outside finite supported0..20')
    return n

def events(text,*,source=False):
    if '⟦'in text or '⟧'in text:raise ValueError('Protected math requires a stronger source-specific schema')
    patterns=[('length',EN_LENGTH),('run',EN_RUN)]if source else [('length',CN_LENGTH),('run',CN_RUN1),('run',CN_RUN2),('length',EN_LENGTH),('run',EN_RUN)]
    found=[]
    for role,pattern in patterns:
        for m in pattern.finditer(text):
            start,end=m.span('n')
            found.append({'role':role,'literal':m['n'],'start':start,'end':end,'value':value(m['n']),**({'bit':int(m['bit'])}if role=='run'else{})})
    if Counter(e['role']for e in found)!=Counter({'length':1,'run':1}):raise ValueError('Exactly one complete source-anchored length and negative run count are required')
    found.sort(key=lambda x:x['start'])
    if found[0]['role']!='length':raise ValueError('Unknown bit-string role order')
    return found

def make_policy(element):
    if any(k in element.metadata for k in ['percentage_unit_policy','century_policy']):raise ValueError('Mixed numeric policy unsupported')
    if 'numeric_policy'in element.metadata and element.metadata['numeric_policy'].get('schema')!=SCHEMA:raise ValueError('Mixed numeric policy unsupported')
    src=events(element.text,source=True)
    dispatch=Path(__file__).with_name('numeric_policy_dispatch.py')
    return {'schema':SCHEMA,'implementation_sha256':implementation_sha256(),'schema_dispatch_sha256':hashlib.sha256(dispatch.read_bytes()).hexdigest(),'legacy_numeric_policy_sha256':legacy_numeric_sha256(),
        'source_hash':element.source_hash,**legacy_source_binding(element),'source_events':src,'independent_semantic_review_required':True}

def policy_fields(element):
    if 'numeric_policy'not in element.metadata:return {}
    p=element.metadata['numeric_policy']
    if not isinstance(p,dict)or set(p)!=KEYS or make_policy(element)!=p:raise ValueError('Unsupported/stale cardinal source-policy binding')
    return {'numeric_policy':deepcopy(p)}

def check(element,target):
    try:
        p=policy_fields(element)['numeric_policy'];src=p['source_events'];dst=events(target)
        identity=lambda es:{e['role']:(e['value'],e.get('bit'))for e in es}
        if identity(src)!=identity(dst):raise ValueError('Cardinal value/bit/role changed')
        def remove(text,es):
            for e in sorted(es,key=lambda x:x['start'],reverse=True):text=text[:e['start']]+' '+text[e['end']:]
            return text
        a,b=remove(element.text,src),remove(target,dst)
        if numeric_fingerprint(a)!=numeric_fingerprint(b):raise ValueError('Other legacy numbers changed')
        return {'equivalent':True,'reason':None,'source_events':src,'target_events':dst,'source_remaining':a,'target_remaining':b,'independent_semantic_review_required':True}
    except (ValueError,TypeError,KeyError)as e:return {'equivalent':False,'reason':str(e),'independent_semantic_review_required':True}

def proposed_facade_fields(element):
    from .numeric_policy_dispatch import numeric_policy_fields
    return numeric_policy_fields(element)
