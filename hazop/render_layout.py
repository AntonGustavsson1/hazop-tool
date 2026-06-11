# -*- coding: utf-8 -*-
"""Render the proposed board layout as a PNG for visual inspection."""
import sys, os, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import fitz
from analyze_refs import Shim, sheet_id_from_filename
from pid_viewer import _DIALECTS, _detect_dialect, _sheet_ref_variants, _propose_layout


def main(folder, out_png):
    pdfs = sorted(f for f in os.listdir(folder) if f.lower().endswith('.pdf'))
    merged = fitz.open(); page_sheet = {}
    sample_texts = []
    for fn in pdfs:
        d = fitz.open(os.path.join(folder, fn))
        merged.insert_pdf(d, from_page=0, to_page=0)
        page_sheet[merged.page_count - 1] = sheet_id_from_filename(fn)
        if len(sample_texts) < 6:
            sample_texts.append(d.load_page(0).get_text("text"))
        d.close()

    shim = Shim()
    shim._dialect = _detect_dialect(sample_texts)
    all_conns = []
    for pn in range(merged.page_count):
        page = merged.load_page(pn)
        spans = shim._get_spans(page)
        all_conns.extend(shim._find_in_zones(
            spans, pn, page.rect.width, page.rect.height,
            ocr_used=False, page=page))
    lookup = {}
    for pn, sid in page_sheet.items():
        for v in _sheet_ref_variants(sid):
            lookup.setdefault(v, pn)
    conns = shim._match_connections(all_conns, lookup, merged.page_count,
                                    {str(k): v for k, v in page_sheet.items()})
    pws = {pn: merged.load_page(pn).rect.width  for pn in range(merged.page_count)}
    phs = {pn: merged.load_page(pn).rect.height for pn in range(merged.page_count)}
    layout = _propose_layout(conns, list(range(merged.page_count)), pws, phs, 1.0)

    # ── draw ───────────────────────────────────────────────────────────────────
    from PIL import Image, ImageDraw
    S = 0.025   # scene px → image px
    W = int((max(x + pws[p] for p, (x, y) in layout.items()) + 800) * S)
    H = int((max(y + phs[p] for p, (x, y) in layout.items()) + 800) * S)
    img = ImageDraw.Draw(im := Image.new('RGB', (W, H), 'white'))

    def center(p):
        x, y = layout[p]
        return ((x + pws[p] / 2) * S, (y + phs[p] / 2) * S)

    for c in conns:
        a, b = c['from_page'], c['to_page']
        if b is None or a not in layout or b not in layout:
            continue
        xa, ya = center(a); xb, yb = center(b)
        col = (200, 60, 60) if c['is_bidirectional'] else (120, 120, 220)
        img.line([xa, ya, xb, yb], fill=col, width=1)

    for p, (x, y) in layout.items():
        x0, y0 = x * S, y * S
        x1, y1 = (x + pws[p]) * S, (y + phs[p]) * S
        img.rectangle([x0, y0, x1, y1], outline=(40, 40, 40),
                      fill=(245, 245, 235), width=2)
        img.text((x0 + 4, y0 + 4), page_sheet[p][-4:], fill=(0, 0, 0))

    # redraw lines on top half-transparent? keep simple: draw lines again thin
    for c in conns:
        a, b = c['from_page'], c['to_page']
        if b is None or a not in layout or b not in layout:
            continue
        xa, ya = center(a); xb, yb = center(b)
        col = (200, 60, 60) if c['is_bidirectional'] else (90, 90, 210)
        img.line([xa, ya, xb, yb], fill=col, width=1)

    im.save(out_png)
    print(f"saved {out_png}  ({W}x{H})")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'P&ID ref/Ref från LKAB Demo',
         sys.argv[2] if len(sys.argv) > 2 else '_layout.png')
