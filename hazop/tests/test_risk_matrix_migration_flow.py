"""Regression coverage for reviewed matrix migrations and rendered links."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import copy
import unittest
from PyQt6.QtWidgets import QApplication
from tests import test_database as fixtures
from hazop_preparation_panel import RiskMatrixMigrationDialog


class MigrationFlowTests(unittest.TestCase):
    setUp = fixtures.RiskMatrixTemplateMigrationTests.setUp
    tearDown = fixtures.RiskMatrixTemplateMigrationTests.tearDown

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def dialog(self):
        dialog = RiskMatrixMigrationDialog(
            self.db, self.db.get_risk_matrix(), self.target, 'Ny mall')
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_category_links_are_actually_painted_and_follow_global(self):
        dialog = self.dialog()
        dialog.set_axis_mapping('severity', 2, 4)
        dialog.show()
        dialog._show_axis_page()
        for category in dialog.plan['source_categories']:
            sid = str(category['source_id'])
            dialog._select_category_scope(sid)
            self.app.processEvents()
            self.assertTrue(dialog._global_mapping_toolbar.isHidden())
            field = dialog._category_scope_fields[sid]
            drawn = []
            original = field._draw_curve
            def draw(painter, start, end, **kwargs):
                drawn.append((start, end))
                original(painter, start, end, **kwargs)
            field._draw_curve = draw
            field.grab()
            field._draw_curve = original
            self.assertEqual(3, len(drawn))
            self.assertIn((('severity', 2), 4), field.iter_mappings())
        dialog.close()

    def test_category_override_survives_global_edits_in_actual_database(self):
        dialog = self.dialog()
        sid = str(self.category_id)
        dialog.set_category_severity_mapping(sid, 3, 2)
        dialog.set_axis_mapping('severity', 3, 1)
        record = next(r for r in dialog.plan['severity_records'] if r['kind'] == 'category')
        self.assertEqual(2, record['target'])
        dialog.suggest_automatically()
        self.assertEqual(2, record['target'])
        self.db.apply_risk_matrix_migration(dialog.plan)
        self.assertEqual(2, self.db.get_consequence_severities(self.cons_id)[0]['severity'])

    def test_live_categories_are_normalised_consistently_without_false_conflict(self):
        # Real templates can use keys different from project category names.
        cfg = self.db.get_risk_matrix()
        cfg['consequence_categories'][0]['key'] = 'people'
        self.db.set_risk_matrix(cfg)
        plan = self.db.risk_matrix_migration_preview(self.db.get_risk_matrix(), self.target)
        self.db.apply_risk_matrix_migration(plan)
        self.assertEqual(4, self.db.get_risk_matrix()['rows'])

    def test_real_matrix_change_still_rejects_and_preserves_assessments(self):
        dialog = self.dialog()
        cfg = self.db.get_risk_matrix()
        cfg['freq_boundaries'] = [0.02, 2.0]
        self.db.set_risk_matrix(cfg)
        with self.assertRaisesRegex(RuntimeError, 'Riskmatrisen .*ndrades'):
            self.db.apply_risk_matrix_migration(dialog.plan)
        self.assertEqual(3, self.db.get_consequence_severities(self.cons_id)[0]['severity'])

    def test_template_definition_resolves_collapsed_old_descriptions(self):
        self.db.set_severity_definition(1, self.category_id, 'Liten skada')
        target = copy.deepcopy(self.target)
        target['consequence_categories'] = self.db._project_category_template(4)
        target['consequence_categories'][0]['descriptions'] = ['Ny 1', 'Ny 2', 'Ny 3', 'Ny 4']
        plan = self.db.risk_matrix_migration_preview(self.db.get_risk_matrix(), target)
        for row in plan['definition_records']:
            row['target'] = 1
        self.db.apply_risk_matrix_migration(plan)
        self.assertEqual('Ny 1', self.db.get_severity_definitions()[1][self.category_id])

    def test_category_relink_refreshes_destination_chips(self):
        dialog = self.dialog()
        sid = str(self.category_id)
        key = dialog.plan['target_categories'][1]['key']
        dialog.plan['target_categories'][1]['descriptions'] = ['Environment level'] * 4
        dialog.set_category_mapping(sid, key)
        self.assertIn('Environment level', dialog._category_scope_fields[sid].target_chips[('severity', 1)].text())
