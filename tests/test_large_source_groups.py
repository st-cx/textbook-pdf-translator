"""Large source groups remain reachable without weakening the task contract.

All requests are mocked. The synthetic formula, source text and unused provider
configuration are unrelated to private books or credentials.
"""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch
import uuid

from textbook_pdf import translation as tr
from textbook_pdf.models import Asset, Book, Element, digest
from textbook_pdf.store import Store


class LargeSourceGroups(unittest.TestCase):
    def setUp(self):
        self.base = Path(__file__).resolve().parent / "_tmp_large_source_groups"
        self.base.mkdir(exist_ok=True)
        self.directory = self.base / uuid.uuid4().hex
        self.directory.mkdir()
        self.config = self.directory / "unused-config.json"
        self.config.write_text(json.dumps({"providers": {"mock": {
            "base_url": "http://never-contact.invalid/v1", "api_key": "unused-synthetic",
            "model": "mock", "max_tokens": 8192, "max_attempts": 1,
            "requests_per_minute": 600000}}}), encoding="utf8")
        self.stores = []

    def tearDown(self):
        for store in reversed(self.stores):
            store.close()
        self.assertTrue(self.directory.resolve().is_relative_to(self.base.resolve()))
        shutil.rmtree(self.directory)

    def fixture(self, count):
        formula = Asset("f1", "formula", "unused-synthetic.pdf", 1,
                        [10, 10, 20, 20], 10, 10, text="p implies q",
                        sha256="synthetic-formula", metadata={"pdf_page": 0})
        book = Book("unused-synthetic-source.pdf", "synthetic-source", "Synthetic", 1, [1],
                    assets={"f1": formula}, elements=[Element(
                        f"e{i}", "exercise", f"In part {i}, prove that 2 cases satisfy ⟦F:f1⟧.", 1,
                        asset_ids=["f1"], group_id="whole-question", chapter="1")
                        for i in range(count)])
        store = Store(self.directory / f"state-{len(self.stores)}.db")
        self.stores.append(store)
        store.register(book, {})
        tasks = tr.make_tasks(book, {}, target_chars=100000)
        self.assertEqual(len(tasks), 1)
        store.add_tasks(tasks)
        return book, store, tasks[0]

    @staticmethod
    def packet(messages):
        return json.loads(messages[-1]["content"])

    @staticmethod
    def good(packet, ids=None):
        ids = packet["requested_element_ids"] if ids is None else ids
        return json.dumps({"task_id": packet["task_id"], "source_hash": packet["source_hash"],
                           "translations": {eid: f"第 {eid[1:]} 小问，证明 2 种情况满足 ⟦F:f1⟧。" for eid in ids}},
                          ensure_ascii=False), {"prompt_tokens": 100, "completion_tokens": 40}

    def assert_group_context(self, packet, task):
        self.assertEqual(packet["task_id"], task["id"])
        self.assertEqual(packet["source_hash"], task["source_hash"])
        self.assertLessEqual(len(packet["requested_element_ids"]), 6)
        self.assertTrue(packet["requested_element_ids"])
        self.assertNotIn("protected_asset_descriptors", packet["payload"])
        self.assertEqual([e["id"] for e in packet["payload"]["elements"]], packet["requested_element_ids"])
        groups = packet["payload"]["readonly_source_groups"]
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertTrue(group["readonly"])
        self.assertEqual(group["member_ids"], task["element_ids"])
        self.assertEqual(group["group_id"], "whole-question")
        self.assertEqual(group["chapter"], "1")
        for source, frozen in zip(group["elements"], task["payload"]["elements"]):
            self.assertTrue(source["readonly"])
            self.assertEqual({k: v for k, v in source.items() if k != "readonly"}, frozen)
        self.assertEqual(len(group["elements"]), len(task["element_ids"]))
        self.assertEqual(packet["payload"]["protected_assets"], task["payload"]["protected_assets"])

    def run_api(self, book, store, api, **kwargs):
        with patch.object(tr, "_chat", side_effect=api):
            return tr.run_translation(book, store, self.config, provider="mock", workers=1, **kwargs)

    def test_seven_and_thirty_four_ids_all_reachable_through_real_store(self):
        for count in (7, 34):
            with self.subTest(count=count):
                book, store, task = self.fixture(count)
                frozen = deepcopy(task)
                calls = []
                def api(provider, messages):
                    packet = self.packet(messages)
                    self.assert_group_context(packet, task)
                    calls.append(packet)
                    return self.good(packet)
                report = self.run_api(book, store, api, task_ids=[task["id"]])
                self.assertEqual(report["completed"], 1)
                self.assertEqual(report["translation_deferred_groups"], 0)
                requested = [eid for p in calls for eid in p["requested_element_ids"]]
                self.assertEqual(Counter(requested), Counter(task["element_ids"]))
                self.assertEqual(len(store.translations()), count)
                self.assertEqual(len(calls), (count + 5) // 6)
                self.assertEqual(store.db.execute("SELECT state FROM tasks").fetchone()[0], "done")
                self.assertEqual(task, frozen)
                self.assertEqual(digest(task["payload"]), task["source_hash"])
                self.assertNotIn("readonly_source_groups", task["payload"])
                self.assertEqual(tr.make_tasks(book, {}, target_chars=100000), [frozen])

    def test_cancel_and_resume_keeps_context_and_never_retranslates_saved_members(self):
        book, store, task = self.fixture(34)
        session = tr.TranslationRoutingSession(json.loads(self.config.read_text("utf8")), "mock")
        first = []
        def interrupt_after_reply(provider, messages):
            packet = self.packet(messages)
            self.assert_group_context(packet, task)
            first.extend(packet["requested_element_ids"])
            session.stop.set()
            return self.good(packet)
        stopped = self.run_api(book, store, interrupt_after_reply, routing_session=session)
        self.assertTrue(stopped["cancelled"])
        self.assertEqual(len(first), 6)
        self.assertEqual(set(store.translations()), set(first))
        self.assertEqual(store.db.execute("SELECT count(*) FROM tasks WHERE state='leased'").fetchone()[0], 0)
        resumed_calls = []
        def resume(provider, messages):
            packet = self.packet(messages)
            self.assert_group_context(packet, task)
            self.assertFalse(set(first) & set(packet["requested_element_ids"]))
            completed = {e["id"] for e in packet["payload"]["completed_context"]}
            self.assertTrue(set(first) <= completed)
            resumed_calls.extend(packet["requested_element_ids"])
            return self.good(packet)
        # A cancelled task retains its normal retry cooldown. Resume after it,
        # rather than clearing queue fields or weakening the claim protocol.
        with patch("textbook_pdf.store.time.time", return_value=time.time() + 120):
            done = self.run_api(book, store, resume, task_ids=[task["id"]])
        self.assertEqual(done["completed"], 1)
        self.assertEqual(Counter(first + resumed_calls), Counter(task["element_ids"]))
        self.assertEqual(len(store.translations()), 34)

    def test_complete_readonly_context_does_not_cross_source_page_gap(self):
        book, store, _ = self.fixture(14)
        book.page_count = 4
        book.selected_pages = [1, 4]
        for element in book.elements[7:]:
            element.page = 4
        store.register(book, {})
        tasks = tr.make_tasks(book, {}, target_chars=100000)
        self.assertEqual(len(tasks), 2)
        store.add_tasks(tasks)
        calls = []
        def api(provider, messages):
            packet = self.packet(messages)
            task = next(t for t in tasks if t["id"] == packet["task_id"])
            self.assert_group_context(packet, task)
            group = packet["payload"]["readonly_source_groups"][0]
            self.assertEqual(len({row["page"] for row in group["elements"]}), 1)
            source_page = group["elements"][0]["page"]
            self.assertTrue(all(row["page"] == source_page for key in
                                ("context_before", "context_after")
                                for row in packet["payload"][key]))
            calls.append(packet)
            return self.good(packet)
        report = self.run_api(book, store, api, task_ids=[t["id"] for t in tasks])
        self.assertEqual(report["completed"], 2)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(store.translations()), 14)

    def test_partial_progress_with_one_attempt_reaches_all_members_without_retrying_accepted(self):
        book, store, task = self.fixture(7)
        saved = set()
        calls = []
        def api(provider, messages):
            packet = self.packet(messages)
            self.assert_group_context(packet, task)
            requested = packet["requested_element_ids"]
            self.assertFalse(saved & set(requested))
            calls.append(requested)
            saved.add(requested[0])
            return self.good(packet, requested[:1])
        report = self.run_api(book, store, api)
        self.assertEqual(report["completed"], 1)
        self.assertEqual(len(calls), 7)
        self.assertEqual(len(store.translations()), 7)
        self.assertEqual(report["failed_requests"], 0)

    def test_truncation_splits_large_group_at_element_boundaries_and_never_repeats_full_body(self):
        book, store, task = self.fixture(7)
        calls = []
        def api(provider, messages):
            packet = self.packet(messages)
            self.assert_group_context(packet, task)
            calls.append(tuple(packet["requested_element_ids"]))
            if len(calls) == 1:
                return "{", {"prompt_tokens": 100, "completion_tokens": 8192, "_truncated": True}
            return self.good(packet)
        report = self.run_api(book, store, api)
        self.assertEqual([len(x) for x in calls], [6, 3, 3, 1])
        self.assertEqual(calls.count(calls[0]), 1)
        self.assertEqual(report["completed"], 1)
        self.assertEqual(store.db.execute("SELECT count(*) FROM usage").fetchone()[0], 4)

    def test_single_remaining_truncated_member_stays_pending_once(self):
        book, store, task = self.fixture(7)
        for i in range(6):
            self.assertTrue(store.put_candidate(f"e{i}", f"已有 {i} 小问的 2 种情况 ⟦F:f1⟧。", "synthetic")["accepted"])
        calls = []
        def api(provider, messages):
            packet = self.packet(messages)
            self.assert_group_context(packet, task)
            calls.append(packet["requested_element_ids"])
            return "{", {"prompt_tokens": 100, "completion_tokens": 8192, "_truncated": True}
        report = self.run_api(book, store, api)
        self.assertEqual(calls, [["e6"]])
        self.assertEqual(report["translation_deferred_groups"], 1)
        self.assertEqual(set(store.translations()), {f"e{i}" for i in range(6)})
        self.assertEqual(report["completed"], 0)

    def test_readonly_siblings_cannot_be_submitted_as_extra_translation_ids(self):
        book, store, task = self.fixture(7)
        def api(provider, messages):
            packet = self.packet(messages)
            self.assert_group_context(packet, task)
            other = next(eid for eid in task["element_ids"] if eid not in packet["requested_element_ids"])
            return self.good(packet, [*packet["requested_element_ids"], other])
        report = self.run_api(book, store, api)
        self.assertEqual(store.translations(), {})
        self.assertEqual(report["completed"], 0)
        self.assertEqual(report["requests"], 2)

    def test_large_group_keeps_current_source_and_asset_cas_from_second_connection(self):
        book, store, task = self.fixture(7)
        other = Store(store.path)
        self.stores.append(other)
        calls = []
        def api(provider, messages):
            packet = self.packet(messages)
            self.assert_group_context(packet, task)
            calls.append(packet)
            newer = deepcopy(book)
            newer.assets["f1"].metadata["pdf_page"] = 1
            other.register(newer, {})
            other.add_tasks(tr.make_tasks(newer, {}, target_chars=100000))
            other.put_candidate("e0", "当前 0 小问的 2 种情况 ⟦F:f1⟧。", "new-source-fixture")
            return self.good(packet)
        report = self.run_api(book, store, api)
        self.assertEqual(len(calls), 1)
        self.assertEqual(other.translations(), {"e0": "当前 0 小问的 2 种情况 ⟦F:f1⟧。"})
        self.assertEqual(report["completed"], 0)

    def test_large_group_request_hard_limit_preserves_saved_packets(self):
        book, store, task = self.fixture(34)
        calls = []
        def api(provider, messages):
            packet = self.packet(messages)
            self.assert_group_context(packet, task)
            calls.append(packet)
            return self.good(packet)
        with patch.object(tr, "TRANSLATION_TASK_MAX_REQUESTS", 2):
            report = self.run_api(book, store, api)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(store.translations()), 12)
        self.assertEqual(report["completed"], 0)
        self.assertTrue(any(i["code"] == "translation_request_limit" for i in store.issues()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
