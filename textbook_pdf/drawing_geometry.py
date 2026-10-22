"""Native drawing extents with the source PDF's real clipping evidence."""
from __future__ import annotations

import pymupdf as fitz


def source_drawings(page):
    default = page.get_drawings()
    clips_by_path = {}
    stack = []
    for item in page.get_drawings(extended=True):
        level = item.get('level', 0)
        while stack and stack[-1][0] >= level:
            stack.pop()
        if item['type'] == 'clip':
            stack.append((level, fitz.Rect(item['scissor'])))
        elif 'seqno' in item and 'rect' in item:
            clip = fitz.Rect(page.rect)
            for _, bounds in stack:
                clip &= bounds
            key = (item['seqno'], tuple(item['rect']))
            clips_by_path[key] = clip
    result = []
    for index, item in enumerate(default):
        raw = fitz.Rect(item['rect'])
        clip = clips_by_path.get((item['seqno'], tuple(raw)), page.rect)
        # Stroked horizontal/vertical rules have zero-area path bounds; use
        # their actual stroke width before testing clipping, not Rect.is_empty.
        stroke = float(item.get('width') or 0)/2 if 's' in item['type'] else 0
        extent = raw + (-stroke, -stroke, stroke, stroke)
        visible = extent & clip
        entirely_clipped = visible.is_empty
        result.append({**item, 'rect': visible if not entirely_clipped else raw,
                       'source_drawing_index': index, 'source_rect': list(raw),
                       'source_clip_bbox': list(clip), 'fully_source_clipped': entirely_clipped,
                       'source_clipping_evidence': 'MuPDF extended native clip stack and source page CropBox'})
    return result
