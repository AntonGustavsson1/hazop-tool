"""Focused tests for the first LOPA vertical slice.

These tests intentionally cover the dependency-free calculator as well as
the SQLite migration/import route.  A LOPA result must never look complete
when its numeric demand frequency or TEL is missing.
"""

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
from lopa_models import calculate_lopa, normalise_lopa_config


class LopaModelTests(unittest.TestCase):
    def _matrix(self):
        return {
            'rows': 3,
            'consequence_categories': [
                {'key': 'person', 'name': 'Person', 'color': '#123456'},
                {'key': 'rykte', 'name': 'Rykte', 'color': '#654321'},
            ],
        }

    def test_lopa_config_follows_dynamic_matrix_categories_and_keeps_blank_tel(self):
        config = normalise_lopa_config({
            'category_settings': {
                'person': {'tel': [0.1, None, 0.001]},
            },
        }, self._matrix())
        self.assertEqual({'person', 'rykte'}, set(config['category_settings']))
        self.assertEqual([0.1, None, 0.001], config['category_settings']['person']['tel'])
        self.assertEqual([0.01, 0.001, 0.0001],
                         config['category_settings']['rykte']['tel'])

    def test_calculator_uses_percentage_as_fraction_and_selects_governing_category(self):
        matrix = self._matrix()
        config = normalise_lopa_config({}, matrix)
        result = calculate_lopa({
            'matrix': matrix,
            'base_frequency': 0.1,
            'assumption_percent': 10,
            'barriers': [
                {'description': 'Oberoende barriär', 'rrf': 10, 'categories': None},
            ],
            'categories': [
                {'category_key': 'person', 'category_name': 'Person', 'severity': 3,
                 'factors': {'antandning': 10, 'narvaro': 100, 'skada': 100}},
                {'category_key': 'rykte', 'category_name': 'Rykte', 'severity': 2,
                 'factors': {'antandning': 100, 'narvaro': 100, 'skada': 100}},
            ],
        }, config)
        person = next(row for row in result['categories'] if row['category_key'] == 'person')
        self.assertAlmostEqual(0.01, result['effective_frequency'])
        self.assertAlmostEqual(0.001, person['remaining_frequency'])
        self.assertAlmostEqual(0.0001, person['accident_frequency'])
        # Both rows require RRF 1 here; a deterministic tie keeps the first
        # visible category as governing rather than inventing a second rule.
        self.assertEqual('person', result['governing_category_key'])
        self.assertTrue(result['complete'])

    def test_calculator_never_claims_sil_without_numeric_frequency_or_tel(self):
        matrix = self._matrix()
        config = normalise_lopa_config({
            'category_settings': {'person': {'tel': [None, None, None]}},
        }, matrix)
        result = calculate_lopa({
            'matrix': matrix,
            'base_frequency': None,
            'categories': [{'category_key': 'person', 'severity': 3}],
        }, config)
        self.assertFalse(result['complete'])
        self.assertIsNone(result['sil'])
        self.assertIn('Numerisk grundfrekvens saknas.', result['messages'])


class LopaDatabaseTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='hazop_lopa_test_')
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))

    def tearDown(self):
        try:
            self.db.conn.close()
        except Exception:
            pass
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _hazop_chain(self):
        node_id = self.db.add_node()
        deviation_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(deviation_id)
        self.db.update_cause(cause_id, description='LT-101 HH', base_frequency=0.1)
        consequence_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(consequence_id, 'Utsläpp', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(consequence_id, category['id'], 3)
        sensor_sg = self.db.add_safeguard(consequence_id)
        self.db.update_safeguard(sensor_sg, description='LSHH trip', rrf=100, sg_type='SIS')
        other_sg = self.db.add_safeguard(consequence_id)
        self.db.update_safeguard(other_sg, description='Tryckavlastning', rrf=10, sg_type='Mekanisk')
        equipment_id = self.db.add_equipment_item(
            'LT-101', 'LT-101', 'LT', 0, 'Nivågivare', '', 0)
        self.db.add_safeguard_equipment_link(sensor_sg, equipment_id, 'HH')
        return cause_id, sensor_sg, other_sg, equipment_id

    def test_create_import_copy_and_lock_lopa_revision(self):
        _cause_id, sensor_sg, other_sg, equipment_id = self._hazop_chain()
        created = self.db.create_lopa(sif_name='SIF nivåstopp')
        self.assertEqual('001', self.db.get_lopa_record(created['lopa_id'])['display_number'])
        self.assertEqual('00', self.db.get_lopa_revision(created['revision_id'])['label'])
        self.assertIn('rykte', self.db.lopa_matrix_config()['category_settings'])

        imported = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor_sg)
        self.assertTrue(imported['created'])
        source = self.db.lopa_sources(created['revision_id'])[0]
        self.assertEqual(0.1, source['base_frequency'])
        self.assertEqual('HH', source['trigger_code'])
        members = self.db.lopa_sensor_members(
            self.db.lopa_sensor_groups(created['revision_id'])[0]['id'])
        self.assertEqual([(equipment_id, 'HH')],
                         [(row['equipment_id'], row['trigger_code']) for row in members])
        barriers = self.db.lopa_barriers(created['revision_id'], imported['source_id'])
        self.assertEqual([other_sg], [row['source_safeguard_id'] for row in barriers])
        calculation = self.db.lopa_source_calculation(imported['source_id'])
        self.assertTrue(calculation['complete'])

        copied_revision = self.db.create_lopa_revision(created['lopa_id'])
        self.assertEqual('01', self.db.get_lopa_revision(copied_revision)['label'])
        self.assertEqual(1, len(self.db.lopa_sources(copied_revision)))
        self.assertEqual(1, len(self.db.lopa_barriers(copied_revision)))
        self.db.lock_lopa_revision(copied_revision, actor='Anton')
        with self.assertRaises(PermissionError):
            self.db.add_lopa_barrier(copied_revision, description='Otillåten', rrf=10)
        with self.assertRaises(ValueError):
            self.db.unlock_lopa_revision(copied_revision, '')
        self.db.unlock_lopa_revision(copied_revision, 'Ny granskning', actor='Anton')
        self.assertEqual('Utkast', self.db.get_lopa_revision(copied_revision)['status'])

    def test_manual_archived_number_requires_explicit_confirmation(self):
        first = self.db.create_lopa(display_number='017')
        self.db.archive_lopa(first['lopa_id'])
        with self.assertRaises(ValueError):
            self.db.create_lopa(display_number='017')
        second = self.db.create_lopa(display_number='017', allow_archived_reuse=True)
        self.assertNotEqual(first['lopa_id'], second['lopa_id'])

    def test_second_sensor_requires_explicit_voting_confirmation(self):
        _cause_id, sensor_sg, _other_sg, _equipment_id = self._hazop_chain()
        second_equipment = self.db.add_equipment_item(
            'LT-102', 'LT-102', 'LT', 0, 'Nivågivare', '', 0)
        self.db.add_safeguard_equipment_link(sensor_sg, second_equipment, 'HH')
        created = self.db.create_lopa()
        self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor_sg)
        group = self.db.lopa_sensor_groups(created['revision_id'])[0]
        self.assertEqual('1oo1', group['voting'])
        self.assertTrue(group['needs_voting_review'])
        self.db.set_lopa_sensor_group_voting(group['id'], '1oo2')
        self.assertEqual('1oo2', self.db.lopa_sensor_groups(created['revision_id'])[0]['voting'])
        self.assertFalse(self.db.lopa_sensor_groups(created['revision_id'])[0]['needs_voting_review'])

    def test_lopa_detail_edits_are_revision_scoped_and_traceable(self):
        _cause_id, sensor_sg, _other_sg, _equipment_id = self._hazop_chain()
        created = self.db.create_lopa()
        imported = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor_sg)
        revision_id = created['revision_id']
        source_id = imported['source_id']

        self.db.update_lopa_revision_details(
            revision_id, performed_by_text='Anton', approved_by_text='Granskare')
        revision = self.db.get_lopa_revision(revision_id)
        self.assertEqual('Anton', revision['performed_by_text'])
        self.assertEqual('Granskare', revision['approved_by_text'])

        self.db.set_lopa_source_scenario_text(source_id, 'Nivån stiger lokalt')
        source = self.db.lopa_sources(revision_id)[0]
        self.assertEqual('Nivån stiger lokalt', source['scenario_text'])
        self.assertFalse(source['follows_hazop'])

        consequence = self.db.lopa_source_consequences(source_id)[0]
        self.db.set_lopa_consequence_active(consequence['id'], False)
        self.assertFalse(self.db.lopa_source_consequences(source_id)[0]['active'])
        self.db.update_lopa_consequence(consequence['id'], severity=2)
        consequence = self.db.lopa_source_consequences(source_id)[0]
        self.assertEqual(2, consequence['severity'])
        self.assertFalse(consequence['follows_hazop'])

        barrier = self.db.lopa_barriers(revision_id, source_id)[0]
        self.db.set_lopa_barrier_active(barrier['id'], False)
        self.assertFalse(self.db.lopa_barriers(revision_id, source_id)[0]['active'])
        self.db.update_lopa_barrier(barrier['id'], rrf=25, sg_type='Mekanisk')
        barrier = self.db.lopa_barriers(revision_id, source_id)[0]
        self.assertEqual(25.0, barrier['rrf'])
        self.assertFalse(barrier['follows_hazop'])
        self.db.set_lopa_barrier_category_keys(barrier['id'], ['person'])
        self.assertEqual(['person'], [row['category_key']
                                      for row in self.db.lopa_barrier_categories(barrier['id'])])

        self.assertEqual(1, len(self.db.lopa_escalation_rows(source_id)))
        self.db.set_lopa_escalation_values(
            source_id, 'person', {'antandning': 10, 'narvaro': 20, 'skada': 30},
            reason='Endast vid uppstart')
        result = self.db.lopa_source_calculation(source_id)
        category = next(row for row in result['categories'] if row['category_key'] == 'person')
        self.assertAlmostEqual(0.006, category['escalation_factor'])

        added_equipment = self.db.add_equipment_item(
            'LT-103', 'LT-103', 'LT', 0, 'Nivågivare', '', 0)
        group = self.db.lopa_sensor_groups(revision_id)[0]
        self.db.add_lopa_sensor_member(revision_id, added_equipment, 'HH', group_id=group['id'])
        self.assertTrue(self.db.lopa_sensor_groups(revision_id)[0]['needs_voting_review'])
        self.db.set_lopa_sensor_group_voting(group['id'], '1oo2')

        self.db.lock_lopa_revision(revision_id)
        with self.assertRaises(PermissionError):
            self.db.set_lopa_source_active(source_id, False)

    def test_deleted_hazop_source_is_retained_and_flagged_for_lopa(self):
        _cause_id, sensor_sg, _other_sg, _equipment_id = self._hazop_chain()
        created = self.db.create_lopa()
        self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor_sg)
        self.db.delete_safeguard(sensor_sg)
        source = self.db.lopa_sources(created['revision_id'])[0]
        self.assertTrue(source['source_missing'])
        self.assertIsNone(source['origin_safeguard_id'])

    def test_document_fields_final_element_and_revision_copy_are_traceable(self):
        _cause_id, sensor_sg, _other_sg, equipment_id = self._hazop_chain()
        created = self.db.create_lopa()
        imported = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor_sg)
        revision_id = created['revision_id']
        source_id = imported['source_id']

        self.db.update_lopa_revision_details(
            revision_id, document_date='2026-09-02', performed_by_text='Anton',
            approved_by_text='Granskare', additional_actions='Verifiera ventilläge',
            additional_requirements='Separat SIF-krav', process_safety_time='12,5')
        self.db.update_lopa_source_analysis_details(
            source_id, control_frequency='Årligt provtest', assumption_percent=10,
            assumption_reason='Endast under uppstart')
        final_group = self.db.add_lopa_final_group(revision_id, '1oo1')
        final_member = self.db.add_lopa_final_member(
            revision_id, equipment_id=equipment_id, action_text='Stäng ventil', group_id=final_group)
        self.db.update_lopa_final_member(final_member, action_text='Snabbstäng ventil')
        self.db.add_lopa_comment(revision_id, 'Kontrollera oberoende mellan givare och ventil.', 'Anton')

        revision = self.db.get_lopa_revision(revision_id)
        self.assertEqual('2026-09-02', revision['document_date'])
        self.assertEqual('Verifiera ventilläge', revision['additional_actions'])
        self.assertAlmostEqual(12.5, revision['process_safety_time'])
        source = self.db.lopa_sources(revision_id)[0]
        self.assertEqual(10.0, source['assumption_percent'])
        self.assertEqual('Årligt provtest', source['control_frequency'])
        self.assertEqual('Snabbstäng ventil', self.db.lopa_final_members(final_group)[0]['action_text'])
        self.assertEqual('Anton', self.db.lopa_comments(revision_id)[0]['author'])

        copied_id = self.db.create_lopa_revision(created['lopa_id'], revision_id)
        copied = self.db.get_lopa_revision(copied_id)
        self.assertEqual('2026-09-02', copied['document_date'])
        self.assertEqual('Separat SIF-krav', copied['additional_requirements'])
        copied_group = self.db.lopa_final_groups(copied_id)[0]
        self.assertEqual('1oo1', copied_group['voting'])
        self.assertEqual('Snabbstäng ventil', self.db.lopa_final_members(copied_group['id'])[0]['action_text'])
        self.assertEqual('Årligt provtest', self.db.lopa_sources(copied_id)[0]['control_frequency'])
        # Comments are revision-specific evidence, not duplicated into a new draft.
        self.assertEqual([], self.db.lopa_comments(copied_id))
        self.db.update_lopa_revision_details(copied_id, process_safety_time='')
        self.assertIsNone(self.db.get_lopa_revision(copied_id)['process_safety_time'])

    def test_hazop_sync_check_warns_without_overwriting_revision_snapshot(self):
        cause_id, sensor_sg, _other_sg, _equipment_id = self._hazop_chain()
        created = self.db.create_lopa()
        imported = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor_sg)
        source_id = imported['source_id']
        self.assertEqual('current', self.db.lopa_source_sync_state(source_id)['state'])

        self.db.update_cause(cause_id, description='LT-101 HH uppdaterad', base_frequency=0.2)
        state = self.db.lopa_source_sync_state(source_id)
        self.assertEqual('changed', state['state'])
        self.assertIn('orsakstext', state['messages'])
        # Read-only sync detection does not silently alter the stored LOPA
        # calculation basis; the analyst must choose a later reconciliation.
        self.assertEqual('LT-101 HH', self.db.lopa_sources(created['revision_id'])[0]['cause_text'])

        self.db.set_lopa_source_scenario_text(source_id, 'Lokal scenarioavvikelse')
        self.assertEqual('detached', self.db.lopa_source_sync_state(source_id)['state'])
