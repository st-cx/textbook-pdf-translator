"""Visible bounds for native extensible math glyphs in the pinned MuPDF build.

Some mathematical fonts place a tall delimiter below its text origin. Their
nominal ascender box can clip most of the actual glyph. Render the exact native
glyph ID from the embedded font, without reconstructing a Unicode substitute.
This adapter uses the same MuPDF renderer as the source PDF, at 8 pixels/point.
"""
from __future__ import annotations

import re
import pymupdf as fitz

_TARGET_FONTS = re.compile(r'extensions|(?:stix|cmex|size)[-_ ]?[1-5]', re.I)
_SCALE = 8


def _font_key(name):
    return re.sub(r'[^a-z0-9]', '', name.lower())


def _glyph_ink_box(font, glyph_id, size):
    pix = fitz.mupdf.fz_render_glyph_pixmap(
        font.this, glyph_id, fitz.mupdf.FzMatrix(size*_SCALE, 0, 0, -size*_SCALE, 0, 0),
        fitz.mupdf.FzIrect(-4096, -4096, 4096, 4096), 8)
    if pix.w() == 0 or pix.h() == 0:
        raise ValueError('Native mathematical glyph has no rendered ink')
    return [pix.x()/_SCALE-.15, pix.y()/_SCALE-.15,
            (pix.x()+pix.w())/_SCALE+.15, (pix.y()+pix.h())/_SCALE+.15]


def correct_native_math_boxes(page, glyphs):
    selected = [g for g in glyphs if _TARGET_FONTS.search(g['font']) and g['c'].strip()]
    if not selected:
        return
    doc = page.parent
    if not hasattr(doc, '_textbook_font_geometry'):
        doc._textbook_font_geometry = {'fonts': {}, 'boxes': {}}
    cache = doc._textbook_font_geometry
    fonts = page.get_fonts(full=True)
    traces = {}
    for span in page.get_texttrace():
        if span['wmode'] != 0 or abs(span['dir'][0]-1) > .001 or abs(span['dir'][1]) > .001:
            continue
        for code, gid, origin, _ in span['chars']:
            key = (span['font'][:31], code, round(origin[0], 3), round(origin[1], 3))
            traces.setdefault(key, set()).add((gid, round(span['size'], 5)))
    for glyph in selected:
        try:
            key = (glyph['font'][:31], ord(glyph['c']),
                   round(glyph['origin'][0], 3), round(glyph['origin'][1], 3))
            matches = traces.get(key, set())
            candidates = {entry[0] for entry in fonts
                          if _font_key(entry[3].split('+')[-1]).startswith(_font_key(glyph['font'][:31]))}
            if len(matches) != 1 or len(candidates) != 1:
                raise ValueError('Native glyph/font identity is ambiguous')
            gid, size = next(iter(matches))
            xref = next(iter(candidates))
            if xref not in cache['fonts']:
                data = doc.extract_font(xref)[3]
                if not data:
                    raise ValueError('Mathematical font is not embedded')
                cache['fonts'][xref] = fitz.Font(fontbuffer=data)
            identity = (xref, gid, size)
            if identity not in cache['boxes']:
                cache['boxes'][identity] = _glyph_ink_box(cache['fonts'][xref], gid, size)
            relative = cache['boxes'][identity]
            ink = fitz.Rect(relative) + (glyph['origin'][0], glyph['origin'][1],
                                         glyph['origin'][0], glyph['origin'][1])
            if ink.width > size*8 or ink.height > size*12:
                raise ValueError('Native math glyph exceeds bounded geometry')
            glyph['nominal_bbox'] = glyph['bbox']
            glyph['bbox'] = list(ink)
            glyph['ink_geometry'] = {'method': 'embedded-native-glyph-render-v1', 'font_xref': xref,
                                     'glyph_id': gid, 'pixels_per_point': _SCALE,
                                     'pymupdf_version': fitz.VersionBind}
        except (ValueError, RuntimeError, TypeError, AttributeError, OverflowError) as error:
            glyph['ink_geometry_error'] = type(error).__name__
