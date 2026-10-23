from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import json,shutil,sys,unittest,uuid
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path[:0]=[str(HERE),str(ROOT)]
from textbook_pdf.models import Book,Element,Asset,digest,file_sha256
from textbook_pdf.store import Store,translation_rule_hash
from textbook_pdf.translation import _review_binding,review_status,_review_snapshot,make_tasks,run_translation
from textbook_pdf.semantic_units import UnitReviewStore,SCHEMA,PROFILE_KEY,build_packet,validate_response

class UnitStoreTests(unittest.TestCase):
    def setUp(self):
        self.root=HERE/('synthetic-'+uuid.uuid4().hex);self.root.mkdir();self.s=Store(self.root/'tasks.db')
        (self.root/'native.bin').write_bytes(b'self-authored-native-fixture-not-a-math-quality-certificate')
        a=Asset('a','formula','native.bin',1,[0,0,10,10],10,10,8,'x',file_sha256(self.root/'native.bin'),{'glyph_ids':['g'],'pdf_page':0})
        self.elements=[Element('before','paragraph','Context ⟦F:a⟧.',1,asset_ids=['a']),
            Element('left','paragraph','A note from one sensor',1,printed_page='885'),
            Element('h1','running_header','885',1),Element('h2','running_header','886',2),
            Element('right','paragraph','to another becomes a shared message.',2,printed_page='886'),
            Element('after','paragraph','The following paragraph.',2)]
        self.b=Book('synthetic.pdf','own-source','Self-authored source',2,[1,2],elements=self.elements,assets={'a':a})
        self.s.register(self.b,{})
        self.trans={'before':'上下文⟦F:a⟧。','left':'来自一个传感器发往另一个传感器的备注','right':'成为共享消息。','after':'后续段落。'}
        for eid,text in self.trans.items():self.assertTrue(self.s.put_candidate(eid,text,'synthetic')['accepted'])
        r=self.b.element_map()['right'];binding=_review_binding(r,self.trans['right'],self.b,{});binding.pop('element_id')
        self.assertTrue(self.s.apply_semantic_review('right',self.trans['right'],passed=False,problems=['fragment-only missing language'],provider='synthetic',**binding)['applied'])
        old=self.s.db.execute("SELECT fingerprint,json FROM issues WHERE json_extract(json,'$.code')='semantic_review_failed'").fetchone()
        self.failure={'fingerprint':old['fingerprint'],'json_sha256':digest(json.loads(old['json']))}
        self.u={'schema':SCHEMA,'id':'own-prose-unit','role':'body_prose','source_pdf_sha256':'own-source',
            'member_ids':['left','right'],'context_ids':['before','after'],'intervening_header_ids':['h1','h2'],
            'adjacency_chain':['left','h1','h2','right'],'source_ordinals':{e.id:i for i,e in enumerate(self.elements)},
            'member_source_full_sha256':{e.id:digest(asdict(e))for e in self.elements if e.id in('left','right')},
            'context_source_full_sha256':{e.id:digest(asdict(e))for e in self.elements if e.id not in('left','right')},
            'source_visual_evidence':[{'self_authored':True,'boundary':'one sensor / to another'}],
            'boundary':{'id':'b','left_suffix':'from one sensor','right_prefix':'to another','source_join':'space'},
            'authorized_issue_resolutions':[self.failure],'restore_review_failed_member_ids':['right']}
        self.b.profile[PROFILE_KEY]={self.u['id']:self.u};self.s.register(self.b,{})
        self.facade=UnitReviewStore(self.s);self.packet=self.facade.packet(self.u['id'])
        self.response=deepcopy(self.packet['required_response'])
    def tearDown(self):
        self.s.close();assert self.root.resolve().is_relative_to(HERE.resolve());shutil.rmtree(self.root)
    def has_table(self):return self.s.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='semantic_unit_reviews'").fetchone()is not None
    def apply_pass(self):
        r=self.facade.apply(self.packet,self.response);self.assertTrue(r['applied'],r);return r
    def register_modified(self,func):
        b=deepcopy(self.b);func(b);self.s.register(b,{});return b
    def test_snapshot_does_not_create_result_table(self):self.assertFalse(self.has_table());self.assertEqual(self.facade.status(self.u['id'])['state'],'pending')
    def test_pass_stores_one_group_proof_and_zero_individual_passes(self):
        r=self.apply_pass();self.assertEqual(r['individual_semantic_passes_written'],0)
        self.assertEqual(self.s.db.execute("SELECT count(*) FROM issues WHERE json_extract(json,'$.code')='semantic_review_passed'").fetchone()[0],0)
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM semantic_unit_reviews').fetchone()[0],1)
        self.assertEqual(self.facade.status(self.u['id'])['state'],'passed')
    def test_exact_false_failure_resolved_but_original_json_preserved(self):
        before=self.s.db.execute('SELECT json FROM issues WHERE fingerprint=?',(self.failure['fingerprint'],)).fetchone()[0];self.apply_pass()
        after=self.s.db.execute('SELECT json,resolved,resolution_reason FROM issues WHERE fingerprint=?',(self.failure['fingerprint'],)).fetchone()
        self.assertEqual(after['json'],before);self.assertEqual(after['resolved'],1);self.assertIn('semantic_unit_context_passed',after['resolution_reason'])
    def test_restores_only_current_failed_translation_without_text_change(self):
        self.apply_pass();self.assertEqual(self.s.translations(),self.trans)
        self.assertEqual(self.packet['payload']['source_fragments'][0]['printed_page'],'885');self.assertEqual(self.packet['payload']['source_fragments'][1]['printed_page'],'886')
    def test_normal_task_coverage_finishes_only_after_exact_member_restored(self):
        self.s.add_tasks(make_tasks(self.b,{}));self.assertTrue(self.s.pending_tasks());self.apply_pass();self.assertEqual(self.s.pending_tasks(),[])
    def test_derived_read_status_passes_both_only_while_unit_current(self):
        self.apply_pass();r=review_status(self.b,self.s);self.assertEqual(set(r['ids']['passed']),{'left','right'});self.assertEqual(r['individual_semantic_passes_synthesized'],0)
    def test_member_translation_change_invalidates_every_member(self):
        self.apply_pass();self.s.put_candidate('left','新的另一种语言研究','synthetic')
        self.assertEqual(self.facade.status(self.u['id'])['state'],'pending');r=review_status(self.b,self.s)
        self.assertTrue({'left','right'}<=set(r['ids']['pending']));self.assertFalse({'left','right'}&set(r['ids']['passed']));self.assertFalse(r['coverage_complete'])
    def test_context_translation_change_invalidates_group(self):
        self.apply_pass();self.s.put_candidate('after','更新的后续段落。','synthetic');self.assertEqual(self.facade.status(self.u['id'])['state'],'pending')
    def test_newly_accepted_member_revision_is_never_overwritten_by_old_response(self):
        self.s.put_candidate('right','新版本：引出了另一形式概念。','synthetic');r=self.facade.apply(self.packet,self.response)
        self.assertTrue(r['stale']);self.assertEqual(self.s.translations()['right'],'新版本：引出了另一形式概念。')
    def test_member_source_or_style_change_rejects_stale_response(self):
        self.register_modified(lambda b:b.elements[1].style.update(source_size=99));self.assertTrue(self.facade.apply(self.packet,self.response)['stale'])
    def test_context_source_change_rejects_stale_response(self):
        self.register_modified(lambda b:setattr(b.elements[0],'text','Changed context ⟦F:a⟧.'));self.assertTrue(self.facade.apply(self.packet,self.response)['stale'])
    def test_asset_text_change_invalidates_all_members(self):
        self.apply_pass();self.register_modified(lambda b:setattr(b.assets['a'],'text','different math'))
        self.assertEqual(self.facade.status(self.u['id'])['state'],'pending_unverifiable')
    def test_actual_asset_file_tamper_rejects_stale_response(self):
        (self.root/'native.bin').write_bytes(b'tampered');self.assertTrue(self.facade.apply(self.packet,self.response)['stale'])
    def test_asset_page_index_change_rejects_response(self):
        self.register_modified(lambda b:b.assets['a'].metadata.update(pdf_page=1));self.assertTrue(self.facade.apply(self.packet,self.response)['stale'])
    def test_changed_global_glossary_rejects_response(self):
        self.s.register(self.b,{'translation':'翻译'});self.assertTrue(self.facade.apply(self.packet,self.response)['stale'])
    def test_inserted_intervening_body_not_silently_skipped(self):
        self.register_modified(lambda b:b.elements.insert(2,Element('new-body','paragraph','Hidden source.',1)))
        self.assertTrue(self.facade.apply(self.packet,self.response)['stale'])
    def test_skipped_header_changed_to_body_not_allowed(self):
        self.register_modified(lambda b:setattr(b.elements[2],'kind','paragraph'));self.assertTrue(self.facade.apply(self.packet,self.response)['stale'])
    def test_response_cannot_return_duplicate_translation_patch(self):
        r=deepcopy(self.response);r['translations']={'right':'到另一种语言成为共享消息。'}
        with self.assertRaises(ValueError):self.facade.apply(self.packet,r)
        self.assertFalse(self.has_table());self.assertEqual(self.s.translations(accepted_only=False)['right'],self.trans['right'])
    def test_missing_joint_boundary_coverage_rejected(self):
        r=deepcopy(self.response);r['boundary_coverage']['reviewed_together']=False
        with self.assertRaises(ValueError):validate_response(self.packet,r)
    def test_stale_unit_hash_response_rejected(self):
        r=deepcopy(self.response);r['unit_review_hash']='0'*64
        with self.assertRaises(ValueError):validate_response(self.packet,r)
    def test_pass_with_duplicate_flag_rejected(self):
        r=deepcopy(self.response);r['boundary_coverage']['no_omission_or_duplicate']=False
        with self.assertRaises(ValueError):validate_response(self.packet,r)
    def test_new_individual_current_failure_not_cleared_by_old_authorization(self):
        self.s.record_issue({'code':'semantic_review_failed','severity':'error','element_id':'right','page':2,
            'source_hash':self.packet['payload']['elements']['right']['source_hash'],
            'translation_hash':digest(self.trans['right']),'detail':'A different current failure'})
        r=self.facade.apply(self.packet,self.response);self.assertEqual(r['reason'],'unauthorized_current_failure');self.assertFalse(self.has_table())
    def test_historical_other_translation_failure_not_cleared(self):
        issue={'code':'semantic_review_failed','severity':'error','element_id':'right','page':2,
            'source_hash':self.packet['payload']['elements']['right']['source_hash'],'translation_hash':digest('old different translation'),'detail':'old history'}
        self.s.record_issue(issue);self.apply_pass();self.assertIn(issue,self.s.issues())
    def test_failure_without_exact_restore_permission_cannot_restore_row(self):
        self.u['restore_review_failed_member_ids']=[];self.s.register(self.b,{})
        p=self.facade.packet(self.u['id']);r=self.facade.apply(p,p['required_response']);self.assertEqual(r['reason'],'review_failed_restore_not_authorized')
    def test_fault_rolls_back_group_result_resolution_and_restore(self):
        def fault(where):
            if where=='after_writes_before_commit':raise RuntimeError('synthetic injected fault')
        with self.assertRaises(RuntimeError):self.facade.apply(self.packet,self.response,fault=fault)
        self.assertFalse(self.has_table());self.assertEqual(self.s.db.execute('SELECT resolved FROM issues WHERE fingerprint=?',(self.failure['fingerprint'],)).fetchone()[0],0)
        self.assertNotIn('right',self.s.translations())
    def test_outer_transaction_and_nested_readers_preserve_rollback_ownership(self):
        with self.assertRaises(RuntimeError):
            with self.s._atomic_write():
                r=self.facade.apply(self.packet,self.response);self.assertEqual(r['commit_state'],'pending_outer_transaction')
                self.assertEqual(self.facade.status(self.u['id'])['state'],'passed');self.facade.packet(self.u['id'])
                raise RuntimeError('caller rolls back')
        self.assertFalse(self.has_table());self.assertNotIn('right',self.s.translations())
        self.assertEqual(self.s.db.execute('SELECT resolved FROM issues WHERE fingerprint=?',(self.failure['fingerprint'],)).fetchone()[0],0)
    def test_other_store_source_update_rejects_stale_session(self):
        other=Store(self.root/'tasks.db')
        try:
            b=deepcopy(self.b);b.elements[4].text='to another led to something new.';other.register(b,{})
            self.assertTrue(self.facade.apply(self.packet,self.response)['stale'])
        finally:other.close()
    def test_external_file_change_between_checks_is_rechecked(self):
        def fault(where):
            if where=='after_all_member_checks':(self.root/'native.bin').write_bytes(b'changed during checks')
        r=self.facade.apply(self.packet,self.response,fault=fault);self.assertTrue(r['stale']);self.assertFalse(self.has_table())
    def test_rejected_unit_verdict_never_resolves_individual_failure(self):
        r=deepcopy(self.response);r['passed']=False;r['issues']=['Overall semantic mismatch'];r['boundary_coverage']['no_omission_or_duplicate']=False
        self.assertTrue(self.facade.apply(self.packet,r)['applied']);self.assertEqual(self.facade.status(self.u['id'])['state'],'failed')
        self.assertEqual(self.s.db.execute('SELECT resolved FROM issues WHERE fingerprint=?',(self.failure['fingerprint'],)).fetchone()[0],0)
    def test_per_element_review_scheduler_excludes_opted_unit(self):
        snapshot=_review_snapshot(self.b,self.s);self.assertFalse({'left','right'}&set(snapshot[4]))
    def test_automatic_fragment_translation_blocked_before_provider_or_api(self):
        self.s.add_tasks(make_tasks(self.b,{}))
        with self.assertRaisesRegex(ValueError,'joint review'):run_translation(self.b,self.s,'definitely-not-a-config.json',workers=1,max_tasks=1)
    def test_disabling_registry_does_not_create_individual_pass_or_clear_pending_marker(self):
        self.apply_pass();b=deepcopy(self.b);b.profile.pop(PROFILE_KEY);self.s.register(b,{})
        status=review_status(b,self.s);self.assertFalse({'left','right'}&set(status['ids']['passed']));self.assertTrue({'left','right'}<=set(status['ids']['pending']))
    def test_index_role_cannot_be_opted_in_as_prose(self):
        self.u['role']='subject_index';self.s.register(self.b,{})
        with self.assertRaises(ValueError):self.facade.packet(self.u['id'])
    def test_reading_guide_exact_discretionary_hyphen_join_retains_page_fragments(self):
        with self.s.lock:snapshot=deepcopy(self.facade._current(self.u['id'])[1])
        u=deepcopy(self.u);u['role']='reading_guide_prose';u['boundary'].update(source_join='exact_discretionary_hyphen',left_suffix='Cran-',right_prefix='dall',original_hyphen_glyph_evidence={'source_gid':'own-g1','character':'-','page':1})
        for eid,text in [('left','References in Cran-'),('right','dall and Pomerance are useful.')]:
            row=snapshot['elements'][eid];row['element']['text']=text;row['source_hash']=Element(**row['element']).source_hash;row['translation']['source_hash']=row['source_hash'];u['member_source_full_sha256'][eid]=digest(row['element'])
        p=build_packet(u,snapshot);self.assertIn('Crandall',p['payload']['joined_source_readonly']);self.assertEqual([x['physical_page']for x in p['payload']['source_fragments']],[1,2])
        u['boundary'].pop('original_hyphen_glyph_evidence')
        with self.assertRaises(ValueError):build_packet(u,snapshot)
    def test_pure_snapshot_inconsistent_source_hash_rejected(self):
        with self.s.lock:snapshot=deepcopy(self.facade._current(self.u['id'])[1])
        snapshot['elements']['left']['element']['text']='Changed one language';u=deepcopy(self.u);u['member_source_full_sha256']['left']=digest(snapshot['elements']['left']['element'])
        with self.assertRaises(ValueError):build_packet(u,snapshot)

if __name__=='__main__':unittest.main()
