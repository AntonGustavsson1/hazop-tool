"""Unit tests for symbol_geometry.py — geometric detection of vector-drawn
P&ID equipment symbols and tag-to-symbol leader-line linking.

All fixtures are synthetic PDFs built at test time via PyMuPDF's
page.new_shape() — no real P&ID files are used or committed as test
fixtures (see the "Autodetektera utrustning" plan: real reference P&IDs in
P&ID ref/ are for manual/local review only, some are marked confidential).

Run with:
    python -m pytest hazop/test_symbol_geometry.py -v
or:
    python -m unittest hazop.test_symbol_geometry -v
"""
import math
import sys
import time
import unittest
from pathlib import Path

_HAZOP_DIR = Path(__file__).resolve().parent
if str(_HAZOP_DIR) not in sys.path:
    sys.path.insert(0, str(_HAZOP_DIR))

import fitz
import symbol_geometry as sg


def _new_page(w=200, h=200):
    doc = fitz.open()
    return doc, doc.new_page(width=w, height=h)


class PrimitiveExtractionTests(unittest.TestCase):
    def test_extract_line_segments_returns_real_data(self):
        """Regression test for the fixed dead-code bug: the old
        _extract_pdf_lines_for_page tested hasattr(path, 'rects') /
        hasattr(path, 'lines') against plain dicts (always False) and
        always returned []. extract_line_segments must return real,
        non-empty segment data derived from the actual drawn geometry.
        """
        doc, page = _new_page(100, 100)
        shape = page.new_shape()
        shape.draw_line(fitz.Point(0, 0), fitz.Point(50, 50))
        shape.finish(color=(0, 0, 0), width=1)
        shape.draw_rect(fitz.Rect(10, 10, 30, 30))
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()

        segments = sg.extract_line_segments(page)
        self.assertGreaterEqual(len(segments), 5,
            "expected at least 1 line + 4 rect edges, got none — extraction is dead")

        # The rectangle's 4 edges must be present (order-independent check).
        rect_edges = {
            (10.0, 10.0, 30.0, 10.0), (30.0, 10.0, 30.0, 30.0),
            (30.0, 30.0, 10.0, 30.0), (10.0, 30.0, 10.0, 10.0),
        }
        found = set(segments)
        self.assertTrue(rect_edges.issubset(found),
            f"rectangle edges missing from extracted segments: {found}")
        doc.close()

    def test_extract_line_segments_empty_page_returns_empty_list(self):
        doc, page = _new_page()
        self.assertEqual(sg.extract_line_segments(page), [])
        doc.close()


class ClusteringAndClassificationTests(unittest.TestCase):
    """Uses a page with a long pipe line, a two-triangle valve bowtie
    (drawn as two SEPARATE get_drawings() dicts, as real P&ID valve
    symbols often are), an instrument-bubble circle, and a title-block
    frame with real page text present (so size normalization behaves like
    a real drawing, not the no-text fallback)."""

    def _build_page(self):
        doc, page = _new_page(200, 200)
        shape = page.new_shape()

        # Simulated pipe: one long straight line.
        shape.draw_line(fitz.Point(10, 10), fitz.Point(190, 10))
        shape.finish(color=(0, 0, 0), width=1)

        # Simulated valve bowtie: two triangles sharing an apex, as two
        # independent finish() calls (two separate get_drawings() dicts).
        shape.draw_polyline([fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

        # Simulated instrument bubble: a circle.
        shape.draw_circle(fitz.Point(150, 150), 8)
        shape.finish(color=(0, 0, 0), width=1)

        # Simulated title-block frame: a large bounding rectangle.
        shape.draw_rect(fitz.Rect(0, 0, 200, 200))
        shape.finish(color=(0, 0, 0), width=2)
        shape.commit()

        # Real page text, at a normal-ish size, so dominant_text_size()
        # doesn't fall back to its no-text default and the frame's
        # normalized size correctly lands in the "too large" exclusion.
        page.insert_text(fitz.Point(5, 195), "TITLE BLOCK", fontsize=6)
        return doc, page

    def test_bowtie_triangles_merge_into_one_cluster(self):
        doc, page = self._build_page()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims)

        bowtie_groups = [g for g in groups
                         if sg.cluster_features(prims, g)['bbox'] == (90.0, 90.0, 110.0, 110.0)]
        self.assertEqual(len(bowtie_groups), 1,
            "the two triangles (two separate get_drawings() dicts) must merge into one cluster")
        feats = sg.cluster_features(prims, bowtie_groups[0])
        self.assertEqual(feats['n_sources'], 2,
            "cluster must record that it merged 2 originally-separate drawing dicts")
        doc.close()

    def test_pipe_line_gets_excluded(self):
        doc, page = self._build_page()
        clusters = sg.find_symbol_clusters(page, min_confidence=0.0)
        pipe = next((c for c in clusters if c['bbox'][2] - c['bbox'][0] > 100), None)
        self.assertIsNotNone(pipe, "expected to find the long pipe line as its own cluster")
        self.assertEqual(sg.classify_cluster(pipe), 0.0,
            "a long thin line must score 0 confidence (aspect > 3.0 hard exclude)")
        doc.close()

    def test_bowtie_and_circle_score_high_confidence(self):
        doc, page = self._build_page()
        clusters = sg.find_symbol_clusters(page, min_confidence=0.3)
        found_bboxes = {c['bbox'] for c in clusters}
        self.assertIn((90.0, 90.0, 110.0, 110.0), found_bboxes, "bowtie cluster must pass the confidence filter")
        self.assertIn((142.0, 142.0, 158.0, 158.0), found_bboxes, "circle cluster must pass the confidence filter")
        for c in clusters:
            if c['bbox'] in ((90.0, 90.0, 110.0, 110.0), (142.0, 142.0, 158.0, 158.0)):
                self.assertGreaterEqual(c['confidence'], 0.7)
        doc.close()

    def test_title_block_frame_excluded_when_real_text_present(self):
        doc, page = self._build_page()
        clusters = sg.find_symbol_clusters(page, min_confidence=0.3)
        frame_bbox = (0.0, 0.0, 200.0, 200.0)
        found_bboxes = {c['bbox'] for c in clusters}
        self.assertNotIn(frame_bbox, found_bboxes,
            "a page-spanning frame must be excluded once real text sets a sane size scale")
        doc.close()

    def test_no_bbox_bridging_through_large_enclosing_shape(self):
        """The bowtie and circle must NOT be merged into the frame's cluster
        just because the frame's bounding box spatially contains them — only
        actual edge proximity should drive clustering (see _prim_gap)."""
        doc, page = self._build_page()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims)
        # Every primitive belongs to exactly one group; the frame's 4 edges
        # must end up in a DIFFERENT group than the interior bowtie/circle.
        def group_of(pred):
            for g in groups:
                if any(pred(prims[i]) for i in g):
                    return frozenset(g)
            return None
        frame_group = group_of(lambda p: p['bbox'] == (0.0, 0.0, 200.0, 200.0))
        bowtie_group = group_of(lambda p: p['bbox'][0] in (90.0,) and p['bbox'][2] - p['bbox'][0] == 10.0)
        self.assertIsNotNone(frame_group)
        self.assertIsNotNone(bowtie_group)
        self.assertNotEqual(frame_group, bowtie_group,
            "large enclosing frame must not bridge-merge with shapes drawn inside it")
        doc.close()


class PipeNetworkBridgingTests(unittest.TestCase):
    """A valve is spliced directly into its pipe with zero gap, by design.
    Confirmed on a real LKAB P&ID (drawing S0000159): a valve's bow-tie
    quad touching a long pipe run transitively merged into that pipe's
    ENTIRE connected network via cluster_primitives' edge-proximity
    union-find — in the worst observed case, one page's whole piping
    collapsed into a single 330-primitive cluster spanning nearly the
    whole page, hiding 6 of the page's 7 valves behind the aspect/
    norm_size filters meant for compact symbols. _is_pipe_run_line +
    cluster_primitives(scale=...) fixes this by never letting a long
    straight line bridge two primitives together."""

    def test_valve_on_long_pipe_does_not_merge_with_pipe_network(self):
        doc, page = _new_page(400, 200)
        shape = page.new_shape()
        # One long horizontal pipe spanning almost the whole page — well
        # over the default scale=10.0's 6x (60pt) "long line" threshold.
        shape.draw_line(fitz.Point(5, 100), fitz.Point(395, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # A valve spliced directly into it at x=100 (self-intersecting
        # quad, touching the pipe with zero gap — same as the real files).
        q = fitz.Quad(fitz.Point(90, 90), fitz.Point(110, 110),
                      fitz.Point(90, 110), fitz.Point(110, 90))
        shape.draw_quad(q)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # A second valve further along the SAME pipe, simulating a real
        # network where many valves share one continuous run.
        q2 = fitz.Quad(fitz.Point(290, 90), fitz.Point(310, 110),
                       fitz.Point(290, 110), fitz.Point(310, 90))
        shape.draw_quad(q2)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()

        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()

        def group_of(pred):
            for g in groups:
                if any(pred(prims[i]) for i in g):
                    return frozenset(g)
            return None

        pipe_group = group_of(lambda p: p['kind'] == 'l')
        valve1_group = group_of(lambda p: p['kind'] == 'qu' and p['bbox'][0] == 90.0)
        valve2_group = group_of(lambda p: p['kind'] == 'qu' and p['bbox'][0] == 290.0)
        self.assertIsNotNone(valve1_group)
        self.assertIsNotNone(valve2_group)
        self.assertNotEqual(pipe_group, valve1_group,
            "a valve must not bridge-merge into the long pipe it's mounted on")
        self.assertNotEqual(valve1_group, valve2_group,
            "two valves on the same long pipe must not merge into one cluster via the pipe")
        self.assertEqual(len(valve1_group), 1, "the valve's own cluster must contain only its quad")

    def test_short_connector_still_merges_normally(self):
        """Guard against overcorrecting: a SHORT line (e.g. a real
        actuator stem or drain stub, well under the long-line threshold)
        between two primitives must still merge them as before."""
        doc, page = _new_page(200, 200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 85), 8)
        shape.finish(color=(0, 0, 0), width=1)
        shape.draw_line(fitz.Point(100, 93), fitz.Point(100, 103))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()

        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1,
            "a short connector line must still bridge two nearby primitives into one cluster")

    def test_long_pipe_still_reported_as_its_own_cluster(self):
        """The excluded pipe primitive isn't dropped — it still comes back
        as its own (harmless, aspect-filtered) singleton cluster."""
        doc, page = _new_page(400, 200)
        shape = page.new_shape()
        shape.draw_line(fitz.Point(5, 100), fitz.Point(395, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()

        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 1)


class LeaderLineResolutionTests(unittest.TestCase):
    def _build_page_with_leader(self):
        doc, page = _new_page(200, 200)
        shape = page.new_shape()
        # Valve bowtie at (100,100).
        shape.draw_polyline([fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        # Bent leader line from the valve down to where the tag will be.
        shape.draw_line(fitz.Point(100, 90), fitz.Point(100, 60))
        shape.finish(color=(0, 0, 0), width=1)
        shape.draw_line(fitz.Point(100, 60), fitz.Point(100, 42))
        shape.finish(color=(0, 0, 0), width=1)
        # A decoy circle far away with no leader line at all.
        shape.draw_circle(fitz.Point(150, 150), 8)
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        page.insert_text(fitz.Point(95, 45), "V-101", fontsize=8)
        return doc, page

    def test_leader_line_resolves_to_correct_symbol_not_decoy(self):
        doc, page = self._build_page_with_leader()
        prims = sg.extract_primitives(page)
        clusters = sg.find_symbol_clusters(page, min_confidence=0.3)

        tag_point = (100, 42)
        cluster, method = sg.resolve_tag_symbol(tag_point, clusters, prims)
        self.assertEqual(method, 'leader')
        self.assertEqual(cluster['bbox'], (90.0, 90.0, 110.0, 110.0),
            "must resolve to the bowtie connected by the leader line, not the closer-by-distance decoy")
        doc.close()

    def test_no_leader_line_falls_back_without_false_positive(self):
        """A tag sitting near a symbol with NO drawn connecting line must
        still resolve (via containment/nearest), but never report method
        'leader' when no such line exists."""
        doc, page = _new_page(200, 200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 100), 8)
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        page.insert_text(fitz.Point(96, 112), "TI-201", fontsize=6)

        prims = sg.extract_primitives(page)
        clusters = sg.find_symbol_clusters(page, min_confidence=0.3)
        tag_point = (99, 115)
        cluster, method = sg.resolve_tag_symbol(tag_point, clusters, prims)
        self.assertIn(method, ('contain', 'nearest'))
        self.assertIsNotNone(cluster)
        doc.close()

    def test_no_symbol_nearby_reports_none_not_hidden(self):
        doc, page = _new_page(1000, 1000)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(50, 50), 8)
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()

        prims = sg.extract_primitives(page)
        clusters = sg.find_symbol_clusters(page, min_confidence=0.3)
        far_point = (900, 900)   # far outside the search radius
        cluster, method = sg.resolve_tag_symbol(far_point, clusters, prims)
        self.assertEqual(method, 'none')
        self.assertIsNone(cluster)
        doc.close()


class RotatedPageCoordinateTests(unittest.TestCase):
    """page.get_drawings() reports coordinates in the page's raw UNROTATED
    mediabox space, never in the rotated space this app treats as "PDF
    space" (matching page.rect, what page.get_pixmap() renders, and what
    pid_viewer's pdf_to_scene()/scene_to_pdf() assume for every marker).
    Found while verifying find_valve_shapes() against a real rotated P&ID
    (182036 Hybrit, /Rotate 270) — a drawn shape near one raw-mediabox
    edge was reported far outside the rendered page's bounds. extract_
    primitives() must apply page.rotation_matrix so bboxes always land
    inside page.rect."""

    def test_primitive_bbox_lands_inside_rotated_page_rect(self):
        doc = fitz.open()
        # Non-square mediabox + 90-degree rotation makes a coordinate-space
        # mix-up impossible to miss: page.rect swaps width and height.
        page = doc.new_page(width=100, height=300)
        page.set_rotation(90)
        shape = page.new_shape()
        # Drawn near one edge of the RAW mediabox (x~10-30, y~270-290) —
        # under the bug this bbox exceeded the rotated page.rect entirely
        # (height only 100pt after rotation).
        shape.draw_rect(fitz.Rect(10, 270, 30, 290))
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()

        prims = sg.extract_primitives(page)
        doc.close()
        self.assertTrue(prims)
        x0, y0, x1, y1 = prims[0]['bbox']
        self.assertTrue(0 <= x0 <= 300 and 0 <= x1 <= 300,
            f"bbox x-range {(x0, x1)} must fall inside the rotated page (0..300)")
        self.assertTrue(0 <= y0 <= 100 and 0 <= y1 <= 100,
            f"bbox y-range {(y0, y1)} must fall inside the rotated page (0..100)")

    def test_unrotated_page_is_unaffected(self):
        """rotation_matrix is the identity when rotation==0 — this must be
        a pure no-op for the (far more common) unrotated case."""
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(90, 90, 110, 110))
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        prims = sg.extract_primitives(page)
        doc.close()
        self.assertEqual(prims[0]['bbox'], (90.0, 90.0, 110.0, 110.0))


class DiagonalLineFilterTests(unittest.TestCase):
    """Piping in a P&ID is drawn strictly horizontally/vertically by
    convention — Anton's observation while reviewing the bow-tie detector:
    a diagonal line segment can therefore only come from a symbol's own
    geometry (a valve's triangle edges), never from a pipe run, a
    title-block grid line, or an instrument-bubble's straight stems.
    find_valve_shapes() uses cluster_features()['has_diagonal'] as an
    extra false-positive filter alongside bowtie_score."""

    def _cluster_diagonal(self, build):
        doc, page = _new_page()
        shape = page.new_shape()
        build(shape)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims)
        result = any(sg.cluster_features(prims, g)['has_diagonal'] for g in groups)
        doc.close()
        return result

    def test_horizontal_line_is_not_diagonal(self):
        def draw(shape):
            shape.draw_line(fitz.Point(80, 100), fitz.Point(120, 100))
            shape.finish(color=(0, 0, 0), width=1)
        self.assertFalse(self._cluster_diagonal(draw))

    def test_vertical_line_is_not_diagonal(self):
        def draw(shape):
            shape.draw_line(fitz.Point(100, 80), fitz.Point(100, 120))
            shape.finish(color=(0, 0, 0), width=1)
        self.assertFalse(self._cluster_diagonal(draw))

    def test_45_degree_line_is_diagonal(self):
        def draw(shape):
            shape.draw_line(fitz.Point(80, 80), fitz.Point(120, 120))
            shape.finish(color=(0, 0, 0), width=1)
        self.assertTrue(self._cluster_diagonal(draw))

    def test_rect_edges_are_not_diagonal(self):
        """An ordinary axis-aligned rectangle ('re', always axis-aligned by
        construction — unlike a self-intersecting 'qu', see
        SelfIntersectingQuadTests) must not register as diagonal."""
        def draw(shape):
            shape.draw_rect(fitz.Rect(90, 90, 110, 110))
            shape.finish(color=(0, 0, 0), width=1)
        self.assertFalse(self._cluster_diagonal(draw))

    def test_bowtie_triangle_edges_are_diagonal(self):
        """The real case this filter is meant to keep: a bow-tie's
        triangle edges are diagonal, so a genuine valve symbol always
        passes this filter."""
        def draw(shape):
            shape.draw_polyline([fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        self.assertTrue(self._cluster_diagonal(draw))


class SelfIntersectingQuadTests(unittest.TestCase):
    """Some CAD sources draw a bow-tie valve body as a SINGLE 'qu' (quad)
    primitive whose 4 corners are ordered so that connecting them in
    sequence (ul->ur->lr->ll->ul) traces two CROSSING diagonals — a
    self-intersecting quad — rather than a simple axis-aligned rectangle.
    Confirmed by direct inspection of real P&ID PDFs (LKAB/Metso, Hybrit,
    Swerim): e.g. one real valve's raw drawn quad was
    ul=(637.9,911.4) ur=(609.5,897.2) lr=(609.5,911.4) ll=(637.9,897.2) —
    ul-ur and lr-ll are the hourglass's two diagonal edges, ur-lr/ll-ul its
    short vertical sides. The dict holding it contains ONLY that one 'qu'
    item (closePath=False, stroke-only) — no accompanying 'l' primitives —
    confirmed by reading the raw get_drawings() dict directly.

    Before the fix, extract_primitives() only kept such a quad's bbox (a
    plain enclosing rectangle), so _prim_is_diagonal and
    _sample_primitive_points both operated on that rectangle instead of
    the real crossing-diagonal shape — has_diagonal came out False and
    bowtie_score came out 0.0 for a genuine, well-formed valve symbol,
    causing find_valve_shapes() to reject it outright."""

    def _self_intersecting_quad_page(self):
        doc, page = _new_page()
        shape = page.new_shape()
        # ul, ur, lr, ll in DRAWN order — ul-ur and lr-ll cross, matching
        # the real valve quad above (mirrored/scaled to a 20x20 test box).
        q = fitz.Quad(fitz.Point(90, 90), fitz.Point(110, 110),
                      fitz.Point(90, 110), fitz.Point(110, 90))
        shape.draw_quad(q)
        # closePath=False, no fill — matches the real files exactly (a
        # fill or closePath makes PyMuPDF also emit 4 separate 'l' items
        # for the outline, which would trivially pass the OLD, buggy code
        # too via its already-working 'l'-kind handling and defeat the
        # point of this test).
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        return doc, page

    def test_corners_kept_in_self_intersecting_drawn_order(self):
        doc, page = self._self_intersecting_quad_page()
        prims = sg.extract_primitives(page)
        doc.close()
        quads = [p for p in prims if p['kind'] == 'qu']
        self.assertEqual(len(quads), 1)
        self.assertEqual(quads[0]['corners'],
            [(90.0, 90.0), (110.0, 110.0), (110.0, 90.0), (90.0, 110.0)])

    def test_self_intersecting_quad_edge_is_diagonal(self):
        """Regression test: has_diagonal must be True for this quad even
        though rects/quads are usually axis-aligned — its real ul-ur edge
        genuinely runs at 45 degrees, unlike an ordinary rectangle's."""
        doc, page = self._self_intersecting_quad_page()
        prims = sg.extract_primitives(page)
        doc.close()
        self.assertTrue(sg._prim_is_diagonal(prims[0]))

    def test_self_intersecting_quad_scores_high_bowtie(self):
        """The core regression test: sampling this quad's actual drawn
        edges (not its bbox perimeter) must reproduce the hourglass pinch
        bowtie_score looks for. Before the fix this scored exactly 0.0."""
        doc, page = self._self_intersecting_quad_page()
        prims = sg.extract_primitives(page)
        doc.close()
        score = sg.bowtie_score(prims, [0])
        self.assertGreaterEqual(score, 0.5,
            "a self-intersecting bow-tie quad must score as a valve, not a plain rectangle")

    def test_ordinary_axis_aligned_quad_is_unaffected(self):
        """Same drawing call, but with corners in ordinary (non-crossing)
        order — must behave exactly like a plain rectangle: no diagonal,
        low bowtie score. Guards against the fix over-firing on every
        quad regardless of corner order."""
        doc, page = _new_page()
        shape = page.new_shape()
        q = fitz.Quad(fitz.Point(90, 90), fitz.Point(110, 90),
                      fitz.Point(90, 110), fitz.Point(110, 110))
        shape.draw_quad(q)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        doc.close()
        self.assertFalse(sg._prim_is_diagonal(prims[0]))
        self.assertLess(sg.bowtie_score(prims, [0]), 0.5)


class ClusterCoreTests(unittest.TestCase):
    """_cluster_core() — grows a compact "symbol core" outward from a
    cluster's most symbol-like seed, stopping before absorbing an attached
    appendage (actuator stem, drain stub) that would blow the aspect ratio
    past a bare bow-tie's own. Found necessary on a real LKAB P&ID: a
    valve's cluster correctly also contains a short connecting stem (same
    physical assembly — see PipeNetworkBridgingTests for why they merge),
    but scoring/filtering the WHOLE cluster hid 6 of 7 real valves behind
    filters meant for a compact symbol."""

    def _valve_with_stem_page(self):
        doc, page = _new_page(200, 200)
        shape = page.new_shape()
        q = fitz.Quad(fitz.Point(90, 90), fitz.Point(110, 110),
                      fitz.Point(90, 110), fitz.Point(110, 90))
        shape.draw_quad(q)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # A stem long enough that quad+stem together exceed aspect 3.0
        # (20pt wide, 70pt tall combined -> aspect 3.5), but still short
        # of cluster_primitives' own "long pipe run" threshold (60pt at
        # the default scale=10.0) so it still bridges into one cluster —
        # same proportions as the real LKAB valve+drain-stub assembly
        # that motivated this.
        shape.draw_line(fitz.Point(100, 110), fitz.Point(100, 160))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        return doc, page

    def test_core_excludes_appendage_that_would_blow_aspect(self):
        doc, page = self._valve_with_stem_page()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1, "the quad and stem must still merge into one cluster")
        core = sg._cluster_core(prims, groups[0])
        self.assertEqual(len(core), 1, "the core must be just the quad, excluding the stem")
        core_bbox = sg._group_bbox(prims, core)
        self.assertEqual(core_bbox, (90.0, 90.0, 110.0, 110.0))

    def test_core_scores_as_valve_even_though_full_cluster_would_not(self):
        doc, page = self._valve_with_stem_page()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        full_feats = sg.cluster_features(prims, groups[0])
        self.assertGreater(full_feats['aspect'], 3.0,
            "sanity check: the full cluster (quad+stem) must exceed the compact-symbol aspect ceiling")
        core = sg._cluster_core(prims, groups[0])
        core_feats = sg.cluster_features(prims, core)
        self.assertLessEqual(core_feats['aspect'], 3.0)
        self.assertGreaterEqual(sg.bowtie_score(prims, core), 0.5)

    def test_two_triangle_bowtie_core_is_unchanged(self):
        """Guard against overcorrecting: a normal two-triangle bow-tie
        (both triangles sharing an apex, no appendage) must keep ALL its
        primitives in the core — nothing should ever get trimmed from a
        bare, well-formed bow-tie."""
        doc, page = _new_page()
        shape = page.new_shape()
        shape.draw_polyline([fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1)
        core = sg._cluster_core(prims, groups[0])
        self.assertEqual(set(core), set(groups[0]))

    def test_text_glyph_fragment_excluded_from_core(self):
        """Found on a real LKAB P&ID: a valve's own tag text ended up
        double-rendered as tiny vector glyph strokes close enough to
        bridge into the valve's cluster (see cluster_primitives),
        diluting bowtie_score's point cloud until an otherwise-identical,
        correctly-shaped valve scored 0.0 instead of ~0.77. Unlike the
        stem/drain-stub appendage above, a small glyph fragment doesn't
        push the aspect ratio past the limit on its own — the aspect
        check alone doesn't catch it. Only an explicit text_bboxes
        exclusion does."""
        doc, page = _new_page()
        shape = page.new_shape()
        q = fitz.Quad(fitz.Point(90, 90), fitz.Point(110, 110),
                      fitz.Point(90, 110), fitz.Point(110, 90))
        shape.draw_quad(q)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # A tiny stroke sitting right at the quad's edge (touches within
        # cluster_primitives' gap) and inside where the tag text below
        # will report its own bbox.
        shape.draw_line(fitz.Point(89, 96), fitz.Point(90.5, 98))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        page.insert_text(fitz.Point(70, 100), "TAG1", fontsize=8)

        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        text_bboxes = sg._text_word_bboxes(page)
        doc.close()
        self.assertEqual(len(groups), 1,
            "sanity check: the glyph fragment must actually touch the quad's cluster")
        self.assertEqual(len(groups[0]), 2)

        core_without_filter = sg._cluster_core(prims, groups[0])
        self.assertEqual(len(core_without_filter), 2,
            "sanity check: without text_bboxes, the tiny fragment doesn't blow the aspect ratio "
            "and would normally join the core")

        core = sg._cluster_core(prims, groups[0], text_bboxes=text_bboxes)
        self.assertEqual(len(core), 1, "the glyph fragment must be excluded from the core")
        self.assertEqual(prims[core[0]]['kind'], 'qu')


class ClusterCorePinchGuardTests(unittest.TestCase):
    """_cluster_core()'s pinch-signal guard — found on a real LKAB P&ID: a
    vertically-mounted valve's own connecting stem was short enough that
    quad+stem landed at aspect 2.99, just inside _CORE_ASPECT_LIMIT (3.0),
    so the aspect check alone let it join the core — yet the stem's
    constant-x sample points fell inside bowtie_score's "wide open end"
    slices and dropped the score from ~0.77 to 0.0, hiding a real valve.
    """

    def _vertical_valve_with_short_stem_page(self, stem_len):
        doc, page = _new_page()
        shape = page.new_shape()
        # A vertically-pinched bow-tie (10 wide, 20 tall) — same
        # self-intersecting-quad construction as ClusterCoreTests, but
        # built so the caps are horizontal (top/bottom) and the pinch
        # runs along Y, matching the real vertically-mounted valve.
        q = fitz.Quad(fitz.Point(90, 110), fitz.Point(100, 90),
                      fitz.Point(90, 90), fitz.Point(100, 110))
        shape.draw_quad(q)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # The connecting pipe stub, leading into the valve from above.
        shape.draw_line(fitz.Point(95, 90), fitz.Point(95, 90 - stem_len))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        return doc, page

    def test_short_stem_under_aspect_limit_still_corrupts_bowtie_score(self):
        """Sanity check reproducing the real bug: a stem short enough to
        keep aspect under 3.0 (here 9.9pt, giving 29.9/10 = 2.99) still
        collapses bowtie_score to ~0 once merged with the quad."""
        doc, page = self._vertical_valve_with_short_stem_page(stem_len=9.9)
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1, "stem must still bridge into the valve's cluster")
        full_feats = sg.cluster_features(prims, groups[0])
        self.assertLessEqual(full_feats['aspect'], 3.0,
            "sanity check: aspect alone would NOT reject this stem")
        self.assertLess(sg.bowtie_score(prims, groups[0]), 0.5,
            "sanity check: the stem still corrupts the pinch profile enough to fail "
            "find_valve_shapes' own min_bowtie_score filter, despite the aspect passing")

    def test_core_excludes_stem_even_though_aspect_stays_in_limit(self):
        doc, page = self._vertical_valve_with_short_stem_page(stem_len=9.9)
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        core = sg._cluster_core(prims, groups[0])
        self.assertEqual(len(core), 1, "the stem must be excluded despite aspect staying under the limit")
        self.assertEqual(prims[core[0]]['kind'], 'qu')
        self.assertGreaterEqual(sg.bowtie_score(prims, core), 0.5)

    def test_guard_does_not_fire_when_core_is_not_already_a_good_bowtie(self):
        """Guard against overcorrecting: the pinch-signal veto must only
        kick in for a cluster that already reads as a decent bow-tie —
        otherwise it would interfere with ordinary, non-valve compact-
        symbol growth that never had a meaningful bowtie_score to
        protect in the first place."""
        doc, page = _new_page()
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(90, 90, 100, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=True)
        shape.draw_line(fitz.Point(95, 100), fitz.Point(95, 108))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1)
        core = sg._cluster_core(prims, groups[0])
        self.assertEqual(set(core), set(groups[0]),
            "a plain rect+stub (not a bow-tie) must grow normally, unaffected by the pinch guard")

    def test_guard_fires_even_when_corruption_happens_at_low_aspect(self):
        """Regression test: an earlier version of this guard only checked
        the pinch signal once aspect exceeded a fixed floor (added to cut
        down on bowtie_score calls). That let a real Gryaab valve's score
        collapse silently through several growth steps that all stayed
        under the floor — best_score never updated, so a MUCH later step
        got compared against a stale, too-generous reference and the
        guard let the corruption through. Reproduced in miniature here:
        two stem segments added one at a time, each keeping aspect at or
        under 2.0 the whole way, where only the SECOND one collapses the
        score — the guard must catch it at that exact step, not defer
        based on aspect."""
        doc, page = _new_page()
        shape = page.new_shape()
        q = fitz.Quad(fitz.Point(90, 110), fitz.Point(110, 90),
                      fitz.Point(90, 90), fitz.Point(110, 110))
        shape.draw_quad(q)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(95, 90), fitz.Point(95, 80))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(95, 80), fitz.Point(95, 70))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1, "both stem segments must still bridge into the cluster")
        self.assertLess(sg.bowtie_score(prims, groups[0]), 0.5,
            "sanity check: both stems together must corrupt the full cluster's score")

        core = sg._cluster_core(prims, groups[0])
        self.assertGreaterEqual(sg.bowtie_score(prims, core), 0.5,
            "the corrupting second stem must be excluded regardless of aspect staying low")


class ClusterCoresPeelingTests(unittest.TestCase):
    """_cluster_cores() — peels multiple compact symbol cores out of one
    cluster_primitives() group, instead of assuming exactly one real
    symbol per cluster (see _cluster_core, which alone only recovers the
    first). Found necessary on a real LKAB P&ID: a shared instrument
    signal wire bridged a valve, a pump, and several instrument bubbles
    roughly 500pt apart into ONE cluster."""

    def _two_bridged_valves_page(self):
        doc, page = _new_page(200, 300)
        shape = page.new_shape()
        # Valve A: a vertically-pinched bow-tie at (90,90)-(110,110).
        qa = fitz.Quad(fitz.Point(90, 110), fitz.Point(110, 90),
                       fitz.Point(90, 90), fitz.Point(110, 110))
        shape.draw_quad(qa)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # A connector well under the "long pipe run" exclusion threshold
        # (60pt at scale=10.0) so it still bridges the two valves into
        # one cluster — same proportions as the real LKAB assembly.
        shape.draw_line(fitz.Point(100, 110), fitz.Point(100, 160))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # Valve B: an identical bow-tie 70pt further down.
        qb = fitz.Quad(fitz.Point(90, 180), fitz.Point(110, 160),
                       fitz.Point(90, 160), fitz.Point(110, 180))
        shape.draw_quad(qb)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        return doc, page

    def test_two_bridged_valves_merge_into_one_raw_cluster(self):
        doc, page = self._two_bridged_valves_page()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1,
            "sanity check: the connector must actually bridge both valves into one cluster")

    def test_single_core_only_recovers_one_of_the_two_valves(self):
        """Sanity check reproducing the original limitation: _cluster_core
        alone stops after the first valve (the connector already blows
        the aspect ratio past the limit), leaving the second valve
        completely unrepresented in its result."""
        doc, page = self._two_bridged_valves_page()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        core = sg._cluster_core(prims, groups[0])
        core_bbox = sg._group_bbox(prims, core)
        self.assertNotEqual(core_bbox, (90.0, 90.0, 110.0, 180.0),
            "sanity check: a single core cannot legitimately span both valves")
        # Whichever valve it kept, the other valve's quad is NOT in it.
        self.assertEqual(len(core), 1)

    def test_cluster_cores_recovers_both_valves(self):
        doc, page = self._two_bridged_valves_page()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        cores = sg._cluster_cores(prims, groups[0])
        self.assertEqual(len(cores), 2, "both valves must be recovered as separate cores")
        bboxes = {sg._group_bbox(prims, c) for c in cores}
        self.assertEqual(bboxes, {(90.0, 90.0, 110.0, 110.0), (90.0, 160.0, 110.0, 180.0)})
        for core in cores:
            self.assertGreaterEqual(sg.bowtie_score(prims, core), 0.5)

    def test_ordinary_single_symbol_cluster_yields_one_core(self):
        """Guard against overcorrecting: a normal, non-bridged bow-tie
        must still yield exactly one core, not get needlessly split."""
        doc, page = _new_page()
        shape = page.new_shape()
        q = fitz.Quad(fitz.Point(90, 110), fitz.Point(110, 90),
                      fitz.Point(90, 90), fitz.Point(110, 110))
        shape.draw_quad(q)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        cores = sg._cluster_cores(prims, groups[0])
        self.assertEqual(len(cores), 1)
        self.assertEqual(set(cores[0]), set(groups[0]))

    def test_core_growth_is_capped_by_member_count_not_just_shape(self):
        """Regression test: found on a real Hybrit P&ID where a ~540-line
        cluster was a large non-symbol graphic that happened to stay
        physically compact (under _CORE_MAX_NORM_SIZE) and roughly
        square (under _CORE_ASPECT_LIMIT) while growing — neither limit
        caught it, and _touches_any's cost scales with the CURRENT core
        size, so absorbing all ~540 members took 28+ seconds. Reproduced
        here with a seed quad surrounded by a dense hatch of 200 tiny
        touching line segments confined to a small, square area (built
        directly as primitive dicts — real PDF rendering of 200 shapes
        would make the test itself slow) — aspect and size both stay
        low throughout, so only _CORE_MAX_MEMBERS stops the growth."""
        prims = [{
            'kind': 'qu', 'bbox': (90.0, 90.0, 110.0, 110.0),
            'p0': (90.0, 90.0), 'p1': (110.0, 110.0),
            'closed': True, 'filled': False,
            'corners': [(90.0, 110.0), (110.0, 90.0), (90.0, 90.0), (110.0, 110.0)],
            'width': 1.0, 'source': 0,
        }]
        # A tight grid of short touching hatch segments confined to
        # (85,85)-(115,115) — stays compact and square-ish no matter how
        # many are absorbed.
        n = 0
        for row in range(20):
            y = 85.0 + row * 1.5
            for col in range(10):
                x = 85.0 + col * 3.0
                n += 1
                prims.append({
                    'kind': 'l', 'bbox': (x, y, x + 3.0, y),
                    'p0': (x, y), 'p1': (x + 3.0, y),
                    'closed': False, 'filled': False, 'width': 1.0, 'source': n,
                })
        group = list(range(len(prims)))
        core = sg._cluster_core(prims, group)
        self.assertLessEqual(len(core), sg._CORE_MAX_MEMBERS,
            "growth must stop at the member cap even though aspect/size never trigger")


class ClosedShapeFilterTests(unittest.TestCase):
    """Found on a real LKAB P&ID: the vector-drawn letter "M" (a motor
    label rendered as outline strokes rather than searchable text) is an
    open zigzag — two verticals meeting at a point in the middle — that
    reads as a textbook wide-narrow-wide silhouette (bowtie_score ~0.85)
    despite never being a closed shape, unlike every real valve on that
    page (always a closed quad or closed/filled triangle paths).
    cluster_features()['has_closed_or_filled'] tells them apart."""

    def test_open_zigzag_has_no_closed_or_filled_member(self):
        doc, page = _new_page()
        shape = page.new_shape()
        # Two verticals connected by a V in the middle, all open strokes —
        # same shape as the real "M" glyph that produced a false positive.
        shape.draw_line(fitz.Point(90, 110), fitz.Point(90, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(90, 90), fitz.Point(100, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(100, 100), fitz.Point(110, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(110, 90), fitz.Point(110, 110))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()

        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1)
        feats = sg.cluster_features(prims, groups[0])
        # Sanity check this fixture reproduces the real false positive's
        # shape signature before asserting the distinguishing feature.
        self.assertGreaterEqual(sg.bowtie_score(prims, groups[0]), 0.5)
        self.assertFalse(feats['has_closed_or_filled'],
            "an open zigzag of unclosed line strokes must never register as closed/filled")

    def test_real_bowtie_has_closed_or_filled_member(self):
        """A genuine self-intersecting quad valve (always closed=True, see
        extract_primitives) must keep passing this filter."""
        doc, page = _new_page()
        shape = page.new_shape()
        q = fitz.Quad(fitz.Point(90, 90), fitz.Point(110, 110),
                      fitz.Point(90, 110), fitz.Point(110, 90))
        shape.draw_quad(q)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        doc.close()
        feats = sg.cluster_features(prims, [0])
        self.assertTrue(feats['has_closed_or_filled'])


class ClosedLoopFilterTests(unittest.TestCase):
    """_has_closed_loop() — replaces has_closed_or_filled as the valve
    filter's "is this really a closed shape" check. Found necessary on
    real Sunpine/Swerim/ITS P&IDs: they draw a valve's two triangles as
    separate UNCLOSED 'l' segments with no fill (closePath=False, no
    fill color) — has_closed_or_filled rejected these as false negatives
    (confirmed: roughly a third to two thirds of otherwise-valid bow-tie
    candidates on sampled pages from those files used this convention).
    _has_closed_loop instead checks whether the segments' endpoints
    trace an actual cycle, tolerant of the small real-world gap (~3.5pt
    on a real Swerim valve) some exports leave at one corner."""

    def test_open_stroke_triangle_with_real_world_gap_closes(self):
        """Reproduces a real Swerim valve's exact triangle coordinates:
        3 unclosed 'l' segments whose supposed shared corner is off by
        3.48pt (192, matching the source dict's own coordinates) rather
        than landing exactly on top of each other."""
        doc, page = _new_page()
        shape = page.new_shape()
        shape.draw_line(fitz.Point(93.8, 15.32), fitz.Point(97.28, 15.32))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(97.28, 15.32), fitz.Point(93.8, 22.4))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(97.28, 22.4), fitz.Point(93.8, 15.32))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1)
        self.assertFalse(sg.cluster_features(prims, groups[0])['has_closed_or_filled'],
            "sanity check: none of these 3 segments is individually closed/filled")
        self.assertTrue(sg._has_closed_loop(prims, groups[0]),
            "3 unclosed segments whose endpoints nearly coincide must still register as a closed loop")

    def test_open_zigzag_does_not_close(self):
        """The real false positive this filter must still reject: the
        vector-drawn "M" glyph's open zigzag (see ClosedShapeFilterTests)
        — its loose ends are 8.6pt apart, well beyond the tolerance that
        correctly closes the Swerim valve's 3.48pt gap above."""
        doc, page = _new_page()
        shape = page.new_shape()
        shape.draw_line(fitz.Point(90, 110), fitz.Point(90, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(90, 90), fitz.Point(100, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(100, 100), fitz.Point(110, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(110, 90), fitz.Point(110, 110))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1)
        self.assertFalse(sg._has_closed_loop(prims, groups[0]),
            "an open zigzag must never register as a closed loop, at any real gap size")

    def test_pipe_corner_bend_does_not_close(self):
        """A simple two-segment corner (an ordinary pipe elbow) must not
        register as closed — guards against the tolerance merge being so
        loose it treats any two nearby segments as a loop."""
        doc, page = _new_page()
        shape = page.new_shape()
        shape.draw_line(fitz.Point(50, 50), fitz.Point(50, 70))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(50, 70), fitz.Point(70, 70))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        doc.close()
        self.assertFalse(sg._has_closed_loop(prims, [0, 1]))

    def test_closed_quad_uses_fast_path(self):
        """A real self-intersecting quad (closed=True by construction,
        see extract_primitives) must pass via the fast path without
        needing the graph walk."""
        doc, page = _new_page()
        shape = page.new_shape()
        q = fitz.Quad(fitz.Point(90, 90), fitz.Point(110, 110),
                      fitz.Point(90, 110), fitz.Point(110, 90))
        shape.draw_quad(q)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        doc.close()
        self.assertTrue(sg._has_closed_loop(prims, [0]))


def _draw_polygon_circle(shape, cx, cy, r, n_sides=32):
    """Draw a closed n-sided line polygon approximating a circle —
    confirmed by direct inspection to be exactly how real Sunpine/Swerim
    P&IDs draw every circular symbol (measured: 32 segments per circle,
    zero bezier curves anywhere on multiple sampled pages of each file),
    unlike the bezier-curve convention LKAB/Gryaab/ITS use."""
    import math
    pts = [fitz.Point(cx + r * math.cos(2 * math.pi * k / n_sides),
                       cy + r * math.sin(2 * math.pi * k / n_sides))
           for k in range(n_sides)]
    for k in range(n_sides):
        shape.draw_line(pts[k], pts[(k + 1) % n_sides])
        shape.finish(color=(0, 0, 0), width=1, closePath=False)


class PolygonCircleTests(unittest.TestCase):
    """_polygon_circle_islands()/_circle_islands() — pump/instrument
    detection must work whether a circle is drawn as true bezier curves
    (LKAB/Gryaab/ITS) or as a many-sided line polygon (Sunpine/Swerim,
    confirmed: extract_primitives found ZERO 'c' kind primitives
    anywhere on multiple sampled pages of each file — every circular
    symbol, including pump bodies and instrument bubbles, is
    approximated with straight segments instead)."""

    def test_polygon_circle_is_found_as_an_island(self):
        doc, page = _new_page()
        shape = page.new_shape()
        _draw_polygon_circle(shape, 100, 100, 20)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1)
        islands = sg._polygon_circle_islands(prims, groups[0])
        self.assertEqual(len(islands), 1)
        self.assertEqual(len(islands[0]), 32)

    def test_few_sided_polygon_is_not_a_circle(self):
        """A triangle or rectangle (a valve bowtie, a title-block frame)
        must never register as a circle — _POLYGON_CIRCLE_MIN_SIDES
        guards against a few-sided polygon looking "round enough"."""
        doc, page = _new_page()
        shape = page.new_shape()
        _draw_polygon_circle(shape, 100, 100, 20, n_sides=4)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(sg._polygon_circle_islands(prims, groups[0]), [])

    def test_internal_decoration_does_not_break_circularity_check(self):
        """Found necessary on a real Swerim P&ID: a temperature-sensor
        circle had an internal cross-tick mark (4 short lines near the
        center) bridged into the SAME connected component as the 32
        boundary segments — without filtering by segment length, the
        cross-tick's near-center points skew the circularity check
        (points-from-centroid variance) enough to reject a genuine
        circle."""
        doc, page = _new_page()
        shape = page.new_shape()
        _draw_polygon_circle(shape, 100, 100, 20)
        # A small cross-tick mark at the center, touching the boundary
        # circle at one point so it joins the same cluster. Kept much
        # shorter than the boundary's own ~3.9pt segments (a 20pt-radius,
        # 32-sided polygon) so the length filter has an unambiguous
        # signal to work with in this test.
        shape.draw_line(fitz.Point(100, 100), fitz.Point(100, 80))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(99.5, 80.5), fitz.Point(100.5, 80.5))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1, "the tick mark must bridge into the circle's own cluster")
        islands = sg._polygon_circle_islands(prims, groups[0])
        self.assertEqual(len(islands), 1)
        self.assertEqual(len(islands[0]), 32,
            "the circularity check must find just the 32 boundary segments, ignoring the tick mark")

    def test_pump_detected_via_polygon_circle(self):
        """A polygon-circle with a diagonal impeller mark inside it must
        be found as a pump, exactly like a bezier-curve one."""
        doc, page = _new_page()
        shape = page.new_shape()
        _draw_polygon_circle(shape, 100, 100, 20)
        shape.draw_line(fitz.Point(100, 86), fitz.Point(120, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(120, 100), fitz.Point(100, 114))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        found = sg.pump_shapes_in_cluster(prims, groups[0])
        self.assertEqual(len(found), 1)

    def test_instrument_detected_via_polygon_circle_capsule(self):
        """Two polygon-circles bridged by a horizontal divider (a
        capsule's two end-caps, drawn as line polygons) must be found
        as an instrument, exactly like the bezier-curve version."""
        doc, page = _new_page()
        shape = page.new_shape()
        _draw_polygon_circle(shape, 85, 100, 10)
        _draw_polygon_circle(shape, 115, 100, 10)
        shape.draw_line(fitz.Point(85, 90), fitz.Point(115, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(85, 110), fitz.Point(115, 110))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(75, 100), fitz.Point(125, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1, "the divider must bridge both polygon-circle caps into one cluster")
        found = sg.instrument_shapes_in_cluster(prims, groups[0])
        self.assertEqual(len(found), 1)

    def test_curve_and_polygon_circles_both_found_in_same_call(self):
        """_circle_islands must find both conventions at once — a file
        is never assumed to use exclusively one or the other at this
        level (that assumption is only used as a page-level PERFORMANCE
        gate in find_symbol_clusters, not a correctness one here)."""
        doc, page = _new_page(300, 200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(80, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        _draw_polygon_circle(shape, 220, 100, 20)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        all_islands = []
        for g in groups:
            all_islands.extend(sg._circle_islands(prims, g))
        self.assertEqual(len(all_islands), 2)

    def test_large_non_circular_cluster_stays_fast(self):
        """Regression test: found on a real Hybrit P&ID where a page had
        zero curve primitives (triggering the line-polygon search on
        every cluster) and one cluster was a ~3500-segment pipe-network
        zigzag, not a circle at all. The original implementation did a
        raw O(n^2) pairwise scan to find connected components among a
        cluster's own line segments, which took minutes on that cluster
        alone. Reproduced here with a 400-segment zigzag chain (built
        directly as primitive dicts — real PDF rendering of that many
        shapes would make the test itself slow) plus one genuine
        32-sided polygon circle bridged onto the end of the chain. Must
        both run in well under a second and still find the one real
        circle among the noise."""
        prims = []
        # A long zigzag of touching line segments — one connected
        # component, never anywhere close to forming a circle.
        x = 0.0
        for i in range(400):
            x0, x1 = x, x + 2.0
            y0, y1 = (0.0, 5.0) if i % 2 == 0 else (5.0, 0.0)
            prims.append({
                'kind': 'l', 'bbox': (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                'p0': (x0, y0), 'p1': (x1, y1),
                'closed': False, 'filled': False, 'width': 1.0, 'source': i,
            })
            x = x1
        # One genuine 32-sided circle, bridged onto the end of the chain.
        cx, cy, r, n_sides = x + 1.0, 20.0, 5.0, 32
        for k in range(n_sides):
            a0 = 2 * math.pi * k / n_sides
            a1 = 2 * math.pi * (k + 1) / n_sides
            p0 = (cx + r * math.cos(a0), cy + r * math.sin(a0))
            p1 = (cx + r * math.cos(a1), cy + r * math.sin(a1))
            prims.append({
                'kind': 'l', 'bbox': (min(p0[0], p1[0]), min(p0[1], p1[1]),
                                       max(p0[0], p1[0]), max(p0[1], p1[1])),
                'p0': p0, 'p1': p1,
                'closed': False, 'filled': False, 'width': 1.0, 'source': 400 + k,
            })
        group = list(range(len(prims)))

        t0 = time.time()
        islands = sg._polygon_circle_islands(prims, group)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 3.0,
            f"took {elapsed:.1f}s — the O(n^2) pairwise scan regressed")
        self.assertEqual(len(islands), 1, "the one genuine circle must still be found")
        self.assertEqual(len(islands[0]), n_sides)


class PumpShapeTests(unittest.TestCase):
    """pump_shapes_in_cluster() identifies pump symbols: a round, closed
    body (drawn as a circle via bezier curves) with a diagonal impeller
    mark inside it — confirmed by direct inspection of real LKAB and
    Gryaab P&IDs, both showing a circle with two ~30pt diagonal lines
    (in a ~42.5pt circle, ~71% of its diameter) meeting at a point on
    the circle's right rim."""

    def _pump_page(self, cx=100, cy=100, r=20):
        """A circle with two diagonal lines meeting at its right rim —
        proportioned like the real LKAB pump (diagonals ~70% of the
        circle's diameter), inside its own bbox."""
        doc, page = _new_page(200, 200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(cx, cy), r)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(cx, cy - 0.7 * r), fitz.Point(cx + r, cy))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(cx + r, cy), fitz.Point(cx, cy + 0.7 * r))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        return doc, page

    def test_circle_with_impeller_diagonal_is_a_pump(self):
        doc, page = self._pump_page()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1, "circle + diagonals must cluster together (they touch)")
        found = sg.pump_shapes_in_cluster(prims, groups[0])
        self.assertEqual(len(found), 1)
        x0, y0, x1, y1 = found[0]
        self.assertAlmostEqual(x0, 80.0, delta=1.0)
        self.assertAlmostEqual(x1, 120.0, delta=1.0)

    def test_plain_circle_is_not_a_pump(self):
        """An instrument bubble (a plain circle, no diagonal) must not
        register as a pump — confirmed against real PI/LC/LI bubbles."""
        doc, page = _new_page()
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        found = sg.pump_shapes_in_cluster(prims, groups[0])
        self.assertEqual(found, [])

    def test_tiny_diagonal_fragment_inside_circle_is_not_a_pump(self):
        """Found on a real Gryaab P&ID: a plain motor ("M") circle with
        no impeller mark still false-positived, because the letter "M"
        (drawn as a vector glyph, not searchable text — see
        ClosedShapeFilterTests) has its own tiny diagonal strokes that
        happen to fall inside the circle. A real impeller diagonal spans
        a large fraction of the circle's own diameter; a stray glyph
        fragment does not — _PUMP_DIAGONAL_MIN_FRACTION tells them apart."""
        doc, page = _new_page()
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # A tiny (1.5pt) diagonal fragment well under 40% of the 40pt
        # circle diameter — proportioned like the real glyph artifact
        # (observed: ~0.5pt fragment inside a ~14pt circle, ~3.5%).
        shape.draw_line(fitz.Point(99.0, 99.0), fitz.Point(100.5, 100.5))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        found = sg.pump_shapes_in_cluster(prims, groups[0])
        self.assertEqual(found, [],
            "a diagonal fragment far shorter than the circle's own diameter must not count as an impeller mark")

    def test_two_pumps_merged_into_one_cluster_both_found(self):
        """Found on a real Gryaab P&ID: a busy process area with no long
        pipe run to break the chain (see _is_pipe_run_line) merged an
        entire neighborhood — including TWO separate pumps — into one
        290-primitive cluster. pump_shapes_in_cluster must return both,
        not just the first."""
        doc, page = _new_page(200, 200)
        shape = page.new_shape()
        for cx in (60, 140):
            shape.draw_circle(fitz.Point(cx, 100), 20)
            shape.finish(color=(0, 0, 0), width=1, closePath=False)
            shape.draw_line(fitz.Point(cx, 86), fitz.Point(cx + 20, 100))
            shape.finish(color=(0, 0, 0), width=1, closePath=False)
            shape.draw_line(fitz.Point(cx + 20, 100), fitz.Point(cx, 114))
            shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # A short (20pt, well under the "long pipe run" exclusion
        # threshold) connecting line bridges the two circles into one
        # cluster — same as the real Gryaab case's short pipe stub.
        shape.draw_line(fitz.Point(80, 100), fitz.Point(120, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1, "sanity check: the connector must bridge both pumps into one cluster")
        found = sg.pump_shapes_in_cluster(prims, groups[0])
        self.assertEqual(len(found), 2)


class InstrumentShapeTests(unittest.TestCase):
    """instrument_shapes_in_cluster() identifies instrument "bubble"
    bodies: a circle or elongated capsule/"stadium" shape (confirmed by
    direct inspection of a real LKAB P&ID: two semicircle end-caps 28.3pt
    apart, joined by straight top/bottom edges — NOT within
    _CLUSTER_GAP of each other directly, unlike a pump's single
    continuous circle) with a horizontal divider line at its vertical
    midpoint — the ISA-5.1 convention for a shared-display (panel-
    mounted) instrument."""

    def _capsule_page(self, with_divider=True):
        doc, page = _new_page(200, 200)
        shape = page.new_shape()
        # Two small circles standing in for the two semicircle end-caps
        # of a real capsule — far enough apart (10pt gap between their
        # own edges) that _curve_islands treats them as separate
        # islands, exactly like the real ~28.3pt-apart caps do. The
        # connecting lines' endpoints land exactly on each circle's own
        # top/bottom/side point (a full circle has no flat edge to
        # attach to, unlike a real semicircle cap) so they genuinely
        # touch within _CLUSTER_GAP.
        shape.draw_circle(fitz.Point(85, 100), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_circle(fitz.Point(115, 100), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(85, 90), fitz.Point(115, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(85, 110), fitz.Point(115, 110))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        if with_divider:
            shape.draw_line(fitz.Point(75, 100), fitz.Point(125, 100))
            shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        return doc, page

    def test_capsule_with_midline_divider_is_an_instrument(self):
        doc, page = self._capsule_page(with_divider=True)
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(groups), 1, "the divider must bridge both caps into one cluster")
        found = sg.instrument_shapes_in_cluster(prims, groups[0])
        self.assertEqual(len(found), 1)
        x0, y0, x1, y1 = found[0]
        self.assertAlmostEqual(x0, 75.0, delta=1.0)
        self.assertAlmostEqual(x1, 125.0, delta=1.0)

    def test_undivided_capsule_is_not_an_instrument(self):
        """A field-mounted instrument (no divider — see a real LKAB
        "LI"/second-"PI" bubble) must not be reported. Deliberately not
        detected by design: a plain undivided bubble is geometrically
        identical to a motor label circle or plenty of other things —
        see the module docstring for why only the divided case is
        unambiguous enough to target."""
        doc, page = self._capsule_page(with_divider=False)
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        found = sg.instrument_shapes_in_cluster(prims, groups[0])
        self.assertEqual(found, [])

    def test_capsule_outline_edges_are_not_mistaken_for_a_divider(self):
        """Regression test for the bug this exact fixture caught during
        development: the capsule's own top/bottom outline edges connect
        the same two islands and span the same full width as a genuine
        divider — only _INSTRUMENT_DIVIDER_MIDLINE_TOLERANCE (requiring
        the line to sit near the vertical midpoint, not at the top/
        bottom extremes) tells them apart. Covered implicitly by
        test_undivided_capsule_is_not_an_instrument above, named
        explicitly here so the reason is unmistakable if it regresses."""
        doc, page = self._capsule_page(with_divider=False)
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        found = sg.instrument_shapes_in_cluster(prims, groups[0])
        self.assertEqual(found, [], "the capsule's own top/bottom edges must never count as a divider")

    def test_pump_circle_with_through_line_is_not_an_instrument(self):
        """Found on a real Gryaab P&ID: a pump's connecting pipe is
        drawn as ONE continuous horizontal line straight through the
        circle's own vertical midpoint (not stopping at its rim) —
        geometrically identical to a genuine instrument divider at that
        same midpoint. Only the pump's own diagonal impeller mark tells
        them apart; a real instrument bubble never has one."""
        doc, page = _new_page()
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # A line straight through the middle (the "pipe"), PLUS the
        # pump's own diagonal impeller mark.
        shape.draw_line(fitz.Point(70, 100), fitz.Point(130, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(100, 86), fitz.Point(120, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(120, 100), fitz.Point(100, 114))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims, scale=10.0)
        doc.close()
        self.assertEqual(len(sg.pump_shapes_in_cluster(prims, groups[0])), 1,
            "sanity check: this must still register as a pump")
        self.assertEqual(sg.instrument_shapes_in_cluster(prims, groups[0]), [],
            "a pump's circle must never also be reported as an instrument")


class BowtieScoreTests(unittest.TestCase):
    """bowtie_score() — "🦋 Hitta ventilformer" identifies valves by their
    bow-tie/hourglass silhouette (wide-narrow-wide) rather than requiring a
    tag to already be known nearby. Two real bugs were caught and fixed
    while building this: (1) rects/quads were sampled at their 4 corners
    only, leaving every slice between them with zero data points — which
    the pinch test misread as "narrowed to nothing" rather than "no
    primitive reaches here", making a plain rectangle score a perfect 1.0
    bowtie; (2) orientation was picked from the cluster's bbox aspect
    ratio, but a bowtie on a vertical pipe run has just as square a bbox
    as one on a horizontal run, so the wrong axis got tested and it scored
    far lower than its horizontal twin. Both are covered below.
    """

    def _cluster_and_score(self, shape_fn, w=200, h=200):
        doc, page = _new_page(w, h)
        shape = page.new_shape()
        shape_fn(shape)
        shape.commit()
        prims = sg.extract_primitives(page)
        groups = sg.cluster_primitives(prims)
        best = max((sg.bowtie_score(prims, g) for g in groups), default=0.0)
        doc.close()
        return best

    def test_two_triangle_bowtie_scores_high(self):
        def draw(shape):
            shape.draw_polyline([fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        self.assertGreater(self._cluster_and_score(draw), 0.6)

    def test_vertical_bowtie_scores_as_high_as_horizontal(self):
        """Regression test: orientation must not be guessed from bbox
        aspect ratio alone — a vertical bowtie's bbox is just as square as
        a horizontal one's."""
        def horizontal(shape):
            shape.draw_polyline([fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

        def vertical(shape):
            shape.draw_polyline([fitz.Point(90, 90), fitz.Point(110, 90), fitz.Point(100, 100)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(90, 110), fitz.Point(110, 110), fitz.Point(100, 100)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

        h_score = self._cluster_and_score(horizontal)
        v_score = self._cluster_and_score(vertical)
        self.assertAlmostEqual(h_score, v_score, delta=0.05,
            msg=f"horizontal={h_score} vertical={v_score} should score about the same")
        self.assertGreater(v_score, 0.6)

    def test_single_path_bowtie_scores_high(self):
        """A bow-tie drawn as ONE continuous pinched polygon (common when a
        symbol library draws it as a single path) must score the same as
        the two-separate-triangles version."""
        def draw(shape):
            shape.draw_polyline([
                fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100),
                fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100),
                fitz.Point(90, 90)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        self.assertGreater(self._cluster_and_score(draw), 0.6)

    def test_rectangle_scores_low(self):
        """Regression test for the corner-only-sampling bug: a plain
        rectangle used to score a perfect 1.0 (higher than a real bowtie!)
        because its 4 corners left every slice between them with zero
        data, misread as a pinch. Edge sampling must fix this."""
        def draw(shape):
            shape.draw_rect(fitz.Rect(90, 90, 110, 110))
            shape.finish(color=(0, 0, 0), width=1)
        self.assertLess(self._cluster_and_score(draw), 0.2)

    def test_circle_scores_low(self):
        def draw(shape):
            shape.draw_circle(fitz.Point(100, 100), 10)
            shape.finish(color=(0, 0, 0), width=1)
        self.assertLess(self._cluster_and_score(draw), 0.2)

    def test_diamond_scores_low(self):
        """A diamond (4 straight edges meeting at 4 points) must not be
        confused with a bow-tie — it never pinches to near-zero width at
        its center, it's just a plain quadrilateral."""
        def draw(shape):
            shape.draw_polyline([fitz.Point(100, 90), fitz.Point(110, 100),
                                  fitz.Point(100, 110), fitz.Point(90, 100), fitz.Point(100, 90)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        self.assertLess(self._cluster_and_score(draw), 0.2)

    def test_long_line_scores_zero(self):
        def draw(shape):
            shape.draw_line(fitz.Point(10, 100), fitz.Point(190, 100))
            shape.finish(color=(0, 0, 0), width=1)
        self.assertEqual(self._cluster_and_score(draw), 0.0)

    def test_find_symbol_clusters_includes_bowtie_score(self):
        doc, page = _new_page()
        shape = page.new_shape()
        shape.draw_polyline([fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()
        clusters = sg.find_symbol_clusters(page, min_confidence=0.0)
        doc.close()
        self.assertTrue(clusters)
        self.assertIn('bowtie_score', clusters[0])
        self.assertGreater(clusters[0]['bowtie_score'], 0.6)


class BboxIouTests(unittest.TestCase):
    """bbox_iou() — new defensive dedup helper (Fas 1+2, 2026-08-06); no
    IoU/overlap function existed in this module before."""

    def test_identical_boxes_score_one(self):
        box = (0.0, 0.0, 10.0, 10.0)
        self.assertEqual(sg.bbox_iou(box, box), 1.0)

    def test_disjoint_boxes_score_zero(self):
        self.assertEqual(sg.bbox_iou((0, 0, 10, 10), (20, 20, 30, 30)), 0.0)

    def test_partial_overlap_matches_expected_fraction(self):
        # (0,0,10,10) and (5,5,15,15) overlap in a 5x5=25 square;
        # union = 100+100-25 = 175 -> IoU = 25/175
        iou = sg.bbox_iou((0, 0, 10, 10), (5, 5, 15, 15))
        self.assertAlmostEqual(iou, 25.0 / 175.0, places=6)

    def test_cluster_distance_uses_bbox_centers(self):
        cluster_a = {'bbox': (0.0, 0.0, 10.0, 10.0)}    # center (5,5)
        cluster_b = {'bbox': (0.0, 0.0, 10.0, 30.0)}    # center (5,15)
        self.assertAlmostEqual(sg.cluster_distance(cluster_a, cluster_b), 10.0)


class ScoreTagClusterLinkTests(unittest.TestCase):
    """score_tag_cluster_link()/build_pair_scores() — the weighted
    plausibility score associate_tags_to_clusters() (pid_viewer.py) uses
    for global tag<->symbol assignment instead of resolve_tag_symbol's
    fixed leader>contain>nearest cascade."""

    def test_leader_line_scores_higher_than_distance_alone(self):
        doc, page = _new_page(200, 200)
        shape = page.new_shape()
        # Bowtie at (100,100).
        shape.draw_polyline([fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        # Leader line from the bowtie straight to the tag point.
        shape.draw_line(fitz.Point(100, 90), fitz.Point(100, 60))
        shape.finish(color=(0, 0, 0), width=1)
        # A second, decoy bowtie with NO leader line, closer by raw distance
        # to the tag point (but its bbox deliberately doesn't reach anywhere
        # near the leader line's own tag-point endpoint (100,60) — extract_
        # primitives emits both directions of every line, and a leader-line
        # search that bounces back along the reverse copy of the SAME line
        # it just walked would otherwise land back on (100,60) and produce
        # a spurious "leader" match against any decoy placed there).
        shape.draw_polyline([fitz.Point(65, 40), fitz.Point(65, 60), fitz.Point(75, 50)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(85, 40), fitz.Point(85, 60), fitz.Point(75, 50)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()

        prims = sg.extract_primitives(page)
        clusters = sg.find_symbol_clusters(page, min_confidence=0.3)
        tag_point = (100, 60)
        by_center_y = sorted(clusters, key=lambda c: sg._bbox_center(c['bbox'])[1])
        decoy, real_target = by_center_y[0], by_center_y[1]

        score_real  = sg.score_tag_cluster_link(tag_point, real_target, prims)
        score_decoy = sg.score_tag_cluster_link(tag_point, decoy, prims)
        doc.close()
        self.assertGreater(score_real, score_decoy,
            "the leader-line-connected symbol must outscore the closer-by-distance decoy")
        self.assertGreaterEqual(score_real, 0.5)

    def test_out_of_radius_scores_zero(self):
        doc, page = _new_page(1000, 1000)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(50, 50), 8)
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        prims = sg.extract_primitives(page)
        clusters = sg.find_symbol_clusters(page, min_confidence=0.3)
        far_point = (900, 900)
        score = sg.score_tag_cluster_link(far_point, clusters[0], prims, search_radius=220.0)
        doc.close()
        self.assertEqual(score, 0.0)

    def test_build_pair_scores_omits_out_of_range_pairs(self):
        doc, page = _new_page(1000, 1000)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(50, 50), 8)
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        prims = sg.extract_primitives(page)
        clusters = sg.find_symbol_clusters(page, min_confidence=0.3)
        tag_points = [(0, (52, 60)), (1, (900, 900))]   # tag 0 near, tag 1 far
        scores = sg.build_pair_scores(tag_points, clusters, prims)
        doc.close()
        self.assertTrue(any(ti == 0 for ti, _ci in scores))
        self.assertFalse(any(ti == 1 for ti, _ci in scores),
            "a tag outside search_radius of every cluster must not appear in the pair matrix")


class PipeTraceTests(unittest.TestCase):
    """trace_pipe_points_from_bbox() — vector-only BFS along connected pipe
    primitives outward from a valve's bbox, used to find nearby line-
    number/medium/DN text (pid_viewer.trace_line_info_for_cluster).
    Deliberately not raster/A* (see SmartPipeTracer) — must stay cheap
    across a 50-page document."""

    def test_walks_along_connected_line_from_bbox_edge(self):
        doc, page = _new_page(200, 200)
        shape = page.new_shape()
        # A pipe leaving the right edge of a valve bbox at (110,100),
        # running out to (180,100) where a DN callout would sit.
        shape.draw_line(fitz.Point(110, 100), fitz.Point(180, 100))
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        prims = sg.extract_primitives(page)
        doc.close()

        bbox = (90.0, 90.0, 110.0, 110.0)
        points = sg.trace_pipe_points_from_bbox(bbox, prims)
        self.assertTrue(points, "must find at least the pipe's far endpoint")
        self.assertIn((180.0, 100.0), points)

    def test_no_connected_line_returns_empty(self):
        doc, page = _new_page(200, 200)
        shape = page.new_shape()
        # A line nowhere near the given bbox.
        shape.draw_line(fitz.Point(10, 10), fitz.Point(20, 10))
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        prims = sg.extract_primitives(page)
        doc.close()

        points = sg.trace_pipe_points_from_bbox((90.0, 90.0, 110.0, 110.0), prims)
        self.assertEqual(points, [])

    def test_multi_hop_chain_extends_reach(self):
        doc, page = _new_page(300, 300)
        shape = page.new_shape()
        shape.draw_line(fitz.Point(110, 100), fitz.Point(150, 100))
        shape.finish(color=(0, 0, 0), width=1)
        shape.draw_line(fitz.Point(150, 100), fitz.Point(150, 180))
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        prims = sg.extract_primitives(page)
        doc.close()

        points = sg.trace_pipe_points_from_bbox((90.0, 90.0, 110.0, 110.0), prims)
        self.assertIn((150.0, 180.0), points,
            "a 2-hop chain must be reachable within max_hops")

    def test_respects_rotated_page_primitives(self):
        """RotatedPageCoordinateTests above found that get_drawings()
        reports coordinates in raw mediabox space; extract_primitives()
        already corrects that (via page.rotation_matrix), so tracing on a
        rotated page's ALREADY-CORRECTED primitives must behave exactly
        like the unrotated case above — this test guards against a future
        change accidentally reintroducing raw-space coordinates upstream
        of trace_pipe_points_from_bbox."""
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        page.set_rotation(90)
        shape = page.new_shape()
        shape.draw_line(fitz.Point(110, 100), fitz.Point(180, 100))
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        prims = sg.extract_primitives(page)
        doc.close()

        bbox = (90.0, 90.0, 110.0, 110.0)
        points = sg.trace_pipe_points_from_bbox(bbox, prims)
        self.assertTrue(points)
        for x, y in points:
            self.assertTrue(0 <= x <= 200 and 0 <= y <= 200,
                f"traced point {(x, y)} must land inside the rotated page bounds")


class ClusterSimilarityTests(unittest.TestCase):
    """cluster_similarity()/find_cluster_at_point() — "Hitta liknande
    symbol" (2026-08-10, see NOTES.md). Pure-geometry half of the
    feature; the orchestration (find_similar_shapes) is tested in
    test_regression.py."""

    def _feats(self, **overrides):
        base = {'aspect': 1.5, 'norm_size': 3.0, 'fold_ratio': 4.0,
                'has_curve': False, 'has_diagonal': True, 'has_closed_or_filled': True}
        base.update(overrides)
        return base

    def test_identical_features_score_1(self):
        f = self._feats()
        self.assertAlmostEqual(sg.cluster_similarity(f, dict(f)), 1.0)

    def test_completely_different_scores_low(self):
        a = self._feats(aspect=1.0, norm_size=1.0, fold_ratio=1.0,
                        has_curve=False, has_diagonal=True, has_closed_or_filled=True)
        b = self._feats(aspect=20.0, norm_size=40.0, fold_ratio=30.0,
                        has_curve=True, has_diagonal=False, has_closed_or_filled=False)
        self.assertLess(sg.cluster_similarity(a, b), 0.15)

    def test_same_shape_traits_different_size_scores_moderately(self):
        """Same boolean shape family, very different size — partial
        credit, not a full match and not a total mismatch."""
        a = self._feats(norm_size=2.0)
        b = self._feats(norm_size=20.0)
        sim = sg.cluster_similarity(a, b)
        self.assertGreater(sim, 0.5)
        self.assertLess(sim, 1.0)

    def _cluster(self, bbox):
        return {'bbox': bbox}

    def test_find_cluster_at_point_returns_containing_cluster(self):
        clusters = [self._cluster((0, 0, 10, 10)), self._cluster((20, 20, 30, 30))]
        found = sg.find_cluster_at_point(clusters, 25, 25)
        self.assertIs(found, clusters[1])

    def test_find_cluster_at_point_falls_back_to_nearest(self):
        clusters = [self._cluster((0, 0, 10, 10)), self._cluster((100, 100, 110, 110))]
        found = sg.find_cluster_at_point(clusters, 12, 12)
        self.assertIs(found, clusters[0])

    def test_find_cluster_at_point_respects_max_distance(self):
        clusters = [self._cluster((0, 0, 10, 10))]
        found = sg.find_cluster_at_point(clusters, 1000, 1000, max_distance=50)
        self.assertIsNone(found)

    def test_find_cluster_at_point_empty_list_returns_none(self):
        self.assertIsNone(sg.find_cluster_at_point([], 0, 0))


if __name__ == '__main__':
    unittest.main()
