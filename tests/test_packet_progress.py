"""Synthetic request progress regressions; never contacts a provider."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

from textbook_pdf import translation as tr
from textbook_pdf.models import Asset,Book,Element
from textbook_pdf.store import Store


class PacketProgress(unittest.TestCase):
    def setUp(self):
        self.base=Path(__file__).resolve().parent/'_tmp_packet_progress'
        self.directory=self.base/uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.config=self.directory/'unused-config.json'
        self.config.write_text(json.dumps({'providers':{'mock':{
            'base_url':'http://never-contact.invalid/v1','api_key':'synthetic-unused',
            'model':'mock','max_tokens':8192,'max_attempts':3,'requests_per_minute':600000}}}),encoding='utf8')
        self.stores=[];self.calls=[]

    def tearDown(self):
        for store in reversed(self.stores):store.close()
        self.assertTrue(self.directory.resolve().is_relative_to(self.base.resolve()))
        shutil.rmtree(self.directory)

    def fixture(self,count=12):
        a=Asset('f1','formula','unused.pdf',1,[0,0,10,10],10,10,text='p implies q',sha256='synthetic',metadata={'pdf_page':0})
        self.book=Book('unused.pdf','synthetic','Synthetic',1,[1],assets={'f1':a},elements=[
            Element(f'e{i}','exercise',f'Part {i}: consider 2 cases with ⟦F:f1⟧.',1,
                    asset_ids=['f1'],group_id='whole-question',chapter='1')for i in range(count)])
        self.store=Store(self.directory/'state.db');self.stores.append(self.store)
        self.store.register(self.book,{})
        tasks=tr.make_tasks(self.book,{},target_chars=100000);self.assertEqual(len(tasks),1)
        self.task=tasks[0];self.store.add_tasks(tasks)

    def api(self,provider,messages):
        p=json.loads(messages[-1]['content']);ids=p['requested_element_ids'];self.calls.append(ids)
        self.assertEqual(p['task_id'],self.task['id']);self.assertEqual(p['source_hash'],self.task['source_hash'])
        self.assertLessEqual(len(ids),6)
        self.assertEqual(p['payload']['readonly_source_groups'][0]['member_ids'],self.task['element_ids'])
        values={eid:f'第{eid[1:]}小问，考虑'+('3'if int(eid[1:])<6 else'2')+'种情况与⟦F:f1⟧。'for eid in ids}
        return json.dumps({'task_id':p['task_id'],'source_hash':p['source_hash'],'translations':values},ensure_ascii=False),{'prompt_tokens':1,'completion_tokens':1}

    def run_worker(self,api=None,**kwargs):
        with patch.object(tr,'_chat',side_effect=api or self.api):
            return tr.run_translation(self.book,self.store,self.config,provider='mock',workers=1,task_ids=[self.task['id']],**kwargs)

    def assert_pending_without_lease(self):
        self.assertEqual(self.store.db.execute('SELECT state FROM tasks').fetchone()[0],'pending')
        self.assertEqual(self.store.db.execute("SELECT count(*)FROM tasks WHERE state='leased'").fetchone()[0],0)

    def test_invalid_front_packet_does_not_starve_valid_tail(self):
        self.fixture();report=self.run_worker()
        self.assertEqual(set(self.store.translations()),{f'e{i}'for i in range(6,12)})
        self.assertEqual(self.calls[:2],[self.task['element_ids'][:6]]*2)
        self.assertEqual(self.calls[2],self.task['element_ids'][6:])
        self.assertEqual(report['requests'],3);self.assertEqual(report['completed'],0)
        self.assert_pending_without_lease()
        rejected={r[0]for r in self.store.db.execute('SELECT element_id,issues FROM candidates')
                  if any(i.get('severity','error')=='error'for i in json.loads(r[1]))}
        self.assertEqual(rejected,{f'e{i}'for i in range(6)})

    def test_resume_never_retranslates_saved_tail(self):
        self.fixture();self.run_worker();saved=dict(self.store.translations())
        self.assertEqual(len(saved),6)
        self.store.db.execute('UPDATE tasks SET next_attempt=0');self.store.db.commit();self.calls=[]
        self.run_worker()
        self.assertEqual(self.store.translations(),saved)
        self.assertTrue(self.calls)
        self.assertTrue(all(not set(ids)&saved.keys()for ids in self.calls))
        self.assert_pending_without_lease()

    def test_protected_token_rejection_stays_rejected_while_tail_saves(self):
        self.fixture();base=self.api
        def bad_token(provider,messages):
            raw,usage=base(provider,messages);p=json.loads(raw)
            for eid in p['translations']:
                if int(eid[1:])<6:p['translations'][eid]=f'第{eid[1:]}小问，考虑2种情况。'
            return json.dumps(p,ensure_ascii=False),usage
        self.run_worker(bad_token)
        self.assertEqual(set(self.store.translations()),{f'e{i}'for i in range(6,12)})
        errors=[json.loads(r[0])for r in self.store.db.execute('SELECT issues FROM candidates')]
        self.assertTrue(any(any(i['code']=='protected_token_mismatch'for i in row)for row in errors))
        self.assert_pending_without_lease()

    def test_cancel_after_tail_reply_saves_it_and_releases_lease(self):
        self.fixture(18);session=tr.TranslationRoutingSession(json.loads(self.config.read_text('utf8')),'mock');base=self.api
        def cancel(provider,messages):
            value=base(provider,messages)
            if any(int(eid[1:])>=6 for eid in self.calls[-1]):session.stop.set()
            return value
        report=self.run_worker(cancel,routing_session=session)
        self.assertTrue(report['cancelled'])
        self.assertEqual(set(self.store.translations()),{f'e{i}'for i in range(6,12)})
        self.assertFalse(any(int(eid[1:])>=12 for ids in self.calls for eid in ids))
        self.assert_pending_without_lease()

    def test_total_request_cap_still_stops_all_later_packets(self):
        self.fixture(24)
        with patch.object(tr,'TRANSLATION_TASK_MAX_REQUESTS',3):report=self.run_worker()
        self.assertEqual(report['requests'],3);self.assertEqual(len(self.calls),3)
        self.assertEqual(set(self.store.translations()),{f'e{i}'for i in range(6,12)})
        self.assert_pending_without_lease()

    def test_current_asset_revision_prevents_stale_tail_acceptance(self):
        self.fixture();base=self.api;other=Store(self.directory/'state.db');self.stores.append(other)
        def change_source(provider,messages):
            value=base(provider,messages)
            if len(self.calls)==2:
                changed=deepcopy(self.book);changed.assets['f1'].metadata['pdf_page']=1
                other.register(changed,{})
            return value
        self.run_worker(change_source)
        self.assertEqual(other.translations(),{})
        self.assertEqual(self.store.db.execute("SELECT count(*)FROM tasks WHERE state='leased'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
