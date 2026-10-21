"""Local image-first parsing for raster and hidden-OCR pages.

OCR text is a hypothesis, never a native glyph ledger. Original PDF image
composition remains the authority for mathematical and visual objects.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import gzip
import json
import math
from pathlib import Path
import re
import statistics
import time

import numpy as np
import pymupdf

from .models import Asset, Book, Element, TOKEN_RE, digest, file_sha256

SCAN_SCHEMA = 'scan-regions-v1'
_PROTECTED = {'formula_inline', 'formula_display', 'figure', 'table', 'code'}
_MATH = {'formula_inline', 'formula_display'}
_LATIN_WORD = re.compile(r'[A-Za-z]{4,}')
_CODE = re.compile(r'\b(?:void|int|bool|const|vector|class|struct|template)\b.*[;({<]|\b(?:for|while|if)\s*\(|\breturn\b.*;|[{}]\s*$')


def scan_pages(pdf_path, pages=None):
    """Detect actual raster/hidden OCR without relying on bookmarks or labels."""
    result=[]
    with pymupdf.open(pdf_path) as doc:
        for n in (range(1,len(doc)+1) if pages is None else pages):
            page=doc[n-1]
            images=page.get_image_info()
            visible_unrotated=page.rect*page.derotation_matrix
            full=any((pymupdf.Rect(im['bbox']) & visible_unrotated).get_area()>=visible_unrotated.get_area()*.78 for im in images)
            text=page.get_text('dict')
            spans=[s for b in text['blocks'] for l in b.get('lines',[]) for s in l.get('spans',[])]
            glyphless=any('glyphless' in s.get('font','').lower() for s in spans)
            if full or glyphless:
                result.append(n)
            elif not any(s.get('text','').strip() for s in spans):
                # Blank pages do not require a model. Outlined text still does.
                pix=page.get_pixmap(matrix=pymupdf.Matrix(.5,.5),colorspace=pymupdf.csGRAY,alpha=False)
                if np.count_nonzero(np.frombuffer(pix.samples,dtype=np.uint8)<230)>4:result.append(n)
    return result


def _box(value,width=None,height=None):
    if len(value)!=4 or not all(math.isfinite(float(v)) for v in value):raise ValueError('Invalid scan region coordinates')
    x0,y0,x1,y1=map(float,value)
    if width is not None:x0,x1=max(0,x0),min(width,x1)
    if height is not None:y0,y1=max(0,y0),min(height,y1)
    if x1<=x0 or y1<=y0:raise ValueError('Empty scan region')
    return [x0,y0,x1,y1]


def _area(r):return max(0,r[2]-r[0])*max(0,r[3]-r[1])
def _inter(a,b):return max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
def _union(values):
    return [min(r[0] for r in values),min(r[1] for r in values),max(r[2] for r in values),max(r[3] for r in values)]
def _center(r):return ((r[0]+r[2])/2,(r[1]+r[3])/2)
def _covered(r,container,fraction=.5):return _inter(r,container)>=_area(r)*fraction


def _issue(book,code,detail,page,severity='warning',**kw):
    book.issues.append(dict(code=code,detail=detail,page=page,severity=severity,**kw))


def _json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temp.replace(path)


def _cache(path,signature,run):
    if path.is_file():
        try:
            data=json.loads(path.read_text(encoding='utf-8'))
            if data.get('signature')==signature:return data['result']
        except (ValueError,KeyError,OSError):pass
    value=run();_json(path,{'signature':signature,'result':value});return value


def _lines(value,width,height):
    result=[]
    for i,row in enumerate(value.get('lines',[])):
        text=row.get('text','')
        if not isinstance(text,str) or not text.strip():continue
        bbox=_box(row['bbox'],width,height)
        confidence=row.get('confidence')
        if not isinstance(confidence,(float,int)) or not 0<=confidence<=1:confidence=0.0
        words=[]
        for j,word in enumerate(row.get('words',[])):
            if not str(word.get('text','')).strip():continue
            words.append({'id':f'l{i:04d}-w{j:04d}','text':str(word['text']),
                          'bbox':_box(word['bbox'],width,height),'confidence':float(word.get('confidence',confidence))})
        if not words:words=[{'id':f'l{i:04d}-w0000','text':text,'bbox':bbox,'confidence':confidence}]
        result.append({'id':f'l{i:04d}','text':text,'bbox':bbox,'confidence':confidence,'words':words})
    return result


def _select_regions(detections,raw,width,height):
    """Retain complete original objects; overlapping detectors are evidence."""
    regions=[]
    for i,item in enumerate(detections['regions']):
        kind=item['kind']
        if kind not in _PROTECTED or item.get('confidence',0)<.25:continue
        bbox=_box(item['bbox'],width,height)
        words=[line['text'] for line in raw if _covered(line['bbox'],bbox,.6)]
        if kind in {'figure','table'} and sum(bool(_CODE.search(t)) for t in words)>=2:kind='code'
        if kind in _MATH and item.get('detector')=='layout':
            natural=[w for t in words for w in _LATIN_WORD.findall(t) if w.lower() not in {'frac','left','right','sqrt','lim','log','sin','cos','tan'}]
            if len(natural)>1:continue
        regions.append({**item,'id':f'r{i:04d}','kind':kind,'bbox':bbox,'evidence_ids':[item.get('id',f'r{i:04d}')]})
    # A layout display box may supply a missed prefix or vertical ellipsis.
    # Union connected mathematical proposals, never discard it merely because
    # an MFD proposal overlaps. Non-overlapping equations stay separate.
    math_regions=[r for r in regions if r['kind'] in _MATH];merged=[]
    while math_regions:
        current=math_regions.pop(0);changed=True
        while changed:
            changed=False
            for other in math_regions[:]:
                if _inter(current['bbox'],other['bbox'])>.1*min(_area(current['bbox']),_area(other['bbox'])):
                    current={**current,'bbox':_union([current['bbox'],other['bbox']]),
                             'kind':'formula_display' if 'formula_display' in {current['kind'],other['kind']} else 'formula_inline',
                             'detector':'combined-original-math','evidence_ids':current['evidence_ids']+other['evidence_ids']}
                    math_regions.remove(other);changed=True
        merged.append(current)
    kept=[r for r in regions if r['kind'] not in _MATH]
    kept += [r for r in merged if not any(_covered(r['bbox'],k['bbox'],.85) for k in kept)]
    # Code is protected as a complete layout block, including comment-only and
    # declaration lines which a keyword-only per-line detector would omit.
    for i,item in enumerate(detections['regions']):
        if item['kind']!='text':continue
        bbox=_box(item['bbox'],width,height)
        lines=[l for l in raw if _covered(l['bbox'],bbox,.7)]
        seeds=[l for l in lines if _CODE.search(l['text'])]
        if len(seeds)<3 or len(seeds)<len(lines)*.18:continue
        if any(k['kind']=='code' and _covered(bbox,k['bbox'],.85) for k in kept):continue
        kept=[r for r in kept if not _covered(r['bbox'],bbox,.85)]
        kept.append({'id':f'code{i:04d}','kind':'code','bbox':bbox,'confidence':min(l['confidence'] for l in seeds),
                     'detector':'ocr-code-layout-block','evidence_ids':[item.get('id',str(i))]})
    # Fallback clusters require horizontal overlap as well as vertical proximity.
    seeds=[l for l in raw if _CODE.search(l['text']) and not any(_covered(l['bbox'],r['bbox']) for r in kept)]
    clusters=[]
    for line in sorted(seeds,key=lambda l:(l['bbox'][1],l['bbox'][0])):
        match=next((g for g in reversed(clusters) if 0<=line['bbox'][1]-g[-1]['bbox'][3]<max(30,(line['bbox'][3]-line['bbox'][1])*3)
                    and min(line['bbox'][2],g[-1]['bbox'][2])>max(line['bbox'][0],g[-1]['bbox'][0])),None)
        if match is not None:match.append(line)
        else:clusters.append([line])
    for i,group in enumerate(clusters):
        if len(group)<3:continue
        b=_union([line['bbox'] for line in group]);pad=3
        kept=[r for r in kept if r['kind'] not in _MATH or not _covered(r['bbox'],b,.85)]
        kept.append({'id':f'fallback-code{i:04d}','kind':'code','bbox':_box([b[0]-pad,b[1]-pad,b[2]+pad,b[3]+pad],width,height),
                     'confidence':min(l['confidence'] for l in group),'detector':'ocr-code-geometry'})
    for line in raw:
        for word in line['words']:
            text=word['text'].strip('.,;:!?()')
            if not re.fullmatch(r'[b-zB-HJ-Z]',text) or line['confidence']<.97:continue
            bbox=word['bbox'];h=bbox[3]-bbox[1]
            if bbox[2]-bbox[0]>h*1.6 or word['confidence']<.95:continue
            if any(_inter(bbox,r['bbox'])>0 for r in kept):continue
            kept.append({'id':f"variable-{word['id']}",'kind':'formula_inline','bbox':bbox[:],
                         'confidence':word['confidence'],'detector':'ocr-single-variable-review-required'})
    # `log N` is one mathematical object, even if MFD only found N.
    for r in kept:
        if r['kind']!='formula_inline':continue
        for line in raw:
            for word in line['words']:
                if word['text'].strip() not in {'log','sin','cos','tan','ln','lg'}:continue
                b=word['bbox'];h=b[3]-b[1]
                if 0<=r['bbox'][0]-b[2]<h*.85 and abs(_center(b)[1]-_center(r['bbox'])[1])<h*.7:
                    r['bbox']=_union([r['bbox'],b]);r['detector']='math-function-original-crop'
    for r in kept:
        x0,y0,x1,y1=r['bbox'];pad=2
        r['bbox']=_box([x0-pad,y0-pad,x1+pad,y1+pad],width,height)
    return sorted(kept,key=lambda r:(r['bbox'][1],r['bbox'][0]))


def _whole_ink_component(image,seed):
    """Recover a complete component, expanding if the candidate touches a crop.

    A short CTC seed or clipped comma tail is never treated as a full glyph.
    """
    import cv2
    height,width=image.shape[:2];cx,cy=_center(seed)
    padding=max(8.,max(seed[2]-seed[0],seed[3]-seed[1])*.6)
    chosen=None
    for attempt in range(9):
        x0,y0,x1,y1=map(int,_box([seed[0]-padding,seed[1]-padding,seed[2]+padding,seed[3]+padding],width,height))
        gray=cv2.cvtColor(image[y0:y1,x0:x1],cv2.COLOR_RGB2GRAY)
        foreground=(gray<min(200,float(np.percentile(gray,85))-25)).astype(np.uint8)
        count,_,stats,_=cv2.connectedComponentsWithStats(foreground,8)
        candidates=[]
        for i in range(1,count):
            x,y,w,h,area=stats[i];box=[x0+int(x),y0+int(y),x0+int(x+w),y0+int(y+h)]
            if area<3 or _inter(box,seed)<=0:continue
            score=abs(_center(box)[0]-cx)+abs(_center(box)[1]-cy)*.3
            candidates.append((score,box,(x,y,w,h)))
        if not candidates:return chosen or seed
        _,chosen,(x,y,w,h)=min(candidates,key=lambda v:v[0])
        cut=(x==0 and x0>0) or (y==0 and y0>0) or (x+w>=x1-x0 and x1<width) or (y+h>=y1-y0 and y1<height)
        if not cut:return chosen
        padding*=2
    raise ValueError('Ink component still crosses the expanded source region; manual boundary review required')


def _detach_prose_punctuation(regions,raw,image):
    """Keep ordinary trailing prose punctuation outside immutable math crops.

    Both OCR suffix evidence and a separate small rightmost ink component are
    required. Commas inside tuples and tall mathematical delimiters stay intact.
    """
    import cv2
    height,width=image.shape[:2]
    words=[w for line in raw for w in line['words']]
    for region in regions:
        if region['kind']!='formula_inline':continue
        b=region['bbox'];h=b[3]-b[1]
        candidates=[w for w in words if re.search(r'[.,;:]$',w['text'].strip()) and _inter(w['bbox'],b)>0
                    and abs(w['bbox'][2]-b[2])<h*.65]
        if not candidates:continue
        word=min(candidates,key=lambda w:abs(w['bbox'][2]-b[2]));punct=word['text'].strip()[-1]
        x0,y0,x1,y1=map(int,b);gray=cv2.cvtColor(image[y0:y1,x0:x1],cv2.COLOR_RGB2GRAY)
        foreground=(gray<min(200,float(np.percentile(gray,85))-25)).astype(np.uint8)
        count,_,stats,_=cv2.connectedComponentsWithStats(foreground,8)
        parts=[]
        for i in range(1,count):
            x,y,w,hh,area=stats[i]
            if area<3:continue
            parts.append((int(x),int(y),int(w),int(hh)))
        if not parts:continue
        rightmost=max(parts,key=lambda v:v[0]+v[2]);x,y,w,hh=rightmost
        if hh>h*.32 or y+hh<h*.65 or x<(b[2]-b[0])*.6:continue
        if any(px>x+1 and ph>h*.32 for px,py,pw,ph in parts):continue
        cut=x0+x-1
        if cut<=b[0]+3:continue
        region['bbox_before_punctuation_detach']=b[:]
        region['bbox']=[b[0],b[1],float(cut),b[3]]
        punct_box=[x0+x,y0+y,x0+x+w,y0+y+hh]
        # A detector boundary may itself cut the tiny dot. Recover its complete
        # connected ink component from the original image, keeping QA's2px
        # tolerance unchanged.
        punct_box=_whole_ink_component(image,punct_box)
        region['detached_prose_punctuation']={'text':punct,'bbox':punct_box,
                                             'basis':'OCR-suffix-and-separate-whole-rightmost-ink','source_ocr_confidence':word['confidence']}
    return regions


def _refine_variable_regions(regions,image):
    """Expand estimated single-variable seeds to their entire original ink."""
    height,width=image.shape[:2]
    for region in regions:
        if region.get('detector')!='ocr-single-variable-review-required':continue
        b=region['bbox'];chosen=_whole_ink_component(image,b)
        region['ctc_bbox_before_refinement']=b
        region['bbox']=_box([chosen[0]-2,chosen[1]-2,chosen[2]+2,chosen[3]+2],width,height)
        region['boundary_method']='estimated-ctc-to-nearest-whole-ink-component'
    return regions


def _page_label(raw,width,height,override=None):
    if override is not None:return str(override),'reviewed-page-evidence',1.0
    candidates=[]
    for line in raw:
        x0,y0,x1,y1=line['bbox'];text=line['text'].strip()
        if not (y1<height*.095 or y0>height*.91):continue
        if re.fullmatch(r'\d{1,4}|[ivxlcdmIVXLCDM]{1,9}',text) and line['confidence']>=.85:
            candidates.append((text,line))
    if len(candidates)==1:return candidates[0][0],'scan-ocr-margin',candidates[0][1]['confidence']
    edge=[c for c in candidates if c[1]['bbox'][0]<width*.2 or c[1]['bbox'][2]>width*.8]
    if len(edge)==1:return edge[0][0],'scan-ocr-margin',edge[0][1]['confidence']
    return None,'absent' if not candidates else 'ambiguous',0.0


def _asset(book,source_doc,page,outdir,number,region,scale_x,scale_y,baseline=None):
    pixel=region['bbox'];bbox=[pixel[0]*scale_x,pixel[1]*scale_y,pixel[2]*scale_x,pixel[3]*scale_y]
    kind='formula' if region['kind'] in _MATH else region['kind']
    aid=f'p{number:04d}-scan-{kind}-{digest([book.source_sha256,bbox])[:14]}'
    bundle=book._scan_bundle
    target=bundle.new_page(width=bbox[2]-bbox[0],height=bbox[3]-bbox[1])
    private=pymupdf.open();private.insert_pdf(source_doc,from_page=0,to_page=0)
    private[0].clip_to_rect(pymupdf.Rect(bbox));target.show_pdf_page(target.rect,private,0,clip=pymupdf.Rect(bbox));private.close()
    metric=(baseline-pixel[1])*scale_y if baseline is not None else (bbox[3]-bbox[1])*.8
    metric=max(.1,min(bbox[3]-bbox[1],metric))
    asset=Asset(aid,kind,f'assets/scan-p{number:04d}.pdf',number,bbox,bbox[2]-bbox[0],bbox[3]-bbox[1],metric,'','',
                {'format':'pdf','vector':False,'source_is_scan':True,'original_page_composition_preserved':True,
                 'pdf_page':len(bundle)-1,'baseline_from_top':metric,'baseline_method':'neighbor-ocr-line-estimate' if baseline is not None else 'object-estimate',
                 'source_sha256':book.source_sha256,'source_pixel_bbox':pixel,'detector':region.get('detector'),
                 'detection_confidence':region.get('confidence'),'untranscribed_original_math':kind=='formula'})
    book.assets[aid]=asset;region['asset_id']=aid;return asset


def _row_text(items):
    result='';previous=None
    for item in sorted(items,key=lambda t:t['bbox'][0]):
        text=item['text']
        if previous is not None:
            gap=item['bbox'][0]-previous['bbox'][2]
            charlike=len(text)==len(previous['text'])==1
            h=min(item['bbox'][3]-item['bbox'][1],previous['bbox'][3]-previous['bbox'][1])
            if not charlike or gap>h*.22:result+=' '
        result+=text;previous=item
    return result.strip()


def _joinable(a,b,regions,layout_regions=()):
    left,right=(a,b) if a[0]<b[0] else (b,a)
    gap=right[0]-left[2];h=min(a[3]-a[1],b[3]-b[1])
    shared=any(r['kind'] in {'text','heading','caption'} and _covered(a,r['bbox'],.9) and _covered(b,r['bbox'],.9) for r in layout_regions)
    # A coarse layout box can cover two columns. Geometry alone does not
    # prove that independent OCR lines form one sentence.
    if shared and gap<h*.18:return True
    bridge=[r['bbox'] for r in regions if r['kind']=='formula_inline' and left[2]-h<r['bbox'][0]<right[0] and
            left[2]<r['bbox'][2]<right[0]+h and abs(_center(r['bbox'])[1]-_center(a)[1])<h]
    return bool(bridge and min(r[0] for r in bridge)-left[2]<h*1.8 and right[0]-max(r[2] for r in bridge)<h*1.8)


def _text_rows(lines,regions,width,height,layout_regions=()):
    rows=[]
    for line in lines:
        if any(_covered(line['bbox'],r['bbox'],.8) for r in regions if r['kind'] in {'figure','table','code'}):continue
        words=[w for w in line['words'] if not any(_covered(w['bbox'],r['bbox'],.5) for r in regions)]
        if not words:continue
        b=line['bbox'];mid=(b[1]+b[3])/2
        match=next((r for r in rows if abs(r['mid']-mid)<min(r['height'],b[3]-b[1])*.4 and _inter([r['bbox'][0],b[1],r['bbox'][2],b[3]],b)==0 and not (re.match(r'^\s*(?:\d+\.|[a-z]\))\s',_row_text(r['items'])) and re.match(r'^\s*(?:\d+\.|[a-z]\))\s',line['text'])) and _joinable(r['bbox'],b,regions,layout_regions)),None)
        if match:
            match['items']+=words;match['bbox']=_union([match['bbox'],b]);match['source_lines'].append(line['id']);match['confidence']=min(match['confidence'],line['confidence'])
        else:rows.append({'bbox':b,'mid':mid,'height':b[3]-b[1],'items':words[:],'source_lines':[line['id']],'confidence':line['confidence']})
    for region in regions:
        if region['kind']!='formula_inline':continue
        b=region['bbox'];middle=(b[1]+b[3])/2
        manual=next((r for r in rows if any(f"⟦F:{region['asset_id']}⟧" in item['text'] for item in r['items'])),None)
        if manual is not None:region['row']=manual;continue
        available=[r for r in rows if r['bbox'][1]-r['height']*.35<middle<r['bbox'][3]+r['height']*.35 and max(r['bbox'][0]-b[2],b[0]-r['bbox'][2],0)<r['height']*2.5]
        if available:
            row=min(available,key=lambda r:abs(r['mid']-middle)+max(r['bbox'][0]-b[2],b[0]-r['bbox'][2],0)*.1)
            region['row']=row;row['items'].append({'text':f"⟦F:{region['asset_id']}⟧",'bbox':b,'confidence':1.0,'asset_id':region['asset_id']})
            row['bbox']=_union([row['bbox'],b])
    for region in regions:
        punctuation=region.get('detached_prose_punctuation');row=region.get('row')
        if not punctuation or row is None:continue
        token=f"⟦F:{region['asset_id']}⟧";mark=punctuation['text']
        present=any((token in item['text'] and re.search(re.escape(token)+r'\s*'+re.escape(mark),item['text']))
                    or (token not in item['text'] and mark in item['text'] and _inter(item['bbox'],punctuation['bbox'])>0) for item in row['items'])
        if not present:row['items'].append({'text':mark,'bbox':punctuation['bbox'],'confidence':punctuation.get('source_ocr_confidence',0.)})
    for row in rows:row['text']=_row_text(row['items'])
    return rows


def _build_page(book,doc,number,outdir,dpi,correction):
    from .ocr_engine import recognize,engine_identity
    from .scan_detectors import detect_regions,detector_identity
    from .scan_quality import audit_scan_page
    started=time.perf_counter();original=doc[number-1]
    source=pymupdf.open();source.insert_pdf(doc,from_page=number-1,to_page=number-1)
    if source[0].rotation:source[0].remove_rotation()
    source.bake(annots=True,widgets=True)
    page=source[0]
    if math.ceil(page.rect.width*dpi/72)*math.ceil(page.rect.height*dpi/72)>40_000_000:raise ValueError('Scan page exceeds 40 megapixels; lower --ocr-dpi to bound local memory')
    pix=page.get_pixmap(matrix=pymupdf.Matrix(dpi/72,dpi/72),colorspace=pymupdf.csRGB,alpha=False,annots=True)
    image=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,3).copy()
    image_hash=digest(image.tobytes());folder=outdir/'scan';folder.mkdir(exist_ok=True)
    image_path=folder/f'p{number:04d}.png';pix.save(image_path)
    width,height=pix.width,pix.height;page_width,page_height=page.rect.width,page.rect.height;sx,sy=page_width/width,page_height/height
    if correction:
        if correction.get('image_sha256')!=image_hash:raise ValueError(f'Scan corrections image fingerprint changed on page {number}')
        if not correction.get('reviewed') or not str(correction.get('reason','')).strip():raise ValueError('Scan corrections need reviewed=true and a reason')
    ident={'ocr':engine_identity(),'detectors':detector_identity(),'image':image_hash,'dpi':dpi,'schema':SCAN_SCHEMA}
    detections=_cache(folder/f'p{number:04d}.detection.json',digest(ident),lambda:detect_regions(image))
    raw_value=_cache(folder/f'p{number:04d}.raw-ocr.json',digest(ident),lambda:recognize(image))
    raw=_lines(raw_value,width,height)
    regions=_detach_prose_punctuation(_refine_variable_regions(_select_regions(detections,raw,width,height),image),raw,image)
    if correction.get('regions') is not None:
        regions=[]
        for i,r in enumerate(correction['regions']):
            if r.get('kind') not in _PROTECTED:raise ValueError('Unknown corrected protection region kind')
            bbox=_box(r['bbox'],width,height)
            if bbox!=list(r['bbox']):raise ValueError('Reviewed region extends outside source image')
            regions.append({**r,'id':r.get('id',f'reviewed-r{i:04d}'),'bbox':bbox,'detector':'reviewed-region','confidence':1.0})
        if len({r['id'] for r in regions})!=len(regions):raise ValueError('Duplicate reviewed region IDs')
    masked=image.copy()
    for r in regions:
        x0,y0,x1,y1=r['bbox'];masked[max(0,int(y0)-1):min(height,math.ceil(y1)+1),max(0,int(x0)-1):min(width,math.ceil(x1)+1)]=255
    masked_id=digest([ident,[{k:r[k] for k in ('id','kind','bbox')} for r in regions]])
    prose_value=_cache(folder/f'p{number:04d}.prose-ocr.json',masked_id,lambda:recognize(masked))
    prose=_lines(prose_value,width,height)
    line_map_hash=digest({'engine_signature':prose_value.get('engine',{}).get('signature'),
                          'lines':[{k:l[k] for k in ('id','text','bbox')} for l in prose],
                          'regions':[{k:r[k] for k in ('id','kind','bbox')} for r in regions]})
    if (correction.get('line_text') or correction.get('line_kinds')) and correction.get('line_map_sha256')!=line_map_hash:
        raise ValueError('Scan correction line map changed; review the current OCR lines before applying edits')
    edits=correction.get('line_text',{})
    for line in prose:
        if line['id'] in edits:
            line['text']=edits[line['id']];line['confidence']=1.0
            line['words']=[{'id':line['id']+'-reviewed','text':line['text'],'bbox':line['bbox'],'confidence':1.0}]
    unknown=set(edits)-{l['id'] for l in prose}
    if unknown:raise ValueError('Corrections reference unknown OCR lines: '+str(sorted(unknown)))
    printed,label_source,label_confidence=_page_label(raw,width,height,correction.get('printed_page'))
    book._scan_bundle=pymupdf.open()
    for region in regions:
        baseline=None
        if region['kind']=='formula_inline' and prose:
            center=_center(region['bbox'])[1];nearest=min(prose,key=lambda l:abs(_center(l['bbox'])[1]-center))
            if abs(_center(nearest['bbox'])[1]-center)<(nearest['bbox'][3]-nearest['bbox'][1])*1.2:
                baseline=nearest['bbox'][3]-(nearest['bbox'][3]-nearest['bbox'][1])*.15
        _asset(book,source,page,outdir,number,region,sx,sy,baseline)
    region_map={r['id']:r for r in regions}
    for line in prose:
        if line['id'] in edits:
            def reviewed_token(match):
                if match[1] not in region_map:raise ValueError('Reviewed line references unknown protected region '+match[1])
                return f"⟦F:{region_map[match[1]]['asset_id']}⟧"
            line['text']=re.sub(r'⟦R:([^⟧]+)⟧',reviewed_token,line['text'])
            line['words']=[{'id':line['id']+'-reviewed','text':line['text'],'bbox':line['bbox'],'confidence':1.0}]
    for line in prose:
        if line['id'] in edits:continue
        for word in line['words']:
            for region in regions:
                if region['kind']!='formula_inline':continue
                b=region['bbox'];w=word['bbox']
                if w[0]<b[0] and w[2]>b[2] and _inter(w,b)>0 and len(word['text'])>2:
                    _issue(book,'scan_inline_word_crossing','遮罩后的OCR词跨越行内公式，须核对分词并以区域标记修正',number,'error',line_id=line['id'],region_id=region['id'],word=word['text'])
    rows=_text_rows(prose,regions,width,height,detections['regions'])
    median_height=statistics.median(r['height'] for r in rows) if rows else 20
    left=[r for r in rows if r['bbox'][0]<width*.28 and r['bbox'][2]<width*.52]
    right=[r for r in rows if width*.43<r['bbox'][0]<width*.63 and r['bbox'][2]-r['bbox'][0]<width*.5]
    columns=2 if len(left)>=3 and len(right)>=3 else 1
    entries=[];units=[];processed=[]
    for row in rows:
        b=row['bbox'];text=row['text']
        peripheral=b[3]<height*.085 or b[1]>height*.925
        pure_label=printed is not None and text.strip()==printed
        label_lines=[l for l in raw if printed is not None and l['text'].strip()==printed and (l['bbox'][3]<height*.095 or l['bbox'][1]>height*.91)]
        header_evidence=peripheral and label_lines and any(abs(_center(b)[1]-_center(l['bbox'])[1])<median_height*.55 for l in label_lines)
        if pure_label and peripheral:kind='page_number'
        elif header_evidence:kind='running_header' if b[3]<height*.085 else 'running_footer'
        elif any(r['kind']=='heading' and _covered(b,r['bbox'],.55) for r in detections['regions']):kind='heading'
        elif re.match(r'^(?:Figure|Table)\s+\d',text,re.I) or any(r['kind']=='caption' and _covered(b,r['bbox'],.55) for r in detections['regions']):kind='caption'
        else:kind='paragraph'
        reviewed_kinds=correction.get('line_kinds',{})
        override_kinds={reviewed_kinds[lid] for lid in row['source_lines'] if lid in reviewed_kinds}
        if len(override_kinds)>1:raise ValueError('Conflicting reviewed line roles')
        if override_kinds:
            kind=override_kinds.pop()
            if kind not in {'paragraph','heading','exercise','caption','footnote','running_header','running_footer','page_number'}:raise ValueError('Invalid reviewed line role')
        if kind=='paragraph' and re.match(r'^\d{1,3}\.\s+[A-Za-z]',text):kind='exercise'
        column=int(_center(b)[0]>width/2) if columns==2 else 0
        key=None
        for i,r in enumerate(detections['regions']):
            if r['kind'] in {'text','heading','caption'} and _covered(b,r['bbox'],.55):key=(i,column,kind);break
        entries.append({'bbox':b,'text':text,'kind':kind,'column':column,'key':key,'rows':[row]})
    # Merge only adjacent lines within the same verified region/column and role.
    grouped=[]
    for entry in sorted(entries,key=lambda e:(e['column'],e['bbox'][1],e['bbox'][0])):
        prev=grouped[-1] if grouped else None
        can_join=prev and entry['kind'] in {'paragraph','heading'} and prev['kind']==entry['kind'] and prev['column']==entry['column'] and entry['key'] is not None and entry['key']==prev['key'] and not re.match(r'^\s*[•●]\s',entry['text']) and entry['bbox'][1]-prev['bbox'][3]<median_height*1.5
        if can_join:
            if prev['text'].endswith('-') and entry['text'][:1].islower():prev['text']=prev['text'][:-1]+entry['text']
            else:prev['text']+=' '+entry['text']
            prev['bbox']=_union([prev['bbox'],entry['bbox']]);prev['rows']+=entry['rows']
        else:grouped.append(entry)
    elements=[]
    for entry in grouped:
        b=entry['bbox'];bbox=[b[0]*sx,b[1]*sy,b[2]*sx,b[3]*sy];text=entry['text'];kind=entry['kind']
        eid=f'p{number:04d}-scan-e-{digest([book.source_sha256,bbox,text])[:14]}'
        aid=TOKEN_RE.findall(text)
        metadata={'source_is_scan':True,'ocr_line_ids':[x for row in entry['rows'] for x in row['source_lines']],
                  'ocr_confidence':min(row['confidence'] for row in entry['rows']),
                  'ocr_engine_identity':digest(ident),'correction_hash':digest(correction) if correction else None}
        style={'columns':columns,'column':entry['column'],'source_size':median_height*sy*.8,'level':2}
        elements.append(Element(eid,kind,text,number,bbox,printed,style,aid,eid,'',metadata))
        if metadata['ocr_confidence']<.75 and kind not in {'page_number','running_header','running_footer'}:
            _issue(book,'scan_source_low_confidence','原图正文低置信度，须核对并修正OCR后再翻译',number,'error',element_id=eid)
        if kind in {'heading','exercise'}:metadata['list_label']=re.match(r'^(\d+(?:\.\d+)*)',text).group(1) if re.match(r'^\d',text) else None
        for row in entry['rows']:
            processed.append({'bbox':row['bbox'],'owner':eid,'kind':'text'})
            for word in row['items']:
                if 'asset_id' not in word:units.append({**word,'owner':eid,'disposition':'ocr-translation-source' if kind not in {'page_number','running_header','running_footer'} else 'excluded-peripheral'})
    for region in regions:
        aid=region['asset_id'];processed.append({'bbox':region['bbox'],'owner':aid,'kind':region['kind']})
        if region.get('row') is not None:continue
        asset=book.assets[aid];kind=asset.kind
        eid=f'p{number:04d}-scan-visual-{digest([aid,kind])[:14]}'
        elements.append(Element(eid,kind,'',number,asset.bbox,printed,{'columns':columns,'column':int(_center(region['bbox'])[0]>width/2) if columns==2 else 0},[aid],eid,'',{'source_is_scan':True,'preserve_only':True}))
    for visual in [e for e in elements if e.kind in {'figure','table','code'}]:
        candidates=[e for e in elements if e.kind=='caption' and min(abs(e.bbox[1]-visual.bbox[3]),abs(e.bbox[3]-visual.bbox[1]))<60
                    and min(e.bbox[2],visual.bbox[2])>max(e.bbox[0],visual.bbox[0])]
        if candidates:
            caption=min(candidates,key=lambda e:abs(e.bbox[1]-visual.bbox[3]));caption.group_id=visual.group_id
            caption.metadata['anchor_id']=visual.id;visual.metadata['caption_id']=caption.id
    if columns==1:elements.sort(key=lambda e:(e.bbox[1],e.bbox[0]))
    else:
        # Whole-width headings delimit reading-order bands; each band reads
        # left column top-to-bottom before its right column.
        spans=sorted([e for e in elements if e.bbox[2]-e.bbox[0]>page.rect.width*.62],key=lambda e:e.bbox[1])
        rest=[e for e in elements if e not in spans];ordered=[];boundary=0
        for span in spans:
            band=[e for e in rest if e.bbox[1]<span.bbox[1]]
            ordered+=sorted(band,key=lambda e:(e.style['column'],e.bbox[1]));rest=[e for e in rest if e not in band];ordered.append(span)
        elements=ordered+sorted(rest,key=lambda e:(e.style['column'],e.bbox[1]))
    for current,following in zip(elements,elements[1:]):
        if current.kind=='paragraph' and following.kind=='formula' and len(current.text)<900 and current.bbox[3]-current.bbox[1]<page_height*.25:
            current.group_id=following.group_id;current.metadata['role']='body_connective'
            current.metadata['anchor_id']=following.id;current.metadata['scan_formula_intro']=True
    for region in regions:
        punctuation=region.get('detached_prose_punctuation')
        if not punctuation:continue
        owner=next((e for e in elements if region['asset_id'] in e.asset_ids and punctuation['text'] in TOKEN_RE.sub('',e.text)),None)
        if owner:
            processed.append({'bbox':punctuation['bbox'],'owner':owner.id,'kind':'detached-prose-punctuation'})
            units.append({'id':region['id']+'-source-punctuation','text':punctuation['text'],'bbox':punctuation['bbox'],
                          'confidence':punctuation.get('source_ocr_confidence',0.),'owner':owner.id,
                          'disposition':'translation-source-detached-punctuation','basis':'derived-original-prose-punctuation-evidence'})
        else:_issue(book,'scan_detached_punctuation_unassigned','公式外的原正文标点未在关联正文中找到，须核图修正',number,'error',region_id=region['id'])
    for exclusion in correction.get('exclusions',[]):
        if not str(exclusion.get('reason','')).strip():raise ValueError('Reviewed exclusion requires a specific reason')
        processed.append({'bbox':_box(exclusion['bbox'],width,height),'owner':'reviewed-exclusion:'+exclusion['reason'],'kind':'explicit-exclusion'})
    audit=audit_scan_page(image,processed,units)
    for issue in audit.get('issues',[]):book.issues.append({**issue,'page':number})
    if not elements and np.count_nonzero(image.mean(axis=2)<230)>20:_issue(book,'scan_ocr_empty','可见扫描页没有可处理内容，需修正识别或区域',number,'error')
    for r in regions:
        if r['kind'] in {'figure','table'}:
            language=[w for l in raw for w in l['words'] if _covered(w['bbox'],r['bbox'],.75) and _LATIN_WORD.search(w['text'])]
            if language:_issue(book,'scan_visual_language_untranslated','图表内有自然语言标签待独立翻译；原图已保留',number,'error',asset_id=r['asset_id'])
    if not printed:_issue(book,'printed_page_unresolved','扫描页无可靠印刷页码，使用原PDF物理页码',number)
    _issue(book,'scan_ocr_source_review','正文来自本地OCR；墨迹覆盖和置信度均不能代替原图语义核验',number)
    bundle=book._scan_bundle
    if len(bundle):
        path=outdir/f'assets/scan-p{number:04d}.pdf';bundle.save(path,garbage=4,deflate=True);fingerprint=file_sha256(path)
        for a in book.assets.values():
            if a.page==number and a.path==path.relative_to(outdir).as_posix():a.sha256=fingerprint
    bundle.close();del book._scan_bundle;source.close()
    ledger=folder/f'p{number:04d}.units.json.gz'
    with gzip.open(ledger,'wt',encoding='utf-8') as stream:json.dump(units,stream,ensure_ascii=False)
    review={'schema':SCAN_SCHEMA,'source_sha256':book.source_sha256,'page':number,'image_sha256':image_hash,'dpi':dpi,
            'printed_page':printed,'line_map_sha256':line_map_hash,'lines':[{k:l[k] for k in ('id','text','bbox','confidence')} for l in prose],
            'regions':[{k:v for k,v in r.items() if k not in {'row'}} for r in regions],
            'correction_example':{'reviewed':True,'reason':'核对原扫描图后修正','image_sha256':image_hash,'line_map_sha256':line_map_hash,'line_text':{}},'correction_notes':'省略 regions 字段即保留检测结果；如提供该字段，必须是完整保护区域列表。'}
    _json(folder/f'p{number:04d}.review.json',review)
    _json(folder/f'p{number:04d}.quality.json',audit)
    info={'number':number,'width':page_width,'height':page_height,'printed_page':printed,
          'label_source':label_source,'label_confidence':label_confidence,'columns':columns,'column_split':page_width/2,
          'parser_backend':'local-scan-ocr','element_ids':[e.id for e in elements],
          'coverage':{'total_glyphs':0,'unresolved':0,'total_ocr_units':len(units),'basis':'raster-ink-and-ocr-units','ledger':ledger.relative_to(outdir).as_posix()},
          'scan_evidence':{'schema':SCAN_SCHEMA,'image':image_path.relative_to(outdir).as_posix(),'image_sha256':image_hash,'image_file_sha256':file_sha256(image_path),'dpi':dpi,
                           'image_size':[width,height],'ocr_identity':ident,'quality':audit,'source_rotation':original.rotation,
                           'original_composed_page_preserved':True,'recognition_accuracy':None,'optical_recognition_verified':False,
                           'elapsed_seconds':time.perf_counter()-started}}
    return elements,info


def analyze(pdf_path,outdir,pages=None,backend='auto',*,ocr='auto',ocr_dpi=300,corrections=None):
    path=Path(pdf_path).resolve();outdir=Path(outdir).resolve();outdir.mkdir(parents=True,exist_ok=True);(outdir/'assets').mkdir(exist_ok=True)
    if not 150<=int(ocr_dpi)<=600:raise ValueError('OCR dpi must be between 150 and 600')
    correction_data=json.loads(Path(corrections).read_text(encoding='utf-8')) if corrections else {}
    with pymupdf.open(path) as doc:
        selected=sorted(set(range(1,len(doc)+1) if pages is None else pages))
        if not selected or min(selected)<1 or max(selected)>len(doc):raise ValueError('Invalid scan page range')
        book=Book(str(path),file_sha256(path),doc.metadata.get('title') or path.stem,len(doc),selected)
        if correction_data and correction_data.get('source_sha256')!=book.source_sha256:raise ValueError('Scan corrections belong to another source PDF')
        selected_scans=set(selected if ocr=='force' else scan_pages(path,selected))
        from .ocr_engine import engine_identity
        from .scan_detectors import detector_identity
        from .extraction import _restore_checkpoint,_checkpoint_manifest,_checkpoint_page
        signature=digest({'source':book.source_sha256,'pages':selected,'dpi':int(ocr_dpi),'ocr':engine_identity(),
                          'detectors':detector_identity(),'corrections':correction_data,'backend':backend,'ocr_mode':ocr,'selected_scans':sorted(selected_scans),
                          'files':{name:file_sha256(Path(__file__).with_name(name)) for name in ('scan_adapter.py','scan_quality.py','ocr_engine.py','scan_detectors.py','models.py','extraction.py','babel_adapter.py','paragraph_adapter.py','table_adapter.py')}})
        (outdir/'analysis.parts').mkdir(exist_ok=True)
        try:book=_restore_checkpoint(book,outdir,signature)
        except (OSError,ValueError,KeyError,TypeError):pass
        completed={p['number'] for p in book.pages}
        for p in book.pages:
            if p.get('scan_evidence'):
                evidence=p['scan_evidence'];image_file=outdir/evidence['image']
                if not image_file.is_file() or file_sha256(image_file)!=evidence.get('image_file_sha256'):
                    raise ValueError('Scan checkpoint image changed; use a fresh job folder')
        book.profile.update(analysis_signature=signature,job_dir=str(outdir),parser_backend='local-scan-ocr',source_mode='mixed' if len(selected_scans)<len(selected) else 'scan',
                            scan_schema=SCAN_SCHEMA,ocr_dpi=int(ocr_dpi),ocr_corrections_sha256=file_sha256(corrections) if corrections else None)
        if len(selected_scans)==len(selected):book.profile['heading_color']='202730'
        native=[n for n in selected if n not in selected_scans]
        if native and not all(n in completed for n in native):
            from .extraction import analyze as native_analyze
            native_root=outdir/'native'
            result=native_analyze(path,native_root,pages=native,backend=backend,ocr='off')
            for asset in result.assets.values():asset.path=(Path('native')/asset.path).as_posix()
            for page in result.pages:
                ledger=page.get('coverage',{}).get('ledger')
                if ledger:page['coverage']['ledger']=(Path('native')/ledger).as_posix()
            book.elements.extend(result.elements);book.assets.update(result.assets);book.pages.extend(result.pages);book.issues.extend(result.issues)
            for n in native:_checkpoint_page(book,outdir,signature,n)
            completed.update(native);_checkpoint_manifest(book,outdir,signature,completed)
        for number in sorted(selected_scans):
            if number in completed:continue
            elements,info=_build_page(book,doc,number,outdir,int(ocr_dpi),correction_data.get('pages',{}).get(str(number),{}))
            book.elements.extend(elements);book.pages.append(info)
            book.pages.sort(key=lambda p:p['number']);book.elements.sort(key=lambda e:(e.page,book.pages[[p['number'] for p in book.pages].index(e.page)]['element_ids'].index(e.id)))
            _checkpoint_page(book,outdir,signature,number);completed.add(number)
            book.profile['analysis_processed_pages']=len(completed);_checkpoint_manifest(book,outdir,signature,completed)
        book.profile['analysis_complete']=True;book.profile['analysis_processed_pages']=len(book.pages)
        book.profile['columns']=2 if any(p.get('columns')==2 for p in book.pages) else 1
        _checkpoint_manifest(book,outdir,signature,completed)
        book.save(outdir/'book.json')
        return book
