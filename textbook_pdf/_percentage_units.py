"""Private explicit-Arabic percentage unit probe, independent of production.

Adds a dimensional check beside the existing number check. It cannot replace
ordinary numeric/token validation or assess the object to which a rate applies.
Unparsed markers (e.g. symbolic rates or modulo) are explicitly unverifiable,
not silently equated or treated as numeric percentage events.
"""
from collections import Counter
from decimal import Decimal
import re
import unicodedata

TOKEN=re.compile(r'⟦F:[A-Za-z0-9_.-]+⟧')
RAW=r'[+−-]?[0-9]+(?:,[0-9]+)*(?:\.[0-9]+)?'
POINT=r'percentage\s+points?(?![A-Za-z0-9_])|percent\s+points?(?![A-Za-z0-9_])|个百分点|百分点'
PERCENT=r'%|％|percent(?![A-Za-z0-9_])|per\s+cent(?![A-Za-z0-9_])'
SUFFIX=re.compile(r'(?<![A-Za-z0-9_.])(?P<number>'+RAW+r')\s*(?P<unit>'+POINT+'|'+PERCENT+r')',re.I)
PREFIX=re.compile(r'百分之\s*(?P<number>'+RAW+r')(?![0-9.])')
MARKER=re.compile(POINT+'|'+PERCENT+'|百分之',re.I)
MAGNITUDE=re.compile(r'\s*(?:万亿|十亿|千万|百万|亿|万|thousand(?![A-Za-z0-9_])|million(?![A-Za-z0-9_])|billion(?![A-Za-z0-9_])|trillion(?![A-Za-z0-9_]))',re.I)

PAIRS={'(':')','（':'）','[':']','［':'］','{':'}','【':'】'}
CLOSERS=frozenset(PAIRS.values())
SIGNS=frozenset('+-−＋－﹢﹣‐‑‒–—')
OPERATORS=frozenset('/\\∕⁄*×÷^=√∛∜∑∏·⋅•∙∘・·')
FENCES=frozenset('|‖∣∥⌈⌉⌊⌋\'"“”‘’«»‹›')
MAX_WRAPPERS=16
MAX_BOUNDARY_SPAN=1024
PROSE_PUNCTUATION=frozenset('.,，。;；:：!！?？…、')

def wrapped_span(text,start,end):
    """Only consume balanced wrappers enclosing exactly this rate plus space.

    This is a small token scanner, not a regex that skips arbitrary opening
    characters. Every wrapper must match its own closing kind. Negation and
    arithmetic *outside* these wrappers are unsupported; they are never dropped
    while treating an inner positive decimal as the complete expression.
    """
    original_start,original_end=start,end
    depth=0
    while True:
        left=start-1
        while left>=0 and text[left].isspace():left-=1
        right=end
        while right<len(text) and text[right].isspace():right+=1
        opening=text[left]if left>=0 else''
        closing=text[right]if right<len(text)else''
        if opening in PAIRS and opening:
            if closing!=PAIRS[opening]:return None,'unbalanced_or_nonatomic_percentage_wrapper'
            depth+=1
            if depth>MAX_WRAPPERS:return None,'bounded_percentage_wrapper_depth'
            start,end=left,right+1
            if end-start>MAX_BOUNDARY_SPAN:return None,'bounded_percentage_wrapper_span'
            continue
        if closing in CLOSERS:return None,'unmatched_percentage_closing_wrapper'
        if (opening and (unicodedata.category(opening)in{'Ps','Pe'}or opening in FENCES)
                or closing and (unicodedata.category(closing)in{'Ps','Pe'}or closing in FENCES)):
            return None,'unsupported_percentage_fence'
        return (start,end,depth,original_start,original_end),None

def numeric_boundary_error(text,match,*,prefix_form):
    """Support one signed decimal rate, optionally in balanced plain wrappers.

    Fractions, ratios, products, functions and outer arithmetic signs are closed
    as unverifiable. A word followed by a prose colon remains distinct from a
    numeric or single-letter ratio. Prefix-form decimal coefficients may not be
    truncated before an exponent, division, or another numeric token.
    """
    span,error=wrapped_span(text,*match.span())
    if error:return error
    start,end,depth,_,_=span;left=text[:start];right=text[end:]
    l=left.rstrip();r=right.lstrip()
    if l and l[-1]in SIGNS:return 'outer_repeated_mixed_or_detached_numeric_sign'
    if re.search(r'(?:(?<![A-Za-z])(?:minus|negative|plus|positive)|[负負正])\s*$',left,re.I):
        return 'unsupported_textual_numeric_sign'
    if l and l[-1]in OPERATORS:return 'composite_numeric_prefix'
    if l and unicodedata.category(l[-1])=='Sm':return 'unsupported_mathematical_prefix'
    if l and unicodedata.category(l[-1])[0]in{'M','N'}:return 'unsupported_numeric_or_script_prefix'
    if l and l[-1].isalpha() and not l[-1].isascii() and not ('\u3400'<=l[-1]<='\u9fff'):
        return 'unsupported_nonprose_letter_coefficient_prefix'
    if re.search(r'(?:[0-9)）\]］}]|(?<![A-Za-z])[A-Za-z])\s*[:：]\s*$',left):
        return 'ratio_numeric_prefix'
    if re.search(r'[0-9][0-9,\s]*,\s*$',left):return 'unparsed_numeric_grouping_prefix'
    if re.search(r'[0-9]\s+$',left):return 'adjacent_numeric_coefficient_prefix'
    if depth and left and re.fullmatch(r'[A-Za-z0-9_]',left[-1]):return 'function_or_product_numeric_prefix'
    if re.search(r'(?<![A-Za-z])[A-Za-z]\s+$',left):return 'ambiguous_single_letter_coefficient_prefix'
    if l and (l[-1]in CLOSERS or l[-1]in FENCES):return 'composite_preceding_wrapper'
    if l and unicodedata.category(l[-1])[0]in{'P','S'}and l[-1]not in PROSE_PUNCTUATION:
        # A preceding, whitespace-separated complete rate is another event;
        # every other unrecognized symbol is not a proven prose separator.
        previous=list(SUFFIX.finditer(left))
        if not(left and left[-1].isspace() and previous and previous[-1].end()==len(l)):
            return 'unsupported_punctuation_or_symbol_prefix'
    # Non-CJK letters adjacent to the numeric token/wrapper are not prose
    # separators (e.g. a Unicode mathematical function variable). Chinese prose
    # such as 比率（2%） remains legal. No Unicode letter is rewritten to ASCII.
    if left and left[-1].isalnum() and not ('\u3400'<=left[-1]<='\u9fff'):
        return 'unsupported_alphanumeric_percentage_prefix'
    if prefix_form and re.match(r'[A-Za-z0-9_.]',text[match.end():]):return 'partial_prefix_form_numeric_coefficient'
    if r and r[0]in OPERATORS:return 'composite_numeric_suffix'
    if r and unicodedata.category(r[0])=='Sm':return 'unsupported_mathematical_suffix'
    if r and unicodedata.category(r[0])[0]in{'P','S'}and r[0]not in PROSE_PUNCTUATION:
        return 'unsupported_punctuation_or_symbol_suffix'
    # A percent marker never terminates an attached exponent, identifier,
    # superscript/subscript, vulgar fraction or combining mathematical mark.
    # No-depth events obey the same rule as wrapped events. Chinese prose may
    # immediately follow a rate; Latin prose must be separated by whitespace.
    if right and (unicodedata.category(right[0])[0]in{'M','N'}or
            right[0].isalpha()and not ('\u3400'<=right[0]<='\u9fff')):
        return 'attached_percentage_identifier_or_script_suffix'
    if r and unicodedata.category(r[0])[0]=='M':return 'unsupported_spaced_percentage_script_suffix'
    if r and r[0].isalpha() and not r[0].isascii() and not ('\u3400'<=r[0]<='\u9fff'):
        return 'unsupported_nonprose_letter_coefficient_suffix'
    word=re.match(r'([A-Za-z]+)(?![A-Za-z])',r)
    if word and (len(word[1])==1 and not re.match(r'a\s+(?:year|month|week|day)\b',r,re.I)
            or re.match(r'[A-Za-z]+[0-9_]',r)):
        return 'ambiguous_spaced_percentage_identifier_suffix'
    if re.match(r'[:：]\s*[+−-]?[0-9]',r):return 'ratio_numeric_suffix'
    if r and r[0]in SIGNS:return 'arithmetic_or_range_numeric_suffix'
    if depth and right and re.fullmatch(r'[A-Za-z0-9_]',right[0]):return 'function_or_product_numeric_suffix'
    if r and (r[0].isnumeric()or r[0]=='_'):
        # Separate complete rates remain separate events, so dropping one gives
        # a definite count mismatch. No inner substring is accepted here: the
        # next event is still independently checked by this same scanner.
        if not(right and right[0].isspace() and SUFFIX.match(r)):
            return 'adjacent_numeric_coefficient_suffix'
    if MAGNITUDE.match(right):return 'percentage_with_unhandled_magnitude'
    return None

def canonical(raw):
    if len(raw)>256:raise ValueError('bounded_decimal_precision')
    raw=raw.replace('−','-');integer=raw.lstrip('+-').split('.',1)[0]
    if ','in integer and not re.fullmatch(r'[0-9]{1,3}(?:,[0-9]{3})+',integer):
        raise ValueError('ambiguous_grouping')
    value=Decimal(raw.replace(',',''))
    if value.is_zero():return '0'
    out=format(value,'f')
    return out.rstrip('0').rstrip('.')if'.'in out else out

def events(text):
    masked=TOKEN.sub(lambda m:' '*len(m[0]),text);found=[];covered=[];errors=[]
    # Invisible format/control characters can hide the connection between an
    # outer sign/operator and a bracket. Do not skip them as benign whitespace.
    if any(unicodedata.category(c)in{'Cf','Cs'}or(unicodedata.category(c)=='Cc'and not c.isspace())for c in masked):
        return [],[{'reason':'unsupported_hidden_percentage_boundary_character'}]
    for regex in(SUFFIX,PREFIX):
        for m in regex.finditer(masked):
            if any(m.start()<end and m.end()>start for start,end in covered):
                errors.append({'reason':'overlapping_percentage_parse','span':list(m.span())});continue
            boundary=numeric_boundary_error(masked,m,prefix_form=regex is PREFIX)
            if boundary:
                errors.append({'reason':boundary,'span':list(m.span())});continue
            try:value=canonical(m['number'])
            except ValueError as e:errors.append({'reason':str(e),'span':list(m.span())});continue
            if MAGNITUDE.match(masked[m.end():]):
                errors.append({'reason':'percentage_with_unhandled_magnitude','span':list(m.span())});continue
            unit=m['unit'].lower()if regex is SUFFIX else '百分之'
            kind='percentage_point'if re.search('point|百分点',unit)else'percentage'
            found.append({'value':value,'unit':kind,'literal':m[0],'span':list(m.span())});covered.append(m.span())
    for m in MARKER.finditer(masked):
        if not any(start<=m.start()and m.end()<=end for start,end in covered):
            errors.append({'reason':'unparsed_percentage_marker','literal':m[0],'span':list(m.span())})
    found.sort(key=lambda e:e['span'])
    return found,errors

def compare(source,target):
    a,ae=events(source);b,be=events(target)
    if ae or be:
        return {'supported':False,'equivalent':None,'reason':'unverifiable_percentage_expression',
                'source_events':a,'target_events':b,'source_unverifiable':ae,'target_unverifiable':be,
                'independent_semantic_review_required':True}
    key=lambda e:(e['unit'],e['value'])
    equal=Counter(map(key,a))==Counter(map(key,b))
    return {'supported':True,'equivalent':equal,'reason':None if equal else'percentage_value_unit_or_count_changed',
            'source_events':a,'target_events':b,'order_review_required':equal and list(map(key,a))!=list(map(key,b)),
            'independent_semantic_review_required':True}
