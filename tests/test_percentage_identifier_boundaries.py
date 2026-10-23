"""Direct rate grammar: attached scripts/identifiers are never prose tails."""
from pathlib import Path
from unittest import TestCase
import json,uuid,tempfile
from textbook_pdf.models import Book,Element,validate_translation
from textbook_pdf.store import Store
from textbook_pdf.percentage_policy import FIELD,make_percentage_policy,percentage_policy_fields
from textbook_pdf import _percentage_units as parser
HERE=Path(__file__).resolve().parent
def element(text):return Element('direct-rate','paragraph',text,1,[0,0,200,20],group_id='direct-rate')
def optin(text):
 e=element(text);e.metadata[FIELD]=make_percentage_policy(e);return e
def book(e):return Book('','synthetic-direct-rates','Self-authored rate boundary',1,[1],elements=[e])
def codes(e,t):return{x['code']for x in validate_translation(e,t,book(e))}

class DirectPercentageIdentifierBoundaryTests(TestCase):
 def test_exact_root_six_v3_false_Store_acceptances_now_rejected(self):
  # Exact root-discovered strings; all synthetic, no local fixture dependency.
  cases=[{'source':'A rate is 2%.','target':x}for x in ('比率为2%²。','比率为2%₂。','比率为−x·2%。','比率为2%½。','比率为2%e2。','比率为2%x。')]
  self.assertEqual(len(cases),6)
  for c in cases:
   with self.subTest(target=c['target']):
    e=optin(c['source']);path=Path(tempfile.gettempdir())/('direct-rate-fixture-'+uuid.uuid4().hex);path.mkdir()
    with Store(path/'tasks.db')as s:
     s.register(book(e),{});r=s.put_candidate(e.id,c['target'],'root-v3-exact-regression')
     self.assertFalse(r['accepted']);self.assertNotIn(e.id,s.translations())
     self.assertIn('percentage_unit_mismatch',{x['code']for x in r['issues']})
     self.assertEqual(s.db.execute('SELECT count(*)FROM candidates').fetchone()[0],1)

 def test_unwrapped_or_wrapped_scripts_fractions_and_identifiers_reject(self):
  e=optin('A rate is 2%.')
  for raw in ('2%²','2%₂','2%½','2%e2','2%x','2%𝑥','2%α','2%\u0304','(2%)²','［2%］e2','2% annually2','2% x','2% α','2% ²','2% ½'):
   with self.subTest(raw=raw):self.assertIn('percentage_unit_mismatch',codes(e,'比率为'+raw+'。'))

 def test_implicit_product_prefix_and_suffix_cannot_compile_policy(self):
  for raw in ('−x·2%','x⋅2%','x•2%','x∙2%','x・2%','x 2%','α 2%','𝑥 2%','2%·x','2% ⋅ x','2% x','2% α','−x․2%','−`2%`','2%`x`'):
   with self.subTest(raw=raw),self.assertRaises(ValueError):make_percentage_policy(element('A rate is '+raw+'.'))

 def test_space_separated_English_prose_CJK_and_punctuation_are_legal(self):
  e=optin('A rate is 2%.')
  for t in ('比率为2% annually。','比率为2% a year。','增长率为2%每年。','增长率: (2%)。','比率为2%，其后说明。','比率为2%；其后说明。','比率为（2%）。'):
   with self.subTest(target=t):self.assertEqual(codes(e,t),set())

 def test_separate_complete_events_count_and_normal_signed_rates_are_preserved(self):
  self.assertFalse(parser.compare('1% 2%','1%')['equivalent'])
  self.assertFalse(parser.compare('1%','1% 1%')['equivalent'])
  for s,t in [('A rate is -2%.','比率为−2%每年。'),('A rate is +2%.','比率为（+2%）。')]:
   with self.subTest(source=s):self.assertEqual(codes(optin(s),t),set())

 def test_v3_policy_does_not_inherit_v4_acceptance_or_task_identity(self):
  e=optin('A rate is 2%.');e.metadata[FIELD]['schema']='explicit-arabic-percent-units-v3'
  with self.assertRaises(ValueError):percentage_policy_fields(e)
