"""Lazy independent transactional argument verdict table; no per-member passes."""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import json,time
from .models import Book,Element,Asset,TOKEN_RE,digest,file_sha256,needs_translation,protected_asset_descriptor,SCHEMA_VERSION,RULES_VERSION
from .argument_units import registry,checked_units,PROFILE_KEY
from .argument_review import build_packet,validate_response

class ArgumentJointStore:
    def __init__(self,store):
        self.store=store
        checked_units(store.book,scoped=True)
    @contextmanager
    def _read(self):
        s=self.store
        with s.lock:
            owned=s.db.in_transaction
            if not owned:s.db.execute('BEGIN')
            try:yield
            finally:
                if not owned:s.db.rollback()
    def _result_token(self,packet):
        db=self.store.db
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table'AND name='argument_joint_reviews'").fetchone()
        if not exists:return None
        row=db.execute('SELECT id,response_json FROM argument_joint_reviews WHERE unit_id=? AND argument_review_hash=? ORDER BY id DESC LIMIT 1',
          (packet['unit_id'],packet['argument_review_hash'])).fetchone()
        return digest({'id':row['id'],'response_json':row['response_json']})if row else None
    def _current(self,uid):
        s=self.store;units=registry(s.book)
        if uid not in units:raise ValueError('Argument is not explicitly opted in')
        u=units[uid];positions={e.id:i for i,e in enumerate(s.book.elements)};members=u['member_ids'];chain=[u['parent_id']]+members
        start,end=positions[chain[0]],positions[chain[-1]]
        context=[e.id for e in s.book.elements[max(0,start-2):start]]+[e.id for e in s.book.elements[end+1:end+3]]
        ids=chain+context;lookup=s.book.element_map();all_aids=sorted({aid for eid in ids for aid in set(lookup[eid].asset_ids)|set(TOKEN_RE.findall(lookup[eid].text))})
        head=['$.source_sha256','$.schema_version','$.profile.'+PROFILE_KEY,'$.profile.job_dir']
        elementpaths={eid:f'$.elements[{positions[eid]}]'for eid in ids};assetpaths={aid:'$.assets.'+json.dumps(aid)for aid in all_aids}
        paths=head+list(elementpaths.values())+list(assetpaths.values());persisted={}
        for offset in range(0,len(paths),32):
            chunk=paths[offset:offset+32];arguments=chunk if len(chunk)>1 else chunk+['$.schema_version']
            row=s.db.execute('SELECT json_extract(value,'+','.join('?'for _ in arguments)+")FROM metadata WHERE key='book'",arguments).fetchone()
            if not row or row[0]is None:raise ValueError('Registered argument book missing')
            persisted.update(zip(chunk,json.loads(row[0])))
        source,version,registered,jobroot=[persisted[p]for p in head]
        if source!=s.book.source_sha256 or version!=s.book.schema_version or registered!=units:
            raise ValueError('Registered argument source/profile differs from current session')
        if jobroot!=s.book.profile.get('job_dir'):raise ValueError('Registered argument root differs')
        root=Path(jobroot).resolve()if jobroot else s.path.parent.resolve();assets={}
        for aid in all_aids:
            a=Asset(**persisted[assetpaths[aid]]);p=(root/a.path).resolve()
            if not p.is_relative_to(root)or not p.is_file()or file_sha256(p)!=a.sha256:
                raise ValueError('Actual argument asset unavailable/changed/outside registered root')
            assets[aid]=a
        grow=s.db.execute("SELECT value FROM metadata WHERE key='glossary'").fetchone()
        if not grow:raise ValueError('Argument frozen glossary missing')
        glossary=json.loads(grow[0]);rows={}
        from .store import translation_rule_hash
        from .translation import STYLE_RULES
        for eid in ids:
            source_row=s.db.execute('SELECT * FROM elements WHERE id=?',(eid,)).fetchone()
            if not source_row:raise ValueError('Current argument source/context missing')
            e=Element(**json.loads(source_row['json']))
            if persisted[elementpaths[eid]]!=asdict(e)or asdict(e)!=asdict(lookup[eid]):
                raise ValueError('Argument source/context/order changed')
            tiny=Book('',source,'',0,[],elements=[e],assets=assets,profile={PROFILE_KEY:registered})
            rule=translation_rule_hash(tiny,e,glossary)
            if source_row['source_hash']!=e.source_hash or source_row['rule_hash']!=rule:
                raise ValueError('Argument current source/rule mismatch')
            t=s.db.execute('SELECT * FROM translations WHERE element_id=?',(eid,)).fetchone();tr=None
            if t:
                if t['source_hash']!=e.source_hash or t['rule_hash']!=rule or t['status']not in('accepted','review_failed'):
                    raise ValueError('Argument/context translation is stale/ineligible')
                tr={'text':t['text'],'source_hash':t['source_hash'],'rule_hash':t['rule_hash'],'translation_hash':digest(t['text'])}
            aids=sorted(set(e.asset_ids)|set(TOKEN_RE.findall(e.text)))
            rows[eid]={'element':asdict(e),'source_hash':e.source_hash,'rule_hash':rule,'translation':tr,
              'needs_translation':needs_translation(e),'rendered_or_review_text':tr['text']if tr else e.text,
              'protected_assets':{aid:protected_asset_descriptor(assets[aid])for aid in aids},
              'actual_asset_bindings':{aid:{'actual_file_sha256':assets[aid].sha256,'complete_asset_sha256':digest(asdict(assets[aid]))}for aid in aids}}
        snapshot={'source_pdf_sha256':source,'context_ids':context,'source_ordered_ordinals':{eid:positions[eid]for eid in ids},
          'elements':rows,'glossary_hash':digest(glossary),'style_hash':digest(STYLE_RULES)}
        packet=build_packet(u,snapshot)
        # One complete current-transaction preimage serves all dependency
        # review hashes. A long-lived Store's in-memory Book/glossary may be
        # older than another Store's normal registration of the same source.
        preimage={'packet_sha256':digest(packet),'source_pdf_sha256':source,'schema_version':version,
          'argument_registry':registered,'elements':{eid:row['element']for eid,row in rows.items()},
          'assets':{aid:asdict(a)for aid,a in assets.items()},'glossary':glossary,'style_rules':list(STYLE_RULES)}
        self._review_book_from_preimage(packet,preimage)
        return {'packet':packet,'result_token':self._result_token(packet),
          'frozen_rules_readonly':{'glossary':glossary,'style_rules':list(STYLE_RULES)},
          'current_review_preimage':preimage}
    def prepare(self,uid):
        with self._read():return self._current(uid)
    @staticmethod
    def _review_book_from_preimage(packet,preimage):
        """Validate complete current records against the actual joint packet.

        This is deliberately independent of every cached session Book field.
        The caller must obtain the preimage from _current in the same read or
        write transaction; apply also compares that entire planned snapshot.
        """
        from .store import translation_rule_hash
        from .translation import STYLE_RULES
        p=packet['payload'];rows=p['elements']
        if (not isinstance(preimage,dict)or preimage.get('packet_sha256')!=digest(packet)
            or digest(p)!=packet['argument_review_hash']
            or preimage.get('source_pdf_sha256')!=p['source_pdf_sha256']
            or preimage.get('schema_version')!=SCHEMA_VERSION
            or not isinstance(preimage.get('glossary'),dict)
            or digest(preimage['glossary'])!=p['glossary_hash']
            or preimage.get('style_rules')!=list(STYLE_RULES)
            or digest(preimage['style_rules'])!=p['style_hash']or p['rules_version']!=RULES_VERSION):
            raise ValueError('Current argument review preimage source/rules/style do not match packet')
        units=preimage.get('argument_registry')
        if not isinstance(units,dict)or units.get(packet['unit_id'])!=p['definition']:
            raise ValueError('Current argument review preimage registry does not match packet')
        if preimage.get('elements')!={eid:row['element']for eid,row in rows.items()}:
            raise ValueError('Current argument review preimage elements do not match packet')
        ids=sorted(rows,key=p['source_ordered_ordinals'].__getitem__)
        elements=[Element(**preimage['elements'][eid])for eid in ids]
        assets={aid:Asset(**a)for aid,a in preimage.get('assets',{}).items()}
        required={aid for e in elements for aid in set(e.asset_ids)|set(TOKEN_RE.findall(e.text))}
        if required!=set(assets):raise ValueError('Current argument review preimage asset scope does not match packet')
        book=Book('',preimage['source_pdf_sha256'],'',0,[],elements=elements,assets=assets,
          profile={PROFILE_KEY:units},schema_version=preimage['schema_version'])
        for e in elements:
            row=rows[e.id];aids=sorted(set(e.asset_ids)|set(TOKEN_RE.findall(e.text)))
            expected={aid:protected_asset_descriptor(assets[aid])for aid in aids}
            bindings={aid:{'actual_file_sha256':assets[aid].sha256,'complete_asset_sha256':digest(asdict(assets[aid]))}for aid in aids}
            if row['protected_assets']!=expected or row['actual_asset_bindings']!=bindings:
                raise ValueError('Current argument review preimage complete assets do not match packet')
            rule=translation_rule_hash(book,e,preimage['glossary'])
            if row['source_hash']!=e.source_hash or row['rule_hash']!=rule or row['needs_translation']!=needs_translation(e):
                raise ValueError('Current argument review preimage element rules do not match packet')
            tr=row['translation']
            if tr is not None and(tr['source_hash']!=e.source_hash or tr['rule_hash']!=rule or tr['translation_hash']!=digest(tr['text'])):
                raise ValueError('Current argument review preimage translation does not match packet')
        return book
    def _current_individual_failures(self,packet,*,current_review_preimage):
        """Hold a joint pass if any translated actual packet dependency failed."""
        from .translation import _review_hash
        book=self._review_book_from_preimage(packet,current_review_preimage)
        glossary=current_review_preimage['glossary']
        failures=[]
        # Parent and readonly neighbours participate in this joint judgement,
        # so a known wrong current translation there cannot be hidden by it.
        for eid,row in packet['payload']['elements'].items():
            if not row['needs_translation']or row['translation']is None:continue
            e=Element(**row['element'])
            for old in self.store.db.execute("SELECT json FROM issues WHERE resolved=0 AND json_extract(json,'$.element_id')=? AND json_extract(json,'$.code')='semantic_review_failed'",(eid,)).fetchall():
                issue=json.loads(old[0])
                if (issue.get('source_hash')==e.source_hash and issue.get('translation_hash')==row['translation']['translation_hash']
                    and issue.get('review_hash')==_review_hash(e,row['translation']['text'],book,glossary)):
                    failures.append(eid);break
        return failures
    def status(self,uid):
        with self._read():
            try:planned=self._current(uid)
            except (ValueError,KeyError,TypeError,OSError)as exc:
                return {'unit_id':uid,'state':'awaiting_translation'if str(exc).startswith('awaiting_argument_translation:')else'unverifiable','reason':str(exc)}
            if planned['result_token']is None:return {'unit_id':uid,'state':'pending','argument_review_hash':planned['packet']['argument_review_hash']}
            row=self.store.db.execute('SELECT response_json FROM argument_joint_reviews WHERE unit_id=? AND argument_review_hash=? ORDER BY id DESC LIMIT 1',
              (uid,planned['packet']['argument_review_hash'])).fetchone();response=validate_response(planned['packet'],json.loads(row[0]))
            failures=self._current_individual_failures(planned['packet'],current_review_preimage=planned['current_review_preimage'])
            if response['passed']and failures:
                return {'unit_id':uid,'state':'failed','argument_review_hash':planned['packet']['argument_review_hash'],
                  'reason':'current_individual_failure_requires_explicit_resolution_authority','dependency_ids_with_current_failure':failures}
            return {'unit_id':uid,'state':'passed'if response['passed']else'failed','argument_review_hash':planned['packet']['argument_review_hash']}
    def apply(self,planned,response,provider='fake',fault=None):
        packet=planned['packet'];validate_response(packet,response);s=self.store
        with s.lock,s._atomic_write():
            try:current=self._current(packet['unit_id'])
            except (ValueError,KeyError,TypeError,OSError):return {'applied':False,'stale':True,'reason':'source_context_assets_or_current_revision_changed'}
            if current!=planned:return {'applied':False,'stale':True,'reason':'argument_binding_or_result_CAS_changed'}
            from .models import validate_translation
            review_book=self._review_book_from_preimage(packet,current['current_review_preimage'])
            for eid in packet['payload']['ordered_member_ids']:
                row=packet['payload']['elements'][eid];e=Element(**row['element'])
                if row['needs_translation']:
                    if any(v.get('severity')=='error'for v in validate_translation(e,row['translation']['text'],review_book)):
                        return {'applied':False,'stale':False,'reason':'argument_member_validation_failed'}
            if response['passed']and self._current_individual_failures(packet,current_review_preimage=current['current_review_preimage']):
                return {'applied':False,'stale':False,'reason':'current_individual_failure_requires_explicit_resolution_authority'}
            if fault:fault('after_current_checks')
            if self._current(packet['unit_id'])!=current:return {'applied':False,'stale':True,'reason':'dependency_changed_after_checks'}
            s.db.execute('CREATE TABLE IF NOT EXISTS argument_joint_reviews(id INTEGER PRIMARY KEY,unit_id TEXT NOT NULL,argument_review_hash TEXT NOT NULL,packet_json TEXT NOT NULL,response_json TEXT NOT NULL,provider TEXT NOT NULL,created_at REAL NOT NULL)')
            s.db.execute('INSERT INTO argument_joint_reviews(unit_id,argument_review_hash,packet_json,response_json,provider,created_at)VALUES(?,?,?,?,?,?)',
              (packet['unit_id'],packet['argument_review_hash'],json.dumps(packet,ensure_ascii=False),json.dumps(response,ensure_ascii=False),provider,time.time()))
            if fault:fault('after_insert_before_commit')
            return {'applied':True,'stale':False,'argument_passed':response['passed'],'individual_semantic_passes_created':0,
              'source_profile_writes':0,'existing_semantic_failures_resolved':0}

    def record_attempt(self,packet,raw,usage,provider,error=None,usage_record=None):
        """Persist raw before parsing, then use Store's observable usage API."""
        s=self.store
        with s.lock,s._atomic_write():
            s.db.execute('CREATE TABLE IF NOT EXISTS argument_joint_attempts(id INTEGER PRIMARY KEY,unit_id TEXT NOT NULL,argument_review_hash TEXT NOT NULL,raw_response TEXT,usage_json TEXT NOT NULL,provider TEXT NOT NULL,error_code TEXT,created_at REAL NOT NULL)')
            s.db.execute('INSERT INTO argument_joint_attempts(unit_id,argument_review_hash,raw_response,usage_json,provider,error_code,created_at)VALUES(?,?,?,?,?,?,?)',
              (packet['unit_id'],packet['argument_review_hash'],raw,json.dumps(usage,ensure_ascii=False),provider,error,time.time()))
        if usage_record is not None:
            # Keep existing budget/telemetry wrappers around record_usage in
            # the path; direct SQL would hide attempts from those observers.
            s.record_usage('argument-review-'+packet['argument_review_hash'][:24],provider,usage_record)
