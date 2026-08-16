"""Unit tests for image_symbol_matching.py — pixel/image-based "find
similar symbol" (2026-08-15, see NOTES.md "Bildbaserad 'hitta liknande
symbol' — vid sidan av vektorlogiken").

All fixtures are synthetic PDFs built at test time via PyMuPDF's
page.new_shape(), mirroring test_symbol_geometry.py's own convention —
no real P&ID files are used or committed as test fixtures.

Run with:
    python -m unittest hazop.test_image_symbol_matching -v
"""
import math
import sys
import unittest
import unittest.mock
from pathlib import Path

_HAZOP_DIR = Path(__file__).resolve().parent
if str(_HAZOP_DIR) not in sys.path:
    sys.path.insert(0, str(_HAZOP_DIR))

import fitz
import image_symbol_matching as ism


def _new_page(w=400, h=300):
    doc = fitz.open()
    return doc, doc.new_page(width=w, height=h)


def _draw_bowtie(shape, cx, cy, r=8, rotation_deg=0):
    """A small filled bow-tie (two triangles sharing an apex) — stands in
    for a real valve symbol without needing a real P&ID fixture."""
    def rot(x, y):
        if rotation_deg == 0:
            return fitz.Point(x, y)
        rad = math.radians(rotation_deg)
        dx, dy = x - cx, y - cy
        return fitz.Point(cx + dx * math.cos(rad) - dy * math.sin(rad),
                           cy + dx * math.sin(rad) + dy * math.cos(rad))
    shape.draw_polyline([rot(cx - r, cy - r), rot(cx - r, cy + r), rot(cx, cy)])
    shape.finish(color=(0, 0, 0), fill=(0, 0, 0), closePath=True)
    shape.draw_polyline([rot(cx + r, cy - r), rot(cx + r, cy + r), rot(cx, cy)])
    shape.finish(color=(0, 0, 0), fill=(0, 0, 0), closePath=True)


def _draw_square(shape, cx, cy, r=8):
    """A clearly different filled shape — the "must not match" decoy."""
    shape.draw_rect(fitz.Rect(cx - r, cy - r, cx + r, cy + r))
    shape.finish(color=(0, 0, 0), fill=(0, 0, 0))


class RenderGrayTests(unittest.TestCase):
    def test_full_page_render_has_expected_dimensions(self):
        doc, page = _new_page(200, 100)
        arr = ism.render_gray(page, bbox=None, dpi=72)
        self.assertEqual(arr.shape, (100, 200))
        doc.close()

    def test_cropped_region_is_smaller_than_full_page(self):
        doc, page = _new_page(200, 100)
        arr = ism.render_gray(page, bbox=(10, 10, 60, 60), dpi=72)
        self.assertEqual(arr.shape, (50, 50))
        doc.close()


class FindSimilarShapesVisualTests(unittest.TestCase):
    def _two_bowties_and_a_decoy(self):
        doc, page = _new_page(400, 300)
        shape = page.new_shape()
        _draw_bowtie(shape, 60, 60)
        _draw_bowtie(shape, 300, 200)   # duplicate elsewhere on the page
        _draw_square(shape, 200, 60)    # clearly different — must not match
        shape.commit()
        return doc

    def test_finds_the_duplicate_shape_on_the_same_page(self):
        doc = self._two_bowties_and_a_decoy()
        ref_bbox = (50, 50, 70, 70)
        cands = ism.find_similar_shapes_visual(doc, 0, ref_bbox, pages=[0],
                                                min_similarity=0.6)
        matches = [c for c in cands if math.hypot(c[2] - 300, c[3] - 200) < 5]
        self.assertEqual(len(matches), 1,
            f"expected exactly one match near the duplicate bow-tie, got {cands}")
        self.assertGreater(matches[0][0], 0.9)
        doc.close()

    def test_rejects_a_dissimilar_shape_at_default_threshold(self):
        doc = self._two_bowties_and_a_decoy()
        ref_bbox = (50, 50, 70, 70)
        cands = ism.find_similar_shapes_visual(doc, 0, ref_bbox, pages=[0],
                                                min_similarity=0.6)
        near_decoy = [c for c in cands if math.hypot(c[2] - 200, c[3] - 60) < 5]
        self.assertEqual(near_decoy, [],
            "the clearly-different square must not pass a 60% threshold")
        doc.close()

    def test_reference_region_itself_is_excluded_from_results(self):
        doc = self._two_bowties_and_a_decoy()
        ref_bbox = (50, 50, 70, 70)
        cands = ism.find_similar_shapes_visual(doc, 0, ref_bbox, pages=[0],
                                                min_similarity=0.3)
        near_ref = [c for c in cands if math.hypot(c[2] - 60, c[3] - 60) < 5]
        self.assertEqual(near_ref, [],
            "the reference must never match itself in its own results")
        doc.close()

    def test_default_rotation_mode_still_finds_a_90_degree_rotated_duplicate(self):
        """Parity with the vector path's own claim: symbols rotated in
        90-degree steps are found automatically without needing 'alla
        vinklar'."""
        doc, page = _new_page(400, 300)
        shape = page.new_shape()
        _draw_bowtie(shape, 60, 60)
        _draw_bowtie(shape, 300, 200, rotation_deg=90)
        shape.commit()
        ref_bbox = (50, 50, 70, 70)
        cands = ism.find_similar_shapes_visual(doc, 0, ref_bbox, pages=[0],
                                                min_similarity=0.6, rotation_mode='none')
        matches = [c for c in cands if math.hypot(c[2] - 300, c[3] - 200) < 5]
        self.assertEqual(len(matches), 1)
        doc.close()

    def test_ignore_scale_finds_a_resized_duplicate(self):
        doc, page = _new_page(400, 300)
        shape = page.new_shape()
        _draw_bowtie(shape, 60, 60, r=8)
        _draw_bowtie(shape, 300, 200, r=10)   # 1.25x larger
        shape.commit()
        ref_bbox = (50, 50, 70, 70)

        same_size_only = ism.find_similar_shapes_visual(
            doc, 0, ref_bbox, pages=[0], min_similarity=0.8, ignore_scale=False)
        matches_same = [c for c in same_size_only if math.hypot(c[2] - 300, c[3] - 200) < 6]
        self.assertEqual(matches_same, [],
            "a meaningfully larger duplicate should not pass at native scale only")

        with_scale = ism.find_similar_shapes_visual(
            doc, 0, ref_bbox, pages=[0], min_similarity=0.8, ignore_scale=True)
        matches_scaled = [c for c in with_scale if math.hypot(c[2] - 300, c[3] - 200) < 6]
        self.assertEqual(len(matches_scaled), 1,
            "ignore_scale=True must find the resized duplicate")
        doc.close()

    def test_should_cancel_stops_after_the_current_page(self):
        doc, page0 = _new_page(400, 300)
        shape0 = page0.new_shape()
        _draw_bowtie(shape0, 60, 60)
        shape0.commit()
        page1 = doc.new_page(width=400, height=300)
        shape1 = page1.new_shape()
        _draw_bowtie(shape1, 60, 60)
        shape1.commit()

        seen_pages = []
        def should_cancel():
            return len(seen_pages) >= 1
        def progress(page_num, total, msg):
            seen_pages.append(page_num)

        ism.find_similar_shapes_visual(
            doc, 0, (50, 50, 70, 70), pages=[0, 1], min_similarity=0.6,
            progress_callback=progress, should_cancel=should_cancel)
        self.assertEqual(seen_pages, [0],
            "should_cancel must stop the scan before a second page is processed")
        doc.close()

    def test_should_cancel_stops_mid_page_not_just_at_page_boundaries(self):
        """Found in the wild (2026-08-16, see NOTES.md "Bildmatchning
        visar ingen symbol och kraschar lätt"): should_cancel used to be
        checked only once per PAGE — with ignore_scale=True and
        rotation_mode='any' (up to 7 scales x 12 rotations = 84
        cv2.matchTemplate calls for a single page), cancelling mid-scan
        had to wait for that entire page's remaining combinations to
        finish before it took effect, which could block
        SimilarSymbolSearchDialog._on_dialog_finished's un-timed
        `worker.wait()` long enough to read as a hang/crash. Verifies
        should_cancel is now checked far more often than once — well
        under the full scales x rotations product — by counting how many
        times cv2.matchTemplate actually runs before a should_cancel that
        fires on its very first call takes effect."""
        doc, page = _new_page(400, 300)
        shape = page.new_shape()
        _draw_bowtie(shape, 60, 60)
        shape.commit()

        call_count = {'n': 0}
        cancel_state = {'checks': 0}
        import cv2
        real_match_template = cv2.matchTemplate

        def counting_match_template(*args, **kwargs):
            call_count['n'] += 1
            return real_match_template(*args, **kwargs)

        def should_cancel():
            # False for the page-level check and the first couple of
            # (scale, rotation) checks, then True — so the ONLY way this
            # can stop the scan this early is if should_cancel() is
            # actually being polled repeatedly WITHIN the page, not just
            # once at the page boundary (which already happened, and
            # returned False, before any of these later calls occur).
            cancel_state['checks'] += 1
            return cancel_state['checks'] > 2

        with unittest.mock.patch.object(cv2, 'matchTemplate', side_effect=counting_match_template):
            ism.find_similar_shapes_visual(
                doc, 0, (50, 50, 70, 70), pages=[0], min_similarity=0.6,
                ignore_scale=True, rotation_mode='any',
                should_cancel=should_cancel)
        # scales x rotations is up to 7 x 12 = 84 combinations — if
        # should_cancel were only checked once per page (as before this
        # fix), it would already have returned False at that single check
        # and the full 84 combinations would run, each triggering its own
        # cv2.matchTemplate call.
        self.assertLess(call_count['n'], 84,
            "should_cancel must be polled within a page, not just once at the page boundary")
        doc.close()

    def test_returns_empty_list_without_cv2(self):
        doc = self._two_bowties_and_a_decoy()
        with unittest.mock.patch.object(ism, 'HAS_CV2', False):
            cands = ism.find_similar_shapes_visual(doc, 0, (50, 50, 70, 70), pages=[0])
        self.assertEqual(cands, [])
        doc.close()


class NmsTests(unittest.TestCase):
    def test_collapses_overlapping_boxes_to_the_highest_score(self):
        matches = [
            (0.95, (10, 10, 30, 30)),
            (0.80, (11, 11, 31, 31)),   # near-duplicate of the same detection
            (0.70, (200, 200, 220, 220)),   # unrelated, far away
        ]
        kept = ism._nms(matches, iou_threshold=0.3)
        self.assertEqual(len(kept), 2)
        scores = sorted(s for s, _b in kept)
        self.assertAlmostEqual(scores[-1], 0.95)
        self.assertAlmostEqual(scores[0], 0.70)

    def test_non_overlapping_boxes_are_all_kept(self):
        matches = [(0.9, (0, 0, 10, 10)), (0.8, (100, 100, 110, 110))]
        kept = ism._nms(matches, iou_threshold=0.3)
        self.assertEqual(len(kept), 2)


if __name__ == '__main__':
    unittest.main()
