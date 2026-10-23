"""Measure actual selected native ink without moving or resizing source text.

Keep structural/nominal boxes separate from the measured capture envelope.
Use the pinned public native-operator observer, original embedded font program,
native glyph ID and complete effective 2x2 transform. Never synthesize Unicode
or choose one of several distinct same-name embedded font programs.
"""
from collections import OrderedDict, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata

import pymupdf as fitz
from .native_clipping import record_native_text_operators, _font_name, _raw_glyphs, verify_selected_native_glyphs, _font_resource_indices, _font_program_candidates

SCALE = 8


def _name(value):
    return re.sub(r'[^a-z0-9]', '', _font_name(value).lower())


def capture_grid(rectangle):
    """Outward 1/8pt enclosure of a measured 8px/pt envelope, not padding."""
    r = fitz.Rect(rectangle)
    return fitz.Rect(math.floor(r.x0*SCALE)/SCALE, math.floor(r.y0*SCALE)/SCALE,
                     math.ceil(r.x1*SCALE)/SCALE, math.ceil(r.y1*SCALE)/SCALE)


def _references(value):
    return {int(xref) for xref in re.findall(r'\b(\d+)\s+\d+\s+R\b',value)}


def _resource_state(page,cache):
    """Strict mutable-document path: actual transitive resource identities.

    Sources declared immutable use whole-file attestation instead. The slow
    path hashes Forms, their Resources and referenced font programs; a direct
    page-content digest alone cannot detect a nested Form being rewritten.
    """
    document=page.parent
    fonts=page.get_fonts(full=True)
    forms=page.get_xobjects()
    resource='null';parent=page.xref;seen=set()
    while parent and parent not in seen:
        seen.add(parent)
        _,resource=document.xref_get_key(parent,'Resources')
        if resource!='null':break
        _,value=document.xref_get_key(parent,'Parent')
        refs=_references(value)
        if len(refs)>1:raise ValueError('page_resource_inheritance_ambiguous')
        parent=next(iter(refs),0)
    seeds=set(page.get_contents())|_references(resource)|{row[0] for row in fonts}|{row[0] for row in forms}
    nodes={};children={};pending=list(seeds)
    while pending:
        xref=pending.pop()
        if xref in nodes:continue
        if not 0<xref<document.xref_length():raise ValueError('resource_dependency_reference_unverified')
        if len(nodes)>=10000:raise ValueError('resource_dependency_graph_bound_reached')
        raw=document.xref_object(xref,compressed=False)
        stream=document.xref_stream_raw(xref) if document.xref_is_stream(xref) else b''
        nodes[xref]=hashlib.sha256(raw.encode('utf8')+b'\0'+stream).hexdigest()
        children[xref]=_references(raw)
        pending.extend(children[xref]-nodes.keys())
    cache['stats']['dependency_objects_hashed']+=len(nodes)
    page_state={'contents':page.get_contents(),'resources':resource,'fonts':fonts,'forms':forms,
        'matrix':list(page.transformation_matrix),'rect':list(page.rect),
        'mediabox':list(page.mediabox),'cropbox':list(page.cropbox),'nodes':sorted(nodes.items())}
    signature=hashlib.sha256(json.dumps(page_state,sort_keys=True).encode()).hexdigest()
    def font_state(xref):
        reached=set();todo=[xref]
        while todo:
            value=todo.pop()
            if value in reached:continue
            reached.add(value);todo.extend(children[value]-reached)
        return hashlib.sha256(json.dumps(sorted((value,nodes[value]) for value in reached)).encode()).hexdigest()
    return signature,fonts,{row[0]:font_state(row[0]) for row in fonts}


def _immutable_state(document,cache,declared_sha256):
    attested=cache.get('immutable_source')
    if declared_sha256 is None and attested is None:return None
    if declared_sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}',declared_sha256):
        raise ValueError('invalid_immutable_source_identity')
    if document.is_dirty:raise ValueError('declared_immutable_source_document_was_mutated')
    path=Path(document.name).resolve() if document.name else None
    if path is None or not path.is_file():raise ValueError('immutable_source_requires_saved_original_pdf')
    stat=path.stat();identity=(str(path),stat.st_size,stat.st_mtime_ns)
    if attested is not None:
        if identity!=attested['file_stat'] or (declared_sha256 is not None and declared_sha256!=attested['sha256']):
            raise ValueError('declared_immutable_source_identity_changed')
        return attested['sha256']
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    cache['stats']['immutable_source_file_hashes']+=1
    if digest.hexdigest()!=declared_sha256:raise ValueError('declared_immutable_source_file_sha_mismatch')
    cache['immutable_source']={'sha256':declared_sha256,'file_stat':identity}
    return declared_sha256


def _program(cache,document,resource):
    key=resource['resource_key'];xref=resource['xref']
    if key not in cache['font_resources']:
        data=document.extract_font(xref)[3]
        cache['stats']['font_program_extract_calls']+=1
        sha=hashlib.sha256(data).hexdigest() if data else None
        cache['font_resources'][key]=(sha,data)
        while len(cache['font_resources'])>256:cache['font_resources'].popitem(last=False)
    cache['font_resources'].move_to_end(key)
    sha,data=cache['font_resources'][key]
    previous=cache['font_xref_identities'].get(xref)
    if xref in cache['font_xref_identities'] and previous!=sha:
        raise ValueError('mutable_embedded_font_program_changed')
    cache['font_xref_identities'][xref]=sha
    return {**resource,'sha256':sha,'data':data}


def _index(page,immutable_source_sha256=None,*,fresh=False):
    if fitz.VersionBind != '1.28.2':
        raise ValueError('native_ink_requires_pinned_pymupdf_1.28.2')
    if page.rotation % 360:
        raise ValueError('page_rotation_coordinate_mapping_unverified')
    document = page.parent
    if not hasattr(document, '_textbook_capture_ink_v2'):
        document._textbook_capture_ink_v2 = {'pages':OrderedDict(),'fonts':OrderedDict(),'boxes':OrderedDict(),
            'font_resources':OrderedDict(),'font_xref_identities':{},'stats':{'font_program_extract_calls':0,'native_operator_page_reads':0,
                'dependency_objects_hashed':0,'immutable_source_file_hashes':0}}
    cache = document._textbook_capture_ink_v2
    attested=None if fresh else _immutable_state(document,cache,immutable_source_sha256)
    if fresh:
        # Actual target checking deliberately does not reuse a page index.
        # Extract only the selected fonts lazily, once within this comparison.
        signature=None;nonce=object();fonts=page.get_fonts(full=True)
        resource_keys={row[0]:('fresh-no-reuse',nonce,row[0]) for row in fonts}
    elif attested is not None:
        signature=('whole-file-immutable',attested,page.number)
        if signature in cache['pages']:
            cache['pages'].move_to_end(signature)
            return cache,cache['pages'][signature]
        fonts=page.get_fonts(full=True)
        resource_keys={row[0]:('immutable',attested,row[0]) for row in fonts}
    else:
        state,fonts,identities=_resource_state(page,cache)
        signature=('mutable-transitive-resources',page.number,state)
        if signature in cache['pages']:
            cache['pages'].move_to_end(signature)
            return cache,cache['pages'][signature]
        resource_keys={xref:('mutable',xref,value) for xref,value in identities.items()}
    programs = []
    for row in fonts:
        programs.append({'xref':row[0],'name':row[3],'resource_key':resource_keys[row[0]]})
    traces = defaultdict(list)
    for span in page.get_texttrace():
        for char in span['chars']:
            if 0 <= char[0] <= 0x10ffff:
                traces[(chr(char[0]), _name(span['font']))].append((span, char))
    operators = defaultdict(list)
    for row in record_native_text_operators(page):
        operators[row['character']].append(row)
    cache['stats']['native_operator_page_reads']+=1
    index = {'programs': programs, 'traces': traces, 'operators': operators}
    if signature is not None:
        cache['pages'][signature] = index
        while len(cache['pages']) > 2:
            cache['pages'].popitem(last=False)
    return cache, index


def _measure(cache, index, glyph, source_glyphs,document):
    if len(glyph['c']) != 1:
        raise ValueError('multicodepoint_native_mapping_unverified')
    available = index['traces'][(glyph['c'], _name(glyph['font']))]
    matches = [(span, char) for span, char in available
               if max(abs(char[2][n]-glyph['origin'][n]) for n in (0, 1)) <= .001]
    binding = 'unique_rawdict_trace_operator_origin'
    nominal = fitz.Rect(glyph.get('nominal_bbox', glyph['bbox']))
    zero = abs(nominal.width) < 1e-8 or abs(nominal.height) < 1e-8
    if not matches and zero and unicodedata.category(glyph['c']).startswith('M'):
        # Preserve the public zero-advance exception only with a globally
        # unique source mark and trace; never pair marks by nearest distance.
        logical = [g for g in source_glyphs if g['c']==glyph['c'] and _name(g['font'])==_name(glyph['font'])
                   and abs(g['size']-glyph['size']) <= 1e-4]
        if len(logical)==len(available)==1:
            matches = available
            binding = 'unique_zero_advance_combining_trace_operator'
    if len(matches) != 1:
        raise ValueError('native_trace_identity_ambiguous_or_absent')
    span, char = matches[0]
    rows = [row for row in index['operators'][glyph['c']]
            if max(abs(row['effective_matrix'][n+4]-char[2][n]) for n in (0, 1)) <= .001]
    if len(rows) != 1:
        raise ValueError('native_operator_identity_ambiguous_or_absent')
    operator = rows[0]
    if operator['render_mode'] != 0 or abs(operator['fill_alpha']-1) > 1e-6:
        raise ValueError('nonstandard_native_render_mode_or_alpha_unverified')
    matrix = operator['effective_matrix']
    if not all(math.isfinite(n) for n in matrix) or abs(matrix[0]*matrix[3]-matrix[1]*matrix[2]) < 1e-12:
        raise ValueError('nonfinite_or_singular_native_matrix')
    loaded={}
    def load_resource(i):
        if i not in loaded:
            loaded[i]=_program(cache,document,index['programs'][i])
        return loaded[i]['data']
    selected_resources=_font_program_candidates(glyph['font'],[p['name'] for p in index['programs']],load_resource)
    resources=[loaded[i] for i,_ in selected_resources]
    if not resources or any(not p['sha256'] for p in resources):
        raise ValueError('original_embedded_font_program_unavailable')
    identities = {p['sha256'] for p in resources}
    if len(identities) != 1:
        raise ValueError('distinct_same_name_font_subset_programs_ambiguous')
    sha = next(iter(identities))
    if sha not in cache['fonts']:
        cache['fonts'][sha] = fitz.Font(fontbuffer=resources[0]['data'])
        while len(cache['fonts'])>128:cache['fonts'].popitem(last=False)
    cache['fonts'].move_to_end(sha)
    key = (sha, char[1], tuple(matrix[:4]))
    if key not in cache['boxes']:
        low = fitz.mupdf.fz_render_glyph_pixmap(cache['fonts'][sha].this, char[1],
            fitz.mupdf.FzMatrix(*(n*SCALE for n in matrix[:4]), 0, 0),
            fitz.mupdf.FzIrect(-4096, -4096, 4096, 4096), 8)
        pix = fitz.Pixmap(low)
        if not pix.width or not pix.height or not any(pix.samples):
            raise ValueError('nonspace_native_glyph_has_no_ink')
        if abs(pix.x) >= 4095 or abs(pix.y) >= 4095 or pix.width >= 8190 or pix.height >= 8190:
            raise ValueError('native_glyph_raster_bound_reached')
        cache['boxes'][key] = (pix.x/SCALE, pix.y/SCALE,
                               (pix.x+pix.width)/SCALE, (pix.y+pix.height)/SCALE)
        while len(cache['boxes']) > 4096:
            cache['boxes'].popitem(last=False)
    cache['boxes'].move_to_end(key)
    x0, y0, x1, y1 = cache['boxes'][key]
    box = [matrix[4]+x0, matrix[5]+y0, matrix[4]+x1, matrix[5]+y1]
    return box, {'schema':'original-native-full-matrix-ink-v2',
        'font_program_sha256':sha, 'font_resource_xrefs':[p['xref'] for p in resources],
        'native_glyph_id':char[1], 'effective_matrix':list(matrix),
        'pixels_per_point':SCALE, 'scalar_size_approximation':False,
        'rawdict_size':glyph['size'], 'trace_size':span['size'],
        'source_trace_color':span.get('color'), 'source_trace_colorspace':span.get('colorspace'),
        'source_trace_wmode':span.get('wmode'), 'source_trace_dir':span.get('dir'),
        'source_trace_state':{key:span.get(key)for key in ('color','colorspace','opacity','type','wmode','dir')},
        'source_operator_state':{key:operator[key]for key in ('render_mode','fill_alpha','stroke_alpha')},
        'origin_binding':binding, 'structural_bbox_unchanged':True}


def measure_selected_native_ink(page, glyphs, selected_ids,*,immutable_source_sha256=None):
    """Attach capture evidence only; never overwrite bbox/origin/font/size."""
    for glyph in glyphs:
        if glyph['id'] in selected_ids:
            glyph.pop('native_ink_bbox',None)
            glyph.pop('native_ink_geometry',None)
    selected = [g for g in glyphs if g['id'] in selected_ids and g['c'].strip()]
    result = {'verified':[], 'unverified':[], 'pixels_per_point':SCALE,
              'capture_grid_pt':1/SCALE, 'structural_geometry_changed':False}
    counts={gid:sum(g['id']==gid for g in glyphs) for gid in selected_ids}
    invalid=[gid for gid,n in counts.items() if n!=1]
    if invalid:
        result['unverified']=[{'glyph_id':gid,'reason':'selected_source_id_missing_or_duplicated'} for gid in invalid]
        return result
    try:
        cache, index = _index(page,immutable_source_sha256)
    except (ValueError, RuntimeError, TypeError, AttributeError, OverflowError) as error:
        result['unverified'] = [{'glyph_id':g['id'], 'reason':str(error)} for g in selected]
        return result
    for glyph in selected:
        try:
            box, proof = _measure(cache, index, glyph, glyphs,page.parent)
            glyph['native_ink_bbox'] = box
            glyph['native_ink_geometry'] = proof
            result['verified'].append({'glyph_id':glyph['id'], 'ink_bbox':box, 'identity':proof})
        except (ValueError, RuntimeError, TypeError, AttributeError, OverflowError) as error:
            result['unverified'].append({'glyph_id':glyph['id'], 'reason':str(error)})
    return result


def verify_actual_capture(source_page,target_page,glyphs,selected_ids,*,source_to_target,expected_dimensions=None):
    """New strong gate on the actual outer Form target, before serialization.

    The public legacy check is an identity layer only. Strong proof freshly
    observes target operators/fonts and compares complete native state.
    """
    selected=[g for g in glyphs if g['id']in selected_ids and g['c'].strip()]
    shift=[float(n)for n in source_to_target]
    result={'schema':'actual-target-native-ink-state-v1','verified':False,'records':[],'failures':[],
            'source_to_target_translation':shift,'whole_formula_accepted':False,
            'origin_tolerance_pt':.001,'linear_matrix_tolerance':1e-5,'raw_size_tolerance_pt':1e-4,
            'target_page_index_reused':False}
    try:
        if len(shift)!=2 or not all(math.isfinite(n)for n in shift):
            raise ValueError('actual_target_translation_invalid')
        result['actual_target_dimensions']=[target_page.rect.width,target_page.rect.height]
        if expected_dimensions is not None and max(abs(result['actual_target_dimensions'][n]-expected_dimensions[n])for n in (0,1))>.0002:
            raise ValueError('actual_target_dimensions_changed')
        weak=verify_selected_native_glyphs(source_page,target_page,selected,target_offset=[-n for n in shift])
        result['legacy_identity_layer']=weak
        if not weak['verified']or weak['unselected_target_glyph_count']:
            raise ValueError('actual_target_legacy_identity_or_foreign_glyph_failure')
        cache,index=_index(target_page,fresh=True)
        target=[{**g,'id':f'capture-target-{i}'}for i,g in enumerate(_raw_glyphs(target_page))]
        operator_count=sum(len(rows)for char,rows in index['operators'].items()if char.strip())
        trace_count=sum(len(rows)for(char,font),rows in index['traces'].items()if char.strip())
        result.update(actual_target_nonempty_operators=operator_count,actual_target_nonempty_traces=trace_count)
        if operator_count!=len(selected)or trace_count!=len(selected)or len(target)!=len(selected):
            raise ValueError('actual_target_native_count_or_foreign_glyph_failure')
        used=set()
        for source in selected:
            original=source.get('native_ink_geometry')
            if not original:raise ValueError('actual_target_source_ink_identity_not_verified')
            matrix=original['effective_matrix'];expected=[matrix[n+4]+shift[n]for n in (0,1)]
            rows=[g for g in target if g['id']not in used and g['c']==source['c']and _name(g['font'])==_name(source['font'])
                  and max(abs(g['origin'][n]-expected[n])for n in (0,1))<=.001]
            if not rows and unicodedata.category(source['c']).startswith('M'):
                nominal=fitz.Rect(source.get('nominal_bbox',source['bbox']))
                if abs(nominal.width)<1e-8 or abs(nominal.height)<1e-8:
                    rows=[g for g in target if g['id']not in used and g['c']==source['c']and _name(g['font'])==_name(source['font'])]
            if len(rows)!=1:raise ValueError('actual_target_raw_identity_or_origin_ambiguous')
            current=rows[0]
            box,actual=_measure(cache,index,current,target,target_page.parent)
            delta=max(abs(actual['effective_matrix'][n]-matrix[n])for n in range(4))
            if delta>1e-5:raise ValueError('actual_target_linear_matrix_changed')
            origin=max(abs(actual['effective_matrix'][n+4]-expected[n])for n in (0,1))
            if origin>.001:raise ValueError('actual_target_native_origin_changed')
            if actual['native_glyph_id']!=original['native_glyph_id']:raise ValueError('actual_target_native_gid_changed')
            if actual['font_program_sha256']!=original['font_program_sha256']:raise ValueError('actual_target_font_program_changed')
            size_delta=abs(current['size']-source['size'])
            if size_delta>1e-4:raise ValueError('actual_target_original_raw_size_changed')
            if actual['source_trace_state']!=original['source_trace_state']or actual['source_operator_state']!=original['source_operator_state']:
                raise ValueError('actual_target_original_render_state_changed')
            rect=target_page.rect;enclosure=fitz.Rect(box)
            if enclosure.x0<rect.x0-.0002 or enclosure.y0<rect.y0-.0002 or enclosure.x1>rect.x1+.0002 or enclosure.y1>rect.y1+.0002:
                raise ValueError('actual_target_native_ink_support_outside_visible_page')
            used.add(current['id'])
            result['records'].append({'source_glyph_id':source['id'],'target_raw_id':current['id'],
                'source_identity_sha256':hashlib.sha256(json.dumps(original,sort_keys=True).encode()).hexdigest(),
                'actual_target_font_program_sha256':actual['font_program_sha256'],
                'actual_target_native_gid':actual['native_glyph_id'],
                'actual_target_effective_matrix':actual['effective_matrix'],
                'actual_target_rawdict_size':current['size'],
                'actual_target_render_state':actual['source_trace_state'],
                'actual_target_ink_bbox':box,'linear_matrix_delta':delta,'native_origin_delta_pt':origin,
                'raw_size_delta_pt':size_delta,'actual_ink_enclosed':True})
        result['verified']=len(used)==len(selected)
    except (ValueError,RuntimeError,KeyError,TypeError,AttributeError,OverflowError)as error:
        result['failures'].append({'reason':str(error),'error_type':type(error).__name__})
    return result
