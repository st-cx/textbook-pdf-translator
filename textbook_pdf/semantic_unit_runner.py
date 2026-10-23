"""Conditional model scheduling for explicitly proved whole semantic units.

Transport and transaction state stay in the existing translation/Store layers.
No inference of units, fragment translation, or individual passing reviews.
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .models import digest
from .semantic_units import UnitReviewStore, definitions, member_ids


class _FrozenRulesError(ValueError):
    pass


def _checked_rules(packet, rules):
    """Reveal only the exact glossary/style preimages already bound by packet."""
    try:
        payload = packet['payload']
        if (packet['unit_review_hash'] != digest(payload) or not isinstance(rules, dict)
                or set(rules) != {'glossary', 'style_rules'}
                or not isinstance(rules['glossary'], dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in rules['glossary'].items())
                or not isinstance(rules['style_rules'], list)
                or any(not isinstance(v, str) for v in rules['style_rules'])
                or digest(rules['glossary']) != payload['glossary_hash']
                or digest(rules['style_rules']) != payload['style_hash']):
            raise _FrozenRulesError('frozen_review_rules_hash_mismatch')
    except (KeyError, TypeError) as error:
        raise _FrozenRulesError('frozen_review_rules_missing_or_malformed') from None
    return deepcopy(rules)


def _freeze_unit_rules(store, packet):
    # Caller already holds UnitReviewStore._read's lock and SQLite snapshot.
    from .translation import STYLE_RULES
    row = store.db.execute("SELECT value FROM metadata WHERE key='glossary'").fetchone()
    if not row:
        raise _FrozenRulesError('frozen_review_glossary_missing')
    try:
        rules = {'glossary': json.loads(row[0]), 'style_rules': list(STYLE_RULES)}
    except (ValueError, TypeError):
        raise _FrozenRulesError('frozen_review_glossary_malformed') from None
    return _checked_rules(packet, rules)


def _unit_request_packet(planned):
    packet = planned['packet']
    rules = _checked_rules(packet, planned.get('_frozen_rules_readonly'))
    request = deepcopy(packet)
    request['frozen_rules_readonly'] = rules
    return request


def _result_token(store, packet):
    """Current result version, independent of source/translation dependencies."""
    exists = store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_unit_reviews'").fetchone()
    if not exists:
        return None
    row = store.db.execute('SELECT id,response_json FROM semantic_unit_reviews WHERE unit_id=? AND unit_review_hash=? ORDER BY id DESC LIMIT 1',
                           (packet['unit_id'], packet['unit_review_hash'])).fetchone()
    return digest({'id': row['id'], 'response_json': row['response_json']}) if row else None


def _has_current_revision(store, eid):
    row = store.db.execute('SELECT t.status,t.source_hash AS ts,t.rule_hash AS tr,e.source_hash AS es,e.rule_hash AS er '
                           'FROM elements e LEFT JOIN translations t ON t.element_id=e.id WHERE e.id=?', (eid,)).fetchone()
    return bool(row and row['status'] in ('accepted', 'review_failed') and row['ts'] == row['es'] and row['tr'] == row['er'])


def unit_queue_snapshot(book, store):
    """Read only: classify ready versus awaiting/unverifiable units precisely."""
    units = definitions(book)
    if not units:
        return []
    if (not store.book or definitions(store.book) != units
            or store.book.source_sha256 != book.source_sha256):
        raise ValueError('Caller and Store semantic-unit registry/source differ')
    facade = UnitReviewStore(store)
    queue = []
    with facade._read():
        for uid, definition in units.items():
            status = facade.status(uid)
            item = {**status, 'member_ids': list(definition['member_ids'])}
            if status['state'] == 'pending_unverifiable':
                missing = [eid for eid in definition['member_ids'] if not _has_current_revision(store, eid)]
                item.update(state='awaiting_translation' if missing else 'unverifiable',
                            awaiting_member_ids=missing)
                # This is a queue classification, not source acceptance.
                item['source_binding_verified'] = False
            else:
                packet = facade.packet(uid)
                try:
                    rules = _freeze_unit_rules(store, packet)
                except _FrozenRulesError:
                    item.update(state='unverifiable', source_binding_verified=False, reason='frozen_review_rules_unverifiable')
                else:
                    item.update(packet=packet, result_token=_result_token(store, packet),
                                _frozen_rules_readonly=rules, source_binding_verified=True)
            queue.append(item)
    return queue


def public_unit_status(queue):
    states = [{key: deepcopy(value) for key, value in item.items() if key not in ('packet', 'result_token', '_frozen_rules_readonly')}
              for item in queue]
    return {'counts': {state: sum(item['state'] == state for item in states)
                       for state in ('pending', 'passed', 'failed', 'awaiting_translation', 'unverifiable')},
            'units': states}


def overlay_review_status(book, store, base):
    """Keep members visible while all semantic coverage derives from whole units."""
    queue = unit_queue_snapshot(book, store)
    if not queue:
        return base
    out = deepcopy(base)
    sets = {key: set(values) for key, values in out['ids'].items()}
    for item in queue:
        members = set(item['member_ids'])
        for key in sets:
            if key != 'selected':
                sets[key] -= members
        sets['selected'] |= members
        state = item['state']
        sets[state if state in ('passed', 'failed', 'awaiting_translation') else 'pending'] |= members
        if state == 'unverifiable':
            sets['unverifiable'] |= members
    out['ids'] = {key: sorted(values) for key, values in sets.items()}
    for key in ('selected', 'passed', 'pending', 'failed', 'awaiting_translation'):
        out[key] = len(sets[key])
    out['semantic_units'] = public_unit_status(queue)
    out['individual_semantic_passes_synthesized'] = 0
    out['coverage_complete'] = not any(sets[key] for key in ('pending', 'failed', 'awaiting_translation'))
    return out


def _apply_current_result(store, planned, response, provider):
    """Short write CAS also rejects an overtaking/duplicate review result."""
    facade = UnitReviewStore(store)
    with store.lock:
        if store.db.in_transaction:
            raise ValueError('Model scheduler cannot own or commit a caller transaction')
        with store._atomic_write():
            if _result_token(store, planned['packet']) != planned['result_token']:
                return {'applied': False, 'stale': True, 'reason': 'another_unit_result_committed'}
            result = facade.apply(planned['packet'], response, provider=provider)
        if result.get('applied'):
            result['commit_state'] = 'committed_when_returned'
        return result


def _planned_current(store, planned):
    facade = UnitReviewStore(store)
    try:
        with facade._read():
            return (facade.packet(planned['packet']['unit_id']) == planned['packet']
                    and _result_token(store, planned['packet']) == planned['result_token'])
    except (ValueError, KeyError, TypeError, OSError):
        return False


def _run_one_unit(store, planned, provider, limiter, stop):
    from . import translation as transport
    from .semantic_units import validate_response
    started = time.perf_counter()
    report = transport._new_review_report(provider.name, 0, 0, 0, 0)
    report.update(unit_review_passed=0, unit_review_failed=0, unit_review_pending=0,
                  unit_stale_results=0, unit_rejected_results=0, unit_review_truncated_calls=0, unit_results=[])
    packet = planned['packet']
    uid = packet['unit_id']
    task_id = 'unit-review-' + packet['unit_review_hash'][:24]
    result, last_error = None, 'Review not completed'
    try:
        for attempt in range(provider.attempts):
            accounted = False
            try:
                if stop.is_set():
                    raise transport._Cancelled()
                # Frozen source/text/actual files may have changed while queued.
                if not _planned_current(store, planned):
                    report['unit_stale_results'] += 1
                    report['unit_results'].append({'unit_id': uid, 'state': 'stale_before_request'})
                    return report
                limiter.acquire()
                if not _planned_current(store, planned):
                    report['unit_stale_results'] += 1
                    report['unit_results'].append({'unit_id': uid, 'state': 'stale_before_request'})
                    return report
                request_packet = _unit_request_packet(planned)
                report['requests'] += 1
                raw, usage = transport._chat(provider, [
                    {'role': 'system', 'content': '独立审校完整跨页英文数学教材中译语义单元。逐项检查所有内容、量词、否定、条件方向、数字单位、引用、术语和受保护数学上下文。中文短语可跨原页边界移动，检查合起来完整且仅出现一次。只按required_response返回整单元JSON与boundary_coverage，不返回改译、单元素通过或补译。'},
                    {'role': 'user', 'content': json.dumps(request_packet, ensure_ascii=False)},
                ])
                limiter.success()
                inp, out = usage.get('prompt_tokens'), usage.get('completion_tokens')
                known = isinstance(inp, int) and isinstance(out, int)
                inp, out = (inp, out) if known else (0, 0)
                priced = known and provider.price_in is not None and provider.price_out is not None
                cost = (inp * provider.price_in + out * provider.price_out) / 1e6 if priced else 0.0
                report['input_tokens'] += inp
                report['output_tokens'] += out
                report['estimated_cost'] += cost
                report['usage_missing_calls'] += int(not known)
                report['cost_estimate_complete'] &= bool(priced)
                store.record_usage(task_id, provider.name, {'stage': 'review_unit', 'unit_id': uid,
                    'unit_review_hash': packet['unit_review_hash'], 'prompt_tokens': inp, 'completion_tokens': out,
                    'usage_known': known, 'estimated_cost': cost, 'model': provider.model, 'currency': provider.currency})
                accounted = True
                if usage.get('_truncated'):
                    raise transport._OutputTruncated('Complete unit output truncated')
                result = validate_response(packet, transport._json_no_duplicates(raw))
                break
            except _FrozenRulesError:
                report['unit_rejected_results'] += 1
                last_error = 'frozen_review_rules_missing_or_hash_mismatch_before_api'
                break
            except transport._Cancelled:
                raise
            except transport._OutputTruncated:
                report['failed_requests'] += 1
                report['unit_review_truncated_calls'] += 1
                last_error = 'complete_unit_output_truncated_no_split_or_identical_retry'
                break
            except transport._HTTPFailure as error:
                report['failed_requests'] += 1
                report['usage_missing_calls'] += 1
                report['cost_estimate_complete'] = False
                store.record_usage(task_id, provider.name, {'stage': 'review_unit', 'unit_id': uid,
                    'unit_review_hash': packet['unit_review_hash'], 'request_failed': True, 'http_status': error.status,
                    'usage_known': False, 'prompt_tokens': 0, 'completion_tokens': 0,
                    'estimated_cost': 0.0, 'currency': provider.currency})
                last_error = str(error)
                if error.status in {401, 402, 403}:
                    report['provider_blocked'] = {'provider': provider.name, 'http_status': error.status}
                    stop.set()
                if error.status == 429 or error.status >= 500:
                    limiter.throttle(error.retry_after)
                if not error.retryable:
                    break
            except Exception as error:
                report['failed_requests'] += 1
                if not accounted:
                    report['usage_missing_calls'] += 1
                    report['cost_estimate_complete'] = False
                    store.record_usage(task_id, provider.name, {'stage': 'review_unit', 'unit_id': uid,
                        'unit_review_hash': packet['unit_review_hash'], 'request_failed': True,
                        'usage_known': False, 'prompt_tokens': 0, 'completion_tokens': 0,
                        'estimated_cost': 0.0, 'currency': provider.currency})
                last_error = type(error).__name__
            if attempt + 1 < provider.attempts:
                stop.wait(min(30, 2 ** attempt + random.random()))
    except (KeyboardInterrupt, transport._Cancelled):
        report['cancelled'] = True
        stop.set()
        last_error = 'cancelled'
    if result is None:
        report['unit_review_pending'] += 1
        report['unit_results'].append({'unit_id': uid, 'state': 'pending', 'reason': last_error})
    else:
        applied = _apply_current_result(store, planned, result, provider.name)
        if not applied['applied']:
            if applied.get('stale'):
                report['unit_stale_results'] += 1
            else:
                report['unit_rejected_results'] += 1
                report['unit_review_pending'] += 1
        else:
            report['unit_review_passed' if result['passed'] else 'unit_review_failed'] += 1
        report['unit_results'].append({'unit_id': uid, **applied})
    report['elapsed_seconds'] = time.perf_counter() - started
    return report


def run_review_with_units(book, store, config_path, provider, max_elements=None, workers=4, *, unit_ids=None, max_units=None):
    """One bounded executor/provider limiter for units and ordinary review jobs."""
    from . import translation as transport
    started = time.perf_counter()
    if not isinstance(workers, int) or workers < 1 or workers > 32:
        raise ValueError('workers must be between 1 and 32')
    if max_elements is not None and max_elements < 0:
        raise ValueError('max_elements must be nonnegative')
    if max_units is not None and (not isinstance(max_units, int) or isinstance(max_units, bool) or max_units < 0):
        raise ValueError('max_units must be a nonnegative integer')
    with store.lock:
        if store.db.in_transaction:
            raise ValueError('Finish the caller transaction before scheduling model requests')
    queue = unit_queue_snapshot(book, store)
    wanted = None
    if unit_ids is not None:
        if isinstance(unit_ids, (str, bytes)):
            raise ValueError('unit_ids must be a collection of explicit unit IDs')
        wanted = set(unit_ids)
        if any(not isinstance(uid, str) or not uid for uid in wanted) or wanted - {item['unit_id'] for item in queue}:
            raise ValueError('Unknown/unbound semantic unit IDs')
    grouped = member_ids(book)
    ordinary_view = replace(book, elements=[e for e in book.elements if e.id not in grouped])
    migration = transport.migrate_review_bindings(ordinary_view, store)
    glossary, accepted, elements, selected, pending = transport._review_snapshot(book, store)
    chosen = [] if wanted is not None else pending if max_elements is None else pending[:max_elements]
    chosen_units = [item for item in queue if 'packet' in item and
                    (item['unit_id'] in wanted if wanted is not None else item['state'] == 'pending')]
    if max_units is not None:
        chosen_units = chosen_units[:max_units]
    report = transport._new_review_report(provider, len(accepted), len(selected), len(pending), len(chosen))
    report.update(review_binding_migration=migration, workers_requested=workers, workers_actual=0,
                  worker_failures=0, worker_elapsed_seconds=0.0, unit_review_passed=0, unit_review_failed=0,
                  unit_review_pending=0, unit_stale_results=0, unit_rejected_results=0, unit_review_truncated_calls=0,
                  unit_results=[], unit_scheduled=len(chosen_units), semantic_units_before=public_unit_status(queue),
                  explicit_unit_ids=sorted(wanted) if wanted is not None else None,
                  ordinary_review_suppressed_for_explicit_unit_ids=wanted is not None)
    batches = transport._review_batches(chosen, elements, accepted)
    if not batches and not chosen_units:
        report['semantic_units_after'] = public_unit_status(unit_queue_snapshot(book, store))
        report['elapsed_seconds'] = time.perf_counter() - started
        return report
    selected_provider = transport._load_provider(json.loads(Path(config_path).read_text(encoding='utf-8-sig')), provider)
    report.update(provider=selected_provider.name, model=selected_provider.model, currency=selected_provider.currency,
                  price_source='config.json per million tokens; not live billing')
    stop = threading.Event()
    limiter = transport._Limiter(selected_provider.requests_per_minute, stop)
    ordinary_count = min(workers, len(batches))
    partitions = [[] for _ in range(ordinary_count)]
    for index, ids in enumerate(batches):
        partitions[index % ordinary_count].extend(ids)
    count = min(workers, len(chosen_units) + len(partitions))
    report['workers_actual'] = count
    totals = ('review_passed', 'review_failed', 'review_pending', 'review_batches', 'review_split_batches',
              'review_truncated_calls', 'stale_results', 'requests', 'failed_requests', 'usage_missing_calls',
              'input_tokens', 'output_tokens', 'estimated_cost', 'unit_review_passed', 'unit_review_failed',
              'unit_review_pending', 'unit_stale_results', 'unit_rejected_results', 'unit_review_truncated_calls')
    executor = ThreadPoolExecutor(max_workers=count, thread_name_prefix='textbook-joint-review')
    futures, collected = {}, set()

    def collect(future):
        if future in collected:
            return
        kind, ids = futures[future]
        try:
            if future.cancelled():
                report['unit_review_pending' if kind == 'unit' else 'review_pending'] += len(ids)
            else:
                child = future.result()
                for key in totals:
                    report[key] += child.get(key, 0)
                report['unit_results'].extend(child.get('unit_results', []))
                report['cost_estimate_complete'] &= child['cost_estimate_complete']
                report['cancelled'] |= child['cancelled']
                if child.get('provider_blocked'):
                    report['provider_blocked'] = child['provider_blocked']
                report['worker_elapsed_seconds'] += child.get('elapsed_seconds', 0.0)
        except Exception as error:
            report['worker_failures'] += 1
            report['unit_review_pending' if kind == 'unit' else 'review_pending'] += len(ids)
            report['cost_estimate_complete'] = False
            store.record_issue({'code': 'semantic_unit_review_worker_failed' if kind == 'unit' else 'semantic_review_worker_failed',
                                'severity': 'warning', 'detail': 'Review worker did not complete: ' + type(error).__name__})
        collected.add(future)

    try:
        for item in chosen_units:
            future = executor.submit(_run_one_unit, store, item, selected_provider, limiter, stop)
            futures[future] = ('unit', [item['unit_id']])
        for ids in partitions:
            view = replace(book, elements=[elements[eid] for eid in ids])
            future = executor.submit(transport._run_review_sequential, view, store, config_path, selected_provider.name,
                                     _shared_limiter=limiter, _shared_stop=stop, _provider=selected_provider)
            futures[future] = ('ordinary', ids)
        remaining = set(futures)
        while remaining:
            finished, remaining = wait(remaining, timeout=.5, return_when=FIRST_COMPLETED)
            for future in finished:
                collect(future)
    except KeyboardInterrupt:
        report['cancelled'] = True
        stop.set()
        for future in futures:
            future.cancel()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        for future in futures:
            collect(future)
    report['semantic_units_after'] = public_unit_status(unit_queue_snapshot(book, store))
    current = transport.review_status(book, store)
    report['current_review_status'] = {key: value for key, value in current.items() if key != 'ids'}
    report['elapsed_seconds'] = time.perf_counter() - started
    return report
