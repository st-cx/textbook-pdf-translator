"""Bounded complete rate tokens: actual reviewer regressions and signed controls."""
from pathlib import Path
from unittest import TestCase
import json,tempfile,uuid
from textbook_pdf.models import Book,Element,validate_translation
from textbook_pdf.store import Store
from textbook_pdf.percentage_policy import FIELD,make_percentage_policy,percentage_policy_fields
from textbook_pdf import _percentage_units as parser

HERE=Path(__file__).resolve().parent
def element(text):return Element('wrapped-rate','paragraph',text,1,[0,0,200,20],group_id='wrapped-rate')
def optin(text):
 e=element(text);e.metadata[FIELD]=make_percentage_policy(e);return e
def book(e):return Book('','synthetic-wrapped-rates','Self-authored wrappers',1,[1],elements=[e])
def codes(e,target):return {x['code']for x in validate_translation(e,target,book(e))}

class WrappedPercentageTokenTests(TestCase):
 def test_exact_four_v2_reviewer_Store_acceptances_now_rejected(self):
  # Exact independently found strings, self-authored synthetic material.
  cases=[{'source':'A rate is 2%.','target':x} for x in ('比率为−（2%）。','比率为-( (2%))。','比率为-(（2%）)。','比率为-［2%］。')]
  self.assertEqual(len(cases),4)
  for case in cases:
   with self.subTest(target=case['target']):
    e=optin(case['source']);p=Path(tempfile.gettempdir())/('wrapped-rate-fixture-'+uuid.uuid4().hex);p.mkdir()
    with Store(p/'tasks.db')as s:
     s.register(book(e),{});r=s.put_candidate(e.id,case['target'],'exact-v2-reviewer-negative')
     self.assertFalse(r['accepted']);self.assertNotIn(e.id,s.translations())
     self.assertIn('percentage_unit_mismatch',{i['code']for i in r['issues']})
     self.assertEqual(s.db.execute('SELECT count(*)FROM candidates').fetchone()[0],1)

 def test_all_supported_wrapper_kinds_reject_every_outer_minus_and_plus(self):
  e=optin('A rate is 2%.')
  for left,right in parser.PAIRS.items():
   for sign in ('-','−','－','+','＋'):
    for space in ('',' ','\t'):
     target='比率为'+sign+space+left+' 2% '+right+'。'
     with self.subTest(target=target):self.assertIn('percentage_unit_mismatch',codes(e,target))

 def test_nested_mixed_wrapper_and_whitespace_combinations_cannot_drop_sign(self):
  e=optin('A rate is 2%.')
  for a,b in parser.PAIRS.items():
   for c,d in parser.PAIRS.items():
    target='比率为- '+a+' '+c+'2%'+d+' '+b+'。'
    with self.subTest(target=target):self.assertIn('percentage_unit_mismatch',codes(e,target))

 def test_signed_decimal_inside_balanced_nested_wrappers_is_legal(self):
  for source,target in [('A rate is -2%.','比率为（(-2%)）。'),
                        ('A rate is −2%.','比率为[ （−2%） ]。'),
                        ('A rate is +2%.','比率为（(+2%)）。'),
                        ('A rate is +2%.','比率为【2%】。'),
                        ('A rate is -0.125 per cent.','比率为（−0.125％）。')]:
   with self.subTest(target=target):self.assertEqual(codes(optin(source),target),set())

 def test_prose_word_colon_and_fullwidth_parentheses_are_legal(self):
  e=optin('A rate is 2%.')
  for target in ('增长率: (2%)。','增长率：（2%）。','增长率为( (2%))。','Growth: (2%)，即增长率。'):
   with self.subTest(target=target):self.assertEqual(codes(e,target),set())

 def test_composite_ratio_fraction_with_wrappers_cannot_compile(self):
  for raw in ('1/（2%）','1/( (2%))','1∕［2%］','百分之1/2','1: (2%)','x:（2%）','x/(2%)','f(2%)','x (2%)'):
   with self.subTest(raw=raw),self.assertRaises(ValueError):make_percentage_policy(element('A rate is '+raw+'.'))

 def test_right_side_composites_through_wrappers_are_not_partial_events(self):
  for raw in ('（2%）/3','[(2%)]×3','（2%）:3','[2%]+3','[2%]−3','[2%]3','（2%）万亿'):
   with self.subTest(raw=raw),self.assertRaises(ValueError):make_percentage_policy(element('A rate is '+raw+'.'))

 def test_mismatched_unbalanced_unknown_and_excess_wrappers_are_unverifiable(self):
  for raw in ('(2%','2%)','[(2%)]）','(（2%）]','⌈2%⌉','|2%|','「2%」','('*17+'2%'+')'*17):
   with self.subTest(raw=raw),self.assertRaises(ValueError):make_percentage_policy(element('A rate is '+raw+'.'))

 def test_outer_signed_source_is_explicitly_unsupported_not_reinterpreted(self):
  for raw in ('−（2%）','-( (2%))','+ [2%]','- {2%}'):
   with self.subTest(raw=raw),self.assertRaises(ValueError):make_percentage_policy(element('A rate is '+raw+'.'))

 def test_old_v2_bound_policy_is_stale_under_v3_even_when_decimal_same(self):
  e=optin('A rate is 2%.');e.metadata[FIELD]['schema']='explicit-arabic-percent-units-v2'
  with self.assertRaises(ValueError):percentage_policy_fields(e)

 def test_equivalent_order_change_remains_warning_and_independent_review(self):
  result=parser.compare('Rates are 2% and 3%.','比率分别为3%和2%。')
  self.assertTrue(result['equivalent']);self.assertTrue(result['order_review_required']);self.assertTrue(result['independent_semantic_review_required'])

 def test_wrapper_scanner_bounded_without_unbounded_recursion(self):
  e=optin('A rate is 2%.');target='比率为'+('('*10000)+'2%'+(')'*10000)+'。'
  self.assertIn('percentage_unit_mismatch',codes(e,target))

 def test_other_explicit_unknown_operator_or_hidden_sign_boundaries_close(self):
  e=optin('A rate is 2%.')
  for raw in ('±(2%)','∓（2%）','¬[2%]','−\u200b(2%)','−“2%”','𝑓(2%)','α2%','负2%','負（2%）','negative (2%)'):
   with self.subTest(raw=raw):self.assertIn('percentage_unit_mismatch',codes(e,'比率为'+raw+'。'))
