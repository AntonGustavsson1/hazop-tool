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

    def test_invalid_inputs_and_missing_category_cannot_produce_governing_sil(self):
        matrix = self._matrix()
        config = normalise_lopa_config({}, matrix)
        base = {'matrix': matrix, 'base_frequency': 0.1,
                'categories': [{'category_key': 'person', 'severity': 2}]}
        cases = [
            {'base_frequency': -1},
            {'assumption_percent': -1},
            {'assumption_percent': 101},
            {'assumption_percent': 'invalid'},
            {'barriers': [{'description': 'Invalid', 'rrf': 0}]},
            {'categories': [{'category_key': 'person', 'severity': 2,
                             'factors': {'narvaro': 101}}]},
            {'categories': [{'category_key': 'person', 'severity': 2},
                            {'category_key': 'rykte', 'severity': 0}]},
            {'categories': [{'category_key': 'person', 'severity': 2},
                            {'category_key': 'unknown', 'severity': 2}]},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                result = calculate_lopa({**base, **changes}, config)
                self.assertFalse(result['complete'])
                self.assertIsNone(result['required_rrf'])
                self.assertIsNone(result['sil'])
                self.assertTrue(result['messages'])


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
        self.db.update_cause(cause_id, description='LT-101 HH', base_frequency=0.1,
                             frequency_cleared=False)
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

    def test_imported_lopa_keeps_consequences_separate_from_category_assessments(self):
        """A source is one cause, not one row for every category assessment."""
        cause_id, sensor_sg, _other_sg, _equipment_id = self._hazop_chain()
        first_consequence_id = self.db.get_safeguard(sensor_sg)['consequence_id']
        categories = self.db.consequence_categories()
        self.db.set_consequence_severity(first_consequence_id, categories[1]['id'], 2)
        second_consequence_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(second_consequence_id, 'Sekundärt utsläpp', 4, '')
        self.db.set_consequence_severity(second_consequence_id, categories[0]['id'], 4)
        self.db.set_consequence_severity(second_consequence_id, categories[1]['id'], 1)
        unassessed_consequence_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(unassessed_consequence_id, 'Oklassificerad följd', 0, '')

        created = self.db.create_lopa()
        source_id = self.db.add_lopa_source_from_safeguard(
            created['lopa_id'], sensor_sg)['source_id']

        groups = self.db.lopa_source_consequence_groups(source_id)
        self.assertEqual(
            [first_consequence_id, second_consequence_id, unassessed_consequence_id],
            [group['hazop_consequence_id'] for group in groups])
        self.assertEqual(['Utsläpp', 'Sekundärt utsläpp', 'Oklassificerad följd'],
                         [group['description'] for group in groups])
        self.assertEqual([2, 2, 0], [len(group['assessments']) for group in groups])

        calculation = self.db.lopa_source_calculation(source_id)
        self.assertEqual(4, len(calculation['categories']))
        self.assertEqual(
            [first_consequence_id, first_consequence_id,
             second_consequence_id, second_consequence_id],
            [row['hazop_consequence_id'] for row in calculation['categories']])
        self.assertTrue(all(row['lopa_consequence_id'] is not None
                            for row in calculation['categories']))

        # Text belongs to the consequence itself, while level belongs to one
        # category. A local text override must never split the same HAZOP
        # consequence into contradictory category-specific descriptions.
        first_assessment = groups[0]['assessments'][0]
        self.db.update_lopa_consequence(
            first_assessment['id'], description='Lokal överfyllnadstext')
        first_rows = [row for row in self.db.lopa_source_consequences(source_id)
                      if row['hazop_consequence_id'] == first_consequence_id]
        self.assertEqual(['Lokal överfyllnadstext', 'Lokal överfyllnadstext'],
                         [row['description'] for row in first_rows])

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

    def test_missing_hazop_source_blocks_draft_but_preserves_locked_calculation(self):
        _cause_id, sensor_sg, _other_sg, _equipment_id = self._hazop_chain()
        created = self.db.create_lopa()
        source_id = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor_sg)['source_id']
        before = self.db.lopa_source_calculation(source_id)
        self.assertTrue(before['complete'])
        locked_revision = self.db.create_lopa_revision(created['lopa_id'], created['revision_id'])
        locked_source = self.db.lopa_sources(locked_revision)[0]['id']
        self.db.lock_lopa_revision(locked_revision)
        locked_before = self.db.lopa_source_calculation(locked_source)
        self.db.delete_safeguard(sensor_sg)
        draft = self.db.lopa_source_calculation(source_id)
        self.assertFalse(draft['complete'])
        self.assertIsNone(draft['required_rrf'])
        self.assertIsNone(draft['sil'])
        self.assertTrue(any('HAZOP-källan saknas' in m for m in draft['messages']))
        self.assertTrue(all(row['sil'] is None for row in draft['categories']))
        with self.assertRaisesRegex(ValueError, 'saknas'):
            self.db.lock_lopa_revision(created['revision_id'])
        self.assertEqual('Utkast', self.db.get_lopa_revision(created['revision_id'])['status'])
        self.assertEqual(locked_before, self.db.lopa_source_calculation(locked_source))
        self.assertTrue(self.db.undo())
        self.assertEqual(before, self.db.lopa_source_calculation(source_id))
        self.assertTrue(self.db.redo())
        self.assertFalse(self.db.lopa_source_calculation(source_id)['complete'])

    def test_deleted_imported_barrier_cannot_silently_reduce_draft_risk(self):
        _cause, sensor, barrier, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.db.delete_safeguard(barrier)
        result = self.db.lopa_source_calculation(source)
        self.assertFalse(result['complete'])
        self.assertIsNone(result['sil'])
        self.assertTrue(any('HAZOP-barriär saknas' in message for message in result['messages']))
        self.assertTrue(self.db.undo())
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(self.db.redo())
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])

    def test_deleted_imported_consequence_cannot_remain_a_complete_draft(self):
        cause, sensor, _barrier, _equipment = self._hazop_chain()
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Ytterligare konsekvens', 2, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(consequence, category['id'], 2)
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.db.delete_consequence(consequence)
        result = self.db.lopa_source_calculation(source)
        self.assertFalse(result['complete'])
        self.assertTrue(any('HAZOP-konsekvens saknas' in message for message in result['messages']))
        self.assertTrue(self.db.undo())
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(self.db.redo())
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])

    def test_cascaded_deletion_of_original_safeguard_is_detected(self):
        _cause, sensor, _barrier, _equipment = self._hazop_chain()
        origin_consequence = self.db.get_safeguard(sensor)['consequence_id']
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.delete_consequence(origin_consequence)
        result = self.db.lopa_source_calculation(source)
        self.assertFalse(result['complete'])
        self.assertTrue(any('HAZOP-källan saknas' in message for message in result['messages']))
        self.assertIsNone(result['sil'])

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

    def test_locked_revision_retains_object_tags_as_they_were_at_lock(self):
        _cause, sensor, _other, equipment = self._hazop_chain()
        created = self.db.create_lopa()
        revision_id = created['revision_id']
        self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)
        sensor_group = self.db.lopa_sensor_groups(revision_id)[0]['id']
        final_group = self.db.add_lopa_final_group(revision_id)
        self.db.add_lopa_final_member(revision_id, equipment_id=equipment, group_id=final_group)
        self.db.update_equipment_item(equipment, 'LT-102', 'LT', 'Nivågivare', '')
        self.db.lock_lopa_revision(revision_id)
        self.db.update_equipment_item(equipment, 'LT-103', 'LT', 'Nivågivare', '')
        self.assertEqual('LT-102', self.db.lopa_sources(revision_id)[0]['equipment_tag'])
        self.assertEqual('LT-102', self.db.lopa_sensor_members(sensor_group)[0]['tag'])
        self.assertEqual('LT-102', self.db.lopa_final_members(final_group)[0]['tag'])
        new_revision = self.db.create_lopa_revision(created['lopa_id'], revision_id)
        new_sensor = self.db.lopa_sensor_groups(new_revision)[0]['id']
        new_final = self.db.lopa_final_groups(new_revision)[0]['id']
        self.assertEqual('LT-103', self.db.lopa_sources(new_revision)[0]['equipment_tag'])
        self.assertEqual('LT-103', self.db.lopa_sensor_members(new_sensor)[0]['tag'])
        self.assertEqual('LT-103', self.db.lopa_final_members(new_final)[0]['tag'])

    def test_older_database_backfills_new_source_tag_snapshot_once(self):
        _cause, sensor, _other, equipment = self._hazop_chain()
        created = self.db.create_lopa()
        self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)
        self.db.lock_lopa_revision(created['revision_id'])
        self.db.conn.execute('ALTER TABLE lopa_source_scenarios DROP COLUMN equipment_tag_snapshot')
        self.db.conn.execute('ALTER TABLE lopa_source_scenarios DROP COLUMN consequence_groups_snapshot_json')
        self.db.conn.execute('ALTER TABLE lopa_revisions DROP COLUMN identity_snapshot_json')
        self.db.conn.commit()
        self.db.conn.close()
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))
        self.assertEqual('LT-101', self.db.lopa_sources(created['revision_id'])[0]['equipment_tag'])
        self.assertIsNotNone(self.db.lopa_record_for_revision(created['revision_id']))
        self.db.update_equipment_item(equipment, 'LT-102', 'LT', 'Nivågivare', '')
        self.assertEqual('LT-101', self.db.lopa_sources(created['revision_id'])[0]['equipment_tag'])

    def test_locked_revision_retains_header_and_visible_consequence_groups(self):
        cause, sensor, _other, _equipment = self._hazop_chain()
        extra = self.db.add_consequence(cause)
        self.db.update_consequence(extra, 'Oklassificerad följd', 0, '')
        created = self.db.create_lopa(sif_number='SIF-1', sif_name='Tidigare namn', sis_name='SIS-A')
        revision = created['revision_id']
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        original_groups = self.db.lopa_source_consequence_groups(source)
        self.db.lock_lopa_revision(revision)
        self.assertTrue(self.db.undo())
        self.assertEqual('Utkast', self.db.get_lopa_revision(revision)['status'])
        self.assertTrue(self.db.redo())
        self.assertEqual('Låst', self.db.get_lopa_revision(revision)['status'])
        self.db.update_lopa_record(created['lopa_id'], sif_name='Nytt namn')
        self.db.update_consequence(extra, 'Ändrad följd', 0, '')
        later = self.db.add_consequence(cause)
        self.db.update_consequence(later, 'Ny följd efter låsning', 0, '')
        self.assertEqual('Tidigare namn', self.db.lopa_record_for_revision(revision)['sif_name'])
        self.assertEqual(original_groups, self.db.lopa_source_consequence_groups(source))
        draft = self.db.create_lopa_revision(created['lopa_id'], revision)
        draft_source = self.db.lopa_sources(draft)[0]['id']
        self.assertEqual('Nytt namn', self.db.lopa_record_for_revision(draft)['sif_name'])
        names = [item['description'] for item in self.db.lopa_source_consequence_groups(draft_source)]
        self.assertIn('Ändrad följd', names)
        self.assertIn('Ny följd efter låsning', names)

    def test_locked_group_description_matches_stored_assessment_when_hazop_changed(self):
        _cause, sensor, _other, _equipment = self._hazop_chain()
        consequence = self.db.get_safeguard(sensor)['consequence_id']
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.update_consequence(consequence, 'Ny HAZOP-text', 3, '')
        self.assertEqual('changed', self.db.lopa_source_sync_state(source)['state'])
        self.db.lock_lopa_revision(created['revision_id'])
        group = self.db.lopa_source_consequence_groups(source)[0]
        self.assertEqual('Utsläpp', group['description'])
        self.assertEqual('Utsläpp', group['assessments'][0]['description'])
