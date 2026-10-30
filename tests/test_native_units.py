"""Source-unit regressions use original PDF paths and image paints, not mocks of layout."""
import copy
import uuid
import shutil
import unittest
from pathlib import Path
import pymupdf as fitz

from textbook_pdf.models import Book, Element, Asset
from textbook_pdf.native_units import plan_native_units, repair_native_units, resolve_proved_source_warnings


class NativeUnitTests(unittest.TestCase):
    def chart(self):
        doc=fitz.open();p=doc.new_page(width=400,height=400)
        for y in (65,100,180):p.draw_line((40,y),(350,y),width=.5)
        p.draw_circle((100,82),1.4,color=None,fill=(0,0,0))
        for x in (90,140,190):
            p.draw_line((x,130),(x+10,150));p.draw_circle((x,130),1.4,fill=(0,0,0))
        p.insert_text((40,55),'Basis step',fontsize=8)
        p.insert_text((40,197),'FIGURE 3 Example.',fontsize=10)
        glyphs=[]
        for b,block in enumerate(p.get_text('rawdict')['blocks']):
            for l,line in enumerate(block.get('lines',[])):
                for s,span in enumerate(line['spans']):
                    for c,g in enumerate(span['chars']):
                        glyphs.append(dict(id=f'b{b}.l{l}.s{s}.c{c}',c=g['c'],bbox=list(g['bbox']),origin=list(g['origin']),font=span['font'],size=span['size'],block=b,line=l,owner='label' if b==0 else 'caption',asset_id=None))
        elements=[Element('label','sidebar','Basis step',1,[40,45,85,57],metadata={'glyph_ids':[g['id'] for g in glyphs if g['owner']=='label']}),Element('caption','caption','FIGURE 3 Example.',1,[40,186,135,200],metadata={'glyph_ids':[g['id'] for g in glyphs if g['owner']=='caption']})]
        book=Book('', 'source-hash','test',1,[1],elements=elements)
        return doc,p,glyphs,elements,book

    def test_detached_point_and_all_graphs_are_one_content_unit(self):
        doc,p,gs,es,b=self.chart();r=plan_native_units(p,gs,es,b.assets)
        units=[u for u in r['units'] if u['kind']=='ruled-caption-diagram']
        self.assertEqual(len(units),1)
        self.assertEqual(len(units[0]['drawing_indices']),len(p.get_drawings()))
        self.assertIn('label',units[0]['consumed_element_ids'])
        self.assertEqual(units[0]['caption_id'],'caption')
        doc.close()

    def test_missing_caption_does_not_join_nearby_graphs(self):
        doc,p,gs,es,b=self.chart();es=[e for e in es if e.kind!='caption']
        self.assertEqual(plan_native_units(p,gs,es,b.assets)['units'],[]);doc.close()

    def test_less_than_three_rules_is_not_whole_figure_evidence(self):
        doc,p,gs,es,b=self.chart();p2=doc.new_page(width=400,height=400)
        p2.draw_line((40,100),(350,100));p2.draw_line((40,180),(350,180))
        self.assertFalse(plan_native_units(p2,gs,es,b.assets)['units']);doc.close()

    def test_long_prose_inside_candidate_stays_unresolved(self):
        doc,p,gs,es,b=self.chart()
        for i,c in enumerate('This unrelated body paragraph must never be swallowed into the mathematical figure container. '*2):
            gs.append(dict(id=f'prose-{i}',c=c,bbox=[45+i*.5,110,46+i*.5,118],origin=[45+i*.5,117],font='Times-Roman',size=8,block=9,line=0,owner='prose'))
        es.append(Element('prose','paragraph','unrelated prose',1,[45,110,200,118]))
        self.assertFalse(plan_native_units(p,gs,es,b.assets)['units']);doc.close()

    def test_source_path_crossing_container_is_not_silently_cut(self):
        doc,p,gs,es,b=self.chart();p.draw_line((100,130),(380,250))
        r=plan_native_units(p,gs,es,b.assets)
        self.assertFalse(r['units']);self.assertTrue(r['unresolved']);doc.close()

    def test_repair_transfers_glyphs_without_replacing_original_page(self):
        doc,p,gs,es,b=self.chart();before=p.get_pixmap().samples
        def factory(book,source,page,outdir,kind,bbox,text,baseline=None,metadata=None):
            a=Asset('new-math',kind,'test.pdf',1,list(bbox),bbox.width,bbox.height,text=text,metadata=metadata or {})
            book.assets[a.id]=a;return a
        r=repair_native_units(b,doc,p,Path('.'),es,gs,asset_factory=factory)
        self.assertEqual(len(r['applied']),1)
        self.assertEqual(p.get_pixmap().samples,before)
        self.assertTrue(next(e for e in es if e.id=='label').metadata['render_suppressed'])
        self.assertNotEqual(next(g for g in gs if g['owner']!='caption')['owner'],'label')
        parent=next(e for e in es if e.id==r['applied'][0]['element_id'])
        self.assertEqual(parent.kind,'figure');self.assertEqual(len(parent.metadata['figure_label_ids']),1)
        self.assertEqual(next(e for e in es if e.id=='caption').group_id,parent.id)
        doc.close()

    def photo(self):
        doc=fitz.open();p=doc.new_page(width=400,height=400)
        pix=fitz.Pixmap(fitz.csRGB,fitz.IRect(0,0,20,20),False);pix.clear_with(80)
        p.insert_image(fitz.Rect(100,150,170,230),stream=pix.tobytes('png'))
        p.draw_rect((99,149,171,231),width=.5)
        p.draw_line((150,133),(155,140),color=(0,.7,.9));p.insert_text((125,140),'Links',fontsize=8)
        a=Asset('wide','figure','a.pdf',1,[98,130,172,232],74,102,metadata={'detector':'translated-figure-background'})
        b=Asset('photo','figure','a.pdf',1,[99,149,171,231],72,82,metadata={'detector':'merged-layered-source-image'})
        es=[Element('wide-e','figure','',1,a.bbox,asset_ids=['wide'],metadata={'figure_label_ids':['link']}),Element('photo-e','figure','',1,b.bbox,asset_ids=['photo']),Element('link','figure_label','Links',1,[125,131,145,142],metadata={'parent_figure_id':'wide-e'})]
        gs=[dict(id=f'label-{i}',c=c,bbox=[125+i*3,131,128+i*3,142],origin=[125+i*3,140],font='Helvetica',size=8,owner='link',block=1,line=0) for i,c in enumerate('Links')]
        return doc,p,gs,es,{'wide':a,'photo':b}

    def test_overlapping_source_image_paint_is_split_from_external_label(self):
        doc,p,gs,es,assets=self.photo();r=plan_native_units(p,gs,es,assets)
        u=next(u for u in r['units'] if u['kind']=='external-image-label')
        self.assertEqual(u['element_id'],'wide-e');self.assertEqual(u['preserved_image_owner_id'],'photo-e')
        self.assertLess(u['bbox'][3],149)
        self.assertTrue(u['image_paints_preserved_once']);doc.close()

    def test_bbox_overlap_without_native_image_never_proves_duplicate(self):
        doc,p,gs,es,assets=self.photo();blank=doc.new_page(width=400,height=400)
        self.assertFalse(plan_native_units(blank,gs,es,assets)['units']);doc.close()

    def test_label_touching_photo_is_not_masked_or_dropped(self):
        doc,p,gs,es,assets=self.photo()
        for g in gs:g['bbox']=[g['bbox'][0],155,g['bbox'][2],165]
        self.assertFalse([u for u in plan_native_units(p,gs,es,assets)['units'] if u['kind']=='external-image-label']);doc.close()

    def test_planning_is_read_only(self):
        doc,p,gs,es,assets=self.photo();before=copy.deepcopy((gs,es,assets));plan_native_units(p,gs,es,assets)
        self.assertEqual((gs,es,assets),before);doc.close()

    def test_math_variables_are_preserved_not_detached_as_language(self):
        doc,p,gs,es,b=self.chart()
        for g in gs:
            if g['owner']=='label':g.update(c='x',font='STIXGeneral-Italic',flags=2)
        u=plan_native_units(p,gs,es,b.assets)['units'][0]
        self.assertEqual(u['labels'],[])
        self.assertEqual(set(u['protected_glyph_ids']),{g['id'] for g in gs if g['owner']=='label'});doc.close()

    def test_mixed_math_label_is_not_translated(self):
        doc,p,gs,es,b=self.chart()
        row=[g for g in gs if g['owner']=='label']
        text='Step x = 1'
        for i,g in enumerate(row):g['c']=text[i] if i<len(text) else ' '
        self.assertEqual(plan_native_units(p,gs,es,b.assets)['units'][0]['labels'],[]);doc.close()

    def test_unassigned_old_vector_member_blocks_image_recrop(self):
        doc,p,gs,es,assets=self.photo();p.draw_line((98,185),(174,185))
        r=plan_native_units(p,gs,es,assets)
        self.assertFalse(r['units']);self.assertTrue(r['unresolved']);doc.close()

    def test_other_active_glyph_reference_blocks_container_transfer(self):
        doc,p,gs,es,b=self.chart()
        for g in gs:
            if g['owner']=='label':g.update(c='x',font='STIXGeneral-Italic',flags=2)
        aid='shared';b.assets[aid]=Asset(aid,'formula','a.pdf',1,[0,0,10,10],10,10,metadata={'glyph_ids':[gs[0]['id']]})
        es.append(Element('outside','paragraph','⟦F:shared⟧',1,[0,250,20,260],asset_ids=[aid]))
        r=plan_native_units(p,gs,es,b.assets)
        self.assertFalse(r['units']);self.assertTrue(r['unresolved']);doc.close()

    def test_repair_is_idempotent_and_source_hash_bound(self):
        doc,p,gs,es,b=self.chart()
        def factory(book,source,page,outdir,kind,bbox,text,baseline=None,metadata=None):
            a=Asset('new-math',kind,'test.pdf',1,list(bbox),bbox.width,bbox.height,text=text,metadata=metadata or {})
            book.assets[a.id]=a;return a
        first=repair_native_units(b,doc,p,Path('.'),es,gs,asset_factory=factory)
        self.assertEqual(first['applied'][0]['proof']['source_sha256'],b.source_sha256)
        self.assertFalse(repair_native_units(b,doc,p,Path('.'),es,gs,asset_factory=factory)['applied']);doc.close()

    def test_actual_analyze_preserves_detached_point_and_source_caption(self):
        from textbook_pdf.extraction import analyze
        import gzip,json
        root=Path(__file__).resolve().parents[1]/'_testdata'/('native-unit-'+uuid.uuid4().hex)
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,root)
        doc,p,gs,es,b=self.chart();source=root/'original.pdf';doc.save(source);doc.close()
        raw=source.read_bytes();book=analyze(source,root/'job',backend='native')
        parents=[e for e in book.elements if e.metadata.get('native_unit_proof') and e.kind=='figure' and not e.metadata.get('render_suppressed')]
        self.assertEqual(len(parents),1)
        parent=parents[0];a=book.assets[parent.asset_ids[0]]
        self.assertTrue(a.metadata['native_unit_proof']['created_native_unit_verification']['verified'])
        self.assertEqual(len(a.metadata['source_drawing_ids']),10)
        captions=[e for e in book.elements if e.kind=='caption' and not e.metadata.get('render_suppressed')]
        self.assertEqual(captions[0].group_id,parent.id)
        self.assertNotIn('parent_figure_id',captions[0].metadata)
        self.assertEqual(source.read_bytes(),raw)
        with gzip.open(root/'job'/book.pages[0]['coverage']['ledger'],'rt',encoding='utf8') as f:ledger=json.load(f)
        active={e.id for e in book.elements if not e.metadata.get('render_suppressed')}
        self.assertFalse([g for g in ledger if g.get('owner') not in active and g['c'].strip()])

    def test_failed_crop_does_not_retire_source_members(self):
        doc,p,gs,es,b=self.chart();before=copy.deepcopy((es,gs))
        def fail(*a,**kw):raise ValueError('actual original path lost')
        result=repair_native_units(b,doc,p,Path('.'),es,gs,asset_factory=fail)
        self.assertFalse(result['applied']);self.assertEqual(result['unresolved'][0]['severity'],'error')
        self.assertEqual((es,gs),before);doc.close()

    def test_warning_resolution_retains_original_record_and_every_error(self):
        b=Book('','source','test',1,[1]);original={'code':'visual_caption_unresolved','severity':'warning','page':1,'element_id':'old','detail':'No caption'}
        error={**original,'severity':'error'};unrelated={**original,'element_id':'other'}
        b.issues=[original,error,unrelated]
        proof={'kind':'ruled-caption-diagram','element_id':'new','asset_id':'new-a','consumed_element_ids':['old'],'proof':{'native':'verified'}}
        resolved=resolve_proved_source_warnings(b,[proof])
        self.assertEqual(b.issues,[error,unrelated]);self.assertEqual(resolved[0]['original_issue'],original)
        self.assertFalse(resolved[0]['certifies_translated_page'])


if __name__=='__main__':unittest.main()
