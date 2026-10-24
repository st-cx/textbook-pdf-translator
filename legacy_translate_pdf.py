# -*- coding: utf-8 -*-
"""
大型英文数学 PDF 教材 → 中文/双语 Markdown 翻译工具
====================================================
专为《Discrete Mathematics and Its Applications》这类大型英文数学教材设计：
  - 逐块抽取文本（保留标题层级、阅读顺序、连字符修复）
  - 公式/符号/真值表密集的块自动原样保留，不做机翻
  - 离散数学/微积分/概率论术语表约束，保证全书术语一致
  - 并发翻译 + SQLite 缓存断点续传（1118 页中断随时可续）
  - 输出：双语 Markdown、纯中文 Markdown、带样式 HTML

用法示例：
  python translate_pdf.py "离散数学及其应用.pdf"                    # 整本翻译
  python translate_pdf.py "xx.pdf" --pages 50-60                   # 只翻第50~60物理页
  python translate_pdf.py "xx.pdf" --estimate                      # 只估算 token 与费用
  python translate_pdf.py --import-ccswitch                        # 从 CC-Switch 导入密钥
  python translate_pdf.py --test                                   # 测试各供应商连通性
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pymupdf
except ImportError:
    sys.exit("缺少依赖：pip install pymupdf")
try:
    from openai import OpenAI
except ImportError:
    sys.exit("缺少依赖：pip install openai")

if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROMPT_VERSION = "v6"
CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks(
    file_key TEXT, chunk_hash TEXT, chunk_id INTEGER,
    model TEXT, translation TEXT, created_at REAL,
    PRIMARY KEY(file_key, chunk_hash)
);
"""

# ---------------------------------------------------------------------------
# 术语表（写在系统提示里，保证全书译名一致）
# ---------------------------------------------------------------------------
GLOSSARY = """proposition=命题; truth table=真值表; logically equivalent=逻辑等价; tautology=重言式;
contradiction=矛盾式; contingency=偶然式; converse=逆命题; contrapositive=逆否命题;
predicate=谓词; quantifier=量词; existential quantifier=存在量词; universal quantifier=全称量词;
argument=论证; premise=前提; conclusion=结论; valid=有效的; fallacy=谬误;
proof by contradiction=反证法; proof by contraposition=逆否证明法; vacuous proof=空证明;
trivial proof=平凡证明; direct proof=直接证明; exhaustion=穷举证明; counterexample=反例;
set=集合; subset=子集; proper subset=真子集; union=并集; intersection=交集;
difference=差集; complement=补集; Cartesian product=笛卡儿积; power set=幂集;
function=函数; domain=定义域; codomain=陪域; range=值域; one-to-one=一对一的(单射);
onto=满射; bijection=双射; inverse function=反函数; floor=下取整; ceiling=上取整;
sequence=序列; series=级数; recurrence relation=递推关系; summation=求和;
induction=数学归纳法; strong induction=强归纳法; well-ordering property=良序性质;
countable=可数的; uncountable=不可数的; cardinality=基数; matrix=矩阵;
division algorithm=整除算法(带余除法); divisor=因子; prime=素数; composite=合数;
greatest common divisor=最大公因子; least common multiple=最小公倍数;
congruence=同余; modular arithmetic=模运算; hash function=散列函数;
base=进制/基数; Boolean algebra=布尔代数; Boolean function=布尔函数;
relation=关系; reflexive=自反的; symmetric=对称的; antisymmetric=反对称的;
transitive=传递的; equivalence relation=等价关系; equivalence class=等价类;
partial ordering=偏序; lattice=格; Hasse diagram=哈斯图; total ordering=全序;
graph=图; vertex=顶点; edge=边; directed graph=有向图; undirected graph=无向图;
simple graph=简单图; multigraph=多重图; bipartite=二部图; degree=度数;
path=路径/通路; circuit=回路; connected=连通的; Euler circuit=欧拉回路;
Hamilton path=哈密顿路径; tree=树; rooted tree=根树; spanning tree=生成树;
leaf=叶结点; subtree=子树; adjacency list=邻接表; adjacency matrix=邻接矩阵;
isomorphism=同构; planar=可平面化的; chromatic number=着色数;
combinatorics=组合数学; permutation=排列; combination=组合; pigeonhole principle=鸽笼原理(抽屉原理);
Binomial Theorem=二项式定理; probability=概率; sample space=样本空间;
event=事件; conditional probability=条件概率; independence=相互独立;
random variable=随机变量; expected value=期望值; variance=方差;
algorithm=算法; pseudocode=伪代码; time complexity=时间复杂度;
Big-O=大O记号; Big-Omega=大Ω记号; Big-Theta=大Θ记号; greedy algorithm=贪心算法"""

SYSTEM_PROMPT = f"""你是一位专业的英文数学教材译者，负责把英文数学教材逐段译成简体中文（面向中国大学生）。
必须遵守的规则：
1. 只翻译，不改写、不解释、不增删内容；例题编号(EXAMPLE 3→例3)、图表引用(Table 4→表4)、章节引用保持对应。
2. 【最重要的规则】数学公式、变量、符号、表达式必须原样保留，一字不改：如 p → q、¬p ∨ q、∀xP(x)、f(a+b)、2^n、Z_5、⌈x⌉、∑、∈、⊆ 等，出现什么就原样保留什么。
3. 独立的公式行、真值表、矩阵等（输入中会被 ⟦⟧ 包裹或标注 KEEP）原样输出，不要翻译。
4. 译文必须是地道的中文教材书面语：先读懂整句再翻译，长句按中文习惯拆分，避免逐词直译和欧化句式；技术陈述准确优先，读起来应像中文母语者编写的教材。
5. 术语必须遵循本术语表，没有的术语用国内教材通用译法，前后一致：
{GLOSSARY}
6. 数字、单位、人名：人名按通用译名（如 De Morgan→德摩根，首次可附原文）。
7. 输出协议（严格遵守）：对输入中每个 `### 编号` 块，先原样输出该 `### 编号`，换行后输出译文，再处理下一块；也可以用 `编号|||译文` 单行形式。不要输出输入中的 `[第X页]` 标记，不要添加任何解释或总结。"""

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Block:
    page: int          # 1-based 物理页码
    kind: str          # heading / para / keep / figure
    level: int         # 标题级别 1-4
    text: str
    rect: tuple = ()   # 页面上的 bbox (x0,y0,x1,y1)，overlay 还原排版用
    size: float = 0.0  # 原字号
    bold: bool = False # 原文是否加粗（overlay 保留强调）
    color: int = 0     # 原文字颜色（标题还原书本蓝色用）
    chunk_id: int = -1

@dataclass
class Cfg:
    base_url: str
    api_key: str
    model: str
    price_in: float = 2.0    # ¥/百万输入token，仅用于估算
    price_out: float = 8.0   # ¥/百万输出token

# ---------------------------------------------------------------------------
# PDF 抽取
# ---------------------------------------------------------------------------
MATH_CHARS = set("=≠≤≥±×÷∈∉⊆⊂⊇⊃∪∩∅∀∃¬∧∨→←↔⇔⇒∑∏∫⌊⌋⌈⌉√∞∣∤∥⊥∘·⊕⊗≈≅≃δΔεθλμσΣΩπΠφψΓ")
LIGATURES = {"\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
             "\ufb04": "ffl", "\u2010": "-", "\u00ad": ""}
MARGIN_NOISE = re.compile(r"(?<![\w])(Extra Examples|Biographies|Demo)(?![\w])")
RE_RUNNING_HEAD = re.compile(r"^(\d{1,4}\s+)?\d+\s*/\s*[A-Z].{0,90}$")  # "28 1 / The Foundations…"

def clean_text(t: str) -> str:
    for k, v in LIGATURES.items():
        t = t.replace(k, v)
    return t

RE_STANDALONE_TF = re.compile(r"(?<![A-Za-z])[TF](?![A-Za-z])")
RE_MATHSYM = re.compile(r"[0-9=≠≤≥±×÷∈⊆∪∩∀∃¬∧∨→←↔⇔⇒∑∏∫⌊⌋⌈⌉√≡≃≅∘⊥·⊕⊗∣∥]")

def looks_like_formula(text: str) -> bool:
    """公式/真值表/数字表密集、自然语言稀少的块 → 原样保留不翻译。"""
    core = RE_STANDALONE_TF.sub(" ", text)          # 独立 T/F 是真值符号，不算单词
    if re.search(r"[A-Za-z]{4,}", core):            # 含≥4字母英文单词 → 自然语言
        return False
    return bool(RE_MATHSYM.search(core) or RE_STANDALONE_TF.search(text))

def _merge_rects(rects, gap=6.0):
    """把相互重叠或间距<=gap 的矩形合并成簇（按 x0 扫描，接近线性）。"""
    rs = sorted((pymupdf.Rect(r) for r in rects), key=lambda r: (r.x0, r.y0))
    result, active = [], []
    for r in rs:
        still = []
        for c in active:
            (result if c.x1 + gap < r.x0 else still).append(c)
        active = still
        pad = pymupdf.Rect(r.x0 - gap, r.y0 - gap, r.x1 + gap, r.y1 + gap)
        grown = None
        rest = []
        for c in active:
            if grown is None and c.intersects(pad):
                c |= r
                grown = c
            elif grown is not None and grown.intersects(
                    pymupdf.Rect(c.x0 - gap, c.y0 - gap, c.x1 + gap, c.y1 + gap)):
                grown |= c          # 新簇把两个旧簇桥接起来
            else:
                rest.append(c)
        if grown is not None:
            active = rest + [grown]
        else:
            active = rest + [pymupdf.Rect(r)]
    result.extend(active)
    return result

SUP_MAP = {'0': '⁰', '1': '¹', '2': '²', '3': '³', '4': '⁴', '5': '⁵',
           '6': '⁶', '7': '⁷', '8': '⁸', '9': '⁹', 'n': 'ⁿ', 'i': 'ⁱ',
           '+': '⁺', '-': '⁻', '(': '⁽', ')': '⁾'}

def _span_text(s, line_max_sz):
    """span 文本；仅当 span 明显小于行主体（真上标）时转 Unicode 上标。
    有些字体给普通数字也打 superscript 标志，靠字号比值过滤。"""
    t = s["text"]
    if not t.strip() or not (s.get("flags", 0) & 1):
        return t
    if s.get("size", 0) > 0.72 * line_max_sz + 0.05:
        return t
    mapped = "".join(SUP_MAP.get(c, "") for c in t)
    return mapped if mapped else "^{" + t.strip() + "}"

def extract_blocks(doc, only_pages=None, figures_dir=None, sample_pages=40):
    """抽取为结构化块列表：标题层级 + 段落 + 公式块 + 图示 PNG。"""
    sizes = {}
    for i in range(0, doc.page_count, max(1, doc.page_count // sample_pages)):
        for b in doc[i].get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                for s in l.get("spans", []):
                    if len(s["text"].strip()) > 3:
                        sizes[round(s["size"], 1)] = sizes.get(round(s["size"], 1), 0) + len(s["text"])
    body_size = max(sizes, key=sizes.get) if sizes else 10.0

    if figures_dir:
        os.makedirs(figures_dir, exist_ok=True)

    blocks = []
    for pno in range(doc.page_count):
        if only_pages and (pno + 1) not in only_pages:
            continue
        page = doc[pno]
        d = page.get_text("dict")

        # --- 文本块 ---
        tblocks = []   # (bbox, maxsz, raw_lines)
        for b in d["blocks"]:
            lines, maxsz = [], 0.0
            bold_chars = total_chars = 0
            size_cnt = {}
            head_color = 0
            for l in b.get("lines", []):
                spans = l.get("spans", [])
                lmax = max((s["size"] for s in spans), default=body_size)
                txt = "".join(_span_text(s, lmax) for s in spans)
                for s in spans:
                    n = len(s["text"].strip())
                    total_chars += n
                    if n:
                        size_cnt[round(s["size"], 1)] = size_cnt.get(round(s["size"], 1), 0) + n
                    if n and (s.get("flags", 0) & 16):
                        bold_chars += n
                if txt.strip():
                    lines.append(txt.rstrip())
                    maxsz = max(maxsz, lmax)
            if lines:
                all_spans = [s for l2 in b.get("lines", []) for s in l2.get("spans", [])]
                blk_color = Counter(s.get("color", 0) for s in all_spans).most_common(1)[0][0] if all_spans else 0
                dom_sz = max(size_cnt, key=size_cnt.get) if size_cnt else maxsz
                tblocks.append((pymupdf.Rect(b["bbox"]), maxsz, lines, blk_color, dom_sz))

        # --- 图示检测：矢量路径 + 位图 聚类 ---
        elems = []
        for i in page.get_image_info():
            r = pymupdf.Rect(i["bbox"])
            if r.width >= 4 and r.height >= 4:
                elems.append(r)
        for dr in page.get_drawings():
            r = dr["rect"]
            if r.width < 0.4 and r.height < 0.4:
                continue
            elems.append(r)
        figs = []
        for c in (_merge_rects(elems) if elems else []):
            if c.width < 14 or c.height < 14 or c.get_area() < 350:
                continue
            if c.width > page.rect.width * 0.95 and c.height > page.rect.height * 0.9:
                continue  # 整页背景/边框
            centers = [pymupdf.Point((bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2) for bb, _, _, _, _ in tblocks]
            inside = [tb for tb, p in zip(tblocks, centers) if c.contains(p)]
            joined = [clean_text("\n".join(ln)).replace("\n", " ") for _, _, ln, _, _ in inside]
            if any(len(t) >= 25 and re.search(r"[a-z]{4,}", t) for t in joined):
                continue  # 簇内有成句英文 → 是带边框的例题/定义框，不是图
            keep_area = sum(bb.get_area() for bb, _, ln2, _c1, _d1 in inside
                            if looks_like_formula(re.sub(r"\s{2,}", " ",
                                clean_text("\n".join(ln2)).replace("\n", " "))))
            if keep_area > 0.35 * c.get_area():
                continue  # 簇内主要是公式/表格文字 → 表格框，不是图
            figs.append(c)

        # 被图示"吞掉"的文字块（图内标签），不再作为文本输出
        fig_texts = set()
        for c in figs:
            for j, (bb, _, _, _c0, _d0) in enumerate(tblocks):
                if c.contains(pymupdf.Point((bb.x0 + bb.x1) / 2, (bb.y0 + bb.y1) / 2)):
                    fig_texts.add(j)

        # --- 渲染图示 PNG ---
        items = []   # (y, x, type, payload)
        for n, c in enumerate(figs):
            fname = f"p{pno+1}_{n:02d}.png"
            if figures_dir and not os.path.exists(os.path.join(figures_dir, fname)):
                pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3),
                                      clip=c + (-2, -2, 2, 2), alpha=False)
                pix.save(os.path.join(figures_dir, fname))
            items.append((c.y0, c.x0, "figure", (fname, tuple(round(v, 1) for v in c))))

        for j, (bb, maxsz, raw_lines, blk_color, dom_sz) in enumerate(tblocks):
            if j in fig_texts:
                continue
            items.append((bb.y0, bb.x0, "text",
                          (maxsz, raw_lines, tuple(round(v, 1) for v in bb), blk_color, dom_sz)))

        items.sort(key=lambda t: (round(t[0] / 12), t[1]))

        pending_keep = []   # 相邻公式块合并，防错位
        def flush_keep():
            nonlocal pending_keep
            if pending_keep:
                text = "\n".join(pending_keep).strip()
                if text:
                    blocks.append(Block(page=pno + 1, kind="keep", level=0, text=text))
                pending_keep = []

        for _, _, typ, payload in items:
            if typ == "figure":
                flush_keep()
                fname, frect = payload
                blocks.append(Block(page=pno + 1, kind="figure", level=0,
                                    text=f"![图示（原文第{pno+1}页）](figures/{fname})",
                                    rect=frect))
                continue
            maxsz, raw_lines, cur_rect, blk_color, dom_sz = payload
            raw = re.sub(r"(\w)-\n(\w)", r"\1\2", "\n".join(raw_lines))  # 连字符跨行
            joined = MARGIN_NOISE.sub(" ", clean_text(raw).replace("\n", " "))
            joined = re.sub(r"\s{2,}", " ", joined).strip()
            if not joined:
                continue
            if re.fullmatch(r"\d{1,4}", joined) or RE_RUNNING_HEAD.match(joined):
                continue  # 纯页码 / 页眉页脚
            if looks_like_formula(joined):
                keep_lines = [clean_text(l).strip() for l in raw.splitlines()]
                pending_keep.extend([l for l in keep_lines if l])
                continue
            flush_keep()
            is_head = maxsz >= body_size * 1.06 and len(joined) <= 150
            if is_head:
                level = 1 if maxsz >= body_size * 1.30 else \
                    2 if maxsz >= body_size * 1.16 else 3
                blocks.append(Block(page=pno + 1, kind="heading", level=level, text=joined,
                                    rect=cur_rect, size=round(dom_sz, 1),
                                    bold=total_chars > 0 and bold_chars / total_chars > 0.55,
                                    color=blk_color))
            else:
                blocks.append(Block(page=pno + 1, kind="para", level=0, text=joined,
                                    rect=cur_rect, size=round(dom_sz, 1),
                                    bold=total_chars > 0 and bold_chars / total_chars > 0.55))
        flush_keep()
    return blocks

# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------
def make_chunks(blocks, target_chars=2000, max_chars=3600):
    """把待翻译块按页序聚成 chunk（keep 块直接透传，不进 API）。"""
    chunks, cur, cur_len = [], [], 0
    for i, b in enumerate(blocks):
        if b.kind in ("keep", "figure"):
            continue
        cur.append(i); cur_len += len(b.text)
        if cur_len >= target_chars or (len(cur) >= 24):
            chunks.append(cur); cur, cur_len = [], 0
        elif cur_len > max_chars:
            chunks.append(cur); cur, cur_len = [], 0
    if cur:
        chunks.append(cur)
    for cid, idxs in enumerate(chunks):
        for i in idxs:
            blocks[i].chunk_id = cid
    return chunks

def build_user_prompt(blocks, idxs):
    parts = []
    for n, i in enumerate(idxs, 1):
        b = blocks[i]
        prefix = f"[第{b.page}页]\n" if n == 1 or blocks[idxs[n - 2]].page != b.page else ""
        parts.append(f"{prefix}### {n}\n{b.text}")
    return "\n\n".join(parts)

# ---------------------------------------------------------------------------
# 翻译执行
# ---------------------------------------------------------------------------
class Translator:
    def __init__(self, cfg: Cfg, workers: int):
        self.cfg = cfg
        self.client = OpenAI(base_url=cfg.base_url, api_key=cfg.api_key,
                             timeout=240, max_retries=2)
        self.lock = threading.Lock()
        self.tokens_in = 0
        self.tokens_out = 0
        self.workers = workers

    def _call(self, user_prompt):
        resp = self.client.chat.completions.create(
            model=self.cfg.model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": user_prompt}],
            temperature=0.2,
            max_tokens=6000,
        )
        usage = resp.usage
        with self.lock:
            if usage:
                self.tokens_in += usage.prompt_tokens or 0
                self.tokens_out += usage.completion_tokens or 0
        return resp.choices[0].message.content or ""

    def translate_chunk(self, blocks, idxs):
        user_prompt = build_user_prompt(blocks, idxs)
        best, last_err = {}, None
        for attempt in range(4):
            try:
                raw = self._call(user_prompt)
                out = parse_reply(raw, len(idxs))
                if len(out) == len(idxs):
                    return out
                if len(out) > len(best):
                    best = out
                last_err = f"解析到 {len(out)}/{len(idxs)} 块"
            except Exception as e:
                last_err = str(e)[:200]
            time.sleep(2 * (attempt + 1))
        if best:
            print(f"\n[警告] chunk 仅解析出 {len(best)}/{len(idxs)} 块（{last_err}），"
                  f"缺失块保留英文", file=sys.stderr)
            return best
        print(f"\n[警告] chunk 翻译失败({last_err})，相关块将保留英文原文", file=sys.stderr)
        return {}

RE_NUM_ALONE = re.compile(r"^#{0,6}\s*(\d{1,3})\s*$")                 # "### 5" / "5"
RE_NUM_TEXT = re.compile(r"^#{1,6}\s*(\d{1,3})[\.\):：]?\s+(.+)$")    # "### 5 译文…"
RE_PIPE = re.compile(r"^(\d{1,3})\s*\|\|\|\s*(.*)$")
RE_PAGE_TAG = re.compile(r"^\[第\d+页\]\s*$")

def _marker_num(line):
    m = RE_PIPE.match(line)
    if m:
        return int(m.group(1)), m.group(2).strip(), True
    m = RE_NUM_TEXT.match(line)
    if m:
        return int(m.group(1)), m.group(2).strip(), True
    m = RE_NUM_ALONE.match(line)
    if m:
        return int(m.group(1)), "", True
    return None

def parse_reply(raw, n_expected):
    """解析模型回复，支持 `### N\\n译文`、`N|||译文`、`### N 译文` 三种形态。"""
    out, lines, i = {}, raw.splitlines(), 0
    while i < len(lines):
        line = lines[i].strip()
        if RE_PAGE_TAG.match(line):
            i += 1; continue
        mk = _marker_num(line)
        if mk:
            num, first_txt, _ = mk
            parts = [first_txt] if first_txt else []
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if RE_PAGE_TAG.match(nxt) or _marker_num(nxt):
                    break
                parts.append(nxt)
                i += 1
            if 1 <= num <= n_expected and num not in out:
                out[num] = "\n".join(p for p in parts if p).strip()
            continue
        i += 1
    return out

# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def dump_blob(d):
    return json.dumps({str(k): v for k, v in d.items()}, ensure_ascii=False)

def load_blob(text, n_expected):
    """缓存 blob → {块号: 译文}。兼容 JSON 与旧 `N|||译文` 两种格式。"""
    try:
        d = json.loads(text)
        return {int(k): v for k, v in d.items() if 1 <= int(k) <= n_expected and v}
    except Exception:
        return parse_reply(text, n_expected)

def open_cache(path):
    con = sqlite3.connect(path)
    con.execute(CACHE_SCHEMA)
    con.commit()
    return con

def file_key(path):
    st = os.stat(path)
    return hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime:.0f}".encode()).hexdigest()[:16]

def chunk_hash(model, user_prompt):
    return hashlib.sha1(f"{PROMPT_VERSION}|{model}|{user_prompt}".encode()).hexdigest()

# ---------------------------------------------------------------------------
# 输出组装
# ---------------------------------------------------------------------------
HEAD_PREFIX = {1: "# ", 2: "## ", 3: "### ", 4: "#### "}

def assemble(blocks, translations, bilingual):
    lines = []
    for b in blocks:
        t = translations.get(id(b))
        if b.kind == "figure":
            lines += ["", b.text, ""]
        elif b.kind == "heading":
            lines += ["", HEAD_PREFIX.get(b.level, "### ") + (t or b.text), ""]
        elif b.kind == "keep":
            lines += ["```text", b.text, "```", ""]
        else:
            if bilingual:
                lines += ["> " + b.text, "", (t or f"*[未翻译]* {b.text}"), ""]
            else:
                lines += [t or b.text, ""]
    return "\n".join(lines).replace("\n\n\n", "\n\n")

HTML_TMPL = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{title}</title>
<style>
body{{font-family:"Source Han Serif SC","Noto Serif CJK SC",Georgia,serif;max-width:52em;
margin:2em auto;padding:0 1em;line-height:1.75;color:#1a1a1a}}
h1,h2,h3{{color:#0b3d91;line-height:1.3}}
pre{{background:#f5f5f0;padding:.8em;border-radius:6px;overflow-x:auto;
font-family:Consolas,"Cambria Math",serif;font-size:1.05em}}
blockquote{{border-left:3px solid #bbb;margin:0;padding:.1em 1em;color:#555;
font-style:italic;background:#fafafa}}
</style></head><body>{body}</body></html>"""

def md_to_html(md_text):
    """极简 md→html（够用于本书结构：标题/代码块/引用/段落）。"""
    html, in_code = [], False
    for line in md_text.splitlines():
        if line.strip().startswith("```"):
            html.append("</pre>" if in_code else "<pre>"); in_code = not in_code; continue
        if in_code:
            html.append(line.replace("&", "&amp;").replace("<", "&lt;")); continue
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            lv = len(m.group(1)); html.append(f"<h{lv}>{m.group(2)}</h{lv}>"); continue
        m = re.match(r"^!\[([^\]]*)\]\(([^)]+)\)", line.strip())
        if m:
            html.append(f'<p><img src="{m.group(2)}" alt="{m.group(1)}" '
                        f'style="max-width:100%;border:1px solid #ddd"></p>'); continue
        if line.startswith("> "):
            html.append(f"<blockquote>{line[2:]}</blockquote>"); continue
        if line.strip():
            html.append(f"<p>{line}</p>")
    return "\n".join(html)

# ---------------------------------------------------------------------------
# CC-Switch 导入 / 连通性测试
# ---------------------------------------------------------------------------
CCSWITCH_DB = Path(os.environ.get("TEXTBOOK_CCSWITCH_DB", str(Path.home() / ".cc-switch" / "cc-switch.db"))).expanduser()

def import_ccswitch(cfg_path):
    import sqlite3 as sq
    database = Path(CCSWITCH_DB).expanduser().resolve()
    if not database.is_file():
        sys.exit("未找到 CC-Switch 数据库；请设置 TEXTBOOK_CCSWITCH_DB 或手动填写 config.json。")
    # Explicit import only; never create or update the user's credential database.
    con = sq.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT name, settings_config FROM providers").fetchall()
    finally:
        con.close()
    found = {}

    def add(tag, base_url, key, model, price_in, price_out):
        if not key:
            return
        e = found.setdefault(tag, dict(base_url=base_url, api_key=key, model=model,
                                       price_in=price_in, price_out=price_out))

    for name, cfg_json in rows:
        try:
            c = json.loads(cfg_json)
        except Exception:
            continue
        env = c.get("env", {}) or {}
        key = (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("OPENAI_API_KEY")
               or c.get("apiKey") or c.get("api_key")
               or (c.get("auth", {}) or {}).get("OPENAI_API_KEY") or "")
        url = (c.get("baseUrl") or c.get("base_url")
               or env.get("ANTHROPIC_BASE_URL") or env.get("OPENAI_BASE_URL") or "")
        host = re.sub(r"^https?://", "", url).split("/")[0] if url else ""
        models = [m.get("id") or m.get("model", "") for m in c.get("models", [])]
        models = [m for m in models if m]
        catalog = [m.get("model", "") for m in (c.get("modelCatalog", {}) or {}).get("models", [])]
        catalog = [m for m in catalog if m]
        blob = name.lower() + host
        if "deepseek" in blob or "deepseek" in host:
            add("deepseek", "https://api.deepseek.com/v1", key, "deepseek-chat", 2.0, 8.0)
            for m in set(models + catalog):
                if "v4-flash" in m:
                    add("deepseek-flash", "https://api.deepseek.com/v1", key, m, 1.0, 4.0)
                elif "v4-pro" in m:
                    add("deepseek-pro", "https://api.deepseek.com/v1", key, m, 4.0, 16.0)
        elif "bigmodel" in blob or "zhipu" in blob or "glm" in blob:
            m = "glm-4.5-air" if ("glm-4.5-air" in models or not models) else models[0]
            add("glm", "https://open.bigmodel.cn/api/paas/v4", key, m, 2.0, 8.0)
        elif "moonshot" in blob or "kimi" in blob:
            m = models[0] if models else "kimi-k2.5"
            add("kimi", "https://api.moonshot.cn/v1", key, m, 4.0, 16.0)
        elif "openrouter" in blob:
            m = models[0] if models else "deepseek/deepseek-chat"
            add("openrouter", "https://openrouter.ai/api/v1", key, m, 2.0, 8.0)
        elif "mimo" in blob:
            m = models[0] if models else "mimo-v2-pro"
            add("mimo", "https://api.mimo.ai/v1", key, m, 2.0, 8.0)
    if not found:
        sys.exit("未能从 CC-Switch 数据库找到任何 API Key，请手动编辑 config.json")
    default_name = "deepseek" if "deepseek" in found else next(iter(found))
    json.dump({"providers": {k: v for k, v in found.items()},
               "_default": default_name}, open(cfg_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"已导入 {len(found)} 个供应商 → {cfg_path}（默认 {default_name}）")
    for k, v in found.items():
        if k == "_default":
            continue
        print(f"  - {k:15s} {v['model']:28s} 凭据已保存（不显示）")

def test_providers(cfg_path):
    conf = json.load(open(cfg_path, encoding="utf-8"))
    for name, p in conf["providers"].items():
        if name == "_default":
            continue
        try:
            cli = OpenAI(base_url=p["base_url"], api_key=p["api_key"], timeout=30, max_retries=0)
            r = cli.chat.completions.create(model=p["model"],
                messages=[{"role": "user", "content": "回复：OK"}], max_tokens=8)
            print(f"[通过] {name:12s} {p['model']} → {r.choices[0].message.content!r}")
        except Exception as e:
            print(f"[失败] {name:12s} {p['model']} → {str(e)[:120]}")

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="大型英文数学 PDF 翻译工具")
    ap.add_argument("pdf", nargs="?", help="输入 PDF 路径")
    ap.add_argument("--out", default=None, help="输出目录（默认 PDF 同目录/译文_书名）")
    ap.add_argument("--pages", default=None, help="只翻译指定物理页，如 50-60 或 1,3,50-52")
    ap.add_argument("--provider", default=None, help="config.json 中的供应商名（默认取 _default）")
    ap.add_argument("--model", default=None, help="覆盖模型名")
    ap.add_argument("--workers", type=int, default=6, help="并发数（默认 6）")
    ap.add_argument("--target-chars", type=int, default=2000, help="每块目标字符数")
    ap.add_argument("--only-md", action="store_true", help="只输出纯中文 Markdown")
    ap.add_argument("--only-bilingual", action="store_true", help="只输出双语 Markdown")
    ap.add_argument("--estimate", action="store_true", help="只估算 token 与费用，不翻译")
    ap.add_argument("--dump", action="store_true", help="只抽取并导出 manifest.json（子代理流水线用）")
    ap.add_argument("--assemble-only", action="store_true", help="不从 API 翻译，仅从缓存汇总输出")
    ap.add_argument("--import-ccswitch", action="store_true", help="从 CC-Switch 导入密钥生成 config.json")
    ap.add_argument("--test", action="store_true", help="测试 config.json 各供应商连通性")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(here, "config.json")

    if args.import_ccswitch:
        return import_ccswitch(cfg_path)
    if args.test:
        return test_providers(cfg_path)
    if not args.pdf:
        ap.print_help(); return

    conf = json.load(open(cfg_path, encoding="utf-8"))
    pname = args.provider or conf.get("_default") or next(
        (k for k in conf["providers"] if k != "_default"), None)
    if pname not in conf["providers"]:
        sys.exit(f"config.json 中不存在供应商 {pname}，可选：{[k for k in conf['providers'] if k != '_default']}")
    p = dict(conf["providers"][pname])
    if args.model:
        p["model"] = args.model
    cfg = Cfg(p["base_url"], p["api_key"], p["model"],
              p.get("price_in", 2.0), p.get("price_out", 8.0))

    pdf_path = os.path.abspath(args.pdf)
    stem = re.sub(r"[\\/:*?\"<>|]", "_", os.path.splitext(os.path.basename(pdf_path))[0])[:60]
    outdir = args.out or os.path.join(os.path.dirname(pdf_path), f"译文_{stem}")
    os.makedirs(outdir, exist_ok=True)

    wanted = None
    if args.pages:
        wanted = set()
        for part in args.pages.split(","):
            if "-" in part:
                a, b = part.split("-"); wanted |= set(range(int(a), int(b) + 1))
            else:
                wanted.add(int(part))

    print(f"打开 PDF：{pdf_path}")
    doc = pymupdf.open(pdf_path)
    print(f"共 {doc.page_count} 页，抽取文本块与图示…")
    blocks = extract_blocks(doc, only_pages=wanted,
                            figures_dir=os.path.join(outdir, "figures"))

    n_keep = sum(1 for b in blocks if b.kind == "keep")
    n_text = len(blocks) - n_keep
    total_chars = sum(len(b.text) for b in blocks if b.kind != "keep")
    print(f"待处理块 {len(blocks)} 个（段落/标题 {n_text}，公式/表格原样保留 {n_keep}），"
          f"正文约 {total_chars/10000:.1f} 万字符")

    chunks = make_chunks(blocks, target_chars=args.target_chars)

    # ---- 估算 ----
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        tokens_in_est = sum(len(enc.encode(b.text)) for b in blocks if b.kind not in ("keep", "figure"))
    except Exception:
        tokens_in_est = int(total_chars / 3.7)
    tokens_out_est = int(tokens_in_est * 1.1)
    cost = tokens_in_est / 1e6 * cfg.price_in + tokens_out_est / 1e6 * cfg.price_out
    print(f"估算：输入≈{tokens_in_est/1e6:.2f}M tok，输出≈{tokens_out_est/1e6:.2f}M tok"
          f"（{cfg.model} 计价约 ¥{cost:.1f}，仅供参考）")
    if args.estimate:
        return

    if args.dump:
        manifest = {
            "pdf": pdf_path, "n_pages": doc.page_count,
            "prompt_version": PROMPT_VERSION,
            "fkey": file_key(pdf_path) + (f"|p{args.pages}" if args.pages else ""),
            "blocks": [{"page": b.page, "kind": b.kind, "level": b.level, "text": b.text,
                        "rect": list(b.rect), "size": b.size, "bold": b.bold,
                        "color": b.color}
                       for b in blocks],
            "chunks": [{"id": i, "idxs": idxs} for i, idxs in enumerate(chunks)],
        }
        mp = os.path.join(outdir, "manifest.json")
        json.dump(manifest, open(mp, "w", encoding="utf-8"), ensure_ascii=False)
        print(f"manifest 已导出：{len(blocks)} 块 / {len(chunks)} chunks → {mp}")
        return

    def cache_lookup(idxs, extra_models=(), cid=None):
        """按 (fkey, chunk_hash) 查缓存；失败时按 chunk_id 抢救旧版译文（块数须一致）。"""
        up = build_user_prompt(blocks, idxs)
        for model_tag in (cfg.model, *extra_models):
            ch = chunk_hash(model_tag, up)
            row = cache.execute(
                "SELECT translation FROM chunks WHERE file_key=? AND chunk_hash=?",
                (fkey, ch)).fetchone()
            if row:
                return load_blob(row[0], len(idxs))
        if cid is not None:
            row = cache.execute(
                "SELECT translation FROM chunks WHERE file_key=? AND chunk_id=? "
                "AND model='subagent'", (fkey, cid)).fetchone()
            if row:
                parsed = load_blob(row[0], len(idxs))
                if len(parsed) == len(idxs):
                    return parsed
        return None

    # ---- 断点续传 ----
    cache = open_cache(os.path.join(outdir, "_cache.db"))
    fkey = file_key(pdf_path) + (f"|p{args.pages}" if args.pages else "")

    if args.assemble_only:
        translations, miss = {}, []
        for cid, idxs in enumerate(chunks):
            parsed = cache_lookup(idxs, extra_models=("subagent",), cid=cid)
            if cid == 24:
                print(f"[DBG] chunk24 parsed={len(parsed) if parsed else None} 类型={type(parsed)}",
                      file=sys.stderr)
                if parsed:
                    t17 = [blocks[i].text[:30] for i in idxs if blocks[i].page == 17]
                    print(f"[DBG] chunk24 页17块源文数={len(t17)} 首块={t17[:3]}", file=sys.stderr)
            if parsed:
                for n, txt in parsed.items():
                    translations[id(blocks[idxs[n - 1]])] = txt
            else:
                miss.append(cid)
        print(f"缓存覆盖 {len(chunks)-len(miss)}/{len(chunks)} 个 chunk"
              + (f"，缺失 {miss[:30]}…" if miss else ""))
        write_outputs(blocks, translations, outdir, stem, args)
        return

    tr = Translator(cfg, workers=args.workers)
    translations = {}   # id(block) -> 译文

    todo = []
    for cid, idxs in enumerate(chunks):
        parsed = cache_lookup(idxs, cid=cid)
        if parsed:
            for n, txt in parsed.items():
                translations[id(blocks[idxs[n - 1]])] = txt
        else:
            todo.append((cid, idxs))
    print(f"chunk 总数 {len(chunks)}，缓存命中 {len(chunks)-len(todo)}，待翻译 {len(todo)}")

    t0 = time.time()
    done_cnt = 0
    failures = []

    def worker(job):
        cid, idxs = job
        return job, tr.translate_chunk(blocks, idxs)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(worker, job): job for job in todo}
        for fut in as_completed(futs):
            (cid, idxs), res = fut.result()
            if res:
                ch = chunk_hash(cfg.model, build_user_prompt(blocks, idxs))
                cache.execute("INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?,?)",
                              (fkey, ch, cid, cfg.model, dump_blob(res), time.time()))
                cache.commit()
                for n, txt in res.items():
                    if n <= len(idxs):
                        translations[id(blocks[idxs[n - 1]])] = txt
            else:
                failures.append(cid)
            done_cnt += 1
            el = time.time() - t0
            eta = el / done_cnt * (len(todo) - done_cnt) if done_cnt else 0
            print(f"\r[{done_cnt}/{len(todo)}] 已用 {el/60:.1f} 分钟，"
                  f"剩余约 {eta/60:.1f} 分钟，tokens(in/out)：{tr.tokens_in}/{tr.tokens_out}   ",
                  end="", flush=True)
    print()
    if failures:
        print(f"[提示] {len(failures)} 个 chunk 彻底失败（编号 {failures[:20]}…），"
              f"对应段落保留英文；重新运行本命令即可只重试失败部分。", file=sys.stderr)

    write_outputs(blocks, translations, outdir, stem, args)

def write_outputs(blocks, translations, outdir, stem, args):
    """按参数写出 双语/中文/HTML/原版排版PDF 四种产物。"""
    if not args.only_md:
        md_bi = assemble(blocks, translations, bilingual=True)
        p_bi = os.path.join(outdir, f"{stem}_双语.md")
        open(p_bi, "w", encoding="utf-8").write(f"# {stem}（双语对照）\n\n" + md_bi)
        print("已输出：", p_bi)
    if not args.only_bilingual:
        md_zh = assemble(blocks, translations, bilingual=False)
        p_zh = os.path.join(outdir, f"{stem}_中文.md")
        open(p_zh, "w", encoding="utf-8").write(f"# {stem}（中文翻译）\n\n" + md_zh)
        print("已输出：", p_zh)
        write_chapter_html(blocks, translations, outdir, stem)
        build_overlay(blocks, translations, outdir, stem)

def write_chapter_html(blocks, translations, outdir, stem):
    """按章拆分 HTML（防止单文件+全部图片把浏览器卡死），加索引页。"""
    htmldir = os.path.join(outdir, "html")
    os.makedirs(htmldir, exist_ok=True)
    chapters = []   # (标题, [blocks])
    cur = None
    for b in blocks:
        if b.kind == "heading" and b.level == 1:
            cur = (b.text, [])
            chapters.append(cur)
            continue
        if cur is None:
            cur = ("前言", [])
            chapters.append(cur)
        cur[1].append(b)
    if len(chapters) == 1:
        chapters[0] = (stem, chapters[0][1])
    index = [HTML_TMPL.format(title=stem + " 目录",
                              body=f"<h1>{stem}</h1><ul>" + "".join(
                                  f'<li><a href="ch{i:03d}.html">{t}</a></li>'
                                  for i, (t, _) in enumerate(chapters)) + "</ul>")]
    open(os.path.join(htmldir, "index.html"), "w", encoding="utf-8").write(index[0])
    nav_prev = None
    for i, (title, cblocks) in enumerate(chapters):
        lines = [f'<p><a href="index.html">◀ 目录</a>　'
                 f'<a href="ch{i-1:03d}.html">上一章</a>　'
                 f'<a href="ch{i+1:03d}.html">下一章</a></p>']
        body = md_to_html(assemble(cblocks, translations, bilingual=False))
        open(os.path.join(htmldir, f"ch{i:03d}.html"), "w", encoding="utf-8").write(
            HTML_TMPL.format(title=f"{stem} · {title[:40]}", body="\n".join(lines) + body))
    print(f"已输出：{htmldir}（index.html + {len(chapters)} 个分章文件）")

_FONT_DIR = r"C:\Windows\Fonts"

def _load_fonts():
    import os as _os
    fonts = {}
    for key, fname in (("zh", "simsun.ttc"), ("zhb", "simhei.ttf"), ("sym", "seguisym.ttf"),
                       ("lat", "times.ttf"), ("lati", "timesi.ttf")):
        p = _os.path.join(_FONT_DIR, fname)
        if _os.path.exists(p):
            try:
                fonts[key] = pymupdf.Font(fontfile=p)
            except Exception:
                pass
    return fonts

_FONTS = None

def _pick_font(ch, bold=False):
    """逐字符选字体：中文用宋体；拉丁字母/数字用 Times 衬线（单字母斜体，
    贴近原书 STIX 数学观感）；宋体缺的字形用 Segoe UI Symbol 补。"""
    global _FONTS
    if _FONTS is None:
        _FONTS = _load_fonts()
    f_main = _FONTS.get("zhb" if bold else "zh")
    f_sym = _FONTS.get("sym")
    cp = ord(ch)
    if ("0" <= ch <= "9") or ("A" <= ch <= "Z") or ("a" <= ch <= "z"):
        f = _FONTS.get("lati" if (len(ch) == 1 and ch.isalpha()) else "lat")
        if f and f.has_glyph(cp):
            return f
    if f_main and f_main.has_glyph(cp):
        return f_main
    if f_sym and f_sym.has_glyph(cp):
        return f_sym
    return f_main or (f_sym or None)

NO_LINE_START = set("，。、；：？！）】》〉」』”’…%·″℃1234567890%.,;:?!)]}")
NO_LINE_END = set("（《〈「『“‘")

def _is_cjk(ch):
    return ord(ch) > 0x2E80

def _layout_rich(rect, text, fontsize, bold=False):
    """贪心换行 + 逐字符字体选择 + 禁则处理 + 中西文间距。
    返回 [(x, y_baseline, [(ch, font, dx)]…行列表)] 或 None（高度放不下）。"""
    f_main = _FONTS["zhb" if bold else "zh"] if _FONTS else None
    gap = fontsize * 0.11          # 中西文之间的间距（盘古之白）
    max_w = rect.width - 2
    tokens = [m.group(0) for m in re.finditer(r"[A-Za-z0-9']+|\n|.", text)]

    def tok_width(tok, prev_ch):
        w, p = 0.0, prev_ch
        for c in tok:
            cw = _pick_font(c, bold).text_length(c, fontsize)
            if p and ((_is_cjk(p)) != (_is_cjk(c))) and (p != " " and c != " "):
                cw += gap
            w += cw
            p = c
        return w

    lines, cur, cur_w = [], [], 0.0
    prev = ""
    for tok in tokens:
        if tok == "\n":
            if cur:
                lines.append(cur)
            cur, cur_w, prev = [], 0.0, ""
            continue
        tw = tok_width(tok, prev)
        if tok != " " and cur and cur_w + tw > max_w:
            lines.append(cur)
            cur, cur_w = [], 0.0
            if tok == " ":
                prev = " "; continue
        elif tok == " " and not cur:
            prev = " "; continue
        cur.append((tok, tw))
        cur_w += tw
        prev = tok[-1]
    if cur:
        lines.append(cur)

    # 禁则处理：行首禁排标点挪回上一行；行末禁排开引号/开括号挪到下一行
    for _ in range(3):
        changed = False
        for i in range(len(lines) - 1):
            while lines[i + 1] and lines[i + 1][0][0][0] in NO_LINE_START:
                tok, w = lines[i + 1].pop(0)
                lines[i].append((tok, w))
                changed = True
                if not lines[i + 1]:
                    break
            while lines[i] and lines[i][-1][0][0] in NO_LINE_END and lines[i + 1] is not None:
                tok, w = lines[i].pop()
                lines[i + 1].insert(0, (tok, w))
                changed = True
                break
        if not changed:
            break
    lines = [ln for ln in lines if ln]

    line_h = fontsize * 1.24
    # 底部校验：末行基线 + 下伸部分不得越过矩形底部（防止与下一块字形重叠）
    if len(lines) * line_h > rect.height + 0.5 or             (len(lines) - 1) * line_h + fontsize * 1.12 > rect.height + 1.0:
        return None
    out = []
    f0 = f_main or _pick_font("中")
    y = rect.y0 + fontsize * 0.95        # CJK 常规基线位置（顶部留出上伸部）
    for ln in lines:
        x = rect.x0 + 1
        runs = []
        prev_c = ""
        for tok, _w in ln:
            single_var = len(tok) == 1 and tok.isascii() and tok.isalpha()
            for c in tok:
                f = (_FONTS.get("lati") or _pick_font(c, bold)) if single_var else _pick_font(c, bold)
                cw = f.text_length(c, fontsize)
                if prev_c and ((_is_cjk(prev_c)) != (_is_cjk(c))) and                         prev_c != " " and c != " ":
                    x += gap
                runs.append((c, f, cw))
                x += cw
                prev_c = c
        out.append((rect.x0 + 1, y, runs))
        y += line_h
    return out

def build_overlay(blocks, translations, outdir, stem):
    """原版排版 overlay：只抹掉已译段落的英文、原位写入中文。
    公式/图示/表格、未译部分保持原样。含段落聚类、习题硬换行、目录对齐。"""
    global _FONTS
    if _FONTS is None:
        _FONTS = _load_fonts()
    if not _FONTS:
        print("[overlay] 找不到 Windows 字体，跳过", file=sys.stderr)
        return
    pdf_path = None
    try:
        pdf_path = json.load(open(os.path.join(outdir, "manifest.json"),
                                  encoding="utf-8"))["pdf"]
    except Exception:
        print("[overlay] 找不到 manifest.json，跳过", file=sys.stderr)
        return
    by_page = {}
    for b in blocks:
        if b.kind in ("para", "heading") and id(b) in translations and b.rect:
            by_page.setdefault(b.page, []).append(b)
    if not by_page:
        print("[overlay] 暂无已翻译段落，跳过", file=sys.stderr)
        return
    doc = pymupdf.open(pdf_path)
    n_ins = n_skip = 0

    LIST_RE = re.compile(r"^([0-9]+[.\)]\s*|[a-zA-Z]\)\s*|∗|习题\s*\d|例\s*\d|定义\s*\d|注：|解：|表\s*\d|图\s*\d|章末材料|Key Terms|Review|Writing|EXAMPLE|Example\s*\d|Definition|Solution|Remark|Proof|Table\s*\d|Figure\s*\d)")
    HEAD_RE = re.compile(r"^\d{1,2}\.\d+\s+\S.{2,60}\s\d{1,3}$")
    DOTS_RE = re.compile(r"(?:\.[\s.]*){3,}")
    ITEM_SPLIT = re.compile(r"\s+(?=\d{1,3}\.\s)")
    ITEM_SPLIT2 = re.compile(r"(?<=[^\sd\n])(?=\d{1,3}\.\s)")

    def _crosses_table(rect, strokes):
        n_v = n_h = 0
        for r, vertical in strokes:
            if vertical:
                if rect.x0 + 2 < r.x0 < rect.x1 - 2 and \
                        min(rect.y1, r.y1) - max(rect.y0, r.y0) > 3:
                    n_v += 1
            else:
                if rect.y0 + 2 < r.y0 < rect.y1 - 2 and \
                        min(rect.x1, r.x1) - max(rect.x0, r.x0) > 3:
                    n_h += 1
        return n_v >= 1 or n_h >= 2

    for pno, blist0 in sorted(by_page.items()):
        page = doc[pno - 1]
        # 阅读顺序修复：按 (y0, x0) 重排（原 bucket+x 排序会 interleave 双栏）
        blist = sorted(blist0, key=lambda b: (round(pymupdf.Rect(b.rect).y0),
                                              pymupdf.Rect(b.rect).x0))
        strokes = []
        for dr in page.get_drawings():
            r = dr["rect"]
            if r.width < 1.5 and r.height > 10:
                strokes.append((r, True))
            elif r.height < 1.5 and r.width > 10:
                strokes.append((r, False))
        # keep/figure/未译块矩形（图文环绕/公式区，禁止簇跨越）
        figrects = [pymupdf.Rect(b2.rect) for b2 in blocks
                    if b2.page == pno and b2.rect and b2.kind in ("keep", "figure")]
        cluster_ids = {id(b) for b in blist}

        def _merge_ok(prev, b):
            if prev.kind != "para" or b.kind != "para":
                return False
            for t in (prev.text.strip(), b.text.strip()):
                if HEAD_RE.match(t) or DOTS_RE.search(t):
                    return False
            if LIST_RE.match(b.text.strip()):
                return False
            pr1, br1 = pymupdf.Rect(prev.rect), pymupdf.Rect(b.rect)
            # 中间夹块守卫
            for b2 in blist:
                if b2 is prev or b2 is b or b2.kind not in ("para", "heading"):
                    continue
                if not b2.rect:
                    continue
                r2 = pymupdf.Rect(b2.rect)
                if r2.y0 > pr1.y1 - 1 and r2.y1 < br1.y0 + 1 and \
                        min(pr1.x1, r2.x1) - max(pr1.x0, r2.x0) > 10:
                    return False
            # 两块之间隔着横向分隔线 → 不合并
            for r, vertical in strokes:
                if not vertical and pr1.y1 - 1 < r.y0 < br1.y0 + 1 and \
                        min(pr1.x1, r.x1) - max(pr1.x0, r.x0) > 30:
                    return False
            union = pr1 | br1
            for fr in figrects:
                if union.intersects(fr):
                    return False
            gap = br1.y0 - pr1.y1
            if not (-2 <= gap <= 2.6 * (prev.size or 10.5)):
                return False
            ov = min(pr1.x1, br1.x1) - max(pr1.x0, br1.x0)
            return ov > 0.5 * min(pr1.width, br1.width)

        clusters = []
        for b in blist:
            crossed = _crosses_table(pymupdf.Rect(b.rect), strokes)
            if clusters and not crossed and _merge_ok(clusters[-1][-1], b):
                clusters[-1].append(b)
            else:
                clusters.append([b])
        cluster_ids = set()
        for members in clusters:
            cluster_ids.update(id(mb) for mb in members)
        # 封顶：其他簇顶 + 非簇内块（keep/figure/未译）顶
        tops = sorted([pymupdf.Rect(b2.rect).y0 for b2 in blocks
                       if b2.page == pno and b2.rect and id(b2) not in cluster_ids]
                      + [pymupdf.Rect(ms[0].rect).y0 for ms in clusters if ms])

        writers = {0: pymupdf.TextWriter(page.rect)}
        todo = []
        occupied = []
        fallback = []   # 本页已绘制簇的矩形，后续簇与之相交则跳过（防叠印）
        for members in clusters:
            b = members[0]
            rects = [pymupdf.Rect(mb.rect) for mb in members]
            rect = rects[0]
            for r2 in rects[1:]:
                rect |= r2
            if rect.is_empty or rect.width < 8 or rect.height < 5:
                n_skip += 1
                continue
            if _crosses_table(rect, strokes) and len(members) == 1:
                n_skip += 1
                fallback.append((members, translations.get(id(members[0]), "")))
                continue
            zh = translations.get(id(members[0]), "")
            for mb in members[1:]:
                part = translations.get(id(mb), "")
                if not part:
                    continue
                if zh and part and zh[-1].isascii() and zh[-1].isalnum() \
                        and part[0].isascii() and part[0].isalnum():
                    zh += " "
                zh += part
            if not zh:
                n_skip += 1
                continue
            if '"' in zh:
                out_c, open_q = [], True
                for c2 in zh:
                    if c2 == '"':
                        out_c.append("\u201c" if open_q else "\u201d")
                        open_q = not open_q
                    else:
                        out_c.append(c2)
                zh = "".join(out_c)
            # 习题列表按题号硬换行
            if len(re.findall(r"\d{1,3}\.\s", zh)) >= 2:
                zh = re.sub(r"\s+(?=\d{1,3}\.\s)", "\n", zh)
                zh = re.sub(r"(?<=[^\sd\n])(?=\d{1,3}\.\s)", "\n", zh)
            base_fs = b.size or 10.5
            bold = b.bold
            cap = next((t for t in tops if t > rect.y0 + 2), page.rect.y1 - 18)
            first_h = rects[0].height
            max_h = max(cap - rect.y0 - 1, first_h)

            # 目录条目
            toc = None
            if len(members) == 1 and rect.width > 150 and b.kind == "para" \
                    and DOTS_RE.search(b.text):
                segs = [s.strip() for s in re.split(r"(?:[\s.·]\s*){3,}", zh) if s.strip()]
                if len(segs) >= 2:
                    entries, tp_ = [], segs[0]
                    ok = True
                    for seg in segs[1:]:
                        ms2 = re.match(r"^(\d{1,4})\s+(.+)$", seg)
                        if ms2:
                            entries.append((tp_, ms2.group(1)))
                            tp_ = ms2.group(2)
                        elif seg.isdigit():
                            entries.append((tp_, seg))
                            tp_ = ""
                        else:
                            ok = False
                            break
                    if ok and entries and tp_ == "":
                        toc = entries
            if toc:
                laid = None
                ne = max(len(toc), 1)
                for fs in (base_fs, base_fs * 0.93, base_fs * 0.85, base_fs * 0.75):
                    line = rect.x1 - rect.x0 - 18
                    line_h = fs * 1.28
                    if ne * line_h > max_h + 0.5:
                        continue
                    fdot = _FONTS.get("lat") or _pick_font(".")
                    dot_w = fdot.text_length(".", fs)
                    rows, y, good = [], rect.y0 + fs * 0.95, True
                    for title_z, num in toc:
                        trr = _layout_rich(pymupdf.Rect(rect.x0 + 7, y, rect.x1 - 7,
                                                        y + line_h), title_z, fs, bold=bold)
                        nr = _layout_rich(pymupdf.Rect(0, 0, line, line_h), num, fs)
                        if not trr or not nr:
                            good = False
                            break
                        used = sum(w for _, _, w in trr[0][2])
                        n_w = sum(w for _, _, w in nr[0][2])
                        nd = int((line - used - n_w - 8) / dot_w)
                        if nd < 0:
                            good = False
                            break
                        rows.append((rect.x0 + 8, y,
                                     trr[0][2] + [(".", fdot, dot_w)] * nd + nr[0][2]))
                        y += line_h
                    if good and rows:
                        laid = rows
                        break
                if laid:
                    page.add_redact_annot(rect)
                    todo.append((b, laid, fs))
                    n_ins += 1
                    continue

            # 栏内右边界（防串栏）
            member_ids = {id(mb) for mb in members}
            band = [pymupdf.Rect(b2.rect) for b2 in blocks
                    if b2.page == pno and b2.rect and id(b2) not in member_ids
                    and not (pymupdf.Rect(b2.rect).y1 < rect.y0 + 2 or
                             pymupdf.Rect(b2.rect).y0 > rect.y1 - 2)]
            right_limits = [r.x0 - 4 for r in band if r.x0 > rect.x1 - 2]
            page_x1 = max((pymupdf.Rect(b2.rect).x1 for b2 in blocks
                           if b2.page == pno and b2.rect), default=rect.x1)
            x1_max = min(right_limits) if right_limits else max(page_x1, rect.x1)
            x1_max = min(x1_max, rect.x0 + 560)
            wrect = pymupdf.Rect(rect.x0, rect.y0, max(x1_max, rect.x1), rect.y1)
            laid = None
            for fs, hmul, rr in ((base_fs, 1.0, rect), (base_fs, 1.3, rect),
                                 (base_fs, 1.0, wrect), (base_fs, 1.6, wrect),
                                 (base_fs, 2.4, wrect),
                                 (base_fs * 0.93, 2.4, wrect),
                                 (base_fs * 0.85, 3.0, wrect),
                                 (max(base_fs * 0.72, 6.0), 3.4, wrect),
                                 (max(base_fs * 0.60, 5.0), 3.6, wrect)):
                r = pymupdf.Rect(rr.x0 + 7, rect.y0, rr.x1 - 7,
                                 min(rect.y0 + rect.height * hmul,
                                     rect.y0 + max_h,
                                     rect.y1 + 8))
                laid = _layout_rich(r, zh, fs, bold=bold)
                if laid:
                    break
            if not laid:
                n_skip += 1
                fallback.append((members, zh))
                continue  # 放不下：先记入兜底队列
            # 与已绘制区域碰撞 → 整簇跳过（保留英文，绝不叠印）
            clash = False
            for oc in occupied:
                if rect.intersects(oc):
                    inter = rect & oc
                    if not inter.is_empty and inter.get_area() > 0.25 * rect.get_area():
                        clash = True
                        break
            if clash:
                n_skip += 1
                fallback.append((members, zh))
                continue
            occupied.append(pymupdf.Rect(rect))
            for r2 in rects:
                page.add_redact_annot(r2)
            # 微型公式碎片（x⁶、√ 等上标残片）落在簇排版区内 → 一并抹除，
            # 其内容已包含于译文中，避免小字符与译文叠印
            laid_bottom = max(y for _, y, _ in laid) + fs * 0.4
            laid_top = min(y for _, y, _ in laid) - fs * 0.3
            for kb in blocks:
                if kb.page != pno or kb.kind != "keep" or not kb.rect:
                    continue
                kr = pymupdf.Rect(kb.rect)
                if kr.height > 16 or kr.width > 90:
                    continue
                if kr.y1 < laid_top or kr.y0 > laid_bottom:
                    continue
                if not (rect.x0 - 14 <= kr.x0 and kr.x1 <= rect.x1 + 14):
                    continue
                if re.search(r"[⺀-鿿]", kb.text):
                    continue  # 含中文的碎片不抹
                page.add_redact_annot(kr)
            todo.append((b, laid, fs))
        # ---- 兜底通道：主通道跳过的簇逐成员降级重排，消灭英文残留 ----
        for members, zh_fb in fallback:
            if not zh_fb:
                continue
            done_any = False
            for mb in members:
                zm = translations.get(id(mb))
                if not zm:
                    continue
                if '"' in zm:
                    oc, oq = [], True
                    for c2 in zm:
                        if c2 == '"':
                            oc.append("\u201c" if oq else "\u201d")
                            oq = not oq
                        else:
                            oc.append(c2)
                    zm = "".join(oc)
                if len(re.findall(r"\d{1,3}\.\s", zm)) >= 2:
                    zm = re.sub(r"\s+(?=\d{1,3}\.\s)", "\n", zm)
                mr = pymupdf.Rect(mb.rect)
                if mr.is_empty or mr.width < 8:
                    continue
                mfs = mb.size or 9.0
                ml = None
                for _ in range(40):
                    ml = _layout_rich(mr, zm, mfs, bold=mb.bold)
                    if ml:
                        break
                    mfs = max(mfs - 0.4, 4.0)
                if not ml:
                    mfs = 4.0
                    ml = _layout_rich(pymupdf.Rect(mr.x0, mr.y0, mr.x1, mr.y0 + 400),
                                      zm, mfs, bold=mb.bold)
                if not ml:
                    continue
                page.add_redact_annot(mr)
                todo.append((mb, ml, mfs))
                done_any = True
            if done_any:
                n_ins += len(members)
        # 红抹统一执行：必须在兜底通道的红框全部加入之后、中文写入之前，
        # 否则兜底的红框不会生效（英文残留 + 叠印 + 红框显示）
        try:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED)
        except TypeError:
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
        for b, laid, fs in todo:
            color = b.color if b.kind == "heading" else 0
            if color not in writers:
                writers[color] = pymupdf.TextWriter(page.rect)
            tw = writers[color]
            for x, y, runs in laid:
                for c, f, w in runs:
                    tw.append(pymupdf.Point(x, y), c, font=f, fontsize=fs)
                    x += w
            n_ins += 1
        for color, tw in writers.items():
            if color:
                r = ((color >> 16) & 255) / 255 * 0.72
                g = ((color >> 8) & 255) / 255 * 0.72
                bch = (color & 255) / 255 * 0.72
                tw.write_text(page, color=(r, g, bch))
            else:
                tw.write_text(page)
    out = os.path.join(outdir, f"{stem}_中文_原版排版.pdf")
    try:
        doc.save(out, garbage=3, deflate=True)
    except Exception:
        out = os.path.join(outdir, f"{stem}_中文_原版排版_new.pdf")
        doc.save(out, garbage=3, deflate=True)
    print(f"已输出：{out}（{n_ins} 段中文已替换原位，{n_skip} 段跳过；"
          f"公式/图示/未译部分保持原版原样）")

if __name__ == "__main__":
    main()
