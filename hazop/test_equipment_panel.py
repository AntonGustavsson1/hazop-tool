#!/usr/bin/env python3
"""Split out of test_regression.py 2026-08-20 (see NOTES.md
"Dela upp test_regression.py i per-modul testfiler") — tests
primarily covering equipment_panel.py, plus any cross-module glue they
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
_HAZOP_DIR = Path(__file__).resolve().parent
if str(_HAZOP_DIR) not in sys.path:
    sys.path.insert(0, str(_HAZOP_DIR))

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

# ══════════════════════════════════════════════════════════════════════════
# "Hitta ventiler" -> "Hitta objekt" (2026-08-10, see NOTES.md): widened
# from VALVE_COMPONENT_TYPES-only to every equipment type. The shape side
# (detect_equipment_and_valves) has hunted pump/instrument-shaped symbols
# since 2026-08-07 regardless of this filter; a known pump/instrument tag
# just never got to PARTICIPATE in the weighted tag<->symbol association.
# ══════════════════════════════════════════════════════════════════════════

class AutodetectAllEquipmentTypesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_autodetectscope_test_")
        self.db_path = os.path.join(self._tmpdir, "test_project.db")
        self.db = Database(path=self.db_path)

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_autodetect_proceeds_when_only_valve_rows_present(self):
        """No PDF path is configured in this test, so _autodetect() must
        reach its 'Ingen P&ID' warning (not the 'Inga taggar i
        registret' info message) -- proof the valve rows were NOT
        filtered out before the tag_points-empty check."""
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox
        self.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
        self.db.add_equipment_item("PSV-201", "PSV-201", "PSV", 0,
                                   "Säkerhetsventil (PSV)", '', 0)
        panel = EquipmentPanel(self.db)
        panel.refresh()
        try:
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn, \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                panel._autodetect()
            mock_warn.assert_called_once()
            mock_info.assert_not_called()
        finally:
            panel.deleteLater()

    def test_autodetect_includes_non_valve_rows(self):
        """Instrument/pump/unclassified rows must now be included in
        tag_points too (2026-08-10) — proceeds straight to the 'Ingen
        P&ID' warning rather than the 'Inga taggar i registret' info
        message, proof they were NOT filtered out."""
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox
        self.db.add_equipment_item("TI-301", "TI-301", "TI", 0, "Instrument / Sensor", '', 0)
        self.db.add_equipment_item("P-401", "P-401", "P", 0, "Pump", '', 0)
        self.db.add_equipment_item("X-501", "X-501", "X", 0, "", '', 0)   # unclassified
        panel = EquipmentPanel(self.db)
        panel.refresh()
        try:
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn, \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                panel._autodetect()
            mock_warn.assert_called_once()
            mock_info.assert_not_called()
        finally:
            panel.deleteLater()

    def test_autodetect_mixed_register_uses_every_type(self):
        """A register with both valve and non-valve rows proceeds past
        the empty-check regardless of the mix — the type filter is gone
        entirely, not just tolerant of a mix."""
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox
        self.db.add_equipment_item("HV-101", "HV-101", "HV", 0, "Ventil", '', 0)
        self.db.add_equipment_item("TI-301", "TI-301", "TI", 0, "Instrument / Sensor", '', 0)
        panel = EquipmentPanel(self.db)
        panel.refresh()
        try:
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn, \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                panel._autodetect()
            mock_warn.assert_called_once()
            mock_info.assert_not_called()
        finally:
            panel.deleteLater()

    def test_autodetect_shows_generic_empty_message_when_no_tags_at_all(self):
        """With zero rows in the register at all, the empty-state message
        must be the new generic wording, not the old valve-specific one."""
        from hazop import EquipmentPanel
        from PyQt6.QtWidgets import QMessageBox
        panel = EquipmentPanel(self.db)
        panel.refresh()
        try:
            with unittest.mock.patch.object(QMessageBox, 'warning') as mock_warn, \
                 unittest.mock.patch.object(QMessageBox, 'information') as mock_info:
                panel._autodetect()
            mock_info.assert_called_once()
            mock_warn.assert_not_called()
            title = mock_info.call_args[0][1]
            self.assertIn('taggar', title.lower())
        finally:
            panel.deleteLater()


class ObjectPickerPopupTests(unittest.TestCase):
    """New (2026-08-12, see NOTES.md): lets the user pick an already-
    registered P&ID object to auto-tag a new cause/consequence/safeguard,
    instead of only being able to drag-and-drop from the P&ID."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_objpicker_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _add_two_equipment(self):
        id1 = self.db.add_equipment_item("PV-101", "PV-101", "PV", 0, "Ventil", "Tryckventil", 0)
        id2 = self.db.add_equipment_item("TT-201", "TT-201", "TT", 0, "Givare", "Temperaturgivare", 0)
        return id1, id2

    def test_lists_all_registered_equipment_regardless_of_marker_state(self):
        from hazop import ObjectPickerPopup
        self._add_two_equipment()
        popup = ObjectPickerPopup(self.db)
        try:
            self.assertEqual(popup._list.count(), 2)
        finally:
            popup.deleteLater()

    def test_search_filters_by_tag_type_or_description(self):
        from hazop import ObjectPickerPopup
        self._add_two_equipment()
        popup = ObjectPickerPopup(self.db)
        try:
            popup._search.setText("temperatur")
            self.assertEqual(popup._list.count(), 1)
            self.assertIn("TT-201", popup._list.item(0).text())

            popup._search.setText("")
            self.assertEqual(popup._list.count(), 2)
        finally:
            popup.deleteLater()

    def test_pick_button_disabled_until_selection_made(self):
        from hazop import ObjectPickerPopup
        self._add_two_equipment()
        popup = ObjectPickerPopup(self.db)
        try:
            self.assertFalse(popup._pick_btn.isEnabled())
            popup._list.setCurrentRow(0)
            self.assertTrue(popup._pick_btn.isEnabled())
        finally:
            popup.deleteLater()

    def test_accept_selected_sets_selected_and_accepts(self):
        from hazop import ObjectPickerPopup
        from PyQt6.QtWidgets import QDialog
        self._add_two_equipment()
        popup = ObjectPickerPopup(self.db)
        try:
            popup._list.setCurrentRow(0)
            popup._accept_selected()
            self.assertEqual(popup.result(), QDialog.DialogCode.Accepted)
            self.assertEqual(popup.selected['tag'], "PV-101")
        finally:
            popup.deleteLater()

    def test_accept_skip_accepts_with_none_selected(self):
        from hazop import ObjectPickerPopup
        from PyQt6.QtWidgets import QDialog
        self._add_two_equipment()
        popup = ObjectPickerPopup(self.db)
        try:
            popup._accept_skip()
            self.assertEqual(popup.result(), QDialog.DialogCode.Accepted)
            self.assertIsNone(popup.selected)
        finally:
            popup.deleteLater()


class EquipmentPanelManualAddUnificationTests(unittest.TestCase):
    """EquipmentPanel._add_manual used to open a bare QInputDialog.getText()
    (tag only, type always auto-guessed from KNOWN_PREFIXES, no duplicate
    check) instead of the richer EquipmentTagPopup already used by the
    P&ID's "🔧 Objekt" action. Unified onto the same popup + committed
    handler (2026-08-09, see NOTES.md)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_manualadd_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))
        from hazop import EquipmentPanel
        self.panel = EquipmentPanel(self.db)

    def tearDown(self):
        self.panel.deleteLater()
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_commit_creates_new_catalog_row_with_chosen_type(self):
        self.panel._on_manual_equipment_committed("P-101", "Pump")
        row = self.db.get_equipment_by_tag("P-101")
        self.assertIsNotNone(row)
        self.assertEqual(row['equipment_type'], "Pump")

    def test_commit_of_existing_tag_reuses_row_instead_of_duplicating(self):
        self.db.add_equipment_item("V-01", "V-01", "V", 0, "Ventil", '', 0)
        self.panel._on_manual_equipment_committed("V-01", "Ventil")
        matches = [r for r in self.db.equipment_items() if r['tag'] == 'V-01']
        self.assertEqual(len(matches), 1,
                          "committing an already-catalogued tag must not create a duplicate row")

    def test_commit_of_existing_tag_updates_type_when_changed(self):
        self.db.add_equipment_item("V-02", "V-02", "V", 0, "", '', 0)
        self.panel._on_manual_equipment_committed("V-02", "Ventil")
        row = self.db.get_equipment_by_tag("V-02")
        self.assertEqual(row['equipment_type'], "Ventil")

    def test_commit_with_blank_tag_is_a_noop(self):
        before = len(self.db.equipment_items())
        self.panel._on_manual_equipment_committed("", "Pump")
        self.assertEqual(len(self.db.equipment_items()), before)


class EquipmentTagPopupDuplicateHintTests(unittest.TestCase):
    """EquipmentTagPopup surfaces when a typed tag already exists in the
    catalog, since place_equipment_marker silently reuses that row rather
    than creating a duplicate (2026-08-10, see NOTES.md)."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_dupcheck_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_hint_shown_for_existing_tag(self):
        from hazop import EquipmentTagPopup
        self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        popup = EquipmentTagPopup(self.db)
        try:
            popup._tag_edit.setText("P-101")
            self.assertIn("P-101", popup._dup_hint.text())
            self.assertIn("finns redan", popup._dup_hint.text())
        finally:
            popup.deleteLater()

    def test_no_hint_for_new_tag(self):
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            popup._tag_edit.setText("V-999")
            self.assertEqual(popup._dup_hint.text(), "")
        finally:
            popup.deleteLater()

    def test_hint_clears_when_tag_edited_to_no_longer_match(self):
        from hazop import EquipmentTagPopup
        self.db.add_equipment_item("P-101", "P-101", "P", 0, "Pump", '', 0)
        popup = EquipmentTagPopup(self.db)
        try:
            popup._tag_edit.setText("P-101")
            self.assertNotEqual(popup._dup_hint.text(), "")
            popup._tag_edit.setText("P-999")
            self.assertEqual(popup._dup_hint.text(), "")
        finally:
            popup.deleteLater()


class EquipmentTagPopupCustomTypeTests(unittest.TestCase):
    """"det är här jag vill kunna lägga till nya typer av objekt som inte
    redan finns i listan" (2026-08-13). First attempt made the "Typ"
    combo editable directly — reverted the same day ("Rullgardinen ...
    har försvunnit. Det ska vara de valen som det var innan") because an
    editable QComboBox loses its usual dropdown-arrow affordance under
    this app's global stylesheet. The combo stays non-editable with its
    original pick-from-list behaviour; a dedicated "+" button next to it
    opens a text prompt to add a brand-new type instead."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="hazop_customtype_test_")
        self.db = Database(path=os.path.join(self._tmpdir, "test_project.db"))

    def tearDown(self):
        try:
            del self.db
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_type_combo_is_not_editable(self):
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            self.assertFalse(popup._type_cb.isEditable())
        finally:
            popup.deleteLater()

    def test_plus_button_adds_and_selects_a_new_type(self):
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            with unittest.mock.patch.object(
                    hazop.QInputDialog, 'getText', return_value=("Ett helt nytt objekt", True)):
                popup._add_new_type()
            self.assertEqual(popup._type_cb.currentText(), "Ett helt nytt objekt")
        finally:
            popup.deleteLater()

    def test_plus_button_also_registers_it_as_a_standard_object(self):
        """"lägger jag till ytterligare något här skall det också dyka
        upp i standardobjekt. Dessa skall prata med varandra."
        (2026-08-13) — a brand-new type typed via the "+" button must
        immediately become a Standardobjekt too, not just a local combo
        entry, so it's available in the cause-suggestion forms."""
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            with unittest.mock.patch.object(
                    hazop.QInputDialog, 'getText', return_value=("Ett helt nytt objekt", True)):
                popup._add_new_type()
            names = [o['name'] for o in self.db.standard_objects()]
            self.assertIn("Ett helt nytt objekt", names)
        finally:
            popup.deleteLater()

    def test_plus_button_does_not_duplicate_an_existing_standard_object(self):
        """Case-insensitive match against an existing Standardobjekt
        (e.g. one added via Inställningar) must not create a near-
        duplicate entry that only differs by case."""
        from hazop import EquipmentTagPopup
        self.db.add_standard_object("Ventil")
        popup = EquipmentTagPopup(self.db)
        try:
            with unittest.mock.patch.object(
                    hazop.QInputDialog, 'getText', return_value=("ventil", True)):
                popup._add_new_type()
            names = [o['name'] for o in self.db.standard_objects()]
            self.assertEqual(names.count("Ventil") + names.count("ventil"), 1)
        finally:
            popup.deleteLater()

    def test_standard_object_added_elsewhere_appears_in_type_dropdown(self):
        """The reverse direction of the same sync: a Standardobjekt added
        via Inställningar (not through this popup at all) must show up
        as a selectable type here too."""
        from hazop import EquipmentTagPopup
        self.db.add_standard_object("Ett objekt satt via Inställningar")
        popup = EquipmentTagPopup(self.db)
        try:
            self.assertGreaterEqual(
                popup._type_cb.findText("Ett objekt satt via Inställningar"), 0)
        finally:
            popup.deleteLater()

    def test_plus_button_cancelled_leaves_selection_unchanged(self):
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            before = popup._type_cb.currentText()
            with unittest.mock.patch.object(
                    hazop.QInputDialog, 'getText', return_value=("", False)):
                popup._add_new_type()
            self.assertEqual(popup._type_cb.currentText(), before)
        finally:
            popup.deleteLater()

    def test_new_type_from_plus_button_commits_unchanged(self):
        from hazop import EquipmentTagPopup
        popup = EquipmentTagPopup(self.db)
        try:
            popup._tag_edit.setText("XYZ-1")
            with unittest.mock.patch.object(
                    hazop.QInputDialog, 'getText', return_value=("Ett helt nytt objekt", True)):
                popup._add_new_type()
            captured = []
            popup.committed.connect(lambda tag, typ: captured.append((tag, typ)))
            popup._ok()
            self.assertEqual(captured, [("XYZ-1", "Ett helt nytt objekt")])
        finally:
            popup.deleteLater()

    def test_previously_used_custom_type_is_offered_again(self):
        """Once a custom type has been used anywhere in the catalog, it
        should be a selectable dropdown entry next time, not something
        the user has to retype from scratch every time."""
        from hazop import EquipmentTagPopup
        self.db.add_equipment_item("XYZ-1", "XYZ-1", "XYZ", 0, "Ett helt nytt objekt", '', 0)
        popup = EquipmentTagPopup(self.db)
        try:
            self.assertGreaterEqual(popup._type_cb.findText("Ett helt nytt objekt"), 0)
        finally:
            popup.deleteLater()





if __name__ == "__main__":
    unittest.main()
