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

    def test_equipment_scoped_deviation_grouped_under_ledord_then_equip_t(self):
        """A single deviation for this equipment+guide-word combo (the
        overwhelmingly common case — get_or_create_deviation is idempotent
        per node+description+equipment) merges directly onto the
        equipment's own tree item instead of wrapping it in a separate
        DEV_T child (2026-08-09, see NOTES.md 'kaka på kaka' — the
        deviation's description is always identical to the LEDORD_T
        group's own label, so a nested child just repeated the same text
        the user already saw one level up). The merged item carries the
        DEVIATION's identity (not EQUIP_T) so 'add cause' and
        equipment-dropped-on-deviation both work directly on it."""
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        dev_id = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        items = self._tree_items()
        # add_node() auto-seeds ~16 default guide words, so filter down to
        # just the one this test cares about, not "any LEDORD_T at all".
        ledord_rows = [x for x in items if x[0] == LEDORD_T and x[2] == NODE_T
                       and str(x[1]).endswith("Lågt flöde")]
        self.assertEqual(len(ledord_rows), 1, "the guide word must appear as its own tree item under the node")
        self.assertEqual(len([x for x in items if x[0] == EQUIP_T and x[1] == eq_id]), 0,
            "a single deviation must not get a separate EQUIP_T wrapper anymore")
        dev_rows = [x for x in items if x[0] == DEV_T and x[1] == dev_id]
        self.assertEqual(len(dev_rows), 1)
        self.assertEqual(dev_rows[0][2], LEDORD_T,
            "the merged equipment+deviation item must sit directly under the LEDORD_T (guide word) item")
        self.assertEqual(self.panel._resolve_equipment_id(DEV_T, dev_id), eq_id,
            "the merged item's underlying deviation must still resolve back to its equipment")

    def test_two_equipment_sharing_same_guide_word_grouped_under_one_ledord(self):
        """The core reason for this hierarchy: 'Lågt flöde' for a pump AND
        a valve under the same node must appear under ONE shared guide-word
        item, each with its own equipment sub-item — not two separate
        top-level groups. Each equipment has only one deviation here, so
        each merges directly onto its own item (2026-08-09, see NOTES.md
        'kaka på kaka') rather than wrapping in a separate EQUIP_T+DEV_T pair."""
        pump_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        valve_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        pump_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=pump_id)
        valve_dev = self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=valve_id)
        self.panel.refresh()

        items = self._tree_items()
        ledord_rows = [x for x in items if x[0] == LEDORD_T]
        matching = [x for x in ledord_rows if str(x[1]).endswith("Lågt flöde")]
        self.assertEqual(len(matching), 1, "both equipment must share ONE 'Lågt flöde' guide-word item")
        dev_rows = [x for x in items if x[0] == DEV_T and x[2] == LEDORD_T
                    and x[1] in (pump_dev, valve_dev)]
        self.assertEqual({x[1] for x in dev_rows}, {pump_dev, valve_dev})
        self.assertEqual(self.panel._resolve_equipment_id(DEV_T, pump_dev), pump_id)
        self.assertEqual(self.panel._resolve_equipment_id(DEV_T, valve_dev), valve_id)

    def test_numbering_stays_sequential_and_visible_after_linking_equipment(self):
        """Bug report (2026-08-13): 'Nummereringen av lågt flöde, högt
        flöde osv blir konstig när man lägger till objekt i trädet.'
        First fix attempt made the merged equipment row simply consume
        no number at all — which then made a SECOND report surface:
        'jag vill att den ska kvarstå så att det alltid syns att det är
        exempelvis 16 avikelser' (the guide word's own number must stay
        visible even once it's wrapped/merged, not disappear). The
        Ledord wrapper itself now carries the guide word's number, and
        equipment/deviation-instance items inside it use their own
        separate local counter that can never steal from this top-level,
        one-per-guide-word sequence."""
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        it = QTreeWidgetItemIterator(self.panel.tree)
        lagt_wrapper = hogt_item = None
        while it.value():
            item = it.value()
            t = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if t == LEDORD_T and "Lågt flöde" in item.text(0):
                lagt_wrapper = item
            if (t == DEV_T and item.parent() is not None
                    and item.parent().data(0, Qt.ItemDataRole.UserRole + 1) == NODE_T
                    and "Högt flöde" in item.text(0)):
                hogt_item = item
            it += 1
        self.assertIsNotNone(lagt_wrapper, "'Lågt flöde' must be wrapped once equipment is linked")
        self.assertIn("1. Lågt flöde", lagt_wrapper.text(0),
            f"the guide word's own number must stay visible after wrapping, got: {lagt_wrapper.text(0)!r}")
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

    def test_merged_equipment_deviation_item_does_not_repeat_guide_word_text(self):
        """Exact reported bug: '=M1.GPA6 — Pump' nested under 'Lågt flöde'
        used to show ANOTHER child item labelled '1. Lågt flöde' — the
        same guide-word text shown twice in a row for no reason, with the
        real cause nested one level deeper still (2026-08-09, screenshot
        in conversation). The merged item's own label must be the
        equipment tag/type, never a repeat of the guide-word text."""
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
        self.assertIn("M1.GPA6", dev_item.text(0))
        self.assertNotIn("Lågt flöde", dev_item.text(0),
            "the merged item must not repeat the guide-word text its LEDORD_T parent already shows")
        # The cause must be one level directly below the merged item, not two.
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
        equipment-scoped sibling for the same guide word also exists."""
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        node_id = self.db.add_node()
        generic_dev = next(d for d in self.db.deviations(node_id)
                            if d['description'] == "Lågt flöde")
        self.db.add_cause(generic_dev['id'])
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        items = self._tree_items()
        direct_dev_rows = [x for x in items if x[0] == DEV_T and x[2] == LEDORD_T
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
        it = QTreeWidgetItemIterator(self.panel.tree)
        while it.value():
            item = it.value()
            if item.text(0).strip().startswith(self.db.get_equipment_by_id(eq_id)['tag']):
                return item
            it += 1
        return None

    def test_undefined_equipment_shows_ej_definierad_italic(self):
        """"Idag: 'TAG-ABC —' ... Ska bli 'TAG-ABC, ej definierad' ...
        (kursivt)" (2026-08-17) — an equipment_type of '' used to leave a
        bare trailing dash with nothing after it."""
        eq_id = self.db.add_equipment_item("TAG-ABC", "TAG-ABC", "T", 0, "", "", 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        item = self._equip_item(eq_id)
        self.assertIsNotNone(item)
        self.assertIn("TAG-ABC, ej definierad", item.text(0))
        self.assertNotIn("—", item.text(0))
        self.assertTrue(item.font(0).italic())

    def test_defined_equipment_shows_type_not_italic(self):
        eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", "", 0)
        node_id = self.db.add_node()
        self.db.get_or_create_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        item = self._equip_item(eq_id)
        self.assertIsNotNone(item)
        self.assertIn("V-101, Ventil", item.text(0))
        self.assertFalse(item.font(0).italic())

    def test_double_click_undefined_equipment_opens_type_picker_and_persists(self):
        """"Dubbelklick på 'ej definierad'/'ventil' -> välj typ från
        Standardobjekt -> uppdaterar överallt taggen förekommer" (2026-08-17).
        2026-08-18: the QInputDialog type-only picker was replaced by the
        same Tag+Typ CauseTagPopup used for a tag click in the scenario
        table, with no OK button — selecting a type commits immediately.
        Forces the genuinely-EQUIP_T (un-merged) code path via two manual
        add_deviation() calls sharing one equipment_id + guide word — the
        idempotent get_or_create_deviation used everywhere else in the app
        never produces this combination "in practice" (see this class's
        own docstring), but the code path exists and must work."""
        from hazop import CauseTagPopup
        eq_id = self.db.add_equipment_item("TAG-XYZ", "TAG-XYZ", "T", 0, "", "", 0)
        self.db.add_standard_object("Ventil")
        node_id = self.db.add_node()
        self.db.add_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.db.add_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()

        item = self._equip_item(eq_id)
        self.assertIsNotNone(item)
        self.assertEqual(item.data(0, Qt.ItemDataRole.UserRole + 1), EQUIP_T,
                          "sanity: two deviations sharing one equipment+guide-word "
                          "must produce a real (un-merged) EQUIP_T row")

        self.panel._on_item_double_click(item, 0)
        popups = self.panel.findChildren(CauseTagPopup)
        self.assertEqual(len(popups), 1)
        popup = popups[0]
        popup._type_cb.setCurrentIndex(popup._type_cb.findText("Ventil"))
        popup._commit()

        self.assertEqual(self.db.get_equipment_by_id(eq_id)['equipment_type'], "Ventil")
        item_after = self._equip_item(eq_id)
        self.assertIn("TAG-XYZ, Ventil", item_after.text(0))

    def test_double_click_equipment_type_picker_emits_item_edited_inline(self):
        from hazop import CauseTagPopup
        eq_id = self.db.add_equipment_item("TAG-XYZ", "TAG-XYZ", "T", 0, "", "", 0)
        # "Pump" is already present in the default seeded standard_objects
        # library (Database() seeds it on construction) — no need to add it.
        node_id = self.db.add_node()
        self.db.add_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.db.add_deviation(node_id, "Lågt flöde", equipment_id=eq_id)
        self.panel.refresh()
        item = self._equip_item(eq_id)

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
    """"Auto-collapse" toggle below the tree (2026-08-24, see NOTES.md
    "Åtta UX/logik-förbättringar") — when on, folds away every node/
    avvikelse other than the one currently active, cutting visual noise in
    large studies. The active node and its active deviation always stay
    expanded/visible; off (the default) leaves the existing expand/
    collapse behavior completely untouched."""

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

    def test_off_by_default_and_persisted_via_app_config(self):
        self.assertFalse(self.panel._auto_collapse_chk.isChecked())
        self.assertEqual(self.db.get_config('tree_auto_collapse', '0'), '0')

    def test_enabling_persists_to_app_config(self):
        self.panel._auto_collapse_chk.setChecked(True)
        self.assertEqual(self.db.get_config('tree_auto_collapse', '0'), '1')

    def test_enabling_collapses_all_nodes_except_the_selected_one(self):
        node_a = self.db.add_node()
        node_b = self.db.add_node()
        self.panel.refresh()
        item_a = _find_tree_item(self.panel.tree, NODE_T, node_a)
        item_b = _find_tree_item(self.panel.tree, NODE_T, node_b)
        self.panel.tree.setCurrentItem(item_a)

        self.panel._auto_collapse_chk.setChecked(True)

        self.assertTrue(item_a.isExpanded(), "the active node must stay expanded")
        self.assertFalse(item_b.isExpanded(), "an inactive node must fold away")

    def test_only_active_deviation_stays_expanded_within_active_node(self):
        node_id = self.db.add_node()
        devs = self.db.deviations(node_id)
        self.panel.refresh()
        dev_item_0 = _find_tree_item(self.panel.tree, DEV_T, devs[0]['id'])
        dev_item_1 = _find_tree_item(self.panel.tree, DEV_T, devs[1]['id'])
        self.panel.tree.setCurrentItem(dev_item_0)

        self.panel._auto_collapse_chk.setChecked(True)

        node_item = _find_tree_item(self.panel.tree, NODE_T, node_id)
        self.assertTrue(node_item.isExpanded(), "the active deviation's own node must stay open")
        self.assertTrue(dev_item_0.isExpanded())
        self.assertFalse(dev_item_1.isExpanded())

    def test_switching_selection_live_recollapses_previous_node(self):
        """Re-applied from _on_select too, not just refresh() — clicking a
        different node must fold the previous one away immediately,
        without waiting for unrelated data to change."""
        node_a = self.db.add_node()
        node_b = self.db.add_node()
        self.panel.refresh()
        item_a = _find_tree_item(self.panel.tree, NODE_T, node_a)
        item_b = _find_tree_item(self.panel.tree, NODE_T, node_b)
        self.panel.tree.setCurrentItem(item_a)
        self.panel._auto_collapse_chk.setChecked(True)
        self.assertTrue(item_a.isExpanded())

        self.panel.tree.setCurrentItem(item_b)

        self.assertFalse(item_a.isExpanded(),
            "switching the active node must fold the previous one away immediately")
        self.assertTrue(item_b.isExpanded())

    def test_disabled_auto_collapse_leaves_expand_all_alone(self):
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
        from PyQt6.QtWidgets import QDialog
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        self.panel.refresh()
        dev_item = _find_tree_item(self.panel.tree, DEV_T, dev_id)
        self.panel.tree.setCurrentItem(dev_item)
        before = len(self.db.causes_for_deviation(dev_id))

        # add_cause() -> _open_cause_picker_for_deviation() opens a real
        # StandardCausesPickerPopup — same mock pattern as
        # test_integration.py's test_tree_add_cause_via_picker_also_creates_empty_consequence.
        def _fake_exec(self):
            self.cause_picked.emit("Ny orsak (test)", None)
            return QDialog.DialogCode.Accepted

        with unittest.mock.patch.object(hazop.StandardCausesPickerPopup, 'exec', new=_fake_exec):
            self._button("+ Orsak").click()

        self.assertEqual(len(self.db.causes_for_deviation(dev_id)), before + 1)

    def test_clicking_button_with_no_deviation_selected_shows_a_hint_not_a_crash(self):
        with unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
            self._button("+ Orsak").click()
        mock_info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
