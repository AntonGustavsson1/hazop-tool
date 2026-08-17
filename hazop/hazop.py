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
)
from tree_panel import (
    TreePanel, StandardCausesPickerPopup, CauseObjectPopup, CauseTagPopup,
    RRFPopup, FrequencyPickerPopup, DeviationPickerPopup,
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


def parse_chain_from_json(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


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
                # Qt auto-assigns one pushbutton in a QDialog as the
                # "default"/initially-focused button (normally the first
                # one created) — the app's global stylesheet then paints
                # THAT button with an extra blue focus/default outline
                # (QPushButton:focus/:default rules) on top of whichever
                # cell already has the real black is_current border below,
                # so two cells looked marked regardless of hover
                # (2026-08-14 follow-up to the hover fix below — a
                # second, independent cause of the same symptom).
                btn.setAutoDefault(False)
                btn.setDefault(False)
                btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                # Hover style is intentionally NOT a solid black border —
                # that looked identical to the is_current marker above,
                # so hovering a different cell than the actual current one
                # looked like "two cells are marked" (reported feedback).
                # Dashed blue is unmistakably "just hovering", never "this
                # is the current value".
                btn.setStyleSheet(
                    f"QPushButton{{background:{color}; color:{fg};"
                    f"font-size:8px; font-weight:bold;"
                    f"border:{border}; border-radius:0px; margin:0px;}}"
                    f"QPushButton:hover{{border:2px dashed #2f6fed;}}")
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
        "  border:1px solid #2F5FD0; }"
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
            pin_btn = QPushButton()
            pin_btn.setIcon(_icon('pin', 14, '#dc2626'))
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
        add_more_btn = QPushButton("Lägg till ytterligare objekt")
        add_more_btn.setIcon(_icon('pin', 16, '#ffffff'))
        add_more_btn.setToolTip(
            "Spara denna kedja och återgå till P&ID-läge\n"
            "för att omedelbart markera ytterligare ett objekt.")
        add_more_btn.setStyleSheet(
            "background:#2F5FD0; color:white; border:none;"
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
                     "background:#2F5FD0; border-radius:3px; padding:3px;")
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

        wrap_cols = {panel._C_ORS, panel._C_KON, panel._C_REK}
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
            w -= _KON_CAT_W
            w = max(40, w)
            rect = fm.boundingRect(0, 0, w, 10000, Qt.TextFlag.TextWordWrap, text)
            return QSize(option.rect.width(), max(one_line_h, rect.height() + 4))
        elif col == panel._C_SG:
            w -= _RRF_W
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
_PLUS_BADGE_SIZE = 16      # pixel size of the in-cell "+" quick-add badge (bottom-right corner)

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
            clean = _PID_ICON_RE.sub('', str(raw))
            # A brand-new/cleared KON or SG cell displays a plain "—"
            # placeholder (2026-08-12, see NOTES.md) rather than literal
            # "Ny konsekvens"/"Ny safeguard" text — QTableWidgetItem has
            # no real Display-vs-EditRole divergence (setting one
            # overwrites what the other reads back), so the dash reaches
            # here too; start the editor blank instead of on top of it.
            if clean == '—':
                clean = ''
            editor.setText(clean)
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
            offset = _KON_CAT_W
            editor.setGeometry(QRect(r.left() + offset, r.top(),
                                     max(10, r.width() - offset), r.height()))
            return
        elif col == self._panel._C_SG:
            # 2026-08-10 fix: this used to span the full remaining width,
            # visually covering the RRF badge (_RRF_W) while editing.
            editor.setGeometry(QRect(r.left(), r.top(),
                                     max(10, r.width() - _RRF_W),
                                     r.height()))
            return
        editor.setGeometry(r)

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

                body_top = r.top()
                body_h   = r.height()

                # Layout: [description ...][RRF badge 54px]
                desc_w    = r.width() - _RRF_W
                desc_rect = QRect(r.left(), body_top, desc_w, body_h)
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
                badge_bg = QColor('#2F5FD0') if sel else QColor('#F5F5F3')
                painter.fillRect(rrf_rect, badge_bg)
                badge_tc = QColor('#ffffff') if sel else QColor('#17191C')
                painter.setPen(badge_tc)
                badge_font = QFont(option.font)
                badge_font.setBold(True)
                painter.setFont(badge_font)
                # Just the number — "RRF" now lives in the column header
                # instead, so this single-line badge doesn't force the
                # row taller than the ORS/description content needs
                # (2026-08-14, see NOTES.md).
                painter.drawText(rrf_rect.adjusted(2, 1, -2, -1),
                                 Qt.AlignmentFlag.AlignCenter,
                                 f"{rrf}")

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

                self._draw_plus_badge(painter, r, row, col)
                painter.restore()
                return

        # ── Cause cells: top strip [tag|freq|dots] + description below ────────
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

                # ── Tag + frequency geometry (shared with the click hit-test
                # in eventFilter() via _ors_tag_zone_geometry — see its
                # docstring for why: 2026-08-11, "tag numret klipps av ...
                # högerställ frekvens". The frequency zone is anchored to
                # the dots margin at the strip's right edge FIRST; the tag
                # zone then gets whatever room that leaves, instead of
                # being capped at the fixed _cause_obj_w divider width
                # regardless of free space. ─────────────────────────────
                tag_x = r.left()
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

                self._draw_plus_badge(painter, r, row, col)
                painter.restore()
                return

        # ── Consequence cells: [cat-badge][description] ────────────────────────
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

                body_top = r.top()
                body_h   = r.height()

                cat_rect   = QRect(r.left(), body_top, _KON_CAT_W, body_h)
                txt_rect   = QRect(r.left() + _KON_CAT_W, body_top,
                                   r.width() - _KON_CAT_W, body_h)

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

                painter.setPen(QPen(QColor('#E2E3E1'), 1))
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

                self._draw_plus_badge(painter, r, row, col)
                painter.restore()
                return

        # ── Default: delegate straight to the base description painting ────────
        super().paint(painter, option, index)

    def _draw_plus_badge(self, painter, rect, row, col):
        """Small "+" in a cell's bottom-right corner, offering to add
        another cause/consequence/safeguard right where you're already
        looking — reported feedback (2026-08-12, see NOTES.md): a
        dedicated "+" ROW "tar upp alldeles för mycket plats då de tar
        hela rader med blankt" (takes up way too much space with whole
        blank rows); this only ever marks the LAST real row of a group,
        drawn on top of that row's own real content instead of a
        separate row. Hit-tested by eventFilter()'s _PLUS_BADGE_SIZE
        zone, not by this delegate — painting and hit-testing share
        nothing but the corner geometry, matching every other in-cell
        zone in this class (tag zone, category badge, RRF badge, …)."""
        if self._panel._row_plus_cols.get(row, {}).get(col) is None:
            return
        sz = _PLUS_BADGE_SIZE
        badge = QRect(rect.right() - sz - 2, rect.bottom() - sz - 2, sz, sz)
        painter.setPen(QColor('#8D9299'))
        f = QFont(painter.font())
        f.setBold(True)
        f.setPointSize(max(7, painter.font().pointSize()))
        painter.setFont(f)
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, '+')


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
                "QPushButton{background:#2F5FD0;color:white;border:none;"
                "border-radius:3px;padding:3px;font-weight:bold;font-size:9px;}"
                "QPushButton:hover{background:#3D6BD8;}")
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
            sep.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep)
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
            sep3.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep3)
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
        sep2.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep2)

        btn_row = QHBoxLayout()
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.setStyleSheet(
            "QPushButton{font-size:10px;padding:2px 12px;"
            "background:#2F5FD0;color:white;border-radius:3px;}"
            "QPushButton:hover{background:#3D6BD8;}")
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
        sep.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep)

        btn_row = QHBoxLayout()
        ok = QPushButton("OK")
        ok.setDefault(True)
        ok.setStyleSheet(
            "QPushButton{font-size:10px;padding:2px 12px;"
            "background:#2F5FD0;color:white;border-radius:3px;}"
            "QPushButton:hover{background:#3D6BD8;}")
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
        sep.setStyleSheet("color:#E2E3E1;"); outer.addWidget(sep)

        btn_row = QHBoxLayout()
        clr = QPushButton("Rensa alla")
        clr.setStyleSheet("font-size:10px; padding:2px 8px;")
        clr.clicked.connect(self._clear)
        ok  = QPushButton("OK")
        ok.setDefault(True)
        ok.setStyleSheet(
            "QPushButton{font-size:10px;padding:2px 12px;"
            "background:#2F5FD0;color:white;border-radius:3px;}"
            "QPushButton:hover{background:#3D6BD8;}")
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
            return ("QPushButton{background:#2F5FD0;color:white;"
                    "border:2px solid #2F5FD0;border-radius:3px;"
                    "font-size:9px;font-weight:bold;}"
                    "QPushButton:hover{background:#3D6BD8;}")
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
    structure_changed          = pyqtSignal()           # item moved/deleted/duplicated → refresh tree
    equipment_renamed          = pyqtSignal()           # an ORS tag edit renamed the linked equipment_catalog row

    # Column indices
    _C_NOD, _C_UTR, _C_DEV, _C_ORS, _C_KON, _C_RFORE = 0, 1, 2, 3, 4, 5
    _C_SG, _C_LOPA, _C_SLUT, _C_REK                   = 6, 7, 8, 9

    _COLS = [
        'Nod',
        'Utrustning',
        'Avvikelse',
        'Orsak',
        'Konsekvens',
        'Risk före barriär',
        'Barriärer (RRF)',
        'Enablers',
        'Slutkonsekvens',
        'Rekommendation',
    ]

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.cause_id = None
        self._node_id = None
        self._deviation_id = None
        self._all_nodes = False  # if True, show every node's full hierarchy (set by load_all)
        self._equipment_filter_id = None  # if set, show only causes mentioning this equipment_catalog id (set by load_equipment)
        self._show_empty_deviations = False  # if True, deviations with zero causes get a placeholder row instead of being omitted
        self._force_dev_column_visible = False  # if True, Avvikelse column stays visible regardless of _all_nodes (set by always_show_deviation_column)
        self._force_utr_column_hidden = False  # if True, Utrustning column stays hidden regardless of _all_nodes (set by hide_equipment_column)
        self._row_meta = []   # list of (dev_id, cause_id, cons_id, sg_id) per visible row
        # row index -> {col: ('cause', dev_id) | ('consequence', cause_id) |
        # ('safeguard', cons_id)} — marks the LAST real row of a group that
        # already has content with a small in-cell "+" quick-add badge in
        # the given column's bottom-right corner (2026-08-12, see NOTES.md
        # — a dedicated "+" ROW took up too much space with whole blank
        # rows). A group with zero content still invites Enter-to-add on
        # its existing placeholder row instead (unchanged) — no badge
        # needed there since that row's own cell is already the target.
        self._row_plus_cols = {}
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
        self._fill_btn = QPushButton("Fyll bredd")
        self._fill_btn.setIcon(_icon('resize-horizontal'))
        self._fill_btn.setToolTip(
            "Fördela om Orsak/Konsekvens/Barriärer-kolumnerna så de fyller "
            "hela bredden just nu — kolumnerna går alltid att dra i")
        self._fill_btn.clicked.connect(self._fill_width_once)
        hdr_row.addWidget(self._fill_btn)
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
            self._C_REK:   (QHeaderView.ResizeMode.Interactive, 140),
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

        # ── Persist manually-resized column widths (2026-08-10, see
        # NOTES.md) — previously reset to the hardcoded defaults every
        # time the app restarted. Columns are always Interactive (see
        # resize_modes above) so dragging works regardless of whether
        # "↔ Fyll bredd" has ever been clicked.
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
        self._equipment_filter_id = None
        self._set_all_nodes_columns_visible(False)
        self._rebuild()

    def load_deviation(self, deviation_id):
        dev = self.db.get_deviation(deviation_id)
        self._node_id = dev['node_id'] if dev else None
        self._deviation_id = deviation_id
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = False
        self._equipment_filter_id = None
        self._set_all_nodes_columns_visible(False)
        self._rebuild()

    def load_cause(self, cause_id):
        self._node_id = None
        self._deviation_id = None
        self.cause_id = cause_id
        self._cons_id = None
        self._all_nodes = False
        self._equipment_filter_id = None
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
            self._equipment_filter_id = None
            self._set_all_nodes_columns_visible(False)
            self._rebuild()

    def load_all(self):
        """Show the entire study: every node's full deviation/cause/consequence/safeguard hierarchy."""
        self._node_id = None
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = True
        self._equipment_filter_id = None
        self._set_all_nodes_columns_visible(True)
        self._rebuild()

    def load_equipment(self, equipment_id):
        """Filter the scenario table to only the causes that mention this
        specific P&ID equipment object anywhere in their chain (deviation,
        cause, consequence or safeguard) — used when the user clicks a
        defined (red/green) equipment marker on the P&ID (2026-08-12, see
        NOTES.md: 'de orsaker som visas i hazop scenario är de där
        objektet finns med'). NOD/DEV/UTR columns are shown (same as "all
        nodes" mode) since the matching rows can span several deviations
        or even nodes."""
        self._node_id = None
        self._deviation_id = None
        self.cause_id = None
        self._cons_id = None
        self._all_nodes = False
        self._equipment_filter_id = equipment_id
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
        self._equipment_filter_id = None
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
        keeps only the Avvikelse column visible regardless of `visible` —
        for hosts like HAZOPWorksheet/the embedded P&ID scenario panel
        where deviation context should always be in the grid, not just in
        the sticky header bar. Utrustning does NOT follow that same force
        (reported feedback: it duplicated the tag already shown at the top
        of each Orsak cell) — it only appears in genuine "all nodes" mode,
        where it still earns its keep by disambiguating the several
        equipment groups a single interleaved view can span. `self.
        _force_utr_column_hidden` (set via hide_equipment_column()) is the
        opposite override — for a host that doesn't want Utrustning even
        in "all nodes" mode (2026-08-13, see NOTES.md: "i worksheet
        behöver inte objekt kolumnen synas")."""
        self._table.setColumnHidden(self._C_NOD, not visible)
        self._table.setColumnHidden(
            self._C_DEV, not (visible or self._force_dev_column_visible))
        self._table.setColumnHidden(
            self._C_UTR, self._force_utr_column_hidden or not visible)

    def always_show_deviation_column(self):
        """Keep the Avvikelse column visible at all times, regardless of
        "Visa samtliga noder" / "Visa avvikelser utan orsaker" state —
        opt-in for hosts (e.g. HAZOPWorksheet, the embedded P&ID scenario
        panel) that want deviation context always shown in the grid
        itself. Utrustning is unaffected — see _set_all_nodes_columns_visible."""
        self._force_dev_column_visible = True
        self._set_all_nodes_columns_visible(self._all_nodes)

    def hide_equipment_column(self):
        """Keep the Utrustning column hidden even in "all nodes" mode
        (2026-08-13, see NOTES.md) — opt-in for hosts (HAZOPWorksheet)
        that don't need it disambiguating equipment groups in the
        interleaved view; the tag is already shown at the top of each
        Orsak cell regardless."""
        self._force_utr_column_hidden = True
        self._set_all_nodes_columns_visible(self._all_nodes)

    # Columns that stretch to fill remaining space in fill mode
    _STRETCH_COLS = None  # set after class constants are known

    def _fill_width_once(self):
        """Redistribute ORS/KON/SG to fill the table's current width right
        now. Previously "Fyll skärm" was a persistent checkbox that locked
        those columns into Stretch mode (blocking manual dragging
        entirely) and locked RFORE/LOPA/SLUT into Fixed — unchecking it
        only changed the resize MODE, not any pixel width, so it looked
        like it "had no effect" (reported feedback). Columns are now
        always Interactive (see resize_modes in __init__), so dragging
        works regardless of whether this button has ever been clicked;
        this just gives it one immediate, visible effect instead of a
        silent mode flip."""
        stretch_cols = [self._C_ORS, self._C_KON, self._C_SG]
        other_cols = [c for c in range(self._table.columnCount())
                      if c not in stretch_cols and not self._table.isColumnHidden(c)]
        used = sum(self._table.columnWidth(c) for c in other_cols)
        available = max(0, self._table.viewport().width() - used)
        per_col = max(60, available // len(stretch_cols))
        for col in stretch_cols:
            self._table.setColumnWidth(col, per_col)

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

    def rebuild(self):
        """Public entry point for a full, immediate table rebuild."""
        self._rebuild()

    def schedule_rebuild(self):
        """Public entry point for a deferred rebuild — see _schedule_rebuild."""
        self._schedule_rebuild()

    def get_equipment_filter(self):
        """The equipment_catalog id the table is currently filtered to, or None."""
        return self._equipment_filter_id

    def position_near_row(self, cons_id: int, popup_size):
        """Public wrapper — see _pos_near_cons_row."""
        return self._pos_near_cons_row(cons_id, popup_size)

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
                self._row_plus_cols = {}

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

    def _cause_tag_display(self, cause_d):
        """(comp_type, comp_tag) for the ORS tag strip — resolved LIVE
        from equipment_catalog via causes.equipment_id when the cause is
        linked to a real object (2026-08-13, see NOTES.md: "taggen är
        kopplad till objekten ... ändrar jag ... p&id" — renaming the
        object must show up here on the very next redraw, same live-FK
        pattern _equipment_for_dev above already uses for the Utrustning
        column), falling back to the frozen comp_type/comp_tag strings
        for a custom/unmatched tag (equipment_id is None) or if the
        linked row was since deleted."""
        eq_id = cause_d.get('equipment_id')
        if eq_id:
            eq = self.db.get_equipment_by_id(eq_id)
            if eq:
                return eq.get('equipment_type') or '', eq.get('tag') or ''
        return cause_d.get('comp_type') or '', cause_d.get('comp_tag') or ''

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

    def _causes_for_equipment(self, equipment_id):
        """Return [(cause_dict, deviation_dict), ...] for every cause that
        mentions this equipment anywhere in its chain — see
        Database.causes_for_equipment(). Used by the "click an equipment
        marker on P&ID" filter (load_equipment)."""
        result = []
        for c in self.db.causes_for_equipment(equipment_id):
            c_d = dict(c)
            dev = self.db.get_deviation(c_d.get('deviation_id'))
            dev_d = dict(dev) if dev else {'id': None, 'node_id': c_d.get('node_id'), 'description': '—'}
            result.append((c_d, dev_d))
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
        elif self._equipment_filter_id is not None:
            causes_to_show.extend(self._causes_for_equipment(self._equipment_filter_id))
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
            elif self._equipment_filter_id is not None:
                # No causes mention this equipment yet — nothing sensible to
                # show as a placeholder (no single deviation to attach it to).
                equip = self.db.get_equipment_by_id(self._equipment_filter_id)
                tag = equip.get('tag', '?') if equip else '?'
                self._hdr_lbl.setText(f"HAZOP Scenario — Objekt: {tag} (inga orsaker än)")
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
        elif self._equipment_filter_id is not None:
            equip = self.db.get_equipment_by_id(self._equipment_filter_id)
            tag = equip.get('tag', '?') if equip else '?'
            self._hdr_lbl.setText(f"HAZOP Scenario — Objekt: {tag}")
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
                first_row_for_cons = self._table.rowCount()
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
                # "+" badge on the SG cell — only when this consequence
                # already has at least one real safeguard; an empty one
                # already invites Enter-to-add on its own placeholder row.
                if n_sgs > 0:
                    self._mark_plus_target(self._table.rowCount() - 1, self._C_SG,
                                            'safeguard', cons_d['id'])
            if self._table.rowCount() == first_row_for_cause:
                logging.info('_build_rows: G3 — cause %s had no rows, adding empty row',
                             cause_d.get('id'))
                self._add_empty_row(node_name, dev_d, cause_d, freq, freq_lbl)
            elif all_cons:
                # "+" badge on the KON cell — only when this cause already
                # has at least one real consequence (mirrors the safeguard
                # rule above; the empty case is _add_empty_row, just above).
                # Anchored at first_row_for_cons (the LAST consequence's
                # own first row), not the last physical row — KON spans by
                # cons_id (_apply_spans), and Qt's delegate paints a
                # spanned cell using its ANCHOR (first) row, not any later
                # row the span happens to cover; a badge keyed to the last
                # row would silently never be found by paint().
                self._mark_plus_target(first_row_for_cons, self._C_KON,
                                        'consequence', cause_d['id'])
            # "+" badge on the ORS cell — once per deviation, after its
            # LAST real cause (sentinel/empty-deviation entries never
            # reach this point, they `continue` above, so this only fires
            # for deviations that had at least one real cause). Anchored
            # at first_row_for_cause for the same span-anchor reason as
            # the KON badge above — ORS spans by cause_id.
            _next_dev_id = (causes_to_show[_cause_idx + 1][1].get('id')
                            if _cause_idx + 1 < len(causes_to_show) else None)
            if dev_d.get('id') != _next_dev_id:
                self._mark_plus_target(first_row_for_cause, self._C_ORS,
                                        'cause', dev_d.get('id'))
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

        # KON, LOPA and REK: span by cons_id (whole consequence merged) —
        # a recommendation belongs to the CONSEQUENCE, not to one of its
        # safeguard rows, same as KON/LOPA already group.
        for col in (self._C_KON, self._C_LOPA, self._C_REK):
            _span_col(col, lambda r: _meta(r, 2))
        logging.info('_apply_spans: J5 — KON/LOPA/REK columns spanned')

        # RFORE, SLUT: span by (cons_id, cat_id)
        # → non-category rows all merge; per-category rows each stay separate
        for col in (self._C_RFORE, self._C_SLUT):
            _span_col(col, _cat_key)
        logging.info('_apply_spans: J6 — RFORE/SLUT columns spanned, done')

    def _compute_row_height(self, row, fm=None):
        """The height `row` needs across EVERY column that can affect it —
        ORS/KON wrapped text, the fixed-height _LopaWidget in the FA/Ant.
        column, SG's one-line minimum, and the ORS readability floor —
        folded into a single per-row function so a caller updating just
        ONE column's text can never accidentally shrink the row below what
        its OTHER columns need.

        2026-08-11 ("skapat en konsekvens och sedan suddar ut allt krymper
        raden ... jag inte ser vad som står på orsak och FA/antändning ser
        konstigt ut"): before this, _update_row_text_only()'s fast path set
        a row's height to ONLY what the just-edited column (e.g. KON, once
        cleared back to empty text) needed, discarding whatever a long ORS
        cause description or the LOPA widget's own fixed height required —
        the row shrank to one line, clipping the cause text and squashing
        the FA/Ant. widget below its own setFixedHeight(). This function is
        now the single source of truth for "how tall must this row be",
        used by both the full _resize_rows_manual() rebuild pass AND
        _update_row_text_only()'s single-row fast path, so the two can
        never again disagree about one row's height. Also folds in the ORS
        minimum-readable-height floor _resize_rows() used to apply in a
        SEPARATE pass reachable only from a full rebuild — the fast path
        never went through that pass at all, so a short ORS text edited via
        the fast path could previously end up below the floor too.
        """
        table = self._table
        if fm is None:
            fm = QFontMetrics(table.font())
        one_line_h = fm.height() + 6
        wrap_cols = (self._C_ORS, self._C_KON, self._C_REK)
        max_h = one_line_h
        for col in range(table.columnCount()):
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
            elif col == self._C_KON:
                cell_w = max(40, w - _KON_CAT_W)
                rect = fm.boundingRect(0, 0, cell_w, 10000,
                                      Qt.TextFlag.TextWordWrap, text)
                h = max(one_line_h, rect.height() + 4)
            else:   # self._C_REK
                cell_w = max(40, w - 6)
                rect = fm.boundingRect(0, 0, cell_w, 10000,
                                      Qt.TextFlag.TextWordWrap, text)
                h = max(one_line_h, rect.height() + 4)
            if h > max_h:
                max_h = h

        ors_item = table.item(row, self._C_ORS)
        if ors_item and ors_item.text():
            min_ors = fm.height() * 2 + 20  # floor for ORS rows: ~2 lines + strip
            if max_h < min_ors:
                max_h = min_ors
        return max_h

    def _resize_rows_manual(self):
        """
        Compute and apply each row's height directly in Python instead of
        calling QTableWidget.resizeRowsToContents() (see _resize_rows()
        docstring for why). Delegates the actual per-row formula to
        _compute_row_height() (shared with _update_row_text_only()'s fast
        path — see that method's docstring) but keeps the QFontMetrics
        instance built ONCE up here rather than once per row: only the
        columns that actually need wrapping-height computation (_C_ORS,
        _C_KON — see _ScenarioDelegate._size_hint_impl) run the expensive
        QFontMetrics.boundingRect() path, but constructing QFontMetrics
        itself hundreds of times (once per row, in "all nodes" mode) is
        needless allocation churn worth avoiding when a single shared
        instance works just as well.
        """
        table = self._table
        row_count = table.rowCount()
        col_count = table.columnCount()
        fm = QFontMetrics(table.font())
        one_line_h = fm.height() + 6

        logging.info('_resize_rows_manual: L0 — entry (rows=%d, cols=%d)',
                     row_count, col_count)

        for row in range(row_count):
            try:
                max_h = self._compute_row_height(row, fm=fm)
            except Exception:
                # Defensive: this is user-facing rebuild code and a single
                # row's height computation should never take down the whole
                # rebuild. This can only catch genuine Python-level
                # exceptions (attribute errors, etc.) — it is not a safety
                # net for native crashes, since the whole point of this
                # method is to avoid the native resizeRowsToContents() path
                # that was pinpointed as the actual crash site.
                logging.exception('_resize_rows_manual: L1 — row %d height calc raised', row)
                max_h = one_line_h

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
        ors.setData(Qt.ItemDataRole.UserRole + 2, self._cause_tag_display(cause_d))
        ors.setData(Qt.ItemDataRole.UserRole + 3, freq)
        self._table.setItem(r, self._C_ORS, ors)

        kon = _ro()
        kon.setToolTip("Enter för att lägga till konsekvens")
        self._table.setItem(r, self._C_KON, kon)

        for col in (self._C_RFORE, self._C_SG,
                    self._C_LOPA, self._C_SLUT):
            self._table.setItem(r, col, _ro())

        pass  # row height set by resizeRowsToContents at end of _rebuild

    _PLUS_TIPS = {
        'cause':       "Klicka för att lägga till en ny orsak, valfritt kopplad till ett P&ID-objekt",
        'consequence': "Klicka för att lägga till en ny konsekvens, valfritt kopplad till ett P&ID-objekt",
        'safeguard':   "Klicka för att lägga till en ny barriär, valfritt kopplad till ett P&ID-objekt",
    }

    def _mark_plus_target(self, row, col, kind, group_id):
        """Flags an ALREADY-BUILT real row's cell to show a small in-cell
        "+" badge in its bottom-right corner (2026-08-12, see NOTES.md —
        reported feedback: a dedicated "+" ROW "tar upp alldeles för
        mycket plats då de tar hela rader med blankt", i.e. took up way
        too much space with whole blank rows; "lägg hellre ett plus om
        det redan finns en orsak/konsekvens/safeguard i den rutan"). Only
        ever called on the LAST real row of a non-empty group — a group
        with zero content still invites Enter-to-add on its existing
        placeholder row instead (_add_placeholder_row/_add_empty_row,
        unchanged). Painted by _PidDelegate._draw_plus_badge(), hit-
        tested by eventFilter()."""
        self._row_plus_cols.setdefault(row, {})[col] = (kind, group_id)
        item = self._table.item(row, col)
        if item is not None:
            item.setToolTip((item.toolTip() + '\n' if item.toolTip() else '')
                             + self._PLUS_TIPS[kind])

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
        ors.setData(Qt.ItemDataRole.UserRole + 2, self._cause_tag_display(cause_d))
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

        # "—" placeholder when empty, not literal "Ny konsekvens" text
        # (2026-08-12, see NOTES.md). No separate EditRole is set here —
        # QTableWidgetItem aliases Display/EditRole to the same storage
        # (verified: setData() on one overwrites what the other reads
        # back), so _PidDelegate.createEditor() strips the "—" sentinel
        # itself when opening the editor instead.
        kon_item = QTableWidgetItem(cons_d['description'] or '—')
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
        # One point smaller than the general cell font (reported: this
        # cell's text got cut off at the default 85px column width) and
        # scales with "Textstorlek" instead of being hardcoded at 9pt.
        rb.setFont(QFont("Consolas", max(6, self._cell_font_size - 1)))
        self._table.setItem(r, self._C_RFORE, rb)

        # ── Col 5: Barriär ───────────────────────────────────────────────────
        if sg is None:
            sg_item = QTableWidgetItem('—')
            sg_item.setFlags(sg_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            sg_item.setToolTip("Enter för att lägga till barriär")
        else:
            rrf = sg.get('rrf', 1) or 1
            # "—" placeholder when empty (2026-08-12, see NOTES.md) — no
            # separate EditRole set here, see the KON cell's comment above
            # on why that would silently overwrite this back to empty.
            sg_item = QTableWidgetItem(sg['description'] or '—')
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

        # ── Col REK: Rekommendation (2026-08-13, see NOTES.md) ───────────────
        # Backed by the pre-existing actions table/ActionEditor (previously
        # unreachable in the UI) rather than a new free-text field — a
        # scenario can have several recommendations (responsible/due date/
        # status each), not just one line of text.
        acts = self.db.actions(cid)
        rek_item = QTableWidgetItem(self._recommendation_summary(acts))
        rek_item.setFlags(rek_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        rek_item.setData(Qt.ItemDataRole.UserRole, ('recommendation', cid))
        rek_item.setToolTip("Klicka för att lägga till/redigera rekommendationer")
        if not acts:
            rek_item.setForeground(QBrush(QColor('#8D9299')))
        self._table.setItem(r, self._C_REK, rek_item)

        pass  # row height set by resizeRowsToContents at end of _rebuild

    def _recommendation_summary(self, acts):
        """REK-cell text for a consequence's action/recommendation list
        (2026-08-13, see NOTES.md: "samtliga tillagda rekomendationer
        ... nummereras efter tilläggsordning") — "—" placeholder when
        empty (same convention as KON/SG), otherwise EVERY recommendation
        listed on its own line, numbered 1.. in the order they were
        added (db.actions() already returns them ORDER BY id). The
        column joins wrap_cols so multi-line content gets the row
        height it needs, same as ORS/KON."""
        if not acts:
            return '—'
        return '\n'.join(f"{i}. {a['description'] or 'Ny åtgärd'}"
                          for i, a in enumerate(acts, 1))

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

    def _open_recommendation_editor(self, cons_id):
        """Open the Rekommendation-column popup (2026-08-13, see
        NOTES.md) — ActionEditor already persists every change itself,
        so no Accepted/Rejected distinction is needed; just refresh the
        cell's summary text once the dialog closes either way."""
        dlg = RecommendationEditorDialog(self.db, cons_id, self)
        dlg.move(self._pos_near_cons_row(cons_id, dlg.sizeHint()))
        dlg.exec()
        self._refresh_recommendation_cell(cons_id)

    def _refresh_recommendation_cell(self, cons_id):
        """Fast in-place patch of every row's REK cell for cons_id,
        mirroring _update_row_text_only()'s pattern (same re-entrancy
        guard, same table.item()-is-None check to skip span-covered
        rows that have no real item of their own)."""
        if getattr(self, '_rebuilding', False):
            return
        acts = self.db.actions(cons_id)
        summary = self._recommendation_summary(acts)
        self._table.blockSignals(True)
        try:
            for row, meta in enumerate(self._row_meta):
                if meta[2] != cons_id:
                    continue
                item = self._table.item(row, self._C_REK)
                if item is not None:
                    item.setText(summary)
                    item.setForeground(QBrush(QColor('#8D9299' if not acts else '#000000')))
        finally:
            self._table.blockSignals(False)

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
        if self._all_nodes or self._force_dev_column_visible or self._equipment_filter_id is not None:
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
                    # Recompute the WHOLE row's height (_compute_row_height),
                    # not just what this one column now needs — otherwise
                    # clearing e.g. a consequence's text back to empty would
                    # shrink the row below what a long cause description (or
                    # the FA/Ant. column's fixed-height widget) in the SAME
                    # row still requires (2026-08-11, see NOTES.md).
                    self._table.setRowHeight(row, self._compute_row_height(row))
        finally:
            self._table.blockSignals(False)

    def _wrap_col_row_height(self, row, col):
        """Height a single ORS/KON cell alone needs for its current text —
        no longer used by _update_row_text_only() (see _compute_row_height,
        which considers the whole row), kept as a small standalone helper
        purely because TextOnlyEditFastPathTests asserts it still agrees
        with a real _resize_rows_manual() pass for a single column, so the
        two formulas can never silently drift apart."""
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
            cell_w = max(40, w - _KON_CAT_W)
            rect = fm.boundingRect(0, 0, cell_w, 10000, Qt.TextFlag.TextWordWrap, text)
            return max(one_line_h, rect.height() + 4)

    def refresh_placed(self):
        """Repaint the table — kept as a thin call so its many existing
        call sites (after any data change that might affect what's shown)
        keep working unchanged; it no longer tracks P&ID placement state
        (2026-08-13, see NOTES.md: the P&ID canvas is now
        object-placement-only, so cause/consequence/safeguard rows have no
        "placed on P&ID" concept anymore)."""
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

    def active_edit_target(self):
        """Returns (editor, kind, id_) for the ORS/KON/SG cell currently
        being edited, or None — kind is 'cause'/'consequence'/'safeguard',
        id_ the matching cause_id/cons_id/sg_id. Used by PIDPanel's
        Shift+click-on-marker tag-insert feature (2026-08-13, see
        NOTES.md: "jag kan fortsätta skriva efter objektet ... jag
        hoppar inte ut ur textediteringsvyn") so it can insert into an
        already-open editor instead of disturbing it — same EditingState
        guard select_cause() above already uses to avoid stealing focus.
        kind/id_ let the caller also sync tagged_refs so the eventual
        saved text gets the same bold-tag-highlight treatment the
        drag-and-drop path already gives KON/SG cells.

        Resolves row/col from the editor's OWN 'editing_row'/'editing_col'
        properties (set by _ScenarioDelegate.createEditor) rather than
        self._table.currentItem()/currentRow() — those aren't reliably
        in sync with which cell is actually mid-edit (e.g. right after
        editItem()), while the editor's own properties always are, same
        as eventFilter() already relies on elsewhere in this class."""
        if self._table.state() != QAbstractItemView.State.EditingState:
            return None
        editor = self._table.focusWidget()
        if not isinstance(editor, QLineEdit):
            return None
        row, col = editor.property('editing_row'), editor.property('editing_col')
        if row is None or col is None:
            return None
        item = self._table.item(row, col)
        if item is None:
            return None
        meta = item.data(Qt.ItemDataRole.UserRole)
        if not meta or meta[0] not in ('cause', 'consequence', 'safeguard'):
            return None
        return editor, meta[0], meta[1]

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

    def _on_table_context_menu(self, pos):
        col = self._table.columnAt(pos.x())
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= len(self._row_meta):
            return
        if col not in (self._C_ORS, self._C_KON, self._C_SG):
            return
        if not self._cell_has_item(row, col):
            return  # no item here at all — e.g. safeguard row with no safeguard yet
        menu = QMenu(self)
        if col == self._C_KON and row < len(self._row_meta):
            cons_id = self._row_meta[row][2]
            if cons_id is not None:
                menu.addSeparator()
                a_chain = menu.addAction(_icon('clipboard'), "Redigera konsekvenskedja (Del1–Del5)…")
                a_chain.triggered.connect(lambda: self._open_chain_editor(cons_id))
        if col == self._C_SG and row < len(self._row_meta):
            sg_id = self._row_meta[row][3]
            if sg_id is not None:
                menu.addSeparator()
                a_rrf = menu.addAction(_icon('settings'), "Ändra RRF...")
                a_rrf.triggered.connect(lambda: self._show_rrf_popup(row, sg_id))
        # Feature 4: clone scenario to another deviation
        if col == self._C_ORS and row < len(self._row_meta):
            cause_id = self._row_meta[row][1]
            if cause_id is not None:
                menu.addSeparator()
                a_clone = menu.addAction(_icon('clipboard'), "Duplicera scenario till annan avvikelse…")
                a_clone.triggered.connect(lambda: self._clone_scenario(cause_id))
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _on_cell_clicked(self, row, col):
        if col == self._C_ORS and row < len(self._row_meta):
            dev_id, cause_id = self._row_meta[row][0], self._row_meta[row][1]
            if cause_id is not None:
                self.item_selected.emit(CAUSE_T, cause_id)
            elif dev_id is not None:
                # Empty placeholder ORS cell (2026-08-12, see NOTES.md) —
                # open the same CauseObjectPopup as "+ Ny orsak" instead of
                # starting inline text edit, so creating a cause behaves
                # identically regardless of entry point.
                idx = self._table.model().index(row, col)
                gp = self._table.viewport().mapToGlobal(self._table.visualRect(idx).topLeft())
                self._add_cause_via_plus_row(dev_id, global_pos=gp)
                return
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
        if col == self._C_REK and row < len(self._row_meta):
            cons_id = self._row_meta[row][2]
            if cons_id is not None:
                self._open_recommendation_editor(cons_id)
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
        # Double-click starts inline edit — consistent across ORS/KON/SG
        # (reported feedback: KON used to open the "Konsekvenskedja" wizard
        # instead, which felt out of place and inconsistent with ORS/SG's
        # plain edit-in-place). The chain wizard remains reachable via the
        # right-click context menu (_open_chain_editor, unchanged there).
        if col in (self._C_ORS, self._C_KON, self._C_SG):
            if not bool(item.flags() & Qt.ItemFlag.ItemIsEditable):
                # A KON cell is always backed by a real (if blank)
                # consequence row — every cause gets one auto-created —
                # so it's never left non-editable. An SG cell IS, since
                # safeguards aren't auto-created (2026-08-17, see
                # NOTES.md "dubbelklicka på safeguards"): double-click on
                # an empty one used to just do nothing, unlike KON's
                # "double-click to add" feel. Quick-add one here instead,
                # same no-popup straight-to-inline-edit path Enter/the
                # "+" row already use.
                if col == self._C_SG:
                    cons_id = self._row_meta[row][2] if row < len(self._row_meta) else None
                    if cons_id is not None:
                        self._quick_add_safeguard(cons_id)
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
        """A plain click on the ORS tag zone opens just a tag+type
        popup (2026-08-14, see NOTES.md) — the full avvikelse-context +
        standard-cause CauseObjectPopup is still reachable, unchanged,
        from the detail panel (_edit_cause_obj) and quick-add
        (_quick_add_cause)."""
        item      = self._table.item(row, self._C_ORS)
        obj_data  = item.data(Qt.ItemDataRole.UserRole + 2) if item else None
        comp_type, comp_tag = obj_data if obj_data else ('', '')

        popup = CauseTagPopup(self.db, comp_type, comp_tag, parent=self)
        popup.committed.connect(
            lambda ct, tg, r=row, cid=cause_id:
                self._apply_cause_obj(r, cid, ct, tg, '', None))
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
        # Live tag link (2026-08-13, see NOTES.md: "taggen är kopplad
        # till objekten i orsaken ... ändrar jag i hazop scenario
        # ändras namnet på p&id och vice versa"). Two cases:
        # - Already linked to a real object and the typed tag text now
        #   differs from that object's own tag → RENAME the object
        #   itself (equipment_catalog), everywhere in the app, not just
        #   this cell's frozen comp_tag copy.
        # - Not linked yet, but the typed tag happens to match an
        #   existing object's tag exactly → just link to it (no rename,
        #   nothing to rename FROM).
        cause = self.db.get_cause(cause_id)
        old_equipment_id = cause.get('equipment_id') if cause else None
        new_tag = (comp_tag or '').strip()
        equipment_id = old_equipment_id
        renamed = False
        if old_equipment_id is not None:
            old_eq = self.db.get_equipment_by_id(old_equipment_id)
            if old_eq and new_tag and new_tag != (old_eq.get('tag') or ''):
                self.db.update_equipment_item(
                    old_equipment_id, new_tag, old_eq.get('prefix') or '',
                    old_eq.get('equipment_type') or comp_type, old_eq.get('description') or '')
                self.equipment_renamed.emit()
                renamed = True
        elif new_tag:
            match = self.db.get_equipment_by_tag(new_tag)
            equipment_id = match['id'] if match else None

        # Do all DB writes first — learning is handled inside update_cause
        self.db.update_cause(cause_id, comp_type=comp_type, comp_tag=comp_tag,
                              equipment_id=equipment_id)
        if description:
            kwargs = {'description': description}
            if frequency is not None:
                kwargs['base_frequency'] = frequency
            self.db.update_cause(cause_id, **kwargs)
        if description or renamed:
            # Description changed, or a rename may affect OTHER rows
            # sharing the same equipment_id too → full rebuild (item
            # refs are stale after rebuild anyway).
            self._schedule_rebuild()
        else:
            # Only this row's own tag/type changed → update item in-place
            # with signals blocked.
            self._table.blockSignals(True)
            item = self._table.item(row, self._C_ORS)
            if item:
                item.setData(Qt.ItemDataRole.UserRole + 2, (comp_type, comp_tag))
            self._table.blockSignals(False)
            self._table.viewport().update()

    def _update_sg_rrf(self, row, sg_id, rrf, sg_type=None):
        self.db.update_safeguard(sg_id, rrf=rrf, sg_type=sg_type)
        self._schedule_rebuild()

    def _on_ors_frequency_picked(self, cause_id, f_level, numeric):
        """FrequencyPickerPopup.frequency_selected handler for the ORS
        strip's frequency zone click (2026-08-14, see NOTES.md). Exactly
        one of f_level/numeric is non-None — a preset sets the matrix
        F-level and clears any manual numeric override; a custom value
        sets base_frequency directly (causes.likelihood is then derived
        from it, see _sync_f_levels_from_base_frequency)."""
        if f_level is not None:
            self.db.update_cause(cause_id, likelihood=f_level, base_frequency=None)
        else:
            self.db.update_cause(cause_id, base_frequency=numeric)
        self._schedule_rebuild()

    def _on_deviation_picked(self, cause_id, node_id, dev_id, new_desc):
        """DeviationPickerPopup.deviation_picked handler for the
        Avvikelse cell click (2026-08-14, see NOTES.md). Exactly one of
        dev_id/new_desc is non-None — a preset moves the cause directly;
        free text gets-or-creates that deviation first."""
        target_dev_id = dev_id if dev_id is not None else \
            self.db.get_or_create_deviation(node_id, new_desc)
        self.db.move_cause_to_deviation(cause_id, target_dev_id)
        self.structure_changed.emit()
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
            div_x = self._table.columnViewportPosition(self._C_ORS) + self._cause_obj_w
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

        # ── Drag: record press position for potential drag-start ─────────────────
        if (obj is self._table.viewport() and
                event.type() == QEvent.Type.MouseButtonPress and
                event.button() == Qt.MouseButton.LeftButton):
            pos = event.pos()
            row = self._table.rowAt(pos.y())
            col = self._table.columnAt(pos.x())
            if row >= 0 and col in (self._C_ORS, self._C_KON, self._C_SG):
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
            div_x = self._table.columnViewportPosition(self._C_ORS) + self._cause_obj_w
            if abs(pos.x() - div_x) <= 4:
                self._drag_obj_w_active = True
                self._drag_obj_w_start_x = pos.x()
                self._drag_obj_w_start_w = self._cause_obj_w
                self._table.viewport().setCursor(Qt.CursorShape.SizeHorCursor)
                return True
            col = self._table.columnAt(pos.x())
            row = self._table.rowAt(pos.y())

            # ➕ In-cell "+" quick-add badge — bottom-right corner of the last
            # row of a group (2026-08-12 redesign, see NOTES.md: replaces the
            # old separate blank "+" row, which "tar upp alldeles för mycket
            # plats"). Checked here, ahead of the column's other right-edge
            # zones (RRF badge, clone/comment icons, …), so a click landing
            # specifically inside the small badge box wins; anywhere else in
            # those wider zones falls through to them unaffected.
            if row >= 0 and col in (self._C_ORS, self._C_KON, self._C_SG):
                plus = self._row_plus_cols.get(row, {}).get(col)
                if plus is not None:
                    idx = self._table.model().index(row, col)
                    cr  = self._table.visualRect(idx)
                    sz  = _PLUS_BADGE_SIZE
                    badge = QRect(cr.right() - sz - 2, cr.bottom() - sz - 2, sz, sz)
                    if badge.contains(pos):
                        kind, group_id = plus
                        if group_id is not None:
                            if kind == 'cause':
                                gp = self._table.viewport().mapToGlobal(pos)
                                self._add_cause_via_plus_row(group_id, global_pos=gp)
                            elif kind == 'consequence':
                                self._add_consequence_via_plus_row(group_id)
                            elif kind == 'safeguard':
                                self._add_safeguard_via_plus_row(group_id)
                        return True

            # Avvikelse cell click — reassign which deviation the row's
            # cause belongs to (2026-08-14, see NOTES.md: "klockan man
            # på avvikelsen justerar man avvikelsen"). Only meaningful
            # once the row actually has a cause (placeholder "no causes
            # yet" rows have cause_id None and nothing to move).
            if row >= 0 and col == self._C_DEV and row < len(self._row_meta):
                dev_id, cause_id = self._row_meta[row][0], self._row_meta[row][1]
                if cause_id is not None and dev_id is not None:
                    node_id = self.db.get_deviation(dev_id)['node_id']
                    gp = self._table.viewport().mapToGlobal(pos)
                    popup = DeviationPickerPopup.create_positioned(
                        self.db, node_id, dev_id, gp, parent=self)
                    popup.deviation_picked.connect(
                        lambda picked_dev_id, new_desc, cid=cause_id, nid=node_id:
                            self._on_deviation_picked(cid, nid, picked_dev_id, new_desc))
                    popup.exec()
                    return True

            # Object-tag zone click — left (0 .. tag_zone_w) of cause cell.
            # tag_zone_w is computed the same way paint()
            # computes it (via _ors_tag_zone_geometry) rather than the raw
            # _cause_obj_w divider width — otherwise, once a long tag's
            # DRAWN width expands past the old fixed cap (2026-08-11 fix),
            # clicking on the now-visible-but-previously-uncounted part of
            # the tag would silently do nothing (stale hit-test rectangle).
            if row >= 0 and col == self._C_ORS and row < len(self._row_meta):
                col_x      = self._table.columnViewportPosition(col)
                obj_start  = col_x
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

            # Frequency zone click — between the tag zone and the status
            # dots, using the exact same freq_zone_x/freq_zone_w geometry
            # paint() draws the text with (2026-08-14, see NOTES.md:
            # "klickar man på frekvens skall man kunna justera frekvens").
            # Checked here, BEFORE the clone/comment zone below, so a
            # click anywhere on the actually-rendered frequency text
            # always wins — the clone zone's own (independently sized)
            # boundary can geometrically overlap the visible frequency
            # text when it's long, and this ordering is what stops that
            # overlap from silently triggering "clone" instead (2026-08-14
            # research finding). Restricted to the strip's own height so
            # it doesn't also swallow clicks on the description below.
            if (row >= 0 and col == self._C_ORS and row < len(self._row_meta) and
                    pos.y() - self._table.rowViewportPosition(row) < _ORS_STRIP_H):
                cause_id = self._row_meta[row][1]
                if cause_id is not None:
                    col_x      = self._table.columnViewportPosition(col)
                    cell_right = col_x + self._table.columnWidth(col) - 1
                    item       = self._table.item(row, col)
                    _tw, freq_zone_x, freq_zone_w, freq_str = \
                        self._ors_tag_zone_geometry(item, col_x, cell_right)
                    if freq_str and freq_zone_x <= pos.x() < freq_zone_x + freq_zone_w:
                        gp = self._table.viewport().mapToGlobal(pos)
                        cur_f_level = item.data(Qt.ItemDataRole.UserRole + 3) if item else None
                        cur_numeric = item.data(Qt.ItemDataRole.UserRole + 5) if item else None
                        popup = FrequencyPickerPopup.create_positioned(
                            gp, current_f_level=cur_f_level,
                            current_numeric_freq=cur_numeric, parent=self)
                        popup.frequency_selected.connect(
                            lambda f_level, numeric, cid=cause_id:
                                self._on_ors_frequency_picked(cid, f_level, numeric))
                        popup.exec()
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
                cat_start = col_x
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
        menu.addAction(_icon('settings'), f'Ny orsak under avvikelse  [{dev_name}]',
                       lambda: self._quick_add_cause(dev_id))
        menu.addAction(_icon('warning'), "Ny konsekvens på denna orsak",
                       lambda: self._quick_add_consequence(cause_id))
        sg_action = menu.addAction(_icon('shield'), "Ny safeguard på denna konsekvens",
                       lambda: self._quick_add_safeguard(cons_id))
        sg_action.setEnabled(cons_id is not None)

        idx   = self._table.model().index(row, self._C_ORS)
        rect  = self._table.visualRect(idx)
        pos   = self._table.viewport().mapToGlobal(rect.bottomLeft())
        menu.exec(pos)

    def _quick_add_cause(self, deviation_id, global_pos=None):
        """Reported feedback (2026-08-12, see NOTES.md): a new/empty cause
        in HAZOP scenario should open the same compact CauseObjectPopup
        ("Orsak på P&ID" — Tag + Typ + Standardorsaker) already used
        everywhere a cause's tag/type/description is edited, instead of
        the larger StandardCausesPickerPopup this used to open. Reused by
        both the "+ Ny orsak" affordance and clicking an empty ORS
        placeholder cell (_on_cell_clicked), so both entry points behave
        identically."""
        dev = self.db.get_deviation(deviation_id)
        dev_desc = dev['description'] if dev else ''

        popup = CauseObjectPopup(
            '', '', self.db, dev_description=dev_desc,
            current_description='', deviation_id=deviation_id, parent=self)

        def _on_committed(comp_type, comp_tag, description, frequency):
            new_id = self.db.add_cause(deviation_id)
            self.db.update_cause(new_id, comp_type=comp_type, comp_tag=comp_tag,
                                  description=description or '',
                                  base_frequency=frequency)
            cons_id = self.db.add_consequence(new_id)
            # Jump straight to the new consequence's KON cell (not the cause's
            # own ORS cell) — the cause's description was already chosen in
            # the popup above, so typing the consequence is the next natural
            # step ("så fort jag lagt till en orsak", see NOTES.md).
            self.new_item_created.emit(CONS_T, cons_id)

        popup.committed.connect(_on_committed)
        if global_pos is not None:
            popup.adjustSize()
            _scr   = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
            screen = _scr.availableGeometry()
            pw, ph = popup.sizeHint().width(), popup.sizeHint().height()
            x, y   = global_pos.x(), global_pos.y() + 6
            if y + ph > screen.bottom(): y = global_pos.y() - ph - 6
            if x + pw > screen.right():  x = screen.right() - pw - 4
            popup.move(max(screen.left() + 4, x), max(screen.top() + 4, y))
        popup.exec()

    def _quick_add_consequence(self, cause_id):
        """Reported feedback (2026-08-12): unlike a new cause, a new
        consequence should never show a popup — create it blank and jump
        straight to inline editing (unchanged from before the object-
        picker experiment). Tagging an object onto it is still done via
        drag-and-drop from the P&ID, same as editing an existing row."""
        new_id = self.db.add_consequence(cause_id)
        self.new_item_created.emit(CONS_T, new_id)

    def _quick_add_safeguard(self, cons_id):
        """Same as _quick_add_consequence — no popup, straight to inline edit."""
        new_id = self.db.add_safeguard(cons_id)
        self.new_item_created.emit(SG_T, new_id)

    def _add_cause_via_plus_row(self, deviation_id, global_pos=None):
        self._quick_add_cause(deviation_id, global_pos=global_pos)

    def _add_consequence_via_plus_row(self, cause_id):
        self._quick_add_consequence(cause_id)

    def _add_safeguard_via_plus_row(self, cons_id):
        self._quick_add_safeguard(cons_id)

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
            # No 'Ny safeguard' fallback (2026-08-12, see NOTES.md) —
            # clearing the text back to empty must actually save empty
            # (displayed as "—"), not silently resurrect placeholder text.
            edit_val = item.data(Qt.ItemDataRole.EditRole)
            desc = str(edit_val).strip() if edit_val is not None else text.split('\n')[0].strip()
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
        copy_row = menu.addAction(_icon('clipboard'), "Kopiera rad  (Ctrl+C)")
        copy_row.triggered.connect(lambda: self._copy_row_to_clipboard(row))
        menu.addSeparator()

        # ── Orsak-åtgärder ──────────────────────────────────────────────
        if col in (self._C_ORS, self._C_NOD, self._C_DEV) and cause_id:
            c = self.db.get_cause(cause_id)
            c_desc = dict(c).get('description', '?')[:40] if c else '?'
            menu.addSection(_icon('settings'), f"Orsak: {c_desc}")
            menu.addAction(_icon('edit'), "Redigera",
                lambda: self._try_start_edit(row, self._C_ORS))
            a_dup = menu.addAction(_icon('document'), "Duplicera orsak (med konsekvenser)")
            a_dup.triggered.connect(
                lambda: self._duplicate_cause(cause_id))
            a_move = menu.addAction("↕  Flytta till annan avvikelse…")
            a_move.triggered.connect(
                lambda: self._move_cause_dialog(cause_id))
            menu.addSeparator()
            a_del = menu.addAction(_icon('delete'), "Ta bort orsak")
            a_del.triggered.connect(lambda cid=cause_id: self._confirm_delete('cause', cid))

        # ── Konsekvens-åtgärder ─────────────────────────────────────────
        elif col in (self._C_KON, self._C_RFORE) and cons_id:
            k = self.db.get_consequence(cons_id)
            k_desc = dict(k).get('description', '?')[:40] if k else '?'
            menu.addSection(_icon('warning'), f"Konsekvens: {k_desc}")
            a_dup = menu.addAction(_icon('document'), "Duplicera konsekvens (med barriärer)")
            a_dup.triggered.connect(
                lambda: self._duplicate_consequence(cons_id, cause_id))
            a_move = menu.addAction("↕  Flytta till annan orsak…")
            a_move.triggered.connect(
                lambda: self._move_consequence_dialog(cons_id))
            if k and (dict(k).get('comp_tag') or dict(k).get('comp_type')):
                a_untag = menu.addAction(_icon('close'), "Ta bort tagg")
                a_untag.triggered.connect(lambda cid=cons_id: self._untag_consequence(cid))
            menu.addSeparator()
            a_del = menu.addAction(_icon('delete'), "Ta bort konsekvens")
            a_del.triggered.connect(lambda cid=cons_id: self._confirm_delete('cons', cid))

        # ── Barriär-åtgärder ────────────────────────────────────────────
        elif col in (self._C_SG, self._C_LOPA, self._C_SLUT) and sg_id:
            sg = self.db.get_safeguard(sg_id)
            sg_desc = dict(sg).get('description', '?')[:40] if sg else '?'
            menu.addSection(_icon('shield'), f"Barriär: {sg_desc}")
            menu.addAction(_icon('edit'), "Redigera",
                lambda: self._try_start_edit(row, self._C_SG))
            a_copy = menu.addAction(_icon('clipboard'), "Kopiera till annan konsekvens…")
            a_copy.triggered.connect(
                lambda: self._copy_safeguard_dialog(sg_id))
            a_move = menu.addAction("↕  Flytta till annan konsekvens…")
            a_move.triggered.connect(
                lambda: self._move_safeguard_dialog(sg_id))
            if sg and (dict(sg).get('comp_tag') or dict(sg).get('comp_type')):
                a_untag = menu.addAction(_icon('close'), "Ta bort tagg")
                a_untag.triggered.connect(lambda sid=sg_id: self._untag_safeguard(sid))
            menu.addSeparator()
            a_del = menu.addAction(_icon('delete'), "Ta bort barriär")
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
        btn_ren_c  = QPushButton("Byt namn")
        btn_ren_c.setIcon(_icon('edit'))
        btn_del_c  = QPushButton("Ta bort")
        btn_del_c.setIcon(_icon('delete'))
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
        btn_del_m = QPushButton("Ta bort vald")
        btn_del_m.setIcon(_icon('delete'))
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


# ══════════════════════════════════════════════════════════════════════════════
# EQUIPMENT PANEL
# ══════════════════════════════════════════════════════════════════════════════

def _tag_prefix(tag: str) -> str:
    m = re.match(r'^([A-Z]+)', tag.upper())
    return m.group(1) if m else tag


class ObjectPickerPopup(QDialog):
    """Lets the user pick an already-registered P&ID object
    (equipment_catalog row — everything found via manual add or
    "🎯 Hitta objekt på P&ID", regardless of its green/red marker state)
    to auto-tag a newly created cause/consequence/safeguard. Used by the
    "+" quick-add rows (2026-08-12, see NOTES.md) as an alternative to
    having to drag-and-drop a marker from the P&ID for every new row.

    Three outcomes, distinguished by `.selected` after a call to exec():
    - exec() != Accepted (Escape/closed): whole add was cancelled, caller
      must not create a new row at all.
    - exec() == Accepted and .selected is a dict: user picked that object.
    - exec() == Accepted and .selected is None: user explicitly skipped
      tagging — caller still creates the row, just untagged (free text),
      same as clicking "+" always did before this feature existed.
    """

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self._db = db
        self.selected = None
        self.setWindowTitle("Välj objekt")
        self.setMinimumWidth(340)
        self.setMinimumHeight(380)

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        hdr = QLabel(
            "Välj ett registrerat P&ID-objekt att koppla till den nya "
            "raden, eller hoppa över för fri text.")
        hdr.setWordWrap(True)
        hdr.setStyleSheet("font-size:10px; color:#8D9299;")
        layout.addWidget(hdr)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Sök tagg, typ eller beskrivning…")
        self._search.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search)

        self._list = QListWidget()
        self._list.itemSelectionChanged.connect(self._update_pick_enabled)
        self._list.itemDoubleClicked.connect(lambda _item: self._accept_selected())
        layout.addWidget(self._list, 1)

        btns = QHBoxLayout()
        self._pick_btn = QPushButton("Välj objekt")
        self._pick_btn.setDefault(True)
        self._pick_btn.setEnabled(False)
        self._pick_btn.clicked.connect(self._accept_selected)
        skip_btn = QPushButton("Hoppa över (fri text)")
        skip_btn.clicked.connect(self._accept_skip)
        btns.addWidget(self._pick_btn)
        btns.addStretch()
        btns.addWidget(skip_btn)
        layout.addLayout(btns)

        # Populated after _pick_btn exists — _populate() enables/disables
        # it based on the current selection.
        self._items = [dict(r) for r in db.equipment_items()]
        self._populate(self._items)

        self._search.setFocus()

    def _populate(self, items):
        self._list.clear()
        for row in items:
            label = f"{row.get('tag') or '(ingen tagg)'}  —  {row.get('equipment_type') or '?'}"
            if row.get('description'):
                label += f"  —  {row['description']}"
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, row)
            self._list.addItem(it)
        self._update_pick_enabled()

    def _update_pick_enabled(self):
        self._pick_btn.setEnabled(self._list.currentItem() is not None)

    def _apply_filter(self, text):
        text = (text or '').strip().lower()
        if not text:
            self._populate(self._items)
            return
        filtered = [r for r in self._items
                    if text in (r.get('tag') or '').lower()
                    or text in (r.get('equipment_type') or '').lower()
                    or text in (r.get('description') or '').lower()]
        self._populate(filtered)

    def _accept_selected(self):
        it = self._list.currentItem()
        if it is None:
            return
        self.selected = it.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _accept_skip(self):
        self.selected = None
        self.accept()


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

        # Non-editable dropdown (2026-08-13 follow-up: "Rullgardinen ...
        # har försvunnit. Det ska vara de valen som det var innan" — an
        # editable QComboBox loses its usual dropdown-arrow affordance
        # under this app's global stylesheet, so it looked broken instead
        # of just "also typable"). A brand-new type is instead added via
        # the "+" button next to it, which keeps the normal pick-from-list
        # experience intact and adds a distinct, explicit action for the
        # rarer "this type doesn't exist yet" case.
        self._type_cb = QComboBox()
        self._type_cb.setFixedHeight(CONFIG['H_BTN_SMALL'])
        self._type_cb.setStyleSheet(_small)
        self._type_cb.addItems(_equipment_type_options(db))
        if suggested_type:
            idx = self._type_cb.findText(suggested_type)
            if idx < 0:
                self._type_cb.addItem(suggested_type)
                idx = self._type_cb.count() - 1
            self._type_cb.setCurrentIndex(idx)
        typ_row = QHBoxLayout()
        typ_row.setSpacing(4)
        typ_row.addWidget(self._type_cb)
        add_type_btn = QPushButton("+")
        add_type_btn.setFixedSize(CONFIG['H_BTN_SMALL'], CONFIG['H_BTN_SMALL'])
        add_type_btn.setToolTip("Lägg till en ny objekttyp")
        add_type_btn.clicked.connect(self._add_new_type)
        typ_row.addWidget(add_type_btn)
        typ_lbl = QLabel("Typ:")
        typ_lbl.setStyleSheet(_small)
        form.addRow(typ_lbl, typ_row)
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

    def _add_new_type(self):
        """"+" button next to the Typ dropdown (2026-08-13 follow-up) —
        equipment_catalog.equipment_type is plain free text, so a brand
        new type just needs adding to the combo and selecting; no
        equipment_catalog write happens until the popup itself is
        committed via _ok(). Also registers the name as a Standardobjekt
        right away (2026-08-13, same-day follow-up: "lägger jag till
        ytterligare något här skall det också dyka upp i standardobjekt
        ... Dessa skall prata med varandra") so it's immediately
        available in the cause-suggestion forms too, not just here."""
        name, ok = QInputDialog.getText(self, "Ny objekttyp", "Namn:")
        name = (name or '').strip()
        if not ok or not name:
            return
        idx = self._type_cb.findText(name)
        if idx < 0:
            self._type_cb.addItem(name)
            idx = self._type_cb.count() - 1
        self._type_cb.setCurrentIndex(idx)
        exists = self._db.conn.execute(
            "SELECT 1 FROM standard_objects WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
        if not exists:
            self._db.add_standard_object(name)

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
        self._scan_btn = QPushButton("Skanna P&ID")
        self._scan_btn.setIcon(_icon('scan', 16, '#ffffff'))
        self._scan_btn.setToolTip("Skannar inläst P&ID-fil efter utrustningstaggar")
        self._scan_btn.setStyleSheet(
            "background:#2F5FD0; color:white; border:none; border-radius:4px; padding:3px 10px;")
        self._scan_btn.clicked.connect(self._scan)

        add_btn = QPushButton("+ Lägg till")
        add_btn.setToolTip("Lägg till en tagg manuellt")
        add_btn.clicked.connect(self._add_manual)

        refresh_btn = QPushButton("Uppdatera")
        refresh_btn.setIcon(_icon('refresh'))
        refresh_btn.clicked.connect(self.refresh)

        self._create_btn = QPushButton("🏭 Skapa HAZOP-noder")
        self._create_btn.setToolTip("Skapar en nod per ikryssad rad")
        self._create_btn.clicked.connect(self._create_nodes)

        self._autodetect_btn = QPushButton("Hitta objekt på P&ID")
        self._autodetect_btn.setIcon(_icon('target'))
        self._autodetect_btn.setToolTip(
            "Analyserar utrustning (ventiler, pumpar, instrument m.fl.): kopplar\n"
            "varje känd tagg till dess ritade symbol OCH letar efter formigenkända\n"
            "symboler som saknar tagg — allt i en bakgrundskörning med synlig\n"
            "progress.\n"
            "Kör 🔍 Skanna P&ID först om registret är tomt.")
        self._autodetect_btn.clicked.connect(self._autodetect)

        clear_btn = QPushButton("Rensa utrustning")
        clear_btn.setIcon(_icon('delete'))
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

    def set_db(self, db):
        """Swap the database, including the table model's own separate
        db reference (needed for setData()/delete_row() to write through
        directly — see MainWindow._reload_all_panels)."""
        self.db = db
        self._model.db = db

    def autodetect(self):
        """Public entry point for 🎯 Hitta objekt på P&ID — see _autodetect."""
        self._autodetect()

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
            review_dlg = EquipmentMarkerReviewDialog(
                results, self.db, parent=self, rejected=rejected, pdf_path=path)
            if review_dlg.exec():
                self.markers_saved.emit()

        thread.page_progress.connect(dlg.set_page_status)
        thread.finished_analysis.connect(_on_finished)
        dlg.canceled.connect(_on_cancel)
        thread.start()
        dlg.exec()


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
