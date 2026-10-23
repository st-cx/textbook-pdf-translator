"""Opt-in explicit Decimal amounts; all other syntax stays on legacy checks.

Private prototype. No global numeric validator or RULES_VERSION replacement.
"""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

VERSION = 'explicit-decimal-amounts-v2'
SCALES = {'thousand':3,'million':6,'billion':9,'trillion':12,
          '万':4,'百万':6,'千万':7,'亿':8,'十亿':9,'万亿':12}
TOKEN = re.compile(r'⟦F:([A-Za-z0-9_.-]+)⟧')
RAW = r'[+−-]?[0-9]+(?:,[0-9]+)*(?:\.[0-9]+)?'
UNIT = r'thousand(?![A-Za-z0-9_])|million(?![A-Za-z0-9_])|billion(?![A-Za-z0-9_])|trillion(?![A-Za-z0-9_])|万亿|十亿|千万|百万|亿|万'
PERCENT = r'(?:%|％|percent(?![A-Za-z0-9_])|per\s+cent(?![A-Za-z0-9_]))'
AMOUNT = re.compile(r'(?<![A-Za-z0-9_.])('+RAW+r')\s*('+UNIT+r')',re.I)
BARE = re.compile(r'(?<![A-Za-z0-9_.])'+RAW)
POLICY_KEYS = {'schema','implementation_sha256','source_hash','source_amounts',
               'chinese_classifiers','independent_semantic_review_required',
               'source_element_id','source_record_sha256'}


def implementation_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _source_binding(element):
    record=asdict(element)
    record['metadata'].pop('numeric_policy',None)
    # Rendering-only association must not invalidate semantic/numeric identity.
    # Unknown metadata (including semantic units) remains bound conservatively.
    record['metadata'].pop('display_asset_ids',None)
    encoded=json.dumps(record,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    return {'source_element_id':element.id,'source_record_sha256':hashlib.sha256(encoded).hexdigest()}


def canonical(raw, exponent=0):
    raw=raw.replace('−','-')
    if len(raw)>256:raise ValueError('Explicit quantity exceeds bounded decimal precision')
    integer=raw.lstrip('+-').split('.',1)[0]
    if ','in integer and not re.fullmatch(r'[0-9]{1,3}(?:,[0-9]{3})+',integer):
        raise ValueError('Explicit quantity has ambiguous comma grouping')
    d=Decimal(raw.replace(',',''));sign,digits,power=d.as_tuple()
    value=Decimal((sign,digits,power+exponent))
    if value.is_zero():return '0'
    out=format(value,'f')
    return out.rstrip('0').rstrip('.')if'.'in out else out


def _unprotected(text):
    return TOKEN.sub(lambda m:' '*len(m[0]),text)


def normalize_explicit_amounts(text,classifiers):
    source=_unprotected(text);pieces=[];cursor=0;amounts=[]
    for m in AMOUNT.finditer(source):
        unit=m[2].lower();end=m.end();tail=source[end:]
        if re.match(r'\s*'+PERCENT,tail,re.I)or source[max(0,m.start()-3):m.start()]=='百分之':
            raise ValueError('Magnitude combined with a percentage requires a different policy')
        if unit[0].isascii():
            if re.match(r'\s*(?:'+UNIT+r')',tail,re.I):
                raise ValueError('Stacked magnitude words are not an explicit amount')
        else:
            # A following Chinese word may start with 万 (万维网/万能), so
            # accept only the per-element classifier or a terminal boundary.
            if tail and not re.match(r'[，。、,;；:：)）.!?？\]]|$',tail)and not any(tail.startswith(c)for c in classifiers):
                continue
            if any(tail.startswith(c)and re.match(r'\s*'+PERCENT,tail[len(c):],re.I)for c in classifiers):
                raise ValueError('Quantity classifier is followed by a percentage')
        value=canonical(m[1],SCALES[unit])
        pieces.append(source[cursor:m.start()]);pieces.append(value);cursor=end
        amounts.append({'literal':m[0],'start':m.start(),'end':end,'unit':unit,'value':value})
    pieces.append(source[cursor:])
    return ''.join(pieces),amounts


def _matching_bare_amounts(text,values):
    out=[]
    for m in BARE.finditer(text):
        try:value=canonical(m[0])
        except ValueError:continue  # unrelated legacy syntax is not reparsed
        if value.lstrip('-')in values:out.append((m,value))
    return out


def make_policy(element,*,chinese_classifiers=('个',)):
    if not chinese_classifiers or any(not isinstance(c,str)or not c or len(c)>4 for c in chinese_classifiers):
        raise ValueError('Explicit classifier list required')
    normalized,amounts=normalize_explicit_amounts(element.text,chinese_classifiers)
    if not amounts:raise ValueError('No explicit Arabic coefficient + supported magnitude')
    values={a['value'].lstrip('-')for a in amounts}
    # Do not guess which equal-valued number belongs to the quantity. Such
    # cases require a stronger future policy rather than an association guess.
    matches=[value.lstrip('-')for m,value in _matching_bare_amounts(normalized,values)]
    if Counter(matches)!=Counter(a['value'].lstrip('-')for a in amounts):
        raise ValueError('Explicit source amount collides with an unrelated numeric event')
    return {'schema':VERSION,'implementation_sha256':implementation_sha256(),
        'source_hash':element.source_hash,'source_amounts':amounts,
        'chinese_classifiers':list(chinese_classifiers),'independent_semantic_review_required':True,
        **_source_binding(element)}


def numeric_policy_fields(element):
    if 'numeric_policy'not in element.metadata:return {}
    p=element.metadata['numeric_policy']
    allowed=({DATE_VERSION:DATE_KEYS,NAMED_MONTH_VERSION:NAMED_MONTH_KEYS}.get(p.get('schema'),POLICY_KEYS)
             if isinstance(p,dict) else POLICY_KEYS)
    if (not isinstance(p,dict)or set(p)!=allowed or p.get('schema')not in (VERSION,DATE_VERSION,NAMED_MONTH_VERSION)
            or p.get('implementation_sha256')!=implementation_sha256()
            or p.get('independent_semantic_review_required')is not True
            or p.get('source_hash')!=element.source_hash):
        raise ValueError('Unsupported, stale or unbound numeric policy')
    expected=(make_bibliography_date_policy(element) if p['schema']==DATE_VERSION
              else make_named_month_policy(element) if p['schema']==NAMED_MONTH_VERSION
              else make_policy(element,chinese_classifiers=tuple(p.get('chinese_classifiers',()))))
    if expected!=p:
        raise ValueError('Numeric policy does not match the exact source amounts')
    return {'numeric_policy':deepcopy(p)}


def frozen_policy_matches(row,element):
    expected=numeric_policy_fields(element)
    return (('numeric_policy'in row)==bool(expected)
            and ('numeric_policy'not in row or row['numeric_policy']==expected['numeric_policy']))


def check_policy_numbers(element,target):
    """Narrow quantity rewrite then legacy fingerprints, never broad parsing."""
    from .numbers import numeric_fingerprint
    try:
        policy=numeric_policy_fields(element)['numeric_policy']
        if policy['schema']==DATE_VERSION:
            return check_bibliography_date_numbers(element,target,policy)
        if policy['schema']==NAMED_MONTH_VERSION:
            return check_named_month_numbers(element,target,policy)
        a,src=normalize_explicit_amounts(element.text,policy['chinese_classifiers'])
        b,dst=normalize_explicit_amounts(target,policy['chinese_classifiers'])
        expected=Counter(s['value']for s in src)
        # Units and signs cannot be discarded by the legacy digit-only layer.
        if not Counter(v['value']for v in dst)<=expected:
            return {'equivalent':False,'reason':'explicit_quantity_value_or_unit_changed','order_review_required':False}
        expected_abs={v.lstrip('-')for v in expected}
        matched=_matching_bare_amounts(b,expected_abs)
        if any(re.match(r'\s*'+PERCENT,b[m.end():],re.I)
               or b[max(0,m.start()-3):m.start()]=='百分之'for m,value in matched):
            return {'equivalent':False,'reason':'quantity_became_a_percentage','order_review_required':False}
        actual_values=[value for m,value in matched]
        if Counter(actual_values)!=expected:
            return {'equivalent':False,'reason':'explicit_quantity_sign_count_or_value_changed','order_review_required':False}
        # Only numbers already proved to be this source's exact quantities
        # may lose grouping commas. Every other numeric/date/reference form
        # remains byte-for-byte on the legacy validation path.
        for m,value in reversed(matched):b=b[:m.start()]+value+b[m.end():]
        equal=numeric_fingerprint(a)==numeric_fingerprint(b)
        seq=lambda s:re.findall(r'(?<![A-Za-z0-9_])\d+(?:\.\d+)?',s)
        return {'equivalent':equal,'reason':None if equal else'legacy_remaining_numbers_differ',
            'order_review_required':equal and seq(a)!=seq(b),
            'source_rewritten':a,'target_rewritten':b,'source_amounts':src,'target_amounts':dst,
            'independent_semantic_review_required':True}
    except (ValueError,TypeError,KeyError)as error:
        return {'equivalent':False,'reason':'explicit_numeric_policy_unverifiable:'+str(error),
                'order_review_required':False}


def policy_number_issues(element,target):
    result=check_policy_numbers(element,target)
    base={'element_id':element.id,'page':element.page}
    if not result['equivalent']:
        return [{**base,'code':'number_mismatch','severity':'error',
            'detail':'显式数字策略或其余正文数字不一致/不可核验：'+str(result['reason'])}]
    if result['order_review_required']:
        return [{**base,'code':'numeric_order_changed','severity':'warning',
            'detail':'显式数量级精确等值，数值语序已调整；独立语义复核须核对数值与对象的对应关系。'}]
    return []


# Date policy is independently opt-in; never reinterpret global legacy dates.
DATE_VERSION='bibliography-month-year-v2'
DATE_KEYS={'schema','implementation_sha256','source_hash','source_dates',
           'citation_keys','independent_semantic_review_required','source_element_id','source_record_sha256'}
MONTHS=('January','February','March','April','May','June','July','August',
        'September','October','November','December')
EN_MONTH_YEAR=re.compile(r'\b('+ '|'.join(MONTHS)+r')\s*,?\s+([12][0-9]{3})(?![0-9])')
CN_MONTH_YEAR=re.compile(r'(?<![A-Za-z0-9_])([12][0-9]{3})年([0-9]{1,2}|[一二三四五六七八九十]{1,3})月')
CITATION_KEY=re.compile(r'\[[A-Za-z][A-Za-z0-9]{1,30}\]')

def _month_number(raw):
    if raw.isdigit():value=int(raw)
    else:
        chinese={'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
        if raw=='十':value=10
        elif raw in ('十一','十二'):value=10+chinese[raw[1]]
        elif raw in chinese:value=chinese[raw]
        else:raise ValueError('Unverifiable Chinese month')
    if value<1 or value>12:raise ValueError('Invalid month')
    return value

def normalize_bibliography_dates(text,*,source_only=False):
    text=_unprotected(text);keys=[{'key':m[0],'start':m.start(),'end':m.end()}for m in CITATION_KEY.finditer(text)]
    spans=[]
    for m in EN_MONTH_YEAR.finditer(text):
        spans.append((m.start(),m.end(),MONTHS.index(m[1].capitalize())+1,int(m[2]),m[0]))
    if not source_only:
        for m in CN_MONTH_YEAR.finditer(text):
            if re.match(r'\s*[0-9一二三四五六七八九十]+[日号]',text[m.end():]):
                raise ValueError('Month-year gained an unsupported day')
            spans.append((m.start(),m.end(),_month_number(m[2]),int(m[1]),m[0]))
    spans.sort();cursor=0;parts=[];dates=[]
    for start,end,month,year,literal in spans:
        if start<cursor:raise ValueError('Overlapping month-year interpretations')
        prior=[k for k in keys if k['end']<=start]
        if not prior:raise ValueError('Bibliographic month-year lacks preceding citation key')
        key=prior[-1]['key'];parts.append(text[cursor:start]);parts.append(' ');cursor=end
        dates.append({'literal':literal,'start':start,'end':end,'month':month,'year':year,'citation_key':key})
    parts.append(text[cursor:])
    return ''.join(parts),dates,[k['key']for k in keys]

def make_bibliography_date_policy(element):
    if AMOUNT.search(_unprotected(element.text)):
        raise ValueError('Mixed bibliography date and magnitude requires a composite policy')
    normalized,dates,keys=normalize_bibliography_dates(element.text,source_only=True)
    if not dates or not keys:raise ValueError('No exact citation-bound English month-year')
    if len(keys)!=len(set(keys)):raise ValueError('Repeated citation key requires a stronger bibliography policy')
    return {'schema':DATE_VERSION,'implementation_sha256':implementation_sha256(),
        'source_hash':element.source_hash,'source_dates':dates,'citation_keys':keys,
        'independent_semantic_review_required':True,**_source_binding(element)}

def check_bibliography_date_numbers(element,target,policy):
    from .numbers import numeric_fingerprint
    a,src,source_keys=normalize_bibliography_dates(element.text,source_only=True)
    b,dst,target_keys=normalize_bibliography_dates(target)
    if source_keys!=target_keys:
        return {'equivalent':False,'reason':'bibliography_citation_keys_or_order_changed','order_review_required':False}
    fingerprint=lambda dates:Counter((d['citation_key'],d['year'],d['month'])for d in dates)
    if fingerprint(src)!=fingerprint(dst):
        return {'equivalent':False,'reason':'bibliography_date_value_count_or_citation_association_changed','order_review_required':False}
    equal=numeric_fingerprint(a)==numeric_fingerprint(b)
    return {'equivalent':equal,'reason':None if equal else 'legacy_remaining_numbers_differ',
        'source_rewritten':a,'target_rewritten':b,'source_dates':src,'target_dates':dst,
        'order_review_required':equal and [(x['citation_key'],x['year'],x['month'])for x in src]!=[(x['citation_key'],x['year'],x['month'])for x in dst],
        'independent_semantic_review_required':True}


# Named months without day/year are another explicit, source-bound policy.
# A real source noun anchor is required. Bare May/March never become dates.
NAMED_MONTH_VERSION='explicit-named-months-v1'
NAMED_MONTH_KEYS={'schema','implementation_sha256','source_hash','source_months',
                  'independent_semantic_review_required','source_element_id','source_record_sha256'}
EN_NOUN_MONTH=re.compile(r'\bmonth\s+of\s+('+'|'.join(MONTHS)+r')\b')
CN_NAMED_MONTH=re.compile(r'(?<![A-Za-z0-9_零〇一二三四五六七八九十两])([0-9]{1,2}|[一二三四五六七八九十两]{1,3})月(?:份)?')
CN_EXPLICIT_DAY_YEAR=re.compile(r'[0-9零〇一二三四五六七八九十两廿卅]{1,8}\s*[年日号]')

def normalize_named_months(text,*,source_only=False):
    """Remove only explicit noun-bound month events and preserve every digit.

    This schema deliberately excludes year/day syntax. Bibliographic dates use
    their own citation-bound schema; mixed temporal forms require another policy.
    """
    text=_unprotected(text)
    if (CN_EXPLICIT_DAY_YEAR.search(text) or EN_MONTH_YEAR.search(text)
            or re.search(r'\b(?:day|days|year|years)\b',text,re.I)):
        raise ValueError('Named-month policy cannot add or reinterpret a day/year')
    spans=[]
    for m in EN_NOUN_MONTH.finditer(text):
        # Do not silently reinterpret January 1 / January, 2018 / January 1st.
        if re.match(r'\s*,?\s*[0-9]',text[m.end():]):
            raise ValueError('Named month is followed by an unsupported day/year')
        spans.append({'literal':m[0],'start':m.start(),'end':m.end(),
                      'month':MONTHS.index(m[1])+1,'anchor':'english-month-of-noun'})
    if not source_only:
        for m in CN_NAMED_MONTH.finditer(text):
            spans.append({'literal':m[0],'start':m.start(),'end':m.end(),
                          'month':_month_number(m[1]),'anchor':'explicit-chinese-month-noun'})
    spans.sort(key=lambda s:s['start']);cursor=0;pieces=[]
    for span in spans:
        if span['start']<cursor:raise ValueError('Overlapping named-month interpretations')
        pieces.append(text[cursor:span['start']]);pieces.append(' ');cursor=span['end']
    pieces.append(text[cursor:]);remaining=''.join(pieces)
    # Any capitalized English month left outside proven noun events is
    # ambiguous. In particular, do not guess whether May/March are names/verbs.
    if re.search(r'\b(?:'+'|'.join(MONTHS)+r')\b',remaining):
        raise ValueError('Unanchored English month word requires explicit source evidence')
    if source_only and CN_NAMED_MONTH.search(remaining):
        raise ValueError('Source already mixes Chinese and English month forms')
    return remaining,spans

def make_named_month_policy(element):
    if AMOUNT.search(_unprotected(element.text)):
        raise ValueError('Mixed named-month and magnitude policy is unsupported')
    remaining,months=normalize_named_months(element.text,source_only=True)
    if not months:raise ValueError('No explicit month-of source noun anchor')
    return {'schema':NAMED_MONTH_VERSION,'implementation_sha256':implementation_sha256(),
        'source_hash':element.source_hash,'source_months':months,
        'independent_semantic_review_required':True,**_source_binding(element)}

def check_named_month_numbers(element,target,policy):
    from .numbers import numeric_fingerprint
    a,src=normalize_named_months(element.text,source_only=True)
    b,dst=normalize_named_months(target)
    # These two contrastive graph months carry ordered associations. A month
    # swap is a hard error, unlike legitimate quantity/date prose reordering.
    if [s['month']for s in src]!=[d['month']for d in dst]:
        return {'equivalent':False,'reason':'named_month_value_count_or_order_changed',
                'order_review_required':False,'independent_semantic_review_required':True}
    equal=numeric_fingerprint(a)==numeric_fingerprint(b)
    return {'equivalent':equal,'reason':None if equal else 'legacy_remaining_numbers_differ',
        'source_months':src,'target_months':dst,'source_rewritten':a,'target_rewritten':b,
        'order_review_required':False,'independent_semantic_review_required':True}
