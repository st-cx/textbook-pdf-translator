"""Offline orchestration checks: real Store/commit gates, mocked HTTP/rendering."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from textbook_pdf import autopilot, translation
from textbook_pdf.models import Asset, Book, Element, file_sha256
from textbook_pdf.store import Store


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / ("_tmp_autopilot_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.pdf = self.root / "original.pdf"
        self.pdf.write_bytes(b"synthetic PDF boundary mocked by analyze")
        self.out = self.root / "job"
        self.config = self.root / "config.json"
        self.secret = "SECRET-MUST-NOT-APPEAR"
        self.config.write_text(json.dumps({"providers": {"fake": {
            "base_url": "http://never-contact.invalid/v1", "api_key": self.secret,
            "model": "fake", "requests_per_minute": 60000, "max_attempts": 1}}}), encoding="utf-8")
        self.book = Book(str(self.pdf), file_sha256(self.pdf), "Original fixture", 3, [1, 2, 3],
            elements=[Element("e" + str(n), "paragraph", "There is one case.", n,
                              chapter="chapter-" + str(n)) for n in range(1, 4)])
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.root)

    def options(self, **kwargs):
        values = dict(pdf=self.pdf, out=self.out, config_path=self.config,
                      batch_size=1, workers=1, target_chars=1)
        values.update(kwargs)
        return autopilot.AutoOptions(**values)

    def chat(self, provider, messages):
        packet = json.loads(messages[-1]["content"])
        self.calls.append(packet)
        return json.dumps({"task_id": packet["task_id"], "source_hash": packet["source_hash"],
            "translations": {eid: "这是一种情况。" for eid in packet["requested_element_ids"]}}, ensure_ascii=False), {"prompt_tokens": 20, "completion_tokens": 10}

    def draft(self, book, translations, outdir, **kwargs):
        Path(outdir).mkdir(exist_ok=True)
        target = Path(outdir) / "reading.pdf"
        target.write_bytes(b"mock reading draft")
        return {"pdf_path": str(target), "page_count": 3, "source_pages": 3,
                "translated_elements": len(translations), "untranslated_elements": 3-len(translations),
                "preview": True, "accepted": False}

    def run_job(self, options=None, callback=None, *, api=None):
        with patch.object(autopilot, "analyze", return_value=deepcopy(self.book)) as analyze, \
             patch.object(autopilot, "_source_preflight", return_value={"blocked": False}), \
             patch.object(autopilot, "load_glossary", return_value={}), \
             patch.object(autopilot, "compose_reading_draft", side_effect=self.draft), \
             patch.object(translation, "_chat", side_effect=api or self.chat):
            result = autopilot.run_job(options or self.options(), callback)
        return result, analyze.call_count

    def test_real_store_end_to_end_and_resume_does_not_repeat(self):
        result, analyzed = self.run_job()
        self.assertEqual(result["state"], "finished", result)
        self.assertEqual((analyzed, len(self.calls)), (1, 3))
        self.assertEqual(result["summary"]["accepted"], 3)
        self.assertTrue(Path(result["pdf_path"]).is_file())
        self.assertFalse(result["accepted"])
        second, analyzed = self.run_job()
        self.assertEqual(second["state"], "finished")
        self.assertEqual((analyzed, len(self.calls)), (0, 3))
        with Store(self.out / "tasks.db") as store:
            self.assertEqual(len(store.translations()), 3)
            self.assertEqual(store.db.execute("SELECT count(*) FROM usage").fetchone()[0], 3)

    def test_default_never_inline_or_standalone_reviews(self):
        with patch.object(autopilot, "run_review") as review, \
             patch.object(autopilot, "run_translation", wraps=translation.run_translation) as run:
            result, _ = self.run_job()
        self.assertEqual(result["state"], "finished")
        self.assertFalse(review.called)
        for call in run.call_args_list:
            self.assertIsNone(call.kwargs["review_provider"])
            self.assertEqual(len(call.kwargs["task_ids"]), 1)

    def test_hold_is_local_and_safe_chapters_progress(self):
        self.book.issues.append({"code": "source_hold", "severity": "error", "element_ids": ["e1", "e2"], "element_id": "e1", "page": 1})
        result, _ = self.run_job()
        self.assertEqual(result["summary"]["accepted"], 1, result)
        self.assertEqual(self.calls[0]["requested_element_ids"], ["e3"])
        self.assertEqual(result["state"], "no_progress")
        self.assertIsNotNone(result["pdf_path"])

    def test_all_source_held_still_drafts_without_config(self):
        self.book.issues = [{"code": "source_hold", "severity": "error", "page": n} for n in range(1, 4)]
        self.config.unlink()
        result, _ = self.run_job()
        self.assertEqual(result["state"], "no_progress", result)
        self.assertEqual(self.calls, [])
        self.assertIsNotNone(result["pdf_path"])

    def test_preflight_blocked_does_not_stop_independent_safe_work(self):
        def preflight(book, outdir):
            book.issues.append({"code": "source_preflight_unavailable", "severity": "error", "page": 1})
            return {"blocked": True}
        with patch.object(autopilot, "analyze", return_value=deepcopy(self.book)), \
             patch.object(autopilot, "_source_preflight", side_effect=preflight), \
             patch.object(autopilot, "load_glossary", return_value={}), \
             patch.object(autopilot, "compose_reading_draft", side_effect=self.draft), \
             patch.object(translation, "_chat", side_effect=self.chat):
            result = autopilot.run_job(self.options())
        self.assertEqual(result["summary"]["accepted"], 2, result)

    def test_stop_file_between_batches_and_resume(self):
        stop = self.root / "STOP"
        def event(value):
            if value["stage"] == "translate" and value["summary"]["accepted"] == 1:
                stop.touch()
        first, _ = self.run_job(self.options(stop_file=stop), event)
        self.assertEqual(first["state"], "stopped")
        self.assertEqual(first["summary"]["accepted"], 1)
        self.assertIsNone(first["pdf_path"])
        stop.unlink()
        second, _ = self.run_job()
        self.assertEqual(second["summary"]["accepted"], 3)
        self.assertEqual(len(self.calls), 3)

    def test_cancel_event_and_callback_false(self):
        cancelled = threading.Event()
        cancelled.set()
        result, _ = self.run_job(self.options(cancel_event=cancelled))
        self.assertEqual(result["state"], "stopped")
        self.assertFalse(self.calls)
        result, _ = self.run_job(callback=lambda event: False)
        self.assertEqual(result["state"], "stopped")
        self.assertFalse(self.calls)

    def test_budget_zero_needs_no_provider_and_still_drafts(self):
        self.config.unlink()
        result, _ = self.run_job(self.options(max_tasks=0, review=True))
        self.assertEqual(result["reason"], "task_budget", result)
        self.assertFalse(self.calls)
        self.assertIsNotNone(result["pdf_path"])

    def test_task_and_time_budgets(self):
        result, _ = self.run_job(self.options(max_tasks=1))
        self.assertEqual(result["summary"]["accepted"], 1)
        self.assertEqual(result["metrics"]["attempted_tasks"], 1)
        result, _ = self.run_job(self.options(max_seconds=0))
        self.assertEqual(result["reason"], "time_budget")
        self.assertEqual(len(self.calls), 1)

    def test_invalid_response_does_not_loop_or_reset_failure(self):
        def invalid(provider, messages):
            packet = json.loads(messages[-1]["content"])
            self.calls.append(packet)
            return "{}", {"prompt_tokens": 1, "completion_tokens": 1}
        result, _ = self.run_job(api=invalid)
        self.assertEqual(result["state"], "no_progress")
        self.assertEqual(len(self.calls), 3)
        with Store(self.out / "tasks.db") as store:
            with store.db:
                store.db.execute("UPDATE tasks SET state='failed'")
        result, _ = self.run_job()
        self.assertEqual(result["state"], "no_progress")
        self.assertEqual(len(self.calls), 3)
        with Store(self.out / "tasks.db") as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM tasks WHERE state='failed'").fetchone()[0], 3)

    def test_disk_book_mismatch_refuses_without_registration(self):
        self.run_job(self.options(max_tasks=0))
        book = Book.load(self.out / "book.json")
        book.elements[0].group_id = "different-group-same-text"
        book.save(self.out / "book.json")
        result, _ = self.run_job()
        self.assertEqual(result["reason"], "book_mismatch")
        self.assertFalse(self.calls)
        with Store(self.out / "tasks.db") as store:
            self.assertEqual(store.book.elements[0].group_id, "")

    def test_source_mutation_and_live_lease_refuse(self):
        self.run_job(self.options(max_tasks=0))
        with Store(self.out / "tasks.db") as store:
            store.claim("another-worker", lease_seconds=600)
        result, _ = self.run_job()
        self.assertEqual(result["reason"], "live_leases")
        self.pdf.write_bytes(b"changed")
        result, _ = self.run_job()
        self.assertEqual(result["reason"], "source_mismatch")
        self.assertFalse(self.calls)

    def test_os_lock_rejects_second_instance(self):
        with autopilot._job_lock(self.out):
            result, _ = self.run_job()
        self.assertEqual(result["state"], "busy")
        self.assertFalse((self.out / "tasks.db").exists())

    def test_revision_change_stops_before_draft(self):
        def event(value):
            if value["stage"] == "translate" and value["summary"]["accepted"] == 1:
                with Store(self.out / "tasks.db") as other:
                    other.register(other.book, other.glossary)
        result, _ = self.run_job(callback=event)
        self.assertEqual(result["reason"], "source_changed", result)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(result["pdf_path"])

    def test_readiness_no_api_and_errors_never_echo_secrets(self):
        with patch.object(translation, "_chat", side_effect=AssertionError("no network")):
            ready = autopilot.check_readiness(self.options())
        self.assertTrue(ready["ready"])
        self.config.write_text(self.secret, encoding="utf-8")
        result, _ = self.run_job()
        self.assertEqual(result["reason"], "provider_unavailable")
        logs = "".join(p.read_text(encoding="utf-8") for p in self.out.glob("autopilot*.json*"))
        self.assertNotIn(self.secret, logs)

    def test_standalone_review_after_translation_bounded_and_draft_survives(self):
        def review(book, store, config, provider, **kwargs):
            self.assertEqual(len(store.translations()), 3)
            self.assertEqual(kwargs["max_elements"], 32)
            return {"requests": 1}
        with patch.object(autopilot, "run_review", side_effect=review) as reviewed:
            result, _ = self.run_job(self.options(review=True))
        self.assertTrue(reviewed.called)
        self.assertEqual(result["metrics"]["requests"], 4)
        self.assertIsNotNone(result["pdf_path"])

    def test_events_are_bounded_and_persisted(self):
        events = []
        result, _ = self.run_job(callback=events.append)
        self.assertTrue(events)
        self.assertTrue(all(0 <= e["progress"] <= 1 for e in events))
        self.assertTrue(all(isinstance(e["message"], str) for e in events))
        receipt = json.loads((self.out / "autopilot_status.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt, result)

    def test_review_exception_keeps_draft_and_no_sensitive_body(self):
        with patch.object(autopilot, "run_review", side_effect=RuntimeError(self.secret)):
            result, _ = self.run_job(self.options(review=True))
        self.assertEqual(result["metrics"]["review_errors"], 1)
        self.assertIsNotNone(result["pdf_path"])
        self.assertNotIn(self.secret, json.dumps(result))

    def test_cancel_skips_render_and_keeps_previous_pdf(self):
        first, _ = self.run_job(self.options(max_tasks=0))
        cancelled = threading.Event(); cancelled.set()
        with patch.object(autopilot, "compose_reading_draft") as compose:
            second = autopilot.run_job(self.options(cancel_event=cancelled))
        self.assertFalse(compose.called)
        self.assertEqual(second["pdf_path"], first["pdf_path"])
        self.assertEqual(second["state"], "stopped")

    def test_render_chunk_progress_and_safe_stop(self):
        event = threading.Event()
        events = []
        def callback(value):
            events.append(value)
            if value["stage"] == "draft" and value["progress"] == 1/3:
                event.set()
        def rendering(book, translations, outdir, **kwargs):
            kwargs["event_callback"]({"source_pages": [1]})
            self.fail("cancel must stop before next chunk")
        with patch.object(autopilot, "analyze", return_value=deepcopy(self.book)), \
             patch.object(autopilot, "_source_preflight", return_value={}), \
             patch.object(autopilot, "load_glossary", return_value={}), \
             patch.object(autopilot, "compose_reading_draft", side_effect=rendering):
            result = autopilot.run_job(self.options(max_tasks=0, cancel_event=event), callback)
        self.assertEqual(result["state"], "stopped", result)
        self.assertTrue(any(v["stage"] == "draft" and v["progress"] == 1/3 for v in events))

    def test_cooldown_and_review_failed_do_not_requeue(self):
        self.run_job(self.options(max_tasks=0))
        with Store(self.out / "tasks.db") as store:
            with store.db:
                store.db.execute("UPDATE tasks SET next_attempt=? WHERE id IN (SELECT id FROM tasks ORDER BY id LIMIT 1)", (time.time()+600,))
            task = store.claim("fixture")
            store.submit(task["id"], "fixture", task["source_hash"], {eid: "这是一种情况。" for eid in task["element_ids"]}, "fake")
            with store.db:
                store.db.execute("UPDATE translations SET status='review_failed'")
                store.db.execute("UPDATE tasks SET state='pending' WHERE id=?", (task["id"],))
        result, _ = self.run_job()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["summary"]["accepted"], 1)

    def test_semantic_unit_tasks_are_isolated_from_normal_work(self):
        self.book.profile["semantic_review_units"] = {"test": {"id": "test", "schema": "semantic-unit-review-v1",
            "role": "body_prose", "source_visual_evidence": "synthetic-only", "member_ids": ["e1", "e2"],
            "source_pdf_sha256": self.book.source_sha256, "boundary": {"left_suffix": "case.", "right_prefix": "There"}}}
        result, _ = self.run_job()
        self.assertEqual(result["summary"]["accepted"], 1, result)
        self.assertEqual(self.calls[0]["requested_element_ids"], ["e3"])

    def test_glossary_path_alias_and_invalid_options(self):
        glossary = self.root / "glossary.json"; glossary.write_text("{}", encoding="utf-8")
        result, _ = self.run_job(self.options(glossary_path=glossary, max_tasks=0))
        self.assertEqual(result["reason"], "task_budget")
        other = self.root / "other.json"
        ready = autopilot.check_readiness(self.options(glossary=other, glossary_path=glossary))
        self.assertEqual(ready["reason"], "invalid_options")
        ready = autopilot.check_readiness(self.options(max_seconds=float("nan")))
        self.assertEqual(ready["reason"], "invalid_options")

    def test_existing_tasks_with_blocked_readonly_context_are_not_sent(self):
        for element in self.book.elements:
            element.chapter = "one-chapter"
        self.book.elements.append(Element("safe", "paragraph", "There is one case.", 3, chapter="other-chapter"))
        self.run_job(self.options(max_tasks=0))
        # Simulate an explicitly prepared current source revision retaining old
        # task payloads. Its context must be checked, not merely its body IDs.
        with Store(self.out / "tasks.db") as store:
            book = store.book
            book.issues.append({"code": "source_hold", "severity": "error", "element_id": "e2"})
            book.save(self.out / "book.json")
            store.register(book, store.glossary)
        result, _ = self.run_job()
        self.assertEqual(result["summary"]["accepted"], 1, result)
        self.assertEqual([p["requested_element_ids"] for p in self.calls], [["safe"]])

    def test_formula_commit_gate_is_not_bypassed_by_autopilot(self):
        self.book.elements = [Element("math", "exercise", "Prove ⟦F:f1⟧ holds.", 1, asset_ids=["f1"])]
        self.book.assets = {"f1": Asset("f1", "formula", "assets/f1.pdf", 1,
            [0, 0, 20, 10], 20, 10, text="p → q", sha256="synthetic-fixture")}
        def missing_formula(provider, messages):
            packet = json.loads(messages[-1]["content"])
            self.calls.append(packet)
            return json.dumps({"task_id": packet["task_id"], "source_hash": packet["source_hash"],
                "translations": {"math": "证明该结论成立。"}}, ensure_ascii=False), {"prompt_tokens": 20, "completion_tokens": 10}
        result, _ = self.run_job(api=missing_formula)
        self.assertEqual(result["summary"]["accepted"], 0, result)
        self.assertEqual(len(self.calls), 1)
        with Store(self.out / "tasks.db") as store:
            self.assertFalse(store.translations())
            self.assertTrue(any(i["code"] == "protected_token_mismatch" for i in store.issues()))

    def test_failed_batch_does_not_prevent_independent_safe_task(self):
        ordinary = translation.run_translation
        attempts = []
        def fail_one(*args, **kwargs):
            attempts.append(kwargs["task_ids"])
            if len(attempts) == 1:
                raise RuntimeError(self.secret)
            return ordinary(*args, **kwargs)
        with patch.object(autopilot, "run_translation", side_effect=fail_one):
            result, _ = self.run_job()
        self.assertEqual(result["summary"]["accepted"], 2)
        self.assertEqual(result["metrics"]["batch_errors"], 1)
        self.assertEqual(len(attempts), 3)
        self.assertNotIn(self.secret, json.dumps(result))

    def test_callback_exception_safely_stops_without_echoing_body(self):
        def broken(event):
            raise RuntimeError(self.secret)
        result, _ = self.run_job(callback=broken)
        self.assertEqual(result["state"], "stopped")
        self.assertFalse(self.calls)
        self.assertNotIn(self.secret, json.dumps(result))

    def test_provider_block_stops_next_batch_without_retrying(self):
        def rejected(provider, messages):
            self.calls.append(json.loads(messages[-1]["content"]))
            raise translation._HTTPFailure(402, self.secret)
        result, _ = self.run_job(api=rejected)
        self.assertEqual(result["state"], "blocked", result)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(result["pdf_path"])
        self.assertNotIn(self.secret, json.dumps(result))

    def test_expired_lease_and_stale_lock_file_resume_normally(self):
        self.run_job(self.options(max_tasks=0))
        with Store(self.out / "tasks.db") as store:
            claimed = store.claim("previous-process")
            with store.db:
                store.db.execute("UPDATE tasks SET lease_until=? WHERE id=?", (time.time()-1, claimed["id"]))
        (self.out / ".autopilot.lock").write_bytes(b"old process file")
        result, _ = self.run_job()
        self.assertEqual(result["summary"]["accepted"], 3, result)
        self.assertEqual(len(self.calls), 3)

    def test_native_reading_draft_cancel_exception_is_stopped(self):
        from textbook_pdf.reading_draft import ReadingDraftCancelled
        self.run_job(self.options(max_tasks=0))
        with patch.object(autopilot, "compose_reading_draft", side_effect=ReadingDraftCancelled()):
            result = autopilot.run_job(self.options(max_tasks=0))
        self.assertEqual(result["state"], "stopped", result)
        self.assertTrue(Path(result["pdf_path"]).is_file())

    def test_resuming_only_out_uses_original_source_and_current_tasks(self):
        self.run_job(self.options(max_tasks=0))
        result, analyzed = self.run_job(self.options(pdf=None))
        self.assertEqual(analyzed, 0)
        self.assertEqual(result["summary"]["accepted"], 3)

    def test_review_excludes_source_hold_old_accepted_and_preserves_store_book(self):
        for element in self.book.elements:
            element.kind = "exercise"
        self.run_job()
        with Store(self.out / "tasks.db") as store:
            book = store.book
            book.issues.append({"code": "source_hold", "severity": "error", "element_id": "e2", "page": 2})
            book.save(self.out / "book.json")
            store.register(book, store.glossary)
            before = deepcopy(store.book)
            revision = autopilot._revision(store)
        reviewed_ids = []
        def reviewing(provider, messages):
            packet = json.loads(messages[-1]["content"])
            rows = packet.get("elements", packet.get("payload", {}).get("elements", []))
            reviewed_ids.extend(row["id"] for row in rows)
            return json.dumps({"task_id": packet["task_id"], "source_hash": packet["source_hash"],
                "reviews": {row["id"]: {"passed": True, "issues": []} for row in rows}}), {"prompt_tokens": 20, "completion_tokens": 10}
        result, _ = self.run_job(self.options(review=True), api=reviewing)
        self.assertEqual(set(reviewed_ids), {"e1", "e3"}, result)
        self.assertEqual(result["metrics"]["review_scope"]["skipped_source_elements"], 1)
        with Store(self.out / "tasks.db") as store:
            self.assertEqual(store.book, before)
            self.assertEqual(autopilot._revision(store), revision)
            passed = [i for i in store.issues() if i["code"] == "semantic_review_passed"]
            self.assertEqual({i["element_id"] for i in passed}, {"e1", "e3"})
            self.assertTrue(all(translation.review_record_matches(i, store.book.element_map()[i["element_id"]], store.translations()[i["element_id"]], store.book, store.glossary) for i in passed))

    def test_review_view_excludes_joint_members_without_mutating_source(self):
        self.book.profile["semantic_review_units"] = {"test": {"id": "test", "schema": "semantic-unit-review-v1",
            "role": "body_prose", "source_visual_evidence": "synthetic-only", "member_ids": ["e1", "e2"],
            "source_pdf_sha256": self.book.source_sha256, "boundary": {"left_suffix": "case.", "right_prefix": "There"}}}
        before = deepcopy(self.book)
        view, scope = autopilot._ordinary_review_view(self.book)
        self.assertEqual([e.id for e in view.elements], ["e3"])
        self.assertEqual(scope["skipped_joint_elements"], 2)
        self.assertEqual(view.assets, self.book.assets)
        self.assertNotIn("semantic_review_units", view.profile)
        self.assertEqual(self.book, before)

    def test_no_draft_completion_does_not_claim_pdf_exists(self):
        events = []
        result, _ = self.run_job(self.options(draft=False), events.append)
        self.assertIsNone(result["pdf_path"])
        self.assertNotIn("阅读稿可查看", events[-1]["message"])

    def test_streaming_book_save_unicode_newlines_without_second_load(self):
        self.book.title = "原创数学：集合与映射 α→β"
        self.book.elements[0].text = "Unicode 原生文字与小数 0.125"
        self.book.elements[0].bbox = [0.0, 1.25, -2.5, 123.456789]
        self.book.profile["nested"] = {"中文": [None, True, {"literal": "A\\B\n第二行"}]}
        path = self.root / "saved-book.json"
        self.book.save(path)
        with patch.object(Book, "load", side_effect=AssertionError("canonical path must stream")):
            self.assertTrue(autopilot._book_file_matches(self.book, path))
            raw = path.read_text(encoding="utf-8")
            path.write_bytes(raw.replace("\n", "\r\n").encode("utf-8"))
            self.assertTrue(autopilot._book_file_matches(self.book, path))

    def test_streaming_book_field_mutation_is_rejected(self):
        path = self.root / "saved-book.json"
        self.book.save(path)
        changed = deepcopy(self.book)
        changed.elements[0].metadata["same-text-different-source-role"] = True
        self.assertFalse(autopilot._book_file_matches(changed, path))

    def test_noncanonical_json_matches_without_loading_second_book(self):
        path = self.root / "saved-book.json"
        self.book.save(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(raw, ensure_ascii=True, sort_keys=True), encoding="utf-8")
        with patch.object(Book, "load", side_effect=AssertionError("never load second book")) as load:
            self.assertTrue(autopilot._book_file_matches(self.book, path))
        self.assertEqual(load.call_count, 0)

    def test_large_trailing_whitespace_is_valid_without_book_load(self):
        path = self.root / "large-book.json"
        self.book.save(path)
        with path.open("ab") as stream:
            block = b" " * (1024 * 1024)
            for _ in range(32):
                stream.write(block)
        self.assertGreaterEqual(path.stat().st_size, 32 * 1024 * 1024)
        with patch.object(Book, "load", side_effect=AssertionError("large fallback must not load")):
            self.assertTrue(autopilot._book_file_matches(self.book, path))

    def test_streaming_asset_keys_and_numeric_representations_are_semantic(self):
        self.book.assets = {"z": Asset("z", "formula", "z.pdf", 1, [1, 2, 3, 4], 2, 2),
                            "a": Asset("a", "formula", "a.pdf", 2, [1.0, 2.0, 3.0, 4.0], 2.0, 2.0)}
        self.book.pages = [{"number": 1, "width": 1, "height": 2.0}]
        path = self.root / "saved-book.json"
        self.book.save(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["assets"] = dict(reversed(list(value["assets"].items())))
        value["assets"]["z"]["bbox"] = [1.0, 2.0, 3.0, 4.0]
        value["pages"][0]["width"] = 1.0
        path.write_text(json.dumps(value, sort_keys=True, ensure_ascii=True), encoding="utf-8")
        with patch.object(Book, "load", side_effect=AssertionError("no full load")):
            self.assertTrue(autopilot._book_file_matches(self.book, path))
            value["assets"]["z"]["width"] = 3
            path.write_text(json.dumps(value), encoding="utf-8")
            self.assertFalse(autopilot._book_file_matches(self.book, path))

    def test_streaming_duplicate_unknown_missing_and_truncated_fields_reject(self):
        path = self.root / "saved-book.json"
        self.book.save(path)
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
        missing = dict(value); del missing["source_sha256"]
        nested_duplicate = raw.replace('"body_size": 10.5', '"body_size": 10.5, "body_size": 10.5')
        for malformed in (raw[:-1], raw + "false", raw.replace('"title":', '"title": "duplicate", "title":', 1),
                          nested_duplicate, json.dumps({**value, "unknown": 1}), json.dumps(missing)):
            with self.subTest(kind=malformed[:30]):
                path.write_text(malformed, encoding="utf-8")
                self.assertFalse(autopilot._book_file_matches(self.book, path))

    def test_stream_reader_bounds_and_split_numbers(self):
        import io
        class ShortReads(io.StringIO):
            def read(self, size=-1):
                self.assertion(size)
                return super().read(min(size, 1))
            def assertion(self, size):
                if size < 0 or size > 65536:
                    raise AssertionError("unbounded read")
        reader = autopilot._JSONStream(ShortReads('[1.25e+2, "escaped\\n字"]'), 1024)
        reader.expect("[")
        self.assertEqual(reader.value(), 125.0)
        reader.expect(",")
        self.assertEqual(reader.value(), "escaped\n字")
        reader.expect("]")
        self.assertEqual(reader.peek(), "")
        oversized = autopilot._JSONStream(io.StringIO('"' + "a" * 100000 + '"'), 65536)
        with self.assertRaises(ValueError):
            oversized.value()

    def test_default_and_manual_workers_really_fill_batches_and_continue(self):
        self.book.page_count = 64
        self.book.selected_pages = list(range(1, 65))
        self.book.elements = [Element("parallel-" + str(n), "paragraph", "There is one case.", n,
            chapter="chapter-" + str(n)) for n in range(1, 65)]
        defaults = autopilot.AutoOptions()
        self.assertEqual(defaults.workers, 32)
        self.assertIsNone(defaults.batch_size)
        for workers in (32, 4, 16):
            with self.subTest(workers=workers):
                self.calls.clear()
                barrier = threading.Barrier(workers)
                guard = threading.Lock()
                activity = {"current": 0, "peak": 0}
                sessions, events = [], []
                def parallel_api(provider, messages):
                    with guard:
                        activity["current"] += 1
                        activity["peak"] = max(activity["peak"], activity["current"])
                    try:
                        barrier.wait(timeout=10)
                        return self.chat(provider, messages)
                    finally:
                        with guard:
                            activity["current"] -= 1
                original = translation.run_translation
                def observed(*args, **kwargs):
                    sessions.append(kwargs["routing_session"])
                    return original(*args, **kwargs)
                option = self.options(out=self.root / ("workers-" + str(workers)), workers=workers, batch_size=None, draft=False)
                with patch.object(autopilot, "run_translation", side_effect=observed):
                    result, _ = self.run_job(option, events.append, api=parallel_api)
                self.assertEqual(result["state"], "finished", result)
                self.assertEqual(activity["peak"], workers)
                self.assertEqual(len(self.calls), 64)
                self.assertEqual(result["summary"]["accepted"], 64)
                self.assertEqual(result["metrics"]["batches"], 64 // workers)
                self.assertEqual(result["metrics"]["configured_workers"], workers)
                self.assertEqual(result["metrics"]["effective_batch_size"], workers)
                self.assertGreaterEqual(len(sessions), 2)
                self.assertTrue(all(session is sessions[0] for session in sessions))
                self.assertTrue(all(e["configured_workers"] == workers and e["effective_batch_size"] == workers for e in events))
                self.assertTrue(any(str(workers) + " 个任务" in e["message"] for e in events))

    def test_explicit_smaller_batch_limits_actual_concurrency(self):
        self.book.page_count = 8
        self.book.selected_pages = list(range(1, 9))
        self.book.elements = [Element("limited-" + str(n), "paragraph", "There is one case.", n,
            chapter="chapter-" + str(n)) for n in range(1, 9)]
        barrier, guard = threading.Barrier(4), threading.Lock()
        activity = {"current": 0, "peak": 0}
        def parallel_api(provider, messages):
            with guard:
                activity["current"] += 1
                activity["peak"] = max(activity["peak"], activity["current"])
            try:
                barrier.wait(timeout=10)
                return self.chat(provider, messages)
            finally:
                with guard:
                    activity["current"] -= 1
        result, _ = self.run_job(self.options(workers=32, batch_size=4, draft=False), api=parallel_api)
        self.assertEqual(result["summary"]["accepted"], 8, result)
        self.assertEqual(activity["peak"], 4)
        self.assertEqual(result["metrics"]["batches"], 2)
        self.assertEqual(result["metrics"]["effective_batch_size"], 4)
        for invalid in (0, 129, True, "32"):
            self.assertEqual(autopilot.check_readiness(self.options(batch_size=invalid))["reason"], "invalid_options")

    def test_translation_pool_readiness_keeps_review_on_explicit_provider(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["providers"]["second"] = {**config["providers"]["fake"], "model": "other-model"}
        config["provider_pools"] = {"pool": {"providers": ["fake", "second"]}}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        with patch.object(translation, "_chat", side_effect=AssertionError("readiness must not send requests")):
            self.assertTrue(autopilot.check_readiness(self.options(provider="pool"))["ready"])
            self.assertFalse(autopilot.check_readiness(self.options(provider="pool", review=True))["ready"])
            self.assertFalse(autopilot.check_readiness(self.options(provider="pool", review=True, review_provider="pool"))["ready"])
            self.assertTrue(autopilot.check_readiness(self.options(provider="pool", review=True, review_provider="fake"))["ready"])
            self.assertTrue(autopilot.check_readiness(self.options(provider="pool", batch_size=128))["ready"])

    def test_pool_blocked_route_health_is_reused_across_batches(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["providers"]["second"] = {**config["providers"]["fake"], "model": "other-model"}
        config["provider_pools"] = {"pool": {"providers": ["fake", "second"]}}
        self.config.write_text(json.dumps(config), encoding="utf-8")
        routes, sessions = [], []
        def route_api(provider, messages):
            routes.append(provider.name)
            if provider.name == "fake":
                raise translation._HTTPFailure(402)
            return self.chat(provider, messages)
        original = translation.run_translation
        def observed(*args, **kwargs):
            sessions.append(kwargs["routing_session"])
            return original(*args, **kwargs)
        with patch.object(autopilot, "run_translation", side_effect=observed):
            result, _ = self.run_job(self.options(provider="pool", batch_size=1), api=route_api)
        self.assertEqual(result["state"], "finished", result)
        self.assertEqual(result["summary"]["accepted"], 3)
        self.assertEqual(routes.count("fake"), 1)
        self.assertEqual(routes.count("second"), 3)
        self.assertEqual(len(sessions), 3)
        self.assertTrue(all(session is sessions[0] for session in sessions))
        self.assertIn("fake", sessions[0].snapshot()["blocked"])


if __name__ == "__main__":
    unittest.main()
