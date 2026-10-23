"""Explicit opt-in dimensional percentage guard; the legacy path is unchanged.

This adapter reuses the frozen Arabic percentage parser. It complements the
ordinary number, quantity and protected-token checks. It cannot prove the
semantic association between rates and the entities to which they apply.
"""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import hashlib
import json

from . import _percentage_units as parser

VERSION = 'explicit-arabic-percent-units-v4'
FIELD = 'percentage_unit_policy'
KEYS = {'schema', 'implementation_sha256', 'parser_sha256', 'source_hash',
        'source_element_id', 'source_record_sha256', 'source_events',
        'independent_semantic_review_required'}


def implementation_sha256():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def parser_sha256():
    return hashlib.sha256(Path(parser.__file__).read_bytes()).hexdigest()


def _source_binding(element):
    record = asdict(element)
    # Both policies bind the semantic source independently. The unchanged
    # numeric adapter is rebuilt last and additionally binds this final policy.
    # Excluding both here avoids a mutually recursive policy identity.
    record['metadata'].pop(FIELD, None)
    record['metadata'].pop('numeric_policy', None)
    record['metadata'].pop('display_asset_ids', None)
    raw = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return {'source_element_id': element.id,
            'source_record_sha256': hashlib.sha256(raw).hexdigest()}


def make_percentage_policy(element):
    events, errors = parser.events(element.text)
    if errors or not 1 <= len(events) <= 32:
        raise ValueError('Source requires one to 32 fully parsed explicit Arabic percentage events')
    return {'schema': VERSION, 'implementation_sha256': implementation_sha256(),
            'parser_sha256': parser_sha256(), 'source_hash': element.source_hash,
            'source_events': events, 'independent_semantic_review_required': True,
            **_source_binding(element)}


def percentage_policy_fields(element):
    if FIELD not in element.metadata:
        return {}
    policy = element.metadata[FIELD]
    if (not isinstance(policy, dict) or set(policy) != KEYS
            or policy.get('schema') != VERSION
            or policy.get('implementation_sha256') != implementation_sha256()
            or policy.get('parser_sha256') != parser_sha256()
            or policy.get('source_hash') != element.source_hash
            or policy.get('independent_semantic_review_required') is not True):
        raise ValueError('Unsupported, stale or unbound percentage unit policy')
    if policy != make_percentage_policy(element):
        raise ValueError('Percentage policy differs from the exact source record/events')
    return {FIELD: deepcopy(policy)}


def frozen_percentage_policy_matches(row, element):
    expected = percentage_policy_fields(element)
    return ((FIELD in row) == bool(expected)
            and (FIELD not in row or row[FIELD] == expected[FIELD]))


def check_percentage_numbers(element, target):
    try:
        percentage_policy_fields(element)[FIELD]
        result = parser.compare(element.text, target)
    except (ValueError, TypeError, KeyError) as error:
        return {'supported': False, 'equivalent': False,
                'reason': 'percentage_policy_unverifiable:' + str(error),
                'order_review_required': False,
                'independent_semantic_review_required': True}
    if not result['supported']:
        result = dict(result, equivalent=False)
    return result


def percentage_unit_issues(element, target):
    result = check_percentage_numbers(element, target)
    base = {'element_id': element.id, 'page': element.page}
    if result['equivalent'] is not True:
        return [{**base, 'code': 'percentage_unit_mismatch', 'severity': 'error',
                 'detail': '显式百分比数值、符号、数量或单位不一致/不可核验：' + str(result['reason'])}]
    if result.get('order_review_required'):
        return [{**base, 'code': 'percentage_order_changed', 'severity': 'warning',
                 'detail': '显式百分比数值与单位等值，语序已调整；独立语义复核须核对比率与对象的对应关系。'}]
    return []
