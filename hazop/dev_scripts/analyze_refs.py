# -*- coding: utf-8 -*-
"""Standalone analysis of off-page connector patterns in the P&ID ref library.

Merges every PDF in a library folder into one document (filename = sheet id),
runs the same connector extraction as the app, and reports:
  * connectors per sheet (edge, direction, ref)
  * resolution rate (how many refs point to a sheet that exists)
  * reciprocity  (A->B matched by B->A?)
  * edge statistics per direction
Run:  python analyze_refs.py "P&ID ref/Ref från LKAB Demo"
"""
import sys, os, re, io
from collections import defaultdict

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import fitz
from pid_viewer import (_DIALECTS, _detect_dialect, ConnectorAnalyzer,
                        _sheet_ref_variants, _propose_layout)


class Shim:
    """Borrow ConnectorAnalyzer's extraction methods without QThread init."""
    _get_spans         = ConnectorAnalyzer._get_spans
    _find_in_zones     = ConnectorAnalyzer._find_in_zones
    _cluster_spans     = ConnectorAnalyzer._cluster_spans
    _parse_connector   = ConnectorAnalyzer._parse_connector
    _find_arrow_shapes = ConnectorAnalyzer._find_arrow_shapes
    _match_connections = ConnectorAnalyzer._match_connections


def sheet_id_from_filename(fn):
    base = os.path.splitext(os.path.basename(fn))[0]
    base = re.sub(r'[-_][Rr]ev[A-Z0-9]*$', '', base)   # strip -revC suffix
    return base.upper()


def main(folder):
    pdfs = sorted(f for f in os.listdir(folder) if f.lower().endswith('.pdf'))
    print(f"=== {folder} — {len(pdfs)} PDFs ===\n")

    docs = []           # (sheet_id, page)
    sample_texts = []
    merged = fitz.open()
    page_sheet = {}     # pn -> sheet id (from filename)
    for fn in pdfs:
        path = os.path.join(folder, fn)
        try:
            d = fitz.open(path)
        except Exception as e:
            print(f"  ! could not open {fn}: {e}")
            continue
        for p in range(d.page_count):
            merged.insert_pdf(d, from_page=p, to_page=p)
            page_sheet[merged.page_count - 1] = sheet_id_from_filename(fn)
        if len(sample_texts) < 6:
            sample_texts.append(d.load_page(0).get_text("text"))
        d.close()

    dialect = _detect_dialect(sample_texts)
    print(f"Detected dialect: {dialect}  ({_DIALECTS[dialect]['name']})\n")

    shim = Shim()
    shim._dialect = dialect

    # ── also extract title-block sheet number to compare with filename ─────────
    dconf = _DIALECTS[dialect]
    title_sheet = {}
    all_conns = []
    for pn in range(merged.page_count):
        page = merged.load_page(pn)
        pw, ph = float(page.rect.width), float(page.rect.height)
        ta = dconf['title_area']
        ttext = page.get_text("text", clip=fitz.Rect(pw*ta[0], ph*ta[1], pw*ta[2], ph*ta[3]))
        m = dconf['sheet_num_re'].search(ttext)
        if m:
            title_sheet[pn] = m.group(1).upper().strip()
        spans = shim._get_spans(page)
        conns = shim._find_in_zones(spans, pn, pw, ph, ocr_used=False, page=page)
        all_conns.extend(conns)

    # ── sheet lookup from filenames (authoritative) + title block ─────────────
    lookup = {}
    for pn, sid in page_sheet.items():
        for v in _sheet_ref_variants(sid):
            lookup.setdefault(v, pn)
    for pn, sid in title_sheet.items():
        for v in _sheet_ref_variants(sid):
            lookup.setdefault(v, pn)

    def resolve(ref):
        for v in _sheet_ref_variants(ref):
            if v in lookup:
                return lookup[v]
        # suffix fuzzy
        for k, v in lookup.items():
            if k.endswith(ref) or ref.endswith(k):
                return v
        return None

    # ── per-sheet report ───────────────────────────────────────────────────────
    by_page = defaultdict(list)
    for c in all_conns:
        by_page[c['pid_page']].append(c)

    n_res = n_unres = 0
    edges_by_dir = defaultdict(lambda: defaultdict(int))
    arcs = defaultdict(list)   # (from_pn,to_pn) -> [connector]

    print(f"{'sheet':<14} {'titleblk':<12} conns")
    for pn in range(merged.page_count):
        own = page_sheet[pn]
        tb  = title_sheet.get(pn, '-')
        items = []
        for c in sorted(by_page[pn], key=lambda c: (c['edge'], c['y_pdf'])):
            tp  = resolve(c['ref_sheet'])
            ok  = '' if tp is not None else ' ???'
            slf = ' SELF' if tp == pn else ''
            items.append(f"{c['edge'][:1].upper()}/{c['direction'][:3]}->{c['ref_sheet']}{ok}{slf}")
            if tp is None:
                n_unres += 1
            elif tp != pn:
                n_res += 1
                edges_by_dir[c['direction']][c['edge']] += 1
                if c['direction'] == 'in':
                    arcs[(tp, pn)].append(c)
                else:
                    arcs[(pn, tp)].append(c)
        print(f"{own:<14} {tb:<12} {'; '.join(items) if items else '(none)'}")

    print(f"\nResolved: {n_res}   Unresolved: {n_unres}")
    print("\nEdge stats per direction:")
    for d, edges in sorted(edges_by_dir.items()):
        print(f"  {d:<8} {dict(sorted(edges.items()))}")

    # ── reciprocity ────────────────────────────────────────────────────────────
    print("\nReciprocity (A->B vs B->A):")
    pairs = set()
    for (a, b) in arcs:
        pairs.add((min(a, b), max(a, b)))
    n_recip = n_one = 0
    for (a, b) in sorted(pairs):
        fwd = arcs.get((a, b), [])
        rev = arcs.get((b, a), [])
        sa, sb = page_sheet[a], page_sheet[b]
        fo = sum(1 for c in fwd if c['direction'] == 'out')
        fi = sum(1 for c in fwd if c['direction'] == 'in')
        ro = sum(1 for c in rev if c['direction'] == 'out')
        ri = sum(1 for c in rev if c['direction'] == 'in')
        tag = 'RECIP' if (fwd and rev) else 'one-way'
        if fwd and rev: n_recip += 1
        else: n_one += 1
        print(f"  {sa}->{sb}: fwd(out={fo},in={fi}) rev(out={ro},in={ri})  {tag}")
    print(f"\n{n_recip} reciprocal pairs, {n_one} one-way pairs")

    # ── Run the production matcher + layered layout ────────────────────────────
    sheet_for_match = {pn: page_sheet[pn] for pn in page_sheet}
    sheet_for_match.update(title_sheet)
    connections = shim._match_connections(
        all_conns, lookup, merged.page_count,
        {str(k): v for k, v in sheet_for_match.items()})
    n_real  = sum(1 for c in connections if not c['is_ghost'])
    n_ghost = sum(1 for c in connections if c['is_ghost'])
    n_both  = sum(1 for c in connections
                  if not c['is_ghost'] and c['n_out'] and c['n_in'])
    n_bidir = sum(1 for c in connections if c['is_bidirectional'])
    print(f"\n_match_connections: {n_real} real ({n_both} confirmed from both "
          f"ends, {n_bidir} bidirectional), {n_ghost} ghost")

    pws = {pn: merged.load_page(pn).rect.width  for pn in range(merged.page_count)}
    phs = {pn: merged.load_page(pn).rect.height for pn in range(merged.page_count)}
    layout = _propose_layout(connections, list(range(merged.page_count)),
                             pws, phs, 1.0)

    # ── ASCII board map: bucket positions into a coarse grid ──────────────────
    print("\nBoard map (each cell ≈ one sheet position):")
    cw = max(pws.values()) + 650
    ch = max(phs.values()) + 420
    grid = defaultdict(list)
    for pn, (x, y) in layout.items():
        grid[(int(y // ch), int(x // cw))].append(pn)
    rows = sorted({r for r, _ in grid})
    cols_ = sorted({c for _, c in grid})
    for r in rows:
        line = []
        for c in cols_:
            cell = grid.get((r, c), [])
            line.append(','.join(page_sheet[p][-3:] for p in sorted(cell)).ljust(8))
        print('  ' + ' '.join(line).rstrip())

    bw = max(x for x, _ in layout.values()) + cw
    bh = max(y for _, y in layout.values()) + ch
    print(f"\nBoard size: {bw/1000:.0f}k x {bh/1000:.0f}k px "
          f"(aspect {bw/max(bh,1):.1f})")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'P&ID ref/Ref från LKAB Demo')
