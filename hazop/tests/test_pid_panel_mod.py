#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering pid_panel_mod.py, plus any cross-module glue they
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
    QComboBox, QPushButton, QMessageBox, QInputDialog, QLineEdit, QLabel,
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

class EquipmentDeviationBarTests(unittest.TestCase):
    """The small popup shown near a clicked equipment marker on the P&ID —
    see NOTES.md 'Nod → Utrustning → Avvikelse' and the 2026-08-12
    follow-up ('en liten popup ... där jag kan välja lågt, högt flöde osv
    istället för den menyn som är nu') that turned it from a persistent
    bottom-docked bar with inline cause/frequency-combo editing into this
    auto-dismissing popup with just a deviation checklist — editing a
    cause's text/frequency once it exists is a scenario-table job now,
    not this popup's."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_equipbar_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from pid_viewer import EquipmentDeviationBar
        self.bar = EquipmentDeviationBar(self.db)
        self.eq_id = self.db.add_equipment_item("V-101", "V-101", "V", 0, "Ventil", '', 0)
        self.marker_id = self.db.add_equipment_marker(
            self.eq_id, "V-101", 0, 100.0, 100.0, "Ventil", confidence=0.9, link_method='leader')

    def tearDown(self):
        self.bar.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_load_populates_title_with_tag_and_type(self):
        self.bar.load(self.eq_id, self.marker_id)
        self.assertIn("V-101", self.bar._title_lbl.text())
        self.assertIn("Ventil", self.bar._title_lbl.text())

    def test_show_near_makes_the_popup_visible(self):
        from PyQt6.QtCore import QPoint
        self.bar.load(self.eq_id, self.marker_id)
        self.bar.show_near(QPoint(100, 100))
        self.assertTrue(self.bar.isVisible())

    def test_popup_uses_the_auto_dismiss_window_flag(self):
        """Clicking outside must close it on its own — the whole point of
        replacing the old persistent bar — which Qt.WindowType.Popup
        gives for free, no manual outside-click detection needed."""
        self.assertTrue(self.bar.windowFlags() & Qt.WindowType.Popup)

    def test_show_near_sizes_scroll_area_to_available_screen_space(self):
        """2026-08-13 feedback: 'rulllistan väldigt kort på en liten
        skärm' — the checklist's scroll area used to be pinned at a
        fixed 220px no matter how much room was actually available; it
        must now use up to its natural content height (a fresh node has
        16 auto-seeded deviations, well past what fits in 220px), bounded
        only by real screen space, and the whole popup must stay fully
        on-screen either way."""
        from PyQt6.QtCore import QPoint
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id, active_node_id=node_id)
        scr = QApplication.primaryScreen().availableGeometry()
        self.bar.show_near(QPoint(scr.center().x(), scr.center().y()))
        self.assertGreater(self.bar._checklist._checklist_scroll.maximumHeight(), 220)
        self.assertGreaterEqual(self.bar.geometry().top(), scr.top())
        self.assertLessEqual(self.bar.geometry().bottom(), scr.bottom())

    def test_show_near_keeps_popup_on_screen_when_clicked_near_bottom_edge(self):
        """A click near the screen's bottom edge must open the popup
        UPWARD instead of letting it run off-screen — this is the actual
        'liten skärm' scenario: less room below the click than the
        checklist's natural height needs."""
        from PyQt6.QtCore import QPoint
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id, active_node_id=node_id)
        scr = QApplication.primaryScreen().availableGeometry()
        self.bar.show_near(QPoint(scr.center().x(), scr.bottom() - 20))
        self.assertGreaterEqual(self.bar.geometry().top(), scr.top())
        self.assertLessEqual(self.bar.geometry().bottom(), scr.bottom())

    def test_checking_deviation_without_a_node_selected_is_a_no_op(self):
        self.bar.load(self.eq_id, self.marker_id)
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 0)
        # No node yet (no active_node_id given, equipment has none of its
        # own) — checkboxes must be disabled, nothing to toggle.
        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        self.assertFalse(checkbox.isEnabled())

    def test_checking_deviation_after_node_selected_creates_it(self):
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)

        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)

        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)
        self.assertEqual(self.db.equipment_node_id(self.eq_id), node_id)

    def test_smart_node_default_assigns_active_node_when_equipment_has_none(self):
        """See NOTES.md 'Slippa välja nod varje gång': the popup assigns
        PIDPanel._active_node_id immediately when the equipment has no node
        of its own yet, so checking a deviation works right away instead of
        forcing a manual node pick every time — explicit user request."""
        node_id = self.db.add_node()
        self.bar.load(self.eq_id, self.marker_id, active_node_id=node_id)
        self.assertEqual(self.db.equipment_node_id(self.eq_id), node_id)
        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        self.assertTrue(checkbox.isEnabled())

    def test_smart_node_default_does_not_override_existing_node(self):
        node_id = self.db.add_node()
        other_node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id, active_node_id=other_node_id)
        self.assertEqual(self.db.equipment_node_id(self.eq_id), node_id)

    def test_number_key_shortcut_toggles_matching_checkbox(self):
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)

        self.assertGreaterEqual(len(self.bar._checklist._checklist_checkboxes), 1)
        self.bar._checklist._toggle_checkbox_by_number(1)

        self.assertTrue(self.bar._checklist._checklist_checkboxes[0].isChecked())
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)

    def test_number_key_shortcut_out_of_range_is_a_no_op(self):
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)
        # One past the last real row — must not raise or toggle anything.
        out_of_range = len(self.bar._checklist._checklist_checkboxes) + 1
        self.bar._checklist._toggle_checkbox_by_number(out_of_range)
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 0)

    def _select_node_and_stub_cause_creation(self):
        """Shared setup for the suggested-cause tests: assigns a node and
        installs a fake _create_cause_fn that creates a real cause row via
        Database directly, standing in for PIDPanel._create_cause_for_bar
        (which needs a real P&ID marker/scene this test class doesn't
        construct).

        Uses a Pump equipment item rather than self.eq_id ("Ventil") because
        standard_causes is only seeded per specific valve/equipment
        sub-type (e.g. "Manuell ventil", "On-off ventil") — "Pump" is
        seeded and matches the user's own example ("Lågt flöde" + Pump →
        "Pump stopp")."""
        pump_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        pump_marker_id = self.db.add_equipment_marker(
            pump_id, "P-101", 0, 200.0, 200.0, "Pump", confidence=0.9, link_method='leader')
        node_id = self.db.add_node()
        self.db.set_equipment_node(pump_id, node_id)
        self.bar.load(pump_id, pump_marker_id)

        created = {'pump_id': pump_id, 'node_id': node_id}

        def fake_create_cause(dev_id, comp_type, comp_tag, description, frequency=None):
            cause_id = self.db.add_cause(dev_id)
            self.db.update_cause(cause_id, description, comp_type=comp_type, comp_tag=comp_tag)
            if frequency is not None:
                # Stand-in for place_cause_from_template's real
                # _compute_f_level() conversion — this test class only
                # needs base_frequency to actually persist.
                self.db.update_cause(cause_id, likelihood=0, base_frequency=frequency)
            created['cause_id'] = cause_id
            created['dev_id'] = dev_id
            created['frequency'] = frequency
            created['description'] = description
            return cause_id

        self.bar._create_cause_fn = fake_create_cause
        return created

    def test_checking_deviation_auto_creates_suggested_cause(self):
        """Förenklat orsaksval, ta bort dubbla val (NOTES.md): checking the
        deviation alone must create the top-suggested cause immediately —
        no separate chip/dropdown needed, that editing now happens in the
        scenario table once the cause row exists."""
        created = self._select_node_and_stub_cause_creation()

        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)

        self.assertIn('cause_id', created)
        causes = self.db.causes_for_deviation(created['dev_id'])
        self.assertEqual(len(causes), 1)
        self.assertEqual(causes[0]['description'], created['description'])

    def test_checking_deviation_passes_through_seeded_frequency(self):
        """'Pump stopp' is seeded with a real frequency estimate
        (standard_causes.frequency) — auto-creating it on check must pass
        that through to _create_cause_fn (and from there to
        place_cause_from_template's _compute_f_level conversion) instead
        of discarding it, per the user's own request: 'får gärna vara
        kopplad till databasen med frekvenser'."""
        created = self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)
        self.assertIsNotNone(created.get('frequency'),
                              "expected the seeded standard_causes.frequency to flow through")

    def test_generic_equipment_type_resolves_object_based_causes(self):
        """Bug report: 'Orsaksväljaren skall ge fler alternativ'.
        equipment_type 'Ventil' matches ZERO standard_causes.comp_type rows
        directly (seeding is per specific sub-type like 'Manuell
        ventil'/'On-off ventil'), so the checklist used to fall back to the
        full unfiltered standard_deviations catalogue with no cause
        suggestions at all. The object-based fallback (_resolve_object_id +
        standard_causes_for_object) must find a substring match
        (_obj_type_matches) and produce a real suggestion instead — proven
        here by checking the deviation and confirming a real (non-blank)
        cause got auto-created for it, not just an empty placeholder."""
        self.assertEqual(
            self.db.standard_causes_for_comp_type("Ventil"), [],
            "sanity check: literal comp_type match is empty for this generic label")
        obj_id = self.bar._checklist._resolve_object_id("Ventil")
        self.assertIsNotNone(
            obj_id, "expected a substring match against standard_objects (e.g. 'Manuell ventil')")

        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)
        captured = []
        self.bar._create_cause_fn = lambda dev_id, ct, tag, desc, freq=None: (
            captured.append(desc), self.db.add_cause(dev_id))[-1]
        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)
        self.assertTrue(captured, "expected a real cause suggestion once the object-based fallback resolves")

    def test_unchecking_deviation_without_causes_deletes_silently(self):
        """Kryssrutan ska gå att av-/aktivera (NOTES.md) — unchecking a
        deviation that never got a cause (e.g. no template match, user
        never picked one) must delete it right away with no confirmation
        prompt (nothing meaningful to lose)."""
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)

        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        with unittest.mock.patch('pid_viewer.QMessageBox.question') as mock_q:
            checkbox.setChecked(True)
            self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)
            checkbox.setChecked(False)
        mock_q.assert_not_called()
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 0)
        self.assertTrue(checkbox.isEnabled())
        self.assertFalse(checkbox.isChecked())

    def test_unchecking_deviation_with_causes_asks_for_confirmation(self):
        """A deviation with a real cause attached must be confirmed before
        deletion — same pattern as ScenarioTablePanel's own 'Ta bort
        orsak'/'Ta bort konsekvens' confirmations."""
        created = self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)   # auto-creates a cause via the stub
        self.assertIn('cause_id', created)

        with unittest.mock.patch('pid_viewer.QMessageBox.question',
                                  return_value=QMessageBox.StandardButton.No) as mock_q:
            checkbox.setChecked(False)
        mock_q.assert_called_once()
        # Declined -> deviation and cause both survive, checkbox reverts.
        self.assertTrue(checkbox.isChecked())
        self.assertIsNotNone(self.db.get_cause(created['cause_id']))

        with unittest.mock.patch('pid_viewer.QMessageBox.question',
                                  return_value=QMessageBox.StandardButton.Yes):
            checkbox.setChecked(False)
        self.assertFalse(checkbox.isChecked())
        self.assertIsNone(self.db.get_cause(created['cause_id']))
        self.assertEqual(self.db.equipment_deviation_count(created['pump_id']), 0)

    def test_unchecking_emits_deviation_removed(self):
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)

        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)

        received = []
        self.bar.deviation_removed.connect(lambda dev_id, eq_id: received.append((dev_id, eq_id)))

        with unittest.mock.patch('pid_viewer.QMessageBox.question'):
            checkbox.setChecked(True)
            checkbox.setChecked(False)

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1], self.eq_id)

    def test_can_recheck_after_unchecking(self):
        """The whole point: checking, unchecking, and checking again must
        all work — not a one-way lock like the old v1 behavior."""
        node_id = self.db.add_node()
        self.db.set_equipment_node(self.eq_id, node_id)
        self.bar.load(self.eq_id, self.marker_id)

        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        with unittest.mock.patch('pid_viewer.QMessageBox.question'):
            checkbox.setChecked(True)
            checkbox.setChecked(False)
            checkbox.setChecked(True)
        self.assertTrue(checkbox.isChecked())
        self.assertTrue(checkbox.isEnabled())
        self.assertEqual(self.db.equipment_deviation_count(self.eq_id), 1)

    def test_unchecking_confirmation_states_full_cascade_counts(self):
        """The confirmation message must count consequences/safeguards too,
        not just causes (2026-08-09, see NOTES.md) — a deviation's single
        cause can carry several consequences, each with several
        safeguards, and the old message ('har N orsak(er) kopplade')
        silently understated how much data a checkbox-uncheck would
        actually destroy."""
        created = self._select_node_and_stub_cause_creation()
        row_widget = self.bar._checklist._checklist_layout.itemAt(0).widget()
        checkbox = row_widget.findChild(QCheckBox)
        checkbox.setChecked(True)   # auto-creates a cause via the stub
        cause_id = created['cause_id']
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)
        self.db.add_safeguard(cons_id)

        with unittest.mock.patch(
                'pid_viewer.QMessageBox.question',
                return_value=QMessageBox.StandardButton.No) as mock_q:
            checkbox.setChecked(False)

        mock_q.assert_called_once()
        message = mock_q.call_args[0][2]
        self.assertIn("1 orsak", message)
        self.assertIn("1 konsekvens", message)
        self.assertIn("2 barriär", message)
        self.assertTrue(checkbox.isChecked(),
                         "declining the confirmation must leave the checkbox checked")
        self.assertEqual(self.db.equipment_deviation_count(created['pump_id']), 1)


class EquipmentObjectPlacementTests(unittest.TestCase):
    """P&ID right-click -> "🔧 Objekt" (2026-08-07, see NOTES.md) —
    PIDPanel.place_equipment_marker resolves an existing equipment_catalog
    row by tag (never creates a duplicate) or creates a new one, places a
    marker at the clicked point, and opens EquipmentPlacementPopup
    immediately (2026-08-18: tag+typ fields AND the deviation checklist
    together, replacing the old two-step EquipmentTagPopup then
    EquipmentDeviationBar sequence) so filling in the tag/type and
    ticking a deviation are all available right away."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_objplacement_test_")
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

    def test_new_tag_creates_catalog_row_and_marker(self):
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("HV-201", "Ventil", QPointF(10, 10), 0)

        equip = self.db.get_equipment_by_tag("HV-201")
        self.assertIsNotNone(equip)
        self.assertEqual(equip['equipment_type'], "Ventil")
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]['equipment_id'], equip['id'])

    def test_existing_tag_reuses_catalog_row_no_duplicate(self):
        from PyQt6.QtCore import QPointF
        eq_id = self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)

        self.panel.place_equipment_marker("P-101", "Pump", QPointF(20, 20), 0)

        self.assertEqual(len(self.db.equipment_items()), 1,
            "must not create a duplicate catalog row for an already-known tag")
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(markers[0]['equipment_id'], eq_id)

    def test_placement_opens_equipment_placement_popup(self):
        """2026-08-18: opens EquipmentPlacementPopup (tag+typ+avvikelser
        combined), not the old EquipmentDeviationBar — isVisible() would
        report False here regardless (the panel itself is never shown in
        this headless test, and QWidget.isVisible() reflects the whole
        ancestor chain, not just this widget's own setVisible(True)
        call), so assert on which equipment/marker it's bound to and that
        the tag/type fields are pre-filled instead."""
        from PyQt6.QtCore import QPointF
        from pid_panel_mod import EquipmentPlacementPopup
        self.panel.place_equipment_marker("T-301", "Behållare", QPointF(5, 5), 0)
        equip_row = self.db.get_equipment_by_tag("T-301")
        self.assertIsNotNone(equip_row)
        popups = self.panel.viewer.findChildren(EquipmentPlacementPopup)
        self.assertEqual(len(popups), 1)
        popup = popups[0]
        self.assertEqual(popup._equipment_id, equip_row['id'])
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(popup._marker_id, markers[0]['id'])
        self.assertEqual(popup._tag_edit.text(), "T-301")
        self.assertEqual(popup._type_cb.currentText(), "Behållare")

    def test_blank_tag_still_creates_marker_from_type_alone(self):
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("", "Pump", QPointF(1, 1), 0)
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(len(markers), 1)
        equip = self.db.get_equipment_by_id(markers[0]['equipment_id'])
        self.assertEqual(equip['equipment_type'], "Pump")


class EquipmentPlacementRubberBandSimplePopupTests(unittest.TestCase):
    """Rubber-band placements (pdf_rect given) get a simplified popup —
    Objekt + Objekttyp only, no deviation checklist — positioned beside
    the drawn rectangle instead of on top of it (2026-08-24, see NOTES.md
    "Åtta UX/logik-förbättringar"). A plain right-click placement
    (pdf_rect=None, covered by EquipmentObjectPlacementTests above) keeps
    the original full popup with its checklist, unchanged."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_rbplacement_test_")
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

    def _place_with_rect(self, tag="PV-101", comp_type="Ventil"):
        from PyQt6.QtCore import QPointF, QRectF
        from pid_panel_mod import EquipmentPlacementPopup
        rect = QRectF(100, 100, 50, 50)
        center = QPointF(rect.center().x(), rect.center().y())
        self.panel.place_equipment_marker(tag, comp_type, center, 0, pdf_rect=rect)
        popups = self.panel.viewer.findChildren(EquipmentPlacementPopup)
        self.assertEqual(len(popups), 1)
        return popups[0]

    def test_rubber_band_placement_uses_simple_popup_with_no_checklist(self):
        popup = self._place_with_rect()
        self.assertTrue(popup._simple)
        self.assertIsNone(popup._checklist,
            "the rubber-band popup must not embed a deviation checklist")

    def test_plain_click_placement_keeps_full_popup_with_checklist(self):
        from PyQt6.QtCore import QPointF
        from pid_panel_mod import EquipmentPlacementPopup
        self.panel.place_equipment_marker("PV-101", "Ventil", QPointF(10, 10), 0)
        popup = self.panel.viewer.findChildren(EquipmentPlacementPopup)[0]
        self.assertFalse(popup._simple)
        self.assertIsNotNone(popup._checklist)

    def test_add_type_button_has_visible_text_not_a_bare_plus(self):
        popup = self._place_with_rect()
        add_type_btns = [b for b in popup.findChildren(QPushButton)
                          if "Lägg till" in b.text()]
        self.assertEqual(len(add_type_btns), 1,
            "the add-object-type button must have real, visible text, not a bare '+'")

    def test_object_field_label_says_objekt_not_tag(self):
        """The simplified popup's field is framed as "Objekt"/"Objekttyp"
        (per the request), not the full popup's "Tag"/"Typ" wording."""
        popup = self._place_with_rect()
        labels = [w.text() for w in popup.findChildren(QLabel)]
        self.assertIn("Objekt:", labels)
        self.assertIn("Objekttyp:", labels)

    def test_show_near_rect_positions_beside_not_over_the_rect(self):
        from pid_panel_mod import EquipmentPlacementPopup
        from PyQt6.QtCore import QRect
        eq_id = self.db.add_equipment_item("", "", "", 0, "", "", 0)
        popup = EquipmentPlacementPopup(self.db, eq_id, None,
                                        parent=self.panel.viewer, simple=True)
        try:
            rect = QRect(100, 100, 40, 40)
            popup.show_near_rect(rect.left(), rect.top(), rect.right(), rect.bottom())
            popup_geo = QRect(popup.pos(), popup.frameGeometry().size())
            self.assertFalse(popup_geo.intersects(rect),
                f"popup {popup_geo} must not overlap the marked rect {rect}")
        finally:
            popup.deleteLater()


class EquipmentPlacementAsyncSearchTests(unittest.TestCase):
    """PIDPanel.place_equipment_marker's async tag search (2026-08-18, see
    NOTES.md "kombinerad placeringsmeny") — EquipmentPlacementPopup shows
    instantly, a background EquipmentTagSearchWorker fills in the tag
    field once it resolves (or a configurable timeout gives up first),
    and never clobbers text the user already typed themselves."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        import fitz
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_asyncsearch_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        db_path = Path(self.db.path)
        self.pdf_path = str(db_path.with_name(db_path.stem + '_pid.pdf'))
        doc = fitz.open()
        page = doc.new_page(width=400.0, height=300.0)
        page.insert_text((60, 60), "PV-101")
        doc.save(self.pdf_path)
        doc.close()

        from pid_viewer import PIDPanel
        self.panel = PIDPanel(self.db)
        self.panel.viewer.load_pdf(self.pdf_path)

    def tearDown(self):
        # A test that starts a real background EquipmentTagSearchWorker
        # and doesn't itself wait for it (e.g. only checking the spinner
        # appeared) must not leave it running past the test — PyQt
        # crashes ("QThread: Destroyed while thread is still running")
        # if a QThread object is torn down mid-run.
        for worker in list(self.panel._tag_search_workers):
            worker.wait(2000)
        # 2026-08-24: a popup's _tag_edit losing focus as part of the
        # deleteLater() teardown cascade below can synthesize a real
        # editingFinished -> _commit_tag() call — deleteLater() is
        # deferred, so this sometimes only actually fires during a LATER
        # test's own processEvents() call, calling _commit_tag() with its
        # default show_warning=True against a duplicate tag on an object
        # nobody is looking at anymore (found via a real hang once
        # _commit_tag started raising a blocking QMessageBox on a
        # duplicate, see NOTES.md). blockSignals is a persistent per-
        # object flag, so this protects against the callback firing no
        # matter which test's event loop turn actually processes the
        # deletion.
        from pid_panel_mod import EquipmentPlacementPopup
        for popup in self.panel.viewer.findChildren(EquipmentPlacementPopup):
            popup._tag_edit.blockSignals(True)
        self.panel.deleteLater()
        # This class creates several real QThreads/QGraphicsView-backed
        # popups per test (a heavier mix than most fixtures in this file)
        # — an explicit processEvents()+gc.collect() here actually reclaims
        # native widget/thread handles between tests instead of letting
        # them pile up across the whole class, same mitigation
        # _TempDbMainWindow.__exit__ already uses for the same reason
        # (see NOTES.md "misstänkt resursuttömning").
        self.app.processEvents()
        gc.collect()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _popup(self):
        from pid_panel_mod import EquipmentPlacementPopup
        popups = self.panel.viewer.findChildren(EquipmentPlacementPopup)
        self.assertEqual(len(popups), 1)
        return popups[0]

    def test_blank_tag_starts_background_search_and_shows_spinner(self):
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("", "Ventil", QPointF(60, 60), 0)
        popup = self._popup()
        self.assertTrue(popup._searching_lbl.isVisible())
        self.assertEqual(len(self.panel._tag_search_workers), 1)

    def test_known_tag_skips_the_search_entirely(self):
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("V-999", "Ventil", QPointF(60, 60), 0)
        popup = self._popup()
        self.assertFalse(popup._searching_lbl.isVisible())
        self.assertEqual(len(self.panel._tag_search_workers), 0)

    def test_worker_result_fills_tag_field_when_user_has_not_typed(self):
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("", "Ventil", QPointF(60, 60), 0)
        popup = self._popup()
        worker = self.panel._tag_search_workers[0]
        self.assertTrue(worker.wait(5000), "worker.run() did not finish within 5s")
        self.app.processEvents()

        self.assertEqual(popup._tag_edit.text(), "PV-101")
        self.assertFalse(popup._searching_lbl.isVisible())
        self.assertEqual(self.db.get_equipment_by_id(popup._equipment_id)['tag'], "PV-101")
        self.assertEqual(len(self.panel._tag_search_workers), 0,
            "the worker must remove itself from the keep-alive list once finished")

    def test_user_typed_tag_not_overwritten_by_late_async_result(self):
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("", "Ventil", QPointF(60, 60), 0)
        popup = self._popup()

        popup._tag_edit.setText("MANUAL-1")
        popup._on_tag_edited_by_user("MANUAL-1")   # what the real textEdited signal would do

        popup.set_detected_tag("PV-101")   # simulates the late async result arriving

        self.assertEqual(popup._tag_edit.text(), "MANUAL-1")
        self.assertFalse(popup._searching_lbl.isVisible(),
            "the spinner must still be hidden once ANY result (used or not) arrives")

    def test_timeout_clears_spinner_and_leaves_tag_blank_for_manual_entry(self):
        """QTimer.singleShot is mocked so the timeout fires deterministically
        and instantly instead of requiring a real wait — this also
        confirms the configured setting value is what gets used."""
        from PyQt6.QtCore import QPointF
        self.db.set_config('equipment_tag_search_timeout_ms', '500')
        with unittest.mock.patch('pid_panel_mod.QTimer.singleShot') as mock_singleshot:
            self.panel.place_equipment_marker("", "Ventil", QPointF(60, 60), 0)
            popup = self._popup()
            self.assertTrue(popup._searching_lbl.isVisible())
            mock_singleshot.assert_called_once()
            timeout_ms, on_timeout = mock_singleshot.call_args[0]
            self.assertEqual(timeout_ms, 500)
            on_timeout()   # simulate the timeout firing before the worker ever finishes

        self.assertFalse(popup._searching_lbl.isVisible())
        self.assertEqual(popup._tag_edit.text(), "")

    def test_reassign_to_existing_merges_typed_tag_without_creating_duplicate(self):
        """A tag typed after placement can turn out to already belong to
        a different, real catalog row — must reuse it, not leave a
        duplicate (same "aldrig dubbletter" guarantee
        place_equipment_marker's own creation-time check already
        enforces, see NOTES.md)."""
        from PyQt6.QtCore import QPointF
        existing_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)

        self.panel.place_equipment_marker("", "Ventil", QPointF(200, 200), 0)
        popup = self._popup()
        placeholder_id = popup._equipment_id
        self.assertNotEqual(placeholder_id, existing_id)

        popup._tag_edit.setText("PV-101")
        # 2026-08-24: _commit_tag now also raises a blocking QMessageBox
        # warning on a duplicate tag (see NOTES.md) — mock it out so the
        # test doesn't hang waiting for a real dialog to be dismissed.
        with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn:
            popup._commit_tag()
        mock_warn.assert_called_once()
        warned_text = mock_warn.call_args[0][2]
        self.assertIn("PV-101", warned_text,
            "the warning must clearly name the duplicate tag number")

        self.assertEqual(popup._equipment_id, existing_id)
        self.assertIsNone(self.db.get_equipment_by_id(placeholder_id),
            "the empty placeholder row must be deleted after merging")
        self.assertEqual(len(self.db.equipment_items()), 1)
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0]['equipment_id'], existing_id)

    def test_reassign_does_not_merge_once_a_deviation_was_already_checked(self):
        """If the user already ticked a deviation box against the
        placeholder before a conflicting tag arrives, merging would
        silently orphan that real data — must leave the placeholder as
        a (rare, informational-only) duplicate instead."""
        from PyQt6.QtCore import QPointF
        node_id = self.db.add_node()
        existing_id = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", '', 0)

        self.panel.place_equipment_marker("", "Ventil", QPointF(200, 200), 0)
        popup = self._popup()
        placeholder_id = popup._equipment_id
        self.panel._active_node_id = node_id
        popup.load_checklist(active_node_id=node_id)
        popup._checklist._checklist_checkboxes[0].setChecked(True)
        self.assertTrue(self.db.deviations_for_equipment(placeholder_id))

        popup._tag_edit.setText("PV-101")
        with unittest.mock.patch.object(QMessageBox, 'warning'):
            popup._commit_tag()

        self.assertEqual(popup._equipment_id, placeholder_id,
            "must not merge away a placeholder that already has real data")
        self.assertIsNotNone(self.db.get_equipment_by_id(placeholder_id))
        self.assertIn("finns redan i katalogen", popup._dup_hint.text())

    def test_checking_a_deviation_in_the_new_popup_creates_a_cause(self):
        """The embedded _DeviationChecklist inside EquipmentPlacementPopup
        must create causes exactly like EquipmentDeviationBar's own
        checklist already does (2026-08-18 — same underlying widget,
        see NOTES.md)."""
        from PyQt6.QtCore import QPointF
        node_id = self.db.add_node()
        self.panel._active_node_id = node_id

        self.panel.place_equipment_marker("V-1", "Ventil", QPointF(60, 60), 0)
        popup = self._popup()
        self.assertTrue(popup._checklist._checklist_checkboxes)

        popup._checklist._checklist_checkboxes[0].setChecked(True)

        eq = self.db.get_equipment_by_tag("V-1")
        devs = self.db.deviations_for_equipment(eq['id'])
        self.assertEqual(len(devs), 1)
        causes = self.db.causes_for_deviation(devs[0]['id'])
        self.assertEqual(len(causes), 1)

    def test_checking_a_deviation_creates_a_cause_when_type_is_picked_after_placement(self):
        """The real rubber-band/right-click flow always places equipment
        with comp_type='' (see hazop.py _on_equipment_placement_requested,
        2026-08-18) — the type is picked in THIS popup afterward, not
        known up front like the test above assumes. _commit_type() must
        rebuild the checklist so its per-row standard-cause lookup (keyed
        on comp_type) sees the type just picked; without that rebuild, a
        box ticked after choosing a type created the deviation but never
        the auto-suggested cause that goes with it (2026-08-18 follow-up:
        "läggs det inte till någon standardorsak när jag definerat
        objekttyp + avikelse som innan")."""
        from PyQt6.QtCore import QPointF
        node_id = self.db.add_node()
        self.panel._active_node_id = node_id

        self.panel.place_equipment_marker("", "", QPointF(60, 60), 0)
        popup = self._popup()

        idx = popup._type_cb.findText("Ventil")
        if idx < 0:
            popup._type_cb.addItem("Ventil")
            idx = popup._type_cb.count() - 1
        popup._type_cb.setCurrentIndex(idx)
        popup._commit_type()

        self.assertTrue(popup._checklist._checklist_checkboxes)
        popup._checklist._checklist_checkboxes[0].setChecked(True)

        eq = self.db.get_equipment_by_id(popup._equipment_id)
        devs = self.db.deviations_for_equipment(eq['id'])
        self.assertEqual(len(devs), 1)
        causes = self.db.causes_for_deviation(devs[0]['id'])
        self.assertEqual(len(causes), 1,
            "checking a box after picking a type must still auto-create "
            "the type's suggested standard cause")

    def test_checking_a_deviation_refreshes_tree_and_scenario(self):
        """A checked deviation box must tell the rest of the app to
        redraw, exactly like EquipmentDeviationBar's existing-marker popup
        already does — otherwise the new cause exists in the database but
        nothing on screen shows it (2026-08-18 follow-up: "Jag ser
        dessutom inget i hazop scenario när jag klickar"). Regression
        test for a missing signal connection: EquipmentPlacementPopup's
        embedded checklist emitted deviation_added just fine, but nothing
        forwarded it out of the popup to PIDPanel."""
        from PyQt6.QtCore import QPointF
        node_id = self.db.add_node()
        self.panel._active_node_id = node_id
        fired = []
        self.panel.equipment_deviation_created.connect(
            lambda dev_id, eq_id: fired.append((dev_id, eq_id)))

        self.panel.place_equipment_marker("V-2", "Ventil", QPointF(60, 60), 0)
        popup = self._popup()
        popup._checklist._checklist_checkboxes[0].setChecked(True)

        eq = self.db.get_equipment_by_tag("V-2")
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0][1], eq['id'])


class ObjectMenuAndToolbarButtonsTests(unittest.TestCase):
    """"⚙️ Orsak"/"⚠️ Konsekvens" mode-toggle buttons removed from the P&ID
    toolbar (2026-08-07, see NOTES.md). The right-click menu's own "Orsak"/
    "Konsekvens"/"Safeguard" actions, and the MODE_CAUSE_TEMPLATE/
    MODE_CONSEQUENCE/MODE_SAFEGUARD modes they drove, were later removed
    entirely (2026-08-13, see NOTES.md: the P&ID canvas is now
    object-placement-only) — "🔧 Objekt" is the only right-click action
    left that creates something new."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_toolbarmenu_test_")
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

    def test_only_navigate_button_remains(self):
        """The toolbar has exactly one mode button — Navigera. The P&ID
        canvas is object-placement-only (2026-08-13, see NOTES.md);
        equipment placement is armed via the right-click/rubber-band menus,
        not via its own toolbar toggle."""
        from pid_viewer import MODE_NAV
        self.assertIn(MODE_NAV, self.panel.mode_buttons)
        self.assertEqual(len(self.panel.mode_buttons), 1)

    def test_context_menu_equipment_action_emits_placement_signal(self):
        from PyQt6.QtCore import QPointF
        captured = []
        self.panel.equipment_placement_requested.connect(
            lambda tag, pos, page: captured.append((tag, pos, page)))
        pt = QPointF(7, 7)

        self.panel._on_context_action('equipment', pt, 2)

        self.assertEqual(len(captured), 1)
        tag, pos, page = captured[0]
        self.assertEqual(page, 2)
        self.assertEqual(pos, pt)

    def test_context_menu_find_similar_action_warns_with_no_pid_open(self):
        """"🔎 Hitta liknande symbol" (2026-08-10, see NOTES.md) routes to
        _find_similar_symbol — with no PDF loaded (this fixture's
        default) it must warn, not crash."""
        from PyQt6.QtCore import QPointF
        from PyQt6.QtWidgets import QMessageBox
        with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn:
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_warn.assert_called_once()

    def _mock_accepted_search_params_dialog(self, mock_dlg_cls, final_results=None):
        """SimilarSymbolSearchDialog is a real modal QDialog that runs
        its own background scan (2026-08-15, see NOTES.md "Hitta
        liknande symbol" — uppföljningsfunktioner) — tests that just
        want the flow past it (not the dialog itself) mock the class
        and stub final_results(), mirroring how other modal-dialog call
        sites in this file are tested (see
        test_edit_extra_defers_rebuild_instead_of_calling_it_directly)."""
        from PyQt6.QtWidgets import QDialog
        inst = mock_dlg_cls.return_value
        inst.exec.return_value = QDialog.DialogCode.Accepted
        inst.final_results.return_value = final_results if final_results is not None else []
        return inst

    def test_find_similar_symbol_falls_back_to_image_mode_when_no_reference_cluster_resolved(self):
        """2026-08-15 (see NOTES.md "Bildbaserad 'hitta liknande symbol'
        — vid sidan av vektorlogiken"): a click with no vector cluster
        nearby (a scanned page, or an empty spot) no longer dead-ends
        with a rejection message — it opens SimilarSymbolSearchDialog in
        forced image-matching mode instead, with primitives/index_group
        both None and a click-centered ref_bbox."""
        from PyQt6.QtCore import QPointF
        from PyQt6.QtWidgets import QMessageBox, QDialog
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=None), \
             unittest.mock.patch.object(QMessageBox, 'information') as mock_info, \
             unittest.mock.patch('pid_panel_mod.SimilarSymbolSearchDialog') as mock_params_dlg:
            mock_params_dlg.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_info.assert_not_called()
        mock_params_dlg.assert_called_once()
        args, kwargs = mock_params_dlg.call_args
        self.assertIsNone(args[0])
        self.assertIsNone(args[1])
        self.assertIn('ref_bbox', kwargs)
        self.assertIsNotNone(kwargs['ref_bbox'])

    def test_find_similar_symbol_does_not_check_results_when_params_dialog_cancelled(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtWidgets import QDialog
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=([], [], {'bbox': (0, 0, 10, 10)})), \
             unittest.mock.patch('pid_panel_mod.SimilarSymbolSearchDialog') as mock_params_dlg:
            mock_params_dlg.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_params_dlg.return_value.final_results.assert_not_called()

    def test_find_similar_symbol_shows_info_when_nothing_found(self):
        from PyQt6.QtCore import QPointF
        from PyQt6.QtWidgets import QMessageBox
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=([], [], {'bbox': (0, 0, 10, 10)})), \
             unittest.mock.patch('pid_panel_mod.SimilarSymbolSearchDialog') as mock_params_dlg, \
             unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
            self._mock_accepted_search_params_dialog(mock_params_dlg, final_results=[])
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_info.assert_called_once()

    def test_find_similar_symbol_opens_review_dialog_and_reloads_on_accept(self):
        from PyQt6.QtCore import QPointF
        fake_results = [{'tag': '', 'page': 0, 'comp_type': '', 'x': 1.0, 'y': 1.0,
                         'outline': [], 'link_method': 'similar', 'tag_status': 'untagged',
                         'temporary_id': 'SIMILAR-0-0', 'detection_confidence': 0.9}]
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=([], [], {'bbox': (0, 0, 10, 10)})), \
             unittest.mock.patch('pid_panel_mod.SimilarSymbolSearchDialog') as mock_params_dlg, \
             unittest.mock.patch('pid_panel_mod.EquipmentMarkerReviewDialog') as mock_dlg_cls, \
             unittest.mock.patch.object(self.panel, 'reload_overlays') as mock_reload:
            self._mock_accepted_search_params_dialog(mock_params_dlg, final_results=fake_results)
            mock_dlg_cls.return_value.exec.return_value = 1   # QDialog.Accepted
            self.panel._on_context_action('find_similar', QPointF(5, 5), 0)
        mock_dlg_cls.assert_called_once()
        args, kwargs = mock_dlg_cls.call_args
        self.assertEqual(args[0], fake_results)
        mock_reload.assert_called_once()

    def test_find_similar_symbol_constructs_dialog_with_pdf_path_page_scale_and_viewer(self):
        """The dialog now runs its own background scan, so it needs the
        PDF path (its worker opens its own fitz.Document), the
        reference page, the page's text scale, and the viewer (for the
        on-canvas preview) — not just primitives/index_group."""
        from PyQt6.QtCore import QPointF
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        self.panel.viewer._pdf_path = '/fake/path.pdf'
        fake_primitives = [{'bbox': (1.0, 2.0, 3.0, 4.0)}]
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=(fake_primitives, [0], {'bbox': (0, 0, 10, 10)})), \
             unittest.mock.patch('pid_viewer.symbol_geometry.dominant_text_size',
                                 return_value=12.5), \
             unittest.mock.patch('pid_viewer.symbol_geometry.primitives_near_point',
                                 return_value=[]), \
             unittest.mock.patch('pid_panel_mod.SimilarSymbolSearchDialog') as mock_params_dlg:
            self._mock_accepted_search_params_dialog(mock_params_dlg, final_results=[])
            with unittest.mock.patch.object(QMessageBox, 'information'):
                self.panel._on_context_action('find_similar', QPointF(5, 5), 3)
        args, kwargs = mock_params_dlg.call_args
        self.assertEqual(args[0], fake_primitives)
        # No nearby primitives found (mocked empty) — the widened group is
        # just the auto-detected native group, unioned with nothing.
        self.assertEqual(args[1], [0])
        self.assertEqual(args[2], '/fake/path.pdf')
        self.assertEqual(args[3], 3)
        self.assertEqual(args[4], 12.5)
        self.assertEqual(kwargs['viewer'], self.panel.viewer)
        self.assertEqual(kwargs['native_index_group'], [0])
        self.assertEqual(kwargs['initial_excluded'], set())

    def test_find_similar_symbol_ref_bbox_covers_the_widened_group_not_just_the_tiny_core(self):
        """Bildmatchning's reference crop is built directly from this
        ref_bbox (see SimilarSymbolSearchDialog._render_image_preview) —
        it must cover the whole connectivity-widened group, not just
        resolve_reference_cluster's own auto-detected core, or a
        densely-fragmented file whose core seed is a single tiny
        fragment (confirmed on the active project's own
        hazop_project_pid.pdf: a real instrument bubble's resolved core
        was one lone 6x6pt curve — a single corner of the circle, while
        the connectivity-widened group's own bbox tightly covered the
        whole circle+label) crops Bildmatchning down to a sliver of the
        actual symbol instead of the whole thing (2026-08-16, see
        NOTES.md "Bildmatchning klipper fel — visar bara en del av det
        markerade fältet")."""
        from PyQt6.QtCore import QPointF
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        self.panel.viewer._pdf_path = '/fake/path.pdf'
        fake_primitives = [
            {'bbox': (10.0, 10.0, 11.0, 11.0)},   # index 0: tiny native "core"
            {'bbox': (0.0, 0.0, 30.0, 25.0)},     # index 1: much bigger, widened-in
        ]
        with unittest.mock.patch('pid_viewer.equipment_detection.resolve_reference_cluster',
                                 return_value=(fake_primitives, [0],
                                               {'bbox': (10.0, 10.0, 11.0, 11.0)})), \
             unittest.mock.patch('pid_viewer.symbol_geometry.dominant_text_size',
                                 return_value=12.5), \
             unittest.mock.patch('pid_viewer.symbol_geometry.primitives_near_point',
                                 return_value=[1]), \
             unittest.mock.patch('pid_viewer.symbol_geometry.widen_by_connectivity',
                                 return_value={0, 1}), \
             unittest.mock.patch('pid_panel_mod.SimilarSymbolSearchDialog') as mock_params_dlg:
            self._mock_accepted_search_params_dialog(mock_params_dlg, final_results=[])
            with unittest.mock.patch.object(QMessageBox, 'information'):
                self.panel._on_context_action('find_similar', QPointF(5, 5), 3)
        _, kwargs = mock_params_dlg.call_args
        self.assertEqual(kwargs['ref_bbox'], (0.0, 0.0, 30.0, 25.0))

    def test_find_similar_symbol_from_template_warns_with_no_pid_open(self):
        from pid_viewer import PIDPanel
        panel = PIDPanel(self.db)
        try:
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn:
                panel._find_similar_symbol_from_template()
            mock_warn.assert_called_once()
        finally:
            panel.deleteLater()

    def test_find_similar_symbol_from_template_shows_info_with_no_saved_templates(self):
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        with unittest.mock.patch.object(QMessageBox, 'information') as mock_info, \
             unittest.mock.patch('pid_panel_mod.SymbolTemplatePickerDialog') as mock_picker:
            self.panel._find_similar_symbol_from_template()
        mock_info.assert_called_once()
        mock_picker.assert_not_called()

    def test_find_similar_symbol_from_template_does_nothing_when_picker_cancelled(self):
        from PyQt6.QtWidgets import QDialog
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        self.db.add_symbol_template("Test-mall", '{"aspect": 1.0}')
        with unittest.mock.patch('pid_panel_mod.SymbolTemplatePickerDialog') as mock_picker, \
             unittest.mock.patch('pid_panel_mod.SimilarSymbolSearchDialog') as mock_params_dlg:
            mock_picker.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.panel._find_similar_symbol_from_template()
        mock_params_dlg.assert_not_called()

    def test_find_similar_symbol_from_template_opens_search_dialog_in_template_mode(self):
        from PyQt6.QtWidgets import QDialog
        self.panel.viewer.pdf_doc = unittest.mock.MagicMock()
        self.panel.viewer._pdf_path = '/fake/path.pdf'
        self.panel.viewer.current_page = 4
        self.db.add_symbol_template("Metso-ventil", '{"aspect": 2.0}', comp_type='Ventil')
        template_row = self.db.symbol_templates()[0]
        with unittest.mock.patch('pid_panel_mod.SymbolTemplatePickerDialog') as mock_picker, \
             unittest.mock.patch('pid_panel_mod.SimilarSymbolSearchDialog') as mock_params_dlg:
            mock_picker.return_value.exec.return_value = QDialog.DialogCode.Accepted
            mock_picker.return_value.selected_template = template_row
            self._mock_accepted_search_params_dialog(mock_params_dlg, final_results=[])
            with unittest.mock.patch.object(QMessageBox, 'information'):
                self.panel._find_similar_symbol_from_template()
        args, kwargs = mock_params_dlg.call_args
        self.assertIsNone(args[0])
        self.assertIsNone(args[1])
        self.assertEqual(args[2], '/fake/path.pdf')
        self.assertEqual(args[3], 4)
        self.assertEqual(kwargs['template_name'], 'Metso-ventil')
        self.assertEqual(kwargs['template_features'], {"aspect": 2.0})


class EquipmentMarkerEditContextMenuTests(unittest.TestCase):
    """Reported feedback (2026-08-12, see NOTES.md): right-clicking an
    existing P&ID object offered no way to edit its tag/type — it fell
    through to the generic "add a new object here" menu. New "✏️ Redigera
    objekt" action, offered only when the right-click actually lands on
    an existing equipment marker."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def _make_view_with_marker(self, marker_id=1):
        from pid_viewer import PIDGraphicsView, MODE_NAV
        view = PIDGraphicsView()
        view.mode = MODE_NAV
        view.add_equipment_marker(marker_id, 10.0, 10.0, "Ventil", tag="V-1")
        item = view._type_items['equipment'][0]
        scene_pos = item.sceneBoundingRect().center()
        return view, scene_pos

    def test_context_menu_offers_edit_when_hovering_equipment_marker(self):
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QMenu
        view, scene_pos = self._make_view_with_marker()
        texts = []

        def _fake_exec(menu_self, _pos=None):
            texts.extend(a.text() for a in menu_self.actions())
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(scene_pos, QPoint(0, 0))

        self.assertTrue(any("Redigera objekt" in t for t in texts), texts)

    def test_triggering_edit_action_emits_equipment_edit_requested(self):
        from PyQt6.QtCore import QPoint
        from PyQt6.QtWidgets import QMenu
        view, scene_pos = self._make_view_with_marker(marker_id=42)
        received = []
        view.equipment_edit_requested.connect(received.append)

        def _fake_exec(menu_self, _pos=None):
            for a in menu_self.actions():
                if "Redigera objekt" in a.text():
                    a.trigger()
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(scene_pos, QPoint(0, 0))

        self.assertEqual(received, [42])

    def test_no_edit_action_when_not_hovering_a_marker(self):
        from PyQt6.QtCore import QPointF, QPoint
        from PyQt6.QtWidgets import QMenu
        view, _scene_pos = self._make_view_with_marker()
        texts = []

        def _fake_exec(menu_self, _pos=None):
            texts.extend(a.text() for a in menu_self.actions())
            return None

        with unittest.mock.patch.object(QMenu, 'exec', new=_fake_exec):
            view._show_context_menu(QPointF(-500, -500), QPoint(0, 0))

        self.assertFalse(any("Redigera objekt" in t for t in texts), texts)


class EquipmentIdentityLiveResolveTests(unittest.TestCase):
    """"Objektets identitet på P&ID, HAZOP scenario och trädet måste höra
    ihop" (2026-08-18) — equipment_markers.tag/comp_type were frozen at
    placement time; renaming/retyping the linked equipment_catalog row
    anywhere never updated the marker's own on-canvas label, even after
    reload_overlays() ran. _load_overlays() now resolves both fields LIVE
    from equipment_catalog via equipment_id, exactly like
    ScenarioTablePanel._cause_tag_display already does for the ORS
    strip."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_equipidentity_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_marker_shows_current_catalog_tag_and_type_after_rename(self):
        from pid_viewer import PIDPanel
        panel = PIDPanel(self.db)
        try:
            eq_id = self.db.add_equipment_item('V-101', 'V-101', 'V', 0, 'Ventil', '', 0)
            self.db.add_equipment_marker(eq_id, 'V-101', 0, 10.0, 10.0, 'Ventil')
            # Rename directly in equipment_catalog, as the tree/scenario/
            # Utrustningsregister edit paths all do — the marker row
            # itself is deliberately left untouched (frozen).
            self.db.update_equipment_item(eq_id, 'V-102', 'V', 'Pump', '')

            _fake_pdf_loaded(panel)
            with unittest.mock.patch.object(panel.viewer, 'add_equipment_marker') as mock_add:
                panel.reload_overlays()

            mock_add.assert_called_once()
            args = mock_add.call_args.args
            # add_equipment_marker(marker_id, x, y, comp_type, tag, confidence, ...)
            self.assertEqual(args[3], 'Pump')
            self.assertEqual(args[4], 'V-102')
        finally:
            panel.deleteLater()

    def test_unlinked_marker_still_shows_its_own_frozen_tag(self):
        """A marker never linked to a catalog row (equipment_id=None) has
        no live source to resolve from — it must keep showing its own
        tag/type exactly as before."""
        from pid_viewer import PIDPanel
        panel = PIDPanel(self.db)
        try:
            self.db.add_equipment_marker(None, 'AUTO-1', 0, 10.0, 10.0, 'Okänd')

            _fake_pdf_loaded(panel)
            with unittest.mock.patch.object(panel.viewer, 'add_equipment_marker') as mock_add:
                panel.reload_overlays()

            mock_add.assert_called_once()
            args = mock_add.call_args.args
            self.assertEqual(args[3], 'Okänd')
            self.assertEqual(args[4], 'AUTO-1')
        finally:
            panel.deleteLater()


class ObjektInRubberBandMenuTests(unittest.TestCase):
    """'När jag håller nere högerknappen och drar fram gummiband vill jag
    ... även kunna välja Objekt.' (2026-08-09, see NOTES.md) — the
    right-drag rubber-band handler (PIDPanel._on_zone_drawn) originally
    showed a menu of Objekt/Orsak/Konsekvens/Safeguard. Since the P&ID
    canvas is now object-placement-only (2026-08-13, see NOTES.md — the
    other three actions were removed), a drawn rectangle always becomes a
    new equipment object directly — no menu needed for a single choice.
    Still reuses the existing EquipmentTagPopup flow and threads the drawn
    rectangle through so the new marker gets a real outline shape instead
    of the generic bowtie-icon fallback a bare point gets."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_rbandobj_test_")
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

    def test_choosing_objekt_emits_placement_signal_with_the_drawn_rect(self):
        """No menu to choose from anymore — a drawn rectangle always
        becomes a new equipment object directly (2026-08-13, see
        NOTES.md)."""
        from PyQt6.QtCore import QRectF
        captured = []
        self.panel.equipment_placement_requested.connect(
            lambda tag, pos, page, rect: captured.append((tag, pos, page, rect)))
        pdf_rect = QRectF(5.0, 6.0, 10.0, 8.0)

        self.panel._on_zone_drawn(pdf_rect, 3)

        self.assertEqual(len(captured), 1)
        tag, pos, page, rect = captured[0]
        self.assertEqual(page, 3)
        self.assertEqual(rect, pdf_rect)

    def test_place_equipment_marker_with_rect_stores_shape_outline(self):
        import json
        from PyQt6.QtCore import QPointF, QRectF
        rect = QRectF(5.0, 6.0, 10.0, 8.0)

        self.panel.place_equipment_marker("V-500", "Ventil", QPointF(50, 50), 0, pdf_rect=rect)

        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(len(markers), 1)
        outline = json.loads(markers[0]['shape_outline'])
        self.assertEqual(len(outline), 4)
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        self.assertAlmostEqual(min(xs), rect.left())
        self.assertAlmostEqual(max(xs), rect.right())
        self.assertAlmostEqual(min(ys), rect.top())
        self.assertAlmostEqual(max(ys), rect.bottom())

    def test_place_equipment_marker_without_rect_has_blank_shape_outline(self):
        from PyQt6.QtCore import QPointF
        self.panel.place_equipment_marker("V-600", "Ventil", QPointF(20, 20), 0)
        markers = self.db.equipment_markers_for_page(0)
        self.assertEqual(markers[0]['shape_outline'], '')

    def test_plain_right_click_objekt_still_has_no_rect(self):
        """The plain right-click "🔧 Objekt" action (already shipped) must
        keep passing None for pdf_rect — a single point has no rectangle
        to give it."""
        from PyQt6.QtCore import QPointF
        captured = []
        self.panel.equipment_placement_requested.connect(
            lambda tag, pos, page, rect: captured.append(rect))
        self.panel._on_context_action('equipment', QPointF(5, 5), 0)
        self.assertEqual(captured, [None])


class EquipmentDragNavButtonResetTests(unittest.TestCase):
    """Shift-dragging an equipment marker onto the tree/scenario uses
    QDrag.exec(), a native modal drag loop — Qt suppresses the normal
    hover/leave events other widgets rely on to clear their pressed look
    during it, which could leave the "🔍 Navigera" toolbar button visually
    stuck looking pressed in after the drop even though nothing is
    actually held down (reported: nav button stays pressed after
    drag-and-dropping an equipment marker to the tree/hazop scenario)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_dragnav_test_")
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

    def test_equipment_drag_finished_releases_stuck_nav_button(self):
        from pid_viewer import MODE_NAV
        nav_btn = self.panel.mode_buttons[MODE_NAV]
        nav_btn.setDown(True)   # simulate the stuck-pressed visual left by a native QDrag

        self.panel.viewer.equipment_drag_finished.emit()

        self.assertFalse(nav_btn.isDown())

    def test_viewer_emits_equipment_drag_finished_after_shift_drag_release(self):
        """The signal must actually fire once a real Shift+drag of an
        equipment marker completes, not just work in isolation when
        emitted by hand."""
        from PyQt6.QtCore import QPointF
        received = []
        self.panel.viewer.equipment_drag_finished.connect(lambda: received.append(True))

        with unittest.mock.patch('pid_graphics_view.QDrag') as mock_drag_cls:
            mock_drag = mock_drag_cls.return_value
            start = QPointF(0, 0)
            self.panel.viewer._equip_drag_candidate = (999, start)
            event = unittest.mock.MagicMock()
            event.buttons.return_value = Qt.MouseButton.LeftButton
            event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
            event.position.return_value = QPointF(
                QApplication.startDragDistance() + 5, 0)

            self.panel.viewer.mouseMoveEvent(event)

        mock_drag.exec.assert_called_once()
        self.assertEqual(len(received), 1,
            "equipment_drag_finished must fire exactly once the drag.exec() call returns")

    def test_shift_drag_resets_scroll_hand_drag_mode_after_native_drag(self):
        """2026-08-13 follow-up report: 'musen sitter kvar i dra-läge' —
        the earlier fix above only cleared the toolbar button's stuck
        LOOK; it didn't address the actual root cause. MODE_NAV's press
        handler falls through to super().mousePressEvent() (needed so a
        Shift+click that never crosses the drag threshold still
        click-dispatches normally), which arms Qt's own ScrollHandDrag
        hand-scroll tracking. Because drag.exec() hijacks the gesture
        instead of a normal move/release pair, Qt never sees the
        matching release that would close that out — leaving the
        viewport's cursor/pan state stuck as if still mid-drag. Toggling
        dragMode off and back on must run right after drag.exec()
        returns, for every drop target (this is what "till alla celler"
        needs — the reset isn't conditional on where the drop landed)."""
        from PyQt6.QtCore import QPointF
        from pid_viewer import MODE_NAV
        self.panel.viewer.set_mode(MODE_NAV)
        # Simulate what the real mousePressEvent's fallthrough to
        # super().mousePressEvent() leaves behind: Qt's ScrollHandDrag
        # switches the cursor to a closed hand the moment the button
        # went down, before mouseMoveEvent ever gets a chance to hijack
        # the gesture into a native drag.
        self.panel.viewer.setCursor(Qt.CursorShape.ClosedHandCursor)

        with unittest.mock.patch('pid_graphics_view.QDrag'):
            start = QPointF(0, 0)
            self.panel.viewer._equip_drag_candidate = (999, start)
            event = unittest.mock.MagicMock()
            event.buttons.return_value = Qt.MouseButton.LeftButton
            event.modifiers.return_value = Qt.KeyboardModifier.ShiftModifier
            event.position.return_value = QPointF(
                QApplication.startDragDistance() + 5, 0)

            self.panel.viewer.mouseMoveEvent(event)

        self.assertEqual(self.panel.viewer.dragMode(),
                          self.panel.viewer.DragMode.ScrollHandDrag)
        self.assertEqual(self.panel.viewer.cursor().shape(), Qt.CursorShape.OpenHandCursor,
            "the cursor must be forced back to the idle open-hand look, not left as a closed hand")


# ══════════════════════════════════════════════════════════════════════════
# PIDPanel._export_pdf — four separate bugs reported together (2026-08-17,
# see NOTES.md): "Dels kan den inte hantera om P&ID har roterats. Dels blir
# texten i fel storlek. Dels får text en bakrungsfärg som inte syns annars.
# Och dels kommer inte övriga objekt jag satt ut på P&ID med, dvs objekt,
# varken dom röda eller gröna."
# ══════════════════════════════════════════════════════════════════════════

class ExportPdfMarkupTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        import fitz
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_export_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        # _export_pdf reads _working_pdf_path(), derived purely from
        # db.path — the test PDF must live at that exact name for the
        # live viewer and the exporter to agree on the same file.
        db_path = Path(self.db.path)
        self.pdf_path = str(db_path.with_name(db_path.stem + '_pid.pdf'))
        doc = fitz.open()
        doc.new_page(width=400.0, height=300.0)
        doc.save(self.pdf_path)
        doc.close()
        self.out_path = os.path.join(self._tmpdir, "out.pdf")

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

    def _export(self, panel):
        # QMessageBox.information's final "Export klar" success popup is a
        # REAL modal dialog — unmocked, it blocks forever in a headless
        # test run waiting for a click that will never come.
        with unittest.mock.patch('pid_viewer.QFileDialog.getSaveFileName',
                                  return_value=(self.out_path, '')), \
             unittest.mock.patch.object(QMessageBox, 'information'):
            panel._export_pdf()

    def test_export_applies_page_rotation_override(self):
        """"Dels kan den inte hantera om P&ID har roterats" — the export
        opened its own fresh fitz.Document and never called
        equipment_detection.apply_page_rotations(), unlike every other
        code path that opens the PDF independently of the live viewer."""
        panel = self._make_panel()
        try:
            self.db.set_page_rotation(0, 90)
            self._export(panel)
            import fitz
            out = fitz.open(self.out_path)
            self.assertEqual(out.load_page(0).rotation, 90)
            out.close()
        finally:
            panel.deleteLater()

    def test_export_node_name_font_size_matches_on_screen(self):
        """"Dels blir texten i fel storlek" — the node-name label was
        hardcoded to 8pt in the export vs. 11pt in PIDGraphicsView.
        add_node_overlay's on-screen QFont."""
        panel = self._make_panel()
        try:
            self.db.add_node_with_markup(
                "Reaktor 101", [[10, 10], [100, 10], [100, 80], [10, 80]], {}, 0)
            self._export(panel)
            import fitz
            out = fitz.open(self.out_path)
            spans = [s for b in out.load_page(0).get_text("dict")["blocks"]
                     for l in b.get("lines", []) for s in l["spans"]
                     if "Reaktor" in s["text"]]
            out.close()
            self.assertTrue(spans, "the node name must actually be drawn")
            self.assertAlmostEqual(spans[0]["size"], 11, delta=0.5)
        finally:
            panel.deleteLater()

    def test_export_text_markup_has_no_background_fill(self):
        """"Dels får text en bakrungsfärg som inte syns annars" — a plain
        'text'-type node markup (as opposed to 'comment') has no
        background on screen (_add_markup_text_item's own
        `if type_ == 'comment':` guard) but the export filled every
        text-type annotation with a tinted background regardless."""
        panel = self._make_panel()
        try:
            node_id = self.db.add_node()
            self.db.add_node_markup(
                node_id, 'text', [[20, 20]], 'Etikett', '#1565C0', 0.45, 12, 0)
            self._export(panel)
            import fitz
            out = fitz.open(self.out_path)
            annots = list(out.load_page(0).annots())
            self.assertTrue(annots, "the text markup must actually be drawn")
            # For a FreeText annotation specifically, PyMuPDF exposes the
            # PDF's own /C entry (the actual background colour for this
            # subtype) under colors['stroke'], not colors['fill'] —
            # confirmed empirically by inspecting the raw annotation
            # dict and rendering the page (a 'comment' shows its yellow
            # box; 'text' shows none). Must read .colors BEFORE closing
            # `out` — an Annot is a live handle into the document; once
            # closed it silently reports empty colors instead of raising,
            # which would make this assertion pass for the wrong reason.
            stroke = annots[0].colors.get('stroke')
            out.close()
            self.assertEqual(stroke, [],
                "a plain 'text' node-name label must have no background, matching the live view")
        finally:
            panel.deleteLater()

    def test_export_comment_markup_keeps_its_highlight_background(self):
        """The fix for the bug above must not remove 'comment's own
        intentional highlight background — only 'text' loses it."""
        panel = self._make_panel()
        try:
            node_id = self.db.add_node()
            self.db.add_node_markup(
                node_id, 'comment', [[20, 20]], 'En kommentar', '#1565C0', 0.45, 12, 0)
            self._export(panel)
            import fitz
            out = fitz.open(self.out_path)
            annots = list(out.load_page(0).annots())
            self.assertTrue(annots)
            stroke = annots[0].colors.get('stroke')   # read before closing, see note above
            out.close()
            self.assertTrue(stroke,
                "'comment' must still show its highlight background")
        finally:
            panel.deleteLater()

    def test_export_includes_equipment_markers(self):
        """"Och dels kommer inte övriga objekt jag satt ut på P&ID med,
        dvs objekt. varken dom röda eller gröna" — _export_pdf never
        looped over equipment_markers_for_page() at all; only cause/
        consequence/safeguard markers were drawn."""
        panel = self._make_panel()
        try:
            eq_id = self.db.add_equipment_item('V-101', 'V-101', 'V', 0, 'Ventil', '', 0)
            self.db.add_equipment_marker(eq_id, 'V-101', 0, 50.0, 60.0, 'Ventil')
            self._export(panel)
            import fitz
            out = fitz.open(self.out_path)
            text = out.load_page(0).get_text()
            out.close()
            self.assertIn('V-101', text)
        finally:
            panel.deleteLater()

    def _insert_cause_marker(self, cause_id, page, x, y, comp_type=''):
        """Database.add_cause_marker was removed 2026-08-13 (see NOTES.md) —
        insert directly, same as ExportPdfRotationRemapTests' identical helper."""
        self.db.conn.execute(
            "INSERT INTO cause_markers (cause_id,pid_page,x,y,component_type) "
            "VALUES (?,?,?,?,?)", (cause_id, page, x, y, comp_type))
        self.db.commit()

    def test_marker_position_follows_page_rotation(self):
        """Fas D (2026-08-17, see NOTES.md): the earlier rotation fix only
        made the PAGE attribute rotate correctly in the export — marker
        (x, y) is stored in the LIVE VIEW's already-rotated display space
        (scene_to_pdf, pure scale+offset, no rotation math), but PyMuPDF's
        drawing API always addresses the page's raw, UNROTATED content
        space regardless of /Rotate. A marker drawn with the raw stored
        x/y therefore lands on the wrong physical spot whenever the page
        is rotated — this must be un-rotated via page.derotation_matrix
        first."""
        import fitz
        panel = self._make_panel()
        try:
            self.db.set_page_rotation(0, 90)
            node_id = self.db.add_node()
            dev_id = self.db.deviations(node_id)[0]['id']
            cause_id = self.db.add_cause(dev_id)
            # The "real" equipment position in the PDF's own raw content
            # space (this is what a human would see the marker land on
            # top of, regardless of rotation).
            content_pt = fitz.Point(100, 50)
            # What the live view would actually have stored: content-space
            # transformed into the rotated DISPLAY space via the same
            # rotation the export will apply to its own output page.
            probe_doc = fitz.open(self.pdf_path)
            probe_page = probe_doc.load_page(0)
            probe_page.set_rotation(90)
            stored_pt = content_pt * probe_page.rotation_matrix
            probe_doc.close()

            self._insert_cause_marker(cause_id, 0, stored_pt.x, stored_pt.y)
            self._export(panel)

            out = fitz.open(self.out_path)
            out_page = out.load_page(0)
            self.assertEqual(out_page.rotation, 90)
            # _draw_pid_marker fills a small rounded rect anchored near
            # (x, y) in content space — find the drawn fill rect and
            # confirm its corner sits near the ORIGINAL content_pt, not
            # near the raw (unrotated) stored_pt (which would be the
            # pre-fix, wrong behavior).
            fills = [d for d in out_page.get_drawings() if d.get('fill')]
            out.close()
            self.assertTrue(fills, "the marker must actually draw a filled shape")
            corners = [pt for d in fills for item in d['items']
                       for part in item[1:] for pt in self._extract_points(part)]
            self.assertTrue(corners)
            nearest = min(corners, key=lambda p: abs(p - content_pt))
            self.assertLess(abs(nearest - content_pt), 20,
                "marker must land near the equipment's actual content-space "
                "position after un-rotating, not near the raw stored (display-space) x/y")
            # Sanity: the raw un-transformed stored point is nowhere near this
            # (proves the test would have failed before the fix).
            self.assertGreater(abs(stored_pt - content_pt), 20)
        finally:
            panel.deleteLater()

    @staticmethod
    def _extract_points(obj):
        """PyMuPDF's get_drawings() 'items' can hold Point, Quad, or Rect
        objects depending on how the shape was drawn/simplified — pull out
        plain Points from whichever it is."""
        import fitz
        if hasattr(obj, 'x') and hasattr(obj, 'y') and not hasattr(obj, 'x0'):
            return [obj]
        if hasattr(obj, 'ul'):   # Quad
            return [obj.ul, obj.ur, obj.ll, obj.lr]
        if hasattr(obj, 'x0'):   # Rect
            return [fitz.Point(obj.x0, obj.y0), fitz.Point(obj.x1, obj.y1)]
        return []

    def test_node_markup_polygon_follows_page_rotation(self):
        """Same rotated-display-space storage bug as marker x/y above,
        for a node's own markup polygon points (nodes.markup_points)."""
        import fitz
        panel = self._make_panel()
        try:
            self.db.set_page_rotation(0, 90)
            content_pts = [fitz.Point(50, 50), fitz.Point(150, 50),
                           fitz.Point(150, 120), fitz.Point(50, 120)]
            probe_doc = fitz.open(self.pdf_path)
            probe_page = probe_doc.load_page(0)
            probe_page.set_rotation(90)
            stored_pts = [p * probe_page.rotation_matrix for p in content_pts]
            probe_doc.close()

            self.db.add_node_with_markup(
                "Nod A", [[p.x, p.y] for p in stored_pts], {'color': '#ff8c00'}, 0)
            self._export(panel)

            out = fitz.open(self.out_path)
            out_page = out.load_page(0)
            drawings = out_page.get_drawings()
            out.close()
            all_pts = [pt for d in drawings for item in d['items']
                       for part in item[1:] for pt in self._extract_points(part)]
            self.assertTrue(all_pts, "the node polygon must actually be drawn")
            # Every one of the four real corners must be reproduced
            # (within a small tolerance) among the drawn points — proves
            # the whole polygon landed back at its real content-space
            # position, not just "somewhere in the right neighborhood".
            for corner in content_pts:
                nearest = min(all_pts, key=lambda p: abs(p - corner))
                self.assertLess(abs(nearest - corner), 2,
                    f"corner {corner} must be reproduced after un-rotating, "
                    f"nearest drawn point was {nearest}")
        finally:
            panel.deleteLater()


if __name__ == '__main__':
    unittest.main(verbosity=2)


if __name__ == "__main__":
    unittest.main()
