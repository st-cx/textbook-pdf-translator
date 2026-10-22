"""Candidate/diagnostic atomicity with real nested SQLite transactions."""
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from textbook_pdf.models import Book,Element
from textbook_pdf.store import Store


class CandidateTransactionTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).parent/'_tmp_store_transactions'/uuid.uuid4().hex
        self.root.mkdir(parents=True);self.addCleanup(shutil.rmtree,self.root)
        self.path=self.root/'state.db'
        self.store=Store(self.path);self.addCleanup(self.store.close)
        self.book=Book('fixture.pdf','fixture-source','Fixture',1,[1],
                       elements=[Element('e1','exercise','Choose 2 cases.',1)])
        self.store.register(self.book,{})
        self.old='选择2种情况。';self.new='选取2种情况。'
        self.store.put_candidate('e1',self.old,'original')
        self.pending={'code':'semantic_review_pending','element_id':'e1','severity':'warning','detail':'Pending new candidate.'}

    def candidates(self):
        return self.store.db.execute('SELECT count(*) FROM candidates').fetchone()[0]

    def test_accepted_candidate_and_pending_roll_back_with_outer_failure(self):
        before=self.candidates()
        with self.assertRaisesRegex(RuntimeError,'outer failure'):
            with self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                self.store.record_issue(self.pending)
                self.assertTrue(self.store.put_candidate('e1',self.new,'migration')['accepted'])
                self.assertTrue(self.store.db.in_transaction)
                raise RuntimeError('outer failure')
        self.assertEqual(self.store.translations(),{'e1':self.old})
        self.assertEqual(self.candidates(),before)
        self.assertNotIn(self.pending,self.store.issues())

    def test_rejected_candidate_and_pending_roll_back_with_outer_failure(self):
        before=self.candidates()
        with self.assertRaisesRegex(RuntimeError,'outer rejection'):
            with self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                self.store.record_issue(self.pending)
                result=self.store.put_candidate('e1','选择3种情况。','migration')
                self.assertFalse(result['accepted'])
                self.assertTrue(self.store.db.in_transaction)
                raise RuntimeError('outer rejection')
        self.assertEqual(self.store.translations(),{'e1':self.old})
        self.assertEqual(self.candidates(),before)
        self.assertEqual(self.store.issues(),[])

    def test_explicit_outer_commit_persists_both_candidate_and_pending(self):
        self.store.db.execute('BEGIN IMMEDIATE')
        self.store.record_issue(self.pending)
        self.assertTrue(self.store.put_candidate('e1',self.new,'migration')['accepted'])
        self.assertTrue(self.store.db.in_transaction)
        self.store.db.commit()
        with Store(self.path)as independent:
            self.assertEqual(independent.translations(),{'e1':self.new})
            self.assertIn(self.pending,independent.issues())

    def test_standalone_call_still_commits_valid_candidate(self):
        self.assertTrue(self.store.put_candidate('e1',self.new,'standalone')['accepted'])
        self.assertFalse(self.store.db.in_transaction)
        with Store(self.path)as independent:self.assertEqual(independent.translations(),{'e1':self.new})

    def test_standalone_rejection_remains_a_durable_candidate_without_overwriting(self):
        before=self.candidates()
        self.assertFalse(self.store.put_candidate('e1','选择3种情况。','standalone')['accepted'])
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.candidates(),before+1)
        with Store(self.path)as independent:
            self.assertEqual(independent.translations(),{'e1':self.old})
            self.assertTrue(any(i['code']=='number_mismatch'for i in independent.issues()))

    def test_internal_failure_rolls_back_only_its_savepoint(self):
        before=self.candidates();original=self.store._accept
        def fail(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('candidate write failed')
        self.store.db.execute('BEGIN IMMEDIATE');self.store.record_issue(self.pending)
        with patch.object(self.store,'_accept',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'candidate write failed'):
                self.store.put_candidate('e1',self.new,'migration')
        self.assertTrue(self.store.db.in_transaction)
        self.assertEqual(self.store.translations(),{'e1':self.old})
        self.assertEqual(self.candidates(),before)
        self.store.db.commit()
        self.assertIn(self.pending,self.store.issues())

    def test_keyboard_interrupt_rolls_back_a_standalone_candidate(self):
        before=self.candidates();original=self.store._accept
        def interrupt(*args,**kwargs):
            original(*args,**kwargs)
            raise KeyboardInterrupt()
        with patch.object(self.store,'_accept',side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):self.store.put_candidate('e1',self.new,'interrupted')
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.translations(),{'e1':self.old})
        self.assertEqual(self.candidates(),before)


if __name__=='__main__':unittest.main()
