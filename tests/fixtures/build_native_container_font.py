"""Regenerate the project-authored tall delimiter regression font.

Only regeneration needs fonttools==4.60.2; normal tests use the committed
fixture. These geometric outlines are original, not textbook/system fonts.
"""
from pathlib import Path
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen

def own_tall_font(path):
    """Original test outlines; no textbook or third-party font is copied."""
    font=FontBuilder(1000,isTTF=True)
    font.setupGlyphOrder(['.notdef','space','left','right'])
    font.setupCharacterMap({32:'space',91:'left',93:'right'})
    glyphs={}
    for name,points in {'.notdef':[], 'space':[],
            'left':[(40,800),(280,800),(280,700),(140,700),(140,-1500),(280,-1500),(280,-1600),(40,-1600)],
            'right':[(20,800),(260,800),(260,-1600),(20,-1600),(20,-1500),(160,-1500),(160,700),(20,700)]}.items():
        pen=TTGlyphPen(None)
        if points:
            pen.moveTo(points[0])
            for point in points[1:]:pen.lineTo(point)
            pen.closePath()
        glyphs[name]=pen.glyph()
    font.setupGlyf(glyphs)
    font.setupHorizontalMetrics({n:(300,0) for n in glyphs})
    font.setupHorizontalHeader(ascent=800,descent=-200)
    font.setupNameTable({'familyName':'STIXMathSize1OwnDemo','styleName':'Regular',
        'uniqueFontIdentifier':'Original native container regression font 1',
        'fullName':'STIXMathSize1OwnDemo','psName':'STIXMathSize1OwnDemo'})
    font.setupOS2(sTypoAscender=800,sTypoDescender=-200,usWinAscent=800,usWinDescent=1600)
    font.setupPost();font.setupMaxp()
    font.font['head'].created=3406620153; font.font['head'].modified=3406620153
    font.font.recalcTimestamp=False
    font.save(path)


if __name__=='__main__':
    own_tall_font(Path(__file__).with_name('original-tall-containers.ttf'))
