"""Workbook verification for the revision-safe LOPA Excel export."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_TEST_DIR = Path(__file__).resolve().parent
_HAZOP_DIR = _TEST_DIR.parent
for _path in (_HAZOP_DIR, _TEST_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from database import Database
from lopa_export import export_lopa_excel


class LopaExportTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='hazop_lopa_export_')
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))

    def tearDown(self):
        self.db.conn.close()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_export_contains_revision_snapshot_and_lopa_sections(self):
        node_id = self.db.add_node()
        deviation_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(deviation_id)
        self.db.update_cause(cause_id, description='LT-101 HH', base_frequency=0.1,
                             frequency_cleared=False)
        consequence_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(consequence_id, 'Överfyllnad', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(consequence_id, category['id'], 3)
        sensor = self.db.add_safeguard(consequence_id)
        self.db.update_safeguard(sensor, description='LSHH', rrf=100, sg_type='SIS')
        barrier = self.db.add_safeguard(consequence_id)
        self.db.update_safeguard(barrier, description='PSV', rrf=10, sg_type='Mekanisk')
        equipment_id = self.db.add_equipment_item('LT-101', 'LT-101', 'LT', 0, 'Nivågivare', '', 0)
        self.db.add_safeguard_equipment_link(sensor, equipment_id, 'HH')
        created = self.db.create_lopa(display_number='017', sif_number='SIF-017',
                                      sif_name='Överfyllnadsskydd', sis_name='SIS-A')
        imported = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)
        self.db.update_lopa_source_analysis_details(
            imported['source_id'], control_frequency='Årligen', assumption_percent=10)
        final_group = self.db.add_lopa_final_group(created['revision_id'])
        self.db.add_lopa_final_member(
            created['revision_id'], equipment_id=equipment_id, action_text='Stäng ventil',
            group_id=final_group)
        self.db.update_lopa_revision_details(
            created['revision_id'], document_date='2026-09-02',
            additional_actions='Verifiera ändlägesbrytare', process_safety_time='8')
        self.db.add_lopa_comment(created['revision_id'], 'Exportkommentar', 'Anton')

        path = os.path.join(self._tmpdir, 'lopa.xlsx')
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        import openpyxl
        workbook = openpyxl.load_workbook(path, data_only=True)
        self.assertEqual(['LOPA sammanfattning', 'LOPA 017 R00'], workbook.sheetnames)
        summary = workbook['LOPA sammanfattning']
        self.assertEqual('LOPA-nr', summary['A3'].value)
        self.assertEqual('017', summary['A4'].value)
        self.assertNotEqual('Ofullständig', summary['I4'].value)
        sheet = workbook['LOPA 017 R00']
        values = [cell.value for row in sheet.iter_rows() for cell in row if cell.value]
        for required in ('Skyddsbarriäranalys (LOPA)', 'Källscenarier från HAZOP',
                         'Givardel', 'Manöverdel', 'Oberoende barriärer',
                         'Eskalering och beräkningsspår', 'Kommentarer',
                         'Exportkommentar', 'SIF-017', 'Stäng ventil',
                         'Konsekvens', 'Överfyllnad', 'Gäller för konsekvens',
                         'Konsekvens: Överfyllnad'):
            self.assertIn(required, values)

        self.db.set_consequence_severity(consequence_id, category['id'], 2)
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        workbook = openpyxl.load_workbook(path, data_only=True)
        values = [cell.value for row in workbook['LOPA 017 R00'].iter_rows() for cell in row]
        self.assertIn('HAZOP har ändrats', values)

        config = self.db.lopa_matrix_config()
        key = next(iter(config['category_settings']))
        config['category_settings'][key]['tel'][0] = 0.000001
        self.db.set_lopa_matrix_config(config)
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        workbook = openpyxl.load_workbook(path, data_only=True)
        self.assertIn('Riskmatrisen', workbook['LOPA 017 R00']['A6'].value)

        incomplete = self.db.add_lopa_local_source(created['revision_id'], cause_text='Ofullständig')
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        workbook = openpyxl.load_workbook(path, data_only=True)
        self.assertEqual('Ofullständig', workbook['LOPA sammanfattning']['I4'].value)
        self.db.set_lopa_source_active(incomplete, False)
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        workbook = openpyxl.load_workbook(path, data_only=True)
        self.assertNotEqual('Ofullständig', workbook['LOPA sammanfattning']['I4'].value)

        self.db.lock_lopa_revision(created['revision_id'])
        self.db.update_lopa_record(created['lopa_id'], sif_name='Namn efter låsning')
        self.db.update_equipment_item(equipment_id, 'LT-102', 'LT', 'Nivågivare', '')
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        workbook = openpyxl.load_workbook(path, data_only=True)
        values = [cell.value for row in workbook['LOPA 017 R00'].iter_rows() for cell in row]
        self.assertIn('LT-101', values)
        self.assertIn('LT-101 HH', values)
        self.assertIn('Överfyllnadsskydd', values)
        self.assertNotIn('Namn efter låsning', values)
        self.assertNotIn('LT-102', values)
        self.assertNotIn('LT-102 HH', values)

    def test_export_includes_reviewed_independence_and_excludes_same_sif(self):
        node = self.db.add_node()
        deviation = self.db.deviations(node)[0]['id']
        cause = self.db.add_cause(deviation)
        self.db.update_cause(cause, description='Gemensam givare', base_frequency=0.1,
                             frequency_cleared=False)
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Utsläpp', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(consequence, category['id'], 3)
        equipment = self.db.add_equipment_item('LT-1', 'LT-1', 'LT', 0, 'Givare', '', 0)
        sensor = self.db.add_safeguard(consequence)
        self.db.add_safeguard_equipment_link(sensor, equipment, 'HH')
        duplicate = self.db.add_safeguard(consequence)
        self.db.add_safeguard_equipment_link(duplicate, equipment, 'HH')
        other = self.db.add_safeguard(consequence)
        self.db.add_safeguard_equipment_link(other, equipment, 'LL')
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        reviewed = next(row for row in self.db.lopa_barriers(created['revision_id'], source)
                        if row['source_safeguard_id'] == other)
        self.db.confirm_lopa_barrier_independence(reviewed['id'], 'Ritning 42 granskad')
        path = os.path.join(self._tmpdir, 'oberoende.xlsx')
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        import openpyxl
        workbook = openpyxl.load_workbook(path, data_only=True)
        values = [cell.value for row in workbook.worksheets[1].iter_rows() for cell in row]
        self.assertIn('Samma SIF – räknas inte som barriär', values)
        self.assertIn('Gemensamt objekt – oberoende bedömt', values)
        self.assertIn('Ritning 42 granskad', values)

    def test_export_lists_unassessed_consequence_and_exclusion_reason(self):
        node = self.db.add_node()
        cause = self.db.add_cause(self.db.deviations(node)[0]['id'])
        self.db.update_cause(cause, description='Källa', base_frequency=0.1, frequency_cleared=False)
        assessed = self.db.add_consequence(cause)
        self.db.update_consequence(assessed, 'Bedömd', 3, '')
        self.db.set_consequence_severity(assessed, self.db.consequence_categories()[0]['id'], 3)
        sensor = self.db.add_safeguard(assessed)
        unassessed = self.db.add_consequence(cause)
        self.db.update_consequence(unassessed, 'Obedömd', 0, '')
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        path = os.path.join(self._tmpdir, 'konsekvenser.xlsx')
        import openpyxl
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        workbook = openpyxl.load_workbook(path, data_only=True)
        self.assertEqual('Ofullständig', workbook.worksheets[0]['I4'].value)
        values = [cell.value for row in workbook.worksheets[1].iter_rows() for cell in row]
        self.assertIn('Bedömning saknas', values)
        self.db.set_lopa_unassessed_exclusion(source, unassessed, 'Utanför analysen')
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        workbook = openpyxl.load_workbook(path, data_only=True)
        values = [cell.value for row in workbook.worksheets[1].iter_rows() for cell in row]
        self.assertIn('Obedömd', values)
        self.assertIn('Utanför analysen', values)

    def test_export_marks_changed_origin_sensor_as_incomplete(self):
        node = self.db.add_node()
        cause = self.db.add_cause(self.db.deviations(node)[0]['id'])
        self.db.update_cause(cause, description='Nivå', base_frequency=0.1, frequency_cleared=False)
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Utsläpp', 3, '')
        self.db.set_consequence_severity(consequence, self.db.consequence_categories()[0]['id'], 3)
        sensor = self.db.add_safeguard(consequence)
        equipment = self.db.add_equipment_item('LT-3', 'LT-3', 'LT', 0, 'Givare', '', 0)
        self.db.add_safeguard_equipment_link(sensor, equipment, 'HH')
        created = self.db.create_lopa()
        self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)
        self.db.add_safeguard_equipment_link(sensor, equipment, 'LL')
        path = os.path.join(self._tmpdir, 'sensor.xlsx')
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        import openpyxl
        workbook = openpyxl.load_workbook(path, data_only=True)
        self.assertEqual('Ofullständig', workbook.worksheets[0]['I4'].value)
        values = [cell.value for row in workbook.worksheets[1].iter_rows() for cell in row]
        self.assertIn('HAZOP-givarkoppling ändrad', values)

    def test_export_labels_reconstructed_legacy_origin_even_after_locking(self):
        node = self.db.add_node()
        cause = self.db.add_cause(self.db.deviations(node)[0]['id'])
        self.db.update_cause(cause, description='Källa', base_frequency=0.1, frequency_cleared=False)
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Utsläpp', 3, '')
        self.db.set_consequence_severity(consequence, self.db.consequence_categories()[0]['id'], 3)
        sensor = self.db.add_safeguard(consequence)
        equipment = self.db.add_equipment_item('LT-4', 'LT-4', 'LT', 0, 'Givare', '', 0)
        self.db.add_safeguard_equipment_link(sensor, equipment, 'HH')
        created = self.db.create_lopa()
        self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)
        self.db.conn.execute('ALTER TABLE lopa_source_scenarios DROP COLUMN origin_equipment_snapshot_json')
        self.db.conn.execute('ALTER TABLE lopa_source_scenarios DROP COLUMN origin_equipment_snapshot_legacy')
        self.db.conn.commit()
        self.db.conn.close()
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))
        self.db.lock_lopa_revision(created['revision_id'])
        path = os.path.join(self._tmpdir, 'gammal-sensor.xlsx')
        ok, error = export_lopa_excel(self.db, path, [(created['lopa_id'], created['revision_id'])])
        self.assertTrue(ok, error)
        import openpyxl
        workbook = openpyxl.load_workbook(path, data_only=True)
        values = [cell.value for row in workbook.worksheets[1].iter_rows() for cell in row]
        self.assertIn('Låst; äldre givarkoppling återskapad från HAZOP', values)

    def test_export_accepts_multiple_explicit_revisions(self):
        first = self.db.create_lopa(display_number='001', sif_name='Första SIF')
        second = self.db.create_lopa(display_number='002', sif_name='Andra SIF')
        path = os.path.join(self._tmpdir, 'flera_lopa.xlsx')
        ok, error = export_lopa_excel(
            self.db, path,
            [(first['lopa_id'], first['revision_id']),
             (second['lopa_id'], second['revision_id'])])
        self.assertTrue(ok, error)
        import openpyxl
        workbook = openpyxl.load_workbook(path, data_only=True)
        self.assertEqual(['LOPA sammanfattning', 'LOPA 001 R00', 'LOPA 002 R00'], workbook.sheetnames)
        self.assertEqual('001', workbook['LOPA sammanfattning']['A4'].value)
        self.assertEqual('002', workbook['LOPA sammanfattning']['A5'].value)


if __name__ == '__main__':
    unittest.main()
