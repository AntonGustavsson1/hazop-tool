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
import sys
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
        """Rects/quads are axis-aligned by construction — has_diagonal must
        only ever be set by an actual sloped 'l' (line) primitive."""
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


if __name__ == '__main__':
    unittest.main()
