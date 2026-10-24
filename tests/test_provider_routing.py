"""Real Store + offline HTTP doubles for capacity, failover and safe resumption."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from textbook_pdf import translation as tr
from textbook_pdf.models import Book, Element
from textbook_pdf.store import Store


class ProviderRoutingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / ("textbook-routing-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.path = self.root / "config.json"
        self.config = {"_default": "pool", "providers": {
            name: {"base_url": "https://example.invalid/v1", "api_key": "test-only",
                   "model": name, "requests_per_minute": 6000000,
                   "max_attempts": 1, "max_concurrency": 2}
            for name in ("a", "b")},
            "provider_pools": {"pool": {"providers": ["a", "b"]}}}
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        shutil.rmtree(self.root)

    def store(self, n=1, *, same_task=False, kind="paragraph"):
        book = Book("self-authored.pdf", "synthetic-sha", "Routing fixture", n, list(range(1, n+1)),
                    elements=[Element("e"+str(i), kind, "There is one case.", i,
                                      chapter="same" if same_task else str(i)) for i in range(1, n+1)])
        store = Store(self.root / ("state-"+str(len(self.stores))+".db"))
        store.register(book, {})
        store.add_tasks(tr.make_tasks(book, {}, target_chars=2400 if same_task else 1))
        self.stores.append(store)
        return book, store

    def run_translation(self, book, store, api, **kw):
        self.path.write_text(json.dumps(self.config), encoding="utf8")
        with patch.object(tr, "_chat", side_effect=api):
            return tr.run_translation(book, store, self.path, workers=kw.pop("workers", 1), **kw)

    @staticmethod
    def reply(messages, *, ids=None):
        packet = json.loads(messages[-1]["content"])
        return json.dumps({"task_id": packet["task_id"], "source_hash": packet["source_hash"],
                           "translations": {eid: "这是一种情况。" for eid in
                                            (packet["requested_element_ids"] if ids is None else ids)}},
                          ensure_ascii=False), {"prompt_tokens": 20, "completion_tokens": 10}

    def test_pool_really_overlaps_routes_and_obeys_each_capacity(self):
        book, store = self.store(8)
        barrier, lock = threading.Barrier(4, timeout=5), threading.Lock()
        active, peak, names, ordinal = {}, {}, [], 0
        def api(provider, messages):
            nonlocal ordinal
            with lock:
                ordinal += 1
                count = ordinal
                active[provider.name] = active.get(provider.name, 0) + 1
                peak[provider.name] = max(peak.get(provider.name, 0), active[provider.name])
                names.append(provider.name)
            if count <= 4:
                barrier.wait()
            time.sleep(.005)
            with lock:
                active[provider.name] -= 1
            return self.reply(messages)
        report = self.run_translation(book, store, api, workers=8)
        self.assertEqual(report["completed"], 8, report)
        self.assertEqual(peak, {"a": 2, "b": 2})
        self.assertEqual(set(names), {"a", "b"})
        self.assertEqual(sum(v["requests"] for v in report["usage_by_provider"].values()), 8)

    def test_credit_failure_disables_only_route_and_records_actual_provider(self):
        book, store = self.store(2)
        calls = []
        def api(provider, messages):
            calls.append(provider.name)
            if provider.name == "a":
                raise tr._HTTPFailure(402)
            return self.reply(messages)
        report = self.run_translation(book, store, api)
        self.assertEqual(report["completed"], 2, report)
        self.assertEqual(calls.count("a"), 1)
        self.assertIsNone(report["provider_blocked"])
        self.assertEqual(report["routing"]["blocked"], {"a": 402})
        self.assertEqual({r[0] for r in store.db.execute("SELECT provider FROM translations")}, {"b"})
        self.assertEqual(report["usage_by_provider"]["a"]["requests"], 1)
        self.assertEqual(report["usage_by_provider"]["b"]["requests"], 2)
        failed = json.loads(store.db.execute("SELECT json FROM usage WHERE provider='a'").fetchone()[0])
        self.assertEqual(failed["http_status"], 402)
        self.assertFalse(failed["usage_known"])

    def test_429_honors_shared_clock_and_uses_another_route(self):
        book, store = self.store()
        session = tr.TranslationRoutingSession(self.config)
        def api(provider, messages):
            if provider.name == "a":
                raise tr._HTTPFailure(429, 60)
            return self.reply(messages)
        report = self.run_translation(book, store, api, routing_session=session)
        self.assertEqual(report["completed"], 1, report)
        self.assertIn("a", report["routing"]["cooling_down"])
        self.assertGreater(session.limiters[session.groups["a"]].next_at, time.monotonic()+58)
        self.assertEqual(report["requests"], 2)

    def test_connection_failure_fails_over_without_retranslating_neighbours(self):
        book, store = self.store(2, same_task=True)
        calls = []
        def api(provider, messages):
            packet = json.loads(messages[-1]["content"])
            calls.append((provider.name, packet["requested_element_ids"]))
            if len(calls) == 1:
                return self.reply(messages, ids=["e1"])
            if provider.name == "b":
                raise ConnectionError("transport detail must not enter state")
            return self.reply(messages)
        report = self.run_translation(book, store, api)
        self.assertEqual(report["completed"], 1, report)
        self.assertEqual([ids for _, ids in calls], [["e1", "e2"], ["e2"], ["e2"]])
        self.assertEqual(len(store.translations()), 2)

    def test_partial_reply_provenance_keeps_the_route_of_each_element(self):
        book, store = self.store(2, same_task=True)
        calls = []
        def api(provider, messages):
            calls.append(provider.name)
            return self.reply(messages, ids=["e1"] if len(calls) == 1 else ["e2"])
        report = self.run_translation(book, store, api)
        self.assertEqual(report["completed"], 1, report)
        self.assertEqual(dict(store.db.execute("SELECT element_id,provider FROM translations")),
                         {"e1": "a", "e2": "b"})

    def test_all_routes_blocked_remain_pending_and_resume_without_repeating_saved_text(self):
        book, store = self.store(2, same_task=True)
        def api(provider, messages):
            packet = json.loads(messages[-1]["content"])
            if provider.name == "a" and "e1" in packet["requested_element_ids"]:
                return self.reply(messages, ids=["e1"])
            raise tr._HTTPFailure(402)
        report = self.run_translation(book, store, api)
        self.assertEqual(store.translations(), {"e1": "这是一种情况。"})
        self.assertTrue(report["provider_blocked"])
        self.assertEqual(store.db.execute("SELECT state FROM tasks").fetchone()[0], "pending")
        with store.db:
            store.db.execute("UPDATE tasks SET next_attempt=0")
        requested = []
        def restored(provider, messages):
            requested.append(json.loads(messages[-1]["content"])["requested_element_ids"])
            return self.reply(messages)
        second = self.run_translation(book, store, restored)
        self.assertEqual(second["completed"], 1)
        self.assertEqual(requested, [["e2"]])
        self.assertEqual(dict(store.db.execute("SELECT element_id,provider FROM translations")),
                         {"e1": "a", "e2": "a"})

    def test_one_session_keeps_failed_route_disabled_between_batches(self):
        book, store = self.store(2)
        session = tr.TranslationRoutingSession(self.config)
        calls = []
        def api(provider, messages):
            calls.append(provider.name)
            if provider.name == "a":
                raise tr._HTTPFailure(403)
            return self.reply(messages)
        ids = [t["id"] for t in store.pending_tasks()]
        for tid in ids:
            report = self.run_translation(book, store, api, task_ids=[tid], routing_session=session)
            self.assertEqual(report["completed"], 1, report)
        self.assertEqual(calls, ["a", "b", "b"])

    def test_aliases_share_the_strictest_capacity_and_clock(self):
        self.config["providers"]["b"].update(model="a", max_concurrency=1, requests_per_minute=30)
        session = tr.TranslationRoutingSession(self.config)
        self.assertEqual(session.groups["a"], session.groups["b"])
        first = session.acquire()
        self.assertEqual(session.capacity[session.groups[first.name]], 1)
        limiter = session.limiters[session.groups[first.name]]
        self.assertEqual(limiter.minimum, 2)
        self.assertGreater(limiter.next_at, time.monotonic()+1.9)
        session.release(first, status=402, failed=True)
        self.assertEqual(session.snapshot()["blocked"], {"a": 402, "b": 402})
        with self.assertRaises(tr._RoutesUnavailable):
            session.acquire()

    def test_explicit_quota_group_shares_different_model_aliases(self):
        for pv in self.config["providers"].values():
            pv["rate_limit_group"] = "same-account-quota"
        session = tr.TranslationRoutingSession(self.config)
        self.assertEqual(session.groups["a"], session.groups["b"])

    def test_429_without_retry_after_cools_the_whole_alias_group(self):
        self.config["providers"]["b"]["model"] = "a"
        session = tr.TranslationRoutingSession(self.config)
        first = session.acquire()
        session.limiters[session.groups[first.name]].throttle()
        session.release(first, status=429, failed=True)
        self.assertEqual(set(session.snapshot()["cooling_down"]), {"a", "b"})
        # Cancellation while the alias waits proves it did not bypass cooldown.
        timer = threading.Timer(.03, session.stop.set)
        timer.start()
        try:
            with self.assertRaises(tr._Cancelled):
                session.acquire(excluded=[first.name])
        finally:
            timer.cancel()

    def test_exact_alias_cannot_split_capacity_using_different_group_labels_or_url_forms(self):
        self.config["providers"]["a"]["rate_limit_group"] = "first-label"
        self.config["providers"]["b"].update(model="a", rate_limit_group="second-label",
                                              base_url="https://example.invalid/v1/chat/completions")
        session = tr.TranslationRoutingSession(self.config)
        self.assertEqual(session.groups["a"], session.groups["b"])

    def test_single_provider_also_obeys_its_configured_capacity(self):
        self.config["providers"]["a"]["max_concurrency"] = 1
        book, store = self.store(8)
        lock = threading.Lock()
        active = peak = 0
        def api(provider, messages):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(.005)
            with lock:
                active -= 1
            return self.reply(messages)
        report = self.run_translation(book, store, api, provider="a", workers=8)
        self.assertEqual(report["completed"], 8, report)
        self.assertEqual(peak, 1)

    def test_review_inherits_same_account_rate_clock_and_blocked_state(self):
        self.config["providers"]["review"] = dict(self.config["providers"]["a"],
            model="full-review", rate_limit_group="shared")
        self.config["providers"]["a"]["rate_limit_group"] = "shared"
        reviewer = tr._load_provider(self.config, "review")
        session = tr.TranslationRoutingSession(self.config, extra_providers=[reviewer])
        self.assertEqual(session.groups["a"], session.groups["review"])
        selected = session.acquire()
        session.release(selected, status=402, failed=True)
        book, store = self.store()
        task = store.claim("fixture")
        store.submit(task["id"], "fixture", task["source_hash"], {"e1": "这是一种情况。"})
        store.record_issue({"code": "semantic_review_pending", "element_id": "e1"})
        self.path.write_text(json.dumps(self.config), encoding="utf8")
        with patch.object(tr, "_chat", side_effect=AssertionError("A blocked quota must not be called")):
            report = tr.run_review(book, store, self.path, "review", routing_session=session)
        self.assertEqual(report["requests"], 0)
        self.assertEqual(report["provider_blocked"]["http_status"], 402)

    def test_review_cannot_bypass_shared_group_cooldown(self):
        self.config["providers"]["review"] = dict(self.config["providers"]["a"],
            model="review", rate_limit_group="shared")
        self.config["providers"]["a"]["rate_limit_group"] = "shared"
        reviewer = tr._load_provider(self.config, "review")
        session = tr.TranslationRoutingSession(self.config, extra_providers=[reviewer])
        first = session.acquire()
        session.release(first, status=429, failed=True)
        book, store = self.store(kind="exercise")
        task = store.claim("fixture")
        store.submit(task["id"], "fixture", task["source_hash"], {"e1": "这是一种情况。"})
        self.path.write_text(json.dumps(self.config), encoding="utf8")
        timer = threading.Timer(.03, session.stop.set)
        timer.start()
        try:
            with patch.object(tr, "_chat", side_effect=AssertionError("Cooldown was bypassed")):
                report = tr.run_review(book, store, self.path, "review", routing_session=session)
        finally:
            timer.cancel()
        self.assertEqual(report["requests"], 0)
        self.assertTrue(report["cancelled"])
        self.assertEqual(report["review_pending"], 1)

    def test_review_uses_strictest_shared_capacity(self):
        self.config["providers"]["review"] = dict(self.config["providers"]["a"],
            model="review", rate_limit_group="shared", max_concurrency=8)
        self.config["providers"]["a"].update(rate_limit_group="shared", max_concurrency=1)
        reviewer = tr._load_provider(self.config, "review")
        session = tr.TranslationRoutingSession(self.config, extra_providers=[reviewer])
        book, store = self.store(18, kind="exercise")
        for row in store.pending_tasks():
            task = store.claim("fixture", task_id=row["id"])
            store.submit(task["id"], "fixture", task["source_hash"],
                         {eid: "这是一种情况。" for eid in task["element_ids"]})
        self.path.write_text(json.dumps(self.config), encoding="utf8")
        def api(provider, messages):
            packet = json.loads(messages[-1]["content"])
            return json.dumps(packet["required_response"]), {"prompt_tokens": 10, "completion_tokens": 10}
        with patch.object(tr, "_chat", side_effect=api):
            report = tr.run_review(book, store, self.path, "review", workers=8, routing_session=session)
        self.assertEqual(report["workers_actual"], 1)
        self.assertEqual(report["review_passed"], 18)
        self.assertFalse(any(session.active.values()))

    def test_failed_review_route_does_not_stop_healthy_translation_pool(self):
        self.config["providers"]["review"] = dict(self.config["providers"]["a"], model="review")
        reviewer = tr._load_provider(self.config, "review")
        session = tr.TranslationRoutingSession(self.config, extra_providers=[reviewer])
        book, store = self.store(2, kind="exercise")
        task = store.claim("fixture", task_id=next(t["id"] for t in store.pending_tasks() if "e1" in t["element_ids"]))
        store.submit(task["id"], "fixture", task["source_hash"], {"e1": "这是一种情况。"})
        self.path.write_text(json.dumps(self.config), encoding="utf8")
        with patch.object(tr, "_chat", side_effect=tr._HTTPFailure(402)):
            report = tr.run_review(book, store, self.path, "review", routing_session=session)
        self.assertEqual(report["requests"], 1)
        self.assertFalse(session.stop.is_set())
        translated = self.run_translation(book, store, lambda p,m: self.reply(m), routing_session=session)
        self.assertEqual(translated["completed"], 1, translated)
        self.assertEqual(len(store.translations()), 2)

    def test_review_route_becoming_unavailable_before_http_does_not_invent_usage(self):
        self.config["providers"]["review"] = dict(self.config["providers"]["a"], model="review")
        reviewer = tr._load_provider(self.config, "review")
        session = tr.TranslationRoutingSession(self.config, extra_providers=[reviewer])
        book, store = self.store(kind="exercise")
        task = store.claim("fixture")
        store.submit(task["id"], "fixture", task["source_hash"], {"e1": "这是一种情况。"})
        self.path.write_text(json.dumps(self.config), encoding="utf8")
        with patch.object(session, "acquire", side_effect=tr._RoutesUnavailable("blocked")), \
                patch.object(tr, "_chat", side_effect=AssertionError("No HTTP may be sent")):
            report = tr.run_review(book, store, self.path, "review", routing_session=session)
        self.assertEqual((report["requests"], report["failed_requests"]), (0, 0))
        self.assertEqual(store.db.execute("SELECT count(*) FROM usage").fetchone()[0], 0)
        self.assertEqual(report["review_pending"], 1)

    def test_inline_review_failure_is_isolated_from_healthy_translation_routes(self):
        self.config["providers"]["review"] = dict(self.config["providers"]["a"], model="independent-review")
        book, store = self.store(4, kind="exercise")
        calls = []
        def api(provider, messages):
            calls.append(provider.name)
            if provider.name == "review":
                raise tr._HTTPFailure(402)
            return self.reply(messages)
        report = self.run_translation(book, store, api, review_provider="review")
        self.assertEqual(report["completed"], 4, report)
        self.assertEqual(calls.count("review"), 1)
        self.assertIsNone(report["provider_blocked"])
        self.assertFalse(report["cancelled"])
        self.assertEqual(len(store.translations()), 4)
        self.assertEqual(tr.review_status(book, store)["pending"], 4)

    def test_configuration_change_cannot_reuse_old_session(self):
        book, store = self.store()
        session = tr.TranslationRoutingSession(self.config)
        self.config["providers"]["b"]["model"] = "different"
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            self.run_translation(book, store, lambda *a: self.fail("Unexpected request"), routing_session=session)
        self.assertEqual(store.translations(), {})

    def test_single_provider_selection_never_implicitly_enables_pool(self):
        book, store = self.store()
        calls = []
        def api(provider, messages):
            calls.append(provider.name)
            return self.reply(messages)
        report = self.run_translation(book, store, api, provider="b")
        self.assertEqual(report["provider"], "b")
        self.assertEqual(calls, ["b"])

    def test_invalid_pool_is_rejected_before_claim_or_request(self):
        invalid = [None, [], {}, {"providers": []}, {"providers": ["missing"]},
                   {"providers": ["a", "a"]}, {"providers": ["pool"]},
                   {"providers": ["a"]*17}]
        for value in invalid:
            c = deepcopy(self.config)
            c["provider_pools"]["pool"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                tr.TranslationRoutingSession(c)
        self.assertNotIn("test-only", json.dumps(tr.TranslationRoutingSession(self.config).snapshot()))


if __name__ == "__main__":
    unittest.main()
