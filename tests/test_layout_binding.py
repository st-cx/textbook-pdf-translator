"""Changing layout files invalidates rendered output without invalidating translation."""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import pymupdf

from textbook_pdf import layout
from textbook_pdf.models import Book, Element, file_sha256
from textbook_pdf.qa import verify
from textbook_pdf.store import Store


class RenderingIdentityTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "_tmp_layout_binding"
        self.base.mkdir(exist_ok=True)
        self.directory = self.base / uuid.uuid4().hex
        self.directory.mkdir()
        self.book = Book("fixture.pdf", "fixture-source", "Fixture", 1, [1],
                         elements=[Element("body", "paragraph", "Original paragraph.", 1)])
        self.translations = {"body": "原始段落的译文。"}

    def tearDown(self):
        self.assertTrue(self.directory.resolve().is_relative_to(self.base.resolve()))
        shutil.rmtree(self.directory)

    def test_identity_records_compositor_template_hashes_and_engine_version(self):
        identity = layout.rendering_binding(self.book, self.translations)["renderer_identity"]
        self.assertEqual(identity, {
            "layout_sha256": file_sha256(Path(layout.__file__).resolve()),
            "template_sha256": file_sha256(layout._TEMPLATE),
            "typst_version": "0.15.1",
        })

    def test_isolated_template_edit_marks_pdf_stale_and_keeps_translation_cache(self):
        template = self.directory / "isolated-book.typ"
        template.write_bytes(layout._TEMPLATE.read_bytes())
        pdf = self.directory / "rendered-fixture.preview.pdf"
        with pymupdf.open() as document:
            document.new_page(width=160, height=160).insert_text((15, 40), "Preview fixture")
            document.save(pdf)
        with Store(self.directory / "tasks.db") as store:
            store.register(self.book, {})
            self.assertTrue(store.put_candidate("body", self.translations["body"], "fixture")["accepted"])
            with patch.object(layout, "_TEMPLATE", template):
                before = layout.rendering_binding(self.book, self.translations)
                mapping = {
                    "elements": {"body": [{"page": 1, "bbox": [15, 20, 140, 50]}]},
                    "assets": {}, "pages": [{"page": 1, "source_pages": [1]}],
                    "render_binding": before, "pdf_sha256": file_sha256(pdf),
                }
                current = verify(self.book, self.translations, self.directory / "qa-before",
                                 pdf_path=pdf, layout_map=mapping)
                self.assertEqual(current["render_revision"], "current")
                template.write_text(template.read_text(encoding="utf-8") + "\n// isolated layout revision\n",
                                    encoding="utf-8")
                after = layout.rendering_binding(self.book, self.translations)
                changed = {key for key in before if before[key] != after[key]}
                self.assertEqual(changed, {"renderer_identity"})
                self.assertNotEqual(before["renderer_identity"]["template_sha256"],
                                    after["renderer_identity"]["template_sha256"])
                stale = verify(self.book, self.translations, self.directory / "qa-after",
                               pdf_path=pdf, layout_map=mapping)
                self.assertEqual(stale["render_revision"], "stale")
                self.assertFalse(stale["technical_checks_passed"])
                issue = next(i for i in stale["issues"] if i["code"] == "stale_rendered_pdf")
                self.assertEqual(issue["changed_fields"], ["renderer_identity"])
                self.assertEqual(file_sha256(pdf), mapping["pdf_sha256"])
                store.register(self.book, {})
                self.assertEqual(store.translations(), self.translations)

    def test_isolated_renderer_file_edit_changes_only_renderer_identity(self):
        renderer = self.directory / "isolated-layout.py"
        renderer.write_bytes(Path(layout.__file__).read_bytes())
        with patch.object(layout, "__file__", str(renderer)):
            before = layout.rendering_binding(self.book, self.translations)
            renderer.write_text(renderer.read_text(encoding="utf-8") + "\n# isolated renderer revision\n",
                                encoding="utf-8")
            after = layout.rendering_binding(self.book, self.translations)
        self.assertEqual({key for key in before if before[key] != after[key]}, {"renderer_identity"})
        self.assertNotEqual(before["renderer_identity"]["layout_sha256"],
                            after["renderer_identity"]["layout_sha256"])
        self.assertEqual(before["renderer_identity"]["template_sha256"],
                         after["renderer_identity"]["template_sha256"])


if __name__ == "__main__":
    unittest.main()
