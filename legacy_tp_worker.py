# -*- coding: utf-8 -*-
"""
子代理翻译流水线的工作脚本（由编排者/子代理调用，不直接调 API）。

命令：
  python tp_worker.py --book <输出目录> show <chunk_id>          打印该 chunk 的编号英文块
  python tp_worker.py --book <输出目录> save <chunk_id> <文件>    解析译文文件并入缓存，报告缺失
  python tp_worker.py --book <输出目录> status                    总体完成度
  python tp_worker.py --book <输出目录> todo [起] [止]             列出未完成的 chunk id
"""
import argparse
import json
import os
import sys
import time

if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import importlib.util
_spec = importlib.util.spec_from_file_location("tp", os.path.join(HERE, "legacy_translate_pdf.py"))
tp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tp)

SUBAGENT_MODEL = "subagent"

def load_book(bookdir):
    m = json.load(open(os.path.join(bookdir, "manifest.json"), encoding="utf-8"))
    blocks = [tp.Block(**b) for b in m["blocks"]]
    cache = tp.open_cache(os.path.join(bookdir, "_cache.db"))
    return m, blocks, cache

def chunk_coverage(cache, fkey, blocks, idxs, prompt_version, cid=None):
    """返回 (parsed or None, chunk_hash)。支持按 chunk_id 抢救旧版译文。"""
    up = tp.build_user_prompt(blocks, idxs)
    ch = tp.chunk_hash(SUBAGENT_MODEL, up)
    row = cache.execute("SELECT translation FROM chunks WHERE file_key=? AND chunk_hash=?",
                        (fkey, ch)).fetchone()
    parsed = tp.load_blob(row[0], len(idxs)) if row else None
    if parsed is None and cid is not None:
        row = cache.execute(
            "SELECT translation FROM chunks WHERE file_key=? AND chunk_id=? "
            "AND model='subagent'", (fkey, cid)).fetchone()
        if row:
            p2 = tp.load_blob(row[0], len(idxs))
            if len(p2) == len(idxs):
                parsed = p2
    return parsed, ch

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, help="译文输出目录（含 manifest.json）")
    ap.add_argument("cmd", choices=["show", "save", "status", "todo"])
    ap.add_argument("a1", nargs="?", default=None)
    ap.add_argument("a2", nargs="?", default=None)
    args = ap.parse_args()

    m, blocks, cache = load_book(args.book)
    fkey = m["fkey"]
    chunks = m["chunks"]

    if args.cmd == "show":
        cid = int(args.a1)
        idxs = chunks[cid]["idxs"]
        print(tp.build_user_prompt(blocks, idxs))
        return

    if args.cmd == "save":
        cid = int(args.a1)
        reply_file = args.a2
        idxs = chunks[cid]["idxs"]
        n = len(idxs)
        raw = open(reply_file, encoding="utf-8-sig", errors="replace").read()
        parsed = tp.parse_reply(raw, n)
        # 与已有部分合并（已有块保留，新块补入）
        existing, ch = chunk_coverage(cache, fkey, blocks, idxs, m["prompt_version"])
        if existing:
            for k, v in existing.items():
                parsed.setdefault(k, v)
        if not parsed:
            print(f"chunk {cid}: 解析到 0/{n} 块！请检查文件格式（每个块：### N 换行 译文）")
            sys.exit(2)
        cache.execute("INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?,?)",
                      (fkey, ch, cid, SUBAGENT_MODEL, tp.dump_blob(parsed), time.time()))
        cache.commit()
        missing = [i for i in range(1, n + 1) if i not in parsed]
        print(f"chunk {cid}: 已保存 {len(parsed)}/{n} 块"
              + (f"；missing={missing}" if missing else "；完整 ✓"))
        return

    if args.cmd == "status":
        done = partial = 0
        incomplete = []
        for c in chunks:
            idxs = c["idxs"]
            parsed, _ = chunk_coverage(cache, fkey, blocks, idxs, m["prompt_version"], cid=c["id"])
            if parsed is None:
                incomplete.append(c["id"])
            elif len(parsed) < len(idxs):
                partial += 1
                incomplete.append(c["id"])
            else:
                done += 1
        total = len(chunks)
        print(f"chunk 总数 {total} | 完整 {done} | 部分完成 {partial} | 未动 {total-done-partial}")
        print(f"完成度 {done*100//max(total,1)}%")
        if incomplete:
            print(f"未完成 chunk id（前80个）：{incomplete[:80]}")
        return

    if args.cmd == "todo":
        lo = int(args.a1) if args.a1 else 0
        hi = int(args.a2) if args.a2 else len(chunks)
        out = []
        for c in chunks:
            if not (lo <= c["id"] < hi):
                continue
            parsed, _ = chunk_coverage(cache, fkey, blocks, c["idxs"], m["prompt_version"], cid=c["id"])
            if parsed is None or len(parsed) < len(c["idxs"]):
                out.append(c["id"])
        print(" ".join(map(str, out)) if out else "（无）")

if __name__ == "__main__":
    main()
