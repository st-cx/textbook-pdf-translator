"""Verify source pagination from each physical page, without an offset guess.

Native marginal text or explicit PDF PageLabels provide evidence. A missing
printed label deliberately retains the physical PDF page as its fallback.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import statistics

import pymupdf as fitz

from .models import Book, file_sha256


def page_label(record):
    number = record.get('number', record.get('page'))
    printed = record.get('printed_page')
    return f'原书 p.{printed}' if printed else f'原 PDF 第{number}页'


def footer_label(labels):
    labels = list(dict.fromkeys(labels))
    if len(labels) > 1 and all(re.fullmatch(r'原书 p\.\d+', label) for label in labels):
        numbers = sorted({int(label.removeprefix('原书 p.')) for label in labels})
        if numbers[-1] - numbers[0] == len(numbers) - 1:
            return f'原书 pp.{numbers[0]}–{numbers[-1]}'
    return ' · '.join(labels)


PAGE_NUMBER = re.compile(r'\d{1,4}|[ivxlcdmIVXLCDM]{1,10}|[A-Za-z]{1,4}[-–]\d{1,4}')
_MATHEMATICAL_FONT = re.compile(r'math|symbol|cmmi|cmsy|cmex|msam|msbm|mt.?extra|euclid', re.I)


def pagination_candidates(page, lines):
    """Require a page-edge text run, never a mathematical superscript/limit."""
    candidates=[]
    for line in lines:
        box=fitz.Rect(line['bbox'])
        if not (box.y1 <= page.rect.height*.075 or box.y0 >= page.rect.height*.925):
            continue
        text=line['text'].strip()
        chars=[g for g in line.get('chars',[]) if g.get('c','').strip()]
        if not PAGE_NUMBER.fullmatch(text):
            continue
        if not (text.isdigit() or len(text)>1 or text.lower() in {'i','v','x'}):
            continue
        if any(_MATHEMATICAL_FONT.search(g.get('font','')) or g.get('flags',0)&3 for g in chars):
            continue
        size=statistics.median(g.get('size',0) for g in chars) if chars else box.height
        support=[]
        if box.y1<=page.rect.height*.075:
            for other in lines:
                if other is line or not re.search(r'[A-Za-z]{3,}',other['text']):continue
                other_box=fitz.Rect(other['bbox'])
                if abs((box.y0+box.y1-other_box.y0-other_box.y1)*.5)<=max(1.5,size*.25):
                    support.append(other['bbox'])
        candidates.append({'text':text,'bbox':list(box),'font_size':size,
                           'running_header_support':support})
    return candidates


def recognize_printed_page(page, lines):
    candidates=pagination_candidates(page,lines)
    unique={entry['text'] for entry in candidates}
    if len(unique)==1:
        return next(iter(unique)), 'margin-glyph', .98
    if len(unique)>1:
        supported=[entry for entry in candidates if entry['running_header_support']]
        if len({entry['text'] for entry in supported})==1:
            return supported[0]['text'],'margin-glyph',.98
        outer=[entry for entry in candidates if entry['bbox'][0]<page.rect.width*.2 or entry['bbox'][2]>page.rect.width*.8]
        if len({entry['text'] for entry in outer})==1:
            return outer[0]['text'],'margin-glyph',.9
        return None,'ambiguous',0
    try:
        label=page.get_label()
    except (IndexError,ValueError,RuntimeError):
        label=None
    return (label,'pdf-page-label',.8) if label else (None,'absent',0)


def _native_lines(page):
    result = []
    for block in page.get_text('dict')['blocks']:
        for line in block.get('lines', []):
            chars=[{'c':c,'font':span['font'],'size':span['size'],'flags':span['flags']}
                   for span in line['spans'] for c in span['text']]
            result.append({'text':''.join(g['c'] for g in chars),'bbox':line['bbox'],'chars':chars})
    return result


def audit_source_pages(book, source_path=None):
    """Read and bind actual page evidence; unresolved labels are not invented."""
    path = Path(source_path or book.source_path)
    result = {'schema_version': 1, 'source_sha256': book.source_sha256, 'read_only': True,
              'status': 'unverified', 'checked_pages': 0, 'printed_labels_supported': 0,
              'physical_fallback_pages': 0, 'scan_labels_requiring_visual_review':0,'issues': [], 'pages': [],
              'limitations': ['Marginal glyph and explicit PageLabels checks do not replace visual review of ambiguous or scanned pagination.',
                              'No constant physical-to-printed offset is inferred.']}
    def issue(code, detail, **fields):
        result['issues'].append(dict(code=code, severity='error', detail=detail, **fields))
    if not path.is_file() or file_sha256(path) != book.source_sha256:
        issue('source_pagination_identity_unverified', '原书缺失或内容哈希改变，无法核验实际页码')
        return result
    seen = set()
    with fitz.open(path) as pdf:
        for record in book.pages:
            number = record.get('number', record.get('page'))
            if not isinstance(number, int) or number in seen or not 1 <= number <= len(pdf):
                issue('source_page_identity_invalid', '原书物理页记录重复或不存在', page=number)
                continue
            seen.add(number)
            page = pdf[number-1]
            if 'scan_evidence' in record or record.get('coverage',{}).get('basis')=='raster-ink-and-ocr-units':
                # A scan's native/hidden text is deliberately not pagination
                # truth. OCR confidence is not a visual pagination proof.
                printed=record.get('printed_page')
                result['checked_pages']+=1
                result['pages'].append({'physical_page':number,'printed_page':printed,'display_label':page_label(record),
                    'status':'scan_label_requires_visual_review' if printed else 'physical_fallback',
                    'label_source':record.get('label_source'),
                    'scan_image_sha256':record.get('scan_evidence',{}).get('image_sha256'),
                    'native_margin_glyphs_certified':False})
                if printed:
                    result['scan_labels_requiring_visual_review']+=1
                    result['issues'].append(dict(code='scanned_pagination_requires_visual_review',severity='warning',page=number,
                        detail='扫描页码依赖原图与逐页核图证据；缺少原生字形不等于页码矛盾，本项不以OCR置信度代替目视通过'))
                else:result['physical_fallback_pages']+=1
                continue
            lines=_native_lines(page)
            candidates = pagination_candidates(page,lines)
            recognized,method,confidence=recognize_printed_page(page,lines)
            printed = str(record['printed_page']) if record.get('printed_page') else None
            try:
                pdf_label = page.get_label()
            except (IndexError,ValueError,RuntimeError):
                pdf_label = None
            evidence = {'physical_page': number, 'printed_page': printed, 'display_label': page_label(record),
                        'native_margin_candidates': candidates, 'pdf_page_label': pdf_label or None,
                        'recognized_printed_page':recognized,'recognition_method':method,'confidence':confidence}
            if printed is None:
                result['physical_fallback_pages'] += 1
                evidence['status'] = 'physical_fallback'
                if recognized:
                    issue('printed_page_available_but_not_mapped','本页存在可靠页码证据但来源仍使用物理页，应修正来源标记',page=number,recognized_printed_page=recognized)
            elif recognized == printed and method=='margin-glyph':
                result['printed_labels_supported'] += 1
                evidence['status'] = 'native_margin_glyph_supported'
            elif recognized == printed and method=='pdf-page-label':
                result['printed_labels_supported'] += 1
                evidence['status'] = 'explicit_pdf_page_label_supported'
            else:
                evidence['status'] = 'unsupported'
                issue('printed_page_evidence_mismatch', '已记录印刷页码未在本物理页的页边字形或明确PDF标签中得到支持',
                      page=number, printed_page=printed)
            result['checked_pages'] += 1
            result['pages'].append(evidence)
    records = {p.get('number', p.get('page')): p for p in book.pages}
    for element in book.elements:
        record = records.get(element.page)
        if record is None:
            issue('element_source_page_missing', '元素来源页没有对应物理页记录', page=element.page, element_id=element.id)
        elif element.printed_page and str(element.printed_page) != str(record.get('printed_page') or ''):
            issue('element_printed_page_mismatch', '元素页码与所在原书物理页的页码记录不一致', page=element.page, element_id=element.id)
    if seen != set(book.selected_pages):
        issue('source_page_records_incomplete', '原书页记录与实际选择页集合不一致')
    result['status'] = 'supported_with_physical_fallbacks' if not result['issues'] else 'review_required'
    return result


def check_output_source_labels(book, layout_map, pdf_path=None):
    """Check every added output page against its actual measured source units."""
    issues = []
    records = {p.get('number', p.get('page')): p for p in book.pages}
    positions = layout_map.get('elements', {})
    by_id = book.element_map()
    expected = {}
    for eid, fragments in positions.items():
        element = by_id.get(eid)
        if element is None or element.metadata.get('render_suppressed'):
            continue
        for fragment in fragments:
            expected.setdefault(fragment.get('page'), set()).add(element.page)
    pdf = fitz.open(pdf_path) if pdf_path and Path(pdf_path).is_file() else None
    def add(code, detail, output_page):
        issues.append(dict(code=code, severity='error', detail=detail, output_page=output_page))
    try:
        seen=set()
        for record in layout_map.get('pages', []):
            output = record.get('page')
            if output in seen:
                add('output_source_page_record_duplicate','同一成品页有多条来源页记录',output)
            seen.add(output)
            pages = record.get('source_pages', [])
            if set(pages) != expected.get(output, set()) or len(set(pages))!=len(pages):
                add('output_source_page_mapping_mismatch', '成品页的原页映射与本页实际元素来源不一致', output)
            if any(page not in records for page in pages):
                add('output_source_page_unknown', '成品来源指向不存在的原书页', output)
                continue
            labels = [page_label(records[page]) for page in pages]
            if record.get('source_labels') != labels:
                add('output_source_label_mismatch', '成品页的页码文字与逐页来源记录不一致', output)
            if pdf is not None and labels:
                if not isinstance(output, int) or not 1 <= output <= len(pdf):
                    add('output_source_footer_page_missing', '页码标注指向不存在的成品页', output)
                    continue
                page = pdf[output-1]
                # The compositor places the visible source marker in the footer.
                text = page.get_text(clip=fitz.Rect(0, page.rect.height-70, page.rect.width, page.rect.height))
                normalize = lambda value: re.sub(r'\s+', '', value).replace('−', '–').replace('-', '–')
                wanted = footer_label(labels)
                # Non-consecutive source labels may follow reading order instead
                # of physical order. Check each explicit label in that case.
                parts = [wanted] if ' · ' not in wanted else labels
                if any(normalize(part) not in normalize(text) for part in parts):
                    add('output_source_footer_missing_or_wrong', '成品实际页边未读到与来源记录一致的原书页码', output)
        for output in sorted(set(expected)-seen):
            add('output_source_page_record_missing','本页有实际源元素，但缺少该成品页的来源记录',output)
        if pdf is not None:
            for output in sorted(set(range(1,len(pdf)+1))-seen):
                add('output_source_page_record_missing','成品PDF中的本页没有对应的来源页记录',output)
    finally:
        if pdf is not None:
            pdf.close()
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('book', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    report = audit_source_pages(Book.load(args.book))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({k: v for k, v in report.items() if k not in {'pages', 'issues', 'limitations'}}, ensure_ascii=False))
    print(json.dumps({'errors': len(report['issues'])}))
    return 2 if report['issues'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
