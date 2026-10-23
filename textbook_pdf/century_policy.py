"""Explicit century-only opt-in; never change the frozen legacy number parser."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import hashlib
import json
import re
import unicodedata

from .models import TOKEN_RE
from .numbers import numeric_fingerprint

VERSION = 'explicit-single-century-v3'
FIELD = 'century_policy'
KEYS = {'schema', 'implementation_sha256', 'source_hash', 'source_element_id',
        'source_record_sha256', 'source_event', 'independent_semantic_review_required'}
ORDINALS = ('first second third fourth fifth sixth seventh eighth ninth tenth '
            'eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth '
            'eighteenth nineteenth twentieth twenty-first').split()
WORDS = {word: n for n, word in enumerate(ORDINALS, 1)}
EN = re.compile(r'(?<![A-Za-z0-9_\-])(' + '|'.join(sorted(WORDS, key=len, reverse=True))
                + r'|[1-9][0-9]?(?:st|nd|rd|th))[\s-]+century(?![A-Za-z0-9_])', re.I)
CN = re.compile(r'(?<![A-Za-z0-9零〇一二三四五六七八九十百千万亿两第.+−\-])第?'
                r'([1-9][0-9]?|[一二三四五六七八九十]{1,3})\s*世纪')
RESIDUAL = re.compile(r'centur(?:y|ies)|世纪', re.I)
ERA = re.compile(r'\b(?:B\.?C\.?E?\.?|A\.?D\.?|C\.?E\.?)\b|公元前|西元前|公元后|西元后', re.I)
COMPOUND = re.compile(r'\b(?:' + '|'.join(ORDINALS) + r')\b', re.I)


def implementation_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def source_binding(element):
    record = asdict(element)
    record['metadata'].pop(FIELD, None)
    record['metadata'].pop('display_asset_ids', None)
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return {'source_element_id': element.id, 'source_record_sha256': hashlib.sha256(raw).hexdigest()}


def _number(raw):
    if raw.isdigit():
        return int(raw)
    digits = dict(zip('一二三四五六七八九', range(1, 10)))
    if raw in digits:
        return digits[raw]
    if raw == '十':
        return 10
    if raw.startswith('十') and len(raw) == 2 and raw[1] in digits:
        return 10 + digits[raw[1]]
    if len(raw) in (2, 3) and raw[0] in digits and raw[0] != '一' and raw[1] == '十':
        return 10 * digits[raw[0]] + (digits.get(raw[2], 0) if len(raw) == 3 else 0)
    raise ValueError('Unsupported Chinese century numeral')


def _english_number(raw):
    if raw.lower() in WORDS:
        return WORDS[raw.lower()]
    value, suffix = int(raw[:-2]), raw[-2:].lower()
    expected = 'th' if value % 100 in (11, 12, 13) else {1: 'st', 2: 'nd', 3: 'rd'}.get(value % 10, 'th')
    if suffix != expected:
        raise ValueError('Invalid English ordinal suffix')
    return value


def _cjk(char):
    return '\u3400' <= char <= '\u9fff'


def _numeric_operator(char):
    """Unicode operators/dashes/rates, including compatibility-width forms."""
    category = unicodedata.category(char)
    name = unicodedata.name(char, '')
    compatible = unicodedata.normalize('NFKC', char)
    return (category[0] == 'S' or category == 'Pd'
            or compatible in ('/', '\\', '%')
            or any(word in name for word in ('PERCENT', 'PER MILLE', 'PER TEN THOUSAND')))


def _standalone_bounds(text, match, language):
    """A century noun phrase, never an inner substring of a numeric expression.

    Parenthesised/quoted prose is allowed; surrounding arithmetic, script
    numerals, partial ordinals and unknown letter attachments are closed.
    This is a bounded lexical guard, not an expression or historical-era parser.
    """
    start, end = match.span()
    left = text[:start]
    # Do not turn unsupported compound ordinals into their final first/second.
    lexical = ''.join('-' if unicodedata.category(c) == 'Pd' or c == '\u2212' else c for c in left)
    if language == 'en' and re.search(
            r'\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
            r'thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|'
            r'thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand)'
            r'(?:[\s-]+|[\s-]+and[\s-]+)$', lexical, re.I):
        raise ValueError('Unsupported compound ordinal prefix')
    if start:
        previous = text[start-1]
        category = unicodedata.category(previous)
        if (category[0] in 'NM' or previous in '._' or
                (previous.isalpha() and not _cjk(previous))):
            raise ValueError('Century is attached to a numeric or letter prefix')
    if end < len(text):
        following = text[end]
        category = unicodedata.category(following)
        if (category[0] in 'NM' or following == '_' or
                following == '.' and end+1 < len(text) and text[end+1].isnumeric() or
                following in '零〇一二三四五六七八九十百千万亿两' or
                (following.isalpha() and not _cjk(following))):
            raise ValueError('Century has an unsupported number, script or letter suffix')
    # Limit wrapper scanning. A sign around nested editorial brackets must not
    # be silently discarded. Brackets themselves are not evaluated as maths.
    cursor = start
    budget = 0
    while cursor and (text[cursor-1].isspace()
                      or unicodedata.category(text[cursor-1]) in ('Ps', 'Pi')
                      or text[cursor-1] in '\"\''):
        cursor -= 1; budget += 1
        if budget > 64:
            raise ValueError('Unsupported century wrapper depth')
    if cursor:
        previous = text[cursor-1]
        if _numeric_operator(previous):
            raise ValueError('Arithmetic or signed century prefixes are unsupported')
        if previous in ':：':
            token = re.search(r'([^\s:：]+)\s*$', text[:cursor-1])
            if token and (any(c.isnumeric() for c in token[1]) or
                          re.fullmatch(r'[A-Za-z]', token[1])):
                raise ValueError('A ratio-like century prefix is unsupported')
    cursor = end
    budget = 0
    while cursor < len(text) and (text[cursor].isspace()
                                 or unicodedata.category(text[cursor]) in ('Pe', 'Pf')
                                 or text[cursor] in '\"\''):
        cursor += 1; budget += 1
        if budget > 64:
            raise ValueError('Unsupported century wrapper depth')
    if cursor < len(text):
        following = text[cursor]
        if (_numeric_operator(following) or unicodedata.category(following)[0] in 'NM'
                and cursor != end):
            raise ValueError('Arithmetic or script century suffixes are unsupported')


def event(text, *, source=False):
    text = TOKEN_RE.sub(' ', text)
    if ERA.search(text):
        raise ValueError('Era-qualified centuries require a separate policy')
    matches = [(m, _english_number(m[1]), 'en') for m in EN.finditer(text)]
    if not source:
        matches += [(m, _number(m[1]), 'zh') for m in CN.finditer(text)]
    if len(matches) != 1:
        raise ValueError('Exactly one standalone explicit century is required')
    m, value, language = matches[0]
    _standalone_bounds(text, m, language)
    if language == 'en' and re.search(r'\b(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)[-\s]+$', text[:m.start()], re.I):
        raise ValueError('A split ordinal prefix cannot be dropped')
    if not 1 <= value <= 21:
        raise ValueError('Century outside the explicit 1 to 21 scope')
    rest = text[:m.start()] + ' ' + text[m.end():]
    if RESIDUAL.search(rest):
        raise ValueError('An additional century expression was not parsed')
    # A bare ordinal in the same sentence may be an elided century range.
    left = re.split(r'[.!?。！？;；]', text[:m.start()])[-1]
    if COMPOUND.search(left) and re.search(r'\b(?:and|or|to|through|between)\b', left, re.I):
        raise ValueError('Elided or coordinated century expressions are unsupported')
    return rest, {'literal': m[0], 'start': m.start(), 'end': m.end(), 'value': value, 'language': language}


def make_century_policy(element):
    if 'numeric_policy' in element.metadata or 'percentage_unit_policy' in element.metadata:
        raise ValueError('Combining explicit numeric policies requires a separately reviewed contract')
    _, found = event(element.text, source=True)
    return {'schema': VERSION, 'implementation_sha256': implementation_sha256(),
            'source_hash': element.source_hash, 'source_event': found,
            'independent_semantic_review_required': True, **source_binding(element)}


def century_policy_fields(element):
    if FIELD not in element.metadata:
        return {}
    p = element.metadata[FIELD]
    if not isinstance(p, dict) or set(p) != KEYS or p != make_century_policy(element):
        raise ValueError('Unsupported, stale or unbound century policy')
    return {FIELD: deepcopy(p)}


def frozen_century_policy_matches(row, element):
    expected = century_policy_fields(element)
    return (FIELD in row) == bool(expected) and (FIELD not in row or row[FIELD] == expected[FIELD])


def check_century_numbers(element, target):
    try:
        century_policy_fields(element)[FIELD]
        src, a = event(element.text, source=True)
        dst, b = event(target)
        equal = a['value'] == b['value'] and numeric_fingerprint(src) == numeric_fingerprint(dst)
        return {'equivalent': equal, 'reason': None if equal else 'century_or_remaining_numbers_changed',
                'source_event': a, 'target_event': b, 'independent_semantic_review_required': True}
    except (ValueError, TypeError, KeyError) as error:
        return {'equivalent': False, 'reason': 'century_policy_unverifiable:' + str(error),
                'independent_semantic_review_required': True}


def century_number_issues(element, target):
    result = check_century_numbers(element, target)
    if result['equivalent']:
        return []
    return [{'element_id': element.id, 'page': element.page, 'code': 'number_mismatch',
             'severity': 'error', 'detail': '显式世纪或其余数字不一致/不可核验：' + result['reason']}]
