"""Risk-matrix TEL placement and the shared risk-profile save action."""

import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtCore import QMimeData, QPoint, QPointF, Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox
from PyQt6.QtTest import QTest

from database import DEFAULT_MATRIX, Database, _normalise_matrix
from hazop_preparation_panel import (
    HAZOPPreparationPanel, ST1_RISK_MATRIX_PRESET, _TEL_MARKER_MIME,
    _MATRIX_CELL_MIME, _PALETTE_MIME,
    _tel_internal_edges, _tel_matrix_boundary,
)


class TelMatrixInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='hazop_tel_matrix_')
        self.db = Database(path=os.path.join(self.directory, 'project.db'))
        self.db.set_risk_matrix(copy.deepcopy(DEFAULT_MATRIX))
        self.panel = HAZOPPreparationPanel(self.db)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.db.conn.close()
        del self.db
        shutil.rmtree(self.directory, ignore_errors=True)

    def _tel_item(self, key, level):
        table = self.panel._lopa_tel_table
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if (item.data(Qt.ItemDataRole.UserRole) == key and
                    item.data(Qt.ItemDataRole.UserRole + 1) == level):
                return table.item(row, 3)
        self.fail(f'TEL editor missing {key}, {level}')

    def _button(self, consequence, frequency):
        return next(button for _row, buttons in self.panel._cell_buttons
                    for button in buttons
                    if button.row == consequence and button.col == frequency)

    def test_boundary_mapping_respects_both_axes_and_reversal(self):
        self.assertEqual(_tel_internal_edges(2, 5, True),
                         {'left': 1, 'right': 2})
        self.assertEqual(_tel_internal_edges(2, 5, True, x_reversed=True),
                         {'right': 1, 'left': 2})
        self.assertEqual(_tel_internal_edges(2, 5, False, y_reversed=True),
                         {'top': 1, 'bottom': 2})
        self.assertEqual(_tel_internal_edges(2, 5, False),
                         {'bottom': 1, 'top': 2})
        self.assertEqual(_tel_internal_edges(0, 5, True), {'right': 0})
        self.assertEqual(_tel_internal_edges(4, 5, True), {'left': 3})
        self.assertEqual(_tel_internal_edges(0, 5, False, include_outer=True),
                         {'bottom': -1, 'top': 0})
        self.assertEqual(_tel_internal_edges(4, 5, False, include_outer=True),
                         {'bottom': 3, 'top': 4})
        self.assertEqual(_tel_internal_edges(0, 5, False, y_reversed=True,
                                             include_outer=True),
                         {'top': -1, 'bottom': 0})

    def test_displayed_tel_edge_follows_axis_orientation(self):
        panel = self.panel
        key = panel._tel_category_combo.currentData()
        for axis, x_reversed, y_reversed, edge in (
                ('frequency', False, False, 'left'),
                ('frequency', True, False, 'right'),
                ('consequence', False, True, 'top'),
                ('consequence', False, False, 'bottom')):
            with self.subTest(axis=axis, x_reversed=x_reversed,
                              y_reversed=y_reversed):
                panel._axis_combo.setCurrentIndex(panel._axis_combo.findData(axis))
                panel._x_rev_chk.setChecked(x_reversed)
                panel._y_rev_chk.setChecked(y_reversed)
                panel._load_lopa_settings()
                boundary = panel._working_frequency_boundaries(panel._last_built_cfg)[2]
                self._tel_item(key, 2).setText(repr(boundary))
                self.assertEqual(self._button(2, 3)._tel_edge, edge)

    def test_exact_and_schematic_tel_are_distinguished(self):
        bounds = [1e-5, 1e-4, 1e-3]
        self.assertEqual(_tel_matrix_boundary(1e-4, bounds), (1, 'exact'))
        self.assertEqual(_tel_matrix_boundary(2e-4, bounds), (1, 'schematic'))
        self.assertEqual(_tel_matrix_boundary(1e-6, bounds), (-1, 'below'))
        self.assertEqual(_tel_matrix_boundary(1e-2, bounds), (3, 'above'))

    def test_drag_drop_snaps_tel_to_real_boundary_then_one_save_persists_it(self):
        panel = self.panel
        key = panel._tel_category_combo.currentData()
        panel._load_lopa_settings()
        self._tel_item(key, 2).setText('2e-5')
        saved_before = self.db.get_risk_matrix()
        source = self._button(2, 1)
        self.assertEqual(source._tel_edge, 'left')
        target = self._button(2, 3)
        target.resize(92, 40)
        mime = QMimeData()
        mime.setData(_TEL_MARKER_MIME, json.dumps({
            'category': key, 'consequence': 2,
        }).encode())
        enter = QDragEnterEvent(QPoint(2, 20), Qt.DropAction.MoveAction, mime,
                                Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
        target.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        self.assertEqual(target._tel_preview_edge, 'left')
        drop = QDropEvent(QPointF(2, 20), Qt.DropAction.MoveAction, mime,
                          Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        target.dropEvent(drop)
        self.assertTrue(drop.isAccepted())
        expected = panel._working_frequency_boundaries(panel._last_built_cfg)[2]
        self.assertEqual(float(self._tel_item(key, 2).text()), expected)
        self.assertIn('Osparad TEL-ändring', panel._tel_summary.text())
        self.assertEqual(self.db.get_risk_matrix(), saved_before)
        with patch.object(QMessageBox, 'information'):
            self.assertTrue(panel._save_current_risk_profile())
        self.assertEqual(self.db.get_risk_matrix()['lopa']['category_settings'][key]['tel'][2],
                         expected)
        self.assertNotIn('Osparad TEL-ändring', panel._tel_summary.text())

    def test_tel_can_be_dropped_at_bottom_of_vertical_frequency_axis(self):
        panel = self.panel
        key = panel._tel_category_combo.currentData()
        panel._axis_combo.setCurrentIndex(panel._axis_combo.findData('consequence'))
        panel._load_lopa_settings()
        self._tel_item(key, 2).setText('0.001')
        target = self._button(2, 0)
        target.resize(92, 40)
        self.assertEqual(target._tel_target_edges['bottom'], -1)
        mime = QMimeData()
        mime.setData(_TEL_MARKER_MIME, json.dumps({
            'category': key, 'consequence': 2,
        }).encode())
        enter = QDragEnterEvent(QPoint(46, 38), Qt.DropAction.MoveAction, mime,
                                Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
        target.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        self.assertEqual(target._tel_preview_edge, 'bottom')
        drop = QDropEvent(QPointF(46, 38), Qt.DropAction.MoveAction, mime,
                          Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        with patch('hazop_preparation_panel.QInputDialog.getText',
                   return_value=('1e-7', True)) as prompt:
            target.dropEvent(drop)
        self.assertTrue(drop.isAccepted())
        self.assertTrue(prompt.called)
        self.assertEqual(float(self._tel_item(key, 2).text()), 1e-7)
        self.assertEqual(target._tel_edge, 'bottom')
        with patch.object(QMessageBox, 'information'):
            self.assertTrue(panel._save_current_risk_profile())
        self.assertEqual(self.db.get_risk_matrix()['lopa']['category_settings'][key]['tel'][2],
                         1e-7)

    def test_outer_tel_drop_requires_an_exact_value_in_the_open_class(self):
        panel = self.panel
        key = panel._tel_category_combo.currentData()
        panel._load_lopa_settings()
        original = self._tel_item(key, 2).text()
        with patch('hazop_preparation_panel.QInputDialog.getText',
                   side_effect=[('0.1', True), ('1e-7', True)]) as prompt, \
             patch.object(QMessageBox, 'warning') as warning:
            panel._move_tel_to_boundary(key, 2, -1)
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(warning.call_count, 1)
        self.assertEqual(float(self._tel_item(key, 2).text()), 1e-7)
        with patch('hazop_preparation_panel.QInputDialog.getText',
                   return_value=('', False)):
            panel._move_tel_to_boundary(key, 2, len(panel._working_frequency_boundaries(
                panel._last_built_cfg)))
        self.assertEqual(float(self._tel_item(key, 2).text()), 1e-7)
        self.assertNotEqual(original, self._tel_item(key, 2).text())

    def test_marker_handle_arms_tel_drag_without_opening_cell_editor(self):
        panel = self.panel
        source = next(button for _row, buttons in panel._cell_buttons
                      for button in buttons if button._tel_edge)
        source.resize(92, 40)
        point = QPoint(13, 20) if source._tel_edge == 'left' else QPoint(79, 20)
        source.clicked.disconnect()
        clicks = []
        source.clicked.connect(lambda: clicks.append(True))
        QTest.mousePress(source, Qt.MouseButton.LeftButton, pos=point)
        self.assertTrue(source._tel_drag_armed)
        QTest.mouseRelease(source, Qt.MouseButton.LeftButton, pos=point)
        self.assertEqual(clicks, [])

    def test_existing_colour_and_cell_copy_drops_still_work(self):
        target = self._button(2, 3)
        target.resize(92, 40)
        for mime_type, payload, expected in (
                (_PALETTE_MIME,
                 {'color': '#123456', 'name': 'Egen', 'fg_color': '#ffffff'},
                 ('#123456', 'Egen', '#ffffff')),
                (_MATRIX_CELL_MIME,
                 {'color': '#abcdef', 'label': '', 'fg_color': '#000000'},
                 ('#abcdef', '', '#000000'))):
            mime = QMimeData()
            mime.setData(mime_type, json.dumps(payload).encode())
            enter = QDragEnterEvent(QPoint(40, 20), Qt.DropAction.CopyAction,
                                    mime, Qt.MouseButton.LeftButton,
                                    Qt.KeyboardModifier.NoModifier)
            target.dragEnterEvent(enter)
            self.assertTrue(enter.isAccepted())
            drop = QDropEvent(QPointF(40, 20), Qt.DropAction.CopyAction, mime,
                              Qt.MouseButton.LeftButton,
                              Qt.KeyboardModifier.NoModifier)
            target.dropEvent(drop)
            self.assertTrue(drop.isAccepted())
            self.assertEqual((target.color(), target.label(), target.fg_color()),
                             expected)

    def test_tel_drag_rejects_other_consequence_or_category(self):
        panel = self.panel
        key = panel._tel_category_combo.currentData()
        target = self._button(2, 3)
        for payload in ({'category': key, 'consequence': 1},
                        {'category': 'another', 'consequence': 2}):
            mime = QMimeData()
            mime.setData(_TEL_MARKER_MIME, json.dumps(payload).encode())
            enter = QDragEnterEvent(QPoint(2, 20), Qt.DropAction.MoveAction,
                                    mime, Qt.MouseButton.LeftButton,
                                    Qt.KeyboardModifier.NoModifier)
            target.dragEnterEvent(enter)
            self.assertFalse(enter.isAccepted())
        self.assertIsNone(target._tel_preview_edge)

    def test_axis_edit_is_visible_before_tel_drop_and_saved_with_it(self):
        panel = self.panel
        key = panel._tel_category_combo.currentData()
        before = self.db.get_risk_matrix()
        panel._set_risk_subview(1)
        panel._frequency_axis_table.item(2, 2).setText('0.002')
        self.assertTrue(panel._axes_dirty)
        panel._set_risk_subview(0)
        self.assertEqual(panel._working_frequency_boundaries(panel._last_built_cfg)[2],
                         0.002)
        self.assertEqual(float(panel._freq_boundary_edits[2].text()), 0.002)
        panel._move_tel_to_boundary(key, 2, 2)
        self.assertEqual(float(self._tel_item(key, 2).text()), 0.002)
        self.assertEqual(self.db.get_risk_matrix(), before)
        with patch.object(QMessageBox, 'information'):
            self.assertTrue(panel._save_current_risk_profile())
        saved = self.db.get_risk_matrix()
        self.assertEqual(saved['freq_boundaries'][2], 0.002)
        self.assertEqual(saved['lopa']['category_settings'][key]['tel'][2], 0.002)

    def test_invalid_axis_boundary_disables_tel_drop_without_losing_input(self):
        panel = self.panel
        key = panel._tel_category_combo.currentData()
        panel._set_risk_subview(1)
        panel._frequency_axis_table.item(2, 2).setText('oklar')
        panel._set_risk_subview(0)
        self.assertTrue(panel._axes_boundary_invalid)
        self.assertIn('Rätta frekvensgränserna', panel._tel_summary.text())
        self.assertEqual(self._button(2, 3)._tel_target_edges, {})
        panel._load_lopa_settings()
        original = self._tel_item(key, 2).text()
        panel._move_tel_to_boundary(key, 2, 2)
        self.assertEqual(self._tel_item(key, 2).text(), original)
        panel._set_risk_subview(1)
        self.assertEqual(panel._frequency_axis_table.item(2, 2).text(), 'oklar')

    def test_reversing_axis_editor_keeps_unsaved_descriptions_and_boundaries(self):
        panel = self.panel
        panel._set_risk_subview(1)
        panel._frequency_axis_table.item(2, 2).setText('0.002')
        panel._category_definition_table.item(1, 1).setText('Ny beskrivning')
        rows = panel._frequency_axis_table.rowCount()
        panel._set_axes_frequency_order(True)
        self.assertEqual(panel._frequency_axis_table.item(rows - 3, 2).text(),
                         '0.002')
        self.assertEqual(panel._category_definition_table.item(1, 1).text(),
                         'Ny beskrivning')
        panel._set_axes_frequency_order(False)
        self.assertEqual(panel._frequency_axis_table.item(2, 2).text(), '0.002')

    def test_invalid_tel_prevents_all_risk_changes_from_being_saved(self):
        panel = self.panel
        key = panel._tel_category_combo.currentData()
        panel._load_lopa_settings()
        before = self.db.get_risk_matrix()
        self._button(2, 3).set_cell('#ffffff', 'Ändrat', '#000000')
        self._tel_item(key, 2).setText('-1')
        with patch.object(QMessageBox, 'warning'):
            self.assertFalse(panel._save_current_risk_profile(False))
        self.assertEqual(self.db.get_risk_matrix(), before)

    def test_grid_boundary_edit_survives_rebuild_and_shared_save(self):
        panel = self.panel
        panel._freq_boundary_edits[2].setText('0.002')
        self.assertTrue(panel._grid_boundaries_dirty)
        with patch.object(QMessageBox, 'information'):
            self.assertTrue(panel._save_current_risk_profile())
        self.assertEqual(self.db.get_risk_matrix()['freq_boundaries'][2], 0.002)

    def test_invalid_grid_boundary_is_kept_for_correction(self):
        panel = self.panel
        before = self.db.get_risk_matrix()
        panel._freq_boundary_edits[2].setText('fel')
        with patch.object(QMessageBox, 'warning'):
            self.assertFalse(panel._save_current_risk_profile(False))
        self.assertEqual(self.db.get_risk_matrix(), before)
        self.assertEqual(panel._freq_boundary_edits[2].text(), 'fel')
        panel._set_risk_subview(1)
        self.assertEqual(panel._frequency_axis_table.item(2, 2).text(), 'fel')

    def test_template_preview_cancel_and_tel_save_keep_template_values(self):
        panel = self.panel
        before = self.db.get_risk_matrix()
        candidate = copy.deepcopy(ST1_RISK_MATRIX_PRESET)
        candidate['lopa'] = copy.deepcopy(_normalise_matrix(copy.deepcopy(before))['lopa'])
        key = candidate['consequence_categories'][0]['key']
        candidate['lopa']['category_settings'][key]['tel'][2] = 3e-5
        self.assertFalse(panel._has_unsaved_risk_profile(),
                         (panel._axes_dirty, panel._matrix_levels_dirty,
                          panel._lopa_dirty, getattr(panel, '_pending_template_categories', None),
                          panel._working_matrix_matches_saved()))
        with patch.object(QMessageBox, 'warning') as warning:
            panel._request_matrix_template(candidate, 'ST1 med TEL')
        self.assertFalse(warning.called)
        self.assertEqual(self.db.get_risk_matrix(), before)
        self.assertEqual(float(self._tel_item(key, 2).text()), 3e-5)
        panel._discard_pending_template()
        self.assertEqual(self.db.get_risk_matrix(), before)
        self.assertIsNone(panel._pending_template_categories)
        with patch.object(QMessageBox, 'warning') as warning:
            panel._request_matrix_template(candidate, 'ST1 med TEL')
        self.assertFalse(warning.called)
        with patch.object(QMessageBox, 'information'):
            self.assertTrue(panel._save_current_risk_profile())
        saved = self.db.get_risk_matrix()
        self.assertEqual(saved['rows'], 6)
        self.assertEqual(saved['lopa']['category_settings'][key]['tel'][2], 3e-5)
        self.assertIn('tekniska riktvärden', panel._tel_summary.text())

    def test_assessed_project_reviews_template_before_any_database_change(self):
        node = self.db.add_node()
        deviation = self.db.deviations(node)[0]['id']
        cause = self.db.add_cause(deviation)
        self.db.update_cause(cause, description='Pumpfel', likelihood=1,
                             frequency_override=1, frequency_cleared=False)
        panel = self.panel
        before = self.db.get_risk_matrix()
        with patch.object(QMessageBox, 'warning'):
            panel._request_matrix_template(ST1_RISK_MATRIX_PRESET, 'ST1')
        self.assertIsNotNone(panel._pending_template_categories)
        self.assertEqual(self.db.get_risk_matrix(), before)

        shown = []

        class CancelledMigration:
            def __init__(self, db, source, target, name, parent=None):
                shown.append((source['rows'], target['rows'], name))

            def exec(self):
                return QDialog.DialogCode.Rejected

        with patch('hazop_preparation_panel.RiskMatrixMigrationDialog',
                   CancelledMigration):
            self.assertFalse(panel._save_current_risk_profile(False))
        self.assertEqual(shown, [(5, 6, 'ST1')])
        self.assertEqual(self.db.get_risk_matrix(), before)
        self.assertIsNotNone(panel._pending_template_categories)

    def test_acceptance_only_edit_uses_shared_save(self):
        panel = self.panel
        item = panel._risk_level_table.item(0, 2)
        item.setText('Ny beslutsregel')
        self.assertTrue(panel._matrix_levels_dirty)
        with patch.object(QMessageBox, 'information'):
            self.assertTrue(panel._save_current_risk_profile())
        self.assertEqual(self.db.get_risk_matrix()['risk_level_definitions'][0]['definition'],
                         'Ny beslutsregel')


if __name__ == '__main__':
    unittest.main()
