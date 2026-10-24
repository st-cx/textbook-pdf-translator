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
    protected = {str((root / a.path).resolve()) for root in layout._source_roots(book, outdir)
                 for a in book.assets.values()}
    if str(output) in protected:
        raise ValueError('阅读稿不能覆盖原书保护对象')
    # Fail once for a missing engine instead of silently retaining a whole book.
    layout._Runtime.find()
    run = outdir / 'reading_parts'
    run.mkdir(exist_ok=True)
    profile = dict(book.profile, layout_chunk_source_pages=max(1, min(80, int(chunk_pages))))
    view = replace(book, profile=profile)
    issues, parts = [], []
    expected = list(book.selected_pages)
    position = {p: i for i, p in enumerate(expected)}
    try:
        ranges = [part.selected_pages for part in layout._partition_book(view)]
        # Include blank/header-only pages too; those must not vanish.
        covered = {p for group in ranges for p in group}
        ranges.extend([[p] for p in expected if p not in covered])
        ranges.sort(key=lambda group: min(position[p] for p in group))
        if [p for group in ranges for p in group] != expected:
            raise ValueError('Partition does not preserve selected source order')
    except (ValueError, KeyError, TypeError):
        ranges = [expected[i:i + chunk_pages] for i in range(0, len(expected), chunk_pages)]
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
        cause = None
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
                     'layout_map_sha256': file_sha256(result['layout_map_path'])}
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
        if len(selected) > 1:
            middle = len(selected) // 2
            render_group(selected[:middle], original)
            render_group(selected[middle:], original)
            return
        number = selected[0]
        label = page_label(records.get(number, {'number': number}))
        _original_page(original, number, pdf, label)
        prior = {'pdf_path': str(pdf), 'pdf_sha256': file_sha256(pdf), 'source_pages': selected,
                 'mode': 'retained_original', 'page_count': 1, 'reason': cause, 'label': label}
        parts.append(prior)
        issues.append({'code': 'retained_untranslated_original_page', 'severity': 'warning',
                       'page': number, 'reason': cause,
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
    offset = 0
    with fitz.open() as merged:
        for part in parts:
            with fitz.open(part['pdf_path']) as pdf:
                if len(pdf) != part['page_count'] or file_sha256(part['pdf_path']) != part['pdf_sha256']:
                    raise RuntimeError('阅读稿分段已改变；旧成品保持不变')
                merged.insert_pdf(pdf)
            if part['mode'] == 'retained_original':
                source_to_output[str(part['source_pages'][0])].append(offset + 1)
            else:
                if file_sha256(part['layout_map_path']) != part['layout_map_sha256']:
                    raise RuntimeError('阅读稿位置记录已改变；旧成品保持不变')
                mapping = _checked_mapping(part['layout_map_path'], part['source_pages'], part['page_count'])
                for row in mapping['pages']:
                    for page in row.get('source_pages', []):
                        source_to_output[str(page)].append(offset + int(row['page']))
            part['output_start_page'] = offset + 1
            offset += part['page_count']
        merged.set_metadata({'title': book.title + ' · 中文阅读稿（待检查）',
                             'producer': 'Textbook PDF automated reading draft'})
        merged.save(temporary, garbage=4, deflate=True)
    with fitz.open(temporary) as check:
        if len(check) != offset or any(not pages or any(type(p) is not int or not 1 <= p <= offset for p in pages)
                                       for pages in source_to_output.values()):
            raise RuntimeError('阅读稿物理页与原页映射不符；旧成品保持不变')
    temporary.replace(output)
    required = {e.id for e in book.elements if needs_translation(e)}
    accepted = {eid for eid in required if isinstance(translations.get(eid), str) and translations[eid].strip()}
    fallback = [p['source_pages'][0] for p in parts if p['mode'] == 'retained_original']
    rendered_required = {e.id for e in book.elements if needs_translation(e) and e.page not in set(fallback)}
    source_errors = [i for i in book.issues if i.get('severity') == 'error']
    report = {'schema_version': 1, 'kind': 'reading_draft', 'preview': True, 'accepted': False,
              'source_pdf_sha256': book.source_sha256,
              'translation_snapshot_sha256': digest({eid: translations[eid] for eid in sorted(accepted)}),
              'pdf_path': str(output), 'pdf_sha256': file_sha256(output), 'page_count': offset,
              'source_pages': len(expected), 'source_page_numbers': expected,
              'required_translations': len(required), 'translated_elements': len(accepted),
              'untranslated_elements': len(required - accepted),
              'displayed_translated_elements': len(accepted & rendered_required),
              'fallback_source_pages': fallback, 'source_to_output': source_to_output,
              'source_diagnostics': len(source_errors),
              'source_diagnostics_by_code': dict(Counter(i.get('code', 'unknown') for i in source_errors)),
              'source_issues': source_errors,
              'untranslated_elements_by_id': [{'element_id': e.id, 'page': e.page}
                                              for e in book.elements if e.id in required - accepted],
              'protected_asset_pixels_checked': False, 'manual_visual_review': 'not_run',
              'full_book_acceptance': False, 'parts_reused': reused, 'parts': parts, 'issues': issues,
              'elapsed_seconds': round(time.perf_counter() - started, 3),
              'report_path': str(outdir / 'reading_draft_report.json'),
              'html_report_path': str(outdir / '阅读稿检查清单.html')}
    _atomic_json(outdir / 'reading_draft_report.json', report)
    rows = ''.join('<tr><td>' + str(p) + '</td><td>' +
                   html.escape(page_label(records.get(p, {'number': p}))) + '</td><td>' +
                   ', '.join(map(str, source_to_output[str(p)])) + '</td><td>' +
                   ('保留原文，尚未翻译' if p in fallback else '已重排；未译块有标记') + '</td></tr>' for p in expected)
    body = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>阅读稿检查清单</title>' \
           '<style>body{font:16px/1.7 system-ui;max-width:1000px;margin:36px auto;padding:0 20px;color:#202b3c}' \
           'table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd}' \
           'aside{background:#fff3dc;padding:18px;border-radius:10px}a{color:#1766a6}</style>' \
           '<h1>阅读稿检查清单</h1><aside>这是自动生成的阅读稿，尚未完成全书验收。' \
           '未译文字与保留原页均有明确标记；没有省略疑难页。公式像素和逐页视觉复核可在成品后单独进行。</aside>' \
           f'<p><a href="{html.escape(output.name, quote=True)}">打开中文阅读稿</a></p>' \
           f'<p>原PDF {len(expected)} 页 → 阅读稿 {offset} 页。已保存译文 {len(accepted)} / {len(required)} 项；' \
           f'其中本稿实际显示 {len(accepted & rendered_required)} 项。{len(fallback)} 页完整保留原文；' \
           f'{len(source_errors)} 条来源诊断保留待查。</p>' \
           '<table><thead><tr><th>原PDF物理页</th><th>原书页码</th><th>阅读稿页</th><th>状态</th></tr></thead><tbody>' + rows + '</tbody></table></html>'
    (outdir / '阅读稿检查清单.html').write_text(body, encoding='utf-8')
    return report
