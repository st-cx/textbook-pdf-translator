"""Versioned neutral document format shared by parsing, translation and composition."""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = 2
RULES_VERSION = "textbook-2026-10-v1"
TOKEN_RE = re.compile(r"⟦F:([A-Za-z0-9_.-]+)⟧")
TRANSLATABLE = {"paragraph", "heading", "exercise", "caption", "footnote", "sidebar", "table_cell", "figure_label"}

def needs_translation(element):
    return element.kind in TRANSLATABLE and element.metadata.get('translatable',True) and not element.metadata.get('preserve_only',False) and bool(element.text.strip())

def safe_filename(value):
    value=re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',str(value)).strip(' .')[:100]
    if not value or value.upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:
        value='translated_book'
    return value

def digest(value) -> str:
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()

def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

@dataclass
class Asset:
    id: str
    kind: str
    path: str
    page: int
    bbox: list[float]
    width: float
    height: float
    baseline: float = 0.0
    text: str = ""
    sha256: str = ""
    metadata: dict = field(default_factory=dict)


def protected_asset_descriptor(asset, include_render_metadata=True):
    """Content identity independent of a copied asset's filesystem location."""
    value={'sha256':asset.sha256,'kind':asset.kind,'text':asset.text,
           'page':asset.page,'bbox':asset.bbox,'width':asset.width,
           'height':asset.height,'baseline':asset.baseline}
    if include_render_metadata:
        value['render_metadata']={key:asset.metadata[key] for key in
            ('pdf_page','baseline_from_top','source_segments','cross_page_math') if key in asset.metadata}
    return value

@dataclass
class Element:
    id: str
    kind: str
    text: str
    page: int
    bbox: list[float] = field(default_factory=list)
    printed_page: str | None = None
    style: dict = field(default_factory=dict)
    asset_ids: list[str] = field(default_factory=list)
    group_id: str = ""
    chapter: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def source_hash(self):
        return digest({"text": self.text, "assets": self.asset_ids, "kind": self.kind})


class _ElementList(list):
    """Track membership/reordering while keeping normal list JSON behavior."""
    def __init__(self,values=()):
        super().__init__(values);self.revision=0
    def _touch(self):self.revision=getattr(self,'revision',0)+1
    def __setitem__(self,key,value):self._touch();return super().__setitem__(key,value)
    def __delitem__(self,key):self._touch();return super().__delitem__(key)
    def append(self,value):self._touch();return super().append(value)
    def extend(self,values):self._touch();return super().extend(values)
    def insert(self,index,value):self._touch();return super().insert(index,value)
    def pop(self,*args):self._touch();return super().pop(*args)
    def remove(self,value):self._touch();return super().remove(value)
    def clear(self):self._touch();return super().clear()
    def reverse(self):self._touch();return super().reverse()
    def sort(self,*args,**kwargs):self._touch();return super().sort(*args,**kwargs)
    def __iadd__(self,values):self._touch();return super().__iadd__(values)
    def __imul__(self,value):self._touch();return super().__imul__(value)

@dataclass
class Book:
    source_path: str
    source_sha256: str
    title: str
    page_count: int
    selected_pages: list[int]
    pages: list[dict] = field(default_factory=list)
    elements: list[Element] = field(default_factory=list)
    assets: dict[str, Asset] = field(default_factory=dict)
    profile: dict = field(default_factory=lambda: {"body_size": 10.5, "exercise_size": 9.5, "caption_size": 8.5, "heading_color": "00a9df", "body_font": "SimSun", "heading_font": "SimHei"})
    issues: list[dict] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(20):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt==19:raise
                time.sleep(.025*(1+min(attempt,5)))

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported document schema; analyze again with a matching tool version")
        data["elements"] = [Element(**e) for e in data["elements"]]
        data["assets"] = {k: Asset(**v) for k, v in data["assets"].items()}
        return cls(**data)

    def element_map(self):
        if not isinstance(self.elements,_ElementList):self.elements=_ElementList(self.elements)
        revision=self.elements.revision
        if getattr(self,'_element_map_source',None) is not self.elements or getattr(self,'_element_map_revision',None)!=revision:
            self._element_index={e.id:e for e in self.elements}
            self._element_map_revision=revision
            self._element_map_source=self.elements
        return self._element_index

def plain_text(element: Element, book: Book) -> str:
    return TOKEN_RE.sub(lambda m: book.assets[m[1]].text, element.text)

def source_label(element: Element) -> str:
    return f"原书 p.{element.printed_page}" if element.printed_page else f"原 PDF 第{element.page}页"

def source_error_scope(book):
    """Block every declared owner of a source error before forming requests.

    A multi-element issue can describe one question across columns or pages.
    Its singular element_id is only an anchor; all element_ids remain blocked.
    An unresolved owner falls back to its page, and a global error blocks all
    selected pages. Malformed scopes fail closed rather than dropping a hold.
    """
    known = set(book.element_map())
    blocked_ids, blocked_pages = set(), set()
    for issue in book.issues:
        if issue.get('severity') != 'error':
            continue
        members = issue.get('element_ids', [])
        if members is None:
            members = []
        anchor = issue.get('element_id')
        if (not isinstance(members, list) or
                any(not isinstance(eid, str) or not eid.strip() for eid in members) or
                (anchor is not None and (not isinstance(anchor, str) or not anchor.strip()))):
            raise ValueError('Malformed source issue element scope')
        ids = set(members)
        if anchor is not None:
            ids.add(anchor)
        page = issue.get('page')
        if page is not None and (type(page) is not int or not 1 <= page <= book.page_count):
            raise ValueError('Malformed source issue page scope')
        blocked_ids.update(ids)
        if not ids or ids - known:
            blocked_pages.update([page] if page is not None else book.selected_pages)
    return blocked_ids, blocked_pages


def source_contract_issues(book):
    """Structural failures must be reportable before a paid task is created."""
    from collections import Counter
    issues=[]
    duplicates={eid for eid,count in Counter(e.id for e in book.elements).items() if count>1}
    for e in book.elements:
        if "century_policy" in e.metadata:
            from .century_policy import century_policy_fields
            try:
                century_policy_fields(e)
            except (ValueError, TypeError, KeyError) as error:
                issues.append({"code":"source_century_policy_invalid","severity":"error",
                    "element_id":e.id,"page":e.page,"detail":str(error)})
        if "numeric_policy" in e.metadata:
            from .numeric_policy_dispatch import numeric_policy_fields
            try:
                numeric_policy_fields(e)
            except (ValueError, TypeError, KeyError) as error:
                issues.append({"code":"source_numeric_policy_invalid","severity":"error",
                    "element_id":e.id,"page":e.page,"detail":str(error)})
        if "percentage_unit_policy" in e.metadata:
            from .percentage_policy import percentage_policy_fields
            try:
                percentage_policy_fields(e)
            except (ValueError, TypeError, KeyError) as error:
                issues.append({"code":"source_percentage_policy_invalid","severity":"error",
                    "element_id":e.id,"page":e.page,"detail":str(error)})
        if e.id in duplicates:
            issues.append({'code':'source_duplicate_element','severity':'error','element_id':e.id,'page':e.page,'detail':'原文元素编号重复，不能建立可靠翻译任务'})
        missing=(set(TOKEN_RE.findall(e.text))|set(e.asset_ids))-book.assets.keys()
        if missing:
            issues.append({'code':'source_asset_reference_missing','severity':'error','element_id':e.id,'page':e.page,'detail':'原文引用的受保护对象不存在，须先修复来源记录','asset_ids':sorted(missing)})
    if book.profile.get("argument_units_v1") or any("argument_member_v1" in e.metadata for e in book.elements) or any(g.get("disposition")=="reconstructed-source-primitive" for p in book.pages for g in p.get("graphics_coverage", [])):
        from .argument_units import source_issues
        issues.extend(source_issues(book))
    return issues


def validate_translation(element: Element, translated: str, book: Book | None = None) -> list[dict]:
    issues = []
    def issue(code, detail):
        issues.append({"code": code, "detail": detail, "element_id": element.id, "page": element.page, "severity": "error"})
    if not isinstance(translated, str) or not translated.strip():
        issue("empty_translation", "译文为空")
        return issues
    expected, actual = TOKEN_RE.findall(element.text), TOKEN_RE.findall(translated)
    from collections import Counter
    if Counter(expected) != Counter(actual):
        issue("protected_token_mismatch", f"应有 {expected}，实有 {actual}")
    if "⟦" in TOKEN_RE.sub("", translated) or "⟧" in TOKEN_RE.sub("", translated):
        issue("malformed_token", "公式标记被修改")
    if "\ufffd" in translated or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", translated):
        issue("invalid_character", "译文含替换字符或控制字符")
    src = TOKEN_RE.sub("", element.text)
    dst = TOKEN_RE.sub("", translated)
    # CJK letters border numerals naturally (第5章、1.8节). Unicode \w would
    # incorrectly suppress those numerals and reject otherwise faithful Chinese.
    from .numbers import numeric_fingerprint
    if "century_policy" in element.metadata:
        from .century_policy import century_number_issues
        issues.extend(century_number_issues(element, translated))
    elif "numeric_policy" in element.metadata:
        from .numeric_policy_dispatch import policy_number_issues
        issues.extend(policy_number_issues(element, translated))
    elif numeric_fingerprint(src) != numeric_fingerprint(dst):
        issue("number_mismatch", "正文数字与原文不一致，需要复核")
    if "percentage_unit_policy" in element.metadata:
        from .percentage_policy import percentage_unit_issues
        issues.extend(percentage_unit_issues(element, translated))
    if re.search(r"[A-Za-z]{3,}", src) and not re.search(r"[\u3400-\u9fff]", dst):
        issue("untranslated", "自然语言段落未检测到中文")
    return issues
