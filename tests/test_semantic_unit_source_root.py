"""Unit review must read the registered source assets across split job folders."""
from pathlib import Path
import shutil
import unittest

import test_semantic_units as fixtures
from textbook_pdf.semantic_units import UnitReviewStore


class SourceRootAssetTests(unittest.TestCase):
    def setUp(self):
        fixtures.UnitStoreTests.setUp(self)
        self.source_root = self.root / 'source-generation'
        self.source_root.mkdir()
        shutil.move(str(self.root / 'native.bin'), str(self.source_root / 'native.bin'))
        self.b.profile['job_dir'] = str(self.source_root)
        self.s.register(self.b, {})
        self.facade = UnitReviewStore(self.s)

    def tearDown(self):
        fixtures.UnitStoreTests.tearDown(self)

    def test_registered_source_folder_is_separate_from_database_folder(self):
        self.assertFalse((self.root / 'native.bin').exists())
        packet = self.facade.packet(self.u['id'])
        self.assertIn('a', packet['payload']['elements']['before']['actual_asset_bindings'])

    def test_registered_source_precedes_stale_job_shadow(self):
        (self.root / 'native.bin').write_bytes(b'wrong-job-shadow')
        self.assertEqual(self.facade.status(self.u['id'])['state'], 'pending')

    def test_bad_source_bytes_do_not_fall_back_to_good_job_shadow(self):
        shutil.copyfile(self.source_root / 'native.bin', self.root / 'native.bin')
        (self.source_root / 'native.bin').write_bytes(b'changed-source')
        with self.assertRaisesRegex(ValueError, 'actual file changed'):
            self.facade.packet(self.u['id'])

    def test_unregistered_local_source_root_is_rejected(self):
        self.s.book.profile['job_dir'] = str(self.root)
        with self.assertRaisesRegex(ValueError, 'source root'):
            self.facade.packet(self.u['id'])


if __name__ == '__main__':
    unittest.main()
