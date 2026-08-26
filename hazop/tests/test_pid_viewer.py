#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering pid_viewer.py, plus any cross-module glue they
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
# 4. ConnectorAnalyzer thread-hang regression (bug #5)
# ══════════════════════════════════════════════════════════════════════════

class ConnectorAnalyzerHangTests(unittest.TestCase):
    """ConnectorAnalyzer.run() used to wrap only the initial fitz.open() call
    in try/except. Any exception raised afterwards (page loop, dialect
    detection, OCR, connection matching, layout proposal) propagated out of
    QThread.run() uncaught. PyQt6 swallows exceptions raised inside
    QThread.run() -- it prints a traceback to stderr but never re-raises and
    never emits any signal -- so `finished_analysis` was never fired. The
    caller (PIDPanel._run_smart_layout) shows a modal, non-cancellable
    QProgressDialog that only closes when `finished_analysis` fires, so the
    whole P&ID panel hung forever with no way out.

    This test simulates a mid-analysis failure (fitz.open() succeeds, but
    the very next call on the document raises) and asserts that
    `finished_analysis` still fires and the (fake) doc still gets closed.
    """

    def setUp(self):
        _ensure_qapp()

    def test_run_emits_finished_analysis_on_mid_scan_exception(self):
        import pid_viewer

        class _ExplodingDoc:
            """Fake fitz.Document whose page_count property raises the
            moment ConnectorAnalyzer.run() tries to use it, simulating a
            malformed-PDF / fitz failure partway through analysis (i.e.
            *after* fitz.open() itself has already "succeeded")."""

            def __init__(self):
                self.closed = False

            @property
            def page_count(self):
                raise RuntimeError("simulated mid-scan fitz failure")

            def close(self):
                self.closed = True

        fake_doc = _ExplodingDoc()
        analyzer = pid_viewer.ConnectorAnalyzer(
            pdf_path="unused.pdf",
            page_count=3,
            page_widths_pdf={0: 100.0},
            page_heights_pdf={0: 100.0},
            render_scale=1.0,
        )

        received = {}

        def _on_done(connectors, connections, layout, sheet_num_map):
            received['args'] = (connectors, connections, layout, sheet_num_map)

        analyzer.finished_analysis.connect(_on_done)

        with unittest.mock.patch.object(pid_viewer.fitz, "open",
                                         return_value=fake_doc):
            # Run synchronously (not analyzer.start()) so the test doesn't
            # depend on Qt event-loop/thread timing -- run() is a plain
            # method and safe to call directly for this test.
            analyzer.run()

        self.assertIn(
            'args', received,
            "finished_analysis was never emitted after a mid-scan exception "
            "-- this reproduces the hang: the caller's modal progress "
            "dialog would wait forever.")
        self.assertEqual(received['args'], ([], [], {}, {}))
        self.assertTrue(
            fake_doc.closed,
            "ConnectorAnalyzer.run() must close() the fitz doc even when "
            "analysis fails mid-scan (no more leaked file handles).")


class EquipmentMarkerReviewDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_eqdialog_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)
        cur = self.db.conn.execute(
            "INSERT INTO equipment_catalog (tag, prefix, pid_page, equipment_type) "
            "VALUES (?,?,?,?)", ("V-101", "V", 0, "Ventil"))
        self.db.commit()
        self.equipment_id = cur.lastrowid

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _sample_results(self):
        return [
            {'tag': 'V-101', 'page': 0, 'comp_type': 'Ventil', 'x': 100.0, 'y': 100.0,
             'confidence': 0.95, 'link_method': 'leader',
             'outline': [[90, 90], [110, 90], [110, 110], [90, 110]],
             'equipment_id': self.equipment_id},
            {'tag': 'V-999', 'page': 0, 'comp_type': 'Ventil', 'x': 0.0, 'y': 0.0,
             'confidence': 0.0, 'link_method': 'not_found',
             'outline': [], 'equipment_id': None},
        ]

    def test_table_populates_from_results(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertEqual(dlg._tbl.rowCount(), 2)
            self.assertEqual(dlg._tbl.item(0, dlg._C_TAG).text(), 'V-101')
        finally:
            dlg.deleteLater()

    def test_high_confidence_row_defaults_checked_low_confidence_unchecked(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtCore import Qt
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertEqual(dlg._tbl.item(0, dlg._C_CHK).checkState(), Qt.CheckState.Checked)
            self.assertEqual(dlg._tbl.item(1, dlg._C_CHK).checkState(), Qt.CheckState.Unchecked)
        finally:
            dlg.deleteLater()

    def test_similarity_search_results_default_checked_regardless_of_confidence(self):
        """"Här skall alla vara förvalda per default" (2026-08-16, see
        NOTES.md "zoomad bild per rad i granskningsdialogen") — a
        "hitta liknande symbol" result (link_method='similar') must
        default to checked even at a low similarity score, unlike a
        shape-only autodetection hit (which only defaults checked at
        detection_confidence>=0.5, see the 'untagged_ok' branch above).
        The user already reviewed similarity BEFORE reaching this
        dialog (picked a reference, chose a threshold) — every row here
        already passed that bar, so there is no separate confidence
        gate to apply again."""
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtCore import Qt
        results = [
            {'tag': '', 'page': 0, 'comp_type': 'Ventil', 'x': 1.0, 'y': 1.0,
             'confidence': 0.61, 'link_method': 'similar', 'outline': [],
             'equipment_id': None, 'tag_status': 'untagged',
             'temporary_id': 'UNASSIGNED-VALVE-1', 'detection_confidence': 0.61},
        ]
        dlg = EquipmentMarkerReviewDialog(results, self.db)
        try:
            self.assertEqual(dlg._tbl.item(0, dlg._C_CHK).checkState(), Qt.CheckState.Checked)
        finally:
            dlg.deleteLater()

    def test_save_writes_only_checked_rows(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            dlg._save()
            rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            self.assertEqual(len(rows), 1, "only the checked (found) row should be saved")
            self.assertEqual(rows[0]['tag'], 'V-101')
        finally:
            dlg.deleteLater()

    def test_save_with_nothing_checked_does_not_write_and_does_not_crash(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QMessageBox
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            for r in range(dlg._tbl.rowCount()):
                dlg._tbl.item(r, dlg._C_CHK).setCheckState(Qt.CheckState.Unchecked)
            with unittest.mock.patch.object(QMessageBox, 'information'):
                dlg._save()
            self.assertEqual(len(self.db.equipment_markers_for_page(0)), 0)
        finally:
            dlg.deleteLater()

    def test_editing_tag_cell_corrects_the_saved_tag(self):
        """Editing the Tagg column before saving must use the corrected text,
        not the original (possibly wrong) detected tag — this is the 'edit
        errors before saving' mechanism the review dialog exists for."""
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            dlg._tbl.item(0, dlg._C_TAG).setText('V-101A')
            dlg._save()
            rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            self.assertEqual(rows[0]['tag'], 'V-101A')
        finally:
            dlg.deleteLater()

    def test_editing_typ_cell_corrects_the_saved_type(self):
        """"Hitta liknande symbol" — uppföljningsfunktioner (2026-08-15,
        see NOTES.md) — the Typ column used to be read-only and
        _save() always wrote the frozen res['comp_type']; it's now an
        editable dropdown (2026-08-17: turned into a combobox) and
        _save() must respect a correction the same way the Tagg column
        already does."""
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            combo = dlg._tbl.cellWidget(0, dlg._C_TYPE)
            self.assertTrue(combo.isEditable(), "Typ cell must be an editable combobox")
            combo.setCurrentText('Pump')
            dlg._save()
            rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            self.assertEqual(rows[0]['comp_type'], 'Pump')
        finally:
            dlg.deleteLater()

    def test_save_assigns_active_node_to_newly_created_equipment(self):
        """"Om jag har lagt till ett objekt manuellt och lägger till
        orsaker får jag upp detta i hazop scenario men om jag har lagt
        till objekt via hitta liknande får jag inte upp det" (2026-08-17,
        see NOTES.md) — manual single placement (place_equipment_marker)
        immediately opens EquipmentDeviationBar with the active node,
        which assigns node_id there; this batch dialog never does, so a
        brand-new equipment row stayed node_id=NULL forever, and
        EquipmentDeviationBar._activate_deviation() silently drops any
        deviation/cause checked for a node_id=NULL object — exactly
        "lägger till orsaker... får inte upp det". A row with no
        equipment_id yet must get the dialog's active_node_id."""
        from pid_viewer import EquipmentMarkerReviewDialog
        node_id = self.db.add_node()
        results = [
            {'tag': 'V-999-NEW', 'page': 0, 'comp_type': 'Ventil', 'x': 5.0, 'y': 5.0,
             'confidence': 0.9, 'link_method': 'similar', 'outline': [], 'equipment_id': None},
        ]
        dlg = EquipmentMarkerReviewDialog(results, self.db, active_node_id=node_id)
        try:
            dlg._save()
            eq = self.db.get_equipment_by_tag('V-999-NEW')
            self.assertIsNotNone(eq)
            self.assertEqual(eq['node_id'], node_id)
        finally:
            dlg.deleteLater()

    def test_save_without_active_node_leaves_new_equipment_unassigned(self):
        """No active node (e.g. the plain document-wide scan, or a
        similarity search run with no node selected) must not fabricate
        a node_id out of nothing — same as before this fix."""
        from pid_viewer import EquipmentMarkerReviewDialog
        results = [
            {'tag': 'V-999-NEW2', 'page': 0, 'comp_type': 'Ventil', 'x': 5.0, 'y': 5.0,
             'confidence': 0.9, 'link_method': 'similar', 'outline': [], 'equipment_id': None},
        ]
        dlg = EquipmentMarkerReviewDialog(results, self.db)
        try:
            dlg._save()
            eq = self.db.get_equipment_by_tag('V-999-NEW2')
            self.assertIsNotNone(eq)
            self.assertIsNone(eq['node_id'])
        finally:
            dlg.deleteLater()

    def test_save_reuses_existing_equipment_by_tag_instead_of_duplicating(self):
        """A second "find similar" hit resolving to the same tag as an
        already-registered object must reuse it (matching place_
        equipment_marker's own dedup check), not spawn a duplicate,
        node_id=NULL row alongside the original — the duplicate would
        itself reproduce the exact "doesn't show up" symptom above."""
        from pid_viewer import EquipmentMarkerReviewDialog
        existing_node_id = self.db.add_node()
        existing_id = self.db.add_equipment_item('V-777', 'V-777', 'V', 0, 'Ventil', '', 0)
        self.db.set_equipment_node(existing_id, existing_node_id)

        other_node_id = self.db.add_node()
        results = [
            {'tag': 'V-777', 'page': 0, 'comp_type': 'Ventil', 'x': 9.0, 'y': 9.0,
             'confidence': 0.9, 'link_method': 'similar', 'outline': [], 'equipment_id': None},
        ]
        dlg = EquipmentMarkerReviewDialog(results, self.db, active_node_id=other_node_id)
        try:
            dlg._save()
            matches = [e for e in self.db.equipment_items() if e['tag'] == 'V-777']
            self.assertEqual(len(matches), 1, "must reuse the existing row, not duplicate it")
            self.assertEqual(matches[0]['id'], existing_id)
            self.assertEqual(matches[0]['node_id'], existing_node_id,
                "reusing an existing object must not steal it onto a different node")
        finally:
            dlg.deleteLater()

    def test_typ_dropdown_includes_standardobjekt_entries(self):
        """"Typen skall vara en gardinlista enligt vad som finns under
        standardobjekt" (2026-08-17, see NOTES.md) — the Typ column's
        options must include every name from Inställningar →
        Standardobjekt, not just the hard-coded COMPONENT_TYPES set."""
        from pid_viewer import EquipmentMarkerReviewDialog
        self.db.add_standard_object('Facklagasanalysator')
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            combo = dlg._tbl.cellWidget(0, dlg._C_TYPE)
            items = [combo.itemText(i) for i in range(combo.count())]
            self.assertIn('Facklagasanalysator', items)
        finally:
            dlg.deleteLater()

    def test_metod_column_is_narrow(self):
        """"metod kan vara en mycket mindre kolumn" (2026-08-17, see
        NOTES.md)."""
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertLess(dlg._tbl.columnWidth(dlg._C_METHOD), 100)
        finally:
            dlg.deleteLater()

    def test_dialog_is_larger_than_before(self):
        """"Du kan göra hela rutan lite större så blir det lättare att
        se" (2026-08-17, see NOTES.md)."""
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertGreaterEqual(dlg.minimumWidth(), 1000)
            self.assertGreaterEqual(dlg.minimumHeight(), 640)
        finally:
            dlg.deleteLater()

    def test_autodetect_tags_button_disabled_without_pdf_path(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertFalse(dlg._autodetect_btn.isEnabled())
        finally:
            dlg.deleteLater()

    def test_autodetect_tags_fills_in_the_nearest_native_text_for_checked_rows(self):
        """"en knapp som heter autodetektera tagnummer som tar den
        närmaste... och presenterar rätt tag nummer" (2026-08-17, see
        NOTES.md) — re-runs find_tag_near_point per checked row and
        writes whatever it finds into that row's own Tagg cell."""
        import fitz
        from pid_viewer import EquipmentMarkerReviewDialog
        path = os.path.join(self._tmpdir, "auto.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        page.insert_text((95, 100), "V-777")
        doc.save(path)
        doc.close()
        results = [
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 100.0, 'y': 100.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-0', 'detection_confidence': 0.9},
        ]
        dlg = EquipmentMarkerReviewDialog(results, self.db, pdf_path=path)
        try:
            self.assertTrue(dlg._autodetect_btn.isEnabled())
            with unittest.mock.patch.object(QMessageBox, 'information'):
                dlg._autodetect_tags()
            self.assertEqual(dlg._tbl.item(0, dlg._C_TAG).text(), 'V-777')
        finally:
            dlg.deleteLater()

    def test_autodetect_tags_leaves_tag_untouched_when_nothing_found_nearby(self):
        import fitz
        from pid_viewer import EquipmentMarkerReviewDialog
        path = os.path.join(self._tmpdir, "empty.pdf")
        doc = fitz.open()
        doc.new_page(width=200, height=200)
        doc.save(path)
        doc.close()
        results = [
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 100.0, 'y': 100.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-0', 'detection_confidence': 0.9},
        ]
        dlg = EquipmentMarkerReviewDialog(results, self.db, pdf_path=path)
        try:
            with unittest.mock.patch.object(QMessageBox, 'information'):
                dlg._autodetect_tags()
            self.assertEqual(dlg._tbl.item(0, dlg._C_TAG).text(), 'SIMILAR-0-0')
        finally:
            dlg.deleteLater()

    def test_autodetect_tags_with_nothing_checked_shows_info(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            dlg._tbl.item(0, dlg._C_CHK).setCheckState(Qt.CheckState.Unchecked)
            dlg._tbl.item(1, dlg._C_CHK).setCheckState(Qt.CheckState.Unchecked)
            with unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                dlg._autodetect_tags()
            mock_info.assert_called_once()
        finally:
            dlg.deleteLater()

    def test_mass_apply_sets_type_and_tag_sequence_on_checked_rows_only(self):
        """"bra om jag kan välja att koppla det till typ av objekt och
        förhoppningsvis tagg nummer" (2026-08-15) — "Tillämpa på
        ikryssade" writes the chosen type and an auto-incrementing tag
        sequence into every CHECKED row, leaving unchecked rows alone."""
        from pid_viewer import EquipmentMarkerReviewDialog
        results = [
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 1.0, 'y': 1.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-0', 'detection_confidence': 0.9},
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 2.0, 'y': 2.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-1', 'detection_confidence': 0.8},
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 3.0, 'y': 3.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-2', 'detection_confidence': 0.05},
        ]
        dlg = EquipmentMarkerReviewDialog(results, self.db)
        try:
            dlg._tbl.item(2, dlg._C_CHK).setCheckState(Qt.CheckState.Unchecked)
            dlg._mass_type_cb.setCurrentText('Ventil')
            dlg._mass_tag_edit.setText('V-201')
            dlg._apply_mass_tag()
            self.assertEqual(dlg._tbl.cellWidget(0, dlg._C_TYPE).currentText(), 'Ventil')
            self.assertEqual(dlg._tbl.cellWidget(1, dlg._C_TYPE).currentText(), 'Ventil')
            self.assertEqual(dlg._tbl.item(0, dlg._C_TAG).text(), 'V-201')
            self.assertEqual(dlg._tbl.item(1, dlg._C_TAG).text(), 'V-202')
            # Unchecked row untouched (its Tagg cell pre-fills with the
            # untagged placeholder temporary_id — see _populate())
            self.assertEqual(dlg._tbl.cellWidget(2, dlg._C_TYPE).currentText(), '')
            self.assertEqual(dlg._tbl.item(2, dlg._C_TAG).text(), 'SIMILAR-0-2')
        finally:
            dlg.deleteLater()

    def test_mass_apply_persists_via_normal_save(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        results = [
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 1.0, 'y': 1.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-0', 'detection_confidence': 0.9},
            {'tag': '', 'page': 0, 'comp_type': '', 'x': 2.0, 'y': 2.0, 'outline': [],
             'link_method': 'similar', 'tag_status': 'untagged',
             'temporary_id': 'SIMILAR-0-1', 'detection_confidence': 0.9},
        ]
        dlg = EquipmentMarkerReviewDialog(results, self.db)
        try:
            dlg._mass_type_cb.setCurrentText('Ventil')
            dlg._mass_tag_edit.setText('V-301')
            dlg._apply_mass_tag()
            dlg._save()
            rows = sorted((dict(r) for r in self.db.equipment_markers_for_page(0)),
                         key=lambda r: r['tag'])
            self.assertEqual([r['tag'] for r in rows], ['V-301', 'V-302'])
            self.assertEqual([r['comp_type'] for r in rows], ['Ventil', 'Ventil'])
        finally:
            dlg.deleteLater()

    def test_mass_apply_with_nothing_checked_shows_info(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QMessageBox
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            for r in range(dlg._tbl.rowCount()):
                dlg._tbl.item(r, dlg._C_CHK).setCheckState(Qt.CheckState.Unchecked)
            dlg._mass_type_cb.setCurrentText('Ventil')
            with unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                dlg._apply_mass_tag()
            mock_info.assert_called_once()
        finally:
            dlg.deleteLater()

    def test_no_edit_shape_button_without_pdf_path(self):
        """"finns det något bra sätt att städa bort ledningen från en
        ventil eller pump" (2026-08-15, see NOTES.md) — without a
        pdf_path there's nothing to re-resolve a cluster from, so the
        column stays empty rather than showing a button that can't work."""
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertIsNone(dlg._tbl.cellWidget(0, dlg._C_EDIT))
        finally:
            dlg.deleteLater()

    def test_thumbnail_rendered_for_rows_with_a_real_pdf(self):
        """"Dvs så man kan se grafiskt att det är korrekt och inte bara
        en lista" (2026-08-16, see NOTES.md "zoomad bild per rad i
        granskningsdialogen") — each row with a resolvable page/pdf_path
        gets a small rendered crop, not just text columns."""
        import fitz
        from PyQt6.QtWidgets import QLabel
        from pid_viewer import EquipmentMarkerReviewDialog
        path = os.path.join(self._tmpdir, "thumb.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(90, 90, 110, 110))
        shape.finish(color=(0, 0, 0), fill=(0, 0, 0))
        shape.commit()
        doc.save(path)
        doc.close()

        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path=path)
        try:
            lbl = dlg._tbl.cellWidget(0, dlg._C_THUMB)
            self.assertIsInstance(lbl, QLabel)
            self.assertFalse(lbl.pixmap().isNull())
        finally:
            dlg.deleteLater()

    def test_thumbnail_crop_is_wider_than_before_for_a_point_only_row(self):
        """"jag granska och detektera får du gärna visa en lite mer
        utzoomad bild" / "På varje objekt" (2026-08-17, see NOTES.md) —
        a row with no outline (just an x/y point) used to crop at
        1.5x the page's own text scale (floor 15pt), tight enough to
        cut straight through a real LKAB-style oblong instrument bubble.
        Must now use a comfortably wider crop."""
        import fitz
        import symbol_geometry as sg
        import image_symbol_matching
        from pid_viewer import EquipmentMarkerReviewDialog
        path = os.path.join(self._tmpdir, "thumb_wide.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        page.insert_text((150, 150), "AAAAAAAAAA", fontsize=10)
        doc.save(path)
        doc.close()

        results = [{'tag': 'V-777', 'page': 0, 'comp_type': 'Ventil', 'x': 200.0, 'y': 200.0,
                    'confidence': 0.9, 'link_method': 'shape', 'outline': []}]
        dlg = EquipmentMarkerReviewDialog(results, self.db, pdf_path=path)
        try:
            live_doc = fitz.open(path)
            scale = sg.dominant_text_size(live_doc[0])
            live_doc.close()
            captured = {}
            real_render_gray = image_symbol_matching.render_gray

            def _spy(page_, bbox=None, dpi=300):
                captured['bbox'] = bbox
                return real_render_gray(page_, bbox=bbox, dpi=dpi)

            with unittest.mock.patch('pid_viewer.image_symbol_matching.render_gray', _spy):
                thumb_doc = fitz.open(path)
                dlg._render_thumbnail(thumb_doc, results[0], {})
                thumb_doc.close()
            x0, y0, x1, y1 = captured['bbox']
            self.assertGreaterEqual((x1 - x0) / 2, max(scale * 3.0, 28.0) - 0.01)
        finally:
            dlg.deleteLater()

    def test_no_thumbnail_without_pdf_path(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db)
        try:
            self.assertIsNone(dlg._tbl.cellWidget(0, dlg._C_THUMB))
        finally:
            dlg.deleteLater()

    def test_thumbnail_absent_but_no_crash_with_unresolvable_pdf_path(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            self.assertIsNone(dlg._tbl.cellWidget(0, dlg._C_THUMB))
        finally:
            dlg.deleteLater()

    def test_edit_shape_button_only_shown_for_rows_with_an_outline(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QPushButton
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            self.assertIsInstance(dlg._tbl.cellWidget(0, dlg._C_EDIT), QPushButton)
            self.assertIsNone(dlg._tbl.cellWidget(1, dlg._C_EDIT))
        finally:
            dlg.deleteLater()

    def test_edit_shape_updates_outline_and_recenters_x_y_on_accept(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            with unittest.mock.patch('pid_viewer.fitz.open'), \
                 unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                     return_value=(['prims'], ['idx'], {})), \
                 unittest.mock.patch('pid_viewer.symbol_geometry.primitives_near_point',
                                     return_value=[]), \
                 unittest.mock.patch('pid_viewer.MarkerShapeEditDialog') as mock_editor_cls:
                mock_editor_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
                mock_editor_cls.return_value.edited_outline.return_value = \
                    [[0, 0], [20, 0], [20, 10], [0, 10]]
                dlg._edit_shape(0)
            self.assertEqual(dlg._results[0]['outline'], [[0, 0], [20, 0], [20, 10], [0, 10]])
            self.assertEqual(dlg._results[0]['x'], 10.0)
            self.assertEqual(dlg._results[0]['y'], 5.0)
        finally:
            dlg.deleteLater()

    def test_edit_shape_leaves_outline_unchanged_when_editor_cancelled(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            original_outline = list(dlg._results[0]['outline'])
            with unittest.mock.patch('pid_viewer.fitz.open'), \
                 unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                     return_value=(['prims'], ['idx'], {})), \
                 unittest.mock.patch('pid_viewer.symbol_geometry.primitives_near_point',
                                     return_value=[]), \
                 unittest.mock.patch('pid_viewer.MarkerShapeEditDialog') as mock_editor_cls:
                mock_editor_cls.return_value.exec.return_value = QDialog.DialogCode.Rejected
                dlg._edit_shape(0)
            self.assertEqual(dlg._results[0]['outline'], original_outline)
        finally:
            dlg.deleteLater()

    def test_edit_shape_shows_info_when_no_cluster_resolved(self):
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QMessageBox
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            with unittest.mock.patch('pid_viewer.fitz.open'), \
                 unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                     return_value=None), \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                dlg._edit_shape(0)
            mock_info.assert_called_once()
        finally:
            dlg.deleteLater()

    def test_edited_shape_is_persisted_on_save(self):
        """The whole point: an edited outline must actually reach the
        database, not just live in the row's in-memory dict."""
        import json
        from pid_viewer import EquipmentMarkerReviewDialog
        from PyQt6.QtWidgets import QDialog
        dlg = EquipmentMarkerReviewDialog(self._sample_results(), self.db, pdf_path='fake.pdf')
        try:
            with unittest.mock.patch('pid_viewer.fitz.open'), \
                 unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                     return_value=(['prims'], ['idx'], {})), \
                 unittest.mock.patch('pid_viewer.symbol_geometry.primitives_near_point',
                                     return_value=[]), \
                 unittest.mock.patch('pid_viewer.MarkerShapeEditDialog') as mock_editor_cls:
                mock_editor_cls.return_value.exec.return_value = QDialog.DialogCode.Accepted
                mock_editor_cls.return_value.edited_outline.return_value = \
                    [[0, 0], [20, 0], [20, 10], [0, 10]]
                dlg._edit_shape(0)
            dlg._save()
            rows = [dict(r) for r in self.db.equipment_markers_for_page(0)]
            saved = next(r for r in rows if r['tag'] == 'V-101')
            self.assertEqual(json.loads(saved['shape_outline']), [[0, 0], [20, 0], [20, 10], [0, 10]])
        finally:
            dlg.deleteLater()


class ClusterPreviewCanvasTests(unittest.TestCase):
    """_ClusterPreviewCanvas (pid_viewer.py) — the segment-exclusion
    preview in SimilarSymbolSearchDialog (2026-08-14, see NOTES.md
    "Hitta liknande symbol" — sökparametrar). Directly implements
    "ta bort något som inte tillhör"."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _line(self, x0, y0, x1, y1, source=0, filled=False):
        return {'kind': 'l', 'bbox': (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                'p0': (x0, y0), 'p1': (x1, y1), 'closed': False, 'filled': filled,
                'width': 1.0, 'source': source}

    def test_edited_index_group_starts_as_the_full_group(self):
        from pid_viewer import _ClusterPreviewCanvas
        prims = [self._line(0, 0, 10, 0, source=0), self._line(10, 0, 10, 10, source=1)]
        canvas = _ClusterPreviewCanvas(prims, [0, 1])
        try:
            self.assertEqual(canvas.edited_index_group(), [0, 1])
            self.assertFalse(canvas.has_edits())
        finally:
            canvas.deleteLater()

    def _click_at(self, canvas, x, y):
        from PyQt6.QtCore import QPoint, QEvent, Qt as _Qt
        from PyQt6.QtGui import QMouseEvent
        pos = QPoint(int(x), int(y))
        ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                         _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                         _Qt.KeyboardModifier.NoModifier)
        canvas.mousePressEvent(ev)

    def test_clicking_a_segment_toggles_its_exclusion(self):
        from pid_viewer import _ClusterPreviewCanvas
        prims = [self._line(0, 0, 10, 0, source=0), self._line(0, 20, 10, 20, source=1)]
        canvas = _ClusterPreviewCanvas(prims, [0, 1])
        try:
            canvas.resize(240, 180)
            scale, ox, oy = canvas._transform()
            x0, y0 = prims[0]['p0']
            x1, y1 = prims[0]['p1']
            mx, my = (x0 + x1) / 2 * scale + ox, (y0 + y1) / 2 * scale + oy

            self._click_at(canvas, mx, my)
            self.assertEqual(canvas.edited_index_group(), [1])
            self.assertTrue(canvas.has_edits())

            self._click_at(canvas, mx, my)   # click again — re-include it
            self.assertEqual(canvas.edited_index_group(), [0, 1])
            self.assertFalse(canvas.has_edits())
        finally:
            canvas.deleteLater()

    def test_clicking_far_from_any_segment_changes_nothing(self):
        from pid_viewer import _ClusterPreviewCanvas
        prims = [self._line(0, 0, 10, 0)]
        canvas = _ClusterPreviewCanvas(prims, [0])
        try:
            canvas.resize(240, 180)
            self._click_at(canvas, 1, 1)   # far corner, well away from the segment
            self.assertEqual(canvas.edited_index_group(), [0])
        finally:
            canvas.deleteLater()

    def test_selection_changed_signal_emitted_on_click(self):
        from pid_viewer import _ClusterPreviewCanvas
        prims = [self._line(0, 0, 10, 0)]
        canvas = _ClusterPreviewCanvas(prims, [0])
        try:
            canvas.resize(240, 180)
            scale, ox, oy = canvas._transform()
            mx, my = 5 * scale + ox, 0 * scale + oy
            spy = unittest.mock.Mock()
            canvas.selection_changed.connect(spy)
            self._click_at(canvas, mx, my)
            spy.assert_called_once()
        finally:
            canvas.deleteLater()

    def test_edited_outline_is_bbox_of_surviving_primitives_only(self):
        """"städa bort en ledning från en ventil/pump" (2026-08-15, see
        NOTES.md) — excluding the "pipe" primitive must shrink the
        returned outline to just the remaining ("valve") primitives."""
        from pid_viewer import _ClusterPreviewCanvas
        valve = self._line(0, 0, 10, 10, source=0)     # bbox (0,0,10,10)
        pipe = self._line(10, 10, 100, 10, source=1)   # bbox (10,10,100,10) — stretches the outline far out
        canvas = _ClusterPreviewCanvas([valve, pipe], [0, 1])
        try:
            self.assertEqual(canvas.edited_outline(), [[0, 0], [100, 0], [100, 10], [0, 10]])
            canvas._excluded_sources.add(1)   # exclude the "pipe"
            self.assertEqual(canvas.edited_outline(), [[0, 0], [10, 0], [10, 10], [0, 10]])
        finally:
            canvas.deleteLater()

    def test_edited_outline_empty_when_everything_excluded(self):
        from pid_viewer import _ClusterPreviewCanvas
        canvas = _ClusterPreviewCanvas([self._line(0, 0, 10, 0)], [0])
        try:
            canvas._excluded_sources.add(0)
            self.assertEqual(canvas.edited_outline(), [])
        finally:
            canvas.deleteLater()

    def test_primitives_sharing_a_source_toggle_together(self):
        """The whole point of grouping by source (2026-08-15, see
        NOTES.md "Referens-canvasen: rendera fyllnad som svart + gruppera
        klick per ritad väg") — a tessellated shape's fragments all
        share one drawn path and must exclude/include as ONE unit, not
        one tiny fragment at a time."""
        from pid_viewer import _ClusterPreviewCanvas
        # Two fragments of the SAME source, physically apart within the
        # canvas — clicking either one must toggle BOTH.
        frag_a = self._line(0, 0, 2, 0, source=5)
        frag_b = self._line(0, 20, 2, 20, source=5)
        other = self._line(50, 50, 52, 50, source=9)
        canvas = _ClusterPreviewCanvas([frag_a, frag_b, other], [0, 1, 2])
        try:
            canvas.resize(240, 180)
            scale, ox, oy = canvas._transform()
            mx, my = 1 * scale + ox, 0 * scale + oy
            self._click_at(canvas, mx, my)
            self.assertEqual(canvas.edited_index_group(), [2],
                "clicking one fragment of source 5 must exclude BOTH its fragments")
        finally:
            canvas.deleteLater()

    def test_edited_index_group_stays_primitive_granular(self):
        """Even though exclusion is per-source, the public contract
        (edited_index_group -> similarity_features's ref_index_group)
        must still be primitive indices, not source ids."""
        from pid_viewer import _ClusterPreviewCanvas
        prims = [self._line(0, 0, 2, 0, source=5), self._line(0, 20, 2, 20, source=5)]
        canvas = _ClusterPreviewCanvas(prims, [0, 1])
        try:
            self.assertEqual(canvas.edited_index_group(), [0, 1])
            canvas._excluded_sources.add(5)
            self.assertEqual(canvas.edited_index_group(), [])
        finally:
            canvas.deleteLater()

    def test_filled_group_still_renders_as_stroked_outline_only(self):
        """2026-08-15 follow-up (see NOTES.md "Referens-canvasen"): a
        convex-hull SOLID fill for filled=True groups was tried and
        shipped, but Anton reported it wasn't visually convincing in
        practice ("Det blev inte jättelyckat med att fylla dem") and
        asked for it to be removed — the per-source CLICK grouping
        stays, only the fill rendering is gone. Sample actual rendered
        pixels (same technique RiskCellActualRenderColorTests already
        uses) to confirm a point INSIDE a filled=True triangle's
        interior stays the white background — only its edges are drawn."""
        from pid_viewer import _ClusterPreviewCanvas
        prims = [
            self._line(0, 0, 40, 0, source=1, filled=True),
            self._line(40, 0, 0, 40, source=1, filled=True),
            self._line(0, 40, 0, 0, source=1, filled=True),
        ]
        canvas = _ClusterPreviewCanvas(prims, [0, 1, 2])
        try:
            canvas.resize(200, 200)
            pix = canvas.grab()
            img = pix.toImage()
            scale, ox, oy = canvas._transform()
            # Well inside the triangle (near its centroid) — must NOT be filled.
            inside = img.pixelColor(int(12 * scale + ox), int(12 * scale + oy))
            self.assertGreater(inside.lightness(), 200,
                f"filled=True must no longer solid-fill — got {inside.name()}")
        finally:
            canvas.deleteLater()


class MarkerShapeEditDialogTests(unittest.TestCase):
    """MarkerShapeEditDialog (pid_viewer.py) — "finns det något bra
    sätt att städa bort ledningen från en ventil eller pump" (2026-08-15,
    see NOTES.md). Reuses _ClusterPreviewCanvas so ANY detected marker
    (not just a similarity-search reference) can be pruned before saving."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _line(self, x0, y0, x1, y1, source=0):
        return {'kind': 'l', 'bbox': (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                'p0': (x0, y0), 'p1': (x1, y1), 'closed': False, 'filled': False,
                'width': 1.0, 'source': source}

    def test_ok_returns_the_canvas_edited_outline(self):
        from pid_viewer import MarkerShapeEditDialog
        from PyQt6.QtWidgets import QDialog
        prims = [self._line(0, 0, 10, 0, source=0), self._line(10, 0, 50, 0, source=1)]
        dlg = MarkerShapeEditDialog(prims, [0, 1])
        try:
            dlg._canvas._excluded_sources.add(1)
            dlg.accept()
            self.assertEqual(dlg.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(dlg.edited_outline(), [[0, 0], [10, 0], [10, 0], [0, 0]])
        finally:
            dlg.deleteLater()

    def test_cancel_rejects_the_dialog(self):
        from pid_viewer import MarkerShapeEditDialog
        from PyQt6.QtWidgets import QDialog, QPushButton
        dlg = MarkerShapeEditDialog([self._line(0, 0, 10, 0)], [0])
        try:
            cancel_btn = [b for b in dlg.findChildren(QPushButton) if b.text() == 'Avbryt'][0]
            cancel_btn.click()
            self.assertEqual(dlg.result(), QDialog.DialogCode.Rejected)
        finally:
            dlg.deleteLater()


class ImageRefCropCanvasTests(unittest.TestCase):
    """_ImageRefCropCanvas (pid_viewer.py) — the rubber-band crop tool
    for the image-matching reference preview (2026-08-15, see NOTES.md
    "Bildbaserad 'hitta liknande symbol'" real-file verification
    follow-up). Directly answers the earlier finding that a reference
    bbox including an adjacent tag label scores far worse than a
    tightly-cropped symbol-only region — image mode previously had no
    way for the user to fix that themselves."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _gray(self, w=100, h=80):
        import numpy as np
        return np.full((h, w), 255, dtype=np.uint8)

    def _drag(self, canvas, x0, y0, x1, y1):
        from PyQt6.QtCore import QPoint, QEvent, Qt as _Qt
        from PyQt6.QtGui import QMouseEvent
        press = QMouseEvent(QEvent.Type.MouseButtonPress, QPoint(x0, y0).toPointF(),
                             _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                             _Qt.KeyboardModifier.NoModifier)
        canvas.mousePressEvent(press)
        move = QMouseEvent(QEvent.Type.MouseMove, QPoint(x1, y1).toPointF(),
                            _Qt.MouseButton.NoButton, _Qt.MouseButton.LeftButton,
                            _Qt.KeyboardModifier.NoModifier)
        canvas.mouseMoveEvent(move)
        release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPoint(x1, y1).toPointF(),
                               _Qt.MouseButton.LeftButton, _Qt.MouseButton.NoButton,
                               _Qt.KeyboardModifier.NoModifier)
        canvas.mouseReleaseEvent(release)

    def test_current_bbox_is_the_full_reference_before_any_crop(self):
        from pid_viewer import _ImageRefCropCanvas
        canvas = _ImageRefCropCanvas()
        try:
            canvas.set_reference(self._gray(), (10.0, 20.0, 30.0, 40.0))
            self.assertFalse(canvas.has_crop())
            self.assertEqual(canvas.current_bbox(), (10.0, 20.0, 30.0, 40.0))
        finally:
            canvas.deleteLater()

    def test_dragging_a_rectangle_crops_to_a_pdf_space_sub_bbox(self):
        from pid_viewer import _ImageRefCropCanvas
        canvas = _ImageRefCropCanvas()
        try:
            canvas.resize(240, 180)
            canvas.set_reference(self._gray(100, 80), (0.0, 0.0, 100.0, 80.0))
            self._drag(canvas, 20, 20, 100, 100)
            self.assertTrue(canvas.has_crop())
            x0, y0, x1, y1 = canvas.current_bbox()
            # The dragged rectangle must be a strict sub-region of the
            # full reference, not the whole thing again.
            self.assertGreater(x0, 0.0)
            self.assertGreater(y0, 0.0)
            self.assertLess(x1, 100.0)
            self.assertLess(y1, 80.0)
        finally:
            canvas.deleteLater()

    def test_normal_drag_on_a_tiny_zoomed_in_reference_still_creates_a_crop(self):
        """Found in the wild (2026-08-16, see NOTES.md "rutan i
        bildmatchning stämmer inte med det markerade" — Anton: "Det
        verkar inte som rutan som visas på bildmatchning stämmer
        överens med vad jag markerat."): a real resolved reference on
        the active project's own hazop_project_pid.pdf was only 12x24
        ARRAY pixels — rendered at ~9-10x zoom to fill a normal-sized
        dialog widget. At that zoom, the OLD min-drag-distance check
        (measured in ARRAY pixels, after the zoom) meant a completely
        normal ~30 on-screen-pixel drag converted to under 4 array
        pixels and was silently discarded as "just a stray click" — so
        the crop box shown never matched what was actually dragged.
        The check must be measured in WIDGET pixels (what the user
        actually sees/controls) instead."""
        from pid_viewer import _ImageRefCropCanvas
        import numpy as np
        canvas = _ImageRefCropCanvas()
        try:
            canvas.resize(240, 180)
            canvas.set_reference(np.full((12, 24), 255, dtype='uint8'), (0.0, 0.0, 24.0, 12.0))
            # A visually obvious, deliberate 130x30 on-screen drag — but
            # at this reference's own zoom level (~9.3x to fill the
            # widget), 30 widget px converts to ~3.2 ARRAY px, under the
            # old (wrong) array-space threshold of 4.
            self._drag(canvas, 20, 50, 150, 80)
            self.assertTrue(canvas.has_crop(),
                "a clear ~130x30 on-screen drag must not be discarded as a stray click, "
                "even when the reference is tiny and heavily zoomed to fill the widget")
        finally:
            canvas.deleteLater()

    def test_a_stray_click_does_not_create_a_crop(self):
        from pid_viewer import _ImageRefCropCanvas
        canvas = _ImageRefCropCanvas()
        try:
            canvas.resize(240, 180)
            canvas.set_reference(self._gray(), (0.0, 0.0, 100.0, 80.0))
            self._drag(canvas, 50, 50, 51, 51)   # a sub-pixel-scale jitter, not a real drag
            self.assertFalse(canvas.has_crop())
        finally:
            canvas.deleteLater()

    def test_reset_crop_restores_the_full_reference_and_emits_once(self):
        from pid_viewer import _ImageRefCropCanvas
        canvas = _ImageRefCropCanvas()
        try:
            canvas.resize(240, 180)
            canvas.set_reference(self._gray(), (0.0, 0.0, 100.0, 80.0))
            self._drag(canvas, 20, 20, 100, 100)
            spy = unittest.mock.Mock()
            canvas.crop_changed.connect(spy)
            canvas.reset_crop()
            self.assertFalse(canvas.has_crop())
            self.assertEqual(canvas.current_bbox(), (0.0, 0.0, 100.0, 80.0))
            spy.assert_called_once()
            canvas.reset_crop()   # already reset — must not emit again
            spy.assert_called_once()
        finally:
            canvas.deleteLater()

    def test_set_reference_clears_any_previous_crop(self):
        from pid_viewer import _ImageRefCropCanvas
        canvas = _ImageRefCropCanvas()
        try:
            canvas.resize(240, 180)
            canvas.set_reference(self._gray(), (0.0, 0.0, 100.0, 80.0))
            self._drag(canvas, 20, 20, 100, 100)
            self.assertTrue(canvas.has_crop())
            canvas.set_reference(self._gray(), (5.0, 5.0, 105.0, 85.0))
            self.assertFalse(canvas.has_crop())
            self.assertEqual(canvas.current_bbox(), (5.0, 5.0, 105.0, 85.0))
        finally:
            canvas.deleteLater()


class _SyncFakeSimilarSymbolSearchWorker(QThread):
    """Test double for SimilarSymbolSearchWorker (2026-08-15, see
    NOTES.md "Hitta liknande symbol" — uppföljningsfunktioner) — a real
    QThread subclass (so SimilarSymbolSearchDialog's real
    .progress.connect()/.finished_scan.connect() wiring works exactly
    as in production) but start() runs synchronously on the calling
    thread and emits a pre-set candidate list immediately, instead of
    opening a real fitz.Document in a real background thread. Set
    `next_candidates` (a class attribute) before constructing the
    dialog to control what the "scan" finds."""
    progress      = pyqtSignal(int, int, str)
    finished_scan = pyqtSignal(list)

    next_candidates = []   # overridden per-test

    def __init__(self, pdf_path, ref_features, ref_page, ref_native_index_group,
                 pages=None, ignore_scale=False, rotation_mode='none',
                 page_rotations=None, parent=None):
        super().__init__()
        self.start_count = 0
        self._ref_features = ref_features
        self._ref_page = ref_page
        self._ref_native_index_group = ref_native_index_group
        self._pages = pages
        self._ignore_scale = ignore_scale
        self._rotation_mode = rotation_mode
        self._page_rotations = page_rotations
        type(self).instances.append(self)

    instances = []   # every instance constructed during a test, for call-count assertions

    def start(self):
        self.start_count += 1
        self.finished_scan.emit(list(type(self).next_candidates))

    def isRunning(self):
        return False

    def requestInterruption(self):
        pass

    def wait(self, *a):
        pass


class _SyncFakeImageSymbolSearchWorker(QThread):
    """Test double for ImageSymbolSearchWorker (2026-08-15, see NOTES.md
    "Bildbaserad 'hitta liknande symbol' — vid sidan av vektorlogiken")
    — same synchronous-start convention as
    _SyncFakeSimilarSymbolSearchWorker above, just matching
    ImageSymbolSearchWorker's own constructor signature."""
    progress      = pyqtSignal(int, int, str)
    finished_scan = pyqtSignal(list)

    next_candidates = []
    instances = []

    def __init__(self, pdf_path, ref_page, ref_bbox, pages=None,
                 ignore_scale=False, rotation_mode='none',
                 page_rotations=None, parent=None):
        super().__init__()
        self.start_count = 0
        self._ref_page = ref_page
        self._ref_bbox = ref_bbox
        self._pages = pages
        self._ignore_scale = ignore_scale
        self._rotation_mode = rotation_mode
        self._page_rotations = page_rotations
        type(self).instances.append(self)

    def start(self):
        self.start_count += 1
        self.finished_scan.emit(list(type(self).next_candidates))

    def isRunning(self):
        return False

    def requestInterruption(self):
        pass

    def wait(self, *a):
        pass


class SimilarSymbolSearchDialogTests(unittest.TestCase):
    """SimilarSymbolSearchDialog (pid_viewer.py) — search-parameter
    controls for "Hitta liknande symbol" (2026-08-14/15, see NOTES.md
    "Hitta liknande symbol" — sökparametrar / uppföljningsfunktioner).
    The document scan itself (SimilarSymbolSearchWorker) is replaced
    with _SyncFakeSimilarSymbolSearchWorker so these tests exercise the
    dialog's real wiring (progress/finished_scan signals, live count,
    restart-on-setting-change) without a real background thread or PDF."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        _SyncFakeSimilarSymbolSearchWorker.next_candidates = []
        _SyncFakeSimilarSymbolSearchWorker.instances = []
        self._worker_patcher = unittest.mock.patch(
            'pid_viewer.SimilarSymbolSearchWorker', _SyncFakeSimilarSymbolSearchWorker)
        self._worker_patcher.start()
        _SyncFakeImageSymbolSearchWorker.next_candidates = []
        _SyncFakeImageSymbolSearchWorker.instances = []
        self._image_worker_patcher = unittest.mock.patch(
            'pid_viewer.ImageSymbolSearchWorker', _SyncFakeImageSymbolSearchWorker)
        self._image_worker_patcher.start()
        self.db = None   # set by the "Spara som mall" tests only

    def tearDown(self):
        self._worker_patcher.stop()
        self._image_worker_patcher.stop()
        if self.db is not None:
            try:
                del self.db
            except Exception:
                pass

    def _line(self, x0, y0, x1, y1, source=0):
        return {'kind': 'l', 'bbox': (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
                'p0': (x0, y0), 'p1': (x1, y1), 'closed': False, 'filled': False,
                'width': 1.0, 'source': source}

    def _dialog(self, viewer=None, db=None):
        from pid_viewer import SimilarSymbolSearchDialog
        prims = [self._line(0, 0, 10, 0, source=0), self._line(10, 0, 10, 10, source=1)]
        return SimilarSymbolSearchDialog(prims, [0, 1], 'fake.pdf', 0, 10.0,
                                          ref_bbox=(0, 0, 10, 10), db=db, viewer=viewer)

    def _forced_image_dialog(self, viewer=None, db=None):
        """No vector cluster resolved at all — primitives/index_group
        both None, matching _find_similar_symbol's own fallback."""
        from pid_viewer import SimilarSymbolSearchDialog
        return SimilarSymbolSearchDialog(None, None, 'fake.pdf', 0, 10.0,
                                          ref_bbox=(0, 0, 10, 10), db=db, viewer=viewer)

    def _template_dialog(self, template_features=None, db=None, viewer=None, initial_comp_type=''):
        from pid_viewer import SimilarSymbolSearchDialog
        return SimilarSymbolSearchDialog(
            None, None, 'fake.pdf', 0, None, db=db, viewer=viewer,
            template_name="Test-mall",
            template_features=template_features or {'aspect': 1.0, 'norm_size': 2.0,
                                                    'fold_ratio': 1.0, 'has_curve': False,
                                                    'has_diagonal': True,
                                                    'has_closed_or_filled': True},
            initial_comp_type=initial_comp_type)

    def test_default_values_match_find_similar_shapes_defaults(self):
        dlg = self._dialog()
        try:
            self.assertAlmostEqual(dlg.min_similarity(), 0.6)
            self.assertFalse(dlg.ignore_scale())
            self.assertEqual(dlg.rotation_mode(), 'none')
            self.assertFalse(dlg.search_this_page_only())
            self.assertEqual(dlg.edited_index_group(), [0, 1])
        finally:
            dlg.deleteLater()

    def test_type_selector_defaults_to_empty_and_is_settable(self):
        """"kunna välja vilken typ av objekt det är i både raster och
        vektor" (2026-08-16, see NOTES.md) — an ad-hoc (non-template)
        search starts with no type chosen, but the user can set one
        before running "Sök", and it must be reflected by
        selected_comp_type() regardless of matching method."""
        dlg = self._dialog()
        try:
            self.assertEqual(dlg.selected_comp_type(), '')
            dlg._type_cb.setCurrentText('Ventil')
            self.assertEqual(dlg.selected_comp_type(), 'Ventil')
            dlg._method_image.setChecked(True)
            self.assertEqual(dlg.selected_comp_type(), 'Ventil',
                "the chosen type must survive switching to Bildmatchning")
        finally:
            dlg.deleteLater()

    def test_type_selector_offers_every_known_component_type(self):
        import equipment_detection
        dlg = self._dialog()
        try:
            items = {dlg._type_cb.itemText(i) for i in range(dlg._type_cb.count())}
            self.assertEqual(items, set(equipment_detection.COMPONENT_TYPES.keys()))
        finally:
            dlg.deleteLater()

    def test_template_mode_prefills_type_selector_from_template(self):
        dlg = self._template_dialog(initial_comp_type='Pump')
        try:
            self.assertEqual(dlg.selected_comp_type(), 'Pump')
        finally:
            dlg.deleteLater()

    def test_scan_runs_automatically_on_open(self):
        """The scan starts as soon as the dialog is constructed —
        no separate "start search" step needed before the live
        count/preview are useful."""
        dlg = self._dialog()
        try:
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1)
            self.assertEqual(_SyncFakeSimilarSymbolSearchWorker.instances[0].start_count, 1)
        finally:
            dlg.deleteLater()

    def test_search_button_disabled_until_scan_finishes(self):
        """Real behaviour: the fake worker emits finished_scan
        synchronously from start(), so by the time __init__ returns the
        button is already re-enabled — this asserts THAT happened via
        _on_scan_finished, not that it starts disabled and stays so."""
        dlg = self._dialog()
        try:
            self.assertTrue(dlg._search_btn.isEnabled())
        finally:
            dlg.deleteLater()

    def test_threshold_slider_maps_percent_to_0_1_range(self):
        dlg = self._dialog()
        try:
            dlg._threshold.setValue(85)
            self.assertAlmostEqual(dlg.min_similarity(), 0.85)
        finally:
            dlg.deleteLater()

    def test_threshold_change_updates_live_count_without_a_new_scan(self):
        _SyncFakeSimilarSymbolSearchWorker.next_candidates = [
            (0.9, 0, 1.0, 1.0, []), (0.7, 0, 2.0, 2.0, []), (0.3, 0, 3.0, 3.0, []),
        ]
        dlg = self._dialog()
        try:
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1)
            dlg._threshold.setValue(60)
            self.assertEqual(dlg._count_lbl.text(), "≈ 2 träffar")
            dlg._threshold.setValue(80)
            self.assertEqual(dlg._count_lbl.text(), "≈ 1 träffar")
            # No new worker was constructed just from moving the slider.
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1)
        finally:
            dlg.deleteLater()

    def test_choosing_alla_storlekar_sets_ignore_scale_and_restarts_scan(self):
        dlg = self._dialog()
        try:
            dlg._scale_any.setChecked(True)
            self.assertTrue(dlg.ignore_scale())
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 2,
                "changing Skala affects candidate scores — must trigger a fresh scan")
        finally:
            dlg.deleteLater()

    def test_choosing_alla_vinklar_sets_rotation_mode_any_and_restarts_scan(self):
        dlg = self._dialog()
        try:
            dlg._rotation_any.setChecked(True)
            self.assertEqual(dlg.rotation_mode(), 'any')
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 2)
        finally:
            dlg.deleteLater()

    def test_choosing_denna_sida_sets_search_this_page_only_and_restarts_scan(self):
        dlg = self._dialog()
        try:
            dlg._scope_page.setChecked(True)
            self.assertTrue(dlg.search_this_page_only())
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 2)
        finally:
            dlg.deleteLater()

    def test_excluding_a_segment_in_the_canvas_restarts_the_scan(self):
        dlg = self._dialog()
        try:
            dlg._canvas._excluded_sources.add(1)
            dlg._canvas.selection_changed.emit()
            self.assertEqual(dlg.edited_index_group(), [0])
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 2,
                "editing the reference shape changes ref_features — must re-scan")
        finally:
            dlg.deleteLater()

    def test_excluding_every_segment_shows_a_message_instead_of_crashing(self):
        """Real crash found in the wild (2026-08-15, see NOTES.md):
        excluding EVERY primitive left an empty index_group, which
        crashed all the way down in symbol_geometry.cluster_features()
        (min() on an empty list of members) the instant the scan tried
        to restart."""
        dlg = self._dialog()
        try:
            dlg._canvas._excluded_sources.update({0, 1})
            dlg._canvas.selection_changed.emit()   # must not raise
            self.assertEqual(dlg.edited_index_group(), [])
            self.assertIn("Inget kvar", dlg._status_lbl.text())
            self.assertFalse(dlg._search_btn.isEnabled())
            self.assertFalse(dlg._progress_bar.isVisible())
        finally:
            dlg.deleteLater()

    def test_search_button_accepts_the_dialog(self):
        from PyQt6.QtWidgets import QDialog
        dlg = self._dialog()
        try:
            search_btn = [b for b in dlg.findChildren(QPushButton) if b.text() == 'Sök'][0]
            search_btn.click()
            self.assertEqual(dlg.result(), QDialog.DialogCode.Accepted)
        finally:
            dlg.deleteLater()

    def test_cancel_button_rejects_the_dialog(self):
        from PyQt6.QtWidgets import QDialog
        dlg = self._dialog()
        try:
            cancel_btn = [b for b in dlg.findChildren(QPushButton) if b.text() == 'Avbryt'][0]
            cancel_btn.click()
            self.assertEqual(dlg.result(), QDialog.DialogCode.Rejected)
        finally:
            dlg.deleteLater()

    def test_final_results_reuses_cached_candidates_shaped_and_thresholded(self):
        _SyncFakeSimilarSymbolSearchWorker.next_candidates = [
            (0.9, 0, 1.0, 1.0, [[0, 0], [1, 0], [1, 1]]),
            (0.3, 0, 2.0, 2.0, [[0, 0], [1, 0], [1, 1]]),
        ]
        dlg = self._dialog()
        try:
            results = dlg.final_results(comp_type='Ventil')
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]['comp_type'], 'Ventil')
            self.assertAlmostEqual(results[0]['detection_confidence'], 0.9)
        finally:
            dlg.deleteLater()

    def test_preview_checkbox_disabled_without_a_viewer(self):
        dlg = self._dialog(viewer=None)
        try:
            self.assertFalse(dlg._preview_cb.isEnabled())
        finally:
            dlg.deleteLater()

    def test_preview_checkbox_draws_only_current_page_candidates_above_threshold(self):
        _SyncFakeSimilarSymbolSearchWorker.next_candidates = [
            (0.9, 0, 1.0, 1.0, [[0, 0], [1, 0], [1, 1]]),   # right page, above threshold
            (0.9, 1, 5.0, 5.0, [[0, 0], [1, 0], [1, 1]]),   # wrong page
            (0.3, 0, 2.0, 2.0, [[0, 0], [1, 0], [1, 1]]),   # right page, below threshold
        ]
        fake_viewer = unittest.mock.Mock()
        fake_viewer.current_page = 0
        dlg = self._dialog(viewer=fake_viewer)
        try:
            dlg._preview_cb.setChecked(True)
            fake_viewer.add_shape_highlight.assert_called_once_with([[0, 0], [1, 0], [1, 1]])
        finally:
            dlg.deleteLater()

    def test_dialog_finished_clears_preview_and_stops_any_running_worker(self):
        fake_viewer = unittest.mock.Mock()
        fake_viewer.current_page = 0
        dlg = self._dialog(viewer=fake_viewer)
        try:
            dlg.reject()
            fake_viewer.clear_shape_preview.assert_called()
        finally:
            dlg.deleteLater()

    def test_save_as_template_button_disabled_without_db(self):
        dlg = self._dialog(db=None)
        try:
            self.assertFalse(dlg._save_template_btn.isEnabled())
        finally:
            dlg.deleteLater()

    def test_save_as_template_persists_current_reference_features(self):
        import json
        self.db = Database(path=os.path.join(
            tempfile.mkdtemp(prefix="hazop_savetemplate_test_"), "test_project.db"))
        dlg = self._dialog(db=self.db)
        try:
            self.assertTrue(dlg._save_template_btn.isEnabled())
            with unittest.mock.patch('pid_viewer.QInputDialog.getText',
                                     return_value=("Min ventil", True)), \
                 unittest.mock.patch('pid_viewer.QMessageBox.information'):
                dlg._save_as_template()
            rows = self.db.symbol_templates()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['name'], "Min ventil")
            feats = json.loads(rows[0]['features_json'])
            self.assertIn('aspect', feats)
        finally:
            dlg.deleteLater()

    def test_save_as_template_warns_on_duplicate_name(self):
        self.db = Database(path=os.path.join(
            tempfile.mkdtemp(prefix="hazop_savetemplate_test_"), "test_project.db"))
        self.db.add_symbol_template("Upptaget namn", '{}')
        dlg = self._dialog(db=self.db)
        try:
            with unittest.mock.patch('pid_viewer.QInputDialog.getText',
                                     return_value=("Upptaget namn", True)), \
                 unittest.mock.patch('pid_viewer.QMessageBox.warning') as mock_warn:
                dlg._save_as_template()
            mock_warn.assert_called_once()
            self.assertEqual(len(self.db.symbol_templates()), 1)
        finally:
            dlg.deleteLater()

    def test_save_as_template_shows_message_instead_of_crashing_when_all_segments_excluded(self):
        """Same real crash class as test_excluding_every_segment_shows_a_
        message_instead_of_crashing (2026-08-15, see NOTES.md), but hit via
        a second, separately unguarded call site: "💾 Spara som mall…" also
        calls similarity_features()/cluster_features() with the edited
        (possibly emptied) index_group, and crashed the same way
        (min() on an empty list) if a user excluded every segment before
        clicking Save (crash_20260815_175028_ValueError.json)."""
        self.db = Database(path=os.path.join(
            tempfile.mkdtemp(prefix="hazop_savetemplate_test_"), "test_project.db"))
        dlg = self._dialog(db=self.db)
        try:
            dlg._canvas._excluded_sources.update({0, 1})
            dlg._canvas.selection_changed.emit()
            self.assertEqual(dlg.edited_index_group(), [])
            with unittest.mock.patch('pid_viewer.QInputDialog.getText') as mock_input, \
                 unittest.mock.patch('pid_viewer.QMessageBox.warning') as mock_warn:
                dlg._save_as_template()   # must not raise
            mock_input.assert_not_called()
            mock_warn.assert_called_once()
            self.assertEqual(len(self.db.symbol_templates()), 0)
        finally:
            dlg.deleteLater()

    def test_method_toggle_visible_when_a_vector_cluster_was_resolved(self):
        dlg = self._dialog()
        try:
            self.assertIsNotNone(dlg._method_image)
            self.assertFalse(dlg.use_image_matching())
        finally:
            dlg.deleteLater()

    def test_method_toggle_absent_in_template_mode(self):
        dlg = self._template_dialog()
        try:
            self.assertIsNone(dlg._method_image)
            self.assertFalse(dlg.use_image_matching())
        finally:
            dlg.deleteLater()

    def test_forced_image_mode_when_no_vector_cluster_resolved(self):
        """primitives=None/index_group=None (no vector cluster at all,
        see _find_similar_symbol's fallback) — image matching is the
        only option, no toggle shown."""
        dlg = self._forced_image_dialog()
        try:
            self.assertIsNone(dlg._method_image)
            self.assertIsNone(dlg._canvas)
            self.assertTrue(dlg.use_image_matching())
            self.assertEqual(_SyncFakeImageSymbolSearchWorker.instances[-1].start_count, 1)
            self.assertEqual(_SyncFakeSimilarSymbolSearchWorker.instances, [],
                "forced image mode must never construct the vector worker")
        finally:
            dlg.deleteLater()

    def test_switching_to_bildmatchning_restarts_scan_with_image_worker(self):
        dlg = self._dialog()
        try:
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1)
            self.assertEqual(len(_SyncFakeImageSymbolSearchWorker.instances), 0)
            dlg._method_image.setChecked(True)
            self.assertTrue(dlg.use_image_matching())
            self.assertEqual(len(_SyncFakeImageSymbolSearchWorker.instances), 1,
                "toggling to Bildmatchning must (re-)run the scan via the image worker")
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1,
                "no extra vector scan should be triggered by the method toggle")
        finally:
            dlg.deleteLater()

    def test_tiny_reference_bbox_shows_warning(self):
        """"Bildmatchning visar ingen symbol" (2026-08-16, see NOTES.md
        "Bildmatchning visar ingen symbol och kraschar lätt") —
        resolve_reference_cluster() is deliberately permissive and can
        resolve a click to a degenerate sliver of a cluster (confirmed
        on the active project's own hazop_project_pid.pdf: a real click
        resolved to a 1.3x1.3pt cluster). _update_tiny_ref_warning() must
        flag that instead of silently showing a near-blank preview."""
        from pid_viewer import SimilarSymbolSearchDialog
        prims = [self._line(0, 0, 1, 0, source=0)]
        dlg = SimilarSymbolSearchDialog(prims, [0], 'fake.pdf', 0, 10.0,
                                         ref_bbox=(0, 0, 1, 1), viewer=None)
        try:
            dlg._update_tiny_ref_warning()
            self.assertFalse(dlg._tiny_ref_warning.isHidden())
        finally:
            dlg.deleteLater()

    def test_normal_reference_bbox_hides_warning(self):
        dlg = self._dialog()
        try:
            dlg._ref_bbox = (0, 0, 40, 40)   # diag ~56.6, comfortably above the 1.5*scale=15pt floor
            dlg._update_tiny_ref_warning()
            self.assertTrue(dlg._tiny_ref_warning.isHidden())
        finally:
            dlg.deleteLater()

    def test_reference_preview_renders_at_a_higher_dpi_than_matching_uses(self):
        """"Det ser väldigt B ut när det visas en så lågupplöst version
        av ventilen." (2026-08-16, see NOTES.md "Rutan i bildmatchning
        stämmer inte med det markerade" follow-up) — a real resolved
        reference can be just a handful of pixels across at the matching
        DPI (300), reading as an illegible, blocky thumbnail once
        _ImageRefCropCanvas zooms it up to fill the widget. The PREVIEW
        must render at pid_viewer._PREVIEW_DPI (4x the matching DPI),
        independent of image_symbol_matching._DEFAULT_DPI, which stays
        at 300 for MATCHING accuracy (see NOTES.md "Högre DPI för
        bildmatchning — testat, ingen förbättring" — higher DPI
        measurably makes matching worse, not better)."""
        import fitz
        from pid_viewer import SimilarSymbolSearchDialog, _PREVIEW_DPI
        import image_symbol_matching
        self.assertGreater(_PREVIEW_DPI, image_symbol_matching._DEFAULT_DPI,
            "the preview DPI must be higher than the matching DPI, not equal to it")
        tmpdir = tempfile.mkdtemp(prefix="hazop_previewdpi_test_")
        try:
            path = os.path.join(tmpdir, "previewdpi.pdf")
            doc = fitz.open()
            doc.new_page(width=200, height=200)
            doc.save(path)
            doc.close()

            ref_bbox = (10.0, 10.0, 20.0, 15.0)   # 10x5pt — tiny, like a real degenerate reference
            dlg = SimilarSymbolSearchDialog(None, None, path, 0, 10.0,
                                             ref_bbox=ref_bbox, viewer=None)
            try:
                dlg._render_image_preview()
                h, w = dlg._image_ref_canvas._gray.shape
                expected_h = round(5.0 * _PREVIEW_DPI / 72.0)
                expected_w = round(10.0 * _PREVIEW_DPI / 72.0)
                self.assertAlmostEqual(h, expected_h, delta=1)
                self.assertAlmostEqual(w, expected_w, delta=1)
            finally:
                dlg.deleteLater()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_dragging_a_crop_in_image_mode_narrows_what_gets_searched(self):
        """Integration check that _restart_scan actually reads
        _image_ref_canvas.current_bbox() rather than the dialog's raw
        _ref_bbox — the whole point of the crop tool."""
        from PyQt6.QtCore import QPoint, QEvent, Qt as _Qt
        from PyQt6.QtGui import QMouseEvent
        import numpy as np

        def drag(canvas, x0, y0, x1, y1):
            canvas.resize(240, 180)
            press = QMouseEvent(QEvent.Type.MouseButtonPress, QPoint(x0, y0).toPointF(),
                                 _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                                 _Qt.KeyboardModifier.NoModifier)
            canvas.mousePressEvent(press)
            move = QMouseEvent(QEvent.Type.MouseMove, QPoint(x1, y1).toPointF(),
                                _Qt.MouseButton.NoButton, _Qt.MouseButton.LeftButton,
                                _Qt.KeyboardModifier.NoModifier)
            canvas.mouseMoveEvent(move)
            release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPoint(x1, y1).toPointF(),
                                   _Qt.MouseButton.LeftButton, _Qt.MouseButton.NoButton,
                                   _Qt.KeyboardModifier.NoModifier)
            canvas.mouseReleaseEvent(release)

        dlg = self._dialog()
        try:
            dlg._method_image.setChecked(True)
            canvas = dlg._image_ref_canvas
            canvas.set_reference(np.full((80, 100), 255, dtype='uint8'), (0, 0, 10, 10))
            n_before = len(_SyncFakeImageSymbolSearchWorker.instances)
            drag(canvas, 20, 20, 100, 100)   # crop_changed -> _restart_scan
            self.assertEqual(len(_SyncFakeImageSymbolSearchWorker.instances), n_before + 1,
                "dragging a crop must re-run the scan")
            searched_bbox = _SyncFakeImageSymbolSearchWorker.instances[-1]._ref_bbox
            self.assertNotEqual(searched_bbox, (0, 0, 10, 10),
                "the worker must search the CROPPED bbox, not the original full one")
        finally:
            dlg.deleteLater()

    def test_switching_back_to_vector_restarts_scan_with_vector_worker(self):
        dlg = self._dialog()
        try:
            dlg._method_image.setChecked(True)
            dlg._method_vector.setChecked(True)
            self.assertFalse(dlg.use_image_matching())
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 2)
        finally:
            dlg.deleteLater()

    def test_canvas_and_image_preview_visibility_follow_the_method_toggle(self):
        dlg = self._dialog()
        try:
            self.assertFalse(dlg._canvas.isHidden())
            self.assertTrue(dlg._image_ref_container.isHidden())
            dlg._method_image.setChecked(True)
            self.assertTrue(dlg._canvas.isHidden())
            self.assertFalse(dlg._image_ref_container.isHidden())
        finally:
            dlg.deleteLater()

    def test_save_template_button_disabled_in_image_mode(self):
        self.db = Database(path=os.path.join(
            tempfile.mkdtemp(prefix="hazop_savetemplate_test_"), "test_project.db"))
        dlg = self._dialog(db=self.db)
        try:
            self.assertTrue(dlg._save_template_btn.isEnabled())
            dlg._method_image.setChecked(True)
            self.assertFalse(dlg._save_template_btn.isEnabled())
        finally:
            dlg.deleteLater()

    def test_template_mode_has_no_canvas_and_disables_rotation_toggle(self):
        """A saved template has no live primitives to preview/edit, and
        its features were already computed in one fixed rotation basis
        when saved — "alla vinklar" has nothing to recompute against."""
        dlg = self._template_dialog()
        try:
            self.assertIsNone(dlg._canvas)
            self.assertIsNone(dlg.edited_index_group())
            self.assertFalse(dlg._rotation_any.isEnabled())
            self.assertFalse(dlg._save_template_btn.isEnabled(),
                "no new reference to save — this already IS a saved template")
        finally:
            dlg.deleteLater()

    def test_template_mode_scan_uses_the_given_features_directly(self):
        template_feats = {'aspect': 3.3, 'norm_size': 9.9, 'fold_ratio': 1.0,
                          'has_curve': False, 'has_diagonal': True,
                          'has_closed_or_filled': True}
        dlg = self._template_dialog(template_features=template_feats)
        try:
            self.assertEqual(len(_SyncFakeSimilarSymbolSearchWorker.instances), 1)
            worker = _SyncFakeSimilarSymbolSearchWorker.instances[0]
            self.assertEqual(worker._ref_features, template_feats)
        finally:
            dlg.deleteLater()


class SymbolTemplatePickerDialogTests(unittest.TestCase):
    """SymbolTemplatePickerDialog (pid_viewer.py) — "🔎 Hitta liknande
    symbol (från mall)" (2026-08-15, see NOTES.md "Hitta liknande
    symbol" — uppföljningsfunktioner: symbolbibliotek)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_templatepicker_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_lists_saved_templates(self):
        from pid_viewer import SymbolTemplatePickerDialog
        self.db.add_symbol_template("Metso-ventil", '{}', comp_type='Ventil')
        self.db.add_symbol_template("Endress+Hauser", '{}')
        dlg = SymbolTemplatePickerDialog(self.db)
        try:
            self.assertEqual(dlg._list.count(), 2)
        finally:
            dlg.deleteLater()

    def test_selecting_a_template_and_accepting_sets_selected_template(self):
        from pid_viewer import SymbolTemplatePickerDialog
        tid = self.db.add_symbol_template("Metso-ventil", '{"aspect": 2.0}')
        dlg = SymbolTemplatePickerDialog(self.db)
        try:
            dlg._list.setCurrentRow(0)
            dlg._accept_selected()
            self.assertEqual(dlg.result(), 1)   # QDialog.Accepted
            self.assertEqual(dlg.selected_template['id'], tid)
        finally:
            dlg.deleteLater()

    def test_accepting_with_no_selection_shows_info_and_does_not_accept(self):
        from pid_viewer import SymbolTemplatePickerDialog
        self.db.add_symbol_template("Metso-ventil", '{}')
        dlg = SymbolTemplatePickerDialog(self.db)
        try:
            with unittest.mock.patch('pid_viewer.QMessageBox.information') as mock_info:
                dlg._accept_selected()
            mock_info.assert_called_once()
            self.assertIsNone(dlg.selected_template)
        finally:
            dlg.deleteLater()

    def test_delete_removes_the_template_and_refreshes_the_list(self):
        from pid_viewer import SymbolTemplatePickerDialog
        self.db.add_symbol_template("Att ta bort", '{}')
        dlg = SymbolTemplatePickerDialog(self.db)
        try:
            dlg._list.setCurrentRow(0)
            dlg._delete_selected()
            self.assertEqual(dlg._list.count(), 0)
            self.assertEqual(self.db.symbol_templates(), [])
        finally:
            dlg.deleteLater()


class SimilarSymbolSearchWorkerTests(unittest.TestCase):
    """SimilarSymbolSearchWorker (pid_viewer.py) — the REAL QThread
    behind SimilarSymbolSearchDialog's background scan (2026-08-15, see
    NOTES.md "Hitta liknande symbol" — uppföljningsfunktioner), tested
    directly (not via the dialog's fake test double). Modelled on
    EquipmentAnalysisWorkerTests: must always emit finished_scan, even
    when fitz.open() itself raises, and must actually find a real
    candidate end-to-end against a real synthetic PDF."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_simsearchworker_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_finished_scan_emitted_even_when_pdf_open_fails(self):
        from pid_viewer import SimilarSymbolSearchWorker
        received = {}

        def _capture(candidates):
            received['candidates'] = candidates

        worker = SimilarSymbolSearchWorker(
            '/nonexistent/path/does-not-exist.pdf', {}, 0, [])
        worker.finished_scan.connect(_capture)
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()   # pump the queued cross-thread signal delivery
        self.assertIn('candidates', received,
            "finished_scan must fire even when fitz.open() raises")
        self.assertEqual(received['candidates'], [])

    def test_finds_a_real_candidate_end_to_end(self):
        import fitz
        import symbol_geometry as sg
        from equipment_detection import resolve_reference_cluster
        from pid_viewer import SimilarSymbolSearchWorker

        path = os.path.join(self._tmpdir, "worker.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()

        def bowtie(cx, cy):
            shape.draw_polyline([fitz.Point(cx - 10, cy - 10), fitz.Point(cx - 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(cx + 10, cy - 10), fitz.Point(cx + 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

        bowtie(60, 60)
        bowtie(300, 300)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            primitives, index_group, cluster = resolve_reference_cluster(doc, 0, 60, 60)
            ref_features = sg.similarity_features(primitives, index_group)
        finally:
            doc.close()

        received = {}
        worker = SimilarSymbolSearchWorker(
            path, ref_features, 0, cluster['_index_group'], pages=[0])
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()
        candidates = received.get('candidates')
        self.assertTrue(candidates)
        self.assertGreater(max(c[0] for c in candidates), 0.9)

    def test_page_rotations_places_candidates_in_the_live_views_coordinate_space(self):
        """The actual reported bug ("hittar massa liknande med vektor men
        placerar ut dem felaktigt"): without page_rotations, a candidate
        found on a page with a manual rotation override is reported in
        the page's NATIVE coordinate space while the live view expects
        the OVERRIDDEN one — same physical symbol, wrong reported
        position. Confirmed on the real active project (page 0 has a
        90-degree override in pid_page_rotation)."""
        import fitz
        import symbol_geometry as sg
        from equipment_detection import resolve_reference_cluster
        from pid_viewer import SimilarSymbolSearchWorker

        path = os.path.join(self._tmpdir, "rotworker.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()

        def bowtie(cx, cy):
            shape.draw_polyline([fitz.Point(cx - 10, cy - 10), fitz.Point(cx - 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(cx + 10, cy - 10), fitz.Point(cx + 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

        bowtie(60, 60)
        bowtie(300, 300)
        shape.commit()
        doc.save(path)
        doc.close()

        # LIVE view: the manual override already applied, as PIDGraphicsView
        # would — the reference is resolved using ROTATED-space coordinates.
        live_doc = fitz.open(path)
        live_doc[0].set_rotation(90)
        primitives, index_group, cluster = resolve_reference_cluster(live_doc, 0, 340, 60)
        self.assertIsNotNone(cluster, "sanity check: reference must resolve in rotated space")
        ref_features = sg.similarity_features(primitives, index_group)
        native_index_group = cluster['_index_group']
        live_doc.close()

        received = {}
        worker = SimilarSymbolSearchWorker(
            path, ref_features, 0, native_index_group, pages=[0],
            page_rotations={0: 90})
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()
        candidates = [c for c in received.get('candidates', []) if c[0] > 0.9]
        self.assertTrue(candidates, "the other bow-tie must still be found")
        cx, cy = candidates[0][2], candidates[0][3]
        # Rotated-space center of the OTHER bow-tie is ~(100, 300) — its
        # NATIVE-space center (300, 300) would mean the fix is inert.
        self.assertAlmostEqual(cx, 100.0, delta=2.0)
        self.assertAlmostEqual(cy, 300.0, delta=2.0)


class ImageSymbolSearchWorkerTests(unittest.TestCase):
    """ImageSymbolSearchWorker (pid_viewer.py) — the REAL QThread behind
    SimilarSymbolSearchDialog's image-matching mode (2026-08-15, see
    NOTES.md "Bildbaserad 'hitta liknande symbol' — vid sidan av
    vektorlogiken"), tested directly. Modelled exactly on
    SimilarSymbolSearchWorkerTests above: must always emit finished_scan,
    even when fitz.open() itself raises, and must actually find a real
    candidate end-to-end against a real synthetic PDF."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_imgsearchworker_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_finished_scan_emitted_even_when_pdf_open_fails(self):
        from pid_viewer import ImageSymbolSearchWorker
        received = {}
        worker = ImageSymbolSearchWorker(
            '/nonexistent/path/does-not-exist.pdf', 0, (0, 0, 10, 10))
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()
        self.assertIn('candidates', received,
            "finished_scan must fire even when fitz.open() raises")
        self.assertEqual(received['candidates'], [])

    def test_finds_a_real_candidate_end_to_end(self):
        import fitz
        from pid_viewer import ImageSymbolSearchWorker

        path = os.path.join(self._tmpdir, "imgworker.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()

        def bowtie(cx, cy):
            shape.draw_polyline([fitz.Point(cx - 10, cy - 10), fitz.Point(cx - 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(0, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(cx + 10, cy - 10), fitz.Point(cx + 10, cy + 10),
                                 fitz.Point(cx, cy)])
            shape.finish(color=(0, 0, 0), fill=(0, 0, 0), closePath=True)

        bowtie(60, 60)
        bowtie(300, 300)
        shape.commit()
        doc.save(path)
        doc.close()

        received = {}
        worker = ImageSymbolSearchWorker(path, 0, (48, 48, 72, 72), pages=[0])
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()
        candidates = received.get('candidates')
        self.assertTrue(candidates)
        self.assertGreater(max(c[0] for c in candidates), 0.9)

    def _multi_page_bowtie_pdf(self, n_pages):
        """n_pages pages, each with the SAME bow-tie at (60, 60) — used
        to force the real parallel path (see _should_parallelize, which
        requires >= 4 pages) and confirm it finds the expected match on
        every non-reference page, not just page 0."""
        import fitz
        path = os.path.join(self._tmpdir, "multipage.pdf")
        doc = fitz.open()
        for _ in range(n_pages):
            page = doc.new_page(width=200, height=200)
            shape = page.new_shape()
            shape.draw_polyline([fitz.Point(50, 50), fitz.Point(50, 70), fitz.Point(60, 60)])
            shape.finish(color=(0, 0, 0), fill=(0, 0, 0), closePath=True)
            shape.draw_polyline([fitz.Point(70, 50), fitz.Point(70, 70), fitz.Point(60, 60)])
            shape.finish(color=(0, 0, 0), fill=(0, 0, 0), closePath=True)
            shape.commit()
        doc.save(path)
        doc.close()
        return path

    def test_parallel_path_finds_candidates_across_multiple_pages(self):
        """raster-sökning — parallellisering över flera processer
        (2026-08-16, see NOTES.md): a 5-page document forces
        _should_parallelize's real ProcessPoolExecutor path (not just the
        untouched sequential fallback single-page searches already
        exercise above) — must still find the same bow-tie on every
        OTHER page, with results shaped identically to the sequential
        path (same (sim, page_num, x, y, outline) tuple contract)."""
        from pid_viewer import ImageSymbolSearchWorker
        path = self._multi_page_bowtie_pdf(5)
        received = {}
        worker = ImageSymbolSearchWorker(path, 0, (48, 48, 72, 72))   # pages=None -> whole document
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        worker.start()
        self.assertTrue(worker.wait(20000), "parallel worker.run() did not finish within 20s")
        self.app.processEvents()
        candidates = received.get('candidates')
        self.assertTrue(candidates)
        found_pages = {c[1] for c in candidates if c[0] > 0.9}
        self.assertEqual(found_pages, {1, 2, 3, 4},
            "the bow-tie must be found on every page except the reference's own (page 0)")

    def test_parallel_path_emits_finished_scan_on_cancel(self):
        """Same contract ParallelTagScanWorkerTests already establishes
        for the tag scan's own parallel path: finished_scan must fire
        even when cancelled before any worker process completes."""
        from pid_viewer import ImageSymbolSearchWorker
        path = self._multi_page_bowtie_pdf(5)
        worker = ImageSymbolSearchWorker(path, 0, (48, 48, 72, 72))
        received = {}
        worker.finished_scan.connect(lambda c: received.setdefault('candidates', c))
        with unittest.mock.patch.object(worker, 'isInterruptionRequested', return_value=True):
            worker.run()
        self.assertIn('candidates', received)
        self.assertEqual(received['candidates'], [])


class EquipmentAnalysisWorkerTests(unittest.TestCase):
    """EquipmentAnalysisWorker (pid_viewer.py) — the QThread behind
    EquipmentPanel._autodetect(). Modelled on ConnectorAnalyzer: must
    always emit finished_analysis, even when fitz.open() itself raises,
    so the caller's modal QProgressDialog can never hang forever."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_finished_analysis_emitted_even_when_pdf_open_fails(self):
        from pid_viewer import EquipmentAnalysisWorker
        received = {}

        def _capture(results, rejected):
            received['results'] = results
            received['rejected'] = rejected

        worker = EquipmentAnalysisWorker("/nonexistent/path/does-not-exist.pdf", [])
        worker.finished_analysis.connect(_capture)
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        # finished_analysis is queued across threads (the slot is a plain
        # function with no QObject thread affinity) — wait() only blocks
        # until the WORKER thread stops, it doesn't pump the main thread's
        # event queue, so the delivery itself needs an explicit tick.
        self.app.processEvents()
        self.assertIn('results', received,
            "finished_analysis must fire even when fitz.open() raises")
        self.assertEqual(received['results'], [])
        self.assertEqual(received['rejected'], [])


class PickWorkerCountTests(unittest.TestCase):
    """_pick_worker_count (pid_viewer.py) — how many worker PROCESSES to
    use for a parallel scan/analysis."""

    def test_caps_easyocr_workers_even_with_many_cpus(self):
        from pid_viewer import _pick_worker_count
        with unittest.mock.patch('pid_viewer.os.cpu_count', return_value=32), \
             unittest.mock.patch('pid_viewer.ocr_status',
                                  return_value={'tesseract': False, 'easyocr': True, 'rapidocr': False}):
            n = _pick_worker_count(n_pages=100, use_ocr=True, ocr_engine='auto')
        self.assertLessEqual(
            n, 3, "EasyOCR workers must be capped regardless of CPU count — each "
                  "loads its own ~1GB model (see NOTES.md)")

    def test_rapidocr_not_capped_like_easyocr(self):
        from pid_viewer import _pick_worker_count
        with unittest.mock.patch('pid_viewer.os.cpu_count', return_value=8), \
             unittest.mock.patch('pid_viewer.ocr_status',
                                  return_value={'tesseract': False, 'easyocr': True, 'rapidocr': True}):
            n = _pick_worker_count(n_pages=100, use_ocr=True, ocr_engine='auto')
        self.assertEqual(n, 7, "leaves one core for the UI thread, no extra RapidOCR cap")

    def test_never_exceeds_page_count(self):
        from pid_viewer import _pick_worker_count
        with unittest.mock.patch('pid_viewer.os.cpu_count', return_value=16):
            n = _pick_worker_count(n_pages=2, use_ocr=False, ocr_engine='auto')
        self.assertLessEqual(n, 2)


class OcrEngineThreadLimitTests(unittest.TestCase):
    """'jag tycker analysera p&id tar för lång tid' (2026-08-10, see
    NOTES.md) — investigated and measured: RapidOCR builds its
    onnxruntime session with no explicit thread limit, so it defaults to
    every logical core. When several worker PROCESSES each do that
    concurrently (the whole point of parallelizing 'Analysera P&ID'),
    they compete for the same cores instead of adding throughput —
    confirmed by direct experiment (8 concurrent RapidOCR processes:
    273s unconstrained vs 111s limited to a couple of threads each,
    2.46x, identical OCR output). _limit_ocr_engine_threads() fixes this
    for real."""

    def setUp(self):
        import onnxruntime as ort
        self._orig_session_options = ort.SessionOptions

    def tearDown(self):
        import onnxruntime as ort
        ort.SessionOptions = self._orig_session_options

    def test_limit_ocr_engine_threads_patches_session_options(self):
        from equipment_detection import _limit_ocr_engine_threads
        import onnxruntime as ort
        _limit_ocr_engine_threads(2)
        so = ort.SessionOptions()
        self.assertEqual(so.intra_op_num_threads, 2)
        self.assertEqual(so.inter_op_num_threads, 1)
        self.assertTrue(issubclass(ort.SessionOptions, self._orig_session_options))

    def test_scan_page_range_worker_limits_only_when_multiple_workers_and_ocr(self):
        from equipment_detection import _scan_page_range_worker
        with unittest.mock.patch('equipment_detection._limit_ocr_engine_threads') as mock_limit, \
             unittest.mock.patch('equipment_detection._scan_one_page_native', return_value=[]), \
             unittest.mock.patch('equipment_detection._scan_one_page_ocr', return_value=([], None)), \
             unittest.mock.patch('fitz.open'):
            _scan_page_range_worker('fake.pdf', [0], use_ocr=False, ocr_engine='auto', n_workers=8)
            mock_limit.assert_not_called()

            _scan_page_range_worker('fake.pdf', [0], use_ocr=True, ocr_engine='auto', n_workers=1)
            mock_limit.assert_not_called()

            _scan_page_range_worker('fake.pdf', [0], use_ocr=True, ocr_engine='auto', n_workers=8)
            mock_limit.assert_called_once()

    def test_scan_page_range_worker_divides_cores_by_worker_count(self):
        from equipment_detection import _scan_page_range_worker
        with unittest.mock.patch('equipment_detection._limit_ocr_engine_threads') as mock_limit, \
             unittest.mock.patch('equipment_detection._scan_one_page_native', return_value=[]), \
             unittest.mock.patch('equipment_detection._scan_one_page_ocr', return_value=([], None)), \
             unittest.mock.patch('equipment_detection.os.cpu_count', return_value=14), \
             unittest.mock.patch('fitz.open'):
            _scan_page_range_worker('fake.pdf', [0], use_ocr=True, ocr_engine='auto', n_workers=8)
        mock_limit.assert_called_once_with(1)   # max(1, 14 // 8)


class ShouldParallelizeTests(unittest.TestCase):
    def test_below_threshold_stays_sequential(self):
        from pid_viewer import _should_parallelize
        self.assertFalse(_should_parallelize(3, 4))
        self.assertFalse(_should_parallelize(10, 1))

    def test_meets_threshold_parallelizes(self):
        from pid_viewer import _should_parallelize
        self.assertTrue(_should_parallelize(4, 2))


class ParallelWorkerCancellationTests(unittest.TestCase):
    """ParallelTagScanWorker/ParallelEquipmentAnalysisWorker must ALWAYS
    emit their 'finished' signal, even when cancelled mid-run — the same
    contract EquipmentAnalysisWorker/ConnectorAnalyzer already guarantee.
    Uses a document large enough to force the real parallel path (see
    _should_parallelize) so this actually exercises ProcessPoolExecutor
    cancellation, not just the untouched sequential fallback."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_parcancel_test_")
        self.pdf_path = os.path.join(self._tmpdir, "test.pdf")
        import fitz
        doc = fitz.open()
        for i in range(6):
            doc.new_page(width=200, height=200)
        doc.save(self.pdf_path)
        doc.close()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_parallel_tag_scan_worker_emits_finished_scan_on_cancel(self):
        from pid_viewer import ParallelTagScanWorker
        worker = ParallelTagScanWorker(self.pdf_path, use_ocr=False)
        received = {}
        worker.finished_scan.connect(lambda r: received.setdefault('result', r))
        with unittest.mock.patch.object(worker, 'isInterruptionRequested', return_value=True):
            worker.run()
        self.assertIn('result', received)
        self.assertEqual(received['result'], {})

    def test_parallel_equipment_analysis_worker_emits_finished_on_cancel(self):
        from pid_viewer import ParallelEquipmentAnalysisWorker
        worker = ParallelEquipmentAnalysisWorker(self.pdf_path, tag_points=[])
        received = {}
        worker.finished_analysis.connect(
            lambda results, rejected: received.setdefault('r', (results, rejected)))
        with unittest.mock.patch.object(worker, 'isInterruptionRequested', return_value=True):
            worker.run()
        self.assertIn('r', received)
        self.assertEqual(received['r'], ([], []))


class PageProgressDialogTests(unittest.TestCase):
    """PageProgressDialog (pid_viewer.py) — the per-page status board
    replacing the old single-line QProgressDialog."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_set_page_status_updates_summary_count(self):
        from pid_viewer import PageProgressDialog
        dlg = PageProgressDialog("Test", 3)
        try:
            self.assertIn("0/3", dlg._summary_lbl.text())
            dlg.set_page_status(0, 'running')
            self.assertIn("0/3", dlg._summary_lbl.text(),
                          "'running' must not count as done")
            dlg.set_page_status(0, 'done')
            self.assertIn("1/3", dlg._summary_lbl.text())
            dlg.set_page_status(2, 'done')
            self.assertIn("2/3", dlg._summary_lbl.text())
        finally:
            dlg.deleteLater()

    def test_unknown_page_number_is_a_no_op(self):
        from pid_viewer import PageProgressDialog
        dlg = PageProgressDialog("Test", 2)
        try:
            dlg.set_page_status(99, 'done')   # out of range — must not raise
            self.assertIn("0/2", dlg._summary_lbl.text())
        finally:
            dlg.deleteLater()

    def test_cancel_button_emits_canceled_signal(self):
        from pid_viewer import PageProgressDialog
        dlg = PageProgressDialog("Test", 2)
        try:
            received = []
            dlg.canceled.connect(lambda: received.append(True))
            dlg._on_cancel_clicked()
            self.assertEqual(received, [True])
        finally:
            dlg.deleteLater()


class EquipmentTagSearchWorkerTests(unittest.TestCase):
    """EquipmentTagSearchWorker (pid_viewer.py, 2026-08-18, see NOTES.md
    "kombinerad placeringsmeny") — the QThread behind the background tag
    search a freshly-placed object's identity popup no longer blocks on.
    Modelled on EquipmentAnalysisWorkerTests: must always emit
    finished_search, even when fitz.open() itself raises."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        import fitz
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_tagsearchworker_test_")
        self.pdf_path = os.path.join(self._tmpdir, "test.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400.0, height=300.0)
        page.insert_text((60, 60), "PV-101")
        doc.save(self.pdf_path)
        doc.close()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _run(self, worker):
        received = {}
        worker.finished_search.connect(lambda tag: received.setdefault('tag', tag))
        worker.start()
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()
        return received.get('tag')

    def test_finds_tag_inside_the_given_rect(self):
        from pid_viewer import EquipmentTagSearchWorker
        worker = EquipmentTagSearchWorker(self.pdf_path, 0, rect=(50, 50, 150, 75))
        self.assertEqual(self._run(worker), "PV-101")

    def test_does_not_fall_back_to_point_search_when_rect_is_empty(self):
        """2026-08-24 (see NOTES.md "Åtta UX/logik-förbättringar") — a rect
        with no text inside it must return an empty tag, NOT fall back to
        a nearby-point/whole-page search. That fallback used to exist
        (mirroring what _on_zone_drawn did synchronously before this
        worker), but on P&IDs where a tag's text is missing or sits far
        from its symbol it grabbed whatever tag-like text happened to be
        nearest on the page — producing a wrong object/tag number for a
        rectangle the user explicitly drew to contain the right one (or
        nothing). HAS_PIL forced off so an empty rect returns quickly
        instead of running a real (possibly slow/model-downloading) OCR
        pass first — that engine-availability behavior is exercised
        elsewhere, not the point of this test."""
        from pid_viewer import EquipmentTagSearchWorker
        with unittest.mock.patch('equipment_detection.HAS_PIL', False):
            worker = EquipmentTagSearchWorker(self.pdf_path, 0, rect=(200, 200, 220, 220))
            self.assertEqual(self._run(worker), "",
                "a rect with no text inside it must not pick up 'PV-101' from "
                "elsewhere on the page")

    def test_point_only_mode_finds_tag_near_point(self):
        from pid_viewer import EquipmentTagSearchWorker
        worker = EquipmentTagSearchWorker(self.pdf_path, 0, point=(65, 65), radius=50)
        self.assertEqual(self._run(worker), "PV-101")

    def test_finished_search_emitted_even_when_pdf_path_invalid(self):
        from pid_viewer import EquipmentTagSearchWorker
        worker = EquipmentTagSearchWorker(
            "/nonexistent/path/does-not-exist.pdf", 0, point=(0, 0))
        self.assertEqual(self._run(worker), "")




if __name__ == "__main__":
    unittest.main()
