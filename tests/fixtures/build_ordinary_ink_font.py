"""Original geometric font; no textbook/system/upstream glyph outlines."""
from pathlib import Path
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen


def build(path, *, family='Original Ordinary Ink Fixture', shift=0):
    shapes = {
        '.notdef': [(0, 0), (400, 0), (400, 500), (0, 500)],
        # Negative side bearing, descender and right overshoot deliberately
        # extend beyond advance metrics while remaining valid native glyphs.
        'p': [(-260+shift, -300), (-150+shift, -300), (150+shift, 600), (40+shift, 600)],
        'x': [(-180+shift, -180), (-60+shift, -180), (580+shift, 700), (460+shift, 700)],
        'n': [(40, 20), (300, 20), (300, 480), (40, 480)],
        'arrow': [(20, 200), (280, 200), (200, 280), (230, 310), (380, 160),
                  (230, 10), (200, 40), (280, 120), (20, 120)],
        'space': [],
    }
    glyphs = {}
    for name, points in shapes.items():
        pen = TTGlyphPen(None)
        if points:
            pen.moveTo(points[0])
            for point in points[1:]:
                pen.lineTo(point)
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder(list(shapes))
    builder.setupCharacterMap({112: 'p', 120: 'x', 110: 'n', 0x2192: 'arrow', 32: 'space'})
    builder.setupGlyf(glyphs)
    builder.setupHorizontalMetrics({'.notdef': (400, 0), 'p': (400, -260+shift),
        'x': (400, -180+shift), 'n': (400, 40), 'arrow': (400, 20), 'space': (250, 0)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    postscript = family.replace(' ', '')
    builder.setupNameTable({'familyName': family, 'styleName': 'Italic',
        'uniqueFontIdentifier': postscript+'-'+str(shift), 'fullName': family,
        'psName': postscript, 'copyright': 'Project-authored geometric test fixture'})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200)
    builder.setupPost(italicAngle=-15); builder.setupMaxp()
    builder.font['head'].created = 3406620153
    builder.font['head'].modified = 3406620153
    builder.font.recalcTimestamp = False
    builder.save(path)
    return Path(path)


if __name__ == '__main__':
    build(Path(__file__).with_name('original-ordinary-ink.ttf'))
    build(Path(__file__).with_name('original-ordinary-ink-distinct.ttf'), shift=40)
