#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering worksheet.py, plus any cross-module glue they
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
from PyQt6.QtGui import QPixmap, QFocusEvent, QKeyEvent  # noqa: E402
from PyQt6.QtCore import (  # noqa: E402
    Qt, QPoint, QDate, QEvent, QThread, pyqtSignal,
    QItemSelection, QItemSelectionModel,
)
from equipment_detection import COMPONENT_TYPES  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# Shared QApplication — Qt only allows one per process.
# ══════════════════════════════════════════════════════════════════════════


from test_helpers import (
    _ensure_qapp, _menu_action_labels, _fake_pdf_loaded,
    _TempDbMainWindow, _find_tree_item,
)

class HAZOPWorksheetTests(unittest.TestCase):
    """HAZOPWorksheet: node-picker + 'Visa samtliga noder' checkbox wired to
    the embedded ScenarioTablePanel's load_node()/load_all()."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_worksheet_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)
        # 2026-08-24: a fresh Database now auto-seeds one default node (see
        # Database.__init__'s pre_existing_db check) — these tests build
        # their own controlled set of nodes and assert exact counts/order
        # against it, so remove the auto-seeded one to keep that intact.
        for n in self.db.nodes():
            self.db.delete_node(n['id'])

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain(self, node_name=None):
        node_id = self.db.add_node()
        if node_name is not None:
            # Direct SQL rename -- Database.update_node() requires several
            # other positional fields (description, pid_ref, ...) that are
            # irrelevant to these tests, so avoid coupling to that full
            # signature just to set a display name.
            self.db.conn.execute(
                "UPDATE nodes SET name=? WHERE id=?", (node_name, node_id))
            self.db.commit()
        deviation_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(deviation_id)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_instantiates_headless_and_refreshes_on_empty_db(self):
        from hazop import HAZOPWorksheet

        ws = HAZOPWorksheet(self.db)
        try:
            try:
                ws.refresh()
            except Exception as e:
                self.fail(f"HAZOPWorksheet.refresh() on an empty DB raised: {e!r}")
            self.assertEqual(ws._node_combo.count(), 0)
        finally:
            ws.deleteLater()

    def test_embedded_object_rename_is_relayed_to_main_window(self):
        """Worksheet has a separate ScenarioTablePanel, so its shared
        object popup must expose a rename to MainWindow too."""
        from hazop import HAZOPWorksheet

        ws = HAZOPWorksheet(self.db)
        emitted = []
        try:
            ws.equipment_renamed.connect(lambda: emitted.append(True))
            ws._table_panel.equipment_renamed.emit()
            self.assertEqual(emitted, [True])
        finally:
            ws.deleteLater()

    def test_equipment_column_stays_hidden_even_in_all_nodes_mode(self):
        """"i worksheet behöver inte objekt kolumnen synas" (2026-08-13)
        — Utrustning normally reappears in "Visa samtliga noder" mode
        (see ScenarioTablePanel._set_all_nodes_columns_visible's
        docstring), but HAZOPWorksheet opts out of that via
        hide_equipment_column(); the tag is already shown at the top of
        each Orsak cell regardless."""
        from hazop import HAZOPWorksheet
        ws = HAZOPWorksheet(self.db)
        try:
            panel = ws._table_panel
            self.assertTrue(panel._table.isColumnHidden(panel._C_UTR),
                "Utrustning must start hidden")
            ws._all_nodes_cb.setChecked(True)
            self.assertTrue(panel._table.isColumnHidden(panel._C_UTR),
                "Utrustning must stay hidden even in Visa samtliga noder mode")
        finally:
            ws.deleteLater()

    def test_empty_consequence_double_click_edits_inline_not_chain_popup(self):
        """Worksheet keeps consequence editing in the table, even when blank."""
        from hazop import HAZOPWorksheet, ScenarioTablePanel

        ids = self._make_full_chain(node_name="Nod A")
        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()
            panel = ws._table_panel
            consequence_row = next(
                row for row, meta in enumerate(panel._row_meta)
                if meta[2] == ids['cons_id'])
            item = panel._table.item(consequence_row, ScenarioTablePanel._C_KON)
            self.assertIsNotNone(item)
            self.assertFalse(panel._empty_consequence_chain_popup_enabled)

            with (
                unittest.mock.patch.object(panel, "_open_chain_editor") as popup,
                unittest.mock.patch.object(panel._table, "edit",
                                           return_value=True) as edit,
            ):
                panel._on_cell_double_clicked(item)

            popup.assert_not_called()
            edit.assert_called_once_with(
                panel._table.model().index(
                    consequence_row, ScenarioTablePanel._C_KON))
        finally:
            ws.deleteLater()

    def test_single_click_cause_selects_without_opening_inline_editor(self):
        """A cause click is selection; editing requires a deliberate action."""
        from hazop import HAZOPWorksheet, ScenarioTablePanel

        ids = self._make_full_chain(node_name='Nod A')
        ws = HAZOPWorksheet(self.db)
        selected = []
        try:
            ws.refresh()
            panel = ws._table_panel
            row = next(row for row, meta in enumerate(panel._row_meta)
                       if meta[1] == ids['cause_id'])
            panel.item_selected.connect(
                lambda kind, item_id: selected.append((kind, item_id)))

            with unittest.mock.patch.object(panel, '_try_start_edit') as edit:
                panel._on_cell_clicked(row, ScenarioTablePanel._C_ORS)

            self.assertEqual(selected, [(CAUSE_T, ids['cause_id'])])
            edit.assert_not_called()
        finally:
            ws.deleteLater()

    def test_office_copy_contains_visible_hierarchy_spans_and_formatting(self):
        """Word/Excel clipboard data must be a faithful rich worksheet table."""
        from hazop import HAZOPWorksheet

        ids = self._make_full_chain(node_name='Nod A')
        equipment_id = self.db.add_equipment_item(
            'PV-101', 'PV-101', 'PV', 1, 'Ventil', '', 0)
        self.db.update_cause(ids['cause_id'], description='Felar stängd',
                             comp_type='Ventil', comp_tag='PV-101',
                             equipment_id=equipment_id)
        self.db.update_consequence(
            ids['cons_id'], 'PV-101 ger stopp', 3,
            comp_type='Ventil', comp_tag='PV-101', tagged_refs='PV-101')
        self.db.update_safeguard(ids['sg_id'], description='LSHH stoppar', rrf=100)
        self.db.add_safeguard(ids['cons_id'])  # gives the hierarchy a rowspan
        category_id = self.db.consequence_categories()[0]['id']
        self.db.set_consequence_severity(ids['cons_id'], category_id, 4)

        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()
            panel = ws._table_panel
            html, plain_text = panel._office_clipboard_payload('HAZOP Worksheet')
            self.assertIn('<table', html)
            self.assertIn('<th', html)
            self.assertNotIn('<p ', html)
            self.assertNotIn('HAZOP Worksheet', html)
            self.assertIn('Nod', html)
            self.assertIn('Nod A', html)
            self.assertIn('Felar stängd', html)
            self.assertIn('PV-101', html)
            self.assertIn('<strong>PV-101</strong>', html)
            self.assertIn('rowspan="2"', html)
            self.assertIn('background:', html)
            self.assertIn('Nod\tAvvikelse', plain_text)
            self.assertIn('LSHH stoppar', plain_text)
            self.assertIn('RRF 100', plain_text)

            self.assertTrue(panel.copy_visible_table_to_office_clipboard())
            mime = QApplication.clipboard().mimeData()
            self.assertTrue(mime.hasHtml())
            self.assertNotIn('HAZOP Worksheet', mime.html())
            with unittest.mock.patch('worksheet.QTimer.singleShot') as delayed:
                ws._office_copy_btn.click()
            self.assertEqual(ws._office_copy_btn.text(), 'Markering kopierad')
            delayed.assert_called_once()
        finally:
            ws.deleteLater()

    def test_office_copy_uses_selected_multirow_multicolumn_rectangle(self):
        """A Worksheet selection must export only its rectangular data block."""
        from hazop import HAZOPWorksheet

        ids = self._make_full_chain(node_name='Nod A')
        self.db.update_cause(ids['cause_id'], description='Orsak A')
        self.db.update_consequence(ids['cons_id'], 'Konsekvens A', 3)
        self.db.update_safeguard(ids['sg_id'], description='Barriär A', rrf=100)
        self.db.add_safeguard(ids['cons_id'])

        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()
            panel = ws._table_panel
            table = panel._table
            cause_row = next(row for row, meta in enumerate(panel._row_meta)
                             if meta[1] == ids['cause_id'])
            last_row = cause_row + 1
            selection = QItemSelection(
                table.model().index(cause_row, panel._C_ORS),
                table.model().index(last_row, panel._C_SG))
            table.selectionModel().select(
                selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)

            html, plain_text = panel._office_clipboard_payload('Vald del')

            self.assertIn('Orsak', html)
            self.assertIn('Konsekvens', html)
            self.assertIn('Barriär', html)
            self.assertIn('Orsak A', html)
            self.assertIn('Konsekvens A', html)
            self.assertIn('Barriär A', html)
            self.assertNotIn('Nod A', html)
            self.assertIn('rowspan="2"', html)
            self.assertTrue(plain_text.startswith('Orsak (frekvens)\tKonsekvens'))
            self.assertEqual(len(plain_text.splitlines()), 3)
            self.assertNotIn('Vald del', html)

            self.assertTrue(panel.copy_visible_table_to_office_clipboard('Vald del'))
            mime = QApplication.clipboard().mimeData()
            self.assertTrue(mime.hasHtml())
            self.assertNotIn('Vald del', mime.html())
            self.assertNotIn('Nod A', mime.html())
        finally:
            ws.deleteLater()

    def test_office_copy_merges_one_cause_across_two_consequences(self):
        """A visual cause span must not become duplicate Office cells."""
        from hazop import HAZOPWorksheet, ScenarioTablePanel
        from ui_helpers import freq_axis_label

        ids = self._make_full_chain(node_name='Nod A')
        second_cons_id = self.db.add_consequence(ids['cause_id'])
        second_sg_id = self.db.add_safeguard(second_cons_id)
        self.db.update_cause(ids['cause_id'], description='Orsak A')
        self.db.update_consequence(ids['cons_id'], 'Konsekvens 1', 3)
        self.db.update_consequence(second_cons_id, 'Konsekvens 2', 4)
        self.db.update_safeguard(ids['sg_id'], description='Barriär 1', rrf=10)
        self.db.update_safeguard(second_sg_id, description='Barriär 2', rrf=100)
        category_id = self.db.consequence_categories()[0]['id']
        self.db.set_consequence_severity(ids['cons_id'], category_id, 3)
        self.db.set_consequence_severity(second_cons_id, category_id, 4)

        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()
            panel = ws._table_panel
            table = panel._table
            rows = [row for row, meta in enumerate(panel._row_meta)
                    if meta[1] == ids['cause_id']]
            self.assertEqual(len(rows), 2)
            selection = QItemSelection(
                table.model().index(rows[0], ScenarioTablePanel._C_ORS),
                table.model().index(rows[-1], ScenarioTablePanel._C_SG))
            table.selectionModel().select(
                selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)

            html, plain_text = panel._office_clipboard_payload()
            frequency = table.item(rows[0], ScenarioTablePanel._C_ORS).data(
                Qt.ItemDataRole.UserRole + 3)

            self.assertEqual(html.count('Orsak A'), 1)
            self.assertEqual(html.count('rowspan="2"'), 1)
            self.assertIn('Konsekvens 1', html)
            self.assertIn('Konsekvens 2', html)
            self.assertIn(freq_axis_label(frequency), html)
            self.assertIn('RRF 10', html)
            self.assertIn('RRF 100', html)
            # The TSV fallback has no merge primitive, so its covered cause
            # position stays blank rather than duplicating the cause text.
            self.assertEqual(plain_text.count('Orsak A'), 1)
        finally:
            ws.deleteLater()

    def test_office_copy_uses_live_entity_data_in_each_object_column(self):
        """Clipboard data must not borrow a stale tag/RRF from an item role."""
        from hazop import HAZOPWorksheet, ScenarioTablePanel

        ids = self._make_full_chain(node_name='Nod A')
        for tag, equipment_type in (
                ('KON-TRUE', 'Tank'), ('SG-TRUE', 'Sensor'),
                ('REC-TRUE', 'Ventil')):
            self.db.add_equipment_item(tag, tag, tag.split('-')[0], 1,
                                       equipment_type, '', 0)
        self.db.update_cause(
            ids['cause_id'], description='Orsak', comp_type='Ventil',
            comp_tag='CAUSE-TRUE')
        self.db.update_consequence(
            ids['cons_id'], 'KON-TRUE konsekvens', 3,
            comp_type='Tank', comp_tag='KON-TRUE', tagged_refs='KON-TRUE')
        self.db.update_safeguard(
            ids['sg_id'], description='SG-TRUE barriär', rrf=10,
            tagged_refs='SG-TRUE')
        self.db.set_safeguard_tag(ids['sg_id'], 'SG-TRUE', 'Sensor')
        self.db.add_reduction_factor(ids['cons_id'], 'Enabler', 10)
        self.db.add_recommendation_to_consequence(
            ids['cons_id'], 'REC-TRUE rekommendation')

        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()
            panel = ws._table_panel
            table = panel._table
            row = next(row for row, meta in enumerate(panel._row_meta)
                       if meta[2] == ids['cons_id'])

            # These role/button changes emulate the short interval after a
            # user changes an object or RRF and before an old widget instance
            # is destroyed. The copied result must still use the database.
            table.item(row, ScenarioTablePanel._C_ORS).setData(
                Qt.ItemDataRole.UserRole + 2, ('Fel typ', 'CAUSE-WRONG'))
            table.item(row, ScenarioTablePanel._C_SG).setData(
                Qt.ItemDataRole.UserRole + 1, 999)
            table.blockSignals(True)
            try:
                table.item(row, ScenarioTablePanel._C_REK).setText(
                    '999. REC-WRONG rekommendation')
            finally:
                table.blockSignals(False)
            lopa = table.cellWidget(row, ScenarioTablePanel._C_LOPA)
            lopa._extra_btn.setText('99 (999)')

            selection = QItemSelection(
                table.model().index(row, ScenarioTablePanel._C_NOD),
                table.model().index(row, ScenarioTablePanel._C_REK))
            table.selectionModel().select(
                selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            html, plain_text = panel._office_clipboard_payload()

            self.assertIn('<strong>CAUSE-TRUE</strong>', html)
            self.assertIn('<strong>KON-TRUE</strong>', html)
            self.assertIn('<strong>SG-TRUE</strong>', html)
            self.assertIn('<strong>REC-TRUE</strong>', html)
            self.assertIn('RRF 10', html)
            self.assertIn('1 (10)', html)
            self.assertIn('RRF 10', plain_text)
            self.assertNotIn('CAUSE-WRONG', html)
            self.assertNotIn('REC-WRONG', html)
            self.assertNotIn('999', html)
        finally:
            ws.deleteLater()

    def test_scenario_office_copy_uses_live_deviation_equipment(self):
        """Scenario's equipment column must not export an old object label."""
        from hazop import ScenarioTablePanel

        ids = self._make_full_chain(node_name='Nod A')
        equipment_id = self.db.add_equipment_item(
            'DEV-TRUE', 'DEV-TRUE', 'DEV', 1, 'Pump', '', 0)
        self.db.conn.execute(
            'UPDATE deviations SET equipment_id=? WHERE id=?',
            (equipment_id, ids['deviation_id']))
        self.db.commit()

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids['node_id'])
            table = panel._table
            table.setColumnHidden(ScenarioTablePanel._C_UTR, False)
            row = next(row for row, meta in enumerate(panel._row_meta)
                       if meta[0] == ids['deviation_id'])
            table.blockSignals(True)
            try:
                table.item(row, ScenarioTablePanel._C_UTR).setText('DEV-WRONG')
            finally:
                table.blockSignals(False)
            selection = QItemSelection(
                table.model().index(row, ScenarioTablePanel._C_UTR),
                table.model().index(row, ScenarioTablePanel._C_UTR))
            table.selectionModel().select(
                selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)

            html, plain_text = panel._office_clipboard_payload()
            self.assertIn('DEV-TRUE', html)
            self.assertIn('Pump', html)
            self.assertIn('DEV-TRUE', plain_text)
            self.assertNotIn('DEV-WRONG', html)
        finally:
            panel.deleteLater()

    def test_ctrl_c_copies_exact_selected_cells_and_hazop_entity(self):
        """Ctrl+C keeps Office cells while also enabling scoped HAZOP paste."""
        from hazop import HAZOPWorksheet

        ids = self._make_full_chain(node_name='Nod A')
        self.db.update_cause(ids['cause_id'], description='Orsak A')
        self.db.update_consequence(ids['cons_id'], 'Konsekvens A', 3)
        self.db.update_safeguard(ids['sg_id'], description='Barriär A', rrf=100)
        self.db.add_safeguard(ids['cons_id'])

        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()
            panel = ws._table_panel
            table = panel._table
            cause_row = next(row for row, meta in enumerate(panel._row_meta)
                             if meta[1] == ids['cause_id'])
            table.setCurrentCell(cause_row, panel._C_ORS)
            selection = QItemSelection(
                table.model().index(cause_row, panel._C_ORS),
                table.model().index(cause_row + 1, panel._C_SG))
            table.selectionModel().select(
                selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)

            event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C,
                              Qt.KeyboardModifier.ControlModifier)
            self.assertTrue(panel.eventFilter(table, event))

            mime = QApplication.clipboard().mimeData()
            self.assertTrue(mime.hasHtml())
            self.assertTrue(mime.hasFormat(panel._COPY_MIME))
            self.assertIn('Orsak A', mime.html())
            self.assertIn('Konsekvens A', mime.html())
            self.assertIn('rowspan="2"', mime.html())
            self.assertNotIn('Nod A', mime.html())
        finally:
            ws.deleteLater()

    def test_office_copy_keeps_ctrl_selected_node_and_deviation_rows_compact(self):
        """Separate Nod/Avvikelse selections must not expand rowspans.

        A node and deviation can cover many physical scenario rows.  Ctrl
        selecting the two hierarchy cells for two nodes should therefore
        export two hierarchy rows, not a long run of duplicate node labels.
        """
        from hazop import HAZOPWorksheet, ScenarioTablePanel

        first = self._make_full_chain(node_name='Nod A')
        second = self._make_full_chain(node_name='Nod B')
        self.db.conn.execute('UPDATE deviations SET description=? WHERE id=?',
                             ('Avvikelse A', first['deviation_id']))
        self.db.conn.execute('UPDATE deviations SET description=? WHERE id=?',
                             ('Avvikelse B', second['deviation_id']))
        self.db.commit()

        ws = HAZOPWorksheet(self.db)
        try:
            ws._all_nodes_cb.setChecked(True)
            ws.refresh()
            panel = ws._table_panel
            table = panel._table
            first_row = next(row for row, meta in enumerate(panel._row_meta)
                             if meta[1] == first['cause_id'])
            second_row = next(row for row, meta in enumerate(panel._row_meta)
                              if meta[1] == second['cause_id'])
            selection_model = table.selectionModel()
            for flags, row in (
                    (QItemSelectionModel.SelectionFlag.ClearAndSelect, first_row),
                    (QItemSelectionModel.SelectionFlag.Select, second_row)):
                selection_model.select(
                    QItemSelection(
                        table.model().index(row, ScenarioTablePanel._C_NOD),
                        table.model().index(row, ScenarioTablePanel._C_DEV)),
                    flags)

            html, plain_text = panel._office_clipboard_payload()

            self.assertEqual(len(plain_text.splitlines()), 3)
            self.assertEqual(html.count('Nod A'), 1)
            self.assertEqual(html.count('Nod B'), 1)
            self.assertEqual(html.count('Avvikelse A'), 1)
            self.assertEqual(html.count('Avvikelse B'), 1)
        finally:
            ws.deleteLater()

    def test_refresh_after_creating_nodes_populates_and_loads(self):
        from hazop import HAZOPWorksheet

        ws = HAZOPWorksheet(self.db)
        try:
            ids = self._make_full_chain(node_name="Nod A")
            try:
                ws.refresh()
            except Exception as e:
                self.fail(f"HAZOPWorksheet.refresh() after adding a node raised: {e!r}")
            self.assertEqual(ws._node_combo.count(), 1)
            self.assertEqual(ws._node_combo.currentData(), ids['node_id'])
        finally:
            ws.deleteLater()

    def test_node_combo_populates_from_db_nodes(self):
        from hazop import HAZOPWorksheet

        ids1 = self._make_full_chain(node_name="Nod A")
        ids2 = self._make_full_chain(node_name="Nod B")

        ws = HAZOPWorksheet(self.db)
        try:
            self.assertEqual(ws._node_combo.count(), 2)
            self.assertEqual(ws._node_combo.itemText(0), "Nod A")
            self.assertEqual(ws._node_combo.itemData(0), ids1['node_id'])
            self.assertEqual(ws._node_combo.itemText(1), "Nod B")
            self.assertEqual(ws._node_combo.itemData(1), ids2['node_id'])
        finally:
            ws.deleteLater()

    def test_selecting_combo_entry_calls_load_node_with_right_id(self):
        from hazop import HAZOPWorksheet

        ids1 = self._make_full_chain(node_name="Nod A")
        ids2 = self._make_full_chain(node_name="Nod B")

        ws = HAZOPWorksheet(self.db)
        try:
            # "Visa samtliga noder" now defaults to checked (2026-08-26) —
            # uncheck it first so combo selection actually drives
            # load_node() again, same precondition this test always
            # assumed, just no longer the construction-time default.
            ws._all_nodes_cb.setChecked(False)
            ws._table_panel.load_node = unittest.mock.Mock()
            ws._node_combo.setCurrentIndex(1)
            ws._table_panel.load_node.assert_called_once_with(ids2['node_id'])

            ws._table_panel.load_node.reset_mock()
            ws._node_combo.setCurrentIndex(0)
            ws._table_panel.load_node.assert_called_once_with(ids1['node_id'])
        finally:
            ws.deleteLater()

    def test_checking_all_nodes_disables_combo_and_calls_load_all(self):
        from hazop import HAZOPWorksheet

        self._make_full_chain(node_name="Nod A")
        ids2 = self._make_full_chain(node_name="Nod B")

        ws = HAZOPWorksheet(self.db)
        try:
            # "Visa samtliga noder" now defaults to checked (2026-08-26) —
            # start from unchecked so the checked/unchecked transitions
            # below actually fire toggled (Qt only emits it on a real
            # value change) and exercise the same wiring this test always
            # meant to cover.
            ws._all_nodes_cb.setChecked(False)
            ws._node_combo.setCurrentIndex(1)  # select "Nod B" first
            ws._table_panel.load_node = unittest.mock.Mock()
            ws._table_panel.load_all = unittest.mock.Mock()

            ws._all_nodes_cb.setChecked(True)
            self.assertFalse(ws._node_combo.isEnabled(),
                              "combo must be disabled while 'Visa samtliga noder' is checked")
            ws._table_panel.load_all.assert_called_once()
            ws._table_panel.load_node.assert_not_called()

            ws._table_panel.load_all.reset_mock()
            ws._all_nodes_cb.setChecked(False)
            self.assertTrue(ws._node_combo.isEnabled(),
                             "combo must be re-enabled after unchecking")
            ws._table_panel.load_node.assert_called_once_with(ids2['node_id'])
        finally:
            ws.deleteLater()

    def test_worksheet_refresh_respects_all_nodes_checkbox(self):
        """refresh() (called by MainWindow._switch_view on page==1) must
        re-load in whichever mode the checkbox currently reflects."""
        from hazop import HAZOPWorksheet

        self._make_full_chain(node_name="Nod A")

        ws = HAZOPWorksheet(self.db)
        try:
            ws._all_nodes_cb.setChecked(True)
            ws._table_panel.load_all = unittest.mock.Mock()
            ws._table_panel.load_node = unittest.mock.Mock()

            ws.refresh()
            ws._table_panel.load_all.assert_called_once()
            ws._table_panel.load_node.assert_not_called()
        finally:
            ws.deleteLater()

    def test_enter_created_item_is_reloaded_and_selected(self):
        from hazop import HAZOPWorksheet, CONS_T

        self._make_full_chain(node_name="Nod A")
        ws = HAZOPWorksheet(self.db)
        try:
            ws._table_panel.load_all = unittest.mock.Mock()
            ws._table_panel.select_item = unittest.mock.Mock()
            ws._on_new_item_created(CONS_T, 123)
            ws._table_panel.load_all.assert_called_once()
            ws._table_panel.select_item.assert_called_once_with(CONS_T, 123)
        finally:
            ws.deleteLater()

    def test_show_empty_dev_checkbox_calls_set_show_empty_deviations(self):
        """The 'Visa avvikelser utan orsaker' checkbox must be wired directly
        to the embedded ScenarioTablePanel's set_show_empty_deviations(bool).

        The signal is connected straight to the bound method at construction
        time (`toggled.connect(self._table_panel.set_show_empty_deviations)`),
        so a plain attribute-patch after construction would not intercept the
        already-connected Qt slot. Verify the wiring by its real effect: the
        panel's underlying flag (and the resulting row set) instead.
        """
        from hazop import HAZOPWorksheet

        ids = self._make_full_chain(node_name="Nod A")
        # Give the node a second, cause-less deviation so toggling the
        # checkbox has an observable effect on the row count too.
        self.db.add_deviation(ids['node_id'], description="Tom avvikelse")

        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()  # populate combo + load the node into _table_panel
            # "Visa avvikelser utan orsaker" now defaults to checked
            # (2026-08-26) — uncheck it first so the check/uncheck
            # transitions below start from the same known baseline this
            # test always assumed.
            ws._show_empty_dev_cb.setChecked(False)
            self.assertFalse(ws._table_panel._show_empty_deviations)
            rows_before = ws._table_panel._table.rowCount()

            ws._show_empty_dev_cb.setChecked(True)
            self.assertTrue(ws._table_panel._show_empty_deviations,
                "checking the box must call set_show_empty_deviations(True) "
                "on the embedded ScenarioTablePanel")
            self.assertGreater(ws._table_panel._table.rowCount(), rows_before,
                "the empty deviation must now show as a placeholder row")

            ws._show_empty_dev_cb.setChecked(False)
            self.assertFalse(ws._table_panel._show_empty_deviations,
                "unchecking the box must call set_show_empty_deviations(False)")
            self.assertEqual(ws._table_panel._table.rowCount(), rows_before)
        finally:
            ws.deleteLater()

    def test_all_nodes_checkbox_defaults_to_checked_and_loads_all(self):
        """"I Worksheet ska rutorna visa samtliga noder som standard."
        (2026-08-26) — both the checkbox state AND the actual effect
        (load_all(), not load_node()) must hold from construction, not
        just the checkbox's own visual state."""
        from hazop import HAZOPWorksheet
        self._make_full_chain(node_name="Nod A")
        self._make_full_chain(node_name="Nod B")

        ws = HAZOPWorksheet(self.db)
        try:
            self.assertTrue(ws._all_nodes_cb.isChecked())
            self.assertFalse(ws._node_combo.isEnabled(),
                "combo must start disabled -- 'Visa samtliga noder' is on by default")
            self.assertTrue(ws._table_panel._all_nodes,
                "the embedded ScenarioTablePanel must actually be in "
                "load_all() mode from construction, not just have a "
                "checked-looking checkbox")
        finally:
            ws.deleteLater()

    def test_show_empty_deviations_checkbox_defaults_to_checked(self):
        """"Inställningen visa orsaker utan avvikelser ska vara ikryssad
        som default." (2026-08-26, user's own wording reversed from the
        actual 'Visa avvikelser utan orsaker' checkbox label -- same,
        only existing such checkbox in the Worksheet)."""
        from hazop import HAZOPWorksheet
        ids = self._make_full_chain(node_name="Nod A")
        self.db.add_deviation(ids['node_id'], description="Tom avvikelse")

        ws = HAZOPWorksheet(self.db)
        try:
            self.assertTrue(ws._show_empty_dev_cb.isChecked())
            self.assertTrue(ws._table_panel._show_empty_deviations,
                "must actually be applied to the embedded ScenarioTablePanel "
                "from construction, not just the checkbox's visual state")
        finally:
            ws.deleteLater()

    def test_deviation_column_always_visible_regardless_of_checkboxes(self):
        """The Avvikelse column must stay visible in the Worksheet even with
        both 'Visa samtliga noder' and 'Visa avvikelser utan orsaker'
        unchecked — there's no separate deviation-picker, only a node
        dropdown, so rows need the Avvikelse column to stay distinguishable."""
        from hazop import HAZOPWorksheet
        from hazop import ScenarioTablePanel

        self._make_full_chain(node_name="Nod A")

        ws = HAZOPWorksheet(self.db)
        try:
            # Both checkboxes now default to checked (2026-08-26) — force
            # the "neither checked" scenario this test is actually about.
            ws._all_nodes_cb.setChecked(False)
            ws._show_empty_dev_cb.setChecked(False)
            ws.refresh()
            self.assertFalse(ws._all_nodes_cb.isChecked())
            self.assertFalse(ws._show_empty_dev_cb.isChecked())
            self.assertFalse(
                ws._table_panel._table.isColumnHidden(ScenarioTablePanel._C_DEV),
                "Avvikelse column must be visible with neither checkbox checked")

            # Must also stay visible through mode changes (all-nodes on/off).
            ws._all_nodes_cb.setChecked(True)
            self.assertFalse(
                ws._table_panel._table.isColumnHidden(ScenarioTablePanel._C_DEV))
            ws._all_nodes_cb.setChecked(False)
            self.assertFalse(
                ws._table_panel._table.isColumnHidden(ScenarioTablePanel._C_DEV))
        finally:
            ws.deleteLater()

    def test_main_pid_scenario_panel_dev_column_unaffected(self):
        """always_show_deviation_column() is opt-in per instance — a plain
        ScenarioTablePanel (as used standalone on the P&ID page) must keep
        its original hide-unless-all-nodes behavior for the Avvikelse column."""
        from pid_viewer import PIDPanel  # noqa: F401  (ensures hazop module fully loaded)
        from hazop import ScenarioTablePanel

        panel = ScenarioTablePanel(self.db)
        try:
            self.assertTrue(panel._table.isColumnHidden(ScenarioTablePanel._C_DEV),
                "a plain ScenarioTablePanel must still hide Avvikelse by default")
        finally:
            panel.deleteLater()

    def test_utrustning_column_stays_hidden_with_forced_dev_column_in_single_node_view(self):
        """Reported feedback: the leftmost "Utrustning" column duplicates
        the tag already shown at the top of each Orsak cell. It used to
        follow Avvikelse's forced-visible state (always_show_deviation_column())
        even in single-node view — now it only appears in genuine "all
        nodes" mode, where multiple equipment groups are actually
        interleaved and the column earns its keep."""
        from hazop import ScenarioTablePanel

        node_id = self.db.add_node()
        deviation_id = self.db.deviations(node_id)[0]['id']
        self.db.add_cause(deviation_id)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.always_show_deviation_column()
            panel.load_node(node_id)   # single-node view, _all_nodes=False

            self.assertFalse(panel._table.isColumnHidden(panel._C_DEV),
                "Avvikelse must still be forced visible")
            self.assertTrue(panel._table.isColumnHidden(panel._C_UTR),
                "Utrustning must stay hidden in single-node view even when forced")

            panel._all_nodes = True
            panel._set_all_nodes_columns_visible(True)
            self.assertTrue(panel._table.isColumnHidden(panel._C_UTR),
                "Utrustning must remain retired in genuine all-nodes mode")
        finally:
            panel.deleteLater()

    def test_sticky_ctx_bar_hidden_when_dev_column_forced_visible(self):
        """The sticky context bar (which shows 'current Nod + Avvikelse' as a
        text header) duplicates the now-always-visible Avvikelse column in
        the Worksheet -- both showed Nod/Avvikelse on their own row, wasting
        vertical space. Once always_show_deviation_column() is in effect,
        the context bar must stay hidden, matching the existing "all nodes"
        mode reasoning (the visible column already shows the same info)."""
        from hazop import HAZOPWorksheet

        node_id = self.db.add_node()
        self.db.conn.execute("UPDATE nodes SET name=? WHERE id=?", ("Nod A", node_id))
        self.db.commit()
        deviation_id = self.db.deviations(node_id)[0]['id']
        self.db.add_cause(deviation_id)

        ws = HAZOPWorksheet(self.db)
        try:
            ws.refresh()  # loads the node into the embedded ScenarioTablePanel
            self.assertFalse(
                ws._table_panel._table.isColumnHidden(ws._table_panel._C_DEV),
                "sanity check: Avvikelse column must be visible in Worksheet")
            self.assertFalse(
                ws._table_panel._ctx_bar.isVisible(),
                "the sticky context bar must be hidden once the Avvikelse "
                "column is force-visible -- otherwise Nod/Avvikelse are "
                "shown redundantly on two separate rows")
        finally:
            ws.deleteLater()




if __name__ == "__main__":
    unittest.main()
