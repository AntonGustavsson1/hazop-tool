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


class NodeInfoMergedButtonTests(unittest.TestCase):
    """(2026-09-06) Anton: "ute till höger finns idag 6 knappar på nod...
    jag vill slå ihop de tre övre (dvs dom som innehåller namn,
    beskrivning och media) till en." The old 🏷/📄/⚗ trio
    (_edit_node_name/_edit_node_desc/_edit_node_params) is merged into a
    single _edit_node_info popup covering all six fields (name, P&ID-ref,
    description, media, pressure, temperature) at once."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_node_info_merge_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_node_ribbon_has_one_info_button_instead_of_three(self):
        from hazop import PropertiesRibbon
        node_id = self.db.add_node()
        panel = PropertiesRibbon(self.db)
        panel.set_item(NODE_T, node_id)
        try:
            tooltips = [b.toolTip() for b in panel._btns if isinstance(b, QPushButton)]
            self.assertEqual(
                sum(1 for t in tooltips if t.startswith('Redigera nod')), 1, tooltips)
            self.assertNotIn('Redigera beskrivning', tooltips)
            self.assertFalse(
                any(t.startswith('Redigera processparametrar') for t in tooltips),
                tooltips)
        finally:
            panel.deleteLater()

    def test_merged_dialog_saves_all_six_fields_in_one_go(self):
        from hazop import PropertiesRibbon
        from PyQt6.QtWidgets import QDialog, QTextEdit
        node_id = self.db.add_node()
        panel = PropertiesRibbon(self.db)
        panel.set_item(NODE_T, node_id)
        try:
            info_btn = next(b for b in panel._btns
                            if isinstance(b, QPushButton)
                            and b.toolTip().startswith('Redigera nod'))

            def _fake_show_popup(btn, dlg):
                dlg.findChild(QLineEdit, 'node_name_edit').setText('Reaktor 1')
                dlg.findChild(QLineEdit, 'node_pid_ref_edit').setText('PID-100')
                dlg.findChild(QTextEdit, 'node_desc_edit').setPlainText('Ny beskrivning')
                dlg.findChild(QLineEdit, 'node_media_edit').setText('Vätgas')
                dlg.findChild(QLineEdit, 'node_pressure_edit').setText('12 bar')
                dlg.findChild(QLineEdit, 'node_temperature_edit').setText('80 C')
                return QDialog.DialogCode.Accepted

            changed = []
            panel.item_changed.connect(lambda: changed.append(True))
            with unittest.mock.patch.object(
                    panel, '_show_popup', side_effect=_fake_show_popup):
                info_btn.click()

            self.assertEqual(changed, [True])
            n = dict(self.db.get_node(node_id))
            self.assertEqual(n['name'], 'Reaktor 1')
            self.assertEqual(n['pid_ref'], 'PID-100')
            self.assertEqual(n['description'], 'Ny beskrivning')
            self.assertEqual(n['media'], 'Vätgas')
            self.assertEqual(n['pressure'], '12 bar')
            self.assertEqual(n['temperature'], '80 C')
        finally:
            panel.deleteLater()


class MarkupTableClickToEditStyleTests(unittest.TestCase):
    """(2026-09-06) Anton: in "Nodmarkeringar" (the bottom table shown
    while editing node markup) he wants to click directly on the
    Färg/Opacitet/Tjocklek/Font cells to change that markup's style,
    with the P&ID updating automatically — previously this needed a
    right-click "Ändra stil..." context-menu action. Both paths now go
    through the shared MarkupTablePanel._edit_style(), which the
    already-existing item_style_changed → pid_panel.refresh_markup_overlays()
    wiring (hazop.py) picks up for the live P&ID update."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_markup_table_style_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.node_id = self.db.add_node()
        self.mu_id = self.db.add_node_markup(
            self.node_id, 'polygon', [[0, 0], [10, 0], [10, 10]], 'Zon 1',
            '#E53935', 0.7, 2, 0)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_panel(self):
        from hazop import MarkupTablePanel
        panel = MarkupTablePanel(self.db)
        panel.load(self.node_id)
        return panel

    def _patch_style_dialog(self, color='#1565C0', opacity=0.3, width=5, font_size=20,
                            accepted=True):
        from node_markup import _MarkupStyleDialog
        from PyQt6.QtWidgets import QDialog
        exec_patch = unittest.mock.patch.object(
            _MarkupStyleDialog, 'exec',
            return_value=(QDialog.DialogCode.Accepted if accepted
                         else QDialog.DialogCode.Rejected))
        style_patch = unittest.mock.patch.object(
            _MarkupStyleDialog, 'get_style',
            return_value=(color, opacity, width, font_size))
        return exec_patch, style_patch

    def test_clicking_color_cell_opens_style_dialog_and_saves(self):
        panel = self._make_panel()
        try:
            changed = []
            panel.item_style_changed.connect(changed.append)
            exec_patch, style_patch = self._patch_style_dialog(color='#43A047')
            with exec_patch, style_patch:
                panel._on_cell_clicked(0, 2)   # Färg column

            self.assertEqual(changed, [self.mu_id])
            mu = dict(self.db.get_node_markup(self.mu_id))
            self.assertEqual(mu['color'], '#43A047')
        finally:
            panel.deleteLater()

    def test_clicking_opacity_cell_opens_style_dialog_and_saves(self):
        panel = self._make_panel()
        try:
            exec_patch, style_patch = self._patch_style_dialog(opacity=0.9)
            with exec_patch, style_patch:
                panel._on_cell_clicked(0, 3)   # Opacitet column
            mu = dict(self.db.get_node_markup(self.mu_id))
            self.assertAlmostEqual(mu['opacity'], 0.9)
        finally:
            panel.deleteLater()

    def test_clicking_width_cell_opens_style_dialog_and_saves(self):
        panel = self._make_panel()
        try:
            exec_patch, style_patch = self._patch_style_dialog(width=9)
            with exec_patch, style_patch:
                panel._on_cell_clicked(0, 4)   # Tjocklek column
            mu = dict(self.db.get_node_markup(self.mu_id))
            self.assertEqual(mu['line_width'], 9)
        finally:
            panel.deleteLater()

    def test_clicking_font_cell_opens_style_dialog_and_saves(self):
        panel = self._make_panel()
        try:
            exec_patch, style_patch = self._patch_style_dialog(font_size=30)
            with exec_patch, style_patch:
                panel._on_cell_clicked(0, 5)   # Font column
            mu = dict(self.db.get_node_markup(self.mu_id))
            self.assertEqual(mu['font_size'], 30)
        finally:
            panel.deleteLater()

    def test_cancelling_style_dialog_leaves_markup_unchanged(self):
        panel = self._make_panel()
        try:
            changed = []
            panel.item_style_changed.connect(changed.append)
            exec_patch, style_patch = self._patch_style_dialog(
                color='#000000', accepted=False)
            with exec_patch, style_patch:
                panel._on_cell_clicked(0, 2)

            self.assertEqual(changed, [])
            mu = dict(self.db.get_node_markup(self.mu_id))
            self.assertEqual(mu['color'], '#E53935')
        finally:
            panel.deleteLater()

    def test_style_edit_refreshes_table_row_with_new_values(self):
        panel = self._make_panel()
        try:
            exec_patch, style_patch = self._patch_style_dialog(
                color='#1565C0', opacity=0.5, width=7, font_size=18)
            with exec_patch, style_patch:
                panel._on_cell_clicked(0, 3)
            self.assertEqual(panel._table.item(0, 2).text(), '#1565C0')
            self.assertEqual(panel._table.item(0, 3).text(), '50%')
            self.assertEqual(panel._table.item(0, 4).text(), '7')
            self.assertEqual(panel._table.item(0, 5).text(), '18')
        finally:
            panel.deleteLater()

    def test_clicking_visibility_column_still_toggles_instead_of_opening_style(self):
        """Column 6 (👁) must keep its own dedicated behaviour — the new
        style-editing columns are only 2-5."""
        panel = self._make_panel()
        try:
            changed = []
            panel.item_style_changed.connect(changed.append)
            vis_toggled = []
            panel.item_vis_toggled.connect(lambda mu_id, vis: vis_toggled.append((mu_id, vis)))
            panel._on_cell_clicked(0, 6)
            self.assertEqual(changed, [])
            self.assertEqual(vis_toggled, [(self.mu_id, False)])
        finally:
            panel.deleteLater()

    def test_clicking_label_column_still_only_selects(self):
        """Column 1 (Etikett) is unaffected by this change — no style
        dialog, just the pre-existing item_selected signal."""
        panel = self._make_panel()
        try:
            changed = []
            panel.item_style_changed.connect(changed.append)
            selected = []
            panel.item_selected.connect(selected.append)
            panel._on_cell_clicked(0, 1)
            self.assertEqual(changed, [])
            self.assertEqual(selected, [self.mu_id])
        finally:
            panel.deleteLater()

    def test_context_menu_style_action_still_works_via_shared_helper(self):
        """The refactor moved the ctx-menu dialog code into _edit_style();
        calling that helper directly (what the "Ändra stil..." action now
        does) must behave identically to the column-click path."""
        panel = self._make_panel()
        try:
            changed = []
            panel.item_style_changed.connect(changed.append)
            exec_patch, style_patch = self._patch_style_dialog(color='#F9A825')
            with exec_patch, style_patch:
                panel._edit_style(self.mu_id)
            self.assertEqual(changed, [self.mu_id])
            mu = dict(self.db.get_node_markup(self.mu_id))
            self.assertEqual(mu['color'], '#F9A825')
        finally:
            panel.deleteLater()


class MarkupModeHidesPlainNodeButtonsTests(unittest.TestCase):
    """(2026-09-06) Anton: "När man klickar på pennan i nodmarkeringar kan
    du släcka dom tre övre knapparna ute till höger då dessa inte har
    någon funktion längre och flytta upp knapparna som har med
    nodmarkeringen att göra." While the ✏️ markup toggle is checked, the
    plain node buttons (namn/status/zoom — merged into one 🏷 button plus
    ✅/📍, see the earlier "slå ihop de tre övre" change) do nothing for
    the markup being edited, so they're skipped entirely and the markup
    toggle becomes the first widget in the ribbon instead of sitting
    below an inert button group."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_markup_mode_hides_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _plain_button_tooltips(self, panel):
        return [b.toolTip() for b in panel._btns if isinstance(b, QPushButton)
                and b is not getattr(panel, '_markup_toggle_btn', None)
                and b not in panel._tool_btns.values()
                and b is not getattr(panel, '_place_symbol_btn', None)
                and b is not getattr(panel, '_all_vis_btn', None)
                and b is not getattr(panel, '_prev_btn', None)
                and b is not getattr(panel, '_next_btn', None)
                and b is not getattr(panel, '_bottom_toggle_btn', None)]

    def test_plain_node_buttons_present_before_entering_markup_mode(self):
        from hazop import PropertiesRibbon
        node_id = self.db.add_node()
        panel = PropertiesRibbon(self.db)
        panel.set_item(NODE_T, node_id)
        try:
            tooltips = self._plain_button_tooltips(panel)
            self.assertTrue(any(t.startswith('Redigera nod') for t in tooltips), tooltips)
            self.assertTrue(any('status' in t.lower() for t in tooltips), tooltips)
            self.assertTrue(any('Visa nod på P&ID' == t for t in tooltips), tooltips)
        finally:
            panel.deleteLater()

    def test_entering_markup_mode_hides_the_plain_node_buttons(self):
        from hazop import PropertiesRibbon
        node_id = self.db.add_node()
        panel = PropertiesRibbon(self.db)
        panel.set_item(NODE_T, node_id)
        panel.enter_markup_mode(node_id)
        try:
            tooltips = self._plain_button_tooltips(panel)
            self.assertEqual(tooltips, [],
                "plain node buttons must be gone entirely while markup "
                "editing is active, not just hidden or disabled")
            # The markup toggle and its tools must still be there.
            self.assertIsNotNone(panel._markup_toggle_btn)
            self.assertTrue(panel._markup_toggle_btn.isChecked())
            self.assertIn('select', panel._tool_btns)
        finally:
            panel.deleteLater()

    def test_markup_toggle_is_the_first_widget_when_active(self):
        from hazop import PropertiesRibbon
        node_id = self.db.add_node()
        panel = PropertiesRibbon(self.db)
        panel.set_item(NODE_T, node_id)
        panel.enter_markup_mode(node_id)
        try:
            first_item = panel._outer.itemAt(0)
            self.assertIs(first_item.widget(), panel._markup_toggle_btn,
                "the markup toggle should be the very first widget — no "
                "leading separator or leftover plain-button group above it")
        finally:
            panel.deleteLater()

    def test_exiting_markup_mode_restores_the_plain_node_buttons(self):
        from hazop import PropertiesRibbon
        node_id = self.db.add_node()
        panel = PropertiesRibbon(self.db)
        panel.set_item(NODE_T, node_id)
        panel.enter_markup_mode(node_id)
        panel.exit_markup_mode()
        try:
            tooltips = self._plain_button_tooltips(panel)
            self.assertTrue(any(t.startswith('Redigera nod') for t in tooltips), tooltips)
            self.assertEqual(panel._tool_btns, {},
                "markup tool buttons must be torn down once editing exits")
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
    placing a symbol briefly switches into red-markup mode and back.

    2026-08-26 follow-up ("Gör om Red Markup-knappen", see NOTES.md): the
    old Red Markup VIEW (this ribbon shown via splitter resize, plus
    RedMarkupTablePanel, a table of existing red markups) is torn down
    completely — the button now opens ONLY the small symbol-selector
    popup, no chrome changes at all. RedMarkupTablePanel is deleted
    outright; RedMarkupPanel itself is kept (never shown) purely as the
    non-visual state/signal object MainWindow still needs."""

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
                # 2026-08-26 rework: the old Red Markup view is gone — the
                # button must NOT reveal red_markup_panel anymore, and
                # props_ribbon's own chrome (still showing the node-markup
                # toolbar) is left completely untouched.
                self.assertTrue(win.red_markup_panel.isHidden())
                self.assertTrue(win.props_ribbon._markup_active)
                self.assertEqual(win._return_to_node_markup_node_id, node_id)
            finally:
                win._on_close_red_markup()

    def test_place_symbol_opens_only_the_popup_no_other_chrome_changes(self):
        """Direct regression test for "Gör om Red Markup-knappen" (see
        NOTES.md, 2026-08-26): clicking the place-symbol button must not
        resurrect any part of the old Red Markup view — no RedMarkupPanel
        ribbon, no RedMarkupTablePanel (deleted outright), and none of
        tree_panel/scenario_panel/props_ribbon's own visibility changes at
        all. Only the small symbol popup opens."""
        with _TempDbMainWindow() as win:
            self.assertFalse(hasattr(win, 'red_markup_table_panel'),
                "RedMarkupTablePanel must be torn down entirely, not just hidden")
            node_id = win.db.add_node()
            win._on_edit_node_markup(node_id)
            tree_hidden_before = win.tree_panel.isHidden()
            scenario_hidden_before = win.scenario_panel.isHidden()
            ribbon_hidden_before = win.props_ribbon.isHidden()
            try:
                with unittest.mock.patch.object(
                        win.red_markup_panel, 'open_symbol_picker') as mock_open:
                    win._on_place_symbol_requested()
                    mock_open.assert_called_once()
                self.assertTrue(win.red_markup_panel.isHidden(),
                    "the old Red Markup ribbon must never be shown")
                self.assertEqual(win.tree_panel.isHidden(), tree_hidden_before)
                self.assertEqual(win.scenario_panel.isHidden(), scenario_hidden_before)
                self.assertEqual(win.props_ribbon.isHidden(), ribbon_hidden_before)
            finally:
                win._on_close_red_markup()

    def test_place_symbol_then_draw_saves_to_db_and_returns_to_node_markup(self):
        """End-to-end regression for the exact risk flagged while reworking
        this flow: essential plumbing (set_active_node + the
        markup_draw_finished/markup_item_clicked wiring done by
        pid_panel.enter_red_markup_edit) must still run even though none
        of the old view's chrome does anymore — otherwise a symbol drawn
        on the canvas would silently never reach the DB. Simulates the
        full user flow: click place-symbol -> pick a symbol from the
        popup -> draw it on the canvas (viewer emits markup_draw_finished)
        -> verify a row actually landed in node_red_markups, and that the
        app automatically snapped back to node-markup editing for the
        same node afterward (no old Red Markup view left open)."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win._on_edit_node_markup(node_id)

            win._on_place_symbol_requested()
            self.assertEqual(win.pid_panel._active_markup_class, 'red')
            self.assertEqual(win.pid_panel._active_node_id, node_id)

            # Pick a symbol from the popup (mirrors what a real click on a
            # popup button does: RedMarkupPanel._on_symbol_selected).
            win.red_markup_panel._on_symbol_selected('valve')

            # Draw it on the canvas — same call the viewer itself makes
            # once a symbol placement click completes.
            win.pid_panel._on_viewer_markup_drawn('symbol', [[10.0, 20.0]], 0)

            rows = [dict(r) for r in win.db.node_red_markups_for_node(node_id)]
            self.assertEqual(len(rows), 1,
                "the drawn symbol must actually be saved to the DB")
            self.assertEqual(rows[0]['type'], 'symbol')
            self.assertEqual(rows[0]['label'], 'valve')

            # The whole point of the detour is done — must have snapped
            # back to node-markup editing automatically, no manual close
            # required (the old ✕ close button is unreachable now).
            self.assertIsNone(win._return_to_node_markup_node_id)
            self.assertEqual(win.pid_panel._active_markup_class, 'node')
            self.assertTrue(win.red_markup_panel.isHidden())
            self.assertEqual(win.props_ribbon.node_id, node_id)
            win._on_close_node_markup()

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
        anymore; _on_edit_red_markup, the old method that used to wrap
        this, was itself deleted 2026-08-26 along with the rest of the
        old Red Markup view, see NOTES.md "Gör om Red Markup-knappen" —
        entering pid_panel.enter_red_markup_edit directly is now the only
        way to simulate this defensive scenario), closing it must fall
        back to the normal closed state instead of crashing on a
        stale/missing return target."""
        with _TempDbMainWindow() as win:
            node_id = win.db.add_node()
            win.pid_panel.enter_red_markup_edit(node_id)
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
