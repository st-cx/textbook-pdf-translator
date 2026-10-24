from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from textbook_pdf.desktop import (
    DesktopApp, JobWorker, ProviderChoices, open_directory,
    default_runner, read_provider_choices, safe_message, validate_inputs,
)


class _InputFixture:
    def setUp(self):
        self.folder = Path(tempfile.gettempdir()) / ("textbook-desktop-test-" + uuid.uuid4().hex)
        self.folder.mkdir()
        self.config = self.folder / "config.json"
        self.config.write_text(json.dumps({"_default": "translate", "providers": {
            "translate": {"model": "example/translation", "api_key": "private-example-credential"},
            "independent-review": {"model": "example/review", "api_key": "another-private-credential"},
        }}), encoding="utf-8")
        self.pdf = self.folder / "original.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nsynthetic UI input only")
        self.choices = read_provider_choices(self.config)
        self.values = {
            "pdf": str(self.pdf), "out": str(self.folder / "new-job"),
            "config": str(self.config), "pages": "", "workers": "8",
            "provider": self.choices.labels[0], "review_provider": self.choices.labels[1],
            "review_level": "快速抽查",
        }

    def tearDown(self):
        shutil.rmtree(self.folder)


class DesktopInputsTests(_InputFixture, unittest.TestCase):

    def test_translation_parallelism_defaults_to_32_and_batch_tracks_manual_value(self):
        values = dict(self.values)
        values.pop("workers")
        options = validate_inputs(values, self.choices)
        self.assertEqual((options["workers"], options["batch_size"]), (32, 32))
        for number in (1, 4, 16, 32):
            with self.subTest(number=number):
                options = validate_inputs(dict(values, workers=str(number)), self.choices)
                self.assertEqual((options["workers"], options["batch_size"]), (number, number))

    def test_pool_is_translation_choice_and_review_stays_single_provider(self):
        data = json.loads(self.config.read_text())
        data.update(_default="balanced", _review_default="independent-review",
                    provider_pools={"balanced": {"providers": ["translate", "independent-review"]}})
        self.config.write_text(json.dumps(data), encoding="utf-8")
        before = self.config.read_bytes()
        choices = read_provider_choices(self.config)
        self.assertEqual(choices.pool_names, ("balanced",))
        self.assertEqual(choices.default, "balanced")
        label = choices.label_for("balanced")
        self.assertIn("多路由 / 2个供应商", label)
        self.assertNotIn(label, choices.review_labels)
        self.assertIsNone(choices.review_name_for(label))
        options = validate_inputs(dict(self.values, provider=label), choices)
        self.assertEqual(options["provider"], "balanced")
        self.assertEqual(options["review_provider"], "independent-review")
        with self.assertRaises(ValueError):
            validate_inputs(dict(self.values, provider=label, review_provider=label), choices)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertNotIn("private-example-credential", repr(choices))

    def test_invalid_pools_are_rejected_without_echoing_configuration(self):
        original = json.loads(self.config.read_text())
        invalid = (
            {"pool": {"providers": []}},
            {"pool": {"providers": ["missing-private-example-credential"]}},
            {"pool": {"providers": ["translate", "translate"]}},
            {"translate": {"providers": ["translate"]}},
            {"pool": ["translate"]},
            {"pool": {"providers": "translate"}},
            {"pool": {"providers": [1]}},
            {"pool": {"providers": ["translate"] * 17}},
            {" ": {"providers": ["translate"]}},
            [], None,
        )
        for pools in invalid:
            with self.subTest(pools=pools):
                self.config.write_text(json.dumps(dict(original, provider_pools=pools)), encoding="utf-8")
                with self.assertRaises(ValueError) as caught:
                    read_provider_choices(self.config)
                self.assertNotIn("private-example-credential", str(caught.exception))

    def test_old_provider_choices_positional_fields_remain_compatible(self):
        choices = ProviderChoices(("single label",), ("single",), "single", ("secret",), "single")
        self.assertEqual(choices.review_default, "single")
        self.assertEqual(choices.pool_names, ())
        self.assertEqual(choices.review_labels, ("single label",))

    def test_configuration_is_read_only_and_keys_never_enter_choices_repr(self):
        before = self.config.read_bytes()
        choices = read_provider_choices(self.config)
        self.assertEqual(choices.names, ("translate", "independent-review"))
        self.assertEqual(choices.default, "translate")
        self.assertIn("example/translation", choices.labels[0])
        self.assertNotIn("private-example-credential", repr(choices))
        self.assertEqual(self.config.read_bytes(), before)

    def test_default_provider_alias_and_bom(self):
        self.config.write_text(json.dumps({"default_provider": "b", "_review_default": "a", "providers": {
            "a": {"model": "a"}, "b": {"model": "b"}}}), encoding="utf-8-sig")
        self.assertEqual(read_provider_choices(self.config).default, "b")
        self.assertEqual(read_provider_choices(self.config).review_default, "a")

    def test_bad_config_errors_hide_source_body(self):
        self.config.write_text("not-json private-example-credential", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "无法读取配置文件") as caught:
            read_provider_choices(self.config)
        self.assertNotIn("private-example-credential", str(caught.exception))

    def test_new_job_exact_options_and_no_directory_write(self):
        options = validate_inputs(self.values, self.choices)
        self.assertEqual(options["workers"], 8)
        self.assertEqual(options["batch_size"], 8)
        self.assertEqual(options["provider"], "translate")
        self.assertEqual(options["review_provider"], "independent-review")
        self.assertIs(options["review"], True)
        self.assertEqual(options["max_review_elements"], 32)
        self.assertIsNone(options["max_tasks"])
        self.assertFalse(Path(options["out"]).exists())

    def test_review_levels_preserve_independent_provider(self):
        self.values["review_level"] = "较严抽查"
        options = validate_inputs(self.values, self.choices)
        self.assertEqual(options["max_review_elements"], 128)
        self.values["review_level"] = "先翻译"
        self.assertIs(validate_inputs(self.values, self.choices)["review"], False)

    def test_resume_does_not_parse_book_or_require_pdf(self):
        job = self.folder / "job"
        job.mkdir()
        # Deliberately non-JSON: the UI must not read the large book or claim it is valid.
        (job / "book.json").write_text("autopilot validates this", encoding="utf-8")
        self.values.update(pdf="", out=str(job))
        options = validate_inputs(self.values, self.choices)
        self.assertIsNone(options["pdf"])

    def test_draft_only_never_requests_translation_or_review(self):
        job = self.folder / "job"
        job.mkdir()
        (job / "book.json").touch()
        self.values.update(pdf="", out=str(job), provider="", review_provider="", config=str(self.folder / "missing.json"))
        options = validate_inputs(self.values, self.choices, draft_only=True)
        self.assertEqual(options["max_tasks"], 0)
        self.assertIs(options["review"], False)
        self.assertIs(options["draft"], True)
        self.assertIsNone(options["config_path"])

    def test_pages_validate_before_worker(self):
        self.values["pages"] = "1-20，25"
        self.assertEqual(validate_inputs(self.values, self.choices)["pages"], "1-20,25")
        for invalid in ("0", "4-2", "abc", "1,,2"):
            with self.subTest(invalid=invalid):
                self.values["pages"] = invalid
                with self.assertRaises(ValueError):
                    validate_inputs(self.values, self.choices)

    def test_missing_paths_invalid_workers_and_stale_provider_are_rejected(self):
        for update in ({"pdf": ""}, {"out": ""}, {"workers": "0"},
                       {"workers": "33"}, {"workers": "8.2"}, {"provider": "removed"},
                       {"review_provider": "removed"}, {"review_level": "full"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_inputs(dict(self.values, **update), self.choices)

    def test_open_nonexistent_directory_never_launches_external_program(self):
        with patch("subprocess.Popen") as launch, self.assertRaises(ValueError):
            open_directory(self.folder / "missing")
        launch.assert_not_called()

    def test_logs_mask_credentials_and_multiline_headers(self):
        result = safe_message("private-example-credential\nAuthorization: Bearer ABC123 token=XYZ sk-abcdefghi https://user:password@example.invalid",
                              self.choices.secrets)
        for secret in ("private-example-credential", "ABC123", "XYZ", "sk-abcdefghi", "password"):
            self.assertNotIn(secret, result)
        self.assertNotIn("\n", result)


class DesktopWorkerTests(unittest.TestCase):
    def test_actual_autopilot_options_interface_without_any_job_or_request(self):
        from textbook_pdf.autopilot import AutoOptions
        cancel = threading.Event()
        options = {"pdf": None, "out": "synthetic-job", "config_path": None,
                   "provider": None, "review_provider": None, "workers": 8,
                   "batch_size": 8, "pages": None, "review": False,
                   "max_review_elements": 32, "max_tasks": 0, "draft": True,
                   "cancel_event": cancel}
        callback = lambda event: True
        with patch("textbook_pdf.autopilot.run_job", return_value={"state": "incomplete"}) as run:
            self.assertEqual(default_runner(options, callback), {"state": "incomplete"})
        args = run.call_args.args
        self.assertIsInstance(args[0], AutoOptions)
        self.assertIs(args[0].cancel_event, cancel)
        self.assertEqual(args[0].max_tasks, 0)
        self.assertIs(args[1], callback)

    def test_queued_event_is_deep_copied(self):
        def runner(options, callback):
            event = {"summary": {"accepted": 3}}
            callback(event)
            event["summary"]["accepted"] = 100
            return {"state": "incomplete"}
        worker = JobWorker(runner)
        worker.start({})
        worker.thread.join(2)
        self.assertEqual(worker.events.get_nowait()[1]["summary"]["accepted"], 3)

    def test_worker_runs_on_background_thread_and_copies_options(self):
        main_ident = threading.get_ident()
        seen = []

        def runner(options, callback):
            seen.append((threading.get_ident(), options))
            callback({"stage": "translate", "current": 1, "total": 2, "progress": .5})
            return {"state": "incomplete"}

        worker = JobWorker(runner)
        options = {"workers": 8}
        worker.start(options)
        worker.thread.join(2)
        self.assertNotEqual(seen[0][0], main_ident)
        self.assertNotIn("cancel_event", options)
        self.assertIs(seen[0][1]["cancel_event"], worker.cancel_event)
        self.assertEqual(worker.events.get_nowait()[0], "progress")
        self.assertEqual(worker.events.get_nowait(), ("done", {"state": "incomplete"}))

    def test_safe_stop_uses_event_and_callback_without_killing_worker(self):
        entered, release = threading.Event(), threading.Event()
        callbacks = []

        def runner(options, callback):
            entered.set()
            release.wait(2)
            callbacks.append(callback({"stage": "finished", "state": "stopped"}))
            return {"state": "stopped"}

        worker = JobWorker(runner)
        worker.start({})
        self.assertTrue(entered.wait(2))
        with self.assertRaises(RuntimeError):
            worker.start({})
        worker.stop()
        self.assertTrue(worker.running)
        release.set()
        worker.thread.join(2)
        self.assertEqual(callbacks, [False])
        self.assertEqual(worker.events.get_nowait()[0], "progress")
        self.assertEqual(worker.events.get_nowait()[1]["state"], "stopped")

    def test_failure_and_restart_do_not_replay_stale_events(self):
        def fail(options, callback):
            raise RuntimeError("synthetic failure")
        worker = JobWorker(fail)
        worker.start({})
        worker.thread.join(2)
        self.assertEqual(worker.events.get_nowait(), ("error", ("RuntimeError", "synthetic failure")))
        worker.events.put(("done", {"state": "old"}))
        worker.runner = lambda options, callback: {"state": "finished"}
        worker.start({})
        worker.thread.join(2)
        self.assertEqual(worker.events.get_nowait(), ("done", {"state": "finished"}))
        self.assertTrue(worker.events.empty())


class DesktopTkTests(_InputFixture, unittest.TestCase):
    """Real Tk widgets with synthetic files and runners; no service calls."""
    def setUp(self):
        super().setUp()
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest("Tk display unavailable: " + str(exc))
        self.root.withdraw()
        self.app = DesktopApp(self.root, config_path=self.config, runner=lambda o, c: {"state": "incomplete"})
        self.initial_workers = self.app.vars["workers"].get()
        for key, value in self.values.items():
            self.app.vars[key].set(value)

    def tearDown(self):
        if hasattr(self, "root"):
            if self.app.worker.running:
                self.app.worker.stop()
                self.app.worker.thread.join(3)
            try:
                self.app.close()
            except Exception:
                pass
        super().tearDown()

    def pump_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_actual_widgets_start_progress_stop_and_finish(self):
        entered, release = threading.Event(), threading.Event()
        options_seen = []

        def runner(options, callback):
            options_seen.append(options)
            callback({"stage": "translate", "message": "已保存一个批次", "progress": .5,
                      "current": 5, "total": 10, "speed_elements_per_minute": 2})
            entered.set()
            release.wait(3)
            return {"state": "stopped", "pdf_path": str(self.folder / "reading.pdf")}

        self.app.worker.runner = runner
        self.app.start_button.invoke()
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.app._busy)
        self.assertEqual(str(self.app.provider_combo.cget("state")), "disabled")
        self.pump_until(lambda: float(self.app.progress["value"]) == 50)
        self.assertIn("5 / 10", self.app.detail.get())
        self.app.stop_button.invoke()
        self.assertTrue(options_seen[0]["cancel_event"].is_set())
        release.set()
        self.pump_until(lambda: not self.app._busy)
        self.assertIn("已安全停止", self.app.status.get())
        self.assertEqual(str(self.app.provider_combo.cget("state")), "readonly")
        self.assertIn("reading.pdf", self.app.log.get("1.0", "end"))

    def test_manual_translation_parallelism_widgets_4_16_32_and_resume(self):
        self.assertEqual(self.initial_workers, "32")
        self.assertEqual(str(self.app.workers_spin.cget("state")), "normal")
        seen = []
        for number in (4, 16, 32):
            release = threading.Event()

            def runner(options, callback):
                seen.append(options)
                release.wait(2)
                return {"state": "stopped"}

            self.app.worker.runner = runner
            self.app.workers_spin.delete(0, "end")
            self.app.workers_spin.insert(0, str(number))
            self.app.start_button.invoke()
            self.assertEqual(str(self.app.workers_spin.cget("state")), "disabled")
            self.pump_until(lambda: len(seen) == (4, 16, 32).index(number) + 1)
            self.assertEqual((seen[-1]["workers"], seen[-1]["batch_size"]), (number, number))
            self.app.stop_button.invoke()
            self.assertTrue(seen[-1]["cancel_event"].is_set())
            release.set()
            self.pump_until(lambda: not self.app._busy)
            self.assertEqual(str(self.app.workers_spin.cget("state")), "normal")

    def test_pool_default_translation_and_real_review_selection_in_actual_widgets(self):
        data = json.loads(self.config.read_text())
        data.update(_default="balanced", _review_default="independent-review",
                    provider_pools={"balanced": {"providers": ["translate", "independent-review"]}})
        self.config.write_text(json.dumps(data), encoding="utf-8")
        self.app.vars["provider"].set("")
        self.app.vars["review_provider"].set("")
        self.app.load_config()
        pool_label = self.app.choices.label_for("balanced")
        self.assertEqual(self.app.vars["provider"].get(), pool_label)
        self.assertIn(pool_label, self.app.provider_combo.cget("values"))
        self.assertNotIn(pool_label, self.app.review_provider_combo.cget("values"))
        self.assertEqual(self.app.choices.review_name_for(self.app.vars["review_provider"].get()), "independent-review")
        seen = []
        self.app.worker.runner = lambda opts, cb: seen.append(opts) or {"state": "incomplete"}
        self.app.start_button.invoke()
        self.pump_until(lambda: not self.app._busy)
        self.assertEqual(seen[0]["provider"], "balanced")
        self.assertEqual(seen[0]["review_provider"], "independent-review")
        self.app.vars["provider"].set(self.app.choices.label_for("translate"))
        self.app.load_config()
        self.assertEqual(self.app.choices.name_for(self.app.vars["provider"].get()), "translate")

    def test_pool_without_dedicated_review_falls_back_to_real_provider(self):
        self.config.write_text(json.dumps({"_default": "one-pool", "providers": {
            "single": {"model": "route/model"}}, "provider_pools": {
            "one-pool": {"providers": ["single"]}}}), encoding="utf-8")
        self.app.load_config()
        self.assertEqual(self.app.choices.name_for(self.app.vars["provider"].get()), "one-pool")
        self.assertEqual(self.app.choices.review_name_for(self.app.vars["review_provider"].get()), "single")

    def test_file_and_existing_job_buttons_are_simulated(self):
        with patch("textbook_pdf.desktop.filedialog.askopenfilename", return_value=str(self.pdf)):
            self.app.vars["out"].set("")
            self.app.choose_pdf()
        self.assertEqual(self.app.vars["pdf"].get(), str(self.pdf))
        self.assertTrue(self.app.vars["out"].get().endswith("original_翻译任务"))
        job = self.folder / "resume"
        job.mkdir()
        (job / "book.json").touch()
        with patch("textbook_pdf.desktop.filedialog.askdirectory", return_value=str(job)):
            self.app.choose_job()
        self.assertEqual(self.app.vars["out"].get(), str(job))
        self.assertEqual(self.app.vars["pdf"].get(), "")
        self.assertEqual(self.app.vars["pages"].get(), "")

    def test_draft_button_passes_zero_tasks_and_no_review(self):
        seen = []
        job = self.folder / "resume"
        job.mkdir()
        (job / "book.json").touch()
        self.app.vars["out"].set(str(job))
        self.app.vars["pdf"].set("")
        self.app.worker.runner = lambda opts, cb: seen.append(opts) or {"state": "finished"}
        self.app.draft_button.invoke()
        self.pump_until(lambda: not self.app._busy)
        self.assertEqual(seen[0]["max_tasks"], 0)
        self.assertIs(seen[0]["review"], False)

    def test_bad_input_does_not_start_worker_and_error_masks_key(self):
        self.app.vars["pages"].set("0")
        with patch("textbook_pdf.desktop.messagebox.showerror") as error:
            self.app.start()
        error.assert_called_once()
        self.assertIsNone(self.app.worker.thread)
        self.app.vars["pages"].set("")
        self.app.worker.runner = lambda opts, cb: (_ for _ in ()).throw(RuntimeError("private-example-credential"))
        self.app.start()
        self.pump_until(lambda: not self.app._busy)
        self.assertNotIn("private-example-credential", self.app.log.get("1.0", "end"))
        self.assertIn("[已隐藏]", self.app.log.get("1.0", "end"))

    def test_review_default_and_config_button_preserve_explicit_selection(self):
        self.config.write_text(json.dumps({"_default": "first", "_review_default": "second", "providers": {
            "first": {"model": "route/first"}, "second": {"model": "route/second"}}}), encoding="utf-8")
        with patch("textbook_pdf.desktop.filedialog.askopenfilename", return_value=str(self.config)):
            self.app.choose_config()
        self.assertEqual(self.app.choices.name_for(self.app.vars["provider"].get()), "first")
        self.assertEqual(self.app.choices.name_for(self.app.vars["review_provider"].get()), "second")
        self.app.vars["review_provider"].set(self.app.choices.labels[0])
        self.app.load_config()
        self.assertEqual(self.app.choices.name_for(self.app.vars["review_provider"].get()), "first")

    def test_reading_draft_works_without_config_and_log_remains_a_preview(self):
        job = self.folder / "resume-no-config"
        job.mkdir()
        (job / "book.json").touch()
        self.app.vars["out"].set(str(job))
        self.app.vars["pdf"].set("")
        self.app.vars["config"].set(str(self.folder / "absent.json"))
        self.app.choices = ProviderChoices((), (), None)
        seen = []
        self.app.worker.runner = lambda opts, cb: seen.append(opts) or {"state": "finished"}
        self.app.draft_button.invoke()
        self.pump_until(lambda: not self.app._busy)
        self.assertIsNone(seen[0]["config_path"])
        self.assertEqual(seen[0]["max_tasks"], 0)
        self.assertIn("不代表整本验收通过", self.app.detail.get())


if __name__ == "__main__":
    unittest.main()
