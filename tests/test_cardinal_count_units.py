from copy import deepcopy
import json,sys,unittest
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path.insert(0,str(ROOT))
from textbook_pdf.models import Element,digest
from textbook_pdf.numeric_policy import make_policy as old_make,numeric_policy_fields as old_fields,implementation_sha256 as old_sha
from textbook_pdf import cardinal_counts as C
from textbook_pdf import numeric_policy_dispatch as D

class CardinalCountsTests(unittest.TestCase):
    def setUp(self):
        self.e=Element('own-cardinal','exercise','15. How many bit strings of length eight do not contain six consecutive 0s?',1,group_id='own-cardinal',chapter='own-chapter')
        self.e.metadata['numeric_policy']=C.make_policy(self.e)
    def test_current_factory_explicitly_cannot_handle_source(self):
        e=deepcopy(self.e);del e.metadata['numeric_policy']
        with self.assertRaisesRegex(ValueError,'No explicit Arabic'):old_make(e)
    def test_arabic_chinese_and_english_cardinal_equivalence(self):
        for a in ['8','八','eight']:
            for b in ['6','六','six']:
                with self.subTest(a=a,b=b):self.assertTrue(C.check(self.e,f'15. 长度为{a}且不包含{b}个连续0的位串有多少个？')['equivalent'])
    def test_consecutive_alternative(self):self.assertTrue(C.check(self.e,'15. 长度为八且不含连续六个0的位串有多少个？')['equivalent'])
    def test_wrong_values_or_roles_are_errors(self):
        for t in ['15. 长度为7且不包含6个连续0的位串有多少个？','15. 长度为eight且不包含five个连续0的位串有多少个？','15. 长度为六且不包含八个连续0的位串有多少个？','15. 长度为七且不包含五个连续0的位串有多少个？']:
            with self.subTest(t=t):self.assertFalse(C.check(self.e,t)['equivalent'])
    def test_question_label_bit_and_extra_number_still_legacy_checked(self):
        for t in ['16. 长度为8且不包含6个连续0的位串有多少个？','15. 长度为8且不包含6个连续1的位串有多少个？','15. 长度为8且不包含6个连续0的位串有多少个？另有2种。']:
            with self.subTest(t=t):self.assertFalse(C.check(self.e,t)['equivalent'])
    def test_ambiguous_math_units_wrappers_or_partial_tokens_closed(self):
        for raw in ['-8','−8','+8','＋8','8²','8₂','8%','8‰','8万','8.0','08','8e2','8/1','8x','eight hundred','(8)','（8）']:
            with self.subTest(raw=raw):self.assertFalse(C.check(self.e,f'15. 长度为{raw}且不包含6个连续0的位串有多少个？')['equivalent'])
    def test_ambiguous_run_closed(self):
        for raw in ['-6','六万','6.0','6²','6%','6/1','(6)','six hundred']:
            with self.subTest(raw=raw):self.assertFalse(C.check(self.e,f'15. 长度为8且不包含{raw}个连续0的位串有多少个？')['equivalent'])
    def test_dropped_negation_and_duplicate_role_closed(self):
        for t in ['15. 长度为8且包含6个连续0的位串有多少个？','15. 长度为8且不包含6个连续0的位串有多少个？长度为8的位串。']:
            with self.subTest(t=t):self.assertFalse(C.check(self.e,t)['equivalent'])
    def test_full_source_identity_strict(self):
        for field,val in [('id','other'),('page',608),('group_id','other'),('kind','paragraph')]:
            e=deepcopy(self.e);setattr(e,field,val)
            with self.subTest(field=field):self.assertFalse(C.check(e,'15. 长度为8且不包含6个连续0的位串有多少个？')['equivalent'])
    def test_text_style_or_context_semantic_metadata_change_stale(self):
        for path in ['text','style','metadata']:
            e=deepcopy(self.e)
            if path=='text':e.text=e.text.replace('six','five')
            if path=='style':e.style['column']=9
            if path=='metadata':e.metadata['semantic_dependency']='new'
            self.assertFalse(C.check(e,'15. 长度为8且不包含6个连续0的位串有多少个？')['equivalent'])
    def test_layout_only_display_association_does_not_rebind(self):
        e=deepcopy(self.e);e.metadata['display_asset_ids']=['layout-only']
        self.assertEqual(C.policy_fields(self.e),C.policy_fields(e))
    def test_no_optin_defaults_are_exact_legacy_fields(self):
        e=deepcopy(self.e);del e.metadata['numeric_policy']
        self.assertEqual(old_fields(e),C.proposed_facade_fields(e));self.assertEqual(digest(old_fields(e)),digest(C.proposed_facade_fields(e)))
    def test_old_quantity_schema_keeps_exact_old_implementation_binding(self):
        e=Element('own-e','paragraph','There are 2.9 million vertices.',1)
        e.metadata['numeric_policy']=old_make(e)
        self.assertEqual(e.metadata['numeric_policy']['implementation_sha256'],old_sha())
        self.assertEqual(old_fields(e),C.proposed_facade_fields(e))
    def test_unchanged_legacy_module_does_not_accidentally_accept_new_schema(self):
        with self.assertRaises(ValueError):old_fields(self.e)
    def test_old_policy_or_old_member_not_equivalent(self):
        e=deepcopy(self.e);e.metadata['numeric_policy']['source_events'][0]['value']=7
        self.assertFalse(C.check(e,'15. 长度为8且不包含6个连续0的位串有多少个？')['equivalent'])
    def test_frozen_task_with_or_without_new_schema_cannot_mix(self):
        fields=D.numeric_policy_fields(self.e)
        self.assertTrue(D.frozen_policy_matches(fields,self.e))
        self.assertFalse(D.frozen_policy_matches({},self.e))
        bad=deepcopy(fields);bad['numeric_policy']['source_record_sha256']='0'*64
        self.assertFalse(D.frozen_policy_matches(bad,self.e))
        old=deepcopy(self.e);del old.metadata['numeric_policy']
        self.assertFalse(D.frozen_policy_matches(fields,old))
    def test_dispatch_and_implementation_dependencies_stale(self):
        for key in ['implementation_sha256','schema_dispatch_sha256','legacy_numeric_policy_sha256']:
            e=deepcopy(self.e);e.metadata['numeric_policy'][key]='0'*64
            with self.subTest(key=key):self.assertFalse(C.check(e,'15. 长度为8且不包含6个连续0的位串有多少个？')['equivalent'])
    def test_facade_returns_hard_error_for_bad_counts(self):
        self.assertEqual(D.policy_number_issues(self.e,'15. 长度为8且不包含6个连续0的位串有多少个？'),[])
        self.assertEqual(D.policy_number_issues(self.e,'15. 长度为7且不包含5个连续0的位串有多少个？')[0]['code'],'number_mismatch')

if __name__=='__main__':unittest.main(verbosity=2)
