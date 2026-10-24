from pathlib import Path
from copy import deepcopy
from dataclasses import asdict
import json,sys,threading,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from argument_fixture import make_fixture
from textbook_pdf.models import Book,Element,digest,source_contract_issues
from textbook_pdf.argument_units import checked_units,source_issues,PROFILE_KEY,MEMBER_KEY,argument_member_fields
from textbook_pdf.argument_joint_store import ArgumentJointStore
from textbook_pdf.argument_review import validate_response
from textbook_pdf.store import Store,translation_rule_hash
from textbook_pdf.translation import make_tasks,_current_task,_request_payload,_review_hash,run_review,review_status

def folder(label):
    p=ROOT/'evidence'/'test-artifacts'/(label+'-'+uuid.uuid4().hex[:10]);p.parent.mkdir(parents=True,exist_ok=True);return p

class ArgumentSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.book,cls.texts=make_fixture(folder('source'))
    def test_valid_source_and_coverage(self):self.assertEqual(source_contract_issues(self.book),[]);self.assertEqual(list(checked_units(self.book)),['self-argument'])
    def test_forbidden_source_mutations(self):
        mutations={
          'source_member_text':lambda b:setattr(b.elements[1],'text','A different premise.'),
          'source_parent':lambda b:setattr(b.elements[0],'text','2. Another exercise.'),
          'swapped_roles':lambda b:b.profile[PROFILE_KEY]['self-argument']['member_roles'].reverse(),
          'divider_width':lambda b:b.profile[PROFILE_KEY]['self-argument']['divider'].__setitem__('preserve_raw_width_pt',.4),
          'divider_color':lambda b:b.profile[PROFILE_KEY]['self-argument']['divider'].__setitem__('stroke_components',[0,0,0,1]),
          'divider_wrong_between':lambda b:b.profile[PROFILE_KEY]['self-argument']['divider'].__setitem__('between_member_ids',['p1','c']),
          'mutable_source_verdict':lambda b:b.profile[PROFILE_KEY]['self-argument'].__setitem__('passed',True),
          'joint_pass_inferred':lambda b:b.profile[PROFILE_KEY]['self-argument']['joint_review'].__setitem__('individual_passes_imply_unit_pass',True),
          'old_semantic_unit_relaxation':lambda b:b.profile[PROFILE_KEY]['self-argument']['joint_review'].__setitem__('legacy_semantic_unit_v1_is_applicable',True),
          'glyph_partition_overlap':lambda b:b.profile[PROFILE_KEY]['self-argument']['member_source_glyph_ids']['p2'].append(b.profile[PROFILE_KEY]['self-argument']['member_source_glyph_ids']['p1'][0]),
          'unknown_member_role':lambda b:b.elements[1].metadata[MEMBER_KEY].__setitem__('role','footnote'),
          'marker_removed':lambda b:b.elements[-1].asset_ids.clear(),
          'source_size_shrink':lambda b:b.profile[PROFILE_KEY]['self-argument']['layout'].__setitem__('fixed_text_size_pt',8),
          'source_asset_changed':lambda b:setattr(next(iter(b.assets.values())),'baseline',1),
          'primitive_unsupported':lambda b:b.pages[0]['graphics_coverage'][0].__setitem__('disposition','preserved-in-asset'),
          'primitive_unbound':lambda b:b.pages[0]['graphics_coverage'][0].__setitem__('argument_unit_id','unknown'),
          'primitive_fake_asset':lambda b:b.pages[0]['graphics_coverage'][0].__setitem__('asset_ids',[next(iter(b.assets))]),
          'partial_parent':lambda b:b.elements.pop(0),
          'interposed_sidebar':lambda b:b.elements.insert(2,Element('aside','sidebar','A true independent sidebar.',1,metadata={})),
          'registry_removed':lambda b:b.profile.pop(PROFILE_KEY),
        }
        for label,mutate in mutations.items():
            with self.subTest(label=label):
                book=deepcopy(self.book);mutate(book);self.assertTrue(source_issues(book))
    def test_default_fields_empty_and_default_task_has_no_argument_keys(self):
        b=Book('','a'*64,'Plain',1,[1],elements=[Element('x','paragraph','Plain source.',1)])
        self.assertEqual(argument_member_fields(b.elements[0],b),{})
        task=make_tasks(b,{})[0];self.assertNotIn('argument_context',task['payload']);self.assertNotIn('argument_policy',task['payload']['elements'][0]);self.assertTrue(_current_task(task,b,{}))
    def test_policy_canonical_readonly_completed_and_request(self):
        tasks=make_tasks(self.book,{});self.assertEqual(len(tasks),1);task=tasks[0]
        self.assertTrue(_current_task(task,self.book,{}));self.assertIn('argument_context',task['payload'])
        self.assertTrue(all('argument_policy'in row for row in task['payload']['elements']))
        p=_request_payload(task,['p2'],{'p1':self.texts['p1']})['payload']
        self.assertIn('argument_policy',p['completed_context'][0]);self.assertIn('argument_context',p)
    def test_unchanged_text_old_rule_task_review_stale_on_relation_change(self):
        before=self.book;after=deepcopy(before);parent=after.elements[0];parent.text='1. Check the exact ordered premises.'
        after.profile[PROFILE_KEY]['self-argument']['parent_full_source_sha256']=digest(asdict(parent))
        e=before.elements[1];new=after.elements[1];self.assertEqual(e.source_hash,new.source_hash)
        self.assertNotEqual(translation_rule_hash(before,e,{}),translation_rule_hash(after,new,{}))
        self.assertNotEqual(_review_hash(e,self.texts[e.id],before,{}),_review_hash(new,self.texts[new.id],after,{}))
        self.assertFalse(_current_task(make_tasks(before,{})[0],after,{}))
    def test_partial_scope_cannot_drop_parent_or_untranslated_formula(self):
        b=deepcopy(self.book);b.elements=b.elements[1:]
        with self.assertRaises(ValueError):make_tasks(b,{})
    def test_old_semantic_unit_protocol_not_reused(self):
        from textbook_pdf.semantic_units import definitions
        b=deepcopy(self.book);b.profile['semantic_review_units']={'bad':{'id':'bad','schema':'semantic-unit-review-v1','role':'exercise','member_ids':['p1','p2','c']}}
        with self.assertRaises(ValueError):definitions(b)
    def test_readonly_parent_native_outside_neighbor_window_is_required(self):
        book=deepcopy(self.book);u=book.profile[PROFILE_KEY]['self-argument'];parent=book.elements[0]
        asset=deepcopy(next(iter(book.assets.values())));asset.id='readonly-parent-native';book.assets[asset.id]=asset
        parent.text='Evaluate ⟦F:readonly-parent-native⟧.';parent.asset_ids=[asset.id]
        for e in book.elements:e.group_id=e.id
        u['parent_full_source_sha256']=digest(asdict(parent))
        u['member_source_full_sha256']={e.id:digest(asdict(e))for e in book.elements[1:]}
        tasks=make_tasks(book,{},target_chars=1);task=next(t for t in tasks if t['element_ids']==['c'])
        self.assertNotIn('parent',[r['id']for r in task['payload']['context_before']])
        self.assertIn(asset.id,task['payload']['protected_asset_descriptors']);self.assertTrue(_current_task(task,book,{}))
        for key in ['protected_asset_descriptors','protected_assets']:task['payload'][key].pop(asset.id)
        task['source_hash']=digest(task['payload'])
        self.assertFalse(_current_task(task,book,{}))

class ArgumentTransactionTests(unittest.TestCase):
    def setUp(self):
        self.root=folder('transaction');self.book,self.texts=make_fixture(self.root);self.db=self.root/'fake-tasks.db';self.store=Store(self.db);self.store.register(self.book,{})
        self.store.add_tasks(make_tasks(self.book,{}))
        for e in self.book.elements:
            result=self.store.put_candidate(e.id,self.texts[e.id],'self-authored-fixture');self.assertTrue(result['accepted'],result)
        self.facade=ArgumentJointStore(self.store)
    def tearDown(self):self.store.close()
    def good(self,planned):return deepcopy(planned['packet']['required_response'])
    def test_init_and_prepare_do_not_create_review_tables(self):
        self.facade.prepare('self-argument');self.assertFalse(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='argument_joint_reviews'").fetchone())
    def test_joint_pass_only_no_member_passes(self):
        planned=self.facade.prepare('self-argument');r=self.facade.apply(planned,self.good(planned));self.assertTrue(r['applied']);self.assertEqual(r['individual_semantic_passes_created'],0)
        self.assertEqual(self.facade.status('self-argument')['state'],'passed')
        self.assertEqual(self.store.db.execute("SELECT count(*)FROM issues WHERE json_extract(json,'$.code')='semantic_review_passed'").fetchone()[0],0)
        s=review_status(self.book,self.store);self.assertEqual(s['argument_units']['counts']['passed'],1)
    def test_individual_pass_does_not_imply_joint_pass(self):
        from textbook_pdf.translation import _review_binding
        for e in self.book.elements[1:]:
            binding=_review_binding(e,self.texts[e.id],self.book,{})
            self.store.apply_semantic_review(binding.pop('element_id'),self.texts[e.id],passed=True,problems=[],provider='fake',**binding)
        self.assertEqual(self.facade.status('self-argument')['state'],'pending');self.assertEqual(review_status(self.book,self.store)['argument_units']['counts']['passed'],0)
    def test_duplicate_and_overtaking_result_rejected(self):
        a=self.facade.prepare('self-argument');b=deepcopy(a);self.assertTrue(self.facade.apply(a,self.good(a))['applied'])
        bad=self.good(b);bad.update(passed=False,issues=['Incorrect implication direction.'])
        self.assertFalse(self.facade.apply(b,bad)['applied']);self.assertEqual(self.facade.status('self-argument')['state'],'passed')
    def test_two_store_concurrent_CAS_only_one_commit(self):
        other=Store(self.db);other.book=self.book;other.glossary={};facade=ArgumentJointStore(other)
        try:
            a=self.facade.prepare('self-argument');b=facade.prepare('self-argument');barrier=threading.Barrier(2)
            def apply(f,p):barrier.wait(timeout=5);return f.apply(p,self.good(p))
            with ThreadPoolExecutor(max_workers=2)as pool:
                futures=[pool.submit(apply,self.facade,a),pool.submit(apply,facade,b)]
                results=[f.result(timeout=10)for f in futures]
            self.assertEqual(sum(r['applied']for r in results),1)
            self.assertEqual(sum(r.get('stale',False)for r in results),1)
            self.assertEqual(self.store.db.execute('SELECT count(*)FROM argument_joint_reviews').fetchone()[0],1)
        finally:other.close()
    def test_later_current_individual_failure_does_not_hide_behind_joint_pass(self):
        from textbook_pdf.translation import _review_binding
        a=self.facade.prepare('self-argument');self.assertTrue(self.facade.apply(a,self.good(a))['applied'])
        e=self.book.element_map()['p2'];binding=_review_binding(e,self.texts[e.id],self.book,{});binding.pop('element_id')
        self.assertTrue(self.store.apply_semantic_review(e.id,self.texts[e.id],passed=False,problems=['New negation defect'],provider='fake',**binding)['applied'])
        self.assertEqual(self.facade.status('self-argument')['state'],'failed')
        current=self.facade.prepare('self-argument');result=self.facade.apply(current,self.good(current))
        self.assertFalse(result['applied']);self.assertIn('explicit_resolution',result['reason'])
        self.assertEqual(review_status(self.book,self.store)['argument_units']['counts']['passed'],0)
    def test_registered_argument_profile_cached_only_under_current_registration_revision(self):
        queries=[];self.store.db.set_trace_callback(queries.append);e=self.book.elements[1]
        try:
            with self.store._atomic_write():self.store._current_review_book(e)
            count=lambda:sum("json_extract(value,'$.profile.argument_units_v1')"in q for q in queries)
            first=count();self.assertEqual(first,1)
            with self.store._atomic_write():self.store._current_review_book(e)
            self.assertEqual(count(),first)
            self.store.register(self.book,{})
            with self.store._atomic_write():self.store._current_review_book(e)
            self.assertEqual(count(),first+1)
            self.book.profile.pop(PROFILE_KEY)
            for item in self.book.elements:item.metadata.pop(MEMBER_KEY,None)
            self.store.register(self.book,{})
            with self.store._atomic_write():self.store._current_review_book(e)
            self.assertEqual(count(),first+2)
            with self.store._atomic_write():current=self.store._current_review_book(e)
            self.assertEqual(count(),first+2);self.assertNotIn(PROFILE_KEY,current.profile)
        finally:self.store.db.set_trace_callback(None)
    def test_readonly_parent_current_failure_holds_old_and_new_joint_pass(self):
        from textbook_pdf.translation import _review_binding
        a=self.facade.prepare('self-argument');self.assertTrue(self.facade.apply(a,self.good(a))['applied'])
        e=self.book.element_map()['parent'];binding=_review_binding(e,self.texts[e.id],self.book,{});binding.pop('element_id')
        self.assertTrue(self.store.apply_semantic_review(e.id,self.texts[e.id],passed=False,problems=['Current question meaning incorrect'],provider='fake',**binding)['applied'])
        status=self.facade.status('self-argument');self.assertEqual(status['state'],'failed')
        self.assertIn('parent',status['dependency_ids_with_current_failure'])
        b=self.facade.prepare('self-argument');self.assertFalse(self.facade.apply(b,self.good(b))['applied'])
        self.assertEqual(self.store.db.execute('SELECT count(*)FROM argument_joint_reviews').fetchone()[0],1)
    def test_readonly_neighbor_current_failure_holds_old_and_new_joint_pass(self):
        from textbook_pdf.translation import _review_binding
        e=Element('neighbor','paragraph','Source context before argument.',1)
        self.book.elements.insert(0,e);self.book.pages[0]['element_ids'].insert(0,e.id);self.store.register(self.book,{})
        text='论证之前的只读上下文。';self.assertTrue(self.store.put_candidate(e.id,text,'self-authored')['accepted'])
        a=self.facade.prepare('self-argument');self.assertIn(e.id,a['packet']['payload']['readonly_context_ids'])
        self.assertTrue(self.facade.apply(a,self.good(a))['applied'])
        binding=_review_binding(e,text,self.book,{});binding.pop('element_id')
        self.assertTrue(self.store.apply_semantic_review(e.id,text,passed=False,problems=['Current context negation wrong'],provider='fake',**binding)['applied'])
        status=self.facade.status('self-argument');self.assertEqual(status['state'],'failed');self.assertIn(e.id,status['dependency_ids_with_current_failure'])
        b=self.facade.prepare('self-argument');self.assertFalse(self.facade.apply(b,self.good(b))['applied'])
        self.assertEqual(review_status(self.book,self.store)['argument_units']['counts']['passed'],0)
    def test_unknown_or_stale_failures_do_not_hold_unchanged_current_dependencies(self):
        from textbook_pdf.translation import _review_binding
        a=self.facade.prepare('self-argument');self.assertTrue(self.facade.apply(a,self.good(a))['applied'])
        e=self.book.element_map()['parent'];binding=_review_binding(e,self.texts[e.id],self.book,{})
        for field in ['source_hash','translation_hash','review_hash']:
            for value in [None,'0'*64]:
                issue={'code':'semantic_review_failed','severity':'error','element_id':e.id,
                  'source_hash':binding['source_hash'],'translation_hash':digest(self.texts[e.id]),'review_hash':binding['review_hash'],'detail':'Historical unrelated evidence'}
                issue[field]=value;self.store.record_issue(issue)
                self.assertEqual(self.facade.status('self-argument')['state'],'passed')
        b=self.facade.prepare('self-argument');self.assertTrue(self.facade.apply(b,self.good(b))['applied'])
        self.assertEqual(self.facade.status('self-argument')['state'],'passed')
    def test_other_store_glossary_revision_current_parent_failure_holds_existing_and_new_pass(self):
        from textbook_pdf.translation import _review_binding
        other=Store(self.db)
        try:
            glossary={'argument':'论证'};other.register(self.book,glossary)
            for e in self.book.elements:self.assertTrue(other.put_candidate(e.id,self.texts[e.id],'synthetic-current-rule')['accepted'])
            self.assertEqual(self.store.glossary,{})
            current=self.facade.prepare('self-argument');self.assertEqual(current['frozen_rules_readonly']['glossary'],glossary)
            self.assertTrue(self.facade.apply(current,self.good(current))['applied'])
            e=self.book.element_map()['parent'];binding=_review_binding(e,self.texts[e.id],self.book,glossary);binding.pop('element_id')
            self.assertTrue(other.apply_semantic_review(e.id,self.texts[e.id],passed=False,problems=['Current glossary parent failure'],provider='fake',**binding)['applied'])
            status=self.facade.status('self-argument');self.assertEqual(status['state'],'failed');self.assertIn(e.id,status['dependency_ids_with_current_failure'])
            current=self.facade.prepare('self-argument');result=self.facade.apply(current,self.good(current))
            self.assertFalse(result['applied']);self.assertIn('explicit_resolution',result['reason'])
            self.assertEqual(self.store.glossary,{})
        finally:other.close()
    def test_new_glossary_revision_does_not_reuse_old_failure_or_planned_packet(self):
        from textbook_pdf.translation import _review_binding
        other=Store(self.db)
        try:
            old=self.facade.prepare('self-argument');self.assertTrue(self.facade.apply(old,self.good(old))['applied'])
            e=self.book.element_map()['parent'];binding=_review_binding(e,self.texts[e.id],self.book,{});binding.pop('element_id')
            self.assertTrue(other.apply_semantic_review(e.id,self.texts[e.id],passed=False,problems=['Old revision failure'],provider='fake',**binding)['applied'])
            glossary={'argument':'论证'};other.register(self.book,glossary)
            for item in self.book.elements:self.assertTrue(other.put_candidate(item.id,self.texts[item.id],'synthetic-new-rule')['accepted'])
            self.assertTrue(self.facade.apply(old,self.good(old))['stale'])
            current=self.facade.prepare('self-argument');self.assertNotEqual(current['packet']['argument_review_hash'],old['packet']['argument_review_hash'])
            self.assertTrue(self.facade.apply(current,self.good(current))['applied'])
            self.assertEqual(self.facade.status('self-argument')['state'],'passed')
        finally:other.close()
    def test_failure_check_requires_matching_current_frozen_glossary_preimage(self):
        a=self.facade.prepare('self-argument')
        with self.facade._read():
            bad=deepcopy(a['current_review_preimage']);bad['glossary']={'argument':'different'}
            with self.assertRaisesRegex(ValueError,'preimage'):
                self.facade._current_individual_failures(a['packet'],current_review_preimage=bad)
            self.assertEqual(self.facade._current_individual_failures(a['packet'],current_review_preimage=a['current_review_preimage']),[])
    def native_dependency(self,role):
        a=deepcopy(next(iter(self.book.assets.values())));a.id='readonly-native';a.text='native label';self.book.assets[a.id]=a
        if role=='parent':
            e=self.book.element_map()['parent'];e.text='1. Check the mathematical object ⟦F:'+a.id+'⟧.';e.asset_ids=[a.id]
            self.texts[e.id]='1. 检查数学对象⟦F:'+a.id+'⟧。'
            self.book.profile[PROFILE_KEY]['self-argument']['parent_full_source_sha256']=digest(asdict(e))
        else:
            e=Element('native-neighbor','paragraph','Readonly mathematical object ⟦F:'+a.id+'⟧.',1,asset_ids=[a.id])
            self.book.elements.insert(0,e);self.book.pages[0]['element_ids'].insert(0,e.id);self.texts[e.id]='只读数学对象⟦F:'+a.id+'⟧。'
        self.store.register(self.book,{})
        for item in self.book.elements:self.assertTrue(self.store.put_candidate(item.id,self.texts[item.id],'synthetic-before')['accepted'])
        return e,a
    def current_native_failure(self,role,field):
        from textbook_pdf.translation import _review_binding,relevant_review_glossary
        e,a=self.native_dependency(role);other=Store(self.db)
        glossary={'relation':'关系'}if field=='text'else{}
        try:
            old=self.facade.prepare('self-argument');self.assertTrue(self.facade.apply(old,self.good(old))['applied'])
            current_book=deepcopy(self.book)
            if field=='text':current_book.assets[a.id].text='relation'
            else:current_book.assets[a.id].baseline+=.125
            self.assertFalse(source_contract_issues(current_book));checked_units(current_book)
            self.assertEqual(asdict(e),asdict(current_book.element_map()[e.id]))
            self.assertEqual(a.sha256,current_book.assets[a.id].sha256)
            other.register(current_book,glossary)
            for item in current_book.elements:self.assertTrue(other.put_candidate(item.id,self.texts[item.id],'synthetic-current')['accepted'])
            current=self.facade.prepare('self-argument');self.assertTrue(self.facade.apply(old,self.good(old))['stale'])
            self.assertTrue(self.facade.apply(current,self.good(current))['applied'])
            current= self.facade.prepare('self-argument')
            binding=_review_binding(current_book.element_map()[e.id],self.texts[e.id],current_book,glossary);binding.pop('element_id')
            stale=_review_hash(e,self.texts[e.id],self.book,glossary)
            self.assertNotEqual(binding['review_hash'],stale)
            if field=='text':
                self.assertEqual(relevant_review_glossary(e,self.book,glossary),{})
                self.assertEqual(relevant_review_glossary(e,current_book,glossary),glossary)
            self.assertTrue(other.apply_semantic_review(e.id,self.texts[e.id],passed=False,problems=['Known current native dependency failure'],provider='synthetic-only',**binding)['applied'])
            status=self.facade.status('self-argument');self.assertEqual(status['state'],'failed');self.assertIn(e.id,status['dependency_ids_with_current_failure'])
            fresh=self.facade.prepare('self-argument');result=self.facade.apply(fresh,self.good(fresh))
            self.assertFalse(result['applied']);self.assertFalse(result['stale']);self.assertIn('explicit_resolution',result['reason'])
            self.assertEqual(asdict(self.store.book.assets[a.id]),asdict(a))
        finally:other.close()
    def test_other_store_parent_baseline_current_failure(self):self.current_native_failure('parent','baseline')
    def test_other_store_readonly_context_baseline_current_failure(self):self.current_native_failure('neighbor','baseline')
    def test_other_store_parent_asset_text_changes_relevant_glossary(self):self.current_native_failure('parent','text')
    def test_other_store_readonly_context_asset_text_changes_relevant_glossary(self):self.current_native_failure('neighbor','text')
    def test_old_asset_failure_does_not_block_new_asset_revision(self):
        from textbook_pdf.translation import _review_binding
        e,a=self.native_dependency('parent');other=Store(self.db)
        try:
            old=self.facade.prepare('self-argument');old_binding=_review_binding(e,self.texts[e.id],self.book,{})
            current=deepcopy(self.book);current.assets[a.id].baseline+=.125;other.register(current,{})
            for item in current.elements:self.assertTrue(other.put_candidate(item.id,self.texts[item.id],'synthetic-new')['accepted'])
            self.store.record_issue({'code':'semantic_review_failed','severity':'error','element_id':e.id,'source_hash':e.source_hash,
              'translation_hash':digest(self.texts[e.id]),'review_hash':old_binding['review_hash'],'detail':'Old descriptor failure retained explicitly'})
            self.assertTrue(self.facade.apply(old,self.good(old))['stale'])
            fresh=self.facade.prepare('self-argument');self.assertTrue(self.facade.apply(fresh,self.good(fresh))['applied'])
            self.assertEqual(self.facade.status('self-argument')['state'],'passed')
            self.assertEqual(self.store.db.execute("SELECT count(*)FROM issues WHERE resolved=0 AND json_extract(json,'$.code')='semantic_review_failed'").fetchone()[0],1)
        finally:other.close()
    def test_complete_current_preimage_rejects_forged_fields(self):
        e,a=self.native_dependency('parent');planned=self.facade.prepare('self-argument');pre=planned['current_review_preimage']
        mutations=[lambda p:p['assets'][a.id].__setitem__('baseline',.3),lambda p:p['assets'][a.id].__setitem__('text','other'),
          lambda p:p['assets'][a.id]['metadata'].__setitem__('not_in_protected_descriptor','forged'),
          lambda p:p['elements'][e.id].__setitem__('chapter','other'),lambda p:p['glossary'].__setitem__('relation','关系'),
          lambda p:p['style_rules'].append('other'),lambda p:p['argument_registry']['self-argument'].__setitem__('source_facts_sha256','0'*64),
          lambda p:p.__setitem__('source_pdf_sha256','0'*64),lambda p:p.__setitem__('packet_sha256','0'*64)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                bad=deepcopy(pre);mutate(bad)
                with self.assertRaisesRegex(ValueError,'preimage'):
                    self.facade._current_individual_failures(planned['packet'],current_review_preimage=bad)
                forged=deepcopy(planned);forged['current_review_preimage']=bad
                self.assertTrue(self.facade.apply(forged,self.good(forged))['stale'])
    def test_current_dependency_review_never_reads_cached_session_assets(self):
        e,a=self.native_dependency('neighbor');old_assets=self.store.book.assets
        class NoAssetFallback(dict):
            def __getitem__(self,key):raise AssertionError('Cached session asset used')
        self.store.book.assets=NoAssetFallback()
        try:
            current=self.facade.prepare('self-argument');self.assertTrue(self.facade.apply(current,self.good(current))['applied'])
            self.assertEqual(self.facade.status('self-argument')['state'],'passed')
        finally:self.store.book.assets=old_assets
    def test_member_validation_uses_same_current_preimage(self):
        from textbook_pdf import models
        e,a=self.native_dependency('neighbor');current=self.facade.prepare('self-argument');seen=[];original=models.validate_translation
        def observe(element,text,book):
            seen.append(book);self.assertEqual(asdict(book.assets[a.id]),current['current_review_preimage']['assets'][a.id])
            self.assertIsNot(book,self.store.book);return original(element,text,book)
        with patch('textbook_pdf.models.validate_translation',side_effect=observe):self.assertTrue(self.facade.apply(current,self.good(current))['applied'])
        self.assertEqual(len(seen),3)
    def test_readonly_neighbor_source_and_translation_CAS(self):
        before=Element('neighbor','paragraph','Source context before argument.',1)
        self.book.elements.insert(0,before);self.book.pages[0]['element_ids'].insert(0,before.id)
        self.store.register(self.book,{})
        self.assertTrue(self.store.put_candidate(before.id,'论证之前的只读上下文。','self-authored')['accepted'])
        a=self.facade.prepare('self-argument');self.assertIn('neighbor',a['packet']['payload']['readonly_context_ids'])
        self.store.put_candidate(before.id,'修改后的只读上下文。','self-authored')
        self.assertTrue(self.facade.apply(a,self.good(a))['stale'])
        a=self.facade.prepare('self-argument')
        with self.store.db:
            e=deepcopy(before);e.text='Changed source context.'
            self.store.db.execute('UPDATE elements SET json=?WHERE id=?',(json.dumps(asdict(e)),e.id))
        self.assertTrue(self.facade.apply(a,self.good(a))['stale'])
    def test_actual_asset_changed_after_snapshot_rejected(self):
        planned=self.facade.prepare('self-argument');p=self.root/next(iter(self.book.assets.values())).path;old=p.read_bytes()
        try:p.write_bytes(old+b'\n% changed\n');self.assertFalse(self.facade.apply(planned,self.good(planned))['applied'])
        finally:p.write_bytes(old)
    def test_translation_parent_context_glossary_and_registered_source_CAS(self):
        for kind in('member','parent','glossary','registered_unit','ordered_source'):
            with self.subTest(kind=kind):
                a=self.facade.prepare('self-argument')
                with self.store.db:
                    if kind in('member','parent'):
                        eid='p1'if kind=='member'else'parent';self.store.db.execute('UPDATE translations SET text=text||? WHERE element_id=?',(' ',eid))
                    elif kind=='glossary':self.store.db.execute("UPDATE metadata SET value=? WHERE key='glossary'",(json.dumps({'switch':'开关'}),))
                    else:
                        row=json.loads(self.store.db.execute("SELECT value FROM metadata WHERE key='book'").fetchone()[0])
                        if kind=='registered_unit':row['profile'][PROFILE_KEY]['self-argument']['source_facts_sha256']='b'*64
                        else:row['elements'][1],row['elements'][2]=row['elements'][2],row['elements'][1]
                        self.store.db.execute("UPDATE metadata SET value=?WHERE key='book'",(json.dumps(row,ensure_ascii=False),))
                self.assertFalse(self.facade.apply(a,self.good(a))['applied'])
                self.store.register(self.book,{})
                for e in self.book.elements:self.store.put_candidate(e.id,self.texts[e.id],'restore-self-fixture')
    def test_fault_rollback_keeps_no_partial_verdict(self):
        a=self.facade.prepare('self-argument')
        def fail(stage):
            if stage=='after_insert_before_commit':raise RuntimeError('controlled fault')
        with self.assertRaises(RuntimeError):self.facade.apply(a,self.good(a),fault=fail)
        self.assertEqual(self.facade.status('self-argument')['state'],'pending')
    def test_response_envelope_rejects_patch_individual_partial_wrong_hash(self):
        a=self.facade.prepare('self-argument');good=self.good(a)
        bads=[]
        b=deepcopy(good);b['translations']={'p1':'改译'};bads.append(b)
        b=deepcopy(good);b['argument_review_hash']='a'*64;bads.append(b)
        b=deepcopy(good);b['argument_coverage']['ordered_member_ids'].reverse();bads.append(b)
        b=deepcopy(good);b['argument_coverage']['entire_argument_reviewed']=False;bads.append(b)
        b=deepcopy(good);b['argument_coverage']['negation_quantifiers_preserved']=False;bads.append(b)
        b=deepcopy(good);b['individual_passes']=[True,True,True];bads.append(b)
        for b in bads:
            with self.assertRaises(ValueError):validate_response(a['packet'],b)
    def config(self):
        p=self.root/'fake-provider.json';p.write_text(json.dumps({'default_provider':'fake','providers':{'fake':{'base_url':'http://invalid.local','api_key':'synthetic-only','model':'fake','max_attempts':2,'requests_per_minute':6000}}}),encoding='utf8');return p
    def test_fake_chat_complete_argument_and_raw_before_parse(self):
        calls=[]
        def chat(provider,messages):
            p=json.loads(messages[1]['content']);calls.append(p)
            self.assertEqual(p['payload']['ordered_member_ids'],['p1','p2','c']);self.assertEqual(p['payload']['readonly_parent_id'],'parent')
            return ('not-json'if len(calls)==1 else json.dumps(p['required_response'])),{'prompt_tokens':100,'completion_tokens':20}
        with patch('textbook_pdf.translation._chat',side_effect=chat):r=run_review(self.book,self.store,self.config(),'fake',argument_ids=['self-argument'],workers=1)
        self.assertEqual(r['requests'],2);self.assertEqual(r['argument_review_passed'],1)
        self.assertEqual(self.store.db.execute('SELECT count(*)FROM argument_joint_attempts').fetchone()[0],2)
        self.assertEqual(self.store.db.execute('SELECT raw_response FROM argument_joint_attempts ORDER BY id').fetchone()[0],'not-json')
    def test_fake_truncation_no_split_no_identical_retry(self):
        with patch('textbook_pdf.translation._chat',return_value=('partial',{'_truncated':True,'prompt_tokens':1,'completion_tokens':1}))as call:
            r=run_review(self.book,self.store,self.config(),'fake',argument_ids=['self-argument'],workers=1)
        self.assertEqual(call.call_count,1);self.assertEqual(r['argument_review_truncated_calls'],1);self.assertEqual(self.facade.status('self-argument')['state'],'pending')
    def test_every_actual_transport_attempt_observable_once_success_HTTP_timeout_invalid_truncation(self):
        from textbook_pdf import translation as t
        for scenario in ['HTTP429','HTTP402','timeout','invalid','truncated']:
            with self.subTest(scenario=scenario):
                before_usage=self.store.db.execute('SELECT count(*)FROM usage').fetchone()[0]
                before_attempts=self.store.db.execute("SELECT count(*)FROM argument_joint_attempts").fetchone()[0]if self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='argument_joint_attempts'").fetchone()else 0
                observed=[];original=self.store.record_usage;calls=[]
                def observe(task,provider,usage):observed.append(deepcopy(usage));return original(task,provider,usage)
                def chat(provider,messages):
                    packet=json.loads(messages[1]['content']);calls.append(packet)
                    if len(calls)==1:
                        if scenario=='HTTP429':raise t._HTTPFailure(429)
                        if scenario=='HTTP402':raise t._HTTPFailure(402)
                        if scenario=='timeout':raise ConnectionError('synthetic timeout')
                        if scenario=='invalid':return 'not-json',{'prompt_tokens':10,'completion_tokens':5}
                        if scenario=='truncated':return 'partial',{'prompt_tokens':10,'completion_tokens':5,'_truncated':True}
                    return json.dumps(packet['required_response']),{'prompt_tokens':10,'completion_tokens':5}
                self.store.record_usage=observe
                try:
                    with patch('textbook_pdf.translation._chat',side_effect=chat),patch('textbook_pdf.translation._Limiter.throttle'):
                        report=run_review(self.book,self.store,self.config(),'fake',argument_ids=['self-argument'],workers=1)
                finally:self.store.record_usage=original
                expected=1 if scenario in ['HTTP402','truncated']else 2
                self.assertEqual(report['worker_failures'],0,report)
                self.assertEqual(len(calls),expected);self.assertEqual(report['requests'],expected)
                self.assertEqual(len(observed),expected)
                self.assertEqual(self.store.db.execute('SELECT count(*)FROM usage').fetchone()[0]-before_usage,expected)
                self.assertEqual(self.store.db.execute('SELECT count(*)FROM argument_joint_attempts').fetchone()[0]-before_attempts,expected)
                if scenario in ['HTTP429','HTTP402','timeout']:
                    self.assertTrue(observed[0]['request_failed']);self.assertFalse(observed[0]['usage_known'])
                    self.assertFalse(report['cost_estimate_complete']);self.assertEqual(report['usage_missing_calls'],1)
                else:self.assertTrue(all(r['usage_known']for r in observed))
    def test_cancelled_before_transport_has_no_attempt_or_usage(self):
        from textbook_pdf.translation import _Cancelled
        with patch('textbook_pdf.translation._Limiter.acquire',side_effect=_Cancelled),patch('textbook_pdf.translation._chat')as chat:
            result=run_review(self.book,self.store,self.config(),'fake',argument_ids=['self-argument'],workers=1)
        chat.assert_not_called();self.assertEqual(result['requests'],0);self.assertTrue(result['cancelled'])
        self.assertEqual(self.store.db.execute('SELECT count(*)FROM usage').fetchone()[0],0)
        self.assertFalse(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name='argument_joint_attempts'").fetchone())
    def test_fake_reply_overtaken_by_new_translation(self):
        def chat(provider,messages):
            p=json.loads(messages[1]['content'])
            with self.store.db:self.store.db.execute('UPDATE translations SET text=text||?WHERE element_id=?',(' ', 'p2'))
            return json.dumps(p['required_response']),{'prompt_tokens':1,'completion_tokens':1}
        with patch('textbook_pdf.translation._chat',side_effect=chat):r=run_review(self.book,self.store,self.config(),'fake',argument_ids=['self-argument'],workers=1)
        self.assertEqual(r['argument_review_passed'],0);self.assertEqual(r['argument_stale_results'],1)
    def test_zero_limit_never_calls_chat_or_creates_table(self):
        with patch('textbook_pdf.translation._chat',side_effect=AssertionError('not authorized in tests'))as call:
            run_review(self.book,self.store,self.config(),'fake',max_arguments=0,max_elements=0,workers=1)
        self.assertEqual(call.call_count,0)
    def test_argument_and_legacy_prose_units_share_scheduler_without_rule_relaxation(self):
        left=Element('legacy-left','paragraph','A note from one sensor',2)
        right=Element('legacy-right','paragraph','to another becomes a shared message.',3)
        self.book.elements.extend([left,right]);self.book.page_count=3;self.book.selected_pages=[1,2,3]
        uid='legacy-prose';positions={e.id:i for i,e in enumerate(self.book.elements)}
        self.book.profile['semantic_review_units']={uid:{'schema':'semantic-unit-review-v1','id':uid,'role':'body_prose',
          'source_pdf_sha256':self.book.source_sha256,'member_ids':[left.id,right.id],'context_ids':[],
          'intervening_header_ids':[],'adjacency_chain':[left.id,right.id],'source_ordinals':positions,
          'member_source_full_sha256':{e.id:digest(asdict(e))for e in(left,right)},'context_source_full_sha256':{},
          'source_visual_evidence':[{'self_authored':True,'boundary':'sensor/to another'}],
          'boundary':{'id':'legacy-boundary','left_suffix':'from one sensor','right_prefix':'to another','source_join':'space'}}}
        self.store.register(self.book,{})
        for e,text in[(left,'一条来自传感器的备注'),(right,'传递给另一传感器，成为共享消息。')]:
            self.assertTrue(self.store.put_candidate(e.id,text,'self-authored')['accepted'])
        calls=[]
        def chat(provider,messages):
            packet=json.loads(messages[1]['content']);calls.append(packet)
            return json.dumps(packet['required_response']),{'prompt_tokens':10,'completion_tokens':5}
        with patch('textbook_pdf.translation._chat',side_effect=chat):
            result=run_review(self.book,self.store,self.config(),'fake',max_elements=0,workers=2)
        self.assertEqual(result['worker_failures'],0,result)
        self.assertEqual(result['requests'],2);self.assertEqual(result['argument_review_passed'],1)
        self.assertEqual(result['unit_review_passed'],1);self.assertEqual(result['unit_scheduled'],1)
        self.assertEqual(result['semantic_units_after']['counts']['passed'],1)
        self.assertEqual({p['payload']['schema']for p in calls},{'source-anchored-argument-joint-review-v1','semantic-unit-review-v1'})

if __name__=='__main__':unittest.main()
