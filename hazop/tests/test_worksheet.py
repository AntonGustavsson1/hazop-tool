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
