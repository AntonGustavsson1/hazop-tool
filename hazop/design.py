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
ACCENT_HOVER = '#3D6BD8'
ACCENT_SELECTION = '#E6ECFA'
POPUP_BORDER = '#4B5563'
INPUT_BORDER = '#B8BDC4'

# HAZOP's deliberately neutral, flat selected-cell overlay.  Custom-painted
# delegates use the same values, so a selected cell does not turn into a
# second blue edit widget.
SCENARIO_SELECTION_BG = '#D9DBD8'
SCENARIO_SELECTION_FG = TEXT
SCENARIO_SELECTION_HOVER = '#C8CCC8'

# Shared compact summary controls used by Enablers, RRF, and Frequency.
SUMMARY_BADGE_BG = SUBTLE_SURFACE
SUMMARY_SEPARATOR = SEPARATOR
# The painted RRF/frequency zones use the same practical width as the
# original semantic table zones.  32 px made the control look like a tiny
# label beside the full Enablers button and clipped numeric values too easily.
SUMMARY_BADGE_WIDTH = 50
SUMMARY_BADGE_HEIGHT = 22
SUMMARY_BUTTON_HEIGHT = 24
SUMMARY_BADGE_RADIUS = 0
SUMMARY_BADGE_FONT_SIZE = 9
SUMMARY_BADGE_PADDING_X = 4
SUMMARY_BADGE_BORDER = FIELD_BORDER

# Shared risk-bar geometry.  The risk colour itself remains data-driven and
# is therefore intentionally not part of this design module.
RISK_BAR_HEIGHT = 22
RISK_BAR_MARGIN_X = 2
RISK_BAR_MARGIN_Y = 1
RISK_BAR_RADIUS = 5
RISK_COLUMN_DEFAULT_WIDTH = 52

# LOPA is a document-like analysis page, not another variant of the dense
# Scenario table.  Keep its calm cards and status chips here so the page can
# follow the same design contract as the rest of the application.
LOPA_CARD_RADIUS = 6
# The LOPA page is a working sheet with many related records.  Keep cards
# visibly separated but compact enough for a complete analysis on one screen.
LOPA_CARD_PADDING = 7


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


def summary_badge_stylesheet(
        selected: bool = False,
        object_name: str = 'enablerSummaryButton') -> str:
    """Return the common Enablers/RRF/Frequency summary-button style."""
    background = SCENARIO_SELECTION_BG if selected else SUMMARY_BADGE_BG
    hover = SCENARIO_SELECTION_HOVER if selected else HOVER_SURFACE
    border = SCROLL_HANDLE_HOVER if selected else SUMMARY_BADGE_BORDER
    return (
        f'QPushButton#{object_name}{{background:{background};'
        f'color:{TEXT};border:1px solid {border};'
        f'border-radius:{SUMMARY_BADGE_RADIUS}px;font-size:{SUMMARY_BADGE_FONT_SIZE}px;'
        f'font-weight:bold;'
        f'padding:2px {SUMMARY_BADGE_PADDING_X}px;}}'
        f'QPushButton#{object_name}:hover{{background:{hover};'
        f'border-color:{SCROLL_HANDLE_HOVER};}}'
    )


def lopa_card_stylesheet(object_name: str = 'lopaCard') -> str:
    """Compact neutral card used by the LOPA overview and detail panels."""
    return (
        f'QFrame#{object_name}{{background:{SURFACE};border:1px solid {SEPARATOR};'
        f'border-radius:{LOPA_CARD_RADIUS}px;}}'
        f'QFrame#{object_name} QLabel{{background:transparent;border:none;}}'
    )


def lopa_status_stylesheet(status: str) -> str:
    """Subtle textual status chip; risk colours remain reserved for matrix data."""
    if status in ('Låst', 'Godkänd'):
        background, border = '#E8E9E6', '#B3B7B2'
    elif status == 'Arkiverad':
        background, border = '#F1F1EF', '#CFD1CE'
    else:
        background, border = '#F5F5F3', '#CFD1CE'
    return (
        f'QLabel{{background:{background};color:{TEXT};border:1px solid {border};'
        'border-radius:9px;padding:2px 7px;font-size:9px;font-weight:600;}'
    )


def lopa_title_stylesheet() -> str:
    """Main LOPA page title, kept separate from document card headings."""
    return f'font-size:20px;font-weight:700;color:{TEXT};'


def lopa_section_title_stylesheet() -> str:
    """Quiet document-section heading shared by all LOPA cards."""
    return f'font-size:11px;font-weight:700;color:{TEXT};'


def lopa_note_stylesheet() -> str:
    """Small explanatory text for LOPA source, revision and review hints."""
    return f'color:{SECONDARY_TEXT};font-size:9pt;'


def lopa_table_stylesheet() -> str:
    """Flat readable LOPA table with the application's neutral selection."""
    return (
        f'QTableWidget{{background:{SURFACE};border:1px solid {SEPARATOR};'
        'border-radius:4px;gridline-color:#E1E3E5;}'
        f'QTableWidget::item:selected{{background:{ACCENT_SELECTION};color:{TEXT};}}'
        f'QHeaderView::section{{background:{SUBTLE_SURFACE};color:{TEXT};'
        f'border:none;border-bottom:1px solid {SEPARATOR};padding:4px;font-weight:600;}}'
    )


def summary_badge_colors(selected: bool = False,
                         hovered: bool = False) -> tuple[str, str]:
    """Return ``(background, text)`` for painted summary badges."""
    if selected:
        return (SCENARIO_SELECTION_HOVER if hovered else SCENARIO_SELECTION_BG,
                SCENARIO_SELECTION_FG)
    return (HOVER_SURFACE if hovered else SUMMARY_BADGE_BG, TEXT)


def summary_badge_border_color(selected: bool = False,
                               hovered: bool = False) -> str:
    """Return the border colour for a painted summary badge.

    Delegate-painted badges cannot consume the QPushButton stylesheet, so
    their border state is exposed beside ``summary_badge_colors``.  Keeping
    this small decision here prevents the delegate and the real Enablers
    button from drifting apart again.
    """
    if hovered or selected:
        return SCROLL_HANDLE_HOVER
    return SUMMARY_BADGE_BORDER


def summary_badge_override_stylesheet(
        object_name: str = 'frequencySummaryButton') -> str:
    """Return the summary-button style for an explicit value override.

    A manually entered frequency is still the same compact control, but keeps
    the existing warm hint that its value differs from the catalogue value.
    Geometry, typography and border treatment remain shared with Enablers and
    the normal RRF/frequency controls.
    """
    return (
        f'QPushButton#{object_name}{{background:#FDE8CC;color:#7B2D00;'
        f'border:1px solid #E7B77A;border-radius:{SUMMARY_BADGE_RADIUS}px;'
        f'font-size:{SUMMARY_BADGE_FONT_SIZE}px;font-weight:bold;'
        f'padding:2px {SUMMARY_BADGE_PADDING_X}px;}}'
        f'QPushButton#{object_name}:hover{{background:#FBD4A0;'
        f'border-color:#D89B50;}}'
    )


def popup_shell_stylesheet(object_name: str, *, border: str = POPUP_BORDER,
                           radius: int = 3) -> str:
    """Return the common framed shell for a compact popup."""
    return (
        f'QWidget#{object_name}{{background:{SURFACE};'
        f'border:1px solid {border};border-radius:{int(radius)}px;}}'
    )


def popup_label_stylesheet() -> str:
    """Return the borderless default label style inside a popup."""
    return f'QLabel{{border:none;color:{TEXT};}}'


def popup_input_stylesheet() -> str:
    """Return the compact input style used inside object/risk popups."""
    return (
        f'QComboBox,QLineEdit{{border:1px solid {INPUT_BORDER};'
        f'border-radius:2px;padding:2px 5px;background:{SURFACE};'
        f'color:{TEXT};}}'
    )


def popup_form_button_stylesheet(*, default: bool = False) -> str:
    """Return the compact bordered button used by popup form actions."""
    default_rule = ''
    if default:
        default_rule = (
            f'QPushButton:default{{background:{ACCENT};color:{SURFACE};'
            f'border-color:{ACCENT};}}'
            f'QPushButton:default:hover{{background:{ACCENT_HOVER};}}'
        )
    return (
        f'QPushButton{{border:1px solid {MUTED_TEXT};border-radius:2px;'
        f'padding:3px 7px;background:{SUBTLE_SURFACE};color:{TEXT};}}'
        f'QPushButton:hover{{background:{HOVER_SURFACE};}}'
        f'{default_rule}'
    )


def popup_action_button_stylesheet(*, padding: str = '3px 6px',
                                   pressed: bool = False) -> str:
    """Return a flat, left-aligned popup option-button style."""
    pressed_rule = (
        f'QPushButton:pressed{{background:{ACCENT_SELECTION};}}'
        if pressed else ''
    )
    return (
        f'QPushButton{{text-align:left;font-size:10px;padding:{padding};'
        f'border:none;background:transparent;color:{TEXT};border-radius:0px;}}'
        f'QPushButton:hover{{background:{SUBTLE_SURFACE};}}'
        f'{pressed_rule}'
    )


def popup_frequency_button_stylesheet() -> str:
    """Return the compact bold frequency action style."""
    return (
        f'QPushButton{{color:{TEXT};background:{SUBTLE_SURFACE};'
        f'border-radius:0px;padding:1px 6px;font-size:10px;'
        f'font-weight:bold;border:none;}}'
        f'QPushButton:hover{{background:{HOVER_SURFACE};}}'
    )


def popup_list_stylesheet() -> str:
    """Return the shared compact list style for popup choices."""
    return (
        f'QListWidget{{border:none;background:{SURFACE};font-size:10px;}}'
        f'QListWidget::item{{padding:3px 6px;color:{TEXT};}}'
        f'QListWidget::item:hover{{background:{SUBTLE_SURFACE};}}'
        f'QListWidget::item:selected{{background:{HOVER_SURFACE};'
        f'color:{TEXT};}}'
    )


def popup_primary_button_stylesheet() -> str:
    """Return the standard compact affirmative popup button style."""
    return (
        f'QPushButton{{border:none;font-size:10px;padding:3px 12px;'
        f'background:{ACCENT};color:{SURFACE};border-radius:0px;}}'
        f'QPushButton:hover{{background:{ACCENT_HOVER};}}'
    )


def popup_secondary_button_stylesheet() -> str:
    """Return the standard compact secondary popup button style."""
    return 'QPushButton{font-size:10px;padding:2px 8px;}'


def popup_toggle_button_stylesheet(selected: bool, *,
                                   font_size: str = '9px') -> str:
    """Return the selected/unselected style for compact matrix choices."""
    if selected:
        return (
            f'QPushButton{{background:{ACCENT};color:{SURFACE};'
            f'border:2px solid {ACCENT};border-radius:0px;'
            f'font-size:{font_size};font-weight:bold;}}'
            f'QPushButton:hover{{background:{ACCENT_HOVER};}}'
        )
    return (
        f'QPushButton{{background:{SUBTLE_SURFACE};color:{TEXT};'
        f'border:1px solid {FIELD_BORDER};border-radius:0px;'
        f'font-size:{font_size};}}'
        f'QPushButton:hover{{background:{HOVER_SURFACE};'
        f'border:1px solid {SCROLL_HANDLE_HOVER};}}'
    )


def popup_separator_stylesheet() -> str:
    """Return the shared separator colour used inside compact popups."""
    return f'color:{SEPARATOR};'


def popup_muted_label_stylesheet(font_size: str = '9px') -> str:
    """Return a small neutral explanatory-label style."""
    return f'color:{SECONDARY_TEXT};font-size:{font_size};'


def compact_control_stylesheet(font_size: str = '10px') -> str:
    """Return the shared small-control override used in compact panels."""
    return f'font-size:{font_size};'


def separator_line_stylesheet(color: str = SEPARATOR) -> str:
    """Return the flat one-pixel separator style used by tool panels."""
    return f'background:{color};max-height:1px;border:none;'


def ribbon_button_stylesheet(*, font_size: str | None = None,
                             checkable: bool = False) -> str:
    """Return the common compact button style used by vertical ribbons."""
    size = f'font-size:{font_size};' if font_size else ''
    checked = ''
    if checkable:
        checked = (
            f'QPushButton:checked{{background:{ACCENT};border-color:{ACCENT};}}'
        )
    return (
        f'QPushButton{{border:1px solid {SEPARATOR};border-radius:5px;'
        f'background:{SURFACE};padding:0px;{size}}}'
        f'{checked}'
        f'QPushButton:hover:!checked{{background:{SUBTLE_SURFACE};'
        f'border-color:{FIELD_BORDER};}}'
        f'QPushButton:pressed{{background:{PRESSED_SURFACE};}}'
    )


def dialog_primary_button_stylesheet() -> str:
    """Return the legacy-sized primary button used by detail popups."""
    return (
        f'QPushButton{{background:{ACCENT};color:{SURFACE};border:none;'
        f'border-radius:4px;padding:4px 16px;}}'
        f'QPushButton:hover{{background:{ACCENT_HOVER};}}'
    )


def compact_table_stylesheet() -> str:
    """Return the compact table override used by markup tables."""
    return (
        f'QTableWidget{{border:1px solid {SEPARATOR};font-size:10px;}}'
        f'QTableWidget::item:selected{{background:{ACCENT_SELECTION};'
        f'color:{TEXT};}}'
    )


def popup_compact_title_stylesheet() -> str:
    """Return the compact bold title style used by list popups."""
    return f'border:none;color:{TEXT};font-size:10px;font-weight:bold;'


def popup_muted_heading_stylesheet(font_size: str = '11px') -> str:
    """Return the muted heading style used by compact detail popups."""
    return f'border:none;color:{MUTED_TEXT};font-size:{font_size};'


def popup_clear_button_stylesheet(object_name: str) -> str:
    """Return the unobtrusive clear/action link style for a popup."""
    return (
        f'QPushButton#{object_name}{{border:none;color:{POPUP_BORDER};'
        f'padding:2px 4px;font-size:10px;text-align:left;}}'
        f'QPushButton#{object_name}:hover{{color:{TEXT};'
        'text-decoration:underline;}'
    )


def compact_panel_stylesheet(background: str = APP_BACKGROUND) -> str:
    """Return the flat background style for a narrow utility panel."""
    return f'background:{background};'


def side_panel_stylesheet(background: str = SURFACE) -> str:
    """Return the utility-panel style with the standard right separator."""
    return f'background:{background};border-right:1px solid {SEPARATOR};'


def colour_swatch_stylesheet(color: str, *, selected: bool = False,
                             selected_border: str = '#333') -> str:
    """Return a small data-colour swatch style with an optional selection ring."""
    border = selected_border if selected else 'transparent'
    return f'background:{color};border:2px solid {border};border-radius:3px;'


def colour_strip_stylesheet(color: str, *, radius: int = 3) -> str:
    """Return the flat colour preview strip used by markup controls."""
    return f'background:{color};border-radius:{int(radius)}px;border:none;'


def markup_flyout_stylesheet() -> str:
    """Return the small flyout shell used by markup-tool settings."""
    return (
        f'QWidget{{background:{SURFACE};border-radius:4px;}}'
        f'QLabel{{font-size:10px;color:{TEXT};border:none;}}'
    )


def palette_button_stylesheet() -> str:
    """Return the compact button style for opening a custom colour palette."""
    return (
        f'font-size:10px;border:1px solid {FIELD_BORDER};'
        'border-radius:3px;'
    )


def visibility_toggle_stylesheet() -> str:
    """Return the shared show/hide-all markup toggle style."""
    return (
        f'QPushButton{{border:none;border-radius:5px;padding:0px;'
        f'background:#27AE60;}}'
        f'QPushButton:!checked{{background:#E74C3C;}}'
    )


def visibility_layer_button_stylesheet(
        color: str, foreground: str, *, off_background: str = '#e4e7e9',
        off_foreground: str = '#7a7f83') -> str:
    """Return the tree layer-toggle style while keeping its color dynamic."""
    return (
        f'QPushButton{{background:{color};color:{foreground};border:none;'
        'border-radius:3px;font-size:10px;font-weight:bold;padding:0 4px;}'
        f'QPushButton:!checked{{background:{off_background};'
        f'color:{off_foreground};}}'
    )


def symbol_selector_stylesheet() -> str:
    """Return the compact P&ID-symbol picker shell and button style."""
    return (
        f'QFrame{{background:{SURFACE};border:1px solid {FIELD_BORDER};'
        'border-radius:6px;}'
        f'QPushButton{{border:1px solid {SEPARATOR};border-radius:4px;'
        f'background:{SUBTLE_SURFACE};padding:2px;}}'
        f'QPushButton:hover{{background:{SUBTLE_SURFACE};'
        f'border-color:{FIELD_BORDER};}}'
        f'QPushButton:checked{{background:{ACCENT};border-color:{ACCENT};}}'
    )


def symbol_selector_tab_stylesheet() -> str:
    """Return the compact tab override used by the symbol picker."""
    return (
        f'QTabBar::tab{{padding:4px 10px;font-size:9px;}}'
        f'QTabBar::tab:selected{{background:{ACCENT_SELECTION};}}'
    )


def red_markup_tool_stylesheet() -> str:
    """Return the legacy red-markup tool-button style."""
    return (
        f'QPushButton{{border:1px solid #D0D4DA;border-radius:5px;'
        f'background:{SURFACE};padding:0px;}}'
        f'QPushButton:checked{{background:#C62828;border-color:#C62828;}}'
        'QPushButton:hover:!checked{background:#FFEBEE;border-color:#EF9A9A;}'
    )


def red_markup_close_stylesheet() -> str:
    """Return the legacy red-markup close-button style."""
    return (
        'QPushButton{background:#546E7A;border:none;border-radius:5px;padding:0px;}'
        'QPushButton:hover{background:#37474F;}'
    )
