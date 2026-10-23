"""Project-owned synthetic font metadata; no textbook/system font outlines."""
from pathlib import Path
import hashlib,struct,unittest
from unittest.mock import patch
import pymupdf as fitz
from textbook_pdf.native_clipping import _embedded_postscript_names,_font_program_candidates,_embedded_font_sha,_font_resource_indices
from textbook_pdf.native_ink_geometry import measure_selected_native_ink
from textbook_pdf.extraction import _collect

def sfnt_names(names):
    # Metadata-only bytes test the bounded identity reader, not native outlines.
    records=[];storage=b''
    for platform,value in names:
        raw=value.encode('utf-16-be'if platform in(0,3)else'mac_roman')
        records.append(struct.pack('>HHHHHH',platform,1,0x409,6,len(raw),len(storage)));storage+=raw
    table=struct.pack('>HHH',0,len(records),6+12*len(records))+b''.join(records)+storage
    return b'\x00\x01\x00\x00'+struct.pack('>HHHH',1,16,0,0)+b'name'+struct.pack('>III',0,28,len(table))+table
def cff_names(names):
    strings=[value.encode('ascii')for value in names];offsets=[1]
    for value in strings:offsets.append(offsets[-1]+len(value))
    return bytes([1,0,4,1])+struct.pack('>H',len(names))+bytes([1])+bytes(offsets)+b''.join(strings)
class MetadataPage:
    def __init__(self,resources):self.resources=resources;self.parent=self;self.extract_calls=[]
    def get_fonts(self,full=True):return [(i,'ttf','Type0',name,'R'+str(i),'Identity-H',0)for i,name,_ in self.resources]
    def extract_font(self,i):self.extract_calls.append(i);return('dummy','ttf','Type0',next(data for n,_,data in self.resources if n==i))

class FontProgramMetadataTests(unittest.TestCase):
    def test_sfnt_unicode_and_mac_ps_name_agree(self):
        self.assertEqual(_embedded_postscript_names(sfnt_names([(3,'ExamplePS-Bold'),(1,'ExamplePS-Bold')])),('ExamplePS-Bold',))
    def test_cff_name_index_exact(self):
        self.assertEqual(_embedded_postscript_names(cff_names(['ExamplePS-Bold'])),('ExamplePS-Bold',))
    def test_actual_embedded_cff_program_metadata(self):
        data=fitz.Font('hebo').buffer;names=_embedded_postscript_names(data)
        self.assertEqual(len(names),1);self.assertIn('Nimbus',names[0])
        page=MetadataPage([(1,'Unrelated Human Family',data)])
        self.assertEqual(_embedded_font_sha(page,names[0]),hashlib.sha256(data).hexdigest())
    def test_truncated_human_fullname_real_ps_exact_source(self):
        font=Path(__file__).parent/'fixtures/original-ordinary-ink.ttf'
        with fitz.open()as doc:
            p=doc.new_page();p.insert_font(fontname='owned',fontfile=str(font));p.insert_text((40,70),'p',fontname='owned')
            _,glyphs=_collect(p);g=glyphs[0];row=p.get_fonts(full=True)[0]
            self.assertNotEqual(g['font'],row[3]);self.assertEqual(len(row[3]),31)
            self.assertEqual(_font_resource_indices(g['font'],[row[3]]),[])
            self.assertEqual(_embedded_postscript_names(doc.extract_font(row[0])[3]),(g['font'],))
            result=measure_selected_native_ink(p,glyphs,{g['id']});self.assertFalse(result['unverified'])
            self.assertEqual(_embedded_font_sha(p,g['font']),result['verified'][0]['identity']['font_program_sha256'])
    def test_same_ps_different_real_owned_programs_remain_rejected(self):
        base=Path(__file__).parent/'fixtures'
        left=(base/'original-ordinary-ink.ttf').read_bytes();right=(base/'original-ordinary-ink-distinct.ttf').read_bytes()
        self.assertNotEqual(left,right);name=_embedded_postscript_names(left)[0]
        self.assertEqual(_embedded_postscript_names(right),(name,))
        page=MetadataPage([(1,'Human Family One',left),(2,'Human Family Two',right)])
        with self.assertRaisesRegex(ValueError,'ambiguous'):_embedded_font_sha(page,name)
    def test_same_ps_same_program_different_resources_accepted(self):
        data=sfnt_names([(3,'ExamplePS')]);page=MetadataPage([(1,'Human A',data),(2,'Human B',data)])
        self.assertEqual(_embedded_font_sha(page,'ExamplePS'),hashlib.sha256(data).hexdigest())
    def test_metadata_fallback_is_case_and_punctuation_exact(self):
        data=sfnt_names([(3,'ExamplePS-Bold')])
        self.assertEqual(_font_program_candidates('examplepsbold',['Human Family'],lambda i:data),[])
    def test_metadata_cannot_restore_arbitrary_short_prefix(self):
        data=sfnt_names([(3,'ExamplePS-BoldItalic')])
        self.assertEqual(_font_program_candidates('ExamplePS-Bold',['Human Family'],lambda i:data),[])
    def test_legal_subset_prefix_can_be_removed_for_exact_ps_identity(self):
        data=sfnt_names([(3,'ABCDEF+ExamplePS')])
        self.assertEqual(_font_program_candidates('ExamplePS',['Human Family'],lambda i:data),[(0,data)])
    def test_arbitrary_plus_is_not_a_subset_prefix(self):
        data=sfnt_names([(3,'Example+PS')])
        self.assertEqual(_font_program_candidates('PS',['Human Family'],lambda i:data),[])
    def test_exact_named_resource_does_not_load_unrelated_program(self):
        page=MetadataPage([(1,'ExamplePS',b'exact-program'),(2,'Other Font',b'unrelated')])
        self.assertEqual(_embedded_font_sha(page,'ExamplePS'),hashlib.sha256(b'exact-program').hexdigest())
        self.assertEqual(page.extract_calls,[1])
    def test_missing_exact_resource_cannot_use_other_metadata(self):
        page=MetadataPage([(1,'ExamplePS',b''),(2,'Other Human Name',sfnt_names([(3,'ExamplePS')]))])
        with self.assertRaisesRegex(ValueError,'unavailable'):_embedded_font_sha(page,'ExamplePS')
    def test_unknown_missing_and_type1_cannot_supply_fallback_identity(self):
        for data in(b'',b'bad',b'%!PS-AdobeFont-1.0 /FontName /ExamplePS def',b'ttcf'+b'\0'*100):
            with self.subTest(data=data[:4]):
                self.assertEqual(_embedded_postscript_names(data),())
                self.assertEqual(_font_program_candidates('ExamplePS',['Human Family'],lambda i:data),[])
    def test_conflicting_name_id6_languages_do_not_supply_unique_identity(self):
        data=sfnt_names([(3,'ExamplePS'),(1,'DifferentPS')])
        self.assertEqual(_font_program_candidates('ExamplePS',['Human Family'],lambda i:data),[])
    def test_multiple_cff_names_do_not_guess_a_face(self):
        self.assertEqual(_font_program_candidates('ExamplePS',['Human Family'],lambda i:cff_names(['ExamplePS','AnotherPS'])),[])
    def test_sfnt_name_table_outside_program_rejected(self):
        data=bytearray(sfnt_names([(3,'ExamplePS')]));data[20:24]=(1000000).to_bytes(4,'big')
        self.assertEqual(_embedded_postscript_names(bytes(data)),())
    def test_sfnt_strings_overlapping_records_rejected(self):
        data=bytearray(sfnt_names([(3,'ExamplePS')]));data[32:34]=b'\x00\x00'
        self.assertEqual(_embedded_postscript_names(bytes(data)),())
    def test_truncated_sfnt_and_cff_offsets_rejected(self):
        for data in(sfnt_names([(3,'ExamplePS')])[:-2],cff_names(['ExamplePS'])[:-1],bytes([1,0,4,1,0,1,5])):
            self.assertEqual(_embedded_postscript_names(data),())
    def test_invalid_ps_space_rejected(self):
        self.assertEqual(_embedded_postscript_names(sfnt_names([(3,'Not A PS Name')])),())

if __name__=='__main__':unittest.main()
