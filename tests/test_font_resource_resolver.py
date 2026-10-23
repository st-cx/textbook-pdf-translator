"""Meaningful resource-selection regressions; includes actual pinned-MuPDF truncation."""
import hashlib,json,re,unittest
import pymupdf as fitz
from textbook_pdf.native_clipping import _font_resource_indices,_embedded_font_sha
from textbook_pdf.native_ink_geometry import measure_selected_native_ink
from textbook_pdf.extraction import _collect

def h(data):return hashlib.sha256(data).hexdigest()
class FakeDoc:
    def __init__(self,data):self.data=data
    def extract_font(self,xref):return ('dummy','otf','Type0',self.data[xref])
class FakePage:
    def __init__(self,resources):
        self.resources=resources;self.parent=FakeDoc({x[0]:x[2]for x in resources})
    def get_fonts(self,full=True):return [(xref,'otf','Type0',name,'R'+str(xref),'Identity-H',0)for xref,name,_ in self.resources]

def renamed_pdf(font_names):
    doc=fitz.open();page=doc.new_page(width=300,height=100)
    for i,(reported,builtin)in enumerate(font_names):
        xref=page.insert_font(fontname='Resource'+str(i),fontbuffer=fitz.Font(builtin).buffer)
        page.insert_text((30+90*i,45),'p',fontsize=12,fontname='Resource'+str(i))
        child=int(re.search(r'(\d+) 0 R',doc.xref_get_key(xref,'DescendantFonts')[1])[1])
        descriptor=int(re.search(r'(\d+) 0 R',doc.xref_get_key(child,'FontDescriptor')[1])[1])
        for resource,key in((xref,'BaseFont'),(child,'BaseFont'),(descriptor,'FontName')):
            doc.xref_set_key(resource,key,'/'+reported)
    data=doc.tobytes();doc.close();return fitz.open(stream=data,filetype='pdf')

class ResourceResolverTests(unittest.TestCase):
    def test_short_complete_bold_does_not_match_bolditalic(self):
        self.assertEqual(_font_resource_indices('STIXGeneral-Bold',['STIXGeneral-Bold','STIXGeneral-BoldItalic']),[0])
    def test_short_complete_italic_exact(self):
        self.assertEqual(_font_resource_indices('STIXGeneral-BoldItalic',['STIXGeneral-Bold','STIXGeneral-BoldItalic']),[1])
    def test_subset_prefix_and_spacing_normalization(self):
        self.assertEqual(_font_resource_indices('STIXGeneral-Bold',['ABCDEF+STIX General Bold','DEFGHI+STIXGeneral-BoldItalic']),[0])
    def test_short_unmatched_cannot_guess_prefix(self):
        self.assertEqual(_font_resource_indices('STIXGeneral-Bold',['STIXGeneral-BoldItalic']),[])
    def test_full_long_name_not_artificially_truncated(self):
        name='SomeExtremelyLongPostScriptFontFamily-Bold'
        self.assertEqual(_font_resource_indices(name,[name+'Italic']),[])
    def test_exact31_beats_longer_prefix(self):
        name='ABCDEFGHIJKLMNOPQRSTUVWXYZabcde';self.assertEqual(len(name),31)
        self.assertEqual(_font_resource_indices(name,[name,name+'Bold']),[0])
    def test_empty_reported_rejects_all(self):
        self.assertEqual(_font_resource_indices('', ['AnyFont']),[])
    def test_exact_same_program_subsets_are_accepted(self):
        page=FakePage([(1,'ABCDEF+STIXGeneral-Bold',b'program'),(2,'DEFGHI+STIXGeneral-Bold',b'program'),(3,'STIXGeneral-BoldItalic',b'other')])
        self.assertEqual(_embedded_font_sha(page,'STIXGeneral-Bold'),h(b'program'))
    def test_exact_different_program_subsets_reject(self):
        page=FakePage([(1,'ABCDEF+STIXGeneral-Bold',b'one'),(2,'DEFGHI+STIXGeneral-Bold',b'two')])
        with self.assertRaisesRegex(ValueError,'ambiguous'):_embedded_font_sha(page,'STIXGeneral-Bold')
    def test_missing_exact_program_cannot_fall_back_to_italic(self):
        page=FakePage([(1,'STIXGeneral-Bold',b''),(2,'STIXGeneral-BoldItalic',b'other')])
        with self.assertRaisesRegex(ValueError,'unavailable'):_embedded_font_sha(page,'STIXGeneral-Bold')
    def test_one_empty_same_name_subset_cannot_be_ignored(self):
        page=FakePage([(1,'ABCDEF+STIXGeneral-Bold',b'program'),(2,'DEFGHI+STIXGeneral-Bold',b'')])
        with self.assertRaisesRegex(ValueError,'unavailable'):_embedded_font_sha(page,'STIXGeneral-Bold')
    def test_unmatched_program_rejects(self):
        with self.assertRaises(ValueError):_embedded_font_sha(FakePage([(1,'Another',b'program')]),'Missing')
    def test_truncated_same_program_subsets_accept(self):
        long='SomeExtremelyLongPostScriptFontFamily-Bold';short=long[:31]
        self.assertEqual(_embedded_font_sha(FakePage([(1,'ABCDEF+'+long,b'program'),(2,'DEFGHI+'+long,b'program')]),short),h(b'program'))
    def test_truncated_distinct_programs_reject(self):
        long='SomeExtremelyLongPostScriptFontFamily-Bold';short=long[:31]
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            _embedded_font_sha(FakePage([(1,long,b'one'),(2,short+'Italic',b'two')]),short)
    def test_actual_pdf_short_bold_bolditalic_core_callers(self):
        with renamed_pdf([('STIXGeneral-Bold','hebo'),('STIXGeneral-BoldItalic','hebi')])as doc:
            page=doc[0];_,glyphs=_collect(page);self.assertEqual({g['font']for g in glyphs},{'STIXGeneral-Bold','STIXGeneral-BoldItalic'})
            for glyph in glyphs:
                expected=next(doc.extract_font(row[0])[3]for row in page.get_fonts(full=True)if row[3]==glyph['font'])
                self.assertEqual(_embedded_font_sha(page,glyph['font']),h(expected))
                measured=measure_selected_native_ink(page,glyphs,{glyph['id']})
                self.assertFalse(measured['unverified']);self.assertEqual(len(measured['verified']),1)
                self.assertEqual(measured['verified'][0]['identity']['font_program_sha256'],h(expected))
    def test_actual_pinned_mupdf31truncation_core_callers(self):
        full='SomeExtremelyLongPostScriptFontFamily-Bold'
        with renamed_pdf([(full,'hebo')])as doc:
            page=doc[0];_,glyphs=_collect(page);self.assertEqual(len(glyphs),1);g=glyphs[0]
            self.assertEqual(g['font'],full[:31]);self.assertEqual(len(g['font']),31)
            self.assertEqual(page.get_fonts(full=True)[0][3],full)
            data=doc.extract_font(page.get_fonts(full=True)[0][0])[3]
            self.assertEqual(_embedded_font_sha(page,g['font']),h(data))
            measured=measure_selected_native_ink(page,glyphs,{g['id']})
            self.assertFalse(measured['unverified']);self.assertEqual(measured['verified'][0]['identity']['font_program_sha256'],h(data))
    def test_actual31truncation_before_subset_strip_core_callers(self):
        full='ABCDEF+SomeLongPostScriptFontFamily-Regular'
        with renamed_pdf([(full,'hebo')]) as doc:
            page=doc[0];_,glyphs=_collect(page);g=glyphs[0]
            self.assertEqual(g['font'],full[:31].split('+')[-1])
            self.assertEqual(len(g['font']),24)
            data=doc.extract_font(page.get_fonts(full=True)[0][0])[3]
            self.assertEqual(_embedded_font_sha(page,g['font']),h(data))
            measured=measure_selected_native_ink(page,glyphs,{g['id']})
            self.assertFalse(measured['unverified'])
    def test_pre_subset31_different_programs_still_reject(self):
        left='ABCDEF+SomeLongPostScriptFontFamily-Regular'
        right='DEFGHI+'+left.split('+')[-1]
        with self.assertRaisesRegex(ValueError,'ambiguous'):
            _embedded_font_sha(FakePage([(1,left,b'one'),(2,right,b'two')]),left[:31].split('+')[-1])
    def test_short_prefix_without_legal_subsettag_still_reject(self):
        name='SomeLongPostScriptFontFamily-Regular';short=('ABCDEF+'+name)[:31].split('+')[-1]
        self.assertEqual(_font_resource_indices(short,[name]),[])
    def test_arbitrary_short_subset_prefix_still_reject(self):
        name='ABCDEF+SomeLongPostScriptFontFamily-Regular'
        self.assertEqual(_font_resource_indices('SomeLongPostScript', [name]),[])
    def test_base14_unembedded_remains_rejected(self):
        with fitz.open()as doc:
            page=doc.new_page();page.insert_text((40,40),'p');_,glyphs=_collect(page)
            with self.assertRaises(ValueError):_embedded_font_sha(page,glyphs[0]['font'])
            measured=measure_selected_native_ink(page,glyphs,{glyphs[0]['id']})
            self.assertFalse(measured['verified']);self.assertTrue(measured['unverified'])

if __name__=='__main__':unittest.main(verbosity=2)
