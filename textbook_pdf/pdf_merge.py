"""Exact embedded-font-program stream deduplication before a garbage=2 save.

Only FontDescriptor FontFile/FontFile2/FontFile3 pointers may change. Decoded
program bytes AND every non-encoding stream dictionary value must be identical.
No font names, widths, encoding maps, outlines, descriptors, or page streams are
rewritten. Use garbage=2 afterwards; this never invokes generic object dedup.
"""
from collections import Counter
import hashlib,re,time

VERSION='font-program-stream-exact-dedup-v1'
FONT_KEYS=('FontFile','FontFile2','FontFile3')
# These encode transport, not the decoded program. All other keys, including
# Type1 Length1/2/3, OpenType Subtype, metadata and unknown extensions, stay exact.
ENCODING_KEYS=frozenset(('Length','Filter','DecodeParms'))
EXTERNAL_KEYS=frozenset(('F','FFilter','FDecodeParms'))
REF=re.compile(r'([1-9][0-9]*)\s+0\s+R\Z')

def _stream_semantics(doc,xref):
    keys=doc.xref_get_keys(xref)
    if EXTERNAL_KEYS.intersection(keys):raise ValueError('External font stream is unsupported')
    return tuple((key,*doc.xref_get_key(xref,key))for key in sorted(keys)if key not in ENCODING_KEYS)

def deduplicate_font_streams(doc):
    """Mutate only a caller-owned open PDF; return exact pointer-change evidence.

    Expected work is linear in xrefs plus decoded font-program bytes. A digest
    match is followed by actual byte equality; a hash never stands in for bytes.
    Unsupported/unreadable entries remain untouched and explicitly reported.
    """
    if not doc.is_pdf or doc.needs_pass:raise ValueError('Unlocked PDF required')
    start=time.perf_counter();registry={};streams={};changes=[];skipped=[]
    descriptor_count=font_refs=decoded_bytes=0;start_xrefs=doc.xref_length()
    for descriptor in range(1,start_xrefs):
        if doc.xref_get_key(descriptor,'Type')!=('name','/FontDescriptor'):continue
        descriptor_count+=1
        for key in FONT_KEYS:
            kind,value=doc.xref_get_key(descriptor,key)
            if kind=='null':continue
            font_refs+=1;ref=REF.fullmatch(value)if kind=='xref'else None
            if ref is None:
                skipped.append({'descriptor':descriptor,'key':key,'reason':'nonzero_generation_or_nonreference'});continue
            xref=int(ref[1]);identity=(key,xref)
            if identity in streams:
                info=streams[identity]
            else:
                try:
                    if not doc.xref_is_stream(xref):raise ValueError('Font program is not a stream')
                    semantics=_stream_semantics(doc,xref);data=doc.xref_stream(xref)
                    if not isinstance(data,bytes)or not data:raise ValueError('Empty/unreadable decoded font program')
                except (ValueError,RuntimeError,TypeError)as error:
                    skipped.append({'descriptor':descriptor,'key':key,'stream':xref,'reason':str(error)});continue
                decoded_bytes+=len(data);sha=hashlib.sha256(data).hexdigest()
                lookup=(key,semantics,len(data),sha)
                matches=registry.setdefault(lookup,[])
                canonical=next((prior for prior,raw in matches if raw==data),None)
                if canonical is None:canonical=xref;matches.append((xref,data))
                info={'canonical':canonical,'decoded_sha256':sha,'decoded_bytes':len(data),
                      'semantics':semantics,'semantic_dictionary_sha256':hashlib.sha256(repr(semantics).encode()).hexdigest()}
                streams[identity]=info
            canonical=info['canonical']
            if canonical==xref:continue
            before=doc.xref_get_key(descriptor,key)
            assert before==('xref',f'{xref} 0 R')
            doc.xref_set_key(descriptor,key,f'{canonical} 0 R')
            assert doc.xref_get_key(descriptor,key)==('xref',f'{canonical} 0 R')
            changes.append({'descriptor':descriptor,'font_key':key,'old_stream':xref,'canonical_stream':canonical,
                            'decoded_sha256':info['decoded_sha256'],'decoded_bytes':info['decoded_bytes'],
                            'semantic_dictionary_sha256':info['semantic_dictionary_sha256']})
    assert doc.xref_length()==start_xrefs
    removed={c['old_stream']for c in changes}
    return {'schema':VERSION,'state':'exact_font_program_references_rewritten_not_generic_pdf_dedup',
            'xref_count':start_xrefs,'font_descriptors':descriptor_count,'font_references':font_refs,
            'distinct_font_stream_refs_examined':len(streams),'canonical_programs':sum(len(v)for v in registry.values()),
            'pointer_changes':len(changes),'duplicate_stream_xrefs':len(removed),
            'decoded_bytes_hashed':decoded_bytes,'change_counts':dict(Counter(c['font_key']for c in changes)),
            'changes':changes,'skipped':skipped,'elapsed_seconds':round(time.perf_counter()-start,6),
            'next_save_garbage':2,'only_font_descriptor_program_pointers_changed':True}
