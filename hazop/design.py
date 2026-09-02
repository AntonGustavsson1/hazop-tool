"""Central front-end design tokens and shared Qt styles for HAZOP.

This module is deliberately independent of application behaviour and the
database.  Visual changes should start here: components may consume the
semantic tokens or one of the named stylesheet builders below, while their
event handling and data/layout logic remains in the component module.

The first extraction keeps the current appearance intact.  It centralises
the application theme and the shared Scenario/Worksheet cell treatment so
future design work has one obvious place to start without changing the
editing, selection, or table-building logic.
"""

# Semantic palette.  Keep names stable and use these names in new styles
# instead of introducing another hard-coded colour in a panel module.
APP_BACKGROUND = '#FBFBFA'
SURFACE = '#FFFFFF'
SUBTLE_SURFACE = '#F5F5F3'
HOVER_SURFACE = '#E8E9E6'
PRESSED_SURFACE = '#E8E9E6'
TEXT = '#17191C'
MUTED_TEXT = '#8D9299'
SECONDARY_TEXT = '#6B7280'
FIELD_BORDER = '#CFD1CE'
SEPARATOR = '#E2E3E1'
GRIDLINE = '#EEEFEC'
SCROLL_HANDLE = '#CFD1CE'
SCROLL_HANDLE_HOVER = '#B3B7B2'
ACCENT = '#2F5FD0'
ACCENT_SELECTION = '#E6ECFA'

# HAZOP's deliberately neutral, flat selected-cell overlay.  Custom-painted
# delegates use the same values, so a selected cell does not turn into a
# second blue edit widget.
SCENARIO_SELECTION_BG = '#D9DBD8'
SCENARIO_SELECTION_FG = TEXT
SCENARIO_SELECTION_HOVER = '#C8CCC8'

# Shared compact summary controls used by Enablers, RRF, and Frequency.
SUMMARY_BADGE_BG = SUBTLE_SURFACE
SUMMARY_SEPARATOR = SEPARATOR
SUMMARY_BADGE_WIDTH = 32
SUMMARY_BADGE_HEIGHT = 22
SUMMARY_BUTTON_HEIGHT = 24

# Shared risk-bar geometry.  The risk colour itself remains data-driven and
# is therefore intentionally not part of this design module.
RISK_BAR_HEIGHT = 22
RISK_BAR_MARGIN_X = 2
RISK_BAR_MARGIN_Y = 1
RISK_BAR_RADIUS = 5
RISK_COLUMN_DEFAULT_WIDTH = 52


def app_stylesheet() -> str:
    """Return the application-wide light theme.

    Deliberately no ``font-size`` is attached to the universal selector.
    Scenario/Worksheet owns a user-adjustable text size and a universal QSS
    font-size silently overrides a widget's own ``setFont()`` call.
    """
    return f"""
    * {{
        font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
    }}

    QMainWindow, QDialog, QWidget {{
        background-color: {APP_BACKGROUND};
        color: {TEXT};
    }}

    QPushButton {{
        background-color: {SURFACE};
        color: {TEXT};
        border: 1px solid {SEPARATOR};
        border-radius: 4px;
        padding: 4px 10px;
        font-weight: 500;
    }}
    QPushButton:hover {{
        background-color: {SUBTLE_SURFACE};
        border-color: {FIELD_BORDER};
    }}
    QPushButton:pressed {{
        background-color: {PRESSED_SURFACE};
    }}
    QPushButton:checked {{
        background-color: {ACCENT};
        color: {SURFACE};
        border-color: {ACCENT};
    }}
    QPushButton:focus {{
        outline: 2px solid {ACCENT};
        outline-offset: 2px;
    }}
    QPushButton:default {{
        border-color: {ACCENT};
    }}

    QCheckBox, QRadioButton {{ color: {TEXT}; spacing: 6px; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 14px; height: 14px;
        border: 1px solid {FIELD_BORDER};
        background-color: {SURFACE};
    }}
    QCheckBox::indicator {{ border-radius: 3px; }}
    QRadioButton::indicator {{ border-radius: 7px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {TEXT}; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background-color: {ACCENT};
        border-color: {ACCENT};
    }}

    QSpinBox, QDoubleSpinBox {{
        background-color: {SURFACE};
        color: {TEXT};
        border: 1px solid {FIELD_BORDER};
        border-radius: 4px;
        padding: 3px 4px;
    }}
    QSpinBox:focus, QDoubleSpinBox:focus {{ border: 2px solid {ACCENT}; }}

    QListWidget, QListView {{
        background-color: {SURFACE};
        border: 1px solid {SEPARATOR};
        border-radius: 4px;
    }}
    QListWidget::item:hover, QListView::item:hover {{ background-color: {SUBTLE_SURFACE}; }}
    QListWidget::item:selected, QListView::item:selected {{
        background-color: {ACCENT_SELECTION};
        color: {TEXT};
    }}

    QTabWidget::pane {{ border: 1px solid {SEPARATOR}; border-radius: 4px; }}
    QTabBar::tab {{
        background-color: {SUBTLE_SURFACE};
        color: {TEXT};
        padding: 5px 12px;
        border: 1px solid {SEPARATOR};
        border-bottom: none;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
    }}
    QTabBar::tab:hover {{ background-color: {HOVER_SURFACE}; }}
    QTabBar::tab:selected {{
        background-color: {SURFACE};
        border-bottom: 2px solid {ACCENT};
    }}

    QLineEdit, QTextEdit, QPlainTextEdit {{
        background-color: {SURFACE};
        color: {TEXT};
        border: 1px solid {FIELD_BORDER};
        border-radius: 4px;
        padding: 4px 6px;
        selection-background-color: {ACCENT};
        selection-color: {SURFACE};
    }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
        border: 2px solid {ACCENT};
        padding: 3px 5px;
    }}

    QComboBox {{
        background-color: {SURFACE};
        color: {TEXT};
        border: 1px solid {FIELD_BORDER};
        border-radius: 4px;
        padding: 4px 8px;
    }}
    QComboBox:focus {{ border: 2px solid {ACCENT}; }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox::down-arrow {{ image: none; }}

    QTableWidget, QTableView {{
        background-color: {SURFACE};
        alternate-background-color: {SUBTLE_SURFACE};
        gridline-color: {GRIDLINE};
        border: 1px solid {SEPARATOR};
        border-radius: 4px;
    }}
    QTableWidget::item, QTableView::item {{ padding: 4px; border: none; }}
    QTableWidget::item:selected, QTableView::item:selected {{
        background-color: {ACCENT_SELECTION};
        color: {TEXT};
    }}
    QHeaderView::section {{
        background-color: {SUBTLE_SURFACE};
        color: {MUTED_TEXT};
        padding: 4px;
        border: none;
        border-bottom: 1px solid {SEPARATOR};
        font-weight: 600;
        font-size: 8pt;
        letter-spacing: 0.5px;
    }}

    QTreeWidget, QTreeView {{
        background-color: {SURFACE};
        alternate-background-color: {SUBTLE_SURFACE};
        border: 1px solid {SEPARATOR};
        border-radius: 4px;
    }}
    QTreeWidget::item:hover, QTreeView::item:hover {{ background-color: {SUBTLE_SURFACE}; }}
    QTreeWidget::item:selected, QTreeView::item:selected {{
        background-color: {ACCENT_SELECTION};
        color: {TEXT};
    }}

    QFrame {{ background-color: {SURFACE}; border: none; }}
    QGroupBox {{
        color: {TEXT};
        border: 1px solid {SEPARATOR};
        border-radius: 6px;
        margin-top: 8px;
        padding-top: 8px;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 3px; color: {MUTED_TEXT}; }}

    QScrollBar:vertical {{ background-color: {SUBTLE_SURFACE}; border: none; width: 12px; }}
    QScrollBar::handle:vertical {{ background-color: {SCROLL_HANDLE}; border-radius: 6px; margin: 2px; min-height: 20px; }}
    QScrollBar::handle:vertical:hover {{ background-color: {SCROLL_HANDLE_HOVER}; }}
    QScrollBar:horizontal {{ background-color: {SUBTLE_SURFACE}; border: none; height: 12px; }}
    QScrollBar::handle:horizontal {{ background-color: {SCROLL_HANDLE}; border-radius: 6px; margin: 2px; min-width: 20px; }}
    QScrollBar::handle:horizontal:hover {{ background-color: {SCROLL_HANDLE_HOVER}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ border: none; background: none; }}

    QDialog {{ background-color: {SURFACE}; }}
    QSplitter::handle {{ background-color: {SEPARATOR}; }}
    QSplitter::handle:hover {{ background-color: {FIELD_BORDER}; }}

    QMenuBar {{ background-color: {SURFACE}; color: {TEXT}; border-bottom: 1px solid {SEPARATOR}; }}
    QMenuBar::item:selected {{ background-color: {SUBTLE_SURFACE}; }}
    QMenu {{ background-color: {SURFACE}; color: {TEXT}; border: 1px solid {FIELD_BORDER}; border-radius: 4px; }}
    QMenu::item:selected {{ background-color: {SUBTLE_SURFACE}; }}

    QToolTip {{
        background-color: {TEXT};
        color: {SURFACE};
        border: none;
        border-radius: 4px;
        padding: 4px 8px;
    }}
    """


def scenario_table_stylesheet() -> str:
    """Return the shared flat style for HAZOP Scenario and Worksheet."""
    return (
        'QTableWidget{border-radius:0px;}'
        'QTableWidget::item{padding:2px 3px;border:none;}'
        f'QTableWidget::item:selected{{background:{SCENARIO_SELECTION_BG};'
        f'color:{SCENARIO_SELECTION_FG};border:none;outline:none;}}'
        'QTableWidget::item:focus{border:none;outline:none;}'
        f'QHeaderView::section{{background:{SUBTLE_SURFACE};'
        f'color:{MUTED_TEXT};font-weight:600;padding:3px;border-radius:0px;}}'
    )


def summary_badge_stylesheet(selected: bool = False) -> str:
    """Return the common Enablers/RRF/Frequency summary-button style."""
    background = SCENARIO_SELECTION_BG if selected else SUMMARY_BADGE_BG
    hover = SCENARIO_SELECTION_HOVER if selected else HOVER_SURFACE
    return (
        f'QPushButton#enablerSummaryButton{{background:{background};'
        f'color:{TEXT};border:none;font-size:9px;font-weight:bold;'
        f'padding:2px 4px;}}'
        f'QPushButton#enablerSummaryButton:hover{{background:{hover};}}'
    )
