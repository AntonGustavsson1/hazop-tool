#!/usr/bin/env python3
"""Focused tests for lopa_panel.py's inline HAZOP-SCENARIER editing (2026-09-04).

Covers the "Lägg till scenario" removal / "Egen LOPA-konsekvens" rework
(single click creates a local scenario + consequence directly as an
editable table row, no popup) and the new HAZOP-sync behaviour: editing
Grundfrekvens/Konsekvens on a HAZOP-linked row writes through to the real
HAZOP cause/consequence (not just the LOPA-local mirror), as one Ctrl+Z
step, while Orsak stays read-only for those rows. See NOTES.md.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

_TEST_DIR = Path(__file__).resolve().parent
_HAZOP_DIR = _TEST_DIR.parent
for _p in (_HAZOP_DIR, _TEST_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from database import Database
from lopa_panel import LopaPanel


def _ensure_qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


class LopaHierarchySyncTests(unittest.TestCase):
    """Database-level: the new sync methods behind inline editing."""

    def setUp(self):
        self.app = _ensure_qapp()
        self._tmpdir = tempfile.mkdtemp(prefix='hazop_lopa_panel_test_')
        self.db = Database(path=os.path.join(self._tmpdir, 'test.db'))

    def tearDown(self):
        del self.db
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _seed_hazop_source(self):
        """One HAZOP cause/consequence pair, imported into a fresh LOPA."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, description='Test', base_frequency=0.1)
        cons_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(cons_id, 'Test-konsekvens', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(cons_id, category['id'], 3)
        sg_id = self.db.add_safeguard(cons_id)
        self.db.update_safeguard(sg_id, description='LSHH', rrf=10, sg_type='SIS')
        created = self.db.create_lopa(sif_name='Sync test')
        imported = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sg_id)
        assessment = next(row for row in self.db.lopa_source_consequences(imported['source_id'])
                          if row['hazop_consequence_id'] == cons_id)
        return {
            'cause_id': cause_id, 'cons_id': cons_id,
            'source_id': imported['source_id'], 'assessment_id': assessment['id'],
        }

    def test_sync_frequency_updates_hazop_and_mirror_as_one_undo_step(self):
        seed = self._seed_hazop_source()
        before_undo_count = self.db.undo_count
        self.db.sync_lopa_cause_frequency_to_hazop(seed['source_id'], seed['cause_id'], 0.02)

        self.assertEqual(0.02, self.db.get_cause(seed['cause_id'])['base_frequency'])
        mirror = self.db.conn.execute(
            'SELECT base_frequency FROM lopa_source_scenarios WHERE id=?',
            (seed['source_id'],)).fetchone()
        self.assertEqual(0.02, mirror['base_frequency'])
        self.assertEqual(before_undo_count + 1, self.db.undo_count)

        self.assertTrue(self.db.undo())
        self.assertEqual(0.1, self.db.get_cause(seed['cause_id'])['base_frequency'])
        mirror = self.db.conn.execute(
            'SELECT base_frequency FROM lopa_source_scenarios WHERE id=?',
            (seed['source_id'],)).fetchone()
        self.assertEqual(0.1, mirror['base_frequency'])

    def test_sync_consequence_description_updates_hazop_and_mirror_as_one_undo_step(self):
        seed = self._seed_hazop_source()
        before_undo_count = self.db.undo_count
        self.db.sync_lopa_consequence_description_to_hazop(
            seed['assessment_id'], seed['cons_id'], 'Ändrad text')

        self.assertEqual('Ändrad text', self.db.get_consequence(seed['cons_id'])['description'])
        mirror = self.db.conn.execute(
            'SELECT description,follows_hazop FROM lopa_source_consequences WHERE id=?',
            (seed['assessment_id'],)).fetchone()
        self.assertEqual('Ändrad text', mirror['description'])
        # This is a sync, not a deliberate local override/detach.
        self.assertEqual(1, mirror['follows_hazop'])
        self.assertEqual(before_undo_count + 1, self.db.undo_count)

        self.assertTrue(self.db.undo())
        self.assertEqual('Test-konsekvens', self.db.get_consequence(seed['cons_id'])['description'])

    def test_local_source_edits_only_touch_lopa_mirror(self):
        created = self.db.create_lopa(sif_name='Local test')
        source_id = self.db.add_lopa_local_source(created['revision_id'])
        self.db.set_lopa_source_cause_text(source_id, 'Fritt formulerad orsak')
        self.db.set_lopa_source_frequency(source_id, 0.005)
        row = self.db.conn.execute(
            'SELECT cause_text,local_cause_text,base_frequency,hazop_cause_id '
            'FROM lopa_source_scenarios WHERE id=?', (source_id,)).fetchone()
        # cause_text keeps meaning the "L-001" local-source reference label
        # (add_lopa_local_source/_build_scenario_reference) -- the free-typed
        # Orsak text goes into local_cause_text instead, a separate column
        # (2026-09-04 fix: it used to collide with the reference label and
        # was never even displayed back -- see NOTES.md).
        self.assertEqual('L-001', row['cause_text'])
        self.assertEqual('Fritt formulerad orsak', row['local_cause_text'])
        self.assertEqual(0.005, row['base_frequency'])
        self.assertIsNone(row['hazop_cause_id'])


class LopaHierarchyTableTests(unittest.TestCase):
    """Panel-level: button removal, no-popup creation flow, cell flags."""

    def setUp(self):
        self.app = _ensure_qapp()
        self._tmpdir = tempfile.mkdtemp(prefix='hazop_lopa_panel_ui_test_')
        self.db = Database(path=os.path.join(self._tmpdir, 'test.db'))
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(self.cause_id, description='UI test', base_frequency=0.1)
        cons_id = self.db.add_consequence(self.cause_id)
        self.db.update_consequence(cons_id, 'UI-konsekvens', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(cons_id, category['id'], 3)
        sg_id = self.db.add_safeguard(cons_id)
        self.db.update_safeguard(sg_id, description='LSHH', rrf=10, sg_type='SIS')
        created = self.db.create_lopa(sif_name='UI test')
        self.lopa_id = created['lopa_id']
        self.revision_id = created['revision_id']
        self.db.add_lopa_source_from_safeguard(self.lopa_id, sg_id)
        self.panel = LopaPanel(self.db)
        self.panel.activate_lopa(self.lopa_id, self.revision_id)
        self.app.processEvents()

    def tearDown(self):
        self.panel.deleteLater()
        del self.db
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_add_scenario_button_removed(self):
        self.assertFalse(hasattr(self.panel, '_add_scenario_btn'))

    def test_egen_lopa_konsekvens_creates_row_without_popup(self):
        before_rows = self.panel._hazop_hierarchy.rowCount()
        # No dialog is shown any more -- a direct call (same as a real
        # click) must return without needing to mock/close a QDialog.
        self.panel._add_custom_consequence()
        self.app.processEvents()
        self.assertEqual(before_rows + 1, self.panel._hazop_hierarchy.rowCount())
        row = self.panel._hazop_hierarchy.currentRow()
        item = self.panel._hazop_hierarchy.item(row, self.panel._HAZOP_CONSEQUENCE_COL)
        self.assertIsNotNone(item)
        self.assertTrue(bool(item.flags() & Qt.ItemFlag.ItemIsEditable))

    def test_hazop_row_orsak_is_read_only_local_row_is_editable(self):
        hazop_row = next(
            row for row in range(self.panel._hazop_hierarchy.rowCount())
            if self.panel._hazop_hierarchy.item(row, self.panel._HAZOP_CAUSE_COL)
                .data(self.panel._ROLE_HAZOP_CAUSE_ID) == self.cause_id)
        hazop_item = self.panel._hazop_hierarchy.item(hazop_row, self.panel._HAZOP_CAUSE_COL)
        self.assertFalse(bool(hazop_item.flags() & Qt.ItemFlag.ItemIsEditable))

        self.panel._add_custom_consequence()
        self.app.processEvents()
        local_row = self.panel._hazop_hierarchy.currentRow()
        local_item = self.panel._hazop_hierarchy.item(local_row, self.panel._HAZOP_CAUSE_COL)
        self.assertTrue(bool(local_item.flags() & Qt.ItemFlag.ItemIsEditable))

    def test_editing_frequency_cell_syncs_to_hazop(self):
        hazop_row = next(
            row for row in range(self.panel._hazop_hierarchy.rowCount())
            if self.panel._hazop_hierarchy.item(row, self.panel._HAZOP_CAUSE_COL)
                .data(self.panel._ROLE_HAZOP_CAUSE_ID) == self.cause_id)
        item = self.panel._hazop_hierarchy.item(hazop_row, self.panel._HAZOP_FREQUENCY_COL)
        item.setText('0.03')
        self.app.processEvents()
        self.assertAlmostEqual(0.03, self.db.get_cause(self.cause_id)['base_frequency'])

    def test_orsak_uses_cause_description_not_deviation_text(self):
        """The HAZOP-hierarki Orsak cell shows the cause's own HAZOP text
        ("UI test"), not the deviation/guide-word text (2026-09-04)."""
        dev_id = self.db.deviations(self.db.nodes()[0]['id'])[0]['id']
        deviation_text = self.db.conn.execute(
            'SELECT description FROM deviations WHERE id=?', (dev_id,)).fetchone()[0]
        hazop_row = next(
            row for row in range(self.panel._hazop_hierarchy.rowCount())
            if self.panel._hazop_hierarchy.item(row, self.panel._HAZOP_CAUSE_COL)
                .data(self.panel._ROLE_HAZOP_CAUSE_ID) == self.cause_id)
        text = self.panel._hazop_hierarchy.item(hazop_row, self.panel._HAZOP_CAUSE_COL).text()
        self.assertIn('UI test', text)
        self.assertNotIn(deviation_text, text)

    def test_local_orsak_edit_persists_and_displays(self):
        """Regression test for the bug where a local row's typed Orsak text
        was saved into the wrong column and never even displayed back --
        always showing "Orsak saknas" regardless of what was typed."""
        self.panel._add_custom_consequence()
        self.app.processEvents()
        local_row = self.panel._hazop_hierarchy.currentRow()
        item = self.panel._hazop_hierarchy.item(local_row, self.panel._HAZOP_CAUSE_COL)
        item.setText('Min egen orsakstext')
        self.app.processEvents()
        # _save_hazop_hierarchy_text_edit repopulates the whole table --
        # find the row again and confirm the typed text is what shows now.
        refreshed_row = next(
            row for row in range(self.panel._hazop_hierarchy.rowCount())
            if self.panel._hazop_hierarchy.item(row, self.panel._HAZOP_CAUSE_COL)
                .text() == 'Min egen orsakstext')
        self.assertIsNotNone(refreshed_row)

    def test_egen_lopa_konsekvens_creates_one_assessment_per_category(self):
        options = self.panel._category_options()
        self.assertGreaterEqual(len(options), 2, 'fixture needs 2+ categories to be a real test')
        self.panel._add_custom_consequence()
        self.app.processEvents()
        row = self.panel._hazop_hierarchy.currentRow()
        source_id = self.panel._hazop_hierarchy.item(
            row, self.panel._HAZOP_CONSEQUENCE_COL).data(self.panel._ROLE_SOURCE_ID)
        assessments = self.db.lopa_source_consequences(source_id)
        self.assertEqual(len(options), len(assessments))
        group_ids = {row['local_group_id'] for row in assessments}
        self.assertEqual(1, len(group_ids), 'every category assessment shares one local_group_id')

    def test_local_category_spinbox_sets_severity_without_confirm_dialog(self):
        self.panel._add_custom_consequence()
        self.app.processEvents()
        row = self.panel._hazop_hierarchy.currentRow()
        source_id = self.panel._hazop_hierarchy.item(
            row, self.panel._HAZOP_CONSEQUENCE_COL).data(self.panel._ROLE_SOURCE_ID)
        assessment_id = self.db.lopa_source_consequences(source_id)[0]['id']
        called = []
        self.panel._confirm_lopa_only = lambda *a, **k: called.append(1) or True
        self.panel._on_local_category_severity_changed(assessment_id, 7)
        self.assertEqual([], called, 'local severity change must not ask to detach from HAZOP')
        updated = next(r for r in self.db.lopa_source_consequences(source_id)
                       if r['id'] == assessment_id)
        self.assertEqual(7, updated['severity'])

    def test_hazop_category_toggle_still_asks_confirm(self):
        called = []
        self.panel._confirm_lopa_only = lambda *a, **k: called.append(1) or False
        # Any real assessment id + hazop_linked=True must still prompt.
        self.panel._on_hazop_category_active_changed(1, 1, True, hazop_linked=True)
        self.assertEqual([1], called)


if __name__ == '__main__':
    unittest.main()
