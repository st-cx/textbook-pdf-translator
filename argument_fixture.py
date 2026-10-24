"""Self-authored source-backed argument fixtures, never Rosen translations."""
from pathlib import Path
from types import SimpleNamespace
from dataclasses import asdict
from copy import deepcopy
import json
import pymupdf as fitz
from textbook_pdf.models import Book,Element,file_sha256,digest
from textbook_pdf.extraction import _collect,_asset,_finalize_assets
from textbook_pdf.argument_units import PROFILE_KEY,MEMBER_KEY,JOINT_PROTOCOL

def make_fixture(root,premises=2,role='exercise',leading=0):
    root=Path(root);root.mkdir(parents=True,exist_ok=False);(root/'assets').mkdir()
    source=root/'self-authored-source.pdf';doc=fitz.open();page=doc.new_page(width=320,height=max(500,70+(premises+3)*24))
    page.insert_font(fontname='ArgLatin',fontbuffer=fitz.Font('helv').buffer)
    page.insert_font(fontname='ArgSymbol',fontbuffer=fitz.Font('symbol').buffer)
    lines=[('parent','1. Determine whether the argument is valid.')]
    for i in range(premises):lines.append((f'p{i+1}','If the switch is on, the light is on.'if i==0 else'The switch is on.'if i==premises-1 else'Premise alpha holds.'))
    lines.append(('c','The light is on.'));ys={}
    for i,(eid,text)in enumerate(lines):
        y=45+i*24;ys[eid]=y
        if eid=='c':page.insert_text((18,y),'∴',fontname='ArgSymbol',fontsize=10)
        page.insert_text((30 if eid=='c'else 18,y),text,fontname='ArgLatin',fontsize=10)
    bar_y=ys['c']-13;page.draw_line((18,bar_y),(187.371,bar_y),color=(1,0,0,0),width=.504)
    doc.save(source,garbage=3,deflate=True);doc.close();sha=file_sha256(source)
    with fitz.open(source)as original:
        page=original[0];_,glyphs=_collect(page);marker=[g for g in glyphs if g['c']=='∴'];assert len(marker)==1
        mini=SimpleNamespace(source_sha256=sha,assets={},elements=[],issues=[],profile={'job_dir':str(root)},_active_glyphs=glyphs,_asset_prefix='self-arg-')
        a=_asset(mini,original,page,root,'formula',fitz.Rect(marker[0]['bbox']), '∴',marker[0]['origin'][1],{'glyph_ids':[marker[0]['id']],'detector':'self-authored-test-original-native'})
        _finalize_assets(mini,1,root);assert not mini.issues
        elements=[];ordered_glyphs={};translations={};kind='exercise'if role=='exercise'else'paragraph'
        for eid,text in lines:
            gs=[g for g in glyphs if abs(g['origin'][1]-ys[eid])<.01];assert gs
            b=fitz.Rect(gs[0]['bbox'])
            for g in gs[1:]:b|=fitz.Rect(g['bbox'])
            source_text=('⟦F:'+a.id+'⟧'if eid=='c'else'')+text
            elements.append(Element(eid,kind,source_text,1,list(b),'1',{'columns':1,'column':0,'source_size':10},[a.id]if eid=='c'else[],'parent','Fixture',{'glyph_ids':[g['id']for g in gs]}))
            ordered_glyphs[eid]=[g['id']for g in gs]
            translations[eid]='1. 判断以下论证是否有效。'if eid=='parent'else'如果开关打开，灯就亮。'if eid=='p1'else'开关打开。'if eid==f'p{premises}'else('⟦F:'+a.id+'⟧灯亮。'if eid=='c'else'前提甲成立。')
        before={'id':'drawing-0','bbox':[18,bar_y,187.371,bar_y],'asset_ids':[],'disposition':'design-reference','reason':'self-authored original source divider',
          'color':list(page.get_drawings()[0]['color']),'fill':None,'width':page.get_drawings()[0]['width']}
        state={'page':1,'graphic_id':'drawing-0','complete_before_graphics_ledger':deepcopy(before),'fresh_native_drawing':repr(page.get_drawings()[0]),
          'raw_length_pt':169.371,'native_effective_length_pt':169.371,'raw_width_pt':.504,'original_color_space':'DeviceCMYK','original_components':[1,0,0,0],
          'original_source_content_sha256':digest(page.read_contents()),'raw_stroke_operator':'1 0 0 0 K','source_page_transform':list(page.transformation_matrix)}
    uid='self-argument';members=[eid for eid,_ in lines[1:]];roles=['premise']*premises+['conclusion']
    for ordinal,e in enumerate(elements[1:]):
        e.metadata[MEMBER_KEY]={'schema':'source-anchored-argument-member-v1','unit_id':uid,'role':roles[ordinal],'member_ordinal':ordinal,'parent_id':'parent',
          'source_page':1,'layout_role':role,'source_glyph_ids':ordered_glyphs[e.id],'where_clause_belongs_to_this_premise':False,'joint_review_protocol':JOINT_PROTOCOL}
    u={'schema':'source-anchored-argument-unit-v1','id':uid,'source_pdf_sha256':sha,'origin_book_sha256':digest('self-authored source'),
      'source_facts_sha256':digest({'source':sha,'lines':lines}),'parent_id':'parent','parent_full_source_sha256':digest(asdict(elements[0])),
      'source_page':1,'printed_page':'1','original_source_owner_ids':members,'original_source_full_sha256':{e.id:digest(asdict(e))for e in elements[1:]},
      'member_ids':members,'member_roles':roles,'member_source_full_sha256':{e.id:digest(asdict(e))for e in elements[1:]},
      'member_source_glyph_ids':{eid:ordered_glyphs[eid]for eid in members},'complete_immutable_asset_sha256':{a.id:digest(asdict(a))},
      'marker':{'strategy':'separate_original_native_asset','complete_original_asset_ids':[a.id],'marker_is_inside_original_complete_object':False,'extra_marker_drawing_forbidden':True,'conclusion_must_start_with_original_marker_token':True},
      'divider':{'page':1,'graphic_id':'drawing-0','semantic_role':'argument_divider','primitive_schema':'source-anchored-argument-divider-v1','source_draw_state_sha256':digest(state),
        'complete_original_draw_state':state,'between_member_ids':members[-2:],'preserve_raw_length_pt':169.371,'preserve_raw_width_pt':.504,'color_space':'DeviceCMYK','stroke_components':[1,0,0,0]},
      'layout':{'role':role,'fixed_text_size_pt':9.5 if role=='exercise'else 10.5,'protected_native_objects':'original_dimensions_and_baseline_no_scaling','short_block':'parent_and_all_members_together','long_block':'between_complete_premise_members_only',
        'atomic_tail_member_ids':[members[-2],'divider',members[-1]],'overlong_premise_or_tail':'block_no_font_object_or_line_compression','page_label_source':'each_member_actual_original_page_and_printed_page'},
      'joint_review':{'requires_joint_review':True,'individual_passes_imply_unit_pass':False,'protocol':JOINT_PROTOCOL,'mutable_verdict_storage':'separate_transactional_Store_table_not_source_profile','legacy_semantic_unit_v1_is_applicable':False},
      'authored_argument_validity':'not_inferred_or_corrected'}
    graphics={**before,'disposition':'reconstructed-source-primitive','reason':'Exact argument primitive reconstruction','semantic_role':'argument_divider','argument_unit_id':uid,'primitive_schema':'source-anchored-argument-divider-v1',
      'source_original_graphic_before_argument_reclassification':before,'source_draw_state_sha256':digest(state),'requires_validated_reconstruction':True}
    profile={'job_dir':str(root.resolve()),'body_size':10.5,'exercise_size':9.5,'caption_size':8.5,'body_font':'SimSun','heading_font':'SimHei',
      'page_width':320,'page_height':430,'margins':{'left':34,'right':34,'top':34,'bottom':40},PROFILE_KEY:{uid:u}}
    book=Book(str(source.resolve()),sha,'Self-authored argument fixture',1,[1],pages=[{'number':1,'width':320,'height':max(500,70+(premises+3)*24),'printed_page':'1','columns':1,'element_ids':[e.id for e in elements],'graphics_coverage':[graphics]}],elements=elements,assets={a.id:a},profile=profile)
    if leading:
        lead=Element('lead','paragraph','Introductory material.',1,[18,0,300,15],'1',{'columns':1},[],'','Fixture',{})
        book.elements.insert(0,lead);book.pages[0]['element_ids'].insert(0,lead.id);translations[lead.id]='引导文字。'*leading
    return book,translations
