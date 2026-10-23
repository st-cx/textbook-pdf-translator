"""A file changing between two references must be rejected in the same packet."""
from dataclasses import asdict
from pathlib import Path
import unittest
from unittest.mock import patch
import test_semantic_units as fixtures
from textbook_pdf.models import digest,file_sha256

class RepeatedAssetReadTests(unittest.TestCase):
 def setUp(self):
  self.fixture=fixtures.UnitStoreTests();self.fixture.setUp();c=self.fixture
  after=c.elements[-1];after.text='The following paragraph ⟦F:a⟧.';after.asset_ids=['a']
  c.u['context_source_full_sha256']['after']=digest(asdict(after))
  c.s.register(c.b,{})
  self.assertTrue(c.s.put_candidate('after','后续段落⟦F:a⟧。','synthetic')['accepted'])
 def tearDown(self):self.fixture.tearDown()
 def test_unchanged_shared_file_is_rechecked_for_each_reference(self):
  calls=[];target=(self.fixture.root/'native.bin').resolve()
  def check(path):
   if Path(path).resolve()==target:calls.append(path)
   return file_sha256(path)
  with patch('textbook_pdf.models.file_sha256',side_effect=check):self.fixture.facade.packet(self.fixture.u['id'])
  self.assertEqual(len(calls),2)
 def test_mutation_before_second_reference_rejected(self):
  calls=[];target=(self.fixture.root/'native.bin').resolve()
  def check(path):
   if Path(path).resolve()==target:
    calls.append(path)
    if len(calls)==2:target.write_bytes(b'changed between repeated references')
   return file_sha256(path)
  with patch('textbook_pdf.models.file_sha256',side_effect=check):
   with self.assertRaisesRegex(ValueError,'actual file changed'):self.fixture.facade.packet(self.fixture.u['id'])
  self.assertEqual(len(calls),2)

if __name__=='__main__':unittest.main()
