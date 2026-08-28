#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering scenario_panel.py, plus any cross-module glue they
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
    QTableWidgetItem,
)
from PyQt6.QtGui import QPixmap, QFocusEvent  # noqa: E402
from PyQt6.QtCore import (Qt, QPoint, QDate, QEvent, QThread, pyqtSignal,
                          QItemSelectionModel)  # noqa: E402
from equipment_detection import COMPONENT_TYPES  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# Shared QApplication — Qt only allows one per process.
# ══════════════════════════════════════════════════════════════════════════


from test_helpers import (
    _ensure_qapp, _menu_action_labels, _fake_pdf_loaded,
    _TempDbMainWindow, _find_tree_item, count_selects,
)

# ══════════════════════════════════════════════════════════════════════════
# 6. Worksheet page: ScenarioTablePanel "all nodes" mode + HAZOPWorksheet
#    node-picker/checkbox wiring (feature: Worksheet mirrors the full HAZOP
#    hierarchy per node, or the whole study at once).
# ══════════════════════════════════════════════════════════════════════════

class ScenarioTablePanelAllNodesTests(unittest.TestCase):
    """ScenarioTablePanel.load_all() must show every node's full
    deviation/cause/consequence/safeguard hierarchy concatenated, without
    disturbing the existing single-filter load_node/load_deviation/load_cause/
    load_consequence behaviour (that class is shared with the main P&ID
    page's scenario_panel, so this must be additive only).
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_allnodes_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

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

    def test_load_all_does_not_crash_and_spans_multiple_nodes(self):
        from hazop import ScenarioTablePanel

        ids1 = self._make_full_chain(node_name="Nod A")
        ids2 = self._make_full_chain(node_name="Nod B")

        panel = ScenarioTablePanel(self.db)
        try:
            try:
                panel.load_all()
            except Exception as e:
                self.fail(f"load_all() raised: {e!r}")

            self.assertTrue(panel._all_nodes)
            cause_ids_in_rows = {meta[1] for meta in panel._row_meta if meta[1] is not None}
            self.assertIn(ids1['cause_id'], cause_ids_in_rows,
                          "load_all() rows must include node A's cause")
            self.assertIn(ids2['cause_id'], cause_ids_in_rows,
                          "load_all() rows must include node B's cause")

            cons_ids_in_rows = {meta[2] for meta in panel._row_meta if meta[2] is not None}
            self.assertIn(ids1['cons_id'], cons_ids_in_rows)
            self.assertIn(ids2['cons_id'], cons_ids_in_rows)

            # NOD/DEV columns must become visible in all-nodes mode (multiple
            # nodes are interleaved, so the sticky header-bar shorthand no
            # longer applies).
            self.assertFalse(panel._table.isColumnHidden(panel._C_NOD))
            self.assertFalse(panel._table.isColumnHidden(panel._C_DEV))
        finally:
            panel.deleteLater()

    def test_load_all_on_empty_db_does_not_crash(self):
        """No nodes at all yet — load_all() must not raise."""
        from hazop import ScenarioTablePanel

        panel = ScenarioTablePanel(self.db)
        try:
            try:
                panel.load_all()
            except Exception as e:
                self.fail(f"load_all() on an empty study raised: {e!r}")
            self.assertEqual(panel._table.rowCount(), 0)
        finally:
            panel.deleteLater()

    def test_toggle_load_all_then_load_node_then_load_all_again(self):
        """Switching all-nodes -> single-node -> all-nodes must not crash or
        leave stale filter state (each load_* must fully reset the others)."""
        from hazop import ScenarioTablePanel

        ids1 = self._make_full_chain(node_name="Nod A")
        ids2 = self._make_full_chain(node_name="Nod B")

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_all()
            self.assertTrue(panel._all_nodes)
            self.assertFalse(panel._table.isColumnHidden(panel._C_NOD))

            panel.load_node(ids1['node_id'])
            self.assertFalse(panel._all_nodes,
                              "load_node() must clear _all_nodes")
            self.assertIsNone(panel._deviation_id)
            self.assertIsNone(panel.cause_id)
            self.assertIsNone(panel._cons_id)
            self.assertTrue(panel._table.isColumnHidden(panel._C_NOD),
                             "NOD column must be hidden again in single-node mode")
            cause_ids_in_rows = {meta[1] for meta in panel._row_meta if meta[1] is not None}
            self.assertIn(ids1['cause_id'], cause_ids_in_rows)
            self.assertNotIn(ids2['cause_id'], cause_ids_in_rows,
                              "load_node() must show only the selected node's causes")

            try:
                panel.load_all()
            except Exception as e:
                self.fail(f"load_all() after load_node() raised: {e!r}")
            self.assertTrue(panel._all_nodes)
            cause_ids_in_rows = {meta[1] for meta in panel._row_meta if meta[1] is not None}
            self.assertIn(ids1['cause_id'], cause_ids_in_rows)
            self.assertIn(ids2['cause_id'], cause_ids_in_rows)
        finally:
            panel.deleteLater()

    def test_single_node_filters_unchanged_by_all_nodes_feature(self):
        """Sanity check that load_node/load_deviation/load_cause/
        load_consequence still behave exactly as single-item filters (the
        critical constraint: ScenarioTablePanel is shared with the main
        P&ID page's scenario_panel, so Part 1 changes must be additive)."""
        from hazop import ScenarioTablePanel

        ids1 = self._make_full_chain(node_name="Nod A")
        self._make_full_chain(node_name="Nod B")

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids1['node_id'])
            self.assertEqual(panel._node_id, ids1['node_id'])
            self.assertFalse(panel._all_nodes)
            rows_node = panel._table.rowCount()
            self.assertGreater(rows_node, 0)

            panel.load_deviation(ids1['deviation_id'])
            self.assertEqual(panel._deviation_id, ids1['deviation_id'])
            self.assertEqual(panel._node_id, ids1['node_id'],
                              "load_deviation() must set _node_id from the deviation's own node_id")
            self.assertFalse(panel._all_nodes)

            panel.load_cause(ids1['cause_id'])
            self.assertEqual(panel.cause_id, ids1['cause_id'])
            self.assertFalse(panel._all_nodes)

            panel.load_consequence(ids1['cons_id'])
            self.assertEqual(panel._cons_id, ids1['cons_id'])
            self.assertFalse(panel._all_nodes)

            panel.clear()
            self.assertFalse(panel._all_nodes)
            self.assertEqual(panel._table.rowCount(), 0)
        finally:
            panel.deleteLater()


    def test_grouped_deviation_keeps_one_visible_number(self):
        from hazop import ScenarioTablePanel

        node_id = self.db.add_node()
        first_dev = self.db.get_or_create_deviation(node_id, "LÃ¥gt flÃ¶de")
        eq_id = self.db.add_equipment_item(
            "V-2", "V-2", "V", 0, "Ventil", '', 0)
        sibling_dev = self.db.add_deviation(
            node_id, "LÃ¥gt flÃ¶de", equipment_id=eq_id)
        panel = ScenarioTablePanel(self.db)
        try:
            first_number = panel._deviation_number(node_id, first_dev)
            self.assertEqual(
                panel._deviation_number(node_id, sibling_dev), first_number)
        finally:
            panel.deleteLater()


class ScenarioTablePanelShowEmptyDeviationsTests(unittest.TestCase):
    """ScenarioTablePanel.set_show_empty_deviations(): deviations with zero
    causes are silently omitted by default (_causes_for_node's normal
    behaviour). When the flag is on, each such deviation must get its own
    placeholder row (via _add_placeholder_row), interleaved in deviation
    order alongside deviations that do have causes — not just in the
    "whole node/study is empty" fallback branch of _build_rows()."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_showempty_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_node_with_one_cause_and_empty_deviations(self, node_name=None):
        """add_node() auto-creates several standard deviations. Give only
        the FIRST one a cause (+consequence+safeguard); leave the rest
        (at least one more, per the standard deviation set) with zero
        causes."""
        node_id = self.db.add_node()
        if node_name is not None:
            self.db.conn.execute(
                "UPDATE nodes SET name=? WHERE id=?", (node_name, node_id))
            self.db.commit()
        devs = self.db.deviations(node_id)
        self.assertGreaterEqual(len(devs), 2,
            "test assumes add_node() creates >=2 standard deviations")
        dev_with_cause = devs[0]['id']
        empty_dev_ids = [d['id'] for d in devs[1:]]
        cause_id = self.db.add_cause(dev_with_cause)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        return {
            'node_id': node_id,
            'dev_with_cause': dev_with_cause,
            'empty_dev_ids': empty_dev_ids,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_default_off_omits_empty_deviations(self):
        from hazop import ScenarioTablePanel

        ids = self._make_node_with_one_cause_and_empty_deviations("Nod A")
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids['node_id'])
            self.assertFalse(panel._show_empty_deviations)
            dev_ids_in_rows = {meta[0] for meta in panel._row_meta}
            self.assertIn(ids['dev_with_cause'], dev_ids_in_rows)
            for empty_id in ids['empty_dev_ids']:
                self.assertNotIn(empty_id, dev_ids_in_rows,
                    "empty deviations must be omitted when the flag is off")
        finally:
            panel.deleteLater()

    def test_enabling_flag_adds_placeholder_rows_for_empty_deviations(self):
        from hazop import ScenarioTablePanel

        ids = self._make_node_with_one_cause_and_empty_deviations("Nod A")
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids['node_id'])
            rows_before = panel._table.rowCount()

            panel.set_show_empty_deviations(True)
            self.assertTrue(panel._show_empty_deviations)

            rows_after = panel._table.rowCount()
            self.assertGreater(rows_after, rows_before,
                "turning the flag on must add rows for the empty deviations")

            dev_ids_in_rows = [meta[0] for meta in panel._row_meta]
            self.assertIn(ids['dev_with_cause'], dev_ids_in_rows)
            for empty_id in ids['empty_dev_ids']:
                self.assertIn(empty_id, dev_ids_in_rows,
                    "empty deviations must appear as placeholder rows when the flag is on")

            # The placeholder row(s) for empty deviations must carry None for
            # cause/cons/sg ids in _row_meta.
            for i, meta in enumerate(panel._row_meta):
                if meta[0] in ids['empty_dev_ids']:
                    self.assertIsNone(meta[1], "placeholder row must have cause_id=None")
                    self.assertIsNone(meta[2], "placeholder row must have cons_id=None")
                    self.assertIsNone(meta[3], "placeholder row must have sg_id=None")
        finally:
            panel.deleteLater()

    def test_works_in_all_nodes_mode_across_multiple_nodes(self):
        from hazop import ScenarioTablePanel

        ids1 = self._make_node_with_one_cause_and_empty_deviations("Nod A")
        ids2 = self._make_node_with_one_cause_and_empty_deviations("Nod B")

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_all()
            panel.set_show_empty_deviations(True)
            self.assertTrue(panel._all_nodes)
            self.assertTrue(panel._show_empty_deviations)

            dev_ids_in_rows = [meta[0] for meta in panel._row_meta]
            for empty_id in ids1['empty_dev_ids'] + ids2['empty_dev_ids']:
                self.assertIn(empty_id, dev_ids_in_rows,
                    "empty deviations from every node must appear in all-nodes mode")
            self.assertIn(ids1['dev_with_cause'], dev_ids_in_rows)
            self.assertIn(ids2['dev_with_cause'], dev_ids_in_rows)
        finally:
            panel.deleteLater()

    def test_toggling_flag_on_and_off_does_not_crash_and_changes_row_count(self):
        from hazop import ScenarioTablePanel

        ids = self._make_node_with_one_cause_and_empty_deviations("Nod A")
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids['node_id'])
            rows_off = panel._table.rowCount()

            try:
                panel.set_show_empty_deviations(True)
            except Exception as e:
                self.fail(f"set_show_empty_deviations(True) raised: {e!r}")
            rows_on = panel._table.rowCount()
            self.assertGreater(rows_on, rows_off)

            try:
                panel.set_show_empty_deviations(False)
            except Exception as e:
                self.fail(f"set_show_empty_deviations(False) raised: {e!r}")
            rows_off_again = panel._table.rowCount()
            self.assertEqual(rows_off_again, rows_off)

            # Calling with the same value again must be a no-op (early return)
            # and must not raise.
            try:
                panel.set_show_empty_deviations(False)
            except Exception as e:
                self.fail(f"set_show_empty_deviations(False) again raised: {e!r}")
        finally:
            panel.deleteLater()

    def test_flag_persists_across_load_node_switches(self):
        """This is a display PREFERENCE (like font size / 'Fyll skärm'), not
        a per-node filter, so switching nodes must not reset it."""
        from hazop import ScenarioTablePanel

        ids1 = self._make_node_with_one_cause_and_empty_deviations("Nod A")
        ids2 = self._make_node_with_one_cause_and_empty_deviations("Nod B")

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids1['node_id'])
            panel.set_show_empty_deviations(True)

            panel.load_node(ids2['node_id'])
            self.assertTrue(panel._show_empty_deviations,
                "load_node() must not reset the show-empty-deviations preference")
            dev_ids_in_rows = [meta[0] for meta in panel._row_meta]
            for empty_id in ids2['empty_dev_ids']:
                self.assertIn(empty_id, dev_ids_in_rows)
        finally:
            panel.deleteLater()

    def test_clear_resets_the_flag(self):
        """clear() is a full state reset (unlike the load_* switches), so it
        should reset this preference along with the other filter state."""
        from hazop import ScenarioTablePanel

        ids1 = self._make_node_with_one_cause_and_empty_deviations("Nod A")

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids1['node_id'])
            panel.set_show_empty_deviations(True)
            panel.clear()
            self.assertFalse(panel._show_empty_deviations)
        finally:
            panel.deleteLater()


class ResizeRowsManualNoNativeCrashTests(unittest.TestCase):
    """_resize_rows() used to call QTableWidget.resizeRowsToContents(), which
    was pinpointed via diagnostic K0/K1 checkpoint logging (commit 2aba0b4)
    as the exact site of a silent native (C++-level) crash: the process died
    inside that call with no Python exception, after several rapid rebuild
    cycles in quick succession (e.g. the Worksheet node-picker dropdown being
    switched quickly between nodes). Elsewhere in this suite,
    scenario_panel.load_deviation()/load_consequence()/load_cause() are
    stubbed out specifically to dodge this same native crash (see
    test_select_safeguard_in_tree_no_crash's docstring), which is exactly
    why this class instead calls the real, un-stubbed load_node() repeatedly.

    The fix (this session) replaces resizeRowsToContents() with a manual
    per-row/per-cell height computation in _resize_rows_manual(), never
    invoking the native call at all. These tests exercise that new code path
    directly and would have reproduced the native crash before the fix.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_resize_rows_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain(self, node_name, long_text=False):
        """Build node -> deviation -> cause -> consequence -> safeguard,
        optionally with long cause/consequence text to force the ORS/KON
        wrapping-height computation path in _resize_rows_manual()."""
        node_id = self.db.add_node()
        self.db.conn.execute(
            "UPDATE nodes SET name=? WHERE id=?", (node_name, node_id))
        self.db.commit()
        deviation_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(deviation_id)
        if long_text:
            self.db.update_cause(
                cause_id,
                description="Mycket lång orsakstext som ska radbrytas flera "
                             "gånger i tabellcellen för att tvinga fram "
                             "höjdberäkning via QFontMetrics.boundingRect " * 3)
        cons_id = self.db.add_consequence(cause_id)
        if long_text:
            self.db.update_consequence(
                cons_id,
                "Mycket lång konsekvensbeskrivning som också radbryts flera "
                "gånger för att övning täcker KON-kolumnens höjdlogik " * 3,
                3, '')
        sg_id = self.db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_rapid_node_switching_does_not_crash_and_row_heights_are_sane(self):
        """Simulates the Worksheet node-picker dropdown being changed rapidly:
        real (un-stubbed) load_node() calls across several nodes, each with a
        full deviation/cause/consequence/safeguard chain and long wrapping
        text, repeated enough times to meaningfully exercise the manual
        row-height computation added in this fix."""
        from hazop import ScenarioTablePanel

        node_ids = []
        for i in range(3):
            ids = self._make_full_chain(f"Nod {i}", long_text=True)
            node_ids.append(ids['node_id'])

        panel = ScenarioTablePanel(self.db)
        try:
            for _cycle in range(10):
                for node_id in node_ids:
                    try:
                        panel.load_node(node_id)
                    except Exception as e:
                        self.fail(
                            f"load_node({node_id}) raised on rapid-switch "
                            f"cycle {_cycle}: {e!r}")

            # Final state sanity: rows exist and every row has a positive,
            # sane height (not 0, not some absurd default).
            row_count = panel._table.rowCount()
            self.assertGreater(row_count, 0)
            for r in range(row_count):
                h = panel._table.rowHeight(r)
                self.assertGreater(h, 0, f"row {r} has non-positive height")
                self.assertLess(h, 2000, f"row {r} has a suspiciously huge height")
        finally:
            panel.deleteLater()

    def test_resize_rows_manual_sizes_all_rows_without_resize_rows_to_contents(self):
        """Directly checks _resize_rows_manual() (the new helper) sizes every
        row to a positive height, for both a single node's chain (typical
        single-node worksheet view) and load_all() (potentially hundreds of
        rows across many nodes)."""
        from hazop import ScenarioTablePanel

        ids1 = self._make_full_chain("Nod A", long_text=True)
        self._make_full_chain("Nod B", long_text=True)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(ids1['node_id'])
            self.assertGreater(panel._table.rowCount(), 0)
            for r in range(panel._table.rowCount()):
                self.assertGreater(panel._table.rowHeight(r), 0)

            try:
                panel.load_all()
            except Exception as e:
                self.fail(f"load_all() raised: {e!r}")
            self.assertGreater(panel._table.rowCount(), 0)
            for r in range(panel._table.rowCount()):
                self.assertGreater(panel._table.rowHeight(r), 0)
        finally:
            panel.deleteLater()


class OrsStripHeightConsistencyTests(unittest.TestCase):
    """'Ibland så göms text på raderna i hazop scenario. särskilt de som
    står under orsaker. Dessutom ser det ut som att en spöktext ligger
    kvar när man redigerar.' (2026-08-11). Root cause: the ORS cell's
    top strip ([pin|tag|freq|dots]) is drawn/positioned at 17px
    (_PidDelegate.paint(), updateEditorGeometry) but the row-height
    calculations (sizeHint/_resize_rows_manual/_wrap_col_row_height —
    including the fast-path _update_row_text_only() that runs right
    after finishing an edit) only ever reserved 14px for it — a
    long-standing, pre-existing mismatch across five places, none of
    which agreed with each other. Every ORS row was silently 3px too
    short for its own wrapped description, clipping the bottom of the
    last line (worse right after an edit, since that's exactly when
    _wrap_col_row_height's wrong number gets freshly (re)applied via
    setRowHeight — the "ghost text" symptom). Fixed by unifying all
    five call sites onto one shared constant (now _ORS_FIRST_LINE_H,
    renamed 2026-08-25 when the separate tag strip these tests describe
    was removed entirely — see NOTES.md "Slå ihop objektbaren i
    Orsak-kolumnen"; the sync-across-five-places PRINCIPLE these tests
    guard is unchanged, only what's being synced)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_orsstrip_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _row_for_cause(self, panel, cause_id):
        return next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

    def _assert_enough_room_for_wrapped_text(self, panel, row):
        from PyQt6.QtGui import QFontMetrics
        item = panel._table.item(row, panel._C_ORS)
        fm = QFontMetrics(panel._table.font())
        cell_w = max(40, panel._table.columnWidth(panel._C_ORS) - 6)
        # 2026-08-25 (see NOTES.md "Slå ihop objektbaren i
        # Orsak-kolumnen"): no more separate tag strip to subtract — the
        # tag (if any) is an inline bold prefix on the SAME wrapped text
        # block now, measured via the same _ors_combined_text() helper
        # paint()/sizeHint() themselves use, so the whole row height must
        # cover the combined text directly.
        combined = panel._ors_combined_text(item, item.text())
        rect = fm.boundingRect(0, 0, cell_w, 10000, Qt.TextFlag.TextWordWrap, combined)
        row_height = panel._table.rowHeight(row)
        self.assertGreaterEqual(
            row_height, rect.height(),
            f"row height {row_height} is not enough for the wrapped tag+"
            f"description text, which needs {rect.height()}px — the last "
            f"line will be clipped")

    def test_row_height_leaves_enough_room_after_initial_load(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(
            cause_id,
            description="En mycket lång orsakstext som ska radbrytas flera "
                        "gånger i cellen för att verkligen tvinga fram en "
                        "flerradig beskrivning under taggremsan " * 3)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = self._row_for_cause(panel, cause_id)
            self._assert_enough_room_for_wrapped_text(panel, row)
        finally:
            panel.deleteLater()

    def test_row_height_leaves_enough_room_after_editing_description(self):
        """The specific "ghost text after editing" report — exercises the
        fast-path _update_row_text_only(), not just the initial-load
        sizing path above."""
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            long_text = ("En mycket lång orsakstext som ska radbrytas flera "
                        "gånger i cellen efter redigering " * 3)
            panel._update_row_text_only('cause', cause_id, long_text)
            row = self._row_for_cause(panel, cause_id)
            self._assert_enough_room_for_wrapped_text(panel, row)
        finally:
            panel.deleteLater()

    def test_very_long_description_is_not_capped_at_four_lines(self):
        """_resize_rows() used to forcibly shrink any row back down to
        ~4 text lines' worth of height even when _resize_rows_manual()
        had already correctly computed a taller one for a longer
        description — silently clipping everything past the 4th line
        with no visual sign anything was cut off. A HAZOP tool hiding
        part of a cause description is a far worse failure mode than a
        tall row, so the cap is gone; this pins that down directly with
        a description long enough to need well over 4 lines."""
        from hazop import ScenarioTablePanel
        from PyQt6.QtGui import QFontMetrics
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(
            cause_id,
            description="En mycket lång orsakstext som garanterat radbryts till "
                        "betydligt fler än fyra rader i cellen " * 8)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = self._row_for_cause(panel, cause_id)
            row_height = panel._table.rowHeight(row)
            fm = QFontMetrics(panel._table.font())
            four_line_cap = fm.height() * 4 + 12
            self.assertGreater(
                row_height, four_line_cap,
                "a description this long must grow the row past the old "
                "4-line cap, not get silently clipped at it")
            self._assert_enough_room_for_wrapped_text(panel, row)
        finally:
            panel.deleteLater()


class SafeguardRowHeightCompactionTests(unittest.TestCase):
    """'krymper höjden på safeguards ... för att spara plats när man
    lägger till flera safeguards' (2026-08-18). Every safeguard under one
    consequence gets its own physical table row (_apply_spans spans
    NOD/UTR/DEV/ORS/KON/LOPA/REK/RFORE/SLUT across them all, only SG has
    real per-row content) — a first version of this feature computed
    "does this row have content of its own in another column" via
    table.item(row, c) is None, which is ALWAYS False: _add_row() gives
    every physical row its own freshly-built item/widget regardless of
    spanning (setSpan/setCellWidget only change how Qt paints covered
    cells). That made the whole feature a silent no-op — every safeguard
    row stayed at full height. Fixed by comparing each column's own span
    key (from _apply_spans) against the previous row instead.

    2026-08-19 follow-up ("Översta safeguarden blir 3 rader lång ...
    kopplad till FA, ant+övriga"): that first fix only stopped counting
    a shared requirement (LOPA's fixed height, the ORS readability
    floor) on non-anchor rows — it still dumped the FULL, undivided
    requirement onto the anchor row alone, so the anchor ended up
    disproportionately tall next to its now-compact siblings instead of
    genuinely evenly sized. Fixed by dividing each shared requirement by
    however many rows its own span covers and applying that share to
    every row in the group — see the tests below and
    _compute_row_height's own docstring."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_sgheight_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_second_and_third_safeguard_rows_are_shorter_than_the_first(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        self.db.add_safeguard(cons_id)
        self.db.add_safeguard(cons_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            rows = [r for r, m in enumerate(panel._row_meta) if m[2] == cons_id]
            self.assertEqual(len(rows), 3)
            heights = [panel._table.rowHeight(r) for r in rows]
            self.assertGreater(heights[0], heights[1],
                "the anchor row (carrying ORS/KON/LOPA's real content) must stay tall")
            self.assertEqual(heights[1], heights[2],
                "every safeguard-only continuation row must compact to the same height")
            self.assertLess(heights[1], heights[0],
                "a pure safeguard continuation row must actually shrink, not silently "
                "stay at full height (the original no-op bug)")
        finally:
            panel.deleteLater()

    def test_a_single_safeguard_is_not_compacted(self):
        """A cause with exactly one safeguard has no continuation rows at
        all — its one physical row IS the anchor and must keep full
        height, not be mistaken for a compactable continuation."""
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            fm_h_plus_4 = panel._sg_row_height(panel._table.font())
            self.assertGreater(panel._table.rowHeight(row), fm_h_plus_4,
                "a lone safeguard row must not be compacted down to the SG-only floor")
        finally:
            panel.deleteLater()

    def test_a_new_cause_right_after_a_multi_safeguard_block_is_not_compacted(self):
        """A fresh cause/deviation immediately following a multi-safeguard
        block must get its own full-height anchor row — no leftover state
        from the previous block's continuation rows should leak forward."""
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        devs = self.db.deviations(node_id)
        cause1 = self.db.add_cause(devs[0]['id'])
        cons1 = self.db.add_consequence(cause1)
        self.db.add_safeguard(cons1)
        self.db.add_safeguard(cons1)
        cause2 = self.db.add_cause(devs[1]['id'])
        cons2 = self.db.add_consequence(cause2)
        self.db.add_safeguard(cons2)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row2 = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons2)
            fm_h_plus_4 = panel._sg_row_height(panel._table.font())
            self.assertGreater(panel._table.rowHeight(row2), fm_h_plus_4,
                "the new cause's own safeguard row must not inherit compaction from "
                "the previous, unrelated cause's continuation rows")
        finally:
            panel.deleteLater()

    def test_first_safeguard_row_is_not_disproportionately_tall(self):
        """2026-08-19 follow-up: with a short cause description, the
        anchor row's ORS/LOPA requirements are small enough that once
        divided across the group they should settle close to the
        compact continuation rows' own height — not the old ~2.5x
        (52px vs 20px) mismatch that looked like "3 rows" next to "1
        row"."""
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        self.db.add_safeguard(cons_id)
        self.db.add_safeguard(cons_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            rows = [r for r, m in enumerate(panel._row_meta) if m[2] == cons_id]
            heights = [panel._table.rowHeight(r) for r in rows]
            self.assertLessEqual(heights[0], heights[1] + 4,
                "the anchor row must not be dramatically taller than its compact "
                "siblings just because LOPA/the ORS floor happened to land there")
        finally:
            panel.deleteLater()

    def test_long_description_total_height_preserved_across_safeguard_group(self):
        """The shared-requirement distribution must never UNDER-provision
        space — dividing a long description's wrapped-text height across
        several safeguard rows must still sum to at least what a single
        safeguard with the SAME text would need, or the description's
        later lines would silently clip (exactly the 2026-08-11 bug this
        session's other fixes were careful not to reintroduce)."""
        from hazop import ScenarioTablePanel
        long_text = ("Detta är en mycket lång orsaksbeskrivning som ska "
                     "wrappa över flera rader i cellen. " * 8).strip()
        node_id = self.db.add_node()
        devs = self.db.deviations(node_id)

        cause_multi = self.db.add_cause(devs[0]['id'])
        self.db.update_cause(cause_multi, description=long_text)
        cons_multi = self.db.add_consequence(cause_multi)
        self.db.add_safeguard(cons_multi)
        self.db.add_safeguard(cons_multi)
        self.db.add_safeguard(cons_multi)

        cause_single = self.db.add_cause(devs[1]['id'])
        self.db.update_cause(cause_single, description=long_text)
        cons_single = self.db.add_consequence(cause_single)
        self.db.add_safeguard(cons_single)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            multi_total = sum(panel._table.rowHeight(r)
                              for r, m in enumerate(panel._row_meta) if m[2] == cons_multi)
            single_total = sum(panel._table.rowHeight(r)
                               for r, m in enumerate(panel._row_meta) if m[2] == cons_single)
            self.assertGreaterEqual(multi_total, single_total,
                "the 3-safeguard group's TOTAL spanned height must still fit the same "
                "long description a single safeguard with identical text gets")
        finally:
            panel.deleteLater()


class ConsequenceStepPickerColumnsTests(unittest.TestCase):
    """Regression tests for ConsequenceStepPickerDialog's multi-column
    layout (all _N_STEPS 'Del N' columns shown side by side, replacing an
    intermediate one-step-at-a-time wizard redesign).

    The data model (_CONSEQ_NODES / _CONSEQ_ENTRY / _CONSEQ_GENERIC_NEXT,
    _successor_pairs, _resolve, Database.set_consequence_steps /
    get_consequence_steps) is unchanged across all these presentation
    changes — these tests confirm the all-columns-visible presentation
    still drives that model correctly and that the persistence format is
    unchanged.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_stepcolumns_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_chain(self, deviation="Lågt flöde", comp_type="Pump"):
        node_id = self.db.add_node()
        dev_id = self.db.add_deviation(node_id, deviation)
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        return node_id, dev_id, cause_id, cons_id

    def test_col1_options_match_entry_pairs_count(self):
        """Del1's list must show exactly _entry_pairs()'s result count of
        items -- not a hardcoded 5, since node option counts vary 0-6."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            expected = dlg._entry_pairs()
            self.assertGreater(len(expected), 0)
            self.assertEqual(dlg._cols[0]['list'].count(), len(expected))
            self.assertFalse(dlg._cols[0]['list'].isHidden())
            self.assertTrue(dlg._cols[0]['end_lbl'].isHidden())
            for (key, text), i in zip(expected, range(len(expected))):
                self.assertIn(dlg._resolve(text, ''), dlg._cols[0]['list'].item(i).text())
        finally:
            dlg.deleteLater()

    def test_other_columns_start_neutral_not_terminal(self):
        """Columns 2-5 haven't been reached yet on a fresh dialog -- they
        must show a neutral empty list, NOT the 'chain ends here' message
        (that message is reserved for an actually-terminal graph node)."""
        from hazop import ConsequenceStepPickerDialog, _N_STEPS
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            for i in range(1, _N_STEPS):
                self.assertEqual(dlg._cols[i]['list'].count(), 0)
                self.assertFalse(dlg._cols[i]['list'].isHidden())
                self.assertTrue(dlg._cols[i]['end_lbl'].isHidden())
        finally:
            dlg.deleteLater()

    def test_clicking_option_populates_next_column_with_successor_pairs(self):
        """Clicking an option in Del1 must populate Del2 with exactly the
        pairs _successor_pairs() returns for the chosen node."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            dlg._cols[0]['list'].setCurrentRow(0)
            dlg._list_clicked(0, dlg._cols[0]['list'])
            chosen_key = dlg._opt_keys[0][0]
            expected = dlg._successor_pairs(chosen_key)
            self.assertEqual(dlg._options[1], [t for _, t in expected])
            self.assertEqual(dlg._opt_keys[1], [k for k, _ in expected])
        finally:
            dlg.deleteLater()

    def test_second_click_deselects_and_clears_downstream(self):
        """Clicking an already-selected option a second time must clear the
        selection and cascade the clear downstream (no stale successors
        left over from the previous choice)."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            dlg._cols[0]['list'].setCurrentRow(0)
            dlg._list_clicked(0, dlg._cols[0]['list'])
            self.assertGreaterEqual(dlg._cols[0]['sel'], 0)
            self.assertGreater(len(dlg._options[1]), 0)

            dlg._cols[0]['list'].setCurrentRow(0)
            dlg._list_clicked(0, dlg._cols[0]['list'])
            self.assertEqual(dlg._cols[0]['sel'], -1)
            self.assertEqual(dlg._options[1], [])
            self.assertFalse(dlg._cols[1]['list'].isHidden())
            self.assertTrue(dlg._cols[1]['end_lbl'].isHidden())
        finally:
            dlg.deleteLater()

    def test_terminal_node_shows_end_of_chain_message_not_empty_list(self):
        """Reaching a 0-next node (e.g. 'fatality') must show the
        end-of-chain message and hide the (empty) list for that column."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            # 'fatality' is a real terminal node in _CONSEQ_NODES (next=[]).
            self.assertEqual(dlg._successor_pairs('fatality'), [])
            dlg._populate_column(1, dlg._successor_pairs('fatality'), upstream_has_sel=True)
            self.assertTrue(dlg._cols[1]['list'].isHidden())
            self.assertFalse(dlg._cols[1]['end_lbl'].isHidden())
            self.assertIn('Kedjan slutar här', dlg._cols[1]['end_lbl'].text())
        finally:
            dlg.deleteLater()

    def test_freetext_entry_cascades_using_generic_pairs(self):
        """Typing free text in a column (instead of picking an option) must
        populate the next column from _generic_pairs(), matching the
        original fallback behavior."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            dlg._cols[0]['ft_edit'].setText("Eget alternativ")
            expected = dlg._generic_pairs()
            self.assertEqual(dlg._options[1], [t for _, t in expected])
            self.assertIsNone(dlg._selected_key(0))
        finally:
            dlg.deleteLater()

    def test_save_produces_same_node_keys_as_direct_graph_walk(self):
        """Saving must persist the same node_key values (and text) that a
        direct walk of the graph would produce."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            key0 = dlg._opt_keys[0][0]
            expected_text0 = dlg._resolve(dlg._options[0][0], '')
            dlg._cols[0]['list'].setCurrentRow(0)
            dlg._list_clicked(0, dlg._cols[0]['list'])

            key1 = dlg._opt_keys[1][0]
            expected_text1 = dlg._resolve(dlg._options[1][0], '')
            dlg._cols[1]['list'].setCurrentRow(0)
            dlg._list_clicked(1, dlg._cols[1]['list'])

            dlg._do_save()
            saved = self.db.get_consequence_steps(cons_id)
            self.assertEqual(len(saved), 2)
            self.assertEqual(saved[0]['step'], 1)
            self.assertEqual(saved[0]['node_key'], key0)
            self.assertEqual(saved[0]['text'], expected_text0)
            self.assertEqual(saved[1]['step'], 2)
            self.assertEqual(saved[1]['node_key'], key1)
            self.assertEqual(saved[1]['text'], expected_text1)

            cons = self.db.get_consequence(cons_id)
            self.assertIn(expected_text0, cons['description'])
            self.assertIn(expected_text1, cons['description'])
        finally:
            dlg.deleteLater()

    def test_reopening_dialog_restores_saved_chain_selection(self):
        """A saved chain (node_key based) must be restored selection-by-
        selection when the dialog is reopened."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg1 = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        key0 = dlg1._opt_keys[0][0]
        dlg1._cols[0]['list'].setCurrentRow(0)
        dlg1._list_clicked(0, dlg1._cols[0]['list'])
        dlg1._do_save()
        dlg1.deleteLater()

        dlg2 = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            self.assertEqual(dlg2._cols[0]['sel'], 0)
            self.assertEqual(dlg2._cols[0]['list'].currentRow(), 0)
            self.assertEqual(dlg2._opt_keys[0][dlg2._cols[0]['sel']], key0)
        finally:
            dlg2.deleteLater()

    def test_pin_button_flow_refills_ref_tag_for_column(self):
        """The ref-tag pin-button flow (_request_pick_for_col hides the
        dialog and waits; the caller fills the waiting column's ref_edit
        and re-shows) must update that column's live, always-mounted
        ref_edit widget and cascade the list label refresh."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            dlg._request_pick_for_col(1)
            self.assertEqual(dlg._waiting_col_idx, 1)
            self.assertTrue(dlg.isHidden())

            # Simulate MainWindow._on_ref_tag_picked's refill + re-show.
            col_idx = dlg._waiting_col_idx
            dlg._cols[col_idx]['ref_edit'].setText("T-101")
            dlg._waiting_col_idx = None
            dlg.show()

            self.assertIsNone(dlg._waiting_col_idx)
            self.assertEqual(dlg._cols[1]['ref_edit'].text(), "T-101")
        finally:
            dlg.deleteLater()

    def test_quickselect_removed(self):
        """The 'Snabbval' quick-select text field is dropped for a cleaner,
        tighter dialog. Confirm it is gone rather than silently broken."""
        from hazop import ConsequenceStepPickerDialog
        _, _, _, cons_id = self._make_chain()
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id, deviation="Lågt flöde", comp_type="Pump")
        try:
            self.assertFalse(hasattr(dlg, '_apply_quickselect'))
            self.assertFalse(hasattr(dlg, '_qs_edit'))
            self.assertFalse(hasattr(dlg, '_qs_btn'))
        finally:
            dlg.deleteLater()

    def test_dialog_opens_near_scenario_table_row_not_screen_center(self):
        """The dialog must open positioned near its cons_id's row in the
        HAZOP scenario table (ScenarioTablePanel._pos_near_cons_row), not at
        the OS's default screen-centered dialog placement -- per explicit
        user feedback that it should appear "nere vid hazop scenario" (down
        by the scenario table) rather than as a generic centered popup.
        """
        from hazop import ScenarioTablePanel
        node_id, dev_id, cause_id, cons_id = self._make_chain()

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            expected_anchor = panel._table.viewport().mapToGlobal(
                panel._table.visualRect(
                    panel._table.model().index(row, panel._C_KON)).bottomLeft())

            pos = panel._pos_near_cons_row(cons_id, __import__('PyQt6.QtCore', fromlist=['QSize']).QSize(420, 480))

            # Clamped-to-screen position must still originate from the row's
            # anchor point, not an arbitrary screen-center/default position.
            self.assertLessEqual(abs(pos.x() - expected_anchor.x()), 5)
            self.assertLessEqual(abs(pos.y() - expected_anchor.y()), 5)
        finally:
            panel.deleteLater()

    def test_pos_near_cons_row_falls_back_to_cursor_when_row_not_visible(self):
        """If cons_id isn't in the table's current filter scope (e.g. a
        different node/cause is loaded), _pos_near_cons_row() must not raise
        -- it falls back to the cursor position instead of crashing or
        returning a nonsensical location."""
        from hazop import ScenarioTablePanel
        from PyQt6.QtCore import QSize
        _, _, _, cons_id = self._make_chain()

        panel = ScenarioTablePanel(self.db)
        try:
            panel.clear()  # nothing loaded -> _row_meta is empty
            try:
                pos = panel._pos_near_cons_row(cons_id, QSize(420, 480))
            except Exception as e:
                self.fail(f"_pos_near_cons_row() must not raise when the row "
                          f"isn't visible, raised: {e!r}")
            self.assertIsNotNone(pos)
        finally:
            panel.deleteLater()


class ScenarioPanelLoadEquipmentFilterTests(unittest.TestCase):
    """ScenarioTablePanel.load_equipment() — the worksheet-side half of
    'click an equipment marker on P&ID -> show only causes mentioning it'
    (2026-08-12, see NOTES.md)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_load_equip_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.node_id = self.db.add_node()
        self.eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_only_matching_causes_appear_in_row_meta(self):
        from hazop import ScenarioTablePanel
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        matching_cause = self.db.add_cause(dev_id)
        self.db.add_consequence(matching_cause)

        other_dev = self.db.get_or_create_deviation(self.node_id, "Högt flöde")
        unrelated_cause = self.db.add_cause(other_dev)
        self.db.add_consequence(unrelated_cause)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_equipment(self.eq_id)
            cause_ids_shown = {m[1] for m in panel._row_meta if m[1] is not None}
            self.assertEqual(cause_ids_shown, {matching_cause})
        finally:
            panel.deleteLater()

    def test_header_shows_the_equipment_tag(self):
        from hazop import ScenarioTablePanel
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        cause_id = self.db.add_cause(dev_id)
        self.db.add_consequence(cause_id)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_equipment(self.eq_id)
            self.assertIn("PV-101", panel._hdr_lbl.text())
        finally:
            panel.deleteLater()

    def test_empty_result_shows_no_rows_and_no_crash(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_equipment(self.eq_id)   # equipment exists, zero causes mention it
            self.assertEqual(panel._row_meta, [])
            self.assertIn("PV-101", panel._hdr_lbl.text())
        finally:
            panel.deleteLater()

    def test_nod_and_dev_columns_are_visible_like_all_nodes_mode(self):
        from hazop import ScenarioTablePanel
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        cause_id = self.db.add_cause(dev_id)
        self.db.add_consequence(cause_id)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_equipment(self.eq_id)
            self.assertFalse(panel._table.isColumnHidden(panel._C_NOD))
            self.assertFalse(panel._table.isColumnHidden(panel._C_DEV))
        finally:
            panel.deleteLater()

    def test_load_node_after_load_equipment_clears_the_filter(self):
        """Switching back to a normal node view must not leave the
        equipment filter silently still active."""
        from hazop import ScenarioTablePanel
        dev_id = self.db.get_or_create_deviation(self.node_id, "Lågt flöde", equipment_id=self.eq_id)
        cause_id = self.db.add_cause(dev_id)
        self.db.add_consequence(cause_id)
        other_dev = self.db.get_or_create_deviation(self.node_id, "Högt flöde")
        other_cause = self.db.add_cause(other_dev)
        self.db.add_consequence(other_cause)

        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_equipment(self.eq_id)
            panel.load_node(self.node_id)
            self.assertIsNone(panel._equipment_filter_id)
            cause_ids_shown = {m[1] for m in panel._row_meta if m[1] is not None}
            self.assertEqual(cause_ids_shown, {cause_id, other_cause})
        finally:
            panel.deleteLater()


class PlusRowRenderingTests(unittest.TestCase):
    """The "+" quick-add affordance (2026-08-12, see NOTES.md). Originally
    a separate blank row per group; the user rejected that too ("tar upp
    alldeles för mycket plats då de tar hela rader med blankt") and asked
    for a small "+" badge painted in the bottom-right corner of the LAST
    real content row of a group instead, with clicking that badge zone
    inserting a new row only then. `_row_plus_cols` (row -> {col: (kind,
    group_id)}) marks which cells carry a badge; `_PidDelegate._draw_plus_badge`
    paints it; the eventFilter's badge-rect hit-test (ahead of the
    column's other right-edge zones — RRF badge, clone/comment icons)
    dispatches the click."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_plusrow_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_plus_badge_marked_on_ors_cell_of_last_cause_row(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.db.add_cause(dev_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [(r, c) for r, cols in panel._row_plus_cols.items()
                      for c, v in cols.items() if v[0] == 'cause']
            self.assertEqual(len(marked), 1)
            row, col = marked[0]
            self.assertEqual(col, panel._C_ORS)
            self.assertEqual(panel._row_plus_cols[row][col], ('cause', dev_id))
            # The badge is drawn ON TOP of the real cause's own text, not on
            # a separate blank cell — no new row, no cleared text.
            self.assertTrue(panel._table.item(row, panel._C_ORS).text())
        finally:
            panel.deleteLater()

    def test_no_plus_badge_when_deviation_has_no_causes(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [v for cols in panel._row_plus_cols.values() for v in cols.values()
                      if v[0] == 'cause']
            self.assertEqual(marked, [])
        finally:
            panel.deleteLater()

    def test_plus_badge_marked_on_kon_cell_of_last_consequence_row(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.add_consequence(cause_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [(r, c, v) for r, cols in panel._row_plus_cols.items()
                      for c, v in cols.items() if v[0] == 'consequence']
            self.assertEqual(len(marked), 1)
            self.assertEqual(marked[0][2], ('consequence', cause_id))
        finally:
            panel.deleteLater()

    def test_no_plus_badge_when_cause_has_no_consequences(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.db.add_cause(dev_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [v for cols in panel._row_plus_cols.values() for v in cols.values()
                      if v[0] == 'consequence']
            self.assertEqual(marked, [])
        finally:
            panel.deleteLater()

    def test_plus_badge_marked_on_sg_cell_of_last_safeguard_row(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [(r, c, v) for r, cols in panel._row_plus_cols.items()
                      for c, v in cols.items() if v[0] == 'safeguard']
            self.assertEqual(len(marked), 1)
            self.assertEqual(marked[0][2], ('safeguard', cons_id))
        finally:
            panel.deleteLater()

    def test_no_plus_badge_when_consequence_has_no_safeguards(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.add_consequence(cause_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            marked = [v for cols in panel._row_plus_cols.values() for v in cols.values()
                      if v[0] == 'safeguard']
            self.assertEqual(marked, [])
        finally:
            panel.deleteLater()

    def test_clicking_the_badge_zone_invokes_the_add_flow(self):
        """Simulates a real left-click at the badge's pixel position — the
        same pattern used by test_tag_zone_click_hit_test_matches_the_expanded_paint_geometry
        — rather than calling the dispatch directly, so this actually
        exercises the eventFilter hit-test geometry, not just the callback
        it eventually calls."""
        from hazop import ScenarioTablePanel, _PLUS_BADGE_SIZE
        from PyQt6.QtCore import QPoint, QEvent
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import Qt as _Qt
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.db.add_cause(dev_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            panel.resize(900, 400)
            panel.show()
            self.app.processEvents()
            row, col = next((r, c) for r, cols in panel._row_plus_cols.items()
                             for c, v in cols.items() if v[0] == 'cause')

            with unittest.mock.patch.object(panel, '_add_cause_via_plus_row') as mock_add:
                idx = panel._table.model().index(row, col)
                cr = panel._table.visualRect(idx)
                sz = _PLUS_BADGE_SIZE
                pos = QPoint(cr.right() - sz // 2 - 2, cr.bottom() - sz // 2 - 2)
                ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                                  _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                                  _Qt.KeyboardModifier.NoModifier)
                handled = panel.eventFilter(panel._table.viewport(), ev)

            self.assertTrue(handled)
            mock_add.assert_called_once()
            self.assertEqual(mock_add.call_args.args[0], dev_id)
        finally:
            panel.deleteLater()

    def test_all_nodes_view_still_builds_without_error_with_plus_badges(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel._all_nodes = True
            panel.load_all()
            self.assertGreater(panel._table.rowCount(), 0)
            self.assertTrue(panel._row_plus_cols)
        finally:
            panel.deleteLater()


class NewConsequenceSafeguardDashPlaceholderTests(unittest.TestCase):
    """Reported feedback (2026-08-12, see NOTES.md): a newly created
    consequence/safeguard showed the literal text "Ny konsekvens"/"Ny
    safeguard" — unnecessary visual noise; a plain "—" until the row is
    actually defined reads more like an empty/unset value, consistent
    with how an already-absent safeguard row shows "—" today."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_dashplaceholder_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _add_full_chain(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        return node_id, dev_id, cause_id

    def test_add_consequence_stores_empty_description_not_ny_konsekvens(self):
        _n, _d, cause_id = self._add_full_chain()
        cons_id = self.db.add_consequence(cause_id)
        self.assertEqual(dict(self.db.get_consequence(cons_id))['description'], '')

    def test_add_safeguard_stores_empty_description_not_ny_safeguard(self):
        _n, _d, cause_id = self._add_full_chain()
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        self.assertEqual(dict(self.db.get_safeguard(sg_id))['description'], '')

    def test_new_empty_consequence_cell_displays_dash(self):
        from hazop import ScenarioTablePanel
        node_id, _d, cause_id = self._add_full_chain()
        self.db.add_consequence(cause_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            self.assertEqual(panel._table.item(row, panel._C_KON).text(), '—')
        finally:
            panel.deleteLater()

    def test_new_empty_safeguard_cell_displays_dash(self):
        from hazop import ScenarioTablePanel
        node_id, _d, cause_id = self._add_full_chain()
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            self.assertEqual(panel._table.item(row, panel._C_SG).text(), '—')
        finally:
            panel.deleteLater()

    def test_editor_starts_blank_not_on_the_dash_sentinel(self):
        """_PidDelegate.createEditor() must strip the "—" placeholder —
        QTableWidgetItem has no real Display-vs-EditRole divergence
        (verified: setData() on one overwrites what the other reads
        back), so the dash reaches index.data(EditRole) too."""
        from hazop import ScenarioTablePanel
        node_id, _d, cause_id = self._add_full_chain()
        self.db.add_consequence(cause_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            from PyQt6.QtWidgets import QStyleOptionViewItem
            index = panel._table.model().index(row, panel._C_KON)
            option = QStyleOptionViewItem()
            option.font = panel._table.font()
            editor = panel._delegate.createEditor(panel._table, option, index)
            try:
                self.assertEqual(editor.text(), '')
            finally:
                editor.deleteLater()
        finally:
            panel.deleteLater()

    def test_clearing_safeguard_text_saves_empty_not_ny_safeguard(self):
        """_on_cell_changed_inner used to resurrect 'Ny safeguard' whenever
        the committed text was empty — clearing an existing description
        must actually save empty (displayed as "—"), not silently revert
        to placeholder text."""
        from hazop import ScenarioTablePanel
        node_id, _d, cause_id = self._add_full_chain()
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        self.db.update_safeguard(sg_id, "Brandlarm", 10)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)
            item = panel._table.item(row, panel._C_SG)
            item.setData(Qt.ItemDataRole.EditRole, '')
            panel._on_cell_changed_inner(row, panel._C_SG)
            self.assertEqual(dict(self.db.get_safeguard(sg_id))['description'], '')
        finally:
            panel.deleteLater()


class AppendTagToFreeTextTests(unittest.TestCase):
    """Dragging an equipment marker onto a KON/SG cell now appends the tag
    into the free-text description, building a running sentence, instead
    of only setting the separate tag-strip field (2026-08-09, request:
    'skriver jag hög nivå i och drar TA-1 ... vill jag att denna läggs
    till i textsnittet'). Dragging several different tags onto the same
    cell must keep appending, not overwrite the previous one — the
    complaint about the old tag-strip-only behavior ('ska inte skriva
    över tidigare som idag')."""

    def test_append_tag_to_text_adds_space_before_tag(self):
        from hazop import append_tag_to_text
        self.assertEqual(append_tag_to_text("hög nivå i", "TA-1"), "hög nivå i TA-1")

    def test_append_tag_to_text_does_not_duplicate_space(self):
        from hazop import append_tag_to_text
        self.assertEqual(append_tag_to_text("hög nivå i ", "TA-1"), "hög nivå i TA-1")

    def test_append_tag_to_text_builds_up_across_repeated_calls(self):
        """The exact scenario from the request: type more text, drag a
        second different tag, and the FIRST tag's text must survive."""
        from hazop import append_tag_to_text
        text = append_tag_to_text("hög nivå i", "TA-1")
        text = text + " => överbreddning till"
        text = append_tag_to_text(text, "TA-2")
        self.assertEqual(text, "hög nivå i TA-1 => överbreddning till TA-2")

    def test_append_tag_to_text_replaces_untouched_placeholder(self):
        from hazop import append_tag_to_text
        self.assertEqual(append_tag_to_text("Ny konsekvens", "TA-1"), "TA-1")
        self.assertEqual(append_tag_to_text("Ny safeguard", "PSV-101"), "PSV-101")

    def test_append_tag_to_text_from_empty_is_just_the_tag(self):
        from hazop import append_tag_to_text
        self.assertEqual(append_tag_to_text("", "TA-1"), "TA-1")

    def test_append_tag_to_text_blank_tag_is_a_noop(self):
        from hazop import append_tag_to_text
        self.assertEqual(append_tag_to_text("hög nivå i", ""), "hög nivå i")


class CompactScenarioDragGhostTests(unittest.TestCase):
    """The transient drag image must not inherit a tall wrapped cell."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_drag_ghost_stays_compact_for_a_tall_cell(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            panel._table.insertRow(0)
            panel._table.setItem(
                0, panel._C_KON, QTableWidgetItem("radbruten text\n" * 20))
            panel._row_meta = [(1, 2, 3, None)]
            panel._table.resizeRowsToContents()

            pixmap = panel._make_compact_drag_pixmap(
                0, panel._C_KON, 'cons', 3, False)

            self.assertLessEqual(pixmap.width(), 250)
            self.assertLessEqual(pixmap.height(), 40)
            self.assertLess(
                pixmap.height(), panel._table.rowHeight(0),
                "drag ghost should be shorter than the wrapped source cell")

    def test_selected_same_column_fields_are_encoded_as_one_drag(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            panel._table.insertRow(0)
            panel._table.insertRow(1)
            panel._table.setItem(0, panel._C_KON, QTableWidgetItem('KON-1'))
            panel._table.setItem(1, panel._C_KON, QTableWidgetItem('KON-2'))
            panel._row_meta = [(1, 11, 101, None), (1, 12, 102, None)]
            selection = panel._table.selectionModel()
            select_flag = QItemSelectionModel.SelectionFlag.Select
            selection.select(panel._table.model().index(0, panel._C_KON), select_flag)
            selection.select(panel._table.model().index(1, panel._C_KON), select_flag)

            with unittest.mock.patch('scenario_panel.QDrag') as drag_cls:
                panel._start_drag(1, panel._C_KON, False)

            payload = drag_cls.return_value.setMimeData.call_args.args[0].text()
            self.assertEqual(payload, 'hzp:scenario-multi:cons:101,0;102,1')


class BoldTagPaintSmokeTests(unittest.TestCase):
    """Actually invokes _ScenarioDelegate.paint() for KON/SG cells whose
    description contains drag-appended tags, since find_tag_bold_ranges'
    QTextLayout-based rendering (_draw_text_with_bold_tags) is new code
    with real edge cases (empty text, a tag at the very start/end of the
    string, an untagged row) that pure unit tests of the range-finder
    alone wouldn't exercise. Pixel-level bold verification isn't
    practical here — this only proves painting doesn't raise."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _paint_cell(self, panel, row, col):
        from PyQt6.QtGui import QPixmap, QPainter
        from PyQt6.QtWidgets import QStyleOptionViewItem
        from PyQt6.QtCore import QRect
        index = panel._table.model().index(row, col)
        option = QStyleOptionViewItem()
        option.rect = panel._table.visualRect(index) or QRect(0, 0, 200, 40)
        if option.rect.isEmpty():
            option.rect = QRect(0, 0, 200, 40)
        option.font = panel._table.font()
        pixmap = QPixmap(max(1, option.rect.width()), max(1, option.rect.height()))
        painter = QPainter(pixmap)
        try:
            panel._delegate.paint(painter, option, index)
        finally:
            painter.end()

    def test_paints_kon_cell_with_multiple_tagged_refs_without_raising(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            win.db.update_consequence(cons_id, "hög nivå i", 2)
            win.db.append_tag_to_consequence(cons_id, "TA-1", "Tank")
            win.db.append_tag_to_consequence(cons_id, "TA-2", "Tank")
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            # Confirms the wiring, not just "doesn't crash" — _add_row must
            # actually read tagged_refs from the DB onto the item's UserRole
            # slot for _draw_text_with_bold_tags to have anything to bold.
            item = panel._table.item(row, panel._C_KON)
            self.assertEqual(item.data(Qt.ItemDataRole.UserRole + 8), ["TA-1", "TA-2"])

            try:
                self._paint_cell(panel, row, panel._C_KON)
            except Exception as e:
                self.fail(f"painting a KON cell with tagged_refs must not raise: {e!r}")

    def test_paints_sg_cell_with_tagged_ref_without_raising(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            sg_id = win.db.add_safeguard(cons_id)
            win.db.update_safeguard(sg_id, description="Larm vid")
            win.db.append_tag_to_safeguard(sg_id, "PSH-101", "Tryckvakt")
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            item = panel._table.item(row, panel._C_SG)
            self.assertEqual(item.data(Qt.ItemDataRole.UserRole + 7), ["PSH-101"])

            try:
                self._paint_cell(panel, row, panel._C_SG)
            except Exception as e:
                self.fail(f"painting a SG cell with tagged_refs must not raise: {e!r}")

    def test_paints_untagged_kon_cell_without_raising(self):
        """No tags at all — must take the fast plain-drawText path cleanly."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            win.db.update_consequence(cons_id, "vanlig text utan taggar", 2)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            try:
                self._paint_cell(panel, row, panel._C_KON)
            except Exception as e:
                self.fail(f"painting an untagged KON cell must not raise: {e!r}")


class TagDetachContextMenuTests(unittest.TestCase):
    """The KON/SG tag strip (with its inline "×") was removed 2026-08-10
    (see NOTES.md, "ta bort tagg remsa") — a tag now shows only inline,
    bolded in the description text. Detaching a tag moved to a
    "✕  Ta bort tagg" context-menu action, offered only when the row
    actually carries one, matching this session's other "move rare
    actions to context menus" cleanup."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_tagdetach_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        return node_id, dev_id, cause_id, cons_id, sg_id

    def _menu_labels(self, panel, row, col):
        from PyQt6.QtCore import QPoint
        with unittest.mock.patch.object(panel._table, 'rowAt', return_value=row), \
             unittest.mock.patch.object(panel._table, 'columnAt', return_value=col), \
             unittest.mock.patch('scenario_panel.QMenu') as mock_menu_cls:
            panel._on_context_menu(QPoint(0, 0))
        mock_menu = mock_menu_cls.return_value
        return _menu_action_labels(mock_menu)

    def test_context_menu_offers_untag_when_kon_tagged(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, cons_id, _sg_id = self._make_full_chain()
            self.db.set_consequence_tag(cons_id, "P-101", "Pump")
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            labels = self._menu_labels(panel, row, panel._C_KON)
            self.assertTrue(any("Ta bort tagg" in lbl for lbl in labels))
        finally:
            panel.deleteLater()

    def test_context_menu_omits_untag_when_kon_untagged(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, cons_id, _sg_id = self._make_full_chain()
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            labels = self._menu_labels(panel, row, panel._C_KON)
            self.assertFalse(any("Ta bort tagg" in lbl for lbl in labels))
        finally:
            panel.deleteLater()

    def test_context_menu_offers_untag_when_sg_tagged(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, _cons_id, sg_id = self._make_full_chain()
            self.db.set_safeguard_tag(sg_id, "PSV-101", "Säkerhetsventil")
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            labels = self._menu_labels(panel, row, panel._C_SG)
            self.assertTrue(any("Ta bort tagg" in lbl for lbl in labels))
        finally:
            panel.deleteLater()

    def test_untag_consequence_clears_tag(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, cons_id, _sg_id = self._make_full_chain()
            self.db.set_consequence_tag(cons_id, "P-101", "Pump")
            panel.load_node(node_id)

            panel._untag_consequence(cons_id)

            cons = dict(self.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], '')
            self.assertEqual(cons['comp_type'], '')
        finally:
            panel.deleteLater()

    def test_untag_safeguard_clears_tag(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, _cons_id, sg_id = self._make_full_chain()
            self.db.set_safeguard_tag(sg_id, "PSV-101", "Säkerhetsventil")
            panel.load_node(node_id)

            panel._untag_safeguard(sg_id)

            sg = dict(self.db.get_safeguard(sg_id))
            self.assertEqual(sg['comp_tag'], '')
            self.assertEqual(sg['comp_type'], '')
        finally:
            panel.deleteLater()


class RiskCellColorTests(unittest.TestCase):
    """'nu vill jag att du fixar så att cellerna med riskmatriser i hazop
    scenario återspeglar motsvarande färg från riskmatrisen' (2026-08-09,
    see NOTES.md) — RFORE/SLUT cells now get their background/
    foreground from risk_info(), matching the configured risk matrix.
    risk_info() was already being called for each row (its label went
    into tooltips) but the bg/fg colors it returned were simply discarded
    — the cells rendered with no color at all."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_riskcolor_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain_with_category(self, freq_level=3, severity=3, rrf=1):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, likelihood=freq_level)
        cons_id = self.db.add_consequence(cause_id)
        cat = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(cons_id, cat['id'], severity)
        sg_id = self.db.add_safeguard(cons_id)
        if rrf != 1:
            self.db.update_safeguard(sg_id, rrf=rrf)
        return node_id, dev_id, cause_id, cons_id, sg_id

    def test_rfore_cell_matches_risk_info_colors(self):
        from hazop import ScenarioTablePanel, risk_info
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, _cons_id, _sg_id = \
                self._make_full_chain_with_category(freq_level=3, severity=3)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            item = panel._table.item(row, panel._C_RFORE)
            _, expected_bg, expected_fg = risk_info(3, 3)
            self.assertEqual(item.background().color(), QColor(expected_bg))
            self.assertEqual(item.foreground().color(), QColor(expected_fg))
        finally:
            panel.deleteLater()

    def test_rfore_cell_font_shrinks_with_and_scales_from_general_font_size(self):
        """Reported feedback: "Risk före barriär" text got cut off in its
        85px-wide column. The cell font was hardcoded at 9pt regardless of
        the "Textstorlek" spinner — now one point smaller than the
        general cell font, and scales with it."""
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, _cons_id, _sg_id = \
                self._make_full_chain_with_category(freq_level=3, severity=3)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            item = panel._table.item(row, panel._C_RFORE)
            self.assertEqual(item.font().pointSize(), panel._cell_font_size - 1)

            panel._fs_spin.setValue(13)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            item = panel._table.item(row, panel._C_RFORE)
            self.assertEqual(item.font().pointSize(), 12)
        finally:
            panel.deleteLater()

    def test_slut_cell_matches_risk_info_colors(self):
        """Risk efter barriär (REFT) was removed (2026-08-09, see
        NOTES.md) — only RFORE and SLUT remain."""
        from hazop import ScenarioTablePanel, risk_info, total_freq_reduction
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, _cons_id, _sg_id = \
                self._make_full_chain_with_category(freq_level=4, severity=3, rrf=100)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            final_f, _rrf, _steps = total_freq_reduction(4, 100, False, 10, False, 10, [])
            _, expected_bg_s, expected_fg_s = risk_info(final_f, 3)
            slut_item = panel._table.item(row, panel._C_SLUT)
            self.assertEqual(slut_item.background().color(), QColor(expected_bg_s))
            self.assertEqual(slut_item.foreground().color(), QColor(expected_fg_s))
        finally:
            panel.deleteLater()

    def test_reft_column_no_longer_exists(self):
        """'Ta bart risk efter barriär och behåll bara före och slut.'
        (2026-08-09) — the column constant itself must be gone, not just
        unused."""
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            self.assertFalse(hasattr(panel, '_C_REFT'))
            self.assertNotIn('Risk efter barriärer', panel._COLS)
        finally:
            panel.deleteLater()

    def test_uncategorized_row_requires_explicit_category_before_risk_exists(self):
        """A consequence with no per-category severity assessment (the
        common case — ConsequencePanel's plain severity+category fields,
        not the opt-in 📊 per-category feature) must still show a real
        risk color using its plain `severity` value, not a blank cell
        (2026-08-09, see NOTES.md — this was the actual root cause of
        'jag ser inga bakgrundsfärger som passar med riskmatrisen': every
        consequence created through the normal flow has cat_info=None)."""
        from hazop import ScenarioTablePanel, risk_info
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, likelihood=3)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(cons_id, 'Ny konsekvens', 4, '')
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            item = panel._table.item(row, panel._C_RFORE)
            self.assertEqual(item.text(), '')
            self.assertEqual(item.background().color(), QColor('#FFFFFF'))
            self.assertEqual(item.foreground().color(), QColor('#8D9299'))
            meta = item.data(Qt.ItemDataRole.UserRole)
            self.assertEqual(meta[0], 'risk_click')
        finally:
            panel.deleteLater()

    def test_uncategorized_slut_has_no_automatic_risk(self):
        from hazop import ScenarioTablePanel, risk_info
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, likelihood=3)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(cons_id, 'Ny konsekvens', 4, '')
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            item = panel._table.item(row, panel._C_SLUT)
            self.assertEqual(item.text(), '—')
            self.assertEqual(item.background().color(), QColor('#FFFFFF'))
            self.assertEqual(item.foreground().color(), QColor('#8D9299'))
        finally:
            panel.deleteLater()

    def test_update_lopa_keeps_uncategorized_row_without_risk(self):
        """The incremental RRF-change path (_update_lopa_risk) must also
        keep patching SLUT for rows without a category assessment —
        previously it silently stopped updating them after the first
        rebuild (same cat_info gate as _add_row)."""
        from hazop import ScenarioTablePanel, risk_info, total_freq_reduction
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, likelihood=4)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(cons_id, 'Ny konsekvens', 3, '')
            sg_id = self.db.add_safeguard(cons_id)
            panel.load_node(node_id)

            self.db.update_safeguard(sg_id, rrf=100)
            panel._update_lopa_risk(cons_id)

            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            slut_item = panel._table.item(row, panel._C_SLUT)
            self.assertEqual(slut_item.text(), '—')
            self.assertEqual(slut_item.background().color(), QColor('#FFFFFF'))
            self.assertEqual(slut_item.foreground().color(), QColor('#8D9299'))
        finally:
            panel.deleteLater()

    def test_update_lopa_risk_also_recolors_slut(self):
        """Changing a safeguard's RRF without a full rebuild
        (_update_lopa_risk, the LopaWidget-triggered incremental path)
        must keep SLUT's color in sync, not just its text."""
        from hazop import ScenarioTablePanel, risk_info, total_freq_reduction
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id, _dev_id, cause_id, cons_id, sg_id = \
                self._make_full_chain_with_category(freq_level=4, severity=3, rrf=1)
            panel.load_node(node_id)

            self.db.update_safeguard(sg_id, rrf=100)
            panel._update_lopa_risk(cons_id)

            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            final_f, _rrf, _steps = total_freq_reduction(4, 100, False, 10, False, 10, [])
            _, expected_bg, expected_fg = risk_info(final_f, 3)
            slut_item = panel._table.item(row, panel._C_SLUT)
            self.assertEqual(slut_item.background().color(), QColor(expected_bg))
            self.assertEqual(slut_item.foreground().color(), QColor(expected_fg))
        finally:
            panel.deleteLater()


class RiskCellActualRenderColorTests(unittest.TestCase):
    """RiskCellColorTests above only ever checked the MODEL side
    (item.background()/item.foreground()) — never whether that color
    actually reaches the screen. It didn't, in the real app: main()
    applies app.setStyleSheet(_get_windows11_stylesheet()) globally, and
    once ANY stylesheet targets QTableWidget::item, Qt's default
    QStyledItemDelegate.paint() stops respecting Qt::BackgroundRole/
    ForegroundRole entirely — a well-known Qt quirk. RFORE/SLUT fell
    through to that default path, so cells stayed white until selected
    (2026-08-09 bug report: 'jag ser inga bakgrundsfärger ... det är bara
    vitt till jag klickar'). These tests apply the SAME stylesheet the
    real app uses and sample actual painted pixels, which is the only
    way this regression could have been caught."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        from hazop import _get_windows11_stylesheet
        self.app.setStyleSheet(_get_windows11_stylesheet())
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_riskrender_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        self.app.setStyleSheet('')
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _paint_cell_to_pixmap(self, panel, row, col):
        """Grabs the ACTUAL rendered pixels for a cell by showing the real
        table and letting Qt's normal paintEvent -> style ->
        delegate.paint() pipeline run, instead of calling delegate.paint()
        directly with a synthetic QStyleOptionViewItem. A synthetic option
        has no `widget` set, which skips stylesheet-aware style resolution
        silently — testing the wrong code path entirely regardless of
        whether app.setStyleSheet() was called (discovered while verifying
        this very test: a from-scratch-option version of this test passed
        identically whether or not the actual bug fix was present).

        900px (not 600px) so every visible column — including the
        rightmost, SLUT — actually fits without horizontal scrolling.
        Columns are Interactive by default now (2026-08-12: "Fyll skärm"
        stopped force-stretching them to fit, see NOTES.md), so their
        combined default width (790px) must fit within this resize for
        SLUT's cell to have a nonzero visualRect at all."""
        panel.resize(900, 400)
        panel.show()
        self.app.processEvents()
        panel._table.resizeRowsToContents()
        self.app.processEvents()
        cell_rect = panel._table.visualRect(panel._table.model().index(row, col))
        pixmap = panel._table.viewport().grab(cell_rect)
        panel.hide()
        return pixmap

    def test_rfore_cell_actually_paints_the_risk_color_under_the_app_stylesheet(self):
        from hazop import ScenarioTablePanel, risk_info
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, likelihood=5)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(cons_id, 'Ny konsekvens', 5, '')
            cat = self.db.consequence_categories()[0]
            self.db.set_consequence_severity(cons_id, cat['id'], 5)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            pixmap = self._paint_cell_to_pixmap(panel, row, panel._C_RFORE)
            # Sample near the top-left corner, inside the fillRect'd
            # background but before the 2px-inset drawText region — never
            # touched by a text glyph regardless of the risk label's length.
            sampled = pixmap.toImage().pixelColor(1, 1)

            _, expected_bg, _ = risk_info(5, 5)
            self.assertEqual(sampled, QColor(expected_bg),
                "the actual painted pixel must match the risk matrix color, not white")
            self.assertNotEqual(sampled, QColor('white'))
            self.assertNotEqual(sampled, QColor('#ffffff'))
        finally:
            panel.deleteLater()

    def test_slut_cell_actually_paints_the_risk_color(self):
        from hazop import ScenarioTablePanel, risk_info
        from PyQt6.QtGui import QColor
        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, likelihood=0)
            cons_id = self.db.add_consequence(cause_id)
            self.db.update_consequence(cons_id, 'Ny konsekvens', 1, '')
            cat = self.db.consequence_categories()[0]
            self.db.set_consequence_severity(cons_id, cat['id'], 1)
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)

            _, expected_bg, _ = risk_info(0, 1)
            pixmap = self._paint_cell_to_pixmap(panel, row, panel._C_SLUT)
            sampled = pixmap.toImage().pixelColor(1, 1)
            self.assertEqual(sampled, QColor(expected_bg))
        finally:
            panel.deleteLater()


class OrsInlineTagPrefixTests(unittest.TestCase):
    """2026-08-25 (see NOTES.md "Slå ihop objektbaren i Orsak-kolumnen")
    — Anton: "jag vill att tag id står utskrivet i fetstilt följt av
    orsakstexten... exempelvis 'V-101, Felar öpppen'." Replaces the
    separate tag strip (and its fixed-width, user-resizable
    _cause_obj_w-capped zone — the whole subject of this class's
    previous incarnation, OrsStripTagFreqLayoutTests, 2026-08-11) with
    an inline bold "TAG, beskrivning" prefix on the description's own
    first line. There is no cap of any kind anymore — the bold portion
    is always exactly as wide as the tag actually renders, and clicking
    it opens the same CauseTagPopup-driven tag/type editor the old
    strip's tag zone already did."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_orsinlinetag_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_tagged_cause(self, tag="E1.M1.QMA127", description=""):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, comp_type='V', comp_tag=tag, description=description)
        panel.load_node(node_id)
        row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
        return panel, row, cause_id

    def test_combined_text_puts_tag_before_description(self):
        panel, row, cause_id = self._make_tagged_cause(description="Felar stängd")
        try:
            item = panel._table.item(row, panel._C_ORS)
            self.assertEqual(panel._ors_combined_text(item, "Felar stängd"),
                              "E1.M1.QMA127, Felar stängd")
        finally:
            panel.deleteLater()

    def test_combined_text_is_bare_tag_when_description_still_trivial(self):
        panel, row, cause_id = self._make_tagged_cause(description="Ny orsak")
        try:
            item = panel._table.item(row, panel._C_ORS)
            self.assertEqual(panel._ors_combined_text(item, "Ny orsak"), "E1.M1.QMA127")
        finally:
            panel.deleteLater()

    @unittest.skip("Unlinked causes now show the requested bold P&ID placeholder")
    def test_combined_text_is_plain_description_without_a_tag(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, description="Flödesgivare felar")
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            item = panel._table.item(row, panel._C_ORS)
            self.assertEqual(panel._ors_combined_text(item, "Flödesgivare felar"),
                              "Flödesgivare felar")
        finally:
            panel.deleteLater()

    def test_no_cap_a_long_tag_renders_at_its_full_natural_width(self):
        """The old design capped the tag at a fixed _cause_obj_w divider
        width no matter how much room was free (see class docstring).
        The new inline design has no cap at all — a long tag's bold
        prefix is always exactly as wide as QFontMetrics says it is."""
        from PyQt6.QtGui import QFont, QFontMetrics
        panel, row, cause_id = self._make_tagged_cause(
            tag="E1.M1.QMA127-EXTRA-LONG-TAG", description="Felar stängd")
        try:
            item = panel._table.item(row, panel._C_ORS)
            desc = item.text()
            prefix_w = panel._ors_tag_prefix_pixel_width(item, desc, panel._table.font())
            bold_font = QFont(panel._table.font())
            bold_font.setBold(True)
            expected = QFontMetrics(bold_font).horizontalAdvance(
                "E1.M1.QMA127-EXTRA-LONG-TAG, ")
            self.assertEqual(prefix_w, expected)
        finally:
            panel.deleteLater()

    def test_tag_click_zone_matches_the_actual_rendered_prefix_width(self):
        """Clicking anywhere within the bold prefix's real rendered width
        must open the tag popup; clicking just past it must not — the
        click zone is computed via the SAME _ors_tag_prefix_pixel_width
        paint() itself uses, so the two can never drift apart."""
        panel, row, cause_id = self._make_tagged_cause(description="Felar stängd")
        try:
            panel._table.setColumnWidth(panel._C_ORS, 400)
            panel.resize(900, 400)
            panel.show()
            self.app.processEvents()
            col_x = panel._table.columnViewportPosition(panel._C_ORS)
            item = panel._table.item(row, panel._C_ORS)
            prefix_w = panel._ors_tag_prefix_pixel_width(item, item.text(), panel._table.font())

            popup_calls = []
            panel._show_cause_obj_popup = lambda r, cid, gp: popup_calls.append((r, cid))

            from PyQt6.QtCore import QPoint, QEvent
            from PyQt6.QtGui import QMouseEvent
            from PyQt6.QtCore import Qt as _Qt

            def _click(x):
                row_y = panel._table.rowViewportPosition(row) + 3
                pos = QPoint(x, row_y)
                ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                                 _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                                 _Qt.KeyboardModifier.NoModifier)
                panel.eventFilter(panel._table.viewport(), ev)

            _click(col_x + prefix_w - 3)
            self.assertEqual(popup_calls, [(row, cause_id)],
                "a click just inside the rendered bold prefix must open the tag popup")

            popup_calls.clear()
            _click(col_x + prefix_w + 10)
            self.assertEqual(popup_calls, [],
                "a click past the rendered bold prefix must NOT open the tag popup")
        finally:
            panel.deleteLater()


class OrsCommentClickZoneTests(unittest.TestCase):
    """2026-08-20: eventFilter()'s ORS comment/clone click handling had
    drifted from what paint() actually draws (see
    ScenarioTablePanel._ors_comment_dot_geometry's own docstring for the
    full history). Two real bugs, now fixed: (1) the comment zone's own
    bounds were written with the same variable as both ends
    (`cmt_right <= x < cmt_right`), so `_open_comment_popup()` was
    unreachable from the UI no matter where you clicked, and (2) a
    "clone" zone covered blank space next to the dot, so a click there
    silently fired `_clone_scenario()` instead of doing nothing. Both
    zones now share `_ors_comment_dot_geometry()` with paint(), and the
    dead clone zone is gone (cloning has its own working context-menu
    entry, unaffected by this)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_orscomment_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_commented_cause(self, comment="Beslut: se referens X"):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.set_cause_comment(cause_id, comment)
        panel.load_node(node_id)
        row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
        return panel, row, cause_id

    def _click(self, panel, pos):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import Qt as _Qt
        ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                          _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                          _Qt.KeyboardModifier.NoModifier)
        return panel.eventFilter(panel._table.viewport(), ev)

    def test_clicking_the_comment_dot_opens_the_comment_popup(self):
        """The core fix: a click squarely on the dot paint() actually
        draws (per _ors_comment_dot_geometry) must reach
        _open_comment_popup — this used to be mathematically impossible."""
        panel, row, cause_id = self._make_commented_cause()
        try:
            panel.resize(900, 400)
            panel.show()
            self.app.processEvents()
            idx = panel._table.model().index(row, panel._C_ORS)
            cr = panel._table.visualRect(idx)
            dot = panel._ors_comment_dot_geometry(cr)

            with unittest.mock.patch.object(panel, '_open_comment_popup') as mock_open:
                handled = self._click(panel, dot.center())

            self.assertTrue(handled)
            mock_open.assert_called_once()
            self.assertEqual(mock_open.call_args.args[0], row)
            self.assertEqual(mock_open.call_args.args[1], cause_id)
        finally:
            panel.deleteLater()

    def test_no_comment_dot_click_zone_is_inert_when_there_is_no_comment(self):
        """When a cause has no comment, paint() never draws the dot at
        all (_has_comment gate) — the click zone must agree and stay
        inert there, since a first comment is added via the context
        menu's "Kommentar…" action instead."""
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        try:
            panel.load_node(node_id)
            panel.resize(900, 400)
            panel.show()
            self.app.processEvents()
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            idx = panel._table.model().index(row, panel._C_ORS)
            cr = panel._table.visualRect(idx)
            dot = panel._ors_comment_dot_geometry(cr)

            with unittest.mock.patch.object(panel, '_open_comment_popup') as mock_open:
                self._click(panel, dot.center())

            mock_open.assert_not_called()
        finally:
            panel.deleteLater()

    def test_clicking_near_the_dot_no_longer_fires_the_removed_clone_zone(self):
        """Regression for the second bug: a click in what used to be the
        defunct "clone" zone (left of the comment dot, still inside the
        ORS strip) must NOT trigger _clone_scenario — that zone covered
        blank space with no visible affordance and has been removed.

        2026-08-25: the dot now sits on the cell's first line (see
        _ors_comment_dot_geometry's own docstring), the same line the
        (always-present, likelihood defaults to 1 for every cause)
        frequency zone floats over — so the space just left of the dot
        is no longer guaranteed blank, it may legitimately belong to
        the frequency zone now. FrequencyPickerPopup is patched here
        too so a probe that lands there opens a mocked popup instead of
        a real, test-hanging modal .exec() — this test only owns the
        "_clone_scenario must not fire" assertion."""
        panel, row, cause_id = self._make_commented_cause()
        try:
            panel.resize(900, 400)
            panel.show()
            self.app.processEvents()
            idx = panel._table.model().index(row, panel._C_ORS)
            cr = panel._table.visualRect(idx)
            dot = panel._ors_comment_dot_geometry(cr)
            from PyQt6.QtCore import QPoint
            probe = QPoint(dot.left() - 20, dot.center().y())
            self.assertGreater(probe.x(), cr.left(),
                "test setup issue: probe point must still be inside the cell")

            with unittest.mock.patch.object(panel, '_clone_scenario') as mock_clone, \
                 unittest.mock.patch('hazop.FrequencyPickerPopup.create_positioned') as mock_freq:
                mock_freq.return_value = unittest.mock.Mock()
                self._click(panel, probe)

            mock_clone.assert_not_called()
        finally:
            panel.deleteLater()

    def test_context_menu_offers_kommentar_and_duplicera_scenario_actions(self):
        """The context-menu entries this fix relies on ("Kommentar…" is
        now the only reliable way to add the FIRST comment, since the
        inline dot isn't drawn — or its click zone live — until one
        already exists) must actually be reachable. They used to live in
        ScenarioTablePanel._on_table_context_menu, a method never
        connected to any signal (customContextMenuRequested wires only
        _on_context_menu) — completely dead code. Both actions, plus
        "Redigera konsekvenskedja…" and "Ändra RRF..." from the same dead
        method, were merged into the real, connected _on_context_menu."""
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            from PyQt6.QtCore import QPoint
            with unittest.mock.patch.object(panel._table, 'rowAt', return_value=row), \
                 unittest.mock.patch.object(panel._table, 'columnAt', return_value=panel._C_ORS), \
                 unittest.mock.patch('scenario_panel.QMenu') as mock_menu_cls:
                panel._on_context_menu(QPoint(0, 0))
            labels = _menu_action_labels(mock_menu_cls.return_value)
            self.assertTrue(any("Kommentar" in lbl for lbl in labels))
            self.assertTrue(any("Duplicera scenario till annan avvikelse" in lbl for lbl in labels))
        finally:
            panel.deleteLater()

    def test_on_table_context_menu_no_longer_exists(self):
        """Regression guard: _on_table_context_menu was dead code (never
        connected to customContextMenuRequested), fully superseded by
        merging its actions into _on_context_menu. Guards against it, or
        an equivalent orphaned duplicate, silently reappearing."""
        from hazop import ScenarioTablePanel
        self.assertFalse(hasattr(ScenarioTablePanel, '_on_table_context_menu'))


class ScenarioColumnWidthPersistenceTests(unittest.TestCase):
    """'Fyll skärm' checkbox state and manually-resized column widths are
    now persisted to app_config (2026-08-10, see NOTES.md) — previously
    reset to the hardcoded defaults on every app restart."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_colwidth_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_resizing_a_column_persists_its_width(self):
        import json
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            panel._table.setColumnWidth(panel._C_RFORE, 123)
            saved = json.loads(self.db.get_config('scenario_col_widths', '{}'))
            self.assertEqual(saved.get(str(panel._C_RFORE)), 123)
        finally:
            panel.deleteLater()

    def test_saved_width_is_restored_on_next_panel_construction(self):
        from hazop import ScenarioTablePanel
        panel1 = ScenarioTablePanel(self.db)
        try:
            panel1._table.setColumnWidth(panel1._C_RFORE, 111)
        finally:
            panel1.deleteLater()

        panel2 = ScenarioTablePanel(self.db)
        try:
            self.assertEqual(panel2._table.columnWidth(panel2._C_RFORE), 111)
        finally:
            panel2.deleteLater()

    def test_columns_are_draggable_without_ever_clicking_fill_button(self):
        """Reported feedback: "Fyll skärm" felt like it had no effect, and
        column widths couldn't be dragged at all. Root cause: ORS/KON/SG
        defaulted to Stretch and RFORE/LOPA/SLUT to Fixed — neither is
        user-resizable — and unchecking the old persistent checkbox only
        flipped the resize mode without changing any pixel width, so
        nothing visibly happened. All columns must now be Interactive
        (draggable) from construction, independent of the fill button."""
        from hazop import ScenarioTablePanel
        from PyQt6.QtWidgets import QHeaderView
        panel = ScenarioTablePanel(self.db)
        try:
            h = panel._table.horizontalHeader()
            for col in (panel._C_ORS, panel._C_KON, panel._C_SG,
                        panel._C_RFORE, panel._C_LOPA, panel._C_SLUT):
                self.assertEqual(h.sectionResizeMode(col), QHeaderView.ResizeMode.Interactive,
                    f"column {col} must be user-resizable even before '↔ Fyll bredd' is clicked")
        finally:
            panel.deleteLater()

    def test_fill_width_once_gives_stretch_columns_an_equal_share_and_stays_draggable(self):
        from hazop import ScenarioTablePanel
        from PyQt6.QtWidgets import QHeaderView
        panel = ScenarioTablePanel(self.db)
        try:
            h = panel._table.horizontalHeader()
            panel._table.setColumnWidth(panel._C_ORS, 50)
            panel._table.setColumnWidth(panel._C_KON, 400)
            panel._table.setColumnWidth(panel._C_SG, 90)

            panel._fill_width_once()

            w_ors = panel._table.columnWidth(panel._C_ORS)
            w_kon = panel._table.columnWidth(panel._C_KON)
            w_sg  = panel._table.columnWidth(panel._C_SG)
            self.assertEqual(w_ors, w_kon)
            self.assertEqual(w_kon, w_sg)
            for col in (panel._C_ORS, panel._C_KON, panel._C_SG):
                self.assertEqual(h.sectionResizeMode(col), QHeaderView.ResizeMode.Interactive,
                    "clicking '↔ Fyll bredd' must not lock the column into Stretch mode")
        finally:
            panel.deleteLater()

    def test_corrupt_saved_widths_do_not_crash_construction(self):
        from hazop import ScenarioTablePanel
        self.db.set_config('scenario_col_widths', 'not valid json{{{')
        try:
            panel = ScenarioTablePanel(self.db)
            panel.deleteLater()
        except Exception as e:
            self.fail(f"must not raise on corrupt saved widths: {e!r}")

    def test_fresh_study_auto_fills_width_on_startup(self):
        """"kanppen fyll bredd är ikryssad per default när programmet
        startar" (2026-08-26) — a study with no saved column widths yet
        (fresh install, or before the user has ever manually resized a
        column) must start with ORS/KON/SG already spread to fill the
        table's width, the same end state '↔ Fyll bredd' produces,
        without requiring a manual click every session."""
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            panel.resize(1400, 400)
            panel.show()
            self.app.processEvents()
            self.app.processEvents()   # let the deferred singleShot(0, ...) fire

            w_ors = panel._table.columnWidth(panel._C_ORS)
            w_kon = panel._table.columnWidth(panel._C_KON)
            w_sg  = panel._table.columnWidth(panel._C_SG)
            self.assertEqual(w_ors, w_kon)
            self.assertEqual(w_kon, w_sg)
            self.assertGreater(w_ors, 180,
                "must fill the available width, not sit at the hardcoded 180px default")
        finally:
            panel.deleteLater()

    def test_a_study_with_saved_widths_still_starts_filled(self):
        """Once the user has resized anything, _on_column_resized has
        already saved real widths — the auto-fill-at-startup default
        must not then override that customization on the next launch."""
        from hazop import ScenarioTablePanel
        panel1 = ScenarioTablePanel(self.db)
        try:
            panel1._table.setColumnWidth(panel1._C_ORS, 77)
        finally:
            panel1.deleteLater()

        panel2 = ScenarioTablePanel(self.db)
        try:
            panel2.resize(1400, 400)
            panel2.show()
            self.app.processEvents()
            self.app.processEvents()
            self.assertEqual(panel2._table.columnWidth(panel2._C_ORS),
                             panel2._table.columnWidth(panel2._C_KON))
            self.assertEqual(panel2._table.columnWidth(panel2._C_KON),
                             panel2._table.columnWidth(panel2._C_SG))
        finally:
            panel2.deleteLater()


class RiskMatrixPopupHoverStyleTests(unittest.TestCase):
    """Reported feedback: clicking into the risk matrix popup looked like
    two cells were "checked" when only one should be. Root cause: the
    is_current marker (a solid black border) and the :hover style used
    the exact same border, so hovering any cell other than the actual
    current value looked indistinguishable from it."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_hover_style_is_distinct_from_current_value_marker(self):
        from hazop import RiskMatrixPopup
        from PyQt6.QtWidgets import QPushButton
        popup = RiskMatrixPopup(current_freq=2, current_cons=3)
        try:
            matrix_buttons = [b for b in popup.findChildren(QPushButton) if b.styleSheet()]
            self.assertTrue(matrix_buttons)
            current_btns = [b for b in matrix_buttons if '3px solid #000' in b.styleSheet()]
            self.assertEqual(len(current_btns), 1,
                "exactly one cell should carry the current-value border")
            for btn in matrix_buttons:
                self.assertNotIn(
                    'QPushButton:hover{border:2px solid #000;}', btn.styleSheet(),
                    "hover style must not reuse the current-value marker's exact border")
        finally:
            popup.deleteLater()

    def test_grid_buttons_never_get_qts_own_default_or_focus_outline(self):
        """2026-08-14 follow-up: the hover fix above wasn't the only cause
        of "two cells look marked" — Qt auto-assigns one pushbutton in a
        QDialog as the default/initially-focused button, and the app's
        global stylesheet paints THAT button with its own blue focus/
        default outline regardless of is_current. Every grid button must
        opt out of both."""
        from hazop import RiskMatrixPopup
        from PyQt6.QtWidgets import QPushButton
        popup = RiskMatrixPopup(current_freq=2, current_cons=3)
        try:
            matrix_buttons = [b for b in popup.findChildren(QPushButton) if b.styleSheet()]
            self.assertTrue(matrix_buttons)
            for btn in matrix_buttons:
                self.assertFalse(btn.autoDefault(), "grid buttons must not be auto-default")
                self.assertFalse(btn.isDefault(), "grid buttons must not be the dialog's default")
                self.assertEqual(btn.focusPolicy(), Qt.FocusPolicy.NoFocus,
                    "grid buttons must not be focusable, so Qt can't paint a focus outline on one")
        finally:
            popup.deleteLater()


class RiskMatrixPopupDismissalTests(unittest.TestCase):
    """"Riskmatrisens popup ska stängas både med Avbryt och när användaren
    klickar utanför popupen" (2026-08-26). Switched from an application-
    modal QDialog shown via exec() to Qt.WindowType.Popup shown via
    show() -- the same window type QMenu/QComboBox use for their own
    dropdowns, which Qt closes automatically the instant a click lands
    outside its geometry, on top of the existing Cancel/Escape handling."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_uses_popup_window_type_not_a_modal_dialog(self):
        from hazop import RiskMatrixPopup
        popup = RiskMatrixPopup(current_freq=2, current_cons=3)
        try:
            self.assertTrue(bool(popup.windowFlags() & Qt.WindowType.Popup),
                "must use Qt.WindowType.Popup so outside clicks auto-dismiss it")
        finally:
            popup.deleteLater()

    def test_cancel_button_closes_the_popup(self):
        from hazop import RiskMatrixPopup
        from PyQt6.QtWidgets import QPushButton
        popup = RiskMatrixPopup(current_freq=2, current_cons=3)
        try:
            popup.show()
            self.assertTrue(popup.isVisible())
            cancel_btn = next(b for b in popup.findChildren(QPushButton) if b.text() == 'Avbryt')
            cancel_btn.click()
            self.assertFalse(popup.isVisible())
        finally:
            popup.deleteLater()

    # NOTE: an end-to-end "does a real click outside the popup dismiss it"
    # test was tried and dropped -- the offscreen QPA platform this suite
    # runs under does not implement mouse/keyboard grabbing ("This plugin
    # does not support grabbing the keyboard"), which is exactly the OS-
    # level mechanism Qt.WindowType.Popup's outside-click dismissal relies
    # on, so it can never pass headless regardless of correctness. The
    # window-flag test above is what's actually within this suite's power
    # to verify; the dismissal behavior itself is Qt's own well-established
    # built-in Popup semantics (same as every QMenu/QComboBox already
    # relies on), not new code written here.


class RiskMatrixCategorySectionTests(unittest.TestCase):
    """"Ta bort kategori-/C-värdesvalet från konsekvensfältet i HAZOP
    Scenario... Flytta detta till riskmatrisen. Där ska användaren kunna
    ange konsekvensnivå separat per kategori... och se respektive
    position i matrisen. Frekvens hämtas från orsaken." (2026-08-26, see
    NOTES.md "Flytta konsekvenskategori till riskmatrisen") —
    RiskMatrixPopup now optionally hosts the per-category severity
    picker that used to live behind the KON cell's "📊" badge
    (ConsCategoryMatrixPopup, still used unchanged from the P&ID
    node-markup ribbon) when constructed with db=/cons_id=."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_riskmatrix_cat_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.cons_id = self.db.add_consequence(cause_id)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_without_db_or_cons_id_no_category_section_is_built(self):
        """Backward compat — every pre-existing call site/test that
        doesn't pass db/cons_id (e.g. RiskMatrixPopupDismissalTests
        above) must keep working exactly as before."""
        from hazop import RiskMatrixPopup
        popup = RiskMatrixPopup(current_freq=2, current_cons=3)
        try:
            self.assertFalse(hasattr(popup, '_cat_buttons'))
        finally:
            popup.deleteLater()

    def test_with_db_and_cons_id_shows_one_row_per_category(self):
        from hazop import RiskMatrixPopup
        popup = RiskMatrixPopup(current_freq=2, current_cons=3,
                                 db=self.db, cons_id=self.cons_id)
        try:
            cats = self.db.consequence_categories()
            self.assertTrue(cats)
            cat_ids = {c['id'] for c in cats}
            button_cat_ids = {cid for (cid, _sev) in popup._cat_buttons}
            self.assertEqual(button_cat_ids, cat_ids)
        finally:
            popup.deleteLater()

    def test_clicking_a_severity_button_saves_immediately_and_emits_signal(self):
        from hazop import RiskMatrixPopup
        popup = RiskMatrixPopup(current_freq=2, current_cons=3,
                                 db=self.db, cons_id=self.cons_id)
        try:
            cat = self.db.consequence_categories()[0]
            emitted = []
            popup.category_changed.connect(lambda: emitted.append(True))
            popup._cat_buttons[(cat['id'], 5)].click()

            saved = {r['category_id']: r['severity']
                     for r in self.db.get_consequence_severities(self.cons_id)}
            self.assertEqual(saved.get(cat['id']), 5)
            self.assertEqual(emitted, [True])
        finally:
            popup.deleteLater()

    def test_clicking_the_same_severity_again_clears_it(self):
        from hazop import RiskMatrixPopup
        cat = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(self.cons_id, cat['id'], 4)
        popup = RiskMatrixPopup(current_freq=2, current_cons=3,
                                 db=self.db, cons_id=self.cons_id)
        try:
            popup._cat_buttons[(cat['id'], 4)].click()
            saved = {r['category_id']: r['severity']
                     for r in self.db.get_consequence_severities(self.cons_id)}
            self.assertNotIn(cat['id'], saved)
        finally:
            popup.deleteLater()

    def test_category_at_the_shared_frequency_gets_a_marker_on_the_grid(self):
        """"...och se respektive position i matrisen" — a category's
        current severity must show up as a marker on the matrix cell it
        occupies (always in the shared/from-the-cause frequency column,
        since frequency isn't editable per category here)."""
        from hazop import RiskMatrixPopup
        cat = self.db.consequence_categories()[0]
        popup = RiskMatrixPopup(current_freq=2, current_cons=3,
                                 db=self.db, cons_id=self.cons_id)
        try:
            popup._cat_buttons[(cat['id'], 5)].click()
            btn, _base = popup._grid_buttons[(2, 5)]
            self.assertIn(cat['name'][:3], btn.text())
            # A cell in a DIFFERENT frequency column must never get a
            # marker — frequency always comes from the cause, never
            # settable per category here.
            other_btn, other_base = popup._grid_buttons[(3, 5)]
            self.assertEqual(other_btn.text(), other_base)
        finally:
            popup.deleteLater()

    def test_existing_severities_are_preseeded_as_checked(self):
        from hazop import RiskMatrixPopup
        cats = self.db.consequence_categories()
        self.db.set_consequence_severity(self.cons_id, cats[0]['id'], 3)
        popup = RiskMatrixPopup(current_freq=2, current_cons=3,
                                 db=self.db, cons_id=self.cons_id)
        try:
            self.assertTrue(popup._cat_buttons[(cats[0]['id'], 3)].isChecked())
        finally:
            popup.deleteLater()

    def test_grid_click_does_not_create_uncategorized_assessment(self):
        from hazop import RiskMatrixPopup
        popup = RiskMatrixPopup(current_freq=2, current_cons=3,
                                 db=self.db, cons_id=self.cons_id)
        try:
            emitted = []
            popup.selection_made.connect(lambda *args: emitted.append(args))
            popup._grid_buttons[(2, 4)][0].click()
            self.assertEqual(emitted, [])
            self.assertEqual(self.db.get_consequence_severities(self.cons_id), [])
        finally:
            popup.deleteLater()


class KonCellCategoryBadgeMovedToRiskMatrixTests(unittest.TestCase):
    """The old "📊" category badge at the left of the KON cell (and its
    "Per C5"-style stacked labels) is gone — assigning a consequence
    level per category now happens inside the risk matrix popup (see
    RiskMatrixCategorySectionTests above) instead."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_kon_cat_removed_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_kon_cell_tooltip_no_longer_mentions_the_badge(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        cat = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(cons_id, cat['id'], 4)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            item = panel._table.item(row, panel._C_KON)
            self.assertNotIn('📊', item.toolTip())
        finally:
            panel.deleteLater()

    def test_scenario_panel_no_longer_has_a_kon_category_zone_geometry_helper(self):
        from hazop import ScenarioTablePanel
        self.assertFalse(hasattr(ScenarioTablePanel, '_kon_cat_zone_geometry'))

    def test_clicking_the_risk_cell_opens_a_category_aware_popup(self):
        """The risk-matrix click path (_on_cell_clicked's _C_RFORE
        branch) is the new entry point — it must now pass db/cons_id
        through so the popup it opens can show the per-category
        section (this is what makes the moved feature reachable at
        all, now that the KON badge is gone)."""
        from hazop import ScenarioTablePanel, RiskMatrixPopup
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            captured = {}
            orig_init = RiskMatrixPopup.__init__
            def spy_init(self, *a, **kw):
                captured.update(kw)
                orig_init(self, *a, **kw)
            with unittest.mock.patch.object(RiskMatrixPopup, '__init__', spy_init):
                panel._on_cell_clicked(row, panel._C_RFORE)
            self.assertIs(captured.get('db'), self.db)
            self.assertEqual(captured.get('cons_id'), cons_id)
        finally:
            panel.deleteLater()

    def test_category_prefix_is_shown_and_slut_has_no_step_text(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, likelihood=3)
        cons_id = self.db.add_consequence(cause_id)
        cat = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(cons_id, cat['id'], 4)
        sg_id = self.db.add_safeguard(cons_id)
        self.db.update_safeguard(sg_id, rrf=10)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            prefix = cat['name'][:3]
            self.assertTrue(panel._table.item(row, panel._C_RFORE).text().startswith(prefix))
            slut_text = panel._table.item(row, panel._C_SLUT).text()
            self.assertTrue(slut_text.startswith(prefix))
            self.assertNotIn('steg', slut_text.lower())
        finally:
            panel.deleteLater()


class TooltipContrastTests(unittest.TestCase):
    """Tooltip appearance is application-global, never copied into local QSS."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_tooltip_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_scenario_table_has_no_local_tooltip_override(self):
        from hazop import ScenarioTablePanel
        panel = ScenarioTablePanel(self.db)
        try:
            ss = panel._table.styleSheet()
            self.assertNotIn('QToolTip', ss)
        finally:
            panel.deleteLater()

    def test_risk_matrix_buttons_have_no_local_tooltip_override(self):
        from hazop import RiskMatrixPopup
        from PyQt6.QtWidgets import QPushButton
        popup = RiskMatrixPopup(current_freq=2, current_cons=3)
        try:
            buttons = [b for b in popup.findChildren(QPushButton) if b.toolTip()]
            self.assertTrue(buttons)
            for btn in buttons:
                self.assertNotIn('QToolTip', btn.styleSheet())
        finally:
            popup.deleteLater()


class SafeguardEditorTopAlignmentTests(unittest.TestCase):
    """SG editing uses the wrapped text area and leaves the RRF badge clear."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_sgtopalign_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_sg_editor_geometry_uses_wrapped_text_area_on_a_tall_row(self):
        from hazop import ScenarioTablePanel
        from scenario_panel import _BoldTagTextEdit
        from PyQt6.QtWidgets import QStyleOptionViewItem
        from PyQt6.QtCore import QRect

        panel = ScenarioTablePanel(self.db)
        try:
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            # Long enough to word-wrap across many lines at any realistic
            # column width, forcing this physical row much taller than a
            # single line -- exactly the case that used to make the SG
            # editor's text jump to vertical center.
            self.db.update_cause(cause_id, description="Lång orsakstext " * 30)
            cons_id = self.db.add_consequence(cause_id)
            sg_id = self.db.add_safeguard(cons_id)
            panel.load_node(node_id)

            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)
            row_h = panel._table.rowHeight(row)
            sg_h  = panel._sg_row_height(panel._table.font())
            self.assertGreater(row_h, sg_h,
                "sanity check: the row must actually be taller than one "
                "SG line, or this test isn't exercising the bug at all")

            index = panel._table.model().index(row, panel._C_SG)
            option = QStyleOptionViewItem()
            option.rect = QRect(0, 0, 200, row_h)
            option.font = panel._table.font()
            editor = _BoldTagTextEdit(panel._table)
            try:
                panel._pid_delegate.updateEditorGeometry(editor, option, index)
                geo = editor.geometry()
                self.assertEqual(geo.top(), option.rect.top() + 1,
                    "the editor must stay anchored to the TOP of the cell")
                self.assertEqual(geo.height(), row_h - 2,
                    "the multiline editor must use the available row height")
                self.assertEqual(geo.width(), 200 - 32 - 4,
                    "the editor must leave the RRF badge area untouched")
            finally:
                editor.deleteLater()
        finally:
            panel.deleteLater()


class PopupsPreferOpeningAboveTheirFieldTests(unittest.TestCase):
    """"Alla mindre popup-rutor och dropdowns i HAZOP Scenario ska öppnas
    ovanför sitt fält istället för nedanför." (2026-08-26). Every
    manually-positioned popup in scenario_panel.py used to anchor BELOW
    its cell/click point first, only flipping above if below ran off
    screen (or, for a few, not flipping at all). All of them were
    rewritten to prefer above, falling back to below only when there's
    genuinely no room above on screen -- these tests cover the shared
    `_pos_near_cons_row` helper (reused by the chain editor,
    the recommendation editor, and MainWindow.position_near_row) plus
    two of the cursor/click-anchored popups as representative examples
    of the same pattern applied throughout the file."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_popupabove_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.panel.resize(900, 600)
        self.panel.show()
        self.node_id = self.db.add_node()
        dev_id = self.db.deviations(self.node_id)[0]['id']
        self.cause_id = self.db.add_cause(dev_id)
        self.cons_id = self.db.add_consequence(self.cause_id)
        self.panel.load_node(self.node_id)
        self.app.processEvents()

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _kon_cell_top_global(self):
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id)
        rect = self.panel._table.visualRect(
            self.panel._table.model().index(row, self.panel._C_KON))
        return self.panel._table.viewport().mapToGlobal(rect.topLeft()), rect.height()

    def test_pos_near_cons_row_prefers_above_when_it_fits(self):
        from PyQt6.QtCore import QSize
        top, _h = self._kon_cell_top_global()
        pos = self.panel._pos_near_cons_row(self.cons_id, QSize(300, 40))
        self.assertLess(pos.y(), top.y(),
            "a small enough popup must open ABOVE the KON cell, not below")

    def test_pos_near_cons_row_falls_back_below_when_it_does_not_fit(self):
        from PyQt6.QtCore import QSize
        top, h = self._kon_cell_top_global()
        pos = self.panel._pos_near_cons_row(self.cons_id, QSize(300, 200))
        self.assertGreaterEqual(pos.y(), top.y() + h,
            "a popup too tall to fit above must fall back to below the cell")

    def test_comment_popup_prefers_above_the_click_point(self):
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QDialog
        click = QPoint(400, 400)   # comfortably away from every screen edge
        captured = {}

        def _capture_move(self_popup, x, y):
            captured['pos'] = (x, y)

        with unittest.mock.patch.object(QDialog, 'move', _capture_move), \
             unittest.mock.patch.object(QDialog, 'exec', return_value=QDialog.DialogCode.Rejected):
            self.panel._open_comment_popup(0, self.cause_id, click)
        self.assertIn('pos', captured, "popup.move() must have been called")
        self.assertLess(captured['pos'][1], click.y(),
            "the comment popup must open ABOVE the click point, not below")

    def test_cat_sg_popup_prefers_above_the_cursor(self):
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QDialog
        from scenario_panel import CatSGSelectionPopup
        cursor_pos = QPoint(400, 400)
        sg_id = self.db.add_safeguard(self.cons_id)
        captured = {}

        def _capture_move(self_popup, x, y):
            captured['pos'] = (x, y)

        with unittest.mock.patch('scenario_panel.QCursor.pos', return_value=cursor_pos), \
             unittest.mock.patch.object(QDialog, 'exec', return_value=QDialog.DialogCode.Rejected), \
             unittest.mock.patch.object(CatSGSelectionPopup, 'move', _capture_move):
            self.panel._show_cat_sg_popup(1, [{'id': sg_id, 'description': 'SG', 'rrf': 1}])
        self.assertIn('pos', captured, "popup.move() must have been called")
        self.assertLess(captured['pos'][1], cursor_pos.y(),
            "the safeguard-selection popup must open ABOVE the cursor, not below")


class SafeguardRRFBadgeHeaderTests(unittest.TestCase):
    """"rrf rutan på safeguard blir väldigt hög. gör denna lägre genom
    att ta bort rrf och låta det stå i kolumneubriken istället."
    (2026-08-14) — the badge text used to be a two-line "RRF\\n{rrf}",
    which forced the badge box (and thus visually the cell) taller than
    a plain number needs. Move the "RRF" label into the column header
    instead and paint just the number in the cell."""

    def test_safeguard_badge_paints_only_the_number(self):
        import inspect
        import hazop as hazop_mod
        src = inspect.getsource(hazop_mod._PidDelegate.paint)
        self.assertNotIn('RRF\\n{rrf}', src,
            "SG badge must no longer draw the two-line 'RRF\\n<value>' text")
        self.assertIn('f"{rrf}"', src,
            "SG badge should paint just the bare RRF number")

    def test_barriarer_column_header_mentions_rrf(self):
        from hazop import ScenarioTablePanel
        self.assertIn('RRF', ScenarioTablePanel._COLS[ScenarioTablePanel._C_SG],
            "since the cell badge no longer spells out 'RRF', the column header must")


class SafeguardObjectFeatureRemovedTests(unittest.TestCase):
    """The safeguard emoji/object-picker was removed and archived 2026-08-27."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_sgobj_removed_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.sg_id = self.db.add_safeguard(cons_id)
        self.panel.load_node(node_id)
        self.row = next(r for r, m in enumerate(self.panel._row_meta)
                        if m[3] == self.sg_id)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_picker_class_and_click_entry_points_are_absent(self):
        import scenario_panel
        self.assertFalse(hasattr(scenario_panel, 'SafeguardObjectPopup'))
        self.assertFalse(hasattr(self.panel, '_show_sg_object_popup_at'))
        self.assertFalse(hasattr(self.panel, '_sg_icon_zone_geometry'))

    def test_safeguard_tooltip_no_longer_mentions_emoji_picker(self):
        item = self.panel._table.item(self.row, self.panel._C_SG)
        self.assertNotIn('🏷', item.toolTip())
        self.assertNotIn('välja P&ID-objekt', item.toolTip())
        self.assertIn('RRF', item.toolTip())

    def test_safeguard_painter_no_longer_draws_object_emoji(self):
        import inspect
        from scenario_panel import _PidDelegate
        src = inspect.getsource(_PidDelegate.paint)
        self.assertNotIn('🏷', src)
        self.assertNotIn('_SG_TAG_ICON_ZONE_W', src)


@unittest.skip("Archived safeguard object-picker tests; feature removed 2026-08-27")
class SafeguardObjectPickerTests(unittest.TestCase):
    """Originally 2026-08-19 ("när jag väljer safeguards i hazop
    scenario får jag upp en rullista med objekt ... Du kan även
    inkludera en inställningsknapp ... vilka typer av objekt"), rebuilt
    2026-08-26 ("Riv den nuvarande safeguard-funktionen markerad med
    emoji. ... en sökbar rullgardinslista visa alla taggar/objekt
    definierade på P&ID, sorterade numeriskt. Sökningen ska matcha var
    som helst i taggen"): the gear-button equipment-type filter is gone
    -- the 🏷 icon at the left of the SG cell now opens
    SafeguardObjectPopup showing EVERY P&ID tag, naturally/numerically
    sorted, with a "match anywhere" QCompleter (free text still
    allowed)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_sgobjpicker_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.node_id = self.db.add_node()
        dev_id = self.db.deviations(self.node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.sg_id = self.db.add_safeguard(cons_id)
        self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
        self.db.add_equipment_item("LT-101", "LT-101", "LT", 0, "Instrument", '', 0)
        self.panel.load_node(self.node_id)
        self.row = next(r for r, m in enumerate(self.panel._row_meta) if m[3] == self.sg_id)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _click(self, x, y):
        from PyQt6.QtGui import QMouseEvent
        pos = QPoint(x, y)
        ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                          Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        return self.panel.eventFilter(self.panel._table.viewport(), ev)

    def _cell_rect(self):
        idx = self.panel._table.model().index(self.row, self.panel._C_SG)
        return self.panel._table.visualRect(idx)

    def test_clicking_icon_zone_opens_object_popup_not_rrf_popup(self):
        from scenario_panel import SafeguardObjectPopup
        cr = self._cell_rect()
        with unittest.mock.patch.object(self.panel, '_show_rrf_popup_at') as mock_rrf, \
             unittest.mock.patch.object(self.panel, '_show_sg_object_popup_at') as mock_obj:
            self._click(cr.left() + 2, cr.center().y())
            mock_obj.assert_called_once()
            mock_rrf.assert_not_called()

    def test_clicking_rrf_zone_still_opens_rrf_popup_not_object_popup(self):
        """Regression guard: the new icon zone must not swallow clicks
        that belong to the pre-existing RRF badge zone."""
        cr = self._cell_rect()
        with unittest.mock.patch.object(self.panel, '_show_rrf_popup_at') as mock_rrf, \
             unittest.mock.patch.object(self.panel, '_show_sg_object_popup_at') as mock_obj:
            self._click(cr.right() - 2, cr.center().y())
            mock_rrf.assert_called_once()
            mock_obj.assert_not_called()

    def test_picking_a_known_tag_resolves_its_catalog_type(self):
        from scenario_panel import SafeguardObjectPopup
        popup = SafeguardObjectPopup(self.db, self.sg_id, '', parent=self.panel)
        try:
            popup._combo.setCurrentText("PV-101")
            popup._commit()
            sg = dict(self.db.get_safeguard(self.sg_id))
            self.assertEqual(sg['comp_tag'], 'PV-101')
            self.assertEqual(sg['comp_type'], 'Ventil')
        finally:
            popup.deleteLater()

    def test_free_text_with_no_catalog_match_leaves_type_blank(self):
        from scenario_panel import SafeguardObjectPopup
        popup = SafeguardObjectPopup(self.db, self.sg_id, '', parent=self.panel)
        try:
            popup._combo.setCurrentText("Ett eget objekt")
            popup._commit()
            sg = dict(self.db.get_safeguard(self.sg_id))
            self.assertEqual(sg['comp_tag'], 'Ett eget objekt')
            self.assertEqual(sg['comp_type'], '')
        finally:
            popup.deleteLater()

    def test_picking_none_option_clears_the_tag(self):
        from scenario_panel import SafeguardObjectPopup
        self.db.set_safeguard_tag(self.sg_id, 'PV-101', 'Ventil')
        popup = SafeguardObjectPopup(self.db, self.sg_id, 'PV-101', parent=self.panel)
        try:
            popup._combo.setCurrentIndex(0)   # "— Inget objekt —"
            popup._commit()
            sg = dict(self.db.get_safeguard(self.sg_id))
            self.assertEqual(sg['comp_tag'], '')
            self.assertEqual(sg['comp_type'], '')
        finally:
            popup.deleteLater()

    def test_picking_an_object_does_not_touch_the_free_text_description(self):
        """Deliberately uses set_safeguard_tag, not append_tag_to_safeguard
        — re-picking a different object must not leave old tag
        fragments behind in the description (unlike the drag-and-drop
        gesture, which intentionally DOES build a running sentence)."""
        from scenario_panel import SafeguardObjectPopup
        self.db.update_safeguard(self.sg_id, description="Min egen text")
        popup = SafeguardObjectPopup(self.db, self.sg_id, '', parent=self.panel)
        try:
            popup._combo.setCurrentText("PV-101")
            popup._commit()
            sg = dict(self.db.get_safeguard(self.sg_id))
            self.assertEqual(sg['description'], "Min egen text")
        finally:
            popup.deleteLater()

    def test_dropdown_lists_every_tag_no_type_filter_exists_anymore(self):
        """"Riv den nuvarande safeguard-funktionen markerad med emoji"
        (2026-08-26) tore out the gear-button equipment-type filter
        entirely -- the dropdown must always list every P&ID tag,
        regardless of type."""
        from scenario_panel import SafeguardObjectPopup
        popup = SafeguardObjectPopup(self.db, self.sg_id, '', parent=self.panel)
        try:
            items = [popup._combo.itemData(i) for i in range(popup._combo.count())]
            self.assertIn('LT-101', items)
            self.assertIn('PV-101', items)
            self.assertFalse(hasattr(popup, '_allowed_types'),
                "the type-filter mechanism must be gone, not just unused")
        finally:
            popup.deleteLater()

    def test_dropdown_is_sorted_numerically_not_lexicographically(self):
        """"sorterade numeriskt" (2026-08-26) — 'O2-PI123' must sort
        before 'O10-PI123', unlike plain string order (where '1' < '2'
        puts "O10" ahead of "O2")."""
        from scenario_panel import SafeguardObjectPopup
        self.db.add_equipment_item("O10-PI123", "O10-PI123", "PI", 0, "Instrument", '', 0)
        self.db.add_equipment_item("O2-PI123", "O2-PI123", "PI", 0, "Instrument", '', 0)
        popup = SafeguardObjectPopup(self.db, self.sg_id, '', parent=self.panel)
        try:
            items = [popup._combo.itemData(i) for i in range(popup._combo.count())]
            self.assertLess(items.index('O2-PI123'), items.index('O10-PI123'),
                "O2-PI123 must sort before O10-PI123 (numeric), not after (string)")
        finally:
            popup.deleteLater()

    def test_search_matches_the_tag_suffix_not_just_a_prefix(self):
        """"Sökningen ska matcha var som helst i taggen, t.ex. PI123 ska
        visa både O1-PI123 och O2-PI123." (2026-08-26)"""
        from scenario_panel import SafeguardObjectPopup
        self.db.add_equipment_item("O1-PI123", "O1-PI123", "PI", 0, "Instrument", '', 0)
        self.db.add_equipment_item("O2-PI123", "O2-PI123", "PI", 0, "Instrument", '', 0)
        popup = SafeguardObjectPopup(self.db, self.sg_id, '', parent=self.panel)
        try:
            completer = popup._combo.completer()
            self.assertEqual(completer.filterMode(), Qt.MatchFlag.MatchContains,
                "must match anywhere in the tag, not just a prefix")
            completer.setCompletionPrefix("PI123")
            matches = {completer.completionModel().index(i, 0).data()
                       for i in range(completer.completionCount())}
            self.assertEqual(matches, {"O1-PI123", "O2-PI123"})
        finally:
            popup.deleteLater()

    def test_sg_cell_paint_does_not_crash_with_no_equipment_catalog(self):
        """Rendering the icon must not assume equipment_catalog has any
        rows — a brand new project with no P&ID objects yet must still
        paint the cell fine."""
        db2_dir = tempfile.mkdtemp(prefix="hazop_sgobjpicker_empty_test_")
        try:
            db2 = Database(path=os.path.join(db2_dir, "empty.db"))
            from hazop import ScenarioTablePanel
            panel2 = ScenarioTablePanel(db2)
            node_id = db2.add_node()
            dev_id = db2.deviations(node_id)[0]['id']
            cause_id = db2.add_cause(dev_id)
            cons_id = db2.add_consequence(cause_id)
            db2.add_safeguard(cons_id)
            panel2.load_node(node_id)
            panel2.resize(900, 400)
            panel2.show()
            self.app.processEvents()
            panel2.hide()
            panel2.deleteLater()
        finally:
            shutil.rmtree(db2_dir, ignore_errors=True)


class OrsStripReworkTests(unittest.TestCase):
    """Anton, print screen `Screenshot 2026-08-18 134727.png`: "Frekvensen
    som står i hazop scenario skall stå längst ut till höger men flyttas
    från objektbannern till orsaksfältet då det hör hemma mer här. Varje
    orsak skall ha en frekvens. Detta gör också att när man står på ett
    objekt i hazop trädet behöver inte dubbla objektbanners visas som
    idag. Du kan även skrota pluppen som syns som grön och orange baserat
    på vad som är ifyllt." (2026-08-18) — three changes to the ORS cell's
    _PidDelegate.paint(): the green/yellow/orange/red fill-status dot is
    gone entirely, the frequency label moved from the tag strip into the
    orsaksfält, and the tag itself is hidden on a row whose object is the
    same as the immediately preceding cause row's — a same-day follow-up
    replaced an initial, too-broad "hide whenever Utrustning is visible"
    rule with this consecutive-repeat one (see the tests below)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_orsrework_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    _TEXT_DARK = (0x17, 0x19, 0x1C)

    def _is_dark(self, px, tol=40):
        return (abs(px.red()   - self._TEXT_DARK[0]) <= tol and
                abs(px.green() - self._TEXT_DARK[1]) <= tol and
                abs(px.blue()  - self._TEXT_DARK[2]) <= tol)

    def _has_pixel_matching(self, image, rgb, tol=30):
        for y in range(image.height()):
            for x in range(image.width()):
                px = image.pixelColor(x, y)
                if (abs(px.red() - rgb[0]) <= tol and abs(px.green() - rgb[1]) <= tol
                        and abs(px.blue() - rgb[2]) <= tol):
                    return True
        return False

    def _render_ors_cell(self, panel, row):
        panel._table.setColumnWidth(panel._C_ORS, 300)
        panel.resize(900, 400)
        panel.show()
        self.app.processEvents()
        panel._resize_rows_manual()
        self.app.processEvents()
        index = panel._table.model().index(row, panel._C_ORS)
        cell_rect = panel._table.visualRect(index)
        pixmap = panel._table.viewport().grab(cell_rect)
        panel.hide()
        return pixmap.toImage()

    def test_status_dot_is_never_drawn(self):
        """A cause with NO consequence at all used to draw a red
        (#dc2626) fill-status dot, and one with every field filled in
        drew green (#16a34a) — neither should appear anywhere in the
        rendered cell anymore."""
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)   # no consequence -> old code drew red
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            image = self._render_ors_cell(panel, row)
            self.assertFalse(self._has_pixel_matching(image, (0xdc, 0x26, 0x26)),
                "no red fill-status dot should be drawn")
            self.assertFalse(self._has_pixel_matching(image, (0x16, 0xa3, 0x4a)),
                "no green fill-status dot should be drawn")
        finally:
            panel.deleteLater()

    def test_tag_shown_regardless_of_utrustning_visibility_when_it_is_the_only_occurrence(self):
        """2026-08-18 follow-up ("Orsaken har tidigare visat objekt-tagen
        i bannern men denna är nu borttagen. Jag vill att denna syns."):
        an earlier version of this fix hid the ORS tag banner whenever
        the Utrustning column was merely VISIBLE — that hid it even in
        views (e.g. clicking an object's own P&ID marker, load_all()'s
        "all nodes" mode with no repeat) where it never had anything
        redundant to hide from, so it never showed at all. Column
        visibility is no longer a factor — see the consecutive-repeat
        tests below for the rule that replaced it."""
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, comp_type="Ventil", comp_tag="V-101", equipment_id=eq_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            self.assertTrue(panel._table.isColumnHidden(panel._C_UTR),
                "test assumes load_node() hides the Utrustning column by default")
            item = panel._table.item(row, panel._C_ORS)
            tag_label, show_tag = panel._ors_tag_prefix(item)
            self.assertTrue(show_tag,
                "the tag prefix must be shown when Utrustning is hidden")
            self.assertEqual(tag_label, "V-101")

            panel.load_all()
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            self.assertTrue(panel._table.isColumnHidden(panel._C_UTR),
                "Utrustning must remain retired in load_all()")
            item = panel._table.item(row, panel._C_ORS)
            tag_label, show_tag = panel._ors_tag_prefix(item)
            self.assertTrue(show_tag,
                "the tag prefix must ALSO be shown when Utrustning is visible, as long "
                "as it isn't a repeat of the immediately preceding row")
            self.assertEqual(tag_label, "V-101")
        finally:
            panel.deleteLater()

    def test_tag_hidden_only_on_a_consecutive_repeat_of_the_same_object(self):
        """2026-08-18 follow-up ("om det visas flera avikelser efter
        varandra som tillhör samma objekttagg behöver denna inte
        repeteras ... tagbannern [kan] försvinna på nummer två i listan
        och nedåt"): the tag banner is hidden only when this row's object
        is the SAME as the immediately preceding cause row's — not tied
        to Utrustning-column visibility at all anymore."""
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        devs = self.db.deviations(node_id)
        c1 = self.db.add_cause(devs[0]['id'])
        self.db.update_cause(c1, comp_type="Ventil", comp_tag="V-1")
        c2 = self.db.add_cause(devs[1]['id'])
        self.db.update_cause(c2, comp_type="Ventil", comp_tag="V-1")   # same tag, consecutive
        c3 = self.db.add_cause(devs[2]['id'])
        self.db.update_cause(c3, comp_type="Pump", comp_tag="P-1")     # different tag
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row1 = next(r for r, m in enumerate(panel._row_meta) if m[1] == c1)
            row2 = next(r for r, m in enumerate(panel._row_meta) if m[1] == c2)
            row3 = next(r for r, m in enumerate(panel._row_meta) if m[1] == c3)

            def _tag_visible(row):
                item = panel._table.item(row, panel._C_ORS)
                _tag_label, show_tag = panel._ors_tag_prefix(item)
                return show_tag

            self.assertTrue(_tag_visible(row1),
                "first occurrence of V-1 must show its tag prefix")
            self.assertFalse(_tag_visible(row2),
                "an immediate repeat of the same object (V-1) must not repeat the prefix")
            self.assertTrue(_tag_visible(row3),
                "a different object (P-1) right after must show its own tag prefix")
        finally:
            panel.deleteLater()

    def test_frequency_renders_centered_on_the_cells_first_line(self):
        """2026-08-18 follow-up ("hamnar nu på olika rader vilket tar
        onödigt mycket plats"): the frequency doesn't get its own
        reserved row — it floats centered in its compact badge on the first line,
        the SAME line the (now inline) tag prefix and description text
        start on (2026-08-25, see NOTES.md "Slå ihop objektbaren i
        Orsak-kolumnen" — there's no more separate tag strip for it to
        sit "below" anymore, both share one first line)."""
        from hazop import ScenarioTablePanel, _ORS_FIRST_LINE_H
        from scenario_panel import _RRF_W
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, base_frequency=0.5)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            image = self._render_ors_cell(panel, row)

            # The frequency badge is deliberately compact and shares the
            # RRF width; inspect only that zone rather than an old 60px strip.
            right_x_range = range(image.width() - _RRF_W + 2, image.width() - 2)
            freq_zone_has_text = any(
                self._is_dark(image.pixelColor(x, y))
                for x in right_x_range for y in range(0, _ORS_FIRST_LINE_H))
            self.assertTrue(freq_zone_has_text,
                "frequency text must be drawn right-aligned on the cell's first line")

            below_first_line_has_text = any(
                self._is_dark(image.pixelColor(x, y))
                for x in right_x_range
                for y in range(_ORS_FIRST_LINE_H, _ORS_FIRST_LINE_H + 10))
            self.assertFalse(below_first_line_has_text,
                "frequency must not spill onto a second line")
        finally:
            panel.deleteLater()


class ScenarioTablePanelBuildRowsQueryBatchingTests(unittest.TestCase):
    """ScenarioTablePanel._build_rows() used to issue a query per cause
    (get_node, consequences), per consequence (safeguards,
    get_consequence_severities, and — inside _add_row(), once per
    RENDERED ROW rather than once per consequence — reduction_factors),
    per category row (get_severity_excluded_sgs), and per safeguard
    (get_safeguard_excluded_causes, and the ORS status icon's
    consequences()/safeguards_for_cause() re-fetch). Batched into a
    handful of bulk queries total (2026-08-24, see NOTES.md,
    Database._fetch_grouped) — this locks in that "Visa samtliga noder"
    mode's query count stays bounded as the study grows."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_buildrowsbatch_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _add_full_chains(self, n_nodes, causes_per_node=2):
        cat_id = self.db.add_category('Person')
        for _ in range(n_nodes):
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            for _ in range(causes_per_node):
                cause_id = self.db.add_cause(dev_id)
                cons_id = self.db.add_consequence(cause_id)
                self.db.set_consequence_severity(cons_id, cat_id, 3)
                sg_id = self.db.add_safeguard(cons_id)
                self.db.add_reduction_factor(cons_id, 'test', 10)
                self.db.set_severity_excluded_sgs(
                    self.db.get_consequence_severities(cons_id)[0]['id'], [])
                self.db.set_safeguard_excluded_causes(sg_id, [])

    def test_load_all_query_count_does_not_scale_with_study_size(self):
        self._add_full_chains(n_nodes=2, causes_per_node=2)
        small_count = count_selects(self.db, self.panel.load_all)

        self._add_full_chains(n_nodes=15, causes_per_node=2)
        large_count = count_selects(self.db, self.panel.load_all)

        self.assertLess(large_count, small_count + 20,
            f"_build_rows() SELECT count grew with study size ({small_count} "
            f"-> {large_count}) — the N+1 query pattern may have regressed")


class OrsStandardCausesForRowTests(unittest.TestCase):
    """ScenarioTablePanel._ors_standard_causes_for_row (2026-08-25, see
    NOTES.md "Standardorsak-popup vid redigering av Orsak-cellen") —
    the shared dev_id -> std_dev_id -> object_id resolution chain both
    _attach_cause_completer and StandardCauseSuggestPopup draw from.
    Exercises the same three-step fallback cascade
    CauseObjectPopup._rebuild_causes (tree_panel.py) already uses, one
    step at a time."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_ors_std_causes_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.node_id = self.db.add_node()
        self.dev_id = self.db.deviations(self.node_id)[0]['id']
        self.dev_description = self.db.get_deviation(self.dev_id)['description']

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_row(self, comp_type=''):
        cause_id = self.db.add_cause(self.dev_id)
        if comp_type:
            self.db.update_cause(cause_id, comp_type=comp_type, comp_tag='V-1')
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == cause_id)
        return row, cause_id

    def test_object_hierarchy_match_is_preferred(self):
        """Best case: a standard_deviations row with the same text AND
        a standard_causes row scoped to (that deviation, that object)."""
        std_dev_id = self.db.conn.execute(
            "INSERT INTO standard_deviations (description) VALUES (?)",
            (self.dev_description,)).lastrowid
        self.db.commit()
        obj_id = self.db.add_standard_object("Xyzzyobjekt")
        self.db.add_standard_cause_with_object(std_dev_id, obj_id, "Via objekt-hierarki")

        row, _ = self._make_row(comp_type="Xyzzyobjekt")
        _std_dev_id, comp_type, dev_desc, rows = \
            self.panel._ors_standard_causes_for_row(row)
        self.assertEqual(comp_type, "Xyzzyobjekt")
        self.assertEqual(dev_desc, self.dev_description)
        self.assertEqual([r['description'] for r in rows], ["Via objekt-hierarki"])

    def test_falls_back_to_comp_type_plus_deviation_text(self):
        """A standard_deviations row matching this deviation's text
        exists (so std_dev_id resolves), but no standard_objects row
        named "Xyzzyobjekt" exists — the object-hierarchy step (step 1)
        therefore can't resolve an object_id and fails, falling back to
        a plain comp_type + deviation-text match (step 2) instead."""
        std_dev_id = self.db.conn.execute(
            "INSERT INTO standard_deviations (description) VALUES (?)",
            (self.dev_description,)).lastrowid
        self.db.conn.execute(
            "INSERT INTO standard_causes (deviation_id, description, comp_type) "
            "VALUES (?, 'Via comp_type+avvikelse', 'Xyzzyobjekt')", (std_dev_id,))
        self.db.commit()

        row, _ = self._make_row(comp_type="Xyzzyobjekt")
        _std_dev_id, comp_type, dev_desc, rows = \
            self.panel._ors_standard_causes_for_row(row)
        self.assertEqual([r['description'] for r in rows], ["Via comp_type+avvikelse"])

    def test_falls_back_to_comp_type_with_no_deviation_filter(self):
        """Neither the object hierarchy nor a deviation-text match apply
        — only a bare comp_type match, from a standard_causes row tied
        to some OTHER deviation entirely."""
        other_dev_id = self.db.conn.execute(
            "INSERT INTO standard_deviations (description) VALUES ('Ett helt annat ord')"
        ).lastrowid
        self.db.commit()
        self.db.conn.execute(
            "INSERT INTO standard_causes (deviation_id, description, comp_type) "
            "VALUES (?, 'Via bar comp_type', 'Xyzzyobjekt')", (other_dev_id,))
        self.db.commit()

        row, _ = self._make_row(comp_type="Xyzzyobjekt")
        _std_dev_id, comp_type, dev_desc, rows = \
            self.panel._ors_standard_causes_for_row(row)
        self.assertEqual([r['description'] for r in rows], ["Via bar comp_type"])

    def test_no_comp_type_and_no_match_returns_empty(self):
        row, _ = self._make_row(comp_type='')
        _std_dev_id, comp_type, dev_desc, rows = \
            self.panel._ors_standard_causes_for_row(row)
        self.assertEqual(comp_type, '')
        self.assertEqual(rows, [])

    def test_unknown_comp_type_with_no_matching_standard_cause_returns_empty(self):
        row, _ = self._make_row(comp_type="Okänd typ som inte finns")
        _std_dev_id, comp_type, dev_desc, rows = \
            self.panel._ors_standard_causes_for_row(row)
        self.assertEqual(rows, [])


class CauseCompleterFallbackTests(unittest.TestCase):
    """_attach_cause_completer (scenario_panel.py) rewritten 2026-08-25
    to call the new shared _ors_standard_causes_for_row instead of
    duplicating its own copy of the resolution chain — these tests lock
    in that the completer's own EXTRA, wider fallback ("suggest every
    standard cause description in the whole database" when nothing more
    specific matches) survived the refactor unchanged. That extra step
    is deliberately NOT part of the shared helper itself (it would make
    StandardCauseSuggestPopup's button list unusably long), so it must
    still live in this method alone."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_completer_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.node_id = self.db.add_node()
        self.dev_id = self.db.deviations(self.node_id)[0]['id']

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _start_edit_and_get_editor(self, cause_id):
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == cause_id)
        idx = self.panel._table.model().index(row, self.panel._C_ORS)
        self.panel._table.setCurrentIndex(idx)
        self.panel._table.edit(idx)
        self.app.processEvents()
        from scenario_panel import _BoldTagTextEdit
        editors = [w for w in self.panel._table.viewport().findChildren(_BoldTagTextEdit)
                   if w.property('editing_row') == row]
        return editors[0] if editors else None

    def test_completer_falls_back_to_every_standard_cause_when_nothing_matches(self):
        self.db.conn.execute(
            "INSERT INTO standard_deviations (description) VALUES ('Helt orelaterat')")
        self.db.conn.execute(
            "INSERT INTO standard_causes (deviation_id, description) "
            "VALUES ((SELECT id FROM standard_deviations WHERE description='Helt orelaterat'), "
            "'Global standardorsak')")
        self.db.commit()

        cause_id = self.db.add_cause(self.dev_id)   # no comp_type at all
        editor = self._start_edit_and_get_editor(cause_id)
        self.assertIsNotNone(editor)
        completer = editor.completer()
        self.assertIsNotNone(completer, "completer must still be attached")
        model = completer.model()
        descs = [model.index(i, 0).data() for i in range(model.rowCount())]
        self.assertIn('Global standardorsak', descs,
            "completer's own wider fallback must still fire when the shared "
            "helper's narrower cascade finds nothing")

    def test_completer_uses_narrow_cascade_result_when_available(self):
        std_dev_id = self.db.conn.execute(
            "INSERT INTO standard_deviations (description) VALUES (?)",
            (self.db.get_deviation(self.dev_id)['description'],)).lastrowid
        self.db.commit()
        obj_id = self.db.add_standard_object("Xyzzyobjekt")
        self.db.add_standard_cause_with_object(std_dev_id, obj_id, "Specifik träff")
        # An unrelated global cause that must NOT appear once a specific match exists
        self.db.conn.execute(
            "INSERT INTO standard_deviations (description) VALUES ('Helt orelaterat')")
        self.db.conn.execute(
            "INSERT INTO standard_causes (deviation_id, description) "
            "VALUES ((SELECT id FROM standard_deviations WHERE description='Helt orelaterat'), "
            "'Bör inte synas')")
        self.db.commit()

        cause_id = self.db.add_cause(self.dev_id)
        self.db.update_cause(cause_id, comp_type="Xyzzyobjekt", comp_tag="V-1")
        editor = self._start_edit_and_get_editor(cause_id)
        completer = editor.completer()
        model = completer.model()
        descs = [model.index(i, 0).data() for i in range(model.rowCount())]
        self.assertEqual(descs, ["Specifik träff"])


class ConsequenceHistoryAutocompleteTests(unittest.TestCase):
    """"Spara varje konsekvens som skrivs i HAZOP Scenario i en databas.
    Vid redigering ska en rullgardinslista visa tidigare konsekvenser.
    Filtrera listan direkt när användaren skriver, case-insensitive,
    baserat på att texten börjar med det inskrivna värdet." (2026-08-26)"""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_conshistcompleter_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.node_id = self.db.add_node()
        self.dev_id = self.db.deviations(self.node_id)[0]['id']
        self.cause_id = self.db.add_cause(self.dev_id)
        self.cons_id = self.db.add_consequence(self.cause_id)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _row_for_cons(self):
        return next(r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id)

    def test_committing_a_kon_cell_edit_saves_it_to_history(self):
        self.panel.load_node(self.node_id)
        row = self._row_for_cons()
        item = self.panel._table.item(row, self.panel._C_KON)
        item.setText("Hög nivå i tanken")
        self.panel._on_cell_changed_inner(row, self.panel._C_KON)
        self.assertIn("Hög nivå i tanken", self.db.consequence_history())

    def test_blank_kon_edit_does_not_pollute_history(self):
        self.panel.load_node(self.node_id)
        row = self._row_for_cons()
        item = self.panel._table.item(row, self.panel._C_KON)
        item.setText("")
        self.panel._on_cell_changed_inner(row, self.panel._C_KON)
        self.assertEqual(self.db.consequence_history(), [])

    def _start_edit_and_get_editor(self):
        self.panel.load_node(self.node_id)
        row = self._row_for_cons()
        idx = self.panel._table.model().index(row, self.panel._C_KON)
        self.panel._table.setCurrentIndex(idx)
        self.panel._table.edit(idx)
        self.app.processEvents()
        from scenario_panel import _BoldTagTextEdit
        editors = [w for w in self.panel._table.viewport().findChildren(_BoldTagTextEdit)
                   if w.property('editing_row') == row]
        return editors[0] if editors else None

    def test_kon_editor_gets_a_prefix_matching_completer_from_history(self):
        self.db.add_consequence_history("Hög nivå i tank")
        self.db.add_consequence_history("Högt tryck i rörledning")
        self.db.add_consequence_history("Läckage vid fläns")

        editor = self._start_edit_and_get_editor()
        self.assertIsNotNone(editor)
        completer = editor.completer()
        self.assertIsNotNone(completer, "KON editor must get a completer once history exists")
        self.assertEqual(completer.filterMode(), Qt.MatchFlag.MatchStartsWith,
            "must filter by prefix, not 'contains' like the ORS completer")
        self.assertEqual(completer.caseSensitivity(), Qt.CaseSensitivity.CaseInsensitive)
        model = completer.model()
        descs = {model.index(i, 0).data() for i in range(model.rowCount())}
        self.assertEqual(descs, {"Hög nivå i tank", "Högt tryck i rörledning", "Läckage vid fläns"})

    def test_kon_editor_has_no_completer_when_history_is_empty(self):
        editor = self._start_edit_and_get_editor()
        self.assertIsNotNone(editor)
        self.assertIsNone(editor.completer())


class StandardCauseSuggestPopupTests(unittest.TestCase):
    """The popup itself (2026-08-25, see NOTES.md "Standardorsak-popup
    vid redigering av Orsak-cellen") — Anton: "När jag vill editera
    orsakstexten och står i editerarläget vill jag även att det dyker
    upp en liten popupruta (som inte täcker cellen) ... jag skall kunna
    välja bland de 'standard'-orsaker som finns för objektypen och
    avikelsen. Denna popupruta behöver bara innehålla detta samt
    möjlighet att editera frekvens genom att klicka på frekvensen."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_stdcausepopup_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.panel.resize(900, 500)
        self.panel.show()
        self.node_id = self.db.add_node()
        self.dev_id = self.db.deviations(self.node_id)[0]['id']
        self.dev_description = self.db.get_deviation(self.dev_id)['description']
        self.std_dev_id = self.db.conn.execute(
            "INSERT INTO standard_deviations (description) VALUES (?)",
            (self.dev_description,)).lastrowid
        self.db.commit()
        self.obj_id = self.db.add_standard_object("Xyzzyobjekt")

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_cause(self, comp_type="Xyzzyobjekt", comp_tag="V-101"):
        cause_id = self.db.add_cause(self.dev_id)
        if comp_type:
            self.db.update_cause(cause_id, comp_type=comp_type, comp_tag=comp_tag)
        return cause_id

    def _start_edit(self, cause_id):
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == cause_id)
        idx = self.panel._table.model().index(row, self.panel._C_ORS)
        self.panel._table.setCurrentIndex(idx)
        self.panel._table.edit(idx)
        self.app.processEvents()
        return row

    def _editor_for_row(self, row):
        from scenario_panel import _BoldTagTextEdit
        editors = [w for w in self.panel._table.viewport().findChildren(_BoldTagTextEdit)
                   if w.property('editing_row') == row]
        return editors[0] if editors else None

    def _popup(self):
        from scenario_panel import StandardCauseSuggestPopup
        found = self.panel.window().findChildren(StandardCauseSuggestPopup)
        return found[0] if found else None

    def test_popup_appears_when_ors_edit_starts(self):
        self.db.add_standard_cause_with_object(self.std_dev_id, self.obj_id, "Felar stängd")
        cause_id = self._make_cause()
        self._start_edit(cause_id)
        popup = self._popup()
        self.assertIsNotNone(popup, "popup must appear as soon as ORS editing starts")
        self.assertTrue(popup.isVisible())

    def test_popup_does_not_appear_for_kon_or_sg_columns(self):
        """Only the Orsak (ORS) cell triggers this popup — editing a
        Konsekvens or Safeguard cell must not show it."""
        cause_id = self._make_cause()
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == cause_id)
        for col in (self.panel._C_KON, self.panel._C_SG):
            idx = self.panel._table.model().index(row, col)
            self.panel._table.setCurrentIndex(idx)
            self.panel._table.edit(idx)
            self.app.processEvents()
            self.assertIsNone(self._popup(),
                f"column {col} must never trigger the standard-cause popup")

    def test_popup_does_not_appear_for_placeholder_row_with_no_cause(self):
        """An empty ORS placeholder row (dev_id set, cause_id None) has
        nothing to attach a description/frequency to — no popup."""
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] is None)
        idx = self.panel._table.model().index(row, self.panel._C_ORS)
        self.panel._table.setCurrentIndex(idx)
        self.panel._table.edit(idx)
        self.app.processEvents()
        self.assertIsNone(self._popup())

    def test_editor_keeps_focus_and_stays_alive_while_popup_is_shown(self):
        """The core regression this feature could easily introduce:
        showing ANY new widget/window while an editor is open must not
        cause Qt's default FocusOut handling to silently commit+close
        it. Confirmed empirically during development that a naive
        separate-top-level-window popup DID trigger exactly this."""
        self.db.add_standard_cause_with_object(self.std_dev_id, self.obj_id, "Felar stängd")
        cause_id = self._make_cause()
        row = self._start_edit(cause_id)
        editor = self._editor_for_row(row)
        self.assertIsNotNone(editor, "editor must exist right after starting the edit")

        # Give any stray deferred/timer-based teardown a chance to run.
        for _ in range(5):
            self.app.processEvents()

        editor_again = self._editor_for_row(row)
        self.assertIsNotNone(editor_again,
            "the cell editor must still exist after the popup has shown — "
            "it must never be silently closed by the popup merely appearing")
        self.assertTrue(editor_again.hasFocus(),
            "the cell editor must keep keyboard focus while the popup is shown")

    def test_empty_state_shows_message_and_does_not_crash(self):
        cause_id = self._make_cause(comp_type='')   # no object -> no matches possible
        self._start_edit(cause_id)
        popup = self._popup()
        self.assertIsNotNone(popup)
        header = popup.layout().itemAt(0).widget()
        self.assertIn("Ingen standardorsak", header.text())

    def test_picking_a_standard_cause_saves_it_and_closes_the_editor(self):
        self.db.add_standard_cause_with_object(self.std_dev_id, self.obj_id, "Felar stängd")
        cause_id = self._make_cause()
        row = self._start_edit(cause_id)
        popup = self._popup()
        self.assertIsNotNone(popup)

        popup._pick("Felar stängd")
        # closeEditor's actual widget teardown is a deferred (deleteLater)
        # Qt event, not synchronous — a real Enter keypress needs the same
        # settling time (confirmed empirically), so give it a moment
        # rather than asserting immediately after one processEvents().
        from PyQt6.QtTest import QTest
        QTest.qWait(20)

        self.assertIsNone(self._editor_for_row(row),
            "picking a standard cause must close the cell editor")
        self.assertEqual(self.db.get_cause(cause_id)['description'], "Felar stängd")

    def test_clicking_frequency_commits_unconfirmed_text_first(self):
        """Regression guard for the most fragile part of this feature:
        _on_ors_frequency_picked -> _schedule_rebuild() tears down the
        active cell editor as a side effect (ScenarioTablePanel._rebuild()'s
        "Proactively clear focus from any active cell editor" step) — if
        the in-progress description text weren't committed FIRST, that
        teardown would silently discard it."""
        cause_id = self._make_cause()
        row = self._start_edit(cause_id)
        editor = self._editor_for_row(row)
        editor.setText("Ej sparad text")

        popup = self._popup()
        with unittest.mock.patch('scenario_panel.FrequencyPickerPopup.create_positioned') as mk:
            fake_freq_popup = unittest.mock.Mock()
            mk.return_value = fake_freq_popup
            popup._edit_frequency()

        self.app.processEvents()
        self.assertEqual(self.db.get_cause(cause_id)['description'], "Ej sparad text",
            "the description typed before clicking frequency must be saved, not discarded")

    def test_popup_closes_when_editor_is_destroyed(self):
        from PyQt6.QtWidgets import QStyledItemDelegate
        cause_id = self._make_cause()
        row = self._start_edit(cause_id)
        popup = self._popup()
        self.assertIsNotNone(popup)
        editor = self._editor_for_row(row)

        self.panel._delegate.commitData.emit(editor)
        self.panel._delegate.closeEditor.emit(
            editor, QStyledItemDelegate.EndEditHint.NoHint)
        from PyQt6.QtTest import QTest
        QTest.qWait(20)

        self.assertIsNone(self._popup(), "the popup must close once the editor is destroyed")


class InlineIdentityEditTests(unittest.TestCase):
    """Prompt 2: tag edits in KON/SG are guarded before persistence."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="hazop_inline_identity_")
        self.db = Database(path=os.path.join(self.tmpdir, "project.db"))
        from scenario_panel import ScenarioTablePanel, _BoldTagTextEdit
        self.panel = ScenarioTablePanel(self.db)
        self.editor_type = _BoldTagTextEdit
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.cons_id = self.db.add_consequence(cause_id)
        self.sg_id = self.db.add_safeguard(self.cons_id)
        self.db.update_safeguard(self.sg_id, description='Old text')

    def tearDown(self):
        self.panel.deleteLater()
        self.db.conn.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_existing_match_is_connected_without_renaming_old_object(self):
        old_id = self.db.add_equipment_item('PSHH-101', 'PSHH-101', 'PSHH', 1,
                                            'Tryckvakt', '', 0)
        new_id = self.db.add_equipment_item('PSHH-102', 'PSHH-102', 'PSHH', 1,
                                            'Tryckvakt', '', 0)
        self.db.set_consequence_tag(self.cons_id, 'PSHH-101', 'Tryckvakt')
        self.db.update_consequence(self.cons_id, 'PSHH-101 High pressure', 3)
        with unittest.mock.patch.object(self.panel, '_confirm_equipment_tag_change',
                                        return_value='connect'):
            accepted, desc = self.panel._confirm_inline_identity_change(
                'consequence', self.cons_id, 'PSHH-102 High pressure')
        self.assertTrue(accepted)
        self.assertEqual(desc, 'PSHH-102 High pressure')
        self.assertEqual(self.db.get_consequence(self.cons_id)['comp_tag'], 'PSHH-102')
        self.assertEqual(self.db.get_equipment_by_id(old_id)['tag'], 'PSHH-101')
        self.assertEqual(self.db.get_equipment_by_id(new_id)['tag'], 'PSHH-102')

    def test_cancel_does_not_write_the_new_description_or_tag(self):
        self.db.set_safeguard_tag(self.sg_id, 'PSHH-101', 'Tryckvakt')
        self.db.update_safeguard(self.sg_id, description='PSHH-101 Trip')
        with unittest.mock.patch.object(self.panel, '_confirm_equipment_tag_change',
                                        return_value='cancel'):
            accepted, _ = self.panel._confirm_inline_identity_change(
                'safeguard', self.sg_id, 'PSHH-102 Trip')
        self.assertFalse(accepted)
        saved = self.db.get_safeguard(self.sg_id)
        self.assertEqual(saved['comp_tag'], 'PSHH-101')
        self.assertEqual(saved['description'], 'PSHH-101 Trip')

    def test_inline_editors_are_tag_aware(self):
        editor = self.editor_type()
        editor.setText('PSHH-101 Trip')
        editor.set_bold_tags(['PSHH-101'])
        self.assertEqual(editor._bold_tags, ['PSHH-101'])

    def test_bold_refresh_preserves_cursor_selection(self):
        editor = self.editor_type()
        editor.setText('PSHH-101 High pressure trip')
        cursor = editor.textCursor()
        cursor.setPosition(5)
        cursor.setPosition(12, cursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        editor.set_bold_tags(['PSHH-101'])
        self.assertEqual(editor.textCursor().anchor(), 5)
        self.assertEqual(editor.textCursor().position(), 12)

    def test_double_click_caret_is_positioned_without_selection(self):
        editor = self.editor_type(self.panel._table.viewport())
        editor.setGeometry(20, 0, 240, 28)
        editor.setProperty('editing_row', 0)
        editor.setProperty('editing_col', self.panel._C_KON)
        editor.setText('PSHH-101 High pressure trip')
        editor.show()
        self.app.processEvents()
        editor.selectAll()
        self.panel._place_editor_caret(0, self.panel._C_KON, QPoint(20 + 9 * 7, 10))
        self.assertEqual(editor.selectedText(), '')
        self.assertGreater(editor.cursorPosition(), 0)


if __name__ == "__main__":
    unittest.main()
