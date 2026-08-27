#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering node_markup.py, plus any cross-module glue they
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

class NodeMarkupPanelNavigateTests(unittest.TestCase):
    """The node-markup toolbar's prev/next node buttons (⬆/⬇) crashed with
    TypeError: 'method' object is not iterable — _navigate_prev/
    _navigate_next read `self.db.nodes` (the bound method itself) instead
    of calling `self.db.nodes()` (2026-08-11 crash reports,
    crash_20260811_162420/162424_TypeError.json). The toolbar itself was
    a separate NodeMarkupPanel widget at the time; merged into
    PropertiesRibbon 2026-08-19 (see NOTES.md "Slå ihop nodmarkup i
    nodinställningar") — same methods, now on the merged ribbon."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_navtest_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        # 2026-08-24: a fresh Database now auto-seeds one default node (see
        # Database.__init__'s pre_existing_db check) — these tests assert
        # exact prev/next behavior against their OWN controlled node
        # ordering, so remove the auto-seeded one first.
        for n in self.db.nodes():
            self.db.delete_node(n['id'])

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_panel(self, node_id):
        from hazop import PropertiesRibbon
        panel = PropertiesRibbon(self.db)
        panel.set_item(NODE_T, node_id)
        panel.enter_markup_mode(node_id)
        return panel

    def test_navigate_prev_emits_previous_node_id(self):
        node_a = self.db.add_node()
        node_b = self.db.add_node()
        panel = self._make_panel(node_b)
        try:
            seen = []
            panel.navigate_node_requested.connect(seen.append)
            panel._navigate_prev()
            self.assertEqual(seen, [node_a])
        finally:
            panel.deleteLater()

    def test_bottom_toggle_button_emits_signal(self):
        panel = self._make_panel(self.db.add_node())
        try:
            seen = []
            panel.bottom_panel_toggled.connect(seen.append)
            panel._bottom_toggle_btn.setChecked(True)
            self.assertEqual(seen, [True])
        finally:
            panel.deleteLater()

    def test_set_bottom_toggle_checked_does_not_emit(self):
        """Programmatic reset (entering markup-edit mode) must not
        re-trigger MainWindow's own visibility-swap handler."""
        panel = self._make_panel(self.db.add_node())
        try:
            seen = []
            panel.bottom_panel_toggled.connect(seen.append)
            panel.set_bottom_toggle_checked(True)
            self.assertEqual(seen, [])
            self.assertTrue(panel._bottom_toggle_btn.isChecked())
        finally:
            panel.deleteLater()

    def test_navigate_next_emits_next_node_id(self):
        node_a = self.db.add_node()
        node_b = self.db.add_node()
        panel = self._make_panel(node_a)
        try:
            seen = []
            panel.navigate_node_requested.connect(seen.append)
            panel._navigate_next()
            self.assertEqual(seen, [node_b])
        finally:
            panel.deleteLater()

    def test_navigate_prev_at_first_node_is_noop(self):
        node_a = self.db.add_node()
        self.db.add_node()
        panel = self._make_panel(node_a)
        try:
            seen = []
            panel.navigate_node_requested.connect(seen.append)
            panel._navigate_prev()
            self.assertEqual(seen, [])
        finally:
            panel.deleteLater()


class RedMarkupConsolidationTests(unittest.TestCase):
    """Fas F del 2 (2026-08-17, see NOTES.md "Red markup konsolideras") —
    "Skrota allt utom 'Välj P&ID-symbol', flytta in i nodmarkup-panelen."
    RedMarkupPanel keeps only Välj/flytta (needed to select an
    already-placed symbol for size/rotation editing) + Lägg ut
    P&ID-symbol; NodeMarkupPanel gets a new button that's the sole entry
    point now (the tree's own "Editera redmarkup" context-menu action is
    gone). The two edit-mode state machines stay technically separate —
    placing a symbol briefly switches into red-markup mode and back."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_redmarkup_consolidation_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_red_markup_panel_only_has_select_and_symbol_tools(self):
        from hazop import RedMarkupPanel
        panel = RedMarkupPanel(self.db)
        try:
            self.assertEqual(set(panel._tool_btns.keys()), {'select', 'symbol'})
            self.assertFalse(hasattr(panel, '_all_vis_btn'))
            self.assertFalse(hasattr(panel, '_color_strip'))
        finally:
            panel.deleteLater()

    def test_tree_context_menu_no_longer_offers_editera_redmarkup(self):
        db = self.db
        panel = TreePanel(db)
        try:
            node_id = db.add_node()
            panel.refresh()
            item = _find_tree_item(panel.tree, NODE_T, node_id)
            self.assertIsNotNone(item)
            with unittest.mock.patch.object(panel.tree, 'itemAt', return_value=item), \
                 unittest.mock.patch('tree_panel.QMenu') as mock_menu_cls:
                mock_menu = mock_menu_cls.return_value
                panel._context_menu(QPoint(0, 0))
                all_str_args = [a for call in mock_menu.addAction.call_args_list
                                 for a in list(call.args) + list(call.kwargs.values())
                                 if isinstance(a, str)]
                self.assertFalse(any('redmarkup' in s.lower() for s in all_str_args),
                    "the standalone 'Editera redmarkup' menu entry must be gone")
                self.assertTrue(any('nodmarkup' in s.lower() for s in all_str_args),
                    "sanity: 'Editera nodmarkup' must still be offered")
        finally:
            panel.deleteLater()

    def test_node_markup_panel_has_place_symbol_button(self):
        from hazop import PropertiesRibbon
        panel = PropertiesRibbon(self.db)
        try:
            node_id = self.db.add_node()
            panel.set_item(NODE_T, node_id)
            panel.enter_markup_mode(node_id)
            seen = []
            panel.place_symbol_requested.connect(lambda: seen.append(True))
            panel._place_symbol_btn.click()
            self.assertEqual(seen, [True])
        finally:
            panel.deleteLater()

    def test_place_symbol_switches_to_red_markup_and_opens_picker(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win._on_edit_node_markup(node_id)
            try:
                with unittest.mock.patch.object(
                        win.red_markup_panel, 'open_symbol_picker') as mock_open:
                    win._on_place_symbol_requested()
                    mock_open.assert_called_once()
                self.assertFalse(win.red_markup_panel.isHidden())
                self.assertFalse(win.props_ribbon._markup_active)
                self.assertEqual(win._return_to_node_markup_node_id, node_id)
            finally:
                win._on_close_red_markup()

    def test_closing_red_markup_returns_to_node_markup_for_same_node(self):
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win._on_edit_node_markup(node_id)
            win._on_place_symbol_requested()

            win._on_close_red_markup()

            self.assertIsNone(win._return_to_node_markup_node_id)
            self.assertTrue(win.props_ribbon._markup_active)
            self.assertTrue(win.red_markup_panel.isHidden())
            self.assertEqual(win.props_ribbon.node_id, node_id)
            win._on_close_node_markup()

    def test_closing_red_markup_without_place_symbol_flow_goes_to_welcome(self):
        """If red-markup mode were ever entered WITHOUT going through
        _on_place_symbol_requested (defensive — no such path exists
        anymore, but _on_close_red_markup must still degrade safely),
        closing it must fall back to the normal closed state instead of
        crashing on a stale/missing return target."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win._on_edit_red_markup(node_id)
            self.assertIsNone(win._return_to_node_markup_node_id)

            win._on_close_red_markup()

            self.assertFalse(win.tree_panel.isHidden())
            self.assertFalse(win.scenario_panel.isHidden())
            self.assertTrue(win.red_markup_panel.isHidden())


class SmartPolylineRemovedTests(unittest.TestCase):
    """"Smart polylinje" (the SmartPipeTracer-backed markup tool, informally
    reported by the user as "Smart Polygon") was torn out of the active app
    2026-08-26 and archived to archive/smart_pipe_tracer.py (see NOTES.md).
    Confirms the node-markup toolbar no longer exposes a clickable 'smart'
    button, and that the toolbar's other tools still work fine with that
    tool gone — i.e. removing it left no gap/crash in the surrounding
    button-building or tool-selection code."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_smart_removed_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_panel(self):
        from hazop import PropertiesRibbon
        panel = PropertiesRibbon(self.db)
        node_id = self.db.add_node()
        panel.set_item(NODE_T, node_id)
        panel.enter_markup_mode(node_id)
        return panel

    def test_smart_not_in_markup_tools_spec(self):
        from hazop import PropertiesRibbon
        tool_names = [spec[0] for spec in PropertiesRibbon._MARKUP_TOOLS]
        self.assertNotIn('smart', tool_names)

    def test_smart_not_in_style_popup_tool_names(self):
        from node_markup import _StylePopup
        self.assertNotIn('smart', _StylePopup._TOOL_NAMES)

    def test_smart_button_not_built_on_toolbar(self):
        panel = self._make_panel()
        try:
            self.assertNotIn('smart', panel._tool_btns)
        finally:
            panel.deleteLater()

    def test_other_tool_button_still_works_after_smart_removal(self):
        """Clicking a surviving tool (polygon) must still select it and
        emit tool_changed — i.e. the button-building loop and _on_tool
        dispatch were not disturbed by dropping the 'smart' entry."""
        panel = self._make_panel()
        try:
            seen = []
            panel.tool_changed.connect(seen.append)
            panel._tool_btns['polygon'].click()
            self.assertEqual(seen, ['polygon'])
            self.assertEqual(panel._current_tool, 'polygon')
        finally:
            panel.deleteLater()

    def test_select_tool_still_works_after_smart_removal(self):
        panel = self._make_panel()
        try:
            seen = []
            panel.tool_changed.connect(seen.append)
            panel._tool_btns['select'].click()
            self.assertEqual(seen, ['select'])
        finally:
            panel.deleteLater()


if __name__ == "__main__":
    unittest.main()
