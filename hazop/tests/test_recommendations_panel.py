#!/usr/bin/env python3
"""Tests for recommendations_panel.py (RecommendationsPanel) — the
"Rekommendationer" page added 2026-08-26 (see NOTES.md), inserted right
after Worksheet in MainWindow's nav rail / view_stack (new index 3,
shifting Utrustning/Studiehantering/Inställningar from 3/4/5 to 4/5/6).

Follows this repo's per-module test-file boilerplate exactly (see
tests/test_worksheet.py) — the _HAZOP_DIR/_TEST_DIR sys.path bootstrap,
QT_QPA_PLATFORM=offscreen, and test_helpers.py's shared fixtures."""

import os
import sys
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

# ── Headless Qt setup — MUST happen before importing PyQt6 or hazop ────────
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QEvent
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QHeaderView, QMessageBox

_TEST_DIR = Path(__file__).resolve().parent
_HAZOP_DIR = _TEST_DIR.parent
for _p in (_HAZOP_DIR, _TEST_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hazop  # noqa: E402  (import after sys.path setup, by design)
from hazop import Database, RecommendationsPanel  # noqa: E402

from test_helpers import _ensure_qapp, _TempDbMainWindow  # noqa: E402


class RecommendationsPanelConstructionTests(unittest.TestCase):
    """Headless construction + refresh() on an empty DB must not crash."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_recpanel_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)
        # A fresh Database auto-seeds one default node (see Database.__init__'s
        # pre_existing_db check) -- remove it so tests build their own
        # controlled fixtures without an extra, unaccounted-for node.
        for n in self.db.nodes():
            self.db.delete_node(n['id'])

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_constructs_and_refreshes_headlessly_on_empty_db(self):
        panel = RecommendationsPanel(self.db)
        try:
            try:
                panel.refresh()
            except Exception as e:
                self.fail(f"RecommendationsPanel.refresh() on an empty DB raised: {e!r}")
            self.assertEqual(panel._table.rowCount(), 0)
            self.assertEqual(panel._table.columnCount(), 5)
            self.assertEqual(
                panel._table.horizontalHeaderItem(panel._COL_RESPONSIBLE).text(),
                "Ansvarig")
        finally:
            panel.deleteLater()

    def test_responsible_column_is_wide_and_columns_are_draggable(self):
        panel = RecommendationsPanel(self.db)
        try:
            header = panel._table.horizontalHeader()
            self.assertGreaterEqual(panel._table.columnWidth(panel._COL_RESPONSIBLE), 200)
            self.assertTrue(header.sectionsMovable())
            for col in range(panel._table.columnCount()):
                self.assertEqual(header.sectionResizeMode(col),
                                 QHeaderView.ResizeMode.Interactive)
        finally:
            panel.deleteLater()

    def test_delete_key_removes_selected_recommendation_after_confirmation(self):
        rec_id = self.db.add_recommendation(description="Ta bort mig")
        panel = RecommendationsPanel(self.db)
        try:
            panel.load()
            panel._table.selectRow(0)
            with unittest.mock.patch(
                    'recommendations_panel.QMessageBox.question',
                    return_value=QMessageBox.StandardButton.Yes):
                event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete,
                                  Qt.KeyboardModifier.NoModifier)
                panel._table.keyPressEvent(event)
            self.assertIsNone(self.db.get_recommendation(rec_id))
            self.assertEqual(panel._table.rowCount(), 0)
        finally:
            panel.deleteLater()

    def test_catalog_delete_defaults_to_yes_and_emits_refresh_signal(self):
        rec_id = self.db.add_recommendation(description="Ta bort mig")
        panel = RecommendationsPanel(self.db)
        changed = []
        panel.recommendations_changed.connect(lambda: changed.append(True))
        try:
            panel.load()
            panel._table.selectRow(0)
            with unittest.mock.patch(
                    'recommendations_panel.QMessageBox.question',
                    return_value=QMessageBox.StandardButton.Yes) as question:
                panel._delete_selected()
            self.assertEqual(question.call_args.args[4], QMessageBox.StandardButton.Yes)
            self.assertIsNone(self.db.get_recommendation(rec_id))
            self.assertEqual(changed, [True])
        finally:
            panel.deleteLater()


class RecommendationsPanelReferenceTests(unittest.TestCase):
    """Numbering/reference logic — one recommendation catalog row per
    db.all_recommendations() entry, column 2 shows every
    studie.nod.avvikelse.orsak.konsekvens reference it currently resolves
    to via consequence_recommendations."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_recpanel_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)
        for n in self.db.nodes():
            self.db.delete_node(n['id'])

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _row_for(self, panel, rec_id):
        """Find the table row whose column-1 label starts with this
        recommendation's R-XXX prefix, and return (label, ref_text)."""
        prefix = f"R-{rec_id:03d}."
        for row in range(panel._table.rowCount()):
            label = panel._table.item(row, panel._COL_REC).text()
            if label.startswith(prefix):
                return label, panel._table.item(row, panel._COL_REF).text()
        self.fail(f"no row found for recommendation {rec_id} (prefix {prefix!r})")

    def test_specific_position_produces_exact_hierarchical_reference(self):
        """Deliberately NOT the trivial 1.1.1.1.1 case -- build a 2nd node,
        take its 3rd (raw-DB-order) deviation, add a 2nd cause under it,
        and a 2nd consequence under that cause, to actually exercise the
        1-based position computation at every one of the 5 levels."""
        db = self.db
        db.add_node()                       # node position 1 (unused)
        node2 = db.add_node()                # node position 2
        devs = db.deviations(node2)
        self.assertGreaterEqual(len(devs), 3,
            "fixture assumption: add_node() seeds several standard deviations")
        target_dev = devs[2]['id']           # deviation position 3 under node2

        db.add_cause(target_dev)             # cause position 1 (unused)
        cause2 = db.add_cause(target_dev)    # cause position 2

        db.add_consequence(cause2)           # consequence position 1 (unused)
        cons2 = db.add_consequence(cause2)   # consequence position 2

        rec_id = db.add_recommendation_to_consequence(
            cons2, description="Testrekommendation")

        panel = RecommendationsPanel(db)
        try:
            panel.load()
            label, ref_text = self._row_for(panel, rec_id)
            self.assertEqual(label, f"R-{rec_id:03d}. Testrekommendation")
            self.assertEqual(ref_text, "1.2.3.2.2")
        finally:
            panel.deleteLater()

    def test_zero_linked_recommendation_shows_placeholder(self):
        """An orphaned-but-reusable catalog entry (no current links) must
        show the '-' placeholder, not a blank string -- this app never
        deletes a recommendation just because its last link was removed."""
        db = self.db
        rec_id = db.add_recommendation(description="Ingen koppling")

        panel = RecommendationsPanel(db)
        try:
            panel.load()
            label, ref_text = self._row_for(panel, rec_id)
            self.assertEqual(label, f"R-{rec_id:03d}. Ingen koppling")
            self.assertEqual(ref_text, "—")
        finally:
            panel.deleteLater()

    def test_recommendation_linked_to_two_consequences_shows_both_references(self):
        """A reused recommendation (consequence_recommendations is many-to-
        many) must appear on exactly ONE row, with both references joined
        by ', ' -- not duplicated across two rows."""
        db = self.db
        node1 = db.add_node()
        dev1 = db.deviations(node1)[0]['id']
        cause1 = db.add_cause(dev1)
        cons_a = db.add_consequence(cause1)   # 1.1.1.1.1
        cons_b = db.add_consequence(cause1)   # 1.1.1.1.2

        rec_id = db.add_recommendation(description="Delad rekommendation")
        db.link_recommendation_to_consequence(rec_id, cons_a)
        db.link_recommendation_to_consequence(rec_id, cons_b)

        panel = RecommendationsPanel(db)
        try:
            panel.load()
            rows_with_prefix = [
                r for r in range(panel._table.rowCount())
                if panel._table.item(r, panel._COL_REC).text().startswith(f"R-{rec_id:03d}.")
            ]
            self.assertEqual(len(rows_with_prefix), 1,
                "a reused recommendation must appear on exactly one row, not once per link")
            label, ref_text = self._row_for(panel, rec_id)
            self.assertEqual(ref_text, "1.1.1.1.1, 1.1.1.1.2")
        finally:
            panel.deleteLater()

    def test_all_recommendations_listed_in_catalog_id_order(self):
        db = self.db
        rec_a = db.add_recommendation(description="Först")
        rec_b = db.add_recommendation(description="Sedan")

        panel = RecommendationsPanel(db)
        try:
            panel.load()
            self.assertEqual(panel._table.rowCount(), 2)
            self.assertEqual(
                panel._table.item(0, panel._COL_REC).text(), f"R-{rec_a:03d}. Först")
            self.assertEqual(
                panel._table.item(1, panel._COL_REC).text(), f"R-{rec_b:03d}. Sedan")
        finally:
            panel.deleteLater()

    def test_responsible_person_is_shown_and_missing_value_uses_placeholder(self):
        db = self.db
        assigned = db.add_recommendation(
            description="Kontrollera ventil", responsible="Anna Andersson")
        unassigned = db.add_recommendation(description="Utan ansvarig")

        panel = RecommendationsPanel(db)
        try:
            panel.load()
            responsible_by_id = {}
            for row in range(panel._table.rowCount()):
                label = panel._table.item(row, panel._COL_REC).text()
                responsible = panel._table.item(row, panel._COL_RESPONSIBLE).text()
                responsible_by_id[label.split('.', 1)[0]] = responsible

            self.assertEqual(responsible_by_id[f"R-{assigned:03d}"], "Anna Andersson")
            self.assertEqual(responsible_by_id[f"R-{unassigned:03d}"], "—")
        finally:
            panel.deleteLater()

    def test_search_and_status_filter_hide_non_matching_rows(self):
        db = self.db
        open_id = db.add_recommendation(description="Kontrollera pump", status="Öppen")
        db.add_recommendation(description="Byt packning", status="Klar")
        panel = RecommendationsPanel(db)
        try:
            panel.load()
            self.assertEqual(panel._count_label.text(), "Visar 2 av 2")
            panel._search.setText("pump")
            self.assertEqual(panel._count_label.text(), "Visar 1 av 2")
            self.assertFalse(panel._table.isRowHidden(0 if open_id < 2 else 1))
            panel._search.clear()
            panel._status_filter.setCurrentText("Klar")
            self.assertEqual(panel._count_label.text(), "Visar 1 av 2")
        finally:
            panel.deleteLater()

    def test_status_combo_and_description_edit_are_saved(self):
        db = self.db
        rec_id = db.add_recommendation(description="Gammal text", status="Öppen")
        panel = RecommendationsPanel(db)
        try:
            panel.load()
            status = panel._table.cellWidget(0, panel._COL_STATUS)
            status.setCurrentText("Pågår")
            item = panel._table.item(0, panel._COL_REC)
            item.setText(f"R-{rec_id:03d}. Ny text")
            saved = db.get_recommendation(rec_id)
            self.assertEqual(saved['status'], "Pågår")
            self.assertEqual(saved['description'], "Ny text")
        finally:
            panel.deleteLater()


class MainWindowNavRailRenumberingTests(unittest.TestCase):
    """Regression guard for the 2026-08-26 Rekommendationer insertion:
    every nav button (old and new) must switch to its own correct index,
    and view_stack must contain the pages in the expected order."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_view_stack_has_beta_page_after_settings(self):
        with _TempDbMainWindow() as win:
            self.assertEqual(win.view_stack.count(), 8)
            self.assertIs(win.view_stack.widget(0), win.hazop_prep_panel)
            self.assertIs(win.view_stack.widget(1), win._h_splitter)
            self.assertIs(win.view_stack.widget(2), win.worksheet)
            self.assertIs(win.view_stack.widget(3), win.recommendations_panel)
            self.assertIs(win.view_stack.widget(4), win.equipment_panel)
            self.assertIs(win.view_stack.widget(5), win.admin_panel)
            self.assertIs(win.view_stack.widget(6), win.settings_panel)
            self.assertIs(win.view_stack.widget(7), win.beta_panel)

    def test_btn_recommendations_switches_to_index_3_and_calls_refresh(self):
        with _TempDbMainWindow() as win:
            win.recommendations_panel.refresh = unittest.mock.Mock()
            win.btn_recommendations.click()
            self.assertEqual(win.view_stack.currentIndex(), 3)
            win.recommendations_panel.refresh.assert_called_once()
            self.assertTrue(win.btn_recommendations.isChecked())

    def test_every_other_nav_button_switches_to_its_own_shifted_index(self):
        """Regression guard against an off-by-one in the renumbering that
        accompanied inserting Rekommendationer as the new index 3."""
        with _TempDbMainWindow() as win:
            expected = {
                'btn_prep': 0,
                'btn_pid': 1,
                'btn_sheet': 2,
                'btn_recommendations': 3,
                'btn_equip': 4,
                'btn_admin': 5,
                'btn_settings': 6,
                'btn_beta': 7,
            }
            for attr, idx in expected.items():
                getattr(win, attr).click()
                self.assertEqual(
                    win.view_stack.currentIndex(), idx,
                    f"{attr} must switch to view_stack index {idx}")
                self.assertTrue(
                    getattr(win, attr).isChecked(),
                    f"{attr} must be the checked nav button after its own click")


if __name__ == "__main__":
    unittest.main()
