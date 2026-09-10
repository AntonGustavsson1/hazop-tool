"""Regression coverage for the reusable ProSa IPS risk-matrix presets."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

TEST_DIR = Path(__file__).resolve().parent
HAZOP_DIR = TEST_DIR.parent
for path in (HAZOP_DIR, TEST_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from PyQt6.QtWidgets import QApplication, QPushButton

from database import Database
from hazop import HAZOPPreparationPanel
from hazop_preparation_panel import IPS_RISK_MATRIX_PRESET, IPS5_RISK_MATRIX_PRESET


class IPSRiskMatrixPresetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix='hazop_ips_preset_')
        self.db = Database(path=os.path.join(self.tempdir, 'project.db'))

    def tearDown(self):
        self.db.conn.close()
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def test_ips5_is_a_complete_standard_template_and_loads_from_its_button(self):
        self.assertEqual(IPS_RISK_MATRIX_PRESET['rows'], 5)
        self.assertEqual(IPS5_RISK_MATRIX_PRESET['cols'], 7)
        self.assertEqual(
            [item['label'] for item in IPS5_RISK_MATRIX_PRESET['risk_level_definitions']],
            ['Låg', 'Medium', 'Hög'])
        self.assertTrue(all(item['definition'] for item in
                            IPS5_RISK_MATRIX_PRESET['risk_level_definitions']))
        self.assertEqual(
            [item['name'] for item in IPS5_RISK_MATRIX_PRESET['consequence_categories']],
            ['Person', 'Miljö', 'Ekonomi', 'Anläggning', 'Rykte'])

        panel = HAZOPPreparationPanel(self.db)
        try:
            buttons = {button.text(): button for button in panel.findChildren(QPushButton)}
            self.assertIn('IPS', buttons)
            self.assertIn('IPS5', buttons)
            buttons['IPS5'].click()
            self.assertEqual(panel._rows_spin.value(), 5)
            self.assertEqual(panel._cols_spin.value(), 7)
            self.assertEqual(panel._last_built_cfg['cell_labels'][1][6], 'Hög')
            self.assertEqual(
                [item['label'] for item in panel._last_built_cfg['risk_level_definitions']],
                ['Låg', 'Medium', 'Hög'])
            self.assertIn('lopa', panel._last_built_cfg)
            self.assertEqual(
                [item['name'] for item in panel._last_built_cfg['consequence_categories']],
                ['Person', 'Miljö', 'Ekonomi', 'Anläggning', 'Rykte'])
        finally:
            panel.deleteLater()


if __name__ == '__main__':
    unittest.main()
