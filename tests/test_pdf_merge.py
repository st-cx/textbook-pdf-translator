"""Actual project-owned native fonts plus conservative dictionary counterexamples."""
from pathlib import Path
from collections import Counter
import hashlib,json,re,unittest
import pymupdf as fitz
from textbook_pdf.pdf_merge import deduplicate_font_streams

FIXTURES=Path(__file__).resolve().parent/'fixtures'
FONT=FIXTURES/'original-ordinary-ink.ttf';DISTINCT=FIXTURES/'original-ordinary-ink-distinct.ttf'

def fingerprint(doc):
    result=[]
    for p in doc:
        fonts=[]
        for f in p.get_fonts(full=True):
            name,ext,typ,data=doc.extract_font(f[0]);fonts.append((name,ext,typ,hashlib.sha256(data).hexdigest()))
        pix=p.get_pixmap(matrix=fitz.Matrix(4,4),alpha=False)
        result.append({'rect':tuple(p.rect),'rotation':p.rotation,'text':p.get_text('rawdict'),
            'trace':p.get_texttrace(),'drawings':p.get_drawings(),'fonts':sorted(fonts),
            'pixel_sha256':hashlib.sha256(pix.samples).hexdigest()})
    # garbage=2 renumbers indirect objects. Keep every TOC destination field;
    # its internal xref number identifies storage, not the bookmark destination.
    toc=doc.get_toc(simple=False)
    for row in toc:
        if len(row)>3:row[3]={k:v for k,v in row[3].items()if k!='xref'}
    return result,doc.metadata,toc

def make_pdf(fonts=(FONT,FONT)):
    doc=fitz.open()
    for i,font in enumerate(fonts):
        part=fitz.open();p=part.new_page(width=240,height=140)
        p.insert_font(fontname='Own',fontfile=str(font));p.insert_text((35,55),'pnx →',fontname='Own',fontsize=24)
        p.draw_rect(fitz.Rect(15,15,225,120),color=(0,0,1),width=.7)
        if i%2:p.set_rotation(90)
        doc.insert_pdf(part);part.close()
    doc.set_metadata({'title':'Original font dedup fixture','author':'Project-owned synthetic shapes','subject':'Metadata retained'})
    doc.set_toc([[1,'Own fixture',1]])
    return doc

def descriptors(doc):
    return [x for x in range(1,doc.xref_length())if doc.xref_get_key(x,'Type')==('name','/FontDescriptor')]

def ref(doc,descriptor,key='FontFile2'):
    return int(re.fullmatch(r'(\d+) 0 R',doc.xref_get_key(descriptor,key)[1])[1])

def reopen(doc):return fitz.open(stream=doc.tobytes(garbage=2,deflate=True),filetype='pdf')

class ExactFontStreamTests(unittest.TestCase):
    def test_duplicate_native_programs_preserve_all_pixels_text_fonts_positions_and_metadata(self):
        with make_pdf()as doc:
            expected=fingerprint(doc);report=deduplicate_font_streams(doc)
            self.assertEqual(report['pointer_changes'],1);self.assertEqual(report['canonical_programs'],1)
            with reopen(doc)as saved:self.assertEqual(fingerprint(saved),expected)

    def test_only_descriptor_pointers_change_no_stream_or_other_object_edits(self):
        with make_pdf()as doc:
            objects={x:doc.xref_object(x)for x in range(1,doc.xref_length())}
            streams={x:doc.xref_stream_raw(x)for x in objects if doc.xref_is_stream(x)}
            report=deduplicate_font_streams(doc);changed={r['descriptor']for r in report['changes']}
            for x,before in objects.items():
                if x not in changed:self.assertEqual(doc.xref_object(x),before)
            for x,raw in streams.items():self.assertEqual(doc.xref_stream_raw(x),raw)

    def test_same_postscript_name_different_original_programs_never_merge(self):
        with make_pdf((FONT,DISTINCT))as doc:
            expected=fingerprint(doc);report=deduplicate_font_streams(doc)
            self.assertEqual(report['pointer_changes'],0);self.assertEqual(report['canonical_programs'],2)
            with reopen(doc)as saved:self.assertEqual(fingerprint(saved),expected)

    def test_same_bytes_different_unknown_semantic_metadata_never_merge(self):
        with make_pdf()as doc:
            ds=descriptors(doc);doc.xref_set_key(ref(doc,ds[1]),'OriginalSemanticTag','/Different')
            expected=fingerprint(doc);report=deduplicate_font_streams(doc)
            self.assertEqual(report['pointer_changes'],0)
            with reopen(doc)as saved:self.assertEqual(fingerprint(saved),expected)

    def test_same_bytes_different_length1_never_merge(self):
        with make_pdf()as doc:
            ds=descriptors(doc);xref=ref(doc,ds[1]);typ,val=doc.xref_get_key(xref,'Length1')
            doc.xref_set_key(xref,'Length1',str(int(val)+1));report=deduplicate_font_streams(doc)
            self.assertEqual(report['pointer_changes'],0)

    def test_compressed_and_uncompressed_identical_decoded_programs_can_merge(self):
        with make_pdf()as doc:
            xref=ref(doc,descriptors(doc)[1]);data=doc.xref_stream(xref);doc.update_stream(xref,data,compress=False)
            expected=fingerprint(doc);report=deduplicate_font_streams(doc)
            self.assertEqual(report['pointer_changes'],1)
            with reopen(doc)as saved:self.assertEqual(fingerprint(saved),expected)

    def test_descriptor_metrics_are_kept_independent_while_program_is_shared(self):
        with make_pdf()as doc:
            ds=descriptors(doc);doc.xref_set_key(ds[1],'Ascent','999');doc.xref_set_key(ds[1],'Flags','96')
            expected=fingerprint(doc);report=deduplicate_font_streams(doc)
            self.assertEqual(report['pointer_changes'],1)
            self.assertEqual(doc.xref_get_key(ds[1],'Ascent'),('int','999'))
            with reopen(doc)as saved:self.assertEqual(fingerprint(saved),expected)

    def test_second_run_is_idempotent(self):
        with make_pdf((FONT,FONT,FONT))as doc:
            self.assertEqual(deduplicate_font_streams(doc)['pointer_changes'],2)
            self.assertEqual(deduplicate_font_streams(doc)['pointer_changes'],0)

    def test_non_font_duplicate_streams_are_untouched(self):
        with make_pdf()as doc:
            x,y=doc.get_new_xref(),doc.get_new_xref()
            for n in(x,y):doc.update_object(n,'<< /OriginalNotFont true >>');doc.update_stream(n,b'project-owned opaque duplicate')
            before=[doc.xref_object(n)for n in(x,y)];deduplicate_font_streams(doc)
            self.assertEqual([doc.xref_object(n)for n in(x,y)],before)

    def test_fontfile_kinds_and_subtypes_are_part_of_group_identity(self):
        # Dictionary-routing contract only; these unreferenced synthetic objects
        # deliberately do not claim to be rendered Type1/CFF font programs.
        with fitz.open()as doc:
            doc.new_page();groups=[]
            for key,subtype in [('FontFile',None),('FontFile',None),('FontFile2',None),('FontFile2',None),('FontFile3','Type1C'),('FontFile3','Type1C'),('FontFile3','OpenType')]:
                stream=doc.get_new_xref();doc.update_object(stream,'<<'+(' /Subtype /'+subtype if subtype else '')+' >>');doc.update_stream(stream,b'original-test-font-dictionary-routing')
                descriptor=doc.get_new_xref();doc.update_object(descriptor,f'<< /Type /FontDescriptor /FontName /Original /{key} {stream} 0 R >>');groups.append((descriptor,key,stream))
            r=deduplicate_font_streams(doc)
            self.assertEqual(r['pointer_changes'],3);self.assertEqual(r['change_counts'],{'FontFile':1,'FontFile2':1,'FontFile3':1})
            self.assertEqual(ref(doc,*groups[-1][:2]),groups[-1][2])

if __name__=='__main__':unittest.main(verbosity=2)
