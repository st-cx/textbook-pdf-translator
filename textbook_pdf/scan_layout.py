"""Scan-only paper-style wrapper; fixed native renderer is kept unchanged."""
from __future__ import annotations
import json
from pathlib import Path
import time
import uuid
import numpy as np
import pymupdf
from PIL import Image
from .models import file_sha256,digest
from .layout import compose as _compose

POLICY='original-scan-paper-behind-content-v1'

def _paper_tones(book,outdir):
    tones={}
    for page in book.pages:
        evidence=page.get('scan_evidence')
        if not evidence:continue
        path=(Path(book.profile.get('job_dir',outdir))/evidence['image']).resolve()
        if file_sha256(path)!=evidence.get('image_file_sha256'):raise ValueError('Scan source image changed before paper-style composition')
        with Image.open(path) as im:
            rgb=np.array(im.convert('RGB'))
        # A strided sample avoids counting a dense paragraph more often than
        # unprinted paper. Median rejects ordinary black ink and margin labels.
        sampled=rgb[::16,::16].reshape(-1,3)
        tone=np.median(sampled,axis=0)
        if float(tone.min())<190:raise ValueError('Scan paper is dark or uncertain; explicit reviewed style required')
        tones[str(page['number'])]=[int(v) for v in tone]
    return tones

def compose(book,translations,outdir,output_path=None,allow_incomplete=False):
    started=time.perf_counter();outdir=Path(outdir).resolve();outdir.mkdir(parents=True,exist_ok=True)
    tones=_paper_tones(book,outdir)
    if not tones:return _compose(book,translations,outdir,output_path=output_path,allow_incomplete=allow_incomplete)
    enabled=book.profile.get('scan_paper_background',True)
    identity={'policy':POLICY,'module_sha256':file_sha256(Path(__file__)), 'enabled':bool(enabled),'source_paper_rgb':tones}
    book.profile['scan_composition']=identity;book.save(outdir/'book.json')
    result=_compose(book,translations,outdir,output_path=output_path,allow_incomplete=allow_incomplete)
    mapping_path=Path(result['layout_map_path']);mapping=json.loads(mapping_path.read_text(encoding='utf-8'))
    output=Path(result['pdf_path']);before=file_sha256(output);pages=[]
    temporary=output.with_name('.'+output.stem+'-'+uuid.uuid4().hex+'.paper.pdf')
    try:
        with pymupdf.open(output) as pdf:
            for info,page in zip(mapping['pages'],pdf):
                candidates=[tones[str(n)] for n in info.get('source_pages',[]) if str(n) in tones]
                if not candidates:continue
                tone=[int(v) for v in np.median(candidates,axis=0)]
                if enabled:page.draw_rect(page.rect,color=None,fill=[v/255 for v in tone],overlay=False)
                pages.append({'output_page':page.number+1,'source_pages':info.get('source_pages',[]),'paper_rgb':tone})
            pdf.save(temporary,garbage=4,deflate=True)
        temporary.replace(output)
    finally:
        if temporary.exists():temporary.unlink()
    mapping['pdf_sha256']=file_sha256(output)
    mapping['scan_composition']={'identity':identity,'pages':pages,'pdf_before_sha256':before,'pdf_after_sha256':mapping['pdf_sha256'],
                                 'object_geometry_changed':False,'source_asset_pixels_changed':False}
    mapping_path.write_text(json.dumps(mapping,ensure_ascii=False,indent=2),encoding='utf-8')
    result['scan_composition']=mapping['scan_composition'];result['elapsed_seconds']=round(time.perf_counter()-started,3)
    return result
