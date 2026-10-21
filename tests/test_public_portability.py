"""Public-source credential import is explicit, read-only and never logs keys."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]


class PublicLegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parent / "_tmp_public_portability" / uuid.uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.directory)

    def run_import(self, database):
        destination = self.directory / "local-config.json"
        environment = dict(os.environ, TEXTBOOK_CCSWITCH_DB=str(database), PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", "-c", "import sys; import legacy_translate_pdf as m; m.import_ccswitch(sys.argv[1])", str(destination)],
            cwd=ROOT, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        return result, destination

    def test_missing_database_is_not_created_and_no_config_is_written(self):
        database = self.directory / "missing.db"
        result, destination = self.run_import(database)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(database.exists())
        self.assertFalse(destination.exists())
        self.assertIn("TEXTBOOK_CCSWITCH_DB", result.stderr)

    def test_explicit_fixture_import_is_readonly_and_does_not_log_any_key_prefix(self):
        database = self.directory / "providers.db"
        key = "fixture-not-a-real-credential-123456789"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE providers(name TEXT, settings_config TEXT)")
            connection.execute("INSERT INTO providers VALUES(?,?)", ("deepseek synthetic", json.dumps({"api_key": key, "models": [{"id": "synthetic-model"}]})))
        connection.close()
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        result, destination = self.run_import(database)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
        data = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(data["providers"]["deepseek"]["api_key"], key)
        output = result.stdout + result.stderr
        self.assertNotIn(key, output)
        self.assertNotIn(key[:8], output)
        self.assertIn("1", result.stdout)


if __name__ == "__main__":
    unittest.main()
