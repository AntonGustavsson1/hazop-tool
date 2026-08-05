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


if __name__ == '__main__':
    unittest.main()
