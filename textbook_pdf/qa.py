"""Document, translation and rendered-output checks with explicit acceptance boundaries."""
from __future__ import annotations

import html
import json
import math
from collections import Counter
from pathlib import Path

from .models import TRANSLATABLE, TOKEN_RE, file_sha256, validate_translation, needs_translation
from .visual_checks import PROTECTED_ASSET_KINDS, compare_assets, resolve_asset_path, archived_asset_dispositions


def _visual_summary(book, result=None, reason=None):
    archived=archived_asset_dispositions(book)
    by_kind = Counter(asset.kind for aid,asset in book.assets.items() if aid not in archived)
    eligible = {kind:count for kind,count in by_kind.items() if kind in PROTECTED_ASSET_KINDS}
    excluded = {kind:count for kind,count in by_kind.items() if kind not in PROTECTED_ASSET_KINDS}
    summary = {'status':'not_run','total_book_assets':len(book.assets),
               'eligible_source_assets':sum(eligible.values()), 'eligible_source_assets_by_kind':eligible,
               'excluded_source_assets':sum(excluded.values()), 'excluded_source_assets_by_kind':excluded,
               'archived_source_assets':len(archived),'archived_source_assets_by_kind':dict(Counter(book.assets[aid].kind for aid in archived)),
               'archived_asset_dispositions':list(archived.values()),
               'eligible_occurrences':None, 'checked_occurrences':0, 'passed_occurrences':0,
               'compared_unique_source_assets':0,'review_occurrences':0,'skipped':0,'limited':0,
               'errors':0,'review_warnings':0,'all_eligible_occurrences_checked':False,
               'all_eligible_occurrences_passed':False,'all_book_assets_passed':False,
               'reason':reason,'scope':'Visible ink in measured immutable-object boxes; figures/tables require unchanged geometry.'}
    if result is None:
        return summary
    errors=sum(issue.get('severity')=='error' for issue in result['issues'])
    warnings=sum(issue.get('severity','warning')=='warning' for issue in result['issues'])
    checked,passed=result['checked'],result['passed']
    unique_assets=len({metric['asset_id'] for metric in result['metrics']})
    complete=bool(result['all_eligible_occurrences_checked'])
    all_passed=complete and checked>0 and checked==passed
    summary.update({'status':'completed' if not errors and not warnings and complete else 'review_required',
                    'eligible_occurrences':result['eligible_occurrences'],'checked_occurrences':checked,
                    'passed_occurrences':passed,'compared_unique_source_assets':unique_assets,
                    'review_occurrences':checked-passed,'skipped':result['skipped'],'limited':result['limited'],
                    'errors':errors,'review_warnings':warnings,'all_eligible_occurrences_checked':complete,
                    'all_eligible_occurrences_passed':all_passed,
                    'all_book_assets_passed':all_passed and not excluded and unique_assets==len(book.assets),
                    'report_path':result['report_path'],'parameters':result['parameters'],
                    'limitations':result['limitations'],'reason':None})
    if not eligible:
        summary['status']='not_applicable'
    return summary


def assembled_source_dispositions(book):
    """Verify that suppressed labels really moved into one active source unit."""
    elements=book.element_map();result={}
    for child in book.elements:
        target=elements.get(child.metadata.get('superseded_by'))
        moved=set(child.metadata.get('source_glyph_ids',[]))
        if (not child.metadata.get('render_suppressed') or not child.metadata.get('preserve_only')
            or not child.metadata.get('exclusion_reason') or not target
            or target.page!=child.page or target.metadata.get('render_suppressed') or child.metadata.get('glyph_ids')
            or not moved or not moved<=set(target.metadata.get('glyph_ids',[]))):continue
        expected=Counter(TOKEN_RE.findall(child.text));actual=Counter(TOKEN_RE.findall(target.text))
        if any(actual[aid]<count for aid,count in expected.items()):continue
        result[child.id]={'element_id':child.id,'replacement_element_id':target.id,
            'source_page':child.page,'source_glyphs_transferred':len(moved),
            'protected_tokens_transferred':dict(expected),'reason':child.metadata['exclusion_reason']}
    return result


def scan_source_summary(book, outdir):
    """Summarize immutable scan evidence without rerunning recognition.

    Native glyph counts, pixel ownership and CTC confidence remain different
    observations. The saved image hash binds decoded RGB bytes, not PNG bytes.
    """
    outdir=Path(outdir)
    pages=[];issues=[]
    def add(code,detail,severity='error',**fields):
        issues.append(dict(code=code,detail=detail,severity=severity,**fields))
    def count(value):
        return value if isinstance(value,int) and not isinstance(value,bool) and value>=0 else None
    for page in book.pages:
        coverage=page.get('coverage',{})
        if 'scan_evidence' not in page and coverage.get('basis')!='raster-ink-and-ocr-units':continue
        number=page.get('page',page.get('number'))
        evidence=page.get('scan_evidence')
        if not isinstance(evidence,dict):evidence={}
        quality=evidence.get('quality')
        valid=isinstance(quality,dict) and quality.get('coverage_basis')=='raster-ink-and-ocr-units'
        foreground=covered=unassigned=None
        if valid:
            foreground=count(quality.get('foreground_pixels'))
            covered=count(quality.get('covered_ink_pixels'))
            unassigned=count(quality.get('unassigned_ink_pixels'))
            valid=(None not in (foreground,covered,unassigned) and covered<=foreground and covered+unassigned==foreground)
        if not valid:
            add('scan_raster_evidence_invalid','缺少有效扫描墨迹归属证据；不能以原文字形为0代替识别覆盖',page=number)
            quality=quality if isinstance(quality,dict) else {}
            foreground=covered=unassigned=None
        quality_issues=quality.get('issues',[])
        if not isinstance(quality_issues,list):
            add('scan_quality_issues_invalid','扫描质量诊断记录格式无效',page=number)
            quality_issues=[]
        for issue in quality_issues:
            if isinstance(issue,dict) and issue.get('code'):
                issues.append({**issue,'page':number})
        if valid and (count(quality.get('unassigned_meaningful_component_count')) or 0)>0:
            if not any(i.get('code')=='scan_unassigned_ink' for i in quality_issues if isinstance(i,dict)):
                add('scan_unassigned_ink','扫描图存在未归属的有意义墨迹组件',page=number)
        image_status='unverified'
        image_value=evidence.get('image');expected=evidence.get('image_sha256')
        if not isinstance(image_value,str) or not image_value or not isinstance(expected,str) or len(expected)!=64:
            add('scan_source_image_identity_missing','扫描原图路径或RGB内容哈希缺失，无法验证源图证据',page=number)
        else:
            path=Path(image_value)
            roots=[outdir]
            if book.profile.get('job_dir'):roots.append(Path(book.profile['job_dir']))
            candidates=[]
            if path.is_absolute():
                if any(path.resolve().is_relative_to(root.resolve()) for root in roots):candidates=[path]
            else:
                candidates=[root/path for root in roots if (root/path).resolve().is_relative_to(root.resolve())]
            found=next((candidate for candidate in candidates if candidate.is_file()),None)
            if found is None:
                add('scan_source_image_missing','扫描原图缺失或路径超出任务目录；原图证据未验证',page=number)
                image_status='missing'
            else:
                try:
                    import hashlib
                    from PIL import Image
                    with Image.open(found) as original:
                        if original.width*original.height>40_000_000:raise ValueError('扫描图超过像素检查预算')
                        image=original.convert('RGB')
                        actual=hashlib.sha256(image.tobytes()).hexdigest()
                        image_size=[image.width,image.height]
                    if actual!=expected:
                        image_status='changed'
                        add('scan_source_image_changed','扫描原图RGB像素已变化，保存的OCR和墨迹证据已失去绑定',page=number)
                    elif evidence.get('image_size') is not None and evidence['image_size']!=image_size:
                        image_status='size_mismatch'
                        add('scan_source_image_size_mismatch','扫描原图尺寸与识别坐标系不一致',page=number)
                    else:image_status='current'
                except Exception as error:
                    image_status='unreadable'
                    add('scan_source_image_unreadable','扫描原图证据无法解码或超过资源边界：'+type(error).__name__,page=number)
        if evidence.get('optical_recognition_verified') is True or evidence.get('recognition_accuracy') is not None:
            add('scan_recognition_claim_unsupported','像素归属和CTC置信度不能证明OCR准确率；本报告不采用该识别通过声明',severity='warning',page=number)
        if evidence.get('original_composed_page_preserved') is not True:
            add('scan_composed_source_unverified','未记录完整原扫描合成画面的保留证据',page=number)
        ocr=quality.get('ocr_units',{})
        if not isinstance(ocr,dict):ocr={}
        low=count(ocr.get('low_confidence'))
        unknown=count(ocr.get('unknown_confidence'))
        for code,value,detail in [('scan_low_confidence_ocr',low,'扫描来源仍有低置信OCR单元'),
                                  ('scan_ocr_confidence_unavailable',unknown,'扫描来源有未取得有效置信度的OCR单元')]:
            if value and not any(isinstance(i,dict) and i.get('code')==code for i in quality_issues):
                add(code,detail,severity='warning',page=number,unit_count=value)
        pages.append({'page':number,'printed_page':page.get('printed_page'),
                      'coverage_basis':'raster-ink-and-ocr-units','quality_evidence_valid':valid,
                      'source_image':image_value,'source_image_status':image_status,
                      'dpi':evidence.get('dpi'),'image_size':evidence.get('image_size'),
                      'native_pdf_glyphs':count(coverage.get('total_glyphs')),
                      'ocr_units':count(coverage.get('total_ocr_units')),
                      'low_confidence_ocr_units':low,'unknown_confidence_ocr_units':unknown,
                      'foreground_pixels':foreground,'covered_ink_pixels':covered,'unassigned_ink_pixels':unassigned,
                      'unassigned_meaningful_component_count':count(quality.get('unassigned_meaningful_component_count')),
                      'ink_coverage_ratio':covered/foreground if valid and foreground else None,
                      'raster_coverage_complete':bool(valid and image_status=='current' and quality.get('raster_coverage_complete') and count(quality.get('unassigned_meaningful_component_count'))==0 and not any(isinstance(i,dict) and i.get('severity')=='error' for i in quality_issues)),
                      'optical_recognition_verified':False,'recognition_accuracy':None,
                      'ocr_identity':evidence.get('ocr_identity'),
                      'quality_issue_count':len(quality_issues),'thresholds':quality.get('thresholds',{})})
    if pages:
        add('scan_optical_review_required','扫描页来自本地OCR；墨迹归属不证明OCR准确，原PDF字形为0不代表没有漏字。仍须对照原图复核文字、公式、代码与页码',
            severity='warning',source_pages=[p['page'] for p in pages])
    valid_pages=[p for p in pages if p['quality_evidence_valid']]
    total=lambda key:sum(p[key] or 0 for p in valid_pages)
    foreground=total('foreground_pixels');covered=total('covered_ink_pixels')
    summary={'status':'review_required' if pages else 'not_applicable',
             'coverage_basis':'raster-ink-and-ocr-units','scanned_pages':len(pages),
             'source_pages':[p['page'] for p in pages],
             'pages_with_quality_evidence':len(valid_pages),'pages_without_quality_evidence':len(pages)-len(valid_pages),
             'source_images_verified':sum(p['source_image_status']=='current' for p in pages),
             'ink_totals_scope':'pages_with_valid_saved_quality_evidence_only',
             'all_scan_pages_have_current_evidence':bool(pages and len(valid_pages)==len(pages) and all(p['source_image_status']=='current' for p in pages)),
             'foreground_pixels':foreground,'covered_ink_pixels':covered,
             'unassigned_ink_pixels':total('unassigned_ink_pixels'),
             'ink_coverage_ratio':covered/foreground if foreground else None,
             'total_ocr_units':sum(p['ocr_units'] or 0 for p in pages),
             'pages_with_unknown_ocr_unit_count':sum(p['ocr_units'] is None for p in pages),
             'low_confidence_ocr_units':sum(p['low_confidence_ocr_units'] or 0 for p in pages),
             'unknown_confidence_ocr_units':sum(p['unknown_confidence_ocr_units'] or 0 for p in pages),
             'raster_coverage_complete':bool(pages and all(p['raster_coverage_complete'] for p in pages)),
             'optical_recognition_verified':False,'recognition_accuracy':None,'pages':pages,
             'limitations':['CTC词框和字符框是估计位置，不是PDF原生字形。',
                            '区域和墨迹覆盖不证明未漏字，也不证明文字、公式或代码识别正确。',
                            'OCR置信度是未校准模型分数，不是实测准确率。',
                            '原PDF字形为0、墨迹全覆盖或生成成功均不能作为扫描验收通过。',
                            '本阶段读取已保存证据，不重新执行OCR或版面检测；全文仍需视觉和语义验收。']}
    return summary,issues


def verify(book, translations, outdir, pdf_path=None, layout_map=None, extra_issues=None, resolved_issues=None, semantic_status=None):
    outdir=Path(outdir)
    outdir.mkdir(parents=True,exist_ok=True)
    issues=list(book.issues)+list(extra_issues or [])
    if book.profile.get("argument_units_v1") or any(g.get("disposition")=="reconstructed-source-primitive" for p in book.pages for g in p.get("graphics_coverage",[])):
        from .argument_units import source_issues
        issues.extend(source_issues(book))
    scans,scan_issues=scan_source_summary(book,outdir)
    existing_scan_keys={(i.get('code'),i.get('page'),i.get('severity','warning'),i.get('detail')) for i in issues}
    for issue in scan_issues:
        key=(issue.get('code'),issue.get('page'),issue.get('severity','warning'),issue.get('detail'))
        if key not in existing_scan_keys:issues.append(issue);existing_scan_keys.add(key)
    def add(code,detail,severity='error',**kw):
        issues.append(dict(code=code,detail=detail,severity=severity,**kw))
    native_source={'status':'not_applicable','all_eligible_glyph_counts_present':None,
                   'scope':'Native source glyph identities before output composition; not formula semantics.'}
    if any(asset.metadata.get('glyph_ids') for asset in book.assets.values()):
        from .source_integrity import audit_native_assets
        try:
            audit=audit_native_assets(book,outdir,outdir/'source_integrity')
            issues.extend(audit['issues'])
            native_source={k:v for k,v in audit.items() if k not in {'issues','page_results'}}
            native_source['status']=('present' if audit['all_eligible_glyph_counts_present'] is True
                                     else 'review_required' if audit['all_eligible_glyph_counts_present'] is False
                                     else 'not_applicable')
            native_source['report_path']='source_integrity/source-native-glyph-audit.json'
        except (OSError,ValueError,RuntimeError,KeyError,TypeError) as error:
            native_source['status']='unavailable'
            add('native_source_integrity_unavailable','原生保护对象字形检查未完成：'+type(error).__name__)
    pagination={'status':'not_run','reason':'No original PDF with a content identity is available.'}
    if Path(book.source_path).is_file() and len(book.source_sha256)==64:
        from .source_pages import audit_source_pages
        pagination=audit_source_pages(book)
        issues.extend(pagination['issues'])
        (outdir/'source_page_audit.json').write_text(json.dumps(pagination,ensure_ascii=False,indent=2),encoding='utf8')
        pagination={k:v for k,v in pagination.items() if k not in {'issues','pages'}}
        pagination['report_path']='source_page_audit.json'
    scan_style=book.profile.get('scan_composition')
    if scan_style:
        style_source=Path(__file__).with_name('scan_layout.py')
        if not style_source.is_file() or scan_style.get('module_sha256')!=file_sha256(style_source):
            add('scan_compositor_changed','扫描排版纸色处理程序已改变，须重新排版以核验当前版本')
    ids=[e.id for e in book.elements]
    if len(set(ids)) != len(ids):add('duplicate_element_id','原文元素编号重复')
    source_pages={p.get('page',p.get('number')):p for p in book.pages}
    for p in book.pages:
        cov=p.get('coverage',{})
        if cov.get('unaccounted',cov.get('unaccounted_glyphs',cov.get('unresolved',0))):
            add('unaccounted_source','存在未说明去向的原文字形',page=p.get('page',p.get('number')))
    missing=[]
    expected_assets=Counter()
    assembled=assembled_source_dispositions(book)
    for e in book.elements:
        if needs_translation(e):
            if e.id not in translations:
                missing.append(e.id)
                add('missing_translation','待翻译或待校验',element_id=e.id,page=e.page)
            else:issues.extend(validate_translation(e,translations[e.id],book))
        for aid in TOKEN_RE.findall(e.text):
            if e.id not in assembled:expected_assets[aid]+=1
            if aid not in book.assets:add('missing_asset','公式对象不存在',element_id=e.id,page=e.page,asset_id=aid)
        for aid in e.asset_ids:
            if aid not in book.assets:add('missing_asset','原文对象不存在',element_id=e.id,page=e.page,asset_id=aid)
    asset_hashes={}
    for aid,a in book.assets.items():
        path=resolve_asset_path(book,a,outdir,pdf_path)
        if path is None:add('asset_file_missing','受保护资产文件缺失',asset_id=aid,page=a.page)
        else:
            if path not in asset_hashes:asset_hashes[path]=file_sha256(path)
            if a.sha256 and asset_hashes[path]!=a.sha256:add('asset_changed','受保护资产被修改',asset_id=aid,page=a.page)
    if layout_map is None and (outdir/'layout_map.json').exists():
        layout_map=json.loads((outdir/'layout_map.json').read_text(encoding='utf-8'))
    elif isinstance(layout_map,(str,Path)):
        layout_map=json.loads(Path(layout_map).read_text(encoding='utf-8'))
    panel_check={'status':'not_applicable'}
    if book.profile.get('source_design_panels_v1'):
        try:
            from .design_panels import check_exact_paint
            if not pdf_path or not Path(pdf_path).is_file() or not isinstance(layout_map,dict):
                raise ValueError('Reviewed source panels need the actual composed PDF and its layout map')
            traces=layout_map.get('source_design_panel_traces')
            if not isinstance(traces,list):
                raise ValueError('Reviewed source panel traces are missing')
            panel_check={'status':'passed',**check_exact_paint(book,pdf_path,traces)}
        except Exception as error:
            panel_check={'status':'failed','reason':str(error),'error_type':type(error).__name__}
            add('source_design_panel_check_failed','原书底色组合的实际成品检查未通过：'+str(error))
    render_revision='not_checked'
    if pdf_path and Path(pdf_path).is_file() and isinstance(layout_map,dict):
        if not layout_map.get('render_binding') or not layout_map.get('pdf_sha256'):
            render_revision='unverified'
            add('render_revision_unverified','缺少成品与当前原文、译文、版式的版本绑定记录；请重新排版',severity='warning')
        else:
            from .layout import rendering_binding
            current=rendering_binding(book,translations)
            changed=[key for key,value in current.items() if layout_map['render_binding'].get(key)!=value]
            if file_sha256(pdf_path)!=layout_map['pdf_sha256']:changed.append('pdf_sha256')
            render_revision='stale' if changed else 'current'
            if changed:add('stale_rendered_pdf','成品对应的原文、译文、资产或版式已经改变；请只重新排版',changed_fields=changed)
    page_count=0;page_sizes={}
    if pdf_path and Path(pdf_path).exists():
        import pymupdf
        with pymupdf.open(pdf_path) as pdf:
            page_count=len(pdf)
            for n,p in enumerate(pdf,1):
                page_sizes[n]=(p.rect.width,p.rect.height)
                words=p.get_text('words')
                for w in words:
                    r=pymupdf.Rect(w[:4])
                    if r.x0< -0.8 or r.y0< -0.8 or r.x1>p.rect.width+0.8 or r.y1>p.rect.height+0.8:
                        add('text_outside_page','文字超出页面',output_page=n,bbox=list(r),text=w[4][:30])
                for b in p.get_text('dict')['blocks']:
                    for l in b.get('lines',[]):
                        for s in l['spans']:
                            t=s['text']
                            if '\ufffd' in t:add('replacement_glyph','成品含替换字符',output_page=n)
                            peripheral = s['bbox'][1] > p.rect.height-book.profile.get('margins',{}).get('bottom',45)+4 or s['bbox'][3] < book.profile.get('margins',{}).get('top',45)-4
                            if not peripheral and any('\u4e00'<=ch<='\u9fff' for ch in t) and s['size']<book.profile.get('caption_size',8.5)-0.15:
                                add('tiny_chinese','中文小于固定角色字号下限',output_page=n,size=s['size'],text=t[:30])
                # Large intersections between independent words expose cross-column overwrites.
                ws=sorted(words,key=lambda w:w[1]);reported=0
                for i,w in enumerate(ws):
                    r1=pymupdf.Rect(w[:4])
                    if r1.get_area()<2:continue
                    for v in ws[i+1:]:
                        if v[1]>=r1.y1-0.5:break
                        r2=pymupdf.Rect(v[:4]);inter=r1&r2
                        if inter.is_empty or r2.get_area()<2:continue
                        if inter.get_area()>0.5*min(r1.get_area(),r2.get_area()) and inter.get_area()>8:
                            add('possible_overlap','文字区域明显重叠，需要视觉核对',severity='warning',output_page=n,text=w[4][:20]+' / '+v[4][:20]);reported+=1
                            if reported>=3:break
                    if reported>=3:break
    if layout_map:
        if book.profile.get("argument_units_v1") and pdf_path and Path(pdf_path).is_file():
            from .argument_qa import check_argument_layout
            from .layout import _page_geometry
            traces=layout_map.get("argument_traces", [])
            # The map stores argument and complete element traces as a fallback.
            tracefile=outdir/"typst-traces.json"
            if tracefile.is_file():traces=json.loads(tracefile.read_text(encoding="utf8"))
            argument_check=check_argument_layout(book,traces,_page_geometry(book),pdf_path)
            issues.extend(argument_check["issues"])
        issues.extend(layout_map.get('issues',[]))
        positions=layout_map.get('elements',{})
        assets=layout_map.get('assets',{})
        for category in ('elements','assets'):
            for identity,records in layout_map.get(category,{}).items():
                for record in records:
                    output_page=record.get('page')
                    if not page_sizes:continue
                    if output_page not in page_sizes:
                        add('rendered_position_page_missing','成品位置指向不存在的输出页',output_page=output_page,object_id=identity)
                        continue
                    try:
                        box=[float(v) for v in record['bbox']]
                        valid=len(box)==4 and all(math.isfinite(v) for v in box) and box[2]>=box[0] and box[3]>=box[1]
                    except (ValueError,TypeError,KeyError):valid=False
                    if not valid:
                        add('invalid_rendered_object_box','成品对象位置记录无效',output_page=output_page,object_id=identity)
                        continue
                    width,height=page_sizes[output_page]
                    if box[0]<-.8 or box[1]<-.8 or box[2]>width+.8 or box[3]>height+.8:
                        add('rendered_object_outside_page','实际测量的正文、边注或保护对象超出纸张边界，可能被裁切',
                            output_page=output_page,object_id=identity,object_kind=category,bbox=box)
        for e in book.elements:
            if e.id not in positions:add('not_rendered','元素缺少成品位置记录',element_id=e.id,page=e.page)
        for aid,count in expected_assets.items():
            if len(assets.get(aid,[]))<count:add('formula_not_rendered','受保护公式未全部出现在成品中',asset_id=aid,page=book.assets[aid].page if aid in book.assets else None)
        groups={}
        for e in book.elements:
            if e.group_id:
                groups.setdefault(e.group_id,[]).append(e)
        for gid,els in groups.items():
            if not any(e.kind in {'figure','table','caption'} for e in els):continue
            ps=[r['page'] for e in els for r in positions.get(e.id,[])]
            if ps and max(ps)-min(ps)>1:add('anchor_distance','图表与相关内容相隔超过一页',group_id=gid,source_pages=sorted({e.page for e in els}))
        for p in layout_map.get('pages',[]):
            if not p.get('source_pages'):add('source_label_missing','输出页缺少原书页码映射',output_page=p.get('page'))
        from .source_pages import check_output_source_labels
        issues.extend(check_output_source_labels(book,layout_map,pdf_path))
        from .cross_page_math import check_joined_math_geometry
        issues.extend(check_joined_math_geometry(book, assets))
    visual=_visual_summary(book,reason='缺少成品PDF或可用的实际位置记录，尚未执行像素核对。')
    if pdf_path and Path(pdf_path).is_file() and isinstance(layout_map,dict) and isinstance(layout_map.get('assets'),dict):
        try:
            pixel_result=compare_assets(book,layout_map,pdf_path,outdir)
            visual=_visual_summary(book,pixel_result)
            issues.extend(pixel_result['issues'])
        except Exception as error:
            visual['reason']='像素核对未完成：'+type(error).__name__
            add('visual_asset_checks_unavailable',visual['reason'],severity='warning')
    elif visual['eligible_source_assets']:
        add('visual_asset_checks_not_run',visual['reason'],severity='warning')
    if visual['excluded_source_assets']:
        add('visual_asset_scope_excluded','部分资产类型不适用当前像素核对，仍需人工检查',severity='warning',
            excluded_by_kind=visual['excluded_source_assets_by_kind'])
    # Deduplicate diagnostics; severity does not disappear in summary counts.
    unique={json.dumps(i,ensure_ascii=False,sort_keys=True,default=str):i for i in issues}
    issues=list(unique.values())
    errors=[i for i in issues if i.get('severity','warning')=='error']
    warnings=[i for i in issues if i.get('severity','warning')=='warning']
    report={'schema_version':2,'title':book.title,'source_pages':len(book.selected_pages),'output_pages':page_count,'elements':len(book.elements),'accepted_translations':len(translations),'missing_translations':len(missing),'protected_assets':len(book.assets),'technical_checks_passed':not errors,'acceptance_status':'needs_visual_and_semantic_review' if not errors else 'incomplete','visual_review':'protected_asset_pixels_only' if visual['status']!='not_run' else 'not_claimed','manual_visual_review':'not_claimed','visual_asset_checks':visual,'semantic_review':'see task diagnostics','errors':len(errors),'warnings':len(warnings),'issues':issues}
    report['render_revision']=render_revision
    report['source_design_panel_checks']=panel_check
    report['scan_source_checks']=scans
    report['native_source_glyph_checks']=native_source
    report['source_pagination_checks']=pagination
    report['assembled_source_elements']=list(assembled.values())
    if semantic_status is not None:
        report['semantic_review']=semantic_status
        report['semantic_review_passed']=semantic_status['passed']
    closed=[i for i in (resolved_issues or []) if i.get('severity','warning') in {'error','warning'}]
    report['resolved_issues']=closed
    report['resolved_issue_count']=len(closed)
    (outdir/'quality_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def evidence_links(entry):
        result=[]
        for key,label in [('source_png','原对象'),('target_png','成品对象')]:
            value=entry.get('evidence',{}).get(key)
            if value:
                path=Path(value)
                try:target=path.resolve().relative_to(outdir.resolve()).as_posix()
                except ValueError:continue
                result.append('<a href="'+html.escape(target,quote=True)+'">'+label+'</a>')
        return ' '.join(result)
    rows=''.join('<tr><td>'+html.escape(str(i.get('severity','warning')))+'</td><td>'+html.escape(str(i.get('page',i.get('output_page',''))))+'</td><td>'+html.escape(i.get('code',''))+'</td><td>'+html.escape(str(i.get('detail','')))+' '+evidence_links(i)+'</td><td>'+html.escape(str(i.get('element_id',i.get('asset_id',''))))+'</td></tr>' for i in issues)
    kind_names={'formula':'公式','code':'代码','figure':'图形','table':'表格','image':'图片','equation':'公式','math':'数学对象','drawing':'绘图','diagram':'示意图'}
    scope='、'.join(f'{kind_names.get(kind,kind)} {count}' for kind,count in visual['eligible_source_assets_by_kind'].items()) or '无适用对象'
    excluded='、'.join(f'{kind} {count}' for kind,count in visual['excluded_source_assets_by_kind'].items()) or '无'
    detail_link='<a href="visual_asset_report.json">逐对象指标与证据</a>' if visual.get('report_path') else ''
    occurrence_count=str(visual['eligible_occurrences']) if visual['eligible_occurrences'] is not None else '未知（尚未取得测量记录）'
    coverage_note='全部适用输出位置均已核对。' if visual['all_eligible_occurrences_checked'] else '适用位置核对尚未全部完成，跳过或未运行的对象不计为通过。'
    pixel_note=(f'本书资产共 {len(book.assets)} 个；本项适用 {visual["eligible_source_assets"]} 个（{html.escape(scope)}）。未纳入类型：{html.escape(excluded)}。'
                f'<br>另有 {visual["archived_source_assets"]} 个历史资产已说明由当前对象替代；保留来源，不计为像素通过，也不视为漏渲染。'
                f'<br>适用输出位置 {occurrence_count} 次；实际核对 {visual["checked_occurrences"]} 次，通过 {visual["passed_occurrences"]} 次；待复核 {visual["review_occurrences"]} 次，跳过 {visual["skipped"]} 次。'
                f'<br>{coverage_note}'
                f'<br>像素检查错误 {visual["errors"]} 项，人工复核提醒 {visual["review_warnings"]} 项。{detail_link}'
                '<br>核对范围为实际测量框内的可见墨迹；不能代替整页观感、相邻正文基线、颜色和语义的人工验收。')
    if visual.get('reason'):pixel_note+='<br>'+html.escape(visual['reason'])
    semantic_note='<p>尚未取得当前版本的语义复核覆盖记录。</p>'
    if semantic_status is not None:
        semantic_note=f"<div class='note'>需翻译 {semantic_status['required']} 段，当前有效 {semantic_status['accepted']} 段；分层选入复核 {semantic_status['selected']} 段，通过 {semantic_status['passed']}，待复核 {semantic_status['pending']}，未通过 {semantic_status['failed']}，待翻译 {semantic_status['awaiting_translation']}。<br>仅统计绑定当前原文、公式、译文与规则的结论；历史通过次数不计为当前覆盖。</div>"
    scan_section=''
    if scans['scanned_pages']:
        scan_rows=''.join('<tr><td>'+html.escape(str(p['page']))+'</td><td>'+html.escape(str(p['ocr_units'] if p['ocr_units'] is not None else '未知'))+'</td><td>'+html.escape(str(p['foreground_pixels'] if p['foreground_pixels'] is not None else '未知'))+'</td><td>'+html.escape(str(p['unassigned_ink_pixels'] if p['unassigned_ink_pixels'] is not None else '未知'))+'</td><td>'+html.escape(str(p['low_confidence_ocr_units'] if p['low_confidence_ocr_units'] is not None else '未知'))+'</td><td>'+html.escape(p['source_image_status'])+'</td></tr>' for p in scans['pages'])
        scan_section=(f"<h2>扫描来源检查</h2><div class='note'>扫描来源 {scans['scanned_pages']} 页；有有效墨迹证据 {scans['pages_with_quality_evidence']} 页，源图像素哈希已核对 {scans['source_images_verified']} 页；OCR单元 {scans['total_ocr_units']} 个，低置信 {scans['low_confidence_ocr_units']} 个。"
                      '<br>墨迹归属不证明OCR准确；原PDF字形为0不代表识别完整。CTC框为估计位置，置信度不是实测准确率。未声明光学识别准确率或全书验收通过。</div>'
                      '<table><tr><th>原PDF页</th><th>OCR单元</th><th>可见墨迹像素</th><th>未归属墨迹</th><th>低置信单元</th><th>原图证据</th></tr>'+scan_rows+'</table>')
    closed_rows=''.join('<tr><td>'+html.escape(str(i.get('page','')))+'</td><td>'+html.escape(str(i.get('code','')))+'</td><td>'+html.escape(str(i.get('detail','')))+'</td></tr>' for i in closed)
    closed_section=f'<h2>已修复或经复核关闭的记录（{len(closed)}）</h2><p>旧候选和诊断仍保存在任务数据库；下列问题已由当前有效译文或绑定版本的复核结果关闭。</p><table><tr><th>原页</th><th>类型</th><th>原问题</th></tr>{closed_rows}</table>'
    body=f'''<!doctype html><html lang="zh"><meta charset="utf-8"><title>质量检查报告</title><style>body{{font-family:system-ui;max-width:1100px;margin:40px auto;padding:0 20px;color:#223}}h1{{color:#087da6}}table{{border-collapse:collapse;width:100%}}td,th{{text-align:left;border-bottom:1px solid #ddd;padding:9px}}.note{{background:#eef6f8;padding:18px;line-height:1.8}}</style><h1>{html.escape(book.title)}：质量报告</h1><div class="note">原书 {len(book.selected_pages)} 页，成品 {page_count} 页。待处理错误 {len(errors)} 项，提醒 {len(warnings)} 项。<br>此报告证明已运行的结构和成品检查；全书视觉与语义验收另行记录，不能以生成成功代替。</div>{scan_section}<h2>受保护对象像素核对</h2><div class="note">{pixel_note}</div><h2>当前版本语义复核</h2>{semantic_note}<h2>问题目录</h2><table><tr><th>级别</th><th>页</th><th>类型</th><th>说明</th><th>元素</th></tr>{rows}</table>{closed_section}</html>'''
    (outdir/'质量报告.html').write_text(body,encoding='utf-8')
    return report
