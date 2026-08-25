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
    _TempDbMainWindow, _find_tree_item, count_selects,
)

class TreePanelEquipmentGroupingTests(unittest.TestCase):
    """TreePanel.refresh() groups a node's deviations by guide-word text
    FIRST (LEDORD_T — several deviation rows across different equipment can
    share one description), then within each guide word, by equipment_id
    (EQUIP_T) — see NOTES.md 'Nod → Ledord → Utrustning'. The LEDORD_T
    wrapper is skipped entirely when there's nothing to group — a single,
    plain (no equipment) deviation for a guide word attaches directly to
    the node instead, to avoid showing the same guide-word text twice in a
    row for no reason (see NOTES.md's follow-up 'varför är det dubbelt?').
    It reappears as soon as a second deviation (equipment-scoped, or
    another plain one) shares that same guide word."""

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

    def test_equipment_scoped_deviation_renders_as_flat_row_under_node(self):
        """2026-08-25 (see NOTES.md 'Slå ihop objekt-rad + avvikelse-rad'):
        a single deviation for this equipment+guide-word combo (the
        overwhelmingly common case — get_or_create_deviation is idempotent
        per node+description+equipment) is ONE flat row directly under the
        node — no separate LEDORD_T wrapper, no separate EQUIP_T item.
        The row's label combines the tag with the deviation text, and it
        carries the DEVIATION's identity (not EQUIP_T) so 'add cause' and
        equipment-dropped-on-deviation both work directly on it."""
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        items = self._tree_items()
        self.assertEqual(len([x for x in items if x[0] == LEDORD_T
                              and str(x[1]).endswith("Lågt flöde")]), 0,
            "an equipment-linked guide word must no longer get a LEDORD_T wrapper")
        self.assertEqual(len([x for x in items if x[0] == EQUIP_T and x[1] == eq_id]), 0,
            "a single deviation must not get a separate EQUIP_T row either")
        dev_rows = [x for x in items if x[0] == DEV_T and x[1] == dev_id]
        self.assertEqual(len(dev_rows), 1)
        self.assertEqual(dev_rows[0][2], NODE_T,
            "the flat equipment+deviation row must sit directly under the node")
        self.assertEqual(self.panel._resolve_equipment_id(DEV_T, dev_id), eq_id,
            "the flat row's underlying deviation must still resolve back to its equipment")

    def test_two_equipment_sharing_same_guide_word_render_as_separate_flat_rows(self):
        """2026-08-25, confirmed via AskUserQuestion (see NOTES.md 'Slå
        ihop objekt-rad + avvikelse-rad'): a pump AND a valve that both
        have 'Lågt flöde' under the same node no longer share a grouped
        guide-word heading — each gets its own independent flat row, even
        though they read the exact same deviation text. This is a
        deliberate reversal of the 2026-08-13 grouped-numbering
        preference for the object-linked case specifically."""
        pump_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        valve_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        pump_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=pump_id)
        valve_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=valve_id)
        self.panel.refresh()

        items = self._tree_items()
        self.assertEqual(len([x for x in items if x[0] == LEDORD_T
                              and str(x[1]).endswith("Lågt flöde")]), 0,
            "no shared guide-word heading must exist anymore for the object-linked case")
        dev_rows = [x for x in items if x[0] == DEV_T and x[2] == NODE_T
                    and x[1] in (pump_dev, valve_dev)]
        self.assertEqual({x[1] for x in dev_rows}, {pump_dev, valve_dev},
            "both must render as their own flat rows directly under the node")
        self.assertEqual(self.panel._resolve_equipment_id(DEV_T, pump_dev), pump_id)
        self.assertEqual(self.panel._resolve_equipment_id(DEV_T, valve_dev), valve_id)

    def test_numbering_stays_sequential_and_visible_after_linking_equipment(self):
        """Bug report (2026-08-13): 'Nummereringen av lågt flöde, högt
        flöde osv blir konstig när man lägger till objekt i trädet.'
        First fix attempt made the merged equipment row simply consume
        no number at all — which then made a SECOND report surface:
        'jag vill att den ska kvarstå så att det alltid syns att det är
        exempelvis 16 avikelser' (the guide word's own number must stay
        visible even once equipment is linked, not disappear).

        2026-08-25 (see NOTES.md 'Slå ihop objekt-rad + avvikelse-rad'):
        the flat equipment row now carries this SAME number directly in
        its own combined label (no more separate wrapper to carry it
        instead) — 'Lågt flöde' becomes '1. V-101 — Lågt flöde' in place,
        still first in the sequence; the next plain guide word
        ('Högt flöde') still continues right after it."""
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        lagt_item = hogt_item = None
        while it.value():
            item = it.value()
            t = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if (t == DEV_T and item.parent() is not None
                    and item.parent().data(0, Qt.ItemDataRole.UserRole + 1) == NODE_T):
                if "Lågt flöde" in item.text(0):
                    lagt_item = item
                if "Högt flöde" in item.text(0):
                    hogt_item = item
            it += 1
        self.assertIsNotNone(lagt_item, "'Lågt flöde' must still be a flat row directly under the node")
        self.assertIn("1. V-101 — Lågt flöde", lagt_item.text(0),
            f"the guide word's own number must stay visible on the flat row, got: {lagt_item.text(0)!r}")
        self.assertIsNotNone(hogt_item, "'Högt flöde' must still attach directly to the node")
        self.assertIn("2. Högt flöde", hogt_item.text(0),
            f"expected the next sequential number, got: {hogt_item.text(0)!r}")

    def test_all_seeded_guide_words_stay_numbered_one_through_sixteen(self):
        """Direct check of the user's own framing: 'jag vill att den ska
        kvarstå så att det alltid syns att det är exempelvis 16
        avikelser om jag inte lägger till nya avikelser i trädet' — a
        fresh node's 16 auto-seeded guide words must show as a gapless
        1..16 sequence, and linking equipment to any ONE of them (moving
        it from the plain to the wrapped rendering path) must not change
        that count or leave a gap/duplicate anywhere."""
        import re
        # 2026-08-24: Database now auto-seeds one default node on a brand
        # new project (see Database.__init__'s pre_existing_db check), so
        # self.db already has a first node with its own 1..16 numbering
        # before this test adds its OWN node — scope the scan to just this
        # test's node (not the whole tree) so the two don't double up.
        node_id = self.db.add_node()
        n_seeded = len(self.db.deviations(node_id))
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        node_item = _find_tree_item(self.panel.tree, NODE_T, node_id)
        self.assertIsNotNone(node_item)

        def _is_within(item, ancestor):
            p = item.parent()
            while p is not None:
                if p is ancestor:
                    return True
                p = p.parent()
            return False

        numbers = []
        it = QTreeWidgetItemIterator(self.panel.tree)
        while it.value():
            item = it.value()
            t = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if t in (DEV_T, LEDORD_T) and _is_within(item, node_item):
                m = re.search(r'(\d+)\.\s', item.text(0))
                if m:
                    numbers.append(int(m.group(1)))
            it += 1
        self.assertEqual(sorted(numbers), list(range(1, n_seeded + 1)),
            f"expected a gapless 1..{n_seeded} sequence, got: {sorted(numbers)}")

    def test_flat_equipment_deviation_row_combines_tag_and_deviation_text(self):
        """2026-08-25 (see NOTES.md 'Slå ihop objekt-rad + avvikelse-rad'):
        with the LEDORD_T wrapper gone, the deviation text ('Lågt flöde')
        no longer appears anywhere else — the flat row's own label must
        show BOTH the tag and the deviation text together ('M1.GPA6 —
        Lågt flöde'), not just the tag alone (the old, now-obsolete
        'don't repeat the guide-word text' rule from 2026-08-09 applied
        to a different tree shape that no longer exists)."""
        eq_id = self.db.add_equipment_item("M1.GPA6", "M1.GPA6", "M1", 0, "Pump", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.db.add_cause(dev_id)
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        dev_item = None
        while it.value():
            item = it.value()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) == DEV_T
                    and item.data(0, Qt.ItemDataRole.UserRole) == dev_id):
                dev_item = item
            it += 1
        self.assertIsNotNone(dev_item)
        self.assertIn("M1.GPA6 — Lågt flöde", dev_item.text(0))
        # The cause must be one level directly below the flat row, not two.
        self.assertEqual(dev_item.childCount(), 1)
        cause_item = dev_item.child(0)
        self.assertEqual(cause_item.data(0, Qt.ItemDataRole.UserRole + 1), CAUSE_T)

    def test_trivial_tagged_cause_merges_into_equipment_header_row(self):
        """'Det känns onödigt att objektet redovisas två gånger i
        hierarkin i trädet. Detta går att slå ihop till en.' (2026-08-10,
        screenshot in conversation) — dragging equipment onto a deviation
        (_create_tagged_cause) creates a cause with no real description
        yet, whose own tree label therefore falls back to the SAME
        equipment tag the merged header row above it already shows
        ('=E1.M1.QMA102 — Ventil' followed by '1. E1.M1.QMA102'). One
        more 'kaka på kaka' level: the trivial cause's identity (and its
        consequences) now attaches directly to the header row instead of
        a separate, redundant child."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("E1.M1.QMA102", "E1.M1.QMA102", "E1", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Högt flöde", equipment_id=eq_id)
        cause_id, cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "E1.M1.QMA102")
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        header_item = None
        while it.value():
            item = it.value()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) == CAUSE_T
                    and item.data(0, Qt.ItemDataRole.UserRole) == cause_id):
                header_item = item
            it += 1
        self.assertIsNotNone(header_item,
            "the merged row must carry the CAUSE's identity, not a separate DEV_T row above it")
        self.assertIn("E1.M1.QMA102", header_item.text(0))
        self.assertEqual(len([x for x in self._tree_items() if x[0] == DEV_T and x[1] == dev_id]), 0,
            "no separate DEV_T row should remain once merged into the cause")
        # The consequence must be one level directly below the merged
        # row, not nested under yet another redundant cause row.
        self.assertEqual(header_item.childCount(), 1)
        self.assertEqual(header_item.child(0).data(0, Qt.ItemDataRole.UserRole + 1), CONS_T)

    def test_cause_with_real_description_does_not_merge_into_equipment_header(self):
        """The merge only applies to a genuinely trivial, still-unedited
        placeholder cause — once the user types a real description, the
        cause has its own distinct content and must show as a normal,
        separate child row again."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id, _cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        self.db.update_cause(cause_id, description="Inget flöde till M1.GPA2")
        self.panel.refresh()

        items = self._tree_items()
        dev_rows = [x for x in items if x[0] == DEV_T and x[1] == dev_id]
        self.assertEqual(len(dev_rows), 1,
            "a cause with real content must not collapse its parent DEV_T row")
        cause_rows = [x for x in items if x[0] == CAUSE_T and x[1] == cause_id]
        self.assertEqual(len(cause_rows), 1)
        self.assertEqual(cause_rows[0][2], DEV_T)

    def _find_item(self, type_, id_):
        it = QTreeWidgetItemIterator(self.panel.tree)
        while it.value():
            candidate = it.value()
            if (candidate.data(0, Qt.ItemDataRole.UserRole + 1) == type_
                    and candidate.data(0, Qt.ItemDataRole.UserRole) == id_):
                return candidate
            it += 1
        return None

    def test_double_click_dev_merged_equipment_row_opens_tag_popup_not_inline_edit(self):
        """2026-08-18 bug report: double-clicking a merged equipment-tag
        row (the common case, a single deviation for this equipment+guide
        word, see refresh()'s "kaka på kaka" collapse) opened inline text
        editing of the DEVIATION it happened to be standing in for,
        showing the guide word text ("avvikelsetexten") instead of
        anything related to the tag. It must open the Tag+Typ popup
        instead (CauseTagPopup, 2026-08-18 follow-up — same popup used
        for a tag click in the scenario table), exactly like a genuine
        EQUIP_T row."""
        from hazop import _create_tagged_cause, CauseTagPopup
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id, _cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        self.db.update_cause(cause_id, description="Inget flöde till M1.GPA2")
        self.panel.refresh()

        item = self._find_item(DEV_T, dev_id)
        self.assertIsNotNone(item)

        self.panel._on_item_double_click(item, 0)
        popups = self.panel.findChildren(CauseTagPopup)
        self.assertEqual(len(popups), 1,
            "double-clicking the tag row must open the Tag+Typ popup")
        self.assertEqual(popups[0]._tag_edit.text(), "V-101")
        self.assertIsNone(self.panel._inline_edit_target)
        self.assertEqual(len(self.panel.tree.viewport().findChildren(QLineEdit)), 0)

    def test_double_click_cause_merged_equipment_row_opens_tag_popup_not_inline_edit(self):
        """Same fix, other merge branch — the trivial-cause -> CAUSE_T
        merge (see refresh())."""
        from hazop import _create_tagged_cause, CauseTagPopup
        eq_id = self.db.add_equipment_item("E1.M1.QMA102", "E1.M1.QMA102", "E1", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Högt flöde", equipment_id=eq_id)
        cause_id, _cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "E1.M1.QMA102")
        self.panel.refresh()

        item = self._find_item(CAUSE_T, cause_id)
        self.assertIsNotNone(item)

        self.panel._on_item_double_click(item, 0)
        popups = self.panel.findChildren(CauseTagPopup)
        self.assertEqual(len(popups), 1)
        self.assertEqual(popups[0]._tag_edit.text(), "E1.M1.QMA102")
        self.assertIsNone(self.panel._inline_edit_target)
        self.assertEqual(len(self.panel.tree.viewport().findChildren(QLineEdit)), 0)

    def test_equipment_tag_popup_edits_tag_and_type_live(self):
        """The tag popup has no OK/Avbryt button (2026-08-18 user
        request) — changing the tag (Enter/focus-out) or picking a type
        commits immediately to the equipment_catalog row."""
        from hazop import CauseTagPopup
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()
        item = self._find_item(EQUIP_T, eq_id) or self._equip_item_fallback(eq_id)
        self.assertIsNotNone(item)

        self.panel._on_item_double_click(item, 0)
        popup = self.panel.findChildren(CauseTagPopup)[0]

        popup._tag_edit.setText("V-102")
        popup._tag_edit.editingFinished.emit()
        self.assertEqual(self.db.get_equipment_by_id(eq_id)['tag'], "V-102")

        popup._type_cb.setCurrentIndex(popup._type_cb.findText("Pump"))
        popup._type_cb.activated.emit(popup._type_cb.currentIndex())
        self.assertEqual(self.db.get_equipment_by_id(eq_id)['equipment_type'], "Pump")

    def _equip_item_fallback(self, eq_id):
        """A lone equipment-scoped deviation collapses onto a DEV_T row
        (see refresh()'s "kaka på kaka") rather than staying EQUIP_T —
        find it by its _EQUIP_TAG_ROLE instead when a plain EQUIP_T
        lookup comes up empty."""
        it = QTreeWidgetItemIterator(self.panel.tree)
        while it.value():
            candidate = it.value()
            if candidate.data(0, self.panel._EQUIP_TAG_ROLE) == eq_id:
                return candidate
            it += 1
        return None

    def test_second_trivial_cause_prevents_merge(self):
        """A second cause under the same equipment-scoped deviation means
        there are now two distinct things to show — merging either one
        into the header row would hide the other; both must stay normal,
        separate child rows."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id1, _c1 = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        cause_id2, _c2 = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        self.panel.refresh()

        items = self._tree_items()
        dev_rows = [x for x in items if x[0] == DEV_T and x[1] == dev_id]
        self.assertEqual(len(dev_rows), 1, "two causes must not collapse the parent DEV_T row")
        cause_rows = {x[1] for x in items if x[0] == CAUSE_T and x[1] in (cause_id1, cause_id2)}
        self.assertEqual(cause_rows, {cause_id1, cause_id2})

    def test_merged_cause_header_still_offers_add_cause_in_context_menu(self):
        """The merged row (carrying the CAUSE's identity, see above) must
        still let the user add a SECOND, distinct cause to the same
        deviation — add_cause() already resolves the deviation via the
        cause's own deviation_id regardless of which row type triggered
        it, so this is purely a context-menu-visibility check."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id, _cons_id = _create_tagged_cause(self.db, dev_id, "Ventil", "V-101")
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        header_item = None
        while it.value():
            item = it.value()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) == CAUSE_T
                    and item.data(0, Qt.ItemDataRole.UserRole) == cause_id):
                header_item = item
            it += 1
        self.assertIsNotNone(header_item)

        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=header_item), \
             unittest.mock.patch('tree_panel.QMenu') as mock_menu_cls:
            self.panel._context_menu(QPoint(0, 0))
        mock_menu = mock_menu_cls.return_value
        labels = _menu_action_labels(mock_menu)
        self.assertTrue(any("Lägg till orsak" in lbl for lbl in labels))
        self.assertTrue(any("Lägg till konsekvens" in lbl for lbl in labels))

    def test_cause_row_shows_real_description_not_redundant_tag(self):
        """'Det räcker om instrumentet E1.M1.QMA127 dyker upp på en rad
        i trädhierarkin' (2026-08-11) — a cause with a REAL, meaningful
        description was showing its tag instead (redundant with the
        equipment header directly above it), because add_causes_to_item's
        label logic always preferred the tag over the description
        whenever a tag existed, regardless of whether the description
        was meaningful. Confirmed on a real project database: a cause
        reading "Flödesgivare felar -> styrventil stänger" displayed as
        just "=E1.M1.QMA127", the same tag its own parent row already
        shows."""
        from hazop import _create_tagged_cause
        eq_id = self.db.add_equipment_item("E1.M1.QMA127", "E1.M1.QMA127", "QMA", 0,
                                           "Instrument / Sensor", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        cause_id, _cons_id = _create_tagged_cause(
            self.db, dev_id, "Instrument / Sensor", "E1.M1.QMA127")
        self.db.update_cause(cause_id, description="Flödesgivare felar -> styrventil stänger")
        self.panel.refresh()

        # A real description means the cause must NOT have merged into
        # the equipment header (that merge only applies to a still-
        # trivial cause) — it stays its own, separate CAUSE_T child.
        cause_rows = [x for x in self._tree_items() if x[0] == CAUSE_T and x[1] == cause_id]
        self.assertEqual(len(cause_rows), 1)

        it = QTreeWidgetItemIterator(self.panel.tree)
        cause_item = None
        while it.value():
            item = it.value()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) == CAUSE_T
                    and item.data(0, Qt.ItemDataRole.UserRole) == cause_id):
                cause_item = item
            it += 1
        self.assertIsNotNone(cause_item)
        self.assertIn("Flödesgivare felar", cause_item.text(0))
        self.assertNotIn("E1.M1.QMA127", cause_item.text(0),
            "must show the real description, not repeat the tag its parent row already shows")

    def test_merged_equipment_deviation_item_offers_add_cause_context_menu(self):
        """Right-clicking the equipment row used to be a dead end (EQUIP_T
        items get no context menu at all) — now that this row IS the
        deviation for the common single-deviation case, it must offer
        '+ Lägg till orsak' just like any other DEV_T item."""
        eq_id = self.db.add_equipment_item("M1.GPA6", "M1.GPA6", "M1", 0, "Pump", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        dev_item = None
        while it.value():
            item = it.value()
            if (item.data(0, Qt.ItemDataRole.UserRole + 1) == DEV_T
                    and item.data(0, Qt.ItemDataRole.UserRole) == dev_id):
                dev_item = item
            it += 1
        self.assertIsNotNone(dev_item)

        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=dev_item), \
             unittest.mock.patch('tree_panel.QMenu') as mock_menu_cls:
            self.panel._context_menu(QPoint(0, 0))
        mock_menu_cls.assert_called_once()
        mock_menu = mock_menu_cls.return_value
        labels = _menu_action_labels(mock_menu)
        self.assertTrue(any("Lägg till orsak" in lbl for lbl in labels))

    def test_lone_generic_deviation_skips_ledord_wrapper(self):
        """Bug report: 'varför är det dubbelt?' — a single, plain
        (no equipment) deviation for a guide word with no other sibling
        used to STILL get wrapped in a LEDORD_T item carrying the exact
        same guide-word text as its own only child, e.g.
        '⬡ Lågt flöde' -> '1. Lågt flöde' — the same words shown twice in
        a row for no structural reason. With nothing to group (no
        equipment, no second deviation sharing the guide word), the
        deviation now attaches directly to the NODE, exactly like before
        this feature existed. The wrapper only reappears once there's a
        second deviation for the same guide word to actually distinguish
        (see test_two_equipment_sharing_same_guide_word_grouped_under_one_ledord
        and test_generic_deviation_stays_visible_once_it_has_a_cause)."""
        node_id = self.db.add_node()
        dev_id = self.db.add_deviation(node_id, "Övrigt-avvikelse")
        self.panel.refresh()

        items = self._tree_items()
        dev_rows = [x for x in items if x[0] == DEV_T and x[1] == dev_id]
        self.assertEqual(len(dev_rows), 1)
        self.assertEqual(dev_rows[0][2], NODE_T,
                          "a lone deviation with nothing to group against must "
                          "attach directly to the node, no redundant Ledord wrapper")
        self.assertEqual(len([x for x in items if x[0] == EQUIP_T]), 0)

    def test_brand_new_node_has_no_ledord_wrappers_at_all(self):
        """The exact real-world screenshot that triggered the fix: a freshly
        created node (all ~16 auto-seeded guide words, no equipment
        touched yet) must show every guide word as a single flat row
        directly under the node — zero LEDORD_T items anywhere, since
        there is nothing anywhere to group."""
        node_id = self.db.add_node()
        self.panel.refresh()

        items = self._tree_items()
        self.assertEqual(len([x for x in items if x[0] == LEDORD_T]), 0)
        dev_rows = [x for x in items if x[0] == DEV_T]
        self.assertTrue(dev_rows)
        self.assertTrue(all(x[2] == NODE_T for x in dev_rows))

    def test_empty_generic_deviation_hidden_when_equipment_scoped_sibling_exists(self):
        """Bug report: 'Lågt flöde dyker upp två gånger i trädet'.
        add_node() auto-seeds an empty, generic (equipment_id=NULL) 'Lågt
        flöde' deviation for every node. Once a piece of equipment ALSO
        gets its own 'Lågt flöde', the still-empty generic one is just
        unused scaffolding sitting right next to it under the same guide
        word — hide it (it is not deleted; see the sibling test below)."""
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        node_id = self.db.add_node()   # already auto-seeds a generic "Lågt flöde"
        generic_dev = next(d for d in self.db.deviations(node_id)
                            if d['description'] == "Lågt flöde")
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        items = self._tree_items()
        # The equipment-scoped deviation (single deviation for this
        # equipment+guide-word combo) now merges directly onto the
        # equipment's own tree item (2026-08-09, see NOTES.md "kaka på
        # kaka") — a legitimate DEV_T item directly under the LEDORD_T.
        # This test only cares whether the separate GENERIC (no-equipment)
        # deviation is hidden, so it must check that specific id, not
        # "any DEV_T at all" under the ledord.
        generic_rows = [x for x in items if x[0] == DEV_T and x[1] == generic_dev['id']]
        self.assertEqual(
            len(generic_rows), 0,
            "the empty auto-seeded generic deviation must be hidden once an "
            "equipment-scoped sibling exists for the same guide word")

    def test_generic_deviation_stays_visible_once_it_has_a_cause(self):
        """The hide-when-empty rule must never hide real user data: a
        generic deviation that already has a cause stays visible even if an
        equipment-scoped sibling for the same guide word also exists.
        2026-08-25 (see NOTES.md 'Slå ihop objekt-rad + avvikelse-rad'):
        it now attaches directly to the node (flat), not to a shared
        LEDORD_T wrapper — the equipment-linked sibling is its own,
        separate flat row and no longer shares a parent with this one."""
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        node_id = self.db.add_node()
        generic_dev = next(d for d in self.db.deviations(node_id)
                            if d['description'] == "Lågt flöde")
        self.db.add_cause(generic_dev['id'])
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        items = self._tree_items()
        direct_dev_rows = [x for x in items if x[0] == DEV_T and x[2] == NODE_T
                            and x[1] == generic_dev['id']]
        self.assertEqual(len(direct_dev_rows), 1,
                          "a generic deviation with an existing cause must remain visible")

    def test_resolve_node_id_for_equip_t(self):
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        self.db.set_equipment_node(eq_id, node_id)
        self.assertEqual(self.panel._resolve_node_id(EQUIP_T, eq_id), node_id)

    def test_resolve_node_id_for_ledord_t(self):
        node_id = self.db.add_node()
        self.db.add_deviation(node_id, "Lågt flöde")
        self.panel.refresh()
        items = self._tree_items()
        ledord_id = next(x[1] for x in items if x[0] == LEDORD_T)
        self.assertEqual(self.panel._resolve_node_id(LEDORD_T, ledord_id), node_id)

    def test_context_menu_is_a_no_op_for_ledord_t(self):
        """LEDORD_T is a pure grouping view (like EQUIP_T) — right-clicking
        it must return before ever building/exec-ing a QMenu (QMenu.exec()
        is modal and would otherwise hang a headless/offscreen test run
        indefinitely if the LEDORD_T check were ever bypassed).

        Patches QTreeWidget.itemAt directly rather than relying on
        visualItemRect()-derived coordinates: self.panel is never shown, so
        the tree has no real layout geometry, and itemAt() on an arbitrary
        point can resolve to the wrong item (or None) — which is exactly
        what silently happened here once already, sending this test
        through the real menu-building path and hanging on menu.exec()."""
        node_id = self.db.add_node()
        self.db.add_deviation(node_id, "Lågt flöde")
        self.panel.refresh()
        it = QTreeWidgetItemIterator(self.panel.tree)
        ledord_item = None
        while it.value():
            if it.value().data(0, Qt.ItemDataRole.UserRole + 1) == LEDORD_T:
                ledord_item = it.value()
                break
            it += 1
        self.assertIsNotNone(ledord_item)

        with unittest.mock.patch.object(self.panel.tree, 'itemAt', return_value=ledord_item), \
             unittest.mock.patch('tree_panel.QMenu') as mock_menu_cls:
            try:
                self.panel._context_menu(QPoint(0, 0))
            except Exception as e:
                self.fail(f"right-clicking a LEDORD_T item must not raise: {e!r}")
            mock_menu_cls.assert_not_called()

    def _equip_item(self, eq_id):
        # 2026-08-25 (see NOTES.md "Slå ihop objekt-rad + avvikelse-rad"):
        # the flat row now carries a "⬡ N. " numbering prefix ahead of the
        # tag, so a plain startswith(tag) check (which worked when the
        # tag was the very first thing in the label) no longer matches —
        # find it via _EQUIP_TAG_ROLE instead, same robust lookup
        # _equip_item_fallback already uses elsewhere in this file.
        it = QTreeWidgetItemIterator(self.panel.tree)
        while it.value():
            item = it.value()
            if item.data(0, self.panel._EQUIP_TAG_ROLE) == eq_id:
                return item
            it += 1
        return None

    def test_undefined_equipment_type_shows_italic_but_no_type_text(self):
        """2026-08-25 (see NOTES.md 'Slå ihop objekt-rad + avvikelse-rad'):
        "I trädet skall enbart Objektag + avikelsetexten stå" — object
        type is no longer spelled out as text at all (a later, separate
        change will show it as a clickable icon instead), so the old
        "TAG-ABC, ej definierad" wording is gone entirely. The italic
        font is kept as a quiet "type not set" signal in the meantime."""
        eq_id = self.db.add_equipment_item("TAG-ABC", "TAG-ABC", "T", 0, "", "", 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        item = self._equip_item(eq_id)
        self.assertIsNotNone(item)
        self.assertIn("TAG-ABC — Lågt flöde", item.text(0))
        self.assertNotIn("ej definierad", item.text(0))
        self.assertTrue(item.font(0).italic())

    def test_defined_equipment_type_not_italic_and_type_not_shown(self):
        """Object type ("Ventil") must NOT appear in the label text at all
        anymore, defined or not — only tag + deviation text."""
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", "", 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        item = self._equip_item(eq_id)
        self.assertIsNotNone(item)
        self.assertIn("V-101 — Lågt flöde", item.text(0))
        self.assertNotIn("Ventil", item.text(0))
        self.assertFalse(item.font(0).italic())

    def test_double_click_undefined_equipment_opens_type_picker_and_persists(self):
        """"Dubbelklick på 'ej definierad'/'ventil' -> välj typ från
        Standardobjekt -> uppdaterar överallt taggen förekommer" (2026-08-17).
        2026-08-18: the QInputDialog type-only picker was replaced by the
        same Tag+Typ CauseTagPopup used for a tag click in the scenario
        table, with no OK button — selecting a type commits immediately.

        2026-08-25 (see NOTES.md 'Slå ihop objekt-rad + avvikelse-rad'):
        this now uses the COMMON single-deviation flat-row scenario — the
        old EQUIP_T-at-rest state this test used to force via two manual
        add_deviation() calls is no longer reachable at all, since every
        object-linked deviation is now always a flat DEV_T/CAUSE_T row
        regardless of count (see
        test_two_equipment_sharing_same_guide_word_render_as_separate_flat_rows).
        _EQUIP_TAG_ROLE-based popup routing is unaffected either way."""
        from hazop import CauseTagPopup
        eq_id = self.db.add_equipment_item("TAG-XYZ", "TAG-XYZ", "T", 0, "", "", 0)
        self.db.add_standard_object("Ventil")
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        item = self._equip_item(eq_id)
        self.assertIsNotNone(item)

        self.panel._on_item_double_click(item, 0)
        popups = self.panel.findChildren(CauseTagPopup)
        self.assertEqual(len(popups), 1)
        popup = popups[0]
        popup._type_cb.setCurrentIndex(popup._type_cb.findText("Ventil"))
        popup._commit()

        self.assertEqual(self.db.get_equipment_by_id(eq_id)['equipment_type'], "Ventil")
        item_after = self._equip_item(eq_id)
        self.assertIn("TAG-XYZ — Lågt flöde", item_after.text(0))

    def test_double_click_equipment_type_picker_emits_item_edited_inline(self):
        """2026-08-25: uses the common single-deviation flat-row scenario
        — see test_double_click_undefined_equipment_opens_type_picker_and_persists's
        own docstring for why the old forced-EQUIP_T setup is gone.
        _apply_equipment_tag_edit always emits (EQUIP_T, eq_id) here
        regardless of the row's own current type (DEV_T at rest, in this
        case) — unchanged by this rewrite, since that method itself
        wasn't touched."""
        from hazop import CauseTagPopup
        eq_id = self.db.add_equipment_item("TAG-XYZ", "TAG-XYZ", "T", 0, "", "", 0)
        # "Pump" is already present in the default seeded standard_objects
        # library (Database() seeds it on construction) — no need to add it.
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()
        item = self._equip_item(eq_id)
        self.assertEqual(item.data(0, Qt.ItemDataRole.UserRole + 1), DEV_T,
            "sanity: a single equipment-linked deviation is a flat DEV_T row now")

        captured = []
        self.panel.item_edited_inline.connect(lambda t, i: captured.append((t, i)))
        self.panel._on_item_double_click(item, 0)
        popup = self.panel.findChildren(CauseTagPopup)[0]
        popup._type_cb.setCurrentIndex(popup._type_cb.findText("Pump"))
        popup._commit()

        self.assertEqual(captured, [(EQUIP_T, eq_id)])


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

    def test_deviations_toggle_keeps_shared_ledord_group_and_all_deviations_visible(self):
        """Deviations merged under a shared guide-word grouping (Ledord)
        must all stay visible regardless of which one is active — the
        toggle only ever collapses the cause level, never hides a row."""
        pump_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        valve_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        pump_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=pump_id)
        valve_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=valve_id)
        self.panel.refresh()
        pump_item = _find_tree_item(self.panel.tree, DEV_T, pump_dev)
        valve_item = _find_tree_item(self.panel.tree, DEV_T, valve_dev)
        self.panel.tree.setCurrentItem(pump_item)

        self.panel._auto_collapse_deviations_chk.setChecked(True)

        self.assertFalse(pump_item.isHidden())
        self.assertFalse(valve_item.isHidden(), "an inactive sibling deviation must stay visible")
        self.assertFalse(pump_item.parent().isHidden(),
            "the shared Ledord group must stay visible")

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
