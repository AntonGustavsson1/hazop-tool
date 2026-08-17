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

from constants import (
    CONFIG, SEV_LABELS, RRF_VALUES, RRF_LABELS, SG_TYPES, MARKUP_COLORS,
    RISK_ICON, NODE_T, CAUSE_T, CONS_T, SG_T, DEV_T, EQUIP_T, LEDORD_T,
    DEVIATION_TYPES,
)
from database import (
    Database, DB_PATH, DEFAULT_MATRIX, _normalise_matrix, _risk_matrix_cache,
    load_matrix, get_matrix, risk_info, freq_to_f_level, DEFAULT_FREQ_BOUNDARIES,
    _STD_OBJECTS, parse_tag_refs, append_tag_to_text, add_tag_ref,
)
from pid_viewer import (
    PIDPanel, COMPONENT_TYPES, CONSEQUENCE_TEMPLATES, HAS_PYMUPDF,
    MODE_NAV, MODE_NODE, MODE_PICK_REF_TAG,
    scan_pdf_for_equipment, ocr_status, resolve_ocr_scan_choice, KNOWN_PREFIXES, invert_cause_text,
    _RED_MARKUP_SYMBOLS, _get_red_symbol_svg,
    _equip_prefix_from_tag,
    detect_equipment_symbols, EquipmentMarkerReviewDialog,
    apply_scan_result_to_equipment_catalog, upsert_identified_tags_from_scan,
    ParallelTagScanWorker, ParallelEquipmentAnalysisWorker,
    PageProgressDialog,
    FREQ_LABELS, freq_to_idx, idx_to_freq,
    _obj_type_matches,
    _mk_pm, _mk_icon, _icon, _EMOJI_ICON,
)
from ui_helpers import (
    freq_axis_label, freq_axis_label_full, cons_axis_label,
    _equipment_type_options, _lookup_comp_type_for_tag, _make_tag_completer,
    _resolve_std_deviation_id, _create_cause_from_pick, _EQ_TYPE_ITEMS,
    find_tag_bold_ranges, _draw_text_with_bold_tags,
    total_freq_reduction, CHAIN_ITEMS, build_consequence_text, parse_chain_from_json,
)
from tree_panel import (
    TreePanel, StandardCausesPickerPopup, CauseObjectPopup, CauseTagPopup,
    RRFPopup, FrequencyPickerPopup, DeviationPickerPopup,
)
from scenario_panel import (
    ScenarioTablePanel, RiskMatrixPopup, ConsequenceStepPickerDialog,
    ReductionFactorsDialog, _ScenarioDelegate, _PidDelegate, _LopaWidget,
    _CONSEQ_ENTRY, _CONSEQ_GENERIC_NEXT, _CONSEQ_NODES, _N_STEPS,
    _ORS_STRIP_H, _PID_ICON_W, _PLUS_BADGE_SIZE,
)
from equipment_panel import (
    EquipmentPanel, EquipmentTagPopup, ObjectPickerPopup, PIDAnalysisPanel,
    TagDatabasePanel, _EquipmentTableModel, _IdentifiedTagsModel, _tag_prefix,
)
from node_markup import (
    PropertiesRibbon, NodeMarkupPanel, MarkupTablePanel, RedMarkupPanel,
    RedMarkupTablePanel,
)
from worksheet import HAZOPWorksheet

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
    QButtonGroup, QRadioButton, QToolButton,
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


def _configure_utf8_console_output():
    """Reconfigure stdout/stderr to UTF-8 so logging an emoji (the app's
    log/status messages are full of them, e.g. 🏭/🎯/🔍) never crashes.

    Windows' console/terminal streams default to the system codepage
    (cp1252 here) rather than UTF-8 unless PYTHONUTF8/PYTHONIOENCODING is
    set. The crash log's own FileHandler already passes encoding='utf-8'
    explicitly, but the console echo handler wrote straight to the
    unreconfigured sys.stderr — any emoji anywhere in a logged message
    then raised UnicodeEncodeError: 'charmap' codec can't encode character
    (real crash report, crash_20260807_115134_UnicodeEncodeError.json;
    trivially reproducible on this machine via print('\U0001f3ed')).
    errors='replace' rather than 'strict' so an exotic character can never
    crash the console echo again, even one outside cp1252 AND unexpected
    by whoever writes the next log message.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass


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
        background-color: #2F5FD0;
        color: #FFFFFF;
        border-color: #2F5FD0;
    }
    QPushButton:focus {
        outline: 2px solid #2F5FD0;
        outline-offset: 2px;
    }
    QPushButton:default {
        border-color: #2F5FD0;
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
        background-color: #2F5FD0;
        border-color: #2F5FD0;
    }

    QSpinBox, QDoubleSpinBox {
        background-color: #FFFFFF;
        color: #17191C;
        border: 1px solid #CFD1CE;
        border-radius: 4px;
        padding: 3px 4px;
    }
    QSpinBox:focus, QDoubleSpinBox:focus { border: 2px solid #2F5FD0; }

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
        border-bottom: 2px solid #2F5FD0;
    }

    QLineEdit, QTextEdit, QPlainTextEdit {
        background-color: #FFFFFF;
        color: #17191C;
        border: 1px solid #CFD1CE;
        border-radius: 4px;
        padding: 4px 6px;
        selection-background-color: #2F5FD0;
        selection-color: #FFFFFF;
    }
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {
        border: 2px solid #2F5FD0;
        padding: 3px 5px;
    }

    QComboBox {
        background-color: #FFFFFF;
        color: #17191C;
        border: 1px solid #CFD1CE;
        border-radius: 4px;
        padding: 4px 8px;
    }
    QComboBox:focus { border: 2px solid #2F5FD0; }
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


def _contrast_fg(bg_hex):
    """Return black or white text color for best contrast against bg_hex."""
    c = QColor(bg_hex)
    luminance = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255
    return '#000000' if luminance > 0.55 else '#ffffff'


# Keep old alias so existing code that references LIKE_LABELS doesn't crash
LIKE_LABELS = FREQ_LABELS


def get_sev_labels():
    """Return severity labels from current matrix config (y_labels), falling back to SEV_LABELS."""
    cfg = get_matrix()
    y = cfg.get('y_labels', [])
    n = cfg.get('rows', 5)
    if y and len(y) >= n:
        return [f"C{i+1} – {y[i]}" if not y[i].startswith('C') else y[i] for i in range(n)]
    return SEV_LABELS[:n] if n <= len(SEV_LABELS) else SEV_LABELS + [f"C{i+1}" for i in range(len(SEV_LABELS), n)]


def _get_node_color(node_id):
    """Get a unique color for a node based on its ID."""
    return MARKUP_COLORS[node_id % len(MARKUP_COLORS)]


def _create_tagged_cause(db, deviation_id, comp_type, comp_tag, equipment_id=None):
    """Create a new cause under deviation_id (only its equipment tag/type
    set) plus one empty consequence — used when an equipment marker is
    dropped directly onto a deviation in the HAZOP tree (2026-08-08, see
    NOTES.md). No popup: the description defaults to the same "Ny orsak"
    placeholder _create_cause_from_pick's own fallback uses (2026-08-10 —
    was blank, unified to match every other auto-created cause/
    consequence/safeguard's placeholder-text convention), immediately
    inline-editable/overtype-able.

    `equipment_id` (2026-08-13, see NOTES.md) links the cause's tag
    strip live to the equipment_catalog row it came from, when the
    caller already knows it (as every current caller does, from the
    marker it just resolved) — no text-matching needed here.
    Returns (cause_id, consequence_id).
    """
    new_id = db.add_cause(deviation_id)
    db.update_cause(new_id, description='Ny orsak', comp_type=comp_type, comp_tag=comp_tag,
                     equipment_id=equipment_id)
    cons_id = db.add_consequence(new_id)
    return new_id, cons_id


# ══════════════════════════════════════════════════════════════════════════════
# SHARED WIDGETS
# ══════════════════════════════════════════════════════════════════════════════

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


class RecommendationEditorDialog(QDialog):
    """Popup opened from the Worksheet's "Rekommendation" column
    (2026-08-13, see NOTES.md) — just ActionEditor (already fully wired
    to the actions table) wrapped in a small dialog, since it previously
    had no reachable place in the UI after the PropertiesRibbon
    migration left ConsequencePanel unshown."""

    def __init__(self, db, consequence_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rekommendationer")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        self._editor = ActionEditor(db)
        self._editor.load(consequence_id)
        layout.addWidget(self._editor)
        btns = QHBoxLayout()
        btns.addStretch()
        close_btn = QPushButton("Stäng")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        layout.addLayout(btns)


# ══════════════════════════════════════════════════════════════════════════════
# NODE MARKUP — RIBBON + STYLE POPUP + TABLE PANEL
# ══════════════════════════════════════════════════════════════════════════════

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
        sep.setStyleSheet("background:#E2E3E1;border:none;")
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
    """4-level editable hierarchy: Nodtyp → Avvikelse → Objekt → Orsaker."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._loading = False
        self._loading_nt = False
        self._node_type_ids = []

        layout = QHBoxLayout(self)

        # ── Col 0: Nodtyp (2026-08-17 user request) ──────────────────────────
        # Filters _dev_list to deviations belonging to the selected node
        # type; drag a deviation from _dev_list onto a node type here to
        # COPY it (deep, independent copy incl. its causes — user confirmed
        # via AskUserQuestion, not a move/link) into that type.
        c0 = QVBoxLayout()
        c0.addWidget(QLabel("<b>Nodtyp</b>"))
        self._nodetype_list = QListWidget()
        self._nodetype_list.currentRowChanged.connect(lambda _row: self._load_deviations())
        self._nodetype_list.itemChanged.connect(self._on_nodetype_item_changed)
        self._nodetype_list.setAcceptDrops(True)
        self._nodetype_list.viewport().setAcceptDrops(True)
        self._nodetype_list.installEventFilter(self)
        self._nodetype_list.viewport().installEventFilter(self)
        c0.addWidget(self._nodetype_list)
        c0b = QHBoxLayout()
        for icon, slot in (('+', self._add_node_type), ('−', self._del_node_type)):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c0b.addWidget(b)
        c0b.addStretch(); c0.addLayout(c0b)
        # _load_node_types() is deferred to the end of __init__ (below,
        # after _dev_list exists) — its setCurrentRow() call fires
        # currentRowChanged immediately, which cascades into
        # _load_deviations(), so calling it here (before _dev_list is
        # built) would crash with AttributeError.

        # ── Col 1: Avvikelse ──────────────────────────────────────────────────
        c1 = QVBoxLayout()
        c1.addWidget(QLabel("<b>Avvikelse</b>"))
        self._dev_list = QListWidget()
        self._dev_list.currentRowChanged.connect(self._on_dev_sel)
        self._dev_list.setDragEnabled(True)
        # Instance-level override (same monkeypatch pattern already used
        # elsewhere in this file, e.g. ParticipantMatrixPanel's Enter-key
        # handling) — carries the deviation's DB id as custom mime text
        # instead of Qt's default internal model-index payload, so the
        # Nodtyp column's drop handler can read it directly.
        def _dev_list_mime_data(items, _list=self._dev_list):
            md = QMimeData()
            if items:
                dev_id = items[0].data(Qt.ItemDataRole.UserRole)
                md.setText(f'hzp:stddev:{dev_id}')
            return md
        self._dev_list.mimeData = _dev_list_mime_data
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
        # "implementera de funktioner som finns i standardobjekt även i
        # standard orsaker så man kan lägga till nya objekt under
        # standardorsaker" (2026-08-17, see NOTES.md) — same add/delete/
        # reorder/rename CRUD as StandardObjectsSettingsPanel's own
        # _list, over the exact same standard_objects table, so a new
        # object type no longer requires switching tabs.
        c2b = QHBoxLayout()
        for icon, slot in (('+', self._add_obj), ('−', self._del_obj),
                           ('↑', lambda: self._move_obj(-1)), ('↓', lambda: self._move_obj(1))):
            b = QPushButton(icon); b.setFixedWidth(28); b.clicked.connect(slot); c2b.addWidget(b)
        c2b.addStretch(); c2.addLayout(c2b)
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

        layout.addLayout(c0, 1)
        layout.addLayout(c1, 1)
        layout.addLayout(c2, 1)
        layout.addLayout(c3, 1)
        self._load_node_types()   # cascades into _load_deviations() via currentRowChanged

    # ── Load helpers ──────────────────────────────────────────────────────────
    # ── Node type CRUD (2026-08-17, see NOTES.md) ────────────────────────────
    def _load_node_types(self):
        self._loading_nt = True
        cur = self._nodetype_list.currentRow()
        self._nodetype_list.clear()
        types = self.db.node_types()
        self._node_type_ids = [t['id'] for t in types]
        for t in types:
            item = QListWidgetItem(t['name'])
            item.setData(Qt.ItemDataRole.UserRole, t['id'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self._nodetype_list.addItem(item)
        self._loading_nt = False
        self._nodetype_list.setCurrentRow(max(0, min(cur, self._nodetype_list.count() - 1)))

    def _current_node_type_id(self):
        item = self._nodetype_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_nodetype_item_changed(self, item):
        if self._loading_nt:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        name = item.text().strip()
        if id_ is not None and name:
            self.db.rename_node_type(id_, name)

    def _add_node_type(self):
        new_id = self.db.add_node_type('Ny nodtyp')
        item = QListWidgetItem('Ny nodtyp')
        item.setData(Qt.ItemDataRole.UserRole, new_id)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self._nodetype_list.addItem(item)
        self._node_type_ids.append(new_id)
        self._nodetype_list.setCurrentItem(item)
        self._nodetype_list.editItem(item)

    def _del_node_type(self):
        item = self._nodetype_list.currentItem()
        if not item:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if len(self._node_type_ids) <= 1:
            QMessageBox.information(self, 'Kan inte ta bort',
                                     'Minst en nodtyp måste finnas kvar.')
            return
        if QMessageBox.question(
                self, 'Ta bort nodtyp',
                'Ta bort nodtypen? Avvikelser under den flyttas till standardtypen.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                ) == QMessageBox.StandardButton.Yes:
            self.db.delete_node_type(id_)
            self._load_node_types()

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        _drop_targets = (self._nodetype_list, self._nodetype_list.viewport())
        if obj in _drop_targets and event.type() == QEvent.Type.DragEnter:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:stddev:'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.DragMove:
            if event.mimeData().hasText() and event.mimeData().text().startswith('hzp:stddev:'):
                event.acceptProposedAction()
                return True
        if obj in _drop_targets and event.type() == QEvent.Type.Drop:
            text = event.mimeData().text() if event.mimeData().hasText() else ''
            if text.startswith('hzp:stddev:'):
                self._handle_deviation_drop(event, obj)
                return True
        return super().eventFilter(obj, event)

    def _handle_deviation_drop(self, event, source_obj):
        text = event.mimeData().text()
        try:
            dev_id = int(text.split(':')[2])
        except (IndexError, ValueError):
            event.ignore()
            return
        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        # Qt delivers drop events to either the outer QListWidget or its
        # viewport depending on version/setup (same lesson as TreePanel's
        # equipment-drop handling) — itemAt() always expects viewport
        # coordinates, so remap only when the event landed on the outer
        # widget instead of the viewport directly.
        if source_obj is self._nodetype_list:
            pos = self._nodetype_list.viewport().mapFrom(self._nodetype_list, pos)
        item = self._nodetype_list.itemAt(pos)
        if item is None:
            event.ignore()
            return
        node_type_id = item.data(Qt.ItemDataRole.UserRole)
        self.db.copy_standard_deviation_to_node_type(dev_id, node_type_id)
        event.acceptProposedAction()
        if node_type_id == self._current_node_type_id():
            self._load_deviations()

    def _load_deviations(self):
        self._loading = True
        cur = self._dev_list.currentRow()
        self._dev_list.clear()
        nt_id = self._current_node_type_id()
        default_nt_id = self._node_type_ids[0] if self._node_type_ids else None
        for d in self.db.standard_deviations():
            d_nt = d['node_type_id']
            belongs = (d_nt == nt_id) or (d_nt is None and nt_id == default_nt_id)
            if not belongs:
                continue
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
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            if n:
                item.setForeground(QColor('#17191C'))
            self._obj_list.addItem(item)
        try:
            self._obj_list.itemChanged.disconnect(self._on_obj_changed)
        except TypeError:
            pass   # wasn't connected yet (first call)
        self._obj_list.itemChanged.connect(self._on_obj_changed)
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
        new_id = self.db.add_standard_deviation('Ny avvikelse', self._current_node_type_id())
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

    # ── Object CRUD (2026-08-17, see NOTES.md — same standard_objects
    # table/methods as StandardObjectsSettingsPanel's own _list) ─────────────────
    def _on_obj_changed(self, item):
        if self._loading:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is None:
            return
        # Strip the "  (n)" cause-count suffix _load_objects() appends
        # for display — an edit must only ever change the object's own
        # name, never bake the count into it.
        name = re.sub(r'\s*\(\d+\)$', '', item.text()).strip()
        if name:
            self.db.update_standard_object(id_, name)
        self._load_objects()

    def _add_obj(self):
        new_id = self.db.add_standard_object('Nytt objekt')
        if not self._show_all_obj_chk.isChecked():
            # A brand-new object has zero causes yet, so it wouldn't
            # appear in the "only objects with causes" view at all —
            # switch to "Visa alla objekt" so it's actually visible to
            # rename/edit right away. Its own stateChanged already
            # triggers _load_objects().
            self._show_all_obj_chk.setChecked(True)
        else:
            self._load_objects()
        for i in range(self._obj_list.count()):
            if self._obj_list.item(i).data(Qt.ItemDataRole.UserRole) == new_id:
                self._obj_list.setCurrentRow(i)
                self._obj_list.editItem(self._obj_list.item(i))
                break

    def _del_obj(self):
        item = self._obj_list.currentItem()
        if not item:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is None:
            return
        self.db.delete_standard_object(id_)
        self._load_objects()

    def _move_obj(self, direction):
        row = self._obj_list.currentRow()
        new_row = row + direction
        if not (0 <= new_row < self._obj_list.count()):
            return
        a = self._obj_list.takeItem(row)
        self._obj_list.insertItem(new_row, a)
        self._obj_list.setCurrentRow(new_row)
        ids = [self._obj_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._obj_list.count())]
        self.db.reorder_standard_objects(ids)

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
                dd['causes'].append({k: cd.get(k) for k in
                    ['description', 'comp_type', 'frequency', 'object_id']})
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
        btn_clear = QPushButton("Rensa allt")
        btn_clear.setIcon(_icon('delete'))
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
        btn_del = QPushButton("Ta bort markerade")
        btn_del.setIcon(_icon('delete'))
        btn_del.clicked.connect(self._delete_selected)
        btn_row.addWidget(btn_del)
        lay.addLayout(btn_row)

        # ── Fingerprints ───────────────────────────────────────────────────────
        fp_hdr = QHBoxLayout()
        fp_title = QLabel("Visuella fingeravtryck (symbolmönster)")
        fp_title.setFont(tf)
        fp_hdr.addWidget(fp_title)
        fp_hdr.addStretch()
        btn_fp_clear = QPushButton("Rensa fingeravtryck")
        btn_fp_clear.setIcon(_icon('delete'))
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


class ParticipantMatrixPanel(QWidget):
    """Deltagarmatris: participants as rows (Förnamn/Efternamn/Roll) ×
    analystillfällen as columns, with a checkbox per cell marking
    attendance. Replaces the old free-text "Deltagare" field in the
    Projekt tab (2026-08-11, user request: "en till flik med deltagare
    istället där man definerar förnamn, efternamn, roll på y axel och
    analystillfälen på x axeln så det blir en matris" — see NOTES.md for
    the full design rationale)."""

    _FIXED_COLS = ['Förnamn', 'Efternamn']

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._loading = False
        self._participant_ids = []
        self._session_ids = []
        self._column_ids = []   # custom participant_columns, between Efternamn and sessions

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        lbl = QLabel(
            "<b>Deltagarmatris</b> — en rad per deltagare (förnamn, efternamn, roll) "
            "och en kolumn per analystillfälle. Bocka i cellen för att markera att "
            "deltagaren var med vid det tillfället.")
        lbl.setWordWrap(True)
        layout.addWidget(lbl)

        self._table = QTableWidget(0, len(self._FIXED_COLS))
        self._table.setHorizontalHeaderLabels(self._FIXED_COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.itemChanged.connect(self._on_item_changed)
        # Enter on a selected (non-editing) cell adds a new participant row,
        # matching the same "+"-button action (2026-08-17 user request).
        _base_kp = self._table.keyPressEvent
        def _table_key_press(event, _base=_base_kp):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
                    self._table.state() != QTableWidget.State.EditingState:
                self._add_participant()
            else:
                _base(event)
        self._table.keyPressEvent = _table_key_press
        layout.addWidget(self._table)

        btn_row = QHBoxLayout()
        btn_add_p = QPushButton("+ Lägg till deltagare")
        btn_add_p.clicked.connect(self._add_participant)
        btn_del_p = QPushButton("Ta bort deltagare")
        btn_del_p.setToolTip("Tar bort den markerade raden (deltagaren)")
        btn_del_p.clicked.connect(self._delete_participant)
        btn_add_col = QPushButton("+ Lägg till kolumn")
        btn_add_col.setToolTip("Lägg till en egen namngiven kolumn (t.ex. E-post, Företag, Roll)")
        btn_add_col.clicked.connect(self._add_column)
        btn_del_col = QPushButton("Ta bort kolumn")
        btn_del_col.setToolTip("Tar bort den egna kolumn en markerad cell tillhör")
        btn_del_col.clicked.connect(self._delete_column)
        btn_add_s = QPushButton("+ Lägg till analystillfälle")
        btn_add_s.clicked.connect(self._add_session)
        btn_del_s = QPushButton("Ta bort analystillfälle")
        btn_del_s.setToolTip("Tar bort kolumnen för det tillfälle en markerad cell tillhör")
        btn_del_s.clicked.connect(self._delete_session)
        for b in (btn_add_p, btn_del_p, btn_add_col, btn_del_col, btn_add_s, btn_del_s):
            btn_row.addWidget(b)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.refresh()

    def refresh(self):
        self._loading = True
        try:
            columns = self.db.list_participant_columns()
            sessions = self.db.list_analysis_sessions()
            participants = self.db.list_participants()
            attendance = self.db.get_attendance_matrix()
            col_values = self.db.get_participant_column_values()

            self._column_ids = [c['id'] for c in columns]
            self._session_ids = [s['id'] for s in sessions]
            self._participant_ids = [p['id'] for p in participants]

            headers = (list(self._FIXED_COLS) + [c['name'] for c in columns] +
                       [(s['label'] or f"Tillfälle {s['id']}") for s in sessions])
            self._table.setColumnCount(len(headers))
            self._table.setHorizontalHeaderLabels(headers)
            self._table.setRowCount(len(participants))

            n_custom = len(columns)
            n_fixed = len(self._FIXED_COLS) + n_custom
            for row, p in enumerate(participants):
                self._table.setItem(row, 0, QTableWidgetItem(p['first_name'] or ''))
                self._table.setItem(row, 1, QTableWidgetItem(p['last_name'] or ''))
                for ci, col_def in enumerate(columns):
                    val = col_values.get((p['id'], col_def['id']), '')
                    self._table.setItem(row, len(self._FIXED_COLS) + ci, QTableWidgetItem(val))
                for col, sess in enumerate(sessions):
                    item = QTableWidgetItem()
                    item.setFlags(
                        (item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                        & ~Qt.ItemFlag.ItemIsEditable)
                    attended = attendance.get((p['id'], sess['id']), False)
                    item.setCheckState(
                        Qt.CheckState.Checked if attended else Qt.CheckState.Unchecked)
                    self._table.setItem(row, n_fixed + col, item)
        finally:
            self._loading = False

    def _on_item_changed(self, item):
        if self._loading:
            return
        row, col = item.row(), item.column()
        if row < 0 or row >= len(self._participant_ids):
            return
        pid = self._participant_ids[row]
        n_base = len(self._FIXED_COLS)
        if col == 0:
            self.db.update_participant(pid, first_name=item.text())
        elif col == 1:
            self.db.update_participant(pid, last_name=item.text())
        elif col < n_base + len(self._column_ids):
            col_id = self._column_ids[col - n_base]
            self.db.set_participant_column_value(pid, col_id, item.text())
        else:
            sess_idx = col - n_base - len(self._column_ids)
            if 0 <= sess_idx < len(self._session_ids):
                sess_id = self._session_ids[sess_idx]
                attended = item.checkState() == Qt.CheckState.Checked
                self.db.set_attendance(pid, sess_id, attended)

    def _add_participant(self):
        self.db.add_participant('', '', '')
        self.refresh()

    def _delete_participant(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._participant_ids):
            return
        self.db.delete_participant(self._participant_ids[row])
        self.refresh()

    def _add_column(self):
        name, ok = QInputDialog.getText(
            self, "Ny kolumn", "Kolumnnamn (t.ex. E-post, Företag, Roll):")
        if ok and name.strip():
            self.db.add_participant_column(name.strip())
            self.refresh()

    def _delete_column(self):
        col = self._table.currentColumn()
        col_idx = col - len(self._FIXED_COLS)
        if col < 0 or col_idx < 0 or col_idx >= len(self._column_ids):
            return
        self.db.delete_participant_column(self._column_ids[col_idx])
        self.refresh()

    def _add_session(self):
        dlg = _AnalysisSessionDateDialog(self)
        if dlg.exec():
            self.db.add_analysis_session(dlg.selected_date_label())
            self.refresh()

    def _delete_session(self):
        col = self._table.currentColumn()
        sess_idx = col - len(self._FIXED_COLS) - len(self._column_ids)
        if col < 0 or sess_idx < 0 or sess_idx >= len(self._session_ids):
            return
        self.db.delete_analysis_session(self._session_ids[sess_idx])
        self.refresh()


class _AnalysisSessionDateDialog(QDialog):
    """Date-picker replacement for the old free-text QInputDialog when adding
    an analystillfälle (2026-08-17 user request) — a QDateEdit + "Idag"
    button, same widgets/pattern as the Projekt tab's date-range row."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nytt analystillfälle")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Datum för analystillfället:"))
        row = QHBoxLayout()
        self._date_edit = QDateEdit()
        self._date_edit.setCalendarPopup(True)
        self._date_edit.setDisplayFormat("yyyy-MM-dd")
        self._date_edit.setDate(QDate.currentDate())
        today_btn = QPushButton("Idag")
        today_btn.clicked.connect(lambda: self._date_edit.setDate(QDate.currentDate()))
        row.addWidget(self._date_edit)
        row.addWidget(today_btn)
        lay.addLayout(row)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def selected_date_label(self):
        return self._date_edit.date().toString('yyyy-MM-dd')


class HAZOPPreparationPanel(QWidget):
    """Administrative HAZOP-prep material, collected under its own top-level
    nav entry (2026-08-17, user request: "flytta om flikarna... Skapa en ny
    huvudflik i Claude med namnet HAZOP preperation. Fliken ska samla
    följande administrativa underlag: Projekt, Deltagare, Riskmatris,
    Standardorsaker... Denna fliken ska ligga ute i det svarta fältet till
    vänster högst upp") — these four used to live buried several clicks deep
    as tabs inside Inställningar; extracted here into their own page since
    Anton wanted them front-and-center. Placed at MainWindow.view_stack
    index 0 (see NOTES.md for why: not just visually first in the nav rail,
    Anton explicitly wants it structurally first, so every OTHER page's
    index shifts +1 — see the "_switch_view" renumbering that accompanies
    this class).

    "Riskmatris & Kategorier" brings essentially all of the OLD
    SettingsPanel's own methods along with it (17 of them) — before this
    split, that risk-matrix/palette/category editing WAS almost the entire
    class; SettingsPanel keeps only the tabs that were already their own
    standalone panel classes or simple inline forms unrelated to the matrix.

    Keeps its OWN `matrix_changed` signal (rather than somehow reaching
    across to SettingsPanel's) — SettingsPanel's TagDatabasePanel forwards
    its own settings_changed into a `matrix_changed` of its own for the same
    "please refresh" purpose (MainWindow._on_matrix_changed refreshes tree/
    scenario views generically, not just for matrix edits) — cleanest to let
    each panel own the exact signal for whatever changes it makes, and have
    MainWindow.__init__ connect both to the same handler."""

    matrix_changed = pyqtSignal()
    sheets_changed = pyqtSignal()
    structure_changed = pyqtSignal()   # a node was added/renamed from the Noder tab

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

        # ── Tab: Projekt ──────────────────────────────────────────────────────
        proj_tab = QWidget()
        proj_outer = QVBoxLayout(proj_tab)
        proj_outer.setContentsMargins(0, 0, 0, 0)
        proj_form_w = QWidget()
        pl = QFormLayout(proj_form_w)
        pl.setSpacing(10)
        pl.setContentsMargins(16, 16, 16, 16)
        proj_outer.addWidget(proj_form_w)

        self._proj_name = QLineEdit()
        self._proj_name.editingFinished.connect(
            lambda: self.db.set_config('project_name', self._proj_name.text()))
        pl.addRow("Projektnamn:", self._proj_name)

        self._proj_number = QLineEdit()
        self._proj_number.editingFinished.connect(
            lambda: self.db.set_config('project_number', self._proj_number.text()))
        pl.addRow("Projektnummer:", self._proj_number)

        self._proj_client = QLineEdit()
        self._proj_client.editingFinished.connect(
            lambda: self.db.set_config('project_client', self._proj_client.text()))
        pl.addRow("Kund/Företag:", self._proj_client)

        self._proj_facility = QLineEdit()
        self._proj_facility.editingFinished.connect(
            lambda: self.db.set_config('project_facility', self._proj_facility.text()))
        pl.addRow("Anläggning:", self._proj_facility)

        date_row_w = QWidget()
        date_row_w.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        date_row_l = QHBoxLayout(date_row_w)
        date_row_l.setContentsMargins(0, 0, 0, 0)
        date_row_l.setSpacing(6)
        self._proj_date_start = QDateEdit()
        self._proj_date_start.setCalendarPopup(True)
        self._proj_date_start.setDisplayFormat("yyyy-MM-dd")
        self._proj_date_end = QDateEdit()
        self._proj_date_end.setCalendarPopup(True)
        self._proj_date_end.setDisplayFormat("yyyy-MM-dd")
        _date_edit_w = QFontMetrics(self._proj_date_start.font()).horizontalAdvance(
            "9999-99-99") + 40
        self._proj_date_start.setMaximumWidth(_date_edit_w)
        self._proj_date_end.setMaximumWidth(_date_edit_w)
        self._proj_date_start.dateChanged.connect(
            lambda d: self.db.set_config('project_date_start', d.toString('yyyy-MM-dd')))
        self._proj_date_end.dateChanged.connect(
            lambda d: self.db.set_config('project_date_end', d.toString('yyyy-MM-dd')))
        self._proj_date_start_today_btn = QPushButton("Idag")
        self._proj_date_start_today_btn.setToolTip("Sätt startdatum till dagens datum")
        self._proj_date_start_today_btn.clicked.connect(
            lambda: self._proj_date_start.setDate(QDate.currentDate()))
        self._proj_date_end_today_btn = QPushButton("Idag")
        self._proj_date_end_today_btn.setToolTip("Sätt slutdatum till dagens datum")
        self._proj_date_end_today_btn.clicked.connect(
            lambda: self._proj_date_end.setDate(QDate.currentDate()))
        date_row_l.addWidget(self._proj_date_start)
        date_row_l.addWidget(self._proj_date_start_today_btn)
        date_row_l.addWidget(QLabel("  –  "))
        date_row_l.addWidget(self._proj_date_end)
        date_row_l.addWidget(self._proj_date_end_today_btn)
        pl.addRow("Datum (från–till):", date_row_w)

        # ── Revision: flera rader (Rev/Datum/Beskrivning) ────────────────────
        rev_box = QGroupBox("Revision")
        rev_lay = QVBoxLayout(rev_box)
        self._proj_rev_table = QTableWidget(0, 3)
        self._proj_rev_table.setHorizontalHeaderLabels(["Rev", "Datum", "Beskrivning"])
        self._proj_rev_table.horizontalHeader().setStretchLastSection(True)
        self._proj_rev_table.setColumnWidth(0, 60)
        self._proj_rev_table.setColumnWidth(1, 120)
        self._proj_rev_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._proj_rev_table.customContextMenuRequested.connect(self._proj_rev_context_menu)
        self._proj_rev_table.itemChanged.connect(self._on_proj_rev_item_changed)
        rev_lay.addWidget(self._proj_rev_table)
        rev_btn_row = QHBoxLayout()
        rev_add_btn = QPushButton("+ Lägg till rad")
        rev_add_btn.clicked.connect(self._add_project_revision_row)
        rev_btn_row.addWidget(rev_add_btn)
        rev_btn_row.addStretch()
        rev_lay.addLayout(rev_btn_row)
        proj_outer.addWidget(rev_box)

        # ── Egna fria fält ────────────────────────────────────────────────
        fields_box = QGroupBox("Egna fält")
        self._proj_fields_lay = QVBoxLayout(fields_box)
        self._proj_field_rows = {}   # field id -> (name_edit, value_edit)
        fields_add_btn = QPushButton("+ Lägg till fält")
        fields_add_btn.clicked.connect(self._add_project_custom_field_row)
        fields_btn_row = QHBoxLayout()
        fields_btn_row.addWidget(fields_add_btn)
        fields_btn_row.addStretch()
        self._proj_fields_lay.addLayout(fields_btn_row)
        proj_outer.addWidget(fields_box)
        proj_outer.addStretch()

        tabs.addTab(proj_tab, "Projekt")

        # ── Tab: Deltagare ────────────────────────────────────────────────────
        # Replaces the old free-text "Deltagare" field (2026-08-11, user
        # request: "skulle även gilla ... en till flik med deltagare
        # istället där man definerar förnamn, efternamn, roll på y axel och
        # analystillfälen på x axeln så det blir en matris" — "istället"
        # means this REPLACES the free-text field, not adds to it). See
        # ParticipantMatrixPanel below and NOTES.md for the schema/UI
        # design rationale.
        self._participant_matrix_panel = ParticipantMatrixPanel(self.db)
        tabs.addTab(self._participant_matrix_panel, "Deltagare")

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

        edit_col_btn = QPushButton("Redigera")
        edit_col_btn.setIcon(_icon('edit'))
        edit_col_btn.setFixedHeight(CONFIG['H_ROW_STD'])
        edit_col_btn.clicked.connect(self._palette_edit)
        pal_lay.addWidget(edit_col_btn)

        del_col_btn = QPushButton("Ta bort")
        del_col_btn.setIcon(_icon('delete'))
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
        # Clickable arrows instead of checkboxes (2026-08-17 user request) —
        # QToolButton in checkable mode is a drop-in for QCheckBox here:
        # every other call site only ever touches .isChecked()/.setChecked()/
        # .toggled, which QAbstractButton gives both classes identically, so
        # nothing downstream (_apply_size, _load_matrix_ui, _build_matrix_grid,
        # _save_matrix) needed to change.
        self._x_rev_chk = QToolButton()
        self._x_rev_chk.setCheckable(True)
        self._x_rev_chk.setAutoRaise(True)
        self._y_rev_chk = QToolButton()
        self._y_rev_chk.setCheckable(True)
        self._y_rev_chk.setAutoRaise(True)

        def _update_x_arrow(checked):
            self._x_rev_chk.setText("X ←" if checked else "X →")
            self._x_rev_chk.setToolTip(
                "X-axeln vänd: högt värde till vänster" if checked
                else "X-axeln normal: klicka för att vända (högt värde till vänster)")

        def _update_y_arrow(checked):
            self._y_rev_chk.setText("Y ↑" if checked else "Y ↓")
            self._y_rev_chk.setToolTip(
                "Y-axeln vänd: högst upp" if checked
                else "Y-axeln normal: klicka för att vända (högst upp)")

        self._x_rev_chk.toggled.connect(_update_x_arrow)
        self._y_rev_chk.toggled.connect(_update_y_arrow)
        _update_x_arrow(False)
        _update_y_arrow(False)
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

        save_matrix_btn = QPushButton("Spara riskmatris")
        save_matrix_btn.setIcon(_icon('save', 16, '#ffffff'))
        save_matrix_btn.setStyleSheet(
            "background:#2F5FD0; color:#fff; font-weight:bold; padding:4px 12px;")
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
        btn_up   = QPushButton("▲")
        btn_down = QPushButton("▼")
        btn_up.setToolTip("Flytta vald kategori uppåt")
        btn_down.setToolTip("Flytta vald kategori nedåt")
        btn_up.setFixedWidth(CONFIG['W_ICON_BTN'])
        btn_down.setFixedWidth(CONFIG['W_ICON_BTN'])
        btn_add.clicked.connect(self._cat_add)
        btn_ren.clicked.connect(self._cat_rename)
        btn_del.clicked.connect(self._cat_delete)
        btn_up.clicked.connect(lambda: self._cat_move(-1))
        btn_down.clicked.connect(lambda: self._cat_move(1))
        for b in [btn_add, btn_ren, btn_del, btn_up, btn_down]: cat_btns.addWidget(b)
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
        tabs.addTab(combined_tab, "Riskmatris")

        # ── Tab: Standardorsaker ─────────────────────────────────────────────
        self._std_causes_panel = StandardCausesSettingsPanel(self.db)
        tabs.addTab(self._std_causes_panel, "Avvikelser & Orsaker")

        # ── Tab: Blad (moved from Studiehantering → PID-hantering, 2026-08-17,
        # see NOTES.md) ───────────────────────────────────────────────────────
        sheets_widget = QWidget()
        sheets_layout = QVBoxLayout(sheets_widget)
        sheets_layout.setContentsMargins(8, 8, 8, 8)
        sheets_layout.setSpacing(6)

        sheet_hdr = QHBoxLayout()
        sheet_hdr.addWidget(QLabel("Bladordning — dra för att ändra ordning:"))
        sheet_hdr.addStretch()
        rename_btn = QPushButton("Byt namn")
        rename_btn.setIcon(_icon('edit'))
        rename_btn.clicked.connect(self._rename_sheet)
        sheet_hdr.addWidget(rename_btn)
        delete_btn = QPushButton("Ta bort")
        delete_btn.setIcon(_icon('delete'))
        delete_btn.clicked.connect(self._delete_sheets)
        sheet_hdr.addWidget(delete_btn)
        sheets_layout.addLayout(sheet_hdr)

        rev_row = QHBoxLayout()
        rev_row.addWidget(QLabel("P&ID-revision för valt blad:"))
        self._sheet_rev_combo = QComboBox()
        self._sheet_rev_combo.currentIndexChanged.connect(self._on_sheet_revision_changed)
        rev_row.addWidget(self._sheet_rev_combo, 1)
        sheets_layout.addLayout(rev_row)

        self._sheet_list = QListWidget()
        self._sheet_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self._sheet_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._sheet_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._sheet_list.model().rowsMoved.connect(self._on_sheets_reordered)
        self._sheet_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._sheet_list.customContextMenuRequested.connect(self._sheet_context_menu)
        self._sheet_list.currentItemChanged.connect(self._on_sheet_selection_changed)
        _base_kp = self._sheet_list.keyPressEvent
        def _sheet_key_press(event, _base=_base_kp):
            if event.key() == Qt.Key.Key_Delete:
                self._delete_sheets()
            else:
                _base(event)
        self._sheet_list.keyPressEvent = _sheet_key_press
        sheets_layout.addWidget(self._sheet_list)
        tabs.addTab(sheets_widget, "Blad")

        # ── Tab: Noder ────────────────────────────────────────────────────────
        # Mirrors the HAZOP tree's node list both ways: renaming/creating a
        # node here refreshes the tree via structure_changed, and any tree
        # change that calls this panel's refresh_nodes() shows up here
        # (2026-08-17, see NOTES.md "Ny Noder-flik").
        nodes_widget = QWidget()
        nodes_layout = QVBoxLayout(nodes_widget)
        nodes_layout.setContentsMargins(8, 8, 8, 8)
        nodes_layout.setSpacing(6)
        nodes_hdr = QHBoxLayout()
        nodes_hdr.addWidget(QLabel("Alla noder:"))
        nodes_hdr.addStretch()
        add_node_btn = QPushButton("+ Ny nod")
        add_node_btn.clicked.connect(self._add_node_from_noder_tab)
        nodes_hdr.addWidget(add_node_btn)
        nodes_layout.addLayout(nodes_hdr)
        self._nodes_table = QTableWidget(0, 3)
        self._nodes_table.setHorizontalHeaderLabels(["Nummer", "Namn", "Blad"])
        self._nodes_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._nodes_table.verticalHeader().setVisible(False)
        self._nodes_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._nodes_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._nodes_table.cellDoubleClicked.connect(self._on_nodes_table_double_clicked)
        nodes_layout.addWidget(self._nodes_table)
        tabs.addTab(nodes_widget, "Noder")

        self._load_all()

    def _load_all(self):
        self._load_matrix_ui()
        self._load_palette_ui()
        self._load_categories()
        self._proj_name.setText(self.db.get_config('project_name', ''))
        self._proj_number.setText(self.db.get_config('project_number', ''))
        self._proj_client.setText(self.db.get_config('project_client', ''))
        self._proj_facility.setText(self.db.get_config('project_facility', ''))

        today = QDate.currentDate()
        start_str = self.db.get_config('project_date_start', '')
        end_str   = self.db.get_config('project_date_end', '')
        start_d = QDate.fromString(start_str, 'yyyy-MM-dd') if start_str else QDate()
        end_d   = QDate.fromString(end_str, 'yyyy-MM-dd') if end_str else QDate()
        self._proj_date_start.setDate(start_d if start_d.isValid() else today)
        self._proj_date_end.setDate(end_d if end_d.isValid() else today)

        self._load_project_revisions()
        self._load_project_custom_fields()
        self.refresh_sheets()
        self.refresh_nodes()

    # ── Blad (2026-08-17, moved from PIDManagementPanel, see NOTES.md) ──────
    def refresh_sheets(self):
        self._sheet_rev_combo.blockSignals(True)
        self._sheet_rev_combo.clear()
        self._sheet_rev_combo.addItem("(ingen)", None)
        for rev in self.db.get_revisions():
            self._sheet_rev_combo.addItem(rev['revision'] or f"Revision {rev['id']}", rev['id'])
        self._sheet_rev_combo.blockSignals(False)

        self._sheet_list.clear()
        for sheet in self.db.get_sheets():
            item = QListWidgetItem(
                f"{sheet['display_order'] + 1}. {sheet['sheet_name']}  "
                f"(PDF-sida {sheet['physical_page'] + 1})")
            item.setData(Qt.ItemDataRole.UserRole, sheet['id'])
            item.setData(Qt.ItemDataRole.UserRole + 1, sheet['revision_id'])
            nodes = self.db.nodes_on_page(sheet['physical_page'])
            if nodes:
                names = ', '.join(n['name'] or f"Nod {n['id']}" for n in nodes)
                item.setToolTip(f"Noder på detta blad: {names}")
            self._sheet_list.addItem(item)

    def _on_sheets_reordered(self, *_):
        ids = [self._sheet_list.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self._sheet_list.count())]
        self.db.reorder_sheets(ids)
        self.refresh_sheets()

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
            self.refresh_sheets()

    def _delete_sheets(self):
        selected = self._sheet_list.selectedItems()
        if not selected:
            return
        ids = [item.data(Qt.ItemDataRole.UserRole) for item in selected]

        all_sheets = {s['id']: s for s in self.db.get_sheets()}
        pages_info = [(ids[i], all_sheets[ids[i]]['physical_page'],
                       all_sheets[ids[i]]['sheet_name'])
                      for i in range(len(ids)) if ids[i] in all_sheets]
        physical_pages = [p for _, p, _ in pages_info]

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
        self.refresh_sheets()
        self.sheets_changed.emit()

    def _sheet_context_menu(self, pos):
        selected = self._sheet_list.selectedItems()
        if not selected:
            return
        menu = QMenu(self)
        if len(selected) == 1:
            menu.addAction(_icon('edit'), "Byt namn", self._rename_sheet)
        menu.addAction(_icon('delete'), "Ta bort", self._delete_sheets)
        menu.exec(self._sheet_list.viewport().mapToGlobal(pos))

    def _on_sheet_selection_changed(self, current, previous):
        self._sheet_rev_combo.blockSignals(True)
        if current is None:
            self._sheet_rev_combo.setCurrentIndex(0)
        else:
            rev_id = current.data(Qt.ItemDataRole.UserRole + 1)
            idx = self._sheet_rev_combo.findData(rev_id)
            self._sheet_rev_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._sheet_rev_combo.blockSignals(False)

    def _on_sheet_revision_changed(self, _index):
        item = self._sheet_list.currentItem()
        if item is None:
            return
        sheet_id = item.data(Qt.ItemDataRole.UserRole)
        rev_id = self._sheet_rev_combo.currentData()
        self.db.set_sheet_revision(sheet_id, rev_id)
        item.setData(Qt.ItemDataRole.UserRole + 1, rev_id)

    # ── Noder (2026-08-17, see NOTES.md "Ny Noder-flik") ─────────────────────
    def refresh_nodes(self):
        sheets_by_page = {s['physical_page']: s['sheet_name'] for s in self.db.get_sheets()}
        nodes = self.db.nodes()
        self._nodes_table.setRowCount(len(nodes))
        for row, node in enumerate(nodes):
            self._nodes_table.setItem(row, 0, QTableWidgetItem(str(node['id'])))
            name_item = QTableWidgetItem(node['name'] or f"Nod {node['id']}")
            name_item.setData(Qt.ItemDataRole.UserRole, node['id'])
            self._nodes_table.setItem(row, 1, name_item)
            pages = self.db.pages_for_node(node['id'])
            sheet_names = [sheets_by_page.get(p, f"sida {p + 1}") for p in pages]
            self._nodes_table.setItem(row, 2, QTableWidgetItem(', '.join(sheet_names)))

    def _add_node_from_noder_tab(self):
        self.db.add_node()
        self.refresh_nodes()
        self.structure_changed.emit()

    def _on_nodes_table_double_clicked(self, row, col):
        if col != 1:
            return
        item = self._nodes_table.item(row, 1)
        if item is None:
            return
        node_id = item.data(Qt.ItemDataRole.UserRole)
        node = self.db.get_node(node_id)
        if not node:
            return
        name, ok = QInputDialog.getText(self, "Döp om nod", "Nytt namn:",
                                         text=node['name'] or '')
        if not ok or not name.strip():
            return
        self.db.update_node(node_id, name.strip(), node.get('description') or '',
                             node.get('pid_ref') or '', node.get('media') or '',
                             node.get('pressure') or '', node.get('temperature') or '')
        self.refresh_nodes()
        self.structure_changed.emit()

    def _next_revision_letter(self):
        n = len(self.db.project_revisions())
        letters = ''
        n1 = n
        while True:
            letters = chr(65 + n1 % 26) + letters
            n1 = n1 // 26 - 1
            if n1 < 0:
                break
        return letters

    def _load_project_revisions(self):
        self._proj_rev_table.blockSignals(True)
        rows = self.db.project_revisions()
        self._proj_rev_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            item_label = QTableWidgetItem(row['label'])
            item_label.setData(Qt.ItemDataRole.UserRole, row['id'])
            self._proj_rev_table.setItem(r, 0, item_label)
            date_edit = QDateEdit()
            date_edit.setCalendarPopup(True)
            date_edit.setDisplayFormat("yyyy-MM-dd")
            d = QDate.fromString(row['date'], 'yyyy-MM-dd')
            date_edit.setDate(d if d.isValid() else QDate.currentDate())
            date_edit.dateChanged.connect(
                lambda d, id_=row['id']: self.db.update_project_revision(
                    id_, date=d.toString('yyyy-MM-dd')))
            self._proj_rev_table.setCellWidget(r, 1, date_edit)
            item_desc = QTableWidgetItem(row['description'])
            item_desc.setData(Qt.ItemDataRole.UserRole, row['id'])
            self._proj_rev_table.setItem(r, 2, item_desc)
        self._proj_rev_table.blockSignals(False)

    def _add_project_revision_row(self):
        label = self._next_revision_letter()
        self.db.add_project_revision(label, QDate.currentDate().toString('yyyy-MM-dd'), '')
        self._load_project_revisions()

    def _on_proj_rev_item_changed(self, item):
        id_ = item.data(Qt.ItemDataRole.UserRole)
        if id_ is None:
            return
        if item.column() == 0:
            self.db.update_project_revision(id_, label=item.text())
        elif item.column() == 2:
            self.db.update_project_revision(id_, description=item.text())

    def _proj_rev_context_menu(self, pos):
        item = self._proj_rev_table.itemAt(pos)
        if item is None:
            return
        id_ = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        del_action = menu.addAction("Ta bort rad")
        action = menu.exec(self._proj_rev_table.viewport().mapToGlobal(pos))
        if action == del_action and id_ is not None:
            self.db.delete_project_revision(id_)
            self._load_project_revisions()

    def _load_project_custom_fields(self):
        for name_edit, value_edit, row_w in self._proj_field_rows.values():
            self._proj_fields_lay.removeWidget(row_w)
            row_w.deleteLater()
        self._proj_field_rows = {}
        for field in self.db.project_custom_fields():
            self._add_project_custom_field_widget(field['id'], field['name'], field['value'])

    def _add_project_custom_field_widget(self, id_, name, value):
        row_w = QWidget()
        row_l = QHBoxLayout(row_w)
        row_l.setContentsMargins(0, 0, 0, 0)
        name_edit = QLineEdit(name)
        name_edit.setPlaceholderText("Fältnamn")
        value_edit = QLineEdit(value)
        value_edit.setPlaceholderText("Värde")
        del_btn = QPushButton("✕")
        del_btn.setFixedWidth(28)
        name_edit.editingFinished.connect(
            lambda: self.db.update_project_custom_field(id_, name=name_edit.text()))
        value_edit.editingFinished.connect(
            lambda: self.db.update_project_custom_field(id_, value=value_edit.text()))
        del_btn.clicked.connect(lambda: self._delete_project_custom_field(id_))
        row_l.addWidget(name_edit)
        row_l.addWidget(value_edit)
        row_l.addWidget(del_btn)
        self._proj_fields_lay.insertWidget(self._proj_fields_lay.count() - 1, row_w)
        self._proj_field_rows[id_] = (name_edit, value_edit, row_w)

    def _add_project_custom_field_row(self):
        id_ = self.db.add_project_custom_field('', '')
        self._add_project_custom_field_widget(id_, '', '')

    def _delete_project_custom_field(self, id_):
        self.db.delete_project_custom_field(id_)
        name_edit, value_edit, row_w = self._proj_field_rows.pop(id_)
        self._proj_fields_lay.removeWidget(row_w)
        row_w.deleteLater()

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
            # QLineEdit.setText() leaves the cursor at the END of the text —
            # for a label wider than the fixed 80px field (e.g. "< 0.1/år"
            # at 8px font measures ~96px, see NOTES.md "'<'-tecknet syns
            # inte i gränsvärden"), the widget auto-scrolls to keep the
            # cursor visible, which scrolls the leading "<"/"≥" out of view.
            # Reset to show from the start instead (2026-08-17).
            e.setCursorPosition(0)
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
            ey.setCursorPosition(0)   # see column-header comment above
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
            # r -> list of this row's category QTextEdits, used below to size
            # the row header + cell buttons + category cells all to the
            # tallest wrapped text in that row (2026-08-17 user request —
            # only this orientation needed it, the `not freq_on_x` branch
            # above already had a working fixed row height).
            row_cat_edits = [[] for _ in range(n_drows)]

            for cat_i, cat in enumerate(cats):
                cat_id  = cat['id']
                cat_col = base_col + cat_i

                cat_hdr = QLabel(cat['name'])
                cat_hdr.setStyleSheet(_cat_hdr_style)
                cat_hdr.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cat_hdr.setMinimumHeight(CONFIG['H_ROW_STD'])
                cat_hdr.setMinimumWidth(130)
                cat_hdr.setWordWrap(True)
                self._matrix_grid.addWidget(cat_hdr, 0, cat_col)

                for r in range(n_drows):      # n_drows = n_cons
                    disp_r    = (n_drows - 1 - r) if not y_rev else r
                    sev_level = disp_r + 1
                    text = defs.get(sev_level, {}).get(cat_id, '')
                    e = QTextEdit()
                    e.setPlainText(text)
                    e.setFixedWidth(130)
                    e.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
                    e.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                    e.setStyleSheet(_def_style)
                    e.setPlaceholderText("—")
                    e.textChanged.connect(
                        lambda cid=cat_id, sl=sev_level, _e=e:
                        self.db.set_severity_definition(sl, cid, _e.toPlainText().strip()))
                    self._matrix_grid.addWidget(e, r + 1, cat_col)
                    self._sev_def_edits[(cat_id, sev_level)] = e
                    row_cat_edits[r].append(e)

            for r in range(n_drows):
                if not row_cat_edits[r]:
                    continue
                needed = CONFIG['H_ROW_STD']
                for e in row_cat_edits[r]:
                    doc = e.document()
                    doc.setTextWidth(e.width())
                    needed = max(needed, int(doc.size().height()) + 8)
                self._y_label_edits[r].setFixedHeight(needed)
                for btn in self._cell_buttons[r][1]:
                    btn.setFixedHeight(needed)
                for e in row_cat_edits[r]:
                    e.setFixedHeight(needed)

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
                    self._x_label_edits[affected_c].setCursorPosition(0)

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
                    e.setCursorPosition(0)
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
                    e.setCursorPosition(0)
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
        # 2026-08-11 fix ('När jag ... tar bort en konsekvenskategori skall
        # detta synas i riskmatrisen direkt') — _cat_add/_cat_rename already
        # called _apply_size() to rebuild the matrix grid; delete was
        # missing this call, so the matrix kept showing the deleted
        # category's severity-definition row until the next unrelated
        # rebuild (e.g. resizing the rows/cols spinners).
        self._apply_size()
        if hasattr(self, '_sev_def_panel') and self._sev_def_panel:
            self._sev_def_panel.refresh()

    def _cat_move(self, direction):
        """Move the selected category up (direction=-1) or down (+1) in
        display order (2026-08-11, 'jag vill även kunna justera ordningen,
        exempelvis genom vilken ordning de dyker upp')."""
        item = self._cat_list.currentItem()
        if not item:
            return
        row = self._cat_list.row(item)
        new_row = row + direction
        if not (0 <= new_row < self._cat_list.count()):
            return
        ordered_ids = [self._cat_list.item(i).data(Qt.ItemDataRole.UserRole)
                       for i in range(self._cat_list.count())]
        ordered_ids[row], ordered_ids[new_row] = ordered_ids[new_row], ordered_ids[row]
        self.db.reorder_categories(ordered_ids)
        self._load_categories()
        self._cat_list.setCurrentRow(new_row)
        self._apply_size()


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════════════

class SettingsPanel(QWidget):
    """P&ID-scoped and tag-recognition settings. Used to also host Projekt/
    Deltagare/Riskmatris & Kategorier/Standardorsaker — those four moved out
    into their own top-level HAZOPPreparationPanel (2026-08-17, see NOTES.md)
    since Anton wanted them front-and-center in the nav rail rather than
    buried as tabs here. Keeps its own `matrix_changed` (TagDatabasePanel's
    settings_changed still forwards into it, unrelated to the risk matrix
    itself) — see HAZOPPreparationPanel's own docstring for why this signal
    is duplicated across both panels rather than shared."""

    matrix_changed = pyqtSignal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db

        tabs = QTabWidget()
        self._tabs = tabs   # kept as an attribute for testability (tabText() lookups)
        main = QVBoxLayout(self)
        main.addWidget(tabs)

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

        # ── Tab: Standardobjekt ───────────────────────────────────────────────
        self._std_objects_panel = StandardObjectsSettingsPanel(self.db)
        tabs.addTab(self._std_objects_panel, "Standardobjekt")

        # ── Tab: Smart igenkänning ────────────────────────────────────────────
        self._tag_memory_panel = TagMemoryPanel(self.db)
        tabs.addTab(self._tag_memory_panel, _icon('brain'), "Smart igenkänning")
        tabs.currentChanged.connect(
            lambda i: self._tag_memory_panel.refresh()
            if tabs.widget(i) is self._tag_memory_panel else None)

        self._load_all()

    def _load_all(self):
        self._strip_spaces_chk.setChecked(
            self.db.get_config('tag_strip_spaces', '1') == '1')

        idx = self._ocr_default_combo.findData(self.db.get_config('ocr_default_engine', 'ask'))
        if idx >= 0:
            self._ocr_default_combo.setCurrentIndex(idx)
        idx = self._page_orientation_combo.findData(
            self.db.get_config('pid_page_orientation_hint', 'auto'))
        if idx >= 0:
            self._page_orientation_combo.setCurrentIndex(idx)

    def refresh_tag_memory(self):
        """Refresh the Smart igenkänning tab so newly learned tags show up."""
        self._tag_memory_panel.refresh()


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
        clear_all_btn = QPushButton("Rensa samtliga P&ID och all data")
        clear_all_btn.setIcon(_icon('delete'))
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

        # "Blad" (sheet ordering/rename/delete) moved out to
        # HAZOPPreparationPanel (2026-08-17, see NOTES.md) — this panel now
        # only manages revision history. sheets_changed is still emitted
        # from here by _clear_all_pid (which also wipes sheets), so the
        # moved Blad list can still react to a full-clear from this tab.
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
        refresh_btn = QPushButton("Uppdatera")
        refresh_btn.setIcon(_icon('refresh'))
        refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(refresh_btn)
        backup_btn = QPushButton("Skapa säkerhetskopia nu")
        backup_btn.setIcon(_icon('save'))
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
        #   Analys       — Statistik / Godkänn
        #   Inställningar — Mörkt läge
        # Node/deviation/delete actions moved to a button row in TreePanel
        # instead (see TreePanel.__init__), since they act on the tree
        # selection rather than the document as a whole.
        mb = self.menuBar()
        file_menu = mb.addMenu("Fil")
        file_menu.addAction(_icon('document'), "Nytt projekt",      self._hzp_new)
        file_menu.addAction(_icon('import'), "Öppna (.hzp)…",     self._hzp_open)
        file_menu.addSeparator()
        self._act_save = file_menu.addAction(_icon('save'), "Spara",         self._hzp_save)
        file_menu.addAction(_icon('save'), "Spara som…",         self._hzp_save_as)
        file_menu.addSeparator()
        file_menu.addAction(_icon('print'), "Skriv ut",           self._print_scenario_table)
        file_menu.addSeparator()
        file_menu.addAction(_icon('close'), "Avsluta",            self.close)

        export_menu = mb.addMenu("Export")
        export_menu.addAction(_icon('chart'), "Excel",           self._export_excel)
        export_menu.addAction(_icon('document'), "PDF",             self._export_pdf)
        export_menu.addAction(_icon('clipboard'), "Åtgärder",        self._export_actions_pdf)

        analysis_menu = mb.addMenu("Analys")
        analysis_menu.addAction(_icon('trend-chart'), "Statistik",     self._show_statistics)
        analysis_menu.addAction(_icon('check'), "Godkänn",       self._approve_node)

        settings_menu = mb.addMenu("Inställningar")
        settings_menu.addAction(_icon('moon'), "Mörkt läge",    self._toggle_dark_mode)

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

        def _rail_icon(name):
            # Inverted two-state icon for the dark nav rail (2026-08-12,
            # see NOTES.md) — white on the dark unchecked background,
            # near-black on the white checked background. The opposite
            # color pairing from _mk_icon()'s own dark/white convention
            # (built for light-background toolbars), so built inline here
            # rather than reusing it.
            ic = QIcon()
            ic.addPixmap(_mk_pm(name, 18, QColor('#ffffff')), QIcon.Mode.Normal, QIcon.State.Off)
            ic.addPixmap(_mk_pm(name, 18, QColor('#17191C')), QIcon.Mode.Normal, QIcon.State.On)
            return ic

        # "HAZOP preparation" (2026-08-17, see NOTES.md) — Projekt/Deltagare/
        # Riskmatris & Kategorier/Standardorsaker, extracted out of
        # Inställningar into their own top-level page. Deliberately FIRST in
        # both the nav rail (visual order) AND view_stack (structural index
        # 0, per Anton's explicit request) — every other page's index shifts
        # +1 accordingly; see _switch_view and every other hardcoded
        # _switch_view(N) call elsewhere in this file, all renumbered
        # together with this change.
        self.btn_prep      = QPushButton()
        self.btn_pid       = QPushButton()
        self.btn_sheet     = QPushButton()
        self.btn_equip     = QPushButton()
        self.btn_admin     = QPushButton()
        self.btn_settings  = QPushButton()

        _nav_labels = {
            self.btn_prep:     "HAZOP preparation",
            self.btn_pid:      "P&ID-vy",
            self.btn_sheet:    "Worksheet",
            self.btn_equip:    "Utrustning",
            self.btn_admin:    "Studiehantering",
            self.btn_settings: "Inställningar",
        }
        _nav_icons = {
            self.btn_prep:     'check',
            self.btn_pid:      'map',
            self.btn_sheet:    'clipboard',
            self.btn_equip:    'bolt-nut',
            self.btn_admin:    'document',
            self.btn_settings: 'settings',
        }

        for btn in (self.btn_prep, self.btn_pid, self.btn_sheet, self.btn_equip,
                    self.btn_admin, self.btn_settings):
            btn.setCheckable(True)
            btn.setFixedSize(40, 40)
            btn.setToolTip(_nav_labels[btn])
            btn.setIcon(_rail_icon(_nav_icons[btn]))
            btn.setIconSize(QSize(18, 18))
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
        self.btn_prep.clicked.connect(lambda: self._switch_view(0))
        self.btn_pid.clicked.connect(lambda: self._switch_view(1))
        self.btn_sheet.clicked.connect(lambda: self._switch_view(2))
        self.btn_equip.clicked.connect(lambda: self._switch_view(3))
        self.btn_admin.clicked.connect(lambda: self._switch_view(4))
        self.btn_settings.clicked.connect(lambda: self._switch_view(5))

        # ── Page 0: HAZOP preparation ────────────────────────────────────────
        # Added FIRST so it becomes view_stack index 0 (QStackedWidget numbers
        # pages in addWidget() call order) — must precede every other
        # addWidget() call below, not just visually lead the nav rail.
        self.hazop_prep_panel = HAZOPPreparationPanel(self.db)
        self.view_stack.addWidget(self.hazop_prep_panel)

        # ── Page 1: P&ID view ─────────────────────────────────────────────────
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

        # ── Page 2: Worksheet ─────────────────────────────────────────────────
        self.worksheet = HAZOPWorksheet(self.db)
        self.view_stack.addWidget(self.worksheet)

        # ── Page 3: Equipment ─────────────────────────────────────────────────
        self.equipment_panel = EquipmentPanel(self.db)
        self.equipment_panel.markers_saved.connect(self.pid_panel.reload_overlays)
        self.view_stack.addWidget(self.equipment_panel)

        # ── Page 4: Study management ──────────────────────────────────────────
        self.admin_panel = StudyManagementPanel(self.db)
        self.view_stack.addWidget(self.admin_panel)

        # ── Page 5: Settings ──────────────────────────────────────────────────
        self.settings_panel = SettingsPanel(self.db)
        self.settings_panel.matrix_changed.connect(self._on_matrix_changed)
        self.hazop_prep_panel.matrix_changed.connect(self._on_matrix_changed)
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
        # Noder tab (HAZOPPreparationPanel, 2026-08-17) must reflect node
        # add/rename/delete that originated in the tree, not just its own edits.
        self.tree_panel.structure_changed.connect(self.hazop_prep_panel.refresh_nodes)
        # Inline tree editing (2026-08-17, see NOTES.md) — mirrors
        # scenario_panel.item_edited's own connection (_on_scenario_item_edited)
        # but in the opposite direction: an edit made IN THE TREE must also
        # refresh the scenario table + P&ID overlays (bold tag references,
        # markers/tooltips) since they all read from the same DB rows.
        self.tree_panel.item_edited_inline.connect(
            lambda type_, id_: (self.scenario_panel.refresh(),
                                 self.pid_panel.reload_overlays()))
        self.tree_panel.item_edited_inline.connect(self.hazop_prep_panel.refresh_nodes)
        self.tree_panel.visibility_changed.connect(
            lambda t, v: self.pid_panel.viewer.set_marker_visibility(t, v))

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
        self.scenario_panel.structure_changed.connect(
            lambda: (self.tree_panel.refresh(), self.pid_panel.reload_overlays()))

        self.tree_panel.equipment_dropped_on_deviation.connect(
            self._on_equipment_dropped_on_deviation)
        self.tree_panel.edit_node_markup_requested.connect(self._on_edit_node_markup)
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
        self.node_markup_panel.bottom_panel_toggled.connect(self._on_toggle_bottom_panel)
        self.node_markup_panel.place_symbol_requested.connect(self._on_place_symbol_requested)
        self._return_to_node_markup_node_id = None
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
        self.pid_panel.ref_tag_picked.connect(self._on_ref_tag_picked)
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
                # Stay on the equipment filter if one is active (2026-08-12,
                # see NOTES.md: 'en mindre fix är att det skall se ut såhär
                # även när jag klickar på en rödmarkerad och lägger till
                # exempelvis lågt och högt flöde') — this handler also fires
                # for the auto-created cause right after
                # _on_equipment_deviation_created's own load_equipment()
                # call (checking a deviation box immediately suggests a
                # cause), so an unconditional load_node() here would
                # silently widen the just-filtered worksheet back out on
                # every single checkbox toggle.
                if self.scenario_panel.get_equipment_filter() is not None:
                    self.scenario_panel.refresh()
                else:
                    self.scenario_panel.load_node(node_id)
            self.scenario_panel.refresh_placed()
            # select_cause() itself refuses to steal the current cell from
            # an active edit / a row the user already navigated to (see
            # ScenarioTablePanel.select_cause) — this deferral just lets
            # the rebuild above settle before it scans _row_meta.
            QTimer.singleShot(0, lambda c=cid: self.scenario_panel.select_cause(c))
            # Refresh Smart Recognition panel so new learning is immediately visible
            try:
                self.settings_panel.refresh_tag_memory()
            except Exception:
                pass
        self.pid_panel.cause_template_created.connect(_on_cause_template_created)
        self.pid_panel.equipment_placement_requested.connect(self._on_equipment_placement_requested)
        self.pid_panel.equipment_edit_requested.connect(self._on_equipment_edit_requested)
        self.pid_panel.marker_navigated.connect(self._on_marker_navigate)
        # Shift+click a marker while an ORS/KON/SG cell is being edited
        # inserts the tag into the open editor instead of navigating
        # away and destroying it (2026-08-13, see NOTES.md).
        self.pid_panel._active_edit_query_fn = self.scenario_panel.active_edit_target
        # Renaming an equipment via the ORS tag strip (2026-08-13, see
        # NOTES.md) reaches into equipment_catalog directly from
        # ScenarioTablePanel — keep the P&ID markers' own overlay text
        # in sync right away too, not just on their next unrelated redraw.
        self.scenario_panel.equipment_renamed.connect(self.pid_panel.reload_overlays)
        self.pid_panel.equipment_deviation_created.connect(self._on_equipment_deviation_created)
        self.pid_panel.pid_analysis_done.connect(self._on_pid_analysis_done)
        self.admin_panel._pid_mgmt.sheets_changed.connect(self._on_sheets_changed)
        # Blad moved to HAZOPPreparationPanel (2026-08-17, see NOTES.md) — it
        # needs its own sheets_changed wired to the same reload, AND to
        # refresh when "Rensa samtliga P&ID" (still in Revisioner) wipes
        # sheets out from under it.
        self.hazop_prep_panel.sheets_changed.connect(self._on_sheets_changed)
        self.admin_panel._pid_mgmt.sheets_changed.connect(self.hazop_prep_panel.refresh_sheets)
        self.hazop_prep_panel.structure_changed.connect(self._on_hazop_prep_structure_changed)

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
        # Index 1, not 0, since HAZOP preparation (2026-08-17, see NOTES.md)
        # is now index 0 — every branch below shifted +1 accordingly.
        self._v_splitter.setVisible(page == 1)
        self.btn_prep.setChecked(page == 0)
        self.btn_pid.setChecked(page == 1)
        self.btn_sheet.setChecked(page == 2)
        self.btn_equip.setChecked(page == 3)
        self.btn_admin.setChecked(page == 4)
        self.btn_settings.setChecked(page == 5)
        if page == 1 and prev != 1:
            self.pid_panel.reload_overlays()
        if page == 2: self.worksheet.refresh()
        if page == 3: self.equipment_panel.refresh()
        if page == 4:
            self.admin_panel.refresh()
            self.admin_panel.refresh_pid()
        if page == 5:
            # Guard against settings_panel not being initialized yet
            if hasattr(self, 'settings_panel') and self.settings_panel:
                self.settings_panel.refresh_tag_memory()

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
            # A node rename here (PropertiesRibbon's Namn-popup) already
            # updated node_markups via Database.update_node() (2026-08-17,
            # see NOTES.md), but nothing else on this path re-draws the
            # P&ID — without this the on-canvas "Lägg ut nodnamn" label
            # stays visibly stale until some unrelated action happens to
            # trigger reload_overlays().
            if self._cur_type == NODE_T:
                self.pid_panel.refresh_markup_overlays()
        self.scenario_panel.rebuild()

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
        self.scenario_panel.clear()
        self.pid_panel.clear_active_selection()
        self.pid_panel.reload_overlays()

    def _on_sheets_changed(self):
        """Reload the study board after sheets are added or deleted."""
        self.pid_panel.try_reload_pdf()

    def _on_hazop_prep_structure_changed(self):
        """A node was added/renamed from HAZOPPreparationPanel's Noder tab
        (2026-08-17) — the tree doesn't know about it on its own since the
        edit didn't originate there, unlike TreePanel's own structure_changed."""
        self.tree_panel.refresh()
        self._on_structure_changed()

    def _on_pid_analysis_done(self):
        """Switch to Settings → Identifierade objekt after P&ID analysis,
        then offer to chain straight into '🎯 Hitta objekt på P&ID' (2026-08-11):
        'Efter jag klickat på analysera P&ID vill jag få upp samma popupruta
        som innan, sedan vill jag att en popupfråga om jag vill hitta objekt
        på P&ID ska komma upp. Då ska samma körning som "hitta objekt på
        P&ID" knappen köras.' The 'Analys klar' popup itself is unchanged
        (shown earlier, in PIDPanel._analyze_pid, before this signal fires);
        this only adds the follow-up confirm + chained run."""
        self._switch_view(5)   # Settings page
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
            self.equipment_panel.autodetect()

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
            self._switch_view(3)
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
        """Clicking an already-placed (red or green) equipment marker on the
        P&ID should surface ONLY the HAZOP scenario rows that mention it —
        causes, consequences AND safeguards (2026-08-12, see NOTES.md: 'de
        orsaker som visas i hazop scenario är de där objektet finns med').
        Filtered via scenario_panel.load_equipment(), not the whole node —
        an earlier version of this feature (2026-08-11) showed the entire
        node instead, which the user clarified was too broad.

        No _switch_view() call is needed: the marker can only be clicked
        while already on the P&ID page, and the scenario table is the
        bottom pane of that same page.
        """
        row = self.db.conn.execute(
            "SELECT equipment_id FROM equipment_markers WHERE id=?", (marker_id,)).fetchone()
        if not row or row['equipment_id'] is None:
            return
        equipment_id = row['equipment_id']
        node_id = self.db.equipment_node_id(equipment_id)
        if node_id is not None:
            self.tree_panel.refresh(NODE_T, node_id, emit_selection=False)
        self.scenario_panel.load_equipment(equipment_id)
        self.scenario_panel.refresh_placed()
        self.equipment_panel.select_row_by_equipment_id(equipment_id)

    def _on_equipment_deviation_created(self, deviation_id, equipment_id):
        """A deviation was checked (or unchecked) on in EquipmentDeviationBar
        — refresh the tree (new LEDORD_T/EQUIP_T/DEV_T items) and worksheet.

        emit_selection=False (2026-08-07 fix, real crash-adjacent bug
        report: "kan inte lägga till konsekvens"): the original version
        called tree_panel.refresh(DEV_T, deviation_id) with the default
        emit_selection=True, which cascades into _on_selected(DEV_T, ...)
        -> scenario_panel.load_deviation(...) — narrowing the worksheet to
        just THIS ONE deviation on every single checkbox toggle. That hid
        sibling deviations for the same node/equipment (the user's "I want
        to see BOTH deviations" complaint) and left keyboard focus inside
        the bar's comboboxes, so the worksheet's Enter-to-add-consequence
        shortcut never fired (the user's "I can't add a consequence"
        complaint) — same emit_selection anti-pattern already fixed
        elsewhere per commit 84c8b7c (see _on_props_changed,
        _on_safeguard_created, _on_marker_navigate).

        scenario_panel.load_equipment(), not load_node() (2026-08-12, see
        NOTES.md): if the worksheet was already filtered to this equipment
        (the user just clicked its red/green marker), checking a deviation
        box in the bar used to silently widen it back to the whole node —
        'en mindre fix är att det skall se ut såhär även när jag klickar på
        en rödmarkerad och lägger till exempelvis lågt och högt flöde. Då
        skall det enbart vara kopplat till det objektet.' load_equipment()
        still shows every deviation under THIS equipment together (same
        "not narrowed to one deviation" property load_node() had), just
        without pulling in the rest of the node's unrelated causes too.
        """
        self.tree_panel.refresh(DEV_T, deviation_id, emit_selection=False)
        self.scenario_panel.load_equipment(equipment_id)
        self.scenario_panel.refresh_placed()

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

    def _on_equipment_edit_requested(self, marker_id):
        """Right-click "✏️ Redigera objekt" on an existing equipment marker
        (2026-08-12, see NOTES.md — reported feedback: "jag vill kunna
        högerklicka på ett objekt och kunna editera det, både tagnummer
        och vad det är för typ av utrustning"). Same EquipmentTagPopup as
        placing a brand new object, just pre-filled from and writing back
        to the existing equipment_catalog row instead of creating a new
        marker/placement."""
        equip = self.db.get_equipment_by_marker_id(marker_id)
        if not equip:
            QMessageBox.information(self, "Inget objekt",
                "Den här markören är inte kopplad till något registrerat objekt.")
            return
        popup = EquipmentTagPopup(self.db, suggested_tag=equip.get('tag') or '',
                                  suggested_type=equip.get('equipment_type') or '',
                                  parent=self)

        def _on_picked(tag, comp_type):
            tag = tag.strip().upper()
            pfx = _tag_prefix(tag) if tag else (equip.get('prefix') or '')
            self.db.update_equipment_item(
                equip['id'], tag, pfx, comp_type, equip.get('description') or '')
            self.pid_panel.reload_overlays()
            # Live tag link (2026-08-13, see NOTES.md) — any ORS tag
            # strip linked to this equipment resolves its display live
            # via causes.equipment_id, so a rebuild here shows the new
            # name immediately instead of on the next unrelated redraw.
            self.scenario_panel.schedule_rebuild()

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
                self.db, dev_id, equip.get('equipment_type', ''), equip.get('tag', ''),
                equipment_id=equip['id'])
            last_cause_id = cause_id
        if last_cause_id is not None:
            self.tree_panel.refresh(CAUSE_T, last_cause_id)
            self.scenario_panel.refresh_placed()
            if node_id is not None:
                self.scenario_panel.load_node(node_id)

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

            initial_tag = ''
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
            dlg.move(self.scenario_panel.position_near_row(cons_id, dlg.sizeHint()))
            self._active_step_picker = dlg
            logging.info('_open_consequence_step_picker: calling dlg.exec()')
            result = dlg.exec()
            logging.info('_open_consequence_step_picker: dlg.exec() returned %s', result)
            if result == QDialog.DialogCode.Accepted:
                self._active_step_picker = None
                logging.info('_open_consequence_step_picker: accepted — calling scenario_panel.rebuild()')
                try:
                    self.scenario_panel.rebuild()
                    logging.info('_open_consequence_step_picker: _rebuild() done')
                except Exception:
                    logging.exception('_open_consequence_step_picker: CRASH in _rebuild()')
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
        """Tree right-click NODE → 'Editera nodmarkup'.

        2026-08-17 (see NOTES.md "nodmarkup dockas till höger"): this used
        to hide tree_panel/props_ribbon/scenario_panel entirely, replacing
        almost the whole window with just the P&ID canvas + a narrow
        ribbon — Anton wanted the panel to feel docked alongside the rest
        of the app instead. tree_panel and props_ribbon now stay visible
        (node_markup_panel already sits to their right in _h_splitter's
        add-order, so nothing needed to move); only the BOTTOM strip still
        swaps scenario_panel out for markup_table_panel by default, since
        showing a tag-picker table meant for editing HAZOP causes at the
        same time as drawing node markup would be confusing — the new
        toggle button lets the user bring scenario_panel back without
        leaving markup-edit mode."""
        self._switch_view(1)
        self.node_markup_panel.load(node_id)
        self.markup_table_panel.load(node_id)
        self.node_markup_panel.setVisible(True)
        self.scenario_panel.setVisible(False)
        self.markup_table_panel.setVisible(True)
        self.node_markup_panel.set_bottom_toggle_checked(False)
        self._h_splitter.setSizes([260, 600, 62, 220, 0])
        self._v_splitter.setSizes([0, 200, 0])
        self._outer_splitter.setSizes([560, 200])
        self.pid_panel.enter_markup_edit(node_id)
        self._markup_undo_stack.clear()
        self._undo_shortcut.setEnabled(True)

    def _on_toggle_bottom_panel(self, checked):
        """New switch next to node_markup_panel (2026-08-17) — flips the
        bottom strip between Nodmarkeringar (default while editing) and
        HAZOP scenario, without leaving markup-edit mode."""
        self.markup_table_panel.setVisible(not checked)
        self.scenario_panel.setVisible(checked)
        self._v_splitter.setSizes([220, 0, 0] if checked else [0, 200, 0])

    def _on_close_node_markup(self):
        """Ribbon close button clicked — leave markup edit mode."""
        self.pid_panel.exit_markup_mode()
        self.pid_panel.reload_overlays()
        self.node_markup_panel.setVisible(False)
        self.scenario_panel.setVisible(True)
        self.markup_table_panel.setVisible(False)
        self._h_splitter.setSizes([260, 650, 370, 0, 0])
        self._v_splitter.setSizes([220, 0, 0])
        self._outer_splitter.setSizes([640, 220])
        self._markup_undo_stack.clear()
        self._undo_shortcut.setEnabled(False)

    def _on_place_symbol_requested(self):
        """NodeMarkupPanel's "Lägg ut P&ID-symbol" button (2026-08-17, see
        NOTES.md "Red markup konsolideras") — the two edit modes stay
        technically separate under the hood (lower regression risk in the
        heavily-tested P&ID drawing code than merging their state
        machines), but the user experience is a single, continuous flow:
        briefly switch into red-markup mode to place the symbol, then
        _on_close_red_markup returns to node markup editing automatically."""
        node_id = self.node_markup_panel.node_id
        if node_id is None:
            return
        self._return_to_node_markup_node_id = node_id
        self._on_edit_red_markup(node_id)
        self.red_markup_panel.open_symbol_picker()

    def _on_edit_red_markup(self, node_id):
        """Enter red-markup edit mode — 2026-08-17: only reachable via
        _on_place_symbol_requested now (the tree's own "Editera redmarkup"
        context-menu entry was removed, see NOTES.md "Red markup
        konsolideras"). tree_panel/props_ribbon stay visible, matching
        node markup's own docking fix — this mode is always a brief detour
        FROM node markup editing now, so hiding and reshowing them across
        the transition would just be visual noise."""
        self._switch_view(1)
        self.red_markup_panel.load(node_id)
        self.red_markup_table_panel.load(node_id)
        self.node_markup_panel.setVisible(False)
        self.red_markup_panel.setVisible(True)
        self.markup_table_panel.setVisible(False)
        self.scenario_panel.setVisible(False)
        self.red_markup_table_panel.setVisible(True)
        self._h_splitter.setSizes([260, 600, 62, 0, 220])
        self._v_splitter.setSizes([0, 0, 200])
        self._outer_splitter.setSizes([560, 200])
        self.pid_panel.enter_red_markup_edit(node_id)

    def _on_close_red_markup(self):
        """Red markup ribbon close button clicked — leave red markup edit
        mode. 2026-08-17: always returns to node markup editing for the
        same node now (see _on_place_symbol_requested) rather than closing
        everything, since the user only ever asked to place a symbol, not
        to leave node markup editing."""
        self.pid_panel.exit_red_markup_mode()
        self.pid_panel.reload_overlays()
        self.red_markup_panel.setVisible(False)
        self.red_markup_table_panel.setVisible(False)
        return_node_id = self._return_to_node_markup_node_id
        self._return_to_node_markup_node_id = None
        if return_node_id is not None:
            self._on_edit_node_markup(return_node_id)
            return
        self.tree_panel.setVisible(True)
        self.props_ribbon.setVisible(True)
        self.scenario_panel.setVisible(True)
        self._h_splitter.setSizes([260, 650, 370, 0, 0])
        self._v_splitter.setSizes([220, 0, 0])
        self._outer_splitter.setSizes([640, 220])

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

    def _on_matrix_changed(self):
        self.tree_panel.refresh()
        if self._cur_type == CAUSE_T and self._cur_id:
            self.scenario_panel.load_cause(self._cur_id)

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
        self._switch_view(1)
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
            # Deltagarmatris (2026-08-11) — replaces the old
            # 'project_participants' app_config field, see NOTES.md.
            'participants', 'analysis_sessions', 'participant_attendance',
            'participant_columns', 'participant_column_values',
            # Manuell sidrotation (2026-08-12), see NOTES.md.
            'pid_page_rotation',
            # Projekt-flikens revisionstabell + fria fält (2026-08-17), se NOTES.md.
            'project_revisions', 'project_custom_fields',
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
                    # 'project_hazop_leader' kept above for legacy DBs created
                    # before the field was removed (2026-08-17) in favor of a
                    # free Deltagare column, see NOTES.md.
                    'project_date_start', 'project_date_end', 'project_number',
                    'project_client',
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
        for panel in [self.tree_panel,
                      self.scenario_panel, self.equipment_panel,
                      self.admin_panel, self.settings_panel, self.hazop_prep_panel,
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
            self.equipment_panel.set_db(db)
        except Exception:
            pass

        # Also update db on settings sub-panels (they have their own db reference)
        sp = self.settings_panel
        for attr in ('_std_objects_panel', '_tag_memory_panel', '_tag_db_panel',
                     'analysis_panel'):
            try:
                sub = getattr(sp, attr, None)
                if sub is not None:
                    sub.db = db
            except Exception:
                pass

        # Same nested-sub-panel-with-its-own-db-reference pattern, for the
        # sub-panels that moved from SettingsPanel to HAZOPPreparationPanel
        # (2026-08-17, see NOTES.md) — same fix, new host object.
        hp = self.hazop_prep_panel
        for attr in ('_std_causes_panel', '_participant_matrix_panel'):
            try:
                sub = getattr(hp, attr, None)
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

        # Reload P&ID
        if pdf_path:
            self.pid_panel.try_reload_pdf(pdf_path)
        else:
            self.pid_panel.try_reload_pdf()


if __name__ == '__main__':
    import logging

    _configure_utf8_console_output()

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
    _palette.setColor(QPalette.ColorRole.Highlight, QColor('#2F5FD0'))
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
