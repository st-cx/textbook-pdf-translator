"""Conservative native-line evidence for a numeric RHS at a line wrap.

This does not infer mathematics from a numbered line alone.  The immediately
preceding native row must itself end in a required operator, and both rows must
agree on source block, column, body typography and adjacent text baselines.
"""
from __future__ import annotations

from collections import Counter
import re
from statistics import median


_NUMBER_SENTENCE = re.compile(r"^(\d{1,3})\.\s+(?=[A-Z][a-z])")
_REQUIRED_OPERATOR = re.compile(r"[=+−×÷∧∨→↔]\s*$")
_CODE_FONT = re.compile(r"mono|courier|consolas|typewriter", re.I)


def _column(row, columns):
    rect = row['bbox']
    if (columns.get('count') == 2
            and columns.get('start_y', float('inf')) - 2 <= rect[1]
            < columns.get('end_y', float('inf')) - 2):
        return int((rect[0] + rect[2]) / 2 > columns['split'])
    return 0


def _body_metrics(chars):
    ordinary = [g for g in chars if g['c'].isalpha() and not g.get('flags', 0) & 2]
    if not ordinary:
        return None
    size = median(g['size'] for g in ordinary)
    body = [g for g in ordinary if abs(g['size'] - size) <= .03]
    if not body:
        return None
    font, color = Counter((g['font'], g.get('color', 0)) for g in body).most_common(1)[0][0]
    body = [g for g in body if (g['font'], g.get('color', 0)) == (font, color)]
    return size, median(g['origin'][1] for g in body), font, color


def rhs_numeric_continuation(previous, row, columns):
    """Return verifiable split evidence, or None; never mutate glyphs/rows.

    The number is the only protected operand.  Its following full stop and
    sentence remain translatable prose, rather than an exercise marker.
    """
    if previous is None or previous.get('block') != row.get('block'):
        return None
    if row.get('line') != previous.get('line', -2) + 1:
        return None
    if _column(previous, columns) != _column(row, columns):
        return None
    first, second = previous.get('chars', []), row.get('chars', [])
    if not first or not second or any(g.get('owner') is not None for g in first + second):
        return None
    # An error inside an earlier token is not evidence about the end of a row.
    prior_text = ''.join(g['c'] for g in first)
    match = _NUMBER_SENTENCE.match(''.join(g['c'] for g in second))
    operator = _REQUIRED_OPERATOR.search(prior_text)
    if not match or not operator or not prior_text[:operator.start()].rstrip():
        return None
    if any(_CODE_FONT.search(g['font']) for g in first + second):
        return None
    before, after = _body_metrics(first), _body_metrics(second)
    if before is None or after is None:
        return None
    size, baseline, font, color = before
    if (abs(after[0] - size) > .03 or after[2:] != (font, color)
            or not size * .7 <= after[1] - baseline <= size * 1.7
            or abs(row['bbox'][0] - previous['bbox'][0]) > 2
            or not -size * .2 <= row['bbox'][1] - previous['bbox'][3] <= size * .6):
        return None
    # Glyph offsets, not guessed character widths.  Numeric glyphs, dot and
    # sentence must all use the source body baseline; bold real labels veto.
    end = match.end(1)
    if any(len(g['c']) != 1 for g in second[:end + 1]):
        return None
    prefix = second[:end + 1]
    if (''.join(g['c'] for g in prefix) != match[1] + '.'
            or any(g.get('flags', 0) & (2 | 16) for g in prefix)
            or any(g['font'] != font or g.get('color', 0) != color
                   or abs(g['size'] - size) > .03
                   or abs(g['origin'][1] - after[1]) > .03 for g in prefix)):
        return None
    tail = [g for g in first if g['c'].strip()][-1]
    if tail['c'] != operator[0].strip() or abs(tail['origin'][1] - baseline) > .03:
        return None
    return {'method': 'native-same-block-numeric-rhs-v1',
            'source_block': row['block'],
            'source_lines': [previous['line'], row['line']],
            'column': _column(row, columns), 'number': match[1],
            'numeric_char_end': end, 'glyph_ids': [g['id'] for g in second[:end]],
            'operator': operator[0].strip(),
            'source_baselines': [baseline, after[1]], 'source_size': size}


def annotate_rhs_numeric_continuations(rows, columns):
    """Annotate a copied row only on strict evidence; retain original objects."""
    result = []
    for index, row in enumerate(rows):
        evidence = rhs_numeric_continuation(rows[index - 1] if index else None, row, columns)
        result.append({**row, 'rhs_numeric_continuation': evidence} if evidence else row)
    return result
