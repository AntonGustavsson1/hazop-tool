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
    def test_table_header_text_has_readable_contrast(self):
        from design import TABLE_HEADER_TEXT, SUBTLE_SURFACE

        def luminance(color):
            values = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            values = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
                      for v in values]
            return 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]
        ratio = (luminance(SUBTLE_SURFACE) + 0.05) / (luminance(TABLE_HEADER_TEXT) + 0.05)
        self.assertGreaterEqual(ratio, 4.5)

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

    def test_admin_styles_use_shared_surfaces_and_distinct_danger_states(self):
        from design import (
            ADMIN_SUMMARY_BG, ADMIN_SUMMARY_BORDER, DANGER,
            DANGER_HOVER, DANGER_PRESSED, SECONDARY_TEXT, SURFACE,
            admin_danger_button_stylesheet, admin_summary_stylesheet,
        )

        summary = admin_summary_stylesheet()
        danger = admin_danger_button_stylesheet()
        self.assertIn(ADMIN_SUMMARY_BG, summary)
        self.assertIn(ADMIN_SUMMARY_BORDER, summary)
        self.assertIn(f'color:{SURFACE}', danger)
        self.assertIn(DANGER, danger)
        self.assertIn(DANGER_HOVER, danger)
        self.assertIn(DANGER_PRESSED, danger)
        self.assertIn('QPushButton:disabled', danger)
        self.assertIn(SECONDARY_TEXT, danger)

    def test_lopa_document_builders_share_the_neutral_design_tokens(self):
        from design import (
            ACCENT_SELECTION, MUTED_TEXT, SECONDARY_TEXT, SEPARATOR, SURFACE, TEXT,
            lopa_card_stylesheet, lopa_note_stylesheet, lopa_section_title_stylesheet,
            lopa_hierarchy_reference_stylesheet, lopa_hierarchy_stylesheet,
            lopa_table_stylesheet, lopa_title_stylesheet,
        )

        self.assertIn(SURFACE, lopa_card_stylesheet())
        self.assertIn(SEPARATOR, lopa_card_stylesheet())
        self.assertIn(TEXT, lopa_title_stylesheet())
        # Section headings are deliberately muted, not full-strength TEXT
        # (2026-09-02 overview pass) -- they're page chrome that should
        # recede behind the data, not compete with it -- but still a named
        # design token, not an ad hoc colour.
        self.assertIn(MUTED_TEXT, lopa_section_title_stylesheet())
        self.assertIn(SECONDARY_TEXT, lopa_note_stylesheet())
        self.assertIn(ACCENT_SELECTION, lopa_table_stylesheet())
        self.assertIn(ACCENT_SELECTION, lopa_hierarchy_stylesheet())
        self.assertIn('lopaHierarchyReference', lopa_hierarchy_reference_stylesheet())

    def test_lopa_responsive_geometry_is_centralized_in_design(self):
        from design import (
            LOPA_BREAKPOINT_HEADER_SINGLE, LOPA_BREAKPOINT_OVERVIEW,
            LOPA_BREAKPOINT_SIDEBAR, LOPA_BREAKPOINT_TWO_COLUMN,
            LOPA_MAIN_MAX_WIDTH, LOPA_SIDEBAR_MAX_WIDTH,
            LOPA_SIDEBAR_MIN_WIDTH,
        )

        self.assertEqual(1400, LOPA_MAIN_MAX_WIDTH)
        self.assertEqual((160, 220), (LOPA_SIDEBAR_MIN_WIDTH, LOPA_SIDEBAR_MAX_WIDTH))
        self.assertEqual((1024, 1200, 900, 600), (
            LOPA_BREAKPOINT_SIDEBAR, LOPA_BREAKPOINT_OVERVIEW,
            LOPA_BREAKPOINT_TWO_COLUMN, LOPA_BREAKPOINT_HEADER_SINGLE))


class CommonControlRenderingTests(unittest.TestCase):
    """Shared controls need visible state and affordances under the real theme."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def _render(self, widget):
        from design import app_stylesheet
        widget.setStyleSheet(app_stylesheet())
        widget.show()
        self.app.processEvents()
        return widget.grab().toImage()

    def test_table_respects_item_colors_under_application_theme(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor
        from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem
        from design import ACCENT_SELECTION, app_stylesheet

        table = QTableWidget(1, 1)
        item = QTableWidgetItem('Risk')
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setBackground(QColor('#27ae60'))
        item.setForeground(QColor('#ffffff'))
        table.setItem(0, 0, item)
        table.setColumnWidth(0, 110)
        table.setRowHeight(0, 50)
        table.resize(180, 120)
        previous_stylesheet = self.app.styleSheet()
        previous_style = self.app.style().objectName()
        try:
            self.app.setStyle('Fusion')
            self.app.setStyleSheet(app_stylesheet())
            table.show()
            self.app.processEvents()
            table.clearSelection()
            self.app.processEvents()
            rect = table.visualItemRect(item)

            def pixels():
                image = table.viewport().grab().toImage()
                scale = image.devicePixelRatio()
                return [image.pixelColor(int(x * scale), int(y * scale))
                        for y in range(rect.top() + 3, rect.bottom() - 2)
                        for x in range(rect.left() + 3, rect.right() - 2)]

            def green_count(colors):
                # Fusion can tint the focused cell slightly; its configured
                # green must still cover the background, regardless of tint.
                return sum(c.green() > c.red() * 2 and c.green() > c.blue() + 40
                           for c in colors)

            unselected = pixels()
            self.assertGreater(green_count(unselected), 100)
            self.assertGreater(sum(min(c.red(), c.green(), c.blue()) > 200
                                   for c in unselected), 3, 'the cell text should be visible')
            table.setCurrentCell(0, 0)
            self.app.processEvents()
            selected = pixels()
            self.assertGreater(sum(c.name() == ACCENT_SELECTION.lower() for c in selected), 100)
            table.clearSelection()
            self.app.processEvents()
            self.assertGreater(green_count(pixels()), 100)
        finally:
            table.close()
            table.deleteLater()
            self.app.setStyleSheet(previous_stylesheet)
            self.app.setStyle(previous_style)

    @staticmethod
    def _white_pixels(image, rect):
        count = 0
        scale = image.devicePixelRatio()
        for y in range(rect.top() + 2, rect.bottom() - 1):
            for x in range(rect.left() + 2, rect.right() - 1):
                color = image.pixelColor(int(x * scale), int(y * scale))
                if color.red() > 220 and color.green() > 220 and color.blue() > 220:
                    count += 1
        return count

    @staticmethod
    def _blue_pixels(image, rect):
        scale = image.devicePixelRatio()
        return sum(
            1 for y in range(rect.top() + 2, rect.bottom() - 1)
            for x in range(rect.left() + 2, rect.right() - 1)
            if (color := image.pixelColor(int(x * scale), int(y * scale))).blue() > color.red() + 50)

    def test_checked_and_partial_checkboxes_show_a_white_symbol(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QCheckBox, QStyle, QStyleOptionButton

        for state in (Qt.CheckState.Checked, Qt.CheckState.PartiallyChecked):
            with self.subTest(state=state):
                checkbox = QCheckBox('Tillämplig')
                checkbox.setTristate(True)
                checkbox.setCheckState(state)
                try:
                    image = self._render(checkbox)
                    option = QStyleOptionButton()
                    checkbox.initStyleOption(option)
                    rect = checkbox.style().subElementRect(
                        QStyle.SubElement.SE_CheckBoxIndicator, option, checkbox)
                    self.assertGreater(self._blue_pixels(image, rect), 20)
                    self.assertGreater(self._white_pixels(image, rect), 2,
                                       'checked state needs a visible symbol')
                finally:
                    checkbox.close()
                    checkbox.deleteLater()

    def test_checked_radio_button_has_visible_dot(self):
        from PyQt6.QtWidgets import QRadioButton, QStyle, QStyleOptionButton

        radio = QRadioButton('Alternativ')
        radio.setChecked(True)
        try:
            image = self._render(radio)
            option = QStyleOptionButton()
            radio.initStyleOption(option)
            rect = radio.style().subElementRect(
                QStyle.SubElement.SE_RadioButtonIndicator, option, radio)
            self.assertGreater(self._blue_pixels(image, rect), 15)
            self.assertGreater(self._white_pixels(image, rect), 2)
        finally:
            radio.close()
            radio.deleteLater()

    def test_combobox_has_a_visible_dropdown_indicator(self):
        from PyQt6.QtWidgets import QComboBox, QStyle, QStyleOptionComboBox

        combo = QComboBox()
        combo.addItems(['Första', 'Andra'])
        combo.resize(200, 30)
        try:
            image = self._render(combo)
            option = QStyleOptionComboBox()
            combo.initStyleOption(option)
            rect = combo.style().subControlRect(
                QStyle.ComplexControl.CC_ComboBox, option,
                QStyle.SubControl.SC_ComboBoxArrow, combo)
            scale = image.devicePixelRatio()
            dark_pixels = sum(
                1 for y in range(rect.top() + 4, rect.bottom() - 3)
                for x in range(rect.left() + 3, rect.right() - 2)
                if max(image.pixelColor(int(x * scale), int(y * scale)).getRgb()[:3]) < 180)
            self.assertGreater(dark_pixels, 2, 'dropdown needs a visible arrow')
        finally:
            combo.close()
            combo.deleteLater()

    def test_checkbox_can_still_be_toggled_with_keyboard(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QCheckBox

        checkbox = QCheckBox('Tillämplig')
        try:
            self._render(checkbox)
            checkbox.setFocus()
            QTest.keyClick(checkbox, Qt.Key.Key_Space)
            self.assertTrue(checkbox.isChecked())
            QTest.keyClick(checkbox, Qt.Key.Key_Space)
            self.assertFalse(checkbox.isChecked())
        finally:
            checkbox.close()
            checkbox.deleteLater()

    def test_disabled_button_is_visually_distinct_from_enabled(self):
        from PyQt6.QtWidgets import QPushButton

        button = QPushButton('Spara')
        button.resize(120, 34)
        try:
            enabled = self._render(button).pixelColor(8, 17)
            button.setEnabled(False)
            self.app.processEvents()
            disabled = button.grab().toImage().pixelColor(8, 17)
            self.assertNotEqual(enabled, disabled)
        finally:
            button.close()
            button.deleteLater()


if __name__ == '__main__':
    unittest.main()
