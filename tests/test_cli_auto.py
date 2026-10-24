import contextlib
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid
from textbook_pdf.cli import main
from textbook_pdf.models import Book, Element, file_sha256
from textbook_pdf.store import Store
from textbook_pdf.translation import make_tasks


class AutoCliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / ("_tmp_cli_auto_" + uuid.uuid4().hex)
        self.root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_automatic_options_reach_real_options_type(self):
        with patch('textbook_pdf.autopilot.run_job', return_value={'state': 'finished'}) as run, contextlib.redirect_stdout(io.StringIO()):
            code = main(['--auto', '--out', 'unused-job', '--provider', 'translate', '--review-provider', 'review',
                         '--workers', '8', '--target-chars', '900', '--glossary', 'terms.json'])
        self.assertEqual(code, 0)
        option = run.call_args.args[0]
        self.assertEqual(option.workers, 8)
        self.assertEqual(option.target_chars, 900)
        self.assertEqual(option.glossary_path, 'terms.json')
        self.assertTrue(option.review)

    def test_draft_only_never_requests_translation_or_review(self):
        with patch('textbook_pdf.autopilot.run_job', return_value={'state': 'budget_reached'}) as run, contextlib.redirect_stdout(io.StringIO()):
            main(['--auto', '--out', 'unused-job', '--draft-only', '--review-provider', 'review'])
        option = run.call_args.args[0]
        self.assertEqual(option.max_tasks, 0)
        self.assertFalse(option.review)

    def test_estimate_cannot_accidentally_trigger_auto_calls(self):
        with patch('textbook_pdf.autopilot.run_job') as run, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main(['--auto', '--estimate', '--out', 'unused-job'])
        run.assert_not_called()

    def test_provider_failure_is_not_reported_as_success(self):
        with patch('textbook_pdf.autopilot.run_job', return_value={'state': 'blocked'}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--auto', '--out', 'unused-job']), 2)

    def test_auto_defaults_to_32_with_batch_following_workers(self):
        with patch('textbook_pdf.autopilot.run_job', return_value={'state': 'finished'}) as run, contextlib.redirect_stdout(io.StringIO()):
            main(['--auto', '--out', 'unused-job'])
        option = run.call_args.args[0]
        self.assertEqual(option.workers, 32)
        self.assertIsNone(option.batch_size)

    def test_auto_respects_manual_worker_and_explicit_batch(self):
        for workers in (4, 16):
            with self.subTest(workers=workers), patch('textbook_pdf.autopilot.run_job', return_value={'state': 'finished'}) as run, contextlib.redirect_stdout(io.StringIO()):
                main(['--auto', '--out', 'unused-job', '--workers', str(workers), '--batch-size', '3'])
            option = run.call_args.args[0]
            self.assertEqual(option.workers, workers)
            self.assertEqual(option.batch_size, 3)

    def test_nonauto_translation_retains_default_four_workers(self):
        pdf = self.root / 'fixture.pdf'
        pdf.write_bytes(b'original fixture; no rendering or HTTP in this test')
        book = Book(str(pdf), file_sha256(pdf), 'Fixture', 1, [1],
                    elements=[Element('e', 'paragraph', 'One case.', 1)])
        job = self.root / 'job'
        job.mkdir()
        book.save(job / 'book.json')
        with Store(job / 'tasks.db') as store:
            store.register(book, {})
            store.add_tasks(make_tasks(book, {}))
        with patch('textbook_pdf.translation.run_translation', return_value={}) as run, contextlib.redirect_stdout(io.StringIO()):
            code = main(['--stage', 'translate', '--out', str(job), '--no-legacy-import'])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs['workers'], 4)

    def test_worker_validation_and_auto_help(self):
        with contextlib.redirect_stderr(io.StringIO()):
            for value in ('0', '33'):
                with self.assertRaises(SystemExit):
                    main(['--auto', '--workers', value])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            main(['--help'])
        self.assertIn('默认跟随并发数', output.getvalue())


if __name__ == '__main__':
    unittest.main()
