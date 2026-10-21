import tempfile
import shutil
import uuid
import time
import unittest
from pathlib import Path

from textbook_pdf.models import Asset,Book,Element,digest,validate_translation,protected_asset_descriptor
from textbook_pdf.store import Store

def fixture():
    a=Asset('f1','formula','assets/f1.pdf',1,[0,0,20,10],20,10,text='x²')
    b=Book('source.pdf','source-sha','Test',1,[1],elements=[Element('e1','paragraph','Let ⟦F:f1⟧ be 2.',1,asset_ids=['f1']),Element('e2','paragraph','A set is given.',1)],assets={'f1':a})
    task={'id':'t1','source_hash':'task-sha','element_ids':['e1','e2'],'payload':{'elements':[{'id':e.id,'source_hash':e.source_hash} for e in b.elements],'protected_assets_schema':'protected-assets-v2','protected_asset_descriptors':{aid:protected_asset_descriptor(asset) for aid,asset in b.assets.items()}}}
    return b,task

class ContractTests(unittest.TestCase):
    def setUp(self):
        test_temp=Path(__file__).resolve().parent/'_tmp'
        test_temp.mkdir(exist_ok=True)
        self.tmp_path=test_temp/uuid.uuid4().hex
        self.tmp_path.mkdir()
        self.path=self.tmp_path/'jobs.db'
        self.book,self.task=fixture()
        self.store=Store(self.path);self.store.register(self.book,{});self.store.add_tasks([self.task])
    def tearDown(self):
        self.store.close()
        assert self.tmp_path.resolve().is_relative_to(Path(__file__).resolve().parent)
        shutil.rmtree(self.tmp_path)
    def test_reanalyze_other_book_rejects_before_overwriting_source_assets(self):
        from unittest.mock import patch
        from textbook_pdf.models import file_sha256
        from textbook_pdf.cli import main
        first=self.tmp_path/'first.pdf';first.write_bytes(b'first original bytes')
        other=self.tmp_path/'other.pdf';other.write_bytes(b'other original bytes')
        self.book.source_path=str(first);self.book.source_sha256=file_sha256(first)
        target=self.tmp_path/'book.json';self.book.save(target);before=target.read_bytes()
        asset=self.tmp_path/'assets'/'formula.pdf';asset.parent.mkdir();asset.write_bytes(b'protected original vector')
        with patch('textbook_pdf.extraction.analyze') as analyze:
            with self.assertRaisesRegex(ValueError,'另一份原书'):
                main([str(other),'--out',str(self.tmp_path),'--stage','analyze'])
            analyze.assert_not_called()
        self.assertEqual(target.read_bytes(),before)
        self.assertEqual(asset.read_bytes(),b'protected original vector')

    def test_reanalyze_other_page_set_rejects_before_parser_writes(self):
        from unittest.mock import patch
        from textbook_pdf.models import file_sha256
        from textbook_pdf.cli import main
        first=self.tmp_path/'first.pdf';first.write_bytes(b'first original bytes')
        self.book.source_path=str(first);self.book.source_sha256=file_sha256(first)
        target=self.tmp_path/'book.json';self.book.save(target);before=target.read_bytes()
        with patch('textbook_pdf.extraction.analyze') as analyze:
            with self.assertRaisesRegex(ValueError,'另一组页码'):
                main([str(first),'--out',str(self.tmp_path),'--stage','analyze','--pages','2'])
            analyze.assert_not_called()
        self.assertEqual(target.read_bytes(),before)

    def test_source_checkpoint_survives_temporary_windows_reader_lock(self):
        from unittest.mock import patch
        target=self.tmp_path/'book.json';target.write_text('old-source-checkpoint',encoding='utf-8')
        replace=Path.replace;calls=[]
        def transient(path,destination):
            calls.append(True)
            if len(calls)<3:
                self.assertEqual(target.read_text(encoding='utf-8'),'old-source-checkpoint')
                raise PermissionError('simulated Windows file-sharing lock')
            return replace(path,destination)
        with patch.object(Path,'replace',transient),patch('textbook_pdf.models.time.sleep'):
            self.book.save(target)
        self.assertEqual(Book.load(target).source_sha256,self.book.source_sha256)
        self.assertEqual(len(calls),3)

    def test_same_length_source_replacement_refreshes_index(self):
        first=self.book.element_map()['e1']
        replacement=Element('e1','paragraph','Different source.',1)
        self.book.elements[0]=replacement
        self.assertIs(self.book.element_map()['e1'],replacement)
        self.assertIsNot(self.book.element_map()['e1'],first)
        self.book.elements=[Element('new','paragraph','New source.',1),self.book.elements[1]]
        self.assertNotIn('e1',self.book.element_map());self.assertIn('new',self.book.element_map())

    def test_asset_metadata_change_rejects_already_leased_result(self):
        from copy import deepcopy
        self.store.claim('old')
        revised=deepcopy(self.book);revised.assets['f1'].metadata['pdf_page']=1
        self.store.register(revised,{})
        with self.assertRaisesRegex(ValueError,'对象已改变'):
            self.store.submit('t1','old','task-sha',{'e1':'设 ⟦F:f1⟧ 为 2。'})
        self.assertEqual(self.store.translations(),{})
        self.assertEqual(self.store.db.execute('select count(*) from candidates').fetchone()[0],0)

    def test_task_protocol_upgrade_keeps_current_paid_semantic_verdict(self):
        from copy import deepcopy
        from textbook_pdf.translation import _review_hash,review_status
        text='设 ⟦F:f1⟧ 为 2。';e=self.book.elements[0]
        self.store.put_candidate('e1',text,'cache')
        proof={'code':'semantic_review_passed','severity':'info','element_id':'e1','task_id':'t1',
            'source_hash':e.source_hash,'translation_hash':digest(text),
            'review_hash':_review_hash(e,text,self.book,{})}
        self.store.record_issue(proof)
        replacement=deepcopy(self.task);replacement['id']='t2';replacement['source_hash']='new-protocol'
        self.store.add_tasks([replacement])
        self.assertIn(proof,self.store.issues())
        self.assertIn('e1',review_status(self.book,self.store)['ids']['passed'])

    def test_separate_worker_cannot_overwrite_new_source_translation(self):
        from copy import deepcopy
        old_worker=Store(self.path)
        try:
            old_worker.claim('inflight')
            revised=deepcopy(self.book);revised.assets['f1'].metadata['pdf_page']=1
            self.store.register(revised,{})
            self.store.put_candidate('e1','设 ⟦F:f1⟧ 为 2。','new-source-agent')
            before=self.store.db.execute("select text,rule_hash,provider from translations where element_id='e1'").fetchone()
            with self.assertRaisesRegex(ValueError,'其他执行者更新'):
                old_worker.submit('t1','inflight','task-sha',{'e1':'设 ⟦F:f1⟧ 为 2。'})
            after=self.store.db.execute("select text,rule_hash,provider from translations where element_id='e1'").fetchone()
            self.assertEqual(tuple(before),tuple(after))
            self.assertIn('e1',self.store.translations())
        finally:old_worker.close()

    def test_frozen_context_change_rejects_result_in_same_store(self):
        from copy import deepcopy
        task=deepcopy(self.task)
        task['element_ids']=['e1'];task['payload']['elements']=task['payload']['elements'][:1]
        task['payload']['context_after']=[{'id':'e2','source_hash':self.book.elements[1].source_hash}]
        with self.store.db:self.store.db.execute('DELETE FROM tasks')
        self.store.add_tasks([task]);self.store.claim('inflight')
        revised=deepcopy(self.book);revised.elements[1].text='A red ball is given.'
        self.store.register(revised,{})
        with self.assertRaisesRegex(ValueError,'只读上下文已改变'):
            self.store.submit('t1','inflight','task-sha',{'e1':'设 ⟦F:f1⟧ 为 2。'})
        self.assertEqual(self.store.db.execute('select count(*) from candidates').fetchone()[0],0)

    def test_removed_source_rejects_another_connection_inflight_result(self):
        from copy import deepcopy
        old_worker=Store(self.path)
        try:
            old_worker.claim('inflight')
            revised=deepcopy(self.book);revised.elements=revised.elements[1:]
            self.store.register(revised,{})
            with self.assertRaisesRegex(ValueError,'其他执行者更新'):
                old_worker.submit('t1','inflight','task-sha',{'e1':'设 ⟦F:f1⟧ 为 2。'})
            self.assertEqual(self.store.db.execute('select count(*) from candidates').fetchone()[0],0)
        finally:old_worker.close()

    def test_context_protected_object_cannot_be_omitted_from_descriptor(self):
        from copy import deepcopy
        task=deepcopy(self.task)
        task['element_ids']=['e2'];task['payload']['elements']=task['payload']['elements'][1:]
        task['payload']['context_before']=[{'id':'e1','source_hash':self.book.elements[0].source_hash}]
        task['payload']['protected_asset_descriptors']={}
        with self.store.db:self.store.db.execute('DELETE FROM tasks')
        self.store.add_tasks([task]);self.store.claim('inflight')
        with self.assertRaisesRegex(ValueError,'对象已改变'):
            self.store.submit('t1','inflight','task-sha',{'e2':'给定一个集合。'})
        self.assertEqual(self.store.translations(),{})

    def test_old_unversioned_asset_task_cannot_submit(self):
        import json
        payload=dict(self.task['payload']);payload.pop('protected_assets_schema')
        with self.store.db:self.store.db.execute('update tasks set payload=? where id=?',(json.dumps(payload),'t1'))
        self.store.claim('old')
        with self.assertRaisesRegex(ValueError,'完整公式来源版本'):
            self.store.submit('t1','old','task-sha',{'e1':'设 ⟦F:f1⟧ 为 2。'})

    def test_atomic_claim_between_connections(self):
        other=Store(self.path)
        try:
            self.assertIsNotNone(self.store.claim('first'))
            self.assertIsNone(other.claim('second'))
        finally:other.close()
    def test_reject_stale_hash_and_foreign_owner(self):
        self.store.claim('first')
        with self.assertRaises(ValueError):self.store.submit('t1','first','wrong',{})
        with self.assertRaises(ValueError):self.store.submit('t1','second','task-sha',{})
    def test_partial_is_not_complete_and_resumes(self):
        self.store.claim('first')
        r=self.store.submit('t1','first','task-sha',{'e1':'设 ⟦F:f1⟧ 为 2。'})
        self.assertFalse(r['complete']);self.assertEqual(r['missing'],['e2'])
        task=self.store.claim('next');self.assertEqual(task['missing_element_ids'],['e2'])
        r=self.store.submit('t1','next','task-sha',{'e2':'给定一个集合。'})
        self.assertTrue(r['complete']);self.assertIsNone(self.store.claim('third'))
    def test_modified_formula_and_number_rejected(self):
        self.assertFalse(self.store.put_candidate('e1','设 x 为 3。','test')['accepted'])
        self.assertEqual(self.store.translations(),{})
    def test_redo_only_one_element_and_rejects_inflight_reply(self):
        self.store.put_candidate('e2','给定一个集合。','test')
        self.store.claim('inflight')
        result=self.store.redo_element('e1')
        self.assertEqual(result['requeued_tasks'],1)
        with self.assertRaises(ValueError):
            self.store.submit('t1','inflight','task-sha',{'e1':'设 ⟦F:f1⟧ 为 2。'})
        self.assertIn('e2',self.store.translations())
        task=self.store.claim('redo')
        self.assertEqual(task['missing_element_ids'],['e1'])
    def test_chinese_adjacent_chapter_numbers_are_preserved(self):
        e=Element('numbers','paragraph','See Chapter 5 and Section 1.8.',1)
        self.assertEqual(validate_translation(e,'参见第5章和1.8节。'),[])
        self.assertTrue(any(i['code']=='number_mismatch' for i in validate_translation(e,'参见第6章和1.8节。')))
    def test_font_change_preserves_translation(self):
        self.store.put_candidate('e2','给定一个集合。','test')
        self.book.profile['body_size']=11
        self.store.register(self.book,{})
        self.assertIn('e2',self.store.translations())
    def test_only_relevant_glossary_invalidates(self):
        self.store.put_candidate('e1','设 ⟦F:f1⟧ 为 2。','test')
        self.store.put_candidate('e2','给定一个集合。','test')
        self.store.register(self.book,{'set':'集'})
        self.assertIn('e1',self.store.translations());self.assertNotIn('e2',self.store.translations())
    def test_changed_protected_asset_invalidates_only_its_translation(self):
        from copy import deepcopy
        self.store.put_candidate('e1','设 ⟦F:f1⟧ 为 2。','test')
        self.store.put_candidate('e2','给定一个集合。','test')
        revised=deepcopy(self.book);revised.assets['f1'].sha256='new-immutable-vector-content'
        self.assertEqual(revised.elements[0].source_hash,self.book.elements[0].source_hash)
        self.store.register(revised,{})
        self.assertNotIn('e1',self.store.translations())
        self.assertIn('e2',self.store.translations())
        self.assertEqual(self.store.db.execute("select count(*) from candidates where element_id='e1'").fetchone()[0],1)

    def test_old_rule_schema_upgrade_preserves_binding_to_previous_asset(self):
        import json
        from copy import deepcopy
        from textbook_pdf.store import translation_rule_hash
        self.store.put_candidate('e1','设 ⟦F:f1⟧ 为 2。','test')
        self.store.put_candidate('e2','给定一个集合。','test')
        with self.store.db:
            for element in self.book.elements:
                old=translation_rule_hash(self.book,element,{},legacy=True)
                self.store.db.execute('UPDATE translations SET rule_hash=? WHERE element_id=?',(old,element.id))
                self.store.db.execute('UPDATE elements SET rule_hash=? WHERE id=?',(old,element.id))
            self.store.db.execute("DELETE FROM metadata WHERE key='rule_hash_schema'")
        self.store.close();self.store=Store(self.path)
        self.assertEqual(set(self.store.translations()),{'e1','e2'})
        revised=deepcopy(self.book);revised.assets['f1'].sha256='changed-after-old-snapshot'
        self.store.register(revised,{})
        self.assertEqual(set(self.store.translations()),{'e2'})

    def test_previous_asset_schema_preserves_then_invalidates_bundle_page_change(self):
        import json
        from copy import deepcopy
        from textbook_pdf.store import translation_rule_hash
        self.book.assets['f1'].metadata['pdf_page']=0
        self.store.register(self.book,{})
        self.store.put_candidate('e1','设 ⟦F:f1⟧ 为 2。','test')
        self.store.put_candidate('e2','给定一个集合。','test')
        with self.store.db:
            for e in self.book.elements:
                old=translation_rule_hash(self.book,e,{},schema='protected-assets-v1')
                self.store.db.execute('UPDATE translations SET rule_hash=? WHERE element_id=?',(old,e.id))
                self.store.db.execute('UPDATE elements SET rule_hash=? WHERE id=?',(old,e.id))
            self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES('rule_hash_schema',?)",(json.dumps('protected-assets-v1'),))
        self.store.close();self.store=Store(self.path)
        self.assertEqual(set(self.store.translations()),{'e1','e2'})
        revised=deepcopy(self.book);revised.assets['f1'].metadata['pdf_page']=1
        self.store.register(revised,{})
        self.assertEqual(set(self.store.translations()),{'e2'})

    def test_register_migrates_old_review_before_protected_metadata_changes(self):
        from copy import deepcopy
        from textbook_pdf.translation import legacy_review_hash,review_record_matches
        self.book.assets['f1'].sha256='same-vector-bundle'
        self.book.assets['f1'].metadata['pdf_page']=0
        self.store.register(self.book,{})
        text='设 ⟦F:f1⟧ 为 2。';e=self.book.elements[0]
        self.store.put_candidate('e1',text,'test')
        original={'code':'semantic_review_passed','severity':'info','element_id':'e1','source_hash':e.source_hash,
                  'translation_hash':digest(text),'review_hash':legacy_review_hash(e,text,self.book,{})}
        self.store.record_issue(original)
        revised=deepcopy(self.book);revised.assets['f1'].metadata['pdf_page']=1
        self.store.register(revised,{})
        active=[i for i in self.store.issues() if i.get('code')=='semantic_review_passed']
        self.assertFalse(any(review_record_matches(i,revised.elements[0],text,revised,{}) for i in active))
        self.assertTrue(any(i.get('review_hash')==original['review_hash'] for i in self.store.resolved_issues()))

    def test_candidate_history_keeps_original_source_and_protected_rule_version(self):
        from copy import deepcopy
        self.store.put_candidate('e1','设 ⟦F:f1⟧ 为 2。','first-version')
        first=self.store.db.execute("select source_hash,rule_hash from candidates where element_id='e1' order by id").fetchone()
        revised=deepcopy(self.book);revised.elements[0].text='Let ⟦F:f1⟧ be 3.';revised.assets['f1'].sha256='new-vector-content'
        self.store.register(revised,{})
        self.store.put_candidate('e1','设 ⟦F:f1⟧ 为 3。','second-version')
        rows=self.store.db.execute("select source_hash,rule_hash from candidates where element_id='e1' order by id").fetchall()
        self.assertEqual(tuple(rows[0]),tuple(first))
        self.assertEqual(rows[0][0],self.book.elements[0].source_hash)
        self.assertEqual(rows[1][0],revised.elements[0].source_hash)
        self.assertNotEqual(rows[0][1],rows[1][1])

    def test_expired_lease_cannot_overwrite(self):
        self.store.claim('old',lease_seconds=.01);time.sleep(.02)
        self.assertIsNotNone(self.store.claim('new'))
        with self.assertRaises(ValueError):self.store.submit('t1','old','task-sha',{'e2':'旧译文。'})
    def test_standalone_issue_persists(self):
        self.store.record_issue({'code':'test','detail':'persist'})
        other=Store(self.path)
        try:self.assertTrue(any(i['code']=='test' for i in other.issues()))
        finally:other.close()
    def test_page_migration_warning_only_resolved_after_full_page(self):
        issue={'code':'legacy_no_exact_source','severity':'warning','page':1,'detail':'旧缓存没有精确匹配'}
        self.store.record_issue(issue)
        self.store.put_candidate('e2','给定一个集合。','test')
        self.assertEqual(self.store.reconcile_diagnostics(),0)
        self.store.put_candidate('e1','设 ⟦F:f1⟧ 为 2。','test')
        self.assertEqual(self.store.reconcile_diagnostics(),1)
        self.assertFalse(any(i['code']=='legacy_no_exact_source' for i in self.store.issues()))
        # A recurrence must reopen the existing diagnostic rather than vanish.
        self.store.record_issue(issue)
        self.assertTrue(any(i['code']=='legacy_no_exact_source' for i in self.store.issues()))
    def test_no_progress_warning_closes_only_after_task_is_complete(self):
        self.store.record_issue({'code':'translation_no_progress','severity':'warning','task_id':'t1','detail':'Repeated invalid response'})
        self.assertEqual(self.store.reconcile_diagnostics(),0)
        self.store.claim('agent')
        self.store.submit('t1','agent','task-sha',{'e1':'设 ⟦F:f1⟧ 为 2。','e2':'给定一个集合。'})
        self.assertEqual(self.store.reconcile_diagnostics(),1)
        self.assertFalse(any(i['code']=='translation_no_progress' for i in self.store.issues()))
        self.assertTrue(any(i['code']=='translation_no_progress' for i in self.store.resolved_issues()))

    def test_unsafe_source_page_is_not_sent_to_api(self):
        from textbook_pdf.translation import make_tasks
        self.book.issues=[{'code':'scanned_page_unsupported','severity':'error','page':1}]
        self.assertEqual(make_tasks(self.book,{}),[])

class NumericTests(unittest.TestCase):
    def test_equivalent_dates_and_centuries_keep_values(self):
        e=Element('dates','paragraph','Born in November 1815, he wrote in the 1800s and the mid-nineteenth century.',1)
        self.assertEqual(validate_translation(e,'他出生于1815年11月，写作于19世纪以及十九世纪中叶。'),[])
        self.assertTrue(any(i['code']=='number_mismatch' for i in validate_translation(e,'他出生于1816年11月，写作于19世纪以及十九世纪中叶。')))

if __name__=='__main__':unittest.main()
