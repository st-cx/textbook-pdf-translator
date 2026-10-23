"""Private explicit semantic-unit reviews; no per-element semantic pass copying.

All helpers are transport-independent. UnitReviewStore uses an existing Store's
synthetic/test connection; callers must never instantiate it on a real job until
the explicit opt-in and the source-bound contract have been independently vetted.
"""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict
import json,re,time
from pathlib import Path
from .models import Element,Book,TOKEN_RE,RULES_VERSION,digest,protected_asset_descriptor,validate_translation
from .store import translation_rule_hash
from .translation import STYLE_RULES

SCHEMA='semantic-unit-review-v1'
PROFILE_KEY='semantic_review_units'
ALLOWED_ROLES={'body_prose','reading_guide_prose'}

def definitions(book):
    units=book.profile.get(PROFILE_KEY,{})
    if not isinstance(units,dict):raise ValueError('Unit registry must be an explicit mapping')
    seen=set()
    for uid,u in units.items():
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+',uid) or u.get('id')!=uid or u.get('schema')!=SCHEMA
            or u.get('role')not in ALLOWED_ROLES or not u.get('source_visual_evidence')
            or len(u.get('member_ids',[]))!=2 or len(set(u['member_ids']))!=2
            or u.get('source_pdf_sha256')!=book.source_sha256
            or len(u.get('boundary',{}).get('left_suffix',''))==0
            or len(u.get('boundary',{}).get('right_prefix',''))==0):raise ValueError('Unverified/unsupported explicit prose unit')
        if seen.intersection(u['member_ids']):raise ValueError('Overlapping semantic units require a stronger protocol')
        seen.update(u['member_ids'])
    return units

def member_ids(book):return {eid for u in definitions(book).values()for eid in u['member_ids']}

def build_packet(definition,snapshot):
    """Pure snapshot→immutable packet. Never infer a unit or revise text."""
    uid=definition['id'];members=definition['member_ids'];rows=snapshot['elements']
    if definition.get('schema')!=SCHEMA or not definition.get('source_visual_evidence'):raise ValueError('Missing immutable source evidence')
    if definition['role']not in ALLOWED_ROLES:raise ValueError('Index/bibliography entries are not prose units')
    if snapshot['source_pdf_sha256']!=definition['source_pdf_sha256']:raise ValueError('Different original PDF')
    for eid in members:
        r=rows[eid]
        if r['element']['kind']!='paragraph':raise ValueError('Only explicitly proved prose paragraphs supported')
        if Element(**r['element']).source_hash!=r['source_hash']:raise ValueError('Inconsistent pure member source hash')
        if digest(r['element'])!=definition['member_source_full_sha256'][eid]:raise ValueError('Member source/role/geometry changed')
        t=r['translation']
        if (not t or t['status']not in('accepted','review_failed') or t['source_hash']!=r['source_hash']
            or t['rule_hash']!=r['rule_hash']):raise ValueError('Missing or stale member revision')
    for eid in definition.get('context_ids',[])+definition.get('intervening_header_ids',[]):
        if Element(**rows[eid]['element']).source_hash!=rows[eid]['source_hash']:raise ValueError('Inconsistent pure context source hash')
        if digest(rows[eid]['element'])!=definition['context_source_full_sha256'][eid]:raise ValueError('Context/header source changed')
    for eid in definition.get('intervening_header_ids',[]):
        if rows[eid]['element']['kind']!='running_header':raise ValueError('Cannot skip intervening body/index/figure content')
    chain=definition.get('adjacency_chain',[])
    if chain!=[members[0]]+definition.get('intervening_header_ids',[])+[members[1]]:raise ValueError('Incomplete source adjacency chain')
    positions=[definition['source_ordinals'][eid]for eid in chain]
    if positions!=list(range(positions[0],positions[0]+len(chain))):raise ValueError('Intervening source content was omitted')
    left,right=[rows[eid]['element']for eid in members]
    b=definition['boundary']
    if not left['text'].endswith(b['left_suffix'])or not right['text'].startswith(b['right_prefix']):raise ValueError('Source continuation anchor changed')
    if right['page']-left['page']!=1:raise ValueError('This prototype accepts one proved adjacent-page boundary only')
    if b.get('source_join')=='space':joined_source=left['text']+' '+right['text']
    elif b.get('source_join')=='exact_discretionary_hyphen':
        if not b['left_suffix'].endswith('-')or not b.get('original_hyphen_glyph_evidence'):raise ValueError('Unproved hyphen omission')
        joined_source=left['text'][:-1]+right['text']
    else:raise ValueError('Unsupported source join')
    bound={eid:deepcopy(rows[eid])for eid in members+definition.get('context_ids',[])+definition.get('intervening_header_ids',[])}
    # A previous accepted/review_failed status is a workflow state, not content
    # identity. Exact source/rule/text stay bound when restoring an authorized
    # false failure. Missing/stale/noneligible status was rejected above.
    for r in bound.values():
        if r['translation']:r['translation'].pop('status',None)
    from .models import file_sha256
    payload={'schema':SCHEMA,'implementation_sha256':file_sha256(Path(__file__)),'unit_id':uid,'definition':deepcopy(definition),'source_pdf_sha256':snapshot['source_pdf_sha256'],
        'rules_version':RULES_VERSION,'glossary_hash':snapshot['glossary_hash'],'style_hash':digest(STYLE_RULES),
        'elements':bound,'ordered_member_ids':members,
        'source_fragments':[{'element_id':eid,'physical_page':rows[eid]['element']['page'],
            'printed_page':rows[eid]['element'].get('printed_page'),'bbox':rows[eid]['element']['bbox']}for eid in members],
        'joined_source_readonly':joined_source,'joined_translation_readonly':'\n'.join(rows[eid]['translation']['text']for eid in members),
        'boundary_instructions':'Review the whole semantic unit. Natural Chinese may move a phrase across the original page boundary. Check it occurs once overall; do not demand it in each fragment. Do not return or modify translations, source IDs, original pages, numbers or protected objects.',
        'individual_failures_to_reassess_readonly':definition.get('authorized_issue_resolutions',[])}
    binding=digest(payload)
    return {'unit_id':uid,'unit_review_hash':binding,'payload':payload,
        'required_response':{'unit_id':uid,'unit_review_hash':binding,'passed':True,'issues':[],
            'boundary_coverage':{'boundary_id':b['id'],'reviewed_together':True,'no_omission_or_duplicate':True}}}

def validate_response(packet,response):
    keys={'unit_id','unit_review_hash','passed','issues','boundary_coverage'}
    if not isinstance(response,dict)or set(response)!=keys:raise ValueError('Review-only envelope; translation patches forbidden')
    if response['unit_id']!=packet['unit_id']or response['unit_review_hash']!=packet['unit_review_hash']:raise ValueError('Different/stale semantic unit')
    if type(response['passed'])is not bool or not isinstance(response['issues'],list)or any(not isinstance(i,str)for i in response['issues']):raise ValueError('Invalid verdict')
    coverage=response['boundary_coverage']
    if (not isinstance(coverage,dict)or set(coverage)!={'boundary_id','reviewed_together','no_omission_or_duplicate'}
        or coverage['boundary_id']!=packet['payload']['definition']['boundary']['id']or coverage['reviewed_together']is not True
        or type(coverage['no_omission_or_duplicate'])is not bool):raise ValueError('Explicit joint-boundary review required')
    if response['passed']and(response['issues']or coverage['no_omission_or_duplicate']is not True):raise ValueError('Passing verdict contradicts boundary review')
    if not response['passed']and not response['issues']:raise ValueError('A failing joint review requires concrete issues')
    return deepcopy(response)

class UnitReviewStore:
    """Lazily adds a new result table only on commit; old failures stay history."""
    def __init__(self,store):self.store=store
    @contextmanager
    def _read(self):
        s=self.store
        with s.lock:
            nested=s.db.in_transaction
            if not nested:s.db.execute('BEGIN')
            try:yield
            finally:
                if not nested:s.db.rollback()
    @contextmanager
    def _write(self):
        s=self.store
        with s.lock:
            outer_owned=s.db.in_transaction
            with s._atomic_write():yield outer_owned
    def _current(self,uid):
        s=self.store
        local=definitions(s.book)
        if uid not in local:raise ValueError('Unit is not explicitly opted in')
        u=local[uid]
        ids=u['member_ids']+u.get('context_ids',[])+u.get('intervening_header_ids',[])
        if len(ids)!=len(set(ids)):raise ValueError('Member/context/header identity overlap')
        sources={};translations={};aids_by_element={};positions={}
        for eid in ids:
            row=s.db.execute('SELECT * FROM elements WHERE id=?',(eid,)).fetchone()
            if not row:raise ValueError('Missing current source member/context')
            e=Element(**json.loads(row['json']))
            position=u['source_ordinals'][eid]
            if not isinstance(position,int)or isinstance(position,bool)or position<0:raise ValueError('Invalid source ordinal')
            sources[eid]=(row,e);positions[eid]=position
            translations[eid]=s.db.execute('SELECT * FROM translations WHERE element_id=?',(eid,)).fetchone()
            aids_by_element[eid]=sorted(set(e.asset_ids)|set(TOKEN_RE.findall(e.text)))
        # All values come from the caller's same held read/write transaction.
        # Extract many small fields in one parse of the large registered Book.
        # Bounded chunks avoid SQLite argument limits for unusually long units.
        header=['$.source_sha256','$.profile.'+PROFILE_KEY+'.'+json.dumps(uid),'$.schema_version','$.profile.job_dir']
        body_paths={eid:f'$.elements[{positions[eid]}]'for eid in ids}
        asset_paths={aid:'$.assets.'+json.dumps(aid)for aids in aids_by_element.values()for aid in aids}
        paths=list(dict.fromkeys(header+list(body_paths.values())+list(asset_paths.values())))
        persisted={}
        for index in range(0,len(paths),32):
            chunk=paths[index:index+32]
            arguments=chunk if len(chunk)>1 else chunk+['$.schema_version']
            row=s.db.execute('SELECT json_extract(value,'+','.join('?'for _ in arguments)+") FROM metadata WHERE key='book'",arguments).fetchone()
            if not row:raise ValueError('Current registered book missing')
            values=json.loads(row[0])
            persisted.update(zip(chunk,values))
        source,current_unit,version,registered_root=[persisted[p]for p in header]
        if current_unit!=u or source!=s.book.source_sha256 or version!=s.book.schema_version:raise ValueError('Registered unit or source differs from local session')
        if registered_root!=s.book.profile.get('job_dir'):raise ValueError('Registered source root differs from local session')
        if registered_root is not None and(not isinstance(registered_root,str)or not registered_root.strip()):
            raise ValueError('Invalid registered source root')
        source_root=Path(registered_root).resolve()if registered_root is not None else s.path.parent.resolve()
        glossary=json.loads(s.db.execute("SELECT value FROM metadata WHERE key='glossary'").fetchone()[0])
        rows={};actual={};assets={}
        for eid in ids:
            r,e=sources[eid];t=translations[eid]
            if persisted[body_paths[eid]]!=asdict(e):raise ValueError('Source ordering or persisted body/context changed')
            aids=aids_by_element[eid]
            for aid in aids:
                value=persisted[asset_paths[aid]]
                if not value:raise ValueError('Missing current protected descriptor')
                from .models import Asset,file_sha256
                asset=Asset(**value);path=Path(asset.path)
                if not path.is_absolute():
                    roots=(source_root,s.path.parent.resolve())
                    path=next((root/path for root in roots if(root/path).is_file()),source_root/path)
                path=path.resolve()
                if not path.is_file()or file_sha256(path)!=asset.sha256:raise ValueError('Protected actual file changed/unavailable')
                assets[aid]=asset;actual[aid]={'file_sha256':asset.sha256,'full_asset_sha256':digest(asdict(asset))}
            small=Book('',source,'',0,[],elements=[e],assets={aid:assets[aid]for aid in aids})
            if e.source_hash!=r['source_hash']or translation_rule_hash(small,e,glossary)!=r['rule_hash']:raise ValueError('Current source/rule inconsistent')
            tr=None
            if t:
                if t['source_hash']!=r['source_hash']or t['rule_hash']!=r['rule_hash']or t['status']not in('accepted','review_failed'):
                    raise ValueError('Member/context translation is not a current revision')
                tr={k:t[k]for k in('text','source_hash','rule_hash','status')}
                tr['translation_hash']=digest(t['text'])
            rows[eid]={'element':asdict(e),'source_hash':r['source_hash'],'rule_hash':r['rule_hash'],
                'translation':tr,'protected_assets':{aid:protected_asset_descriptor(assets[aid])for aid in aids},
                'actual_asset_bindings':{aid:actual[aid]for aid in aids}}
        snapshot={'source_pdf_sha256':source,'glossary_hash':digest(glossary),'elements':rows}
        return build_packet(u,snapshot),snapshot,glossary,assets
    def packet(self,uid):
        s=self.store
        with self._read():
            return self._current(uid)[0]
    def status(self,uid):
        s=self.store
        with self._read():
            try:packet=self._current(uid)[0]
            except (ValueError,KeyError,TypeError):return {'unit_id':uid,'state':'pending_unverifiable','current':False}
            exists=s.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_unit_reviews'").fetchone()
            row=s.db.execute('SELECT response_json FROM semantic_unit_reviews WHERE unit_id=? AND unit_review_hash=? ORDER BY id DESC LIMIT 1',
                (uid,packet['unit_review_hash'])).fetchone()if exists else None
            if not row:return {'unit_id':uid,'state':'pending','current':True,'unit_review_hash':packet['unit_review_hash']}
            response=validate_response(packet,json.loads(row[0]))
            if response['passed']:
                for eid in packet['payload']['ordered_member_ids']:
                    r=packet['payload']['elements'][eid]
                    for f in s.db.execute("SELECT json FROM issues WHERE resolved=0 AND json_extract(json,'$.code')='semantic_review_failed' AND json_extract(json,'$.element_id')=?",(eid,)).fetchall():
                        issue=json.loads(f[0])
                        if issue.get('source_hash')==r['source_hash']and issue.get('translation_hash')==r['translation']['translation_hash']:
                            return {'unit_id':uid,'state':'failed','current':True,'reason':'current_failure_after_or_outside_unit_verdict'}
            return {'unit_id':uid,'state':'passed'if response['passed']else'failed','current':True,'unit_review_hash':packet['unit_review_hash']}
    def apply(self,packet,response,provider='synthetic',fault=None):
        validate_response(packet,response);s=self.store
        with self._write() as outer_owned:
            try:current,snapshot,glossary,assets=self._current(packet['unit_id'])
            except (ValueError,KeyError,TypeError):return {'applied':False,'stale':True,'reason':'unit_source_context_assets_or_revision_changed'}
            if current!=packet:return {'applied':False,'stale':True,'reason':'unit_binding_changed'}
            u=packet['payload']['definition'];resolutions=u.get('authorized_issue_resolutions',[])if response['passed']else[]
            authorized={x['fingerprint']:x for x in resolutions}
            currentfailures=[]
            for eid in u['member_ids']:
                r=snapshot['elements'][eid];t=r['translation']
                e=Element(**r['element']);small=Book('',snapshot['source_pdf_sha256'],'',0,[],elements=[e],assets=assets)
                if any(i.get('severity')=='error'for i in validate_translation(e,t['text'],small)):return {'applied':False,'stale':False,'reason':'member_structural_validation_failed'}
                failures=s.db.execute("SELECT fingerprint,json,resolved FROM issues WHERE json_extract(json,'$.element_id')=? AND json_extract(json,'$.code')='semantic_review_failed'",(eid,)).fetchall()
                for f in failures:
                    issue=json.loads(f['json'])
                    if (not f['resolved']and issue.get('source_hash')==r['source_hash']and issue.get('translation_hash')==t['translation_hash']):
                        currentfailures.append(f)
                        if response['passed']and(f['fingerprint']not in authorized or authorized[f['fingerprint']]['json_sha256']!=digest(issue)):
                            return {'applied':False,'stale':False,'reason':'unauthorized_current_failure'}
                if response['passed']and t['status']=='review_failed'and eid not in u.get('restore_review_failed_member_ids',[]):
                    return {'applied':False,'stale':False,'reason':'review_failed_restore_not_authorized'}
            if fault:fault('after_all_member_checks')
            try:latest=self._current(packet['unit_id'])[0]
            except (ValueError,KeyError,TypeError):return {'applied':False,'stale':True,'reason':'dependency_changed_after_checks'}
            if latest!=current:return {'applied':False,'stale':True,'reason':'dependency_changed_after_checks'}
            s.db.execute('CREATE TABLE IF NOT EXISTS semantic_unit_reviews(id INTEGER PRIMARY KEY,unit_id TEXT NOT NULL,unit_review_hash TEXT NOT NULL,packet_json TEXT NOT NULL,response_json TEXT NOT NULL,provider TEXT NOT NULL,created_at REAL NOT NULL)')
            s.db.execute('INSERT INTO semantic_unit_reviews(unit_id,unit_review_hash,packet_json,response_json,provider,created_at) VALUES(?,?,?,?,?,?)',
                (packet['unit_id'],packet['unit_review_hash'],json.dumps(packet,ensure_ascii=False),json.dumps(response,ensure_ascii=False),provider,time.time()))
            # Keep a dependency marker, never a per-element pass. If someone
            # removes the opt-in registry, normal readers still see review due.
            for eid in u['member_ids']:
                r=snapshot['elements'][eid]
                s.record_issue({'code':'semantic_review_pending','severity':'warning','element_id':eid,
                    'page':r['element']['page'],'source_hash':r['source_hash'],
                    'translation_hash':r['translation']['translation_hash'],'semantic_unit_id':u['id'],
                    'semantic_unit_review_hash':packet['unit_review_hash'],
                    'detail':'Requires current full-member semantic-unit evidence; this marker is not an individual passing review.'})
            if response['passed']:
                for f in currentfailures:
                    s.db.execute('UPDATE issues SET resolved=1,resolution_reason=? WHERE fingerprint=? AND resolved=0',
                        ('semantic_unit_context_passed:'+packet['unit_review_hash'],f['fingerprint']))
                for eid in u.get('restore_review_failed_member_ids',[]):
                    if eid not in u['member_ids']:raise ValueError('Cannot restore nonmember')
                    r=snapshot['elements'][eid];t=r['translation']
                    s.db.execute("UPDATE translations SET status='accepted' WHERE element_id=? AND source_hash=? AND rule_hash=? AND text=? AND status='review_failed'",(eid,r['source_hash'],r['rule_hash'],t['text']))
                s._finish_covered_tasks()
            if fault:fault('after_writes_before_commit')
            return {'applied':True,'stale':False,'unit_state':'passed'if response['passed']else'failed',
                'individual_semantic_passes_written':0,'exact_old_failure_rows_resolved':len(currentfailures)if response['passed']else 0,
                'commit_state':'pending_outer_transaction'if outer_owned else'committed_when_returned'}

def overlay_review_status(book,store,base):
    """Derived unit coverage; default no-registry result stays byte-identical."""
    units=definitions(book)
    if not units:return base
    out=deepcopy(base);sets={k:set(out['ids'][k])for k in out['ids']}
    states=[]
    for uid,u in units.items():
        state=UnitReviewStore(store).status(uid);states.append(state);members=set(u['member_ids'])
        for key in sets:
            if key!='selected':sets[key]-=members
        sets['selected']|=members
        sets['passed'if state['state']=='passed'else'failed'if state['state']=='failed'else'pending']|=members
    for key,values in sets.items():out['ids'][key]=sorted(values)
    for key in('selected','passed','pending','failed','awaiting_translation'):out[key]=len(sets[key])
    out['semantic_units']=states;out['individual_semantic_passes_synthesized']=0
    out['coverage_complete']=not any(sets[k]for k in('pending','failed','awaiting_translation'))
    return out
