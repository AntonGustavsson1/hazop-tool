"""Focused tests for the first LOPA vertical slice.

These tests intentionally cover the dependency-free calculator as well as
the SQLite migration/import route.  A LOPA result must never look complete
when its numeric demand frequency or TEL is missing.
"""

import json
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

    def test_barrier_only_reduces_its_own_consequence(self):
        matrix = self._matrix()
        result = calculate_lopa({
            'matrix': matrix, 'base_frequency': 0.1,
            'categories': [
                {'category_key': 'person', 'severity': 3, 'hazop_consequence_id': 11},
                {'category_key': 'person', 'severity': 3, 'hazop_consequence_id': 12},
            ],
            'barriers': [{'description': 'Barriär', 'rrf': 10, 'hazop_consequence_id': 11}],
        }, normalise_lopa_config({}, matrix))
        self.assertEqual([10, 1], [row['barrier_rrf'] for row in result['categories']])
        self.assertAlmostEqual(0.01, result['categories'][0]['accident_frequency'])
        self.assertAlmostEqual(0.1, result['categories'][1]['accident_frequency'])
        self.assertEqual(result['categories'][1]['required_rrf'], result['required_rrf'])
        self.assertEqual(1, result['governing_category_index'])

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

    def _lopa_with_shared_object(self, barrier_trigger):
        cause, sensor, _other, equipment = self._hazop_chain()
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Annan konsekvens', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(consequence, category['id'], 3)
        barrier = self.db.add_safeguard(consequence)
        self.db.update_safeguard(barrier, description='Skydd på samma givare', rrf=100, sg_type='SIS')
        self.db.add_safeguard_equipment_link(barrier, equipment, barrier_trigger)
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        imported = next(row for row in self.db.lopa_barriers(created['revision_id'], source)
                        if row['source_safeguard_id'] == barrier)
        return created, source, imported, equipment, barrier, consequence

    def test_same_sif_on_another_hazop_consequence_is_not_credited(self):
        created, source, imported, _equipment, _barrier, consequence = self._lopa_with_shared_object('HH')
        result = self.db.lopa_source_calculation(source)
        row = next(row for row in result['categories'] if row['hazop_consequence_id'] == consequence)
        self.assertTrue(result['complete'])
        self.assertEqual(1, row['barrier_rrf'])
        self.assertEqual('same', self.db.lopa_barrier_identity_status(imported)['state'])
        self.assertTrue(any('Samma SIF' in message for message in result['messages']))
        with self.assertRaises(ValueError):
            self.db.confirm_lopa_barrier_independence(imported['id'], 'Inte oberoende')
        self.db.lock_lopa_revision(created['revision_id'])

    def test_shared_object_requires_recorded_review_and_invalidates_it_on_change(self):
        created, source, imported, equipment, _barrier, consequence = self._lopa_with_shared_object('LL')
        result = self.db.lopa_source_calculation(source)
        self.assertFalse(result['complete'])
        self.assertIsNone(result['sil'])
        self.assertEqual('needs_review', self.db.lopa_barrier_identity_status(imported)['state'])
        with self.assertRaises(ValueError):
            self.db.confirm_lopa_barrier_independence(imported['id'], '  ')
        with self.assertRaisesRegex(ValueError, 'oberoende'):
            self.db.lock_lopa_revision(created['revision_id'])
        self.db.confirm_lopa_barrier_independence(imported['id'], 'Oberoende kedjor verifierade mot ritning')
        result = self.db.lopa_source_calculation(source)
        row = next(row for row in result['categories'] if row['hazop_consequence_id'] == consequence)
        self.assertTrue(result['complete'])
        self.assertEqual(100, row['barrier_rrf'])
        self.assertTrue(self.db.undo())
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(self.db.redo())
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.db.add_lopa_sensor_member(created['revision_id'], equipment, trigger_code='LL')
        result = self.db.lopa_source_calculation(source)
        self.assertEqual(1, next(row for row in result['categories']
                                 if row['hazop_consequence_id'] == consequence)['barrier_rrf'])
        imported = next(row for row in self.db.lopa_barriers(created['revision_id'], source)
                        if row['id'] == imported['id'])
        self.assertEqual('same', self.db.lopa_barrier_identity_status(imported)['state'])

    def test_locked_sif_identity_survives_deletion_of_hazop_rows_and_equipment(self):
        created, source, imported, equipment, barrier, consequence = self._lopa_with_shared_object('HH')
        self.db.lock_lopa_revision(created['revision_id'])
        before = self.db.lopa_source_calculation(source)
        self.db.delete_safeguard(barrier)
        self.db.delete_equipment_item(equipment)
        after = self.db.lopa_source_calculation(source)
        self.assertEqual(before, after)
        imported = next(row for row in self.db.lopa_barriers(created['revision_id'], source)
                        if row['id'] == imported['id'])
        self.assertEqual('same', self.db.lopa_barrier_identity_status(imported)['state'])

    def test_missing_trigger_on_shared_object_requires_review(self):
        _created, source, imported, _equipment, _barrier, _consequence = self._lopa_with_shared_object('')
        self.assertEqual('needs_review', self.db.lopa_barrier_identity_status(imported)['state'])
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.db.set_lopa_barrier_active(imported['id'], False)
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])

    def test_old_project_reconstructs_identity_and_marks_it_as_legacy(self):
        created, source, imported, equipment, _barrier, _consequence = self._lopa_with_shared_object('HH')
        self.db.lock_lopa_revision(created['revision_id'])
        self.db.conn.execute('ALTER TABLE lopa_barriers DROP COLUMN equipment_identity_snapshot_json')
        self.db.conn.execute('ALTER TABLE lopa_barriers DROP COLUMN identity_snapshot_legacy')
        self.db.conn.execute('ALTER TABLE lopa_barriers DROP COLUMN independence_fingerprint')
        self.db.conn.execute('ALTER TABLE lopa_barriers DROP COLUMN independence_reason')
        self.db.conn.execute('ALTER TABLE lopa_sensor_members DROP COLUMN equipment_id_snapshot')
        self.db.conn.commit()
        self.db.conn.close()
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))
        imported = next(row for row in self.db.lopa_barriers(created['revision_id'], source)
                        if row['id'] == imported['id'])
        self.assertTrue(imported['identity_snapshot_legacy'])
        self.assertEqual('same', self.db.lopa_barrier_identity_status(imported)['state'])
        group = self.db.lopa_sensor_groups(created['revision_id'])[0]
        self.assertEqual(equipment, self.db.lopa_sensor_members(group['id'])[0]['equipment_id_snapshot'])

    def test_hazop_object_link_changes_warn_without_overwriting_revision_identity(self):
        _cause, sensor, barrier, equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        imported = self.db.lopa_barriers(created['revision_id'], source)[0]
        self.assertEqual('unlinked', self.db.lopa_barrier_identity_status(imported)['state'])
        self.db.add_safeguard_equipment_link(barrier, equipment, 'HH')
        self.assertIn('barriärens objektkoppling', self.db.lopa_source_sync_state(source)['messages'])
        self.assertEqual('changed', self.db.lopa_barrier_identity_status(imported)['state'])
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.db.refresh_lopa_barrier_identity(imported['id'])
        imported = self.db.lopa_barriers(created['revision_id'], source)[0]
        self.assertEqual('same', self.db.lopa_barrier_identity_status(imported)['state'])
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(self.db.undo())
        imported = self.db.lopa_barriers(created['revision_id'], source)[0]
        self.assertEqual('changed', self.db.lopa_barrier_identity_status(imported)['state'])
        self.assertTrue(self.db.redo())
        self.assertEqual('current', self.db.lopa_source_sync_state(source)['state'])

    def test_changed_origin_safeguard_identity_requires_review(self):
        _cause, sensor, _barrier, equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.db.add_safeguard_equipment_link(sensor, equipment, 'LL')
        state = self.db.lopa_source_sync_state(source)
        result = self.db.lopa_source_calculation(source)
        self.assertEqual('changed', state['state'])
        self.assertIn('givarens objektkoppling', state['messages'])
        self.assertFalse(result['complete'])
        self.assertIsNone(result['sil'])
        with self.assertRaisesRegex(ValueError, 'givarkoppling'):
            self.db.lock_lopa_revision(created['revision_id'])
        with self.assertRaisesRegex(ValueError, 'Lägg först till'):
            self.db.refresh_lopa_source_origin_identity(source)
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.db.add_lopa_sensor_member(created['revision_id'], equipment, trigger_code='LL')
        group = self.db.lopa_sensor_groups(created['revision_id'])[0]
        self.db.set_lopa_sensor_group_voting(group['id'], '1oo2')
        self.db.refresh_lopa_source_origin_identity(source)
        self.assertEqual('current', self.db.lopa_source_origin_identity_status(source)['state'])
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(self.db.undo())
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(self.db.redo())
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])

    def test_changed_origin_identity_does_not_rewrite_locked_revision(self):
        _cause, sensor, _barrier, equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.lock_lopa_revision(created['revision_id'])
        before = self.db.lopa_source_calculation(source)
        self.db.add_safeguard_equipment_link(sensor, equipment, 'LL')
        self.assertEqual(before, self.db.lopa_source_calculation(source))
        self.assertEqual('historical', self.db.lopa_source_origin_identity_status(source)['state'])
        with self.assertRaises(PermissionError):
            self.db.refresh_lopa_source_origin_identity(source)
        draft = self.db.create_lopa_revision(created['lopa_id'], created['revision_id'])
        draft_source = self.db.lopa_sources(draft)[0]['id']
        self.assertFalse(self.db.lopa_source_calculation(draft_source)['complete'])
        self.db.set_lopa_source_active(draft_source, False)
        self.assertEqual([], self.db.lopa_source_calculation(draft_source)['origin_identity_problems'])

    def test_first_sensor_creates_group_in_one_undoable_transaction(self):
        created = self.db.create_lopa()
        equipment = self.db.add_equipment_item('LT-201', 'LT-201', 'LT', 0, 'Givare', '', 0)
        before = self.db.undo_count
        member_id = self.db.add_lopa_sensor_member(created['revision_id'], equipment, trigger_code='HH')
        groups = self.db.lopa_sensor_groups(created['revision_id'])
        self.assertEqual(1, len(groups))
        self.assertEqual(member_id, self.db.lopa_sensor_members(groups[0]['id'])[0]['id'])
        self.assertEqual(before + 1, self.db.undo_count)
        self.assertTrue(self.db.undo())
        self.assertEqual([], self.db.lopa_sensor_groups(created['revision_id']))
        self.assertTrue(self.db.redo())
        self.assertEqual(1, len(self.db.lopa_sensor_groups(created['revision_id'])))

    def test_origin_review_updates_source_identity_only_after_matching_sensor_exists(self):
        _cause, sensor, _barrier, equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.conn.execute('DELETE FROM safeguard_equipment_links WHERE safeguard_id=?', (sensor,))
        self.db.commit()
        self.db.add_safeguard_equipment_link(sensor, equipment, 'LL')
        with self.assertRaisesRegex(ValueError, 'Lägg först till'):
            self.db.refresh_lopa_source_origin_identity(source)
        self.assertEqual('HH', self.db.lopa_sources(created['revision_id'])[0]['trigger_code'])
        self.db.add_lopa_sensor_member(created['revision_id'], equipment, trigger_code='LL')
        group = self.db.lopa_sensor_groups(created['revision_id'])[0]
        old_member = next(member for member in self.db.lopa_sensor_members(group['id'])
                          if member['trigger_code'] == 'HH')
        self.db.set_lopa_sensor_member_active(old_member['id'], False)
        self.db.set_lopa_sensor_group_voting(group['id'], '1oo1')
        self.db.refresh_lopa_source_origin_identity(source)
        updated = self.db.lopa_sources(created['revision_id'])[0]
        self.assertEqual('LL', updated['trigger_code'])
        self.assertEqual(equipment, updated['equipment_id'])
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])

    def test_disabling_primary_sensor_cannot_make_same_sif_a_credited_barrier(self):
        created, source, imported, _equipment, _barrier, consequence = self._lopa_with_shared_object('HH')
        group = self.db.lopa_sensor_groups(created['revision_id'])[0]
        member = self.db.lopa_sensor_members(group['id'])[0]
        self.db.set_lopa_sensor_member_active(member['id'], False)
        result = self.db.lopa_source_calculation(source)
        self.assertFalse(result['complete'])
        self.assertIsNone(result['sil'])
        self.assertTrue(any('aktiv givare' in message for message in result['messages']))
        self.assertEqual('same', self.db.lopa_barrier_identity_status(imported)['state'])
        self.db.set_lopa_sensor_member_active(member['id'], True)
        result = self.db.lopa_source_calculation(source)
        self.assertTrue(result['complete'])
        self.assertEqual(1, next(row for row in result['categories']
                                 if row['hazop_consequence_id'] == consequence)['barrier_rrf'])

    def test_old_project_preserves_original_primary_sensor_on_migration(self):
        _cause, sensor, _barrier, equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.conn.execute('DELETE FROM safeguard_equipment_links WHERE safeguard_id=?', (sensor,))
        self.db.commit()
        self.db.add_safeguard_equipment_link(sensor, equipment, 'LL')
        self.db.conn.execute('ALTER TABLE lopa_source_scenarios DROP COLUMN origin_equipment_snapshot_json')
        self.db.conn.execute('ALTER TABLE lopa_source_scenarios DROP COLUMN origin_equipment_snapshot_legacy')
        self.db.conn.commit()
        self.db.conn.close()
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))
        self.assertEqual('changed', self.db.lopa_source_origin_identity_status(source)['state'])
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])

    def test_reconstructed_old_origin_identity_can_be_reviewed_and_undone(self):
        _cause, sensor, _barrier, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.conn.execute('ALTER TABLE lopa_source_scenarios DROP COLUMN origin_equipment_snapshot_json')
        self.db.conn.execute('ALTER TABLE lopa_source_scenarios DROP COLUMN origin_equipment_snapshot_legacy')
        self.db.conn.commit()
        self.db.conn.close()
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))
        status = self.db.lopa_source_origin_identity_status(source)
        self.assertEqual('current', status['state'])
        self.assertTrue(status['legacy'])
        self.db.refresh_lopa_source_origin_identity(source)
        self.assertFalse(self.db.lopa_source_origin_identity_status(source)['legacy'])
        self.assertTrue(self.db.undo())
        self.assertTrue(self.db.lopa_source_origin_identity_status(source)['legacy'])

    def test_removed_all_hazop_sensor_links_cannot_be_accepted_silently(self):
        _cause, sensor, _barrier, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.conn.execute('DELETE FROM safeguard_equipment_links WHERE safeguard_id=?', (sensor,))
        self.db.commit()
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        with self.assertRaisesRegex(ValueError, 'saknar giltiga objekt'):
            self.db.refresh_lopa_source_origin_identity(source)
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])

    def test_sensor_tag_rename_does_not_mistake_same_object_for_a_new_identity(self):
        _cause, sensor, _barrier, equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.update_equipment_item(equipment, 'LT-102', 'LT', 'Nivågivare', '')
        self.assertEqual('current', self.db.lopa_source_origin_identity_status(source)['state'])
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.assertEqual('LT-102', self.db.lopa_sources(created['revision_id'])[0]['equipment_tag'])


    def test_new_hazop_barrier_is_reported_and_imported_without_changing_existing_rows(self):
        _cause, sensor, original, equipment = self._hazop_chain()
        consequence = self.db.get_safeguard(sensor)['consequence_id']
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        previous = self.db.lopa_barriers(created['revision_id'], source)[0]
        duplicate = self.db.add_safeguard(consequence)
        self.db.update_safeguard(duplicate, description='Samma SIF igen', rrf=100, sg_type='SIS')
        self.db.add_safeguard_equipment_link(duplicate, equipment, 'HH')
        state = self.db.lopa_source_sync_state(source)
        self.assertEqual('changed', state['state'])
        self.assertIn('ny HAZOP-barriär', state['messages'])
        self.assertEqual(1, self.db.import_lopa_new_barriers(source))
        self.assertEqual(0, self.db.import_lopa_new_barriers(source))
        barriers = {row['source_safeguard_id']: row for row in self.db.lopa_barriers(created['revision_id'], source)}
        self.assertEqual(previous, barriers[original])
        self.assertEqual('same', self.db.lopa_barrier_identity_status(barriers[duplicate])['state'])
        self.assertEqual(10, self.db.lopa_source_calculation(source)['categories'][0]['barrier_rrf'])
        self.assertEqual('current', self.db.lopa_source_sync_state(source)['state'])
        self.assertTrue(self.db.undo())
        self.assertIn('ny HAZOP-barriär', self.db.lopa_source_sync_state(source)['messages'])
        self.assertTrue(self.db.redo())
        self.assertEqual('current', self.db.lopa_source_sync_state(source)['state'])

    def test_new_barrier_import_respects_hazop_category_exclusions(self):
        _cause, sensor, _original, _equipment = self._hazop_chain()
        consequence = self.db.get_safeguard(sensor)['consequence_id']
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        new_barrier = self.db.add_safeguard(consequence)
        self.db.update_safeguard(new_barrier, description='Gäller annan kategori', rrf=100, sg_type='Mekanisk')
        severity = self.db.get_consequence_severities(consequence)[0]
        self.db.set_severity_excluded_sgs(severity['id'], [new_barrier])
        self.assertEqual(1, self.db.import_lopa_new_barriers(source))
        imported = next(row for row in self.db.lopa_barriers(created['revision_id'], source)
                        if row['source_safeguard_id'] == new_barrier)
        self.assertEqual([], self.db.lopa_barrier_categories(imported['id']))
        self.assertEqual(10, self.db.lopa_source_calculation(source)['categories'][0]['barrier_rrf'])

    def test_matrix_change_is_detected_without_altering_revision_snapshot(self):
        created = self.db.create_lopa()
        revision = created['revision_id']
        snapshot = self.db.lopa_revision_matrix(revision)
        self.assertFalse(self.db.lopa_matrix_has_changed(revision))
        config = self.db.lopa_matrix_config()
        key = next(iter(config['category_settings']))
        config['category_settings'][key]['tel'][0] = 0.000001
        self.db.set_lopa_matrix_config(config)
        self.assertTrue(self.db.lopa_matrix_has_changed(revision))
        self.assertEqual(snapshot, self.db.lopa_revision_matrix(revision))
        self.assertTrue(self.db.undo())
        self.assertFalse(self.db.lopa_matrix_has_changed(revision))
        self.assertTrue(self.db.redo())
        self.assertTrue(self.db.lopa_matrix_has_changed(revision))

    def test_revision_can_adopt_current_matrix_as_one_undoable_change(self):
        _cause, sensor, _other, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)
        revision = created['revision_id']
        old = self.db.lopa_revision_matrix(revision)
        config = self.db.lopa_matrix_config()
        key = next(iter(config['category_settings']))
        config['category_settings'][key]['tel'][0] = 0.000001
        self.db.set_lopa_matrix_config(config)
        self.db.update_lopa_revision_matrix(revision)
        self.assertFalse(self.db.lopa_matrix_has_changed(revision))
        self.assertEqual(0.000001, self.db.lopa_revision_matrix(revision)['lopa']['category_settings'][key]['tel'][0])
        self.assertTrue(self.db.undo())
        self.assertEqual(old, self.db.lopa_revision_matrix(revision))
        self.assertTrue(self.db.redo())
        self.assertFalse(self.db.lopa_matrix_has_changed(revision))
        self.db.lock_lopa_revision(revision)
        with self.assertRaises(PermissionError):
            self.db.update_lopa_revision_matrix(revision)

    def test_matrix_update_refuses_removed_used_category(self):
        _cause, sensor, _other, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        assessment = self.db.lopa_source_consequences(source)[0]
        old = self.db.lopa_revision_matrix(created['revision_id'])
        matrix = self.db._lopa_matrix_snapshot()
        matrix['consequence_categories'] = [c for c in matrix['consequence_categories']
                                            if c['key'] != assessment['category_key']]
        self.db.set_risk_matrix(matrix)
        count = self.db.undo_count
        with self.assertRaisesRegex(ValueError, 'kategorin'):
            self.db.update_lopa_revision_matrix(created['revision_id'])
        self.assertEqual(old, self.db.lopa_revision_matrix(created['revision_id']))
        self.assertEqual(count, self.db.undo_count)

    def test_matrix_update_refuses_to_discard_saved_factor_values(self):
        _cause, sensor, _other, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        key = self.db.lopa_source_consequences(source)[0]['category_key']
        config = self.db.lopa_matrix_config()
        factor = config['category_settings'][key]['escalation_factors'][0]['key']
        self.db.set_lopa_escalation_values(source, key, {factor: 10})
        config['category_settings'][key]['escalation_factors'] = [
            item for item in config['category_settings'][key]['escalation_factors'] if item['key'] != factor]
        self.db.set_lopa_matrix_config(config)
        before = self.db.lopa_revision_matrix(created['revision_id'])
        with self.assertRaisesRegex(ValueError, 'eskaleringsfaktorer'):
            self.db.update_lopa_revision_matrix(created['revision_id'])
        self.assertEqual(before, self.db.lopa_revision_matrix(created['revision_id']))

    def test_matrix_update_refuses_changed_severity_semantics(self):
        _cause, sensor, _other, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)
        before = self.db.lopa_revision_matrix(created['revision_id'])
        matrix = self.db._lopa_matrix_snapshot()
        axis = 'y' if matrix.get('x_axis', 'frequency') == 'frequency' else 'x'
        matrix[f'{axis}_labels'][2] = 'Annan innebörd'
        self.db.set_risk_matrix(matrix)
        with self.assertRaisesRegex(ValueError, 'Konsekvensskalan'):
            self.db.update_lopa_revision_matrix(created['revision_id'])
        self.assertEqual(before, self.db.lopa_revision_matrix(created['revision_id']))

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

    def test_imported_barrier_stays_with_its_hazop_consequence(self):
        cause, sensor, _barrier, _equipment = self._hazop_chain()
        first = self.db.get_safeguard(sensor)['consequence_id']
        second = self.db.add_consequence(cause)
        self.db.update_consequence(second, 'Annan konsekvens', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(second, category['id'], 3)
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        before = self.db.lopa_source_calculation(source)
        rows = {row['hazop_consequence_id']: row for row in before['categories']}
        self.assertEqual(10, rows[first]['barrier_rrf'])
        self.assertEqual(1, rows[second]['barrier_rrf'])
        self.assertEqual(rows[second]['required_rrf'], before['required_rrf'])
        self.db.lock_lopa_revision(created['revision_id'])
        self.db.delete_consequence(first)
        after = self.db.lopa_source_calculation(source)
        rows_after = {row['hazop_consequence_id']: row for row in after['categories']}
        self.assertEqual(10, rows_after[first]['barrier_rrf'])
        self.assertEqual(1, rows_after[second]['barrier_rrf'])
        self.assertEqual(before['required_rrf'], after['required_rrf'])
        self.assertEqual(before, after)

    def test_manual_barrier_can_apply_to_both_consequences(self):
        cause, sensor, _barrier, _equipment = self._hazop_chain()
        first = self.db.get_safeguard(sensor)['consequence_id']
        second = self.db.add_consequence(cause)
        self.db.update_consequence(second, 'Annan konsekvens', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(second, category['id'], 3)
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        barrier_id = self.db.add_lopa_barrier(created['revision_id'], source, 'Gemensam barriär', 2)
        barriers = self.db.lopa_barriers(created['revision_id'], source)
        manual = next(row for row in barriers if row['id'] == barrier_id)
        self.assertEqual('Alla konsekvenser i källscenariot', self.db.lopa_barrier_scope_label(manual))
        rows = {row['hazop_consequence_id']: row
                for row in self.db.lopa_source_calculation(source)['categories']}
        self.assertEqual(20, rows[first]['barrier_rrf'])
        self.assertEqual(2, rows[second]['barrier_rrf'])

    def test_unknown_imported_barrier_scope_never_produces_sil(self):
        _cause, sensor, _barrier, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.conn.execute('UPDATE lopa_barriers SET hazop_consequence_snapshot_id=NULL WHERE source_id=?', (source,))
        self.db.commit()
        result = self.db.lopa_source_calculation(source)
        self.assertFalse(result['complete'])
        self.assertIsNone(result['sil'])
        self.assertTrue(any('koppling till konsekvens' in message for message in result['messages']))
        self.assertTrue(all(row['barrier_rrf'] is None and row['remaining_frequency'] is None
                            for row in result['categories']))
        with self.assertRaises(ValueError):
            self.db.lock_lopa_revision(created['revision_id'])

    def test_locked_legacy_revision_with_unknown_barrier_scope_is_incomplete(self):
        _cause, sensor, _barrier, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.lock_lopa_revision(created['revision_id'])
        # Simulate an older locked project whose barrier source was already
        # removed before the migration could capture its consequence.
        self.db.conn.execute('UPDATE lopa_barriers SET hazop_consequence_snapshot_id=NULL WHERE source_id=?', (source,))
        self.db.conn.commit()
        result = self.db.lopa_source_calculation(source)
        self.assertFalse(result['complete'])
        self.assertIsNone(result['sil'])
        self.assertTrue(any('koppling till konsekvens' in message for message in result['messages']))
        self.assertTrue(all(row['barrier_rrf'] is None and row['remaining_frequency'] is None
                            for row in result['categories']))

    def test_old_project_migration_captures_barrier_and_consequence_links(self):
        cause, sensor, _barrier, _equipment = self._hazop_chain()
        first = self.db.get_safeguard(sensor)['consequence_id']
        second = self.db.add_consequence(cause)
        self.db.update_consequence(second, 'Annan konsekvens', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(second, category['id'], 3)
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.conn.execute('ALTER TABLE lopa_barriers DROP COLUMN hazop_consequence_snapshot_id')
        self.db.conn.execute('ALTER TABLE lopa_source_consequences DROP COLUMN hazop_consequence_snapshot_id')
        self.db.conn.commit()
        self.db.conn.close()
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))
        rows = {row['hazop_consequence_id']: row
                for row in self.db.lopa_source_calculation(source)['categories']}
        self.assertEqual(10, rows[first]['barrier_rrf'])
        self.assertEqual(1, rows[second]['barrier_rrf'])
        barrier = self.db.lopa_barriers(created['revision_id'], source)[0]
        self.assertEqual('Konsekvens: Utsläpp', self.db.lopa_barrier_scope_label(barrier))

    def test_old_locked_project_without_barrier_origin_is_not_reconstructed_by_guessing(self):
        _cause, sensor, barrier, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.lock_lopa_revision(created['revision_id'])
        self.db.delete_safeguard(barrier)
        self.db.conn.execute('ALTER TABLE lopa_barriers DROP COLUMN hazop_consequence_snapshot_id')
        self.db.conn.execute('ALTER TABLE lopa_source_consequences DROP COLUMN hazop_consequence_snapshot_id')
        self.db.conn.commit()
        self.db.conn.close()
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))
        result = self.db.lopa_source_calculation(source)
        self.assertFalse(result['complete'])
        self.assertIsNone(result['sil'])
        self.assertTrue(any('koppling till konsekvens' in message for message in result['messages']))

    def test_old_locked_group_snapshot_retains_scope_after_source_deletion(self):
        cause, sensor, _barrier, _equipment = self._hazop_chain()
        first = self.db.get_safeguard(sensor)['consequence_id']
        second = self.db.add_consequence(cause)
        self.db.update_consequence(second, 'Annan konsekvens', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(second, category['id'], 3)
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.lock_lopa_revision(created['revision_id'])
        before = self.db.lopa_source_calculation(source)
        row = self.db.conn.execute('SELECT consequence_groups_snapshot_json FROM lopa_source_scenarios WHERE id=?',
                                   (source,)).fetchone()
        groups = json.loads(row[0])
        for group in groups:
            for assessment in group.get('assessments', []):
                assessment.pop('hazop_consequence_snapshot_id', None)
        self.db.conn.execute('UPDATE lopa_source_scenarios SET consequence_groups_snapshot_json=? WHERE id=?',
                             (json.dumps(groups), source))
        self.db.conn.execute('ALTER TABLE lopa_barriers DROP COLUMN hazop_consequence_snapshot_id')
        self.db.conn.execute('ALTER TABLE lopa_source_consequences DROP COLUMN hazop_consequence_snapshot_id')
        self.db.conn.commit()
        self.db.conn.close()
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))
        self.db.delete_consequence(first)
        self.assertEqual(before, self.db.lopa_source_calculation(source))

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
        self.assertFalse(calculation['complete'])
        self.assertTrue(any('Oklassificerad följd' in message for message in calculation['messages']))
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

    def test_unassessed_consequence_requires_a_revisioned_decision(self):
        cause, sensor, _barrier, _equipment = self._hazop_chain()
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Obedömt utsläpp', 0, '')
        created = self.db.create_lopa()
        revision = created['revision_id']
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        with self.assertRaisesRegex(ValueError, 'konsekvens'):
            self.db.lock_lopa_revision(revision)
        with self.assertRaises(ValueError):
            self.db.set_lopa_unassessed_exclusion(source, consequence, ' ')
        self.db.set_lopa_unassessed_exclusion(source, consequence, 'Utanför avgränsningen')
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(self.db.undo())
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(self.db.redo())
        self.db.lock_lopa_revision(revision)
        before = self.db.lopa_source_calculation(source)
        new_revision = self.db.create_lopa_revision(created['lopa_id'], revision)
        new_source = self.db.lopa_sources(new_revision)[0]['id']
        self.assertTrue(self.db.lopa_source_calculation(new_source)['complete'])
        self.db.delete_consequence(consequence)
        self.assertEqual(before, self.db.lopa_source_calculation(source))
        groups = self.db.lopa_source_consequence_groups(source)
        self.assertEqual('Utanför avgränsningen', groups[-1]['unassessed_exclusion']['reason'])

    def test_new_hazop_assessment_reopens_exclusion_and_can_be_imported(self):
        cause, sensor, _barrier, _equipment = self._hazop_chain()
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Tidigare obedömd', 0, '')
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.set_lopa_unassessed_exclusion(source, consequence, 'Avgränsning')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(consequence, category['id'], 3)
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.db.import_lopa_unassessed_consequence(source, consequence)
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(any(row['hazop_consequence_id'] == consequence
                            for row in self.db.lopa_source_consequences(source)))
        self.assertTrue(self.db.undo())
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(self.db.redo())
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])

    def test_changed_unassessed_consequence_requires_new_exclusion_decision(self):
        cause, sensor, _barrier, _equipment = self._hazop_chain()
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Ursprunglig konsekvens', 0, '')
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.set_lopa_unassessed_exclusion(source, consequence, 'Utanför analysen')
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])
        self.db.update_consequence(consequence, 'Ändrad konsekvens', 0, '')
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.db.set_lopa_unassessed_exclusion(source, consequence, 'Ny avgränsning')
        self.assertTrue(self.db.lopa_source_calculation(source)['complete'])

    def test_importing_new_consequence_adds_its_barriers_without_changing_existing_rows(self):
        cause, sensor, original_barrier, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        existing = self.db.lopa_barriers(created['revision_id'], source)[0]
        self.db.set_lopa_barrier_category_keys(existing['id'], [])
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Senare konsekvens', 3, '')
        self.db.set_consequence_severity(consequence, self.db.consequence_categories()[0]['id'], 3)
        new_barrier = self.db.add_safeguard(consequence)
        self.db.update_safeguard(new_barrier, description='Nytt skydd', rrf=10, sg_type='Mekanisk')
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.db.import_lopa_unassessed_consequence(source, consequence)
        barriers = {row['source_safeguard_id']: row for row in self.db.lopa_barriers(created['revision_id'], source)}
        self.assertEqual([], self.db.lopa_barrier_categories(barriers[original_barrier]['id']))
        self.assertEqual(consequence, barriers[new_barrier]['hazop_consequence_snapshot_id'])
        result = self.db.lopa_source_calculation(source)
        self.assertTrue(result['complete'])
        self.assertEqual(10, next(row for row in result['categories']
                                  if row['hazop_consequence_id'] == consequence)['barrier_rrf'])
        self.assertTrue(self.db.undo())
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.assertNotIn(new_barrier, [row['source_safeguard_id']
                                     for row in self.db.lopa_barriers(created['revision_id'], source)])

    def test_old_project_migration_does_not_assume_unassessed_consequence_was_excluded(self):
        cause, sensor, _barrier, _equipment = self._hazop_chain()
        consequence = self.db.add_consequence(cause)
        self.db.update_consequence(consequence, 'Obedömd sedan tidigare', 0, '')
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.db.conn.execute('ALTER TABLE lopa_source_scenarios DROP COLUMN unassessed_exclusions_json')
        self.db.conn.commit()
        self.db.conn.close()
        self.db = Database(path=os.path.join(self._tmpdir, 'project.db'))
        self.assertFalse(self.db.lopa_source_calculation(source)['complete'])
        self.assertTrue(any('Obedömd sedan tidigare' in message
                            for message in self.db.lopa_source_calculation(source)['messages']))

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

    def test_voting_rejects_impossible_m_out_of_n_values(self):
        created = self.db.create_lopa()
        for value in ('0oo1', '1oo0', '3oo2', '0oo0', 'otydligt'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.db.add_lopa_sensor_group(created['revision_id'], value)
                with self.assertRaises(ValueError):
                    self.db.add_lopa_final_group(created['revision_id'], value)

    def test_lock_requires_confirmed_sensor_voting_for_active_members(self):
        _cause, sensor, _other, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)
        second = self.db.add_equipment_item('LT-202', 'LT-202', 'LT', 0, 'Givare', '', 0)
        group = self.db.lopa_sensor_groups(created['revision_id'])[0]
        self.db.add_lopa_sensor_member(created['revision_id'], second, 'HH', group_id=group['id'])
        with self.assertRaisesRegex(ValueError, 'voting'):
            self.db.lock_lopa_revision(created['revision_id'])
        self.db.set_lopa_sensor_group_voting(group['id'], '1oo3')
        self.assertIn('stämmer inte med 2 aktiva rader', self.db.lopa_voting_issues(created['revision_id'])[0])
        with self.assertRaisesRegex(ValueError, 'voting'):
            self.db.lock_lopa_revision(created['revision_id'])
        self.db.set_lopa_sensor_group_voting(group['id'], '1oo2')
        self.db.lock_lopa_revision(created['revision_id'])

    def test_final_voting_must_be_reconfirmed_after_active_members_change(self):
        _cause, sensor, _other, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)
        group = self.db.add_lopa_final_group(created['revision_id'])
        self.db.add_lopa_final_member(created['revision_id'], name='Ventil A', group_id=group)
        second = self.db.add_lopa_final_member(created['revision_id'], name='Ventil B', group_id=group)
        with self.assertRaisesRegex(ValueError, 'voting'):
            self.db.lock_lopa_revision(created['revision_id'])
        self.db.set_lopa_final_group_voting(group, '1oo2')
        self.assertEqual([], self.db.lopa_voting_issues(created['revision_id']))
        self.db.update_lopa_final_member(second, active=False)
        self.assertIn('stämmer inte med 1 aktiv rad', self.db.lopa_voting_issues(created['revision_id'])[0])
        self.db.set_lopa_final_group_voting(group, '1oo1')
        self.assertEqual([], self.db.lopa_voting_issues(created['revision_id']))
        self.db.update_lopa_final_member(second, active=True)
        self.assertTrue(self.db.lopa_voting_issues(created['revision_id']))
        self.assertTrue(self.db.undo())
        self.assertEqual([], self.db.lopa_voting_issues(created['revision_id']))
        self.assertTrue(self.db.redo())
        self.db.set_lopa_final_group_voting(group, '1oo2')
        self.db.lock_lopa_revision(created['revision_id'])

    def test_first_final_member_and_group_are_one_undoable_action(self):
        created = self.db.create_lopa()
        before = self.db.undo_count
        member = self.db.add_lopa_final_member(created['revision_id'], name='Ventil')
        groups = self.db.lopa_final_groups(created['revision_id'])
        self.assertEqual(member, self.db.lopa_final_members(groups[0]['id'])[0]['id'])
        self.assertEqual(before + 1, self.db.undo_count)
        self.assertTrue(self.db.undo())
        self.assertEqual([], self.db.lopa_final_groups(created['revision_id']))
        self.assertTrue(self.db.redo())
        self.assertEqual(1, len(self.db.lopa_final_groups(created['revision_id'])))

    def test_legacy_invalid_voting_remains_visible_and_blocks_locking(self):
        created = self.db.create_lopa()
        group = self.db.add_lopa_final_group(created['revision_id'])
        self.db.add_lopa_final_member(created['revision_id'], name='Ventil', group_id=group)
        self.db.conn.execute("UPDATE lopa_final_groups SET voting='3oo1',needs_voting_review=0 WHERE id=?", (group,))
        self.db.conn.commit()
        self.assertIn('ogiltig voting', self.db.lopa_voting_issues(created['revision_id'])[0])
        with self.assertRaisesRegex(ValueError, 'voting'):
            self.db.lock_lopa_revision(created['revision_id'])

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

    def test_deleted_barrier_for_excluded_consequence_does_not_block_other_consequence(self):
        cause, sensor, barrier, _equipment = self._hazop_chain()
        first = self.db.get_safeguard(sensor)['consequence_id']
        second = self.db.add_consequence(cause)
        self.db.update_consequence(second, 'Annan konsekvens', 3, '')
        category = self.db.consequence_categories()[0]
        self.db.set_consequence_severity(second, category['id'], 3)
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        first_assessment = next(row for row in self.db.lopa_source_consequences(source)
                                if row['hazop_consequence_id'] == first)
        self.db.set_lopa_consequence_active(first_assessment['id'], False)
        self.db.delete_safeguard(barrier)
        result = self.db.lopa_source_calculation(source)
        self.assertTrue(result['complete'])
        self.assertEqual(second, result['categories'][result['governing_category_index']]['hazop_consequence_id'])
        self.assertEqual(1, result['categories'][result['governing_category_index']]['barrier_rrf'])

    def test_deleted_barrier_with_no_active_categories_does_not_block_result(self):
        _cause, sensor, barrier, _equipment = self._hazop_chain()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        imported = self.db.lopa_barriers(created['revision_id'], source)[0]
        self.db.set_lopa_barrier_category_keys(imported['id'], [])
        self.db.delete_safeguard(barrier)
        result = self.db.lopa_source_calculation(source)
        self.assertTrue(result['complete'])
        self.assertEqual(1, result['categories'][result['governing_category_index']]['barrier_rrf'])

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

    def test_sync_detects_changed_and_new_hazop_consequence_assessments(self):
        _cause, sensor, _other, _equipment = self._hazop_chain()
        consequence = self.db.get_safeguard(sensor)['consequence_id']
        categories = self.db.consequence_categories()
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.assertEqual('current', self.db.lopa_source_sync_state(source)['state'])
        self.db.set_consequence_severity(consequence, categories[0]['id'], 2)
        state = self.db.lopa_source_sync_state(source)
        self.assertEqual('changed', state['state'])
        self.assertIn('konsekvensbedömning', state['messages'])
        stored = self.db.lopa_source_consequences(source)[0]
        self.assertEqual(3, stored['severity'])
        self.assertTrue(self.db.undo())
        self.assertEqual('current', self.db.lopa_source_sync_state(source)['state'])
        self.assertTrue(self.db.redo())
        self.assertEqual('changed', self.db.lopa_source_sync_state(source)['state'])
        self.db.set_consequence_severity(consequence, categories[1]['id'], 1)
        self.assertIn('ny konsekvensbedömning', self.db.lopa_source_sync_state(source)['messages'])

    def test_sync_detects_material_changes_in_very_small_frequencies(self):
        cause, sensor, _other, _equipment = self._hazop_chain()
        self.db.update_cause(cause, base_frequency=1e-14, frequency_cleared=False)
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor)['source_id']
        self.assertEqual('current', self.db.lopa_source_sync_state(source)['state'])
        self.db.update_cause(cause, base_frequency=2e-14)
        self.assertIn('grundfrekvens', self.db.lopa_source_sync_state(source)['messages'])

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
        self.db.set_lopa_unassessed_exclusion(source, extra, 'Utanför denna analys')
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
