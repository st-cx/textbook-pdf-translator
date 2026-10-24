"""Resumable, bounded desktop/CLI orchestration over the ordinary Store API.

Cancellation and time budgets are cooperative at batch boundaries. A reading
draft is a preview, never a claim that source or translation QA has passed.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass, replace
import json
import math
import os
from pathlib import Path
import time
import uuid

from .cli import _source_preflight, load_glossary, parse_pages
from .extraction import analyze
from .models import Book, digest, file_sha256, needs_translation, source_contract_issues, source_error_scope
from .store import Store
from .translation import _current_task, _load_provider, make_tasks, run_review, run_translation


@dataclass
class AutoOptions:
    pdf: str | Path | None = None
    out: str | Path | None = None
    config_path: str | Path | None = None
    provider: str | None = None
    review_provider: str | None = None
    workers: int = 8
    batch_size: int = 8
    target_chars: int = 2400
    pages: str | list[int] | None = None
    backend: str = "auto"
    ocr: str = "auto"
    ocr_dpi: int = 300
    glossary: str | Path | None = None
    glossary_path: str | Path | None = None
    review: bool = False
    max_review_elements: int | None = 32
    draft: bool = True
    max_tasks: int | None = None
    max_seconds: float | None = None
    stop_file: str | Path | None = None
    cancel_event: object | None = None


class _Refusal(Exception):
    def __init__(self, reason, message, state="error"):
        self.reason, self.message, self.state = reason, message, state


class _DraftStopped(Exception):
    pass


def _paths(options):
    pdf = Path(options.pdf).expanduser().resolve() if options.pdf else None
    if options.out:
        out = Path(options.out).expanduser().resolve()
    elif pdf:
        out = pdf.parent / (pdf.stem + "_翻译任务")
    else:
        raise _Refusal("missing_input", "请选择 PDF 或已有任务目录。")
    config = Path(options.config_path).expanduser().resolve() if options.config_path else Path(__file__).resolve().parent.parent / "config.json"
    return pdf, out, config


def _pages(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return parse_pages(value)
    if not isinstance(value, (list, tuple)) or any(type(n) is not int or n < 1 for n in value):
        raise ValueError("invalid page selection")
    return sorted(set(value))


def _validate(options):
    if options.glossary is not None and options.glossary_path is not None and Path(options.glossary).resolve() != Path(options.glossary_path).resolve():
        raise _Refusal("invalid_options", "术语表的两个参数指向不同文件。")
    for name, low, high in (("workers", 1, 32), ("batch_size", 1, 128),
                            ("target_chars", 1, 100000), ("ocr_dpi", 72, 1200)):
        value = getattr(options, name)
        if type(value) is not int or not low <= value <= high:
            raise _Refusal("invalid_options", "运行参数超出允许范围。")
    for name in ("max_tasks", "max_review_elements"):
        value = getattr(options, name)
        if value is not None and (type(value) is not int or value < 0):
            raise _Refusal("invalid_options", "任务和抽查数量必须为非负整数。")
    if options.max_seconds is not None and (isinstance(options.max_seconds, bool)
            or not isinstance(options.max_seconds, (int, float))
            or not math.isfinite(options.max_seconds) or options.max_seconds < 0):
        raise _Refusal("invalid_options", "时间预算必须为有限非负数。")
    try:
        return _pages(options.pages)
    except (ValueError, TypeError):
        raise _Refusal("invalid_pages", "页码范围无效。") from None


def _providers(config, options):
    try:
        data = json.loads(config.read_text(encoding="utf-8-sig"))
        selected = _load_provider(data, options.provider)
        if options.review:
            _load_provider(data, options.review_provider or selected.name)
        return selected.name
    except Exception:
        raise _Refusal("provider_unavailable", "供应商配置缺失或无效，请先检查设置。") from None


def check_readiness(options: AutoOptions) -> dict:
    """Check local inputs and provider shape without making an API request."""
    try:
        _validate(options)
        pdf, out, config = _paths(options)
        if pdf is not None and not pdf.is_file():
            raise _Refusal("missing_pdf", "找不到所选 PDF 文件。")
        if pdf is None and not (out / "tasks.db").is_file() and not (out / "book.json").is_file():
            raise _Refusal("missing_job", "任务目录没有可续跑的数据库。")
        if options.max_tasks != 0:
            _providers(config, options)
        return {"ready": True, "state": "ready", "job_dir": str(out),
                "message": "本地设置已就绪；尚未发送任何请求。"}
    except _Refusal as error:
        return {"ready": False, "state": error.state, "reason": error.reason, "message": error.message}
    except Exception:
        return {"ready": False, "state": "error", "reason": "readiness_failed", "message": "无法检查本地设置。"}


@contextmanager
def _job_lock(out):
    """An OS lock survives stale lock files and is released on process exit."""
    out.mkdir(parents=True, exist_ok=True)
    stream = (out / ".autopilot.lock").open("a+b")
    locked = False
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0"); stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            raise _Refusal("job_busy", "此任务正在另一个窗口运行。", "busy") from None
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class _JSONStream:
    """A bounded JSON value decoder; document arrays are walked separately."""
    def __init__(self, stream, entry_bytes):
        self.stream, self.limit = stream, entry_bytes
        self.buffer, self.position, self.bytes, self.eof = "", 0, 0, False

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result

        def nonstandard(value):
            raise ValueError("Nonstandard JSON constant")

        self.decoder = json.JSONDecoder(object_pairs_hook=unique, parse_constant=nonstandard)

    def more(self):
        if self.position:
            self.buffer = self.buffer[self.position:]
            self.position = 0
            self.bytes = len(self.buffer.encode("utf-8"))
        chunk = self.stream.read(65536)
        self.eof = not chunk
        self.buffer += chunk
        self.bytes += len(chunk.encode("utf-8"))

    def peek(self):
        while True:
            while self.position < len(self.buffer) and self.buffer[self.position] in " \t\r\n":
                self.position += 1
            if self.position < len(self.buffer):
                return self.buffer[self.position]
            if self.eof:
                return ""
            self.more()

    def expect(self, character):
        if self.peek() != character:
            raise ValueError("Invalid JSON structure")
        self.position += 1

    def value(self):
        if not self.peek():
            raise ValueError("Truncated JSON value")
        # Drop previously consumed siblings before buffering this one entry.
        if self.position:
            self.buffer = self.buffer[self.position:]
            self.position = 0
            self.bytes = len(self.buffer.encode("utf-8"))
        while True:
            if self.bytes > self.limit:
                raise ValueError("JSON entry exceeds memory bound")
            try:
                value, end = self.decoder.raw_decode(self.buffer)
            except json.JSONDecodeError:
                if self.eof:
                    raise ValueError("Invalid or truncated JSON value") from None
                self.more()
                continue
            # Numbers can be split immediately before a decimal/exponent. Do
            # not accept a valid prefix until its following delimiter is known.
            if end == len(self.buffer) and not self.eof:
                self.more()
                continue
            if end < len(self.buffer) and self.buffer[end] not in " \t\r\n,]}:":
                if type(value) in (int, float) and self.buffer[end] in ".eE+-0123456789" and not self.eof:
                    self.more()
                    continue
                raise ValueError("Invalid JSON token boundary")
            self.position = end
            return value

    def key(self):
        value = self.value()
        if not isinstance(value, str):
            raise ValueError("JSON object key is not text")
        self.expect(":")
        return value


def _json_same(actual, expected):
    """JSON semantics: object order is irrelevant; booleans remain distinct."""
    if is_dataclass(expected) and not isinstance(expected, type):
        if not isinstance(actual, dict):
            return False
        try:
            actual = type(expected)(**actual)
        except (TypeError, ValueError):
            return False
        return all(_json_same(getattr(actual, field.name), getattr(expected, field.name)) for field in fields(expected))
    if isinstance(expected, dict):
        return isinstance(actual, dict) and actual.keys() == expected.keys() and all(_json_same(actual[key], value) for key, value in expected.items())
    if isinstance(expected, (list, tuple)):
        return isinstance(actual, (list, tuple)) and len(actual) == len(expected) and all(_json_same(a, b) for a, b in zip(actual, expected))
    if type(expected) in (int, float):
        return (type(actual) in (int, float)
                and not (isinstance(actual, float) and not math.isfinite(actual))
                and not (isinstance(expected, float) and not math.isfinite(expected)) and actual == expected)
    return type(actual) is type(expected) and actual == expected


def _book_file_matches(book, path, *, entry_bytes=64 * 1024 * 1024):
    """Semantic comparison with one resident Book and bounded entry decoding.

    Large arrays and the asset object are never decoded as a whole. Formatting,
    object key order, JSON escapes, and 1 versus 1.0 do not change identity.
    Duplicate/unknown/missing fields and incomplete input fail closed.
    """
    path = Path(path)
    if not path.is_file():
        return False

    def array(reader, expected):
        reader.expect("[")
        for index, value in enumerate(expected):
            if index:
                reader.expect(",")
            if not _json_same(reader.value(), value):
                return False
        reader.expect("]")
        return True

    def assets(reader):
        reader.expect("{")
        seen = set()
        while reader.peek() != "}":
            if seen:
                reader.expect(",")
            key = reader.key()
            if key in seen or key not in book.assets:
                return False
            seen.add(key)
            if not _json_same(reader.value(), book.assets[key]):
                return False
        reader.expect("}")
        return seen == book.assets.keys()

    try:
        with path.open("r", encoding="utf-8-sig", newline=None) as stream:
            reader = _JSONStream(stream, entry_bytes)
            required = {field.name for field in fields(book)}
            seen = set()
            reader.expect("{")
            while reader.peek() != "}":
                if seen:
                    reader.expect(",")
                key = reader.key()
                if key in seen or key not in required:
                    return False
                seen.add(key)
                expected = getattr(book, key)
                if key in {"elements", "pages", "issues"}:
                    if not array(reader, expected):
                        return False
                elif key == "assets":
                    if not assets(reader):
                        return False
                elif not _json_same(reader.value(), expected):
                    return False
            reader.expect("}")
            return seen == required and reader.peek() == ""
    except (ValueError, TypeError, OSError, UnicodeError, RecursionError, OverflowError):
        return False


def compose_reading_draft(*args, **kwargs):
    # Keep the UI import light and let callers test this rendering boundary.
    from .reading_draft import compose_reading_draft as compose
    return compose(*args, **kwargs)


def _revision(store):
    row = store.db.execute("SELECT value FROM metadata WHERE key='book_registration_revision'").fetchone()
    return row[0] if row else None


def _live_leases(store):
    return store.db.execute("SELECT count(*) FROM tasks WHERE state='leased' AND lease_until>?", (time.time(),)).fetchone()[0]


def _select_tasks(book, store, attempted, limit):
    """Use the shared task/context gates, without reviving failed tasks."""
    grouped = set()
    if book.profile.get("semantic_review_units"):
        from .semantic_units import member_ids
        grouped = member_ids(book)
    failed = {r[0] for r in store.db.execute("SELECT t.element_id FROM translations t JOIN elements e ON e.id=t.element_id WHERE t.status='review_failed' AND t.source_hash=e.source_hash AND t.rule_hash=e.rule_hash")}
    now = time.time()
    selected = []
    for task in store.pending_tasks():
        if task["id"] in attempted or task["state"] not in {"pending", "partial", "leased"}:
            continue
        if task["state"] == "leased" and task["lease_until"] > now:
            continue
        if task["next_attempt"] > now or not task["missing_element_ids"]:
            continue
        if set(task["element_ids"]) & (grouped | failed):
            continue
        if _current_task(task, book, store.glossary):
            selected.append(task["id"])
            if len(selected) >= limit:
                break
    return selected


def _ordinary_review_view(book):
    """A temporary safe sample view; never change Store's registered Book.

    Joint protocols retain their own explicit review stage. Removing their
    parents/members first keeps ordinary hash bindings unchanged when the two
    registries are absent from this temporary view.
    """
    blocked_ids, blocked_pages = source_error_scope(book)
    grouped = set()
    if book.profile.get("semantic_review_units"):
        from .semantic_units import member_ids
        grouped.update(member_ids(book))
    if book.profile.get("argument_units_v1"):
        from .argument_units import registry
        for unit in registry(book).values():
            grouped.update(unit["member_ids"])
            grouped.add(unit["parent_id"])
    unsafe = {e.id for e in book.elements if e.id in blocked_ids or e.page in blocked_pages}
    elements = [e for e in book.elements if e.id not in unsafe | grouped]
    profile = dict(book.profile)
    profile.pop("semantic_review_units", None)
    profile.pop("argument_units_v1", None)
    required = {e.id for e in book.elements if needs_translation(e)}
    return replace(book, elements=elements, profile=profile), {
        "ordinary_elements": len(required - unsafe - grouped),
        "skipped_source_elements": len(required & unsafe),
        "skipped_joint_elements": len(required & grouped),
        "joint_review_run": False,
        "mode": "ordinary_safe_sample",
    }


def run_job(options: AutoOptions, event_callback=None) -> dict:
    """Run bounded translation, optional review, and a clearly labelled draft.

    A callback receives safe dictionaries with stage/state/message/progress and
    summary. Returning False requests a stop after the current bounded batch.
    No raw provider reply, credential, or exception body enters these logs.
    """
    started = time.monotonic()
    out = None
    stop_requested = False
    metrics = {"attempted_tasks": 0, "batches": 0, "new_accepted": 0, "requests": 0,
               "failed_requests": 0, "input_tokens": 0, "output_tokens": 0}
    summary = {"accepted": 0, "total": 0, "remaining": 0, "source_errors": 0, "output_pdf": None}

    def emit(stage, state, message, progress=None):
        nonlocal stop_requested
        elapsed = max(0.0, time.monotonic() - started)
        speed = round(metrics["new_accepted"] * 60 / max(.001, elapsed), 3)
        event = {"stage": stage, "state": state, "message": message,
                 "progress": progress if progress is not None else summary["accepted"] / summary["total"] if summary["total"] else 0.0,
                 "current": summary["accepted"], "total": summary["total"], "summary": dict(summary),
                 "elapsed_seconds": round(elapsed, 3),
                 "elements_per_minute": speed, "speed_elements_per_minute": speed}
        if out is not None:
            with (out / "autopilot_events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            _atomic_json(out / "autopilot_status.json", {**event, "metrics": dict(metrics), "preview": True, "accepted": False})
        if event_callback:
            try:
                if event_callback(event) is False:
                    stop_requested = True
            except Exception:
                stop_requested = True
        return event

    def stop_reason():
        if stop_requested or (options.cancel_event is not None and options.cancel_event.is_set()):
            return "cancelled"
        if options.stop_file and Path(options.stop_file).is_file():
            return "stop_file"
        if options.max_seconds is not None and time.monotonic() - started >= options.max_seconds:
            return "time_budget"
        return None

    def refresh(store):
        status = store.status()
        summary.update(accepted=status["accepted_translatable"], total=status["required_translations"],
                       remaining=status["required_translations"] - status["accepted_translatable"],
                       source_errors=status["source_errors"])

    try:
        pages = _validate(options)
        glossary_path = options.glossary_path or options.glossary
        pdf, chosen_out, config = _paths(options)
        with _job_lock(chosen_out):
            out = chosen_out
            try:
                previous = json.loads((out / "autopilot_status.json").read_text(encoding="utf-8"))
                prior_pdf = previous.get("pdf_path") or previous.get("summary", {}).get("output_pdf")
                if prior_pdf and Path(prior_pdf).is_file():
                    summary["output_pdf"] = prior_pdf
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            bookfile = out / "book.json"
            with Store(out / "tasks.db") as store:
                if store.book is not None:
                    book = store.book
                    if not _book_file_matches(book, bookfile):
                        raise _Refusal("book_mismatch", "任务文件与数据库来源记录不一致或文件不完整；请先单独准备并核对，已停止以保护已有译文。")
                    if glossary_path is not None and digest(load_glossary(glossary_path)) != digest(store.glossary):
                        raise _Refusal("glossary_mismatch", "现有任务采用另一版术语表，请在专用准备阶段更新。")
                else:
                    if bookfile.exists():
                        book = Book.load(bookfile)
                    else:
                        if pdf is None or not pdf.is_file():
                            raise _Refusal("missing_pdf", "新任务需要可读取的 PDF 文件。")
                        emit("analyze", "running", "正在解析原书和保护公式。")
                        if stop_reason() in {"cancelled", "stop_file"}:
                            raise _Refusal("cancelled", "已停止，未开始新的解析。", "stopped")
                        book = analyze(pdf, out, pages=pages, backend=options.backend, ocr=options.ocr, ocr_dpi=options.ocr_dpi)
                    if pdf is not None and file_sha256(pdf) != book.source_sha256:
                        raise _Refusal("source_mismatch", "原书内容与任务不一致，请使用新目录。")
                    if pages is not None and pages != book.selected_pages:
                        raise _Refusal("pages_mismatch", "此目录采用另一组原书页码。")
                    emit("prepare", "running", "正在核验来源并建立可续跑任务。")
                    if stop_reason() in {"cancelled", "stop_file"}:
                        raise _Refusal("cancelled", "已停止，尚未建立新的翻译任务。", "stopped")
                    book.issues.extend(source_contract_issues(book))
                    _source_preflight(book, out)
                    book.save(bookfile)
                    store.register(book, load_glossary(glossary_path))
                    store.add_tasks(make_tasks(book, store.glossary, target_chars=options.target_chars))
                    with store.db:
                        store.db.execute("INSERT OR REPLACE INTO metadata VALUES('task_target_chars',?)", (json.dumps(options.target_chars),))
                source = pdf if pdf is not None else Path(book.source_path)
                if not source.is_file() or file_sha256(source) != book.source_sha256:
                    raise _Refusal("source_mismatch", "原书丢失或内容已改变；没有继续发送请求。")
                if pages is not None and pages != book.selected_pages:
                    raise _Refusal("pages_mismatch", "此目录采用另一组原书页码。")
                if _live_leases(store):
                    raise _Refusal("live_leases", "任务仍有其他执行者的有效领取记录，请稍后继续。", "busy")
                revision = _revision(store)

                def current():
                    if _revision(store) != revision:
                        raise _Refusal("source_changed", "运行期间来源版本已变化，已停止；请重新打开任务。")

                refresh(store)
                initial = summary["accepted"]
                attempted = set()
                state, reason = "incomplete", "pending_work"
                configured = False
                emit("prepare", "running", "任务已就绪；问题范围将保留原文，其余内容继续处理。")
                while True:
                    current()
                    why = stop_reason()
                    if why:
                        state, reason = ("budget_reached" if why == "time_budget" else "stopped"), why
                        break
                    if options.max_tasks is not None and len(attempted) >= options.max_tasks:
                        state, reason = "budget_reached", "task_budget"
                        break
                    remaining_budget = options.batch_size if options.max_tasks is None else min(options.batch_size, options.max_tasks-len(attempted))
                    ids = _select_tasks(book, store, attempted, remaining_budget)
                    if not ids:
                        state = "finished" if not summary["remaining"] else "no_progress"
                        reason = "translation_complete" if state == "finished" else "no_runnable_tasks"
                        break
                    if not configured:
                        _providers(config, options); configured = True
                    emit("translate", "running", "正在翻译一批内容；本批结束后保存进度。")
                    # Callback may have requested stop before any new request.
                    if stop_reason():
                        state, reason = "stopped", "cancelled"
                        break
                    attempted.update(ids)
                    metrics["attempted_tasks"] = len(attempted)
                    try:
                        report = run_translation(book, store, config, provider=options.provider,
                            workers=options.workers, review_provider=None, max_tasks=len(ids), task_ids=ids)
                    except Exception:
                        # Preserve failed/cooldown state. Never retry this batch
                        # in a loop or expose the provider exception's body.
                        metrics["batch_errors"] = metrics.get("batch_errors", 0) + 1
                        report = {}
                    metrics["batches"] += 1
                    for key in ("requests", "failed_requests", "input_tokens", "output_tokens"):
                        metrics[key] += report.get(key, 0)
                    current(); refresh(store)
                    metrics["new_accepted"] = max(0, summary["accepted"] - initial)
                    emit("translate", "running", "本批结果已保存。")
                    if report.get("provider_blocked"):
                        state, reason = "blocked", "provider_blocked"
                        break
                    if report.get("cancelled"):
                        state, reason = "stopped", "cancelled"
                        break
                if options.review and options.max_tasks != 0 and not stop_reason() and state not in {"blocked", "stopped"}:
                    current(); _providers(config, options)
                    emit("review", "running", "正在独立抽查已完成译文。")
                    try:
                        review_view, metrics["review_scope"] = _ordinary_review_view(book)
                        reviewed = run_review(review_view, store, config, options.review_provider or options.provider,
                            max_elements=options.max_review_elements, workers=options.workers)
                    except Exception:
                        reviewed = {}
                        metrics["review_errors"] = 1
                        emit("review", "incomplete", "本次抽查未完成，译文已保留并继续生成阅读稿。")
                    for key in ("requests", "failed_requests", "input_tokens", "output_tokens"):
                        metrics[key] += reviewed.get(key, 0)
                    current(); refresh(store)
                draft = None
                if options.draft and state != "stopped" and stop_reason() not in {"cancelled", "stop_file"}:
                    current()
                    emit("draft", "running", "正在生成阅读稿；未完成范围将明确标注。")
                    rendered = set()
                    def draft_event(event):
                        current()
                        rendered.update(p for p in event.get("source_pages", []) if p in book.selected_pages)
                        emit("draft", "running", "阅读稿已处理 " + str(len(rendered)) + "/" + str(len(book.selected_pages)) + " 个原页。",
                             progress=len(rendered) / max(1, len(book.selected_pages)))
                        if stop_reason() in {"cancelled", "stop_file"}:
                            raise _DraftStopped()
                    try:
                        from .reading_draft import ReadingDraftCancelled
                        if stop_reason() in {"cancelled", "stop_file"}:
                            raise _DraftStopped()
                        draft = compose_reading_draft(book, store.translations(), out / "reading_draft", event_callback=draft_event, cancel_event=options.cancel_event)
                    except (_DraftStopped, ReadingDraftCancelled):
                        state, reason = "stopped", "cancelled"
                    current()
                    if draft is not None:
                        summary["output_pdf"] = draft.get("pdf_path")
                refresh(store)
                # A successful translation is distinct from semantic/layout QA.
                if state == "finished" and summary["remaining"]:
                    state, reason = "incomplete", "review_requires_attention"
                if metrics.get("review_errors") and state == "finished":
                    state, reason = "incomplete", "review_unavailable"
                result = {"state": state, "status": state, "reason": reason, "job_dir": str(out),
                          "pdf_path": summary["output_pdf"], "output_pdf": summary["output_pdf"],
                          "summary": dict(summary), "metrics": metrics, "preview": True, "accepted": False}
                if draft is not None:
                    result["draft"] = {k: draft.get(k) for k in ("pdf_path", "page_count", "source_pages", "required_translations", "translated_elements", "untranslated_elements", "fallback_source_pages", "report_path", "html_report_path", "preview", "accepted")}
                final_message = ("进度已保存，阅读稿可查看；待处理问题保留在报告中。" if summary["output_pdf"]
                                 else "进度已保存，可继续翻译或单独生成阅读稿。")
                event = emit("finished", state, final_message)
                result["metrics"].update(elapsed_seconds=event["elapsed_seconds"], elements_per_minute=event["elements_per_minute"], speed_elements_per_minute=event["elements_per_minute"])
                _atomic_json(out / "autopilot_status.json", result)
                return result
    except KeyboardInterrupt:
        result = {"state": "stopped", "status": "stopped", "reason": "interrupted", "message": "已停止；已提交结果保留，重新运行可继续。"}
    except _Refusal as error:
        result = {"state": error.state, "status": error.state, "reason": error.reason, "message": error.message}
    except Exception as error:
        result = {"state": "error", "status": "error", "reason": "run_failed", "error_type": type(error).__name__, "message": "运行未完成；已保存的有效结果保留，请查看本地状态后续跑。"}
    result.update(job_dir=str(out) if out else None, summary=dict(summary), metrics=metrics,
                  pdf_path=summary["output_pdf"], output_pdf=summary["output_pdf"], preview=True, accepted=False)
    if out is not None:
        # The OS lock has been released: avoid overwriting another runner's
        # normal status file. Each rejected run keeps its own safe receipt.
        try:
            _atomic_json(out / ("autopilot_error_" + uuid.uuid4().hex + ".json"), result)
        except OSError:
            pass
    return result
