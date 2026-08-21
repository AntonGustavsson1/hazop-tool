#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering equipment_detection.py, plus any cross-module glue they
directly depend on. Test bodies are unchanged from the
original file, only their file location moved."""
"""Regression test suite for the HAZOP PyQt6 application.

Covers crash patterns that have been found and fixed in this codebase over
recent sessions:

  1. Orphaned-data crashes: deleting a "cause" can leave orphaned
     "consequence"/"safeguard" records; P&ID overlay code that draws
     connection lines between markers used to crash with KeyError /
     AttributeError when it hit an orphaned record's missing parent
     reference.
  2. sqlite3.Row objects do not support `.get()` — several code paths used
     to call `.get()` directly on a raw Row instead of converting to a dict
     first, causing AttributeError.
  3. ComboBox `currentIndex()` returning -1 (uninitialized/empty widget)
     used to cause IndexError when used to index into arrays such as
     RRF_VALUES / SG_TYPES.
  4. A settings panel referenced `self._sev_def_panel`, which was never
     actually instantiated, causing AttributeError when deleting a
     consequence category.

Run with:
    python -m pytest hazop/test_regression.py -v
or:
    python -m unittest hazop.test_regression -v

Requires QT_QPA_PLATFORM=offscreen for headless CI environments — this is
set automatically at the top of this file, before PyQt6/hazop is imported,
so the suite runs without a display (CI, SSH, etc.).
"""

import gc
import io
import os
import sys
import shutil
import sqlite3
import tempfile
import unittest
import unittest.mock
from pathlib import Path

# ── Headless Qt setup — MUST happen before importing PyQt6 or hazop ────────
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# hazop.py / pid_viewer.py are large standalone scripts (not a package) that
# import each other via plain `from pid_viewer import ...`, so the hazop/
# directory must be on sys.path for those imports to resolve regardless of
# the current working directory the tests are launched from.
_TEST_DIR = Path(__file__).resolve().parent
_HAZOP_DIR = _TEST_DIR.parent
for _p in (_HAZOP_DIR, _TEST_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hazop  # noqa: E402  (import after sys.path setup, by design)
from hazop import (  # noqa: E402
    Database, TreePanel, MainWindow,
    NODE_T, DEV_T, CAUSE_T, CONS_T, SG_T, EQUIP_T, LEDORD_T,
    freq_to_idx,
)
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QGraphicsPixmapItem, QTreeWidgetItemIterator, QCheckBox,
    QComboBox, QPushButton, QMessageBox, QInputDialog, QLineEdit,
)
from PyQt6.QtGui import QPixmap, QFocusEvent  # noqa: E402
from PyQt6.QtCore import Qt, QPoint, QDate, QEvent, QThread, pyqtSignal  # noqa: E402
from equipment_detection import COMPONENT_TYPES  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# Shared QApplication — Qt only allows one per process.
# ══════════════════════════════════════════════════════════════════════════


from test_helpers import (
    _ensure_qapp, _menu_action_labels, _fake_pdf_loaded,
    _TempDbMainWindow, _find_tree_item,
)

# ══════════════════════════════════════════════════════════════════════════
# 10. Bow-tie shape-based valve detection — "🦋 Hitta ventilformer" finds
#     valves by their drawn silhouette (symbol_geometry.bowtie_score)
#     instead of requiring a tag to already be scanned nearby, so it also
#     catches untagged valves. find_valve_shapes()/find_nearby_tag_text()
#     are unit-tested against synthetic PDFs here; the geometry itself
#     (bowtie_score orientation/sampling correctness) is covered in
#     test_symbol_geometry.BowtieScoreTests.
# ══════════════════════════════════════════════════════════════════════════

class BowtieValveDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_bowtie_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _bowtie_pdf(self, with_tag=True):
        import fitz
        path = os.path.join(self._tmpdir, "valve.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_polyline([fitz.Point(90, 90), fitz.Point(90, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(110, 90), fitz.Point(110, 110), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()
        if with_tag:
            page.insert_text(fitz.Point(95, 75), "V-201", fontsize=8)
        doc.save(path)
        doc.close()
        return path

    def test_find_valve_shapes_detects_bowtie_with_nearby_tag(self):
        from equipment_detection import find_valve_shapes
        import fitz
        doc = fitz.open(self._bowtie_pdf(with_tag=True))
        try:
            results = find_valve_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag'], 'V-201')
        self.assertEqual(results[0]['link_method'], 'shape')
        self.assertGreater(results[0]['confidence'], 0.5)

    def test_find_valve_shapes_leaves_tag_empty_when_none_nearby(self):
        """This is the scope the user explicitly asked for: valves with no
        tag anywhere near them must still be found and returned, with an
        empty (not guessed, not dropped) tag for manual entry in the
        review dialog."""
        from equipment_detection import find_valve_shapes
        import fitz
        doc = fitz.open(self._bowtie_pdf(with_tag=False))
        try:
            results = find_valve_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag'], '')
        self.assertEqual(results[0]['link_method'], 'shape')

    def test_find_valve_shapes_respects_min_bowtie_score(self):
        from equipment_detection import find_valve_shapes
        import fitz
        path = os.path.join(self._tmpdir, "rect.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(90, 90, 110, 110))
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_valve_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(results, [], "a plain rectangle must not be reported as a valve")

    def test_find_valve_shapes_and_tag_link_survive_page_rotation(self):
        """Regression test for the coordinate mix-up found during real-file
        verification against 182036 Hybrit (/Rotate 270): get_text() and
        get_drawings() both report positions in the page's raw unrotated
        mediabox space, but this app's marker placement (pdf_to_scene())
        assumes the ROTATED space matching page.rect. Without rotating
        both the shape and the tag text into the same space, the tag
        would fail to link (find_nearby_tag_text comparing a rotated-space
        point against raw-space text positions) and/or the reported x/y
        would land outside the rendered page entirely."""
        from equipment_detection import find_valve_shapes
        import fitz
        path = os.path.join(self._tmpdir, "rotated_valve.pdf")
        doc = fitz.open()
        # Non-square mediabox + 90° rotation: page.rect swaps width/height,
        # so a coordinate-space mix-up cannot accidentally look correct.
        page = doc.new_page(width=100, height=300)
        page.set_rotation(90)
        shape = page.new_shape()
        shape.draw_polyline([fitz.Point(10, 270), fitz.Point(10, 290), fitz.Point(20, 280)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(30, 270), fitz.Point(30, 290), fitz.Point(20, 280)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()
        page.insert_text(fitz.Point(15, 255), "V-500", fontsize=8)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            page_rect = doc[0].rect
            results = find_valve_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag'], 'V-500',
            "tag must still link across the rotation-space mismatch")
        self.assertTrue(0 <= results[0]['x'] <= page_rect.width,
            f"x={results[0]['x']} must fall inside the rendered page (0..{page_rect.width})")
        self.assertTrue(0 <= results[0]['y'] <= page_rect.height,
            f"y={results[0]['y']} must fall inside the rendered page (0..{page_rect.height})")

    def test_find_valve_shapes_detects_small_valve_on_no_text_dense_noise_page(self):
        """End-to-end reproduction of a real generalization failure found
        while auditing against the "P&ID ref/" reference library (2026-
        08-12): Loket, Smurfit Kappa, and one Swerim/NYA file all draw
        EVERY piece of text as pure vector strokes via a non-embedded/
        SHX-style font, so page.get_text() finds zero words/spans despite
        tens of thousands of vector primitives (a real Loket page measured
        54,396 primitives against 0 text spans, ~5x denser than a normal
        text-bearing P&ID of the same size). Two compounding bugs hid real
        valves on such pages: (1) dominant_text_size()'s blind 10.0pt
        no-text fallback shrank norm_size for every real symbol until it
        fell under classify_cluster's/find_valve_shapes' 1.5 floor, and
        (2) cluster_primitives' dense-grid-cell skip fragmented a real
        valve's own few edges into unmergeable singletons whenever they
        shared a 20x20pt cell with a patch of glyph-stroke noise — visibly
        intact bow-tie valve icons next to real tags (confirmed: Loket tag
        "1-RV-25") queried as nothing but n_items=1 singleton clusters.
        This test needs BOTH fixes together: fixing only the scale
        wouldn't help while the valve's own primitives still can't merge,
        and fixing only the clustering wouldn't help while the merged
        valve's norm_size still falls under the floor."""
        from equipment_detection import find_valve_shapes
        import fitz
        path = os.path.join(self._tmpdir, "no_text_dense_noise.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        # Small bow-tie valve (bbox diagonal ~11.3pt) — proportioned like
        # many real bow-tie icons on the real Loket file.
        shape.draw_polyline([fitz.Point(96, 96), fitz.Point(96, 104), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([fitz.Point(104, 96), fitz.Point(104, 104), fitz.Point(100, 100)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        # Dense "vector-outlined text" noise: 45 tiny primitives packed
        # into the SAME 20x20pt grid cell as the valve (cell (4,4), pt
        # range 80-100 on both axes), far enough from the valve's own bbox
        # (>_CLUSTER_GAP=3.0pt) that they could never legitimately merge
        # with it.
        for i in range(45):
            col, row = i % 9, i // 9
            x = 80.0 + col * 0.12
            y = 80.0 + row * 0.12
            shape.draw_line(fitz.Point(x, y), fitz.Point(x + 0.05, y + 0.05))
            shape.finish(color=(0, 0, 0), width=0.05, closePath=False)
        shape.commit()
        # Deliberately NO insert_text call anywhere on this page — the
        # whole point is reproducing a page with zero native text.
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            words = doc[0].get_text("words")
            self.assertEqual(words, [], "fixture must have zero native text, matching the real files")
            results = find_valve_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1,
            "the small valve must be found despite the page having no "
            "native text and a dense patch of glyph-noise-sized primitives "
            "sharing its grid cell")
        self.assertGreater(results[0]['confidence'], 0.5)

    def test_find_pump_shapes_detects_circle_with_impeller_diagonal(self):
        """find_pump_shapes() — the pump counterpart to find_valve_shapes(),
        added after studying real LKAB/Gryaab P&IDs: a pump is a circle
        with two diagonal lines (an impeller mark) meeting on its rim,
        proportioned at ~70% of the circle's own diameter (confirmed: a
        real LKAB pump's two 30.1pt diagonals inside a 42.5pt circle)."""
        from equipment_detection import find_pump_shapes
        import fitz
        path = os.path.join(self._tmpdir, "pump.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(100, 86), fitz.Point(120, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(120, 100), fitz.Point(100, 114))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        page.insert_text(fitz.Point(80, 130), "PU-101", fontsize=8)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_pump_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag'], 'PU-101')
        self.assertEqual(results[0]['comp_type'], 'Pump')

    def test_find_pump_shapes_ignores_plain_instrument_bubble(self):
        from equipment_detection import find_pump_shapes
        import fitz
        path = os.path.join(self._tmpdir, "bubble.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        page.insert_text(fitz.Point(90, 100), "PI-1", fontsize=8)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_pump_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(results, [], "a plain instrument bubble (no impeller diagonal) must not be reported as a pump")

    def test_find_pump_shapes_ignores_title_block_corner(self):
        """A pump-shaped hit in the bottom-right title-block corner is
        excluded (2026-08-10, see NOTES.md known limitations, fixed) — a
        real ITS P&ID had a stylized company logo there false-positive as
        a pump. The same shape at page center (test above) still counts."""
        from equipment_detection import find_pump_shapes
        import fitz
        path = os.path.join(self._tmpdir, "pump_titleblock.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        # Bottom-right corner for a 200x200 page with the (0.20, 0.10)
        # fraction is x>=160, y>=180 — center the pump well inside that.
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(185, 190), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(185, 183), fitz.Point(195, 190))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(195, 190), fitz.Point(185, 197))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_pump_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(results, [], "a pump-shaped hit inside the title-block corner must be excluded")

    def test_find_instrument_shapes_detects_divided_capsule(self):
        """find_instrument_shapes() — the instrument counterpart to
        find_valve_shapes()/find_pump_shapes(), added after studying a
        real LKAB P&ID: an instrument bubble is a circle/capsule with a
        horizontal divider line at its vertical midpoint (ISA-5.1's
        shared-display/panel-mounted convention)."""
        from equipment_detection import find_instrument_shapes
        import fitz
        path = os.path.join(self._tmpdir, "instrument.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(85, 100), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_circle(fitz.Point(115, 100), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(85, 90), fitz.Point(115, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(85, 110), fitz.Point(115, 110))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(75, 100), fitz.Point(125, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        # fontsize=10 (not the more typical 8) so dominant_text_size()
        # comes out high enough that the 50pt divider line stays safely
        # under _is_pipe_run_line's scale-relative "long pipe" threshold
        # (6x text size) — with fontsize=8 that threshold is 48pt and
        # the divider gets excluded from bridging, splitting the capsule
        # into two clusters and hiding it from instrument_shapes_in_cluster.
        page.insert_text(fitz.Point(70, 130), "PI-101", fontsize=10)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_instrument_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag'], 'PI-101')
        self.assertEqual(results[0]['comp_type'], 'Instrument / Sensor')

    def test_find_instrument_shapes_ignores_pump_circle(self):
        """A pump's circle (with its own diagonal impeller mark and a
        pipe line straight through its midpoint) must never also be
        reported as an instrument — see
        InstrumentShapeTests.test_pump_circle_with_through_line_is_not_an_instrument
        for the geometric ambiguity this guards against."""
        from equipment_detection import find_instrument_shapes
        import fitz
        path = os.path.join(self._tmpdir, "pump_not_instrument.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_circle(fitz.Point(100, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(70, 100), fitz.Point(130, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(100, 86), fitz.Point(120, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(120, 100), fitz.Point(100, 114))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        page.insert_text(fitz.Point(90, 130), "PU-1", fontsize=8)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_instrument_shapes(doc)
        finally:
            doc.close()
        self.assertEqual(results, [])

    def test_find_nearby_tag_text_finds_closest_tag_within_radius(self):
        from equipment_detection import find_nearby_tag_text
        import fitz
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        page.insert_text(fitz.Point(95, 75), "V-201", fontsize=8)
        try:
            tag, prefix = find_nearby_tag_text(page, (100, 100), radius=150.0)
            self.assertEqual(tag, 'V-201')
            self.assertEqual(prefix, 'V')
        finally:
            doc.close()

    def test_find_nearby_tag_text_returns_none_when_too_far(self):
        from equipment_detection import find_nearby_tag_text
        import fitz
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        page.insert_text(fitz.Point(10, 20), "V-201", fontsize=8)
        try:
            tag, prefix = find_nearby_tag_text(page, (390, 390), radius=50.0)
            self.assertIsNone(tag)
            self.assertIsNone(prefix)
        finally:
            doc.close()

    def test_detect_equipment_and_valves_links_shape_hit_to_existing_equipment_row(self):
        """Replacement for the old, now-removed '🦋 Hitta ventilformer'
        button (its shape-hunting is folded into detect_equipment_and_valves,
        the engine behind EquipmentPanel._autodetect — see NOTES.md,
        "Fas 1+2" 2026-08-06): a bow-tie shape whose nearby tag matches an
        already-known equipment_catalog row must resolve with a real
        link_method (not 'none'), so _autodetect's tag_to_equipment_id
        lookup can attach it to the existing row instead of duplicating it."""
        from equipment_detection import detect_equipment_and_valves
        import fitz
        cur = self.db.conn.execute(
            "INSERT INTO equipment_catalog (tag, prefix, pid_page, equipment_type) "
            "VALUES (?,?,?,?)", ("V-201", "V", 0, "Ventil"))
        self.db.commit()

        doc = fitz.open(self._bowtie_pdf(with_tag=True))
        try:
            tag_points = [("V-201", "V", 0, None, None, 1.0)]
            results, _rejected = detect_equipment_and_valves(doc, tag_points)
            tagged = [r for r in results if r['tag'] == 'V-201']
            self.assertEqual(len(tagged), 1)
            self.assertNotEqual(tagged[0]['link_method'], 'none')
            self.assertEqual(tagged[0]['tag_status'], 'tagged')
        finally:
            doc.close()

    def test_autodetect_no_pdf_shows_warning(self):
        """Replacement for the old PIDPanel._find_valve_shapes no-PDF test —
        EquipmentPanel._autodetect is now the single consolidated entry
        point and must warn the same way when no P&ID path is configured."""
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox
        self.db.add_equipment_item("V-201", "V-201", "V", 0, "Ventil", '', 0)
        panel = EquipmentPanel(self.db)
        try:
            panel.refresh()
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn:
                panel._autodetect()
            self.assertEqual(mock_warn.call_count, 1)
        finally:
            panel.deleteLater()

    def test_review_dialog_save_creates_equipment_catalog_row_for_taggedbut_unlinked_row(self):
        """A shape-detected valve whose suggested tag has NO existing
        equipment_catalog row (equipment_id=None) must get one created on
        save, per Task 20's tag-less-row handling."""
        from pid_viewer import EquipmentMarkerReviewDialog
        results = [{'tag': 'V-301', 'page': 0, 'comp_type': 'Ventil',
                    'x': 100.0, 'y': 100.0, 'confidence': 0.8,
                    'link_method': 'shape', 'outline': [[90, 90], [110, 110]],
                    'equipment_id': None}]
        dlg = EquipmentMarkerReviewDialog(results, self.db)
        try:
            dlg._save()
            cat_rows = [dict(r) for r in self.db.equipment_items()]
            self.assertEqual(len(cat_rows), 1)
            self.assertEqual(cat_rows[0]['tag'], 'V-301')
            marker_rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            self.assertEqual(len(marker_rows), 1)
        finally:
            dlg.deleteLater()

    def test_review_dialog_save_with_empty_tag_and_no_equipment_id_still_saves(self):
        """A shape-detected valve with NEITHER a suggested tag NOR a linked
        equipment_catalog row (the 'valve with no tag anywhere nearby'
        case) must still be checkable and saveable — the whole point of
        the tag-less scope the user asked for."""
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtCore import Qt
        results = [{'tag': '', 'page': 0, 'comp_type': 'Ventil',
                    'x': 50.0, 'y': 50.0, 'confidence': 0.8,
                    'link_method': 'shape', 'outline': [[40, 40], [60, 60]],
                    'equipment_id': None}]
        dlg = EquipmentMarkerReviewDialog(results, self.db)
        try:
            dlg._tbl.item(0, dlg._C_CHK).setCheckState(Qt.CheckState.Checked)
            dlg._save()
            marker_rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            self.assertEqual(len(marker_rows), 1)
        finally:
            dlg.deleteLater()


    # ══════════════════════════════════════════════════════════════════════
    # Fas 1+2 (2026-08-06): detect_equipment_and_valves() unifies the
    # tag-anchored and shape-anchored paths against one shared per-page
    # cluster extraction, so a valve that has BOTH a known tag AND a
    # matching bow-tie shape must appear exactly once, and one with
    # neither must still surface as an untagged row, not get dropped.
    # ══════════════════════════════════════════════════════════════════════

    def test_detect_equipment_and_valves_no_double_count_when_tag_and_shape_both_match(self):
        from equipment_detection import detect_equipment_and_valves
        import fitz
        doc = fitz.open(self._bowtie_pdf(with_tag=True))
        try:
            tag_points = [("V-201", "V", 0, None, None, 1.0)]
            results, rejected = detect_equipment_and_valves(doc, tag_points)
        finally:
            doc.close()
        self.assertEqual(len(results), 1,
            "the same physical valve must not appear once as a tagged row "
            "and again as a separate untagged shape-anchored row")
        self.assertEqual(results[0]['tag'], 'V-201')
        self.assertEqual(results[0]['tag_status'], 'tagged')

    def test_detect_equipment_and_valves_surfaces_untagged_valve(self):
        """The scope the user explicitly asked ChatGPT's spec to cover:
        valves with no tag anywhere nearby must still be found and
        returned with a temporary id, never silently dropped."""
        from equipment_detection import detect_equipment_and_valves
        import fitz
        doc = fitz.open(self._bowtie_pdf(with_tag=False))
        try:
            results, rejected = detect_equipment_and_valves(doc, [])
        finally:
            doc.close()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tag_status'], 'untagged')
        self.assertEqual(results[0]['tag'], '')
        self.assertTrue(results[0]['temporary_id'].startswith('UNASSIGNED-VALVE-0-'),
            f"got temporary_id={results[0]['temporary_id']!r}")

    def test_detect_equipment_and_valves_surfaces_untagged_pump_and_instrument(self):
        """User request (2026-08-07): 'Hitta på P&ID' (detect_equipment_and_valves,
        the engine behind the old '🦋 Hitta ventilformer' button) must also
        surface pump and instrument shapes, not just valves — they were
        previously only reachable via the standalone find_pump_shapes()/
        find_instrument_shapes(), never wired into this unified pipeline."""
        from equipment_detection import detect_equipment_and_valves
        import fitz
        path = os.path.join(self._tmpdir, "pump_and_instrument.pdf")
        doc = fitz.open()
        page = doc.new_page(width=300, height=200)
        shape = page.new_shape()
        # A pump: circle + diagonal impeller mark.
        shape.draw_circle(fitz.Point(60, 100), 20)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(60, 86), fitz.Point(80, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(80, 100), fitz.Point(60, 114))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        # A divided instrument capsule, far enough away to be its own cluster.
        shape.draw_circle(fitz.Point(225, 100), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_circle(fitz.Point(255, 100), 10)
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(225, 90), fitz.Point(255, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(225, 110), fitz.Point(255, 110))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.draw_line(fitz.Point(215, 100), fitz.Point(265, 100))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        page.insert_text(fitz.Point(40, 130), "PU-101", fontsize=10)
        page.insert_text(fitz.Point(210, 130), "PI-101", fontsize=10)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results, _rejected = detect_equipment_and_valves(doc, [])
        finally:
            doc.close()

        pumps = [r for r in results if r['comp_type'] == 'Pump']
        instruments = [r for r in results if r['comp_type'] == 'Instrument / Sensor']
        self.assertEqual(len(pumps), 1)
        self.assertEqual(pumps[0]['tag'], 'PU-101')
        self.assertEqual(pumps[0]['tag_status'], 'untagged')
        self.assertTrue(pumps[0]['temporary_id'].startswith('UNASSIGNED-PUMP-0-'))
        self.assertEqual(len(instruments), 1)
        self.assertEqual(instruments[0]['tag'], 'PI-101')
        self.assertEqual(instruments[0]['tag_status'], 'untagged')
        self.assertTrue(instruments[0]['temporary_id'].startswith('UNASSIGNED-INSTRUMENT-0-'))

    def test_valve_rejection_reason_reports_single_near_miss_only(self):
        """_valve_rejection_reason surfaces a near-miss (exactly one of the
        five bow-tie/valve-shape filters failed) for the review dialog's
        'avvisade kandidater' section, but stays silent for a cluster that
        either passes everything or is too far off (>1 filter failed) to
        be an interesting near-miss."""
        from equipment_detection import _valve_rejection_reason
        near_miss = {'bowtie_score': 0.7, 'aspect': 5.0, 'norm_size': 10.0,
                     'has_diagonal': True, 'has_closed_loop': True}
        reason = _valve_rejection_reason(near_miss, min_bowtie_score=0.5)
        self.assertIsNotNone(reason)
        self.assertIn('avlångt', reason.lower())

        too_far_off = {'bowtie_score': 0.1, 'aspect': 5.0, 'norm_size': 10.0,
                       'has_diagonal': True, 'has_closed_loop': True}
        self.assertIsNone(_valve_rejection_reason(too_far_off, min_bowtie_score=0.5))

        passes_all = {'bowtie_score': 0.7, 'aspect': 1.5, 'norm_size': 10.0,
                      'has_diagonal': True, 'has_closed_loop': True}
        self.assertIsNone(_valve_rejection_reason(passes_all, min_bowtie_score=0.5))

        no_closed_shape = {'bowtie_score': 0.7, 'aspect': 1.5, 'norm_size': 10.0,
                            'has_diagonal': True, 'has_closed_loop': False}
        reason2 = _valve_rejection_reason(no_closed_shape, min_bowtie_score=0.5)
        self.assertIsNotNone(reason2)
        self.assertIn('sluten', reason2.lower())


class FindSimilarShapesTests(unittest.TestCase):
    """find_similar_shapes() (equipment_detection.py) — "Hitta liknande
    symbol" (2026-08-10, see NOTES.md): user picks a reference point,
    every other vector cluster in the document gets ranked against it
    via symbol_geometry.cluster_similarity()."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_findsimilar_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _two_bowties_and_a_rect_pdf(self):
        """Two IDENTICAL bow-tie shapes (well separated) plus one plainly
        different long rectangle, on one page."""
        import fitz
        path = os.path.join(self._tmpdir, "similar.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()

        def _bowtie(cx, cy):
            shape.draw_polyline([fitz.Point(cx - 10, cy - 10), fitz.Point(cx - 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(cx + 10, cy - 10), fitz.Point(cx + 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

        _bowtie(60, 60)     # reference
        _bowtie(300, 300)   # identical, far away
        shape.draw_rect(fitz.Rect(60, 300, 260, 320))   # plainly different (long, no diagonals)
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        doc.save(path)
        doc.close()
        return path

    def test_finds_identical_shape_elsewhere_on_page(self):
        import fitz
        from equipment_detection import find_similar_shapes
        doc = fitz.open(self._two_bowties_and_a_rect_pdf())
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                          pages=[0], min_similarity=0.5)
        finally:
            doc.close()
        self.assertTrue(results, "the identical bow-tie elsewhere on the page must be found")
        best = results[0]
        self.assertAlmostEqual(best['x'], 300, delta=5)
        self.assertAlmostEqual(best['y'], 300, delta=5)

    def test_result_shape_matches_review_dialog_contract(self):
        """EquipmentMarkerReviewDialog._populate()/_save() index these
        dict keys directly — a typo here would only surface as a KeyError
        deep inside dialog code, not at the call site."""
        import fitz
        from equipment_detection import find_similar_shapes
        doc = fitz.open(self._two_bowties_and_a_rect_pdf())
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                          pages=[0], min_similarity=0.5, comp_type='Ventil')
        finally:
            doc.close()
        self.assertTrue(results)
        r = results[0]
        for key in ('tag', 'page', 'comp_type', 'x', 'y', 'outline',
                   'link_method', 'tag_status', 'temporary_id', 'detection_confidence'):
            self.assertIn(key, r)
        self.assertEqual(r['tag'], '')
        self.assertEqual(r['comp_type'], 'Ventil')
        self.assertEqual(r['link_method'], 'similar')
        self.assertEqual(r['tag_status'], 'untagged')
        self.assertTrue(0.0 <= r['detection_confidence'] <= 1.0)

    def test_excludes_reference_cluster_itself(self):
        import fitz
        from equipment_detection import find_similar_shapes
        doc = fitz.open(self._two_bowties_and_a_rect_pdf())
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                          pages=[0], min_similarity=0.0)
        finally:
            doc.close()
        for r in results:
            self.assertFalse(abs(r['x'] - 60) < 2 and abs(r['y'] - 60) < 2,
                "the reference shape must not be returned as a match for itself")

    def test_sorted_by_similarity_descending(self):
        import fitz
        from equipment_detection import find_similar_shapes
        doc = fitz.open(self._two_bowties_and_a_rect_pdf())
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                          pages=[0], min_similarity=0.0)
        finally:
            doc.close()
        sims = [r['detection_confidence'] for r in results]
        self.assertEqual(sims, sorted(sims, reverse=True))

    def test_returns_empty_for_page_with_no_vector_data(self):
        """A fully rasterized/scanned page (no vector drawings at all,
        see NOTES.md's 2026-08-10 corpus review) has nothing for
        extract_primitives() to find — must return [] cleanly, not
        raise, regardless of where the user clicked."""
        import fitz
        from equipment_detection import find_similar_shapes
        path = os.path.join(self._tmpdir, "blank.pdf")
        doc = fitz.open()
        doc.new_page(width=200, height=200)   # no shapes drawn at all
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=100, ref_y=100, pages=[0])
        finally:
            doc.close()
        self.assertEqual(results, [])

    def test_caps_at_50_results(self):
        import fitz
        from equipment_detection import find_similar_shapes
        path = os.path.join(self._tmpdir, "many.pdf")
        doc = fitz.open()
        page = doc.new_page(width=2000, height=2000)
        shape = page.new_shape()
        for i in range(60):
            cx, cy = 40 + (i % 10) * 60, 40 + (i // 10) * 60
            shape.draw_polyline([fitz.Point(cx - 10, cy - 10), fitz.Point(cx - 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(cx + 10, cy - 10), fitz.Point(cx + 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=40, ref_y=40,
                                          pages=[0], min_similarity=0.0)
        finally:
            doc.close()
        self.assertLessEqual(len(results), 50)


class NextTagSequenceTests(unittest.TestCase):
    """_next_tag_sequence() (equipment_detection.py) — mass-tagging in
    EquipmentMarkerReviewDialog (2026-08-15, see NOTES.md "Hitta
    liknande symbol" — uppföljningsfunktioner)."""

    def test_increments_preserving_zero_padding(self):
        from equipment_detection import _next_tag_sequence
        self.assertEqual(_next_tag_sequence('FT-009', 3), ['FT-009', 'FT-010', 'FT-011'])

    def test_does_not_overflow_into_extra_digits(self):
        from equipment_detection import _next_tag_sequence
        # "101" is a 3-digit run — the 900th increment must still be
        # zero-padded to 3 digits' worth of width, not silently grown.
        seq = _next_tag_sequence('V-101', 2)
        self.assertEqual(seq, ['V-101', 'V-102'])

    def test_skips_tags_already_taken(self):
        from equipment_detection import _next_tag_sequence
        seq = _next_tag_sequence('V-101', 3, existing_tags=['V-102'])
        self.assertEqual(seq, ['V-101', 'V-103', 'V-104'])

    def test_skips_tags_already_taken_case_insensitively(self):
        from equipment_detection import _next_tag_sequence
        seq = _next_tag_sequence('V-101', 2, existing_tags=['v-102'])
        self.assertEqual(seq, ['V-101', 'V-103'])

    def test_no_digit_run_falls_back_to_dash_suffix(self):
        from equipment_detection import _next_tag_sequence
        self.assertEqual(_next_tag_sequence('PUMP', 3), ['PUMP', 'PUMP-2', 'PUMP-3'])

    def test_never_generates_a_duplicate_within_the_same_batch(self):
        from equipment_detection import _next_tag_sequence
        seq = _next_tag_sequence('V-1', 10)
        self.assertEqual(len(seq), len(set(t.upper() for t in seq)))


class ApplyPageRotationsTests(unittest.TestCase):
    """equipment_detection.apply_page_rotations() (2026-08-15, see
    NOTES.md "Hitta liknande symbol placerar fel — sidrotation"). Real
    bug: "Det ser ut som den hittar massa liknande med vektor men den
    placerar ut dem felaktigt på P&ID:et." Root cause: a background
    worker's own freshly-opened fitz.Document never sees
    PIDGraphicsView's live manual-rotation-override state, so a page
    with an override (confirmed on the real active project: page 0 has
    a 90-degree override in pid_page_rotation) gets its vector geometry
    computed in the WRONG coordinate space — cluster_similarity's shape
    scoring is already rotation-invariant in 90-degree steps so matches
    are still found, but their x/y/outline land nowhere near where the
    live view expects them."""

    def test_noop_without_any_override(self):
        import fitz
        import equipment_detection as ed
        doc = fitz.open()
        doc.new_page(width=200, height=100)
        ed.apply_page_rotations(doc, None)
        self.assertEqual(doc[0].rotation, 0)
        ed.apply_page_rotations(doc, {})
        self.assertEqual(doc[0].rotation, 0)
        doc.close()

    def test_composes_extra_rotation_with_intrinsic(self):
        import fitz
        import equipment_detection as ed
        doc = fitz.open()
        doc.new_page(width=200, height=100)
        doc[0].set_rotation(0)
        ed.apply_page_rotations(doc, {0: 90})
        self.assertEqual(doc[0].rotation, 90)
        doc.close()

    def test_out_of_range_page_numbers_are_ignored(self):
        import fitz
        import equipment_detection as ed
        doc = fitz.open()
        doc.new_page(width=200, height=100)
        ed.apply_page_rotations(doc, {5: 90})   # must not raise
        self.assertEqual(doc[0].rotation, 0)
        doc.close()

    def test_same_primitive_lands_in_the_same_coordinates_as_the_live_view(self):
        """The actual bug, reproduced directly: without re-applying the
        override, a freshly-opened document computes primitives in a
        completely different coordinate space than the live view's own
        (already-overridden) document — this is what silently misplaced
        every "hitta liknande symbol" result."""
        import fitz
        import symbol_geometry as sg
        import equipment_detection as ed
        path = None
        import tempfile, os
        tmpdir = tempfile.mkdtemp(prefix="hazop_pagerot_test_")
        path = os.path.join(tmpdir, "t.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=100)
        shape = page.new_shape()
        shape.draw_line(fitz.Point(10, 20), fitz.Point(30, 20))
        shape.finish(color=(0, 0, 0))
        shape.commit()
        doc.save(path)
        doc.close()

        # LIVE view: override applied directly to the live document.
        live_doc = fitz.open(path)
        live_doc[0].set_rotation(90)
        live_bbox = sg.extract_primitives(live_doc[0])[0]['bbox']
        live_doc.close()

        # Worker WITHOUT the fix: fresh doc, override never re-applied.
        broken_doc = fitz.open(path)
        broken_bbox = sg.extract_primitives(broken_doc[0])[0]['bbox']
        broken_doc.close()
        self.assertNotEqual(broken_bbox, live_bbox,
            "sanity check: an unfixed fresh document really does diverge")

        # Worker WITH the fix.
        fixed_doc = fitz.open(path)
        ed.apply_page_rotations(fixed_doc, {0: 90})
        fixed_bbox = sg.extract_primitives(fixed_doc[0])[0]['bbox']
        fixed_doc.close()
        self.assertEqual(fixed_bbox, live_bbox)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════
# Multi-core parallel P&ID analysis (2026-08-07) — see NOTES.md "Flerkärnig
# parallellisering av Analysera P&ID". scan_pdf_for_equipment() and
# detect_equipment_and_valves() can now be split across several worker
# PROCESSES. These tests verify: (1) the merge/split logic is correct and
# order-independent regardless of what order workers finish in, without
# needing to spin up real OS processes for every case; (2) the two real
# multiprocessing round-trips that DO matter (cancellation always still
# emits the "finished" signal) actually work end to end; (3) the OCR-crop
# optimization's dispatch/fallback/coordinate-translation logic, mocking
# the OCR engine layer itself since this suite never depends on a real
# installed OCR engine (none of tesseract/easyocr/rapidocr are guaranteed
# present in every environment this suite runs in).
# ══════════════════════════════════════════════════════════════════════════

class SplitIntoChunksTests(unittest.TestCase):
    def test_contiguous_and_covers_all_pages(self):
        from equipment_detection import _split_into_chunks
        chunks = _split_into_chunks(10, 3)
        self.assertEqual(sorted(p for c in chunks for p in c), list(range(10)))
        for c in chunks:
            self.assertEqual(
                c, list(range(c[0], c[0] + len(c))),
                "each chunk must be contiguous and ascending")

    def test_more_workers_than_pages_caps_at_page_count(self):
        from equipment_detection import _split_into_chunks
        chunks = _split_into_chunks(3, 10)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(sorted(p for c in chunks for p in c), [0, 1, 2])

    def test_zero_pages_returns_no_chunks(self):
        from equipment_detection import _split_into_chunks
        self.assertEqual(_split_into_chunks(0, 4), [])


class MergeScanPageRowsTests(unittest.TestCase):
    """_merge_scan_page_rows (equipment_detection.py) combines flat
    per-page scan rows into scan_pdf_for_equipment()'s nested result
    shape, order-independently — pages processed out of order across
    parallel worker processes must still produce the same result as the
    original sequential 0..N loop."""

    def test_lowest_page_wins_regardless_of_row_order(self):
        from equipment_detection import _merge_scan_page_rows
        row_page5 = {'tag': 'V-1', 'prefix': 'V', 'page_num': 5,
                     'cx': 50.0, 'cy': 60.0, 'from_ocr': False, 'source': 'native'}
        row_page2 = {'tag': 'V-1', 'prefix': 'V', 'page_num': 2,
                     'cx': 10.0, 'cy': 20.0, 'from_ocr': False, 'source': 'native'}
        # Deliberately out of page order — simulates a worker for a LATER
        # page range finishing before one for an EARLIER range.
        result = _merge_scan_page_rows([row_page5, row_page2])
        self.assertEqual(result['V']['pages']['V-1'], 2)
        self.assertEqual(result['V']['positions']['V-1'], (10.0, 20.0))

    def test_prefers_real_coordinates_over_pass1_placeholder_on_same_page(self):
        from equipment_detection import _merge_scan_page_rows
        placeholder = {'tag': 'V-1', 'prefix': 'V', 'page_num': 3,
                       'cx': 0.0, 'cy': 0.0, 'from_ocr': False, 'source': 'native'}
        precise = {'tag': 'V-1', 'prefix': 'V', 'page_num': 3,
                   'cx': 42.0, 'cy': 24.0, 'from_ocr': False, 'source': 'native'}
        result = _merge_scan_page_rows([placeholder, precise])
        self.assertEqual(result['V']['positions']['V-1'], (42.0, 24.0))

    def test_ocr_pages_is_a_union_across_all_rows(self):
        from equipment_detection import _merge_scan_page_rows
        rows = [
            {'tag': 'V-1', 'prefix': 'V', 'page_num': 1, 'cx': 1.0, 'cy': 1.0,
             'from_ocr': True, 'source': 'ocr'},
            {'tag': 'V-1', 'prefix': 'V', 'page_num': 4, 'cx': 4.0, 'cy': 4.0,
             'from_ocr': True, 'source': 'ocr'},
        ]
        result = _merge_scan_page_rows(rows)
        self.assertEqual(result['V']['ocr_pages'], {1, 4})
        self.assertEqual(result['V']['pages']['V-1'], 1)

    def test_matches_sequential_scan_pdf_for_equipment_on_a_real_document(self):
        """The determinism guarantee end to end: scanning a real multi-page
        PDF page-by-page (as _scan_page_range_worker would, one chunk at a
        time) and merging out of order must produce an identical result to
        running scan_pdf_for_equipment() sequentially."""
        import fitz
        from equipment_detection import (
            scan_pdf_for_equipment, _scan_one_page_native, _merge_scan_page_rows)
        doc = fitz.open()
        for i in range(4):
            p = doc.new_page(width=200, height=200)
            p.insert_text(fitz.Point(10, 20), f"V-{100 + i}", fontsize=10)
        sequential = scan_pdf_for_equipment(doc, use_ocr=False)
        sequential.pop('_meta', None)

        # Simulate two workers, each handling a contiguous chunk, whose
        # rows are then merged in REVERSE completion order (the worker for
        # pages 2-3 "finishes" before the one for pages 0-1).
        rows_a = []
        for pn in (2, 3):
            rows_a.extend(_scan_one_page_native(doc[pn], pn))
        rows_b = []
        for pn in (0, 1):
            rows_b.extend(_scan_one_page_native(doc[pn], pn))
        merged = _merge_scan_page_rows(rows_a + rows_b)
        merged.pop('_meta', None)
        doc.close()

        self.assertEqual(merged, sequential)


class AnalyzePageRangeWorkerTests(unittest.TestCase):
    """_analyze_page_range_worker (equipment_detection.py) — the
    multiprocessing target wrapping detect_equipment_and_valves() for a
    page range — must produce the same rows as calling
    detect_equipment_and_valves() sequentially across ALL pages, whether
    split into chunks or not (that function was already confirmed fully
    page-independent — this locks in the wrapper's own correctness)."""

    def test_matches_sequential_detect_equipment_and_valves(self):
        import fitz
        from equipment_detection import (
            detect_equipment_and_valves, _analyze_page_range_worker, _split_into_chunks)
        tmpdir = tempfile.mkdtemp(prefix="hazop_paranalyze_test_")
        try:
            pdf_path = os.path.join(tmpdir, "test.pdf")
            doc = fitz.open()
            tag_points = []
            for i in range(6):
                p = doc.new_page(width=200, height=200)
                tag = f"V-{100 + i}"
                p.insert_text(fitz.Point(10, 20), tag, fontsize=10)
                tag_points.append((tag, 'V', i, None, None, 1.0))
            doc.save(pdf_path)
            doc.close()

            doc2 = fitz.open(pdf_path)
            try:
                seq_results, seq_rejected = detect_equipment_and_valves(doc2, tag_points)
            finally:
                doc2.close()

            chunks = _split_into_chunks(6, 3)
            all_results, all_rejected = [], []
            for chunk in chunks:
                results, rejected = _analyze_page_range_worker(
                    pdf_path, chunk, tag_points, 0.5, 0.3)
                all_results.extend(results)
                all_rejected.extend(rejected)

            def _key(r):
                return (r['tag'], r['page'], r['comp_type'])
            self.assertEqual(sorted(map(_key, all_results)), sorted(map(_key, seq_results)))
            self.assertEqual(len(all_rejected), len(seq_rejected))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class NumericPrefixParsingTests(unittest.TestCase):
    """_equip_prefix_from_tag/_parse_tag used to treat a purely numeric
    text span (e.g. a title-block date "2019-09-30") as a valid
    equipment tag with prefix "2019" (2026-08-10, see NOTES.md known
    limitations — fixed). A prefix must contain at least one letter."""

    def test_date_like_string_has_no_prefix(self):
        from equipment_detection import _equip_prefix_from_tag
        self.assertEqual(_equip_prefix_from_tag("2019-09-30"), '')

    def test_pure_digit_string_has_no_prefix(self):
        from equipment_detection import _equip_prefix_from_tag
        self.assertEqual(_equip_prefix_from_tag("123456"), '')

    def test_real_tag_prefix_still_works(self):
        from equipment_detection import _equip_prefix_from_tag
        self.assertEqual(_equip_prefix_from_tag("PCV-101"), 'PCV')
        self.assertEqual(_equip_prefix_from_tag("HV0063"), 'HV')

    def test_parse_tag_rejects_date_like_string(self):
        from equipment_detection import _parse_tag
        self.assertEqual(_parse_tag("2019-09-30"), (None, None))


class TagFallbackDigitGuardTests(unittest.TestCase):
    """'jag vill att programmet blir mycket bättre och snabbare på att
    analysera och hitta objekt på P&ID' (2026-08-10) — a baseline sweep
    against a new, ~20-vendor P&ID corpus found that 57% of all "tag"
    hits had no mapped equipment type. Root cause: _parse_tag's and
    _score_tag_word's last-resort fallbacks accepted ANY 2+ letter word
    with no digit at all as a "tag" (title-block/disclaimer text like
    "THIS", "CONFIDENTIAL", "REPRODUCTION", "NODE" — none of which have
    a digit, unlike every real tag format documented in this module).
    Fixed by requiring at least one digit before the fallback fires —
    every other tag regex already required this; only these two loose
    fallbacks didn't."""

    def test_parse_tag_rejects_plain_english_words(self):
        from equipment_detection import _parse_tag
        for word in ("THIS", "CONFIDENTIAL", "REPRODUCTION", "NODE", "AND", "FOR"):
            self.assertEqual(_parse_tag(word), (None, None), f"{word!r} must not parse as a tag")

    def test_parse_tag_still_accepts_real_tags(self):
        from equipment_detection import _parse_tag
        self.assertEqual(_parse_tag("PCV-101"), ("PCV-101", "PCV"))
        self.assertEqual(_parse_tag("HV0063"), ("HV-0063", "HV"))
        self.assertEqual(_parse_tag("NODE-1"), ("NODE-1", "NODE"))

    def test_score_tag_word_rejects_plain_english_words(self):
        from equipment_detection import _score_tag_word
        for word in ("THIS", "CONFIDENTIAL", "AND", "TO"):
            self.assertEqual(_score_tag_word(word), (None, 0), f"{word!r} must not score as a tag")

    def test_score_tag_word_still_finds_real_tags(self):
        from equipment_detection import _score_tag_word
        tag, score = _score_tag_word("HV-101")
        self.assertEqual(tag, "HV-101")
        self.assertGreaterEqual(score, 2)

    def test_parse_tag_exempts_bare_known_instrument_codes(self):
        """'programmet har svårt för att känna igen instrument PI, FI'
        (2026-08-11) — a real regression the digit guard above
        introduced: LKAB P&IDs genuinely label some LOCAL indicators
        with just the bare ISA code and no loop number at all (a real
        convention KNOWN_PREFIXES itself already anticipated —
        'Tryckmätare (lokal)', 'Flödesmätare (lokal)'). Confirmed via
        git-history diff against the real LKAB reference corpus: 'PI'/
        'FI' were recognised before the digit guard, silently stopped
        being recognised after it, with no other code change in
        between."""
        from equipment_detection import _parse_tag
        self.assertEqual(_parse_tag("PI"), ("PI", "PI"))
        self.assertEqual(_parse_tag("FI"), ("FI", "FI"))

    def test_parse_tag_still_rejects_onoff_state_annotation(self):
        """The exemption above must be an EXACT whole-string match only
        — "ON" is ALSO a real KNOWN_PREFIXES entry (Avstängningsventil),
        but "ON/OFF" (confirmed real noise on a NYA-corpus file,
        2026-08-10) must stay rejected: it's ordinary valve-state
        annotation text, not a bare instrument tag, even though "ON"
        appears as a substring of it."""
        from equipment_detection import _parse_tag
        self.assertEqual(_parse_tag("ON/OFF"), (None, None))
        self.assertEqual(_parse_tag("ON/OFFSWITCHLOCALLYMOUNTED"), (None, None))

    def test_score_tag_word_exempts_bare_known_instrument_codes(self):
        from equipment_detection import _score_tag_word
        tag, score = _score_tag_word("PI")
        self.assertEqual(tag, "PI")
        self.assertGreaterEqual(score, 1)

    def test_score_tag_word_still_rejects_onoff_state_annotation(self):
        from equipment_detection import _score_tag_word
        self.assertEqual(_score_tag_word("ON/OFF"), (None, 0))


class ExtTagConsolidationTests(unittest.TestCase):
    """2026-08-21: _parse_tag/_pick_best_tag/_score_tag_word each had their
    own hand-copied "did _EXT_TAG_RE actually find a real tag" check —
    comments in the code already admitted they had to be kept in sync
    manually ("same as _parse_tag's own EXT_TAG_RE branch"). They'd
    already drifted: _pick_best_tag had NO validity check at all, and
    _score_tag_word returned the raw un-normalised match instead of
    running it through _normalize_ext_tag like the other two. Consolidated
    into one shared _match_ext_tag() helper; these tests lock in both the
    fixes and the (deliberately unchanged) existing behaviour."""

    def test_pick_best_tag_rejects_a_coincidental_ext_tag_match_with_no_real_prefix(self):
        """'DN50-PN16' (two pipe-spec codes _equip_prefix_from_tag's own
        `skip` set exists to reject) matches the loose _EXT_TAG_RE pattern
        shape but is not a real tag. Before the fix, _pick_best_tag
        returned it completely unvalidated, verbatim."""
        from equipment_detection import _pick_best_tag
        self.assertNotEqual(_pick_best_tag("DN50-PN16"), "DN50-PN16")

    def test_score_tag_word_normalises_a_dotted_compound_tag_like_the_other_two_functions(self):
        """Before the fix, _score_tag_word returned the raw, un-normalised
        match ('E1.M1.GPA4') while _parse_tag/_pick_best_tag both
        normalised the same input to 'E1.M1-GPA4' — the exact
        two-different-strings-for-one-instrument bug _normalize_ext_tag
        was written to prevent (2026-08-13), reintroduced through this
        function's own separate copy."""
        from equipment_detection import _parse_tag, _pick_best_tag, _score_tag_word
        expected = _parse_tag("E1.M1.GPA4")[0]
        self.assertEqual(_pick_best_tag("E1.M1.GPA4"), expected)
        tag, score = _score_tag_word("E1.M1.GPA4")
        self.assertEqual(tag, expected)
        self.assertEqual(score, 2)

    def test_single_letter_prefix_compound_tag_still_accepted_by_parse_and_pick_best(self):
        """_parse_tag/_pick_best_tag have always accepted a single-letter
        recognised prefix in a compound tag (e.g. area-qualified 'E');
        the consolidation must not raise that bar for them."""
        from equipment_detection import _parse_tag, _pick_best_tag
        self.assertEqual(_parse_tag("20-E-101"), ("20-E-101", "E"))
        self.assertEqual(_pick_best_tag("20-E-101"), "20-E-101")

    def test_single_letter_prefix_compound_tag_still_scores_zero(self):
        """_score_tag_word has always required a 2+ letter prefix for its
        compound-tag confidence tier — a deliberate, more conservative
        scoring choice, not a bug, and must be unaffected by the shared
        helper's default (min_prefix_len=1, used by the other two)."""
        from equipment_detection import _score_tag_word
        self.assertEqual(_score_tag_word("20-E-101"), (None, 0))

    def test_all_three_functions_still_agree_on_ordinary_real_tags(self):
        """Sweep of representative real plant-convention tags (from the
        module's own docstrings) — normalised forms must match exactly
        across all three functions, and scoring must be the high-confidence
        tier for every one of them."""
        from equipment_detection import _parse_tag, _pick_best_tag, _score_tag_word
        cases = [
            "20-PCV-101", "100-MAS10A", "G45-100-EAS10A",
            "60-RV-009", "=E1.M1.QMA081",
        ]
        for raw in cases:
            norm, pfx = _parse_tag(raw)
            with self.subTest(raw=raw):
                self.assertIsNotNone(norm)
                self.assertEqual(_pick_best_tag(raw), norm)
                tag, score = _score_tag_word(raw)
                self.assertEqual(tag, norm)
                self.assertEqual(score, 2)


class SpatialCombineGlueWordTests(unittest.TestCase):
    """_spatial_combine()'s plain inter-word-gap join (for tag sub-parts
    the PDF exporter split apart, e.g. "20"-"PCV"-"101") also fused
    ordinary adjacent prose words into bogus tag candidates when the gap
    between them happened to be small — confirmed on a real file
    (2026-08-10, see NOTES.md): "TO" immediately followed by "PRI-421"
    (normal sentence text, e.g. '... TO PRI-421 ...') combined into
    "TOPRI-421", which then parsed as a fake tag with prefix "TOPRI".
    Fixed by refusing to extend a group past a short, common English
    connector word via the gap-based path (the separator-token path,
    e.g. a literal "-" between two real tag halves, is unaffected)."""

    def test_common_connector_word_does_not_glue_onto_following_tag(self):
        from equipment_detection import _spatial_combine
        words = [
            (100.0, 100.0, 110.0, 108.0, "TO"),
            (112.0, 100.0, 140.0, 108.0, "PRI-421"),
        ]
        results = _spatial_combine(words, gap_limit=22.0)
        texts = [r[0] for r in results]
        self.assertNotIn("TOPRI-421", texts)
        self.assertIn("PRI-421", texts)
        self.assertIn("TO", texts)

    def test_real_tag_subparts_still_combine(self):
        """Sanity check: genuine split tag fragments (no connector word
        involved) must still combine exactly as before."""
        from equipment_detection import _spatial_combine
        words = [
            (100.0, 100.0, 112.0, 108.0, "20"),
            (114.0, 100.0, 118.0, 108.0, "-"),
            (120.0, 100.0, 134.0, 108.0, "PCV"),
            (136.0, 100.0, 140.0, 108.0, "-"),
            (142.0, 100.0, 156.0, 108.0, "101"),
        ]
        results = _spatial_combine(words, gap_limit=22.0)
        texts = [r[0] for r in results]
        self.assertIn("20-PCV-101", texts)


class NewCorpusPrefixReviewTests(unittest.TestCase):
    """Two genuinely new, real-corpus-confirmed prefixes added to
    KNOWN_PREFIXES (2026-08-10, see NOTES.md): HVPT (compound hand-valve
    + pressure-transmitter tag) and bare FC (Flow Controller, distinct
    from the existing FCV). Also confirms PN (pipe nominal pressure
    rating, e.g. PN100/PN-30) is now excluded the same way DN already
    was — it was showing up as a false "tag" on a real file."""

    def test_hvpt_recognised(self):
        from equipment_detection import _equip_prefix_from_tag, KNOWN_PREFIXES
        self.assertEqual(_equip_prefix_from_tag("HVPT-301"), "HVPT")
        self.assertIn("HVPT", KNOWN_PREFIXES)

    def test_fc_recognised_distinct_from_fcv(self):
        from equipment_detection import _equip_prefix_from_tag, KNOWN_PREFIXES
        self.assertEqual(_equip_prefix_from_tag("FC-E-20A"), "FC")
        self.assertEqual(_equip_prefix_from_tag("FCV-101"), "FCV")
        self.assertIn("FC", KNOWN_PREFIXES)
        self.assertIn("FCV", KNOWN_PREFIXES)

    def test_pn_pressure_rating_excluded_like_dn(self):
        from equipment_detection import _equip_prefix_from_tag
        self.assertEqual(_equip_prefix_from_tag("PN100BSPP"), '')
        self.assertEqual(_equip_prefix_from_tag("PN-30"), '')




if __name__ == "__main__":
    unittest.main()
