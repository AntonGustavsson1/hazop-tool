#!/usr/bin/env python3
"""HAZOP Tool — Hazard and Operability Study Manager v2"""

import sys
import re
import json
import sqlite3
import math
import datetime
import logging
import traceback
from pathlib import Path
from functools import partial
import platform
import inspect

from pid_viewer import (
    PIDPanel, COMPONENT_TYPES, CONSEQUENCE_TEMPLATES, HAS_PYMUPDF,
    MODE_NAV, MODE_NODE, MODE_CONSEQUENCE, MODE_SAFEGUARD, MODE_PICK_REF_TAG,
    scan_pdf_for_equipment, ocr_status, resolve_ocr_scan_choice, KNOWN_PREFIXES, invert_cause_text,
    _RED_MARKUP_SYMBOLS, _get_red_symbol_svg,
    _equip_prefix_from_tag,
    detect_equipment_symbols, EquipmentMarkerReviewDialog,
    apply_scan_result_to_equipment_catalog, upsert_identified_tags_from_scan,
    ParallelTagScanWorker, ParallelEquipmentAnalysisWorker,
    PageProgressDialog,
    FREQ_LABELS, freq_to_idx, idx_to_freq,
    _obj_type_matches,
)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QScrollArea,
    QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QStackedWidget,
    QTabWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout, QGridLayout,
    QLineEdit, QTextEdit, QPlainTextEdit, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QTableView,
    QComboBox, QDialog, QDialogButtonBox, QDateEdit,
    QMessageBox, QFileDialog, QGroupBox,
    QMenu, QToolBar, QStatusBar, QSizePolicy,
    QSpinBox, QDoubleSpinBox, QSlider, QColorDialog, QFrame, QListWidget, QListWidgetItem,
    QProgressDialog, QAbstractItemView, QToolTip, QInputDialog, QCheckBox,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle, QStyleOptionButton,
    QButtonGroup, QRadioButton,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QSize, QPointF, QRectF, QRect, QPoint, QTimer, QMimeData, QEvent,
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, QDate,
)
from PyQt6.QtGui import QFont, QFontMetrics, QColor, QAction, QBrush, QPen, QPainter, QDrag, QPainterPath, QPixmap, QIcon, QPolygonF, QShortcut, QKeySequence, QCursor, QPalette, QTextLayout, QTextOption, QTextCharFormat

# ══════════════════════════════════════════════════════════════════════════════
# SPLASH SCREEN — STARTUP PROGRESS
# ══════════════════════════════════════════════════════════════════════════════

class SplashScreen(QWidget):
    """Modern splash screen with progress indicator during startup."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.SplashScreen | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(400, 300)

        # Center on screen
        screen = QApplication.primaryScreen()
        geometry = screen.geometry()
        x = (geometry.width() - 400) // 2
        y = (geometry.height() - 300) // 2
        self.move(x, y)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(20)
        layout.addStretch()

        # Logo/title
        title = QLabel("HAZOP Tool")
        title.setStyleSheet("font-size: 24px; font-weight: bold; color: #17191C;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # Subtitle
        subtitle = QLabel("Startar upp...")
        subtitle.setStyleSheet("font-size: 12px; color: #666666;")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle = subtitle
        layout.addWidget(subtitle)

        # Spinner (simple rotating dots)
        spinner = QLabel("●  ○  ○")
        spinner.setStyleSheet("font-size: 16px; color: #17191C; letter-spacing: 8px;")
        spinner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spinner = spinner
        layout.addWidget(spinner)

        self._spinner_state = 0
        self._spinner_frames = [
            "●  ○  ○",
            "○  ●  ○",
            "○  ○  ●",
            "○  ●  ○",
        ]

        # Timer for spinner animation
        self._timer = QTimer()
        self._timer.timeout.connect(self._animate_spinner)
        self._timer.start(200)

        layout.addStretch()

    def _animate_spinner(self):
        self.spinner.setText(self._spinner_frames[self._spinner_state % len(self._spinner_frames)])
        self._spinner_state += 1

    def set_status(self, text):
        self.subtitle.setText(text)
        QApplication.processEvents()

    def close_splash(self):
        self._timer.stop()
        self.close()


# ══════════════════════════════════════════════════════════════════════════════
# CRASH REPORTING & DIAGNOSTICS
# ══════════════════════════════════════════════════════════════════════════════

class CrashReporter:
    """Automatic crash reporting with structured JSON output for easy analysis."""

    CRASH_DIR = Path(__file__).parent / 'crashes'

    @classmethod
    def setup(cls):
        """Install crash handler for uncaught exceptions."""
        cls.CRASH_DIR.mkdir(exist_ok=True)
        # sys.excepthook itself is installed at module load time as
        # _global_exception_hook (defined right after this class) -- not
        # here, since this method only runs once __main__ calls it.
        logging.info(f"Crash reporting initialized: {cls.CRASH_DIR}")

    @classmethod
    def handle_exception(cls, exc_type, exc_value, exc_tb):
        """Catch uncaught exceptions and generate detailed crash report."""
        try:
            report = cls.generate_report(exc_type, exc_value, exc_tb)
            cls.save_report(report)
            cls.log_report(report)
        except Exception as e:
            # If crash reporter itself fails, fall back to stderr
            print(f"Failed to generate crash report: {e}", file=sys.stderr)
            traceback.print_exception(exc_type, exc_value, exc_tb, file=sys.stderr)

    @classmethod
    def generate_report(cls, exc_type, exc_value, exc_tb):
        """Generate structured crash report with all diagnostic data."""
        # Extract full traceback with frames
        tb_frames = []
        tb = exc_tb
        while tb is not None:
            frame = tb.tb_frame
            tb_frames.append({
                'filename': frame.f_code.co_filename,
                'function': frame.f_code.co_name,
                'lineno': tb.tb_lineno,
                'locals_preview': cls._format_locals(frame.f_locals),
            })
            tb = tb.tb_next

        report = {
            'timestamp': datetime.datetime.now().isoformat(),
            'exception': {
                'type': exc_type.__name__,
                'message': str(exc_value),
                'module': exc_type.__module__,
            },
            'traceback': {
                'frames': tb_frames,
                'full_text': ''.join(traceback.format_exception(exc_type, exc_value, exc_tb)),
            },
            'environment': {
                'python_version': platform.python_version(),
                'platform': platform.platform(),
                'machine': platform.machine(),
                'processor': platform.processor(),
            },
            'imports': cls._get_module_versions(),
        }
        return report

    @classmethod
    def _format_locals(cls, locals_dict, max_len=100):
        """Format local variables for crash report (safe truncation)."""
        result = {}
        for key, val in locals_dict.items():
            if key.startswith('_'):
                continue
            try:
                val_str = repr(val)
                if len(val_str) > max_len:
                    val_str = val_str[:max_len] + '...'
                result[key] = val_str
            except Exception:
                result[key] = f'<{type(val).__name__}>'
        return result

    @classmethod
    def _get_module_versions(cls):
        """Collect versions of key dependencies."""
        versions = {}
        for module_name in ['PyQt6', 'fitz', 'easyocr', 'rapidocr_onnxruntime', 'PIL']:
            try:
                mod = __import__(module_name.split('.')[0])
                if hasattr(mod, '__version__'):
                    versions[module_name] = mod.__version__
            except (ImportError, AttributeError):
                pass
        return versions

    @classmethod
    def save_report(cls, report):
        """Save crash report as JSON file."""
        timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        exc_type = report['exception']['type']
        filename = cls.CRASH_DIR / f'crash_{timestamp}_{exc_type}.json'

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        logging.error(f"Crash report saved to: {filename}")

    @classmethod
    def log_report(cls, report):
        """Log crash summary to console and file."""
        exc = report['exception']
        logging.error(f"=== CRASH REPORT ===")
        logging.error(f"Exception: {exc['type']}: {exc['message']}")
        logging.error(f"Location: {report['traceback']['frames'][-1]['filename']}:{report['traceback']['frames'][-1]['lineno']}")
        logging.error(f"Function: {report['traceback']['frames'][-1]['function']}")

    @classmethod
    def list_crashes(cls):
        """Return list of all crash reports, most recent first."""
        if not cls.CRASH_DIR.exists():
            return []
        crashes = sorted(cls.CRASH_DIR.glob('crash_*.json'), reverse=True)
        return crashes

    @classmethod
    def get_latest_crash_summary(cls):
        """Get summary of the most recent crash (for CLI debugging)."""
        crashes = cls.list_crashes()
        if not crashes:
            return None

        latest = crashes[0]
        try:
            with open(latest, 'r', encoding='utf-8') as f:
                report = json.load(f)

            exc = report['exception']
            frames = report['traceback']['frames']

            return {
                'file': str(latest),
                'timestamp': report['timestamp'],
                'type': exc['type'],
                'message': exc['message'],
                'location': f"{frames[-1]['filename']}:{frames[-1]['lineno']}",
                'function': frames[-1]['function'],
                'full_report': report,
            }
        except Exception as e:
            logging.error(f"Failed to read crash report {latest}: {e}")
            return None


# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL EXCEPTION HOOK — keep the app alive when a Qt slot raises
# ══════════════════════════════════════════════════════════════════════════════
#
# In PyQt5.5+/PyQt6, an exception raised inside a Qt signal/slot callback
# (button click, tree selection, etc.) that is not caught anywhere propagates
# to sys.excepthook. If sys.excepthook is left at its default, PyQt prints the
# traceback and then aborts the whole process. Installing this hook means the
# offending slot call simply fails (the rest of that slot's code does not
# run), while the QApplication event loop keeps running so the user does not
# lose their whole session over one bad callback.
#
# This must be installed before app.exec() starts (module import time is
# early enough and keeps it in effect for the entire process lifetime).

def _global_exception_hook(exc_type, exc_value, exc_tb):
    """Replacement for sys.excepthook: log + report + inform the user,
    but never re-raise, sys.exit(), or os.abort() — the event loop must
    keep running afterwards."""

    # Let Ctrl+C behave normally (default handling), same as stock Python.
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return

    # Structured crash report (JSON under hazop/crashes/). Never allow a bug
    # in the crash reporter itself to take down the exception hook.
    try:
        CrashReporter.handle_exception(exc_type, exc_value, exc_tb)
    except Exception as e:
        logging.error(f"Failed to generate crash report: {e}")

    # Always also log the traceback via the standard logging module (this
    # matches the pattern used elsewhere in this file, e.g. the crash log
    # written to hazop_crash.log).
    msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logging.error('UNHANDLED EXCEPTION IN SLOT\n%s', msg)

    # Show a dialog so the user knows something went wrong, but only if a
    # QApplication is actually running (otherwise there is nothing to show
    # a dialog on top of, e.g. exceptions during module import).
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        if QApplication.instance() is not None:
            try:
                crash_dir = CrashReporter.CRASH_DIR
                QMessageBox.critical(
                    None,
                    'Ett oväntat fel uppstod',
                    f'<b>{exc_type.__name__}:</b> {exc_value}<br><br>'
                    f'Programmet försöker fortsätta köra, men kontrollera '
                    f'att inget gick fel.<br><br>'
                    f'Detaljerad felrapport sparad i:<br>'
                    f'<code>{crash_dir}</code><br><br>'
                    f'Skicka innehållet från JSON-filen när du rapporterar felet.')
            except Exception:
                pass
    except Exception:
        pass


sys.excepthook = _global_exception_hook


# ══════════════════════════════════════════════════════════════════════════════
# WINDOWS 11 THEME — LJUST TEMA
# ══════════════════════════════════════════════════════════════════════════════

def _get_windows11_stylesheet():
    """Near-monochrome theme with one signal accent, matching the design mockup.

    Deliberately no `font-size` here (2026-08-11, bug report: "möjligheten
    att förstora och förminska texten" disappeared) — a universal `* {
    font-size: ... }` rule wins over ANY later QWidget.setFont() call on a
    matching widget (the same "QSS always wins over Qt::*Role" quirk
    already documented elsewhere in this file for background/foreground
    colors), which silently broke ScenarioTablePanel's "Textstorlek"
    spinbox. The app-wide default size is set via QApplication.setFont()
    in main() instead — that one DOES yield to a widget's own setFont().
    """
    return """
    * {
        font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif;
    }

    QMainWindow, QDialog, QWidget {
        background-color: #FBFBFA;
        color: #17191C;
    }

    QPushButton {
        background-color: #FFFFFF;
        color: #17191C;
        border: 1px solid #E2E3E1;
        border-radius: 4px;
        padding: 4px 10px;
        font-weight: 500;
    }
    QPushButton:hover {
        background-color: #F5F5F3;
        border-color: #CFD1CE;
    }
    QPushButton:pressed {
        background-color: #E8E9E6;
    }
    QPushButton:checked {
        background-color: #17191C;
        color: #FFFFFF;
        border-color: #17191C;
    }
    QPushButton:focus {
        outline: 2px solid #17191C;
        outline-offset: 2px;
    }
    QPushButton:default {
        border-color: #17191C;
    }

    QCheckBox, QRadioButton { color: #17191C; spacing: 6px; }
    QCheckBox::indicator, QRadioButton::indicator {
        width: 14px; height: 14px;
        border: 1px solid #CFD1CE;
        background-color: #FFFFFF;
    }
    QCheckBox::indicator { border-radius: 3px; }
    QRadioButton::indicator { border-radius: 7px; }
    QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: #17191C; }
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {
        background-color: #17191C;
        border-color: #17191C;
    }

    QSpinBox, QDoubleSpinBox {
        background-color: #FFFFFF;
        color: #17191C;
        border: 1px solid #CFD1CE;
        border-radius: 4px;
        padding: 3px 4px;
    }
    QSpinBox:focus, QDoubleSpinBox:focus { border: 2px solid #17191C; }

    QListWidget, QListView {
        background-color: #FFFFFF;
        border: 1px solid #E2E3E1;
        border-radius: 4px;
    }
    QListWidget::item:hover, QListView::item:hover { background-color: #F5F5F3; }
    QListWidget::item:selected, QListView::item:selected {
        background-color: #E6ECFA;
        color: #17191C;
    }

    QTabWidget::pane { border: 1px solid #E2E3E1; border-radius: 4px; }
    QTabBar::tab {
        background-color: #F5F5F3;
        color: #17191C;
        padding: 5px 12px;
        border: 1px solid #E2E3E1;
        border-bottom: none;
        border-top-left-radius: 4px;
        border-top-right-radius: 4px;
    }
    QTabBar::tab:hover { background-color: #E8E9E6; }
    QTabBar::tab:selected {
        background-color: #FFFFFF;
        border-bottom: 2px solid #17191C;
    }

    QLineEdit, QTextEdit, QPlainTextEdit {
        background-color: #FFFFFF;
        color: #17191C;
        border: 1px solid #CFD1CE;
        border-radius: 4px;
        padding: 4px 6px;
        selection-background-color: #17191C;
        selection-color: #FFFFFF;
    }
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
        border: 2px solid #17191C;
        padding: 3px 5px;
    }

    QComboBox {
        background-color: #FFFFFF;
        color: #17191C;
        border: 1px solid #CFD1CE;
        border-radius: 4px;
        padding: 4px 8px;
    }
    QComboBox:focus { border: 2px solid #17191C; }
    QComboBox::drop-down { border: none; width: 20px; }
    QComboBox::down-arrow { image: none; }

    QTableWidget, QTableView {
        background-color: #FFFFFF;
        alternate-background-color: #F5F5F3;
        gridline-color: #EEEFEC;
        border: 1px solid #E2E3E1;
        border-radius: 4px;
    }
    QTableWidget::item, QTableView::item { padding: 4px; border: none; }
    QTableWidget::item:selected, QTableView::item:selected {
        background-color: #E6ECFA;
        color: #17191C;
    }
    QHeaderView::section {
        background-color: #F5F5F3;
        color: #8D9299;
        padding: 4px;
        border: none;
        border-bottom: 1px solid #E2E3E1;
        font-weight: 600;
        font-size: 8pt;
        letter-spacing: 0.5px;
    }

    QTreeWidget, QTreeView {
        background-color: #FFFFFF;
        alternate-background-color: #F5F5F3;
        border: 1px solid #E2E3E1;
        border-radius: 4px;
    }
    QTreeWidget::item:hover, QTreeView::item:hover { background-color: #F5F5F3; }
    QTreeWidget::item:selected, QTreeView::item:selected {
        background-color: #E6ECFA;
        color: #17191C;
    }

    QFrame { background-color: #FFFFFF; border: none; }
    QGroupBox {
        color: #17191C;
        border: 1px solid #E2E3E1;
        border-radius: 6px;
        margin-top: 8px;
        padding-top: 8px;
    }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; color: #8D9299; }

    QScrollBar:vertical { background-color: #F5F5F3; border: none; width: 12px; }
    QScrollBar::handle:vertical { background-color: #CFD1CE; border-radius: 6px; margin: 2px; min-height: 20px; }
    QScrollBar::handle:vertical:hover { background-color: #B3B7B2; }
    QScrollBar:horizontal { background-color: #F5F5F3; border: none; height: 12px; }
    QScrollBar::handle:horizontal { background-color: #CFD1CE; border-radius: 6px; margin: 2px; min-width: 20px; }
    QScrollBar::handle:horizontal:hover { background-color: #B3B7B2; }
    QScrollBar::add-line, QScrollBar::sub-line { border: none; background: none; }

    QDialog { background-color: #FFFFFF; }
    QSplitter::handle { background-color: #E2E3E1; }
    QSplitter::handle:hover { background-color: #CFD1CE; }

    QMenuBar { background-color: #FFFFFF; color: #17191C; border-bottom: 1px solid #E2E3E1; }
    QMenuBar::item:selected { background-color: #F5F5F3; }
    QMenu { background-color: #FFFFFF; color: #17191C; border: 1px solid #CFD1CE; border-radius: 4px; }
    QMenu::item:selected { background-color: #F5F5F3; }

    QToolTip {
        background-color: #17191C;
        color: #FFFFFF;
        border: none;
        border-radius: 4px;
        padding: 4px 8px;
    }
    """

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION & MAGIC NUMBER CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    # ===== TIMERS (milliseconds) =====
    'TIMER_DEFERRED_MS': 0,           # Deferred execution
    'TIMER_NAV_QUICK_MS': 50,         # Quick navigation/pan
    'TIMER_ZOOM_MS': 80,              # Zoom animation
    'TIMER_EDIT_START_MS': 200,       # Start edit deferred
    'TIMER_PDF_EXTRACT_MS': 100,      # PDF line extraction

    # ===== WIDGET HEIGHTS (pixels) =====
    'H_SEP_LINE': 1,                  # Separator line
    'H_COLOR_STRIP': 7,               # Color strip
    'H_BADGE': 20,                    # Icon/badge
    'H_BTN_SMALL': 22,                # Small buttons
    'H_CTRL_STD': 24,                 # Standard control
    'H_ROW_COMPACT': 26,              # Compact row
    'H_ROW_STD': 28,                  # Standard row
    'H_BTN_OK': 34,                   # OK/Cancel button
    'H_DESC_SM': 55,                  # Small description
    'H_DESC_MD': 65,                  # Medium description
    'H_DESC_LG': 80,                  # Large description
    'H_EDIT_LG': 100,                 # Large text editor
    'H_PANEL_MIN_LG': 120,            # Large panel minimum
    'H_FREQ_MAX': 140,                # Frequency panel max
    'H_TABLE_MIN': 150,               # Table minimum
    'H_TABLE_STD': 160,               # Standard table
    'H_PANEL_MAX': 300,               # Panel maximum
    'H_PANEL_MAX_ALT': 380,           # Alternative max
    'H_PANEL_MIN_XL': 520,            # Extra-large panel
    'H_PANEL_MIN_XXL': 560,           # XXL panel

    # ===== WIDGET WIDTHS (pixels) =====
    'W_LABEL_PCT': 10,                # Percentage label
    'W_ICON_BTN': 28,                 # Icon button
    'W_OPACITY_LBL': 36,              # Opacity label
    'W_LABEL_MD': 42,                 # Medium label
    'W_CORNER': 50,                   # Corner widget
    'W_BTN_COMPACT': 52,              # Compact button
    'W_SPINNER': 58,                  # Spinner width
    'W_FREQ_LBL': 88,                 # Frequency label
    'W_COL_MD': 100,                  # Medium column
    'W_COL_LG': 120,                  # Large column
    'W_CAT_LBL': 130,                 # Category label
    'W_DIALOG_MIN': 260,              # Min dialog
    'W_PANEL_MIN': 280,               # Min panel
    'W_DIALOG_MD': 300,               # Medium dialog
    'W_DIALOG_LG': 320,               # Large dialog
    'W_DIALOG_XL': 340,               # Extra-large dialog
    'W_PANEL_MIN_MD': 460,            # Medium panel min
    'W_PANEL_MIN_LG': 480,            # Large panel min
    'W_PANEL_MIN_XL': 500,            # XL panel min
    'W_PANEL_MIN_XXL': 640,           # XXL panel min

    # ===== SEMANTIC ZONE WIDTHS (pixel regions in cells) =====
    'ZONE_PID_ICON': 22,              # P&ID pin icon
    'ZONE_CONS_CAT': 26,              # Consequence category
    'ZONE_CONS_CHAIN': 24,            # Consequence chain link
    'ZONE_CAUSE_OBJ': 64,             # Cause object-tag
    'ZONE_CAUSE_COMMENT': 22,         # Cause comment icon
    'ZONE_CAUSE_CLONE': 22,           # Cause clone icon
    'ZONE_CAUSE_FREQ': 50,            # Cause frequency badge
    'ZONE_SG_RRF': 54,                # Safeguard RRF badge
    'ZONE_EQUIP_ICON': 20,            # Equipment icon
}

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

DB_PATH = Path(__file__).parent / "hazop_project.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL DEFAULT 'Ny nod',
    description TEXT DEFAULT '',
    pid_ref     TEXT DEFAULT '',
    media       TEXT DEFAULT '',
    pressure    TEXT DEFAULT '',
    temperature TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS causes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id     INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    description TEXT NOT NULL DEFAULT 'Ny orsak',
    likelihood  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS consequences (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    cause_id          INTEGER NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
    description       TEXT NOT NULL DEFAULT 'Ny konsekvens',
    severity          INTEGER NOT NULL DEFAULT 1,
    category          TEXT DEFAULT '',
    consequence_chain TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS safeguards (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consequence_id  INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
    description     TEXT NOT NULL DEFAULT '',
    rrf             INTEGER NOT NULL DEFAULT 1,
    source_id       INTEGER DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS reduction_factors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consequence_id  INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
    description     TEXT NOT NULL DEFAULT '',
    rrf             INTEGER NOT NULL DEFAULT 10,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consequence_id  INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
    description     TEXT NOT NULL DEFAULT '',
    responsible     TEXT DEFAULT '',
    due_date        TEXT DEFAULT '',
    status          TEXT DEFAULT 'Öppen'
);

CREATE TABLE IF NOT EXISTS app_config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS consequence_categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS component_types (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS failure_modes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id INTEGER NOT NULL REFERENCES component_types(id) ON DELETE CASCADE,
    description  TEXT NOT NULL DEFAULT '',
    freq_per_year REAL DEFAULT NULL,
    sort_order   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reduction_factors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    consequence_id  INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
    description     TEXT NOT NULL DEFAULT '',
    rrf             INTEGER NOT NULL DEFAULT 10,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS equipment_types (
    prefix          TEXT PRIMARY KEY,
    equipment_type  TEXT NOT NULL,
    display_name    TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tag_database (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_code    TEXT NOT NULL,
    name_sv     TEXT DEFAULT '',
    name_en     TEXT DEFAULT '',
    category    TEXT DEFAULT '',
    standard    TEXT DEFAULT '',
    source      TEXT DEFAULT 'excel',
    active      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tag_database_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS pid_identified_tags (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tag_code     TEXT NOT NULL UNIQUE,
    examples     TEXT DEFAULT '',
    name_sv      TEXT DEFAULT '',
    comp_type    TEXT DEFAULT '',
    confirmed    INTEGER DEFAULT 0,
    source       TEXT DEFAULT 'scan'
);

CREATE TABLE IF NOT EXISTS equipment_catalog (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tag            TEXT NOT NULL,
    original_tag   TEXT DEFAULT '',
    prefix         TEXT DEFAULT '',
    pid_page       INTEGER DEFAULT 0,
    equipment_type TEXT DEFAULT '',
    description    TEXT DEFAULT '',
    is_ocr         INTEGER DEFAULT 0,
    include        INTEGER DEFAULT 1
);
"""

# Frequency axis: F=-1..5 (7 levels, logarithmic events/year)
# Consequence axis: C=1..5 (5 levels)
DEFAULT_MATRIX = {
    'rows': 5,   # consequence rows, index 0 = C1 (lowest)
    'cols': 7,   # frequency columns, index 0 = F-1 (lowest)
    'x_axis': 'frequency',
    'x_labels': [
        'F-1 – Otänkbar (<1/100 000 år)',
        'F0 – Extremt sällan (1/100 000 år)',
        'F1 – Sällan (1/10 000 år)',
        'F2 – Osannolik (1/1 000 år)',
        'F3 – Möjlig (1/100 år)',
        'F4 – Trolig (1–10 år)',
        'F5 – Frekvent (>1/år)',
    ],
    'y_labels': [
        'C1 – Försumbar',
        'C2 – Liten',
        'C3 – Måttlig',
        'C4 – Allvarlig',
        'C5 – Katastrofal',
    ],
    'cell_colors': [
        # C=1: F-1 → F5
        ['#27ae60', '#27ae60', '#27ae60', '#27ae60', '#f39c12', '#e67e22', '#e74c3c'],
        # C=2
        ['#27ae60', '#27ae60', '#27ae60', '#f39c12', '#e67e22', '#e74c3c', '#e74c3c'],
        # C=3
        ['#27ae60', '#27ae60', '#f39c12', '#e67e22', '#e74c3c', '#e74c3c', '#e74c3c'],
        # C=4
        ['#27ae60', '#f39c12', '#e67e22', '#e74c3c', '#e74c3c', '#e74c3c', '#e74c3c'],
        # C=5
        ['#f39c12', '#e67e22', '#e74c3c', '#e74c3c', '#e74c3c', '#e74c3c', '#e74c3c'],
    ],
    'cell_labels': [
        ['Låg',    'Låg',    'Låg',    'Låg',    'Medium', 'Hög',    'Kritisk'],
        ['Låg',    'Låg',    'Låg',    'Medium', 'Hög',    'Kritisk','Kritisk'],
        ['Låg',    'Låg',    'Medium', 'Hög',    'Kritisk','Kritisk','Kritisk'],
        ['Låg',    'Medium', 'Hög',    'Kritisk','Kritisk','Kritisk','Kritisk'],
        ['Medium', 'Hög',    'Kritisk','Kritisk','Kritisk','Kritisk','Kritisk'],
    ],
}

# ── Risk Matrix Caching with Automatic Invalidation ──────────────────────────
# The risk matrix is read from the database and cached for performance. When a
# matrix is updated via set_risk_matrix(), the cache must be invalidated to ensure
# all subsequent get_matrix() calls reflect the new state. Using a dedicated
# manager class ensures invalidation is automatic when the setter is called.

class _RiskMatrixCache:
    """Manager for risk matrix caching with automatic invalidation support."""
    def __init__(self):
        self._current_matrix = None
        self._db = None

    def load(self, db):
        """Load and cache the risk matrix from database."""
        self._db = db
        cfg = db.get_risk_matrix()
        if cfg:
            self._current_matrix = _normalise_matrix(cfg)
        else:
            self._current_matrix = DEFAULT_MATRIX

    def invalidate(self):
        """Invalidate the cache; next get() will reload from DB."""
        self._current_matrix = None

    def get(self):
        """Get the current cached matrix (or DEFAULT if not loaded)."""
        return self._current_matrix or DEFAULT_MATRIX

    def reload_from_db(self):
        """Force reload from database (used after set_risk_matrix)."""
        if self._db:
            self.load(self._db)


_risk_matrix_cache = _RiskMatrixCache()


def _normalise_matrix(cfg: dict) -> dict:
    """Ensure a stored matrix config is internally consistent.

    Pads x_labels / y_labels and cell arrays to match rows/cols.
    Used once on load so the rest of the code can trust the structure.
    """
    rows = int(cfg.get('rows', 5))
    cols = int(cfg.get('cols', 7))

    # Pad or trim x_labels
    x = list(cfg.get('x_labels', []))
    while len(x) < cols:
        x.append(f'F{len(x) - 1}')
    cfg['x_labels'] = x[:cols]

    # Pad or trim y_labels
    y = list(cfg.get('y_labels', []))
    while len(y) < rows:
        y.append(f'C{len(y) + 1}')
    cfg['y_labels'] = y[:rows]

    # Pad or trim cell_colors / cell_labels
    def _pad_grid(grid, default_val):
        result = []
        for r in range(rows):
            row = list(grid[r]) if r < len(grid) else []
            while len(row) < cols:
                row.append(default_val)
            result.append(row[:cols])
        return result

    cfg['cell_colors']    = _pad_grid(cfg.get('cell_colors', []), '#27ae60')
    cfg['cell_labels']    = _pad_grid(cfg.get('cell_labels', []), 'Låg')
    cfg['cell_fg_colors'] = _pad_grid(cfg.get('cell_fg_colors', []), '#ffffff')
    cfg['rows'] = rows
    cfg['cols'] = cols
    return cfg


def load_matrix(db):
    """Load risk matrix from database into the cache."""
    _risk_matrix_cache.load(db)


def get_matrix():
    """Get the currently cached risk matrix."""
    return _risk_matrix_cache.get()


def risk_info(frequency, consequence):
    """Return (label, bg_color, fg_color) from matrix lookup.

    Data is always stored as cell_colors[cons_idx][freq_idx].
    x_axis only controls display orientation — not data access.
    """
    cfg   = get_matrix()
    rows  = cfg.get('rows', 5)   # consequence levels
    cols  = cfg.get('cols', 7)   # frequency levels
    c_idx = max(0, min(int(consequence) - 1, rows - 1))   # C=1 → 0
    f_idx = max(0, min(int(frequency)  + 1, cols - 1))   # F=-1 → 0
    try:
        color = cfg['cell_colors'][c_idx][f_idx]   # always [cons][freq]
        label = cfg['cell_labels'][c_idx][f_idx]
        if not color:
            color = '#27ae60'
        if not label:
            label = 'Låg'
    except (IndexError, KeyError, TypeError):
        color, label = '#27ae60', 'Låg'
    try:
        fg = cfg['cell_fg_colors'][c_idx][f_idx] or '#ffffff'
    except (IndexError, KeyError, TypeError):
        fg = '#ffffff'
    return label, color, fg


def _contrast_fg(bg_hex):
    """Return black or white text color for best contrast against bg_hex."""
    c = QColor(bg_hex)
    luminance = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255
    return '#000000' if luminance > 0.55 else '#ffffff'


def freq_axis_label(f_val: int) -> str:
    """Short configured label (first word only) for a frequency value (-1..5)."""
    cfg  = get_matrix()
    cols = cfg.get('cols', 7)
    idx  = max(0, min(int(f_val) + 1, cols - 1))
    lbls = cfg.get('x_labels', [])
    full = lbls[idx] if idx < len(lbls) else f'F={f_val}'
    return full.split()[0] if full.strip() else f'F={f_val}'


def freq_axis_label_full(f_val: int) -> str:
    """Full configured label for a frequency value (-1..5), e.g. 'F3 – Möjlig (1/100 år)'."""
    cfg  = get_matrix()
    cols = cfg.get('cols', 7)
    idx  = max(0, min(int(f_val) + 1, cols - 1))
    lbls = cfg.get('x_labels', [])
    return lbls[idx] if idx < len(lbls) else f'F={f_val}'


def cons_axis_label(c_val: int) -> str:
    """Short configured label for a consequence value (1..5). y_labels always stores cons labels."""
    cfg  = get_matrix()
    rows = cfg.get('rows', 5)
    idx  = max(0, min(int(c_val) - 1, rows - 1))
    lbls = cfg.get('y_labels', [])
    full = lbls[idx] if idx < len(lbls) else f'C={c_val}'
    return full.split()[0] if full.strip() else f'C={c_val}'


def effective_f_level(f_level, rrf):
    """Reduce F-level by floor(log10(rrf)) steps; minimum F=-1."""
    if rrf <= 1:
        return f_level
    reduction = int(math.log10(max(1, rrf)))
    return max(-1, f_level - reduction)


# Keep old names as aliases for backward compatibility
effective_frequency = effective_f_level
effective_likelihood = effective_f_level


def prob_to_reduction(prob_pct) -> int:
    """Convert probability % to frequency step reduction.

    10%  → 1 step  (≈ RRF 10)
    1%   → 2 steps (≈ RRF 100)
    0.1% → 3 steps (≈ RRF 1000)
    ≥100% or ≤0% → 0 steps
    """
    try:
        p = float(prob_pct)
    except (TypeError, ValueError):
        return 0
    if p <= 0 or p >= 100:
        return 0
    return int(math.floor(-math.log10(p / 100.0)))


def total_freq_reduction(base_f_level: int, safeguard_rrf: int,
                         fa_active: bool, fa_prob,
                         ignition_active: bool, ignition_prob,
                         extra_rfactors) -> tuple:
    """Return (final_f_level, total_equivalent_rrf, total_steps).

    fa_prob / ignition_prob: probability in % (10.0 = 10% = −1 step).
    extra_rfactors: iterable of dicts with 'rrf' (also treated as %) and 'active'.
    """
    # Safeguards reduce by RRF steps
    sg_steps    = int(math.log10(max(1, safeguard_rrf))) if safeguard_rrf > 1 else 0
    fa_steps    = prob_to_reduction(fa_prob)    if fa_active    else 0
    ign_steps   = prob_to_reduction(ignition_prob) if ignition_active else 0
    extra_steps = sum(
        prob_to_reduction(rf.get('rrf', 10))
        for rf in extra_rfactors
        if rf.get('active')
    )
    total_steps = sg_steps + fa_steps + ign_steps + extra_steps
    total_rrf   = 10 ** total_steps if total_steps > 0 else 1
    return max(-1, base_f_level - total_steps), total_rrf, total_steps


# ── Consequence chain definitions ────────────────────────────────────────────
# Each entry: (key, display_label, group_header_or_None)
CHAIN_ITEMS = [
    # Intermediate event
    ('loc',           'LOC — Utsläpp / läcka',                    'Intermediär händelse'),
    # Ignition outcomes
    ('fire',          'Brand (pool fire / jet fire)',              'Antändning / explosion'),
    ('flash_fire',    'Flash fire',                                None),
    ('explosion',     'Explosion (VCE / BLEVE)',                   None),
    # Toxic / environmental
    ('toxic',         'Toxisk exponering',                         'Toxisk / miljö'),
    ('environmental', 'Miljöutsläpp',                              None),
    # Human / asset
    ('personnel',     'Personskador',                              'Personell / tillgång'),
    ('fatality',      'Dödsfall',                                  None),
    ('equipment',     'Utrustningsskador',                         None),
    ('production',    'Driftstopp / produktionsbortfall',          None),
    # User-defined
    ('custom',        'Övrigt (se text)',                          'Övrigt'),
]
CHAIN_KEYS = [k for k, _, _ in CHAIN_ITEMS]


def build_consequence_text(base: str, chain: dict) -> str:
    """Build full consequence description from base event + chain selections."""
    parts = [base.strip()] if base.strip() else []
    for key, label, _ in CHAIN_ITEMS:
        if chain.get(key):
            # Use short label for the chain (without parenthetical detail)
            short = label.split('(')[0].strip().split(' — ')[-1].strip()
            parts.append(short)
    return ' → '.join(parts)


def append_tag_to_text(description: str, tag: str) -> str:
    """Append an equipment tag to the end of a free-text description with
    a single separating space (2026-08-09, see NOTES.md) — used when an
    equipment marker is drag-and-dropped onto a KON/SG cell, building a
    running sentence ("hög nivå i" + drop TA-1 -> "hög nivå i TA-1", then
    "... => överbreddning till" + drop TA-2 -> "... => överbreddning
    till TA-2") instead of overwriting a separate tag field. Starting
    from the still-untouched default placeholder text replaces it
    outright rather than appending to boilerplate nobody wrote."""
    description = description or ''
    tag = (tag or '').strip()
    if not tag:
        return description
    stripped = description.strip()
    if not stripped or stripped in ('Ny konsekvens', 'Ny safeguard', 'Ny orsak'):
        return tag
    if description[-1].isspace():
        return description + tag
    return description + ' ' + tag


def parse_tag_refs(raw: str) -> list:
    """Decode tagged_refs (comma-separated, order preserved) into a list
    of tag strings — every tag ever drag-appended into a KON/SG cell's
    free text, used to bold those substrings when rendering the cell
    (2026-08-09, see NOTES.md)."""
    if not raw:
        return []
    return [t for t in (s.strip() for s in raw.split(',')) if t]


def add_tag_ref(raw: str, tag: str) -> str:
    """Append tag to the tagged_refs list, deduplicated, order preserved
    (the most recent drop moves to the end)."""
    tag = (tag or '').strip()
    if not tag:
        return raw or ''
    refs = [t for t in parse_tag_refs(raw) if t != tag]
    refs.append(tag)
    return ','.join(refs)


def find_tag_bold_ranges(text: str, tags: list) -> list:
    """Return sorted, non-overlapping (start, end) character ranges in
    `text` where a member of `tags` occurs as a whole word — not as a
    substring of a larger word/tag (e.g. tag "TA-1" must not match inside
    "TA-10") — used to bold drag-and-dropped equipment references within
    a free-text description (2026-08-09, see NOTES.md)."""
    ranges = []
    for tag in tags:
        tag = (tag or '').strip()
        if not tag:
            continue
        start = 0
        while True:
            idx = text.find(tag, start)
            if idx < 0:
                break
            end = idx + len(tag)
            before_ok = idx == 0 or not text[idx - 1].isalnum()
            after_ok = end == len(text) or not text[end].isalnum()
            if before_ok and after_ok:
                ranges.append((idx, end))
            start = idx + 1
    ranges.sort()
    merged = []
    for s, e in ranges:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def parse_chain_from_json(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


# Frequency F=-1..5, stored as integer in causes.likelihood.
# FREQ_LABELS/freq_to_idx/idx_to_freq now live in pid_viewer.py (imported
# above) since EquipmentDeviationBar needs them too and pid_viewer.py
# cannot import back from hazop.py without a circular import.
_FREQ_VALUES = [-1, 0, 1, 2, 3, 4, 5]

# Default frequency boundaries (events/year) between each F-column.
# 6 boundaries for 7 columns (F=-1..F5).
# freq < boundaries[0]       → F=-1
# boundaries[i] <= freq < boundaries[i+1] → F=i
# freq >= boundaries[5]      → F=5
DEFAULT_FREQ_BOUNDARIES = [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]


def freq_to_f_level(freq_per_year, boundaries=None) -> int:
    """Convert numeric frequency (events/year) to F-level (-1..5).

    0.05/year → F=3  (10-100 year interval)
    0.5/year  → F=4  (1-10 year interval)
    """
    if boundaries is None:
        cfg = get_matrix()
        boundaries = cfg.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES)
    boundaries = sorted(float(b) for b in boundaries)
    if not freq_per_year or freq_per_year <= 0:
        return -1
    for i, b in enumerate(boundaries):
        if float(freq_per_year) < b:
            return i - 1
    return len(boundaries) - 1   # above all → F=5


# Keep old alias so existing code that references LIKE_LABELS doesn't crash
LIKE_LABELS = FREQ_LABELS

SEV_LABELS  = ['C1 – Försumbar', 'C2 – Liten', 'C3 – Måttlig', 'C4 – Allvarlig', 'C5 – Katastrofal']


def get_sev_labels():
    """Return severity labels from current matrix config (y_labels), falling back to SEV_LABELS."""
    cfg = get_matrix()
    y = cfg.get('y_labels', [])
    n = cfg.get('rows', 5)
    if y and len(y) >= n:
        return [f"C{i+1} – {y[i]}" if not y[i].startswith('C') else y[i] for i in range(n)]
    return SEV_LABELS[:n] if n <= len(SEV_LABELS) else SEV_LABELS + [f"C{i+1}" for i in range(len(SEV_LABELS), n)]


RRF_VALUES  = [1, 10, 100, 1000, 10000]
RRF_LABELS  = ['1 – Ingen', '10 – RRF10', '100 – RRF100', '1000 – RRF1000', '10000 – RRF10000']
SG_TYPES      = ['BPCS', 'SIS', 'Mekanisk', 'Administrativ', 'Övrigt']
MARKUP_COLORS = ['#E53935', '#F57C00', '#F9A825', '#388E3C',
                  '#00796B', '#1565C0', '#7B1FA2', '#FF4081']
RISK_ICON   = {'Låg': '🟢', 'Medium': '🟡', 'Hög': '🟠', 'Kritisk': '🔴'}

def _get_node_color(node_id):
    """Get a unique color for a node based on its ID."""
    return MARKUP_COLORS[node_id % len(MARKUP_COLORS)]

# Component-specific standard causes seeded on first run.
# comp_type must match keys in COMPONENT_TYPES (pid_viewer.py).
# ── Standardorsaker per avvikelse och objekt ──────────────────────────────────
# Format: {avvikelse: {objektnamn: [(beskrivning, frekvens_per_år | None)]}}
# Frekvenser är typvärden (OREDA / processsindustri) — justera per projekt.
# Generiska beskrivningar: täcker orsaken, inte det specifika scenariot.
_COMP_STD_CAUSES = {
    # ── Lågt flöde ────────────────────────────────────────────────────────────
    "Lågt flöde": {
        "Manuell ventil":     [("Ventil stängd / delvis stängd",       1e-3),
                               ("Ventil blockerad (igensättning)",      5e-4),
                               ("Blind platta / blindning kvarglömd",   1e-4)],
        "On-off ventil":      [("Ventil felar stängd (fail-closed)",    1e-2),
                               ("Ventil fastnar i stängt läge",         5e-3),
                               ("Manöversignal uteblir",                1e-2)],
        "Reglerventil":       [("Reglerventil felar stängd",            2e-2),
                               ("Ventil fastnar / stiction",            1e-2),
                               ("Felaktig styrsignal — lågt utflöde",   5e-3)],
        "Backventil":         [("Backventil fastnar stängd",            1e-2),
                               ("Backventil monterad baklänges",        1e-4)],
        "Pump":               [("Pump stopp",                           2e-2),
                               ("Reducerad pumpkapacitet",              1e-2),
                               ("Kavitation",                           5e-3),
                               ("Inlopp blockerat",                     5e-3)],
        "Kompressor / fläkt": [("Kompressor / fläkt stopp",            2e-2),
                               ("Reducerad kapacitet",                  1e-2),
                               ("Inloppsfilter igensatt",               5e-2)],
        "Filter / sil":       [("Filter / sil igensatt",               0.1),
                               ("Filterelement felaktigt monterat",     1e-3)],
        "Värmeväxlare / kylare / värmare": [
                               ("Rör igensatta — fouling",              5e-2),
                               ("Vakuumbrott / tömning",                1e-3)],
        "Tank / kärl / kolonn":[("Låg nivå i matningskärl",            5e-2),
                               ("Utlopp stängt / nivåstyrning",         1e-2)],
        "Rörledning / slang": [("Igensatt rörledning",                  5e-3),
                               ("Luftlås / hydrater / is",              1e-3)],
        "Instrument":         [("Flödesgivare felar — styrventil stänger", 0.1),
                               ("Börvärde felaktigt inställt",          1e-2)],
        "Styrsystem / PLC / DCS": [("Styrsignal felar — ventil stänger", 5e-3),
                               ("Kommunikationsavbrott",                1e-2)],
    },

    # ── Högt flöde ────────────────────────────────────────────────────────────
    "Högt flöde": {
        "Manuell ventil":     [("Ventil öppnad felaktigt",              1e-3),
                               ("Bypassventil öppnad",                  5e-4)],
        "On-off ventil":      [("Ventil felar öppen (fail-open)",       1e-2),
                               ("Ventil fastnar i öppet läge",          5e-3)],
        "Reglerventil":       [("Reglerventil felar öppen",             2e-2),
                               ("Felaktig styrsignal — högt utflöde",   5e-3)],
        "Pump":               [("Pumpkapacitet för hög",                5e-3),
                               ("Frekvensomformare — fel varvtal",      1e-2)],
        "Kompressor / fläkt": [("Kompressor — för hög kapacitet",       5e-3)],
        "Tank / kärl / kolonn":[("Övertryck driver högre flöde",        1e-2)],
        "Instrument":         [("Flödesgivare felar — styrventil öppnar", 0.1),
                               ("Börvärde felaktigt högt",              1e-2)],
        "Styrsystem / PLC / DCS": [("Styrsignal felar — ventil öppnar", 5e-3)],
    },

    # ── Högt tryck ────────────────────────────────────────────────────────────
    "Högt tryck": {
        "Manuell ventil":     [("Utloppsventil stängd",                 1e-3),
                               ("Ventil blockerad",                     5e-4)],
        "On-off ventil":      [("Utloppsventil felar stängd",           1e-2),
                               ("Ventil fastnar stängd på utlopp",      5e-3)],
        "Reglerventil":       [("Reglerventil på utlopp felar stängd",  2e-2),
                               ("Felaktig tryckreglering",              5e-3)],
        "Pump":               [("Pump deadhead — utlopp blockerat",     5e-3)],
        "Värmeväxlare / kylare / värmare": [
                               ("Kylningsbortfall",                     5e-3),
                               ("Termisk expansion utan ventilering",   1e-3)],
        "Tank / kärl / kolonn":[("Blockerat avluftningssystem",         5e-4)],
        "Säkerhetsventil / sprängbleck": [
                               ("Säkerhetsventil felar stängd",         1e-3),
                               ("Sprängbleck defekt",                   5e-4)],
        "Instrument":         [("Trycktransmitter felar — styrventil stänger", 0.1),
                               ("Börvärde tryckreglering felaktigt",    1e-2)],
        "Styrsystem / PLC / DCS": [("Tryckreglering felar",             5e-3)],
        "Rörledning / slang": [("Blockerad utloppsledning",             5e-4)],
        "Kompressor / fläkt": [("Kompressorsurge",                      1e-2)],
        "Backventil":         [("Backventil blockerar utflöde",         5e-3)],
        "Filter / sil":       [("Filter igensatt — tryckstegring uppströms", 0.1)],
    },

    # ── Lågt tryck ────────────────────────────────────────────────────────────
    "Lågt tryck": {
        "Manuell ventil":     [("Dräneringsventil öppnad",              5e-4),
                               ("Läckage via öppen ventil",             1e-3)],
        "On-off ventil":      [("Utloppsventil felar öppen",            1e-2),
                               ("Avblåsningsventil fastnar öppen",      5e-3)],
        "Reglerventil":       [("Reglerventil felar öppen",             2e-2)],
        "Pump":               [("Pump stopp — tryckfall",               2e-2)],
        "Rörledning / slang": [("Rörläckage / slangbrott",              5e-4),
                               ("Packningsläckage",                     1e-3)],
        "Fläns / koppling / packning": [
                               ("Packningsläckage",                     2e-3),
                               ("Flänsläckage",                         5e-4)],
        "Säkerhetsventil / sprängbleck": [
                               ("Säkerhetsventil öppnar för tidigt",    1e-3),
                               ("Sprängbleck utlöst",                   1e-4)],
        "Instrument":         [("Tryckmätare felar — styrventil öppnar", 0.1)],
        "Tank / kärl / kolonn":[("Kärl dränerat",                       5e-3)],
    },

    # ── Hög nivå ──────────────────────────────────────────────────────────────
    "Hög nivå": {
        "Manuell ventil":     [("Utloppsventil stängd",                 1e-3),
                               ("Inloppsventil öppnad utan utlopp",     5e-4)],
        "On-off ventil":      [("Utloppsventil felar stängd",           1e-2),
                               ("Inloppsventil felar öppen",            1e-2)],
        "Reglerventil":       [("Utloppsreglering felar stängd",        2e-2),
                               ("Inloppsreglering felar öppen",         1e-2)],
        "Pump":               [("Utloppspump stopp",                    2e-2)],
        "Instrument":         [("Nivågivare felar — reglering stänger utlopp", 0.1),
                               ("Börvärde nivå felaktigt",              1e-2)],
        "Tank / kärl / kolonn":[("Inflöde > utflöde",                  5e-3)],
        "Styrsystem / PLC / DCS": [("Nivåreglering felar",              5e-3)],
        "Backventil":         [("Backventil läcker — backflöde till kärl", 5e-3)],
    },

    # ── Låg nivå ──────────────────────────────────────────────────────────────
    "Låg nivå": {
        "Manuell ventil":     [("Inloppsventil stängd",                 1e-3),
                               ("Dräneringsventil öppnad",              5e-4)],
        "On-off ventil":      [("Inloppsventil felar stängd",           1e-2),
                               ("Utloppsventil felar öppen",            1e-2)],
        "Reglerventil":       [("Inloppsreglering felar stängd",        2e-2),
                               ("Utloppsreglering felar öppen",         1e-2)],
        "Pump":               [("Inloppspump stopp",                    2e-2),
                               ("Pumpläckage / tätningsfel",            5e-3)],
        "Rörledning / slang": [("Rörläckage",                           5e-4)],
        "Instrument":         [("Nivågivare felar — reglering öppnar utlopp", 0.1)],
        "Tank / kärl / kolonn":[("Läckage via botten / sida",           5e-4)],
    },

    # ── Hög temperatur ────────────────────────────────────────────────────────
    "Hög temperatur": {
        "Manuell ventil":     [("Kylmediumventil stängd",               5e-4),
                               ("Värmemediumventil öppnad",             5e-4)],
        "Reglerventil":       [("Kylventil felar stängd",               2e-2),
                               ("Värmeventil felar öppen",              1e-2)],
        "Värmeväxlare / kylare / värmare": [
                               ("Kylningsbortfall",                     5e-3),
                               ("Värmetillförsel okontrollerad",        1e-3)],
        "Instrument":         [("Temperaturgivare felar — kylning stängs", 0.1)],
        "Tank / kärl / kolonn":[("Exoterm reaktion",                    1e-4),
                               ("Extern värmetillförsel",               1e-4)],
        "Rörledning / slang": [("Isolationsfel / brandpåverkan",        5e-4)],
        "Styrsystem / PLC / DCS": [("Temperaturreglering felar",        5e-3)],
        "Pump":               [("Pumpfriktionsvärme",                   5e-3)],
        "Kompressor / fläkt": [("Kompressionsöverhettning",             1e-2)],
    },

    # ── Låg temperatur ────────────────────────────────────────────────────────
    "Låg temperatur": {
        "Manuell ventil":     [("Värmemediumventil stängd",             5e-4),
                               ("Kylmediumventil öppnad",               5e-4)],
        "Reglerventil":       [("Värmeventil felar stängd",             2e-2),
                               ("Kylventil felar öppen",                1e-2)],
        "Värmeväxlare / kylare / värmare": [
                               ("Värmebortfall",                        5e-3),
                               ("Överkylning",                          1e-3)],
        "Instrument":         [("Temperaturgivare felar — värmning stängs", 0.1)],
        "Rörledning / slang": [("Frysrisk — isolationsbortfall",        1e-3)],
        "Tank / kärl / kolonn":[("Endoterm reaktion / avdunstning",     1e-4)],
    },

    # ── Omvänt flöde ─────────────────────────────────────────────────────────
    "Omvänt flöde": {
        "Backventil":         [("Backventil defekt — läcker",           1e-2),
                               ("Backventil saknas",                    1e-4)],
        "Manuell ventil":     [("Ventil öppnas mot tryckkälla",         5e-4)],
        "Pump":               [("Pump stopp — backflöde via pump",      2e-2),
                               ("Pump roterar baklänges",               1e-3)],
        "Rörledning / slang": [("Felkopplad ledning",                   1e-4)],
        "Styrsystem / PLC / DCS": [("Ventilstyrning felar", 5e-3)],
    },

    # ── Missriktat flöde ──────────────────────────────────────────────────────
    "Missriktat flöde": {
        "Manuell ventil":     [("Fel ventil öppnad",                    1e-3),
                               ("Bypassventil öppnad",                  5e-4)],
        "On-off ventil":      [("Automatstyrd ventil öppnar fel väg",   1e-2)],
        "Reglerventil":       [("Styrventil öppnar alternativ väg",     5e-3)],
        "Rörledning / slang": [("Felkopplad ledning",                   1e-4)],
        "Styrsystem / PLC / DCS": [("Felaktig ventilstyrning",          5e-3)],
        "Instrument":         [("Flödesgivare i fel linje",             0.1)],
    },

    # ── Avvikande sammansättning ──────────────────────────────────────────────
    "Avvikande sammansättning": {
        "Manuell ventil":     [("Fel ventil öppnad — korsflöde",        1e-3)],
        "Reglerventil":       [("Dos- / blandningsventil i fel läge",   5e-3)],
        "Tank / kärl / kolonn":[("Kontamination i kärl",                5e-4),
                               ("Fel råmaterial / kemikalie",           1e-3)],
        "Rörledning / slang": [("Felkopplad ledning",                   1e-4)],
        "Instrument":         [("Analysgivare felar — doseringsstyrning", 0.1)],
        "Pump":               [("Felaktigt pumpmedium",                 5e-4)],
    },

    # ── Bortfall av hjälpsystem ───────────────────────────────────────────────
    "Bortfall av hjälpsystem": {
        "Elförsörjning":      [("Strömavbrott",                         0.1),
                               ("Säkring / skydd löser ut",             0.5)],
        "Tryckluft / instrumentluft": [
                               ("Lufttrycksfall",                       5e-2),
                               ("Luftkompressor stopp",                 0.1)],
        "Kylsystem / värmesystem": [
                               ("Kylvattenpump stopp",                  2e-2),
                               ("Kylvattentryck faller",                5e-2)],
        "Styrsystem / PLC / DCS": [("DCS / PLC haveri",                 1e-2),
                               ("Kommunikationsavbrott",                0.1)],
    },

    # ── Drift ─────────────────────────────────────────────────────────────────
    "Drift": {
        "Manuell ventil":     [("Felaktig manöver — fel ventil",        1e-2),
                               ("Ventil glömd i fel läge",              5e-3)],
        "Operatör / procedur / underhåll": [
                               ("Felaktig procedur / fel sekvens",      5e-2),
                               ("Procedur saknas eller otydlig",        None),
                               ("Kommunikationsfel",                    None)],
        "Instrument":         [("Felläsning av mätvärde",               5e-2)],
    },

    # ── Underhåll ─────────────────────────────────────────────────────────────
    "Underhåll": {
        "Manuell ventil":     [("Isolationsventil felaktigt ställd",    5e-3),
                               ("Ventil i fel läge efter arbete",       1e-3)],
        "Fläns / koppling / packning": [
                               ("Felaktig packning installerad",        1e-3),
                               ("Flansbultar ej åtdragna",              5e-4)],
        "Operatör / procedur / underhåll": [
                               ("Felaktig isolering (LOTO)",            5e-3),
                               ("Arbete på trycksatt system",           1e-3),
                               ("Fel komponent installerad",            5e-4)],
        "Rörledning / slang": [("Blind platta kvarglömd",               5e-4)],
        "Instrument":         [("Instrument ej återdriftsatt",          1e-2)],
    },

    # ── Start-up / Shut-down ──────────────────────────────────────────────────
    "Start-up / Shut-down": {
        "Manuell ventil":     [("Fel ventilsekvens",                    1e-2),
                               ("Ventil stängd vid pumpstart",          5e-3)],
        "Rörledning / slang": [("Kondensatbank — vätskeslag",           1e-3),
                               ("Luftlås vid start",                    5e-4)],
        "Pump":               [("Pump startas mot stängt utlopp",       5e-3),
                               ("Pump startas utan inloppstryck",       5e-3)],
        "Operatör / procedur / underhåll": [
                               ("Felaktig start-/stoppsekvens",         1e-2),
                               ("Procedur ej följd",                    5e-2)],
        "Tank / kärl / kolonn":[("Kärl ej förberett vid start",         1e-3)],
        "Reglerventil":       [("Reglerventil i manuellt läge vid start", 5e-3)],
        "Värmeväxlare / kylare / värmare": [
                               ("Termisk chock vid uppstart",           1e-3)],
    },
}

_STD_OBJECTS = [
    "Manuell ventil", "On-off ventil", "Reglerventil", "Backventil",
    "Säkerhetsventil / sprängbleck", "Pump", "Kompressor / fläkt",
    "Filter / sil", "Värmeväxlare / kylare / värmare", "Tank / kärl / kolonn",
    "Rörledning / slang", "Fläns / koppling / packning", "Instrument",
    "Styrsystem / PLC / DCS", "Elförsörjning", "Tryckluft / instrumentluft",
    "Kylsystem / värmesystem", "Blandare / omrörare",
    "Operatör / procedur / underhåll", "Övrigt",
]

_COMP_TYPE_TO_OBJ: dict = {
    'Pump': 'Pump', 'Kompressor': 'Kompressor / fläkt',
    'Ventil': 'Reglerventil', 'Rörledning': 'Rörledning / slang',
    'Instrument / Sensor': 'Instrument', 'Tank / Kärl': 'Tank / kärl / kolonn',
    'Värmeväxlare': 'Värmeväxlare / kylare / värmare',
}

_COMP_KEY_TO_OBJ: dict = {
    # Legacy comp_type keys from old _COMP_STD_CAUSES
    'Pump':                    'Pump',
    'Kompressor':              'Kompressor / fläkt',
    'Ventil':                  'Reglerventil',
    'Rörledning':              'Rörledning / slang',
    'Instrument / Sensor':     'Instrument',
    'Tank / Kärl':             'Tank / kärl / kolonn',
    'Värmeväxlare':            'Värmeväxlare / kylare / värmare',
    # New exact keys matching _STD_OBJECTS names (no mapping needed — identity)
    'Manuell ventil':               'Manuell ventil',
    'On-off ventil':                'On-off ventil',
    'Reglerventil':                 'Reglerventil',
    'Backventil':                   'Backventil',
    'Säkerhetsventil / sprängbleck':'Säkerhetsventil / sprängbleck',
    'Kompressor / fläkt':           'Kompressor / fläkt',
    'Filter / sil':                 'Filter / sil',
    'Värmeväxlare / kylare / värmare':'Värmeväxlare / kylare / värmare',
    'Tank / kärl / kolonn':         'Tank / kärl / kolonn',
    'Rörledning / slang':           'Rörledning / slang',
    'Fläns / koppling / packning':  'Fläns / koppling / packning',
    'Instrument':                   'Instrument',
    'Styrsystem / PLC / DCS':       'Styrsystem / PLC / DCS',
    'Elförsörjning':                'Elförsörjning',
    'Tryckluft / instrumentluft':   'Tryckluft / instrumentluft',
    'Kylsystem / värmesystem':      'Kylsystem / värmesystem',
    'Blandare / omrörare':          'Blandare / omrörare',
    'Operatör / procedur / underhåll':'Operatör / procedur / underhåll',
    'Övrigt':                       'Övrigt',
}


def _fix_instrument_causes_v2(conn):
    """No-op: instrument causes now seeded correctly via _COMP_STD_CAUSES."""
    pass


def _fix_instrument_causes_v3(conn):
    """No-op: instrument causes now seeded correctly via _COMP_STD_CAUSES."""
    pass


def _seed_standard_objects(conn):
    for i, name in enumerate(_STD_OBJECTS):
        conn.execute(
            "INSERT OR IGNORE INTO standard_objects (name, sort_order) VALUES (?,?)",
            (name, i))
    conn.commit()


def _seed_component_causes(conn):
    """Insert standard causes (idempotent). Entries can be str or (desc, freq) tuples."""
    for dev_name, by_type in _COMP_STD_CAUSES.items():
        row = conn.execute(
            "SELECT id FROM standard_deviations WHERE description=?", (dev_name,)).fetchone()
        if not row:
            continue
        dev_id = row[0]
        max_sort = (conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_causes WHERE deviation_id=?",
            (dev_id,)).fetchone()[0] or 0)
        sort_i = max_sort + 1
        for comp_key, causes in by_type.items():
            obj_name = _COMP_KEY_TO_OBJ.get(comp_key, comp_key)
            obj_row  = conn.execute(
                "SELECT id FROM standard_objects WHERE name=?", (obj_name,)).fetchone()
            obj_id   = obj_row[0] if obj_row else None
            for entry in causes:
                c_desc = entry[0] if isinstance(entry, tuple) else entry
                c_freq = entry[1] if isinstance(entry, tuple) and len(entry) > 1 else None
                exists = conn.execute(
                    "SELECT id FROM standard_causes "
                    "WHERE deviation_id=? AND description=?",
                    (dev_id, c_desc)).fetchone()
                if not exists:
                    conn.execute(
                        "INSERT INTO standard_causes "
                        "(deviation_id, description, sort_order, comp_type, object_id, frequency)"
                        " VALUES (?,?,?,?,?,?)",
                        (dev_id, c_desc, sort_i, comp_key, obj_id, c_freq))
                    sort_i += 1
                else:
                    updates, vals = [], []
                    if obj_id is not None:
                        updates.append("object_id=?"); vals.append(obj_id)
                    if c_freq is not None:
                        updates.append("frequency=?"); vals.append(c_freq)
                    if updates:
                        vals.append(exists[0])
                        conn.execute(
                            f"UPDATE standard_causes SET {','.join(updates)} WHERE id=?", vals)
    conn.commit()


def _migrate_causes_to_object_id(conn):
    """Populate standard_causes.object_id from comp_type using _COMP_TYPE_TO_OBJ mapping."""
    for comp, obj_name in _COMP_TYPE_TO_OBJ.items():
        row = conn.execute(
            "SELECT id FROM standard_objects WHERE name=?", (obj_name,)).fetchone()
        if not row:
            continue
        conn.execute(
            "UPDATE standard_causes SET object_id=? WHERE comp_type=? AND object_id IS NULL",
            (row[0], comp))
    conn.commit()


def _sync_f_levels_from_base_frequency(conn):
    """Set causes.likelihood (F-level) from standard_cause/base_frequency when frequency data exists."""
    updated = 0
    rows = conn.execute("""
        SELECT c.id, c.base_frequency, c.likelihood, sc.frequency AS sc_freq
        FROM causes c
        LEFT JOIN standard_causes sc ON sc.id = c.standard_cause_id
    """).fetchall()
    for row in rows:
        base_freq_per_year = row['sc_freq'] if row['sc_freq'] is not None else row['base_frequency']
        if base_freq_per_year is None or base_freq_per_year <= 0:
            continue
        f_level = freq_to_f_level(base_freq_per_year)
        if row['likelihood'] != f_level or (row['base_frequency'] is None and row['sc_freq'] is not None):
            conn.execute(
                "UPDATE causes SET likelihood=?, base_frequency=COALESCE(base_frequency, ?) WHERE id=?",
                (f_level, base_freq_per_year, row['id']))
            updated += 1
    return updated


# Keep old name as alias for backward compatibility
_sync_cause_likelihoods_from_frequency = _sync_f_levels_from_base_frequency


# ── Default cause descriptions per component-type cause ───────────────────────
# Keyed by normalized cause text prefix; each value is a list of short phrases
# that describe *how* this cause manifests. These are the third hierarchy level
# shown in the TemplateCausePickerDialog.
_CAUSE_DESCRIPTIONS: dict = {
    # Ventiler
    "Manuell ventil":          ["Felaktigt stängd", "Felaktigt öppnad", "Kvarglömd i fel läge efter underhåll", "Inte märkt / fel märkning"],
    "On-off ventil":           ["Felar stängd (fail-close)", "Felar öppen (fail-open)", "Fastnar i mellanlage", "Aktuatorsignal förlorad"],
    "Reglerventil":            ["Felar stängd (fail-close)", "Felar öppen (fail-open)", "Stiction — fastnar i fel läge", "Positioneringsfel (signal/kalibrering)"],
    "Backventil":              ["Defekt sätestätning — ej tätt", "Fastnar öppen", "Fastnar stängd", "Monterad baklänges"],
    "Säkerhetsventil / sprängbleck": ["Öppnar vid för lågt tryck (felkalibrerad)", "Öppnar inte (igensatt / rostfri)", "Avspärrad under underhåll", "Sprängbleck har brustit"],
    # Roterande utrustning
    "Pump":                    ["Pump stopp (elfel / motorskydd)", "Kavitation (lågt NPSH)", "Tätningsläckage", "Lagerhaveri", "Deadhead (stängt utlopp)", "Felkopplad motor — roterar baklänges"],
    "Kompressor / fläkt":      ["Kompressorstopp", "Surge (instabilt flöde)", "Tätningsläckage", "Igensatt inloppsfilter", "Deadhead (stängt utlopp)"],
    "Blandare / omrörare":     ["Motor stopp", "Rörverksbrott", "Lagerhaveri", "Tätningsläckage"],
    # Statisk utrustning
    "Filter / sil":            ["Igensatt (högt DP)", "Felaktig installation", "Bristande underhåll"],
    "Värmeväxlare / kylare / värmare": ["Igensatta rör (fouling)", "Intern läcka (rörbrott)", "Kylvattenflöde avbryts", "Värmekällan faller bort", "Differenstrycksskydd stänger"],
    "Tank / kärl / kolonn":    ["Yttre läcka (korrosion / spricka)", "Överfyllnad", "Undertryck (vakuumkollaps)", "Exoterm reaktion / polymerisation", "Nivåmätning felaktig"],
    # Rör och kopplingar
    "Rörledning / slang":      ["Korrosionsgenomslag", "Mekanisk skada", "Isblockering / hydratblockering", "Felaktig rörkoppling (monteringsfel)", "Slangbrott"],
    "Fläns / koppling / packning": ["Packningsläckage", "Felåtdragen fläns", "Fel packningsmaterial", "Korroderad flänsbult"],
    # Instrument och styrning
    "Instrument":              ["Signalfel högt", "Signalfel lågt", "Igensatt mätintag", "Felkalibrerad", "Mätledning bruten / läckande"],
    "Styrsystem / PLC / DCS":  ["Styrsignalfel (hög signal)", "Styrsignalfel (låg signal)", "Kommunikationsavbrott", "Felaktig styrlogik", "Operatörsinmatning felaktig"],
    # Hjälpsystem
    "Elförsörjning":           ["Strömavbrott (nätfel)", "Säkring / jordfelsbrytare löser ut", "UPS-batteri tömt", "Generator startar ej"],
    "Tryckluft / instrumentluft": ["Lufttryck faller (kompressorstopp)", "Läckage i luftledning", "Fukt / föroreningar i luft", "Instrument-air-torkare havererar"],
    "Kylsystem / värmesystem": ["Kylvattenpump stopp", "Kylmedelsläckage", "Blockeringsventil stängd", "Värmekällan faller bort"],
    # Övrigt
    "Operatör / procedur / underhåll": ["Felaktig manöver", "Procedurfel / steg utelämnat", "Fel enhet manövrerad", "Kommunikationsfel vid skiftbyte", "Otillräcklig utbildning"],
    "Övrigt":                  ["Se kommentar", "Utredning pågår"],
}


def _seed_cause_descriptions(conn):
    """Add default cause descriptions under matching standard_causes (idempotent)."""
    for i, cause_row in enumerate(
            conn.execute("SELECT id, description, comp_type FROM standard_causes").fetchall()):
        cid   = cause_row[0]
        cdesc = cause_row[1] or ''
        comp  = cause_row[2] or ''
        # Match by comp_type first, then by description prefix
        descs = _CAUSE_DESCRIPTIONS.get(comp) or _CAUSE_DESCRIPTIONS.get(cdesc.split()[0] if cdesc else '')
        if not descs:
            continue
        # Only insert if none exist yet
        if conn.execute("SELECT COUNT(*) FROM cause_descriptions WHERE cause_id=?",
                        (cid,)).fetchone()[0]:
            continue
        for j, d in enumerate(descs):
            conn.execute(
                "INSERT INTO cause_descriptions (cause_id, description, sort_order) VALUES (?,?,?)",
                (cid, d, j))
    conn.commit()


def _tag_letter_prefix(tag: str) -> str:
    """Extract the instrument-code letter prefix from a P&ID tag.
    Delegates to _equip_prefix_from_tag for compound-tag handling.
    'E1.M1.PU101' → 'PU', 'E1' → 'E', 'PCV-101' → 'PCV', '20-FT-201' → 'FT'
    """
    return _equip_prefix_from_tag(tag) if tag else ''


def _lookup_comp_type_for_tag(tag: str, db) -> str:
    """Return the component type the user has taught for this tag's prefix.

    ONLY uses study_tag_memory — the smart recognition table that is built
    exclusively from explicit user confirmations via rubber-band markup.
    Numbers are ignored; the letter prefix is the key (321HV3333 → HV).
    Returns '' if the prefix has not been taught or smart recognition is off.
    """
    if not tag:
        return ''
    if hasattr(db, 'get_config'):
        if db.get_config('smart_recognition_enabled', '1') != '1':
            return ''
    pfx = _tag_letter_prefix(tag)
    if not pfx:
        return ''
    try:
        return db.get_prefix_memory(pfx) if hasattr(db, 'get_prefix_memory') else ''
    except Exception:
        return ''


# _obj_type_matches now lives in pid_viewer.py (imported above) — EquipmentDeviationBar
# needs it too, and pid_viewer.py can't import back from hazop.py.


def _make_tag_completer(db, parent):
    """Build a QCompleter of known equipment tags for a Tag-ID QLineEdit.
    Returns None (leaving the field plain) if the catalog can't be read.
    """
    try:
        tags = [r[0] for r in db.conn.execute(
            "SELECT DISTINCT tag FROM equipment_catalog ORDER BY tag").fetchall()]
        comp = QCompleter(tags, parent)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        return comp
    except Exception:
        return None


def _resolve_std_deviation_id(db, deviation_description):
    """Look up the standard_deviations.id matching a node deviation's
    description text, or None if it doesn't match a standard deviation
    (e.g. a free-typed deviation name).
    """
    if not deviation_description:
        return None
    row = db.conn.execute(
        "SELECT id FROM standard_deviations WHERE description=? COLLATE NOCASE LIMIT 1",
        (deviation_description,)).fetchone()
    return row[0] if row else None


def _create_cause_from_pick(db, deviation_id, description, frequency):
    """Create a new cause under deviation_id from a StandardCausesPickerPopup
    pick, applying the description/likelihood/frequency consistently —
    shared by every quick-add entry point so a freshly created cause always
    starts with real content instead of a blank placeholder.

    Also creates one empty consequence for the new cause (2026-08-07, see
    NOTES.md "direkt konsekvensinmatning") — the same no-popup
    db.add_consequence() call TreePanel.add_consequence() already uses, so
    the HAZOP scenario table's KON cell is ready for inline typing the
    instant the cause exists, without a separate add-consequence step.
    Returns (cause_id, consequence_id).
    """
    new_id = db.add_cause(deviation_id)
    like = freq_to_f_level(frequency) if frequency is not None else 3
    db.update_cause(new_id, description=description or 'Ny orsak', likelihood=like)
    if frequency is not None:
        db.conn.execute("UPDATE causes SET base_frequency=? WHERE id=?", (frequency, new_id))
        db.commit()
    cons_id = db.add_consequence(new_id)
    return new_id, cons_id


def _create_tagged_cause(db, deviation_id, comp_type, comp_tag):
    """Create a new cause under deviation_id (only its equipment tag/type
    set) plus one empty consequence — used when an equipment marker is
    dropped directly onto a deviation in the HAZOP tree (2026-08-08, see
    NOTES.md). No popup: the description defaults to the same "Ny orsak"
    placeholder _create_cause_from_pick's own fallback uses (2026-08-10 —
    was blank, unified to match every other auto-created cause/
    consequence/safeguard's placeholder-text convention), immediately
    inline-editable/overtype-able.
    Returns (cause_id, consequence_id).
    """
    new_id = db.add_cause(deviation_id)
    db.update_cause(new_id, description='Ny orsak', comp_type=comp_type, comp_tag=comp_tag)
    cons_id = db.add_consequence(new_id)
    return new_id, cons_id


def _maybe_save_as_standard_cause(parent, db, dev_id, obj_id, obj_name, description):
    """Ask if a free-typed cause should be promoted into the standard_causes
    library (with an optional frequency), so it's offered again next time.
    Shared by every cause-entry dialog that has a free-text fallback field.
    """
    if dev_id is None or obj_id is None or db is None or not description:
        return
    ans = QMessageBox.question(
        parent, "Spara som standardorsak?",
        f"Vill du spara\n\"{description}\"\nsom standardorsak för {obj_name or 'detta objekt'}?\n\n"
        "Den kommer då att finnas i listan nästa gång.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No)
    if ans != QMessageBox.StandardButton.Yes:
        return

    freq_str, ok = QInputDialog.getText(
        parent, "Frekvens (valfritt)",
        "Ange typfrekvens i händelser/år (lämna tomt om okänd).\n"
        "Exempel: 0.01  (= 1e-2/år, ungefär vart 100:e år)",
        QLineEdit.EchoMode.Normal, '')
    freq = None
    if ok and freq_str.strip():
        try:
            freq = float(freq_str.strip().replace(',', '.'))
        except ValueError:
            pass

    try:
        existing = db.conn.execute(
            "SELECT id FROM standard_causes WHERE deviation_id=? AND description=?",
            (dev_id, description)).fetchone()
        if not existing:
            db.conn.execute(
                "INSERT INTO standard_causes "
                "(deviation_id, description, sort_order, object_id, frequency)"
                " VALUES (?,?,?,?,?)",
                (dev_id, description,
                 (db.conn.execute(
                     "SELECT COALESCE(MAX(sort_order),0)+1 FROM standard_causes "
                     "WHERE deviation_id=?", (dev_id,)).fetchone()[0]),
                 obj_id, freq))
            db.commit()
    except Exception:
        pass


class Database:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        # A pre-existing, non-empty DB file means _migrate() below may run real
        # ALTER TABLE/CREATE TABLE statements against live user data. Snapshot
        # it *before* touching the schema so a buggy/failed migration can
        # always be recovered from. Brand-new (empty) DBs have nothing to lose.
        pre_existing_db = self.path.exists() and self.path.stat().st_size > 0
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")   # faster concurrent reads
        self.conn.executescript(SCHEMA)
        self.commit()
        if pre_existing_db:
            try:
                self._write_backup(startup=True)   # pre-migration safety snapshot
            except Exception:
                logging.warning("Pre-migration backup failed", exc_info=True)
        self._migrate()
        self._write_backup(startup=True)   # unconditional post-migration snapshot

    def __del__(self):
        """Clean up database connection on object destruction.

        Ensures the SQLite connection is properly closed even if the Database
        object is replaced or goes out of scope without explicit cleanup.
        """
        try:
            if hasattr(self, 'conn') and self.conn:
                # Flush WAL checkpoint before closing
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.conn.close()
        except Exception:
            # Silently ignore errors during cleanup
            pass

    def _migrate(self):
        # Kolumnmigreringarna körs TVÅ gånger: före executescript (befintliga
        # databaser) och efter (färska databaser där CREATE TABLE just skapat
        # bastabellerna utan de migrerade kolumnerna — annars kraschar
        # seedningen på t.ex. standard_causes.comp_type). Alla satser är
        # idempotenta; fel ignoreras.
        logging.info("Database: starting migration...")
        self._column_migrations()
        self._migrate_tables_and_seed()
        self._drop_legacy_consequence_likelihood_column()
        logging.info("Database: migration complete")
        self._validate_schema()

    def _drop_legacy_consequence_likelihood_column(self):
        """consequences.likelihood predates the schema redesign that moved
        likelihood onto causes (see CLAUDE.md — 'Likelihood lives on
        causes, severity on consequences'). Nothing reads or writes it
        anymore, but old database files still carry it with stale values.
        A no-op on any database created after the redesign; harmless if
        the installed SQLite predates DROP COLUMN support (3.35+, 2021)."""
        try:
            cols = [r['name'] for r in self.conn.execute("PRAGMA table_info(consequences)")]
            if 'likelihood' in cols:
                self.conn.execute("ALTER TABLE consequences DROP COLUMN likelihood")
                self.commit()
                logging.info("Dropped legacy consequences.likelihood column")
        except sqlite3.OperationalError as e:
            logging.warning(f"Could not drop legacy consequences.likelihood column: {e}")

    def _column_migrations(self):
        """Execute idempotent column migrations with proper error handling.

        Distinguishes between benign errors (column already exists) and real failures
        (syntax errors, permission issues) for accurate logging and diagnostics.
        """
        migration_count = 0
        skipped_count = 0
        error_count = 0

        migrations = [
            "ALTER TABLE nodes ADD COLUMN markup_points TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN markup_style TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN pid_page INTEGER DEFAULT 0",
            "ALTER TABLE nodes ADD COLUMN media TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN pressure TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN temperature TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN updated_at TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN updated_by TEXT DEFAULT ''",
            "ALTER TABLE causes ADD COLUMN likelihood INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE causes ADD COLUMN source_id INTEGER DEFAULT NULL",
            "ALTER TABLE causes ADD COLUMN base_frequency REAL DEFAULT NULL",
            "ALTER TABLE causes ADD COLUMN deviation_id INTEGER REFERENCES deviations(id)",
            "ALTER TABLE safeguards ADD COLUMN rrf INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE safeguards ADD COLUMN source_id INTEGER DEFAULT NULL",
            "ALTER TABLE consequences ADD COLUMN category TEXT DEFAULT ''",
            "ALTER TABLE consequences ADD COLUMN consequence_chain TEXT DEFAULT ''",
            "ALTER TABLE consequences ADD COLUMN source_id INTEGER DEFAULT NULL",
            "ALTER TABLE consequences ADD COLUMN fa_active INTEGER DEFAULT 0",
            "ALTER TABLE consequences ADD COLUMN fa_rrf INTEGER DEFAULT 10",
            "ALTER TABLE consequences ADD COLUMN ignition_active INTEGER DEFAULT 0",
            "ALTER TABLE consequences ADD COLUMN ignition_rrf INTEGER DEFAULT 10",
            "ALTER TABLE cause_markers ADD COLUMN component_tag TEXT DEFAULT ''",
            "ALTER TABLE standard_causes ADD COLUMN comp_type TEXT DEFAULT ''",
            "ALTER TABLE standard_causes ADD COLUMN frequency REAL DEFAULT NULL",
            "ALTER TABLE standard_causes ADD COLUMN use_in_cause_form INTEGER DEFAULT 1",
            "ALTER TABLE causes ADD COLUMN standard_cause_id INTEGER DEFAULT NULL",
            "ALTER TABLE causes ADD COLUMN comp_type TEXT DEFAULT ''",
            "ALTER TABLE causes ADD COLUMN comp_tag  TEXT DEFAULT ''",
            "ALTER TABLE causes ADD COLUMN linked_consequence_id INTEGER DEFAULT NULL",
            "ALTER TABLE safeguards ADD COLUMN sg_type TEXT DEFAULT 'Övrigt'",
            "ALTER TABLE node_markups ADD COLUMN font_size INTEGER DEFAULT 12",
            "ALTER TABLE cause_markers ADD COLUMN rect_w REAL DEFAULT NULL",
            "ALTER TABLE cause_markers ADD COLUMN rect_h REAL DEFAULT NULL",
            "ALTER TABLE consequence_markers ADD COLUMN rect_w REAL DEFAULT NULL",
            "ALTER TABLE consequence_markers ADD COLUMN rect_h REAL DEFAULT NULL",
            "ALTER TABLE safeguard_markers ADD COLUMN rect_w REAL DEFAULT NULL",
            "ALTER TABLE safeguard_markers ADD COLUMN rect_h REAL DEFAULT NULL",
            "ALTER TABLE off_page_connector ADD COLUMN ref_page INTEGER DEFAULT NULL",
            "ALTER TABLE off_page_connector ADD COLUMN dot_scene_x REAL DEFAULT NULL",
            "ALTER TABLE off_page_connector ADD COLUMN dot_scene_y REAL DEFAULT NULL",
            "ALTER TABLE consequence_steps ADD COLUMN node_key TEXT DEFAULT ''",
            "ALTER TABLE standard_causes ADD COLUMN object_id INTEGER REFERENCES standard_objects(id)",
            "ALTER TABLE causes ADD COLUMN comment TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN approved_by TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN approved_at TEXT DEFAULT ''",
            "ALTER TABLE nodes ADD COLUMN study_status TEXT DEFAULT 'draft'",
            "ALTER TABLE study_tag_memory ADD COLUMN active INTEGER DEFAULT 1",
            # Smart object recognition — composite key so the same prefix can
            # map to multiple types (e.g. HV→Handventil×5, HV→Backventil×2).
            # The type with the highest usage_count wins on lookup.
            """CREATE TABLE IF NOT EXISTS study_tag_memory (
                tag         TEXT NOT NULL,
                comp_type   TEXT NOT NULL DEFAULT '',
                comp_tag    TEXT NOT NULL DEFAULT '',
                phash       TEXT NOT NULL DEFAULT '',
                usage_count INTEGER NOT NULL DEFAULT 1,
                updated     TEXT NOT NULL DEFAULT '',
                active      INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (tag, comp_type)
            )""",
            """CREATE TABLE IF NOT EXISTS symbol_fingerprints (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                phash       TEXT NOT NULL,
                comp_type   TEXT NOT NULL DEFAULT '',
                tag_example TEXT NOT NULL DEFAULT '',
                usage_count INTEGER NOT NULL DEFAULT 1
            )""",
            # Fas 1+2 valve-detection improvements (2026-08-06, see NOTES.md) —
            # per-field confidence + line/medium/DN tracing + untagged-valve
            # status on equipment_markers. Existing 'confidence' column is
            # kept and populated with the weakest-link min() of the four new
            # ones, so every existing reader keeps working unchanged.
            "ALTER TABLE equipment_markers ADD COLUMN detection_confidence REAL DEFAULT NULL",
            "ALTER TABLE equipment_markers ADD COLUMN tag_reading_confidence REAL DEFAULT NULL",
            "ALTER TABLE equipment_markers ADD COLUMN tag_assignment_confidence REAL DEFAULT NULL",
            "ALTER TABLE equipment_markers ADD COLUMN line_assignment_confidence REAL DEFAULT NULL",
            "ALTER TABLE equipment_markers ADD COLUMN line_number TEXT DEFAULT ''",
            "ALTER TABLE equipment_markers ADD COLUMN medium_code TEXT DEFAULT ''",
            "ALTER TABLE equipment_markers ADD COLUMN medium_code_verified INTEGER DEFAULT 0",
            "ALTER TABLE equipment_markers ADD COLUMN nominal_size TEXT DEFAULT ''",
            "ALTER TABLE equipment_markers ADD COLUMN tag_status TEXT DEFAULT 'tagged'",
            # Nod → Utrustning → Avvikelse (2026-08-07, se NOTES.md) — kopplar
            # en utrustning till en nod, och en avvikelse till en specifik
            # utrustning. Båda nullable: befintliga rader/avvikelser lämnas
            # helt orörda (equipment_id/node_id=NULL), inget backfill behövs.
            "ALTER TABLE equipment_catalog ADD COLUMN node_id INTEGER REFERENCES nodes(id)",
            "ALTER TABLE deviations ADD COLUMN equipment_id INTEGER REFERENCES equipment_catalog(id)",
            # Drag-and-drop tagg från P&ID till konsekvens (2026-08-07, se
            # NOTES.md) — en konsekvens kan nu bära ett eget taggnummer
            # (t.ex. en pump nedströms orsaken), visat högst upp i
            # KON-kolumnen precis som orsakskolumnen redan visar sin egen
            # tagg. Fri text (description) rörs inte av detta — taggen är
            # ett komplement, inte en ersättning.
            "ALTER TABLE consequences ADD COLUMN comp_type TEXT DEFAULT ''",
            "ALTER TABLE consequences ADD COLUMN comp_tag  TEXT DEFAULT ''",
            # Drag-and-drop tagg från P&ID till safeguard (2026-08-08, se
            # NOTES.md) — samma komplement-inte-ersättning-mönster som
            # consequences.comp_tag/comp_type ovan.
            "ALTER TABLE safeguards ADD COLUMN comp_type TEXT DEFAULT ''",
            "ALTER TABLE safeguards ADD COLUMN comp_tag  TEXT DEFAULT ''",
            # Drag-and-drop taggar in i fritexten (2026-08-09, se NOTES.md) —
            # comp_tag ovan bara visar det SENAST dragna objektet, men flera
            # olika objekt kan nu byggas in i samma fritext. tagged_refs
            # (komma-separerad lista, dedup, ordning bevarad) håller reda på
            # ALLA taggar som någonsin dragits in, så cellen kan fetmarkera
            # varje förekomst av dem i texten.
            "ALTER TABLE consequences ADD COLUMN tagged_refs TEXT DEFAULT ''",
            "ALTER TABLE safeguards ADD COLUMN tagged_refs TEXT DEFAULT ''",
        ]

        for sql in migrations:
            try:
                logging.debug(f"Attempting migration: {sql[:70]}...")
                self.conn.execute(sql)
                migration_count += 1
                logging.debug(f"Migration complete: {sql[:70]}...")
            except sqlite3.OperationalError as e:
                error_msg = str(e).lower()
                # Benign errors: column/table already exists
                if "already exists" in error_msg or "no such table" in error_msg or "duplicate column" in error_msg:
                    logging.debug(f"Skipping (already exists): {sql[:70]}")
                    skipped_count += 1
                else:
                    # Real operational error (constraint violation, syntax error, etc.)
                    logging.error(f"Operational error during migration: {str(e)}")
                    logging.error(f"SQL: {sql[:100]}")
                    error_count += 1
            except sqlite3.DatabaseError as e:
                # Database errors (corruption, permission, etc.)
                logging.error(f"Database error during migration: {str(e)}")
                logging.error(f"SQL: {sql[:100]}")
                error_count += 1
            except Exception as e:
                # Unexpected errors
                logging.error(f"Unexpected error during migration: {type(e).__name__}: {str(e)}")
                logging.error(f"SQL: {sql[:100]}")
                error_count += 1

        logging.info(f"Column migrations: {migration_count} applied, {skipped_count} skipped, {error_count} errors")

        if error_count > 0:
            logging.warning(f"Migration had {error_count} real errors — database may be in inconsistent state")

    def _migrate_tables_and_seed(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS pid_config (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS equipment_types (
                prefix         TEXT PRIMARY KEY,
                equipment_type TEXT NOT NULL,
                display_name   TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS pid_identified_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_code TEXT NOT NULL UNIQUE,
                examples TEXT DEFAULT '', name_sv TEXT DEFAULT '',
                comp_type TEXT DEFAULT '', confirmed INTEGER DEFAULT 0,
                source TEXT DEFAULT 'scan'
            );
            CREATE TABLE IF NOT EXISTS tag_database (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_code TEXT NOT NULL, name_sv TEXT DEFAULT '',
                name_en TEXT DEFAULT '', category TEXT DEFAULT '',
                standard TEXT DEFAULT '', source TEXT DEFAULT 'excel',
                active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS tag_database_settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS reduction_factors (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                consequence_id  INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
                description     TEXT NOT NULL DEFAULT '',
                rrf             INTEGER NOT NULL DEFAULT 10,
                active          INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS equipment_catalog (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                tag            TEXT NOT NULL,
                original_tag   TEXT DEFAULT '',
                prefix         TEXT DEFAULT '',
                pid_page       INTEGER DEFAULT 0,
                equipment_type TEXT DEFAULT '',
                description    TEXT DEFAULT '',
                is_ocr         INTEGER DEFAULT 0,
                include        INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS app_config (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS consequence_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS severity_definitions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                severity_level INTEGER NOT NULL,
                category_id    INTEGER NOT NULL REFERENCES consequence_categories(id) ON DELETE CASCADE,
                description    TEXT    DEFAULT '',
                UNIQUE(severity_level, category_id)
            );
            CREATE TABLE IF NOT EXISTS deviations (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                description TEXT NOT NULL DEFAULT 'Övrigt'
            );
            CREATE TABLE IF NOT EXISTS cause_markers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cause_id INTEGER NOT NULL REFERENCES causes(id) ON DELETE CASCADE,
                pid_page INTEGER DEFAULT 0, x REAL DEFAULT 0, y REAL DEFAULT 0,
                component_type TEXT DEFAULT '', component_tag TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS consequence_markers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                consequence_id INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
                pid_page INTEGER DEFAULT 0, x REAL DEFAULT 0, y REAL DEFAULT 0,
                target_name TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS safeguard_markers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                safeguard_id INTEGER NOT NULL REFERENCES safeguards(id) ON DELETE CASCADE,
                pid_page INTEGER DEFAULT 0, x REAL DEFAULT 0, y REAL DEFAULT 0,
                tag TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS equipment_markers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                equipment_id INTEGER REFERENCES equipment_catalog(id) ON DELETE CASCADE,
                tag TEXT DEFAULT '',
                pid_page INTEGER DEFAULT 0, x REAL DEFAULT 0, y REAL DEFAULT 0,
                comp_type TEXT DEFAULT '',
                shape_outline TEXT DEFAULT '',
                confidence REAL DEFAULT 0,
                link_method TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS pid_revisions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                revision    TEXT NOT NULL DEFAULT '',
                notes       TEXT DEFAULT '',
                created_at  TEXT DEFAULT '',
                pdf_path    TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS pid_sheets (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                display_order INTEGER NOT NULL,
                physical_page INTEGER NOT NULL,
                sheet_name    TEXT DEFAULT '',
                revision_id   INTEGER DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS standard_deviations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT NOT NULL,
                sort_order  INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS standard_causes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                deviation_id INTEGER NOT NULL REFERENCES standard_deviations(id) ON DELETE CASCADE,
                description  TEXT NOT NULL,
                sort_order   INTEGER DEFAULT 0,
                object_id    INTEGER REFERENCES standard_objects(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS standard_objects (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL UNIQUE,
                sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS cause_descriptions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                cause_id        INTEGER NOT NULL REFERENCES standard_causes(id) ON DELETE CASCADE,
                description     TEXT NOT NULL,
                sort_order      INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS board_annotations (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                x      REAL DEFAULT 0,
                y      REAL DEFAULT 0,
                w      REAL DEFAULT 200,
                h      REAL DEFAULT 80,
                text   TEXT DEFAULT '',
                color  TEXT DEFAULT '#fff9c4'
            );
            CREATE TABLE IF NOT EXISTS consequence_steps (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                consequence_id INTEGER NOT NULL REFERENCES consequences(id) ON DELETE CASCADE,
                step           INTEGER NOT NULL,   -- 1..5
                text           TEXT    NOT NULL DEFAULT '',
                ref_tag        TEXT    DEFAULT '',
                node_key       TEXT    DEFAULT ''  -- konsekvensgraf-nod (för beroende kolumner)
            );
            CREATE TABLE IF NOT EXISTS node_markups (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id    INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                type       TEXT NOT NULL DEFAULT 'polygon',
                points     TEXT DEFAULT '[]',
                label      TEXT DEFAULT '',
                color      TEXT DEFAULT '#1565C0',
                opacity    REAL DEFAULT 0.45,
                line_width INTEGER DEFAULT 12,
                font_size  INTEGER DEFAULT 12,
                visible    INTEGER DEFAULT 1,
                pid_page   INTEGER DEFAULT 0,
                sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS safeguard_cause_exclusions (
                safeguard_id INTEGER NOT NULL REFERENCES safeguards(id) ON DELETE CASCADE,
                cause_id     INTEGER NOT NULL REFERENCES causes(id)     ON DELETE CASCADE,
                PRIMARY KEY (safeguard_id, cause_id)
            );
            CREATE TABLE IF NOT EXISTS node_red_markups (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id    INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
                type       TEXT NOT NULL DEFAULT 'polygon',
                points     TEXT DEFAULT '[]',
                label      TEXT DEFAULT '',
                color      TEXT DEFAULT '#CC0000',
                opacity    REAL DEFAULT 1.0,
                line_width INTEGER DEFAULT 4,
                font_size  INTEGER DEFAULT 12,
                visible    INTEGER DEFAULT 1,
                pid_page   INTEGER DEFAULT 0,
                sort_order INTEGER DEFAULT 0,
                symbol_w   REAL DEFAULT 40,
                symbol_h   REAL DEFAULT 40,
                symbol_rot REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS off_page_connector (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                pid_page   INTEGER NOT NULL,
                x_pdf      REAL,
                y_pdf      REAL,
                direction  TEXT,
                edge       TEXT,
                ref_text   TEXT,
                ref_sheet  TEXT,
                ref_line_id TEXT,
                media_type TEXT,
                weight     REAL DEFAULT 0.0,
                confidence REAL DEFAULT 0.5,
                raw_text   TEXT,
                ocr_used   INTEGER DEFAULT 0,
                analyzed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS pid_connection (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                from_page       INTEGER,
                to_page         INTEGER,
                from_connector  INTEGER,
                to_connector    INTEGER,
                media_type      TEXT,
                weight          REAL DEFAULT 0.0,
                confidence      REAL DEFAULT 0.5,
                is_bidirectional INTEGER DEFAULT 0,
                is_ghost        INTEGER DEFAULT 0,
                ghost_ref       TEXT,
                warning         TEXT
            );
        """)

        # Second pass: fresh DBs now have all base tables — add migrated columns
        self._column_migrations()

        if not self.conn.execute("SELECT COUNT(*) FROM consequence_categories").fetchone()[0]:
            for i, name in enumerate(['Person', 'Miljö', 'Ekonomi', 'Anläggning', 'Rykte']):
                self.conn.execute(
                    "INSERT INTO consequence_categories (name, sort_order) VALUES (?,?)", (name, i))

        # Seed component types from hardcoded COMPONENT_TYPES if table is empty
        if not self.conn.execute("SELECT COUNT(*) FROM component_types").fetchone()[0]:
            from pid_viewer import COMPONENT_TYPES as _CT
            for sort_i, (comp_name, modes) in enumerate(_CT.items()):
                cur = self.conn.execute(
                    "INSERT INTO component_types (name, sort_order) VALUES (?,?)",
                    (comp_name, sort_i))
                comp_id = cur.lastrowid
                for mode_i, mode in enumerate(modes):
                    self.conn.execute(
                        "INSERT INTO failure_modes (component_id, description, sort_order)"
                        " VALUES (?,?,?)", (comp_id, mode, mode_i))

        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS component_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS failure_modes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                component_id INTEGER NOT NULL REFERENCES component_types(id) ON DELETE CASCADE,
                description TEXT NOT NULL DEFAULT '',
                freq_per_year REAL DEFAULT NULL,
                sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS consequence_severities (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                consequence_id INTEGER NOT NULL,
                category_id    INTEGER NOT NULL,
                severity       INTEGER NOT NULL DEFAULT 1,
                UNIQUE(consequence_id, category_id)
            );
            CREATE TABLE IF NOT EXISTS consequence_severity_exclusions (
                severity_id  INTEGER NOT NULL,
                safeguard_id INTEGER NOT NULL,
                PRIMARY KEY (severity_id, safeguard_id)
            );
        """)

        # Seed missing deviation_id for existing causes
        orphan_nodes = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT node_id FROM causes WHERE deviation_id IS NULL").fetchall()]
        for nid in orphan_nodes:
            row = self.conn.execute(
                "SELECT id FROM deviations WHERE node_id=? AND description='Övrigt' LIMIT 1",
                (nid,)).fetchone()
            if row:
                dev_id = row[0]
            else:
                cur = self.conn.execute(
                    "INSERT INTO deviations (node_id, description) VALUES (?, 'Övrigt')", (nid,))
                dev_id = cur.lastrowid
            self.conn.execute(
                "UPDATE causes SET deviation_id=? WHERE node_id=? AND deviation_id IS NULL",
                (dev_id, nid))

        # Seed standard_deviations template library if empty
        if not self.conn.execute("SELECT COUNT(*) FROM standard_deviations").fetchone()[0]:
            _STD_CAUSES = {
                "Lågt flöde":    ["Stängd ventil", "Delvis stängd ventil", "Igensatt filter/sil",
                                   "Stoppad pump", "Igensatt rör/ledning", "Läckage uppströms",
                                   "Fel på reglerventil (ej öppnar)"],
                "Högt flöde":    ["Felöppen ventil", "Fel på reglerventil (ej stänger)",
                                   "Ökat drifttryck uppströms", "Ökad pumpkapacitet"],
                "Missriktat flöde": ["Felaktig rörledningsdragning", "Fel rörkoppling",
                                     "Backventil saknas / ur funktion"],
                "Omvänt flöde":  ["Backventil saknas / ur funktion", "Pumpfel – flöde vänds",
                                   "Tryckfall uppströms"],
                "Högt tryck":    ["Stängd utloppsventil", "Blockerat utlopp",
                                   "Ökat inflöde", "Övervärmd gas/vätska", "Felaktig tryckreglering"],
                "Lågt tryck":    ["Läckage i system", "Otäta flänsar/koppling",
                                   "Öppet/läckande utlopp", "Pumphaveri"],
                "Hög nivå":      ["Öppet inlopp", "Stängd utloppsventil", "Felaktig nivåreglering",
                                   "Läckage till kärl"],
                "Låg nivå":      ["Läckage i botten/sida", "Felaktig nivåreglering",
                                   "Stängd inloppsventil", "Pumphaveri"],
                "Hög temperatur": ["Värmeväxlare ur funktion", "Övervärmd inkommande fluid",
                                    "Felaktig temperaturreglering", "Exoterm reaktion"],
                "Låg temperatur": ["Kylmedelfel", "Underkylning av inkommande fluid",
                                    "Felaktig temperaturreglering"],
                "Avvikande sammansättning": ["Fel råvara", "Förorenad råvara",
                                              "Felaktig dosering", "Läckage av annat medium"],
                "Bortfall av hjälpsystem": ["Strömavbrott", "Instrumentluftsfel",
                                             "Kylarfel", "Automatikfel"],
                "Drift":         ["Mänskligt fel vid drift", "Felaktig procedur",
                                   "Kommunikationsfel"],
                "Underhåll":     ["Arbete på trycksatt system", "Felaktig isolering",
                                   "Verktyg kvar i system"],
                "Start-up / Shut-down": ["Felaktig sekvens", "Valves i fel läge",
                                          "Instrument ej kalibrerade"],
                "Övrigt":        [],
            }
            for sort_i, dev_name in enumerate(DEVIATION_TYPES):
                cur = self.conn.execute(
                    "INSERT INTO standard_deviations (description, sort_order) VALUES (?,?)",
                    (dev_name, sort_i))
                dev_tmpl_id = cur.lastrowid
                for cause_i, c_desc in enumerate(_STD_CAUSES.get(dev_name, [])):
                    self.conn.execute(
                        "INSERT INTO standard_causes (deviation_id, description, sort_order)"
                        " VALUES (?,?,?)", (dev_tmpl_id, c_desc, cause_i))

        # Seed standard objects FIRST — causes need object IDs
        if not self.conn.execute(
                "SELECT value FROM app_config WHERE key='std_objects_seeded_v1'").fetchone():
            _seed_standard_objects(self.conn)
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value) VALUES ('std_objects_seeded_v1','1')")

        # Seed v1/v2/v3 legacy sentinels (mark as done so old code doesn't re-run)
        for sv in ('comp_causes_seeded_v1', 'comp_causes_seeded_v2', 'comp_causes_seeded_v3',
                   'causes_object_id_migrated_v1'):
            self.conn.execute(
                f"INSERT OR IGNORE INTO app_config (key,value) VALUES ('{sv}','legacy')")

        # Seed full object-keyed causes (v4) — replaces old comp_type seeding
        if not self.conn.execute(
                "SELECT value FROM app_config WHERE key='comp_causes_seeded_v4'").fetchone():
            _seed_component_causes(self.conn)
            _migrate_causes_to_object_id(self.conn)   # backfill existing rows
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value) VALUES ('comp_causes_seeded_v4','1')")

        # v5: replace verbose/specific causes with generic ones + seed frequencies
        if not self.conn.execute(
                "SELECT value FROM app_config WHERE key='comp_causes_seeded_v5'").fetchone():
            # Delete all seeded standard causes and reseed with generic versions
            self.conn.execute("DELETE FROM standard_causes")
            _seed_component_causes(self.conn)
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value) VALUES ('comp_causes_seeded_v5','1')")

        # Seed default cause descriptions per standard cause (idempotent)
        if not self.conn.execute(
                "SELECT value FROM app_config WHERE key='cause_descs_seeded_v1'").fetchone():
            _seed_cause_descriptions(self.conn)
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value) VALUES ('cause_descs_seeded_v1','1')")

        # Ensure every node has all standard deviations from template library
        std_devs = [r[0] for r in self.conn.execute(
            "SELECT description FROM standard_deviations ORDER BY sort_order").fetchall()]
        if not std_devs:
            std_devs = DEVIATION_TYPES
        all_nodes = [r[0] for r in self.conn.execute("SELECT id FROM nodes").fetchall()]
        for nid in all_nodes:
            existing = {r[0] for r in self.conn.execute(
                "SELECT description FROM deviations WHERE node_id=?", (nid,)).fetchall()}
            for dev_type in std_devs:
                if dev_type not in existing:
                    self.conn.execute(
                        "INSERT INTO deviations (node_id, description) VALUES (?,?)",
                        (nid, dev_type))

        _sync_cause_likelihoods_from_frequency(self.conn)

        # Migration v1: collapse __PFX__ sentinels and full tags into bare prefixes
        if not self.conn.execute(
                "SELECT value FROM app_config WHERE key='tag_memory_prefix_only_v1'").fetchone():
            try:
                rows = self.conn.execute(
                    "SELECT tag, comp_type, phash, usage_count FROM study_tag_memory").fetchall()
                self.conn.execute("DELETE FROM study_tag_memory")
                merged: dict = {}
                for r in rows:
                    raw = r[0]
                    if raw.upper().startswith('__PFX__'):
                        raw = raw[7:]
                    pfx = _tag_letter_prefix(raw) if raw else ''
                    if not pfx:
                        continue
                    prev = merged.get(pfx)
                    if prev is None or r[3] > prev[2]:
                        merged[pfx] = (r[1], r[2] or '', r[3] or 1)
                now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
                for pfx, (ct, ph, cnt) in merged.items():
                    self.conn.execute(
                        "INSERT OR IGNORE INTO study_tag_memory "
                        "(tag,comp_type,phash,usage_count,updated) VALUES (?,?,?,?,?)",
                        (pfx, ct, ph, cnt, now))
            except Exception:
                pass
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value)"
                " VALUES ('tag_memory_prefix_only_v1','1')")

        # Migration v2: change from single-key (tag PK) to composite key (tag,comp_type).
        # Old DBs have tag as sole PRIMARY KEY — recreate with composite key so the
        # same prefix can accumulate counts for multiple types independently.
        if not self.conn.execute(
                "SELECT value FROM app_config WHERE key='tag_memory_composite_v1'").fetchone():
            try:
                old_rows = self.conn.execute(
                    "SELECT tag, comp_type, comp_tag, phash, usage_count, updated, "
                    "COALESCE(active,1) FROM study_tag_memory").fetchall()
                self.conn.executescript("""
                    DROP TABLE IF EXISTS study_tag_memory_old;
                    ALTER TABLE study_tag_memory RENAME TO study_tag_memory_old;
                    CREATE TABLE study_tag_memory (
                        tag         TEXT NOT NULL,
                        comp_type   TEXT NOT NULL DEFAULT '',
                        comp_tag    TEXT NOT NULL DEFAULT '',
                        phash       TEXT NOT NULL DEFAULT '',
                        usage_count INTEGER NOT NULL DEFAULT 1,
                        updated     TEXT NOT NULL DEFAULT '',
                        active      INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY (tag, comp_type)
                    );
                """)
                for r in old_rows:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO study_tag_memory "
                        "(tag,comp_type,comp_tag,phash,usage_count,updated,active) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (r[0], r[1], r[2], r[3], r[4], r[5], r[6]))
                self.conn.execute("DROP TABLE IF EXISTS study_tag_memory_old")
            except Exception:
                pass
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value)"
                " VALUES ('tag_memory_composite_v1','1')")

        # Fix accidental active=0 from a previous bad implementation that
        # deactivated entries in upsert_tag_memory.  Restore active=1 for
        # all rows so the count-based winner logic works correctly.
        # Only runs once; user's intentional active=0 (via panel checkbox)
        # is re-applied afterward if they choose to.
        if not self.conn.execute(
                "SELECT value FROM app_config "
                "WHERE key='tag_memory_restore_active_v1'").fetchone():
            try:
                self.conn.execute(
                    "UPDATE study_tag_memory SET active=1 WHERE usage_count > 0")
            except Exception:
                pass
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value)"
                " VALUES ('tag_memory_restore_active_v1','1')")

        self.commit()

    def _validate_schema(self):
        """Validate that all expected tables and critical columns exist.

        Runs after migrations to ensure the database schema is complete.
        Logs warnings for missing columns that are required for functionality.
        """
        logging.info("Database: validating schema...")

        # Critical tables that must exist
        critical_tables = {
            'nodes', 'causes', 'consequences', 'safeguards', 'deviations',
            'cause_markers', 'consequence_markers', 'safeguard_markers',
            'standard_causes', 'standard_deviations', 'app_config'
        }

        # Map of table -> list of critical columns that should exist
        critical_columns = {
            'nodes': ['id', 'name', 'markup_points', 'markup_style', 'pid_page'],
            'causes': ['id', 'description', 'likelihood', 'deviation_id', 'comp_type'],
            'consequences': ['id', 'description', 'severity', 'category'],
            'safeguards': ['id', 'description', 'rrf', 'sg_type'],
            'deviations': ['id', 'node_id', 'description'],
            'cause_markers': ['id', 'cause_id', 'pid_page', 'x', 'y'],
            'consequence_markers': ['id', 'consequence_id', 'pid_page', 'x', 'y'],
            'safeguard_markers': ['id', 'safeguard_id', 'pid_page', 'x', 'y'],
            'standard_causes': ['id', 'deviation_id', 'description', 'comp_type'],
            'standard_deviations': ['id', 'description'],
            'app_config': ['key', 'value'],
        }

        missing_tables = []
        for table in critical_tables:
            try:
                cursor = self.conn.execute(f"PRAGMA table_info({table})")
                if not cursor.fetchall():
                    missing_tables.append(table)
            except sqlite3.OperationalError:
                missing_tables.append(table)

        if missing_tables:
            logging.error(f"Database validation: missing tables: {', '.join(missing_tables)}")

        # Check for critical columns
        missing_columns = {}
        for table, columns in critical_columns.items():
            if table in missing_tables:
                continue  # Skip tables that are already missing

            try:
                cursor = self.conn.execute(f"PRAGMA table_info({table})")
                existing_cols = {row[1] for row in cursor.fetchall()}  # Column name is index 1
                missing = [col for col in columns if col not in existing_cols]
                if missing:
                    missing_columns[table] = missing
            except sqlite3.OperationalError as e:
                logging.error(f"Database validation: cannot check {table}: {e}")

        if missing_columns:
            for table, cols in missing_columns.items():
                logging.warning(f"Database validation: {table} missing columns: {', '.join(cols)}")

        if not missing_tables and not missing_columns:
            logging.info("Database: schema validation passed — all critical tables and columns present")
        else:
            logging.warning("Database: schema validation found issues — app may have reduced functionality")

    # ── Config ────────────────────────────────────────────────────────────────
    def get_config(self, key, default=None):
        try:
            row = self.conn.execute(
                "SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
            return row['value'] if row else default
        except Exception:
            return default

    def set_config(self, key, value):
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO app_config (key,value) VALUES (?,?)", (key, value))
            self.commit()
        except Exception:
            pass

    _DEFAULT_PALETTE = [
        {'name': 'Kritisk', 'color': '#e74c3c', 'fg_color': '#ffffff'},
        {'name': 'Hög',     'color': '#e67e22', 'fg_color': '#ffffff'},
        {'name': 'Medium',  'color': '#f39c12', 'fg_color': '#000000'},
        {'name': 'Låg',     'color': '#27ae60', 'fg_color': '#ffffff'},
    ]

    def get_color_palette(self):
        val = self.get_config('color_palette')
        if val:
            try:
                return json.loads(val)
            except Exception:
                pass
        return list(self._DEFAULT_PALETTE)

    def set_color_palette(self, palette):
        self.set_config('color_palette', json.dumps(palette))

    def get_risk_matrix(self):
        val = self.get_config('risk_matrix')
        if val:
            try:
                return json.loads(val)
            except Exception:
                pass
        return None

    def set_risk_matrix(self, cfg):
        """Store risk matrix and invalidate the global cache."""
        self.set_config('risk_matrix', json.dumps(cfg))
        # Invalidate the global cache so next get_matrix() reloads from DB
        _risk_matrix_cache.invalidate()

    # ── Tag database ──────────────────────────────────────────────────────────
    def tag_database_entries(self, standard=None):
        if standard:
            return self.conn.execute(
                "SELECT * FROM tag_database WHERE standard=? AND active=1 ORDER BY tag_code",
                (standard,)).fetchall()
        return self.conn.execute(
            "SELECT * FROM tag_database WHERE active=1 ORDER BY tag_code").fetchall()

    def tag_database_standards(self):
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT standard FROM tag_database WHERE standard!='' ORDER BY standard"
        ).fetchall()]

    def import_tag_database_excel(self, filepath: str):
        """Import tag codes from all relevant sheets in the Excel file."""
        try:
            import openpyxl
            wb = openpyxl.load_workbook(filepath, data_only=True)
        except Exception as e:
            return 0, str(e)

        # Sheet name → standard name mapping
        SHEET_MAP = {
            'ISA-5.1':          'ISA-5.1',
            'SSG-5276':         'SSG-5276',
            'ISO-10628_14617':  'ISO-10628',
            'ISO-15519':        'ISO-15519',
            'IEC-DIN_EN_62424': 'IEC-62424',
            'DIN_19227_28000':  'DIN-19227',
            'PIP_PIC001':       'PIP-PIC001',
        }
        imported = 0
        for sheet_name, standard in SHEET_MAP.items():
            if sheet_name not in wb.sheetnames:
                continue
            ws = wb[sheet_name]
            # Find header row (look for 'Taggkod' / 'Tag code')
            header_row = None
            for r in ws.iter_rows(max_row=10, values_only=True):
                for cell in r:
                    if cell and 'taggkod' in str(cell).lower():
                        header_row = r
                        break
                if header_row:
                    break
            if not header_row:
                continue
            # Map column indices
            cols = {str(v).strip().lower(): i
                    for i, v in enumerate(header_row) if v}
            c_code = next((i for k, i in cols.items() if 'taggkod' in k or 'tag code' in k), 0)
            c_sv   = next((i for k, i in cols.items() if 'svenska' in k or 'sv' in k or 'benom' in k), 3)
            c_en   = next((i for k, i in cols.items() if 'english' in k or 'en' in k), 4)
            c_cat  = next((i for k, i in cols.items() if 'kategori' in k or 'categ' in k), 5)

            start_row = ws.max_row  # will be overridden
            for i, row in enumerate(ws.iter_rows(values_only=True), 1):
                if row and row[c_code] and str(row[c_code]).strip().lower() == \
                        (header_row[c_code] or '').lower():
                    start_row = i + 1
                    break

            for row in ws.iter_rows(min_row=start_row, values_only=True):
                if not row or not row[c_code]:
                    continue
                code = str(row[c_code]).strip().upper()
                if not code or len(code) > 10:
                    continue
                sv  = str(row[c_sv]).strip()  if c_sv < len(row) and row[c_sv] else ''
                en  = str(row[c_en]).strip()  if c_en < len(row) and row[c_en] else ''
                cat = str(row[c_cat]).strip() if c_cat < len(row) and row[c_cat] else ''
                # Upsert
                self.conn.execute(
                    "INSERT OR REPLACE INTO tag_database "
                    "(tag_code,name_sv,name_en,category,standard,source,active) "
                    "VALUES (?,?,?,?,?,'excel',1)",
                    (code, sv, en, cat, standard))
                imported += 1

        self.commit()
        return imported, ''

    def tag_db_setting(self, key, default=None):
        r = self.conn.execute(
            "SELECT value FROM tag_database_settings WHERE key=?", (key,)).fetchone()
        return r['value'] if r else default

    def set_tag_db_setting(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO tag_database_settings (key,value) VALUES (?,?)",
            (key, str(value)))
        self.commit()

    def tag_code_lookup(self, prefix: str) -> dict:
        """Look up a tag prefix in the active tag databases. Returns best match."""
        active_std = self.tag_db_setting('active_standard', '')
        if active_std:
            rows = self.conn.execute(
                "SELECT * FROM tag_database WHERE tag_code=? AND standard=? AND active=1",
                (prefix.upper(), active_std)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tag_database WHERE tag_code=? AND active=1",
                (prefix.upper(),)).fetchall()
        return dict(rows[0]) if rows else {}

    # ── PID identified tags ───────────────────────────────────────────────────
    def pid_identified_tags(self):
        return self.conn.execute(
            "SELECT * FROM pid_identified_tags ORDER BY tag_code").fetchall()

    def upsert_pid_tag(self, tag_code, examples, name_sv, comp_type):
        """Insert or update a scanned tag entry (keeps existing confirmed status)."""
        existing = self.conn.execute(
            "SELECT confirmed FROM pid_identified_tags WHERE tag_code=?",
            (tag_code,)).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE pid_identified_tags SET examples=?,name_sv=?,"
                "comp_type=CASE WHEN confirmed=0 THEN ? ELSE comp_type END "
                "WHERE tag_code=?",
                (examples, name_sv, comp_type, tag_code))
        else:
            self.conn.execute(
                "INSERT INTO pid_identified_tags "
                "(tag_code,examples,name_sv,comp_type,confirmed) VALUES (?,?,?,?,0)",
                (tag_code, examples, name_sv, comp_type))
        self.commit()

    def confirm_pid_tag(self, tag_code, comp_type, confirmed):
        self.conn.execute(
            "UPDATE pid_identified_tags SET comp_type=?,confirmed=? WHERE tag_code=?",
            (comp_type, int(confirmed), tag_code))
        self.commit()

    def confirmed_comp_for_tag(self, prefix: str) -> str:
        """Return confirmed component type for a tag prefix, or ''."""
        r = self.conn.execute(
            "SELECT comp_type FROM pid_identified_tags "
            "WHERE tag_code=? AND confirmed=1", (prefix.upper(),)).fetchone()
        return r['comp_type'] if r else ''

    def all_active_tag_codes(self) -> list:
        """Return list of all active tag codes for highlight scanning."""
        active_std = self.tag_db_setting('active_standard', '')
        if active_std:
            rows = self.conn.execute(
                "SELECT tag_code FROM tag_database WHERE standard=? AND active=1",
                (active_std,)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT tag_code FROM tag_database WHERE active=1").fetchall()
        return [r[0] for r in rows]

    # ── Equipment catalog ─────────────────────────────────────────────────────
    def equipment_items(self):
        return self.conn.execute(
            "SELECT * FROM equipment_catalog ORDER BY prefix, tag").fetchall()

    def add_equipment_item(self, tag, original_tag, prefix, page, eq_type, desc, is_ocr):
        cur = self.conn.execute(
            "INSERT INTO equipment_catalog "
            "(tag,original_tag,prefix,pid_page,equipment_type,description,is_ocr,include) "
            "VALUES (?,?,?,?,?,?,?,1)",
            (tag, original_tag, prefix, page, eq_type, desc, is_ocr))
        self.commit()
        return cur.lastrowid

    def update_equipment_item(self, id_, tag, prefix, eq_type, desc):
        self.conn.execute(
            "UPDATE equipment_catalog SET tag=?,prefix=?,equipment_type=?,description=? WHERE id=?",
            (tag, prefix, eq_type, desc, id_))
        self.commit()

    def delete_equipment_item(self, id_):
        # deviations.equipment_id (added 2026-08-07 for "Nod → Utrustning →
        # Avvikelse", see NOTES.md) has NO ON DELETE clause — clear the
        # reference first (keeps the deviation + its causes/consequences,
        # just detaches it from the deleted equipment row) instead of
        # hitting sqlite3.IntegrityError: FOREIGN KEY constraint failed,
        # the same root cause already found and fixed for
        # equipment_catalog.node_id in delete_node().
        self.conn.execute("UPDATE deviations SET equipment_id=NULL WHERE equipment_id=?", (id_,))
        self.conn.execute("DELETE FROM equipment_catalog WHERE id=?", (id_,))
        self.commit()

    def clear_equipment_catalog(self):
        # Same fix as delete_equipment_item() above, but for the full-
        # rescan-replaces-catalog path ("🔍 Skanna P&ID"/"📋 Analysera P&ID").
        self.conn.execute("UPDATE deviations SET equipment_id=NULL WHERE equipment_id IS NOT NULL")
        self.conn.execute("DELETE FROM equipment_catalog")
        self.commit()

    # ── Nod ↔ Utrustning (2026-08-07) ────────────────────────────────────────
    def equipment_node_id(self, equipment_id):
        row = self.conn.execute(
            "SELECT node_id FROM equipment_catalog WHERE id=?", (equipment_id,)).fetchone()
        return row['node_id'] if row else None

    def set_equipment_node(self, equipment_id, node_id):
        self.conn.execute(
            "UPDATE equipment_catalog SET node_id=? WHERE id=?", (node_id, equipment_id))
        self.commit()

    def equipment_deviation_count(self, equipment_id):
        row = self.conn.execute(
            "SELECT COUNT(*) FROM deviations WHERE equipment_id=?", (equipment_id,)).fetchone()
        return row[0] if row else 0

    def equipment_consequence_count(self, comp_tag, comp_type=''):
        """How many consequences reference this equipment's tag — the
        'förekomster i konsekvenser' counter (2026-08-11, see NOTES.md).
        consequences has no equipment_id FK (only the flat comp_tag/
        comp_type columns set by set_consequence_tag), so — unlike
        equipment_deviation_count's FK join — this matches by tag+type."""
        if not comp_tag:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) FROM consequences WHERE comp_tag=? AND comp_type=?",
            (comp_tag, comp_type or '')).fetchone()
        return row[0] if row else 0

    def equipment_safeguard_count(self, comp_tag, comp_type=''):
        """How many safeguards reference this equipment's tag — the
        'förekomster i safeguards' counter (2026-08-11, see NOTES.md).
        Mirrors equipment_consequence_count (safeguards also has no
        equipment_id FK, only comp_tag/comp_type)."""
        if not comp_tag:
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) FROM safeguards WHERE comp_tag=? AND comp_type=?",
            (comp_tag, comp_type or '')).fetchone()
        return row[0] if row else 0

    def set_deviation_equipment(self, deviation_id, equipment_id):
        """Tie an EXISTING deviation to a specific equipment item — used
        when equipment is drag-and-dropped directly onto a deviation
        already sitting in the HAZOP tree (2026-08-09, see NOTES.md),
        as opposed to get_or_create_deviation()'s own equipment_id param
        (which only ever applies to a brand-new deviation it creates).
        Backs both the Nod → Ledord → Utrustning tree grouping and the
        worksheet's separate Utrustning column."""
        self.conn.execute(
            "UPDATE deviations SET equipment_id=? WHERE id=?", (equipment_id, deviation_id))
        self.commit()

    # ── Equipment types ───────────────────────────────────────────────────────
    def get_equipment_type(self, prefix: str):
        """Return saved equipment_type for this prefix, or None."""
        row = self.conn.execute(
            "SELECT equipment_type FROM equipment_types WHERE prefix=?", (prefix,)).fetchone()
        return row['equipment_type'] if row else None

    def save_equipment_type(self, prefix: str, equipment_type: str, display_name: str = ''):
        self.conn.execute(
            "INSERT OR REPLACE INTO equipment_types (prefix, equipment_type, display_name) "
            "VALUES (?,?,?)", (prefix, equipment_type, display_name))
        self.commit()

    def all_equipment_types(self):
        return self.conn.execute(
            "SELECT * FROM equipment_types ORDER BY prefix").fetchall()

    def get_equipment_by_tag(self, tag: str):
        """Return equipment_catalog row for a full tag string (case-insensitive)."""
        row = self.conn.execute(
            "SELECT * FROM equipment_catalog WHERE UPPER(tag)=UPPER(?) AND include=1 LIMIT 1",
            (tag,)).fetchone()
        return dict(row) if row else None

    def get_equipment_by_id(self, id_):
        row = self.conn.execute(
            "SELECT * FROM equipment_catalog WHERE id=?", (id_,)).fetchone()
        return dict(row) if row else None

    def get_equipment_by_marker_id(self, marker_id):
        """Resolve an equipment_markers.id (what a P&ID drag/drop mime
        carries — see NOTES.md) to its linked equipment_catalog row, or
        None if the marker has no linked equipment (untagged shape hit).
        Single shared lookup for every equipment drag-and-drop target
        (KON/SG cells, HAZOP tree deviations) instead of each repeating
        the same two-step marker->catalog join."""
        row = self.conn.execute(
            "SELECT equipment_id FROM equipment_markers WHERE id=?", (marker_id,)).fetchone()
        if not row or row['equipment_id'] is None:
            return None
        return self.get_equipment_by_id(row['equipment_id'])

    # ── Smart object recognition: study tag memory ─────────────────────────────

    def get_tag_memory(self, tag: str):
        """Look up study_tag_memory by the letter prefix of tag (active entries only)."""
        pfx = _tag_letter_prefix(tag) if tag else ''
        if not pfx:
            return None
        row = self.conn.execute(
            "SELECT * FROM study_tag_memory WHERE UPPER(tag)=UPPER(?) AND active=1 LIMIT 1",
            (pfx,)).fetchone()
        return dict(row) if row else None

    def upsert_tag_memory(self, tag: str, comp_type: str,
                          comp_tag: str = '', phash: str = ''):
        """Increment the usage counter for (prefix, comp_type).

        Each (prefix, comp_type) pair has its own counter so the same prefix
        can accumulate counts for multiple types independently.  On lookup,
        the type with the highest count wins.

        Numbers are ignored: 'PU101', 'PU102', 'E1.M1.PU103' all update 'PU'.

        Only increments the usage counter — never deactivates other types.
        The winner is determined by highest count among active entries.
        active=0 is only set manually via the Smart Recognition panel.
        """
        if not comp_type:
            return
        pfx = _tag_letter_prefix(tag) if tag else ''
        if not pfx:
            return
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        existing = self.conn.execute(
            "SELECT usage_count FROM study_tag_memory "
            "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
            (pfx, comp_type)).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE study_tag_memory SET comp_tag=?,phash=?,"
                "usage_count=usage_count+1,updated=? "
                "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
                (comp_tag, phash, now, pfx, comp_type))
        else:
            self.conn.execute(
                "INSERT INTO study_tag_memory (tag,comp_type,comp_tag,phash,active,updated)"
                " VALUES (?,?,?,?,1,?)",
                (pfx, comp_type, comp_tag, phash, now))
        self.commit()

    def get_prefix_memory(self, prefix: str) -> str:
        """Return the most-confirmed comp_type for a letter prefix.
        Highest usage_count among active=1 entries wins.
        Tie-broken by most recently updated (latest confirmation wins).
        """
        if not prefix:
            return ''
        try:
            row = self.conn.execute(
                "SELECT comp_type FROM study_tag_memory "
                "WHERE UPPER(tag)=UPPER(?) AND active=1 "
                "ORDER BY usage_count DESC, updated DESC LIMIT 1",
                (prefix,)).fetchone()
            return row['comp_type'] if row else ''
        except Exception:
            try:
                row = self.conn.execute(
                    "SELECT comp_type FROM study_tag_memory "
                    "WHERE UPPER(tag)=UPPER(?) "
                    "ORDER BY usage_count DESC, updated DESC LIMIT 1",
                    (prefix,)).fetchone()
                return row['comp_type'] if row else ''
            except Exception:
                return ''

    def set_tag_memory_active(self, prefix: str, comp_type: str, active: bool):
        """Enable/disable a specific (prefix, comp_type) entry."""
        self.conn.execute(
            "UPDATE study_tag_memory SET active=? "
            "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
            (1 if active else 0, prefix, comp_type))
        self.commit()

    def find_fingerprint(self, phash: str, max_distance: int = 50):
        """Return best matching symbol_fingerprints row by Hamming distance, or None."""
        if not phash:
            return None
        try:
            h1 = int(phash, 16)
        except ValueError:
            return None
        best = None; best_dist = max_distance + 1
        rows = self.conn.execute(
            "SELECT * FROM symbol_fingerprints ORDER BY usage_count DESC").fetchall()
        for row in rows:
            try:
                h2 = int(row['phash'], 16)
                dist = bin(h1 ^ h2).count('1')
                if dist < best_dist:
                    best_dist = dist
                    best = dict(row)
            except Exception:
                continue
        return best

    def store_fingerprint(self, phash: str, comp_type: str, tag_example: str = ''):
        """Save or increment usage count for a visual fingerprint."""
        if not phash or not comp_type:
            return
        existing = self.find_fingerprint(phash, max_distance=30)
        if existing:
            self.conn.execute(
                "UPDATE symbol_fingerprints SET usage_count=usage_count+1,comp_type=? WHERE id=?",
                (comp_type, existing['id']))
        else:
            self.conn.execute(
                "INSERT INTO symbol_fingerprints (phash,comp_type,tag_example) VALUES (?,?,?)",
                (phash, comp_type, tag_example))
        self.commit()

    # ── Categories ────────────────────────────────────────────────────────────
    def consequence_categories(self):
        return self.conn.execute(
            "SELECT * FROM consequence_categories ORDER BY sort_order, name").fetchall()

    def get_consequence_severities(self, consequence_id):
        """Return list of {id, category_id, name, severity} for each assessed category."""
        return self.conn.execute(
            "SELECT cs.id, cs.category_id, cc.name, cs.severity "
            "FROM consequence_severities cs "
            "JOIN consequence_categories cc ON cc.id=cs.category_id "
            "WHERE cs.consequence_id=? ORDER BY cc.sort_order, cc.name",
            (consequence_id,)).fetchall()

    def set_consequence_severity(self, consequence_id, category_id, severity):
        """Set (or clear when severity=0) a per-category severity for a consequence."""
        if not severity:
            self.conn.execute(
                "DELETE FROM consequence_severities "
                "WHERE consequence_id=? AND category_id=?",
                (consequence_id, category_id))
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO consequence_severities "
                "(consequence_id, category_id, severity) VALUES (?,?,?)",
                (consequence_id, category_id, severity))
        self.commit()

    def get_severity_excluded_sgs(self, severity_id):
        """Return set of safeguard_ids excluded from this category assessment."""
        rows = self.conn.execute(
            "SELECT safeguard_id FROM consequence_severity_exclusions WHERE severity_id=?",
            (severity_id,)).fetchall()
        return {r[0] for r in rows}

    def set_severity_excluded_sgs(self, severity_id, excluded_sg_ids):
        self.conn.execute(
            "DELETE FROM consequence_severity_exclusions WHERE severity_id=?",
            (severity_id,))
        for sg_id in excluded_sg_ids:
            self.conn.execute(
                "INSERT OR IGNORE INTO consequence_severity_exclusions "
                "(severity_id, safeguard_id) VALUES (?,?)", (severity_id, sg_id))
        self.commit()

    def get_safeguard_excluded_causes(self, sg_id):
        """Return set of cause_ids excluded from this safeguard."""
        rows = self.conn.execute(
            "SELECT cause_id FROM safeguard_cause_exclusions WHERE safeguard_id=?",
            (sg_id,)).fetchall()
        return {r[0] for r in rows}

    def set_safeguard_excluded_causes(self, sg_id, cause_id_set):
        self.conn.execute(
            "DELETE FROM safeguard_cause_exclusions WHERE safeguard_id=?", (sg_id,))
        for cid in cause_id_set:
            self.conn.execute(
                "INSERT OR IGNORE INTO safeguard_cause_exclusions "
                "(safeguard_id, cause_id) VALUES (?,?)", (sg_id, cid))
        self.commit()

    def add_category(self, name):
        cur = self.conn.execute(
            "INSERT INTO consequence_categories (name) VALUES (?)", (name,))
        self.commit()
        return cur.lastrowid

    def update_category(self, id_, name):
        self.conn.execute("UPDATE consequence_categories SET name=? WHERE id=?", (name, id_))
        self.commit()

    def delete_category(self, id_):
        self.conn.execute("DELETE FROM consequence_categories WHERE id=?", (id_,))
        self.commit()

    def get_severity_definitions(self):
        """Return dict: severity_level (1-based int) -> {category_id -> description}."""
        rows = self.conn.execute(
            "SELECT severity_level, category_id, description FROM severity_definitions"
        ).fetchall()
        result = {}
        for r in rows:
            lvl = r['severity_level']
            if lvl not in result:
                result[lvl] = {}
            result[lvl][r['category_id']] = r['description']
        return result

    def set_severity_definition(self, severity_level, category_id, description):
        self.conn.execute(
            "INSERT INTO severity_definitions (severity_level, category_id, description) "
            "VALUES (?,?,?) ON CONFLICT(severity_level,category_id) DO UPDATE SET description=excluded.description",
            (severity_level, category_id, description))
        self.commit()

    # ── Component types & failure modes ───────────────────────────────────────
    def component_types(self):
        return self.conn.execute(
            "SELECT * FROM component_types ORDER BY sort_order, name").fetchall()

    def failure_modes(self, component_id):
        return self.conn.execute(
            "SELECT * FROM failure_modes WHERE component_id=? ORDER BY sort_order, id",
            (component_id,)).fetchall()

    def add_component_type(self, name):
        cur = self.conn.execute(
            "INSERT INTO component_types (name) VALUES (?)", (name,))
        self.commit()
        return cur.lastrowid

    def update_component_type(self, id_, name):
        self.conn.execute("UPDATE component_types SET name=? WHERE id=?", (name, id_))
        self.commit()

    def delete_component_type(self, id_):
        self.conn.execute("DELETE FROM component_types WHERE id=?", (id_,))
        self.commit()

    def add_failure_mode(self, component_id, description, freq=None):
        cur = self.conn.execute(
            "INSERT INTO failure_modes (component_id, description, freq_per_year) VALUES (?,?,?)",
            (component_id, description, freq))
        self.commit()
        return cur.lastrowid

    def update_failure_mode(self, id_, description, freq=None):
        self.conn.execute(
            "UPDATE failure_modes SET description=?, freq_per_year=? WHERE id=?",
            (description, freq, id_))
        self.commit()

    def delete_failure_mode(self, id_):
        self.conn.execute("DELETE FROM failure_modes WHERE id=?", (id_,))
        self.commit()

    # ── P&ID helpers ──────────────────────────────────────────────────────────
    def get_pid_path(self):
        row = self.conn.execute("SELECT value FROM pid_config WHERE key='path'").fetchone()
        return row['value'] if row else None

    def set_pid_path(self, path):
        self.conn.execute(
            "INSERT OR REPLACE INTO pid_config (key,value) VALUES ('path',?)", (str(path),))
        self.commit()

    def get_pid_config_value(self, key):
        row = self.conn.execute(
            "SELECT value FROM pid_config WHERE key=?", (key,)).fetchone()
        return row['value'] if row else None

    def set_pid_config_value(self, key, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO pid_config (key,value) VALUES (?,?)", (key, str(value)))
        self.commit()

    def clear_connector_analysis(self):
        self.conn.execute("DELETE FROM off_page_connector")
        self.conn.execute("DELETE FROM pid_connection")
        self.commit()

    def save_connectors(self, rows):
        if not rows:
            return
        for r in rows:
            r.setdefault('ref_page', None)
        self.conn.executemany(
            "INSERT INTO off_page_connector "
            "(pid_page,x_pdf,y_pdf,direction,edge,ref_text,ref_sheet,"
            "ref_line_id,media_type,weight,confidence,raw_text,ocr_used,analyzed_at,ref_page) "
            "VALUES(:pid_page,:x_pdf,:y_pdf,:direction,:edge,:ref_text,:ref_sheet,"
            ":ref_line_id,:media_type,:weight,:confidence,:raw_text,:ocr_used,:analyzed_at,:ref_page)",
            rows)
        self.commit()

    def update_connector_dot_position(self, connector_id, x, y):
        """Persist a manually dragged dot position for one off-page connector."""
        self.conn.execute(
            "UPDATE off_page_connector SET dot_scene_x=?, dot_scene_y=? WHERE id=?",
            (x, y, connector_id))
        self.commit()

    def save_pid_connections(self, rows):
        if not rows:
            return
        self.conn.executemany(
            "INSERT INTO pid_connection "
            "(from_page,to_page,from_connector,to_connector,media_type,weight,"
            "confidence,is_bidirectional,is_ghost,ghost_ref,warning) "
            "VALUES(:from_page,:to_page,:from_connector,:to_connector,:media_type,"
            ":weight,:confidence,:is_bidirectional,:is_ghost,:ghost_ref,:warning)",
            rows)
        self.commit()

    # ── Board annotations (sticky notes, feature 8) ──────────────────────────
    def get_board_annotations(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT id,x,y,w,h,text,color FROM board_annotations")]

    def add_board_annotation(self, x, y, text='', color='#fff9c4', w=200, h=80):
        cur = self.conn.execute(
            "INSERT INTO board_annotations (x,y,w,h,text,color) VALUES (?,?,?,?,?,?)",
            (x, y, w, h, text, color))
        self.commit()
        return cur.lastrowid

    def update_board_annotation(self, id_, x=None, y=None, w=None, h=None,
                                 text=None, color=None):
        sets, vals = [], []
        for col, val in (('x',x),('y',y),('w',w),('h',h),('text',text),('color',color)):
            if val is not None:
                sets.append(f"{col}=?"); vals.append(val)
        if sets:
            self.conn.execute(
                f"UPDATE board_annotations SET {', '.join(sets)} WHERE id=?",
                vals + [id_])
            self.commit()

    def delete_board_annotation(self, id_):
        self.conn.execute("DELETE FROM board_annotations WHERE id=?", (id_,))
        self.commit()

    def get_pid_connections(self):
        return self.conn.execute("SELECT * FROM pid_connection").fetchall()

    def get_connectors(self):
        return self.conn.execute("SELECT * FROM off_page_connector").fetchall()

    def delete_pid_connection(self, conn_id):
        self.conn.execute("DELETE FROM pid_connection WHERE id=?", (conn_id,))
        self.commit()

    def add_manual_pid_connection(self, from_page, to_page):
        """Insert a manual (user-defined) inter-sheet link with max confidence."""
        import datetime
        self.conn.execute(
            "INSERT INTO pid_connection "
            "(from_page,to_page,from_connector,to_connector,media_type,weight,"
            "confidence,is_bidirectional,is_ghost,ghost_ref,warning) "
            "VALUES (?,?,NULL,NULL,'unknown',1.0,1.0,1,0,NULL,'manual')",
            (from_page, to_page))
        self.commit()

    # ── PID revisions & sheets ────────────────────────────────────────────────
    def add_revision(self, revision, notes, pdf_path, created_at=''):
        if not created_at:
            created_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        cur = self.conn.execute(
            "INSERT INTO pid_revisions (revision,notes,created_at,pdf_path) VALUES (?,?,?,?)",
            (revision, notes, created_at, str(pdf_path)))
        self.commit()
        return cur.lastrowid

    def get_revisions(self):
        return self.conn.execute(
            "SELECT * FROM pid_revisions ORDER BY id DESC").fetchall()

    def ensure_sheets_initialized(self, page_count):
        existing = self.conn.execute("SELECT COUNT(*) FROM pid_sheets").fetchone()[0]
        if existing == 0 and page_count > 0:
            for i in range(page_count):
                self.conn.execute(
                    "INSERT INTO pid_sheets (display_order,physical_page,sheet_name) VALUES (?,?,?)",
                    (i, i, f"Blad {i + 1}"))
            self.commit()

    def get_sheets(self):
        return self.conn.execute(
            "SELECT * FROM pid_sheets ORDER BY display_order").fetchall()

    def append_sheets(self, physical_pages, sheet_names, revision_id=None):
        max_row = self.conn.execute(
            "SELECT MAX(display_order) FROM pid_sheets").fetchone()[0]
        start_order = (max_row + 1) if max_row is not None else 0
        for i, (phys, name) in enumerate(zip(physical_pages, sheet_names)):
            self.conn.execute(
                "INSERT INTO pid_sheets (display_order,physical_page,sheet_name,revision_id) "
                "VALUES (?,?,?,?)",
                (start_order + i, phys, name, revision_id))
        self.commit()

    def reorder_sheets(self, ordered_ids):
        for disp_order, sheet_id in enumerate(ordered_ids):
            self.conn.execute(
                "UPDATE pid_sheets SET display_order=? WHERE id=?",
                (disp_order, sheet_id))
        self.commit()

    def update_sheet_name(self, id_, name):
        self.conn.execute("UPDATE pid_sheets SET sheet_name=? WHERE id=?", (name, id_))
        self.commit()

    def delete_sheets(self, ids):
        for id_ in ids:
            self.conn.execute("DELETE FROM pid_sheets WHERE id=?", (id_,))
        remaining = self.conn.execute(
            "SELECT id FROM pid_sheets ORDER BY display_order").fetchall()
        for disp_order, row in enumerate(remaining):
            self.conn.execute(
                "UPDATE pid_sheets SET display_order=? WHERE id=?",
                (disp_order, row['id']))
        self.commit()

    def delete_objects_on_pages(self, physical_pages):
        """Delete all P&ID placements (markers and node markups) on the given physical pages."""
        for page in physical_pages:
            self.conn.execute("DELETE FROM node_markups WHERE pid_page=?", (page,))
            self.conn.execute("DELETE FROM cause_markers WHERE pid_page=?", (page,))
            self.conn.execute("DELETE FROM consequence_markers WHERE pid_page=?", (page,))
            self.conn.execute("DELETE FROM safeguard_markers WHERE pid_page=?", (page,))
        self.commit()

    def objects_on_pages(self, physical_pages):
        """Return counts of HAZOP objects on the given physical page numbers.
        Returns dict: physical_page -> {markups, causes, consequences, safeguards}."""
        result = {}
        for page in physical_pages:
            markups = self.conn.execute(
                "SELECT COUNT(*) FROM node_markups WHERE pid_page=?", (page,)).fetchone()[0]
            causes = self.conn.execute(
                "SELECT COUNT(*) FROM cause_markers WHERE pid_page=?", (page,)).fetchone()[0]
            consequences = self.conn.execute(
                "SELECT COUNT(*) FROM consequence_markers WHERE pid_page=?", (page,)).fetchone()[0]
            safeguards = self.conn.execute(
                "SELECT COUNT(*) FROM safeguard_markers WHERE pid_page=?", (page,)).fetchone()[0]
            result[page] = {
                'markups': markups,
                'causes': causes,
                'consequences': consequences,
                'safeguards': safeguards,
            }
        return result

    def get_sheet_physical_page(self, display_index):
        row = self.conn.execute(
            "SELECT physical_page FROM pid_sheets ORDER BY display_order "
            "LIMIT 1 OFFSET ?", (display_index,)).fetchone()
        return row['physical_page'] if row else display_index

    def get_display_page_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM pid_sheets").fetchone()[0]

    def clear_sheets(self):
        self.conn.execute("DELETE FROM pid_sheets")
        self.commit()

    def clear_all_pid_data(self):
        """Remove all P&ID revisions, sheets, placements, markups and connectors."""
        for table in (
            "pid_sheets", "pid_revisions",
            "cause_markers", "consequence_markers", "safeguard_markers",
            "node_markups", "node_red_markups",
            "off_page_connector", "pid_connection",
        ):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.execute("DELETE FROM pid_config WHERE key='path'")
        self.commit()

    def add_node_with_markup(self, name, points, style, page):
        cur = self.conn.execute(
            "INSERT INTO nodes (name, markup_points, markup_style, pid_page) VALUES (?,?,?,?)",
            (name, json.dumps(points), json.dumps(style), page))
        self.commit()
        return cur.lastrowid

    def add_cause_marker(self, cause_id, page, x, y, comp_type, tag='',
                         rect_w=None, rect_h=None):
        self.conn.execute(
            "INSERT INTO cause_markers "
            "(cause_id,pid_page,x,y,component_type,component_tag,rect_w,rect_h) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cause_id, page, x, y, comp_type, tag, rect_w, rect_h))
        self.commit()

    def add_consequence_marker(self, cons_id, page, x, y, target,
                               rect_w=None, rect_h=None):
        self.conn.execute(
            "INSERT INTO consequence_markers "
            "(consequence_id,pid_page,x,y,target_name,rect_w,rect_h) "
            "VALUES (?,?,?,?,?,?,?)",
            (cons_id, page, x, y, target, rect_w, rect_h))
        self.commit()

    def add_safeguard_marker(self, sg_id, page, x, y, tag='',
                             rect_w=None, rect_h=None):
        self.conn.execute(
            "INSERT INTO safeguard_markers "
            "(safeguard_id,pid_page,x,y,tag,rect_w,rect_h) "
            "VALUES (?,?,?,?,?,?,?)",
            (sg_id, page, x, y, tag, rect_w, rect_h))
        self.commit()

    def update_marker_rect(self, marker_type, marker_id, page,
                           cx, cy, rect_w, rect_h):
        """Update center position + zone rect dimensions for a placed marker."""
        if marker_type == 'cause':
            self.conn.execute(
                "UPDATE cause_markers SET x=?,y=?,rect_w=?,rect_h=? "
                "WHERE cause_id=? AND pid_page=?",
                (cx, cy, rect_w, rect_h, marker_id, page))
        elif marker_type == 'consequence':
            self.conn.execute(
                "UPDATE consequence_markers SET x=?,y=?,rect_w=?,rect_h=? "
                "WHERE consequence_id=? AND pid_page=?",
                (cx, cy, rect_w, rect_h, marker_id, page))
        elif marker_type == 'safeguard':
            self.conn.execute(
                "UPDATE safeguard_markers SET x=?,y=?,rect_w=?,rect_h=? "
                "WHERE safeguard_id=? AND pid_page=?",
                (cx, cy, rect_w, rect_h, marker_id, page))
        self.commit()

    def cause_markers_for_page(self, page):
        return self.conn.execute(
            "SELECT * FROM cause_markers WHERE pid_page=?", (page,)).fetchall()

    def consequence_markers_for_page(self, page):
        return self.conn.execute(
            "SELECT * FROM consequence_markers WHERE pid_page=?", (page,)).fetchall()

    def safeguard_markers_for_page(self, page):
        return self.conn.execute(
            "SELECT * FROM safeguard_markers WHERE pid_page=?", (page,)).fetchall()

    def marked_cause_ids(self):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT cause_id FROM cause_markers").fetchall()}

    def marked_consequence_ids(self):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT consequence_id FROM consequence_markers").fetchall()}

    def marked_safeguard_ids(self):
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT safeguard_id FROM safeguard_markers").fetchall()}

    def remove_cause_marker(self, cause_id):
        self.conn.execute("DELETE FROM cause_markers WHERE cause_id=?", (cause_id,))
        self.commit()

    def remove_consequence_marker(self, consequence_id):
        self.conn.execute("DELETE FROM consequence_markers WHERE consequence_id=?", (consequence_id,))
        self.commit()

    def remove_safeguard_marker(self, safeguard_id):
        self.conn.execute("DELETE FROM safeguard_markers WHERE safeguard_id=?", (safeguard_id,))
        self.commit()

    def get_cause_marker(self, cause_id):
        row = self.conn.execute(
            "SELECT pid_page, x, y FROM cause_markers WHERE cause_id=? LIMIT 1",
            (cause_id,)).fetchone()
        return dict(row) if row else None

    def cause_markers_for_cause(self, cause_id):
        """Return all markers for a specific cause (page, x, y, comp_type, tag)."""
        return self.conn.execute(
            "SELECT pid_page, x, y, component_type, component_tag "
            "FROM cause_markers WHERE cause_id=?",
            (cause_id,)).fetchall()

    def get_consequence_marker(self, consequence_id):
        row = self.conn.execute(
            "SELECT pid_page, x, y FROM consequence_markers WHERE consequence_id=? LIMIT 1",
            (consequence_id,)).fetchone()
        return dict(row) if row else None

    def get_safeguard_marker(self, safeguard_id):
        row = self.conn.execute(
            "SELECT pid_page, x, y FROM safeguard_markers WHERE safeguard_id=? LIMIT 1",
            (safeguard_id,)).fetchone()
        return dict(row) if row else None

    # ── Equipment markers (auto-detected symbols, "🎯 Hitta på P&ID") ──────────
    def add_equipment_marker(self, equipment_id, tag, page, x, y, comp_type,
                             shape_outline='', confidence=0.0, link_method='',
                             detection_confidence=None, tag_reading_confidence=None,
                             tag_assignment_confidence=None, line_assignment_confidence=None,
                             line_number='', medium_code='', medium_code_verified=0,
                             nominal_size='', tag_status='tagged'):
        cur = self.conn.execute(
            "INSERT INTO equipment_markers "
            "(equipment_id,tag,pid_page,x,y,comp_type,shape_outline,confidence,link_method,"
            "detection_confidence,tag_reading_confidence,tag_assignment_confidence,"
            "line_assignment_confidence,line_number,medium_code,medium_code_verified,"
            "nominal_size,tag_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (equipment_id, tag, page, x, y, comp_type, shape_outline, confidence, link_method,
             detection_confidence, tag_reading_confidence, tag_assignment_confidence,
             line_assignment_confidence, line_number, medium_code, medium_code_verified,
             nominal_size, tag_status))
        self.commit()
        return cur.lastrowid

    def equipment_markers_for_page(self, page):
        return self.conn.execute(
            "SELECT * FROM equipment_markers WHERE pid_page=?", (page,)).fetchall()

    def delete_equipment_marker(self, id_):
        self.conn.execute("DELETE FROM equipment_markers WHERE id=?", (id_,))
        self.commit()

    # ── Queries ───────────────────────────────────────────────────────────────
    def nodes(self):
        return self.conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()

    def causes(self, node_id):
        return self.conn.execute(
            "SELECT * FROM causes WHERE node_id=? ORDER BY id", (node_id,)).fetchall()

    def consequences(self, cause_id):
        return self.conn.execute(
            "SELECT * FROM consequences WHERE cause_id=? ORDER BY id", (cause_id,)).fetchall()

    def safeguards_for_cause(self, cause_id):
        """Return all safeguards attached to any consequence of cause_id."""
        return self.conn.execute(
            "SELECT s.id FROM safeguards s "
            "JOIN consequences c ON c.id=s.consequence_id "
            "WHERE c.cause_id=?", (cause_id,)).fetchall()

    def safeguards(self, consequence_id):
        return self.conn.execute(
            "SELECT * FROM safeguards WHERE consequence_id=? ORDER BY id", (consequence_id,)).fetchall()

    def actions(self, consequence_id):
        return self.conn.execute(
            "SELECT * FROM actions WHERE consequence_id=? ORDER BY id", (consequence_id,)).fetchall()

    def get_node(self, id_):
        row = self.conn.execute("SELECT * FROM nodes WHERE id=?", (id_,)).fetchone()
        return dict(row) if row else None

    def get_cause(self, id_):
        row = self.conn.execute("SELECT * FROM causes WHERE id=?", (id_,)).fetchone()
        return dict(row) if row else None

    def get_consequence(self, id_):
        row = self.conn.execute("SELECT * FROM consequences WHERE id=?", (id_,)).fetchone()
        return dict(row) if row else None

    def get_safeguard(self, id_):
        row = self.conn.execute("SELECT * FROM safeguards WHERE id=?", (id_,)).fetchone()
        return dict(row) if row else None

    def cause_base_frequency_per_year(self, cause):
        """Return frequency in events/year from standard cause or base_frequency, or None."""
        if cause is None:
            return None
        d = dict(cause)
        std_id = d.get('standard_cause_id')
        if std_id:
            sc = self.get_standard_cause(std_id)
            if sc and sc.get('frequency') is not None:
                return sc['frequency']
        bf = d.get('base_frequency')
        return bf if bf is not None else None

    def cause_f_level(self, cause, default=3):
        """Return F-level (-1..5): standard_cause/base_frequency first, else manual likelihood."""
        base_freq_per_year = self.cause_base_frequency_per_year(cause)
        if base_freq_per_year is not None:
            return freq_to_f_level(base_freq_per_year)
        if cause is None:
            return default
        like = dict(cause).get('likelihood')
        return like if like is not None else default

    # Keep old names as aliases for backward compatibility
    cause_base_frequency = cause_base_frequency_per_year
    cause_frequency_level = cause_f_level

    # ── Add ───────────────────────────────────────────────────────────────────
    def add_node(self):
        cur = self.conn.execute("INSERT INTO nodes (name) VALUES ('Ny nod')")
        node_id = cur.lastrowid
        std = [r[0] for r in self.conn.execute(
            "SELECT description FROM standard_deviations ORDER BY sort_order").fetchall()]
        for dev_type in (std or DEVIATION_TYPES):
            self.conn.execute(
                "INSERT INTO deviations (node_id, description) VALUES (?,?)",
                (node_id, dev_type))
        self.commit()
        return node_id

    def deviations(self, node_id):
        return self.conn.execute(
            "SELECT * FROM deviations WHERE node_id=? ORDER BY id", (node_id,)).fetchall()

    def deviations_for_equipment(self, equipment_id):
        return self.conn.execute(
            "SELECT * FROM deviations WHERE equipment_id=? ORDER BY id", (equipment_id,)).fetchall()

    def get_deviation(self, id_):
        row = self.conn.execute("SELECT * FROM deviations WHERE id=?", (id_,)).fetchone()
        return dict(row) if row else None

    def causes_for_node_all(self, node_id):
        """All causes for a node across all deviations."""
        return self.conn.execute(
            "SELECT c.* FROM causes c "
            "JOIN deviations d ON d.id=c.deviation_id "
            "WHERE d.node_id=?", (node_id,)).fetchall()

    def consequences_for_node(self, node_id):
        """All consequences for a node across all causes."""
        return self.conn.execute(
            "SELECT k.* FROM consequences k "
            "JOIN causes c ON c.id=k.cause_id "
            "JOIN deviations d ON d.id=c.deviation_id "
            "WHERE d.node_id=?", (node_id,)).fetchall()

    def causes_for_deviation(self, deviation_id):
        return self.conn.execute(
            "SELECT * FROM causes WHERE deviation_id=? ORDER BY id", (deviation_id,)).fetchall()

    def causes_for_node_excluding_deviation(self, node_id, deviation_id):
        """Return causes for the node that belong to OTHER deviations (for reuse dialog)."""
        return self.conn.execute(
            "SELECT c.id, c.description, c.comp_type, c.comp_tag, "
            "d.description AS deviation_name, d.id AS deviation_id "
            "FROM causes c "
            "JOIN deviations d ON c.deviation_id = d.id "
            "WHERE d.node_id=? AND d.id!=? "
            "ORDER BY d.id, c.id",
            (node_id, deviation_id)).fetchall()

    def add_deviation(self, node_id, description="Övrigt", equipment_id=None):
        cur = self.conn.execute(
            "INSERT INTO deviations (node_id, description, equipment_id) VALUES (?,?,?)",
            (node_id, description, equipment_id))
        self.commit()
        return cur.lastrowid

    def update_deviation(self, id_, description):
        self.conn.execute("UPDATE deviations SET description=? WHERE id=?", (description, id_))
        self.commit()

    def delete_deviation(self, id_):
        for cause in self.causes_for_deviation(id_):
            self.delete_cause(cause['id'])
        self.conn.execute("DELETE FROM deviations WHERE id=?", (id_,))
        self.commit()

    def get_or_create_deviation(self, node_id, description="Övrigt", equipment_id=None):
        row = self.conn.execute(
            "SELECT id FROM deviations WHERE node_id=? AND description=? AND equipment_id IS ? "
            "ORDER BY id LIMIT 1",
            (node_id, description, equipment_id)).fetchone()
        return row[0] if row else self.add_deviation(node_id, description, equipment_id)

    # ── Standard deviation / cause template library ───────────────────────────
    def standard_deviations(self):
        return self.conn.execute(
            "SELECT * FROM standard_deviations ORDER BY sort_order, id").fetchall()

    def add_standard_deviation(self, description):
        max_ord = self.conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_deviations").fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO standard_deviations (description, sort_order) VALUES (?,?)",
            (description, max_ord + 1))
        self.commit()
        return cur.lastrowid

    def update_standard_deviation(self, id_, description):
        self.conn.execute(
            "UPDATE standard_deviations SET description=? WHERE id=?", (description, id_))
        self.commit()

    def delete_standard_deviation(self, id_):
        self.conn.execute("DELETE FROM standard_deviations WHERE id=?", (id_,))
        self.commit()

    def reorder_standard_deviations(self, ordered_ids):
        for i, id_ in enumerate(ordered_ids):
            self.conn.execute(
                "UPDATE standard_deviations SET sort_order=? WHERE id=?", (i, id_))
        self.commit()

    def get_standard_cause(self, id_):
        row = self.conn.execute(
            "SELECT * FROM standard_causes WHERE id=?", (id_,)).fetchone()
        return dict(row) if row else None

    def standard_causes(self, deviation_id):
        return self.conn.execute(
            "SELECT * FROM standard_causes WHERE deviation_id=? ORDER BY sort_order, id",
            (deviation_id,)).fetchall()

    def standard_causes_for_name(self, deviation_name):
        row = self.conn.execute(
            "SELECT id FROM standard_deviations WHERE description=? LIMIT 1",
            (deviation_name,)).fetchone()
        if not row:
            return []
        return self.standard_causes(row[0])

    def add_standard_cause(self, deviation_id, description):
        max_ord = self.conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_causes WHERE deviation_id=?",
            (deviation_id,)).fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO standard_causes (deviation_id, description, sort_order) VALUES (?,?,?)",
            (deviation_id, description, max_ord + 1))
        self.commit()
        return cur.lastrowid

    def update_standard_cause(self, id_, description=None, **kwargs):
        sets, vals = [], []
        if description is not None:
            sets.append("description=?"); vals.append(description)
        if 'frequency' in kwargs:
            sets.append("frequency=?"); vals.append(kwargs['frequency'])
        if 'use_in_cause_form' in kwargs:
            sets.append("use_in_cause_form=?"); vals.append(kwargs['use_in_cause_form'])
        if sets:
            vals.append(id_)
            self.conn.execute(f"UPDATE standard_causes SET {', '.join(sets)} WHERE id=?", vals)
            self.commit()

    def delete_standard_cause(self, id_):
        self.conn.execute("DELETE FROM standard_causes WHERE id=?", (id_,))
        self.commit()

    def reorder_standard_causes(self, ordered_ids):
        for i, id_ in enumerate(ordered_ids):
            self.conn.execute(
                "UPDATE standard_causes SET sort_order=? WHERE id=?", (i, id_))
        self.commit()

    def distinct_comp_types(self):
        """Return sorted list of all comp_type values used in standard_causes (excl. empty)."""
        rows = self.conn.execute(
            "SELECT DISTINCT comp_type FROM standard_causes "
            "WHERE comp_type != '' ORDER BY comp_type").fetchall()
        return [r[0] for r in rows]

    def standard_causes_for_comp_type(self, comp_type, deviation_description=None):
        """Return standard_causes for comp_type, optionally filtered to one deviation."""
        if deviation_description:
            return self.conn.execute(
                "SELECT sc.id, sc.description, sc.sort_order, sc.comp_type, sc.frequency, "
                "sd.description AS deviation_name, sd.id AS deviation_id "
                "FROM standard_causes sc "
                "JOIN standard_deviations sd ON sc.deviation_id = sd.id "
                "WHERE sc.comp_type=? AND sd.description=? "
                "ORDER BY sd.sort_order, sc.sort_order",
                (comp_type, deviation_description)).fetchall()
        return self.conn.execute(
            "SELECT sc.id, sc.description, sc.sort_order, sc.comp_type, sc.frequency, "
            "sd.description AS deviation_name, sd.id AS deviation_id "
            "FROM standard_causes sc "
            "JOIN standard_deviations sd ON sc.deviation_id = sd.id "
            "WHERE sc.comp_type=? ORDER BY sd.sort_order, sc.sort_order",
            (comp_type,)).fetchall()

    def add_standard_cause_for_comp_type(self, deviation_id, description, comp_type):
        max_ord = (self.conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_causes WHERE deviation_id=?",
            (deviation_id,)).fetchone()[0] or 0)
        cur = self.conn.execute(
            "INSERT INTO standard_causes (deviation_id, description, sort_order, comp_type)"
            " VALUES (?,?,?,?)", (deviation_id, description, max_ord + 1, comp_type))
        self.commit()
        return cur.lastrowid

    # ── Hierarchy: deviation → object → causes ────────────────────────────────
    def objects_for_deviation(self, deviation_id):
        """Standard objects that have at least one cause for this deviation, sorted."""
        rows = self.conn.execute(
            """SELECT so.id, so.name, so.sort_order,
                      COUNT(sc.id) AS n_causes
               FROM standard_objects so
               JOIN standard_causes sc ON sc.object_id = so.id
               WHERE sc.deviation_id = ?
               GROUP BY so.id
               ORDER BY so.sort_order, so.name""",
            (deviation_id,)).fetchall()
        return [dict(r) for r in rows]

    def deviations_for_object(self, object_id):
        """Standard deviations that have at least one cause for this object,
        sorted — the mirror of objects_for_deviation(), used by
        EquipmentDeviationBar to offer the same richer, object-based
        deviation/cause set as StandardCausesPickerPopup instead of the
        narrower literal-comp_type lookup (see NOTES.md)."""
        rows = self.conn.execute(
            """SELECT sd.id, sd.description, sd.sort_order,
                      COUNT(sc.id) AS n_causes
               FROM standard_deviations sd
               JOIN standard_causes sc ON sc.deviation_id = sd.id
               WHERE sc.object_id = ?
               GROUP BY sd.id
               ORDER BY sd.sort_order, sd.id""",
            (object_id,)).fetchall()
        return [dict(r) for r in rows]

    def all_objects_with_cause_counts(self, deviation_id):
        """All standard objects with cause count for this deviation (0 = no causes yet)."""
        rows = self.conn.execute(
            """SELECT so.id, so.name, so.sort_order,
                      COALESCE(cnt.n, 0) AS n_causes
               FROM standard_objects so
               LEFT JOIN (
                   SELECT object_id, COUNT(*) AS n
                   FROM standard_causes WHERE deviation_id=?
                   GROUP BY object_id
               ) cnt ON cnt.object_id = so.id
               ORDER BY so.sort_order, so.name""",
            (deviation_id,)).fetchall()
        return [dict(r) for r in rows]

    def standard_causes_for_object(self, deviation_id, object_id):
        """Standard causes for a specific deviation + object combination."""
        return [dict(r) for r in self.conn.execute(
            """SELECT sc.id, sc.description, sc.sort_order, sc.comp_type,
                      sc.frequency, sc.use_in_cause_form, sc.object_id
               FROM standard_causes sc
               WHERE sc.deviation_id=? AND sc.object_id=?
               ORDER BY sc.sort_order, sc.id""",
            (deviation_id, object_id))]

    def add_standard_cause_with_object(self, deviation_id, object_id, description):
        max_ord = (self.conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_causes WHERE deviation_id=?",
            (deviation_id,)).fetchone()[0] or 0)
        # Look up comp_type from object name for backwards compat
        obj = self.conn.execute(
            "SELECT name FROM standard_objects WHERE id=?", (object_id,)).fetchone()
        comp = obj[0] if obj else ''
        cur = self.conn.execute(
            "INSERT INTO standard_causes (deviation_id, description, sort_order, comp_type, object_id)"
            " VALUES (?,?,?,?,?)",
            (deviation_id, description, max_ord + 1, comp, object_id))
        self.commit()
        return cur.lastrowid

    # ── Standard objects ──────────────────────────────────────────────────────
    def standard_objects(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT id, name, sort_order FROM standard_objects ORDER BY sort_order, id")]

    def add_standard_object(self, name):
        max_ord = (self.conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM standard_objects").fetchone()[0] or 0)
        cur = self.conn.execute(
            "INSERT INTO standard_objects (name, sort_order) VALUES (?,?)", (name, max_ord + 1))
        self.commit()
        return cur.lastrowid

    def update_standard_object(self, id_, name):
        self.conn.execute("UPDATE standard_objects SET name=? WHERE id=?", (name, id_))
        self.commit()

    def delete_standard_object(self, id_):
        self.conn.execute("DELETE FROM standard_objects WHERE id=?", (id_,))
        self.commit()

    def reorder_standard_objects(self, ordered_ids):
        for i, id_ in enumerate(ordered_ids):
            self.conn.execute("UPDATE standard_objects SET sort_order=? WHERE id=?", (i, id_))
        self.commit()

    # ── Cause descriptions ────────────────────────────────────────────────────
    def cause_descriptions(self, cause_id):
        return [dict(r) for r in self.conn.execute(
            "SELECT id, description, sort_order FROM cause_descriptions "
            "WHERE cause_id=? ORDER BY sort_order, id", (cause_id,))]

    def add_cause_description(self, cause_id, description):
        max_ord = (self.conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM cause_descriptions WHERE cause_id=?",
            (cause_id,)).fetchone()[0] or 0)
        cur = self.conn.execute(
            "INSERT INTO cause_descriptions (cause_id, description, sort_order) VALUES (?,?,?)",
            (cause_id, description, max_ord + 1))
        self.commit()
        return cur.lastrowid

    def update_cause_description(self, id_, description):
        self.conn.execute("UPDATE cause_descriptions SET description=? WHERE id=?",
                          (description, id_))
        self.commit()

    def delete_cause_description(self, id_):
        self.conn.execute("DELETE FROM cause_descriptions WHERE id=?", (id_,))
        self.commit()

    def reorder_cause_descriptions(self, ordered_ids):
        for i, id_ in enumerate(ordered_ids):
            self.conn.execute("UPDATE cause_descriptions SET sort_order=? WHERE id=?", (i, id_))
        self.commit()

    def add_cause(self, deviation_id):
        dev = self.get_deviation(deviation_id)
        node_id = dev['node_id'] if dev else None
        cur = self.conn.execute(
            "INSERT INTO causes (node_id,deviation_id,description,likelihood) VALUES (?,?,'Ny orsak',1)",
            (node_id, deviation_id))
        self.commit()
        return cur.lastrowid

    def add_consequence(self, cause_id):
        cur = self.conn.execute(
            "INSERT INTO consequences (cause_id,description,severity) VALUES (?,'Ny konsekvens',1)", (cause_id,))
        self.commit()
        return cur.lastrowid

    def add_safeguard(self, consequence_id):
        cur = self.conn.execute(
            "INSERT INTO safeguards (consequence_id,description,rrf) VALUES (?,'Ny safeguard',1)", (consequence_id,))
        self.commit()
        return cur.lastrowid

    def add_action(self, consequence_id):
        cur = self.conn.execute(
            "INSERT INTO actions (consequence_id,description,status) VALUES (?,'Ny åtgärd','Öppen')",
            (consequence_id,))
        self.commit()
        return cur.lastrowid

    # ── Update ────────────────────────────────────────────────────────────────
    def set_cause_comment(self, cause_id, comment):
        self.conn.execute("UPDATE causes SET comment=? WHERE id=?", (comment, cause_id))
        self.commit()

    def get_cause_comment(self, cause_id):
        r = self.conn.execute("SELECT comment FROM causes WHERE id=?", (cause_id,)).fetchone()
        return r[0] if r else ''

    def approve_node(self, node_id, user):
        import datetime as _dt
        self.conn.execute(
            "UPDATE nodes SET study_status='approved', approved_by=?, approved_at=? WHERE id=?",
            (user, _dt.datetime.now().strftime('%Y-%m-%d %H:%M'), node_id))
        self.commit()

    def set_node_status(self, node_id, status):
        self.conn.execute("UPDATE nodes SET study_status=? WHERE id=?", (status, node_id))
        self.commit()

    # ── Backup system ─────────────────────────────────────────────────────────
    # Backups live in  <project_dir>/hazop_backups/  so they never clutter the
    # project folder itself.  Two tiers:
    #   • Hourly snapshots  kept 48 h  — cover accidental data loss within a day
    #   • Daily  snapshots  kept 30 d  — cover longer-term "I need last week"
    # The DB uses SQLite's built-in BACKUP API so the copy is always consistent
    # even while the connection is live.

    _BACKUP_DIR_NAME   = "hazop_backups"
    _HOURLY_KEEP_H     = 48      # keep hourly backups for this many hours
    _DAILY_KEEP_D      = 30      # keep daily backups for this many days
    _COMMIT_INTERVAL_S  = 120    # write a new hourly backup at most every N seconds
    _PRUNE_INTERVAL_S   = 3600   # prune at most once per hour
    _last_backup_ts: float = 0.0
    _last_prune_ts:  float = 0.0

    def _backup_dir(self) -> 'Path':
        d = self.path.parent / self._BACKUP_DIR_NAME
        d.mkdir(exist_ok=True)
        return d

    def _write_backup(self, startup: bool = False):
        """Write a timestamped backup using SQLite's online backup API.

        Throttled to at most once per _COMMIT_INTERVAL_S seconds so that
        frequent commits don't hammer the disk, but startup always writes.
        Returns the backup file's Path on success, or None (throttled or
        failed) — existing call sites all ignore the return value, so this
        is purely additive for callers (e.g. a manual "backup now" button)
        that want to report success/failure to the user.
        """
        import time, sqlite3, datetime as _dt
        now = time.monotonic()
        if not startup and (now - Database._last_backup_ts) < self._COMMIT_INTERVAL_S:
            return None
        Database._last_backup_ts = now
        try:
            d   = self._backup_dir()
            ts  = _dt.datetime.now().strftime('%Y-%m-%dT%H-%M-%S-%f')
            dst = d / f"backup_{ts}.db"
            # SQLite online backup — safe while the DB is open and being written
            bk_conn = sqlite3.connect(str(dst))
            with bk_conn:
                self.conn.backup(bk_conn)
            bk_conn.close()
            self._prune_backups(d)
            return dst
        except Exception:
            return None   # never crash the app due to backup failure

    def _prune_backups(self, d: 'Path'):
        """Remove old backups according to retention policy (rate-limited to once/hour)."""
        import time, datetime as _dt
        now_ts = time.monotonic()
        if (now_ts - Database._last_prune_ts) < self._PRUNE_INTERVAL_S:
            return
        Database._last_prune_ts = now_ts

        now   = _dt.datetime.now()
        files = sorted(d.glob("backup_*.db"), reverse=True)
        # Skip entirely if the directory is too small to prune anything
        if len(files) <= self._HOURLY_KEEP_H + self._DAILY_KEEP_D:
            return
        # Parse timestamp from filename; skip files that don't match
        def parse_ts(f):
            for fmt in ("backup_%Y-%m-%dT%H-%M-%S-%f", "backup_%Y-%m-%dT%H-%M-%S"):
                try:
                    return _dt.datetime.strptime(f.stem, fmt)
                except ValueError:
                    pass
            return None
        kept_dates = set()   # dates for which we already have a daily backup
        for f in files:
            ts = parse_ts(f)
            if ts is None:
                continue
            age_h = (now - ts).total_seconds() / 3600
            date_key = ts.date()
            # Always keep if within hourly window
            if age_h <= self._HOURLY_KEEP_H:
                continue
            # Outside hourly window — keep ONE per calendar day for the daily window
            if (now - ts).days <= self._DAILY_KEEP_D:
                if date_key not in kept_dates:
                    kept_dates.add(date_key)
                    continue   # keep this one as the day's representative
            # Otherwise delete
            try:
                f.unlink()
            except Exception:
                pass

    def commit(self):
        """Write-through commit: flush DB, then write a throttled backup."""
        try:
            self.conn.commit()
            self._write_backup()
        except Exception:
            pass

    def touch_node(self, node_id):
        """Update updated_at/updated_by on node (feature 20)."""
        import datetime as _dt
        user = (self.get_config('user_name', '') or '').strip() or 'okänd'
        self.conn.execute(
            "UPDATE nodes SET updated_at=?,updated_by=? WHERE id=?",
            (_dt.datetime.now().strftime('%Y-%m-%d %H:%M'), user, node_id))
        self.commit()

    def update_node(self, id_, name, description, pid_ref,
                    media='', pressure='', temperature=''):
        self.conn.execute(
            "UPDATE nodes SET name=?,description=?,pid_ref=?,"
            "media=?,pressure=?,temperature=? WHERE id=?",
            (name, description, pid_ref, media, pressure, temperature, id_))
        self.commit()
        self.touch_node(id_)

    # ── Node markup CRUD ──────────────────────────────────────────────────────
    def add_node_markup(self, node_id, type_, pts, label, color, opacity, line_width, page,
                        font_size=12):
        cur = self.conn.execute(
            "INSERT INTO node_markups (node_id,type,points,label,color,opacity,line_width,"
            "font_size,pid_page) VALUES (?,?,?,?,?,?,?,?,?)",
            (node_id, type_, json.dumps(pts), label, color, opacity, line_width,
             font_size, page))
        self.commit()
        return cur.lastrowid

    def node_markups_for_node(self, node_id):
        return self.conn.execute(
            "SELECT * FROM node_markups WHERE node_id=? ORDER BY sort_order,id",
            (node_id,)).fetchall()

    def node_markups_for_page(self, page):
        return self.conn.execute(
            "SELECT * FROM node_markups WHERE pid_page=? ORDER BY sort_order,id",
            (page,)).fetchall()

    def get_node_markup(self, mu_id):
        row = self.conn.execute(
            "SELECT * FROM node_markups WHERE id=?", (mu_id,)).fetchone()
        return dict(row) if row else None

    def update_node_markup(self, mu_id, label=None, color=None, opacity=None,
                           line_width=None, font_size=None, visible=None, points=None):
        sets, vals = [], []
        if label      is not None: sets.append("label=?");      vals.append(label)
        if color      is not None: sets.append("color=?");      vals.append(color)
        if opacity    is not None: sets.append("opacity=?");    vals.append(opacity)
        if line_width is not None: sets.append("line_width=?"); vals.append(line_width)
        if font_size  is not None: sets.append("font_size=?");  vals.append(font_size)
        if visible    is not None: sets.append("visible=?");    vals.append(int(visible))
        if points     is not None: sets.append("points=?");     vals.append(json.dumps(points))
        if sets:
            vals.append(mu_id)
            self.conn.execute(f"UPDATE node_markups SET {','.join(sets)} WHERE id=?", vals)
            self.commit()

    def delete_node_markup(self, mu_id):
        self.conn.execute("DELETE FROM node_markups WHERE id=?", (mu_id,))
        self.commit()

    def set_all_node_markups_visible(self, node_id, visible):
        self.conn.execute("UPDATE node_markups SET visible=? WHERE node_id=?",
                          (int(visible), node_id))
        self.commit()

    def has_node_markups(self, node_id) -> bool:
        r = self.conn.execute(
            "SELECT COUNT(*) FROM node_markups WHERE node_id=?", (node_id,)).fetchone()
        return r[0] > 0

    def has_visible_node_markups(self, node_id) -> bool:
        r = self.conn.execute(
            "SELECT COUNT(*) FROM node_markups WHERE node_id=? AND visible=1",
            (node_id,)).fetchone()
        return r[0] > 0

    # ── Node red markup CRUD ──────────────────────────────────────────────────
    def add_node_red_markup(self, node_id, type_, pts, label, color, opacity,
                            line_width, page, font_size=12,
                            symbol_w=40, symbol_h=40, symbol_rot=0):
        cur = self.conn.execute(
            "INSERT INTO node_red_markups "
            "(node_id,type,points,label,color,opacity,line_width,font_size,pid_page,"
            "symbol_w,symbol_h,symbol_rot) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (node_id, type_, json.dumps(pts), label, color, opacity, line_width,
             font_size, page, symbol_w, symbol_h, symbol_rot))
        self.commit()
        return cur.lastrowid

    def node_red_markups_for_node(self, node_id):
        return self.conn.execute(
            "SELECT * FROM node_red_markups WHERE node_id=? ORDER BY sort_order,id",
            (node_id,)).fetchall()

    def node_red_markups_for_page(self, page):
        return self.conn.execute(
            "SELECT * FROM node_red_markups WHERE pid_page=? ORDER BY sort_order,id",
            (page,)).fetchall()

    def get_node_red_markup(self, mu_id):
        row = self.conn.execute(
            "SELECT * FROM node_red_markups WHERE id=?", (mu_id,)).fetchone()
        return dict(row) if row else None

    def update_node_red_markup(self, mu_id, label=None, color=None, opacity=None,
                               line_width=None, font_size=None, visible=None,
                               points=None, symbol_w=None, symbol_h=None, symbol_rot=None):
        sets, vals = [], []
        if label      is not None: sets.append("label=?");      vals.append(label)
        if color      is not None: sets.append("color=?");      vals.append(color)
        if opacity    is not None: sets.append("opacity=?");    vals.append(opacity)
        if line_width is not None: sets.append("line_width=?"); vals.append(line_width)
        if font_size  is not None: sets.append("font_size=?");  vals.append(font_size)
        if visible    is not None: sets.append("visible=?");    vals.append(int(visible))
        if points     is not None: sets.append("points=?");     vals.append(json.dumps(points))
        if symbol_w   is not None: sets.append("symbol_w=?");   vals.append(symbol_w)
        if symbol_h   is not None: sets.append("symbol_h=?");   vals.append(symbol_h)
        if symbol_rot is not None: sets.append("symbol_rot=?"); vals.append(symbol_rot)
        if sets:
            vals.append(mu_id)
            self.conn.execute(
                f"UPDATE node_red_markups SET {','.join(sets)} WHERE id=?", vals)
            self.commit()

    def delete_node_red_markup(self, mu_id):
        self.conn.execute("DELETE FROM node_red_markups WHERE id=?", (mu_id,))
        self.commit()

    def set_all_node_red_markups_visible(self, node_id, visible):
        self.conn.execute(
            "UPDATE node_red_markups SET visible=? WHERE node_id=?",
            (int(visible), node_id))
        self.commit()

    def has_node_red_markups(self, node_id) -> bool:
        r = self.conn.execute(
            "SELECT COUNT(*) FROM node_red_markups WHERE node_id=?", (node_id,)).fetchone()
        return r[0] > 0

    def has_visible_node_red_markups(self, node_id) -> bool:
        r = self.conn.execute(
            "SELECT COUNT(*) FROM node_red_markups WHERE node_id=? AND visible=1",
            (node_id,)).fetchone()
        return r[0] > 0

    def get_node_number(self, node_id) -> int:
        """Return 1-based position of node_id in creation order (0 if not found)."""
        rows = self.conn.execute("SELECT id FROM nodes ORDER BY id").fetchall()
        for i, r in enumerate(rows, 1):
            if r[0] == node_id:
                return i
        return 0

    def sync_node_text_markups(self, node_id, new_name):
        """Update label of all 'text' type markups for a node to match its new name."""
        self.conn.execute(
            "UPDATE node_markups SET label=? WHERE node_id=? AND type='text'",
            (new_name, node_id))
        self.commit()

    _SENTINEL = object()

    def update_cause(self, id_, description=None, likelihood=None, base_frequency=_SENTINEL,
                     standard_cause_id=_SENTINEL, comp_type=_SENTINEL, comp_tag=_SENTINEL,
                     base_freq=_SENTINEL):
        # Support old parameter name for backward compatibility
        if base_freq is not Database._SENTINEL and base_frequency is Database._SENTINEL:
            base_frequency = base_freq

        sets, vals = [], []
        if description is not None:
            sets.append("description=?"); vals.append(description)
        if likelihood is not None:
            sets.append("likelihood=?"); vals.append(likelihood)
        if base_frequency is not Database._SENTINEL:
            sets.append("base_frequency=?"); vals.append(base_frequency)
        if standard_cause_id is not Database._SENTINEL:
            sets.append("standard_cause_id=?"); vals.append(standard_cause_id)
        if comp_type is not Database._SENTINEL:
            sets.append("comp_type=?"); vals.append(comp_type)
        if comp_tag is not Database._SENTINEL:
            sets.append("comp_tag=?"); vals.append(comp_tag)
        if sets:
            vals.append(id_)
            self.conn.execute(f"UPDATE causes SET {', '.join(sets)} WHERE id=?", vals)
            self.commit()

        # Learn from every explicit tag+type assignment, regardless of which
        # UI path triggered it.  Read back the current values if either param
        # was not part of this call so we always have both.
        effective_ct  = comp_type  if comp_type  is not Database._SENTINEL else None
        effective_tag = comp_tag   if comp_tag   is not Database._SENTINEL else None
        if effective_ct is None or effective_tag is None:
            row = self.conn.execute(
                "SELECT comp_type, comp_tag FROM causes WHERE id=?", (id_,)).fetchone()
            if row:
                if effective_ct  is None: effective_ct  = row['comp_type']  or ''
                if effective_tag is None: effective_tag = row['comp_tag']   or ''
        if effective_ct and effective_tag:
            try:
                self.upsert_tag_memory(effective_tag, effective_ct)
            except Exception:
                pass

    def update_cause_freqs_from_standard(self):
        """Overwrite base_frequency on all causes linked to a standard cause that has a frequency."""
        self.conn.execute("""
            UPDATE causes
            SET base_frequency = (
                SELECT frequency FROM standard_causes WHERE id = causes.standard_cause_id
            )
            WHERE standard_cause_id IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM standard_causes
                WHERE id = causes.standard_cause_id AND frequency IS NOT NULL
              )
        """)
        n = _sync_f_levels_from_base_frequency(self.conn)
        self.commit()
        return n

    def update_consequence(self, id_, description, severity, category='',
                           consequence_chain='', comp_tag=None, comp_type=None,
                           tagged_refs=None):
        self.conn.execute(
            "UPDATE consequences SET description=?,severity=?,category=?,"
            "consequence_chain=? WHERE id=?",
            (description, severity, category, consequence_chain, id_))
        # comp_tag/comp_type (2026-08-07, drag-and-drop tag from P&ID —
        # see NOTES.md) — optional, None means "don't touch", same
        # backward-compatible convention update_cause already uses, so
        # every existing call site (which never passes these) is unaffected.
        # tagged_refs (2026-08-09) follows the same optional convention.
        if comp_tag is not None or comp_type is not None or tagged_refs is not None:
            parts, vals = [], []
            if comp_tag is not None:
                parts.append("comp_tag=?"); vals.append(comp_tag)
            if comp_type is not None:
                parts.append("comp_type=?"); vals.append(comp_type)
            if tagged_refs is not None:
                parts.append("tagged_refs=?"); vals.append(tagged_refs)
            vals.append(id_)
            self.conn.execute(f"UPDATE consequences SET {', '.join(parts)} WHERE id=?", vals)
        self.commit()

    def set_consequence_tag(self, id_, comp_tag, comp_type):
        """Attach an equipment tag/type to a consequence's tag-strip
        display without touching its description/severity — the
        low-level primitive append_tag_to_consequence() builds on to also
        update the free text (2026-08-09, see NOTES.md)."""
        self.conn.execute(
            "UPDATE consequences SET comp_tag=?, comp_type=? WHERE id=?",
            (comp_tag, comp_type, id_))
        self.commit()

    def set_safeguard_tag(self, id_, comp_tag, comp_type):
        """Attach an equipment tag/type to a safeguard's tag-strip display
        without touching its description/rrf — same complement-not-
        replacement rule as set_consequence_tag; append_tag_to_safeguard()
        builds on this to also update the free text (2026-08-09, see
        NOTES.md)."""
        self.conn.execute(
            "UPDATE safeguards SET comp_tag=?, comp_type=? WHERE id=?",
            (comp_tag, comp_type, id_))
        self.commit()

    def append_tag_to_consequence(self, id_, comp_tag, comp_type):
        """Drag-and-drop an equipment marker onto a KON cell (2026-08-09,
        see NOTES.md): appends the tag into the free-text description
        (building a running sentence across repeated drags, e.g. "hög
        nivå i" -> "hög nivå i TA-1" -> "... => överbreddning till TA-2"),
        instead of the tag-strip-only behavior of set_consequence_tag,
        which used to silently overwrite the PREVIOUS drop's tag on every
        new one. Still updates the tag-strip too (shows the most recently
        dropped tag) — the full history lives in the text now."""
        row = self.get_consequence(id_)
        if not row:
            return
        new_desc = append_tag_to_text(row['description'], comp_tag)
        new_refs = add_tag_ref(row.get('tagged_refs') or '', comp_tag)
        self.update_consequence(id_, new_desc, row['severity'], row['category'] or '',
                                 row['consequence_chain'] or '',
                                 comp_tag=comp_tag, comp_type=comp_type,
                                 tagged_refs=new_refs)

    def append_tag_to_safeguard(self, id_, comp_tag, comp_type):
        """Same as append_tag_to_consequence, for a safeguard cell."""
        row = self.get_safeguard(id_)
        if not row:
            return
        new_desc = append_tag_to_text(row['description'], comp_tag)
        new_refs = add_tag_ref(row.get('tagged_refs') or '', comp_tag)
        self.update_safeguard(id_, description=new_desc, tagged_refs=new_refs)
        self.set_safeguard_tag(id_, comp_tag, comp_type)

    def update_safeguard(self, id_, description=None, rrf=None, sg_type=None, tagged_refs=None):
        if description is None and rrf is None and sg_type is None and tagged_refs is None:
            return
        parts, vals = [], []
        if description is not None:
            parts.append("description=?"); vals.append(description)
        if rrf is not None:
            parts.append("rrf=?"); vals.append(rrf)
        if sg_type is not None:
            parts.append("sg_type=?"); vals.append(sg_type)
        if tagged_refs is not None:
            parts.append("tagged_refs=?"); vals.append(tagged_refs)
        vals.append(id_)
        self.conn.execute(f"UPDATE safeguards SET {', '.join(parts)} WHERE id=?", vals)
        self.commit()

    def update_action(self, id_, description, responsible, due_date, status):
        self.conn.execute(
            "UPDATE actions SET description=?,responsible=?,due_date=?,status=? WHERE id=?",
            (description, responsible, due_date, status, id_))
        self.commit()

    # ── Delete ────────────────────────────────────────────────────────────────
    def delete_node(self, id_):
        # Deleting a node cascades through causes -> consequences -> safeguards,
        # i.e. it can wipe out an entire branch of the HAZOP tree in one go.
        # Force an un-throttled backup right before this destructive operation
        # so a crash mid-cascade (or a bug in the cascade logic) is always
        # recoverable. Never let a backup failure block the actual delete.
        try:
            self._write_backup(startup=True)
        except Exception:
            logging.warning("Pre-delete backup failed", exc_info=True)
        # No FK cascade exists from causes(node_id) down into
        # consequence_severities / consequence_severity_exclusions / linked_consequence_id,
        # so route through delete_cause() for each direct cause (mirrors delete_deviation()).
        for cause in self.causes(id_):
            self.delete_cause(cause['id'])
        # equipment_catalog.node_id (added 2026-08-07 for "Nod → Utrustning
        # → Avvikelse", see NOTES.md) has NO ON DELETE clause — unlike
        # deviations.node_id, equipment assigned to this node must NOT be
        # deleted along with it (the assignment is optional/soft, the
        # equipment itself lives independently in the register), so clear
        # the assignment instead of cascading. Without this, deleting a
        # node with any equipment assigned to it raised sqlite3.IntegrityError:
        # FOREIGN KEY constraint failed (real crash report, 2026-08-07).
        self.conn.execute("UPDATE equipment_catalog SET node_id=NULL WHERE node_id=?", (id_,))
        self.conn.execute("DELETE FROM nodes WHERE id=?", (id_,)); self.commit()

    def delete_cause(self, id_):
        # Clean up severity/exclusion data for all consequences under this cause
        # (no FK cascade exists for these tables).
        self.conn.execute(
            "DELETE FROM consequence_severity_exclusions "
            "WHERE severity_id IN ("
            "  SELECT cs.id FROM consequence_severities cs "
            "  JOIN consequences c ON cs.consequence_id = c.id "
            "  WHERE c.cause_id=?)",
            (id_,))
        self.conn.execute(
            "DELETE FROM consequence_severities "
            "WHERE consequence_id IN (SELECT id FROM consequences WHERE cause_id=?)",
            (id_,))
        self.conn.execute(
            "UPDATE causes SET linked_consequence_id=NULL "
            "WHERE linked_consequence_id IN (SELECT id FROM consequences WHERE cause_id=?)",
            (id_,))
        self.conn.execute(
            "DELETE FROM consequence_severity_exclusions WHERE safeguard_id IN ("
            "  SELECT id FROM safeguards WHERE consequence_id IN "
            "  (SELECT id FROM consequences WHERE cause_id=?))",
            (id_,))
        self.conn.execute("DELETE FROM causes WHERE id=?", (id_,)); self.commit()

    def delete_consequence(self, id_):
        # Clean up orphaned severity data (no FK cascade exists for these tables)
        self.conn.execute(
            "DELETE FROM consequence_severity_exclusions "
            "WHERE severity_id IN (SELECT id FROM consequence_severities WHERE consequence_id=?)",
            (id_,))
        self.conn.execute("DELETE FROM consequence_severities WHERE consequence_id=?", (id_,))
        # Null out any causes that chain-link to this consequence (cross-branch reference, no FK)
        self.conn.execute("UPDATE causes SET linked_consequence_id=NULL WHERE linked_consequence_id=?", (id_,))
        self.conn.execute(
            "DELETE FROM consequence_severity_exclusions WHERE safeguard_id IN "
            "(SELECT id FROM safeguards WHERE consequence_id=?)",
            (id_,))
        self.conn.execute("DELETE FROM consequences WHERE id=?", (id_,)); self.commit()

    # ── Consequence steps (Del1-Del5 escalation chain) ────────────────────────
    def get_consequence_steps(self, consequence_id):
        """Return list of dicts: {step, text, ref_tag, node_key} sorted by step."""
        rows = self.conn.execute(
            "SELECT step, text, ref_tag, node_key FROM consequence_steps "
            "WHERE consequence_id=? ORDER BY step", (consequence_id,)).fetchall()
        return [dict(r) for r in rows]

    def set_consequence_steps(self, consequence_id, steps):
        """Replace all steps for a consequence.

        steps: list of dicts with keys step(int), text(str), ref_tag(str)
        and optional node_key(str) — the consequence-graph node id, used to
        restore dependent column options when the chain is reopened.
        Empty text entries are omitted.
        """
        self.conn.execute(
            "DELETE FROM consequence_steps WHERE consequence_id=?",
            (consequence_id,))
        for s in steps:
            text = (s.get('text') or '').strip()
            if text:
                self.conn.execute(
                    "INSERT INTO consequence_steps "
                    "(consequence_id, step, text, ref_tag, node_key)"
                    " VALUES (?,?,?,?,?)",
                    (consequence_id, int(s['step']), text,
                     (s.get('ref_tag') or '').strip(),
                     (s.get('node_key') or '').strip()))
        self.commit()

    def consequence_steps_as_text(self, consequence_id):
        """Return 'Del1 → Del2 → …' string built from stored steps."""
        rows = self.get_consequence_steps(consequence_id)
        parts = [r['text'] for r in rows if r['text']]
        return ' → '.join(parts) if parts else ''

    def delete_safeguard(self, id_):
        # No FK cascade exists for consequence_severity_exclusions.safeguard_id
        self.conn.execute("DELETE FROM consequence_severity_exclusions WHERE safeguard_id=?", (id_,))
        self.conn.execute("DELETE FROM safeguards WHERE id=?", (id_,)); self.commit()

    def delete_action(self, id_):
        self.conn.execute("DELETE FROM actions WHERE id=?", (id_,)); self.commit()

    # ── Reduction factors ─────────────────────────────────────────────────────
    def reduction_factors(self, consequence_id):
        return self.conn.execute(
            "SELECT * FROM reduction_factors WHERE consequence_id=? ORDER BY id",
            (consequence_id,)).fetchall()

    def add_reduction_factor(self, consequence_id, description='', rrf=10):
        cur = self.conn.execute(
            "INSERT INTO reduction_factors (consequence_id,description,rrf,active) VALUES (?,?,?,1)",
            (consequence_id, description, rrf))
        self.commit()
        return cur.lastrowid

    def update_reduction_factor(self, id_, description, rrf, active):
        self.conn.execute(
            "UPDATE reduction_factors SET description=?,rrf=?,active=? WHERE id=?",
            (description, rrf, int(active), id_))
        self.commit()

    def delete_reduction_factor(self, id_):
        self.conn.execute("DELETE FROM reduction_factors WHERE id=?", (id_,))
        self.commit()

    def update_consequence_factors(self, id_, fa_active, fa_rrf, ignition_active, ignition_rrf):
        self.conn.execute(
            "UPDATE consequences SET fa_active=?,fa_rrf=?,ignition_active=?,ignition_rrf=? WHERE id=?",
            (int(fa_active), fa_rrf, int(ignition_active), ignition_rrf, id_))
        self.commit()

    # ── Copy support ──────────────────────────────────────────────────────────
    def copy_cause(self, cause_id, target_deviation_id):
        orig = self.get_cause(cause_id)
        if not orig:
            return None
        dev = self.get_deviation(target_deviation_id)
        node_id = dev['node_id'] if dev else orig['node_id']
        cur = self.conn.execute(
            "INSERT INTO causes (node_id,deviation_id,description,likelihood,source_id) VALUES (?,?,?,?,?)",
            (node_id, target_deviation_id, orig['description'], orig['likelihood'], cause_id))
        self.commit()
        return cur.lastrowid

    def copy_consequence(self, cons_id, target_cause_id):
        orig = self.get_consequence(cons_id)
        if not orig:
            return None
        cur = self.conn.execute(
            "INSERT INTO consequences (cause_id,description,severity,category,"
            "fa_active,fa_rrf,ignition_active,ignition_rrf,source_id) VALUES (?,?,?,?,?,?,?,?,?)",
            (target_cause_id, orig['description'], orig['severity'], orig['category'] or '',
             orig['fa_active'] or 0, orig['fa_rrf'] or 10,
             orig['ignition_active'] or 0, orig['ignition_rrf'] or 10, cons_id))
        self.commit()
        new_id = cur.lastrowid
        # Copy safeguards
        for sg in self.safeguards(cons_id):
            self.conn.execute(
                "INSERT INTO safeguards (consequence_id,description,rrf,sg_type,source_id) VALUES (?,?,?,?,?)",
                (new_id, sg['description'], sg['rrf'], dict(sg).get('sg_type','Övrigt'), sg['id']))
        # Copy reduction factors
        for rf in self.reduction_factors(cons_id):
            self.conn.execute(
                "INSERT INTO reduction_factors (consequence_id,description,rrf,active) VALUES (?,?,?,?)",
                (new_id, rf['description'], rf['rrf'], rf['active']))
        self.commit()
        return new_id

    def copy_safeguard(self, sg_id, target_cons_id):
        orig = self.get_safeguard(sg_id)
        if not orig:
            return None
        cur = self.conn.execute(
            "INSERT INTO safeguards (consequence_id,description,rrf,sg_type,source_id) VALUES (?,?,?,?,?)",
            (target_cons_id, orig['description'], orig['rrf'],
             dict(orig).get('sg_type', 'Övrigt'), sg_id))
        self.commit()
        return cur.lastrowid

    # ── Move support ──────────────────────────────────────────────────────────
    def move_cause(self, cause_id, target_node_id):
        self.conn.execute("UPDATE causes SET node_id=? WHERE id=?",
                          (target_node_id, cause_id))
        self.commit()

    def move_cause_to_deviation(self, cause_id, target_deviation_id):
        dev = self.get_deviation(target_deviation_id)
        if dev:
            self.conn.execute(
                "UPDATE causes SET deviation_id=?, node_id=? WHERE id=?",
                (target_deviation_id, dev['node_id'], cause_id))
            self.commit()

    def move_consequence(self, cons_id, target_cause_id):
        self.conn.execute("UPDATE consequences SET cause_id=? WHERE id=?",
                          (target_cause_id, cons_id))
        self.commit()

    def move_safeguard(self, sg_id, target_cons_id):
        self.conn.execute("UPDATE safeguards SET consequence_id=? WHERE id=?",
                          (target_cons_id, sg_id))
        self.commit()

    def stats(self):
        return {
            'nodes':        self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0],
            'causes':       self.conn.execute("SELECT COUNT(*) FROM causes").fetchone()[0],
            'consequences': self.conn.execute("SELECT COUNT(*) FROM consequences").fetchone()[0],
            'safeguards':   self.conn.execute("SELECT COUNT(*) FROM safeguards").fetchone()[0],
            'open_actions': self.conn.execute(
                "SELECT COUNT(*) FROM actions WHERE status='Öppen'").fetchone()[0],
        }

    def all_data(self):
        rows = []
        for node in self.nodes():
            for cause in self.causes(node['id']):
                for cons in self.consequences(cause['id']):
                    sgs = [dict(s) for s in self.safeguards(cons['id'])]
                    acts = [dict(a) for a in self.actions(cons['id'])]
                    rows.append({
                        'node_name':      node['name'],
                        'node_pid':       node['pid_ref'] or '',
                        'cause_id':       cause['id'],
                        'cause':          cause['description'],
                        'likelihood':     self.cause_frequency_level(cause),
                        'consequence_id': cons['id'],
                        'consequence':    cons['description'],
                        'severity':       cons['severity'],
                        'category':       cons['category'] or '',
                        'safeguards':     sgs,
                        'safeguards_text': '; '.join(s['description'] for s in sgs),
                        'actions':        acts,
                    })
        return rows


# ══════════════════════════════════════════════════════════════════════════════
# SHARED WIDGETS
# ══════════════════════════════════════════════════════════════════════════════

class RiskBadge(QLabel):
    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedSize(200, 38)
        self.setWordWrap(True)
        f = QFont("Consolas", 9); f.setBold(True)
        self.setFont(f)
        self.set_empty()

    def update_risk(self, frequency, consequence, base_frequency_per_year=None):
        label, bg, fg = risk_info(frequency, consequence)
        if base_frequency_per_year is not None:
            freq_str = f"{base_frequency_per_year:g}/år"
            self.setText(f"{label}  F={frequency} C={consequence}\n🗄️ {freq_str}")
        else:
            self.setText(f"{label}  F={frequency} C={consequence}")
        self.setStyleSheet(f"background:{bg}; color:{fg}; border-radius:5px; padding:2px 8px;")

    def set_empty(self):
        self.setText("—  (ingen frekvens)")
        self.setStyleSheet(
            "background:#F5F5F3; color:#8D9299; border-radius:4px; "
            "padding:2px 8px; border:1px solid #E2E3E1;")


class SafeguardEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.consequence_id = None
        self._parent_cause_likelihood = 1

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        btn = QPushButton("+ Lägg till safeguard")
        btn.clicked.connect(self._add)
        layout.addWidget(btn)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Beskrivning', 'Typ', 'RRF', 'Eff. risk', ''])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 100)
        self.table.setColumnWidth(4, 65)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setFixedHeight(CONFIG['H_TABLE_STD'])
        layout.addWidget(self.table)

    def load(self, consequence_id, cause_likelihood=1):
        self.consequence_id = consequence_id
        self._parent_cause_likelihood = cause_likelihood
        self._refresh()

    def _refresh(self):
        try:
            self.table.cellChanged.disconnect()
        except Exception:
            pass
        self.table.setRowCount(0)
        if self.consequence_id is None:
            return
        cons = self.db.get_consequence(self.consequence_id)
        severity = dict(cons).get('severity', 1) if cons else 1
        for sg in self.db.safeguards(self.consequence_id):
            row = self.table.rowCount()
            self.table.insertRow(row)
            sg_d = dict(sg)

            item = QTableWidgetItem(sg_d['description'])
            item.setData(Qt.ItemDataRole.UserRole, sg_d['id'])
            self.table.setItem(row, 0, item)

            sid = sg_d['id']
            type_combo = QComboBox()
            type_combo.addItems(SG_TYPES)
            sg_type = sg_d.get('sg_type', 'Övrigt') or 'Övrigt'
            type_combo.setCurrentIndex(SG_TYPES.index(sg_type) if sg_type in SG_TYPES else len(SG_TYPES)-1)
            type_combo.currentIndexChanged.connect(
                lambda idx, s=sid, r=row: self._type_changed(s, r, idx))
            self.table.setCellWidget(row, 1, type_combo)

            rrf_combo = QComboBox()
            rrf_combo.addItems(RRF_LABELS)
            sg_rrf_val = sg_d['rrf'] if sg_d['rrf'] is not None else 1
            rrf_idx = RRF_VALUES.index(sg_rrf_val) if sg_rrf_val in RRF_VALUES else 0
            rrf_combo.setCurrentIndex(rrf_idx)
            rrf_combo.currentIndexChanged.connect(
                lambda idx, s=sid, r=row: self._rrf_changed(s, r, idx))
            self.table.setCellWidget(row, 2, rrf_combo)

            eff_f = effective_frequency(self._parent_cause_likelihood, sg_d['rrf'] or 1)
            badge = RiskBadge()
            badge.update_risk(eff_f, severity)
            self.table.setCellWidget(row, 3, badge)

            del_btn = QPushButton("Ta bort")
            del_btn.clicked.connect(partial(self._delete, sid))
            self.table.setCellWidget(row, 4, del_btn)

        self.table.cellChanged.connect(self._cell_changed)

    def _add(self):
        if self.consequence_id is None:
            return
        self.db.add_safeguard(self.consequence_id)
        self._refresh()
        self.changed.emit()

    def _delete(self, sg_id):
        self.db.delete_safeguard(sg_id)
        self._refresh()
        self.changed.emit()

    def _type_changed(self, sg_id, row, idx):
        sg_type = SG_TYPES[idx] if 0 <= idx < len(SG_TYPES) else 'Övrigt'
        item = self.table.item(row, 0)
        desc = item.text() if item else ''
        rrf_w = self.table.cellWidget(row, 2)
        rrf_idx = rrf_w.currentIndex() if rrf_w else -1
        rrf = RRF_VALUES[rrf_idx] if 0 <= rrf_idx < len(RRF_VALUES) else 1
        self.db.update_safeguard(sg_id, desc, rrf, sg_type)
        self.changed.emit()

    def _rrf_changed(self, sg_id, row, idx):
        rrf = RRF_VALUES[idx] if 0 <= idx < len(RRF_VALUES) else 1
        item = self.table.item(row, 0)
        desc = item.text() if item else ''
        type_w = self.table.cellWidget(row, 1)
        type_idx = type_w.currentIndex() if type_w else -1
        sg_type = SG_TYPES[type_idx] if 0 <= type_idx < len(SG_TYPES) else 'Övrigt'
        self.db.update_safeguard(sg_id, desc, rrf, sg_type)
        self._refresh()
        self.changed.emit()

    def _cell_changed(self, row, col):
        if col != 0:
            return
        item = self.table.item(row, 0)
        if not item:
            return
        sg_id = item.data(Qt.ItemDataRole.UserRole)
        type_w = self.table.cellWidget(row, 1)
        type_idx = type_w.currentIndex() if type_w else -1
        sg_type = SG_TYPES[type_idx] if 0 <= type_idx < len(SG_TYPES) else 'Övrigt'
        rrf_w = self.table.cellWidget(row, 2)
        rrf_idx = rrf_w.currentIndex() if rrf_w else -1
        rrf = RRF_VALUES[rrf_idx] if 0 <= rrf_idx < len(RRF_VALUES) else 1
        self.db.update_safeguard(sg_id, item.text(), rrf, sg_type)


class ActionEditor(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.consequence_id = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        btn = QPushButton("+ Lägg till åtgärd")
        btn.clicked.connect(self._add)
        layout.addWidget(btn)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Åtgärd', 'Ansvarig', 'Datum', 'Status', ''])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i, w in zip([1, 2, 3, 4], [100, 90, 90, 72]):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(i, w)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(CONFIG['H_TABLE_MIN'])
        layout.addWidget(self.table)

    def load(self, consequence_id):
        self.consequence_id = consequence_id
        self._refresh()

    def _refresh(self):
        try:
            self.table.cellChanged.disconnect()
        except Exception:
            pass
        self.table.setRowCount(0)
        if self.consequence_id is None:
            return
        for act in self.db.actions(self.consequence_id):
            row = self.table.rowCount()
            self.table.insertRow(row)
            desc = QTableWidgetItem(act['description'])
            desc.setData(Qt.ItemDataRole.UserRole, act['id'])
            self.table.setItem(row, 0, desc)
            self.table.setItem(row, 1, QTableWidgetItem(act['responsible'] or ''))
            self.table.setItem(row, 2, QTableWidgetItem(act['due_date'] or ''))
            combo = QComboBox()
            combo.addItems(['Öppen', 'Pågår', 'Klar'])
            combo.setCurrentText(act['status'] or 'Öppen')
            aid = act['id']
            combo.currentTextChanged.connect(lambda s, a=aid, r=row: self._save_row(r))
            self.table.setCellWidget(row, 3, combo)
            del_btn = QPushButton("Ta bort")
            del_btn.clicked.connect(partial(self._delete, aid))
            self.table.setCellWidget(row, 4, del_btn)
        self.table.cellChanged.connect(self._cell_changed)

    def _add(self):
        if self.consequence_id is None:
            return
        self.db.add_action(self.consequence_id)
        self._refresh()

    def _delete(self, act_id):
        self.db.delete_action(act_id)
        self._refresh()

    def _cell_changed(self, row, col):
        if col <= 2:
            self._save_row(row)

    def _save_row(self, row):
        item = self.table.item(row, 0)
        if not item:
            return
        act_id = item.data(Qt.ItemDataRole.UserRole)
        desc   = item.text()
        resp   = self.table.item(row, 1).text() if self.table.item(row, 1) else ''
        due    = self.table.item(row, 2).text() if self.table.item(row, 2) else ''
        combo  = self.table.cellWidget(row, 3)
        status = combo.currentText() if combo else 'Öppen'
        self.db.update_action(act_id, desc, resp, due, status)


# ══════════════════════════════════════════════════════════════════════════════
# DETAIL PANELS
# ══════════════════════════════════════════════════════════════════════════════

class WelcomePanel(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl = QLabel("Välj ett objekt i trädet\neller skapa en ny nod.")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = QFont(); f.setPointSize(13)
        lbl.setFont(f)
        lbl.setStyleSheet("color: #888;")
        layout.addWidget(lbl)


class NodePanel(QWidget):
    saved = pyqtSignal(int, str)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.node_id = None
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(10, 10, 10, 10)

        self._title_lbl = QLabel("Nod")
        f = QFont(); f.setPointSize(12); f.setBold(True)
        self._title_lbl.setFont(f)
        layout.addWidget(self._title_lbl)
        sep = QLabel(); sep.setFixedHeight(CONFIG['H_SEP_LINE']); sep.setStyleSheet("background:#ddd;")
        layout.addWidget(sep)

        form = QFormLayout()
        form.setSpacing(5)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("t.ex. Pump P-101")
        self.name_edit.editingFinished.connect(self._save)
        form.addRow("Namn:", self.name_edit)

        self.pid_edit = QLineEdit()
        self.pid_edit.setPlaceholderText("t.ex. P&ID-001")
        self.pid_edit.editingFinished.connect(self._save)
        form.addRow("P&ID-ref:", self.pid_edit)

        self.desc_edit = QTextEdit()
        self.desc_edit.setPlaceholderText("Beskrivning av noden / systemgränsen...")
        self.desc_edit.setFixedHeight(CONFIG['H_DESC_SM'])
        _orig_foe = QTextEdit.focusOutEvent
        _w = self.desc_edit
        _s = self._save
        def _desc_foe(e, _w=_w, _s=_s, _orig=_orig_foe):
            _s()
            _orig(_w, e)
        self.desc_edit.focusOutEvent = _desc_foe
        form.addRow("Beskrivning:", self.desc_edit)

        sep2 = QLabel("Processparametrar")
        f2 = QFont(); f2.setBold(True); f2.setPointSize(9)
        sep2.setFont(f2)
        sep2.setStyleSheet("color:#8D9299; margin-top:2px;")
        form.addRow(sep2)

        self.media_edit = QLineEdit()
        self.media_edit.setPlaceholderText("t.ex. Vätgas (H₂), Vatten, Naturgas, Ammoniak")
        self.media_edit.editingFinished.connect(self._save)
        form.addRow("Media:", self.media_edit)

        self.pressure_edit = QLineEdit()
        self.pressure_edit.setPlaceholderText("t.ex. 10 bar g,  0–25 barg,  1.5 MPa")
        self.pressure_edit.editingFinished.connect(self._save)
        form.addRow("Tryck:", self.pressure_edit)

        self.temperature_edit = QLineEdit()
        self.temperature_edit.setPlaceholderText("t.ex. 150 °C,  −20 till 80 °C")
        self.temperature_edit.editingFinished.connect(self._save)
        form.addRow("Temperatur:", self.temperature_edit)

        layout.addLayout(form)

        # Feature 20: timestamp + responsible
        self._ts_lbl = QLabel('')
        self._ts_lbl.setStyleSheet("color:#999; font-size:9px; margin-top:4px;")
        layout.addWidget(self._ts_lbl)
        layout.addStretch()

    def load(self, node_id):
        self.node_id = node_id
        n = self.db.get_node_number(node_id)
        self._title_lbl.setText(f"Nod {n}" if n else "Nod")
        row = self.db.get_node(node_id)
        if row:
            self._loading = True
            self.name_edit.setText(row['name'])
            self.pid_edit.setText(row['pid_ref'] or '')
            self.desc_edit.setPlainText(row['description'] or '')
            self.media_edit.setText(row['media'] or '')
            self.pressure_edit.setText(row['pressure'] or '')
            self.temperature_edit.setText(row['temperature'] or '')
            at = row['updated_at'] if 'updated_at' in row.keys() else ''
            by = row['updated_by'] if 'updated_by' in row.keys() else ''
            if at:
                self._ts_lbl.setText(f"Senast redigerad: {at}" + (f" av {by}" if by else ""))
            else:
                self._ts_lbl.setText('')
            self._loading = False

    def _save(self):
        if self._loading or self.node_id is None:
            return
        name = self.name_edit.text().strip() or 'Ny nod'
        self.db.update_node(
            self.node_id, name,
            self.desc_edit.toPlainText(),
            self.pid_edit.text(),
            self.media_edit.text(),
            self.pressure_edit.text(),
            self.temperature_edit.text())
        self.saved.emit(self.node_id, name)


class ConsequencePanel(QWidget):
    saved        = pyqtSignal(int)
    place_on_pid = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.consequence_id = None
        self._loading = False
        self._chain = {}
        self._chain_checks = {}
        # Initialise preview label early so _rebuild_preview is always safe
        self._chain_preview = QLabel("—")

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 12, 16, 12)

        title = QLabel("Konsekvens (Consequence)")
        f = QFont(); f.setPointSize(15); f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)
        sep = QLabel(); sep.setFixedHeight(CONFIG['H_SEP_LINE']); sep.setStyleSheet("background:#ddd;")
        layout.addWidget(sep)

        # ── Beskrivning (bas-händelse) ─────────────────────────────────────────
        desc_box = QGroupBox("Händelse / Direkt konsekvens")
        desc_lay = QVBoxLayout(desc_box)
        desc_lay.setSpacing(4)
        self.desc_edit = QTextEdit()
        self.desc_edit.setPlaceholderText(
            "Beskriv den direkta händelsen, t.ex. 'Högt flöde till T-001'")
        self.desc_edit.setFixedHeight(CONFIG['H_DESC_MD'])
        _orig_foe = QTextEdit.focusOutEvent
        _w = self.desc_edit
        _s = self._save
        _rb = self._rebuild_preview
        def _desc_foe(e, _w=_w, _s=_s, _rb=_rb, _orig=_orig_foe):
            _rb()
            _s()
            _orig(_w, e)
        self.desc_edit.focusOutEvent = _desc_foe
        desc_lay.addWidget(self.desc_edit)
        layout.addWidget(desc_box)

        # ── Konsekvenskedja ───────────────────────────────────────────────────
        chain_box = QGroupBox("Konsekvenskedja  (kryssa i för att bygga kedjan)")
        chain_lay = QVBoxLayout(chain_box)
        chain_lay.setSpacing(4)

        last_group = None
        grid = QGridLayout(); grid.setSpacing(3)
        col, row_idx = 0, 0

        for key, label, group in CHAIN_ITEMS:
            if group and group != last_group:
                if col > 0:
                    row_idx += 1; col = 0
                hdr = QLabel(group)
                hdr.setStyleSheet("color:#8D9299; font-weight:bold; font-size:10px; margin-top:4px;")
                grid.addWidget(hdr, row_idx, 0, 1, 3)
                row_idx += 1; col = 0
                last_group = group

            chk = QCheckBox(label)
            chk.toggled.connect(self._on_chain_changed)
            self._chain_checks[key] = chk
            grid.addWidget(chk, row_idx, col)
            col += 1
            if col >= 3:
                col = 0; row_idx += 1

        chain_lay.addLayout(grid)

        # Preview of generated text (widget was pre-created in __init__)
        sep_lbl = QLabel("Genererad text:")
        sep_lbl.setStyleSheet("color:#555; font-size:10px; margin-top:4px;")
        chain_lay.addWidget(sep_lbl)
        self._chain_preview.setStyleSheet(
            "color:#17191C; font-weight:bold; font-size:11px;"
            "background:#F5F5F3; border:1px solid #E2E3E1; border-radius:3px; padding:3px 6px;")
        self._chain_preview.setWordWrap(True)
        chain_lay.addWidget(self._chain_preview)

        apply_btn = QPushButton("↑ Tillämpa genererad text i beskrivningsfältet")
        apply_btn.setStyleSheet(
            "font-size:10px; padding:2px 8px; background:#17191C; color:white;"
            "border:none; border-radius:3px;")
        apply_btn.clicked.connect(self._apply_chain_to_desc)
        chain_lay.addWidget(apply_btn)

        layout.addWidget(chain_box)

        # ── Riskbedömning ─────────────────────────────────────────────────────
        risk_box = QGroupBox("Riskbedömning")
        risk_lay = QFormLayout(risk_box)
        risk_lay.setSpacing(6)

        self.sev_combo = QComboBox()
        self.sev_combo.addItems(SEV_LABELS)
        self.sev_combo.currentIndexChanged.connect(self._risk_changed)
        risk_lay.addRow("Konsekvens (C):", self.sev_combo)

        self.cat_combo = QComboBox()
        self.cat_combo.currentIndexChanged.connect(self._save)
        risk_lay.addRow("Kategori:", self.cat_combo)

        self.risk_badge = RiskBadge()
        risk_lay.addRow("Risknivå:", self.risk_badge)
        layout.addWidget(risk_box)

        # ── Konsekvensdefinitioner (reference) ───────────────────────────────
        self._crit_box = QGroupBox("Konsekvensdefinitioner för vald nivå")
        crit_lay = QVBoxLayout(self._crit_box)
        crit_lay.setSpacing(2)
        self._crit_label = QLabel("—")
        self._crit_label.setWordWrap(True)
        self._crit_label.setStyleSheet(
            "background:#f0f4f8; border:1px solid #ccd; border-radius:4px;"
            "padding:6px 8px; font-size:11px;")
        crit_lay.addWidget(self._crit_label)
        layout.addWidget(self._crit_box)

        # ── Safeguards + Åtgärder ─────────────────────────────────────────────
        sg_box = QGroupBox("Safeguards")
        sg_lay = QVBoxLayout(sg_box)
        self.sg_editor = SafeguardEditor(db)
        sg_lay.addWidget(self.sg_editor)
        layout.addWidget(sg_box)

        act_box = QGroupBox("Åtgärder / Rekommendationer")
        act_lay = QVBoxLayout(act_box)
        self.act_editor = ActionEditor(db)
        act_lay.addWidget(self.act_editor)
        layout.addWidget(act_box)

        layout.addStretch()

    # ── Chain helpers ─────────────────────────────────────────────────────────

    def _rebuild_preview(self):
        if not hasattr(self, '_chain_preview') or self._chain_preview is None:
            return
        base = self.desc_edit.toPlainText().strip() if hasattr(self, 'desc_edit') else ''
        text = build_consequence_text(base, self._chain)
        self._chain_preview.setText(text if text else "—")

    def _on_chain_changed(self):
        if self._loading:
            return
        self._chain = {k: chk.isChecked() for k, chk in self._chain_checks.items()}
        self._rebuild_preview()
        self._save()

    def _apply_chain_to_desc(self):
        """Copy generated text into the description field."""
        text = build_consequence_text(
            self.desc_edit.toPlainText().strip(), self._chain)
        if text:
            self._loading = True
            self.desc_edit.setPlainText(text)
            self._loading = False
        self._save()

    # ── Load / Save ───────────────────────────────────────────────────────────

    def _load_categories(self):
        self.cat_combo.blockSignals(True)
        self.cat_combo.clear()
        self.cat_combo.addItem('')
        for cat in self.db.consequence_categories():
            self.cat_combo.addItem(cat['name'])
        self.cat_combo.blockSignals(False)

    def _load_sev_labels(self):
        self.sev_combo.blockSignals(True)
        cur = self.sev_combo.currentIndex()
        self.sev_combo.clear()
        self.sev_combo.addItems(get_sev_labels())
        self.sev_combo.setCurrentIndex(cur)
        self.sev_combo.blockSignals(False)

    def load(self, consequence_id):
        self.consequence_id = consequence_id
        self._load_sev_labels()
        self._load_categories()
        row = self.db.get_consequence(consequence_id)
        if row:
            self._loading = True
            self.desc_edit.setPlainText(row['description'])
            self.sev_combo.setCurrentIndex(max(0, (row['severity'] or 1) - 1))
            cat = row['category'] or ''
            idx = self.cat_combo.findText(cat)
            self.cat_combo.setCurrentIndex(max(0, idx))
            # Restore chain checkboxes
            self._chain = parse_chain_from_json(
                row['consequence_chain'] if 'consequence_chain' in row.keys() else '')
            for key, chk in self._chain_checks.items():
                chk.setChecked(bool(self._chain.get(key, False)))
            self._loading = False

        self._rebuild_preview()

        cause_id = dict(row)['cause_id'] if row else None
        base_freq_per_year = None
        std_linked = False
        freq = 3
        if cause_id:
            cause = self.db.get_cause(cause_id)
            if cause:
                freq = self.db.cause_f_level(cause)
                base_freq_per_year = self.db.cause_base_frequency_per_year(cause)
                std_linked = bool(dict(cause).get('standard_cause_id') and base_freq_per_year is not None)
        sev = (row['severity'] or 1) if row else 1
        if base_freq_per_year is not None:
            self.risk_badge.update_risk(freq, sev, base_frequency_per_year=base_freq_per_year if std_linked else None)
        else:
            self.risk_badge.set_empty()
        self.sg_editor.load(consequence_id, freq)
        self.act_editor.load(consequence_id)
        self._update_criteria()

    def _risk_changed(self):
        if not self._loading:
            self._save()
        self._update_criteria()

    def _update_criteria(self):
        """Show severity definitions for currently selected severity level."""
        sev_idx = self.sev_combo.currentIndex()
        sev = sev_idx + 1 if sev_idx >= 0 else 1  # 1-based
        defs = self.db.get_severity_definitions()
        lvl_defs = defs.get(sev, {})
        if not lvl_defs:
            self._crit_label.setText("(Inga konsekvensdefinitioner inlagda ännu — se Inställningar → Konsekvenskriterier)")
            return
        cats = {c['id']: c['name'] for c in self.db.consequence_categories()}
        lines = []
        for cat_id, desc in lvl_defs.items():
            if desc:
                lines.append(f"<b>{cats.get(cat_id, '?')}:</b> {desc}")
        self._crit_label.setText("<br>".join(lines) if lines else "—")

    def _save(self):
        if self._loading or self.consequence_id is None:
            return
        sev_idx = self.sev_combo.currentIndex()
        sev   = sev_idx + 1 if sev_idx >= 0 else 1
        desc  = self.desc_edit.toPlainText().strip() or 'Ny konsekvens'
        cat   = self.cat_combo.currentText()
        chain = json.dumps(self._chain)
        self.db.update_consequence(self.consequence_id, desc, sev, cat, chain)
        self.saved.emit(self.consequence_id)


class SafeguardPanel(QWidget):
    saved        = pyqtSignal(int)
    place_on_pid = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.safeguard_id = None
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Safeguard")
        f = QFont(); f.setPointSize(15); f.setBold(True)
        title.setFont(f)
        layout.addWidget(title)
        sep = QLabel(); sep.setFixedHeight(CONFIG['H_SEP_LINE']); sep.setStyleSheet("background:#ddd;")
        layout.addWidget(sep)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.desc_edit = QTextEdit()
        self.desc_edit.setPlaceholderText("Beskriv safeguarden...")
        self.desc_edit.setFixedHeight(CONFIG['H_DESC_LG'])
        _orig_foe = QTextEdit.focusOutEvent
        _w = self.desc_edit
        _s = self._save
        def _desc_foe(e, _w=_w, _s=_s, _orig=_orig_foe):
            _s()
            _orig(_w, e)
        self.desc_edit.focusOutEvent = _desc_foe
        form.addRow("Beskrivning:", self.desc_edit)

        self.type_combo = QComboBox()
        self.type_combo.addItems(SG_TYPES)
        self.type_combo.currentIndexChanged.connect(self._save)
        form.addRow("Typ:", self.type_combo)

        self.rrf_combo = QComboBox()
        self.rrf_combo.addItems(RRF_LABELS)
        self.rrf_combo.currentIndexChanged.connect(self._save)
        form.addRow("RRF:", self.rrf_combo)

        self.risk_badge = RiskBadge()
        form.addRow("Effektiv risk:", self.risk_badge)

        layout.addLayout(form)
        layout.addStretch()

    def load(self, safeguard_id):
        self.safeguard_id = safeguard_id
        sg = self.db.get_safeguard(safeguard_id)
        if not sg:
            return
        self._loading = True
        sg_d = dict(sg)
        self.desc_edit.setPlainText(sg_d['description'])
        sg_type = sg_d.get('sg_type', 'Övrigt') or 'Övrigt'
        self.type_combo.setCurrentIndex(
            SG_TYPES.index(sg_type) if sg_type in SG_TYPES else len(SG_TYPES)-1)
        rrf = sg_d['rrf'] if sg_d['rrf'] in RRF_VALUES else 1
        self.rrf_combo.setCurrentIndex(RRF_VALUES.index(rrf))
        self._update_badge(sg_d)
        self._loading = False

    def _update_badge(self, sg=None):
        if sg is None:
            sg = self.db.get_safeguard(self.safeguard_id)
            if not sg:
                return
        cons = self.db.get_consequence(sg.get('consequence_id') if sg else None)
        if not cons:
            return
        cause = self.db.get_cause(cons.get('cause_id') if cons else None)
        freq = self.db.cause_frequency_level(cause)
        sev = cons.get('severity') or 1 if cons else 1
        rrf_idx = self.rrf_combo.currentIndex()
        rrf = RRF_VALUES[rrf_idx] if 0 <= rrf_idx < len(RRF_VALUES) else 1
        eff_f = effective_frequency(freq, rrf)
        self.risk_badge.update_risk(eff_f, sev)

    def _save(self):
        if self._loading or self.safeguard_id is None:
            return
        desc    = self.desc_edit.toPlainText().strip() or 'Ny safeguard'
        rrf_idx = self.rrf_combo.currentIndex()
        rrf     = RRF_VALUES[rrf_idx] if 0 <= rrf_idx < len(RRF_VALUES) else 1
        type_idx = self.type_combo.currentIndex()
        sg_type = SG_TYPES[type_idx] if 0 <= type_idx < len(SG_TYPES) else 'Övrigt'
        self.db.update_safeguard(self.safeguard_id, desc, rrf, sg_type)
        self._update_badge()
        self.saved.emit(self.safeguard_id)


# ══════════════════════════════════════════════════════════════════════════════
# NODE MARKUP — RIBBON + STYLE POPUP + TABLE PANEL
# ══════════════════════════════════════════════════════════════════════════════

# ── Ribbon icon renderer ──────────────────────────────────────────────────────

def _mk_pm(name: str, sz: int, fg: QColor) -> QPixmap:
    """Render one icon onto a transparent QPixmap using QPainter."""
    pm = QPixmap(sz, sz)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    m  = sz * 0.11                 # margin
    S  = sz - 2 * m               # drawable area side
    sw = max(1.6, sz * 0.078)     # stroke width
    dr = max(2.0, sz * 0.075)     # vertex dot radius

    def pt(fx, fy):
        return QPointF(m + S * fx, m + S * fy)

    pen = QPen(fg, sw, Qt.PenStyle.SolidLine,
               Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    no_pen   = QPen(Qt.PenStyle.NoPen)
    solid_br = QBrush(fg)
    no_br    = QBrush(Qt.BrushStyle.NoBrush)

    if name == 'close':
        p.setPen(QPen(fg, sw * 1.5, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawLine(pt(0.08, 0.08), pt(0.92, 0.92))
        p.drawLine(pt(0.92, 0.08), pt(0.08, 0.92))

    elif name == 'select':
        # Classic cursor arrow: tip at top-left, shaft goes down-right
        path = QPainterPath()
        coords = [
            (0.00, 0.00),   # tip
            (0.00, 0.82),   # left edge base
            (0.26, 0.60),   # inner notch left
            (0.46, 1.00),   # shaft bottom right-inner
            (0.60, 0.93),   # shaft bottom right-outer
            (0.38, 0.54),   # inner notch right
            (0.66, 0.54),   # arrowhead right shoulder
        ]
        first = pt(*coords[0])
        path.moveTo(first)
        for c in coords[1:]:
            path.lineTo(pt(*c))
        path.closeSubpath()
        p.setPen(pen)
        p.setBrush(solid_br)
        p.drawPath(path)

    elif name == 'polygon':
        # Irregular quadrilateral that reads as "polygon" + vertex dots
        verts = [pt(0.10, 0.10), pt(0.90, 0.18),
                 pt(0.82, 0.90), pt(0.12, 0.78)]
        poly = QPolygonF(verts)
        p.setPen(pen)
        p.setBrush(no_br)
        p.drawPolygon(poly)
        p.setPen(no_pen)
        p.setBrush(solid_br)
        for v in verts:
            p.drawEllipse(v, dr, dr)

    elif name == 'polyline':
        # 4-point zigzag with vertex dots
        verts = [pt(0.04, 0.75), pt(0.34, 0.15),
                 pt(0.66, 0.68), pt(0.96, 0.10)]
        p.setPen(pen)
        for i in range(len(verts) - 1):
            p.drawLine(verts[i], verts[i + 1])
        p.setPen(no_pen)
        p.setBrush(solid_br)
        for v in verts:
            p.drawEllipse(v, dr, dr)

    elif name == 'text':
        # Bold "T" — same family as a label/text tool
        font = QFont("Arial", max(10, int(sz * 0.60)))
        font.setBold(True)
        p.setFont(font)
        p.setPen(QPen(fg))
        p.drawText(QRectF(0, 0, sz, sz),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                   "T")

    elif name == 'comment':
        # Speech bubble: rounded rect body + filled triangle tail
        bw, bh = S, S * 0.70
        radius = S * 0.15
        p.setPen(pen)
        p.setBrush(no_br)
        p.drawRoundedRect(QRectF(m, m, bw, bh), radius, radius)
        # Tail
        tail = QPolygonF([
            pt(0.16, 0.68),
            pt(0.36, 0.68),
            pt(0.18, 1.00),
        ])
        p.setPen(no_pen)
        p.setBrush(solid_br)
        p.drawPolygon(tail)
        # Two horizontal text-lines inside the bubble
        lpen = QPen(fg, sw * 0.75, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        p.setPen(lpen)
        p.drawLine(pt(0.22, 0.28), pt(0.82, 0.28))
        p.drawLine(pt(0.22, 0.50), pt(0.70, 0.50))

    elif name == 'eye':
        # Almond outline + solid pupil
        cy_f = 0.50
        path = QPainterPath()
        path.moveTo(pt(0.02, cy_f))
        path.cubicTo(pt(0.25, cy_f - 0.32), pt(0.75, cy_f - 0.32), pt(0.98, cy_f))
        path.cubicTo(pt(0.75, cy_f + 0.32), pt(0.25, cy_f + 0.32), pt(0.02, cy_f))
        p.setPen(pen)
        p.setBrush(no_br)
        p.drawPath(path)
        pr = S * 0.14
        p.setPen(no_pen)
        p.setBrush(solid_br)
        p.drawEllipse(pt(0.50, cy_f), pr, pr)

    elif name == 'smart':
        # Pipe-route icon: horizontal entry, 90° bend, vertical exit, with endpoint dots
        p.setPen(pen)
        p.setBrush(no_br)
        # Horizontal segment bottom-left
        p.drawLine(pt(0.05, 0.75), pt(0.45, 0.75))
        # Bend corner
        p.drawLine(pt(0.45, 0.75), pt(0.45, 0.25))
        # Horizontal segment top-right
        p.drawLine(pt(0.45, 0.25), pt(0.92, 0.25))
        # Start dot (filled circle at left)
        p.setPen(no_pen)
        p.setBrush(solid_br)
        p.drawEllipse(pt(0.05, 0.75), dr, dr)
        # End dot (filled circle at right)
        p.drawEllipse(pt(0.92, 0.25), dr, dr)
        # Small waypoint at corner
        small_r = dr * 0.65
        p.drawEllipse(pt(0.45, 0.75), small_r, small_r)
        p.drawEllipse(pt(0.45, 0.25), small_r, small_r)

    p.end()
    return pm


def _mk_icon(name: str, sz: int = 28) -> QIcon:
    """Return a QIcon with dark pixmap for normal state, white for checked state."""
    icon = QIcon()
    icon.addPixmap(_mk_pm(name, sz, QColor("#2c2c2c")),
                   QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(_mk_pm(name, sz, QColor("#ffffff")),
                   QIcon.Mode.Normal, QIcon.State.On)
    return icon


# ── Style popup ───────────────────────────────────────────────────────────────

class _StylePopup(QWidget):
    """Per-tool flyout popup — appears to the left of the clicked tool button."""

    _TOOL_NAMES = {
        'polygon':  'Rita polygon',
        'polyline': 'Rita polylinje',
        'text':     'Lägg ut nodnamn',
        'comment':  'Lägg till kommentar',
        'smart':    'Smart polylinje',
    }

    def __init__(self, ribbon, parent=None):
        super().__init__(parent,
                         Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet(
            "QWidget{background:#fff;border-radius:4px;}"
            "QLabel{font-size:10px;color:#444;border:none;}")
        self._ribbon = ribbon

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        outer.setSpacing(6)

        # Title
        self._title_lbl = QLabel()
        f = QFont(); f.setBold(True); f.setPointSize(10)
        self._title_lbl.setFont(f)
        outer.addWidget(self._title_lbl)

        sep = QLabel(); sep.setFixedHeight(CONFIG['H_SEP_LINE'])
        sep.setStyleSheet("background:#ddd;border:none;")
        outer.addWidget(sep)

        # Colour swatches (always shown)
        color_widget = QWidget()
        crow = QHBoxLayout(color_widget)
        crow.setContentsMargins(0, 0, 0, 0); crow.setSpacing(3)
        crow.addWidget(QLabel("Färg:"))
        self._cbts = []
        for hc in MARKUP_COLORS:
            cb = QPushButton(); cb.setFixedSize(22, 22)
            cb.setStyleSheet(f"background:{hc};border:2px solid transparent;"
                             f"border-radius:3px;")
            cb.clicked.connect(partial(self._pick, hc))
            crow.addWidget(cb); self._cbts.append((hc, cb))
        pal = QPushButton("···"); pal.setFixedSize(28, 22)
        pal.setStyleSheet("font-size:10px;border:1px solid #ccc;border-radius:3px;")
        pal.clicked.connect(self._open_palette)
        crow.addWidget(pal); crow.addStretch()
        outer.addWidget(color_widget)

        self._bar = QLabel(); self._bar.setFixedHeight(CONFIG['H_COLOR_STRIP'])
        self._bar.setStyleSheet("border:none;")
        outer.addWidget(self._bar)

        sep2 = QLabel(); sep2.setFixedHeight(CONFIG['H_SEP_LINE'])
        sep2.setStyleSheet("background:#eee;border:none;")
        outer.addWidget(sep2)

        # Opacity row (polygon, polyline, comment)
        self._opacity_row = QWidget()
        orow = QHBoxLayout(self._opacity_row)
        orow.setContentsMargins(0, 0, 0, 0)
        orow.addWidget(QLabel("Opacitet:"))
        self._op_sl = QSlider(Qt.Orientation.Horizontal)
        self._op_sl.setRange(10, 90)
        orow.addWidget(self._op_sl)
        self._op_lbl = QLabel(); self._op_lbl.setFixedWidth(CONFIG['W_OPACITY_LBL'])
        orow.addWidget(self._op_lbl)
        self._op_sl.valueChanged.connect(
            lambda v: (ribbon._apply_opacity(v), self._op_lbl.setText(f"{v}%")))
        outer.addWidget(self._opacity_row)

        # Line width row (polygon, polyline)
        self._width_row = QWidget()
        wrow = QHBoxLayout(self._width_row)
        wrow.setContentsMargins(0, 0, 0, 0); wrow.setSpacing(5)
        wrow.addWidget(QLabel("Tjocklek:"))
        self._w_sp = QSpinBox(); self._w_sp.setRange(1, 99)
        self._w_sp.setMaximumWidth(58)
        self._w_sp.valueChanged.connect(ribbon._apply_width)
        wrow.addWidget(self._w_sp); wrow.addStretch()
        outer.addWidget(self._width_row)

        # Font size row (text, comment)
        self._font_row = QWidget()
        frow = QHBoxLayout(self._font_row)
        frow.setContentsMargins(0, 0, 0, 0); frow.setSpacing(5)
        frow.addWidget(QLabel("Textstorlek:"))
        self._f_sp = QSpinBox(); self._f_sp.setRange(6, 99)
        self._f_sp.setMaximumWidth(58)
        self._f_sp.valueChanged.connect(ribbon._apply_font)
        frow.addWidget(self._f_sp); frow.addStretch()
        outer.addWidget(self._font_row)

        # Snap row (polygon, polyline)
        self._snap_row = QWidget()
        srow = QHBoxLayout(self._snap_row)
        srow.setContentsMargins(0, 0, 0, 0)
        self._snap_cb = QCheckBox("Snap till befintliga punkter")
        self._snap_cb.setChecked(True)
        self._snap_cb.toggled.connect(ribbon._apply_snap)
        srow.addWidget(self._snap_cb); srow.addStretch()
        outer.addWidget(self._snap_row)

        self.setMinimumWidth(CONFIG['W_DIALOG_MD'])

    def _configure_for(self, tool):
        self._title_lbl.setText(self._TOOL_NAMES.get(tool, tool))
        self._opacity_row.setVisible(tool in ('polygon', 'polyline', 'comment', 'smart'))
        self._width_row.setVisible(tool in ('polygon', 'polyline', 'smart'))
        self._font_row.setVisible(tool in ('text', 'comment'))
        self._snap_row.setVisible(tool in ('polygon', 'polyline'))

    def show_for(self, tool, btn):
        self._configure_for(tool)
        self._sync()
        self.adjustSize()
        # Position to the left of the tool button
        gp = btn.mapToGlobal(btn.rect().topLeft())
        self.move(gp.x() - self.width() - 4, gp.y())
        self.show()

    def _sync(self):
        r = self._ribbon
        self._bar.setStyleSheet(f"background:{r._color};border-radius:2px;border:none;")
        self._op_sl.blockSignals(True); self._op_sl.setValue(int(r._opacity * 100))
        self._op_sl.blockSignals(False)
        self._op_lbl.setText(f"{int(r._opacity * 100)}%")
        self._w_sp.blockSignals(True); self._w_sp.setValue(r._width)
        self._w_sp.blockSignals(False)
        self._f_sp.blockSignals(True); self._f_sp.setValue(r._font_size)
        self._f_sp.blockSignals(False)
        self._snap_cb.blockSignals(True); self._snap_cb.setChecked(r._snap)
        self._snap_cb.blockSignals(False)
        for hc, cb in self._cbts:
            cb.setStyleSheet(
                f"background:{hc};border:2px solid "
                f"{'#333' if hc == r._color else 'transparent'};border-radius:3px;")

    def _pick(self, hex_c):
        self._ribbon._apply_color(hex_c)
        self._bar.setStyleSheet(f"background:{hex_c};border-radius:2px;border:none;")
        for hc, cb in self._cbts:
            cb.setStyleSheet(
                f"background:{hc};border:2px solid "
                f"{'#333' if hc == hex_c else 'transparent'};border-radius:3px;")

    def _open_palette(self):
        self.hide()
        c = QColorDialog.getColor(QColor(self._ribbon._color), None, "Välj färg")
        if c.isValid():
            self._ribbon._apply_color(c.name())

    def showEvent(self, event):
        self._sync()
        super().showEvent(event)


class PropertiesRibbon(QWidget):
    """Narrow (62 px) vertical ribbon replacing the right detail panel.

    Shows icon buttons for each editable field of the selected item.
    Each button opens a small floating popup for editing that field.
    Style mirrors NodeMarkupPanel.
    """
    item_changed = pyqtSignal()   # emitted after any field is saved

    _BTN_SZ  = 50
    _WIDTH   = 62
    _BTN_SS  = (
        "QPushButton{border:1px solid #E2E3E1;border-radius:5px;"
        "background:#FFFFFF;padding:0px;font-size:15px;}"
        "QPushButton:hover{background:#F5F5F3;border-color:#CFD1CE;}"
        "QPushButton:pressed{background:#E8E9E6;}"
    )
    _GRP_SS  = "font-size:8px;color:#888;margin:0px;padding:0px;"
    # Shared style for the OK button inside floating popups
    _OK_BTN_SS = ("background:#17191C;color:white;border:none;"
                  "border-radius:4px;padding:4px 16px;")

    def __init__(self, db, main_window=None, parent=None):
        super().__init__(parent)
        self.db          = db
        self._mw         = main_window
        self._type       = None
        self._id         = None
        self._btns       = []

        self.setFixedWidth(self._WIDTH)
        self.setStyleSheet("background:#F0F2F5;")
        self._outer = QVBoxLayout(self)
        self._outer.setContentsMargins(6, 8, 6, 8)
        self._outer.setSpacing(3)
        self._outer.addStretch()

    # ── Public API ────────────────────────────────────────────────────────────
    def set_item(self, type_: int, id_: int):
        old_type = self._type
        self._type = type_
        self._id   = id_
        if type_ != old_type:   # skip widget churn when type is unchanged
            self._rebuild()

    def clear(self):
        self._type = None
        self._id   = None
        self._rebuild()

    # ── Internal ──────────────────────────────────────────────────────────────
    def _rebuild(self):
        # Single-pass teardown: drain the layout, deleting widgets only
        while self._outer.count():
            item = self._outer.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._btns.clear()

        buttons = self._buttons_for_type()
        for spec in buttons:
            if spec is None:
                sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
                sep.setStyleSheet("background:#E8E8E8;max-height:1px;border:none;")
                sep.setFixedHeight(CONFIG['H_SEP_LINE'])
                self._outer.addWidget(sep)
                self._btns.append(sep)
            elif isinstance(spec, str):
                lbl = QLabel(spec); lbl.setStyleSheet(self._GRP_SS)
                lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._outer.addWidget(lbl)
                self._btns.append(lbl)
            else:
                emoji, tip, slot = spec
                btn = QPushButton(emoji)
                btn.setFixedSize(self._BTN_SZ, self._BTN_SZ)
                btn.setToolTip(tip)
                btn.setStyleSheet(self._BTN_SS)
                btn.clicked.connect(lambda _, s=slot, b=btn: s(b))
                self._outer.addWidget(btn)
                self._btns.append(btn)
        self._outer.addStretch()

    def _buttons_for_type(self) -> list:
        # Returns bound-method references (self.method) so the lambda in
        # _rebuild fires correctly.  A class-level dict with cls.method
        # (unbound) was tried but broke: s(btn) passed btn as self.
        T = self._type
        if T == 1:   # NODE_T
            return [
                "NOD",
                ("🏷", "Redigera namn och P&ID-referens",    self._edit_node_name),
                ("📄", "Redigera beskrivning",                self._edit_node_desc),
                ("⚗", "Redigera processparametrar\n(media, tryck, temperatur)",
                                                              self._edit_node_params),
                None,
                ("✅", "Sätt status / godkänn nod",          self._edit_node_status),
                ("📍", "Visa nod på P&ID",                   self._zoom_to_node),
            ]
        if T == 5:   # DEV_T
            return [
                "AVVIK.",
                ("📝", "Redigera avvikelsebeskrivning",       self._edit_dev_desc),
            ]
        if T == 2:   # CAUSE_T
            return [
                "ORSAK",
                ("📝", "Redigera orsak (beskrivning, objekt, tag)", self._edit_cause_obj),
                ("📊", "Ange frekvens / F-nivå",              self._edit_cause_freq),
                ("💬", "Redigera kommentar",                  self._edit_cause_comment),
                None,
                ("📍", "Visa orsak på P&ID",                 self._zoom_to_cause),
            ]
        if T == 3:   # CONS_T
            return [
                "KONS.",
                ("📋", "Redigera konsekvenskedja (Del1–Del5)", self._edit_cons_chain),
                ("📊", "Sätt allvarlighet per kategori",      self._edit_cons_sev),
                None,
                ("📍", "Visa konsekvens på P&ID",            self._zoom_to_cons),
            ]
        if T == 4:   # SG_T
            return [
                "BARRIÄR",
                ("📝", "Redigera barriärsbeskrivning",        self._edit_sg_desc),
                ("⚡", "Ange RRF och typ",                    self._edit_sg_rrf),
                None,
                ("📍", "Visa barriär på P&ID",               self._zoom_to_sg),
            ]
        return []

    # ── Popup helper ──────────────────────────────────────────────────────────
    def _popup_near(self, btn):
        """Return global position to anchor a popup to the left of the ribbon."""
        gp = btn.mapToGlobal(btn.rect().topLeft())
        scr = (QApplication.screenAt(gp) or QApplication.primaryScreen()).availableGeometry()
        return gp, scr

    def _show_popup(self, btn, popup):
        popup.adjustSize()
        gp, scr = self._popup_near(btn)
        pw, ph  = popup.sizeHint().width(), popup.sizeHint().height()
        x = gp.x() - pw - 6
        y = gp.y()
        if x < scr.left(): x = gp.x() + self._WIDTH + 6
        if y + ph > scr.bottom(): y = scr.bottom() - ph
        popup.move(max(scr.left(), x), max(scr.top(), y))
        return popup.exec()

    def _text_popup(self, btn, title: str, current: str,
                    multiline: bool = False, placeholder: str = ''):
        """Generic text-editing popup. Returns new text or None on cancel."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dlg.setMinimumWidth(CONFIG['W_DIALOG_LG'])
        lay = QVBoxLayout(dlg)
        lay.setSpacing(6); lay.setContentsMargins(10, 10, 10, 10)
        hdr = QLabel(f"<b>{title}</b>")
        hdr.setStyleSheet("color:#8D9299;")
        lay.addWidget(hdr)
        if multiline:
            ed = QTextEdit(); ed.setPlainText(current)
            ed.setPlaceholderText(placeholder)
            ed.setFixedHeight(CONFIG['H_EDIT_LG'])
        else:
            ed = QLineEdit(current)
            ed.setPlaceholderText(placeholder)
        lay.addWidget(ed)
        row = QHBoxLayout()
        ok = QPushButton("OK"); ok.setDefault(True)
        ok.setStyleSheet(self._OK_BTN_SS)
        ok.clicked.connect(dlg.accept)
        cancel = QPushButton("Avbryt"); cancel.clicked.connect(dlg.reject)
        row.addStretch(); row.addWidget(cancel); row.addWidget(ok)
        lay.addLayout(row)
        if isinstance(ed, QLineEdit):
            ed.returnPressed.connect(dlg.accept)
        if self._show_popup(btn, dlg) == QDialog.DialogCode.Accepted:
            return (ed.toPlainText() if multiline else ed.text()).strip()
        return None

    # ── NODE actions ──────────────────────────────────────────────────────────
    def _edit_node_name(self, btn):
        if not self._id: return
        n = self.db.get_node(self._id)
        if not n: return
        n = dict(n)
        dlg = QDialog(self)
        dlg.setWindowTitle("Nod")
        dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dlg.setMinimumWidth(CONFIG['W_DIALOG_MD'])
        lay = QFormLayout(dlg); lay.setContentsMargins(10,10,10,10)
        name_e = QLineEdit(n['name'] or '')
        pid_e  = QLineEdit(n.get('pid_ref') or '')
        lay.addRow("<b>Namn:</b>", name_e)
        lay.addRow("P&ID-ref:", pid_e)
        row = QHBoxLayout()
        ok = QPushButton("OK"); ok.setDefault(True)
        ok.setStyleSheet(self._OK_BTN_SS)
        ok.clicked.connect(dlg.accept)
        cancel = QPushButton("Avbryt"); cancel.clicked.connect(dlg.reject)
        row.addStretch(); row.addWidget(cancel); row.addWidget(ok)
        lay.addRow(row)
        name_e.returnPressed.connect(dlg.accept)
        if self._show_popup(btn, dlg) == QDialog.DialogCode.Accepted:
            name = name_e.text().strip() or 'Ny nod'
            self.db.update_node(self._id, name, n.get('description',''),
                                pid_e.text().strip(),
                                n.get('media',''), n.get('pressure',''),
                                n.get('temperature',''))
            self.item_changed.emit()

    def _edit_node_desc(self, btn):
        if not self._id: return
        n = self.db.get_node(self._id)
        if not n: return
        n = dict(n)
        val = self._text_popup(btn, "Beskrivning", n.get('description','') or '',
                               multiline=True, placeholder="Beskriv noden...")
        if val is not None:
            self.db.update_node(self._id, n['name'], val,
                                n.get('pid_ref',''), n.get('media',''),
                                n.get('pressure',''), n.get('temperature',''))
            self.item_changed.emit()

    def _edit_node_params(self, btn):
        if not self._id: return
        n = self.db.get_node(self._id)
        if not n: return
        n = dict(n)
        dlg = QDialog(self)
        dlg.setWindowTitle("Processparametrar")
        dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dlg.setMinimumWidth(CONFIG['W_DIALOG_MD'])
        lay = QFormLayout(dlg); lay.setContentsMargins(10,10,10,10)
        me = QLineEdit(n.get('media','') or '')
        pe = QLineEdit(n.get('pressure','') or '')
        te = QLineEdit(n.get('temperature','') or '')
        me.setPlaceholderText("t.ex. Vätgas, Vatten")
        pe.setPlaceholderText("t.ex. 10 bar g")
        te.setPlaceholderText("t.ex. 150 °C")
        lay.addRow("Media:", me)
        lay.addRow("Tryck:", pe)
        lay.addRow("Temperatur:", te)
        row = QHBoxLayout()
        ok = QPushButton("OK"); ok.setDefault(True)
        ok.setStyleSheet(self._OK_BTN_SS)
        ok.clicked.connect(dlg.accept); cancel = QPushButton("Avbryt")
        cancel.clicked.connect(dlg.reject)
        row.addStretch(); row.addWidget(cancel); row.addWidget(ok)
        lay.addRow(row)
        if self._show_popup(btn, dlg) == QDialog.DialogCode.Accepted:
            self.db.update_node(self._id, n['name'], n.get('description',''),
                                n.get('pid_ref',''),
                                me.text().strip(), pe.text().strip(), te.text().strip())
            self.item_changed.emit()

    def _edit_node_status(self, btn):
        if not self._mw or not self._id: return
        self._mw._approve_node(node_id=self._id)

    def _zoom_to_node(self, btn):
        if not self._mw or not self._id: return
        self._mw._switch_view(0)
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(CONFIG['TIMER_NAV_QUICK_MS'],
                         partial(self._mw.zoom_to_node, self._id))

    # ── DEVIATION actions ─────────────────────────────────────────────────────
    def _edit_dev_desc(self, btn):
        if not self._id: return
        d = self.db.get_deviation(self._id)
        if not d: return
        d = dict(d)
        val = self._text_popup(btn, "Avvikelse", d['description'] or '')
        if val is not None:
            self.db.conn.execute(
                "UPDATE deviations SET description=? WHERE id=?", (val, self._id))
            self.db.commit()
            self.item_changed.emit()

    # ── CAUSE actions ─────────────────────────────────────────────────────────
    def _edit_cause_obj(self, btn):
        """Open the combined CauseObjectPopup for editing description,
        comp_type and comp_tag together — replaces the old split of a
        separate free-text description popup and a separate object/tag
        popup, so cause editing is consistent with every other entry point.
        """
        if not self._id or not self._mw: return
        c = dict(self.db.get_cause(self._id) or {})
        dev = self.db.get_deviation(c.get('deviation_id')) if c.get('deviation_id') else None
        popup = CauseObjectPopup(
            c.get('comp_type',''), c.get('comp_tag',''),
            self.db, dev_description=dev['description'] if dev else None,
            current_description=c.get('description',''), parent=self)
        popup.setWindowFlags(popup.windowFlags() | Qt.WindowType.FramelessWindowHint)
        gp, scr = self._popup_near(btn)
        popup.adjustSize()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = gp.x() - pw - 6
        y = gp.y()
        if x < scr.left(): x = gp.x() + self._WIDTH + 6
        if y + ph > scr.bottom(): y = scr.bottom() - ph
        popup.move(max(scr.left(), x), max(scr.top(), y))
        def _on_committed(ct, tag, desc, freq):
            self.db.update_cause(self._id, comp_type=ct, comp_tag=tag)
            if desc is not None: self.db.update_cause(self._id, description=desc)
            if freq is not None: self.db.update_cause(self._id, base_frequency=freq)
            self.item_changed.emit()
        popup.committed.connect(_on_committed)
        popup.exec()

    def _edit_cause_freq(self, btn):
        if not self._id: return
        c = dict(self.db.get_cause(self._id) or {})
        current_freq = c.get('base_frequency')
        dlg = QDialog(self)
        dlg.setWindowTitle("Frekvens")
        dlg.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        dlg.setMinimumWidth(CONFIG['W_DIALOG_MIN'])
        lay = QFormLayout(dlg); lay.setContentsMargins(10,10,10,10)
        freq_e = QLineEdit(f"{current_freq:g}" if current_freq else '')
        freq_e.setPlaceholderText("t.ex. 0.01")
        level_lbl = QLabel('')
        level_lbl.setStyleSheet("color:#8D9299;font-size:10px;")
        def _upd(txt):
            try: level_lbl.setText(freq_axis_label(freq_to_f_level(float(txt))))
            except: level_lbl.setText('')
        freq_e.textChanged.connect(_upd)
        _upd(freq_e.text())
        lay.addRow("Frekvens (/år):", freq_e)
        lay.addRow("F-nivå:", level_lbl)
        row = QHBoxLayout()
        ok = QPushButton("OK"); ok.setDefault(True)
        ok.setStyleSheet(self._OK_BTN_SS)
        ok.clicked.connect(dlg.accept)
        cancel = QPushButton("Avbryt"); cancel.clicked.connect(dlg.reject)
        row.addStretch(); row.addWidget(cancel); row.addWidget(ok)
        lay.addRow(row)
        freq_e.returnPressed.connect(dlg.accept)
        if self._show_popup(btn, dlg) == QDialog.DialogCode.Accepted:
            try:
                freq = float(freq_e.text().strip()) if freq_e.text().strip() else None
            except ValueError:
                freq = None
            self.db.update_cause(self._id, base_frequency=freq)
            self.item_changed.emit()

    def _edit_cause_comment(self, btn):
        if not self._id: return
        current = self.db.get_cause_comment(self._id) or ''
        val = self._text_popup(btn, "Kommentar", current,
                               multiline=True, placeholder="Notering, beslut, referens...")
        if val is not None:
            self.db.set_cause_comment(self._id, val)
            self.item_changed.emit()

    def _zoom_to_cause(self, btn):
        if not self._id or not self._mw: return
        self._mw._switch_view(0)
        markers = self.db.cause_markers_for_cause(self._id)
        if markers:
            m = markers[0]
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(CONFIG['TIMER_NAV_QUICK_MS'],
                             partial(self._mw.pid_panel.navigate_to_marker,
                                    m['pid_page'], m['x'], m['y']))

    # ── CONSEQUENCE actions ───────────────────────────────────────────────────
    def _edit_cons_chain(self, btn):
        if not self._id or not self._mw: return
        self._mw._open_consequence_step_picker(self._id)

    def _edit_cons_sev(self, btn):
        if not self._id or not self._mw: return
        popup = ConsCategoryMatrixPopup(self.db, self._id, self)
        if self._show_popup(btn, popup) == QDialog.DialogCode.Accepted:
            self.item_changed.emit()

    def _zoom_to_cons(self, btn):
        if not self._id or not self._mw: return
        self._mw._switch_view(0)
        rows = self.db.conn.execute(
            "SELECT pid_page,x,y FROM consequence_markers WHERE consequence_id=? LIMIT 1",
            (self._id,)).fetchone()
        if rows:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(CONFIG['TIMER_NAV_QUICK_MS'],
                             partial(self._mw.pid_panel.navigate_to_marker,
                                    rows[0], rows[1], rows[2]))

    # ── SAFEGUARD actions ─────────────────────────────────────────────────────
    def _edit_sg_desc(self, btn):
        if not self._id: return
        sg = self.db.get_safeguard(self._id)
        if not sg: return
        sg = dict(sg)
        val = self._text_popup(btn, "Barriär", dict(sg).get('description','') or '',
                               multiline=True, placeholder="Beskriv barriären...")
        if val is not None:
            self.db.update_safeguard(self._id, description=val)
            self.item_changed.emit()

    def _edit_sg_rrf(self, btn):
        if not self._id or not self._mw: return
        sg = self.db.get_safeguard(self._id)
        if not sg: return
        sgd = dict(sg)
        sg_id = self._id   # capture by value for the signal lambda
        popup = RRFPopup(int(sgd.get('rrf', 1)), sgd.get('sg_type', 'Övrigt'), self)
        popup.rrf_selected.connect(
            lambda v, t, sid=sg_id: (self.db.update_safeguard(sid, rrf=v, sg_type=t),
                                     self.item_changed.emit()))
        self._show_popup(btn, popup)

    def _zoom_to_sg(self, btn):
        if not self._id or not self._mw: return
        self._mw._switch_view(0)
        rows = self.db.conn.execute(
            "SELECT pid_page,x,y FROM safeguard_markers WHERE safeguard_id=? LIMIT 1",
            (self._id,)).fetchone()
        if rows:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(CONFIG['TIMER_NAV_QUICK_MS'],
                             partial(self._mw.pid_panel.navigate_to_marker,
                                    rows[0], rows[1], rows[2]))


class NodeMarkupPanel(QWidget):
    """Narrow vertical ribbon for node markup tool selection."""
    closed          = pyqtSignal()
    tool_changed    = pyqtSignal(str)
    all_vis_toggled = pyqtSignal(bool)
    style_changed   = pyqtSignal(str, float, int)   # color, opacity, line_width
    snap_changed    = pyqtSignal(bool)
    navigate_node_requested = pyqtSignal(int)  # node_id

    _TOOLS = [
        ('select',   'select',   'Välj/flytta'),
        ('polygon',  'polygon',  'Rita polygon'),
        ('polyline', 'polyline', 'Rita polylinje'),
        ('smart',    'smart',    'Smart polylinje'),
        ('text',     'text',     'Lägg ut nodnamn'),
        ('comment',  'comment',  'Lägg till kommentar'),
    ]

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db            = db
        self.node_id       = None
        self._color        = MARKUP_COLORS[5]
        self._opacity      = 0.45
        self._width        = 12
        self._font_size    = 24
        self._snap         = True
        self._current_tool = 'select'
        self._popup        = None

        SZ = 48
        ISZ = 28   # icon size within button
        self.setFixedWidth(CONFIG['W_SPINNER'])
        self.setStyleSheet("background:#FFFFFF; border-right: 1px solid #E8E8E8;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 6, 5, 6)
        outer.setSpacing(3)

        _btn_ss = (
            "QPushButton{border:1px solid #E2E3E1;border-radius:5px;"
            "background:#FFFFFF;padding:0px;}"
            "QPushButton:checked{background:#17191C;border-color:#17191C;}"
            "QPushButton:hover:!checked{background:#F5F5F3;border-color:#CFD1CE;}")

        # ── Navigation row ────────────────────────────────────────────────────
        nav_lay = QHBoxLayout()
        nav_lay.setContentsMargins(0, 0, 0, 0)
        nav_lay.setSpacing(2)

        self._prev_btn = QPushButton()
        self._prev_btn.setFixedSize(24, SZ)
        self._prev_btn.setToolTip("Föregående nod (⬆)")
        self._prev_btn.setIcon(_mk_icon('arrow_up', 16))
        self._prev_btn.setIconSize(QSize(16, 16))
        self._prev_btn.setStyleSheet(_btn_ss)
        self._prev_btn.clicked.connect(self._navigate_prev)
        nav_lay.addWidget(self._prev_btn)

        self._next_btn = QPushButton()
        self._next_btn.setFixedSize(24, SZ)
        self._next_btn.setToolTip("Nästa nod (⬇)")
        self._next_btn.setIcon(_mk_icon('arrow_down', 16))
        self._next_btn.setIconSize(QSize(16, 16))
        self._next_btn.setStyleSheet(_btn_ss)
        self._next_btn.clicked.connect(self._navigate_next)
        nav_lay.addWidget(self._next_btn)

        outer.addLayout(nav_lay)

        # ── Close button ──────────────────────────────────────────────────────
        close_btn = QPushButton()
        close_btn.setFixedSize(SZ, SZ)
        close_btn.setToolTip("Avsluta redigering")
        close_icon = QIcon()
        close_icon.addPixmap(_mk_pm('close', ISZ, QColor("#ffffff")))
        close_btn.setIcon(close_icon)
        close_btn.setIconSize(QSize(ISZ, ISZ))
        close_btn.setStyleSheet(
            "QPushButton{background:#546E7A;border:none;border-radius:5px;padding:0px;}"
            "QPushButton:hover{background:#37474F;}")
        close_btn.clicked.connect(self.closed.emit)
        outer.addWidget(close_btn)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet("background:#E8E8E8;max-height:1px;border:none;")
        outer.addWidget(sep1)

        # ── Tool buttons — each click selects tool AND opens per-tool popup ───
        self._tool_btns = {}
        for tool, icon_name, tip in self._TOOLS:
            btn = QPushButton()
            btn.setFixedSize(SZ, SZ)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setIcon(_mk_icon(icon_name, ISZ))
            btn.setIconSize(QSize(ISZ, ISZ))
            btn.setStyleSheet(_btn_ss)
            btn.clicked.connect(lambda _, t=tool, b=btn: self._on_tool(t, b))
            outer.addWidget(btn)
            self._tool_btns[tool] = btn

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("background:#E8E8E8;max-height:1px;border:none;")
        outer.addWidget(sep2)

        # ── Color strip ───────────────────────────────────────────────────────
        self._color_strip = QLabel()
        self._color_strip.setFixedHeight(CONFIG['H_COLOR_STRIP'])
        self._color_strip.setStyleSheet(
            f"background:{self._color};border-radius:3px;border:none;")
        outer.addWidget(self._color_strip)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setStyleSheet("background:#E8E8E8;max-height:1px;border:none;")
        outer.addWidget(sep3)

        # ── Visibility toggle ─────────────────────────────────────────────────
        self._all_vis_btn = QPushButton()
        self._all_vis_btn.setFixedSize(SZ, SZ)
        self._all_vis_btn.setCheckable(True)
        self._all_vis_btn.setChecked(True)
        self._all_vis_btn.setToolTip("Dölj/visa alla markeringar")
        eye_icon = QIcon()
        eye_icon.addPixmap(_mk_pm('eye', ISZ, QColor("#ffffff")),
                           QIcon.Mode.Normal, QIcon.State.Off)
        eye_icon.addPixmap(_mk_pm('eye', ISZ, QColor("#ffffff")),
                           QIcon.Mode.Normal, QIcon.State.On)
        self._all_vis_btn.setIcon(eye_icon)
        self._all_vis_btn.setIconSize(QSize(ISZ, ISZ))
        self._all_vis_btn.setStyleSheet(
            "QPushButton{border:none;border-radius:5px;padding:0px;"
            "background:#27AE60;}"
            "QPushButton:!checked{background:#E74C3C;}")
        self._all_vis_btn.clicked.connect(self._on_all_vis)
        outer.addWidget(self._all_vis_btn)

        outer.addStretch()
        self._on_tool('select')

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, node_id):
        self.node_id = node_id
        self._all_vis_btn.setChecked(True)
        self._on_tool('select')

    def refresh(self):
        pass

    def on_markup_saved(self, mu_id):
        pass

    def select_markup(self, mu_id):
        pass

    def get_current_style(self):
        return self._color, self._opacity, self._width, self._font_size

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_tool(self, tool, btn=None):
        self._current_tool = tool
        for t, b in self._tool_btns.items():
            b.setChecked(t == tool)
        self.tool_changed.emit(tool)
        # Open per-tool popup for all drawing tools
        if tool != 'select' and btn is not None:
            self._show_tool_popup(tool, btn)

    def _show_tool_popup(self, tool, btn):
        if self._popup is None:
            self._popup = _StylePopup(self)
        self._popup.show_for(tool, btn)

    def _on_all_vis(self, checked):
        if self.node_id is None:
            return
        self.db.set_all_node_markups_visible(self.node_id, checked)
        self.all_vis_toggled.emit(checked)

    def _apply_color(self, hex_c):
        self._color = hex_c
        self._color_strip.setStyleSheet(f"background:{hex_c};border-radius:3px;")
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_opacity(self, val):
        self._opacity = val / 100.0
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_width(self, val):
        self._width = val
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_font(self, val):
        self._font_size = val

    def _apply_snap(self, enabled):
        self._snap = enabled
        self.snap_changed.emit(enabled)

    def _navigate_prev(self):
        """Jump to previous node."""
        if self.node_id is None:
            return
        all_nodes = [r[0] for r in self.db.nodes]
        try:
            current_idx = all_nodes.index(self.node_id)
            if current_idx > 0:
                next_node_id = all_nodes[current_idx - 1]
                self.navigate_node_requested.emit(next_node_id)
        except (ValueError, IndexError):
            pass

    def _navigate_next(self):
        """Jump to next node."""
        if self.node_id is None:
            return
        all_nodes = [r[0] for r in self.db.nodes]
        try:
            current_idx = all_nodes.index(self.node_id)
            if current_idx < len(all_nodes) - 1:
                next_node_id = all_nodes[current_idx + 1]
                self.navigate_node_requested.emit(next_node_id)
        except (ValueError, IndexError):
            pass


class _MarkupStyleDialog(QDialog):
    def __init__(self, mu_type, color, opacity, line_width, font_size, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ändra stil")
        self.setFixedWidth(CONFIG['W_DIALOG_LG'])
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        # Color row
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Färg:"))
        self._color = color
        self._color_btns = []
        for hc in MARKUP_COLORS:
            btn = QPushButton()
            btn.setFixedSize(22, 22)
            sel = hc.lower() == color.lower()
            btn.setStyleSheet(
                f"background:{hc};border:2px solid {'#222' if sel else 'transparent'};"
                f"border-radius:3px;")
            btn.clicked.connect(partial(self._pick, hc))
            color_row.addWidget(btn)
            self._color_btns.append((hc, btn))
        color_row.addStretch()
        outer.addLayout(color_row)

        # Opacity
        self._opacity_row = QWidget()
        op_lay = QHBoxLayout(self._opacity_row)
        op_lay.setContentsMargins(0, 0, 0, 0)
        op_lay.addWidget(QLabel("Opacitet:"))
        self._opacity_sl = QSlider(Qt.Orientation.Horizontal)
        self._opacity_sl.setRange(10, 100)
        self._opacity_sl.setValue(int(opacity * 100))
        op_lay.addWidget(self._opacity_sl)
        self._opacity_row.setVisible(mu_type in ('polygon', 'polyline', 'comment'))
        outer.addWidget(self._opacity_row)

        # Line width
        self._width_row = QWidget()
        w_lay = QHBoxLayout(self._width_row)
        w_lay.setContentsMargins(0, 0, 0, 0)
        w_lay.addWidget(QLabel("Tjocklek:"))
        self._width_sp = QSpinBox()
        self._width_sp.setRange(1, 20)
        self._width_sp.setValue(int(line_width))
        w_lay.addWidget(self._width_sp)
        w_lay.addStretch()
        self._width_row.setVisible(mu_type in ('polygon', 'polyline'))
        outer.addWidget(self._width_row)

        # Font size
        self._font_row = QWidget()
        f_lay = QHBoxLayout(self._font_row)
        f_lay.setContentsMargins(0, 0, 0, 0)
        f_lay.addWidget(QLabel("Teckenstorlek:"))
        self._font_sp = QSpinBox()
        self._font_sp.setRange(6, 72)
        self._font_sp.setValue(int(font_size))
        f_lay.addWidget(self._font_sp)
        f_lay.addStretch()
        self._font_row.setVisible(mu_type in ('text', 'comment'))
        outer.addWidget(self._font_row)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _pick(self, hc):
        self._color = hc
        for c, btn in self._color_btns:
            btn.setStyleSheet(
                f"background:{c};border:2px solid {'#222' if c.lower()==hc.lower() else 'transparent'};"
                f"border-radius:3px;")

    def get_style(self):
        return (self._color,
                self._opacity_sl.value() / 100.0,
                self._width_sp.value(),
                self._font_sp.value())


# ══════════════════════════════════════════════════════════════════════════════
# MARKUP TABLE PANEL  (bottom panel, shown during markup edit mode)
# ══════════════════════════════════════════════════════════════════════════════

class MarkupTablePanel(QWidget):
    """Table of markups for the active node — lives in bottom splitter alongside scenario panel."""
    item_deleted     = pyqtSignal(int)        # mu_id
    item_vis_toggled = pyqtSignal(int, bool)  # mu_id, visible
    item_selected    = pyqtSignal(int)        # mu_id
    item_style_changed = pyqtSignal(int)      # mu_id
    item_duplicated  = pyqtSignal(int)        # mu_id

    _TYPE_ICON = {'polygon': '◻', 'polyline': '〰', 'text': '𝐀', 'comment': '💬'}
    _COLS      = ['Typ', 'Etikett', 'Färg', 'Opacitet', 'Tjocklek', 'Font', '👁']

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db      = db
        self.node_id = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)

        hdr = QHBoxLayout()
        title = QLabel("Nodmarkeringar")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        title.setFont(f)
        hdr.addWidget(title)
        hdr.addStretch()
        lay.addLayout(hdr)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_ctx_menu)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.setStyleSheet(
            "QTableWidget{border:1px solid #E2E3E1;font-size:10px;}"
            "QTableWidget::item:selected{background:#E6ECFA;color:#17191C;}")

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self._table)

    def load(self, node_id):
        self.node_id = node_id
        self.refresh()

    def refresh(self):
        self._table.setRowCount(0)
        if self.node_id is None:
            return
        for mu in self.db.node_markups_for_node(self.node_id):
            m = dict(mu)
            row = self._table.rowCount()
            self._table.insertRow(row)
            mu_id   = m['id']
            typ     = m.get('type', 'polygon')
            label   = m.get('label', '') or ''
            color   = m.get('color', '#1565C0')
            opacity = m.get('opacity', 0.45)
            width   = m.get('line_width', 12)
            font_sz = m.get('font_size', 12)
            visible = bool(m.get('visible', 1))

            icon_item = QTableWidgetItem(self._TYPE_ICON.get(typ, '◻'))
            icon_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            icon_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 0, icon_item)

            lbl_item = QTableWidgetItem(label)
            lbl_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            self._table.setItem(row, 1, lbl_item)

            color_item = QTableWidgetItem(color)
            color_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            color_item.setBackground(QColor(color))
            color_item.setForeground(QColor(color))
            color_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 2, color_item)

            op_item = QTableWidgetItem(f"{int(opacity * 100)}%")
            op_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            op_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 3, op_item)

            w_item = QTableWidgetItem(str(width))
            w_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            w_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 4, w_item)

            f_item = QTableWidgetItem(str(font_sz))
            f_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            f_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 5, f_item)

            vis_item = QTableWidgetItem('👁' if visible else '○')
            vis_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            vis_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 6, vis_item)

    def select_markup(self, mu_id):
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == mu_id:
                self._table.selectRow(row)
                break

    def clear(self):
        self.node_id = None
        self._table.setRowCount(0)

    def _on_cell_clicked(self, row, col):
        item = self._table.item(row, 0)
        if item is None:
            return
        mu_id = item.data(Qt.ItemDataRole.UserRole)
        if col == 6:
            self._toggle_visibility(row, mu_id)
        else:
            self.item_selected.emit(mu_id)

    def _toggle_visibility(self, row, mu_id):
        mu = self.db.get_node_markup(mu_id)
        if not mu:
            return
        new_vis = not bool(dict(mu).get('visible', 1))
        self.db.update_node_markup(mu_id, visible=new_vis)
        vis_item = self._table.item(row, 6)
        if vis_item:
            vis_item.setText('👁' if new_vis else '○')
        self.item_vis_toggled.emit(mu_id, new_vis)

    def _on_ctx_menu(self, pos):
        seen, rows = set(), []
        for idx in self._table.selectedIndexes():
            r = idx.row()
            if r not in seen:
                seen.add(r)
                item = self._table.item(r, 0)
                if item:
                    rows.append(item)
        if not rows:
            return
        menu = QMenu(self)
        n = len(rows)
        lbl = f"🗑 Ta bort ({n} valda)" if n > 1 else "🗑 Ta bort"
        act_del = menu.addAction(lbl)
        act_style = None
        act_dup   = None
        if n == 1:
            act_style = menu.addAction("✏ Ändra stil...")
            act_dup   = menu.addAction("📋 Duplicera")
        result = menu.exec(self._table.viewport().mapToGlobal(pos))
        if result == act_del:
            for item in rows:
                mu_id = item.data(Qt.ItemDataRole.UserRole)
                self.db.delete_node_markup(mu_id)
                self.item_deleted.emit(mu_id)
            self.refresh()
        elif act_style is not None and result == act_style:
            mu_id = rows[0].data(Qt.ItemDataRole.UserRole)
            mu = self.db.get_node_markup(mu_id)
            if mu:
                mu = dict(mu)
                dlg = _MarkupStyleDialog(
                    mu.get('type', 'polygon'),
                    mu.get('color', '#E53935'),
                    float(mu.get('opacity', 0.7)),
                    int(mu.get('line_width', 2)),
                    int(mu.get('font_size', 12)),
                    self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    c, op, lw, fs = dlg.get_style()
                    self.db.update_node_markup(mu_id, color=c, opacity=op,
                                               line_width=lw, font_size=fs)
                    self.item_style_changed.emit(mu_id)
                    self.refresh()
        elif act_dup is not None and result == act_dup:
            mu_id = rows[0].data(Qt.ItemDataRole.UserRole)
            self.item_duplicated.emit(mu_id)


# ══════════════════════════════════════════════════════════════════════════════
# RED MARKUP PANELS
# ══════════════════════════════════════════════════════════════════════════════

def _mk_symbol_icon(svg_str: str, sz: int = 32) -> QIcon:
    """Render an SVG string to a QIcon for the symbol selector buttons."""
    from PyQt6.QtSvg import QSvgRenderer
    from PyQt6.QtCore import QByteArray
    renderer = QSvgRenderer(QByteArray(svg_str.encode('utf-8')))
    pm = QPixmap(sz, sz)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if renderer.isValid():
        renderer.render(p)
    p.end()
    return QIcon(pm)


class _SymbolSelectorPopup(QFrame):
    """Floating popup with P&ID symbol buttons grouped by category."""
    symbol_selected = pyqtSignal(str)  # symbol_id

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setStyleSheet(
            "QFrame{background:#FFFFFF;border:1px solid #CFD1CE;border-radius:6px;}"
            "QPushButton{border:1px solid #E2E3E1;border-radius:4px;background:#FAFAFA;"
            "padding:2px;}"
            "QPushButton:hover{background:#F5F5F3;border-color:#CFD1CE;}"
            "QPushButton:checked{background:#17191C;border-color:#17191C;}")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        lbl = QLabel("Välj P&ID-symbol")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        lbl.setFont(f)
        outer.addWidget(lbl)

        tabs = QTabWidget()
        tabs.setStyleSheet(
            "QTabBar::tab{padding:4px 10px;font-size:9px;}"
            "QTabBar::tab:selected{background:#E6ECFA;}")
        outer.addWidget(tabs)

        for cat, syms in _RED_MARKUP_SYMBOLS.items():
            tab = QWidget()
            grid = QGridLayout(tab)
            grid.setContentsMargins(4, 4, 4, 4)
            grid.setSpacing(4)
            for i, (sid, sname, svg) in enumerate(syms):
                btn = QPushButton()
                btn.setFixedSize(40, 40)
                btn.setIcon(_mk_symbol_icon(svg, 28))
                btn.setIconSize(QSize(28, 28))
                btn.setToolTip(sname)
                btn.clicked.connect(lambda _, s=sid: (self.symbol_selected.emit(s), self.hide()))
                row, col = divmod(i, 4)
                grid.addWidget(btn, row, col)
            tabs.addTab(tab, cat)

        self.setFixedSize(220, 280)

    def show_near(self, btn):
        gp = btn.mapToGlobal(btn.rect().bottomLeft())
        self.move(gp.x() - self.width() - 4, gp.y())
        self.show()


class RedMarkupPanel(QWidget):
    """Narrow vertical ribbon for red markup tool selection (P&ID symbols + shapes)."""
    closed          = pyqtSignal()
    tool_changed    = pyqtSignal(str)
    symbol_selected = pyqtSignal(str)   # symbol_id
    all_vis_toggled = pyqtSignal(bool)
    style_changed   = pyqtSignal(str, float, int)   # color, opacity, line_width
    snap_changed    = pyqtSignal(bool)
    symbol_dims_changed = pyqtSignal(float, float, float)  # w, h, rot

    _TOOLS = [
        ('select',   'select',   'Välj/flytta'),
        ('polygon',  'polygon',  'Rita polygon'),
        ('polyline', 'polyline', 'Rita polylinje'),
        ('smart',    'smart',    'Smart polylinje'),
        ('comment',  'comment',  'Lägg till kommentar'),
        ('symbol',   'symbol',   'Lägg ut P&ID-symbol'),
    ]

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db            = db
        self.node_id       = None
        self._color        = '#CC0000'
        self._opacity      = 1.0
        self._width        = 4
        self._font_size    = 16
        self._snap         = True
        self._current_tool = 'select'
        self._selected_symbol_id = None
        self._popup        = None
        self._sym_popup    = None

        SZ = 48
        ISZ = 28
        self.setFixedWidth(CONFIG['W_SPINNER'])
        self.setStyleSheet("background:#FFFFFF; border-right: 1px solid #E8E8E8;")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 6, 5, 6)
        outer.setSpacing(3)

        _btn_ss = (
            "QPushButton{border:1px solid #D0D4DA;border-radius:5px;"
            "background:#FFFFFF;padding:0px;}"
            "QPushButton:checked{background:#C62828;border-color:#C62828;}"
            "QPushButton:hover:!checked{background:#FFEBEE;border-color:#EF9A9A;}")

        # Close button
        close_btn = QPushButton()
        close_btn.setFixedSize(SZ, SZ)
        close_btn.setToolTip("Avsluta redigering")
        close_icon = QIcon()
        close_icon.addPixmap(_mk_pm('close', ISZ, QColor("#ffffff")))
        close_btn.setIcon(close_icon)
        close_btn.setIconSize(QSize(ISZ, ISZ))
        close_btn.setStyleSheet(
            "QPushButton{background:#546E7A;border:none;border-radius:5px;padding:0px;}"
            "QPushButton:hover{background:#37474F;}")
        close_btn.clicked.connect(self.closed.emit)
        outer.addWidget(close_btn)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet("background:#E8E8E8;max-height:1px;border:none;")
        outer.addWidget(sep1)

        self._tool_btns = {}
        for tool, icon_name, tip in self._TOOLS:
            btn = QPushButton()
            btn.setFixedSize(SZ, SZ)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            if tool == 'symbol':
                # Custom red X icon for symbol tool
                sym_pm = QPixmap(ISZ, ISZ)
                sym_pm.fill(Qt.GlobalColor.transparent)
                p = QPainter(sym_pm)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setPen(QPen(QColor("#CC0000"), 3))
                p.drawText(QRect(0, 0, ISZ, ISZ), Qt.AlignmentFlag.AlignCenter, "⚙")
                p.end()
                sym_icon = QIcon()
                sym_icon.addPixmap(sym_pm, QIcon.Mode.Normal)
                btn.setIcon(sym_icon)
            else:
                btn.setIcon(_mk_icon(icon_name, ISZ))
            btn.setIconSize(QSize(ISZ, ISZ))
            btn.setStyleSheet(_btn_ss)
            btn.clicked.connect(lambda _, t=tool, b=btn: self._on_tool(t, b))
            outer.addWidget(btn)
            self._tool_btns[tool] = btn

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("background:#E8E8E8;max-height:1px;border:none;")
        outer.addWidget(sep2)

        self._color_strip = QLabel()
        self._color_strip.setFixedHeight(CONFIG['H_COLOR_STRIP'])
        self._color_strip.setStyleSheet(
            f"background:{self._color};border-radius:3px;border:none;")
        outer.addWidget(self._color_strip)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setStyleSheet("background:#E8E8E8;max-height:1px;border:none;")
        outer.addWidget(sep3)

        self._all_vis_btn = QPushButton()
        self._all_vis_btn.setFixedSize(SZ, SZ)
        self._all_vis_btn.setCheckable(True)
        self._all_vis_btn.setChecked(True)
        self._all_vis_btn.setToolTip("Dölj/visa alla redmarkeringar")
        eye_icon = QIcon()
        eye_icon.addPixmap(_mk_pm('eye', ISZ, QColor("#ffffff")),
                           QIcon.Mode.Normal, QIcon.State.Off)
        eye_icon.addPixmap(_mk_pm('eye', ISZ, QColor("#ffffff")),
                           QIcon.Mode.Normal, QIcon.State.On)
        self._all_vis_btn.setIcon(eye_icon)
        self._all_vis_btn.setIconSize(QSize(ISZ, ISZ))
        self._all_vis_btn.setStyleSheet(
            "QPushButton{border:none;border-radius:5px;padding:0px;"
            "background:#27AE60;}"
            "QPushButton:!checked{background:#E74C3C;}")
        self._all_vis_btn.clicked.connect(self._on_all_vis)
        outer.addWidget(self._all_vis_btn)

        outer.addStretch()
        self._on_tool('select')

    # ── Public API ────────────────────────────────────────────────────────────

    def load(self, node_id):
        self.node_id = node_id
        self._all_vis_btn.setChecked(True)
        self._on_tool('select')

    def get_current_style(self):
        return self._color, self._opacity, self._width, self._font_size

    def get_selected_symbol(self):
        return self._selected_symbol_id

    def get_symbol_dims(self):
        """Returns (w, h, rot) for the currently selected symbol."""
        return 40.0, 40.0, 0.0

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_tool(self, tool, btn=None):
        self._current_tool = tool
        for t, b in self._tool_btns.items():
            b.setChecked(t == tool)
        if tool == 'symbol' and btn is not None:
            if self._sym_popup is None:
                self._sym_popup = _SymbolSelectorPopup(self)
                self._sym_popup.symbol_selected.connect(self._on_symbol_selected)
            self._sym_popup.show_near(btn)
            return
        elif tool != 'select' and btn is not None:
            self._show_tool_popup(tool, btn)
        self.tool_changed.emit(tool)

    def _on_symbol_selected(self, symbol_id):
        self._selected_symbol_id = symbol_id
        self.symbol_selected.emit(symbol_id)
        self.tool_changed.emit('symbol')

    def _show_tool_popup(self, tool, btn):
        if self._popup is None:
            self._popup = _StylePopup(self)
        self._popup.show_for(tool, btn)

    def _on_all_vis(self, checked):
        if self.node_id is None:
            return
        self.db.set_all_node_red_markups_visible(self.node_id, checked)
        self.all_vis_toggled.emit(checked)

    def _apply_color(self, hex_c):
        self._color = hex_c
        self._color_strip.setStyleSheet(f"background:{hex_c};border-radius:3px;")
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_opacity(self, val):
        self._opacity = val / 100.0
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_width(self, val):
        self._width = val
        self.style_changed.emit(self._color, self._opacity, self._width)

    def _apply_font(self, val):
        self._font_size = val

    def _apply_snap(self, enabled):
        self._snap = enabled
        self.snap_changed.emit(enabled)


class RedMarkupTablePanel(QWidget):
    """Table of red markups for the active node."""
    item_deleted     = pyqtSignal(int)
    item_vis_toggled = pyqtSignal(int, bool)
    item_selected    = pyqtSignal(int)
    item_style_changed = pyqtSignal(int)

    _TYPE_ICON = {'polygon': '◻', 'polyline': '〰', 'text': '𝐀',
                  'comment': '💬', 'symbol': '⚙'}
    _COLS      = ['Typ', 'Etikett', 'Färg', 'Opacitet', 'Tjocklek', '👁']

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db      = db
        self.node_id = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(2)

        hdr = QHBoxLayout()
        title = QLabel("🔴 Redmarkeringar")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        title.setFont(f)
        hdr.addWidget(title)
        hdr.addStretch()
        lay.addLayout(hdr)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_ctx_menu)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.setStyleSheet(
            "QTableWidget{border:1px solid #FFCDD2;font-size:10px;}"
            "QTableWidget::item:selected{background:#FFEBEE;color:#C62828;}")

        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self._table)

    def load(self, node_id):
        self.node_id = node_id
        self.refresh()

    def refresh(self):
        self._table.setRowCount(0)
        if self.node_id is None:
            return
        for mu in self.db.node_red_markups_for_node(self.node_id):
            m = dict(mu)
            row = self._table.rowCount()
            self._table.insertRow(row)
            mu_id   = m['id']
            typ     = m.get('type', 'polygon')
            label   = m.get('label', '') or ''
            color   = m.get('color', '#CC0000')
            opacity = m.get('opacity', 1.0)
            width   = m.get('line_width', 4)
            visible = bool(m.get('visible', 1))

            icon_item = QTableWidgetItem(self._TYPE_ICON.get(typ, '◻'))
            icon_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            icon_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 0, icon_item)

            display_label = label if typ != 'symbol' else f"⚙ {label}"
            lbl_item = QTableWidgetItem(display_label)
            lbl_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            self._table.setItem(row, 1, lbl_item)

            color_item = QTableWidgetItem(color)
            color_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            color_item.setBackground(QColor(color))
            color_item.setForeground(QColor(color))
            color_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 2, color_item)

            op_item = QTableWidgetItem(f"{int(opacity * 100)}%")
            op_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            op_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 3, op_item)

            w_item = QTableWidgetItem(str(width))
            w_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            w_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 4, w_item)

            vis_item = QTableWidgetItem('👁' if visible else '○')
            vis_item.setData(Qt.ItemDataRole.UserRole, mu_id)
            vis_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 5, vis_item)

    def select_markup(self, mu_id):
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == mu_id:
                self._table.selectRow(row)
                break

    def clear(self):
        self.node_id = None
        self._table.setRowCount(0)

    def _on_cell_clicked(self, row, col):
        item = self._table.item(row, 0)
        if item is None:
            return
        mu_id = item.data(Qt.ItemDataRole.UserRole)
        if col == 5:
            self._toggle_visibility(row, mu_id)
        else:
            self.item_selected.emit(mu_id)

    def _toggle_visibility(self, row, mu_id):
        mu = self.db.get_node_red_markup(mu_id)
        if not mu:
            return
        new_vis = not bool(dict(mu).get('visible', 1))
        self.db.update_node_red_markup(mu_id, visible=new_vis)
        vis_item = self._table.item(row, 5)
        if vis_item:
            vis_item.setText('👁' if new_vis else '○')
        self.item_vis_toggled.emit(mu_id, new_vis)

    def _on_ctx_menu(self, pos):
        seen, rows = set(), []
        for idx in self._table.selectedIndexes():
            r = idx.row()
            if r not in seen:
                seen.add(r)
                item = self._table.item(r, 0)
                if item:
                    rows.append(item)
        if not rows:
            return
        menu = QMenu(self)
        n = len(rows)
        lbl = f"🗑 Ta bort ({n} valda)" if n > 1 else "🗑 Ta bort"
        act_del = menu.addAction(lbl)
        act_style = None
        if n == 1:
            mu = self.db.get_node_red_markup(rows[0].data(Qt.ItemDataRole.UserRole))
            if mu and dict(mu).get('type') == 'symbol':
                act_style = menu.addAction("📐 Ändra storlek/rotation...")
            else:
                act_style = menu.addAction("✏ Ändra stil...")
        result = menu.exec(self._table.viewport().mapToGlobal(pos))
        if result == act_del:
            for item in rows:
                mu_id = item.data(Qt.ItemDataRole.UserRole)
                self.db.delete_node_red_markup(mu_id)
                self.item_deleted.emit(mu_id)
            self.refresh()
        elif act_style is not None and result == act_style:
            mu_id = rows[0].data(Qt.ItemDataRole.UserRole)
            mu = self.db.get_node_red_markup(mu_id)
            if mu:
                mu = dict(mu)
                if mu.get('type') == 'symbol':
                    dlg = _SymbolDimsDialog(
                        float(mu.get('symbol_w', 40)),
                        float(mu.get('symbol_h', 40)),
                        float(mu.get('symbol_rot', 0)),
                        self)
                    if dlg.exec() == QDialog.DialogCode.Accepted:
                        w, h, rot = dlg.get_dims()
                        self.db.update_node_red_markup(mu_id, symbol_w=w, symbol_h=h, symbol_rot=rot)
                        self.item_style_changed.emit(mu_id)
                        self.refresh()
                else:
                    dlg = _MarkupStyleDialog(
                        mu.get('type', 'polygon'),
                        mu.get('color', '#CC0000'),
                        float(mu.get('opacity', 1.0)),
                        int(mu.get('line_width', 4)),
                        int(mu.get('font_size', 12)),
                        self)
                    if dlg.exec() == QDialog.DialogCode.Accepted:
                        c, op, lw, fs = dlg.get_style()
                        self.db.update_node_red_markup(mu_id, color=c, opacity=op,
                                                       line_width=lw, font_size=fs)
                        self.item_style_changed.emit(mu_id)
                        self.refresh()


class _SymbolDimsDialog(QDialog):
    """Dialog to adjust symbol width, height, and rotation."""
    def __init__(self, w=40, h=40, rot=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Symbolstorlek och rotation")
        self.setFixedWidth(CONFIG['W_PANEL_MIN'])
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        outer = QVBoxLayout(self)
        outer.setSpacing(10)

        form = QFormLayout()
        self._w_sp = QSpinBox(); self._w_sp.setRange(5, 500); self._w_sp.setValue(int(w))
        self._w_sp.setSuffix(" pt")
        self._h_sp = QSpinBox(); self._h_sp.setRange(5, 500); self._h_sp.setValue(int(h))
        self._h_sp.setSuffix(" pt")
        self._r_sp = QSpinBox(); self._r_sp.setRange(-360, 360); self._r_sp.setValue(int(rot))
        self._r_sp.setSuffix(" °")
        form.addRow("Bredd:", self._w_sp)
        form.addRow("Höjd:", self._h_sp)
        form.addRow("Rotation:", self._r_sp)
        outer.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def get_dims(self):
        return float(self._w_sp.value()), float(self._h_sp.value()), float(self._r_sp.value())


# ══════════════════════════════════════════════════════════════════════════════
# TREE PANEL
# ══════════════════════════════════════════════════════════════════════════════

NODE_T = 1
CAUSE_T = 2
CONS_T = 3
SG_T = 4
DEV_T = 5
EQUIP_T = 6
LEDORD_T = 7   # pure grouping level (guide word / "ledord") — no DB row of
               # its own, several deviation rows across different equipment
               # can share one. See NOTES.md "Nod → Ledord → Utrustning".

DEVIATION_TYPES = [
    "Lågt flöde",
    "Högt flöde",
    "Missriktat flöde",
    "Omvänt flöde",
    "Högt tryck",
    "Lågt tryck",
    "Hög nivå",
    "Låg nivå",
    "Hög temperatur",
    "Låg temperatur",
    "Avvikande sammansättning",
    "Bortfall av hjälpsystem",
    "Drift",
    "Underhåll",
    "Start-up / Shut-down",
    "Övrigt",
]


class _PickDeviationDialog(QDialog):
    """Small dialog to pick/type a deviation description when adding a new deviation."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lägg till avvikelse")
        self.description = ""
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Välj eller skriv en avvikelse:"))
        self.combo = QComboBox()
        self.combo.addItems(DEVIATION_TYPES)
        self.combo.setEditable(True)
        self.combo.setCurrentText(DEVIATION_TYPES[0])
        layout.addWidget(self.combo)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self.resize(300, 100)

    def _accept(self):
        self.description = self.combo.currentText().strip() or "Övrigt"
        self.accept()


class TreePanel(QWidget):
    item_selected               = pyqtSignal(int, int)
    add_causes_on_pid_requested       = pyqtSignal(int)   # deviation_id
    add_consequences_on_pid_requested = pyqtSignal(int)   # cause_id
    add_safeguards_on_pid_requested   = pyqtSignal(int)   # consequence_id
    edit_node_markup_requested        = pyqtSignal(int)        # node_id
    edit_red_markup_requested         = pyqtSignal(int)        # node_id
    node_markup_vis_requested         = pyqtSignal(int, bool)  # node_id, visible
    node_jump_to_markup               = pyqtSignal(int)         # node_id
    structure_changed           = pyqtSignal()
    visibility_changed          = pyqtSignal(str, bool)   # marker_type, visible
    exit_pid_mode_requested     = pyqtSignal()    # exit any active P&ID placement mode
    # Equipment marker(s) dragged from the P&ID onto a deviation item (e.g.
    # "Lågt flöde") — 2026-08-08, see NOTES.md. Args: (deviation_id, list
    # of equipment_markers.id).
    equipment_dropped_on_deviation = pyqtSignal(int, object)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._clipboard = None  # {'type': T, 'id': id}

        lbl = QLabel("HAZOP-träd")
        f = QFont(); f.setBold(True)
        lbl.setFont(f)
        layout.addWidget(lbl)

        # ── Visibility toggle buttons at TOP (before tree) ──────────────────────
        vis_row = QHBoxLayout()
        vis_row.setSpacing(4)

        _VIS_BTNS = [
            ('cause',        '⚙️ Orsaker',       '#e74c3c', '#fde8e8'),
            ('consequence',  '⚠️ Konsekvenser',  '#e67e22', '#fef0e0'),
            ('safeguard',    '🛡️ Safeguards',    '#27ae60', '#e8f8e8'),
            ('equipment',    '🔧 Utrustning',    '#7f8c8d', '#ecf0f1'),
        ]
        self._vis_btns = {}
        for type_key, label, color_on, color_off in _VIS_BTNS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(True)
            btn.setFixedHeight(CONFIG['H_CTRL_STD'])
            btn.setStyleSheet(
                f"QPushButton{{background:{color_on}; color:white; border:none;"
                f" border-radius:3px; font-size:10px; font-weight:bold; padding:0 4px;}}"
                f"QPushButton:!checked{{background:{color_off}; color:#aaa;}}")
            btn.toggled.connect(
                lambda checked, t=type_key: self.visibility_changed.emit(t, checked))
            vis_row.addWidget(btn)
            self._vis_btns[type_key] = btn

        layout.addLayout(vis_row)

        # ── Tree action buttons (2nd row) — Nod/Avvikelse have no natural
        # right-click target of their own (they act on the whole tree or
        # need a node selected first), and "Ta bort" is common enough to
        # warrant a one-click button alongside the context-menu entry.
        # Orsak/Konsekvens/Safeguard stay right-click-only (add_deviation on
        # a NODE_T item, add_cause on DEV_T, etc.) since those already have
        # an obvious parent item to right-click.
        action_row = QHBoxLayout()
        action_row.setSpacing(4)
        for label, tip, slot in (
            ("+ Nod",       "Lägg till ny nod",       self.add_node),
            ("+ Avvikelse", "Lägg till ny avvikelse", self.add_deviation),
            ("🗑 Ta bort",  "Ta bort markerat",       self.delete_selected),
        ):
            btn = QPushButton(label)
            btn.setToolTip(tip)
            btn.setFixedHeight(CONFIG['H_CTRL_STD'])
            btn.clicked.connect(slot)
            action_row.addWidget(btn)
        layout.addLayout(action_row)

        # ── Tree widget ──────────────────────────────────────────────────────────
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        # Accepts an external drop (equipment marker dragged from the P&ID
        # view onto a deviation, e.g. "Lågt flöde" — 2026-08-08, see
        # NOTES.md) — handled in eventFilter below, not Qt's own internal
        # DragDropMode (this tree has no internal drag-reordering).
        self.tree.setAcceptDrops(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.currentItemChanged.connect(self._on_select)
        self.tree.itemDoubleClicked.connect(self._on_item_double_click)
        self.tree.installEventFilter(self)   # keyboard navigation (feature 17)
        layout.addWidget(self.tree)

        # ── Collapse/Expand buttons (compact control bar) ───────────────────────
        compact_row = QHBoxLayout()
        compact_row.setSpacing(4)
        compact_row.addStretch()

        btn_collapse = QPushButton("⊟")
        btn_collapse.setFixedSize(26, 26)
        btn_collapse.setToolTip("Kollapsa alla")
        btn_collapse.clicked.connect(lambda: self.tree.collapseAll())
        compact_row.addWidget(btn_collapse)

        btn_expand = QPushButton("⊞")
        btn_expand.setFixedSize(26, 26)
        btn_expand.setToolTip("Expandera alla")
        btn_expand.clicked.connect(lambda: self.tree.expandAll())
        compact_row.addWidget(btn_expand)

        layout.addLayout(compact_row)

    def refresh(self, select_type=None, select_id=None, emit_selection=True):
        expanded = set()
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            if item.isExpanded():
                expanded.add((item.data(0, Qt.ItemDataRole.UserRole + 1),
                              item.data(0, Qt.ItemDataRole.UserRole)))
            it += 1

        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            target = None
            bold_font = QFont(); bold_font.setBold(True)

            marked_causes = self.db.marked_cause_ids()
            marked_consequences = self.db.marked_consequence_ids()
            marked_safeguards = self.db.marked_safeguard_ids()

            def add_cause_children(citem, cause):
                """Append the consequence/safeguard subtree for a single
                cause as children of citem — factored out of
                add_causes_to_item so the equipment-merged trivial-cause
                case below (an empty, just-tagged cause whose only
                content duplicates its own equipment header) can attach
                it directly to that header row instead of a separate,
                redundant cause item (2026-08-10, see NOTES.md "objektet
                redovisas två gånger")."""
                nonlocal target
                for ki, cons in enumerate(self.db.consequences(cause['id']), 1):
                    cause_freq = self.db.cause_frequency_level(cause)
                    level, _, _ = risk_info(cause_freq, cons['severity'])
                    risk_icon = RISK_ICON.get(level, '⚪')
                    placed_k = cons['id'] in marked_consequences
                    kitem = QTreeWidgetItem([f"      {risk_icon}  {ki}. {cons['description'][:40]}"])
                    kitem.setIcon(0, _make_pin_icon(placed_k))
                    kitem.setData(0, Qt.ItemDataRole.UserRole, cons['id'])
                    kitem.setData(0, Qt.ItemDataRole.UserRole + 1, CONS_T)
                    citem.addChild(kitem)
                    if (CONS_T, cons['id']) in expanded: kitem.setExpanded(True)
                    if select_type == CONS_T and select_id == cons['id']: target = kitem

                    for si, sg in enumerate(self.db.safeguards(cons['id']), 1):
                        rrf = (sg['rrf'] or 1) if sg['rrf'] is not None else 1
                        rrf_str = f"RRF{rrf}" if rrf > 1 else "—"
                        try:
                            linked = bool(sg['source_id'])
                        except (IndexError, KeyError):
                            linked = False
                        sg_icon = "🔗🛡" if linked else "🛡"
                        placed_s = sg['id'] in marked_safeguards
                        sgitem = QTreeWidgetItem([f"         {sg_icon}  {si}. {sg['description'][:35]}  [{rrf_str}]"])
                        sgitem.setIcon(0, _make_pin_icon(placed_s))
                        sgitem.setData(0, Qt.ItemDataRole.UserRole, sg['id'])
                        sgitem.setData(0, Qt.ItemDataRole.UserRole + 1, SG_T)
                        kitem.addChild(sgitem)
                        if select_type == SG_T and select_id == sg['id']: target = sgitem

            def add_causes_to_item(ditem, dev_id):
                """Append the cause/consequence/safeguard subtree for
                deviation dev_id as children of ditem — factored out of
                add_deviation_subtree so the equipment-grouped single-
                deviation case (below) can attach it directly to the
                equipment item instead of a separate, redundant deviation
                item (2026-08-09, see NOTES.md "kaka på kaka")."""
                nonlocal target
                for ci, cause in enumerate(self.db.causes_for_deviation(dev_id), 1):
                    placed_c = cause['id'] in marked_causes
                    tag    = (cause['comp_tag'] or '').strip() if cause['comp_tag'] else ''
                    desc   = (cause['description'] or '').strip()
                    # A REAL description is always more useful in the tree
                    # than repeating the tag a second row down (the tag is
                    # already visible one level up, on the equipment/
                    # deviation header) — only fall back to the tag for a
                    # still-untouched placeholder cause with nothing else
                    # to show yet (2026-08-11, bug report: a real cause
                    # "Flödesgivare felar -> styrventil stänger" was
                    # showing as just "=E1.M1.QMA127", the same tag its
                    # own parent row already displays, see NOTES.md).
                    trivial_desc = desc in ('', 'Ny orsak')
                    c_label = (tag if tag else desc[:50]) if trivial_desc else desc[:50]
                    citem = QTreeWidgetItem([f"    ⚙ {ci}. {c_label}"])
                    citem.setIcon(0, _make_pin_icon(placed_c))
                    citem.setData(0, Qt.ItemDataRole.UserRole, cause['id'])
                    citem.setData(0, Qt.ItemDataRole.UserRole + 1, CAUSE_T)
                    ditem.addChild(citem)
                    if (CAUSE_T, cause['id']) in expanded: citem.setExpanded(True)
                    if select_type == CAUSE_T and select_id == cause['id']: target = citem
                    add_cause_children(citem, cause)

            def add_deviation_subtree(parent_item, dev, di):
                nonlocal target
                ditem = QTreeWidgetItem([f"  ⬡  {di}. {dev['description'][:55]}"])
                ditem.setData(0, Qt.ItemDataRole.UserRole, dev['id'])
                ditem.setData(0, Qt.ItemDataRole.UserRole + 1, DEV_T)
                dev_font = QFont(); dev_font.setItalic(True)
                ditem.setFont(0, dev_font)
                parent_item.addChild(ditem)
                if (DEV_T, dev['id']) in expanded: ditem.setExpanded(True)
                if select_type == DEV_T and select_id == dev['id']: target = ditem
                add_causes_to_item(ditem, dev['id'])

            for ni, node in enumerate(self.db.nodes(), 1):
                node_on_pid = bool(node['markup_points'])
                pid_pin = " 📍" if node_on_pid else ""
                nitem = QTreeWidgetItem([f"🏭  {ni}. {node['name']}{pid_pin}"])
                nitem.setData(0, Qt.ItemDataRole.UserRole, node['id'])
                nitem.setData(0, Qt.ItemDataRole.UserRole + 1, NODE_T)
                nitem.setFont(0, bold_font)
                nitem.setToolTip(0, node['pid_ref'] or '')
                self.tree.addTopLevelItem(nitem)
                if (NODE_T, node['id']) in expanded: nitem.setExpanded(True)
                if select_type == NODE_T and select_id == node['id']: target = nitem

                # Nod → Ledord → Utrustning → Avvikelse (2026-08-07, see
                # NOTES.md): deviations are grouped by their guide-word text
                # FIRST (several deviation rows across different equipment
                # can share the same description, e.g. "Lågt flöde" for both
                # a pump and a valve under one node), then WITHIN each guide
                # word, split into equipment_id-tagged rows (grouped under a
                # "Utrustning" item) and equipment_id=NULL rows (shown
                # directly under the guide word — every deviation that
                # existed before this feature, unaffected in substance,
                # just one extra grouping level to expand).
                ledord_groups = {}
                for dev in self.db.deviations(node['id']):
                    ledord_groups.setdefault(dev['description'], []).append(dev)

                di = 0
                for description, dev_list in ledord_groups.items():
                    equipment_groups = {}
                    ungrouped_devs = []
                    for dev in dev_list:
                        eq_id = dev['equipment_id']
                        if eq_id:
                            equipment_groups.setdefault(eq_id, []).append(dev)
                        else:
                            ungrouped_devs.append(dev)

                    # Skip the Ledord wrapper for the common case: exactly
                    # one plain (no equipment) deviation for this guide
                    # word — no equipment to distinguish between, so the
                    # wrapper item would just repeat the SAME guide-word
                    # text directly above its own single child (reported:
                    # "varför är det dubbelt?" — every guide word showed
                    # its own name twice). Put the deviation straight under
                    # the node instead, exactly like before this feature
                    # existed. Once a SECOND deviation for this guide word
                    # shows up (equipment-scoped, or another plain one),
                    # the wrapper starts pulling real weight and comes back.
                    if not equipment_groups and len(ungrouped_devs) == 1:
                        di += 1
                        add_deviation_subtree(nitem, ungrouped_devs[0], di)
                        continue

                    litem = QTreeWidgetItem([f"  ⬡  {description}"])
                    ledord_key = f"{node['id']}:{description}"
                    litem.setData(0, Qt.ItemDataRole.UserRole, ledord_key)
                    litem.setData(0, Qt.ItemDataRole.UserRole + 1, LEDORD_T)
                    led_font = QFont(); led_font.setItalic(True)
                    litem.setFont(0, led_font)
                    nitem.addChild(litem)
                    if (LEDORD_T, ledord_key) in expanded: litem.setExpanded(True)

                    for eq_id, eq_devs in equipment_groups.items():
                        eq = self.db.get_equipment_by_id(eq_id)
                        eq_label = f"{eq['tag']} — {eq['equipment_type']}" if eq else f"Utrustning #{eq_id}"
                        eitem = QTreeWidgetItem([f"    🔧  {eq_label}"])
                        eq_font = QFont(); eq_font.setBold(True)
                        eitem.setFont(0, eq_font)
                        litem.addChild(eitem)
                        if len(eq_devs) == 1:
                            # Collapse the redundant deviation-description
                            # level (2026-08-09, see NOTES.md "kaka på
                            # kaka") — a deviation's description is always
                            # identical to this Ledord group's own label
                            # (grouped by description above), so a separate
                            # child item under the equipment just repeats
                            # text the user already sees one level up. This
                            # item carries the DEVIATION's identity instead
                            # of EQUIP_T (get_or_create_deviation makes this
                            # the only deviation for this equipment+guide-word
                            # combo in practice), so it's the direct,
                            # interactive target for "add cause" and
                            # equipment-dropped-on-deviation — previously
                            # dead ends when the row was EQUIP_T.
                            dev = eq_devs[0]
                            di += 1
                            dev_causes = self.db.causes_for_deviation(dev['id'])
                            merge_tag = ((eq['tag'] or '').strip() if eq else '')
                            trivial_desc = (dev_causes[0]['description'] or '').strip() in ('', 'Ny orsak') \
                                if dev_causes else False
                            if (len(dev_causes) == 1
                                    and trivial_desc
                                    and (dev_causes[0]['comp_tag'] or '').strip() == merge_tag
                                    and merge_tag):
                                # One more "kaka på kaka" level (2026-08-10,
                                # see NOTES.md "objektet redovisas två
                                # gånger"): this deviation's only cause has
                                # no real content yet — created empty by a
                                # drag-and-drop tag placement — so its own
                                # tree label falls back to the SAME
                                # equipment tag this header row already
                                # shows. Attach the cause's identity (and
                                # its consequences) directly to this row
                                # instead of a redundant child that repeats
                                # the tag a second time with nothing new to
                                # say. Reappears as a normal child row the
                                # moment the cause gets a real description,
                                # or a second cause is added.
                                cause = dev_causes[0]
                                placed_c = cause['id'] in marked_causes
                                eitem.setIcon(0, _make_pin_icon(placed_c))
                                eitem.setData(0, Qt.ItemDataRole.UserRole, cause['id'])
                                eitem.setData(0, Qt.ItemDataRole.UserRole + 1, CAUSE_T)
                                if (CAUSE_T, cause['id']) in expanded: eitem.setExpanded(True)
                                if select_type == CAUSE_T and select_id == cause['id']: target = eitem
                                add_cause_children(eitem, cause)
                            else:
                                eitem.setData(0, Qt.ItemDataRole.UserRole, dev['id'])
                                eitem.setData(0, Qt.ItemDataRole.UserRole + 1, DEV_T)
                                if (DEV_T, dev['id']) in expanded: eitem.setExpanded(True)
                                if select_type == DEV_T and select_id == dev['id']: target = eitem
                                add_causes_to_item(eitem, dev['id'])
                        else:
                            eitem.setData(0, Qt.ItemDataRole.UserRole, eq_id)
                            eitem.setData(0, Qt.ItemDataRole.UserRole + 1, EQUIP_T)
                            if (EQUIP_T, eq_id) in expanded: eitem.setExpanded(True)
                            if select_type == EQUIP_T and select_id == eq_id: target = eitem
                            for dev in eq_devs:
                                di += 1
                                add_deviation_subtree(eitem, dev, di)

                    for dev in ungrouped_devs:
                        # Every node is auto-seeded with one empty, generic
                        # (equipment_id=NULL) deviation per guide word — see
                        # add_node(). Once THIS SAME guide word also has a
                        # real equipment-scoped entry, the still-empty
                        # generic one is just unused scaffolding sitting
                        # right next to it under the same Ledord label —
                        # reads as "Lågt flöde" appearing twice. Hide it
                        # (not delete — reappears the moment it gets a real
                        # cause, and non-empty generic entries always show).
                        if equipment_groups and not self.db.causes_for_deviation(dev['id']):
                            continue
                        di += 1
                        add_deviation_subtree(litem, dev, di)

            if target and not emit_selection:
                # Update the tree's visual highlight while signals are still
                # blocked, so setCurrentItem does NOT cascade into
                # currentItemChanged -> _on_select -> item_selected -> _on_selected.
                # Callers that pass emit_selection=False (e.g. _on_marker_navigate)
                # already trigger the selection-handling logic explicitly afterward,
                # so we must not let the tree fire it a second time here.
                self.tree.setCurrentItem(target)
                self.tree.scrollToItem(target)
        finally:
            self.tree.blockSignals(False)
            if target and emit_selection:
                self.tree.setCurrentItem(target)
                self.tree.scrollToItem(target)

    def _current(self):
        item = self.tree.currentItem()
        if item is None:
            return None, None
        return (item.data(0, Qt.ItemDataRole.UserRole + 1),
                item.data(0, Qt.ItemDataRole.UserRole))

    def _resolve_node_id(self, type_, id_):
        if type_ == NODE_T: return id_
        if type_ == EQUIP_T: return self.db.equipment_node_id(id_)
        if type_ == LEDORD_T:
            # id_ is "node_id:description" (see refresh()) — LEDORD_T has no
            # DB row of its own, but the node_id is encoded right in the key.
            try:
                return int(str(id_).split(':', 1)[0])
            except (ValueError, IndexError):
                return None
        if type_ == DEV_T:
            r = self.db.get_deviation(id_); return r['node_id'] if r else None
        if type_ == CAUSE_T:
            r = self.db.get_cause(id_); return r['node_id'] if r else None
        if type_ == CONS_T:
            r = self.db.get_consequence(id_)
            if r:
                c = self.db.get_cause(r['cause_id']); return c['node_id'] if c else None
        if type_ == SG_T:
            r = self.db.get_safeguard(id_)
            if r:
                c = self.db.get_consequence(r['consequence_id'])
                if c:
                    ca = self.db.get_cause(c['cause_id']); return ca['node_id'] if ca else None
        return None

    def _resolve_equipment_id(self, type_, id_):
        """Walk any tree item back to the equipment it's grouped under, or
        None if it sits directly under a node (no equipment_id set on its
        deviation) — see 'Nod → Utrustning → Avvikelse' in NOTES.md."""
        if type_ == EQUIP_T: return id_
        dev_id = self._resolve_deviation_id(type_, id_) if type_ != DEV_T else id_
        if dev_id is None:
            return None
        r = self.db.get_deviation(dev_id)
        return r['equipment_id'] if r else None

    def _resolve_deviation_id(self, type_, id_):
        if type_ == DEV_T: return id_
        if type_ == CAUSE_T:
            r = self.db.get_cause(id_); return r['deviation_id'] if r else None
        if type_ == CONS_T:
            r = self.db.get_consequence(id_)
            if r:
                c = self.db.get_cause(r['cause_id'])
                return c['deviation_id'] if c else None
        if type_ == SG_T:
            r = self.db.get_safeguard(id_)
            if r:
                c = self.db.get_consequence(r['consequence_id'])
                if c:
                    ca = self.db.get_cause(c['cause_id'])
                    return ca['deviation_id'] if ca else None
        return None

    def _resolve_cause_id(self, type_, id_):
        if type_ == CAUSE_T: return id_
        if type_ == CONS_T:
            r = self.db.get_consequence(id_); return r['cause_id'] if r else None
        if type_ == SG_T:
            r = self.db.get_safeguard(id_)
            if r:
                c = self.db.get_consequence(r['consequence_id']); return c['cause_id'] if c else None
        return None

    def _resolve_consequence_id(self, type_, id_):
        if type_ == CONS_T: return id_
        if type_ == SG_T:
            r = self.db.get_safeguard(id_); return r['consequence_id'] if r else None
        return None

    def add_node(self):
        new_id = self.db.add_node()
        self.refresh(NODE_T, new_id)
        self.structure_changed.emit()

    def add_deviation(self):
        type_, id_ = self._current()
        node_id = self._resolve_node_id(type_, id_) if type_ else None
        if node_id is None:
            QMessageBox.information(self, "Välj nod", "Välj en nod i trädet."); return
        dlg = _PickDeviationDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_id = self.db.add_deviation(node_id, dlg.description)
        self.refresh(DEV_T, new_id)
        self.structure_changed.emit()

    def add_cause(self):
        type_, id_ = self._current()
        dev_id = self._resolve_deviation_id(type_, id_) if type_ else None
        if dev_id is None:
            QMessageBox.information(self, "Välj avvikelse", "Välj en avvikelse i trädet."); return
        self._open_cause_picker_for_deviation(dev_id, self._resolve_node_id(type_, id_))

    def _open_cause_picker_for_deviation(self, dev_id, node_id=None):
        """Open the standard-causes picker to create a new cause under dev_id.
        Shared by every 'add cause under this deviation' entry point in the
        tree so a freshly created cause always starts with real content
        instead of a silent blank 'Ny orsak' placeholder.
        """
        dev = self.db.get_deviation(dev_id)
        dev_desc = dev['description'] if dev else ''
        if node_id is None:
            node_id = dev['node_id'] if dev else None
        std_dev_id = _resolve_std_deviation_id(self.db, dev_desc)

        popup = StandardCausesPickerPopup(
            self.db, std_dev_id, deviation_name=dev_desc,
            node_id=node_id, parent=self)

        def _on_picked(description, frequency):
            new_id, _cons_id = _create_cause_from_pick(self.db, dev_id, description, frequency)
            self.refresh(CAUSE_T, new_id)
            self.structure_changed.emit()

        popup.cause_picked.connect(_on_picked)
        popup.exec()

    def add_consequence(self):
        type_, id_ = self._current()
        cause_id = self._resolve_cause_id(type_, id_) if type_ else None
        if cause_id is None:
            QMessageBox.information(self, "Välj cause", "Välj en cause i trädet."); return
        new_id = self.db.add_consequence(cause_id)
        self.exit_pid_mode_requested.emit()
        self.refresh(CONS_T, new_id)
        self.structure_changed.emit()

    def add_safeguard(self):
        type_, id_ = self._current()
        cons_id = self._resolve_consequence_id(type_, id_) if type_ else None
        if cons_id is None:
            QMessageBox.information(self, "Välj konsekvens", "Välj en konsekvens i trädet."); return
        new_id = self.db.add_safeguard(cons_id)
        self.exit_pid_mode_requested.emit()
        self.refresh(SG_T, new_id)
        self.structure_changed.emit()

    def delete_selected(self):
        type_, id_ = self._current()
        if type_ is None: return
        names = {NODE_T: 'noden', DEV_T: 'avvikelsen', CAUSE_T: 'orsaken',
                 CONS_T: 'konsekvensen', SG_T: 'safeguarden'}
        reply = QMessageBox.question(self, "Ta bort",
            f"Ta bort {names.get(type_, 'objektet')} och allt under den?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes: return
        deletors = {NODE_T: self.db.delete_node, DEV_T: self.db.delete_deviation,
                    CAUSE_T: self.db.delete_cause, CONS_T: self.db.delete_consequence,
                    SG_T: self.db.delete_safeguard}
        if type_ in deletors:
            deletors[type_](id_)
        self.refresh()
        self.structure_changed.emit()

    def _on_select(self, current, _previous):
        if current is None: return
        type_ = current.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = current.data(0, Qt.ItemDataRole.UserRole)
        self.item_selected.emit(type_, id_)

    def _on_item_double_click(self, item, col):
        if item is None:
            return
        type_ = item.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = item.data(0, Qt.ItemDataRole.UserRole)
        if type_ == NODE_T and self.db.has_node_markups(id_):
            self.node_jump_to_markup.emit(id_)

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None: return
        type_ = item.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = item.data(0, Qt.ItemDataRole.UserRole)
        if type_ in (EQUIP_T, LEDORD_T):
            # Both are live grouping views (over equipment_catalog/deviations,
            # or over deviation description text), not their own DB row —
            # nothing to add/copy/delete here from the tree (use the
            # equipment's own bottom bar on the P&ID, or the Utrustningsregister).
            return
        menu  = QMenu(self)

        if type_ == NODE_T:
            menu.addAction("+ Lägg till avvikelse", self.add_deviation)
            menu.addAction("✏️ Editera nodmarkup",
                           lambda i=id_: self.edit_node_markup_requested.emit(i))
            menu.addAction("🔴 Editera redmarkup",
                           lambda i=id_: self.edit_red_markup_requested.emit(i))
            if self.db.has_node_markups(id_):
                is_vis = self.db.has_visible_node_markups(id_)
                if is_vis:
                    menu.addAction("🙈 Dölj nod på P&ID",
                                   lambda i=id_: self.node_markup_vis_requested.emit(i, False))
                else:
                    menu.addAction("👁 Visa nod på P&ID",
                                   lambda i=id_: self.node_markup_vis_requested.emit(i, True))
        elif type_ == DEV_T:
            menu.addAction("+ Lägg till orsak", self.add_cause)
            menu.addAction("📍 Lägg till orsaker på P&ID",
                           lambda i=id_: self.add_causes_on_pid_requested.emit(i))
        elif type_ == CAUSE_T:
            # "+ Lägg till orsak" also offered here (not just on DEV_T) so
            # a cause row that merged with its deviation's own header
            # (2026-08-10, see NOTES.md "objektet redovisas två gånger")
            # still lets you add a SECOND, distinct cause to the same
            # deviation — add_cause() already resolves the deviation via
            # the cause's own deviation_id regardless of which row type
            # triggered it.
            menu.addAction("+ Lägg till orsak", self.add_cause)
            menu.addAction("+ Lägg till konsekvens", self.add_consequence)
            menu.addAction("📍 Lägg till konsekvens på P&ID",
                           lambda i=id_: self.add_consequences_on_pid_requested.emit(i))
        elif type_ == CONS_T:
            menu.addAction("+ Lägg till safeguard", self.add_safeguard)
            menu.addAction("📍 Lägg till safeguard på P&ID",
                           lambda i=id_: self.add_safeguards_on_pid_requested.emit(i))

        # Copy
        copy_labels = {CAUSE_T: "📋 Kopiera orsak",
                       CONS_T:  "📋 Kopiera konsekvens",
                       SG_T:    "📋 Kopiera safeguard"}
        if type_ in copy_labels:
            menu.addAction(copy_labels[type_],
                           lambda t=type_, i=id_: self._copy_item(t, i))

        # Paste (only if clipboard is compatible with current target)
        if self._clipboard:
            ct = self._clipboard['type']
            can_paste = (
                (ct == CAUSE_T and type_ in (NODE_T, DEV_T, CAUSE_T, CONS_T, SG_T)) or
                (ct == CONS_T  and type_ in (CAUSE_T, CONS_T, SG_T)) or
                (ct == SG_T    and type_ in (CONS_T, SG_T))
            )
            if can_paste:
                menu.addAction("📋 Klistra in här", self._paste_item)

        menu.addSeparator()
        menu.addAction("Ta bort", self.delete_selected)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _copy_item(self, type_, id_):
        self._clipboard = {'type': type_, 'id': id_}

    def _paste_item(self):
        if not self._clipboard:
            return
        ct    = self._clipboard['type']
        cid   = self._clipboard['id']
        type_, id_ = self._current()

        if ct == CAUSE_T:
            dev_id = self._resolve_deviation_id(type_, id_)
            if not dev_id:
                # Fall back: get or create "Övrigt" deviation on the resolved node
                node_id = self._resolve_node_id(type_, id_)
                if not node_id:
                    return
                dev_id = self.db.get_or_create_deviation(node_id)
            new_id = self.db.copy_cause(cid, dev_id)
            if new_id:
                self.refresh(CAUSE_T, new_id)
                self.structure_changed.emit()

        elif ct == CONS_T:
            cause_id = self._resolve_cause_id(type_, id_)
            if not cause_id:
                return
            new_id = self.db.copy_consequence(cid, cause_id)
            if new_id:
                self.refresh(CONS_T, new_id)
                self.structure_changed.emit()

        elif ct == SG_T:
            # Resolve consequence
            cons_id = None
            if type_ == CONS_T:
                cons_id = id_
            elif type_ == SG_T:
                sg = self.db.get_safeguard(id_)
                if sg:
                    cons_id = sg['consequence_id']
            if not cons_id:
                return
            new_id = self.db.copy_safeguard(cid, cons_id)
            if new_id:
                self.refresh(SG_T, new_id)
                self.structure_changed.emit()

    # ── Feature 17: keyboard navigation ───────────────────────────────────────
    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        # ── External drop: equipment marker(s) dragged from the P&ID onto a
        # deviation item (2026-08-08, see NOTES.md). Qt delivers drag/drop
        # events to the tree's VIEWPORT, not the outer QTreeWidget — see
        # the identical lesson in ScenarioTablePanel.eventFilter — so both
        # objects are accepted defensively.
        _drop_targets = (self.tree, self.tree.viewport())
        if obj in _drop_targets and event.type() == QEvent.Type.DragEnter:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:equipment'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.DragMove:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:equipment'):
                if self._deviation_item_at(event, obj) is not None:
                    event.acceptProposedAction()
                else:
                    event.ignore()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.Drop:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:equipment'):
                self._handle_equipment_drop(event, obj)
                return True

        if obj is not self.tree or event.type() != QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)
        key  = event.key()
        item = self.tree.currentItem()
        if item is None:
            return False
        type_ = item.data(0, Qt.ItemDataRole.UserRole + 1)
        id_   = item.data(0, Qt.ItemDataRole.UserRole)

        if key == Qt.Key.Key_Right:
            if item.childCount():
                item.setExpanded(True)
                self.tree.setCurrentItem(item.child(0))
            return True
        if key == Qt.Key.Key_Left:
            if item.isExpanded():
                item.setExpanded(False)
            elif item.parent():
                self.tree.setCurrentItem(item.parent())
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Add child at next level
            if type_ == NODE_T:
                self.add_cause()
            elif type_ == DEV_T and id_ is not None:
                self._add_cause_for_deviation(id_)
            elif type_ == CAUSE_T and id_ is not None:
                new_id = self.db.add_consequence(id_)
                self.refresh(CONS_T, new_id); self.structure_changed.emit()
            elif type_ == CONS_T and id_ is not None:
                new_id = self.db.add_safeguard(id_)
                self.refresh(SG_T, new_id); self.structure_changed.emit()
            return True
        if key == Qt.Key.Key_Delete and id_ is not None:
            label = {NODE_T: 'nod', DEV_T: 'avvikelse', CAUSE_T: 'orsak',
                     CONS_T: 'konsekvens', SG_T: 'safeguard'}.get(type_, 'objekt')
            if QMessageBox.question(
                    self, 'Ta bort', f'Ta bort {label}?',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            ) == QMessageBox.StandardButton.Yes:
                self._delete_item(type_, id_)
            return True
        return False

    def _event_pos_in_viewport(self, event, source_obj):
        """Drag/drop event positions are relative to whichever widget the
        event was actually delivered to — remap to viewport coordinates
        only when that was the outer tree widget, matching
        ScenarioTablePanel._handle_drop's identical fix (2026-08-08, see
        NOTES.md)."""
        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        if source_obj is self.tree:
            return self.tree.viewport().mapFrom(self.tree, pos)
        return pos

    def _deviation_item_at(self, event, source_obj):
        """Return the DEV_T tree item under the drag position, or None."""
        pos = self._event_pos_in_viewport(event, source_obj)
        target = self.tree.itemAt(pos)
        if target is None or target.data(0, Qt.ItemDataRole.UserRole + 1) != DEV_T:
            return None
        return target

    def _handle_equipment_drop(self, event, source_obj):
        text = event.mimeData().text()
        parts = text.split(':')
        if len(parts) < 3:
            event.ignore(); return
        ids_field = parts[2]
        try:
            marker_ids = [int(s) for s in ids_field.split(',') if s.strip()]
        except ValueError:
            event.ignore(); return
        if not marker_ids:
            event.ignore(); return

        target = self._deviation_item_at(event, source_obj)
        if target is None:
            event.ignore(); return
        dev_id = target.data(0, Qt.ItemDataRole.UserRole)
        if dev_id is None:
            event.ignore(); return

        self.equipment_dropped_on_deviation.emit(dev_id, marker_ids)
        event.acceptProposedAction()

    def _add_cause_for_deviation(self, dev_id):
        self._open_cause_picker_for_deviation(dev_id)

    def _delete_item(self, type_, id_):
        if type_ == NODE_T:      self.db.delete_node(id_)
        elif type_ == DEV_T:     self.db.delete_deviation(id_)
        elif type_ == CAUSE_T:   self.db.delete_cause(id_)
        elif type_ == CONS_T:    self.db.delete_consequence(id_)
        elif type_ == SG_T:      self.db.delete_safeguard(id_)
        self.refresh(); self.structure_changed.emit()


# ══════════════════════════════════════════════════════════════════════════════
# SCENARIO TABLE PANEL  (6-column bottom panel)
# ══════════════════════════════════════════════════════════════════════════════

_CAUSE_OBJ_W = 64   # width of the object-tag zone on the left of Orsak cells

# Icon size used in the obj-zone (square, left part of _CAUSE_OBJ_W)
_EQUIP_ICON_SZ = 20


def _icon_category(comp_type: str) -> str:
    """Map any object-type name (from the DB-backed standard_objects list,
    which is more granular than the drawing categories below) onto the
    fixed set of icon categories _draw_equip_icon knows how to draw.
    """
    if not comp_type:
        return ''
    t = comp_type.lower()
    if 'säkerhetsventil' in t or 'sprängbleck' in t:
        return 'Säkerhetsventil (PSV)'
    if 'ventil' in t:
        return 'Ventil'
    if 'pump' in t:
        return 'Pump'
    if 'kompressor' in t or 'fläkt' in t:
        return 'Kompressor'
    if 'tank' in t or 'kärl' in t or 'kolonn' in t:
        return 'Tank / Kärl'
    if 'värmeväxlare' in t or 'kylare' in t or 'värmare' in t:
        return 'Värmeväxlare'
    if 'rörledning' in t or 'slang' in t:
        return 'Rörledning'
    if 'instrument' in t or 'sensor' in t:
        return 'Instrument / Sensor'
    return ''


def _draw_equip_icon(painter, rect, comp_type):
    """Draw a colorful QPainter icon for the given equipment type.

    rect  -- the QRect to draw inside (icon is centred/fitted)
    comp_type -- a standard_objects name (or empty / unknown); mapped onto
    a drawing category via _icon_category() before matching below.
    """
    original_empty = not comp_type
    comp_type = _icon_category(comp_type) or comp_type
    sz    = min(rect.width(), rect.height()) - 4
    sz    = max(6, sz)
    cx    = float(rect.center().x())
    cy    = float(rect.center().y())
    half  = sz / 2.0

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    if comp_type == 'Pump':
        # Blue filled circle with a white rotation arrow inside
        body = QColor('#2980b9')
        dark = QColor('#1a5276')
        painter.setBrush(QBrush(body))
        painter.setPen(QPen(dark, 1.2))
        painter.drawEllipse(QPointF(cx, cy), half, half)
        # White curved arrow (two small arcs simulated as a triangle near top-right)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255, 220)))
        # Impeller: three short radial lines
        pen_imp = QPen(QColor(255, 255, 255, 220), max(1.0, sz * 0.12))
        pen_imp.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_imp)
        for angle_deg in (0, 120, 240):
            angle = math.radians(angle_deg)
            r_in  = half * 0.25
            r_out = half * 0.65
            painter.drawLine(
                QPointF(cx + r_in  * math.cos(angle), cy + r_in  * math.sin(angle)),
                QPointF(cx + r_out * math.cos(angle), cy + r_out * math.sin(angle)),
            )

    elif comp_type == 'Ventil':
        # Orange bowtie / valve body
        col  = QColor('#e67e22')
        dark = QColor('#935116')
        half_v = half * 0.85
        pts = [
            QPointF(cx - half_v, cy - half_v),
            QPointF(cx + half_v, cy + half_v),
            QPointF(cx + half_v, cy - half_v),
            QPointF(cx - half_v, cy + half_v),
        ]
        painter.setBrush(QBrush(col))
        painter.setPen(QPen(dark, 1.2))
        painter.drawPolygon(QPolygonF(pts))
        # Stem line upward
        painter.setPen(QPen(dark, max(1.0, sz * 0.12)))
        painter.drawLine(QPointF(cx, cy - half_v), QPointF(cx, cy - half_v - half * 0.4))
        # Handwheel circle at top of stem
        painter.setPen(QPen(dark, 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy - half_v - half * 0.4), half * 0.25, half * 0.25)

    elif comp_type == 'Kompressor':
        # Green diamond-ish rotary symbol
        col  = QColor('#27ae60')
        dark = QColor('#1a6b3c')
        painter.setBrush(QBrush(col))
        painter.setPen(QPen(dark, 1.2))
        painter.drawEllipse(QPointF(cx, cy), half * 0.9, half * 0.9)
        # Inner × marks
        pen_x = QPen(QColor(255, 255, 255, 200), max(1.0, sz * 0.14))
        pen_x.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_x)
        off = half * 0.5
        painter.drawLine(QPointF(cx - off, cy - off), QPointF(cx + off, cy + off))
        painter.drawLine(QPointF(cx + off, cy - off), QPointF(cx - off, cy + off))

    elif comp_type == 'Tank / Kärl':
        # Gray rounded rectangle (vessel)
        col  = QColor('#7f8c8d')
        dark = QColor('#2c3e50')
        rw   = half * 0.85
        rh   = half * 0.95
        painter.setBrush(QBrush(col))
        painter.setPen(QPen(dark, 1.2))
        painter.drawRoundedRect(
            QRectF(cx - rw, cy - rh, rw * 2, rh * 2), 3.0, 3.0)
        # Horizontal seam line
        painter.setPen(QPen(QColor(255, 255, 255, 150), 1.0))
        painter.drawLine(QPointF(cx - rw + 2, cy), QPointF(cx + rw - 2, cy))

    elif comp_type == 'Värmeväxlare':
        # Red/blue split rectangle with heat exchange arrows
        rw  = half * 0.85
        rh  = half * 0.8
        painter.setBrush(QBrush(QColor('#e74c3c')))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(QRectF(cx - rw, cy - rh, rw * 2, rh))
        painter.setBrush(QBrush(QColor('#2980b9')))
        painter.drawRect(QRectF(cx - rw, cy,      rw * 2, rh))
        painter.setPen(QPen(QColor('#2c3e50'), 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(cx - rw, cy - rh, rw * 2, rh * 2))
        # Divider
        painter.setPen(QPen(QColor('#2c3e50'), 1.0))
        painter.drawLine(QPointF(cx - rw, cy), QPointF(cx + rw, cy))

    elif comp_type == 'Rörledning':
        # Teal horizontal pipe with end flanges
        col  = QColor('#16a085')
        dark = QColor('#0e6655')
        rh   = half * 0.28
        painter.setBrush(QBrush(col))
        painter.setPen(QPen(dark, 1.0))
        painter.drawRect(QRectF(cx - half * 0.9, cy - rh, half * 1.8, rh * 2))
        # Flanges
        flange_w = max(1.5, sz * 0.1)
        for fx in (cx - half * 0.9, cx + half * 0.9):
            painter.drawLine(QPointF(fx, cy - rh * 1.8), QPointF(fx, cy + rh * 1.8))
        # Arrow head
        painter.setBrush(QBrush(QColor('#ffffff')))
        painter.setPen(Qt.PenStyle.NoPen)
        ax = cx + half * 0.3
        aw = half * 0.25
        ah = rh * 1.4
        arrow = QPolygonF([
            QPointF(ax,       cy),
            QPointF(ax - aw,  cy - ah),
            QPointF(ax - aw,  cy + ah),
        ])
        painter.drawPolygon(arrow)

    elif comp_type == 'Instrument / Sensor':
        # White circle with blue border (ISA instrument bubble) + letter I
        painter.setBrush(QBrush(QColor('#ffffff')))
        painter.setPen(QPen(QColor('#2471a3'), 1.8))
        painter.drawEllipse(QPointF(cx, cy), half * 0.85, half * 0.85)
        # Dashed line inside (indicates field-mounted)
        pen_d = QPen(QColor('#2471a3'), 1.0)
        pen_d.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen_d)
        painter.drawLine(QPointF(cx - half * 0.6, cy), QPointF(cx + half * 0.6, cy))

    elif comp_type == 'Säkerhetsventil (PSV)':
        # Purple filled diamond with upward spike
        col  = QColor('#8e44ad')
        dark = QColor('#6c3483')
        hd   = half * 0.75
        diamond = QPolygonF([
            QPointF(cx,       cy - hd),
            QPointF(cx + hd,  cy),
            QPointF(cx,       cy + hd),
            QPointF(cx - hd,  cy),
        ])
        painter.setBrush(QBrush(col))
        painter.setPen(QPen(dark, 1.2))
        painter.drawPolygon(diamond)
        # Discharge spike upward
        painter.setPen(QPen(dark, max(1.5, sz * 0.13)))
        painter.drawLine(QPointF(cx, cy - hd), QPointF(cx, cy - hd - half * 0.45))
        # Small horizontal discharge bar at top
        painter.drawLine(QPointF(cx - half * 0.3, cy - hd - half * 0.45),
                         QPointF(cx + half * 0.3, cy - hd - half * 0.45))

    else:
        # Generic: gray circle with '?' — Övrigt or unknown
        painter.setBrush(QBrush(QColor('#bdc3c7')))
        painter.setPen(QPen(QColor('#7f8c8d'), 1.2))
        painter.drawEllipse(QPointF(cx, cy), half * 0.85, half * 0.85)
        if original_empty:
            # '+' — not yet set
            pen_plus = QPen(QColor('#7f8c8d'), max(1.2, sz * 0.13))
            pen_plus.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen_plus)
            painter.drawLine(QPointF(cx - half * 0.45, cy), QPointF(cx + half * 0.45, cy))
            painter.drawLine(QPointF(cx, cy - half * 0.45), QPointF(cx, cy + half * 0.45))
        else:
            # '?' mark
            f = QFont()
            f.setPointSize(max(5, int(sz * 0.45)))
            f.setBold(True)
            painter.setFont(f)
            painter.setPen(QColor('#555'))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, '?')

    painter.restore()


class StandardCausesPickerPopup(QDialog):
    """Orsaksväljare: Avvikelse (header) → Objekt (vänster) → Orsaker (höger).

    Visas vid P&ID-klick och från knappen 'Orsak' ovanför P&ID-vyn.
    Innehåller sökfält, tag-ID-fält, frekvensvisning och fritext-fallback.
    """
    cause_picked = pyqtSignal(str, object)   # (description, frequency_or_None)

    _OBJ_BTN_STYLE = (
        "QPushButton { text-align:left; padding:2px 6px; border:1px solid #E2E3E1;"
        " border-radius:3px; background:#FAFAFA; font-size:10px; }"
        "QPushButton:hover { background:#F5F5F3; border-color:#CFD1CE; }"
        "QPushButton:checked { background:#17191C; color:white; border-color:#17191C;"
        " font-weight:bold; }")

    def __init__(self, db, deviation_id: int, deviation_name: str = '',
                 comp_type: str = '', initial_tag: str = '',
                 node_id: int = None, parent=None):
        super().__init__(parent)
        self._db = db
        self._dev_id = deviation_id   # standard_deviations.id
        self.selected_node_dev_id = None  # deviations.id to use in add_cause()
        self._dev_items = []   # [(deviations.id, desc, standard_deviations.id)]
        self._dev_combo = None

        # Resolve selected_node_dev_id from node's deviations
        if node_id is not None:
            for d in db.deviations(node_id):
                std_row = db.conn.execute(
                    "SELECT id FROM standard_deviations WHERE description=? COLLATE NOCASE LIMIT 1",
                    (d['description'],)).fetchone()
                self._dev_items.append((d['id'], d['description'],
                                        std_row[0] if std_row else None))
            # Pre-select by deviation_name
            match = next((item for item in self._dev_items
                          if item[1] == deviation_name), None)
            if match:
                self.selected_node_dev_id = match[0]
                self._dev_id = match[2]
            elif self._dev_items:
                self.selected_node_dev_id = self._dev_items[0][0]
                self._dev_id = self._dev_items[0][2]
                deviation_name = self._dev_items[0][1]

        self.setWindowTitle("Lägg till orsak")
        self.setMinimumWidth(640)
        self.setMinimumHeight(CONFIG['H_PANEL_MIN_XL'])
        main = QVBoxLayout(self)
        main.setSpacing(0)
        main.setContentsMargins(0, 0, 0, 0)

        # ── Coloured header band ──────────────────────────────────────────────
        hdr_w = QWidget()
        hdr_w.setStyleSheet("background:#17191C;")
        hdr_l = QVBoxLayout(hdr_w)
        hdr_l.setContentsMargins(12, 8, 12, 8)
        hdr_l.setSpacing(3)
        title = QLabel("Lägg till orsak på P&ID")
        title.setStyleSheet("color:white; font-size:13px; font-weight:bold;")
        hdr_l.addWidget(title)
        sub = QLabel(f"Avvikelse: {deviation_name}")
        sub.setStyleSheet("color:#D7E3FA; font-size:10px;")
        hdr_l.addWidget(sub)
        main.addWidget(hdr_w)

        body = QWidget()
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(10, 10, 10, 10)
        body_l.setSpacing(8)
        main.addWidget(body, 1)

        # ── Deviation picker (shown when node has multiple deviations) ─────────
        if len(self._dev_items) > 1:
            dev_row = QHBoxLayout()
            dev_row.setSpacing(6)
            dev_lbl = QLabel("Avvikelse:")
            dev_lbl.setStyleSheet("font-size:10px; color:#555; font-weight:bold;")
            dev_row.addWidget(dev_lbl)
            self._dev_combo = QComboBox()
            for _, desc, _ in self._dev_items:
                self._dev_combo.addItem(desc)
            cur_idx = next((i for i, (nid, _, _) in enumerate(self._dev_items)
                            if nid == self.selected_node_dev_id), 0)
            self._dev_combo.setCurrentIndex(cur_idx)
            self._dev_combo.currentIndexChanged.connect(self._on_dev_combo_changed)
            dev_row.addWidget(self._dev_combo, 1)
            body_l.addLayout(dev_row)

        # ── Tag + search row ──────────────────────────────────────────────────
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        tag_lbl = QLabel("Tag-ID:")
        tag_lbl.setStyleSheet("font-size:10px; color:#555;")
        top_row.addWidget(tag_lbl)
        self._tag_edit = QLineEdit(initial_tag)
        self._tag_edit.setPlaceholderText("t.ex. P-101")
        self._tag_edit.setMaximumWidth(110)
        # Autocomplete from equipment catalog
        completer = _make_tag_completer(db, self)
        if completer:
            self._tag_edit.setCompleter(completer)
        self._tag_edit.textChanged.connect(self._on_tag_id_changed)
        self._tag_edit.textChanged.connect(self._update_tag_style)
        self._update_tag_style(initial_tag)
        top_row.addWidget(self._tag_edit)

        top_row.addSpacing(8)
        search_lbl = QLabel("Sök:")
        search_lbl.setStyleSheet("font-size:10px; color:#555;")
        top_row.addWidget(search_lbl)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Filtrera orsaker…")
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._on_search)
        top_row.addWidget(self._search_edit, 1)
        body_l.addLayout(top_row)

        # ── Main split: objects (left) + causes (right) ───────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: object buttons (scrollable)
        obj_frame = QFrame()
        obj_frame.setFrameShape(QFrame.Shape.StyledPanel)
        obj_frame.setStyleSheet(
            "QFrame { border:1px solid #e5e7eb; border-radius:6px; background:#f9fafb; }")
        obj_fl = QVBoxLayout(obj_frame)
        obj_fl.setContentsMargins(4, 4, 4, 4)
        obj_fl.setSpacing(2)
        obj_hdr_row = QHBoxLayout()
        obj_hdr = QLabel("<b>Objekttyp</b>")
        obj_hdr.setStyleSheet("font-size:10px; color:#8D9299; padding:2px 4px;")
        obj_hdr_row.addWidget(obj_hdr)
        obj_hdr_row.addStretch()
        btn_add_obj = QPushButton("+ Ny")
        btn_add_obj.setFixedHeight(CONFIG['H_BADGE'])
        btn_add_obj.setStyleSheet(
            "QPushButton{font-size:9px;padding:1px 5px;border:1px solid #CFD1CE;"
            "border-radius:3px;background:#F5F5F3;color:#17191C;}"
            "QPushButton:hover{background:#E8E9E6;}")
        btn_add_obj.setToolTip("Lägg till ny objekttyp (t.ex. Transportör)")
        btn_add_obj.clicked.connect(self._add_object_type)
        obj_hdr_row.addWidget(btn_add_obj)
        obj_fl.addLayout(obj_hdr_row)
        obj_scroll = QScrollArea()
        obj_scroll.setWidgetResizable(True)
        obj_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._obj_inner = QWidget()
        self._obj_inner_l = QVBoxLayout(self._obj_inner)
        self._obj_inner_l.setContentsMargins(0, 0, 0, 0)
        self._obj_inner_l.setSpacing(1)
        obj_scroll.setWidget(self._obj_inner)
        obj_fl.addWidget(obj_scroll)
        self._obj_btn_group = []   # list of QPushButton
        splitter.addWidget(obj_frame)

        # Right: cause list
        cause_frame = QFrame()
        cause_frame.setFrameShape(QFrame.Shape.StyledPanel)
        cause_frame.setStyleSheet(
            "QFrame { border:1px solid #e5e7eb; border-radius:6px; background:white; }")
        cause_fl = QVBoxLayout(cause_frame)
        cause_fl.setContentsMargins(4, 4, 4, 4)
        cause_fl.setSpacing(2)
        self._cause_hdr = QLabel("<b>Orsaker</b>")
        self._cause_hdr.setStyleSheet("font-size:10px; color:#8D9299; padding:2px 4px;")
        cause_fl.addWidget(self._cause_hdr)
        self._cause_list = QListWidget()
        self._cause_list.setAlternatingRowColors(False)
        self._cause_list.setWordWrap(True)
        self._cause_list.setSpacing(1)
        self._cause_list.setStyleSheet(
            "QListWidget { border:none; }"
            "QListWidget::item { padding:6px 10px; border-bottom:1px solid #f3f4f6; }"
            "QListWidget::item:selected { background:#E6ECFA; color:#17191C;"
            "  border-left:3px solid #17191C; font-weight:bold; }"
            "QListWidget::item:hover:!selected { background:#F5F5F3; }")
        cause_fl.addWidget(self._cause_list)
        splitter.addWidget(cause_frame)
        splitter.setSizes([200, 420])
        body_l.addWidget(splitter, 1)

        # ── Free-text + bottom row ────────────────────────────────────────────
        ft_frame = QFrame()
        ft_frame.setStyleSheet(
            "QFrame { background:#f8fafc; border:1px solid #e2e8f0; border-radius:4px; }")
        ft_l = QHBoxLayout(ft_frame)
        ft_l.setContentsMargins(8, 4, 8, 4)
        ft_l.setSpacing(6)
        ft_lbl = QLabel("Fritext:")
        ft_lbl.setStyleSheet("font-size:10px; color:#64748b;")
        ft_l.addWidget(ft_lbl)
        self._ft_edit = QLineEdit()
        self._ft_edit.setPlaceholderText("Ange orsak manuellt om inget standardalternativ passar…")
        self._ft_edit.setStyleSheet("border:none; background:transparent;")
        ft_l.addWidget(self._ft_edit, 1)
        body_l.addWidget(ft_frame)

        # Buttons
        btn_row = QHBoxLayout()
        self._ok_btn = QPushButton("✓  Välj orsak")
        self._ok_btn.setDefault(True)
        self._ok_btn.setMinimumHeight(CONFIG['H_BTN_OK'])
        self._ok_btn.setStyleSheet(
            "QPushButton { background:#17191C; color:white; border:none;"
            " border-radius:5px; padding:6px 20px; font-weight:bold; font-size:11px; }"
            "QPushButton:hover { background:#2A2E34; }"
            "QPushButton:pressed { background:#0B0C0E; }")
        self._ok_btn.clicked.connect(self._pick_selected)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.setMinimumHeight(CONFIG['H_BTN_OK'])
        cancel_btn.setStyleSheet(
            "QPushButton { background:#FFFFFF; color:#17191C; border:1px solid #E2E3E1;"
            " border-radius:5px; padding:6px 16px; }"
            "QPushButton:hover { background:#F5F5F3; }")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._ok_btn)
        body_l.addLayout(btn_row)

        self._cause_list.itemDoubleClicked.connect(self._pick_selected)
        self._ft_edit.returnPressed.connect(self._pick_selected)
        self._search_edit.installEventFilter(self)

        # Derive preselect from tag first (most reliable), fall back to comp_type
        preselect = _lookup_comp_type_for_tag(initial_tag, db) if initial_tag else ''
        if not preselect:
            preselect = comp_type
        self._initial_comp_type = preselect
        self._populate_objects(preselect)

    # ── Object buttons ────────────────────────────────────────────────────────
    def _on_tag_id_changed(self, text: str):
        """Tag-ID field changed — look up type from smart recognition and pre-select."""
        if not text.strip():
            return
        comp = _lookup_comp_type_for_tag(text.strip(), self._db)
        if comp:
            self._initial_comp_type = comp
            self._select_obj_by_type(comp)

    def _update_tag_style(self, text: str):
        """Highlight tag field orange when empty — tag is required for smart learning."""
        if text.strip():
            self._tag_edit.setStyleSheet('')
            self._tag_edit.setToolTip('')
        else:
            self._tag_edit.setStyleSheet(
                'QLineEdit { background:#fff7ed; border:1px solid #f97316; border-radius:3px; }')
            self._tag_edit.setToolTip(
                'Fyll i Tag-ID för att programmet ska lära sig objekttypen.\n'
                'Utan tag lagras ingen information i Smart igenkänning.')

    def _select_obj_by_type(self, comp_type: str):
        """Check the object button that matches comp_type (case-insensitive substring).
        If the matching button doesn't exist yet (filtered out by deviation),
        repopulates the list with all objects so the button is present.
        """
        if not comp_type:
            return
        # Try existing buttons first
        for btn in self._obj_btn_group:
            if _obj_type_matches(comp_type, btn.property('obj_name') or ''):
                if not btn.isChecked():
                    for b in self._obj_btn_group:
                        b.setChecked(False)
                    btn.setChecked(True)
                    self._load_causes_for_obj(
                        btn.property('obj_id'), btn.property('obj_name'))
                return
        # Button not found — expand the list to all objects and try again
        self._populate_objects(comp_type)

    def _on_dev_combo_changed(self, idx):
        """Deviation combo changed — update selected IDs and reload object list."""
        if 0 <= idx < len(self._dev_items):
            self.selected_node_dev_id = self._dev_items[idx][0]
            self._dev_id              = self._dev_items[idx][2]
        self._populate_objects(getattr(self, '_initial_comp_type', ''))

    def _populate_objects(self, preselect_comp: str = ''):
        # Remove old buttons
        for btn in self._obj_btn_group:
            btn.setParent(None)
        self._obj_btn_group.clear()

        if self._dev_id is None:
            objs = self._db.standard_objects()
        else:
            objs = self._db.objects_for_deviation(self._dev_id)

        if not objs:
            objs = self._db.standard_objects()

        # If a preselect type is requested but absent from the filtered list,
        # fall back to all objects so the button actually exists.
        if preselect_comp:
            if not any(_obj_type_matches(preselect_comp, o['name']) for o in objs):
                objs = self._db.standard_objects()

        def _make_btn(obj):
            btn = QPushButton(obj['name'])
            btn.setCheckable(True)
            btn.setStyleSheet(self._OBJ_BTN_STYLE)
            btn.setProperty('obj_id',   obj['id'])
            btn.setProperty('obj_name', obj['name'])
            btn.clicked.connect(partial(self._on_obj_btn, btn))
            return btn

        sel_btn = None
        for obj in objs:
            btn = _make_btn(obj)
            self._obj_inner_l.addWidget(btn)
            self._obj_btn_group.append(btn)
            if preselect_comp and sel_btn is None and _obj_type_matches(preselect_comp, obj['name']):
                sel_btn = btn
        self._obj_inner_l.addStretch()

        # Only pre-select if smart recognition produced a match.
        # If nothing was learned for this prefix, leave all buttons unchecked
        # so the user clearly sees there is no suggestion — no random default.
        if sel_btn:
            sel_btn.setChecked(True)
            self._load_causes_for_obj(sel_btn.property('obj_id'), sel_btn.property('obj_name'))

    def _on_obj_btn(self, clicked_btn):
        for btn in self._obj_btn_group:
            btn.setChecked(btn is clicked_btn)
        self._search_edit.clear()
        self._load_causes_for_obj(clicked_btn.property('obj_id'),
                                   clicked_btn.property('obj_name'))

    def _add_object_type(self):
        """Add a new standard object type (e.g. Transportör) to the database."""
        name, ok = QInputDialog.getText(
            self, "Ny objekttyp",
            "Ange namn på den nya objekttypen\n(t.ex. Transportör, Kross, Silovinagg):")
        if not ok or not name.strip():
            return
        name = name.strip()
        try:
            # Check if already exists
            existing = self._db.conn.execute(
                "SELECT id FROM standard_objects WHERE LOWER(name)=LOWER(?)",
                (name,)).fetchone()
            if existing:
                new_id = existing[0]
            else:
                new_id = self._db.add_standard_object(name)
            self._db.commit()
        except Exception as e:
            QMessageBox.warning(self, "Fel", f"Kunde inte lägga till objekttyp:\n{e}")
            return
        # Re-populate to include the new type and select it
        self._populate_objects(name)

    def _load_causes_for_obj(self, obj_id, obj_name):
        self._cause_list.clear()
        self._cause_hdr.setText(f"<b>Orsaker</b> — {obj_name}")
        filt = self._search_edit.text().strip().lower()
        # When no standard deviation is matched, show ALL causes for this object
        if self._dev_id is None:
            rows = self._db.conn.execute(
                "SELECT sc.id, sc.description, sc.frequency "
                "FROM standard_causes sc "
                "WHERE sc.object_id=? ORDER BY sc.deviation_id, sc.sort_order",
                (obj_id,)).fetchall()
            causes = [dict(r) for r in rows]
        else:
            causes = self._db.standard_causes_for_object(self._dev_id, obj_id)
        for c in causes:
            freq  = c.get('frequency')
            label = c['description']
            if filt and filt not in label.lower():
                continue
            # Show frequency visibly in the list item text
            if freq is not None:
                f_level = freq_to_f_level(freq) if freq else None
                freq_lbl = freq_axis_label(f_level) if f_level is not None else ''
                display = f"{label}  [{freq:g}/år{(' · ' + freq_lbl) if freq_lbl else ''}]"
            else:
                display = label
            ci = QListWidgetItem(display)
            ci.setData(Qt.ItemDataRole.UserRole + 1, c['description'])
            ci.setData(Qt.ItemDataRole.UserRole + 2, freq)
            if freq is not None:
                ci.setForeground(QColor('#17191C'))
            self._cause_list.addItem(ci)
        if self._cause_list.count():
            self._cause_list.setCurrentRow(0)

    # ── Search ────────────────────────────────────────────────────────────────
    def _on_search(self, text):
        filt = text.strip().lower()
        if not filt:
            # Restore current object
            for btn in self._obj_btn_group:
                if btn.isChecked():
                    self._load_causes_for_obj(btn.property('obj_id'), btn.property('obj_name'))
                    return
            return
        self._cause_list.clear()
        self._cause_hdr.setText("<b>Sökresultat</b>")
        for obj in self._db.objects_for_deviation(self._dev_id):
            for c in self._db.standard_causes_for_object(self._dev_id, obj['id']):
                if filt in c['description'].lower():
                    freq  = c.get('frequency')
                    ci = QListWidgetItem(f"{c['description']}  ·  {obj['name']}")
                    ci.setData(Qt.ItemDataRole.UserRole + 1, c['description'])
                    ci.setData(Qt.ItemDataRole.UserRole + 2, freq)
                    ci.setForeground(QColor('#374151'))
                    self._cause_list.addItem(ci)
        if self._cause_list.count():
            self._cause_list.setCurrentRow(0)

    # ── Keyboard shortcuts ────────────────────────────────────────────────────
    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if event.type() == QEvent.Type.KeyPress:
            if obj is self._search_edit:
                if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._cause_list.setFocus()
                    if self._cause_list.count():
                        self._cause_list.setCurrentRow(0)
                    if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) \
                            and self._cause_list.currentItem():
                        self._pick_selected()
                    return True
            if obj is self._cause_list:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._pick_selected()
                    return True
        return super().eventFilter(obj, event)

    # ── Pick ──────────────────────────────────────────────────────────────────
    def _pick_selected(self):
        try:
            ft = self._ft_edit.text().strip()
            if ft:
                # Ask if the user wants to save this as a standard cause
                self._maybe_save_as_standard(ft)
                self.cause_picked.emit(ft, None)
                self.accept(); return
            cause_item = self._cause_list.currentItem()
            if not cause_item: return
            desc = cause_item.data(Qt.ItemDataRole.UserRole + 1) or cause_item.text()
            freq = cause_item.data(Qt.ItemDataRole.UserRole + 2)
            self.cause_picked.emit(desc, freq)
            self.accept()
        except Exception as e:
            import traceback
            QMessageBox.critical(self, "Fel i orsakspickern",
                                 f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")

    def _maybe_save_as_standard(self, description: str):
        """Ask if the free-text cause should be saved as a standard cause with frequency."""
        checked = next((b for b in self._obj_btn_group if b.isChecked()), None)
        obj_id   = checked.property('obj_id')   if checked else None
        obj_name = checked.property('obj_name') if checked else ''
        _maybe_save_as_standard_cause(self, self._db, self._dev_id, obj_id, obj_name, description)


class CauseObjectPopup(QDialog):
    """Combined popup: set Tag-ID + equipment type, then pick a standard cause."""
    committed = pyqtSignal(str, str, str, object)  # (comp_type, comp_tag, description, freq|None)

    def __init__(self, comp_type: str, comp_tag: str, db,
                 dev_description=None, current_description='',
                 node_id=None, deviation_id=None, parent=None):
        super().__init__(parent)
        self._db              = db
        self._dev_description = dev_description
        self._deviation_id    = deviation_id   # preferred: used for new hierarchy lookup
        self._dev_combo       = None
        self._cause_buttons   = []   # list of (QRadioButton, description, freq)
        self._freq_overrides  = {}   # QRadioButton → custom freq (overrides standard)

        self.setWindowTitle("Objekt / Standardorsak")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setMinimumWidth(CONFIG['W_PANEL_MIN'])
        self.setMaximumWidth(340)

        _small = "font-size:10px;"
        _btn_style = ("QPushButton{font-size:10px; padding:2px 10px;"
                      "border:1px solid #E2E3E1; border-radius:3px; background:#FFFFFF;}"
                      "QPushButton:hover{background:#F5F5F3;}"
                      "QPushButton:default{background:#17191C; color:white; border-color:#17191C;}"
                      "QPushButton:default:hover{background:#2A2E34;}")

        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 8, 8, 8)

        # ── Header: icon + title ──────────────────────────────────────────────
        hdr = QHBoxLayout()
        hdr.setSpacing(6)
        self._icon_lbl = QLabel()
        self._icon_lbl.setFixedSize(22, 22)
        hdr.addWidget(self._icon_lbl)
        title = QLabel("<b>Orsak på P&amp;ID</b>")
        title.setStyleSheet("font-size:11px; color:#8D9299;")
        hdr.addWidget(title)
        hdr.addStretch()
        layout.addLayout(hdr)

        # ── Form: (Avvikelse) + Tag-ID + Type ────────────────────────────────
        form = QFormLayout()
        form.setSpacing(3)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Optional deviation picker — only shown when node_id is supplied
        if node_id is not None and db:
            self._dev_combo = QComboBox()
            self._dev_combo.setFixedHeight(22)
            self._dev_combo.setStyleSheet(_small)
            self._dev_combo.setMaxVisibleItems(12)
            try:
                devs = db.deviations(node_id)
            except Exception:
                devs = []
            for d in devs:
                self._dev_combo.addItem(d['description'][:70], d['id'])
            if deviation_id:
                for i in range(self._dev_combo.count()):
                    if self._dev_combo.itemData(i) == deviation_id:
                        self._dev_combo.setCurrentIndex(i)
                        break
            dev_lbl = QLabel("Avvikelse:")
            dev_lbl.setStyleSheet(_small)
            form.addRow(dev_lbl, self._dev_combo)
            # Keep _dev_description in sync and refresh cause list on change
            if self._dev_combo.count() > 0:
                self._dev_description = self._dev_combo.currentText()
            def _on_dev_changed():
                self._dev_description = self._dev_combo.currentText() or None
                self._populate_type_combo(self._type_cb.currentText())
                self._rebuild_causes(self._type_cb.currentText())
            self._dev_combo.currentIndexChanged.connect(_on_dev_changed)

        self._tag_edit = QLineEdit(comp_tag)
        self._tag_edit.setPlaceholderText("t.ex. P-101")
        self._tag_edit.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._tag_edit.setStyleSheet(_small)
        if db:
            completer = _make_tag_completer(db, self)
            if completer:
                self._tag_edit.setCompleter(completer)
        tag_lbl = QLabel("Tag:")
        tag_lbl.setStyleSheet(_small)
        form.addRow(tag_lbl, self._tag_edit)

        self._type_cb = QComboBox()
        self._type_cb.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._type_cb.setStyleSheet(_small)
        self._populate_type_combo(comp_type)
        typ_lbl = QLabel("Typ:")
        typ_lbl.setStyleSheet(_small)
        form.addRow(typ_lbl, self._type_cb)
        layout.addLayout(form)

        # ── Thin separator ────────────────────────────────────────────────────
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#e0e0e0; margin:0px;")
        sep.setFixedHeight(CONFIG['H_SEP_LINE'])
        layout.addWidget(sep)

        # ── Standard causes section ───────────────────────────────────────────
        self._causes_header = QLabel()
        self._causes_header.setStyleSheet("color:#777; font-size:9px;")
        layout.addWidget(self._causes_header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setMaximumHeight(150)
        layout.addWidget(self._scroll)

        self._btn_group = QButtonGroup(self)
        self._btn_group.setExclusive(True)

        # Freetext radio — always the last option
        self._freetext_radio = QRadioButton("Fritext:")
        self._freetext_radio.setStyleSheet(_small)
        self._freetext_edit  = QLineEdit(current_description)
        self._freetext_edit.setPlaceholderText("Beskriv orsaken…")
        self._freetext_edit.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._freetext_edit.setStyleSheet(_small)
        self._freetext_radio.toggled.connect(
            lambda on: self._freetext_edit.setEnabled(on))

        # ── Buttons ───────────────────────────────────────────────────────────
        btns = QHBoxLayout()
        btns.setSpacing(4)
        ok  = QPushButton("OK")
        ok.setDefault(True)
        ok.setFixedHeight(CONFIG['H_CTRL_STD'])
        ok.setStyleSheet(_btn_style)
        ok.clicked.connect(self._ok)
        clr = QPushButton("Rensa")
        clr.setFixedHeight(CONFIG['H_CTRL_STD'])
        clr.setStyleSheet(_btn_style)
        clr.clicked.connect(self._clear)
        cancel = QPushButton("Avbryt")
        cancel.setFixedHeight(CONFIG['H_CTRL_STD'])
        cancel.setStyleSheet(_btn_style)
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addStretch()
        btns.addWidget(clr)
        btns.addWidget(cancel)
        layout.addLayout(btns)

        # ── Wire signals ──────────────────────────────────────────────────────
        self._type_cb.currentTextChanged.connect(self._rebuild_causes)
        self._tag_edit.textChanged.connect(self._on_tag_changed)
        self._tag_edit.returnPressed.connect(self._ok)
        if comp_tag:
            self._on_tag_changed(comp_tag)

        # Build initial causes list (triggers icon update too)
        self._rebuild_causes(self._type_cb.currentText(), pre_select=current_description)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _populate_type_combo(self, preselect_comp: str = ''):
        """Populate the type combo from the same DB-backed standard_objects
        list StandardCausesPickerPopup uses, instead of a hardcoded list —
        so both dialogs offer the same object-type vocabulary.
        """
        self._type_cb.blockSignals(True)
        self._type_cb.clear()
        self._type_cb.addItem('')
        objs = []
        if self._db is not None:
            try:
                dev_id = self._deviation_id
                if dev_id is None and self._dev_description:
                    r = self._db.conn.execute(
                        "SELECT id FROM standard_deviations WHERE description=? LIMIT 1",
                        (self._dev_description,)).fetchone()
                    if r:
                        dev_id = r[0]
                objs = self._db.objects_for_deviation(dev_id) if dev_id is not None else []
                if not objs:
                    objs = self._db.standard_objects()
            except Exception:
                objs = []
        for o in objs:
            self._type_cb.addItem(o['name'])

        idx = -1
        if preselect_comp:
            for i in range(self._type_cb.count()):
                if _obj_type_matches(preselect_comp, self._type_cb.itemText(i)):
                    idx = i
                    break
            if idx < 0:
                # Not in the standard list (e.g. legacy free-typed value) —
                # keep it selectable rather than silently discarding it.
                self._type_cb.addItem(preselect_comp)
                idx = self._type_cb.count() - 1
        self._type_cb.setCurrentIndex(max(0, idx))
        self._type_cb.blockSignals(False)

    def _resolve_dev_obj_ids(self, comp_type):
        """Resolve (deviation_id, object_id) for the given comp_type string
        against the DB-backed standard_deviations/standard_objects tables.
        Caches the resolved deviation id on self._deviation_id.
        """
        dev_id = self._deviation_id
        if dev_id is None and self._dev_description and self._db is not None:
            r = self._db.conn.execute(
                "SELECT id FROM standard_deviations WHERE description=? LIMIT 1",
                (self._dev_description,)).fetchone()
            if r:
                dev_id = r[0]
                self._deviation_id = dev_id
        obj_id = None
        if comp_type and self._db is not None:
            for o in self._db.standard_objects():
                if _obj_type_matches(comp_type, o['name']):
                    obj_id = o['id']
                    break
        return dev_id, obj_id

    def _update_icon(self, comp_type):
        px = QPixmap(22, 22)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        _draw_equip_icon(p, QRect(0, 0, 22, 22), comp_type)
        p.end()
        self._icon_lbl.setPixmap(px)

    def _on_tag_changed(self, text):
        if not self._db or not text.strip():
            return
        detected = _lookup_comp_type_for_tag(text.strip(), self._db)
        if detected:
            idx = next((i for i in range(self._type_cb.count())
                        if _obj_type_matches(detected, self._type_cb.itemText(i))), -1)
            if idx >= 0:
                self._type_cb.setCurrentIndex(idx)
                self._rebuild_causes(self._type_cb.itemText(idx))
            else:
                # Not in the current list (e.g. filtered by deviation) —
                # repopulate so the learned type is selectable.
                self._populate_type_combo(detected)
                self._rebuild_causes(self._type_cb.currentText())

    def _rebuild_causes(self, comp_type, pre_select=''):
        self._update_icon(comp_type)

        # Clear old buttons from group
        for btn, _, _ in self._cause_buttons:
            self._btn_group.removeButton(btn)
        self._cause_buttons.clear()
        self._freq_overrides.clear()
        if self._freetext_radio in self._btn_group.buttons():
            self._btn_group.removeButton(self._freetext_radio)

        # Query causes: prefer new hierarchy (deviation + object), fall back to comp_type
        rows = []
        if comp_type and self._db is not None:
            dev_id, obj_id = self._resolve_dev_obj_ids(comp_type)
            if dev_id is not None and obj_id is not None:
                rows = self._db.standard_causes_for_object(dev_id, obj_id)
            if not rows:
                rows = self._db.standard_causes_for_comp_type(comp_type, self._dev_description)
            if not rows:
                rows = self._db.standard_causes_for_comp_type(comp_type)

        _rs = "font-size:10px;"

        inner = QWidget()
        vbox  = QVBoxLayout(inner)
        vbox.setSpacing(1)
        vbox.setContentsMargins(2, 1, 2, 1)

        to_check = None   # radio to pre-select

        for r in rows:
            r = dict(r)
            freq  = r.get('frequency')
            desc  = r['description']
            radio = QRadioButton(desc)
            radio.setStyleSheet(_rs)
            self._btn_group.addButton(radio)
            self._cause_buttons.append((radio, desc, freq))

            row_w = QWidget()
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.setSpacing(4)
            row_h.addWidget(radio, stretch=1)

            if freq is not None:
                freq_str = f"{freq:.3g} /år" if freq >= 0.01 else f"{freq:.2e} /år"
                fb = QPushButton(freq_str)
                fb.setFixedHeight(CONFIG['H_BADGE'])
                fb.setStyleSheet(
                    "QPushButton{color:#17191C; background:#F5F5F3; border-radius:3px;"
                    "padding:1px 5px; font-size:10px; font-weight:bold; border:none;}"
                    "QPushButton:hover{background:#E8E9E6;}")
                fb.setToolTip("Klicka för att ange anpassad frekvens")
                # capture radio + fb in closure
                def _make_freq_handler(r=radio, btn=fb, base=freq):
                    def _handler():
                        cur = self._freq_overrides.get(r, base)
                        val, ok = QInputDialog.getDouble(
                            self, "Anpassad frekvens",
                            "Frekvens (händelser/år):",
                            cur, 0.0, 1e6, 6)
                        if ok:
                            self._freq_overrides[r] = val
                            label = f"{val:.3g} /år" if val >= 0.01 else f"{val:.2e} /år"
                            btn.setText(label)
                            btn.setStyleSheet(
                                "QPushButton{color:#7B2D00; background:#fde8cc;"
                                "border-radius:3px; padding:1px 5px;"
                                "font-size:10px; font-weight:bold; border:none;}"
                                "QPushButton:hover{background:#fbd4a0;}")
                    return _handler
                fb.clicked.connect(_make_freq_handler())
                row_h.addWidget(fb)

            vbox.addWidget(row_w)

            if pre_select and desc == pre_select:
                to_check = radio

        # Freetext option (always last)
        ft_row = QWidget()
        ft_h   = QHBoxLayout(ft_row)
        ft_h.setContentsMargins(0, 0, 0, 0)
        ft_h.setSpacing(6)
        ft_h.addWidget(self._freetext_radio)
        ft_h.addWidget(self._freetext_edit, stretch=1)
        vbox.addWidget(ft_row)
        self._btn_group.addButton(self._freetext_radio)

        vbox.addStretch()
        self._scroll.setWidget(inner)

        # Pre-select
        if to_check:
            to_check.setChecked(True)
            self._freetext_edit.setEnabled(False)
        else:
            self._freetext_radio.setChecked(True)
            self._freetext_edit.setEnabled(True)

        # Update header text
        has_std = bool(rows)
        if has_std and self._dev_description:
            self._causes_header.setText(
                f"Standardorsaker  —  {comp_type}  /  {self._dev_description}")
        elif has_std:
            self._causes_header.setText(f"Standardorsaker  —  {comp_type}")
        else:
            self._causes_header.setText("Ingen standardorsak — ange fritext")
        self._causes_header.setVisible(True)

    def _ok(self):
        comp_type = self._type_cb.currentText()
        comp_tag  = self._tag_edit.text().strip()

        desc, freq = '', None
        if self._freetext_radio.isChecked():
            desc = self._freetext_edit.text().strip()
            if desc:
                dev_id, obj_id = self._resolve_dev_obj_ids(comp_type)
                _maybe_save_as_standard_cause(self, self._db, dev_id, obj_id, comp_type, desc)
        else:
            for radio, d, f in self._cause_buttons:
                if radio.isChecked():
                    desc = d
                    freq = self._freq_overrides.get(radio, f)
                    break

        self.committed.emit(comp_type, comp_tag, desc, freq)
        self.accept()

    @property
    def selected_deviation_id(self):
        if self._dev_combo is not None:
            return self._dev_combo.currentData()
        return None

    def _clear(self):
        self.committed.emit('', '', '', None)
        self.accept()


class RRFPopup(QDialog):
    """Quick-pick popup for setting a safeguard's RRF value and type."""
    rrf_selected = pyqtSignal(int, str)   # (rrf_value, sg_type)

    def __init__(self, current_rrf: int, current_sg_type: str = 'Övrigt', parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ändra RRF")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        layout.addWidget(QLabel("<b>Risk Reduction Factor (RRF)</b>"))

        # Type selector
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Typ:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(SG_TYPES)
        idx = self._type_combo.findText(current_sg_type)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        self._type_combo.setStyleSheet("font-size:10px;")
        type_row.addWidget(self._type_combo)
        layout.addLayout(type_row)

        # Preset buttons
        presets = QHBoxLayout()
        for val in (1, 10, 100, 1000, 10000):
            btn = QPushButton(str(val))
            btn.setFixedWidth(62)
            btn.setStyleSheet(
                "QPushButton{background:#17191C;color:white;border:none;"
                "border-radius:4px;padding:5px;font-weight:bold;}"
                "QPushButton:hover{background:#2A2E34;}")
            btn.clicked.connect(partial(self._pick, val))
            presets.addWidget(btn)
        layout.addLayout(presets)

        # Custom value
        custom_row = QHBoxLayout()
        custom_row.addWidget(QLabel("Eget:"))
        self._spin = QSpinBox()
        self._spin.setRange(1, 1_000_000)
        self._spin.setValue(current_rrf)
        custom_row.addWidget(self._spin)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(partial(self._pick, self._spin.value()))
        custom_row.addWidget(ok_btn)
        layout.addLayout(custom_row)

    def _pick(self, val: int):
        self.rrf_selected.emit(val, self._type_combo.currentText())
        self.accept()


class FrequencyPickerPopup(QDialog):
    """Quick-pick popup for setting a cause's frequency: either a matrix
    F-level preset (labelled with the live-configured axis text) or an
    exact numeric events/year value.

    Mirrors RRFPopup's "preset buttons + custom spinbox" layout and
    ConsCategoryMatrixPopup's frameless small-popup styling.
    """

    # (f_level_int_or_None, numeric_freq_or_None) — exactly one is non-None.
    frequency_selected = pyqtSignal(object, object)

    def __init__(self, current_f_level=None, current_numeric_freq=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ändra frekvens")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        layout.addWidget(QLabel("<b>Frekvens</b>"))

        cfg  = get_matrix()
        cols = cfg.get('cols', 7)
        # Valid F-level range is -1 .. (cols - 2): column 0 is F=-1.
        f_levels = list(range(-1, cols - 1))

        # ── Preset buttons (wrapped grid, matrix-configured labels) ──────────
        presets = QGridLayout()
        presets.setSpacing(4)
        self._preset_btns = {}
        per_row = 4
        for i, f in enumerate(f_levels):
            btn = QPushButton(freq_axis_label_full(f))
            btn.setToolTip(freq_axis_label_full(f))
            btn.setStyleSheet(self._bstyle(f == current_f_level))
            btn.clicked.connect(partial(self._pick_preset, f))
            self._preset_btns[f] = btn
            presets.addWidget(btn, i // per_row, i % per_row)
        layout.addLayout(presets)

        # ── Custom numeric value (events/year) ───────────────────────────────
        custom_row = QHBoxLayout()
        custom_row.addWidget(QLabel("Eget (händelser/år):"))
        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(6)
        self._spin.setRange(0.0, 1_000_000.0)
        self._spin.setSingleStep(0.01)
        if current_numeric_freq is not None:
            self._spin.setValue(float(current_numeric_freq))
        self._spin.valueChanged.connect(self._update_preview_label)
        custom_row.addWidget(self._spin)
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(self._pick_numeric)
        custom_row.addWidget(ok_btn)
        layout.addLayout(custom_row)

        # ── Live F-level preview for the numeric field ───────────────────────
        self._preview_lbl = QLabel()
        self._preview_lbl.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(self._preview_lbl)
        if current_numeric_freq is not None:
            self._update_preview_label(float(current_numeric_freq))
        else:
            self._update_preview_label(self._spin.value())

        self.adjustSize()

    @staticmethod
    def _bstyle(selected: bool) -> str:
        if selected:
            return ("QPushButton{background:#17191C;color:white;border:none;"
                    "border-radius:4px;padding:5px;font-weight:bold;font-size:10px;}"
                    "QPushButton:hover{background:#2A2E34;}")
        return ("QPushButton{background:#F5F5F3;color:#17191C;border:1px solid #CFD1CE;"
                "border-radius:4px;padding:5px;font-size:10px;}"
                "QPushButton:hover{background:#E8E9E6;border:1px solid #B3B7B2;}")

    def _update_preview_label(self, val):
        f_lvl = freq_to_f_level(val) if val else -1
        self._preview_lbl.setText(f"→ {freq_axis_label_full(f_lvl)}")

    def _pick_preset(self, f_level: int):
        self.frequency_selected.emit(f_level, None)
        self.accept()

    def _pick_numeric(self):
        self.frequency_selected.emit(None, self._spin.value())
        self.accept()

    @classmethod
    def create_positioned(cls, global_pos, current_f_level=None,
                           current_numeric_freq=None, parent=None):
        """Construct the popup and position it near global_pos, clamped to
        the screen — mirrors the clamping pattern used at RRFPopup's and
        ConsCategoryMatrixPopup's call sites elsewhere in this file
        (adjustSize() → compute available screen geometry → clamp x/y).

        Callers should connect `frequency_selected` and then call
        `.exec()` themselves, exactly like the existing RRFPopup /
        ConsCategoryMatrixPopup call sites do.
        """
        popup = cls(current_f_level, current_numeric_freq, parent)
        popup.adjustSize()
        scr = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = min(global_pos.x(), scr.right() - pw)
        y = min(global_pos.y() + 4, scr.bottom() - ph)
        popup.move(max(scr.left(), x), max(scr.top(), y))
        return popup


class RiskMatrixPopup(QDialog):
    """Popup risk matrix matching the configured format in Settings."""

    selection_made = pyqtSignal(int, int)   # freq_value, cons_value

    def __init__(self, current_freq: int, current_cons: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Välj risknivå")
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        cfg       = get_matrix()
        n_cons    = cfg.get('rows', 5)
        n_freq    = cfg.get('cols', 7)
        x_lbls    = cfg.get('x_labels', [f'F{c-1}' for c in range(n_freq)])
        y_lbls    = cfg.get('y_labels', [f'C{r+1}' for r in range(n_cons)])
        colors         = cfg.get('cell_colors', [])
        cell_lbl       = cfg.get('cell_labels', [])
        cell_fg_colors = cfg.get('cell_fg_colors', [])
        freq_on_x = cfg.get('x_axis', 'frequency') == 'frequency'
        x_rev     = cfg.get('x_reversed', False)
        y_rev     = cfg.get('y_reversed', False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(4)

        hdr = QLabel("Klicka på en cell för att sätta risknivå")
        hdr.setStyleSheet("font-weight:bold; font-size:11px; padding:2px;")
        outer.addWidget(hdr)

        grid = QGridLayout()
        grid.setSpacing(0)

        # Determine display dimensions
        if freq_on_x:
            n_dcols, n_drows = n_freq, n_cons
            col_lbls, row_lbls = x_lbls, y_lbls
            corner_txt = "C \\ F"
        else:
            n_dcols, n_drows = n_cons, n_freq
            col_lbls, row_lbls = y_lbls, x_lbls
            corner_txt = "F \\ C"

        # Corner
        corner = QLabel(corner_txt)
        corner.setStyleSheet("font-size:9px; color:#666;")
        corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        corner.setFixedWidth(50)
        grid.addWidget(corner, 0, 0)

        # Column headers — respect x_rev
        for c in range(n_dcols):
            data_c = (n_dcols - 1 - c) if x_rev else c
            full   = col_lbls[data_c] if data_c < len(col_lbls) else str(data_c)
            # Short label: take first token (e.g. "F3" from "F3 – Möjlig | 10-100 år")
            short  = full.split()[0] if full.strip() else str(data_c)
            lbl = QLabel(short)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setFixedWidth(50)
            lbl.setStyleSheet("font-size:9px; font-weight:bold; padding:1px;")
            lbl.setToolTip(full)
            grid.addWidget(lbl, 0, c + 1)

        # Rows — respect y_rev
        for r in range(n_drows):
            if y_rev:
                disp_r = r
            else:
                disp_r = n_drows - 1 - r

            # Row header
            full_r = row_lbls[disp_r] if disp_r < len(row_lbls) else str(disp_r)
            short_r = full_r.split()[0] if full_r.strip() else str(disp_r)
            rl = QLabel(short_r)
            rl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            rl.setStyleSheet("font-size:9px; font-weight:bold; padding-right:4px;")
            rl.setToolTip(full_r)
            rl.setFixedWidth(50)
            grid.addWidget(rl, r + 1, 0)

            for c in range(n_dcols):
                data_c = (n_dcols - 1 - c) if x_rev else c
                # Map to (cons_idx, freq_idx)
                if freq_on_x:
                    cons_idx, freq_idx = disp_r, data_c
                else:
                    freq_idx, cons_idx = disp_r, data_c

                freq_val = freq_idx - 1   # F=-1..5 (col 0 → F=-1)
                cons_val = cons_idx + 1   # C=1..5

                try:
                    color = colors[cons_idx][freq_idx]
                    lbl   = cell_lbl[cons_idx][freq_idx]
                except (IndexError, KeyError):
                    color, lbl = '#27ae60', 'Låg'
                try:
                    fg = cell_fg_colors[cons_idx][freq_idx] or '#ffffff'
                except (IndexError, KeyError, TypeError):
                    fg = '#ffffff'

                is_current = (freq_val == current_freq and cons_val == current_cons)
                border = '3px solid #000' if is_current else '0px'

                btn = QPushButton(lbl[:4])
                btn.setFixedSize(50, 32)
                btn.setToolTip(f"F={freq_val}  C={cons_val}  →  {lbl}")
                btn.setStyleSheet(
                    f"QPushButton{{background:{color}; color:{fg};"
                    f"font-size:8px; font-weight:bold;"
                    f"border:{border}; border-radius:0px; margin:0px;}}"
                    f"QPushButton:hover{{border:2px solid #000;}}")
                btn.clicked.connect(
                    lambda _, fv=freq_val, cv=cons_val: self._pick(fv, cv))
                grid.addWidget(btn, r + 1, c + 1)

        outer.addLayout(grid)

        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        outer.addWidget(cancel_btn)

        self.adjustSize()

    def _pick(self, freq, cons):
        self.selection_made.emit(freq, cons)
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)


class ConsequenceChainDialog(QDialog):
    """Popup chain editor with QCheckBoxes — kept for legacy compatibility."""

    def __init__(self, db: Database, cons_id: int, parent=None):
        super().__init__(parent)
        self.db      = db
        self.cons_id = cons_id
        self.setWindowTitle("Konsekvenskedja")
        self.setMinimumWidth(CONFIG['W_PANEL_MIN_MD'])

        row = db.get_consequence(cons_id)
        self._chain = parse_chain_from_json(
            row['consequence_chain'] if row and 'consequence_chain' in row.keys() else '')
        raw_desc = row['description'] if row else ''

        layout = QVBoxLayout(self)

        form = QFormLayout(); form.setSpacing(8)
        self._base_edit = QLineEdit(raw_desc)
        self._base_edit.setPlaceholderText("Händelse / direkt konsekvens")
        self._base_edit.textChanged.connect(self._update_preview)
        form.addRow("Händelse:", self._base_edit)
        layout.addLayout(form)

        chain_box = QGroupBox("Konsekvenskedja — välj eskalering")
        chain_lay = QGridLayout(chain_box)
        chain_lay.setSpacing(4)
        self._checks: dict = {}
        row_idx, col_idx, last_group = 0, 0, None

        for key, label, group in CHAIN_ITEMS:
            if group and group != last_group:
                if col_idx > 0:
                    row_idx += 1; col_idx = 0
                hdr = QLabel(group)
                hdr.setStyleSheet(
                    "color:#8D9299; font-weight:bold; font-size:10px; margin-top:4px;")
                chain_lay.addWidget(hdr, row_idx, 0, 1, 2)
                row_idx += 1; col_idx = 0
                last_group = group
            chk = QCheckBox(label)
            chk.setChecked(bool(self._chain.get(key, False)))
            chk.stateChanged.connect(self._update_preview)
            self._checks[key] = chk
            chain_lay.addWidget(chk, row_idx, col_idx)
            col_idx += 1
            if col_idx >= 2:
                col_idx = 0; row_idx += 1

        layout.addWidget(chain_box)

        preview_lbl = QLabel("Genererad text:")
        preview_lbl.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(preview_lbl)
        self._preview = QLabel("—")
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet(
            "color:#17191C; font-weight:bold; font-size:11px;"
            "background:#F5F5F3; border:1px solid #E2E3E1;"
            "border-radius:3px; padding:4px 8px;")
        layout.addWidget(self._preview)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save_and_accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self._update_preview()

    def _update_preview(self):
        chain = {k: chk.isChecked() for k, chk in self._checks.items()}
        text  = build_consequence_text(self._base_edit.text().strip(), chain)
        self._preview.setText(text or "—")

    def _save_and_accept(self):
        chain    = {k: chk.isChecked() for k, chk in self._checks.items()}
        base     = self._base_edit.text().strip()
        full     = build_consequence_text(base, chain) or base or 'Ny konsekvens'
        cons     = self.db.get_consequence(self.cons_id)
        if cons:
            self.db.update_consequence(
                self.cons_id, full,
                cons['severity'] or 1,
                cons['category'] or '',
                json.dumps(chain))
        self.accept()


# ── NEW: column-based step picker ─────────────────────────────────────────────
_N_STEPS = 5

# ── Konsekvensgraf (event-tree-logik) ─────────────────────────────────────────
# Varje nod har 'text' och 'next' (logiska eskaleringssteg).
# Grafen följer event tree-analys (CCPS/DNV). Allt omitigerat.
# [objekt] ersätts med kolumnens Ref-tag vid visning/sparning.
_CONSEQ_NODES: dict = {
    'reduced_flow':         {'text': 'Reducerat flöde till [objekt]',           'next': ['low_level','hx_overheat','reaction_upset','quality_offspec','backpressure_upstream']},
    'no_flow':              {'text': 'Inget flöde till [objekt]',                'next': ['low_level','hx_overheat','pump_dryrun','reaction_upset','production_stop']},
    'high_flow':            {'text': 'För högt flöde till [objekt]',             'next': ['high_level','erosion','hx_undercool','carryover']},
    'low_level':            {'text': 'Låg nivå i [objekt]',                      'next': ['pump_dryrun','vortex_gas','no_flow']},
    'high_level':           {'text': 'Hög nivå i [objekt]',                      'next': ['overfill','carryover']},
    'overfill':             {'text': 'Överfyllnad av [objekt]',                  'next': ['pool_formation','env_release']},
    'carryover':            {'text': 'Vätskedroppar förs med gasen från [objekt]','next': ['liquid_slug']},
    'liquid_slug':          {'text': 'Vätskeslag i [objekt]',                    'next': ['equipment_catastrophic','loc_large']},
    'vortex_gas':           {'text': 'Gasinblandning i utlopp från [objekt]',    'next': ['pump_dryrun']},
    'pump_dryrun':          {'text': '[objekt] torrkör / kaviterar',              'next': ['seal_fail','bearing_fail']},
    'seal_fail':            {'text': 'Tätningsläckage på [objekt]',              'next': ['loc_small']},
    'bearing_fail':         {'text': 'Mekanisk skada på [objekt] (lager / löphjul)', 'next': ['pump_breakdown']},
    'pump_breakdown':       {'text': '[objekt] havererar',                       'next': ['production_stop','no_flow']},
    'backpressure_upstream':{'text': 'Ökat mottryck uppströms [objekt]',         'next': ['overpressure']},
    'erosion':              {'text': 'Erosion i [objekt] (hög strömningshastighet)', 'next': ['loc_small']},
    'hx_overheat':          {'text': 'Otillräcklig kylning — temperaturen i [objekt] stiger', 'next': ['vapor_pressure_rise','runaway','seal_degradation','quality_offspec']},
    'hx_undercool':         {'text': 'Överkylning av processmedium i [objekt]',  'next': ['quality_offspec','freeze_damage','hydrate_blockage']},
    'reaction_upset':       {'text': 'Reaktionsstörning i [objekt]',             'next': ['quality_offspec','runaway','toxic_gas_generation']},
    'quality_offspec':      {'text': 'Produkt utanför specifikation',            'next': ['production_stop']},
    'overpressure':         {'text': 'Trycket i [objekt] överstiger konstruktionstrycket', 'next': ['flange_leak','rupture']},
    'flange_leak':          {'text': 'Fläns- / packningsläckage vid [objekt]',   'next': ['loc_small']},
    'rupture':              {'text': '[objekt] brister',                          'next': ['loc_large','equipment_catastrophic']},
    'vacuum':               {'text': 'Undertryck i [objekt] under lägsta tillåtna driftstryck', 'next': ['vacuum_collapse','air_ingress']},
    'vacuum_collapse':      {'text': '[objekt] kollapsas av undertrycket',        'next': ['equipment_catastrophic','loc_small']},
    'air_ingress':          {'text': 'Luftinträngning i [objekt]',               'next': ['internal_flammable','quality_offspec']},
    'internal_flammable':   {'text': 'Brännbar atmosfär inuti [objekt]',         'next': ['internal_explosion']},
    'internal_explosion':   {'text': 'Intern explosion i [objekt]',              'next': ['equipment_catastrophic','loc_large','personnel_injury']},
    'flashing':             {'text': 'Processvätska förångas / flashar i [objekt]', 'next': ['pump_dryrun','quality_offspec']},
    'temp_above_design':    {'text': 'Temperaturen i [objekt] överstiger konstruktionsgränsen', 'next': ['vapor_pressure_rise','runaway','seal_degradation','material_creep','quality_offspec']},
    'vapor_pressure_rise':  {'text': 'Ångbildning — trycket i [objekt] stiger', 'next': ['overpressure']},
    'runaway':              {'text': 'Okontrollerad exoterm reaktion i [objekt]','next': ['rapid_pressure_rise','toxic_gas_generation']},
    'rapid_pressure_rise':  {'text': 'Snabb tryck- och temperaturökning i [objekt]', 'next': ['rupture']},
    'material_creep':       {'text': 'Reducerad hållfasthet i [objekt] (krypning)', 'next': ['rupture']},
    'seal_degradation':     {'text': 'Tätningar och packningar i [objekt] degraderas', 'next': ['seal_fail','flange_leak']},
    'temp_below_design':    {'text': 'Temperaturen i [objekt] understiger konstruktionsgränsen', 'next': ['brittle_fracture','hydrate_blockage','freeze_damage']},
    'brittle_fracture':     {'text': 'Försprödning av [objekt] — risk för sprödbrott', 'next': ['rupture']},
    'hydrate_blockage':     {'text': 'Hydrat- / isproppsbildning i [objekt]',    'next': ['no_flow','overpressure']},
    'freeze_damage':        {'text': 'Sönderfrysning av [objekt]',               'next': ['loc_small']},
    'reverse_flow':         {'text': 'Backflöde genom [objekt]',                 'next': ['upstream_contamination','pump_reverse','incompatible_mixing']},
    'upstream_contamination':{'text':'Kontaminering av uppströmssystemet via [objekt]', 'next': ['quality_offspec','incompatible_mixing']},
    'pump_reverse':         {'text': '[objekt] roterar baklänges',               'next': ['bearing_fail']},
    'misdirected_flow':     {'text': 'Flödet leds till [objekt] i stället för avsedd destination', 'next': ['high_level','incompatible_mixing','no_flow']},
    'incompatible_mixing':  {'text': 'Inkompatibla medier blandas i [objekt]',  'next': ['runaway','toxic_gas_generation','overpressure']},
    'contamination_feed':   {'text': 'Avvikande sammansättning i inflödet till [objekt]', 'next': ['quality_offspec','incompatible_mixing','reaction_upset']},
    'toxic_gas_generation': {'text': 'Giftig eller korrosiv gas bildas i [objekt]', 'next': ['overpressure','toxic_exposure']},
    'utility_loss':         {'text': 'Hjälpmedier till [objekt] faller bort',   'next': ['hx_overheat','no_flow','production_stop']},
    'loc_small':            {'text': 'Läckage från [objekt]',                    'next': ['jet_fire','pool_formation','flash_fire','toxic_exposure','env_release']},
    'loc_large':            {'text': 'Okontrollerat utsläpp från [objekt]',      'next': ['jet_fire','pool_formation','vce','flash_fire','toxic_exposure','env_release']},
    'pool_formation':       {'text': 'Vätskepöl bildas vid [objekt]',            'next': ['pool_fire','env_release','toxic_exposure']},
    'jet_fire':             {'text': 'Jetbrand vid [objekt]',                    'next': ['escalation_bleve','personnel_injury','equipment_damage']},
    'pool_fire':            {'text': 'Pölbrand vid [objekt]',                    'next': ['escalation_bleve','personnel_injury','equipment_damage']},
    'flash_fire':           {'text': 'Fördröjd antändning — flash fire',         'next': ['personnel_injury']},
    'vce':                  {'text': 'Ångmolnsexplosion (VCE)',                  'next': ['fatality','equipment_catastrophic']},
    'escalation_bleve':     {'text': 'Brandpåverkan på intilliggande kärl — BLEVE / dominoeffekt', 'next': ['fatality','equipment_catastrophic']},
    'toxic_exposure':       {'text': 'Exponering av personal för giftig gas',    'next': ['personnel_injury','fatality']},
    'env_release':          {'text': 'Utsläpp till mark, vatten eller luft',     'next': ['env_impact']},
    'personnel_injury':     {'text': 'Personskada',                              'next': ['fatality']},
    'fatality':             {'text': 'Dödsolycka',                               'next': []},
    'equipment_damage':     {'text': 'Utrustningsskada',                         'next': ['production_stop']},
    'equipment_catastrophic':{'text':'Allvarlig skada på utrustning och struktur','next': ['production_stop']},
    'production_stop':      {'text': 'Produktionsavbrott',                       'next': []},
    'env_impact':           {'text': 'Miljöpåverkan — sanering och myndighetsrapportering krävs', 'next': []},
    'tube_failure':         {'text': 'Rörbrott i värmeväxlare — korsflöde',         'next': ['incompatible_mixing', 'overpressure', 'loc_small']},
}

_CONSEQ_ENTRY: dict = {
    # ── Generic (wildcard object) ──────────────────────────────────────────────
    ('Lågt flöde', '*'):              ['reduced_flow', 'no_flow'],
    ('Högt flöde', '*'):              ['high_flow', 'high_level', 'erosion'],
    ('Högt tryck', '*'):              ['overpressure'],
    ('Lågt tryck', '*'):              ['vacuum', 'flashing', 'reduced_flow'],
    ('Hög nivå', '*'):                ['high_level', 'carryover'],
    ('Låg nivå', '*'):                ['low_level'],
    ('Hög temperatur', '*'):          ['temp_above_design'],
    ('Låg temperatur', '*'):          ['temp_below_design'],
    ('Omvänt flöde', '*'):            ['reverse_flow'],
    ('Missriktat flöde', '*'):        ['misdirected_flow'],
    ('Avvikande sammansättning', '*'): ['contamination_feed'],
    ('Bortfall av hjälpsystem', '*'): ['utility_loss'],
    ('Drift', '*'):                   ['no_flow', 'high_level', 'overpressure', 'loc_small', 'misdirected_flow'],
    ('Underhåll', '*'):               ['loc_small', 'no_flow', 'overpressure', 'internal_flammable'],
    ('Start-up / Shut-down', '*'):    ['loc_small', 'overpressure', 'internal_flammable', 'liquid_slug', 'brittle_fracture'],

    # ── Pump ──────────────────────────────────────────────────────────────────
    ('Lågt flöde',  'Pump'):          ['pump_dryrun', 'vortex_gas', 'reduced_flow'],
    ('Högt flöde',  'Pump'):          ['erosion', 'bearing_fail'],
    ('Högt tryck',  'Pump'):          ['seal_fail', 'overpressure'],
    ('Lågt tryck',  'Pump'):          ['pump_dryrun', 'vacuum'],
    ('Hög temperatur', 'Pump'):       ['seal_degradation', 'bearing_fail'],
    ('Låg temperatur', 'Pump'):       ['freeze_damage', 'hydrate_blockage'],
    ('Omvänt flöde',   'Pump'):       ['pump_reverse', 'bearing_fail'],

    # ── Tank / kärl ───────────────────────────────────────────────────────────
    ('Lågt flöde',  'Tank / kärl / kolonn'):  ['low_level', 'pump_dryrun'],
    ('Högt flöde',  'Tank / kärl / kolonn'):  ['high_level', 'overfill'],
    ('Högt tryck',  'Tank / kärl / kolonn'):  ['overpressure', 'rupture'],
    ('Lågt tryck',  'Tank / kärl / kolonn'):  ['vacuum', 'vacuum_collapse'],
    ('Hög nivå',    'Tank / kärl / kolonn'):  ['high_level', 'overfill', 'carryover'],
    ('Låg nivå',    'Tank / kärl / kolonn'):  ['low_level', 'vortex_gas', 'pump_dryrun'],
    ('Hög temperatur','Tank / kärl / kolonn'):['vapor_pressure_rise', 'runaway', 'temp_above_design'],
    ('Låg temperatur','Tank / kärl / kolonn'):['brittle_fracture', 'freeze_damage', 'temp_below_design'],
    ('Avvikande sammansättning','Tank / kärl / kolonn'): ['incompatible_mixing', 'reaction_upset', 'contamination_feed'],

    # ── Rörledning ────────────────────────────────────────────────────────────
    ('Lågt flöde',  'Rörledning / slang'):  ['reduced_flow', 'hydrate_blockage'],
    ('Högt tryck',  'Rörledning / slang'):  ['flange_leak', 'loc_small'],
    ('Lågt tryck',  'Rörledning / slang'):  ['vacuum', 'air_ingress'],
    ('Hög temperatur','Rörledning / slang'):['seal_degradation', 'flange_leak'],
    ('Låg temperatur','Rörledning / slang'):['freeze_damage', 'brittle_fracture'],
    ('Omvänt flöde', 'Rörledning / slang'): ['upstream_contamination', 'reverse_flow'],

    # ── Värmeväxlare ──────────────────────────────────────────────────────────
    ('Lågt flöde',  'Värmeväxlare / kylare / värmare'): ['hx_overheat', 'reduced_flow'],
    ('Högt flöde',  'Värmeväxlare / kylare / värmare'): ['hx_undercool', 'erosion'],
    ('Hög temperatur','Värmeväxlare / kylare / värmare'):['tube_failure', 'overpressure'],
    ('Låg temperatur','Värmeväxlare / kylare / värmare'):['hx_undercool', 'freeze_damage'],

    # ── Manuell ventil ────────────────────────────────────────────────────────
    ('Lågt flöde',     'Manuell ventil'):     ['reduced_flow', 'no_flow'],
    ('Högt flöde',     'Manuell ventil'):     ['high_flow', 'erosion'],
    ('Högt tryck',     'Manuell ventil'):     ['overpressure', 'flange_leak'],
    ('Omvänt flöde',   'Manuell ventil'):     ['reverse_flow', 'upstream_contamination'],
    ('Missriktat flöde','Manuell ventil'):    ['misdirected_flow'],
    ('Underhåll',      'Manuell ventil'):     ['loc_small', 'no_flow', 'internal_flammable'],

    # ── On-off ventil ─────────────────────────────────────────────────────────
    ('Lågt flöde',     'On-off ventil'):      ['no_flow', 'reduced_flow'],
    ('Högt tryck',     'On-off ventil'):      ['overpressure', 'flange_leak'],
    ('Missriktat flöde','On-off ventil'):     ['misdirected_flow'],
    ('Bortfall av hjälpsystem','On-off ventil'): ['no_flow', 'overpressure'],
    ('Start-up / Shut-down','On-off ventil'): ['overpressure', 'loc_small', 'liquid_slug'],

    # ── Reglerventil ──────────────────────────────────────────────────────────
    ('Lågt flöde',     'Reglerventil'):       ['reduced_flow', 'no_flow'],
    ('Högt flöde',     'Reglerventil'):       ['high_flow', 'high_level', 'erosion'],
    ('Högt tryck',     'Reglerventil'):       ['overpressure', 'flange_leak'],
    ('Missriktat flöde','Reglerventil'):      ['misdirected_flow', 'high_level'],
    ('Bortfall av hjälpsystem','Reglerventil'): ['no_flow', 'overpressure', 'high_level'],
    ('Drift',          'Reglerventil'):       ['no_flow', 'overpressure', 'misdirected_flow'],

    # ── Backventil ────────────────────────────────────────────────────────────
    ('Omvänt flöde',   'Backventil'):         ['reverse_flow', 'upstream_contamination', 'pump_reverse'],
    ('Lågt flöde',     'Backventil'):         ['no_flow', 'reduced_flow'],
    ('Drift',          'Backventil'):         ['reverse_flow', 'upstream_contamination'],
    ('Underhåll',      'Backventil'):         ['reverse_flow', 'loc_small'],

    # ── Fläns / koppling / packning ───────────────────────────────────────────
    ('Högt tryck',     'Fläns / koppling / packning'): ['flange_leak', 'overpressure'],
    ('Hög temperatur', 'Fläns / koppling / packning'): ['seal_degradation', 'flange_leak'],
    ('Låg temperatur', 'Fläns / koppling / packning'): ['brittle_fracture', 'freeze_damage'],
    ('Underhåll',      'Fläns / koppling / packning'): ['loc_small', 'internal_flammable'],
    ('Drift',          'Fläns / koppling / packning'): ['flange_leak', 'loc_small'],

    # ── Kompressor / fläkt ────────────────────────────────────────────────────
    ('Lågt flöde',     'Kompressor / fläkt'): ['pump_dryrun', 'no_flow', 'bearing_fail'],
    ('Högt flöde',     'Kompressor / fläkt'): ['erosion', 'bearing_fail'],
    ('Högt tryck',     'Kompressor / fläkt'): ['overpressure', 'seal_fail'],
    ('Lågt tryck',     'Kompressor / fläkt'): ['pump_dryrun', 'vacuum'],
    ('Hög temperatur', 'Kompressor / fläkt'): ['seal_degradation', 'bearing_fail', 'temp_above_design'],
    ('Bortfall av hjälpsystem','Kompressor / fläkt'): ['no_flow', 'production_stop'],
    ('Start-up / Shut-down','Kompressor / fläkt'): ['liquid_slug', 'overpressure', 'bearing_fail'],

    # ── Filter / sil ──────────────────────────────────────────────────────────
    ('Lågt flöde',     'Filter / sil'):       ['reduced_flow', 'no_flow', 'backpressure_upstream'],
    ('Högt tryck',     'Filter / sil'):       ['overpressure', 'flange_leak'],
    ('Avvikande sammansättning','Filter / sil'): ['contamination_feed', 'quality_offspec'],
    ('Underhåll',      'Filter / sil'):       ['loc_small', 'no_flow'],
    ('Drift',          'Filter / sil'):       ['reduced_flow', 'backpressure_upstream'],

    # ── Säkerhetsventil / sprängbleck ─────────────────────────────────────────
    ('Högt tryck',     'Säkerhetsventil / sprängbleck'): ['loc_small', 'overpressure'],
    ('Drift',          'Säkerhetsventil / sprängbleck'): ['loc_small', 'production_stop'],
    ('Underhåll',      'Säkerhetsventil / sprängbleck'): ['loc_small', 'no_flow', 'overpressure'],
    ('Bortfall av hjälpsystem','Säkerhetsventil / sprängbleck'): ['overpressure', 'rupture'],

    # ── Instrument ────────────────────────────────────────────────────────────
    ('Bortfall av hjälpsystem','Instrument'): ['no_flow', 'overpressure', 'production_stop'],
    ('Drift',          'Instrument'):         ['quality_offspec', 'overpressure', 'no_flow'],
    ('Underhåll',      'Instrument'):         ['no_flow', 'production_stop'],

    # ── Styrsystem / PLC / DCS ────────────────────────────────────────────────
    ('Bortfall av hjälpsystem','Styrsystem / PLC / DCS'): ['no_flow', 'overpressure', 'high_level', 'production_stop'],
    ('Drift',          'Styrsystem / PLC / DCS'): ['misdirected_flow', 'overpressure', 'no_flow', 'high_level'],
    ('Start-up / Shut-down','Styrsystem / PLC / DCS'): ['overpressure', 'no_flow', 'liquid_slug'],
    ('Underhåll',      'Styrsystem / PLC / DCS'): ['no_flow', 'production_stop', 'overpressure'],
}

_CONSEQ_GENERIC_NEXT = [
    'loc_small', 'overpressure', 'equipment_damage',
    'personnel_injury', 'env_release', 'production_stop',
]

class ConsequenceStepPickerDialog(QDialog):
    """Konsekvenskedja Del1 → Del2 → Del3 → Del4 → Del5.

    All _N_STEPS columns are shown side by side — compact and tight, but
    the whole chain stays visible and directly editable at a glance rather
    than navigating one step at a time. Each column has a status-colored
    "Del N" header (muted gray = not reached / chain ended, light blue =
    options available, filled blue = a choice has been made), a scrollable
    option list built from the dependency graph, a free-text fallback, and
    a compact tag + object-type row. Picking an option (or typing free
    text) in column N immediately recomputes column N+1's options
    (cascading) and clears anything further downstream.
    """
    # Set when the "add more objects" button is clicked
    add_more_requested = False

    _COL_W = 150   # fixed width per "Del N" column — keeps the dialog tight

    _LIST_SS = (
        "QListWidget { border:1px solid #E2E3E1; border-radius:5px; background:white; }"
        "QListWidget::item { padding:3px 5px; border-radius:3px; font-size:10px; }"
        "QListWidget::item:selected {"
        "  background:#E6ECFA; color:#17191C; font-weight:bold;"
        "  border:1px solid #17191C; }"
        "QListWidget::item:hover:!selected { background:#F5F5F3; }"
    )

    def __init__(self, db: 'Database', cons_id: int,
                 deviation: str = '', comp_type: str = '',
                 cause_text: str = '', initial_ref_tag: str = '',
                 parent=None):
        super().__init__(parent)
        self.db       = db
        self.cons_id  = cons_id
        self._dev     = deviation
        self._comp    = comp_type
        self._cause   = cause_text
        self.add_more_requested = False

        self.setWindowTitle("Konsekvenskedja — Del 1–5")
        col_gap = 9
        self.setMinimumWidth(min(
            QApplication.primaryScreen().availableGeometry().width() - 80,
            _N_STEPS * self._COL_W + (_N_STEPS - 1) * col_gap + 24))
        self.setMinimumHeight(440)
        # initial_ref_tag is pre-filled in Del1's ref-tag from the P&ID click
        self._initial_ref_tag = initial_ref_tag

        # Existing steps from DB
        existing = {s['step']: s for s in db.get_consequence_steps(cons_id)}

        # Context header
        cons_row = db.get_consequence(cons_id)
        self._orig_desc = cons_row['description'] if cons_row else ''

        main = QVBoxLayout(self)
        main.setSpacing(5)
        main.setContentsMargins(8, 8, 8, 8)

        # ── Context info ──────────────────────────────────────────────────────
        if deviation or comp_type or cause_text:
            ctx_parts = []
            if deviation:  ctx_parts.append(f"<b>Avvikelse:</b> {deviation}")
            if comp_type:  ctx_parts.append(f"<b>Objekt:</b> {comp_type}")
            if cause_text: ctx_parts.append(f"<b>Orsak:</b> {cause_text[:80]}")
            ctx = QLabel("  ·  ".join(ctx_parts))
            ctx.setStyleSheet("color:#17191C; font-size:10px; padding:2px 4px;"
                              "background:#F5F5F3; border-radius:3px;")
            ctx.setWordWrap(True)
            main.addWidget(ctx)

        # ── Column grid ───────────────────────────────────────────────────────
        cols_widget = QWidget()
        cols_layout = QHBoxLayout(cols_widget)
        cols_layout.setSpacing(0)
        cols_layout.setContentsMargins(0, 0, 0, 0)

        self._cols: list = []      # per-step state
        self._options: list = []   # option texts per step (graph node texts)
        self._opt_keys: list = []  # parallel node keys (None = free/saved text)

        for step in range(1, _N_STEPS + 1):
            self._options.append([])
            self._opt_keys.append([])

            if step > 1:
                cols_layout.addSpacing(4)
                divider = QFrame()
                divider.setFixedWidth(1)
                divider.setStyleSheet("background:#e5e7eb;")
                cols_layout.addWidget(divider)
                cols_layout.addSpacing(4)

            col_w = QWidget()
            col_w.setFixedWidth(self._COL_W)
            col_l = QVBoxLayout(col_w)
            col_l.setContentsMargins(0, 0, 0, 0)
            col_l.setSpacing(3)

            hdr = QLabel(f"Del {step}")
            hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col_l.addWidget(hdr)

            # QListWidget — native scrolling + word wrap, stays compact
            lst = QListWidget()
            lst.setWordWrap(True)
            lst.setSpacing(1)
            lst.setAlternatingRowColors(False)
            lst.setMinimumHeight(110)
            lst.setStyleSheet(self._LIST_SS)
            lst.itemClicked.connect(
                lambda it, s=step-1, lw=lst: self._list_clicked(s, lw))
            col_l.addWidget(lst, 1)

            # Terminal-chain message — shares the list's space (only one of
            # the two is ever visible), shown once a graph node with no
            # successors is reached instead of leaving an empty list box.
            end_lbl = QLabel("Kedjan slutar här")
            end_lbl.setWordWrap(True)
            end_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            end_lbl.setStyleSheet(
                "color:#9a6a3d; font-size:9px; background:#fff7ed;"
                "border:1px solid #fde8cc; border-radius:5px; padding:6px;")
            end_lbl.setMinimumHeight(110)
            end_lbl.setVisible(False)
            col_l.addWidget(end_lbl, 1)

            # Free-text input
            ft_edit = QLineEdit()
            ft_edit.setPlaceholderText("Fritext…")
            ft_edit.setStyleSheet("font-size:10px;")
            ft_edit.textChanged.connect(
                lambda _t, s=step-1: (self._cascade_from(s),
                                      self._refresh_header_state(s),
                                      self._update_preview()))
            col_l.addWidget(ft_edit)

            # Tag row: compact "Tag" label + field + pin button (no separate
            # label line — the placeholder text carries the hint instead)
            tag_row = QHBoxLayout()
            tag_row.setContentsMargins(0, 0, 0, 0)
            tag_row.setSpacing(2)
            tag_lbl = QLabel("Tag")
            tag_lbl.setStyleSheet("color:#999; font-size:9px;")
            tag_lbl.setFixedWidth(20)
            ref_edit = QLineEdit()
            ref_edit.setPlaceholderText("t.ex. T-101")
            ref_edit.setMaximumHeight(22)
            ref_edit.setStyleSheet("font-size:10px;")
            if step in existing:
                ref_edit.setText(existing[step].get('ref_tag', '') or '')
            elif step == 1 and self._initial_ref_tag:
                ref_edit.setText(self._initial_ref_tag)
            pin_btn = QPushButton("📍")
            pin_btn.setFixedSize(22, 22)
            pin_btn.setToolTip("Klicka på P&ID för att välja referensobjekt")
            pin_btn.setStyleSheet(
                "QPushButton { border:1px solid #dc2626; border-radius:3px;"
                "  background:#fee2e2; color:#dc2626; font-size:10px; }"
                "QPushButton:hover { background:#fca5a5; }")
            pin_btn.clicked.connect(partial(self._request_pick_for_col, step-1))
            tag_row.addWidget(tag_lbl)
            tag_row.addWidget(ref_edit)
            tag_row.addWidget(pin_btn)
            col_l.addLayout(tag_row)

            # Object-type row: compact "Typ" label + combo, pre-filled from
            # smart recognition on ref-tag change
            typ_row = QHBoxLayout()
            typ_row.setContentsMargins(0, 0, 0, 0)
            typ_row.setSpacing(2)
            typ_lbl = QLabel("Typ")
            typ_lbl.setStyleSheet("color:#999; font-size:9px;")
            typ_lbl.setFixedWidth(20)
            obj_combo = QComboBox()
            obj_combo.setFixedHeight(22)
            obj_combo.setStyleSheet("font-size:9px;")
            obj_combo.setToolTip("Objekttyp")
            obj_combo.addItem('')
            try:
                for o in db.standard_objects():
                    obj_combo.addItem(o['name'])
            except Exception:
                pass
            # Pre-select based on initial data
            if step == 1 and comp_type:
                _idx = obj_combo.findText(comp_type)
                if _idx >= 0:
                    obj_combo.setCurrentIndex(_idx)
            typ_row.addWidget(typ_lbl)
            typ_row.addWidget(obj_combo, 1)
            col_l.addLayout(typ_row)

            # Connect after layout so step-1 index is correct
            ref_edit.textChanged.connect(
                lambda tag, s=step-1, cb=obj_combo: (
                    self._refresh_list_labels(s, tag),
                    self._autofill_obj_combo(cb, tag),
                    self._update_preview()))
            obj_combo.currentTextChanged.connect(
                lambda txt, s=step-1: (
                    self._on_obj_type_changed(s, txt),
                    self._update_preview()))

            cols_layout.addWidget(col_w)

            col_state = {
                'list':      lst,
                'end_lbl':   end_lbl,
                'sel':       -1,
                'ft_edit':   ft_edit,
                'ref_edit':  ref_edit,
                'obj_combo': obj_combo,
                'hdr':       hdr,
            }
            self._cols.append(col_state)

        # Fill columns: Del1 from entry nodes, Del2+ cascades from selections.
        # Saved chains are restored selection by selection.
        self._init_columns(existing)

        main.addWidget(cols_widget, 1)

        # ── Preview strip ─────────────────────────────────────────────────────
        prev_frame = QFrame()
        prev_frame.setFrameShape(QFrame.Shape.StyledPanel)
        prev_frame.setStyleSheet("background:#f0f9ff; border-radius:4px;")
        prev_lay = QVBoxLayout(prev_frame)
        prev_lay.setContentsMargins(6, 4, 6, 4)
        lbl = QLabel("Genererad kedjetext:")
        lbl.setStyleSheet("color:#555; font-size:10px;")
        prev_lay.addWidget(lbl)
        self._preview = QLabel("—")
        self._preview.setWordWrap(True)
        self._preview.setStyleSheet(
            "color:#17191C; font-weight:bold; font-size:11px;")
        prev_lay.addWidget(self._preview)
        main.addWidget(prev_frame)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        add_more_btn = QPushButton("📍 Lägg till ytterligare objekt")
        add_more_btn.setToolTip(
            "Spara denna kedja och återgå till P&ID-läge\n"
            "för att omedelbart markera ytterligare ett objekt.")
        add_more_btn.setStyleSheet(
            "background:#17191C; color:white; border:none;"
            "border-radius:4px; padding:4px 10px;")
        add_more_btn.clicked.connect(self._save_and_add_more)
        btn_row.addWidget(add_more_btn)
        btn_row.addStretch()
        std_btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        std_btns.accepted.connect(self._save_and_accept)
        std_btns.rejected.connect(self.reject)
        btn_row.addWidget(std_btns)
        main.addLayout(btn_row)

        self._waiting_col_idx = None

        # ── Tab-key navigation between columns ────────────────────────────────
        # Tab in ft_edit or ref_edit of column N → focus ft_edit of column N+1
        for i, col in enumerate(self._cols):
            for field in (col['ft_edit'], col['ref_edit']):
                field.installEventFilter(self)
        self._field_order = []
        for col in self._cols:
            self._field_order.append(col['ft_edit'])
            self._field_order.append(col['ref_edit'])

        self._update_preview()

    # ── Graf-baserade alternativ ──────────────────────────────────────────────
    def _entry_pairs(self, obj_type: str = ''):
        """[(node_key, text)] for Del1, looked up per deviation + object type."""
        comp = obj_type or (
            self._cols[0]['obj_combo'].currentText()
            if self._cols else self._comp) or self._comp
        keys = (_CONSEQ_ENTRY.get((self._dev, comp)) or
                _CONSEQ_ENTRY.get((self._dev, '*')) or
                _CONSEQ_ENTRY.get((self._dev, '')) or
                _CONSEQ_GENERIC_NEXT)
        return [(k, _CONSEQ_NODES[k]['text']) for k in keys if k in _CONSEQ_NODES]

    def _autofill_obj_combo(self, combo: 'QComboBox', tag: str):
        """Look up object type from smart recognition for this tag and set combo."""
        if not tag.strip():
            return
        comp = _lookup_comp_type_for_tag(tag.strip(), self.db)
        if comp:
            idx = combo.findText(comp)
            if idx >= 0 and combo.currentIndex() != idx:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)

    def _on_obj_type_changed(self, step_idx: int, obj_type: str):
        """Object type changed in a column — repopulate Del1 if this is col 0."""
        if step_idx == 0:
            # Re-initialise column 0 with deviation + new object type
            self._populate_column(0, self._entry_pairs(obj_type))
            self._cascade_from(0)

    @staticmethod
    def _successor_pairs(node_key):
        node = _CONSEQ_NODES.get(node_key)
        if not node:
            return []
        return [(k, _CONSEQ_NODES[k]['text'])
                for k in node['next'] if k in _CONSEQ_NODES]

    @staticmethod
    def _generic_pairs():
        return [(k, _CONSEQ_NODES[k]['text'])
                for k in _CONSEQ_GENERIC_NEXT if k in _CONSEQ_NODES]

    def _populate_column(self, step_idx: int, pairs, upstream_has_sel: bool = True):
        """Fill column step_idx with (key, text) pairs; clears selection.

        upstream_has_sel distinguishes two visually different empty states:
        when the PREVIOUS column has a selection but pairs is still empty,
        a real graph terminal was reached ("Kedjan slutar här" is shown);
        when the previous column has nothing chosen yet, this column just
        hasn't been reached, so it's left as a neutral empty list instead.
        """
        col = self._cols[step_idx]
        lst = col['list']
        tag = col['ref_edit'].text().strip()
        lst.clear()
        keys, texts = [], []
        for k, t in pairs:
            keys.append(k)
            texts.append(t)
            lst.addItem(QListWidgetItem(
                f"{len(texts)}. {self._resolve(t, tag)}"))
        self._options[step_idx]  = texts
        self._opt_keys[step_idx] = keys
        col['sel'] = -1
        lst.setCurrentRow(-1)
        has_opts = bool(pairs)
        show_end_msg = (not has_opts) and upstream_has_sel
        lst.setVisible(not show_end_msg)
        col['end_lbl'].setVisible(show_end_msg)
        self._refresh_header_state(step_idx)

    def _selected_key(self, step_idx: int):
        """Node key of the column's selection, or None (free text / none)."""
        col = self._cols[step_idx]
        if col['ft_edit'].text().strip():
            return None
        sel = col['list'].currentRow()
        keys = self._opt_keys[step_idx]
        if 0 <= sel < len(keys):
            return keys[sel]
        return None

    def _next_pairs_after(self, step_idx: int):
        """Compute the (key, text) pairs for step_idx+1 based on step_idx's state."""
        col = self._cols[step_idx]
        ft = col['ft_edit'].text().strip()
        key = self._selected_key(step_idx)
        has_sel = (col['list'].currentRow() >= 0) or bool(ft)
        if key:
            return self._successor_pairs(key)
        elif has_sel:
            return self._generic_pairs()   # free text / unknown node
        return []                           # nothing chosen → chain ends here

    def _cascade_from(self, step_idx: int):
        """Repopulate column step_idx+1 based on this column's state; clear rest."""
        if step_idx + 1 >= _N_STEPS:
            return
        col = self._cols[step_idx]
        upstream_has_sel = (col['list'].currentRow() >= 0) or bool(col['ft_edit'].text().strip())
        self._populate_column(step_idx + 1, self._next_pairs_after(step_idx), upstream_has_sel)
        self._cascade_from(step_idx + 1)     # downstream columns reset too

    def _init_columns(self, existing: dict):
        """Initial fill: entry nodes in Del1, neutral empty state for the
        rest, then walk saved chain if any (restoring selections)."""
        self._populate_column(0, self._entry_pairs())
        for i in range(1, _N_STEPS):
            self._populate_column(i, [], upstream_has_sel=False)
        for i in range(_N_STEPS):
            step  = i + 1
            saved = existing.get(step) if existing else None
            if not saved or not (saved.get('text') or '').strip():
                break
            s_text = saved['text']
            s_key  = (saved.get('node_key') or '').strip() or None
            s_tag  = (saved.get('ref_tag') or '').strip()
            keys   = self._opt_keys[i]
            texts  = self._options[i]
            sel = -1
            if s_key and s_key in keys:
                sel = keys.index(s_key)
            else:
                for j, t in enumerate(texts):
                    if self._resolve(t, s_tag) == s_text or t == s_text:
                        sel = j
                        break
            if sel < 0:
                # Saved text not among graph options — prepend it
                pairs = [(s_key, s_text)] + list(zip(keys, texts))
                self._populate_column(i, pairs)
                sel = 0
            self._cols[i]['sel'] = sel
            self._cols[i]['list'].setCurrentRow(sel)
            self._refresh_header_state(i)
            if i + 1 < _N_STEPS:
                nxt_key = self._opt_keys[i][sel] if sel < len(self._opt_keys[i]) else None
                pairs = self._successor_pairs(nxt_key) if nxt_key else self._generic_pairs()
                self._populate_column(i + 1, pairs, upstream_has_sel=True)

    # ── Column header status color ────────────────────────────────────────────
    def _refresh_header_state(self, step_idx: int):
        """Color-code the 'Del N' header: outlined = options available,
        filled dark = a choice has been made, muted gray = nothing to
        do here (not yet reached, or the chain ended) — an at-a-glance
        progress indicator across all visible columns."""
        col = self._cols[step_idx]
        has_sel  = col['sel'] >= 0 or bool(col['ft_edit'].text().strip())
        has_opts = bool(self._options[step_idx])
        if has_sel:
            style = ("font-weight:bold; color:white; font-size:10px;"
                     "background:#17191C; border-radius:3px; padding:3px;")
        elif not has_opts:
            style = ("font-weight:bold; color:#8D9299; font-size:10px;"
                     "background:#F5F5F3; border-radius:3px; padding:3px;")
        else:
            style = ("font-weight:bold; color:#17191C; font-size:10px;"
                     "background:#FFFFFF; border:1px solid #CFD1CE; border-radius:3px; padding:2px;")
        col['hdr'].setStyleSheet(style)

    # ── Selection logic ───────────────────────────────────────────────────────
    def _list_clicked(self, step_idx: int, listwidget):
        row = listwidget.currentRow()
        old = self._cols[step_idx]['sel']
        if old == row:
            # Second click deselects
            listwidget.clearSelection()
            listwidget.setCurrentRow(-1)
            self._cols[step_idx]['sel'] = -1
        else:
            self._cols[step_idx]['sel'] = row
        # Dependent columns: repopulate Del(n+1) from the new selection
        self._cascade_from(step_idx)
        self._refresh_header_state(step_idx)
        self._update_preview()

    # ── Preview ───────────────────────────────────────────────────────────────
    def _selected_text(self, step_idx: int) -> str:
        col = self._cols[step_idx]
        tag = col['ref_edit'].text().strip()
        ft  = col['ft_edit'].text().strip()
        if ft:
            return self._resolve(ft, tag)
        sel = col['list'].currentRow()
        opts = self._options[step_idx]
        if 0 <= sel < len(opts):
            t = opts[sel]
            if t.startswith("("):
                return ''
            return self._resolve(t, tag)
        return ''

    def _update_preview(self):
        parts = []
        for i in range(_N_STEPS):
            t = self._selected_text(i)
            if t:
                parts.append(t)
        self._preview.setText(' → '.join(parts) if parts else '—')

    # ── [objekt] substitution ─────────────────────────────────────────────────
    @staticmethod
    def _resolve(text: str, tag: str) -> str:
        """Replace [objekt] with the ref-tag when a tag is known.
        When no tag is set, strip the placeholder and trailing preposition
        so the text reads naturally without a dangling noun.

        Examples:
          tag='T-101':  'Låg nivå i [objekt]'      → 'Låg nivå i T-101'
          tag='':       'Låg nivå i [objekt]'      → 'Låg nivå'
          tag='T-101':  '[objekt] torrkör'          → 'T-101 torrkör'
          tag='':       '[objekt] torrkör'          → 'Torrkörning'
          tag='T-101':  'Trycket överstiger gränsen'→ 'Trycket överstiger gränsen'
        """
        if not text:
            return text
        if tag:
            out = text.replace('[objekt]', tag)
            if out and out[0].islower():
                out = out[0].upper() + out[1:]
            return out
        # No tag — strip placeholder + any preceding OR following preposition phrase
        # so "Flödet leds till [objekt] i stället för avsedd destination"
        # → "Flödet leds" (not "Flödet leds i stället för avsedd destination")
        import re as _re
        # Remove "till [objekt] i stället för <something>" as a unit
        stripped = _re.sub(
            r'\s+(?:i\s+stället\s+för|inuti|i\s+inflödet\s+till|från|till|vid|av|för|mot|ur|på|i)\s+\[objekt\](?:\s+i\s+stället\s+för\s+[^,—]*)?',
            '', text, flags=_re.IGNORECASE)
        stripped = stripped.replace(' [objekt]', '').replace('[objekt] ', '').replace('[objekt]', '')
        stripped = stripped.rstrip(' \t,;—')
        if stripped and stripped[0].islower():
            stripped = stripped[0].upper() + stripped[1:]
        return stripped

    def _refresh_list_labels(self, step_idx: int, tag: str):
        """Update list item display text when ref-tag changes for this column."""
        lst  = self._cols[step_idx]['list']
        opts = self._options[step_idx]
        for i, opt in enumerate(opts):
            item = lst.item(i)
            if item is not None:
                item.setText(f"{i+1}. {self._resolve(opt, tag)}")

    # ── Pin button: pick ref-tag from P&ID ────────────────────────────────────
    def _request_pick_for_col(self, col_idx: int):
        """Hide dialog, enter MODE_PICK_REF_TAG; MainWindow refills col on pick."""
        self._waiting_col_idx = col_idx
        self.hide()
        # Walk up to MainWindow and trigger the pick mode
        p = self.parent()
        while p is not None:
            if hasattr(p, 'pid_panel') and hasattr(p.pid_panel, '_set_mode'):
                p.pid_panel._set_mode(MODE_PICK_REF_TAG)
                break
            p = p.parent() if hasattr(p, 'parent') else None

    # ── Tab navigation event filter ───────────────────────────────────────────
    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if (event.type() == QEvent.Type.KeyPress and
                event.key() == Qt.Key.Key_Tab):
            try:
                idx = self._field_order.index(obj)
                nxt = self._field_order[(idx + 1) % len(self._field_order)]
                nxt.setFocus()
                nxt.selectAll()
                return True
            except ValueError:
                pass
        return super().eventFilter(obj, event)

    # ── Save helpers ──────────────────────────────────────────────────────────
    def _save_and_add_more(self):
        self.add_more_requested = True
        self._do_save()
        self.accept()

    # ── Save ──────────────────────────────────────────────────────────────────
    def _save_and_accept(self):
        self.add_more_requested = False
        self._do_save()
        self.accept()

    def _do_save(self):
        steps = []
        for i in range(_N_STEPS):
            text = self._selected_text(i)
            ref  = self._cols[i]['ref_edit'].text().strip()
            if text or ref:
                steps.append({'step': i + 1, 'text': text, 'ref_tag': ref,
                              'node_key': self._selected_key(i) or ''})
        self.db.set_consequence_steps(self.cons_id, steps)

        parts = [s['text'] for s in steps if s['text']]
        full  = ' → '.join(parts) if parts else (self._orig_desc or 'Ny konsekvens')
        cons = self.db.get_consequence(self.cons_id)
        if cons:
            self.db.update_consequence(
                self.cons_id, full,
                cons['severity'] or 1,
                cons['category'] or '',
                cons['consequence_chain'] or '')




class ReductionFactorsDialog(QDialog):
    """Edit the list of extra reduction factors for a consequence."""

    def __init__(self, db, consequence_id, parent=None):
        super().__init__(parent)
        self.db = db
        self.consequence_id = consequence_id
        self.setWindowTitle("Övriga reduktionsfaktorer")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Lägg till faktorer som reducerar slutkonsekvensfrekvensen:"))

        self._tbl = QTableWidget(0, 3)
        self._tbl.setHorizontalHeaderLabels(['Beskrivning', 'RRF', ''])
        self._tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._tbl.setColumnWidth(1, 80); self._tbl.setColumnWidth(2, 64)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.cellChanged.connect(self._on_cell)
        layout.addWidget(self._tbl)

        add_btn = QPushButton("+ Lägg till faktor")
        add_btn.clicked.connect(self._add)
        layout.addWidget(add_btn)
        layout.addWidget(QDialogButtonBox(QDialogButtonBox.StandardButton.Close,
                                          accepted=self.accept, rejected=self.accept))
        self._refresh()

    def _refresh(self):
        try: self._tbl.cellChanged.disconnect()
        except RuntimeError as e: logging.warning(f"Table cellChanged signal not connected: {e}")
        self._tbl.setRowCount(0)
        for rf in self.db.reduction_factors(self.consequence_id):
            r = self._tbl.rowCount(); self._tbl.insertRow(r)
            desc = QTableWidgetItem(rf['description'])
            desc.setData(Qt.ItemDataRole.UserRole, rf['id'])
            self._tbl.setItem(r, 0, desc)
            self._tbl.setItem(r, 1, QTableWidgetItem(str(rf['rrf'])))
            del_btn = QPushButton("Ta bort")
            del_btn.clicked.connect(lambda _, rid=rf['id']: (
                self.db.delete_reduction_factor(rid), self._refresh()))
            self._tbl.setCellWidget(r, 2, del_btn)
            self._tbl.setRowHeight(r, 26)
        self._tbl.cellChanged.connect(self._on_cell)

    def _add(self):
        new_id = self.db.add_reduction_factor(self.consequence_id, 'Ny faktor', 10)
        self._refresh()

    def _on_cell(self, row, col):
        item = self._tbl.item(row, 0)
        if not item: return
        rf_id = item.data(Qt.ItemDataRole.UserRole)
        desc = self._tbl.item(row, 0).text() if self._tbl.item(row, 0) else ''
        try: rrf = int(self._tbl.item(row, 1).text()) if self._tbl.item(row, 1) else 10
        except ValueError: rrf = 10
        self.db.update_reduction_factor(rf_id, desc, rrf, 1)


def _draw_text_with_bold_tags(painter, rect, text, tags, base_font, color, word_wrap):
    """Draw `text` inside `rect`, rendering any whole-word occurrence of a
    member of `tags` in bold (2026-08-09, see NOTES.md "fetmarkera
    objekttexten i konsekvensen så man ser att det är som ett objekt").
    Falls back to a single plain drawText call when there's nothing to
    bold — the common case for untouched free text — so this stays as
    cheap as the original code for every row that was never drag-tagged.
    `word_wrap=True` mirrors the KON column's multi-line wrapping;
    `word_wrap=False` mirrors the SG column's single-line elided text."""
    flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
    if word_wrap:
        flags |= Qt.TextFlag.TextWordWrap
    ranges = find_tag_bold_ranges(text, tags) if tags else []
    if not ranges:
        painter.setFont(base_font)
        painter.setPen(color)
        painter.drawText(rect, flags, text)
        return

    if not word_wrap:
        # Single-line elided mode (SG cell) — elide first; a bold range
        # that falls in the elided tail is simply lost, same information
        # loss the plain-text path already accepted for long descriptions.
        fm = QFontMetrics(base_font)
        text = fm.elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        ranges = find_tag_bold_ranges(text, tags)

    layout = QTextLayout(text, base_font)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WordWrap if word_wrap
                     else QTextOption.WrapMode.NoWrap)
    layout.setTextOption(opt)

    bold_font = QFont(base_font)
    bold_font.setBold(True)
    bold_fmt = QTextCharFormat()
    bold_fmt.setFont(bold_font)
    formats = []
    for s, e in ranges:
        fr = QTextLayout.FormatRange()
        fr.start = s
        fr.length = e - s
        fr.format = bold_fmt
        formats.append(fr)
    layout.setFormats(formats)

    layout.beginLayout()
    y = 0.0
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(rect.width())
        line.setPosition(QPointF(0, y))
        y += line.height()
        if not word_wrap:
            break
    layout.endLayout()

    painter.setPen(color)
    layout.draw(painter, QPointF(rect.left(), rect.top()))


class _ScenarioDelegate(QStyledItemDelegate):
    """Custom delegate: word-wrap for ORS/KON/SG cells; passes eventFilter to editors."""

    _WRAP_COLS = None   # set after panel constants are known

    def __init__(self, panel):
        super().__init__(panel)
        self._panel   = panel
        self._fm_font = None   # cached QFont
        self._fm      = None   # cached QFontMetrics — rebuilt only when font changes

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if editor is not None:
            editor.setProperty('editing_row', index.row())
            editor.setProperty('editing_col', index.column())
            editor.installEventFilter(self._panel)
        return editor

    def sizeHint(self, option, index):
        # Defensive hardening: sizeHint() is invoked for every visible cell
        # during resizeRowsToContents(), including — in theory — cells whose
        # backing _row_meta/_row_cat_info could be read mid-_build_rows() if
        # Qt ever triggers a repaint/layout pass reentrantly while rows are
        # still being constructed. A genuinely native (C++-level) crash here
        # cannot be caught by Python try/except, but if any part of this path
        # (QFontMetrics.boundingRect, index.data, attribute access on a
        # transient state) raises a Python-level exception instead, falling
        # back to a safe default size costs nothing and avoids compounding
        # a silent failure with an unhandled Python exception on top.
        try:
            return self._size_hint_impl(option, index)
        except Exception:
            logging.exception('_ScenarioDelegate.sizeHint: fallback after exception '
                              '(row=%d col=%d)', index.row(), index.column())
            return QSize(max(40, option.rect.width()), 24)

    def _size_hint_impl(self, option, index):
        col = index.column()
        panel = self._panel
        # Cache QFontMetrics — reconstructed only when the font changes
        if option.font != self._fm_font:
            self._fm_font = option.font
            self._fm = QFontMetrics(option.font)
        fm = self._fm
        one_line_h = fm.height() + 6

        wrap_cols = {panel._C_ORS, panel._C_KON}
        if col not in wrap_cols:
            if col == panel._C_SG:
                # SG's description never word-wraps (unlike ORS/KON) — a
                # single compact line is always enough.
                return QSize(option.rect.width(), one_line_h)
            # Non-wrap columns (risk cells) stay at one compact line
            base = super().sizeHint(option, index)
            return QSize(base.width(), one_line_h)

        text = index.data(Qt.ItemDataRole.DisplayRole) or ''
        if not text:
            return QSize(option.rect.width(), one_line_h)

        w = option.rect.width() if option.rect.width() > 0 else 200
        if col == panel._C_ORS:
            w = max(40, option.rect.width() - 6)
            rect = fm.boundingRect(0, 0, w, 10000, Qt.TextFlag.TextWordWrap, text)
            return QSize(option.rect.width(),
                         _ORS_STRIP_H + max(one_line_h, rect.height() + 4))
        elif col == panel._C_KON:
            w -= _PID_ICON_W + _KON_CAT_W
            w = max(40, w)
            rect = fm.boundingRect(0, 0, w, 10000, Qt.TextFlag.TextWordWrap, text)
            return QSize(option.rect.width(), max(one_line_h, rect.height() + 4))
        elif col == panel._C_SG:
            w -= _PID_ICON_W + _RRF_W
        w = max(40, w)
        rect = fm.boundingRect(0, 0, w, 10000,
                               Qt.TextFlag.TextWordWrap, text)
        return QSize(option.rect.width(), max(one_line_h, rect.height() + 4))

    def paint(self, painter, option, index):
        """RFORE/SLUT (this base delegate — ORS/KON/SG are handled by
        the _PidDelegate subclass installed for those specific columns,
        see ScenarioTablePanel.__init__) need their own custom paint:
        the app-wide QSS rule targeting QTableWidget::item (see CONFIG's
        global stylesheet, applied via app.setStyleSheet() in main())
        means Qt's default QStyledItemDelegate.paint() (super().paint(),
        used for every other column here — NOD/UTR/DEV/LOPA) stops
        respecting Qt::BackgroundRole/ForegroundRole once any stylesheet
        touches ::item, a well-known Qt quirk: stylesheet-driven item
        rendering only ever reads QSS rules, never the model's own
        background/foreground data. setBackground()/setForeground() on
        these items (set in _add_row/_update_lopa_risk) therefore had no
        visible effect in the real app — cells stayed white until
        selected (the QSS DOES define its own :selected background,
        which is why only THAT part ever showed); this was invisible to
        every earlier test because none of them ever applied the real
        app stylesheet before painting (2026-08-09, see NOTES.md).
        NOD/UTR/DEV/LOPA never set a custom background, so their default
        palette-driven look is unaffected and stays on the super().paint()
        path unchanged."""
        col = index.column()
        panel = self._panel
        if col not in (panel._C_RFORE, panel._C_SLUT):
            super().paint(painter, option, index)
            return
        sel = bool(option.state & QStyle.StateFlag.State_Selected)
        r = option.rect
        painter.save()
        if sel:
            painter.fillRect(r, option.palette.highlight())
        else:
            bg = index.data(Qt.ItemDataRole.BackgroundRole)
            painter.fillRect(r, bg if bg is not None else (
                option.palette.alternateBase() if index.row() % 2 == 1
                else option.palette.base()))
        if sel:
            tc = option.palette.highlightedText().color()
        else:
            fg = index.data(Qt.ItemDataRole.ForegroundRole)
            tc = fg.color() if fg is not None else option.palette.text().color()
        painter.setPen(tc)
        font = index.data(Qt.ItemDataRole.FontRole)
        painter.setFont(font if font is not None else option.font)
        text_rect = QRect(r.left() + _PID_ICON_W, r.top(),
                          r.width() - _PID_ICON_W, r.height())
        painter.drawText(text_rect.adjusted(2, 2, -2, -2),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                         index.data(Qt.ItemDataRole.DisplayRole) or '')
        painter.restore()


_PID_ICON_W  = 22          # pixels reserved on the left for the pin icon
_KON_CAT_W   = 26          # pixels for the category badge zone in KON cells
_ORS_COMMENT_W = 22        # 💬 comment icon zone (rightmost of ORS)
_ORS_CLONE_W   = 22        # 📋 clone-scenario icon zone
# Height of the ORS cell's top strip ([pin|tag|freq|dots], see _PidDelegate.
# paint()'s "Cause cells" branch). MUST match everywhere a row's needed
# height is computed (sizeHint/_resize_rows_manual/_wrap_col_row_height)
# AND everywhere the strip is actually drawn/the editor is positioned below
# it (paint()/updateEditorGeometry) — these used to disagree (14 vs 17px),
# which under-allocated 3px of vertical space for the wrapped description
# text below the strip on every ORS row, silently clipping its bottom few
# pixels (2026-08-11, bug report: "text göms på raderna ... spöktext ligger
# kvar när man redigerar" — the clipped-then-stale pixels from the
# undersized row explain both symptoms). See NOTES.md.
_ORS_STRIP_H = 17

_ORS_FREQ_W  = 50          # pixels for the frequency badge zone after obj zone in ORS cells
_RRF_W       = 54          # pixel width of the RRF badge column on the right of safeguard cells

_PID_ICON_RE = re.compile(r'^[🟢📌]\s*')   # strip any old emoji prefix


def _draw_pid_pin(painter, rect, placed):
    """Draw a needle pin (circle + stick) inside rect. Green=placed, red=not placed."""
    color = QColor('#27ae60') if placed else QColor('#e74c3c')
    dark  = color.darker(150)

    r      = 4.5          # circle radius
    stick  = 5.0          # stick length below circle
    total  = r * 2 + stick

    cx  = float(rect.center().x())
    top = float(rect.center().y()) - total / 2.0

    circle_cy = top + r
    stick_top = top + r * 2
    stick_bot = top + total

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Stick
    pen = QPen(dark, 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(cx, stick_top), QPointF(cx, stick_bot))

    # Circle head
    painter.setBrush(QBrush(color))
    painter.setPen(QPen(dark, 1.0))
    painter.drawEllipse(QPointF(cx, circle_cy), r, r)

    # White highlight dot
    dot_r = r * 0.3
    painter.setBrush(QBrush(QColor(255, 255, 255, 170)))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QPointF(cx - r * 0.35, circle_cy - r * 0.35), dot_r, dot_r)
    painter.restore()


def _make_pin_icon(placed, size=16):
    """Return a QIcon with the needle pin rendered at the given size."""
    px = QPixmap(size, size)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    _draw_pid_pin(p, QRect(0, 0, size, size), placed)
    p.end()
    return QIcon(px)


class _PidDelegate(_ScenarioDelegate):
    """Draws a P&ID placement icon on the left of Orsak/Konsekvens/Barriär cells.
    The editor always shows only the clean description (emoji stripped)."""

    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        if editor is not None:
            # Show only the clean description (EditRole is already clean)
            raw = index.data(Qt.ItemDataRole.EditRole) or ''
            editor.setText(_PID_ICON_RE.sub('', str(raw)))
            if index.column() == self._panel._C_ORS:
                self._attach_cause_completer(editor, index)
        return editor

    def _attach_cause_completer(self, editor, index):
        """Suggest standard-cause descriptions while inline-editing an Orsak
        cell — the same library StandardCausesPickerPopup/CauseObjectPopup
        draw from, so quick text edits get the same suggestions as the
        popups instead of a bare, unassisted QLineEdit.
        """
        db = getattr(self._panel, 'db', None)
        if db is None:
            return
        try:
            row = index.row()
            row_meta = getattr(self._panel, '_row_meta', [])
            dev_id = row_meta[row][0] if row < len(row_meta) else None
            item = self._panel._table.item(row, self._panel._C_ORS)
            obj_data = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
            comp_type = (obj_data or ('', ''))[0]

            descs = []
            if dev_id is not None:
                dev = db.get_deviation(dev_id)
                std_dev_id = _resolve_std_deviation_id(db, dev['description'] if dev else '')
                if std_dev_id is not None:
                    obj_id = None
                    if comp_type:
                        for o in db.standard_objects():
                            if _obj_type_matches(comp_type, o['name']):
                                obj_id = o['id']
                                break
                    if obj_id is not None:
                        descs = [c['description'] for c in
                                 db.standard_causes_for_object(std_dev_id, obj_id)]
            if not descs:
                descs = [r[0] for r in db.conn.execute(
                    "SELECT DISTINCT description FROM standard_causes").fetchall()]

            if descs:
                comp = QCompleter(descs, editor)
                comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                comp.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
                comp.setFilterMode(Qt.MatchFlag.MatchContains)
                editor.setCompleter(comp)
        except Exception:
            pass

    def setModelData(self, editor, model, index):
        clean = _PID_ICON_RE.sub('', editor.text().strip())
        model.setData(index, clean, Qt.ItemDataRole.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        r = option.rect
        col = index.column()
        if col == self._panel._C_ORS:
            # Editor sits in the description area, below the top strip
            _STRIP_H = _ORS_STRIP_H
            r = option.rect
            editor.setGeometry(QRect(r.left() + 2, r.top() + _STRIP_H,
                                     max(10, r.width() - 4),
                                     max(10, r.height() - _STRIP_H)))
            return
        elif col == self._panel._C_KON:
            offset = _PID_ICON_W + _KON_CAT_W
            editor.setGeometry(QRect(r.left() + offset, r.top(),
                                     max(10, r.width() - offset), r.height()))
            return
        elif col == self._panel._C_SG:
            # 2026-08-10 fix: this used to span the full remaining width,
            # visually covering the RRF badge (_RRF_W) while editing.
            editor.setGeometry(QRect(r.left() + _PID_ICON_W, r.top(),
                                     max(10, r.width() - _PID_ICON_W - _RRF_W),
                                     r.height()))
            return
        else:
            offset = _PID_ICON_W
        editor.setGeometry(QRect(r.left() + offset, r.top(),
                                 max(10, r.width() - offset), r.height()))

    def paint(self, painter, option, index):
        row, col = index.row(), index.column()
        sel = bool(option.state & QStyle.StateFlag.State_Selected)

        # ── Safeguard cells: side-by-side description | RRF badge ───────────
        if col == self._panel._C_SG:
            rrf = index.data(Qt.ItemDataRole.UserRole + 1)
            if rrf is not None:
                r = option.rect
                painter.save()
                # Background
                if sel:
                    painter.fillRect(r, option.palette.highlight())
                elif row % 2 == 1:
                    painter.fillRect(r, option.palette.alternateBase())
                else:
                    painter.fillRect(r, option.palette.base())

                # Tag data still tracked (drives pin color below) but no
                # longer shown as its own strip (2026-08-10, see NOTES.md
                # "ta bort tagg remsa") — a dragged-in tag now only shows
                # inline, bolded in the description text.
                comp_type, comp_tag = index.data(Qt.ItemDataRole.UserRole + 6) or ('', '')
                has_tag = bool(comp_tag or comp_type)
                body_top = r.top()
                body_h   = r.height()

                # Layout: [pin 22px][description ...][RRF badge 54px]
                desc_w    = r.width() - _PID_ICON_W - _RRF_W
                # Pin anchored to the TOP of the cell (2026-08-11: "det vore
                # snyggt om nålpluppen i HAZOP scenario stod i överkant") —
                # _draw_pid_pin centers itself within whatever rect it's
                # given, so a rect spanning the FULL (possibly tall) row
                # left it drifting to the vertical middle instead.
                pin_rect  = QRect(r.left(), body_top, _PID_ICON_W, min(body_h, _PID_ICON_W))
                desc_rect = QRect(r.left() + _PID_ICON_W, body_top, desc_w, body_h)
                rrf_rect  = QRect(r.right() - _RRF_W, body_top, _RRF_W, body_h)

                # Description text (elided to one line), drag-appended tags
                # in bold (2026-08-09, see NOTES.md "fetmarkera objekttexten")
                desc = index.data(Qt.ItemDataRole.DisplayRole) or ''
                tc = (option.palette.highlightedText().color() if sel
                      else option.palette.text().color())
                tagged_refs = index.data(Qt.ItemDataRole.UserRole + 7) or []
                _draw_text_with_bold_tags(
                    painter, desc_rect.adjusted(2, 2, -2, -2), desc,
                    tagged_refs, option.font, tc, word_wrap=False)

                # RRF badge (right column)
                badge_bg = QColor('#17191C') if sel else QColor('#F5F5F3')
                painter.fillRect(rrf_rect, badge_bg)
                badge_tc = QColor('#ffffff') if sel else QColor('#17191C')
                painter.setPen(badge_tc)
                badge_font = QFont(option.font)
                badge_font.setPointSize(max(6, option.font.pointSize() - 2))
                badge_font.setBold(True)
                painter.setFont(badge_font)
                painter.drawText(rrf_rect.adjusted(2, 1, -2, -1),
                                 Qt.AlignmentFlag.AlignCenter,
                                 f"RRF\n{rrf}")

                # Separator line between description and badge
                painter.setPen(QPen(QColor('#bcd'), 1))
                painter.drawLine(rrf_rect.left(), r.top(), rrf_rect.left(), r.bottom())

                # Amber ○ indicator when safeguard excluded from any
                # category — muted to match the app's near-monochrome
                # theme (2026-08-09, see NOTES.md) instead of a pure,
                # saturated gold that stood out against everything else.
                excl_cats = index.data(Qt.ItemDataRole.UserRole + 2) or []
                if excl_cats:
                    csz = 9
                    circ = QRect(rrf_rect.right() - csz - 2,
                                 rrf_rect.top() + 2, csz, csz)
                    painter.setBrush(QBrush(QColor('#F5C97A')))
                    painter.setPen(QPen(QColor('#B8860B'), 1))
                    painter.drawEllipse(circ)
                    painter.setBrush(Qt.BrushStyle.NoBrush)

                # Pin icon — green once EITHER a real P&ID marker exists OR
                # an object has been drag-appended (2026-08-09, see
                # NOTES.md: "drar jag in ett objekt ... så skall ju pluppen
                # ändras från röd till grön") — a dragged-in object is a
                # real connection to the P&ID (the equipment marker itself
                # is placed there) even without a separate marker of its
                # own for this row.
                if self._panel._cell_has_item(row, col):
                    _draw_pid_pin(painter, pin_rect,
                                 self._panel._is_cell_placed(row, col) or has_tag)

                painter.restore()
                return

        # ── Cause cells: top strip [pin|tag|freq|dots] + description below ────────
        if col == self._panel._C_ORS:
            obj_data = index.data(Qt.ItemDataRole.UserRole + 2)
            if obj_data is not None:
                comp_type, comp_tag = obj_data
                has_tag    = bool(comp_tag or comp_type)
                # freq_val/base_freq_per_year read inside
                # _ors_tag_zone_geometry() below (shared with the click
                # hit-test in eventFilter()) rather than here.
                status_icon = index.data(Qt.ItemDataRole.UserRole + 6) or ''

                meta_      = self._panel._row_meta
                _cause_id  = meta_[row][1] if row < len(meta_) else None
                _has_comment = False
                if _cause_id:
                    try:
                        c = self._panel.db.get_cause_comment(_cause_id)
                        _has_comment = bool(c and c.strip())
                    except Exception:
                        pass

                r = option.rect
                painter.save()

                # Cell background
                if sel:
                    painter.fillRect(r, option.palette.highlight())
                elif row % 2 == 1:
                    painter.fillRect(r, option.palette.alternateBase())
                else:
                    painter.fillRect(r, option.palette.base())

                # ── Vertical split ────────────────────────────────────────────
                _SH = _ORS_STRIP_H   # strip height (top row)
                strip_rect = QRect(r.left(), r.top(), r.width(), _SH)
                desc_rect  = QRect(r.left() + 2, r.top() + _SH,
                                   r.width() - 4, max(0, r.height() - _SH))

                # Strip background
                if not sel:
                    painter.fillRect(strip_rect, QColor('#F5F5F3'))
                else:
                    painter.fillRect(strip_rect,
                                     option.palette.highlight().color().darker(110))

                # Separator line between strip and description
                painter.setPen(QPen(QColor('#bcd'), 1))
                painter.drawLine(r.left(), r.top() + _SH, r.right(), r.top() + _SH)

                # ── Pin icon (left of strip) — green once EITHER a real
                # P&ID marker exists OR an object has been drag-appended
                # (2026-08-09, see NOTES.md) ────────────────────────────
                pin_rect = QRect(r.left(), r.top(), _PID_ICON_W, _SH)
                if _cause_id is not None:
                    _draw_pid_pin(painter, pin_rect,
                                 self._panel._is_cell_placed(row, col) or has_tag)
                else:
                    _draw_pid_pin(painter, pin_rect, False)

                # ── Tag + frequency geometry (shared with the click hit-test
                # in eventFilter() via _ors_tag_zone_geometry — see its
                # docstring for why: 2026-08-11, "tag numret klipps av ...
                # högerställ frekvens". The frequency zone is anchored to
                # the dots margin at the strip's right edge FIRST; the tag
                # zone then gets whatever room that leaves, instead of
                # being capped at the fixed _cause_obj_w divider width
                # regardless of free space. ─────────────────────────────
                tag_x = r.left() + _PID_ICON_W
                tag_zone_w, freq_zone_x, freq_zone_w, freq_str = \
                    self._panel._ors_tag_zone_geometry(index, tag_x, r.right())

                # ── Tag number (bold, left-aligned, now sized to leftover space)
                tag_label = comp_tag or ''
                tf = QFont(option.font)
                tf.setPointSize(max(6, option.font.pointSize() - 1))
                tf.setBold(True)
                painter.setFont(tf)
                tfm = painter.fontMetrics()
                if has_tag:
                    tag_w = min(tfm.horizontalAdvance(tag_label) + 6, tag_zone_w)
                    tag_draw_rect = QRect(tag_x, r.top(), tag_w, _SH)
                    tag_tc = (option.palette.highlightedText().color() if sel
                              else QColor('#17191C'))
                    painter.setPen(tag_tc)
                    painter.drawText(tag_draw_rect.adjusted(2, 0, -1, 0),
                                     Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                                     tfm.elidedText(tag_label,
                                                    Qt.TextElideMode.ElideRight,
                                                    tag_draw_rect.width() - 3))

                # ── Frequency label — right-anchored against the dots, not
                # left-aligned right after the tag, so it no longer strands
                # blank space between itself and the dots (the user's
                # actual complaint: that stranded space is what the tag
                # needed).
                if freq_str is not None:
                    ff = QFont(option.font)
                    ff.setPointSize(max(6, option.font.pointSize() - 1))
                    painter.setFont(ff)
                    f_tc = (option.palette.highlightedText().color() if sel
                            else QColor('#17191C'))
                    painter.setPen(f_tc)
                    freq_draw_rect = QRect(freq_zone_x, r.top(), freq_zone_w, _SH)
                    painter.drawText(freq_draw_rect.adjusted(0, 0, -3, 0),
                                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                                     painter.fontMetrics().elidedText(
                                         freq_str, Qt.TextElideMode.ElideRight,
                                         freq_zone_w - 3))

                # ── Status + comment dots (right of strip) ─────────────────────
                _STATUS_COLORS = {
                    '🟢': QColor('#16a34a'), '🟡': QColor('#ca8a04'),
                    '🟠': QColor('#ea580c'), '🔴': QColor('#dc2626'),
                }
                dot_r = 4
                dot_y = r.top() + _SH // 2
                dot_x = r.right() - 5
                if _has_comment:
                    painter.setBrush(QBrush(QColor('#17191C')))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawEllipse(QRect(dot_x - dot_r, dot_y - dot_r,
                                              dot_r * 2, dot_r * 2))
                    dot_x -= dot_r * 2 + 3
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                if status_icon in _STATUS_COLORS:
                    painter.setBrush(QBrush(_STATUS_COLORS[status_icon]))
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawEllipse(QRect(dot_x - dot_r, dot_y - dot_r,
                                              dot_r * 2, dot_r * 2))
                    painter.setBrush(Qt.BrushStyle.NoBrush)

                # ── Description text (below strip, full width) ─────────────────
                desc = index.data(Qt.ItemDataRole.DisplayRole) or ''
                painter.setFont(option.font)
                tc = (option.palette.highlightedText().color() if sel
                      else option.palette.text().color())
                painter.setPen(tc)
                painter.drawText(desc_rect.adjusted(0, 1, 0, -1),
                                 Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop |
                                 Qt.TextFlag.TextWordWrap, desc)

                painter.restore()
                return

        # ── Consequence cells: [pin][cat-badge][description] ──────────────────
        if col == self._panel._C_KON:
            con_data = index.data(Qt.ItemDataRole.UserRole)
            if con_data and con_data[0] == 'consequence':
                r = option.rect
                painter.save()
                if sel:
                    painter.fillRect(r, option.palette.highlight())
                elif row % 2 == 1:
                    painter.fillRect(r, option.palette.alternateBase())
                else:
                    painter.fillRect(r, option.palette.base())

                # Tag data still tracked (drives pin color below) but no
                # longer shown as its own strip (2026-08-10, see NOTES.md
                # "ta bort tagg remsa") — a dragged-in tag now only shows
                # inline, bolded in the description text.
                comp_type, comp_tag = index.data(Qt.ItemDataRole.UserRole + 7) or ('', '')
                has_tag = bool(comp_tag or comp_type)
                body_top = r.top()
                body_h   = r.height()

                # Pin anchored to the TOP of the cell, not centered across a
                # possibly tall wrapped-text row — see the SG cell's
                # identical fix above for the full rationale.
                pin_rect   = QRect(r.left(), body_top, _PID_ICON_W, min(body_h, _PID_ICON_W))
                cat_rect   = QRect(r.left() + _PID_ICON_W, body_top, _KON_CAT_W, body_h)
                txt_rect   = QRect(r.left() + _PID_ICON_W + _KON_CAT_W, body_top,
                                   r.width() - _PID_ICON_W - _KON_CAT_W, body_h)

                # Category badges — stacked vertically, one per category
                n_cats      = index.data(Qt.ItemDataRole.UserRole + 4) or 0
                all_cats    = index.data(Qt.ItemDataRole.UserRole + 5) or []
                if all_cats:
                    n         = len(all_cats)
                    badge_h   = max(14, cat_rect.height() // n)
                    cf = QFont(option.font)
                    cf.setPointSize(max(6, option.font.pointSize() - 2))
                    cf.setBold(True)
                    painter.setFont(cf)
                    for i, (cat_id, sev_id, cat_name, cat_sev) in enumerate(all_cats):
                        badge = QRect(cat_rect.left() + 2,
                                      cat_rect.top() + i * badge_h,
                                      cat_rect.width() - 4,
                                      badge_h - 1)
                        badge_tc = (option.palette.highlightedText().color() if sel
                                    else option.palette.text().color())
                        painter.setPen(badge_tc)
                        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter,
                                         f"{cat_name[:3]} {cons_axis_label(cat_sev)}")
                elif n_cats > 0:
                    painter.setPen(QColor('#17191C'))
                    f2 = QFont(option.font)
                    f2.setPointSize(max(6, option.font.pointSize() - 1))
                    painter.setFont(f2)
                    painter.drawText(cat_rect, Qt.AlignmentFlag.AlignCenter, f"📊{n_cats}")
                # else: no category assessment yet — leave the zone blank
                # rather than showing a muted "📊" placeholder on every
                # single uncategorized row (2026-08-10, see NOTES.md
                # "det känns lite plottrigt"; reduce chrome for unused
                # features instead of always reserving visual weight for
                # them).

                painter.setPen(QPen(QColor('#ddd'), 1))
                painter.drawLine(cat_rect.right(), r.top(), cat_rect.right(), r.bottom())

                # Description text — word-wrapped, drag-appended tags in
                # bold (2026-08-09, see NOTES.md "fetmarkera objekttexten")
                display = index.data(Qt.ItemDataRole.DisplayRole) or ''
                tc = (option.palette.highlightedText().color() if sel
                      else option.palette.text().color())
                tagged_refs = index.data(Qt.ItemDataRole.UserRole + 8) or []
                _draw_text_with_bold_tags(
                    painter, txt_rect.adjusted(2, 2, -2, -2), display,
                    tagged_refs, option.font, tc, word_wrap=True)

                # Pin icon — green once EITHER a real P&ID marker exists OR
                # an object has been drag-appended (2026-08-09, see NOTES.md)
                meta = self._panel._row_meta
                _cid = meta[row][2] if row < len(meta) else None
                if _cid is not None:
                    _draw_pid_pin(painter, pin_rect,
                                 self._panel._is_cell_placed(row, col) or has_tag)
                else:
                    _draw_pid_pin(painter, pin_rect, False)
                painter.restore()
                return

        # ── Default: shift content right, draw pin on left ────────────────────
        opt = QStyleOptionViewItem(option)
        opt.rect = option.rect.adjusted(_PID_ICON_W, 0, 0, 0)
        super().paint(painter, opt, index)
        icon_rect = QRect(option.rect.left(), option.rect.top(),
                          _PID_ICON_W, option.rect.height())
        if sel:
            painter.fillRect(icon_rect, option.palette.highlight())
        elif row % 2 == 1:
            painter.fillRect(icon_rect, option.palette.alternateBase())
        else:
            painter.fillRect(icon_rect, option.palette.base())
        if not self._panel._cell_has_item(row, col):
            return
        _draw_pid_pin(painter, icon_rect, self._panel._is_cell_placed(row, col))


class SgRRFCategoryPopup(QDialog):
    """Popup: change a safeguard's RRF, type, per-category and per-cause exclusions."""

    def __init__(self, db, sg_id, current_rrf, current_sg_type,
                 sev_cat_list, cause_list=None, parent=None):
        super().__init__(parent)
        self.db              = db
        self._sg_id          = sg_id
        self._current_rrf    = current_rrf
        self._current_type   = current_sg_type or 'Övrigt'
        self._sev_cat_list   = sev_cat_list    # [(sev_id, cat_name), ...]
        self._cause_list     = cause_list or [] # [(cause_id, desc, is_chain), ...]
        self._cat_checks:   dict[int, QCheckBox] = {}
        self._cause_checks: dict[int, QCheckBox] = {}
        self.setWindowTitle("Barriär — RRF & tillämpning")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._build()

    def _build(self):
        excl_by_sev   = {sev_id: self._sg_id in self.db.get_severity_excluded_sgs(sev_id)
                         for sev_id, _ in self._sev_cat_list}
        excl_cause_ids = self.db.get_safeguard_excluded_causes(self._sg_id)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(5)

        title = QLabel("RRF & tillämpning")
        tf = QFont(); tf.setBold(True); tf.setPointSize(9)
        title.setFont(tf)
        outer.addWidget(title)

        # Type selector
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Typ:"))
        self._type_combo = QComboBox()
        self._type_combo.addItems(SG_TYPES)
        idx = self._type_combo.findText(self._current_type)
        if idx >= 0:
            self._type_combo.setCurrentIndex(idx)
        self._type_combo.setStyleSheet("font-size:10px;")
        type_row.addWidget(self._type_combo)
        outer.addLayout(type_row)

        # RRF preset buttons + custom spinbox
        rrf_lbl = QLabel("RRF:")
        rrf_lbl.setStyleSheet("font-size:9px; color:#666;")
        outer.addWidget(rrf_lbl)
        presets = QHBoxLayout()
        for v in (1, 10, 100, 1000, 10000):
            btn = QPushButton(str(v))
            btn.setFixedWidth(52)
            btn.setStyleSheet(
                "QPushButton{background:#17191C;color:white;border:none;"
                "border-radius:3px;padding:3px;font-weight:bold;font-size:9px;}"
                "QPushButton:hover{background:#2A2E34;}")
            btn.clicked.connect(lambda _, v=v: self._spin.setValue(v))
            presets.addWidget(btn)
        outer.addLayout(presets)
        spin_row = QHBoxLayout()
        spin_row.addWidget(QLabel("Eget:"))
        self._spin = QSpinBox()
        self._spin.setRange(1, 1_000_000)
        self._spin.setValue(self._current_rrf)
        self._spin.setStyleSheet("font-size:10px;")
        spin_row.addWidget(self._spin)
        outer.addLayout(spin_row)

        if self._sev_cat_list:
            sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("color:#ddd;"); outer.addWidget(sep)
            lbl = QLabel("Gäller ej för kategori:")
            lbl.setStyleSheet("font-size:9px; color:#666;")
            outer.addWidget(lbl)
            for sev_id, cat_name in self._sev_cat_list:
                cb = QCheckBox(f"{cat_name}")
                cb.setStyleSheet("font-size:10px;")
                cb.setChecked(excl_by_sev.get(sev_id, False))
                self._cat_checks[sev_id] = cb
                outer.addWidget(cb)

        if self._cause_list:
            sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
            sep3.setStyleSheet("color:#ddd;"); outer.addWidget(sep3)
            lbl2 = QLabel("Gäller ej för orsak:")
            lbl2.setStyleSheet("font-size:9px; color:#666;")
            outer.addWidget(lbl2)
            for cause_id, desc, is_chain in self._cause_list:
                prefix = "⛓ " if is_chain else "⚙ "
                label  = f"{prefix}{desc[:40]}"
                cb = QCheckBox(label)
                cb.setStyleSheet("font-size:10px;")
                cb.setChecked(cause_id in excl_cause_ids)
                self._cause_checks[cause_id] = cb
                outer.addWidget(cb)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color:#ddd;"); outer.addWidget(sep2)

        btn_row = QHBoxLayout()
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.setStyleSheet(
            "QPushButton{font-size:10px;padding:2px 12px;"
            "background:#17191C;color:white;border-radius:3px;}"
            "QPushButton:hover{background:#2A2E34;}")
        ok.clicked.connect(self._ok)
        cancel = QPushButton("Avbryt")
        cancel.setStyleSheet("font-size:10px; padding:2px 8px;")
        cancel.clicked.connect(self.reject)
        btn_row.addStretch(); btn_row.addWidget(cancel); btn_row.addWidget(ok)
        outer.addLayout(btn_row)

    def _ok(self):
        new_rrf  = self._spin.value()
        new_type = self._type_combo.currentText()
        if new_rrf != self._current_rrf or new_type != self._current_type:
            self.db.update_safeguard(self._sg_id, rrf=new_rrf, sg_type=new_type)

        # Save category exclusions
        for sev_id, cb in self._cat_checks.items():
            excl_set = self.db.get_severity_excluded_sgs(sev_id)
            if cb.isChecked():
                excl_set.add(self._sg_id)
            else:
                excl_set.discard(self._sg_id)
            self.db.set_severity_excluded_sgs(sev_id, excl_set)

        # Save cause exclusions
        excl_cause_ids = {cid for cid, cb in self._cause_checks.items() if cb.isChecked()}
        self.db.set_safeguard_excluded_causes(self._sg_id, excl_cause_ids)

        self.accept()


class CatSGSelectionPopup(QDialog):
    """Popup: select which safeguards apply for a category-row (gäller ej för)."""

    def __init__(self, db, severity_id, all_sgs, parent=None):
        super().__init__(parent)
        self.db = db
        self._sev_id = severity_id
        self._all_sgs = all_sgs
        self.setWindowTitle("Barriärer — gäller ej för")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._checks: dict[int, QCheckBox] = {}
        self._build()

    def _build(self):
        excluded = self.db.get_severity_excluded_sgs(self._sev_id)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        title = QLabel("Barriärer för detta scenario")
        tf = QFont(); tf.setBold(True); tf.setPointSize(9)
        title.setFont(tf)
        outer.addWidget(title)

        note = QLabel("Avmarkera 'Gäller ej' för barriärer som inte gäller denna kategori.")
        note.setStyleSheet("font-size:9px; color:#666;")
        note.setWordWrap(True)
        outer.addWidget(note)

        if not self._all_sgs:
            outer.addWidget(QLabel("Inga barriärer tillagda ännu."))
        for sg in self._all_sgs:
            sg_id = sg['id']
            rrf   = sg.get('rrf', 1) or 1
            cb = QCheckBox(f"{sg['description']}  (RRF {rrf})")
            cb.setStyleSheet("font-size:10px;")
            cb.setChecked(sg_id not in excluded)
            self._checks[sg_id] = cb
            outer.addWidget(cb)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#ddd;"); outer.addWidget(sep)

        btn_row = QHBoxLayout()
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.setStyleSheet(
            "QPushButton{font-size:10px;padding:2px 12px;"
            "background:#17191C;color:white;border-radius:3px;}"
            "QPushButton:hover{background:#2A2E34;}")
        ok.clicked.connect(self._ok)
        cancel = QPushButton("Avbryt")
        cancel.setStyleSheet("font-size:10px; padding:2px 8px;")
        cancel.clicked.connect(self.reject)
        btn_row.addStretch(); btn_row.addWidget(cancel); btn_row.addWidget(ok)
        outer.addLayout(btn_row)

    def _ok(self):
        excluded = [sg_id for sg_id, cb in self._checks.items() if not cb.isChecked()]
        self.db.set_severity_excluded_sgs(self._sev_id, excluded)
        self.accept()


class ConsCategoryMatrixPopup(QDialog):
    """Small popup: select severity per consequence category, one row per category."""

    def __init__(self, db: 'Database', consequence_id: int, parent=None):
        super().__init__(parent)
        self.db = db
        self._cons_id = consequence_id
        self.setWindowTitle("Konsekvens per kategori")
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._sel: dict[int, int] = {}
        self._buttons: dict[tuple, QPushButton] = {}
        self._build()

    def _build(self):
        cats  = [dict(r) for r in self.db.consequence_categories()]
        saved = {r['category_id']: r['severity']
                 for r in self.db.get_consequence_severities(self._cons_id)}
        mat   = self.db.get_risk_matrix() or DEFAULT_MATRIX
        n_sev = mat.get('n_consequences', 5)

        self._sel = {c['id']: saved.get(c['id'], 0) for c in cats}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        title = QLabel("Konsekvens per kategori")
        tf = QFont(); tf.setBold(True); tf.setPointSize(9)
        title.setFont(tf)
        outer.addWidget(title)

        # Header: severity labels
        hdr = QHBoxLayout(); hdr.setSpacing(2)
        pad = QLabel(); pad.setFixedWidth(88)
        hdr.addWidget(pad)
        for s in range(1, n_sev + 1):
            lbl = QLabel(cons_axis_label(s))
            lbl.setFixedWidth(42)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("font-size:9px; color:#555;")
            hdr.addWidget(lbl)
        outer.addLayout(hdr)

        # One row per category
        for cat in cats:
            cid = cat['id']
            row_l = QHBoxLayout(); row_l.setSpacing(2); row_l.setContentsMargins(0,0,0,0)
            name_l = QLabel(cat['name'])
            name_l.setFixedWidth(88)
            name_l.setStyleSheet("font-size:10px;")
            row_l.addWidget(name_l)
            for s in range(1, n_sev + 1):
                btn = QPushButton()
                btn.setFixedSize(42, 22)
                btn.setCheckable(True)
                btn.setChecked(self._sel.get(cid, 0) == s)
                btn.setStyleSheet(self._bstyle(btn.isChecked()))
                btn.clicked.connect(lambda _, ci=cid, sv=s: self._toggle(ci, sv))
                self._buttons[(cid, s)] = btn
                row_l.addWidget(btn)
            outer.addLayout(row_l)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#ddd;"); outer.addWidget(sep)

        btn_row = QHBoxLayout()
        clr = QPushButton("Rensa alla")
        clr.setStyleSheet("font-size:10px; padding:2px 8px;")
        clr.clicked.connect(self._clear)
        ok  = QPushButton("OK")
        ok.setDefault(True)
        ok.setStyleSheet(
            "QPushButton{font-size:10px;padding:2px 12px;"
            "background:#17191C;color:white;border-radius:3px;}"
            "QPushButton:hover{background:#2A2E34;}")
        ok.clicked.connect(self._ok)
        cancel = QPushButton("Avbryt")
        cancel.setStyleSheet("font-size:10px; padding:2px 8px;")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(clr); btn_row.addStretch()
        btn_row.addWidget(cancel); btn_row.addWidget(ok)
        outer.addLayout(btn_row)

    @staticmethod
    def _bstyle(selected: bool) -> str:
        if selected:
            return ("QPushButton{background:#17191C;color:white;"
                    "border:2px solid #17191C;border-radius:3px;"
                    "font-size:9px;font-weight:bold;}"
                    "QPushButton:hover{background:#2A2E34;}")
        return ("QPushButton{background:#F5F5F3;color:#17191C;"
                "border:1px solid #CFD1CE;border-radius:3px;font-size:9px;}"
                "QPushButton:hover{background:#E8E9E6;border:1px solid #B3B7B2;}")

    def _toggle(self, cat_id: int, sev: int):
        self._sel[cat_id] = 0 if self._sel.get(cat_id) == sev else sev
        self._refresh()

    def _clear(self):
        self._sel = {k: 0 for k in self._sel}
        self._refresh()

    def _refresh(self):
        for (cid, s), btn in self._buttons.items():
            selected = self._sel.get(cid, 0) == s
            btn.setChecked(selected)
            btn.setStyleSheet(self._bstyle(selected))

    def _ok(self):
        for cat_id, sev in self._sel.items():
            self.db.set_consequence_severity(self._cons_id, cat_id, sev)
        self.accept()


class _LopaWidget(QWidget):
    """Compact stacked FA / Antändning / Övriga cell widget for ScenarioTablePanel."""

    changed = pyqtSignal(int)   # emits cons_id after any save

    def __init__(self, db: 'Database', cons_id: int,
                 fa_active: bool, fa_rrf,
                 ign_active: bool, ign_rrf,
                 n_extra: int, parent=None):
        super().__init__(parent)
        self.db       = db
        self.cons_id  = cons_id
        self._saving  = False

        _ROW_H = 16   # fixed height per mini-row — keeps widget compact

        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(0)

        _cb_ss  = "font-size:8pt;"
        _ed_ss  = "font-size:8pt; padding:0px 1px;"
        _btn_ss = "font-size:7pt; text-align:left; padding:0px 2px; border:none;"

        def _pct_edit(val):
            e = QLineEdit(str(val))
            e.setMaximumWidth(36); e.setMinimumWidth(28)
            e.setFixedHeight(_ROW_H)
            e.setAlignment(Qt.AlignmentFlag.AlignRight)
            e.setStyleSheet(_ed_ss)
            return e

        def _cb(label, checked, tip):
            c = QCheckBox(label)
            c.setChecked(checked)
            c.setToolTip(tip)
            c.setFixedHeight(_ROW_H)
            c.setStyleSheet(_cb_ss)
            return c

        # FA row
        fa_row = QHBoxLayout()
        fa_row.setContentsMargins(0, 0, 0, 0); fa_row.setSpacing(2)
        self._fa_cb   = _cb("FA", bool(fa_active), "Närvaro/FA-sannolikhet\n10%=−1 steg")
        self._fa_edit = _pct_edit(fa_rrf)
        self._fa_edit.setToolTip("Sannolikhet i % (t.ex. 10 eller 1)")
        fa_pct = QLabel("%"); fa_pct.setFixedWidth(10); fa_pct.setStyleSheet(_cb_ss)
        fa_row.addWidget(self._fa_cb); fa_row.addStretch()
        fa_row.addWidget(self._fa_edit); fa_row.addWidget(fa_pct)
        lay.addLayout(fa_row)

        # Antändning row
        ign_row = QHBoxLayout()
        ign_row.setContentsMargins(0, 0, 0, 0); ign_row.setSpacing(2)
        self._ign_cb   = _cb("Ant.", bool(ign_active), "Antändningssannolikhet\n10%=−1 steg")
        self._ign_edit = _pct_edit(ign_rrf)
        self._ign_edit.setToolTip("Sannolikhet i % (t.ex. 10 eller 1)")
        ign_pct = QLabel("%"); ign_pct.setFixedWidth(10); ign_pct.setStyleSheet(_cb_ss)
        ign_row.addWidget(self._ign_cb); ign_row.addStretch()
        ign_row.addWidget(self._ign_edit); ign_row.addWidget(ign_pct)
        lay.addLayout(ign_row)

        # Övriga faktorer button
        self._extra_btn = QPushButton(
            f"+{n_extra} övr." if n_extra else "+ övriga")
        self._extra_btn.setFlat(True)
        self._extra_btn.setFixedHeight(_ROW_H)
        self._extra_btn.setStyleSheet(_btn_ss)
        lay.addWidget(self._extra_btn)

        self.setFixedHeight(_ROW_H * 3 + 2)

        self._fa_cb.toggled.connect(self._save)
        self._fa_edit.editingFinished.connect(self._save)
        self._ign_cb.toggled.connect(self._save)
        self._ign_edit.editingFinished.connect(self._save)

    def update_extra_count(self, n: int):
        self._extra_btn.setText(f"+ {n} övr." if n else "+ övriga")

    def _parse_pct(self, edit: 'QLineEdit') -> float:
        try:
            v = float(edit.text().replace('%', '').strip() or '10')
            return max(0.001, min(99.9, v))
        except ValueError:
            return 10.0

    def _save(self):
        if self._saving:
            return
        self._saving = True
        try:
            self.db.update_consequence_factors(
                self.cons_id,
                self._fa_cb.isChecked(),  self._parse_pct(self._fa_edit),
                self._ign_cb.isChecked(), self._parse_pct(self._ign_edit))
            self.changed.emit(self.cons_id)
        finally:
            self._saving = False


class ScenarioTablePanel(QWidget):
    """Extended scenario table with FA, Antändning, Övriga faktorer and Slutkonsekvens."""

    item_selected              = pyqtSignal(int, int)   # (type_, id_) — cell clicked → open right panel
    new_item_created           = pyqtSignal(int, int)   # (type_, id_) — after quick-add via Enter menu
    item_edited                = pyqtSignal(int, int)   # (type_, id_) — cell edit committed → sync right panel
    place_requested            = pyqtSignal(int, int)   # (type_, id_) — place/add marker
    navigate_to_pid            = pyqtSignal(int, int)   # (type_, id_) — navigate to existing marker
    remove_requested           = pyqtSignal(int, int)   # (type_, id_) — delete all markers
    add_causes_on_pid_requested = pyqtSignal(int)       # deviation_id — red pin click on empty ORS row
    structure_changed          = pyqtSignal()           # item moved/deleted/duplicated → refresh tree

    # Column indices
    _C_NOD, _C_UTR, _C_DEV, _C_ORS, _C_KON, _C_RFORE = 0, 1, 2, 3, 4, 5
    _C_SG, _C_LOPA, _C_SLUT                           = 6, 7, 8

    _COLS = [
        'Nod',
        'Utrustning',
        'Avvikelse',
        'Orsak  →',
        'Konsekvens',
        'Risk före barriär',
        'Barriärer  →',
        'FA / Ant. / Övriga',
        'Slutkonsekvens',
    ]

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.cause_id = None
        self._node_id = None
        self._deviation_id = None
        self._all_nodes = False  # if True, show every node's full hierarchy (set by load_all)
        self._show_empty_deviations = False  # if True, deviations with zero causes get a placeholder row instead of being omitted
        self._force_dev_column_visible = False  # if True, Avvikelse column stays visible regardless of _all_nodes (set by always_show_deviation_column)
        self._row_meta = []   # list of (dev_id, cause_id, cons_id, sg_id) per visible row
        self._cons_id  = None  # if set, show only this consequence (set by load_consequence)
        self._enter_row = -1
        self._enter_col = -1
        self._last_enter_committed = False
        self._cell_font_size = 9
        self._cause_obj_w = int(self.db.get_config('cause_obj_w', '64'))
        self._drag_obj_w_active = False
        self._drag_obj_w_start_x = 0
        self._drag_obj_w_start_w = 0
        # Parallel list to _row_meta: None or (cat_id, cat_name, cat_sev)
        self._row_cat_info: list = []
        self.setMinimumHeight(CONFIG['H_TABLE_STD'])
        # 380px cap fits the P&ID page's bottom-splitter usage, where this panel
        # shares vertical space with the canvas above it. A full-page host
        # (e.g. HAZOPWorksheet) should call allow_full_height() to lift this cap.
        self.setMaximumHeight(380)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 2, 4, 2)
        outer.setSpacing(2)

        hdr_row = QHBoxLayout()
        self._hdr_lbl = QLabel("HAZOP Scenario")
        f = QFont(); f.setBold(True); f.setPointSize(9)
        self._hdr_lbl.setFont(f)
        hdr_row.addWidget(self._hdr_lbl)
        hdr_row.addStretch()
        self._fill_chk = QCheckBox("Fyll skärm")
        self._fill_chk.setChecked(True)
        self._fill_chk.setToolTip("Sträck kolumnerna så tabellen fyller hela bredden")
        self._fill_chk.toggled.connect(self._apply_fill_mode)
        hdr_row.addWidget(self._fill_chk)
        hdr_row.addSpacing(8)
        hdr_row.addWidget(QLabel("Textstorlek:"))
        self._fs_spin = QSpinBox()
        self._fs_spin.setRange(7, 16)
        self._fs_spin.setValue(9)
        self._fs_spin.setSuffix(" pt")
        self._fs_spin.setFixedWidth(62)
        self._fs_spin.setToolTip("Teckenstorlek i scenario-tabellen")
        self._fs_spin.valueChanged.connect(self._on_font_size_changed)
        hdr_row.addWidget(self._fs_spin)
        outer.addLayout(hdr_row)

        self._table = QTableWidget(0, len(self._COLS))
        self._table.setHorizontalHeaderLabels(self._COLS)
        h = self._table.horizontalHeader()
        # NOD, UTR and DEV are hidden — context is shown in the header label instead
        self._table.setColumnHidden(self._C_NOD, True)
        self._table.setColumnHidden(self._C_UTR, True)
        self._table.setColumnHidden(self._C_DEV, True)
        resize_modes = {
            self._C_NOD:   (QHeaderView.ResizeMode.Interactive,  70),
            self._C_UTR:   (QHeaderView.ResizeMode.Interactive, 110),
            self._C_DEV:   (QHeaderView.ResizeMode.Interactive, 120),
            self._C_ORS:   (QHeaderView.ResizeMode.Interactive, 180),
            self._C_KON:   (QHeaderView.ResizeMode.Interactive, 180),
            self._C_RFORE: (QHeaderView.ResizeMode.Interactive,  85),
            self._C_SG:    (QHeaderView.ResizeMode.Interactive, 130),
            self._C_LOPA:  (QHeaderView.ResizeMode.Interactive, 130),
            self._C_SLUT:  (QHeaderView.ResizeMode.Interactive,  85),
        }
        for col, (mode, width) in resize_modes.items():
            h.setSectionResizeMode(col, mode)
            self._table.setColumnWidth(col, width)
        self._table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._table.verticalHeader().setVisible(False)
        # Compact row heights — category sub-rows should be tight
        _init_fm = QFontMetrics(self._table.font())
        _compact = _init_fm.height() + 4
        self._table.verticalHeader().setDefaultSectionSize(_compact)
        self._table.verticalHeader().setMinimumSectionSize(_compact)
        self._table.setAlternatingRowColors(True)
        self._table.setWordWrap(True)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:3px;}")
        self._table.cellChanged.connect(self._on_cell_changed)
        self._table.cellClicked.connect(self._on_cell_clicked)
        self._table.itemDoubleClicked.connect(self._on_cell_double_clicked)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        self._table.setAcceptDrops(True)
        self._table.installEventFilter(self)
        self._table.viewport().installEventFilter(self)
        # Drag state
        self._drag_press_pos  = None
        self._drag_press_row  = -1
        self._drag_press_col  = -1
        self._delegate = _ScenarioDelegate(self)
        self._table.setItemDelegate(self._delegate)
        self._pid_delegate = _PidDelegate(self)
        for col in (self._C_ORS, self._C_KON, self._C_SG):
            self._table.setItemDelegateForColumn(col, self._pid_delegate)
        self._table.viewport().installEventFilter(self)
        self._apply_fill_mode(True)   # default: stretch to fill screen

        # ── Persist "Fyll skärm" + manually-resized column widths
        # (2026-08-10, see NOTES.md) — previously reset to the hardcoded
        # defaults every time the app restarted.
        if self.db.get_config('scenario_fill_mode', '1') != '1':
            self._fill_chk.setChecked(False)   # triggers _apply_fill_mode(False) above
        self._fill_chk.toggled.connect(
            lambda checked: self.db.set_config('scenario_fill_mode', '1' if checked else '0'))
        saved_widths = self.db.get_config('scenario_col_widths', '')
        if saved_widths:
            try:
                for col_str, w in json.loads(saved_widths).items():
                    col = int(col_str)
                    if 0 <= col < self._table.columnCount():
                        self._table.setColumnWidth(col, w)
            except Exception:
                pass
        h.sectionResized.connect(self._on_column_resized)

        self._placed_causes       = set()
        self._placed_consequences = set()
        self._placed_safeguards   = set()

        # ── Sticky context bar — always shows current Nod + Avvikelse ──────────
        self._ctx_bar = QLabel()
        self._ctx_bar.setStyleSheet(
            "QLabel { background:#F5F5F3; color:#17191C; font-size:10px;"
            " padding:3px 8px; border-bottom:1px solid #E2E3E1; }")
        self._ctx_bar.setWordWrap(False)
        self._ctx_bar.hide()   # hidden until content is loaded
        outer.addWidget(self._ctx_bar)
        outer.addWidget(self._table)

        self._table.verticalScrollBar().valueChanged.connect(
            self._update_ctx_bar)

        # ── Deferred rebuild system (signal-based, not timer-based) ──────────────
        self._rebuild_pending = False

    def allow_full_height(self):
        """Lift the 380px cap so this panel fills whatever container it's
        placed in — for hosts that give it a whole page (e.g. HAZOPWorksheet),
        as opposed to the P&ID page's bottom splitter (which relies on the cap
        to leave room for the canvas above it)."""
        self.setMaximumHeight(16777215)  # Qt's QWIDGETSIZE_MAX — effectively "no cap"
        self._table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_show_empty_deviations(self, show: bool):
        """Toggle whether deviations with zero causes get their own placeholder
        row (interleaved with deviations that do have causes), instead of being
        silently omitted from the single-node/all-nodes view."""
        if self._show_empty_deviations == show:
            return
        self._show_empty_deviations = show
        self._rebuild()

    # ── Load ──────────────────────────────────────────────────────────────────

    def load_node(self, node_id):
        self._node_id = node_id
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = False
        self._set_all_nodes_columns_visible(False)
        self._rebuild()

    def load_deviation(self, deviation_id):
        dev = self.db.get_deviation(deviation_id)
        self._node_id = dev['node_id'] if dev else None
        self._deviation_id = deviation_id
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = False
        self._set_all_nodes_columns_visible(False)
        self._rebuild()

    def load_cause(self, cause_id):
        self._node_id = None
        self._deviation_id = None
        self.cause_id = cause_id
        self._cons_id = None
        self._all_nodes = False
        self._set_all_nodes_columns_visible(False)
        self._rebuild()

    def load_consequence(self, cons_id):
        row = self.db.get_consequence(cons_id)
        if row:
            self._node_id = None
            self._deviation_id = None
            self.cause_id = dict(row)['cause_id']
            self._cons_id = cons_id
            self._all_nodes = False
            self._set_all_nodes_columns_visible(False)
            self._rebuild()

    def load_all(self):
        """Show the entire study: every node's full deviation/cause/consequence/safeguard hierarchy."""
        self._node_id = None
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = True
        self._set_all_nodes_columns_visible(True)
        self._rebuild()

    def refresh(self):
        """Rebuild in place — keeps the current filter unchanged."""
        self._rebuild()

    def clear(self):
        self._node_id = None
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = False
        self._show_empty_deviations = False
        self._set_all_nodes_columns_visible(False)
        self._table.setRowCount(0)
        self._hdr_lbl.setText("HAZOP Scenario")

    def _set_all_nodes_columns_visible(self, visible: bool):
        """NOD/UTR/DEV columns are normally hidden (context shown in the
        sticky header bar / _hdr_lbl instead). In "all nodes" mode multiple
        nodes and deviations are interleaved in one table, so those columns
        must become visible so rows remain identifiable.

        `self._force_dev_column_visible` (set via always_show_deviation_column())
        keeps the Avvikelse AND Utrustning columns visible regardless of
        `visible` — for hosts like HAZOPWorksheet where deviation/equipment
        context should always be in the grid, not just in the sticky header
        bar. UTR follows DEV's visibility rather than getting its own flag:
        a node can have MULTIPLE equipment groups (unlike a single node,
        which is constant for every row in single-node view), so "show
        which deviation this row is" and "show which equipment it belongs
        to" are the same kind of per-row context, not per-node context."""
        self._table.setColumnHidden(self._C_NOD, not visible)
        self._table.setColumnHidden(
            self._C_DEV, not (visible or self._force_dev_column_visible))
        self._table.setColumnHidden(
            self._C_UTR, not (visible or self._force_dev_column_visible))

    def always_show_deviation_column(self):
        """Keep the Avvikelse (and Utrustning) column visible at all times,
        regardless of "Visa samtliga noder" / "Visa avvikelser utan orsaker"
        state — opt-in for hosts (e.g. HAZOPWorksheet) that want deviation
        context always shown in the grid itself."""
        self._force_dev_column_visible = True
        self._set_all_nodes_columns_visible(self._all_nodes)

    # Columns that stretch to fill remaining space in fill mode
    _STRETCH_COLS = None  # set after class constants are known

    def _apply_fill_mode(self, fill: bool = True):
        """Toggle between stretch-to-fill and interactive column widths."""
        h = self._table.horizontalHeader()
        stretch_cols = {self._C_ORS, self._C_KON, self._C_SG}
        fixed_widths = {
            self._C_RFORE: 85,
            self._C_LOPA:  130, self._C_SLUT: 85,
        }
        if fill:
            for col in stretch_cols:
                h.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
            for col, w in fixed_widths.items():
                h.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
                self._table.setColumnWidth(col, w)
            self._table.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            for col in stretch_cols:
                h.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            for col in fixed_widths:
                h.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self._table.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def _on_column_resized(self, col, old_size, new_size):
        """Persist manually-resized column widths (2026-08-10, see
        NOTES.md) — only meaningful for Interactive columns ("Fyll skärm"
        unchecked), but harmless to also record Stretch/Fixed-driven
        resizes since they'd just re-save the same hardcoded defaults."""
        try:
            saved = self.db.get_config('scenario_col_widths', '')
            widths = json.loads(saved) if saved else {}
        except Exception:
            widths = {}
        widths[str(col)] = new_size
        self.db.set_config('scenario_col_widths', json.dumps(widths))

    def _on_font_size_changed(self, size):
        self._cell_font_size = size
        f = QFont()
        f.setPointSize(size)
        self._table.setFont(f)
        # Keep default section size at one-line height so resizeRowToContents
        # can shrink rows freely.  Row heights are set by resizeRowsToContents
        # at the end of _rebuild.
        fm = QFontMetrics(f)
        self._table.verticalHeader().setDefaultSectionSize(fm.height() + 6)
        self._table.verticalHeader().setMinimumSectionSize(fm.height() + 4)
        self._rebuild()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _schedule_rebuild(self):
        """
        Schedule a deferred rebuild on the next event loop iteration.
        Prevents cascading timer-based deferred calls and ensures proper event ordering.
        Safe to call multiple times — queued as a single rebuild.
        """
        if self._rebuild_pending:
            return  # Already scheduled
        self._rebuild_pending = True
        QTimer.singleShot(0, self._on_rebuild_scheduled)

    def _on_rebuild_scheduled(self):
        """Called when rebuild is scheduled. Executes the deferred rebuild."""
        self._rebuild_pending = False
        self._rebuild()

    def _rebuild(self):
        """
        Orchestrate full table rebuild: clear, build rows, apply spans, resize, restore state.
        Re-entrancy safe via _rebuilding flag to prevent cascading rebuilds.
        """
        if getattr(self, '_rebuilding', False):
            return
        self._rebuilding = True
        try:
            # Save scroll position and suppress visual updates to prevent jumping
            _vscroll = self._table.verticalScrollBar().value()
            _hscroll = self._table.horizontalScrollBar().value()
            self._table.setUpdatesEnabled(False)
            try:
                self._table.cellChanged.disconnect()
            except Exception:
                pass
            self._table.blockSignals(True)
            try:
                self._table.clearSpans()
                # Proactively clear focus from any active cell editor (e.g. a
                # _LopaWidget QLineEdit) before tearing down rows. Destroying a
                # focused widget forces a synchronous focus-out, which would
                # fire editingFinished -> _save -> changed.emit -> _update_lopa_risk
                # reentrantly mid-teardown. The _rebuilding guard in
                # _update_lopa_risk covers this too, but avoiding the signal
                # firing at all is a cleaner first line of defense.
                focused = self._table.focusWidget()
                if focused is not None:
                    focused.clearFocus()
                logging.info('_rebuild: D — setRowCount(0)')
                self._table.setRowCount(0)
                logging.info('_rebuild: E — reset meta')
                self._row_meta = []
                self._row_cat_info = []

                # Build rows with signals blocked
                self._build_rows()
                logging.info('_rebuild: F — _build_rows() done (rowCount=%d)',
                             self._table.rowCount())

                # Reconnect signals before calling _apply_spans
                self._table.cellChanged.connect(self._on_cell_changed)

                # Apply row merging (spans)
                self._apply_spans()
                logging.info('_rebuild: G — _apply_spans() done')

                # Finalize: resize rows and restore scroll position
                self._resize_rows(_vscroll, _hscroll)
                logging.info('_rebuild: H — _resize_rows() done')
            finally:
                self._table.blockSignals(False)
        except Exception as e:
            logging.exception('_rebuild: Python exception')
            QMessageBox.critical(self, "Fel i scenariopanel", str(e))
        finally:
            self._rebuild_pending = False
            self._rebuilding = False
            self._update_ctx_bar()

    def _equipment_for_dev(self, dev_d):
        """(equipment_id, label) for a deviation dict's equipment_id, or
        (None, '') if it's not tied to a specific equipment — see NOTES.md
        "Nod → Utrustning → Avvikelse"."""
        eq_id = dev_d.get('equipment_id') if dev_d else None
        if not eq_id:
            return None, ''
        eq = self.db.get_equipment_by_id(eq_id)
        return eq_id, (f"{eq['tag']} — {eq['equipment_type']}" if eq else '')

    def _causes_for_node(self, node_id):
        """Return [(cause_dict, deviation_dict), ...] for every cause under
        every deviation of node_id, in deviation/cause order. Shared by the
        single-node branch of _build_rows() and the "all nodes" mode (used
        once per node, in node order) so both walk the exact same hierarchy."""
        result = []
        for dev in self.db.deviations(node_id):
            dev_d = dict(dev)
            causes = list(self.db.causes_for_deviation(dev['id']))
            if not causes:
                if self._show_empty_deviations:
                    result.append((None, dev_d))  # sentinel: deviation has no causes
                continue
            for c in causes:
                result.append((dict(c), dev_d))
        return result

    def _build_rows(self):
        """
        Build the scenario table rows from current filters (node, deviation, cause, consequence).
        Modifies self._table, self._row_meta, self._row_cat_info in place.
        Called with table signals blocked, so cellChanged won't fire during construction.
        """
        logging.info('_build_rows: F0 — entry (all_nodes=%s node_id=%s dev_id=%s '
                     'cause_id=%s cons_id=%s)',
                     self._all_nodes, self._node_id, self._deviation_id,
                     self.cause_id, self._cons_id)
        # Build list of (cause_dict, deviation_dict) to display
        causes_to_show = []
        if self._all_nodes:
            for node_row in self.db.nodes():
                causes_to_show.extend(self._causes_for_node(node_row['id']))
        elif self.cause_id is not None:
            c = self.db.get_cause(self.cause_id)
            if c:
                c_d = dict(c)
                dev = self.db.get_deviation(c_d.get('deviation_id'))
                causes_to_show = [(c_d, dict(dev) if dev else {'id': None, 'description': '—'})]
        elif self._deviation_id is not None:
            dev = self.db.get_deviation(self._deviation_id)
            dev_d = dict(dev) if dev else {'id': self._deviation_id, 'description': '—'}
            for c in self.db.causes_for_deviation(self._deviation_id):
                causes_to_show.append((dict(c), dev_d))
        elif self._node_id is not None:
            causes_to_show.extend(self._causes_for_node(self._node_id))

        logging.info('_build_rows: F1 — causes_to_show resolved (n=%d)', len(causes_to_show))

        if not causes_to_show:
            # Show placeholder rows so the user can start adding content
            if self._all_nodes:
                # No nodes (or no deviations/causes) anywhere in the study yet —
                # nothing sensible to show as a placeholder across all nodes.
                self._hdr_lbl.setText("HAZOP Scenario — Hela studien")
            elif self._deviation_id is not None:
                dev = self.db.get_deviation(self._deviation_id)
                if dev:
                    dev_d = dict(dev)
                    node  = self.db.get_node(dev_d['node_id'])
                    nn    = node['name'] if node else '?'
                    self._hdr_lbl.setText(f"HAZOP Scenario — {nn} / {dev_d['description']}")
                    self._add_placeholder_row(nn, dev_d)
            elif self._node_id is not None:
                node = self.db.get_node(self._node_id)
                nn   = node['name'] if node else '?'
                self._hdr_lbl.setText(f"HAZOP Scenario — {nn}")
                devs = list(self.db.deviations(self._node_id))
                if devs:
                    for dev in devs:
                        self._add_placeholder_row(nn, dict(dev))
                else:
                    self._add_placeholder_row(nn, None)
            logging.info('_build_rows: F2 — placeholder-only branch done, returning')
            return

        # Determine header title from first cause's node (or, for a sentinel
        # "empty deviation" entry — cause_d is None — fall back to its dev_d,
        # which always carries node_id).
        first_cause, first_dev = causes_to_show[0]
        first_node_id = first_cause['node_id'] if first_cause is not None else first_dev.get('node_id')
        node = self.db.get_node(first_node_id) if first_node_id else None
        node_name_hdr = node['name'] if node else '?'
        if self._all_nodes:
            self._hdr_lbl.setText("HAZOP Scenario — Hela studien")
        elif self._cons_id is not None:
            cons = self.db.get_consequence(self._cons_id)
            cons_desc = cons['description'] if cons else '?'
            _first_desc = first_cause.get('description', '?') if first_cause is not None else '?'
            self._hdr_lbl.setText(
                f"HAZOP Scenario — {node_name_hdr} / {_first_desc} / {cons_desc}")
        elif self._deviation_id is not None:
            dev = self.db.get_deviation(self._deviation_id)
            self._hdr_lbl.setText(
                f"HAZOP Scenario — {node_name_hdr} / {dev['description'] if dev else ''}")
        elif self.cause_id is not None:
            self._hdr_lbl.setText(
                f"HAZOP Scenario — {node_name_hdr} / {first_cause.get('description', '?')}")
        elif self._node_id is not None:
            self._hdr_lbl.setText(f"HAZOP Scenario — {node_name_hdr}")
        else:
            self._hdr_lbl.setText(f"HAZOP Scenario — {node_name_hdr}")

        logging.info('_build_rows: G0 — header set (%r)', self._hdr_lbl.text())
        self.refresh_placed()
        logging.info('_build_rows: G1 — refresh_placed done, entering cause loop (n=%d)',
                     len(causes_to_show))
        for _cause_idx, (cause_d, dev_d) in enumerate(causes_to_show):
            if cause_d is None:
                # Sentinel from _causes_for_node(): this deviation has zero causes,
                # but "Visa avvikelser utan orsaker" is on — show it as its own
                # placeholder row instead of skipping it.
                if _cause_idx % 10 == 0 or _cause_idx == len(causes_to_show) - 1:
                    logging.info('_build_rows: G2 — cause loop iter %d/%d (empty deviation, '
                                 'dev_id=%s)', _cause_idx, len(causes_to_show), dev_d.get('id'))
                node = self.db.get_node(dev_d.get('node_id')) if dev_d.get('node_id') else None
                node_name = node['name'] if node else '?'
                self._add_placeholder_row(node_name, dev_d)
                continue
            if _cause_idx % 10 == 0 or _cause_idx == len(causes_to_show) - 1:
                logging.info('_build_rows: G2 — cause loop iter %d/%d (cause_id=%s)',
                             _cause_idx, len(causes_to_show), cause_d.get('id'))
            node = self.db.get_node(cause_d['node_id'])
            node_name = node['name'] if node else '?'
            freq = self.db.cause_frequency_level(cause_d)
            _fi = freq_to_idx(freq)
            freq_lbl = FREQ_LABELS[_fi] if _fi < len(FREQ_LABELS) else f'F{freq}'
            first_row_for_cause = self._table.rowCount()
            all_cons = list(self.db.consequences(cause_d['id']))
            if self._cons_id is not None:
                all_cons = [c for c in all_cons if dict(c)['id'] == self._cons_id]
            for _cons_idx, cons in enumerate(all_cons):
                cons_d = dict(cons)
                logging.info('_build_rows: H0 — cause %s cons_idx %d/%d cons_id=%s',
                             cause_d.get('id'), _cons_idx, len(all_cons), cons_d.get('id'))
                sgs    = [dict(s) for s in self.db.safeguards(cons_d['id'])]
                cat_rows = [dict(r) for r in
                            self.db.get_consequence_severities(cons_d['id'])]
                n_cats = len(cat_rows)
                n_sgs  = len(sgs)
                n_rows = max(n_cats, n_sgs, 1)

                # Precompute exclusions per severity assessment
                cat_excl_map = {}           # sev_id → set of excluded sg_ids
                for _cr in cat_rows:
                    cat_excl_map[_cr['id']] = self.db.get_severity_excluded_sgs(_cr['id'])

                # Which safeguards are excluded from at least one category?
                any_excl_map = {}           # sg_id → list of category names
                for _sg in sgs:
                    any_excl_map[_sg['id']] = [
                        _cr['name'] for _cr in cat_rows
                        if _sg['id'] in cat_excl_map.get(_cr['id'], set())]

                # Which safeguards are excluded from this specific cause?
                cause_excl_sgs = set()
                for _sg in sgs:
                    excl_causes = self.db.get_safeguard_excluded_causes(_sg['id'])
                    if cause_d['id'] in excl_causes:
                        cause_excl_sgs.add(_sg['id'])

                # Category list for the RRF popup: [(sev_id, cat_name), ...]
                sev_cat_list = [(cr['id'], cr['name']) for cr in cat_rows]
                # Full category info for stacked badges in KON cell
                all_cat_infos = [(cr['category_id'], cr['id'],
                                  cr['name'], cr['severity']) for cr in cat_rows]
                # Cause list for the RRF popup
                _direct_cause = self.db.get_cause(cons_d.get('cause_id')) if cons_d.get('cause_id') else None
                cause_popup_list = []
                if _direct_cause:
                    cause_popup_list.append((dict(_direct_cause)['id'],
                                             dict(_direct_cause)['description'], False))

                logging.info('_build_rows: H1 — cons_id=%s about to add %d row(s) '
                             '(n_cats=%d n_sgs=%d)',
                             cons_d.get('id'), n_rows, n_cats, n_sgs)
                for i in range(n_rows):
                    sg_i    = sgs[i] if i < n_sgs else None
                    cr_i    = cat_rows[i] if i < n_cats else None
                    cat_info_i = ((cr_i['category_id'], cr_i['id'],
                                   cr_i['name'], cr_i['severity'])
                                  if cr_i else None)
                    excl_for_cat  = cat_excl_map.get(cr_i['id'], set()) if cr_i else set()
                    excl_cat_names = any_excl_map.get(sg_i['id'], []) if sg_i else []
                    logging.info('_build_rows: H2 — _add_row cons_id=%s row_i=%d/%d '
                                 '(will create _LopaWidget)',
                                 cons_d.get('id'), i, n_rows)
                    self._add_row(node_name, dev_d, cause_d, freq, freq_lbl,
                                  cons_d, sgs, sg_i,
                                  cat_info=cat_info_i,
                                  excl_cat_names=excl_cat_names,
                                  excl_for_cat=excl_for_cat,
                                  cause_excl_sgs=cause_excl_sgs,
                                  sev_cat_list=sev_cat_list,
                                  all_cat_infos=all_cat_infos,
                                  cause_popup_list=cause_popup_list,
                                  n_cats=n_cats)
                    logging.info('_build_rows: H3 — _add_row cons_id=%s row_i=%d done',
                                 cons_d.get('id'), i)
            if self._table.rowCount() == first_row_for_cause:
                logging.info('_build_rows: G3 — cause %s had no rows, adding empty row',
                             cause_d.get('id'))
                self._add_empty_row(node_name, dev_d, cause_d, freq, freq_lbl)
        logging.info('_build_rows: I0 — cause loop complete, rowCount=%d',
                     self._table.rowCount())

    def _apply_spans(self):
        """Merge consecutive rows that share the same Nod or Orsak."""
        n = self._table.rowCount()
        logging.info('_apply_spans: J0 — entry (rowCount=%d)', n)
        if n < 2:
            logging.info('_apply_spans: J1 — fewer than 2 rows, nothing to span')
            return

        def _span_col(col, key_fn):
            r = 0
            while r < n:
                k = key_fn(r)
                span = 1
                while r + span < n and key_fn(r + span) == k and k is not None:
                    span += 1
                if span > 1:
                    self._table.setSpan(r, col, span, 1)
                    item = self._table.item(r, col)
                    if item:
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                r += span

        def _meta(r, idx):
            return self._row_meta[r][idx] if r < len(self._row_meta) else None

        # Nod: group by node_id stored in UserRole
        _span_col(self._C_NOD, lambda r: (
            self._table.item(r, self._C_NOD).data(Qt.ItemDataRole.UserRole)
            if self._table.item(r, self._C_NOD) else None))
        logging.info('_apply_spans: J2 — NOD column spanned')

        # Utrustning: group by equipment_id stored in UserRole — spans ALL of
        # an equipment's deviation rows together (like NOD spans a whole
        # node), not just rows sharing one deviation. Rows with no equipment
        # (key None) never span, same as _span_col's general None handling.
        _span_col(self._C_UTR, lambda r: (
            self._table.item(r, self._C_UTR).data(Qt.ItemDataRole.UserRole)
            if self._table.item(r, self._C_UTR) else None))
        logging.info('_apply_spans: J2b — UTR column spanned')

        # Avvikelse: group by dev_id (index 0 in row_meta)
        _span_col(self._C_DEV, lambda r: _meta(r, 0))
        logging.info('_apply_spans: J3 — DEV column spanned')

        # Orsak: group by cause_id (index 1)
        _span_col(self._C_ORS, lambda r: _meta(r, 1))
        logging.info('_apply_spans: J4 — ORS column spanned')

        # Consequence-level columns: group by (cons_id, cat_id) so each
        # category assessment forms its own span group
        def _cat_key(r):
            cons_id  = _meta(r, 2)
            cat_info = self._row_cat_info[r] if r < len(self._row_cat_info) else None
            cat_id   = cat_info[0] if cat_info else None
            return (cons_id, cat_id)

        # KON and LOPA: span by cons_id (whole consequence merged)
        for col in (self._C_KON, self._C_LOPA):
            _span_col(col, lambda r: _meta(r, 2))
        logging.info('_apply_spans: J5 — KON/LOPA columns spanned')

        # RFORE, SLUT: span by (cons_id, cat_id)
        # → non-category rows all merge; per-category rows each stay separate
        for col in (self._C_RFORE, self._C_SLUT):
            _span_col(col, _cat_key)
        logging.info('_apply_spans: J6 — RFORE/SLUT columns spanned, done')

    def _resize_rows_manual(self):
        """
        Compute and apply each row's height directly in Python instead of
        calling QTableWidget.resizeRowsToContents() (see _resize_rows()
        docstring for why). Mirrors the logic _ScenarioDelegate.sizeHint()
        uses per-cell, but:

          - Only the columns that actually need wrapping-height computation
            (_C_ORS, _C_KON — see _ScenarioDelegate._size_hint_impl) run the
            expensive QFontMetrics.boundingRect() path; every other column
            is known to always report a fixed one-line height, so it's used
            directly without going through sizeHint()/the delegate at all.
            This keeps the cost roughly proportional to the old native call
            instead of paying full per-cell sizeHint() overhead for every
            column in tables with hundreds of rows (e.g. "all nodes" mode).
          - Hidden columns (_C_NOD/_C_DEV in single-node mode) are skipped.
          - The _C_LOPA column uses a fixed-height cell widget (_LopaWidget,
            setFixedHeight(_ROW_H*3+2) — see class _LopaWidget), not a
            delegate-painted item, so its height is read directly from the
            widget rather than computed via QFontMetrics.
        """
        table = self._table
        row_count = table.rowCount()
        col_count = table.columnCount()
        fm_font = table.font()
        fm = QFontMetrics(fm_font)
        one_line_h = fm.height() + 6
        wrap_cols = (self._C_ORS, self._C_KON)

        logging.info('_resize_rows_manual: L0 — entry (rows=%d, cols=%d)',
                     row_count, col_count)

        for row in range(row_count):
            max_h = one_line_h
            try:
                for col in range(col_count):
                    if table.isColumnHidden(col):
                        continue

                    if col == self._C_LOPA:
                        widget = table.cellWidget(row, col)
                        if widget is not None:
                            h = widget.sizeHint().height()
                            if h > max_h:
                                max_h = h
                        continue

                    if col == self._C_SG:
                        # SG's description never word-wraps — a single
                        # compact line is always enough.
                        if one_line_h > max_h:
                            max_h = one_line_h
                        continue

                    if col not in wrap_cols:
                        # Fixed one-line columns (matches _ScenarioDelegate's
                        # non-wrap branch) — no font-metric work needed.
                        continue

                    item = table.item(row, col)
                    text = item.text() if item is not None else ''
                    if not text:
                        continue

                    w = table.columnWidth(col)
                    if col == self._C_ORS:
                        cell_w = max(40, w - 6)
                        rect = fm.boundingRect(0, 0, cell_w, 10000,
                                              Qt.TextFlag.TextWordWrap, text)
                        h = _ORS_STRIP_H + max(one_line_h, rect.height() + 4)
                    else:   # self._C_KON
                        cell_w = max(40, w - _PID_ICON_W - _KON_CAT_W)
                        rect = fm.boundingRect(0, 0, cell_w, 10000,
                                              Qt.TextFlag.TextWordWrap, text)
                        h = max(one_line_h, rect.height() + 4)
                    if h > max_h:
                        max_h = h
            except Exception:
                # Defensive: this is user-facing rebuild code and a single
                # row's height computation should never take down the whole
                # rebuild. This can only catch genuine Python-level
                # exceptions (attribute errors, etc.) — it is not a safety
                # net for native crashes, since the whole point of this
                # method is to avoid the native resizeRowsToContents() path
                # that was pinpointed as the actual crash site.
                logging.exception('_resize_rows_manual: L1 — row %d height calc raised', row)
                max_h = max(max_h, one_line_h)

            if max_h > 0:
                table.setRowHeight(row, max_h)

        logging.info('_resize_rows_manual: L2 — done (%d rows sized)', row_count)

    def _resize_rows(self, vscroll_value, hscroll_value):
        """
        Apply row height constraints and restore scroll position.
        Called after _apply_spans() to finalize table layout.
        Extracted from _rebuild() closure for clarity and testability.

        NOTE: this deliberately does NOT call QTableWidget.resizeRowsToContents().
        That native Qt call was pinpointed (via the K0/K1 checkpoint logging
        added in 2aba0b4) as the exact site of a silent native (C++-level)
        crash after rapid rebuild cycles — the process died inside the C++
        call with no Python exception and no further log output. Since a
        native crash can't be fixed with a Python try/except (there's nothing
        to catch), the fix is to never invoke that specific machinery at all:
        the per-row/per-cell height is instead computed directly in Python
        below, using the same logic _ScenarioDelegate.sizeHint() uses
        internally, and applied via the plain (safe) setRowHeight() API.
        """
        logging.info('_resize_rows: K0 — entry (rowCount=%d), computing row heights manually',
                     self._table.rowCount())
        self._resize_rows_manual()
        logging.info('_resize_rows: K1 — manual row-height loop done')
        _fm  = QFontMetrics(self._table.font())
        _min_ors = _fm.height() * 2 + 20  # floor for ORS rows: ~2 lines + strip
        for _r in range(self._table.rowCount()):
            h = self._table.rowHeight(_r)
            # ORS cell in this row has content → enforce minimum readable height.
            # There used to also be an upper CAP here (~4 text lines) that
            # silently shrank any row whose wrapped description needed more
            # room than that, clipping the rest of the text with no visual
            # indication anything was cut off — exactly the "text göms på
            # raderna ... särskilt de som står under orsaker" bug report
            # (2026-08-11, see NOTES.md). In a safety-documentation tool,
            # a tall row is a far smaller problem than a hazard/cause
            # description silently missing its last few lines, so the cap
            # is gone — rows now grow to fit however much text is actually
            # there, exactly what "flerradig, auto-höjd" is supposed to mean.
            ors_item = self._table.item(_r, self._C_ORS)
            if ors_item and ors_item.text() and h < _min_ors:
                self._table.setRowHeight(_r, _min_ors)
        logging.info('_resize_rows: K2 — row-height pass done, restoring scroll position')
        self._table.verticalScrollBar().setValue(vscroll_value)
        self._table.horizontalScrollBar().setValue(hscroll_value)
        self._table.setUpdatesEnabled(True)
        logging.info('_resize_rows: K3 — done (setUpdatesEnabled True)')

    def _add_placeholder_row(self, node_name, dev_d):
        """Empty row shown when a node/deviation has no causes yet."""
        r = self._table.rowCount()
        self._table.insertRow(r)
        dev_id = dev_d['id'] if dev_d else None
        self._row_meta.append((dev_id, None, None, None))
        self._row_cat_info.append(None)

        def _ro(text=''):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        nod = _ro(node_name)
        self._table.setItem(r, self._C_NOD, nod)
        eq_id, eq_label = self._equipment_for_dev(dev_d or {})
        utr = _ro(eq_label)
        utr.setData(Qt.ItemDataRole.UserRole, eq_id)
        self._table.setItem(r, self._C_UTR, utr)
        dev_item = _ro(dev_d['description'] if dev_d else '')
        dev_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._table.setItem(r, self._C_DEV, dev_item)

        # ORS cell shows the two-zone layout (with '+' in obj zone) but has no cause yet
        ors = QTableWidgetItem('')
        ors.setData(Qt.ItemDataRole.UserRole + 2, ('', ''))
        ors.setToolTip("Enter för att lägga till orsak")
        self._table.setItem(r, self._C_ORS, ors)

        for col in (self._C_KON, self._C_RFORE, self._C_SG,
                    self._C_LOPA, self._C_SLUT):
            self._table.setItem(r, col, _ro())
        pass  # row height set by resizeRowsToContents at end of _rebuild

    def _add_empty_row(self, node_name, dev_d, cause_d, freq, freq_lbl):
        """Placeholder row when a cause has no consequences yet."""
        r = self._table.rowCount()
        self._table.insertRow(r)
        dev_id = dev_d['id'] if dev_d else None
        self._row_meta.append((dev_id, cause_d['id'], None, None))
        self._row_cat_info.append(None)

        def _ro(text=''):
            item = QTableWidgetItem(text)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return item

        nod = _ro(node_name)
        nod.setData(Qt.ItemDataRole.UserRole, cause_d['node_id'])
        self._table.setItem(r, self._C_NOD, nod)

        eq_id, eq_label = self._equipment_for_dev(dev_d or {})
        utr = _ro(eq_label)
        utr.setData(Qt.ItemDataRole.UserRole, eq_id)
        self._table.setItem(r, self._C_UTR, utr)

        dev_item = _ro(dev_d['description'] if dev_d else '')
        dev_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._table.setItem(r, self._C_DEV, dev_item)

        ors = QTableWidgetItem(cause_d['description'])
        ors.setData(Qt.ItemDataRole.UserRole,     ('cause', cause_d['id']))
        ors.setData(Qt.ItemDataRole.UserRole + 2, (cause_d.get('comp_type') or '',
                                                    cause_d.get('comp_tag')  or ''))
        ors.setData(Qt.ItemDataRole.UserRole + 3, freq)
        self._table.setItem(r, self._C_ORS, ors)

        kon = _ro()
        kon.setToolTip("Enter för att lägga till konsekvens")
        self._table.setItem(r, self._C_KON, kon)

        for col in (self._C_RFORE, self._C_SG,
                    self._C_LOPA, self._C_SLUT):
            self._table.setItem(r, col, _ro())

        pass  # row height set by resizeRowsToContents at end of _rebuild

    def _add_row(self, node_name, dev_d, cause_d, freq, freq_lbl, cons_d, all_sgs, sg,
                 cat_info=None, excl_cat_names=None, excl_for_cat=None,
                 cause_excl_sgs=None, sev_cat_list=None, all_cat_infos=None,
                 cause_popup_list=None, n_cats=0):
        """One row in the scenario table.

        sg            – the safeguard for this row (None = no safeguard on this row).
        cat_info      – (cat_id, sev_id, cat_name, cat_sev) for the category shown on
                        this row; None when this row has no category assessment.
        excl_cat_names – list of category names this safeguard is excluded from
                        (used for yellow ○ indicator on RRF badge).
        excl_for_cat  – set of sg_ids excluded from THIS row's category (for REFT calc).
        sev_cat_list  – [(sev_id, cat_name), ...] all category assessments for this
                        consequence (stored in SG cell for the extended RRF popup).
        n_cats        – total number of category assessments.
        """
        if excl_cat_names is None:
            excl_cat_names = []
        if excl_for_cat is None:
            excl_for_cat = set()
        if cause_excl_sgs is None:
            cause_excl_sgs = set()
        if sev_cat_list is None:
            sev_cat_list = []
        if cause_popup_list is None:
            cause_popup_list = []

        r      = self._table.rowCount()
        self._table.insertRow(r)
        cid    = cons_d['id']
        dev_id = dev_d['id'] if dev_d else None

        self._row_meta.append((dev_id, cause_d['id'], cid, sg['id'] if sg else None))
        self._row_cat_info.append(cat_info)

        # ── Risk calculations ─────────────────────────────────────────────────
        if cat_info:
            cat_id, sev_id, cat_name, cat_sev = cat_info
            sev = cat_sev or 1
            # Effective RRF for this category (exclude excluded safeguards)
            active_sgs = [s for s in all_sgs
                          if s['id'] not in excl_for_cat and s['id'] not in cause_excl_sgs]
            sg_rrf = 1
            for s in active_sgs:
                sg_rrf *= (s.get('rrf') or 1)
        else:
            sev = cons_d['severity'] or 1
            sg_rrf = 1
            for s in all_sgs:
                if s['id'] not in cause_excl_sgs:
                    sg_rrf *= (s.get('rrf') or 1)

        rfs        = [dict(rf) for rf in self.db.reduction_factors(cid)]
        fa_active  = bool(cons_d.get('fa_active', 0))
        fa_rrf     = cons_d.get('fa_rrf', 10) or 10
        ign_active = bool(cons_d.get('ignition_active', 0))
        ign_rrf    = cons_d.get('ignition_rrf', 10) or 10

        final_f, total_rrf, total_steps = total_freq_reduction(
            freq, sg_rrf, fa_active, fa_rrf, ign_active, ign_rrf, rfs)

        level_b, bg_b, fg_b = risk_info(freq, sev)
        level_s, bg_s, fg_s = risk_info(final_f, sev)

        # ── Col 0: Nod ────────────────────────────────────────────────────────
        nod = QTableWidgetItem(node_name)
        nod.setFlags(nod.flags() & ~Qt.ItemFlag.ItemIsEditable)
        nod.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        nod.setData(Qt.ItemDataRole.UserRole, cause_d['node_id'])
        self._table.setItem(r, self._C_NOD, nod)

        # ── Col: Utrustning ──────────────────────────────────────────────────
        eq_id, eq_label = self._equipment_for_dev(dev_d or {})
        utr_item = QTableWidgetItem(eq_label)
        utr_item.setFlags(utr_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        utr_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        utr_item.setData(Qt.ItemDataRole.UserRole, eq_id)
        self._table.setItem(r, self._C_UTR, utr_item)

        # ── Col 1: Avvikelse ─────────────────────────────────────────────────
        dev_item = QTableWidgetItem(dev_d['description'] if dev_d else '')
        dev_item.setFlags(dev_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        dev_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self._table.setItem(r, self._C_DEV, dev_item)

        # ── Col 2: Orsak ─────────────────────────────────────────────────────
        # Status icon (feature 5): green=complete, orange=partial, red=empty
        _cons_list   = self.db.consequences(cause_d['id'])
        _has_cons    = len(_cons_list) > 0
        _has_sev     = any(c.get('severity', 0) and c.get('severity', 0) > 0
                           for c in [dict(x) for x in _cons_list])
        _has_sg      = bool(self.db.safeguards_for_cause(cause_d['id']))
        if _has_cons and _has_sev and _has_sg:
            _status_icon = '🟢'
            _status_tip  = 'Komplett: konsekvens + allvarlighet + barriär'
        elif _has_cons and _has_sev:
            _status_icon = '🟡'
            _status_tip  = 'Saknar barriär'
        elif _has_cons:
            _status_icon = '🟠'
            _status_tip  = 'Saknar allvarlighetsgradering'
        else:
            _status_icon = '🔴'
            _status_tip  = 'Ingen konsekvens angiven'

        ors = QTableWidgetItem(cause_d['description'])
        ors.setData(Qt.ItemDataRole.UserRole,     ('cause', cause_d['id']))
        ors.setData(Qt.ItemDataRole.UserRole + 2, (cause_d.get('comp_type') or '',
                                                    cause_d.get('comp_tag')  or ''))
        ors.setData(Qt.ItemDataRole.UserRole + 3, freq)
        ors.setData(Qt.ItemDataRole.UserRole + 5, cause_d.get('base_frequency'))
        ors.setData(Qt.ItemDataRole.UserRole + 6, _status_icon)
        ors.setToolTip(f"{_status_icon} {_status_tip}\n"
                       "Dubbelklicka för att redigera\n"
                       "Klicka på objektzonen (vänster) för att sätta utrustnings-tag\n"
                       "Enter för att lägga till ny orsak")
        self._table.setItem(r, self._C_ORS, ors)

        # ── Col 3: Konsekvens ─────────────────────────────────────────────────
        chain_data   = parse_chain_from_json(cons_d.get('consequence_chain', ''))
        display_desc = (build_consequence_text(cons_d['description'], chain_data)
                        or cons_d['description'])

        kon_item = QTableWidgetItem(cons_d['description'])
        kon_item.setData(Qt.ItemDataRole.UserRole, ('consequence', cid))
        kon_item.setData(Qt.ItemDataRole.UserRole + 3, None)   # no per-row cat badge
        kon_item.setData(Qt.ItemDataRole.UserRole + 4, n_cats)
        kon_item.setData(Qt.ItemDataRole.UserRole + 5, all_cat_infos or [])
        kon_item.setData(Qt.ItemDataRole.UserRole + 7, (cons_d.get('comp_type') or '',
                                                         cons_d.get('comp_tag')  or ''))
        # Every tag ever drag-appended into this text, bolded on paint
        # (2026-08-09, see NOTES.md "fetmarkera objekttexten") — comp_tag
        # above only ever holds the MOST RECENT one.
        kon_item.setData(Qt.ItemDataRole.UserRole + 8,
                         parse_tag_refs(cons_d.get('tagged_refs') or ''))
        tip = ("Klicka på 📊-ikonen för att sätta konsekvens per kategori\n"
               "Dra en utrustningsmarkör hit (håll Shift) för att sätta tag\n"
               "Dubbelklicka för att redigera\nEnter för att lägga till ny konsekvens")
        if display_desc != cons_d['description']:
            tip += f"\nKedjetext: {display_desc}"
        kon_item.setToolTip(tip)
        self._table.setItem(r, self._C_KON, kon_item)

        # ── Col 4: Risk före barriär ──────────────────────────────────────────
        # Shown for EVERY row, not just ones with a per-category severity
        # assessment (2026-08-09, see NOTES.md) — freq/sev/bg_b/fg_b are
        # already computed unconditionally above (the cat_info/plain-severity
        # branch just above), so a consequence that only ever got its plain
        # severity+category set via ConsequencePanel (the common case — the
        # per-category 📊 assessment is an opt-in power-user feature) used to
        # render this cell completely blank/uncolored despite having a
        # perfectly valid risk value. Falls back to the plain 'risk_click'
        # action (pre-existing in _on_risk_cell_clicked, previously dead code
        # since nothing ever emitted it) instead of 'risk_click_cat' when
        # there's no real category_id/severity_id to edit.
        rb = QTableWidgetItem(f"{freq_axis_label(freq)}  {cons_axis_label(sev)}")
        rb.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        rb.setFlags(rb.flags() & ~Qt.ItemFlag.ItemIsEditable)
        rb.setToolTip(f"🖱 Klicka för att ändra i riskmatrisen\n{level_b}")
        if cat_info:
            rb.setData(Qt.ItemDataRole.UserRole,
                       ('risk_click_cat', cause_d['id'], cid, cat_id, sev_id, freq, sev))
        else:
            rb.setData(Qt.ItemDataRole.UserRole,
                       ('risk_click', cause_d['id'], cid, freq, sev))
        rb.setBackground(QBrush(QColor(bg_b)))
        rb.setForeground(QBrush(QColor(fg_b)))
        rb.setFont(QFont("Consolas", 9))
        self._table.setItem(r, self._C_RFORE, rb)

        # ── Col 5: Barriär ───────────────────────────────────────────────────
        if sg is None:
            sg_item = QTableWidgetItem('—')
            sg_item.setFlags(sg_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            sg_item.setToolTip("Enter för att lägga till barriär")
        else:
            rrf = sg.get('rrf', 1) or 1
            sg_item = QTableWidgetItem(sg['description'])
            sg_item.setData(Qt.ItemDataRole.EditRole, sg['description'])
            sg_item.setData(Qt.ItemDataRole.UserRole,     ('safeguard', sg['id']))
            sg_item.setData(Qt.ItemDataRole.UserRole + 1, rrf)
            # Yellow indicator: list of category names this sg is excluded from
            sg_item.setData(Qt.ItemDataRole.UserRole + 2, excl_cat_names)
            # Category data for extended RRF popup: (cons_id, [(sev_id, cat_name), ...])
            sg_item.setData(Qt.ItemDataRole.UserRole + 3, (cid, sev_cat_list) if sev_cat_list else None)
            # Cause list for RRF popup cause-exclusion section
            sg_item.setData(Qt.ItemDataRole.UserRole + 4, cause_popup_list)
            excl_cause_ids = self.db.get_safeguard_excluded_causes(sg['id'])
            excl_cause_names = [desc for cid2, desc, _ in cause_popup_list
                                if cid2 in excl_cause_ids]
            sg_item.setData(Qt.ItemDataRole.UserRole + 5, excl_cause_names)
            sg_item.setData(Qt.ItemDataRole.UserRole + 6, (sg.get('comp_type') or '',
                                                            sg.get('comp_tag')  or ''))
            sg_item.setData(Qt.ItemDataRole.UserRole + 7,
                             parse_tag_refs(sg.get('tagged_refs') or ''))
            tip = "Dra en utrustningsmarkör hit (håll Shift) för att sätta tag\n" \
                  "Dubbelklicka för att redigera\nEnter för att lägga till ny barriär\nKlicka på RRF-kolumnen för att ändra värdet"
            if excl_cat_names:
                tip += "\n⚠ Gäller ej för kategori: " + ", ".join(excl_cat_names)
            if excl_cause_names:
                tip += "\n⚠ Gäller ej för orsak: " + ", ".join(excl_cause_names)
            sg_item.setToolTip(tip)
        self._table.setItem(r, self._C_SG, sg_item)

        # ── Col LOPA: FA / Antändning / Övriga (merged LOPA column) ──────────
        n_active = sum(1 for rf in rfs if rf.get('active'))
        lopa_w = _LopaWidget(self.db, cid,
                             fa_active, fa_rrf, ign_active, ign_rrf, n_active)
        lopa_w._extra_btn.clicked.connect(partial(self._edit_extra, cid))
        lopa_w.changed.connect(self._update_lopa_risk)
        self._table.setCellWidget(r, self._C_LOPA, lopa_w)

        # ── Col SLUT: Slutkonsekvens ──────────────────────────────────────────
        # Shown for every row now (2026-08-09, see NOTES.md) — same fallback
        # rationale as RFORE above; final_f/sev/bg_s/fg_s are already
        # computed unconditionally regardless of cat_info.
        slut_text = (f"−{total_steps} steg\n" if total_steps else "") + \
                    f"{freq_axis_label(final_f)}  {cons_axis_label(sev)}"
        rs = QTableWidgetItem(slut_text)
        rs.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        rs.setFlags(rs.flags() & ~Qt.ItemFlag.ItemIsEditable)
        rs.setToolTip(f"{level_s} — {freq_axis_label(final_f)}  {cons_axis_label(sev)}  (−{total_steps} steg totalt)")
        rs.setBackground(QBrush(QColor(bg_s)))
        rs.setForeground(QBrush(QColor(fg_s)))
        rs.setFont(QFont("Consolas", 9))
        self._table.setItem(r, self._C_SLUT, rs)

        pass  # row height set by resizeRowsToContents at end of _rebuild

    def _get_cons_context(self, cons_id: int):
        """Return (deviation, comp_type, cause_text) for the consequence."""
        cons = self.db.get_consequence(cons_id)
        if not cons:
            return '', '', ''
        cause = self.db.get_cause(cons['cause_id'])
        if not cause:
            return '', '', ''
        cause_d  = dict(cause)
        comp     = cause_d.get('comp_type', '') or ''
        cause_tx = cause_d.get('description', '') or ''
        dev_id   = cause_d.get('deviation_id')
        dev_desc = ''
        if dev_id:
            dev = self.db.get_deviation(dev_id)
            if dev:
                dev_desc = dev['description'] or ''
        return dev_desc, comp, cause_tx

    def _pos_near_cons_row(self, cons_id: int, popup_size):
        """Global top-left position to show a popup near cons_id's KON cell in
        the scenario table, clamped to the screen — so it opens right where
        the user is working instead of centered on screen. Falls back to the
        current cursor position if cons_id isn't visible in the table right
        now (e.g. filtered out by the current node/deviation/cause scope)."""
        row = next((r for r, m in enumerate(self._row_meta) if m[2] == cons_id), -1)
        if row >= 0:
            rect = self._table.visualRect(self._table.model().index(row, self._C_KON))
            anchor = self._table.viewport().mapToGlobal(rect.bottomLeft())
        else:
            anchor = QCursor.pos()
        scr = (QApplication.screenAt(anchor) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup_size.width(), popup_size.height()
        x = min(anchor.x(), scr.right() - pw)
        y = min(anchor.y() + 4, scr.bottom() - ph)
        return QPoint(max(scr.left(), x), max(scr.top(), y))

    def _open_chain_editor(self, cons_id: int, label_widget=None):
        """Open the consequence step picker dialog; refresh the cell on accept."""
        dev, comp, cause_tx = self._get_cons_context(cons_id)
        dlg = ConsequenceStepPickerDialog(
            self.db, cons_id,
            deviation=dev, comp_type=comp, cause_text=cause_tx,
            parent=self)
        dlg.move(self._pos_near_cons_row(cons_id, dlg.sizeHint()))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Rebuild risk cells (description changed)
            self._schedule_rebuild()

    def _edit_extra(self, cons_id):
        # This slot runs on the call stack of a _LopaWidget's _extra_btn
        # QPushButton.clicked signal — that button is a live cell widget
        # embedded in self._table. dlg.exec() below pumps a NESTED Qt event
        # loop; any QTimer.singleShot(0, ...) already queued by
        # _schedule_rebuild() (24 call sites in this class) fires DURING
        # that nested loop, not after it. If it fires here, it calls
        # _rebuild() while THIS method (and the button's clicked handler)
        # is still executing underneath dlg.exec() on the C++ call stack.
        # _rebuild()'s setRowCount(0) then destroys the _LopaWidget/_extra_btn
        # that originated this very call — a use-after-free once dlg.exec()
        # returns and this frame resumes. Calling self._rebuild() directly
        # here (as this used to do) compounds the same risk a second time.
        # Every other dialog handler in this class defers via
        # _schedule_rebuild() for exactly this reason — do the same here.
        dlg = ReductionFactorsDialog(self.db, cons_id, self)
        dlg.exec()
        self._schedule_rebuild()

    # ── P&ID placement helpers ─────────────────────────────────────────────────

    def _update_ctx_bar(self, *_):
        """Refresh the sticky context bar to show Nod + Avvikelse of the topmost visible row."""
        if self._all_nodes or self._force_dev_column_visible:
            # Multiple nodes are interleaved in one table in "all nodes" mode,
            # or the host (e.g. HAZOPWorksheet, via always_show_deviation_
            # column()) forces the Avvikelse column always visible — either
            # way, a single "current node/deviation" context bar is redundant
            # with what the now-visible NOD/DEV columns already show per row,
            # and just costs an extra row of vertical space for the same info.
            self._ctx_bar.hide()
            return
        if not self._row_meta:
            self._ctx_bar.hide()
            return
        # Find the first row whose top edge is at or below the viewport top
        vp = self._table.viewport()
        top_row = self._table.rowAt(0)   # row at y=0 of the viewport
        if top_row < 0:
            top_row = 0
        if top_row >= len(self._row_meta):
            top_row = len(self._row_meta) - 1

        dev_id, cause_id, _, _ = self._row_meta[top_row]

        # Resolve Nod name from cause or deviation
        node_name = ''
        dev_desc  = ''
        try:
            if cause_id:
                cause = self.db.get_cause(cause_id)
                if cause:
                    node = self.db.get_node(cause['node_id'])
                    if node:
                        node_name = node['name']
            if dev_id:
                dev = self.db.get_deviation(dev_id)
                if dev:
                    dev_desc = dev['description']
        except Exception:
            pass

        if not node_name and not dev_desc:
            self._ctx_bar.hide()
            return

        parts = []
        if node_name:
            parts.append(f"🏭 <b>{node_name}</b>")
        if dev_desc:
            parts.append(f"⬡ {dev_desc}")
        self._ctx_bar.setText("   " + "     ›     ".join(parts))
        self._ctx_bar.show()

    def _update_lopa_risk(self, cons_id: int):
        """Targeted update of the SLUT cell when FA/IGN/Övriga changes.

        Avoids a full _rebuild() — only recalculates risk values for the
        rows belonging to *cons_id* and patches those cells in-place.
        """
        if getattr(self, '_rebuilding', False):
            return  # Table is mid-teardown/rebuild; a cell widget's focus-out signal
                     # fired reentrantly — ignore it, _rebuild() will reflect current
                     # state correctly once it completes.
        cons_d = self.db.get_consequence(cons_id)
        if not cons_d:
            return
        cons_d = dict(cons_d)
        cause_id = cons_d.get('cause_id')
        cause = self.db.get_cause(cause_id) if cause_id else None
        if not cause:
            return
        cause_d = dict(cause)
        freq    = self.db.cause_frequency_level(cause_d)
        rfs     = [dict(rf) for rf in self.db.reduction_factors(cons_id)]
        fa_active  = bool(cons_d.get('fa_active', 0))
        fa_rrf     = cons_d.get('fa_rrf', 10) or 10
        ign_active = bool(cons_d.get('ignition_active', 0))
        ign_rrf    = cons_d.get('ignition_rrf', 10) or 10
        all_sgs = [dict(s) for s in self.db.safeguards(cons_id)]

        cause_excl = set()
        for sg in all_sgs:
            ec = self.db.get_safeguard_excluded_causes(sg['id'])
            if cause_d['id'] in ec:
                cause_excl.add(sg['id'])

        self._table.blockSignals(True)
        try:
            for row, (_, cid_row, cid, sg_id) in enumerate(self._row_meta):
                if cid != cons_id:
                    continue
                cat_info = self._row_cat_info[row] if row < len(self._row_cat_info) else None

                # Build sg_rrf for this row
                if cat_info:
                    cat_id, sev_id, cat_name, cat_sev = cat_info
                    sev = cat_sev or 1
                    excl_for_cat = self.db.get_severity_excluded_sgs(sev_id)
                    active_sgs = [s for s in all_sgs
                                  if s['id'] not in excl_for_cat and s['id'] not in cause_excl]
                    sg_rrf = 1
                    for s in active_sgs:
                        sg_rrf *= (s.get('rrf') or 1)
                else:
                    sev = cons_d.get('severity') or 1
                    sg_rrf = 1
                    for s in all_sgs:
                        if s['id'] not in cause_excl:
                            sg_rrf *= (s.get('rrf') or 1)

                final_f, total_rrf, total_steps = total_freq_reduction(
                    freq, sg_rrf, fa_active, fa_rrf, ign_active, ign_rrf, rfs)
                _, bg_s, fg_s       = risk_info(final_f, sev)

                # Patched for every row now (2026-08-09, see NOTES.md) — same
                # fallback rationale as _add_row: bg_s/fg_s are already
                # computed unconditionally above, regardless of cat_info, so
                # a non-categorized consequence's SLUT cell used to go
                # stale/blank forever after an RRF change.
                slut_text = (f"−{total_steps} steg\n" if total_steps else "") + \
                            f"{freq_axis_label(final_f)}  {cons_axis_label(sev)}"
                rs = self._table.item(row, self._C_SLUT)
                if rs:
                    rs.setText(slut_text)
                    rs.setBackground(QBrush(QColor(bg_s)))
                    rs.setForeground(QBrush(QColor(fg_s)))
        finally:
            self._table.blockSignals(False)

    def _update_row_text_only(self, kind, id_, new_desc):
        """Fast path for a pure description-text edit: patch just the
        affected cell's text on every row referencing id_ (a cause/
        consequence/safeguard can appear on more than one row when spans
        merge same-id rows visually), without a full _rebuild().

        No _apply_spans() or _resize_rows() pass is needed: _apply_spans()
        groups rows purely by IDs in _row_meta (never by cell text — see its
        docstring), which a description edit never changes, and only
        _C_ORS/_C_KON ever need a height recompute for long/short text
        (_C_SG is a fixed one-line column per _ScenarioDelegate's wrap_cols).
        Mirrors _update_lopa_risk()'s established pattern (re-entrancy guard,
        blockSignals, patch in place, no rebuild).
        """
        if getattr(self, '_rebuilding', False):
            return  # mid-teardown/rebuild — the coming _rebuild() will show
                     # correct text anyway; avoid touching a row index that
                     # may no longer correspond to the same item.
        col = {'cause': self._C_ORS, 'consequence': self._C_KON,
               'safeguard': self._C_SG}.get(kind)
        if col is None:
            return
        field_idx = {'cause': 1, 'consequence': 2, 'safeguard': 3}[kind]
        needs_height_recalc = col in (self._C_ORS, self._C_KON)

        self._table.blockSignals(True)
        try:
            for row, meta in enumerate(self._row_meta):
                if meta[field_idx] != id_:
                    continue
                item = self._table.item(row, col)
                if item is not None:
                    item.setText(new_desc)
                if needs_height_recalc:
                    self._table.setRowHeight(row, self._wrap_col_row_height(row, col))
        finally:
            self._table.blockSignals(False)

    def _wrap_col_row_height(self, row, col):
        """Height a single ORS/KON cell needs for its current text, matching
        _resize_rows_manual()'s per-column formula exactly (kept in sync
        with it deliberately — see that method for why boundingRect() is
        only used for these two wrap-sensitive columns)."""
        table = self._table
        fm = QFontMetrics(table.font())
        one_line_h = fm.height() + 6
        item = table.item(row, col)
        text = item.text() if item is not None else ''
        if not text:
            return one_line_h
        w = table.columnWidth(col)
        if col == self._C_ORS:
            cell_w = max(40, w - 6)
            rect = fm.boundingRect(0, 0, cell_w, 10000, Qt.TextFlag.TextWordWrap, text)
            return _ORS_STRIP_H + max(one_line_h, rect.height() + 4)
        else:   # self._C_KON
            cell_w = max(40, w - _PID_ICON_W - _KON_CAT_W)
            rect = fm.boundingRect(0, 0, cell_w, 10000, Qt.TextFlag.TextWordWrap, text)
            return max(one_line_h, rect.height() + 4)

    def refresh_placed(self):
        """Reload which IDs are placed on the P&ID and repaint the table."""
        try:
            self._placed_causes       = set(self.db.marked_cause_ids())
            self._placed_consequences = set(self.db.marked_consequence_ids())
            self._placed_safeguards   = set(self.db.marked_safeguard_ids())
        except Exception:
            pass
        self._table.viewport().update()

    def select_cause(self, cause_id: int):
        """Scroll to and select the first row for *cause_id* in the scenario
        table. Never steals the current cell away from an active edit or a
        row the user has already navigated to on their own — this used to
        unconditionally force the ORS column current a moment after cause
        creation, which could yank focus out from under a user who had
        already clicked into that very row's KON cell to type a
        consequence (reported as "kan fortfarande inte lägga in text
        [i konsekvens]"). Always still scrolls the row into view."""
        for row, (dev_id, cid, cons_id, sg_id) in enumerate(self._row_meta):
            if cid == cause_id:
                already_editing = self._table.state() == QAbstractItemView.State.EditingState
                already_on_row = self._table.currentRow() == row
                if not already_editing and not already_on_row:
                    self._table.setCurrentCell(row, self._C_ORS)
                self._table.scrollTo(
                    self._table.model().index(row, self._C_ORS),
                    QAbstractItemView.ScrollHint.PositionAtCenter)
                return

    def ors_cell_global_pos(self, dev_id):
        """Return global top-right corner of the first placeholder ORS cell for dev_id."""
        for row, meta in enumerate(self._row_meta):
            if meta[0] == dev_id:
                rect = self._table.visualRect(
                    self._table.model().index(row, self._C_ORS))
                return self._table.viewport().mapToGlobal(rect.topRight())
        return None

    def _cell_has_item(self, row, col):
        """Returns True only when the cell actually has a placeable item ID."""
        if row >= len(self._row_meta):
            return False
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS:
            return cause_id is not None
        if col == self._C_KON:
            return cons_id is not None
        if col == self._C_SG:
            return sg_id is not None
        return False

    def _is_cell_placed(self, row, col):
        if row >= len(self._row_meta):
            return False
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS:
            return cause_id in self._placed_causes
        if col == self._C_KON:
            return cons_id in self._placed_consequences
        if col == self._C_SG:
            return sg_id is not None and sg_id in self._placed_safeguards
        return False

    def _place_from_table(self, row, col):
        if row >= len(self._row_meta):
            return
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS and cause_id is not None:
            self.place_requested.emit(CAUSE_T, cause_id)
        elif col == self._C_KON and cons_id is not None:
            self.place_requested.emit(CONS_T, cons_id)
        elif col == self._C_SG and sg_id is not None:
            self.place_requested.emit(SG_T, sg_id)

    def _emit_navigate(self, row, col):
        if row >= len(self._row_meta):
            return
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS and cause_id is not None:
            self.navigate_to_pid.emit(CAUSE_T, cause_id)
        elif col == self._C_KON and cons_id is not None:
            self.navigate_to_pid.emit(CONS_T, cons_id)
        elif col == self._C_SG and sg_id is not None:
            self.navigate_to_pid.emit(SG_T, sg_id)

    def _remove_from_pid(self, row, col):
        if row >= len(self._row_meta):
            return
        _dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_ORS and cause_id is not None:
            self.remove_requested.emit(CAUSE_T, cause_id)
        elif col == self._C_KON and cons_id is not None:
            self.remove_requested.emit(CONS_T, cons_id)
        elif col == self._C_SG and sg_id is not None:
            self.remove_requested.emit(SG_T, sg_id)

    def _on_table_context_menu(self, pos):
        col = self._table.columnAt(pos.x())
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._row_meta):
            return
        if col not in (self._C_ORS, self._C_KON, self._C_SG):
            return
        if not self._cell_has_item(row, col):
            return  # no item to place/remove — e.g. safeguard row with no safeguard yet
        is_placed = self._is_cell_placed(row, col)
        menu = QMenu(self)
        if not is_placed:
            a = menu.addAction("📍 Lägg till på P&ID")
            a.triggered.connect(lambda: self._place_from_table(row, col))
        else:
            a1 = menu.addAction("📍 Lägg till ytterligare på P&ID")
            a1.triggered.connect(lambda: self._place_from_table(row, col))
            a2 = menu.addAction("🗑 Ta bort från P&ID")
            a2.triggered.connect(lambda: self._remove_from_pid(row, col))
        if col == self._C_KON and row < len(self._row_meta):
            cons_id = self._row_meta[row][2]
            if cons_id is not None:
                menu.addSeparator()
                a_chain = menu.addAction("📋 Redigera konsekvenskedja (Del1–Del5)…")
                a_chain.triggered.connect(lambda: self._open_chain_editor(cons_id))
        if col == self._C_SG and row < len(self._row_meta):
            sg_id = self._row_meta[row][3]
            if sg_id is not None:
                menu.addSeparator()
                a_rrf = menu.addAction("⚙ Ändra RRF...")
                a_rrf.triggered.connect(lambda: self._show_rrf_popup(row, sg_id))
        # Feature 4: clone scenario to another deviation
        if col == self._C_ORS and row < len(self._row_meta):
            cause_id = self._row_meta[row][1]
            if cause_id is not None:
                menu.addSeparator()
                a_clone = menu.addAction("📋 Duplicera scenario till annan avvikelse…")
                a_clone.triggered.connect(lambda: self._clone_scenario(cause_id))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_cell_clicked(self, row, col):
        if col == self._C_ORS and row < len(self._row_meta):
            cause_id = self._row_meta[row][1]
            if cause_id is not None:
                self.item_selected.emit(CAUSE_T, cause_id)
            # Feature 7: single-click on already-current ORS cell → start edit
            if self._table.currentRow() == row and self._table.currentColumn() == col:
                QTimer.singleShot(200, lambda r=row, c=col: self._try_start_edit(r, c))
            return
        if col == self._C_KON and row < len(self._row_meta):
            cons_id = self._row_meta[row][2]
            if cons_id is not None:
                self.item_selected.emit(CONS_T, cons_id)
            # Feature 7 (2026-08-07): single-click on already-current KON
            # cell → start inline edit, same as ORS/SG — "trycka direkt på
            # konsekvensen för att redigera den direkt där" (NOTES.md).
            # Double-click still opens the chain wizard (_on_cell_double_clicked).
            if self._table.currentRow() == row and self._table.currentColumn() == col:
                QTimer.singleShot(200, lambda r=row, c=col: self._try_start_edit(r, c))
            return
        if col == self._C_SG and row < len(self._row_meta):
            sg_id = self._row_meta[row][3]
            if sg_id is not None:
                self.item_selected.emit(SG_T, sg_id)
            # Feature 7: single-click on already-current SG cell → start edit
            if self._table.currentRow() == row and self._table.currentColumn() == col:
                QTimer.singleShot(200, lambda r=row, c=col: self._try_start_edit(r, c))
            return
        if col != self._C_RFORE:
            return
        item = self._table.item(row, col)
        if not item:
            return
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta or meta[0] not in ('risk_click', 'risk_click_cat'):
            return

        if meta[0] == 'risk_click_cat':
            _, cause_id, cons_id, cat_id, sev_id, cur_freq, cur_cons = meta
            popup = RiskMatrixPopup(cur_freq, cur_cons, self)
            popup.selection_made.connect(
                lambda f, c, caid=cause_id, coid=cons_id, catid=cat_id:
                    self._apply_risk_from_matrix_cat(caid, coid, catid, f, c))
        else:
            _, cause_id, cons_id, cur_freq, cur_cons = meta
            popup = RiskMatrixPopup(cur_freq, cur_cons, self)
            popup.selection_made.connect(
                lambda f, c, caid=cause_id, coid=cons_id:
                    self._apply_risk_from_matrix(caid, coid, f, c))

        # Position popup: prefer above the cell, fall back to below if off-screen
        popup.adjustSize()
        cell_rect  = self._table.visualItemRect(item)
        anchor     = self._table.viewport().mapToGlobal(cell_rect.topLeft())
        _scr       = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        screen     = _scr.availableGeometry()
        ph         = popup.sizeHint().height()
        pw         = popup.sizeHint().width()
        # Try above first
        y = anchor.y() - ph - 4
        if y < screen.top():
            y = anchor.y() + cell_rect.height() + 4   # fall back: below
        x = max(screen.left(), min(anchor.x(), screen.right() - pw))
        popup.move(x, y)
        popup.exec()

    def _apply_risk_from_matrix(self, cause_id, cons_id, new_freq, new_cons):
        self.db.update_cause(cause_id, likelihood=new_freq)
        cons = self.db.get_consequence(cons_id)
        if cons:
            self.db.update_consequence(
                cons_id, cons['description'], new_cons, cons['category'] or '')
        self._schedule_rebuild()

    def _apply_risk_from_matrix_cat(self, cause_id, cons_id, cat_id, new_freq, new_cons):
        """Bidirectional: update frequency on cause and category severity on consequence."""
        self.db.update_cause(cause_id, likelihood=new_freq)
        self.db.set_consequence_severity(cons_id, cat_id, new_cons)
        self._schedule_rebuild()

    def _show_cat_sg_popup(self, sev_id, all_sgs):
        """Open the safeguard-selection popup for a category row."""
        popup = CatSGSelectionPopup(self.db, sev_id, all_sgs, self)
        popup.adjustSize()
        gp  = QCursor.pos()
        scr = (QApplication.screenAt(gp) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = min(gp.x(), scr.right() - pw)
        y = min(gp.y() + 4, scr.bottom() - ph)
        popup.move(max(scr.left(), x), max(scr.top(), y))
        if popup.exec() == QDialog.DialogCode.Accepted:
            self._schedule_rebuild()

    def _on_cell_double_clicked(self, item):
        if item is None:
            return
        row = item.row()
        col = item.column()
        # Double-click on KON opens step picker instead of inline text edit
        if col == self._C_KON and row < len(self._row_meta):
            cons_id = self._row_meta[row][2]
            if cons_id is not None:
                self._open_chain_editor(cons_id)
            return
        if col in (self._C_ORS, self._C_SG):
            if not bool(item.flags() & Qt.ItemFlag.ItemIsEditable):
                return
            self._table.setFocus()
            self._table.edit(self._table.model().index(row, col))

    def _show_rrf_popup(self, row, sg_id):
        """Called from context menu — centre on the cell."""
        item = self._table.item(row, self._C_SG)
        if item:
            cr = self._table.visualItemRect(item)
            gp = self._table.viewport().mapToGlobal(cr.center())
        else:
            gp = self._table.viewport().mapToGlobal(self._table.viewport().rect().center())
        self._show_rrf_popup_at(row, sg_id, gp)

    def _show_rrf_popup_at(self, row, sg_id, global_pos):
        """Show RRF popup near global_pos, keeping it within the screen."""
        sg = self.db.get_safeguard(sg_id)
        sg_d        = dict(sg) if sg else {}
        current_rrf     = int(sg_d.get('rrf', 1))
        current_sg_type = sg_d.get('sg_type', 'Övrigt') or 'Övrigt'

        # Use extended popup when consequence has category assessments
        item          = self._table.item(row, self._C_SG)
        cat_pop_data  = item.data(Qt.ItemDataRole.UserRole + 3) if item else None
        cause_pop_list = item.data(Qt.ItemDataRole.UserRole + 4) if item else None

        if cat_pop_data or cause_pop_list:
            _cons_id, sev_cat_list = cat_pop_data if cat_pop_data else (None, [])
            popup = SgRRFCategoryPopup(
                self.db, sg_id, current_rrf, current_sg_type,
                sev_cat_list, cause_pop_list or [], self)
        else:
            popup = RRFPopup(current_rrf, current_sg_type, self)
            popup.rrf_selected.connect(
                lambda v, t, r=row, sid=sg_id: self._update_sg_rrf(r, sid, v, t))

        popup.adjustSize()
        _scr   = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        screen = _scr.availableGeometry()
        pw = popup.sizeHint().width()
        ph = popup.sizeHint().height()
        x = global_pos.x()
        y = global_pos.y() + 6
        if y + ph > screen.bottom():
            y = global_pos.y() - ph - 6
        if x + pw > screen.right():
            x = screen.right() - pw - 4
        x = max(screen.left() + 4, x)
        y = max(screen.top() + 4, y)
        popup.move(x, y)
        if popup.exec() == QDialog.DialogCode.Accepted:
            self._schedule_rebuild()

    # ORS strip layout constants — shared between paint() (_PidDelegate,
    # below) and the click hit-test in eventFilter() so the drawn tag zone
    # and the clickable tag zone can never drift apart. This file has a
    # documented history of exactly that kind of desync between paint code
    # and geometry code computed elsewhere (see NOTES.md's notes on
    # _wrap_col_row_height/_resize_rows_manual needing to stay in sync with
    # paint) — keeping this one calculation in one place avoids repeating it.
    _ORS_DOTS_MARGIN = 22   # room reserved for the status/comment dots at the strip's right edge
    _ORS_FREQ_MAX_W  = 90   # sane ceiling; real frequency strings are short ("3/år", "1.2e-3/år")

    def _ors_freq_label(self, freq_val, base_freq_per_year):
        """The exact frequency text shown in the ORS strip. Split out so
        the width calc below and the paint code always agree on what
        string they're sizing/drawing."""
        if freq_val is None:
            return None
        if base_freq_per_year is not None:
            bfv = float(base_freq_per_year)
            if bfv >= 0.1:     return f"{bfv:.2g}/år"
            elif bfv >= 0.001: return f"{bfv:.3g}/år"
            else:              return f"{bfv:.1e}".replace('e-0', 'e-') + "/år"
        return freq_axis_label(freq_val)

    def _ors_tag_zone_geometry(self, item, tag_x, cell_right):
        """Return (tag_zone_w, freq_zone_x, freq_zone_w, freq_str) for the ORS strip.

        2026-08-11: "tag numret klipps av ... högerställ frekvens" — the
        tag used to be capped at the fixed _cause_obj_w divider width no
        matter how much space was actually free, while the frequency was
        drawn left-aligned right after it, stranding a gap of blank space
        between the (short) frequency text and the status dots. Fix:
        right-anchor the frequency zone against the dots margin FIRST,
        then let the tag zone claim whatever is left over — reclaiming
        exactly the space the old layout was wasting. _cause_obj_w (the
        user-draggable divider, still used for the drag handle and its
        persisted width) stays in play as a FLOOR so dragging it can still
        only ever make the promised tag zone wider, never narrower than
        what the user last set.
        """
        freq_val = item.data(Qt.ItemDataRole.UserRole + 3) if item else None
        base_freq_per_year = item.data(Qt.ItemDataRole.UserRole + 5) if item else None
        freq_str = self._ors_freq_label(freq_val, base_freq_per_year)
        freq_zone_w = 0
        if freq_str:
            ff = QFont(self._table.font())
            ff.setPointSize(max(6, self._table.font().pointSize() - 1))
            freq_zone_w = min(QFontMetrics(ff).horizontalAdvance(freq_str) + 6,
                              self._ORS_FREQ_MAX_W)
        freq_zone_x = cell_right - self._ORS_DOTS_MARGIN - freq_zone_w
        tag_zone_w  = max(self._cause_obj_w, freq_zone_x - tag_x - 3)
        return tag_zone_w, freq_zone_x, freq_zone_w, freq_str

    def _show_cause_obj_popup(self, row, cause_id, global_pos):
        item      = self._table.item(row, self._C_ORS)
        obj_data  = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
        comp_type, comp_tag = obj_data if obj_data else ('', '')
        current_desc = (item.text() if item else '') or ''

        dev_id   = self._row_meta[row][0] if row < len(self._row_meta) else None
        dev_desc = None
        if dev_id is not None:
            dev_row = self.db.get_deviation(dev_id)
            if dev_row:
                dev_desc = dev_row['description']

        popup = CauseObjectPopup(
            comp_type, comp_tag, self.db,
            dev_description=dev_desc,
            current_description=current_desc,
            parent=self)
        popup.committed.connect(
            lambda ct, tg, desc, freq, r=row, cid=cause_id:
                self._apply_cause_obj(r, cid, ct, tg, desc, freq))
        popup.adjustSize()
        _scr   = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        screen = _scr.availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x, y   = global_pos.x(), global_pos.y() + 6
        if y + ph > screen.bottom(): y = global_pos.y() - ph - 6
        if x + pw > screen.right():  x = screen.right() - pw - 4
        x = max(screen.left() + 4, x)
        y = max(screen.top()  + 4, y)
        popup.move(x, y)
        popup.exec()

    def _apply_cause_obj(self, row, cause_id, comp_type, comp_tag, description, frequency):
        # Do all DB writes first — learning is handled inside update_cause
        self.db.update_cause(cause_id, comp_type=comp_type, comp_tag=comp_tag)
        if description:
            kwargs = {'description': description}
            if frequency is not None:
                kwargs['base_frequency'] = frequency
            self.db.update_cause(cause_id, **kwargs)
            # Description changed → full rebuild (item refs are stale after rebuild anyway)
            self._schedule_rebuild()
        else:
            # Only tag/type changed → update item in-place with signals blocked
            self._table.blockSignals(True)
            item = self._table.item(row, self._C_ORS)
            if item:
                item.setData(Qt.ItemDataRole.UserRole + 2, (comp_type, comp_tag))
            self._table.blockSignals(False)
            self._table.viewport().update()

    def _update_sg_rrf(self, row, sg_id, rrf, sg_type=None):
        self.db.update_safeguard(sg_id, rrf=rrf, sg_type=sg_type)
        self._schedule_rebuild()

    def _open_comment_popup(self, row, cause_id, global_pos):
        """Floating comment editor for a cause row."""
        current = self.db.get_cause_comment(cause_id) or ''
        popup = QDialog(self)
        popup.setWindowTitle("Kommentar")
        popup.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        popup.setMinimumWidth(340)
        lay = QVBoxLayout(popup)
        lay.setContentsMargins(10, 10, 10, 10)
        lbl = QLabel("💬  Kommentar till orsaksraden:")
        lbl.setStyleSheet("font-weight:bold; font-size:10px; color:#8D9299;")
        lay.addWidget(lbl)
        txt = QTextEdit(current)
        txt.setPlaceholderText("Ange notering, beslut eller referens…")
        txt.setFixedHeight(CONFIG['H_EDIT_LG'])
        lay.addWidget(txt)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(popup.accept)
        btns.rejected.connect(popup.reject)
        lay.addWidget(btns)
        popup.adjustSize()
        scr = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = min(global_pos.x(), scr.right() - pw)
        y = min(global_pos.y() + 4, scr.bottom() - ph)
        popup.move(max(scr.left(), x), max(scr.top(), y))
        if popup.exec() == QDialog.DialogCode.Accepted:
            self.db.set_cause_comment(cause_id, txt.toPlainText().strip())
            self._schedule_rebuild()

    # ── Feature 4: clone scenario ─────────────────────────────────────────────
    def _clone_scenario(self, cause_id):
        cause = self.db.get_cause(cause_id)
        if not cause: return
        cause = dict(cause)   # sqlite3.Row → dict so .get() works
        dev_id = cause['deviation_id']
        node_id = cause['node_id']
        devs = [d for d in self.db.deviations(node_id) if d['id'] != dev_id]
        if not devs:
            QMessageBox.information(self, 'Duplicera scenario',
                'Inga andra avvikelser att duplicera till på denna nod.')
            return
        items = [d['description'] for d in devs]
        choice, ok = QInputDialog.getItem(self, 'Duplicera scenario',
            'Välj avvikelse att kopiera scenario till:', items, 0, False)
        if not ok: return
        target_dev = next(d for d in devs if d['description'] == choice)
        # Copy cause
        new_cid = self.db.add_cause(target_dev['id'])
        self.db.update_cause(new_cid,
            description=cause['description'],
            comp_type=cause.get('comp_type', ''),
            comp_tag=cause.get('comp_tag', ''))
        # Copy consequences + safeguards
        for cons in self.db.consequences(cause_id):
            new_oid = self.db.copy_consequence(cons['id'], new_cid)
        self.new_item_created.emit(CAUSE_T, new_cid)
        self._schedule_rebuild()

    # ── Enter-tangent: snabblägg-till ─────────────────────────────────────────

    def eventFilter(self, obj, event):
        ctrl = bool(event.type() == QEvent.Type.KeyPress and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier)

        # Viewport mouse: drag divider between obj-zone and text in ORS column.
        # This handle intentionally still tracks _cause_obj_w itself (the
        # user's persisted minimum), NOT the wider tag_zone_w the strip may
        # actually render at (2026-08-11, see _ors_tag_zone_geometry) — the
        # handle is where the user asked the floor to be, and dragging it
        # only ever raises or lowers that floor, regardless of how much
        # extra elbow room a given row's tag currently happens to have.
        if obj is self._table.viewport() and event.type() == QEvent.Type.MouseMove:
            pos = event.pos()
            if self._drag_obj_w_active:
                delta = pos.x() - self._drag_obj_w_start_x
                self._cause_obj_w = max(30, min(300, self._drag_obj_w_start_w + delta))
                self._table.viewport().update()
                return True
            div_x = self._table.columnViewportPosition(self._C_ORS) + _PID_ICON_W + self._cause_obj_w
            if abs(pos.x() - div_x) <= 4:
                self._table.viewport().setCursor(Qt.CursorShape.SizeHorCursor)
            else:
                self._table.viewport().unsetCursor()

        if obj is self._table.viewport() and event.type() == QEvent.Type.MouseButtonRelease:
            if self._drag_obj_w_active:
                self._drag_obj_w_active = False
                self._table.viewport().unsetCursor()
                self.db.set_config('cause_obj_w', str(self._cause_obj_w))
                return True

        # Right-click: pin-zone toggles P&ID placement; elsewhere → context menu
        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonPress and
                event.button() == Qt.MouseButton.RightButton):
            pos = event.pos()
            col = self._table.columnAt(pos.x())
            row = self._table.rowAt(pos.y())
            if row >= 0 and col in (self._C_ORS, self._C_KON, self._C_SG):
                col_x = self._table.columnViewportPosition(col)
                if pos.x() - col_x < _PID_ICON_W and self._cell_has_item(row, col):
                    if self._is_cell_placed(row, col):
                        self._remove_from_pid(row, col)
                    else:
                        self._place_from_table(row, col)
                    return True
            # Let Qt dispatch CustomContextMenu signal (falls through to _on_context_menu)
            return False

        # ── Drag: record press position for potential drag-start ─────────────────
        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonPress and
                event.button() == Qt.MouseButton.LeftButton):
            pos = event.pos()
            row = self._table.rowAt(pos.y())
            col = self._table.columnAt(pos.x())
            if row >= 0 and col in (self._C_ORS, self._C_KON, self._C_SG):
                col_x = self._table.columnViewportPosition(col)
                if pos.x() - col_x >= _PID_ICON_W:   # not in pin zone
                    self._drag_press_pos = pos
                    self._drag_press_row = row
                    self._drag_press_col = col
            else:
                self._drag_press_pos = None

        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonRelease):
            self._drag_press_pos = None

        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseMove and
                self._drag_press_pos is not None and
                event.buttons() & Qt.MouseButton.LeftButton):
            dist = (event.pos() - self._drag_press_pos).manhattanLength()
            if dist >= QApplication.startDragDistance():
                self._drag_press_pos = None
                self._start_drag(self._drag_press_row, self._drag_press_col,
                                 event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                return True

        # ── Drop events on table ──────────────────────────────────────────────
        # Qt/PyQt6 delivers DragEnter/DragMove/Drop to whichever widget is
        # actually under the cursor — for a QAbstractItemView-based widget
        # like QTableWidget that's the VIEWPORT, not the outer table widget
        # (the viewport is the real scrollable surface; the outer widget is
        # just its frame). Checking only `obj is self._table` here meant
        # this branch never matched for a REAL cross-widget drag (e.g. the
        # Shift-drag-a-tag-from-P&ID feature), so the drop silently did
        # nothing — this only worked at all in tests because they called
        # _handle_drop() directly, bypassing event delivery entirely (see
        # NOTES.md "Drag-and-drop till KON fungerade inte i praktiken").
        # Accept either object defensively rather than betting on one.
        _drop_targets = (self._table, self._table.viewport())
        if obj in _drop_targets and event.type() == QEvent.Type.DragEnter:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.DragMove:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.Drop:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:'):
                self._handle_drop(event, source_obj=obj)
                return True

        # Viewport mouse: detect LEFT-click in icon strip or RRF row
        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonPress and
                event.button() == Qt.MouseButton.LeftButton):
            pos = event.pos()
            # Check for divider drag start before any other click handling
            div_x = self._table.columnViewportPosition(self._C_ORS) + _PID_ICON_W + self._cause_obj_w
            if abs(pos.x() - div_x) <= 4:
                self._drag_obj_w_active = True
                self._drag_obj_w_start_x = pos.x()
                self._drag_obj_w_start_w = self._cause_obj_w
                self._table.viewport().setCursor(Qt.CursorShape.SizeHorCursor)
                return True
            col = self._table.columnAt(pos.x())
            row = self._table.rowAt(pos.y())
            if row >= 0 and col in (self._C_ORS, self._C_KON, self._C_SG):
                col_x = self._table.columnViewportPosition(col)
                if pos.x() - col_x < _PID_ICON_W:
                    if not self._cell_has_item(row, col):
                        # Placeholder ORS row — red pin click → enter P&ID placement mode
                        if col == self._C_ORS and row < len(self._row_meta):
                            dev_id = self._row_meta[row][0]
                            if dev_id is not None:
                                self.add_causes_on_pid_requested.emit(dev_id)
                        return True
                    if self._is_cell_placed(row, col):
                        # 🟢 → navigate to marker on P&ID
                        self._emit_navigate(row, col)
                    elif col == self._C_ORS and row < len(self._row_meta):
                        # 🔴 on existing unplaced cause → open P&ID add panel
                        dev_id = self._row_meta[row][0]
                        if dev_id is not None:
                            self.add_causes_on_pid_requested.emit(dev_id)
                    else:
                        # 🔴 other columns → place this item
                        self._place_from_table(row, col)
                    return True  # consume left-click; right-click falls through to context menu

            # Object-tag zone click — left (_PID_ICON_W .. _PID_ICON_W+tag_zone_w)
            # of cause cell. tag_zone_w is computed the same way paint()
            # computes it (via _ors_tag_zone_geometry) rather than the raw
            # _cause_obj_w divider width — otherwise, once a long tag's
            # DRAWN width expands past the old fixed cap (2026-08-11 fix),
            # clicking on the now-visible-but-previously-uncounted part of
            # the tag would silently do nothing (stale hit-test rectangle).
            if row >= 0 and col == self._C_ORS and row < len(self._row_meta):
                col_x      = self._table.columnViewportPosition(col)
                obj_start  = col_x + _PID_ICON_W
                cell_right = col_x + self._table.columnWidth(col) - 1
                item       = self._table.item(row, col)
                tag_zone_w, _fx, _fw, _fs = self._ors_tag_zone_geometry(item, obj_start, cell_right)
                obj_end    = obj_start + tag_zone_w
                if obj_start <= pos.x() < obj_end:
                    cause_id = self._row_meta[row][1]
                    if cause_id is not None:
                        gp = self._table.viewport().mapToGlobal(pos)
                        self._show_cause_obj_popup(row, cause_id, gp)
                    return True

            # 💬 Comment + 📋 Clone icon clicks in ORS cell (inline, replaces context menu)
            if row >= 0 and col == self._C_ORS and row < len(self._row_meta):
                cause_id = self._row_meta[row][1]
                if cause_id is not None:
                    ci = self._table.model().index(row, col)
                    cr = self._table.visualRect(ci)
                    # rightmost zones: [📋clone:18][💬comment:20][🟢status:18]
                    clone_right  = cr.right() - 18 - 20        # start of 📋 zone
                    cmt_right    = cr.right() - 18              # start of 💬 zone
                    if pos.x() >= clone_right and pos.x() < cmt_right:
                        # 📋 Clone scenario
                        self._clone_scenario(cause_id)
                        return True
                    if pos.x() >= cmt_right and pos.x() < cr.right() - 18:
                        # 💬 Comment popup
                        self._open_comment_popup(row, cause_id,
                                                  self._table.viewport().mapToGlobal(pos))
                        return True

            # 📊 Category badge click in KON cell
            if row >= 0 and col == self._C_KON and row < len(self._row_meta):
                col_x     = self._table.columnViewportPosition(col)
                cat_start = col_x + _PID_ICON_W
                cat_end   = cat_start + _KON_CAT_W
                if cat_start <= pos.x() < cat_end:
                    cons_id = self._row_meta[row][2]
                    if cons_id is not None:
                        gp = self._table.viewport().mapToGlobal(pos)
                        popup = ConsCategoryMatrixPopup(self.db, cons_id, self)
                        popup.adjustSize()
                        scr    = (QApplication.screenAt(gp) or QApplication.primaryScreen()).availableGeometry()
                        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
                        x = min(gp.x(), scr.right() - pw)
                        y = min(gp.y() + 4, scr.bottom() - ph)
                        popup.move(max(scr.left(), x), max(scr.top(), y))
                        if popup.exec() == QDialog.DialogCode.Accepted:
                            self._schedule_rebuild()
                    return True

            # ⚡ RRF badge click — right _RRF_W pixels of safeguard cell
            if (row >= 0 and col == self._C_SG and row < len(self._row_meta)):
                sg_id = self._row_meta[row][3]
                if sg_id is not None:
                    cell_idx = self._table.model().index(row, col)
                    cr = self._table.visualRect(cell_idx)
                    if pos.x() >= cr.right() - _RRF_W:
                        gp = self._table.viewport().mapToGlobal(pos)
                        self._show_rrf_popup_at(row, sg_id, gp)
                        return True

        # Delegate inline editor (regular cell in edit mode)
        if (isinstance(obj, QLineEdit) and
                obj.property('editing_row') is not None and
                obj.property('sg_id') is None):
            if event.type() == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    row = obj.property('editing_row')
                    col = obj.property('editing_col')
                    self._delegate.commitData.emit(obj)
                    self._delegate.closeEditor.emit(obj, QStyledItemDelegate.EndEditHint.NoHint)
                    if ctrl:
                        self._ctrl_enter(row, col)
                    return True  # always consume Enter in editor — prevents table-level handler

        # Table-level keyboard shortcuts
        if obj is self._table and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                row = self._table.currentRow()
                col = self._table.currentColumn()
                self._ctrl_enter(row, col)
                return True
            if (event.key() == Qt.Key.Key_C and
                    event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._copy_row_to_clipboard(self._table.currentRow())
                return True
            if event.key() == Qt.Key.Key_Delete:
                self._delete_current_item()
                return True
            # F2 or any printable key → start inline edit on ORS/SG cells (feature 7)
            if event.key() == Qt.Key.Key_F2:
                row = self._table.currentRow()
                col = self._table.currentColumn()
                self._try_start_edit(row, col)
                return True
        return False

    def _ctrl_enter(self, row, col):
        """Enter on table (not in editor): create a new sibling at the same level."""
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, _sg_id = self._row_meta[row]
        if col in (self._C_ORS, self._C_NOD, self._C_DEV):
            if dev_id is not None:
                self._quick_add_cause(dev_id)
        elif col in (self._C_KON, self._C_RFORE):
            if cause_id is not None:
                self._quick_add_consequence(cause_id)
        else:
            if cons_id is not None:
                self._quick_add_safeguard(cons_id)

    def _on_enter_after_edit(self):
        row = self._enter_row
        if row < 0 or row >= len(self._row_meta):
            return
        item = self._table.item(row, self._enter_col)
        is_editable = item is not None and bool(item.flags() & Qt.ItemFlag.ItemIsEditable)
        if is_editable and not self._last_enter_committed:
            return
        self._last_enter_committed = False
        # Directly add next item based on column (no menu, feature 3)
        self._ctrl_enter(row, self._enter_col)

    def _show_quick_add(self, row, dev_id, cause_id, cons_id):
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        dev = self.db.get_deviation(dev_id) if dev_id else None
        dev_name = dev['description'] if dev else '?'

        menu = QMenu(self)
        menu.addSection("Lägg till i hierarkin")
        menu.addAction(f'⚙  Ny orsak under avvikelse  [{dev_name}]',
                       lambda: self._quick_add_cause(dev_id))
        menu.addAction("⚠  Ny konsekvens på denna orsak",
                       lambda: self._quick_add_consequence(cause_id))
        sg_action = menu.addAction("🛡  Ny safeguard på denna konsekvens",
                       lambda: self._quick_add_safeguard(cons_id))
        sg_action.setEnabled(cons_id is not None)

        idx   = self._table.model().index(row, self._C_ORS)
        rect  = self._table.visualRect(idx)
        pos   = self._table.viewport().mapToGlobal(rect.bottomLeft())
        menu.exec(pos)

    def _quick_add_cause(self, deviation_id):
        dev = self.db.get_deviation(deviation_id)
        dev_desc = dev['description'] if dev else ''
        node_id = dev['node_id'] if dev else None
        std_dev_id = _resolve_std_deviation_id(self.db, dev_desc)

        popup = StandardCausesPickerPopup(
            self.db, std_dev_id, deviation_name=dev_desc,
            node_id=node_id, parent=self)

        def _on_picked(description, frequency):
            new_id, cons_id = _create_cause_from_pick(self.db, deviation_id, description, frequency)
            # Jump straight to the new consequence's KON cell (not the cause's
            # own ORS cell) — the cause's description was already chosen in
            # the popup above, so typing the consequence is the next natural
            # step ("så fort jag lagt till en orsak", see NOTES.md).
            self.new_item_created.emit(CONS_T, cons_id)

        popup.cause_picked.connect(_on_picked)
        popup.exec()

    def _quick_add_consequence(self, cause_id):
        new_id = self.db.add_consequence(cause_id)
        self.new_item_created.emit(CONS_T, new_id)

    def _quick_add_safeguard(self, cons_id):
        new_id = self.db.add_safeguard(cons_id)
        self.new_item_created.emit(SG_T, new_id)

    def select_item(self, type_, id_):
        """Move the current cell to the row for (type_, id_) and start inline
        editing where supported (Orsak/Safeguard columns). Call this after
        refresh()/_rebuild() so the row/table is already populated — used
        when a new cause/consequence/safeguard was just created (e.g. via
        Enter-to-add-next-row), so the user's editing cursor stays on the
        new item instead of the table rebuild silently dropping selection
        and leaving the user unsure where they ended up."""
        col_for_type = {CAUSE_T: self._C_ORS, CONS_T: self._C_KON, SG_T: self._C_SG}
        col = col_for_type.get(type_)
        if col is None:
            return
        # index into each _row_meta tuple: (dev_id, cause_id, cons_id, sg_id)
        field_idx = {CAUSE_T: 1, CONS_T: 2, SG_T: 3}[type_]
        for row, meta in enumerate(self._row_meta):
            if meta[field_idx] == id_:
                self._table.setCurrentCell(row, col)
                item = self._table.item(row, col)
                if item is not None:
                    self._table.scrollToItem(item)
                self._try_start_edit(row, col)  # KON supported too since 2026-08-07 — see NOTES.md
                return

    def _on_cell_changed(self, row, col):
        try:
            self._on_cell_changed_inner(row, col)
        except Exception as e:
            QMessageBox.critical(self, "Fel vid celländring (scenario)", str(e))

    def _on_cell_changed_inner(self, row, col):
        item = self._table.item(row, col)
        if not item:
            return
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta:
            return
        kind, id_ = meta
        text = item.text().strip()

        if kind == 'cause':
            desc = text.split('\n')[0].strip()
            cause = self.db.get_cause(id_)
            if cause:
                # Check if the text is comp_tag (component tag) or description
                old_comp_tag = cause.get('comp_tag', '') or ''
                old_desc = cause.get('description', '') or ''

                # If the edited text matches old_comp_tag, we're editing comp_tag
                # Otherwise, we're editing description
                if desc and old_comp_tag and old_comp_tag.strip() == text:
                    # User edited comp_tag
                    self.db.update_cause(id_, comp_tag=desc)
                else:
                    # User edited description
                    self.db.update_cause(id_, desc)
                    # Sync any OTHER row showing this same cause (span groups
                    # merge same-id rows visually, but each still has its own
                    # QTableWidgetItem) — no full rebuild needed, see
                    # _update_row_text_only()'s docstring for why.
                    self._update_row_text_only('cause', id_, desc)
            self.item_edited.emit(CAUSE_T, id_)

        elif kind == 'consequence':
            desc = text.split('\n')[0].strip()
            cons = self.db.get_consequence(id_)
            if cons:
                self.db.update_consequence(id_, desc, cons['severity'], cons['category'] or '')
                self._update_row_text_only('consequence', id_, desc)
            self.item_edited.emit(CONS_T, id_)

        elif kind == 'safeguard':
            edit_val = item.data(Qt.ItemDataRole.EditRole)
            desc = (str(edit_val).strip() if edit_val is not None else text.split('\n')[0].strip()) or 'Ny safeguard'
            sg = self.db.get_safeguard(id_)
            if sg:
                self.db.update_safeguard(id_, desc, sg['rrf'] or 1)
                # A safeguard's description never affects its own row's RRF/
                # risk-derived columns (those depend on rrf, not text) or any
                # other row, so a full _rebuild() was pure overhead here —
                # patch the text in place instead (see _update_row_text_only).
                self._update_row_text_only('safeguard', id_, desc)
            self.item_edited.emit(SG_T, id_)

        if (row, col) == (self._enter_row, self._enter_col):
            self._last_enter_committed = True

    # ── Feature 7: try start inline edit ──────────────────────────────────────
    def _try_start_edit(self, row, col):
        # _C_KON added 2026-08-07 (see NOTES.md "Klicka direkt på
        # konsekvens") — the commit path (_on_cell_changed_inner's
        # 'consequence' branch) already existed and worked; only the
        # trigger was missing. Double-click still opens the step-by-step
        # chain wizard (_open_chain_editor) for anyone who wants that.
        if row < 0 or col not in (self._C_ORS, self._C_SG, self._C_KON):
            return
        item = self._table.item(row, col)
        if item and bool(item.flags() & Qt.ItemFlag.ItemIsEditable):
            self._table.setFocus()
            self._table.edit(self._table.model().index(row, col))

    # ── Feature 2: Ctrl+C clipboard copy ─────────────────────────────────────
    def _copy_row_to_clipboard(self, row):
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        parts = []
        def _txt(col):
            item = self._table.item(row, col)
            return item.text().strip() if item else ''
        parts.append(_txt(self._C_NOD))
        parts.append(_txt(self._C_DEV))
        c = self.db.get_cause(cause_id) if cause_id else None
        parts.append(dict(c).get('description', '') if c else '')
        k = self.db.get_consequence(cons_id) if cons_id else None
        parts.append(dict(k).get('description', '') if k else '')
        parts.append(_txt(self._C_RFORE))
        sg = self.db.get_safeguard(sg_id) if sg_id else None
        parts.append(dict(sg).get('description', '') if sg else '')
        parts.append(_txt(self._C_SLUT))
        QApplication.clipboard().setText('\t'.join(parts))

    # ── Feature: Delete key ───────────────────────────────────────────────────
    def _delete_current_item(self):
        row = self._table.currentRow()
        col = self._table.currentColumn()
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_SG and sg_id:
            if QMessageBox.question(self, "Ta bort barriär",
                    "Ta bort denna barriär?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    ) == QMessageBox.StandardButton.Yes:
                self.db.delete_safeguard(sg_id)
                self.structure_changed.emit()
                self._schedule_rebuild()
        elif col == self._C_KON and cons_id:
            if QMessageBox.question(self, "Ta bort konsekvens",
                    "Ta bort konsekvens och alla dess barriärer?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    ) == QMessageBox.StandardButton.Yes:
                self.db.delete_consequence(cons_id)
                self.structure_changed.emit()
                self._schedule_rebuild()
        elif col == self._C_ORS and cause_id:
            if QMessageBox.question(self, "Ta bort orsak",
                    "Ta bort orsak och alla konsekvenser/barriärer?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    ) == QMessageBox.StandardButton.Yes:
                self.db.delete_cause(cause_id)
                self.structure_changed.emit()
                self._schedule_rebuild()

    # ── Feature 1 & 6: Drag start ─────────────────────────────────────────────
    def _start_drag(self, row, col, is_copy_modifier):
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]
        if col == self._C_SG and sg_id:
            kind = 'sg'; item_id = sg_id
        elif col == self._C_ORS and cause_id:
            kind = 'cause'; item_id = cause_id
        elif col == self._C_KON and cons_id:
            kind = 'cons'; item_id = cons_id
        else:
            return

        mime = QMimeData()
        mime.setText(f'hzp:{kind}:{item_id}:{row}:{col}')

        # Drag pixmap: render the source cell
        idx = self._table.model().index(row, col)
        cell_rect = self._table.visualRect(idx)
        px = self._table.viewport().grab(cell_rect)
        pm = QPixmap(px.size())
        pm.fill(QColor(255, 255, 255, 180))
        p = QPainter(pm)
        p.drawPixmap(0, 0, px)
        p.end()

        drag = QDrag(self._table)
        drag.setMimeData(mime)
        drag.setPixmap(pm)
        drag.setHotSpot(pm.rect().center())
        action = (Qt.DropAction.CopyAction if is_copy_modifier
                  else Qt.DropAction.MoveAction | Qt.DropAction.CopyAction)
        drag.exec(action)

    def _handle_drop(self, event, source_obj=None):
        text = event.mimeData().text()
        if not text.startswith('hzp:'):
            return
        parts = text.split(':')
        if len(parts) < 5:
            return
        kind, item_id_s, src_row_s = parts[1], parts[2], parts[3]
        try:
            src_row = int(src_row_s)
        except ValueError:
            return
        is_copy = bool(event.dropAction() == Qt.DropAction.CopyAction)

        # Find target row/col from drop position. The event's position is
        # relative to whichever widget it was actually delivered to
        # (source_obj) — only remap it into viewport coordinates when it
        # came in relative to the outer table widget; if it's already
        # viewport-relative (the common case — see the eventFilter comment
        # above), remapping again would shift it by the header/frame
        # offset a second time and silently miss the target row.
        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        if source_obj is self._table:
            vp_pos = self._table.viewport().mapFrom(self._table, pos)
        else:
            vp_pos = pos
        tgt_row = self._table.rowAt(vp_pos.y())
        tgt_col = self._table.columnAt(vp_pos.x())
        if tgt_row < 0 or tgt_row >= len(self._row_meta):
            event.ignore(); return

        tgt_dev, tgt_cause, tgt_cons, tgt_sg = self._row_meta[tgt_row]

        if kind in ('equipment', 'equipment-multi'):
            # Shift-drag of one or more P&ID equipment markers onto a KON
            # or SG cell. `item_id_s` is a comma-separated list of
            # equipment_markers.id values (a single id for the plain
            # 'equipment' kind). Dropping several objects at once (2026-08-09,
            # see NOTES.md) behaves differently per target: onto a
            # CONSEQUENCE, every dragged object builds up the SAME
            # consequence's text (they describe one scenario in sequence,
            # e.g. "TA-1 ... TA-2"); onto a SAFEGUARD, only the first
            # object goes onto the cell actually dropped on — each
            # additional object becomes its OWN new safeguard row under
            # the same consequence, since distinct objects there read as
            # distinct barriers, not one merged sentence.
            try:
                marker_ids = [int(s) for s in item_id_s.split(',') if s.strip()]
            except ValueError:
                event.ignore(); return
            if not marker_ids:
                event.ignore(); return
            equips = [e for e in (self.db.get_equipment_by_marker_id(m) for m in marker_ids) if e]
            if not equips:
                event.ignore(); return
            if tgt_col == self._C_KON and tgt_cons is not None:
                for equip in equips:
                    self.db.append_tag_to_consequence(
                        tgt_cons, equip.get('tag', ''), equip.get('equipment_type', ''))
            elif tgt_col == self._C_SG and tgt_sg is not None:
                # The dropped-on row only ever absorbs an object if it has
                # no object on it yet — once it already carries a tag
                # (from an earlier drop, single or multi), a NEW drop must
                # still land on its own new row, not merge into that
                # row's text (2026-08-09, see NOTES.md: "jag vill att den
                # ... skall lägga till flera olika objekt om jag drar till
                # safeguards med (flera rader)" — applies whether the
                # extra objects arrive in one multi-select drag or as
                # separate later single-object drags onto the same row).
                sg_row = self.db.get_safeguard(tgt_sg)
                row_is_free = bool(sg_row) and not (sg_row.get('tagged_refs') or '').strip()
                for i, equip in enumerate(equips):
                    if i == 0 and row_is_free:
                        self.db.append_tag_to_safeguard(
                            tgt_sg, equip.get('tag', ''), equip.get('equipment_type', ''))
                    else:
                        new_sg_id = self.db.add_safeguard(tgt_cons)
                        self.db.append_tag_to_safeguard(
                            new_sg_id, equip.get('tag', ''), equip.get('equipment_type', ''))
            else:
                event.ignore(); return
            self._schedule_rebuild()
            event.acceptProposedAction()
            return

        try:
            item_id = int(item_id_s)
        except ValueError:
            return

        if kind == 'sg':
            if tgt_cons is None or tgt_cons == self._row_meta[src_row][2]:
                event.ignore(); return
            if is_copy:
                self.db.copy_safeguard(item_id, tgt_cons)
            else:
                self.db.move_safeguard(item_id, tgt_cons)
            self._schedule_rebuild()
            QTimer.singleShot(0, self.structure_changed.emit)
            event.acceptProposedAction()

        elif kind == 'cons':
            if tgt_cause is None or tgt_cause == self._row_meta[src_row][1]:
                event.ignore(); return
            if is_copy:
                self.db.copy_consequence(item_id, tgt_cause)
            else:
                self.db.move_consequence(item_id, tgt_cause)
            self._schedule_rebuild()
            QTimer.singleShot(0, self.structure_changed.emit)
            event.acceptProposedAction()

        elif kind == 'cause':
            if tgt_dev is None or tgt_dev == self._row_meta[src_row][0]:
                event.ignore(); return
            if is_copy:
                self.db.copy_cause(item_id, tgt_dev)
            else:
                self.db.move_cause_to_deviation(item_id, tgt_dev)
            self._schedule_rebuild()
            QTimer.singleShot(0, self.structure_changed.emit)
            event.acceptProposedAction()

    # ── Feature 4 & 5: Context menu ───────────────────────────────────────────
    def _on_context_menu(self, pos):
        row = self._table.rowAt(pos.y())
        col = self._table.columnAt(pos.x())
        if row < 0 or row >= len(self._row_meta):
            return
        dev_id, cause_id, cons_id, sg_id = self._row_meta[row]

        menu = QMenu(self)

        # ── Ctrl+C shortcut hint ────────────────────────────────────────
        copy_row = menu.addAction("📋  Kopiera rad  (Ctrl+C)")
        copy_row.triggered.connect(lambda: self._copy_row_to_clipboard(row))
        menu.addSeparator()

        # ── Orsak-åtgärder ──────────────────────────────────────────────
        if col in (self._C_ORS, self._C_NOD, self._C_DEV) and cause_id:
            c = self.db.get_cause(cause_id)
            c_desc = dict(c).get('description', '?')[:40] if c else '?'
            menu.addSection(f"⚙ Orsak: {c_desc}")
            menu.addAction("✏  Redigera",
                lambda: self._try_start_edit(row, self._C_ORS))
            a_dup = menu.addAction("📄  Duplicera orsak (med konsekvenser)")
            a_dup.triggered.connect(
                lambda: self._duplicate_cause(cause_id))
            a_move = menu.addAction("↕  Flytta till annan avvikelse…")
            a_move.triggered.connect(
                lambda: self._move_cause_dialog(cause_id))
            menu.addSeparator()
            a_del = menu.addAction("🗑  Ta bort orsak")
            a_del.triggered.connect(lambda cid=cause_id: self._confirm_delete('cause', cid))

        # ── Konsekvens-åtgärder ─────────────────────────────────────────
        elif col in (self._C_KON, self._C_RFORE) and cons_id:
            k = self.db.get_consequence(cons_id)
            k_desc = dict(k).get('description', '?')[:40] if k else '?'
            menu.addSection(f"⚠ Konsekvens: {k_desc}")
            a_dup = menu.addAction("📄  Duplicera konsekvens (med barriärer)")
            a_dup.triggered.connect(
                lambda: self._duplicate_consequence(cons_id, cause_id))
            a_move = menu.addAction("↕  Flytta till annan orsak…")
            a_move.triggered.connect(
                lambda: self._move_consequence_dialog(cons_id))
            if k and (dict(k).get('comp_tag') or dict(k).get('comp_type')):
                a_untag = menu.addAction("✕  Ta bort tagg")
                a_untag.triggered.connect(lambda cid=cons_id: self._untag_consequence(cid))
            menu.addSeparator()
            a_del = menu.addAction("🗑  Ta bort konsekvens")
            a_del.triggered.connect(lambda cid=cons_id: self._confirm_delete('cons', cid))

        # ── Barriär-åtgärder ────────────────────────────────────────────
        elif col in (self._C_SG, self._C_LOPA, self._C_SLUT) and sg_id:
            sg = self.db.get_safeguard(sg_id)
            sg_desc = dict(sg).get('description', '?')[:40] if sg else '?'
            menu.addSection(f"🛡 Barriär: {sg_desc}")
            menu.addAction("✏  Redigera",
                lambda: self._try_start_edit(row, self._C_SG))
            a_copy = menu.addAction("📋  Kopiera till annan konsekvens…")
            a_copy.triggered.connect(
                lambda: self._copy_safeguard_dialog(sg_id))
            a_move = menu.addAction("↕  Flytta till annan konsekvens…")
            a_move.triggered.connect(
                lambda: self._move_safeguard_dialog(sg_id))
            if sg and (dict(sg).get('comp_tag') or dict(sg).get('comp_type')):
                a_untag = menu.addAction("✕  Ta bort tagg")
                a_untag.triggered.connect(lambda sid=sg_id: self._untag_safeguard(sid))
            menu.addSeparator()
            a_del = menu.addAction("🗑  Ta bort barriär")
            a_del.triggered.connect(lambda sid=sg_id: self._confirm_delete('sg', sid))

        if not menu.isEmpty():
            menu.exec(self._table.viewport().mapToGlobal(pos))

    def _untag_consequence(self, cons_id):
        """Detach a dragged-in equipment tag from a KON cell without
        deleting the row — the inline "×" this replaced sat in the tag
        strip, which was removed 2026-08-10 (see NOTES.md; the tag still
        shows bolded in the description text via tagged_refs)."""
        self.db.set_consequence_tag(cons_id, '', '')
        self._schedule_rebuild()

    def _untag_safeguard(self, sg_id):
        """Same as _untag_consequence, for a safeguard cell."""
        self.db.set_safeguard_tag(sg_id, '', '')
        self._schedule_rebuild()

    def _confirm_delete(self, kind, item_id):
        labels = {'cause': ('orsak', 'cause'), 'cons': ('konsekvens', 'consequence'),
                  'sg': ('barriär', 'safeguard')}
        swe, db_kind = labels.get(kind, (kind, kind))
        if QMessageBox.question(self, f"Ta bort {swe}",
                f"Ta bort {swe}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) == QMessageBox.StandardButton.Yes:
            if db_kind == 'cause':
                self.db.delete_cause(item_id)
            elif db_kind == 'consequence':
                self.db.delete_consequence(item_id)
            else:
                self.db.delete_safeguard(item_id)
            self.structure_changed.emit()
            self._schedule_rebuild()

    # ── Feature 5: Duplicate ──────────────────────────────────────────────────
    def _duplicate_consequence(self, cons_id, cause_id):
        new_id = self.db.copy_consequence(cons_id, cause_id)
        if new_id:
            self.structure_changed.emit()
            self._schedule_rebuild()

    def _duplicate_cause(self, cause_id):
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        dev_id = dict(cause).get('deviation_id')
        if dev_id is None:
            return
        new_id = self.db.copy_cause(cause_id, dev_id)
        if new_id:
            self.structure_changed.emit()
            self._schedule_rebuild()

    # ── Feature 6: Move dialogs ───────────────────────────────────────────────
    def _move_cause_dialog(self, cause_id):
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        node_id = dict(cause)['node_id']
        cur_dev = dict(cause).get('deviation_id')
        devs = [d for d in self.db.deviations(node_id) if d['id'] != cur_dev]
        if not devs:
            QMessageBox.information(self, "Flytta orsak",
                "Ingen annan avvikelse finns under denna nod.\n"
                "Lägg till fler avvikelser i trädet först.")
            return
        items = [f"{d['description']}" for d in devs]
        choice, ok = QInputDialog.getItem(self, "Flytta orsak",
            "Välj målавvikelse:", items, 0, False)
        if ok:
            idx = items.index(choice)
            self.db.move_cause_to_deviation(cause_id, devs[idx]['id'])
            self.structure_changed.emit()
            self._schedule_rebuild()

    def _move_consequence_dialog(self, cons_id):
        cons = self.db.get_consequence(cons_id)
        if not cons:
            return
        cur_cause = dict(cons)['cause_id']
        cur_cause_row = self.db.get_cause(cur_cause)
        if not cur_cause_row:
            return
        node_id = dict(cur_cause_row)['node_id']
        all_causes = []
        for dev in self.db.deviations(node_id):
            for c in self.db.causes_for_deviation(dev['id']):
                if c['id'] != cur_cause:
                    all_causes.append((c, dev))
        if not all_causes:
            QMessageBox.information(self, "Flytta konsekvens",
                "Ingen annan orsak finns under denna nod.")
            return
        items = [f"{dev['description']} → {c['description']}"
                 for c, dev in all_causes]
        choice, ok = QInputDialog.getItem(self, "Flytta konsekvens",
            "Välj målorsak:", items, 0, False)
        if ok:
            idx = items.index(choice)
            tgt_cause_id = all_causes[idx][0]['id']
            self.db.move_consequence(cons_id, tgt_cause_id)
            self.structure_changed.emit()
            self._schedule_rebuild()

    def _copy_safeguard_dialog(self, sg_id):
        self._pick_target_cons_dialog(sg_id, move=False)

    def _move_safeguard_dialog(self, sg_id):
        self._pick_target_cons_dialog(sg_id, move=True)

    def _pick_target_cons_dialog(self, sg_id, move=False):
        sg = self.db.get_safeguard(sg_id)
        if not sg:
            return
        cur_cons = dict(sg)['consequence_id']
        # Collect all consequences across all nodes
        all_cons = []
        for node in self.db.nodes():
            for dev in self.db.deviations(node['id']):
                for cause in self.db.causes_for_deviation(dev['id']):
                    for cons in self.db.consequences(cause['id']):
                        if cons['id'] != cur_cons:
                            all_cons.append((cons, cause, dev, node))
        if not all_cons:
            QMessageBox.information(self, "Välj konsekvens",
                "Inga andra konsekvenser finns.")
            return
        items = [f"{n['name']} / {d['description']} / {c['description'][:30]} / {k['description'][:30]}"
                 for k, c, d, n in all_cons]
        verb = "Flytta" if move else "Kopiera"
        choice, ok = QInputDialog.getItem(self, f"{verb} barriär",
            "Välj målkonsekvens:", items, 0, False)
        if ok:
            idx = items.index(choice)
            tgt_cons_id = all_cons[idx][0]['id']
            if move:
                self.db.move_safeguard(sg_id, tgt_cons_id)
            else:
                self.db.copy_safeguard(sg_id, tgt_cons_id)
            self.structure_changed.emit()
            self._schedule_rebuild()


# ══════════════════════════════════════════════════════════════════════════════
# HAZOP WORKSHEET
# ══════════════════════════════════════════════════════════════════════════════

class HAZOPWorksheet(QWidget):
    """Worksheet page: mirrors the full HAZOP hierarchy (Nod → Avvikelse →
    Orsak → Konsekvens → Barriärer) for one node at a time via a dropdown,
    or the entire study at once via "Visa samtliga noder".

    Reuses ScenarioTablePanel (the same row-building/editing logic used on
    the main P&ID page) instead of duplicating it in a second flat table —
    see load_all()/_all_nodes on ScenarioTablePanel.
    """

    def __init__(self, db: Database, main_window=None):
        super().__init__()
        self.db = db
        # Optional back-reference to MainWindow, used only to wire the
        # embedded ScenarioTablePanel's navigate_to_pid signal (jumping to
        # the P&ID view from a row here). Left None-safe throughout so this
        # widget still works standalone (e.g. in tests).
        self._main_window = main_window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Top bar: node picker + "show all nodes" checkbox
        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("Nod:"))
        self._node_combo = QComboBox()
        self._node_combo.setMinimumWidth(240)
        top_bar.addWidget(self._node_combo)
        self._all_nodes_cb = QCheckBox("Visa samtliga noder")
        top_bar.addWidget(self._all_nodes_cb)
        self._show_empty_dev_cb = QCheckBox("Visa avvikelser utan orsaker")
        top_bar.addWidget(self._show_empty_dev_cb)
        top_bar.addStretch()
        layout.addLayout(top_bar)

        # Embedded scenario table (full hierarchy for selected node, or all nodes).
        # Lift the 380px height cap ScenarioTablePanel normally uses for the
        # P&ID page's bottom-splitter placement — here it owns the whole page,
        # so it should fill all available vertical space instead of leaving a
        # large blank gap below/around a height-capped table.
        self._table_panel = ScenarioTablePanel(db)
        self._table_panel.allow_full_height()
        # Avvikelse column should always be visible here — there's no separate
        # deviation-picker (only a node dropdown), and rows aren't distinguishable
        # by node/deviation otherwise when neither checkbox above is checked.
        self._table_panel.always_show_deviation_column()
        layout.addWidget(self._table_panel, 1)

        self._node_combo.currentIndexChanged.connect(self._on_node_combo_changed)
        self._all_nodes_cb.toggled.connect(self._on_all_nodes_toggled)
        self._show_empty_dev_cb.toggled.connect(self._table_panel.set_show_empty_deviations)

        # navigate_to_pid ("go to P&ID" pin click on a row): MainWindow's own
        # scenario_panel wires this to _on_scenario_navigate_to_pid, which
        # switches view_stack to the P&ID page and zooms to the marker. Reuse
        # that exact handler here when a MainWindow reference is available,
        # so the embedded worksheet instance behaves identically. Left
        # unconnected when main_window is None (e.g. headless/unit tests).
        if self._main_window is not None:
            self._table_panel.navigate_to_pid.connect(
                self._main_window._on_scenario_navigate_to_pid)
        # place_requested (placing NEW markers on the P&ID canvas) is not
        # wired: it only makes sense in the P&ID page's own placement-mode
        # context, not from the Worksheet page.
        # item_selected (row click -> update right-hand properties ribbon)
        # is not wired for v1: the Worksheet page has no properties ribbon
        # of its own, and piping it to MainWindow's ribbon would couple this
        # page to P&ID-page-only UI state for little benefit.

        self._populate_node_combo()

    def _populate_node_combo(self):
        """Refill the node dropdown from the DB, preserving the current selection if possible."""
        current_id = self._node_combo.currentData() if self._node_combo.count() else None
        self._node_combo.blockSignals(True)
        try:
            self._node_combo.clear()
            for node in self.db.nodes():
                self._node_combo.addItem(node['name'] or f"Nod {node['id']}", node['id'])
            if current_id is not None:
                idx = self._node_combo.findData(current_id)
                if idx >= 0:
                    self._node_combo.setCurrentIndex(idx)
        finally:
            self._node_combo.blockSignals(False)

    def _on_node_combo_changed(self, idx):
        if self._all_nodes_cb.isChecked():
            return  # combo is disabled in all-nodes mode; ignore stray signals
        node_id = self._node_combo.currentData()
        if node_id is not None:
            self._table_panel.load_node(node_id)

    def _on_all_nodes_toggled(self, checked):
        self._node_combo.setEnabled(not checked)
        if checked:
            self._table_panel.load_all()
        else:
            node_id = self._node_combo.currentData()
            if node_id is not None:
                self._table_panel.load_node(node_id)

    def refresh(self):
        """Called when the Worksheet page becomes visible (MainWindow._switch_view page==1)."""
        self._populate_node_combo()
        if self._all_nodes_cb.isChecked():
            self._table_panel.load_all()
        elif self._node_combo.count() > 0:
            node_id = self._node_combo.currentData()
            if node_id is not None:
                self._table_panel.load_node(node_id)


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS PANEL
# ══════════════════════════════════════════════════════════════════════════════

class ComponentEditorPanel(QWidget):
    """Settings panel for managing component types and failure modes."""

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._cur_comp_id = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # ── Left: component type list ─────────────────────────────────────────
        left = QVBoxLayout()
        left.addWidget(QLabel("Komponenttyper:"))
        self._comp_list = QListWidget()
        self._comp_list.currentItemChanged.connect(self._on_comp_selected)
        left.addWidget(self._comp_list)

        comp_btns = QHBoxLayout()
        btn_add_c  = QPushButton("+ Lägg till")
        btn_ren_c  = QPushButton("✎ Byt namn")
        btn_del_c  = QPushButton("✕ Ta bort")
        btn_add_c.clicked.connect(self._comp_add)
        btn_ren_c.clicked.connect(self._comp_rename)
        btn_del_c.clicked.connect(self._comp_delete)
        for b in [btn_add_c, btn_ren_c, btn_del_c]:
            comp_btns.addWidget(b)
        left.addLayout(comp_btns)
        layout.addLayout(left, 1)

        # ── Right: failure modes table ────────────────────────────────────────
        right = QVBoxLayout()
        right.addWidget(QLabel("Felmoder för vald komponent:"))

        self._mode_table = QTableWidget(0, 3)
        self._mode_table.setHorizontalHeaderLabels(
            ['Beskrivning', 'Frekvens (/år)', 'F-nivå (auto)'])
        h = self._mode_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._mode_table.setColumnWidth(1, 110)
        self._mode_table.setColumnWidth(2, 90)
        self._mode_table.verticalHeader().setVisible(False)
        self._mode_table.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;"
            "font-weight:600;padding:3px;}")
        self._mode_table.cellChanged.connect(self._on_mode_cell)
        right.addWidget(self._mode_table)

        mode_btns = QHBoxLayout()
        btn_add_m = QPushButton("+ Lägg till felmod")
        btn_del_m = QPushButton("✕ Ta bort vald")
        btn_add_m.clicked.connect(self._mode_add)
        btn_del_m.clicked.connect(self._mode_delete)
        mode_btns.addWidget(btn_add_m)
        mode_btns.addWidget(btn_del_m)
        mode_btns.addStretch()
        right.addLayout(mode_btns)

        freq_note = QLabel(
            "Frekvens i händelser/år.  Exempel: 0.05/år = en gång per 20 år → F=3 (10-100 år)\n"
            "F-nivån beräknas automatiskt från frekvensgränserna i riskmatrisen.")
        freq_note.setStyleSheet("color:#666; font-size:10px;")
        right.addWidget(freq_note)

        layout.addLayout(right, 2)
        self._refresh_comp_list()

    # ── Component list ────────────────────────────────────────────────────────

    def _refresh_comp_list(self):
        self._comp_list.blockSignals(True)
        self._comp_list.clear()
        for ct in self.db.component_types():
            item = QListWidgetItem(ct['name'])
            item.setData(Qt.ItemDataRole.UserRole, ct['id'])
            self._comp_list.addItem(item)
        self._comp_list.blockSignals(False)
        if self._cur_comp_id:
            for i in range(self._comp_list.count()):
                if self._comp_list.item(i).data(Qt.ItemDataRole.UserRole) == self._cur_comp_id:
                    self._comp_list.setCurrentRow(i)
                    break

    def _on_comp_selected(self, current, _prev):
        if current:
            self._cur_comp_id = current.data(Qt.ItemDataRole.UserRole)
            self._refresh_mode_table()
        else:
            self._cur_comp_id = None
            self._mode_table.setRowCount(0)

    def _comp_add(self):
        name, ok = QInputDialog.getText(self, "Ny komponenttyp", "Namn:")
        if ok and name.strip():
            self._cur_comp_id = self.db.add_component_type(name.strip())
            self._refresh_comp_list()

    def _comp_rename(self):
        item = self._comp_list.currentItem()
        if not item: return
        name, ok = QInputDialog.getText(self, "Byt namn", "Nytt namn:", text=item.text())
        if ok and name.strip():
            self.db.update_component_type(item.data(Qt.ItemDataRole.UserRole), name.strip())
            self._refresh_comp_list()

    def _comp_delete(self):
        item = self._comp_list.currentItem()
        if not item: return
        reply = QMessageBox.question(self, "Ta bort",
            f"Ta bort '{item.text()}' och alla dess felmoder?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.db.delete_component_type(item.data(Qt.ItemDataRole.UserRole))
            self._cur_comp_id = None
            self._refresh_comp_list()
            self._mode_table.setRowCount(0)

    # ── Failure modes table ───────────────────────────────────────────────────

    def _refresh_mode_table(self):
        try:
            self._mode_table.cellChanged.disconnect()
        except Exception:
            pass
        self._mode_table.setRowCount(0)
        if not self._cur_comp_id:
            self._mode_table.cellChanged.connect(self._on_mode_cell)
            return

        for fm in self.db.failure_modes(self._cur_comp_id):
            r = self._mode_table.rowCount()
            self._mode_table.insertRow(r)

            desc = QTableWidgetItem(fm['description'])
            desc.setData(Qt.ItemDataRole.UserRole, fm['id'])
            self._mode_table.setItem(r, 0, desc)

            freq = fm['freq_per_year']
            freq_item = QTableWidgetItem(
                f"{freq:.4g}" if freq is not None else "")
            freq_item.setToolTip("Händelser per år, t.ex. 0.05 (en gång per 20 år)")
            self._mode_table.setItem(r, 1, freq_item)

            f_level = freq_to_f_level(freq) if freq else None
            f_item = QTableWidgetItem(
                f"F={f_level}" if f_level is not None else "—")
            f_item.setFlags(f_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if f_level is not None:
                label, bg, _ = risk_info(f_level, 3)
                f_item.setBackground(QBrush(QColor(bg)))
                f_item.setForeground(QBrush(QColor('#fff')))
                f_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._mode_table.setItem(r, 2, f_item)
            self._mode_table.setRowHeight(r, 26)

        self._mode_table.cellChanged.connect(self._on_mode_cell)

    def _mode_add(self):
        if not self._cur_comp_id:
            QMessageBox.information(self, "Välj komponent",
                "Välj en komponenttyp i listan till vänster.")
            return
        self.db.add_failure_mode(self._cur_comp_id, "Ny felmod")
        self._refresh_mode_table()

    def _mode_delete(self):
        rows = {idx.row() for idx in self._mode_table.selectedIndexes()}
        if not rows: return
        for r in sorted(rows, reverse=True):
            item = self._mode_table.item(r, 0)
            if item:
                self.db.delete_failure_mode(item.data(Qt.ItemDataRole.UserRole))
        self._refresh_mode_table()

    def _on_mode_cell(self, row, col):
        item0 = self._mode_table.item(row, 0)
        if not item0: return
        fm_id = item0.data(Qt.ItemDataRole.UserRole)
        desc  = item0.text().strip() or 'Ny felmod'
        freq_item = self._mode_table.item(row, 1)
        freq = None
        if freq_item:
            try:
                freq = float(freq_item.text().strip()) if freq_item.text().strip() else None
            except ValueError:
                freq = None
        self.db.update_failure_mode(fm_id, desc, freq)
        # Update F-level cell
        f_level = freq_to_f_level(freq) if freq else None
        f_item = self._mode_table.item(row, 2)
        if f_item:
            self._mode_table.blockSignals(True)
            f_item.setText(f"F={f_level}" if f_level is not None else "—")
            if f_level is not None:
                _, bg, _ = risk_info(f_level, 3)
                f_item.setBackground(QBrush(QColor(bg)))
                f_item.setForeground(QBrush(QColor('#fff')))
            self._mode_table.blockSignals(False)


class _ComboBoxCellDelegate(QStyledItemDelegate):
    """Editable-combo-box cell for a QTableView, without a persistent QComboBox
    per row. The combo only exists while a cell is actually being edited —
    used by PIDAnalysisPanel and EquipmentPanel, both of which used to embed
    one real QComboBox per row via setCellWidget(); with thousands of rows
    that alone took tens of seconds to build. Pair with a view whose
    `clicked` signal calls view.edit(index) for this column so a single
    click opens the dropdown, matching the old always-visible-combo feel."""

    def __init__(self, items, parent=None):
        super().__init__(parent)
        self._items = items

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.addItems(self._items)
        return combo

    def setEditorData(self, editor, index):
        text = index.data(Qt.ItemDataRole.EditRole) or ''
        i = editor.findText(text)
        editor.setCurrentIndex(i if i >= 0 else 0)
        editor.showPopup()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)


class _ButtonCellDelegate(QStyledItemDelegate):
    """Paints a push-button label in a QTableView cell without a persistent
    QPushButton per row (same rationale as _ComboBoxCellDelegate above).
    on_click(index) is called with the *view's* model index (which may be a
    proxy index — map through the proxy before touching the source model)."""

    def __init__(self, text, on_click, parent=None):
        super().__init__(parent)
        self._text     = text
        self._on_click = on_click

    def paint(self, painter, option, index):
        opt = QStyleOptionButton()
        opt.rect  = option.rect.adjusted(3, 2, -3, -2)
        opt.text  = self._text
        opt.state = QStyle.StateFlag.State_Enabled
        QApplication.style().drawControl(QStyle.ControlElement.CE_PushButton, opt, painter)

    def editorEvent(self, event, model, option, index):
        if (event.type() == QEvent.Type.MouseButtonRelease
                and option.rect.contains(event.pos())):
            self._on_click(index)
            return True
        return False

    def createEditor(self, parent, option, index):
        return None   # never a real editor widget — clicks are handled above


_PA_CODE, _PA_EX, _PA_SUGG, _PA_TYPE, _PA_USE = range(5)
_PA_HEADERS = ['Prefix', 'Exempeltaggar', 'Databas-förslag', 'Komponenttyp', 'Använd ✓']


class _IdentifiedTagsModel(QAbstractTableModel):
    """Backs PIDAnalysisPanel's QTableView. Rows are kept as plain dicts in
    memory (cheap) and DB writes happen in setData() — no per-row widgets."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db    = db
        self._rows = []   # list[dict], one per pid_identified_tags row

    def load(self):
        self.beginResetModel()
        self._rows = [dict(r) for r in self.db.pid_identified_tags()]
        self.endResetModel()

    def rows(self):
        return self._rows

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 5

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _PA_HEADERS[section]
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        col = index.column()
        if col == _PA_TYPE:
            return base | Qt.ItemFlag.ItemIsEditable
        if col == _PA_USE:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == _PA_CODE: return row['tag_code']
            if col == _PA_EX:   return row['examples'] or ''
            if col == _PA_SUGG: return row['name_sv'] or '—'
            if col == _PA_TYPE: return row['comp_type'] or ''
            return None
        if role == Qt.ItemDataRole.CheckStateRole and col == _PA_USE:
            return Qt.CheckState.Checked if row['confirmed'] else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.FontRole and col == _PA_CODE:
            return QFont('Courier', 10)
        if role == Qt.ItemDataRole.ForegroundRole:
            if col == _PA_EX:   return QBrush(QColor('#555555'))
            if col == _PA_SUGG: return QBrush(QColor('#8D9299'))
        if role == Qt.ItemDataRole.TextAlignmentRole and col == _PA_USE:
            return Qt.AlignmentFlag.AlignCenter
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        row = self._rows[index.row()]
        col = index.column()
        try:
            if role == Qt.ItemDataRole.CheckStateRole and col == _PA_USE:
                confirmed = value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
                row['confirmed'] = 1 if confirmed else 0
                self.db.confirm_pid_tag(row['tag_code'], row['comp_type'] or '', confirmed)
            elif role == Qt.ItemDataRole.EditRole and col == _PA_TYPE:
                row['comp_type'] = str(value)
                self.db.confirm_pid_tag(row['tag_code'], row['comp_type'], bool(row['confirmed']))
            else:
                return False
        except Exception:
            logging.exception('_IdentifiedTagsModel.setData: DB write failed (row=%d col=%d)',
                              index.row(), col)
            return False
        self.dataChanged.emit(index, index, [role])
        return True

    def bulk_set_confirmed(self, confirm: bool):
        """Set 'confirmed' for every row with a single commit — setData()
        commits per call, which is fine for one edit but would mean one
        fsync-ish SQLite commit per row (thousands of them) for 'Välj alla'."""
        if not self._rows:
            return
        conf = 1 if confirm else 0
        for row in self._rows:
            row['confirmed'] = conf
        try:
            self.db.conn.executemany(
                "UPDATE pid_identified_tags SET comp_type=?,confirmed=? WHERE tag_code=?",
                [(row['comp_type'] or '', conf, row['tag_code']) for row in self._rows])
            self.db.conn.commit()
        except Exception:
            logging.exception('_IdentifiedTagsModel.bulk_set_confirmed: DB write failed')
            return
        self.dataChanged.emit(self.index(0, _PA_USE), self.index(len(self._rows) - 1, _PA_USE),
                              [Qt.ItemDataRole.CheckStateRole])


class PIDAnalysisPanel(QWidget):
    """Settings panel: shows all tag prefixes found in the P&ID with component-type mapping."""

    # Component types available for selection
    _COMP_TYPES = [
        '', 'Ventil', 'Säkerhetsventil (PSV)', 'Pump', 'Kompressor',
        'Tank / Kärl', 'Värmeväxlare', 'Instrument / Sensor',
        'Rörledning', 'Övrigt',
    ]

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._loaded = False   # first refresh() deferred to showEvent — see below
        self._model  = _IdentifiedTagsModel(db, self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        hdr = QHBoxLayout()
        title = QLabel("Identifierade objekt — P&ID-nyckel")
        f = QFont(); f.setBold(True); f.setPointSize(13)
        title.setFont(f)
        hdr.addWidget(title)
        hdr.addStretch()

        note = QLabel(
            "Kryssa i 'Använd' för att pre-fylla orsaksmenyn med rätt komponenttyp.")
        note.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(title)
        layout.addWidget(note)

        # Table — QTableView + QAbstractTableModel instead of QTableWidget:
        # populating this used to mean inserting one row (with a real
        # QComboBox widget) per identified tag prefix, which does not scale.
        # See _IdentifiedTagsModel / _ComboBoxCellDelegate above.
        self._tbl = QTableView()
        self._tbl.setModel(self._model)
        self._tbl.setItemDelegateForColumn(
            _PA_TYPE, _ComboBoxCellDelegate(self._COMP_TYPES, self._tbl))
        h = self._tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._tbl.setColumnWidth(0, 70)
        self._tbl.setColumnWidth(2, 180)
        self._tbl.setColumnWidth(3, 160)
        self._tbl.setColumnWidth(4, 70)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.verticalHeader().setDefaultSectionSize(28)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                  QAbstractItemView.EditTrigger.EditKeyPressed)
        self._tbl.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:3px;}")
        self._tbl.clicked.connect(self._on_cell_clicked)
        layout.addWidget(self._tbl)

        btn_row = QHBoxLayout()
        sel_all = QPushButton("Välj alla")
        sel_all.clicked.connect(lambda: self._bulk_confirm(True))
        desel   = QPushButton("Avmarkera alla")
        desel.clicked.connect(lambda: self._bulk_confirm(False))
        btn_row.addWidget(sel_all); btn_row.addWidget(desel); btn_row.addStretch()
        self._status = QLabel("")
        self._status.setStyleSheet("color:#555; font-size:10px;")
        btn_row.addWidget(self._status)
        layout.addLayout(btn_row)

        self._model.dataChanged.connect(lambda *a: self._update_status())

    def showEvent(self, event):
        # See _IdentifiedTagsModel docstring: populating used to block the
        # whole app at startup even when the user never opens Inställningar
        # → Identifierade objekt. Defer to the first time the tab is shown.
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.refresh()

    def _on_cell_clicked(self, index):
        if index.column() == _PA_TYPE:
            self._tbl.edit(index)

    def refresh(self):
        self._loaded = True   # any explicit refresh() satisfies showEvent's lazy-load too
        self._model.load()
        self._update_status()

    def _bulk_confirm(self, confirm: bool):
        self._model.bulk_set_confirmed(confirm)
        self._update_status()

    def _update_status(self):
        total     = self._model.rowCount()
        confirmed = sum(1 for row in self._model.rows() if row['confirmed'])
        self._status.setText(f"{total} prefix hittade  |  {confirmed} bekräftade")


class TagDatabasePanel(QWidget):
    """Settings panel for managing the P&ID tag-code database."""

    settings_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("Tagdatabas — P&ID taggkodnycklar")
        f = QFont(); f.setBold(True); f.setPointSize(13)
        title.setFont(f)
        layout.addWidget(title)

        # ── Import section ────────────────────────────────────────────────────
        import_box = QGroupBox("Importera Excel-databas")
        imp_lay = QHBoxLayout(import_box)
        self._excel_lbl = QLabel("Ingen fil vald")
        self._excel_lbl.setStyleSheet("color:#555;")
        imp_lay.addWidget(self._excel_lbl, 1)
        imp_btn = QPushButton("📂 Välj Excel-fil…")
        imp_btn.clicked.connect(self._import_excel)
        imp_lay.addWidget(imp_btn)
        layout.addWidget(import_box)

        # ── Standard selection ────────────────────────────────────────────────
        std_box = QGroupBox("Aktiv standard")
        std_lay = QHBoxLayout(std_box)
        std_lay.addWidget(QLabel("Följ standard:"))
        self._std_combo = QComboBox()
        self._std_combo.addItem("Alla standarder (union)")
        self._std_combo.currentIndexChanged.connect(self._on_std_changed)
        std_lay.addWidget(self._std_combo, 1)
        layout.addWidget(std_box)

        # ── Smart database ────────────────────────────────────────────────────
        smart_box = QGroupBox("Smart databas")
        smart_lay = QVBoxLayout(smart_box)
        self._smart_chk = QCheckBox(
            "Aktivera smart databas — skannar automatiskt inläst P&ID och "
            "identifierar taggar (pump, ventil, instrument…)")
        self._smart_chk.setChecked(
            self.db.tag_db_setting('smart_enabled', '0') == '1')
        self._smart_chk.toggled.connect(self._on_smart_toggled)
        smart_lay.addWidget(self._smart_chk)
        smart_note = QLabel(
            "Identifierade taggar markeras med ljusgul bakgrund på P&ID:n.\n"
            "Definierade orsaker (HAZOP) markeras med ljusgrön bakgrund.")
        smart_note.setStyleSheet("color:#555; font-size:10px;")
        smart_lay.addWidget(smart_note)
        layout.addWidget(smart_box)

        # ── Tag table ─────────────────────────────────────────────────────────
        self._tbl = QTableWidget(0, 5)
        self._tbl.setHorizontalHeaderLabels(
            ['Taggkod', 'Svensk benämning', 'Engelsk benämning', 'Kategori', 'Standard'])
        h = self._tbl.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self._tbl.setColumnWidth(0, 80)
        self._tbl.setColumnWidth(3, 110)
        self._tbl.setColumnWidth(4, 100)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:3px;}")
        layout.addWidget(self._tbl)

        self._status = QLabel("")
        self._status.setStyleSheet("color:#555; font-size:10px;")
        layout.addWidget(self._status)

        self._refresh()

    def _refresh(self):
        # Update standard combo
        self._std_combo.blockSignals(True)
        cur = self.db.tag_db_setting('active_standard', '')
        self._std_combo.clear()
        self._std_combo.addItem("Alla standarder (union)", '')
        for std in self.db.tag_database_standards():
            self._std_combo.addItem(std, std)
        idx = self._std_combo.findData(cur)
        if idx >= 0:
            self._std_combo.setCurrentIndex(idx)
        self._std_combo.blockSignals(False)

        # Update table
        entries = self.db.tag_database_entries()
        self._tbl.setRowCount(0)
        for e in entries:
            r = self._tbl.rowCount(); self._tbl.insertRow(r)
            for col, val in enumerate([
                    e['tag_code'], e['name_sv'], e['name_en'],
                    e['category'], e['standard']]):
                self._tbl.setItem(r, col, QTableWidgetItem(val or ''))
            self._tbl.setRowHeight(r, 22)

        n = len(entries)
        stds = self.db.tag_database_standards()
        self._status.setText(
            f"{n} taggkoder  |  {len(stds)} standarder: {', '.join(stds)}")

    def _import_excel(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Välj Excel-databas", "", "Excel (*.xlsx *.xls)")
        if not path:
            return
        n, err = self.db.import_tag_database_excel(path)
        if err:
            QMessageBox.critical(self, "Importfel", err)
        else:
            QMessageBox.information(self, "Importerat",
                f"{n} taggkoder importerade från\n{path}")
            self._excel_lbl.setText(path)
            self._refresh()
            self.settings_changed.emit()

    def _on_std_changed(self):
        std = self._std_combo.currentData() or ''
        self.db.set_tag_db_setting('active_standard', std)
        self.settings_changed.emit()

    def _on_smart_toggled(self, checked):
        self.db.set_tag_db_setting('smart_enabled', '1' if checked else '0')
        self.settings_changed.emit()


_PALETTE_MIME = 'application/x-hazop-palette-color'


class DraggableColorSwatch(QLabel):
    """Draggable color swatch in the palette — drag onto a matrix cell."""

    def __init__(self, name: str, color: str, fg_color: str = None, parent=None):
        super().__init__(name, parent)
        self._name     = name
        self._color    = color
        self._fg_color = fg_color  # None = auto-calculated from luminance
        self.setFixedSize(76, 28)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self._refresh()

    def _refresh(self):
        r, g, b = int(self._color[1:3], 16), int(self._color[3:5], 16), int(self._color[5:7], 16)
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        auto_txt = '#000' if lum > 160 else '#fff'
        txt = self._fg_color if self._fg_color else auto_txt
        self.setStyleSheet(
            f"background:{self._color}; color:{txt}; font-weight:bold; font-size:10px;"
            f"border:1px solid #555; border-radius:4px;")
        self.setText(self._name)

    def set_swatch(self, name: str, color: str, fg_color: str = None):
        self._name = name; self._color = color; self._fg_color = fg_color
        self._refresh()

    def name(self):     return self._name
    def color(self):    return self._color
    def fg_color(self): return self._fg_color

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_PALETTE_MIME,
                         json.dumps({'color': self._color, 'name': self._name,
                                     'fg_color': self._fg_color or '#ffffff'}).encode())
            drag.setMimeData(mime)
            drag.setPixmap(self.grab())
            drag.setHotSpot(event.position().toPoint())
            drag.exec(Qt.DropAction.CopyAction)
        else:
            super().mousePressEvent(event)


class MatrixCellButton(QPushButton):
    """Risk matrix cell — collapsed-border grid (no double-lines between cells)."""

    def __init__(self, row, col, color, label, fg_color='#ffffff',
                 is_top_row=False, is_left_col=False, parent=None):
        super().__init__(label, parent)
        self.row = row
        self.col = col
        self._color    = color
        self._fg_color = fg_color
        self._label    = label
        self._is_top   = is_top_row
        self._is_left  = is_left_col
        self.setFixedSize(80, 40)
        self.setAcceptDrops(True)
        self._apply_style()

    def _apply_style(self):
        top  = "border-top:1px solid #444;"  if self._is_top  else ""
        left = "border-left:1px solid #444;" if self._is_left else ""
        self.setStyleSheet(
            f"QPushButton{{"
            f"background:{self._color}; color:{self._fg_color}; font-weight:bold;"
            f"border-bottom:1px solid #444; border-right:1px solid #444;"
            f"{top}{left}"
            f"border-radius:0px; margin:0px; padding:0px;}}"
            f"QPushButton:hover{{border:2px solid #000; margin:-1px;}}")
        self.setText(self._label)

    def set_cell(self, color, label=None, fg_color=None):
        self._color = color
        if label is not None:
            self._label = label
        if fg_color is not None:
            self._fg_color = fg_color
        self._apply_style()

    def color(self):    return self._color
    def label(self):    return self._label
    def fg_color(self): return self._fg_color

    # ── Drag-and-drop ─────────────────────────────────────────────────────────
    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(_PALETTE_MIME):
            self.setStyleSheet(
                f"background:{self._color}; color:white; font-weight:bold;"
                f"border:3px dashed #000;")
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._apply_style()

    def dropEvent(self, event):
        if event.mimeData().hasFormat(_PALETTE_MIME):
            data = json.loads(
                event.mimeData().data(_PALETTE_MIME).data().decode())
            self.set_cell(data['color'], data['name'], data.get('fg_color', '#ffffff'))
            event.acceptProposedAction()
        else:
            event.ignore()


class StandardCausesSettingsPanel(QWidget):
    """3-level editable hierarchy: Avvikelse → Objekt → Orsaker (+Orsaksbeskrivningar)."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._loading = False

        layout = QHBoxLayout(self)

        # ── Col 1: Avvikelse ──────────────────────────────────────────────────
        c1 = QVBoxLayout()
        c1.addWidget(QLabel("<b>Avvikelse</b>"))
        self._dev_list = QListWidget()
        self._dev_list.currentRowChanged.connect(self._on_dev_sel)
        c1.addWidget(self._dev_list)
        c1b = QHBoxLayout()
        for icon, slot in (('+', self._add_dev), ('−', self._del_dev),
                           ('↑', lambda: self._move_dev(-1)), ('↓', lambda: self._move_dev(1))):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c1b.addWidget(b)
        c1b.addStretch(); c1.addLayout(c1b)

        # ── Col 2: Objekt ─────────────────────────────────────────────────────
        c2 = QVBoxLayout()
        self._obj_lbl = QLabel("<b>Objekt</b>")
        c2.addWidget(self._obj_lbl)
        self._obj_list = QListWidget()
        self._obj_list.currentRowChanged.connect(self._on_obj_sel)
        c2.addWidget(self._obj_list)
        # Show all objects; objects with causes are highlighted
        self._show_all_obj_chk = QCheckBox("Visa alla objekt")
        self._show_all_obj_chk.setChecked(True)
        self._show_all_obj_chk.stateChanged.connect(lambda _: self._load_objects())
        c2.addWidget(self._show_all_obj_chk)

        # ── Col 3: Orsaker ────────────────────────────────────────────────────
        c3 = QVBoxLayout()
        self._cause_lbl = QLabel("<b>Orsaker</b>")
        c3.addWidget(self._cause_lbl)
        self._cause_list = QListWidget()
        self._cause_list.currentRowChanged.connect(self._on_cause_sel)
        c3.addWidget(self._cause_list)

        # Frequency field for selected cause
        freq_row = QHBoxLayout()
        freq_lbl = QLabel("Frekvens (/år):")
        freq_lbl.setStyleSheet("font-size:10px; color:#555;")
        freq_row.addWidget(freq_lbl)
        self._freq_edit = QLineEdit()
        self._freq_edit.setPlaceholderText("t.ex. 0.01")
        self._freq_edit.setMaximumWidth(90)
        self._freq_edit.setToolTip("Basfrekvens för vald orsak (händelser/år). Lämna tomt om okänd.")
        self._freq_edit.editingFinished.connect(self._save_freq)
        freq_row.addWidget(self._freq_edit)
        self._freq_level_lbl = QLabel("")
        self._freq_level_lbl.setStyleSheet("color:#8D9299; font-size:10px;")
        freq_row.addWidget(self._freq_level_lbl)
        freq_row.addStretch()
        c3.addLayout(freq_row)

        c3b = QHBoxLayout()
        for icon, slot in (('+', self._add_cause), ('−', self._del_cause),
                           ('↑', lambda: self._move_cause(-1)), ('↓', lambda: self._move_cause(1))):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c3b.addWidget(b)
        c3b.addStretch(); c3.addLayout(c3b)
        btn_sync = QPushButton("Synka frekvenser →")
        btn_sync.setToolTip("Uppdaterar frekvensen på alla orsaker kopplade till standardorsaker.")
        btn_sync.clicked.connect(self._sync_freqs)
        c3.addWidget(btn_sync)
        # Feature 16: export/import buttons
        io_row = QHBoxLayout()
        btn_exp = QPushButton("↑ Exportera")
        btn_exp.setToolTip("Exportera hela standardbiblioteket till JSON")
        btn_exp.clicked.connect(self._export_library)
        btn_imp = QPushButton("↓ Importera")
        btn_imp.setToolTip("Importera standardbibliotek från JSON (lägger till, skriver ej över)")
        btn_imp.clicked.connect(self._import_library)
        io_row.addWidget(btn_exp); io_row.addWidget(btn_imp)
        c3.addLayout(io_row)

        # ── Col 4: Orsaksbeskrivningar ────────────────────────────────────────
        c4 = QVBoxLayout()
        self._desc_lbl = QLabel("<b>Orsaksbeskrivningar</b>")
        c4.addWidget(self._desc_lbl)
        self._desc_list = QListWidget()
        c4.addWidget(self._desc_list)
        c4b = QHBoxLayout()
        for icon, slot in (('+', self._add_desc), ('−', self._del_desc),
                           ('↑', lambda: self._move_desc(-1)), ('↓', lambda: self._move_desc(1))):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c4b.addWidget(b)
        c4b.addStretch(); c4.addLayout(c4b)

        layout.addLayout(c1, 1)
        layout.addLayout(c2, 1)
        layout.addLayout(c3, 1)
        layout.addLayout(c4, 1)
        self._load_deviations()

    # ── Load helpers ──────────────────────────────────────────────────────────
    def _load_deviations(self):
        self._loading = True
        cur = self._dev_list.currentRow()
        self._dev_list.clear()
        for d in self.db.standard_deviations():
            item = QListWidgetItem(d['description'])
            item.setData(Qt.ItemDataRole.UserRole, d['id'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._dev_list.addItem(item)
        self._loading = False
        self._dev_list.setCurrentRow(max(0, min(cur, self._dev_list.count()-1)))

    def _current_dev_id(self):
        item = self._dev_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load_objects(self, dev_id=None):
        if dev_id is None:
            dev_id = self._current_dev_id()
        self._loading = True
        cur = self._obj_list.currentRow()
        self._obj_list.clear()
        if dev_id is None:
            self._loading = False; return
        show_all = self._show_all_obj_chk.isChecked()
        if show_all:
            rows = self.db.all_objects_with_cause_counts(dev_id)
        else:
            rows = self.db.objects_for_deviation(dev_id)
        for r in rows:
            label = r['name']
            n = r.get('n_causes', 0)
            if n:
                label = f"{r['name']}  ({n})"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, r['id'])
            item.setData(Qt.ItemDataRole.UserRole + 1, r['name'])
            if n:
                item.setForeground(QColor('#17191C'))
            self._obj_list.addItem(item)
        self._loading = False
        self._obj_list.setCurrentRow(max(0, min(cur, self._obj_list.count()-1)))

    def _current_obj_id(self):
        item = self._obj_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load_causes(self, dev_id=None, obj_id=None):
        if dev_id is None: dev_id = self._current_dev_id()
        if obj_id is None: obj_id = self._current_obj_id()
        self._loading = True
        cur = self._cause_list.currentRow()
        self._cause_list.clear()
        if dev_id is None or obj_id is None:
            self._loading = False; return
        dev_item = self._dev_list.currentItem()
        obj_item = self._obj_list.currentItem()
        dev_name = dev_item.text() if dev_item else ''
        obj_name = obj_item.data(Qt.ItemDataRole.UserRole + 1) if obj_item else ''
        self._cause_lbl.setText(f"<b>Orsaker</b> — {dev_name} / {obj_name}")
        for c in self.db.standard_causes_for_object(dev_id, obj_id):
            freq = c.get('frequency')
            label = c['description']
            if freq is not None:
                label += f"  [{freq:g}/år]"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole,     c['id'])
            item.setData(Qt.ItemDataRole.UserRole + 1, c['description'])
            item.setData(Qt.ItemDataRole.UserRole + 2, freq)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._cause_list.addItem(item)
        self._loading = False
        self._cause_list.setCurrentRow(max(0, min(cur, self._cause_list.count()-1)))
        # Clear freq field if no cause selected after reload
        if self._cause_list.currentRow() < 0:
            self._freq_edit.clear()
            self._freq_level_lbl.setText('')

    def _current_cause_id(self):
        item = self._cause_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _load_descriptions(self, cause_id):
        self._desc_list.blockSignals(True)
        try:
            self._desc_list.clear()
            if cause_id is None:
                return
            for d in self.db.cause_descriptions(cause_id):
                item = QListWidgetItem(d['description'])
                item.setData(Qt.ItemDataRole.UserRole, d['id'])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                self._desc_list.addItem(item)
        finally:
            self._desc_list.blockSignals(False)
            try:
                self._desc_list.itemChanged.disconnect(self._on_desc_changed)
            except TypeError:
                pass   # wasn't connected yet (first call)
            self._desc_list.itemChanged.connect(self._on_desc_changed)

    # ── Slot chains ───────────────────────────────────────────────────────────
    def _on_dev_sel(self, row):
        if self._loading: return
        dev_item = self._dev_list.item(row)
        if dev_item:
            self._obj_lbl.setText(f"<b>Objekt</b> — {dev_item.text()}")
        self._load_objects()

    def _on_obj_sel(self, row):
        if self._loading: return
        self._load_causes()

    def _on_cause_sel(self, row):
        if self._loading: return
        item = self._cause_list.item(row)
        cid = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._load_descriptions(cid)
        # Populate freq field
        freq = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
        self._freq_edit.blockSignals(True)
        self._freq_edit.setText(f"{freq:g}" if freq is not None else '')
        self._freq_edit.blockSignals(False)
        self._freq_level_lbl.setText(
            freq_axis_label(freq_to_f_level(freq)) if freq is not None else '')

    def _save_freq(self):
        """Save the edited frequency for the currently selected standard cause."""
        item = self._cause_list.currentItem()
        if not item: return
        cid = item.data(Qt.ItemDataRole.UserRole)
        if cid is None: return
        text = self._freq_edit.text().strip()
        if not text:
            freq = None
            self._freq_level_lbl.setText('')
        else:
            try:
                freq = float(text)
                self._freq_level_lbl.setText(freq_axis_label(freq_to_f_level(freq)))
            except ValueError:
                self._freq_level_lbl.setText('Ogiltigt')
                return
        self.db.update_standard_cause(cid, frequency=freq)
        # Update display label in list
        item.setData(Qt.ItemDataRole.UserRole + 2, freq)
        desc = item.data(Qt.ItemDataRole.UserRole + 1) or item.text()
        if freq is not None:
            item.setText(f"{desc}  [{freq:g}/år]")
        else:
            item.setText(desc)

    # ── Deviation CRUD ────────────────────────────────────────────────────────
    def _add_dev(self):
        new_id = self.db.add_standard_deviation('Ny avvikelse')
        item = QListWidgetItem('Ny avvikelse')
        item.setData(Qt.ItemDataRole.UserRole, new_id)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._dev_list.addItem(item)
        self._dev_list.editItem(item)

    def _del_dev(self):
        item = self._dev_list.currentItem()
        if not item: return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ and QMessageBox.question(self, 'Ta bort', 'Ta bort avvikelse och alla dess orsaker?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) == QMessageBox.StandardButton.Yes:
            self.db.delete_standard_deviation(id_)
            self._load_deviations()

    def _move_dev(self, d):
        row = self._dev_list.currentRow()
        new_row = row + d
        if not (0 <= new_row < self._dev_list.count()): return
        a = self._dev_list.takeItem(row)
        self._dev_list.insertItem(new_row, a)
        self._dev_list.setCurrentRow(new_row)
        ids = [self._dev_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._dev_list.count())]
        self.db.reorder_standard_deviations(ids)

    # ── Cause CRUD ────────────────────────────────────────────────────────────
    def _add_cause(self):
        dev_id = self._current_dev_id()
        obj_id = self._current_obj_id()
        if dev_id is None or obj_id is None: return
        new_id = self.db.add_standard_cause_with_object(dev_id, obj_id, 'Ny orsak')
        item = QListWidgetItem('Ny orsak')
        item.setData(Qt.ItemDataRole.UserRole, new_id)
        item.setData(Qt.ItemDataRole.UserRole + 1, 'Ny orsak')
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._cause_list.addItem(item)
        self._cause_list.editItem(item)
        self._load_objects()   # refresh object cause counts

    def _del_cause(self):
        item = self._cause_list.currentItem()
        if not item: return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_:
            self.db.delete_standard_cause(id_)
            row = self._cause_list.row(item)
            self._cause_list.takeItem(row)
            self._load_objects()

    def _move_cause(self, d):
        row = self._cause_list.currentRow()
        new_row = row + d
        if not (0 <= new_row < self._cause_list.count()): return
        a = self._cause_list.takeItem(row)
        self._cause_list.insertItem(new_row, a)
        self._cause_list.setCurrentRow(new_row)
        ids = [self._cause_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._cause_list.count())]
        self.db.reorder_standard_causes(ids)

    def _on_cause_changed(self, item):
        if self._loading: return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_:
            self.db.update_standard_cause(id_, description=item.text().strip())

    # ── Description CRUD ──────────────────────────────────────────────────────
    def _add_desc(self):
        cid = self._current_cause_id()
        if cid is None: return
        new_id = self.db.add_cause_description(cid, 'Ny beskrivning')
        item = QListWidgetItem('Ny beskrivning')
        item.setData(Qt.ItemDataRole.UserRole, new_id)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._desc_list.addItem(item)
        self._desc_list.editItem(item)

    def _del_desc(self):
        item = self._desc_list.currentItem()
        if not item: return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_: self.db.delete_cause_description(id_)
        self._desc_list.takeItem(self._desc_list.row(item))

    def _move_desc(self, d):
        row = self._desc_list.currentRow()
        new_row = row + d
        if not (0 <= new_row < self._desc_list.count()): return
        a = self._desc_list.takeItem(row)
        self._desc_list.insertItem(new_row, a)
        self._desc_list.setCurrentRow(new_row)
        ids = [self._desc_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._desc_list.count())]
        self.db.reorder_cause_descriptions(ids)

    def _on_desc_changed(self, item):
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_: self.db.update_cause_description(id_, item.text().strip())

    # ── Sync ──────────────────────────────────────────────────────────────────
    def _sync_freqs(self):
        ret = QMessageBox.question(self, 'Synka frekvenser',
            'Uppdatera frekvenser på alla kopplade orsaker?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret == QMessageBox.StandardButton.Yes:
            n = self.db.update_cause_freqs_from_standard()
            QMessageBox.information(self, 'Klart', f'{n} orsak(er) uppdaterades.')

    # ── Feature 16: Export/import standard library ────────────────────────────
    def _export_library(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'Exportera standardbibliotek', '', 'JSON (*.json)')
        if not path: return
        data = {'deviations': [], 'objects': []}
        for dev in self.db.standard_deviations():
            dd = {'description': dev['description'], 'causes': []}
            for c in self.db.standard_causes(dev['id']):
                cd = dict(c)
                cd['descriptions'] = [d['description']
                                       for d in self.db.cause_descriptions(cd['id'])]
                dd['causes'].append({k: cd.get(k) for k in
                    ['description', 'comp_type', 'frequency', 'object_id', 'descriptions']})
            data['deviations'].append(dd)
        for obj in self.db.standard_objects():
            data['objects'].append(obj['name'])
        import json as _json
        with open(path, 'w', encoding='utf-8') as f:
            f.write(_json.dumps(data, ensure_ascii=False, indent=2))
        QMessageBox.information(self, 'Exporterat', f'Sparat till:\n{path}')

    def _import_library(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Importera standardbibliotek', '', 'JSON (*.json)')
        if not path: return
        import json as _json
        try:
            with open(path, encoding='utf-8') as f:
                data = _json.loads(f.read())
        except Exception as e:
            QMessageBox.critical(self, 'Fel', str(e)); return
        added_devs = added_causes = added_objs = 0
        for obj_name in data.get('objects', []):
            if not self.db.conn.execute(
                    "SELECT id FROM standard_objects WHERE name=?", (obj_name,)).fetchone():
                self.db.add_standard_object(obj_name); added_objs += 1
        for dev_d in data.get('deviations', []):
            dev_row = self.db.conn.execute(
                "SELECT id FROM standard_deviations WHERE description=?",
                (dev_d['description'],)).fetchone()
            if not dev_row:
                dev_id = self.db.add_standard_deviation(dev_d['description'])
                added_devs += 1
            else:
                dev_id = dev_row[0]
            for c in dev_d.get('causes', []):
                obj_id = c.get('object_id')
                if not self.db.conn.execute(
                        "SELECT id FROM standard_causes WHERE deviation_id=? AND description=?",
                        (dev_id, c['description'])).fetchone():
                    self.db.add_standard_cause_with_object(dev_id, obj_id or 0, c['description'])
                    added_causes += 1
        self.db.conn.commit()
        self._load_deviations()
        QMessageBox.information(self, 'Importerat',
            f'Lagt till: {added_devs} avvikelser, {added_causes} orsaker, {added_objs} objekt.')


class StandardObjectsSettingsPanel(QWidget):
    """Editable list of standard object types (from orsaker.txt)."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "<b>Standardobjekt</b> — dessa objekttyper är tillgängliga i orsaksformulären "
            "och kan kopplas till orsaksbeskrivningar."))

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        layout.addWidget(self._list)

        btns = QHBoxLayout()
        btn_add = QPushButton("+ Lägg till")
        btn_add.clicked.connect(self._add)
        btn_del = QPushButton("− Ta bort")
        btn_del.clicked.connect(self._delete)
        btn_up  = QPushButton("↑")
        btn_up.setFixedWidth(28)
        btn_up.clicked.connect(lambda: self._move(-1))
        btn_dn  = QPushButton("↓")
        btn_dn.setFixedWidth(28)
        btn_dn.clicked.connect(lambda: self._move(1))
        btn_reset = QPushButton("Återställ standard")
        btn_reset.setToolTip("Lägger tillbaka alla standardobjekt från ursprungslistan (lägger inte till dubbletter)")
        btn_reset.clicked.connect(self._reset)
        for b in (btn_add, btn_del, btn_up, btn_dn, btn_reset):
            btns.addWidget(b)
        btns.addStretch()
        layout.addLayout(btns)

        self._loading = False
        self._load()

    def _load(self):
        self._loading = True
        cur = self._list.currentRow()
        self._list.clear()
        for obj in self.db.standard_objects():
            item = QListWidgetItem(obj['name'])
            item.setData(Qt.ItemDataRole.UserRole, obj['id'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._list.addItem(item)
        try:
            self._list.itemChanged.disconnect(self._on_changed)
        except TypeError:
            pass   # wasn't connected yet (first call)
        self._list.itemChanged.connect(self._on_changed)
        self._loading = False
        if cur >= 0:
            self._list.setCurrentRow(min(cur, self._list.count() - 1))

    def _on_changed(self, item):
        if self._loading:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is not None:
            self.db.update_standard_object(id_, item.text().strip())

    def _add(self):
        new_id = self.db.add_standard_object('Nytt objekt')
        item = QListWidgetItem('Nytt objekt')
        item.setData(Qt.ItemDataRole.UserRole, new_id)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._list.addItem(item)
        self._list.editItem(item)

    def _delete(self):
        item = self._list.currentItem()
        if not item:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is not None:
            self.db.delete_standard_object(id_)
        self._list.takeItem(self._list.row(item))

    def _move(self, direction):
        row = self._list.currentRow()
        new_row = row + direction
        if not (0 <= new_row < self._list.count()):
            return
        a = self._list.takeItem(row)
        self._list.insertItem(new_row, a)
        self._list.setCurrentRow(new_row)
        ids = [self._list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._list.count())]
        self.db.reorder_standard_objects(ids)

    def _reset(self):
        for name in _STD_OBJECTS:
            exists = self.db.conn.execute(
                "SELECT id FROM standard_objects WHERE name=?", (name,)).fetchone()
            if not exists:
                self.db.add_standard_object(name)
        self._load()


class SeverityDefinitionsPanel(QWidget):
    """Grid panel: consequence categories (rows) × severity levels (cols).
    Each cell holds a short description of what that level means for that category."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._edits = {}   # (severity_level, category_id) → QLineEdit

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        lbl = QLabel(
            "Definiera vad varje konsekvensgrad (C1–CN) innebär per kategori. "
            "Värdena visas som referens vid bedömning av konsekvenser.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#555; font-size:11px;")
        outer.addWidget(lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._scroll_widget = QWidget()
        self._grid_layout = QGridLayout(self._scroll_widget)
        self._grid_layout.setSpacing(4)
        scroll.setWidget(self._scroll_widget)
        outer.addWidget(scroll)

        self.refresh()

    def refresh(self):
        """Rebuild grid from current matrix config + categories."""
        # Save pending edits before rebuild
        self._flush_pending()

        cfg  = get_matrix()
        y    = cfg.get('y_labels', [])
        n    = cfg.get('rows', 5)
        cats = self.db.consequence_categories()
        defs = self.db.get_severity_definitions()

        # Clear grid
        while self._grid_layout.count():
            item = self._grid_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._edits.clear()

        if not cats:
            self._grid_layout.addWidget(
                QLabel("Lägg till konsekvenskategorier i fliken Kategorier först."), 0, 0)
            return

        # Header row: severity level labels
        self._grid_layout.addWidget(QLabel(""), 0, 0)  # top-left corner
        for col_idx in range(n):
            label = y[col_idx] if col_idx < len(y) else f"C{col_idx+1}"
            hdr = QLabel(f"<b>C{col_idx+1}</b><br><small>{label}</small>")
            hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hdr.setStyleSheet(
                "background:#F5F5F3; color:#17191C; border-radius:3px; padding:4px 6px;")
            hdr.setMinimumWidth(120)
            self._grid_layout.addWidget(hdr, 0, col_idx + 1)

        # Data rows: one per category
        for row_idx, cat in enumerate(cats):
            cat_id = cat['id']
            cat_name = cat['name']

            cat_lbl = QLabel(f"<b>{cat_name}</b>")
            cat_lbl.setStyleSheet("padding:2px 4px;")
            cat_lbl.setMinimumWidth(90)
            self._grid_layout.addWidget(cat_lbl, row_idx + 1, 0)

            for col_idx in range(n):
                sev_lvl = col_idx + 1  # 1-based
                desc = defs.get(sev_lvl, {}).get(cat_id, '')
                edit = QLineEdit(desc)
                edit.setPlaceholderText(f"C{sev_lvl}, {cat_name}…")
                edit.setMinimumWidth(120)
                # Save on focus-out
                _lvl, _cid = sev_lvl, cat_id
                edit.editingFinished.connect(
                    lambda _e=edit, _l=_lvl, _c=_cid:
                        self.db.set_severity_definition(_l, _c, _e.text().strip()))
                self._edits[(_lvl, _cid)] = edit
                self._grid_layout.addWidget(edit, row_idx + 1, col_idx + 1)

        self._grid_layout.setColumnStretch(0, 0)
        for c in range(1, n + 1):
            self._grid_layout.setColumnStretch(c, 1)

    def _flush_pending(self):
        """Save all currently displayed edits to DB."""
        for (lvl, cid), edit in self._edits.items():
            self.db.set_severity_definition(lvl, cid, edit.text().strip())


class TagMemoryPanel(QWidget):
    """View and edit the smart object recognition memory for this project."""

    # Column indices
    _C_USE  = 0   # "Använd" checkbox
    _C_PFX  = 1   # prefix
    _C_TYPE = 2   # comp_type (editable)
    _C_CNT  = 3   # usage count
    _C_UPD  = 4   # updated

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        tf = QFont(); tf.setBold(True); tf.setPointSize(10)

        # ── Master toggle ──────────────────────────────────────────────────────
        master_row = QHBoxLayout()
        self._master_cb = QCheckBox("Använd smart igenkänning")
        self._master_cb.setToolTip(
            "När ikryssad föreslår programmet objekttyp automatiskt baserat på "
            "tagg-prefixet (t.ex. GPA → Pump).")
        self._master_cb.setChecked(
            db.get_config('smart_recognition_enabled', '1') == '1')
        self._master_cb.toggled.connect(self._on_master_toggled)
        f = QFont(); f.setBold(True)
        self._master_cb.setFont(f)
        master_row.addWidget(self._master_cb)
        master_row.addStretch()
        btn_clear = QPushButton("🗑 Rensa allt")
        btn_clear.setToolTip("Ta bort alla lärda mappningar för detta projekt")
        btn_clear.clicked.connect(self._clear_all)
        master_row.addWidget(btn_clear)
        lay.addLayout(master_row)

        info = QLabel(
            "Ikryssad rad = aktiv förval för det prefixet. "
            "Att kryssa i en rad inaktiverar automatiskt övriga för samma prefix. "
            "Lägg till mappningar manuellt nedan — de gäller omedelbart.")
        info.setWordWrap(True)
        info.setStyleSheet("color:#555; font-size:10px;")
        lay.addWidget(info)

        # ── Manual add row ─────────────────────────────────────────────────────
        add_row = QHBoxLayout()
        add_row.setSpacing(6)
        self._pfx_edit  = QLineEdit()
        self._pfx_edit.setPlaceholderText("Prefix  t.ex. QMA")
        self._pfx_edit.setMaximumWidth(100)
        self._pfx_edit.setFixedHeight(CONFIG['H_CTRL_STD'])
        add_row.addWidget(self._pfx_edit)
        self._type_combo = QComboBox()
        self._type_combo.setFixedHeight(CONFIG['H_CTRL_STD'])
        from pid_viewer import KNOWN_PREFIXES as _KP
        obj_names = sorted({v[1] for v in _KP.values() if v[1]})
        # Also add standard object names
        for nm in ['Manuell ventil','On-off ventil','Reglerventil','Backventil',
                   'Säkerhetsventil / sprängbleck','Pump','Kompressor / fläkt',
                   'Värmeväxlare / kylare / värmare','Tank / kärl / kolonn',
                   'Rörledning / slang','Instrument','Övrigt']:
            if nm not in obj_names:
                obj_names.append(nm)
        obj_names.sort()
        self._type_combo.addItems(obj_names)
        add_row.addWidget(self._type_combo, 1)
        btn_add = QPushButton("+ Lägg till")
        btn_add.setFixedHeight(CONFIG['H_CTRL_STD'])
        btn_add.clicked.connect(self._add_manual_entry)
        add_row.addWidget(btn_add)
        lay.addLayout(add_row)

        # ── Tag memory table ───────────────────────────────────────────────────
        self._tbl = QTableWidget(0, 5)
        self._tbl.setHorizontalHeaderLabels(
            ["Använd", "Prefix", "Komponenttyp", "Antal", "Senast"])
        h = self._tbl.horizontalHeader()
        h.setSectionResizeMode(self._C_USE,  QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(self._C_PFX,  QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(self._C_TYPE, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(self._C_CNT,  QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(self._C_UPD,  QHeaderView.ResizeMode.ResizeToContents)
        self._tbl.setColumnWidth(self._C_PFX, 90)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self._tbl.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self._tbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_del = QPushButton("🗑 Ta bort markerade")
        btn_del.clicked.connect(self._delete_selected)
        btn_row.addWidget(btn_del)
        lay.addLayout(btn_row)

        # ── Fingerprints ───────────────────────────────────────────────────────
        fp_hdr = QHBoxLayout()
        fp_title = QLabel("Visuella fingeravtryck (symbolmönster)")
        fp_title.setFont(tf)
        fp_hdr.addWidget(fp_title)
        fp_hdr.addStretch()
        btn_fp_clear = QPushButton("🗑 Rensa fingeravtryck")
        btn_fp_clear.clicked.connect(self._clear_fingerprints)
        fp_hdr.addWidget(btn_fp_clear)
        lay.addLayout(fp_hdr)

        self._fp_tbl = QTableWidget(0, 3)
        self._fp_tbl.setHorizontalHeaderLabels(
            ["Komponenttyp", "Exempeltagg", "Antal matchningar"])
        fp_h = self._fp_tbl.horizontalHeader()
        fp_h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        fp_h.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        fp_h.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._fp_tbl.setColumnWidth(1, 120)
        self._fp_tbl.verticalHeader().setVisible(False)
        self._fp_tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._fp_tbl.setAlternatingRowColors(True)
        self._fp_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._fp_tbl.setMaximumHeight(140)
        lay.addWidget(self._fp_tbl)

        self.refresh()

    def _on_master_toggled(self, checked: bool):
        self.db.set_config('smart_recognition_enabled', '1' if checked else '0')

    def _add_manual_entry(self):
        """Manually add/override a prefix→type mapping and make it the active choice."""
        pfx = self._pfx_edit.text().strip().upper()
        comp = self._type_combo.currentText().strip()
        if not pfx or not comp:
            return
        # Deactivate all existing entries for this prefix
        try:
            self.db.conn.execute(
                "UPDATE study_tag_memory SET active=0 WHERE UPPER(tag)=UPPER(?)",
                (pfx,))
            # Insert/update the chosen (prefix, type) with high count + active=1
            existing = self.db.conn.execute(
                "SELECT usage_count FROM study_tag_memory "
                "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
                (pfx, comp)).fetchone()
            now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')
            if existing:
                self.db.conn.execute(
                    "UPDATE study_tag_memory SET active=1, usage_count=usage_count+1, updated=? "
                    "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
                    (now, pfx, comp))
            else:
                self.db.conn.execute(
                    "INSERT INTO study_tag_memory (tag,comp_type,active,updated) VALUES (?,?,1,?)",
                    (pfx, comp, now))
            self.db.commit()
        except Exception as e:
            QMessageBox.warning(self, "Fel", str(e))
            return
        self._pfx_edit.clear()
        self.refresh()

    def refresh(self):
        self._tbl.blockSignals(True)
        self._tbl.setRowCount(0)
        try:
            rows = self.db.conn.execute(
                "SELECT tag, comp_type, usage_count, updated, active "
                "FROM study_tag_memory ORDER BY tag, usage_count DESC").fetchall()
        except Exception:
            try:
                rows = self.db.conn.execute(
                    "SELECT tag, comp_type, usage_count, updated, 1 as active "
                    "FROM study_tag_memory ORDER BY tag, usage_count DESC").fetchall()
            except Exception:
                rows = []

        # Find the winning (highest-count active) type per prefix for highlighting
        best: dict = {}  # prefix → max active usage_count
        for row in rows:
            d = dict(row)
            if d['active']:
                best[d['tag']] = max(best.get(d['tag'], 0), d['usage_count'])

        for row in rows:
            r = self._tbl.rowCount()
            self._tbl.insertRow(r)
            d = dict(row)
            is_winner = d['active'] and d['usage_count'] == best.get(d['tag'], -1)

            # Col 0 — "Använd" checkbox
            use_item = QTableWidgetItem()
            use_item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            use_item.setCheckState(
                Qt.CheckState.Checked if d['active'] else Qt.CheckState.Unchecked)
            # Store both prefix AND comp_type for the DB update
            use_item.setData(Qt.ItemDataRole.UserRole, (d['tag'], d['comp_type']))
            self._tbl.setItem(r, self._C_USE, use_item)

            # Col 1 — prefix (bold if this is the winning row)
            pfx_item = QTableWidgetItem(d['tag'])
            pfx_item.setData(Qt.ItemDataRole.UserRole, d['tag'])
            colour = QColor('#17191C') if d['active'] else QColor('#aaa')
            pfx_item.setForeground(QBrush(colour))
            if is_winner:
                f = pfx_item.font(); f.setBold(True); pfx_item.setFont(f)
            pfx_item.setFlags(pfx_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._tbl.setItem(r, self._C_PFX, pfx_item)

            # Col 2 — comp_type (not editable — type is defined by what you pick)
            ct = QTableWidgetItem(d['comp_type'])
            if not d['active']:
                ct.setForeground(QBrush(QColor('#aaa')))
            elif is_winner:
                f = ct.font(); f.setBold(True); ct.setFont(f)
                ct.setToolTip('Används som förval (flest val)')
            ct.setFlags(ct.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._tbl.setItem(r, self._C_TYPE, ct)

            # Col 3 — count
            uc = QTableWidgetItem(str(d['usage_count']))
            uc.setFlags(uc.flags() & ~Qt.ItemFlag.ItemIsEditable)
            uc.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if is_winner:
                f = uc.font(); f.setBold(True); uc.setFont(f)
            self._tbl.setItem(r, self._C_CNT, uc)

            # Col 4 — updated
            upd = QTableWidgetItem(d['updated'] or '')
            upd.setFlags(upd.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._tbl.setItem(r, self._C_UPD, upd)

        self._tbl.blockSignals(False)

        # Fingerprints
        self._fp_tbl.setRowCount(0)
        try:
            fp_rows = self.db.conn.execute(
                "SELECT comp_type, tag_example, usage_count "
                "FROM symbol_fingerprints ORDER BY usage_count DESC").fetchall()
        except Exception:
            fp_rows = []
        for row in fp_rows:
            r = self._fp_tbl.rowCount()
            self._fp_tbl.insertRow(r)
            d = dict(row)
            self._fp_tbl.setItem(r, 0, QTableWidgetItem(d['comp_type']))
            self._fp_tbl.setItem(r, 1, QTableWidgetItem(d['tag_example']))
            uc = QTableWidgetItem(str(d['usage_count']))
            uc.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._fp_tbl.setItem(r, 2, uc)

    def _on_item_changed(self, item):
        col = item.column()
        row = item.row()
        key_item = self._tbl.item(row, self._C_USE)
        if not key_item:
            return
        key_data = key_item.data(Qt.ItemDataRole.UserRole)  # (prefix, comp_type) tuple
        if not isinstance(key_data, tuple) or len(key_data) != 2:
            return
        prefix, comp_type = key_data

        if col == self._C_USE:
            active = item.checkState() == Qt.CheckState.Checked
            self._tbl.blockSignals(True)
            try:
                if active:
                    # Exclusive per prefix — deactivate all other types for this prefix
                    self.db.conn.execute(
                        "UPDATE study_tag_memory SET active=0 WHERE UPPER(tag)=UPPER(?)",
                        (prefix,))
                    self.db.conn.execute(
                        "UPDATE study_tag_memory SET active=1 "
                        "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
                        (prefix, comp_type))
                    self.db.commit()
                    # Reflect the change visually for all rows of this prefix
                    for r2 in range(self._tbl.rowCount()):
                        ki2 = self._tbl.item(r2, self._C_USE)
                        if ki2 and isinstance(ki2.data(Qt.ItemDataRole.UserRole), tuple):
                            pfx2, ct2 = ki2.data(Qt.ItemDataRole.UserRole)
                            if pfx2.upper() == prefix.upper():
                                is_this_row = (ct2.upper() == comp_type.upper())
                                ki2.setCheckState(
                                    Qt.CheckState.Checked if is_this_row
                                    else Qt.CheckState.Unchecked)
                                colour = QColor('#17191C') if is_this_row else QColor('#aaa')
                                for c in (self._C_PFX, self._C_TYPE, self._C_CNT):
                                    it2 = self._tbl.item(r2, c)
                                    if it2:
                                        it2.setForeground(QBrush(colour))
                else:
                    self.db.set_tag_memory_active(prefix, comp_type, False)
                    grey = QColor('#aaa')
                    for c in (self._C_PFX, self._C_TYPE, self._C_CNT):
                        it = self._tbl.item(row, c)
                        if it:
                            it.setForeground(QBrush(grey))
            except Exception:
                pass
            self._tbl.blockSignals(False)

    def _delete_selected(self):
        rows = sorted({i.row() for i in self._tbl.selectedItems()}, reverse=True)
        for r in rows:
            key_item = self._tbl.item(r, self._C_USE)
            if key_item:
                key_data = key_item.data(Qt.ItemDataRole.UserRole)
                if isinstance(key_data, tuple) and len(key_data) == 2:
                    prefix, comp_type = key_data
                    try:
                        self.db.conn.execute(
                            "DELETE FROM study_tag_memory "
                            "WHERE UPPER(tag)=UPPER(?) AND UPPER(comp_type)=UPPER(?)",
                            (prefix, comp_type))
                    except Exception:
                        pass
        self.db.commit()
        self.refresh()

    def _clear_all(self):
        if QMessageBox.question(
                self, "Rensa tagminne",
                "Ta bort alla lärda tagg-mappningar för detta projekt?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            try:
                self.db.conn.execute("DELETE FROM study_tag_memory")
                self.db.commit()
            except Exception:
                pass
            self.refresh()

    def _clear_fingerprints(self):
        if QMessageBox.question(
                self, "Rensa fingeravtryck",
                "Ta bort alla visuella fingeravtryck?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            try:
                self.db.conn.execute("DELETE FROM symbol_fingerprints")
                self.db.commit()
            except Exception:
                pass
            self.refresh()


class SettingsPanel(QWidget):
    matrix_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._cell_buttons   = []
        self._x_label_edits  = []   # QLineEdit per column
        self._y_label_edits  = []   # QLineEdit per row (high→low)
        self._palette_swatches = []
        self._sev_def_edits  = {}   # (cat_id, sev_level) → QLineEdit, embedded in matrix grid

        tabs = QTabWidget()
        self._tabs = tabs   # kept as an attribute for testability (tabText() lookups)
        main = QVBoxLayout(self)
        main.addWidget(tabs)

        # ── Tab: Riskmatris ───────────────────────────────────────────────────
        matrix_tab = QWidget()
        ml = QVBoxLayout(matrix_tab)
        ml.setSpacing(6)

        # Size row
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Konsekvens-fält:"))
        self._rows_spin = QSpinBox()
        self._rows_spin.setRange(2, 15)
        self._rows_spin.setValue(5)
        self._rows_spin.setToolTip("Antal nivåer på konsekvens-axeln (C1…Cn)")
        size_row.addWidget(self._rows_spin)

        size_row.addWidget(QLabel("  Frekvens-fält:"))
        self._cols_spin = QSpinBox()
        self._cols_spin.setRange(2, 15)
        self._cols_spin.setValue(7)
        self._cols_spin.setToolTip("Antal nivåer på frekvens-axeln (F-1…Fn)")
        size_row.addWidget(self._cols_spin)
        size_row.addStretch()
        ml.addLayout(size_row)

        # ── Colour palette ────────────────────────────────────────────────────
        pal_box = QGroupBox("Färgpalett — dra en färg och släpp på en cell")
        pal_lay = QHBoxLayout(pal_box)
        pal_lay.setSpacing(4)
        self._palette_container = pal_lay

        add_col_btn = QPushButton("+ Lägg till")
        add_col_btn.setFixedHeight(CONFIG['H_ROW_STD'])
        add_col_btn.clicked.connect(self._palette_add)
        pal_lay.addWidget(add_col_btn)

        edit_col_btn = QPushButton("✎ Redigera")
        edit_col_btn.setFixedHeight(CONFIG['H_ROW_STD'])
        edit_col_btn.clicked.connect(self._palette_edit)
        pal_lay.addWidget(edit_col_btn)

        del_col_btn = QPushButton("✕ Ta bort")
        del_col_btn.setFixedHeight(CONFIG['H_ROW_STD'])
        del_col_btn.clicked.connect(self._palette_delete)
        pal_lay.addWidget(del_col_btn)

        pal_lay.addStretch()
        ml.addWidget(pal_box)

        # ── Matrix grid ───────────────────────────────────────────────────────
        # Use a wrapper so matrix stays at natural size (top-left) while the
        # scroll area fills remaining space with the stretch below it.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        _wrap = QWidget()
        _wrap_lay = QVBoxLayout(_wrap)
        _wrap_lay.setContentsMargins(0, 0, 0, 0)
        _wrap_lay.setSpacing(0)

        self._matrix_container = QWidget()
        self._matrix_grid = QGridLayout(self._matrix_container)
        self._matrix_grid.setSpacing(0)
        self._matrix_grid.setContentsMargins(0, 0, 0, 0)

        _wrap_lay.addWidget(self._matrix_container,
                            alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        _wrap_lay.addStretch(1)
        scroll.setWidget(_wrap)
        ml.addWidget(scroll)

        # Axis orientation + direction controls
        ax_row = QHBoxLayout()
        ax_row.addWidget(QLabel("Axlar:"))
        self._axis_combo = QComboBox()
        self._axis_combo.addItem("Frekvens → X,  Konsekvens → Y  (standard)", 'frequency')
        self._axis_combo.addItem("Konsekvens → X,  Frekvens → Y", 'consequence')
        ax_row.addWidget(self._axis_combo, 1)
        ax_row.addWidget(QLabel("  Riktning:"))
        self._x_rev_chk = QCheckBox("Vänd X ←")
        self._x_rev_chk.setToolTip("Vänd X-axeln: hög värde till vänster")
        self._y_rev_chk = QCheckBox("Vänd Y ↓")
        self._y_rev_chk.setToolTip("Vänd Y-axeln: lägst upp, högst ner")
        ax_row.addWidget(self._x_rev_chk)
        ax_row.addWidget(self._y_rev_chk)
        ml.addLayout(ax_row)

        # Live update: rebuild grid immediately on any control change
        self._axis_combo.currentIndexChanged.connect(self._apply_size)
        self._x_rev_chk.toggled.connect(self._apply_size)
        self._y_rev_chk.toggled.connect(self._apply_size)
        self._rows_spin.valueChanged.connect(self._apply_size)
        self._cols_spin.valueChanged.connect(self._apply_size)

        # Frequency label presets
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Frekvens-mall:"))
        norsok_btn = QPushButton("NORSOK Z-013  (AAA – E)")
        norsok_btn.setToolTip(
            "Fyll frekvensaxeln med NORSOK Z-013-etiketter:\n"
            "AAA (< 10⁻⁵/år)  →  E (> 1/år)\n"
            "Gränsvärden sätts automatiskt.")
        norsok_btn.clicked.connect(lambda: self._apply_freq_preset(
            ['AAA', 'AA', 'A', 'B', 'C', 'D', 'E'],
            [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]))
        fscale_btn = QPushButton("F-skala  (F-1 – F5)")
        fscale_btn.setToolTip(
            "Fyll frekvensaxeln med internt F-skaleetiketter:\n"
            "F-1 (Otänkbar)  →  F5 (Frekvent > 1/år)\n"
            "Gränsvärden sätts automatiskt.")
        fscale_btn.clicked.connect(lambda: self._apply_freq_preset(
            ['F-1 – Otänkbar', 'F0 – Extremt sällan', 'F1 – Sällan',
             'F2 – Osannolik', 'F3 – Möjlig', 'F4 – Trolig', 'F5 – Frekvent'],
            [1e-5, 1e-4, 1e-3, 1e-2, 0.1, 1.0]))
        preset_row.addWidget(norsok_btn)
        preset_row.addWidget(fscale_btn)
        preset_row.addStretch()
        ml.addLayout(preset_row)

        save_matrix_btn = QPushButton("💾 Spara riskmatris")
        save_matrix_btn.setStyleSheet(
            "background:#17191C; color:#fff; font-weight:bold; padding:4px 12px;")
        save_matrix_btn.clicked.connect(self._save_matrix)
        ml.addWidget(save_matrix_btn)

        # ── Tab: Kategorier ───────────────────────────────────────────────────
        cat_tab = QWidget()
        cl = QVBoxLayout(cat_tab)
        cl.addWidget(QLabel("Konsekvensskategorier:"))
        self._cat_list = QListWidget()
        cl.addWidget(self._cat_list)
        cat_btns = QHBoxLayout()
        btn_add  = QPushButton("+ Lägg till")
        btn_ren  = QPushButton("Byt namn")
        btn_del  = QPushButton("Ta bort")
        btn_add.clicked.connect(self._cat_add)
        btn_ren.clicked.connect(self._cat_rename)
        btn_del.clicked.connect(self._cat_delete)
        for b in [btn_add, btn_ren, btn_del]: cat_btns.addWidget(b)
        cl.addLayout(cat_btns)
        cl.addStretch()

        # ── Merged tab: Riskmatris & Kategorier ─────────────────────────────
        # Design choice (2026-08-11, user request: "'riskmatris' och
        # 'kategorier' borde gå att slå ihop till en sida" / "Låt Claude
        # välja bästa GUI-lösningen"): a QSplitter, categories on the left
        # and the matrix on the right, rather than a nested tab-within-tab.
        # Reasoning: the matrix tab is inherently tall/wide (size controls +
        # colour palette + a scrollable grid + axis controls + frequency
        # presets + a save button), while the categories tab is just a short
        # list with three buttons — putting categories in their own nested
        # tab would hide them behind an extra click AND waste most of that
        # tab's vertical space. Categories also feed the matrix conceptually
        # (they're consequence-axis metadata), so keeping both visible
        # side-by-side, with the narrow categories panel user-resizable via
        # the splitter handle, reads as one coherent risk-classification
        # screen instead of two unrelated hidden pages.
        combined_tab = QWidget()
        combined_l = QHBoxLayout(combined_tab)
        combined_l.setContentsMargins(0, 0, 0, 0)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(cat_tab)
        splitter.addWidget(matrix_tab)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 760])
        combined_l.addWidget(splitter)
        tabs.addTab(combined_tab, "Riskmatris & Kategorier")

        # ── Tab: Projekt ──────────────────────────────────────────────────────
        proj_tab = QWidget()
        pl = QFormLayout(proj_tab)
        pl.setSpacing(10)
        pl.setContentsMargins(16, 16, 16, 16)

        self._proj_name = QLineEdit()
        self._proj_name.editingFinished.connect(
            lambda: self.db.set_config('project_name', self._proj_name.text()))
        pl.addRow("Projektnamn:", self._proj_name)

        self._proj_facility = QLineEdit()
        self._proj_facility.editingFinished.connect(
            lambda: self.db.set_config('project_facility', self._proj_facility.text()))
        pl.addRow("Anläggning:", self._proj_facility)

        self._proj_leader = QLineEdit()
        self._proj_leader.editingFinished.connect(
            lambda: self.db.set_config('project_hazop_leader', self._proj_leader.text()))
        pl.addRow("HAZOP-ledare:", self._proj_leader)

        # Datum: date RANGE (workshop start/end) instead of a single
        # free-text field (2026-08-11, user request: "Gör så att datum kan
        # väljas inom ett intervall"). Replaces the old single-value
        # 'project_date' config key with 'project_date_start' /
        # 'project_date_end' — see NOTES.md for the migration note and the
        # project-reset cleanup list update.
        date_row_w = QWidget()
        date_row_l = QHBoxLayout(date_row_w)
        date_row_l.setContentsMargins(0, 0, 0, 0)
        self._proj_date_start = QDateEdit()
        self._proj_date_start.setCalendarPopup(True)
        self._proj_date_start.setDisplayFormat("yyyy-MM-dd")
        self._proj_date_end = QDateEdit()
        self._proj_date_end.setCalendarPopup(True)
        self._proj_date_end.setDisplayFormat("yyyy-MM-dd")
        self._proj_date_start.dateChanged.connect(
            lambda d: self.db.set_config('project_date_start', d.toString('yyyy-MM-dd')))
        self._proj_date_end.dateChanged.connect(
            lambda d: self.db.set_config('project_date_end', d.toString('yyyy-MM-dd')))
        date_row_l.addWidget(self._proj_date_start)
        date_row_l.addWidget(QLabel("  –  "))
        date_row_l.addWidget(self._proj_date_end)
        date_row_l.addStretch()
        pl.addRow("Datum (från–till):", date_row_w)

        self._proj_rev = QLineEdit()
        self._proj_rev.editingFinished.connect(
            lambda: self.db.set_config('project_revision', self._proj_rev.text()))
        pl.addRow("Revision:", self._proj_rev)

        self._proj_participants = QPlainTextEdit()
        self._proj_participants.setPlaceholderText(
            "En deltagare per rad, t.ex.:\nAnna Andersson (Processägare)\nBengt Bengtsson (Drift)")
        self._proj_participants.setFixedHeight(90)
        # QPlainTextEdit has no editingFinished signal — save on focus-out
        # instead, same pattern already used for multi-line description
        # fields elsewhere in this file (e.g. NodeEditDialog.desc_edit).
        _orig_participants_foe = QPlainTextEdit.focusOutEvent
        def _participants_foe(e, _orig=_orig_participants_foe):
            self.db.set_config('project_participants', self._proj_participants.toPlainText())
            _orig(self._proj_participants, e)
        self._proj_participants.focusOutEvent = _participants_foe
        pl.addRow("Deltagare:", self._proj_participants)
        tabs.addTab(proj_tab, "Projekt")

        # ── Tab: P&ID-inställningar ───────────────────────────────────────────
        # Renamed from "P&ID" (2026-08-11, user request: "Fliken PID borde
        # kunna ändras till något mer generiskt för inställning" / "Byt namn
        # + lägg till OCR/sid-inställningar"). "P&ID-inställningar" was
        # chosen over a fully generic name like "Analys" or "Inställningar"
        # because this tab already lives inside a settings screen next to
        # "Tagdatabas" and "Identifierade objekt" (both P&ID-specific DATA
        # views) — a bare "Analys" would read as ambiguous next to those,
        # while "P&ID-inställningar" keeps the P&ID scope clear but no
        # longer implies (like the old "P&ID" name did) that tag-stripping
        # is the only setting that belongs here.
        pid_tab = QWidget()
        pid_l = QVBoxLayout(pid_tab)
        pid_l.setContentsMargins(16, 16, 16, 16)
        pid_l.setSpacing(12)

        tag_grp = QGroupBox("Tagg-identifiering")
        tag_gl = QVBoxLayout(tag_grp)
        tag_gl.setSpacing(6)

        self._strip_spaces_chk = QCheckBox(
            "Ta bort mellanslag i tagg-nummer  (t.ex. \"P 101\" → \"P101\")")
        self._strip_spaces_chk.setToolTip(
            "När ett tagg-nummer identifieras via klick eller gummiband på P&ID\n"
            "tas alla mellanslag bort automatiskt innan det fylls i tag-fältet.")
        self._strip_spaces_chk.toggled.connect(
            lambda on: self.db.set_config('tag_strip_spaces', '1' if on else '0'))
        tag_gl.addWidget(self._strip_spaces_chk)

        pid_l.addWidget(tag_grp)

        # ── OCR-standardval ───────────────────────────────────────────────
        # Lets the user skip the per-scan "Använd OCR?" Yes/No prompt shown
        # by "🔍 Skanna P&ID" (EquipmentPanel._scan, hazop.py) and "📋
        # Analysera P&ID" (PIDPanel._analyze_pid, pid_viewer.py) by picking
        # a fixed default engine ahead of time. Wired into both scan entry
        # points via pid_viewer.resolve_ocr_scan_choice() — this is NOT a
        # dead setting, it actually changes scan behaviour.
        ocr_grp = QGroupBox("OCR-standardval")
        ocr_gl = QVBoxLayout(ocr_grp)
        ocr_gl.setSpacing(6)
        ocr_lbl = QLabel(
            "Motor att använda automatiskt vid P&ID-skanning\n"
            "(🔍 Skanna P&ID / 📋 Analysera P&ID), utan att fråga varje gång:")
        ocr_gl.addWidget(ocr_lbl)
        self._ocr_default_combo = QComboBox()
        self._ocr_default_combo.addItem("Fråga varje gång (standard)", 'ask')
        self._ocr_default_combo.addItem("Automatiskt — bästa tillgängliga motor", 'auto')
        _ocr_st = ocr_status()
        if _ocr_st.get('rapidocr'):
            self._ocr_default_combo.addItem("RapidOCR", 'rapidocr')
        if _ocr_st.get('tesseract'):
            self._ocr_default_combo.addItem("Tesseract", 'tesseract')
        if _ocr_st.get('easyocr'):
            self._ocr_default_combo.addItem("EasyOCR", 'easyocr')
        self._ocr_default_combo.setToolTip(
            "Styr om/vilken OCR-motor som används automatiskt vid P&ID-skanning —\n"
            "hoppar då över Ja/Nej-frågan om OCR för den körningen.\n"
            "\"Fråga varje gång\" behåller nuvarande beteende.")
        self._ocr_default_combo.currentIndexChanged.connect(
            lambda: self.db.set_config(
                'ocr_default_engine', self._ocr_default_combo.currentData()))
        ocr_gl.addWidget(self._ocr_default_combo)
        pid_l.addWidget(ocr_grp)

        # ── Sid-orientering ───────────────────────────────────────────────
        # Investigated first (per process convention) whether an
        # auto-detection system already exists: it does not — the app
        # always just follows the PDF's own /Rotate page attribute
        # (fitz_page.rotation_matrix, see PIDPanel._highlight_tags in
        # pid_viewer.py), there is no heuristic "guess the orientation"
        # layer to conflict with. This setting is therefore stored as a
        # forward-looking override/hint only; it is NOT yet read by the
        # rendering/scanning pipeline (that would mean threading an
        # override through PDF rendering, OCR preprocessing, and the
        # multi-process scan workers — out of scope for this change; see
        # NOTES.md "Kända begränsningar" for this known limitation).
        orient_grp = QGroupBox("Sid-orientering")
        orient_gl = QVBoxLayout(orient_grp)
        orient_gl.setSpacing(6)
        orient_lbl = QLabel(
            "Förvalt antagande om sidans orientering vid rendering/analys\n"
            "av P&ID-sidor. OBS: sparas som inställning men styr ännu inte\n"
            "den faktiska renderingen/analysen (appen använder idag alltid\n"
            "PDF-filens egen rotationsflagga automatiskt) — känd begränsning,\n"
            "se NOTES.md.")
        orient_lbl.setWordWrap(True)
        orient_gl.addWidget(orient_lbl)
        self._page_orientation_combo = QComboBox()
        self._page_orientation_combo.addItem(
            "Använd PDF:ens egen rotation (standard)", 'auto')
        self._page_orientation_combo.addItem("Tvinga liggande", 'landscape')
        self._page_orientation_combo.addItem("Tvinga stående", 'portrait')
        self._page_orientation_combo.currentIndexChanged.connect(
            lambda: self.db.set_config(
                'pid_page_orientation_hint', self._page_orientation_combo.currentData()))
        orient_gl.addWidget(self._page_orientation_combo)
        pid_l.addWidget(orient_grp)

        pid_l.addStretch()
        tabs.addTab(pid_tab, "P&ID-inställningar")

        # ── Tab: Tagdatabas ───────────────────────────────────────────────────
        self._tag_db_panel = TagDatabasePanel(self.db)
        self._tag_db_panel.settings_changed.connect(self.matrix_changed.emit)
        tabs.addTab(self._tag_db_panel, "Tagdatabas")

        # ── Tab: Identifierade objekt ─────────────────────────────────────────
        self.analysis_panel = PIDAnalysisPanel(self.db)
        tabs.addTab(self.analysis_panel, "Identifierade objekt")

        # ── Tab: Standardavvikelser & Orsaker ─────────────────────────────────
        self._std_causes_panel = StandardCausesSettingsPanel(self.db)
        tabs.addTab(self._std_causes_panel, "Standardorsaker")

        # ── Tab: Standardobjekt ───────────────────────────────────────────────
        self._std_objects_panel = StandardObjectsSettingsPanel(self.db)
        tabs.addTab(self._std_objects_panel, "Standardobjekt")

        # ── Tab: Smart igenkänning ────────────────────────────────────────────
        self._tag_memory_panel = TagMemoryPanel(self.db)
        tabs.addTab(self._tag_memory_panel, "🧠 Smart igenkänning")
        tabs.currentChanged.connect(
            lambda i: self._tag_memory_panel.refresh()
            if tabs.widget(i) is self._tag_memory_panel else None)

        self._load_all()

    def _load_all(self):
        self._load_matrix_ui()
        self._load_palette_ui()
        self._load_categories()
        self._proj_name.setText(self.db.get_config('project_name', ''))
        self._proj_facility.setText(self.db.get_config('project_facility', ''))
        self._proj_leader.setText(self.db.get_config('project_hazop_leader', ''))
        self._proj_rev.setText(self.db.get_config('project_revision', ''))
        self._proj_participants.setPlainText(self.db.get_config('project_participants', ''))

        today = QDate.currentDate()
        start_str = self.db.get_config('project_date_start', '')
        end_str   = self.db.get_config('project_date_end', '')
        start_d = QDate.fromString(start_str, 'yyyy-MM-dd') if start_str else QDate()
        end_d   = QDate.fromString(end_str, 'yyyy-MM-dd') if end_str else QDate()
        self._proj_date_start.setDate(start_d if start_d.isValid() else today)
        self._proj_date_end.setDate(end_d if end_d.isValid() else today)

        self._strip_spaces_chk.setChecked(
            self.db.get_config('tag_strip_spaces', '1') == '1')

        idx = self._ocr_default_combo.findData(self.db.get_config('ocr_default_engine', 'ask'))
        if idx >= 0:
            self._ocr_default_combo.setCurrentIndex(idx)
        idx = self._page_orientation_combo.findData(
            self.db.get_config('pid_page_orientation_hint', 'auto'))
        if idx >= 0:
            self._page_orientation_combo.setCurrentIndex(idx)

    # ── Palette ───────────────────────────────────────────────────────────────

    def _load_palette_ui(self):
        # Remove existing swatches (keep the 3 buttons at end)
        for sw in self._palette_swatches:
            self._palette_container.removeWidget(sw)
            sw.deleteLater()
        self._palette_swatches = []
        palette = self.db.get_color_palette()
        for entry in palette:
            sw = DraggableColorSwatch(entry['name'], entry['color'], entry.get('fg_color'))
            # Insert before the "Lägg till / Redigera / Ta bort" buttons
            insert_pos = self._palette_container.count() - 4
            self._palette_container.insertWidget(max(0, insert_pos), sw)
            self._palette_swatches.append(sw)

    def _palette_add(self):
        name, ok = QInputDialog.getText(self, "Ny palettefärg", "Namn (t.ex. Kritisk):")
        if not ok or not name.strip():
            return
        color = QColorDialog.getColor(QColor('#e74c3c'), self, "Välj bakgrundsfärg")
        if not color.isValid():
            return
        # Auto-calculate fg and let user override
        r, g, b = color.red(), color.green(), color.blue()
        auto_fg = '#000000' if (0.299*r + 0.587*g + 0.114*b) > 160 else '#ffffff'
        fg_color_obj = QColorDialog.getColor(QColor(auto_fg), self, "Välj textfärg (auto-föreslagen)")
        fg = fg_color_obj.name() if fg_color_obj.isValid() else auto_fg
        palette = self.db.get_color_palette()
        palette.append({'name': name.strip(), 'color': color.name(), 'fg_color': fg})
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    def _palette_edit(self):
        palette = self.db.get_color_palette()
        if not palette:
            return
        names = [e['name'] for e in palette]
        chosen, ok = QInputDialog.getItem(self, "Redigera", "Välj färg:", names, 0, False)
        if not ok:
            return
        idx = names.index(chosen)
        new_name, ok2 = QInputDialog.getText(self, "Nytt namn", "Namn:", text=chosen)
        if not ok2:
            return
        new_color = QColorDialog.getColor(QColor(palette[idx]['color']), self, "Välj färg")
        if not new_color.isValid():
            return
        # Ask for text color too
        old_fg = palette[idx].get('fg_color', '#ffffff')
        fg_color_obj = QColorDialog.getColor(QColor(old_fg), self, "Välj textfärg")
        new_fg = fg_color_obj.name() if fg_color_obj.isValid() else old_fg
        palette[idx] = {'name': new_name.strip() or chosen, 'color': new_color.name(), 'fg_color': new_fg}
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    def _palette_delete(self):
        palette = self.db.get_color_palette()
        if not palette:
            return
        names = [e['name'] for e in palette]
        chosen, ok = QInputDialog.getItem(self, "Ta bort", "Välj färg att ta bort:", names, 0, False)
        if not ok:
            return
        palette = [e for e in palette if e['name'] != chosen]
        self.db.set_color_palette(palette)
        self._load_palette_ui()

    # ── Matrix ────────────────────────────────────────────────────────────────

    def _load_matrix_ui(self):
        cfg = self.db.get_risk_matrix() or DEFAULT_MATRIX
        self._last_built_cfg = None   # reset before blocking so _apply_size sees None
        # Block all signals that would trigger _apply_size while we populate controls
        _senders = (self._rows_spin, self._cols_spin, self._axis_combo,
                    self._x_rev_chk, self._y_rev_chk)
        for w in _senders:
            w.blockSignals(True)
        self._rows_spin.setValue(cfg.get('rows', 5))
        self._cols_spin.setValue(cfg.get('cols', 7))
        x_axis = cfg.get('x_axis', 'frequency')
        idx = self._axis_combo.findData(x_axis)
        if idx >= 0:
            self._axis_combo.setCurrentIndex(idx)
        self._x_rev_chk.setChecked(bool(cfg.get('x_reversed', False)))
        self._y_rev_chk.setChecked(bool(cfg.get('y_reversed', False)))
        for w in _senders:
            w.blockSignals(False)
        self._build_matrix_grid(cfg)

    def _apply_size(self):
        """Rebuild the matrix grid. Handles axis swap without losing data."""
        n_cons    = self._rows_spin.value()
        n_freq    = self._cols_spin.value()
        old       = self.db.get_risk_matrix() or DEFAULT_MATRIX
        new_xaxis = self._axis_combo.currentData() or 'frequency'
        x_rev     = self._x_rev_chk.isChecked()
        y_rev     = self._y_rev_chk.isChecked()

        # ── Recover semantic labels ───────────────────────────────────────────
        # Start from last-built config (source of truth for semantic order).
        # Only fall back to DB when the grid has never been built.
        disp          = getattr(self, '_last_built_cfg', None) or old
        disp_freq_on_x = disp.get('x_axis', 'frequency') == 'frequency'
        disp_x_rev    = disp.get('x_reversed', False)
        disp_y_rev    = disp.get('y_reversed', False)

        freq_lbls = list(disp.get('x_labels', old.get('x_labels', FREQ_LABELS[:n_freq])))
        cons_lbls = list(disp.get('y_labels', old.get('y_labels', SEV_LABELS[:n_cons])))

        # Apply any manual text edits from display widgets by mapping each
        # widget directly to its data index (no reversal ambiguity).
        if self._x_label_edits:
            nc = len(self._x_label_edits)
            for c, e in enumerate(self._x_label_edits):
                data_c = (nc - 1 - c) if disp_x_rev else c
                txt = e.text().strip()
                if not txt:
                    continue
                if disp_freq_on_x:
                    if data_c < len(freq_lbls):
                        freq_lbls[data_c] = txt
                else:
                    if data_c < len(cons_lbls):
                        cons_lbls[data_c] = txt

        if self._y_label_edits:
            nr = len(self._y_label_edits)
            for r, e in enumerate(self._y_label_edits):
                data_r = r if disp_y_rev else (nr - 1 - r)
                txt = e.text().strip()
                if not txt:
                    continue
                if disp_freq_on_x:
                    if data_r < len(cons_lbls):
                        cons_lbls[data_r] = txt
                else:
                    if data_r < len(freq_lbls):
                        freq_lbls[data_r] = txt

        # Pad/trim to new dimensions
        while len(freq_lbls) < n_freq:
            freq_lbls.append(f'F{len(freq_lbls)-1}')
        while len(cons_lbls) < n_cons:
            cons_lbls.append(f'C{len(cons_lbls)+1}')
        freq_lbls = freq_lbls[:n_freq]
        cons_lbls = cons_lbls[:n_cons]

        # ── Cell data: current buttons override DB values ─────────────────────
        colors    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        lbl2d     = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        fg_colors = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        # 1. Fill from DB
        old_c  = old.get('cell_colors', [])
        old_l  = old.get('cell_labels', [])
        old_fg = old.get('cell_fg_colors', [])
        for ci in range(n_cons):
            for fi in range(n_freq):
                try:    colors[ci][fi]    = old_c[ci][fi]  or '#27ae60'
                except: colors[ci][fi]    = '#27ae60'
                try:    lbl2d[ci][fi]     = old_l[ci][fi]  or 'Låg'
                except: lbl2d[ci][fi]     = 'Låg'
                try:    fg_colors[ci][fi] = old_fg[ci][fi] or '#ffffff'
                except: fg_colors[ci][fi] = '#ffffff'
        # 2. Override with any user edits in the current buttons
        for _dr, row_btns in self._cell_buttons:
            for btn in row_btns:
                ci, fi = btn.row, btn.col
                if ci < n_cons and fi < n_freq:
                    if btn.color():    colors[ci][fi]    = btn.color()
                    if btn.label():    lbl2d[ci][fi]     = btn.label()
                    if btn.fg_color(): fg_colors[ci][fi] = btn.fg_color()

        new_cfg = {
            'rows': n_cons, 'cols': n_freq,
            'x_axis':         new_xaxis,
            'x_reversed':     x_rev,
            'y_reversed':     y_rev,
            'x_labels':       freq_lbls,   # ALWAYS stores frequency labels
            'y_labels':       cons_lbls,   # ALWAYS stores consequence labels
            'cell_colors':    colors,
            'cell_labels':    lbl2d,
            'cell_fg_colors': fg_colors,
            'freq_boundaries': old.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES),
        }
        self._last_built_cfg = new_cfg
        self._build_matrix_grid(new_cfg)

    def _build_matrix_grid(self, cfg):
        """Build the matrix grid respecting axis orientation and intervals."""
        self._last_built_cfg = cfg   # track for _apply_size label recovery
        while self._matrix_grid.count():
            item = self._matrix_grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cell_buttons       = []
        self._x_label_edits      = []
        self._y_label_edits      = []
        self._freq_boundary_edits = []
        self._sev_def_edits       = {}

        # Data always stored as [consequence_idx][frequency_idx]
        n_cons = cfg.get('rows', 5)    # consequence levels
        n_freq = cfg.get('cols', 7)    # frequency levels
        freq_labels = cfg.get('x_labels', [f'F{c-1}' for c in range(n_freq)])
        cons_labels = cfg.get('y_labels', [f'C{r+1}' for r in range(n_cons)])
        colors          = cfg.get('cell_colors',    [['#27ae60'] * n_freq] * n_cons)
        cell_labels     = cfg.get('cell_labels',    [['Låg']     * n_freq] * n_cons)
        cell_fg_colors  = cfg.get('cell_fg_colors', [['#ffffff'] * n_freq] * n_cons)
        boundaries  = list(cfg.get('freq_boundaries', DEFAULT_FREQ_BOUNDARIES))

        x_axis    = cfg.get('x_axis', 'frequency')
        freq_on_x = (x_axis == 'frequency')
        x_rev     = cfg.get('x_reversed', False)   # True = high value on left/top of X
        y_rev     = cfg.get('y_reversed', False)   # True = low value at top of Y

        # Determine display dimensions
        if freq_on_x:
            n_dcols, n_drows = n_freq, n_cons   # cols=freq, rows=cons
            col_lbls, row_lbls = freq_labels, cons_labels
            corner_txt = "C \\ F"
            col_tip = "Frekvensetikett (X-axel)\nExempel: F3 – Möjlig | 10-100 år"
            row_tip = "Konsekvensnivå (Y-axel)\nExempel: C4 – Allvarlig"
        else:
            n_dcols, n_drows = n_cons, n_freq   # cols=cons, rows=freq
            col_lbls, row_lbls = cons_labels, freq_labels
            corner_txt = "F \\ C"
            col_tip = "Konsekvensnivå (X-axel)\nExempel: C4 – Allvarlig"
            row_tip = "Frekvensetikett (Y-axel)\nExempel: F3 – Möjlig | 10-100 år"

        _hdr_style = ("font-size:8px; font-weight:bold;"
                      "border:1px solid #aaa; border-radius:0px;"
                      "background:#eef2f7; padding:0 3px;")

        # Corner
        corner = QLabel(corner_txt)
        corner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        corner.setStyleSheet("font-size:9px; color:#555;")
        self._matrix_grid.addWidget(corner, 0, 0)

        # Column headers — apply x_rev: if reversed, col 0 shows the highest value
        for c in range(n_dcols):
            data_c = (n_dcols - 1 - c) if x_rev else c
            txt = col_lbls[data_c] if data_c < len(col_lbls) else str(data_c)
            e = QLineEdit(txt)
            e.setFixedSize(80, 28)
            e.setAlignment(Qt.AlignmentFlag.AlignCenter)
            e.setStyleSheet(_hdr_style)
            e.setToolTip(col_tip + "\nEtiketten uppdateras automatiskt när du ändrar gränsvärdet.")
            self._matrix_grid.addWidget(e, 0, c + 1)
            self._x_label_edits.append(e)

        # Rows — apply y_rev: if NOT reversed, highest value is at top (default)
        for r in range(n_drows):
            if y_rev:
                disp_r = r              # low at top (r=0 = lowest value)
            else:
                disp_r = n_drows - 1 - r  # high at top (default)

            # Row header
            txt = row_lbls[disp_r] if disp_r < len(row_lbls) else str(disp_r)
            ey = QLineEdit(txt)
            ey.setFixedSize(90, 40)
            ey.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            ey.setStyleSheet(_hdr_style)
            ey.setToolTip(row_tip)
            self._matrix_grid.addWidget(ey, r + 1, 0)
            self._y_label_edits.append(ey)   # index 0 = top row

            row_btns = []
            for c in range(n_dcols):
                # Resolve display column to data column (accounting for x_rev)
                data_c = (n_dcols - 1 - c) if x_rev else c
                # Map display → data (cons_idx, freq_idx)
                if freq_on_x:
                    cons_idx = disp_r
                    freq_idx = data_c
                else:
                    freq_idx = disp_r
                    cons_idx = data_c

                try: cc = colors[cons_idx][freq_idx]
                except (IndexError, KeyError): cc = '#27ae60'
                try: cl = cell_labels[cons_idx][freq_idx]
                except (IndexError, KeyError): cl = 'Låg'
                try: cf = cell_fg_colors[cons_idx][freq_idx]
                except (IndexError, KeyError): cf = '#ffffff'

                btn = MatrixCellButton(cons_idx, freq_idx, cc, cl, cf,
                                       is_top_row=(r == 0),
                                       is_left_col=(c == 0))
                btn.clicked.connect(partial(self._edit_cell, btn))
                self._matrix_grid.addWidget(btn, r + 1, c + 1)
                row_btns.append(btn)
            self._cell_buttons.append((disp_r, row_btns))

        # ── Interval / boundary row below cells ───────────────────────────────
        # Only shown when frequency is on X-axis (boundaries are per-frequency-column)
        if freq_on_x:
            while len(boundaries) < n_freq - 1:
                boundaries.append(10 ** (len(boundaries) - 5))

            bnd_lbl = QLabel("Gräns\n(/år)")
            bnd_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            bnd_lbl.setStyleSheet("font-size:8px; color:#555; padding:0 3px;")
            self._matrix_grid.addWidget(bnd_lbl, n_drows + 1, 0)

            # When x_rev, the highest-freq column is at c=0 (leftmost) — ">allt" moves there
            # and the boundary values follow the reversed column order.
            highest_col = 0 if x_rev else n_dcols - 1
            for c in range(n_dcols):
                if c == highest_col:
                    lbl = QLabel(">allt")
                    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    lbl.setStyleSheet("font-size:8px; color:#aaa;")
                    self._matrix_grid.addWidget(lbl, n_drows + 1, c + 1)
                else:
                    # Map display col → data freq index to pick the correct boundary
                    bval_idx = (n_dcols - 1 - c) if x_rev else c
                    bval  = boundaries[bval_idx] if bval_idx < len(boundaries) else ''
                    btext = f"{float(bval):.4g}" if bval != '' else ''
                    e = QLineEdit(btext)
                    e.setPlaceholderText("—")
                    e.setFixedSize(80, 22)
                    e.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    e.setStyleSheet(
                        "font-size:9px; border:1px solid #aaa; background:#fffde7;"
                        "border-radius:0px;")
                    e.setToolTip(
                        f"Övre gräns (händelser/år) för kolumn {c}.\n"
                        f"Frekvenser under detta värde tillhör denna kolumn.\n"
                        f"Exempel: 0.1 = en gång per 10 år")
                    self._matrix_grid.addWidget(e, n_drows + 1, c + 1)
                    self._freq_boundary_edits.append(e)
                    # Connect boundary edit → auto-update adjacent axis labels
                    e.editingFinished.connect(
                        lambda _e=e, _c=c: self._sync_freq_label_from_boundary(_e, _c))
        else:
            # When frequency on Y: add interval boundary column on the right
            bnd_lbl = QLabel("Gräns\n(/år)")
            bnd_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            bnd_lbl.setStyleSheet("font-size:8px; color:#555;")
            self._matrix_grid.addWidget(bnd_lbl, 0, n_dcols + 1)

            while len(boundaries) < n_freq - 1:
                boundaries.append(10 ** (len(boundaries) - 5))

            # Last row always gets ">allt" (the extreme bucket with no further boundary).
            # bval_idx depends on y_rev: y_rev=False → high-at-top, reversed boundary order.
            for r in range(n_drows):
                if r == n_drows - 1:
                    lbl = QLabel(">allt")
                    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    lbl.setStyleSheet("font-size:8px; color:#aaa;")
                    self._matrix_grid.addWidget(lbl, r + 1, n_dcols + 1)
                else:
                    bval_idx = r if y_rev else (n_drows - 2 - r)
                    bval  = boundaries[bval_idx] if bval_idx < len(boundaries) else ''
                    btext = f"{float(bval):.4g}" if bval != '' else ''
                    e = QLineEdit(btext)
                    e.setPlaceholderText("—")
                    e.setFixedSize(70, 40)
                    e.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    e.setStyleSheet(
                        "font-size:9px; border:1px solid #aaa; background:#fffde7;"
                        "border-radius:0px;")
                    self._matrix_grid.addWidget(e, r + 1, n_dcols + 1)
                    self._freq_boundary_edits.append(e)

        # ── Consequence category definitions embedded in matrix ────────────────
        cats = self.db.consequence_categories()
        defs = self.db.get_severity_definitions()  # {sev_level: {cat_id: description}}

        _def_style = ("font-size:9px; border:1px solid #ccc; border-radius:0;"
                      "background:#f8f8ff; padding:1px 3px;")
        _cat_hdr_style = ("font-size:9px; font-weight:bold; background:#e8edf5;"
                          "border:1px solid #bbb; padding:2px 6px;")

        if not freq_on_x:
            # Consequence on X (columns) → category rows go BELOW the matrix
            # n_drows = n_freq; no boundary row exists (boundary is a column)
            base_row = n_drows + 1

            # Thin separator spanning all columns
            sep = QLabel("── Konsekvensdefinitioner ──")
            sep.setStyleSheet("font-size:8px; color:#888; padding:2px 4px;")
            sep.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            self._matrix_grid.addWidget(sep, base_row, 0, 1, n_dcols + 1)

            for cat_i, cat in enumerate(cats):
                cat_id  = cat['id']
                cat_row = base_row + 1 + cat_i

                cat_lbl = QLabel(cat['name'])
                cat_lbl.setStyleSheet(_cat_hdr_style)
                cat_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                cat_lbl.setMinimumHeight(CONFIG['H_ROW_STD'])
                self._matrix_grid.addWidget(cat_lbl, cat_row, 0)

                for c in range(n_dcols):      # n_dcols = n_cons
                    data_c    = (n_dcols - 1 - c) if x_rev else c
                    sev_level = data_c + 1
                    text = defs.get(sev_level, {}).get(cat_id, '')
                    e = QLineEdit(text)
                    e.setMinimumHeight(CONFIG['H_ROW_STD'])
                    e.setStyleSheet(_def_style)
                    e.setPlaceholderText("—")
                    e.editingFinished.connect(
                        lambda cid=cat_id, sl=sev_level, _e=e:
                        self.db.set_severity_definition(sl, cid, _e.text().strip()))
                    self._matrix_grid.addWidget(e, cat_row, c + 1)
                    self._sev_def_edits[(cat_id, sev_level)] = e
        else:
            # Consequence on Y (rows) → category columns go to the RIGHT
            # n_dcols = n_freq; no boundary column exists (boundary is a row)
            base_col = n_dcols + 1

            for cat_i, cat in enumerate(cats):
                cat_id  = cat['id']
                cat_col = base_col + cat_i

                cat_hdr = QLabel(cat['name'])
                cat_hdr.setStyleSheet(_cat_hdr_style)
                cat_hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cat_hdr.setMinimumHeight(CONFIG['H_ROW_STD'])
                cat_hdr.setMinimumWidth(130)
                self._matrix_grid.addWidget(cat_hdr, 0, cat_col)

                for r in range(n_drows):      # n_drows = n_cons
                    disp_r    = (n_drows - 1 - r) if not y_rev else r
                    sev_level = disp_r + 1
                    text = defs.get(sev_level, {}).get(cat_id, '')
                    e = QLineEdit(text)
                    e.setMinimumWidth(130)
                    e.setStyleSheet(_def_style)
                    e.setPlaceholderText("—")
                    e.editingFinished.connect(
                        lambda cid=cat_id, sl=sev_level, _e=e:
                        self.db.set_severity_definition(sl, cid, _e.text().strip()))
                    self._matrix_grid.addWidget(e, r + 1, cat_col)
                    self._sev_def_edits[(cat_id, sev_level)] = e

    def _sync_freq_label_from_boundary(self, boundary_edit, col_idx: int):
        """Auto-update the frequency axis label(s) adjacent to the changed boundary."""
        try:
            val = float(boundary_edit.text().strip())
        except ValueError:
            return
        if val <= 0:
            return

        def _fmt(v):
            if v >= 1:       return f"{v:.3g}/år"
            if v >= 0.001:   return f"{v:.3g}/år"
            return f"{v:.2e}/år"

        # Collect all boundary values to compute ranges
        bvals = []
        for e in self._freq_boundary_edits:
            try:
                bvals.append(float(e.text()))
            except ValueError:
                bvals.append(None)

        def _label_for_col(c):
            """Return an auto-generated interval label for display column c."""
            left  = bvals[c-1] if c > 0 and c-1 < len(bvals) else None
            right = bvals[c]   if c < len(bvals) else None
            if left is None and right is not None:
                return f"< {_fmt(right)}"
            if left is not None and right is None:
                return f"≥ {_fmt(left)}"
            if left is not None and right is not None:
                return f"{_fmt(left)} – {_fmt(right)}"
            return ""

        # Update the two adjacent column labels (col_idx and col_idx+1)
        for affected_c in (col_idx, col_idx + 1):
            if 0 <= affected_c < len(self._x_label_edits):
                new_lbl = _label_for_col(affected_c)
                if new_lbl:
                    self._x_label_edits[affected_c].setText(new_lbl)

    def _edit_cell(self, btn):
        """Click a cell → choose background color, label, and text color."""
        color = QColorDialog.getColor(QColor(btn.color()), self, "Välj bakgrundsfärg för cell")
        if not color.isValid():
            return
        label, ok = QInputDialog.getText(
            self, "Celltext", "Risknivå-etikett (t.ex. Låg, Medium, Hög, Kritisk):",
            text=btn.label())
        if not ok:
            return
        # Auto-suggest fg based on luminance; let user override
        r, g, b = color.red(), color.green(), color.blue()
        auto_fg = '#000000' if (0.299*r + 0.587*g + 0.114*b) > 160 else '#ffffff'
        current_fg = btn.fg_color() if btn.fg_color() else auto_fg
        fg_obj = QColorDialog.getColor(QColor(current_fg), self, "Välj textfärg")
        fg = fg_obj.name() if fg_obj.isValid() else current_fg
        btn.set_cell(color.name(), label.strip() or btn.label(), fg)

    def _save_matrix(self):
        n_cons = self._rows_spin.value()   # consequence levels (rows in data)
        n_freq = self._cols_spin.value()   # frequency levels  (cols in data)
        x_axis = self._axis_combo.currentData() or 'frequency'
        freq_on_x = (x_axis == 'frequency')

        # Cell buttons store (cons_idx, freq_idx) regardless of display orientation
        colors    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        labels    = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        fg_colors = [['' for _ in range(n_freq)] for _ in range(n_cons)]
        for _disp_r, row_btns in self._cell_buttons:
            for btn in row_btns:
                cons_i, freq_i = btn.row, btn.col   # (cons_idx, freq_idx)
                if cons_i < n_cons and freq_i < n_freq:
                    colors[cons_i][freq_i]    = btn.color()
                    labels[cons_i][freq_i]    = btn.label()
                    fg_colors[cons_i][freq_i] = btn.fg_color()

        # Axis labels: _x_label_edits are the column headers (whatever axis),
        # _y_label_edits are the row headers (reversed, highest at top)
        raw_col = [e.text().strip() for e in self._x_label_edits]
        raw_row = list(reversed([e.text().strip() for e in self._y_label_edits]))  # low→high

        if freq_on_x:
            # X=freq columns, Y=cons rows
            x_labels = raw_col or [f'F{i-1}' for i in range(n_freq)]
            y_labels = raw_row or [f'C{i+1}' for i in range(n_cons)]
        else:
            # X=cons columns, Y=freq rows
            y_labels = raw_col or [f'C{i+1}' for i in range(n_cons)]
            x_labels = raw_row or [f'F{i-1}' for i in range(n_freq)]

        # Pad/trim to correct lengths
        while len(x_labels) < n_freq: x_labels.append(f'F{len(x_labels)-1}')
        while len(y_labels) < n_cons: y_labels.append(f'C{len(y_labels)+1}')
        x_labels = x_labels[:n_freq]
        y_labels = y_labels[:n_cons]

        cfg = {
            'rows': n_cons, 'cols': n_freq,
            'x_axis':      x_axis,
            'x_reversed':  self._x_rev_chk.isChecked(),
            'y_reversed':  self._y_rev_chk.isChecked(),
            'x_labels':    x_labels,
            'y_labels':    y_labels,
            'cell_colors':    colors,
            'cell_labels':    labels,
            'cell_fg_colors': fg_colors,
        }
        # Read frequency boundaries from editable row/column (display order)
        freq_boundaries = []
        for e in getattr(self, '_freq_boundary_edits', []):
            try:
                v = float(e.text().strip())
                if v > 0:
                    freq_boundaries.append(v)
            except ValueError:
                pass
        if not freq_boundaries:
            freq_boundaries = list(DEFAULT_FREQ_BOUNDARIES)
        # Boundary edits were laid out in display order; convert back to data order
        # (lowest freq level first) by reversing when the display was reversed:
        #   freq_on_x + x_rev: highest-freq col is leftmost → edits stored high-to-low
        #   freq_on_y + NOT y_rev: highest-freq row is topmost → edits stored high-to-low
        _is_reversed_display = (freq_on_x and self._x_rev_chk.isChecked()) or \
                               (not freq_on_x and not self._y_rev_chk.isChecked())
        if _is_reversed_display:
            freq_boundaries = list(reversed(freq_boundaries))
        cfg['freq_boundaries'] = freq_boundaries

        cfg = _normalise_matrix(cfg)   # ensure consistent before saving
        self.db.set_risk_matrix(cfg)
        # set_risk_matrix() automatically invalidates the cache; reload from DB
        _risk_matrix_cache.reload_from_db()
        QMessageBox.information(self, "Sparat", "Riskmatris sparad.")
        self.matrix_changed.emit()

    def _apply_freq_preset(self, labels: list, bounds: list):
        """Populate frequency axis headers and boundary edits from a preset.

        labels: ordered lowest-to-highest frequency (data order).
        bounds: n-1 boundary values (events/year), data order lowest first.
        Accounts for current axis orientation (freq_on_x/y) and direction (x_rev/y_rev).
        """
        freq_on_x = (self._axis_combo.currentData() or 'frequency') == 'frequency'
        x_rev     = self._x_rev_chk.isChecked()
        y_rev     = self._y_rev_chk.isChecked()
        n         = len(labels)

        if freq_on_x:
            # _x_label_edits[i] = display column i → data index (n-1-i if x_rev else i)
            for i, e in enumerate(self._x_label_edits):
                data_idx = (n - 1 - i) if x_rev else i
                if 0 <= data_idx < n:
                    e.setText(labels[data_idx])
            # _freq_boundary_edits: edit[i] maps to bval_idx (n-1-(i+1) if x_rev else i)
            for i, e in enumerate(self._freq_boundary_edits):
                bi = (n - 2 - i) if x_rev else i
                if 0 <= bi < len(bounds):
                    e.setText(f"{bounds[bi]:.4g}")
        else:
            # _y_label_edits[0] = top row
            # y_rev=False: top=highest freq → data index n-1-i; y_rev=True: top=lowest → i
            for i, e in enumerate(self._y_label_edits):
                data_idx = i if y_rev else (n - 1 - i)
                if 0 <= data_idx < n:
                    e.setText(labels[data_idx])
            # _freq_boundary_edits for y case: edit[i] → bval_idx (i if y_rev else n-2-i)
            for i, e in enumerate(self._freq_boundary_edits):
                bi = i if y_rev else (n - 2 - i)
                if 0 <= bi < len(bounds):
                    e.setText(f"{bounds[bi]:.4g}")

    def _load_categories(self):
        self._cat_list.clear()
        for cat in self.db.consequence_categories():
            item = QListWidgetItem(cat['name'])
            item.setData(Qt.ItemDataRole.UserRole, cat['id'])
            self._cat_list.addItem(item)

    def _cat_add(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Ny kategori", "Namn:")
        if ok and name.strip():
            self.db.add_category(name.strip())
            self._load_categories()
            self._apply_size()

    def _cat_rename(self):
        from PyQt6.QtWidgets import QInputDialog
        item = self._cat_list.currentItem()
        if not item: return
        name, ok = QInputDialog.getText(self, "Byt namn", "Nytt namn:", text=item.text())
        if ok and name.strip():
            self.db.update_category(item.data(Qt.ItemDataRole.UserRole), name.strip())
            self._load_categories()
            self._apply_size()

    def _cat_delete(self):
        item = self._cat_list.currentItem()
        if not item: return
        self.db.delete_category(item.data(Qt.ItemDataRole.UserRole))
        self._load_categories()
        if hasattr(self, '_sev_def_panel') and self._sev_def_panel:
            self._sev_def_panel.refresh()


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════════════

class PIDManagementPanel(QWidget):
    """PID revision history and sheet reordering panel."""
    sheets_changed = pyqtSignal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 0: Revision history ───────────────────────────────────────────
        rev_widget = QWidget()
        rev_layout = QVBoxLayout(rev_widget)
        rev_layout.setContentsMargins(8, 8, 8, 8)
        rev_layout.setSpacing(6)

        rev_hdr = QHBoxLayout()
        rev_hdr.addWidget(QLabel("Revisionshistorik:"))
        rev_hdr.addStretch()
        clear_all_btn = QPushButton("🗑 Rensa samtliga P&ID och all data")
        clear_all_btn.setStyleSheet(
            "QPushButton{color:#fff;background:#C62828;border-radius:4px;padding:4px 10px;}"
            "QPushButton:hover{background:#B71C1C;}")
        clear_all_btn.clicked.connect(self._clear_all_pid)
        rev_hdr.addWidget(clear_all_btn)
        rev_layout.addLayout(rev_hdr)

        self._rev_table = QTableWidget(0, 4)
        self._rev_table.setHorizontalHeaderLabels(['Revision', 'Anteckningar', 'Datum', 'PDF-fil'])
        self._rev_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self._rev_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._rev_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self._rev_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self._rev_table.setColumnWidth(0, 120)
        self._rev_table.setColumnWidth(2, 130)
        self._rev_table.setColumnWidth(3, 180)
        self._rev_table.verticalHeader().setVisible(False)
        self._rev_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._rev_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._rev_table.setAlternatingRowColors(True)
        self._rev_table.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:4px;}")
        rev_layout.addWidget(self._rev_table)
        tabs.addTab(rev_widget, "Revisioner")

        # ── Tab 1: Sheet management ───────────────────────────────────────────
        sheets_widget = QWidget()
        sheets_layout = QVBoxLayout(sheets_widget)
        sheets_layout.setContentsMargins(8, 8, 8, 8)
        sheets_layout.setSpacing(6)

        sheet_hdr = QHBoxLayout()
        sheet_hdr.addWidget(QLabel("Bladordning — dra för att ändra ordning:"))
        sheet_hdr.addStretch()
        rename_btn = QPushButton("✏️ Byt namn")
        rename_btn.clicked.connect(self._rename_sheet)
        sheet_hdr.addWidget(rename_btn)
        delete_btn = QPushButton("🗑 Ta bort")
        delete_btn.clicked.connect(self._delete_sheets)
        sheet_hdr.addWidget(delete_btn)
        sheets_layout.addLayout(sheet_hdr)

        self._sheet_list = QListWidget()
        self._sheet_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._sheet_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._sheet_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._sheet_list.model().rowsMoved.connect(self._on_sheets_reordered)
        self._sheet_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._sheet_list.customContextMenuRequested.connect(self._sheet_context_menu)
        _base_kp = self._sheet_list.keyPressEvent
        def _sheet_key_press(event, _base=_base_kp):
            if event.key() == Qt.Key.Key_Delete:
                self._delete_sheets()
            else:
                _base(event)
        self._sheet_list.keyPressEvent = _sheet_key_press
        sheets_layout.addWidget(self._sheet_list)
        tabs.addTab(sheets_widget, "Blad")

        self.refresh()

    def refresh(self):
        self._rev_table.setRowCount(0)
        for rev in self.db.get_revisions():
            r = self._rev_table.rowCount()
            self._rev_table.insertRow(r)
            self._rev_table.setItem(r, 0, QTableWidgetItem(rev['revision'] or ''))
            self._rev_table.setItem(r, 1, QTableWidgetItem(rev['notes'] or ''))
            self._rev_table.setItem(r, 2, QTableWidgetItem(rev['created_at'] or ''))
            fname = Path(rev['pdf_path']).name if rev['pdf_path'] else ''
            self._rev_table.setItem(r, 3, QTableWidgetItem(fname))
            self._rev_table.setRowHeight(r, 24)

        self._sheet_list.clear()
        for sheet in self.db.get_sheets():
            item = QListWidgetItem(
                f"{sheet['display_order'] + 1}. {sheet['sheet_name']}  "
                f"(PDF-sida {sheet['physical_page'] + 1})")
            item.setData(Qt.ItemDataRole.UserRole, sheet['id'])
            self._sheet_list.addItem(item)

    def _on_sheets_reordered(self, *_):
        ids = [self._sheet_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._sheet_list.count())]
        self.db.reorder_sheets(ids)
        self.refresh()

    def _rename_sheet(self):
        item = self._sheet_list.currentItem()
        if not item:
            return
        sheet_id = item.data(Qt.ItemDataRole.UserRole)
        current_name = ''
        for s in self.db.get_sheets():
            if s['id'] == sheet_id:
                current_name = s['sheet_name']
                break
        name, ok = QInputDialog.getText(self, "Byt namn", "Bladnamn:", text=current_name)
        if ok and name.strip():
            self.db.update_sheet_name(sheet_id, name.strip())
            self.refresh()

    def _delete_sheets(self):
        selected = self._sheet_list.selectedItems()
        if not selected:
            return
        ids = [item.data(Qt.ItemDataRole.UserRole) for item in selected]

        # Resolve sheet IDs → physical pages and names
        all_sheets = {s['id']: s for s in self.db.get_sheets()}
        pages_info = [(ids[i], all_sheets[ids[i]]['physical_page'],
                       all_sheets[ids[i]]['sheet_name'])
                      for i in range(len(ids)) if ids[i] in all_sheets]
        physical_pages = [p for _, p, _ in pages_info]

        # Check for HAZOP objects on these pages
        objects = self.db.objects_on_pages(physical_pages)
        affected_lines = []
        for sheet_id, phys, name in pages_info:
            obj = objects.get(phys, {})
            parts = []
            if obj.get('markups'):
                parts.append(f"{obj['markups']} nodmarkering{'ar' if obj['markups'] != 1 else ''}")
            if obj.get('causes'):
                parts.append(f"{obj['causes']} orsak{'er' if obj['causes'] != 1 else ''}")
            if obj.get('consequences'):
                parts.append(f"{obj['consequences']} konsekvens{'er' if obj['consequences'] != 1 else ''}")
            if obj.get('safeguards'):
                parts.append(f"{obj['safeguards']} safeguard{'s' if obj['safeguards'] != 1 else ''}")
            if parts:
                affected_lines.append(f"• {name}: {', '.join(parts)}")

        if affected_lines:
            detail = "\n".join(affected_lines)
            box = QMessageBox(self)
            box.setWindowTitle("Ta bort blad")
            box.setIcon(QMessageBox.Icon.Warning)
            count = len(selected)
            box.setText(
                f"{'Dessa blad innehåller' if count > 1 else 'Detta blad innehåller'} "
                f"HAZOP-objekt som kommer tas bort:")
            box.setInformativeText(detail)
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.button(QMessageBox.StandardButton.Yes).setText("Ta bort ändå")
            box.button(QMessageBox.StandardButton.No).setText("Avbryt")
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
        else:
            count = len(selected)
            msg = (f"Ta bort {count} blad?" if count > 1
                   else f"Ta bort '{all_sheets[ids[0]]['sheet_name']}'?")
            ans = QMessageBox.question(self, "Ta bort blad", msg,
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return

        self.db.delete_objects_on_pages(physical_pages)
        self.db.delete_sheets(ids)
        self.refresh()
        self.sheets_changed.emit()

    def _clear_all_pid(self):
        count = len(self.db.get_revisions())
        n_sheets = self.db.conn.execute("SELECT COUNT(*) FROM pid_sheets").fetchone()[0]
        box = QMessageBox(self)
        box.setWindowTitle("Rensa samtliga P&ID och all data")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"Det finns <b>{count} P&ID-revision{'er' if count != 1 else ''}</b> "
            f"och <b>{n_sheets} blad</b> inlagda.\n\n"
            f"Vill du permanent ta bort <b>alla</b> P&ID, blad, markeringar och kopplingar?")
        box.setInformativeText(
            "Denna åtgärd kan inte ångras. HAZOP-analysen (noder, orsaker, konsekvenser) "
            "berörs inte, men alla positioner på P&ID-vyn raderas.")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText("Rensa allt")
        box.button(QMessageBox.StandardButton.No).setText("Avbryt")
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self.db.clear_all_pid_data()
        self.refresh()
        self.sheets_changed.emit()

    def _sheet_context_menu(self, pos):
        selected = self._sheet_list.selectedItems()
        if not selected:
            return
        menu = QMenu(self)
        if len(selected) == 1:
            menu.addAction("✏️ Byt namn", self._rename_sheet)
        menu.addAction("🗑 Ta bort", self._delete_sheets)
        menu.exec(self._sheet_list.viewport().mapToGlobal(pos))


class StudyManagementPanel(QWidget):
    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel("Studiehantering")
        f = QFont(); f.setBold(True); f.setPointSize(14)
        title.setFont(f)
        layout.addWidget(title)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ── Tab 0: Statistics ─────────────────────────────────────────────────
        stats_widget = QWidget()
        stats_layout = QVBoxLayout(stats_widget)
        stats_layout.setContentsMargins(8, 8, 8, 8)
        stats_layout.setSpacing(8)

        self._stats_lbl = QLabel()
        self._stats_lbl.setStyleSheet(
            "background:#f0f4f8; border:1px solid #ccc; border-radius:6px; padding:10px;")
        stats_layout.addWidget(self._stats_lbl)

        bar = QHBoxLayout()
        refresh_btn = QPushButton("🔄 Uppdatera")
        refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(refresh_btn)
        backup_btn = QPushButton("💾 Skapa säkerhetskopia nu")
        backup_btn.setToolTip(
            "Automatiska säkerhetskopior sparas redan löpande i hazop_backups/ — "
            "denna knapp tvingar fram en omedelbar kopia, utan att vänta på nästa automatiska tillfälle.")
        backup_btn.clicked.connect(self._backup_now)
        bar.addWidget(backup_btn)
        bar.addStretch()
        stats_layout.addLayout(bar)

        self._table = QTableWidget(0, 8)
        self._table.setHorizontalHeaderLabels(
            ['Nod', 'Orsak', 'L', 'Konsekvens', 'S', 'Risknivå', 'Kategori', 'Safeguards'])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in [2, 3, 4, 5, 6, 7]:
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        self._table.setColumnWidth(0, 100)
        self._table.setColumnWidth(2, 28)
        self._table.setColumnWidth(4, 28)
        self._table.setColumnWidth(5, 80)
        self._table.setColumnWidth(6, 80)
        self._table.setColumnWidth(7, 150)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:4px;}")
        stats_layout.addWidget(self._table)
        tabs.addTab(stats_widget, "Statistik")

        # ── Tab 1: PID management ─────────────────────────────────────────────
        self._pid_mgmt = PIDManagementPanel(db)
        tabs.addTab(self._pid_mgmt, "PID-hantering")

        self.refresh()

    def _backup_now(self):
        dst = self.db._write_backup(startup=True)   # startup=True bypasses the throttle
        if dst is not None:
            QMessageBox.information(self, "Säkerhetskopia skapad",
                f"Sparade en säkerhetskopia:\n{dst}")
        else:
            QMessageBox.warning(self, "Säkerhetskopiering misslyckades",
                "Kunde inte skapa säkerhetskopian. Kontrollera att det finns "
                "diskutrymme och skrivbehörighet i projektmappen.")

    def refresh(self):
        s = self.db.stats()
        self._stats_lbl.setText(
            f"  Noder: <b>{s['nodes']}</b>   |   Orsaker: <b>{s['causes']}</b>   |   "
            f"Konsekvenser: <b>{s['consequences']}</b>   |   Safeguards: <b>{s['safeguards']}</b>   |   "
            f"Öppna åtgärder: <b>{s['open_actions']}</b>")
        self._stats_lbl.setTextFormat(Qt.TextFormat.RichText)

        self._table.setRowCount(0)
        for row in self.db.all_data():
            level, bg, fg = risk_info(row['likelihood'], row['severity'])
            r = self._table.rowCount()
            self._table.insertRow(r)

            def _c(t, center=False):
                item = QTableWidgetItem(str(t))
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter if center else
                                      Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                return item

            self._table.setItem(r, 0, _c(row['node_name']))
            self._table.setItem(r, 1, _c(row['cause']))
            self._table.setItem(r, 2, _c(row['likelihood'], True))
            self._table.setItem(r, 3, _c(row['consequence']))
            self._table.setItem(r, 4, _c(row['severity'], True))
            risk_item = QTableWidgetItem(f"{level}\nF={row['likelihood']} C={row['severity']}")
            risk_item.setBackground(QBrush(QColor(bg)))
            risk_item.setForeground(QBrush(QColor(fg)))
            risk_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(r, 5, risk_item)
            self._table.setItem(r, 6, _c(row['category']))
            sg_text = '; '.join(
                f"{s['description']}{'(RRF' + str(s['rrf']) + ')' if s['rrf'] > 1 else ''}"
                for s in row['safeguards']) or '—'
            self._table.setItem(r, 7, _c(sg_text))
            self._table.setRowHeight(r, 28)

    def refresh_pid(self):
        self._pid_mgmt.refresh()


# Keep old name as alias so any remaining references don't crash
AdminPanel = StudyManagementPanel


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════════════════════

def export_excel(db: Database, filepath: str):
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    except ImportError:
        return False, "openpyxl saknas.\nKör: pip install openpyxl"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "HAZOP"

    HEADERS = ['Nod', 'P&ID', 'Orsak', 'F', 'Konsekvens', 'C', 'Risknivå',
               'Kategori', 'Safeguards', 'Åtgärder']
    COL_WIDTHS = [20, 12, 32, 6, 32, 6, 14, 12, 40, 42]
    RISK_FILLS = {'Låg': 'C6EFCE', 'Medium': 'FFEB9C', 'Hög': 'FFC7CE', 'Kritisk': 'FF0000'}

    thin   = Side(border_style='thin', color='000000')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal='center', vertical='center', wrap_text=True)
    wrap   = Alignment(vertical='top', wrap_text=True)

    for col, (hdr, width) in enumerate(zip(HEADERS, COL_WIDTHS), 1):
        cell = ws.cell(row=1, column=col, value=hdr)
        cell.fill = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
        cell.font = Font(bold=True, color='FFFFFF', size=10)
        cell.alignment = center; cell.border = border
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    ws.row_dimensions[1].height = 22
    ws.freeze_panes = 'A2'

    for r, row in enumerate(db.all_data(), 2):
        level, _, _ = risk_info(row['likelihood'], row['severity'])
        acts_str = '\n'.join(
            f"• {a['description']} | {a['responsible']} | {a['due_date']} | {a['status']}"
            for a in row['actions'])
        sg_str = '; '.join(
            f"{s['description']} (RRF{s['rrf']})" if s['rrf'] > 1 else s['description']
            for s in row['safeguards'])
        values = [row['node_name'], row['node_pid'], row['cause'], row['likelihood'],
                  row['consequence'], row['severity'], level,
                  row['category'], sg_str, acts_str]
        for c, val in enumerate(values, 1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.border = border
            cell.alignment = center if c in (4, 6) else wrap
            if c == 7:
                fc = RISK_FILLS.get(level, 'FFFFFF')
                cell.fill = PatternFill(start_color=fc, end_color=fc, fill_type='solid')
        ws.row_dimensions[r].height = 36

    try:
        wb.save(filepath); return True, ""
    except Exception as e:
        return False, str(e)


def export_pdf(db: Database, filepath: str):
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    except ImportError:
        return False, "reportlab saknas.\nKör: pip install reportlab"

    doc = SimpleDocTemplate(filepath, pagesize=landscape(A4),
                            leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    cs = ParagraphStyle('c', fontSize=7, leading=9)
    hs = ParagraphStyle('h', fontSize=8, leading=10, textColor=colors.white,
                        fontName='Helvetica-Bold')

    RISK_COLORS_PDF = {
        'Låg':     colors.HexColor('#27ae60'),
        'Medium':  colors.HexColor('#f39c12'),
        'Hög':     colors.HexColor('#e67e22'),
        'Kritisk': colors.HexColor('#e74c3c'),
    }

    headers = ['Nod / P&ID', 'Orsak', 'L', 'Konsekvens', 'S', 'Risknivå', 'Safeguards', 'Åtgärder']
    table_data = [[Paragraph(h, hs) for h in headers]]
    row_styles = []

    for i, row in enumerate(db.all_data(), 1):
        level, _, _ = risk_info(row['likelihood'], row['severity'])
        acts_str = '<br/>'.join(
            f"• {a['description']} ({a['status']})" for a in row['actions']) or '—'
        sg_str = '<br/>'.join(
            f"• {s['description']}" + (f" RRF{s['rrf']}" if s['rrf'] > 1 else '')
            for s in row['safeguards']) or '—'
        table_data.append([
            Paragraph(f"{row['node_name']}<br/><font size='6'>{row['node_pid']}</font>", cs),
            Paragraph(row['cause'], cs),
            Paragraph(str(row['likelihood']), cs),
            Paragraph(row['consequence'], cs),
            Paragraph(str(row['severity']), cs),
            Paragraph(f"<b>{level}</b><br/>F={row['likelihood']} C={row['severity']}", cs),
            Paragraph(sg_str, cs),
            Paragraph(acts_str, cs),
        ])
        rc = RISK_COLORS_PDF.get(level, colors.white)
        row_styles.append(('BACKGROUND', (5, i), (5, i), rc))
        row_styles.append(('TEXTCOLOR', (5, i), (5, i), colors.white))

    col_widths = [32*mm, 40*mm, 8*mm, 45*mm, 8*mm, 22*mm, 55*mm, 55*mm]
    t = Table(table_data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1F4E79')),
        ('GRID', (0, 0), (-1, -1), 0.4, colors.grey),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f5f5f5'), colors.white]),
    ] + row_styles))

    elements = [Paragraph("HAZOP — Rapport", styles['Title']), Spacer(1, 5*mm), t]
    try:
        doc.build(elements); return True, ""
    except Exception as e:
        return False, str(e)


# ══════════════════════════════════════════════════════════════════════════════
# EQUIPMENT PANEL
# ══════════════════════════════════════════════════════════════════════════════

def _tag_prefix(tag: str) -> str:
    m = re.match(r'^([A-Z]+)', tag.upper())
    return m.group(1) if m else tag


_EQ_TYPE_ITEMS = [''] + sorted(COMPONENT_TYPES.keys()) + ['Rörledning', 'Övrigt / Okänd']


class EquipmentTagPopup(QDialog):
    """Small popup for the P&ID right-click menu's "🔧 Objekt" action —
    pick an object type and optionally set/edit its tag, independent of
    any cause (2026-08-07, see NOTES.md). Deliberately not CauseObjectPopup
    or StandardCausesPickerPopup: this has no standard-cause list to show,
    it only resolves (tag, comp_type) for PIDPanel.place_equipment_marker."""
    committed = pyqtSignal(str, str)  # (comp_tag, comp_type)

    def __init__(self, db, suggested_tag='', suggested_type='', parent=None):
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("Objekt")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setMinimumWidth(240)

        _small = "font-size:10px;"
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 8, 8, 8)

        title = QLabel("<b>Nytt objekt på P&amp;ID</b>")
        title.setStyleSheet("font-size:11px; color:#8D9299;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(3)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._tag_edit = QLineEdit(suggested_tag)
        self._tag_edit.setPlaceholderText("t.ex. P-101 (valfritt)")
        self._tag_edit.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._tag_edit.setStyleSheet(_small)
        completer = _make_tag_completer(db, self)
        if completer:
            self._tag_edit.setCompleter(completer)
        tag_lbl = QLabel("Tag:")
        tag_lbl.setStyleSheet(_small)
        form.addRow(tag_lbl, self._tag_edit)

        self._type_cb = QComboBox()
        self._type_cb.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._type_cb.setStyleSheet(_small)
        self._type_cb.addItems(_EQ_TYPE_ITEMS)
        if suggested_type:
            idx = self._type_cb.findText(suggested_type)
            if idx < 0:
                self._type_cb.addItem(suggested_type)
                idx = self._type_cb.count() - 1
            self._type_cb.setCurrentIndex(idx)
        typ_lbl = QLabel("Typ:")
        typ_lbl.setStyleSheet(_small)
        form.addRow(typ_lbl, self._type_cb)
        layout.addLayout(form)

        # Duplicate-tag hint (2026-08-10, see NOTES.md) — place_equipment_marker
        # silently reuses an existing equipment_catalog row for a tag that's
        # already known (never creates a duplicate); this just surfaces that
        # fact to the user instead of leaving it invisible.
        self._dup_hint = QLabel("")
        self._dup_hint.setStyleSheet("font-size:9px; color:#b8860b;")
        self._dup_hint.setWordWrap(True)
        layout.addWidget(self._dup_hint)
        self._tag_edit.textChanged.connect(self._check_duplicate_tag)
        self._check_duplicate_tag(suggested_tag)

        btns = QHBoxLayout()
        btns.setSpacing(4)
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.setFixedHeight(CONFIG['H_CTRL_STD'])
        ok.clicked.connect(self._ok)
        cancel = QPushButton("Avbryt")
        cancel.setFixedHeight(CONFIG['H_CTRL_STD'])
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addStretch()
        btns.addWidget(cancel)
        layout.addLayout(btns)

        self._tag_edit.returnPressed.connect(self._ok)

    def _check_duplicate_tag(self, text):
        tag = (text or '').strip()
        existing = self._db.get_equipment_by_tag(tag) if tag else None
        if existing:
            self._dup_hint.setText(
                f"ℹ️ \"{existing['tag']}\" finns redan i katalogen "
                f"({existing.get('equipment_type') or '?'}) — kopplas till den befintliga raden.")
        else:
            self._dup_hint.setText("")

    def _ok(self):
        tag = self._tag_edit.text().strip().upper()
        comp_type = self._type_cb.currentText().strip()
        if not tag and not comp_type:
            QMessageBox.information(self, "Ange typ eller tag",
                "Ange minst en typ eller ett taggnummer för objektet.")
            return
        self.committed.emit(tag, comp_type)
        self.accept()


# Column indices
_EC_CHK  = 0
_EC_TAG  = 1
_EC_PFX  = 2
_EC_PAGE = 3
_EC_OCR  = 4
_EC_TYPE = 5
_EC_DESC = 6
_EC_DEL  = 7


class _EquipmentTableModel(QAbstractTableModel):
    """Backs EquipmentPanel's QTableView. Rows are kept as plain dicts in
    memory (cheap) and DB writes happen in setData()/delete_row() — no more
    persistent per-row QComboBox/QPushButton widgets, which is what made
    populating 10k+ rows take upwards of a minute (see NOTES.md, 2026-08-06)."""

    write_failed = pyqtSignal(str)   # emitted with an error message on a failed DB write

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db    = db
        self._rows = []   # list[dict], one per equipment_catalog row

    def load(self):
        self.beginResetModel()
        self._rows = [dict(r) for r in self.db.equipment_items()]
        self.endResetModel()

    def rows(self):
        return self._rows

    def row_dict(self, row):
        return self._rows[row]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 8

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ['✓', 'Tagg', 'Prefix', 'Sida', 'OCR', 'Utrustningstyp', 'Beskrivning', ''][section]
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        col = index.column()
        if col == _EC_CHK:
            return base | Qt.ItemFlag.ItemIsUserCheckable
        if col in (_EC_TAG, _EC_TYPE, _EC_DESC):
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == _EC_TAG:  return row['tag']
            if col == _EC_PFX:  return row.get('prefix') or _tag_prefix(row['tag'])
            if col == _EC_PAGE: return str(row.get('pid_page', 0) + 1)
            if col == _EC_OCR:  return '🔬' if row.get('is_ocr') else ''
            if col == _EC_TYPE: return row.get('equipment_type', '') or ''
            if col == _EC_DESC: return row.get('description', '') or ''
            if col == _EC_DEL:  return 'Ta bort' if role == Qt.ItemDataRole.DisplayRole else None
            return None
        if role == Qt.ItemDataRole.CheckStateRole and col == _EC_CHK:
            return Qt.CheckState.Checked if row.get('include', 1) else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (_EC_PFX, _EC_PAGE, _EC_OCR):
            return Qt.AlignmentFlag.AlignCenter
        if role == Qt.ItemDataRole.BackgroundRole and col == _EC_TAG and row.get('is_ocr'):
            return QBrush(QColor('#fff3cd'))
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == _EC_TAG and row.get('is_ocr'):
                return "Identifierad via OCR — kontrollera taggen"
            if col == _EC_OCR:
                return "Hittad via OCR" if row.get('is_ocr') else "Hittad via PDF-text"
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        row_i = index.row()
        col   = index.column()
        row   = self._rows[row_i]
        try:
            if role == Qt.ItemDataRole.CheckStateRole and col == _EC_CHK:
                checked = value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
                row['include'] = 1 if checked else 0
                self.db.conn.execute("UPDATE equipment_catalog SET include=? WHERE id=?",
                                     (row['include'], row['id']))
                self.db.conn.commit()
                self.dataChanged.emit(index, index, [role])
                return True

            if role != Qt.ItemDataRole.EditRole:
                return False

            if col == _EC_TAG:
                new_tag = str(value).strip().upper()
                new_pfx = _tag_prefix(new_tag)
                row['tag']    = new_tag
                row['prefix'] = new_pfx
                # Suggest a type from the new prefix only if none set yet —
                # matches the pre-rewrite behaviour exactly.
                if not row.get('equipment_type'):
                    known = KNOWN_PREFIXES.get(new_pfx)
                    if known:
                        row['equipment_type'] = known[1]
                self.db.update_equipment_item(
                    row['id'], new_tag, new_pfx,
                    row.get('equipment_type', ''), row.get('description', ''))
                first = self.index(row_i, 0)
                last  = self.index(row_i, self.columnCount() - 1)
                self.dataChanged.emit(first, last)
                return True

            if col == _EC_TYPE:
                row['equipment_type'] = str(value)
                self.db.conn.execute(
                    "UPDATE equipment_catalog SET equipment_type=? WHERE id=?",
                    (row['equipment_type'], row['id']))
                self.db.conn.commit()
                self.dataChanged.emit(index, index, [role])
                return True

            if col == _EC_DESC:
                row['description'] = str(value)
                self.db.update_equipment_item(
                    row['id'], row['tag'], row.get('prefix', ''),
                    row.get('equipment_type', ''), row['description'])
                self.dataChanged.emit(index, index, [role])
                return True
        except Exception as e:
            logging.exception('_EquipmentTableModel.setData: DB write failed (row=%d col=%d)',
                              row_i, col)
            self.write_failed.emit(str(e))
            return False
        return False

    def delete_row(self, row_i):
        row = self._rows[row_i]
        self.db.delete_equipment_item(row['id'])
        self.beginRemoveRows(QModelIndex(), row_i, row_i)
        del self._rows[row_i]
        self.endRemoveRows()

    def bulk_set_include(self, row_indices, checked: bool):
        """Set 'include' for many rows with a single commit — setData() commits
        per call, which is correct for one edit but would mean one fsync-ish
        SQLite commit per row (thousands of them) for a bulk checkbox action."""
        if not row_indices:
            return
        inc = 1 if checked else 0
        ids = []
        for r in row_indices:
            row = self._rows[r]
            row['include'] = inc
            ids.append(row['id'])
        try:
            self.db.conn.executemany(
                "UPDATE equipment_catalog SET include=? WHERE id=?", [(inc, i) for i in ids])
            self.db.conn.commit()
        except Exception as e:
            logging.exception('_EquipmentTableModel.bulk_set_include: DB write failed')
            self.write_failed.emit(str(e))
            return
        top = self.index(min(row_indices), _EC_CHK)
        bot = self.index(max(row_indices), _EC_CHK)
        self.dataChanged.emit(top, bot, [Qt.ItemDataRole.CheckStateRole])


class _EquipmentFilterProxy(QSortFilterProxyModel):
    """Search-text + 'OCR only' filter for EquipmentPanel — replaces the old
    per-row setRowHidden() loop, which needed the underlying QTableWidget."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text     = ''
        self._ocr_only = False

    def set_filter_text(self, text: str):
        self._text = text.lower()
        self.invalidateFilter()

    def set_ocr_only(self, on: bool):
        self._ocr_only = on
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        row   = model.row_dict(source_row)
        if self._ocr_only and not row.get('is_ocr'):
            return False
        if self._text:
            tag_t  = row['tag'].lower()
            type_t = (row.get('equipment_type') or '').lower()
            pg_t   = str(row.get('pid_page', 0) + 1)
            if (self._text not in tag_t and self._text not in type_t
                    and self._text not in pg_t):
                return False
        return True


class EquipmentPanel(QWidget):
    """Persistent equipment register — scan P&ID, review, edit and create nodes."""

    markers_saved = pyqtSignal()   # equipment_markers layer changed — P&ID view should reload

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self._model = _EquipmentTableModel(db, self)
        self._model.write_failed.connect(
            lambda msg: QMessageBox.critical(self, "Fel vid celländring (utrustning)", msg))
        self._proxy = _EquipmentFilterProxy(self)
        self._proxy.setSourceModel(self._model)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        title = QLabel("Utrustningsregister")
        f = QFont(); f.setBold(True); f.setPointSize(14)
        title.setFont(f)
        layout.addWidget(title)

        # Toolbar
        tb = QHBoxLayout()
        self._scan_btn = QPushButton("🔍 Skanna P&ID")
        self._scan_btn.setToolTip("Skannar inläst P&ID-fil efter utrustningstaggar")
        self._scan_btn.setStyleSheet(
            "background:#17191C; color:white; border:none; border-radius:4px; padding:3px 10px;")
        self._scan_btn.clicked.connect(self._scan)

        add_btn = QPushButton("+ Lägg till")
        add_btn.setToolTip("Lägg till en tagg manuellt")
        add_btn.clicked.connect(self._add_manual)

        refresh_btn = QPushButton("🔄 Uppdatera")
        refresh_btn.clicked.connect(self.refresh)

        self._create_btn = QPushButton("🏭 Skapa HAZOP-noder")
        self._create_btn.setToolTip("Skapar en nod per ikryssad rad")
        self._create_btn.clicked.connect(self._create_nodes)

        self._autodetect_btn = QPushButton("🎯 Hitta objekt på P&ID")
        self._autodetect_btn.setToolTip(
            "Analyserar utrustning (ventiler, pumpar, instrument m.fl.): kopplar\n"
            "varje känd tagg till dess ritade symbol OCH letar efter formigenkända\n"
            "symboler som saknar tagg — allt i en bakgrundskörning med synlig\n"
            "progress.\n"
            "Kör 🔍 Skanna P&ID först om registret är tomt.")
        self._autodetect_btn.clicked.connect(self._autodetect)

        clear_btn = QPushButton("🗑 Rensa utrustning")
        clear_btn.setToolTip("Tar bort alla poster i utrustningsregistret")
        clear_btn.setStyleSheet("color:#c0392b; font-weight:bold;")
        clear_btn.clicked.connect(self._clear)

        for btn in [self._scan_btn, add_btn, refresh_btn, self._create_btn,
                    self._autodetect_btn, clear_btn]:
            tb.addWidget(btn)
        tb.addStretch()
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color:#555; font-size:11px;")
        tb.addWidget(self._status_lbl)
        layout.addLayout(tb)

        # Filter bar
        fb = QHBoxLayout()
        fb.addWidget(QLabel("Filtrera:"))
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Sök tagg, typ, sida…")
        self._filter.textChanged.connect(self._apply_filter)
        fb.addWidget(self._filter)
        sel_all  = QPushButton("Välj alla")
        desel    = QPushButton("Avmarkera alla")
        self._ocr_only = QPushButton("Visa OCR")
        self._ocr_only.setCheckable(True)
        self._ocr_only.toggled.connect(self._apply_filter)
        sel_all.clicked.connect(lambda: self._bulk_check(True))
        desel.clicked.connect(lambda: self._bulk_check(False))
        for b in [sel_all, desel, self._ocr_only]:
            fb.addWidget(b)
        layout.addLayout(fb)

        # Table — QTableView + QAbstractTableModel instead of QTableWidget:
        # populating this used to mean inserting one row (with a real
        # QComboBox *and* a real QPushButton widget) per equipment item,
        # which does not scale. See _EquipmentTableModel / _ComboBoxCellDelegate
        # / _ButtonCellDelegate above.
        self._tbl = QTableView()
        self._tbl.setModel(self._proxy)
        self._tbl.setItemDelegateForColumn(
            _EC_TYPE, _ComboBoxCellDelegate(_EQ_TYPE_ITEMS, self._tbl))
        self._tbl.setItemDelegateForColumn(
            _EC_DEL, _ButtonCellDelegate("Ta bort", self._on_delete_clicked, self._tbl))
        hdr = self._tbl.horizontalHeader()
        modes = [
            (_EC_CHK,  QHeaderView.ResizeMode.Fixed),
            (_EC_TAG,  QHeaderView.ResizeMode.Interactive),
            (_EC_PFX,  QHeaderView.ResizeMode.Fixed),
            (_EC_PAGE, QHeaderView.ResizeMode.Fixed),
            (_EC_OCR,  QHeaderView.ResizeMode.Fixed),
            (_EC_TYPE, QHeaderView.ResizeMode.Interactive),
            (_EC_DESC, QHeaderView.ResizeMode.Stretch),
            (_EC_DEL,  QHeaderView.ResizeMode.Fixed),
        ]
        widths = {_EC_CHK: 30, _EC_TAG: 110, _EC_PFX: 60, _EC_PAGE: 44,
                  _EC_OCR: 36, _EC_TYPE: 185, _EC_DEL: 64}
        for col, mode in modes:
            hdr.setSectionResizeMode(col, mode)
        for col, w in widths.items():
            self._tbl.setColumnWidth(col, w)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.verticalHeader().setDefaultSectionSize(26)
        self._tbl.setAlternatingRowColors(True)
        self._tbl.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._tbl.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked |
                                  QAbstractItemView.EditTrigger.EditKeyPressed)
        self._tbl.setStyleSheet(
            "QHeaderView::section{background:#F5F5F3;color:#8D9299;font-weight:600;padding:4px;}")
        self._tbl.clicked.connect(self._on_cell_clicked)
        layout.addWidget(self._tbl)

        self._model.dataChanged.connect(lambda *a: self._update_status())

        # No eager refresh() here: populating this table used to mean
        # building thousands of cell widgets, which does not scale — doing
        # that unconditionally in __init__ used to block the whole app at
        # startup even when the user never opens the Equipment page.
        # MainWindow._switch_view() already calls refresh() every time this
        # page (index 2) becomes active, including the first time.

    # ── Populate ──────────────────────────────────────────────────────────────

    def refresh(self):
        self._model.load()
        self._apply_filter()

    def select_row_by_equipment_id(self, equipment_id):
        """Select and scroll to the register row for `equipment_id` — used
        when a valve marker on the P&ID is clicked
        (MainWindow._on_equipment_marker_navigate). Clears any active
        filter first so the target row can never be hidden by it."""
        src_row = next((i for i, row in enumerate(self._model.rows())
                        if row.get('id') == equipment_id), None)
        if src_row is None:
            return
        if self._filter.text() or self._ocr_only.isChecked():
            self._filter.clear()
            self._ocr_only.setChecked(False)
        proxy_index = self._proxy.mapFromSource(self._model.index(src_row, _EC_TAG))
        if not proxy_index.isValid():
            return
        self._tbl.setCurrentIndex(proxy_index)
        self._tbl.selectRow(proxy_index.row())
        self._tbl.scrollTo(proxy_index)

    def _on_cell_clicked(self, index):
        if index.column() == _EC_TYPE:
            self._tbl.edit(index)

    def _on_delete_clicked(self, proxy_index):
        self._model.delete_row(self._proxy.mapToSource(proxy_index).row())
        self._update_status()

    # ── Filter / selection ────────────────────────────────────────────────────

    def _apply_filter(self):
        self._proxy.set_filter_text(self._filter.text())
        self._proxy.set_ocr_only(self._ocr_only.isChecked())
        self._update_status()

    def _bulk_check(self, checked: bool):
        src_rows = [self._proxy.mapToSource(self._proxy.index(pr, _EC_CHK)).row()
                    for pr in range(self._proxy.rowCount())]
        self._model.bulk_set_include(src_rows, checked)
        self._update_status()

    def _update_status(self):
        total_all = self._model.rowCount()
        visible   = self._proxy.rowCount()
        checked   = sum(1 for pr in range(visible)
                        if self._model.row_dict(
                            self._proxy.mapToSource(self._proxy.index(pr, _EC_CHK)).row()
                        ).get('include', 1))
        self._status_lbl.setText(
            f"{total_all} taggar totalt  |  {visible} visas  |  {checked} valda")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _add_manual(self):
        # Same EquipmentTagPopup used by the P&ID's "🔧 Objekt" action
        # (2026-08-09, see NOTES.md) — one dialog for "manually add an
        # equipment item" everywhere, with an actual type field and the
        # duplicate-tag hint, instead of this page's own bare
        # QInputDialog.getText() that only ever guessed the type from
        # KNOWN_PREFIXES and had no duplicate check at all.
        popup = EquipmentTagPopup(self.db, parent=self)
        popup.committed.connect(self._on_manual_equipment_committed)
        popup.exec()

    def _on_manual_equipment_committed(self, tag, comp_type):
        tag = tag.strip().upper()
        if not tag:
            return
        existing = self.db.get_equipment_by_tag(tag)
        if existing:
            if comp_type and comp_type != existing.get('equipment_type'):
                self.db.update_equipment_item(
                    existing['id'], existing['tag'], existing['prefix'],
                    comp_type, existing.get('description') or '')
        else:
            pfx = _tag_prefix(tag)
            known = KNOWN_PREFIXES.get(pfx, ('', ''))
            self.db.add_equipment_item(
                tag, tag, pfx, 0, comp_type or (known[1] if known else ''), '', 0)
        self.refresh()

    def _clear(self):
        n = len(self.db.equipment_items())
        reply = QMessageBox.question(
            self, "Rensa utrustning",
            f"Ta bort alla {n} poster i utrustningsregistret?\n\n"
            "Detta kan inte ångras.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Ok:
            self.db.clear_equipment_catalog()
            self.refresh()

    def _create_nodes(self):
        # Iterates ALL rows (not just those matching the current filter) —
        # only the checkbox state matters, same as before the rewrite.
        to_create = []
        for row in self._model.rows():
            if row.get('include', 1):
                tag  = row['tag']
                pg   = row.get('pid_page', 0)
                et   = row.get('equipment_type', '') or ''
                desc = row.get('description', '') or ''
                if tag:
                    to_create.append((tag, pg, et, desc))
        if not to_create:
            QMessageBox.information(self, "Ingen vald", "Kryssa i minst en rad.")
            return
        created = 0
        for tag, pg, et, desc in to_create:
            nid = self.db.add_node_with_markup(
                tag, [], {'color': '#FF8C00', 'width': 2, 'alpha': 180}, pg)
            self.db.conn.execute(
                "UPDATE nodes SET name=?, pid_ref=?, description=? WHERE id=?",
                (tag, f"Sida {pg + 1}", f"{et}{': ' + desc if desc else ''}", nid))
            self.db.conn.commit()
            created += 1
        QMessageBox.information(self, "Klart",
            f"{created} HAZOP-noder skapade.\nGå till P&ID-vyn och uppdatera trädet.")

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _scan(self):
        """🔍 Skanna P&ID — runs on background worker PROCESSES
        (ParallelTagScanWorker) when the document is large enough for
        multi-core parallelism to be worth it, with live per-page
        progress (PageProgressDialog) — see NOTES.md "Flerkärnig
        parallellisering av Analysera P&ID". Falls back to a single
        sequential pass for small documents or if the process pool can't
        start."""
        if not HAS_PYMUPDF:
            QMessageBox.warning(self, "PyMuPDF saknas",
                "Installera med:  pip install PyMuPDF")
            return

        path = self.db.get_pid_path()
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "Ingen P&ID",
                "Öppna en P&ID-fil i P&ID-vyn först, sedan kan du skanna härifrån.")
            return

        try:
            import fitz
            n_pages = fitz.open(str(path)).page_count
        except Exception as e:
            QMessageBox.warning(self, "PDF-fel", f"Kunde inte öppna PDF:\n{e}")
            return

        # OCR choice -- honours "OCR-standardval" (Inställningar →
        # P&ID-inställningar, config key 'ocr_default_engine') to skip the
        # Yes/No prompt when the user has picked a specific default engine.
        use_ocr, ocr_engine = resolve_ocr_scan_choice(self.db, self)

        dlg = PageProgressDialog("Skannar P&ID…", n_pages, self)
        worker = ParallelTagScanWorker(path, use_ocr=use_ocr, ocr_engine=ocr_engine)
        self._scan_thread = worker   # keep a reference so it isn't GC'd mid-run

        cancelled_flag = {'v': False}

        def _on_cancel():
            cancelled_flag['v'] = True
            worker.requestInterruption()

        def _on_finished(result):
            dlg.close()
            self._scan_thread = None
            if cancelled_flag['v']:
                return

            meta = result.pop('_meta', {})
            real = {k: v for k, v in result.items() if not k.startswith('_')}

            if not real:
                QMessageBox.warning(
                    self, "Inga taggar",
                    "Inga utrustningstaggar hittades.\n\n"
                    + ("Prova med OCR aktiverat (installera pytesseract eller easyocr)."
                       if not use_ocr else
                       "Kontrollera att PDF-texten är läsbar och försök med OCR."))
                return

            # Import to DB — shared with "📋 Analysera P&ID" (PIDPanel._analyze_pid,
            # pid_viewer.py) now that both buttons trigger the same underlying
            # scan; also cross-write "Identifierade objekt" so that panel stays
            # in sync regardless of which button was used.
            apply_scan_result_to_equipment_catalog(self.db, real)
            upsert_identified_tags_from_scan(self.db, real)

            # Build summary
            n_tags   = sum(len(d['tags']) for d in real.values())
            n_groups = len(real)
            ocr_used = meta.get('ocr_used', False)
            ocr_eng  = meta.get('ocr_engine', '')

            type_counts: dict = {}
            for prefix, data in real.items():
                known = KNOWN_PREFIXES.get(prefix)
                et    = known[1] if known else 'Okänd'
                type_counts[et] = type_counts.get(et, 0) + len(data['tags'])

            lines = "\n".join(
                f"  • {t}: {c} st"
                for t, c in sorted(type_counts.items(), key=lambda x: -x[1]))
            ocr_line = f"\n🔬 OCR användes ({ocr_eng})\n" if ocr_used else "\n"

            QMessageBox.information(
                self, "Skanning klar ✅",
                f"Skanning klar!\n\n"
                f"Totalt hittade:  {n_tags}  taggar\n"
                f"Prefix-grupper:  {n_groups}{ocr_line}\n"
                f"Utrustningstyper:\n{lines}\n\n"
                f"Tabellen nedan har uppdaterats.\n"
                f"Redigera eventuella OCR-fel (gul bakgrund) och kryssa i\n"
                f"de taggar du vill skapa HAZOP-noder för.")

            self.refresh()

        worker.page_progress.connect(dlg.set_page_status)
        worker.finished_scan.connect(_on_finished)
        dlg.canceled.connect(_on_cancel)
        worker.start()
        dlg.exec()

    def _autodetect(self):
        """🎯 Hitta objekt på P&ID — full analysis: weighted tag<->symbol
        association for every known tag in the register (any equipment
        type) AND shape-anchored hunting for valve/pump/instrument-shaped
        symbols with no tag, against one shared per-page cluster
        extraction (detect_equipment_and_valves). Runs on background
        worker PROCESSES (ParallelEquipmentAnalysisWorker) when the
        document is large enough for multi-core parallelism to be worth
        it — falls back to the proven single-thread EquipmentAnalysisWorker
        path otherwise — with live per-page progress (PageProgressDialog),
        including on a 50-page document. See NOTES.md "Flerkärnig
        parallellisering av Analysera P&ID".

        Widened from valve-only to every equipment type (2026-08-10, see
        NOTES.md) — the underlying detect_equipment_and_valves() pipeline
        has done shape-anchored pump/instrument hunting on UNTAGGED
        clusters since 2026-08-07 regardless of this filter; restricting
        tag_points to VALVE_COMPONENT_TYPES only meant a real, already-
        known pump/instrument tag never got a chance at weighted
        association with its own symbol, even though the shape side was
        perfectly capable of confirming it. Renamed from "Hitta ventiler"
        to reflect what it's always been trending toward: recognizing the
        SHAPE of any piece of equipment, not just valves.

        Uses EVERY row with a tag in the register, not just checked ones —
        the global weighted association gets WORSE, not just redundant, if
        the candidate pool is pre-filtered, since a real symbol match for
        an unchecked tag would otherwise be unavailable to steal a
        cluster away from a genuinely wrong candidate.
        """
        if not HAS_PYMUPDF:
            QMessageBox.warning(self, "PyMuPDF saknas",
                "Installera med:  pip install PyMuPDF")
            return

        tag_points = []          # (tag, prefix, page, x, y, conf) — x/y resolved in-thread
        tag_to_equipment_id = {}
        for row in self._model.rows():
            tag = (row.get('tag') or '').strip()
            if not tag:
                continue
            prefix = row.get('prefix') or _tag_prefix(tag)
            tag_points.append((tag, prefix, row.get('pid_page', 0), None, None, 1.0))
            tag_to_equipment_id[tag] = row['id']

        if not tag_points:
            QMessageBox.information(
                self, "Inga taggar i registret",
                "Hittade inga taggade rader i Utrustningsregistret.\n\n"
                "Kör 🔍 Skanna P&ID om registret är tomt.")
            return

        path = self.db.get_pid_path()
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "Ingen P&ID",
                "Öppna en P&ID-fil i P&ID-vyn först.")
            return
        try:
            import fitz
            n_pages = fitz.open(str(path)).page_count
        except Exception as e:
            QMessageBox.warning(self, "PDF-fel", f"Kunde inte öppna PDF:\n{e}")
            return

        dlg = PageProgressDialog("Analyserar P&ID…", n_pages, self)
        thread = ParallelEquipmentAnalysisWorker(path, tag_points)
        self._analysis_thread = thread   # keep a reference so it isn't GC'd mid-run

        cancelled_flag = {'v': False}

        def _on_cancel():
            cancelled_flag['v'] = True
            thread.requestInterruption()

        def _on_finished(results, rejected):
            dlg.close()
            self._analysis_thread = None
            if cancelled_flag['v']:
                return
            for res in results:
                if res.get('tag_status') != 'untagged':
                    res['equipment_id'] = tag_to_equipment_id.get(res['tag'])
            if not results:
                QMessageBox.information(self, "Inget hittat",
                    "Inga objekt eller symboler hittades.")
                return
            review_dlg = EquipmentMarkerReviewDialog(results, self.db, parent=self, rejected=rejected)
            if review_dlg.exec():
                self.markers_saved.emit()

        thread.page_progress.connect(dlg.set_page_status)
        thread.finished_analysis.connect(_on_finished)
        dlg.canceled.connect(_on_cancel)
        thread.start()
        dlg.exec()


# ══════════════════════════════════════════════════════════════════════════════
# REUSE CAUSES DIALOG
# ══════════════════════════════════════════════════════════════════════════════

class ReuseDeviationCausesDialog(QDialog):
    """Pre-step dialog shown before P&ID placement.

    Lists causes from other deviations in the same node, organised by
    deviation with hierarchical reference labels (e.g. 1.2.3).
    User can toggle Referera / Invers per cause; accepted selections are
    created as new causes in the target deviation before P&ID mode opens.
    """

    SKIP = 2   # dialog result code for "Hoppa över"

    def __init__(self, target_dev_name, existing_causes, parent=None):
        """
        existing_causes — list of dicts with keys:
            id, description, deviation_name, deviation_id,
            ref_label (e.g. '1.2.3'), dev_label (e.g. '1.2')
        """
        super().__init__(parent)
        self.setWindowTitle("Återanvänd orsaker från andra avvikelser")
        self.setMinimumWidth(720)
        self.setMinimumHeight(560)
        self.resize(800, 640)

        # key → (mode, description, original_cause_id)
        # key is cause_id (int) for individual causes, or f"dev_{dev_id}" for deviation-level
        # original_cause_id is None for deviation-level entries (no marker to copy)
        self._selections: dict = {}

        layout = QVBoxLayout(self)

        hdr = QLabel(
            f"Lägger till orsaker under avvikelsen: <b>{target_dev_name}</b><br>"
            "<span style='color:gray;font-size:11px'>"
            "Välj orsaker från andra avvikelser att referera (kopiera) eller invertera "
            "(högt↔lågt, stänger↔öppnar, …).</span>")
        hdr.setWordWrap(True)
        layout.addWidget(hdr)

        # ── Scrollable cause list ─────────────────────────────────────────────
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(2, 2, 2, 2)
        inner_layout.setSpacing(2)

        grouped: dict = {}
        order:   list = []
        for c in existing_causes:
            dn = c['deviation_name']
            if dn not in grouped:
                grouped[dn] = []
                order.append(dn)
            grouped[dn].append(c)

        global_pos = 0
        for dev_n in order:
            causes  = grouped[dev_n]
            dev_lbl = causes[0]['dev_label']
            dev_id  = causes[0]['deviation_id']
            dev_key = f"dev_{dev_id}"
            dev_pos = global_pos
            global_pos += 1

            # ── Deviation header row ──────────────────────────────────────────
            hdr_w = QWidget()
            hdr_w.setStyleSheet("background:#ebebeb;border-radius:3px;")
            hdr_h = QHBoxLayout(hdr_w)
            hdr_h.setContentsMargins(6, 3, 4, 3)
            hdr_h.setSpacing(6)

            hdr_lbl = QLabel(
                f"<b><span style='color:#555'>{dev_lbl}</span>&nbsp;&nbsp;{dev_n}</b>")
            hdr_h.addWidget(hdr_lbl, 1)

            ref_label   = f"Se {dev_lbl}"
            ref_dev_btn = QPushButton(f"↗ {ref_label}")
            ref_dev_btn.setCheckable(True)
            ref_dev_btn.setToolTip(f"Skapar en referensorsak med texten: {ref_label}")
            ref_dev_btn.setStyleSheet(
                "QPushButton{font-size:10px;padding:2px 8px;border:1px solid #17191C;"
                "border-radius:3px;background:transparent;color:#17191C;font-style:italic;}"
                "QPushButton:checked{background:#17191C;color:white;font-style:normal;}"
                "QPushButton:hover:!checked{background:#F5F5F3;}")
            ref_dev_btn.toggled.connect(
                self._make_ref_handler(dev_key, ref_label, None, None, dev_pos))
            hdr_h.addWidget(ref_dev_btn)
            inner_layout.addWidget(hdr_w)

            for cause in causes:
                cid       = cause['id']
                orig      = cause['description']
                inv_text  = invert_cause_text(orig)
                comp_type = cause['comp_type'] or ''
                comp_tag  = cause['comp_tag']  or ''
                c_pos     = global_pos
                global_pos += 1

                row_w = QWidget()
                row_h = QHBoxLayout(row_w)
                row_h.setContentsMargins(12, 1, 4, 1)
                row_h.setSpacing(6)

                num_lbl = QLabel(
                    f"<span style='color:#888;font-family:monospace'>"
                    f"{cause['ref_label']}</span>")
                num_lbl.setFixedWidth(42)
                row_h.addWidget(num_lbl)

                # Equipment icon + tag badge (only if tag is set)
                if comp_tag:
                    icon_px = QPixmap(18, 18)
                    icon_px.fill(Qt.GlobalColor.transparent)
                    _ip = QPainter(icon_px)
                    _draw_equip_icon(_ip, QRect(0, 0, 18, 18), comp_type)
                    _ip.end()
                    icon_lbl = QLabel()
                    icon_lbl.setPixmap(icon_px)
                    row_h.addWidget(icon_lbl)

                    tag_lbl = QLabel(f"<b>{comp_tag}</b>")
                    tag_lbl.setStyleSheet(
                        "color:#17191C; background:#F5F5F3; border-radius:3px;"
                        "padding:0px 4px; font-size:10px;")
                    tag_lbl.setToolTip(comp_type or "Okänd typ")
                    row_h.addWidget(tag_lbl)

                desc_lbl = QLabel(orig)
                desc_lbl.setToolTip(orig)
                row_h.addWidget(desc_lbl, 1)

                ref_btn = QPushButton("Referera")
                ref_btn.setCheckable(True)
                ref_btn.setFixedWidth(72)
                ref_btn.setStyleSheet(
                    "QPushButton{font-size:10px;padding:2px 4px;border:1px solid #17191C;"
                    "border-radius:3px;}"
                    "QPushButton:checked{background:#17191C;color:white;}"
                    "QPushButton:hover:!checked{background:#F5F5F3;}")

                has_inv = inv_text != orig
                inv_btn = QPushButton("Invers")
                inv_btn.setCheckable(has_inv)
                inv_btn.setEnabled(has_inv)
                inv_btn.setFixedWidth(56)
                if has_inv:
                    inv_btn.setToolTip(f"Skapar: {inv_text}")
                    inv_btn.setStyleSheet(
                        "QPushButton{font-size:10px;padding:2px 4px;border:1px solid #8e44ad;"
                        "border-radius:3px;}"
                        "QPushButton:checked{background:#8e44ad;color:white;}"
                        "QPushButton:hover:!checked{background:#e8daef;}")
                else:
                    inv_btn.setToolTip("Ingen invers hittades för denna orsak")
                    inv_btn.setStyleSheet(
                        "QPushButton{font-size:10px;padding:2px 4px;border:1px solid #ccc;"
                        "border-radius:3px;color:#aaa;background:#f5f5f5;}")

                ref_btn.toggled.connect(
                    self._make_ref_handler(cid, orig, inv_btn, cid, c_pos,
                                           comp_type, comp_tag))
                if has_inv:
                    inv_btn.toggled.connect(
                        self._make_inv_handler(cid, inv_text, ref_btn, cid, c_pos,
                                               comp_type, comp_tag))

                row_h.addWidget(ref_btn)
                row_h.addWidget(inv_btn)
                inner_layout.addWidget(row_w)

        inner_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        # ── Summary ───────────────────────────────────────────────────────────
        self._summary_lbl = QLabel("Inga orsaker markerade — tryck 'Hoppa över' för att gå direkt till P&ID.")
        self._summary_lbl.setStyleSheet("color:gray;font-style:italic;font-size:11px;")
        layout.addWidget(self._summary_lbl)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._create_btn = QPushButton("✔ Skapa markerade och fortsätt till P&ID")
        self._create_btn.setEnabled(False)
        self._create_btn.setStyleSheet(
            "QPushButton{background:#27ae60;color:white;border:none;border-radius:4px;"
            "padding:6px 12px;font-weight:bold;}"
            "QPushButton:hover:enabled{background:#2ecc71;}"
            "QPushButton:disabled{background:#aaa;}")
        self._create_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._create_btn, 1)

        skip_btn = QPushButton("Hoppa över →")
        skip_btn.setToolTip("Gå direkt till P&ID utan att skapa orsaker härifrån")
        skip_btn.setStyleSheet(
            "QPushButton{border:1px solid #aaa;border-radius:4px;padding:6px 10px;}"
            "QPushButton:hover{background:#f0f0f0;}")
        skip_btn.clicked.connect(lambda: self.done(self.SKIP))
        btn_row.addWidget(skip_btn)

        cancel_btn = QPushButton("Avbryt")
        cancel_btn.setStyleSheet(
            "QPushButton{border:1px solid #aaa;border-radius:4px;padding:6px 10px;}"
            "QPushButton:hover{background:#f0f0f0;}")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _make_ref_handler(self, cid, description, inv_btn, original_cause_id, sort_pos,
                          comp_type='', comp_tag=''):
        def handler(checked):
            if checked:
                self._selections[cid] = (
                    'ref', description, original_cause_id, sort_pos, comp_type, comp_tag)
                if inv_btn is not None:
                    inv_btn.blockSignals(True)
                    inv_btn.setChecked(False)
                    inv_btn.blockSignals(False)
            else:
                self._selections.pop(cid, None)
            self._update_summary()
        return handler

    def _make_inv_handler(self, cid, inv_text, ref_btn, original_cause_id, sort_pos,
                          comp_type='', comp_tag=''):
        def handler(checked):
            if checked:
                self._selections[cid] = (
                    'inv', inv_text, original_cause_id, sort_pos, comp_type, comp_tag)
                ref_btn.blockSignals(True)
                ref_btn.setChecked(False)
                ref_btn.blockSignals(False)
            else:
                self._selections.pop(cid, None)
            self._update_summary()
        return handler

    def _update_summary(self):
        n = len(self._selections)
        if n == 0:
            self._summary_lbl.setText(
                "Inga orsaker markerade — tryck 'Hoppa över' för att gå direkt till P&ID.")
            self._create_btn.setEnabled(False)
        else:
            kinds = {'ref': 0, 'inv': 0}
            for v in self._selections.values():
                kinds[v[0]] += 1
            parts = []
            if kinds['ref']: parts.append(f"{kinds['ref']} referens")
            if kinds['inv']: parts.append(f"{kinds['inv']} invers")
            self._summary_lbl.setText(f"{n} orsak(er) markerade: {', '.join(parts)}")
            self._create_btn.setEnabled(True)

    def get_selections(self):
        """Return (description, original_cause_id, comp_type, comp_tag) in list order."""
        sorted_vals = sorted(self._selections.values(), key=lambda v: v[3])
        return [(v[1], v[2], v[4] if len(v) > 4 else '',
                 v[5] if len(v) > 5 else '') for v in sorted_vals]


# ── Feature 19: Global search dialog ──────────────────────────────────────────
class GlobalSearchDialog(QDialog):
    """Ctrl+F floating search across all nodes, causes, consequences, safeguards."""
    navigate_requested = pyqtSignal(int, int)   # (type_, id_)

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db
        self.setWindowTitle("Sök i HAZOP-data")
        self.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumWidth(520)
        self.setMinimumHeight(CONFIG['H_PANEL_MAX_ALT'])
        lay = QVBoxLayout(self)
        lay.setSpacing(6); lay.setContentsMargins(8, 8, 8, 8)

        row = QHBoxLayout()
        self._edit = QLineEdit()
        self._edit.setPlaceholderText("Sök i noder, orsaker, konsekvenser, barriärer…")
        self._edit.textChanged.connect(self._search)
        self._edit.setClearButtonEnabled(True)
        row.addWidget(self._edit)
        lay.addLayout(row)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.itemDoubleClicked.connect(self._navigate)
        lay.addWidget(self._list)

        self._count = QLabel('')
        self._count.setStyleSheet("color:#888; font-size:10px;")
        lay.addWidget(self._count)

        close_btn = QPushButton("Stäng")
        close_btn.clicked.connect(self.close)
        lay.addWidget(close_btn)

        self._edit.setFocus()
        self._edit.installEventFilter(self)

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self._edit and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._list.setFocus()
                if self._list.count():
                    self._list.setCurrentRow(0)
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._list.currentItem():
                    self._navigate(self._list.currentItem())
                return True
        return super().eventFilter(obj, event)

    def _search(self, text):
        self._list.clear()
        q = text.strip()
        if len(q) < 2:
            self._count.setText('')
            return
        q_low = q.lower()
        results = []
        for node in self._db.nodes():
            nd = dict(node)
            if q_low in nd['name'].lower():
                results.append((NODE_T, nd['id'], nd['name'], f"🏭 Nod: {nd['name']}"))
            for dev in self._db.deviations(nd['id']):
                for c in self._db.causes_for_deviation(dev['id']):
                    cd = dict(c)
                    if q_low in cd['description'].lower():
                        results.append((CAUSE_T, cd['id'], cd['description'],
                                        f"⚙ {nd['name']} / {dev['description'][:30]}: {cd['description']}"))
                    for cons in self._db.consequences(cd['id']):
                        kd = dict(cons)
                        if q_low in kd['description'].lower():
                            results.append((CONS_T, kd['id'], kd['description'],
                                            f"⚠ {nd['name']} / {cd['description'][:25]}: {kd['description']}"))
                        for sg in self._db.safeguards(kd['id']):
                            sd = dict(sg)
                            if q_low in sd['description'].lower():
                                results.append((SG_T, sd['id'], sd['description'],
                                                f"🛡 {nd['name']} / {cd['description'][:20]}: {sd['description']}"))
            if len(results) > 200:
                break
        for type_, id_, _, label in results[:200]:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, type_)
            item.setData(Qt.ItemDataRole.UserRole + 1, id_)
            self._list.addItem(item)
        self._count.setText(f"{min(len(results),200)} träffar" + (" (begränsat till 200)" if len(results)>200 else ""))

    def _navigate(self, item):
        if item is None: return
        type_ = item.data(Qt.ItemDataRole.UserRole)
        id_   = item.data(Qt.ItemDataRole.UserRole + 1)
        if type_ is not None and id_ is not None:
            self.navigate_requested.emit(type_, id_)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN WINDOW
# ══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self, hzp_path: str = None):
        super().__init__()
        self._hzp_path    = None   # path to the currently open .hzp file (or None)
        self._search_dialog = None
        self.db = Database()
        load_matrix(self.db)
        self._markup_undo_stack  = []
        self.resize(1440, 900)

        # ── Menu bar ──────────────────────────────────────────────────────────
        # Former toolbar row folded into menus, grouped by purpose:
        #   Fil          — project + Skriv ut
        #   Export       — Excel / PDF / Åtgärder
        #   Analys       — Risk Scenario / Statistik / Godkänn
        #   Inställningar — Mörkt läge
        # Node/deviation/delete actions moved to a button row in TreePanel
        # instead (see TreePanel.__init__), since they act on the tree
        # selection rather than the document as a whole.
        mb = self.menuBar()
        file_menu = mb.addMenu("Fil")
        file_menu.addAction("📄 Nytt projekt",      self._hzp_new)
        file_menu.addAction("📂 Öppna (.hzp)…",     self._hzp_open)
        file_menu.addSeparator()
        self._act_save = file_menu.addAction("💾 Spara",         self._hzp_save)
        file_menu.addAction("💾 Spara som…",         self._hzp_save_as)
        file_menu.addSeparator()
        file_menu.addAction("🖨 Skriv ut",           self._print_scenario_table)
        file_menu.addSeparator()
        file_menu.addAction("❌ Avsluta",            self.close)

        export_menu = mb.addMenu("Export")
        export_menu.addAction("📊 Excel",           self._export_excel)
        export_menu.addAction("📄 PDF",             self._export_pdf)
        export_menu.addAction("📋 Åtgärder",        self._export_actions_pdf)

        analysis_menu = mb.addMenu("Analys")
        analysis_menu.addAction("🔀 Risk Scenario", self._open_risk_scenario_wizard)
        analysis_menu.addAction("📈 Statistik",     self._show_statistics)
        analysis_menu.addAction("✅ Godkänn",       self._approve_node)

        settings_menu = mb.addMenu("Inställningar")
        settings_menu.addAction("🌙 Mörkt läge",    self._toggle_dark_mode)

        self._update_title()

        # ── Status bar ────────────────────────────────────────────────────────
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(f"Databas: {self.db.path}")

        # ── Central widget ────────────────────────────────────────────────────
        # Outer vertical splitter: top pane holds [nav rail | view_stack],
        # bottom pane holds the scenario/markup tables (P&ID page only). The
        # bottom pane spans the FULL window width — it is a sibling of the
        # top pane, not nested inside it — so the nav rail only extends as
        # far down as the tree/P&ID area, not behind the scenario table too.
        self._outer_splitter = QSplitter(Qt.Orientation.Vertical)
        self.setCentralWidget(self._outer_splitter)

        top_widget = QWidget()
        root_layout = QHBoxLayout(top_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Nav rail — narrow vertical strip of square icon buttons, far left
        toggle_bar = QWidget()
        toggle_bar.setFixedWidth(52)
        toggle_bar.setStyleSheet("background:#17191C;")
        toggle_lay = QVBoxLayout(toggle_bar)
        toggle_lay.setContentsMargins(6, 12, 6, 12)
        toggle_lay.setSpacing(8)

        self.btn_pid       = QPushButton("🗺")
        self.btn_sheet     = QPushButton("📋")
        self.btn_equip     = QPushButton("🔩")
        self.btn_admin     = QPushButton("⚙")
        self.btn_settings  = QPushButton("🔧")

        _nav_labels = {
            self.btn_pid:      "P&ID-vy",
            self.btn_sheet:    "Worksheet",
            self.btn_equip:    "Utrustning",
            self.btn_admin:    "Studiehantering",
            self.btn_settings: "Inställningar",
        }

        for btn in (self.btn_pid, self.btn_sheet, self.btn_equip,
                    self.btn_admin, self.btn_settings):
            btn.setCheckable(True)
            btn.setFixedSize(40, 40)
            btn.setToolTip(_nav_labels[btn])
            btn.setStyleSheet(
                "QPushButton{color:#fff;background:#2A2E34;border:none;"
                "border-radius:6px;font-size:16px;}"
                "QPushButton:hover{background:#383D45;}"
                "QPushButton:checked{background:#fff;color:#17191C;}")
            toggle_lay.addWidget(btn, alignment=Qt.AlignmentFlag.AlignHCenter)

        toggle_lay.addStretch()
        root_layout.addWidget(toggle_bar)

        # View stack (init BEFORE setChecked to prevent signal before object exists)
        self.view_stack = QStackedWidget()
        root_layout.addWidget(self.view_stack, 1)

        self._outer_splitter.addWidget(top_widget)

        self.btn_pid.setChecked(True)
        self.btn_pid.clicked.connect(lambda: self._switch_view(0))
        self.btn_sheet.clicked.connect(lambda: self._switch_view(1))
        self.btn_equip.clicked.connect(lambda: self._switch_view(2))
        self.btn_admin.clicked.connect(lambda: self._switch_view(3))
        self.btn_settings.clicked.connect(lambda: self._switch_view(4))

        # ── Page 0: P&ID view ─────────────────────────────────────────────────
        # No wrapper widget here — the view_stack page IS _h_splitter directly.
        # The scenario/markup tables that used to share a vertical splitter
        # with _h_splitter now live in self._v_splitter, added as the outer
        # splitter's own bottom pane (see below) so they span the full window
        # width instead of being indented by the nav rail.
        self._h_splitter = QSplitter(Qt.Orientation.Horizontal)

        self.tree_panel = TreePanel(self.db)
        self.tree_panel.setMinimumWidth(220)
        self.tree_panel.setMaximumWidth(340)
        self._h_splitter.addWidget(self.tree_panel)

        self.pid_panel = PIDPanel(self.db)
        self.pid_panel.setMinimumWidth(400)
        self._h_splitter.addWidget(self.pid_panel)

        # Right panel replaced with narrow PropertiesRibbon (feature request)
        # Keep the old panels instantiated so existing signal wiring still compiles,
        # but they are not shown in the splitter.
        self.welcome_panel    = WelcomePanel()
        self.node_panel       = NodePanel(self.db)
        self.cons_panel       = ConsequencePanel(self.db)
        self.sg_panel         = SafeguardPanel(self.db)
        # Dummy stack kept so existing code that calls self.stack.setCurrentWidget() works
        self.stack = QStackedWidget()
        self._right_scroll = QScrollArea()   # kept for _reload_all_panels compatibility
        for panel in [self.welcome_panel, self.node_panel,
                      self.cons_panel, self.sg_panel]:
            self.stack.addWidget(panel)

        # Narrow properties ribbon
        self.props_ribbon = PropertiesRibbon(self.db, main_window=self)
        self.props_ribbon.item_changed.connect(self._on_props_changed)
        self._h_splitter.addWidget(self.props_ribbon)

        self.node_markup_panel = NodeMarkupPanel(self.db)
        self.node_markup_panel.setVisible(False)
        self._h_splitter.addWidget(self.node_markup_panel)

        self.red_markup_panel = RedMarkupPanel(self.db)
        self.red_markup_panel.setVisible(False)
        self._h_splitter.addWidget(self.red_markup_panel)

        self._h_splitter.setSizes([260, 650, 62, 0, 0])
        self.view_stack.addWidget(self._h_splitter)

        # Bottom pane of the OUTER splitter (full window width, below the
        # nav rail) — scenario table + the two markup-edit tables that swap
        # in during edit modes. Hidden entirely on non-P&ID pages (see
        # _switch_view) so those pages use the whole window height.
        self._v_splitter = QSplitter(Qt.Orientation.Vertical)

        self.scenario_panel = ScenarioTablePanel(self.db)
        # Same opt-in HAZOPWorksheet already uses (see always_show_deviation_column
        # docstring) — needed so load_node() from the equipment bar shows WHICH
        # deviation/equipment each row belongs to, not just the causes themselves.
        self.scenario_panel.always_show_deviation_column()
        self._v_splitter.addWidget(self.scenario_panel)

        self.markup_table_panel = MarkupTablePanel(self.db)
        self.markup_table_panel.setVisible(False)
        self._v_splitter.addWidget(self.markup_table_panel)

        self.red_markup_table_panel = RedMarkupTablePanel(self.db)
        self.red_markup_table_panel.setVisible(False)
        self._v_splitter.addWidget(self.red_markup_table_panel)

        self._v_splitter.setSizes([220, 0, 0])
        self._outer_splitter.addWidget(self._v_splitter)
        self._outer_splitter.setSizes([640, 220])

        # ── Page 1: Worksheet ─────────────────────────────────────────────────
        self.worksheet = HAZOPWorksheet(self.db, main_window=self)
        self.view_stack.addWidget(self.worksheet)

        # ── Page 2: Equipment ─────────────────────────────────────────────────
        self.equipment_panel = EquipmentPanel(self.db)
        self.equipment_panel.markers_saved.connect(self.pid_panel.reload_overlays)
        self.view_stack.addWidget(self.equipment_panel)

        # ── Page 3: Study management ──────────────────────────────────────────
        self.admin_panel = StudyManagementPanel(self.db)
        self.view_stack.addWidget(self.admin_panel)

        # ── Page 4: Settings ──────────────────────────────────────────────────
        self.settings_panel = SettingsPanel(self.db)
        self.settings_panel.matrix_changed.connect(self._on_matrix_changed)
        self.view_stack.addWidget(self.settings_panel)

        # ── Undo shortcut (Ctrl+Z) — only active during markup editing ────────
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self._undo_shortcut.setEnabled(False)
        self._undo_shortcut.activated.connect(self._undo_last_markup)

        # ── Global search Ctrl+F (feature 19) ────────────────────────────────
        _search_sc = QShortcut(QKeySequence("Ctrl+F"), self)
        _search_sc.activated.connect(self._open_global_search)
        self._search_dialog = None

        # ── Wire signals ──────────────────────────────────────────────────────
        self.tree_panel.item_selected.connect(self._on_selected)
        self.tree_panel.structure_changed.connect(self._on_structure_changed)
        self.tree_panel.visibility_changed.connect(
            lambda t, v: self.pid_panel.viewer.set_marker_visibility(t, v))

        self.node_panel.saved.connect(
            lambda id_, name: (
                self.tree_panel.refresh(NODE_T, id_),
                self.db.sync_node_text_markups(id_, name),
                self.pid_panel.refresh_markup_overlays(),
            ))
        self.cons_panel.saved.connect(
            lambda id_: (self.tree_panel.refresh(CONS_T, id_),
                         self.scenario_panel.refresh_placed()))
        self.sg_panel.saved.connect(
            lambda id_: self.tree_panel.refresh(SG_T, id_))

        self.cons_panel.place_on_pid.connect(
            lambda: self.pid_panel._set_mode(MODE_CONSEQUENCE))
        self.sg_panel.place_on_pid.connect(
            lambda: self.pid_panel._set_mode(MODE_SAFEGUARD))

        self.scenario_panel.item_selected.connect(self._on_scenario_item_selected)
        self.scenario_panel.new_item_created.connect(
            lambda type_, id_: (
                # emit_selection=False: the explicit scenario_panel.refresh()
                # right after already rebuilds the table for the new item, so
                # letting refresh()'s setCurrentItem cascade via
                # currentItemChanged -> _on_select -> item_selected ->
                # _on_selected would trigger a second, redundant
                # scenario_panel._rebuild() (same anti-pattern fixed in
                # _on_marker_navigate, commit 84c8b7c). This path is hit when
                # quick-adding a cause/consequence/safeguard directly from the
                # scenario table (e.g. via Enter-to-add-next-row), so a
                # redundant rebuild here can race with a cell editor's
                # focus-out mid-edit-commit.
                self.tree_panel.refresh(type_, id_, emit_selection=False),
                self.scenario_panel.refresh(),
                # Land the editing cursor on the new item instead of leaving
                # selection wherever the rebuild happened to reset it to --
                # otherwise the user's view visibly "jumps away" from the row
                # they were just working on, making it hard to keep typing
                # the next cause/consequence in one flow.
                self.scenario_panel.select_item(type_, id_)))
        self.scenario_panel.item_edited.connect(self._on_scenario_item_edited)
        self.scenario_panel.place_requested.connect(self._on_scenario_place_requested)
        self.scenario_panel.navigate_to_pid.connect(self._on_scenario_navigate_to_pid)
        self.scenario_panel.remove_requested.connect(self._on_scenario_remove_from_pid)
        self.scenario_panel.add_causes_on_pid_requested.connect(self._on_add_causes_on_pid)
        self.scenario_panel.structure_changed.connect(
            lambda: (self.tree_panel.refresh(), self.pid_panel.reload_overlays()))

        self.tree_panel.equipment_dropped_on_deviation.connect(
            self._on_equipment_dropped_on_deviation)
        self.tree_panel.add_causes_on_pid_requested.connect(self._on_add_causes_on_pid_tree)
        self.tree_panel.add_consequences_on_pid_requested.connect(
            self._on_add_consequences_on_pid)
        self.tree_panel.add_safeguards_on_pid_requested.connect(
            self._on_add_safeguards_on_pid)
        self.tree_panel.edit_node_markup_requested.connect(self._on_edit_node_markup)
        self.tree_panel.edit_red_markup_requested.connect(self._on_edit_red_markup)
        self.tree_panel.node_markup_vis_requested.connect(self._on_node_markup_vis)
        self.tree_panel.node_jump_to_markup.connect(self._on_jump_to_node_markup)

        # Node markup ribbon signals
        self.node_markup_panel.closed.connect(self._on_close_node_markup)
        self.node_markup_panel.tool_changed.connect(
            lambda t: self.pid_panel.set_markup_tool(
                t, *self.node_markup_panel.get_current_style()[:3]))
        self.node_markup_panel.all_vis_toggled.connect(
            lambda _: self.pid_panel.refresh_markup_overlays())
        self.node_markup_panel.style_changed.connect(
            lambda color, opacity, width: self.pid_panel.viewer.set_pen_style(
                color, width, int(opacity * 210)))
        self.node_markup_panel.snap_changed.connect(
            self.pid_panel.viewer.set_snap)
        self.node_markup_panel.navigate_node_requested.connect(self._on_edit_node_markup)
        # Red markup ribbon signals
        self.red_markup_panel.closed.connect(self._on_close_red_markup)
        self.red_markup_panel.tool_changed.connect(
            lambda t: self.pid_panel.set_red_markup_tool(
                t, *self.red_markup_panel.get_current_style()[:3],
                symbol_id=self.red_markup_panel.get_selected_symbol()))
        self.red_markup_panel.symbol_selected.connect(
            lambda sid: self.pid_panel.set_red_markup_tool(
                'symbol', *self.red_markup_panel.get_current_style()[:3],
                symbol_id=sid))
        self.red_markup_panel.all_vis_toggled.connect(
            lambda _: self.pid_panel.refresh_red_markup_overlays())
        self.red_markup_panel.style_changed.connect(
            lambda color, opacity, width: self.pid_panel.viewer.set_pen_style(
                color, width, int(opacity * 210)))
        self.red_markup_panel.snap_changed.connect(
            self.pid_panel.viewer.set_snap)
        # Markup table panel signals
        self.markup_table_panel.item_deleted.connect(
            lambda _: self.pid_panel.refresh_markup_overlays())
        self.markup_table_panel.item_vis_toggled.connect(
            lambda mu_id, vis: self.pid_panel.viewer.set_markup_item_visible(mu_id, vis))
        self.markup_table_panel.item_selected.connect(
            lambda mu_id: self.pid_panel.viewer.highlight_markup(mu_id))
        # Red markup table panel signals
        self.red_markup_table_panel.item_deleted.connect(
            lambda _: self.pid_panel.refresh_red_markup_overlays())
        self.red_markup_table_panel.item_vis_toggled.connect(
            lambda mu_id, vis: self.pid_panel.viewer.set_red_markup_item_visible(mu_id, vis))
        self.red_markup_table_panel.item_selected.connect(
            lambda mu_id: self.pid_panel.refresh_red_markup_overlays())
        self.red_markup_table_panel.item_style_changed.connect(
            lambda _: self.pid_panel.refresh_red_markup_overlays())

        self.pid_panel.markup_label_edited.connect(self._on_markup_label_edited)
        self.pid_panel.markup_duplicate_requested.connect(self._on_duplicate_markup)
        self.markup_table_panel.item_style_changed.connect(
            lambda _: self.pid_panel.refresh_markup_overlays())
        self.markup_table_panel.item_duplicated.connect(self._on_duplicate_markup)
        self.pid_panel.markup_draw_finished.connect(self._on_markup_draw_finished)
        self.pid_panel.markup_moved.connect(self._on_markup_moved)
        self.pid_panel.markup_item_selected.connect(
            self.markup_table_panel.select_markup)
        self.pid_panel.red_markup_draw_finished.connect(self._on_red_markup_draw_finished)
        self.pid_panel.red_markup_moved.connect(self._on_red_markup_moved)
        self.pid_panel.red_markup_item_selected.connect(
            self.red_markup_table_panel.select_markup)
        self.pid_panel.markup_symbol_dims_changed.connect(self._on_markup_symbol_dims_changed)
        self.pid_panel.board_layout_changed.connect(self._on_board_layout_changed)
        self.tree_panel.exit_pid_mode_requested.connect(
            lambda: self.pid_panel._set_mode(MODE_NAV))

        self.pid_panel.node_created.connect(
            # emit_selection=False: _on_selected() is called explicitly right
            # after, so refresh()'s setCurrentItem must not also cascade via
            # currentItemChanged -> _on_select -> item_selected ->
            # _on_selected (same anti-pattern fixed in _on_marker_navigate,
            # commit 84c8b7c) — that would double-fire scenario_panel.load_node()
            # and schedule zoom_to_node() twice.
            lambda nid: (self.tree_panel.refresh(NODE_T, nid, emit_selection=False),
                         self._on_selected(NODE_T, nid)))
        self.pid_panel.cause_created.connect(
            lambda cid: (self.tree_panel.refresh(CAUSE_T, cid),
                         self.scenario_panel.refresh_placed()))
        def _on_consequence_created(cid):
            logging.info('consequence_created: cid=%s — entering handler', cid)
            try:
                logging.info('consequence_created: step 1 — calling _open_consequence_step_picker')
                self._open_consequence_step_picker(cid)
                logging.info('consequence_created: step 2 — picker returned, scheduling deferred refresh')
                def _deferred(c=cid):
                    logging.info('consequence_created: deferred step — tree_panel.refresh(%s)', c)
                    try:
                        # emit_selection=False: if the dialog was accepted,
                        # _open_consequence_step_picker() above already called
                        # scenario_panel._rebuild() directly. Letting this
                        # refresh()'s setCurrentItem cascade via
                        # currentItemChanged -> _on_select -> item_selected ->
                        # _on_selected would trigger a second, redundant
                        # scenario_panel._rebuild() on the next event-loop tick
                        # (same anti-pattern fixed in _on_marker_navigate,
                        # commit 84c8b7c) — extra rebuild volume that raises
                        # the odds of racing a cell editor's focus-out.
                        self.tree_panel.refresh(CONS_T, c, emit_selection=False)
                        logging.info('consequence_created: deferred step — tree refresh done')
                        self.scenario_panel.refresh_placed()
                        logging.info('consequence_created: deferred step — refresh_placed done')
                    except Exception:
                        logging.exception('consequence_created: CRASH in deferred refresh')
                QTimer.singleShot(0, _deferred)
                logging.info('consequence_created: handler done (deferred refresh scheduled)')
            except Exception:
                logging.exception('consequence_created: CRASH in handler')
        self.pid_panel.consequence_created.connect(_on_consequence_created)
        self.pid_panel.ref_tag_picked.connect(self._on_ref_tag_picked)
        self.pid_panel.safeguard_created.connect(self._on_safeguard_created)
        self.pid_panel.existing_marker_placed.connect(self._on_existing_marker_placed)
        def _on_cause_template_created(cid):
            # emit_selection=False + explicit load_node (2026-08-07
            # follow-up — same "kan inte lägga till konsekvens" bug class
            # as _on_equipment_deviation_created, but triggered by CAUSE
            # creation this time, not deviation creation): refresh(CAUSE_T,
            # cid) used to run WITHOUT emit_selection=False, cascading into
            # _on_selected(CAUSE_T, cid) -> scenario_panel.load_deviation(...)
            # right as the user's very next move (picking a cause from
            # EquipmentDeviationBar's chip/dropdown, then immediately
            # clicking that row's KON cell to type) lands — narrowing/
            # rebuilding the worksheet mid-interaction. load_node() instead
            # keeps every deviation under the node visible, same as
            # _on_equipment_deviation_created already does.
            self.tree_panel.refresh(CAUSE_T, cid, emit_selection=False)
            cause = self.db.get_cause(cid)
            node_id = cause.get('node_id') if cause else None
            if node_id is not None:
                self.pid_panel.set_active_cause(cid)
                self.scenario_panel.load_node(node_id)
            self.scenario_panel.refresh_placed()
            # select_cause() itself refuses to steal the current cell from
            # an active edit / a row the user already navigated to (see
            # ScenarioTablePanel.select_cause) — this deferral just lets
            # the rebuild above settle before it scans _row_meta.
            QTimer.singleShot(0, lambda c=cid: self.scenario_panel.select_cause(c))
            # Refresh Smart Recognition panel so new learning is immediately visible
            try:
                self.settings_panel._tag_memory_panel.refresh()
            except Exception:
                pass
        self.pid_panel.cause_template_created.connect(_on_cause_template_created)
        self.pid_panel.cause_placement_requested.connect(self._on_cause_placement_requested)
        self.pid_panel.equipment_placement_requested.connect(self._on_equipment_placement_requested)
        self.pid_panel.risk_scenario_requested.connect(self._on_pid_risk_scenario)
        self.pid_panel.marker_navigated.connect(self._on_marker_navigate)
        self.pid_panel.equipment_deviation_created.connect(self._on_equipment_deviation_created)
        self.pid_panel.pid_analysis_done.connect(self._on_pid_analysis_done)
        self.admin_panel._pid_mgmt.sheets_changed.connect(self._on_sheets_changed)

        self._cur_type = None
        self._cur_id   = None

        self.tree_panel.refresh()
        self.pid_panel.try_reload_pdf()

    def _switch_view(self, page):
        prev = self.view_stack.currentIndex()
        self.view_stack.setCurrentIndex(page)
        # Bottom pane (scenario/markup tables) only makes sense on the P&ID
        # page — hidden elsewhere so those pages use the full window height
        # instead of leaving an empty strip below the nav rail's height.
        self._v_splitter.setVisible(page == 0)
        self.btn_pid.setChecked(page == 0)
        self.btn_sheet.setChecked(page == 1)
        self.btn_equip.setChecked(page == 2)
        self.btn_admin.setChecked(page == 3)
        self.btn_settings.setChecked(page == 4)
        if page == 0 and prev != 0:
            self.pid_panel.reload_overlays()
        if page == 1: self.worksheet.refresh()
        if page == 2: self.equipment_panel.refresh()
        if page == 3:
            self.admin_panel.refresh()
            self.admin_panel.refresh_pid()
        if page == 4:
            # Guard against settings_panel or _tag_memory_panel not being initialized
            if (hasattr(self, 'settings_panel') and self.settings_panel and
                hasattr(self.settings_panel, '_tag_memory_panel') and
                self.settings_panel._tag_memory_panel):
                self.settings_panel._tag_memory_panel.refresh()

    def _on_props_changed(self):
        """PropertiesRibbon saved a field — refresh tree + scenario."""
        if self._cur_type is not None and self._cur_id is not None:
            # emit_selection=False: the explicit scenario_panel._rebuild()
            # below already rebuilds the table for the current item, so
            # letting refresh()'s setCurrentItem cascade via
            # currentItemChanged -> _on_select -> item_selected ->
            # _on_selected would trigger a second, redundant
            # scenario_panel._rebuild() (same anti-pattern fixed in
            # _on_marker_navigate, commit 84c8b7c). This handler fires on
            # every properties-field save, so it is a frequent path.
            self.tree_panel.refresh(self._cur_type, self._cur_id, emit_selection=False)
        self.scenario_panel._rebuild()

    def _on_scenario_item_selected(self, type_, id_):
        """Scenario table cell click — update ribbon only; do NOT change scenario filter."""
        self._cur_type = type_
        self._cur_id   = id_
        self.props_ribbon.set_item(type_, id_)
        if type_ == CAUSE_T:
            self.pid_panel.set_active_cause(id_)
        elif type_ == CONS_T:
            self.pid_panel.set_active_consequence(id_)

    def _on_selected(self, type_, id_):
        self._cur_type = type_
        self._cur_id   = id_
        self.props_ribbon.set_item(type_, id_)
        if type_ == NODE_T:
            self.pid_panel.set_active_node(id_)
            self.scenario_panel.load_node(id_)
            if self.view_stack.currentIndex() == 0:
                QTimer.singleShot(80, lambda nid=id_: self.zoom_to_node(nid))
        elif type_ == DEV_T:
            self.pid_panel.set_active_deviation(id_)
            self.scenario_panel.load_deviation(id_)
        elif type_ == CAUSE_T:
            self.pid_panel.set_active_cause(id_)
            _row = self.db.get_cause(id_); cause = dict(_row) if _row else None
            if cause and cause.get('deviation_id'):
                self.scenario_panel.load_deviation(cause['deviation_id'])
            else:
                self.scenario_panel.load_cause(id_)
        elif type_ == CONS_T:
            self.pid_panel.set_active_consequence(id_)
            _cons = self.db.get_consequence(id_)
            cons = dict(_cons) if _cons else None
            if cons:
                _cause = self.db.get_cause(cons['cause_id']) if cons.get('cause_id') else None
                cause = dict(_cause) if _cause else None
                if cause and cause.get('deviation_id'):
                    self.scenario_panel.load_deviation(cause['deviation_id'])
                else:
                    self.scenario_panel.load_consequence(id_)
            else:
                self.scenario_panel.load_consequence(id_)
        elif type_ == SG_T:
            sg = self.db.get_safeguard(id_)
            if sg:
                cons = self.db.get_consequence(sg['consequence_id'])
                if cons:
                    self.pid_panel.set_active_consequence(cons['id'])
                    _cr = self.db.get_cause(cons['cause_id']) if cons.get('cause_id') else None
                    cause = dict(_cr) if _cr else None
                    if cause and cause.get('deviation_id'):
                        self.scenario_panel.load_deviation(cause['deviation_id'])
                    else:
                        self.scenario_panel.load_consequence(cons['id'])

    def _on_scenario_item_edited(self, type_, id_):
        """Scenario table committed an edit — sync tree and P&ID labels.

        emit_selection=False: without it, tree_panel.refresh()'s
        setCurrentItem cascades via currentItemChanged -> _on_select ->
        item_selected -> _on_selected, which reloads the scenario panel and
        triggers a SECOND, redundant _rebuild() on every single cell edit
        (same anti-pattern already fixed for _on_marker_navigate,
        _on_safeguard_created, _on_props_changed and node_created). Besides
        the redundant work, this is what made the table visibly "jump away"
        after committing an edit -- the extra rebuild reset the current
        cell/selection a second time on top of whatever the first rebuild
        already did.
        """
        self.tree_panel.refresh(type_, id_, emit_selection=False)
        self.pid_panel.reload_overlays()

    def _on_structure_changed(self):
        self._cur_type = None
        self._cur_id   = None
        self.stack.setCurrentWidget(self.welcome_panel)
        self.scenario_panel.clear()
        self.pid_panel.reload_overlays()

    def _on_sheets_changed(self):
        """Reload the study board after sheets are added or deleted."""
        self.pid_panel.try_reload_pdf()

    def _on_pid_analysis_done(self):
        """Switch to Settings → Identifierade objekt after P&ID analysis,
        then offer to chain straight into '🎯 Hitta objekt på P&ID' (2026-08-11):
        'Efter jag klickat på analysera P&ID vill jag få upp samma popupruta
        som innan, sedan vill jag att en popupfråga om jag vill hitta objekt
        på P&ID ska komma upp. Då ska samma körning som "hitta objekt på
        P&ID" knappen köras.' The 'Analys klar' popup itself is unchanged
        (shown earlier, in PIDPanel._analyze_pid, before this signal fires);
        this only adds the follow-up confirm + chained run."""
        self._switch_view(4)   # Settings page
        self.settings_panel.analysis_panel.refresh()
        # Switch to the "Identifierade objekt" tab inside settings
        tabs = self.settings_panel.findChild(QTabWidget)
        if tabs:
            for i in range(tabs.count()):
                if "Identifierade" in tabs.tabText(i):
                    tabs.setCurrentIndex(i)
                    break

        reply = QMessageBox.question(
            self, "Hitta objekt på P&ID",
            "Vill du köra 🎯 Hitta objekt på P&ID nu, med de tagg-prefix "
            "som just hittades?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if reply == QMessageBox.StandardButton.Yes:
            # Same run as clicking the button itself — refresh the register
            # model first so it reflects the tags the analysis just wrote.
            self.equipment_panel.refresh()
            self.equipment_panel._autodetect()

    def _on_marker_navigate(self, item_type: str, item_id: int):
        """Navigate tree and detail panel when a P&ID marker is clicked."""
        if item_type == 'equipment':
            # item_id here is equipment_markers.id (the marker row), not
            # equipment_catalog.id — look up which register row it links to.
            self._on_equipment_marker_navigate(item_id)
            return
        if item_type == 'equipment_register':
            # From EquipmentDeviationBar's "Visa i register" link — item_id
            # here IS already equipment_catalog.id (the bar knows it
            # directly, no marker lookup needed).
            self._switch_view(2)
            self.equipment_panel.select_row_by_equipment_id(item_id)
            return
        type_map = {'cause': CAUSE_T, 'consequence': CONS_T, 'safeguard': SG_T}
        t = type_map.get(item_type)
        if t is None:
            return
        # emit_selection=False: avoid double-firing _on_selected (and its
        # downstream scenario_panel._rebuild()) — setCurrentItem's cascade via
        # currentItemChanged is suppressed here since we call _on_selected
        # explicitly right below.
        self.tree_panel.refresh(t, item_id, emit_selection=False)
        self._on_selected(t, item_id)

    def _on_equipment_marker_navigate(self, marker_id: int):
        """Clicking an already-placed (green) equipment marker on the P&ID
        should surface the HAZOP scenario rows that mention it — causes,
        consequences AND safeguards — the same way the worksheet looks right
        after a cause was just created (2026-08-11, 'Om jag har lagt till
        ett objekt på P&ID ... och klickar på det igen så vill jag att
        orsakerna där det nämns dyker upp i hazop scenario ... Detta gäller
        även om de är tillagda på konsekvens och safeguard').

        Equipment only has a HAZOP tree node once it has been linked to one
        (equipment_catalog.node_id, set via 'Nod ↔ Utrustning', see
        Database.set_equipment_node). When that link exists we mirror
        _on_equipment_deviation_created's own sequence exactly: refresh the
        tree for that node and call scenario_panel.load_node(node_id),
        which — unlike load_cause()/load_consequence() — pulls in every
        deviation/cause/consequence/safeguard under the node together, so a
        tag mentioned on a consequence or a safeguard shows up just as well
        as one mentioned on a cause. No _switch_view() call is needed: the
        marker can only be clicked while already on the P&ID page, and the
        scenario table is the bottom pane of that same page.

        If the equipment has no node yet, there is nothing to show in the
        worksheet — keep the old "select the row in Utrustningsregistret"
        behaviour as a fallback so the click still does something useful.
        """
        row = self.db.conn.execute(
            "SELECT equipment_id FROM equipment_markers WHERE id=?", (marker_id,)).fetchone()
        if not row or row['equipment_id'] is None:
            return
        equipment_id = row['equipment_id']
        node_id = self.db.equipment_node_id(equipment_id)
        if node_id is not None:
            self.tree_panel.refresh(NODE_T, node_id, emit_selection=False)
            self.scenario_panel.load_node(node_id)
            self.scenario_panel.refresh_placed()
            return
        self._switch_view(2)   # Utrustning page — fallback, no node to show a worksheet for
        self.equipment_panel.select_row_by_equipment_id(equipment_id)

    def _on_equipment_deviation_created(self, deviation_id, equipment_id):
        """A deviation was checked on in EquipmentDeviationBar — refresh the
        tree (new LEDORD_T/EQUIP_T/DEV_T items) and worksheet.

        emit_selection=False + explicit load_node() (2026-08-07 fix, real
        crash-adjacent bug report: "kan inte lägga till konsekvens"): the
        original version called tree_panel.refresh(DEV_T, deviation_id)
        with the default emit_selection=True, which cascades into
        _on_selected(DEV_T, ...) -> scenario_panel.load_deviation(...) —
        narrowing the worksheet to just THIS ONE deviation on every single
        checkbox toggle. That hid sibling deviations for the same
        node/equipment (the user's "I want to see BOTH deviations"
        complaint) and left keyboard focus inside the bar's comboboxes, so
        the worksheet's Enter-to-add-consequence shortcut never fired
        (the user's "I can't add a consequence" complaint) — same
        emit_selection anti-pattern already fixed elsewhere per commit
        84c8b7c (see _on_props_changed, _on_safeguard_created,
        _on_marker_navigate). load_node() shows every deviation under the
        node together instead, exactly like clicking the node itself.
        """
        node_id = self.db.equipment_node_id(equipment_id)
        self.tree_panel.refresh(DEV_T, deviation_id, emit_selection=False)
        if node_id is not None:
            self.scenario_panel.load_node(node_id)
        self.scenario_panel.refresh_placed()

    def _on_safeguard_created(self, _sg_id):
        if self._cur_type == CONS_T and self._cur_id is not None:
            self.scenario_panel.load_consequence(self._cur_id)
            # emit_selection=False: load_consequence() above already rebuilt the
            # scenario panel for this item — letting refresh()'s setCurrentItem
            # cascade via currentItemChanged -> _on_select -> item_selected ->
            # _on_selected would redundantly call load_consequence() a second
            # time (same anti-pattern fixed in _on_marker_navigate, commit
            # 84c8b7c). The tree's current-item highlight is still updated
            # inside refresh() while signals are blocked, so nothing is lost.
            self.tree_panel.refresh(CONS_T, self._cur_id, emit_selection=False)
        self.scenario_panel.refresh_placed()

    def _on_scenario_place_requested(self, type_, id_):
        """User clicked red pin or context menu 'Lägg till på P&ID' — fast path, no panel reload."""
        # Set only what the P&ID panel needs; skip full _on_selected to avoid heavy reloads
        if type_ == CAUSE_T:
            self.pid_panel.set_active_cause(id_)
        elif type_ == CONS_T:
            self.pid_panel.set_active_consequence(id_)
        elif type_ == SG_T:
            sg = self.db.get_safeguard(id_)
            if sg:
                self.pid_panel.set_active_consequence(sg['consequence_id'])
        self._switch_view(0)
        type_str = {CAUSE_T: 'cause', CONS_T: 'consequence', SG_T: 'safeguard'}.get(type_)
        if type_str:
            self.pid_panel.start_place_existing(type_str, id_)

    def _on_scenario_navigate_to_pid(self, type_, id_):
        """User clicked green pin — switch to P&ID view and zoom to the marker."""
        marker = None
        if type_ == CAUSE_T:
            marker = self.db.get_cause_marker(id_)
        elif type_ == CONS_T:
            marker = self.db.get_consequence_marker(id_)
        elif type_ == SG_T:
            marker = self.db.get_safeguard_marker(id_)
        if not marker:
            return
        self._on_selected(type_, id_)
        self._switch_view(0)
        self.pid_panel.navigate_to_marker(marker['pid_page'], marker['x'], marker['y'])

    def _on_scenario_remove_from_pid(self, type_, id_):
        """Context menu 'Ta bort från P&ID' — delete all markers for this item."""
        type_str = {CAUSE_T: 'cause', CONS_T: 'consequence', SG_T: 'safeguard'}.get(type_)
        if not type_str:
            return
        self.pid_panel.remove_existing_marker(type_str, id_)
        self.scenario_panel.refresh_placed()
        self.tree_panel.refresh()

    def _on_add_causes_on_pid(self, deviation_id):
        """Red-pin click in scenario table → enter P&ID cause-placement mode directly."""
        dev = self.db.get_deviation(deviation_id)
        if not dev:
            return
        self.pid_panel.set_active_node(dev['node_id'])
        self._switch_view(0)
        self.pid_panel.start_cause_template_mode(deviation_id)

    def _on_add_causes_on_pid_tree(self, deviation_id):
        """Right-click deviation in tree → show reuse dialog, then enter P&ID mode."""
        dev = self.db.get_deviation(deviation_id)
        if not dev:
            return
        node_id  = dev['node_id']
        dev_name = dev['description']

        # Build hierarchical reference labels for causes from other deviations
        all_nodes    = self.db.nodes()
        node_idx     = next((i + 1 for i, n in enumerate(all_nodes) if n['id'] == node_id), 1)
        all_devs     = self.db.deviations(node_id)
        dev_pos_map  = {d['id']: i + 1 for i, d in enumerate(all_devs)}
        cause_pos_map = {}
        for d in all_devs:
            for j, c in enumerate(self.db.causes_for_deviation(d['id'])):
                cause_pos_map[c['id']] = j + 1

        raw = self.db.causes_for_node_excluding_deviation(node_id, deviation_id)
        existing_causes = []
        for c in raw:
            cd = dict(c)
            dp = dev_pos_map.get(cd['deviation_id'], 0)
            cp = cause_pos_map.get(cd['id'], 0)
            cd['dev_label'] = f"{node_idx}.{dp}"
            cd['ref_label'] = f"{node_idx}.{dp}.{cp}"
            existing_causes.append(cd)

        if existing_causes:
            dlg = ReuseDeviationCausesDialog(dev_name, existing_causes, parent=self)
            result = dlg.exec()
            if result == QDialog.DialogCode.Rejected:
                return
            if result == QDialog.DialogCode.Accepted:
                markers_need_reload = False
                for desc, orig_cause_id, comp_type, comp_tag in dlg.get_selections():
                    new_cid = self.db.add_cause(deviation_id)
                    self.db.update_cause(new_cid, desc,
                                         comp_type=comp_type, comp_tag=comp_tag)
                    if orig_cause_id is not None:
                        for m in self.db.cause_markers_for_cause(orig_cause_id):
                            self.db.add_cause_marker(
                                new_cid, m['pid_page'], m['x'], m['y'],
                                m['component_type'], m['component_tag'])
                            markers_need_reload = True
                self.tree_panel.refresh()
                if markers_need_reload:
                    self.pid_panel.reload_overlays()

        self.pid_panel.set_active_node(node_id)
        self._switch_view(0)
        self.pid_panel.start_cause_template_mode(deviation_id)

    def _on_cause_placement_requested(self, dev_id, suggested_tag, detected_type,
                                       scene_pos, page, suggested_desc=''):
        """P&ID clicked in cause-template mode — show StandardCausesPickerPopup."""
        dev_row  = self.db.get_deviation(dev_id) if dev_id else None
        dev_desc = dev_row['description'] if dev_row else ''
        node_id  = dev_row['node_id'] if dev_row else getattr(
            self.pid_panel, '_active_node_id', None)

        # Fallback: use first available node when no node is active
        if node_id is None:
            first_node = next(iter(self.db.nodes()), None)
            if first_node:
                node_id = first_node['id']

        # Look up standard_deviations.id from the description
        std_dev_id = None
        if dev_desc:
            row = self.db.conn.execute(
                "SELECT id FROM standard_deviations WHERE description=? COLLATE NOCASE LIMIT 1",
                (dev_desc,)).fetchone()
            if row:
                std_dev_id = row[0]

        # Tag for the popup: prefer the parsed tag over raw area-text.
        # suggested_tag is the clean parsed tag (e.g. 'E1.M1.GPA4').
        # suggested_desc is full area text — used only when no tag was parsed.
        effective_tag = (suggested_tag or suggested_desc or '').strip()
        if self.db.get_config('tag_strip_spaces', '1') == '1':
            effective_tag = effective_tag.replace(' ', '')

        popup = StandardCausesPickerPopup(
            self.db, std_dev_id,
            deviation_name=dev_desc,
            comp_type=detected_type or '',
            initial_tag=effective_tag,
            node_id=node_id,
            parent=self)
        popup.setWindowFlags(popup.windowFlags() | Qt.WindowType.Window)

        # Position near the ORS cell or cursor
        gp = self.scenario_panel.ors_cell_global_pos(dev_id) if dev_id else None
        if gp is None:
            gp = QCursor.pos()
        screen = (QApplication.screenAt(gp) or QApplication.primaryScreen()).availableGeometry()
        popup.adjustSize()
        pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
        x = min(gp.x() + 4, screen.right()  - pw)
        y = min(gp.y(),      screen.bottom() - ph)
        popup.move(max(screen.left(), x), max(screen.top(), y))

        def _on_picked(desc, freq):
            try:
                actual_dev_id = popup.selected_node_dev_id or dev_id
                if not actual_dev_id:
                    QMessageBox.warning(popup, "Välj avvikelse",
                                        "Välj en avvikelse innan du lägger till orsaken.")
                    return
                checked   = next((b for b in popup._obj_btn_group if b.isChecked()), None)
                comp_type = checked.property('obj_name') if checked else ''
                tag_text  = popup._tag_edit.text().strip() if hasattr(popup, '_tag_edit') else effective_tag

                self.pid_panel.place_cause_from_template(
                    actual_dev_id, scene_pos, page, comp_type, tag_text, desc, freq)
            except Exception as e:
                import traceback
                QMessageBox.critical(self, "Fel vid tillägg av orsak",
                                     f"{type(e).__name__}: {e}\n\n{traceback.format_exc()}")

        popup.cause_picked.connect(_on_picked)
        popup.exec()

    def _on_equipment_placement_requested(self, suggested_tag, scene_pos, page, pdf_rect=None):
        """P&ID right-click OR right-drag-rubber-band menu -> "🔧 Objekt"
        (2026-08-07/2026-08-09, see NOTES.md). pdf_rect (rubber-band case
        only) is threaded straight through to place_equipment_marker so
        the new marker gets a real outline shape."""
        detected_type = self.pid_panel._db_comp_for_tag(suggested_tag)
        popup = EquipmentTagPopup(self.db, suggested_tag=suggested_tag,
                                  suggested_type=detected_type, parent=self)

        def _on_picked(tag, comp_type):
            self.pid_panel.place_equipment_marker(tag, comp_type, scene_pos, page, pdf_rect=pdf_rect)

        popup.committed.connect(_on_picked)
        popup.exec()

    def _on_equipment_dropped_on_deviation(self, dev_id, marker_ids):
        """One or more equipment markers dragged from the P&ID onto a
        deviation in the HAZOP tree (2026-08-08, see NOTES.md) — creates
        one empty, tagged cause per marker directly (no popup), same
        immediate-inline-editable spirit as the worksheet/tree "+"
        auto-consequence feature. If the equipment has no node yet, it's
        assigned to the deviation's own node — same "slipp välja nod varje
        gång" convenience rule EquipmentDeviationBar.load() already uses.

        Also ties the FIRST dropped marker's equipment to the deviation
        itself, if it doesn't already have one (2026-08-09, see NOTES.md:
        "drar jag ett eller flera objekt till trädet skall även kolumnen
        utrustning fyllas i så det blir stringent, inte bara under
        orsak") — previously only the created CAUSE got tagged
        (comp_tag/comp_type, shown in the ORS column), leaving the
        worksheet's separate Utrustning column (driven by the
        deviation's own equipment_id, not the cause's tag) empty and
        inconsistent with the EquipmentDeviationBar checkbox flow, which
        always sets both."""
        dev = self.db.get_deviation(dev_id)
        if not dev:
            return
        node_id = dev['node_id']
        dev_equipment_id = dev['equipment_id']
        last_cause_id = None
        for marker_id in marker_ids:
            equip = self.db.get_equipment_by_marker_id(marker_id)
            if not equip:
                continue
            if equip.get('node_id') is None and node_id is not None:
                self.db.set_equipment_node(equip['id'], node_id)
            if dev_equipment_id is None:
                self.db.set_deviation_equipment(dev_id, equip['id'])
                dev_equipment_id = equip['id']
            cause_id, _cons_id = _create_tagged_cause(
                self.db, dev_id, equip.get('equipment_type', ''), equip.get('tag', ''))
            last_cause_id = cause_id
        if last_cause_id is not None:
            self.tree_panel.refresh(CAUSE_T, last_cause_id)
            self.scenario_panel.refresh_placed()
            if node_id is not None:
                self.scenario_panel.load_node(node_id)

    def _on_add_consequences_on_pid(self, cause_id):
        """Right-click cause → 'Lägg till konsekvens på P&ID'."""
        cause = self.db.get_cause(cause_id)
        if not cause:
            return
        node_id = cause['node_id']
        self.pid_panel.set_active_node(node_id)
        self.pid_panel.set_active_cause(cause_id)
        self._switch_view(0)
        self.pid_panel._set_mode(MODE_CONSEQUENCE)

    def _on_add_safeguards_on_pid(self, cons_id):
        """Right-click consequence → 'Lägg till safeguard på P&ID'."""
        cons = self.db.get_consequence(cons_id)
        if not cons:
            return
        cause = self.db.get_cause(cons['cause_id'])
        node_id = cause['node_id'] if cause else None
        if node_id:
            self.pid_panel.set_active_node(node_id)
        self.pid_panel.set_active_consequence(cons_id)
        self._switch_view(0)
        self.pid_panel._set_mode(MODE_SAFEGUARD)

    def _open_consequence_step_picker(self, cons_id: int):
        """Open ConsequenceStepPickerDialog after a new consequence is created on P&ID."""
        logging.info('_open_consequence_step_picker: cons_id=%s', cons_id)
        try:
            cons = self.db.get_consequence(cons_id)
            if not cons:
                logging.warning('_open_consequence_step_picker: consequence %s not found in DB', cons_id)
                return
            cause = self.db.get_cause(cons['cause_id'])
            dev_desc = comp = cause_tx = ''
            if cause:
                cause_d = dict(cause)
                comp    = cause_d.get('comp_type', '') or ''
                cause_tx = cause_d.get('description', '') or ''
                dev_id  = cause_d.get('deviation_id')
                if dev_id:
                    dev = self.db.get_deviation(dev_id)
                    if dev:
                        dev_desc = dev['description'] or ''

            initial_tag = getattr(self.pid_panel, '_pending_cons_tag', '') or ''
            # If the consequence tag is known, look up the object type via
            # smart recognition so the dialog can pre-select the right category.
            if initial_tag and not comp:
                comp = _lookup_comp_type_for_tag(initial_tag, self.db)
            logging.info('_open_consequence_step_picker: creating dialog (dev=%r comp=%r tag=%r)', dev_desc, comp, initial_tag)

            dlg = ConsequenceStepPickerDialog(
                self.db, cons_id,
                deviation=dev_desc, comp_type=comp, cause_text=cause_tx,
                initial_ref_tag=initial_tag,
                parent=self)
            # Open next to the HAZOP scenario table's row for this consequence
            # (falls back to the current cursor position if the row isn't
            # visible in the table's current node/deviation/cause scope)
            # instead of the OS's default centered placement.
            dlg.move(self.scenario_panel._pos_near_cons_row(cons_id, dlg.sizeHint()))
            self._active_step_picker = dlg
            logging.info('_open_consequence_step_picker: calling dlg.exec()')
            result = dlg.exec()
            logging.info('_open_consequence_step_picker: dlg.exec() returned %s', result)
            if result == QDialog.DialogCode.Accepted:
                self._active_step_picker = None
                logging.info('_open_consequence_step_picker: accepted — calling scenario_panel._rebuild()')
                try:
                    self.scenario_panel._rebuild()
                    logging.info('_open_consequence_step_picker: _rebuild() done')
                except Exception:
                    logging.exception('_open_consequence_step_picker: CRASH in _rebuild()')
                if dlg.add_more_requested:
                    logging.info('_open_consequence_step_picker: add_more_requested — set MODE_CONSEQUENCE')
                    self.pid_panel._set_mode(MODE_CONSEQUENCE)
            else:
                logging.info('_open_consequence_step_picker: cancelled/rejected')
                self._active_step_picker = None
        except Exception:
            logging.exception('_open_consequence_step_picker: CRASH')

    def _on_ref_tag_picked(self, tag: str):
        """Called when user clicks P&ID in MODE_PICK_REF_TAG — fill the waiting column."""
        dlg = getattr(self, '_active_step_picker', None)
        if dlg is None or not dlg.isVisible():
            return
        col_idx = getattr(dlg, '_waiting_col_idx', None)
        if col_idx is not None and 0 <= col_idx < len(dlg._cols):
            dlg._cols[col_idx]['ref_edit'].setText(tag)
        dlg._waiting_col_idx = None
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_edit_node_markup(self, node_id):
        """Tree right-click NODE → 'Editera nodmarkup'."""
        self._switch_view(0)
        self.node_markup_panel.load(node_id)
        self.markup_table_panel.load(node_id)
        self.tree_panel.setVisible(False)
        self.props_ribbon.setVisible(False)
        self.node_markup_panel.setVisible(True)
        self.scenario_panel.setVisible(False)
        self.markup_table_panel.setVisible(True)
        self._h_splitter.setSizes([0, 800, 0, 64, 0])
        self._v_splitter.setSizes([0, 200, 0])
        self._outer_splitter.setSizes([560, 200])
        self.pid_panel.enter_markup_edit(node_id)
        self._markup_undo_stack.clear()
        self._undo_shortcut.setEnabled(True)

    def _on_close_node_markup(self):
        """Ribbon close button clicked — leave markup edit mode."""
        self.pid_panel.exit_markup_mode()
        self.pid_panel.reload_overlays()
        self.tree_panel.setVisible(True)
        self.props_ribbon.setVisible(True)
        self.node_markup_panel.setVisible(False)
        self.scenario_panel.setVisible(True)
        self.markup_table_panel.setVisible(False)
        self._h_splitter.setSizes([260, 650, 370, 0, 0])
        self._v_splitter.setSizes([220, 0, 0])
        self._outer_splitter.setSizes([640, 220])
        self.stack.setCurrentWidget(self.welcome_panel)
        self._markup_undo_stack.clear()
        self._undo_shortcut.setEnabled(False)

    def _on_edit_red_markup(self, node_id):
        """Tree right-click NODE → 'Editera redmarkup'."""
        self._switch_view(0)
        self.red_markup_panel.load(node_id)
        self.red_markup_table_panel.load(node_id)
        self.tree_panel.setVisible(False)
        self.props_ribbon.setVisible(False)
        self.red_markup_panel.setVisible(True)
        self.scenario_panel.setVisible(False)
        self.red_markup_table_panel.setVisible(True)
        self._h_splitter.setSizes([0, 800, 0, 0, 64])
        self._v_splitter.setSizes([0, 0, 200])
        self._outer_splitter.setSizes([560, 200])
        self.pid_panel.enter_red_markup_edit(node_id)

    def _on_close_red_markup(self):
        """Red markup ribbon close button clicked — leave red markup edit mode."""
        self.pid_panel.exit_red_markup_mode()
        self.pid_panel.reload_overlays()
        self.tree_panel.setVisible(True)
        self.props_ribbon.setVisible(True)
        self.red_markup_panel.setVisible(False)
        self.scenario_panel.setVisible(True)
        self.red_markup_table_panel.setVisible(False)
        self._h_splitter.setSizes([260, 650, 370, 0, 0])
        self._v_splitter.setSizes([220, 0, 0])
        self._outer_splitter.setSizes([640, 220])
        self.stack.setCurrentWidget(self.welcome_panel)

    def _on_red_markup_draw_finished(self, type_, node_id, pts, page, label):
        """New red markup drawn on P&ID — save to DB and refresh."""
        color, opacity, line_width, font_size = self.red_markup_panel.get_current_style()
        if type_ == 'symbol':
            symbol_id = label  # label holds the symbol_id for symbol type
            sw, sh, sr = self.red_markup_panel.get_symbol_dims()
            mu_id = self.db.add_node_red_markup(
                node_id, type_, pts, symbol_id, color, opacity, line_width,
                page, font_size, sw, sh, sr)
        else:
            mu_id = self.db.add_node_red_markup(
                node_id, type_, pts, label, color, opacity, line_width, page, font_size)
        self.pid_panel.viewer._pending_path_item = None
        self.pid_panel.refresh_red_markup_overlays()
        self.red_markup_table_panel.refresh()
        self.red_markup_table_panel.select_markup(mu_id)

    def _on_red_markup_moved(self, mu_id, new_pts):
        """Red markup item dragged to new position — save to DB."""
        self.db.update_node_red_markup(mu_id, points=new_pts)
        self.red_markup_table_panel.refresh()

    def _on_markup_symbol_dims_changed(self, mu_id, w, h, rot):
        """Symbol resized or rotated — save new dims to DB and re-render."""
        self.db.update_node_red_markup(mu_id, symbol_w=w, symbol_h=h, symbol_rot=rot)
        self.pid_panel.refresh_red_markup_overlays()

    def _on_board_layout_changed(self, layout_json):
        self.db.set_pid_config_value('board_layout', layout_json)

    def _on_node_markup_vis(self, node_id, visible):
        """Tree context menu hide/show all markups for a node."""
        self.db.set_all_node_markups_visible(node_id, visible)
        self.pid_panel.refresh_markup_overlays()
        if self.markup_table_panel.isVisible() and self.markup_table_panel.node_id == node_id:
            self.markup_table_panel.refresh()

    def _on_markup_draw_finished(self, type_, node_id, pts, page, label):
        """New markup drawn on P&ID — save to DB and refresh."""
        color, opacity, line_width, font_size = self.node_markup_panel.get_current_style()
        mu_id = self.db.add_node_markup(
            node_id, type_, pts, label, color, opacity, line_width, page, font_size)
        self._markup_undo_stack.append({'op': 'draw', 'mu_id': mu_id})
        self.pid_panel.viewer._pending_path_item = None
        self.pid_panel.refresh_markup_overlays()
        self.markup_table_panel.refresh()
        self.markup_table_panel.select_markup(mu_id)

    def _on_markup_moved(self, mu_id, new_pts):
        """Markup item dragged to new position — save to DB, push undo entry."""
        red_row = self.db.get_node_red_markup(mu_id)
        if red_row:
            # Red markup item — route to red markup handler
            self._on_red_markup_moved(mu_id, new_pts)
            return
        old_row = self.db.get_node_markup(mu_id)
        if old_row:
            old_pts = json.loads(dict(old_row).get('points', '[]') or '[]')
            self._markup_undo_stack.append({'op': 'move', 'mu_id': mu_id, 'old_pts': old_pts})
        self.db.update_node_markup(mu_id, points=new_pts)
        self.markup_table_panel.refresh()

    def _on_markup_label_edited(self, mu_id, new_label):
        self.db.update_node_markup(mu_id, label=new_label)
        self.pid_panel.refresh_markup_overlays()
        self.markup_table_panel.refresh()

    def _on_jump_to_node_markup(self, node_id):
        """Double-click node with markups in tree → enter markup edit mode and zoom to items."""
        self._on_edit_node_markup(node_id)
        markups = self.db.node_markups_for_node(node_id)
        if not markups:
            return
        markups = [dict(m) for m in markups]
        mu_ids = [m['id'] for m in markups]

        # Navigate to the physical page of the first markup
        phys_page = markups[0].get('pid_page', 0)
        sheet_map = self.pid_panel._sheet_map  # display_index → physical_page
        if sheet_map:
            display_n = next((k for k, v in sheet_map.items() if v == phys_page), 0)
        else:
            display_n = phys_page
        self.pid_panel._goto_page(display_n)

        # Zoom to markup bounding box (overlays already loaded by enter_markup_edit)
        self.pid_panel.viewer.zoom_to_markup_items(mu_ids)

    def _on_duplicate_markup(self, mu_id):
        mu = self.db.get_node_markup(mu_id)
        if not mu:
            return
        mu = dict(mu)
        pts = json.loads(mu.get('points', '[]') or '[]')
        offset_pts = [[p[0] + 20, p[1] + 20] if len(p) >= 2 else p for p in pts]
        new_id = self.db.add_node_markup(
            node_id=mu['node_id'],
            type_=mu['type'],
            pts=offset_pts,
            label=mu['label'] + ' (kopia)',
            color=mu['color'],
            opacity=float(mu['opacity']),
            line_width=int(mu['line_width']),
            page=mu['pid_page'],
            font_size=int(mu['font_size']))
        self._markup_undo_stack.append({'op': 'draw', 'mu_id': new_id})
        self.pid_panel.refresh_markup_overlays()
        self.markup_table_panel.refresh()
        self.markup_table_panel.select_markup(new_id)

    def _undo_last_markup(self):
        if not self._markup_undo_stack:
            return
        entry = self._markup_undo_stack.pop()
        if entry['op'] == 'draw':
            self.db.delete_node_markup(entry['mu_id'])
            self.pid_panel.refresh_markup_overlays()
            self.markup_table_panel.refresh()
        elif entry['op'] == 'move':
            self.db.update_node_markup(entry['mu_id'], points=entry['old_pts'])
            self.pid_panel.refresh_markup_overlays()
            self.markup_table_panel.refresh()

    def _on_existing_marker_placed(self, type_str, id_):
        """Marker placed via 'place existing' flow — refresh pins and tree without reloading panels."""
        type_ = {'cause': CAUSE_T, 'consequence': CONS_T, 'safeguard': SG_T}.get(type_str)
        if type_ is not None:
            self.tree_panel.refresh(type_, id_)
        self.scenario_panel.refresh_placed()

    def _on_matrix_changed(self):
        if self._cur_type == CONS_T and self._cur_id is not None:
            self.cons_panel.load(self._cur_id)
        self.tree_panel.refresh()
        if self._cur_type == CAUSE_T and self._cur_id:
            self.scenario_panel.load_cause(self._cur_id)

    def _open_risk_scenario_wizard(self, node_id=None):
        """Start guided Risk Scenario mode using existing P&ID dialogs."""
        # Resolve node from current selection if not supplied
        if not node_id:
            if self._cur_type in (NODE_T, CAUSE_T, CONS_T, SG_T) and self._cur_id:
                node_id = self.tree_panel._resolve_node_id(self._cur_type, self._cur_id)
        if not node_id:
            nodes = self.db.nodes()
            if not nodes:
                QMessageBox.information(self, "Ingen nod",
                    "Lägg till en nod i trädet innan du startar Risk Scenario.")
                return
            node_id = nodes[0]['id']

        # Switch to P&ID view if not already there
        self._switch_view(0)

        # Start guided mode in PIDPanel
        self.pid_panel.start_scenario_mode(node_id)
        self.status_bar.showMessage(
            "Risk Scenario startat — följ stegen i bannern ovan P&ID:n.", 5000)

    def _on_pid_risk_scenario(self, node_id, pos, page):
        self._open_risk_scenario_wizard(node_id)

    def _export_excel(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Exportera Excel", "hazop_rapport.xlsx", "Excel (*.xlsx)")
        if not path: return
        ok, err = export_excel(self.db, path)
        if ok:
            self.status_bar.showMessage(f"Excel sparad: {path}", 6000)
            QMessageBox.information(self, "Klar", f"Exporterad till:\n{path}")
        else:
            QMessageBox.critical(self, "Fel vid export", err)

    def _export_pdf(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Exportera PDF", "hazop_rapport.pdf", "PDF (*.pdf)")
        if not path: return
        ok, err = export_pdf(self.db, path)
        if ok:
            self.status_bar.showMessage(f"PDF sparad: {path}", 6000)
            QMessageBox.information(self, "Klar", f"Exporterad till:\n{path}")
        else:
            QMessageBox.critical(self, "Fel vid export", err)

    # ── Dark mode ─────────────────────────────────────────────────────────────
    _dark_mode = False

    def _toggle_dark_mode(self):
        self._dark_mode = not self._dark_mode
        if self._dark_mode:
            p = QApplication.instance().palette()
            dark = QColor(30, 30, 30); mid = QColor(50, 50, 50)
            text = QColor(220, 220, 220); accent = QColor(99, 155, 255)
            p.setColor(p.ColorRole.Window,      dark)
            p.setColor(p.ColorRole.WindowText,  text)
            p.setColor(p.ColorRole.Base,        mid)
            p.setColor(p.ColorRole.AlternateBase, QColor(40, 40, 40))
            p.setColor(p.ColorRole.Text,        text)
            p.setColor(p.ColorRole.Button,      mid)
            p.setColor(p.ColorRole.ButtonText,  text)
            p.setColor(p.ColorRole.Highlight,   accent)
            p.setColor(p.ColorRole.HighlightedText, QColor(0,0,0))
            p.setColor(p.ColorRole.ToolTipBase, dark)
            p.setColor(p.ColorRole.ToolTipText, text)
            QApplication.instance().setPalette(p)
        else:
            QApplication.instance().setPalette(QApplication.style().standardPalette())

    # ── Excel export (IEC 61511 layout) ──────────────────────────────────────
    def _export_excel(self):
        try:
            import openpyxl
            from openpyxl.styles import (PatternFill, Font, Alignment, Border, Side)
            from openpyxl.utils import get_column_letter
        except ImportError:
            QMessageBox.critical(self, "Saknar beroende",
                "openpyxl krävs: pip install openpyxl")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Exportera Excel", "hazop_rapport.xlsx", "Excel (*.xlsx)")
        if not path: return

        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        thin = Side(style='thin')
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        hdr_fill = PatternFill("solid", fgColor="1F4E79")
        hdr_font = Font(color="FFFFFF", bold=True, size=10)
        hdr_align = Alignment(horizontal='center', vertical='center', wrap_text=True)

        risk_colors = {'Låg': 'C6EFCE', 'Mellan': 'FFEB9C',
                       'Hög': 'FFC7CE', 'Kritisk': 'FF0000'}

        def make_sheet(wb, node):
            title = node['name'][:25].replace('/', '-').replace('\\', '-')
            ws = wb.create_sheet(title=title)
            ws.sheet_view.showGridLines = True
            cols = ['Nod', 'Avvikelse', 'Orsak', 'Konsekvens',
                    'Risk före', 'Barriär', 'RRF', 'Risk efter', 'Åtgärd']
            for ci, col in enumerate(cols, 1):
                c = ws.cell(row=1, column=ci, value=col)
                c.fill = hdr_fill; c.font = hdr_font
                c.alignment = hdr_align; c.border = border
            ws.row_dimensions[1].height = 28
            ws.column_dimensions['A'].width = 14
            ws.column_dimensions['B'].width = 18
            ws.column_dimensions['C'].width = 30
            ws.column_dimensions['D'].width = 35
            ws.column_dimensions['E'].width = 12
            ws.column_dimensions['F'].width = 28
            ws.column_dimensions['G'].width = 8
            ws.column_dimensions['H'].width = 12
            ws.column_dimensions['I'].width = 30
            return ws

        for node in self.db.nodes():
            nd = dict(node)
            ws = make_sheet(wb, nd)
            r = 2
            for dev in self.db.deviations(nd['id']):
                for cause in self.db.causes_for_deviation(dev['id']):
                    cd = dict(cause)
                    cons_list = list(self.db.consequences(cd['id']))
                    if not cons_list:
                        ws.cell(r, 1, nd['name']); ws.cell(r, 2, dev['description'])
                        ws.cell(r, 3, cd['description']); r += 1
                        continue
                    for cons in cons_list:
                        kd = dict(cons)
                        sgs = list(self.db.safeguards(kd['id']))
                        if not sgs:
                            ws.cell(r, 1, nd['name']); ws.cell(r, 2, dev['description'])
                            ws.cell(r, 3, cd['description']); ws.cell(r, 4, kd['description'])
                            r += 1; continue
                        for sg in sgs:
                            sd = dict(sg)
                            ws.cell(r, 1, nd['name']); ws.cell(r, 2, dev['description'])
                            ws.cell(r, 3, cd['description']); ws.cell(r, 4, kd['description'])
                            ws.cell(r, 6, sd['description']); ws.cell(r, 7, sd.get('rrf', 1))
                            for c in range(1, 10):
                                ws.cell(r, c).border = border
                                ws.cell(r, c).alignment = Alignment(wrap_text=True, vertical='top')
                            r += 1
            ws.freeze_panes = 'A2'
        wb.save(path)
        self.status_bar.showMessage(f"Excel sparad: {path}", 6000)
        QMessageBox.information(self, "Klar", f"Exporterad till:\n{path}")

    # ── Action report PDF ─────────────────────────────────────────────────────
    def _export_actions_pdf(self):
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib import colors
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
            from reportlab.lib.styles import getSampleStyleSheet
        except ImportError:
            QMessageBox.critical(self, "Saknar beroende",
                "reportlab krävs: pip install reportlab")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Åtgärdsrapport", "hazop_atgarder.pdf", "PDF (*.pdf)")
        if not path: return

        styles = getSampleStyleSheet()
        doc = SimpleDocTemplate(path, pagesize=A4)
        elements = []
        elements.append(Paragraph("HAZOP Åtgärdsrapport", styles['Title']))
        elements.append(Spacer(1, 12))
        data = [['Nod', 'Orsak/Konsekvens', 'Åtgärd', 'Ansvarig', 'Deadline', 'Status']]
        for node in self.db.nodes():
            nd = dict(node)
            for dev in self.db.deviations(nd['id']):
                for cause in self.db.causes_for_deviation(dev['id']):
                    for cons in self.db.consequences(cause['id']):
                        for act in self.db.actions(cons['id']):
                            ad = dict(act)
                            data.append([
                                nd['name'][:20],
                                f"{cause['description'][:25]} → {cons['description'][:25]}",
                                ad.get('description', '')[:40],
                                ad.get('responsible', ''),
                                ad.get('due_date', ''),
                                ad.get('status', ''),
                            ])
        if len(data) == 1:
            elements.append(Paragraph("Inga åtgärder registrerade.", styles['Normal']))
        else:
            t = Table(data, colWidths=[80, 130, 140, 70, 60, 55])
            t.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1F4E79')),
                ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
                ('FONTSIZE',   (0,0), (-1,-1), 7),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f0f4f8')]),
                ('GRID', (0,0), (-1,-1), 0.25, colors.grey),
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ]))
            elements.append(t)
        doc.build(elements)
        self.status_bar.showMessage(f"Åtgärdsrapport sparad: {path}", 6000)
        QMessageBox.information(self, "Klar", f"Sparad till:\n{path}")

    # ── Print scenario table ──────────────────────────────────────────────────
    def _print_scenario_table(self):
        from PyQt6.QtPrintSupport import QPrinter, QPrintPreviewDialog
        from PyQt6.QtGui import QTextDocument

        html = ['<html><body style="font-family:Arial;font-size:9pt;">',
                '<h2>HAZOP Scenariotabell</h2>',
                '<table border="1" cellspacing="0" cellpadding="3" width="100%">',
                '<tr style="background:#1F4E79;color:white;font-weight:bold;">',
                '<th>Nod</th><th>Avvikelse</th><th>Orsak</th>',
                '<th>Konsekvens</th><th>Barriär</th><th>Risk</th></tr>']
        alt = False
        for node in self.db.nodes():
            nd = dict(node)
            for dev in self.db.deviations(nd['id']):
                for cause in self.db.causes_for_deviation(dev['id']):
                    cons_list = list(self.db.consequences(cause['id']))
                    for cons in (cons_list or [None]):
                        kd = dict(cons) if cons else {}
                        sgs = list(self.db.safeguards(kd['id'])) if kd else []
                        bg = '#f8f9fa' if alt else '#ffffff'
                        html.append(f'<tr style="background:{bg}">')
                        html.append(f'<td>{nd["name"]}</td>')
                        html.append(f'<td>{dev["description"]}</td>')
                        html.append(f'<td>{cause["description"]}</td>')
                        html.append(f'<td>{kd.get("description","")}</td>')
                        html.append(f'<td>{"<br>".join(s["description"] for s in sgs)}</td>')
                        html.append(f'<td></td></tr>')
                        alt = not alt
        html.append('</table></body></html>')

        doc = QTextDocument()
        doc.setHtml(''.join(html))
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setPageSize(QPrinter.PageSize.A4)
        printer.setPageOrientation(QPrinter.Orientation.Landscape)
        preview = QPrintPreviewDialog(printer, self)
        preview.paintRequested.connect(doc.print_)
        preview.exec()

    # ── Statistics ────────────────────────────────────────────────────────────
    def _show_statistics(self):
        db = self.db
        nodes  = list(db.nodes())
        n_devs = sum(len(list(db.deviations(n['id']))) for n in nodes)
        n_caus = db.conn.execute("SELECT COUNT(*) FROM causes").fetchone()[0]
        n_cons = db.conn.execute("SELECT COUNT(*) FROM consequences").fetchone()[0]
        n_sg   = db.conn.execute("SELECT COUNT(*) FROM safeguards").fetchone()[0]
        n_act  = db.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        # Completeness: causes with ≥1 consequence with severity
        n_complete = db.conn.execute(
            "SELECT COUNT(DISTINCT c.id) FROM causes c "
            "JOIN consequences k ON k.cause_id=c.id "
            "WHERE k.severity > 0").fetchone()[0]
        pct = round(100 * n_complete / max(n_caus, 1))
        high_risk = db.conn.execute(
            "SELECT COUNT(*) FROM consequences WHERE severity >= 4").fetchone()[0]

        msg = (f"<h3>Projektstatistik</h3>"
               f"<table cellspacing='4'>"
               f"<tr><td><b>Noder:</b></td><td>{len(nodes)}</td></tr>"
               f"<tr><td><b>Avvikelser:</b></td><td>{n_devs}</td></tr>"
               f"<tr><td><b>Orsaker:</b></td><td>{n_caus}</td></tr>"
               f"<tr><td><b>Konsekvenser:</b></td><td>{n_cons}</td></tr>"
               f"<tr><td><b>Barriärer:</b></td><td>{n_sg}</td></tr>"
               f"<tr><td><b>Åtgärder:</b></td><td>{n_act}</td></tr>"
               f"<tr><td><b>Täckningsgrad:</b></td><td>{pct}% ({n_complete}/{n_caus})</td></tr>"
               f"<tr><td><b>Hög/Kritisk risk:</b></td>"
               f"<td style='color:#c0392b;font-weight:bold'>{high_risk}</td></tr>"
               f"</table>")
        box = QMessageBox(self)
        box.setWindowTitle("Statistik")
        box.setText(msg)
        box.setTextFormat(Qt.TextFormat.RichText)
        box.exec()

    # ── Sign-off / approval ───────────────────────────────────────────────────
    def _approve_node(self, node_id: int = None):
        if node_id is None:
            node_id = self._cur_id if self._cur_type == NODE_T else None
        if node_id is None:
            QMessageBox.information(self, "Välj nod",
                "Välj en nod i trädet innan du godkänner den.")
            return
        node = self.db.get_node(node_id)
        if not node:
            QMessageBox.information(self, "Nod saknas",
                "Noden finns inte längre (kan ha tagits bort).")
            return
        status = node['study_status'] if 'study_status' in node.keys() else 'draft'
        statuses = ['draft', 'under_review', 'approved']
        labels   = ['Utkast', 'Under granskning', 'Godkänd']
        choice, ok = QInputDialog.getItem(
            self, 'Status', f'Nod: {node["name"]}\nSätt status:',
            labels, statuses.index(status) if status in statuses else 0, False)
        if not ok: return
        new_status = statuses[labels.index(choice)]
        user = self.db.get_config('user_name', '') or 'okänd'
        self.db.set_node_status(node_id, new_status)
        if new_status == 'approved':
            self.db.approve_node(node_id, user)
        self.tree_panel.refresh(NODE_T, node_id)
        self.status_bar.showMessage(f"Status satt till: {choice}", 4000)

    # ── Feature 6: zoom to active node ────────────────────────────────────────
    def zoom_to_node(self, node_id):
        """Collect all markers for node_id and fit the board view to them."""
        if self.pid_panel.viewer.pdf_doc is None:
            return
        rs = self.pid_panel.viewer.render_scale
        offsets = self.pid_panel.viewer._page_offsets
        pts = []
        for c in self.db.causes_for_node_all(node_id):
            for m in self.db.cause_markers_for_cause(c['id']):
                ox, oy = offsets.get(m['pid_page'], (0.0, 0.0))
                pts.append((ox + m['x'] * rs, oy + m['y'] * rs))
        for cons in self.db.consequences_for_node(node_id):
            for m in self.db.conn.execute(
                    "SELECT pid_page,x,y FROM consequence_markers WHERE consequence_id=?",
                    (cons['id'],)).fetchall():
                ox, oy = offsets.get(m['pid_page'], (0.0, 0.0))
                pts.append((ox + m['x'] * rs, oy + m['y'] * rs))
        if not pts:
            return
        PAD = 150
        min_x = min(p[0] for p in pts) - PAD
        min_y = min(p[1] for p in pts) - PAD
        max_x = max(p[0] for p in pts) + PAD
        max_y = max(p[1] for p in pts) + PAD
        from PyQt6.QtCore import QRectF
        self.pid_panel.viewer.fitInView(
            QRectF(min_x, min_y, max_x - min_x, max_y - min_y),
            Qt.AspectRatioMode.KeepAspectRatio)
        self.pid_panel.viewer._apply_lod(self.pid_panel.viewer.transform().m11())
        self.pid_panel.viewer._schedule_lod_update()

    # ── Feature 19: global search ─────────────────────────────────────────────
    def _open_global_search(self):
        if self._search_dialog is not None:
            self._search_dialog.raise_()
            self._search_dialog.activateWindow()
            return
        dlg = GlobalSearchDialog(self.db, self)
        dlg.navigate_requested.connect(self._on_search_navigate)
        dlg.finished.connect(lambda _: setattr(self, '_search_dialog', None))
        self._search_dialog = dlg
        dlg.show()

    def _on_search_navigate(self, type_, id_):
        self._on_selected(type_, id_)
        self._switch_view(0)
        if type_ == CAUSE_T:
            c = self.db.get_cause(id_)
            if c:
                markers = self.db.cause_markers_for_cause(id_)
                if markers:
                    m = markers[0]
                    self.pid_panel.navigate_to_marker(m['pid_page'], m['x'], m['y'])


    # ══════════════════════════════════════════════════════════════════════════
    # HZP file format  (ZIP archive renamed to .hzp)
    # Contents:
    #   hazop_project.db   — the SQLite database
    #   pid/<filename>.pdf — the P&ID PDF (if one is loaded)
    #   meta.json          — {"hzp_version":1, "created":..., "pdf_name":...}
    # ══════════════════════════════════════════════════════════════════════════

    def _update_title(self):
        name = Path(self._hzp_path).name if self._hzp_path else "Osparad"
        self.setWindowTitle(f"HAZOP Tool  —  {name}")

    def _hzp_new(self):
        if not self._confirm_discard():
            return

        # Step 1: clear all project-specific tables using the existing connection.
        # This guarantees a clean slate even if the DB file cannot be deleted.
        _PROJECT_TABLES = [
            'nodes', 'deviations', 'causes', 'consequences', 'safeguards',
            'actions', 'cause_markers', 'consequence_markers', 'safeguard_markers',
            'safeguard_markers', 'study_tag_memory', 'symbol_fingerprints',
            'equipment_catalog', 'pid_identified_tags', 'pid_config',
            'off_page_connector', 'board_annotations', 'pid_connection',
            'node_markups', 'node_red_markups', 'pid_identified_tags',
        ]
        for tbl in _PROJECT_TABLES:
            try:
                self.db.conn.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass
        for key in ('project_name', 'project_date', 'project_revision',
                    # 'project_date' kept above for legacy DBs created before
                    # the 2026-08-11 date-range change; it is no longer
                    # written or read by SettingsPanel, only cleaned up here.
                    'project_date_start', 'project_date_end',
                    'project_facility', 'project_hazop_leader', 'project_participants',
                    'ocr_default_engine', 'pid_page_orientation_hint',
                    'pid_path', 'pid_layout', 'fill_screen'):
            try:
                self.db.conn.execute("DELETE FROM app_config WHERE key=?", (key,))
            except Exception:
                pass
        try:
            self.db.commit()
        except Exception:
            pass

        # Step 2: flush WAL, close, then try to delete the file for a clean
        # file (optional — data is already gone from step 1).
        try:
            self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass
        try:
            self.db.conn.close()
        except Exception:
            pass

        import time as _time
        for ext in ('', '-shm', '-wal'):
            p = Path(str(DB_PATH) + ext)
            for _ in range(5):
                try:
                    p.unlink(missing_ok=True)
                    break
                except PermissionError:
                    _time.sleep(0.1)

        # Step 3: open (or create) a fresh Database object
        try:
            self.db = Database(DB_PATH)
        except Exception as e:
            QMessageBox.critical(self, "Nytt projekt",
                                 f"Kunde inte skapa ny databas:\n{e}")
            return

        self._hzp_path = None
        self._update_title()
        self._reload_all_panels(pdf_path=None)

    def _hzp_open(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Öppna HAZOP-projekt", "", "HAZOP-filer (*.hzp);;Alla filer (*)")
        if not path:
            return
        self._load_hzp(path)

    def _hzp_save(self):
        if not self._hzp_path:
            self._hzp_save_as()
        else:
            self._write_hzp(self._hzp_path)

    def _hzp_save_as(self):
        default = (Path(self._hzp_path).stem if self._hzp_path
                   else "nytt_projekt") + ".hzp"
        path, _ = QFileDialog.getSaveFileName(
            self, "Spara HAZOP-projekt", default, "HAZOP-filer (*.hzp)")
        if not path:
            return
        if not path.lower().endswith('.hzp'):
            path += '.hzp'
        self._write_hzp(path)
        self._hzp_path = path
        self._update_title()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _confirm_discard(self):
        if self._hzp_path:
            r = QMessageBox.question(
                self, "Osparade ändringar",
                "Vill du spara det nuvarande projektet innan du fortsätter?",
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save)
            if r == QMessageBox.StandardButton.Cancel:
                return False
            if r == QMessageBox.StandardButton.Save:
                self._hzp_save()
        return True

    def _write_hzp(self, path: str):
        import zipfile, json, tempfile, datetime, sqlite3 as _sql
        self.db.conn.commit()   # flush all pending writes
        self.db._write_backup(startup=True)   # force immediate backup snapshot

        # Use SQLite online backup API (safe with WAL mode; consistent snapshot)
        fd, tmp_path = tempfile.mkstemp(suffix='.db')
        import os; os.close(fd)
        tmp_db = Path(tmp_path)
        try:
            bk_conn = _sql.connect(str(tmp_db))
            try:
                with bk_conn:
                    self.db.conn.backup(bk_conn)
            finally:
                bk_conn.close()

            # Collect PDF path from pid_config
            pdf_src = None
            try:
                pdf_str = self.db.get_pid_path()
                if pdf_str and Path(pdf_str).exists():
                    pdf_src = Path(pdf_str)
            except Exception:
                pass

            meta = {
                "hzp_version": 1,
                "created": datetime.datetime.now().isoformat(timespec='seconds'),
                "app": "HAZOP Tool",
                "pdf_name": pdf_src.name if pdf_src else None,
            }

            with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(tmp_db, "hazop_project.db")
                if pdf_src:
                    zf.write(pdf_src, f"pid/{pdf_src.name}")
                zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
        finally:
            try:
                tmp_db.unlink(missing_ok=True)   # always clean up even on exception
            except Exception:
                pass   # don't let cleanup failure mask the real error

        self.status_bar.showMessage(f"Sparat: {path}", 5000)
        self._hzp_path = path
        self._update_title()

    def _load_hzp(self, path: str):
        import zipfile, json, shutil, tempfile

        # Extract to a persistent work dir next to the hzp file
        work_dir = Path(path).parent / (Path(path).stem + "_files")
        work_dir.mkdir(exist_ok=True)

        try:
            with zipfile.ZipFile(path, 'r') as zf:
                zf.extractall(work_dir)
        except zipfile.BadZipFile:
            QMessageBox.critical(self, "Fel", f"Filen är inte en giltig .hzp-fil:\n{path}")
            return

        # Load meta
        pdf_name = None
        try:
            meta = json.loads((work_dir / "meta.json").read_text(encoding='utf-8'))
            pdf_name = meta.get("pdf_name")
        except Exception:
            pass

        # Copy extracted DB over the active DB_PATH.
        # Close the old connection AFTER the copy succeeds so a failed copy
        # (permissions, disk full) leaves self.db in a working state.
        extracted_db = work_dir / "hazop_project.db"
        if not extracted_db.exists():
            QMessageBox.critical(self, "Fel", "Projektet saknar databasfil.")
            return
        try:
            shutil.copy2(extracted_db, DB_PATH)
        except Exception as e:
            QMessageBox.critical(self, "Fel vid inläsning",
                                 f"Kunde inte kopiera databas:\n{e}")
            return

        self.db.conn.close()

        # Reopen DB at same path
        new_db = Database(DB_PATH)
        self.db = new_db

        # Resolve PDF
        pdf_path = None
        if pdf_name:
            candidate = work_dir / "pid" / pdf_name
            if candidate.exists():
                # Keep PDF in work_dir and update pid_config to point there
                pdf_path = str(candidate)
                try:
                    new_db.set_pid_config_value('path', pdf_path)
                except Exception:
                    pass

        self._hzp_path = path
        self._update_title()
        self._reload_all_panels(pdf_path=pdf_path)
        # Immediately create a startup backup of the freshly loaded project
        self.db._write_backup(startup=True)
        self.status_bar.showMessage(f"Öppnat: {path}", 5000)

    def _reload_all_panels(self, pdf_path=None):
        """Swap self.db on every panel and refresh all content."""
        db = self.db
        load_matrix(db)

        # Update db reference on every panel (props_ribbon included)
        for panel in [self.tree_panel, self.node_panel,
                      self.cons_panel, self.sg_panel,
                      self.scenario_panel, self.equipment_panel,
                      self.admin_panel, self.settings_panel,
                      self.node_markup_panel, self.markup_table_panel,
                      self.red_markup_panel, self.red_markup_table_panel,
                      self.worksheet, self.props_ribbon]:
            try:
                panel.db = db
            except Exception:
                pass

        # EquipmentPanel's QTableView is backed by _EquipmentTableModel,
        # which keeps its OWN db reference (needed for setData()/delete_row()
        # to write through directly) separate from EquipmentPanel.db above —
        # found via a real crash: "Cannot operate on a closed database" when
        # switching to the Utrustning page after a project reload, because
        # the model was still holding the OLD (by-then-closed) connection.
        try:
            self.equipment_panel._model.db = db
        except Exception:
            pass

        # Also update db on settings sub-panels (they have their own db reference)
        sp = self.settings_panel
        for attr in ('_std_causes_panel', '_std_objects_panel', '_tag_memory_panel',
                     '_tag_db_panel', 'analysis_panel'):
            try:
                sub = getattr(sp, attr, None)
                if sub is not None:
                    sub.db = db
            except Exception:
                pass
        # analysis_panel (PIDAnalysisPanel) is likewise backed by
        # _IdentifiedTagsModel with its own db reference — same fix as
        # equipment_panel above.
        try:
            sp.analysis_panel._model.db = db
        except Exception:
            pass

        # HAZOPWorksheet embeds its own ScenarioTablePanel instance (distinct
        # from self.scenario_panel above) — it has its own stale db reference.
        try:
            self.worksheet._table_panel.db = db
        except Exception:
            pass

        # StudyManagementPanel (admin_panel) embeds its own PIDManagementPanel
        # (revision history + sheet reordering) — same nested-sub-panel-with-
        # its-own-db-reference pattern as equipment_panel._model/worksheet.
        # _table_panel above, missed here until a real crash: "Cannot operate
        # on a closed database" in Database.get_revisions() when switching to
        # the Administration tab after a project reload (2026-08-11, see
        # NOTES.md).
        try:
            self.admin_panel._pid_mgmt.db = db
        except Exception:
            pass

        # PIDPanel wires differently — it holds db on itself and the viewer
        try:
            self.pid_panel.db = db
            self.pid_panel.viewer.db = db
        except Exception:
            pass

        # EquipmentDeviationBar (the bottom-of-P&ID bar shown when an
        # equipment marker is clicked) also keeps its own db reference —
        # same "Cannot operate on a closed database" crash class as
        # equipment_panel._model/worksheet._table_panel above, found via a
        # real crash report the first time a user clicked an equipment
        # marker after a project reload.
        try:
            self.pid_panel._equipment_bar.db = db
        except Exception:
            pass

        # Reload tree + scenario
        self.tree_panel.refresh()
        self.scenario_panel.clear()
        self.stack.setCurrentWidget(self.welcome_panel)

        # Reload P&ID
        if pdf_path:
            self.pid_panel.try_reload_pdf(pdf_path)
        else:
            self.pid_panel.try_reload_pdf()


if __name__ == '__main__':
    import logging

    # ── Crash logger ───────────────────────────────────────────────────────────
    # Structured crash reporting: saves detailed diagnostic info to JSON files
    # in hazop/crashes/ directory for automatic analysis. Also maintains legacy
    # hazop_crash.log for backward compatibility.
    _LOG = Path(__file__).parent / 'hazop_crash.log'
    logging.basicConfig(
        filename=str(_LOG),
        level=logging.DEBUG,
        format='%(asctime)s %(levelname)s %(message)s',
        encoding='utf-8',
    )
    # Also echo to stderr (visible in the console window)
    # During startup, suppress DEBUG output to console; keep to file
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(console_handler)

    # Setup structured crash reporting
    CrashReporter.setup()

    # sys.excepthook was already installed at module load time (see
    # _global_exception_hook, defined right after the CrashReporter class,
    # near the top of this file) — it logs, writes a structured crash
    # report, and shows a QMessageBox, without calling sys.exit()/os.abort()
    # or re-raising, so a single bad slot cannot take down the whole app.
    # Nothing here should reassign sys.excepthook; doing so would just
    # shadow that hook for the rest of the process.
    assert sys.excepthook is _global_exception_hook, (
        "sys.excepthook was reassigned somewhere between module import and "
        "__main__ -- _global_exception_hook must remain installed")

    # Catch exceptions raised inside Qt signal handlers (they bypass sys.excepthook)
    def _qt_message_handler(mode, context, message):
        if mode in (QtMsgType.QtWarningMsg,
                    QtMsgType.QtCriticalMsg,
                    QtMsgType.QtFatalMsg):
            logging.warning('Qt [%s] %s', mode.name, message)
        else:
            logging.debug('Qt [%s] %s', mode.name, message)

    try:
        from PyQt6.QtCore import qInstallMessageHandler, QtMsgType
        qInstallMessageHandler(_qt_message_handler)
    except ImportError:
        pass

    # Route all Python warnings through the log as well
    import warnings
    logging.captureWarnings(True)

    logging.info('=== HAZOP Tool started ===')

    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # Override Fusion's default OS/theme-blue Highlight role with the app's
    # own accent — otherwise native-rendered selections (custom delegate
    # paints via option.palette.highlight(), combo box popups, etc.) show
    # a different blue than the one used throughout the QSS below.
    _palette = app.palette()
    _palette.setColor(QPalette.ColorRole.Highlight, QColor('#17191C'))
    _palette.setColor(QPalette.ColorRole.HighlightedText, QColor('#FFFFFF'))
    _palette.setColor(QPalette.ColorRole.Window, QColor('#FBFBFA'))
    _palette.setColor(QPalette.ColorRole.Base, QColor('#FFFFFF'))
    _palette.setColor(QPalette.ColorRole.Text, QColor('#17191C'))
    _palette.setColor(QPalette.ColorRole.WindowText, QColor('#17191C'))
    app.setPalette(_palette)

    # Apply Windows 11 light theme
    app.setStyleSheet(_get_windows11_stylesheet())
    # Default font size lives here, not in the QSS above — see that
    # function's own docstring for why a QSS font-size rule would block
    # widgets (like ScenarioTablePanel's "Textstorlek" spinbox) from ever
    # changing their own font size again.
    app.setFont(QFont("Segoe UI", 9))

    # Show splash screen during startup
    splash = SplashScreen()
    splash.show()
    app.processEvents()

    # Catch exceptions in Qt event loop (slots/signals)
    _original_notify = app.notify
    def _safe_notify(receiver, event):
        try:
            return _original_notify(receiver, event)
        except Exception:
            logging.exception('Exception in Qt event loop (notify)')
            return False
    app.notify = _safe_notify

    try:
        splash.set_status("Laddar databas...")
        win = MainWindow()
        splash.set_status("Initialiserar gränssnitt...")
        app.processEvents()
        splash.close_splash()
        win.show()
        code = app.exec()
        logging.info('=== HAZOP Tool exited (code %d) ===', code)

        # Clean up global resources before exit (OCR models, DB connections)
        try:
            from pid_viewer import cleanup_ocr_resources
            cleanup_ocr_resources()
        except Exception:
            pass

        sys.exit(code)
    except Exception:
        logging.exception('Fatal exception during startup or main loop')
        raise
