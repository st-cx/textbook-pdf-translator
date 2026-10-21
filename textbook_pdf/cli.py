from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
from pathlib import Path

from .models import Book,TRANSLATABLE,file_sha256,needs_translation,safe_filename,digest,source_contract_issues,protected_asset_descriptor
from .store import Store,RULE_HASH_SCHEMA

def parse_pages(value):
    if not value:return None
    pages=set()
    for item in value.split(','):
        if '-' in item:
            a,b=map(int,item.split('-',1))
            if a<1 or b<a:raise ValueError('页码范围须为正整数且起点不大于终点')
            pages.update(range(a,b+1))
        else:
            n=int(item)
            if n<1:raise ValueError('物理页码从1开始')
            pages.add(n)
    return sorted(pages)

def load_glossary(path=None):
    path=Path(path) if path else Path(__file__).resolve().parent.parent/'glossary.json'
    if path.exists():
        raw=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw,dict):raise ValueError('术语表必须为英文到中文的 JSON 对象')
        return raw
    return {}

def _print(data):
    print(json.dumps(data,ensure_ascii=False,indent=2,default=str),flush=True)

def main(argv=None):
    ap=argparse.ArgumentParser(description='大型教材 PDF 翻译与中文重排：公式保护、统一字号、原书页码标记')
    ap.add_argument('pdf',nargs='?',help='原书 PDF')
    ap.add_argument('--out',help='独立任务目录；默认在原书旁创建“译文_书名_新版”')
    ap.add_argument('--stage',choices=['analyze','prepare','translate','review','layout','qa','all'],default='all')
    ap.add_argument('--pages',help='物理页码，如 24-47 或 79,105,118')
    ap.add_argument('--backend',choices=['auto','native','babeldoc'],default='auto')
    ap.add_argument('--ocr',choices=['auto','off','force'],default='auto',help='分析阶段：自动识别扫描页、禁用OCR或强制从图像识别；不重解析已有翻译/排版任务')
    ap.add_argument('--ocr-dpi',type=int,default=300,help='扫描分析分辨率，150–600，默认300；坐标映射回原PDF')
    ap.add_argument('--scan-corrections',help='绑定原书与扫描图像哈希的人工OCR/区域修正JSON，仅分析阶段使用')
    ap.add_argument('--config',default=str(Path(__file__).resolve().parent.parent/'config.json'))
    ap.add_argument('--provider');ap.add_argument('--review-provider')
    ap.add_argument('--workers',type=int,default=4)
    ap.add_argument('--target-chars',type=int,help='准备任务时的目标原文长度，默认沿用任务目录记录或2400')
    ap.add_argument('--max-tasks',type=int,help='本次最多处理的任务数，便于样章费用测量')
    ap.add_argument('--task-id',action='append',help='只处理指定任务，可重复提供；用于局部恢复')
    ap.add_argument('--glossary');ap.add_argument('--import-legacy',help='旧 manifest.json 与 _cache.db 所在目录')
    ap.add_argument('--no-legacy-import',action='store_true')
    ap.add_argument('--allow-incomplete',action='store_true',help='仅用于显式标注未翻译内容的预览稿')
    ap.add_argument('--assemble-only',action='store_true',help='兼容旧用法：仅排版现有有效译文')
    ap.add_argument('--dump',action='store_true',help='兼容旧用法：只分析并创建任务')
    ap.add_argument('--estimate',action='store_true',help='只分析和估算，不调用翻译 API')
    ap.add_argument('--resume',action='store_true',help='继续任务；默认已自动使用有效缓存')
    ap.add_argument('--retry-failed',action='store_true')
    args=ap.parse_args(argv)
    if args.workers<1 or args.workers>32:ap.error('--workers 必须在1到32之间')
    if not 150<=args.ocr_dpi<=600:ap.error('--ocr-dpi 必须在150到600之间')
    if args.target_chars is not None and args.target_chars<200:ap.error('--target-chars 不得小于200')
    stage='layout' if args.assemble_only else 'prepare' if args.dump else args.stage
    if args.estimate:stage='prepare'
    if not args.pdf and not args.out:ap.error('请指定原书 PDF 或已有任务目录 --out')
    pdf=Path(args.pdf).resolve() if args.pdf else None
    out=Path(args.out).resolve() if args.out else pdf.parent/('译文_'+pdf.stem+'_新版')
    out.mkdir(parents=True,exist_ok=True)
    bookfile=out/'book.json'
    selected=parse_pages(args.pages)
    if stage=='analyze' and bookfile.exists():
        previous_book=Book.load(bookfile)
        if not pdf or not pdf.is_file():ap.error('分析阶段需要存在的原书 PDF')
        if file_sha256(pdf)!=previous_book.source_sha256:
            raise ValueError('此目录属于另一份原书，请换任务目录；原解析和资产保持不变')
        if selected is not None and selected!=previous_book.selected_pages:
            raise ValueError('此目录已记录另一组页码，请换任务目录；原解析和资产保持不变')
        selected=previous_book.selected_pages
    started=time.perf_counter();metrics={}
    from .metrics import RunMetrics
    measurement=RunMetrics(out,metrics,stage)
    if stage in {'analyze','prepare','all'} and (not bookfile.exists() or stage=='analyze'):
        if not pdf or not pdf.is_file():ap.error('分析阶段需要存在的原书 PDF')
        from .extraction import analyze
        t=time.perf_counter();book=analyze(pdf,out,pages=selected,backend=args.backend,ocr=args.ocr,ocr_dpi=args.ocr_dpi,corrections=args.scan_corrections);book.save(bookfile)
        metrics['analyze_seconds']=time.perf_counter()-t
    elif bookfile.exists():
        book=Book.load(bookfile)
        if pdf and file_sha256(pdf)!=book.source_sha256:raise ValueError('原书内容发生变化，请换任务目录重新分析')
        if selected and selected!=book.selected_pages:raise ValueError('此目录已记录另一组页码；请使用新 --out 避免串页')
    else:raise ValueError('任务目录没有 book.json，请先 --stage analyze')
    book.issues.extend(source_contract_issues(book))
    from .translation import make_tasks
    with Store(out/'tasks.db') as store:
        previous=store.book
        glossary=load_glossary(args.glossary) if args.glossary else dict(store.glossary) if previous is not None else load_glossary()
        def task_source(value):
            return digest({'elements':[(e.id,e.source_hash,e.group_id,e.chapter,needs_translation(e)) for e in value.elements],
                           'assets':{aid:protected_asset_descriptor(a) for aid,a in sorted(value.assets.items())},
                           'blocked':[i for i in value.issues if i.get('severity')=='error']})
        changed=previous is not None and (task_source(previous)!=task_source(book) or digest(store.glossary)!=digest(glossary))
        target_record=store.db.execute("SELECT value FROM metadata WHERE key='task_target_chars'").fetchone()
        target_chars=args.target_chars or (json.loads(target_record[0]) if target_record else 2400)
        existing=store.db.execute("SELECT count(*) FROM tasks WHERE state!='superseded'").fetchone()[0]
        old_protocol=store.db.execute("SELECT count(*) FROM tasks WHERE state!='superseded' AND (json_extract(payload,'$.protected_assets_schema') IS NOT ? OR json_type(payload,'$.protected_asset_descriptors') IS NOT 'object')",(RULE_HASH_SCHEMA,)).fetchone()[0]
        regenerate=stage in {'analyze','prepare'} or not existing or changed or bool(old_protocol) or (args.target_chars is not None and (not target_record or target_chars!=json.loads(target_record[0])))
        store.register(book,glossary)
        if regenerate:
            tasks=make_tasks(book,glossary,target_chars=target_chars)
            store.add_tasks(tasks)
            with store.db:store.db.execute("INSERT OR REPLACE INTO metadata VALUES('task_target_chars',?)",(json.dumps(target_chars),))
        else:
            tasks=[r[0] for r in store.db.execute("SELECT id FROM tasks WHERE state!='superseded'")]
        if args.retry_failed:store.retry_failed()
        legacy=Path(args.import_legacy) if args.import_legacy else Path(book.source_path).parent/('译文_'+Path(book.source_path).stem)
        if not args.no_legacy_import and legacy.exists() and stage in {'prepare','all'}:
            migration_input=digest({'source':book.source_sha256,
                'elements':[(e.id,e.source_hash,e.page,e.bbox) for e in book.elements],
                'glossary':glossary,
                'legacy_files':{name:file_sha256(legacy/name) for name in ['manifest.json','_cache.db'] if (legacy/name).is_file()}})
            migration_file=out/'legacy_migration.json'
            previous_migration=json.loads(migration_file.read_text(encoding='utf-8')) if migration_file.exists() else None
            if not previous_migration or previous_migration.get('input_signature')!=migration_input:
                from .migration import import_legacy
                t=time.perf_counter();migration=import_legacy(book,store,legacy)
                migration['input_signature']=migration_input
                if previous_migration:
                    history=out/'migrations';history.mkdir(exist_ok=True)
                    (history/(digest(previous_migration)+'.json')).write_text(json.dumps(previous_migration,ensure_ascii=False,indent=2),encoding='utf-8')
                migration_file.write_text(json.dumps(migration,ensure_ascii=False,indent=2),encoding='utf-8')
                metrics['legacy_migration_seconds']=time.perf_counter()-t
        block_all=stage=='all' and any(i.get('severity')=='error' for i in book.issues)
        if block_all:metrics['translation_skipped_reason']='原书解析存在阻断问题；默认全流程先生成报告，不调用付费翻译。'
        if args.estimate:
            chars=sum(len(e.text) for e in book.elements if needs_translation(e))
            _print({'source_pages':len(book.selected_pages),'text_characters':chars,'tasks':len(tasks),'accepted_elements':len(store.translations()),'estimate_input_tokens_without_repeated_context':round(chars/3.4),'note':'输入还包括重复术语、规则和上下文；实际时间和费用以样章日志为准，不调用API。'})
        elif stage in {'translate','all'} and not block_all:
            from .translation import run_translation
            t=time.perf_counter();result=run_translation(book,store,args.config,provider=args.provider,workers=args.workers,review_provider=args.review_provider,max_tasks=args.max_tasks,task_ids=args.task_id)
            metrics['translation_seconds']=time.perf_counter()-t
            (out/'translation_run.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        if stage=='review' or (stage=='all' and args.review_provider and not block_all):
            from .translation import run_review
            t=time.perf_counter();result=run_review(book,store,args.config,provider=args.review_provider or args.provider,max_elements=args.max_tasks,workers=args.workers)
            metrics['review_seconds']=time.perf_counter()-t
            (out/'review_run.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        store.reconcile_diagnostics()
        pdfout=out/(safe_filename(book.title)+'_中文阅读版.pdf')
        layout_failure=None
        diagnostic_errors=sum(i.get('severity')=='error' for i in book.issues) if stage in {'analyze','prepare'} else 0
        if stage in {'layout','all'}:
            if any('scan_evidence' in p for p in book.pages):
                from .scan_layout import compose
            else:
                from .layout import compose
            t=time.perf_counter()
            try:
                result=compose(book,store.translations(),out,output_path=pdfout,allow_incomplete=args.allow_incomplete)
                if result.get('pdf_path'):pdfout=Path(result['pdf_path'])
            except RuntimeError as error:
                layout_failure={'code':'composition_blocked','severity':'error','detail':str(error)}
                metrics['layout_error']=str(error)
            metrics['layout_seconds']=time.perf_counter()-t
        if stage in {'qa','all','layout'}:
            from .qa import verify
            from .translation import review_status
            t=time.perf_counter();report=verify(book,store.translations(),out,pdf_path=pdfout if pdfout.exists() and not layout_failure else None,layout_map={} if layout_failure else None,extra_issues=store.issues()+([layout_failure] if layout_failure else []),resolved_issues=store.resolved_issues(),semantic_status=review_status(book,store))
            diagnostic_errors=report['errors']
            # Semantic outcomes belong to task state, not to the PDF parser.
            report['task_issues']=store.issues()
            (out/'quality_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            metrics['qa_seconds']=time.perf_counter()-t
            _print({k:report[k] for k in ['source_pages','output_pages','missing_translations','errors','warnings','acceptance_status','visual_asset_checks']})
        if stage in {'analyze','prepare'}:
            (out/'source_analysis_report.json').write_text(json.dumps({'source_pages':len(book.selected_pages),'elements':len(book.elements),'assets':len(book.assets),'issues':book.issues,'source_errors':diagnostic_errors},ensure_ascii=False,indent=2),encoding='utf-8')
        _print(store.status())
    blocked=bool(layout_failure or diagnostic_errors)
    measurement.finish('stage_blocked' if blocked else 'stage_finished')
    return 2 if blocked else 0

def worker_main(argv=None):
    ap=argparse.ArgumentParser(description='代理任务领取与严格提交')
    ap.add_argument('--book',required=True,help='新版任务目录')
    ap.add_argument('--owner',default='agent')
    ap.add_argument('cmd',choices=['claim','show','save','status','todo','renew','redo','terms'])
    ap.add_argument('a1',nargs='?');ap.add_argument('a2',nargs='?')
    ap.add_argument('--lease-seconds',type=int,default=600)
    a=ap.parse_args(argv)
    with Store(Path(a.book)/'tasks.db') as store:
        if not store.book:raise ValueError('请先使用 --stage prepare 建立任务')
        if a.cmd=='status':_print(store.status())
        elif a.cmd=='redo':
            if not a.a1:raise ValueError('redo 需要元素编号；可在质量报告或任务包中查找')
            _print(store.redo_element(a.a1,a.a2 or '手动要求重译此段'))
        elif a.cmd=='terms':
            _print([i for i in store.issues() if i.get('code')=='glossary_term_proposal'])
        elif a.cmd=='todo':_print([{'id':t['id'],'state':t['state'],'missing':len(t['missing_element_ids'])} for t in store.pending_tasks()])
        elif a.cmd=='claim':_print(store.claim(a.owner,a.lease_seconds,task_id=a.a1))
        elif a.cmd=='renew':store.renew(a.a1,a.owner,a.lease_seconds);_print({'renewed':a.a1})
        elif a.cmd=='show':
            row=store.db.execute('SELECT * FROM tasks WHERE id=?',(a.a1,)).fetchone()
            if not row:raise ValueError('任务编号不存在')
            _print(store._task(row))
        else:
            if not a.a1 or not a.a2:raise ValueError('save 需要 task_id 和 UTF-8 结果 JSON 文件')
            from .translation import parse_response
            row=store.db.execute('SELECT * FROM tasks WHERE id=?',(a.a1,)).fetchone()
            if not row:raise ValueError('任务编号不存在')
            data=parse_response(Path(a.a2).read_text(encoding='utf-8-sig'),store._task(row))
            result=store.submit(a.a1,a.owner,data['source_hash'],data['translations'],provider='agent')
            for english,chinese in data.get('proposed_terms',{}).items():
                store.record_issue({'code':'glossary_term_proposal','severity':'info','detail':'待协调合并的术语提议','task_id':a.a1,'english':english,'chinese':chinese})
            _print(result)
    return 0
