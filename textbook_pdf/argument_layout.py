"""Argument layout projection from the one immutable source registry only."""
from dataclasses import asdict
from pathlib import Path
from .models import TOKEN_RE,file_sha256
from .argument_units import checked_units,PROFILE_KEY

def renderer_identity():
    from . import argument_units,argument_qa
    return {'argument_layout_sha256':file_sha256(Path(__file__)),
      'argument_units_sha256':file_sha256(Path(argument_units.__file__)),
      'argument_qa_sha256':file_sha256(Path(argument_qa.__file__))}

def projected_groups(book,groups,geometry):
    units=checked_units(book,scoped=True)
    if not units:return groups,{}
    flat=[e for group,*_ in groups for e in group];pos={e.id:i for i,e in enumerate(flat)}
    byparent={u['parent_id']:u for u in units.values()};members={eid for u in units.values()for eid in u['member_ids']}
    for u in units.values():
        chain=[u['parent_id']]+u['member_ids']
        if any(eid not in pos for eid in chain):raise ValueError('Argument source element hidden by another renderer/container')
        actual=[pos[eid]for eid in chain]
        if actual!=list(range(actual[0],actual[0]+len(chain))):raise ValueError('Visible argument chain interrupted by unrelated content')
    output=[];skip=set()
    for group,keep,columns,width,inset in groups:
        ordinary=[]
        def flush():
            if ordinary:output.append((list(ordinary),keep,columns,width,inset));ordinary.clear()
        for e in group:
            if e.id in skip:continue
            if e.id in byparent:
                flush();u=byparent[e.id];chain=[book.element_map()[eid]for eid in[e.id]+u['member_ids']]
                line=u['divider']['preserve_raw_length_pt']
                cols,w,offset=columns,width,inset
                if line>width+.0002:cols,w,offset=1,geometry['width']-geometry['left']-geometry['right'],0
                if line>w+.0002:raise ValueError('Original argument divider does not fit full page; no line/font/object scaling')
                output.append((chain,False,cols,w,offset));skip.update(u['member_ids'])
            elif e.id in members:
                raise ValueError('Argument parent/member projection lost source order')
            else:ordinary.append(e)
        flush()
    return output,byparent

def markup(unit,texts,book,paths,geometry,width,profile,issues,labels,preview_ids,columns):
    from . import layout as L
    checked_units(book,scoped=True)
    lookup=book.element_map();size=unit['layout']['fixed_text_size_pt']
    parent=lookup[unit['parent_id']];members=[lookup[eid]for eid in unit['member_ids']]
    # Parent stays a separate source element; public label/grid behavior is reused.
    p=L._element_markup(parent,texts[parent.id],book,paths,geometry,width,profile,issues,parent.id in preview_ids,labels,columns,texts,preview_ids)
    lines=[]
    for e,role in zip(members,unit['member_roles']):
        text=texts[e.id]
        if '∴'in TOKEN_RE.sub('',text):raise ValueError('Argument conclusion marker cannot be redrawn in prose')
        if role=='conclusion'and unit['marker']['conclusion_must_start_with_original_marker_token']:
            token='⟦F:'+unit['marker']['complete_original_asset_ids'][0]+'⟧'
            if not text.startswith(token):raise ValueError('Translated conclusion must begin with its complete original marker asset')
        body=L._text_markup(text,e,book,paths,width,issues)
        lines.append('#argument-line('+', '.join([L._q(unit['id']),L._q(e.id),L._q(role),str(e.page),L._q(labels[e.page]),L._number(width)+'pt',L._number(size)+'pt'])+')[as-body]['+body+']')
    # The extra named content argument is explicit; no delimiter inference from translated prose.
    lines=[line.replace(')[as-body][',')[')for line in lines]
    divider='#argument-divider('+L._q(unit['id'])+', '+L._number(unit['divider']['preserve_raw_length_pt'])+'pt, .504pt)'
    return '#argument-unit('+L._q(unit['id'])+', '+L._number(width)+'pt, '+L._number(geometry['height']-geometry['top']-geometry['bottom'])+'pt, parent: ['+p+'], premises: ('+','.join('['+body+']'for body in lines[:-1])+',), divider: ['+divider+'], conclusion: ['+lines[-1]+'])\n'

def page_spans(book,page_index):
    return {('source-argument',uid):(min(page_index[book.element_map()[eid].page]for eid in[u['parent_id']]+u['member_ids']),
       max(page_index[book.element_map()[eid].page]for eid in[u['parent_id']]+u['member_ids']))
       for uid,u in checked_units(book,scoped=True).items()}
