"""Pure future-unit binding compatibility; no unit Store/API integration claim."""
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
import sys,unittest
sys.dont_write_bytecode=True
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent
sys.path[:0]=[str(HERE),str(ROOT)]
from textbook_pdf.models import Element,Book,digest
from textbook_pdf.store import translation_rule_hash
from textbook_pdf.numeric_policy import make_policy
from textbook_pdf.semantic_units import build_packet,validate_response,SCHEMA

def fixture():
    left=Element('left','paragraph','More than 2.9 million vertices are linked',1)
    right=Element('right','paragraph','in this network.',2)
    b=Book('self-authored-unit.pdf','own-unit-source','Synthetic',2,[1,2],elements=[left,right])
    def row(e,text):
        rule=translation_rule_hash(b,e,{})
        return {'element':asdict(e),'source_hash':e.source_hash,'rule_hash':rule,
            'translation':{'text':text,'source_hash':e.source_hash,'rule_hash':rule,'status':'accepted','translation_hash':digest(text)},
            'protected_assets':{},'actual_asset_bindings':{}}
    snapshot={'source_pdf_sha256':b.source_sha256,'glossary_hash':digest({}),
        'elements':{'left':row(left,'超过2.9百万个顶点相互连接'),'right':row(right,'在这个网络中。')}}
    definition={'schema':SCHEMA,'id':'self-authored-pure-unit','role':'body_prose','source_pdf_sha256':b.source_sha256,
        'member_ids':['left','right'],'context_ids':[],'intervening_header_ids':[],
        'adjacency_chain':['left','right'],'source_ordinals':{'left':0,'right':1},
        'member_source_full_sha256':{e.id:digest(asdict(e))for e in b.elements},'context_source_full_sha256':{},
        'source_visual_evidence':[{'self_authored':True}],
        'boundary':{'id':'boundary','left_suffix':'are linked','right_prefix':'in this network','source_join':'space'},
        'authorized_issue_resolutions':[]}
    return b,left,definition,snapshot

class FuturePureUnitPolicyBindingTests(unittest.TestCase):
    def test_policy_changes_full_member_identity_even_source_hash_same(self):
        b,e,d,s=fixture();old=e.source_hash;build_packet(d,s);e.metadata['numeric_policy']=make_policy(e)
        self.assertEqual(e.source_hash,old);s['elements']['left']['element']=asdict(e)
        with self.assertRaises(ValueError):build_packet(d,s)
    def test_rebuilt_definition_does_not_authorize_old_rule_translation(self):
        b,e,d,s=fixture();e.metadata['numeric_policy']=make_policy(e)
        d['member_source_full_sha256']['left']=digest(asdict(e));s['elements']['left']['element']=asdict(e)
        s['elements']['left']['rule_hash']=translation_rule_hash(b,e,{})
        with self.assertRaises(ValueError):build_packet(d,s)
    def test_new_policy_bound_unit_rejects_old_unit_verdict(self):
        b,e,d,s=fixture();old=build_packet(d,s);response=deepcopy(old['required_response'])
        e.metadata['numeric_policy']=make_policy(e);d['member_source_full_sha256']['left']=digest(asdict(e))
        r=s['elements']['left'];r['element']=asdict(e);r['rule_hash']=translation_rule_hash(b,e,{})
        r['translation'].update(rule_hash=r['rule_hash'],text='超过290万个顶点相互连接',translation_hash=digest('超过290万个顶点相互连接'))
        new=build_packet(d,s)
        self.assertNotEqual(new['unit_review_hash'],old['unit_review_hash'])
        self.assertIn('numeric_policy',new['payload']['elements']['left']['element']['metadata'])
        with self.assertRaises(ValueError):validate_response(new,response)

if __name__=='__main__':unittest.main()
