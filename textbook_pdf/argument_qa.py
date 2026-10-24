"""Argument-specific actual PDF validation, separate from native asset identity."""
from collections import defaultdict
from .argument_units import checked_units

def check_argument_layout(book,traces,geometry,pdf_path):
    units=checked_units(book,scoped=True)
    if not units:return {'opt_in':False,'passed':True,'issues':[]}
    issues=[];checks={};points=defaultdict(list)
    def error(code,uid,detail,**extra):issues.append({'code':code,'severity':'error','argument_unit_id':uid,'detail':detail,**extra})
    for t in traces:
        if isinstance(t,dict):points[(t.get('type'),t.get('id'),t.get('phase'))].append(t)
    import pymupdf as fitz
    with fitz.open(pdf_path)as pdf:
      for uid,u in units.items():
        chain=[u['parent_id']]+u['member_ids'];bounds={};size=u['layout']['fixed_text_size_pt']
        for eid in chain:
            starts=points['element',eid,'start'];ends=points['element',eid,'end']
            if len(starts)!=1 or len(ends)!=1:
                error('argument_member_trace_missing_or_duplicate',uid,'Each complete source member needs exactly one start/end',element_id=eid);continue
            start,end=starts[0],ends[0];bounds[eid]=(start,end)
            if eid!=u['parent_id']and start['page']!=end['page']:
                error('argument_premise_or_conclusion_split',uid,'A complete premise/conclusion cannot split internally',element_id=eid)
            src=book.element_map()[eid]
            if start.get('source_page')!=src.page or start.get('source_label')!=('原书 p.'+str(src.printed_page)if src.printed_page else'原 PDF 第'+str(src.page)+'页'):
                error('argument_source_label_changed',uid,'Argument source page label is not bound to its actual source',element_id=eid)
        if set(bounds)!=set(chain):continue
        ordered=[(bounds[eid][0]['page'],float(bounds[eid][0]['y']),float(bounds[eid][0]['x']))for eid in chain]
        if ordered!=sorted(ordered):error('argument_member_order_changed',uid,'Original parent/premises/conclusion order changed')
        if bounds[chain[0]][0]['page']!=bounds[chain[1]][0]['page']:
            error('argument_parent_detached',uid,'Parent and first premise must remain together')
        lastp,conclusion=u['member_ids'][-2:];p_end=bounds[lastp][1];c_start=bounds[conclusion][0]
        if bounds[lastp][0]['page']!=c_start['page']:
            error('argument_atomic_tail_split',uid,'Last complete premise, divider and complete conclusion must stay together')
        dividers=points['argument-divider',uid,'object'];mode=points['argument-unit',uid,'mode']
        if len(dividers)!=1:error('argument_divider_trace_missing_or_duplicate',uid,'Divider trace must occur once')
        if len(mode)!=1:error('argument_mode_trace_missing',uid,'Argument short/long mode trace missing')
        elif mode[0].get('mode')=='short'and len({bounds[e][0]['page']for e in chain})!=1:
            error('argument_short_block_split',uid,'Short argument must move as a complete unit')
        if len(dividers)!=1:continue
        d=dividers[0];page=int(d['page']);width=u['divider']['preserve_raw_length_pt']
        if page!=c_start['page']or float(d['y'])<float(p_end['y'])-.5 or float(d['y'])>float(c_start['y'])+.5:
            error('argument_divider_wrong_scope',uid,'Divider must occur strictly between final premise and conclusion')
        if page<1 or page>len(pdf):error('argument_page_missing',uid,'Argument trace page absent');continue
        target=pdf[page-1];matching=[]
        # A correct tail rule does not excuse an extra rule earlier in the
        # argument. Scan this unit's complete visible page ranges, not only
        # the final-premise/conclusion gap.
        unit_rules=[]
        first,last=bounds[chain[0]][0],bounds[chain[-1]][1]
        for pn in range(int(first['page']),int(last['page'])+1):
            top=float(first['y'])-.75 if pn==int(first['page'])else 0
            bottom=float(last['y'])+.75 if pn==int(last['page'])else pdf[pn-1].rect.height
            for candidate in pdf[pn-1].get_drawings():
                if candidate.get('type')!='s'or len(candidate['items'])!=1 or candidate['items'][0][0]!='l':continue
                a,b=candidate['items'][0][1:];color=candidate.get('color')
                if (abs(a.y-b.y)>.001 or not(top<=a.y<=bottom)or abs(a.x-float(d['x']))>1
                    or abs(b.x-a.x)<5 or abs(float(candidate.get('width',0))-.504)>.001):continue
                if color and len(color)==3 and max(abs(color[i]-v)for i,v in enumerate((0,.6809338331222534,.9379873275756836)))<=.008:
                    unit_rules.append((pn,candidate))
        if len(unit_rules)!=1:
            error('argument_unexpected_rule_in_unit_scope',uid,'Exactly one source-colored divider may occur in the complete argument scope',actual_count=len(unit_rules))
        # Actual paths, not metadata alone, establish the one mandatory rule.
        lo=min(float(p_end['y']),float(c_start['y']))-.75;hi=max(float(p_end['y']),float(c_start['y']))+.75
        for path in target.get_drawings():
            if path.get('type')!='s'or len(path['items'])!=1 or path['items'][0][0]!='l':continue
            a,b=path['items'][0][1:]
            if abs(a.y-b.y)>.001 or not(lo<=a.y<=hi):continue
            if abs(a.x-float(d['x']))>1 or abs(b.x-float(d['x'])-width)>1:continue
            matching.append(path)
        if len(matching)!=1:error('argument_actual_divider_missing_or_duplicate',uid,'Actual PDF must contain exactly one correctly scoped divider',actual_count=len(matching))
        else:
            path=matching[0];a,b=path['items'][0][1:]
            if abs(abs(b.x-a.x)-width)>.001 or abs(float(path.get('width',0))-.504)>.001:
                error('argument_divider_scaled',uid,'Original divider length and width cannot change')
            color=path.get('color')
            if not color or len(color)!=3 or max(abs(color[i]-v)for i,v in enumerate((0,.6809338331222534,.9379873275756836)))>.008:
                error('argument_divider_color_changed',uid,'Divider rendered color differs from original cyan')
            if path.get('stroke_opacity')!=1:error('argument_divider_opacity_changed',uid,'Original opaque divider changed')
        for eid in u['member_ids']:
            st,en=bounds[eid];rows=points['argument-member',eid,'start']
            if len(rows)!=1 or rows[0].get('text_size_pt')!=size:
                error('argument_role_size_trace_changed',uid,'Fixed argument role size missing/changed',element_id=eid)
            if st['page']!=en['page']:continue
            box=fitz.Rect(float(st['x']),float(st['y'])-.5,float(st['x'])+float(st['width']),float(en['y'])+.5)
            for block in pdf[int(st['page'])-1].get_text('dict',clip=box)['blocks']:
                for line in block.get('lines',[]):
                    for span in line['spans']:
                        if any('\u3400'<=c<='\u9fff'for c in span['text'])and abs(span['size']-size)>.001:
                            error('argument_actual_role_font_size_changed',uid,'Actual Chinese text size differs from its fixed source role',element_id=eid,actual_size=span['size'])
        checks[uid]={'source_page':u['source_page'],'member_ids':u['member_ids'],'actual_rule_count':len(matching),'joint_semantics_not_inferred':True}
    return {'opt_in':True,'passed':not issues,'issues':issues,'units':checks,'native_object_identity_checked_separately':True}
