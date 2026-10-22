"""Keep tall native mathematical containers with their enclosed cells.

This is source geometry, not a bracket-string repair. Only the original
embedded delimiters with available ink bounds can establish a tall container.
Ordinary language, ambiguous ownership or failed literal maps block assembly.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import re
import statistics

import pymupdf as fitz

from .glyph_geometry import _TARGET_FONTS
from .models import TOKEN_RE


_PAIRS={'[':']','(':')'}
_OPERATORS=set('=+−×÷<>≤≥≠≈∧∨⊕∪∩∈∉→↔|.,:;!')


def _center(glyph):
    rect=fitz.Rect(glyph.get('nominal_bbox',glyph['bbox']))
    return (rect.tl+rect.br)*.5


def _ordinary_word_ids(glyphs):
    """Reject complete upright words, rather than hiding selected letters."""
    rows=defaultdict(list)
    for g in glyphs:
        if g['c'].strip():rows[round(g['origin'][1],1)].append(g)
    denied=set()
    for row in rows.values():
        row=sorted(row,key=lambda g:(g['origin'][0],g['id']))
        run=[]
        def flush():
            if len(run)>1 and not all(g.get('flags',0)&2 for g in run):
                denied.update(g['id'] for g in run)
        for g in row:
            if (not re.fullmatch('[A-Za-z]',g['c']) or run and
                    g['bbox'][0]-run[-1]['bbox'][2]>max(g['size'],run[-1]['size'])*.3):
                flush();run=[]
            if re.fullmatch('[A-Za-z]',g['c']):run.append(g)
        flush()
    return denied


def _container_pairs(glyphs):
    delimiters=[g for g in glyphs if g['c'] in '[]()'
                and _TARGET_FONTS.search(g['font']) and g.get('ink_geometry')
                and fitz.Rect(g['bbox']).height>g['size']*1.3]
    stacks=defaultdict(list)
    for g in glyphs:
        if g['c'] in '⎡⎢⎣⎤⎥⎦' and _TARGET_FONTS.search(g['font']) and g.get('ink_geometry'):
            key=('left' if g['c'] in '⎡⎢⎣' else 'right',round(g['origin'][0],1),g['font'],round(g['size'],2))
            stacks[key].append(g)
    for key,parts in stacks.items():
        parts=sorted(parts,key=lambda g:g['bbox'][1]);chain=[]
        first,middle,last=('⎡','⎢','⎣') if key[0]=='left' else ('⎤','⎥','⎦')
        for g in parts:
            if g['c']==first:chain=[g];continue
            if (not chain or g['c'] not in {middle,last}
                    or g['bbox'][1]-chain[-1]['bbox'][3]>.3):
                chain=[];continue
            chain.append(g)
            if g['c']==last:
                rect=fitz.Rect(chain[0]['bbox'])
                for item in chain[1:]:rect|=fitz.Rect(item['bbox'])
                delimiters.append({**chain[0],'c':'[' if key[0]=='left' else ']',
                    'bbox':list(rect),'component_glyph_ids':[item['id'] for item in chain]})
                chain=[]
    result=[];used=set()
    # Pair nearest delimiters with the same actual vertical ink extent. A
    # nested taller container belongs to another band, not a guessed string.
    for left in sorted(delimiters,key=lambda g:(g['bbox'][0],g['id']),reverse=True):
        if left['c'] not in _PAIRS:continue
        candidates=[g for g in delimiters if g['id'] not in used
            and g['c']==_PAIRS[left['c']] and g['bbox'][0]>left['bbox'][0]
            and abs(g['bbox'][1]-left['bbox'][1])<.6
            and abs(g['bbox'][3]-left['bbox'][3])<.6]
        if not candidates:continue
        right=min(candidates,key=lambda g:g['bbox'][0])
        used.add(right['id']);result.append((left,right))
    return result


def assemble_native_containers(book,doc,page,outdir,elements,glyphs,drawings):
    """Rebuild proven complete containers or leave an explicit blocking issue.

    It is callable both during analyze and on a private immutable-source repair
    generation. It never reads a not-yet-finalized asset bundle.
    """
    from .extraction import _asset,_issue,_rect_union,_rounded
    from .math_regions import _absorb_literals

    glyph_map={g['id']:g for g in glyphs};denied=_ordinary_word_ids(glyphs)
    active=[e for e in elements if not e.metadata.get('render_suppressed')]
    index={e.id:e for e in active}
    referenced={aid for e in active for aid in set(e.asset_ids)|set(TOKEN_RE.findall(e.text))}
    proofs=[];groups=[]
    for left,right in _container_pairs(glyphs):
        box=_rect_union([left['bbox'],right['bbox']])
        selected={g['id'] for g in glyphs if g['c'].strip() and box.contains(_center(g))}
        delimiter_ids=left.get('component_glyph_ids',[left['id']])+right.get('component_glyph_ids',[right['id']])
        selected.update(delimiter_ids)
        if len(selected)<3:continue
        size=max(left['size'],right['size'])
        # Nearby source relation signs establish a baseline for A = [cells].
        # Grow only along that mathematical row. Complete ordinary words veto
        # their glyphs and stop a body sentence being absorbed into an asset.
        middle=(box.y0+box.y1)/2
        nearby=[g for g in glyphs if g['c'] in '=<>≤≥≠≈→↔'
            and abs(g['origin'][1]-middle)<=size*.65
            and max(0,box.x0-g['bbox'][2],g['bbox'][0]-box.x1)<=size*2]
        baseline=statistics.median(g['origin'][1] for g in nearby) if nearby else middle
        extended=True
        while extended:
            extended=False
            for g in glyphs:
                if (not g['c'].strip() or g['id'] in selected or g['id'] in denied
                        or abs(g['origin'][1]-baseline)>size*.25):continue
                c=g['c'];r=fitz.Rect(g['bbox'])
                acceptable=(c.isdigit() or c in _OPERATORS or g.get('flags',0)&2
                            or re.fullmatch('[A-ZΑ-Ωα-ω]',c))
                owner=index.get(g.get('owner'))
                if owner and owner.metadata.get('list_label'):
                    label=owner.metadata['list_label']+'.'
                    prefix=owner.metadata.get('glyph_ids',[])[:len(label)]
                    if g['id'] in prefix:continue
                if (acceptable and max(0,box.x0-r.x1,r.x0-box.x1)<=size*.65
                        and min(r.y1,box.y1)-max(r.y0,box.y0)>0):
                    selected.add(g['id']);box|=r;extended=True
        groups.append({'glyphs':selected,'bbox':box,'delimiters':delimiter_ids})
    # Nested/adjacent regions sharing source glyphs are a single source object.
    merged=[]
    for group in groups:
        hits=[g for g in merged if g['glyphs']&group['glyphs']]
        for hit in hits:
            group['glyphs']|=hit['glyphs'];group['bbox']|=hit['bbox']
            group['delimiters']+=hit['delimiters'];merged.remove(hit)
        merged.append(group)
    for group in merged:
        active=[e for e in elements if not e.metadata.get('render_suppressed')]
        index={e.id:e for e in active}
        referenced={aid for e in active for aid in set(e.asset_ids)|set(TOKEN_RE.findall(e.text))}
        selected=group['glyphs'];box=group['bbox']
        # A token is atomic. Absorb all its selected glyphs or block; never
        # silently erase an outside portion of a larger existing formula.
        while True:
            before=set(selected)
            old_ids={aid for aid in referenced if aid in book.assets
                     and selected&set(book.assets[aid].metadata.get('glyph_ids',[]))}
            if not old_ids:break
            for aid in old_ids:selected.update(book.assets[aid].metadata.get('glyph_ids',[]))
            selected&=glyph_map.keys()
            box|=_rect_union(glyph_map[k]['bbox'] for k in selected)
            # Any enclosed glyph travels with the container, including plain
            # numeric rows previously emitted as translatable paragraphs.
            selected.update(g['id'] for g in glyphs if g['c'].strip() and box.contains(_center(g)))
            if selected==before:break
        if not old_ids:continue
        owners=[e for e in active if any(aid in old_ids for aid in TOKEN_RE.findall(e.text))]
        if not owners:continue
        keeper=owners[0]
        if selected&denied:
            _issue(book,'source_math_container_unverified','Tall mathematical container encloses ordinary language; separate label handling is required',page.number+1,keeper.id,severity='error')
            continue
        if any(glyph_map[k].get('owner') not in index for k in selected):
            _issue(book,'source_math_container_unverified','Tall mathematical container has an unresolved source owner',page.number+1,keeper.id,severity='error')
            continue
        existing=[book.assets[aid] for aid in old_ids]
        if len(existing)==1 and selected<=set(existing[0].metadata.get('glyph_ids',[])):
            a=existing[0]
            if (fitz.Rect(a.bbox).contains(box) and abs(a.width-fitz.Rect(a.bbox).width)<.001
                    and abs(a.height-fitz.Rect(a.bbox).height)<.001):continue
        literal_text={e.id:_absorb_literals(e,selected,book,glyph_map) for e in active}
        if any(v is None for v in literal_text.values()):
            _issue(book,'source_math_container_unverified','Cannot map every literal cell back to its exact source character before container assembly',page.number+1,keeper.id,severity='error')
            continue
        gs=[glyph_map[k] for k in selected]
        relations=[g['origin'][1] for g in gs if g['c'] in '=<>≤≥≠≈→↔']
        baseline=statistics.median(relations) if relations else (box.y0+box.y1)/2
        positioned=sorted(gs,key=lambda g:(round(g['origin'][1],1),g['bbox'][0],g['id']))
        text=''.join(g['c'] for g in positioned)
        asset=_asset(book,doc,page,outdir,'formula',box+(-.15,-.15,.15,.15),text,baseline,
            {'detector':'complete-native-tall-container-v1','glyph_ids':sorted(selected),
             'source_component_ids':sorted(old_ids),'native_glyph_geometry_preserved':True,
             'source_text_is_positioned_runs':True,
             'container_proof':{'delimiter_glyph_ids':sorted(set(group['delimiters'])),
                 'selected_native_glyphs':len(selected),'ordinary_language_glyphs':0,
                 'source_page':page.number+1,'source_bbox':_rounded(box)}})
        if asset.metadata.get('isolation_issues') or asset.metadata.get('selected_glyphs_lost'):
            _issue(book,'source_math_container_unverified','Complete native container could not be isolated without losing a selected glyph',page.number+1,keeper.id,severity='error')
            continue
        inserted=False
        for e in active:
            original_ids=set(e.metadata.get('glyph_ids',[]));e.text=literal_text[e.id]
            def replace(match):
                nonlocal inserted
                if match[1] not in old_ids:return match[0]
                if not inserted:
                    inserted=True;return f'⟦F:{asset.id}⟧'
                return ''
            e.text=TOKEN_RE.sub(replace,e.text)
            e.asset_ids=[aid for aid in e.asset_ids if aid not in old_ids]
            e.metadata['glyph_ids']=[k for k in e.metadata.get('glyph_ids',[]) if k not in selected]
            if e is keeper:e.asset_ids.append(asset.id)
            if e is not keeper and original_ids and original_ids<=selected and not TOKEN_RE.search(e.text):e.text=''
            if not e.text.strip() and not e.asset_ids and e is not keeper:
                e.metadata.update(render_suppressed=True,superseded_by=keeper.id,
                    exclusion_reason='Original native cell/label now belongs to its complete mathematical container')
        keeper.metadata.setdefault('glyph_ids',[]).extend(sorted(selected))
        if not TOKEN_RE.sub('',keeper.text).strip():
            keeper.kind='formula';keeper.bbox=list(asset.bbox);keeper.metadata['source_text']=text
        for g in gs:g.update(owner=keeper.id,asset_id=asset.id,disposition='preserved-original')
        for aid in old_ids:
            book.assets[aid].metadata.setdefault('combined_into',[]).append(asset.id)
            book.assets[aid].metadata['exclusion_reason']='Native delimiter, all enclosed cells and adjacent mathematical labels protected together'
        proofs.append({'asset_id':asset.id,'component_ids':sorted(old_ids),
            'owner_ids':[e.id for e in owners],'glyph_ids':sorted(selected),'bbox':asset.bbox})
    # Absorbed cell/label rows can leave only source spaces behind. Their
    # original IDs still need an explicit disposition, not a suppressed owner.
    suppressed={e.id:e for e in elements if e.metadata.get('render_suppressed')}
    for g in glyphs:
        owner=suppressed.get(g.get('owner'))
        if owner is None or not g['c'].isspace():continue
        owner.metadata.setdefault('excluded_whitespace_glyph_ids',[]).append(g['id'])
        owner.metadata['glyph_ids']=[gid for gid in owner.metadata.get('glyph_ids',[]) if gid!=g['id']]
        g.update(owner='whitespace',disposition='excluded-whitespace',
                 exclusion_reason='Source space from a cell/label absorbed into its complete native math object')
    return proofs
