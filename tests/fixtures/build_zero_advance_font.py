"""Regenerate the project-authored 1 KB test font; not a system/upstream font.

Only regeneration needs fonttools==4.60.2. Normal tests use the committed tiny
fixture and do not add a fontTools runtime dependency. All outlines below are
original geometric test shapes, distributed under this repository's license.
"""
from pathlib import Path
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen


def main():
    names=['.notdef','a','bar','negation','b','space']
    shapes={'.notdef':[(0,0),(400,0),(400,600),(0,600)],
            'a':[(50,0),(300,0),(300,600),(50,600)],
            'bar':[(180,-50),(230,-50),(230,700),(180,700)],
            'negation':[(-250,-80),(-180,-80),(350,800),(280,800)],
            'b':[(70,0),(330,0),(330,650),(70,650)],'space':[]}
    glyphs={}
    for name,points in shapes.items():
        pen=TTGlyphPen(None)
        if points:
            pen.moveTo(points[0])
            for point in points[1:]:pen.lineTo(point)
            pen.closePath()
        glyphs[name]=pen.glyph()
    builder=FontBuilder(1000,isTTF=True)
    builder.setupGlyphOrder(names)
    builder.setupCharacterMap({97:'a',124:'bar',0x338:'negation',98:'b',32:'space'})
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({'.notdef':(500,0),'a':(400,50),'bar':(400,180),
                                    'negation':(0,-250),'b':(400,70),'space':(250,0)})
    builder.setupHorizontalHeader(ascent=850,descent=-150)
    builder.setupNameTable({'familyName':'Original Zero Advance Fixture','styleName':'Regular',
        'uniqueFontIdentifier':'TextbookPDF-OriginalZeroAdvance-v1',
        'fullName':'Original Zero Advance Fixture','psName':'OriginalZeroAdvanceFixture',
        'copyright':'Project-authored geometric regression fixture; AGPL-3.0-or-later'})
    builder.setupOS2(sTypoAscender=850,sTypoDescender=-150,usWinAscent=850,usWinDescent=150)
    builder.setupPost();builder.setupMaxp()
    builder.font['head'].created=3406620153;builder.font['head'].modified=3406620153
    builder.font.recalcTimestamp=False
    builder.save(Path(__file__).with_name('original-zero-advance.ttf'))


if __name__=='__main__':main()
