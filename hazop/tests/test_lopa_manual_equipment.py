"""Objects without a P&ID can be created and linked from the LOPA bridge."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication, QDialog
from database import Database
from lopa_panel import LopaLinkDialog, LopaEquipmentDialog


class LopaManualEquipmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(path=os.path.join(self.temp.name, 'test.db'))
        node = self.db.add_node()
        cause = self.db.add_cause(self.db.deviations(node)[0]['id'])
        self.db.update_cause(cause, description='Cause', base_frequency=0.1, frequency_cleared=False)
        self.consequence = self.db.add_consequence(cause)
        self.db.set_consequence_severity(self.consequence, self.db.consequence_categories()[0]['id'], 3)
        self.safeguard = self.db.add_safeguard(self.consequence)

    def tearDown(self):
        self.db.conn.close()
        self.temp.cleanup()

    def create_from_bridge(self, bridge):
        def create(dialog):
            dialog._tag.setText('OFF-PID-101')
            dialog._type.setText('Level switch')
            dialog._description.setText('Object without drawing location')
            dialog._create_object()
            return dialog.result()
        with patch.object(LopaEquipmentDialog, 'exec', create):
            bridge._new_equipment.click()
        return bridge._equipment.currentData()

    def test_create_link_and_reuse_without_a_drawing(self):
        bridge = LopaLinkDialog(self.db, self.safeguard)
        self.addCleanup(bridge.deleteLater)
        equipment_id = self.create_from_bridge(bridge)
        equipment = self.db.get_equipment_by_id(equipment_id)
        self.assertEqual('OFF-PID-101', equipment['tag'])
        self.assertEqual('Level switch', equipment['equipment_type'])
        self.assertIsNone(equipment['pid_page'])
        self.assertEqual(0, self.db.conn.execute('SELECT COUNT(*) FROM equipment_markers').fetchone()[0])
        bridge._trigger.setCurrentText('HH')
        bridge._accept_link()
        self.assertEqual(QDialog.DialogCode.Accepted, bridge.result())
        source = self.db.lopa_sources(bridge.result_revision_id)[0]
        self.assertEqual(equipment_id, source['equipment_id'])
        self.assertTrue(self.db.lopa_source_calculation(source['id'])['complete'])
        reopened = LopaLinkDialog(self.db, self.safeguard)
        self.addCleanup(reopened.deleteLater)
        self.assertEqual(equipment_id, reopened._equipment.currentData())
        self.assertEqual('HH', reopened._trigger.currentText())

    def test_new_object_becomes_primary_without_losing_existing_sensor(self):
        existing = self.db.add_equipment_item('OLD', 'OLD', '', 0, '', '', 0)
        self.db.add_safeguard_equipment_link(self.safeguard, existing, 'HH')
        bridge = LopaLinkDialog(self.db, self.safeguard)
        self.addCleanup(bridge.deleteLater)
        created = self.create_from_bridge(bridge)
        bridge._trigger.setCurrentText('HH')
        bridge._accept_link()
        source = self.db.lopa_sources(bridge.result_revision_id)[0]
        self.assertEqual(created, source['equipment_id'])
        group = self.db.lopa_sensor_groups(bridge.result_revision_id)[0]
        self.assertEqual({existing, created}, {r['equipment_id'] for r in self.db.lopa_sensor_members(group['id'])})
        self.assertEqual('', self.db.lopa_source_sensor_membership_issue(source['id']))

    def test_custom_trigger_is_restored_when_reopening(self):
        bridge = LopaLinkDialog(self.db, self.safeguard)
        self.addCleanup(bridge.deleteLater)
        self.create_from_bridge(bridge)
        bridge._trigger.setCurrentIndex(7)
        bridge._custom_trigger.setText('Pump stopped')
        bridge._accept_link()
        reopened = LopaLinkDialog(self.db, self.safeguard)
        self.addCleanup(reopened.deleteLater)
        self.assertEqual(7, reopened._trigger.currentIndex())
        self.assertEqual('Pump stopped', reopened._custom_trigger.text())

    def test_cancel_empty_and_duplicate_create_no_objects(self):
        dialog = LopaEquipmentDialog(self.db)
        self.addCleanup(dialog.deleteLater)
        before = len(self.db.equipment_items())
        dialog.reject()
        self.assertEqual(before, len(self.db.equipment_items()))
        with patch('lopa_panel.QMessageBox.warning') as warning:
            dialog._create_object()
            warning.assert_called_once()
        self.assertEqual(before, len(self.db.equipment_items()))
        self.db.add_equipment_item('OFF-1', 'OFF-1', '', None, '', '', 0)
        dialog._tag.setText('  off-1  ')
        with patch('lopa_panel.QMessageBox.warning') as warning:
            dialog._create_object()
            warning.assert_called_once()
        self.assertEqual(before + 1, len(self.db.equipment_items()))
        self.assertIsNone(dialog.result_equipment_id)

    def test_cancel_bridge_object_dialog_preserves_selection(self):
        bridge = LopaLinkDialog(self.db, self.safeguard)
        self.addCleanup(bridge.deleteLater)
        with patch.object(LopaEquipmentDialog, 'exec', return_value=QDialog.DialogCode.Rejected):
            bridge._new_equipment.click()
        self.assertIsNone(bridge._equipment.currentData())
        self.assertEqual([], self.db.equipment_items())

    def test_object_creation_is_undoable(self):
        dialog = LopaEquipmentDialog(self.db)
        self.addCleanup(dialog.deleteLater)
        dialog._tag.setText('MANUAL-1')
        dialog._create_object()
        equipment_id = dialog.result_equipment_id
        self.assertTrue(self.db.undo())
        self.assertIsNone(self.db.get_equipment_by_id(equipment_id))
        self.assertTrue(self.db.redo())
        self.assertIsNotNone(self.db.get_equipment_by_id(equipment_id))

    def test_locked_lopa_does_not_gain_a_hazop_link(self):
        created = self.db.create_lopa()
        self.db.conn.execute("UPDATE lopa_revisions SET status=? WHERE id=?", ('L\u00e5st', created['revision_id']))
        self.db.commit()
        bridge = LopaLinkDialog(self.db, self.safeguard)
        self.addCleanup(bridge.deleteLater)
        self.create_from_bridge(bridge)
        bridge._lopa.setCurrentIndex(bridge._lopa.findData(created['lopa_id']))
        with patch('lopa_panel.QMessageBox.warning') as warning:
            bridge._accept_link()
            warning.assert_called_once()
        self.assertEqual([], self.db.safeguard_equipment_links(self.safeguard))
        self.assertEqual([], self.db.lopa_sources(created['revision_id']))
