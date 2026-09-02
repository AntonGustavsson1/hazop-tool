#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering pid_graphics_view.py, plus any cross-module glue they
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

class EquipmentMultiSelectTests(unittest.TestCase):
    """Multi-select of equipment markers on the P&ID (2026-08-08, see
    NOTES.md): Ctrl+click toggles, Ctrl+drag rubber-bands several at once,
    and a Shift-drag of a >=2-member selection drags the whole group."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_view_with_markers(self, marker_positions):
        """marker_positions: dict marker_id -> QPointF scene position."""
        from pid_viewer import PIDGraphicsView, MODE_NAV
        view = PIDGraphicsView()
        view.mode = MODE_NAV
        items = {}
        for marker_id, pos in marker_positions.items():
            item = view._scene.addEllipse(pos.x() - 5, pos.y() - 5, 10, 10)
            item.setData(view._DATA_TYPE, 'equipment')
            item.setData(view._DATA_ID, marker_id)
            view._type_items.setdefault('equipment', []).append(item)
            items[marker_id] = item
        return view, items

    def _press(self, view, event):
        from PyQt6.QtWidgets import QGraphicsView
        with unittest.mock.patch.object(QGraphicsView, 'mousePressEvent'):
            view.mousePressEvent(event)

    def _move(self, view, event):
        from PyQt6.QtWidgets import QGraphicsView
        with unittest.mock.patch.object(QGraphicsView, 'mouseMoveEvent'):
            view.mouseMoveEvent(event)

    def _release(self, view, event):
        from PyQt6.QtWidgets import QGraphicsView
        with unittest.mock.patch.object(QGraphicsView, 'mouseReleaseEvent'):
            view.mouseReleaseEvent(event)

    def test_ctrl_click_toggles_marker_into_selection(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({7: QPointF(50, 50)})
        view.mapToScene = lambda pt: QPointF(50, 50)
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        event.position.return_value = QPointF(50, 50)

        self._press(view, event)

        self.assertIn(7, view._selected_equipment_markers)
        self.assertIn(7, view._equip_selection_overlays)

    def test_ctrl_click_again_deselects_marker(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({7: QPointF(50, 50)})
        view.mapToScene = lambda pt: QPointF(50, 50)
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        event.position.return_value = QPointF(50, 50)

        self._press(view, event)
        self._press(view, event)

        self.assertNotIn(7, view._selected_equipment_markers)
        self.assertNotIn(7, view._equip_selection_overlays)

    def test_plain_click_clears_existing_selection(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({7: QPointF(50, 50)})
        view._select_equipment_marker(7)
        self.assertIn(7, view._selected_equipment_markers)

        view.mapToScene = lambda pt: QPointF(200, 200)   # empty area, no marker there
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.NoModifier
        event.position.return_value = QPointF(200, 200)

        self._press(view, event)

        self.assertEqual(view._selected_equipment_markers, set())

    def test_ctrl_drag_rubber_band_selects_markers_in_rect(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({
            1: QPointF(10, 10), 2: QPointF(20, 20), 3: QPointF(500, 500),
        })
        # mapToScene: identity-ish, viewport coords == scene coords for this test
        view.mapToScene = lambda pt: QPointF(pt)

        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        press_event.position.return_value = QPointF(0, 0)
        self._press(view, press_event)
        self.assertIsNotNone(view._ctrl_rband_start_scene)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        move_event.position.return_value = QPointF(30, 30)
        self._move(view, move_event)
        self.assertTrue(view._ctrl_rband_dragging)

        release_event = unittest.mock.MagicMock()
        release_event.button.return_value = Qt.MouseButton.LeftButton
        release_event.position.return_value = QPointF(30, 30)
        self._release(view, release_event)

        self.assertEqual(view._selected_equipment_markers, {1, 2},
            "only markers inside the 0,0-30,30 band should be selected")
        self.assertNotIn(3, view._selected_equipment_markers)

    def test_shift_drag_of_multi_selection_builds_equipment_multi_mime(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({
            5: QPointF(50, 50), 6: QPointF(60, 60),
        })
        view._select_equipment_marker(5)
        view._select_equipment_marker(6)
        view.mapToScene = lambda pt: QPointF(50, 50)

        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        press_event.position.return_value = QPointF(50, 50)
        self._press(view, press_event)
        self.assertEqual(view._equip_drag_candidate[0], 5)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        move_event.position.return_value = QPointF(90, 50)

        with unittest.mock.patch('pid_graphics_view.QDrag') as MockDrag:
            mock_drag = MockDrag.return_value
            view.mouseMoveEvent(move_event)

        mime_arg = mock_drag.setMimeData.call_args[0][0]
        self.assertEqual(mime_arg.text(), 'hzp:equipment-multi:5,6:-1:-1')
        # Selection is cleared once the group drag has actually started.
        self.assertEqual(view._selected_equipment_markers, set())

    def test_shift_drag_of_single_unselected_marker_still_uses_plain_kind(self):
        """A Shift-drag from a marker NOT part of any multi-selection must
        keep using the original single-marker mime — no regression for the
        already-shipped, already-tested single-drag feature."""
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({9: QPointF(50, 50)})
        view.mapToScene = lambda pt: QPointF(50, 50)

        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        press_event.position.return_value = QPointF(50, 50)
        self._press(view, press_event)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
        move_event.position.return_value = QPointF(90, 50)

        with unittest.mock.patch('pid_graphics_view.QDrag') as MockDrag:
            mock_drag = MockDrag.return_value
            view.mouseMoveEvent(move_event)

        mime_arg = mock_drag.setMimeData.call_args[0][0]
        self.assertEqual(mime_arg.text(), 'hzp:equipment:9:-1:-1')

    def test_ctrl_drag_shows_live_count_label(self):
        """(2026-08-10, see NOTES.md) — the count label updates DURING the
        drag, not just after release."""
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({1: QPointF(10, 10), 2: QPointF(20, 20)})
        view.mapToScene = lambda pt: QPointF(pt)

        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        press_event.position.return_value = QPointF(0, 0)
        self._press(view, press_event)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        move_event.position.return_value = QPointF(30, 30)
        self._move(view, move_event)

        self.assertIsNotNone(view._ctrl_rband_count_label)
        self.assertEqual(view._ctrl_rband_count_label.text(), "2 objekt")

    def test_ctrl_drag_count_label_removed_when_band_empty(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({1: QPointF(500, 500)})
        view.mapToScene = lambda pt: QPointF(pt)

        press_event = unittest.mock.MagicMock()
        press_event.button.return_value = Qt.MouseButton.LeftButton
        press_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        press_event.position.return_value = QPointF(0, 0)
        self._press(view, press_event)

        move_event = unittest.mock.MagicMock()
        move_event.buttons.return_value = Qt.MouseButton.LeftButton
        move_event.modifiers.return_value = Qt.KeyboardModifier.ControlModifier
        move_event.position.return_value = QPointF(30, 30)   # marker at (500,500) is outside
        self._move(view, move_event)

        self.assertIsNone(view._ctrl_rband_count_label)

    def test_escape_clears_multi_selection(self):
        """(2026-08-10, see NOTES.md) — previously Escape only cancelled
        placement/drawing modes, never an equipment multi-selection."""
        from PyQt6.QtCore import QPointF, QEvent
        from PyQt6.QtGui import QKeyEvent
        view, _items = self._make_view_with_markers({1: QPointF(10, 10)})
        view._select_equipment_marker(1)
        self.assertEqual(view._selected_equipment_markers, {1})

        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        view.keyPressEvent(ev)

        self.assertEqual(view._selected_equipment_markers, set())

    def test_context_menu_offers_clear_selection_when_markers_selected(self):
        from PyQt6.QtCore import QPointF, QPoint
        from PyQt6.QtWidgets import QMenu
        view, _items = self._make_view_with_markers({1: QPointF(10, 10)})
        view._select_equipment_marker(1)
        texts = []

        def _fake_exec(menu_self, _pos=None):
            texts.extend(a.text() for a in menu_self.actions())
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(QPointF(500, 500), QPoint(0, 0))

        self.assertTrue(any("Rensa markering" in t for t in texts), texts)

    def test_context_menu_does_not_offer_manual_equipment_placement(self):
        from PyQt6.QtCore import QPointF, QPoint
        from PyQt6.QtWidgets import QMenu
        view, _items = self._make_view_with_markers({1: QPointF(10, 10)})
        texts = []

        def _fake_exec(menu_self, _pos=None):
            texts.extend(a.text() for a in menu_self.actions())
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(QPointF(500, 500), QPoint(0, 0))

        self.assertFalse(any("Objekt" in t for t in texts), texts)

    def test_clearing_via_context_menu_action_clears_selection(self):
        from PyQt6.QtCore import QPointF, QPoint
        from PyQt6.QtWidgets import QMenu
        view, _items = self._make_view_with_markers({1: QPointF(10, 10)})
        view._select_equipment_marker(1)

        def _fake_exec(menu_self, _pos=None):
            for a in menu_self.actions():
                if "Rensa markering" in a.text():
                    a.trigger()
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(QPointF(500, 500), QPoint(0, 0))

        self.assertEqual(view._selected_equipment_markers, set())

    def test_equipment_marker_tooltip_mentions_gestures(self):
        """(2026-08-10, see NOTES.md) — Ctrl/Shift modifiers had no other
        visible affordance anywhere in the UI."""
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 10.0, 10.0, "Ventil", tag="V-1")
        item = view._type_items['equipment'][0]
        self.assertIn("Ctrl", item.toolTip())
        self.assertIn("Shift", item.toolTip())


class EquipmentPlainClickHighlightTests(unittest.TestCase):
    """Reported feedback (2026-08-12, see NOTES.md): 'När jag klickar på ett
    definerat objekt (rött eller grönt) vill jag att det blir blått så man
    ser att det är markerat.' A plain (no-modifier) click on an existing
    equipment marker now single-selects it — reusing the exact same
    dashed-blue overlay mechanism Ctrl-click multi-select already draws
    (EquipmentMultiSelectTests), just triggered from the ordinary NAV-mode
    click-dispatch in mouseReleaseEvent instead of only from Ctrl+click."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_view_with_markers(self, marker_positions):
        from pid_viewer import PIDGraphicsView, MODE_NAV
        view = PIDGraphicsView()
        view.mode = MODE_NAV
        items = {}
        for marker_id, pos in marker_positions.items():
            item = view._scene.addEllipse(pos.x() - 5, pos.y() - 5, 10, 10)
            item.setData(view._DATA_TYPE, 'equipment')
            item.setData(view._DATA_ID, marker_id)
            view._type_items.setdefault('equipment', []).append(item)
            items[marker_id] = item
        return view, items

    def _click(self, view, pos):
        """Simulate a full plain-click (press + release, no movement) at
        scene position `pos`, bypassing the real QGraphicsView base
        implementation the same way EquipmentMultiSelectTests does."""
        from PyQt6.QtWidgets import QGraphicsView
        view.mapToScene = lambda pt: pos
        event = unittest.mock.MagicMock()
        event.button.return_value = Qt.MouseButton.LeftButton
        event.modifiers.return_value = Qt.KeyboardModifier.NoModifier
        event.position.return_value = pos
        with unittest.mock.patch.object(QGraphicsView, 'mousePressEvent'):
            view.mousePressEvent(event)
        with unittest.mock.patch.object(QGraphicsView, 'mouseReleaseEvent'):
            view.mouseReleaseEvent(event)

    def test_plain_click_on_equipment_marker_selects_it(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({7: QPointF(50, 50)})
        self._click(view, QPointF(50, 50))
        self.assertEqual(view._selected_equipment_markers, {7})
        self.assertIn(7, view._equip_selection_overlays)

    def test_plain_click_on_equipment_marker_emits_marker_clicked(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({7: QPointF(50, 50)})
        captured = []
        view.marker_clicked.connect(lambda t, i: captured.append((t, i)))
        self._click(view, QPointF(50, 50))
        self.assertEqual(captured, [('equipment', 7)])

    def test_clicking_a_second_marker_replaces_the_first_selection(self):
        from PyQt6.QtCore import QPointF
        view, _items = self._make_view_with_markers({
            1: QPointF(10, 10), 2: QPointF(90, 90),
        })
        self._click(view, QPointF(10, 10))
        self.assertEqual(view._selected_equipment_markers, {1})
        self._click(view, QPointF(90, 90))
        self.assertEqual(view._selected_equipment_markers, {2},
            "clicking a new marker must replace the previous highlight, not add to it")

    def test_reapply_equipment_selection_overlays_survives_marker_rebuild(self):
        """_load_overlays() (triggered e.g. right after checking a
        deviation, to recolor red->green) removes and recreates every
        equipment marker item and calls clear_overlays(), which also wipes
        the independent selection-overlay rects. Without reapplication the
        blue highlight would vanish the instant the user interacts further
        with the very marker they just selected."""
        from PyQt6.QtCore import QPointF
        view, items = self._make_view_with_markers({7: QPointF(50, 50)})
        view._select_equipment_marker(7)
        self.assertIn(7, view._equip_selection_overlays)

        # Simulate clear_overlays() + marker rebuild: old overlay + old
        # marker item both removed from the scene, a fresh marker item
        # (same id) added in their place.
        view._scene.removeItem(view._equip_selection_overlays[7])
        view._scene.removeItem(items[7])
        view._type_items['equipment'] = []
        new_item = view._scene.addEllipse(45, 45, 10, 10)
        new_item.setData(view._DATA_TYPE, 'equipment')
        new_item.setData(view._DATA_ID, 7)
        view._type_items['equipment'].append(new_item)

        view._reapply_equipment_selection_overlays()

        self.assertEqual(view._selected_equipment_markers, {7})
        self.assertIn(7, view._equip_selection_overlays)

    def test_reapply_equipment_selection_overlays_drops_deleted_markers(self):
        from PyQt6.QtCore import QPointF
        view, items = self._make_view_with_markers({7: QPointF(50, 50)})
        view._select_equipment_marker(7)

        view._scene.removeItem(view._equip_selection_overlays[7])
        view._scene.removeItem(items[7])
        view._type_items['equipment'] = []   # marker 7 genuinely gone now

        view._reapply_equipment_selection_overlays()

        self.assertEqual(view._selected_equipment_markers, set())
        self.assertEqual(view._equip_selection_overlays, {})


class TreeContextHighlightTests(unittest.TestCase):
    """set_tree_context_highlights (2026-08-27, see NOTES.md "Dynamisk
    färgmarkering av objekt på P&ID") — the rendering half of the
    tree-context P&ID highlight feature. It colors the existing equipment
    polygon; no separate circular halo is drawn. The dashed-blue multi-select
    rectangle remains separate and can coexist with that color."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_view_with_markers(self, marker_positions):
        from pid_viewer import PIDGraphicsView, MODE_NAV
        view = PIDGraphicsView()
        view.mode = MODE_NAV
        items = {}
        for marker_id, pos in marker_positions.items():
            item = view._scene.addEllipse(pos.x() - 5, pos.y() - 5, 10, 10)
            item.setData(view._DATA_TYPE, 'equipment')
            item.setData(view._DATA_ID, marker_id)
            view._type_items.setdefault('equipment', []).append(item)
            items[marker_id] = item
        return view, items

    def test_set_tree_context_highlights_keeps_existing_marker_neutral(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QColor
        view, items = self._make_view_with_markers({7: QPointF(50, 50)})
        color = QColor(0, 200, 0)

        view.set_tree_context_highlights({7: color})

        self.assertEqual(view._tree_context_highlights, {7: color})
        self.assertEqual(items[7].pen().color(), QColor(120, 120, 120))
        self.assertFalse(hasattr(view, '_tree_context_highlight_overlays'))

    def test_set_tree_context_highlights_replaces_previous_set(self):
        """"objekt som inte längre tillhör aktuell kontext ska återgå
        till sin normala färg" — a fresh call must drop anything not in
        the new map, not accumulate."""
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QColor
        view, items = self._make_view_with_markers({
            1: QPointF(10, 10), 2: QPointF(90, 90),
        })
        color = QColor(0, 200, 0)

        view.set_tree_context_highlights({1: color})
        self.assertEqual(items[1].pen().color(), QColor(120, 120, 120))

        view.set_tree_context_highlights({2: color})
        self.assertEqual(items[1].pen().color(), QColor(120, 120, 120),
            "marker 1 must return to neutral grey once it's out of scope")
        self.assertEqual(items[2].pen().color(), QColor(120, 120, 120))

    def test_tree_context_highlight_coexists_with_multiselect_visually_distinct(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QColor
        view, items = self._make_view_with_markers({7: QPointF(50, 50)})

        view._select_equipment_marker(7)
        view.set_tree_context_highlights({7: QColor(0, 200, 0)})

        self.assertIn(7, view._equip_selection_overlays)
        self.assertEqual(items[7].pen().color(), QColor(120, 120, 120))
        self.assertGreater(view._equip_selection_overlays[7].zValue(),
                           items[7].zValue())

    def test_reapply_tree_context_highlights_survives_marker_rebuild(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QColor
        view, items = self._make_view_with_markers({7: QPointF(50, 50)})
        color = QColor(0, 200, 0)
        view.set_tree_context_highlights({7: color})

        # Simulate clear_overlays() + marker rebuild: the old marker item is
        # removed and a fresh marker item with the same id is added.
        view._scene.removeItem(items[7])
        view._type_items['equipment'] = []
        new_item = view._scene.addEllipse(45, 45, 10, 10)
        new_item.setData(view._DATA_TYPE, 'equipment')
        new_item.setData(view._DATA_ID, 7)
        view._type_items['equipment'].append(new_item)

        view._reapply_tree_context_highlights()

        self.assertEqual(view._tree_context_highlights, {7: color})
        self.assertEqual(new_item.pen().color(), QColor(120, 120, 120))

    def test_reapply_tree_context_highlights_drops_deleted_markers(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QColor
        view, items = self._make_view_with_markers({7: QPointF(50, 50)})
        view.set_tree_context_highlights({7: QColor(0, 200, 0)})

        view._scene.removeItem(items[7])
        view._type_items['equipment'] = []   # marker 7 genuinely gone now

        view._reapply_tree_context_highlights()

        self.assertEqual(view._tree_context_highlights, {})

    def test_clear_tree_context_highlights_removes_everything(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QColor
        view, items = self._make_view_with_markers({1: QPointF(10, 10)})
        view.set_tree_context_highlights({1: QColor(0, 200, 0)})

        view.clear_tree_context_highlights()

        self.assertEqual(view._tree_context_highlights, {})
        self.assertEqual(items[1].pen().color(), QColor(120, 120, 120))


class EquipmentMarkerFourBadgesTests(unittest.TestCase):
    """PIDGraphicsView.add_equipment_marker draws one stable numbered badge
    per visible HAZOP layer. Zero-count badges remain visible in neutral grey
    so changing counts or tree selection does not move the other badges."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _badge_count(self, view):
        from PyQt6.QtWidgets import QGraphicsEllipseItem
        return sum(1 for item in view._type_items.get('equipment', [])
                   if isinstance(item, QGraphicsEllipseItem))

    def test_all_visible_layers_keep_a_grey_zero_badge(self):
        from pid_viewer import PIDGraphicsView
        from PyQt6.QtGui import QColor
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 0, 0, "Ventil", tag="V-101")
        badges = [item for item in view._type_items['equipment']
                  if item.__class__.__name__ == 'QGraphicsEllipseItem']
        self.assertEqual(self._badge_count(view), 4)
        self.assertEqual({badge.brush().color().name() for badge in badges},
                         {QColor(180, 180, 180).name()})
        self.assertEqual(
            {item.text() for item in view._type_items['equipment']
             if item.__class__.__name__ == 'QGraphicsSimpleTextItem'
             and item.data(view._DATA_BADGE_ROLE)},
            {'0'})

    def test_deviation_count_no_longer_changes_marker_from_grey_to_green(self):
        from pid_viewer import PIDGraphicsView
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QGraphicsPolygonItem
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 0, 0, "Ventil", tag="V-101",
                                  deviation_count=3)
        poly = next(item for item in view._type_items['equipment']
                    if isinstance(item, QGraphicsPolygonItem))
        self.assertEqual(poly.pen().color(), QColor(120, 120, 120))
        self.assertEqual(poly.brush().color(), QColor(150, 150, 150, 90))

    def test_all_visible_layers_keep_their_positions_when_counts_differ(self):
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 0, 0, "Ventil", tag="V-101",
                                  deviation_count=2, consequence_count=1, safeguard_count=3)
        self.assertEqual(self._badge_count(view), 4)

    def test_zero_count_badges_are_grey_even_inside_selected_scope(self):
        from pid_viewer import PIDGraphicsView
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QGraphicsEllipseItem
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 0, 0, "Ventil", tag="V-101",
                                  deviation_count=0, consequence_count=5, safeguard_count=0)
        view.set_tree_context_highlights(
            {1: QColor(0, 200, 0)},
            {1: {'cause', 'consequence', 'safeguard', 'recommendation'}})
        self.assertEqual(self._badge_count(view), 4)
        by_role = {
            item.data(view._DATA_BADGE_ROLE): item
            for item in view._type_items['equipment']
            if isinstance(item, QGraphicsEllipseItem)
        }
        self.assertEqual(by_role['cause'].brush().color(), QColor(180, 180, 180))
        self.assertNotEqual(by_role['consequence'].brush().color(),
                            QColor(180, 180, 180))
        self.assertEqual(by_role['safeguard'].brush().color(), QColor(180, 180, 180))
        self.assertEqual(by_role['recommendation'].brush().color(), QColor(180, 180, 180))

    def test_tree_context_lights_only_roles_in_the_selected_tree_scope(self):
        from pid_viewer import (PIDGraphicsView, TREE_CONTEXT_LINK_COLORS,
                                set_tree_context_link_color)
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QGraphicsEllipseItem

        original = {key: QColor(value)
                    for key, value in TREE_CONTEXT_LINK_COLORS.items()}
        try:
            for key, value in {
                    'cause': '#2457a6', 'consequence': '#d97706',
                    'safeguard': '#16a34a', 'recommendation': '#7c3aed'}.items():
                set_tree_context_link_color(key, value)
            view = PIDGraphicsView()
            view.add_equipment_marker(
                1, 0, 0, "Ventil", tag="V-101", deviation_count=2,
                consequence_count=1, safeguard_count=3, recommendation_count=4)
            view.set_tree_context_highlights(
                {1: QColor(0, 200, 0)}, {1: {'cause', 'recommendation'}})

            badges = [item for item in view._type_items['equipment']
                      if isinstance(item, QGraphicsEllipseItem)]
            by_role = {item.data(view._DATA_BADGE_ROLE): item for item in badges}
            self.assertEqual(by_role['cause'].brush().color().name(), '#2457a6')
            self.assertEqual(by_role['recommendation'].brush().color().name(), '#7c3aed')
            self.assertEqual(by_role['consequence'].brush().color(), QColor(180, 180, 180))
            self.assertEqual(by_role['safeguard'].brush().color(), QColor(180, 180, 180))
        finally:
            for key, value in original.items():
                set_tree_context_link_color(key, value)

    def test_badges_use_the_tree_visibility_button_colours(self):
        from pid_viewer import (PIDGraphicsView, TREE_CONTEXT_LINK_COLORS,
                                set_tree_context_link_color)
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QGraphicsSimpleTextItem

        original = {key: QColor(value)
                    for key, value in TREE_CONTEXT_LINK_COLORS.items()}
        chosen = {
            'cause': '#2457a6',
            'consequence': '#d97706',
            'safeguard': '#16a34a',
        }
        try:
            for key, value in chosen.items():
                set_tree_context_link_color(key, value)
            view = PIDGraphicsView()
            view.add_equipment_marker(
                1, 0, 0, "Ventil", tag="V-101", deviation_count=2,
                consequence_count=1, safeguard_count=3,
                show_recommendation=False)
            view.set_tree_context_highlights(
                {1: QColor(0, 200, 0)},
                {1: {'cause', 'consequence', 'safeguard'}})
            badges = [item for item in view._type_items['equipment']
                      if item.__class__.__name__ == 'QGraphicsEllipseItem']
            self.assertEqual({badge.brush().color().name() for badge in badges},
                             set(chosen.values()))
            count_texts = [item for item in view._type_items['equipment']
                           if isinstance(item, QGraphicsSimpleTextItem)]
            self.assertTrue(count_texts)
            self.assertTrue(all(item.brush().color().isValid()
                                for item in count_texts))
        finally:
            for key, value in original.items():
                set_tree_context_link_color(key, value)

    def test_tooltip_mentions_all_three_counts(self):
        from pid_viewer import PIDGraphicsView
        from PyQt6.QtWidgets import QGraphicsPolygonItem
        view = PIDGraphicsView()
        view.add_equipment_marker(1, 0, 0, "Ventil", tag="V-101",
                                  deviation_count=2, consequence_count=1, safeguard_count=3)
        poly = next(item for item in view._type_items['equipment']
                    if isinstance(item, QGraphicsPolygonItem))
        tip = poly.toolTip()
        self.assertIn("2 orsaker", tip)
        self.assertIn("1 konsekvens", tip)
        self.assertIn("3 safeguard", tip)

    def test_tag_and_counters_share_black_rounded_backing(self):
        from pid_viewer import PIDGraphicsView
        from PyQt6.QtWidgets import (QGraphicsEllipseItem,
                                     QGraphicsPathItem,
                                     QGraphicsSimpleTextItem)
        from PyQt6.QtGui import QColor

        view = PIDGraphicsView()
        view.add_equipment_marker(
            1, 0, 0, "Ventil", tag="V-101", deviation_count=2,
            consequence_count=1, safeguard_count=3)
        items = view._type_items['equipment']
        tag_item = next(item for item in items
                        if isinstance(item, QGraphicsSimpleTextItem)
                        and item.text() == 'V-101')
        backing = next(item for item in items
                       if isinstance(item, QGraphicsPathItem))
        badges = [item for item in items if isinstance(item, QGraphicsEllipseItem)]

        self.assertEqual(tag_item.brush().color(), QColor('#FFFFFF'))
        self.assertEqual(backing.brush().color(), QColor(0, 0, 0, 204))
        self.assertGreater(backing.path().elementCount(), 4)
        tag_right = tag_item.sceneBoundingRect().right()
        self.assertTrue(all(badge.sceneBoundingRect().left() > tag_right
                            for badge in badges))
        self.assertTrue(all(backing.path().contains(
            badge.sceneBoundingRect().center()) for badge in badges))

    def test_recommendation_badge_is_last_and_respects_visibility(self):
        from pid_viewer import PIDGraphicsView
        from PyQt6.QtWidgets import QGraphicsEllipseItem
        from PyQt6.QtGui import QColor

        view = PIDGraphicsView()
        view.add_equipment_marker(
            1, 0, 0, "Ventil", tag="V-101", deviation_count=2,
            consequence_count=3, safeguard_count=4, recommendation_count=5,
            show_cause=False, show_consequence=False, show_safeguard=False,
            show_recommendation=True)
        view.set_tree_context_highlights(
            {1: QColor(0, 200, 0)}, {1: {'recommendation'}})
        badges = [item for item in view._type_items['equipment']
                  if isinstance(item, QGraphicsEllipseItem)]
        self.assertEqual(len(badges), 1)
        self.assertEqual(badges[0].brush().color().name(), '#8e44ad')

        view = PIDGraphicsView()
        view.add_equipment_marker(
            2, 0, 0, "Ventil", tag="V-102", recommendation_count=5,
            show_recommendation=False)
        badges = [item for item in view._type_items['equipment']
                  if isinstance(item, QGraphicsEllipseItem)]
        self.assertEqual(len(badges), 3)
        self.assertFalse(any(
            item.data(view._DATA_BADGE_ROLE) == 'recommendation'
            for item in badges))


# ══════════════════════════════════════════════════════════════════════════
# Adaptive re-rasterization on zoom (2026-08-12) — "P&ID blir suddig vid
# inzoomning". PIDGraphicsView's hi-res tier used to always render at a
# FIXED fitz.Matrix scale (_RASTER_SCALE) regardless of how far the
# QGraphicsView itself was zoomed in, so zooming in past what that fixed
# scale could physically supply just stretched the same bitmap (blur).
# _target_raster_scale()/_update_page_lod() now track the current view zoom
# and re-render sharper on demand (capped so an extreme zoom can't render an
# absurdly large pixmap), without changing render_scale itself — the
# scene-units-per-pdf-point factor every coordinate transform assumes.
# ══════════════════════════════════════════════════════════════════════════

class AdaptiveRasterZoomTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_lod_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _make_pdf(self, w=400.0, h=300.0):
        import fitz
        path = os.path.join(self._tmpdir, "test.pdf")
        doc = fitz.open()
        page = doc.new_page(width=w, height=h)
        page.draw_rect(fitz.Rect(10, 10, w - 10, h - 10))
        doc.save(path)
        doc.close()
        return path

    def test_target_raster_scale_at_default_zoom_equals_base(self):
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.render_scale = view._RASTER_SCALE   # set by _render_all_pages() once a PDF is loaded
        self.assertEqual(view.transform().m11(), 1.0)
        target = view._target_raster_scale(1000.0, 800.0)
        self.assertEqual(target, view._RASTER_SCALE,
            "at normal (100%) zoom the fixed base raster is already crisp "
            "enough — must not upgrade unnecessarily")

    def test_min_pdf_line_width_adds_pixels_without_changing_page_geometry(self):
        import fitz
        from pid_viewer import _apply_min_pdf_line_width
        doc = fitz.open()
        page = doc.new_page(width=100, height=80)
        page.draw_line(fitz.Point(10, 40), fitz.Point(90, 40), width=0.2)
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3), alpha=False)
        raw0, width0, height0, stride0 = _apply_min_pdf_line_width(pix, 0)
        raw1, width1, height1, stride1 = _apply_min_pdf_line_width(pix, 1)
        self.assertEqual((width0, height0), (width1, height1))
        self.assertEqual(stride0, stride1)
        self.assertEqual(len(raw0), len(raw1))
        dark0 = sum(1 for i in range(0, len(raw0), 3)
                    if sum(raw0[i:i + 3]) < 700)
        dark1 = sum(1 for i in range(0, len(raw1), 3)
                    if sum(raw1[i:i + 3]) < 700)
        self.assertGreater(dark1, dark0)
        doc.close()

    def test_target_raster_scale_increases_with_zoom(self):
        """The bug report's exact scenario: zooming the QGraphicsView in
        past what _RASTER_SCALE alone can supply must raise the target
        render resolution instead of leaving it fixed."""
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.render_scale = view._RASTER_SCALE
        view.scale(4.0, 4.0)
        # Small page so the pixel-dimension safety cap (tested separately
        # below) doesn't also bind here — isolates the zoom-tracking behaviour.
        target = view._target_raster_scale(100.0, 80.0)
        self.assertGreater(target, view._RASTER_SCALE)
        expected = 4.0 * view.render_scale / view._LOD_MARGIN
        self.assertAlmostEqual(target, expected, places=6)

    def test_target_raster_scale_capped_for_extreme_zoom(self):
        """Safety cap: must not render an absurdly large pixmap just
        because the view zoom is extreme — bounded by pixel dimensions."""
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        view.render_scale = view._RASTER_SCALE
        view.scale(1000.0, 1000.0)
        pw, ph = 1200.0, 600.0   # dim cap = 6000/1200 = 5.0 (between base 3.0 and multiplier cap 12.0)
        target = view._target_raster_scale(pw, ph)
        self.assertAlmostEqual(target, 5.0, places=6)
        self.assertLessEqual(target * pw, view._MAX_RASTER_DIM + 1e-6)

    def test_update_page_lod_rerenders_sharper_when_zoomed_in_far(self):
        from pid_viewer import PIDGraphicsView
        path = self._make_pdf()
        view = PIDGraphicsView()
        self.assertTrue(view.load_pdf(path))
        pn = 0
        view.centerOn(view._all_page_items[pn].sceneBoundingRect().center())
        old_footprint = view._all_page_items[pn].sceneBoundingRect()

        view.scale(6.0, 6.0)   # well past what _RASTER_SCALE=3.0 alone can supply
        view._update_page_lod()
        self.assertIsNotNone(view._lod_renderer,
            "zooming in far enough must trigger a background re-render")
        self.assertTrue(view._lod_renderer.wait(5000),
            "background re-render did not finish in time")
        self.app.processEvents()

        self.assertIn(pn, view._page_cache_scale)
        self.assertGreater(view._page_cache_scale[pn], view._RASTER_SCALE,
            "must have re-rasterized ABOVE the fixed base scale once zoomed in far")
        self.assertEqual(view._page_display_scale.get(pn), view._page_cache_scale[pn])

        # The scene footprint — what scene_to_pdf()/pdf_to_scene() and every
        # marker coordinate assume — must be unchanged by the resolution
        # upgrade; only the underlying pixmap got sharper.
        new_footprint = view._all_page_items[pn].sceneBoundingRect()
        self.assertAlmostEqual(old_footprint.width(),  new_footprint.width(),  places=3)
        self.assertAlmostEqual(old_footprint.height(), new_footprint.height(), places=3)

    def test_update_page_lod_does_not_rerender_when_already_crisp_enough(self):
        """Hysteresis/debounce: a second LOD pass at the same zoom (e.g. a
        follow-up debounce tick after scrolling, with no further zoom
        change) must not kick off another background render."""
        from pid_viewer import PIDGraphicsView
        path = self._make_pdf()
        view = PIDGraphicsView()
        self.assertTrue(view.load_pdf(path))
        pn = 0
        view.centerOn(view._all_page_items[pn].sceneBoundingRect().center())

        view.scale(6.0, 6.0)
        view._update_page_lod()
        self.assertTrue(view._lod_renderer.wait(5000))
        self.app.processEvents()
        self.assertIsNotNone(view._page_display_scale.get(pn))

        view._lod_renderer = None
        view._update_page_lod()
        self.assertIsNone(view._lod_renderer,
            "must not re-render when the currently-displayed tier already satisfies the zoom")


class SmartPolylineRemovedTests(unittest.TestCase):
    """"Smart polylinje" (SmartPipeTracer-backed A* pipe-path tracing tool,
    informally reported by the user as "Smart Polygon") was torn out of
    the active app 2026-08-26 and archived to archive/smart_pipe_tracer.py
    (see NOTES.md). Confirms PIDGraphicsView carries none of its former
    state/constants/methods, and that its mode-dispatch (set_mode,
    mousePressEvent, keyPressEvent) still works cleanly for the modes that
    remain."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def test_mode_smart_polyline_constant_gone(self):
        import pid_viewer
        self.assertFalse(hasattr(pid_viewer, 'MODE_SMART_POLYLINE'))

    def test_smart_pipe_tracer_class_gone_from_pid_viewer(self):
        import pid_viewer
        self.assertFalse(hasattr(pid_viewer, 'SmartPipeTracer'))

    def test_smart_pipe_tracer_still_available_archived(self):
        from archive.smart_pipe_tracer import SmartPipeTracer
        self.assertTrue(hasattr(SmartPipeTracer, 'trace'))

    def test_view_has_no_smart_state_attrs(self):
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        for attr in ('_smart_start_pdf', '_smart_end_pdf', '_smart_paths',
                     '_smart_path_idx', '_smart_preview', '_smart_tracer',
                     '_smart_tracer_page'):
            self.assertFalse(hasattr(view, attr), f"{attr} should no longer exist")

    def test_view_has_no_smart_methods(self):
        from pid_viewer import PIDGraphicsView
        view = PIDGraphicsView()
        for name in ('_clear_smart_preview', '_draw_smart_marker',
                     '_run_smart_trace', '_show_smart_path',
                     '_confirm_smart', '_cancel_smart'):
            self.assertFalse(hasattr(view, name), f"{name} should no longer exist")

    def test_set_mode_still_works_for_surviving_modes(self):
        """Sanity check that removing the MODE_SMART_POLYLINE branch (and
        simplifying the staying_in_draw check in set_mode) didn't break
        dispatch for the modes that remain."""
        from pid_viewer import PIDGraphicsView, MODE_NAV, MODE_MARKUP_SELECT, MODE_MARKUP_POLYLINE
        view = PIDGraphicsView()
        view.set_mode(MODE_MARKUP_POLYLINE)
        self.assertEqual(view.mode, MODE_MARKUP_POLYLINE)
        view.set_mode(MODE_MARKUP_SELECT)
        self.assertEqual(view.mode, MODE_MARKUP_SELECT)
        view.set_mode(MODE_NAV)
        self.assertEqual(view.mode, MODE_NAV)

    def test_key_press_in_polyline_mode_does_not_crash_without_smart_state(self):
        """keyPressEvent's MODE_NODE/MODE_MARKUP_POLYGON/MODE_MARKUP_POLYLINE
        branch (Enter/Escape) must still run fine now that the elif branch
        for MODE_SMART_POLYLINE right after it is gone."""
        from pid_viewer import PIDGraphicsView, MODE_MARKUP_POLYLINE
        view = PIDGraphicsView()
        view.set_mode(MODE_MARKUP_POLYLINE)
        event = unittest.mock.MagicMock()
        event.key.return_value = Qt.Key.Key_Escape
        view.keyPressEvent(event)  # must not raise


if __name__ == "__main__":
    unittest.main()
