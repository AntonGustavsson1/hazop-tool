"""Geometric analysis of vector-drawn P&ID symbols (valves, instruments, etc.)
extracted from PDF page drawings via PyMuPDF's page.get_drawings().

Pure Python/PyMuPDF — no Qt dependency, so this module can be imported by
pid_viewer.py (for the live app) or by standalone scripts/tests without
pulling in the GUI stack.

Scope: vector-drawn PDFs only. Equipment TYPE (valve vs pump vs instrument)
is not guessed from shape here — that already comes from the tag's prefix
via KNOWN_PREFIXES in pid_viewer.py. This module answers a narrower
question: "is there a discrete drawn symbol near this tag, and if so,
exactly where/what shape is it?" — used to place an accurate marker and
confirm the tag-to-symbol link, not to classify ISA valve sub-types.
"""
import math


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
_MAX_CELL_DENSITY = 40   # primitives per grid cell — see cluster_primitives


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


def cluster_primitives(primitives, gap=_CLUSTER_GAP):
    """Group primitives into connected components by actual edge proximity
    (see _prim_gap — deliberately not bbox proximity).

    The spatial grid below is only a broad-phase filter to shortlist which
    pairs are worth the exact edge-distance check; the grid cell a
    primitive's (gap-expanded) bbox touches has no bearing on whether it
    actually merges with a neighbor — only _prim_gap does.

    Cells denser than _MAX_CELL_DENSITY skip the pairwise check entirely.
    A real equipment symbol is a handful of primitives (a bowtie is 6, a
    circle is 4); a 20pt cell packed with dozens+ primitives is a
    hatching/fill-texture or dense title-block-table pattern, not a
    discrete symbol — measured on real P&ID reference files, a single
    such cell can hold 1000+ primitives, which made the O(k^2) pairwise
    edge-distance check take 7-13 seconds on an otherwise ordinary page.
    Primitives left ungrouped by this skip fall out on their own as
    (almost always low-confidence) singleton clusters — classify_cluster
    still runs on them, nothing is silently dropped from the result.

    Returns a list of index-lists into `primitives` (one list per cluster).
    """
    n = len(primitives)
    if n == 0:
        return []
    uf = _UnionFind(n)
    grid = {}
    for idx, prim in enumerate(primitives):
        x0, y0, x1, y1 = _bbox_expand(prim['bbox'], gap)
        for gx in range(int(x0 // _GRID_CELL), int(x1 // _GRID_CELL) + 1):
            for gy in range(int(y0 // _GRID_CELL), int(y1 // _GRID_CELL) + 1):
                grid.setdefault((gx, gy), []).append(idx)

    for cell_items in grid.values():
        if len(cell_items) > _MAX_CELL_DENSITY:
            continue
        for a in range(len(cell_items)):
            i = cell_items[a]
            for b in range(a + 1, len(cell_items)):
                j = cell_items[b]
                if uf.find(i) == uf.find(j):
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


def dominant_text_size(page):
    """Median span font size on the page — used to normalize symbol sizes
    across differently-scaled drawings (A4 vs A0/A1)."""
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
        return 10.0
    sizes.sort()
    return sizes[len(sizes) // 2]


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
    """
    primitives = extract_primitives(page)
    if not primitives:
        return []
    scale = dominant_text_size(page)
    results = []
    for group in cluster_primitives(primitives):
        feats = cluster_features(primitives, group, page_text_scale=scale)
        conf = classify_cluster(feats)
        if conf < min_confidence:
            continue
        x0, y0, x1, y1 = feats['bbox']
        results.append({
            **feats, 'confidence': conf,
            'bowtie_score': bowtie_score(primitives, group),
            'outline': [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
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
    (that needs KNOWN_PREFIXES, which lives in pid_viewer.py, not here).
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
