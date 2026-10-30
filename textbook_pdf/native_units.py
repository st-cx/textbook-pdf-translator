"""Conservative native diagram containers and image-paint ownership repairs.

Planning is read-only. Repair operates only on the caller's supplied Book/page
objects; it never opens a Store or registers a source. An unresolved proposal is
not a completeness certificate. All source paths, image paints and glyphs must
remain at original scale and relative coordinates inside a single source unit.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path
import re
import pymupdf as fitz

from .models import Element, digest, TOKEN_RE

SCHEMA='native-complete-unit-v1'


def _v(obj,key,default=None):
    return obj.get(key,default) if isinstance(obj,dict) else getattr(obj,key,default)


def _union(boxes):
    boxes=list(boxes)
    if not boxes:raise ValueError('A native unit needs nonempty source geometry')
    return fitz.Rect(min(b[0] for b in boxes),min(b[1] for b in boxes),
                     max(b[2] for b in boxes),max(b[3] for b in boxes))


def _touches(a,b):
    """Path rectangles may have zero height/width and still carry visible ink."""
    a,b=fitz.Rect(a),fitz.Rect(b)
    return min(a.x1,b.x1)>=max(a.x0,b.x0) and min(a.y1,b.y1)>=max(a.y0,b.y0)


def _active(elements):
    return [e for e in elements if not _v(e,'metadata',{}).get('render_suppressed')]


def _language_label(row,elements):
    text=''.join(g['c'] for g in row).strip()
    # Some original illustration programs encode English labels in MathJax's
    # Main font. Only this explicit prose-label grammar can override that font
    # evidence. Single variables, named operators and mixed math stay native.
    if re.fullmatch(r'(?:(?:basis|base|recursive|inductive)\s+step|(?:step|stage|iteration)\s+\d+)',text,re.I):
        return not any(g.get('flags',0)&2 for g in row)
    owners={g.get('owner') for g in row}
    explicit=[e for e in elements if _v(e,'id') in owners and _v(e,'kind')=='figure_label'
              and _v(e,'metadata',{}).get('translatable',True)]
    return (bool(explicit) and bool(re.search(r'[A-Za-z]{3}',text))
            and not re.search(r'[=+*/<>∑∏∫√≤≥]',text)
            and not any('math' in g.get('font','').lower() or g.get('flags',0)&2 for g in row))


def _proof(page,box,glyphs,drawings,indices,images=()):
    return {'schema':SCHEMA,'source_page':page.number+1,'bbox':list(box),
            'glyph_ids':[g['id'] for g in glyphs],
            'drawings':[{'index':i,'seqno':drawings[i].get('seqno'),
                         'bbox':list(drawings[i]['rect']),
                         'geometry_sha256':digest(str(drawings[i]))} for i in indices],
            'image_paints':[{'number':i['number'],'digest':i['digest'].hex(),
                             'transform':list(i['transform']),'bbox':list(i['bbox'])} for i in images],
            'transform_policy':'original 1:1 geometry; only one common translation on output',
            'certifies_full_book':False}


def _ruled_units(page,glyphs,elements,drawings):
    units=[];unresolved=[]
    # A long ruled strip with a nearby explicit figure caption supplies a
    # container boundary for disconnected dots/trees. Connectivity alone does
    # not supply that boundary and is deliberately insufficient here.
    captions=[e for e in elements if _v(e,'kind')=='caption'
              and re.match(r'^\s*(?:FIGURE|Figure|图)\s*\d+',_v(e,'text',''))]
    rules=[(i,fitz.Rect(d['rect'])) for i,d in enumerate(drawings)
           if abs(fitz.Rect(d['rect']).height)<.1
           and fitz.Rect(d['rect']).width>=page.rect.width*.65
           and d.get('fill') is None]
    for caption in captions:
        parent=_v(caption,'metadata',{}).get('figure_id')
        if parent and any(_v(e,'id')==parent and _v(e,'metadata',{}).get('native_unit_proof',{}).get('schema')==SCHEMA for e in elements):
            continue
        cb=fitz.Rect(_v(caption,'bbox'))
        ends=[(i,r) for i,r in rules if 5<=cb.y0-r.y0<=25]
        if len(ends)!=1:continue
        _,last=ends[0]
        band=[(i,r) for i,r in rules if abs(r.x0-last.x0)<=.1 and abs(r.x1-last.x1)<=.1
              and 0<=last.y0-r.y0<=page.rect.height*.65]
        if len(band)<3:continue
        first=min(r.y0 for _,r in band)
        search=fitz.Rect(last.x0-1,first-30,last.x1+1,last.y0+.6)
        inside=[g for g in glyphs if search.contains(fitz.Rect(g['bbox']))]
        if not inside:continue
        # Reject prose, incomplete original owner blocks and any boundary path.
        byline=defaultdict(list)
        for g in inside:byline[(g.get('block'),g.get('line'))].append(g)
        lines=[''.join(g['c'] for g in row) for row in byline.values()]
        if any(len(re.sub(r'\s','',t))>64 for t in lines):
            unresolved.append({'kind':'ruled-caption-diagram','reason':'Long prose inside proposed figure','caption_id':_v(caption,'id')});continue
        indices=[i for i,d in enumerate(drawings) if search.contains(fitz.Rect(d['rect']))]
        if len(indices)<7:continue
        crossed=[i for i,d in enumerate(drawings) if _touches(search,d['rect'])
                 and not search.contains(fitz.Rect(d['rect']))]
        if crossed:
            unresolved.append({'kind':'ruled-caption-diagram','reason':'Source path crosses container boundary','drawing_indices':crossed});continue
        owner_ids={g.get('owner') for g in inside}
        partial=[g['id'] for g in glyphs if g.get('owner') in owner_ids and g['c'].strip() and g not in inside]
        if partial:
            unresolved.append({'kind':'ruled-caption-diagram','reason':'Container cuts a source text owner','glyph_ids':partial});continue
        labels=[row for row in byline.values() if _language_label(row,elements)]
        label_ids={g['id'] for row in labels for g in row}
        box=_union([g['bbox'] for g in inside]+[drawings[i]['rect'] for i in indices])+(-.5,-.5,.5,.5)
        consumed=[_v(e,'id') for e in elements if _v(e,'id') in owner_ids
                  or (_v(e,'kind')=='figure' and box.contains(fitz.Rect(_v(e,'bbox'))))]
        if not consumed:continue
        # A second caption within the content region would make ownership of
        # the disconnected components ambiguous.
        if any(_v(e,'id')!=_v(caption,'id') and box.intersects(fitz.Rect(_v(e,'bbox'))) for e in captions):continue
        proof=_proof(page,box,inside,drawings,indices)
        label_rows=[]
        for row in labels:
            rb=_union(g['bbox'] for g in row)
            other=[g for g in inside if g not in row]
            obstacles=[fitz.Rect(g['bbox']) for g in other]+[fitz.Rect(drawings[i]['rect']) for i in indices]
            right=[o.x0 for o in obstacles if o.x0>=rb.x1 and min(o.y1,rb.y1)>=max(o.y0,rb.y0)]
            reserved=fitz.Rect(rb)
            if right:reserved.x1=min(min(right)-2,rb.x1+20)
            color=row[0].get('color','00aeef')
            if isinstance(color,int):color=f'{color:06x}'
            label_rows.append({'text':''.join(g['c'] for g in row),'glyph_ids':[g['id'] for g in row],
                               'bbox':list(reserved),'original_text_bbox':list(rb),
                               'size':max(g['size'] for g in row),'color':color})
        units.append({'kind':'ruled-caption-diagram','bbox':list(box),'caption_id':_v(caption,'id'),
                      'glyph_ids':[g['id'] for g in inside], 'protected_glyph_ids':[g['id'] for g in inside if g['id'] not in label_ids],
                      'labels':label_rows,
                      'consumed_element_ids':consumed,'drawing_indices':indices,'proof':proof})
    return units,unresolved


def _image_units(page,glyphs,elements,assets,drawings):
    units=[];unresolved=[];images=page.get_image_info(hashes=True)
    if not images:return units,unresolved
    byid={_v(e,'id'):e for e in elements}
    figures=[e for e in elements if _v(e,'kind')=='figure' and _v(e,'asset_ids')]
    for broad in figures:
        md=_v(broad,'metadata',{});labels=[byid[i] for i in md.get('figure_label_ids',[]) if i in byid]
        if not labels:continue
        broadbox=fitz.Rect(_v(broad,'bbox'))
        painted=[i for i in images if (broadbox&fitz.Rect(i['bbox'])).get_area()>=fitz.Rect(i['bbox']).get_area()*.80]
        if not painted:continue
        photo=_union(i['bbox'] for i in painted)
        alternatives=[e for e in figures if e is not broad and not _v(e,'metadata',{}).get('figure_label_ids')
                      and all((fitz.Rect(_v(e,'bbox'))&fitz.Rect(i['bbox'])).get_area()>=fitz.Rect(i['bbox']).get_area()*.98 for i in painted)]
        if len(alternatives)!=1:continue
        ids={g['id'] for g in glyphs if g.get('owner') in {_v(e,'id') for e in labels}}
        labelglyphs=[g for g in glyphs if g['id'] in ids]
        if not labelglyphs or any(photo.intersects(fitz.Rect(g['bbox'])) for g in labelglyphs):continue
        parts=[i for i,d in enumerate(drawings) if broadbox.contains(fitz.Rect(d['rect']))
               and not (photo+(-.6,-.6,.6,.6)).intersects(fitz.Rect(d['rect']))]
        if not parts:continue
        box=_union([g['bbox'] for g in labelglyphs]+[drawings[i]['rect'] for i in parts])+(-.5,-.5,.5,.5)
        if box.intersects(photo):
            unresolved.append({'kind':'external-image-label','reason':'Label component cannot be separated from source image paints','element_id':_v(broad,'id')});continue
        portraitbox=fitz.Rect(_v(alternatives[0],'bbox'))
        oldparts=[i for i,d in enumerate(drawings) if _touches(broadbox,d['rect'])]
        destinations={}
        for i in oldparts:
            r=fitz.Rect(drawings[i]['rect'])
            if i in parts and box.contains(r):destinations[str(i)]='external-label-unit'
            elif portraitbox.contains(r):destinations[str(i)]='existing-image-unit'
        if len(destinations)!=len(oldparts):
            unresolved.append({'kind':'external-image-label','reason':'An original vector member has no complete destination',
                               'element_id':_v(broad,'id'),'unassigned_drawing_indices':sorted(set(oldparts)-{int(i) for i in destinations})});continue
        others=[g for g in glyphs if box.intersects(fitz.Rect(g['bbox'])) and g['c'].strip() and g['id'] not in ids]
        if others:continue
        proof=_proof(page,box,labelglyphs,drawings,parts,painted)
        units.append({'kind':'external-image-label','bbox':list(box),'element_id':_v(broad,'id'),
                      'old_asset_ids':list(_v(broad,'asset_ids')),'label_ids':[_v(e,'id') for e in labels],
                      'glyph_ids':sorted(ids),'drawing_indices':parts,'preserved_image_owner_id':_v(alternatives[0],'id'),
                      'image_paints_preserved_once':True,'proof':proof})
        units[-1]['proof']['original_crop_drawing_destinations']=destinations
    return units,unresolved


def plan_native_units(page,glyphs,elements,assets):
    """Return source-proved proposals only; do not mutate supplied state.

    No page numbers, textbook strings or fixed coordinates are recognized.
    Geometry determines candidates; a caption/rule enclosure or original image
    paint identity and a disjoint external label determines acceptance.
    """
    active=_active(elements);drawings=page.get_drawings()
    ids=[g['id'] for g in glyphs]
    if len(ids)!=len(set(ids)):raise ValueError('Duplicate native glyph identity')
    a,ar=_ruled_units(page,glyphs,active,drawings)
    b,br=_image_units(page,glyphs,active,assets,drawings)
    units=[];unresolved=ar+br
    for unit in a+b:
        consumed=set(unit.get('consumed_element_ids',[unit.get('element_id')]))
        selected=set(unit.get('protected_glyph_ids',[]))
        conflict=[]
        for e in active:
            if _v(e,'id') in consumed:continue
            refs=set(_v(e,'asset_ids',[]))|set(TOKEN_RE.findall(_v(e,'text','')))
            for aid in refs:
                if aid in assets and selected.intersection(_v(assets[aid],'metadata',{}).get('glyph_ids',[])):
                    conflict.append({'element_id':_v(e,'id'),'asset_id':aid})
        if conflict:
            unresolved.append({'kind':unit['kind'],'reason':'Selected glyph has an active reference outside consumed owners','conflicts':conflict});continue
        overlap=[old for old in units if fitz.Rect(old['bbox']).intersects(fitz.Rect(unit['bbox']))]
        if overlap:
            unresolved.append({'kind':unit['kind'],'reason':'Overlapping proposals need independent ownership resolution'});continue
        units.append(unit)
    return {'schema':SCHEMA,'units':units,'unresolved':unresolved,'complete_book_verified':False}


def _path_identity(drawing,dx=0,dy=0):
    def item(value):
        if isinstance(value,fitz.Point):return [round(value.x+dx,4),round(value.y+dy,4)]
        if isinstance(value,fitz.Rect):return [round(value.x0+dx,4),round(value.y0+dy,4),round(value.x1+dx,4),round(value.y1+dy,4)]
        if isinstance(value,(tuple,list)):return [item(v) for v in value]
        if isinstance(value,float):return round(value,4)
        return value
    return digest({key:item(drawing.get(key)) for key in ('items','color','fill','width','fill_opacity','stroke_opacity','even_odd','closePath')})


def _path_translation_error(source,target,dx,dy):
    style=('color','fill','width','fill_opacity','stroke_opacity','even_odd','closePath','lineCap','lineJoin','dashes')
    if any(source.get(k)!=target.get(k) for k in style):return None
    def translated(value,x=0,y=0):
        if isinstance(value,fitz.Point):return (value.x+x,value.y+y)
        if isinstance(value,fitz.Rect):return (value.x0+x,value.y0+y,value.x1+x,value.y1+y)
        if isinstance(value,(tuple,list)):return tuple(translated(v,x,y) for v in value)
        return value
    def error(a,b):
        if isinstance(a,(tuple,list)) and isinstance(b,(tuple,list)):
            if len(a)!=len(b):return float('inf')
            return max((error(x,y) for x,y in zip(a,b)),default=0)
        if isinstance(a,(int,float)) and isinstance(b,(int,float)):return abs(a-b)
        return 0 if a==b else float('inf')
    return error(translated(source['items']),translated(target['items'],dx,dy))


def _verify_image_destination(book,outdir,elements,page,unit):
    owner=next(e for e in elements if e.id==unit['preserved_image_owner_id'])
    expected=unit['proof']['image_paints'];seen=[];paths=[]
    for aid in owner.asset_ids:
        asset=book.assets[aid];path=Path(outdir)/asset.path
        pending=getattr(book,'_asset_bundles',{}).get(asset.page)
        use_pending=(pending is not None and not asset.sha256 and not path.is_file())
        if not path.is_file() and not use_pending:raise ValueError('Existing image unit has no readable immutable asset')
        if asset.sha256 and not use_pending:
            from .models import file_sha256
            if file_sha256(path)!=asset.sha256:raise ValueError('Existing image unit bundle hash changed')
        with (nullcontext(pending) if use_pending else fitz.open(path)) as source:
            target=source[asset.metadata.get('pdf_page',0)]
            for image in target.get_image_info(hashes=True):
                matrix=list(image['transform']);matrix[4]+=asset.bbox[0];matrix[5]+=asset.bbox[1]
                seen.append((image['digest'].hex(),matrix))
            paths.extend((d,asset.bbox[0],asset.bbox[1]) for d in target.get_drawings())
    missing=[image['number'] for image in expected if sum(d==image['digest'] and max(abs(a-b) for a,b in zip(m,image['transform']))<=.0002 for d,m in seen)!=1]
    original=page.get_drawings()
    required=[int(i) for i,where in unit['proof']['original_crop_drawing_destinations'].items() if where=='existing-image-unit']
    errors=[]
    for i in required:
        possible=[delta for d,x,y in paths if (delta:=_path_translation_error(original[i],d,x,y)) is not None]
        errors.append(min(possible,default=float('inf')))
    if missing or any(e>.0002 for e in errors):
        raise ValueError('Existing image unit does not prove every transferred original paint/path')
    return {'verified':True,'image_paint_count':len(expected),'drawing_indices':required,
            'maximum_path_coordinate_error_pt':max(errors,default=0),'path_coordinate_float_tolerance_pt':.0002,
            'method':'saved-native-image-digest/full-transform and original path/style identity; coordinate float roundtrip measured'}


def _verify_created_unit(book,page,unit,asset,glyphs):
    """Check the actual newly cropped native page before retiring any owner."""
    bundle=getattr(book,'_asset_bundles',{}).get(asset.page)
    if bundle is None:raise ValueError('New native unit is missing its actual pending PDF page')
    target=bundle[asset.metadata['pdf_page']]
    original=page.get_drawings();actual=target.get_drawings();unused=set(range(len(actual)));errors=[]
    for i in unit['drawing_indices']:
        options=[(e,j) for j in unused if (e:=_path_translation_error(original[i],actual[j],asset.bbox[0],asset.bbox[1])) is not None]
        if not options:raise ValueError('Native unit lost an original vector path/style')
        error,j=min(options)
        if error>.0002:raise ValueError('Native unit changed original vector coordinates')
        unused.remove(j);errors.append(error)
    if unused:raise ValueError('Native unit contains an unassigned source vector path')
    if target.get_image_info():raise ValueError('An external label/chart unexpectedly contains source image paints')
    from collections import Counter
    expected=Counter(g['c'] for g in glyphs if g['id'] in unit.get('protected_glyph_ids',[]) and g['c'].strip())
    present=Counter(c['c'] for block in target.get_text('rawdict')['blocks'] for line in block.get('lines',[])
                    for span in line['spans'] for c in span['chars'] if c['c'].strip())
    if present!=expected:raise ValueError('Native unit has missing or foreign original text operators')
    if expected and not asset.metadata.get('native_ink_output_verification',{}).get('verified'):
        raise ValueError('Native unit glyph/font/full-matrix output identity is unverified')
    return {'verified':True,'original_paths':len(errors),'actual_paths':len(actual),
            'maximum_path_coordinate_error_pt':max(errors,default=0),'path_coordinate_float_tolerance_pt':.0002,
            'actual_text_counter_matches_all_protected_source_members':True,'unexpected_image_paints':0}


def repair_native_units(book,doc,page,outdir,elements,glyphs,*,asset_factory=None):
    """Apply proved units to a caller-owned candidate Book; never touch Store.

    Asset creation delegates to the existing native cropper. The caller must
    finalize bundles and independently validate source/target glyph identities,
    image paints, drawing stencil and final placement before adopting a source.
    This function preserves history and reports every retired asset/element.
    """
    verify_created=asset_factory is None
    if asset_factory is None:
        from .extraction import _asset
        asset_factory=_asset
    plan=plan_native_units(page,glyphs,elements,book.assets)
    byid={e.id:e for e in elements};applied=[]
    for unit in plan['units']:
        unit['proof']['source_sha256']=book.source_sha256
        box=fitz.Rect(unit['bbox'])
        if unit['kind']=='external-image-label':
            try:
                unit['proof']['saved_image_destination_verification']=_verify_image_destination(book,outdir,elements,page,unit)
            except (ValueError,RuntimeError,OSError) as exc:
                plan['unresolved'].append({'kind':unit['kind'],'element_id':unit['element_id'],'severity':'error','reason':str(exc)})
                continue
            parent=byid[unit['element_id']]
            metadata={'detector':'translated-figure-background','glyph_ids':[],
                      'exclude_glyph_ids':unit['glyph_ids'],'detached_label_ids':unit['label_ids'],
                      'label_removal_proof':{'native_glyph_ids':unit['glyph_ids'],'graphics_removed':False},
                      'native_unit_proof':unit['proof']}
            try:asset=asset_factory(book,doc,page,outdir,'figure',box,'',None,metadata)
            except (ValueError,RuntimeError,OSError) as exc:
                plan['unresolved'].append({'kind':unit['kind'],'element_id':unit['element_id'],'severity':'error','reason':str(exc)});continue
            if verify_created:
                try:unit['proof']['created_native_unit_verification']=_verify_created_unit(book,page,unit,asset,glyphs)
                except ValueError as exc:
                    asset.metadata.update(exclusion_reason='Rejected unverified native-unit candidate',native_unit_rejected=True)
                    plan['unresolved'].append({'kind':unit['kind'],'element_id':unit['element_id'],'severity':'error','reason':str(exc)});continue
            old=list(parent.asset_ids);parent.asset_ids=[asset.id];parent.bbox=list(box)
            parent.metadata['native_unit_proof']=unit['proof']
            parent.metadata.update(layout_sticky_next=True,keep_with_next_id=unit['preserved_image_owner_id'],
                                   source_relation='external label/pointer and image originally in the same source crop; image paint now owned once')
            byid[unit['preserved_image_owner_id']].metadata.setdefault('related_external_label_unit_ids',[]).append(parent.id)
            for aid in old:book.assets[aid].metadata.update(superseded_by=asset.id,exclusion_reason='Source image paints exclusively retained in their existing portrait unit; external label and vector pointer recropped separately')
            applied.append({'kind':unit['kind'],'element_id':parent.id,'asset_id':asset.id,'old_asset_ids':old,'proof':unit['proof']})
        else:
            eid=f'p{page.number+1:04d}-native-unit-{digest([book.source_sha256,unit["proof"]])[:14]}'
            captions=byid[unit['caption_id']];labels=[]
            for row in unit['labels']:
                lid=eid+'-label-'+digest(row['glyph_ids'])[:10]
                child=Element(lid,'figure_label',row['text'],page.number+1,row['bbox'],captions.printed_page,
                              {'source_size':row['size'],'color':row['color'],'columns':1,'column':0},[],eid,captions.chapter,
                              {'glyph_ids':row['glyph_ids'],'source_text':row['text'],'parent_figure_id':eid,'anchor_id':eid,
                               'source_label_bbox':row['bbox'],'original_source_text_bbox':row['original_text_bbox'],
                               'recognition':SCHEMA})
                labels.append(child)
            labelids=[g for row in unit['labels'] for g in row['glyph_ids']]
            try:
                asset=asset_factory(book,doc,page,outdir,'figure',box,'',None,
                        {'detector':'translated-figure-background','glyph_ids':unit['protected_glyph_ids'],
                         'exclude_glyph_ids':labelids,'detached_label_ids':[e.id for e in labels],
                         'label_removal_proof':{'native_glyph_ids':labelids,'graphics_removed':False},
                         'native_unit_proof':unit['proof'],'source_drawing_ids':['drawing-'+str(i) for i in unit['drawing_indices']]})
            except (ValueError,RuntimeError,OSError) as exc:
                plan['unresolved'].append({'kind':unit['kind'],'severity':'error','reason':str(exc)});continue
            if verify_created:
                try:unit['proof']['created_native_unit_verification']=_verify_created_unit(book,page,unit,asset,glyphs)
                except ValueError as exc:
                    asset.metadata.update(exclusion_reason='Rejected unverified native-unit candidate',native_unit_rejected=True)
                    plan['unresolved'].append({'kind':unit['kind'],'severity':'error','reason':str(exc)});continue
            parent=Element(eid,'figure','',page.number+1,list(asset.bbox),captions.printed_page,{'columns':1,'column':0},[asset.id],eid,captions.chapter,
                           {'glyph_ids':unit['protected_glyph_ids'],'figure_label_ids':[e.id for e in labels],
                            'source_linguistic_labels_detached':True,'native_unit_proof':unit['proof'],'caption_required':True})
            insert=min(elements.index(byid[i]) for i in unit['consumed_element_ids'])
            oldassets=[]
            for oldid in unit['consumed_element_ids']:
                old=byid[oldid];old.metadata.update(render_suppressed=True,superseded_by=eid,
                    source_glyph_ids=old.metadata.get('glyph_ids',[]),glyph_ids=[],translatable=False,preserve_only=True,
                    exclusion_reason='All original glyph/path members transferred to complete native diagram unit')
                for aid in old.asset_ids:
                    oldassets.append(aid);book.assets[aid].metadata.update(superseded_by=asset.id,exclusion_reason='Complete original diagram replaces connected fragments')
            captions.group_id=eid;captions.metadata.update(anchor_id=eid,figure_id=eid)
            for g in glyphs:
                if g['id'] in unit['protected_glyph_ids']:
                    g.update(owner=eid,asset_id=asset.id,disposition='preserved-in-asset')
                else:
                    for child in labels:
                        if g['id'] in child.metadata['glyph_ids']:g.update(owner=child.id,asset_id=None,disposition='translation-source')
            elements[insert:insert]=[parent,*labels]
            applied.append({'kind':unit['kind'],'element_id':eid,'asset_id':asset.id,'old_asset_ids':oldassets,
                            'consumed_element_ids':unit['consumed_element_ids'],'label_ids':[e.id for e in labels],'proof':unit['proof']})
        # Update path ownership for this exact unit; no template-style exclusion
        # is used as evidence that an original point/line was preserved.
        for info in book.pages:
            if info.get('number')!=page.number+1:continue
            for i in unit['drawing_indices']:
                rows=[d for d in info.get('graphics_coverage',[]) if d['id']==f'drawing-{i}']
                for row in rows:row.update(asset_ids=[asset.id],disposition='preserved-in-asset',reason=SCHEMA)
    return {**plan,'applied':applied,'requires_independent_native_and_final_output_verification':True}


def resolve_proved_source_warnings(book,applied):
    """Archive exact obsolete source-association warnings; never clear errors.

    A whole ruled diagram now has its explicit source caption. An external
    label no longer remains as English ink in the background asset. These two
    bounded conclusions do not certify translation semantics or other warnings.
    """
    resolutions=[];remaining=[]
    for issue in book.issues:
        match=None
        if issue.get('severity','warning')=='warning':
            for unit in applied:
                ruled=(unit['kind']=='ruled-caption-diagram' and issue.get('code')=='visual_caption_unresolved'
                       and issue.get('element_id') in unit.get('consumed_element_ids',[]))
                detached=(unit['kind']=='external-image-label' and issue.get('code')=='visual_label_untranslated'
                          and issue.get('element_id')==unit.get('element_id'))
                if ruled or detached:match=unit;break
        if match is None:remaining.append(issue);continue
        resolutions.append({'original_issue':dict(issue),'original_issue_sha256':digest(issue),
                            'resolution':'Whole original ruled diagram has one explicit source caption' if match['kind']=='ruled-caption-diagram' else 'Natural label is a separate translatable child; background has no original language glyphs',
                            'source_sha256':book.source_sha256,'replacement_element_id':match['element_id'],
                            'replacement_asset_id':match['asset_id'],'native_unit_proof_sha256':digest(match['proof']),
                            'certifies_translated_page':False})
    book.issues=remaining
    if resolutions:book.profile.setdefault('resolved_source_issues',[]).extend(resolutions)
    return resolutions
