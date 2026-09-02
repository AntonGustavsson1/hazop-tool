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

    def test_summary_style_can_be_reused_by_rrf_and_frequency(self):
        from design import (
            SCENARIO_SELECTION_BG, SCENARIO_SELECTION_FG,
            SUMMARY_BADGE_BG, SUMMARY_BADGE_BORDER, TEXT,
            summary_badge_colors, summary_badge_override_stylesheet,
            summary_badge_stylesheet,
        )

        rrf = summary_badge_stylesheet(object_name='rrfSummaryButton')
        frequency = summary_badge_stylesheet(object_name='frequencySummaryButton')
        self.assertIn('QPushButton#rrfSummaryButton', rrf)
        self.assertIn('QPushButton#frequencySummaryButton', frequency)
        self.assertIn(f'border:1px solid {SUMMARY_BADGE_BORDER}', frequency)
        self.assertIn('font-size:9px', frequency)
        self.assertIn('QPushButton#frequencySummaryButton',
                      summary_badge_override_stylesheet())
        self.assertEqual(summary_badge_colors(False), (SUMMARY_BADGE_BG, TEXT))
        self.assertEqual(summary_badge_colors(True),
                         (SCENARIO_SELECTION_BG, SCENARIO_SELECTION_FG))
        self.assertNotEqual(summary_badge_colors(False, hovered=True),
                            summary_badge_colors(False))

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

    def test_common_panel_builders_use_named_design_tokens(self):
        from design import (
            ACCENT, ACCENT_HOVER, ACCENT_SELECTION, FIELD_BORDER,
            POPUP_BORDER, SEPARATOR, SUBTLE_SURFACE, SURFACE, TEXT,
            compact_control_stylesheet, compact_table_stylesheet,
            colour_strip_stylesheet, colour_swatch_stylesheet,
            dialog_primary_button_stylesheet, popup_clear_button_stylesheet,
            popup_compact_title_stylesheet, ribbon_button_stylesheet,
            separator_line_stylesheet, side_panel_stylesheet,
            symbol_selector_stylesheet, visibility_layer_button_stylesheet,
        )

        self.assertEqual(compact_control_stylesheet(), 'font-size:10px;')
        self.assertIn(SEPARATOR, separator_line_stylesheet())
        self.assertIn(SURFACE, ribbon_button_stylesheet())
        ribbon = ribbon_button_stylesheet(checkable=True)
        self.assertIn(ACCENT, ribbon)
        self.assertIn(SUBTLE_SURFACE, ribbon)
        self.assertIn(ACCENT_SELECTION, compact_table_stylesheet())
        self.assertIn(TEXT, compact_table_stylesheet())
        self.assertIn(ACCENT, dialog_primary_button_stylesheet())
        self.assertIn(ACCENT_HOVER, dialog_primary_button_stylesheet())
        self.assertIn(POPUP_BORDER, popup_clear_button_stylesheet('clear'))
        self.assertIn(TEXT, popup_compact_title_stylesheet())
        self.assertIn(SEPARATOR, side_panel_stylesheet())
        self.assertIn(ACCENT, symbol_selector_stylesheet())
        self.assertIn('#123456', colour_strip_stylesheet('#123456'))
        self.assertIn('#333', colour_swatch_stylesheet('#123456', selected=True))
        layer_button = visibility_layer_button_stylesheet('#2457A6', '#FFFFFF')
        self.assertIn('#2457A6', layer_button)
        self.assertIn('#FFFFFF', layer_button)

    def test_lopa_document_builders_share_the_neutral_design_tokens(self):
        from design import (
            ACCENT_SELECTION, SECONDARY_TEXT, SEPARATOR, SURFACE, TEXT,
            lopa_card_stylesheet, lopa_note_stylesheet, lopa_section_title_stylesheet,
            lopa_table_stylesheet, lopa_title_stylesheet,
        )

        self.assertIn(SURFACE, lopa_card_stylesheet())
        self.assertIn(SEPARATOR, lopa_card_stylesheet())
        self.assertIn(TEXT, lopa_title_stylesheet())
        self.assertIn(TEXT, lopa_section_title_stylesheet())
        self.assertIn(SECONDARY_TEXT, lopa_note_stylesheet())
        self.assertIn(ACCENT_SELECTION, lopa_table_stylesheet())


if __name__ == '__main__':
    unittest.main()
