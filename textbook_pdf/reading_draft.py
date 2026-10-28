"""Recoverable reading drafts, separate from formal composition and acceptance.

The source Book and accepted translations never change here. Failed composition
is isolated to a source-page range, then to individual pages. An unrenderable
page is retained at its original size and visibly labelled as untranslated.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import html
import json
from pathlib import Path
import time
import uuid

from .models import TOKEN_RE, digest, file_sha256, needs_translation, safe_filename
from .source_pages import page_label


class ReadingDraftCancelled(RuntimeError):
    """All completed parts are cached; the previous merged PDF is untouched."""


def _atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _subset(book, selected, outdir):
    selected = list(selected)
    pages = set(selected)
    elements = [e for e in book.elements if e.page in pages]
    referenced = {aid for e in elements for aid in set(e.asset_ids) | set(TOKEN_RE.findall(e.text))}
    profile = dict(book.profile)
    # Resolve relative assets against the authoritative job, never this draft's
    # temporary part folder. _partition_book also preserves atomic flow spans.
    profile.setdefault('job_dir', str(Path(outdir).resolve()))
    profile.setdefault('asset_root', str(Path(outdir).resolve()))
    return replace(book, selected_pages=selected, elements=elements,
                   pages=[p for i, p in enumerate(book.pages)
                          if int(p.get('number', p.get('page', i + 1))) in pages],
                   assets={aid: a for aid, a in book.assets.items() if a.page in pages or aid in referenced},
                   profile=profile,
                   issues=[i for i in book.issues if i.get('page') is None or i.get('page') in pages])


def _part_binding(book, translations, outdir):
    from .layout import rendering_binding, _source_roots, _find_asset
    actual = {}
    roots = _source_roots(book, outdir)
    for asset in book.assets.values():
        path = _find_asset(asset, roots)
        key = str(path.resolve())
        if key not in actual:
            actual[key] = file_sha256(path)
        if asset.sha256 and actual[key] != asset.sha256:
            raise ValueError('Protected asset changed; draft cache cannot be reused')
    scan = {}
    if any('scan_evidence' in page for page in book.pages):
        from . import scan_layout
        scan['renderer_sha256'] = file_sha256(Path(scan_layout.__file__))
        scan['source_images'] = {}
        for page in book.pages:
            evidence = page.get('scan_evidence')
            if not evidence:
                continue
            path = (Path(book.profile.get('job_dir', outdir)) / evidence['image']).resolve()
            actual_sha = file_sha256(path)
            if actual_sha != evidence.get('image_file_sha256'):
                raise ValueError('Original scan image changed')
            scan['source_images'][str(page.get('number', page.get('page')))] = actual_sha
    return digest({'schema': 'reading-draft-part-v1',
                   'module': file_sha256(Path(__file__)),
                   'inputs': rendering_binding(book, translations),
                   'issues': book.issues, 'actual_asset_files': actual, 'scan': scan})


def _checked_mapping(path, selected, page_count):
    mapping = json.loads(Path(path).read_text(encoding='utf-8'))
    rows = mapping.get('pages')
    if not isinstance(rows, list) or len(rows) != page_count:
        raise ValueError('Every real output page needs a source mapping')
    indices, mapped = [], set()
    for row in rows:
        page, sources = row.get('page'), row.get('source_pages')
        if (type(page) is not int or not 1 <= page <= page_count
                or not isinstance(sources, list) or not sources
                or any(type(p) is not int or p not in selected for p in sources)):
            raise ValueError('Output page or source mapping is outside its actual range')
        indices.append(page)
        mapped.update(sources)
    if sorted(indices) != list(range(1, page_count + 1)) or mapped != set(selected):
        raise ValueError('Output page mapping is missing, duplicated or stale')
    labels=mapping.get('native_retained_labels',[])
    if not isinstance(labels,list) or any(not isinstance(row,dict)
            or type(row.get('rendered_page')) is not int
            or not 1 <= row['rendered_page'] <= page_count
            or row.get('source_page') not in selected for row in labels):
        raise ValueError('Retained native labels need a real output and source page')
    return mapping


def _original_page(source, number, destination, label):
    """Retain a failed original, with new space for a conspicuous warning."""
    import pymupdf as fitz
    original = source[number - 1]
    with fitz.open() as retained:
        width, height = original.rect.width, original.rect.height
        page = retained.new_page(width=width, height=height + 44)
        page.draw_rect(fitz.Rect(0, 0, width, 44), color=None, fill=(1, .94, .84))
        page.insert_text((12, 17), '待处理：本页保留原文，尚未译成中文', fontname='china-s', fontsize=10,
                         color=(.55, .2, .04))
        page.insert_text((12, 34), label, fontname='china-s', fontsize=9, color=(.3, .3, .3))
        if original.get_contents():
            page.show_pdf_page(fitz.Rect(0, 44, width, height + 44), source, number - 1,
                               keep_proportion=True)
        retained.save(destination, garbage=4, deflate=True)


def _atomic_source_cuts(book):
    """Conservative source connections constrain isolation, never approve content.

    The normal compositor still validates its contracts. Even if an optional
    plan is stale, retrying may not cut an existing equation, question or figure
    away from its actual source members.
    """
    positions={page:i for i,page in enumerate(book.selected_pages)}
    elements=book.element_map()
    blocked=set()
    def connect(pages):
        slots=[positions[p] for p in pages if type(p) is int and p in positions]
        if len(slots)>1:
            blocked.update(range(min(slots)+1,max(slots)+1))
    groups={}
    active_assets=set()
    for element in book.elements:
        if element.metadata.get('render_suppressed'):
            continue
        active_assets.update(element.asset_ids)
        active_assets.update(TOKEN_RE.findall(element.text))
        if element.group_id:
            groups.setdefault(element.group_id,[]).append(element.page)
        anchor=elements.get(element.metadata.get('anchor_id'))
        if anchor is not None:
            connect([element.page,anchor.page])
        proof=element.metadata.get('cross_page_math')
        if isinstance(proof,dict):connect(proof.get('source_pages',[]))
    for pages in groups.values():connect(pages)
    for aid in active_assets:
        asset=book.assets.get(aid)
        if asset is None:continue  # The compositor reports missing source assets.
        proof=asset.metadata.get('cross_page_math')
        if isinstance(proof,dict):connect(proof.get('source_pages',[]))
    plan=book.profile.get('layout_only_exercise_flow_v1')
    if isinstance(plan,dict):
        for unit in plan.get('units',[]):
            ids=[m.get('id') for m in unit.get('members',[]) if isinstance(m,dict)]
            connect([elements[eid].page for eid in ids if eid in elements])
    for key in ('argument_units_v1','semantic_review_units'):
        units=book.profile.get(key)
        if isinstance(units,dict):
            for unit in units.values():
                if not isinstance(unit,dict):continue
                ids=[unit.get('parent_id')]+list(unit.get('member_ids',[]))
                connect([elements[eid].page for eid in ids if eid in elements])
    return blocked


def _bounded_source_ranges(expected, limit, blocked):
    ranges=[];start=0
    while start<len(expected):
        end=min(len(expected),start+limit)
        while end<len(expected) and end in blocked:end+=1
        ranges.append(expected[start:end]);start=end
    return ranges


def compose_reading_draft(book, translations, outdir, output_path=None,
                          event_callback=None, *, chunk_pages=16, cancel_event=None):
    """Produce a labelled, complete-coverage draft; never claim final QA.

    Parts bind current text, real asset bytes, source identity and compositor.
    Rerunning reuses unchanged parts. Existing formal PDFs/maps are untouched.
    """
    import pymupdf as fitz
    from . import layout

    started = time.perf_counter()
    if type(chunk_pages) is not int or not 1 <= chunk_pages <= 80:
        raise ValueError('阅读稿分段页数必须在1到80之间')
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    source_path = Path(book.source_path).resolve()
    if not source_path.is_file() or file_sha256(source_path) != book.source_sha256:
        raise ValueError('原书不存在或已改变，请恢复同一份原书后继续')
    if not book.selected_pages or len(set(book.selected_pages)) != len(book.selected_pages):
        raise ValueError('阅读稿需要互不重复的原PDF页码')
    if any(type(p) is not int or p < 1 or p > book.page_count for p in book.selected_pages):
        raise ValueError('阅读稿的原PDF页码无效')
    output = Path(output_path).resolve() if output_path else outdir / (safe_filename(book.title) + '_中文阅读稿.pdf')
    if output == source_path or output.suffix.lower() != '.pdf':
        raise ValueError('阅读稿必须是另一个PDF，不能覆盖原书')
    if output.exists():
        try:
            prior = json.loads((outdir / 'reading_draft_report.json').read_text(encoding='utf-8'))
            owned = (prior.get('kind') == 'reading_draft' and prior.get('accepted') is False
                     and Path(prior['pdf_path']).resolve() == output
                     and prior.get('pdf_sha256') == file_sha256(output))
        except (OSError, ValueError, KeyError, TypeError):
            owned = False
        if not owned:
            raise ValueError('此路径已有其他PDF；阅读稿不会覆盖正式成品或未认领的文件，请换输出路径')
    asset_paths = {asset.path for asset in book.assets.values()}
    protected = {str((root / path).resolve()) for root in layout._source_roots(book, outdir)
                 for path in asset_paths}
    if str(output) in protected:
        raise ValueError('阅读稿不能覆盖原书保护对象')
    # Fail once for a missing engine instead of silently retaining a whole book.
    layout._Runtime.find()
    run = outdir / 'reading_parts'
    run.mkdir(exist_ok=True)
    profile = dict(book.profile, layout_chunk_source_pages=max(1, min(80, int(chunk_pages))))
    view = replace(book, profile=profile)
    issues, parts, failure_events = [], [], []
    expected = list(book.selected_pages)
    position = {p: i for i, p in enumerate(expected)}
    blocked_cuts = _atomic_source_cuts(book)
    try:
        ranges = [part.selected_pages for part in layout._partition_book(view)]
        # Include blank/header-only pages too; those must not vanish.
        covered = {p for group in ranges for p in group}
        ranges.extend([[p] for p in expected if p not in covered])
        ranges.sort(key=lambda group: min(position[p] for p in group))
        if [p for group in ranges for p in group] != expected:
            raise ValueError('Partition does not preserve selected source order')
        # The engine partition and recursive error isolation must obey the
        # same source connections, including cross-page formula assets.
        joined=[]
        for group in ranges:
            if joined and position[group[0]] in blocked_cuts:
                joined[-1].extend(group)
            else:joined.append(list(group))
        ranges=joined
    except (ValueError, KeyError, TypeError):
        ranges = _bounded_source_ranges(expected,chunk_pages,blocked_cuts)
        issues.append({'code': 'reading_partition_needs_review', 'severity': 'warning',
                       'detail': '来源关联分段无法确认；阅读稿分段保留，正式版仍需检查。'})
    records = {int(p.get('number', p.get('page', i + 1))): p for i, p in enumerate(book.pages)}
    reused = 0
    callback_stop = False

    def check_stop():
        if callback_stop or (cancel_event is not None and cancel_event.is_set()):
            raise ReadingDraftCancelled('阅读稿已安全停止；已排分段已缓存，旧成品保持不变')

    def emit(message, **extra):
        nonlocal callback_stop
        if event_callback:
            callback_stop = event_callback({'stage': 'layout', 'message': message, **extra}) is False
        check_stop()

    def render_group(selected, original):
        nonlocal reused
        check_stop()
        subset = _subset(view, selected, outdir)
        key = '-'.join(map(str, selected))
        partdir = run / ('pages-' + key)
        partdir.mkdir(exist_ok=True)
        pdf = partdir / 'reading.pdf'
        receipt_path = partdir / 'receipt.json'
        # A genuine blank source page has no translation to fail. Verify the
        # actual PDF too; a missing parser element must not hide source ink.
        if len(selected)==1 and not subset.elements:
            page=original[selected[0]-1]
            if (not page.get_text().strip() and not page.get_image_info()
                    and not page.get_drawings() and page.first_annot is None
                    and not page.get_links()):
                with fitz.open() as blank:
                    target=blank.new_page(width=page.rect.width,height=page.rect.height)
                    label=page_label(records.get(selected[0],{'number':selected[0]}))
                    target.insert_text((12,page.rect.height-12),label,fontname='china-s',fontsize=8.5)
                    blank.save(pdf,garbage=4,deflate=True)
                parts.append({'pdf_path':str(pdf),'pdf_sha256':file_sha256(pdf),
                    'source_pages':list(selected),'page_count':1,'mode':'retained_blank'})
                emit('已保留原书空白页',source_pages=selected)
                return
        cause = None
        failures = []
        try:
            binding = _part_binding(subset, translations, outdir)
            if receipt_path.is_file() and pdf.is_file():
                prior = json.loads(receipt_path.read_text(encoding='utf-8'))
                if (prior.get('binding') == binding and prior.get('pdf_sha256') == file_sha256(pdf)
                        and prior.get('source_pages') == selected and prior.get('mode') == 'translated_draft'
                        and Path(prior.get('layout_map_path', '')).is_file()
                        and prior.get('layout_map_sha256') == file_sha256(prior['layout_map_path'])):
                    _checked_mapping(prior['layout_map_path'], selected, prior['page_count'])
                    parts.append(prior)
                    reused += 1
                    emit('复用已排好的阅读稿', source_pages=selected)
                    return
            if any('scan_evidence' in p for p in subset.pages):
                from .scan_layout import compose
            else:
                compose = layout.compose
            result = compose(subset, translations, partdir, output_path=pdf, allow_incomplete=True)
            with fitz.open(pdf) as actual:
                if len(actual) != result['page_count']:
                    raise ValueError('Composer page count differs from the saved PDF')
            mapping = _checked_mapping(result['layout_map_path'], selected, result['page_count'])
            source_codes = {digest(i) for i in subset.issues}
            failures = [i for i in result.get('issues', []) if i.get('severity') == 'error'
                        and i.get('code') != 'untranslated_preview' and digest(i) not in source_codes]
            if failures:
                cause = 'measured_layout_error'
                raise ValueError('Measured layout errors require source-page isolation')
            prior = {'binding': binding, 'pdf_path': str(pdf), 'pdf_sha256': file_sha256(pdf),
                     'source_pages': selected, 'mode': 'translated_draft',
                     'page_count': result['page_count'], 'layout_map_path': str(Path(result['layout_map_path']).resolve()),
                     'layout_map_sha256': file_sha256(result['layout_map_path']),
                     'layout_issues':[i for i in result.get('issues',[]) if digest(i) not in source_codes]}
            _atomic_json(receipt_path, prior)
            parts.append(prior)
            emit('已排好阅读稿的一部分', source_pages=selected)
            return
        except ReadingDraftCancelled:
            raise
        except (RuntimeError, ValueError, OSError, KeyError, TypeError) as error:
            # Exception text can include private paths or provider data. The
            # report needs a stable category, not an unfiltered traceback.
            cause = cause or type(error).__name__
        failure_codes=sorted({i['code'] for i in failures if isinstance(i.get('code'),str)})
        element_ids=sorted({i['element_id'] for i in failures if isinstance(i.get('element_id'),str)})
        asset_ids=sorted({i['asset_id'] for i in failures if isinstance(i.get('asset_id'),str)})
        failure_events.append({'source_pages':list(selected),'reason':cause,
            'failure_codes':failure_codes,'element_ids':element_ids,'asset_ids':asset_ids})
        cuts=[i for i in range(1,len(selected)) if position[selected[i]] not in blocked_cuts]
        if cuts:
            middle=min(cuts,key=lambda i:abs(i-len(selected)/2))
            render_group(selected[:middle], original)
            render_group(selected[middle:], original)
            return
        for number in selected:
            # An indivisible source unit may span several pages. Retain each
            # original without overwriting another member's PDF or losing order.
            retained_dir=run/('pages-'+str(number))
            retained_dir.mkdir(exist_ok=True)
            retained_pdf=retained_dir/'reading.pdf'
            label = page_label(records.get(number, {'number': number}))
            _original_page(original, number, retained_pdf, label)
            prior = {'pdf_path': str(retained_pdf), 'pdf_sha256': file_sha256(retained_pdf), 'source_pages': [number],
                     'mode': 'retained_original', 'page_count': 1, 'reason': cause, 'label': label,
                     'failure_codes':failure_codes,'atomic_source_pages':list(selected)}
            parts.append(prior)
            issues.append({'code': 'retained_untranslated_original_page', 'severity': 'warning',
                           'page': number, 'reason': cause,'failure_codes':failure_codes,
                           'element_ids':element_ids,'asset_ids':asset_ids,
                           'detail': '本页排版未完成，完整保留原页并明确标记；不计作中文已译页。'})
        emit('复杂页已保留原文并列入检查清单', source_pages=selected)

    with fitz.open(source_path) as original:
        if len(original) != book.page_count:
            raise ValueError('原PDF实际页数与任务记录不同')
        for group in ranges:
            render_group(group, original)
    # A future partition/refactor cannot omit, reorder or duplicate a source page.
    check_stop()
    if [p for part in parts for p in part['source_pages']] != expected:
        raise RuntimeError('阅读稿来源页覆盖不完整；保留旧成品，未发布新PDF')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name('.' + output.name + '.' + uuid.uuid4().hex + '.pending.pdf')
    source_to_output = {str(p): [] for p in expected}
    native_labels=[]
    nested_assets={}
    label_expansions=[]
    native_punctuation=[]
    label_decisions=[]
    offset = 0
    merge_started=time.perf_counter()
    emit('正在合并已保存的分段',source_pages=[])
    with fitz.open() as merged:
        for part_index,part in enumerate(parts,1):
            check_stop()
            with fitz.open(part['pdf_path']) as pdf:
                if len(pdf) != part['page_count'] or file_sha256(part['pdf_path']) != part['pdf_sha256']:
                    raise RuntimeError('阅读稿分段已改变；旧成品保持不变')
                merged.insert_pdf(pdf)
            if part['mode'] in {'retained_original','retained_blank'}:
                source_to_output[str(part['source_pages'][0])].append(offset + 1)
            else:
                if file_sha256(part['layout_map_path']) != part['layout_map_sha256']:
                    raise RuntimeError('阅读稿位置记录已改变；旧成品保持不变')
                mapping = _checked_mapping(part['layout_map_path'], part['source_pages'], part['page_count'])
                for row in mapping['pages']:
                    for page in row.get('source_pages', []):
                        source_to_output[str(page)].append(offset + int(row['page']))
                native_labels.extend(dict(row,rendered_page=offset+row['rendered_page'])
                                     for row in mapping.get('native_retained_labels',[]))
                label_expansions.extend(dict(row,rendered_page=offset+row['rendered_page'])
                                     for row in mapping.get('figure_label_blank_expansions',[]))
                native_punctuation.extend(dict(row,rendered_page=offset+row['rendered_page'])
                                     for row in mapping.get('native_spatial_punctuation',[]))
                label_decisions.extend(dict(row,rendered_page=offset+row['rendered_page'])
                                     for row in mapping.get('figure_label_layout_decisions',[]))
                for aid,rows in mapping.get('assets_retained_inside_native_labels',{}).items():
                    nested_assets.setdefault(aid,[]).extend(
                        dict(row,rendered_page=offset+row['rendered_page']) for row in rows)
                issues.extend(part.get('layout_issues',[]))
            part['output_start_page'] = offset + 1
            offset += part['page_count']
            if part_index%20==0 or part_index==len(parts):
                emit('正在合并已保存的分段',source_pages=part['source_pages'],
                     merged_parts=part_index,total_parts=len(parts))
        merged.set_metadata({'title': book.title + ' · 中文阅读稿（待检查）',
                             'producer': 'Textbook PDF automated reading draft'})
        from .pdf_merge import deduplicate_font_streams
        font_program_reuse=deduplicate_font_streams(merged)
        save_started=time.perf_counter()
        # Full object/stream equality scans (garbage >= 3) can dominate a large
        # book. Parts are already compressed; compact unreferenced objects and
        # preserve every imported stream instead of comparing every pair.
        merged.save(temporary, garbage=2, deflate=True)
        saved_at=time.perf_counter()
    with fitz.open(temporary) as check:
        if len(check) != offset or any(not pages or any(type(p) is not int or not 1 <= p <= offset for p in pages)
                                       for pages in source_to_output.values()):
            raise RuntimeError('阅读稿物理页与原页映射不符；旧成品保持不变')
    temporary.replace(output)
    required = {e.id for e in book.elements if needs_translation(e)}
    accepted = {eid for eid in required if isinstance(translations.get(eid), str) and translations[eid].strip()}
    fallback = [p['source_pages'][0] for p in parts if p['mode'] == 'retained_original']
    blank_pages=[p['source_pages'][0] for p in parts if p['mode']=='retained_blank']
    fallback_pages = set(fallback)
    rendered_required = {e.id for e in book.elements if needs_translation(e) and e.page not in fallback_pages}
    not_displayed_labels = {row['element_id'] for row in native_labels
                            if row.get('reason') == 'translated_label_overflow_preview'}
    actual_displayed_ids = (accepted & rendered_required) - not_displayed_labels
    missing_ids = required - accepted
    source_errors = [i for i in book.issues if i.get('severity') == 'error']
    report = {'schema_version': 1, 'kind': 'reading_draft', 'preview': True, 'accepted': False,
              'source_pdf_sha256': book.source_sha256,
              'translation_snapshot_sha256': digest({eid: translations[eid] for eid in sorted(accepted)}),
              'pdf_path': str(output), 'pdf_sha256': file_sha256(output), 'page_count': offset,
              'source_pages': len(expected), 'source_page_numbers': expected,
              'required_translations': len(required), 'translated_elements': len(accepted),
              'untranslated_elements': len(missing_ids),
              'displayed_translated_elements': len(actual_displayed_ids),
              'fallback_source_pages': fallback, 'source_to_output': source_to_output,
              'blank_source_pages':blank_pages,
              'native_retained_labels':native_labels,'native_retained_label_count':len(native_labels),
              'not_displayed_translated_label_ids':sorted(not_displayed_labels),
              'not_displayed_translated_label_count':len(not_displayed_labels),
              'figure_label_layout_decisions':label_decisions,
              'assets_retained_inside_native_labels':nested_assets,
              'figure_label_blank_expansions':label_expansions,
              'native_spatial_punctuation':native_punctuation,
              'source_diagnostics': len(source_errors),
              'source_diagnostics_by_code': dict(Counter(i.get('code', 'unknown') for i in source_errors)),
              'source_issues': source_errors,
              'untranslated_elements_by_id': [{'element_id': e.id, 'page': e.page}
                                              for e in book.elements if e.id in missing_ids],
              'protected_asset_pixels_checked': False, 'manual_visual_review': 'not_run',
              'full_book_acceptance': False, 'parts_reused': reused, 'parts': parts, 'issues': issues,
              'layout_failure_events':failure_events,
              'pdf_merge':{'garbage':2,'deflate':True,
                           'font_program_reuse':font_program_reuse,
                           'font_merge_module_sha256':file_sha256(Path(__file__).with_name('pdf_merge.py')),
                           'merge_seconds':round(save_started-merge_started,3),
                           'save_seconds':round(saved_at-save_started,3)},
              'elapsed_seconds': round(time.perf_counter() - started, 3),
              'report_path': str(outdir / 'reading_draft_report.json'),
              'html_report_path': str(outdir / '阅读稿检查清单.html')}
    _atomic_json(outdir / 'reading_draft_report.json', report)
    need_by_page=Counter(e.page for e in book.elements if e.id in required)
    shown_by_page=Counter(e.page for e in book.elements if e.id in actual_displayed_ids)
    missing_by_page=Counter(e.page for e in book.elements if e.id in missing_ids)
    source_by_page=Counter(i.get('page') for i in source_errors)
    retained_translated_by_page=Counter(e.page for e in book.elements if e.id in not_displayed_labels)
    rows=[]
    for p in expected:
        state=('原书空白页，保留页码对应关系' if p in blank_pages else
               '本页排版未完成，完整保留原文' if p in fallback else
               f'仍有 {missing_by_page[p]} 项未译' if missing_by_page[p] else
               '当前已存译文均已显示；仍待成品检查')
        if retained_translated_by_page[p]:
            state=(f'有 {retained_translated_by_page[p]} 条已存图内译文未显示，本稿保留原文；'
                   + (f'另有 {missing_by_page[p]} 项未译；' if missing_by_page[p] else '')
                   + '仍待成品检查')
        if source_by_page[p]:state+=f'；{source_by_page[p]} 条来源待核'
        cells=[str(p),page_label(records.get(p,{'number':p})),
               ', '.join(map(str,source_to_output[str(p)])),
               f'{shown_by_page[p]} / {need_by_page[p]}',state]
        rows.append('<tr>'+''.join('<td>'+html.escape(value)+'</td>'for value in cells)+'</tr>')
    body = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>阅读稿检查清单</title>' \
           '<style>body{font:16px/1.7 system-ui;max-width:1000px;margin:36px auto;padding:0 20px;color:#202b3c}' \
           'table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd}' \
           'aside{background:#fff3dc;padding:18px;border-radius:10px}a{color:#1766a6}</style>' \
           '<h1>阅读稿检查清单</h1><aside>这是自动生成的阅读稿，尚未完成全书验收。' \
           '未译文字与保留原页均有明确标记；没有省略疑难页。公式像素和逐页视觉复核可在成品后单独进行。</aside>' \
           f'<p><a href="{html.escape(output.name, quote=True)}">打开中文阅读稿</a></p>' \
           f'<p>原PDF {len(expected)} 页 → 阅读稿 {offset} 页。已保存译文 {len(accepted)} / {len(required)} 项；' \
           f'其中本稿实际显示 {len(actual_displayed_ids)} 项。{len(fallback)} 页完整保留原文；' \
           f'{len(source_errors)} 条来源诊断保留待查。</p>' \
           '<table><thead><tr><th>原PDF物理页</th><th>原书页码</th><th>阅读稿页</th><th>本稿显示译文 / 需译项</th><th>状态</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></html>'
    (outdir / '阅读稿检查清单.html').write_text(body, encoding='utf-8')
    return report
