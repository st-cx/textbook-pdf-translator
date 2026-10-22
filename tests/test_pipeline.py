"""Integration checks for real Store state using a completely mocked API boundary.

These tests never read user provider credentials or make network requests.  Their
fixtures use normal Windows directory permissions, matching the desktop sandbox.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
from contextlib import closing
from contextlib import redirect_stdout
import hashlib
import json
import io
from pathlib import Path
import shutil
import sqlite3
import threading
import unittest
from unittest.mock import patch
import uuid

from textbook_pdf import translation
from textbook_pdf.migration import import_legacy, restore_protected_tokens
from textbook_pdf.models import Asset, Book, Element, digest, file_sha256
from textbook_pdf.store import Store
from textbook_pdf.cli import worker_main


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "_tmp_pipeline"
        self.base.mkdir(exist_ok=True)
        self.directory = self.base / uuid.uuid4().hex
        self.directory.mkdir()
        self.stores = []
        self.config = self.directory / "mock-config.json"
        self.write_config()

    def tearDown(self):
        for store in reversed(self.stores):
            store.close()
        target, boundary = self.directory.resolve(), self.base.resolve()
        self.assertTrue(target.is_relative_to(boundary))
        self.assertNotEqual(target, boundary)
        shutil.rmtree(target)

    def write_config(self, attempts=1):
        self.config.write_text(json.dumps({"providers": {"deepseek": {
            "base_url": "http://never-contact-this.invalid/v1", "api_key": "test-only-unused",
            "model": "mock-model", "requests_per_minute": 60000, "max_attempts": attempts,
            "price_in": 2.0, "price_out": 8.0,
        }}}), encoding="utf-8")

    def store(self, book, glossary=None):
        store = Store(self.directory / f"state-{len(self.stores)}.db")
        store.register(book, glossary or {})
        self.stores.append(store)
        return store

    @staticmethod
    def book(elements=None):
        formula = Asset("f1", "formula", "assets/f1.pdf", 1, [30, 20, 50, 30],
                        20, 10, text="p → q", sha256="verified-formula-fixture")
        return Book("fixture.pdf", "fixture-source-sha", "Fixture", 2, [1, 2],
                    elements=elements or [
                        Element("e1", "exercise", "Prove ⟦F:f1⟧ has 2 cases.", 1,
                                [10, 20, 210, 40], asset_ids=["f1"]),
                    ], assets={"f1": formula})

    @staticmethod
    def packet(messages):
        return json.loads(messages[-1]["content"])

    @staticmethod
    def envelope(packet, translations):
        return json.dumps({"task_id": packet["task_id"], "source_hash": packet["source_hash"],
                           "translations": translations}, ensure_ascii=False)

    @staticmethod
    def review_envelope(packet, passed=True):
        elements = packet.get("elements", packet.get("payload", {}).get("elements", []))
        return json.dumps({"task_id": packet["task_id"], "source_hash": packet["source_hash"],
                           "reviews": {e["id"]: {"passed": passed,
                                                  "issues": [] if passed else ["条件方向译反。"]}
                                       for e in elements}}, ensure_ascii=False)

    def usage_rows(self, store):
        return [json.loads(row[0]) for row in store.db.execute("SELECT json FROM usage ORDER BY id")]

    def test_mock_api_protocol_protects_objects_and_frozen_context(self):
        book = self.book([
            Element("previous", "paragraph", "Earlier context.", 1, chapter="1"),
            Element("target", "exercise", "Prove ⟦F:f1⟧ has 2 cases.", 1,
                    asset_ids=["f1"], chapter="1"),
            Element("following", "paragraph", "Later context.", 1, chapter="1"),
        ])
        glossary = {"cases": "情况"}
        store = self.store(book, glossary)
        tasks = translation.make_tasks(book, glossary, target_chars=1)
        store.add_tasks(tasks)
        store.put_candidate("previous", "前面的上下文。", "fixture")
        store.put_candidate("following", "后面的上下文。", "fixture")
        calls = []

        def api(provider, messages):
            packet = self.packet(messages)
            calls.append(packet)
            self.assertEqual(packet["requested_element_ids"], ["target"])
            payload = packet["payload"]
            self.assertEqual(payload["glossary"], glossary)
            self.assertEqual(payload["protected_assets"]["f1"]["text"], "p → q")
            self.assertEqual([e["id"] for e in payload["context_before"]], ["previous"])
            self.assertEqual([e["id"] for e in payload["context_after"]], ["following"])
            self.assertTrue(all(e["readonly"] for e in payload["context_before"] + payload["context_after"]))
            return self.envelope(packet, {"target": "证明 ⟦F:f1⟧ 有 2 种情况。"}), {
                "prompt_tokens": 100, "completion_tokens": 20,
            }

        with patch.object(translation, "_chat", side_effect=api):
            report = translation.run_translation(book, store, self.config, workers=2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["completed"], 1)
        self.assertEqual(store.translations()["target"], "证明 ⟦F:f1⟧ 有 2 种情况。")
        self.assertEqual(report["review_passed"], 0)
        self.assertEqual(report["review_pending"], 1)

    def test_foreign_ids_stale_hash_and_duplicate_json_never_accept(self):
        for error in ("foreign_id", "stale_hash", "duplicate_key"):
            with self.subTest(error=error):
                book = self.book()
                store = self.store(book)

                def api(provider, messages):
                    packet = self.packet(messages)
                    result = {"task_id": packet["task_id"], "source_hash": packet["source_hash"],
                              "translations": {"e1": "证明 ⟦F:f1⟧ 有 2 种情况。"}}
                    if error == "foreign_id":
                        result["translations"]["not-in-task"] = "不属于任务的译文。"
                    elif error == "stale_hash":
                        result["source_hash"] = "older-source-version"
                    raw = json.dumps(result, ensure_ascii=False)
                    if error == "duplicate_key":
                        raw = raw[:-1] + ', "translations": {"e1": "第二份重复译文。"}}'
                    return raw, {"prompt_tokens": 100, "completion_tokens": 20}

                with patch.object(translation, "_chat", side_effect=api):
                    report = translation.run_translation(book, store, self.config, workers=1)
                self.assertEqual(store.translations(), {})
                self.assertEqual(report["completed"], 0)
                self.assertEqual(report["failed"], 1)
                # A rejected output still incurred usage and must be accounted.
                self.assertEqual(self.usage_rows(store)[0]["prompt_tokens"], 100)

    def test_identical_invalid_retry_stops_and_keeps_partial_progress(self):
        self.write_config(attempts=4)
        book = self.book([
            Element("good", "paragraph", "There are 2 cases.", 1),
            Element("bad", "exercise", "Prove ⟦F:f1⟧ holds.", 1, asset_ids=["f1"]),
        ])
        store = self.store(book)
        calls = []

        def api(provider, messages):
            packet = self.packet(messages)
            calls.append(packet)
            result = {"bad": "Prove ⟦F:f1⟧ holds."}
            if len(calls) == 1:
                result["good"] = "共有 2 种情况。"
            return self.envelope(packet, result), {"prompt_tokens": 100, "completion_tokens": 20}

        with patch.object(translation, "_chat", side_effect=api):
            report = translation.run_translation(book, store, self.config, workers=1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["requested_element_ids"], ["good", "bad"])
        self.assertEqual(calls[1]["requested_element_ids"], ["bad"])
        self.assertEqual([e["id"] for e in calls[1]["payload"]["completed_context"]], ["good"])
        self.assertEqual(calls[1]["previous_validation_errors"]["bad"][0]["code"], "untranslated")
        self.assertEqual(store.translations(), {"good": "共有 2 种情况。"})
        self.assertEqual(report["completed"], 0)
        self.assertEqual(report["failed"], 1)
        self.assertTrue(any(i["code"] == "translation_no_progress" for i in store.issues()))
        self.assertEqual(len(self.usage_rows(store)), 2)

    def test_bound_semantic_failure_reason_reaches_only_missing_id_correction(self):
        self.write_config(attempts=2)
        book = self.book([
            Element("good", "paragraph", "This is context.", 1),
            Element("bad", "exercise", "No student likes all courses.", 1),
        ])
        store = self.store(book)
        wrong, corrected = "每个学生都喜欢所有课程。", "没有学生喜欢所有课程。"
        reason = "否定与量词作用域错误：原文说没有学生喜欢所有课程，不能译成每个学生都喜欢所有课程。"
        store.record_issue({"code": "semantic_review_failed", "severity": "error",
                            "element_id": "bad", "translation_hash": digest(wrong),
                            "review_hash": "stale-other-rules", "detail": "过期诊断不得加入纠正请求。"})
        translation_packets, review_packets = [], []

        def api(provider, messages):
            packet = self.packet(messages)
            if "requested_element_ids" in packet:
                translation_packets.append(packet)
                if len(translation_packets) == 1:
                    values = {"good": "这是上下文。", "bad": wrong}
                else:
                    self.assertEqual(packet["requested_element_ids"], ["bad"])
                    self.assertEqual(packet["previous_validation_errors"], {
                        "bad": [{"code": "semantic_review_failed", "detail": reason}]})
                    self.assertEqual([e["id"] for e in packet["payload"]["completed_context"]], ["good"])
                    values = {"bad": corrected}
                return self.envelope(packet, values), {"prompt_tokens": 100, "completion_tokens": 20}
            review_packets.append(packet)
            passed = len(review_packets) > 1
            return json.dumps({"task_id": packet["task_id"], "source_hash": packet["source_hash"],
                               "reviews": {"bad": {"passed": passed, "issues": [] if passed else [reason]}}},
                              ensure_ascii=False), {"prompt_tokens": 100, "completion_tokens": 20}

        with patch.object(translation, "review_selected", side_effect=lambda e: e.kind == "exercise"), \
                patch.object(translation, "_chat", side_effect=api):
            report = translation.run_translation(book, store, self.config, workers=1,
                                                 review_provider="deepseek")
        self.assertEqual(len(translation_packets), 2)
        self.assertEqual(len(review_packets), 2)
        self.assertEqual((report["completed"], report["review_failed"], report["review_passed"]), (1, 1, 1))
        self.assertEqual(store.translations(), {"good": "这是上下文。", "bad": corrected})
        self.assertEqual(translation.review_status(book, store)["failed"], 0)

    def test_both_review_entrypoints_bound_id_count_and_text_size(self):
        elements = [Element(f"review-{i}", "exercise", "Prove a statement. " * (4 if i < 7 else 32), 1)
                    for i in range(13)]
        book = self.book(elements)
        values = {e.id: "证明此命题。" * 6 for e in elements}
        for standalone in (False, True):
            with self.subTest(standalone=standalone):
                store = self.store(book)
                store.add_tasks(translation.make_tasks(book, {}, target_chars=50000))
                if standalone:
                    for eid, value in values.items():
                        store.put_candidate(eid, value, "fixture")
                packets = []
                def api(provider, messages):
                    packet = self.packet(messages)
                    if "requested_element_ids" in packet:
                        return self.envelope(packet, values), {"prompt_tokens": 100, "completion_tokens": 20}
                    entries = packet.get("elements", packet.get("payload", {}).get("elements", []))
                    packets.append(entries)
                    return self.review_envelope(packet), {"prompt_tokens": 100, "completion_tokens": 20}
                with patch.object(translation, "_chat", side_effect=api):
                    report = (translation.run_review(book, store, self.config, "deepseek") if standalone else
                              translation.run_translation(book, store, self.config, workers=1, review_provider="deepseek"))
                self.assertEqual(report["review_passed"], len(elements))
                self.assertEqual(Counter(entry["id"] for entries in packets for entry in entries), Counter(values.keys()))
                self.assertTrue(all(len(entries) <= 6 for entries in packets))
                self.assertTrue(all(sum(len(entry["source"]) + len(entry["translation"]) for entry in entries) <= 3000
                                    or len(entries) == 1 for entries in packets))
                self.assertEqual(translation.review_status(book, store)["passed"], len(elements))

    def test_truncated_review_group_splits_without_repeating_full_payload(self):
        self.write_config(attempts=4)
        book = self.book([Element(eid, "exercise", "Prove a statement.", 1) for eid in ("a", "b")])
        values = {eid: "证明此命题。" for eid in ("a", "b")}
        for standalone in (False, True):
            with self.subTest(standalone=standalone):
                store = self.store(book)
                store.add_tasks(translation.make_tasks(book, {}))
                if standalone:
                    for eid, value in values.items():
                        store.put_candidate(eid, value, "fixture")
                seen = []
                def api(provider, messages):
                    packet = self.packet(messages)
                    if "requested_element_ids" in packet:
                        return self.envelope(packet, values), {"prompt_tokens": 100, "completion_tokens": 20}
                    entries = packet.get("elements", packet.get("payload", {}).get("elements", []))
                    ids = tuple(entry["id"] for entry in entries)
                    seen.append(ids)
                    if len(ids) > 1:
                        return "partial JSON", {"prompt_tokens": 100, "completion_tokens": 8192, "_truncated": True}
                    return self.review_envelope(packet), {"prompt_tokens": 100, "completion_tokens": 20}
                with patch.object(translation, "_chat", side_effect=api):
                    report = (translation.run_review(book, store, self.config, "deepseek") if standalone else
                              translation.run_translation(book, store, self.config, workers=1, review_provider="deepseek"))
                self.assertEqual(seen, [("a", "b"), ("a",), ("b",)])
                self.assertEqual(report["review_passed"], 2)
                self.assertEqual(report["review_pending"], 0)
                usage = self.usage_rows(store)
                self.assertEqual(sum(row["completion_tokens"] for row in usage), report["output_tokens"])
                self.assertEqual(sum(row["completion_tokens"] == 8192 for row in usage), 1)
                self.assertAlmostEqual(sum(row["estimated_cost"] for row in usage), report["estimated_cost"])
                self.assertEqual(translation.review_status(book, store)["passed"], 2)

    def test_single_truncated_review_stays_pending_without_paid_retry(self):
        self.write_config(attempts=4)
        book = self.book([Element("single", "exercise", "Prove a statement.", 1)])
        for standalone in (False, True):
            with self.subTest(standalone=standalone):
                store = self.store(book)
                if standalone:
                    store.put_candidate("single", "证明此命题。", "fixture")
                review_calls = []
                def api(provider, messages):
                    packet = self.packet(messages)
                    if "requested_element_ids" in packet:
                        return self.envelope(packet, {"single": "证明此命题。"}), {
                            "prompt_tokens": 100, "completion_tokens": 20}
                    review_calls.append(packet)
                    return "partial JSON", {"prompt_tokens": 100, "completion_tokens": 8192, "_truncated": True}
                with patch.object(translation, "_chat", side_effect=api):
                    report = (translation.run_review(book, store, self.config, "deepseek") if standalone else
                              translation.run_translation(book, store, self.config, workers=1, review_provider="deepseek"))
                self.assertEqual(len(review_calls), 1)
                self.assertEqual((report["review_passed"], report["review_pending"]), (0, 1))
                self.assertEqual(store.translations(), {"single": "证明此命题。"})
                issue = next(i for i in store.issues() if i["code"] == "semantic_review_pending")
                self.assertIn("长度上限", issue["detail"])
                self.assertEqual(issue["translation_hash"], digest("证明此命题。"))
                self.assertEqual(issue["review_hash"], translation._review_hash(book.elements[0], "证明此命题。", book, {}))
                usage = self.usage_rows(store)
                self.assertEqual(len(usage), 1 if standalone else 2)
                self.assertEqual(sum(row["completion_tokens"] == 8192 for row in usage), 1)

    def test_parallel_review_partitions_are_disjoint_and_share_one_limiter(self):
        elements = [Element(eid, "exercise", "Prove a statement.", 1)
                    for eid in ["seed"] + [f"parallel-{i}" for i in range(13)]]
        book = self.book(elements)
        store = self.store(book)
        for e in elements:
            store.put_candidate(e.id, "证明此命题。", "fixture")
        # All review hashes are deliberately identical. A pass for one ID must
        # not silently certify another element with the same source/text.
        store.record_issue({"code": "semantic_review_passed", "severity": "info", "element_id": "seed",
                            "translation_hash": digest("证明此命题。"),
                            "review_hash": translation._review_hash(elements[0], "证明此命题。", book, {})})
        lock, barrier = threading.Lock(), threading.Barrier(2)
        active, peak, calls = 0, 0, 0
        seen = []
        original_limiter = translation._Limiter
        def api(provider, messages):
            nonlocal active, peak, calls
            packet = self.packet(messages)
            entries = packet["payload"]["elements"]
            with lock:
                active += 1
                peak = max(peak, active)
                calls += 1
                ordinal = calls
                seen.extend(entry["id"] for entry in entries)
            try:
                if ordinal <= 2:
                    barrier.wait(timeout=5)
                return self.review_envelope(packet), {"prompt_tokens": 100, "completion_tokens": 20}
            finally:
                with lock:
                    active -= 1
        with patch.object(translation, "_Limiter", wraps=original_limiter) as limiter, \
                patch.object(translation, "_chat", side_effect=api):
            report = translation.run_review(book, store, self.config, "deepseek", workers=2)
        self.assertEqual(limiter.call_count, 1)
        self.assertEqual(peak, 2)
        self.assertLessEqual(peak, 2)
        self.assertEqual(Counter(seen), Counter(e.id for e in elements if e.id != "seed"))
        self.assertEqual(report["review_passed"], 13)
        self.assertEqual(report["already_reviewed"], 1)
        self.assertEqual(translation.review_status(book, store)["passed"], 14)
        self.assertEqual(len(self.usage_rows(store)), calls)

    def test_parallel_review_interrupt_keeps_inflight_passes_and_stops_new_requests(self):
        elements = [Element(f"interrupt-{i}", "exercise", "Prove a statement.", 1) for i in range(19)]
        book = self.book(elements)
        store = self.store(book)
        for e in elements:
            store.put_candidate(e.id, "证明此命题。", "fixture")
        started, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        calls, stop_events = [], []
        original_limiter = translation._Limiter
        def limiter_factory(rpm, stop):
            stop_events.append(stop)
            return original_limiter(rpm, stop)
        def api(provider, messages):
            packet = self.packet(messages)
            with lock:
                calls.append(packet)
                if len(calls) == 2:
                    started.set()
            self.assertTrue(release.wait(timeout=5))
            return self.review_envelope(packet), {"prompt_tokens": 100, "completion_tokens": 20}
        def interrupt_wait(*args, **kwargs):
            self.assertTrue(started.wait(timeout=5))
            stop_events[0].set()
            release.set()
            raise KeyboardInterrupt()
        with patch.object(translation, "_Limiter", side_effect=limiter_factory), \
                patch.object(translation, "_chat", side_effect=api), \
                patch.object(translation, "wait", side_effect=interrupt_wait):
            report = translation.run_review(book, store, self.config, "deepseek", workers=2)
        self.assertTrue(report["cancelled"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(report["review_passed"], 12)
        self.assertEqual(report["review_pending"], 7)
        self.assertEqual(report["requests"], 2)
        self.assertEqual(len(self.usage_rows(store)), 2)
        self.assertEqual(len(store.translations()), 19)
        self.assertEqual(translation.review_status(book, store)["passed"], 12)

    def test_api_rejects_missing_formula_and_changed_number(self):
        cases = [
            ("证明 p → q 有 2 种情况。", "protected_token_mismatch"),
            ("证明 ⟦F:f1⟧ 有 3 种情况。", "number_mismatch"),
        ]
        for reply, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                book = self.book()
                store = self.store(book)

                def api(provider, messages):
                    return self.envelope(self.packet(messages), {"e1": reply}), {
                        "prompt_tokens": 100, "completion_tokens": 20,
                    }

                with patch.object(translation, "_chat", side_effect=api):
                    report = translation.run_translation(book, store, self.config, workers=1)
                self.assertEqual(store.translations(), {})
                self.assertEqual(report["completed"], 0)
                self.assertTrue(any(i["code"] == expected_code for i in store.issues()))

    def test_concurrent_api_work_is_bounded_and_all_jobs_finish(self):
        book = self.book([Element(f"e{i}", "paragraph", "There are 2 cases.", 1) for i in range(5)])
        store = self.store(book)
        store.add_tasks(translation.make_tasks(book, {}, target_chars=1))
        barrier, lock = threading.Barrier(2), threading.Lock()
        active, peak, calls = 0, 0, 0

        def api(provider, messages):
            nonlocal active, peak, calls
            packet = self.packet(messages)
            with lock:
                active += 1
                calls += 1
                peak = max(peak, active)
                first_pair = calls <= 2
            try:
                if first_pair:
                    barrier.wait(timeout=5)
                return self.envelope(packet, {eid: "共有 2 种情况。" for eid in packet["requested_element_ids"]}), {
                    "prompt_tokens": 100, "completion_tokens": 20,
                }
            finally:
                with lock:
                    active -= 1

        with patch.object(translation, "_chat", side_effect=api):
            report = translation.run_translation(book, store, self.config, workers=2)
        self.assertEqual(peak, 2)
        self.assertEqual(calls, 5)
        self.assertEqual(report["completed"], 5)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(len(store.translations()), 5)

    def test_semantic_failure_revokes_and_old_verdict_cannot_revoke_new_text(self):
        book = self.book()
        store = self.store(book)
        old = "证明 ⟦F:f1⟧ 有 2 种情况。"
        replacement = "应证明 ⟦F:f1⟧ 有 2 种情况。"
        store.add_tasks(translation.make_tasks(book, {}))
        store.put_candidate("e1", old, "fixture")

        def fail_review(provider, messages):
            return self.review_envelope(self.packet(messages), passed=False), {
                "prompt_tokens": 100, "completion_tokens": 20,
            }

        with patch.object(translation, "_chat", side_effect=fail_review):
            result = translation.run_review(book, store, self.config, "deepseek")
        self.assertEqual(result["review_failed"], 1)
        self.assertNotIn("e1", store.translations())
        self.assertTrue(any(i["code"] == "semantic_review_failed" and i["severity"] == "error"
                            for i in store.issues()))
        store.put_candidate("e1", old, "new-unreviewed-candidate")

        def concurrent_review(provider, messages):
            packet = self.packet(messages)
            store.put_candidate("e1", replacement, "concurrent-corrected-version")
            return self.review_envelope(packet, passed=False), {"prompt_tokens": 100, "completion_tokens": 20}

        with patch.object(translation, "_chat", side_effect=concurrent_review):
            result = translation.run_review(book, store, self.config, "deepseek")
        self.assertEqual(result["stale_results"], 1)
        self.assertEqual(result["review_failed"], 0)
        self.assertEqual(store.translations()["e1"], replacement)
        self.assertFalse(store.reject_translation("e1", digest(old)))
        self.assertEqual(store.translations()["e1"], replacement)

    def test_review_cache_tracks_content_and_survives_pagination_changes(self):
        book = self.book()
        store = self.store(book)
        store.put_candidate("e1", "证明 ⟦F:f1⟧ 有 2 种情况。", "fixture")

        def review(provider, messages):
            return self.review_envelope(self.packet(messages)), {"prompt_tokens": 100, "completion_tokens": 20}

        with patch.object(translation, "_chat", side_effect=review) as api:
            self.assertEqual(translation.run_review(book, store, self.config, "deepseek")["review_passed"], 1)
            book.profile["body_size"] = 11
            store.register(book, {})
            self.assertEqual(translation.run_review(book, store, self.config, "deepseek")["scheduled"], 0)
            store.put_candidate("e1", "应证明 ⟦F:f1⟧ 有 2 种情况。", "updated")
            self.assertEqual(translation.run_review(book, store, self.config, "deepseek")["review_passed"], 1)
        self.assertEqual(api.call_count, 2)

    def test_scoped_legacy_import_is_read_only_and_rejects_chunk_id_fallback(self):
        source = self.directory / "legacy-source.pdf"
        source.write_bytes(b"stable source identity; PDF parser is outside this importer test")
        book = self.book([
            Element("exact", "exercise", "Prove ⟦F:f1⟧ has 2 cases.", 1,
                    [10, 20, 210, 40], asset_ids=["f1"]),
            Element("stale", "paragraph", "There are 3 cases.", 1, [10, 60, 210, 80]),
        ])
        book.source_path, book.source_sha256, book.selected_pages = str(source), file_sha256(source), [1]
        blocks = [
            {"page": 1, "kind": "para", "text": "Prove p → q has 2 cases.", "rect": [10, 20, 210, 40]},
            {"page": 1, "kind": "para", "text": "There are 3 cases.", "rect": [10, 60, 210, 80]},
        ] + [{"page": 2, "kind": "para", "text": f"Outside selected pages {i}.",
              "rect": [10, 20, 210, 40]} for i in range(20)]
        manifest = {"pdf": str(source), "fkey": "fixture-key", "prompt_version": "v6",
                    "blocks": blocks, "chunks": [{"id": i, "idxs": [i]} for i in range(len(blocks))]}
        (self.directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        path = self.directory / "_cache.db"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE chunks(file_key TEXT,chunk_hash TEXT,chunk_id INTEGER,model TEXT,translation TEXT,created_at REAL)")
            prompt = "[第1页]\n### 1\nProve p → q has 2 cases."
            expected_hash = hashlib.sha1(("v6|subagent|" + prompt).encode()).hexdigest()
            db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)", (
                "fixture-key", expected_hash, 0, "subagent", json.dumps({"1": "证明 p → q 有 2 种情况。"}), 0,
            ))
            db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)", (
                "fixture-key", "hash-for-an-older-source", 1, "subagent", json.dumps({"1": "共有 3 种情况。"}), 0,
            ))
            db.commit()
        before = file_sha256(path)
        store = self.store(book)
        result = import_legacy(book, store, self.directory)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["exact"], 1)
        self.assertEqual(result["unmatched_chunks"], 1)
        self.assertEqual(result["out_of_scope_chunks"], 20)
        self.assertEqual(result["out_of_scope_blocks"], 20)
        self.assertEqual(store.translations(), {"exact": "证明 ⟦F:f1⟧ 有 2 种情况。"})
        self.assertFalse(any(i.get("page") == 2 for i in store.issues()))
        self.assertEqual(result["reasons"], {"legacy_prompt_hash_mismatch": 1})
        self.assertEqual(file_sha256(path), before)

    def test_invalid_output_cost_is_durable_and_http_failure_cost_is_unknown(self):
        book = self.book()
        store = self.store(book)
        with patch.object(translation, "_chat", return_value=("not JSON", {
            "prompt_tokens": 100, "completion_tokens": 20,
        })):
            report = translation.run_translation(book, store, self.config, workers=1)
        self.assertEqual(report["failed"], 1)
        self.assertAlmostEqual(report["estimated_cost"], 0.00036)
        self.assertEqual(self.usage_rows(store)[0]["prompt_tokens"], 100)
        self.assertTrue(self.usage_rows(store)[0]["usage_known"])
        other = self.store(book)
        with patch.object(translation, "_chat", side_effect=translation._HTTPFailure(401)):
            failure = translation.run_translation(book, other, self.config, workers=1)
        self.assertEqual(failure["requests"], 1)
        self.assertEqual(failure["failed_requests"], 1)
        self.assertFalse(failure["cost_estimate_complete"])
        self.assertFalse(self.usage_rows(other)[0]["usage_known"])
        self.assertEqual(self.usage_rows(other)[0]["http_status"], 401)
        self.assertEqual(other.translations(), {})

    def test_account_error_stops_batch_without_spending_requests_on_other_tasks(self):
        for status in (401, 402, 403):
            with self.subTest(status=status):
                book = self.book([Element(f'e{i}', 'exercise', f'{i}. Prove this statement.', 1)
                                  for i in range(1, 9)])
                store = self.store(book)
                store.add_tasks(translation.make_tasks(book, {}, target_chars=1))
                with patch.object(translation, '_chat', side_effect=translation._HTTPFailure(status)) as api:
                    report = translation.run_translation(book, store, self.config, workers=1)
                self.assertEqual(api.call_count, 1)
                self.assertEqual(report['provider_blocked']['http_status'], status)
                self.assertEqual(store.db.execute("select count(*) from tasks where state='pending'").fetchone()[0], 7)
                self.assertEqual(store.translations(), {})

    def test_account_error_stops_remaining_review_batches_without_rejecting_text(self):
        book = self.book([Element(f'e{i}', 'exercise', f'{i}. Prove this statement.', 1)
                          for i in range(1, 45)])
        store = self.store(book)
        for element in book.elements:
            store.put_candidate(element.id, f'{element.id[1:]}. 证明这个命题。', 'fixture')
        with patch.object(translation, '_chat', side_effect=translation._HTTPFailure(402)) as api:
            report = translation.run_review(book, store, self.config, 'deepseek', workers=1)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(report['provider_blocked']['http_status'], 402)
        self.assertEqual(report['review_pending'], 44)
        self.assertEqual(len(store.translations()), 44)

    def test_changed_glossary_supersedes_leased_task_and_prevents_old_submit(self):
        book = self.book([Element("e1", "paragraph", "A set is given.", 1)])
        old_glossary, new_glossary = {"set": "集合"}, {"set": "集"}
        store = self.store(book, old_glossary)
        old = translation.make_tasks(book, old_glossary)[0]
        store.add_tasks([old])
        self.assertIsNotNone(store.claim("old-owner", task_id=old["id"]))
        store.register(book, new_glossary)
        new = translation.make_tasks(book, new_glossary)[0]
        store.add_tasks([new])
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual([t["id"] for t in store.pending_tasks()], [new["id"]])
        with self.assertRaises(ValueError):
            store.submit(old["id"], "old-owner", old["source_hash"], {"e1": "旧的集合译文。"})
        self.assertEqual(store.translations(), {})
        self.assertIsNotNone(store.claim("new-owner", task_id=new["id"]))
        self.assertTrue(store.submit(new["id"], "new-owner", new["source_hash"], {"e1": "给定一个集。"})["complete"])
        self.assertEqual(store.status()["tasks"], {"done": 1, "superseded": 1})

    def test_current_task_can_be_reactivated_after_rules_are_reverted(self):
        book = self.book([Element("e1", "paragraph", "A set is given.", 1)])
        first, second = {"set": "集合"}, {"set": "集"}
        store = self.store(book, first)
        first_task = translation.make_tasks(book, first)[0]
        store.add_tasks([first_task])
        store.register(book, second)
        store.add_tasks(translation.make_tasks(book, second))
        store.register(book, first)
        store.add_tasks([first_task])
        claimed = store.claim("reverted-rules", task_id=first_task["id"])
        self.assertIsNotNone(claimed, "An active task must not remain superseded after reverting its frozen rules")

    def test_register_new_source_rejects_old_inflight_reply_before_task_rebuild(self):
        book = self.book([Element("e1", "paragraph", "Some elements exist.", 1)])
        store = self.store(book)
        old = translation.make_tasks(book, {})[0]
        store.add_tasks([old])
        store.claim("old-owner", task_id=old["id"])
        updated = deepcopy(book)
        updated.elements[0].text = "All elements exist."
        store.register(updated, {})
        with self.assertRaises(ValueError, msg="An old leased reply must not be accepted against updated source text"):
            store.submit(old["id"], "old-owner", old["source_hash"], {"e1": "存在一些元素。"})
        self.assertEqual(store.translations(), {})

    def test_mixed_api_and_real_worker_commands_share_strict_protocol_and_state(self):
        job = self.directory / "mixed-job"
        job.mkdir()
        book = self.book([
            Element("api", "paragraph", "There are 2 cases.", 1),
            Element("agent", "paragraph", "A set is given.", 1),
        ])
        glossary = {"set": "集合"}
        tasks = translation.make_tasks(book, glossary, target_chars=1)
        agent_task = next(task for task in tasks if task["element_ids"] == ["agent"])

        def api(provider, messages):
            packet = self.packet(messages)
            self.assertEqual(packet["requested_element_ids"], ["api"])
            return self.envelope(packet, {"api": "共有 2 种情况。"}), {
                "prompt_tokens": 100, "completion_tokens": 20,
            }

        with Store(job / "tasks.db") as store:
            store.register(book, glossary)
            store.add_tasks(tasks)
            with patch.object(translation, "_chat", side_effect=api) as transport:
                result = translation.run_translation(book, store, self.config, workers=1, max_tasks=1)
            self.assertEqual(result["completed"], 1)
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(store.translations(), {"api": "共有 2 种情况。"})
        base_args = ["--book", str(job), "--owner", "mixed-agent"]
        captured = io.StringIO()
        with redirect_stdout(captured):
            self.assertEqual(worker_main(base_args + ["claim", agent_task["id"]]), 0)
        claimed = json.loads(captured.getvalue())
        self.assertEqual(claimed["missing_element_ids"], ["agent"])
        response = {"task_id": claimed["id"], "source_hash": claimed["source_hash"],
                    "translations": {"agent": "给定一个集合。"},
                    "proposed_terms": {"domain": "定义域"}}
        result_file = job / "agent-result.json"
        raw = json.dumps(response, ensure_ascii=False)
        result_file.write_text(raw[:-1] + ', "translations": {"agent": "重复键不应被接受。"}}', encoding="utf-8")
        with redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            worker_main(base_args + ["save", claimed["id"], str(result_file)])
        invalid = deepcopy(response)
        invalid["translations"]["api"] = "不能覆盖已经完成的另一个任务。"
        result_file.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")
        with redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            worker_main(base_args + ["save", claimed["id"], str(result_file)])
        result_file.write_text(raw, encoding="utf-8")
        captured = io.StringIO()
        with redirect_stdout(captured):
            self.assertEqual(worker_main(base_args + ["save", claimed["id"], str(result_file)]), 0)
        submitted = json.loads(captured.getvalue())
        self.assertTrue(submitted["complete"])
        with Store(job / "tasks.db") as store:
            self.assertEqual(store.translations(), {"api": "共有 2 种情况。", "agent": "给定一个集合。"})
            self.assertEqual(store.status()["tasks"], {"done": 2})
            self.assertEqual(store.glossary, glossary)
            self.assertTrue(any(issue["code"] == "glossary_term_proposal" and issue.get("english") == "domain"
                                for issue in store.issues()))
            self.assertEqual({row[0] for row in store.db.execute("SELECT provider FROM translations")}, {"deepseek", "agent"})
            self.assertEqual(len(self.usage_rows(store)), 1)


    def test_provider_default_respects_original_top_level_configuration(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["providers"]["custom"] = dict(config["providers"]["deepseek"], model="custom-model")
        config["_default"] = "custom"
        config["default_provider"] = "deepseek"
        config["providers"]["_default"] = "deepseek"
        self.assertEqual(translation._load_provider(config, None).name, "custom")
        self.assertEqual(translation._load_provider(config, "deepseek").name, "deepseek")
        del config["_default"]
        config["default_provider"] = "custom"
        self.assertEqual(translation._load_provider(config, None).name, "custom")
        del config["default_provider"]
        config["providers"]["_default"] = "custom"
        self.assertEqual(translation._load_provider(config, None).name, "custom")

    def test_review_status_counts_current_hashes_and_failed_revisions_only(self):
        book = self.book([Element(eid, "exercise", "Prove a statement.", 1)
                          for eid in ("passed", "stale", "failed", "missing")])
        book.elements.append(Element("literal", "exercise", "T F", 1,
                                     metadata={"translatable": False}))
        store = self.store(book)
        for eid in ("passed", "stale", "failed"):
            store.put_candidate(eid, "证明一个命题。", "fixture")
        for eid in ("passed", "stale"):
            value = store.translations()[eid]
            store.record_issue({"code": "semantic_review_passed", "severity": "info",
                                "element_id": eid, "translation_hash": digest(value),
                                "review_hash": translation._review_hash(book.element_map()[eid], value, book, {})})
        store.put_candidate("stale", "应证明一个命题。", "replacement")
        value = store.translations()["failed"]
        store.record_issue({"code": "semantic_review_failed", "severity": "error",
                            "element_id": "failed", "translation_hash": digest(value),
                            "review_hash": translation._review_hash(book.element_map()["failed"], value, book, {})})
        store.reject_translation("failed", digest(value))
        status = translation.review_status(book, store)
        self.assertEqual({k: status[k] for k in ("required", "accepted", "selected", "passed", "pending", "failed", "awaiting_translation")},
                         {"required": 4, "accepted": 2, "selected": 4, "passed": 1,
                          "pending": 1, "failed": 1, "awaiting_translation": 1})
        self.assertEqual(status["ids"]["passed"], ["passed"])
        self.assertEqual(status["ids"]["pending"], ["stale"])
        self.assertEqual(status["ids"]["failed"], ["failed"])
        self.assertFalse(status["coverage_complete"])
        book.profile["body_size"] = 12
        self.assertEqual(translation.review_status(book, store), status)
        store.put_candidate("failed", "需要证明一个命题。", "corrected")
        updated = translation.review_status(book, store)
        self.assertEqual(updated["failed"], 0)
        self.assertEqual(updated["pending"], 2)

    def test_review_status_invalidates_asset_rules_and_ignores_stale_glossary_flags(self):
        book = self.book()
        store = self.store(book)
        value = "证明 ⟦F:f1⟧ 有 2 种情况。"
        store.put_candidate("e1", value, "fixture")
        store.record_issue({"code": "semantic_review_passed", "severity": "info",
                            "element_id": "e1", "translation_hash": digest(value),
                            "review_hash": translation._review_hash(book.elements[0], value, book, {})})
        self.assertEqual(translation.review_status(book, store)["passed"], 1)
        book.assets["f1"].sha256 = "changed-original-asset"
        self.assertEqual(translation.review_status(book, store)["passed"], 0)
        self.assertEqual(translation.review_status(book, store)["pending"], 1)
        book.assets["f1"].sha256 = "verified-formula-fixture"
        with patch.object(translation, "STYLE_RULES", translation.STYLE_RULES + ("revised style",)):
            self.assertEqual(translation.review_status(book, store)["passed"], 0)
        store.register(book, {"unrelated": "无关"})
        self.assertEqual(translation.review_status(book, store)["passed"], 1)
        ordinary = Element("ordinary", "paragraph", "A statement is given.", 1)
        other = self.store(self.book([ordinary]))
        other.put_candidate("ordinary", "给定一个语句。", "fixture")
        other.record_issue({"code": "glossary_review", "element_id": "ordinary",
                            "translation_hash": digest("旧译文。"), "severity": "warning"})
        with patch.object(translation, "review_selected", return_value=False):
            self.assertEqual(translation.review_status(other.book, other)["selected"], 0)
            other.record_issue({"code": "glossary_review", "element_id": "ordinary", "severity": "warning"})
            self.assertEqual(translation.review_status(other.book, other)["selected"], 1)


    def test_explicit_task_filter_never_claims_unselected_or_unknown_work(self):
        book = self.book([Element(eid, "paragraph", "A statement is given.", 1)
                          for eid in ("first", "target", "last")])
        store = self.store(book)
        tasks = translation.make_tasks(book, {}, target_chars=1)
        store.add_tasks(tasks)
        target = next(t for t in tasks if t["element_ids"] == ["target"])

        def api(provider, messages):
            packet = self.packet(messages)
            self.assertEqual(packet["task_id"], target["id"])
            self.assertEqual(packet["requested_element_ids"], ["target"])
            return self.envelope(packet, {"target": "给定一个语句。"}), {
                "prompt_tokens": 100, "completion_tokens": 20,
            }

        with patch.object(translation, "_chat", side_effect=api) as transport:
            report = translation.run_translation(book, store, self.config, workers=2,
                                                 max_tasks=1, task_ids=[target["id"]])
            self.assertEqual(report["completed"], 1)
            self.assertEqual(transport.call_count, 1)
            with self.assertRaises(ValueError):
                translation.run_translation(book, store, self.config, task_ids=["unknown-task"])
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(store.translations(), {"target": "给定一个语句。"})
        self.assertEqual(store.status()["tasks"], {"done": 1, "pending": 2})
        self.assertEqual({t["id"] for t in store.pending_tasks()}, {t["id"] for t in tasks if t != target})


    def test_task_groups_and_readonly_context_never_bridge_source_page_gaps(self):
        book = self.book([
            Element("a", "paragraph", "First contiguous page.", 1, group_id="same", chapter="one"),
            Element("b", "paragraph", "Second contiguous page.", 2, group_id="same", chapter="one"),
            Element("c", "exercise", "Prove ⟦F:f1⟧ holds.", 9, group_id="same", chapter="one", asset_ids=["f1"]),
            Element("d", "paragraph", "Another contiguous page.", 10, group_id="same", chapter="one"),
        ])
        book.page_count, book.selected_pages = 10, [1, 2, 9, 10]
        tasks = translation.make_tasks(book, {}, target_chars=10000)
        self.assertEqual([task["element_ids"] for task in tasks], [["a", "b"], ["c", "d"]])
        self.assertEqual(tasks[0]["payload"]["protected_assets"], {})
        self.assertEqual(set(tasks[1]["payload"]["protected_assets"]), {"f1"})
        for task in tasks:
            allowed = set(task["element_ids"])
            self.assertTrue(all(e["id"] in allowed for field in ("context_before", "context_after")
                                for e in task["payload"][field]))
            requested = [task["element_ids"][-1]]
            accepted = {"a": "第一段。", "b": "第二段。", "c": "证明 ⟦F:f1⟧ 成立。", "d": "另一段。"}
            packet = translation._request_payload(task, requested, accepted)
            self.assertEqual([e["id"] for e in packet["payload"]["completed_context"]], task["element_ids"][:1])


    def test_pending_review_forces_current_ordinary_revision_until_bound_pass(self):
        book = self.book([Element("ordinary", "paragraph", "A statement is given.", 1)])
        store = self.store(book)
        value = "给定一个语句。"
        store.put_candidate("ordinary", value, "fixture")
        stale = {"code": "semantic_review_pending", "severity": "warning", "element_id": "ordinary",
                 "translation_hash": digest("旧译文。")}
        store.record_issue(stale)
        with patch.object(translation, "review_selected", return_value=False):
            self.assertEqual(translation.review_status(book, store)["selected"], 0)
            with patch.object(translation, "_chat") as transport:
                self.assertEqual(translation.run_review(book, store, self.config, "deepseek")["scheduled"], 0)
                transport.assert_not_called()
            store.record_issue(dict(stale, translation_hash=digest(value)))
            status = translation.review_status(book, store)
            self.assertEqual((status["selected"], status["pending"]), (1, 1))
            def review(provider, messages):
                return self.review_envelope(self.packet(messages)), {"prompt_tokens": 100, "completion_tokens": 20}
            with patch.object(translation, "_chat", side_effect=review) as transport:
                report = translation.run_review(book, store, self.config, "deepseek")
            self.assertEqual(report["review_passed"], 1)
            self.assertEqual(transport.call_count, 1)
            self.assertFalse(any(i["code"] == "semantic_review_pending" for i in store.issues()))
            expected = translation._review_hash(book.elements[0], value, book, {})
            self.assertTrue(any(i.get("review_hash") == expected and i["code"] == "semantic_review_passed"
                                for i in store.issues()))



    def test_legacy_review_migration_keeps_verified_passes_across_unrelated_terms(self):
        book = self.book([Element("e1", "exercise", "Prove a set has 2 cases ⟦F:f1⟧.", 1,
                                  asset_ids=["f1"])])
        old_glossary = {"set": "集合", "inverse function": "反函数"}
        store = self.store(book, old_glossary)
        value = "证明一个集合有 2 种情况 ⟦F:f1⟧。"
        store.put_candidate("e1", value, "fixture")
        old = {"code": "semantic_review_passed", "severity": "info", "element_id": "e1",
               "translation_hash": digest(value), "review_hash": translation.legacy_review_hash(
                   book.elements[0], value, book, old_glossary)}
        store.record_issue(old)
        # Reading legacy status must neither invalidate a genuine verdict nor write aliases.
        before = store.db.execute("SELECT count(*) FROM issues").fetchone()[0]
        self.assertEqual(translation.review_status(book, store)["passed"], 1)
        self.assertEqual(store.db.execute("SELECT count(*) FROM issues").fetchone()[0], before)
        report = translation.migrate_review_bindings(book, store)
        self.assertEqual(report["migrated"], 1)
        self.assertEqual(translation.migrate_review_bindings(book, store)["migrated"], 0)
        new = next(i for i in store.issues() if i.get("previous_review_hash") == old["review_hash"])
        self.assertEqual(new["review_hash_schema"], translation.REVIEW_HASH_SCHEMA)
        self.assertNotEqual(new["review_hash"], old["review_hash"])
        store.register(book, dict(old_glossary, vertex="顶点"))
        self.assertEqual(translation.review_status(book, store)["passed"], 1)
        # Relevant terminology changes must still require a fresh verdict.
        changed = dict(old_glossary, set="集")
        store.register(book, changed)
        store.put_candidate("e1", value, "new-rules-unreviewed")
        self.assertEqual(translation.review_status(book, store)["passed"], 0)
        self.assertEqual(translation.migrate_review_bindings(book, store, previous_glossary=old_glossary)["migrated"], 0)

    def test_explicit_legacy_glossary_proof_cannot_rebind_other_assets_or_ids(self):
        book = self.book()
        old = {"inverse function": "反函数"}
        store = self.store(book, old)
        value = "证明 ⟦F:f1⟧ 有 2 种情况。"
        store.put_candidate("e1", value, "fixture")
        entry = {"code": "semantic_review_passed", "element_id": "e1", "severity": "info",
                 "translation_hash": digest(value), "review_hash": translation.legacy_review_hash(
                     book.elements[0], value, book, old)}
        store.register(book, dict(old, inverse="否命题"))
        # Simulate a late V1 result arriving after an unrelated glossary edit.
        store.record_issue(entry)
        self.assertEqual(translation.review_status(book, store)["passed"], 0)
        self.assertEqual(translation.migrate_review_bindings(book, store, previous_glossary=old)["migrated"], 1)
        self.assertEqual(translation.review_status(book, store)["passed"], 1)
        self.assertFalse(translation.review_record_matches(dict(entry, element_id="other"),
                                                          book.elements[0], value, book, old))
        book.assets["f1"].sha256 = "different-visible-source-asset"
        self.assertFalse(translation.review_record_matches(entry, book.elements[0], value, book, old))
        self.assertEqual(translation.migrate_review_bindings(book, store, previous_glossary=old)["migrated"], 0)

    def test_standalone_failure_resumes_with_reason_and_forces_corrected_review(self):
        book = self.book([Element("target", "paragraph", "Some students attend.", 1),
                          Element("neighbour", "paragraph", "The class begins.", 1)])
        store = self.store(book)
        bad = "所有学生都参加。"
        good = "有学生参加。"
        store.put_candidate("target", bad, "fixture")
        store.put_candidate("neighbour", "课程开始。", "fixture")
        store.add_tasks(translation.make_tasks(book, {}))
        store.record_issue({"code": "semantic_review_pending", "element_id": "target",
                            "translation_hash": digest(bad)})
        calls = []
        def api(provider, messages):
            packet = self.packet(messages)
            calls.append(packet)
            if "requested_element_ids" in packet:
                self.assertEqual(packet["requested_element_ids"], ["target"])
                self.assertIn("将存在量词误译成全称量词", json.dumps(
                    packet["previous_validation_errors"], ensure_ascii=False))
                raw = self.envelope(packet, {"target": good})
            elif len(calls) == 1:
                raw = self.review_envelope(packet, passed=False)
                raw = raw.replace("条件方向译反。", "将存在量词误译成全称量词。")
            else:
                self.assertEqual([e["id"] for e in packet["elements"]], ["target"])
                raw = self.review_envelope(packet)
            return raw, {"prompt_tokens": 100, "completion_tokens": 20}
        with patch.object(translation, "review_selected", return_value=False), \
             patch.object(translation, "_chat", side_effect=api):
            failed = translation.run_review(book, store, self.config, "deepseek", workers=1)
            self.assertEqual(failed["review_failed"], 1)
            result = translation.run_translation(book, store, self.config, workers=1,
                                                 review_provider="deepseek")
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["review_passed"], 1)
        self.assertEqual(store.translations(), {"target": good, "neighbour": "课程开始。"})
        self.assertFalse(any(i["code"] == "semantic_review_failed" for i in store.issues()))

    def test_done_structural_task_pending_review_is_reachable_without_retranslation(self):
        book = self.book()
        store = self.store(book)
        def translate(provider, messages):
            packet = self.packet(messages)
            return self.envelope(packet, {"e1": "证明 ⟦F:f1⟧ 有 2 种情况。"}), {
                "prompt_tokens": 100, "completion_tokens": 20}
        with patch.object(translation, "_chat", side_effect=translate):
            first = translation.run_translation(book, store, self.config, workers=1)
        self.assertEqual(first["review_pending"], 1)
        self.assertEqual(store.status()["tasks"], {"done": 1})
        def review(provider, messages):
            packet = self.packet(messages)
            self.assertNotIn("requested_element_ids", packet)
            return self.review_envelope(packet), {"prompt_tokens": 100, "completion_tokens": 20}
        with patch.object(translation, "_chat", side_effect=review) as transport:
            report = translation.run_review(book, store, self.config, "deepseek", workers=1)
        self.assertEqual((transport.call_count, report["review_passed"]), (1, 1))
        self.assertEqual(store.status()["tasks"], {"done": 1})

    def test_active_current_failure_prevents_old_pass_skipping_a_review(self):
        book = self.book()
        store = self.store(book)
        value = "证明 ⟦F:f1⟧ 有 2 种情况。"
        store.put_candidate("e1", value, "fixture")
        bound = {"element_id": "e1", "translation_hash": digest(value),
                 "review_hash": translation._review_hash(book.elements[0], value, book, {})}
        store.record_issue(dict(bound, code="semantic_review_passed", severity="info"))
        store.record_issue(dict(bound, code="semantic_review_failed", severity="error", detail="随后发现否定遗漏。"))
        def review(provider, messages):
            return self.review_envelope(self.packet(messages)), {"prompt_tokens": 100, "completion_tokens": 20}
        with patch.object(translation, "_chat", side_effect=review) as transport:
            result = translation.run_review(book, store, self.config, "deepseek", workers=1)
        self.assertEqual((transport.call_count, result["review_passed"]), (1, 1))
        self.assertEqual(translation.review_status(book, store)["failed"], 0)



    def test_v2_review_binds_full_protected_descriptor_and_nontoken_assets(self):
        book = self.book()
        book.assets["secondary"] = Asset("secondary", "code", "assets/f1.pdf", 1,
                                          [1, 2, 3, 4], 2, 2, text="code", sha256="same-bundle")
        book.elements[0].asset_ids.append("secondary")
        value = "证明 ⟦F:f1⟧ 有 2 种情况。"
        entry = {"code": "semantic_review_passed", "element_id": "e1",
                 "translation_hash": digest(value), "review_hash_schema": translation.REVIEW_HASH_SCHEMA,
                 "review_hash": translation._review_hash(book.elements[0], value, book, {})}
        legacy = translation.legacy_review_hash(book.elements[0], value, book, {})
        for field, changed in (("text", "p ← q"), ("bbox", [31, 20, 51, 30]),
                               ("width", 21), ("height", 11), ("baseline", 4),
                               ("kind", "code"), ("page", 2)):
            with self.subTest(field=field):
                modified = deepcopy(book)
                setattr(modified.assets["f1"], field, changed)
                self.assertFalse(translation.review_record_matches(entry, modified.elements[0], value, modified, {}))
                # The public legacy helper must exactly preserve the old definition.
                self.assertEqual(translation.legacy_review_hash(modified.elements[0], value, modified, {}), legacy)
        for name, changed in (("pdf_page", 2), ("baseline_from_top", 4),
                              ("source_segments", [{"bbox": [1, 2, 3, 4], "source_order": 1}])):
            with self.subTest(render_metadata=name):
                modified = deepcopy(book)
                modified.assets["f1"].metadata[name] = changed
                self.assertFalse(translation.review_record_matches(entry, modified.elements[0], value, modified, {}))
        modified = deepcopy(book)
        modified.assets["secondary"].baseline = 0.5
        self.assertFalse(translation.review_record_matches(entry, modified.elements[0], value, modified, {}))
        modified = deepcopy(book)
        del modified.assets["secondary"]
        self.assertFalse(translation.review_record_matches(entry, modified.elements[0], value, modified, {}))



    def test_migrated_legacy_proof_is_archived_before_asset_render_metadata_changes(self):
        book = self.book()
        store = self.store(book)
        value = "证明 ⟦F:f1⟧ 有 2 种情况。"
        store.put_candidate("e1", value, "fixture")
        old = {"code": "semantic_review_passed", "severity": "info", "element_id": "e1",
               "translation_hash": digest(value), "review_hash": translation.legacy_review_hash(
                   book.elements[0], value, book, {})}
        store.record_issue(old)
        report = translation.migrate_review_bindings(book, store)
        self.assertEqual((report["migrated"], report["archived_legacy"]), (1, 1))
        self.assertTrue(any(i.get("review_hash") == old["review_hash"]
                            and i["resolution_reason"] == "review_binding_migrated"
                            for i in store.resolved_issues()))
        # A late duplicate V1 reply is closed even if its stronger alias exists.
        store.record_issue(old)
        again = translation.migrate_review_bindings(book, store)
        self.assertEqual((again["migrated"], again["archived_legacy"]), (0, 1))
        self.assertFalse(any(i.get("review_hash") == old["review_hash"] for i in store.issues()))
        changed = deepcopy(book)
        changed.assets["f1"].metadata["pdf_page"] = 3
        store.register(changed, {})
        store.put_candidate("e1", value, "same-text-unreviewed-new-asset")
        self.assertEqual(translation.review_status(changed, store)["passed"], 0)
        self.assertEqual(translation.review_status(changed, store)["pending"], 1)



    def legacy_fixture(self, *, version="v5", translated="共有 2 种情况。", formula=False,
                       model="historical-actual-model"):
        folder = self.directory / ("legacy-" + uuid.uuid4().hex)
        folder.mkdir()
        source = folder / "source.pdf"
        source.write_bytes(b"unchanged historical source identity")
        text = "Prove ⟦F:f1⟧ has 2 cases." if formula else "There are 2 cases."
        old_text = "Prove p → q has 2 cases." if formula else text
        element = Element("e1", "paragraph", text, 1, [10, 20, 210, 40],
                          asset_ids=["f1"] if formula else [])
        book = self.book([element])
        book.source_path, book.source_sha256, book.selected_pages = str(source), file_sha256(source), [1]
        manifest = {"pdf": str(source), "fkey": "fixture-key", "prompt_version": "v6",
                    "blocks": [{"page": 1, "kind": "para", "text": old_text, "rect": element.bbox}],
                    "chunks": [{"id": 0, "idxs": [0]}]}
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        prompt = "[第1页]\n### 1\n" + old_text
        chunk_hash = hashlib.sha1((version + "|" + model + "|" + prompt).encode()).hexdigest()
        with closing(sqlite3.connect(folder / "_cache.db")) as db:
            db.execute("CREATE TABLE chunks(file_key TEXT,chunk_hash TEXT,chunk_id INTEGER,model TEXT,translation TEXT,created_at REAL)")
            # Deliberately wrong chunk_id: exact full-prompt/model hashing is the only lookup.
            db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)", ("fixture-key", chunk_hash, 999, model,
                       json.dumps({"1": translated}, ensure_ascii=False), 0))
            db.commit()
        return folder, book

    def test_v5_exact_source_uses_actual_model_and_remains_hash_bound_pending_review(self):
        folder, book = self.legacy_fixture()
        before = file_sha256(folder / "_cache.db")
        store = self.store(book)
        with patch.object(translation, "review_selected", return_value=False):
            report = import_legacy(book, store, folder)
        self.assertEqual(report["exact"], 1)
        self.assertEqual(report["exact_by_prompt_version"], {"v6": 0, "v5": 1})
        self.assertEqual(report["imported_by_prompt_version"], {"v6": 0, "v5": 1})
        self.assertEqual(report["historical_semantic_pending"], 1)
        self.assertEqual(store.translations(), {"e1": "共有 2 种情况。"})
        pending = next(i for i in store.issues() if i["code"] == "semantic_review_pending")
        self.assertEqual(pending["legacy_prompt_version"], "v5")
        self.assertTrue(translation.review_record_matches(pending, book.elements[0],
                                                         store.translations()["e1"], book, {}))
        self.assertFalse(any(i["code"] == "semantic_review_passed" for i in store.issues()))
        with patch.object(translation, "review_selected", return_value=False):
            self.assertEqual(translation.review_status(book, store)["pending"], 1)
        self.assertEqual(file_sha256(folder / "_cache.db"), before)

    def test_legacy_unknown_corrupt_or_changed_source_never_uses_chunk_id_fallback(self):
        for scenario in ("unknown_cache_version", "corrupt_hash", "source_text_changed",
                         "source_pdf_changed", "unknown_manifest_version"):
            with self.subTest(scenario=scenario):
                folder, book = self.legacy_fixture(version="v4" if scenario == "unknown_cache_version" else "v5")
                if scenario == "corrupt_hash":
                    with closing(sqlite3.connect(folder / "_cache.db")) as db:
                        db.execute("UPDATE chunks SET chunk_hash='damaged-source-hash',chunk_id=0")
                        db.commit()
                elif scenario in ("source_text_changed", "unknown_manifest_version"):
                    path = folder / "manifest.json"
                    manifest = json.loads(path.read_text(encoding="utf-8"))
                    if scenario == "source_text_changed":
                        manifest["blocks"][0]["text"] = "There are 3 cases."
                    else:
                        manifest["prompt_version"] = "v4"
                    path.write_text(json.dumps(manifest), encoding="utf-8")
                elif scenario == "source_pdf_changed":
                    Path(book.source_path).write_bytes(b"a different source PDF replaced the original")
                before = file_sha256(folder / "_cache.db")
                store = self.store(book)
                result = import_legacy(book, store, folder)
                self.assertEqual(result["imported"], 0)
                self.assertEqual(store.translations(), {})
                expected = ("legacy_source_identity_mismatch" if scenario == "source_pdf_changed" else
                            "legacy_prompt_version_unsupported" if scenario == "unknown_manifest_version" else
                            "legacy_prompt_hash_mismatch")
                self.assertIn(expected, result["reasons"])
                self.assertEqual(file_sha256(folder / "_cache.db"), before)

    def test_v5_cache_must_pass_current_numeric_geometry_formula_and_reply_checks(self):
        for scenario in ("number", "geometry", "formula", "reply"):
            with self.subTest(scenario=scenario):
                folder, book = self.legacy_fixture(
                    translated="证明有 2 种情况。" if scenario == "formula" else
                                "共有 3 种情况。" if scenario == "number" else "共有 2 种情况。",
                    formula=scenario == "formula")
                if scenario == "geometry":
                    book.elements[0].bbox = [20, 20, 220, 40]
                if scenario == "reply":
                    with closing(sqlite3.connect(folder / "_cache.db")) as db:
                        db.execute("UPDATE chunks SET translation='unparseable reply without numbered entries'")
                        db.commit()
                store = self.store(book)
                before = file_sha256(folder / "_cache.db")
                result = import_legacy(book, store, folder)
                self.assertEqual(result["exact"], 1)
                self.assertEqual(result["imported"], 0)
                self.assertEqual(store.translations(), {})
                expected = {"number": "legacy_translation_invalid", "geometry": "legacy_geometry_ambiguous",
                            "formula": "legacy_formula_ambiguous", "reply": "legacy_invalid_reply"}[scenario]
                self.assertIn(expected, result["reasons"])
                self.assertEqual(file_sha256(folder / "_cache.db"), before)



    def test_conflicting_exact_v5_v6_replies_are_retained_without_arbitrary_acceptance(self):
        folder, book = self.legacy_fixture()
        prompt = "[第1页]\n### 1\nThere are 2 cases."
        key = hashlib.sha1(("v6|historical-actual-model|" + prompt).encode()).hexdigest()
        with closing(sqlite3.connect(folder / "_cache.db")) as db:
            db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)", ("fixture-key", key, 0,
                       "historical-actual-model", json.dumps({"1": "有 2 个情形。"}), 1))
            db.commit()
        before = file_sha256(folder / "_cache.db")
        store = self.store(book)
        result = import_legacy(book, store, folder)
        self.assertEqual(result["exact"], 1)
        self.assertEqual(result["exact_by_prompt_version"], {"v6": 1, "v5": 1})
        self.assertEqual(result["imported"], 0)
        self.assertIn("legacy_conflicting_candidates", result["reasons"])
        self.assertEqual(store.translations(), {})
        self.assertEqual(store.db.execute("SELECT count(*) FROM candidates").fetchone()[0], 2)
        self.assertEqual(file_sha256(folder / "_cache.db"), before)



    def test_legacy_formula_same_bundle_different_crops_cannot_be_assigned_by_text_order(self):
        book = self.book()
        second = deepcopy(book.assets["f1"])
        second.id = "f2"
        book.assets["f1"].metadata["pdf_page"] = 0
        second.metadata["pdf_page"] = 1
        # Equal file SHA/dimensions/baseline still does not identify a PDF crop.
        book.assets["f2"] = second
        element = Element("e1", "paragraph", "If ⟦F:f1⟧ and ⟦F:f2⟧ hold.", 1,
                          asset_ids=["f1", "f2"])
        book.elements = [element]
        with self.assertRaisesRegex(ValueError, "multiple source objects"):
            restore_protected_tokens(element, "若 p → q 且 p → q 成立。", book)
        # Already explicit, exact source IDs need no text-to-object guess.
        tokenized = "若 ⟦F:f1⟧ 且 ⟦F:f2⟧ 成立。"
        self.assertEqual(restore_protected_tokens(element, tokenized, book), tokenized)
        # Two references to one proven source object remain recoverable by count.
        element.text, element.asset_ids = "If ⟦F:f1⟧ and ⟦F:f1⟧ hold.", ["f1"]
        restored = restore_protected_tokens(element, "若 p → q 且 p → q 成立。", book)
        self.assertEqual(restored.count("⟦F:f1⟧"), 2)



    def test_task_binds_owned_and_context_assets_and_keeps_api_geometry_private(self):
        book = self.book([Element("context", "paragraph", "An earlier ⟦F:f1⟧ is given.", 1, asset_ids=["f1"]),
                          Element("target", "paragraph", "A statement is given.", 1, asset_ids=["owned"])])
        book.assets["owned"] = Asset("owned", "code", "assets/code.pdf", 1, [3, 4, 10, 20], 7, 16,
                                     text="x = 1", sha256="owned-source", metadata={"pdf_page": 4})
        book.assets["f1"].metadata["pdf_page"] = 2
        task = next(t for t in translation.make_tasks(book, {}, target_chars=1) if t["element_ids"] == ["target"])
        self.assertEqual(task["payload"]["protected_assets_schema"], "protected-assets-v2")
        frozen = task["payload"]["protected_asset_descriptors"]
        self.assertEqual(set(frozen), {"f1", "owned"})
        self.assertEqual(frozen["owned"]["render_metadata"]["pdf_page"], 4)
        api = translation._request_payload(task, ["target"], {})
        self.assertNotIn("protected_asset_descriptors", api["payload"])
        self.assertEqual(api["payload"]["protected_assets"]["f1"]["text"], "p → q")
        self.assertEqual(api["source_hash"], task["source_hash"])
        self.assertIn("protected_asset_descriptors", task["payload"])
        self.assertTrue(translation._current_task(task, book, {}))
        book.assets["f1"].metadata["pdf_page"] = 3
        self.assertEqual(frozen["f1"]["render_metadata"]["pdf_page"], 2)
        self.assertFalse(translation._current_task(task, book, {}))
        rebuilt = next(t for t in translation.make_tasks(book, {}, target_chars=1) if t["element_ids"] == ["target"])
        self.assertNotEqual(task["id"], rebuilt["id"])

    def test_old_protected_object_task_is_regenerated_without_retranslating_accepted_neighbour(self):
        book = self.book([Element("neighbour", "paragraph", "The class begins.", 1),
                          Element("target", "exercise", "Prove ⟦F:f1⟧ holds.", 1, asset_ids=["f1"])])
        store = self.store(book)
        tasks = translation.make_tasks(book, {}, target_chars=1)
        old = next(t for t in tasks if t["element_ids"] == ["target"])
        store.add_tasks(tasks)
        store.put_candidate("neighbour", "课程开始。", "fixture")
        current = deepcopy(book)
        current.assets["f1"].metadata["pdf_page"] = 2
        store.register(current, {})
        def api(provider, messages):
            packet = self.packet(messages)
            self.assertNotEqual(packet["task_id"], old["id"])
            self.assertEqual(packet["requested_element_ids"], ["target"])
            self.assertNotIn("protected_asset_descriptors", packet["payload"])
            return self.envelope(packet, {"target": "证明 ⟦F:f1⟧ 成立。"}), {"prompt_tokens": 10, "completion_tokens": 10}
        with patch.object(translation, "_chat", side_effect=api) as transport:
            result = translation.run_translation(current, store, self.config, workers=1)
        self.assertEqual((transport.call_count, result["completed"]), (1, 1))
        self.assertEqual(store.translations()["neighbour"], "课程开始。")
        self.assertEqual(store.db.execute("SELECT state FROM tasks WHERE id=?", (old["id"],)).fetchone()[0], "superseded")

    def test_cancelled_before_review_retains_translation_and_resumes_review_without_retranslation(self):
        book = self.book()
        store = self.store(book)
        acquire_count = [0]
        def acquire(limiter):
            acquire_count[0] += 1
            if acquire_count[0] == 2:
                raise translation._Cancelled()
        def api(provider, messages):
            packet = self.packet(messages)
            return self.envelope(packet, {"e1": "证明 ⟦F:f1⟧ 有 2 种情况。"}), {"prompt_tokens": 10, "completion_tokens": 10}
        with patch.object(translation._Limiter, "acquire", acquire), patch.object(translation, "_chat", side_effect=api) as transport:
            result = translation.run_translation(book, store, self.config, workers=1, review_provider="deepseek")
        self.assertTrue(result["cancelled"])
        self.assertEqual((transport.call_count, result["requests"], result["interrupted_saved_elements"]), (1, 1, 1))
        self.assertEqual(store.translations(), {"e1": "证明 ⟦F:f1⟧ 有 2 种情况。"})
        self.assertEqual(translation.review_status(book, store)["pending"], 1)
        self.assertEqual(store.status()["tasks"], {"done": 1})
        def review(provider, messages):
            packet = self.packet(messages)
            self.assertNotIn("requested_element_ids", packet)
            return self.review_envelope(packet), {"prompt_tokens": 10, "completion_tokens": 10}
        with patch.object(translation, "_chat", side_effect=review) as transport:
            resumed = translation.run_review(book, store, self.config, "deepseek", workers=1)
        self.assertEqual((transport.call_count, resumed["review_passed"]), (1, 1))

    def test_cancel_after_partial_review_never_accepts_failed_candidate(self):
        elements = [Element(f"e{i}", "exercise", "Prove a statement.", 1) for i in range(8)]
        book = self.book(elements)
        store = self.store(book)
        acquisitions, calls = [0], []
        def acquire(limiter):
            acquisitions[0] += 1
            if acquisitions[0] == 3:
                raise translation._Cancelled()
        def api(provider, messages):
            packet = self.packet(messages)
            calls.append(packet)
            if "requested_element_ids" in packet:
                raw = self.envelope(packet, {e.id: "证明一个语句。" for e in elements})
            else:
                data = json.loads(self.review_envelope(packet))
                data["reviews"]["e0"] = {"passed": False, "issues": ["漏译否定。"]}
                raw = json.dumps(data, ensure_ascii=False)
            return raw, {"prompt_tokens": 10, "completion_tokens": 10}
        with patch.object(translation._Limiter, "acquire", acquire), patch.object(translation, "_chat", side_effect=api):
            result = translation.run_translation(book, store, self.config, workers=1, review_provider="deepseek")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["interrupted_saved_elements"], 7)
        self.assertNotIn("e0", store.translations())
        status = translation.review_status(book, store)
        self.assertEqual((status["passed"], status["pending"], status["failed"]), (5, 2, 1))
        self.assertEqual(store.status()["tasks"], {"partial": 1})

    def test_cancelled_save_rejects_changed_protected_source(self):
        book = self.book()
        store = self.store(book)
        count = [0]
        def acquire(limiter):
            count[0] += 1
            if count[0] == 2:
                raise translation._Cancelled()
        def api(provider, messages):
            packet = self.packet(messages)
            current = deepcopy(book)
            current.assets["f1"].metadata["pdf_page"] = 7
            store.register(current, {})
            return self.envelope(packet, {"e1": "证明 ⟦F:f1⟧ 有 2 种情况。"}), {"prompt_tokens": 10, "completion_tokens": 10}
        with patch.object(translation._Limiter, "acquire", acquire), patch.object(translation, "_chat", side_effect=api):
            result = translation.run_translation(book, store, self.config, workers=1, review_provider="deepseek")
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["interrupted_saved_elements"], 0)
        self.assertEqual(result["interrupted_save_rejected"], 1)
        self.assertEqual(store.translations(), {})
        self.assertFalse(any(i.get("code") == "semantic_review_pending" for i in store.issues()))


if __name__ == "__main__":
    unittest.main()
