#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering tree_panel.py, plus any cross-module glue they
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
    NODE_T, DEV_T, CAUSE_T, CONS_T, SG_T, EQUIP_T, LEDORD_T, SYSTEM_T,
    freq_to_idx,
)
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QGraphicsPixmapItem, QTreeWidgetItemIterator, QTreeWidgetItem,
    QCheckBox, QComboBox, QPushButton, QMessageBox, QInputDialog, QLineEdit,
)
from PyQt6.QtGui import QPixmap, QFocusEvent, QColor  # noqa: E402
from PyQt6.QtCore import Qt, QPoint, QDate, QEvent, QThread, pyqtSignal  # noqa: E402
from equipment_detection import COMPONENT_TYPES  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# Shared QApplication — Qt only allows one per process.
# ══════════════════════════════════════════════════════════════════════════


from test_helpers import (
    _ensure_qapp, _menu_action_labels, _fake_pdf_loaded,
    _TempDbMainWindow, _find_tree_item, count_selects,
)


class TreePanelLayerToggleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        from pid_viewer import TREE_CONTEXT_LINK_COLORS
        self._original_context_colors = {
            key: QColor(value) for key, value in TREE_CONTEXT_LINK_COLORS.items()
        }
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_tree_layers_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde")
        self.cause_id = self.db.add_cause(dev_id)
        self.cons_id = self.db.add_consequence(self.cause_id)
        self.sg_id = self.db.add_safeguard(self.cons_id)
        self.panel = TreePanel(self.db)
        self.panel.refresh()

    def tearDown(self):
        from pid_viewer import set_tree_context_link_color
        for key, color in self._original_context_colors.items():
            set_tree_context_link_color(key, color)
        self.panel.deleteLater()
        del self.db
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _item(self, type_, id_):
        return _find_tree_item(self.panel.tree, type_, id_)

    def test_three_hazop_layers_are_green_and_visible_by_default(self):
        for type_key in ('cause', 'consequence', 'safeguard'):
            button = self.panel._vis_btns[type_key]
            self.assertTrue(button.isChecked())
            self.assertEqual(self.panel._vis_colors[type_key].lower(), '#00c800')

    def test_click_only_changes_pid_layer_signal_not_tree_rows(self):
        cases = (
            ('cause', CAUSE_T, self.cause_id),
            ('consequence', CONS_T, self.cons_id),
            ('safeguard', SG_T, self.sg_id),
        )
        emitted = []
        self.panel.visibility_changed.connect(lambda t, v: emitted.append((t, v)))
        for type_key, type_, id_ in cases:
            with self.subTest(type_key=type_key):
                button = self.panel._vis_btns[type_key]
                button.setChecked(False)
                self.assertFalse(self._item(type_, id_).isHidden())
                self.panel.refresh()
                self.assertFalse(self._item(type_, id_).isHidden())
                button.setChecked(True)
                self.assertFalse(self._item(type_, id_).isHidden())
        self.assertEqual(emitted,
                         [(value, checked) for value, _, _ in cases
                          for checked in (False, True)])

    def test_right_click_color_choice_is_saved_and_updates_pid_color(self):
        from pid_viewer import resolve_tree_context_color
        with unittest.mock.patch('tree_panel.QColorDialog.getColor',
                                 return_value=QColor('#2457a6')):
            self.panel._choose_visibility_color('cause')
        self.assertEqual(self.db.get_config('tree_color_cause'), '#2457a6')
        self.assertEqual(resolve_tree_context_color({'cause'}).name(), '#2457a6')
        self.assertIn('#2457a6', self.panel._vis_btns['cause'].styleSheet())

    def test_unchecked_role_is_grey_but_checked_role_stays_coloured(self):
        from pid_viewer import (resolve_tree_context_color,
                                TREE_CONTEXT_HIGHLIGHT_DISABLED)
        self.assertEqual(
            resolve_tree_context_color({'cause'}, {'cause'}).name(),
            TREE_CONTEXT_HIGHLIGHT_DISABLED.name())
        # If an object is linked through more than one role, an enabled role
        # still supplies its colour even when another role is unchecked.
        self.assertEqual(
            resolve_tree_context_color({'cause', 'safeguard'}, {'cause'}).name(),
            resolve_tree_context_color({'safeguard'}).name())

class TreePanelEquipmentGroupingTests(unittest.TestCase):
    """TreePanel.refresh() builds Nod → Avvikelse → Orsak → Konsekvens →
    Safeguard (2026-08-25, see NOTES.md "Rättar ihopslagningen": rättar
    samma dags tidigare, felriktade ihopslagning som kombinerade
    objekt-taggen med AVVIKELSENS text ("Lågt flöde") istället för
    ORSAKENS egen ("Felar stängd" etc)).

    Avvikelsenivån är kvar INTAKT: deviations under a node are grouped by
    their guide-word text (several deviation rows across different
    equipment can share one description, e.g. "Lågt flöde" for both a
    pump and a valve) into exactly ONE tree row per description, always
    — no separate wrapper level, no per-equipment duplication (confirmed
    via AskUserQuestion: several objects sharing one avvikelse text
    share ONE row, not one each). That row anchors on the GENERIC
    (equipment_id IS NULL) deviation auto-seeded per guide word by
    add_node(), so "+ Orsak"/drag-and-drop always has somewhere to land.

    Orsak = objekt-tag + orsaksbeskrivning: every cause across ALL
    deviations sharing that description (equipment-linked or generic)
    becomes a direct child of the ONE avvikelse row, labeled with its
    own comp_tag + description combined (falls back to just the tag if
    the cause is still an untouched placeholder, or just the description
    if it has no tag at all)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_treeequip_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.panel = TreePanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _tree_items(self):
        """Flat list of (type_, id_, parent_type_) for every item in the tree."""
        out = []
        it = QTreeWidgetItemIterator(self.panel.tree)
        while it.value():
            item = it.value()
            t = item.data(0, Qt.ItemDataRole.UserRole + 1)
            i = item.data(0, Qt.ItemDataRole.UserRole)
            pt = item.parent().data(0, Qt.ItemDataRole.UserRole + 1) if item.parent() else None
            out.append((t, i, pt))
            it += 1
        return out

    def _find_item(self, type_, id_):
        it = QTreeWidgetItemIterator(self.panel.tree)
        while it.value():
            candidate = it.value()
            if (candidate.data(0, Qt.ItemDataRole.UserRole + 1) == type_
                    and candidate.data(0, Qt.ItemDataRole.UserRole) == id_):
                return candidate
            it += 1
        return None

    def test_avvikelse_row_shows_only_the_guide_word_text(self):
        """The avvikelse row's own label must never carry any object
        identity — just the guide-word text, unchanged, exactly as
        before ANY equipment-grouping feature ever existed."""
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        # The row anchors on the GENERIC deviation (already auto-seeded
        # by add_node()), not the equipment-scoped id just created above
        # — get_or_create_deviation is idempotent, so this just resolves
        # the same existing generic row.
        anchor_id = self.db.get_or_create_deviation(node_id, "Lågt flöde")
        self.panel.refresh()

        item = self._find_item(DEV_T, anchor_id)
        self.assertIsNotNone(item)
        self.assertEqual(item.parent().data(0, Qt.ItemDataRole.UserRole + 1), NODE_T,
            "the avvikelse row must sit directly under the node — no wrapper level")
        self.assertIn("Lågt flöde", item.text(0))
        self.assertNotIn("V-101", item.text(0),
            "the avvikelse row must not carry any object tag")
        self.assertTrue(item.font(0).italic())

    def test_orsak_row_combines_tag_and_description(self):
        """The whole point of the change: once a cause has both a tag
        AND a real description, its row shows both together."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        anchor_id = self.db.get_or_create_deviation(node_id, "Lågt flöde")
        cause_id, _cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        self.db.update_cause(cause_id, description="Felar stängd")
        self.panel.refresh()

        dev_item = self._find_item(DEV_T, anchor_id)
        cause_item = self._find_item(CAUSE_T, cause_id)
        self.assertIsNotNone(cause_item)
        self.assertIs(cause_item.parent(), dev_item,
            "the orsak row must be a direct child of the avvikelse row")
        self.assertIn("V-101, Felar stängd", cause_item.text(0))

    def test_orsak_row_shows_just_tag_when_cause_still_trivial(self):
        """A freshly tagged, not-yet-described cause shows just the tag
        — "V-101, Ny orsak" would be noise, not information."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id, _cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        self.panel.refresh()

        cause_item = self._find_item(CAUSE_T, cause_id)
        self.assertIsNotNone(cause_item)
        self.assertIn("V-101", cause_item.text(0))
        self.assertNotIn(",", cause_item.text(0),
            "a still-trivial cause must show the bare tag, no separator/description")

    def test_grouped_orsak_row_is_not_bold(self):
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "LÃ¥gt flÃ¶de")
        primary_id = self.db.add_equipment_item(
            "FI-1", "FI-1", "FI", 0, "Instrument", '', 0)
        secondary_id = self.db.add_equipment_item(
            "FV-1", "FV-1", "FV", 0, "Reglerventil", '', 0)
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(
            cause_id, comp_type="Instrument", comp_tag="FI-1 + FV-1",
            equipment_id=primary_id, secondary_equipment_id=secondary_id,
            description="FI-1 felar\nFV-1 öppnar")
        self.panel.refresh()

        cause_item = self._find_item(CAUSE_T, cause_id)
        self.assertIsNotNone(cause_item)
        self.assertFalse(cause_item.font(0).bold())

    def test_orsak_row_shows_just_description_when_no_tag(self):
        """'Det räcker om instrumentet E1.M1.QMA127 dyker upp på en rad
        i trädhierarkin' (2026-08-11) — a plain, untagged cause (no
        comp_tag) shows just its own description, same as it always
        has, regardless of anything to do with objects."""
        node_id = self.db.add_node()
        dev_id = next(d for d in self.db.deviations(node_id)
                       if d['description'] == "Lågt flöde")['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, description="Flödesgivare felar -> styrventil stänger")
        self.panel.refresh()

        cause_item = self._find_item(CAUSE_T, cause_id)
        self.assertIsNotNone(cause_item)
        self.assertIn("Flödesgivare felar", cause_item.text(0))
        self.assertNotIn("—", cause_item.text(0))

    def test_two_objects_sharing_avvikelse_share_one_row_with_separate_orsak_children(self):
        """Confirmed via AskUserQuestion: a pump AND a valve that both
        have 'Lågt flöde' under the same node share ONE avvikelse row —
        not one each — with a separate, own Orsak row for each."""
        from hazop import _create_tagged_cause
        pump_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        valve_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        pump_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=pump_id)
        valve_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=valve_id)
        pump_cause, _c1 = _create_tagged_cause(self.db, pump_dev, "Pump", "P-101")
        self.db.update_cause(pump_cause, description="Felar stängd")
        valve_cause, _c2 = _create_tagged_cause(self.db, valve_dev, "Ventil", "V-101")
        self.db.update_cause(valve_cause, description="Läcker")
        self.panel.refresh()

        # Both causes' underlying deviations share one description, so
        # exactly one DEV_T row anchors it (whichever generic/first one
        # get_or_create_deviation's own bookkeeping picks) — assert via
        # the causes, not by guessing which dev_id is the anchor.
        pump_item = self._find_item(CAUSE_T, pump_cause)
        valve_item = self._find_item(CAUSE_T, valve_cause)
        self.assertIsNotNone(pump_item)
        self.assertIsNotNone(valve_item)
        self.assertIs(pump_item.parent(), valve_item.parent(),
            "both objects' causes must share the SAME avvikelse row as parent")
        self.assertIn("P-101, Felar stängd", pump_item.text(0))
        self.assertIn("V-101, Läcker", valve_item.text(0))
        # Only ONE avvikelse row for "Lågt flöde" under this node.
        parent = pump_item.parent()
        self.assertEqual(parent.data(0, Qt.ItemDataRole.UserRole + 1), DEV_T)
        self.assertIn("Lågt flöde", parent.text(0))

    def test_numbering_stays_sequential_regardless_of_equipment(self):
        """'jag vill att den ska kvarstå så att det alltid syns att det
        är exempelvis 16 avikelser' (2026-08-13) — every avvikelse row
        gets exactly one number, in order, whether or not any equipment
        happens to be linked to it. A fresh node's 16 auto-seeded guide
        words show as a gapless 1..16 sequence; linking equipment to one
        of them must not change that count or leave a gap/duplicate."""
        import re
        node_id = self.db.add_node()
        n_seeded = len(self.db.deviations(node_id))
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        node_item = _find_tree_item(self.panel.tree, NODE_T, node_id)
        self.assertIsNotNone(node_item)
        numbers = []
        for i in range(node_item.childCount()):
            child = node_item.child(i)
            if child.data(0, Qt.ItemDataRole.UserRole + 1) == DEV_T:
                m = re.search(r'(\d+)\.\s', child.text(0))
                if m:
                    numbers.append(int(m.group(1)))
        self.assertEqual(sorted(numbers), list(range(1, n_seeded + 1)),
            f"expected a gapless 1..{n_seeded} sequence, got: {sorted(numbers)}")

    def test_brand_new_node_has_only_flat_avvikelse_rows(self):
        """A freshly created node (all ~16 auto-seeded guide words, no
        equipment touched yet) shows every guide word as its own flat
        row directly under the node — zero LEDORD_T/EQUIP_T anywhere."""
        node_id = self.db.add_node()
        self.panel.refresh()

        items = self._tree_items()
        self.assertEqual(len([x for x in items if x[0] in (LEDORD_T, EQUIP_T)]), 0)
        dev_rows = [x for x in items if x[0] == DEV_T]
        self.assertTrue(dev_rows)
        self.assertTrue(all(x[2] == NODE_T for x in dev_rows))

    def test_avvikelse_row_offers_add_cause_context_menu(self):
        """The avvikelse row is a normal DEV_T item (no special-casing
        left for the equipment-adjacent case) — right-clicking it must
        offer '+ Lägg till orsak' exactly like any other DEV_T row."""
        eq_id = self.db.add_equipment_item("M1.GPA6", "M1.GPA6", "M1", 0, "Pump", '', 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        anchor_id = self.db.get_or_create_deviation(node_id, "Lågt flöde")
        self.panel.refresh()
        dev_item = self._find_item(DEV_T, anchor_id)
        self.assertIsNotNone(dev_item)

        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=dev_item), \
             unittest.mock.patch('tree_panel.QMenu') as mock_menu_cls:
            self.panel._context_menu(QPoint(0, 0))
        mock_menu_cls.assert_called_once()
        labels = _menu_action_labels(mock_menu_cls.return_value)
        self.assertTrue(any("Lägg till orsak" in lbl for lbl in labels))

    def test_double_click_avvikelse_row_does_normal_inline_edit(self):
        """2026-08-25: the avvikelse row no longer carries any object
        identity (_EQUIP_TAG_ROLE is gone), so double-clicking it now
        does normal inline text editing of the guide-word text, same as
        any other DEV_T row always could — not the Tag+Typ popup the
        old, now-removed merged row used to redirect to."""
        from hazop import CauseTagPopup
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        anchor_id = self.db.get_or_create_deviation(node_id, "Lågt flöde")
        self.panel.refresh()
        item = self._find_item(DEV_T, anchor_id)

        self.panel._on_item_double_click(item, 0)

        self.assertEqual(len(self.panel.findChildren(CauseTagPopup)), 0)
        self.assertEqual(self.panel._inline_edit_target, (DEV_T, anchor_id))
        self.assertEqual(len(self.panel.tree.viewport().findChildren(QLineEdit)), 1)

    def test_double_click_orsak_row_does_normal_inline_edit(self):
        """Same for the Orsak row — it's a real CAUSE_T item now, edited
        exactly like any other cause; the tag portion is never part of
        the editable text (_raw_text_for(CAUSE_T, ...) already only
        ever returns cause['description'])."""
        from hazop import _create_tagged_cause, CauseTagPopup
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id, _cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        self.db.update_cause(cause_id, description="Felar stängd")
        self.panel.refresh()
        item = self._find_item(CAUSE_T, cause_id)

        self.panel._on_item_double_click(item, 0)

        self.assertEqual(len(self.panel.findChildren(CauseTagPopup)), 0)
        self.assertEqual(self.panel._inline_edit_target, (CAUSE_T, cause_id))

    def test_empty_generic_deviation_is_the_avvikelse_rows_own_anchor(self):
        """add_node() auto-seeds an empty, generic (equipment_id=NULL)
        'Lågt flöde' deviation for every node — once equipment ALSO gets
        its own 'Lågt flöde', the (still-empty) generic one is exactly
        the anchor the ONE shared avvikelse row points to; it never
        needs separate hiding logic anymore, since it never gets its own
        row to hide in the first place."""
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        node_id = self.db.add_node()
        generic_dev = next(d for d in self.db.deviations(node_id)
                            if d['description'] == "Lågt flöde")
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        # Scoped to THIS test's node — a fresh Database auto-seeds its own
        # default node (with its own "Lågt flöde"), so scanning the whole
        # tree would double-count.
        node_item = _find_tree_item(self.panel.tree, NODE_T, node_id)
        avvikelse_rows = []
        for i in range(node_item.childCount()):
            child = node_item.child(i)
            if (child.data(0, Qt.ItemDataRole.UserRole + 1) == DEV_T
                    and (self.db.get_deviation(child.data(0, Qt.ItemDataRole.UserRole)) or {})
                        .get('description') == "Lågt flöde"):
                avvikelse_rows.append(child.data(0, Qt.ItemDataRole.UserRole))
        self.assertEqual(len(avvikelse_rows), 1,
            "exactly one avvikelse row must exist for 'Lågt flöde', shared by both")
        self.assertEqual(avvikelse_rows[0], generic_dev['id'],
            "the generic deviation is the row's anchor")

    def test_resolve_node_id_for_equip_t(self):
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        self.db.set_equipment_node(eq_id, node_id)
        self.assertEqual(self.panel._resolve_node_id(EQUIP_T, eq_id), node_id)

    def test_resolve_node_id_for_ledord_t(self):
        """LEDORD_T is now unreachable via refresh() (2026-08-25, see
        NOTES.md) — every avvikelse row is DEV_T. The resolver code
        itself is left in place (harmless, zero cost) for the rare case
        it might be needed again; verified directly against a
        synthetically constructed item instead of relying on refresh()
        to produce one."""
        node_id = self.db.add_node()
        ledord_key = f"{node_id}:Lågt flöde"
        item = QTreeWidgetItem(["synthetic"])
        item.setData(0, Qt.ItemDataRole.UserRole, ledord_key)
        item.setData(0, Qt.ItemDataRole.UserRole + 1, LEDORD_T)
        self.assertEqual(self.panel._resolve_node_id(LEDORD_T, ledord_key), node_id)

    def test_context_menu_is_a_no_op_for_ledord_t(self):
        """LEDORD_T is a pure grouping view (like EQUIP_T) — right-clicking
        it must return before ever building/exec-ing a QMenu. Verified
        against a synthetically constructed item (2026-08-25: refresh()
        no longer produces a real LEDORD_T row for any reachable
        scenario) rather than QMenu.exec() hanging a headless test run
        if the check were ever bypassed."""
        node_id = self.db.add_node()
        self.panel.refresh()
        node_item = _find_tree_item(self.panel.tree, NODE_T, node_id)
        ledord_item = QTreeWidgetItem(node_item, ["synthetic"])
        ledord_item.setData(0, Qt.ItemDataRole.UserRole, f"{node_id}:Lågt flöde")
        ledord_item.setData(0, Qt.ItemDataRole.UserRole + 1, LEDORD_T)

        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=ledord_item), \
             unittest.mock.patch('tree_panel.QMenu') as mock_menu_cls:
            try:
                self.panel._context_menu(QPoint(0, 0))
            except Exception as e:
                self.fail(f"right-clicking a LEDORD_T item must not raise: {e!r}")
            mock_menu_cls.assert_not_called()


class TreeNodeRenameTests(unittest.TestCase):
    """Reported feedback (2026-08-12, see NOTES.md): "jag vill kunna döpa
    om noder genom att högerklicka på trädet och välja döp om där." A
    node could already be renamed via PropertiesRibbon's own popup, but
    not directly from the tree's right-click menu."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_noderename_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.panel = TreePanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_node_context_menu_offers_rename(self):
        node_id = self.db.add_node()
        self.panel.refresh()
        item = _find_tree_item(self.panel.tree, NODE_T, node_id)
        self.assertIsNotNone(item)

        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=item), \
             unittest.mock.patch('tree_panel.QMenu') as mock_menu_cls:
            self.panel._context_menu(QPoint(0, 0))

        mock_menu = mock_menu_cls.return_value
        labels = _menu_action_labels(mock_menu)
        self.assertTrue(any("Döp om" in lbl for lbl in labels), labels)

    def test_rename_updates_name_and_preserves_other_fields(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Original", "Beskrivning", "P&ID-1",
                             "Media", "10 bar", "50 C")

        with unittest.mock.patch('hazop.QInputDialog.getText',
                                  return_value=("Nytt namn", True)):
            self.panel._rename_node(node_id)

        updated = dict(self.db.get_node(node_id))
        self.assertEqual(updated['name'], "Nytt namn")
        self.assertEqual(updated['description'], "Beskrivning")
        self.assertEqual(updated['pid_ref'], "P&ID-1")
        self.assertEqual(updated['media'], "Media")

    def test_rename_updates_node_name_markup_on_pid(self):
        """"När jag sedan uppdaterar namnet på noden vill jag att detta
        uppdateras även på P&ID" (2026-08-17, see NOTES.md) —
        Database.update_node() must keep any "Lägg ut nodnamn" markup
        (node_markups.type='text') in sync with the node's current name,
        not leave it frozen at whatever it said when first placed. Goes
        through the actual "Döp om" tree action, not update_node()
        directly, since that's the path that was missing the sync."""
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Original", "", "", "", "", "")
        mu_id = self.db.add_node_markup(
            node_id, 'text', [[10, 10]], "Original", '#1565C0', 1.0, 2, 0)

        with unittest.mock.patch('hazop.QInputDialog.getText',
                                  return_value=("Nytt namn", True)):
            self.panel._rename_node(node_id)

        self.assertEqual(dict(self.db.get_node_markup(mu_id))['label'], "Nytt namn")

    def test_rename_cancelled_leaves_name_unchanged(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Original", "", "", "", "", "")

        with unittest.mock.patch('hazop.QInputDialog.getText',
                                  return_value=("Ignored", False)):
            self.panel._rename_node(node_id)

        self.assertEqual(dict(self.db.get_node(node_id))['name'], "Original")

    def test_rename_with_blank_name_leaves_name_unchanged(self):
        node_id = self.db.add_node()
        self.db.update_node(node_id, "Original", "", "", "", "", "")

        with unittest.mock.patch('hazop.QInputDialog.getText',
                                  return_value=("   ", True)):
            self.panel._rename_node(node_id)

        self.assertEqual(dict(self.db.get_node(node_id))['name'], "Original")

    def test_rename_emits_structure_changed_for_tree_and_scenario_refresh(self):
        node_id = self.db.add_node()
        received = []
        self.panel.structure_changed.connect(lambda: received.append(True))

        with unittest.mock.patch('hazop.QInputDialog.getText',
                                  return_value=("Nytt", True)):
            self.panel._rename_node(node_id)

        self.assertEqual(len(received), 1)


class TreeAutoExpandCappedAtObjectLevelTests(unittest.TestCase):
    """"jag vill att du by default inte öppnar upp trädet mer än till
    objektet. Dvs du kan skippa orsakstexten, konsekvensen och
    safeguards. Dessa skall ju såklart vara öppna manuellt som idag men
    inte så fort de läggs till." (2026-08-18) — TreePanel.refresh()'s
    scrollToItem(target) silently expands every collapsed ancestor of
    whatever gets selected (verified directly against PyQt6: setCurrentItem
    alone does NOT expand anything, scrollToItem does) — so adding a
    cause/consequence/safeguard ANYWHERE in the app (not just the tree)
    used to progressively unfold the whole tree, defeating its use as an
    overview. TreePanel._reveal() now skips that forced scroll/expand for
    Orsak/Konsekvens/Safeguard specifically, unless every ancestor already
    happens to be expanded."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_treeautoexpand_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.panel = TreePanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _chain(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        return node_id, dev_id, cause_id

    def test_adding_a_consequence_does_not_expand_its_cause(self):
        node_id, dev_id, cause_id = self._chain()
        self.panel.refresh()
        cons_id = self.db.add_consequence(cause_id)

        self.panel.refresh(CONS_T, cons_id)

        cause_item = _find_tree_item(self.panel.tree, CAUSE_T, cause_id)
        self.assertIsNotNone(cause_item)
        self.assertFalse(cause_item.isExpanded(),
            "adding a consequence must not auto-open the cause that owns it")
        self.assertIs(self.panel.tree.currentItem(),
                      _find_tree_item(self.panel.tree, CONS_T, cons_id),
            "the new consequence must still become the tree's current item even while hidden")

    def test_adding_a_safeguard_does_not_expand_its_consequence(self):
        node_id, dev_id, cause_id = self._chain()
        cons_id = self.db.add_consequence(cause_id)
        self.panel.refresh()
        sg_id = self.db.add_safeguard(cons_id)

        self.panel.refresh(SG_T, sg_id)

        cons_item = _find_tree_item(self.panel.tree, CONS_T, cons_id)
        self.assertIsNotNone(cons_item)
        self.assertFalse(cons_item.isExpanded(),
            "adding a safeguard must not auto-open the consequence that owns it")

    def test_selecting_a_cause_still_does_not_force_open_by_itself(self):
        """A CAUSE_T target is ALSO in the collapse-by-default set (it
        owns consequences/safeguards one level down) — revealing an
        existing cause must not force its own ancestor chain open either."""
        node_id, dev_id, cause_id = self._chain()
        self.panel.refresh()

        self.panel.refresh(CAUSE_T, cause_id)

        dev_item = _find_tree_item(self.panel.tree, DEV_T, dev_id)
        self.assertIsNotNone(dev_item)
        self.assertFalse(dev_item.isExpanded(),
            "selecting a cause must not auto-open the deviation that owns it")

    def test_selecting_a_deviation_still_reveals_down_to_it(self):
        """Nod/Ledord/Utrustning/Avvikelse ("objektet") are explicitly
        NOT in the collapse-by-default set — this must keep working
        exactly as before. Orsak is deliberately NOT the cutoff here:
        the user's own wording lists "orsakstexten" alongside konsekvens
        and safeguards as things to skip, so a cause's OWN row — not
        just its children — stays collapsed-by-default too (covered by
        test_selecting_a_cause_still_does_not_force_open_by_itself)."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.panel.refresh()

        self.panel.refresh(DEV_T, dev_id)

        node_item = _find_tree_item(self.panel.tree, NODE_T, node_id)
        dev_item = _find_tree_item(self.panel.tree, DEV_T, dev_id)
        self.assertIsNotNone(node_item)
        self.assertIsNotNone(dev_item)
        self.assertTrue(node_item.isExpanded(),
            "the deviation ('objektet' level) must still auto-open")

    def test_already_manually_expanded_branch_still_scrolls_normally(self):
        """A branch the user already opened themselves must not be
        fought — scrolling to a new item inside it is a convenience, not
        new unfolding, since nothing collapsed gets forced open."""
        node_id, dev_id, cause_id = self._chain()
        self.panel.refresh()
        cause_item = _find_tree_item(self.panel.tree, CAUSE_T, cause_id)
        cause_item.setExpanded(True)

        cons_id = self.db.add_consequence(cause_id)
        self.panel.refresh(CONS_T, cons_id)

        cause_item_after = _find_tree_item(self.panel.tree, CAUSE_T, cause_id)
        self.assertTrue(cause_item_after.isExpanded(),
            "a branch already open before the add must stay open")

    def test_manual_expand_all_button_still_opens_everything(self):
        """The existing "expandera allt"/expandAll() escape hatch must be
        completely unaffected by this change."""
        node_id, dev_id, cause_id = self._chain()
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        self.panel.refresh()

        self.panel.tree.expandAll()

        for type_, id_ in ((NODE_T, node_id), (DEV_T, dev_id), (CAUSE_T, cause_id), (CONS_T, cons_id)):
            item = _find_tree_item(self.panel.tree, type_, id_)
            self.assertTrue(item.isExpanded())

    def test_selecting_object_collapses_consequences_until_arrow_is_used(self):
        _node_id, _dev_id, cause_id = self._chain()
        self.db.add_consequence(cause_id)
        self.panel.refresh()
        cause_item = _find_tree_item(self.panel.tree, CAUSE_T, cause_id)
        cause_item.setExpanded(True)

        self.panel.tree.setCurrentItem(cause_item)

        self.assertFalse(cause_item.isExpanded(),
            "selecting an object/cause must leave consequences behind its arrow")
        cause_item.setExpanded(True)  # explicit disclosure-arrow equivalent
        self.assertTrue(cause_item.isExpanded(),
            "manual expansion must remain available")

    def test_selecting_consequence_collapses_safeguards_until_arrow_is_used(self):
        _node_id, _dev_id, cause_id = self._chain()
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        self.panel.refresh()
        cons_item = _find_tree_item(self.panel.tree, CONS_T, cons_id)
        cons_item.setExpanded(True)

        self.panel.tree.setCurrentItem(cons_item)

        self.assertFalse(cons_item.isExpanded(),
            "selecting a consequence must leave barriers behind its arrow")
        cons_item.setExpanded(True)
        self.assertTrue(cons_item.isExpanded(),
            "manual expansion must remain available")


class TreeInternalReparentDragDropTests(unittest.TestCase):
    """"implementera även drag and drop I hazop trädet mellan olika nivåer.
    drar man konsekvens till ett objekt skall exempelvis både konsekvens
    och safeguard hänga med. håller man inne shift och drar skall det
    kopieras" (2026-08-17). Reuses the same move_cause_to_deviation/
    move_consequence/move_safeguard and copy_cause/copy_consequence/
    copy_safeguard DB methods the tree's own right-click Kopiera/Klistra
    in feature already uses (ScenarioTablePanel's near-identical drag/drop
    was the template for the eventFilter shape)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_tree_dnd_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.panel = TreePanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _drop(self, text, target_item, shift=False, event_type=None):
        # A bare, never-shown TreePanel has no real widget geometry, so
        # visualItemRect()-based hit testing returns degenerate rects —
        # mock itemAt() directly instead, same convention already used by
        # TreePanelEquipmentGroupingTests for this exact reason.
        from PyQt6.QtCore import QEvent, QMimeData, QPointF
        mime = QMimeData()
        mime.setText(text)
        event = unittest.mock.MagicMock()
        event.type.return_value = event_type or QEvent.Type.Drop
        event.mimeData.return_value = mime
        event.position.return_value = QPointF(0, 0)
        modifiers = (Qt.KeyboardModifier.ShiftModifier if shift
                     else Qt.KeyboardModifier.NoModifier)
        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=target_item), \
             unittest.mock.patch('hazop.QApplication.keyboardModifiers', return_value=modifiers):
            handled = self.panel.eventFilter(self.panel.tree.viewport(), event)
        return handled, event

    def test_mime_data_only_encodes_cause_cons_sg(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.panel.refresh()
        dev_item = _find_tree_item(self.panel.tree, DEV_T, dev_id)
        node_item = _find_tree_item(self.panel.tree, NODE_T, node_id)
        self.assertEqual(self.panel.tree.mimeData([dev_item]).text(), '')
        self.assertEqual(self.panel.tree.mimeData([node_item]).text(), '')
        self.assertEqual(self.panel.tree.mimeData([dev_item, node_item]).text(), '',
                          "multi-selection must not produce a mime payload")

    def test_drag_cause_onto_different_deviation_moves_it(self):
        node_id = self.db.add_node()
        dev_a, dev_b = self.db.deviations(node_id)[0]['id'], self.db.deviations(node_id)[1]['id']
        cause_id = self.db.add_cause(dev_a)
        self.panel.refresh()
        dev_b_item = _find_tree_item(self.panel.tree, DEV_T, dev_b)

        handled, event = self._drop(f'hzp:treeitem:{CAUSE_T}:{cause_id}', dev_b_item)

        self.assertTrue(handled)
        event.acceptProposedAction.assert_called_once()
        self.assertEqual(self.db.get_cause(cause_id)['deviation_id'], dev_b)

    def test_drag_consequence_onto_different_cause_brings_safeguard_along(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_a = self.db.add_cause(dev_id)
        cause_b = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_a)
        sg_id = self.db.add_safeguard(cons_id)
        self.panel.refresh()
        cause_b_item = _find_tree_item(self.panel.tree, CAUSE_T, cause_b)

        handled, event = self._drop(f'hzp:treeitem:{CONS_T}:{cons_id}', cause_b_item)

        self.assertTrue(handled)
        event.acceptProposedAction.assert_called_once()
        self.assertEqual(self.db.get_consequence(cons_id)['cause_id'], cause_b)
        # The safeguard was never touched — it still points at the SAME
        # consequence id, so it "follows" for free.
        self.assertEqual(self.db.get_safeguard(sg_id)['consequence_id'], cons_id)

    def test_drag_safeguard_onto_different_consequence_moves_it(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        cons_a = self.db.add_consequence(cause_id)
        cons_b = self.db.add_consequence(cause_id)
        sg_id = self.db.add_safeguard(cons_a)
        self.panel.refresh()
        cons_b_item = _find_tree_item(self.panel.tree, CONS_T, cons_b)

        handled, event = self._drop(f'hzp:treeitem:{SG_T}:{sg_id}', cons_b_item)

        self.assertTrue(handled)
        self.assertEqual(self.db.get_safeguard(sg_id)['consequence_id'], cons_b)

    def test_shift_drag_copies_instead_of_moving(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_a = self.db.add_cause(dev_id)
        cause_b = self.db.add_cause(dev_id)
        cons_id = self.db.add_consequence(cause_a)
        self.db.update_consequence(cons_id, "Original", 3)
        self.panel.refresh()
        cause_b_item = _find_tree_item(self.panel.tree, CAUSE_T, cause_b)

        handled, event = self._drop(f'hzp:treeitem:{CONS_T}:{cons_id}', cause_b_item, shift=True)

        self.assertTrue(handled)
        # Original untouched, still under cause_a.
        self.assertEqual(self.db.get_consequence(cons_id)['cause_id'], cause_a)
        # A NEW, independent copy now exists under cause_b.
        copies = [c for c in self.db.consequences(cause_b) if c['source_id'] == cons_id]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0]['description'], "Original")

    def test_dropping_onto_same_parent_is_a_noop(self):
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.panel.refresh()
        dev_item = _find_tree_item(self.panel.tree, DEV_T, dev_id)

        handled, event = self._drop(f'hzp:treeitem:{CAUSE_T}:{cause_id}', dev_item)

        self.assertTrue(handled)
        event.ignore.assert_called_once()
        event.acceptProposedAction.assert_not_called()

    def test_drag_cause_onto_node_uses_or_creates_ovrigt_deviation(self):
        node_a = self.db.add_node()
        node_b = self.db.add_node()
        dev_a = self.db.deviations(node_a)[0]['id']
        cause_id = self.db.add_cause(dev_a)
        self.panel.refresh()
        node_b_item = _find_tree_item(self.panel.tree, NODE_T, node_b)

        handled, event = self._drop(f'hzp:treeitem:{CAUSE_T}:{cause_id}', node_b_item)

        self.assertTrue(handled)
        event.acceptProposedAction.assert_called_once()
        new_dev_id = self.db.get_cause(cause_id)['deviation_id']
        self.assertEqual(self.db.get_deviation(new_dev_id)['node_id'], node_b)

    def test_drag_move_over_target_accepts_without_writing_to_db(self):
        """DragMove is only ever hover feedback — it must never write to
        the DB just because the cursor passed over a valid target."""
        from PyQt6.QtCore import QEvent
        node_id = self.db.add_node()
        dev_a, dev_b = self.db.deviations(node_id)[0]['id'], self.db.deviations(node_id)[1]['id']
        cause_id = self.db.add_cause(dev_a)
        self.panel.refresh()
        dev_b_item = _find_tree_item(self.panel.tree, DEV_T, dev_b)

        handled, event = self._drop(f'hzp:treeitem:{CAUSE_T}:{cause_id}', dev_b_item,
                                     event_type=QEvent.Type.DragMove)

        self.assertTrue(handled)
        event.acceptProposedAction.assert_called_once()
        self.assertEqual(self.db.get_cause(cause_id)['deviation_id'], dev_a,
                          "hovering during drag must not move the cause yet")


class TreePanelAutoCollapseTests(unittest.TestCase):
    """Two independent "Auto-collapse" toggles below the tree (2026-08-24,
    see NOTES.md "Åtta UX/logik-förbättringar", split into two the same
    day per follow-up feedback: "Autocollapse funktion funkar bra med att
    öppna mellan noder ... Men den funkar inte för avikelser").

    2026-08-25 follow-up (see NOTES.md): the "avvikelser" toggle's FIRST
    fix made it setHidden() every inactive deviation — Anton didn't want
    that ("den ska inte dölja avikelser utan den ska dölja orsaks-nivån
    och nedåt... Så står jag på högt flöde skall jag bara se orsaker på
    högt flöde"). Every deviation must stay visible as a sibling; only
    the CAUSE level and below collapses away for whichever deviation
    isn't the active one — via setExpanded(False) on the DEV_T item
    itself, exactly mirroring how "nodes" already treats SYSTEM_T/NODE_T.
    Both toggles default off and leave the existing expand/collapse
    behavior untouched."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_autocollapse_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.panel = TreePanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_both_off_by_default_and_persisted_via_app_config(self):
        self.assertFalse(self.panel._auto_collapse_nodes_chk.isChecked())
        self.assertFalse(self.panel._auto_collapse_deviations_chk.isChecked())
        self.assertEqual(self.db.get_config('tree_auto_collapse_nodes', '0'), '0')
        self.assertEqual(self.db.get_config('tree_auto_collapse_deviations', '0'), '0')

    def test_enabling_each_persists_to_its_own_app_config_key(self):
        self.panel._auto_collapse_nodes_chk.setChecked(True)
        self.assertEqual(self.db.get_config('tree_auto_collapse_nodes', '0'), '1')
        self.assertEqual(self.db.get_config('tree_auto_collapse_deviations', '0'), '0',
            "toggling nodes must not also flip the deviations key")

        self.panel._auto_collapse_deviations_chk.setChecked(True)
        self.assertEqual(self.db.get_config('tree_auto_collapse_deviations', '0'), '1')

    def test_nodes_toggle_collapses_all_nodes_except_the_selected_one(self):
        node_a = self.db.add_node()
        node_b = self.db.add_node()
        self.panel.refresh()
        item_a = _find_tree_item(self.panel.tree, NODE_T, node_a)
        item_b = _find_tree_item(self.panel.tree, NODE_T, node_b)
        self.panel.tree.setCurrentItem(item_a)

        self.panel._auto_collapse_nodes_chk.setChecked(True)

        self.assertTrue(item_a.isExpanded(), "the active node must stay expanded")
        self.assertFalse(item_b.isExpanded(), "an inactive node must fold away")

    def test_nodes_toggle_alone_does_not_hide_deviations_within_active_node(self):
        """The reported bug: with only ONE combined toggle, deviations
        within the still-expanded active node never visibly folded away.
        With the toggles now separate, "nodes" alone must leave every
        deviation in the active node visible — hiding them is the OTHER
        toggle's job."""
        node_id = self.db.add_node()
        devs = self.db.deviations(node_id)
        self.panel.refresh()
        dev_item_0 = _find_tree_item(self.panel.tree, DEV_T, devs[0]['id'])
        dev_item_1 = _find_tree_item(self.panel.tree, DEV_T, devs[1]['id'])
        self.panel.tree.setCurrentItem(dev_item_0)

        self.panel._auto_collapse_nodes_chk.setChecked(True)

        self.assertFalse(dev_item_0.isHidden())
        self.assertFalse(dev_item_1.isHidden(),
            "the 'nodes' toggle alone must not hide sibling deviations")

    def test_deviations_toggle_collapses_causes_but_keeps_every_deviation_visible(self):
        """2026-08-25 fix, see NOTES.md: "den ska inte dölja avikelser
        utan den ska dölja orsaks-nivån och nedåt" — every deviation
        stays visible as a sibling; only the inactive one's own children
        (causes) collapse away, via setExpanded(False) on the DEV_T item
        itself, not setHidden()."""
        node_id = self.db.add_node()
        devs = self.db.deviations(node_id)
        self.db.add_cause(devs[0]['id'])
        self.db.add_cause(devs[1]['id'])
        self.panel.refresh()
        dev_item_0 = _find_tree_item(self.panel.tree, DEV_T, devs[0]['id'])
        dev_item_1 = _find_tree_item(self.panel.tree, DEV_T, devs[1]['id'])
        self.panel.tree.setCurrentItem(dev_item_0)

        self.panel._auto_collapse_deviations_chk.setChecked(True)

        self.assertFalse(dev_item_0.isHidden(), "the active deviation must stay visible")
        self.assertFalse(dev_item_1.isHidden(),
            "an inactive deviation must ALSO stay visible — only its causes collapse")
        self.assertTrue(dev_item_0.isExpanded(), "the active deviation's causes must show")
        self.assertFalse(dev_item_1.isExpanded(),
            "an inactive deviation's causes (and everything below) must collapse")

    def test_deviations_toggle_collapses_causes_from_all_objects_sharing_one_avvikelse(self):
        """2026-08-25 (see NOTES.md 'Rättar ihopslagningen'): objects
        sharing one avvikelse text now contribute their causes to the
        SAME avvikelse row (not one row each, see
        TreePanelEquipmentGroupingTests) — collapsing an inactive
        avvikelse must hide ALL of those causes together, and the row
        itself must never be hidden (only setExpanded, never setHidden,
        same as any other avvikelse)."""
        from hazop import _create_tagged_cause
        pump_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        valve_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        pump_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=pump_id)
        valve_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=valve_id)
        pump_cause, _c1 = _create_tagged_cause(self.db, pump_dev, "Pump", "P-101")
        _valve_cause, _c2 = _create_tagged_cause(self.db, valve_dev, "Ventil", "V-101")
        other_dev = next(d for d in self.db.deviations(node_id) if d['description'] == "Högt flöde")
        self.panel.refresh()
        avvikelse_item = _find_tree_item(self.panel.tree, CAUSE_T, pump_cause).parent()
        other_item = _find_tree_item(self.panel.tree, DEV_T, other_dev['id'])
        self.panel.tree.setCurrentItem(other_item)   # a DIFFERENT avvikelse is now active

        self.panel._auto_collapse_deviations_chk.setChecked(True)

        self.assertFalse(avvikelse_item.isHidden(), "an inactive avvikelse row must stay visible")
        self.assertFalse(avvikelse_item.isExpanded(),
            "an inactive avvikelse's causes (from ALL contributing objects) must collapse together")

    def test_deviations_toggle_behaves_like_nodes_toggle_between_sibling_avvikelser(self):
        """2026-08-25 follow-up, see NOTES.md: Anton — "När jag klickar på
        lågt flöde skall de på lågt flöde öppnas och ligger det
        exempelvis avvikelser på låg nivå skall dessa stängas" — verified
        already true (see NOTES.md) using the exact named guide words
        from the request; locked in here as a real regression test."""
        node_id = self.db.add_node()
        devs = {d['description']: d for d in self.db.deviations(node_id)}
        lagt_flode = devs["Lågt flöde"]
        lag_niva = devs["Låg nivå"]
        c1 = self.db.add_cause(lagt_flode['id'])
        self.db.update_cause(c1, description="Pump stannar")
        c2 = self.db.add_cause(lag_niva['id'])
        self.db.update_cause(c2, description="Nivåmätare felar")
        self.panel.refresh()
        lagt_item = _find_tree_item(self.panel.tree, DEV_T, lagt_flode['id'])
        niva_item = _find_tree_item(self.panel.tree, DEV_T, lag_niva['id'])
        self.panel._auto_collapse_deviations_chk.setChecked(True)

        self.panel.tree.setCurrentItem(lagt_item)
        self.assertTrue(lagt_item.isExpanded(), "clicking 'Lågt flöde' must open it")
        self.assertFalse(niva_item.isExpanded(), "'Låg nivå' must fold away")

        self.panel.tree.setCurrentItem(niva_item)
        self.assertFalse(lagt_item.isExpanded(), "'Lågt flöde' must now fold away")
        self.assertTrue(niva_item.isExpanded(), "clicking 'Låg nivå' must open it")

    def test_switching_selection_live_recollapses_previous_deviation(self):
        """Re-applied from _on_select too, not just refresh() — clicking a
        different deviation must collapse the previous one's causes
        immediately, without waiting for unrelated data to change."""
        node_id = self.db.add_node()
        devs = self.db.deviations(node_id)
        self.db.add_cause(devs[0]['id'])
        self.db.add_cause(devs[1]['id'])
        self.panel.refresh()
        dev_item_0 = _find_tree_item(self.panel.tree, DEV_T, devs[0]['id'])
        dev_item_1 = _find_tree_item(self.panel.tree, DEV_T, devs[1]['id'])
        self.panel.tree.setCurrentItem(dev_item_0)
        self.panel._auto_collapse_deviations_chk.setChecked(True)
        self.assertTrue(dev_item_0.isExpanded())

        self.panel.tree.setCurrentItem(dev_item_1)

        self.assertFalse(dev_item_0.isHidden())
        self.assertFalse(dev_item_1.isHidden())
        self.assertFalse(dev_item_0.isExpanded())
        self.assertTrue(dev_item_1.isExpanded())

    def test_disabling_deviations_toggle_stops_forcing_collapse(self):
        node_id = self.db.add_node()
        devs = self.db.deviations(node_id)
        self.db.add_cause(devs[0]['id'])
        self.db.add_cause(devs[1]['id'])
        self.panel.refresh()
        dev_item_0 = _find_tree_item(self.panel.tree, DEV_T, devs[0]['id'])
        dev_item_1 = _find_tree_item(self.panel.tree, DEV_T, devs[1]['id'])
        self.panel.tree.setCurrentItem(dev_item_0)
        self.panel._auto_collapse_deviations_chk.setChecked(True)
        self.assertFalse(dev_item_1.isExpanded())
        self.assertFalse(dev_item_1.isHidden())

        self.panel._auto_collapse_deviations_chk.setChecked(False)
        dev_item_1.setExpanded(True)
        self.panel.tree.setCurrentItem(dev_item_0)

        self.assertTrue(dev_item_1.isExpanded(),
            "a disabled toggle must not keep forcing the collapse it applied while on")

    def test_both_disabled_leaves_expand_all_alone(self):
        node_a = self.db.add_node()
        node_b = self.db.add_node()
        self.panel.refresh()
        self.panel.tree.expandAll()
        item_a = _find_tree_item(self.panel.tree, NODE_T, node_a)
        item_b = _find_tree_item(self.panel.tree, NODE_T, node_b)
        self.assertTrue(item_a.isExpanded())
        self.assertTrue(item_b.isExpanded())


class TreePanelAddCauseButtonTests(unittest.TestCase):
    """"+ Orsak" button above the tree (2026-08-24, see NOTES.md "Åtta
    UX/logik-förbättringar") — alongside the existing "+ Nod"/"+
    Avvikelse" buttons, wired straight to TreePanel's own already-existing
    add_cause() (previously only reachable via right-click on a DEV_T
    row)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_addcausebtn_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.panel = TreePanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _button(self, label):
        for btn in self.panel.findChildren(QPushButton):
            if btn.text() == label:
                return btn
        return None

    def test_button_exists_alongside_nod_and_avvikelse(self):
        self.assertIsNotNone(self._button("+ Nod"))
        self.assertIsNotNone(self._button("+ Avvikelse"))
        self.assertIsNotNone(self._button("+ Orsak"))

    def test_clicking_button_with_a_deviation_selected_adds_a_cause(self):
        """2026-08-24 (see NOTES.md): add_cause() used to open a
        StandardCausesPickerPopup dialog ("Lägg till orsak på P&ID") —
        removed at Anton's request, now creates a blank cause directly,
        no dialog, same as the "+ Avvikelse"/"+ Nod" buttons."""
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.panel.refresh()
        dev_item = _find_tree_item(self.panel.tree, DEV_T, dev_id)
        self.panel.tree.setCurrentItem(dev_item)
        before = len(self.db.causes_for_deviation(dev_id))

        self._button("+ Orsak").click()

        causes = self.db.causes_for_deviation(dev_id)
        self.assertEqual(len(causes), before + 1)
        self.assertEqual(len(self.db.consequences(causes[-1]['id'])), 1,
            "must also auto-create an empty consequence, same as the picker used to")

    def test_clicking_button_with_no_deviation_selected_shows_a_hint_not_a_crash(self):
        with unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
            self._button("+ Orsak").click()
        mock_info.assert_called_once()


class TreePanelSystemHierarchyTests(unittest.TestCase):
    """New top-level "System" category above Nod (2026-08-24, see
    NOTES.md "Ny toppnivå System") — System → Nod → Avvikelse → ...
    Ungrouped nodes (system_id IS NULL, e.g. any project saved before
    this feature) still render as their own top-level items, exactly as
    every node did before Systems existed."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_systemtree_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.panel = TreePanel(self.db)
        # A fresh Database auto-seeds one default system+node — strip it
        # so each test builds its own controlled, exhaustive hierarchy.
        for n in self.db.nodes():
            self.db.delete_node(n['id'])
        for s in self.db.systems():
            self.db.delete_system(s['id'])

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _button(self, label):
        for btn in self.panel.findChildren(QPushButton):
            if btn.text() == label:
                return btn
        return None

    def test_system_button_exists(self):
        self.assertIsNotNone(self._button("+ System"))

    def test_system_renders_as_top_level_item_with_node_nested(self):
        sid = self.db.add_system("Reaktorsystem")
        node_id = self.db.add_node(system_id=sid)
        self.panel.refresh()

        sitem = _find_tree_item(self.panel.tree, SYSTEM_T, sid)
        self.assertIsNotNone(sitem)
        self.assertIsNone(sitem.parent(), "a System must be a top-level tree item")
        self.assertIn("Reaktorsystem", sitem.text(0))

        nitem = _find_tree_item(self.panel.tree, NODE_T, node_id)
        self.assertIsNotNone(nitem)
        self.assertIs(nitem.parent(), sitem, "the node must nest under its System")

    def test_ungrouped_node_still_renders_top_level(self):
        node_id = self.db.add_node()  # no system_id
        self.panel.refresh()

        nitem = _find_tree_item(self.panel.tree, NODE_T, node_id)
        self.assertIsNotNone(nitem)
        self.assertIsNone(nitem.parent(),
            "a node with no system must render exactly as it did before Systems existed")

    def test_add_system_button_creates_and_selects_a_new_system(self):
        self._button("+ System").click()
        systems = self.db.systems()
        self.assertEqual(len(systems), 1)
        current_type = self.panel.tree.currentItem().data(0, Qt.ItemDataRole.UserRole + 1)
        current_id = self.panel.tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
        self.assertEqual(current_type, SYSTEM_T)
        self.assertEqual(current_id, systems[0]['id'])

    def test_add_node_button_with_a_system_selected_places_node_under_it(self):
        sid = self.db.add_system("Reaktorsystem")
        self.panel.refresh()
        sitem = _find_tree_item(self.panel.tree, SYSTEM_T, sid)
        self.panel.tree.setCurrentItem(sitem)

        self._button("+ Nod").click()

        nodes = self.db.nodes()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]['system_id'], sid)

    def test_add_node_button_with_a_node_selected_keeps_the_same_system(self):
        """Clicking "+ Nod" while a node (not the System row itself) is
        selected must still resolve the owning System, same "derive from
        where you're standing" convention add_cause()/add_consequence()
        already use for node/avvikelse."""
        sid = self.db.add_system("Reaktorsystem")
        first_node_id = self.db.add_node(system_id=sid)
        self.panel.refresh()
        nitem = _find_tree_item(self.panel.tree, NODE_T, first_node_id)
        self.panel.tree.setCurrentItem(nitem)

        self._button("+ Nod").click()

        nodes = self.db.nodes()
        self.assertEqual(len(nodes), 2)
        self.assertTrue(all(n['system_id'] == sid for n in nodes))

    def test_add_node_button_with_nothing_relevant_selected_is_ungrouped(self):
        self._button("+ Nod").click()
        nodes = self.db.nodes()
        self.assertEqual(len(nodes), 1)
        self.assertIsNone(nodes[0]['system_id'])

    def test_rename_system_via_context_menu(self):
        sid = self.db.add_system("Original")
        with unittest.mock.patch('tree_panel.QInputDialog.getText',
                                  return_value=("Nytt namn", True)):
            self.panel._rename_system(sid)
        systems = {s['id']: s for s in self.db.systems()}
        self.assertEqual(systems[sid]['name'], "Nytt namn")

    def test_delete_system_via_delete_selected_keeps_its_nodes(self):
        sid = self.db.add_system("Temporärt")
        node_id = self.db.add_node(system_id=sid)
        self.panel.refresh()
        sitem = _find_tree_item(self.panel.tree, SYSTEM_T, sid)
        self.panel.tree.setCurrentItem(sitem)

        with unittest.mock.patch.object(QMessageBox, 'question',
                                         return_value=QMessageBox.StandardButton.Yes):
            self.panel.delete_selected()

        self.assertEqual(len(self.db.systems()), 0)
        self.assertIsNone(self.db.get_node(node_id)['system_id'])
        self.assertIsNotNone(self.db.get_node(node_id))

    def test_auto_collapse_nodes_toggle_also_collapses_systems(self):
        sid_a = self.db.add_system("A")
        sid_b = self.db.add_system("B")
        self.db.add_node(system_id=sid_a)
        self.db.add_node(system_id=sid_b)
        self.panel.refresh()
        item_a = _find_tree_item(self.panel.tree, SYSTEM_T, sid_a)
        item_b = _find_tree_item(self.panel.tree, SYSTEM_T, sid_b)
        self.panel.tree.setCurrentItem(item_a)

        self.panel._auto_collapse_nodes_chk.setChecked(True)

        self.assertTrue(item_a.isExpanded(), "the active system must stay expanded")
        self.assertFalse(item_b.isExpanded(), "an inactive system must fold away")


class TreePanelRefreshQueryBatchingTests(unittest.TestCase):
    """TreePanel.refresh() used to issue one SELECT per node (deviations),
    per deviation (causes), per cause (consequences), and per consequence
    (safeguards) — an N+1 pattern that ran on nearly every tree rebuild.
    Batched into 4 bulk queries total (2026-08-24, see NOTES.md,
    Database._fetch_grouped) — this locks in that the query count stays
    bounded as the tree grows, instead of scaling with row count."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_refreshbatch_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        self.panel = TreePanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _add_full_chains(self, n_nodes, causes_per_node=2):
        for _ in range(n_nodes):
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            for _ in range(causes_per_node):
                cause_id = self.db.add_cause(dev_id)
                cons_id = self.db.add_consequence(cause_id)
                self.db.add_safeguard(cons_id)

    def test_query_count_does_not_scale_with_tree_size(self):
        self._add_full_chains(n_nodes=2, causes_per_node=2)
        small_tree_count = count_selects(self.db, self.panel.refresh)

        self._add_full_chains(n_nodes=20, causes_per_node=2)
        large_tree_count = count_selects(self.db, self.panel.refresh)

        self.assertLess(large_tree_count, small_tree_count + 15,
            f"refresh() SELECT count grew with tree size ({small_tree_count} "
            f"-> {large_tree_count}) — the N+1 query pattern may have regressed")


if __name__ == "__main__":
    unittest.main()
