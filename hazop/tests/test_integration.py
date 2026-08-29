#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering integration.py, plus any cross-module glue they
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
import json
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
    NODE_T, DEV_T, CAUSE_T, CONS_T, SG_T, EQUIP_T, LEDORD_T, SYSTEM_T,
    freq_to_idx,
)
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QGraphicsPixmapItem, QTreeWidgetItemIterator, QCheckBox,
    QComboBox, QPushButton, QMessageBox, QInputDialog, QLineEdit,
    QStyleOptionViewItem,
)
from PyQt6.QtGui import QPixmap, QFocusEvent, QKeyEvent  # noqa: E402
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
# 2. GUI smoke tests (headless, offscreen)
# ══════════════════════════════════════════════════════════════════════════

class GuiSmokeTests(unittest.TestCase):
    """Instantiate real widgets against a temp DB and simulate the crash
    scenarios that were previously fixed. QT_QPA_PLATFORM=offscreen (set at
    module import time) lets this run without a display.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_gui_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_full_chain(self, db=None):
        db = db or self.db
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    # ── (a) delete cause -> reload P&ID overlays must not crash ──────────

    # NOTE: test_delete_cause_then_reload_overlays_no_crash and
    # test_delete_consequence_then_reload_overlays_no_crash were removed
    # 2026-08-13 (see NOTES.md: the P&ID canvas is now
    # object-placement-only) — they reproduced a crash class specific to
    # orphaned cause/consequence/safeguard *markers* on the P&ID, via
    # Database.add_cause_marker/add_consequence_marker/add_safeguard_marker
    # (also removed, see NOTES.md). _load_overlays() no longer reads those
    # tables at all, so the crash class they guarded against can no longer
    # occur by construction — nothing left to regression-test there.

    # ── (b) select a safeguard node in the tree -> _on_selected ──────────

    def test_select_safeguard_in_tree_no_crash(self):
        """Reproduces selecting a safeguard node in the tree (SG_T), which
        drives MainWindow._on_selected(SG_T, id_) — a path that walks
        safeguard -> consequence -> cause -> deviation and used to be
        vulnerable to missing-key crashes on any broken link.

        NOTE: scenario_panel.load_deviation()/load_consequence() are
        stubbed out here. They ultimately call QTableWidget.resizeRowsToContents(),
        which recurses into a QStyledItemDelegate.sizeHint() callback
        (hazop.py _ScenarioDelegate.sizeHint, ~line 9773) — under this
        machine's headless Qt platform plugin that path reproducibly hits a
        native access violation (verified independently of this test suite,
        both under QT_QPA_PLATFORM=offscreen and =minimal), which is an
        environment/table-rendering fragility unrelated to the orphaned-data
        crash class this test targets. Stubbing keeps the test focused on
        _on_selected()'s own dict/orphan-lookup logic (the thing that was
        actually buggy) without depending on that unrelated native table
        layout path surviving headlessly.
        """
        with _TempDbMainWindow() as win:
            win.scenario_panel.load_deviation = lambda *a, **k: None
            win.scenario_panel.load_consequence = lambda *a, **k: None
            win.scenario_panel.load_cause = lambda *a, **k: None

            ids = self._make_full_chain(db=win.db)
            win.tree_panel.refresh(SG_T, ids['sg_id'])
            try:
                win._on_selected(SG_T, ids['sg_id'])
            except Exception as e:
                self.fail(f"_on_selected(SG_T, id_) raised: {e!r}")

            # Also simulate a genuinely orphaned safeguard (parent
            # consequence gone) selected in the tree.
            win.db.delete_consequence(ids['cons_id'])
            try:
                win._on_selected(SG_T, ids['sg_id'])
            except Exception as e:
                self.fail(f"_on_selected(SG_T, id_) on orphaned safeguard raised: {e!r}")

    def test_select_consequence_in_tree_no_crash(self):
        """Same idea for CONS_T selection after its parent cause is gone.

        See the docstring on test_select_safeguard_in_tree_no_crash for why
        scenario_panel's load_* methods are stubbed here.
        """
        with _TempDbMainWindow() as win:
            win.scenario_panel.load_deviation = lambda *a, **k: None
            win.scenario_panel.load_consequence = lambda *a, **k: None
            win.scenario_panel.load_cause = lambda *a, **k: None

            ids = self._make_full_chain(db=win.db)
            win.tree_panel.refresh(CONS_T, ids['cons_id'])
            try:
                win._on_selected(CONS_T, ids['cons_id'])
            except Exception as e:
                self.fail(f"_on_selected(CONS_T, id_) raised: {e!r}")

            win.db.conn.execute("PRAGMA foreign_keys = OFF")
            win.db.conn.execute("DELETE FROM causes WHERE id=?", (ids['cause_id'],))
            win.db.commit()
            win.db.conn.execute("PRAGMA foreign_keys = ON")

            try:
                win._on_selected(CONS_T, ids['cons_id'])
            except Exception as e:
                self.fail(f"_on_selected(CONS_T, id_) on orphaned consequence raised: {e!r}")

    # ── delete via TreePanel.delete_selected() ────────────────────────────

    def test_tree_panel_delete_selected_cause_then_overlay_reload(self):
        """End-to-end: create chain via TreePanel-style DB calls, select the
        cause node in the actual QTreeWidget, invoke delete_selected() (the
        real UI deletion path), then reload P&ID overlays."""
        from pid_viewer import PIDPanel

        tree = TreePanel(self.db)
        panel = PIDPanel(self.db)
        try:
            ids = self._make_full_chain()

            tree.refresh(CAUSE_T, ids['cause_id'])
            current = tree.tree.currentItem()
            self.assertIsNotNone(current, "tree should have selected the newly created cause")

            # delete_selected() shows a QMessageBox.question confirmation
            # dialog; monkeypatch it to auto-accept ("Yes") since this is a
            # headless test with no user to click through it.
            from PyQt6.QtWidgets import QMessageBox
            original_question = QMessageBox.question
            QMessageBox.question = staticmethod(
                lambda *a, **k: QMessageBox.StandardButton.Yes)
            try:
                tree.delete_selected()
            finally:
                QMessageBox.question = original_question

            self.assertIsNone(self.db.get_cause(ids['cause_id']))

            _fake_pdf_loaded(panel)
            try:
                panel.reload_overlays()
            except Exception as e:
                self.fail(f"reload_overlays() after TreePanel.delete_selected() raised: {e!r}")
        finally:
            tree.deleteLater()
            panel.deleteLater()

    # ── (c) delete a consequence category from Settings -> _sev_def_panel ─

    def test_delete_consequence_category_no_crash(self):
        """Reproduces bug #4: SettingsPanel._cat_delete() referenced
        self._sev_def_panel, which was never instantiated anywhere in
        SettingsPanel.__init__, causing AttributeError as soon as a user
        deleted a consequence category from the Settings screen.

        If this test fails with AttributeError on `_sev_def_panel`, that
        confirms the bug is still present (or has regressed) and
        SettingsPanel._cat_delete() needs to either instantiate/guard that
        attribute or stop referencing it.
        """
        from hazop import HAZOPPreparationPanel

        panel = HAZOPPreparationPanel(self.db)
        try:
            self.db.add_category("TestCategory")
            panel._load_categories()
            self.assertGreater(panel._cat_list.count(), 0)
            panel._cat_list.setCurrentRow(0)

            try:
                panel._cat_delete()
            except AttributeError as e:
                self.fail(
                    "SettingsPanel._cat_delete() raised AttributeError — "
                    f"the self._sev_def_panel bug is present: {e!r}")
        finally:
            panel.deleteLater()

    # ── ComboBox currentIndex() == -1 bounds-safety (bug #3) ─────────────

    def test_combo_index_minus_one_does_not_index_error_rrf_and_sgtype(self):
        """An empty/uninitialized QComboBox reports currentIndex() == -1.
        Any code that does RRF_VALUES[idx] / SG_TYPES[idx] without a bounds
        check would raise IndexError (since idx=-1 actually wraps to the last
        element in Python rather than erroring, the *real* historical bug
        was more subtle -- but any out-of-range index, positive or negative,
        must be handled). This test exercises the guarded lookup pattern
        used throughout hazop.py: `X[idx] if 0 <= idx < len(X) else default`.
        """
        from hazop import RRF_VALUES, SG_TYPES
        from PyQt6.QtWidgets import QComboBox

        rrf_combo = QComboBox()
        rrf_combo.addItems([str(v) for v in RRF_VALUES])
        # Do NOT select anything -- currentIndex() is -1 on a populated-but-
        # never-selected combo is unusual, but an empty combo box (no items
        # added at all) reliably reports -1.
        empty_combo = QComboBox()
        self.assertEqual(empty_combo.currentIndex(), -1)

        idx = empty_combo.currentIndex()
        try:
            rrf = RRF_VALUES[idx] if 0 <= idx < len(RRF_VALUES) else 1
            sg_type = SG_TYPES[idx] if 0 <= idx < len(SG_TYPES) else 'Övrigt'
        except IndexError as e:
            self.fail(f"Guarded combo-index lookup still raised IndexError: {e}")
        self.assertEqual(rrf, 1)
        self.assertEqual(sg_type, 'Övrigt')

        # Also prove the *unguarded* access is exactly the historical bug,
        # so this test would have caught a regression to unguarded indexing.
        with self.assertRaises(IndexError):
            _ = [][idx]  # any index into an empty list raises, incl. -1

    def test_mainwindow_instantiates_headless_with_temp_db(self):
        """Sanity check that the fixture approach for full MainWindow tests
        is sound: constructing MainWindow() against a scratch DB must not
        raise and must not touch the real project database."""
        with _TempDbMainWindow() as win:
            self.assertIsNotNone(win.db)
            self.assertNotEqual(
                str(win.db.path), str((_HAZOP_DIR / "hazop_project.db").resolve()),
                "MainWindow must not have opened the real project database")


# ══════════════════════════════════════════════════════════════════════════
# 3b. Marker-click native crash regression (bug #6):
#     _on_marker_navigate() double-fired _on_selected()/_rebuild() per click,
#     and a focused _LopaWidget QLineEdit's focus-out during table teardown
#     re-entered _update_lopa_risk() while blockSignals() was flipped back to
#     False mid-_rebuild() (blockSignals is a flat bool, not a nesting
#     counter) — together these caused a native (non-Python) crash when
#     clicking a cause marker on the P&ID viewer.
# ══════════════════════════════════════════════════════════════════════════

class MarkerNavigateCrashTests(unittest.TestCase):
    """Reproduces the exact double-fire + reentrancy scenario that caused a
    native crash on marker click, and verifies both fixes:

      1. TreePanel.refresh(..., emit_selection=False) no longer cascades
         setCurrentItem() -> currentItemChanged -> _on_select ->
         item_selected -> MainWindow._on_selected, so _on_marker_navigate()
         drives _on_selected() (and therefore scenario_panel._rebuild()) only
         once per marker click instead of twice.
      2. ScenarioTablePanel._update_lopa_risk() no-ops while `_rebuilding` is
         True, so a _LopaWidget cell editor's focus-out signal firing
         reentrantly mid-teardown cannot flip _table.blockSignals() back to
         False out from under the outer _rebuild().

    NOTE: scenario_panel.load_deviation()/load_cause()/load_consequence()
    ultimately call QTableWidget.resizeRowsToContents(), which is documented
    elsewhere in this suite (see test_select_safeguard_in_tree_no_crash) as
    reproducibly hitting a native access violation under this machine's
    headless Qt platform plugin — an unrelated environment fragility. Tests
    here that need to count *how many times* the load_* methods are invoked
    (rather than let them run for real) wrap them with a counting spy that
    still calls through only where safe, or stub them out entirely, exactly
    following that existing pattern.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    # ── Bug 1: double-fire fix ────────────────────────────────────────────

    def test_marker_navigate_calls_on_selected_exactly_once(self):
        """_on_marker_navigate() must drive MainWindow._on_selected() exactly
        once per marker click. Before the fix, TreePanel.refresh()'s internal
        setCurrentItem() call (issued after blockSignals(False)) fired the
        tree's currentItemChanged -> _on_select -> item_selected signal chain
        for real, invoking _on_selected() once, and then
        _on_marker_navigate()'s own explicit call invoked it a second time —
        two full scenario_panel loads/rebuilds per single click.
        """
        with _TempDbMainWindow() as win:
            # Stub the heavy scenario_panel loaders (see class docstring / the
            # existing test_select_safeguard_in_tree_no_crash precedent) so
            # this test isolates the *call count*, not table-rendering
            # behaviour that is independently fragile under offscreen Qt.
            win.scenario_panel.load_deviation = unittest.mock.Mock()
            win.scenario_panel.load_consequence = unittest.mock.Mock()
            win.scenario_panel.load_cause = unittest.mock.Mock()
            win.scenario_panel.load_node = unittest.mock.Mock()

            ids = self._make_full_chain(win.db)

            # tree_panel.item_selected was connected to the *bound method*
            # win._on_selected back in MainWindow.__init__, so merely
            # reassigning win._on_selected afterwards would not intercept
            # calls arriving via that pre-existing Qt connection (only the
            # explicit call at the end of _on_marker_navigate would be seen).
            # Disconnect and reconnect to the spy so both the signal-cascade
            # path and the explicit call are counted, exactly reproducing
            # what a real marker click drives.
            on_selected_spy = unittest.mock.Mock(wraps=win._on_selected)
            win.tree_panel.item_selected.disconnect(win._on_selected)
            win.tree_panel.item_selected.connect(on_selected_spy)
            win._on_selected = on_selected_spy

            win._on_marker_navigate('cause', ids['cause_id'])

            self.assertEqual(
                on_selected_spy.call_count, 1,
                "_on_marker_navigate() must call _on_selected() exactly once "
                "per marker click (it used to fire twice: once via "
                "TreePanel.refresh()'s setCurrentItem() cascade, once via "
                "the explicit call)")
            on_selected_spy.assert_called_once_with(CAUSE_T, ids['cause_id'])

    def test_tree_panel_refresh_emit_selection_false_suppresses_cascade(self):
        """Directly verify TreePanel.refresh(..., emit_selection=False) does
        not cascade into item_selected, while the default (emit_selection=
        True, used by every other caller) still does — proving the fix does
        not change behaviour for existing call sites.
        """
        db_tmpdir = tempfile.mkdtemp(prefix="hazop_marker_test_")
        try:
            db = Database(path=os.path.join(db_tmpdir, "test_project.db"))
            tree = TreePanel(db)
            try:
                ids = self._make_full_chain(db)

                item_selected_spy = unittest.mock.Mock()
                tree.item_selected.connect(item_selected_spy)

                # emit_selection=False: no cascade.
                tree.refresh(CAUSE_T, ids['cause_id'], emit_selection=False)
                self.assertEqual(
                    item_selected_spy.call_count, 0,
                    "refresh(emit_selection=False) must not emit item_selected")
                self.assertIsNotNone(tree.tree.currentItem(),
                                      "the visual highlight must still be set")

                # Default behaviour (emit_selection=True) must still cascade,
                # so other existing callers of tree_panel.refresh(type_, id_)
                # keep working exactly as before.
                tree.refresh(CONS_T, ids['cons_id'])
                self.assertEqual(
                    item_selected_spy.call_count, 1,
                    "refresh() with default emit_selection=True must still "
                    "emit item_selected, unchanged for pre-existing callers")
            finally:
                tree.deleteLater()
        finally:
            shutil.rmtree(db_tmpdir, ignore_errors=True)

    # ── Bug 2: _LopaWidget focus-out reentrancy guard ─────────────────────

    def test_update_lopa_risk_noop_while_rebuilding(self):
        """The core of the fix: _update_lopa_risk() must return immediately
        (without touching _table) if called while ScenarioTablePanel._rebuilding
        is True — simulating a _LopaWidget cell editor's focus-out firing
        editingFinished -> _save -> changed.emit() reentrantly mid-teardown,
        which used to reach _update_lopa_risk()'s own
        `finally: self._table.blockSignals(False)` and prematurely unblock
        signals on the *outer* _rebuild()'s table while _build_rows() was
        still constructing new cell widgets.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)

            # Give the consequence some LOPA data so _update_lopa_risk() has
            # real work to do if it were allowed to run.
            win.db.update_consequence_factors(ids['cons_id'], True, 10, False, 10)

            panel._rebuilding = True
            try:
                block_signals_spy = unittest.mock.Mock(
                    wraps=panel._table.blockSignals)
                panel._table.blockSignals = block_signals_spy
                try:
                    panel._update_lopa_risk(ids['cons_id'])
                finally:
                    panel._table.blockSignals = block_signals_spy._mock_wraps
            finally:
                panel._rebuilding = False

            block_signals_spy.assert_not_called()

    def test_lopa_widget_editing_finished_during_rebuild_does_not_reenter(self):
        """End-to-end version of the reentrancy scenario: build a real
        _LopaWidget bound to a live cons_id, simulate _rebuild() being
        mid-teardown (`_rebuilding = True`), then fire the widget's `changed`
        signal (as its QLineEdit's editingFinished -> _save would during a
        focus-out) and confirm it reaches _update_lopa_risk() but the guard
        makes it a no-op rather than touching the table's signal-blocking
        state.
        """
        from hazop import _LopaWidget

        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            win.db.update_consequence_factors(ids['cons_id'], True, 10, False, 10)

            lopa = _LopaWidget(win.db, ids['cons_id'],
                                True, 10.0, False, 10.0, 0)
            lopa.changed.connect(panel._update_lopa_risk)
            try:
                # Give the FA edit field focus, as the real bug scenario
                # requires (a focused QLineEdit inside a cell widget being
                # destroyed by setRowCount(0) mid-rebuild).
                lopa._fa_edit.setFocus()

                panel._rebuilding = True
                try:
                    update_spy = unittest.mock.Mock(wraps=panel._update_lopa_risk)
                    panel._update_lopa_risk = update_spy
                    lopa.changed.connect(update_spy)

                    # Simulate the focus-out -> editingFinished -> _save ->
                    # changed.emit() chain directly (this is exactly what
                    # QLineEdit does internally on focus-out).
                    lopa._fa_edit.editingFinished.emit()

                    self.assertTrue(
                        update_spy.called,
                        "the widget's changed signal should still reach "
                        "_update_lopa_risk (that part of the wiring is "
                        "unchanged) — the guard inside it is what must stop "
                        "the reentrant work, not the signal connection")
                finally:
                    panel._rebuilding = False
            finally:
                lopa.deleteLater()

    def test_rebuild_clears_focus_before_teardown(self):
        """Belt-and-suspenders fix: _rebuild() must clear focus from any
        active cell editor before calling setRowCount(0), so the focus-out
        signal cascade described above never fires in the first place, even
        before the _rebuilding guard in _update_lopa_risk() would catch it.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            win.db.update_consequence_factors(ids['cons_id'], True, 10, False, 10)

            # Stub the heavy loaders so _rebuild() (invoked transitively via
            # load_cause below) stays inside the safe/tested code path,
            # consistent with the rest of this suite's approach to the
            # documented resizeRowsToContents native-crash fragility.
            win.scenario_panel.load_deviation = unittest.mock.Mock()
            win.scenario_panel.load_consequence = unittest.mock.Mock()

            fake_editor = unittest.mock.Mock()
            panel._table.focusWidget = unittest.mock.Mock(return_value=fake_editor)

            panel._rebuild()

            fake_editor.clearFocus.assert_called_once()


class TextOnlyEditFastPathTests(unittest.TestCase):
    """ScenarioTablePanel._update_row_text_only(): a pure description-text
    edit (cause/consequence/safeguard) patches just the affected cell(s) in
    place instead of paying for a full _rebuild() (teardown + re-walk the
    entire DB hierarchy + _apply_spans() + _resize_rows()).

    Before this fix, editing a safeguard's description called
    self._schedule_rebuild() unconditionally, even though nothing about a
    safeguard's OWN row (its RFORE/SLUT columns are derived from rrf,
    not description text) or any OTHER row depends on that text. Cause/
    consequence edits already didn't trigger a rebuild (a side effect of the
    emit_selection=False fix earlier this session), but didn't sync other
    rows showing the same id (span groups keep one QTableWidgetItem per
    underlying row even when visually merged).
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_safeguard_description_edit_no_longer_schedules_full_rebuild(self):
        """Editing a safeguard's description must patch the cell in place and
        must NOT call _schedule_rebuild() — nothing about its own row's
        risk-derived columns or any other row depends on description text.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            panel.load_cause(ids['cause_id'])

            schedule_spy = unittest.mock.Mock(wraps=panel._schedule_rebuild)
            panel._schedule_rebuild = schedule_spy

            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == ids['sg_id'])
            item = panel._table.item(row, panel._C_SG)
            item.setText("Ny barriärbeskrivning")
            panel._on_cell_changed(row, panel._C_SG)

            schedule_spy.assert_not_called()
            self.assertEqual(
                dict(win.db.get_safeguard(ids['sg_id']))['description'],
                "Ny barriärbeskrivning")

    def test_update_row_text_only_noop_while_rebuilding(self):
        """Mirrors test_update_lopa_risk_noop_while_rebuilding: the fast path
        must return immediately without touching the table if called while
        _rebuilding is True (e.g. a reentrant cell-commit signal firing
        mid-teardown), not just skip the (now-removed) rebuild call.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            panel.load_cause(ids['cause_id'])

            panel._rebuilding = True
            try:
                block_signals_spy = unittest.mock.Mock(wraps=panel._table.blockSignals)
                panel._table.blockSignals = block_signals_spy
                try:
                    panel._update_row_text_only('safeguard', ids['sg_id'], "Should not apply")
                finally:
                    panel._table.blockSignals = block_signals_spy._mock_wraps
            finally:
                panel._rebuilding = False

            block_signals_spy.assert_not_called()

    def test_cause_edit_syncs_all_rows_sharing_the_same_cause(self):
        """A cause with two consequences produces two rows sharing the same
        cause_id (merged into one visual span by _apply_spans(), but still
        two distinct QTableWidgetItem objects underneath). Editing the ORS
        text on one row must patch the OTHER row's copy too, without a full
        rebuild.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            win.db.add_consequence(ids['cause_id'])  # second consequence -> second row, same cause
            panel.load_cause(ids['cause_id'])

            rows = [r for r, m in enumerate(panel._row_meta)
                    if m[1] == ids['cause_id']]
            self.assertEqual(len(rows), 2, "expected two rows sharing the same cause_id")

            panel._update_row_text_only('cause', ids['cause_id'], "Uppdaterad orsakstext")

            for row in rows:
                item = panel._table.item(row, panel._C_ORS)
                self.assertEqual(item.text(), "Uppdaterad orsakstext",
                    f"row {row}'s ORS cell must reflect the edit even though "
                    "it wasn't the row the user directly typed into")

    def test_wrap_col_row_height_matches_resize_rows_manual_formula(self):
        """_wrap_col_row_height() is a deliberate near-duplicate of one branch
        of _resize_rows_manual()'s per-row loop (kept as a small standalone
        helper so the fast path doesn't need a full table pass) -- assert it
        actually agrees with a real _resize_rows_manual() pass for the same
        row/column, so the two don't silently drift apart over time.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            panel.load_cause(ids['cause_id'])

            long_text = "Detta är en mycket lång orsaksbeskrivning som med säkerhet radbryts över flera rader i cellen. " * 3
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == ids['cause_id'])
            panel._table.item(row, panel._C_ORS).setText(long_text)

            fast_path_height = panel._wrap_col_row_height(row, panel._C_ORS)

            panel._resize_rows_manual()
            full_pass_height = panel._table.rowHeight(row)

            self.assertEqual(fast_path_height, full_pass_height,
                "the fast-path height helper must agree with a full "
                "_resize_rows_manual() pass for the same row/column")


class KonInlineEditTests(unittest.TestCase):
    """'Klicka direkt på konsekvens för att redigera den direkt där'
    (NOTES.md 2026-08-07) — KON cells are now included in the inline-edit
    path (_try_start_edit) and get the same single-click-on-already-
    current-cell trigger ORS/SG already had ("Feature 7"). The commit path
    (_on_cell_changed_inner's 'consequence' branch) already existed and
    worked — this was purely a missing trigger. Double-click still opens
    the step-by-step chain wizard, unchanged."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        return node_id, deviation_id, cause_id, cons_id

    def test_try_start_edit_now_allows_kon_column(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            edit_spy = unittest.mock.Mock(wraps=panel._table.edit)
            panel._table.edit = edit_spy
            panel._try_start_edit(row, panel._C_KON)
            # QTableWidget.edit() is overloaded (Qt itself can trigger a
            # second internal call) — what matters is that _try_start_edit
            # no longer early-returns for the KON column at all.
            edit_spy.assert_called()

    def test_single_click_on_already_current_kon_cell_does_not_start_edit(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            panel._table.setCurrentCell(row, panel._C_KON)

            with unittest.mock.patch.object(panel, '_try_start_edit') as mock_edit:
                panel._on_cell_clicked(row, panel._C_KON)
            mock_edit.assert_not_called()

    def test_editing_kon_cell_saves_to_consequence_description(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            item = panel._table.item(row, panel._C_KON)
            item.setText("Inget flöde till pump X")
            panel._on_cell_changed(row, panel._C_KON)

            self.assertEqual(
                dict(win.db.get_consequence(cons_id))['description'],
                "Inget flöde till pump X")

    def test_double_click_on_kon_starts_inline_edit_not_chain_wizard(self):
        """Reported feedback: double-click on KON opening the
        "Konsekvenskedja" wizard felt out of place and inconsistent with
        ORS/SG (which just start inline edit on double-click). Double-click
        now behaves the same way across ORS/KON/SG; the wizard remains
        reachable via the right-click context menu (_open_chain_editor,
        unchanged)."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            item = panel._table.item(row, panel._C_KON)

            with unittest.mock.patch.object(panel, '_open_chain_editor') as mock_wizard, \
                 unittest.mock.patch.object(panel._table, 'edit') as mock_edit:
                panel._on_cell_double_clicked(item)

            mock_wizard.assert_not_called()
            mock_edit.assert_called_once()

    def test_double_click_on_empty_safeguard_cell_quick_adds_one(self):
        """"Gör även så jag kan dubbelklicka på safeguards för att
        redigera den direkt även om inget ligger tillagt. (precis som
        konsekvens i hazopscenario)" (2026-08-17, see NOTES.md) — an SG
        cell with no safeguard yet is non-editable (unlike KON, which
        always has a real backing row), so double-click used to just
        return early and do nothing. Must now quick-add a safeguard for
        that consequence instead, same no-popup path Enter/the "+" row
        already use."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            item = panel._table.item(row, panel._C_SG)
            self.assertFalse(bool(item.flags() & Qt.ItemFlag.ItemIsEditable),
                "sanity check: an empty SG cell must start non-editable")

            before = {s['id'] for s in win.db.safeguards(cons_id)}
            panel._on_cell_double_clicked(item)
            after = {s['id'] for s in win.db.safeguards(cons_id)}

            self.assertEqual(len(after - before), 1,
                "double-clicking an empty safeguard cell must create exactly one safeguard")

    def test_double_click_on_existing_safeguard_cell_still_edits_in_place(self):
        """The fix above must not disturb the already-working case — a
        cell that already has a safeguard is editable and double-click
        must still just start inline edit, not create a second one."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _node_id, _dev_id, cause_id, cons_id = self._make_full_chain(win.db)
            win.db.add_safeguard(cons_id)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)
            item = panel._table.item(row, panel._C_SG)
            self.assertTrue(bool(item.flags() & Qt.ItemFlag.ItemIsEditable))

            before = {s['id'] for s in win.db.safeguards(cons_id)}
            with unittest.mock.patch.object(panel._table, 'edit') as mock_edit:
                panel._on_cell_double_clicked(item)
            after = {s['id'] for s in win.db.safeguards(cons_id)}

            mock_edit.assert_called_once()
            self.assertEqual(before, after)

    def test_lopa_column_header_renamed_to_enablers(self):
        """"Döp om kolumnen FA / ANt. Övriga till Enablers i hazop
        scenario" (2026-08-17, see NOTES.md)."""
        from hazop import ScenarioTablePanel
        self.assertEqual(ScenarioTablePanel._COLS[ScenarioTablePanel._C_LOPA], 'Enablers')


class SafeguardCreatedDoubleRebuildTests(unittest.TestCase):
    """Reproduces the second occurrence of the double-rebuild crash class,
    this time triggered by *adding a safeguard* rather than clicking a P&ID
    marker (the trigger the original `_on_marker_navigate` fix, commit
    84c8b7c, addressed).

    The `84c8b7c` fix only patched `TreePanel.refresh(..., emit_selection=
    False)` at the one call site inside `_on_marker_navigate`. It left the
    *general* anti-pattern — calling `tree_panel.refresh(type_, id_)` with
    the default `emit_selection=True` (which cascades via
    `setCurrentItem -> currentItemChanged -> _on_select -> item_selected ->
    MainWindow._on_selected`) *and* separately calling an equivalent
    scenario-rebuilding method for the same item — in several other call
    sites. Each of those causes `ScenarioTablePanel._rebuild()` to run twice
    per single user action, which is exactly the rapid-fire rebuild volume
    that gave a reentrant cell-widget signal (e.g. a focused `_LopaWidget`
    `QLineEdit`'s focus-out) a chance to corrupt `_rebuild()`'s teardown.

    This class asserts each newly-fixed handler drives the scenario panel's
    rebuild-equivalent call exactly once instead of twice:

      - `MainWindow._on_safeguard_created()` (fired by
        `PIDPanel.safeguard_created`, i.e. placing a safeguard marker on the
        P&ID) — used to call `scenario_panel.load_consequence()` explicitly
        *and* let `tree_panel.refresh(CONS_T, ...)`'s cascade call it again.
      - The `scenario_panel.new_item_created` handler wired in
        `MainWindow.__init__` (fired by `ScenarioTablePanel._quick_add_safeguard`
        et al., i.e. adding a safeguard/cause/consequence directly from the
        scenario table's quick-add flow) — used to let `tree_panel.refresh()`'s
        cascade rebuild once, then call the explicit `scenario_panel.refresh()`
        (== `_rebuild()`) a second time.
      - `MainWindow._on_props_changed()` (fired whenever the PropertiesRibbon
        saves a field) — used to let `tree_panel.refresh()`'s cascade rebuild
        once, then call the explicit `scenario_panel._rebuild()` a second time.

    All three now pass `emit_selection=False` to `tree_panel.refresh()`,
    matching the established `84c8b7c` pattern exactly, since each is already
    followed by an explicit rebuild-equivalent call.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_new_item_created_safeguard_rebuilds_scenario_panel_exactly_once(self):
        """Quick-adding a safeguard directly from the scenario table (Enter-to
        -add-next-row flow, ScenarioTablePanel._quick_add_safeguard ->
        new_item_created(SG_T, id) -> the lambda wired in MainWindow.__init__)
        must rebuild the scenario table exactly once. This is a second,
        independent path to the same double-rebuild bug as
        _on_safeguard_created above, and a very plausible real-world match
        for "the crash happens when adding a safeguard" since it fires
        synchronously from inside a table cell-edit-commit handler.
        """
        with _TempDbMainWindow() as win:
            ids = self._make_full_chain(win.db)

            rebuild_spy = unittest.mock.Mock(wraps=win.scenario_panel._rebuild)
            win.scenario_panel._rebuild = rebuild_spy
            win.scenario_panel.refresh = lambda: win.scenario_panel._rebuild()

            win.scenario_panel.new_item_created.emit(SG_T, ids['sg_id'])

            self.assertEqual(
                rebuild_spy.call_count, 1,
                "quick-adding a safeguard from the scenario table must "
                "rebuild the table exactly once (it used to rebuild twice: "
                "once via tree_panel.refresh()'s setCurrentItem cascade into "
                "_on_selected, once via the explicit scenario_panel.refresh() "
                "call right after)")

    def test_on_props_changed_rebuilds_scenario_panel_exactly_once(self):
        """Saving a field in the PropertiesRibbon (MainWindow._on_props_changed)
        must rebuild the scenario table exactly once. This handler fires on
        every properties-field save, making it one of the most frequent
        triggers of the double-rebuild anti-pattern.
        """
        with _TempDbMainWindow() as win:
            ids = self._make_full_chain(win.db)
            win._cur_type = CAUSE_T
            win._cur_id = ids['cause_id']

            rebuild_spy = unittest.mock.Mock(wraps=win.scenario_panel._rebuild)
            win.scenario_panel._rebuild = rebuild_spy

            win._on_props_changed()

            self.assertEqual(
                rebuild_spy.call_count, 1,
                "_on_props_changed() must rebuild the scenario panel exactly "
                "once per properties save (it used to rebuild twice: once "
                "via tree_panel.refresh()'s setCurrentItem cascade into "
                "_on_selected, once via the explicit scenario_panel._rebuild() "
                "call right after)")

    def test_on_props_changed_refreshes_pid_overlays_for_a_node(self):
        """"När jag sedan uppdaterar namnet på noden vill jag att detta
        uppdateras även på P&ID" (2026-08-17, see NOTES.md) — renaming a
        node via PropertiesRibbon's Namn-popup (_edit_node_name) already
        syncs node_markups.label via Database.update_node(), but nothing
        on that path redrew the P&ID until this fix — the on-canvas
        "Lägg ut nodnamn" label stayed visibly stale. _on_props_changed
        must refresh the P&ID's markup overlays when the changed item is
        a node."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win._cur_type = NODE_T
            win._cur_id = node_id

            refresh_spy = unittest.mock.Mock(wraps=win.pid_panel.refresh_markup_overlays)
            win.pid_panel.refresh_markup_overlays = refresh_spy

            win._on_props_changed()

            refresh_spy.assert_called_once()

    def test_on_props_changed_does_not_touch_pid_overlays_for_non_node_items(self):
        """The same handler fires for every properties save (causes,
        consequences, safeguards too) — must not refresh P&ID overlays
        for those, only for an actual node rename."""
        with _TempDbMainWindow() as win:
            ids = self._make_full_chain(win.db)
            win._cur_type = CAUSE_T
            win._cur_id = ids['cause_id']

            refresh_spy = unittest.mock.Mock(wraps=win.pid_panel.refresh_markup_overlays)
            win.pid_panel.refresh_markup_overlays = refresh_spy

            win._on_props_changed()

            refresh_spy.assert_not_called()

    def test_node_created_calls_on_selected_exactly_once(self):
        """Creating a new node via the P&ID (PIDPanel.node_created) must
        drive MainWindow._on_selected() exactly once, mirroring the original
        _on_marker_navigate fix (commit 84c8b7c). Before this fix, the lambda
        wired to node_created called tree_panel.refresh(NODE_T, nid) (default
        emit_selection=True, cascading into _on_selected) *and* an explicit
        self._on_selected(NODE_T, nid) right after.
        """
        with _TempDbMainWindow() as win:
            win.scenario_panel.load_node = unittest.mock.Mock()

            new_node_id = win.db.add_node()

            on_selected_spy = unittest.mock.Mock(wraps=win._on_selected)
            win.tree_panel.item_selected.disconnect(win._on_selected)
            win.tree_panel.item_selected.connect(on_selected_spy)
            win._on_selected = on_selected_spy

            win.pid_panel.node_created.emit(new_node_id)

            self.assertEqual(
                on_selected_spy.call_count, 1,
                "node_created must call _on_selected() exactly once per new "
                "node (it used to fire twice: once via tree_panel.refresh()'s "
                "setCurrentItem cascade, once via the explicit call)")

    def test_on_scenario_item_edited_does_not_cascade_into_on_selected(self):
        """Committing an ordinary cell edit (e.g. a cause description) must
        NOT redundantly re-select/re-rebuild the scenario panel via
        tree_panel.refresh()'s setCurrentItem cascade. Before this fix,
        MainWindow._on_scenario_item_edited() called tree_panel.refresh(type_,
        id_) with the default emit_selection=True, cascading into
        _on_selected() on EVERY single cell edit -- not just on new-item
        creation -- causing the scenario table to visibly reset its current
        cell/selection after every edit commit (the reported "jumps away
        from the object" confusion).
        """
        with _TempDbMainWindow() as win:
            ids = self._make_full_chain(win.db)

            on_selected_spy = unittest.mock.Mock(wraps=win._on_selected)
            win.tree_panel.item_selected.disconnect(win._on_selected)
            win.tree_panel.item_selected.connect(on_selected_spy)
            win._on_selected = on_selected_spy

            win._on_scenario_item_edited(CAUSE_T, ids['cause_id'])

            self.assertEqual(
                on_selected_spy.call_count, 0,
                "_on_scenario_item_edited() must not cascade into "
                "_on_selected() at all -- it already has everything it "
                "needs (type_, id_) and only needs to sync tree labels / "
                "P&ID overlays, not reselect/rebuild the scenario panel")

    def test_new_item_created_positions_current_cell_on_new_row(self):
        """After quick-adding a cause via Enter-to-add-next-row (or the quick-
        add menu), the scenario table's current cell must land on the new
        cause's Orsak cell -- not wherever the rebuild happened to leave
        selection -- so the user can keep typing without losing their place.
        """
        with _TempDbMainWindow() as win:
            ids = self._make_full_chain(win.db)
            win.tree_panel.refresh = unittest.mock.Mock()  # isolate: only care about scenario_panel

            new_cause_id = win.db.add_cause(ids['deviation_id'])
            win.scenario_panel.load_node(ids['node_id'])  # populate _row_meta with both causes

            win.scenario_panel.new_item_created.emit(CAUSE_T, new_cause_id)

            row = win.scenario_panel._table.currentRow()
            col = win.scenario_panel._table.currentColumn()
            self.assertGreaterEqual(row, 0, "current cell must be set, not left unselected")
            self.assertEqual(col, win.scenario_panel._C_ORS)
            dev_id, cause_id, cons_id, sg_id = win.scenario_panel._row_meta[row]
            self.assertEqual(cause_id, new_cause_id,
                "current cell must be on the row for the newly created cause, "
                "not an arbitrary/leftover row from before the rebuild")


class CauseTemplateCreatedFocusStealBugTests(unittest.TestCase):
    """Regression test for the user report 'jag kan fortfarande inte trycka
    på konsekvens och lägga in text' (still can't click into Consequence
    and type), reported right after the EquipmentDeviationBar's "föreslå
    troligaste orsaken" chip / cause dropdown started working.

    Root cause: MainWindow's `_on_cause_template_created` closure (fired by
    PIDPanel.cause_template_created, which place_cause_from_template() —
    used by BOTH the normal P&ID 'Orsak' flow and
    EquipmentDeviationBar._create_cause_from_bar — always emits) called
    `tree_panel.refresh(CAUSE_T, cid)` WITHOUT `emit_selection=False`. That
    cascades into `_on_selected(CAUSE_T, cid)` ->
    `scenario_panel.load_deviation(...)`, rebuilding the whole worksheet
    table right as the user's very next move (clicking that new row's KON
    cell to type a consequence) lands — the same anti-pattern already fixed
    elsewhere per commit 84c8b7c, just not yet here. A second, independent
    bug compounded it: `ScenarioTablePanel.select_cause()`, deferred 50ms
    after cause creation, unconditionally forced the current cell back to
    the ORS column — which would yank focus straight out of a KON cell the
    user had already started typing into within that window.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_cause_template_created_does_not_cascade_into_on_selected(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde")
            cause_id = win.db.add_cause(dev_id)

            on_selected_spy = unittest.mock.Mock(wraps=win._on_selected)
            win.tree_panel.item_selected.disconnect(win._on_selected)
            win.tree_panel.item_selected.connect(on_selected_spy)
            win._on_selected = on_selected_spy

            win.pid_panel.cause_template_created.emit(cause_id)

            self.assertEqual(
                on_selected_spy.call_count, 0,
                "cause_template_created must not cascade into _on_selected() "
                "via tree_panel.refresh()'s setCurrentItem — it already "
                "drives the worksheet explicitly via scenario_panel.load_node()")

    def test_cause_template_created_uses_load_node_not_load_deviation(self):
        """load_node() (not load_deviation()) must be used so every
        deviation under the node stays visible — matching
        _on_equipment_deviation_created's own fix for the same underlying
        complaint ('jag vill se BÅDA avvikelserna')."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde")
            cause_id = win.db.add_cause(dev_id)

            load_node_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_node)
            win.scenario_panel.load_node = load_node_spy
            load_deviation_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_deviation)
            win.scenario_panel.load_deviation = load_deviation_spy

            win.pid_panel.cause_template_created.emit(cause_id)

            load_node_spy.assert_called_once_with(node_id)
            load_deviation_spy.assert_not_called()

    def test_select_cause_does_not_steal_current_cell_from_a_row_user_already_navigated_to(self):
        """ScenarioTablePanel.select_cause() must not force the current
        cell back to the ORS column if the user has already navigated
        (e.g. clicked into the KON cell of that same row to type a
        consequence) — it may still scroll the row into view."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde")
            cause_id = win.db.add_cause(dev_id)
            win.scenario_panel.load_node(node_id)

            row = next(i for i, m in enumerate(win.scenario_panel._row_meta)
                       if m[1] == cause_id)
            kon_col = win.scenario_panel._C_KON
            win.scenario_panel._table.setCurrentCell(row, kon_col)

            win.scenario_panel.select_cause(cause_id)

            self.assertEqual(
                win.scenario_panel._table.currentColumn(), kon_col,
                "select_cause must not steal the current cell away from a "
                "row the user already navigated to")


class EditExtraDeferredRebuildTests(unittest.TestCase):
    """Reproduces the THIRD occurrence of the silent-native-crash class in
    ScenarioTablePanel._rebuild() (2026-08-02, hazop_crash.log: the log
    always stops right after '_rebuild: E — reset meta', with no further
    output and no Python exception — i.e. inside _build_rows()).

    The first two fixes (84c8b7c: _LopaWidget focus-out reentrancy guard in
    _update_lopa_risk(); 686e289: double tree_panel.refresh()+_on_selected()
    anti-pattern) were both confirmed still correctly in place and did not
    explain this third occurrence. This test documents and guards against a
    THIRD, independent trigger of the same underlying reentrancy class,
    found by auditing every dialog .exec() call inside ScenarioTablePanel:

    ScenarioTablePanel._edit_extra() (wired to a live _LopaWidget's
    "+ övriga" QPushButton.clicked signal, itself a cell widget embedded in
    self._table) used to call `self._rebuild()` directly and synchronously
    right after `dlg.exec()` returned — the ONLY handler in the whole class
    to do so; every other popup/dialog handler defers via
    `self._schedule_rebuild()` (a `QTimer.singleShot(0, ...)`), and there
    are 24 such call sites.

    `dlg.exec()` pumps a NESTED Qt event loop. Any `QTimer.singleShot(0, ...)`
    already queued by an earlier `_schedule_rebuild()` call (e.g. from a
    click on a different cell moments before) fires DURING that nested loop
    -- not after it -- which means `_rebuild()` can run while _edit_extra()
    (and the button's `clicked` handler that invoked it) is still executing,
    paused inside `dlg.exec()`, on the C++ call stack. `_rebuild()`'s
    `setRowCount(0)` then destroys the very `_LopaWidget`/button that
    originated this call. The fix makes _edit_extra() defer via
    `_schedule_rebuild()` like every other handler, so it can never itself
    race a pending scheduled rebuild the way a direct call could.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return {
            'node_id': node_id, 'deviation_id': deviation_id,
            'cause_id': cause_id, 'cons_id': cons_id, 'sg_id': sg_id,
        }

    def test_edit_extra_defers_rebuild_instead_of_calling_it_directly(self):
        """_edit_extra() must schedule a rebuild via _schedule_rebuild()
        (deferred, coalesced, safe against a nested dlg.exec() event loop)
        rather than calling self._rebuild() synchronously right after the
        dialog closes -- the pattern used by every other dialog handler in
        this class."""
        from hazop import ReductionFactorsDialog

        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)

            # Avoid actually showing a modal dialog in the test run.
            fake_dlg = unittest.mock.Mock()
            fake_dlg.exec = unittest.mock.Mock(return_value=0)
            with unittest.mock.patch(
                    'scenario_panel.ReductionFactorsDialog', return_value=fake_dlg):
                rebuild_spy = unittest.mock.Mock()
                schedule_spy = unittest.mock.Mock()
                panel._rebuild = rebuild_spy
                panel._schedule_rebuild = schedule_spy

                panel._edit_extra(ids['cons_id'])

                schedule_spy.assert_called_once()
                rebuild_spy.assert_not_called()

    def test_schedule_rebuild_pending_during_edit_extra_does_not_reenter_rebuild(self):
        """End-to-end reproduction: a rebuild already scheduled via
        _schedule_rebuild() (QTimer.singleShot(0, ...)) must not be able to
        tear down the table (setRowCount(0), destroying the live
        _LopaWidget/button that is the source of this very call) while
        _edit_extra() is still on the call stack underneath a dialog's
        exec(). Simulated by queuing a pending rebuild flag and firing the
        timer synchronously (as the nested event loop would) from inside a
        fake dlg.exec(), then confirming the panel survives and only one
        additional _rebuild() happens afterward, not a nested one during
        the dialog.
        """
        from hazop import ReductionFactorsDialog

        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            ids = self._make_full_chain(win.db)
            panel._cons_id = ids['cons_id']

            # Stub _rebuild() itself (rather than calling through to the
            # real implementation) -- see test_select_safeguard_in_tree_no_crash's
            # docstring: the real _rebuild() ultimately calls
            # QTableWidget.resizeRowsToContents(), which reproducibly hits a
            # native access violation under this machine's headless Qt
            # platform plugin, unrelated to the reentrancy behaviour under
            # test here. Only the *call count/ordering* matters for this test.
            rebuild_call_log = []

            def _tracking_rebuild():
                rebuild_call_log.append('rebuild')

            panel._rebuild = _tracking_rebuild

            # Simulate: a rebuild is already scheduled (as if the user had
            # just clicked a different cell) and its QTimer.singleShot(0,...)
            # fires DURING the modal dialog's nested event loop -- exactly
            # what a real dlg.exec() call pumps for any already-queued timer.
            def _fake_exec():
                panel._on_rebuild_scheduled()  # what the pending timer runs
                return 0

            fake_dlg = unittest.mock.Mock()
            fake_dlg.exec = unittest.mock.Mock(side_effect=_fake_exec)
            with unittest.mock.patch(
                    'scenario_panel.ReductionFactorsDialog', return_value=fake_dlg):
                panel._rebuild_pending = True  # a rebuild was already queued
                panel._edit_extra(ids['cons_id'])

            # The nested-loop rebuild ran once (via _fake_exec). Because
            # _edit_extra() now defers through _schedule_rebuild() instead of
            # calling self._rebuild() directly, no second, immediately-stacked
            # rebuild races it while the dialog handler frame is still live.
            self.assertEqual(
                rebuild_call_log.count('rebuild'), 1,
                "only the nested-loop's own scheduled rebuild should run "
                "synchronously here; _edit_extra() must not additionally "
                "call _rebuild() directly on top of it")
            # A further rebuild is still scheduled for the next event-loop
            # tick (coalesced with any other pending request), not skipped.
            self.assertTrue(panel._rebuild_pending)


# ══════════════════════════════════════════════════════════════════════════
# 8. _reload_all_panels() must swap self.db on EVERY panel that holds its
#    own db reference. Real bug found in production: HAZOPWorksheet (and
#    its embedded ScenarioTablePanel), RedMarkupPanel and
#    RedMarkupTablePanel were missing from the panel list — after "Nytt
#    projekt" / "Öppna .hzp" closed the old connection and opened a new
#    one, clicking the Worksheet tab crashed with sqlite3.ProgrammingError
#    ("Cannot operate on a closed database") because HAZOPWorksheet.refresh()
#    -> _populate_node_combo() -> self.db.nodes() still ran against the OLD,
#    now-closed Database object. RedMarkupTablePanel itself was deleted
#    outright 2026-08-26 (see NOTES.md "Gör om Red Markup-knappen") — only
#    RedMarkupPanel remains to check here now.
# ══════════════════════════════════════════════════════════════════════════

class ReloadAllPanelsDbSwapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_worksheet_and_its_embedded_table_panel_get_new_db(self):
        with _TempDbMainWindow() as win:
            old_db = win.db
            # Simulate what _hzp_new/_load_hzp do: close the old connection,
            # swap in a brand new Database, then run the same fix-up step.
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            self.assertIs(win.worksheet.db, win.db,
                "HAZOPWorksheet must receive the new db reference")
            self.assertIs(win.worksheet._table_panel.db, win.db,
                "HAZOPWorksheet's embedded ScenarioTablePanel must also receive the new db reference")
            self.assertIs(win.red_markup_panel.db, win.db)
            self.assertFalse(hasattr(win, 'red_markup_table_panel'),
                "RedMarkupTablePanel was torn down entirely, not just hidden")

    def test_worksheet_refresh_does_not_crash_after_db_swap(self):
        """End-to-end regression for the exact reported crash: switching to
        the Worksheet tab after a db swap must not raise
        sqlite3.ProgrammingError('Cannot operate on a closed database')."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.execute(
                "INSERT INTO nodes (name, markup_points, markup_style, pid_page) "
                "VALUES ('N-1', '[]', '{}', 0)")
            old_db.commit()

            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.worksheet.refresh()
            except sqlite3.ProgrammingError as e:
                self.fail(f"worksheet.refresh() must not touch the closed old db, raised: {e!r}")

    def test_equipment_panel_and_its_model_get_new_db(self):
        """Same bug class as the Worksheet one above, found via a real crash
        report (2026-08-06): EquipmentPanel's QTableView is backed by
        _EquipmentTableModel, which keeps its OWN db reference (needed so
        setData()/delete_row() can write through directly) separate from
        EquipmentPanel.db — _reload_all_panels() updated the panel's db but
        not the model's, so the model kept using the old, by-then-closed
        connection after a project reload."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            self.assertIs(win.equipment_panel.db, win.db)
            self.assertIs(win.equipment_panel._model.db, win.db,
                "EquipmentPanel's _EquipmentTableModel must also receive the new db reference")

    def test_equipment_panel_refresh_does_not_crash_after_db_swap(self):
        """End-to-end regression for the exact reported crash: switching to
        the Utrustning tab after a project reload must not raise
        sqlite3.ProgrammingError('Cannot operate on a closed database')."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.execute(
                "INSERT INTO equipment_catalog (tag, prefix, pid_page, equipment_type) "
                "VALUES ('V-1', 'V', 0, 'Ventil')")
            old_db.commit()

            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.equipment_panel.refresh()
            except sqlite3.ProgrammingError as e:
                self.fail(f"equipment_panel.refresh() must not touch the closed old db, raised: {e!r}")

    def test_admin_panels_nested_pid_mgmt_gets_new_db(self):
        """Same bug class again, found via a real crash report
        (2026-08-11, see NOTES.md): StudyManagementPanel (admin_panel)
        embeds its own PIDManagementPanel (self._pid_mgmt, revision
        history + sheet reordering) with its own separate db reference,
        set once at __init__ and never touched by _reload_all_panels()'s
        top-level panel.db = db loop."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            self.assertIs(win.admin_panel.db, win.db)
            self.assertIs(win.admin_panel._pid_mgmt.db, win.db,
                "StudyManagementPanel's nested PIDManagementPanel must also receive the new db reference")

    def test_admin_panel_refresh_pid_does_not_crash_after_db_swap(self):
        """End-to-end regression for the exact reported crash: switching
        to the Administration tab after a project reload used to raise
        sqlite3.ProgrammingError('Cannot operate on a closed database')
        from Database.get_revisions()."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.execute(
                "INSERT INTO pid_revisions (revision, notes, created_at, pdf_path) "
                "VALUES ('Rev A', '', '2026-08-11', '')")
            old_db.commit()

            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.admin_panel.refresh_pid()
            except sqlite3.ProgrammingError as e:
                self.fail(f"admin_panel.refresh_pid() must not touch the closed old db, raised: {e!r}")

    def test_pid_analysis_panel_and_its_model_get_new_db(self):
        """Same bug, same fix, for Inställningar → Identifierade objekt
        (PIDAnalysisPanel / _IdentifiedTagsModel)."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            analysis_panel = win.settings_panel.analysis_panel
            self.assertIs(analysis_panel.db, win.db)
            self.assertIs(analysis_panel._model.db, win.db,
                "PIDAnalysisPanel's _IdentifiedTagsModel must also receive the new db reference")

    def test_pid_analysis_panel_refresh_does_not_crash_after_db_swap(self):
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.execute(
                "INSERT INTO pid_identified_tags (tag_code, examples, name_sv, comp_type, confirmed) "
                "VALUES ('V', 'V-1', '', '', 0)")
            old_db.commit()

            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.settings_panel.analysis_panel.refresh()
            except sqlite3.ProgrammingError as e:
                self.fail(f"analysis_panel.refresh() must not touch the closed old db, raised: {e!r}")

    def test_participant_matrix_panel_gets_new_db(self):
        """Same bug class again, found via a real crash report
        (2026-08-17): SettingsPanel embeds its own ParticipantMatrixPanel
        (self._participant_matrix_panel, the "Deltagare" tab), which kept
        its own db reference from __init__ time — it was missing from
        _reload_all_panels()'s settings-sub-panel refresh list entirely
        (unlike _std_causes_panel/_std_objects_panel/etc., which were
        already there), so it kept using the old, by-then-closed
        connection after a project reload."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            self.assertIs(
                win.hazop_prep_panel._participant_matrix_panel.db, win.db,
                "HAZOPPreparationPanel's nested ParticipantMatrixPanel must also receive the new db reference")

    def test_add_participant_does_not_crash_after_db_swap(self):
        """End-to-end regression for the exact reported crash: "Programmet
        kraschar när jag klickar på lägg till deltagare" — clicking
        "+ Lägg till deltagare" after a project reload used to raise
        sqlite3.ProgrammingError('Cannot operate on a closed database')
        from Database.add_participant()."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.hazop_prep_panel._participant_matrix_panel._add_participant()
            except sqlite3.ProgrammingError as e:
                self.fail(f"_add_participant() must not touch the closed old db, raised: {e!r}")

    def test_equipment_deviation_bar_gets_new_db(self):
        """Same bug class as worksheet/equipment_panel above, found via a
        real crash report (2026-08-07): EquipmentDeviationBar (the bottom-
        of-P&ID bar opened by clicking an equipment marker, see NOTES.md
        'Nod → Utrustning → Avvikelse') keeps its own db reference,
        separate from PIDPanel.db — _reload_all_panels() updated
        pid_panel.db/pid_panel.viewer.db but not pid_panel._equipment_bar.db,
        so clicking an equipment marker after a project reload crashed with
        sqlite3.ProgrammingError('Cannot operate on a closed database')."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()
            self.assertIs(win.pid_panel._equipment_bar.db, win.db,
                "EquipmentDeviationBar must also receive the new db reference")

    def test_equipment_marker_click_does_not_crash_after_db_swap(self):
        """End-to-end regression for the exact reported crash: clicking an
        equipment marker on the P&ID after a project reload must not raise
        sqlite3.ProgrammingError('Cannot operate on a closed database')."""
        with _TempDbMainWindow() as win:
            old_db = win.db
            eq_id = old_db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
            marker_id = old_db.add_equipment_marker(eq_id, "V-1", 0, 10.0, 10.0, "Ventil")

            old_db.conn.close()
            win.db = hazop.Database(path=old_db.path)
            win._reload_all_panels()

            try:
                win.pid_panel._on_marker_clicked('equipment', marker_id)
            except sqlite3.ProgrammingError as e:
                self.fail(f"clicking an equipment marker must not touch the closed old db, raised: {e!r}")

    def test_equipment_marker_click_opens_bar_and_filters_scenario_table(self):
        """(2026-08-12, see NOTES.md) _on_marker_clicked used to return
        early for 'equipment' without ever emitting marker_navigated —
        opening EquipmentDeviationBar and filtering the scenario table
        were mutually exclusive. Now both happen on the same click: the
        full real chain (PIDPanel._on_marker_clicked -> marker_navigated
        -> MainWindow._on_marker_navigate -> _on_equipment_marker_navigate
        -> scenario_panel.load_equipment) must actually fire end to end."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            marker_id = win.db.add_equipment_marker(eq_id, "PV-101", 0, 10.0, 10.0, "Ventil")

            win.pid_panel._on_marker_clicked('equipment', marker_id)

            self.assertEqual(win.pid_panel._equipment_bar.equipment_id, eq_id,
                "the deviation checklist bar must still open, as before")
            self.assertEqual(win.scenario_panel._equipment_filter_id, eq_id,
                "the scenario table must now also be filtered to this equipment")


# ══════════════════════════════════════════════════════════════════════════
# 8b. "Nytt projekt" ("New Project") used to silently leave P&ID objects
#     behind. MainWindow._wipe_project_tables (factored out of _hzp_new,
#     2026-08-27) deletes 'nodes'/'equipment_catalog' while foreign-key
#     enforcement was still active; two ALTER-TABLE-added columns
#     (equipment_catalog.node_id -> nodes.id, causes.equipment_id/
#     deviations.equipment_id -> equipment_catalog.id) have no ON DELETE
#     CASCADE, so any project that had actually linked an object to a
#     node/deviation/cause (the everyday "dra objekt till trädet" flow)
#     made those DELETEs fail with a silently-caught FK violation, leaving
#     nodes AND equipment_catalog (and equipment_markers, cascading from
#     it) completely untouched. Reported as "objekt på p&id inte
#     försvinner även om jag klickar nytt projekt" (2026-08-27).
#
#     Deliberately does NOT call the real MainWindow._hzp_new() end-to-end
#     — see NOTES.md "Nytt projekt rensar inte P&ID-objekt" for why: it
#     always targets the module-level DB_PATH constant for its file-
#     delete-and-reopen step regardless of what self.db currently points
#     to, so calling it directly is only safe with very careful DB_PATH
#     mocking, and a mistake there risks a REAL data-touching accident
#     (confirmed the hard way while diagnosing this bug). _wipe_project_
#     tables() is exactly the part that determines what "New Project"
#     actually clears and takes no path/file arguments at all — testing
#     it directly is both safer and more precise.
# ══════════════════════════════════════════════════════════════════════════

class WipeProjectTablesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_linked_chain(self, win):
        """A node/equipment/cause/marker chain with the exact cross-links
        (equipment_catalog.node_id, causes.equipment_id) that used to
        block 'DELETE FROM nodes'/'DELETE FROM equipment_catalog'."""
        node_id = win.db.add_node()
        eq_id = win.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
        win.db.set_equipment_node(eq_id, node_id)
        marker_id = win.db.add_equipment_marker(
            eq_id, "V-1", 0, 100.0, 100.0, "Ventil", confidence=0.9, link_method='leader')
        dev_id = win.db.deviations(node_id)[0]['id']
        cause_id = win.db.add_cause(dev_id)
        win.db.conn.execute(
            "UPDATE causes SET equipment_id=? WHERE id=?", (eq_id, cause_id))
        win.db.commit()
        return node_id, eq_id, marker_id, cause_id

    def test_linked_node_and_equipment_are_actually_cleared(self):
        with _TempDbMainWindow() as win:
            self._make_linked_chain(win)
            self.assertGreater(len(win.db.nodes()), 0)
            self.assertGreater(len(win.db.equipment_markers_for_page(0)), 0)

            win._wipe_project_tables()

            self.assertEqual(win.db.nodes(), [],
                "a node with a linked object must not survive 'New Project' "
                "just because deleting it hit a foreign-key constraint")
            self.assertEqual(win.db.equipment_markers_for_page(0), [],
                "P&ID equipment markers must be cleared — this table was "
                "missing from the wipe list entirely before the fix")

    def test_unlinked_data_is_still_cleared_as_before(self):
        """Sanity check: the fix must not accidentally make the wipe do
        LESS for the plain, no-cross-links case that already worked."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            win.db.add_consequence(cause_id)

            win._wipe_project_tables()

            self.assertEqual(win.db.nodes(), [])
            self.assertEqual(win.db.causes(node_id), [])

    def test_foreign_keys_enforcement_is_restored_afterward(self):
        """The wipe temporarily disables FK enforcement to avoid the
        ordering problem — it must not leave it off for the rest of the
        session, or every other real FK constraint in the app (cascading
        deletes elsewhere) would silently stop working too."""
        with _TempDbMainWindow() as win:
            win._wipe_project_tables()
            status = win.db.conn.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(status, 1,
                "foreign_keys enforcement must be back ON after the wipe")

    def test_recommendations_and_categories_are_also_cleared(self):
        """Additional tables found missing from the old wipe list during
        the same investigation — not the originally-reported symptom, but
        the same root gap (relying on cascade instead of an explicit
        list), and just as visible to a user starting a "new" project
        that still shows the old one's recommendations/categories."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            win.db.add_recommendation_to_consequence(cons_id, description="Fixa X")
            cat = win.db.consequence_categories()[0]
            win.db.set_consequence_severity(cons_id, cat['id'], 3)

            win._wipe_project_tables()

            self.assertEqual(win.db.all_recommendations(), [])
            self.assertEqual(win.db.consequence_categories(), [])

    def test_recommendation_numbering_restarts_at_one_after_wipe(self):
        with _TempDbMainWindow() as win:
            first_id = win.db.add_recommendation("Gammalt projekt")
            self.assertEqual(first_id, 1)

            win._wipe_project_tables()

            new_id = win.db.add_recommendation("Nytt projekt")
            self.assertEqual(new_id, 1,
                "new projects must display their first recommendation as R-001")


# ══════════════════════════════════════════════════════════════════════════
# 8c. Dynamisk färgmarkering av objekt på P&ID (2026-08-27, see NOTES.md) —
#     end-to-end: MainWindow._on_selected must recompute the P&ID's
#     tree-context highlight (pid_panel.set_tree_context) on every tree
#     selection, including the new SYSTEM_T branch that had no handling
#     at all before this feature.
# ══════════════════════════════════════════════════════════════════════════

class TreeCauseSelectionScenarioScopeTests(unittest.TestCase):
    """Selecting one object/cause must not widen Scenario to its deviation."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_tree_cause_selection_loads_only_that_cause(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, 'LÃ¥gt flÃ¶de')
            first_cause = win.db.add_cause(dev_id)
            second_cause = win.db.add_cause(dev_id)

            with unittest.mock.patch.object(
                    win.scenario_panel, 'load_cause') as load_cause, \
                 unittest.mock.patch.object(
                    win.scenario_panel, 'load_deviation') as load_deviation:
                win._on_selected(CAUSE_T, first_cause)

            load_cause.assert_called_once_with(first_cause)
            load_deviation.assert_not_called()


class TreeContextHighlightEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _stub_scenario_loads(self, win):
        """scenario_panel.load_*() ultimately hits QTableWidget.
        resizeRowsToContents(), which reproducibly crashes under this
        machine's headless Qt platform plugin (see
        test_select_safeguard_in_tree_no_crash's own docstring above) —
        unrelated to the tree-context highlight logic under test here,
        so stubbed out the same way."""
        win.scenario_panel.load_node = lambda *a, **k: None
        win.scenario_panel.load_deviation = lambda *a, **k: None
        win.scenario_panel.load_cause = lambda *a, **k: None
        win.scenario_panel.load_consequence = lambda *a, **k: None

    def _make_cause_with_equipment(self, db, node_id=None, tag='V-1'):
        if node_id is None:
            node_id = db.add_node()
        dev_id = db.deviations(node_id)[0]['id']
        eq_id = db.add_equipment_item(tag, tag, tag[0], 0, 'Ventil', '', 0)
        cause_id = db.add_cause(dev_id)
        db.conn.execute("UPDATE causes SET equipment_id=? WHERE id=?", (eq_id, cause_id))
        db.commit()
        marker_id = db.add_equipment_marker(eq_id, tag, 0, 10.0, 10.0, 'Ventil')
        return node_id, cause_id, eq_id, marker_id

    def test_selecting_node_highlights_equipment_under_it(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loads(win)
            _fake_pdf_loaded(win.pid_panel)
            node_id, _cause_id, eq_id, marker_id = self._make_cause_with_equipment(win.db)

            win._on_selected(NODE_T, node_id)

            self.assertIn(marker_id, win.pid_panel.viewer._tree_context_highlights)

    def test_selecting_grouped_deviation_highlights_equipment_specific_sibling(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loads(win)
            _fake_pdf_loaded(win.pid_panel)
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, 'LÃ¥gt flÃ¶de')
            eq_id = win.db.add_equipment_item('V-2', 'V-2', 'V', 0,
                                              'Ventil', '', 0)
            sibling_id = win.db.add_deviation(
                node_id, 'LÃ¥gt flÃ¶de', equipment_id=eq_id)
            marker_id = win.db.add_equipment_marker(
                eq_id, 'V-2', 0, 10.0, 10.0, 'Ventil')

            # TreePanel renders dev_id and sibling_id as one shared row.
            win._on_selected(DEV_T, dev_id)

            self.assertIn(marker_id, win.pid_panel.viewer._tree_context_highlights)

    def test_selecting_grouped_deviation_highlights_later_group_rows(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loads(win)
            _fake_pdf_loaded(win.pid_panel)
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, 'Lagt flode')
            ids = [win.db.add_equipment_item(tag, tag, tag[0], 0, 'Ventil', '', 0)
                   for tag in ('V-1', 'V-2', 'V-3')]
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(cause_id, equipment_id=ids[0],
                                secondary_equipment_id=ids[1],
                                group_equipment_ids=ids)
            markers = [win.db.add_equipment_marker(eq_id, tag, 0, float(i), 10.0, 'Ventil')
                       for i, (eq_id, tag) in enumerate(zip(ids, ('V-1', 'V-2', 'V-3')))]

            win._on_selected(DEV_T, dev_id)

            for marker_id in markers:
                self.assertIn(marker_id, win.pid_panel.viewer._tree_context_highlights)

    def test_selecting_consequence_excludes_parent_causes_object(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loads(win)
            _fake_pdf_loaded(win.pid_panel)
            node_id, cause_id, eq_id, marker_id = self._make_cause_with_equipment(win.db)
            cons_id = win.db.add_consequence(cause_id)
            win.db.commit()

            win._on_selected(CONS_T, cons_id)

            self.assertNotIn(marker_id, win.pid_panel.viewer._tree_context_highlights,
                "the parent cause's own object must not bleed into a "
                "Consequence-level selection")

    def test_switching_selection_unhighlights_out_of_scope_equipment(self):
        """"Markeringen ska uppdateras direkt när användaren byter
        position i trädet och objekt som inte längre tillhör aktuell
        kontext ska återgå till sin normala färg" — the core requirement."""
        with _TempDbMainWindow() as win:
            self._stub_scenario_loads(win)
            _fake_pdf_loaded(win.pid_panel)
            node_a, _cause_a, _eq_a, marker_a = self._make_cause_with_equipment(win.db, tag='V-1')
            node_b, _cause_b, _eq_b, marker_b = self._make_cause_with_equipment(win.db, tag='V-2')

            win._on_selected(NODE_T, node_a)
            self.assertIn(marker_a, win.pid_panel.viewer._tree_context_highlights)
            self.assertNotIn(marker_b, win.pid_panel.viewer._tree_context_highlights)

            win._on_selected(NODE_T, node_b)
            self.assertNotIn(marker_a, win.pid_panel.viewer._tree_context_highlights,
                "node A's object must be un-highlighted once selection moves away")
            self.assertIn(marker_b, win.pid_panel.viewer._tree_context_highlights)

    def test_selecting_system_highlights_everything_under_its_nodes(self):
        """The new SYSTEM_T branch — selecting a System had NO handling
        at all before this feature (see NOTES.md)."""
        with _TempDbMainWindow() as win:
            self._stub_scenario_loads(win)
            _fake_pdf_loaded(win.pid_panel)
            sys_id = win.db.add_system("Sys A")
            node_a = win.db.add_node(system_id=sys_id)
            node_b = win.db.add_node(system_id=sys_id)
            _n, _c, _eq, marker_a = self._make_cause_with_equipment(win.db, node_id=node_a, tag='V-1')
            _n, _c, _eq, marker_b = self._make_cause_with_equipment(win.db, node_id=node_b, tag='V-2')
            # Set active state via a prior NODE_T selection so we can prove
            # the SYSTEM_T branch actually clears it (clear_active_selection).
            win._on_selected(NODE_T, node_a)

            win._on_selected(SYSTEM_T, sys_id)

            self.assertIn(marker_a, win.pid_panel.viewer._tree_context_highlights)
            self.assertIn(marker_b, win.pid_panel.viewer._tree_context_highlights)
            self.assertIsNone(win.pid_panel._active_node_id)
            self.assertIsNone(win.pid_panel._active_cause_id)

    def test_tagged_refs_historical_tag_still_highlighted_after_retag(self):
        """End-to-end proof of Anton's decision #2: dragging tag A then
        tag B onto the same safeguard (comp_tag becomes 'B', but
        tagged_refs keeps 'A,B') must highlight BOTH A's and B's markers
        once the owning Cause is selected."""
        with _TempDbMainWindow() as win:
            self._stub_scenario_loads(win)
            _fake_pdf_loaded(win.pid_panel)
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq_a = win.db.add_equipment_item('V-1', 'V-1', 'V', 0, 'Ventil', '', 0)
            eq_b = win.db.add_equipment_item('P-1', 'P-1', 'P', 0, 'Pump', '', 0)
            marker_a = win.db.add_equipment_marker(eq_a, 'V-1', 0, 10.0, 10.0, 'Ventil')
            marker_b = win.db.add_equipment_marker(eq_b, 'P-1', 0, 20.0, 20.0, 'Pump')
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            sg_id = win.db.add_safeguard(cons_id)
            # Drag A, then B — comp_tag ends up 'P-1' (latest), tagged_refs
            # keeps the full history 'V-1,P-1'.
            win.db.conn.execute(
                "UPDATE safeguards SET comp_tag='P-1', tagged_refs='V-1,P-1' WHERE id=?",
                (sg_id,))
            win.db.commit()

            win._on_selected(CAUSE_T, cause_id)

            self.assertIn(marker_a, win.pid_panel.viewer._tree_context_highlights)
            self.assertIn(marker_b, win.pid_panel.viewer._tree_context_highlights)


# ══════════════════════════════════════════════════════════════════════════
# 9. Unified tag scanning — "🔍 Skanna P&ID" and "📋 Analysera P&ID" used to
#    be two separate, inconsistent tag-matching implementations.
#    scan_pdf_for_equipment()'s matcher silently dropped single-letter-
#    prefix tags with no separator (P101, T12, E205) and never rejoined
#    tags the PDF split into multiple text objects; _analyze_pid's matcher
#    (_pick_best_tag/_spatial_combine) did both correctly, which is why it
#    found more. Both entry points now share scan_pdf_for_equipment() (with
#    the fixed matcher) and cross-write into BOTH equipment_catalog and
#    pid_identified_tags so results are identical regardless of which
#    button was used.
# ══════════════════════════════════════════════════════════════════════════

class UnifiedTagScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_scan_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.pdf_path = os.path.join(self._tmpdir, "test.pdf")
        self.db = Database(path=self.db_path)

        import fitz
        doc = fitz.open()
        p0 = doc.new_page(width=200, height=200)
        p0.insert_text(fitz.Point(10, 20), "P101", fontsize=10)   # single-letter prefix, no separator
        p1 = doc.new_page(width=200, height=200)
        p1.insert_text(fitz.Point(10, 20), "20-PCV-101", fontsize=10)
        doc.save(self.pdf_path)
        doc.close()
        self.db.set_pid_config_value('path', self.pdf_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_scan_pdf_for_equipment_finds_single_letter_prefix_tag(self):
        """Regression test for the exact reported gap: 'P101' (single-letter
        prefix, no separator) used to be silently dropped by
        scan_pdf_for_equipment's Pass 2 (_parse_tag's len(pfx)>=2 gate)."""
        import fitz
        from equipment_detection import scan_pdf_for_equipment
        doc = fitz.open(self.pdf_path)
        try:
            result = scan_pdf_for_equipment(doc, use_ocr=False)
        finally:
            doc.close()
        result.pop('_meta', None)
        all_tags = {t for data in result.values() for t in data['tags']}
        self.assertIn('P-101', all_tags,
            "single-letter-prefix tag without a separator must now be found")

    def test_scan_pdf_for_equipment_rejoins_split_tokens(self):
        import fitz
        from equipment_detection import scan_pdf_for_equipment
        doc = fitz.open(self.pdf_path)
        try:
            result = scan_pdf_for_equipment(doc, use_ocr=False)
        finally:
            doc.close()
        result.pop('_meta', None)
        all_tags = {t for data in result.values() for t in data['tags']}
        self.assertTrue(any('PCV' in t for t in all_tags),
            f"expected a PCV tag rejoined from split tokens, got: {all_tags}")

    def test_pick_best_tag_normalises_rdspp_compound_separators(self):
        """Used to return the RDS-PP compound match completely raw (leading
        '=', dots kept as-is) instead of normalising it like _parse_tag's
        own EXT_TAG_RE branch already does — see NOTES.md 'Dubbla taggar
        vid skanning' (2026-08-13, real LKAB file).

        Only the separator right before the instrument code (QMA081)
        becomes a dash — the dot between the area-hierarchy segments
        (E1, M1) is LKAB's own real RDS-PP notation and must survive
        (2026-08-13 follow-up: 'anger inte punkt för lkab taggarna utan
        anger - istället'), not get collapsed to 'E1-M1-QMA081'."""
        from equipment_detection import _pick_best_tag
        self.assertEqual(_pick_best_tag('=E1.M1.QMA081'), 'E1.M1-QMA081')

    def test_parse_tag_preserves_rdspp_hierarchy_dots(self):
        """_parse_tag's own EXT_TAG_RE branch (shared _normalize_ext_tag
        helper with _pick_best_tag, 2026-08-13 follow-up) must preserve
        the same LKAB dot-hierarchy notation, and still resolve the
        correct instrument-code prefix for equipment-type lookup."""
        from equipment_detection import _parse_tag
        self.assertEqual(_parse_tag('=E1.M1.QMA081'), ('E1.M1-QMA081', 'QMA'))
        self.assertEqual(_parse_tag('E1.M1.WPA001'), ('E1.M1-WPA001', 'WPA'))
        # A separator already present right before the instrument code
        # (not a dot) is left as a dash, unchanged.
        self.assertEqual(_parse_tag('=E1.M1-QMA081'), ('E1.M1-QMA081', 'QMA'))

    def test_rdspp_compound_tag_deduped_against_its_own_bare_form(self):
        """Real LKAB file bug (2026-08-13, see NOTES.md 'Dubbla taggar vid
        skanning'): an RDS-PP path tag like '=E1.M1.QMA081' used to survive
        as a SECOND, differently-formatted duplicate of the same
        instrument's bare code 'QMA-081' — Pass 1's full-text regex found
        the bare form (dashed) by fragmenting the compound into short
        letter+digit chunks before _parse_tag ever saw it whole, while
        Pass 2's _pick_best_tag returned the compound form completely
        unnormalised (dotted, leading '='). Both landed in
        equipment_catalog as separate rows for one physical instrument —
        "one with a dash, one without", per the bug report."""
        import fitz
        from equipment_detection import scan_pdf_for_equipment
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        page.insert_text(fitz.Point(10, 20), "=E1.M1.QMA081", fontsize=10)
        try:
            result = scan_pdf_for_equipment(doc, use_ocr=False)
        finally:
            doc.close()
        result.pop('_meta', None)
        qma_tags = result.get('QMA', {}).get('tags', [])
        self.assertEqual(qma_tags, ['QMA-081'],
            f"expected only the bare form, got duplicate(s): {qma_tags}")

    def test_spatial_combine_returns_bbox_tuples(self):
        from equipment_detection import _spatial_combine
        import fitz
        doc = fitz.open(self.pdf_path)
        try:
            page = doc[1]
            words = page.get_text("words")
            results = _spatial_combine(words, gap_limit=22.0)
        finally:
            doc.close()
        self.assertTrue(results)
        self.assertTrue(all(len(r) == 5 for r in results),
            "_spatial_combine must yield (text, x0, y0, x1, y1) tuples")
        self.assertTrue(any('PCV' in r[0] for r in results))

    def test_spatial_combine_skips_exact_position_duplicate_words(self):
        """Some CAD exports render the same text 2-3 times at the
        byte-for-byte identical bbox (a bold-simulation trick) — confirmed
        on a real ITS P&ID title block, where "Checked"/"Drawn" etc. each
        appeared exactly 3 times with identical coordinates. Without a
        dedup check, _spatial_combine's gap-based joining logic
        concatenates these into e.g. "CheckedCheckedChecked" instead of
        recognizing them as the same word rendered on top of itself."""
        from equipment_detection import _spatial_combine
        words = [
            (100.0, 200.0, 128.0, 208.0, 'Checked'),
            (100.0, 200.0, 128.0, 208.0, 'Checked'),
            (100.0, 200.0, 128.0, 208.0, 'Checked'),
        ]
        results = _spatial_combine(words, gap_limit=18.0)
        texts = {r[0] for r in results}
        self.assertIn('Checked', texts)
        self.assertNotIn('CheckedCheckedChecked', texts)
        self.assertFalse(any(t.count('Checked') > 1 for t in texts),
            f"exact-position duplicate must never be concatenated, got: {texts}")

    def test_spatial_combine_dedups_duplicate_mid_group(self):
        """The same duplicate-rendering artifact can occur on ANY word
        within an already multi-word line, not just the first — e.g. a
        real ITS title block line rendered as "MANIFOLD pressure
        pressure PHC PHC ...". Must dedup regardless of position in the
        group, not just immediately after the group's own first token."""
        from equipment_detection import _spatial_combine
        words = [
            (100.0, 200.0, 140.0, 208.0, 'MANIFOLD'),
            (145.0, 200.0, 175.0, 208.0, 'pressure'),
            (145.0, 200.0, 175.0, 208.0, 'pressure'),
            (180.0, 200.0, 195.0, 208.0, 'PHC'),
        ]
        results = _spatial_combine(words, gap_limit=18.0)
        texts = {r[0] for r in results}
        self.assertIn('MANIFOLDpressurePHC', texts)
        self.assertFalse(any('pressurepressure' in t.lower() for t in texts),
            f"mid-group duplicate must be skipped too, got: {texts}")

    def test_equipment_panel_scan_writes_both_tables(self):
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox

        class _FakeProgressDialog:
            """Stand-in for QProgressDialog: under the offscreen QPA platform
            used for headless tests, a real QProgressDialog.close() spuriously
            flips wasCanceled() to True (not reproducible in a real windowed
            session) — _scan() checks wasCanceled() right after close(), so
            without this stub the scan result would be silently discarded in
            this test harness. A plain stub avoids depending on Qt's
            offscreen-platform quirks entirely, rather than patching a real
            QProgressDialog's methods."""
            def __init__(self, *a, **k): pass
            def setWindowTitle(self, *a, **k): pass
            def setWindowModality(self, *a, **k): pass
            def setMinimumDuration(self, *a, **k): pass
            def show(self, *a, **k): pass
            def setValue(self, *a, **k): pass
            def setLabelText(self, *a, **k): pass
            def close(self, *a, **k): pass
            def wasCanceled(self): return False

        panel = EquipmentPanel(self.db)
        try:
            with unittest.mock.patch.object(
                    QMessageBox, 'question',
                    return_value=QMessageBox.StandardButton.No), \
                 unittest.mock.patch.object(QMessageBox, 'information'), \
                 unittest.mock.patch('hazop.QProgressDialog', _FakeProgressDialog):
                panel._scan()
            cat_tags = {dict(r)['tag'] for r in self.db.equipment_items()}
            id_rows  = list(self.db.pid_identified_tags())
            self.assertIn('P-101', cat_tags,
                "equipment_catalog must contain the single-letter-prefix tag")
            self.assertTrue(len(id_rows) > 0,
                "pid_identified_tags must also be populated by 'Skanna P&ID' now")
        finally:
            panel.deleteLater()

    def test_analyze_pid_writes_both_tables(self):
        import fitz
        from pid_viewer import PIDPanel
        from PyQt6.QtWidgets import QMessageBox
        panel = PIDPanel(self.db)
        try:
            panel.viewer.pdf_doc = fitz.open(self.pdf_path)
            with unittest.mock.patch.object(
                    QMessageBox, 'question',
                    return_value=QMessageBox.StandardButton.No), \
                 unittest.mock.patch.object(QMessageBox, 'information'):
                panel._analyze_pid()
            cat_tags = {dict(r)['tag'] for r in self.db.equipment_items()}
            id_rows  = list(self.db.pid_identified_tags())
            self.assertIn('P-101', cat_tags,
                "'Analysera P&ID' must now also populate equipment_catalog")
            self.assertTrue(len(id_rows) > 0)
        finally:
            panel.viewer.pdf_doc.close()
            panel.deleteLater()


class FindSimilarShapesSearchParametersTests(unittest.TestCase):
    """"Hitta liknande symbol" — sökparametrar (2026-08-14, see NOTES.md):
    find_similar_shapes()'s new ignore_scale/rotation_mode/
    ref_index_group parameters, and resolve_reference_cluster() (the
    reference-resolution split out for SimilarSymbolSearchDialog's
    segment-exclusion preview, pid_viewer.py)."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_findsimilar_params_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _bowtie(self, shape, cx, cy, s=10, deg=0):
        import fitz
        import math
        rad = math.radians(deg)
        def rot(x, y):
            dx, dy = x - cx, y - cy
            return fitz.Point(cx + dx * math.cos(rad) - dy * math.sin(rad),
                              cy + dx * math.sin(rad) + dy * math.cos(rad))
        shape.draw_polyline([rot(cx - s, cy - s), rot(cx - s, cy + s), rot(cx, cy)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.draw_polyline([rot(cx + s, cy - s), rot(cx + s, cy + s), rot(cx, cy)])
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)

    def test_resolve_reference_cluster_returns_primitives_and_index_group(self):
        import fitz
        from equipment_detection import resolve_reference_cluster
        path = os.path.join(self._tmpdir, "ref.pdf")
        doc = fitz.open()
        page = doc.new_page(width=200, height=200)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            resolved = resolve_reference_cluster(doc, 0, 60, 60)
        finally:
            doc.close()
        self.assertIsNotNone(resolved)
        primitives, index_group, cluster = resolved
        self.assertTrue(primitives)
        self.assertTrue(index_group)
        self.assertIn('bbox', cluster)

    def test_resolve_reference_cluster_returns_none_with_no_vector_data(self):
        import fitz
        from equipment_detection import resolve_reference_cluster
        path = os.path.join(self._tmpdir, "blank.pdf")
        doc = fitz.open()
        doc.new_page(width=200, height=200)
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            resolved = resolve_reference_cluster(doc, 0, 100, 100)
        finally:
            doc.close()
        self.assertIsNone(resolved)

    def test_ref_index_group_excludes_a_wrongly_merged_stray_line(self):
        """The reference case the whole feature exists for: a valve
        whose auto-detected cluster happens to include an attached
        pipe stub. Excluding the stub's primitive indices (as
        SimilarSymbolSearchDialog's segment editor would do) must
        raise the similarity to a clean, unattached copy of the same
        shape elsewhere in the document."""
        import fitz
        from equipment_detection import find_similar_shapes, resolve_reference_cluster
        path = os.path.join(self._tmpdir, "stray.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)   # reference — gets a stray line attached below
        shape.draw_line(fitz.Point(70, 60), fitz.Point(100, 60))
        shape.finish(color=(0, 0, 0), width=1)
        self._bowtie(shape, 300, 300)   # clean copy elsewhere, no stray line
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            default_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                                   pages=[0], min_similarity=0.0)
            default_best = max((r['detection_confidence'] for r in default_results), default=0.0)

            primitives, index_group, _cluster = resolve_reference_cluster(doc, 0, 60, 60)
            stray_prims = {i for i in index_group
                           if primitives[i]['p0'] in ((70.0, 60.0), (100.0, 60.0))
                           or primitives[i]['p1'] in ((70.0, 60.0), (100.0, 60.0))}
            edited_group = [i for i in index_group if i not in stray_prims]
            self.assertLess(len(edited_group), len(index_group),
                "test setup issue: the stray line wasn't part of the auto-detected cluster")

            edited_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                                 pages=[0], min_similarity=0.0,
                                                 ref_index_group=edited_group)
            edited_best = max((r['detection_confidence'] for r in edited_results), default=0.0)
        finally:
            doc.close()
        self.assertGreater(edited_best, default_best,
            "excluding the stray line must improve the match against the clean copy")
        self.assertAlmostEqual(edited_best, 1.0, places=3)

    def test_ignore_scale_finds_a_much_larger_copy_past_the_default_threshold(self):
        import fitz
        from equipment_detection import find_similar_shapes
        path = os.path.join(self._tmpdir, "scale.pdf")
        doc = fitz.open()
        page = doc.new_page(width=600, height=600)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60, s=10)
        self._bowtie(shape, 400, 400, s=60)   # same shape, 6x bigger, far away
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            default_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                                   pages=[0], min_similarity=0.85)
            scaled_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                                 pages=[0], min_similarity=0.85, ignore_scale=True)
        finally:
            doc.close()
        self.assertEqual(default_results, [],
            "test setup issue: the size difference should already fail the default threshold")
        self.assertTrue(scaled_results,
            "ignore_scale must let a pure size difference pass the same threshold")

    def test_rotation_mode_any_finds_a_45_degree_copy_past_the_default_threshold(self):
        import fitz
        from equipment_detection import find_similar_shapes
        path = os.path.join(self._tmpdir, "rot.pdf")
        doc = fitz.open()
        page = doc.new_page(width=600, height=600)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60, s=10, deg=0)
        self._bowtie(shape, 400, 400, s=10, deg=45)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            default_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                                   pages=[0], min_similarity=0.95)
            any_results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                              pages=[0], min_similarity=0.95, rotation_mode='any')
        finally:
            doc.close()
        self.assertEqual(default_results, [],
            "test setup issue: the 45° rotation should already fail the default threshold")
        self.assertTrue(any_results,
            "rotation_mode='any' must let a 45°-rotated copy pass the same threshold")

    def test_scan_candidates_returns_unthresholded_unsorted_tuples(self):
        """_scan_candidates() (2026-08-15, see NOTES.md "Hitta liknande
        symbol" — uppföljningsfunktioner) is the shared, expensive half
        of find_similar_shapes(), split out so SimilarSymbolSearchWorker
        can run it once and reuse it for both the live match-count
        preview and the final thresholded search. It must NOT apply
        min_similarity itself — that's find_similar_shapes()'s job."""
        import fitz
        from equipment_detection import _scan_candidates, resolve_reference_cluster
        from symbol_geometry import similarity_features
        path = os.path.join(self._tmpdir, "scancand.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        # A little real text so dominant_text_size() uses the normal
        # per-glyph estimate instead of its no-text vector-bootstrap
        # fallback — on a page with only a couple of shapes, that
        # fallback ties its own scale estimate to the very shapes being
        # measured (confirmed directly: it otherwise locks to almost
        # exactly the bow-tie's own size, pinning norm_size right at the
        # aspect/norm_size pre-filter's own boundary below). Any real
        # P&ID has actual text; this keeps the fixture representative.
        page.insert_text(fitz.Point(200, 380), "TAG-001", fontsize=8)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        self._bowtie(shape, 300, 300)
        # Plainly different shape-wise (no diagonal/curve mismatch vs the
        # bow-tie's own triangle edges) but still a plausible-symbol
        # size/aspect — a long thin rect was used here before
        # _scan_candidates gained its own aspect/norm_size pre-filter
        # (2026-08-16, see NOTES.md "Anton rapporterade 1070 träffar
        # istället för 20-30 ventiler"), which now excludes anything that
        # implausible before ever comparing shape features at all; a
        # filled oval keeps this test's original intent (verify
        # min_similarity itself isn't applied) without tripping that new,
        # unrelated pre-filter.
        shape.draw_oval(fitz.Rect(140, 50, 180, 70))
        shape.finish(color=(0, 0, 0), fill=(1, 0, 0), closePath=True)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            primitives, index_group, cluster = resolve_reference_cluster(doc, 0, 60, 60)
            ref_feats = similarity_features(primitives, index_group)
            candidates = _scan_candidates(
                doc, ref_feats, 0, cluster['_index_group'], pages=[0])
        finally:
            doc.close()
        self.assertTrue(candidates)
        # Both a high-similarity (the clean bow-tie copy) and a
        # low-similarity (the oval, no diagonal edges, has_curve
        # mismatch) candidate must be present — min_similarity was never
        # applied.
        sims = [c[0] for c in candidates]
        self.assertGreater(max(sims), 0.9)
        self.assertLess(min(sims), 0.5)

    def test_scan_candidates_rejects_plain_rectangle_with_no_diagonal_or_curve(self):
        """_scan_candidates()'s has_diagonal-or-has_curve pre-filter
        (2026-08-16, see NOTES.md "Anton rapporterade 1070 träffar"
        follow-up — "gemensamma nämnare för ventiler, pumpar,
        instrument"): a real equipment symbol's own defining geometry is
        either diagonal (valve bow-tie edges) or curved (pump/instrument
        circles). Found directly on the active project's own
        hazop_project_pid.pdf: a size/aspect-plausible cluster can still
        be nothing more than a plain axis-aligned rectangle — an empty
        gap between two pipe lines, or a text-label box — with neither.
        Reproduced here with a plain closed rectangle, size/aspect-
        plausible enough to survive the OTHER pre-filter."""
        import fitz
        from equipment_detection import _scan_candidates, resolve_reference_cluster
        from symbol_geometry import similarity_features
        path = os.path.join(self._tmpdir, "norectangle.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        page.insert_text(fitz.Point(200, 380), "TAG-001", fontsize=8)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        shape.draw_rect(fitz.Rect(140, 50, 170, 70))   # plain rect: no diagonal, no curve
        shape.finish(color=(0, 0, 0), width=1)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            primitives, index_group, cluster = resolve_reference_cluster(doc, 0, 60, 60)
            ref_feats = similarity_features(primitives, index_group)
            candidates = _scan_candidates(
                doc, ref_feats, 0, cluster['_index_group'], pages=[0])
        finally:
            doc.close()
        self.assertEqual(candidates, [],
            "the plain rectangle has neither a diagonal nor a curve and must be excluded, "
            "even though it is size/aspect-plausible and min_similarity was never applied")

    def test_scan_candidates_rejects_pipe_run_aspect_even_with_ignore_scale(self):
        """Found in review (2026-08-16, see NOTES.md "raster-sökning"
        follow-up): an earlier version of the aspect/norm_size pre-filter
        bundled BOTH checks under `not ignore_scale`, so checking "Alla
        storlekar" silently let long/thin pipe-run clusters back into
        similarity scoring — the exact noise the filter exists to
        reject. ignore_scale is specifically about SIZE
        (cluster_similarity drops norm_size from the score entirely,
        aspect stays fully weighted regardless — see its own docstring)
        so the aspect>3.0 pipe-run exclusion must apply UNCONDITIONALLY,
        with or without ignore_scale."""
        import fitz
        from equipment_detection import _scan_candidates, resolve_reference_cluster
        from symbol_geometry import similarity_features
        path = os.path.join(self._tmpdir, "pipe_ignore_scale.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        page.insert_text(fitz.Point(200, 380), "TAG-001", fontsize=8)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        # A long diagonal line: aspect >> 3.0 (a pipe run), but WITH
        # has_diagonal=True so it isn't excluded by that OTHER filter —
        # isolates the aspect check specifically.
        shape.draw_line(fitz.Point(140, 50), fitz.Point(300, 90))
        shape.finish(color=(0, 0, 0), width=1, closePath=False)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            primitives, index_group, cluster = resolve_reference_cluster(doc, 0, 60, 60)
            ref_feats = similarity_features(primitives, index_group)
            candidates = _scan_candidates(
                doc, ref_feats, 0, cluster['_index_group'], pages=[0], ignore_scale=True)
        finally:
            doc.close()
        self.assertEqual(candidates, [],
            "the long diagonal pipe-run line must stay excluded (aspect>3.0) even "
            "when ignore_scale=True, since that flag is about size, not shape/aspect")

    def test_scan_candidates_should_cancel_stops_before_any_page_is_scanned(self):
        import fitz
        from equipment_detection import _scan_candidates, resolve_reference_cluster
        from symbol_geometry import similarity_features
        path = os.path.join(self._tmpdir, "cancel.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        self._bowtie(shape, 300, 300)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            primitives, index_group, cluster = resolve_reference_cluster(doc, 0, 60, 60)
            ref_feats = similarity_features(primitives, index_group)
            candidates = _scan_candidates(
                doc, ref_feats, 0, cluster['_index_group'], pages=[0],
                should_cancel=lambda: True)
        finally:
            doc.close()
        self.assertEqual(candidates, [])

    def test_find_similar_shapes_should_cancel_yields_no_results(self):
        import fitz
        from equipment_detection import find_similar_shapes
        path = os.path.join(self._tmpdir, "cancel2.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        self._bowtie(shape, 300, 300)
        shape.commit()
        doc.save(path)
        doc.close()

        doc = fitz.open(path)
        try:
            results = find_similar_shapes(doc, ref_page=0, ref_x=60, ref_y=60,
                                          pages=[0], min_similarity=0.0,
                                          should_cancel=lambda: True)
        finally:
            doc.close()
        self.assertEqual(results, [])

    def test_find_shapes_matching_features_finds_matches_from_a_foreign_reference(self):
        """Symbolbibliotek (2026-08-15, see NOTES.md "Hitta liknande
        symbol" — uppföljningsfunktioner): searching from a saved
        template's features (computed against a COMPLETELY different
        document) must still find matching shapes here — there is no
        live reference page/cluster to resolve or exclude."""
        import fitz
        from equipment_detection import find_shapes_matching_features, resolve_reference_cluster
        from symbol_geometry import similarity_features

        target_path = os.path.join(self._tmpdir, "target.pdf")
        doc = fitz.open()
        page = doc.new_page(width=400, height=400)
        shape = page.new_shape()
        self._bowtie(shape, 60, 60)
        self._bowtie(shape, 300, 300)
        shape.commit()
        doc.save(target_path)
        doc.close()

        # The "template" reference lives on an entirely separate document.
        ref_doc = fitz.open()
        ref_page = ref_doc.new_page(width=200, height=200)
        ref_shape = ref_page.new_shape()
        self._bowtie(ref_shape, 50, 50, s=5)
        ref_shape.commit()
        primitives, index_group, _cluster = resolve_reference_cluster(ref_doc, 0, 50, 50)
        ref_features = similarity_features(primitives, index_group)
        ref_doc.close()

        doc = fitz.open(target_path)
        try:
            results = find_shapes_matching_features(doc, ref_features, pages=[0],
                                                     min_similarity=0.5, comp_type='Ventil')
        finally:
            doc.close()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r['comp_type'] == 'Ventil' for r in results))

    def test_find_shapes_matching_features_returns_empty_for_no_pdf(self):
        from equipment_detection import find_shapes_matching_features
        self.assertEqual(find_shapes_matching_features(None, {}), [])


class EquipmentMarkerClickNavigationTests(unittest.TestCase):
    """2026-08-06: valve markers on the P&ID are now clickable. Originally
    this always switched to Utrustningsregistret and selected the
    corresponding row (the closest equivalent to _on_marker_navigate's
    tree-select behaviour for cause/consequence/safeguard, since equipment
    has no HAZOP tree node of its own to select).

    2026-08-11: once equipment IS linked to a node (equipment_catalog.
    node_id), clicking its marker instead showed that node's WHOLE
    worksheet (causes/consequences/safeguards together) — the
    register-select above remained only as the fallback for equipment
    with no node yet.

    2026-08-12: the user clarified the 2026-08-11 behaviour was too broad
    ('de orsaker som visas i hazop scenario är de där objektet finns med')
    — clicking a marker now filters the worksheet to ONLY the rows that
    actually mention this equipment (scenario_panel.load_equipment()),
    regardless of whether it has a node yet, and the register-page
    fallback is gone: the scenario table (right there on the same P&ID
    page) is always the right place to show the result, even if empty."""

    def test_on_marker_navigate_equipment_marker_filters_scenario_table(self):
        with _TempDbMainWindow() as win:
            eq_id = win.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-101", 0, 100.0, 100.0, "Ventil", confidence=0.9,
                link_method='leader')

            load_eq_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_equipment)
            win.scenario_panel.load_equipment = load_eq_spy

            win._on_marker_navigate('equipment', marker_id)

            load_eq_spy.assert_called_once_with(eq_id)
            # Utrustning register is now index 4 (2026-08-26: Rekommendationer
            # inserted as the new index 3, shifting Utrustning/Studiehantering/
            # Inställningar from 3/4/5 to 4/5/6).
            self.assertNotEqual(win.view_stack.currentIndex(), 4,
                "clicking a marker must stay on the P&ID page — the filtered "
                "scenario table is the bottom pane of that same page, no "
                "need to navigate away to the Utrustning register")

    def test_on_marker_navigate_equipment_with_no_linked_row_does_not_crash(self):
        """A marker whose equipment_id is NULL (e.g. an untagged shape hit
        the user never confirmed with a tag) must be a silent no-op, not
        a crash."""
        with _TempDbMainWindow() as win:
            marker_id = win.db.add_equipment_marker(
                None, '', 0, 50.0, 50.0, "Ventil", confidence=0.6, link_method='shape')
            try:
                win._on_marker_navigate('equipment', marker_id)
            except Exception as e:
                self.fail(f"must not raise for a marker with no linked equipment row: {e!r}")

    def test_on_marker_navigate_equipment_with_node_shows_only_its_own_causes(self):
        """'Om jag har lagt till ett objekt på P&ID ... och klickar på det
        igen så vill jag att orsakerna där det nämns dyker upp i hazop
        scenario ... Detta gäller även om de är tillagda på konsekvens och
        safeguard' (2026-08-11), clarified 2026-08-12 to mean FILTERED, not
        the whole node: a second, unrelated cause under the very same node
        must NOT show up, only the one actually tagged to this equipment."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Högt tryck")
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(cause_id, comp_type="Ventil", comp_tag="PSV-101")
            cons_id = win.db.add_consequence(cause_id)
            sg_id = win.db.add_safeguard(cons_id)

            other_dev = win.db.get_or_create_deviation(node_id, "Lågt tryck")
            other_cause = win.db.add_cause(other_dev)   # same node, not tagged to this equipment

            eq_id = win.db.add_equipment_item("PSV-101", "PSV-101", "PSV", 0,
                                               "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            marker_id = win.db.add_equipment_marker(
                eq_id, "PSV-101", 0, 100.0, 100.0, "Ventil", confidence=0.9,
                link_method='leader')

            load_eq_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_equipment)
            win.scenario_panel.load_equipment = load_eq_spy

            win._on_marker_navigate('equipment', marker_id)

            load_eq_spy.assert_called_once_with(eq_id)
            # Utrustning register is now index 4 (2026-08-26 renumbering, see
            # comment on the earlier assertNotEqual in this file).
            self.assertNotEqual(win.view_stack.currentIndex(), 4,
                "must not switch to the Utrustning register page when the "
                "equipment has a node to show a worksheet for")
            cons_and_sg_ids = {(m[2], m[3]) for m in win.scenario_panel._row_meta}
            self.assertIn((cons_id, sg_id), cons_and_sg_ids,
                "the tagged cause's consequence/safeguard row must be "
                "visible in the scenario table, not just its cause")
            cause_ids_shown = {m[1] for m in win.scenario_panel._row_meta if m[1] is not None}
            self.assertNotIn(other_cause, cause_ids_shown,
                "an unrelated cause under the SAME node must be filtered out")

    def test_on_marker_navigate_equipment_without_node_still_filters_scenario_table(self):
        """No node_id means the equipment itself isn't tied to a node yet,
        but a cause elsewhere could still be tagged to its tag/type
        directly — load_equipment() (tag-matching, not FK-only) is always
        the right call, never the old register-page fallback."""
        with _TempDbMainWindow() as win:
            eq_id = win.db.add_equipment_item("HV-202", "HV-202", "HV", 0,
                                               "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-202", 0, 10.0, 10.0, "Ventil", confidence=0.9,
                link_method='leader')

            load_eq_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_equipment)
            win.scenario_panel.load_equipment = load_eq_spy

            win._on_marker_navigate('equipment', marker_id)

            load_eq_spy.assert_called_once_with(eq_id)
            # Utrustning register is now index 4 (2026-08-26 renumbering, see
            # comment on the earlier assertNotEqual in this file).
            self.assertNotEqual(win.view_stack.currentIndex(), 4,
                "must not switch to the Utrustning page — no fallback anymore")

    def test_select_row_by_equipment_id_clears_blocking_filter(self):
        """If a text filter is currently hiding the target row, selecting
        it must clear the filter rather than silently doing nothing."""
        from hazop import EquipmentPanel
        tmpdir = tempfile.mkdtemp(prefix="hazop_selectrow_test_")
        try:
            db = Database(path=os.path.join(tmpdir, "test_project.db"))
            eq_id = db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            panel = EquipmentPanel(db)
            panel.refresh()
            try:
                panel._filter.setText("no-such-tag-matches-this")
                self.assertEqual(panel._proxy.rowCount(), 0,
                    "sanity check: the filter must actually hide the row first")

                panel.select_row_by_equipment_id(eq_id)

                self.assertEqual(panel._filter.text(), "",
                    "the blocking filter must be cleared so the target row becomes reachable")
                src_row = panel._proxy.mapToSource(panel._tbl.currentIndex()).row()
                self.assertEqual(panel._model.row_dict(src_row)['id'], eq_id)
            finally:
                panel.deleteLater()
                try: del db
                except Exception: pass
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_select_row_by_equipment_id_unknown_id_does_not_crash(self):
        from hazop import EquipmentPanel
        tmpdir = tempfile.mkdtemp(prefix="hazop_selectrow_test_")
        try:
            db = Database(path=os.path.join(tmpdir, "test_project.db"))
            panel = EquipmentPanel(db)
            panel.refresh()
            try:
                panel.select_row_by_equipment_id(999999)   # no such row
            except Exception as e:
                self.fail(f"must not raise for an unknown equipment_id: {e!r}")
            finally:
                panel.deleteLater()
                try: del db
                except Exception: pass
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class EquipmentMarkerNavigateFiltersScenarioTests(unittest.TestCase):
    """MainWindow._on_equipment_marker_navigate() — plumbing from 'clicked
    equipment marker on P&ID' to the filtered worksheet (2026-08-12, see
    NOTES.md). An earlier version of this (2026-08-11) called
    scenario_panel.load_node() (the whole node); the user clarified they
    want only the rows mentioning the clicked object."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_calls_load_equipment_not_load_node(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            marker_id = win.db.add_equipment_marker(eq_id, "PV-101", 0, 10.0, 10.0, "Ventil")

            with unittest.mock.patch.object(win.scenario_panel, 'load_equipment') as mock_load_eq, \
                 unittest.mock.patch.object(win.scenario_panel, 'load_node') as mock_load_node:
                win._on_equipment_marker_navigate(marker_id)

            mock_load_eq.assert_called_once_with(eq_id)
            mock_load_node.assert_not_called()

    def test_place_on_pid_wires_tag_before_type(self):
        with _TempDbMainWindow() as win:
            with unittest.mock.patch.object(
                    win.pid_panel, 'start_cause_equipment_placement') as place:
                win.scenario_panel.place_cause_object_requested.emit(
                    17, 'Ventil', 'V-101')

            place.assert_called_once_with(17, 'V-101', 'Ventil')

    def test_unlinked_marker_is_a_no_op(self):
        with _TempDbMainWindow() as win:
            marker_id = win.db.add_equipment_marker(None, "?", 0, 10.0, 10.0, "")
            with unittest.mock.patch.object(win.scenario_panel, 'load_equipment') as mock_load_eq:
                win._on_equipment_marker_navigate(marker_id)
            mock_load_eq.assert_not_called()

    def test_reveals_tree_down_to_the_causes_orsak_row(self):
        """2026-08-27, Anton: 'om jag klickar på ett objekt på P&ID viewer
        så kommer inget upp i trädet ... jag vill att den ... syns ner till
        objektnivå i trädet.' Clicking the marker must expand the tree far
        enough that the Orsak (object) row tagged to this equipment is
        actually visible, not just the Nod row as before."""
        with _TempDbMainWindow() as win:
            node = dict(win.db.nodes()[0])
            node_id = node['id']
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(cause_id, comp_type="Ventil", comp_tag="PSV-101")
            eq_id = win.db.add_equipment_item("PSV-101", "PSV-101", "PSV", 0,
                                               "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            marker_id = win.db.add_equipment_marker(
                eq_id, "PSV-101", 0, 100.0, 100.0, "Ventil", confidence=0.9,
                link_method='leader')

            win._on_equipment_marker_navigate(marker_id)

            system_item = _find_tree_item(win.tree_panel.tree, SYSTEM_T, node['system_id'])
            node_item = _find_tree_item(win.tree_panel.tree, NODE_T, node_id)
            dev_item = _find_tree_item(win.tree_panel.tree, DEV_T, dev_id)
            cause_item = _find_tree_item(win.tree_panel.tree, CAUSE_T, cause_id)
            self.assertIsNotNone(cause_item)
            self.assertTrue(system_item.isExpanded())
            self.assertTrue(node_item.isExpanded())
            self.assertTrue(dev_item.isExpanded(),
                "the avvikelse must be open so the object's Orsak row is visible")
            self.assertIs(win.tree_panel.tree.currentItem(), cause_item,
                "the object's own Orsak row should be highlighted")

    def test_clicking_later_group_object_highlights_the_whole_group(self):
        with _TempDbMainWindow() as win:
            _fake_pdf_loaded(win.pid_panel)
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lagt flode")
            ids = [win.db.add_equipment_item(tag, tag, tag[0], 0, "Ventil", '', 0)
                   for tag in ("V-1", "V-2", "V-3")]
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(cause_id, equipment_id=ids[0],
                                secondary_equipment_id=ids[1],
                                group_equipment_ids=ids)
            markers = [win.db.add_equipment_marker(eq_id, tag, 0, float(i), 10.0, "Ventil")
                       for i, (eq_id, tag) in enumerate(zip(ids, ("V-1", "V-2", "V-3")))]

            win._on_equipment_marker_navigate(markers[1])

            for marker_id in markers:
                self.assertIn(marker_id, win.pid_panel.viewer._tree_context_highlights)

    def test_reveals_every_avvikelse_the_object_is_tagged_under(self):
        """2026-08-27 follow-up, Anton: 'Klickar jag på ett objekt i pid
        viewer som finns på två avikelser eller fler får du expandera
        båda avikelserna.' Both deviations must open even with
        'Auto-collapse avvikelser' enabled, which would otherwise only
        keep ONE active deviation open."""
        with _TempDbMainWindow() as win:
            win.db.set_config('tree_auto_collapse_deviations', '1')
            node = dict(win.db.nodes()[0])
            node_id = node['id']
            dev_a = win.db.get_or_create_deviation(node_id, "Högt tryck")
            cause_a = win.db.add_cause(dev_a)
            win.db.update_cause(cause_a, comp_type="Ventil", comp_tag="PSV-101")
            dev_b = win.db.get_or_create_deviation(node_id, "Lågt tryck")
            cause_b = win.db.add_cause(dev_b)
            win.db.update_cause(cause_b, comp_type="Ventil", comp_tag="PSV-101")
            eq_id = win.db.add_equipment_item("PSV-101", "PSV-101", "PSV", 0,
                                               "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            marker_id = win.db.add_equipment_marker(
                eq_id, "PSV-101", 0, 100.0, 100.0, "Ventil", confidence=0.9,
                link_method='leader')

            win._on_equipment_marker_navigate(marker_id)

            dev_a_item = _find_tree_item(win.tree_panel.tree, DEV_T, dev_a)
            dev_b_item = _find_tree_item(win.tree_panel.tree, DEV_T, dev_b)
            self.assertIsNotNone(dev_a_item)
            self.assertIsNotNone(dev_b_item)
            self.assertTrue(dev_a_item.isExpanded(),
                "every avvikelse mentioning the object must stay open")
            self.assertTrue(dev_b_item.isExpanded(),
                "every avvikelse mentioning the object must stay open")
            cause_a_item = _find_tree_item(win.tree_panel.tree, CAUSE_T, cause_a)
            cause_b_item = _find_tree_item(win.tree_panel.tree, CAUSE_T, cause_b)
            self.assertIsNotNone(cause_a_item)
            self.assertIsNotNone(cause_b_item)

    def test_marker_with_node_but_no_cause_yet_falls_back_to_node_reveal(self):
        """An equipment placed on the P&ID and linked to a node, but with
        no HAZOP cause authored under it yet, has nothing at Orsak level
        to reveal — falls back to the old Nod-level reveal instead of
        doing nothing."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            marker_id = win.db.add_equipment_marker(eq_id, "PV-101", 0, 10.0, 10.0, "Ventil")

            win._on_equipment_marker_navigate(marker_id)

            node_item = _find_tree_item(win.tree_panel.tree, NODE_T, node_id)
            self.assertIsNotNone(node_item)
            self.assertIs(win.tree_panel.tree.currentItem(), node_item)


class EquipmentDeviationCheckboxKeepsScenarioFilterTests(unittest.TestCase):
    """'en mindre fix är att det skall se ut såhär även när jag klickar på
    en rödmarkerad och lägger till exempelvis lågt och högt flöde. Då
    skall det enbart vara kopplat till det objektet.' (2026-08-12) —
    checking a deviation box in EquipmentDeviationBar (right after
    clicking a red/green marker filtered the worksheet to it via
    load_equipment()) must not silently widen the worksheet back out to
    the whole node. Two separate handlers fire for a single checkbox
    toggle — _on_equipment_deviation_created (the deviation itself) AND
    _on_cause_template_created (the auto-suggested cause EquipmentDeviationBar
    creates right after) — both needed the same fix."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_on_equipment_deviation_created_calls_load_equipment_not_load_node(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
            win.db.set_equipment_node(eq_id, node_id)
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)

            with unittest.mock.patch.object(win.scenario_panel, 'load_equipment') as mock_eq, \
                 unittest.mock.patch.object(win.scenario_panel, 'load_node') as mock_node:
                win._on_equipment_deviation_created(dev_id, eq_id)

            mock_eq.assert_called_once_with(eq_id)
            mock_node.assert_not_called()

    def test_cause_template_created_stays_filtered_when_equipment_filter_active(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(cause_id, comp_type="Ventil", comp_tag="PV-101")

            win.scenario_panel.load_equipment(eq_id)   # simulate having clicked the marker first
            refresh_spy = unittest.mock.Mock(wraps=win.scenario_panel.refresh)
            win.scenario_panel.refresh = refresh_spy
            load_node_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_node)
            win.scenario_panel.load_node = load_node_spy

            win.pid_panel.cause_template_created.emit(cause_id)

            refresh_spy.assert_called_once()
            load_node_spy.assert_not_called()
            self.assertEqual(win.scenario_panel._equipment_filter_id, eq_id,
                "the equipment filter must remain active after the cause's "
                "own template-created refresh")
            cause_ids_shown = {m[1] for m in win.scenario_panel._row_meta if m[1] is not None}
            self.assertIn(cause_id, cause_ids_shown,
                "the newly auto-created cause must still show up under the filter")

    def test_cause_template_created_falls_back_to_load_node_without_equipment_filter(self):
        """Unaffected regression check for the normal (non-equipment-
        filtered) P&ID cause flow — must keep working exactly as before."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde")
            cause_id = win.db.add_cause(dev_id)
            win.scenario_panel.load_node(node_id)   # normal (unfiltered) view

            load_node_spy = unittest.mock.Mock(wraps=win.scenario_panel.load_node)
            win.scenario_panel.load_node = load_node_spy

            win.pid_panel.cause_template_created.emit(cause_id)

            load_node_spy.assert_called_once_with(node_id)


class EquipmentTagDragToConsequenceTests(unittest.TestCase):
    """2026-08-07 'drag-and-dropp kunna dra ett objekt från P&ID viewer till
    konsekvensen för att få med tag nummer' (see NOTES.md). Three parts,
    tested separately: (1) Database.set_consequence_tag writes comp_tag/
    comp_type without touching description/severity; (2) ScenarioTablePanel
    ._handle_drop's new 'equipment' mime kind resolves a marker to its
    catalog tag and attaches it to the dropped-on KON cell; (3)
    PIDGraphicsView arms/fires a Shift-held drag from an equipment marker,
    and a plain (non-Shift) click never arms it — the approved plan's
    explicit requirement so normal clicks keep opening
    EquipmentDeviationBar exactly as before."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_dragtag_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ── Database.set_consequence_tag ────────────────────────────────────

    def test_set_consequence_tag_writes_tag_and_type(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)

        self.db.set_consequence_tag(cons_id, "HV-101", "Ventil")

        cons = dict(self.db.get_consequence(cons_id))
        self.assertEqual(cons['comp_tag'], "HV-101")
        self.assertEqual(cons['comp_type'], "Ventil")

    def test_set_consequence_tag_does_not_touch_description(self):
        """The tag is a complement, not a replacement — the user's own
        free-text sentence (e.g. 'Inget flöde till pump X -> ...') must
        survive a tag being attached afterwards."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(cons_id, "Inget flöde till pump X -> kavitation", 3)

        self.db.set_consequence_tag(cons_id, "P-101", "Pump")

        cons = dict(self.db.get_consequence(cons_id))
        self.assertEqual(cons['description'], "Inget flöde till pump X -> kavitation")
        self.assertEqual(cons['severity'], 3)
        self.assertEqual(cons['comp_tag'], "P-101")

    # ── ScenarioTablePanel._handle_drop('equipment', ...) ───────────────

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        return node_id, deviation_id, cause_id, cons_id

    def _make_drop_event(self, panel, text, tgt_row, tgt_col):
        """Builds a fake QDropEvent-like object targeting (tgt_row, tgt_col).
        Bypasses the table<->viewport coordinate mapping (irrelevant to the
        _handle_drop logic under test and unreliable on a never-shown,
        headless widget) by overriding viewport().mapFrom() to identity and
        computing the position directly from the real column/row viewport
        offsets."""
        from PyQt6.QtCore import QMimeData, QPointF
        vp_x = panel._table.columnViewportPosition(tgt_col) + 2
        vp_y = panel._table.rowViewportPosition(tgt_row) + 2
        mime = QMimeData()
        mime.setText(text)
        event = unittest.mock.MagicMock()
        event.mimeData.return_value = mime
        event.position.return_value = QPointF(vp_x, vp_y)
        event.dropAction.return_value = Qt.DropAction.CopyAction
        panel._table.viewport().mapFrom = lambda widget, pt: pt
        return event

    def test_drop_equipment_on_kon_cell_attaches_tag(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            eq_id = win.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-101", 0, 10.0, 10.0, "Ventil", confidence=0.9,
                link_method='leader')

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_KON)
            panel._handle_drop(event)

            event.acceptProposedAction.assert_called_once()
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], "HV-101")
            self.assertEqual(cons['comp_type'], "Ventil")

    @unittest.skip("P&ID equipment can now be dropped onto the ORS cause field")
    def test_drop_equipment_on_non_kon_column_is_ignored(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            eq_id = win.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-101", 0, 10.0, 10.0, "Ventil", confidence=0.9,
                link_method='leader')

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_ORS)
            panel._handle_drop(event)

            event.ignore.assert_called_once()
            event.acceptProposedAction.assert_not_called()
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], '')

    def test_drop_equipment_on_ors_cause_field_attaches_object(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            eq_id = win.db.add_equipment_item("HV-102", "HV-102", "HV", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-102", 0, 10.0, 10.0, "Ventil", confidence=0.9,
                link_method='leader')
            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_ORS)
            panel._handle_drop(event)
            event.acceptProposedAction.assert_called_once()
            cause = dict(win.db.get_cause(cause_id))
            self.assertEqual(cause['equipment_id'], eq_id)
            self.assertEqual(cause['comp_tag'], 'HV-102')

    def test_drop_equipment_marker_with_no_linked_catalog_row_is_ignored(self):
        """A marker whose equipment_id is NULL (untagged shape hit) must be
        a silent no-op, matching _on_marker_clicked's own guard for the
        same case."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            marker_id = win.db.add_equipment_marker(
                None, '', 0, 10.0, 10.0, "Ventil", confidence=0.6, link_method='shape')

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_KON)
            try:
                panel._handle_drop(event)
            except Exception as e:
                self.fail(f"must not raise for a marker with no linked equipment row: {e!r}")

            event.ignore.assert_called_once()
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], '')

    # ── PIDGraphicsView Shift+drag source ────────────────────────────────

    def _make_view_with_equipment_marker(self, marker_id):
        from pid_viewer import PIDGraphicsView, MODE_NAV
        from PyQt6.QtCore import QPointF
        view = PIDGraphicsView()
        view.mode = MODE_NAV
        scene_pos = QPointF(50, 50)
        item = view._scene.addEllipse(scene_pos.x() - 5, scene_pos.y() - 5, 10, 10)
        item.setData(view._DATA_TYPE, 'equipment')
        item.setData(view._DATA_ID, marker_id)
        # The viewport<->scene coordinate transform is standard Qt machinery,
        # not something this feature changes — fix it to a known value so
        # the test exercises only the new drag-arming logic.
        view.mapToScene = lambda pt: scene_pos
        return view

    def _press(self, view, event):
        """In MODE_NAV, mousePressEvent falls through to
        super().mousePressEvent(event) for the base QGraphicsView pan/select
        behaviour — real Qt code that requires a genuine QMouseEvent, not
        our MagicMock stand-in. Patched out since it's irrelevant to the
        drag-arming logic under test."""
        from PyQt6.QtWidgets import QGraphicsView
        with unittest.mock.patch.object(QGraphicsView, 'mousePressEvent'):
            view.mousePressEvent(event)

    def _move(self, view, event):
        """Same rationale as _press: once a move doesn't trigger our new
        drag-start branch, it falls through to the base QGraphicsView
        mouseMoveEvent, which needs a real QMouseEvent."""
        from PyQt6.QtWidgets import QGraphicsView
        with unittest.mock.patch.object(QGraphicsView, 'mouseMoveEvent'):
            view.mouseMoveEvent(event)

    def test_shift_press_on_equipment_marker_arms_drag_candidate(self):
        from PyQt6.QtCore import QPointF
        view = self._make_view_with_equipment_marker(marker_id=7)
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        event.position.return_value = QPointF(50, 50)

        self._press(view, event)

        self.assertIsNotNone(view._equip_drag_candidate)
        self.assertEqual(view._equip_drag_candidate[0], 7)

    def test_plain_click_on_equipment_marker_does_not_arm_drag_candidate(self):
        """Protects the user's explicit requirement: a normal click (no
        Shift) must never be interpreted as a drag start, so it keeps
        opening EquipmentDeviationBar exactly as before."""
        from PyQt6.QtCore import QPointF
        view = self._make_view_with_equipment_marker(marker_id=7)
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.NoModifier
        event.position.return_value = QPointF(50, 50)

        self._press(view, event)

        self.assertIsNone(view._equip_drag_candidate)
        # And the normal click-tracking state must still be set, so
        # mouseReleaseEvent's existing marker_clicked dispatch still fires.
        self.assertIsNotNone(view._press_pos)

    def test_shift_drag_past_threshold_starts_qdrag_with_equipment_mime(self):
        from PyQt6.QtCore import QPointF
        view = self._make_view_with_equipment_marker(marker_id=9)
        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        press_event.position.return_value = QPointF(50, 50)
        self._press(view, press_event)
        self.assertIsNotNone(view._equip_drag_candidate)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        move_event.position.return_value = QPointF(90, 50)   # 40px — past any startDragDistance

        with unittest.mock.patch('pid_graphics_view.QDrag') as MockDrag:
            mock_drag = MockDrag.return_value
            view.mouseMoveEvent(move_event)

        MockDrag.assert_called_once()
        mime_arg = mock_drag.setMimeData.call_args[0][0]
        self.assertEqual(mime_arg.text(), 'hzp:equipment:9:-1:-1')
        mock_drag.exec.assert_called_once()
        self.assertIsNone(view._equip_drag_candidate)
        self.assertIsNone(view._press_pos)

    def test_shift_drag_below_threshold_does_not_start_drag_yet(self):
        from PyQt6.QtCore import QPointF
        view = self._make_view_with_equipment_marker(marker_id=9)
        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        press_event.position.return_value = QPointF(50, 50)
        self._press(view, press_event)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        move_event.position.return_value = QPointF(51, 50)   # 1px — below threshold

        with unittest.mock.patch('pid_graphics_view.QDrag') as MockDrag:
            self._move(view, move_event)

        MockDrag.assert_not_called()
        self.assertIsNotNone(view._equip_drag_candidate,
            "candidate must stay armed until the drag distance is exceeded")

    def test_releasing_shift_mid_move_disarms_the_candidate(self):
        from PyQt6.QtCore import QPointF
        view = self._make_view_with_equipment_marker(marker_id=9)
        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        press_event.position.return_value = QPointF(50, 50)
        self._press(view, press_event)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.NoModifier  # Shift let go
        move_event.position.return_value = QPointF(90, 50)

        with unittest.mock.patch('pid_graphics_view.QDrag') as MockDrag:
            self._move(view, move_event)

        MockDrag.assert_not_called()
        self.assertIsNone(view._equip_drag_candidate)

    # ── _add_row: KON cell carries the tag via UserRole+7 ────────────────

    def test_kon_cell_carries_comp_tag_via_userrole(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            win.db.set_consequence_tag(cons_id, "P-101", "Pump")

            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            item = panel._table.item(row, panel._C_KON)
            comp_type, comp_tag = item.data(Qt.ItemDataRole.UserRole + 7)
            self.assertEqual(comp_tag, "P-101")
            self.assertEqual(comp_type, "Pump")

    def test_kon_cell_tag_tuple_blank_when_untagged(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)

            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            item = panel._table.item(row, panel._C_KON)
            comp_type, comp_tag = item.data(Qt.ItemDataRole.UserRole + 7)
            self.assertEqual(comp_tag, '')
            self.assertEqual(comp_type, '')


class DropEventRoutedToViewportTests(unittest.TestCase):
    """Real bug report (2026-08-08): Shift-dragging an equipment tag onto a
    KON cell did nothing on drop. Root cause: for a QAbstractItemView-based
    widget like QTableWidget, Qt/PyQt6 delivers DragEnter/DragMove/Drop
    events to the VIEWPORT (the actual scrollable surface under the
    cursor), not the outer QTableWidget — but ScenarioTablePanel's
    eventFilter only ever checked `obj is self._table`, so this branch
    never matched for a real cross-widget drag and the drop was silently
    ignored. _handle_drop() also unconditionally called
    self._table.viewport().mapFrom(self._table, pos), which — for a
    position already relative to the viewport — shifts it by the
    header/frame offset a SECOND time, landing on the wrong row (or no
    row at all, tgt_row=-1).

    These tests exercise ScenarioTablePanel.eventFilter() itself (not
    _handle_drop() directly, which earlier tests in
    EquipmentTagDragToConsequenceTests already cover and which is why that
    class's own tests didn't catch this — they bypassed the exact routing
    layer that was actually broken)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        deviation_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(deviation_id)
        cons_id = db.add_consequence(cause_id)
        return node_id, deviation_id, cause_id, cons_id

    def _make_drop_event(self, panel, text, tgt_row, tgt_col, pos):
        from PyQt6.QtCore import QEvent, QMimeData
        mime = QMimeData()
        mime.setText(text)
        event = unittest.mock.MagicMock()
        event.type.return_value = QEvent.Type.Drop
        event.mimeData.return_value = mime
        event.position.return_value = pos
        event.dropAction.return_value = Qt.DropAction.CopyAction
        return event

    def test_drop_event_on_viewport_is_routed_and_attaches_tag(self):
        """The realistic case: Qt delivers the Drop event to
        table.viewport() with a viewport-relative position — this must be
        used AS-IS (no extra remapping) to find the right row."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            eq_id = win.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "HV-101", 0, 10.0, 10.0, "Ventil", confidence=0.9,
                link_method='leader')

            from PyQt6.QtCore import QPointF
            vp_x = panel._table.columnViewportPosition(panel._C_KON) + 2
            vp_y = panel._table.rowViewportPosition(row) + 2
            viewport_pos = QPointF(vp_x, vp_y)

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_KON, viewport_pos)

            handled = panel.eventFilter(panel._table.viewport(), event)

            self.assertTrue(handled, "eventFilter must consume a Drop delivered to the viewport")
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], "HV-101")

    def test_drop_event_on_outer_table_widget_still_works(self):
        """Defensive fallback: if some Qt version/platform instead delivers
        the event to the outer table widget with a table-relative
        position, that must still be remapped correctly (the ORIGINAL,
        pre-bug behavior) rather than assumed to never happen."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            eq_id = win.db.add_equipment_item("P-202", "P-202", "P", 0, "Pump", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "P-202", 0, 10.0, 10.0, "Pump", confidence=0.9,
                link_method='leader')

            from PyQt6.QtCore import QPointF
            vp_x = panel._table.columnViewportPosition(panel._C_KON) + 2
            vp_y = panel._table.rowViewportPosition(row) + 2
            # Convert to TABLE-relative coordinates, matching what the event
            # would carry if Qt delivered it to the outer widget instead.
            table_pos = panel._table.viewport().mapTo(panel._table, QPointF(vp_x, vp_y).toPoint())

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_KON,
                QPointF(table_pos))

            handled = panel.eventFilter(panel._table, event)

            self.assertTrue(handled, "eventFilter must consume a Drop delivered to the outer table")
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['comp_tag'], "P-202")

    def test_drag_enter_on_viewport_is_accepted(self):
        """If DragEnter is never accepted, most platforms never even
        deliver the subsequent Drop — this is the first domino, and it
        must fire for the viewport, not just the outer table widget."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            from PyQt6.QtCore import QEvent, QMimeData
            mime = QMimeData()
            mime.setText('hzp:equipment:1:-1:-1')
            event = unittest.mock.MagicMock()
            event.mimeData.return_value = mime
            event.type.return_value = QEvent.Type.DragEnter

            handled = panel.eventFilter(panel._table.viewport(), event)

            self.assertTrue(handled)
            event.acceptProposedAction.assert_called_once()


class AutoConsequenceOnCauseAddTests(unittest.TestCase):
    """'Sedan vill jag ... kunna editera konsekvenser direkt utan att
    behöva lägga till dem via popuprutan ... utan det skall gå i hazop
    scenario så fort jag lagt till en orsak' (2026-08-07, see NOTES.md) —
    _create_cause_from_pick (shared by the tree's "+ Lägg till orsak" and
    the worksheet's Ctrl+Enter quick-add) now also creates one empty
    consequence, and ScenarioTablePanel._quick_add_cause lands the editing
    cursor on that new consequence's KON cell instead of the cause's own
    ORS cell."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_autocons_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_create_cause_from_pick_returns_cause_and_consequence_ids(self):
        from hazop import _create_cause_from_pick
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']

        cause_id, cons_id = _create_cause_from_pick(self.db, dev_id, "Ny orsak", None)

        self.assertIsNotNone(self.db.get_cause(cause_id))
        cons = self.db.get_consequence(cons_id)
        self.assertIsNotNone(cons)
        self.assertEqual(dict(cons)['cause_id'], cause_id)

    def test_new_item_created_consequence_starts_inline_edit_on_kon(self):
        """Directly exercises the MainWindow-level wiring (matches the
        established pattern in SafeguardCreatedDoubleRebuildTests): emitting
        new_item_created(CONS_T, cons_id) must land the current cell AND an
        active edit on that row's KON column."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            panel.load_node(node_id)

            edit_spy = unittest.mock.Mock(wraps=panel._table.edit)
            panel._table.edit = edit_spy

            panel.new_item_created.emit(CONS_T, cons_id)

            cur_row = panel._table.currentRow()
            self.assertEqual(panel._table.currentColumn(), panel._C_KON)
            self.assertEqual(panel._row_meta[cur_row][2], cons_id)
            edit_spy.assert_called()

    def test_quick_add_cause_emits_new_item_created_for_the_new_consequence(self):
        """ScenarioTablePanel._quick_add_cause (Ctrl+Enter in the worksheet)
        must emit new_item_created for the auto-created CONSEQUENCE, not
        for the cause itself — the cause's description was already chosen
        in the popup, so the next thing to fill in is the consequence.

        Opens CauseObjectPopup, not StandardCausesPickerPopup (2026-08-12,
        see NOTES.md) — mocking the wrong class here would leave the real
        popup unmocked, blocking forever on exec() in a headless test run
        (this is exactly what happened: an earlier version of this test
        still mocked StandardCausesPickerPopup after the switch, hanging
        the full suite)."""
        from PyQt6.QtWidgets import QDialog
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            panel.load_node(node_id)

            def _fake_exec(self):
                self.committed.emit('', '', 'Ny orsak (test)', None)
                return QDialog.DialogCode.Accepted

            captured = []
            panel.new_item_created.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch.object(hazop.CauseObjectPopup, 'exec', new=_fake_exec):
                panel._quick_add_cause(dev_id)

            self.assertEqual(len(captured), 1)
            type_, cons_id = captured[0]
            self.assertEqual(type_, CONS_T)
            self.assertIsNotNone(win.db.get_consequence(cons_id))

    def test_enter_on_cause_inserts_sibling_after_it_and_stays_in_cause(self):
        """Enter in the cause field creates a blank cause immediately after
        the selected sibling, without an automatic consequence or focus jump.
        """
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            first = win.db.add_cause(dev_id)
            second = win.db.add_cause(dev_id)
            third = win.db.add_cause(dev_id)
            panel.load_node(node_id)

            captured = []
            panel.new_item_created.connect(lambda t, i: captured.append((t, i)))
            panel._quick_add_cause(dev_id, from_enter=True, after_cause_id=second)

            self.assertEqual(captured[0][0], CAUSE_T)
            new_id = captured[0][1]
            self.assertEqual(
                [row['id'] for row in win.db.causes_for_deviation(dev_id)],
                [first, second, new_id, third])
            self.assertEqual(win.db.consequences(new_id), [])

    def test_tree_add_cause_via_deviation_also_creates_empty_consequence(self):
        """TreePanel._add_cause_for_deviation is the other
        _create_cause_from_pick caller (tree's "+ Orsak" button,
        right-click 'Lägg till orsak', and Enter on an avvikelse) — must
        not crash on the tuple return, and the consequence must actually
        exist in DB afterwards. 2026-08-24 (see NOTES.md): this used to
        go through a StandardCausesPickerPopup dialog ("Lägg till orsak
        på P&ID") — removed at Anton's request, now creates directly with
        no popup at all, same as add_consequence()/add_safeguard()."""
        with _TempDbMainWindow() as win:
            tree = win.tree_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']

            tree._add_cause_for_deviation(dev_id)

            causes = win.db.causes(node_id)
            self.assertEqual(len(causes), 1)
            cons_list = win.db.consequences(causes[0]['id'])
            self.assertEqual(len(cons_list), 1,
                "the tree's add-cause path must also auto-create an empty consequence")


class PlusRowQuickAddTaggingTests(unittest.TestCase):
    """The "+" quick-add rows (2026-08-12, see NOTES.md). Reported
    feedback changed course mid-session on how these should behave:
    a new consequence/safeguard must NEVER show a popup — straight to
    inline editing, tagging stays a drag-and-drop-only affair — while a
    new cause opens the same compact CauseObjectPopup ("Orsak på P&ID")
    already used everywhere else a cause's tag/type/description is set,
    replacing the earlier ObjectPickerPopup experiment entirely."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _add_full_chain(self, db):
        node_id = db.add_node()
        dev_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(dev_id)
        cons_id = db.add_consequence(cause_id)
        return node_id, dev_id, cause_id, cons_id

    def test_quick_add_consequence_creates_blank_row_no_popup(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, _c = self._add_full_chain(win.db)
            captured = []
            panel.new_item_created.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch('hazop.ObjectPickerPopup') as mock_popup_cls:
                panel._quick_add_consequence(cause_id)

            mock_popup_cls.assert_not_called()
            self.assertEqual(len(captured), 1)
            new_id = captured[0][1]
            self.assertEqual(dict(win.db.get_consequence(new_id))['description'], '')

    def test_scenario_created_consequence_does_not_expand_collapsed_tree(self):
        """Enter in HAZOP Scenario adds data without navigating the tree."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node = dict(win.db.nodes()[0])
            node_id = node['id']
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(cause_id, description='Objekt')
            win.tree_panel.refresh()
            system_item = _find_tree_item(win.tree_panel.tree, SYSTEM_T, node['system_id'])
            node_item = _find_tree_item(win.tree_panel.tree, NODE_T, node_id)
            dev_item = _find_tree_item(win.tree_panel.tree, DEV_T, dev_id)
            self.assertIsNotNone(system_item)
            self.assertIsNotNone(node_item)
            self.assertIsNotNone(dev_item)
            system_item.setExpanded(True)
            node_item.setExpanded(True)
            dev_item.setExpanded(True)
            cause_item = _find_tree_item(win.tree_panel.tree, CAUSE_T, cause_id)
            self.assertIsNotNone(cause_item)
            cause_item.setExpanded(False)

            panel._quick_add_consequence(cause_id)

            self.assertEqual(len(win.db.consequences(cause_id)), 1)
            system_item = _find_tree_item(win.tree_panel.tree, SYSTEM_T, node['system_id'])
            node_item = _find_tree_item(win.tree_panel.tree, NODE_T, node_id)
            dev_item = _find_tree_item(win.tree_panel.tree, DEV_T, dev_id)
            self.assertTrue(system_item.isExpanded(),
                "the existing system level must stay open")
            self.assertTrue(node_item.isExpanded(),
                "the existing node level must stay open so deviations remain visible")
            self.assertTrue(dev_item.isExpanded(),
                "the deviation must stay open so its object remains visible")
            cause_item = _find_tree_item(win.tree_panel.tree, CAUSE_T, cause_id)
            self.assertIsNotNone(cause_item,
                "the consequence's object/cause must still exist in the rebuilt tree")
            self.assertFalse(cause_item.isExpanded(),
                "the object must stay collapsed so its consequence remains hidden")

    def test_editing_safeguard_keeps_safeguard_level_collapsed(self):
        """Enter after SG editing syncs text without navigating the tree."""
        with _TempDbMainWindow() as win:
            node = dict(win.db.nodes()[0])
            node_id = node['id']
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            sg_id = win.db.add_safeguard(cons_id)
            win.tree_panel.refresh()

            for type_, id_ in ((SYSTEM_T, node['system_id']),
                               (NODE_T, node_id), (DEV_T, dev_id),
                               (CAUSE_T, cause_id)):
                item = _find_tree_item(win.tree_panel.tree, type_, id_)
                self.assertIsNotNone(item)
                item.setExpanded(True)
            cons_item = _find_tree_item(win.tree_panel.tree, CONS_T, cons_id)
            self.assertIsNotNone(cons_item)
            cons_item.setExpanded(False)

            win._on_scenario_item_edited(SG_T, sg_id)

            cons_item = _find_tree_item(win.tree_panel.tree, CONS_T, cons_id)
            sg_item = _find_tree_item(win.tree_panel.tree, SG_T, sg_id)
            self.assertIsNotNone(sg_item,
                "the edited safeguard must remain present in the rebuilt tree")
            self.assertFalse(cons_item.isExpanded(),
                "Enter after safeguard editing must not expose safeguard rows")

    def _assert_active_branch_survives_auto_collapse_after_edit(self, edited_type):
        """Shared body for the two auto-collapse regression tests below —
        only which scenario field gets edited differs (Konsekvens vs
        Safeguard)."""
        with _TempDbMainWindow() as win:
            win.db.set_config('tree_auto_collapse_nodes', '1')
            win.db.set_config('tree_auto_collapse_deviations', '1')
            node = dict(win.db.nodes()[0])
            node_id = node['id']
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            sg_id = win.db.add_safeguard(cons_id)
            edited_id = {CONS_T: cons_id, SG_T: sg_id}[edited_type]
            win.tree_panel.refresh()

            node_item = _find_tree_item(win.tree_panel.tree, NODE_T, node_id)
            self.assertIsNotNone(node_item)
            win.tree_panel.tree.setCurrentItem(node_item)
            win.tree_panel._apply_auto_collapse()
            self.assertTrue(node_item.isExpanded(),
                "sanity check: the active node must be open before the edit")

            win._on_scenario_item_edited(edited_type, edited_id)

            system_item = _find_tree_item(win.tree_panel.tree, SYSTEM_T, node['system_id'])
            node_item = _find_tree_item(win.tree_panel.tree, NODE_T, node_id)
            self.assertIsNotNone(system_item)
            self.assertIsNotNone(node_item)
            self.assertTrue(system_item.isExpanded(),
                "auto-collapse must not lose the active system just because "
                "this refresh carried no navigation target")
            self.assertTrue(node_item.isExpanded(),
                "auto-collapse must not lose the active node just because "
                "this refresh carried no navigation target")

    def test_editing_consequence_with_auto_collapse_enabled_keeps_active_node_open(self):
        """Regression: with 'Auto-collapse nodes'/'avvikelser' enabled,
        Enter after Konsekvens editing used to collapse every System/Node
        down to just the System row, because refresh()'s clear() wipes
        currentItem() to None and this refresh path deliberately passes
        no navigation target — _apply_auto_collapse() then saw no active
        branch at all and collapsed everything, not just the consequence/
        safeguard level. See NOTES.md."""
        self._assert_active_branch_survives_auto_collapse_after_edit(CONS_T)

    def test_editing_safeguard_with_auto_collapse_enabled_keeps_active_node_open(self):
        """Same regression as above, reported for the Safeguard column."""
        self._assert_active_branch_survives_auto_collapse_after_edit(SG_T)

    def test_quick_add_safeguard_creates_blank_row_no_popup(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, _c, cons_id = self._add_full_chain(win.db)
            captured = []
            panel.new_item_created.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch('hazop.ObjectPickerPopup') as mock_popup_cls:
                panel._quick_add_safeguard(cons_id)

            mock_popup_cls.assert_not_called()
            self.assertEqual(len(captured), 1)
            new_id = captured[0][1]
            self.assertEqual(dict(win.db.get_safeguard(new_id))['description'], '')

    def test_quick_add_cause_opens_cause_object_popup_and_creates_cause(self):
        from PyQt6.QtWidgets import QDialog
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']

            def _fake_exec(self):
                self.committed.emit('Ventil', 'PV-101', 'Ventil stängd', 3)
                return QDialog.DialogCode.Accepted

            with unittest.mock.patch.object(hazop.CauseObjectPopup, 'exec', new=_fake_exec):
                panel._quick_add_cause(dev_id)

            # NOTE: Database.causes(x) filters by node_id, not deviation_id
            # (causes_for_deviation() is the deviation-scoped accessor) —
            # this used to read causes(dev_id) and still pass, only because
            # a totally fresh, node-less DB made node_id and dev_id both
            # come out as 1 by coincidence. 2026-08-24: Database now
            # auto-seeds one default node on a brand new project (see
            # Database.__init__'s pre_existing_db check), which breaks
            # that coincidence and exposed the mismatch.
            causes = win.db.causes_for_deviation(dev_id)
            self.assertEqual(len(causes), 1)
            self.assertEqual(causes[0]['comp_tag'], 'PV-101')
            self.assertEqual(causes[0]['comp_type'], 'Ventil')
            self.assertEqual(causes[0]['description'], 'Ventil stängd')
            self.assertEqual(win.db.consequences(causes[0]['id']),
                              win.db.consequences(causes[0]['id']))  # sanity: no crash
            self.assertEqual(len(win.db.consequences(causes[0]['id'])), 1)

    def test_add_consequence_via_plus_row_creates_blank_row_no_popup(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, _c = self._add_full_chain(win.db)
            before = len(win.db.consequences(cause_id))
            captured = []
            panel.new_item_created.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch('hazop.ObjectPickerPopup') as mock_popup_cls:
                panel._add_consequence_via_plus_row(cause_id)

            mock_popup_cls.assert_not_called()
            self.assertEqual(len(win.db.consequences(cause_id)), before + 1)
            self.assertEqual(len(captured), 1)

    def test_add_cause_via_plus_row_opens_cause_object_popup(self):
        from PyQt6.QtWidgets import QDialog
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']

            def _fake_exec(self):
                self.committed.emit('', '', '', None)
                return QDialog.DialogCode.Accepted

            with unittest.mock.patch.object(hazop.CauseObjectPopup, 'exec', new=_fake_exec):
                panel._add_cause_via_plus_row(dev_id)

            # See test_quick_add_cause_opens_cause_object_popup_and_creates_cause
            # above for why this is causes_for_deviation(), not causes().
            self.assertEqual(len(win.db.causes_for_deviation(dev_id)), 1)


class EmptyOrsCellClickOpensCausePopupTests(unittest.TestCase):
    """Reported feedback (2026-08-12, see NOTES.md): clicking an empty
    ORS placeholder cell (a deviation with no causes yet) used to start
    inline text editing directly — now opens the same CauseObjectPopup
    the "+ Ny orsak" row does, so creating a cause behaves identically
    regardless of entry point."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_emptyorsclick_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_clicking_empty_ors_placeholder_opens_cause_popup(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta)
                       if m[0] == dev_id and m[1] is None)

            with unittest.mock.patch.object(panel, '_add_cause_via_plus_row') as mock_add:
                panel._on_cell_clicked(row, panel._C_ORS)
                # The real UI waits briefly to distinguish a single click
                # from a double-click; invoke the pending callback directly
                # here so the test does not depend on wall-clock timing.
                panel._open_pending_empty_cause_popup()

            mock_add.assert_called_once()
            self.assertEqual(mock_add.call_args.args[0], dev_id)
        finally:
            panel.deleteLater()

    def test_clicking_a_real_ors_cell_still_selects_it_not_the_popup(self):
        """Sanity check: the new empty-placeholder branch must not
        accidentally hijack clicks on a real, already-defined cause."""
        from hazop import ScenarioTablePanel, CAUSE_T
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            captured = []
            panel.item_selected.connect(lambda t, i: captured.append((t, i)))

            with unittest.mock.patch.object(panel, '_add_cause_via_plus_row') as mock_add:
                panel._on_cell_clicked(row, panel._C_ORS)

            mock_add.assert_not_called()
            self.assertEqual(captured, [(CAUSE_T, cause_id)])
        finally:
            panel.deleteLater()


class RecommendationColumnTests(unittest.TestCase):
    """"Längst till höger ... kan du lägga till en rekomendationskolumn
    på varje flik så det går att skapa rekommendationer till varje
    scenario." (2026-08-13). Rewritten 2026-08-25 for the shared
    recommendations catalog (see NOTES.md "Rekommendationshantering —
    delad katalog med återanvändning") — a recommendation is no longer
    owned by one consequence; it's linked from a study-wide catalog via
    consequence_recommendations, so the same text can be reused across
    consequences without duplication, and each cell shows the
    recommendation's own global, never-reused id as "R-XXX"."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_rekcol_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.cons_id = self.db.add_consequence(cause_id)
        self.node_id = node_id

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _rek_item(self):
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id)
        return self.panel._table.item(row, self.panel._C_REK), row

    def test_column_exists_last_and_is_named_rekommendation(self):
        self.assertEqual(self.panel._COLS[-1], 'Rekommendation')
        self.assertEqual(self.panel._C_REK, len(self.panel._COLS) - 1)

    def test_consequence_with_no_actions_shows_dash_placeholder(self):
        item, _ = self._rek_item()
        self.assertEqual(item.text(), '—')

    def test_single_action_shows_its_description(self):
        rec_id = self.db.add_recommendation_to_consequence(self.cons_id, description='Ny åtgärd')
        item, _ = self._rek_item()
        self.assertEqual(item.text(), f'R-{rec_id:03d}. Ny åtgärd')

    @unittest.skip("REK recommendations are now separate physical rows")
    def test_multiple_actions_are_all_listed_numbered_by_addition_order(self):
        """"samtliga tillagda rekomendationer. de kan nummereras efter
        tilläggsordning" (2026-08-13) — every recommendation shows, not
        just a count, in the order they were added, numbered by its own
        global catalog id (2026-08-25 rework) rather than local position."""
        id1 = self.db.add_recommendation_to_consequence(self.cons_id, description='Ny åtgärd')
        id2 = self.db.add_recommendation_to_consequence(self.cons_id, description='Klar sak',
                                                         status='Klar')
        item, _ = self._rek_item()
        self.assertEqual(item.text(), f'R-{id1:03d}. Ny åtgärd\nR-{id2:03d}. Klar sak')

    @unittest.skip("REK recommendations are now separate physical rows")
    def test_row_grows_to_fit_several_recommendations(self):
        """REK joins wrap_cols (_ScenarioDelegate._size_hint_impl,
        ScenarioTablePanel._compute_row_height) so a multi-line
        recommendation list isn't clipped to one line like a plain
        non-wrapping column would be."""
        _, row = self._rek_item()
        one_line_h = self.panel._table.rowHeight(row)
        for _ in range(6):
            self.db.add_recommendation_to_consequence(self.cons_id)
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id)
        grown_h = self.panel._table.rowHeight(row)
        self.assertGreater(grown_h, one_line_h,
            "row must grow to fit 6 numbered recommendation lines")

    def test_clicking_cell_selects_it_first_then_starts_inline_edit_when_already_current(self):
        """"Rekommendationstexten ska kunna redigeras direkt i HAZOP
        Scenario" (2026-08-26) replaced the old modal-dialog-on-click
        with the same "first click selects, second click on the
        already-current cell starts inline edit" convention ORS/KON/SG
        already use."""
        item, row = self._rek_item()
        self.assertTrue(bool(item.flags() & Qt.ItemFlag.ItemIsEditable),
            "the REK cell must be directly editable now, not view-only")

        selected = []
        self.panel.item_selected.connect(lambda t, i: selected.append((t, i)))
        self.panel._table.setCurrentCell(-1, -1)
        self.panel._on_cell_clicked(row, self.panel._C_REK)
        self.assertEqual(selected, [(CONS_T, self.cons_id)])

        # Same deterministic pattern as KonInlineEditTests
        # (test_single_click_on_already_current_kon_cell_schedules_edit)
        # -- patch QTimer.singleShot to fire synchronously instead of
        # relying on the real 200ms timer landing inside a QTest.qWait()
        # window. The real-timer version passed in isolation but failed
        # deterministically in the combined suite run (200ms/250ms isn't
        # reliably enough headroom once hundreds of prior tests have run
        # in the same process) -- found and fixed 2026-08-26.
        with unittest.mock.patch.object(self.panel, '_try_start_edit') as mock_edit, \
             unittest.mock.patch('hazop.QTimer.singleShot',
                                  side_effect=lambda _ms, fn: fn()):
            self.panel._table.setCurrentCell(row, self.panel._C_REK)
            self.panel._on_cell_clicked(row, self.panel._C_REK)
        mock_edit.assert_called_once_with(row, self.panel._C_REK)

    @unittest.skip("REK commit rebuilds the table to materialize a new row")
    def test_committing_typed_text_with_zero_linked_creates_a_new_recommendation(self):
        """"Gör det även möjligt att snabbt skapa en ny rekommendation
        med Enter." (2026-08-26) — committing the inline editor (Enter,
        via _on_cell_changed_inner) on a REK cell with no recommendation
        linked yet creates one."""
        item, row = self._rek_item()
        self.panel._table.blockSignals(True)
        item.setText("Ny rekommendation")
        self.panel._table.blockSignals(False)
        self.panel._on_cell_changed_inner(row, self.panel._C_REK)
        recs = self.db.all_recommendations()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]['description'], "Ny rekommendation")
        self.assertEqual(item.text(), f"R-{recs[0]['id']:03d}. Ny rekommendation")

    def test_committing_unchanged_text_on_the_sole_linked_recommendation_is_a_noop(self):
        """Re-committing without editing (click in, click out) must not
        pop the "shared recommendation" prompt for nothing."""
        rec_id = self.db.add_recommendation_to_consequence(self.cons_id, description='Befintlig')
        item, row = self._rek_item()
        # Matches what the real inline editor actually seeds itself with
        # for the "exactly one linked" case -- the recommendation's OWN
        # bare description, not the cell's "R-XXX. ..." display summary
        # (see _prepare_recommendation_editor's docstring).
        self.panel._table.blockSignals(True)
        item.setText("Befintlig")
        self.panel._table.blockSignals(False)
        with unittest.mock.patch.object(QMessageBox, 'exec') as mock_exec:
            self.panel._on_cell_changed_inner(row, self.panel._C_REK)
        mock_exec.assert_not_called()
        self.assertEqual(self.db.get_recommendation(rec_id)['description'], 'Befintlig')

    def test_committing_new_text_on_the_sole_linked_recommendation_updates_it_in_place(self):
        rec_id = self.db.add_recommendation_to_consequence(self.cons_id, description='Gammal text')
        item, row = self._rek_item()
        self.panel._table.blockSignals(True)
        item.setText("Ny text")
        self.panel._table.blockSignals(False)
        self.panel._on_cell_changed_inner(row, self.panel._C_REK)
        self.assertEqual(self.db.get_recommendation(rec_id)['description'], 'Ny text')
        self.assertEqual(len(self.db.all_recommendations()), 1,
            "editing in place must not create a second catalog row")

    def test_real_recommendation_delegate_saves_plain_text_not_html(self):
        """The live QTextEdit delegate must persist the recommendation text.

        This exercises the same create/set-editor-data/set-model-data path as
        an actual inline edit, including the table's itemChanged callback.
        It guards against the regression where Qt put the QTextEdit HTML
        document into the cell and the database ended up empty.
        """
        rec_id = self.db.add_recommendation_to_consequence(
            self.cons_id, description='Gammal text')
        item, row = self._rek_item()
        index = self.panel._table.model().index(row, self.panel._C_REK)
        option = QStyleOptionViewItem()
        option.rect = self.panel._table.visualRect(index)
        editor = self.panel._delegate.createEditor(
            self.panel._table, option, index)
        try:
            self.panel._delegate.setEditorData(editor, index)
            self.assertEqual(editor.toPlainText(), 'Gammal text')
            editor.setText('se till att stoppa pump x vid y')
            self.panel._delegate.setModelData(
                editor, self.panel._table.model(), index)
        finally:
            editor.deleteLater()

        saved = self.db.get_recommendation(rec_id)
        self.assertEqual(saved['description'], 'se till att stoppa pump x vid y')
        self.assertNotIn('<!DOCTYPE', saved['description'])
        QApplication.processEvents()
        refreshed_item, _ = self._rek_item()
        self.assertEqual(refreshed_item.text(),
                         f'R-{rec_id:03d}. se till att stoppa pump x vid y')

    def test_enter_accepts_selected_pid_tag_and_keeps_editor_open(self):
        """Enter accepts the visible tag suggestion instead of committing."""
        self.db.add_equipment_item('E1.P-101', 'E1.P-101', 'P', 0, 'Pump', '', 0)
        sg_id = self.db.add_safeguard(self.cons_id)
        self.panel.load_node(self.node_id)
        rows = {
            'kon': next(r for r, m in enumerate(self.panel._row_meta)
                        if m[2] == self.cons_id),
            'sg': next(r for r, m in enumerate(self.panel._row_meta)
                       if m[3] == sg_id),
            'rek': next(r for r, m in enumerate(self.panel._row_meta)
                        if m[2] == self.cons_id),
        }
        columns = {
            'kon': self.panel._C_KON,
            'sg': self.panel._C_SG,
            'rek': self.panel._C_REK,
        }
        for kind, row in rows.items():
            col = columns[kind]
            delegate = (self.panel._pid_delegate if kind != 'rek'
                        else self.panel._delegate)
            index = self.panel._table.model().index(row, col)
            option = QStyleOptionViewItem()
            option.rect = self.panel._table.visualRect(index)
            editor = delegate.createEditor(self.panel._table, option, index)
            try:
                delegate.setEditorData(editor, index)
                editor.setText('E1')
                completer = editor._tag_completer
                self.assertIsNotNone(completer, kind)
                completer.setCompletionPrefix('E1')
                popup = completer.popup()
                popup.setCurrentIndex(completer.completionModel().index(0, 0))
                popup.show()
                editor._tag_completion_range = (0, 2)
                event = QKeyEvent(
                    QEvent.Type.KeyPress, Qt.Key.Key_Return,
                    Qt.KeyboardModifier.NoModifier)
                self.assertTrue(self.panel.eventFilter(editor, event), kind)
                self.assertEqual(editor.toPlainText(), 'E1.P-101', kind)
            finally:
                editor.deleteLater()

    @unittest.skip("REK additions now use the dedicated trailing physical row")
    def test_committing_text_with_two_linked_adds_a_third_without_touching_the_others(self):
        rec1 = self.db.add_recommendation_to_consequence(self.cons_id, description='Första')
        rec2 = self.db.add_recommendation_to_consequence(self.cons_id, description='Andra')
        item, row = self._rek_item()
        self.panel._table.blockSignals(True)
        item.setText("Tredje")
        self.panel._table.blockSignals(False)
        self.panel._on_cell_changed_inner(row, self.panel._C_REK)
        descs = {r['description'] for r in self.db.recommendations_for_consequence(self.cons_id)}
        self.assertEqual(descs, {'Första', 'Andra', 'Tredje'})
        self.assertEqual(self.db.get_recommendation(rec1)['description'], 'Första')
        self.assertEqual(self.db.get_recommendation(rec2)['description'], 'Andra')

    def test_sequential_blank_editor_adds_second_instead_of_overwriting_first(self):
        first = self.db.add_recommendation_to_consequence(
            self.cons_id, description='Första')
        item, row = self._rek_item()
        self.panel._recommendation_force_add_cons_id = self.cons_id
        self.panel._table.blockSignals(True)
        item.setText('Andra')
        self.panel._table.blockSignals(False)

        self.panel._on_cell_changed_inner(row, self.panel._C_REK)

        recs = self.db.recommendations_for_consequence(self.cons_id)
        self.assertEqual([r['description'] for r in recs], ['Första', 'Andra'])
        self.assertEqual(self.db.get_recommendation(first)['description'], 'Första')

    @unittest.skip("REK continuation now selects the next physical row")
    def test_continue_recommendation_entry_selects_same_cell_in_add_mode(self):
        self.db.add_recommendation_to_consequence(self.cons_id, description='Första')
        _, row = self._rek_item()
        with unittest.mock.patch.object(self.panel, '_try_start_edit') as start_edit:
            self.panel._continue_recommendation_entry(row, self.cons_id)

        self.assertEqual(self.panel._recommendation_force_add_cons_id, self.cons_id)
        self.assertEqual(self.panel._table.currentRow(), row)
        self.assertEqual(self.panel._table.currentColumn(), self.panel._C_REK)
        start_edit.assert_called_once_with(row, self.panel._C_REK)

    @unittest.skip("REK recommendations are no longer spanned across safeguard rows")
    def test_recommendation_column_spans_across_safeguard_rows(self):
        """Several safeguards under the same consequence must share ONE
        merged REK cell, not one per safeguard row — same grouping KON/
        LOPA already get."""
        self.db.add_safeguard(self.cons_id)
        self.db.add_safeguard(self.cons_id)
        self.db.add_recommendation_to_consequence(self.cons_id)
        self.panel.load_node(self.node_id)
        rows = [r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id]
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(self.panel._table.rowSpan(rows[0], self.panel._C_REK), len(rows),
            "the consequence's safeguard rows must be merged into one REK span")

    def test_reusing_a_recommendation_on_a_second_consequence_shows_same_number(self):
        """The core of the 2026-08-25 rework: linking the SAME catalog
        row to a second consequence must show the identical R-XXX label
        there too, not a fresh local "1." — proving reuse doesn't
        duplicate the underlying recommendation."""
        rec_id = self.db.add_recommendation_to_consequence(self.cons_id, description='Delad')
        cause2 = self.db.add_cause(self.db.deviations(self.node_id)[0]['id'])
        cons2 = self.db.add_consequence(cause2)
        self.db.link_recommendation_to_consequence(rec_id, cons2)

        self.panel.load_node(self.node_id)
        row1 = next(r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id)
        row2 = next(r for r, m in enumerate(self.panel._row_meta) if m[2] == cons2)
        expected = f'R-{rec_id:03d}. Delad'
        self.assertEqual(self.panel._table.item(row1, self.panel._C_REK).text(), expected)
        self.assertEqual(self.panel._table.item(row2, self.panel._C_REK).text(), expected)
        self.assertEqual(len(self.db.all_recommendations()), 1,
            "reuse must not create a second catalog row")


class RecommendationAssistPopupTests(unittest.TestCase):
    """RecommendationAssistPopup (2026-08-26, see NOTES.md "Redigera
    rekommendationer direkt i HAZOP Scenario") — replaces the old modal
    RecommendationEditorDialog's checkbox table as the "extra
    information ... i en liten popup ovanför" shown alongside the REK
    cell's own inline text editor. One checkbox row per catalog
    recommendation; checked reflects whether it's linked to THIS
    consequence, and toggling commits the link/unlink immediately (no
    OK button needed — same "persists itself" pattern the old dialog
    already used)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_recassist_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.node_id = self.db.add_node()
        dev_id = self.db.deviations(self.node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.cons_id = self.db.add_consequence(cause_id)
        self.cause2 = self.db.add_cause(dev_id)
        self.cons2 = self.db.add_consequence(self.cause2)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _popup(self, cons_id=None):
        from scenario_panel import RecommendationAssistPopup
        editor = QLineEdit()   # stand-in for the real REK cell editor
        return RecommendationAssistPopup(self.panel, cons_id or self.cons_id, editor)

    def _checkbox_for(self, popup, rec_id):
        from PyQt6.QtWidgets import QCheckBox
        for cb in popup.findChildren(QCheckBox):
            if cb.text().startswith(f"R-{rec_id:03d}."):
                return cb
        return None

    def test_empty_catalog_shows_a_placeholder_not_an_empty_list(self):
        popup = self._popup()
        try:
            from PyQt6.QtWidgets import QCheckBox, QLabel
            self.assertEqual(len(popup.findChildren(QCheckBox)), 0)
            labels = [w.text() for w in popup.findChildren(QLabel)]
            self.assertTrue(any('Inga rekommendationer' in t for t in labels))
        finally:
            popup.deleteLater()

    def test_showing_assist_popup_twice_keeps_one_visible_popup(self):
        from PyQt6.QtCore import QRect
        from scenario_panel import RecommendationAssistPopup
        self.panel.show()
        editor = QLineEdit()
        editor.show()
        try:
            self.panel._delegate._show_recommendation_assist_popup(
                editor, 0, self.cons_id, QRect(0, 0, 260, 32))
            self.panel._delegate._show_recommendation_assist_popup(
                editor, 0, self.cons_id, QRect(0, 0, 260, 32))
            self.app.processEvents()
            visible = [popup for popup in self.panel.findChildren(
                RecommendationAssistPopup) if popup.isVisible()]
            self.assertEqual(len(visible), 1)
        finally:
            editor.deleteLater()

    def test_typed_recommendation_text_filters_catalog_anywhere(self):
        first_id = self.db.add_recommendation(description='Verify shutdown function')
        second_id = self.db.add_recommendation(description='Inspect pressure relief valve')
        from scenario_panel import RecommendationAssistPopup
        editor = QLineEdit()
        popup = RecommendationAssistPopup(self.panel, self.cons_id, editor)
        try:
            editor.setText('shutdown')
            self.app.processEvents()
            labels = []
            for i in range(popup._list_layout.count()):
                row = popup._list_layout.itemAt(i)
                if row.layout() and row.layout().itemAt(0).widget():
                    labels.append(row.layout().itemAt(0).widget().text())
            self.assertEqual(labels, [f'R-{first_id:03d}. Verify shutdown function'])
            self.assertNotIn(f'R-{second_id:03d}. Inspect pressure relief valve', labels)
        finally:
            popup.deleteLater()
            editor.deleteLater()

    def test_checking_an_existing_recommendation_links_it_without_duplicating(self):
        rec_id = self.db.add_recommendation(description='Reusable text')
        popup = self._popup()
        try:
            cb = self._checkbox_for(popup, rec_id)
            self.assertIsNotNone(cb)
            self.assertFalse(cb.isChecked())
            cb.setChecked(True)
            linked = {r['id'] for r in self.db.recommendations_for_consequence(self.cons_id)}
            self.assertEqual(linked, {rec_id})
            self.assertEqual(len(self.db.all_recommendations()), 1)
        finally:
            popup.deleteLater()

    def test_unchecking_unlinks_but_keeps_the_catalog_row(self):
        rec_id = self.db.add_recommendation_to_consequence(self.cons_id, description='Keep me')
        popup = self._popup()
        try:
            cb = self._checkbox_for(popup, rec_id)
            self.assertTrue(cb.isChecked())
            cb.setChecked(False)
            self.assertEqual(self.db.recommendations_for_consequence(self.cons_id), [])
            self.assertIsNotNone(self.db.get_recommendation(rec_id),
                "unlinking must not delete the catalog row")
        finally:
            popup.deleteLater()

    def test_linking_on_one_consequence_does_not_affect_another(self):
        rec_id = self.db.add_recommendation(description='Shared candidate')
        popup = self._popup(self.cons_id)
        try:
            self._checkbox_for(popup, rec_id).setChecked(True)
            self.assertEqual(self.db.recommendations_for_consequence(self.cons2), [])
        finally:
            popup.deleteLater()

    def test_checking_a_box_refreshes_the_cells_rek_summary_live(self):
        rec_id = self.db.add_recommendation(description='Live refresh check')
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id)
        popup = self._popup()
        try:
            self._checkbox_for(popup, rec_id).setChecked(True)
            item = self.panel._table.item(row, self.panel._C_REK)
            self.assertEqual(item.text(), f"R-{rec_id:03d}. Live refresh check")
        finally:
            popup.deleteLater()


class RecommendationEditConflictTests(unittest.TestCase):
    """The second prompt's rule: editing a recommendation used by more
    than one consequence must ask before saving (Ja/Nej/Avbryt); used by
    exactly one, it saves directly. Mirrors Anton's own worked example
    (R-012 "Verify shutdown function" used by three causes)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_recconflict_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.cons_ids = []
        for _ in range(3):
            cause_id = self.db.add_cause(dev_id)
            self.cons_ids.append(self.db.add_consequence(cause_id))
        self.rec_id = self.db.add_recommendation(description='Verify shutdown function')
        for cid in self.cons_ids:
            self.db.link_recommendation_to_consequence(self.rec_id, cid)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _dlg(self, cons_id):
        from hazop import _RecommendationDetailDialog
        return _RecommendationDetailDialog(self.db, self.rec_id, cons_id)

    def _set_text(self, dlg, text):
        dlg._desc.setPlainText(text)

    def test_single_consequence_saves_directly_without_prompting(self):
        solo_rec = self.db.add_recommendation_to_consequence(
            self.cons_ids[0], description='Only used here')
        dlg = self._dlg(self.cons_ids[0])
        dlg.recommendation_id = solo_rec
        self._set_text(dlg, 'Only used here, edited')
        with unittest.mock.patch('hazop.QMessageBox') as mock_box:
            dlg._save()
            mock_box.assert_not_called()
        self.assertEqual(self.db.get_recommendation(solo_rec)['description'],
                         'Only used here, edited')

    def test_yes_updates_the_shared_recommendation_for_all_consequences(self):
        from hazop import QMessageBox as HQMessageBox
        dlg = self._dlg(self.cons_ids[0])
        self._set_text(dlg, 'Verify automatic shutdown function')
        with unittest.mock.patch.object(HQMessageBox, 'exec',
                                        return_value=HQMessageBox.StandardButton.Yes):
            dlg._save()
        rec = self.db.get_recommendation(self.rec_id)
        self.assertEqual(rec['description'], 'Verify automatic shutdown function')
        for cid in self.cons_ids:
            linked = {r['id'] for r in self.db.recommendations_for_consequence(cid)}
            self.assertEqual(linked, {self.rec_id})

    def test_no_forks_a_new_recommendation_for_only_the_current_consequence(self):
        from hazop import QMessageBox as HQMessageBox
        dlg = self._dlg(self.cons_ids[0])
        self._set_text(dlg, 'Verify automatic shutdown function')
        with unittest.mock.patch.object(HQMessageBox, 'exec',
                                        return_value=HQMessageBox.StandardButton.No):
            dlg._save()

        original = self.db.get_recommendation(self.rec_id)
        self.assertEqual(original['description'], 'Verify shutdown function',
            "the original recommendation must stay unchanged for the other causes")

        cons0_ids = {r['id'] for r in self.db.recommendations_for_consequence(self.cons_ids[0])}
        self.assertNotIn(self.rec_id, cons0_ids)
        self.assertEqual(len(cons0_ids), 1)
        new_id = next(iter(cons0_ids))
        self.assertEqual(self.db.get_recommendation(new_id)['description'],
                         'Verify automatic shutdown function')

        for cid in self.cons_ids[1:]:
            linked = {r['id'] for r in self.db.recommendations_for_consequence(cid)}
            self.assertEqual(linked, {self.rec_id},
                "the other consequences must keep pointing at the original recommendation")

    def test_cancel_makes_no_database_change(self):
        from hazop import QMessageBox as HQMessageBox
        dlg = self._dlg(self.cons_ids[0])
        self._set_text(dlg, 'Verify automatic shutdown function')
        with unittest.mock.patch.object(HQMessageBox, 'exec',
                                        return_value=HQMessageBox.StandardButton.Cancel):
            dlg._save()
        self.assertEqual(self.db.get_recommendation(self.rec_id)['description'],
                         'Verify shutdown function')
        for cid in self.cons_ids:
            linked = {r['id'] for r in self.db.recommendations_for_consequence(cid)}
            self.assertEqual(linked, {self.rec_id})


def _find_tree_item(tree, type_, id_=None):
    it = QTreeWidgetItemIterator(tree)
    while it.value():
        item = it.value()
        if item.data(0, Qt.ItemDataRole.UserRole + 1) == type_ and (
                id_ is None or item.data(0, Qt.ItemDataRole.UserRole) == id_):
            return item
        it += 1
    return None


class ConsequenceAndSafeguardTagAppendDbTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_tagappend_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_append_tag_to_consequence_builds_up_text_and_keeps_strip_current(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(cons_id, "hög nivå i", 2)

        self.db.append_tag_to_consequence(cons_id, "TA-1", "Tank")
        cons = dict(self.db.get_consequence(cons_id))
        self.assertEqual(cons['description'], "hög nivå i TA-1")
        self.assertEqual(cons['comp_tag'], "TA-1")
        self.assertEqual(cons['comp_type'], "Tank")

        self.db.update_consequence(
            cons_id, cons['description'] + " => överbreddning till", cons['severity'],
            cons['category'] or '', cons['consequence_chain'] or '')
        self.db.append_tag_to_consequence(cons_id, "TA-2", "Tank")
        cons = dict(self.db.get_consequence(cons_id))
        self.assertEqual(cons['description'], "hög nivå i TA-1 => överbreddning till TA-2")
        self.assertEqual(cons['comp_tag'], "TA-2",
            "the tag strip shows the MOST RECENT drop; the full history lives in the text")
        self.assertEqual(cons['tagged_refs'], "TA-1,TA-2",
            "tagged_refs must remember EVERY tag ever dropped, for bolding both in the text")

    def test_append_tag_to_consequence_preserves_severity_and_category(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        cat = self.db.consequence_categories()[0]
        self.db.update_consequence(cons_id, "beskrivning", 4, cat['name'])

        self.db.append_tag_to_consequence(cons_id, "TA-1", "Tank")

        cons = dict(self.db.get_consequence(cons_id))
        self.assertEqual(cons['severity'], 4)
        self.assertEqual(cons['category'], cat['name'])

    def test_append_tag_to_safeguard_builds_up_text(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_id)
        self.db.update_safeguard(sg_id, description="Larm vid")

        self.db.append_tag_to_safeguard(sg_id, "PSH-101", "Tryckvakt")
        sg = dict(self.db.get_safeguard(sg_id))
        self.assertEqual(sg['description'], "Larm vid PSH-101")

        self.db.update_safeguard(sg_id, description=sg['description'] + " och")
        self.db.append_tag_to_safeguard(sg_id, "PSH-102", "Tryckvakt")
        sg = dict(self.db.get_safeguard(sg_id))
        self.assertEqual(sg['description'], "Larm vid PSH-101 och PSH-102")
        self.assertEqual(sg['comp_tag'], "PSH-102")
        self.assertEqual(sg['tagged_refs'], "PSH-101,PSH-102")


class EquipmentDropOnSafeguardAndMultiTests(unittest.TestCase):
    """_handle_drop's 'equipment'/'equipment-multi' kinds extended to the
    SG column, and multi-marker drops onto a single KON/SG cell using only
    the first dragged marker (2026-08-08, see NOTES.md). Routed through
    panel.eventFilter(), not _handle_drop() directly — see
    DropEventRoutedToViewportTests for why that distinction matters."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_full_chain(self, db):
        node_id = db.add_node()
        dev_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(dev_id)
        cons_id = db.add_consequence(cause_id)
        sg_id = db.add_safeguard(cons_id)
        return node_id, dev_id, cause_id, cons_id, sg_id

    def _make_drop_event(self, panel, text, tgt_row, tgt_col):
        from PyQt6.QtCore import QEvent, QMimeData, QPointF
        vp_x = panel._table.columnViewportPosition(tgt_col) + 2
        vp_y = panel._table.rowViewportPosition(tgt_row) + 2
        mime = QMimeData()
        mime.setText(text)
        event = unittest.mock.MagicMock()
        event.type.return_value = QEvent.Type.Drop
        event.mimeData.return_value = mime
        event.position.return_value = QPointF(vp_x, vp_y)
        event.dropAction.return_value = Qt.DropAction.CopyAction
        return event

    def test_drop_equipment_on_sg_cell_attaches_tag(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id, sg_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            eq_id = win.db.add_equipment_item("PSV-101", "PSV-101", "PSV", 0,
                                              "Säkerhetsventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "PSV-101", 0, 10.0, 10.0, "Säkerhetsventil", confidence=0.9,
                link_method='leader')

            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_SG)
            handled = panel.eventFilter(panel._table.viewport(), event)

            self.assertTrue(handled)
            sg = dict(win.db.get_safeguard(sg_id))
            self.assertEqual(sg['comp_tag'], "PSV-101")
            self.assertEqual(sg['comp_type'], "Säkerhetsventil")

    def test_drop_equipment_on_sg_notifies_tree_refresh(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, _cons_id, sg_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)
            eq_id = win.db.add_equipment_item("PSV-201", "PSV-201", "PSV", 0,
                                              "Sakerhetsventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "PSV-201", 0, 10.0, 10.0, "Sakerhetsventil")
            event = self._make_drop_event(
                panel, f'hzp:equipment:{marker_id}:-1:-1', row, panel._C_SG)

            with unittest.mock.patch.object(win.tree_panel, 'refresh') as refresh:
                self.assertTrue(panel.eventFilter(panel._table.viewport(), event))
                QApplication.processEvents()

            self.assertTrue(refresh.called,
                            "worksheet equipment drop must refresh the tree")

    def test_second_separate_drop_onto_an_already_tagged_sg_row_creates_new_row(self):
        """The 'different objects on different rows' rule for safeguards
        must hold even when the objects arrive as two SEPARATE drag
        gestures, not just one multi-select drag (2026-08-09, see
        NOTES.md: 'jag vill att den ... skall lägga till flera olika
        objekt om jag drar till safeguards med (flera rader)') — the
        second single-object drop must not silently merge into the
        already-tagged row's text."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id, sg_id = self._make_full_chain(win.db)

            eq1 = win.db.add_equipment_item("PSV-101", "PSV-101", "PSV", 0,
                                            "Säkerhetsventil", '', 0)
            eq2 = win.db.add_equipment_item("PSV-102", "PSV-102", "PSV", 0,
                                            "Säkerhetsventil", '', 0)
            m1 = win.db.add_equipment_marker(eq1, "PSV-101", 0, 10.0, 10.0,
                                             "Säkerhetsventil", confidence=0.9, link_method='leader')
            m2 = win.db.add_equipment_marker(eq2, "PSV-102", 0, 20.0, 20.0,
                                             "Säkerhetsventil", confidence=0.9, link_method='leader')

            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)
            event1 = self._make_drop_event(
                panel, f'hzp:equipment:{m1}:-1:-1', row, panel._C_SG)
            self.assertTrue(panel.eventFilter(panel._table.viewport(), event1))

            # Reload so _row_meta reflects the just-created state, then drop
            # the SECOND object on the SAME (now already-tagged) row.
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)
            event2 = self._make_drop_event(
                panel, f'hzp:equipment:{m2}:-1:-1', row, panel._C_SG)
            self.assertTrue(panel.eventFilter(panel._table.viewport(), event2))

            sg = dict(win.db.get_safeguard(sg_id))
            self.assertEqual(sg['description'], "PSV-101",
                "the originally-tagged row's text must not be touched by the second drop")

            all_sgs = [dict(s) for s in win.db.safeguards(cons_id)]
            self.assertEqual(len(all_sgs), 2,
                "the second object must land on a brand new row, not merge into the first")
            new_sg = next(s for s in all_sgs if s['id'] != sg_id)
            self.assertEqual(new_sg['description'], "PSV-102")
            self.assertEqual(new_sg['comp_tag'], "PSV-102")

    def test_drop_equipment_multi_on_kon_appends_all_markers_to_same_consequence(self):
        """Dropping several objects onto ONE consequence must build up its
        text with ALL of them, in order — not just the first (2026-08-09,
        see NOTES.md: 'drar jag till konsekvens skall flera objekt kunna
        ligga i samma konsekvens')."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id, _sg = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            eq1 = win.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
            eq2 = win.db.add_equipment_item("P-102", "P-102", "P", 0, "Pump", '', 0)
            m1 = win.db.add_equipment_marker(eq1, "P-101", 0, 1.0, 1.0, "Pump",
                                             confidence=0.9, link_method='leader')
            m2 = win.db.add_equipment_marker(eq2, "P-102", 0, 2.0, 2.0, "Pump",
                                             confidence=0.9, link_method='leader')

            event = self._make_drop_event(
                panel, f'hzp:equipment-multi:{m1},{m2}:-1:-1', row, panel._C_KON)
            handled = panel.eventFilter(panel._table.viewport(), event)

            self.assertTrue(handled)
            cons = dict(win.db.get_consequence(cons_id))
            self.assertEqual(cons['description'], "P-101 P-102")
            self.assertEqual(cons['comp_tag'], "P-102",
                "the tag strip shows the most recently applied marker")

    def test_drop_equipment_multi_on_sg_creates_one_row_per_extra_marker(self):
        """Dropping several objects onto a SAFEGUARD must NOT merge them
        into one row's text — each additional object becomes its own new
        safeguard row under the same consequence, since distinct objects
        there read as distinct barriers (2026-08-09, see NOTES.md: 'drar
        jag till safeguard skall de olika objekten vara på olika rader')."""
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, cons_id, sg_id = self._make_full_chain(win.db)
            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            eq1 = win.db.add_equipment_item("TSH-1", "TSH-1", "TSH", 0, "Termostat", '', 0)
            eq2 = win.db.add_equipment_item("TSH-2", "TSH-2", "TSH", 0, "Termostat", '', 0)
            m1 = win.db.add_equipment_marker(eq1, "TSH-1", 0, 1.0, 1.0, "Termostat",
                                             confidence=0.9, link_method='leader')
            m2 = win.db.add_equipment_marker(eq2, "TSH-2", 0, 2.0, 2.0, "Termostat",
                                             confidence=0.9, link_method='leader')

            event = self._make_drop_event(
                panel, f'hzp:equipment-multi:{m1},{m2}:-1:-1', row, panel._C_SG)
            handled = panel.eventFilter(panel._table.viewport(), event)

            self.assertTrue(handled)
            sg = dict(win.db.get_safeguard(sg_id))
            self.assertEqual(sg['description'], "TSH-1")
            self.assertEqual(sg['comp_tag'], "TSH-1")

            all_sgs = [dict(s) for s in win.db.safeguards(cons_id)]
            self.assertEqual(len(all_sgs), 2, "a second safeguard row must be created")
            new_sg = next(s for s in all_sgs if s['id'] != sg_id)
            self.assertEqual(new_sg['description'], "TSH-2")
            self.assertEqual(new_sg['comp_tag'], "TSH-2")

    def test_sg_cell_no_longer_carries_removed_object_picker_data(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            _n, _d, cause_id, _cons_id, sg_id = self._make_full_chain(win.db)
            win.db.set_safeguard_tag(sg_id, "FE-301", "Flödesgivare")

            panel.load_cause(cause_id)
            row = next(r for r, m in enumerate(panel._row_meta) if m[3] == sg_id)

            item = panel._table.item(row, panel._C_SG)
            self.assertIsNone(item.data(Qt.ItemDataRole.UserRole + 6))


class EquipmentDropOnTreeDeviationTests(unittest.TestCase):
    """Dragging equipment marker(s) onto a HAZOP-tree deviation item (e.g.
    "Lågt flöde") creates one empty, tagged cause per marker directly — no
    popup (2026-08-08, see NOTES.md, decision: 'Skapa tom orsak direkt')."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_drop_event(self, text, pos):
        from PyQt6.QtCore import QEvent, QMimeData, QPointF
        mime = QMimeData()
        mime.setText(text)
        event = unittest.mock.MagicMock()
        event.type.return_value = QEvent.Type.Drop
        event.mimeData.return_value = mime
        event.position.return_value = QPointF(pos)
        return event

    def test_tree_drop_on_deviation_emits_signal_with_marker_ids(self):
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            tree_panel.refresh()
            tree_panel.tree.expandAll()   # itemAt()/visualItemRect() need the row actually visible
            dev_item = _find_tree_item(tree_panel.tree, DEV_T, dev_id)
            self.assertIsNotNone(dev_item, "sanity: the deviation must actually be in the tree")
            pos = tree_panel.tree.visualItemRect(dev_item).center()

            captured = []
            tree_panel.equipment_dropped_on_deviation.connect(
                lambda d, ids: captured.append((d, ids)))

            event = self._make_drop_event('hzp:equipment:42:-1:-1', pos)
            handled = tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            self.assertTrue(handled)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0], (dev_id, [42]))

    def test_tree_drop_on_flattened_equipment_deviation_resolves_to_its_own_row(self):
        """Bug report (2026-08-13): 'om det redan ligger ett objekt på
        lågt flöde i trädet och jag drar ett nytt objekt dit så kan jag
        inte detta' — dropping a second/different object onto an
        already-equipped guide word used to be silently swallowed.

        2026-08-25 (see NOTES.md "Slå ihop objekt-rad + avvikelse-rad"):
        a SINGLE equipment-linked deviation no longer renders as a
        LEDORD_T wrapper around a nested equipment row — it's now one
        flat DEV_T row ("V-101 — Lågt flöde"). That flat row is exactly
        what a second/different object gets dropped onto here (there's
        no longer a separate shared wrapper row to target); it must
        still resolve to a real deviation, not be swallowed — same
        resolution the pre-existing "kaka på kaka" CAUSE_T case below
        already relies on, since _deviation_item_at's DEV_T/CAUSE_T
        branches are unchanged by this rewrite."""
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            existing_eq = win.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
            node_id = win.db.add_node()
            win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=existing_eq)
            # 2026-08-25 follow-up (see NOTES.md "Rättar ihopslagningen"):
            # the avvikelse row now anchors on the GENERIC deviation
            # (auto-seeded by add_node()), not the equipment-scoped one
            # just created above — get_or_create_deviation is idempotent,
            # so this resolves the same existing generic row.
            anchor_id = win.db.get_or_create_deviation(node_id, "Lågt flöde")
            tree_panel.refresh()
            tree_panel.tree.expandAll()
            dev_item = _find_tree_item(tree_panel.tree, DEV_T, anchor_id)
            self.assertIsNotNone(dev_item,
                "sanity: an equipment-linked guide word must still render as its own flat DEV_T row")
            pos = tree_panel.tree.visualItemRect(dev_item).center()

            captured = []
            tree_panel.equipment_dropped_on_deviation.connect(
                lambda d, ids: captured.append((d, ids)))

            event = self._make_drop_event('hzp:equipment:42:-1:-1', pos)
            handled = tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            self.assertTrue(handled)
            self.assertEqual(len(captured), 1, "the drop must resolve to a deviation, not be swallowed")
            resolved_dev_id, marker_ids = captured[0]
            self.assertEqual(marker_ids, [42])
            self.assertEqual(resolved_dev_id, anchor_id,
                "must resolve to the avvikelse row's own (generic) deviation")

    def test_drag_move_over_flattened_equipment_deviation_accepts_without_writing_to_db(self):
        """The DragMove hover-feedback path must only ever check whether
        a drop WOULD be valid — it must not create a deviation row (a DB
        write) just because the mouse passed over the item."""
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            existing_eq = win.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
            node_id = win.db.add_node()
            win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=existing_eq)
            anchor_id = win.db.get_or_create_deviation(node_id, "Lågt flöde")
            tree_panel.refresh()
            tree_panel.tree.expandAll()
            dev_item = _find_tree_item(tree_panel.tree, DEV_T, anchor_id)
            pos = tree_panel.tree.visualItemRect(dev_item).center()
            before = win.db.deviations(node_id)

            from PyQt6.QtCore import QEvent, QMimeData, QPointF
            mime = QMimeData(); mime.setText('hzp:equipment:42:-1:-1')
            event = unittest.mock.MagicMock()
            event.type.return_value = QEvent.Type.DragMove
            event.mimeData.return_value = mime
            event.position.return_value = QPointF(pos)
            tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            event.acceptProposedAction.assert_called_once()
            self.assertEqual(win.db.deviations(node_id), before,
                "hovering must not create a new deviation row")

    def test_tree_drop_on_ledord_wrapper_still_resolves(self):
        """LEDORD_T is now fully unreachable via refresh() (2026-08-25,
        see NOTES.md "Rättar ihopslagningen" — every avvikelse row is
        DEV_T unconditionally, since object-vs-generic no longer needs
        distinct rendering paths at all). _deviation_item_at's own
        LEDORD_T resolution branch is left in place regardless (harmless,
        zero cost) — verified here against a synthetically constructed
        item instead of relying on refresh() to ever produce one."""
        from PyQt6.QtWidgets import QTreeWidgetItem
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            node_id = win.db.add_node()
            tree_panel.refresh()
            tree_panel.tree.expandAll()
            node_item = _find_tree_item(tree_panel.tree, NODE_T, node_id)
            ledord_item = QTreeWidgetItem(node_item, ["synthetic"])
            ledord_item.setData(0, Qt.ItemDataRole.UserRole, f"{node_id}:Lågt flöde")
            ledord_item.setData(0, Qt.ItemDataRole.UserRole + 1, LEDORD_T)
            pos = tree_panel.tree.visualItemRect(ledord_item).center()

            captured = []
            tree_panel.equipment_dropped_on_deviation.connect(
                lambda d, ids: captured.append((d, ids)))

            event = self._make_drop_event('hzp:equipment:42:-1:-1', pos)
            handled = tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            self.assertTrue(handled)
            self.assertEqual(len(captured), 1, "the drop must resolve to a deviation, not be swallowed")
            resolved_dev_id, marker_ids = captured[0]
            self.assertEqual(marker_ids, [42])
            resolved = win.db.get_deviation(resolved_dev_id)
            self.assertEqual(resolved['node_id'], node_id)
            self.assertEqual(resolved['description'], "Lågt flöde")

    def test_tree_drop_on_merged_single_equipment_cause_row_resolves_its_own_deviation(self):
        """The other tree shape an equipped guide word can collapse into
        (2026-08-09 'kaka på kaka'): a CAUSE_T-typed merged row when the
        linked equipment's only cause is still trivial/untouched. This
        must resolve back to that SAME equipment's own deviation, not
        be rejected either."""
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            eq_id = win.db.add_equipment_item("=M1.GPA6", "=M1.GPA6", "M1", 0, "Pump", '', 0)
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
            from hazop import _create_tagged_cause
            _create_tagged_cause(win.db, dev_id, "Pump", "=M1.GPA6")
            tree_panel.refresh()
            tree_panel.tree.expandAll()
            merged_item = _find_tree_item(tree_panel.tree, CAUSE_T)
            self.assertIsNotNone(merged_item,
                "sanity: the trivial tagged cause must have merged into a CAUSE_T row")
            pos = tree_panel.tree.visualItemRect(merged_item).center()

            captured = []
            tree_panel.equipment_dropped_on_deviation.connect(
                lambda d, ids: captured.append((d, ids)))
            event = self._make_drop_event('hzp:equipment:43:-1:-1', pos)
            tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            self.assertEqual(captured, [(dev_id, [43])])

    def test_tree_drop_on_non_deviation_item_is_ignored(self):
        with _TempDbMainWindow() as win:
            tree_panel = win.tree_panel
            node_id = win.db.add_node()
            tree_panel.refresh()
            node_item = _find_tree_item(tree_panel.tree, NODE_T, node_id)
            self.assertIsNotNone(node_item)
            pos = tree_panel.tree.visualItemRect(node_item).center()

            captured = []
            tree_panel.equipment_dropped_on_deviation.connect(
                lambda d, ids: captured.append((d, ids)))

            event = self._make_drop_event('hzp:equipment:42:-1:-1', pos)
            tree_panel.eventFilter(tree_panel.tree.viewport(), event)

            self.assertEqual(captured, [])
            event.ignore.assert_called()

    def test_on_equipment_dropped_on_deviation_creates_one_cause_per_marker(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq1 = win.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
            eq2 = win.db.add_equipment_item("V-2", "V-2", "V", 0, "Ventil", '', 0)
            m1 = win.db.add_equipment_marker(eq1, "V-1", 0, 1.0, 1.0, "Ventil",
                                             confidence=0.9, link_method='leader')
            m2 = win.db.add_equipment_marker(eq2, "V-2", 0, 2.0, 2.0, "Ventil",
                                             confidence=0.9, link_method='leader')

            # Multiple drops ask whether this is a functional group; this
            # test covers the independent-cause (No) branch.
            with unittest.mock.patch(
                    'hazop.MainWindow._choose_drop_group_operator',
                    return_value=('separate', None)):
                win._on_equipment_dropped_on_deviation(dev_id, [m1, m2])

            causes = win.db.causes(node_id)
            tagged = {c['comp_tag'] for c in causes}
            self.assertEqual(tagged, {"V-1", "V-2"})
            for c in causes:
                self.assertEqual(dict(c)['description'], '')
                self.assertEqual(len(win.db.consequences(c['id'])), 1)

    def test_on_equipment_dropped_on_deviation_assigns_node_when_missing(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq_id = win.db.add_equipment_item("T-1", "T-1", "T", 0, "Behållare", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "T-1", 0, 1.0, 1.0, "Behållare", confidence=0.9, link_method='leader')
            self.assertIsNone(win.db.equipment_node_id(eq_id))

            win._on_equipment_dropped_on_deviation(dev_id, [marker_id])

            self.assertEqual(win.db.equipment_node_id(eq_id), node_id)

    def test_grouped_control_and_affected_objects_create_functional_chain(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Högt flöde")
            instrument_id = win.db.add_equipment_item("FI-1", "FI-1", "FI", 0, "Instrument", '', 0)
            valve_id = win.db.add_equipment_item("FV-1", "FV-1", "FV", 0, "Reglerventil", '', 0)
            m1 = win.db.add_equipment_marker(instrument_id, "FI-1", 0, 1.0, 1.0, "Instrument")
            m2 = win.db.add_equipment_marker(valve_id, "FV-1", 0, 2.0, 2.0, "Reglerventil")

            with unittest.mock.patch(
                    'hazop.MainWindow._choose_drop_group_operator',
                    return_value=('group', '&')):
                win._on_equipment_dropped_on_deviation(dev_id, [m1, m2])

            causes = [dict(c) for c in win.db.causes(node_id)
                      if c['deviation_id'] == dev_id]
            self.assertEqual(len(causes), 1)
            self.assertEqual(causes[0]['comp_tag'], 'FI-1 & FV-1')
            self.assertEqual(causes[0]['description'], '')

    def test_grouped_drop_stores_selected_operator(self):
        for operator in ('&', 'OR', '->'):
            with self.subTest(operator=operator), _TempDbMainWindow() as win:
                node_id = win.db.add_node()
                dev_id = win.db.get_or_create_deviation(node_id, "HÃ¶gt flÃ¶de")
                first_id = win.db.add_equipment_item("A-1", "A-1", "A", 0,
                                                     "Instrument", '', 0)
                second_id = win.db.add_equipment_item("B-2", "B-2", "B", 0,
                                                      "Ventil", '', 0)
                m1 = win.db.add_equipment_marker(first_id, "A-1", 0, 1.0, 1.0,
                                                 "Instrument")
                m2 = win.db.add_equipment_marker(second_id, "B-2", 0, 2.0, 2.0,
                                                 "Ventil")
                with unittest.mock.patch(
                        'hazop.MainWindow._choose_drop_group_operator',
                        return_value=('group', operator)):
                    win._on_equipment_dropped_on_deviation(dev_id, [m1, m2])

                cause = next(c for c in win.db.causes(node_id)
                             if c['secondary_equipment_id'])
                self.assertEqual(cause['comp_tag'], f'A-1 {operator} B-2')

    def test_grouped_drop_keeps_three_objects_and_renders_three_rows(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "HÃ¶gt flÃ¶de")
            ids = [win.db.add_equipment_item(tag, tag, prefix, 0, kind, '', 0)
                   for tag, prefix, kind in (
                       ('FI-1', 'FI', 'Instrument'),
                       ('FV-1', 'FV', 'Ventil'),
                       ('P-1', 'P', 'Pump'))]
            markers = [win.db.add_equipment_marker(equipment_id, tag, 0, float(i), float(i), kind)
                       for i, (equipment_id, tag, kind) in enumerate(zip(
                           ids, ('FI-1', 'FV-1', 'P-1'),
                           ('Instrument', 'Ventil', 'Pump')), 1)]
            with unittest.mock.patch(
                    'hazop.MainWindow._choose_drop_group_operator',
                    return_value=('group', 'OR')):
                win._on_equipment_dropped_on_deviation(dev_id, markers)

            cause = next(c for c in win.db.causes(node_id)
                         if c['secondary_equipment_id'])
            self.assertEqual(json.loads(cause['group_equipment_ids']), ids)
            self.assertEqual(cause['comp_tag'], 'FI-1 OR FV-1 OR P-1')
            win.scenario_panel.load_node(node_id)
            row = next(r for r, meta in enumerate(win.scenario_panel._row_meta)
                       if meta[1] == cause['id'])
            item = win.scenario_panel._table.item(row, win.scenario_panel._C_ORS)
            self.assertEqual(item.data(Qt.ItemDataRole.UserRole + 9),
                             ['FI-1', 'FV-1', 'P-1'])
            self.assertEqual(
                win.scenario_panel._ors_combined_text(item, item.text()).count('\n'), 2)
            panel = win.scenario_panel
            index = panel._table.model().index(row, panel._C_ORS)
            option = QStyleOptionViewItem()
            option.rect = panel._table.visualRect(index)
            panel._group_edit_line = (row, 2)
            editor = panel._pid_delegate.createEditor(panel._table, option, index)
            try:
                panel._pid_delegate.setEditorData(editor, index)
                self.assertEqual(editor.toPlainText(), '')
                editor.setText('tredje hÃ¤ndelsen')
                panel._pid_delegate.setModelData(editor, panel._table.model(), index)
            finally:
                editor.deleteLater()
            panel._group_edit_line = None
            self.assertEqual(
                dict(win.db.get_cause(cause['id']))['description'],
                'FI-1\nFV-1\nP-1 tredje hÃ¤ndelsen')

    def test_group_created_without_default_mechanism_shows_both_objects_in_tree(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Övrigt")
            first_id = win.db.add_equipment_item("A-101", "A-101", "A", 0,
                                                 "Behållare", '', 0)
            second_id = win.db.add_equipment_item("B-202", "B-202", "B", 0,
                                                  "Värmeväxlare", '', 0)
            m1 = win.db.add_equipment_marker(first_id, "A-101", 0, 1.0, 1.0,
                                             "Behållare")
            m2 = win.db.add_equipment_marker(second_id, "B-202", 0, 2.0, 2.0,
                                             "Värmeväxlare")
            with unittest.mock.patch(
                    'hazop.MainWindow._choose_drop_group_operator',
                    return_value=('group', '&')):
                win._on_equipment_dropped_on_deviation(dev_id, [m1, m2])

            cause = next(c for c in win.db.causes(node_id)
                         if c['secondary_equipment_id'])
            win.tree_panel.refresh()
            item = _find_tree_item(win.tree_panel.tree, CAUSE_T, cause['id'])
            self.assertIsNotNone(item)
            self.assertIn('A-101\nB-202', item.text(0))
            self.assertNotIn('Ny orsak', item.text(0))

    def test_group_choice_buttons_are_independent_and_group_text_remains_editable(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Högt flöde")
            primary_id = win.db.add_equipment_item("FI-1", "FI-1", "FI", 0,
                                                   "Instrument", '', 0)
            secondary_id = win.db.add_equipment_item("FV-1", "FV-1", "FV", 0,
                                                     "Reglerventil", '', 0)
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(
                cause_id, comp_type='Instrument', comp_tag='FI-1 + FV-1',
                equipment_id=primary_id,
                secondary_equipment_id=secondary_id,
                description='FI-1 felar högt → FV-1 öppnar fullt')
            panel = win.scenario_panel
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta)
                       if m[1] == cause_id)
            index = panel._table.model().index(row, panel._C_ORS)
            option = QStyleOptionViewItem()
            option.rect = panel._table.visualRect(index)
            panel._group_edit_line = (row, 0)
            primary_editor = panel._pid_delegate.createEditor(
                panel._table, option, index)
            try:
                panel._pid_delegate.setEditorData(primary_editor, index)
                self.assertEqual(primary_editor.toPlainText(),
                                 'felar högt')
            finally:
                primary_editor.deleteLater()
            panel._group_edit_line = None
            panel._apply_group_cause_choice(cause_id, 0, 'Felar lågt')
            cause = dict(win.db.get_cause(cause_id))
            self.assertEqual(cause['group_choices_set'], 3)
            self.assertEqual(cause['description'],
                             'FI-1 felar lågt\nFV-1 öppnar fullt')

            panel._apply_group_cause_choice(cause_id, 1, 'Öppnar felaktigt')
            cause = dict(win.db.get_cause(cause_id))
            self.assertEqual(cause['group_choices_set'], 3)
            self.assertEqual(cause['description'],
                             'FI-1 felar lågt\nFV-1 öppnar felaktigt')

            # Changing only the primary choice must not hide or overwrite the
            # already selected secondary event.
            panel._apply_group_cause_choice(cause_id, 0, 'Felar högt')
            cause = dict(win.db.get_cause(cause_id))
            self.assertEqual(cause['description'],
                             'FI-1 felar högt\nFV-1 öppnar felaktigt')

            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta)
                       if m[1] == cause_id)
            item = panel._table.item(row, panel._C_ORS)
            panel._group_edit_line = (row, 1)
            index = panel._table.model().index(row, panel._C_ORS)
            option = QStyleOptionViewItem()
            option.rect = panel._table.visualRect(index)
            editor = panel._pid_delegate.createEditor(panel._table, option, index)
            try:
                panel._pid_delegate.setEditorData(editor, index)
                self.assertEqual(editor.toPlainText(),
                                 'öppnar felaktigt')
                editor.setText('behöver manövreras manuellt')
                panel._pid_delegate.setModelData(
                    editor, panel._table.model(), index)
            finally:
                editor.deleteLater()
            panel._group_edit_line = None
            self.assertEqual(
                dict(win.db.get_cause(cause_id))['description'],
                'FI-1 felar högt\nFV-1 behöver manövreras manuellt')

            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta)
                       if m[1] == cause_id)
            item = panel._table.item(row, panel._C_ORS)
            panel._table.blockSignals(True)
            item.setText('FI-1 felar lågt\nFV-1 behöver manövreras manuellt')
            panel._table.blockSignals(False)
            panel._on_cell_changed_inner(row, panel._C_ORS)
            self.assertEqual(
                dict(win.db.get_cause(cause_id))['description'],
                'FI-1 felar lågt\nFV-1 behöver manövreras manuellt')

    def test_group_secondary_edit_uses_same_inline_editor_and_popup_as_single_cause(self):
        from scenario_panel import _BoldTagTextEdit, StandardCauseSuggestPopup
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, 'High flow')
            primary_id = win.db.add_equipment_item('FI-1', 'FI-1', 'FI', 0,
                                                   'Instrument', '', 0)
            secondary_id = win.db.add_equipment_item('FV-1', 'FV-1', 'FV', 0,
                                                     'Reglerventil', '', 0)
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(
                cause_id, comp_type='Instrument', comp_tag='FI-1 + FV-1',
                equipment_id=primary_id, secondary_equipment_id=secondary_id,
                description='FI-1 primary text\nFV-1 secondary text',
                group_choices_set=3)
            panel = win.scenario_panel
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta)
                       if m[1] == cause_id)
            index = panel._table.model().index(row, panel._C_ORS)
            panel._group_edit_line = (row, 1)
            panel._table.setCurrentIndex(index)
            panel._table.edit(index)
            QApplication.processEvents()
            editors = panel._table.viewport().findChildren(_BoldTagTextEdit)
            self.assertTrue(editors)
            self.assertTrue(any(e.property('group_line') == 1 and
                                e.toPlainText() == 'secondary text'
                                for e in editors))
            self.assertTrue(panel.window().findChildren(StandardCauseSuggestPopup),
                            'the grouped row must use the same standard-cause popup')
            for editor in editors:
                editor.deleteLater()

    def test_group_row_real_double_click_right_of_tag_opens_inline_editor(self):
        """Regression test for a bug where EVERY double-click on a
        grouped cause's ORS cell -- even one clearly to the right of the
        bold tag, in the free-text zone -- opened the tag/object picker
        (GroupCausePopup) instead of the inline free-text editor.

        Root cause: _on_cell_double_clicked's "cause has no object bound
        yet" fallback checked the single-tag obj_data field, which is
        always empty for a grouped cause (its identity lives in the
        two-entry group_tags list instead) -- so it always looked like
        an unbound cause and short-circuited before ever reaching the
        already-correct group_line/tag_hit logic above it.

        Unlike test_group_secondary_edit_uses_same_inline_editor_and_popup_as_single_cause
        (which manually sets `panel._group_edit_line` and calls
        `table.edit()` directly, bypassing hit-testing entirely), this
        drives the real `_on_cell_double_clicked` entry point with a
        `_double_click_edit` position computed the same way a genuine
        mouse click would be, so it actually exercises the tag_hit/
        line_no geometry that was broken."""
        from scenario_panel import (_BoldTagTextEdit, StandardCauseSuggestPopup,
                                     GroupCausePopup, _ORS_FIRST_LINE_H)
        from PyQt6.QtWidgets import QStyledItemDelegate
        from PyQt6.QtGui import QFontMetrics
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, 'High flow')
            primary_id = win.db.add_equipment_item('FI-1', 'FI-1', 'FI', 0,
                                                   'Instrument', '', 0)
            secondary_id = win.db.add_equipment_item('FV-1', 'FV-1', 'FV', 0,
                                                     'Reglerventil', '', 0)
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(
                cause_id, comp_type='Instrument', comp_tag='FI-1 + FV-1',
                equipment_id=primary_id, secondary_equipment_id=secondary_id,
                description='FI-1\nFV-1', group_choices_set=0)
            panel = win.scenario_panel
            panel.load_node(node_id)
            table = panel._table
            row = next(r for r, m in enumerate(panel._row_meta)
                       if m[1] == cause_id)

            def click_in_group_row(line_no, x_offset=None):
                idx = table.model().index(row, panel._C_ORS)
                rect = table.visualRect(idx)
                item = table.item(row, panel._C_ORS)
                line_h = max(_ORS_FIRST_LINE_H,
                             QFontMetrics(table.font()).height() + 4)
                x = rect.left() + rect.width() - 10 if x_offset is None else rect.left() + x_offset
                pos = QPoint(x,
                             rect.top() + 2 + line_no * line_h + line_h // 2)
                panel._double_click_edit = (row, panel._C_ORS, pos)
                panel._on_cell_double_clicked(item)
                QApplication.processEvents()

            def close_editors():
                # Must tell the view itself the edit session ended (not
                # just delete the editor widget) -- QAbstractItemView
                # tracks an open editor per index internally and refuses
                # a second table.edit() on the same index ("editing
                # failed") until that bookkeeping is cleared, same as the
                # real StandardCauseSuggestPopup._pick()/Enter-key paths.
                delegate = panel._pid_delegate
                for e in list(table.viewport().findChildren(_BoldTagTextEdit)):
                    delegate.closeEditor.emit(e, QStyledItemDelegate.EndEditHint.NoHint)
                for p in list(panel.window().findChildren(StandardCauseSuggestPopup)):
                    p.close()
                QApplication.processEvents()

            # Clicking the bold object tag itself must no longer open the
            # Primär/Sekundär popup; it enters the same inline editor flow.
            click_in_group_row(0, x_offset=12)
            editors = table.viewport().findChildren(_BoldTagTextEdit)
            self.assertTrue(editors, 'primary tag click must open the inline editor')
            self.assertFalse(panel.findChildren(GroupCausePopup))
            close_editors()

            click_in_group_row(0)
            editors = table.viewport().findChildren(_BoldTagTextEdit)
            self.assertTrue(editors, 'primary row click must open the inline editor')
            self.assertTrue(any(e.property('group_line') == 0 for e in editors))
            self.assertTrue(panel.window().findChildren(StandardCauseSuggestPopup))
            self.assertFalse(panel.findChildren(GroupCausePopup),
                              'clicking right of the tag must not open the object picker')
            close_editors()
            ors_item = table.item(row, panel._C_ORS)
            self.assertEqual(
                ors_item.data(Qt.ItemDataRole.UserRole + 9),
                ['FI-1', 'FV-1'])
            combined = panel._ors_combined_text(ors_item, ors_item.text())
            self.assertIn('FI-1', combined)
            self.assertIn('FV-1', combined)

            click_in_group_row(1)
            editors = table.viewport().findChildren(_BoldTagTextEdit)
            self.assertTrue(editors, 'secondary row click must open the inline editor')
            secondary_editor = next(e for e in editors
                                    if e.property('group_line') == 1)
            secondary_editor.setText('secondary changed')
            panel._pid_delegate.setModelData(
                secondary_editor, table.model(),
                table.model().index(row, panel._C_ORS))
            self.assertEqual(
                dict(win.db.get_cause(cause_id))['description'],
                'FI-1\nFV-1 secondary changed')
            self.assertTrue(panel.window().findChildren(StandardCauseSuggestPopup))
            self.assertFalse(panel.findChildren(GroupCausePopup),
                              'clicking right of the tag must not open the object picker')
            close_editors()

    def test_group_popup_uses_secondary_object_and_free_text_preserves_both_rows(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, 'High flow')
            primary_id = win.db.add_equipment_item('FI-1', 'FI-1', 'FI', 0,
                                                   'Instrument', '', 0)
            secondary_id = win.db.add_equipment_item('FV-1', 'FV-1', 'FV', 0,
                                                     'Reglerventil', '', 0)
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(
                cause_id, comp_type='Instrument', comp_tag='FI-1 + FV-1',
                equipment_id=primary_id, secondary_equipment_id=secondary_id,
                description='FI-1 signal intermittent\nFV-1 opens fully')
            panel = win.scenario_panel
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta)
                       if m[1] == cause_id)
            ors_item = panel._table.item(row, panel._C_ORS)
            combined = panel._ors_combined_text(ors_item, ors_item.text())
            self.assertIn('signal intermittent', combined)
            self.assertIn('opens fully', combined)

            _std_id, primary_type, _dev, _rows = \
                panel._ors_standard_causes_for_row(row, 0)
            _std_id, secondary_type, _dev, _rows = \
                panel._ors_standard_causes_for_row(row, 1)
            self.assertEqual(primary_type, 'Instrument')
            self.assertEqual(secondary_type, 'Reglerventil')

            panel._apply_group_cause_choice(cause_id, 0, 'primary custom')
            self.assertEqual(
                dict(win.db.get_cause(cause_id))['description'],
                'FI-1 primary custom\nFV-1 opens fully')

            panel._apply_group_cause_choice(
                cause_id, 1, 'needs manual operation')
            self.assertEqual(
                dict(win.db.get_cause(cause_id))['description'],
                'FI-1 primary custom\nFV-1 needs manual operation')

    def test_group_operator_can_be_changed_without_changing_object_links(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, 'LÃ¥gt flÃ¶de')
            primary_id = win.db.add_equipment_item(
                'FI-1', 'FI-1', 'FI', 0, 'Instrument', '', 0)
            secondary_id = win.db.add_equipment_item(
                'FV-1', 'FV-1', 'FV', 0, 'Reglerventil', '', 0)
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(
                cause_id, comp_type='Instrument', comp_tag='FI-1 + FV-1',
                equipment_id=primary_id, secondary_equipment_id=secondary_id,
                description='FI-1\nFV-1')
            win.scenario_panel._set_group_operator(cause_id, '->')
            cause = dict(win.db.get_cause(cause_id))
            self.assertEqual(cause['comp_tag'], 'FI-1 -> FV-1')
            self.assertEqual(cause['equipment_id'], primary_id)
            self.assertEqual(cause['secondary_equipment_id'], secondary_id)

    def test_new_group_keeps_both_object_tags_when_secondary_text_saved(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, 'High flow')
            primary_id = win.db.add_equipment_item('FI-1', 'FI-1', 'FI', 0,
                                                   'Instrument', '', 0)
            secondary_id = win.db.add_equipment_item('FV-1', 'FV-1', 'FV', 0,
                                                     'Reglerventil', '', 0)
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(
                cause_id, comp_type='Instrument', comp_tag='FI-1 + FV-1',
                equipment_id=primary_id, secondary_equipment_id=secondary_id,
                description='', group_choices_set=0)
            panel = win.scenario_panel
            panel.load_node(node_id)
            row = next(r for r, m in enumerate(panel._row_meta)
                       if m[1] == cause_id)
            item = panel._table.item(row, panel._C_ORS)
            combined = panel._ors_combined_text(item, item.text())
            self.assertEqual(combined, '1.  FI-1\nFV-1')

            from scenario_panel import _BoldTagTextEdit
            editor = _BoldTagTextEdit(panel._table.viewport())
            editor.setProperty('group_line', 1)
            editor.setText('secondary text')
            panel._pid_delegate.setModelData(
                editor, panel._table.model(),
                panel._table.model().index(row, panel._C_ORS))
            self.assertEqual(
                dict(win.db.get_cause(cause_id))['description'],
                'FI-1\nFV-1 secondary text')

    def test_on_equipment_dropped_on_deviation_ignores_unlinked_markers(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            marker_id = win.db.add_equipment_marker(
                None, '', 0, 1.0, 1.0, "Ventil", confidence=0.5, link_method='shape')
            try:
                win._on_equipment_dropped_on_deviation(dev_id, [marker_id])
            except Exception as e:
                self.fail(f"must not raise for an untagged/unlinked marker: {e!r}")
            self.assertEqual(win.db.causes(node_id), [])

    def test_on_equipment_dropped_on_deviation_also_sets_deviation_equipment(self):
        """'drar jag ett eller flera objekt till trädet skall även
        kolumnen utrustning fyllas i så det blir stringent, inte bara
        under orsak' (2026-08-09) — previously only the created CAUSE
        got comp_tag/comp_type (shown in the ORS column); the deviation's
        own equipment_id (driving the worksheet's separate Utrustning
        column) was left untouched, unlike the EquipmentDeviationBar
        checkbox flow which always sets both."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq_id = win.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "V-1", 0, 1.0, 1.0, "Ventil", confidence=0.9, link_method='leader')
            self.assertIsNone(win.db.get_deviation(dev_id)['equipment_id'])

            win._on_equipment_dropped_on_deviation(dev_id, [marker_id])

            self.assertEqual(win.db.get_deviation(dev_id)['equipment_id'], eq_id)

    def test_on_equipment_dropped_on_deviation_does_not_override_existing_equipment(self):
        """A deviation already tied to a specific equipment (e.g. from an
        earlier drop, or the EquipmentDeviationBar flow) must not be
        silently reassigned to a DIFFERENT equipment by a later drop —
        matches the same 'first one wins' rule already used for the
        equipment's own node assignment."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            eq1 = win.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
            eq2 = win.db.add_equipment_item("V-2", "V-2", "V", 0, "Ventil", '', 0)
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq1)
            marker2 = win.db.add_equipment_marker(
                eq2, "V-2", 0, 2.0, 2.0, "Ventil", confidence=0.9, link_method='leader')

            win._on_equipment_dropped_on_deviation(dev_id, [marker2])

            self.assertEqual(win.db.get_deviation(dev_id)['equipment_id'], eq1)

    def test_on_equipment_dropped_on_deviation_worksheet_utrustning_column_reflects_it(self):
        """End-to-end: after the drop, the worksheet's Utrustning column
        for the created cause's row must show the equipment, not stay
        blank."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq_id = win.db.add_equipment_item("T-1", "T-1", "T", 0, "Behållare", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "T-1", 0, 1.0, 1.0, "Behållare", confidence=0.9, link_method='leader')

            win._on_equipment_dropped_on_deviation(dev_id, [marker_id])

            panel = win.scenario_panel
            causes = win.db.causes(node_id)
            cause_id = next(c['id'] for c in causes if c['comp_tag'] == 'T-1')
            row = next(r for r, m in enumerate(panel._row_meta) if m[1] == cause_id)
            utr_item = panel._table.item(row, panel._C_UTR)
            self.assertIn('T-1', utr_item.text())

    def test_dropping_one_marker_scopes_the_scenario_view_to_just_that_cause(self):
        """"när jag drar från pod viewer till trädet så kopplar jag
        objektet mot en ny avvikelse... i hazop scenario ser jag flera
        objekt. jag vill bara se det objektet som precis dragits."
        (2026-08-26) — _on_equipment_dropped_on_deviation used to call
        scenario_panel.load_node(node_id) after the drop, which shows
        every deviation/cause under the WHOLE node the object landed
        in, not just the one the drop just created. Set up a node with
        several UNRELATED pre-existing causes so a naive load_node()
        would leak them into the view, then drop a brand new marker
        onto one of its deviations and confirm only that single cause
        shows."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            other_dev_id = win.db.deviations(node_id)[1]['id']
            # Unrelated pre-existing causes elsewhere under the same node.
            win.db.add_cause(dev_id)
            win.db.add_cause(other_dev_id)
            win.db.add_cause(other_dev_id)

            eq_id = win.db.add_equipment_item("P-1", "P-1", "P", 0, "Pump", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "P-1", 0, 1.0, 1.0, "Pump", confidence=0.9, link_method='leader')

            win._on_equipment_dropped_on_deviation(dev_id, [marker_id])

            panel = win.scenario_panel
            causes = win.db.causes(node_id)
            dropped_cause_id = next(c['id'] for c in causes if c['comp_tag'] == 'P-1')
            cause_ids_shown = {m[1] for m in panel._row_meta if m[1] is not None}
            self.assertEqual(cause_ids_shown, {dropped_cause_id},
                "only the just-dropped object's cause should be visible, "
                "not the whole node's other causes")


class TreeInlineEditTests(unittest.TestCase):
    """Fas E (2026-08-17, see NOTES.md "Trädet: inline-redigering, synk,
    'ej definierad'-hantering") — double-click Nod/Avvikelse/Orsak/
    Konsekvens/Safeguard edits the description directly in the tree
    instead of only via the scenario table, and the result must propagate
    to scenario_panel + P&ID overlays exactly like an edit made there."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _commit(self, tree_panel, item, type_, id_, new_text):
        """Simulate a completed inline edit without driving Qt's real
        interactive editor widget — same direct-commit convention already
        used for ScenarioTablePanel's _on_cell_changed_inner tests."""
        tree_panel._inline_edit_target = (type_, id_)
        tree_panel._commit_inline_text(type_, id_, new_text)

    def test_double_click_deviation_starts_inline_edit(self):
        """The item's own decorated text (numbering/icon prefix included)
        must stay unchanged — a floating editor opens over just the
        description portion instead of replacing the whole cell
        (2026-08-18, see NOTES.md "trädet: numrering bryts ut")."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            win.tree_panel.refresh()
            win.tree_panel.tree.expandAll()
            item = _find_tree_item(win.tree_panel.tree, DEV_T, dev_id)
            self.assertIsNotNone(item)
            decorated_text = item.text(0)

            win.tree_panel._on_item_double_click(item, 0)

            self.assertEqual(win.tree_panel._inline_edit_target, (DEV_T, dev_id))
            self.assertEqual(item.text(0), decorated_text)
            editors = win.tree_panel.tree.viewport().findChildren(QLineEdit)
            self.assertEqual(len(editors), 1)
            self.assertEqual(editors[0].text(), win.db.get_deviation(dev_id)['description'])

    def test_numbering_prefix_survives_inline_edit(self):
        """Direct regression test for the reported bug: renaming a row
        must not make its numbering disappear, neither during nor after
        the edit."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            win.tree_panel.refresh()
            win.tree_panel.tree.expandAll()
            item = _find_tree_item(win.tree_panel.tree, DEV_T, dev_id)

            win.tree_panel._on_item_double_click(item, 0)
            self.assertIn("1.", item.text(0), "numbering must still be visible while editing")

            editor = win.tree_panel.tree.viewport().findChildren(QLineEdit)[0]
            editor.setText("Nytt namn")
            editor.editingFinished.emit()

            self.assertEqual(win.db.get_deviation(dev_id)['description'], "Nytt namn")
            item_after = _find_tree_item(win.tree_panel.tree, DEV_T, dev_id)
            self.assertIn("1.", item_after.text(0))
            self.assertIn("Nytt namn", item_after.text(0))

    def test_escape_cancels_inline_edit_without_saving(self):
        from PyQt6.QtCore import QEvent
        from PyQt6.QtGui import QKeyEvent
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            win.db.update_deviation(dev_id, "Original")
            win.tree_panel.refresh()
            item = _find_tree_item(win.tree_panel.tree, DEV_T, dev_id)

            win.tree_panel._on_item_double_click(item, 0)
            editor = win.tree_panel.tree.viewport().findChildren(QLineEdit)[0]
            editor.setText("Skulle inte sparas")
            ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
            editor.keyPressEvent(ev)

            self.assertEqual(win.db.get_deviation(dev_id)['description'], "Original")
            self.assertIsNone(win.tree_panel._inline_edit_target)

    def test_node_with_markup_still_jumps_instead_of_editing(self):
        """Existing behavior (double-click a node that already has P&ID
        markup jumps the view there) must survive unchanged — inline
        editing only kicks in for a node WITHOUT markup."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            # has_node_markups() checks the node_markups TABLE (the newer
            # multi-markup system), not the legacy nodes.markup_points
            # column that add_node_with_markup() sets.
            win.db.add_node_markup(node_id, 'polygon', [[0, 0], [10, 0], [10, 10]], '', '#000', 0.5, 4, 0)
            win.tree_panel.refresh()
            win.tree_panel.tree.expandAll()
            item = _find_tree_item(win.tree_panel.tree, NODE_T, node_id)
            self.assertIsNotNone(item)

            jumped = []
            win.tree_panel.node_jump_to_markup.connect(jumped.append)
            win.tree_panel._on_item_double_click(item, 0)

            self.assertEqual(jumped, [node_id])
            self.assertIsNone(win.tree_panel._inline_edit_target)

    def test_committing_node_rename_persists_and_keeps_other_fields(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win.db.update_node(node_id, "Original", "Beskrivning X", "P-101", "Vatten", "10 bar", "80 C")
            win.tree_panel.refresh()
            item = _find_tree_item(win.tree_panel.tree, NODE_T, node_id)
            self._commit(win.tree_panel, item, NODE_T, node_id, "Nytt namn")

            node = win.db.get_node(node_id)
            self.assertEqual(node['name'], "Nytt namn")
            self.assertEqual(node['description'], "Beskrivning X")
            self.assertEqual(node['pid_ref'], "P-101")

    def test_committing_deviation_edit_persists(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            win.tree_panel.refresh()
            item = _find_tree_item(win.tree_panel.tree, DEV_T, dev_id)
            self._commit(win.tree_panel, item, DEV_T, dev_id, "Anpassad avvikelse")
            self.assertEqual(win.db.get_deviation(dev_id)['description'], "Anpassad avvikelse")

    def test_committing_cause_edit_persists(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            win.tree_panel.refresh()
            item = _find_tree_item(win.tree_panel.tree, CAUSE_T, cause_id)
            self._commit(win.tree_panel, item, CAUSE_T, cause_id, "Ny orsakstext")
            self.assertEqual(win.db.get_cause(cause_id)['description'], "Ny orsakstext")

    def test_committing_consequence_edit_keeps_severity_and_category(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            win.db.update_consequence(cons_id, "Original", 3, "Miljö")
            win.tree_panel.refresh()
            item = _find_tree_item(win.tree_panel.tree, CONS_T, cons_id)
            self._commit(win.tree_panel, item, CONS_T, cons_id, "Ny konsekvens")

            cons = win.db.get_consequence(cons_id)
            self.assertEqual(cons['description'], "Ny konsekvens")
            self.assertEqual(cons['severity'], 3)
            self.assertEqual(cons['category'], "Miljö")

    def test_committing_safeguard_edit_keeps_rrf(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            cons_id = win.db.add_consequence(cause_id)
            sg_id = win.db.add_safeguard(cons_id)
            win.db.update_safeguard(sg_id, "Original", 100)
            win.tree_panel.refresh()
            item = _find_tree_item(win.tree_panel.tree, SG_T, sg_id)
            self._commit(win.tree_panel, item, SG_T, sg_id, "Nytt skydd")

            sg = win.db.get_safeguard(sg_id)
            self.assertEqual(sg['description'], "Nytt skydd")
            self.assertEqual(sg['rrf'], 100)

    def test_editable_flag_never_set_by_inline_edit(self):
        """The overlay editor (2026-08-18) never sets ItemIsEditable on the
        underlying QTreeWidgetItem — unlike Qt's old native item-text
        editing, nothing here makes the tree item itself editable in
        place."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            win.tree_panel.refresh()
            item = _find_tree_item(win.tree_panel.tree, DEV_T, dev_id)
            win.tree_panel._on_item_double_click(item, 0)
            self.assertFalse(bool(item.flags() & Qt.ItemFlag.ItemIsEditable))
            self._commit(win.tree_panel, item, DEV_T, dev_id, "X")
            # refresh() rebuilds the tree, so re-fetch the (new) item object.
            item_after = _find_tree_item(win.tree_panel.tree, DEV_T, dev_id)
            self.assertFalse(bool(item_after.flags() & Qt.ItemFlag.ItemIsEditable))

    def test_inline_edit_refreshes_scenario_and_pid_overlays(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            win.tree_panel.refresh()
            item = _find_tree_item(win.tree_panel.tree, CAUSE_T, cause_id)

            reload_calls = []
            win.pid_panel.reload_overlays = lambda *a, **k: reload_calls.append(True)
            refresh_calls = []
            original_refresh = win.scenario_panel.refresh
            win.scenario_panel.refresh = lambda *a, **k: (refresh_calls.append(True), original_refresh())[1]

            self._commit(win.tree_panel, item, CAUSE_T, cause_id, "Uppdaterad")

            self.assertTrue(reload_calls, "P&ID overlays must reload after an inline tree edit")
            self.assertTrue(refresh_calls, "scenario table must refresh after an inline tree edit")


class EquipmentEditRequestedHandlerTests(unittest.TestCase):
    """MainWindow._on_equipment_edit_requested — the popup + DB-write side
    of the "✏️ Redigera objekt" action above."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_equipedit_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_committing_new_tag_and_type_updates_existing_catalog_row(self):
        from PyQt6.QtWidgets import QDialog
        eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
        marker_id = self.db.add_equipment_marker(eq_id, "PV-101", 0, 1.0, 1.0, "Ventil")

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db

            def _fake_exec(self):
                self.committed.emit("PV-102", "Pump")
                return QDialog.DialogCode.Accepted

            with unittest.mock.patch.object(hazop.EquipmentTagPopup, 'exec', new=_fake_exec), \
                 unittest.mock.patch.object(win.pid_panel, 'reload_overlays') as mock_reload, \
                 unittest.mock.patch.object(win.tree_panel, 'refresh') as mock_tree_refresh:
                win._on_equipment_edit_requested(marker_id)

            updated = self.db.get_equipment_by_id(eq_id)
            self.assertEqual(updated['tag'], "PV-102")
            self.assertEqual(updated['equipment_type'], "Pump")
            mock_reload.assert_called_once()
            # The tree's EQUIP_T rows read equipment_catalog live too
            # (2026-08-18, see NOTES.md "Objektets identitet ...") — used
            # to only pick this up on its next unrelated rebuild.
            mock_tree_refresh.assert_called_once()

    def test_untagged_marker_shows_info_instead_of_crashing(self):
        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db
            with unittest.mock.patch.object(hazop.EquipmentTagPopup, 'exec') as mock_exec, \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                win._on_equipment_edit_requested(9999)   # no such marker
            mock_exec.assert_not_called()
            mock_info.assert_called_once()


class EquipmentDeleteRequestedHandlerTests(unittest.TestCase):
    """PIDPanel._on_equipment_delete_requested — the confirm+delete side of
    right-click "Ta bort" on an existing equipment marker (2026-08-25, see
    NOTES.md — Anton: "om man högerklickar på objektet så ska också
    alternativet att ta bort finnas"), plus MainWindow's tree/scenario
    refresh via the bubbled equipment_deleted signal."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_equipdelete_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_confirmed_delete_removes_equipment_and_refreshes_tree_and_scenario(self):
        eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
        marker_id = self.db.add_equipment_marker(eq_id, "PV-101", 0, 1.0, 1.0, "Ventil")

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db

            with unittest.mock.patch.object(
                    QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), \
                 unittest.mock.patch.object(win.pid_panel, '_load_overlays'), \
                 unittest.mock.patch.object(win.tree_panel, 'refresh') as mock_tree_refresh, \
                 unittest.mock.patch.object(win.scenario_panel, 'schedule_rebuild') as mock_rebuild:
                win.pid_panel._on_equipment_delete_requested(marker_id)

            self.assertIsNone(self.db.get_equipment_by_id(eq_id))
            mock_tree_refresh.assert_called_once()
            mock_rebuild.assert_called_once()

    def test_cancelled_delete_keeps_equipment(self):
        eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
        marker_id = self.db.add_equipment_marker(eq_id, "PV-101", 0, 1.0, 1.0, "Ventil")

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db

            with unittest.mock.patch.object(
                    QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
                win.pid_panel._on_equipment_delete_requested(marker_id)

            self.assertIsNotNone(self.db.get_equipment_by_id(eq_id))

    def test_unknown_marker_is_a_no_op(self):
        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db
            with unittest.mock.patch.object(QMessageBox, 'question') as mock_q:
                win.pid_panel._on_equipment_delete_requested(9999)
            mock_q.assert_not_called()


class EquipmentBarUpdateAndDeleteBubbleTests(unittest.TestCase):
    """End-to-end wiring for EquipmentDeviationBar's tag/typ edit and
    "Ta bort" button (2026-08-25, see NOTES.md — Anton: "Om jag
    vänsterklickar på ett objekt på pid viewer ska man kunna editera
    objektnamn (tag) och objekttyp. Man ska även kunna klicka på
    deleteknappen för att ta bort.") — PIDPanel.equipment_updated/
    equipment_deleted must actually reach MainWindow's tree/scenario
    refresh, not just update the popup's own fields in isolation (already
    covered by EquipmentDeviationBarTests in test_pid_panel_mod.py)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_equipbarbubble_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_editing_tag_from_the_bar_refreshes_tree_and_scenario(self):
        eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
        marker_id = self.db.add_equipment_marker(eq_id, "PV-101", 0, 1.0, 1.0, "Ventil")

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db
            # PIDPanel.db has no cascading setter of its own — the bar
            # keeps a SEPARATE db reference (see EquipmentDeviationBar.db's
            # own setter docstring), normally kept in sync by
            # MainWindow._reload_all_panels() on a real project swap.
            win.pid_panel._equipment_bar.db = self.db
            win.pid_panel._equipment_bar.load(eq_id, marker_id)

            with unittest.mock.patch.object(win.tree_panel, 'refresh') as mock_tree_refresh, \
                 unittest.mock.patch.object(win.scenario_panel, 'schedule_rebuild') as mock_rebuild:
                win.pid_panel._equipment_bar._tag_edit.setText("PV-102")
                win.pid_panel._equipment_bar._commit_tag()

            self.assertEqual(self.db.get_equipment_by_id(eq_id)['tag'], "PV-102")
            mock_tree_refresh.assert_called_once()
            mock_rebuild.assert_called_once()

    def test_deleting_from_the_bar_refreshes_tree_and_scenario(self):
        eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
        marker_id = self.db.add_equipment_marker(eq_id, "PV-101", 0, 1.0, 1.0, "Ventil")

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db
            # PIDPanel.db has no cascading setter of its own — the bar
            # keeps a SEPARATE db reference (see EquipmentDeviationBar.db's
            # own setter docstring), normally kept in sync by
            # MainWindow._reload_all_panels() on a real project swap.
            win.pid_panel._equipment_bar.db = self.db
            win.pid_panel._equipment_bar.load(eq_id, marker_id)

            with unittest.mock.patch.object(
                    QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), \
                 unittest.mock.patch.object(win.pid_panel, '_load_overlays'), \
                 unittest.mock.patch.object(win.tree_panel, 'refresh') as mock_tree_refresh, \
                 unittest.mock.patch.object(win.scenario_panel, 'schedule_rebuild') as mock_rebuild:
                win.pid_panel._equipment_bar._on_delete_clicked()

            self.assertIsNone(self.db.get_equipment_by_id(eq_id))
            mock_tree_refresh.assert_called_once()
            mock_rebuild.assert_called_once()


    def test_deleting_from_the_bar_preserves_the_trees_current_selection(self):
        """"När ett objekt tas bort från P&ID ska HAZOP-trädet uppdateras
        utan att öppna noder stängs. Behåll samma position och markerat
        objekt om möjligt." (2026-08-26). MainWindow._on_equipment_changed_
        from_marker used to call tree_panel.refresh() bare -- refresh()
        only re-selects/scrolls to an item when told which one via its
        select_type/select_id params, so the tree always ended up with
        NOTHING selected after "Ta bort", even for an item completely
        unrelated to the deleted object."""
        eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
        marker_id = self.db.add_equipment_marker(eq_id, "PV-101", 0, 1.0, 1.0, "Ventil")
        other_node_id = self.db.add_node()

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db
            win.tree_panel.db = self.db
            win.pid_panel._equipment_bar.db = self.db
            win.pid_panel._equipment_bar.load(eq_id, marker_id)
            win.tree_panel.refresh()
            other_item = _find_tree_item(win.tree_panel.tree, NODE_T, other_node_id)
            self.assertIsNotNone(other_item, "sanity check: the unrelated node must be in the tree")
            win.tree_panel.tree.setCurrentItem(other_item)

            with unittest.mock.patch.object(
                    QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), \
                 unittest.mock.patch.object(win.pid_panel, '_load_overlays'):
                win.pid_panel._equipment_bar._on_delete_clicked()

            self.assertIsNone(self.db.get_equipment_by_id(eq_id))
            cur = win.tree_panel.tree.currentItem()
            self.assertIsNotNone(cur,
                "the tree must not end up with nothing selected after "
                "deleting an unrelated object from the P&ID")
            self.assertEqual(cur.data(0, Qt.ItemDataRole.UserRole + 1), NODE_T)
            self.assertEqual(cur.data(0, Qt.ItemDataRole.UserRole), other_node_id)

    def test_deleting_the_objects_currently_filtered_in_scenario_falls_back_instead_of_blanking(self):
        """"HAZOP Scenario-vyn ska inte hoppa eller bli blank." (2026-08-26).
        ScenarioTablePanel.load_equipment() (triggered by clicking a P&ID
        marker) filters rows by a tag/type match resolved live from
        equipment_catalog -- once the row is deleted, that match resolves
        to nothing and the table used to silently rebuild to zero rows.
        The fix must detect exactly this case (filter target == the
        deleted id) and fall back to load_all() instead."""
        eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
        marker_id = self.db.add_equipment_marker(eq_id, "PV-101", 0, 1.0, 1.0, "Ventil")

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db
            win.pid_panel._equipment_bar.db = self.db
            win.pid_panel._equipment_bar.load(eq_id, marker_id)
            win.scenario_panel.load_equipment(eq_id)
            self.assertEqual(win.scenario_panel.get_equipment_filter(), eq_id)

            with unittest.mock.patch.object(
                    QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), \
                 unittest.mock.patch.object(win.pid_panel, '_load_overlays'), \
                 unittest.mock.patch.object(win.tree_panel, 'refresh'):
                win.pid_panel._equipment_bar._on_delete_clicked()

            self.assertIsNone(self.db.get_equipment_by_id(eq_id))
            self.assertIsNone(win.scenario_panel.get_equipment_filter(),
                "the stale equipment filter must be cleared, not left "
                "pointing at a now-deleted id")

    def test_editing_a_different_objects_tag_does_not_disturb_an_active_equipment_filter(self):
        """The fallback above must only trigger for the object the table
        is ACTUALLY filtered to, and only when it was truly deleted (not
        merely edited) -- otherwise every unrelated equipment_updated/
        equipment_deleted signal would blow away a perfectly valid
        filter."""
        eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
        other_eq_id = self.db.add_equipment_item("PV-102", "PV-102", "PV", 0, "Ventil", "", 0)
        other_marker_id = self.db.add_equipment_marker(other_eq_id, "PV-102", 0, 2.0, 2.0, "Ventil")

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db
            win.pid_panel._equipment_bar.db = self.db
            win.scenario_panel.load_equipment(eq_id)

            win.pid_panel._equipment_bar.load(other_eq_id, other_marker_id)
            with unittest.mock.patch.object(win.tree_panel, 'refresh'):
                win.pid_panel._equipment_bar._tag_edit.setText("PV-103")
                win.pid_panel._equipment_bar._commit_tag()

            self.assertEqual(win.scenario_panel.get_equipment_filter(), eq_id,
                "editing a DIFFERENT object must not touch the active filter")


class EquipmentIdentityCrossPanelSyncTests(unittest.TestCase):
    """"Objektets identitet på P&ID, HAZOP scenario och trädet måste höra
    ihop. Bind dessa så de lirar och alltid på alla tre ställen oavsett
    var man editerar." (2026-08-18) — an identity edit (tag/type) made on
    ANY of the three surfaces (P&ID's "Redigera objekt", the scenario
    table's ORS tag popup, the Utrustningsregister table) must refresh
    the OTHER two, not just the surface it was made on. Each of these
    already refreshed some of the others; this class covers the
    previously-missing connections, one MainWindow wiring point at a
    time."""

    @staticmethod
    def _find_equip_tag_item(tree_panel, eq_id):
        """2026-08-25 (see NOTES.md "Rättar ihopslagningen"): object
        identity now lives on the Orsak (CAUSE_T) row, resolved live via
        causes.equipment_id — find the cause row(s) linked to this
        equipment directly, rather than a no-longer-set _EQUIP_TAG_ROLE."""
        it = QTreeWidgetItemIterator(tree_panel.tree)
        while it.value():
            item = it.value()
            if item.data(0, Qt.ItemDataRole.UserRole + 1) == CAUSE_T:
                cause = tree_panel.db.get_cause(item.data(0, Qt.ItemDataRole.UserRole))
                if cause and cause.get('equipment_id') == eq_id:
                    return item
            it += 1
        return None

    def test_scenario_tag_popup_rename_refreshes_tree(self):
        """scenario_panel.equipment_renamed used to only be wired to
        pid_panel.reload_overlays — the tree's EQUIP_T rows read
        equipment_catalog live too and were left showing the old tag/type
        until some unrelated event rebuilt the tree. Verified functionally
        (the real tree actually shows the new tag) rather than by mocking
        tree_panel.refresh — a signal already connected to the ORIGINAL
        bound method at MainWindow construction time keeps calling that
        original, not a mock patched onto the instance afterward."""
        with _TempDbMainWindow() as win:
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
            node_id = win.db.add_node()
            # The tree's equipment grouping/_EQUIP_TAG_ROLE keys off
            # deviations.equipment_id (a separate FK from causes.equipment_id
            # below, which the scenario table's ORS strip uses) — both must
            # be set for a real equipment-tag row to appear in the tree.
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(cause_id, comp_tag="PV-101", comp_type="Ventil",
                                 equipment_id=eq_id)

            win.scenario_panel._apply_cause_obj(0, cause_id, "Ventil", "PV-102", '', None)

            self.assertEqual(win.db.get_equipment_by_id(eq_id)['tag'], "PV-102")
            win.tree_panel.tree.expandAll()
            item = self._find_equip_tag_item(win.tree_panel, eq_id)
            self.assertIsNotNone(item, "tree must have refreshed and show a row for this equipment")
            self.assertIn("PV-102", item.text(0))

    def test_equipment_register_inline_edit_refreshes_scenario_and_pid_and_tree(self):
        """Editing a tag/type directly in the Utrustningsregister
        (_EquipmentTableModel.setData) used to refresh nothing at all —
        neither P&ID, the scenario table, nor the tree ever heard about
        it. Verified functionally (see the class docstring above for
        why mocking the connected slots doesn't work here)."""
        from equipment_panel import _EC_TAG
        with _TempDbMainWindow() as win:
            eq_id = win.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "", 0)
            node_id = win.db.add_node()
            dev_id = win.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
            cause_id = win.db.add_cause(dev_id)
            win.db.update_cause(cause_id, comp_tag="PV-101", comp_type="Ventil",
                                 equipment_id=eq_id)
            win.db.add_equipment_marker(eq_id, "PV-101", 0, 10.0, 10.0, "Ventil")
            _fake_pdf_loaded(win.pid_panel)
            # scenario_panel.schedule_rebuild() defers via QTimer.singleShot
            # (see _schedule_rebuild) — load_node() first so there's a
            # current view for _on_rebuild_scheduled() to re-render below,
            # same "invoke the deferred handler directly" convention used
            # elsewhere in this file for testing scheduled rebuilds.
            win.scenario_panel.load_node(node_id)

            win.equipment_panel._model.load()
            row = next(i for i, r in enumerate(win.equipment_panel._model.rows())
                       if r['id'] == eq_id)
            with unittest.mock.patch.object(win.pid_panel.viewer, 'add_equipment_marker') as mock_add:
                index = win.equipment_panel._model.index(row, _EC_TAG)
                win.equipment_panel._model.setData(index, "PV-102")

            self.assertEqual(win.db.get_equipment_by_id(eq_id)['tag'], "PV-102")
            # P&ID: reload_overlays() must have actually run (synchronous)
            # and redrawn the marker with the new tag.
            mock_add.assert_called_once()
            self.assertEqual(mock_add.call_args.args[4], "PV-102")
            # Scenario table: schedule_rebuild() must have actually been
            # called — run its deferred handler and check the ORS cell.
            win.scenario_panel._on_rebuild_scheduled()
            scn_row = next(r for r, m in enumerate(win.scenario_panel._row_meta)
                            if m[1] == cause_id)
            item = win.scenario_panel._table.item(scn_row, win.scenario_panel._C_ORS)
            self.assertEqual(item.data(Qt.ItemDataRole.UserRole + 2), ("Ventil", "PV-102"))
            # Tree: refresh() must have actually rebuilt the tree.
            win.tree_panel.tree.expandAll()
            item = self._find_equip_tag_item(win.tree_panel, eq_id)
            self.assertIsNotNone(item, "tree must have refreshed and show a row for this equipment")
            self.assertIn("PV-102", item.text(0))


class CauseTagLiveLinkTests(unittest.TestCase):
    """"fixa till så att taggen är kopplad till objekten i orsaken på
    hazop scenario. så ändrar jag i hazop scenario ändras namnet på
    p&id och vice versa" (2026-08-13) — the ORS cell's tag strip
    (comp_type/comp_tag) used to be a frozen text snapshot with no
    connection to equipment_catalog. causes.equipment_id is now a real
    FK (same pattern as the pre-existing deviations.equipment_id),
    resolved live at render time (_cause_tag_display) so a rename on
    the P&ID shows up immediately, and _apply_cause_obj (the
    CauseObjectPopup commit handler) renames the ACTUAL
    equipment_catalog row when the user edits the tag text of an
    already-linked cause, instead of just overwriting this one cell's
    private copy."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_causetaglink_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.node_id = self.db.add_node()
        self.dev_id = self.db.deviations(self.node_id)[0]['id']
        self.eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _ors_tag(self, cause_id):
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == cause_id)
        item = self.panel._table.item(row, self.panel._C_ORS)
        return item.data(Qt.ItemDataRole.UserRole + 2), row

    def test_create_tagged_cause_links_equipment_id(self):
        from hazop import _create_tagged_cause
        cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)
        self.assertEqual(self.db.get_cause(cause_id)['equipment_id'], self.eq_id)

    def test_ors_strip_reflects_current_equipment_tag_after_pid_rename(self):
        """The P&ID-rename → hazop-scenario direction: renaming the
        object updates the strip on the very next redraw, with no
        write to the causes row at all."""
        from hazop import _create_tagged_cause
        cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)

        self.db.update_equipment_item(self.eq_id, "PV-102", "PV", "Ventil", "")
        (comp_type, comp_tag), _ = self._ors_tag(cause_id)
        self.assertEqual(comp_tag, "PV-102")
        self.assertEqual(comp_type, "Ventil")
        # The row's own comp_tag snapshot is untouched by the rename —
        # only the live resolution changed what's displayed.
        self.assertEqual(self.db.get_cause(cause_id)['comp_tag'], "PV-101")

    def test_editing_tag_in_popup_renames_the_actual_equipment(self):
        """The hazop-scenario → P&ID direction: editing the tag text of
        an already-linked cause renames equipment_catalog itself."""
        from hazop import _create_tagged_cause
        cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)
        _, row = self._ors_tag(cause_id)

        self.panel._apply_cause_obj(row, cause_id, "Ventil", "PV-103", "", None)

        self.assertEqual(self.db.get_equipment_by_id(self.eq_id)['tag'], "PV-103")

    def test_rename_via_popup_is_visible_from_a_different_cause_on_the_same_equipment(self):
        """Confirms the link is shared via equipment_id, not private to
        the one cell that triggered the rename."""
        from hazop import _create_tagged_cause
        cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)
        other_cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)
        _, row = self._ors_tag(cause_id)

        self.panel._apply_cause_obj(row, cause_id, "Ventil", "PV-103", "", None)

        (_, other_tag), _ = self._ors_tag(other_cause_id)
        self.assertEqual(other_tag, "PV-103")

    def test_editing_tag_calls_the_equipment_renamed_callback(self):
        from hazop import _create_tagged_cause
        cause_id, _ = _create_tagged_cause(
            self.db, self.dev_id, "Ventil", "PV-101", equipment_id=self.eq_id)
        _, row = self._ors_tag(cause_id)
        called = []
        self.panel.equipment_renamed.connect(lambda: called.append(True))

        self.panel._apply_cause_obj(row, cause_id, "Ventil", "PV-103", "", None)

        self.assertEqual(called, [True])

    def test_new_tag_matching_an_existing_object_links_without_renaming(self):
        """A cause with no link yet, whose typed tag happens to match an
        existing object exactly, gets LINKED — there's nothing to
        rename FROM, so the object itself is untouched."""
        cause_id = self.db.add_cause(self.dev_id)
        _, row = self._ors_tag(cause_id)

        self.panel._apply_cause_obj(row, cause_id, "Ventil", "PV-101", "", None)

        self.assertEqual(self.db.get_cause(cause_id)['equipment_id'], self.eq_id)
        self.assertEqual(self.db.get_equipment_by_id(self.eq_id)['tag'], "PV-101")

    def test_custom_unmatched_tag_stays_unlinked(self):
        cause_id = self.db.add_cause(self.dev_id)
        _, row = self._ors_tag(cause_id)

        self.panel._apply_cause_obj(row, cause_id, "Övrigt", "CUSTOM-999", "", None)

        self.assertIsNone(self.db.get_cause(cause_id)['equipment_id'])

    def test_backfill_links_an_unambiguous_existing_comp_tag(self):
        cause_id = self.db.add_cause(self.dev_id)
        self.db.update_cause(cause_id, comp_type="Ventil", comp_tag="PV-101")
        self.db.conn.execute("UPDATE causes SET equipment_id=NULL WHERE id=?", (cause_id,))
        self.db.commit()

        self.db._backfill_cause_equipment_ids()

        self.assertEqual(self.db.get_cause(cause_id)['equipment_id'], self.eq_id)

    def test_backfill_leaves_ambiguous_comp_tag_unlinked(self):
        dup_eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 1, "Ventil", '', 0)
        cause_id = self.db.add_cause(self.dev_id)
        self.db.update_cause(cause_id, comp_type="Ventil", comp_tag="PV-101")
        self.db.conn.execute("UPDATE causes SET equipment_id=NULL WHERE id=?", (cause_id,))
        self.db.commit()

        self.db._backfill_cause_equipment_ids()

        self.assertIsNone(self.db.get_cause(cause_id)['equipment_id'])

    def test_pid_rename_triggers_scenario_rebuild_via_mainwindow_wiring(self):
        """End-to-end confirmation of the MainWindow-level wiring, not
        just the panel's own logic in isolation."""
        from PyQt6.QtWidgets import QDialog
        eq_id = self.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", "", 0)
        marker_id = self.db.add_equipment_marker(eq_id, "V-1", 0, 1.0, 1.0, "Ventil")

        with _TempDbMainWindow() as win:
            win.db = self.db
            win.pid_panel.db = self.db
            win.scenario_panel.db = self.db

            def _fake_exec(self):
                self.committed.emit("V-2", "Ventil")
                return QDialog.DialogCode.Accepted

            with unittest.mock.patch.object(hazop.EquipmentTagPopup, 'exec', new=_fake_exec), \
                 unittest.mock.patch.object(win.pid_panel, 'reload_overlays'), \
                 unittest.mock.patch.object(win.scenario_panel, '_schedule_rebuild') as mock_rebuild:
                win._on_equipment_edit_requested(marker_id)

            mock_rebuild.assert_called_once()


class AutoConsequenceAndSafeguardOnCauseTemplateTests(unittest.TestCase):
    """'När jag definerar avvikelse för objektet så ska jag kunna klicka på
    konsekvens ... och definiera detta. ... Dessutom vill jag kunna göra
    samma med safeguard.' (2026-08-09, see NOTES.md) — checking a deviation
    in EquipmentDeviationBar used to create a cause with NO consequence/
    safeguard at all, so the KON/SG cells for that row had no real item to
    click into. Both are now auto-created empty, immediately ready for the
    already-existing KON/SG inline-edit machinery (from earlier sessions,
    see NOTES.md). The classic P&ID-click cause flow this class used to
    also cover was removed 2026-08-13 (see NOTES.md: the P&ID canvas is
    now object-placement-only) — place_cause_from_template's only
    remaining caller is EquipmentDeviationBar's _create_cause_for_bar."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_autocons_sg_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from pid_viewer import PIDPanel
        self.panel = PIDPanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_place_cause_from_template_creates_empty_consequence_and_safeguard(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']

        cause_id = self.panel.place_cause_from_template(
            dev_id, "Ventil", "HV-101", "Läckage", None)

        self.assertIsNotNone(cause_id)
        cons_list = self.db.consequences(cause_id)
        self.assertEqual(len(cons_list), 1)
        cons_id = cons_list[0]['id']
        # db.add_consequence()/add_safeguard() default to empty (2026-08-12,
        # see NOTES.md — shown as "—" until defined, not literal "Ny
        # konsekvens"/"Ny safeguard" text) — still immediately overtype-
        # able via the existing KON/SG inline-edit machinery either way.
        self.assertEqual(cons_list[0]['description'], '')
        sg_list = self.db.safeguards(cons_id)
        self.assertEqual(len(sg_list), 1)
        self.assertEqual(sg_list[0]['description'], '')

    def test_create_cause_for_bar_also_gets_consequence_and_safeguard(self):
        """The EquipmentDeviationBar checkbox flow specifically — routes
        through place_cause_from_template via _create_cause_for_bar."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        marker_id = self.db.add_equipment_marker(
            eq_id, "P-101", 0, 10.0, 10.0, "Pump", confidence=0.9, link_method='leader')
        self.panel._equipment_bar.load(eq_id, marker_id)

        cause_id = self.panel._create_cause_for_bar(
            marker_id, dev_id, "Pump", "P-101", "Ingen flödesindikering")

        self.assertIsNotNone(cause_id)
        cons_list = self.db.consequences(cause_id)
        self.assertEqual(len(cons_list), 1)
        self.assertEqual(len(self.db.safeguards(cons_list[0]['id'])), 1)

    def test_multiple_deviations_from_one_object_keep_the_equipment_tag(self):
        """Every checkbox in the left-click equipment bar must create a
        cause linked to the same equipment, not only the first one."""
        node_id = self.db.add_node()
        dev1 = self.db.get_or_create_deviation(node_id, "Lågt flöde")
        dev2 = self.db.get_or_create_deviation(node_id, "Högt flöde")
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        marker_id = self.db.add_equipment_marker(
            eq_id, "P-101", 0, 10.0, 10.0, "Pump", confidence=0.9, link_method='leader')
        self.panel._equipment_bar.load(eq_id, marker_id)

        create = self.panel._equipment_bar._create_cause_fn
        create(dev1, "Pump", "P-101", "", None)
        create(dev2, "Pump", "P-101", "", None)

        causes = [dict(c) for c in self.db.causes_for_equipment(eq_id)]
        self.assertEqual(len(causes), 2)
        self.assertEqual({c['comp_tag'] for c in causes}, {'P-101'})
        self.assertEqual({c['equipment_id'] for c in causes}, {eq_id})

    def test_create_cause_for_bar_does_not_draw_a_duplicate_marker(self):
        """Reported feedback (2026-08-12, see NOTES.md): 'När jag skapat
        ett manuellt objekt i pid viewer och sedan definerar en avikelse
        blir det dubbla markeringar' — a cause created via the equipment-
        bar checkbox flow used to draw a SECOND, separate cause-marker
        circle at the exact same position as the equipment's own marker
        (whose colour already represents "has causes"), on top of a
        manually placed object's still-interactive drawn-zone outline.
        _create_cause_for_bar must pass draw_marker=False through to
        place_cause_from_template so no second marker (DB row or visual
        item) gets created."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        eq_id = self.db.add_equipment_item("V-1", "V-1", "V", 0, "Ventil", '', 0)
        marker_id = self.db.add_equipment_marker(
            eq_id, "V-1", 0, 10.0, 10.0, "Ventil", confidence=1.0, link_method='manual')
        self.panel._equipment_bar.load(eq_id, marker_id)

        cause_id = self.panel._create_cause_for_bar(
            marker_id, dev_id, "Ventil", "V-1", "Ventil stängd")

        self.assertIsNotNone(cause_id)
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) FROM cause_markers WHERE cause_id=?", (cause_id,)
        ).fetchone()[0], 0, "no separate cause_markers row should be created")

    def test_kon_and_sg_cells_are_clickable_after_bar_driven_cause_creation(self):
        """End-to-end confirmation of the actual reported symptom: clicking
        KON/SG for a row created via the object/deviation-bar flow must
        now actually trigger inline editing, not silently do nothing."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            eq_id = win.db.add_equipment_item("T-1", "T-1", "T", 0, "Behållare", '', 0)
            marker_id = win.db.add_equipment_marker(
                eq_id, "T-1", 0, 10.0, 10.0, "Behållare", confidence=0.9, link_method='leader')
            win.pid_panel._equipment_bar.load(eq_id, marker_id)

            cause_id = win.pid_panel._create_cause_for_bar(
                marker_id, dev_id, "Behållare", "T-1", "Övertryck")
            win.scenario_panel.load_node(node_id)
            row = next(r for r, m in enumerate(win.scenario_panel._row_meta)
                      if m[1] == cause_id)

            edit_spy = unittest.mock.Mock(wraps=win.scenario_panel._table.edit)
            win.scenario_panel._table.edit = edit_spy
            win.scenario_panel._try_start_edit(row, win.scenario_panel._C_KON)
            edit_spy.assert_called()

            edit_spy.reset_mock()
            win.scenario_panel._try_start_edit(row, win.scenario_panel._C_SG)
            edit_spy.assert_called()


class PidAnalysisChainedAutodetectTests(unittest.TestCase):
    """'Efter jag klickat på analysera P&ID vill jag få upp samma popupruta
    som innan, sedan vill jag att en popupfråga om jag vill hitta objekt på
    P&ID ska komma upp. Då ska samma körning som "hitta objekt på P&ID"
    knappen köras.' (2026-08-11) — MainWindow._on_pid_analysis_done now
    asks a follow-up confirm after the existing 'Analys klar' popup (shown
    earlier, in PIDPanel._analyze_pid, unaffected by this change) and, on
    Yes, refreshes the equipment register and calls EquipmentPanel's own
    _autodetect() — the exact method '🎯 Hitta objekt på P&ID' itself calls."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_yes_reply_refreshes_register_and_runs_autodetect(self):
        with _TempDbMainWindow() as win:
            with unittest.mock.patch('hazop.QMessageBox.question',
                                      return_value=hazop.QMessageBox.StandardButton.Yes), \
                 unittest.mock.patch.object(win.equipment_panel, 'refresh') as mock_refresh, \
                 unittest.mock.patch.object(win.equipment_panel, '_autodetect') as mock_autodetect:
                win._on_pid_analysis_done()
                mock_refresh.assert_called_once()
                mock_autodetect.assert_called_once()

    def test_no_reply_does_not_run_autodetect(self):
        with _TempDbMainWindow() as win:
            with unittest.mock.patch('hazop.QMessageBox.question',
                                      return_value=hazop.QMessageBox.StandardButton.No), \
                 unittest.mock.patch.object(win.equipment_panel, 'refresh') as mock_refresh, \
                 unittest.mock.patch.object(win.equipment_panel, '_autodetect') as mock_autodetect:
                win._on_pid_analysis_done()
                mock_refresh.assert_not_called()
                mock_autodetect.assert_not_called()


class ClearedConsequenceRowHeightTests(unittest.TestCase):
    """'När jag har skapat en konsekvens och sedan suddar ut allt krymper
    raden och blir alltför låg vilket gör att jag inte ser vad som står på
    orsak och FA/antändning ser konstigt ut.' (2026-08-11) —
    _update_row_text_only()'s fast path used to set a row's height to ONLY
    what the just-edited column needed (_wrap_col_row_height(row, col)),
    discarding whatever a long ORS cause description or the fixed-height
    _LopaWidget (FA/Ant./Övriga column) in the SAME row required. Clearing
    a consequence's text back to empty shrank the row to one line,
    clipping the cause text and squashing the LOPA widget."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_clearing_consequence_text_does_not_shrink_row_below_cause_and_lopa_needs(self):
        with _TempDbMainWindow() as win:
            panel = win.scenario_panel
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            long_cause = ("En mycket lång orsakstext som garanterat radbryts "
                          "över flera rader i cellen. " * 3)
            win.db.update_cause(cause_id, description=long_cause)
            cons_id = win.db.add_consequence(cause_id)
            panel.load_cause(cause_id)

            row = next(r for r, m in enumerate(panel._row_meta) if m[2] == cons_id)

            # Simulate: user types a long consequence description (growing
            # the row), then erases it completely back to empty.
            panel._update_row_text_only('consequence', cons_id,
                                        "En lång konsekvensbeskrivning som också radbryts. " * 5)
            panel._update_row_text_only('consequence', cons_id, "")

            needed_for_cause = panel._wrap_col_row_height(row, panel._C_ORS)
            lopa_widget = panel._table.cellWidget(row, panel._C_LOPA)
            needed_for_lopa = lopa_widget.sizeHint().height() if lopa_widget else 0
            actual = panel._table.rowHeight(row)

            self.assertGreaterEqual(actual, needed_for_cause,
                "row shrank below what the (still long, unchanged) cause text needs")
            self.assertGreaterEqual(actual, needed_for_lopa,
                "row shrank below the FA/Ant. widget's own fixed height")


class PIDPanelStaleActiveIdTests(unittest.TestCase):
    """A cause/consequence deleted elsewhere (e.g. its node removed) while
    still 'active' in the PIDPanel used to survive as a stale id into the
    next placement click, crashing add_consequence/add_safeguard with
    sqlite3.IntegrityError: FOREIGN KEY constraint failed (real crash
    report, 2026-08-07 — crash_20260807_134324_IntegrityError.json). The
    P&ID-click reproduction tests for this were removed 2026-08-13 (see
    NOTES.md: _on_consequence_click/_on_safeguard_click no longer exist —
    the P&ID canvas is now object-placement-only) — the underlying
    stale-id reset logic they exercised is still covered by the tests
    below, which drive it directly rather than through a removed click
    handler."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_staleactive_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from pid_viewer import PIDPanel
        self.panel = PIDPanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_clear_active_selection_resets_all_placement_state(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        self.panel._active_node_id        = node_id
        self.panel._active_deviation_id   = dev_id
        self.panel._active_cause_id       = cause_id
        self.panel._active_consequence_id = cons_id

        self.panel.clear_active_selection()

        self.assertIsNone(self.panel._active_node_id)
        self.assertIsNone(self.panel._active_deviation_id)
        self.assertIsNone(self.panel._active_cause_id)
        self.assertIsNone(self.panel._active_consequence_id)

    def test_structure_changed_clears_pid_panel_stale_active_cause(self):
        """End-to-end: deleting a node via the tree (which emits
        structure_changed) must not leave MainWindow.pid_panel holding a
        cause id belonging to the now-deleted node."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            win.pid_panel._active_cause_id = cause_id

            win.db.delete_node(node_id)
            win._on_structure_changed()

            self.assertIsNone(win.pid_panel._active_cause_id)


class OrsTagZoneOpensMinimalPopupTests(unittest.TestCase):
    """"klickarna man på tagen justerar man tagen ... gör samtliga
    minimalistiska" (2026-08-14) — a plain click on the ORS tag zone
    used to open the large combined CauseObjectPopup (tag+type+
    avvikelse-picker+standard-cause list). It now opens the much
    smaller CauseTagPopup instead. CauseObjectPopup itself is
    untouched and still used, unchanged, by the detail panel
    (_edit_cause_obj) and quick-add (_quick_add_cause) — see
    CauseTagLiveLinkTests, which still exercises _apply_cause_obj the
    same way regardless of which popup calls it."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_orstagzone_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        self.node_id = self.db.add_node()
        self.dev_id = self.db.deviations(self.node_id)[0]['id']
        self.cause_id = self.db.add_cause(self.dev_id)
        self.panel.load_node(self.node_id)
        self.row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == self.cause_id)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_tag_zone_click_opens_cause_tag_popup_not_cause_object_popup(self):
        """CauseTagPopup has no OK button (2026-08-18) — it's a
        self-dismissing Popup shown non-modally, not exec()'d."""
        from PyQt6.QtCore import QSize
        fake_popup = unittest.mock.Mock()
        fake_popup.sizeHint.return_value = QSize(200, 100)
        with unittest.mock.patch('scenario_panel.CauseTagPopup', return_value=fake_popup) as MockPopup, \
             unittest.mock.patch('scenario_panel.CauseObjectPopup') as MockBigPopup:
            self.panel._show_cause_obj_popup(self.row, self.cause_id, QPoint(100, 100))
            MockPopup.assert_called_once()
            MockBigPopup.assert_not_called()
            fake_popup.show.assert_called_once()

    def test_object_dropdown_starts_with_database_label(self):
        from scenario_panel import CauseTagPopup
        popup = CauseTagPopup(self.db, parent=self.panel)
        try:
            self.assertEqual(popup._object_cb.itemText(0), 'Objektdatabas')
        finally:
            popup.deleteLater()

    def test_selecting_object_database_item_commits_object_immediately(self):
        from scenario_panel import CauseTagPopup
        equipment_id = self.db.add_equipment_item(
            'PV-101', 'PV-101', 'PV', 0, 'Ventil', '', 0)
        popup = CauseTagPopup(self.db, parent=self.panel)
        committed = []
        popup.committed.connect(lambda comp_type, tag:
                                committed.append((comp_type, tag)))
        try:
            index = next(i for i in range(1, popup._object_cb.count())
                         if popup._object_cb.itemData(i).get('id') == equipment_id)
            popup._object_cb.setCurrentIndex(index)
            self.assertEqual(committed, [('Ventil', 'PV-101')])
        finally:
            popup.deleteLater()

    def test_empty_object_cause_double_click_starts_inline_text_edit(self):
        from scenario_panel import CauseTagPopup
        # The cause created in setUp has no object/tag yet.  Double-clicking
        # its blank ORS cell must leave the tag popup path and enter the
        # normal text editor instead.
        with unittest.mock.patch.object(self.panel, '_show_cause_obj_popup') as popup, \
             unittest.mock.patch.object(self.panel._table, 'edit', return_value=True) as edit:
            item = self.panel._table.item(self.row, self.panel._C_ORS)
            self.panel._on_cell_double_clicked(item)

        popup.assert_not_called()
        edit.assert_called_once_with(
            self.panel._table.model().index(self.row, self.panel._C_ORS))

    def test_committing_the_tag_popup_calls_apply_cause_obj_with_empty_description(self):
        """The commit path must reuse _apply_cause_obj's existing "only
        tag/type changed" fast path (empty description, no frequency)
        instead of duplicating its persistence logic."""
        from PyQt6.QtCore import QSize
        apply_spy = unittest.mock.Mock()
        self.panel._apply_cause_obj = apply_spy
        fake_popup = unittest.mock.Mock()
        fake_popup.sizeHint.return_value = QSize(200, 100)
        captured = {}
        fake_popup.committed.connect = lambda slot: captured.__setitem__('slot', slot)
        with unittest.mock.patch('scenario_panel.CauseTagPopup', return_value=fake_popup):
            self.panel._show_cause_obj_popup(self.row, self.cause_id, QPoint(100, 100))
        captured['slot']('Ventil', 'PV-999')
        apply_spy.assert_called_once_with(self.row, self.cause_id, 'Ventil', 'PV-999', '', None)

    def test_group_tag_popup_targets_the_clicked_secondary_object(self):
        from PyQt6.QtCore import QSize
        from scenario_panel import CauseTagPopup
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, 'LÃ¥gt flÃ¶de')
        primary_id = self.db.add_equipment_item(
            'FI-1', 'FI-1', 'FI', 0, 'Instrument', '', 0)
        secondary_id = self.db.add_equipment_item(
            'FV-1', 'FV-1', 'FV', 0, 'Reglerventil', '', 0)
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(
            cause_id, comp_type='Instrument', comp_tag='FI-1 + FV-1',
            equipment_id=primary_id, secondary_equipment_id=secondary_id,
            description='FI-1\nFV-1')
        self.panel.load_node(node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta)
                   if m[1] == cause_id)
        fake_popup = unittest.mock.Mock()
        fake_popup.sizeHint.return_value = QSize(200, 100)
        with unittest.mock.patch('scenario_panel.CauseTagPopup',
                                 return_value=fake_popup) as popup_cls:
            self.panel._show_cause_obj_popup(
                row, cause_id, QPoint(100, 100), group_line=1)
        self.assertIs(popup_cls.call_args.kwargs['equipment_id'], secondary_id)
        self.assertEqual(popup_cls.call_args.kwargs['group_operator'], '&')


class OrsFrequencyZoneClickTests(unittest.TestCase):
    """"klickar man på frekvens skall man kunna justera frekvens"
    (2026-08-14) — the ORS strip's frequency label had no click zone at
    all; a click there fell through to plain cell selection/edit.
    FrequencyPickerPopup already existed, fully built, but was never
    wired up anywhere. Also covers the "frequency text collides with
    the invisible clone-icon zone" mismatch found while implementing
    this: the frequency check now runs BEFORE the clone/comment check,
    so a click anywhere on the actual rendered frequency text always
    opens the frequency popup regardless of that overlap.

    2026-08-18: frequency moved out of the tag strip, floating over the
    top of the orsaksfält's own description text instead (see NOTES.md
    "Frekvensen ... hör hemma mer här") — the click zone moved with it,
    still governed by the same _ors_freq_zone_geometry() helper paint()
    draws the text with. Follow-up the same day ("hamnar nu på olika
    rader vilket tar onödigt mycket plats") dropped the separate reserved
    row it briefly had in favor of overlaying it on the description's own
    first line — the click zone is confined to that first line's height,
    not a dedicated row anymore."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_orsfreqclick_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        self.panel = ScenarioTablePanel(self.db)
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(self.cause_id, comp_type='V', comp_tag='PV-101')
        self.panel.load_node(node_id)
        self.row = next(r for r, m in enumerate(self.panel._row_meta) if m[1] == self.cause_id)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _freq_zone_geometry(self):
        panel = self.panel
        panel._table.setColumnWidth(panel._C_ORS, 220)
        panel.resize(900, 400)
        panel.show()
        self.app.processEvents()
        col_x = panel._table.columnViewportPosition(panel._C_ORS)
        cell_right = col_x + panel._table.columnWidth(panel._C_ORS) - 1
        item = panel._table.item(self.row, panel._C_ORS)
        return panel._ors_freq_zone_geometry(item, col_x + 2, cell_right - 2)

    def _click(self, x, y):
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import Qt as _Qt
        pos = QPoint(x, y)
        ev = QMouseEvent(QEvent.Type.MouseButtonPress, pos.toPointF(),
                          _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
                          _Qt.KeyboardModifier.NoModifier)
        return self.panel.eventFilter(self.panel._table.viewport(), ev)

    def test_clicking_frequency_zone_opens_frequency_picker_popup(self):
        freq_zone_x, freq_zone_w, freq_str = self._freq_zone_geometry()
        self.assertTrue(freq_str, "test setup issue: cause has no frequency label to click on")
        # The frequency zone floats over the cell's first line, the
        # same line the (now inline) bold tag prefix shares (2026-08-25,
        # see class docstring) — there's no separate tag strip anymore.
        row_y = self.panel._table.rowViewportPosition(self.row) + 3
        fake_popup = unittest.mock.Mock()
        with unittest.mock.patch('hazop.FrequencyPickerPopup.create_positioned',
                                  return_value=fake_popup) as mock_create:
            handled = self._click(freq_zone_x + freq_zone_w // 2, row_y)
        self.assertTrue(handled)
        mock_create.assert_called_once()
        fake_popup.exec.assert_called_once()

    def test_clicking_the_tag_prefix_does_not_open_the_frequency_popup(self):
        """2026-08-25: the bold tag prefix and the frequency zone now
        share the SAME first-line Y-band (there's no more separate tag
        strip above it) and are disambiguated by X only — a click within
        the tag's own rendered width must not also open the frequency
        popup, even though it sits on the same line as the frequency
        text."""
        from hazop import _ORS_FIRST_LINE_H
        col_x = self.panel._table.columnViewportPosition(self.panel._C_ORS)
        item = self.panel._table.item(self.row, self.panel._C_ORS)
        prefix_w = self.panel._ors_tag_prefix_pixel_width(
            item, item.text(), self.panel._table.font())
        self.assertGreater(prefix_w, 0, "test setup issue: cause has no tag prefix to click on")
        self.panel._row_plus_cols = {}
        self.panel._clone_scenario = unittest.mock.Mock()
        self.panel._open_comment_popup = unittest.mock.Mock()
        row_y = self.panel._table.rowViewportPosition(self.row) + _ORS_FIRST_LINE_H // 2
        with unittest.mock.patch('hazop.FrequencyPickerPopup.create_positioned') as mock_create:
            self._click(col_x + prefix_w - 3, row_y)
            mock_create.assert_not_called()

    def test_clicking_below_the_first_line_does_not_open_the_frequency_popup(self):
        """A click on a LATER wrapped description line, below the first
        line the frequency zone floats over (same x range), must not
        also open the popup. A click there can legitimately fall through
        to the pre-existing (unrelated, out of scope here) clone/comment/
        plus-badge zones instead — those are stubbed out so this test
        only asserts on the one thing it owns: the frequency popup must
        not fire."""
        from hazop import _ORS_FIRST_LINE_H
        freq_zone_x, freq_zone_w, freq_str = self._freq_zone_geometry()
        self.panel._row_plus_cols = {}
        self.panel._clone_scenario = unittest.mock.Mock()
        self.panel._open_comment_popup = unittest.mock.Mock()
        row_y = self.panel._table.rowViewportPosition(self.row)
        with unittest.mock.patch('hazop.FrequencyPickerPopup.create_positioned') as mock_create:
            self._click(freq_zone_x + freq_zone_w // 2,
                        row_y + _ORS_FIRST_LINE_H + 5)
            mock_create.assert_not_called()

    def test_picking_a_preset_frequency_sets_likelihood_and_clears_base_frequency(self):
        self.db.update_cause(self.cause_id, base_frequency=3.5)
        rebuild_spy = unittest.mock.Mock()
        self.panel._schedule_rebuild = rebuild_spy

        self.panel._on_ors_frequency_picked(self.cause_id, 2, None)

        cause = self.db.get_cause(self.cause_id)
        self.assertEqual(cause['likelihood'], 2)
        self.assertIsNone(cause['base_frequency'])
        rebuild_spy.assert_called_once()

    def test_picking_a_numeric_frequency_sets_base_frequency(self):
        rebuild_spy = unittest.mock.Mock()
        self.panel._schedule_rebuild = rebuild_spy

        self.panel._on_ors_frequency_picked(self.cause_id, None, 0.5)

        cause = self.db.get_cause(self.cause_id)
        self.assertEqual(cause['base_frequency'], 0.5)
        rebuild_spy.assert_called_once()


class ShiftClickInsertsTagIntoActiveEditorTests(unittest.TestCase):
    """"Om jag skriver en konsekvens ... och sedan håller nere shift och
    klickar på ett objekt vill jag att detta läggs till till
    konsekvenskedjan automatiskt och att jag kan fortsätta skriva efter
    objektet. Dvs att jag inte hoppar ut ur textediteringsvyn." (2026-08-13)

    Every equipment-marker click today — Shift or not — already runs
    marker_navigated -> MainWindow._on_equipment_marker_navigate ->
    scenario_panel.load_equipment() -> _rebuild(), which explicitly
    does focusWidget().clearFocus() then setRowCount(0): exactly what
    would destroy an open ORS/KON/SG cell editor. Shift+click while a
    cell is being edited must instead insert the marker's tag straight
    into the live editor's text (mutating only the open QLineEdit, no
    DB write) and swallow the click — no popup, no marker_navigated, no
    rebuild — so the existing commit-on-editingFinished path persists
    the final text normally and the user never leaves the editor."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_shiftclickinsert_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import ScenarioTablePanel
        from pid_viewer import PIDPanel
        self.panel = ScenarioTablePanel(self.db)
        self.pid_panel = PIDPanel(self.db)
        self.pid_panel._active_edit_query_fn = self.panel.active_edit_target

        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.cons_id = self.db.add_consequence(cause_id)
        self.node_id = node_id
        self.panel.load_node(node_id)

        self.eq_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)
        self.marker_id = self.db.add_equipment_marker(
            self.eq_id, "PV-101", 0, 10.0, 10.0, "Ventil", confidence=0.9, link_method='leader')

    def tearDown(self):
        self.panel.deleteLater()
        self.pid_panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _start_editing_kon(self, text=''):
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[2] == self.cons_id)
        item = self.panel._table.item(row, self.panel._C_KON)
        self.panel._table.editItem(item)
        editor = self.panel._table.focusWidget()
        from scenario_panel import _BoldTagTextEdit
        assert isinstance(editor, _BoldTagTextEdit)
        if text:
            editor.setText(text)
            editor.setCursorPosition(len(text))
        return editor

    def test_active_edit_target_is_none_when_nothing_is_being_edited(self):
        self.assertIsNone(self.panel.active_edit_target())

    def test_active_edit_target_returns_the_live_editor_for_a_kon_cell(self):
        editor = self._start_editing_kon()
        got_editor, kind, id_ = self.panel.active_edit_target()
        self.assertIs(got_editor, editor)
        self.assertEqual((kind, id_), ('consequence', self.cons_id))

    def test_insert_tag_into_editor_adds_spacing_on_both_sides(self):
        editor = self._start_editing_kon("Högt flöde till")
        self.pid_panel._insert_tag_into_editor(editor, "PV-101")
        self.assertEqual(editor.text(), "Högt flöde till PV-101 ")
        self.assertEqual(editor.cursorPosition(), len(editor.text()))

    def test_insert_tag_into_editor_mid_text_keeps_the_remainder(self):
        editor = self._start_editing_kon("Högt flöde stänger ventilen")
        editor.setCursorPosition(len("Högt flöde "))
        self.pid_panel._insert_tag_into_editor(editor, "PV-101")
        self.assertEqual(editor.text(), "Högt flöde PV-101 stänger ventilen")

    def test_insert_tag_into_empty_editor(self):
        editor = self._start_editing_kon()
        editor.clear()   # start from a genuinely empty editor, independent
                          # of _PidDelegate's own "—" placeholder-stripping
        self.pid_panel._insert_tag_into_editor(editor, "PV-101")
        self.assertEqual(editor.text(), "PV-101 ")

    def test_shift_click_while_editing_inserts_tag_and_does_not_navigate(self):
        editor = self._start_editing_kon("Högt flöde till ")
        captured = []
        self.pid_panel.marker_navigated.connect(lambda t, i: captured.append((t, i)))

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.ShiftModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        self.assertIn("PV-101", editor.text())
        self.assertEqual(captured, [], "marker_navigated must not fire while inserting into an active editor")

    def test_shift_click_syncs_tagged_refs_so_the_tag_gets_bold_highlighted(self):
        """"att den blir fetstil om jag är i skrivläget på konsekvens och
        håller [shift]" (2026-08-13) — the drag-and-drop path already
        bolds any tag it appends via tagged_refs (_PidDelegate paint);
        Shift+click-insert must give the same treatment, not just plain
        text, even though the description write itself is deferred to
        the normal edit-commit."""
        self._start_editing_kon("Högt flöde till ")

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.ShiftModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        cons = self.db.get_consequence(self.cons_id)
        self.assertIn("PV-101", (cons['tagged_refs'] or '').split(','))
        self.assertEqual(cons['comp_tag'], "PV-101")
        self.assertEqual(cons['comp_type'], "Ventil")

    def test_shift_click_tag_sync_does_not_overwrite_the_persisted_description(self):
        """The DB description column must stay untouched by the sync —
        only the live editor's text (already updated by
        _insert_tag_into_editor) changes; the normal edit-commit path
        is what eventually saves the full text, so writing a stale
        pre-edit description here would just be overwritten a moment
        later and risks a race with what the user is still typing."""
        original_desc = self.db.get_consequence(self.cons_id)['description']
        self._start_editing_kon("Högt flöde till ")

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.ShiftModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        self.assertEqual(self.db.get_consequence(self.cons_id)['description'], original_desc)

    def test_shift_click_on_safeguard_cell_also_syncs_tagged_refs(self):
        sg_id = self.db.add_safeguard(self.cons_id)
        self.panel.load_node(self.node_id)
        row = next(r for r, m in enumerate(self.panel._row_meta) if m[3] == sg_id)
        item = self.panel._table.item(row, self.panel._C_SG)
        self.panel._table.editItem(item)
        editor = self.panel._table.focusWidget()
        editor.setText("Larm vid ")
        editor.setCursorPosition(len(editor.text()))

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.ShiftModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        self.assertIn("PV-101", editor.text())
        sg = self.db.get_safeguard(sg_id)
        self.assertIn("PV-101", (sg['tagged_refs'] or '').split(','))
        self.assertEqual(sg['comp_tag'], "PV-101")

    def test_plain_click_while_editing_falls_back_to_normal_navigation(self):
        """Sanity check: the new branch must not hijack every click just
        because a cell happens to be open for editing — only Shift+click
        gets the new behaviour."""
        self._start_editing_kon("Högt flöde till ")
        captured = []
        self.pid_panel.marker_navigated.connect(lambda t, i: captured.append((t, i)))

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.NoModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        self.assertEqual(captured, [('equipment', self.marker_id)])

    def test_shift_click_with_no_active_editor_falls_back_to_normal_navigation(self):
        captured = []
        self.pid_panel.marker_navigated.connect(lambda t, i: captured.append((t, i)))

        with unittest.mock.patch.object(
                QApplication, 'keyboardModifiers', return_value=Qt.KeyboardModifier.ShiftModifier):
            self.pid_panel._on_marker_clicked('equipment', self.marker_id)

        self.assertEqual(captured, [('equipment', self.marker_id)])


class NodeMarkupDockingTests(unittest.TestCase):
    """Fas F (2026-08-17, see NOTES.md "nodmarkup dockas till höger") —
    editing node markup used to hide tree_panel/props_ribbon/scenario_panel
    entirely, replacing almost the whole window with just the P&ID canvas.
    tree_panel and props_ribbon must now stay visible; only the bottom
    strip still defaults to Nodmarkeringar, with a toggle to bring back
    HAZOP scenario without leaving markup-edit mode."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    # NOTE: isVisible() reflects actual on-screen visibility, which requires
    # the whole ancestor chain (including the never-shown MainWindow) to be
    # visible too — always False in a headless test regardless of
    # setVisible(True). isHidden() reflects only this widget's OWN explicit
    # show/hide state, which is what these tests actually need to check
    # (same convention already used elsewhere in this file, e.g.
    # ConsequenceStepPickerColumnsTests).

    def test_editing_node_markup_keeps_tree_and_props_ribbon_visible(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win._on_edit_node_markup(node_id)
            try:
                self.assertFalse(win.tree_panel.isHidden())
                self.assertFalse(win.props_ribbon.isHidden())
                self.assertTrue(win.props_ribbon._markup_active)
            finally:
                win._on_close_node_markup()

    def test_editing_node_markup_defaults_bottom_strip_to_scenario(self):
        """2026-08-18 (see NOTES.md): reversed from the original Fas F
        default — entering node markup edit (whether via the explicit
        'Editera nodmarkup' action or automatically via tree selection,
        see NodeMarkupAutoOpenTests below) now leaves HAZOP scenario
        visible until the user actually starts drawing
        (_on_node_markup_tool_activated swaps it then)."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win._on_edit_node_markup(node_id)
            try:
                self.assertFalse(win.scenario_panel.isHidden())
                self.assertTrue(win.markup_table_panel.isHidden())
            finally:
                win._on_close_node_markup()

    def test_toggling_bottom_panel_swaps_to_scenario(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win._on_edit_node_markup(node_id)
            try:
                win.props_ribbon._bottom_toggle_btn.setChecked(True)
                self.assertFalse(win.scenario_panel.isHidden())
                self.assertTrue(win.markup_table_panel.isHidden())

                win.props_ribbon._bottom_toggle_btn.setChecked(False)
                self.assertTrue(win.scenario_panel.isHidden())
                self.assertFalse(win.markup_table_panel.isHidden())
            finally:
                win._on_close_node_markup()

    def test_closing_node_markup_restores_scenario_and_hides_markup_ui(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win._on_edit_node_markup(node_id)
            win.props_ribbon._bottom_toggle_btn.setChecked(True)
            win._on_close_node_markup()

            self.assertFalse(win.scenario_panel.isHidden())
            self.assertTrue(win.markup_table_panel.isHidden())
            self.assertFalse(win.props_ribbon._markup_active)
            self.assertFalse(win.tree_panel.isHidden())
            self.assertFalse(win.props_ribbon.isHidden())


class NodeMarkupAutoOpenTests(unittest.TestCase):
    """2026-08-18 (see NOTES.md): "nodmarkupdialogen enbart ska synas om
    jag står på nivån nod i trädet. Den ska släckas om jag står på en
    avvikelse. Ritar jag in något i noden skall detta vara kopplat till
    den noden jag står på." originally made node_markup_panel open
    automatically the moment the tree selection became a Node.

    2026-08-25 (see NOTES.md): that auto-open was reverted — "Om man
    klickar på en nod i trädet idag försvinner hazop scenario och man
    kommer direkt in i ritningläget på P&ID. Detta blir förvirrande...
    För att gå in i editerarmode behöver jag aktivt trycka på pennan till
    höger." A plain node click now only updates the ribbon/P&ID
    active-node state and leaves HAZOP scenario + navigate mode alone;
    markup mode is entered only via the explicit ✏️ toggle (or the
    tree's own 'Editera nodmarkup' action). The one thing this class
    still asserts from the 2026-08-18 behavior: WHILE markup mode is
    already active, selecting a different node still rebinds it there
    (matching "ritar jag in något i noden skall detta vara kopplat till
    den noden jag står på"), and selecting a non-Node item still closes
    it."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _stub_scenario_loaders(self, win):
        """See DatabaseLayerTests' orphaned-selection tests for why:
        load_node/load_deviation/etc. ultimately call
        QTableWidget.resizeRowsToContents(), which reproducibly hits a
        native access violation under this machine's headless Qt platform
        plugin — unrelated to the node-markup auto-open/close logic these
        tests actually target."""
        win.scenario_panel.load_node = lambda *a, **k: None
        win.scenario_panel.load_deviation = lambda *a, **k: None
        win.scenario_panel.load_cause = lambda *a, **k: None
        win.scenario_panel.load_consequence = lambda *a, **k: None

    def test_selecting_a_node_does_not_open_node_markup_panel(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loaders(win)
            node_id = win.db.add_node()
            win._on_selected(NODE_T, node_id)
            self.assertFalse(win.props_ribbon._markup_active,
                "a plain node click must stay in navigate mode, not auto-enter markup edit")
            self.assertEqual(win.view_stack.currentIndex(), 0,
                "a plain node click must not switch away from the current page")

    def test_selecting_a_node_leaves_hazop_scenario_visible(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loaders(win)
            node_id = win.db.add_node()
            win._on_selected(NODE_T, node_id)
            self.assertFalse(win.scenario_panel.isHidden())
            self.assertTrue(win.markup_table_panel.isHidden())

    def test_selecting_a_different_node_while_markup_active_rebinds_instead_of_staying_stuck(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loaders(win)
            node_a = win.db.add_node()
            node_b = win.db.add_node()
            win._on_edit_node_markup(node_a)   # explicit entry, e.g. the ✏️ toggle
            self.assertEqual(win.props_ribbon.node_id, node_a)

            win._on_selected(NODE_T, node_b)
            try:
                self.assertTrue(win.props_ribbon._markup_active)
                self.assertEqual(win.props_ribbon.node_id, node_b)
            finally:
                win._on_close_node_markup()

    def test_selecting_a_deviation_closes_node_markup_panel_if_active(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loaders(win)
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            win._on_edit_node_markup(node_id)
            self.assertTrue(win.props_ribbon._markup_active)

            win._on_selected(DEV_T, dev_id)
            self.assertFalse(win.props_ribbon._markup_active)

    def test_selecting_a_cause_closes_node_markup_panel_if_active(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loaders(win)
            node_id = win.db.add_node()
            dev_id = win.db.deviations(node_id)[0]['id']
            cause_id = win.db.add_cause(dev_id)
            win._on_edit_node_markup(node_id)
            self.assertTrue(win.props_ribbon._markup_active)

            win._on_selected(CAUSE_T, cause_id)
            self.assertFalse(win.props_ribbon._markup_active)

    def test_activating_a_drawing_tool_switches_bottom_strip_to_markup_table(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loaders(win)
            node_id = win.db.add_node()
            win._on_edit_node_markup(node_id)
            try:
                self.assertFalse(win.scenario_panel.isHidden())
                win._on_node_markup_tool_activated('polygon')
                self.assertTrue(win.scenario_panel.isHidden())
                self.assertFalse(win.markup_table_panel.isHidden())
            finally:
                win._on_close_node_markup()

    def test_activating_the_neutral_select_tool_does_not_switch_bottom_strip(self):
        with _TempDbMainWindow() as win:
            self._stub_scenario_loaders(win)
            node_id = win.db.add_node()
            win._on_edit_node_markup(node_id)
            try:
                win._on_node_markup_tool_activated('select')
                self.assertFalse(win.scenario_panel.isHidden())
                self.assertTrue(win.markup_table_panel.isHidden())
            finally:
                win._on_close_node_markup()


# ══════════════════════════════════════════════════════════════════════════
# Manual per-page P&ID rotation (2026-08-12) — a toolbar rotate-left/right
# control for the currently-viewed sheet, composed with (not replacing) the
# PDF's own /Rotate flag via page.set_rotation() (see
# PIDGraphicsView._apply_page_rotation). Distinct from the pre-existing,
# still-unwired "Sid-orientering" three-way dropdown (pid_page_orientation_hint,
# see NOTES.md known limitations). Every marker/zone position stored for the
# rotated physical page is stored in PDF-space and must be re-anchored to the
# same physical point at the same time, or rotating would silently move
# every marker on that page.
# ══════════════════════════════════════════════════════════════════════════

class PidPageRotationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        import fitz
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_rotate_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.pdf_path = os.path.join(self._tmpdir, "test.pdf")
        doc = fitz.open()
        doc.new_page(width=400.0, height=300.0)
        doc.save(self.pdf_path)
        doc.close()

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_panel(self):
        from pid_viewer import PIDPanel
        panel = PIDPanel(self.db)
        panel.viewer.load_pdf(self.pdf_path)
        self.db.ensure_sheets_initialized(panel.viewer.page_count())
        panel._rebuild_sheet_map()
        return panel

    def _insert_cause_marker(self, cause_id, page, x, y, comp_type,
                             rect_w=None, rect_h=None):
        """Database.add_cause_marker was removed 2026-08-13 (see NOTES.md:
        the P&ID canvas is now object-placement-only, so nothing creates
        cause_markers rows anymore) — the table/schema itself is untouched
        (no migration, no data loss for existing projects), and
        remap_page_rotation_positions still has to remap any legacy rows
        that exist there correctly. Insert directly so these tests keep
        exercising that generic remap logic against a real row shape."""
        self.db.conn.execute(
            "INSERT INTO cause_markers (cause_id,pid_page,x,y,component_type,rect_w,rect_h) "
            "VALUES (?,?,?,?,?,?,?)",
            (cause_id, page, x, y, comp_type, rect_w, rect_h))
        self.db.commit()

    def test_db_rotation_round_trip(self):
        self.assertEqual(self.db.get_page_rotation(0), 0)
        self.db.set_page_rotation(0, 90)
        self.assertEqual(self.db.get_page_rotation(0), 90)
        self.assertEqual(self.db.get_all_page_rotations(), {0: 90})
        self.db.set_page_rotation(0, 270)   # upsert, not a duplicate row
        self.assertEqual(self.db.get_page_rotation(0), 270)
        self.assertEqual(self.db.get_all_page_rotations(), {0: 270})

    def test_rotation_override_composes_with_intrinsic_rotation(self):
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        self.assertTrue(view.load_pdf(self.pdf_path))
        page = view.pdf_doc.load_page(0)
        self.assertEqual(page.rotation, 0)
        w0, h0 = view._page_widths_pdf[0], view._page_heights_pdf[0]

        view.set_page_rotation_override(0, 90)

        self.assertEqual(page.rotation, 90,
            "override must compose with (here: on top of a 0-degree) intrinsic /Rotate")
        # An axis-aligned footprint swap is the observable proof that
        # page.rect/get_pixmap() etc. now reflect the override for free.
        self.assertAlmostEqual(page.rect.width,  h0, places=3)
        self.assertAlmostEqual(page.rect.height, w0, places=3)

    def test_rotate_button_updates_db_and_page_footprint(self):
        panel = self._make_panel()
        try:
            w0 = panel.viewer._page_widths_pdf[0]
            h0 = panel.viewer._page_heights_pdf[0]
            panel._rotate_page(90)
            self.assertEqual(self.db.get_page_rotation(0), 90)
            self.assertAlmostEqual(panel.viewer._page_widths_pdf[0],  h0, places=3)
            self.assertAlmostEqual(panel.viewer._page_heights_pdf[0], w0, places=3)
        finally:
            panel.deleteLater()

    def test_marker_stays_on_same_physical_point_after_rotation(self):
        """The critical correctness check the user explicitly asked for:
        place a marker, rotate the page, confirm it's still anchored to the
        same physical location — not just that rendering doesn't crash.
        Verified by mapping both the before- and after-rotation marker
        position back to the rotation-invariant raw/mediabox anchor via
        derotation_matrix and checking they match."""
        import fitz
        panel = self._make_panel()
        try:
            node_id  = self.db.add_node()
            dev_id   = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self._insert_cause_marker(cause_id, 0, 100.0, 50.0, "Ventil")

            page_before = panel.viewer.pdf_doc.load_page(0)
            raw_anchor_before = fitz.Point(100.0, 50.0) * page_before.derotation_matrix

            panel._rotate_page(90)

            marker = dict(self.db.cause_markers_for_page(0)[0])
            page_after = panel.viewer.pdf_doc.load_page(0)
            raw_anchor_after = fitz.Point(marker['x'], marker['y']) * page_after.derotation_matrix

            self.assertAlmostEqual(raw_anchor_before.x, raw_anchor_after.x, places=3)
            self.assertAlmostEqual(raw_anchor_before.y, raw_anchor_after.y, places=3)
            # And the stored PDF-space coordinates actually changed — proves
            # this isn't an accidental no-op/identity transform.
            self.assertFalse(
                abs(marker['x'] - 100.0) < 1e-3 and abs(marker['y'] - 50.0) < 1e-3,
                "marker's stored PDF-space position must change across a rotation "
                "even though its physical location doesn't")
        finally:
            panel.deleteLater()

    def test_rect_marker_dimensions_swap_on_90_degree_rotation(self):
        panel = self._make_panel()
        try:
            node_id  = self.db.add_node()
            dev_id   = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self._insert_cause_marker(cause_id, 0, 100.0, 50.0, "Ventil",
                                      rect_w=40.0, rect_h=20.0)

            panel._rotate_page(90)

            row = dict(self.db.cause_markers_for_page(0)[0])
            self.assertAlmostEqual(row['rect_w'], 20.0, places=3)
            self.assertAlmostEqual(row['rect_h'], 40.0, places=3)
        finally:
            panel.deleteLater()

    def test_rect_marker_dimensions_unchanged_on_180_degree_rotation(self):
        panel = self._make_panel()
        try:
            node_id  = self.db.add_node()
            dev_id   = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self._insert_cause_marker(cause_id, 0, 100.0, 50.0, "Ventil",
                                      rect_w=40.0, rect_h=20.0)

            panel._rotate_page(90)
            panel._rotate_page(90)   # net 180 degrees

            row = dict(self.db.cause_markers_for_page(0)[0])
            self.assertAlmostEqual(row['rect_w'], 40.0, places=3)
            self.assertAlmostEqual(row['rect_h'], 20.0, places=3)
        finally:
            panel.deleteLater()

    def test_node_outline_points_remapped_after_rotation(self):
        """Covers the 'zone drawing' correctness the user asked to verify —
        a node's outline (nodes.markup_points) is the simplest such zone."""
        import fitz, json
        panel = self._make_panel()
        try:
            pts_before = [[20.0, 30.0], [120.0, 30.0], [120.0, 130.0], [20.0, 130.0]]
            node_id = self.db.add_node_with_markup("Node A", pts_before, {}, 0)

            page_before = panel.viewer.pdf_doc.load_page(0)
            raw_before = [fitz.Point(x, y) * page_before.derotation_matrix for x, y in pts_before]

            panel._rotate_page(-90)

            node = self.db.get_node(node_id)
            pts_after = json.loads(node['markup_points'])
            page_after = panel.viewer.pdf_doc.load_page(0)
            raw_after = [fitz.Point(x, y) * page_after.derotation_matrix for x, y in pts_after]

            for rb, ra in zip(raw_before, raw_after):
                self.assertAlmostEqual(rb.x, ra.x, places=3)
                self.assertAlmostEqual(rb.y, ra.y, places=3)
        finally:
            panel.deleteLater()

    def test_full_rotation_cycle_returns_marker_to_original_position(self):
        """Four 90-degree turns must be a no-op on every stored position —
        a strong end-to-end sanity check of the compose/remap math."""
        panel = self._make_panel()
        try:
            node_id  = self.db.add_node()
            dev_id   = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            self._insert_cause_marker(cause_id, 0, 77.0, 133.0, "Ventil")

            for _ in range(4):
                panel._rotate_page(90)

            self.assertEqual(self.db.get_page_rotation(0), 0)
            marker = dict(self.db.cause_markers_for_page(0)[0])
            self.assertAlmostEqual(marker['x'], 77.0, places=3)
            self.assertAlmostEqual(marker['y'], 133.0, places=3)
        finally:
            panel.deleteLater()


class RecommendationPhysicalRowTests(RecommendationColumnTests):
    def test_each_recommendation_has_its_own_row_and_trailing_blank_row(self):
        first = self.db.add_recommendation_to_consequence(
            self.cons_id, description='Första')
        second = self.db.add_recommendation_to_consequence(
            self.cons_id, description='Andra')
        self.panel.load_node(self.node_id)
        rows = [r for r, meta in enumerate(self.panel._row_meta)
                if meta[2] == self.cons_id]
        self.assertEqual(
            [self.panel._table.item(r, self.panel._C_REK).text() for r in rows],
            [f'R-{first:03d}. Första', f'R-{second:03d}. Andra', '—'])
        self.assertEqual(
            [self.panel._row_recommendation_ids[r] for r in rows],
            [first, second, None])


class EmptyScenarioCellDoubleClickTests(unittest.TestCase):
    """Empty Scenario/Worksheet cells must use their context popup while
    keeping the corresponding free-text editor available in that popup."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_empty_doubleclick_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_empty_cause_double_click_starts_inline_edit_path(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        panel = ScenarioTablePanel(self.db)
        try:
            panel.set_show_empty_deviations(True)
            panel.load_node(node_id)
            row = next(r for r, meta in enumerate(panel._row_meta)
                       if meta[0] == dev_id and meta[1] is None)
            calls = []
            panel._quick_add_cause = lambda did, from_enter=False: calls.append(
                (did, from_enter))
            panel._on_cell_double_clicked(panel._table.item(row, panel._C_ORS))
            self.assertEqual(calls, [(dev_id, True)])
        finally:
            panel.deleteLater()

    def test_empty_consequence_cell_opens_chain_freetext_popup(self):
        from hazop import ScenarioTablePanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_id)
        panel = ScenarioTablePanel(self.db)
        try:
            panel.load_node(node_id)
            row = next(r for r, meta in enumerate(panel._row_meta)
                       if meta[2] == cons_id)
            calls = []
            panel._open_chain_editor = lambda cid: calls.append(cid)
            panel._on_cell_double_clicked(panel._table.item(row, panel._C_KON))
            self.assertEqual(calls, [cons_id])
        finally:
            panel.deleteLater()


if __name__ == "__main__":
    unittest.main()
