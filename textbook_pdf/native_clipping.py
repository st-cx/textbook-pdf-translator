"""Remove outside native PDF content without deleting zero-advance math marks.

MuPDF 1.28.2 builds glyph culling boxes from advance width, so a combining
negation slash can have an empty box despite visible ink. Its stock hard clip
deletes that glyph at any crop size. This pinned adapter retains the unchanged
native text operator when its zero-width origin lies within the cleanup region.

Based on the documented SWIG filter pattern in PyMuPDF 1.28.2 and checked
against MuPDF 1.28.2 source/pdf/pdf-clean.c and pdf-op-filter.c (AGPL-3.0).
It does not redraw Unicode, alter font metrics, or leave the original page's
outside content hidden in the resulting Form XObject.
"""
from __future__ import annotations

import pymupdf as fitz
from collections import Counter,defaultdict
import ctypes
import hashlib
import math
import re
import unicodedata


class _NativeClipOptions(fitz.mupdf.PdfSanitizeFilterOptions2):
    def __init__(self, rectangle):
        super().__init__()
        self.rectangle=fitz.Rect(rectangle)
        self.kept_zero_advance=0
        self.removed_zero_advance=0
        self.use_virtual_culler()

    def culler(self, opaque, bbox, kind):
        rect=fitz.Rect(fitz.mupdf.FzRect(bbox))
        clip=self.rectangle
        zero_x,zero_y=abs(rect.width)<1e-8,abs(rect.height)<1e-8
        if kind==fitz.mupdf.FZ_CULL_GLYPH and (zero_x or zero_y):
            horizontal=(clip.x0<=rect.x0<=clip.x1 if zero_x else min(rect.x1,clip.x1)>max(rect.x0,clip.x0))
            vertical=(clip.y0<=rect.y0<=clip.y1 if zero_y else min(rect.y1,clip.y1)>max(rect.y0,clip.y0))
            keep=horizontal and vertical
            if keep:self.kept_zero_advance+=1
            else:self.removed_zero_advance+=1
            return int(not keep)
        return int((rect&clip).is_empty)


class _SanitizeFactory(fitz.mupdf.PdfFilterFactory2):
    def __init__(self, options):
        super().__init__()
        self.options_object=options
        self.use_virtual_filter()

    def filter(self, ctx, doc, chain, struct_parents, transform, options):
        return fitz.mupdf.ll_pdf_new_sanitize_filter(doc,chain,struct_parents,transform,options,
                                                    self.options_object.internal())


class _NativeOperatorRecorder(fitz.mupdf.PdfSanitizeFilterOptions2):
    def __init__(self, transformation, characters):
        super().__init__()
        self.transformation=transformation
        self.characters=characters
        self.records=[]
        self.use_virtual_text_filter()
        self.use_virtual_culler()

    def culler(self,*args):
        return 0

    def text_filter(self,opaque,buffer,length,trm,ctm,bbox,tr,ca,CA):
        if not 0<length<=16 or not int(buffer):return 0
        values=ctypes.cast(int(buffer),ctypes.POINTER(ctypes.c_int))
        codes=tuple(values[n] for n in range(length))
        if len(codes)!=1 or not 0<=codes[0]<=0x10ffff:return 0
        character=chr(codes[0])
        if self.characters is not None and character not in self.characters:return 0
        text=fitz.Matrix(trm.a,trm.b,trm.c,trm.d,trm.e,trm.f)
        content=fitz.Matrix(ctm.a,ctm.b,ctm.c,ctm.d,ctm.e,ctm.f)
        effective=text*content*self.transformation
        if not all(math.isfinite(x) for x in effective):return 0
        self.records.append({'character':character,'effective_matrix':list(effective),
            'render_mode':int(tr),'fill_alpha':float(ca),'stroke_alpha':float(CA)})
        return 0


def record_native_text_operators(page, characters=None):
    """Observe effective native text matrices in a disposable copy of a page.

    A zero-advance combining mark's rawdict *logical* origin can depend on
    adjacent text. The sanitizer observes its actual PDF operator instead.
    Neither the caller's page nor its PDF objects are edited. Only single-code
    point operators are exposed; ambiguous/unsupported mappings cannot prove
    a combining identity. This API requires the pinned native wrapper.
    """
    if fitz.VersionBind!='1.28.2':
        raise RuntimeError('Native operator identity requires PyMuPDF 1.28.2')
    if page.rotation%360:
        raise ValueError('Native operator identity requires normalized page rotation')
    with fitz.open() as copy:
        copy.insert_pdf(page.parent,from_page=page.number,to_page=page.number)
        current=copy[0]
        observer=_NativeOperatorRecorder(current.transformation_matrix,
                                         None if characters is None else set(characters))
        factory=_SanitizeFactory(observer)
        options=fitz.mupdf.PdfFilterOptions()
        options.recurse=1;options.instance_forms=1;options.ascii=0;options.no_update=1
        options.add_factory(factory.internal())
        native=fitz._as_pdf_page(current)
        fitz.mupdf.pdf_filter_page_contents(native.doc(),native,options)
        return observer.records


def _font_name(value):
    return value.split('+')[-1][:31]


def _raw_glyphs(page):
    raw=page.get_text('rawdict',clip=fitz.INFINITE_RECT(),
                      flags=fitz.TEXT_PRESERVE_LIGATURES|fitz.TEXT_PRESERVE_WHITESPACE)
    return [{**char,'font':span['font'],'size':span['size']}
            for block in raw['blocks'] for line in block.get('lines',[])
            for span in line['spans'] for char in span['chars'] if char['c'].strip()]


def _zero_advance(rectangle):
    rectangle=fitz.Rect(rectangle)
    return (all(math.isfinite(x) for x in rectangle)
            and (abs(rectangle.width)<1e-8 or abs(rectangle.height)<1e-8))


def _point_delta(a,b):
    return max(abs(float(a[n])-float(b[n])) for n in (0,1))


def _font_resource_indices(reported_name, resource_names):
    """Exact full PostScript identity first; only genuine 31-char reports may prefix.

    Callers supply the original rawdict/trace name, never _font_name()'s
    artificially truncated output. Matching names only selects resources;
    program identity/missing-program rejection remains the caller's job.
    """
    short = str(reported_name).split('+')[-1]
    normalized = lambda value: re.sub(r'[^a-z0-9]', '', str(value).split('+')[-1].lower())
    reported = normalized(short)
    if not reported:
        return []
    names = [normalized(value) for value in resource_names]
    exact = [i for i, value in enumerate(names) if value == reported]
    if exact:
        return exact
    selected = [i for i, value in enumerate(names)
                if len(short) == 31 and len(value) > len(reported) and value.startswith(reported)]
    # MuPDF can truncate the original name to31 *before* removing a six-letter
    # subset tag. Then a genuine31 report appears as24 characters in rawdict.
    # Match that exact reproduced representation, never any short prefix.
    for i, value in enumerate(resource_names):
        value = str(value)
        if (re.match(r'^[A-Z]{6}\+', value) and len(value) > 31
                and normalized(value[:31]) == reported and i not in selected):
            selected.append(i)
    return selected


def _embedded_postscript_names(data):
    """Read identity metadata only; bounded SFNT nameID6 or CFF1 NameINDEX.

    No outline interpretation, font substitution, Type1 guessing or TTC face
    selection. Malformed/unsupported metadata supplies no identity evidence.
    """
    def number(offset, size, end):
        if offset < 0 or size < 1 or offset+size > end:
            raise ValueError('font_metadata_out_of_bounds')
        return int.from_bytes(data[offset:offset+size], 'big')
    def valid(value):
        return (value and all(33 <= ord(c) <= 126 and c not in '[](){}<>/%' for c in value))
    try:
        if not data or len(data) < 4:
            return ()
        values=set()
        if data[:4] in (b'\x00\x01\x00\x00', b'OTTO', b'true', b'typ1'):
            tables=number(4,2,len(data))
            if not 1 <= tables <= 4096 or 12+16*tables > len(data):
                return ()
            name_tables=[]
            for i in range(tables):
                off=12+16*i
                if data[off:off+4] == b'name':
                    start=number(off+8,4,len(data));size=number(off+12,4,len(data))
                    if size < 6 or start+size > len(data):
                        return ()
                    name_tables.append((start,start+size))
            if len(name_tables) != 1:
                return ()
            start,end=name_tables[0]
            if number(start,2,end) not in (0,1):
                return ()
            count=number(start+2,2,end);strings=start+number(start+4,2,end)
            if count > 4096 or start+6+12*count > end or not start+6+12*count <= strings <= end:
                return ()
            for i in range(count):
                pos=start+6+12*i
                if number(pos+6,2,end) != 6:
                    continue
                platform=number(pos,2,end);size=number(pos+8,2,end);offset=strings+number(pos+10,2,end)
                if offset < strings or offset+size > end:
                    return ()
                raw=data[offset:offset+size]
                if platform in (0,3):
                    value=raw.decode('utf-16-be')
                elif platform == 1:
                    value=raw.decode('mac_roman')
                else:
                    return ()
                if not valid(value):
                    return ()
                values.add(value)
        elif data[0] == 1:
            start=data[2]
            if start < 4 or start+2 > len(data):
                return ()
            count=number(start,2,len(data))
            if not 1 <= count <= 256:
                return ()
            width=number(start+2,1,len(data))
            if not 1 <= width <= 4:
                return ()
            index=start+3;base=index+width*(count+1)
            offsets=[number(index+i*width,width,len(data)) for i in range(count+1)]
            if offsets[0] != 1 or any(a > b for a,b in zip(offsets,offsets[1:])) or base+offsets[-1]-1 > len(data):
                return ()
            for a,b in zip(offsets,offsets[1:]):
                value=data[base+a-1:base+b-1].decode('ascii')
                if not valid(value):
                    return ()
                values.add(value)
        return tuple(sorted(values))
    except (ValueError,IndexError,UnicodeError,TypeError,OverflowError):
        return ()


def _font_program_candidates(reported_name, resource_names, loader):
    """Keep named matches strict; otherwise require exact real PS metadata.

    ``loader(index)`` returns actual program bytes. The native-ink caller routes
    it through its existing per-document/xref cache, never a second extractor.
    A BaseFont exact/matching resource with missing or distinct programs remains
    a failure; unrelated metadata cannot silently override that evidence.
    """
    names=tuple(resource_names)
    selected=_font_resource_indices(reported_name,names)
    if selected:
        return [(i,loader(i)) for i in selected]
    identity=lambda value:re.sub(r'^[A-Z]{6}\+', '',str(value))
    reported=identity(reported_name)
    if not reported:
        return []
    result=[]
    for i in range(len(names)):
        data=loader(i);postscript=_embedded_postscript_names(data)
        if len(postscript) == 1 and identity(postscript[0]) == reported:
            result.append((i,data))
    return result


def _embedded_font_sha(page,name):
    identities=set()
    # PDF BaseFont can contain the spaced family name, while rawdict/trace
    # reports a compact (and possibly 31-character-truncated) PostScript name.
    # Ambiguous programs remain rejected even when their names share a prefix.
    records=page.get_fonts(full=True)
    candidates=_font_program_candidates(name,[record[3] for record in records],
        lambda index:page.parent.extract_font(records[index][0])[3])
    for index,program in candidates:
        if not program:raise ValueError('embedded_font_program_unavailable')
        identities.add(hashlib.sha256(program).hexdigest())
    if len(identities)!=1:raise ValueError('embedded_font_program_ambiguous')
    return next(iter(identities))


def _combining_trace(page,glyph):
    # A global unique match is deliberately conservative. Repeated marks on a
    # page must not be paired by nearest distance or guessed extraction order.
    return [{'glyph_id':int(char[1]),'origin':list(char[2]),'bbox':list(char[3])}
            for span in page.get_texttrace()
            if _font_name(span['font'])==_font_name(glyph['font'])
            and abs(float(span['size'])-float(glyph['size']))<=1e-4
            for char in span['chars'] if char[0]==ord(glyph['c'])]


def verify_selected_native_glyphs(source_page,target_page,selected_glyphs,*,target_offset=(0,0)):
    """Check selected original identities, with a strict combining-only fallback.

    Ordinary glyphs keep the existing char/font + 0.03pt rawdict rule. A failed
    zero-advance Unicode combining mark additionally needs a unique original
    and target native glyph ID, identical embedded font program, render mode,
    opacity and effective operator matrix. The actual operator's translation
    tolerance is only 0.001pt and its linear tolerance 1e-5. ``target_offset``
    accounts only for a known crop translation; scaling/rotation is not inferred.

    Unselected non-whitespace target glyphs remain explicitly counted. This
    identity check alone does not certify their visible isolation or pixels.
    """
    selected=[g for g in selected_glyphs if g['c'].strip()]
    if len({g['id'] for g in selected})!=len(selected):
        raise ValueError('Selected native glyph identities are duplicated')
    offset=tuple(float(n) for n in target_offset)
    if len(offset)!=2 or not all(math.isfinite(n) for n in offset):
        raise ValueError('Native crop offset must contain two finite coordinates')
    actual=_raw_glyphs(target_page)
    available=set(range(len(actual)))
    missing=[];evidence=[];normal=0;fallback=0
    source_raw=None;source_operators=None;target_operators=None
    def same_identity(a,b):
        return a['c']==b['c'] and _font_name(a['font'])==_font_name(b['font'])
    for glyph in selected:
        candidates=[i for i in available if same_identity(glyph,actual[i])]
        shifted=lambda char:(char['origin'][0]+offset[0],char['origin'][1]+offset[1])
        nearest=min(candidates,key=lambda i:_point_delta(shifted(actual[i]),glyph['origin']),default=None)
        if nearest is not None and _point_delta(shifted(actual[nearest]),glyph['origin'])<=.03:
            available.remove(nearest);normal+=1;continue
        detail={'glyph_id':glyph['id'],'classification':'rejected','reason':'ordinary_rawdict_mismatch'}
        if (len(glyph['c'])!=1 or not unicodedata.category(glyph['c']).startswith('M')
            or not _zero_advance(glyph.get('nominal_bbox',glyph['bbox']))):
            missing.append(glyph['id']);continue
        try:
            if source_raw is None:source_raw=_raw_glyphs(source_page)
            original=[g for g in source_raw if same_identity(glyph,g)
                and abs(float(g['size'])-float(glyph['size']))<=1e-4
                and _point_delta(g['origin'],glyph['origin'])<=.03]
            if len(original)!=1 or not _zero_advance(original[0]['bbox']):
                raise ValueError('source_rawdict_identity_unproven')
            possible=[i for i in candidates
                if abs(float(actual[i]['size'])-float(glyph['size']))<=1e-4
                and _zero_advance(actual[i]['bbox'])]
            if len(possible)!=1:raise ValueError('target_combining_identity_ambiguous')
            source_trace=_combining_trace(source_page,glyph)
            target_trace=_combining_trace(target_page,glyph)
            if len(source_trace)!=1 or len(target_trace)!=1:
                raise ValueError('native_combining_trace_ambiguous')
            before,after=source_trace[0],target_trace[0]
            if (before['glyph_id']!=after['glyph_id']
                or not _zero_advance(before['bbox']) or not _zero_advance(after['bbox'])):
                raise ValueError('native_glyph_identity_changed')
            source_font=_embedded_font_sha(source_page,glyph['font'])
            target_font=_embedded_font_sha(target_page,glyph['font'])
            detail.update(source_font_sha256=source_font,target_font_sha256=target_font,
                          native_glyph_id=before['glyph_id'])
            if source_font!=target_font:raise ValueError('font_program_changed')
            if source_operators is None:
                chars={g['c'] for g in selected if len(g['c'])==1 and unicodedata.category(g['c']).startswith('M')}
                source_operators=record_native_text_operators(source_page,chars)
                target_operators=record_native_text_operators(target_page,chars)
            def bind(records,trace):
                return [r for r in records if r['character']==glyph['c']
                        and _point_delta(r['effective_matrix'][4:],trace['origin'])<=.001]
            src=bind(source_operators,before);dst=bind(target_operators,after)
            if len(src)!=1 or len(dst)!=1:raise ValueError('native_operator_identity_ambiguous')
            src,dst=src[0],dst[0]
            before_matrix=src['effective_matrix'];after_matrix=list(dst['effective_matrix'])
            after_matrix[4]+=offset[0];after_matrix[5]+=offset[1]
            position=_point_delta(before_matrix[4:],after_matrix[4:])
            linear=max(abs(before_matrix[n]-after_matrix[n]) for n in range(4))
            detail.update(source_effective_matrix=before_matrix,target_effective_matrix=after_matrix,
                operator_origin_delta_pt=position,operator_linear_delta=linear,
                rawdict_origin_delta_pt=_point_delta(shifted(actual[possible[0]]),glyph['origin']))
            if position>.001:raise ValueError('operator_position_changed')
            if linear>1e-5:raise ValueError('operator_linear_matrix_changed')
            if (src['render_mode']!=dst['render_mode']
                or abs(src['fill_alpha']-dst['fill_alpha'])>1e-6
                or abs(src['stroke_alpha']-dst['stroke_alpha'])>1e-6):
                raise ValueError('operator_render_state_changed')
            available.remove(possible[0]);fallback+=1
            detail.update(classification='verified_zero_advance_combining_operator',reason=None)
        except (ValueError,RuntimeError,KeyError,TypeError,AttributeError,OverflowError) as error:
            detail['reason']=str(error);missing.append(glyph['id'])
        evidence.append(detail)
    return {'schema':'selected-native-glyph-identity-v1','selected_glyphs':len(selected),
            'missing_ids':missing,'verified':not missing,'matched_by_rawdict':normal,
            'matched_by_native_operator':fallback,'native_operator_evidence':evidence,
            'unselected_target_glyph_count':len(available),
            'limitations':['Identity does not certify visible isolation, pixels, or mathematical meaning.']}


class _SelectedNativeOptions(_NativeClipOptions):
    """Keep only uniquely identified original text operators, without redaction."""
    def __init__(self, rectangle, transformation, selected, source):
        super().__init__(rectangle)
        self.transformation=transformation
        self.selected={g['id'] for g in selected if g['c'].strip()}
        self.by_character=defaultdict(list)
        for glyph in source:
            if glyph['c'].strip():self.by_character[glyph['c']].append(glyph)
        self.seen=Counter();self.ambiguous=0;self.removed=0
        self.use_virtual_text_filter()

    def text_filter(self,opaque,ucsbuf,ucslen,trm,ctm,bbox,tr,ca,CA):
        # MuPDF owns this int[ucslen] for the callback's lifetime. Never retain
        # the pointer, read beyond its supplied bound, or dereference a null.
        if not 0<ucslen<=16 or not int(ucsbuf):
            self.removed+=1;return 1
        values=ctypes.cast(int(ucsbuf),ctypes.POINTER(ctypes.c_int))
        sequence=tuple(values[n] for n in range(ucslen))
        if len(sequence)!=1 or not 0<=sequence[0]<=0x10ffff:
            self.removed+=1;return 1
        char=chr(sequence[0])
        if not char.strip():self.removed+=1;return 1
        text_matrix=fitz.Matrix(trm.a,trm.b,trm.c,trm.d,trm.e,trm.f)
        content_matrix=fitz.Matrix(ctm.a,ctm.b,ctm.c,ctm.d,ctm.e,ctm.f)
        origin=fitz.Point(0,0)*text_matrix*content_matrix*self.transformation
        candidates=[g for g in self.by_character.get(char,[])
                    if max(abs(g['origin'][0]-origin.x),abs(g['origin'][1]-origin.y))<=.03]
        if len(candidates)!=1:
            if any(g['id'] in self.selected for g in candidates):self.ambiguous+=1
            self.removed+=1;return 1
        glyph=candidates[0]
        if glyph['id'] in self.selected:
            self.seen[glyph['id']]+=1;return 0
        self.removed+=1;return 1


def clip_native_content(page, rectangle, *, preserve_zero_advance=True, selected_glyphs=None, source_glyphs=None):
    """Mutate a private page copy only; the caller supplies the final visible clip."""
    if fitz.VersionBind!='1.28.2':
        raise RuntimeError('Native zero-advance clipping requires the pinned PyMuPDF 1.28.2 adapter')
    if page.rotation%360:
        raise ValueError('Native PDF page rotation requires coordinate normalization before protected clipping; source content was not changed')
    rectangle=fitz.Rect(rectangle)&page.rect
    if rectangle.is_empty or rectangle.is_infinite:
        raise ValueError('Native cleanup rectangle must be a finite nonempty page region')
    if not preserve_zero_advance and selected_glyphs is None:
        page.clip_to_rect(rectangle)
        return {'method':'mupdf-hard-clip-no-zero-advance-v1','pymupdf_version':fitz.VersionBind,
                'kept_zero_advance_glyphs':0,'removed_outside_zero_advance_glyphs':None}
    # MuPDF filter boxes are in PDF coordinates, not PyMuPDF's top-left space.
    if selected_glyphs is not None:
        source=list(source_glyphs or [])
        ids=[g['id'] for g in source]
        if len(ids)!=len(set(ids)) or not {g['id']for g in selected_glyphs}<=set(ids):
            raise ValueError('Native source glyph identities are missing or duplicated')
        sanitizer=_SelectedNativeOptions(rectangle*~page.transformation_matrix,page.transformation_matrix,selected_glyphs,source)
    else:sanitizer=_NativeClipOptions(rectangle*~page.transformation_matrix)
    factory=_SanitizeFactory(sanitizer)
    options=fitz.mupdf.PdfFilterOptions()
    options.recurse=0
    options.instance_forms=1
    options.ascii=0
    # The wrapper's constructor can default no_update to a nonzero enum;
    # without this explicit assignment callbacks run but bytes stay unchanged.
    options.no_update=0
    options.add_factory(factory.internal())
    native_page=fitz._as_pdf_page(page)
    document=native_page.doc()
    # Match MuPDF's hard-clip transaction, including rollback on filter errors.
    fitz.mupdf.pdf_begin_operation(document,'Clip native protected content')
    try:
        fitz.mupdf.pdf_filter_page_contents(document,native_page,options)
        fitz.mupdf.pdf_end_operation(document)
    except Exception:
        fitz.mupdf.pdf_abandon_operation(document)
        raise
    result={'method':'native-zero-advance-safe-filter-v1','pymupdf_version':fitz.VersionBind,
            'kept_zero_advance_glyphs':sanitizer.kept_zero_advance,
            'removed_outside_zero_advance_glyphs':sanitizer.removed_zero_advance}
    if selected_glyphs is not None:
        unmatched=[gid for gid in sorted(sanitizer.selected) if sanitizer.seen[gid]!=1]
        result['native_text_selection']={'selected_glyphs':len(sanitizer.selected),
            'selected_operator_seen_once':len(sanitizer.selected)-len(unmatched),
            'unmatched_selected_ids':unmatched,'ambiguous_selected_matches':sanitizer.ambiguous,
            'removed_unselected_text_operators':sanitizer.removed,
            'verified':not unmatched and not sanitizer.ambiguous}
    return result
