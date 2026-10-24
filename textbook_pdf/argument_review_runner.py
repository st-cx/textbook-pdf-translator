"""Whole arguments and legacy units share one bounded transport scheduler."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
import json,threading,time
from .models import digest,needs_translation
from .argument_units import registry
from .argument_joint_store import ArgumentJointStore
from .argument_review import validate_response

def argument_queue(book,store):
    units=registry(book)
    if registry(store.book)!=units or book.source_sha256!=store.book.source_sha256:raise ValueError('Caller/Store argument source differs')
    facade=ArgumentJointStore(store);queue=[]
    with facade._read():
        for uid,u in units.items():
            item={**facade.status(uid),'member_ids':list(u['member_ids'])}
            if item['state']not in('awaiting_translation','unverifiable'):
                item['planned']=facade._current(uid)
            queue.append(item)
    return queue

def overlay_argument_status(book,store,base):
    queue=argument_queue(book,store)
    if not queue:return base
    result=deepcopy(base);sets={k:set(v)for k,v in result['ids'].items()}
    for item in queue:
        # Whole units retain all literal/native members in their packet. Element
        # translation counters count only members that actually need translation.
        members={eid for eid in item['member_ids']if needs_translation(book.element_map()[eid])}
        for key in sets:
            if key!='selected':sets[key]-=members
        sets['selected']|=members
        target=item['state']if item['state']in('passed','failed','awaiting_translation')else'pending'
        sets[target]|=members
        if item['state']=='unverifiable':sets['unverifiable']|=members
    result['ids']={k:sorted(v)for k,v in sets.items()}
    for k in('selected','passed','pending','failed','awaiting_translation'):result[k]=len(sets[k])
    result['argument_units']={'counts':{state:sum(i['state']==state for i in queue)for state in('pending','passed','failed','awaiting_translation','unverifiable')},
      'units':[{k:v for k,v in i.items()if k!='planned'}for i in queue]}
    result['individual_semantic_passes_synthesized']=0
    result['coverage_complete']=not any(sets[k]for k in('pending','failed','awaiting_translation','unverifiable'))and all(i['state']=='passed'for i in queue)
    return result

def _request(planned):
    packet=planned['packet'];rules=planned['frozen_rules_readonly'];p=packet['payload']
    if (digest(p)!=packet['argument_review_hash']or set(rules)!={'glossary','style_rules'}
        or digest(rules['glossary'])!=p['glossary_hash']or digest(rules['style_rules'])!=p['style_hash']):
        raise ValueError('Frozen argument rules preimages do not match')
    return {**deepcopy(packet),'frozen_rules_readonly':deepcopy(rules)}

def _run_one(store,planned,provider,limiter,stop):
    from . import translation as t
    facade=ArgumentJointStore(store);packet=planned['packet'];report=t._new_review_report(provider.name,0,0,0,0)
    report.update(argument_review_passed=0,argument_review_failed=0,argument_review_pending=1,argument_stale_results=0,
      argument_rejected_results=0,argument_review_truncated_calls=0,argument_results=[])
    started=time.perf_counter();response=None;reason='not_completed'
    def failure(code,http_status=None):
        report['usage_missing_calls']+=1;report['cost_estimate_complete']=False
        usage={'stage':'review_argument','unit_id':packet['unit_id'],'argument_review_hash':packet['argument_review_hash'],
          'request_failed':True,'usage_known':False,'prompt_tokens':0,'completion_tokens':0,
          'estimated_cost':0.0,'model':provider.model,'currency':provider.currency}
        if http_status is not None:usage['http_status']=http_status
        facade.record_attempt(packet,None,{'usage_known':False},provider.name,code,usage_record=usage)
    for attempt in range(min(3,provider.attempts)):
        accounted=False;attempted=False
        try:
            if stop.is_set():raise t._Cancelled()
            if facade.prepare(packet['unit_id'])!=planned:raise ValueError('argument_stale_before_request')
            request=_request(planned);limiter.acquire()
            if facade.prepare(packet['unit_id'])!=planned:raise ValueError('argument_stale_before_request')
            report['requests']+=1
            attempted=True
            raw,usage=t._chat(provider,[{'role':'system','content':'独立审校完整有序论证的中译：联合核对全部前提到结论的关系、量词、否定、条件方向和只读题干/上下文。不要纠正作者故意无效的论证；不得返回改译或单元素pass。严格按required_response返回整论证JSON。'},
              {'role':'user','content':json.dumps(request,ensure_ascii=False)}])
            # Persist before parsing, including malformed/truncated replies.
            limiter.success()
            inp,out=usage.get('prompt_tokens'),usage.get('completion_tokens');known=isinstance(inp,int)and isinstance(out,int)
            inp,out=(inp,out)if known else(0,0);priced=known and provider.price_in is not None and provider.price_out is not None
            cost=(inp*provider.price_in+out*provider.price_out)/1e6 if priced else 0
            report['input_tokens']+=inp;report['output_tokens']+=out;report['usage_missing_calls']+=not known
            report['estimated_cost']+=cost;report['cost_estimate_complete']&=bool(priced)
            facade.record_attempt(packet,raw,usage,provider.name,usage_record={'stage':'review_argument','unit_id':packet['unit_id'],
              'argument_review_hash':packet['argument_review_hash'],'prompt_tokens':inp,'completion_tokens':out,'usage_known':known,'estimated_cost':cost,'model':provider.model,'currency':provider.currency})
            accounted=True
            if usage.get('_truncated'):raise t._OutputTruncated('Whole argument response truncated')
            response=validate_response(packet,t._json_no_duplicates(raw));break
        except t._Cancelled:report['cancelled']=True;reason='cancelled';break
        except t._OutputTruncated:
            report['failed_requests']+=1;report['argument_review_truncated_calls']+=1;reason='whole_argument_truncated_no_split_or_identical_retry';break
        except t._HTTPFailure as exc:
            report['failed_requests']+=1;report['cost_estimate_complete']=False;reason='http_'+str(exc.status)
            if attempted and not accounted:failure(reason,exc.status)
            if exc.status in {401,402,403}:report['provider_blocked']={'provider':provider.name,'http_status':exc.status};stop.set()
            if exc.status==429 or exc.status>=500:limiter.throttle(exc.retry_after)
            if not exc.retryable:break
        except (ValueError,KeyError,TypeError,OSError)as exc:
            reason=str(exc)
            if reason.startswith('argument_stale'):
                report['argument_stale_results']+=1;break
            report['failed_requests']+=int(attempted);report['argument_rejected_results']+=1
            if attempted and not accounted:failure(type(exc).__name__)
            if not attempted:break
    if response is not None:
        applied=facade.apply(planned,response,provider.name)
        if applied['applied']:
            report['argument_review_pending']=0;report['argument_review_passed'if response['passed']else'argument_review_failed']+=1
            reason='passed'if response['passed']else'failed'
        else:report['argument_stale_results']+=1;reason=applied['reason']
    report['argument_results'].append({'unit_id':packet['unit_id'],'state':reason});report['elapsed_seconds']=time.perf_counter()-started
    return report

def run_review_with_arguments(book,store,config_path,provider,max_elements=None,workers=4,*,unit_ids=None,max_units=None,argument_ids=None,max_arguments=None):
    from . import translation as t
    from . import semantic_unit_runner as legacy
    started=time.perf_counter()
    if not isinstance(workers,int)or isinstance(workers,bool)or not 1<=workers<=32:raise ValueError('workers must be1..32')
    for value in(max_elements,max_units,max_arguments):
        if value is not None and(type(value)is not int or value<0):raise ValueError('Review limits must be nonnegative integers')
    with store.lock:
        if store.db.in_transaction:raise ValueError('Finish caller transaction before model scheduling')
    queue=argument_queue(book,store);wanted=None
    if argument_ids is not None:
        if isinstance(argument_ids,(str,bytes)):raise ValueError('Argument IDs must be an explicit collection')
        wanted=set(argument_ids)
        if wanted-{i['unit_id']for i in queue}:raise ValueError('Unknown argument IDs')
    arguments=[i for i in queue if 'planned'in i and(i['unit_id']in wanted if wanted is not None else i['state']=='pending')]
    if max_arguments is not None:arguments=arguments[:max_arguments]
    legacy_queue=legacy.unit_queue_snapshot(book,store)if book.profile.get('semantic_review_units')else[]
    legacy_wanted=set(unit_ids)if unit_ids is not None and not isinstance(unit_ids,(str,bytes))else None
    if unit_ids is not None and(legacy_wanted is None or legacy_wanted-{i['unit_id']for i in legacy_queue}):raise ValueError('Unknown/invalid legacy unit IDs')
    legacy_jobs=[i for i in legacy_queue if 'packet'in i and(i['unit_id']in legacy_wanted if legacy_wanted is not None else i['state']=='pending')]
    if wanted is not None and unit_ids is None:legacy_jobs=[]
    if max_units is not None:legacy_jobs=legacy_jobs[:max_units]
    glossary,accepted,elements,selected,pending=t._review_snapshot(book,store)
    ordinary=[]if wanted is not None or unit_ids is not None else pending if max_elements is None else pending[:max_elements]
    report=t._new_review_report(provider,len(accepted),len(selected),len(pending),len(ordinary))
    report.update(argument_scheduled=len(arguments),argument_review_passed=0,argument_review_failed=0,argument_review_pending=0,
      argument_stale_results=0,argument_rejected_results=0,argument_review_truncated_calls=0,argument_results=[],unit_results=[],
      unit_scheduled=len(legacy_jobs),unit_review_passed=0,unit_review_failed=0,unit_review_pending=0,
      unit_stale_results=0,unit_rejected_results=0,unit_review_truncated_calls=0,
      semantic_units_before=legacy.public_unit_status(legacy_queue),
      workers_requested=workers,workers_actual=0,worker_failures=0,individual_semantic_passes_synthesized=0)
    batches=t._review_batches(ordinary,elements,accepted)
    if not arguments and not legacy_jobs and not batches:report['elapsed_seconds']=time.perf_counter()-started;return report
    cfg=json.loads(Path(config_path).read_text(encoding='utf-8-sig'));p=t._load_provider(cfg,provider)
    stop=threading.Event();limiter=t._Limiter(p.requests_per_minute,stop);jobs=[]
    jobs.extend((_run_one,(store,item['planned'],p,limiter,stop))for item in arguments)
    jobs.extend((legacy._run_one_unit,(store,item,p,limiter,stop))for item in legacy_jobs)
    for batch in batches:
        view=replace(book,elements=[elements[eid]for eid in batch])
        jobs.append((lambda v:t._run_review_sequential(v,store,config_path,p.name,_shared_limiter=limiter,_shared_stop=stop,_provider=p),(view,)))
    report['workers_actual']=min(workers,len(jobs));report.update(provider=p.name,model=p.model,currency=p.currency)
    totals=('review_passed','review_failed','review_pending','review_batches','review_split_batches','review_truncated_calls','stale_results',
      'requests','failed_requests','usage_missing_calls','input_tokens','output_tokens','estimated_cost',
      'unit_review_passed','unit_review_failed','unit_review_pending','unit_stale_results','unit_rejected_results','unit_review_truncated_calls',
      'argument_review_passed','argument_review_failed','argument_review_pending','argument_stale_results','argument_rejected_results','argument_review_truncated_calls')
    with ThreadPoolExecutor(max_workers=report['workers_actual'])as pool:
        futures=[pool.submit(fn,*args)for fn,args in jobs]
        try:
            remaining=set(futures)
            while remaining:
                completed,remaining=wait(remaining,timeout=.5,return_when=FIRST_COMPLETED)
                for f in completed:
                    try:child=f.result()
                    except Exception:report['worker_failures']+=1;report['cost_estimate_complete']=False;continue
                    for k in totals:report[k]+=child.get(k,0)
                    report['argument_results']+=child.get('argument_results',[]);report['unit_results']+=child.get('unit_results',[])
                    report['cost_estimate_complete']&=child['cost_estimate_complete']
                    report['cancelled']|=child.get('cancelled',False)
                    if child.get('provider_blocked')is not None:report['provider_blocked']=child['provider_blocked']
        except KeyboardInterrupt:stop.set();report['cancelled']=True
    report['argument_units_after']=[{k:v for k,v in i.items()if k!='planned'}for i in argument_queue(book,store)]
    report['semantic_units_after']=legacy.public_unit_status(legacy.unit_queue_snapshot(book,store))if legacy_queue else legacy.public_unit_status([])
    report['elapsed_seconds']=time.perf_counter()-started
    return report
