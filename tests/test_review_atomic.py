"""Real two-connection review races with a mocked, never contacted provider."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from textbook_pdf.models import Asset,Book,Element,digest
from textbook_pdf.store import Store
from textbook_pdf import translation


class AtomicReviewTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).parent/'_tmp_review_atomic'/uuid.uuid4().hex
        self.root.mkdir(parents=True);self.addCleanup(shutil.rmtree,self.root)
        self.db=self.root/'state.db';self.config=self.root/'mock.json'
        self.config.write_text(json.dumps({'providers':{'mock':{
            'base_url':'https://never-contact.invalid/v1','api_key':'unused-test-value',
            'model':'mock','max_attempts':1,'requests_per_minute':60000,
            'price_in':0,'price_out':0}}}),encoding='utf8')
        self.book=Book('synthetic.pdf','synthetic-source','Synthetic',1,[1],
            elements=[Element('e1','exercise','Prove ⟦F:a1⟧.',1)],
            assets={'a1':Asset('a1','formula','synthetic.pdf',1,[1,1,11,11],10,10,8,'x','sha',{'pdf_page':0})})
        self.text='证明⟦F:a1⟧。'
        self.store=Store(self.db);self.addCleanup(self.store.close)
        self.store.register(self.book,{})
        self.store.add_tasks(translation.make_tasks(self.book,{}))

    def seed(self):
        self.assertTrue(self.store.put_candidate('e1',self.text,'old')['accepted'])

    def replace(self,*,asset=True,headers=False):
        fresh=deepcopy(self.book)
        if asset:fresh.assets['a1'].metadata['pdf_page']=1
        if headers:fresh.elements[0].metadata['table_headers']=['Quantifier']
        with Store(self.db)as other:
            glossary={'quantifier':'量词'} if headers else {}
            other.register(fresh,glossary)
            other.put_candidate('e1',self.text,'new-current')
            pending={'code':'semantic_review_pending','severity':'warning','element_id':'e1',
                     'translation_hash':digest(self.text),'source_hash':fresh.elements[0].source_hash,
                     'review_hash':translation._review_hash(fresh.elements[0],self.text,fresh,glossary),
                     'review_hash_schema':translation.REVIEW_HASH_SCHEMA,'detail':'New revision still needs review.'}
            other.record_issue(pending)
        return fresh,pending

    def response(self,packet,passed):
        return json.dumps({'task_id':packet['task_id'],'source_hash':packet['source_hash'],
            'reviews':{'e1':{'passed':passed,'issues':[] if passed else ['Old revision has a direction error.']}}}),{'prompt_tokens':1,'completion_tokens':1}

    def test_stale_failure_cannot_revoke_same_text_after_asset_version_change(self):
        self.seed()
        def call(provider,messages):
            self.replace()
            return self.response(json.loads(messages[-1]['content']),False)
        with patch('textbook_pdf.translation._chat',side_effect=call):
            result=translation.run_review(self.book,self.store,self.config,'mock',workers=1)
        self.assertEqual(result['stale_results'],1)
        self.assertEqual(result['review_failed'],0)
        with Store(self.db)as current:
            self.assertEqual(current.translations(),{'e1':self.text})
            self.assertFalse(any(i['code']=='semantic_review_failed'for i in current.issues()))

    def test_stale_pass_cannot_resolve_new_pending_diagnostics_or_record_a_pass(self):
        self.seed();new_pending=[]
        def call(provider,messages):
            _,pending=self.replace();new_pending.append(pending)
            return self.response(json.loads(messages[-1]['content']),True)
        with patch('textbook_pdf.translation._chat',side_effect=call):
            result=translation.run_review(self.book,self.store,self.config,'mock',workers=1)
        self.assertEqual(result['stale_results'],1)
        self.assertEqual(result['review_passed'],0)
        with Store(self.db)as current:
            self.assertIn(new_pending[0],current.issues())
            self.assertFalse(any(i['code']=='semantic_review_passed'for i in current.issues()))

    def test_current_db_metadata_and_glossary_are_used_to_derive_review_hash(self):
        self.seed()
        def call(provider,messages):
            self.replace(asset=False,headers=True)
            return self.response(json.loads(messages[-1]['content']),True)
        with patch('textbook_pdf.translation._chat',side_effect=call):
            result=translation.run_review(self.book,self.store,self.config,'mock',workers=1)
        self.assertEqual(result['stale_results'],1)
        with Store(self.db)as current:
            self.assertFalse(any(i['code']=='semantic_review_passed'for i in current.issues()))

    def test_primary_candidate_review_cannot_clear_or_revoke_new_accepted_revision(self):
        def call(provider,messages):
            packet=json.loads(messages[-1]['content'])
            if 'requested_element_ids'in packet:
                return json.dumps({'task_id':packet['task_id'],'source_hash':packet['source_hash'],
                    'translations':{'e1':self.text}}),{'prompt_tokens':1,'completion_tokens':1}
            self.replace()
            return self.response(packet,True)
        with patch('textbook_pdf.translation._chat',side_effect=call):
            result=translation.run_translation(self.book,self.store,self.config,provider='mock',
                                               review_provider='mock',workers=1,max_tasks=1)
        self.assertEqual(result['stale_review_results'],1)
        with Store(self.db)as current:
            self.assertEqual(current.translations(),{'e1':self.text})
            self.assertTrue(any(i['code']=='semantic_review_pending'for i in current.issues()))
            self.assertFalse(any(i['code']=='semantic_review_passed'for i in current.issues()))

    def test_pass_issue_and_resolutions_roll_back_together_if_recording_fails(self):
        self.seed()
        pending={'code':'semantic_review_pending','element_id':'e1','severity':'warning','detail':'Pending.'}
        self.store.record_issue(pending)
        from textbook_pdf.store import translation_rule_hash
        with patch.object(self.store,'record_issue',side_effect=RuntimeError('synthetic write failure')):
            with self.assertRaises(RuntimeError):
                self.store.apply_semantic_review('e1',self.text,
                    source_hash=self.book.elements[0].source_hash,
                    rule_hash=translation_rule_hash(self.book,self.book.elements[0],{}),
                    source_sha256=self.book.source_sha256,
                    review_hash=translation._review_hash(self.book.elements[0],self.text,self.book,{}),
                    passed=True,problems=[],provider='mock')
        self.assertIn(pending,self.store.issues())
        self.assertEqual(self.store.translations(),{'e1':self.text})

    def commit(self,book,passed=True,problems=None):
        from textbook_pdf.store import translation_rule_hash
        return self.store.apply_semantic_review('e1',self.text,
            source_hash=book.elements[0].source_hash,
            rule_hash=translation_rule_hash(book,book.elements[0],{}),
            source_sha256=book.source_sha256,
            review_hash=translation._review_hash(book.elements[0],self.text,book,{}),
            passed=passed,problems=problems or [],provider='mock')

    def test_asset_cache_refreshes_after_other_connection_registers_a_new_revision(self):
        self.seed();self.assertTrue(self.commit(self.book)['applied'])
        before=self.store.db.execute("SELECT value FROM metadata WHERE key='book_registration_revision'").fetchone()[0]
        fresh,_=self.replace()
        after=self.store.db.execute("SELECT value FROM metadata WHERE key='book_registration_revision'").fetchone()[0]
        self.assertNotEqual(before,after)
        # The Store object deliberately still has its old cached Book. The
        # commit must reconstruct the new asset from the current DB instead.
        self.assertEqual(self.store.book.assets['a1'].metadata['pdf_page'],0)
        self.assertTrue(self.commit(fresh)['applied'])
        current_hash=translation._review_hash(fresh.elements[0],self.text,fresh,{})
        self.assertTrue(any(i.get('review_hash')==current_hash and i['code']=='semantic_review_passed'
                            for i in self.store.issues()))

    def test_old_database_without_registration_revision_is_read_without_caching(self):
        self.seed()
        with self.store.db:self.store.db.execute("DELETE FROM metadata WHERE key='book_registration_revision'")
        self.assertTrue(self.commit(self.book)['applied'])
        fresh,_=self.replace()
        with self.store.db:self.store.db.execute("DELETE FROM metadata WHERE key='book_registration_revision'")
        self.assertTrue(self.commit(fresh)['applied'])
        self.assertIsNone(self.store._review_cache_revision)

    def test_failed_review_candidate_and_revocation_roll_back_on_diagnostic_failure(self):
        self.seed()
        before=self.store.db.execute('SELECT count(*) FROM candidates').fetchone()[0]
        with patch.object(self.store,'record_issue',side_effect=RuntimeError('synthetic write failure')):
            with self.assertRaises(RuntimeError):self.commit(self.book,False,['Synthetic failure.'])
        self.assertEqual(self.store.translations(),{'e1':self.text})
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM candidates').fetchone()[0],before)
        self.assertFalse(any(i['code']=='semantic_review_failed'for i in self.store.issues()))

    def test_equivalent_register_epoch_is_a_cache_refresh_not_a_stale_verdict(self):
        self.seed();self.assertTrue(self.commit(self.book)['applied'])
        before=self.store._review_cache_revision
        with Store(self.db)as other:other.register(deepcopy(self.book),{})
        self.assertTrue(self.commit(self.book)['applied'])
        self.assertNotEqual(before,self.store._review_cache_revision)

    def test_expired_candidate_lease_cannot_write_any_review_diagnostic(self):
        from textbook_pdf.store import translation_rule_hash
        task=self.store.claim('original-owner')
        with self.store.db:self.store.db.execute('UPDATE tasks SET lease_until=0 WHERE id=?',(task['id'],))
        result=self.store.apply_semantic_review('e1',self.text,
            source_hash=self.book.elements[0].source_hash,
            rule_hash=translation_rule_hash(self.book,self.book.elements[0],{}),
            source_sha256=self.book.source_sha256,
            review_hash=translation._review_hash(self.book.elements[0],self.text,self.book,{}),
            passed=False,problems=['Obsolete candidate.'],provider='mock',
            task_id=task['id'],owner='original-owner',task_source_hash=task['source_hash'])
        self.assertTrue(result['stale'])
        self.assertEqual(self.store.issues(),[])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM candidates').fetchone()[0],0)

    def test_review_savepoint_does_not_commit_its_callers_transaction(self):
        self.seed()
        self.store.db.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.commit(self.book)['applied'])
        self.assertTrue(self.store.db.in_transaction)
        self.store.db.rollback()
        self.assertFalse(any(i['code']=='semantic_review_passed'for i in self.store.issues()))

    def test_inplace_book_edit_cannot_rebind_a_verdict_after_its_request(self):
        self.seed()
        def call(provider,messages):
            self.book.assets['a1'].metadata['pdf_page']=1
            self.store.register(self.book,{})
            self.store.put_candidate('e1',self.text,'new-current')
            return self.response(json.loads(messages[-1]['content']),True)
        with patch('textbook_pdf.translation._chat',side_effect=call):
            result=translation.run_review(self.book,self.store,self.config,'mock',workers=1)
        self.assertEqual(result['stale_results'],1)
        self.assertEqual(result['review_passed'],0)
        self.assertFalse(any(i['code']=='semantic_review_passed'for i in self.store.issues()))


if __name__=='__main__':unittest.main()
