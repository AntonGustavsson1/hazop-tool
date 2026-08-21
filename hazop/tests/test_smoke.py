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

from PyQt6.QtWidgets import QApplication


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
        db.add_revision('A', '', pdf_path='C:/fake/smoke_test.pdf')
        db.add_equipment_item('SM-101', 'SM-101', 'SM', 0, 'Ventil', '', 0)

    # ── Every extracted module imports cleanly (catches NameError/
    # ImportError from a missed dependency at the module-load level) ──────
    def test_every_module_imports(self):
        import constants, database, ui_helpers, tree_panel, node_markup
        import worksheet, scenario_panel, equipment_panel, settings_panels
        import pid_viewer, pid_graphics_view, pid_panel_mod, hazop
        import equipment_detection, symbol_geometry, image_symbol_matching

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
