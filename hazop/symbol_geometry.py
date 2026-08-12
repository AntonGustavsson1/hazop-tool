"""Geometric analysis of vector-drawn P&ID symbols (valves, instruments, etc.)
extracted from PDF page drawings via PyMuPDF's page.get_drawings().

Pure Python/PyMuPDF — no Qt dependency, so this module can be imported by
equipment_detection.py/pid_viewer.py (for the live app) or by standalone
scripts/tests without pulling in the GUI stack.

Scope: vector-drawn PDFs only. Equipment TYPE (valve vs pump vs instrument)
is not guessed from shape here — that already comes from the tag's prefix
via KNOWN_PREFIXES in equipment_detection.py. This module answers a narrower
question: "is there a discrete drawn symbol near this tag, and if so,
exactly where/what shape is it?" — used to place an accurate marker and
confirm the tag-to-symbol link, not to classify ISA valve sub-types.
"""
import math

import fitz


# ══════════════════════════════════════════════════════════════════════════
# Primitive extraction — flatten get_drawings() into plain (kind, bbox, ...)
# records. A valve "bowtie" is often TWO separate get_drawings() dicts
# (two triangles sharing an apex), so we flatten to individual line/curve/
# rect primitives rather than treating each dict as one shape — clustering
# (below) is what re-merges them into a single symbol.
# ══════════════════════════════════════════════════════════════════════════

def extract_primitives(page):
    """Flatten page.get_drawings() into a flat list of primitive dicts.

    Each primitive: {'kind': 'l'|'c'|'re'|'qu', 'bbox': (x0,y0,x1,y1),
                      'p0': (x,y), 'p1': (x,y), 'closed': bool,
                      'filled': bool, 'width': float, 'source': int}
    'source' is the index of the originating get_drawings() dict — used to
    detect when a cluster merges multiple originally-separate shapes.

    get_drawings() (like get_text()) always reports coordinates in the
    page's UNROTATED mediabox space, never in the rotated space the rest
    of this app treats as "PDF space" (matching page.rect, which is what
    page.get_pixmap() renders and what pid_viewer's pdf_to_scene()/
    scene_to_pdf() assume) — confirmed by rendering a crop at a raw
    get_drawings() coordinate on a real rotated P&ID and finding it did
    not line up with the drawn symbol until page.rotation_matrix was
    applied. Every point is transformed through it here (a no-op identity
    matrix when the page isn't rotated) so callers never have to think
    about page rotation.
    """
    mat = page.rotation_matrix
    prims = []
    for src_idx, d in enumerate(page.get_drawings()):
        items = d.get('items') or []
        closed = bool(d.get('closePath'))
        filled = 'f' in (d.get('type') or '')
        width = d.get('width') or 0.0
        for item in items:
            kind = item[0]
            if kind == 'l':
                p0, p1 = item[1] * mat, item[2] * mat
                prims.append({
                    'kind': 'l', 'bbox': _bbox_of_points([p0, p1]),
                    'p0': (p0.x, p0.y), 'p1': (p1.x, p1.y),
                    'closed': closed, 'filled': filled,
                    'width': width, 'source': src_idx,
                })
            elif kind == 'c':
                pts = [p * mat for p in item[1:5]]
                prims.append({
                    'kind': 'c', 'bbox': _bbox_of_points(pts),
                    'p0': (pts[0].x, pts[0].y), 'p1': (pts[-1].x, pts[-1].y),
                    'closed': closed, 'filled': filled,
                    'width': width, 'source': src_idx,
                })
            elif kind == 're':
                rect = item[1] * mat
                corners = [(rect.x0, rect.y0), (rect.x1, rect.y0),
                           (rect.x1, rect.y1), (rect.x0, rect.y1)]
                prims.append({
                    'kind': 're', 'bbox': (rect.x0, rect.y0, rect.x1, rect.y1),
                    'p0': (rect.x0, rect.y0), 'p1': (rect.x1, rect.y1),
                    'closed': True, 'filled': filled, 'corners': corners,
                    'width': width, 'source': src_idx,
                })
            elif kind == 'qu':
                quad = item[1] * mat
                pts = [quad.ul, quad.ur, quad.lr, quad.ll]
                # Corners are kept in their AS-DRAWN order (ul,ur,lr,ll), not
                # sorted into an axis-aligned rectangle. Some CAD sources
                # (confirmed on real LKAB/Metso, Hybrit and Swerim P&IDs)
                # draw a bow-tie valve body as a single 'qu' primitive whose
                # corners are ordered so that ul-ur and lr-ll are the two
                # crossing diagonal edges of the hourglass silhouette, with
                # ur-lr/ll-ul as its short vertical (or horizontal) sides —
                # a self-intersecting quad, not a simple rectangle. Losing
                # this order (by only keeping the bbox, as before) made such
                # valves indistinguishable from an ordinary axis-aligned
                # rectangle downstream, hiding every bow-tie drawn this way
                # from _prim_is_diagonal/_sample_primitive_points.
                corners = [(pts[0].x, pts[0].y), (pts[1].x, pts[1].y),
                           (pts[2].x, pts[2].y), (pts[3].x, pts[3].y)]
                prims.append({
                    'kind': 'qu', 'bbox': _bbox_of_points(pts),
                    'p0': (pts[0].x, pts[0].y), 'p1': (pts[2].x, pts[2].y),
                    'closed': True, 'filled': filled, 'corners': corners,
                    'width': width, 'source': src_idx,
                })
    return prims


def _bbox_of_points(pts):
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def extract_line_segments(page):
    """Simple (x0,y0,x1,y1) line-segment list for markup-drawing snap.

    Replaces the previously-broken extraction in pid_viewer.py's
    _extract_pdf_lines_for_page, which tested hasattr(path, 'rects') /
    hasattr(path, 'lines') against plain dicts (get_drawings() never
    returns objects with those attributes) and so always returned [].
    Curves are not linearized here — snapping only needs straight edges.
    """
    segments = []
    for prim in extract_primitives(page):
        if prim['kind'] == 'l':
            segments.append((prim['p0'][0], prim['p0'][1], prim['p1'][0], prim['p1'][1]))
        elif prim['kind'] in ('re', 'qu'):
            x0, y0, x1, y1 = prim['bbox']
            segments.append((x0, y0, x1, y0))
            segments.append((x1, y0, x1, y1))
            segments.append((x1, y1, x0, y1))
            segments.append((x0, y1, x0, y0))
    return segments


# ══════════════════════════════════════════════════════════════════════════
# Clustering — connected components over primitive bounding boxes, via a
# coarse spatial grid so lookups stay near-linear on pages with thousands
# of primitives instead of O(n^2) pairwise comparison.
# ══════════════════════════════════════════════════════════════════════════

_CLUSTER_GAP = 3.0   # pt — max gap between primitive bboxes to merge
_GRID_CELL = 20.0    # pt — spatial bucket size for neighbor lookups
_MAX_CELL_DENSITY = 40   # primitives per grid cell — see cluster_primitives.
                         # Tried raising this to 60 (2026-08-06) to fix a
                         # real Swerim instrument bubble (a 32-segment
                         # line-polygon circle, see _polygon_circle_islands)
                         # whose own segments got fragmented across the
                         # density-skip on an unusually dense page (32K+
                         # primitives). Reverted: raising it doesn't just
                         # cost more in cluster_primitives itself — it
                         # also merges many MORE unrelated primitives per
                         # cluster overall, and find_symbol_clusters runs
                         # bowtie_score/pump/instrument island-finding on
                         # every resulting cluster, so a few much-larger
                         # clusters made whole-page detection take 54s
                         # instead of ~1s. See NOTES.md for the follow-up
                         # this needs (a targeted post-merge pass for
                         # fragmented same-symbol pieces specifically,
                         # not a blanket density-limit increase).


def _bbox_expand(bbox, pad):
    x0, y0, x1, y1 = bbox
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


def _pt_seg_dist(p, a, b):
    """Distance from point p to segment ab."""
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_intersect(p1, p2, p3, p4):
    d1, d2 = _orient(p3, p4, p1), _orient(p3, p4, p2)
    d3, d4 = _orient(p1, p2, p3), _orient(p1, p2, p4)
    return (((d1 > 0) != (d2 > 0)) and d1 != 0 and d2 != 0
            and ((d3 > 0) != (d4 > 0)) and d3 != 0 and d4 != 0)


def _seg_seg_dist(a1, a2, b1, b2):
    """Minimum distance between segments a1-a2 and b1-b2 (0 if they touch/cross)."""
    if _segments_intersect(a1, a2, b1, b2):
        return 0.0
    return min(_pt_seg_dist(a1, b1, b2), _pt_seg_dist(a2, b1, b2),
               _pt_seg_dist(b1, a1, a2), _pt_seg_dist(b2, a1, a2))


def _prim_edges(prim):
    """A primitive's shape as a list of (p0, p1) edges — rects/quads expand
    to their 4 sides so a large enclosing rectangle's actual drawn boundary
    (not its bounding box) is what gets measured against other shapes."""
    if prim['kind'] in ('l', 'c'):
        return [(prim['p0'], prim['p1'])]
    x0, y0, x1, y1 = prim['bbox']
    return [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
            ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]


def _prim_gap(a, b):
    """Minimum distance between two primitives' actual drawn edges.

    Deliberately NOT a bbox-to-bbox test: a large enclosing shape (a
    title-block frame, a big vessel outline) has a bbox that spatially
    contains everything inside it, which would wrongly bbox-overlap with —
    and merge into one giant cluster — every small symbol drawn inside that
    frame even though the frame's actual boundary line is far from them.
    """
    best = float('inf')
    for p1, p2 in _prim_edges(a):
        for p3, p4 in _prim_edges(b):
            d = _seg_seg_dist(p1, p2, p3, p4)
            if d < best:
                best = d
                if best <= 0.0:
                    return 0.0
    return best


_LONG_LINE_SCALE = 6.0   # a straight 'l' segment longer than this many
                         # text-heights is a pipe run, not a symbol's own
                         # geometry — see _is_pipe_run_line.


def _is_pipe_run_line(prim, scale):
    """A straight line segment long enough to be a pipe run rather than
    part of an equipment symbol's own geometry (a bow-tie's triangle
    edges, an actuator stem, a drain stub — all well under this length).

    Confirmed necessary on a real LKAB P&ID: a valve is spliced directly
    into its pipe with zero gap (by design — that's what "the valve sits
    on this line" means), and real pipe networks connect end-to-end
    across most of a page via tees/elbows. Without this exclusion,
    cluster_primitives' edge-proximity union-find transitively merges
    every valve into the pipe network it's mounted on — measured on that
    file, one page's entire piping collapsed into a single 330-primitive
    cluster spanning nearly the whole page, hiding 6 of 7 valves behind
    filters meant for compact symbols (aspect/norm_size)."""
    return prim['kind'] == 'l' and _prim_length(prim) > _LONG_LINE_SCALE * max(scale, 1.0)


class _UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, i):
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i, j):
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[ri] = rj


def _grid_bucketed_union_find(primitives, idxs, gap):
    """Union-find over just `idxs` (a subset of `primitives`), connecting
    pairs whose actual drawn edges are within `gap` — same grid-bucketed
    broad-phase technique as cluster_primitives (see its docstring for
    why: an unbucketed O(k^2) pairwise scan is fine for the compact
    handful of primitives a real symbol has, but confirmed on a real
    Hybrit P&ID to take minutes on a single ~3500-line pipe-network
    cluster that isn't a symbol at all. Returns a _UnionFind indexed by
    POSITION in `idxs` (0..len(idxs)-1), not by primitive index — the
    caller maps back via idxs[k].
    """
    k = len(idxs)
    uf = _UnionFind(k)
    grid = {}
    for pos, i in enumerate(idxs):
        x0, y0, x1, y1 = _bbox_expand(primitives[i]['bbox'], gap)
        for gx in range(int(x0 // _GRID_CELL), int(x1 // _GRID_CELL) + 1):
            for gy in range(int(y0 // _GRID_CELL), int(y1 // _GRID_CELL) + 1):
                grid.setdefault((gx, gy), []).append(pos)
    for cell_items in grid.values():
        if len(cell_items) > _MAX_CELL_DENSITY:
            continue
        for a in range(len(cell_items)):
            pa = cell_items[a]
            for b in range(a + 1, len(cell_items)):
                pb = cell_items[b]
                if uf.find(pa) == uf.find(pb):
                    continue
                if _prim_gap(primitives[idxs[pa]], primitives[idxs[pb]]) <= gap:
                    uf.union(pa, pb)
    return uf


_TINY_PRIM_SCALE_FRACTION = 1.0   # a primitive whose own length is under
                                   # this many "reference scale" units is
                                   # noise-sized — see cluster_primitives'
                                   # dense-cell rescue below. Confirmed on
                                   # a real Loket P&ID page: individual
                                   # vector-drawn glyph-stroke clusters
                                   # measured ~1.9pt against a ~2pt
                                   # corrected page scale (right around
                                   # 1x), while every real valve/instrument
                                   # edge sampled was several times the
                                   # page scale or more.


def cluster_primitives(primitives, gap=_CLUSTER_GAP, scale=10.0, rescue_dense_cells=False, pipe_scale=None):
    """Group primitives into connected components by actual edge proximity
    (see _prim_gap — deliberately not bbox proximity).

    The spatial grid below is only a broad-phase filter to shortlist which
    pairs are worth the exact edge-distance check; the grid cell a
    primitive's (gap-expanded) bbox touches has no bearing on whether it
    actually merges with a neighbor — only _prim_gap does.

    Cells denser than _MAX_CELL_DENSITY skip the pairwise check entirely.
    A real equipment symbol is a handful of primitives (a bowtie is 6, a
    circle is 4); a 20pt cell packed with dozens+ primitives is usually a
    hatching/fill-texture or dense title-block-table pattern, not a
    discrete symbol — measured on real P&ID reference files, a single
    such cell can hold 1000+ primitives, which made the O(k^2) pairwise
    edge-distance check take 7-13 seconds on an otherwise ordinary page.
    Primitives left ungrouped by this skip fall out on their own as
    (almost always low-confidence) singleton clusters — classify_cluster
    still runs on them, nothing is silently dropped from the result.

    `rescue_dense_cells` (default False — see find_symbol_clusters for the
    one narrow condition where it's turned on) filters an over-dense cell
    down to its "non-tiny" members (length >= _TINY_PRIM_SCALE_FRACTION *
    scale) instead of skipping it outright, and gives that filtered-down
    set a normal pairwise check if it's itself back under the density cap.
    This exists for a CAD export that draws ALL text as pure vector
    strokes (no embedded/searchable font — confirmed on real Loket/
    Smurfit Kappa/Swerim/NYA P&IDs, up to 68,000+ primitives on a single
    ordinary-sized page): a real valve/instrument symbol can land in the
    SAME 20x20pt cell as a dense patch of glyph-stroke noise, and an
    outright skip there used to fragment that symbol's own few edges into
    singletons too — not just the surrounding noise (confirmed on a real
    Loket page: querying every cluster in a region containing two
    visibly intact bow-tie relief-valve icons and a divided instrument
    bubble returned nothing but n_items=1 singletons).

    This is deliberately NOT the default: verified on a real, normally-
    text-bearing Hybrit P&ID (dense hatching/linework, not glyph noise)
    that turning it on unconditionally can bridge a cell's "non-tiny"
    survivors into an unrelated neighboring shape that the outright skip
    was — by accident, but usefully — keeping apart: a real valve's clean
    26-primitive bow-tie (bowtie_score 0.58) fused into a 74-primitive
    blob spanning a neighboring shape (bowtie_score 0.0, aspect 4.49),
    losing a previously-correct detection. Gating this behind an explicit
    flag that find_symbol_clusters only sets for pages with NO native
    text at all keeps every already-working, text-bearing file's
    clustering (and performance) byte-for-byte identical to before this
    existed, while still recovering the no-text-CAD-export failure mode.

    `scale` (pass dominant_text_size(page), see find_symbol_clusters) sets
    the reference size used for the dense-cell rescue's "tiny" floor (see
    above) — a DIFFERENT concept from `pipe_scale` below despite both
    historically being fed the same single number.

    `pipe_scale` (defaults to `scale` if not given, for backward
    compatibility with every existing caller) sets what counts as a
    "long" pipe-run line for _is_pipe_run_line — such lines are never
    allowed to bridge two primitives into one cluster (see that function
    for why: a valve merges with its own pipe network otherwise). They
    still end up in the returned groups as their own (harmless) cluster,
    same as any other ungrouped primitive.

    These two are DELIBERATELY separate parameters, not one: "how big is
    a single glyph" (what `scale` means on a no-text CAD export page,
    since dominant_text_size falls back to estimating it from vector
    primitives there — see that function) and "how long is too long to
    be part of one symbol assembly rather than a pipe run" describe two
    unrelated physical things that only happen to have been the same
    number historically on a page with real, normal-sized text. Confirmed
    necessary on a real Smurfit Kappa P&ID: feeding the corrected (much
    smaller, glyph-calibrated) no-text scale into the pipe-run threshold
    too shrunk it enough (60pt -> ~30pt) to cut a real valve's own 40pt
    actuator-stem connector, fragmenting a cluster that used to merge
    correctly when the pipe-run threshold stayed at the old, larger,
    glyph-unrelated default. find_symbol_clusters passes the ORIGINAL
    (larger) no-text default for `pipe_scale` specifically, while `scale`
    itself carries the corrected small estimate for norm_size/rescue
    purposes — see its own call site for exactly which value goes where.

    Returns a list of index-lists into `primitives` (one list per cluster).
    """
    n = len(primitives)
    if n == 0:
        return []
    if pipe_scale is None:
        pipe_scale = scale
    uf = _UnionFind(n)
    grid = {}
    for idx, prim in enumerate(primitives):
        x0, y0, x1, y1 = _bbox_expand(prim['bbox'], gap)
        for gx in range(int(x0 // _GRID_CELL), int(x1 // _GRID_CELL) + 1):
            for gy in range(int(y0 // _GRID_CELL), int(y1 // _GRID_CELL) + 1):
                grid.setdefault((gx, gy), []).append(idx)

    tiny_floor = _TINY_PRIM_SCALE_FRACTION * max(scale, 1.0)
    for cell_items in grid.values():
        if len(cell_items) > _MAX_CELL_DENSITY:
            if not rescue_dense_cells:
                continue
            cell_items = [i for i in cell_items if _prim_length(primitives[i]) >= tiny_floor]
            if len(cell_items) < 2 or len(cell_items) > _MAX_CELL_DENSITY:
                continue
        for a in range(len(cell_items)):
            i = cell_items[a]
            for b in range(a + 1, len(cell_items)):
                j = cell_items[b]
                if uf.find(i) == uf.find(j):
                    continue
                if _is_pipe_run_line(primitives[i], pipe_scale) or _is_pipe_run_line(primitives[j], pipe_scale):
                    continue
                if _prim_gap(primitives[i], primitives[j]) <= gap:
                    uf.union(i, j)

    groups = {}
    for idx in range(n):
        root = uf.find(idx)
        groups.setdefault(root, []).append(idx)
    return list(groups.values())


# ══════════════════════════════════════════════════════════════════════════
# Feature extraction + confidence scoring — a coarse "is this a discrete
# symbol, not a pipe run / text / title-block frame" classifier. This is a
# deliberate first pass: thresholds are meant to be tuned once real
# detections have been reviewed in the app's review dialog, not treated as
# final — hence a continuous 0..1 confidence rather than a hard boolean.
# ══════════════════════════════════════════════════════════════════════════

def _dist(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def _prim_length(prim):
    if prim['kind'] in ('l', 'c'):
        return _dist(prim['p0'], prim['p1'])
    x0, y0, x1, y1 = prim['bbox']
    return 2 * ((x1 - x0) + (y1 - y0))   # perimeter for re/qu


_DIAGONAL_TOL_DEG = 12.0   # how far from 0/90/180/270 a line must be to count as diagonal


def _prim_corner_edges(prim):
    """A rect/quad primitive's 4 actual drawn edges (as-drawn corner order,
    closing back to the first corner) — NOT its bounding box perimeter.
    For an ordinary axis-aligned rectangle these are the same thing, but a
    self-intersecting quad (see extract_primitives' 'qu' branch) has real
    edges that cut diagonally across its bbox, which this preserves."""
    corners = prim.get('corners')
    if not corners:
        x0, y0, x1, y1 = prim['bbox']
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return list(zip(corners, corners[1:] + corners[:1]))


def _prim_is_diagonal(prim, tol_deg=_DIAGONAL_TOL_DEG):
    """Does this primitive have an edge that runs neither horizontally nor
    vertically (within tol_deg)? Piping in a P&ID is drawn strictly
    orthogonally by convention — a diagonal edge can only be a symbol's own
    geometry (e.g. a bow-tie valve's triangle edges), never a pipe run, a
    title-block grid line, or an instrument-bubble stem.

    Rects/quads are usually axis-aligned by construction, but some CAD
    sources (confirmed on real LKAB/Metso, Hybrit and Swerim P&IDs) draw a
    bow-tie valve body as a single self-intersecting 'qu' primitive whose
    actual edges ARE diagonal even though its bbox looks like a plain
    rectangle — so rect/quad edges are checked via their real corners
    (_prim_corner_edges), not skipped. Curves are rounded by construction
    and never diagonal."""
    if prim['kind'] == 'l':
        edges = [(prim['p0'], prim['p1'])]
    elif prim['kind'] in ('re', 'qu'):
        edges = _prim_corner_edges(prim)
    else:
        return False
    for (x0, y0), (x1, y1) in edges:
        length = math.hypot(x1 - x0, y1 - y0)
        if length < 1e-6:
            continue
        angle = math.degrees(math.atan2(y1 - y0, x1 - x0)) % 90
        if tol_deg <= angle <= (90 - tol_deg):
            return True
    return False


def cluster_features(primitives, index_group, page_text_scale=10.0):
    """Compute geometric features for one cluster (list of primitive indices
    into `primitives`).

    page_text_scale: the page's dominant font size (pt, see
    dominant_text_size()) — used to normalize absolute sizes so the same
    thresholds work across A4 and A0/A1-format drawings.
    """
    members = [primitives[i] for i in index_group]
    bbox = (
        min(m['bbox'][0] for m in members), min(m['bbox'][1] for m in members),
        max(m['bbox'][2] for m in members), max(m['bbox'][3] for m in members),
    )
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    diag = math.hypot(w, h) or 0.001
    aspect = max(w, h) / max(min(w, h), 0.1)
    has_curve = any(m['kind'] == 'c' for m in members)
    has_closed_or_filled = any(m['closed'] or m['filled'] for m in members)
    has_diagonal = any(_prim_is_diagonal(m) for m in members)
    total_len = sum(_prim_length(m) for m in members)

    scale = max(page_text_scale, 1.0)
    return {
        'bbox': bbox, 'w': w, 'h': h, 'aspect': aspect,
        'n_items': len(members),
        'n_sources': len(set(m['source'] for m in members)),
        'has_curve': has_curve,
        'has_closed_or_filled': has_closed_or_filled,
        'has_diagonal': has_diagonal,
        'fold_ratio': total_len / diag,
        'norm_size': diag / scale,   # symbol size relative to text height
    }


# Weight given to each cluster_features() key in cluster_similarity() —
# the three boolean shape traits (does it have a curve/diagonal/closed
# loop at all) carry the most weight since they're the strongest, least
# noisy signal of "this is geometrically the same KIND of symbol"; the
# three continuous ones refine within that (same-shaped things that are
# a very different size or a very different aspect ratio are probably
# not the same symbol either).
_SIMILARITY_WEIGHTS = {
    'has_curve': 0.20, 'has_diagonal': 0.20, 'has_closed_or_filled': 0.15,
    'aspect': 0.20, 'norm_size': 0.15, 'fold_ratio': 0.10,
}


def cluster_similarity(ref_feats, cand_feats):
    """0..1 similarity between two clusters' cluster_features() dicts —
    used by "Hitta liknande symbol" (2026-08-10, see NOTES.md) to rank
    candidates against a user-picked reference shape.

    Boolean features (has_curve/has_diagonal/has_closed_or_filled)
    contribute their full weight on an exact match, zero otherwise.
    Continuous features (aspect/norm_size/fold_ratio) contribute a
    weight scaled by how close the two values are relative to their own
    magnitude (1.0 - relative_difference, floored at 0) — this makes the
    same absolute gap matter less for two large symbols than for two
    tiny ones, consistent with how norm_size itself is already scale-
    normalized against the page's own text size."""
    score = 0.0
    for key in ('has_curve', 'has_diagonal', 'has_closed_or_filled'):
        if ref_feats[key] == cand_feats[key]:
            score += _SIMILARITY_WEIGHTS[key]
    for key in ('aspect', 'norm_size', 'fold_ratio'):
        a, b = ref_feats[key], cand_feats[key]
        denom = max(a, b, 0.01)
        closeness = max(0.0, 1.0 - abs(a - b) / denom)
        score += _SIMILARITY_WEIGHTS[key] * closeness
    return score


def find_cluster_at_point(clusters, x, y, max_distance=None):
    """Return the cluster (from find_symbol_clusters()'s result list)
    whose bbox contains (x, y), or — if none does — the one whose bbox
    CENTER is nearest, same "click near enough" tolerance the rest of
    the P&ID click-to-place flows already use. Returns None for an
    empty cluster list, or if max_distance is given and the nearest
    cluster's center is further than that.
    """
    if not clusters:
        return None
    for c in clusters:
        x0, y0, x1, y1 = c['bbox']
        if x0 <= x <= x1 and y0 <= y <= y1:
            return c

    def _center_dist(c):
        x0, y0, x1, y1 = c['bbox']
        return math.hypot((x0 + x1) / 2 - x, (y0 + y1) / 2 - y)

    nearest = min(clusters, key=_center_dist)
    if max_distance is not None and _center_dist(nearest) > max_distance:
        return None
    return nearest


def _sample_primitive_points(prim, n=20):
    """Sample n evenly-spaced points along a primitive's extent, used for
    silhouette-profile analysis (bowtie_score). A line/curve's two
    endpoints alone would miss the gradual narrowing along a diagonal
    triangle edge — a bow-tie's edges are exactly such diagonals — so we
    interpolate along it instead of just using p0/p1. Rects/quads sample
    along all 4 of their actual drawn edges (_prim_corner_edges) the same
    way, not just their corners — corners alone would leave every slice
    between them with zero data points, which bowtie_score would misread
    as "pinched to nothing" rather than "no primitive passes through
    here". Using the real corner order (not the bbox perimeter) matters
    for the self-intersecting bow-tie quads described in
    extract_primitives — sampling the bbox instead would flatten the
    hourglass silhouette into a plain rectangle and erase the pinch
    bowtie_score is looking for."""
    if prim['kind'] in ('l', 'c'):
        p0, p1 = prim['p0'], prim['p1']
        return [(p0[0] + (p1[0] - p0[0]) * t / (n - 1),
                  p0[1] + (p1[1] - p0[1]) * t / (n - 1)) for t in range(n)]
    pts = []
    for (ax, ay), (bx, by) in _prim_corner_edges(prim):
        pts.extend((ax + (bx - ax) * t / (n - 1), ay + (by - ay) * t / (n - 1))
                   for t in range(n))
    return pts


def _pinch_profile_score(pts, axis, n_slices):
    """Score how much a point cloud's perpendicular spread narrows at the
    center of the given axis ('x' or 'y') relative to its two far ends."""
    vals = [p[0] for p in pts] if axis == 'x' else [p[1] for p in pts]
    v0, v1 = min(vals), max(vals)
    span = v1 - v0
    if span < 1e-6:
        return 0.0

    slice_spreads = []
    for i in range(n_slices):
        lo = span * i / n_slices
        hi = span * (i + 1) / n_slices
        if axis == 'x':
            perp = [p[1] for p in pts if lo <= (p[0] - v0) <= hi]
        else:
            perp = [p[0] for p in pts if lo <= (p[1] - v0) <= hi]
        slice_spreads.append(max(perp) - min(perp) if len(perp) >= 2 else 0.0)

    mx = max(slice_spreads)
    if mx < 1e-6:
        return 0.0
    norm = [s / mx for s in slice_spreads]

    # Both far ends should be relatively "open" (wide)...
    edge_avg = (sum(norm[:2]) + sum(norm[-2:])) / 4.0
    # ...and the center should be relatively "pinched" (narrow).
    mid = n_slices // 2
    mid_slice = norm[mid - 1:mid + 2]
    mid_val = sum(mid_slice) / len(mid_slice)

    if edge_avg < 0.5:
        return 0.0   # ends aren't wide enough to read as an hourglass at all
    return max(0.0, min(1.0, (edge_avg - mid_val) / edge_avg))


def bowtie_score(primitives, index_group, n_slices=9):
    """Heuristic 0..1 score for "this cluster's silhouette is wide at both
    ends and pinches to a narrow waist near its center" — the signature of
    a bow-tie/hourglass valve symbol, whether drawn as two triangles
    meeting at an apex (two source paths) or as one single pinched
    polygon (one source path).

    Tries both axes and keeps the higher score — a bow-tie sitting on a
    vertical pipe run has a square-ish bounding box just like one on a
    horizontal run, so the bbox aspect ratio alone can't tell you which
    axis the pinch is actually along.
    """
    members = [primitives[i] for i in index_group]
    pts = []
    for m in members:
        pts.extend(_sample_primitive_points(m))
    if len(pts) < 4:
        return 0.0
    return max(_pinch_profile_score(pts, 'x', n_slices),
               _pinch_profile_score(pts, 'y', n_slices))


def _curve_islands(primitives, group, gap=_CLUSTER_GAP):
    """Split a cluster's 'c' (curve) primitives into their own maximal
    connected sub-groups, by real edge proximity among JUST the curve
    primitives — independent of the cluster's own (looser) bridging.

    A single cluster can contain several stacked circles/ovals: e.g. an
    instrument-bubble (SC/FC/...) + a motor circle ("M") + a pump's own
    circular body, connected top-to-bottom by short vertical lines into
    ONE cluster (confirmed on a real LKAB P&ID). Each circle is its own
    'island' here — a pump's diagonal impeller mark belongs to ITS
    circle specifically, not to a neighboring instrument bubble one
    line-segment away in the same vertical stack.
    """
    curve_idxs = [i for i in group if primitives[i]['kind'] == 'c']
    n = len(curve_idxs)
    if n == 0:
        return []
    uf = _UnionFind(n)
    for a in range(n):
        for b in range(a + 1, n):
            if _prim_gap(primitives[curve_idxs[a]], primitives[curve_idxs[b]]) <= gap:
                uf.union(a, b)
    islands = {}
    for idx in range(n):
        root = uf.find(idx)
        islands.setdefault(root, []).append(curve_idxs[idx])
    return list(islands.values())


_POLYGON_CIRCLE_MIN_SIDES = 8   # fewer straight segments than this reads
                                 # as an intentional polygon (a valve
                                 # triangle, a title-block rectangle),
                                 # not an approximated circle.
_POLYGON_CIRCLE_MAX_RADIUS_CV = 0.15   # coefficient of variation (std/
                                        # mean) of every vertex's distance
                                        # from the shape's own centroid —
                                        # a real circle-approximating
                                        # polygon is tight (measured
                                        # 0.0038 on a real Swerim
                                        # instrument bubble's 32-gon);
                                        # 0.15 stays far more generous
                                        # than that measurement while
                                        # still ruling out a shape that's
                                        # only vaguely round.


def _polygon_circle_islands(primitives, group, gap=_CLUSTER_GAP):
    """Split a cluster's 'l' (straight line) primitives into their own
    maximal connected sub-groups (same technique as _curve_islands, but
    for lines) and keep only the ones that trace a closed loop
    approximating a circle — many short, roughly equal-radius segments,
    not a triangle, rectangle, or other few-sided polygon.

    Confirmed necessary on real Sunpine and Swerim P&IDs: both draw
    EVERY circular symbol (pump bodies, instrument bubbles) as a many-
    sided line polygon instead of true bezier curves — extract_primitives
    found zero 'c' kind primitives anywhere on multiple sampled pages
    from each file, meaning _curve_islands (which only looks at 'c'
    kind) finds nothing there at all, regardless of how many real pumps
    or instruments the page actually shows. A real instrument bubble on
    a Swerim page measured as a 32-segment closed polygon with a
    vertex-distance-from-centroid coefficient of variation of 0.0038 —
    about 40x tighter than _POLYGON_CIRCLE_MAX_RADIUS_CV allows.

    Returns islands in the SAME format as _curve_islands (a list of
    index-lists) so pump_shapes_in_cluster/instrument_shapes_in_cluster
    can treat the two interchangeably — see _circle_islands.
    """
    line_idxs = [i for i in group if primitives[i]['kind'] == 'l']
    n = len(line_idxs)
    if n < _POLYGON_CIRCLE_MIN_SIDES:
        return []
    uf = _grid_bucketed_union_find(primitives, line_idxs, gap)
    raw_islands = {}
    for idx in range(n):
        root = uf.find(idx)
        raw_islands.setdefault(root, []).append(line_idxs[idx])

    islands = []
    for island in raw_islands.values():
        if len(island) < _POLYGON_CIRCLE_MIN_SIDES:
            continue
        # A circle's own boundary segments are all similar in length. A
        # decorative line that happens to touch/bridge into the same
        # connected component — an internal cross-tick mark, confirmed
        # on a real Swerim instrument bubble, or a divider/connector
        # line bridging TWO separate circles into one capsule — is
        # usually a visibly different length and would otherwise skew
        # the circularity check below (its points sit much closer to,
        # or much farther from, the centroid than the boundary's own
        # points do). Keep only segments within 2.5x of the component's
        # own median length before testing shape.
        lengths = sorted(_prim_length(primitives[i]) for i in island)
        median_len = lengths[len(lengths) // 2]
        filtered = [i for i in island
                    if median_len > 0 and median_len / 2.5 <= _prim_length(primitives[i]) <= median_len * 2.5]
        if len(filtered) < _POLYGON_CIRCLE_MIN_SIDES:
            continue
        # Removing those outlier-length lines can SPLIT what was one
        # connected component into several — e.g. a divider line
        # bridging two circles into a capsule, confirmed on a real
        # Swerim-style instrument bubble. Re-check connectivity among
        # just the survivors rather than assuming they're still one
        # shape; each resulting piece is tested independently below.
        sub_uf = _grid_bucketed_union_find(primitives, filtered, gap)
        sub_islands = {}
        for idx in range(len(filtered)):
            root = sub_uf.find(idx)
            sub_islands.setdefault(root, []).append(filtered[idx])

        for sub_island in sub_islands.values():
            if len(sub_island) < _POLYGON_CIRCLE_MIN_SIDES:
                continue
            if not _has_closed_loop(primitives, sub_island):
                continue
            pts = []
            for i in sub_island:
                pts.append(primitives[i]['p0'])
                pts.append(primitives[i]['p1'])
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            dists = [math.hypot(p[0] - cx, p[1] - cy) for p in pts]
            mean_d = sum(dists) / len(dists)
            if mean_d < 1e-6:
                continue
            std_d = (sum((d - mean_d) ** 2 for d in dists) / len(dists)) ** 0.5
            if (std_d / mean_d) <= _POLYGON_CIRCLE_MAX_RADIUS_CV:
                islands.append(sub_island)
    return islands


def _circle_islands(primitives, group, try_polygon_circles=True):
    """Every round, closed body in this cluster, however it's drawn —
    the union of _curve_islands (bezier-curve circles, the LKAB/Gryaab/
    ITS convention) and _polygon_circle_islands (many-sided line-
    polygon circles, the Sunpine/Swerim convention). pump_shapes_in_cluster
    and instrument_shapes_in_cluster both use this so neither cares
    which convention a given file happens to use.

    `try_polygon_circles=False` skips the (comparatively expensive,
    O(k^2) per cluster) line-polygon search entirely. find_symbol_clusters
    sets this once per PAGE, not per cluster: every real file sampled
    uses one convention consistently across a whole page (curves
    everywhere, or zero curves anywhere), never a mix — confirmed
    necessary after this search made a busy real Gryaab page (which
    uses curves, not line-polygons, for its own circles) take 5+
    seconds just running the line-polygon search on clusters that were
    never going to contain one."""
    islands = _curve_islands(primitives, group)
    if try_polygon_circles:
        islands += _polygon_circle_islands(primitives, group)
    return islands


_PUMP_ISLAND_ASPECT_LIMIT = 1.8   # a pump/instrument body is round, not
                                  # elongated — well under the generic
                                  # compact-symbol ceiling used elsewhere
                                  # (_CORE_ASPECT_LIMIT=3.0), since a
                                  # genuine circle's aspect is ~1.0.

_PUMP_DIAGONAL_MIN_FRACTION = 0.4   # an impeller diagonal must span at
                                    # least this fraction of the circle's
                                    # own (smaller) dimension — see
                                    # pump_shapes_in_cluster's docstring.


def pump_shapes_in_cluster(primitives, index_group, islands=None):
    """Find every round, closed body (an instrument bubble, a motor, or
    a pump shell — all drawn as a closed curve) within this cluster that
    has at least one diagonal line strictly inside it — the signature of
    a centrifugal pump's impeller mark (confirmed by direct inspection of
    real LKAB and Gryaab P&IDs: a circle with two diagonal lines meeting
    at a point on its rim, drawn as a SEPARATE 'l'-only sub-path sitting
    entirely inside the circle's own bbox).

    Unlike bowtie_score (scored over the WHOLE cluster's pooled point
    cloud), this is scored per circle 'island' (_curve_islands) —
    confirmed necessary on a real LKAB P&ID: a pump's own circle is
    routinely merged, via short connecting lines, into a taller vertical
    stack with an instrument bubble above it and a motor circle in
    between (aspect ~2.25 for the whole stack — still well under the
    generic compact-symbol ceiling, so nothing about the STACK's own
    shape flags it as "not one compact thing"). Scoring each circle on
    its own sidesteps that: a circle's own aspect is ~1.0 regardless of
    what else got merged above or below it.

    Returns a LIST of bboxes (one per qualifying circle), not just the
    single best one — confirmed necessary on a real Gryaab P&ID, where
    an entire process area (no long pipe run to break the chain — see
    _is_pipe_run_line) merged into one 290-primitive cluster containing
    TWO separate pumps; a single best-match result would have silently
    dropped the second one. Empty list if none qualify.

    `islands`, if given, is used as-is instead of recomputing
    _circle_islands — find_symbol_clusters computes it once and shares
    it with instrument_shapes_in_cluster, since both would otherwise
    redundantly repeat the same (non-trivial) island-finding work on
    every single cluster on a page.
    """
    if islands is None:
        islands = _circle_islands(primitives, index_group)
    results = []
    for island in islands:
        if not _has_closed_loop(primitives, island):
            continue
        bbox = _group_bbox(primitives, island)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if min(w, h) < 1e-6:
            continue
        aspect = max(w, h) / min(w, h)
        if aspect > _PUMP_ISLAND_ASPECT_LIMIT:
            continue
        if _island_has_inner_diagonal(primitives, island, index_group, bbox):
            results.append(bbox)
    return results


def _island_has_inner_diagonal(primitives, island, index_group, bbox):
    """Is there a diagonal 'l' primitive, elsewhere in index_group,
    strictly inside `bbox` (an island's own bbox) and spanning a real
    fraction of its smaller dimension? Shared by pump_shapes_in_cluster
    (the check itself) and instrument_shapes_in_cluster (which uses it
    the other way round, to EXCLUDE a pump's circle from also counting
    as an instrument body — see that function's docstring for why the
    two are otherwise ambiguous on a real Gryaab P&ID).

    The diagonal-length requirement (>= _PUMP_DIAGONAL_MIN_FRACTION of
    the island's smaller dimension) matters here too: a genuine impeller
    mark reaches from near one rim to near the opposite rim (confirmed
    on a real LKAB pump: two 30.1pt diagonals inside a 42.5pt circle,
    ~71%). Without it, a vector-drawn "M" motor-label glyph (see
    ClosedShapeFilterTests) sitting inside an unrelated plain motor
    circle produces tiny sub-1pt diagonal fragments from its own
    letterform that otherwise satisfy "diagonal line inside a circle"
    too — confirmed as a false positive on a real Gryaab P&ID.
    """
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    island_set = set(island)
    tol = 1.0
    min_len = _PUMP_DIAGONAL_MIN_FRACTION * min(w, h)
    return any(
        primitives[i]['kind'] == 'l' and _prim_is_diagonal(primitives[i])
        and _prim_length(primitives[i]) >= min_len
        and primitives[i]['bbox'][0] >= bbox[0] - tol
        and primitives[i]['bbox'][1] >= bbox[1] - tol
        and primitives[i]['bbox'][2] <= bbox[2] + tol
        and primitives[i]['bbox'][3] <= bbox[3] + tol
        for i in index_group if i not in island_set
    )


_INSTRUMENT_ASPECT_RANGE = (1.0, 3.2)   # a plain circle (~1.0) up to an
                                         # elongated capsule/"stadium"
                                         # body (~2.0 measured on a real
                                         # LKAB LC/PI instrument bubble).
_INSTRUMENT_DIVIDER_MIN_SPAN_FRACTION = 0.7   # a divider must span most
                                               # of the body's own width
                                               # (measured: spans exactly
                                               # the full width on a real
                                               # LKAB instrument bubble).
_INSTRUMENT_DIVIDER_MIDLINE_TOLERANCE = 0.2   # a divider must sit within
                                               # this fraction of the
                                               # body's own height from
                                               # the vertical midpoint —
                                               # see docstring for why
                                               # this is needed at all.


def _island_touching_point(primitives, islands, point, tol=_CLUSTER_GAP):
    """Which island (if any) has a member primitive within `tol` of
    `point`? Returns the island (a list of indices) or None."""
    for island in islands:
        for i in island:
            x0, y0, x1, y1 = primitives[i]['bbox']
            if x0 - tol <= point[0] <= x1 + tol and y0 - tol <= point[1] <= y1 + tol:
                return island
    return None


def instrument_shapes_in_cluster(primitives, index_group, islands=None):
    """Find every instrument "bubble" body within this cluster — a
    circle or elongated capsule/"stadium" shape (two semicircle end-caps
    joined by straight top/bottom edges — confirmed by direct inspection
    of a real LKAB P&ID) with a horizontal divider line INSIDE it, at
    its vertical midpoint — the ISA-5.1 convention for a shared-display
    (panel-mounted) instrument, as opposed to a plain undivided bubble
    (field-mounted) which this deliberately does NOT try to detect (see
    below for why).

    A capsule's own two curve end-caps are typically NOT within
    _CLUSTER_GAP of each other directly (measured on a real LKAB
    instrument: 28.3pt apart) — they're only joined via straight top/
    bottom connecting lines, unlike a pump's single continuous circle
    (_curve_islands, which only considers curve-to-curve proximity,
    would report them as two SEPARATE islands). So this walks every
    roughly-horizontal line in the cluster and checks whether its two
    endpoints each touch a curve island — if both ends do, the
    instrument body's bbox is the union of that line + both islands
    (or, for a plain circle bisected by one line, both ends touch the
    SAME single island).

    Excluding a capsule's own top/bottom outline edges is essential, not
    optional: those edges connect the exact same two islands, span the
    exact same full width, and would otherwise register as a "divider"
    on every capsule regardless of whether it actually has one —
    confirmed on a real LKAB P&ID, where an UNDIVIDED "PI" bubble (a
    field-mounted instrument, no divider at all) has the identical
    two-cap-plus-top/bottom-edges construction as a divided one, only
    missing the middle line. _INSTRUMENT_DIVIDER_MIDLINE_TOLERANCE
    requires the candidate line to sit near the body's own vertical
    midpoint, which the top/bottom outline edges — sitting at the two
    extremes by construction — never do.

    Also excludes a single circle that qualifies as a PUMP
    (pump_shapes_in_cluster/_island_has_inner_diagonal) — confirmed
    necessary on a real Gryaab P&ID: a pump's connecting pipe is drawn
    as one continuous horizontal line straight through the circle's
    own vertical midpoint (rather than stopping at its rim), which is
    otherwise geometrically indistinguishable from a genuine instrument
    divider at that same midpoint. A pump's own diagonal impeller mark
    is the tell — no real instrument bubble has one.

    Returns a list of bboxes (one per qualifying instrument body).

    `islands`, if given, is used as-is instead of recomputing
    _circle_islands — see pump_shapes_in_cluster's matching parameter;
    find_symbol_clusters computes it once and shares it between both.
    """
    if islands is None:
        islands = _circle_islands(primitives, index_group)
    if not islands:
        return []
    island_members = {i for island in islands for i in island}
    results = []
    seen_pairs = set()
    for i in index_group:
        if i in island_members:
            continue   # a circle's own boundary segment, never a divider candidate
        prim = primitives[i]
        if prim['kind'] != 'l':
            continue
        (px0, py0), (px1, py1) = prim['p0'], prim['p1']
        if abs(py0 - py1) > 1.0:
            continue   # not horizontal enough to be a divider
        length = abs(px1 - px0)
        if length < 1e-6:
            continue
        left_pt = (min(px0, px1), (py0 + py1) / 2)
        right_pt = (max(px0, px1), (py0 + py1) / 2)
        left_island = _island_touching_point(primitives, islands, left_pt)
        right_island = _island_touching_point(primitives, islands, right_pt)
        if left_island is None or right_island is None:
            continue
        pair_key = (id(left_island), id(right_island))
        if pair_key in seen_pairs:
            continue
        if left_island is right_island:
            if _has_closed_loop(primitives, left_island) and _island_has_inner_diagonal(
                    primitives, left_island, index_group, _group_bbox(primitives, left_island)):
                continue   # a pump's circle, not an instrument bubble
            body_bbox = _group_bbox(primitives, left_island)
        else:
            lb, rb = _group_bbox(primitives, left_island), _group_bbox(primitives, right_island)
            body_bbox = (min(lb[0], rb[0], prim['bbox'][0]), min(lb[1], rb[1], prim['bbox'][1]),
                         max(lb[2], rb[2], prim['bbox'][2]), max(lb[3], rb[3], prim['bbox'][3]))
        w, h = body_bbox[2] - body_bbox[0], body_bbox[3] - body_bbox[1]
        if min(w, h) < 1e-6:
            continue
        aspect = max(w, h) / min(w, h)
        if not (_INSTRUMENT_ASPECT_RANGE[0] <= aspect <= _INSTRUMENT_ASPECT_RANGE[1]):
            continue
        if length / w < _INSTRUMENT_DIVIDER_MIN_SPAN_FRACTION:
            continue
        midline = (body_bbox[1] + body_bbox[3]) / 2
        divider_y = (py0 + py1) / 2
        if abs(divider_y - midline) > _INSTRUMENT_DIVIDER_MIDLINE_TOLERANCE * h:
            continue
        seen_pairs.add(pair_key)
        results.append(body_bbox)
    return results


def classify_cluster(features):
    """First-pass threshold+weighted-score classifier. Returns a confidence
    in [0, 1] that this cluster is a discrete equipment symbol (as opposed
    to a pipe run, stray text artifact, or title-block/frame rectangle).
    """
    aspect = features['aspect']
    norm_size = features['norm_size']
    n_items = features['n_items']

    # Hard excludes: long/thin (pipe run) or far larger than a symbol
    # relative to the page's own text size (title block / drawing frame).
    if aspect > 3.0:
        return 0.0
    if norm_size > 40.0:
        return 0.0
    if n_items == 1 and not features['has_curve'] and not features['has_closed_or_filled']:
        return 0.0   # a single bare straight segment

    score = 0.0
    if 0.35 <= aspect <= 2.8:            # roughly square/compact
        score += 0.35
    if 1.5 <= norm_size <= 15.0:         # plausible symbol-size band
        score += 0.25
    if features['has_curve']:            # circle/instrument-bubble signal
        score += 0.20
    if features['has_closed_or_filled']:  # pipes are almost always stroke-only
        score += 0.15
    if n_items >= 2 or features['n_sources'] >= 2:
        score += 0.15
    if features['fold_ratio'] > 1.3:     # folded shape (e.g. bowtie), not a straight run
        score += 0.10

    return min(1.0, score)


_NO_TEXT_SCALE_DEFAULT = 10.0   # absolute last-resort default — only used
                                # when a page has NEITHER native text NOR
                                # any vector primitives to estimate a scale
                                # from at all (a genuinely blank page).


def dominant_text_size(page, primitives=None):
    """Median span font size on the page — used to normalize symbol sizes
    across differently-scaled drawings (A4 vs A0/A1).

    Some real CAD exports (confirmed on Loket, Smurfit Kappa, and one
    Swerim and one NYA reference P&ID) draw ALL text — tags, labels,
    everything — as pure vector strokes via a non-embedded/SHX-style font
    instead of real searchable text objects: page.get_text() finds ZERO
    words/spans anywhere on the page despite tens of thousands of vector
    primitives (a real Loket page measured 54,396 primitives against 0
    text spans). The old hardcoded 10.0pt fallback badly overestimates
    such a page's real reference scale — measured on that same real Loket
    page: its own small-glyph-stroke clusters sit around ~1.9pt, a >5x
    gap — which shrinks every genuine symbol's norm_size by the same
    factor and was confirmed (by directly running find_symbol_clusters
    against the file) to make classify_cluster/find_valve_shapes reject
    real, correctly-shaped valve symbols outright (a visually obvious
    bow-tie relief-valve pair next to tag "1-RV-25" scored well under the
    1.5 norm_size floor for exactly this reason).

    When no native text is found, falls back to estimating the scale from
    the page's own vector primitives instead (see
    _estimate_scale_from_primitives) rather than the blind default.
    `primitives`, if given, is used as-is instead of re-extracting via a
    second get_drawings() call — find_symbol_clusters already extracts
    them before calling this and can pass the same list.
    """
    sizes = []
    try:
        d = page.get_text("dict")
        for block in d.get('blocks', []):
            for line in block.get('lines', []):
                for span in line.get('spans', []):
                    sz = span.get('size')
                    if sz:
                        sizes.append(sz)
    except Exception:
        pass
    if not sizes:
        prims = primitives if primitives is not None else extract_primitives(page)
        return _estimate_scale_from_primitives(prims)
    sizes.sort()
    return sizes[len(sizes) // 2]


_SCALE_ESTIMATE_MIN_ITEMS = 2    # a lone unmerged primitive isn't a whole
                                 # glyph; see _estimate_scale_from_primitives.
_SCALE_ESTIMATE_MAX_ITEMS = 20   # generous ceiling on "one glyph's worth of
                                 # strokes" — well above what a single
                                 # character needs, but far below a real
                                 # merged word/label or a large assembly.
_SCALE_ESTIMATE_MIN_SAMPLES = 15   # need this many small clusters before
                                    # trusting their median as "one glyph's
                                    # size" — a sparse page (a handful of
                                    # real symbols and nothing else, e.g. a
                                    # small synthetic fixture, or a real
                                    # page with little text) has too few
                                    # small clusters for the sample to mean
                                    # anything, and they'd likely BE the
                                    # real symbols themselves rather than
                                    # glyph noise — confirmed necessary
                                    # after this estimate, unguarded, self-
                                    # referentially sized a lone test
                                    # valve's own cluster as "the" glyph
                                    # size, always failing its own
                                    # norm_size floor. A real glyph-heavy
                                    # no-text CAD export page has hundreds
                                    # of qualifying small clusters (596 on
                                    # a real Loket page), so this floor
                                    # only ever falls back to the rougher
                                    # bootstrap estimate on genuinely
                                    # sparse pages.


def _estimate_scale_from_primitives(primitives):
    """Reference-scale estimate for a page with no native text at all —
    see dominant_text_size's docstring for why the blind 10.0pt default
    is wrong for such pages.

    A single vector-outlined character is drawn as SEVERAL short strokes
    (a "V" is 2 lines, an "8" or "0" several curve/line segments), so the
    median length of a page's INDIVIDUAL primitives underestimates a
    whole glyph's own size by several times — confirmed on a real Loket
    page: median individual-primitive length ~0.43-1.0pt vs. a directly-
    measured individual glyph-CLUSTER height of ~1.9-4pt. Using the
    smaller, stroke-level number as the page's reference scale was
    confirmed to under-correct: single vector-outlined characters (not
    genuine symbols) still tested as plausibly "symbol-sized" downstream,
    surfacing real false positives in find_valve_shapes on that same file
    (e.g. the vector-outlined tag label "V-102" itself scoring as a
    bow-tie).

    So this is a two-stage bootstrap: first estimate a rough (deliberately
    undershooting) scale from individual primitive lengths, use THAT to
    run one real clustering pass (with the dense-cell rescue — see
    cluster_primitives — enabled, since that's what's needed to merge
    individual glyphs into their own small clusters at all on these
    pages), then take the median bbox diagonal of the resulting small
    (2-20 primitive) clusters as the final estimate — those clusters are
    overwhelmingly individual glyphs on a text-heavy page like this
    (vastly outnumbering the handful of primitives a real valve/pump/
    instrument symbol has), so their own median size approximates "one
    glyph" much more directly than individual stroke length does.
    """
    lengths = [_prim_length(p) for p in primitives if p['kind'] in ('l', 'c')]
    lengths = [l for l in lengths if l > 0.01]
    if not lengths:
        return _NO_TEXT_SCALE_DEFAULT
    lengths.sort()
    bootstrap_scale = max(lengths[len(lengths) // 2], 1.0)

    groups = cluster_primitives(primitives, scale=bootstrap_scale, rescue_dense_cells=True)
    diags = [_bbox_diag(_group_bbox(primitives, g)) for g in groups
              if _SCALE_ESTIMATE_MIN_ITEMS <= len(g) <= _SCALE_ESTIMATE_MAX_ITEMS]
    if len(diags) < _SCALE_ESTIMATE_MIN_SAMPLES:
        return bootstrap_scale
    diags.sort()
    return max(diags[len(diags) // 2], 1.0)


def _group_bbox(primitives, group):
    return (
        min(primitives[i]['bbox'][0] for i in group),
        min(primitives[i]['bbox'][1] for i in group),
        max(primitives[i]['bbox'][2] for i in group),
        max(primitives[i]['bbox'][3] for i in group),
    )


def _bbox_diag(bbox):
    return math.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1])


_CORE_ASPECT_LIMIT = 3.0   # same compact-symbol ceiling classify_cluster
                           # already uses for aspect — a core only grows
                           # outward while it still looks like one.

_CORE_PINCH_GUARD_MIN = 0.5    # only veto a growth step on pinch-signal
                                # grounds if the core already reads as a
                                # decent bow-tie (>= this score) — leaves
                                # generic, non-valve compact-symbol growth
                                # untouched.
_CORE_PINCH_GUARD_DROP = 0.3   # how much bowtie_score is allowed to fall
                                # in one growth step before it's treated
                                # as "this addition broke the hourglass",
                                # not "normal noise from an appendage".


def _touches_any(primitives, i, members, gap=_CLUSTER_GAP):
    return any(_prim_gap(primitives[i], primitives[j]) <= gap for j in members)


def _text_word_bboxes(page):
    """Native text word bounding boxes, in the SAME rotated coordinate
    space extract_primitives() converts drawing coordinates into.
    page.get_text() (like get_drawings()) always reports raw UNROTATED
    mediabox coordinates, so page.rotation_matrix is applied here too —
    otherwise a comparison against prim['bbox'] would silently misalign
    on any rotated page (this codebase has hit that exact bug twice
    before, see RotatedPageCoordinateTests)."""
    mat = page.rotation_matrix
    bboxes = []
    for w in page.get_text('words'):
        r = fitz.Rect(w[0], w[1], w[2], w[3]) * mat
        bboxes.append((r.x0, r.y0, r.x1, r.y1))
    return bboxes


def _is_text_glyph_primitive(prim, text_bboxes, tol=1.0):
    """Does this primitive's bbox fall (almost entirely) inside a native
    text word's bbox? Some CAD exports double-render a tag's label —
    once as normal searchable text, and again as vector-outlined glyph
    strokes for guaranteed visual fidelity. Confirmed on a real LKAB
    P&ID: a valve's tag text ended 3.6pt into its own quad's bbox, and
    ~36 tiny vector glyph fragments from that text bridged into the
    valve's cluster (see cluster_primitives), diluting bowtie_score's
    point cloud until a correctly-shaped, otherwise-identical valve
    scored 0.0 instead of the ~0.77 every one of its neighbors got."""
    px0, py0, px1, py1 = prim['bbox']
    for tx0, ty0, tx1, ty1 in text_bboxes:
        if px0 >= tx0 - tol and py0 >= ty0 - tol and px1 <= tx1 + tol and py1 <= ty1 + tol:
            return True
    return False


_CORE_MAX_NORM_SIZE = 20.0   # pt, relative to page text size — real valve/
                             # pump/instrument cores measured this session
                             # never exceeded ~15; a generous margin above
                             # that. Confirmed necessary on a real Hybrit
                             # P&ID: a large non-symbol assembly (an aspect
                             # near 1, so _CORE_ASPECT_LIMIT never triggers)
                             # otherwise keeps growing — one such core grew
                             # to norm_size 38 (consuming 300+ primitives
                             # over many rounds) before running out of
                             # touching neighbors, costing over a second of
                             # pure waste since it never scores as a symbol
                             # (bowtie_score 0.0) regardless.

_CORE_MAX_MEMBERS = 80   # real valve/pump/instrument cores measured this
                         # session (including multi-symbol recoveries via
                         # _cluster_cores) never exceeded ~40 primitives; a
                         # generous margin above that. Confirmed necessary
                         # on a real Hybrit P&ID: a ~540-primitive cluster
                         # (a large non-symbol graphic, physically small
                         # enough to slip under _CORE_MAX_NORM_SIZE) took
                         # 28+ seconds to grow because _touches_any's cost
                         # scales with the CURRENT core size, not just
                         # `remaining` — every extra primitive absorbed
                         # makes every future round's touching-check that
                         # much more expensive. Capping member count bounds
                         # that per-round cost directly, where the norm_size
                         # cap alone does not.


def _cluster_core(primitives, group, text_bboxes=None, scale=10.0):
    """A cluster's compact "symbol core", grown outward from its most
    symbol-like seed primitive (the largest closed quad/rect, or a curve
    if there's no quad/rect) — accepting a touching neighbor only as long
    as the growing core's own bbox aspect stays within
    _CORE_ASPECT_LIMIT and its size stays within _CORE_MAX_NORM_SIZE.
    Anything left over (an actuator stem, a drain stub, a flange tick, or
    in a busy area unrelated nearby equipment that happened to bridge in)
    is excluded from the core but NOT dropped from the cluster itself —
    see find_symbol_clusters.

    Confirmed necessary on a real LKAB P&ID: a valve's own bow-tie scores
    perfectly on its own (bowtie_score ~0.6-1.0, aspect ~2), but its
    connected cluster can correctly also include a short stem/drain-stub
    (same physical valve assembly — see cluster_primitives), which alone
    can push the WHOLE cluster's aspect past the compact-symbol threshold
    and dilutes bowtie_score's pooled point cloud until it no longer
    reads as a clean hourglass — hiding a real valve behind filters meant
    for a bare bow-tie.

    A simpler "drop the single longest line, see if the bbox shrinks
    enough" heuristic was tried first and failed on real data: a stem
    plus a drain-stub icon can each independently reach the cluster's
    outer edge, so removing any ONE of them leaves the others still
    covering that same edge and nothing appears to shrink — even though
    the two of them TOGETHER are the appendage. Growing outward from the
    seed sidesteps this: it stops the instant adding the FIRST attached
    primitive would already blow the aspect past the limit, without
    needing to know how many more primitives are attached beyond it.

    `text_bboxes` (see _text_word_bboxes), if given, blocks a further
    case the aspect check alone doesn't catch: many small primitives
    packed tightly together (e.g. a tag's own double-rendered vector
    glyph strokes, see _is_text_glyph_primitive) can sit right next to
    the seed without ever pushing the bbox aspect past the limit — they
    add clutter, not size. Such primitives are skipped during growth
    (never joining the core) regardless of aspect.

    A candidate is also rejected if it would collapse an already-strong
    bow-tie pinch signal (see _CORE_PINCH_GUARD_MIN/_DROP), even though
    the aspect stays within _CORE_ASPECT_LIMIT. Confirmed necessary on a
    real LKAB P&ID: a vertically-mounted valve's own connecting stem was
    only 14pt long, so quad+stem landed at aspect 2.99 — just inside the
    3.0 ceiling — yet the stem's constant-x sample points still fell
    inside bowtie_score's "wide open end" slices and diluted them enough
    to drop the score from 0.77 to 0.0, hiding a real valve. The aspect
    ceiling alone assumes "still under the limit" implies "still looks
    like one compact symbol", which this case disproves; only guarding
    on the actual pinch signal catches it. Skipped for clusters that
    don't already read as a bow-tie (best_score below the min) so this
    never interferes with generic, non-valve compact-symbol growth.
    """
    if len(group) <= 1:
        return list(group)
    seed_candidates = [i for i in group if primitives[i]['kind'] in ('qu', 're')]
    if not seed_candidates:
        seed_candidates = [i for i in group if primitives[i]['kind'] == 'c']
    if not seed_candidates:
        return list(group)
    seed = max(seed_candidates, key=lambda i: _prim_length(primitives[i]))
    core = {seed}
    best_score = bowtie_score(primitives, list(core))
    remaining = set(group) - core
    progress = True
    while progress and remaining and len(core) < _CORE_MAX_MEMBERS:
        progress = False
        touching = sorted(i for i in remaining if _touches_any(primitives, i, core))
        for i in touching:
            if len(core) >= _CORE_MAX_MEMBERS:
                break
            if text_bboxes and _is_text_glyph_primitive(primitives[i], text_bboxes):
                remaining.discard(i)
                continue
            candidate = core | {i}
            x0, y0, x1, y1 = _group_bbox(primitives, candidate)
            w, h = x1 - x0, y1 - y0
            aspect = max(w, h) / max(min(w, h), 0.1)
            if aspect > _CORE_ASPECT_LIMIT:
                continue
            if math.hypot(w, h) / max(scale, 1.0) > _CORE_MAX_NORM_SIZE:
                continue
            # bowtie_score is real sampling/scoring work (not a cheap
            # arithmetic check like aspect), so it's only ever computed
            # once best_score has already shown this core reads as a
            # decent bow-tie — the guard has nothing to protect
            # otherwise. This keeps growth on a large, non-valve cluster
            # (hundreds of members, many growth rounds) exactly as cheap
            # as before this guard existed — confirmed necessary on a
            # real Hybrit P&ID where computing it unconditionally made
            # several of the page's large non-valve clusters take
            # 4-5 seconds each just for one _cluster_core call. An
            # earlier version of this also skipped the check below a
            # fixed aspect floor to save more time, but that let a real
            # Gryaab valve's score silently collapse through several
            # growth steps that all stayed under the floor — best_score
            # never updated, so the guard judged a much later step
            # against a stale, too-generous reference and rejected
            # growth that would have recovered a valid (if different)
            # bow-tie further on. Gating on aspect is unsafe for that
            # reason; only gating on whether the core is bow-tie-like at
            # all (best_score) is.
            if best_score >= _CORE_PINCH_GUARD_MIN:
                candidate_score = bowtie_score(primitives, list(candidate))
                if candidate_score < best_score - _CORE_PINCH_GUARD_DROP:
                    continue
                best_score = candidate_score
            core = candidate
            remaining.discard(i)
            progress = True
    return list(core)


_CLUSTER_CORES_MAX = 8   # hard cap on how many symbol cores one raw
                          # cluster_primitives() group can yield — a
                          # generous bound above any real page's worst
                          # case (see _cluster_cores), just a backstop
                          # against a pathological page.


def _cluster_cores(primitives, group, text_bboxes=None, scale=10.0):
    """Peel every compact "symbol core" out of a cluster, one at a time
    (see _cluster_core), instead of assuming a cluster_primitives() group
    contains exactly one real symbol.

    Confirmed necessary on a real LKAB P&ID: a shared instrument signal
    wire — drawn as many short dash segments with small circular
    junction-dots bridging each dash-to-dash gap (each gap individually
    well under _CLUSTER_GAP, so cluster_primitives correctly merges them
    as one continuous line) — chained a valve, several instrument
    bubbles, a motor, and a VSD box roughly 500pt apart into ONE cluster.
    _cluster_core alone only recovers the first compact symbol reachable
    from the biggest seed (whichever 'qu'/'re'/'c' primitive has the
    largest perimeter) and silently drops every other real symbol still
    sitting in that same oversized cluster.

    Stops once no 'qu'/'re'/'c' primitive remains to seed another core —
    what's left is loose line/glyph fragments that could never score as
    a valve/pump/instrument on their own, so there is no point spending a
    growth pass on them. Also capped at _CLUSTER_CORES_MAX iterations as
    a backstop; every real page sampled while building this needed at
    most a handful of cores out of even the largest merged clusters.
    """
    remaining = set(group)
    cores = []
    while remaining and len(cores) < _CLUSTER_CORES_MAX:
        seed_candidates = [i for i in remaining if primitives[i]['kind'] in ('qu', 're', 'c')]
        if not seed_candidates:
            break
        core = _cluster_core(primitives, remaining, text_bboxes=text_bboxes, scale=scale)
        if not core:
            break
        cores.append(core)
        remaining -= set(core)
    return cores


_LOOP_CLOSURE_TOL = 4.0   # pt — endpoints within this distance count as
                          # the same point when checking for a closed
                          # loop of edges. Must comfortably exceed the
                          # small real-world gap some CAD exports leave
                          # at a triangle's own base (~3.5pt measured on
                          # a real Sunpine/Swerim valve) while staying
                          # well under an unrelated open shape's gap
                          # (~8.6pt for the vector-drawn "M" glyph in
                          # ClosedShapeFilterTests).


def _has_closed_loop(primitives, group, tol=_LOOP_CLOSURE_TOL):
    """Does this cluster contain at least one closed loop of edges — the
    signature of a genuine bow-tie body, however it happens to be drawn?

    cluster_features()['has_closed_or_filled'] (an explicit closed=True
    or filled=True primitive) is what this codebase originally used to
    tell a real valve apart from an open shape like a double-rendered
    tag-text glyph (see ClosedShapeFilterTests) — but that check turned
    out to reject GENUINE valves too: confirmed on real Sunpine, Swerim
    and ITS P&IDs, which draw a valve's two triangles as separate
    unclosed 'l' segments (closePath=False, no fill) — visually a crisp
    closed bow-tie, but with no primitive individually marked closed or
    filled. On a sample Sunpine/ITS/Gryaab page, roughly a third to two
    thirds of otherwise-valid bow-tie candidates used this "open-stroke"
    convention — a widespread pattern, not a one-off.

    Fast path: any 're'/'qu' primitive is closed by construction (see
    extract_primitives), and any 'filled' primitive implies a closed
    outline too — either settles it immediately, covering the LKAB-style
    self-intersecting quad and the closed/filled two-triangle convention
    without the cost of the graph walk below.

    Otherwise, build a graph from every 'l'/'c' segment's endpoints
    (merging endpoints within `tol` into one node) and check whether any
    connected component has at least as many edges as nodes — only
    possible if that component contains a cycle. A real open shape (the
    "M" glyph's zigzag, a pipe corner, a T-junction) never satisfies
    this regardless of tolerance; a triangle missing only its exact
    corner-point precision does.
    """
    members = [primitives[i] for i in group]
    if any(m['closed'] or m['filled'] for m in members):
        return True

    segments = [(m['p0'], m['p1']) for m in members if m['kind'] in ('l', 'c')]
    if len(segments) < 3:
        return False

    nodes = []

    def find_or_add(p):
        for idx, q in enumerate(nodes):
            if _dist(p, q) <= tol:
                return idx
        nodes.append(p)
        return len(nodes) - 1

    edges = [(find_or_add(p0), find_or_add(p1)) for p0, p1 in segments]

    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i0, i1 in edges:
        r0, r1 = find(i0), find(i1)
        if r0 != r1:
            parent[r0] = r1

    comp_nodes = {}
    comp_edges = {}
    for i in range(len(nodes)):
        comp_nodes.setdefault(find(i), set()).add(i)
    for i0, i1 in edges:
        root = find(i0)
        comp_edges[root] = comp_edges.get(root, 0) + 1

    return any(comp_edges.get(root, 0) >= len(members_)
               for root, members_ in comp_nodes.items())


def find_symbol_clusters(page, min_confidence=0.3):
    """Extract, cluster, and score all vector-drawn symbol candidates on a
    page. Returns a list of dicts (cluster features + 'confidence' +
    'bowtie_score' + 'outline', sorted by confidence descending). `outline`
    is a simple bbox-corner polygon [[x,y], ...] directly usable for
    drawing an overlay shape. `bowtie_score` (see bowtie_score()) is
    computed for every cluster regardless of min_confidence, so a caller
    hunting for valve shapes specifically can pass min_confidence=0.0 and
    filter on bowtie_score instead of the generic "is this a symbol at
    all" confidence.

    'confidence' always reflects the FULL cluster (including any
    appendage) — an attached stem/stub is still part of one real symbol,
    so the generic "is this a discrete symbol" question shouldn't ignore
    it. But 'bbox'/'aspect'/'norm_size'/'has_diagonal' are recomputed on
    the trimmed core (_cluster_core) whenever trimming actually changes
    the group — bowtie_score and the shape-based filters in
    pid_viewer.find_valve_shapes() care about the compact body, not how
    far a stem happens to stick out.

    A raw cluster that _cluster_cores splits into MORE than one core
    (see its docstring — a shared signal wire bridging several genuinely
    separate symbols into one cluster) contributes one extra result
    entry per additional core, each with its own bbox/aspect/bowtie_score
    but no pump_bboxes/instrument_bboxes of its own (those are already
    found once, from the raw group, on the FIRST entry — see below —
    so attaching them again here would double-count them).
    """
    primitives = extract_primitives(page)
    if not primitives:
        return []
    scale = dominant_text_size(page, primitives=primitives)
    text_bboxes = _text_word_bboxes(page)
    # cluster_primitives' dense-cell rescue (see its docstring) is only
    # switched on for pages with NO native text at all — the one
    # condition it was confirmed necessary for (Loket/Smurfit Kappa/
    # Swerim/NYA CAD exports that draw all text as vector strokes) and
    # confirmed UNSAFE to enable unconditionally (a real, normally text-
    # bearing Hybrit page lost a correctly-found valve to an unwanted
    # merge when it was on for every page). text_bboxes (already needed
    # below) doubles as the "does this page have native text" signal —
    # no extra get_text() call needed.
    rescue_dense_cells = not text_bboxes
    # The pipe-run-line threshold (see cluster_primitives' `pipe_scale`
    # parameter) is a DIFFERENT concept from the glyph-calibrated `scale`
    # above and must not shrink along with it on a no-text page: confirmed
    # necessary on a real Smurfit Kappa P&ID, where feeding the corrected
    # (much smaller) no-text `scale` into the pipe-run cutoff too cut a
    # real valve's own 40pt actuator-stem connector that the OLD, larger
    # no-text default (_NO_TEXT_SCALE_DEFAULT, unrelated to glyph size)
    # left alone. A page WITH native text is unaffected either way — its
    # `scale` already comes from real font size, not this fallback.
    pipe_scale = scale if text_bboxes else _NO_TEXT_SCALE_DEFAULT
    # Every real file sampled draws circles with ONE convention
    # consistently across a whole page — true bezier curves everywhere
    # (LKAB/Gryaab/ITS), or zero curves anywhere (Sunpine/Swerim, which
    # approximate every circle as a many-sided line polygon instead).
    # Computed once per page, not per cluster: _polygon_circle_islands'
    # O(k^2)-per-cluster line search is only worth paying for on a page
    # that actually needs it — running it unconditionally made a busy
    # real Gryaab page (curves, not line-polygons) take 5+ seconds on
    # clusters that were never going to contain one.
    try_polygon_circles = not any(p['kind'] == 'c' for p in primitives)
    results = []
    for group in cluster_primitives(primitives, scale=scale, rescue_dense_cells=rescue_dense_cells,
                                     pipe_scale=pipe_scale):
        feats = cluster_features(primitives, group, page_text_scale=scale)
        conf = classify_cluster(feats)
        if conf < min_confidence:
            continue
        cores = _cluster_cores(primitives, group, text_bboxes=text_bboxes, scale=scale)
        core = cores[0] if cores else group
        if core != group:
            core_feats = cluster_features(primitives, core, page_text_scale=scale)
            feats = {**feats, 'bbox': core_feats['bbox'], 'aspect': core_feats['aspect'],
                      'norm_size': core_feats['norm_size'], 'has_diagonal': core_feats['has_diagonal']}
        feats['has_closed_loop'] = _has_closed_loop(primitives, core)
        x0, y0, x1, y1 = feats['bbox']
        # Computed on the RAW group, not the valve-appendage-aware `core`
        # — pump/instrument_shapes_in_cluster do their own per-circle
        # 'island' isolation and need the full group to find every
        # distinct circle a giant merged-area cluster may contain, not
        # just whatever _cluster_core's aspect-bounded growth kept.
        # Computed once and shared between both — they'd otherwise each
        # redundantly repeat the same island-finding work per cluster.
        islands = _circle_islands(primitives, group, try_polygon_circles=try_polygon_circles)
        results.append({
            **feats, 'confidence': conf,
            'bowtie_score': bowtie_score(primitives, core),
            'pump_bboxes': pump_shapes_in_cluster(primitives, group, islands=islands),
            'instrument_bboxes': instrument_shapes_in_cluster(primitives, group, islands=islands),
            'outline': [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
        })
        for extra_core in cores[1:]:
            extra_feats = cluster_features(primitives, extra_core, page_text_scale=scale)
            extra_feats['has_closed_loop'] = _has_closed_loop(primitives, extra_core)
            ex0, ey0, ex1, ey1 = extra_feats['bbox']
            results.append({
                **extra_feats, 'confidence': conf,
                'bowtie_score': bowtie_score(primitives, extra_core),
                'pump_bboxes': [], 'instrument_bboxes': [],
                'outline': [[ex0, ey0], [ex1, ey0], [ex1, ey1], [ex0, ey1]],
            })
    results.sort(key=lambda r: -r['confidence'])
    return results


# ══════════════════════════════════════════════════════════════════════════
# Leader-line search — links a tag's text position to the correct nearby
# symbol cluster. Priority: leader line > touches/adjacent > nearest >
# none. "none" is reported, not hidden, so the review dialog can flag it.
# ══════════════════════════════════════════════════════════════════════════

_LEADER_SEARCH_RADIUS = 220.0   # pt — how far from the tag to look for a symbol at all
_LEADER_MAX_CHAIN_LEN = 200.0   # pt — cap total leader-line chain length
_LEADER_ENDPOINT_TOL = 4.0      # pt — "touches" tolerance for chaining segments
_LEADER_MAX_HOPS = 3            # segments — covers a dashed line + one bend


def _point_near_bbox(pt, bbox, tol):
    x0, y0, x1, y1 = bbox
    return (x0 - tol) <= pt[0] <= (x1 + tol) and (y0 - tol) <= pt[1] <= (y1 + tol)


def _bbox_center(bbox):
    x0, y0, x1, y1 = bbox
    return ((x0 + x1) / 2, (y0 + y1) / 2)


def find_leader_line(tag_point, candidate_clusters, primitives,
                      endpoint_tol=_LEADER_ENDPOINT_TOL):
    """Search for a straight line, or a short chain of up to
    _LEADER_MAX_HOPS connected segments (covers dashed leader lines and one
    bend), connecting tag_point to one of candidate_clusters' bboxes.

    Returns the matching cluster dict, or None if no leader line is found.
    """
    lines = [p for p in primitives if p['kind'] == 'l']
    seeds = [ln for ln in lines
             if _dist(ln['p0'], tag_point) <= endpoint_tol
             or _dist(ln['p1'], tag_point) <= endpoint_tol]

    for seed in seeds:
        frontier = (seed['p1'] if _dist(seed['p0'], tag_point) <= endpoint_tol
                    else seed['p0'])
        chain_len = _dist(seed['p0'], seed['p1'])
        visited = {id(seed)}

        for _hop in range(_LEADER_MAX_HOPS):
            for cl in candidate_clusters:
                if _point_near_bbox(frontier, cl['bbox'], endpoint_tol * 2):
                    return cl
            nxt, nxt_far = None, None
            for ln in lines:
                if id(ln) in visited:
                    continue
                if _dist(ln['p0'], frontier) <= endpoint_tol:
                    nxt, nxt_far = ln, ln['p1']
                elif _dist(ln['p1'], frontier) <= endpoint_tol:
                    nxt, nxt_far = ln, ln['p0']
                else:
                    continue
                if chain_len + _dist(nxt['p0'], nxt['p1']) > _LEADER_MAX_CHAIN_LEN:
                    nxt = None
                    continue
                break
            if nxt is None:
                break
            chain_len += _dist(nxt['p0'], nxt['p1'])
            visited.add(id(nxt))
            frontier = nxt_far
    return None


def resolve_tag_symbol(tag_point, clusters, primitives, search_radius=_LEADER_SEARCH_RADIUS):
    """Resolve which symbol cluster a tag is linked to.

    Returns (cluster_or_None, method) where method is one of
    'leader' | 'contain' | 'nearest' | 'none'.
    """
    nearby = [c for c in clusters
              if _dist(_bbox_center(c['bbox']), tag_point) <= search_radius]
    if not nearby:
        return None, 'none'

    leader = find_leader_line(tag_point, nearby, primitives)
    if leader is not None:
        return leader, 'leader'

    for c in nearby:
        if _point_near_bbox(tag_point, c['bbox'], tol=5.0):
            return c, 'contain'

    nearest = min(nearby, key=lambda c: _dist(_bbox_center(c['bbox']), tag_point))
    return nearest, 'nearest'


# ══════════════════════════════════════════════════════════════════════════
# Defensive bbox helpers — dedup safety net for tag/shape association below.
# Neither existed before; resolve_tag_symbol only ever needed point-to-bbox
# and point-to-point distance, not cluster-to-cluster overlap.
# ══════════════════════════════════════════════════════════════════════════

def bbox_iou(bbox_a, bbox_b):
    """Standard intersection-over-union of two (x0,y0,x1,y1) boxes, 0..1."""
    ax0, ay0, ax1, ay1 = bbox_a
    bx0, by0, bx1, by1 = bbox_b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0.0 else 0.0


def cluster_distance(cluster_a, cluster_b):
    """Centroid-to-centroid distance between two cluster dicts (each must
    have a 'bbox' key, as produced by find_symbol_clusters)."""
    return _dist(_bbox_center(cluster_a['bbox']), _bbox_center(cluster_b['bbox']))


# ══════════════════════════════════════════════════════════════════════════
# Weighted tag↔cluster link scoring — a combined 0..1 plausibility score
# instead of resolve_tag_symbol's fixed leader>contain>nearest cascade.
# Used by pid_viewer.associate_tags_to_clusters() to do a GLOBAL assignment
# across every tag and every cluster on a page (so the same cluster can't
# be claimed by two different tags), rather than resolving each tag
# independently the way resolve_tag_symbol does. resolve_tag_symbol itself
# is left untouched — still used by detect_equipment_symbols.
# ══════════════════════════════════════════════════════════════════════════

_LINK_W_LEADER   = 0.50
_LINK_W_CONTAIN  = 0.30
_LINK_W_DISTANCE = 0.20


def score_tag_cluster_link(tag_point, cluster, primitives, search_radius=_LEADER_SEARCH_RADIUS):
    """Geometric plausibility (0..1) that tag_point belongs to `cluster`.

    0.0 if the cluster's centroid is farther than search_radius from
    tag_point. Otherwise a weighted sum of: a leader line reaches this
    exact cluster (+_LINK_W_LEADER), tag_point falls within the cluster's
    bbox (+tol 5pt, +_LINK_W_CONTAIN), and an inverse-distance term
    (+_LINK_W_DISTANCE, scaled by how close within search_radius). Pure
    geometry only — callers may add a tag-format-plausibility bonus on top
    (that needs KNOWN_PREFIXES, which lives in equipment_detection.py, not here).
    """
    dist = _dist(_bbox_center(cluster['bbox']), tag_point)
    if dist > search_radius:
        return 0.0
    score = _LINK_W_DISTANCE * max(0.0, 1.0 - dist / search_radius)
    if _point_near_bbox(tag_point, cluster['bbox'], tol=5.0):
        score += _LINK_W_CONTAIN
    leader = find_leader_line(tag_point, [cluster], primitives)
    if leader is not None:
        score += _LINK_W_LEADER
    return min(1.0, score)


def build_pair_scores(tag_points, clusters, primitives, search_radius=_LEADER_SEARCH_RADIUS):
    """Score every (tag, cluster) pair within search_radius on one page.

    tag_points: [(tag_idx, (x, y)), ...] — tag_idx is just an opaque index
    the caller assigns (e.g. position in its own tag list), not a tag
    string, so this module stays free of any tag-format knowledge.

    Returns {(tag_idx, cluster_idx): score}, omitting pairs scoring 0.0
    (out of range) to keep the candidate matrix small.

    Deliberately does NOT call score_tag_cluster_link() per pair — that
    would call find_leader_line() once per (tag, cluster) combination,
    turning an O(tags) walk into O(tags * clusters); a real busy P&ID page
    (found while investigating a real slowdown report: 60 valves + normal
    piping density took >1s for this step alone with the naive approach)
    makes that blow-up very real. find_leader_line() already checks every
    candidate cluster per hop in one pass, so it only needs to be called
    ONCE per tag, against every nearby cluster at once — exactly like
    resolve_tag_symbol() already does it.
    """
    scores = {}
    for tag_idx, tag_point in tag_points:
        nearby = [(ci, c) for ci, c in enumerate(clusters)
                  if _dist(_bbox_center(c['bbox']), tag_point) <= search_radius]
        if not nearby:
            continue
        leader_cluster = find_leader_line(tag_point, [c for _ci, c in nearby], primitives)
        for cluster_idx, cluster in nearby:
            dist = _dist(_bbox_center(cluster['bbox']), tag_point)
            score = _LINK_W_DISTANCE * max(0.0, 1.0 - dist / search_radius)
            if _point_near_bbox(tag_point, cluster['bbox'], tol=5.0):
                score += _LINK_W_CONTAIN
            if leader_cluster is cluster:
                score += _LINK_W_LEADER
            if score > 0.0:
                scores[(tag_idx, cluster_idx)] = min(1.0, score)
    return scores


# ══════════════════════════════════════════════════════════════════════════
# Pipe-run tracing — walk connected line primitives outward from a symbol's
# bbox to find nearby line-number/medium/DN callouts. Deliberately vector-
# only (reuses find_leader_line's hop-chaining technique) rather than a
# raster search: no page rendering, stays cheap across a 50-page document.
# ══════════════════════════════════════════════════════════════════════════

_TRACE_MAX_HOPS      = 6      # pipe runs are longer than a tag's leader line
_TRACE_MAX_CHAIN_LEN = 600.0  # pt, per branch
_TRACE_MAX_BRANCHES  = 6      # cap fan-out at junctions/manifolds
_TRACE_GRID_CELL     = 20.0   # pt — matches cluster_primitives' own grid-bucketing scale


def build_line_index(primitives, cell=_TRACE_GRID_CELL):
    """Grid-bucket every 'l'-kind primitive's endpoints for fast "which
    lines touch this point" lookups. Without this, trace_pipe_points_
    from_bbox scans EVERY line primitive on the page at every hop, for
    every branch, for every valve — found while investigating a real
    slowdown report: a page with 60 valves and normal piping density
    produced ~1.7 million distance checks for this step alone. Build once
    per page and pass the result to trace_pipe_points_from_bbox via
    line_index= for every valve traced on that page, exactly like
    find_symbol_clusters' primitive extraction is already shared across a
    page's tag-association and shape-hunting.

    Returns (lines, grid_index) — pass both back in as line_index=.
    """
    lines = [p for p in primitives if p['kind'] == 'l']
    index = {}
    for i, ln in enumerate(lines):
        for pt in (ln['p0'], ln['p1']):
            key = (int(pt[0] // cell), int(pt[1] // cell))
            index.setdefault(key, []).append(i)
    return lines, index


def _lines_near_point(lines, index, point, tol, cell=_TRACE_GRID_CELL):
    """Lines with an endpoint within tol of `point`, via the grid index —
    tol is always « cell, so the point's 3x3 cell neighbourhood always
    covers every actually-qualifying line, same as cluster_primitives'
    own grid-bucketed neighbour search."""
    cx, cy = int(point[0] // cell), int(point[1] // cell)
    seen = set()
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            seen.update(index.get((cx + dx, cy + dy), ()))
    return [lines[i] for i in seen]


def _lines_near_bbox(lines, index, bbox, tol, cell=_TRACE_GRID_CELL):
    """Lines with an endpoint within tol of any edge of the (tol-expanded)
    bbox — same grid-index technique as _lines_near_point, just over the
    range of cells the expanded bbox spans instead of a single point."""
    x0, y0, x1, y1 = bbox
    gx0, gy0 = int((x0 - tol) // cell), int((y0 - tol) // cell)
    gx1, gy1 = int((x1 + tol) // cell), int((y1 + tol) // cell)
    seen = set()
    for gx in range(gx0, gx1 + 1):
        for gy in range(gy0, gy1 + 1):
            seen.update(index.get((gx, gy), ()))
    return [lines[i] for i in seen]


def trace_pipe_points_from_bbox(bbox, primitives,
                                 max_hops=_TRACE_MAX_HOPS,
                                 max_chain_len=_TRACE_MAX_CHAIN_LEN,
                                 max_branches=_TRACE_MAX_BRANCHES,
                                 endpoint_tol=_LEADER_ENDPOINT_TOL,
                                 line_index=None):
    """Walk outward from `bbox` along connected 'l'-kind primitives (pipe
    runs), returning every point visited, nearest first.

    Seeds from every line primitive whose endpoint touches the bbox
    boundary within endpoint_tol (a valve sits ON the pipe, so its leaving
    lines start right at its bbox edge). Explores up to max_branches
    distinct seed branches, each capped at max_hops segments /
    max_chain_len total length — a line-number callout is typically
    printed mid-run, not just at the far end, so every hop's endpoint is
    collected, not only the final one.

    line_index: optional (lines, grid_index) pair from build_line_index()
    — pass one built once per page and reused across every valve traced
    on it. If omitted, an index is built internally from `primitives`
    (correct, but repeats the O(lines) index-build cost on every call —
    fine for a single ad-hoc call, wasteful in a per-valve loop).
    """
    # extract_primitives() emits BOTH directions of every line (p0/p1
    # swapped) — harmless for find_leader_line (which tries a fresh
    # `visited` per seed and usually only needs 1-3 hops), but walking up
    # to max_hops here means a bare id()-based visited set lets a branch
    # "step onto" the reverse copy of the line it just came from and
    # bounce straight back the way it arrived. Key visited/seed dedup by
    # each line's canonical (order-independent) endpoint pair instead.
    def _line_key(ln):
        return frozenset((ln['p0'], ln['p1']))

    lines, index = line_index if line_index is not None else build_line_index(primitives)

    seen_seed_keys = set()
    seeds = []
    for ln in _lines_near_bbox(lines, index, bbox, endpoint_tol):
        if not (_point_near_bbox(ln['p0'], bbox, endpoint_tol)
                or _point_near_bbox(ln['p1'], bbox, endpoint_tol)):
            continue
        key = _line_key(ln)
        if key in seen_seed_keys:
            continue
        seen_seed_keys.add(key)
        seeds.append(ln)

    visited_points = []
    for seed in seeds[:max_branches]:
        frontier = (seed['p1'] if _point_near_bbox(seed['p0'], bbox, endpoint_tol)
                    else seed['p0'])
        chain_len = _dist(seed['p0'], seed['p1'])
        visited = {_line_key(seed)}
        visited_points.append((chain_len, frontier))

        for _hop in range(max_hops - 1):
            nxt, nxt_far = None, None
            for ln in _lines_near_point(lines, index, frontier, endpoint_tol):
                if _line_key(ln) in visited:
                    continue
                if _dist(ln['p0'], frontier) <= endpoint_tol:
                    nxt, nxt_far = ln, ln['p1']
                elif _dist(ln['p1'], frontier) <= endpoint_tol:
                    nxt, nxt_far = ln, ln['p0']
                else:
                    continue
                if chain_len + _dist(nxt['p0'], nxt['p1']) > max_chain_len:
                    nxt = None
                    continue
                break
            if nxt is None:
                break
            chain_len += _dist(nxt['p0'], nxt['p1'])
            visited.add(_line_key(nxt))
            frontier = nxt_far
            visited_points.append((chain_len, frontier))

    visited_points.sort(key=lambda t: t[0])
    return [pt for _chain_len, pt in visited_points]
