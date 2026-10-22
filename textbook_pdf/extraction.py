"""Source-grounded textbook extraction with immutable vector math crops.

Every extracted source character receives one disposition in a compressed page
ledger. Native recognition is deliberately conservative and reports ambiguity;
it is not an OCR engine and never reports a heuristic as verified mathematics.
"""
from __future__ import annotations

from collections import Counter
import copy
from dataclasses import asdict, replace
import gc
import gzip
import json
import re
from pathlib import Path
import statistics
import unicodedata

import pymupdf as fitz

from .models import Asset, Book, Element, digest, file_sha256, RULES_VERSION, TOKEN_RE
from .drawing_geometry import source_drawings

MATH_FONT = re.compile(r"math|symbol|cmmi|cmsy|cmex|msam|msbm|mt.?extra|euclid", re.I)
CODE_FONT = re.compile(r"courier|mono|consol|lettergot|typewriter|cmt[tv]", re.I)
EXERCISE = re.compile(r"^\s*(?:[★*∗⋆]\s*)?(\d{1,3})\.\s")
SUBQUESTION = re.compile(r"^\s*([a-z]|[ivxlcdm]{2,6})\s*\)\s*")
CAPTION = re.compile(r"^\s*(FIGURE|TABLE)\s*\d+(?:\.\d+)*(?=\s|$|[.:])")
MATH_SYMBOLS = set("=+−×÷<>≤≥≠≈∼→↔⇒⇔∀∃∈∉⊆⊂∪∩∅¬∧∨⊕∑∏∫√∞∴∵∣∥∂∇ℝℤℕℚℂπθλμσφω∕")
MATH_NEUTRAL = set("0123456789()[]{}|/.,:;!′'\"_^+-= ")


def _rect_union(rects):
    rects = [fitz.Rect(r) for r in rects if r]
    if not rects:
        return fitz.Rect()
    result = fitz.Rect(rects[0])
    for rect in rects[1:]:
        result |= rect
    return result


def _rounded(rect):
    return [round(float(v), 4) for v in rect]


def _issue(book, code, detail, page=None, element_id=None, severity="warning"):
    book.issues.append({"code": code, "detail": detail, "page": page,
                        "element_id": element_id, "severity": severity})


def _atomic_json(path,data):
    path=Path(path)
    temporary=path.with_suffix(path.suffix+".tmp")
    temporary.write_text(json.dumps(data,ensure_ascii=False,separators=(",",":")),encoding="utf-8")
    temporary.replace(path)


def _checkpoint_manifest(book,outdir,signature,completed):
    _atomic_json(Path(outdir)/"analysis.partial.json",{
        "format":"textbook-analysis-page-parts-v1","signature":signature,
        "source_sha256":book.source_sha256,"selected_pages":book.selected_pages,
        "processed_pages":[n for n in book.selected_pages if n in completed],
        "profile":book.profile,"global_issues":[i for i in book.issues if i.get("page") is None]})


def _checkpoint_page(book,outdir,signature,number):
    _atomic_json(Path(outdir)/"analysis.parts"/f"p{number:04d}.json",{
        "signature":signature,"page":next(p for p in book.pages if p["number"]==number),
        "elements":[asdict(e) for e in book.elements if e.page==number],
        "assets":{a.id:asdict(a) for a in book.assets.values() if a.page==number},
        "issues":[i for i in book.issues if i.get("page")==number]})


def _restore_checkpoint(book,outdir,signature):
    partial=Path(outdir)/"analysis.partial.json"
    if not partial.exists():return book
    manifest=json.loads(partial.read_text(encoding="utf-8"))
    if manifest.get("format")!="textbook-analysis-page-parts-v1" or manifest.get("signature")!=signature:return book
    restored=Book(book.source_path,book.source_sha256,book.title,book.page_count,book.selected_pages)
    restored.profile=manifest["profile"]
    restored.issues=manifest.get("global_issues",[])
    checked={}
    for number in manifest["processed_pages"]:
        if number not in book.selected_pages:raise ValueError("Checkpoint has unexpected source page")
        part=json.loads((Path(outdir)/"analysis.parts"/f"p{number:04d}.json").read_text(encoding="utf-8"))
        if part["signature"]!=signature or part["page"]["number"]!=number:raise ValueError("Stale page checkpoint")
        restored.pages.append(part["page"])
        restored.elements.extend(Element(**e) for e in part["elements"])
        restored.issues.extend(part["issues"])
        for aid,record in part["assets"].items():
            asset=Asset(**record)
            saved=(Path(outdir)/asset.path).resolve()
            if not saved.is_relative_to(Path(outdir).resolve()):raise ValueError("Checkpoint asset path escapes job")
            if asset.path not in checked:checked[asset.path]=file_sha256(saved)
            if checked[asset.path]!=asset.sha256:raise ValueError("Checkpoint asset changed")
            restored.assets[aid]=asset
    restored.profile["analysis_resumed_pages"]=len(restored.pages)
    return restored


def _collect(page):
    lines, glyphs = [], []
    raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_LIGATURES | fitz.TEXT_PRESERVE_WHITESPACE)
    for block_index, block in enumerate(raw["blocks"]):
        if block["type"] != 0:
            continue
        for line_index, line in enumerate(block["lines"]):
            chars = []
            for span_index, span in enumerate(line["spans"]):
                for char_index, char in enumerate(span["chars"]):
                    g = {"id": f"b{block_index}.l{line_index}.s{span_index}.c{char_index}",
                         "c": char["c"], "bbox": list(char["bbox"]),
                         "origin": list(char["origin"]), "font": span["font"],
                         "size": float(span["size"]), "flags": span["flags"],
                         "color": span.get("color", 0), "owner": None,
                         "disposition": None, "block": block_index, "line": line_index}
                    chars.append(g)
                    glyphs.append(g)
            if chars:
                lines.append({"bbox": list(line["bbox"]), "chars": chars,
                              "text": "".join(c["c"] for c in chars),
                              "block": block_index, "line": line_index,
                              "dir": list(line.get("dir", (1, 0)))})
    from .glyph_geometry import correct_native_math_boxes
    correct_native_math_boxes(page, glyphs)
    runs=[]
    for line in lines:
        if line.get("dir")!=[1,0]:runs.append(line);continue
        ordered=sorted(line["chars"],key=lambda g:(g["bbox"][0],g["origin"][1]))
        chunks=[];chunk=[]
        for glyph in ordered:
            gap=glyph["bbox"][0]-chunk[-1]["bbox"][2] if chunk else 0
            midpoint=(glyph["bbox"][0]+chunk[-1]["bbox"][2])/2 if chunk else 0
            gutter=page.rect.width*.43<midpoint<page.rect.width*.56 and gap>max(12,glyph["size"]*1.25)
            if chunk and (gap>max(30,glyph["size"]*3.5) or gutter):
                chunks.append(chunk);chunk=[]
            chunk.append(glyph)
        if chunk:chunks.append(chunk)
        for chunk in chunks:runs.append({**line,"chars":chunk,"text":"".join(g["c"] for g in chunk),"bbox":_rounded(_rect_union(g["bbox"] for g in chunk))})
    coalesced=_coalesce_fragments(runs)
    for line in coalesced:
        ordinary=[g for g in line["chars"] if "extensions" not in g["font"].lower()]
        size=statistics.median(g["size"] for g in ordinary or line["chars"])
        normal=[g for g in ordinary if g["size"]>=size*.9]
        line["baseline"]=statistics.median(g["origin"][1] for g in normal or ordinary or line["chars"])
    return coalesced,glyphs


def _coalesce_fragments(lines):
    """Attach detached radicals/scripts to their actual source text line.

    PDF extractors frequently put a radical in one block and its radicand in
    another. Source origins and horizontal adjacency beat top-coordinate order.
    """
    # Section numbers and their titles are often separate PDF text objects.
    heading_removed=set()
    previous=None
    for line in sorted(lines,key=lambda l:(l["block"],l["bbox"][1],l["bbox"][0])):
        gs=[g for g in line["chars"] if g["c"].strip()]
        if not gs:continue
        size=statistics.median(g["size"] for g in gs)
        bold=all(g["flags"]&16 for g in gs if g["c"].isalpha())
        if previous is not None and bold and size>12:
            pg=previous["chars"]
            if (previous["block"]==line["block"] and abs(statistics.median(g["origin"][1] for g in pg)-statistics.median(g["origin"][1] for g in gs))<.75
                and abs(statistics.median(g["size"] for g in pg)-size)<.3
                and 0<=line["bbox"][0]-previous["bbox"][2]<size*3):
                previous["chars"].extend(line["chars"])
                previous["bbox"]=_rounded(_rect_union([previous["bbox"],line["bbox"]]))
                previous["text"]="".join(g["c"] for g in previous["chars"])
                heading_removed.add(id(line))
                continue
        previous=line if bold and size>12 else None
    lines=[l for l in lines if id(l) not in heading_removed]
    # A detached radical owns the radicand immediately to its RIGHT. Looking
    # only left incorrectly attaches it to an equation's left hand side.
    # Require the native glyph boundary and vertical support to agree.
    radical_removed=set()
    for root in sorted(lines,key=lambda l:l["bbox"][0],reverse=True):
        rgs=[g for g in root["chars"] if g["c"].strip()]
        if len(rgs)!=1 or rgs[0]["c"]!="√":continue
        rg=rgs[0];r=fitz.Rect(root["bbox"]);size=rg["size"];candidates=[]
        for target in lines:
            if target is root or id(target) in radical_removed:continue
            tgs=[g for g in target["chars"] if g["c"].strip()]
            if not tgs:continue
            first=min(tgs,key=lambda g:g["bbox"][0]);br=fitz.Rect(target["bbox"])
            gap=br.x0-r.x1
            if not -.6<=gap<max(1.1,size*.2):continue
            if not(_is_math(first) or first["flags"]&2 or first["c"].isdigit()):continue
            support=first["origin"][1]-rg["origin"][1]
            if not -size*.4<=support<=size*1.45:continue
            if min(br.y1,r.y1)-max(br.y0,r.y0)<-size*.2:continue
            candidates.append((abs(gap)+abs(support)*.1,target))
        if candidates:
            target=min(candidates,key=lambda pair:pair[0])[1]
            target["chars"]=root["chars"]+target["chars"]
            target["chars"].sort(key=lambda g:(g["bbox"][0],g["origin"][1]))
            target["bbox"]=_rounded(_rect_union([target["bbox"],root["bbox"]]))
            target["text"]="".join(g["c"] for g in target["chars"])
            radical_removed.add(id(root))
    lines=[l for l in lines if id(l) not in radical_removed]
    bases=[l for l in lines if len(l["text"].strip())>=2]
    removed=set()
    for line in sorted(lines,key=lambda l:l["bbox"][0]):
        gs=[g for g in line["chars"] if g["c"].strip()]
        if not gs or len(gs)>5 or not all(_is_math(g) or g["flags"]&2 or g["c"].isdigit() for g in gs):
            continue
        r=fitz.Rect(line["bbox"])
        baseline=statistics.median(g["origin"][1] for g in gs)
        size=max(g["size"] for g in gs)
        candidates=[]
        for base in bases:
            if base is line or id(base) in removed:
                continue
            br=fitz.Rect(base["bbox"])
            bb=statistics.median(g["origin"][1] for g in base["chars"])
            if all(g["c"].isdigit() for g in gs) and not size<statistics.median(g["size"] for g in base["chars"])*.9:
                continue
            gap=r.x0-br.x1
            base_size=max(g["size"] for g in base["chars"] if "extensions" not in g["font"].lower()) if any("extensions" not in g["font"].lower() for g in base["chars"]) else size
            normal=abs(baseline-bb)<size*1.05
            raised_script=(size<base_size*.82 and baseline<bb and bb-baseline<base_size*1.7 and abs(gap)<max(1.2,size*.25))
            if -.8<=gap<size*1.6 and (normal or raised_script):
                candidates.append((abs(gap)+abs(baseline-bb),base))
        if candidates:
            target=min(candidates,key=lambda c:c[0])[1]
            target["chars"].extend(line["chars"])
            target["chars"].sort(key=lambda g:(g["bbox"][0],g["origin"][1]))
            target["bbox"]=_rounded(_rect_union([target["bbox"],r]))
            target["text"]="".join(g["c"] for g in target["chars"])
            removed.add(id(line))
    lines=[l for l in lines if id(l) not in removed]
    # A radical often separates the rest of its original baseline into a new
    # PDF block, including ordinary prose after the radicand. Join horizontal
    # neighbors by dominant normal-glyph baseline before source tokenization.
    baseline=lambda line:statistics.median(g["origin"][1] for g in line["chars"] if "extensions" not in g["font"].lower()) if any("extensions" not in g["font"].lower() for g in line["chars"]) else statistics.median(g["origin"][1] for g in line["chars"])
    joined=[]
    for line in sorted(lines,key=lambda l:(round(baseline(l),1),l["bbox"][0])):
        previous=joined[-1] if joined else None
        size=statistics.median(g["size"] for g in line["chars"])
        if (previous is not None and abs(baseline(previous)-baseline(line))<.65
            and -.8<=line["bbox"][0]-previous["bbox"][2]<max(4,size*.7)):
            previous["chars"].extend(line["chars"])
            previous["chars"].sort(key=lambda g:(g["bbox"][0],g["origin"][1]))
            previous["bbox"]=_rounded(_rect_union([previous["bbox"],line["bbox"]]))
            previous["text"]="".join(g["c"] for g in previous["chars"])
        else:joined.append(line)
    return joined


def _printed_page(page, lines):
    from .source_pages import recognize_printed_page
    return recognize_printed_page(page,lines)


def _columns(lines, width):
    natural=lambda line:len(re.findall(r"[A-Za-z]{3,}",line["text"]))>=2
    body=[l for l in lines if l["bbox"][1]>48 and natural(l)]
    mids=[l for l in body if width*.42<l["bbox"][0]<width*.61 and l["bbox"][2]-l["bbox"][0]<width*.5]
    lefts=[l for l in body if l["bbox"][0]<width*.28 and l["bbox"][2]<width*.52]
    if len(mids)>=3 and len(lefts)>=3:
        start=min(l["bbox"][1] for l in mids)
        anchors=[l["bbox"][0] for l in lines if width*.42<l["bbox"][0]<width*.61 and (EXERCISE.match(l["text"]) or SUBQUESTION.match(l["text"]))]
        right_edges=sorted(anchors or [l["bbox"][0] for l in mids])
        left_edges=sorted(l["bbox"][2] for l in lefts)
        split=(right_edges[max(0,int(len(right_edges)*.1))]+left_edges[min(len(left_edges)-1,int(len(left_edges)*.9))])/2
        broad=[l for l in body if l["bbox"][1]>=start-2 and l["bbox"][0]<split-width*.15 and l["bbox"][2]>split+width*.15]
        if width*.43<split<width*.56:
            end=float("inf")
            if len(broad)>=3:
                first_broad=min(l["bbox"][1] for l in broad)
                if first_broad-start>240:
                    transitions=[l["bbox"][1] for l in lines if start+200<l["bbox"][1]<=first_broad and l["bbox"][0]<split<l["bbox"][2] and statistics.median(g["size"] for g in l["chars"])>12.5]
                    end=min(transitions) if transitions else first_broad
                else:return {"count":1,"split":width/2,"start_y":float("inf")}
            return {"count":2,"split":split,"start_y":start,"end_y":end}
    return {"count":1,"split":width/2,"start_y":float("inf")}


def _apply_structure_hints(lines,columns,structure,page_width):
    if not structure.get("usable"):return {"usable":False,"reason":"no verified upstream paragraph coverage"}
    paragraphs=structure.get("paragraphs",[])
    if not paragraphs:return {"usable":True,"source_lines":len(lines),"assigned_lines":0}
    assigned=0
    for line in lines:
        gs=[g for g in line["chars"] if g["c"].strip()]
        if not gs:continue
        scored=[]
        for paragraph in paragraphs:
            rect=fitz.Rect(paragraph["bbox"])
            hits=sum((rect+(-g["size"]*.4,-g["size"]*.4,g["size"]*.4,g["size"]*.4)).contains((fitz.Rect(g["bbox"]).tl+fitz.Rect(g["bbox"]).br)*.5) for g in gs)
            if hits:scored.append((hits,paragraph))
        if scored:
            hits,chosen=max(scored,key=lambda pair:(pair[0],-fitz.Rect(pair[1]["bbox"]).get_area()))
            if hits>=len(gs)*.6:assigned+=1
            else:continue
        else:continue
        line.update(structure_id=chosen["id"],structure_class=chosen["layout_class"],structure_order=chosen["order"])
    # Only use the model's reading order when it covers all source text lines.
    # Native glyph/element IDs and per-question boundaries remain unchanged.
    substantive=[line for line in lines if line["text"].strip()]
    if assigned<len(substantive):
        for line in lines:line.pop("structure_order",None)
    narrow=[p for p in paragraphs if p["layout_class"]=="plain text" and p["bbox"][2]-p["bbox"][0]<page_width*.48 and p["bbox"][3]-p["bbox"][1]>50]
    pairs=[(left,right) for left in narrow for right in narrow if left["bbox"][2]<right["bbox"][0] and left["bbox"][0]<page_width*.35 and right["bbox"][0]>page_width*.4 and min(left["bbox"][3],right["bbox"][3])-max(left["bbox"][1],right["bbox"][1])>40]
    if columns["count"]==2 and pairs:
        left,right=max(pairs,key=lambda pair:min(pair[0]["bbox"][3],pair[1]["bbox"][3])-max(pair[0]["bbox"][1],pair[1]["bbox"][1]))
        later=[p["bbox"][1] for p in paragraphs if p["order"]>max(left["order"],right["order"]) and p["bbox"][1]>max(left["bbox"][3],right["bbox"][3])+6]
        columns["split"]=(left["bbox"][2]+right["bbox"][0])/2
        if later:columns["end_y"]=min(columns.get("end_y",float("inf")),min(later))
        columns["structure_evidence"]={"left":left["id"],"right":right["id"],"end_y":columns.get("end_y")}
    return {"usable":True,"source_lines":len(substantive),"assigned_lines":assigned,
            "reading_order_used":assigned==len(substantive),"provenance":"BabelDOC LayoutParser and ParagraphFinder; native glyphs retained"}


def _order(lines, columns):
    ordinate=lambda l:l["bbox"][3] if l["text"].strip() in {"◂","▸","■","□","◻","◼","▹"} else l.get("baseline",l["bbox"][1])
    if any(l.get("structure_order") is not None for l in lines):
        return sorted(lines,key=lambda l:(l.get("structure_order",10**6),round(ordinate(l),1),l["bbox"][0]))
    if columns["count"] == 1:
        return sorted(lines, key=lambda l: (round(ordinate(l), 1), l["bbox"][0]))
    split, start = columns["split"], columns["start_y"]
    # Full width text before a two-column exercise section remains ahead of
    # both columns. Within the section the entire left column precedes right.
    def key(l):
        r = fitz.Rect(l["bbox"])
        if r.y0 < start-2:
            return (0, ordinate(l), r.x0)
        if r.y0>=columns.get("end_y",float("inf"))-2:
            return (4,ordinate(l),r.x0)
        column = 0 if (r.x0+r.x1)/2 < split else 1
        if r.x0 < split-20 and r.x1 > split+20:
            return (1, ordinate(l), r.x0)
        return (2+column, ordinate(l), r.x0)
    return sorted(lines, key=key)


def _is_math(g):
    t = g["c"]
    # MathJax's upright Main font also embeds ordinary English diagram labels.
    # Its Latin letters require mathematical context, unlike dedicated symbol
    # fonts. Treating its family name as proof hid complete English sentences.
    font_math=MATH_FONT.search(g["font"]) is not None
    if re.search(r"mathjax_main-(?:regular|bold)$",g["font"],re.I) and re.fullmatch(r"[A-Za-z]",t) and not g["flags"]&2:
        font_math=False
    return bool(t.strip()) and (font_math or t in MATH_SYMBOLS
           or unicodedata.category(t[0]) == "Sm" or 0x1d400<=ord(t[0])<=0x1d7ff
           or (t=="\ufffd" and "cambria" in g["font"].lower()))


def _subquestion(line):
    match=SUBQUESTION.match(line["text"])
    if not match:return None
    first=next((g for g in line["chars"] if g["c"].strip()),None)
    # r) at a wrapped mathematical expression is not an exercise label. The
    # source uses upright/bold question labels and italic mathematical variables.
    if first and first["flags"]&2 and len(match[1])==1:return None
    return match


def _math_spans(line, hints=()):
    chars = line["chars"]
    if re.search(r"[©Ⓒ⃝]",line["text"]) or re.match(r"^\s*(?:Source|Photo(?:graph)? credit|Image credit)\s*:",line["text"],re.I):
        return []
    mask = [_is_math(g) for g in chars]
    prefix=_subquestion(line) or EXERCISE.match(line["text"])
    prefix_end=prefix.end() if prefix else 0
    # Single italic variable runs are protected, long italic prose stays text.
    i = 0
    while i < len(chars):
        j = i+1
        while j < len(chars) and chars[j]["font"] == chars[i]["font"] and chars[j]["flags"] == chars[i]["flags"]:
            j += 1
        text = "".join(g["c"] for g in chars[i:j]).strip()
        function_run=bool(re.fullmatch(r"[A-Za-zΑ-ω_]{1,40}",text) and j<len(chars) and chars[j]["c"]=="(" and not chars[j-1]["c"].isspace())
        scripted_run=bool(re.fullmatch(r"[A-Za-zΑ-ω_]{2,40}",text)
            and (min(g['size'] for g in chars[i:j])<max(g['size'] for g in chars[i:j])*.85
                 or max(g['origin'][1] for g in chars[i:j])-min(g['origin'][1] for g in chars[i:j])>2))
        if chars[i]["flags"] & 2 and (re.fullmatch(r"[A-Za-zΑ-ω]{1,2}", text) or function_run or scripted_run):
            for k in range(i, j):
                mask[k] = bool(chars[k]["c"].strip())
        i = j
    prose_indices=set()
    for word in re.finditer(r"[A-Za-z]+",line["text"]):
        a,b=word.span()
        scripted_word = (b-a <= 4 and all(chars[k]['flags']&2 for k in range(a,b))
                         and (min(chars[k]['size'] for k in range(a,b)) < max(chars[k]['size'] for k in range(a,b))*.85
                              or max(chars[k]['origin'][1] for k in range(a,b))-min(chars[k]['origin'][1] for k in range(a,b)) > 2))
        prose_connector = (word[0].lower() in {'and','or','if','then','for','because','when','where','otherwise'}
                           and all(re.search(r'mathjax_main', chars[k]['font'], re.I) for k in range(a,b))
                           and not scripted_word)
        if prose_connector:
            for k in range(a,b):
                mask[k] = False
            prose_indices.update(range(a,b))
            continue
        if scripted_word:
            for k in range(a,b):
                mask[k] = True
        # Bounding boxes are only hints. Every ordinary Latin word is vetoed
        # as a whole; an italic variable or actual math-font run is separate
        # positive evidence. This also forbids cutting italic emphasis words.
        trusted=all(_is_math(chars[i]) for i in range(a,b)) or (b-a<=2 and all(chars[i]["flags"]&2 for i in range(a,b))) or scripted_word
        if not trusted:prose_indices.update(range(a,b))
    for word in re.finditer(r"[A-Za-z]+",line["text"]):
        if word[0] not in {"sin","cos","tan","cot","sec","csc","log","ln","lim","gcd","lcm","max","min","det","mod","exp","Pr","Prob"}:
            continue
        right=word.end()
        while right<len(chars) and chars[right]["c"].isspace():right+=1
        native_operand=right<len(chars) and mask[right]
        if right<len(chars) and chars[right]["c"]=="(":
            depth=0
            for end in range(right,min(len(chars),right+160)):
                depth+=(chars[end]["c"]=="(")-(chars[end]["c"]==")")
                if depth==0:
                    argument=chars[right+1:end]
                    native_operand=bool(argument) and (any(mask[k] for k in range(right+1,end))
                        or all(g["c"].isdigit() or g["c"] in ' ,.+-/' for g in argument))
                    break
        trusted_font=all(MATH_FONT.search(chars[k]["font"]) for k in range(word.start(),word.end()))
        if trusted_font or native_operand:
            for k in range(word.start(),word.end()):mask[k]=True;prose_indices.discard(k)
    # Upright alphanumeric constants are actual operands in x = CS1. Do not
    # terminate protection at '=' or promote arbitrary surrounding English.
    for identifier in re.finditer(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]*\d[A-Z0-9_]*(?![A-Za-z0-9_])",line["text"]):
        left=identifier.start()-1
        while left>=0 and chars[left]["c"].isspace():left-=1
        if left>=0 and mask[left] and chars[left]["c"] in '=<>≤≥≠':
            for k in range(identifier.start(),identifier.end()):mask[k]=True;prose_indices.discard(k)
    # A PDF line wrap can leave a complete upright truth constant without its
    # preceding arrow on this line. The original letter is still immutable.
    # Typed null/nil constants require an adjacent relation and italic/code
    # glyph evidence; an ordinary English use of "null" is never promoted.
    for word in re.finditer(r"\b(?:T|F|null|nil|NULL|NIL|true|false)\b", line["text"]):
        a,b=word.span()
        truth=word[0] in {'T','F'}
        left=a-1
        while left>=0 and chars[left]['c'].isspace():left-=1
        typed=all(g['flags']&2 or CODE_FONT.search(g['font']) for g in chars[a:b])
        relation=left>=0 and mask[left] and chars[left]['c'] in '=≠→↔'
        if truth or typed and relation:
            for k in range(a,b):mask[k]=True;prose_indices.discard(k)
    # Native interval endpoints can use the ordinary upright font. A complete
    # same-baseline pair (including mixed open/closed endpoints) establishes
    # the unit; character-count balancing alone cannot recognize [i, infinity).
    endpoint=r'[+−-]?(?:∞|\d+(?:\.\d+)?|[A-Za-zΑ-ω](?:\d+)?)'
    for interval in re.finditer(r'[\[(]\s*'+endpoint+r'\s*,\s*'+endpoint+r'\s*[\])]',line['text']):
        a,b=interval.span();native=chars[a:b]
        baseline=statistics.median(g['origin'][1] for g in native)
        size=statistics.median(g['size'] for g in native)
        if a<prefix_end or any(abs(g['origin'][1]-baseline)>size*.2 for g in native):continue
        if any(((chars[k]['origin'][0]-chars[k-1]['origin'][0])**2
                +(chars[k]['origin'][1]-chars[k-1]['origin'][1])**2)**.5
               >max(chars[k]['size'],chars[k-1]['size'])*1.2 for k in range(a+1,b)):continue
        for k in range(a,b):mask[k]=True;prose_indices.discard(k)
    native_mask=list(mask)
    for h in hints:
        r = fitz.Rect(h["bbox"])
        if not any(native_mask[k] and (r+(-chars[k]["size"]*.4,-chars[k]["size"]*.4,chars[k]["size"]*.4,chars[k]["size"]*.4)).contains((fitz.Rect(chars[k]["bbox"]).tl+fitz.Rect(chars[k]["bbox"]).br)*.5) for k in range(len(chars))):continue
        if not r.intersects(line["bbox"]):continue
        for i, g in enumerate(chars):
            b = fitz.Rect(g["bbox"])
            # Upstream's line/font heuristics can put a formula rectangle over
            # part of an ordinary word. Never tokenize syllables of prose.
            center=(b.tl+b.br)*.5
            tolerance=g["size"]*.35
            nearby=(r+(-tolerance,-tolerance,tolerance,tolerance)).contains(center)
            exact=any(c.get("text")==g["c"] and (fitz.Rect(c["bbox"])+(-tolerance,-tolerance,tolerance,tolerance)).contains(center) for c in h.get("characters",[])) if "characters" in h else r.contains(center)
            bracket_evidence=g["c"] not in "[]" or any(native_mask[max(0,i-3):min(len(chars),i+4)])
            if i not in prose_indices and nearby and exact and bracket_evidence:
                mask[i] = True
    for i in range(min(prefix_end,len(mask))):mask[i]=False
    for roman in re.finditer(r"\([ivxlcdm]{1,8}\)",line["text"]):
        if any(chars[k]["flags"]&2 for k in range(roman.start()+1,roman.end()-1)):
            for k in range(roman.start(),roman.end()):mask[k]=True
    for word in re.finditer(r"[A-Za-zΑ-ω_]+",line["text"]):
        a,b=word.span()
        upright_constant=line["text"][a:b] in {"T","F"}
        if not upright_constant and not all(chars[k]["flags"]&2 for k in range(a,b)):
            continue
        left=a-1
        while left>=0 and chars[left]["c"].isspace():left-=1
        right=b
        while right<len(chars) and chars[right]["c"].isspace():right+=1
        compact_call=not upright_constant and right==b and right<len(chars) and chars[right]["c"]=="("
        if (b-a<=2 and ((left>=0 and mask[left]) or (right<len(chars) and mask[right]))) or compact_call:
            for k in range(a,b):
                if k>=prefix_end:mask[k]=True
    # Function arguments often use the ordinary text font (P(orange),
    # enrolled(kevin,math273)). Protect the complete balanced call whenever the
    # preceding identifier is mathematical, rather than stopping at P(.
    forbidden=set()
    # Some upright MathJax diagram labels use a math font even for an English
    # apostrophe. Keep it with its native word only when *all* surrounding
    # letters already have ordinary-prose evidence. Primes on variables and
    # widely separated source glyphs do not meet this word-level proof.
    for word in re.finditer(r"[A-Za-z]+(?:['’][A-Za-z]+)+",line['text']):
        positions=[k for k in range(*word.span()) if chars[k]['c'].isalpha()]
        if len(positions)<3 or any(k not in prose_indices or mask[k] for k in positions):continue
        contiguous=all(((chars[k]['origin'][0]-chars[k-1]['origin'][0])**2
                        +(chars[k]['origin'][1]-chars[k-1]['origin'][1])**2)**.5
                       <=max(chars[k]['size'],chars[k-1]['size'])*1.2
                       for k in range(word.start()+1,word.end()))
        if contiguous:
            forbidden.update(k for k in range(*word.span()) if chars[k]['c'] in "'’")
    # Compact answer pages place several upright bold option markers on one
    # physical line. Their closing parentheses are prose list punctuation,
    # even when immediately followed by a mathematical expression. Italic
    # variables and balanced mathematical tuples do not meet this evidence.
    for marker in re.finditer(r"(?<![A-Za-z0-9_(])([a-z])\)",line["text"]):
        a,b=marker.span()
        if chars[a]["flags"]&16 and not chars[a]["flags"]&2:
            forbidden.update(range(a,b))
    for i,g in enumerate(chars):
        if g["c"] in "([{" and i and chars[i-1]["c"].isspace():
            tail="".join(c["c"] for c in chars[i+1:]).lstrip()
            word=re.match(r"[A-Za-z]{3,}\b",tail)
            if word is None:
                # Short English clause openings need a following native word.
                # A lone upright i/B inside an interval or matrix expression
                # is not prose, even if its font supplies no math seed.
                word=re.match(r"(?:a|an|it|is|by|as|we|he|to|of|so|do|in|on|if)\b(?=\s+[A-Za-z]{2,}\b)",tail,re.I)
            start=i+1
            while start<len(chars) and chars[start]['c'].isspace():start+=1
            native_word=word and all(mask[k] for k in range(start,min(len(chars),start+len(word[0]))))
            if word and not native_word and not re.match(r"[ivxlcdm]+\)",tail):forbidden.add(i)
        if g["c"] not in "([{":
            continue
        previous=i-1
        while previous>=0 and chars[previous]["c"].isspace():previous-=1
        if i in forbidden or previous<0 or not mask[previous]:
            continue
        opening,closing=g["c"],{"(":")","[":"]","{":"}"}[g["c"]]
        depth=0
        for j in range(i,min(len(chars),i+160)):
            if chars[j]["c"]==opening:depth+=1
            if chars[j]["c"]==closing:depth-=1
            if depth==0:
                for k in range(previous,j+1):mask[k]=True
                break
    for i,g in enumerate(chars):
        if i in forbidden:mask[i]=False
        if g["c"]=="." and not(i and i+1<len(chars) and chars[i-1]["c"].isdigit() and chars[i+1]["c"].isdigit()):
            mask[i]=False
    # Expand from math seeds over numerals, brackets and punctuation, stopping
    # at natural-language words. This protects P(x), x^2, fractions and arrays.
    changed = True
    while changed:
        changed = False
        for i, g in enumerate(chars):
            decimal_dot=g["c"]=="." and i and i+1<len(chars) and chars[i-1]["c"].isdigit() and chars[i+1]["c"].isdigit()
            if i<prefix_end or i in forbidden or (g["c"]=="." and not decimal_dot) or mask[i] or not (g["c"] in MATH_NEUTRAL):
                continue
            if ((i and mask[i-1]) or (i+1 < len(chars) and mask[i+1])):
                # A large gap is a tabular column or text-word boundary.
                left_neighbor=bool(i and mask[i-1])
                neighbor = chars[i-1] if left_neighbor else chars[i+1]
                gap=g["bbox"][0]-neighbor["bbox"][2] if left_neighbor else neighbor["bbox"][0]-g["bbox"][2]
                if abs(gap) < max(g["size"]*.7, 4):
                    mask[i] = True
                    changed = True
    # Prose parentheses surrounding an inline formula stay prose punctuation.
    # Keep unmatched closers only when their opener is on a previous source
    # line, where vector-segment joining will restore the complete expression.
    stack=[]
    pairs={")":"(","]":"[","}":"{"}
    for i,g in enumerate(chars):
        if g["c"] in "([{":stack.append((g["c"],i))
        elif g["c"] in pairs and stack and stack[-1][0]==pairs[g["c"]]:
            _,opening=stack.pop()
            language=any(chars[k]["c"].isalpha() and not mask[k] for k in range(opening+1,i))
            if language:mask[opening]=False;mask[i]=False
            elif mask[i] and not mask[opening]:mask[i]=False
    # Clause punctuation belongs to prose. Keep tuple/matrix punctuation inside
    # its actual delimiters, but expose a following subject or numbered step.
    def subject_has_predicate(start):
        cursor=start
        for _ in range(8):
            while cursor<len(chars) and chars[cursor]["c"].isspace():cursor+=1
            # Native protection identifies the subject, including its scripts
            # or balanced function arguments; ordinary prose is not a subject.
            if cursor>=len(chars) or not mask[cursor]:return False
            beginning=cursor
            while cursor<len(chars) and mask[cursor]:cursor+=1
            # A list of complete relations remains an atomic math list. The
            # new clause case concerns indexed terms as grammatical subjects.
            if any(item["c"] in "=<>≤≥≠≈→↔∈∉" for item in chars[beginning:cursor]):return False
            while cursor<len(chars) and chars[cursor]["c"].isspace():cursor+=1
            word=re.match(r"[A-Za-z]+", "".join(item["c"] for item in chars[cursor:]))
            if not word:return False
            if word[0].lower() in {"is","are","was","were","has","have","denotes","represents"}:return True
            if word[0].lower() not in {"and","or"}:return False
            cursor+=len(word[0])
        return False

    depth=0
    for i,g in enumerate(chars):
        c=g["c"]
        if c in "([{":depth+=1
        elif c in ")]}":depth=max(0,depth-1)
        elif c in {",",";"} and depth==0:
            tail="".join(item["c"] for item in chars[i+1:])
            left=i-1
            while left>=0 and mask[left]:left-=1
            prior="".join(item["c"] for item in chars[left+1:i])
            single=bool(re.match(r"\s*[A-Za-zΑ-ω]\s+(?:is|are|was|were|has|have|denotes|represents)\b",tail))
            # Preserve an ordinary comma-separated mathematical list. The new
            # coordinated-subject case requires a preceding mathematical
            # relation/range, which is the clause boundary being repaired.
            subject=single or (any(c in "=<>≤≥≠≈→↔∈∉" for c in prior) and subject_has_predicate(i+1))
            step=bool(c==";" and re.match(r"\s*\(\d{1,3}\)\s*(?=[A-Za-zΑ-ω(±−+√0-9])",tail))
            if subject or step:mask[i]=False
    for index in forbidden:mask[index]=False
    # Trim sentence punctuation/whitespace from the edge. Parentheses stay if
    # balanced inside; unmatched edge cases are surfaced by the caller.
    spans = []
    i = 0
    while i < len(chars):
        if not mask[i]:
            i += 1
            continue
        j = i+1
        while j < len(chars) and mask[j]:
            j += 1
        a, b = i, j
        while a < b and chars[a]["c"] in " ,.;:": a += 1
        while b > a and chars[b-1]["c"] in " ,.;:": b -= 1
        if a < b:
            spans.append((a, b))
        i = j
    return spans


def _asset(book, doc, page, outdir, kind, bbox, text="", baseline=None, metadata=None):
    bbox = fitz.Rect(bbox) & page.rect
    selected_source=set((metadata or {}).get('glyph_ids',[]))
    if selected_source:
        # The final visible box, as well as the internal cleanup region, must
        # contain each selected native glyph's measured ink. Keeping a text
        # operator alone does not prevent a tall bracket being visually cut.
        ink=[g['bbox'] for g in getattr(book,'_active_glyphs',[]) if g['id'] in selected_source]
        if ink:bbox=(bbox|_rect_union(ink))&page.rect
    if bbox.is_empty or bbox.width <= .05 or bbox.height <= .05:
        raise ValueError(f"Empty {kind} crop on page {page.number+1}: {bbox}")
    identity=[book.source_sha256,_rounded(bbox),text]
    if metadata and metadata.get('exclude_glyph_ids'):
        identity.append({'excluded_glyph_ids':sorted(metadata['exclude_glyph_ids'])})
    aid = f"p{page.number+1:04d}-{kind}-{digest(identity)[:14]}"
    if aid in book.assets:
        return book.assets[aid]
    # One PDF bundle per source page shares the original font/form resources
    # across all formula crops. Separate files would duplicate the full source
    # page tens of thousands of times for a large textbook.
    relative = f"assets/{getattr(book,'_asset_prefix','')}p{page.number+1:04d}-vectors.pdf"
    if not hasattr(book,"_asset_bundles"):
        book._asset_bundles={}
    if page.number+1 not in book._asset_bundles:
        book._asset_bundles[page.number+1]=fitz.open()
    bundle=book._asset_bundles[page.number+1]
    pdf_page=len(bundle)
    target=bundle.new_page(width=bbox.width,height=bbox.height)
    isolated=fitz.open()
    isolated.insert_pdf(doc,from_page=page.number,to_page=page.number)
    ip=isolated[0]
    isolation_issues=[]
    selected_ids=set((metadata or {}).get("glyph_ids",[]))
    excluded_ids=set((metadata or {}).get("exclude_glyph_ids",[]))
    if excluded_ids:
        # Decorations can contain linguistic labels. Remove those actual text
        # objects in the private copy, preserving images and vector backgrounds.
        for glyph in getattr(book,"_active_glyphs",[]):
            if glyph["id"] in excluded_ids:
                ip.add_redact_annot(fitz.Rect(glyph["bbox"]),fill=False,cross_out=False)
        ip.apply_redactions(images=0,graphics=0,text=0)
    if selected_ids:
        glyphs=getattr(book,"_active_glyphs",[])
        # MuPDF redaction uses nominal text boxes. Protect both the true ink
        # and that nominal box, so removing neighbouring prose cannot delete
        # a tall glyph whose visible ink lies below its nominal ascender box.
        selected_rects=[fitz.Rect(g['bbox']) | fitz.Rect(g.get('nominal_bbox',g['bbox']))
                        for g in glyphs if g['id'] in selected_ids]
        # Rotated/anisotropic text has different rawdict and redaction metrics.
        # Protect the native trace envelopes as well, without substituting a
        # Unicode glyph or changing the original text transform.
        selected_keys={(g['c'],g['font'].split('+')[-1][:31],round(g['origin'][0],3),round(g['origin'][1],3))
                       for g in glyphs if g['id'] in selected_ids and g['c'].strip()}
        trace_rects=[fitz.Rect(rect) for span in page.get_texttrace() for code,_,origin,rect in span['chars']
                     if (chr(code),span['font'].split('+')[-1][:31],round(origin[0],3),round(origin[1],3)) in selected_keys]
        selected_rects.extend(trace_rects)
        cleanup_bbox=(bbox|_rect_union(selected_rects))+(-.2,-.2,.2,.2)
        cleanup_bbox &= page.rect
        foreign=[g for g in glyphs if g["id"] not in selected_ids and g["c"].strip()
                 and cleanup_bbox.intersects(g["bbox"])]
        if foreign:
            for glyph in foreign:
                gr=fitz.Rect(glyph["bbox"])
                # Subtract all protected nominal boxes from the unrelated
                # glyph's box and choose its largest remaining rectangle.
                # MuPDF intentionally ignores very small overlap slivers.
                free=[gr]
                for sr in selected_rects:
                    cut=sr+(-.1,-.1,.1,.1)
                    pieces=[]
                    for region in free:
                        hit=region&cut
                        if hit.is_empty:
                            pieces.append(region);continue
                        candidates=[fitz.Rect(region.x0,region.y0,region.x1,hit.y0),
                                    fitz.Rect(region.x0,hit.y1,region.x1,region.y1),
                                    fitz.Rect(region.x0,hit.y0,hit.x0,hit.y1),
                                    fitz.Rect(hit.x1,hit.y0,region.x1,hit.y1)]
                        pieces.extend(c for c in candidates if not c.is_empty)
                    free=pieces
                safe=max(free,key=lambda r:r.get_area()) if free else None
                if safe is not None and safe.get_area()<gr.get_area()*.08:safe=None
                if safe is None:
                    isolation_issues.append(glyph["id"])
                else:
                    ip.add_redact_annot(safe,fill=False,cross_out=False)
            ip.apply_redactions(images=0,graphics=0,text=0)
    # A cropbox alone hides, but does not remove, the entire source page's text
    # inside its Form XObject. Permanently discard outside content in a private
    # page copy, so copy/search and downstream PDF import cannot expose it.
    from .native_clipping import clip_native_content,verify_selected_native_glyphs
    cleanup=cleanup_bbox if selected_ids else bbox
    zero_advance=False
    for g in getattr(book,'_active_glyphs',[]):
        nominal=fitz.Rect(g.get('nominal_bbox',g['bbox']))
        if not g['c'].strip() or not(abs(nominal.width)<1e-8 or abs(nominal.height)<1e-8):continue
        # Closed interval contact on the zero dimension, positive overlap on
        # the other dimension; text can be rotated by 90 or 270 degrees.
        horizontal=(cleanup.x0<=nominal.x0<=cleanup.x1 if abs(nominal.width)<1e-8
                    else min(nominal.x1,cleanup.x1)>max(nominal.x0,cleanup.x0))
        vertical=(cleanup.y0<=nominal.y0<=cleanup.y1 if abs(nominal.height)<1e-8
                  else min(nominal.y1,cleanup.y1)>max(nominal.y0,cleanup.y0))
        if horizontal and vertical:zero_advance=True;break
    clipping_proof=clip_native_content(ip,cleanup,preserve_zero_advance=zero_advance)
    selected_native_proof=(verify_selected_native_glyphs(page,ip,
        [g for g in glyphs if g['id'] in selected_ids]) if selected_ids else {})
    selected_glyphs_lost=selected_native_proof.get('missing_ids',[])
    if selected_ids and (isolation_issues or selected_glyphs_lost):
        # Redaction can conflate overlapping nominal font boxes. Restart from
        # the original private copy and retain the uniquely identified original
        # operators instead; never try to repair an already deleted character.
        exact=fitz.open();exact.insert_pdf(doc,from_page=page.number,to_page=page.number)
        ep=exact[0]
        proof=clip_native_content(ep,cleanup,selected_glyphs=[g for g in glyphs if g['id'] in selected_ids],source_glyphs=glyphs)
        exact_identity=verify_selected_native_glyphs(page,ep,[g for g in glyphs if g['id'] in selected_ids])
        lost=exact_identity['missing_ids']
        if proof.get('native_text_selection',{}).get('verified') and not lost:
            isolated.close();isolated=exact;ip=ep
            clipping_proof=proof;selected_glyphs_lost=[];isolation_issues=[]
            selected_native_proof=exact_identity
        else:exact.close()
    if selected_ids:
        if selected_glyphs_lost:
            _issue(book,'formula_crop_selected_glyph_lost',
                   f'{len(selected_glyphs_lost)} protected native glyphs are missing from isolated asset {aid}',
                   page.number+1,severity='error')
            book.issues[-1]['asset_id']=aid
    target.show_pdf_page(target.rect,isolated,0,clip=bbox)
    isolated.close()
    if isolation_issues:
        _issue(book,"formula_crop_contamination",f"Cannot safely isolate {len(isolation_issues)} overlapping unrelated glyphs in asset {aid}",page.number+1,severity="error")
    metric = float(baseline-bbox.y0) if baseline is not None else bbox.height*.8
    asset = Asset(aid,kind,relative,page.number+1,_rounded(bbox),bbox.width,bbox.height,
                  metric,text,"",
                  {**(metadata or {}),"format":"pdf","vector":True,"baseline_from_top":metric,
                   "source_sha256":book.source_sha256,"pdf_page":pdf_page,
                   "isolated_source_content":True,"isolation_issues":isolation_issues,
                   "selected_native_glyphs_verified":bool(selected_ids) and not selected_glyphs_lost,
                   "selected_glyphs_lost":selected_glyphs_lost,
                   "selected_native_identity":selected_native_proof,
                   "native_content_clipping":clipping_proof,
                   "native_cleanup_bbox":_rounded(cleanup_bbox if selected_ids else bbox)})
    book.assets[aid] = asset
    return asset


def _finalize_assets(book,number,outdir):
    bundle=getattr(book,"_asset_bundles",{}).pop(number,None)
    if bundle is None:
        return
    if not len(bundle):bundle.close();return
    relative=f"assets/{getattr(book,'_asset_prefix','')}p{number:04d}-vectors.pdf"
    destination=Path(outdir)/relative
    temporary=destination.with_suffix(".pdf.tmp")
    try:
        bundle.set_metadata({"title":f"Source page {number} immutable vector crops", "producer":"Textbook PDF"})
        bundle.save(temporary,garbage=4,deflate=True)
    finally:
        bundle.close()
    temporary.replace(destination)
    fingerprint=file_sha256(destination)
    for asset in book.assets.values():
        if asset.page==number and asset.path==relative:
            asset.sha256=fingerprint


def _combine_wrapped_math(book,elements,number,outdir=None,doc=None,page=None):
    """Assemble disjoint complete token chains, never stale overlapping pairs.

    Source crops retain two-dimensional positions when a native page is
    available. The concatenated fallback is only for existing isolated segments
    and explicitly records its geometry; no glyph is resynthesized or scaled.
    """
    from .models import TOKEN_RE
    bundle=getattr(book,"_asset_bundles",{}).get(number)
    if bundle is None:
        if outdir is None:return
        if not hasattr(book,"_asset_bundles"):book._asset_bundles={}
        bundle=fitz.open();book._asset_bundles[number]=bundle
    relative=f"assets/{getattr(book,'_asset_prefix','')}p{number:04d}-vectors.pdf"
    balance=lambda t:(t.count("(")-t.count(")"),t.count("[")-t.count("]"))
    operator=lambda t:bool(re.search(r"[=±+−×÷∧∨→↔√]\s*$",t))
    for element in elements:
        original_text=element.text
        matches=list(TOKEN_RE.finditer(original_text))
        changes=[];i=0
        while i<len(matches):
            start=i
            if matches[i][1] not in book.assets:i+=1;continue
            chain=[book.assets[matches[i][1]]]
            joined=chain[0].text
            while i+1<len(matches):
                first,second=matches[i],matches[i+1]
                if original_text[first.end():second.start()].strip() or second[1] not in book.assets:break
                following=book.assets[second[1]]
                if following.kind!="formula" or any(a.id==following.id for a in chain):break
                if not (any(v>0 for v in balance(joined)) or operator(joined)):break
                # Bare radicals are two-dimensional source components, not a
                # sequence of infix operators. A chain cannot invent radicands.
                if re.fullmatch(r"[√\s]+",joined) and re.fullmatch(r"[√\s]+",following.text):break
                chain.append(following);joined+=following.text;i+=1
            i+=1
            if len(chain)<2:continue
            component_ids={a.id for a in chain}
            glyph_ids=list(dict.fromkeys(g for a in chain for g in a.metadata.get("glyph_ids",[])))
            source_segments=[{"asset_id":a.id,"bbox":a.bbox,"text":a.text} for a in chain]
            if doc is not None and page is not None and glyph_ids:
                bbox=_rect_union(a.bbox for a in chain)
                baseline=min(a.bbox[1]+a.baseline for a in chain)
                combined=_asset(book,doc,page,outdir,"formula",bbox,joined,baseline,
                    {"detector":"whole-native-formula-chain","source_segments":source_segments,
                     "glyph_ids":glyph_ids,"native_glyph_geometry_preserved":True,
                     "source_component_ids":[a.id for a in chain]})
            else:
                copied=fitz.open()
                for original in chain:
                    if original.path==relative:
                        copied.insert_pdf(bundle,from_page=original.metadata["pdf_page"],to_page=original.metadata["pdf_page"])
                    elif outdir is not None:
                        with fitz.open(Path(outdir)/original.path) as stored:
                            copied.insert_pdf(stored,from_page=original.metadata["pdf_page"],to_page=original.metadata["pdf_page"])
                    else:raise ValueError("Existing formula bundle requires its job directory")
                baseline=max(a.baseline for a in chain)
                descent=max(a.height-a.baseline for a in chain)
                width=sum(a.width for a in chain)+len(chain)-1
                height=baseline+descent;index=len(bundle)
                target=bundle.new_page(width=width,height=height);x=0
                for n,a in enumerate(chain):
                    target.show_pdf_page(fitz.Rect(x,baseline-a.baseline,x+a.width,baseline-a.baseline+a.height),copied,n)
                    x+=a.width+1
                copied.close()
                aid=f"p{number:04d}-formula-{digest([[a.id for a in chain],'joined-vector-chain-v2'])[:14]}"
                combined=Asset(aid,"formula",relative,number,_rounded(_rect_union(a.bbox for a in chain)),width,height,baseline,joined,"",
                    {"format":"pdf","vector":True,"pdf_page":index,"baseline_from_top":baseline,
                     "source_sha256":book.source_sha256,"detector":"joined-original-vector-chain",
                     "source_segments":source_segments,"glyph_ids":glyph_ids,
                     "source_component_ids":[a.id for a in chain]})
                book.assets[aid]=combined
            changes.append((matches[start].start(),matches[i-1].end(),combined.id,component_ids))
            for g in getattr(book,"_active_glyphs",[]):
                if g.get("asset_id") in component_ids:g["asset_id"]=combined.id
            for segment in chain:
                segment.metadata.setdefault("combined_into",[]).append(combined.id)
                segment.metadata["exclusion_reason"]="Original vector component retained; assembled use is recorded by combined_into"
        # Text and the declared asset ledger change in the same transaction.
        # Every replacement consumes a disjoint original token interval.
        for first,last,aid,component_ids in reversed(changes):
            element.text=element.text[:first]+f"⟦F:{aid}⟧"+element.text[last:]
        if changes:
            refs=TOKEN_RE.findall(element.text)
            old_ids={aid for _,_,_,ids in changes for aid in ids}
            element.asset_ids=list(dict.fromkeys([aid for aid in element.asset_ids if aid not in old_ids or aid in refs]+refs))
        formulae=[book.assets[x] for x in element.asset_ids if x in book.assets and book.assets[x].kind=="formula"]
        if formulae and all(balance(a.text)==(0,0) for a in formulae):
            book.issues[:]=[issue for issue in book.issues if not(issue["code"]=="formula_boundary_review" and issue.get("element_id")==element.id)]


def _merge_wrapped_elements(book,elements):
    """A PDF block boundary is not a semantic boundary inside a formula."""
    from .models import TOKEN_RE
    result=[]
    for element in elements:
        previous=result[-1] if result else None
        merge=False
        if (previous and previous.kind in {"paragraph","exercise","caption","sidebar","formula"}
            and element.kind in {"paragraph","exercise","formula","sidebar"}
            and previous.style.get("column")==element.style.get("column")
            and not element.metadata.get("list_label")
            and element.bbox[1]-previous.bbox[3]<max(previous.style.get("source_size",11),element.style.get("source_size",11))*1.8):
            a=list(TOKEN_RE.finditer(previous.text));b=TOKEN_RE.match(element.text)
            if a and b and not previous.text[a[-1].end():].strip():
                first=book.assets[a[-1][1]].text
                second=book.assets[b[1]].text
                opening=first.count("(")-first.count(")")+first.count("[")-first.count("]")
                closing=second.count("(")-second.count(")")+second.count("[")-second.count("]")
                continuation=bool(re.search(r"[=±+−×÷∧∨→↔√]\s*$",first))
                merge=(opening>0 and closing<0) or continuation
        if merge:
            previous.text+=" "+element.text
            previous.asset_ids.extend(element.asset_ids)
            previous.bbox=_rounded(_rect_union([previous.bbox,element.bbox]))
            previous.metadata["source_text"]+="\n"+element.metadata.get("source_text","")
            previous.metadata["glyph_ids"].extend(element.metadata.get("glyph_ids",[]))
            for g in getattr(book,"_active_glyphs",[]):
                if g.get("owner")==element.id:g["owner"]=previous.id
            for issue in book.issues:
                if issue.get("element_id")==element.id:issue["element_id"]=previous.id
        else:result.append(element)
    elements[:]=result


def _exercise_marker_anchor(marker,elements):
    """An inter-column marker belongs to the nearest question, not a gutter guess."""
    candidates=[e for e in elements if e.page==marker.page and e.kind=="exercise" and e.bbox]
    if not candidates:return None
    mr=fitz.Rect(marker.bbox)
    overlap=lambda e:min(mr.y1,e.bbox[3])-max(mr.y0,e.bbox[1])
    xgap=lambda e:max(e.bbox[0]-mr.x1,mr.x0-e.bbox[2],0)
    vertical=lambda e:max(e.bbox[1]-mr.y1,mr.y0-e.bbox[3],0)
    overlapping=[e for e in candidates if overlap(e)>0]
    if overlapping:
        return min(overlapping,key=lambda e:(xgap(e),abs(e.bbox[1]-mr.y0),e.id))
    close=[e for e in candidates if vertical(e)<=max(mr.height,e.style.get("source_size",10))*2]
    return min(close,key=lambda e:(vertical(e),xgap(e),abs(e.bbox[1]-mr.y0),e.id)) if close else None


def reanchor_existing(book: Book) -> Book:
    """Pure metadata repair for already parsed books; never reads/writes assets.

    Text, element/asset IDs, source hashes and protected vector bytes remain
    identical. The input book is not mutated. This allows existing accepted
    translations and asset bundles to be reused after an association repair.
    """
    replacements={}
    for marker in book.elements:
        if marker.metadata.get("role")!="exercise_marker":continue
        anchor=_exercise_marker_anchor(marker,book.elements)
        if anchor is None:continue
        replacements[marker.id]=replace(marker,group_id=anchor.group_id,
            style={**marker.style,"column":anchor.style.get("column",0),"columns":anchor.style.get("columns",2)},
            metadata={**marker.metadata,"anchor_id":anchor.id,"anchor_method":"nearest-horizontal-question-with-y-overlap"})
    elements=[replacements.get(e.id,e) for e in book.elements]
    for marker in replacements.values():
        elements=[e for e in elements if e.id!=marker.id]
        index=next((i for i,e in enumerate(elements) if e.id==marker.metadata["anchor_id"]),len(elements))
        elements.insert(index,marker)
    pages=[{**p,"element_ids":[e.id for e in elements if e.page==p["number"]]} for p in book.pages]
    return replace(book,elements=elements,pages=pages,
                   profile={**book.profile,"source_relation_version":"exercise-markers-v2"})


def _merge_baseline_elements(book,elements,glyphs):
    if _frontmatter_kind(elements):return
    by_id={g["id"]:g for g in glyphs}
    def baseline(element):
        gs=[by_id[g] for g in element.metadata.get("glyph_ids",[]) if g in by_id and "extensions" not in by_id[g]["font"].lower()]
        return statistics.median(g["origin"][1] for g in gs) if gs else None
    result=[]
    for element in elements:
        previous=result[-1] if result else None
        first=baseline(previous) if previous else None;second=baseline(element)
        compatible=(previous is not None and previous.kind in {"paragraph","formula"} and element.kind in {"paragraph","formula"}
                    and not previous.metadata.get("role") and not element.metadata.get("role")
                    and first is not None and second is not None and abs(first-second)<.65
                    and previous.metadata.get("source_block")==element.metadata.get("source_block")
                    and -.8<=element.bbox[0]-previous.bbox[2]<max(40,element.style.get("source_size",10)*4))
        if not compatible:result.append(element);continue
        previous.kind="paragraph" if "paragraph" in {previous.kind,element.kind} else "formula"
        previous.text+=" "+element.text
        previous.asset_ids.extend(a for a in element.asset_ids if a not in previous.asset_ids)
        previous.bbox=_rounded(_rect_union([previous.bbox,element.bbox]))
        previous.metadata["source_text"]+=" "+element.metadata.get("source_text","")
        previous.metadata.setdefault("glyph_ids",[]).extend(element.metadata.get("glyph_ids",[]))
        previous.metadata.setdefault("merged_source_ids",[]).append(element.id)
        for glyph in glyphs:
            if glyph.get("owner")==element.id:glyph["owner"]=previous.id
        for issue in book.issues:
            if issue.get("element_id")==element.id:issue["element_id"]=previous.id
    elements[:]=result


def _order_exercise_markers(elements):
    for marker in list(elements):
        if marker.metadata.get("role")!="exercise_marker":continue
        anchor=_exercise_marker_anchor(marker,elements)
        if anchor is None:continue
        marker.group_id=anchor.group_id
        marker.metadata.update(anchor_id=anchor.id,anchor_method="nearest-horizontal-question-with-y-overlap")
        marker.style.update(column=anchor.style.get("column",0),columns=anchor.style.get("columns",2))
        elements.remove(marker);elements.insert(elements.index(anchor),marker)


def _repair_prose_tokens(book,elements,glyphs):
    for element in elements:
        removed=[]
        for aid in list(element.asset_ids):
            asset=book.assets[aid]
            prose=bool(re.search(r"[©Ⓒ⃝]",asset.text) or re.match(r"^[A-Za-z]{3,}\s+\((?:or|also)\b",asset.text))
            if asset.kind!="formula" or not prose:continue
            token=f"⟦F:{aid}⟧"
            if token not in element.text:continue
            element.text=element.text.replace(token,_normal_text(asset.text))
            element.asset_ids.remove(aid);removed.append(aid)
            asset.metadata.update(exclusion_reason="copyright credit or emphasized prose is translated as text",superseded_by=element.id)
            for glyph in glyphs:
                if glyph.get("asset_id")==aid:glyph.pop("asset_id",None)
        if removed:
            element.metadata["deprotected_prose_asset_ids"]=removed
            book.issues=[i for i in book.issues if not (i["code"]=="formula_crop_contamination" and any(a in i["detail"] for a in removed))]


def _merge_body_continuations(book,elements,glyphs,page_width):
    """Margin resource labels do not interrupt the underlying source paragraph."""
    if _frontmatter_kind(elements):return
    glyph_map={g["id"]:g for g in glyphs}
    first_x={}
    first_baseline={};last_baseline={}
    for item in elements:
        size=item.style.get("source_size",10.5)
        gs=[glyph_map[k] for k in item.metadata.get("glyph_ids",[]) if k in glyph_map]
        body=[g for g in gs if size*.9<=g["size"]<=size*1.1 and not MATH_FONT.search(g["font"]) and g["c"].isalpha()]
        gs=body or gs
        if gs:
            baseline=min(g["origin"][1] for g in gs)
            first_x[item.id]=min(g["bbox"][0] for g in gs if g["origin"][1]<=baseline+2)
            first_baseline[item.id]=baseline;last_baseline[item.id]=max(g["origin"][1] for g in gs)
        else:first_x[item.id]=item.bbox[0]
    result=[]
    for element in elements:
        previous=None
        source=element.metadata.get("source_text",element.text).lstrip()
        definition_start="bold" in element.style.get("font","").lower() and ":" in source[:90]
        if element.kind in {"paragraph","sidebar","footnote"} and source and not element.metadata.get("list_label"):
            for candidate in sorted(result[-12:],key=lambda c:abs(first_baseline.get(element.id,element.bbox[1])-last_baseline.get(c.id,c.bbox[3]))):
                size=candidate.style.get("source_size",10.5)
                advance=first_baseline[element.id]-last_baseline[candidate.id] if element.id in first_baseline and candidate.id in last_baseline else None
                flow_ok=(-.75<=advance<=size*1.9) if advance is not None else (-.5<=element.bbox[1]-candidate.bbox[3]<size*.9)
                if (candidate.kind in {"paragraph","footnote"} and candidate.bbox[2]-candidate.bbox[0]>page_width*.35
                    and (candidate.metadata.get("source_block")==element.metadata.get("source_block") or (candidate.metadata.get("structure_id") and candidate.metadata.get("structure_id")==element.metadata.get("structure_id")) or candidate.text.endswith("-") or (abs(first_x[element.id]-candidate.bbox[0])<24 and abs(element.style.get("source_size",size)-size)<.3 and (source[:1].islower() or re.search(r"\b(?:in|at|of|to|and|or|as|the|a|an|with|for|father)\s*$",candidate.text) or (source[:1] in {"“",chr(34)} and abs(first_x[element.id]-candidate.bbox[0])<3 and element.bbox[1]-candidate.bbox[3]<size*.35))))
                    and (not definition_start or candidate.text.endswith("-"))
                    and candidate.style.get("column")==element.style.get("column")
                    and flow_ok
                    and (candidate.bbox[0]-30<=first_x[element.id]<=candidate.bbox[2] or (candidate.metadata.get("source_block")==element.metadata.get("source_block") and source[:1].islower() and candidate.bbox[0]-page_width*.2<=first_x[element.id]<=candidate.bbox[2]))
                    and abs(element.style.get("source_size",size)-size)<size*.2
                    and (element.kind!="sidebar" or candidate.text.endswith("-") or (candidate.metadata.get("structure_id") and candidate.metadata.get("structure_id")==element.metadata.get("structure_id")) or element.style.get("font","").split("-")[0]==candidate.style.get("font","").split("-")[0])
                    and not re.search(r"[.!?。！？;:]\s*$",candidate.text)):
                    previous=candidate;break
        if previous is None:result.append(element);continue
        if previous.text.endswith("-") and element.text[:1].islower():previous.text=previous.text[:-1]+element.text
        else:previous.text+=" "+element.text
        previous.asset_ids.extend(a for a in element.asset_ids if a not in previous.asset_ids)
        previous.bbox=_rounded(_rect_union([previous.bbox,element.bbox]))
        if previous.id in last_baseline and element.id in last_baseline:last_baseline[previous.id]=max(last_baseline[previous.id],last_baseline[element.id])
        previous.metadata["source_text"]+="\n"+element.metadata.get("source_text","")
        previous.metadata.setdefault("glyph_ids",[]).extend(element.metadata.get("glyph_ids",[]))
        previous.metadata.setdefault("merged_source_ids",[]).append(element.id)
        for glyph in glyphs:
            if glyph.get("owner")==element.id:glyph["owner"]=previous.id
        for issue in book.issues:
            if issue.get("element_id")==element.id:issue["element_id"]=previous.id
    elements[:]=result


def _promote_formula_prose(elements):
    """A math-heavy PDF line can still contain words that require translation."""
    for element in elements:
        prose = TOKEN_RE.sub('', element.text)
        if element.kind == 'formula' and re.search(r'[A-Za-z]{3,}', prose):
            element.kind = 'paragraph'
            element.metadata['source_role_repair'] = 'natural-language words remain outside protected formulas'


def _assemble_display_math(book,doc,page,outdir,elements,glyphs):
    """Keep a proven native fraction display as one unchanged source crop.

    Fraction rules plus source glyphs on both sides establish the vertical
    relation. Only standalone math owners enter the crop; prose intersections
    are blocking ambiguities rather than a guessed mathematical reconstruction.
    """
    bars=[]
    for drawing in source_drawings(page):
        if drawing.get('fully_source_clipped'):
            continue
        r=fitz.Rect(drawing["rect"])
        if r.width>2 and r.height<1 and all(item[0]=="l" for item in drawing["items"]):bars.append(r)
    handled=set()
    for bar_index,bar in enumerate(bars):
        if bar_index in handled:continue
        # Connected native regions have already proven and preserved these
        # bars, their scripts and their text owners. Do not reinterpret the
        # old PDF element boxes as another independent fraction display.
        active_assets={aid for e in elements if not e.metadata.get('render_suppressed') for aid in e.asset_ids}
        if any(aid in active_assets and asset.metadata.get('detector')=='native-math-connected-region-v1'
               and not asset.metadata.get('isolation_issues')
               and any(max(abs(x-y) for x,y in zip(box,_rounded(bar))) < .02
                       for box in asset.metadata.get('fraction_bar_bboxes',[]))
               for aid,asset in book.assets.items() if asset.page==page.number+1):
            continue
        formulae=[e for e in elements if e.kind=="formula" and not e.metadata.get("render_suppressed") and e.text.strip() not in {"◂","▸","■","□","◻","◼"}]
        owners={e.id:e for e in formulae}
        near=[g for g in glyphs if g.get("owner") in owners and g["c"].strip()
              and bar.x0-1<=sum(g["bbox"][::2])/2<=bar.x1+1
              and abs(sum(g["bbox"][1::2])/2-bar.y0)<g["size"]*2.2]
        above=[g for g in near if sum(g["bbox"][1::2])/2<bar.y0-.2]
        below=[g for g in near if sum(g["bbox"][1::2])/2>bar.y0+.2]
        if not above or not below:continue
        group_ids={g["owner"] for g in above+below}
        group=[owners[eid] for eid in group_ids]
        box=_rect_union([bar]+[e.bbox for e in group])
        changed=True
        while changed:
            changed=False
            for other in formulae:
                if other.id in group_ids:continue
                r=fitz.Rect(other.bbox);size=other.style.get("source_size",10.5)
                gap=max(0,r.x0-box.x1,box.x0-r.x1)
                overlap=min(r.y1,box.y1)-max(r.y0,box.y0)
                if overlap>0 and gap<size*2:
                    group_ids.add(other.id);group.append(other);box|=r;changed=True
        region_glyphs=[g for g in glyphs if g.get("owner") in group_ids]
        foreign=[g for g in glyphs if g["c"].strip() and g.get("owner") not in group_ids
                 and box.contains((fitz.Rect(g["bbox"]).tl+fitz.Rect(g["bbox"]).br)*.5)]
        keeper=min(group,key=lambda e:(e.bbox[0],e.bbox[1]))
        if foreign:
            _issue(book,"source_display_math_fragmented","Fraction display intersects independent source content; whole native formula extent is unproven",page.number+1,keeper.id,severity="error")
            handled.add(bar_index);continue
        fraction_bars=[]
        for index,r in enumerate(bars):
            if (box+(-.5,-.5,.5,.5)).contains(r):
                fraction_bars.append(_rounded(r));handled.add(index);box|=r
        pieces=[{"element_id":e.id,"bbox":e.bbox,"text":e.metadata.get("source_text",e.text)} for e in sorted(group,key=lambda e:(e.bbox[1],e.bbox[0]))]
        text="\n".join(piece["text"] for piece in pieces)
        if text.count("(")!=text.count(")") or text.count("[")!=text.count("]"):
            _issue(book,"source_display_math_fragmented","Fraction display has an unmatched source delimiter outside the proven native region",page.number+1,keeper.id,severity="error")
            continue
        relations=[g["origin"][1] for g in region_glyphs if g["c"] in "=<>≤≥≠≈↔→"]
        origins=relations or [g["origin"][1] for g in region_glyphs]
        bins=Counter(round(value,2) for value in origins)
        dominant=max(bins,key=bins.get)
        baseline=statistics.median(value for value in origins if abs(value-dominant)<.03)
        glyph_ids=[g["id"] for g in region_glyphs]
        asset=_asset(book,doc,page,outdir,"formula",box+(-.15,-.15,.15,.15),text,baseline,
                     {"detector":"native-fraction-display-region","display_math":True,
                      "native_glyph_geometry_preserved":True,"fraction_bar_bboxes":fraction_bars,
                      "source_pieces":pieces,"glyph_ids":glyph_ids,
                      "source_text_is_positioned_runs":True,"baseline_method":"source-relation-origin" if relations else "dominant-source-origin"})
        old_assets={aid for e in group for aid in e.asset_ids}
        for aid in old_assets:
            if aid==asset.id:continue
            book.assets[aid].metadata.setdefault("combined_into",[]).append(asset.id)
            book.assets[aid].metadata["exclusion_reason"]="Native fraction display preserved in a single original vector region"
        for glyph in region_glyphs:glyph.update(owner=keeper.id,asset_id=asset.id,disposition="preserved-original")
        keeper.text=f"⟦F:{asset.id}⟧";keeper.asset_ids=[asset.id];keeper.bbox=asset.bbox
        keeper.metadata.update(source_text=text,glyph_ids=glyph_ids,display_math=True,
                               fraction_bar_bboxes=fraction_bars,source_pieces=pieces)
        keeper.metadata.setdefault("merged_source_ids",[]).extend(e.id for e in group if e.id!=keeper.id)
        insertion=min(elements.index(e) for e in group)
        elements[:]=[e for e in elements if e.id not in group_ids]
        elements.insert(insertion,keeper)
        book.issues=[i for i in book.issues if not(i.get("element_id") in group_ids and i["code"] in {"formula_boundary_review","formula_atomic_boundary_review"})]
        for issue in book.issues:
            if issue.get("element_id") in group_ids:issue["element_id"]=keeper.id


def _boundary_native_evidence(book,element,asset):
    """Return actual selected source glyphs, never a text-only exemption."""
    ids=set(asset.metadata.get('glyph_ids',[]))
    if not ids or asset.metadata.get('selected_glyphs_lost') or asset.metadata.get('isolation_issues'):
        return None
    active=getattr(book,'_active_glyphs',[])
    active_map={g['id']:g for g in active}
    owners={element.id,*element.metadata.get('merged_source_ids',[])}
    if ids<=active_map.keys() and all(active_map[i].get('owner') in owners for i in ids):
        glyphs=active
    else:
        path=Path(book.source_path)
        if not path.is_file():return None
        key=(str(path.resolve()),book.source_sha256,asset.page)
        cached=getattr(book,'_boundary_native_page',None)
        if cached is None or cached[0]!=key:
            try:
                with fitz.open(path) as doc:
                    if not 1<=asset.page<=len(doc):return None
                    _,glyphs=_collect(doc[asset.page-1])
            except (OSError,ValueError,RuntimeError):return None
            book._boundary_native_page=(key,glyphs)
        else:glyphs=cached[1]
    selected=[g for g in glyphs if g['id'] in ids]
    if len(selected)!=len(ids):return None
    box=fitz.Rect(asset.bbox)+(-.5,-.5,.5,.5)
    if any(not box.contains((fitz.Rect(g['bbox']).tl+fitz.Rect(g['bbox']).br)*.5) for g in selected):
        return None
    if re.sub(r'\s+','',asset.text)!=re.sub(r'\s+','', ''.join(g['c'] for g in selected)):
        return None
    return selected,glyphs


def _native_prose_rhs(book,element,asset):
    """Prove a source definition such as N = number of solutions.

    The right side must exist both in the current translatable element and in
    immediately adjacent, same-baseline ordinary source glyphs. Missing math,
    unrelated prose, another column and an unknown source remain blocking.
    """
    if not re.search(r'=\s*$',asset.text):return None
    matches=[m for m in TOKEN_RE.finditer(element.text) if m[1]==asset.id]
    if len(matches)!=1:return None
    literal=element.text[matches[0].end():].split('⟦',1)[0]
    words=re.findall(r'[A-Za-z]+',literal)
    noun=r'(?:(?:the|a|an)\s+)?(?:(?:total\s+)?(?:number|count|length|size|root|sum|product|value|location|label)\s+of|(?:first|last|next|other|left|right|smallest|largest|least|greatest)\s+(?:vertex|edge|endpoint|child|element|value))\b'
    if len(words)<2 or not re.match(r'\s*\(?\s*'+noun,literal,re.I):return None
    evidence=_boundary_native_evidence(book,element,asset)
    if evidence is None:return None
    selected,glyphs=evidence
    operators=[g for g in selected if g['c']=='=']
    if not operators:return None
    operator=max(operators,key=lambda g:g['bbox'][0])
    baseline=operator['origin'][1];size=operator['size']
    right=sorted((g for g in glyphs if g['c'].strip()
                  and g['bbox'][0]>=operator['bbox'][2]-.1
                  and abs(g['origin'][1]-baseline)<=max(.5,size*.12)),key=lambda g:g['bbox'][0])
    if not right or right[0]['bbox'][0]-operator['bbox'][2]>size*1.5:return None
    # Reconstruct spaces from native x positions, not PDF character order.
    chars=[];previous=operator
    for g in right:
        gap=g['bbox'][0]-previous['bbox'][2]
        if gap>size*1.5:break
        if gap>size*.12:chars.append(' ')
        chars.append(g['c']);previous=g
    native=''.join(chars).lstrip(' (')
    native_noun=re.match(noun,native,re.I)
    if not native_noun:return None
    if native_noun[0].lower().endswith('of') and not native[native_noun.end():].strip(' )'):
        return None
    expected=' '.join(words[:4])
    native_words=re.findall(r'[A-Za-z]+',native)
    if [w.lower() for w in native_words[:len(words[:4])]]!=[w.lower() for w in words[:4]]:return None
    letters=[g for g in right if g['c'].isalpha()][:len(words[0])]
    if not letters or all(_is_math(g) or g['flags']&2 for g in letters):return None
    return {'kind':'native-translatable-prose-rhs','rhs_prefix':' '.join(words[:2]),
            'source_prefix':expected,'source_baseline':baseline,
            'native_glyph_ids':[g['id'] for g in selected],
            'rhs_source_bbox':_rounded(_rect_union(g['bbox'] for g in right[:max(2,len(expected))]))}


def _native_complete_interval(book,element,asset):
    endpoint=r'[+−-]?(?:∞|\d+(?:\.\d+)?|[A-Za-zΑ-ω](?:\d+)?)'
    lhs=r'(?:[A-Za-zΑ-ω][A-Za-zΑ-ω0-9_]*\s*=\s*)?'
    if not re.fullmatch(lhs+r'[\[(]\s*'+endpoint+r'\s*,\s*'+endpoint+r'\s*[\])]',asset.text.strip()):return None
    evidence=_boundary_native_evidence(book,element,asset)
    if evidence is None:return None
    selected,glyphs=evidence
    visible=[g for g in selected if g['c'].strip()]
    if not visible:return None
    opening=next((i for i,g in enumerate(visible) if g['c'] in '(['),None)
    if opening is None:return None
    interval_glyphs=visible[opening:]
    baseline=statistics.median(g['origin'][1] for g in interval_glyphs)
    size=statistics.median(g['size'] for g in interval_glyphs)
    if any(abs(g['origin'][1]-baseline)>size*.2 for g in interval_glyphs):return None
    box=fitz.Rect(asset.bbox)
    if any(g['c'].strip() and g['id'] not in {v['id'] for v in selected}
           and box.contains((fitz.Rect(g['bbox']).tl+fitz.Rect(g['bbox']).br)*.5) for g in glyphs):return None
    return {'kind':'complete-native-interval','source_text':asset.text,
            'native_glyph_ids':[g['id'] for g in visible], 'bbox':asset.bbox}


def _check_atomic_formulas(book,elements):
    element_ids={e.id for e in elements}
    from .native_operators import ISSUE_CODE,check_vertical_operator_limits
    book.issues=[i for i in book.issues if not(i.get('code')==ISSUE_CODE and i.get('element_id') in element_ids)]
    book.issues.extend(check_vertical_operator_limits(book,elements))
    book.issues=[i for i in book.issues if not(i["code"]=="formula_atomic_boundary_review" and i.get("element_id") in element_ids)]
    recorded={(i.get("element_id"),i.get("detail")) for i in book.issues if i["code"]=="formula_atomic_boundary_review"}
    for element in elements:
        for aid in element.asset_ids:
            asset=book.assets[aid]
            if asset.kind!="formula":continue
            text=asset.text.strip()
            interval=_native_complete_interval(book,element,asset)
            if interval:
                resolved=[q for q in book.issues if q['code']=='formula_boundary_review'
                          and q.get('element_id')==element.id and q.get('detail','').endswith(': '+text)]
                for q in resolved:
                    book.profile.setdefault('resolved_source_issues',[]).append({**q,
                        'resolution_reason':'complete native interval has intentional mixed endpoint delimiters',
                        'source_proof':interval,'asset_id':asset.id})
                book.issues=[q for q in book.issues if q not in resolved]
            depth=0;mixed_reference=False
            for i,c in enumerate(text):
                if c in "([{":depth+=1
                elif c in ")]}":depth=max(0,depth-1)
                elif c==";" and depth==0 and re.match(r"\s*\(\d{1,3}\)",text[i+1:]):mixed_reference=True
            if not asset.metadata.get("display_math") and mixed_reference:
                _issue(book,"source_formula_step_reference_mixed","Protected source span mixes a prior step reference with the next numbered proof step",element.page,element.id,severity="error")
            if ("√" in text and text.endswith("√")) or (re.search(r"[A-Za-z0-9]",text) and re.search(r"[=±+−×÷∧∨→↔]\s*$",text)):
                detail="Protected formula ends before a required operand/radicand: "+text
                rhs=_native_prose_rhs(book,element,asset)
                if rhs:
                    record={'code':'formula_atomic_boundary_review','severity':'error','page':element.page,
                            'element_id':element.id,'detail':detail,'asset_id':asset.id,
                            'resolution_reason':'right-hand operand is explicit same-baseline translatable source prose',
                            'source_proof':rhs}
                    saved=book.profile.setdefault('resolved_source_issues',[])
                    if record not in saved:saved.append(record)
                    continue
                if (element.id,detail) not in recorded:
                    _issue(book,"formula_atomic_boundary_review",detail,element.page,element.id,severity="error")
                    recorded.add((element.id,detail))


def _validate_source_references(book,elements,glyphs,number):
    from .models import TOKEN_RE
    codes={"source_asset_reference_missing","source_token_asset_mismatch","source_owner_invalid","source_glyph_asset_missing"}
    book.issues=[i for i in book.issues if not(i.get("page")==number and i["code"] in codes)]
    owners={element.id for element in elements}
    for element in elements:
        referenced=set(element.asset_ids)|set(TOKEN_RE.findall(element.text))
        missing=referenced-book.assets.keys()
        if missing:_issue(book,"source_asset_reference_missing","Missing protected source assets: "+", ".join(sorted(missing)),number,element.id,severity="error")
        tokens=set(TOKEN_RE.findall(element.text))
        formula_assets={aid for aid in element.asset_ids if aid in book.assets and book.assets[aid].kind=="formula"}
        if tokens!=formula_assets:_issue(book,"source_token_asset_mismatch","Protected tokens and element formula asset list differ",number,element.id,severity="error")
    invalid=[g["id"] for g in glyphs if g.get("owner") not in owners|{"whitespace","unresolved",None}]
    if invalid:_issue(book,"source_owner_invalid",f"{len(invalid)} glyph owners point to absent source elements",number,severity="error")
    invalid_assets=[g["id"] for g in glyphs if g.get("asset_id") and g["asset_id"] not in book.assets]
    if invalid_assets:_issue(book,"source_glyph_asset_missing",f"{len(invalid_assets)} glyph assets are absent after source assembly",number,severity="error")


def _assemble_inline_units(elements,glyphs):
    groups={}
    for element in elements:
        group=element.metadata.get("inline_group")
        if group and not element.metadata.get("render_suppressed"):groups.setdefault(group,[]).append(element)
    for members in groups.values():
        if len(members)<2:continue
        keeper=next((e for e in members if e.kind=="paragraph" and not e.metadata.get("drop_cap")),members[0])
        join="" if members[-1].metadata.get("inline_join")=="word" else " "
        keeper.text=join.join(e.text for e in members)
        keeper.metadata["source_text"]=join.join(e.metadata.get("source_text",e.text) for e in members)
        keeper.bbox=_rounded(_rect_union(e.bbox for e in members))
        keeper.asset_ids=list(dict.fromkeys(a for e in members for a in e.asset_ids))
        keeper.metadata["glyph_ids"]=list(dict.fromkeys(g for e in members for g in e.metadata.get("glyph_ids",[])))
        for child in members:
            child.metadata.pop("inline_group",None);child.metadata.pop("inline_join",None)
            if child is keeper:continue
            child.metadata.update(render_suppressed=True,translatable=False,preserve_only=True,superseded_by=keeper.id,
                                  exclusion_reason="Source drop cap or text continuation assembled into its complete sentence",source_glyph_ids=child.metadata.get("glyph_ids",[]),glyph_ids=[])
            keeper.metadata.setdefault("merged_source_ids",[]).append(child.id)
            for glyph in glyphs:
                if glyph.get("owner")==child.id:glyph["owner"]=keeper.id
    entries={}
    for element in elements:
        if element.metadata.get("role")=="chapter_contents" and not element.metadata.get("render_suppressed"):
            entries.setdefault(element.metadata.get("heading_group",element.id),[]).append(element)
        if element.metadata.get("frontmatter_kind")=="contents" and re.fullmatch(r"\d+(?:\.\d+)*",element.text):
            element.metadata.update(translatable=False,preserve_only=True,exclusion_reason="Literal source table-of-contents section number")
    for members in entries.values():
        if len(members)<2:continue
        keeper=members[0]
        keeper.text=" ".join(e.text for e in members)
        keeper.metadata["source_text"]=" ".join(e.metadata.get("source_text",e.text) for e in members)
        keeper.bbox=_rounded(_rect_union(e.bbox for e in members))
        keeper.asset_ids=list(dict.fromkeys(a for e in members for a in e.asset_ids))
        keeper.metadata["glyph_ids"]=[g for e in members for g in e.metadata.get("glyph_ids",[])]
        for child in members[1:]:
            child.metadata.update(render_suppressed=True,translatable=False,preserve_only=True,superseded_by=keeper.id,
                                  exclusion_reason="Source chapter contents line assembled into its complete entry",source_glyph_ids=child.metadata.get("glyph_ids",[]),glyph_ids=[])
            keeper.metadata.setdefault("merged_source_ids",[]).append(child.id)
            for glyph in glyphs:
                if glyph.get("owner")==child.id:glyph["owner"]=keeper.id


def _source_baseline(element,glyph_map):
    size=element.style.get("source_size",10.5)
    gs=[glyph_map[k] for k in element.metadata.get("glyph_ids",[]) if k in glyph_map]
    normal=[g for g in gs if g["size"]>=size*.9 and "extensions" not in g["font"].lower()]
    return statistics.median(g["origin"][1] for g in normal or gs) if gs else element.bbox[3]


def _assemble_statement_rows(book,elements,glyphs):
    """Bind an indexed mathematical label to its same-baseline statement."""
    glyph_map={g["id"]:g for g in glyphs}
    labels=[e for e in elements if e.kind in {"paragraph","sidebar"} and not e.metadata.get("render_suppressed")
            and re.fullmatch(r"\s*[A-Za-zΑ-ω][0-9₀-₉]+\s*:\s*",e.metadata.get("source_text",e.text))]
    for label in labels:
        baseline=_source_baseline(label,glyph_map)
        candidates=[e for e in elements if e.id!=label.id and e.kind=="paragraph" and not e.metadata.get("render_suppressed")
                    and e.metadata.get("source_block")==label.metadata.get("source_block")
                    and 0<=e.bbox[0]-label.bbox[2]<30 and abs(_source_baseline(e,glyph_map)-baseline)<1.5]
        if len(candidates)!=1:
            _issue(book,"source_statement_label_unpaired","Indexed source label has no unique same-baseline statement",label.page,label.id,severity="error")
            continue
        target=candidates[0]
        target.text=label.text+" "+target.text
        target.metadata["source_text"]=label.metadata.get("source_text",label.text)+" "+target.metadata.get("source_text",target.text)
        target.asset_ids=list(dict.fromkeys(label.asset_ids+target.asset_ids))
        target.bbox=_rounded(_rect_union([label.bbox,target.bbox]))
        target.metadata["glyph_ids"]=label.metadata.get("glyph_ids",[])+target.metadata.get("glyph_ids",[])
        target.metadata.setdefault("merged_source_ids",[]).append(label.id)
        target.metadata["statement_label_id"]=label.id
        target.group_id=target.id
        label.metadata.update(render_suppressed=True,translatable=False,preserve_only=True,superseded_by=target.id,
                              source_glyph_ids=label.metadata.get("glyph_ids",[]),glyph_ids=[],
                              exclusion_reason="Indexed label assembled with its unique same-baseline statement")
        for glyph in glyphs:
            if glyph.get("owner")==label.id:glyph["owner"]=target.id
        elements.remove(label);elements.insert(elements.index(target),label)


def _assemble_proof_grids(book,elements,glyphs):
    """Recover a proven unruled Step/Reason grid without inventing text."""
    glyph_map={g["id"]:g for g in glyphs}
    # These are separate native text lines in many unruled tables. Bind only
    # the explicitly named headers; ordinary cross-column prose stays separate.
    for step in list(elements):
        if step.metadata.get("render_suppressed") or step.text.strip().lower()!="step":continue
        candidates=[e for e in elements if not e.metadata.get("render_suppressed") and e.text.strip().lower()=="reason"
                    and e.metadata.get("source_block")==step.metadata.get("source_block")
                    and e.bbox[0]>step.bbox[2] and abs(_source_baseline(e,glyph_map)-_source_baseline(step,glyph_map))<.8]
        if len(candidates)!=1:continue
        reason=candidates[0]
        step.text+=" "+reason.text
        step.metadata["source_text"]=step.metadata.get("source_text","Step")+" "+reason.metadata.get("source_text","Reason")
        step.metadata.setdefault("glyph_ids",[]).extend(reason.metadata.get("glyph_ids",[]))
        step.metadata.setdefault("merged_source_ids",[]).append(reason.id)
        step.bbox=_rounded(_rect_union([step.bbox,reason.bbox]))
        reason.metadata.update(render_suppressed=True,translatable=False,preserve_only=True,superseded_by=step.id,
                               source_glyph_ids=reason.metadata.get("glyph_ids",[]),glyph_ids=[],
                               exclusion_reason="Native Step/Reason header cells assembled by equal baseline")
        for g in glyphs:
            if g.get("owner")==reason.id:g["owner"]=step.id
    headers=[e for e in elements if not e.metadata.get("render_suppressed") and re.fullmatch(r"\s*Step\s+Reason\s*",e.metadata.get("source_text",e.text),re.I)]
    for header in headers:
        hs=sorted([glyph_map[k] for k in header.metadata.get("glyph_ids",[]) if k in glyph_map and glyph_map[k]["c"].strip()],key=lambda g:g["bbox"][0])
        if len(hs)<2:continue
        cut=max(range(1,len(hs)),key=lambda i:hs[i]["bbox"][0]-hs[i-1]["bbox"][2])
        groups=[hs[:cut],hs[cut:]]
        if ["".join(g["c"] for g in gs).lower() for gs in groups]!=["step","reason"]:continue
        right=min(g["bbox"][0] for g in groups[1]);left=min(g["bbox"][0] for g in groups[0])
        possible=[]
        for e in elements:
            if e.kind!="exercise" or e.metadata.get("render_suppressed") or abs(e.bbox[0]-left)>5:continue
            match=re.match(r"\s*(\d{1,2})\.\s",e.metadata.get("source_text",e.text))
            if match and header.bbox[3]-1<=e.bbox[1]<=header.bbox[3]+200:possible.append((int(match[1]),e))
        possible.sort(key=lambda item:_source_baseline(item[1],glyph_map))
        rows=[]
        for number,e in possible:
            if number!=len(rows)+1:break
            if rows and _source_baseline(e,glyph_map)-_source_baseline(rows[-1],glyph_map)>e.style.get("source_size",10.5)*2.8:break
            rows.append(e)
        if len(rows)<3:continue
        reasons=[];used=set();valid=True
        for i,step in enumerate(rows):
            start=_source_baseline(step,glyph_map)
            end=_source_baseline(rows[i+1],glyph_map)-.75 if i+1<len(rows) else start+step.style.get("source_size",10.5)*2.5
            candidates=sorted([e for e in elements if e.id!=header.id and e.kind=="paragraph" and not e.metadata.get("render_suppressed")
                               and e.id not in used and right-4<=e.bbox[0]<=right+24
                               and start-1.5<=_source_baseline(e,glyph_map)<end],key=lambda e:_source_baseline(e,glyph_map))
            if not candidates or abs(_source_baseline(candidates[0],glyph_map)-start)>1.5:valid=False;break
            reasons.append(candidates);used.update(e.id for e in candidates)
        if not valid:
            _issue(book,"source_proof_rows_unpaired","Step/Reason proof grid has an ambiguous or missing source row association",header.page,header.id,severity="error")
            continue
        pid=f"p{header.page:04d}-table-{digest([header.id,[e.id for e in rows]])[:14]}"
        children=[]
        for column,gs in enumerate(groups):
            text="".join(g["c"] for g in gs)
            child=Element(f"{pid}-header-{column}","table_cell",text,header.page,_rounded(_rect_union(g["bbox"] for g in gs)),
                          header.printed_page,dict(header.style),[],pid+"-r0",header.chapter,
                          {"source_text":text,"glyph_ids":[g["id"] for g in gs],"parent_table_id":pid,"table_row":0,"table_column":column})
            children.append(child)
            for glyph in gs:glyph["owner"]=child.id
        cell_rows=[[e.id for e in children]]
        removed={header.id}|{e.id for e in rows}|used
        for row_index,(step,pieces) in enumerate(zip(rows,reasons),1):
            reason=pieces[0]
            for extra in pieces[1:]:
                reason.text=(reason.text[:-1]+extra.text) if reason.text.endswith("-") else reason.text+" "+extra.text
                reason.metadata["source_text"]+="\n"+extra.metadata.get("source_text",extra.text)
                reason.metadata.setdefault("glyph_ids",[]).extend(extra.metadata.get("glyph_ids",[]))
                reason.metadata.setdefault("merged_source_ids",[]).append(extra.id)
                reason.asset_ids.extend(a for a in extra.asset_ids if a not in reason.asset_ids)
                reason.bbox=_rounded(_rect_union([reason.bbox,extra.bbox]))
                for glyph in glyphs:
                    if glyph.get("owner")==extra.id:glyph["owner"]=reason.id
                for issue in book.issues:
                    if issue.get("element_id")==extra.id:issue["element_id"]=reason.id
            for column,cell in enumerate((step,reason)):
                cell.kind="table_cell";cell.group_id=pid+f"-r{row_index}"
                cell.metadata.update(parent_table_id=pid,table_row=row_index,table_column=column,table_headers=[["Step","Reason"]])
                if column==0:cell.metadata.update(translatable=False,preserve_only=True,exclusion_reason="Literal source proof step number and protected mathematics")
            children.extend((step,reason));cell_rows.append([step.id,reason.id])
        bbox=_rounded(_rect_union(e.bbox for e in children))
        parent=Element(pid,"table","",header.page,bbox,header.printed_page,dict(header.style),[],pid,header.chapter,
                       {"proof_grid":True,"glyph_ids":[],"table_grid":{"rows":cell_rows,"header_rows":1,"col_widths":[right-left,bbox[2]-right],
                        "stroke":0,"heading_fill":"ffffff","borderless":True,"header_fill":None,"repeat_header":True,"evidence":"Native Step/Reason header, consecutive step labels and unique source baselines"}})
        header.metadata.update(render_suppressed=True,translatable=False,preserve_only=True,superseded_by=pid,
                               source_glyph_ids=header.metadata.get("glyph_ids",[]),glyph_ids=[],
                               exclusion_reason="Original header split into its two native proof-table cells")
        index=elements.index(header)
        elements[:]=[e for e in elements if e.id not in removed]
        elements[index:index]=[parent,header]+children


def _merge_sidebar_prose(book,elements,glyphs):
    if _frontmatter_kind(elements):return
    previous_by_block={}
    remove=[]
    for element in elements:
        if element.kind not in {"sidebar","footnote"} or element.metadata.get("render_suppressed") or element.metadata.get("role")=="chapter_contents":continue
        if element.text in {"Links","Extra","Examples","Assessment"} or element.text.startswith("EXAMPLE"):continue
        key=(element.kind,element.metadata.get("source_block"),element.style.get("font"),element.style.get("column",0))
        previous=previous_by_block.get(key)
        if (previous and previous.kind==element.kind and previous.metadata.get("source_block")==element.metadata.get("source_block")
            and previous.style.get("font")==element.style.get("font") and abs(previous.bbox[0]-element.bbox[0])<12
            and 0<=element.bbox[1]-previous.bbox[3]<element.style.get("source_size",9)*.9):
            previous.text=(previous.text[:-1]+element.text) if previous.text.endswith("-") else previous.text+" "+element.text
            previous.metadata["source_text"]+="\n"+element.metadata.get("source_text","")
            previous.metadata.setdefault("glyph_ids",[]).extend(element.metadata.get("glyph_ids",[]))
            previous.metadata.setdefault("merged_source_ids",[]).append(element.id)
            previous.asset_ids.extend(element.asset_ids);previous.bbox=_rounded(_rect_union([previous.bbox,element.bbox]));remove.append(element.id)
            for glyph in glyphs:
                if glyph.get("owner")==element.id:glyph["owner"]=previous.id
            for issue in book.issues:
                if issue.get("element_id")==element.id:issue["element_id"]=previous.id
        else:previous_by_block[key]=element
    elements[:]=[e for e in elements if e.id not in remove]


def _repair_hyphenated_flow(book,elements,glyphs):
    """Join a uniquely proven next physical line across PDF element classes.

    Global reading-order adjacency and frontmatter class are irrelevant here:
    numbered hanging indents, narrow continuations and portrait wraps all use
    the same normal-font baseline evidence. A new question is never consumed.
    """
    glyph_map={g["id"]:g for g in glyphs}
    allowed={"paragraph","exercise","sidebar","footnote"}
    def metrics(e):
        size=e.style.get("source_size",10.5)
        gs=[glyph_map[k] for k in e.metadata.get("glyph_ids",[]) if k in glyph_map]
        normal=[g for g in gs if size*.86<=g["size"]<=size*1.14 and g["c"].isalpha()]
        if not normal:normal=[g for g in gs if size*.86<=g["size"]<=size*1.14]
        if normal:
            first=min(g["origin"][1] for g in normal);last=max(g["origin"][1] for g in normal)
            line=[g for g in normal if abs(g["origin"][1]-first)<1]
            return first,last,min(g["bbox"][0] for g in line)
        return e.bbox[1]+size*.8,e.bbox[3]-size*.2,e.bbox[0]
    changed=True
    while changed:
        changed=False
        active=[e for e in elements if e.kind in allowed and not e.metadata.get("render_suppressed") and not e.metadata.get("parent_table_id")]
        for first in active:
            match=re.search(r"([A-Za-z]{2,})-$",first.text)
            if not match:continue
            size=first.style.get("source_size",10.5);_,last,_=metrics(first)
            candidates=[]
            for other in active:
                if other.id==first.id or other.page!=first.page:continue
                source=other.metadata.get("source_text",other.text).lstrip()
                if not re.match(r"[a-z]",source) or EXERCISE.match(source) or SUBQUESTION.match(source):continue
                if other.metadata.get("list_label") or other.metadata.get("role")=="chapter_contents":continue
                if abs(other.style.get("source_size",size)-size)>max(.35,size*.08):continue
                baseline,_,x=metrics(other);advance=baseline-last
                if not size*.7<=advance<=size*1.75:continue
                # The column detector can miss an exercise region; native
                # horizontal overlap remains mandatory even when both say 0.
                overlap=min(first.bbox[2],other.bbox[2])-max(first.bbox[0],other.bbox[0])
                if overlap<=0:continue
                hanging=first.bbox[0]-24<=x<=first.bbox[0]+size*3.5
                portrait=(abs(first.bbox[2]-other.bbox[2])<4 and other.bbox[0]<first.bbox[0]
                          and (first.kind==other.kind=="footnote" or first.metadata.get("source_block")==other.metadata.get("source_block")
                               or first.metadata.get('biography_group') and first.metadata.get('biography_group')==other.metadata.get('biography_group')))
                if not(hanging or portrait):continue
                if first.style.get("column")!=other.style.get("column") and not portrait:continue
                candidates.append((advance,abs(x-first.bbox[0]),other))
            if not candidates:continue
            candidates.sort(key=lambda item:(item[0],item[1],item[2].id))
            if len(candidates)>1 and abs(candidates[0][0]-candidates[1][0])<1 and abs(candidates[0][1]-candidates[1][1])<3:
                continue
            other=candidates[0][2]
            source_before=first.metadata.get("source_text",first.text)
            first.text=first.text[:-1]+other.text
            first.asset_ids=list(dict.fromkeys(first.asset_ids+other.asset_ids))
            first.metadata["source_text"]=source_before+"\n"+other.metadata.get("source_text",other.text)
            first.metadata.setdefault("glyph_ids",[]).extend(other.metadata.get("glyph_ids",[]))
            first.metadata.setdefault("merged_source_ids",[]).extend([other.id]+other.metadata.get("merged_source_ids",[]))
            first.metadata.setdefault("hyphenated_source_joins",[]).append({"continuation_id":other.id,"source_fragment":match[1],
                "continuation_prefix":other.metadata.get("source_text",other.text)[:40],"evidence":"unique next normal-font baseline with overlapping source column"})
            first.bbox=_rounded(_rect_union([first.bbox,other.bbox]))
            for g in glyphs:
                if g.get("owner")==other.id:g["owner"]=first.id
            for issue in book.issues:
                if issue.get("element_id")==other.id:issue["element_id"]=first.id
            elements.remove(other);changed=True;break


def _check_word_fragments(book,elements):
    for i,element in enumerate(elements):
        if element.metadata.get("render_suppressed") or not re.search(r"[A-Za-z]-$",element.text):continue
        close=[other for other in elements[i+1:i+8] if other.style.get("column")==element.style.get("column") and other.kind in {"paragraph","sidebar","footnote","exercise"} and 0<=other.bbox[1]-element.bbox[3]<element.style.get("source_size",10)*1.3 and other.metadata.get("source_text",other.text)[:1].islower()]
        if close:_issue(book,"source_word_fragment_unresolved","Hyphenated source word remains split across independent translation elements",element.page,element.id,severity="error")


def _frontmatter_kind(elements):
    text="\n".join(e.metadata.get("source_text",e.text) for e in elements)
    if re.search(r"\bContents\b",text) and len(re.findall(r"(?:\.\s*){5,}",text))>=3:
        return "contents"
    if re.search(r"\bAbout the Author\b",text):return "biography"
    if re.search(r"\ball rights reserved\b",text,re.I) and re.search(r"\bISBN\b",text):return "colophon"
    if any(re.fullmatch(r"\s*Preface\s*",e.metadata.get("source_text",e.text),re.I) for e in elements):return "preface"
    return None


def _annotate_page_structure(elements):
    """Only add evidence-based rendering metadata; accepted text stays exact."""
    frontmatter=_frontmatter_kind(elements)
    if frontmatter:
        for e in elements:
            e.metadata.update(role="frontmatter",frontmatter_kind=frontmatter)
    # Printed TOC section numbers and their titles are separate source runs.
    # Keep both identities, but expose their shared baseline to the renderer.
    for i,e in enumerate(elements):
        numeric=bool(re.fullmatch(r"\d+(?:\.\d+)*",e.text.strip()))
        if not numeric or not (frontmatter=="contents" or e.kind=="heading"):continue
        candidates=[q for q in elements if q.id!=e.id and q.page==e.page and q.bbox
                    and q.kind in {"paragraph","heading"} and q.bbox[0]>=e.bbox[2]
                    and abs(q.bbox[1]-e.bbox[1])<=max(2,e.style.get("source_size",10)*.3)]
        if not candidates:continue
        title=min(candidates,key=lambda q:q.bbox[0]-e.bbox[2])
        for q in (e,title):q.metadata.update(heading_group=e.id,heading_join="inline")
        if elements.index(title)<elements.index(e):elements.remove(e);elements.insert(elements.index(title),e)
    for element in elements:
        if element.kind=="sidebar" and element.text.strip() in {"and","or","but"}:
            before=[e for e in elements if e.kind=="paragraph" and 0<=element.bbox[1]-e.bbox[3]<35 and abs(e.bbox[0]-element.bbox[0])<30]
            after=[e for e in elements if e.kind=="paragraph" and 0<=e.bbox[1]-element.bbox[3]<35 and abs(e.bbox[0]-element.bbox[0])<30]
            if before and after:
                prior=min(before,key=lambda e:element.bbox[1]-e.bbox[3]);following=min(after,key=lambda e:e.bbox[1]-element.bbox[3])
                element.metadata.update(render_as="paragraph",role="body_connective")
                for member in (prior,element,following):member.group_id=prior.id
    for element in elements:
        if element.kind=="caption" and re.match(r"^\s*(?:Table|Figure)\s*\d+",element.metadata.get("source_text",element.text)):
            element.metadata.update(render_as="paragraph",role="body_reference",caption_candidate_rejected=True)
    for element in list(elements):
        if not element.metadata.get("drop_cap"):continue
        following=[q for q in elements if q.id!=element.id and q.kind=="paragraph" and q.metadata.get("source_text",q.text)[:1].islower()
                   and element.bbox[1]-1<=q.bbox[1]<element.bbox[3] and q.bbox[2]>element.bbox[2]]
        if following:
            target=min(following,key=lambda q:(abs(q.bbox[1]-element.bbox[1]),q.bbox[0]))
            for q in (element,target):q.metadata.update(inline_group=element.id,inline_join="word")
            elements.remove(element);elements.insert(elements.index(target),element)
    if frontmatter=="biography":
        for i,element in enumerate(elements):
            if element.kind!="sidebar" or element.metadata.get("drop_cap"):continue
            previous=next((q for q in reversed(elements[:i]) if q.kind=="paragraph" and q.metadata.get("source_block")==element.metadata.get("source_block")),None)
            if previous and 0<=element.bbox[1]-previous.bbox[3]<previous.style.get("source_size",10.5):
                for q in (previous,element):q.metadata.update(inline_group=previous.id,inline_join="space")
    return frontmatter


def _chapter_rail(elements,page_width):
    banner=next((e for e in elements if e.metadata.get("chapter_start") and e.metadata.get("banner_background_asset_ids")),None)
    if not banner:return None
    rail=[e for e in elements if e.kind=="sidebar" and not e.metadata.get("render_suppressed") and e.bbox[2]<page_width*.28 and e.bbox[0]<page_width*.23]
    if len(rail)<3:return None
    entries=[e for e in rail if re.match(r"^\d+\.\d+\s",e.text)]
    if len(entries)<2:return None
    bottom=max(e.bbox[3] for e in rail)
    body=[e for e in elements if e.kind=="paragraph" and not e.metadata.get("render_suppressed") and e.bbox[0]>=page_width*.25 and e.bbox[1]<bottom+3]
    current=None
    for element in rail:
        if re.match(r"^\d+\.\d+\s",element.text):current=element.id
        element.metadata.update(role="chapter_contents",chapter_contents_group=banner.id,
                                heading_group=current or element.id,heading_join="inline")
    return {"sidebar_ids":[e.id for e in rail],"body_ids":[e.id for e in body],
            "bbox":_rounded(_rect_union(e.bbox for e in rail)),"body_bbox":_rounded(_rect_union(e.bbox for e in body))}


def _caption_band(visual,lines,glyphs):
    """Detach a top TABLE caption without guessing the logical table columns."""
    r=fitz.Rect(visual.bbox)
    inside={g["id"] for g in glyphs if r.contains((fitz.Rect(g["bbox"]).tl+fitz.Rect(g["bbox"]).br)*.5)}
    rows=[]
    for line in lines:
        gs=[g for g in line["chars"] if g["id"] in inside]
        if gs:rows.append({**line,"chars":gs,"text":"".join(g["c"] for g in gs),"bbox":_rounded(_rect_union(g["bbox"] for g in gs))})
    rows.sort(key=lambda l:(l["bbox"][1],l["bbox"][0]))
    if not rows or not re.match(r"^\s*TABLE\s*\d+",rows[0]["text"]):return None
    selected=[rows[0]]
    bottom=rows[0]["bbox"][3]
    for row in rows[1:]:
        size=statistics.median(g["size"] for g in row["chars"])
        if row["bbox"][1]-bottom>max(2,size*.35):break
        selected.append(row);bottom=max(bottom,row["bbox"][3])
    remaining=[g for g in glyphs if g["id"] in inside and g["id"] not in {g["id"] for row in selected for g in row["chars"]}]
    if not remaining or not re.search(r"[A-Za-z]{3,}"," ".join(l["text"] for l in selected)):return None
    next_top=min(g["bbox"][1] for g in remaining)
    if next_top<=bottom+.2:return None
    body=fitz.Rect(r.x0,(bottom+next_top)/2,r.x1,r.y1)
    return selected,remaining,body


def _detached_label(book,doc,page,outdir,lines,kind,printed,column,chapter,parent):
    selected=[g for line in lines for g in line["chars"]]
    eid=f"p{page.number+1:04d}-label-{digest([book.source_sha256,[g['id'] for g in selected]])[:14]}"
    parts,assets=[],[]
    for line in lines:
        gs=line["chars"];cursor=0;fragments=[]
        for a,b in _math_spans(line) if kind=="caption" else []:
            fragments.append("".join(g["c"] for g in gs[cursor:a]));group=gs[a:b]
            box=_rect_union(g["bbox"] for g in group);mt="".join(g["c"] for g in group)
            baseline=statistics.median(g["origin"][1] for g in gs if g["size"]>=statistics.median(q["size"] for q in gs)*.9)
            asset=_asset(book,doc,page,outdir,"formula",box+(-.15,-.15,.15,.15),mt,baseline,
                         {"glyph_ids":[g["id"] for g in group],"detector":"detached-caption-math"})
            assets.append(asset.id);fragments.append(f"⟦F:{asset.id}⟧")
            for g in group:g["asset_id"]=asset.id
            if mt.count("(")!=mt.count(")") or mt.count("[")!=mt.count("]"):
                _issue(book,"formula_boundary_review","Detached caption has ambiguous formula boundary: "+mt,page.number+1,eid,severity="error")
            cursor=b
        fragments.append("".join(g["c"] for g in gs[cursor:]));parts.append("".join(fragments))
    text=_normal_text(" ".join(parts)).strip()
    if kind=="heading":text=re.sub(r"C\s+H\s+A\s+P\s+T\s+E\s+R","CHAPTER",text)
    label=Element(eid,kind,text,page.number+1,_rounded(_rect_union(g["bbox"] for g in selected)),printed,
                  {"source_size":statistics.median(g["size"] for g in selected),"column":column,"columns":1,
                   "color":f"{selected[0]['color']:06x}"},assets,parent.group_id,chapter,
                  {"source_text":"\n".join(l["text"] for l in lines),"glyph_ids":[g["id"] for g in selected],
                   "anchor_id":parent.id,"recognition":"detached-source-label"})
    for g in selected:g.update(owner=eid,disposition="translation-source")
    return label


def _promote_detached_table_bodies(book,doc,page,outdir,elements,lines,glyphs):
    try:
        from .table_adapter import extract_table_body_grid
    except ImportError:return
    for visual in list(elements):
        if visual.kind!="table" or visual.metadata.get("table_body_mode")!="manual-language-cells":continue
        diagnostics=[]
        grid=extract_table_body_grid(page,visual.metadata["source_bbox"],visual.bbox[1],diagnostics=diagnostics)
        if not grid:
            visual.metadata["body_grid_diagnostics"]=diagnostics
            continue
        saved={g["id"]:dict(g) for g in glyphs if g["owner"]==visual.id}
        for glyph in glyphs:
            if glyph["id"] in saved:glyph["owner"]=None
        structured=_structured_table(book,doc,page,outdir,{"bbox":grid["bbox"]},grid,visual.printed_page,
                                      visual.style.get("column",0),visual.chapter,lines,glyphs)
        if not structured:
            for glyph in glyphs:
                if glyph["id"] in saved:glyph.update(saved[glyph["id"]])
            visual.metadata["body_grid_diagnostics"]=[{"reason":"source_glyph_cell_membership_ambiguous"}]
            continue
        parent=structured[0];parent.id=visual.id;parent.group_id=visual.group_id
        parent.metadata.update(labels_detached=True,caption_id=visual.metadata.get("caption_id"),
                               source_bbox=visual.metadata["source_bbox"],table_body_mode="translated-grid",
                               source_text="",caption_prefix_evidence=grid.get("excluded_prefix_rows",[]))
        for child in structured[1:]:child.metadata["parent_table_id"]=parent.id
        for aid in visual.asset_ids:book.assets[aid].metadata.update(superseded_by=parent.id,exclusion_reason="verified caption-free source table body rebuilt from translated cells")
        index=elements.index(visual);elements[index:index+1]=structured
        book.issues=[issue for issue in book.issues if not(issue.get("element_id")==visual.id and issue["code"] in {"visual_label_untranslated","table_grid_manual_review"})]


def _repair_page_structure(book,doc,page,outdir,elements,lines,glyphs):
    """Separate known labels from immutable visuals, retaining exact ownership."""
    number=page.number+1
    # An image and its duplicate mask can be reported as two overlapping image
    # instances. Retain their actual source composition once, not two portraits.
    image_boxes=[fitz.Rect(image['bbox']) for image in page.get_image_info()]
    photos=[e for e in elements if e.kind=="figure" and not e.metadata.get("source_text","").strip()
            and e.asset_ids and (all(book.assets[a].metadata.get("detector")=="embedded-image" for a in e.asset_ids)
            or any((fitz.Rect(e.bbox)&r).get_area()>fitz.Rect(e.bbox).get_area()*.8 for r in image_boxes))]
    for i,first in enumerate(photos):
        if first.metadata.get("render_suppressed"):continue
        for second in photos[i+1:]:
            if second.metadata.get("render_suppressed"):continue
            a,b=fitz.Rect(first.bbox),fitz.Rect(second.bbox)
            if (a&b).get_area()<min(a.get_area(),b.get_area())*.88:continue
            box=a|b
            asset=_asset(book,doc,page,outdir,"figure",box,"",metadata={"detector":"merged-layered-source-image",
                         "exclude_glyph_ids":[g["id"] for g in glyphs if box.intersects(g["bbox"])]})
            for old in first.asset_ids+second.asset_ids:book.assets[old].metadata.update(superseded_by=asset.id,exclusion_reason="overlapping source image layers composed once")
            first.asset_ids=[asset.id];first.bbox=asset.bbox
            first.metadata["merged_visual_ids"]=[second.id]
            second.metadata.update(render_suppressed=True,superseded_by=first.id,exclusion_reason="duplicate overlapping source image layer")
            second.asset_ids=[]
            book.issues=[q for q in book.issues if not(q.get("element_id")==second.id and q["code"]=="visual_caption_unresolved")]
    for visual in list(elements):
        if visual.kind not in {"figure","table"} or visual.metadata.get("labels_detached"):continue
        r=fitz.Rect(visual.bbox)
        inside=[g for g in glyphs if g["owner"]==visual.id]
        if not inside:continue
        label=None
        if visual.kind=="table":
            band=_caption_band(visual,lines,inside)
            if not band:continue
            caption_lines,body_glyphs,body=band
            label=_detached_label(book,doc,page,outdir,caption_lines,"caption",visual.printed_page,
                                   visual.style.get("column",0),visual.chapter,visual)
            body_ids={g["id"] for g in body_glyphs}
            bodytext="\n".join("".join(g["c"] for g in line["chars"] if g["id"] in body_ids) for line in lines if any(g["id"] in body_ids for g in line["chars"]))
            asset=_asset(book,doc,page,outdir,"table",body,bodytext,
                         metadata={"glyph_ids":[g["id"] for g in body_glyphs],"detector":"symbolic-table-body-after-caption"})
            # A merged native cell may contain two logical columns. The vector
            # body preserves those actual columns instead of inventing a grid.
            body_words=re.findall(r"[A-Za-z]{2,}",bodytext)
            symbolic=all(set(word)<={"T","F"} for word in body_words)
            visual.metadata.update(source_text=bodytext,source_bbox=visual.bbox,
                                   glyph_ids=[g["id"] for g in body_glyphs],caption_id=label.id,
                                   table_body_mode="immutable-symbolic-vector" if symbolic else "manual-language-cells")
            visual.bbox=asset.bbox
            if symbolic:
                book.issues=[i for i in book.issues if not (i.get("element_id")==visual.id and i["code"]=="table_grid_manual_review")]
            for g in body_glyphs:g.update(owner=visual.id,asset_id=asset.id,disposition="preserved-in-asset")
        elif r.width>page.rect.width*.85 and r.y0<10 and r.height<page.rect.height*.3:
            ids={g["id"] for g in inside};label_lines=[]
            for line in lines:
                gs=[g for g in line["chars"] if g["id"] in ids]
                if gs:label_lines.append({**line,"chars":gs,"text":"".join(g["c"] for g in gs)})
            label_lines.sort(key=lambda l:(min(g["bbox"][1] for g in l["chars"]),min(g["bbox"][0] for g in l["chars"])))
            raw=" ".join(l["text"] for l in label_lines)
            if len(raw)>220 or not re.search(r"[A-Za-z]{3,}",raw):continue
            label=_detached_label(book,doc,page,outdir,label_lines,"heading",visual.printed_page,0,visual.chapter,visual)
            asset=_asset(book,doc,page,outdir,"figure",r,"",metadata={"exclude_glyph_ids":sorted(ids),"detector":"text-free-banner-background"})
            title_lines=[l for l in label_lines if re.search(r"[A-Za-z]{3,}",l["text"]) and not re.fullmatch(r"C\s+H\s+A\s+P\s+T\s+E\s+R|CHAPTER",l["text"].strip())]
            chapter_label=[g for l in label_lines if re.fullmatch(r"C\s+H\s+A\s+P\s+T\s+E\s+R|CHAPTER",l["text"].strip()) for g in l["chars"]]
            chapter_number=[g for l in label_lines if re.fullmatch(r"\d+",l["text"].strip()) for g in l["chars"]]
            label.metadata.update(banner_background_asset_ids=[asset.id],heading_level=1,
                                  banner_label_bbox=_rounded(_rect_union(g["bbox"] for g in chapter_label)),
                                  banner_number_bbox=_rounded(_rect_union(g["bbox"] for g in chapter_number)),
                                  banner_text_bbox=_rounded(_rect_union(g["bbox"] for l in title_lines for g in l["chars"])),
                                  banner_text_color="231f20",chapter_start=bool(re.search(r"CHAPTER\s*\d+",label.text)))
            if label.metadata["chapter_start"]:
                match=re.search(r"CHAPTER\s*(\d+)",label.text);label.chapter="chapter-"+match[1];label.metadata["chapter_number"]=match[1]
            visual.metadata.update(source_text="",rendered_by_heading=label.id,role="banner_background")
        if label is None:continue
        for old in visual.asset_ids:
            book.assets[old].metadata["superseded_by"]=asset.id
            book.assets[old].metadata["exclusion_reason"]="source linguistic label detached for translation"
        visual.asset_ids=[asset.id]
        visual.metadata["labels_detached"]=True
        for candidate in elements:
            if candidate.metadata.get("anchor_id")==visual.id and candidate.id!=label.id:
                candidate.metadata.pop("anchor_id",None)
                candidate.metadata.update(render_as="paragraph",role="body_reference",caption_candidate_rejected=True)
                candidate.group_id=candidate.id
        if visual.kind=="table":visual.metadata["caption_id"]=label.id
        elements.insert(elements.index(visual)+(1 if visual.kind=="figure" else 0),label)
        book.issues=[i for i in book.issues if not (i.get("element_id")==visual.id and
            (i["code"]=="visual_caption_unresolved" or (i["code"]=="visual_label_untranslated" and not re.search(r"[A-Za-z]{3,}",visual.metadata.get("source_text","")))))]
    _promote_detached_table_bodies(book,doc,page,outdir,elements,lines,glyphs)
    # These narrow blue source chevrons are resource pointers, not illustrations
    # requiring captions. Keep the immutable design, but mark its semantic role.
    for visual in elements:
        r=fitz.Rect(visual.bbox)
        if visual.kind=="figure" and not visual.metadata.get("source_text","").strip() and 8<r.width<16 and 16<r.height<26:
            visual.metadata.update(role="resource_pointer",caption_required=False)
            book.issues=[i for i in book.issues if not (i.get("element_id")==visual.id and i["code"]=="visual_caption_unresolved")]
    tables=[e for e in elements if e.kind=="table"]
    if tables and all(e.metadata.get("table_grid") or e.metadata.get("table_body_mode") in {"immutable-symbolic-vector","translated-grid"} for e in tables):
        book.issues=[q for q in book.issues if not(q.get("page")==number and q["code"]=="table_grid_manual_review" and not q.get("element_id"))]
    _order_exercise_markers(elements)
    frontmatter=_annotate_page_structure(elements)
    _restore_portrait_biographies(elements,page.rect.height)
    _chapter_rail(elements,page.rect.width)
    _assemble_inline_units(elements,glyphs)
    _assemble_statement_rows(book,elements,glyphs)
    _assemble_proof_grids(book,elements,glyphs)
    _merge_sidebar_prose(book,elements,glyphs)
    _repair_hyphenated_flow(book,elements,glyphs)
    _detach_figure_labels(book,doc,page,outdir,elements,lines,glyphs)
    for visual in elements:
        if visual.kind not in {"figure","table"} or visual.metadata.get("render_suppressed") or visual.metadata.get("role")=="banner_background":continue
        if visual.metadata.get("table_grid"):continue
        linguistic=[]
        for source_line in lines:
            gs=[g for g in source_line["chars"] if g.get("owner")==visual.id]
            if not gs:continue
            raw="".join(g["c"] for g in gs)
            protected={i for a,b in _math_spans({**source_line,"chars":gs,"text":raw}) for i in range(a,b)}
            language="".join(g["c"] if i not in protected else " " for i,g in enumerate(gs))
            if re.search(r"[A-Za-z]{3,}",language):linguistic.append(language.strip())
        if linguistic:
            visual.metadata["untranslated_linguistic_labels"]=linguistic
            _issue(book,"source_visual_language_untranslated","Source graphic retains natural-language labels requiring explicit translation: "+" | ".join(linguistic)[:400],number,visual.id,severity="error")
    _check_word_fragments(book,elements)
    return frontmatter


def _detach_figure_labels(book,doc,page,outdir,elements,lines,glyphs):
    """Keep diagram geometry, make natural-language labels real task elements."""
    for visual in list(elements):
        if visual.kind not in {'figure','table'} or visual.metadata.get('render_suppressed'):
            continue
        if visual.metadata.get('table_grid') or visual.metadata.get('figure_label_ids'):
            continue
        label_rows=[]
        for line in lines:
            gs=[g for g in line['chars'] if g.get('owner')==visual.id]
            if not gs:continue
            raw=''.join(g['c'] for g in gs)
            mini={**line,'chars':gs,'text':raw}
            protected={i for a,b in _math_spans(mini) for i in range(a,b)}
            prose=''.join(g['c'] if i not in protected else ' ' for i,g in enumerate(gs))
            if re.search(r'[A-Za-z]{3,}',prose):
                label_rows.append({**mini,'bbox':_rounded(_rect_union(g['bbox'] for g in gs))})
        if not label_rows:continue
        removed={g['id'] for row in label_rows for g in row['chars']}
        children=[]
        for row in label_rows:
            child=_detached_label(book,doc,page,outdir,[row],'caption',visual.printed_page,
                                  visual.style.get('column',0),visual.chapter,visual)
            child.kind='figure_label'
            child.group_id=visual.group_id or visual.id
            child.metadata.update(parent_figure_id=visual.id,role='translated_figure_label',
                                  source_label_bbox=list(child.bbox))
            children.append(child)
        old=list(visual.asset_ids)
        background=_asset(book,doc,page,outdir,'figure',visual.bbox,'',metadata={
            'detector':'translated-figure-background','exclude_glyph_ids':sorted(removed),
            'detached_label_ids':[e.id for e in children],
            'label_removal_proof':{'native_glyph_ids':sorted(removed),'graphics_removed':False}})
        for aid in old:
            if aid!=background.id:
                book.assets[aid].metadata.update(superseded_by=background.id,
                    exclusion_reason='source linguistic labels detached into translated figure-label elements')
        visual.asset_ids=[background.id]
        visual.metadata.update(figure_label_ids=[e.id for e in children],
                               source_linguistic_labels_detached=True)
        visual.metadata.pop('untranslated_linguistic_labels',None)
        visual.metadata['glyph_ids']=[k for k in visual.metadata.get('glyph_ids',[]) if k not in removed]
        at=elements.index(visual)+1
        elements[at:at]=children
        book.issues=[q for q in book.issues if not(q.get('element_id')==visual.id
                     and q['code']=='source_visual_language_untranslated')]


def _restore_portrait_biographies(elements,page_height):
    """A named, dated heading next to a portrait ends inherited exercise mode.

    This is source layout evidence, not a rule that all small text is biography.
    The group also permits the following line to widen around the portrait.
    """
    for heading in elements:
        source=heading.metadata.get('source_text',heading.text).strip()
        date=re.search(r'\(\s*\d{4}\s*[–−-]\s*\d{4}\s*\)',source)
        if not date or not source[:date.start()].strip().isupper():continue
        size=heading.style.get('source_size',10.5)
        if size>9.5:continue
        portraits=[e for e in elements if e.kind=='figure' and not e.metadata.get('render_suppressed')
                   and not e.metadata.get('source_text','').strip() and len(e.bbox)==4
                   and e.bbox[2]<=heading.bbox[0]+6 and 25<e.bbox[2]-e.bbox[0]<150
                   and abs(e.bbox[1]-heading.bbox[1])<22]
        if len(portraits)!=1:continue
        portrait=portraits[0]
        bottom=min([e.bbox[1] for e in elements if e.kind=='heading' and e.bbox[1]>heading.bbox[3]+10]
                   or [page_height-20])
        for e in elements:
            if e.kind not in {'paragraph','exercise','footnote','sidebar'} or e.metadata.get('list_label'):continue
            if not heading.bbox[1]-2<=e.bbox[1]<bottom:continue
            if abs(e.style.get('source_size',10.5)-size)>.35:continue
            if e is not heading and e.bbox[2]<portrait.bbox[2]+10:continue
            e.kind='footnote';e.group_id=heading.id
            e.metadata.update(role='biography',biography_group=heading.id,anchor_id=portrait.id)
        portrait.group_id=heading.id
        portrait.metadata.update(role='biography_portrait',caption_required=False,
                                 biography_heading_id=heading.id)


def repair_existing_structure(book: Book, outdir) -> Book:
    """Repair labels/relations in a saved analysis without modifying old assets.

    Existing prose kind/text/assets remain byte-for-byte stable. New caption or
    banner-title records need translation. Repaired vector bundles have distinct
    paths; the old source bundles and all accepted text remain recoverable.
    """
    repaired=copy.deepcopy(book)
    outdir=Path(outdir).resolve();(outdir/"assets").mkdir(parents=True,exist_ok=True)
    repaired._asset_prefix="structure-v3-"+digest([len(book.elements),sorted(book.assets)])[:8]+"-"
    with fitz.open(book.source_path) as doc:
        for page_record in repaired.pages:
            number=page_record["number"];page=doc[number-1];lines,glyphs=_collect(page)
            ledger=outdir/page_record["coverage"]["ledger"]
            if ledger.is_file():
                with gzip.open(ledger,"rt",encoding="utf-8") as stream:saved=json.load(stream)
                by_id={g["id"]:g for g in saved}
                for g in glyphs:
                    previous=by_id.get(g["id"],{})
                    for key in ("owner","disposition","asset_id"):
                        if key in previous:g[key]=previous[key]
            else:
                for element in repaired.elements:
                    if element.page!=number:continue
                    ids=set(element.metadata.get("glyph_ids",[]))
                    for g in glyphs:
                        if g["id"] in ids:g.update(owner=element.id,disposition="translation-source")
                for element in repaired.elements:
                    if element.page!=number or element.kind not in {"figure","table"}:continue
                    r=fitz.Rect(element.bbox)
                    for g in glyphs:
                        if g["owner"] is None and r.contains((fitz.Rect(g["bbox"]).tl+fitz.Rect(g["bbox"]).br)*.5):g.update(owner=element.id,disposition="preserved-in-asset")
            repaired._active_glyphs=glyphs
            elements=[e for e in repaired.elements if e.page==number]
            cols=_columns(lines,page.rect.width)
            for element in elements:
                column=int((element.bbox[0]+element.bbox[2])/2>cols["split"]) if cols["count"]==2 and element.bbox[1]>=cols["start_y"]-2 and element.bbox[1]<cols.get("end_y",float("inf"))-2 else 0
                element.style.update(column=column,columns=2 if cols["count"]==2 and element.bbox[1]>=cols["start_y"]-2 and element.bbox[1]<cols.get("end_y",float("inf"))-2 and element.bbox[2]-element.bbox[0]<page.rect.width*.55 else 1)
            glyph_map={g["id"]:g for g in glyphs}
            def element_baseline(element):
                gs=[glyph_map[k] for k in element.metadata.get("glyph_ids",[]) if k in glyph_map and "extensions" not in glyph_map[k]["font"].lower() and glyph_map[k]["size"]>=element.style.get("source_size",10)*.9]
                return min(g["origin"][1] for g in gs) if gs else element.bbox[1]
            elements=[record["element"] for record in _order([{"element":e,"bbox":e.bbox,"text":e.text,"baseline":element_baseline(e)} for e in elements],cols)]
            page_record.update(columns=cols["count"],column_split=cols["split"],column_start_y=cols["start_y"] if cols["count"]==2 else None)
            _repair_prose_tokens(repaired,elements,glyphs)
            _merge_baseline_elements(repaired,elements,glyphs)
            _merge_wrapped_elements(repaired,elements)
            _combine_wrapped_math(repaired,elements,number,outdir,doc,page)
            _merge_body_continuations(repaired,elements,glyphs,page.rect.width)
            _combine_wrapped_math(repaired,elements,number,outdir,doc,page)
            _assemble_display_math(repaired,doc,page,outdir,elements,glyphs)
            role=_repair_page_structure(repaired,doc,page,outdir,elements,lines,glyphs)
            _check_atomic_formulas(repaired,elements)
            _validate_source_references(repaired,elements,glyphs,number)
            if role:page_record.update(page_role="frontmatter",frontmatter_kind=role)
            rail=_chapter_rail(elements,page.rect.width)
            if rail:page_record["chapter_rail"]=rail
            first=next((i for i,e in enumerate(repaired.elements) if e.page==number),len(repaired.elements))
            repaired.elements=[e for e in repaired.elements if e.page!=number]
            repaired.elements[first:first]=elements
            if ledger.is_file():
                temporary=ledger.with_suffix(ledger.suffix+".tmp")
                with gzip.open(temporary,"wt",encoding="utf-8") as stream:json.dump(glyphs,stream,ensure_ascii=False,separators=(",",":"))
                temporary.replace(ledger)
            page_record["coverage"]["dispositions"]=dict(Counter(g["disposition"] for g in glyphs))
            _finalize_assets(repaired,number,outdir)
    repaired=reanchor_existing(repaired)
    repaired.profile["structure_repair_version"]="detached-labels-v3"
    return repaired


def _attach_inline_vector_symbols(book,doc,page,outdir,elements,glyphs,drawings):
    """Preserve a vector-only symbol at a uniquely proven prose-line end."""
    owners={e.id:e for e in elements if e.kind in {'paragraph','sidebar','footnote','exercise'}
            and not e.metadata.get('render_suppressed')}
    active={aid for e in elements if not e.metadata.get('render_suppressed') for aid in e.asset_ids}
    for drawing in drawings:
        r=fitz.Rect(drawing['rect'])
        if not (.4<r.width<12 and 3<r.height<18):continue
        if any((fitz.Rect(book.assets[aid].bbox)+(-.3,-.3,.3,.3)).contains(r) for aid in active):continue
        candidates=[]
        for eid,e in owners.items():
            gs=[g for g in glyphs if g.get('owner')==eid and g['c'].strip()]
            if not gs:continue
            punctuation={'.',',',';',':','!','?'}
            content=[g for g in gs if g['c'] not in punctuation]
            if not content:continue
            tail=max(content,key=lambda g:(g['origin'][1],g['bbox'][0]))
            last=max(gs,key=lambda g:(g['origin'][1],g['bbox'][0]))
            trailing=last['c'] if last['c'] in punctuation and last['bbox'][0]>=r.x1-.5 else ''
            if last is not tail and not trailing:continue
            gap=r.x0-tail['bbox'][2]
            if (0<=gap<max(2,tail['size']*.5) and r.y0<=tail['origin'][1]<=r.y1+2
                    and r.height<tail['size']*1.6):
                if trailing and not e.text.rstrip().endswith(trailing):continue
                candidates.append((e,tail,trailing))
        if len(candidates)!=1:continue
        owner,tail,trailing=candidates[0]
        asset=_asset(book,doc,page,outdir,'formula',r+(-.2,-.2,.2,.2),
                     '[original vector-only inline symbol]',tail['origin'][1],metadata={
                     'detector':'native-inline-vector-symbol','source_drawing_index':drawing.get('source_drawing_index'),
                     'association_proof':{'unique_paragraph_tail_glyph':tail['id'],'source_baseline':tail['origin'][1]},
                     'unicode_identity':'not_inferred'})
        marker=' ⟦F:'+asset.id+'⟧'
        owner.text=owner.text.rstrip()[:-1].rstrip()+marker+trailing if trailing else owner.text+marker
        owner.asset_ids.append(asset.id)
        owner.metadata.setdefault('inline_vector_symbol_ids',[]).append(asset.id)
        owner.bbox=_rounded(fitz.Rect(owner.bbox)|r)
        active.add(asset.id)


def _visual_regions(page, drawings, lines, structure=None):
    regions = []
    # Verified upstream layout supplies whole diagrams, while native drawing
    # geometry establishes that the hinted region actually contains graphics.
    # This retains disconnected tree edges and their numeric nodes together.
    for layout in (structure or {}).get("layouts",[]) if (structure or {}).get("usable") else []:
        if layout.get("class_name")!="figure" or layout.get("confidence",0)<.6:continue
        r=fitz.Rect(layout["bbox"])&page.rect
        contained=[d for d in drawings if (r+(-2,-2,2,2)).contains(fitz.Rect(d["rect"]))]
        if not contained or r.width>page.rect.width*.97 or r.height>page.rect.height*.8:continue
        r|=_rect_union(d["rect"] for d in contained)
        regions.append({"kind":"figure","bbox":r+(-.5,-.5,.5,.5),"reason":"verified-babeldoc-figure-with-native-paths",
                        "layout_id":layout.get("id"),"confidence":layout.get("confidence")})
    for image in page.get_image_info():
        r = fitz.Rect(image["bbox"])
        if (r&page.rect).get_area()>page.rect.get_area()*.78:
            # A full scanned page/background is not an ordinary illustration.
            # Its OCR glyph layer must retain ownership and receive a blocking
            # unsupported-input issue from analyze(), never a false figure pass.
            continue
        if r.width > 8 and r.height > 8:
            if any(fitz.Rect(v["bbox"]).contains(r) for v in regions):continue
            regions.append({"kind":"figure", "bbox":r, "reason":"embedded-image"})
    # Use original drawing connectivity, not text block proximity, so unrelated
    # body text is never mistaken for a diagram.
    if drawings:
        try:
            clusters = page.cluster_drawings(drawings=drawings, x_tolerance=3, y_tolerance=3)
        except (AttributeError, TypeError):
            clusters = [d["rect"] for d in drawings]
        for rect in clusters:
            r = fitz.Rect(rect)
            if r.width < 4 or r.height < 3 or r.width > page.rect.width*.96 or r.height > page.rect.height*.75:
                continue
            contained = [d for d in drawings if r.intersects(d["rect"])]
            # MuPDF's cluster bounds can stop before a curve's extremum when
            # its path crosses the CropBox. Include every visible part of the
            # participating source paths before choosing the immutable crop.
            expanded = (r | _rect_union(d['rect'] for d in contained)) & page.rect
            if expanded.width <= page.rect.width*.96 and expanded.height <= page.rect.height*.75:
                r = expanded
            complex_paths = sum(len(d["items"]) for d in contained)
            # A path-count threshold drops semantic tree edges, domino
            # outlines and diagram brackets. Geometry is retained even for a
            # single path; only a fully containing asset can supersede it.
            if any((fitz.Rect(v["bbox"])+(-.5,-.5,.5,.5)).contains(r) for v in regions):
                continue
            proof_end=(abs(r.width-5.44)<.3 and abs(r.height-7.43)<.3
                       and all(d.get("fill") is None for d in contained)
                       and any(d.get("color") and d["color"][2]>.8 and d["color"][0]<.1 for d in contained))
            marker=r.width<22 and r.height<16 and not proof_end
            regions.append({"kind":"figure", "bbox":r+(-1,-1,1,1),
                            "reason":"native-proof-end-marker" if proof_end else "small-vector-marker" if marker else "vector-drawing-cluster",
                            "marker":marker,"role":"end_marker" if proof_end else None})
    # Recognize ruled tables as a single immutable asset. Their cell prose is
    # flagged for review instead of silently disappearing behind a table crop.
    if len(drawings) >= 5:
        try:
            for table in page.find_tables().tables:
                if table.row_count < 2 or table.col_count < 2:
                    continue
                r = fitz.Rect(table.bbox)
                # An axis or narrow numeric subgrid can be detected inside a
                # larger diagram. Partial intersection never authorizes losing
                # the rest of that diagram (open endpoints/rabbit symbols).
                intersecting=[v for v in regions if r.intersects(v["bbox"])]
                if any(not (r+(-2,-2,2,2)).contains(fitz.Rect(v["bbox"])) for v in intersecting):
                    continue
                regions = [v for v in regions if v not in intersecting]
                regions.append({"kind":"table", "bbox":r+(-1,-1,1,1), "reason":"ruled-table-detector"})
        except Exception:
            pass
    return regions


def _normal_text(text):
    return text.replace("⃝", "©").replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff").replace("ﬃ", "ffi").replace("ﬄ", "ffl")


def _structured_table(book,doc,page,outdir,visual,grid,printed,column,chapter,lines,glyphs):
    """Build translated cells only when source ownership is unambiguous."""
    rows=grid.get("rows",[])
    if not rows or not rows[0]:return None
    memberships={}
    cells=[cell for row in rows for cell in row]
    for g in glyphs:
        if g["owner"] is not None:continue
        center=(fitz.Rect(g["bbox"]).tl+fitz.Rect(g["bbox"]).br)*.5
        if not fitz.Rect(visual["bbox"]).contains(center):continue
        owners=[i for i,c in enumerate(cells) if fitz.Rect(c["bbox"]).contains(center)]
        if len(owners)!=1 and g["c"].strip():
            return None
        if owners:memberships[g["id"]]=owners[0]
    parent_id=f"p{page.number+1:04d}-table-{digest([book.source_sha256,_rounded(visual['bbox'])])[:14]}"
    parent=Element(parent_id,"table","",page.number+1,_rounded(grid.get("bbox",visual["bbox"])),printed,
                   {"column":column,"columns":1},[],parent_id,chapter,
                   {"table_grid":{"rows":[],"header_rows":grid.get("header_rows",1),"col_widths":grid.get("col_widths",[]),
                                  "continuation_key":grid.get("continuation_key"),"evidence":grid.get("evidence",{}),
                                  "stroke":"8b8b8b","heading_fill":"eef7fb"},"role":"structured_table"})
    elements=[parent]
    flat_index=0
    for row_index,row in enumerate(rows):
        child_ids=[]
        for col_index,cell in enumerate(row):
            selected=[g for g in glyphs if memberships.get(g["id"])==flat_index]
            flat_index+=1
            eid=f"{parent_id}-r{row_index}c{col_index}"
            child_ids.append(eid)
            tokens,asset_ids=[],[]
            source_lines=[]
            natural=bool(cell.get("translatable",True))
            if selected and not natural:
                box=_rect_union(g["bbox"] for g in selected)
                baseline=statistics.median(g["origin"][1] for g in selected)
                asset=_asset(book,doc,page,outdir,"formula",box+(-.15,-.15,.15,.15),cell.get("text",""),baseline,
                             {"glyph_ids":[g["id"] for g in selected],"detector":"protected-table-literal"})
                asset_ids.append(asset.id)
                tokens.append(f"⟦F:{asset.id}⟧")
                for g in selected:g["asset_id"]=asset.id
            elif selected:
                selected_ids={g["id"] for g in selected}
                for line in lines:
                    gs=[g for g in line["chars"] if g["id"] in selected_ids]
                    if not gs:continue
                    raw="".join(g["c"] for g in gs)
                    source_lines.append(raw)
                    mini={**line,"chars":gs,"text":raw}
                    cursor=0
                    parts=[]
                    for a,b in _math_spans(mini):
                        parts.append("".join(g["c"] for g in gs[cursor:a]))
                        group=gs[a:b]
                        box=_rect_union(g["bbox"] for g in group)
                        mt="".join(g["c"] for g in group)
                        baseline=statistics.median(g["origin"][1] for g in gs)
                        asset=_asset(book,doc,page,outdir,"formula",box+(-.15,-.15,.15,.15),mt,baseline,
                                     {"glyph_ids":[g["id"] for g in group],"detector":"table-cell-native-font-and-symbol"})
                        asset_ids.append(asset.id)
                        parts.append(f"⟦F:{asset.id}⟧")
                        for g in group:g["asset_id"]=asset.id
                        cursor=b
                    parts.append("".join(g["c"] for g in gs[cursor:]))
                    tokens.append("".join(parts))
            child=Element(eid,"table_cell",_normal_text(" ".join(tokens)).strip(),page.number+1,_rounded(cell["bbox"]),printed,
                          {"source_size":statistics.median(g["size"] for g in selected) if selected else 9.5,"column":column,"columns":1},
                          asset_ids,f"{parent_id}-row{row_index}",chapter,{"parent_table_id":parent_id,"row":row_index,"column":col_index,
                          "source_text":"\n".join(source_lines) or cell.get("text",""),"glyph_ids":[g["id"] for g in selected],
                          "table_headers":[[c.get("text","") for c in header] for header in rows[:grid.get("header_rows",1)]],
                          "translatable":natural,"literal":not natural})
            elements.append(child)
            for g in selected:g.update(owner=eid,disposition="translation-source" if natural else "preserved-original")
        parent.metadata["table_grid"]["rows"].append(child_ids)
    return elements


def analyze(pdf_path, outdir, pages=None, backend="auto", *, ocr="auto", ocr_dpi=300, corrections=None) -> Book:
    """Analyze one-based physical pages and save book.json plus immutable assets.

    ``backend`` is auto/native/babeldoc. Explicit BabelDOC requests fail if the
    pinned bridge cannot run; auto records and uses the native fallback.
    """
    if backend not in {"auto", "native", "babeldoc"}:
        raise ValueError("backend must be auto, native or babeldoc")
    if ocr not in {"auto","off","force"}:raise ValueError("ocr must be auto, off or force")
    if ocr != "off":
        from .scan_adapter import analyze as analyze_scan, scan_pages
        with fitz.open(pdf_path) as source_doc:
            if source_doc.needs_pass:raise ValueError("The PDF is encrypted; provide an unlocked copy")
            selected_scan = list(range(1,len(source_doc)+1)) if pages is None else sorted(set(int(n) for n in pages))
            if not selected_scan or min(selected_scan)<1 or max(selected_scan)>len(source_doc):raise ValueError("Invalid physical page range")
        if ocr=="force" or scan_pages(pdf_path,selected_scan):
            return analyze_scan(pdf_path,outdir,selected_scan,backend,ocr=ocr,ocr_dpi=ocr_dpi,corrections=corrections)
    path, outdir = Path(pdf_path).resolve(), Path(outdir).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir/"assets").mkdir(exist_ok=True)
    (outdir/"coverage").mkdir(exist_ok=True)
    (outdir/"analysis.parts").mkdir(exist_ok=True)
    with fitz.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("The PDF is encrypted; provide an unlocked copy")
        selected = list(range(1,len(doc)+1)) if pages is None else sorted(set(int(n) for n in pages))
        if not selected or min(selected) < 1 or max(selected) > len(doc):
            raise ValueError(f"pages must be one-based physical pages in 1..{len(doc)}")
        book = Book(str(path),file_sha256(path),doc.metadata.get("title") or path.stem,len(doc),selected)
        parser_files=[Path(__file__),Path(__file__).with_name("babel_adapter.py"),Path(__file__).with_name("table_adapter.py"),Path(__file__).with_name("paragraph_adapter.py"),Path(__file__).with_name("math_regions.py"),Path(__file__).with_name("glyph_geometry.py"),Path(__file__).with_name("drawing_geometry.py"),Path(__file__).with_name("source_flow.py"),Path(__file__).with_name("native_clipping.py"),Path(__file__).with_name("cross_page_math.py"),Path(__file__).with_name("native_containers.py"),Path(__file__).with_name("native_operators.py")]
        from .babel_adapter import available as babel_available, layout_model_identity
        signature=digest({"source":book.source_sha256,"selected_pages":selected,"backend":backend,"rules":RULES_VERSION,
                          "schema":book.schema_version,"babeldoc":babel_available(),
                          "local_layout_model":layout_model_identity() if backend!="native" else {"disabled":True},
                          "parser_files":{f.name:file_sha256(f) for f in parser_files if f.exists()}})
        partial=outdir/"analysis.partial.json"
        try:
            book=_restore_checkpoint(book,outdir,signature)
        except (ValueError,KeyError,TypeError,OSError,json.JSONDecodeError):
            # A stale/corrupt checkpoint never contaminates a new analysis.
            pass
        book.profile["analysis_signature"]=signature
        book.profile["analysis_requested_backend"]=backend
        book.profile["job_dir"] = str(outdir)
        book.profile.setdefault("parser_backend", "native")
        completed={p["number"] for p in book.pages}
        source = {}
        babel = None
        use_babel = False
        if backend != "native":
            from .babel_adapter import extract, BabelUnavailable, available
            ok, detail = available()
            if ok:
                use_babel = True
            elif backend == "babeldoc":
                raise BabelUnavailable(detail)
            else:
                _issue(book,"parser_native_fallback",detail)
        chapter = book.elements[-1].chapter if book.elements else ""
        for page_index,number in enumerate(selected):
            if number in completed:continue
            page = doc[number-1]
            if use_babel and not source:
                # Bound raw glyph memory even for thousand-page books; upstream
                # IL and styles are released after each twelve-page batch.
                batch=selected[page_index:page_index+12]
                source={n:_collect(doc[n-1]) for n in batch}
                try:
                    babel=extract(path,outdir/"babeldoc"/f"batch-{number:04d}",batch,{n:p[0] for n,p in source.items()})
                    book.profile["parser_backend"]=babel["backend"]
                    book.profile["babeldoc_version"]=babel["version"]
                    book.profile["babeldoc_coordinate_transform"]=babel.get("coordinate_transform_version","unverified")
                    book.profile["structure_provenance"]=babel.get("structure_provenance",{})
                    for diagnostic in babel.get("structure_diagnostics",[]):_issue(book,diagnostic.get("code","neural_structure_unavailable"),diagnostic.get("detail",str(diagnostic)),number)
                    gc.collect()
                except Exception as exc:
                    if backend=="babeldoc":
                        raise
                    _issue(book,"parser_native_fallback",str(exc),number)
                    use_babel=False
                    babel=None
            lines,glyphs=source.pop(number) if number in source else _collect(page)
            uncertain_geometry=[g['id'] for g in glyphs if g.get('ink_geometry_error')]
            if uncertain_geometry:
                _issue(book,'native_math_glyph_geometry_unresolved',
                       f'{len(uncertain_geometry)} extensible mathematical glyphs lack unambiguous native ink bounds',
                       number,severity='error')
            book._active_glyphs=glyphs
            full_images=[image for image in page.get_image_info() if (fitz.Rect(image["bbox"])&page.rect).get_area()>page.rect.get_area()*.78]
            if full_images or any("glyphless" in g["font"].lower() for g in glyphs):
                _issue(book,"scanned_or_ocr_page_unsupported","Page is a full-page raster scan/OCR layer; OCR/math acceptance is outside this release. Source text is recorded, not certified as complete",number,severity="error")
            printed, label_source, label_confidence = _printed_page(page,lines)
            cols = _columns(lines,page.rect.width)
            structure=(babel["pages"].get(number,{}) if babel else {}).get("structure",{})
            structure_alignment=_apply_structure_hints(lines,cols,structure,page.rect.width)
            if babel and not structure.get("usable"):
                detail={"reason":"Neural structure hints rejected; native source geometry remains authoritative", "coverage":structure.get("coverage"),"diagnostics":structure.get("diagnostics",[])}
                _issue(book,"neural_structure_hints_rejected",json.dumps(detail,ensure_ascii=False),number)
            all_drawings = source_drawings(page)
            drawings = [d for d in all_drawings if not d.get('fully_source_clipped')]
            body_sizes = [g["size"] for g in glyphs if g["c"].isalpha() and not MATH_FONT.search(g["font"])]
            body_size = statistics.median(body_sizes) if body_sizes else 10.5
            hints = babel["pages"].get(number,{}).get("formulas",[]) if babel else []
            visuals = _visual_regions(page,drawings,lines,structure)
            page_elements = []
            for visual in visuals:
                r = visual["bbox"]
                if visual["kind"]=="table":
                    diagnostics=[]
                    try:
                        from .table_adapter import extract_table_grid
                        grid=extract_table_grid(page,r,diagnostics=diagnostics)
                    except ImportError:
                        grid=None
                    if grid:
                        structured=_structured_table(book,doc,page,outdir,visual,grid,printed,
                                                     int(r.x0>cols["split"]) if cols["count"]==2 else 0,chapter,lines,glyphs)
                        if structured:
                            page_elements.extend(structured)
                            continue
                        diagnostics.append({"reason":"source_glyph_cell_membership_ambiguous"})
                    visual["table_diagnostics"]=diagnostics
                # Center membership prevents sharing boundary glyphs twice.
                inside = [g for g in glyphs if r.contains((fitz.Rect(g["bbox"]).tl+fitz.Rect(g["bbox"]).br)*.5)]
                text = "".join(g["c"] for g in inside)
                asset = _asset(book,doc,page,outdir,visual["kind"],r,text,
                               metadata={"detector":visual["reason"]})
                eid = f"p{number:04d}-visual-{digest([book.source_sha256,asset.id])[:12]}"
                element = Element(eid,visual["kind"],"",number,asset.bbox,printed,
                                  {"column":int(r.x0>cols["split"]) if cols["count"]==2 else 0},
                                  [asset.id],eid,chapter,{"source_text":text,"preserved_original":True,
                                                        "role":visual.get("role") or ("exercise_marker" if visual.get("marker") else visual["kind"])})
                page_elements.append(element)
                if visual.get("table_diagnostics") is not None:
                    _issue(book,"table_grid_manual_review",json.dumps(visual["table_diagnostics"],ensure_ascii=False) or "Grid confidence insufficient; source table retained",number,eid)
                for g in inside:
                    g.update(owner=eid,disposition="preserved-in-asset")
                if re.search(r"[A-Za-z]{3,}",text):
                    _issue(book,"visual_label_untranslated","Figure/table language retained in immutable source asset; review labels/cells",number,eid)
            from .source_flow import annotate_rhs_numeric_continuations
            ordered = annotate_rhs_numeric_continuations(_order(lines,cols),cols)
            exercise_group = None
            exercise_mode = False
            current = None
            for line in ordered:
                remaining = [g for g in line["chars"] if g["owner"] is None]
                if not remaining:
                    continue
                line = {**line,"chars":remaining,"text":"".join(g["c"] for g in remaining)}
                rect = _rect_union(g["bbox"] for g in remaining)
                original = line["text"]
                text = original.strip()
                subquestion=_subquestion(line)
                rhs_numeric=line.get("rhs_numeric_continuation")
                exercise_match=None if rhs_numeric else EXERCISE.match(text)
                if not text:
                    for g in remaining: g.update(owner="whitespace",disposition="excluded-whitespace")
                    continue
                column = int((rect.x0+rect.x1)/2>cols["split"]) if cols["count"]==2 and rect.y0>=cols["start_y"]-2 and rect.y0<cols.get("end_y",float("inf"))-2 else 0
                eid = f"p{number:04d}-e-{digest([book.source_sha256,[g['id'] for g in remaining]])[:14]}"
                sizes = [g["size"] for g in remaining if g["c"].strip()]
                size = statistics.median(sizes) if sizes else body_size
                code = all(CODE_FONT.search(g["font"]) for g in remaining if g["c"].strip())
                from .source_pages import pagination_candidates
                marginal = rect.y1 < page.rect.height*.065 or bool(pagination_candidates(page,[line]))
                letters=re.findall(r"[A-Za-z]",text)
                heading = size > body_size*1.22 and (len(letters)>1 or bool(re.fullmatch(r"\d+(?:\.\d+)+",text)))
                if marginal:
                    kind = "running_header"
                elif code:
                    kind = "code"
                elif CAPTION.match(text):
                    kind = "caption"
                elif current is not None and current.kind=="caption" and current.metadata["source_block"]==line["block"] and rect.y0-current.bbox[3]<size*.85:
                    kind = "caption"
                elif heading:
                    kind = "heading"
                elif exercise_match or exercise_mode:
                    kind = "exercise"
                elif rect.y0>page.rect.height*.77 and size<body_size*.87:
                    kind = "footnote"
                elif rect.x1<page.rect.width*.29 and rect.width<page.rect.width*.2:
                    kind = "sidebar"
                else:
                    kind = "paragraph"
                if text in {"Exercises","Supplementary Exercises","Review Questions"}:
                    exercise_mode = True
                if exercise_match and cols["count"]==2 and rect.y0>=cols["start_y"]-2:
                    exercise_mode = True
                section=re.match(r"^(\d+(?:\.\d+){0,4})(?:\s+[A-Za-z]|$)",text) if kind=="heading" else None
                if kind=="heading" and section:exercise_mode=False;exercise_group=None
                if section and printed and not re.fullmatch(r"[ivxlcdmIVXLCDM]+",printed):
                    chapter="chapter-"+section[1].split(".")[0]
                if exercise_match:
                    exercise_group = eid
                elif kind != "exercise":
                    exercise_group = None
                assets, parts = [], []
                spans = [] if kind in {"code","running_header"} else _math_spans(line,hints)
                if rhs_numeric and kind not in {"code","running_header"}:
                    end=rhs_numeric["numeric_char_end"]
                    tail={**line,"chars":remaining[end:],"text":"".join(g["c"] for g in remaining[end:])}
                    spans=[(0,end)]+[(a+end,b+end) for a,b in _math_spans(tail,hints)]
                cursor = 0
                for a,b in spans:
                    parts.append("".join(g["c"] for g in remaining[cursor:a]))
                    group = remaining[a:b]
                    mb = _rect_union(g["bbox"] for g in group)
                    # Include fraction bars and radical overbars crossing the
                    # exact glyph crop, without adding neighboring body text.
                    for drawing in drawings:
                        dr = fitz.Rect(drawing["rect"])
                        if dr.height < 2 and dr.width < mb.width+5 and (mb+(-1,-3,1,1)).intersects(dr):
                            mb |= dr
                    baseline_candidates=[g["origin"][1] for g in remaining if g["size"]>=size*.9 and "extensions" not in g["font"].lower()]
                    baseline = statistics.median(baseline_candidates or [g["origin"][1] for g in group])
                    if not mb.y0<=baseline<=mb.y1:
                        baseline=statistics.median(g["origin"][1] for g in group)
                    mt = "".join(g["c"] for g in group)
                    asset = _asset(book,doc,page,outdir,"formula",mb+(-.15,-.15,.15,.15),mt,baseline,
                                   {"detector":"babeldoc-and-native" if hints else "native-font-and-symbol", "glyph_ids":[g["id"] for g in group]})
                    assets.append(asset.id)
                    parts.append(f"⟦F:{asset.id}⟧")
                    for g in group: g["asset_id"] = asset.id
                    if mt.count("(") != mt.count(")") or mt.count("[") != mt.count("]"):
                        _issue(book,"formula_boundary_review","Formula parentheses cross a source line or heuristic boundary: "+mt,number,eid,severity="error")
                    cursor=b
                parts.append("".join(g["c"] for g in remaining[cursor:]))
                translated_source = _normal_text("".join(parts)).strip()
                if kind=="heading":translated_source=re.sub(r"^(\d+(?:\.\d+)+)(?=[A-Z])",r"\1 ",translated_source)
                if kind=="code":
                    asset = _asset(book,doc,page,outdir,"code",rect+(-.2,-.2,.2,.2),original,
                                   statistics.median(g["origin"][1] for g in remaining),
                                   {"glyph_ids":[g["id"] for g in remaining]})
                    assets = [asset.id]
                    translated_source = original
                    for g in remaining:g["asset_id"]=asset.id
                if kind=="paragraph" and spans and not re.search(r"[A-Za-z]",re.sub(r"⟦F:[^⟧]+⟧","",translated_source)) and len(assets)==1:
                    kind="formula"
                style={"source_size":size,"font":remaining[0]["font"],"color":f"{remaining[0]['color']:06x}","column":column,
                       "columns":2 if cols["count"]==2 and rect.y0>=cols["start_y"]-2 and rect.y0<cols.get("end_y",float("inf"))-2 and rect.width<page.rect.width*.55 else 1}
                metadata={"source_text":original,"glyph_ids":[g["id"] for g in remaining],
                          "source_block":line["block"],"source_line":line["line"],"recognition":"native-glyphs-babeldoc-structure" if line.get("structure_id") else "heuristic-native",
                          "structure_id":line.get("structure_id"),"structure_class":line.get("structure_class"),"structure_order":line.get("structure_order"),
                          "list_label":exercise_match.group(1) if exercise_match else (subquestion.group(1) if subquestion else None)}
                if section:
                    metadata["heading_level"]=section[1].count(".")+1
                    metadata["chapter_start"]=metadata["heading_level"]==1
                    style["level"]=metadata["heading_level"]
                if len(text)==1 and text.isalpha() and size>body_size*1.8:
                    metadata["drop_cap"]=True
                element=Element(eid,kind,translated_source,number,_rounded(rect),printed,style,assets,
                                exercise_group or eid,chapter,metadata)
                if text in {"◂","▸","■","□","◻","◼","▹"}:
                    element.metadata["role"]="end_marker"
                    if current is not None:
                        element.group_id=current.group_id
                        element.metadata["anchor_id"]=current.id
                leading=next((g for g in remaining if g["c"].strip()),None)
                definition_start=bool(leading and leading["flags"]&16 and ":" in original[:90])
                if definition_start:element.metadata["definition_start"]=True
                # Merge wrapped body lines only inside the original block and
                # same role/column, never across exercise/subquestion starts.
                merge = (current is not None and current.kind==kind and kind in {"paragraph","exercise","caption","code"}
                         and (current.metadata["source_block"]==line["block"] or (kind=="exercise" and exercise_group==current.group_id)) and current.style["column"]==column
                         and not exercise_match and not subquestion
                         and (not definition_start or current.text.endswith("-"))
                         and (min(rect.x1,current.bbox[2])-max(rect.x0,current.bbox[0])>0
                              or (current.metadata["source_block"]==line["block"] and current.metadata["source_line"]==line["line"]
                                  and 0<=rect.x0-current.bbox[2]<size*7
                                  and not page.rect.width*.43<(rect.x0+current.bbox[2])/2<page.rect.width*.56))
                         and rect.y0-current.bbox[3] < size*.85 and rect.y0>=current.bbox[1])
                if merge:
                    element=current
                    if current.metadata.get("drop_cap") and len(current.text)==1 and translated_source[:1].islower():
                        element.text += translated_source
                        element.metadata.pop("drop_cap",None)
                    elif kind=="code":
                        element.text += "\n"+translated_source
                    elif element.text.endswith("-") and translated_source[:1].islower():
                        element.text=element.text[:-1]+translated_source
                    else:
                        element.text += " "+translated_source
                    element.bbox=_rounded(_rect_union([element.bbox,rect]))
                    element.asset_ids.extend(assets)
                    element.metadata["source_text"] += "\n"+original
                    element.metadata["glyph_ids"].extend(g["id"] for g in remaining)
                    for issue in book.issues:
                        if issue.get("element_id")==eid:issue["element_id"]=element.id
                else:
                    page_elements.append(element)
                    current=element
                if rhs_numeric:
                    element.metadata.setdefault("rhs_numeric_continuations",[]).append(rhs_numeric)
                for g in remaining:
                    g.update(owner=element.id,disposition="preserved-original" if kind in {"code","formula","running_header"} else "translation-source")
            # Interleave visuals with their nearest source paragraph/caption and
            # put both under a shared group, allowing compositor affinity rules.
            visuals_e=[e for e in page_elements if e.kind in {"figure","table"}]
            for visual in visuals_e:
                if visual.metadata.get("role")=="exercise_marker":
                    anchor=_exercise_marker_anchor(visual,page_elements)
                    if anchor:
                        visual.group_id=anchor.group_id
                        visual.metadata["anchor_id"]=anchor.id
                        visual.metadata["anchor_method"]="nearest-horizontal-question-with-y-overlap"
                        visual.style["column"]=anchor.style.get("column",0)
                        visual.style["columns"]=anchor.style.get("columns",2)
                    continue
                distance=lambda e:min(abs(e.bbox[3]-visual.bbox[1]),abs(e.bbox[1]-visual.bbox[3]))
                horizontal=lambda e:min(e.bbox[2],visual.bbox[2])-max(e.bbox[0],visual.bbox[0])
                candidates=[e for e in page_elements if e.kind=="caption" and distance(e)<75 and horizontal(e)>min(e.bbox[2]-e.bbox[0],visual.bbox[2]-visual.bbox[0])*.25]
                if candidates:
                    caption=min(candidates,key=distance)
                    caption.group_id=visual.group_id
                    caption.metadata["anchor_id"]=visual.id
                    visual.metadata["caption_id"]=caption.id
                else:
                    _issue(book,"visual_caption_unresolved","No reliable caption association found",number,visual.id)
            # Existing textual order already handles columns. Insert visuals by
            # nearest predecessor without flattening the two-column reading order.
            for visual in visuals_e:
                page_elements.remove(visual)
                candidates=[(i,e) for i,e in enumerate(page_elements) if e.style.get("column",0)==visual.style.get("column",0) and e.bbox[1]<=visual.bbox[1]]
                index=candidates[-1][0]+1 if candidates else 0
                page_elements.insert(index,visual)
            _merge_baseline_elements(book,page_elements,glyphs)
            _merge_wrapped_elements(book,page_elements)
            # A wrapped RHS is a source-layout accident, not a new question.
            # Join just the proven operator/number pair as original vectors at
            # their unchanged sizes/baselines; sentence punctuation stays text.
            from dataclasses import replace
            for element in page_elements:
                for evidence in element.metadata.get("rhs_numeric_continuations",[]):
                    matches=list(TOKEN_RE.finditer(element.text))
                    for i,match in enumerate(matches):
                        numeric=book.assets.get(match[1])
                        if i==0 or numeric is None or numeric.metadata.get("glyph_ids")!=evidence["glyph_ids"]:continue
                        prior=matches[i-1];left=book.assets.get(prior[1])
                        if (left is None or element.text[prior.end():match.start()].strip()
                            or not left.text.rstrip().endswith(evidence["operator"])):continue
                        components=[left,numeric]
                        if any(abs(a.width-(a.bbox[2]-a.bbox[0]))>.001 or abs(a.height-(a.bbox[3]-a.bbox[1]))>.001 for a in components):continue
                        fragment=element.text[prior.start():match.end()]
                        temporary=replace(element,text=fragment,asset_ids=[a.id for a in components])
                        _combine_wrapped_math(book,[temporary],number,outdir)
                        refs=TOKEN_RE.findall(temporary.text)
                        if len(refs)!=1:continue
                        combined=book.assets[refs[0]];x=0;segments=[]
                        for component in components:
                            y=combined.baseline-component.baseline
                            segments.append({"asset_id":component.id,"source_page":number,
                                "bbox":component.bbox,"text":component.text,
                                "target_bbox":[x,y,x+component.width,y+component.height],
                                "translation_only":[x-component.bbox[0],y-component.bbox[1]]})
                            x+=component.width+1
                        combined.metadata["source_segments"]=segments
                        combined.metadata["native_segment_geometry"]="translation-only-recorded-v1"
                        combined.metadata["rhs_numeric_continuation"]=evidence
                        element.text=element.text[:prior.start()]+temporary.text+element.text[match.end():]
                        refs=TOKEN_RE.findall(element.text)
                        old={a.id for a in components}
                        element.asset_ids=list(dict.fromkeys([aid for aid in element.asset_ids if aid not in old or aid in refs]+refs))
                        break
            _combine_wrapped_math(book,page_elements,number,outdir,doc,page)
            _merge_body_continuations(book,page_elements,glyphs,page.rect.width)
            _combine_wrapped_math(book,page_elements,number,outdir,doc,page)
            from .math_regions import assemble_native_regions
            region_proofs=assemble_native_regions(book,doc,page,outdir,page_elements,glyphs,drawings)
            from .native_containers import assemble_native_containers
            region_proofs.extend(assemble_native_containers(book,doc,page,outdir,page_elements,glyphs,drawings))
            if region_proofs:
                affected={eid for proof in region_proofs for eid in proof['owner_ids']}
                for element in page_elements:
                    if element.id not in affected:continue
                    current=[book.assets[aid].text for aid in element.asset_ids if aid in book.assets and book.assets[aid].kind=='formula']
                    if all(t.count('(')==t.count(')') and t.count('[')==t.count(']') for t in current):
                        book.issues=[q for q in book.issues if not(q.get('element_id')==element.id and q['code']=='formula_boundary_review')]
            _promote_formula_prose(page_elements)
            _assemble_display_math(book,doc,page,outdir,page_elements,glyphs)
            frontmatter_role=_repair_page_structure(book,doc,page,outdir,page_elements,lines,glyphs)
            _attach_inline_vector_symbols(book,doc,page,outdir,page_elements,glyphs,drawings)
            _order_exercise_markers(page_elements)
            _check_atomic_formulas(book,page_elements)
            _validate_source_references(book,page_elements,glyphs,number)
            for g in glyphs:
                if g["owner"] is None:
                    g.update(owner="unresolved",disposition="unresolved")
            unresolved=sum(g["disposition"]=="unresolved" for g in glyphs)
            if unresolved:
                _issue(book,"source_glyph_unresolved",f"{unresolved} source glyphs have no content owner",number,severity="error")
            blank_source=False
            if not glyphs:
                pix=page.get_pixmap(matrix=fitz.Matrix(.5,.5),colorspace=fitz.csGRAY,alpha=False)
                blank_source=sum(value<248 for value in pix.samples)<=max(1,len(pix.samples)*.00001)
                if not blank_source:_issue(book,"scanned_page_unsupported","No extractable source glyphs; OCR required before translation",number,severity="error")
            invalid=[g for g in glyphs if "\ufffd" in g["c"] or "\x00" in g["c"]]
            if any(not g.get("asset_id") for g in invalid):
                _issue(book,"source_unicode_invalid","Source character map has replacement/NUL glyphs; vector originals preserved",number,severity="error")
            elif invalid:
                _issue(book,"math_unicode_vector_preserved",f"{len(invalid)} source math characters lack Unicode mapping; immutable vectors preserved, textual math context needs review",number)
            graphics=[]
            active_asset_ids={aid for e in page_elements if not e.metadata.get('render_suppressed') for aid in e.asset_ids}
            page_assets=[book.assets[aid] for aid in active_asset_ids]
            for drawing_index,drawing in enumerate(all_drawings):
                original_dr=fitz.Rect(drawing['source_rect'])
                dr=fitz.Rect(drawing['rect']) & page.rect
                owners=[]
                for asset in page_assets:
                    regions=[s["bbox"] for s in asset.metadata.get("source_segments",[])] or [asset.bbox]
                    if any((fitz.Rect(region)+(-1,-1,1,1)).contains(dr) for region in regions):owners.append(asset.id)
                if drawing.get('fully_source_clipped') or dr.is_empty:
                    disposition="excluded-by-source-clipping"
                    reason="native path has no visible extent after the original PDF clip stack and CropBox"
                elif owners:
                    disposition="preserved-in-asset"
                    reason="original vector drawing covered by immutable asset"
                elif any(e.kind=="table" and e.metadata.get("table_grid") and (fitz.Rect(e.bbox)+(-1,-1,1,1)).contains(dr) for e in page_elements):
                    disposition="recreated-table-grid"
                    reason="structured table renderer rebuilds original row/column rules; source cells separately covered"
                elif dr.width<2 or dr.height<2 or (len(drawing["items"])<5 and drawing.get("fill") is not None):
                    disposition="design-reference"
                    reason="rule/background recorded as template style reference; no linguistic content"
                else:
                    disposition="unresolved"
                    reason="unclassified drawing must be visually reviewed"
                    _issue(book,"source_graphic_unresolved",f"Drawing {drawing_index} at {_rounded(dr)} has no immutable content asset",number,severity="error")
                graphics.append({"id":f"drawing-{drawing_index}","bbox":_rounded(original_dr),
                                 "visible_bbox":_rounded(dr) if not dr.is_empty and not drawing.get('fully_source_clipped') else None,
                                 "source_clip_bbox":drawing.get('source_clip_bbox'),
                                 "source_clipping_evidence":drawing.get('source_clipping_evidence'),
                                 "source_page_clipping":not page.rect.contains(original_dr),"asset_ids":owners,
                                 "disposition":disposition,"reason":reason,"color":drawing.get("color"),"fill":drawing.get("fill"),"width":drawing.get("width")})
            ledger=f"coverage/p{number:04d}.json.gz"
            with gzip.open(outdir/ledger,"wt",encoding="utf-8") as stream:
                json.dump(glyphs,stream,ensure_ascii=False,separators=(",",":"))
            book.pages.append({"number":number,"width":page.rect.width,"height":page.rect.height,"printed_page":printed,
                               "label_source":label_source,"label_confidence":label_confidence,"columns":cols["count"],
                               "column_split":cols["split"],"column_start_y":cols["start_y"] if cols["count"]==2 else None,"column_end_y":cols.get("end_y") if cols.get("end_y",float("inf"))!=float("inf") else None,
                               "coverage":{"total_glyphs":len(glyphs),"unresolved":unresolved,
                               "dispositions":dict(Counter(g["disposition"] for g in glyphs)),"ledger":ledger},
                               "drawings":len(all_drawings),"graphics_coverage":graphics,
                               "parser_backend":babel["backend"] if babel else "native",
                               "structure_alignment":structure_alignment,
                               "babeldoc_alignment":{k:babel["pages"].get(number,{}).get(k) for k in ("glyph_count","unassigned_glyphs","coordinate_transform")} if babel else None,
                               "element_ids":[e.id for e in page_elements]})
            book.elements.extend(page_elements)
            if not printed:
                _issue(book,"printed_page_unresolved","Using explicit physical PDF page label; printed page unavailable or ambiguous",number)
            if frontmatter_role:book.pages[-1].update(page_role="frontmatter",frontmatter_kind=frontmatter_role)
            rail=_chapter_rail(page_elements,page.rect.width)
            if rail:book.pages[-1]["chapter_rail"]=rail
            if blank_source:book.pages[-1].update(page_role="blank",exclusion_reason="No visible source marks at 36dpi grayscale; intentional blank page retained in provenance")
            _finalize_assets(book,number,outdir)
            completed.add(number)
            _checkpoint_page(book,outdir,signature,number)
            if len(completed)%12==0:
                book.profile["analysis_processed_pages"]=len(completed)
                _checkpoint_manifest(book,outdir,signature,completed)
        book.profile["columns"] = 2 if any(p["columns"]==2 for p in book.pages) else 1
        book.profile["analysis_processed_pages"]=len(completed)
        book.profile["analysis_complete"]=len(completed)==len(selected)
        _checkpoint_manifest(book,outdir,signature,completed)
        from .cross_page_math import link_cross_page_formulas
        link_cross_page_formulas(book)
        book.save(outdir/"book.json")
        return book
