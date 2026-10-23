"""One transactional authority for API workers, agent leases and validated translations."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from .models import Book, Element, RULES_VERSION, SCHEMA_VERSION, digest, validate_translation, TOKEN_RE, protected_asset_descriptor

from .numeric_policy import numeric_policy_fields, frozen_policy_matches

RULE_HASH_SCHEMA='protected-assets-v2'


def translation_rule_hash(book,element,glossary,legacy=False,schema=None):
    relevant={k:v for k,v in glossary.items() if k.lower() in element.text.lower()}
    value={'rules':RULES_VERSION,'glossary':relevant}
    if not legacy:
        ids=set(element.asset_ids)|set(TOKEN_RE.findall(element.text))
        value['protected_assets']={aid:(protected_asset_descriptor(book.assets[aid],
            include_render_metadata=schema!='protected-assets-v1') if aid in book.assets
            else {'missing':True}) for aid in sorted(ids)}
    value.update(numeric_policy_fields(element))
    return digest(value)



class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS elements(id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,rule_hash TEXT NOT NULL,json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,payload TEXT NOT NULL,element_ids TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',owner TEXT,lease_until REAL DEFAULT 0,attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0,error TEXT,updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS translations(element_id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,rule_hash TEXT NOT NULL,text TEXT NOT NULL,status TEXT NOT NULL,provider TEXT NOT NULL,updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS candidates(id INTEGER PRIMARY KEY,element_id TEXT NOT NULL,text TEXT NOT NULL,provenance TEXT NOT NULL,issues TEXT NOT NULL,created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS issues(id INTEGER PRIMARY KEY,fingerprint TEXT UNIQUE,json TEXT NOT NULL,resolved INTEGER DEFAULT 0,resolution_reason TEXT);
        CREATE TABLE IF NOT EXISTS usage(id INTEGER PRIMARY KEY,task_id TEXT,provider TEXT,json TEXT,created_at REAL);
        """)
        if 'resolution_reason' not in {row[1] for row in self.db.execute('PRAGMA table_info(issues)')}:
            self.db.execute('ALTER TABLE issues ADD COLUMN resolution_reason TEXT')
        candidate_columns={row[1] for row in self.db.execute('PRAGMA table_info(candidates)')}
        for name in ['source_hash','rule_hash']:
            if name not in candidate_columns:self.db.execute('ALTER TABLE candidates ADD COLUMN '+name+' TEXT')
        self.db.commit()
        self.book = None
        self._elements = {}
        self.glossary = {}
        row = self.db.execute("SELECT value FROM metadata WHERE key='book'").fetchone()
        if row:
            b = json.loads(row[0])
            from .models import Asset
            b['elements'] = [Element(**e) for e in b['elements']]
            b['assets'] = {k:Asset(**v) for k,v in b['assets'].items()}
            self.book = Book(**b)
            self._elements = self.book.element_map()
        row = self.db.execute("SELECT value FROM metadata WHERE key='glossary'").fetchone()
        if row:
            self.glossary = json.loads(row[0])
        schema=self.db.execute("SELECT value FROM metadata WHERE key='rule_hash_schema'").fetchone()
        if self.book and (not schema or json.loads(schema[0])!=RULE_HASH_SCHEMA):
            # The persisted previous Book is the authority for old asset bytes;
            # never rebind an old translation to a newly analyzed asset here.
            with self.db:
                for element in self.book.elements:
                    previous_schema=json.loads(schema[0]) if schema else None
                    old=translation_rule_hash(self.book,element,self.glossary,legacy=previous_schema not in {'protected-assets-v1',RULE_HASH_SCHEMA},schema=previous_schema)
                    new=translation_rule_hash(self.book,element,self.glossary)
                    self.db.execute('UPDATE translations SET rule_hash=? WHERE element_id=? AND source_hash=? AND rule_hash=?',(new,element.id,element.source_hash,old))
                    self.db.execute('UPDATE elements SET rule_hash=? WHERE id=? AND source_hash=? AND rule_hash=?',(new,element.id,element.source_hash,old))
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES('rule_hash_schema',?)",(json.dumps(RULE_HASH_SCHEMA),))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        with self.lock:
            self.db.close()

    def _rule_hash(self, element):
        return translation_rule_hash(self.book,element,self.glossary)

    def register(self, book, glossary):
        with self.lock, self.db:
            if self.book and self.book.source_sha256 != book.source_sha256:
                raise ValueError("此任务目录属于另一份原书，请使用新的输出目录")
            if self.book is not None:
                # Convert exact old review proofs against the persisted previous
                # source before replacing formula metadata or glossary rules.
                from .translation import migrate_review_bindings
                migrate_review_bindings(self.book,self)
            self.book, self.glossary = book, dict(glossary)
            self._elements = book.element_map()
            for k,v in {'book':asdict(book),'glossary':self.glossary,'rules_version':RULES_VERSION,'rule_hash_schema':RULE_HASH_SCHEMA,
                        'book_registration_revision':uuid.uuid4().hex}.items():
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)",(k,json.dumps(v,ensure_ascii=False)))
            ids = []
            for e in book.elements:
                ids.append(e.id)
                self.db.execute("INSERT OR REPLACE INTO elements VALUES(?,?,?,?)",(e.id,e.source_hash,self._rule_hash(e),json.dumps(asdict(e),ensure_ascii=False)))
            # This table is the current source index, not historical storage.
            # Preserve translation/candidate history while removing inactive IDs.
            self.db.execute("CREATE TEMP TABLE IF NOT EXISTS current_element_ids(id TEXT PRIMARY KEY)")
            self.db.execute("DELETE FROM current_element_ids")
            self.db.executemany("INSERT INTO current_element_ids VALUES(?)",[(eid,) for eid in ids])
            self.db.execute("DELETE FROM elements WHERE id NOT IN (SELECT id FROM current_element_ids)")
            # Old accepted rows remain recoverable, but only current hashes are exposed.
            self.db.execute("UPDATE tasks SET state='pending',owner=NULL,lease_until=0 WHERE state='done' AND EXISTS (SELECT 1 FROM json_each(tasks.element_ids) j LEFT JOIN translations t ON t.element_id=j.value LEFT JOIN elements e ON e.id=j.value WHERE t.element_id IS NULL OR t.source_hash != e.source_hash OR t.rule_hash != e.rule_hash OR t.status != 'accepted')")
        return self.status()

    def add_tasks(self, tasks):
        with self.lock, self.db:
            self.db.execute("CREATE TEMP TABLE IF NOT EXISTS active_task_ids(id TEXT PRIMARY KEY)")
            self.db.execute("DELETE FROM active_task_ids")
            self.db.executemany("INSERT INTO active_task_ids VALUES(?)",[(t['id'],) for t in tasks])
            self.db.execute("UPDATE tasks SET state='superseded',owner=NULL,lease_until=0 WHERE id NOT IN (SELECT id FROM active_task_ids)")
            self.db.execute("UPDATE issues SET resolved=1,resolution_reason='task_version_superseded' WHERE resolved=0 AND json_extract(json,'$.task_id') IN (SELECT id FROM tasks WHERE state='superseded') AND json_extract(json,'$.code') NOT IN ('semantic_review_passed','semantic_review_failed','semantic_review_pending','glossary_review','glossary_review_required')")
            for task in tasks:
                old = self.db.execute("SELECT source_hash FROM tasks WHERE id=?",(task['id'],)).fetchone()
                if old and old[0] != task['source_hash']:
                    raise ValueError("任务编号冲突：内容版本不同")
                self.db.execute("INSERT OR IGNORE INTO tasks(id,source_hash,payload,element_ids,updated_at) VALUES(?,?,?,?,?)",(task['id'],task['source_hash'],json.dumps(task['payload'],ensure_ascii=False),json.dumps(task['element_ids']),time.time()))
            self.db.execute("UPDATE tasks SET state='pending',owner=NULL,lease_until=0 WHERE state='superseded' AND id IN (SELECT id FROM active_task_ids)")
            self._finish_covered_tasks()

    def _finish_covered_tasks(self):
        self.db.execute("UPDATE tasks SET state='done',owner=NULL,lease_until=0 WHERE state NOT IN ('leased','superseded') AND NOT EXISTS (SELECT 1 FROM json_each(tasks.element_ids) j LEFT JOIN translations t ON t.element_id=j.value LEFT JOIN elements e ON e.id=j.value WHERE t.element_id IS NULL OR e.id IS NULL OR t.status != 'accepted' OR t.source_hash != e.source_hash OR t.rule_hash != e.rule_hash)")

    def _task(self, row, accepted=None):
        if not row:
            return None
        task = dict(row)
        task['payload'] = json.loads(task['payload'])
        task['element_ids'] = json.loads(task['element_ids'])
        if accepted is None:accepted = self.translations()
        task['missing_element_ids'] = [i for i in task['element_ids'] if i not in accepted]
        return task

    def claim(self, owner, lease_seconds=600, task_id=None):
        if not owner or lease_seconds <= 0:
            raise ValueError("owner 与有效租期不可为空")
        now = time.time()
        with self.lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("UPDATE tasks SET state='pending',owner=NULL WHERE state='leased' AND lease_until<=?",(now,))
                self._finish_covered_tasks()
                sql = "SELECT * FROM tasks WHERE state IN ('pending','partial') AND next_attempt<=?"
                params = [now]
                if task_id is not None:
                    sql += " AND id=?"; params.append(str(task_id))
                row = self.db.execute(sql + " ORDER BY updated_at,id LIMIT 1",params).fetchone()
                if row:
                    self.db.execute("UPDATE tasks SET state='leased',owner=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE id=?",(owner,now+lease_seconds,now,row['id']))
                    row = self.db.execute("SELECT * FROM tasks WHERE id=?",(row['id'],)).fetchone()
                self.db.commit()
                return self._task(row)
            except Exception:
                self.db.rollback(); raise

    def renew(self, task_id, owner, lease_seconds=600):
        with self.lock,self.db:
            cur = self.db.execute("UPDATE tasks SET lease_until=? WHERE id=? AND owner=? AND state='leased' AND lease_until>?",(time.time()+lease_seconds,task_id,owner,time.time()))
            if cur.rowcount != 1:
                raise ValueError("租约已过期或属于另一个执行者")

    def _check_lease(self, task_id, owner):
        row = self.db.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
        if not row or row['state'] != 'leased' or row['owner'] != owner or row['lease_until'] <= time.time():
            raise ValueError("任务未领取、租约失效或执行者不匹配")
        return row

    def _accept(self, element_id, text, provider, preissues=None):
        e = self._elements.get(element_id)
        if not e:
            raise ValueError("未知的原文元素编号")
        issues = list(preissues or []) + validate_translation(e,text,self.book)
        for english,chinese in self.glossary.items():
            import re
            if re.search(r'(?<![A-Za-z])'+re.escape(english)+r'(?![A-Za-z])', e.text, re.I):
                alternatives = re.split(r'[/／;；()]',chinese)
                if not any(v.strip() and v.strip() in text for v in alternatives):
                    issues.append({'code':'glossary_review','severity':'warning','element_id':e.id,'page':e.page,'detail':f'检查术语 {english} → {chinese}'})
        self.db.execute("INSERT INTO candidates(element_id,text,provenance,issues,created_at,source_hash,rule_hash) VALUES(?,?,?,?,?,?,?)",(element_id,text,provider,json.dumps(issues,ensure_ascii=False),time.time(),e.source_hash,self._rule_hash(e)))
        errors = [i for i in issues if i.get('severity','error')=='error']
        if not errors:
            self.db.execute("INSERT OR REPLACE INTO translations VALUES(?,?,?,?,?,?,?)",(e.id,e.source_hash,self._rule_hash(e),text,'accepted',provider,time.time()))
            # Resolve stale translation diagnostics, retain source/layout/semantic issues.
            for r in self.db.execute("SELECT id,json FROM issues WHERE resolved=0 AND json_extract(json,'$.element_id')=?",(e.id,)).fetchall():
                i = json.loads(r['json'])
                if i.get('element_id')==e.id and i.get('code') in {'empty_translation','protected_token_mismatch','number_mismatch','invalid_character','untranslated','malformed_token'}:
                    self.db.execute("UPDATE issues SET resolved=1 WHERE id=?",(r['id'],))
        for i in issues:
            self.record_issue(i)
        return {'accepted':not errors,'issues':issues}

    @contextmanager
    def _atomic_write(self):
        """Own a transaction, or only a savepoint inside the caller's one."""
        with self.lock:
            nested=self.db.in_transaction
            savepoint='store_'+uuid.uuid4().hex
            self.db.execute('SAVEPOINT '+savepoint if nested else 'BEGIN IMMEDIATE')
            try:
                yield
                if nested:self.db.execute('RELEASE '+savepoint)
                else:self.db.commit()
            except BaseException:
                if nested:
                    self.db.execute('ROLLBACK TO '+savepoint);self.db.execute('RELEASE '+savepoint)
                else:self.db.rollback()
                raise

    def put_candidate(self, element_id,text,provenance,issues=None):
        if not isinstance(text,str):
            raise ValueError("译文必须是字符串")
        with self._atomic_write():
            result=self._accept(element_id,text,provenance,issues)
            return result

    def _validate_task_source(self,row,source_hash):
        """Same frozen source/asset checks for submissions and candidate reviews."""
        if row['source_hash'] != source_hash:
            raise ValueError("任务原文或规则版本不匹配，拒绝旧结果")
        payload=json.loads(row['payload'])
        if 'glossary_hash' in payload and payload['glossary_hash'] != digest(self.glossary):
            raise ValueError("任务术语表已过期，拒绝旧结果")
        current_glossary=self.db.execute("SELECT value FROM metadata WHERE key='glossary'").fetchone()
        if current_glossary and digest(json.loads(current_glossary[0]))!=digest(self.glossary):
            raise ValueError('数据库术语版本已由其他执行者更新，拒绝旧会话结果')
        expected=set(json.loads(row['element_ids']))
        frozen=payload.get('elements',[])
        if set(e.get('id') for e in frozen)!=expected or any(e.get('id') not in self._elements or e.get('source_hash')!=self._elements[e['id']].source_hash for e in frozen):
            raise ValueError("任务对应的原文元素已改变，拒绝旧结果")
        context=[entry for key in ('context_before','context_after') for entry in payload.get(key,[])]
        if any(entry.get('id') not in self._elements or entry.get('source_hash')!=self._elements[entry['id']].source_hash for entry in context):
            raise ValueError('任务只读上下文已改变，拒绝旧结果')
        if any(not frozen_policy_matches(entry,self._elements[entry['id']]) for entry in frozen+context):
            raise ValueError('任务或只读上下文的数量级规则已改变，拒绝旧结果')
        related=expected|{entry['id'] for entry in context}
        for eid in related:
            current=self.db.execute('SELECT source_hash,rule_hash FROM elements WHERE id=?',(eid,)).fetchone()
            local=self._elements.get(eid)
            if not current or local is None or current['source_hash']!=local.source_hash or current['rule_hash']!=self._rule_hash(local):
                raise ValueError('数据库原文或公式版本已由其他执行者更新，拒绝旧会话结果')
        descriptors=payload.get('protected_asset_descriptors')
        if payload.get('protected_assets_schema')!=RULE_HASH_SCHEMA or not isinstance(descriptors,dict):
            raise ValueError('任务缺少完整公式来源版本，请重新准备任务；拒绝旧租约结果')
        required={aid for eid in related for aid in set(self._elements[eid].asset_ids)|set(TOKEN_RE.findall(self._elements[eid].text))}
        if not required<=descriptors.keys() or any(aid not in self.book.assets or value!=protected_asset_descriptor(self.book.assets[aid]) for aid,value in descriptors.items()):
            raise ValueError('任务公式或上下文保护对象已改变，拒绝旧结果')
        return expected

    def submit(self, task_id,owner,source_hash,translations,provider='agent',usage=None):
        with self.lock,self.db:
            # Reserve the database before reading its current source version;
            # a separate API/agent connection may have replaced it meanwhile.
            if not self.db.in_transaction:self.db.execute('BEGIN IMMEDIATE')
            row = self._check_lease(task_id,owner)
            expected=self._validate_task_source(row,source_hash)
            if not isinstance(translations,dict) or set(translations)-expected:
                raise ValueError("提交包含任务外的元素编号")
            if not all(isinstance(v,str) for v in translations.values()):
                raise ValueError("所有译文必须为字符串")
            accepted=[];issues=[]
            for eid,text in translations.items():
                result=self._accept(eid,text,provider)
                issues.extend(result['issues'])
                if result['accepted']:accepted.append(eid)
            valid=self.translations()
            missing=sorted(expected-set(valid))
            self.db.execute("UPDATE tasks SET state=?,owner=NULL,lease_until=0,updated_at=? WHERE id=?",('partial' if missing else 'done',time.time(),task_id))
            if usage:
                self.db.execute("INSERT INTO usage(task_id,provider,json,created_at) VALUES(?,?,?,?)",(task_id,provider,json.dumps(usage,ensure_ascii=False),time.time()))
            return {'accepted':accepted,'issues':issues,'complete':not missing,'missing':missing}

    def fail(self,task_id,owner,error,retryable=True):
        with self.lock,self.db:
            row=self._check_lease(task_id,owner)
            delay=min(60,2**min(row['attempts'],5)) if retryable else 0
            self.db.execute("UPDATE tasks SET state=?,owner=NULL,lease_until=0,error=?,next_attempt=?,updated_at=? WHERE id=?",('pending' if retryable else 'failed',str(error)[:1000],time.time()+delay,time.time(),task_id))

    def retry_failed(self):
        with self.lock,self.db:
            return self.db.execute("UPDATE tasks SET state='pending',next_attempt=0 WHERE state='failed'").rowcount

    def redo_element(self, element_id, reason='手动要求重译此段'):
        """Invalidate one revision and its live leases, preserving all history.

        Unchanged neighbours remain accepted. Revoking the lease also prevents
        a response already in flight from silently undoing the user's redo.
        """
        if element_id not in self._elements:
            raise ValueError('未知的原文元素编号')
        from .models import needs_translation
        if not needs_translation(self._elements[element_id]):
            raise ValueError('此元素原样保留，不属于翻译任务')
        with self.lock, self.db:
            row=self.db.execute('SELECT text FROM translations WHERE element_id=?',(element_id,)).fetchone()
            previous_hash=digest(row[0]) if row else None
            self.db.execute("UPDATE translations SET status='redo_requested' WHERE element_id=?",(element_id,))
            count=self.db.execute("UPDATE tasks SET state='pending',owner=NULL,lease_until=0,next_attempt=0 WHERE state!='superseded' AND EXISTS (SELECT 1 FROM json_each(tasks.element_ids) WHERE value=?)",(element_id,)).rowcount
            self.record_issue({'code':'translation_redo_requested','severity':'info','element_id':element_id,'page':self._elements[element_id].page,'detail':str(reason),'previous_translation_hash':previous_hash})
            return {'element_id':element_id,'requeued_tasks':count,'previous_translation_preserved':bool(row)}

    def translations(self,accepted_only=True):
        with self.lock:
            sql="SELECT t.element_id,t.text FROM translations t JOIN elements e ON e.id=t.element_id WHERE t.source_hash=e.source_hash AND t.rule_hash=e.rule_hash"
            if accepted_only:sql+=" AND t.status='accepted'"
            active=self._elements if self.book else None
            return {r[0]:r[1] for r in self.db.execute(sql) if active is None or r[0] in active}

    def pending_tasks(self):
        with self.lock:
            accepted=self.translations()
            return [self._task(r,accepted) for r in self.db.execute("SELECT * FROM tasks WHERE state NOT IN ('done','superseded') ORDER BY updated_at,id").fetchall()]

    def record_usage(self,task_id,provider,usage):
        with self.lock,self.db:
            self.db.execute("INSERT INTO usage(task_id,provider,json,created_at) VALUES(?,?,?,?)",(task_id,provider,json.dumps(usage,ensure_ascii=False),time.time()))

    def _current_review_book(self,element):
        """Read current protected objects from SQLite, never a stale Store Book.

        A register transaction replaces its opaque revision every time. A small
        cache of independently read objects is safe only under that revision;
        old databases without one use actual JSON extraction on every commit.
        SQLite returns only needed fields, avoiding a Python load of a full book.
        Caller must already hold the review transaction and this Store's lock.
        """
        row=self.db.execute("SELECT value FROM metadata WHERE key='book_registration_revision'").fetchone()
        revision=json.loads(row[0]) if row else None
        if revision is None or getattr(self,'_review_cache_revision',None)!=revision:
            self._review_asset_cache=OrderedDict()
            self._review_cached_source=None
            self._review_cache_revision=revision
        ids=sorted(set(element.asset_ids)|set(TOKEN_RE.findall(element.text)))
        cached=self._review_asset_cache
        assets={aid:cached[aid] for aid in ids if aid in cached}
        missing=[aid for aid in ids if aid not in assets]
        chunks=[missing[n:n+32] for n in range(0,len(missing),32)]
        if not chunks and self._review_cached_source is None:chunks=[[]]
        from .models import Asset
        for chunk in chunks:
            paths=['$.source_sha256','$.schema_version']+['$.assets.'+json.dumps(aid) for aid in chunk]
            query="SELECT json_extract(value,"+','.join('?' for _ in paths)+") FROM metadata WHERE key='book'"
            current=self.db.execute(query,paths).fetchone()
            if not current or current[0] is None:raise ValueError('Current source book is unavailable')
            values=json.loads(current[0])
            identity=(values[0],values[1])
            if self._review_cached_source is not None and identity!=self._review_cached_source:
                raise ValueError('Source identity changed within its registration revision')
            self._review_cached_source=identity
            for aid,value in zip(chunk,values[2:]):
                if not isinstance(value,dict):raise ValueError('Current protected object is unavailable')
                assets[aid]=Asset(**value)
                cached[aid]=assets[aid];cached.move_to_end(aid)
                while len(cached)>64:cached.popitem(last=False)
        source,version=self._review_cached_source
        if version!=SCHEMA_VERSION:raise ValueError('Current source schema is unsupported')
        return Book('',source,'',0,[],elements=[element],assets=assets,schema_version=version)

    def apply_semantic_review(self,element_id,translated,*,source_hash,rule_hash,
                              source_sha256,review_hash,passed,problems,provider,
                              task_id=None,owner=None,task_source_hash=None):
        """Atomically compare the current source/asset/text and apply one verdict.

        Accepted reviews require the still-accepted exact translation. Candidate
        reviews additionally require the same valid API/agent task lease and all
        its frozen context objects; they cannot touch a concurrently accepted
        revision. Passing, failing and pending diagnostics share this transaction.
        Stale work only returns a result: it never clears or revokes new state.
        """
        if (passed is not None and type(passed) is not bool) or not isinstance(translated,str):
            raise ValueError('Invalid semantic review value')
        if not isinstance(problems,list) or any(not isinstance(p,str) for p in problems):
            raise ValueError('Invalid semantic review problems')
        if passed is True and problems:raise ValueError('A passed review cannot contain problems')
        from .translation import _review_hash,REVIEW_HASH_SCHEMA
        candidate=task_id is not None
        def apply():
            stale=lambda reason:{'applied':False,'stale':True,'reason':reason}
            element_row=self.db.execute('SELECT * FROM elements WHERE id=?',(element_id,)).fetchone()
            if (not element_row or element_row['source_hash']!=source_hash
                or element_row['rule_hash']!=rule_hash):return stale('source_or_asset_revision_changed')
            element=Element(**json.loads(element_row['json']))
            if element.source_hash!=source_hash:return stale('source_record_inconsistent')
            row=self.db.execute('SELECT * FROM translations WHERE element_id=?',(element_id,)).fetchone()
            if candidate:
                try:
                    task=self._check_lease(task_id,owner)
                    expected=self._validate_task_source(task,task_source_hash)
                except ValueError:return stale('task_lease_or_frozen_source_changed')
                if element_id not in expected:return stale('element_outside_task')
                if (row and row['status']=='accepted' and row['source_hash']==source_hash
                    and row['rule_hash']==rule_hash):return stale('candidate_already_replaced_by_accepted_revision')
            elif (not row or row['status']!='accepted' or row['source_hash']!=source_hash
                  or row['rule_hash']!=rule_hash or digest(row['text'])!=digest(translated)):
                return stale('translation_revision_changed')
            glossary_row=self.db.execute("SELECT value FROM metadata WHERE key='glossary'").fetchone()
            if not glossary_row:return stale('current_glossary_unavailable')
            glossary=json.loads(glossary_row[0])
            try:
                current=self._current_review_book(element)
                if current.source_sha256!=source_sha256:return stale('book_source_identity_changed')
                if (translation_rule_hash(current,element,glossary)!=rule_hash
                    or _review_hash(element,translated,current,glossary)!=review_hash):
                    return stale('current_review_binding_changed')
            except (ValueError,KeyError,TypeError):return stale('current_review_binding_unavailable')
            entry={'element_id':element_id,'page':element.page,'review_provider':provider,
                   'source_hash':source_hash,'translation_hash':digest(translated),
                   'review_hash':review_hash,'review_hash_schema':REVIEW_HASH_SCHEMA}
            if task_id is not None:entry['task_id']=task_id
            if passed is True:
                codes={'semantic_review_pending','semantic_review_failed','glossary_review','glossary_review_required'}
                for old in self.db.execute("SELECT id,json FROM issues WHERE resolved=0 AND json_extract(json,'$.element_id')=?",(element_id,)).fetchall():
                    if json.loads(old['json']).get('code') in codes:
                        self.db.execute("UPDATE issues SET resolved=1,resolution_reason='current_review_passed' WHERE id=?",(old['id'],))
                entry.update(code='semantic_review_passed',severity='info',
                             detail='独立语义复核已通过；结论绑定当前原文、公式、译文和冻结规则')
            elif passed is False:
                entry.update(code='semantic_review_failed',severity='error',
                             detail='；'.join(problems) or '独立审校未通过')
                issues=[entry]+validate_translation(element,translated,current)
                self.db.execute('INSERT INTO candidates(element_id,text,provenance,issues,created_at,source_hash,rule_hash) VALUES(?,?,?,?,?,?,?)',
                    (element_id,translated,'semantic-review',json.dumps(issues,ensure_ascii=False),time.time(),source_hash,rule_hash))
                if not candidate:
                    self.db.execute("UPDATE translations SET status='review_failed' WHERE element_id=? AND source_hash=? AND rule_hash=? AND text=?",
                                    (element_id,source_hash,rule_hash,translated))
                    self.db.execute("UPDATE tasks SET state='pending',owner=NULL,lease_until=0 WHERE state!='superseded' AND EXISTS (SELECT 1 FROM json_each(tasks.element_ids) WHERE value=?)",(element_id,))
            else:
                entry.update(code='semantic_review_pending',severity='warning',
                             detail='；'.join(problems) or '独立语义复核尚未成功完成')
            self.record_issue(entry)
            return {'applied':True,'stale':False,'code':entry['code']}
        with self._atomic_write():
            return apply()

    def reject_translation(self,element_id,translation_hash):
        with self.lock,self.db:
            row=self.db.execute("SELECT text FROM translations WHERE element_id=?",(element_id,)).fetchone()
            if not row or digest(row[0])!=translation_hash:return False
            self.db.execute("UPDATE translations SET status='review_failed' WHERE element_id=?",(element_id,))
            self.db.execute("UPDATE tasks SET state='pending',owner=NULL,lease_until=0 WHERE state!='superseded' AND EXISTS (SELECT 1 FROM json_each(tasks.element_ids) WHERE value=?)",(element_id,))
            return True

    def resolve_issues(self,element_id,codes):
        with self.lock,self.db:
            for row in self.db.execute("SELECT id,json FROM issues WHERE resolved=0 AND json_extract(json,'$.element_id')=?",(element_id,)).fetchall():
                if json.loads(row['json']).get('code') in codes:
                    self.db.execute("UPDATE issues SET resolved=1 WHERE id=?",(row['id'],))

    def reconcile_diagnostics(self):
        """Close migration warnings only after their current content is covered.

        The read-only migration report and resolved issue rows remain an audit
        trail. A page-level warning cannot be closed by translating one block.
        """
        from .models import needs_translation
        accepted=self.translations()
        bypage={}
        for e in self.book.elements:
            if needs_translation(e):bypage.setdefault(e.page,set()).add(e.id)
        unsafe={i.get('page') for i in self.book.issues if i.get('severity')=='error'}
        complete={p for p,ids in bypage.items() if ids<=accepted.keys() and p not in unsafe}
        for page in self.book.pages:
            coverage=page.get('coverage',{})
            if coverage.get('unresolved',coverage.get('unaccounted',0)):
                complete.discard(page.get('number',page.get('page')))
        element_codes={'legacy_translation_invalid','legacy_formula_ambiguous','legacy_store_rejected','legacy_conflicting_candidates','cache_recovery_conflict'}
        page_codes={'legacy_no_exact_source','legacy_geometry_ambiguous','legacy_missing_translation','legacy_prompt_hash_mismatch'}
        from .translation import review_record_matches
        passed,failed=set(),set()
        for entry in self.issues():
            eid=entry.get('element_id')
            if entry.get('code') in {'semantic_review_passed','semantic_review_failed'} and eid in accepted and eid in self._elements:
                if review_record_matches(entry,self._elements[eid],accepted[eid],self.book,self.glossary):
                    (passed if entry['code']=='semantic_review_passed' else failed).add(eid)
        passed-=failed
        count=0
        with self.lock,self.db:
            done_tasks={row[0] for row in self.db.execute("SELECT id FROM tasks WHERE state='done'")}
            for row in self.db.execute('SELECT id,json FROM issues WHERE resolved=0').fetchall():
                issue=json.loads(row['json']);code=issue.get('code')
                eid=issue.get('element_id');reason=None
                if eid and eid not in self._elements:reason='source_element_superseded'
                elif eid in self._elements and not needs_translation(self._elements[eid]) and code in (element_codes|{'semantic_review_pending','semantic_review_failed','glossary_review','glossary_review_required','number_mismatch','protected_token_mismatch','untranslated','empty_translation','malformed_token'}):reason='element_now_preserved_literal'
                elif code in {'semantic_review_pending','semantic_review_failed','glossary_review','glossary_review_required'}:
                    if eid in passed:reason='current_review_passed'
                    elif eid in accepted and issue.get('translation_hash') and issue['translation_hash']!=digest(accepted[eid]):reason='translation_revision_superseded'
                if reason is None and code=='translation_no_progress' and issue.get('task_id') in done_tasks:reason='current_task_completed'
                if reason is None and code in element_codes and eid in accepted:reason='current_translation_accepted'
                if reason is None and code in page_codes and issue.get('page') in complete:reason='current_page_covered'
                if reason:
                    self.db.execute('UPDATE issues SET resolved=1,resolution_reason=? WHERE id=?',(reason,row['id']));count+=1
        return count

    def record_issue(self,issue):
        with self.lock:
            was_in_transaction = self.db.in_transaction
            obj=dict(issue)
            obj.setdefault('severity','warning')
            self.db.execute("INSERT INTO issues(fingerprint,json) VALUES(?,?) ON CONFLICT(fingerprint) DO UPDATE SET resolved=0,resolution_reason=NULL",(digest(obj),json.dumps(obj,ensure_ascii=False)))
            # Caller may own an enclosing transaction.
            if not was_in_transaction:self.db.commit()

    def resolve_issue(self,issue,reason):
        """Close one exact diagnostic while retaining its original JSON/history."""
        obj=dict(issue);obj.setdefault('severity','warning')
        with self.lock,self.db:
            result=self.db.execute("UPDATE issues SET resolved=1,resolution_reason=? WHERE fingerprint=? AND resolved=0",(str(reason),digest(obj)))
        return result.rowcount

    def issues(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT json FROM issues WHERE resolved=0 ORDER BY id")]

    def resolved_issues(self):
        with self.lock:
            return [{**json.loads(r[0]),"resolution_reason":r[1]} for r in self.db.execute("SELECT json,resolution_reason FROM issues WHERE resolved=1 ORDER BY id")]

    def status(self):
        with self.lock:
            states={r[0]:r[1] for r in self.db.execute("SELECT state,count(*) FROM tasks GROUP BY state")}
            usage=[json.loads(r[0]) for r in self.db.execute("SELECT json FROM usage")]
            summary={'requests':len(usage),'prompt_tokens':sum(u.get('prompt_tokens',0) or 0 for u in usage),'completion_tokens':sum(u.get('completion_tokens',0) or 0 for u in usage),'estimated_cost_cny':round(sum(u.get('estimated_cost',0) or 0 for u in usage if u.get('currency','CNY')=='CNY'),6)}
            from .models import needs_translation
            required={e.id for e in self.book.elements if needs_translation(e)} if self.book else set()
            accepted=self.translations()
            source_issues=self.book.issues if self.book else []
            return {'tasks':states,'accepted_elements':len(accepted),'elements':len(self.book.elements) if self.book else 0,
                    'required_translations':len(required),'accepted_translatable':len(required & accepted.keys()),
                    'source_errors':sum(i.get('severity')=='error' for i in source_issues),
                    'source_warnings':sum(i.get('severity','warning')=='warning' for i in source_issues),
                    'issues':len(self.issues()),'usage':summary}
