"""Consequence scope and numeric regression cases for LOPA."""
import unittest
from tests import test_lopa_models as fixtures
from tests.lopa_scope_fixtures import protect_with_same_function
from lopa_models import calculate_lopa, normalise_lopa_config

class LopaScopeMathTests(unittest.TestCase):
    setUp = fixtures.LopaDatabaseTests.setUp
    tearDown = fixtures.LopaDatabaseTests.tearDown
    _hazop_chain = fixtures.LopaDatabaseTests._hazop_chain

    def test_unrelated_siblings_do_not_enter_import_view_calculation_or_sync(self):
        cause,sensor,barrier,equipment = self._hazop_chain()
        unrelated = self.db.add_consequence(cause)
        self.db.update_consequence(unrelated, 'Unrelated high consequence', 5, '')
        self.db.set_consequence_severity(unrelated, self.db.consequence_categories()[0]['id'], 5)
        other = self.db.add_safeguard(unrelated)
        self.db.add_safeguard_equipment_link(other,equipment,'LL')
        unassessed = self.db.add_consequence(cause)
        created = self.db.create_lopa()
        source = self.db.add_lopa_source_from_safeguard(created['lopa_id'],sensor)['source_id']
        origin = self.db.get_safeguard(sensor)['consequence_id']
        self.assertEqual([origin], [g['hazop_consequence_id'] for g in self.db.lopa_source_consequence_groups(source)])
        self.assertEqual([origin], [r['hazop_consequence_id'] for r in self.db.lopa_source_consequences(source)])
        result = self.db.lopa_source_calculation(source)
        self.assertTrue(result['complete'])
        self.assertAlmostEqual(0.01,result['categories'][0]['remaining_frequency'])
        self.assertEqual('current',self.db.lopa_source_sync_state(source)['state'])
        self.assertNotIn(other,[r['source_safeguard_id'] for r in self.db.lopa_barriers(created['revision_id'],source)])
        with self.assertRaises(ValueError):
            self.db.import_lopa_unassessed_consequence(source,unassessed)

    def test_same_function_on_sibling_is_included_but_never_credited_as_ipl(self):
        cause,sensor,barrier,equipment = self._hazop_chain()
        sibling=self.db.add_consequence(cause)
        self.db.set_consequence_severity(sibling,self.db.consequence_categories()[0]['id'],3)
        sibling_sensor=protect_with_same_function(self.db,sensor,sibling)
        created=self.db.create_lopa()
        source=self.db.add_lopa_source_from_safeguard(created['lopa_id'],sensor)['source_id']
        result=self.db.lopa_source_calculation(source)
        row=next(r for r in result['categories'] if r['hazop_consequence_id']==sibling)
        self.assertEqual(1,row['barrier_rrf'])
        self.assertAlmostEqual(0.1,row['remaining_frequency'])
        duplicate=self.db.add_lopa_source_from_safeguard(created['lopa_id'],sibling_sensor)
        self.assertFalse(duplicate['created'])
        self.assertEqual(source,duplicate['source_id'])

    def test_legacy_unrelated_assessment_is_retained_in_storage_but_not_counted(self):
        cause,sensor,barrier,equipment=self._hazop_chain()
        created=self.db.create_lopa()
        source=self.db.add_lopa_source_from_safeguard(created['lopa_id'],sensor)['source_id']
        sibling=self.db.add_consequence(cause)
        self.db.conn.execute("INSERT INTO lopa_source_consequences(source_id,hazop_consequence_id,hazop_category_id,category_key,category_name,severity,description,hazop_consequence_snapshot_id) SELECT source_id,?,hazop_category_id,category_key,category_name,5,'Legacy sibling',? FROM lopa_source_consequences WHERE source_id=?",(sibling,sibling,source))
        self.db.commit()
        self.assertEqual(2,self.db.conn.execute('SELECT COUNT(*) FROM lopa_source_consequences WHERE source_id=?',(source,)).fetchone()[0])
        self.assertEqual(1,len(self.db.lopa_source_calculation(source)['categories']))

    def test_percent_validation_rejects_invalid_values_without_writing(self):
        created=self.db.create_lopa()
        source=self.db.add_lopa_local_source(created['revision_id'],frequency=0.1)
        for value in (-1,101,float('nan'),float('inf')):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.db.update_lopa_source_analysis_details(source,assumption_percent=value)
                with self.assertRaises(ValueError):
                    self.db.set_lopa_escalation_values(source,'person',{'narvaro':value})

    def test_invalid_or_overflowing_rrf_never_displays_zero_accident_frequency(self):
        matrix={'rows':1,'consequence_categories':[{'key':'person','name':'Person'}]}
        for barriers in ([{'rrf':0}],[{'rrf':1e308},{'rrf':1e308}]):
            result=calculate_lopa({'matrix':matrix,'base_frequency':0.1,'categories':[{'category_key':'person','severity':1}],'barriers':barriers},normalise_lopa_config({},matrix))
            self.assertFalse(result['complete'])
            self.assertIsNone(result['categories'][0]['accident_frequency'])
            self.assertIsNone(result['categories'][0]['barrier_rrf'])

    def test_independent_percentages_and_rrfs_have_traceable_numeric_result(self):
        matrix={'rows':1,'consequence_categories':[{'key':'person','name':'Person'}]}
        config=normalise_lopa_config({'category_settings':{'person':{'tel':[1e-6]}}},matrix)
        result=calculate_lopa({'matrix':matrix,'base_frequency':0.2,'assumption_percent':25,'barriers':[{'rrf':10},{'rrf':5}], 'categories':[{'category_key':'person','severity':1,'factors':{'antandning':20,'narvaro':50,'skada':10}}]},config)
        row=result['categories'][0]
        self.assertAlmostEqual(0.05,result['effective_frequency'])
        self.assertEqual(50,row['barrier_rrf'])
        self.assertAlmostEqual(0.001,row['remaining_frequency'])
        self.assertAlmostEqual(0.00001,row['accident_frequency'])
        self.assertAlmostEqual(10,row['required_rrf'])
