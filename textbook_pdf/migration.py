"""Read-only, source-verified importer for the legacy manifest/_cache.db pair."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .models import TOKEN_RE, TRANSLATABLE, file_sha256, plain_text, validate_translation, needs_translation, digest
from .translation import _json_no_duplicates, review_selected, _review_hash, REVIEW_HASH_SCHEMA

# Verified against full prompt hashes in the immutable Rosen cache audit.
# Unknown versions and matching chunk IDs never authorize source reuse.
LEGACY_PROMPT_VERSIONS = ("v6", "v5")


def _prompt(blocks, indices):
    parts = []
    previous_page = None
    for n, index in enumerate(indices, 1):
        block = blocks[index]
        prefix = f"[第{block['page']}页]\n" if n == 1 or block["page"] != previous_page else ""
        parts.append(f"{prefix}### {n}\n{block['text']}")
        previous_page = block["page"]
    return "\n\n".join(parts)


def _blob(raw, expected):
    try:
        value = _json_no_duplicates(raw)
        if not isinstance(value, dict):
            raise ValueError("Legacy blob is not an object")
        result = {}
        for key, text in value.items():
            index = int(key)
            if index not in range(1, expected + 1) or index in result or not isinstance(text, str):
                raise ValueError("Invalid legacy block ID or translation")
            if text.strip():
                result[index] = text
        return result
    except json.JSONDecodeError:
        # Supported historical numbered replies.  Duplicate/out-of-range IDs
        # reject a blob instead of guessing which reply was intended.
        marker = re.compile(r"^(?:#{0,6}\s*(\d{1,4})\s*\|\|\|\s*(.*)|#{1,6}\s*(\d{1,4})(?:[.):：]?\s+(.*))?|\s*(\d{1,4})\s*)$")
        result, number, parts = {}, None, []
        def flush():
            if number is not None:
                text = "\n".join(parts).strip()
                if text:
                    result[number] = text
        seen = set()
        for line in raw.splitlines():
            match = marker.match(line.strip())
            if match:
                flush()
                number = int(match[1] or match[3] or match[5])
                if number < 1 or number > expected or number in seen:
                    raise ValueError("Duplicate or unexpected legacy block marker")
                seen.add(number)
                first = match[2] if match[1] else (match[4] or "")
                parts = [first] if first else []
            elif re.fullmatch(r"\[第\d+页\]", line.strip()):
                continue
            elif number is not None:
                parts.append(line)
            elif line.strip():
                raise ValueError("Unrecognized legacy preamble")
        flush()
        return result


def _aligned(left, right):
    """Require the same physical rectangle, allowing only PDF rounding noise."""
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)) or len(left) != 4 or len(right) != 4:
        return False
    try:
        return all(abs(float(a) - float(b)) <= 1.0 for a, b in zip(left, right))
    except (TypeError, ValueError):
        return False


def restore_protected_tokens(element, translated, book):
    """Recover object tokens only from unambiguous, verbatim formula text.

    Every protected expression must be present with exactly the expected count;
    a single Latin variable is matched outside English words.  Identical text
    mapped to different source IDs is ambiguous without a verified crop identity.
    Bundle SHA equality is never used as a substitute for that missing proof.
    """
    expected = TOKEN_RE.findall(element.text)
    if not expected:
        return translated
    present = TOKEN_RE.findall(translated)
    if present:
        if Counter(present) != Counter(expected):
            raise ValueError("Legacy protected token set does not match source")
        return translated
    by_text = defaultdict(list)
    for aid in expected:
        asset = book.assets.get(aid)
        if asset is None or not asset.text or not asset.text.strip():
            raise ValueError("Protected source expression has no verified text")
        by_text[asset.text].append(aid)
    replacements = []
    for source, ids in sorted(by_text.items(), key=lambda pair: -len(pair[0])):
        if len(set(ids)) > 1:
            # sha256 may identify an entire multi-crop PDF bundle. Equal bundle
            # bytes cannot prove which crop, baseline or rendered object a plain
            # legacy expression meant; never guess by occurrence/chunk order.
            raise ValueError("Identical formula text maps to multiple source objects; legacy text cannot identify the original crop")
        pattern = re.escape(source)
        if source[0].isascii() and source[0].isalnum():
            pattern = r"(?<![A-Za-z0-9_])" + pattern
        if source[-1].isascii() and source[-1].isalnum():
            pattern += r"(?![A-Za-z0-9_])"
        matches = list(re.finditer(pattern, translated))
        matches = [m for m in matches if not any(m.start() < end and m.end() > start for start, end, _ in replacements)]
        if len(matches) != len(ids):
            raise ValueError("Protected expression is missing, changed, or occurs ambiguously in legacy translation")
        for match, aid in zip(matches, ids):
            replacements.append((match.start(), match.end(), f"⟦F:{aid}⟧"))
    for start, end, replacement in sorted(replacements, reverse=True):
        translated = translated[:start] + replacement + translated[end:]
    if Counter(TOKEN_RE.findall(translated)) != Counter(expected):
        raise ValueError("Failed to recover all source formula tokens")
    return translated


def import_legacy(book, store, legacy_dir) -> dict:
    """Import only rows whose prompt hash, source identity, text and geometry agree.

    A row's chunk_id is used only for diagnostics; it is never a lookup key.
    Existing accepted translations are kept.  Conflicting exact cache entries
    remain candidates for review instead of accepting the newest arbitrarily.
    """
    directory = Path(legacy_dir)
    manifest_path, cache_path = directory / "manifest.json", directory / "_cache.db"
    if not manifest_path.is_file() or not cache_path.is_file():
        raise FileNotFoundError("Legacy import requires manifest.json and _cache.db")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    report = {"exact": 0, "exact_unit": "legacy chunks with verified prompt hash",
              "imported": 0, "rejected": 0, "candidates": 0, "already_accepted": 0,
              "legacy_chunks": len(manifest.get("chunks", [])), "unmatched_chunks": 0,
              "source_matches": 0, "geometry_matches": 0, "reasons": {},
              "scoped_chunks": 0, "out_of_scope_chunks": 0, "out_of_scope_blocks": 0,
              "source_identity_verified": False, "legacy_database_read_only": True,
              "manifest_prompt_version": manifest.get("prompt_version"),
              "accepted_prompt_versions": list(LEGACY_PROMPT_VERSIONS),
              "exact_by_prompt_version": {version: 0 for version in LEGACY_PROMPT_VERSIONS},
              "imported_by_prompt_version": {version: 0 for version in LEGACY_PROMPT_VERSIONS},
              "exact_manifest_version": 0, "exact_historical_only": 0,
              "historical_semantic_pending": 0}
    reasons = Counter()

    def reject(code, detail, element=None, **extra):
        reasons[code] += 1
        report["rejected"] += 1
        entry = {"code": code, "severity": "warning", "detail": detail,
                 "provenance": "legacy", **extra}
        if element is not None:
            entry.update(element_id=element.id, page=element.page)
        store.record_issue(entry)

    legacy_source = Path(manifest.get("pdf", ""))
    claimed_sha = manifest.get("source_sha256") or manifest.get("pdf_sha256")
    if legacy_source.is_file():
        actual_sha = file_sha256(legacy_source)
        if actual_sha != book.source_sha256 or (claimed_sha and claimed_sha != actual_sha):
            reject("legacy_source_identity_mismatch", "旧清单指向的原PDF与当前书籍不是同一文件")
            report["reasons"] = dict(reasons)
            return report
        report["source_identity_verified"] = True
    elif claimed_sha and claimed_sha == book.source_sha256:
        report["source_identity_verified"] = True
    else:
        reject("legacy_source_identity_unverified", "旧原PDF不可读取且没有可验证的SHA256；未导入任何译文")
        report["reasons"] = dict(reasons)
        return report
    blocks, chunks = manifest.get("blocks"), manifest.get("chunks")
    if not isinstance(blocks, list) or not isinstance(chunks, list) or not manifest.get("prompt_version") or not manifest.get("fkey"):
        raise ValueError("Legacy manifest is missing blocks, chunks, prompt version or file key")
    if manifest["prompt_version"] not in LEGACY_PROMPT_VERSIONS:
        reject("legacy_prompt_version_unsupported", "旧清单版本不在已核验的v6/v5白名单内；未导入任何译文")
        report["reasons"] = dict(reasons)
        return report
    selected_pages = set(book.selected_pages)
    report["out_of_scope_blocks"] = sum(block.get("page") not in selected_pages for block in blocks)
    text_index = defaultdict(list)
    element_map = book.element_map()
    for element in book.elements:
        if needs_translation(element):
            try:
                text_index[(element.page, plain_text(element, book))].append(element)
            except KeyError:
                reject("legacy_source_asset_missing", "当前结构中的公式资产缺失", element)
    # SQLite URI mode=ro prevents schema creation, writes and implicit upgrades.
    database = sqlite3.connect(cache_path.resolve().as_uri() + "?mode=ro", uri=True)
    proposals = defaultdict(list)
    try:
        rows = database.execute(
            "SELECT chunk_hash,model,translation FROM chunks WHERE file_key=?", (manifest["fkey"],)
        ).fetchall()
        indexed_rows = defaultdict(list)
        for chunk_hash, model, translation in rows:
            if isinstance(model, str):
                indexed_rows[model].append((chunk_hash, translation))
        rows_by_model = {model: dict(values) for model, values in indexed_rows.items()}
        for chunk in chunks:
            indices = chunk.get("idxs")
            if (not isinstance(indices, list) or len(set(indices)) != len(indices)
                    or any(type(i) is not int or i < 0 or i >= len(blocks) for i in indices)):
                reject("legacy_invalid_chunk", "旧清单的块索引无效", legacy_chunk_id=chunk.get("id"))
                continue
            if not any(blocks[i].get("page") in selected_pages for i in indices):
                report["out_of_scope_chunks"] += 1
                continue
            report["scoped_chunks"] += 1
            prompt = _prompt(blocks, indices)
            matches = []
            for version in LEGACY_PROMPT_VERSIONS:
                for model, values in rows_by_model.items():
                    expected_hash = hashlib.sha1(f"{version}|{model}|{prompt}".encode()).hexdigest()
                    if expected_hash in values:
                        matches.append((version, model, values[expected_hash]))
            if not matches:
                report["unmatched_chunks"] += 1
                reject("legacy_prompt_hash_mismatch", "缓存与当前旧清单原文不精确匹配；未使用chunk_id兜底", legacy_chunk_id=chunk.get("id"))
                continue
            report["exact"] += 1
            versions = {version for version, _, _ in matches}
            for version in versions:
                report["exact_by_prompt_version"][version] += 1
            report["exact_manifest_version"] += int(manifest["prompt_version"] in versions)
            report["exact_historical_only"] += int(manifest["prompt_version"] not in versions)
            for version, model, raw in matches:
                try:
                    parsed = _blob(raw, len(indices))
                except (ValueError, TypeError, OverflowError):
                    reject("legacy_invalid_reply", "旧缓存回复不能无歧义地解析", legacy_chunk_id=chunk.get("id"),
                           legacy_prompt_version=version)
                    continue
                for n, index in enumerate(indices, 1):
                    old = blocks[index]
                    if old.get("page") not in selected_pages:
                        continue
                    if old.get("kind") in {"keep", "figure"}:
                        continue
                    if n not in parsed:
                        reject("legacy_missing_translation", "旧缓存缺少该原文块译文", legacy_block_index=index,
                               page=old.get("page"))
                        continue
                    matches_text = text_index.get((old.get("page"), old.get("text")), [])
                    if not matches_text:
                        reject("legacy_no_exact_source", "原文文本或物理页码与新结构不完全一致", legacy_block_index=index,
                               page=old.get("page"))
                        continue
                    report["source_matches"] += 1
                    aligned = [e for e in matches_text if _aligned(old.get("rect"), e.bbox)]
                    if len(aligned) != 1:
                        reject("legacy_geometry_ambiguous", "原文虽相同但来源区域不唯一或不一致", legacy_block_index=index,
                               page=old.get("page"))
                        continue
                    report["geometry_matches"] += 1
                    element = aligned[0]
                    report["candidates"] += 1
                    try:
                        restored = restore_protected_tokens(element, parsed[n], book)
                    except ValueError as error:
                        problem = {"code": "legacy_formula_ambiguous", "detail": str(error), "severity": "error",
                                   "element_id": element.id, "page": element.page}
                        store.put_candidate(element.id, parsed[n], "legacy-" + version + "-" + model, issues=[problem])
                        reject("legacy_formula_ambiguous", str(error), element)
                        continue
                    errors = validate_translation(element, restored, book)
                    if errors:
                        store.put_candidate(element.id, restored, "legacy-" + version + "-" + model, issues=errors)
                        reject("legacy_translation_invalid", "旧译文未通过公式、数字或字符完整性检查", element)
                        continue
                    proposals[element.id].append((restored, model, version))
    finally:
        database.close()
    accepted = store.translations()
    for element_id, candidates in proposals.items():
        element = element_map[element_id]
        distinct = {text for text, _, _ in candidates}
        if len(distinct) > 1:
            problem = {"code": "legacy_conflicting_candidates", "detail": "多个精确来源缓存给出不同译文，需人工选择",
                       "severity": "error", "element_id": element_id, "page": element.page}
            for text, model, version in candidates:
                store.put_candidate(element_id, text, "legacy-" + version + "-" + model, issues=[problem])
            reject("legacy_conflicting_candidates", problem["detail"], element)
            continue
        text, model, version = candidates[0]
        if element_id in accepted:
            report["already_accepted"] += 1
            continue
        result = store.put_candidate(element_id, text, "legacy-exact-source-geometry-" + version + "-" + model)
        imported = result.get("accepted", False) if isinstance(result, dict) else bool(result)
        if imported:
            report["imported"] += 1
            report["imported_by_prompt_version"][version] += 1
            historical = version == "v5"
            if historical or review_selected(element):
                store.record_issue({"code": "semantic_review_pending", "severity": "warning", "element_id": element_id,
                                    "page": element.page, "detail": "精确迁移译文已通过结构检查，尚未执行新版本语义复核",
                                    "source_hash": element.source_hash, "translation_hash": digest(text),
                                    "review_hash": _review_hash(element, text, book, dict(getattr(store, "glossary", {}))),
                                    "review_hash_schema": REVIEW_HASH_SCHEMA,
                                    "legacy_prompt_version": version})
                report["historical_semantic_pending"] += int(historical)
        else:
            reject("legacy_store_rejected", "共享状态拒绝接受该迁移候选", element)
    report["reasons"] = dict(reasons)
    return report
