#!/usr/bin/env python3
"""Fast smoke-test suite — NOT a replacement for the full regression suite
(818 tests, ~4-5 minutes, split across 14 per-module test_*.py files since
2026-08-20 — see CLAUDE.md's Testing section). Meant to run after every
code change during iterative development (2026-08-18, see NOTES.md
"Snabbare testcykel" — Anton: "Jag skulle vilja begränsa regression test
till kanske 10 test efter varje build. Med möjlighet att full regression
test på begäran.").

Every real crash found during the 2026-08-17/18 module-split session
(_StylePopup, ConsCategoryMatrixPopup, missing equipment_detection OCR
helpers, pathlib.Path in PIDManagementPanel.refresh()) only triggered
against REAL DATA (a revision row with pdf_path set) or a REAL
INTERACTION (clicking a node-markup tool button) — plain construction
against an empty DB would have missed every one of them. So this suite
seeds a small but realistic dataset and, where it matters, clicks the
actual buttons rather than just constructing widgets.

Run this constantly during development (from the hazop/ directory):
    python -m unittest tests.test_smoke -v

Run one module's tests when a change is confined to it (seconds, not
minutes), e.g.:
    python -m unittest tests.test_scenario_panel

Run the full, slow suite for large/risky changes, or whenever you want
real confidence rather than a quick sanity check:
    python -m unittest tests.test_database tests.test_scenario_panel tests.test_pid_viewer tests.test_pid_panel_mod tests.test_pid_graphics_view tests.test_tree_panel tests.test_equipment_panel tests.test_equipment_detection tests.test_settings_panels tests.test_worksheet tests.test_node_markup tests.test_hazop tests.test_ui_helpers tests.test_integration
"""
import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

# All test_*.py files live under hazop/tests/ (2026-08-21, see NOTES.md
# "Flytta alla test_*.py till en egen tests/-mapp") -- hazop.py/scenario_panel.py/
# etc. are large standalone scripts (not a package) one directory up, so
# that directory must be on sys.path for their imports to resolve
# regardless of the current working directory tests are launched from.
_TEST_DIR = Path(__file__).resolve().parent
_HAZOP_DIR = _TEST_DIR.parent
for _p in (_HAZOP_DIR, _TEST_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QBoxLayout, QHeaderView


def _ensure_qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


class SmokeTests(unittest.TestCase):
    """One seeded DB shared across the whole class (setUpClass, not
    setUp) — seeding has a fixed cost and none of these checks mutate
    the DB in a way that would leak between them; they only construct
    widgets and click buttons."""

    @classmethod
    def setUpClass(cls):
        cls.app = _ensure_qapp()
        cls._tmpdir = tempfile.mkdtemp(prefix="hazop_smoke_test_")
        from database import Database
        cls.db = Database(path=os.path.join(cls._tmpdir, "smoke.db"))
        cls._seed(cls.db)

    @classmethod
    def tearDownClass(cls):
        try:
            del cls.db
        except Exception:
            pass
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    @staticmethod
    def _seed(db):
        """One of everything, including the specific shapes that have
        actually caused crashes before — a revision with pdf_path set
        (see PIDManagementPanel.refresh(), 2026-08-18)."""
        node_id = db.add_node()
        dev_id = db.deviations(node_id)[0]['id']
        cause_id = db.add_cause(dev_id)
        cons_id = db.add_consequence(cause_id)
        db.add_safeguard(cons_id)
        db.add_recommendation_to_consequence(cons_id, description='Testrekommendation')
        db.add_revision('A', '', pdf_path='C:/fake/smoke_test.pdf')
        db.add_equipment_item('SM-101', 'SM-101', 'SM', 0, 'Ventil', '', 0)

    # ── Every extracted module imports cleanly (catches NameError/
    # ImportError from a missed dependency at the module-load level) ──────
    def test_every_module_imports(self):
        import constants, design, database, ui_helpers, tree_panel, node_markup
        import worksheet, scenario_panel, equipment_panel, settings_panels
        import pid_viewer, pid_graphics_view, pid_panel_mod, hazop
        import equipment_detection, symbol_geometry, image_symbol_matching, lopa_models, lopa_panel
        import recommendations_panel

    # ── Every major panel constructs (and does its normal startup work)
    # against a REALISTIC, non-empty DB ──────────────────────────────────
    def test_tree_panel_constructs(self):
        from tree_panel import TreePanel
        p = TreePanel(self.db)
        try:
            p.refresh()
        finally:
            p.deleteLater()

    def test_scenario_panel_constructs(self):
        from scenario_panel import ScenarioTablePanel
        p = ScenarioTablePanel(self.db)
        try:
            p.load_all()
        finally:
            p.deleteLater()

    def test_equipment_panel_constructs(self):
        from equipment_panel import EquipmentPanel
        p = EquipmentPanel(self.db)
        try:
            p.refresh()
        finally:
            p.deleteLater()

    def test_recommendations_panel_constructs(self):
        """New page (2026-08-26, see NOTES.md) — construct against the
        seeded DB (which now includes a real linked recommendation, see
        _seed above) and load() it for real, not just __init__."""
        from recommendations_panel import RecommendationsPanel
        p = RecommendationsPanel(self.db)
        try:
            p.load()
        finally:
            p.deleteLater()

    def test_lopa_panel_constructs_and_creates_empty_revision(self):
        """LOPA is a top-level page, so smoke-test its real first workflow."""
        from lopa_panel import LopaPanel
        p = LopaPanel(self.db)
        try:
            created = self.db.create_lopa(sif_name='Smoke SIF')
            p._lopa_id = created['lopa_id']
            p._revision_id = created['revision_id']
            p.refresh()
            self.assertGreaterEqual(p._list.count(), 1)
            self.assertIn('LOPA', p._record_title.text())
        finally:
            p.deleteLater()

    def test_lopa_panel_renders_imported_detail_sections(self):
        """The detailed LOPA workspace must survive a real HAZOP import path."""
        from lopa_panel import LopaPanel
        node_id = self.db.add_node()
        dev_id = self.db.deviations(node_id)[0]['id']
        cause_id = self.db.add_cause(dev_id)
        self.db.update_cause(cause_id, description='LSHH test', base_frequency=0.2)
        cons_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(cons_id, 'Överfyllnad', 3, '')
        categories = self.db.consequence_categories()
        category = categories[0]
        second_category = categories[1]
        self.db.set_consequence_severity(cons_id, category['id'], 3)
        self.db.set_consequence_severity(cons_id, second_category['id'], 2)
        second_cons_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(second_cons_id, 'Utsläpp till mark', 2, '')
        self.db.set_consequence_severity(second_cons_id, category['id'], 2)
        self.db.set_consequence_severity(second_cons_id, second_category['id'], 4)
        unassessed_cons_id = self.db.add_consequence(cause_id)
        self.db.update_consequence(unassessed_cons_id, 'Oklassificerad följd', 0, '')
        sensor_sg = self.db.add_safeguard(cons_id)
        self.db.update_safeguard(sensor_sg, description='LSHH', rrf=100, sg_type='SIS')
        other_sg = self.db.add_safeguard(cons_id)
        self.db.update_safeguard(other_sg, description='PSV', rrf=10, sg_type='Mekanisk')
        equipment_id = self.db.add_equipment_item('LSHH-SMOKE', 'LSHH-SMOKE', 'LS', 0, 'Nivågivare', '', 0)
        self.db.add_safeguard_equipment_link(sensor_sg, equipment_id, 'HH')
        created = self.db.create_lopa(sif_name='Detaljerad smoke')
        imported = self.db.add_lopa_source_from_safeguard(created['lopa_id'], sensor_sg)
        self.db.update_lopa_source_analysis_details(
            imported['source_id'], control_frequency='Årligt provtest',
            assumption_percent=10, assumption_reason='Endast under uppstart')
        final_group = self.db.add_lopa_final_group(created['revision_id'], '1oo1')
        self.db.add_lopa_final_member(
            created['revision_id'], equipment_id=equipment_id,
            action_text='Stäng av flödet', group_id=final_group)
        self.db.update_lopa_revision_details(
            created['revision_id'], document_date='2026-09-02',
            additional_actions='Verifiera ventilen',
            additional_requirements='Definiera provtest', process_safety_time='10')
        self.db.add_lopa_comment(created['revision_id'], 'Smoke-kommentar', 'Testare')
        p = LopaPanel(self.db)
        try:
            p.activate_lopa(created['lopa_id'], created['revision_id'])
            # A populated LOPA is a working sheet, not a dashboard wider or
            # multiple screens taller than a normal desktop workspace.
            p.resize(1720, 1040)
            p.show()
            self.app.processEvents()
            self.app.processEvents()
            self.assertEqual(0, p._detail_scroll.horizontalScrollBar().maximum())
            self.assertLessEqual(p._detail_scroll.verticalScrollBar().maximum(), 220)
            self.assertEqual(0, p._detail_scroll.verticalScrollBar().value())
            self.assertFalse(p._barrier_detail_area.isVisible())
            p._barrier_details_toggle.click()
            self.app.processEvents()
            self.assertTrue(p._barrier_detail_area.isVisible())
            p._barrier_details_toggle.click()
            self.assertEqual(QBoxLayout.Direction.LeftToRight, p._drive_layout.direction())
            self.assertEqual(QBoxLayout.Direction.LeftToRight, p._overview_layout.direction())
            self.assertEqual(4, p._header_columns)

            # The Claude layout contract uses 1200/900/600 px breakpoints.
            p.resize(1150, 1040)
            self.app.processEvents()
            self.app.processEvents()
            self.assertEqual(QBoxLayout.Direction.TopToBottom, p._overview_layout.direction())
            self.assertEqual(QBoxLayout.Direction.LeftToRight, p._drive_layout.direction())
            p.resize(850, 1040)
            self.app.processEvents()
            self.app.processEvents()
            self.assertEqual(QBoxLayout.Direction.TopToBottom, p._drive_layout.direction())
            self.assertEqual(QBoxLayout.Direction.TopToBottom, p._bottom_layout.direction())
            self.assertEqual(2, p._header_columns)
            self.assertFalse(p._analysis_panel.isVisible())
            self.assertTrue(p._analysis_toggle.isVisible())
            p._analysis_toggle.click()
            self.app.processEvents()
            self.assertTrue(p._analysis_panel.isVisible())
            self.assertEqual(0, p._detail_scroll.horizontalScrollBar().maximum())
            p._analysis_toggle.click()
            p.resize(550, 1040)
            self.app.processEvents()
            self.app.processEvents()
            self.assertEqual(1, p._header_columns)
            # One visual scenario row per consequence. Each HAZOP consequence
            # category is its own column, with numeric level and active toggle
            # in the same cell.
            self.assertEqual(3, p._hazop_hierarchy.rowCount())
            headers = [p._hazop_hierarchy.horizontalHeaderItem(index).text()
                       for index in range(p._hazop_hierarchy.columnCount())]
            category_columns = {
                header: index for index, header in enumerate(headers)
            }
            category_column = category_columns[category['name']]
            second_category_column = category_columns[second_category['name']]
            self.assertEqual('Överfyllnad',
                             p._hazop_hierarchy.item(0, p._HAZOP_CONSEQUENCE_COL).text())
            self.assertEqual('3',
                             p._hazop_hierarchy.cellWidget(0, category_column).text())
            self.assertEqual('2',
                             p._hazop_hierarchy.cellWidget(0, second_category_column).text())
            self.assertEqual('2',
                             p._hazop_hierarchy.cellWidget(1, category_column).text())
            self.assertEqual('4',
                             p._hazop_hierarchy.cellWidget(1, second_category_column).text())
            self.assertEqual(unassessed_cons_id, p._hazop_hierarchy.item(
                2, category_column).data(p._ROLE_HAZOP_CONSEQUENCE_ID))
            self.assertEqual(
                ['HAZOP-hierarki', 'Orsak', 'Grundfrekvens', 'Konsekvens'],
                headers[:p._HAZOP_CATEGORY_START_COL])
            self.assertIn(category['name'], headers)
            self.assertIn(second_category['name'], headers)
            self.assertNotIn('Aktiv', headers)
            source_reference = p._hazop_hierarchy.cellWidget(
                0, p._HAZOP_REFERENCE_COL)
            self.assertRegex(source_reference.text(), r'^\d+(\.\d+){3,}$')
            self.assertIsNotNone(p._hazop_hierarchy.cellWidget(
                1, p._HAZOP_REFERENCE_COL))
            self.assertEqual(cons_id, p._hazop_hierarchy.item(
                0, p._HAZOP_CONSEQUENCE_COL).data(p._ROLE_HAZOP_CONSEQUENCE_ID))
            # Each category cell has its own revision-local include control.
            p._confirm_lopa_only = lambda _text: True
            self.app.processEvents()
            hierarchy_geometry = {
                'height': p._hazop_hierarchy.height(),
                'column_widths': [p._hazop_hierarchy.columnWidth(column)
                                  for column in range(p._hazop_hierarchy.columnCount())],
                'row_heights': [p._hazop_hierarchy.rowHeight(row)
                                for row in range(p._hazop_hierarchy.rowCount())],
            }
            assessment_toggle = p._hazop_hierarchy.cellWidget(0, category_column)
            self.assertGreaterEqual(
                assessment_toggle.width(),
                p._hazop_hierarchy.columnWidth(category_column) - 2)
            self.assertGreaterEqual(assessment_toggle.height(),
                                    p._hazop_hierarchy.rowHeight(0) - 2)
            assessment_id = assessment_toggle.property('lopa_assessment_id')
            assessment_toggle.setChecked(False)
            self.app.processEvents()
            assessment = next(row for row in self.db.lopa_source_consequences(
                imported['source_id']) if row['id'] == assessment_id)
            self.assertFalse(assessment['active'])
            self.assertEqual(hierarchy_geometry['height'], p._hazop_hierarchy.height())
            self.assertEqual(hierarchy_geometry['column_widths'], [
                p._hazop_hierarchy.columnWidth(column)
                for column in range(p._hazop_hierarchy.columnCount())])
            self.assertEqual(hierarchy_geometry['row_heights'], [
                p._hazop_hierarchy.rowHeight(row)
                for row in range(p._hazop_hierarchy.rowCount())])
            p._confirm_lopa_only = lambda _text: False
            cancel_toggle = p._hazop_hierarchy.cellWidget(0, second_category_column)
            cancel_id = cancel_toggle.property('lopa_assessment_id')
            cancel_toggle.setChecked(False)
            self.app.processEvents()
            cancelled_assessment = next(row for row in self.db.lopa_source_consequences(
                imported['source_id']) if row['id'] == cancel_id)
            self.assertTrue(cancelled_assessment['active'])
            self.assertEqual(hierarchy_geometry['height'], p._hazop_hierarchy.height())
            self.assertEqual(hierarchy_geometry['column_widths'], [
                p._hazop_hierarchy.columnWidth(column)
                for column in range(p._hazop_hierarchy.columnCount())])
            self.assertEqual(hierarchy_geometry['row_heights'], [
                p._hazop_hierarchy.rowHeight(row)
                for row in range(p._hazop_hierarchy.rowCount())])
            self.assertTrue(self.db.lopa_sources(created['revision_id'])[0]['active'])
            self.assertEqual(1, p._sensor_members.rowCount())
            self.assertEqual(1, p._barriers.rowCount())
            # Every actual consequence/category pair has its own risk row.
            self.assertEqual(4, p._escalation.rowCount())
            self.assertEqual(QHeaderView.ResizeMode.Stretch,
                             p._escalation.horizontalHeader().sectionResizeMode(1))
            self.assertEqual('Överfyllnad', p._escalation.item(0, 1).text())
            self.assertEqual('Utsläpp till mark', p._escalation.item(2, 1).text())
            self.assertIsNotNone(p._sensor_group.currentData())
            self.assertEqual('1oo1', p._sensor_voting.currentText())
            self.assertEqual(1, p._final_members.rowCount())
            self.assertEqual('1oo1', p._final_voting.currentText())
            self.assertEqual(1, p._barrier_matrix.rowCount())
            self.assertIn('Återstående frekvens', p._barrier_summary.text())
            self.assertIn('Dimensionerande kriterium', p._dimensioning_summary.text())
            self.assertEqual('2026-09-02', p._document_date.text())
            self.assertIn('Verifiera ventilen', p._additional_actions.toPlainText())
            self.assertEqual(1, p._comments.rowCount())
            navigated = []
            p.hazop_navigation_requested.connect(navigated.append)
            source_reference.click()
            self.assertEqual([cause_id], navigated)
            self.assertFalse(hasattr(p, '_sync_sources_btn'))
            self.assertFalse(hasattr(p, '_goto_hazop_btn'))
        finally:
            p.deleteLater()

    def test_hazop_preparation_panel_constructs(self):
        from settings_panels import HAZOPPreparationPanel
        p = HAZOPPreparationPanel(self.db)
        p.deleteLater()

    def test_settings_panel_constructs(self):
        from settings_panels import SettingsPanel
        p = SettingsPanel(self.db)
        p.deleteLater()

    def test_study_management_panel_constructs_with_a_real_revision(self):
        """This exact construction path is what caught the real
        NameError: name 'Path' is not defined crash (2026-08-18) —
        StudyManagementPanel builds PIDManagementPanel, which calls
        refresh() during __init__, which crashed on any revision row
        with pdf_path set. An empty-DB construction would NOT have
        caught this."""
        from settings_panels import StudyManagementPanel
        p = StudyManagementPanel(self.db)
        p.deleteLater()

    def _click_every_tool_button(self, panel, load_fn=None):
        """PyQt6's default behaviour for an unhandled exception raised
        inside a signal/slot call (e.g. via .click()) is to print it via
        sys.excepthook and then ABORT THE WHOLE PROCESS, not raise it as
        a normal Python exception the caller can catch — which would
        silently kill this entire smoke run instead of failing one test.
        Swap in a capturing excepthook for the duration of the clicks so
        a bug like the real '_StylePopup is not defined' crash
        (2026-08-18) surfaces as a clean, isolated test failure instead.

        `load_fn(panel, node_id)` defaults to the plain `panel.load(node_id)`
        RedMarkupPanel still uses; PropertiesRibbon's merged-in markup
        toolbar (2026-08-19) needs its own two-step set_item/
        enter_markup_mode instead (see that test below)."""
        if load_fn is None:
            load_fn = lambda p, nid: p.load(nid)
        caught = []
        old_hook = sys.excepthook
        sys.excepthook = lambda *exc_info: caught.append(exc_info)
        try:
            node_id = self.db.add_node()
            load_fn(panel, node_id)
            for tool, btn in panel._tool_btns.items():
                btn.click()
        finally:
            sys.excepthook = old_hook
        if caught:
            import traceback
            msg = ''.join(traceback.format_exception(*caught[0]))
            self.fail(f"Exception raised while clicking tool button(s):\n{msg}")

    def test_node_markup_toolbar_in_properties_ribbon_every_tool_button_works(self):
        """2026-08-19: NodeMarkupPanel merged into PropertiesRibbon (see
        NOTES.md "Slå ihop nodmarkup i nodinställningar") — this exact
        interaction (clicking a real tool button, not just constructing
        the panel) is what caught the real NameError: name '_StylePopup'
        is not defined crash (2026-08-18) when NodeMarkupPanel was still
        its own widget. Plain construction alone would have missed it."""
        from node_markup import PropertiesRibbon
        from constants import NODE_T
        p = PropertiesRibbon(self.db)
        try:
            self._click_every_tool_button(
                p, load_fn=lambda panel, nid: (
                    panel.set_item(NODE_T, nid), panel.enter_markup_mode(nid)))
        finally:
            p.deleteLater()

    def test_red_markup_panel_constructs_and_every_tool_button_works(self):
        from node_markup import RedMarkupPanel
        p = RedMarkupPanel(self.db)
        try:
            self._click_every_tool_button(p)
        finally:
            p.deleteLater()

    def test_pid_panel_constructs(self):
        from pid_panel_mod import PIDPanel
        p = PIDPanel(self.db)
        p.deleteLater()

    def test_main_window_constructs_end_to_end(self):
        """The single most valuable check here — exercises MainWindow's
        entire __init__ wiring (every panel, every signal connection)
        against a realistic DB in one shot."""
        from hazop import MainWindow
        win = MainWindow()
        try:
            win.db = self.db
            win._reload_all_panels()
        finally:
            win.deleteLater()


if __name__ == '__main__':
    unittest.main()
