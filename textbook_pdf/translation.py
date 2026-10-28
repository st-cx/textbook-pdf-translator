"""Versioned translation packets shared by API workers and offline agents.

Only Store owns translation state.  HTTP errors deliberately omit response bodies,
request headers and configuration values so a failed provider cannot leak keys.
"""
from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from .models import (RULES_VERSION, TOKEN_RE, TRANSLATABLE, Book, digest,
                     protected_asset_descriptor, validate_translation, source_error_scope)

from .numeric_policy_dispatch import numeric_policy_fields, frozen_policy_matches
from .percentage_policy import percentage_policy_fields, frozen_percentage_policy_matches
from .century_policy import century_policy_fields, frozen_century_policy_matches
from .argument_units import argument_member_fields, frozen_argument_matches, task_context as argument_task_context

PROTOCOL_VERSION = 2
TRANSLATION_PACKET_MAX_IDS = 6  # Per-request limit; a source group may be larger.
TRANSLATION_TASK_MAX_REQUESTS = 128  # Includes translation and inline review.
TASK_ASSET_SCHEMA = "protected-assets-v2"
REVIEW_HASH_SCHEMA = "relevant-glossary-v2"
STYLE_RULES = (
    "译为简体中文大学教材书面语，准确、自然、克制；只翻译，不添加解释、答案或总结。",
    "逐元素对应；保持题号、小问、章节、图表引用、数字及单位，不合并或遗漏元素。",
    "⟦F:asset_id⟧ 是不可修改、不可复制、不可遗漏的公式对象；只读公式上下文不可输出。",
    "遵守冻结术语表。新术语只在 proposed_terms 中提议，不改变本批统一规则。",
    "可依中文语序调整句子，不删减内容，不通过压缩或填充文字迎合原页面长度。",
    "context_before、context_after、completed_context 均只读，不重新翻译或放入 translations。",
)
DEFAULT_GLOSSARY = {
    "proposition": "命题", "truth table": "真值表", "predicate": "谓词",
    "quantifier": "量词", "universal quantifier": "全称量词",
    "existential quantifier": "存在量词", "proof": "证明", "counterexample": "反例",
    "set": "集合", "subset": "子集", "function": "函数", "relation": "关系",
    "equivalence relation": "等价关系", "equivalence class": "等价类",
    "partial ordering": "偏序", "Hasse diagram": "哈斯图", "graph": "图",
    "vertex": "顶点", "edge": "边", "tree": "树", "algorithm": "算法",
    "probability": "概率", "random variable": "随机变量", "variance": "方差",
}


def _translatable(element):
    if not element.metadata.get('translatable',True) or element.metadata.get('preserve_only',False):
        return False
    return element.kind in TRANSLATABLE and bool(element.text.strip())


def _source_scope_allows_payload(payload, elements, blocked_ids, blocked_pages):
    """Include distant original argument dependencies in the paid-request gate."""
    rows = []
    for key in ('elements', 'context_before', 'context_after'):
        entries = payload.get(key, [])
        if not isinstance(entries, list):
            return False
        rows.extend(entries)
    arguments = payload.get('argument_context') or {}
    if not isinstance(arguments, dict):
        return False
    for unit in arguments.values():
        if not isinstance(unit, dict) or not isinstance(unit.get('ordered_readonly_source'), list):
            return False
        rows.extend(unit['ordered_readonly_source'])
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            return False
        element = elements.get(row['id'])
        if element is None or element.id in blocked_ids or element.page in blocked_pages:
            return False
    return True


def make_tasks(book: Book, glossary: dict, target_chars: int = 2400) -> list[dict]:
    """Create deterministic, complete packets; never cut a paragraph or group.

    Adjacent elements sharing a nonempty group ID (e.g. question and choices)
    remain together even if the group exceeds target_chars.  A chapter boundary
    always starts a new task.  Large individual elements are deliberately kept
    intact instead of silently dropping their tail.
    """
    if not isinstance(target_chars, int) or target_chars < 1:
        raise ValueError("target_chars must be a positive integer")
    frozen_glossary = dict(sorted((str(k), str(v)) for k, v in glossary.items()))
    blocked_ids, blocked_pages = source_error_scope(book)
    element_map = book.element_map()
    elements = [e for e in book.elements if _translatable(e) and e.id not in blocked_ids and e.page not in blocked_pages]
    spans, span = {}, 0
    for index, element in enumerate(elements):
        if index and (element.chapter != elements[index-1].chapter
                      or element.page-elements[index-1].page not in (0, 1)):
            span += 1
        spans[element.id] = span
    groups = []
    for element in elements:
        if (groups and element.group_id and groups[-1][-1].group_id == element.group_id
                and groups[-1][-1].chapter == element.chapter
                and spans[groups[-1][-1].id] == spans[element.id]):
            groups[-1].append(element)
        else:
            groups.append([element])
    batches, current, size = [], [], 0
    for group in groups:
        group_size = sum(len(e.text) for e in group)
        source_span_change = bool(current and spans[group[0].id] != spans[current[-1].id])
        if current and (size + group_size > target_chars or source_span_change):
            batches.append(current)
            current, size = [], 0
        current.extend(group)
        size += group_size
    if current:
        batches.append(current)
    position = {e.id: i for i, e in enumerate(elements)}
    tasks = []
    for batch in batches:
        start, end = position[batch[0].id], position[batch[-1].id]
        context = lambda seq: [
            {"id": e.id, "page": e.page, "text": e.text, "source_hash": e.source_hash, "readonly": True, **numeric_policy_fields(e), **percentage_policy_fields(e), **century_policy_fields(e), **argument_member_fields(e,book)}
            for e in seq if spans[e.id] == spans[batch[0].id]
        ]
        protected_ids = set()
        nearby = [e for e in elements[max(0, start - 2):end + 3] if spans[e.id] == spans[batch[0].id]]
        for e in nearby:
            protected_ids.update(TOKEN_RE.findall(e.text))
            protected_ids.update(e.asset_ids)
        missing = protected_ids - book.assets.keys()
        if missing:
            raise ValueError("Source references missing protected assets: " + ", ".join(sorted(missing)))
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "book_sha256": book.source_sha256,
            "rules_version": RULES_VERSION,
            "glossary": dict(frozen_glossary),
            "glossary_hash": digest(frozen_glossary),
            "style_rules": list(STYLE_RULES),
            "style_hash": digest(STYLE_RULES),
            "elements": [
                {"id": e.id, "source_hash": e.source_hash, "kind": e.kind,
                 "page": e.page, "text": e.text, "group_id": e.group_id, "chapter": e.chapter,
                 "table_headers":e.metadata.get('table_headers',[]), **numeric_policy_fields(e), **percentage_policy_fields(e), **century_policy_fields(e), **argument_member_fields(e,book)}
                for e in batch
            ],
            "protected_assets_schema": TASK_ASSET_SCHEMA,
            "protected_asset_descriptors": {
                aid: deepcopy(protected_asset_descriptor(book.assets[aid])) for aid in sorted(protected_ids)
            },
            "protected_assets": {
                aid: {"text": book.assets[aid].text, "kind": book.assets[aid].kind,
                      "sha256": book.assets[aid].sha256, "readonly": True}
                for aid in sorted(protected_ids)
            },
            "context_before": context(elements[max(0, start - 2):start]),
            "context_after": context(elements[end + 1:end + 3]),
        }
        argument_context=argument_task_context(book,[row["id"] for row in payload["elements"]+payload["context_before"]+payload["context_after"]])
        if argument_context:
            payload["argument_context"]=argument_context
            # Read-only original premises/conclusion include native objects
            # beyond the ordinary two-neighbour prose window.
            for unit in argument_context.values():
                all_readonly_aids={aid for row in unit['ordered_readonly_source']
                                   for aid in set(book.element_map()[row['id']].asset_ids)|set(TOKEN_RE.findall(row['text']))}
                for aid in sorted(all_readonly_aids):
                    asset=book.assets[aid]
                    payload['protected_asset_descriptors'][aid]=deepcopy(protected_asset_descriptor(asset))
                    payload['protected_assets'][aid]={'text':asset.text,'kind':asset.kind,'sha256':asset.sha256,'readonly':True}
        if not _source_scope_allows_payload(payload, element_map, blocked_ids, blocked_pages):
            continue
        source_hash = digest(payload)
        tasks.append({"id": "task-" + source_hash[:24], "element_ids": [e.id for e in batch],
                      "source_hash": source_hash, "payload": payload})
    return tasks


def _json_no_duplicates(raw: str):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("Duplicate JSON key in response")
            out[key] = value
        return out
    text = raw.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if not match:
            raise ValueError("Malformed response code fence")
        text = match[1]
    return json.loads(text, object_pairs_hook=pairs)


def parse_response(raw: str, task: dict, requested_ids: list[str] | None = None) -> dict:
    """Strict public decoder for both API responses and offline submissions.

    Missing IDs are reported separately to permit saving verified partial work;
    unknown or duplicated IDs and stale hashes reject the entire envelope.
    """
    result = _json_no_duplicates(raw)
    if not isinstance(result, dict):
        raise ValueError("Response must be a JSON object")
    if result.get("task_id") != task["id"] or result.get("source_hash") != task["source_hash"]:
        raise ValueError("Response task ID or source hash does not match the claimed task")
    translations = result.get("translations")
    if not isinstance(translations, dict):
        raise ValueError("translations must map stable element IDs to strings")
    expected = set(task["element_ids"] if requested_ids is None else requested_ids)
    if set(translations) - expected:
        raise ValueError("Response contains unexpected element IDs")
    if any(not isinstance(v, str) or not v.strip() for v in translations.values()):
        raise ValueError("Every translation must be a nonempty string")
    terms = result.get("proposed_terms", {})
    if not isinstance(terms, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                           for k, v in terms.items()):
        raise ValueError("proposed_terms must be a string-to-string mapping")
    result["missing_ids"] = sorted(expected - translations.keys())
    return result


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout: float = 180.0
    max_tokens: int = 8192
    attempts: int = 4
    requests_per_minute: float = 60.0
    price_in: float | None = None
    price_out: float | None = None
    currency: str = "CNY"
    thinking: str | None = None
    reasoning_effort: str | None = None
    rate_limit_group: str | None = None
    max_concurrency: int = 32


def _load_provider(config: dict, name: str | None) -> Provider:
    providers = config.get("providers", {})
    chosen = (name or config.get("_default") or config.get("default_provider")
              or providers.get("_default"))
    if not chosen:
        chosen = next((key for key, value in providers.items() if isinstance(value, dict)), None)
    if chosen not in providers or not isinstance(providers[chosen], dict):
        raise ValueError("Requested provider is not present in config.json")
    value = providers[chosen]
    if not all(isinstance(value.get(k), str) and value[k].strip() for k in ("base_url", "api_key", "model")):
        raise ValueError("Provider requires base_url, api_key and model")
    if not value["base_url"].startswith(("https://", "http://")):
        raise ValueError("Provider base_url must use HTTP or HTTPS")
    thinking = value.get("thinking")
    effort = value.get("reasoning_effort")
    if thinking is not None and thinking not in ("enabled", "disabled"):
        raise ValueError("Provider thinking must be enabled or disabled")
    if effort is not None and effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
        raise ValueError("Provider reasoning_effort is unsupported")
    group = value.get("rate_limit_group")
    if group is not None and (not isinstance(group, str) or not group.strip()):
        raise ValueError("Provider rate_limit_group must be a nonempty string")
    concurrency = value.get("max_concurrency", 32)
    if type(concurrency) is not int or not 1 <= concurrency <= 32:
        raise ValueError("Provider max_concurrency must be between 1 and 32")
    return Provider(
        str(chosen), value["base_url"].rstrip("/"), value["api_key"], value["model"],
        timeout=max(1.0, min(float(value.get("timeout", 180)), 240.0)),
        max_tokens=max(512, int(value.get("max_tokens", 8192))),
        attempts=max(1, min(int(value.get("max_attempts", 4)), 6)),
        requests_per_minute=max(0.1, float(value.get("requests_per_minute", 60))),
        price_in=float(value["price_in"]) if value.get("price_in") is not None else None,
        price_out=float(value["price_out"]) if value.get("price_out") is not None else None,
        currency=str(value.get("currency", "CNY")),
        thinking=thinking, reasoning_effort=effort,
        rate_limit_group=group, max_concurrency=concurrency,
    )


def _load_translation_providers(config: dict, name: str | None):
    """A pool is explicit opt-in; selecting a normal provider remains single-route."""
    values = config.get("providers", {})
    pools = config.get("provider_pools", {})
    if not isinstance(values, dict) or not isinstance(pools, dict):
        raise ValueError("providers and provider_pools must be objects")
    chosen = name or config.get("_default") or config.get("default_provider") or values.get("_default")
    if chosen is None:
        chosen = next((key for key, value in values.items() if isinstance(value, dict)), None)
    if chosen in pools:
        if chosen in values:
            raise ValueError("Pool and provider names must be different")
        pool = pools[chosen]
        members = pool.get("providers") if isinstance(pool, dict) else None
        if (not isinstance(members, list) or not 1 <= len(members) <= 16
                or any(not isinstance(n, str) or n not in values for n in members)
                or len(set(members)) != len(members)):
            raise ValueError("Pool requires 1 to 16 distinct configured provider names")
        return chosen, [_load_provider(config, member) for member in members]
    selected = _load_provider(config, chosen)
    return selected.name, [selected]


def _chat_endpoint(base_url):
    endpoint = base_url.rstrip("/")
    return endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions"


class _Cancelled(Exception):
    pass


class _OutputTruncated(ValueError):
    pass


class _HTTPFailure(Exception):
    def __init__(self, status: int, retry_after: float = 0):
        super().__init__(f"Provider HTTP {status}")
        self.status, self.retry_after = status, retry_after
        self.retryable = status in {408, 409, 425, 429} or status >= 500


class _Limiter:
    """One adaptive reservation clock per provider, shared by all worker threads."""
    def __init__(self, rpm: float, stop: threading.Event):
        self.minimum = 60.0 / rpm
        self.interval = self.minimum
        self.next_at = 0.0
        self.lock = threading.Lock()
        self.stop = stop

    def acquire(self):
        while True:
            if self.stop.is_set():
                raise _Cancelled()
            with self.lock:
                delay = self.next_at - time.monotonic()
                if delay <= 0:
                    self.next_at = time.monotonic() + self.interval
                    return
            if self.stop.wait(min(delay, 1.0)):
                raise _Cancelled()

    def success(self):
        with self.lock:
            self.interval = max(self.minimum, self.interval * .95)

    def throttle(self, retry_after: float = 0):
        with self.lock:
            self.interval = min(max(self.minimum, 60.0), self.interval * 1.8)
            self.next_at = max(self.next_at, time.monotonic() + max(retry_after, self.interval))


class _RoutesUnavailable(Exception):
    """All routes failed; the source task is resumable, never permanently bad."""


class TranslationRoutingSession:
    """Shared capacity and route health for one run, including successive batches.

    Exact aliases share a clock and capacity. Different model IDs share it when
    rate_limit_group is configured; aliases do not create additional capacity.
    Credentials are compared privately for account identity, never diagnostics.
    """
    def __init__(self, config, provider=None, stop=None, *, extra_providers=()):
        self.name, self.providers = _load_translation_providers(config, provider)
        self.is_pool = self.name in config.get("provider_pools", {})
        self.stop = stop if stop is not None else threading.Event()
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.groups, self.limiters, self.capacity = {}, {}, {}
        self.active, self.cooldown, self.blocked, self.failures = Counter(), {}, {}, Counter()
        self.cursor = 0
        participants = {p.name: p for p in [*self.providers, *extra_providers]}
        self.participants = participants
        parents = {name: name for name in participants}
        def find(name):
            while parents[name] != name:
                name = parents[name]
            return name
        def merge(a, b):
            parents[find(a)] = find(b)
        entries = list(participants.values())
        for index, selected in enumerate(entries):
            for other in entries[:index]:
                same_account = (_chat_endpoint(selected.base_url) == _chat_endpoint(other.base_url)
                                and selected.api_key == other.api_key)
                exact_alias = selected.model == other.model
                shared_quota = (selected.rate_limit_group is not None
                                and selected.rate_limit_group == other.rate_limit_group)
                if same_account and (exact_alias or shared_quota):
                    merge(selected.name, other.name)
        for selected in entries:
            key = digest(sorted(n for n in participants if find(n) == find(selected.name)))
            self.groups[selected.name] = key
            if key not in self.limiters:
                self.limiters[key] = _Limiter(selected.requests_per_minute, self.stop)
                self.capacity[key] = selected.max_concurrency
            else:
                limiter = self.limiters[key]
                limiter.minimum = max(limiter.minimum, 60 / selected.requests_per_minute)
                limiter.interval = max(limiter.interval, limiter.minimum)
                self.capacity[key] = min(self.capacity[key], selected.max_concurrency)

    def matches(self, config, provider):
        name, providers = _load_translation_providers(config, provider)
        return name == self.name and providers == self.providers

    def acquire(self, excluded=(), *, only=None):
        excluded = set(excluded)
        candidates = [only] if only is not None else self.providers
        while True:
            if self.stop.is_set():
                raise _Cancelled()
            delay = 1.0
            with self.lock:
                live = [p for p in candidates if p.name not in self.blocked and p.name not in excluded]
                if not live:
                    raise _RoutesUnavailable("No untried usable translation route; progress is saved")
                now = time.monotonic()
                for offset in range(len(candidates)):
                    index = (self.cursor + offset) % len(candidates)
                    selected = candidates[index]
                    if selected not in live:
                        continue
                    key = self.groups[selected.name]
                    limiter = self.limiters[key]
                    if self.active[key] >= self.capacity[key]:
                        continue
                    with limiter.lock:
                        ready = max(limiter.next_at, self.cooldown.get(key, 0.0))
                        if ready > now:
                            delay = min(delay, ready - now)
                            continue
                        # Reserve rate and capacity atomically, before HTTP starts.
                        limiter.next_at = now + limiter.interval
                        self.active[key] += 1
                        if only is None:
                            self.cursor = (index + 1) % len(candidates)
                        return selected
                self.changed.wait(max(.001, min(delay, 1.0)))

    def release(self, selected, *, status=None, retry_after=0.0, failed=False):
        with self.lock:
            key = self.groups[selected.name]
            self.active[key] -= 1
            self.changed.notify_all()
            if not failed:
                self.failures[key] = 0
                return
            self.failures[key] += 1
            if status in {400, 401, 402, 403, 404, 405, 415, 422}:
                self.blocked[selected.name] = status
                if status in {401, 402, 403}:
                    for other in self.participants.values():
                        if self.groups[other.name] == key:
                            self.blocked[other.name] = status
            else:
                # Honor Retry-After. Never rotate aliases to evade a shared quota.
                seconds = min(300.0, max(retry_after, 2 ** min(self.failures[key], 5)))
                self.cooldown[key] = time.monotonic() + seconds

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            return {"pool": self.name, "providers": [p.name for p in self.providers],
                    "blocked": {p.name: self.blocked[p.name] for p in self.providers if p.name in self.blocked},
                    "cooling_down": [p.name for p in self.providers
                                     if self.cooldown.get(self.groups[p.name], 0.0) > now],
                    "all_blocked": all(p.name in self.blocked for p in self.providers)}


def _retry_after(value) -> float:
    try:
        return min(300.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        try:
            return min(300.0, max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _chat(provider: Provider, messages: list[dict]) -> tuple[str, dict]:
    """Small OpenAI-compatible adapter; no SDK dependency or secret-bearing logs."""
    endpoint = _chat_endpoint(provider.base_url)
    payload = {"model": provider.model, "messages": messages,
               "temperature": 0.2, "max_tokens": provider.max_tokens}
    if provider.thinking is not None:
        payload["thinking"] = {"type": provider.thinking}
    if provider.reasoning_effort is not None:
        payload["reasoning_effort"] = provider.reasoning_effort
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Authorization": "Bearer " + provider.api_key, "Content-Type": "application/json",
        "User-Agent": "TextbookPDF/2",
    })
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # Do not forward an Authorization header to a redirect target.
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=provider.timeout) as response:
            data = json.loads(response.read(16 * 1024 * 1024))
    except urllib.error.HTTPError as error:
        status, retry = error.code, _retry_after(error.headers.get("Retry-After"))
        error.close()
        raise _HTTPFailure(status, retry) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ConnectionError("Provider connection failed or timed out") from None
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"]
        if not isinstance(text, str):
            raise ValueError("Provider response contains no text")
    except (KeyError, IndexError, TypeError):
        raise ValueError("Malformed provider response") from None
    usage = dict(data.get("usage") or {})
    usage["_truncated"] = choice.get("finish_reason") == "length"
    return text, usage


def _risk(element) -> bool:
    return bool(numeric_policy_fields(element) or percentage_policy_fields(element) or century_policy_fields(element) or TOKEN_RE.search(element.text) or element.kind in {"exercise", "table_cell", "figure_label"}
                or len(element.text) > 1200 or re.search(
                    r"\b(theorem|proof|definition|lemma|corollary|if and only if)\b", element.text, re.I))


def review_selected(element) -> bool:
    """High-risk text plus a reproducible 10% sample of ordinary elements."""
    return _risk(element) or int(digest(element.id)[:8], 16) % 10 == 0


def _review_batches(ids, elements, translations, max_ids=6, max_chars=3000):
    """Bound review responses without cutting one complete source element."""
    batches, batch, chars = [], [], 0
    for eid in ids:
        length = len(elements[eid].text) + len(translations[eid])
        if batch and (len(batch) >= max_ids or chars + length > max_chars):
            batches.append(batch)
            batch, chars = [], 0
        batch.append(eid)
        chars += length
    if batch:
        batches.append(batch)
    return batches


def relevant_review_glossary(element, book, glossary) -> dict:
    """Conservatively bind terminology present in this source and its objects.

    Substring matching also covers inflections (function/functions). Table header
    context and protected source text count; unrelated book terms do not. This
    selects rules only, and never changes the shared glossary or their meanings.
    """
    objects = [book.assets[aid].text for aid in sorted(set(element.asset_ids) | set(TOKEN_RE.findall(element.text)))]
    headers = element.metadata.get("table_headers", [])
    corpus = "\n".join([TOKEN_RE.sub("", element.text), *objects,
                          json.dumps(headers, ensure_ascii=False)]).casefold()
    return {str(k): str(v) for k, v in sorted(glossary.items())
            if str(k) and str(k).casefold() in corpus}


def _review_hash_from_digest(element, translation_hash, book, glossary, *, legacy=False):
    if legacy:
        protected = {aid: book.assets[aid].sha256 for aid in TOKEN_RE.findall(element.text)}
    else:
        ids = set(element.asset_ids) | set(TOKEN_RE.findall(element.text))
        protected = {aid: protected_asset_descriptor(book.assets[aid]) for aid in sorted(ids)}
    value = {"source": element.source_hash, "translation": translation_hash,
             "assets": protected,
             "glossary": digest(glossary if legacy else relevant_review_glossary(element, book, glossary)),
             "rules_version": RULES_VERSION, "style_hash": digest(STYLE_RULES)}
    if not legacy:
        value["review_hash_schema"] = REVIEW_HASH_SCHEMA
    value.update(numeric_policy_fields(element))
    value.update(percentage_policy_fields(element))
    value.update(century_policy_fields(element))
    value.update(argument_member_fields(element,book))
    return digest(value)


def _review_hash(element, translated, book, glossary):
    return _review_hash_from_digest(element, digest(translated), book, glossary)


def _review_binding(element,translated,book,glossary):
    """Freeze scalar identities before sending a request, not after its reply."""
    from .store import translation_rule_hash
    return {'element_id':element.id,'source_hash':element.source_hash,
            'rule_hash':translation_rule_hash(book,element,glossary),
            'source_sha256':book.source_sha256,'review_hash':_review_hash(element,translated,book,glossary)}


def _commit_review(store,element,translated,book,glossary,passed,problems,provider,*,task=None,owner=None,binding=None):
    """Store independently verifies the current DB binding in one transaction."""
    lease=({'task_id':task['id'],'owner':owner,'task_source_hash':task['source_hash']}
           if task is not None else {})
    frozen=dict(binding if binding is not None else _review_binding(element,translated,book,glossary))
    return store.apply_semantic_review(frozen.pop('element_id'),translated,
        passed=passed,problems=problems,provider=provider,**frozen,**lease)


def legacy_review_hash(element, translated, book, glossary):
    """Exact V1 identity for read-only old-cache verification, never a new proof."""
    return _review_hash_from_digest(element, digest(translated), book, glossary, legacy=True)


def _review_record_matches_digest(entry, element, translation_hash, book, glossary,
                                  previous_glossary=None):
    if (entry.get("element_id") != element.id
            or (entry.get("translation_hash") and entry["translation_hash"] != translation_hash)
            or (entry.get("source_hash") and entry["source_hash"] != element.source_hash)):
        return False
    try:
        if entry.get("review_hash") == _review_hash_from_digest(element, translation_hash, book, glossary):
            return True
        previous = glossary if previous_glossary is None else previous_glossary
        if relevant_review_glossary(element, book, previous) != relevant_review_glossary(element, book, glossary):
            return False
        return (entry.get("review_hash_schema") in (None, "whole-glossary-v1")
                and entry.get("review_hash") == _review_hash_from_digest(
                    element, translation_hash, book, previous, legacy=True))
    except KeyError:
        return False


def review_record_matches(entry, element, translated, book, glossary, *, previous_glossary=None) -> bool:
    """Verify a current verdict or an exact legacy proof without writing its DB.

    Call with the old Book/glossary/text when inspecting an old read-only job.
    An explicit previous_glossary can bridge a known glossary edit only when
    this element's relevant term rules are identical; source/assets/text/style
    always remain bound. Merely naming an ID or a translation hash is no proof.
    """
    return _review_record_matches_digest(entry, element, digest(translated), book,
                                          glossary, previous_glossary)


def migrate_review_bindings(book, store, *, previous_glossary=None) -> dict:
    """Append verified V2 aliases and archive exact original V1 diagnostics.

    Original JSON/history rows remain resolved with review_binding_migrated;
    leaving weak V1 proofs active could bypass the stronger asset identity.
    Prefer running before changing a job glossary. A known previous snapshot is
    accepted after an unrelated edit only if the old full V1 identity verifies
    and relevant rules are unchanged. No verdict is inferred from acceptance.
    """
    glossary = dict(getattr(store, "glossary", DEFAULT_GLOSSARY))
    revisions, elements = store.translations(accepted_only=False), book.element_map()
    active = store.issues()
    codes = {"semantic_review_passed", "semantic_review_failed", "semantic_review_pending"}
    existing = {(i.get("code"), i.get("element_id"), i.get("review_hash")) for i in active}
    report = {"schema": REVIEW_HASH_SCHEMA, "scanned": 0, "migrated": 0,
              "already_bound": 0, "unverified": 0, "archived_legacy": 0, "by_code": {}}
    for entry in active:
        code, eid = entry.get("code"), entry.get("element_id")
        if code not in codes:
            continue
        report["scanned"] += 1
        if eid not in elements or eid not in revisions:
            report["unverified"] += 1
            continue
        element, text = elements[eid], revisions[eid]
        if not review_record_matches(entry, element, text, book, glossary,
                                     previous_glossary=previous_glossary):
            report["unverified"] += 1
            continue
        current_hash = _review_hash(element, text, book, glossary)
        key = (code, eid, current_hash)
        if key in existing:
            report["already_bound"] += 1
            if entry.get("review_hash") != current_hash:
                report["archived_legacy"] += store.resolve_issue(entry, "review_binding_migrated")
            continue
        alias = dict(entry, review_hash=current_hash, review_hash_schema=REVIEW_HASH_SCHEMA,
                     translation_hash=digest(text), source_hash=element.source_hash,
                     previous_review_hash=entry["review_hash"],
                     binding_migration="verified-whole-glossary-v1-to-relevant-v2")
        store.record_issue(alias)
        report["archived_legacy"] += store.resolve_issue(entry, "review_binding_migrated")
        existing.add(key)
        report["migrated"] += 1
        report["by_code"][code] = report["by_code"].get(code, 0) + 1
    return report


def _review_snapshot(book, store):
    glossary = dict(getattr(store, "glossary", DEFAULT_GLOSSARY))
    accepted, elements = store.translations(), book.element_map()
    active = store.issues() if hasattr(store, "issues") else []
    forced = {entry.get("element_id") for entry in active
              if entry.get("code") in {"glossary_review", "glossary_review_required", "semantic_review_failed", "semantic_review_pending"}
              and entry.get("element_id") in accepted and entry.get("element_id") in elements
              and (not entry.get("source_hash") or entry["source_hash"] == elements[entry["element_id"]].source_hash)
              and (not entry.get("translation_hash") or entry["translation_hash"] == digest(accepted[entry["element_id"]]))}
    passed, failed = set(), set()
    for entry in active:
        eid = entry.get("element_id")
        if eid in accepted and eid in elements and review_record_matches(
                entry, elements[eid], accepted[eid], book, glossary):
            if entry.get("code") == "semantic_review_passed":
                passed.add(eid)
            elif entry.get("code") == "semantic_review_failed":
                failed.add(eid)
    selected = [e.id for e in book.elements if e.id in accepted and _translatable(e)
                and (review_selected(e) or e.id in forced)]
    pending = [eid for eid in selected if eid not in passed or eid in failed]
    if book.profile.get("semantic_review_units"):
        from .semantic_units import member_ids
        grouped=member_ids(book)
        selected=[eid for eid in selected if eid not in grouped]
        pending=[eid for eid in pending if eid not in grouped]
    if book.profile.get("argument_units_v1"):
        from .argument_units import registry
        grouped={eid for u in registry(book).values()for eid in u["member_ids"]}
        selected=[eid for eid in selected if eid not in grouped]
        pending=[eid for eid in pending if eid not in grouped]
    return glossary, accepted, elements, selected, pending


def _new_review_report(provider, accepted, selected, pending, scheduled):
    return {"provider": provider, "accepted_elements": accepted, "selected": selected,
            "already_reviewed": selected - pending, "scheduled": scheduled,
            "review_passed": 0, "review_failed": 0, "review_pending": pending - scheduled,
            "review_batches": 0, "review_split_batches": 0, "review_truncated_calls": 0,
            "stale_results": 0, "requests": 0, "failed_requests": 0, "usage_missing_calls": 0,
            "input_tokens": 0, "output_tokens": 0, "estimated_cost": 0.0,
            "cost_estimate_complete": True, "cancelled": False, "provider_blocked": None}


def review_status(book, store) -> dict:
    """Read current hash-bound semantic coverage without calls or DB mutation.

    Exact V1 proofs remain verifiable against their frozen current glossary.
    Migrate them before glossary edits to retain unrelated-term independence.
    """
    glossary = dict(getattr(store, "glossary", DEFAULT_GLOSSARY))
    eligible = {e.id: e for e in book.elements if _translatable(e)}
    accepted = {eid: value for eid, value in store.translations().items() if eid in eligible}
    revisions = {eid: value for eid, value in store.translations(accepted_only=False).items()
                 if eid in eligible}
    issues = store.issues() if hasattr(store, "issues") else []
    forced, passed_ids, failed_ids, unbound_failures = set(), set(), set(), set()
    for entry in issues:
        eid, code = entry.get("element_id"), entry.get("code")
        if eid not in eligible:
            continue
        element, translation_hash = eligible[eid], entry.get("translation_hash")
        current_text = accepted.get(eid, revisions.get(eid))
        if entry.get("source_hash") and entry["source_hash"] != element.source_hash:
            continue
        if translation_hash and current_text is not None and translation_hash != digest(current_text):
            continue
        if code in {"glossary_review", "glossary_review_required", "semantic_review_pending"}:
            if not translation_hash or current_text is not None:
                forced.add(eid)
        if code == "semantic_review_passed" and current_text is not None:
            if review_record_matches(entry, element, current_text, book, glossary):
                passed_ids.add(eid)
        elif code == "semantic_review_failed":
            forced.add(eid)
            bound = (isinstance(translation_hash, str) and bool(translation_hash)
                     and _review_record_matches_digest(entry, element, translation_hash, book, glossary))
            (failed_ids if bound else unbound_failures).add(eid)
    selected = [eid for eid, element in eligible.items()
                if review_selected(element) or eid in forced]
    ids = {key: [] for key in ("selected", "passed", "pending", "failed",
                               "awaiting_translation", "unverifiable")}
    ids["selected"] = selected
    for eid in selected:
        if eid in failed_ids:
            ids["failed"].append(eid)
        elif eid not in accepted:
            ids["awaiting_translation"].append(eid)
        else:
            try:
                _review_hash(eligible[eid], accepted[eid], book, glossary)
            except KeyError:
                ids["unverifiable"].append(eid)
                ids["pending"].append(eid)
                continue
            ids["passed" if eid in passed_ids else "pending"].append(eid)
    result = {"required": len(eligible), "accepted": len(accepted),
            **{key: len(ids[key]) for key in ("selected", "passed", "pending", "failed",
                                              "awaiting_translation")},
            "unbound_failure_diagnostics": len(unbound_failures),
            "coverage_complete": not any(ids[key] for key in
                                          ("pending", "failed", "awaiting_translation")),
            "counts_are_current_hash_bound": True, "ids": ids}
    if book.profile.get("semantic_review_units"):
        from .semantic_unit_runner import overlay_review_status
        result=overlay_review_status(book,store,result)
    if book.profile.get("argument_units_v1"):
        from .argument_review_runner import overlay_argument_status
        result=overlay_argument_status(book,store,result)
    return result


def _semantic_failure_feedback(book, store, ids, glossary):
    """Carry exact standalone-review failures into the first correction request."""
    revisions, elements, wanted = store.translations(accepted_only=False), book.element_map(), set(ids)
    feedback = {}
    for entry in store.issues():
        eid = entry.get("element_id")
        if (entry.get("code") == "semantic_review_failed" and eid in wanted and eid in revisions
                and isinstance(entry.get("detail"), str)
                and review_record_matches(entry, elements[eid], revisions[eid], book, glossary)):
            feedback.setdefault(eid, {})[entry["detail"]] = {
                "code": "semantic_review_failed", "detail": entry["detail"]}
    return {eid: list(details.values()) for eid, details in feedback.items()}


def _glossary_issues(element, text: str, glossary: dict) -> list[dict]:
    # Polysemous words make a missing canonical term a review signal, not proof
    # of an incorrect translation (e.g. "set x to zero" is not the noun 集合).
    found = []
    source = TOKEN_RE.sub("", element.text)
    for english, chinese in glossary.items():
        if not english or not chinese:
            continue
        if re.search(r"(?<![A-Za-z])" + re.escape(english) + r"(?![A-Za-z])", source, re.I):
            alternatives = [v.strip() for v in re.split(r"[/／;；]", chinese)]
            if not any(v and v in text for v in alternatives):
                found.append({"code": "glossary_review_required", "severity": "warning",
                              "element_id": element.id, "page": element.page,
                              "detail": f"原文术语 {english} 未检测到冻结译名 {chinese}，需结合语义复核"})
    return found


def _frozen_source_groups(task):
    """Contiguous source groups from the signed task, including saved members.

    Group identity alone cannot bridge a chapter boundary or source-page gap.
    This is request context derivation; it never rewrites the canonical task.
    """
    groups, previous = [], None
    for row in task["payload"]["elements"]:
        same_group = (previous is not None and row.get("group_id")
                      and row.get("group_id") == previous.get("group_id")
                      and row.get("chapter") == previous.get("chapter")
                      and row["page"] - previous["page"] in (0, 1))
        if not same_group:
            groups.append([])
        groups[-1].append(row)
        previous = row
    return groups


def _request_payload(task, requested, accepted, feedback=None):
    payload = dict(task["payload"])
    # The envelope still binds the full canonical DB task hash. Geometry proves
    # source identity to Store/agents; the model needs only read-only math text.
    payload.pop("protected_asset_descriptors", None)
    payload["elements"] = [e for e in payload["elements"] if e["id"] in requested]
    wanted = set(requested)
    # A large question can require several bounded replies. Every such reply
    # sees the COMPLETE frozen question/choices, including already saved and
    # future members, without requesting translations of those read-only rows.
    # The Store envelope still carries the original whole-task source hash.
    source_groups = [group for group in _frozen_source_groups(task)
                     if len(group) > TRANSLATION_PACKET_MAX_IDS
                     and any(row["id"] in wanted for row in group)]
    if source_groups:
        payload["readonly_source_groups"] = [
            {"group_id": group[0].get("group_id"), "chapter": group[0].get("chapter"),
             "member_ids": [row["id"] for row in group], "readonly": True,
             "elements": [dict(deepcopy(row), readonly=True) for row in group]}
            for group in source_groups]
    frozen = task["payload"]["elements"]
    positions = [i for i, row in enumerate(frozen) if row["id"] in requested]
    if positions:
        start, end = min(positions), max(positions)
        context = lambda rows: [dict(row, readonly=True) for row in rows]
        payload["context_before"] = context(frozen[max(0, start - 2):start]) if start else payload["context_before"]
        payload["context_after"] = context(frozen[end + 1:end + 3]) if end + 1 < len(frozen) else payload["context_after"]
    payload["completed_context"] = [
        {"id": e["id"], "source": e["text"], "translation": accepted[e["id"]], "readonly": True,
         **({"numeric_policy": deepcopy(e["numeric_policy"])} if "numeric_policy" in e else {}),
         **({"century_policy": deepcopy(e["century_policy"])} if "century_policy" in e else {}),
         **({"argument_policy": deepcopy(e["argument_policy"])} if "argument_policy" in e else {})}
        for e in task["payload"]["elements"] if e["id"] in accepted and e["id"] not in requested
    ]
    return {"task_id": task["id"], "source_hash": task["source_hash"],
            "requested_element_ids": requested, "payload": payload,
            "previous_validation_errors": {eid: details for eid, details in (feedback or {}).items()
                                           if eid in requested},
            "response_example": {"task_id": task["id"], "source_hash": task["source_hash"],
                                 "translations": {eid: "对应的中文译文" for eid in requested},
                                 "proposed_terms": {}}}


def _translation_packets(task, requested, max_tokens):
    """Size requests without changing the canonical task or cutting elements.

    The output estimate is deliberately conservative, not a token guarantee.
    Explicit provider truncation is handled by splitting these groups further.
    A source group exceeding the ID limit uses complete elements as request
    units. _request_payload supplies its complete frozen read-only context.
    Independent argument/semantic review units retain their existing routing.
    """
    wanted = set(requested)
    rows = task["payload"]["elements"]
    if wanted - {row["id"] for row in rows}:
        raise ValueError("Requested packet contains a foreign source ID")
    groups = []
    for source_group in _frozen_source_groups(task):
        missing = [row["id"] for row in source_group if row["id"] in wanted]
        if len(source_group) > TRANSLATION_PACKET_MAX_IDS:
            # These are transport units, not invented independent proof units.
            # Singleton source elements let explicit truncation bisect a packet
            # while each element, task hash and full question context survive.
            groups.extend([eid] for eid in missing)
        elif missing:
            groups.append(missing)
    by_id = {row["id"]: row for row in rows}
    budget = max(128, int(max_tokens) // 2 - 128)
    packets, packet, deferred, estimate = [], [], [], 0
    for group in groups:
        if not group:
            continue
        group_estimate = sum(96 + 2 * len(by_id[eid]["text"]) for eid in group)
        if packet and (sum(map(len, packet)) + len(group) > TRANSLATION_PACKET_MAX_IDS
                       or estimate + group_estimate > budget):
            packets.append(packet)
            packet, estimate = [], 0
        packet.append(group)
        estimate += group_estimate
    if packet:
        packets.append(packet)
    return packets, deferred


class _TaskRequestLimit(ValueError):
    """No request was issued; the bounded task must remain resumable."""


def _current_task(task, book, glossary):
    """Require complete source-object identity, including read-only context."""
    payload = task.get("payload", {})
    if (payload.get("book_sha256") != book.source_sha256
            or payload.get("rules_version") != RULES_VERSION
            or payload.get("glossary_hash") != digest(dict(sorted(glossary.items())))
            or payload.get("style_hash") != digest(STYLE_RULES)
            or task.get("source_hash") != digest(payload)
            or payload.get("protected_assets_schema") != TASK_ASSET_SCHEMA
            or not isinstance(payload.get("protected_asset_descriptors"), dict)):
        return False
    elements = book.element_map()
    frozen = payload.get("elements", [])
    context = payload.get("context_before", []) + payload.get("context_after", [])
    if any(e["id"] not in elements or e.get("source_hash") != elements[e["id"]].source_hash
           for e in frozen + context):
        return False
    if any(not frozen_policy_matches(row, elements[row["id"]]) or not frozen_percentage_policy_matches(row, elements[row["id"]]) or not frozen_century_policy_matches(row, elements[row["id"]]) or not frozen_argument_matches(row, elements[row["id"]],book) for row in frozen + context):
        return False
    try:
        expected_argument_context=argument_task_context(book,[row["id"] for row in frozen+context])
    except (ValueError,KeyError,TypeError):return False
    if payload.get("argument_context")!=(expected_argument_context or None):return False
    blocked_ids, blocked_pages = source_error_scope(book)
    if not _source_scope_allows_payload(payload, elements, blocked_ids, blocked_pages):
        return False
    descriptors = payload["protected_asset_descriptors"]
    required = {aid for e in frozen + context
                for aid in set(elements[e["id"]].asset_ids) | set(TOKEN_RE.findall(elements[e["id"]].text))}
    for unit in expected_argument_context.values():
        required.update(aid for row in unit['ordered_readonly_source']
                        for aid in set(elements[row['id']].asset_ids)|set(TOKEN_RE.findall(row['text'])))
    if not required <= descriptors.keys():
        return False
    if any(aid not in book.assets or value != protected_asset_descriptor(book.assets[aid])
           for aid, value in descriptors.items()):
        return False
    if set(payload.get("protected_assets", {})) != set(descriptors):
        return False
    return set(task.get("element_ids", [])) == {e["id"] for e in frozen}


def run_translation(book, store, config_path, provider=None, workers=4, review_provider=None, max_tasks=None, task_ids=None, *, routing_session=None) -> dict:
    """Translate pending leased jobs, preserving partial progress and review state.

    A report's completed count concerns structural task acceptance.  Semantic
    review counts are separate and never inferred from translation completion.
    Keyboard interruption stops issuing requests; in-flight verified replies
    may still be saved before workers release their leases.
    """
    started = time.perf_counter()
    if workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")
    if max_tasks is not None and max_tasks < 0:
        raise ValueError("max_tasks must be nonnegative")
    if task_ids is not None:
        if isinstance(task_ids, (str, bytes)):
            raise ValueError("task_ids must be a collection of complete task IDs")
        task_ids = list(task_ids)
        if any(not isinstance(value, str) or not value.strip() for value in task_ids):
            raise ValueError("task_ids must contain nonempty task ID strings")
        task_ids = set(task_ids)
    glossary = dict(getattr(store, "glossary", DEFAULT_GLOSSARY))
    pending = [task for task in store.pending_tasks() if _current_task(task, book, glossary)]
    accepted_snapshot=store.translations()
    if task_ids is None and not pending and any(_translatable(e) and e.id not in accepted_snapshot for e in book.elements):
        store.add_tasks(make_tasks(book, glossary))
        pending = [task for task in store.pending_tasks() if _current_task(task, book, glossary)]
    if task_ids is not None:
        unavailable = task_ids - {task["id"] for task in pending}
        if unavailable:
            raise ValueError("Requested task IDs are not current pending work: " + ", ".join(sorted(unavailable)))
        pending = [task for task in pending if task["id"] in task_ids]
    pending = pending if max_tasks is None else pending[:max_tasks]
    if book.profile.get("semantic_review_units"):
        from .semantic_units import member_ids
        if any(set(t["element_ids"]) & member_ids(book) for t in pending):
            raise ValueError("Opted-in semantic-unit revisions require joint review; automatic fragment retranslation is blocked")
    report = {"provider": None, "review_provider": None, "scheduled": len(pending),
              "task_ids": sorted(task_ids) if task_ids is not None else None,
              "completed": 0, "failed": 0, "skipped_claimed": 0, "cancelled": False, "provider_blocked": None,
              "interrupted_saved_elements": 0, "interrupted_save_rejected": 0,
              "stale_review_results": 0,
              "translation_packets": 0, "translation_split_packets": 0,
              "translation_truncated_calls": 0, "translation_deferred_groups": 0,
              "task_request_limit": TRANSLATION_TASK_MAX_REQUESTS,
              "review_selected": 0, "review_passed": 0, "review_failed": 0, "review_pending": 0,
              "review_batches": 0, "review_split_batches": 0, "review_truncated_calls": 0,
              "requests": 0, "failed_requests": 0, "usage_missing_calls": 0, "input_tokens": 0, "output_tokens": 0,
              "estimated_cost": 0.0, "cost_estimate_complete": True, "usage_by_provider": {},
              "translation_request_seconds": 0.0, "review_request_seconds": 0.0}
    if not pending:
        report.update(elapsed_seconds=time.perf_counter() - started, state=store.status())
        return report
    from pathlib import Path
    config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    reviewer = _load_provider(config, review_provider) if review_provider else None
    session = routing_session or TranslationRoutingSession(config, provider,
        extra_providers=[reviewer] if reviewer else [])
    if not session.matches(config, provider):
        raise ValueError("Translation routing configuration changed; resume with a new session")
    translation_provider = session.providers[0]
    if reviewer and session.participants.get(reviewer.name) != reviewer:
        raise ValueError("Routing session must include its inline review provider")
    report["provider"], report["review_provider"] = session.name, reviewer.name if reviewer else None
    report["configured_workers"] = workers
    report["route_failovers"] = 0
    stop, counter_lock = session.stop, threading.Lock()
    limiters = {p.name: session.limiters[session.groups[p.name]] for p in session.providers}
    if reviewer:
        limiters[reviewer.name] = session.limiters[session.groups[reviewer.name]]
    element_map = book.element_map()
    task_calls = Counter()

    def bump(**values):
        with counter_lock:
            for key, value in values.items():
                report[key] += value

    def issue(code, detail, element=None, severity="warning", **extra):
        entry = {"code": code, "detail": detail, "severity": severity, **extra}
        if code.startswith("semantic_review_") and "review_hash" in entry:
            entry["review_hash_schema"] = REVIEW_HASH_SCHEMA
        if element is not None:
            entry.update(element_id=element.id, page=element.page)
        store.record_issue(entry)
        return entry

    def call(selected, messages, stage, task_id=None, *, reserved=False):
        limiter = limiters[selected.name]
        if not reserved:
            session.acquire(only=selected)
        at = time.perf_counter()
        with counter_lock:
            if task_id is not None and task_calls[task_id] >= TRANSLATION_TASK_MAX_REQUESTS:
                if not reserved:
                    session.release(selected)
                raise _TaskRequestLimit("Per-task request hard limit reached; remaining work is pending")
            if task_id is not None:
                task_calls[task_id] += 1
            report["requests"] += 1
            bucket = report["usage_by_provider"].setdefault(selected.name, {
                "model": selected.model, "currency": selected.currency, "input_tokens": 0,
                "output_tokens": 0, "estimated_cost": 0.0, "requests": 0,
                "price_source": "config.json per million tokens; not live billing",
            })
            bucket["requests"] += 1
        call_failure = None
        try:
            text, usage = _chat(selected, messages)
        except _HTTPFailure as error:
            call_failure = error
            bump(failed_requests=1, usage_missing_calls=1)
            with counter_lock:
                report["cost_estimate_complete"] = False
            if hasattr(store, "record_usage"):
                store.record_usage(task_id, selected.name, {"stage": stage, "request_failed": True,
                    "http_status": error.status, "usage_known": False, "prompt_tokens": 0,
                    "completion_tokens": 0, "estimated_cost": 0.0, "currency": selected.currency})
            if error.status == 429 or error.status >= 500:
                limiter.throttle(error.retry_after)
            if error.status in {401, 402, 403} and stage == "translation" and not session.is_pool:
                # Authentication, credit and access failures concern the whole
                # provider, not a paragraph. Let in-flight work finish saving,
                # and leave unstarted tasks available for a later resumption.
                with counter_lock:
                    report['provider_blocked'] = {'provider': selected.name, 'http_status': error.status}
                stop.set()
            raise
        except Exception as error:
            call_failure = error
            bump(failed_requests=1, usage_missing_calls=1)
            with counter_lock:
                report["cost_estimate_complete"] = False
            if hasattr(store, "record_usage"):
                store.record_usage(task_id, selected.name, {"stage": stage, "request_failed": True,
                    "usage_known": False, "prompt_tokens": 0, "completion_tokens": 0,
                    "estimated_cost": 0.0, "currency": selected.currency})
            raise
        finally:
            bump(**{stage + "_request_seconds": time.perf_counter() - at})
            if not reserved:
                session.release(selected, status=getattr(call_failure, "status", None),
                    retry_after=getattr(call_failure, "retry_after", 0.0), failed=call_failure is not None)
                if session.snapshot()["all_blocked"]:
                    with counter_lock:
                        report["provider_blocked"] = {"provider": session.name,
                            "http_status": getattr(call_failure, "status", None)}
                    stop.set()
        limiter.success()
        inp, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
        known = isinstance(inp, int) and isinstance(out, int)
        in_tokens, out_tokens = (inp, out) if known else (0, 0)
        priced = known and selected.price_in is not None and selected.price_out is not None
        cost = ((in_tokens * selected.price_in + out_tokens * selected.price_out) / 1_000_000) if priced else 0.0
        with counter_lock:
            report["input_tokens"] += in_tokens
            report["output_tokens"] += out_tokens
            report["usage_missing_calls"] += int(not known)
            report["estimated_cost"] += cost
            report["cost_estimate_complete"] &= bool(priced)
            bucket = report["usage_by_provider"][selected.name]
            bucket["input_tokens"] += in_tokens
            bucket["output_tokens"] += out_tokens
            bucket["estimated_cost"] += cost
        record = {"prompt_tokens": in_tokens, "completion_tokens": out_tokens,
                  "estimated_cost": cost, "currency": selected.currency, "usage_known": known,
                    "stage": stage, "model": selected.model, "provider": selected.name}
        if hasattr(store, "record_usage"):
            store.record_usage(task_id, selected.name, record)
        if usage.get("_truncated"):
            bump(failed_requests=1)
            raise _OutputTruncated("Provider output truncated; lower task size or increase provider max_tokens")
        return text, record

    def translate_call(messages, task_id):
        if not session.is_pool:
            selected = session.acquire()
            try:
                return call(selected, messages, "translation", task_id, reserved=True)
            finally:
                session.release(selected)
        tried = set()
        for _ in session.providers:
            try:
                selected = session.acquire(tried)
            except _RoutesUnavailable:
                break
            if tried:
                bump(route_failovers=1)
            tried.add(selected.name)
            try:
                result = call(selected, messages, "translation", task_id, reserved=True)
            except (_Cancelled, _TaskRequestLimit):
                session.release(selected)
                raise
            except _OutputTruncated:
                session.release(selected)
                raise  # Split the complete source groups, not a second paid full reply.
            except _HTTPFailure as error:
                session.release(selected, status=error.status, retry_after=error.retry_after, failed=True)
            except (ConnectionError, TimeoutError, ValueError) as error:
                session.release(selected, failed=True)
            except BaseException:
                session.release(selected)
                raise
            else:
                session.release(selected)
                return result
        snapshot = session.snapshot()
        if snapshot["all_blocked"]:
            with counter_lock:
                report["provider_blocked"] = {"provider": session.name, "routes": snapshot["blocked"]}
            stop.set()
        # A transport/quota error belongs to a route, not to the source text.
        raise _RoutesUnavailable("Translation routes unavailable; resume after cooldown") from None

    def semantic_review(task, candidates, force_ids, lease_owner=None):
        chosen = [eid for eid in candidates if review_selected(element_map[eid]) or eid in force_ids]
        bindings={eid:_review_binding(element_map[eid],candidates[eid],book,glossary) for eid in chosen}
        bump(review_selected=len(chosen))
        if not chosen:
            return set()
        def defer(ids, detail):
            stale=set()
            for eid in ids:
                applied=_commit_review(store,element_map[eid],candidates[eid],book,glossary,
                    None,[detail],reviewer.name if reviewer else None,task=task,owner=lease_owner,binding=bindings[eid])
                if applied['applied']:bump(review_pending=1)
                else:stale.add(eid);bump(stale_review_results=1)
            return stale
        if reviewer is None:
            return defer(chosen, "该元素已通过结构检查，但尚未执行独立语义复核")
        queue = deque(_review_batches(chosen, element_map, candidates))
        rejected = set()
        while queue:
            ids = queue.popleft()
            bump(review_batches=1)
            if lease_owner is not None:
                store.renew(task["id"], lease_owner, lease_seconds=600)
            assets = {aid for eid in ids for aid in TOKEN_RE.findall(element_map[eid].text)}
            packet = {"task_id": task["id"], "source_hash": task["source_hash"],
                      "glossary": glossary, "style_rules": list(STYLE_RULES),
                      "protected_assets": {aid: task["payload"]["protected_assets"][aid] for aid in sorted(assets)},
                      "elements": [{"id": eid, "source": element_map[eid].text, "translation": candidates[eid],
                                    **numeric_policy_fields(element_map[eid]), **percentage_policy_fields(element_map[eid]), **century_policy_fields(element_map[eid])}
                                   for eid in ids],
                      "required_response": {"task_id": task["id"], "source_hash": task["source_hash"],
                                            "reviews": {eid: {"passed": True, "issues": []} for eid in ids}}}
            try:
                raw, _ = call(reviewer, [
                    {"role": "system", "content": "你是独立数学教材翻译审校。检查完整性、数学含义、否定及量词、数字、题号、术语一致性和中文文风。不要解题或改译文。只输出指定JSON；passed必须是布尔值，issues是具体问题字符串列表。"},
                    {"role": "user", "content": json.dumps(packet, ensure_ascii=False)},
                ], "review", task["id"])
                result = _json_no_duplicates(raw)
                reviews = result.get("reviews") if isinstance(result, dict) else None
                if (not isinstance(result, dict) or result.get("task_id") != task["id"]
                        or result.get("source_hash") != task["source_hash"]
                        or not isinstance(reviews, dict) or set(reviews) != set(ids)):
                    raise ValueError("Review response envelope mismatch")
                for value in reviews.values():
                    if (not isinstance(value, dict) or type(value.get("passed")) is not bool
                            or not isinstance(value.get("issues"), list)
                            or any(not isinstance(v, str) for v in value["issues"])
                            or (value["passed"] and value["issues"])):
                        raise ValueError("Malformed semantic review verdict")
            except _OutputTruncated:
                bump(review_truncated_calls=1)
                if len(ids) > 1:
                    middle = len(ids) // 2
                    queue.appendleft(ids[middle:])
                    queue.appendleft(ids[:middle])
                    bump(review_split_batches=1)
                else:
                    rejected.update(defer(ids, "审校回复达到长度上限，单个完整元素仍未完成；保留待审状态"))
                continue
            except _Cancelled:
                raise
            except Exception as error:
                safe = str(error) if isinstance(error, _HTTPFailure) else type(error).__name__
                rejected.update(defer(ids, "语义复核未成功完成：" + safe))
                continue
            for eid, verdict in reviews.items():
                applied=_commit_review(store,element_map[eid],candidates[eid],book,glossary,
                    verdict['passed'],verdict['issues'],reviewer.name,task=task,owner=lease_owner,binding=bindings[eid])
                if not applied['applied']:
                    rejected.add(eid);bump(stale_review_results=1);continue
                if verdict["passed"]:
                    bump(review_passed=1)
                else:
                    rejected.add(eid)
                    bump(review_failed=1)
        return rejected

    def worker(task_id):
        owner = "api-" + uuid.uuid4().hex
        task = store.claim(owner, lease_seconds=600, task_id=task_id)
        if task is None:
            bump(skipped_claimed=1)
            return
        def release(error, retryable=True):
            try:
                store.fail(task["id"], owner, error, retryable=retryable)
            except ValueError:
                # A lease can expire or be superseded while a remote request
                # is in flight.  It must never disturb its new owner.
                pass
        last_error, terminal = "No complete response received", False
        failed_signatures = set()
        failures = Counter()
        initial_accepted = store.translations()
        packets, deferred = _translation_packets(task,
            [eid for eid in task["element_ids"] if eid not in initial_accepted],
            min(p.max_tokens for p in session.providers))
        packets = deque(packets)
        for group in deferred:
            bump(translation_deferred_groups=1)
            issue("translation_group_exceeds_packet_limit",
                  "完整语义组超出单次元素上限，未拆散源组或发起请求；保留待处理",
                  task_id=task["id"], element_ids=group)
        feedback = _semantic_failure_feedback(book, store, task["element_ids"], glossary)
        required_review = set(feedback)
        buffered_valid, buffered_review,buffered_bindings = {}, set(),{}
        result_provider = translation_provider.name

        def preserve_interrupted():
            """Save returned structural work through the same strict lease gate.

            A failed review must stay rejected. An unreviewed successful reply
            becomes accepted structural text with a hash-bound pending verdict,
            so resumption can review it without paying to translate it again.
            """
            if not buffered_valid:
                return
            diagnostics = store.issues()
            failed = {eid for eid, text in buffered_valid.items()
                      if any(entry.get("code") == "semantic_review_failed"
                             and review_record_matches(entry, element_map[eid], text, book, glossary)
                             for entry in diagnostics)}
            values = {eid: text for eid, text in buffered_valid.items() if eid not in failed}
            if not values:
                return
            try:
                saved = store.submit(task["id"], owner, task["source_hash"], values,
                                     provider=result_provider)
            except ValueError:
                # Revoked leases/source revisions must never be bypassed by a
                # cleanup path. Current neighbours and user-requested redo win.
                bump(interrupted_save_rejected=len(values))
                issue("translation_interrupted_save_rejected",
                      "中断前回复的任务租约或受保护来源已经改变，未绑定为当前译文", task_id=task["id"])
                return
            saved_ids = set(saved.get("accepted", []))
            bump(interrupted_saved_elements=len(saved_ids))
            passed = {eid for eid in saved_ids
                      if any(entry.get("code") == "semantic_review_passed"
                             and review_record_matches(entry, element_map[eid], values[eid], book, glossary)
                             for entry in diagnostics)}
            pending = (saved_ids & buffered_review) - passed
            already_pending = {eid for eid in pending
                               if any(entry.get("code") == "semantic_review_pending"
                                      and review_record_matches(entry, element_map[eid], values[eid], book, glossary)
                                      for entry in diagnostics)}
            for eid in pending:
                applied=_commit_review(store,element_map[eid],values[eid],book,glossary,None,
                    ["中断前已保存结构有效译文，独立语义复核待续传"],reviewer.name if reviewer else None,
                    binding=buffered_bindings[eid])
                if not applied['applied']:bump(stale_review_results=1)
                elif eid not in already_pending:bump(review_pending=1)
            if saved.get("complete"):
                bump(completed=1)

        try:
            # A successful packet does not spend another packet's retry quota.
            # Explicit truncation splits groups; a singleton is deferred once.
            while True:
                if stop.is_set():
                    raise _Cancelled()
                store.renew(task["id"], owner, lease_seconds=600)
                accepted = store.translations()
                missing = [eid for eid in task["element_ids"] if eid not in accepted]
                if not missing:
                    result = store.submit(task["id"], owner, task["source_hash"], {},
                                          provider=result_provider)
                    if result.get("complete"):
                        bump(completed=1)
                        return
                    raise ValueError("Store did not complete a fully accepted task")
                while packets:
                    groups = [[eid for eid in group if eid not in accepted] for group in packets[0]]
                    groups = [group for group in groups if group]
                    if groups:
                        packets[0] = groups
                        break
                    packets.popleft()
                if not packets:
                    break
                requested = [eid for group in packets[0] for eid in group]
                request_key = tuple(requested)
                attempt = failures[request_key]
                try:
                    bump(translation_packets=1)
                    raw, usage = translate_call([
                        {"role": "system", "content": "你是专业英文教材译者。严格遵守任务内冻结文体与术语。只翻译 requested_element_ids，保持公式标记和全部数字。只返回 response_example 形式的JSON，task_id及source_hash原样返回。不要输出只读上下文。"},
                        {"role": "user", "content": json.dumps(_request_payload(task, requested, accepted, feedback), ensure_ascii=False)},
                    ], task["id"])
                    result_provider = usage["provider"]
                    decoded = parse_response(raw, task, requested)
                    valid, forced, feedback = {}, set(required_review), {}
                    for eid, text in decoded["translations"].items():
                        errors = validate_translation(element_map[eid], text, book)
                        warnings = _glossary_issues(element_map[eid], text, glossary)
                        for warning in warnings:
                            store.record_issue(warning)
                        if errors:
                            store.put_candidate(eid, text, "api-invalid", issues=errors)
                            feedback[eid] = [{"code": error["code"], "detail": error["detail"]} for error in errors]
                        else:
                            valid[eid] = text
                            if warnings:
                                forced.add(eid)
                    for english, chinese in decoded.get("proposed_terms", {}).items():
                        issue("glossary_term_proposal", "待协调合并的术语提议", severity="info",
                              english=english, chinese=chinese, task_id=task["id"])
                    buffered_valid = dict(valid)
                    buffered_bindings={eid:_review_binding(element_map[eid],text,book,glossary)
                                       for eid,text in valid.items()}
                    buffered_review = set(forced) | {eid for eid in valid if review_selected(element_map[eid])}
                    store.renew(task["id"], owner, lease_seconds=600)
                    blocked = semantic_review(task, valid, forced, lease_owner=owner)
                    if blocked:
                        required_review.update(blocked)
                        # Review feedback is actionable only for this exact failed
                        # candidate and current source/assets/rules. Old verdicts
                        # must not steer the correction of another revision.
                        diagnostics = store.issues()
                        for eid in blocked:
                            bound_hash = _review_hash(element_map[eid], valid[eid], book, glossary)
                            details = [{"code": "semantic_review_failed", "detail": entry["detail"]}
                                       for entry in diagnostics
                                       if entry.get("code") == "semantic_review_failed"
                                       and entry.get("element_id") == eid
                                       and entry.get("translation_hash") == digest(valid[eid])
                                       and entry.get("review_hash") == bound_hash
                                       and isinstance(entry.get("detail"), str)]
                            if details:
                                feedback[eid] = list({digest(detail): detail for detail in details}.values())
                    valid = {eid: text for eid, text in valid.items() if eid not in blocked}
                    buffered_valid = dict(valid)
                    signature = digest({"missing": decoded["missing_ids"], "invalid": {
                        eid: text for eid, text in decoded["translations"].items() if eid not in valid}})
                    if not valid and signature in failed_signatures:
                        last_error = "Identical invalid output repeated; stopped paid retries, inspect source structure and validation diagnostics"
                        issue("translation_no_progress", "相同无效译文重复返回，已停止重复请求；请检查原文结构及公式边界",
                              task_id=task["id"])
                        break
                    failed_signatures.add(signature)
                    result = store.submit(task["id"], owner, task["source_hash"], valid,
                                          provider=result_provider,
                                          usage=None if hasattr(store, "record_usage") else usage)
                    buffered_valid = {}
                    if result.get("complete"):
                        bump(completed=1)
                        return
                    last_error = "Partial or invalid response; verified elements saved and missing elements remain pending"
                    # A partial submission releases its lease.  Reclaim by ID
                    # before another attempt; another agent may have acquired it.
                    refreshed = store.claim(owner, lease_seconds=600, task_id=task["id"])
                    if refreshed is None:
                        bump(skipped_claimed=1)
                        return
                    task = refreshed
                    if result.get("accepted"):
                        # Recompute remaining IDs next iteration. Complete or
                        # partial valid progress does not consume failure quota.
                        continue
                except _Cancelled:
                    raise
                except _OutputTruncated:
                    bump(translation_truncated_calls=1)
                    groups = packets.popleft()
                    if len(groups) > 1:
                        middle = len(groups) // 2
                        packets.appendleft(groups[middle:])
                        packets.appendleft(groups[:middle])
                        bump(translation_split_packets=1)
                        last_error = "Truncated request split into smaller source-group packets"
                    else:
                        last_error = "Single complete source unit/group truncated; left pending without repeating the request"
                        issue("translation_indivisible_group_truncated", last_error,
                              task_id=task["id"], element_ids=requested)
                        bump(translation_deferred_groups=1)
                    continue
                except _TaskRequestLimit:
                    last_error = "Per-task request hard limit reached; remaining work is pending"
                    issue("translation_request_limit", last_error, task_id=task["id"])
                    break
                except _HTTPFailure as error:
                    last_error, terminal = str(error), not error.retryable
                except _RoutesUnavailable:
                    last_error, terminal = "Translation routes unavailable; progress saved for resumption", False
                    if stop.is_set():
                        break
                except Exception as error:
                    # Never persist exception reprs from libraries/config providers.
                    last_error = "Translation response failed validation: " + type(error).__name__
                if terminal:
                    break
                failures[request_key] += 1
                if failures[request_key] >= translation_provider.attempts:
                    packets.popleft()
                    continue
                if stop.wait(min(30, 2 ** attempt + random.random())):
                    raise _Cancelled()
            release(last_error, retryable=not terminal)
            bump(failed=1)
        except _Cancelled:
            with counter_lock:
                report["cancelled"] = True
            stop.set()
            try:
                preserve_interrupted()
            except Exception:
                bump(interrupted_save_rejected=len(buffered_valid))
            release("Cancelled; resume with the same job database")
        except Exception:
            try:
                release("Worker failed; verified partial results preserved")
            finally:
                bump(failed=1)

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="textbook-translation")
    iterator = iter(pending)
    futures = set()
    try:
        for _ in range(min(workers, len(pending))):
            job = next(iterator)
            futures.add(executor.submit(worker, job["id"]))
        while futures:
            finished, futures = wait(futures, timeout=.5, return_when=FIRST_COMPLETED)
            for future in finished:
                future.result()
                job = next(iterator, None)
                if job is not None and not stop.is_set():
                    futures.add(executor.submit(worker, job["id"]))
    except KeyboardInterrupt:
        report["cancelled"] = True
        stop.set()
        for future in futures:
            future.cancel()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    costs = Counter()
    for value in report["usage_by_provider"].values():
        costs[value["currency"]] += value["estimated_cost"]
    report["estimated_cost_by_currency"] = dict(costs)
    if len(costs) > 1:
        report["estimated_cost"] = None
    report.update(elapsed_seconds=time.perf_counter() - started, state=store.status())
    report["routing"] = session.snapshot()
    return report


def _run_review_sequential(book, store, config_path, provider, max_elements=None, *,
                           _shared_limiter=None, _shared_stop=None, _provider=None, _routing_session=None) -> dict:
    """Independently review accepted migrated/previous translations without retranslating.

    Successful verdicts bind source, formula assets, rule/glossary versions and
    translated text.  They survive re-pagination, but never a content revision.
    A failed review is a blocking diagnostic, not an automatic replacement.
    """
    started = time.perf_counter()
    if max_elements is not None and max_elements < 0:
        raise ValueError("max_elements must be nonnegative")
    glossary, accepted, elements, selected, pending = _review_snapshot(book, store)
    chosen = pending if max_elements is None else pending[:max_elements]
    report = _new_review_report(provider, len(accepted), len(selected), len(pending), len(chosen))
    if not chosen:
        report["elapsed_seconds"] = time.perf_counter() - started
        return report
    from pathlib import Path
    if _provider is None:
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
        selected_provider = _load_provider(config, provider)
    else:
        selected_provider = _provider
    report["provider"], report["model"], report["currency"] = selected_provider.name, selected_provider.model, selected_provider.currency
    report["price_source"] = "config.json per million tokens; not live billing"
    stop = _shared_stop if _shared_stop is not None else threading.Event()
    limiter = _shared_limiter if _shared_limiter is not None else _Limiter(selected_provider.requests_per_minute, stop)
    def review_chat(messages):
        if _routing_session is None:
            limiter.acquire()
        else:
            _routing_session.acquire(only=selected_provider)
        report["requests"] += 1
        failure = None
        try:
            return _chat(selected_provider, messages)
        except BaseException as error:
            failure = error
            raise
        finally:
            if _routing_session is not None:
                _routing_session.release(selected_provider,
                    status=getattr(failure, "status", None),
                    retry_after=getattr(failure, "retry_after", 0.0),
                    failed=failure is not None and not isinstance(failure, (_Cancelled, KeyboardInterrupt)))
    batches = deque(_review_batches(chosen, elements, accepted))
    while batches:
        if _routing_session is not None and selected_provider.name in _routing_session.blocked:
            report['provider_blocked'] = {'provider': selected_provider.name,
                'http_status': _routing_session.blocked[selected_provider.name]}
            report['review_pending'] += sum(len(batch) for batch in batches)
            break
        if stop.is_set():
            report["cancelled"] = True
            report["review_pending"] += sum(len(batch) for batch in batches)
            break
        ids = batches.popleft()
        report["review_batches"] += 1
        truncated = False
        assets = sorted({aid for eid in ids for aid in TOKEN_RE.findall(elements[eid].text)})
        bindings={eid:_review_binding(elements[eid],accepted[eid],book,glossary) for eid in ids}
        body = {"rules_version": RULES_VERSION, "glossary": glossary, "style_rules": list(STYLE_RULES),
                "elements": [{"id": eid, "source": elements[eid].text, "translation": accepted[eid],
                              "review_hash": bindings[eid]['review_hash'], **numeric_policy_fields(elements[eid]), **percentage_policy_fields(elements[eid]), **century_policy_fields(elements[eid])} for eid in ids],
                "protected_assets": {aid: {"text": book.assets[aid].text, "sha256": book.assets[aid].sha256}
                                     for aid in assets}}
        source_hash = digest(body)
        task_id = "review-" + source_hash[:24]
        packet = {"task_id": task_id, "source_hash": source_hash, "payload": body,
                  "required_response": {"task_id": task_id, "source_hash": source_hash,
                                        "reviews": {eid: {"passed": True, "issues": []} for eid in ids}}}
        result, last_error = None, "Review not completed"
        try:
            for attempt in range(selected_provider.attempts):
                accounted = False
                try:
                    raw, usage = review_chat([
                        {"role": "system", "content": "独立审校英文数学教材中译。检查漏译、误译、量词和否定、条件方向、数学含义、术语、数字、引用及文风。不要解题或改译文。只输出required_response格式JSON；passed为布尔值，issues为具体问题字符串数组。"},
                        {"role": "user", "content": json.dumps(packet, ensure_ascii=False)},
                    ])
                    limiter.success()
                    inp, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
                    known = isinstance(inp, int) and isinstance(out, int)
                    inp, out = (inp, out) if known else (0, 0)
                    priced = known and selected_provider.price_in is not None and selected_provider.price_out is not None
                    cost = (inp * selected_provider.price_in + out * selected_provider.price_out) / 1e6 if priced else 0.0
                    report["input_tokens"] += inp
                    report["output_tokens"] += out
                    report["estimated_cost"] += cost
                    report["usage_missing_calls"] += int(not known)
                    report["cost_estimate_complete"] &= bool(priced)
                    if hasattr(store, "record_usage"):
                        store.record_usage(task_id, selected_provider.name, {
                            "stage": "review", "prompt_tokens": inp, "completion_tokens": out,
                            "usage_known": known, "estimated_cost": cost,
                            "model": selected_provider.model, "currency": selected_provider.currency,
                        })
                    accounted = True
                    if usage.get("_truncated"):
                        raise _OutputTruncated("Review output truncated")
                    response = _json_no_duplicates(raw)
                    reviews = response.get("reviews") if isinstance(response, dict) else None
                    if (not isinstance(response, dict) or response.get("task_id") != task_id
                            or response.get("source_hash") != source_hash or not isinstance(reviews, dict)
                            or set(reviews) != set(ids)):
                        raise ValueError("Review envelope mismatch")
                    for verdict in reviews.values():
                        if (not isinstance(verdict, dict) or type(verdict.get("passed")) is not bool
                                or not isinstance(verdict.get("issues"), list)
                                or any(not isinstance(v, str) for v in verdict["issues"])
                                or (verdict["passed"] and verdict["issues"])):
                            raise ValueError("Invalid review verdict")
                    result = reviews
                    break
                except _Cancelled:
                    raise
                except _RoutesUnavailable:
                    last_error = "当前复核路线不可用，保留待审状态；其他翻译路线仍可继续"
                    report['provider_blocked'] = {'provider': selected_provider.name,
                        'http_status': _routing_session.blocked.get(selected_provider.name) if _routing_session else None}
                    break  # No HTTP request was issued; do not invent usage.
                except _OutputTruncated:
                    report["failed_requests"] += 1
                    report["review_truncated_calls"] += 1
                    last_error = "审校回复达到长度上限，单个完整元素仍未完成；保留待审状态"
                    truncated = True
                    break  # Split below; never repeat the same truncated payload.
                except _HTTPFailure as error:
                    report["failed_requests"] += 1
                    report["usage_missing_calls"] += 1
                    report["cost_estimate_complete"] = False
                    if hasattr(store, "record_usage"):
                        store.record_usage(task_id, selected_provider.name, {
                            "stage": "review", "request_failed": True, "http_status": error.status,
                            "usage_known": False, "prompt_tokens": 0, "completion_tokens": 0,
                            "estimated_cost": 0.0, "currency": selected_provider.currency,
                        })
                    last_error = str(error)
                    if error.status in {401, 402, 403}:
                        report['provider_blocked'] = {'provider': selected_provider.name, 'http_status': error.status}
                        if _routing_session is None:
                            stop.set()
                    if error.status == 429 or error.status >= 500:
                        limiter.throttle(error.retry_after)
                    if not error.retryable:
                        break
                except Exception as error:
                    report["failed_requests"] += 1
                    if not accounted:
                        report["usage_missing_calls"] += 1
                        report["cost_estimate_complete"] = False
                        if hasattr(store, "record_usage"):
                            store.record_usage(task_id, selected_provider.name, {
                                "stage": "review", "request_failed": True,
                                "usage_known": False, "prompt_tokens": 0, "completion_tokens": 0,
                                "estimated_cost": 0.0, "currency": selected_provider.currency,
                            })
                    last_error = type(error).__name__
                if attempt + 1 < selected_provider.attempts:
                    stop.wait(min(30, 2 ** attempt + random.random()))
        except (KeyboardInterrupt, _Cancelled):
            report["cancelled"] = True
            stop.set()
            report["review_pending"] += len(ids) + sum(len(batch) for batch in batches)
            break
        if truncated and len(ids) > 1:
            middle = len(ids) // 2
            batches.appendleft(ids[middle:])
            batches.appendleft(ids[:middle])
            report["review_split_batches"] += 1
            continue
        if result is None:
            for eid in ids:
                applied=_commit_review(store,elements[eid],accepted[eid],book,glossary,None,
                    ["独立语义复核尚未成功完成："+last_error],selected_provider.name,binding=bindings[eid])
                report['review_pending' if applied['applied'] else 'stale_results']+=1
            continue
        for eid, verdict in result.items():
            applied=_commit_review(store,elements[eid],accepted[eid],book,glossary,
                verdict['passed'],verdict['issues'],selected_provider.name,binding=bindings[eid])
            if not applied['applied']:
                report["stale_results"] += 1
                continue
            if verdict["passed"]:
                report["review_passed"] += 1
            else:
                report["review_failed"] += 1
    report["elapsed_seconds"] = time.perf_counter() - started
    return report

def run_review(book, store, config_path, provider, max_elements=None, workers=4, *, unit_ids=None, max_units=None, argument_ids=None, max_arguments=None, routing_session=None) -> dict:
    """Review disjoint current IDs with bounded workers and one provider limiter.

    Only temporary Book element views are partitioned; Store registration,
    source elements, frozen rules and hash-bound review identities stay intact.
    Interrupts stop future requests and let valid in-flight replies be saved.
    """
    if routing_session is not None and (book.profile.get("argument_units_v1") or book.profile.get("semantic_review_units")):
        raise ValueError("Shared automatic routing applies to ordinary review; use the dedicated joint-review stage")
    if book.profile.get("argument_units_v1"):
        from .argument_review_runner import run_review_with_arguments
        return run_review_with_arguments(book,store,config_path,provider,max_elements,workers,unit_ids=unit_ids,max_units=max_units,argument_ids=argument_ids,max_arguments=max_arguments)
    if argument_ids is not None or max_arguments is not None:
        raise ValueError("No explicit argument units are configured")
    if book.profile.get("semantic_review_units"):
        from .semantic_unit_runner import run_review_with_units
        return run_review_with_units(book,store,config_path,provider,max_elements,workers,unit_ids=unit_ids,max_units=max_units)
    if unit_ids is not None or max_units is not None:
        raise ValueError("No explicit semantic-review units are configured")
    started = time.perf_counter()
    if not isinstance(workers, int) or workers < 1 or workers > 32:
        raise ValueError("workers must be between 1 and 32")
    if max_elements is not None and max_elements < 0:
        raise ValueError("max_elements must be nonnegative")
    binding_migration = migrate_review_bindings(book, store)
    glossary, accepted, elements, selected, pending = _review_snapshot(book, store)
    chosen = pending if max_elements is None else pending[:max_elements]
    report = _new_review_report(provider, len(accepted), len(selected), len(pending), len(chosen))
    report["review_binding_migration"] = binding_migration
    report.update(workers_requested=workers, workers_actual=0, worker_failures=0,
                  worker_elapsed_seconds=0.0)
    if not chosen:
        report["elapsed_seconds"] = time.perf_counter() - started
        return report
    from pathlib import Path
    config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    selected_provider = _load_provider(config, provider)
    report.update(provider=selected_provider.name, model=selected_provider.model,
                  currency=selected_provider.currency,
                  price_source="config.json per million tokens; not live billing")
    if routing_session is not None:
        if (not routing_session.matches(config, routing_session.name)
                or routing_session.participants.get(selected_provider.name) != selected_provider):
            raise ValueError("Routing session must include its independent review provider")
        if selected_provider.name in routing_session.blocked:
            report["provider_blocked"] = {"provider": selected_provider.name,
                                          "http_status": routing_session.blocked[selected_provider.name]}
            report["review_pending"] = len(chosen)
            report["elapsed_seconds"] = time.perf_counter() - started
            return report
        stop = routing_session.stop
        limiter = routing_session.limiters[routing_session.groups[selected_provider.name]]
    else:
        stop = threading.Event()
        limiter = _Limiter(selected_provider.requests_per_minute, stop)
    initial_batches = _review_batches(chosen, elements, accepted)
    capacity = (routing_session.capacity[routing_session.groups[selected_provider.name]]
                if routing_session is not None else selected_provider.max_concurrency)
    count = min(workers, capacity, len(initial_batches))
    report["workers_actual"] = count
    partitions = [[] for _ in range(count)]
    for index, ids in enumerate(initial_batches):
        partitions[index % count].extend(ids)
    totals = ("review_passed", "review_failed", "review_pending", "review_batches",
              "review_split_batches", "review_truncated_calls", "stale_results", "requests",
              "failed_requests", "usage_missing_calls", "input_tokens", "output_tokens", "estimated_cost")
    executor = ThreadPoolExecutor(max_workers=count, thread_name_prefix="textbook-review")
    futures, collected = {}, set()
    def collect(future):
        if future in collected:
            return
        if future.cancelled():
            report["review_pending"] += len(futures[future])
            collected.add(future)
            return
        try:
            child = future.result()
        except Exception as error:
            report["worker_failures"] += 1
            report["review_pending"] += len(futures[future])
            report["cost_estimate_complete"] = False
            store.record_issue({"code": "semantic_review_worker_failed", "severity": "warning",
                                "detail": "独立审校工作线程未完成：" + type(error).__name__})
        else:
            for key in totals:
                report[key] += child[key]
            report["cost_estimate_complete"] &= child["cost_estimate_complete"]
            report["cancelled"] |= child["cancelled"]
            if child.get('provider_blocked'):
                report['provider_blocked'] = child['provider_blocked']
            report["worker_elapsed_seconds"] += child.get("elapsed_seconds", 0.0)
        collected.add(future)
    try:
        for ids in partitions:
            view = replace(book, elements=[elements[eid] for eid in ids])
            future = executor.submit(_run_review_sequential, view, store, config_path, selected_provider.name,
                                     _shared_limiter=limiter, _shared_stop=stop, _provider=selected_provider,
                                     _routing_session=routing_session)
            futures[future] = ids
        remaining = set(futures)
        while remaining:
            finished, remaining = wait(remaining, timeout=.5, return_when=FIRST_COMPLETED)
            for future in finished:
                collect(future)
    except KeyboardInterrupt:
        report["cancelled"] = True
        stop.set()
        for future in futures:
            future.cancel()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        for future in futures:
            collect(future)
    current = review_status(book, store)
    report["current_review_status"] = {key: value for key, value in current.items() if key != "ids"}
    report["elapsed_seconds"] = time.perf_counter() - started
    return report

