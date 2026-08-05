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
    """
    prims = []
    for src_idx, d in enumerate(page.get_drawings()):
        items = d.get('items') or []
        closed = bool(d.get('closePath'))
        filled = 'f' in (d.get('type') or '')
        width = d.get('width') or 0.0
        for item in items:
            kind = item[0]
            if kind == 'l':
                p0, p1 = item[1], item[2]
                prims.append({
                    'kind': 'l', 'bbox': _bbox_of_points([p0, p1]),
                    'p0': (p0.x, p0.y), 'p1': (p1.x, p1.y),
                    'closed': closed, 'filled': filled,
                    'width': width, 'source': src_idx,
                })
            elif kind == 'c':
                pts = item[1:5]
                prims.append({
                    'kind': 'c', 'bbox': _bbox_of_points(pts),
                    'p0': (pts[0].x, pts[0].y), 'p1': (pts[-1].x, pts[-1].y),
                    'closed': closed, 'filled': filled,
                    'width': width, 'source': src_idx,
                })
            elif kind == 're':
                rect = item[1]
                prims.append({
                    'kind': 're', 'bbox': (rect.x0, rect.y0, rect.x1, rect.y1),
                    'p0': (rect.x0, rect.y0), 'p1': (rect.x1, rect.y1),
                    'closed': True, 'filled': filled,
                    'width': width, 'source': src_idx,
                })
            elif kind == 'qu':
                quad = item[1]
                pts = [quad.ul, quad.ur, quad.lr, quad.ll]
                prims.append({
                    'kind': 'qu', 'bbox': _bbox_of_points(pts),
                    'p0': (pts[0].x, pts[0].y), 'p1': (pts[2].x, pts[2].y),
                    'closed': True, 'filled': filled,
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
    total_len = sum(_prim_length(m) for m in members)

    scale = max(page_text_scale, 1.0)
    return {
        'bbox': bbox, 'w': w, 'h': h, 'aspect': aspect,
        'n_items': len(members),
        'n_sources': len(set(m['source'] for m in members)),
        'has_curve': has_curve,
        'has_closed_or_filled': has_closed_or_filled,
        'fold_ratio': total_len / diag,
        'norm_size': diag / scale,   # symbol size relative to text height
    }


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
    'outline', sorted by confidence descending). `outline` is a simple
    bbox-corner polygon [[x,y], ...] directly usable for drawing an overlay
    shape.
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
