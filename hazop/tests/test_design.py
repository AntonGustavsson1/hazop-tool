"""Regression tests for the central HAZOP front-end design layer."""

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

_TEST_DIR = Path(__file__).resolve().parent
_HAZOP_DIR = _TEST_DIR.parent
for _p in (_HAZOP_DIR, _TEST_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


class DesignSystemTests(unittest.TestCase):
    def test_app_stylesheet_is_the_hazop_runtime_source(self):
        from design import app_stylesheet
        from hazop import _get_windows11_stylesheet

        self.assertEqual(_get_windows11_stylesheet(), app_stylesheet())

    def test_universal_rule_keeps_font_size_overridable(self):
        from design import app_stylesheet

        css = app_stylesheet()
        universal = css.split('* {', 1)[1].split('}', 1)[0]
        self.assertNotIn('font-size', universal)

    def test_scenario_and_summary_styles_share_the_neutral_selection(self):
        from design import (
            SCENARIO_SELECTION_BG,
            SCENARIO_SELECTION_FG,
            scenario_table_stylesheet,
            summary_badge_stylesheet,
        )

        table_css = scenario_table_stylesheet()
        selected_badge_css = summary_badge_stylesheet(True)
        self.assertIn(SCENARIO_SELECTION_BG, table_css)
        self.assertIn(SCENARIO_SELECTION_FG, table_css)
        self.assertIn(SCENARIO_SELECTION_BG, selected_badge_css)
        self.assertIn(SCENARIO_SELECTION_FG, selected_badge_css)

    def test_summary_style_has_explicit_selected_and_unselected_variants(self):
        from design import summary_badge_stylesheet

        selected = summary_badge_stylesheet(True)
        unselected = summary_badge_stylesheet(False)
        self.assertNotEqual(selected, unselected)
        self.assertIn('QPushButton#enablerSummaryButton:hover', selected)
        self.assertIn('QPushButton#enablerSummaryButton:hover', unselected)

    def test_popup_builders_use_the_shared_palette(self):
        from design import (
            ACCENT, ACCENT_HOVER, FIELD_BORDER, HOVER_SURFACE, SEPARATOR,
            SUBTLE_SURFACE, SURFACE, TEXT,
            popup_action_button_stylesheet, popup_list_stylesheet,
            popup_primary_button_stylesheet, popup_separator_stylesheet,
            popup_shell_stylesheet, popup_toggle_button_stylesheet,
        )

        shell = popup_shell_stylesheet('testPopup')
        action = popup_action_button_stylesheet(pressed=True)
        listing = popup_list_stylesheet()
        primary = popup_primary_button_stylesheet()
        separator = popup_separator_stylesheet()
        toggle = popup_toggle_button_stylesheet(True)

        self.assertIn(SURFACE, shell)
        for css in (action, listing):
            self.assertIn(TEXT, css)
        self.assertIn(SURFACE, primary)
        self.assertIn(SURFACE, toggle)
        self.assertIn(SEPARATOR, separator)
        self.assertIn(SUBTLE_SURFACE, action)
        self.assertIn(HOVER_SURFACE, listing)
        self.assertIn(ACCENT, primary)
        self.assertIn(ACCENT_HOVER, toggle)
        unselected_toggle = popup_toggle_button_stylesheet(False)
        self.assertIn(TEXT, unselected_toggle)
        self.assertIn(FIELD_BORDER, unselected_toggle)


if __name__ == '__main__':
    unittest.main()
