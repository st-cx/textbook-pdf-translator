import contextlib
import io
import unittest
from unittest.mock import patch
from textbook_pdf.cli import main


class AutoCliTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
