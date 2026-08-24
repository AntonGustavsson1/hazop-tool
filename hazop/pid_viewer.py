#!/usr/bin/env python3
"""P&ID viewer module for the HAZOP tool."""

import re
import json
import os
import sqlite3
import shutil
import tempfile
import datetime
import math
import logging
import importlib.util
import multiprocessing
import concurrent.futures
from pathlib import Path
from functools import partial

from constants import _bundle_dir
import symbol_geometry
import equipment_detection
import image_symbol_matching
from equipment_detection import (
    ocr_status, cleanup_ocr_resources,
    _get_easyocr_reader, _preprocess_for_ocr,
    COMPONENT_TYPES, VALVE_COMPONENT_TYPES, KNOWN_PREFIXES,
    _equip_prefix_from_tag, _extract_prefix, _spatial_combine, _rotate_words,
    _pick_best_tag, _next_tag_sequence,
    scan_pdf_for_equipment, apply_scan_result_to_equipment_catalog,
    upsert_identified_tags_from_scan, detect_equipment_symbols,
    find_valve_shapes, detect_equipment_and_valves, find_tag_near_point,
    extract_tag_from_rect,
    _row_confidence, _row_confidence_breakdown,
)
# NOTE: HAS_TESSERACT/HAS_EASYOCR/HAS_RAPIDOCR/HAS_PIL are intentionally
# NOT imported here — pid_viewer.py detects them independently below
# (equipment_detection.py has its own separate copy of this same
# detection block; see that module's docstring for why it's duplicated
# rather than shared).

# Suppress Qt SVG parser warnings (font references, path truncations)
# These come from PyMuPDF's SVG output and are harmless display artefacts.
from PyQt6.QtCore import qInstallMessageHandler, QtMsgType

def _qt_msg_handler(mode, context, message):
    if message.startswith('qt.svg:'):
        return
    import sys
    print(message, file=sys.stderr)

qInstallMessageHandler(_qt_msg_handler)

from PyQt6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QListWidget, QListWidgetItem, QAbstractItemView,
    QLineEdit, QLabel, QPushButton, QDialogButtonBox, QRadioButton,
    QGraphicsView, QGraphicsScene, QGraphicsItem,
    QGraphicsPixmapItem, QGraphicsPathItem, QGraphicsEllipseItem,
    QGraphicsPolygonItem,
    QGraphicsRectItem, QGraphicsLineItem, QGraphicsSimpleTextItem, QFrame, QSpinBox, QAbstractSpinBox, QCheckBox, QGroupBox,
    QSlider, QColorDialog, QFileDialog, QMessageBox, QInputDialog,
    QSizePolicy, QMenu, QTableWidget, QTableWidgetItem, QHeaderView,
    QProgressDialog, QApplication, QGridLayout, QTextEdit, QButtonGroup,
    QScrollArea, QProgressBar,
)
from PyQt6.QtCore import Qt, pyqtSignal, QPointF, QRectF, QThread, QPoint, QTimer, QMimeData
from PyQt6.QtGui import (
    QColor, QPen, QBrush, QPainterPath, QPolygonF, QPixmap, QImage, QFont,
    QPainter, QPicture, QCursor, QShortcut, QKeySequence, QDrag, QIcon,
)

# Optional OpenGL for GPU-accelerated rendering. Must define QOpenGLWidget
# as a fallback in the except branch (2026-08-24, see NOTES.md "Analysera
# P&ID kraschar och startar om appen vid flera sidor") -- matching the
# QSvgRenderer pattern right below. Without it, if PyQt6.QtOpenGLWidgets
# fails to import in some environment (seen in a PyInstaller-frozen build,
# non-deterministically across otherwise-identical rebuilds -- likely a Qt
# plugin/DLL bundling gap for this optional module), QOpenGLWidget simply
# never exists as a name here at all. pid_graphics_view.py's own
# `from pid_viewer import (..., QOpenGLWidget, ...)` then fails with
# "cannot import name 'QOpenGLWidget' from partially initialized module
# 'pid_viewer' (most likely due to a circular import)" -- a genuinely
# misleading message: Python guesses "circular import" as the likely
# cause of ANY failed from-import of a missing name, even when the real
# cause (as here) is that the name was never defined in the first place.
try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    HAS_OPENGL = True
except ImportError:
    QOpenGLWidget = None
    HAS_OPENGL = False

# Optional SVG vector rendering (preferred — stays sharp at any zoom)
try:
    from PyQt6.QtSvg import QSvgRenderer
    HAS_SVG_RENDERER = True
except ImportError:
    QSvgRenderer = None
    HAS_SVG_RENDERER = False

try:
    import fitz
    HAS_PYMUPDF = True
except Exception:
    fitz = None
    HAS_PYMUPDF = False

# ── Optional OCR engines ──────────────────────────────────────────────────────
try:
    import pytesseract
    HAS_TESSERACT = True
except ImportError:
    pytesseract = None
    HAS_TESSERACT = False

# easyocr and rapidocr_onnxruntime pull in torch/onnxruntime, which take
# several seconds to import. Neither is needed until OCR is actually run, so
# only check whether they're installed here (find_spec is near-instant) and
# defer the real `import` to _OCRLifecycleManager.get_*_reader() below.
HAS_EASYOCR = importlib.util.find_spec('easyocr') is not None
HAS_RAPIDOCR = importlib.util.find_spec('rapidocr_onnxruntime') is not None

try:
    from PIL import Image as _PILImage, ImageFilter, ImageEnhance, ImageOps
    HAS_PIL = True
except ImportError:
    _PILImage = None
    HAS_PIL = False


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION & MAGIC NUMBER CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

CONFIG = {
    # ===== TIMERS (milliseconds) =====
    'TIMER_PDF_EXTRACT_MS': 100,      # PDF line extraction

    # ===== WIDGET DIMENSIONS (pixels) - shared with hazop.py =====
    'W_ICON_BTN': 28,                 # Icon button
    'W_SPINNER': 58,                  # Spinner width
    'H_ROW_STD': 34,                  # Standard row (scenario banner)
    'H_SMALL_BTN': 20,                # Small banner button
}

# ══════════════════════════════════════════════════════════════════════════════
# ICON RENDERER — shared with hazop.py (moved here 2026-08-12, see NOTES.md)
# ══════════════════════════════════════════════════════════════════════════════
# Originally lived in hazop.py, but hazop.py imports FROM pid_viewer.py (not
# the reverse) — pid_viewer.py's own emoji (equipment-marker popups, gesture
# tooltips, etc.) had no way to reach it without a circular import. Moved
# here since this code has no Database/MainWindow dependency, only Qt +
# stdlib; hazop.py re-imports _mk_pm/_mk_icon/_icon/_EMOJI_ICON from here so
# every existing call site in hazop.py keeps working unchanged.

_ICONS_DIR = _bundle_dir() / 'icons'
_SVG_ICON_CACHE: dict[str, str] = {}   # name -> raw SVG text (read once, recolored per call)
# Old procedural-shape names that now have a hand-drawn SVG equivalent under
# icons/ (2026-08-12) — arrow_up/arrow_down previously matched no branch in
# _mk_pm() at all and silently rendered blank icons (NodeMarkupPanel's
# prev/next nav buttons); chevron-up/down fixes that as a side effect.
_SVG_ICON_ALIASES = {'arrow_up': 'chevron-up', 'arrow_down': 'chevron-down'}


def _load_svg_icon_pixmap(name: str, sz: int, fg: QColor) -> QPixmap | None:
    """Render icons/<name>.svg (one of the flat-line icons, originally
    stroked #42474d) recolored to fg, or None if no such file exists so
    _mk_pm() can fall back to its older procedural shapes."""
    name = _SVG_ICON_ALIASES.get(name, name)
    svg_str = _SVG_ICON_CACHE.get(name)
    if svg_str is None:
        path = _ICONS_DIR / f'{name}.svg'
        if not path.exists():
            return None
        svg_str = path.read_text(encoding='utf-8')
        _SVG_ICON_CACHE[name] = svg_str
    svg_str = svg_str.replace('#42474d', fg.name())
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
    return pm


def _mk_pm(name: str, sz: int, fg: QColor) -> QPixmap:
    """Render one icon onto a transparent QPixmap. Prefers a hand-drawn SVG
    from icons/ (2026-08-12) when one matches; falls back to the older
    procedural QPainter shapes below for names with no SVG equivalent
    (select/polygon/polyline/text/smart — P&ID markup drawing tools)."""
    svg_pm = _load_svg_icon_pixmap(name, sz, fg)
    if svg_pm is not None:
        return svg_pm

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


# Emoji -> icons/<name>.svg mapping, shared by every button/menu-action
# construction site being migrated off raw Unicode glyphs (2026-08-12, see
# NOTES.md). Only emoji with an unambiguous, high-confidence icon match are
# listed — anything not here (status dots, one-off glyphs) intentionally
# keeps rendering as plain emoji text.
_EMOJI_ICON = {
    '📋': 'clipboard', '🗑': 'delete', '📍': 'pin',
    '⚙': 'settings', '🔧': 'settings',
    '✏': 'edit', '✎': 'edit', '📝': 'edit',
    '💬': 'comment', '🔍': 'search', '🔎': 'search',
    '👁': 'eye', '🎯': 'target', '🛡': 'shield', '⚠': 'warning',
    '✓': 'check', '✅': 'check', '✕': 'close', '✖': 'close', '❌': 'close',
    '💾': 'save', '🔗': 'link', '🔄': 'refresh',
    '📤': 'export', '📂': 'import', '📄': 'document', '🏭': 'factory',
    '➕': 'add',
    # Second batch (2026-08-12, designed against icon_requests/README.md)
    '📊': 'chart', '↔': 'resize-horizontal', '📐': 'resize-rotate',
    '🖱': 'cursor', '↕': 'resize-vertical', '⚡': 'lightning',
    '🔬': 'microscope', '🔀': 'shuffle', '✨': 'sparkle',
    '🏷': 'tag', '⚗': 'flask', '🧠': 'brain', '📈': 'trend-chart',
    '🌙': 'moon', '🗺': 'map', '🔩': 'bolt-nut', '🦋': 'valve-shape',
    '✂': 'unlink', '↩': 'undo',
}


def _icon(name: str, sz: int = 16, color: str = '#17191C') -> QIcon:
    """Single-state QIcon from icons/<name>.svg (or an _mk_pm() procedural
    shape) for ordinary QPushButton/QAction/QToolButton icons — as opposed
    to _mk_icon()'s two-state dark/white pair for the checkable P&ID markup
    ribbon tool buttons. Default color matches the app's own body-text
    color (#17191C) so the icon reads consistently against the QSS theme's
    button/menu backgrounds (2026-08-12, see NOTES.md)."""
    return QIcon(_mk_pm(name, sz, QColor(color)))


# ══════════════════════════════════════════════════════════════════════════════
# OCR AUTO-INSTALLER
# ══════════════════════════════════════════════════════════════════════════════

def ensure_ocr_available(parent=None) -> bool:
    """Check if RapidOCR is available. Show hint to run starta_hazop.bat if not."""
    if HAS_TESSERACT or HAS_EASYOCR or HAS_RAPIDOCR:
        return True
    QMessageBox.information(
        parent, "OCR saknas",
        "RapidOCR är inte installerat.\n\n"
        "Kör  starta_hazop.bat  en gång för att installera alla beroenden\n"
        "(inkl. rapidocr_onnxruntime, ~25 MB).\n\n"
        "Textextraktion ur PDF-filer med vektorgrafik fungerar inte utan OCR.")
    return False


def resolve_ocr_scan_choice(db, parent=None):
    """Decide whether/which OCR engine to use for a P&ID scan ("🔍 Skanna
    P&ID" / "📋 Analysera P&ID"), honouring the "OCR-standardval" setting
    added to Inställningar → P&ID-inställningar (config key
    'ocr_default_engine', 2026-08-11).

    If that setting names a specific engine that is actually installed (or
    'auto'), the interactive Yes/No prompt is skipped entirely and that
    engine is used directly. Otherwise (setting is 'ask' — the default —
    or names an engine that is no longer installed) falls back to the
    original per-scan Yes/No question, unchanged.

    Returns (use_ocr: bool, ocr_engine: str) — ocr_engine is only
    meaningful when use_ocr is True and is otherwise 'auto'.
    """
    st = ocr_status()
    default_choice = db.get_config('ocr_default_engine', 'ask') if db else 'ask'
    if default_choice and default_choice != 'ask':
        if default_choice == 'auto' and (st['tesseract'] or st['easyocr'] or st['rapidocr']):
            return True, 'auto'
        if default_choice in ('tesseract', 'easyocr', 'rapidocr') and st.get(default_choice):
            return True, default_choice
        # Configured engine no longer installed -- fall through to asking.

    use_ocr = False
    if st['tesseract'] or st['easyocr']:
        engines = [n for n, v in [('pytesseract', st['tesseract']),
                                   ('easyocr', st['easyocr'])] if v]
        reply = QMessageBox.question(
            parent, "OCR",
            f"Tillgänglig OCR-motor: {', '.join(engines)}\n\n"
            "Använd OCR för sidor med lite text?\n"
            "(Bättre för skannade ritningar, tar längre tid.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        use_ocr = (reply == QMessageBox.StandardButton.Yes)
    return use_ocr, 'auto'


CONSEQUENCE_TEMPLATES = [
    'Överfyllnad av {}',
    'Övertryck i {}',
    'Undertryck i {}',
    'Utsläpp / läcka från {}',
    'Inget flöde till {}',
    'För högt flöde till {}',
    'Felaktig temperatur i {}',
    'Kontaminering av {}',
    'Brand / explosion vid {}',
    'Driftstopp för {}',
    'Toxisk exponering vid {}',
    'Miljöutsläpp från {}',
]

# ── Consequence chain (mirrors hazop.py — no circular import) ────────────────
_PID_CHAIN_ITEMS = [
    ('loc',           'LOC — Utsläpp / läcka',            'Intermediär händelse'),
    ('fire',          'Brand',                             'Antändning / explosion'),
    ('flash_fire',    'Flash fire',                        None),
    ('explosion',     'Explosion (VCE / BLEVE)',           None),
    ('toxic',         'Toxisk exponering',                 'Toxisk / miljö'),
    ('environmental', 'Miljöutsläpp',                     None),
    ('personnel',     'Personskador',                      'Personell / tillgång'),
    ('fatality',      'Dödsfall',                          None),
    ('equipment',     'Utrustningsskador',                 None),
    ('production',    'Driftstopp',                        None),
]


def _pid_build_chain_text(base: str, chain: dict) -> str:
    parts = [base.strip()] if base.strip() else []
    for key, label, _ in _PID_CHAIN_ITEMS:
        if chain.get(key):
            short = label.split('(')[0].strip().split(' — ')[-1].strip()
            parts.append(short)
    return ' → '.join(parts)


MODE_NAV             = 0
MODE_NODE            = 1
MODE_MARKUP_POLYGON  = 7   # draw closed polygon markup on a node
MODE_MARKUP_POLYLINE = 8   # draw open polyline markup on a node
MODE_MARKUP_TEXT     = 9   # click to place a text label markup
MODE_MARKUP_COMMENT  = 10  # click to place a comment box markup
MODE_MARKUP_SELECT   = 11  # click existing markup items to select/edit
MODE_SMART_POLYLINE  = 12  # click start+end, algorithm traces pipe path
MODE_RED_MARKUP_SYMBOL = 13  # click to place a red markup P&ID symbol
MODE_BOARD_LAYOUT    = 14  # drag pages to reposition on study board
MODE_ADD_SHEET_LINK  = 15  # click target page to create a manual inter-sheet link
MODE_PICK_REF_TAG   = 16  # one-shot click: detect tag near point → emit ref_tag_picked
MODE_ANNOTATION     = 17  # click on board to place a sticky note

# ── Off-page connector analysis ───────────────────────────────────────────────
_RE_TO_FROM = re.compile(
    r'\b(TO|FROM|CONT\'?D\.?(?:\s+ON)?|TILL|FR[ÅA]N|FRAN)\s+'
    r'([A-Z0-9\+][A-Z0-9\-/\.\+]{1,30})', re.IGNORECASE)
# Direction keyword alone (no ref capture) — used to classify in/out when the
# sheet reference was found via a dialect pattern instead of TO/FROM capture.
_RE_DIR_KW = re.compile(
    r"\b(TO|FROM|CONT'?D\.?(?:\s+ON)?|TILL|FR[ÅA]N|FRAN)\b", re.IGNORECASE)
_RE_LINE_ID = re.compile(
    r'\b(\d{1,4}["\']\-[A-Z]{1,5}\-\d{3,6}[A-Z0-9\-]*|\d{1,4}\-[A-Z]{1,5}\-\d{3,6}[A-Z0-9\-]*)\b')
# Universal sheet-number regex — covers all known customer formats
_RE_SHEET_NUM = re.compile(
    r'\b('
    r'S\d{6,8}'                                 # LKAB:    S0000155
    r'|[A-Z]{2,6}_\d{4,8}'                      # ITS:     XFB_11338
    r'|[A-Z]{2,4}_[A-Z]{2,4}_\d{3,6}'          # Gryaab:  AD_PFS_0003
    r'|[A-Z]{2,4}[-_][A-Z]{2,4}[-_]\d{3,6}'   # Gryaab:  AD-PFS-0003
    r'|[A-Z]{1,5}\-\d{2,6}[A-Z]?\d?'           # classic: P-101
    r'|\d{3}-\d{4}-\d{3}(?:-[A-Z]{1,4})?'      # Hybrit:  242-0000-001, 253-0000-002-PS
    r'|[A-Z]\d{1,2}-\d{3}-\d{3,4}'             # Smurfit: R1-077-012
    r'|\+\d{2,4}[A-Z]\d{3}'                    # Loket:   +100D001
    r'|\d{4,6}\-\d{2,4}'                        # old:     1234-01
    r')\b', re.IGNORECASE)
# LKAB-specific: =M1.GPA3   S0000155
_RE_RDS_SHEET = re.compile(
    r'=([A-Z][A-Z0-9./\-]{1,25})\s+S(\d{6,8})', re.IGNORECASE)
# ITS: XFB_40208/001.2F  — extract the drawing code as the sheet reference
_RE_ITS_CONN = re.compile(
    r'\b([A-Z]{2,6}_\d{4,8})/\d{3,6}\.\w{1,4}\b', re.IGNORECASE)
# Gryaab: AD_DP76, AD_PFS_0003, AD_RR0032-350
_RE_GRYAAB_CONN = re.compile(
    r'\b([A-Z]{2,4}[_-][A-Z]{1,4}\d{2,5}(?:[_-]\d+)?)\b', re.IGNORECASE)

# ── Dialect definitions ────────────────────────────────────────────────────────
# Each dialect describes how to recognise sheet numbers and cross-references for
# a particular P&ID style.  ConnectorAnalyzer auto-detects the best-fitting
# dialect from the first few pages before parsing begins.
_DIALECTS = {
    'lkab': {
        'name':         'LKAB (RDS + S-nummer)',
        'score_re':     re.compile(r'\bS\d{6,8}\b|=[A-Z][A-Z0-9./\-]{1,10}\s+S\d{6,8}', re.I),
        'sheet_num_re': re.compile(r'\b(S\d{6,8})\b', re.I),
        'title_area':   (0.45, 0.86, 1.0, 1.0),
    },
    'its': {
        'name':         'ITS (XFB_NNNNN/NNN.NN)',
        'score_re':     re.compile(r'[A-Z]{2,6}_\d{4,8}/\d{3,6}\.\w', re.I),
        'sheet_num_re': re.compile(r'\b([A-Z]{2,6}_\d{4,8})\b', re.I),
        'title_area':   (0.5, 0.7, 1.0, 1.0),
    },
    'gryaab': {
        'name':         'Gryaab (AD_COMPONENT)',
        'score_re':     re.compile(r'\b[A-Z]{2,4}[_-][A-Z]{1,4}\d{2,5}\b', re.I),
        'sheet_num_re': re.compile(r'\b([A-Z]{2,4}[-_][A-Z]{2,4}[-_]\d{3,6})\b', re.I),
        'title_area':   (0.5, 0.7, 1.0, 1.0),
    },
    'hybrit': {
        'name':         'Hybrit (NNN-NNNN-NNN)',
        'score_re':     re.compile(r'\b\d{3}-\d{4}-\d{3}|\b(TILL|FR[ÅA]N)\b', re.I),
        'sheet_num_re': re.compile(r'\b(\d{3}-\d{4}-\d{3})(?:-[A-Z]{1,4})?\b'),
        'title_area':   (0.0, 0.85, 1.0, 1.0),
    },
    'classic': {
        'name':         'Classic (TO/FROM/TILL/FRÅN DWG)',
        'score_re':     re.compile(r'\b(TO|FROM|CONT\'?D|TILL|FR[ÅA]N)\b', re.I),
        'sheet_num_re': _RE_SHEET_NUM,
        'title_area':   (0.45, 0.75, 1.0, 1.0),
    },
}

def _sheet_ref_variants(ref: str):
    """Return lookup variants for a sheet identifier string.

    Handles the LKAB mismatch where page_sheet_nums stored '0000292' (digits only)
    but ref_sheet is 'S0000292'.  Returns both forms so either DB format matches.
    """
    ref = (ref or '').upper().strip()
    if not ref:
        return (ref,)
    variants = [ref]
    if len(ref) > 1 and ref[0] == 'S' and ref[1:].isdigit():
        variants.append(ref[1:])          # 'S0000292' → '0000292'
    elif ref.isdigit():
        variants.append('S' + ref)        # '0000292'  → 'S0000292'
    return variants


def _detect_dialect(sample_texts):
    """Score each dialect against sample text from the first few pages."""
    scores = {d: 0 for d in _DIALECTS}
    for text in sample_texts:
        for dname, dconf in _DIALECTS.items():
            scores[dname] += len(dconf['score_re'].findall(text))
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else 'classic'

_MEDIA_PATTERNS = [
    # Slurry / pulp
    ('slurry',        re.compile(
        r'\b(SLURRY|PULP|MUD|SLUDGE|UNDERFLOW|THICKENER'
        r'|SLAM|MASSA|LERA)\b', re.I)),
    # Filtrate / leach liquor
    ('filtrate',      re.compile(
        r'\b(FILTRAT|FILTRATE|LEACH\s*FILTRAT|LAKFILTRAT|LAKVÄTSKA)\b', re.I)),
    # Chemicals
    ('chemical',      re.compile(
        r'\b(HCL|HCl|H[Cc][Ll]|CAUSTIC|ACID|MEG|NAOH|METHANOL|INHIBITOR'
        r'|GLYCOL|KCL|KCl|FLOCCULANT|AMMONIAK|AMMONIA|KALK|LIME'
        r'|KEMIKALIE|KEMIKALIER|SVAVELS?YRA|SALTSYRA)\b', re.I)),
    # Leach gas
    ('leach_gas',     re.compile(r'\b(LEACH\s*GAS|LEACHING\s*GAS|LAKGAS)\b', re.I)),
    # Process fluid (general)
    ('process',       re.compile(
        r'\b(FEED|PRODUCT|CRUDE|HC|PROCESS|RAW|APATITE|RESIDUE'
        r'|PANNVATTEN|KONDENSAT|CONDENSATE)\b', re.I)),
    # Flue gas / combustion gas / pure gases
    ('gas',           re.compile(
        r'\b(GAS|VAPOR|VAPOUR|VG|FLUE|RÖKGAS|RÖKGASER'
        r'|FÖRBRÄNNINGSGAS|AVGASER|KVÄVGAS|NITROGEN|VÄTGAS|HYDROGEN|H2'
        r'|SYRGAS|OXYGEN|O2|NATURGAS|BIOGAS|SYNGAS)\b', re.I)),
    # Liquid (general)
    ('liquid',        re.compile(r'\b(LIQ|LIQUID|VÄTSKA)\b', re.I)),
    # Steam
    ('utility_steam', re.compile(
        r'\b(STEAM|HP[\s\-]?STEAM|MP[\s\-]?STEAM|LP[\s\-]?STEAM'
        r'|ÅNGA|HÖGTRYCKSÅNGA|LÅGTRYCKSÅNGA|MELLANTRYCKSÅNGA'
        r'|SOTBLÅSNINGSÅNGA|ÖVERHETTAD)\b', re.I)),
    # Water (utility)
    ('utility_water', re.compile(
        r'\b(C\.?W\.?|F\.?W\.?|P\.?W\.?|BFW|COOLING\s*WATER|FIRE\s*WATER'
        r'|HEATING\s*WATER|PROCESS\s*WATER|MATARVATTEN|KONDENSATTANK'
        r'|KYLVATTEN|DRICKSVATTEN|RÅVATTEN|PROCESSVATTEN|FJÄRRVÄRME'
        r'|SPÄDVATTEN|KYLARVATTEN|SPOLVATTEN|AVJONISERAT)\b', re.I)),
    # Combustion / instrument air
    ('utility_air',   re.compile(
        r'\b(I\.?A\.?|P\.?A\.?|C\.?A\.?|INSTRUMENT\s*AIR|PLANT\s*AIR'
        r'|FÖRBRÄNNINGSLUFT|INSTRUMENTLUFT|TRYCKLUFT|VENTILATIONSLUFT)\b', re.I)),
    # Instrument / signal
    ('instrument',    re.compile(
        r'\b(SIG|SIGNAL|4[\.\-]20|ESD|SIS|INTERLOCK|STYRSIGNAL)\b', re.I)),
    # Biofuel / solid fuel
    ('process',       re.compile(
        r'\b(BIOBRÄNSLE|BRÄNSLE|BOTTENASKA|SANDSILO|SLAMINMATNING'
        r'|REJEKTSILO|SANDÅTERVINNING)\b', re.I)),
    # Drain / vent / flare
    ('drain_vent',    re.compile(
        r'\b(DRAIN|VENT|ATM|FLARE|BLOWDOWN|AVLOPP|AVLUFTNING|FACKEL)\b', re.I)),
]
_MEDIA_WEIGHTS = {
    'process': 1.0, 'gas': 0.9, 'chemical': 0.8, 'liquid': 0.7,
    'slurry': 0.6, 'filtrate': 0.6, 'leach_gas': 0.55,
    'utility_steam': 0.4, 'utility_water': 0.3,
    'utility_air': 0.2, 'instrument': 0.15, 'drain_vent': 0.1, 'unknown': 0.05,
}
# Display colors per media type (used for sheet-connection arcs on the board)
_MEDIA_COLORS = {
    'process':       '#2855d4',   # blue
    'gas':           '#9b59b6',   # purple
    'chemical':      '#e74c3c',   # red
    'filtrate':      '#e67e22',   # orange
    'leach_gas':     '#8e44ad',   # dark purple
    'liquid':        '#2980b9',   # steel blue
    'slurry':        '#a0522d',   # brown
    'utility_steam': '#c0392b',   # dark red
    'utility_water': '#17a589',   # teal
    'utility_air':   '#7f8c8d',   # grey
    'instrument':    '#f39c12',   # yellow-orange
    'drain_vent':    '#95a5a6',   # silver
    'unknown':       '#5d6d7e',   # slate
}

SG_TYPES    = ['BPCS', 'SIS', 'Mekanisk', 'Administrativ', 'Övrigt']
_RRF_VALUES  = [1, 10, 100, 1000, 10000]
RRF_LABELS  = ['1 – Ingen', '10 – RRF10', '100 – RRF100', '1000 – RRF1000', '10000 – RRF10000']

# ── Red Markup P&ID Symbols ───────────────────────────────────────────────────
_RED_MARKUP_SYMBOLS = {
    "Ventiler": [
        ("gate_valve",     "Spjällventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><polygon points="2,11 24,24 2,37"/><polygon points="46,11 24,24 46,37"/></g></svg>'),
        ("gate_valve_nc",  "Spjällventil NC",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5"><line x1="2" y1="24" x2="46" y2="24" fill="none"/><polygon points="2,11 24,24 2,37" fill="red"/><polygon points="46,11 24,24 46,37" fill="red"/></g></svg>'),
        ("butterfly_valve","Fjärilsventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><line x1="24" y1="7" x2="24" y2="41"/><line x1="9" y1="9" x2="39" y2="39"/><line x1="39" y1="9" x2="9" y2="39"/></g></svg>'),
        ("check_valve",    "Backventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><polygon points="10,11 32,24 10,37"/><line x1="32" y1="11" x2="32" y2="37"/></g></svg>'),
        ("globe_valve",    "Globventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><polygon points="2,11 24,24 2,37"/><polygon points="46,11 24,24 46,37"/><circle cx="24" cy="24" r="6"/></g></svg>'),
        ("ball_valve",     "Kulventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5"><line x1="2" y1="24" x2="46" y2="24" fill="none"/><polygon points="2,11 24,24 2,37" fill="red"/><polygon points="46,11 24,24 46,37" fill="red"/></g></svg>'),
        ("safety_valve",   "Säkerhetsventil (PSV)",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5"><line x1="24" y1="2" x2="24" y2="16" fill="none"/><polygon points="8,16 40,16 24,40" fill="red"/><line x1="24" y1="40" x2="24" y2="46" fill="none"/><line x1="14" y1="12" x2="34" y2="12" fill="none"/><line x1="14" y1="8" x2="34" y2="8" fill="none"/></g></svg>'),
        ("control_valve",  "Reglerventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5"><line x1="2" y1="30" x2="46" y2="30" fill="none"/><polygon points="2,18 24,30 2,42" fill="red"/><polygon points="46,18 24,30 46,42" fill="red"/><line x1="24" y1="30" x2="24" y2="18" fill="none"/><polygon points="16,4 24,18 32,4" fill="none" stroke="red"/></g></svg>'),
        ("hand_valve",     "Handventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="28" x2="46" y2="28"/><polygon points="2,16 24,28 2,40"/><polygon points="46,16 24,28 46,40"/><line x1="24" y1="28" x2="24" y2="15"/><line x1="16" y1="11" x2="32" y2="11"/><path d="M16,11 Q24,6 32,11"/></g></svg>'),
        ("motor_valve",    "Motorventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5"><line x1="2" y1="30" x2="46" y2="30" fill="none"/><polygon points="2,18 24,30 2,42" fill="red"/><polygon points="46,18 24,30 46,42" fill="red"/><line x1="24" y1="30" x2="24" y2="19" fill="none"/><rect x="17" y="7" width="14" height="12" fill="none" stroke="red"/></g></svg>'),
        ("three_way_valve","Trevägsventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><line x1="24" y1="24" x2="24" y2="46"/><polygon points="2,14 20,24 2,34"/><polygon points="46,14 28,24 46,34"/><polygon points="14,46 24,28 34,46"/></g></svg>'),
        ("angle_valve",    "Vinkelventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="24" y2="24"/><line x1="24" y1="24" x2="24" y2="46"/><polygon points="2,13 24,24 2,35"/><polygon points="13,46 24,24 35,46"/></g></svg>'),
        ("needle_valve",   "Nålventil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><polygon points="24,10 44,17 44,31 24,38 4,31 4,17"/></g></svg>'),
    ],
    "Kärl": [
        ("horiz_vessel",   "Horisontell behållare",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><rect x="4" y="14" width="40" height="20" rx="10"/></g></svg>'),
        ("vert_vessel",    "Vertikal behållare",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><rect x="14" y="4" width="20" height="40" rx="10"/></g></svg>'),
        ("column",         "Kolonn",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><rect x="13" y="2" width="22" height="44" rx="3"/><line x1="13" y1="14" x2="35" y2="22"/><line x1="13" y1="22" x2="35" y2="14"/><line x1="13" y1="28" x2="35" y2="36"/><line x1="13" y1="36" x2="35" y2="28"/></g></svg>'),
        ("hopper",         "Tratt/Binge",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="5" y1="5" x2="43" y2="5"/><line x1="5" y1="5" x2="19" y2="35"/><line x1="43" y1="5" x2="29" y2="35"/><line x1="19" y1="35" x2="29" y2="35"/><line x1="24" y1="35" x2="24" y2="46"/></g></svg>'),
        ("separator",      "Separator",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><ellipse cx="24" cy="16" rx="18" ry="13"/><line x1="6" y1="16" x2="6" y2="30"/><line x1="42" y1="16" x2="42" y2="30"/><line x1="6" y1="30" x2="24" y2="44"/><line x1="42" y1="30" x2="24" y2="44"/></g></svg>'),
    ],
    "Utrustning": [
        ("pump",           "Pump (centrifugal)",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><circle cx="24" cy="24" r="18"/><polygon points="12,16 38,24 12,32"/></g></svg>'),
        ("heat_exchanger", "Värmeväxlare",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><rect x="3" y="12" width="42" height="24" rx="2"/><line x1="3" y1="21" x2="45" y2="21"/><line x1="3" y1="27" x2="45" y2="27"/><line x1="3" y1="21" x2="3" y2="15"/><line x1="45" y1="21" x2="45" y2="33"/><line x1="3" y1="27" x2="3" y2="33"/><line x1="45" y1="27" x2="45" y2="15"/></g></svg>'),
        ("instrument",     "Instrument (ISA)",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><circle cx="24" cy="24" r="18"/><line x1="8" y1="27" x2="40" y2="27"/><text x="24" y="22" text-anchor="middle" font-size="8" stroke="none" fill="red" font-family="sans-serif">XX</text><text x="24" y="38" text-anchor="middle" font-size="7" stroke="none" fill="red" font-family="sans-serif">XXXX</text></g></svg>'),
        ("mixer",          "Reaktor/Omrörare",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><rect x="12" y="4" width="24" height="36" rx="4"/><line x1="24" y1="4" x2="24" y2="40"/><line x1="14" y1="18" x2="34" y2="18"/><line x1="15" y1="27" x2="33" y2="27"/><line x1="24" y1="40" x2="24" y2="46"/><line x1="20" y1="40" x2="28" y2="40"/></g></svg>'),
        ("filter",         "Filter/Sil",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="46" y2="24"/><rect x="11" y="13" width="26" height="22" rx="2"/><line x1="17" y1="13" x2="17" y2="35"/><line x1="23" y1="13" x2="23" y2="35"/><line x1="29" y1="13" x2="29" y2="35"/><line x1="35" y1="13" x2="35" y2="35"/></g></svg>'),
        ("compressor",     "Kompressor",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><polygon points="2,40 24,8 46,40"/><circle cx="24" cy="30" r="8"/></g></svg>'),
        ("expansion_joint","Expansionskoppling",
         '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg"><g stroke="red" stroke-width="2.5" fill="none"><line x1="2" y1="24" x2="11" y2="24"/><line x1="37" y1="24" x2="46" y2="24"/><line x1="11" y1="13" x2="11" y2="35"/><line x1="37" y1="13" x2="37" y2="35"/><path d="M11,13 Q24,8 37,13"/><path d="M11,35 Q24,40 37,35"/></g></svg>'),
    ],
}


def _get_red_symbol_svg(symbol_id: str) -> str | None:
    """Return the SVG string for the given red markup symbol ID, or None."""
    for syms in _RED_MARKUP_SYMBOLS.values():
        for sid, _sname, svg in syms:
            if sid == symbol_id:
                return svg
    return None

Z_PAGE       = 0
Z_HIGHLIGHT  = 1   # tag highlights (cleared separately by clear_highlights)
Z_SHEET_CONN = 2   # inter-sheet connection arcs on the study board
Z_CONNECT    = 3   # HAZOP cause/consequence/safeguard lines
Z_OVERLAY    = 5
Z_TEMP       = 10


class PDFVectorItem(QGraphicsItem):
    """Renders a PDF page as pure vector — crisp at any zoom.

    Performance strategy (layered):
    1. SVG is parsed ONCE into a QPicture at init (record draw commands).
    2. Each paint call just replays the QPicture — far cheaper than re-parsing SVG.
    3. DeviceCoordinateCache: Qt caches the QPicture replay as a screen-res pixmap.
       Pan  → copy cached pixmap (near-zero cost).
       Zoom → replay QPicture at new resolution (much faster than SVG parse).
    """

    def __init__(self, svg_bytes: bytes):
        super().__init__()
        renderer = QSvgRenderer(svg_bytes)
        vb = renderer.viewBoxF()
        self._rect = vb if (vb.isValid() and vb.width() > 0) \
                     else QRectF(0, 0,
                                 renderer.defaultSize().width(),
                                 renderer.defaultSize().height())

        # Pre-record SVG draw commands into QPicture (parse once, replay many)
        self._picture = QPicture()
        p = QPainter(self._picture)
        renderer.render(p, self._rect)
        p.end()
        # Keep renderer only for fallback; picture is what we actually paint
        del renderer

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption)
        # Qt caches QPicture replay at screen resolution:
        # panning reuses cache; zooming replays QPicture (fast)
        self.setCacheMode(QGraphicsItem.CacheMode.DeviceCoordinateCache)

    def boundingRect(self):
        return self._rect

    def paint(self, painter, option, widget=None):
        if option and option.exposedRect.isValid():
            painter.setClipRect(option.exposedRect)
        painter.drawPicture(QPointF(0, 0), self._picture)

    def page_width(self):
        return self._rect.width()

    def page_height(self):
        return self._rect.height()


def _point_segment_distance(px, py, x0, y0, x1, y1):
    """Shortest distance from (px,py) to the segment (x0,y0)-(x1,y1) —
    used by _ClusterPreviewCanvas's click hit-test."""
    dx, dy = x1 - x0, y1 - y0
    length_sq = dx * dx + dy * dy
    if length_sq < 1e-9:
        return math.hypot(px - x0, py - y0)
    t = max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / length_sq))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


class _ClusterPreviewCanvas(QWidget):
    """Small custom-painted preview of a resolved reference cluster's
    primitives, for SimilarSymbolSearchDialog (2026-08-14, see NOTES.md
    "Hitta liknande symbol" — sökparametrar). Click a segment to
    exclude/include it from the reference shape — directly solves
    "ta bort något som inte tillhör" (Anton's example: a valve whose
    auto-detected cluster pulled in an attached pipe stub). The
    surviving (non-excluded) primitive indices become
    find_similar_shapes()'s ref_index_group.

    `initial_excluded` (2026-08-15, see NOTES.md "'Hitta liknande
    symbol' visar bara ett streck") — `index_group` can be WIDER than
    just what auto-detection found (e.g. every primitive within some
    radius of the click point, via symbol_geometry.primitives_near_point),
    with everything outside the auto-detected core starting excluded.
    This lets the user click to ADD a nearby primitive the clustering
    missed, not just exclude one it wrongly included — needed on very
    densely-fragmented CAD exports where a real symbol's own strokes can
    end up split across many small, ungrouped pieces that auto-detection
    alone has nothing complete to offer for.

    Click/exclusion/render granularity is per drawn PATH (`source`), not
    per individual primitive (2026-08-15, see NOTES.md "Referens-
    canvasen: rendera fyllnad som svart + gruppera klick per ritad väg").
    A single filled shape (a valve's triangle) can be tessellated into
    dozens of tiny line/curve fragments that all share one `source` —
    confirmed on a real file: one small valve's own body+stem spanned
    ~103 primitives across ~12 sources. Rendering and toggling each
    fragment independently made the reference look like a tangle of
    thin strokes instead of the solid filled shape a real PDF viewer
    draws, and made "exclude the one wrong piece" impractical to click
    precisely. Grouping by `source` (already the unit `extract_primitives`
    itself groups a fill flag by — every primitive from one drawn path
    shares the same `filled` value) fixes both: a whole shape toggles as
    one unit, and a filled group renders as one solid region."""

    selection_changed = pyqtSignal()

    _MARGIN = 12
    _HIT_TOL = 7.0   # px

    def __init__(self, primitives, index_group, parent=None, initial_excluded=None):
        super().__init__(parent)
        self._primitives = primitives
        self._index_group = list(index_group)
        self._groups = {}   # source -> [primitive indices], in index_group order
        for i in self._index_group:
            self._groups.setdefault(primitives[i]['source'], []).append(i)
        if initial_excluded:
            self._excluded_sources = {primitives[i]['source'] for i in initial_excluded}
        else:
            self._excluded_sources = set()
        self.setMinimumSize(240, 180)

    def edited_index_group(self):
        """The surviving (non-excluded) primitive indices — this
        widget's whole reason for existing. Still primitive-index-
        granular (find_similar_shapes()'s own contract), even though
        exclusion itself now operates per source group."""
        return [i for i in self._index_group
                if self._primitives[i]['source'] not in self._excluded_sources]

    def has_edits(self):
        return bool(self._excluded_sources)

    def edited_outline(self):
        """The bbox-corner polygon of just the surviving (non-excluded)
        primitives — same [[x,y], ...] shape find_symbol_clusters()'s
        own 'outline' key already uses (2026-08-15, see NOTES.md
        "städa bort en ledning från en ventil/pump" — MarkerShapeEditDialog).
        [] if everything has been excluded."""
        xs, ys = [], []
        for i in self.edited_index_group():
            x0, y0, x1, y1 = self._primitives[i]['bbox']
            xs += [x0, x1]
            ys += [y0, y1]
        if not xs:
            return []
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

    def _bbox(self):
        xs, ys = [], []
        for i in self._index_group:
            x0, y0, x1, y1 = self._primitives[i]['bbox']
            xs += [x0, x1]
            ys += [y0, y1]
        if not xs:
            return (0.0, 0.0, 1.0, 1.0)
        return (min(xs), min(ys), max(xs), max(ys))

    def _transform(self):
        """(scale, offset_x, offset_y) mapping this cluster's PDF-space
        points into the widget's pixel rect, preserving aspect ratio
        and centering the result."""
        x0, y0, x1, y1 = self._bbox()
        w, h = max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)
        avail_w = max(self.width() - 2 * self._MARGIN, 1)
        avail_h = max(self.height() - 2 * self._MARGIN, 1)
        scale = min(avail_w / w, avail_h / h)
        off_x = self._MARGIN + (avail_w - w * scale) / 2 - x0 * scale
        off_y = self._MARGIN + (avail_h - h * scale) / 2 - y0 * scale
        return scale, off_x, off_y

    def _to_widget(self, x, y):
        scale, ox, oy = self._transform()
        return x * scale + ox, y * scale + oy

    def _primitive_polyline(self, i):
        """Points to draw/hit-test as a connected polyline — p0/p1 for
        lines/curves, the as-drawn corners (closing back to the first)
        for rects/quads, same convention _prim_corner_edges uses."""
        prim = self._primitives[i]
        if prim['kind'] in ('l', 'c'):
            return [prim['p0'], prim['p1']]
        corners = prim.get('corners')
        if not corners:
            x0, y0, x1, y1 = prim['bbox']
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return corners + [corners[0]]

    def paintEvent(self, event):
        """2026-08-15 (see NOTES.md "Referens-canvasen"): filled groups
        used to render as a convex-hull fill, but Anton found the result
        visually unconvincing in practice ("Det blev inte jättelyckat
        med att fylla dem") — reverted to plain stroked outlines for
        every group. The per-source CLICK grouping (a whole tessellated
        shape toggles as one unit) stays; only the fill rendering is
        gone."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor('#ffffff'))
        for source, indices in self._groups.items():
            if not indices:
                continue
            excluded = source in self._excluded_sources
            color = QColor('#B3B7B2') if excluded else QColor('#17191C')
            pen = QPen(color, 1.6 if excluded else 2.0)
            pen.setStyle(Qt.PenStyle.DashLine if excluded else Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            for i in indices:
                pts = [self._to_widget(x, y) for x, y in self._primitive_polyline(i)]
                for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                    painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        best_i, best_d = None, self._HIT_TOL
        for i in self._index_group:
            pts = [self._to_widget(x, y) for x, y in self._primitive_polyline(i)]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                d = _point_segment_distance(pos.x(), pos.y(), x0, y0, x1, y1)
                if d < best_d:
                    best_d, best_i = d, i
        if best_i is not None:
            source = self._primitives[best_i]['source']
            if source in self._excluded_sources:
                self._excluded_sources.discard(source)
            else:
                self._excluded_sources.add(source)
            self.update()
            self.selection_changed.emit()


class MarkerShapeEditDialog(QDialog):
    """"finns det något bra sätt att städa bort ledningen från en
    ventil eller pump" (2026-08-15, see NOTES.md) — reuses
    _ClusterPreviewCanvas (built for "Hitta liknande symbol"'s
    reference editing) so ANY detected equipment marker, not just a
    similarity-search reference, can have a wrongly-merged pipe/stem
    clicked away before it's saved. Opened from
    EquipmentMarkerReviewDialog's per-row "✏ Form" button."""

    def __init__(self, primitives, index_group, parent=None, initial_excluded=None):
        super().__init__(parent)
        self.setWindowTitle("Redigera form")
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Klicka ett segment för att utesluta eller lägga till det "
            "(t.ex. en ledning som råkade följa med, eller en bit som "
            "inte hittades automatiskt):"))
        self._canvas = _ClusterPreviewCanvas(
            primitives, index_group, initial_excluded=initial_excluded)
        layout.addWidget(self._canvas)

        btns = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        layout.addLayout(btns)

    def edited_outline(self):
        return self._canvas.edited_outline()


class _ImageRefCropCanvas(QWidget):
    """Rubber-band crop tool for the image-matching reference preview
    (2026-08-15, see NOTES.md "Bildbaserad 'hitta liknande symbol'" —
    real-file verification follow-up). Unlike _ClusterPreviewCanvas
    (which edits a set of vector primitives to exclude/include), there
    is nothing to "exclude" in a raster template — it's either the whole
    rendered reference region or a smaller rectangle the user drags out
    on top of it. Needed because bbox precision measurably affects match
    quality (a reference that happened to include an adjacent tag label
    scored far worse than a tightly-cropped symbol-only region — see
    NOTES.md), and image mode previously had no way to fix that the way
    vector mode's segment editor can.

    Drag a rectangle to crop; click without dragging (or drag a
    smaller-than-a-few-pixels rectangle) leaves the current crop alone,
    same "not a real gesture" tolerance _ClusterPreviewCanvas uses for
    stray clicks."""

    crop_changed = pyqtSignal()

    _MARGIN = 8
    _MIN_DRAG_PX = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(240, 140)
        self._gray = None          # numpy array, the full rendered reference
        self._full_bbox = None     # PDF-space bbox the array was rendered from
        self._drag_start = None
        self._drag_current = None
        self._press_widget_pos = None   # WIDGET pixels — for the min-drag-distance check
        self._crop_px = None       # (x0,y0,x1,y1) in ARRAY pixel space, or None = full

    def set_reference(self, gray, full_bbox):
        """Load a newly-rendered reference crop — resets any previous
        manual crop, since it belonged to the old rendering."""
        self._gray = gray
        self._full_bbox = full_bbox
        self._crop_px = None
        self._drag_start = self._drag_current = None
        self.update()

    def has_crop(self):
        return self._crop_px is not None

    def reset_crop(self):
        if self._crop_px is not None:
            self._crop_px = None
            self.update()
            self.crop_changed.emit()

    def current_bbox(self):
        """The PDF-space bbox to actually search with — the full
        rendered region if the user hasn't dragged a crop, else the
        sub-rectangle they selected, mapped back through the known
        render scale."""
        if self._full_bbox is None:
            return None
        if self._crop_px is None:
            return self._full_bbox
        h, w = self._gray.shape
        x0, y0, x1, y1 = self._full_bbox
        px0, py0, px1, py1 = self._crop_px
        sx, sy = (x1 - x0) / w, (y1 - y0) / h
        return (x0 + px0 * sx, y0 + py0 * sy, x0 + px1 * sx, y0 + py1 * sy)

    def _transform(self):
        """(scale, offset_x, offset_y) mapping the rendered array's own
        pixel space into this widget's pixel rect — same centered,
        aspect-preserving convention as _ClusterPreviewCanvas._transform,
        just over image pixels instead of PDF points."""
        if self._gray is None:
            return 1.0, 0.0, 0.0
        h, w = self._gray.shape
        avail_w = max(self.width() - 2 * self._MARGIN, 1)
        avail_h = max(self.height() - 2 * self._MARGIN, 1)
        scale = min(avail_w / w, avail_h / h)
        off_x = self._MARGIN + (avail_w - w * scale) / 2
        off_y = self._MARGIN + (avail_h - h * scale) / 2
        return scale, off_x, off_y

    def _widget_to_px(self, wx, wy):
        scale, ox, oy = self._transform()
        h, w = self._gray.shape
        return (max(0.0, min(w, (wx - ox) / scale)),
                max(0.0, min(h, (wy - oy) / scale)))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#1c1e21'))
        if self._gray is None:
            painter.end()
            return
        h, w = self._gray.shape
        qimg = QImage(self._gray.tobytes(), w, h, w, QImage.Format.Format_Grayscale8)
        scale, ox, oy = self._transform()
        painter.drawImage(QRectF(ox, oy, w * scale, h * scale), qimg)

        rect_px = None
        if self._drag_start and self._drag_current:
            (x0, y0), (x1, y1) = self._drag_start, self._drag_current
            rect_px = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        elif self._crop_px is not None:
            rect_px = self._crop_px
        if rect_px:
            x0, y0, x1, y1 = rect_px
            painter.setPen(QPen(QColor('#4da3ff'), 2))
            painter.drawRect(QRectF(x0 * scale + ox, y0 * scale + oy,
                                     (x1 - x0) * scale, (y1 - y0) * scale))
        painter.end()

    def mousePressEvent(self, event):
        if self._gray is None:
            return
        self._press_widget_pos = (event.position().x(), event.position().y())
        px = self._widget_to_px(event.position().x(), event.position().y())
        self._drag_start = px
        self._drag_current = px
        self.update()

    def mouseMoveEvent(self, event):
        if self._drag_start is None:
            return
        self._drag_current = self._widget_to_px(event.position().x(), event.position().y())
        self.update()

    def mouseReleaseEvent(self, event):
        if self._drag_start is None:
            return
        (x0, y0), (x1, y1) = self._drag_start, self._drag_current
        self._drag_start = self._drag_current = None
        rx0, ry0 = min(x0, x1), min(y0, y1)
        rx1, ry1 = max(x0, x1), max(y0, y1)
        # "Real gesture" tolerance measured in WIDGET pixels (2026-08-16,
        # see NOTES.md "rutan i bildmatchning stämmer inte med det
        # markerade"), NOT array pixels like before this fix — a real
        # reference can be tiny (confirmed on the active project's own
        # hazop_project_pid.pdf: a resolved reference array was only
        # 12x24px) and still gets rendered zoomed way up to fill the
        # widget (10x+ in that case). At that zoom, a completely normal,
        # deliberate on-screen drag of dozens of widget pixels can
        # convert to under 4 ARRAY pixels in one dimension — the
        # `_MIN_DRAG_PX` check silently discarded the whole gesture as
        # "just a stray click", so the crop box shown never matched what
        # was actually dragged, especially for thin/small references.
        press_x, press_y = self._press_widget_pos
        release_x, release_y = event.position().x(), event.position().y()
        if (abs(release_x - press_x) < self._MIN_DRAG_PX
                or abs(release_y - press_y) < self._MIN_DRAG_PX):
            self.update()   # a stray click, not a real crop gesture
            return
        self._crop_px = (rx0, ry0, rx1, ry1)
        self.update()
        self.crop_changed.emit()


# Rendering DPI for the reference PREVIEW shown in _ImageRefCropCanvas —
# deliberately separate from image_symbol_matching._DEFAULT_DPI (300),
# which is the empirically-grounded sweet spot for actual MATCHING
# accuracy (see NOTES.md "Högre DPI för bildmatchning — testat, ingen
# förbättring": higher DPI measurably makes matching WORSE, not better).
# The preview is a different concern entirely — a human looking at a
# tiny reference (confirmed on the active project's own
# hazop_project_pid.pdf: a real resolved reference rendered to only
# 12x24 pixels at 300 DPI) sees a blocky, illegible thumbnail once
# _ImageRefCropCanvas zooms it up to fill the widget, even though the
# MATCHING itself works fine on that same low-resolution data. Anton:
# "Det ser väldigt B ut när det visas en så lågupplöst version av
# ventilen." (2026-08-16, see NOTES.md). Rendering ONLY the preview
# crop (never a whole page — cheap regardless of DPI) at 4x the
# matching DPI gives roughly 4x the linear pixel detail to display,
# with no effect whatsoever on what find_similar_shapes_visual actually
# searches with.
_PREVIEW_DPI = image_symbol_matching._DEFAULT_DPI * 4


class SimilarSymbolSearchDialog(QDialog):
    """Sökparametrar för "Hitta liknande symbol" (2026-08-14/15, see
    NOTES.md) — opens right after a reference cluster is resolved
    (PIDPanel._find_similar_symbol). Lets the user prune the reference
    shape's own primitives (_ClusterPreviewCanvas) and choose:
    - Likhetströskel (similarity threshold)
    - Skala: endast liknande storlek, eller alla storlekar
      (ignore_scale)
    - Omfattning: bara denna sida, eller hela dokumentet (pages)
    An informational note explains that 90°-rotated symbols are
    already found automatically (cluster_features() is exactly
    invariant under 90°-multiple rotation — see
    symbol_geometry.oriented_features()'s docstring); "Alla vinklar"
    is a real, separate, slower opt-in mode
    (symbol_geometry.oriented_features()), not a placebo toggle.

    No "bara samma typ" FILTER: a shape-similarity candidate has no
    type of its own yet (equipment TYPE is inferred from a tag prefix
    elsewhere in this codebase, never from shape — see
    equipment_detection.py's module docstring) — there is nothing
    meaningful to filter candidates on before they're reviewed/tagged,
    so that control was deliberately left out rather than shipped as
    a no-op.

    There IS a Typ LABEL, though (2026-08-16, see NOTES.md "kunna välja
    vilken typ av objekt det är i både raster och vektor") — an editable
    combo box (selected_comp_type(), same COMPONENT_TYPES list
    EquipmentMarkerReviewDialog's own mass-type combo already uses) the
    user sets BEFORE running the search, applied to every result via
    final_results(comp_type=...) — in both Vektorform and Bildmatchning,
    since it's attached to the search's INTENT, not derived from shape.
    Pre-filled from the template's own comp_type in mall-läge (still
    editable), matching what final_results() already did silently there
    before this control existed.

    2026-08-15 uppföljningsfunktioner (see NOTES.md): the document scan
    (SimilarSymbolSearchWorker/equipment_detection._scan_candidates) now
    runs in a background thread as soon as the dialog opens (and again
    whenever a setting that affects the candidate SCORES changes:
    reference edits, skala, rotation, omfattning) — with inline
    progress + a real cancel (via the same "Avbryt" button, which also
    closes the dialog). The un-thresholded result is cached in
    self._candidates: the similarity-threshold slider alone never
    re-scans, it just re-filters/re-counts that cached list locally,
    which is also what powers the live "≈ N träffar" label and the
    optional on-canvas "Visa på P&ID" preview. "Sök" reuses the same
    cached list (via equipment_detection.shape_similar_results) instead
    of running a second, duplicate scan.

    Symbolbibliotek (2026-08-15, see NOTES.md): "💾 Spara som mall…"
    persists the (possibly edited) reference's similarity_features() as
    a named Database.symbol_templates() row. Passing template_features
    (+ template_name) instead of primitives/index_group switches the
    dialog into "mall-läge": no _ClusterPreviewCanvas (there are no live
    primitives to show for a saved template), and Rotation's "alla
    vinklar" toggle is disabled — a template's features were already
    computed in one fixed rotation basis when it was saved, so there's
    nothing left here to recompute against a different one.

    Bildmatchning (2026-08-15, see NOTES.md "Bildbaserad 'hitta liknande
    symbol' — vid sidan av vektorlogiken"): a second, independent
    matching method living alongside the vector one above, for exactly
    the case the vector docstrings already flag — heavily-tessellated
    CAD exports where a real symbol's own strokes end up split across
    many disconnected drawing paths, leaving vector clustering nothing
    complete to compare. `ref_bbox` (PDF-space bbox of the reference
    region) is required for this — used to crop+render a reference
    bitmap, matched via image_symbol_matching.find_similar_shapes_visual
    (ImageSymbolSearchWorker) instead of the vector
    SimilarSymbolSearchWorker. A "Vektorform"/"Bildmatchning" toggle
    appears whenever a real choice exists (a vector cluster WAS
    resolved, i.e. `primitives` is not None, and this isn't mall-läge —
    a saved template has no image counterpart in this first version).
    When no vector cluster was resolved at all (`primitives is None` —
    a scanned page, or a click with no nearby vector data), the dialog
    silently runs in image-only mode instead of refusing outright, since
    that is precisely the gap the vector path's own docstring already
    called out as "a separate, not-yet-built undertaking". Both
    matching methods emit the exact same (sim, page_num, x, y, outline)
    candidate contract, so every downstream piece — threshold slider,
    live count, "Visa på P&ID" preview, "Sök" — needs no branching of
    its own to work with either. "💾 Spara som mall…" stays disabled in
    Bildmatchning-läge for now — image-based templates would need their
    own storage format, a reasonable future extension not built here."""

    def __init__(self, primitives, index_group, pdf_path, ref_page, ref_scale,
                 ref_bbox=None, db=None, viewer=None, template_name=None,
                 template_features=None, initial_excluded=None,
                 native_index_group=None, page_rotations=None,
                 initial_comp_type='', parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hitta liknande symbol")
        self.setMinimumWidth(360)
        self._db        = db
        self._pdf_path  = pdf_path
        self._ref_page  = ref_page
        self._ref_scale = ref_scale
        self._ref_bbox  = ref_bbox
        # {physical_page: extra_degrees} — re-applied inside the worker's
        # own freshly-opened document, since it never sees PIDGraphicsView's
        # live manual-rotation-override state otherwise (2026-08-15, see
        # NOTES.md "Hitta liknande symbol placerar fel — sidrotation").
        self._page_rotations = page_rotations
        self._viewer    = viewer
        self._template_features = template_features
        # No vector cluster was resolved at all (a scanned page, or a
        # click far from any vector geometry) — image matching is the
        # ONLY option, not a toggled choice; see class docstring.
        self._forced_image_only = template_features is None and primitives is None
        # The auto-detected group ALONE (never the wider, nearby-primitives
        # display set _ClusterPreviewCanvas may also show — see
        # initial_excluded above) — this is what identifies the reference
        # among its own page's candidates during a scan (see _restart_scan),
        # so widening what's shown for editing must never widen what's
        # excluded from the results as "the reference itself" too.
        self._native_index_group = (native_index_group if native_index_group is not None
                                    else index_group)
        self._worker     = None
        self._candidates = []   # raw (sim, page_num, x, y, outline) tuples

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self._method_image = None   # set below only when a real choice exists
        if template_features is not None:
            layout.addWidget(QLabel(
                f"<b>Sökning baserad på sparad mall:</b> {template_name}"))
            self._canvas = None
        elif self._forced_image_only:
            layout.addWidget(QLabel(
                "<b>Ingen vektorgeometri hittades</b> här — söker med "
                "bildmatchning istället (fungerar även på skannade sidor):"))
            self._canvas = None
        else:
            layout.addWidget(QLabel(
                "<b>Referensform</b> — klicka ett segment för att utesluta "
                "eller lägga till det (t.ex. en ledning som råkade följa med "
                "en ventil, eller en bit av ventilen som inte hittades "
                "automatiskt):"))
            self._canvas = _ClusterPreviewCanvas(
                primitives, index_group, initial_excluded=initial_excluded)
            self._canvas.selection_changed.connect(self._restart_scan)
            layout.addWidget(self._canvas)

            method_row = QHBoxLayout()
            method_row.addWidget(QLabel("Matchningsmetod:"))
            self._method_group_btns = QButtonGroup(self)
            self._method_vector = QRadioButton("Vektorform")
            self._method_image = QRadioButton("Bildmatchning")
            self._method_vector.setChecked(True)
            self._method_group_btns.addButton(self._method_vector)
            self._method_group_btns.addButton(self._method_image)
            self._method_vector.toggled.connect(self._on_method_changed)
            method_row.addWidget(self._method_vector)
            method_row.addWidget(self._method_image)
            method_row.addStretch()
            layout.addLayout(method_row)

        self._image_ref_container = QWidget()
        image_ref_row = QVBoxLayout(self._image_ref_container)
        image_ref_row.setContentsMargins(0, 0, 0, 0)
        self._image_ref_canvas = _ImageRefCropCanvas()
        self._image_ref_canvas.crop_changed.connect(self._restart_scan)
        image_ref_row.addWidget(self._image_ref_canvas)
        # "Bildmatchningen visar ingen symbol" (2026-08-16, see NOTES.md
        # "Bildmatchning visar ingen symbol och kraschar lätt"):
        # resolve_reference_cluster() is deliberately permissive (lets a
        # user reference literally any vector shape — see its own
        # docstring), so a click can easily resolve to a degenerate
        # sliver of a cluster (confirmed on the active project's own
        # hazop_project_pid.pdf: a real click resolved to a 1.3x1.3pt
        # cluster) — the rendered reference crop is then only a handful
        # of pixels across and visually reads as "nothing shown", not a
        # crash, just an unhelpfully tiny/blank preview. This warning
        # makes that visible instead of leaving the user to guess why
        # the preview looks empty.
        self._tiny_ref_warning = QLabel(
            "⚠ Referensen verkar mycket liten — kontrollera att du klickade på rätt symbol.")
        self._tiny_ref_warning.setStyleSheet("color:#d9822b; font-size:10px;")
        self._tiny_ref_warning.setWordWrap(True)
        self._tiny_ref_warning.setVisible(False)
        image_ref_row.addWidget(self._tiny_ref_warning)
        crop_btns_row = QHBoxLayout()
        crop_note = QLabel("Dra en ram i förhandsgranskningen för att beskära referensen.")
        crop_note.setStyleSheet("color:#8D9299; font-size:10px;")
        crop_note.setWordWrap(True)
        crop_btns_row.addWidget(crop_note)
        crop_btns_row.addStretch()
        self._reset_crop_btn = QPushButton("Återställ beskärning")
        self._reset_crop_btn.clicked.connect(self._image_ref_canvas.reset_crop)
        crop_btns_row.addWidget(self._reset_crop_btn)
        image_ref_row.addLayout(crop_btns_row)
        layout.addWidget(self._image_ref_container)
        self._update_method_visibility()

        form = QFormLayout()
        form.setSpacing(6)

        self._threshold = QSlider(Qt.Orientation.Horizontal)
        self._threshold.setRange(0, 100)
        self._threshold.setValue(60)
        self._threshold_lbl = QLabel("60%")
        self._threshold.valueChanged.connect(self._on_threshold_changed)
        thr_row = QHBoxLayout()
        thr_row.addWidget(self._threshold)
        thr_row.addWidget(self._threshold_lbl)
        form.addRow("Likhetströskel:", thr_row)

        self._scale_group = QButtonGroup(self)
        self._scale_same = QRadioButton("Endast liknande storlek")
        self._scale_any = QRadioButton("Alla storlekar")
        self._scale_same.setChecked(True)
        self._scale_group.addButton(self._scale_same)
        self._scale_group.addButton(self._scale_any)
        self._scale_same.toggled.connect(self._restart_scan)
        scale_row = QVBoxLayout()
        scale_row.addWidget(self._scale_same)
        scale_row.addWidget(self._scale_any)
        form.addRow("Skala:", scale_row)

        rot_note = QLabel(
            "🔄 Symboler roterade i 90° steg hittas redan automatiskt.")
        rot_note.setStyleSheet("color:#8D9299; font-size:10px;")
        rot_note.setWordWrap(True)
        self._rotation_any = QCheckBox("Sök i alla vinklar (experimentell, långsammare)")
        if template_features is not None:
            self._rotation_any.setEnabled(False)
            self._rotation_any.setToolTip(
                "Inte tillgängligt för sparade mallar — mallen sparades redan "
                "i ett fast rotationsläge.")
        else:
            self._rotation_any.toggled.connect(self._restart_scan)
        rot_col = QVBoxLayout()
        rot_col.addWidget(rot_note)
        rot_col.addWidget(self._rotation_any)
        form.addRow("Rotation:", rot_col)

        self._scope_group = QButtonGroup(self)
        self._scope_page = QRadioButton("Denna sida")
        self._scope_doc = QRadioButton("Hela dokumentet")
        self._scope_doc.setChecked(True)
        self._scope_group.addButton(self._scope_page)
        self._scope_group.addButton(self._scope_doc)
        self._scope_page.toggled.connect(self._restart_scan)
        scope_row = QVBoxLayout()
        scope_row.addWidget(self._scope_page)
        scope_row.addWidget(self._scope_doc)
        form.addRow("Omfattning:", scope_row)

        # Typ (2026-08-16, see NOTES.md "kunna välja vilken typ av objekt
        # det är i både raster och vektor") — a shape-similarity candidate
        # has no type of its own (see this class's own docstring above:
        # equipment TYPE is inferred from a tag prefix elsewhere, never
        # from shape), so this is NOT a candidate filter — it's a label
        # the user attaches up front, applied to every result exactly the
        # way a saved template's own comp_type already was (see
        # final_results below) — just now also available for an ad-hoc
        # (non-template) search, in both Vektorform and Bildmatchning,
        # instead of only reachable afterwards via
        # EquipmentMarkerReviewDialog's own "Tillämpa på ikryssade"
        # mass-type step. Same editable-combobox convention that dialog
        # already uses for exactly this list.
        self._type_cb = QComboBox()
        self._type_cb.setEditable(True)
        self._type_cb.addItems(sorted(equipment_detection.COMPONENT_TYPES.keys()))
        self._type_cb.setCurrentText(initial_comp_type or '')
        form.addRow("Typ:", self._type_cb)

        layout.addLayout(form)

        status_row = QHBoxLayout()
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet("color:#8D9299; font-size:10px;")
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet("font-weight:600;")
        status_row.addWidget(self._status_lbl)
        status_row.addStretch()
        status_row.addWidget(self._count_lbl)
        layout.addLayout(status_row)

        self._progress_bar = QProgressBar()
        self._progress_bar.setVisible(False)
        self._progress_bar.setFixedHeight(6)
        self._progress_bar.setTextVisible(False)
        layout.addWidget(self._progress_bar)

        preview_row = QHBoxLayout()
        self._preview_cb = QCheckBox("Visa på P&ID")
        self._preview_cb.setEnabled(viewer is not None)
        if viewer is None:
            self._preview_cb.setToolTip("Ingen P&ID-vy kopplad till den här sökningen.")
        self._preview_cb.toggled.connect(self._update_preview)
        preview_row.addWidget(self._preview_cb)
        preview_row.addStretch()
        self._save_template_btn = QPushButton("💾 Spara som mall…")
        self._save_template_btn.clicked.connect(self._save_as_template)
        preview_row.addWidget(self._save_template_btn)
        layout.addLayout(preview_row)
        self._update_save_template_button()

        btns = QHBoxLayout()
        self._search_btn = QPushButton("Sök")
        self._search_btn.setDefault(True)
        self._search_btn.setEnabled(False)
        self._search_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(cancel_btn)
        btns.addWidget(self._search_btn)
        layout.addLayout(btns)

        self.finished.connect(self._on_dialog_finished)
        self._restart_scan()

    def edited_index_group(self):
        return self._canvas.edited_index_group() if self._canvas else None

    def selected_comp_type(self):
        """The user-chosen equipment type (2026-08-16, see NOTES.md
        "kunna välja vilken typ av objekt det är i både raster och
        vektor") — applied to every result via final_results(comp_type=...)
        regardless of matching method, same as a saved template's own
        comp_type already was."""
        return self._type_cb.currentText().strip()

    def use_image_matching(self):
        """True if this search should run via image_symbol_matching
        (ImageSymbolSearchWorker) rather than the vector path — either
        because the user picked "Bildmatchning", or because no vector
        cluster was resolved at all and image matching is the only
        option (see class docstring, _forced_image_only)."""
        if self._template_features is not None:
            return False
        if self._forced_image_only:
            return True
        return self._method_image is not None and self._method_image.isChecked()

    def _on_method_changed(self, *_args):
        self._update_method_visibility()
        self._update_save_template_button()
        self._restart_scan()

    def _update_save_template_button(self):
        if self._template_features is not None:
            self._save_template_btn.setEnabled(False)
            self._save_template_btn.setToolTip(
                "Redan en sparad mall — inget nytt att spara.")
        elif self.use_image_matching():
            self._save_template_btn.setEnabled(False)
            self._save_template_btn.setToolTip(
                "Inte tillgängligt för bildmatchning ännu — bara vektorformer "
                "kan sparas som mall i den här versionen.")
        elif self._db is None:
            self._save_template_btn.setEnabled(False)
            self._save_template_btn.setToolTip("Ingen databas kopplad till den här sökningen.")
        else:
            self._save_template_btn.setEnabled(True)
            self._save_template_btn.setToolTip("")

    def _update_method_visibility(self):
        use_image = self.use_image_matching()
        if self._canvas is not None:
            self._canvas.setVisible(not use_image)
        self._image_ref_container.setVisible(use_image)
        if use_image and not self._image_ref_canvas.has_crop():
            self._render_image_preview()

    def _render_image_preview(self):
        """Load the reference-crop preview into _image_ref_canvas —
        direct visual confirmation of what will actually be matched,
        instead of a vector segment editor that has nothing meaningful
        to show for a raster template. Also what the user's rubber-band
        crop (2026-08-15, see NOTES.md) draws on top of."""
        if self._ref_bbox is None:
            return
        doc = None
        try:
            doc = fitz.open(self._pdf_path)
            # self._ref_bbox is in the LIVE view's (possibly manually
            # rotation-overridden) coordinate space — this fresh document
            # needs the same override re-applied before rendering, or the
            # preview crops the wrong region entirely (2026-08-15, see
            # NOTES.md "Hitta liknande symbol placerar fel — sidrotation").
            equipment_detection.apply_page_rotations(doc, self._page_rotations)
            gray = image_symbol_matching.render_gray(
                doc[self._ref_page], bbox=self._ref_bbox,
                dpi=_PREVIEW_DPI)
            self._image_ref_canvas.set_reference(gray, self._ref_bbox)
            self._update_tiny_ref_warning()
        except Exception:
            pass
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

    def _update_tiny_ref_warning(self):
        """Show a warning when the reference bbox is implausibly small
        relative to the page's own dominant scale — same "plausible
        symbol size" floor _scan_candidates uses for candidates
        (1.5 * scale), applied here to warn about the REFERENCE itself
        instead of silently filtering. See _render_image_preview's
        caller and NOTES.md "Bildmatchning visar ingen symbol och
        kraschar lätt"."""
        if self._ref_bbox is None or not self._ref_scale:
            self._tiny_ref_warning.setVisible(False)
            return
        x0, y0, x1, y1 = self._ref_bbox
        diag = math.hypot(x1 - x0, y1 - y0)
        self._tiny_ref_warning.setVisible(diag < 1.5 * self._ref_scale)

    def min_similarity(self):
        return self._threshold.value() / 100.0

    def ignore_scale(self):
        return self._scale_any.isChecked()

    def rotation_mode(self):
        return 'any' if self._rotation_any.isChecked() else 'none'

    def search_this_page_only(self):
        return self._scope_page.isChecked()

    def final_results(self, comp_type=''):
        """The thresholded, shaped result list, reusing the already-
        computed candidate scan — no second document scan."""
        threshold = self.min_similarity()
        filtered = [c for c in self._candidates if c[0] >= threshold]
        return equipment_detection.shape_similar_results(filtered, comp_type)

    def _restart_scan(self, *_args):
        """(Re-)run the background scan — whenever the reference itself
        or a setting that affects candidate SCORES changes (segment
        exclusion, skala, rotation, omfattning). The threshold slider
        alone does NOT call this — see _on_threshold_changed."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait()
        self._candidates = []
        self._search_btn.setEnabled(False)
        self._count_lbl.setText("")
        self._progress_bar.setVisible(False)

        # Crash found in the wild (2026-08-15, see NOTES.md): excluding
        # EVERY primitive in the vector reference canvas leaves an empty
        # index_group, which used to crash all the way down in
        # symbol_geometry.cluster_features() (min() on an empty list) the
        # instant the scan tried to restart. Nothing to search for with
        # zero reference primitives — show a status message instead of
        # starting a worker at all.
        if (not self.use_image_matching() and self._template_features is None
                and not self.edited_index_group()):
            self._status_lbl.setText(
                "Inget kvar av referensformen — inkludera minst ett segment.")
            return

        self._status_lbl.setText("Beräknar…")
        self._progress_bar.setRange(0, 0)   # indeterminate until the first page reports in
        self._progress_bar.setVisible(True)

        pages = [self._ref_page] if self.search_this_page_only() else None
        if self.use_image_matching():
            search_bbox = self._image_ref_canvas.current_bbox() or self._ref_bbox
            self._worker = ImageSymbolSearchWorker(
                self._pdf_path, self._ref_page, search_bbox, pages=pages,
                ignore_scale=self.ignore_scale(), rotation_mode=self.rotation_mode(),
                page_rotations=self._page_rotations, parent=self)
        else:
            if self._template_features is not None:
                ref_features = self._template_features
                ref_native_index_group = []
            else:
                ref_features = symbol_geometry.similarity_features(
                    self._canvas._primitives, self.edited_index_group(),
                    self._ref_scale, rotation_mode=self.rotation_mode())
                ref_native_index_group = self._native_index_group
            self._worker = SimilarSymbolSearchWorker(
                self._pdf_path, ref_features, self._ref_page,
                ref_native_index_group, pages=pages,
                ignore_scale=self.ignore_scale(), rotation_mode=self.rotation_mode(),
                page_rotations=self._page_rotations, parent=self)
        self._worker.progress.connect(self._on_scan_progress)
        self._worker.finished_scan.connect(self._on_scan_finished)
        self._worker.start()

    def _on_scan_progress(self, page_num, total, msg):
        self._progress_bar.setRange(0, max(total, 1))
        self._progress_bar.setValue(page_num + 1)
        self._status_lbl.setText(msg)

    def _on_scan_finished(self, candidates):
        self._candidates = candidates
        self._progress_bar.setVisible(False)
        self._status_lbl.setText("")
        self._search_btn.setEnabled(True)
        self._update_count_label()
        self._update_preview()

    def _update_count_label(self):
        threshold = self.min_similarity()
        n = sum(1 for c in self._candidates if c[0] >= threshold)
        self._count_lbl.setText(f"≈ {n} träffar")

    def _on_threshold_changed(self, value):
        self._threshold_lbl.setText(f"{value}%")
        self._update_count_label()
        self._update_preview()

    def _update_preview(self, *_args):
        if not self._viewer:
            return
        self._viewer.clear_shape_preview()
        if not self._preview_cb.isChecked():
            return
        threshold = self.min_similarity()
        cur_page = getattr(self._viewer, 'current_page', None)
        for sim, page_num, _x, _y, outline in self._candidates:
            if sim >= threshold and page_num == cur_page:
                self._viewer.add_shape_highlight(outline)

    def _on_dialog_finished(self, _result):
        if self._worker is not None and self._worker.isRunning():
            self._worker.requestInterruption()
            self._worker.wait()
        if self._viewer:
            self._viewer.clear_shape_preview()

    def _save_as_template(self):
        """"💾 Spara som mall…" (2026-08-15, see NOTES.md "Hitta liknande
        symbol" — uppföljningsfunktioner: symbolbibliotek) — persists
        the (possibly segment-edited) reference's similarity_features()
        as a named Database.symbol_templates() row, reusable later via
        "🔎 Hitta liknande symbol (från mall)" without picking a point
        on this specific document again."""
        if not self._db or self._canvas is None:
            return
        name, ok = QInputDialog.getText(self, "Spara som mall", "Namn:")
        name = (name or '').strip()
        if not ok or not name:
            return
        ref_features = symbol_geometry.similarity_features(
            self._canvas._primitives, self.edited_index_group(),
            self._ref_scale, rotation_mode=self.rotation_mode())
        try:
            self._db.add_symbol_template(name, json.dumps(ref_features))
        except sqlite3.IntegrityError:
            QMessageBox.warning(self, "Namnet finns redan",
                f'En mall med namnet "{name}" finns redan.')
            return
        QMessageBox.information(self, "Mall sparad", f'"{name}" sparades som mall.')


class SymbolTemplatePickerDialog(QDialog):
    """"🔎 Hitta liknande symbol (från mall)" (2026-08-15, see NOTES.md
    "Hitta liknande symbol" — uppföljningsfunktioner: symbolbibliotek) —
    pick a saved Database.symbol_templates() row to search with,
    instead of clicking a reference point on the current document."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Välj mall")
        self.setMinimumWidth(320)
        self._db = db
        self.selected_template = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Sparade symbolmallar:"))
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self._list)
        self._reload()

        btns = QHBoxLayout()
        del_btn = QPushButton("Ta bort")
        del_btn.clicked.connect(self._delete_selected)
        ok_btn = QPushButton("Sök med mall")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._accept_selected)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(del_btn)
        btns.addStretch()
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        layout.addLayout(btns)

    def _reload(self):
        self._list.clear()
        for row in self._db.symbol_templates():
            label = row['name'] + (f" ({row['comp_type']})" if row['comp_type'] else '')
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, row['id'])
            self._list.addItem(item)

    def _delete_selected(self):
        item = self._list.currentItem()
        if not item:
            return
        self._db.delete_symbol_template(item.data(Qt.ItemDataRole.UserRole))
        self._reload()

    def _accept_selected(self):
        item = self._list.currentItem()
        if not item:
            QMessageBox.information(self, "Ingen mall vald", "Välj en mall i listan.")
            return
        self.selected_template = self._db.get_symbol_template(item.data(Qt.ItemDataRole.UserRole))
        self.accept()


class EquipmentMarkerReviewDialog(QDialog):
    """Review/correct auto-detected equipment-symbol matches before saving
    them as a new 'equipment' marker layer on the P&ID.

    Fed by EquipmentPanel's "🎯 Hitta objekt på P&ID", which runs the
    unified detect_equipment_and_valves() pipeline (tag-anchored AND
    shape-anchored valve/pump/instrument hunting against one shared
    per-page cluster extraction — see that function's docstring). Rows
    with tag_status='untagged' (a shape-recognized symbol with no
    confident tag match) pre-fill the Tagg cell with their temporary_id
    ('UNASSIGNED-VALVE-...') rather than being silently dropped;
    _save() registers a real equipment_catalog entry for them on the fly
    if the (suggested or user-typed) tag is non-empty.

    Unchecking a row skips it entirely; editing the Tagg cell corrects a
    wrong tag-text match, or names a valve that had none, before saving.
    Repositioning a marker that landed in the wrong place is not supported
    yet — remove it here and place it manually via the existing P&ID
    Orsak/Konsekvens-style click-to-place flow instead.

    `rejected` (optional): near-miss candidates that failed exactly one
    valve-shape filter (see _valve_rejection_reason) — shown in an
    optional, read-only, in-memory-only section; never written to the
    database, purely a debugging aid for this review session.

    `pdf_path` (2026-08-15, see NOTES.md "städa bort en ledning från en
    ventil/pump"): if given, each row with a detected outline gets a
    "✏ Form" button that re-resolves that marker's own vector cluster
    (equipment_detection.resolve_reference_cluster, the exact same
    lookup "Hitta liknande symbol" uses) and opens MarkerShapeEditDialog
    — the same segment-exclusion canvas — so a wrongly-merged pipe/stem
    can be clicked away from ANY detected marker before it's saved, not
    just a similarity-search reference. Without pdf_path (e.g. an older
    caller that hasn't been updated) the column is simply omitted.
    """

    _C_CHK, _C_THUMB, _C_TAG, _C_PAGE, _C_TYPE, _C_CONF, _C_METHOD, _C_EDIT = range(8)
    _THUMB_SIZE = 96   # px — zoomed enough to see the symbol's own shape clearly

    _METHOD_LABELS = {
        'leader':    '📐 Ledarlinje',
        'contain':   '📍 Vidrör symbol',
        'nearest':   '≈ Närmaste symbol',
        'shape':     '🦋 Formigenkänning',
        'similar':   '🔎 Liknande symbol',
        'none':      '— Ingen symbol hittad',
        'not_found': '⚠ Tagg ej hittad på sidan',
    }

    def __init__(self, results: list, db, parent=None, rejected: list = None, pdf_path=None,
                 active_node_id=None):
        super().__init__(parent)
        self.db = db
        self._results = results   # dicts: tag,page,comp_type,x,y,confidence(_es),link_method,outline,equipment_id
        self._rejected = rejected or []
        self._pdf_path = pdf_path
        # A brand-new equipment_catalog row saved here otherwise starts
        # with node_id=NULL, same as any other creation path — but manual
        # single-object placement (place_equipment_marker) immediately
        # opens EquipmentDeviationBar with the active node, which
        # auto-assigns it there. This batch dialog never opens that bar
        # for any row, so without this, EquipmentDeviationBar._activate_
        # deviation() (pid_viewer.py) later silently discards every
        # deviation/cause the user checks for it — node_id stays NULL
        # forever unless the marker is manually dragged onto a deviation
        # in the tree (2026-08-17, see NOTES.md "identifierade objekt
        # via 'hitta liknande' dyker inte upp i scenariot": confirmed
        # this is the exact mechanism behind "jag har lagt till orsaker
        # och klickar på dem" producing nothing visible). Only given by
        # the two "Hitta liknande symbol" callers, which run from a live
        # P&ID view where a node may genuinely be active; the plain
        # document-wide "Hitta objekt på P&ID" scan has no such context.
        self._active_node_id = active_node_id
        self.setWindowTitle("Granska autodetekterad utrustning")
        self.setMinimumSize(1000, 640)

        outer = QVBoxLayout(self)

        n = len(results)
        n_found = sum(1 for r in results if r['link_method'] not in ('none', 'not_found'))
        n_untagged = sum(1 for r in results if r.get('tag_status') == 'untagged')
        extra = f" ({n_untagged} utan tagg)" if n_untagged else ""
        hdr = QLabel(
            f"Hittade en symbolmatchning för <b>{n_found} av {n}</b> rader{extra}. "
            "Kryssa ur felaktiga rader, redigera taggtext vid behov (ge ett "
            "namnlöst objekt en tagg om du vill), och spara.")
        hdr.setTextFormat(Qt.TextFormat.RichText)
        hdr.setWordWrap(True)
        hdr.setStyleSheet(
            "padding:5px; background:#F5F5F3; border:1px solid #E2E3E1; border-radius:4px;")
        outer.addWidget(hdr)

        self._tbl = QTableWidget(0, 8)
        self._tbl.setHorizontalHeaderLabels(
            ['✓', 'Bild', 'Tagg', 'Sida', 'Typ', 'Konfidens', 'Metod', ''])
        hh = self._tbl.horizontalHeader()
        hh.setSectionResizeMode(self._C_TAG, QHeaderView.ResizeMode.Stretch)
        for col, w in ((self._C_CHK, 30), (self._C_THUMB, self._THUMB_SIZE + 12),
                       (self._C_PAGE, 50), (self._C_TYPE, 170), (self._C_CONF, 80),
                       (self._C_METHOD, 90), (self._C_EDIT, 70)):
            self._tbl.setColumnWidth(col, w)
        self._tbl.verticalHeader().setVisible(False)
        self._tbl.setAlternatingRowColors(True)
        outer.addWidget(self._tbl)
        self._populate()

        # ── Mass-tagging (2026-08-15, see NOTES.md "Hitta liknande
        # symbol" — uppföljningsfunktioner: "bra om jag kan välja att
        # koppla det till typ av objekt och förhoppningsvis tagg
        # nummer") — a similarity search often returns many untagged
        # hits of the same real-world type; typing Typ+Tagg for each
        # one individually is exactly the tedium this avoids. Only
        # touches CHECKED rows, always overwrites their Typ/Tagg cells
        # (simple, predictable — no partial "only if empty" rules).
        mass_row = QHBoxLayout()
        mass_row.addWidget(QLabel("Tillämpa på ikryssade:"))
        self._mass_type_cb = QComboBox()
        self._mass_type_cb.setEditable(True)
        self._mass_type_cb.addItems(self._type_options())
        self._mass_type_cb.setCurrentText('')
        mass_row.addWidget(self._mass_type_cb)
        self._mass_tag_edit = QLineEdit()
        self._mass_tag_edit.setPlaceholderText("Starttagg, t.ex. V-101")
        mass_row.addWidget(self._mass_tag_edit)
        mass_apply_btn = QPushButton("Tillämpa")
        mass_apply_btn.clicked.connect(self._apply_mass_tag)
        mass_row.addWidget(mass_apply_btn)
        outer.addLayout(mass_row)

        # "Autodetektera tagnummer" (2026-08-17, see NOTES.md) — re-runs
        # the same nearest-native-text lookup the initial scan itself
        # uses (equipment_detection.find_tag_near_point) for every
        # checked row's own detected position, filling in the Tagg cell
        # wherever it finds something. Most useful for shape-only
        # ("untagged") rows whose Tagg cell still shows a placeholder
        # temporary id — a one-click retry instead of hunting the P&ID
        # by eye for each one. Needs pdf_path (same "older caller, no
        # PDF, feature simply absent" convention as the ✏ Form column).
        autodetect_row = QHBoxLayout()
        self._autodetect_btn = QPushButton("🔍 Autodetektera tagnummer")
        self._autodetect_btn.setToolTip(
            "Söker efter närmaste taggtext på PDF-sidan för varje ikryssad "
            "rad och fyller i Tagg-cellen om något hittas.")
        self._autodetect_btn.setEnabled(bool(self._pdf_path))
        self._autodetect_btn.clicked.connect(self._autodetect_tags)
        autodetect_row.addWidget(self._autodetect_btn)
        autodetect_row.addStretch()
        outer.addLayout(autodetect_row)

        self._rejected_toggle = QPushButton(f"▸ Visa avvisade kandidater ({len(self._rejected)})")
        self._rejected_toggle.setCheckable(True)
        self._rejected_toggle.setEnabled(bool(self._rejected))
        self._rejected_toggle.setStyleSheet("text-align:left; border:none; color:#555;")
        self._rejected_toggle.toggled.connect(self._on_rejected_toggled)
        outer.addWidget(self._rejected_toggle)

        self._rejected_tbl = QTableWidget(0, 2)
        self._rejected_tbl.setHorizontalHeaderLabels(['Sida', 'Anledning'])
        self._rejected_tbl.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch)
        self._rejected_tbl.verticalHeader().setVisible(False)
        self._rejected_tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._rejected_tbl.setMaximumHeight(140)
        self._rejected_tbl.setVisible(False)
        for res in self._rejected:
            r = self._rejected_tbl.rowCount()
            self._rejected_tbl.insertRow(r)
            self._rejected_tbl.setItem(r, 0, QTableWidgetItem(str(res.get('page', 0) + 1)))
            self._rejected_tbl.setItem(r, 1, QTableWidgetItem(res.get('reason', '')))
        outer.addWidget(self._rejected_tbl)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Spara markörer")
        save_btn.setIcon(_icon('save'))
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch()
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        outer.addLayout(btn_row)

    def _on_rejected_toggled(self, on):
        self._rejected_tbl.setVisible(on)
        arrow = '▾' if on else '▸'
        self._rejected_toggle.setText(f"{arrow} Visa avvisade kandidater ({len(self._rejected)})")

    def _type_options(self):
        """Combined Typ dropdown options — COMPONENT_TYPES plus
        Rörledning/Övrigt / Okänd, any custom equipment_type already used
        somewhere in the catalog, and every name from Standardobjekt
        (Database.standard_objects(), the admin-managed catalogue under
        Inställningar → Standardobjekt) — mirrors hazop.py's own
        _equipment_type_options exactly (2026-08-17, see NOTES.md "Typen
        skall vara en gardinlista enligt vad som finns under
        standardobjekt") so this dialog's Typ column offers the same
        breadth, not a narrower hard-coded subset."""
        base_head = [''] + sorted(COMPONENT_TYPES.keys())
        base_tail = ['Rörledning', 'Övrigt / Okänd']
        known = set(base_head) | set(base_tail)
        extra = set()
        rows = self.db.conn.execute(
            "SELECT DISTINCT equipment_type FROM equipment_catalog "
            "WHERE equipment_type IS NOT NULL AND equipment_type != ''").fetchall()
        extra |= {r[0] for r in rows} - known
        extra |= {o['name'] for o in self.db.standard_objects()} - known
        return base_head + sorted(extra) + base_tail

    def _populate(self):
        self._tbl.setRowCount(0)
        type_options = self._type_options()
        # "Dvs så man kan se grafiskt att det är korrekt och inte bara
        # en lista" (2026-08-16, see NOTES.md "zoomad bild per rad i
        # granskningsdialogen") — one fitz.Document opened ONCE for the
        # whole table (not per row: rows routinely span the same few
        # pages, and re-opening + re-rendering a full page per row would
        # be needlessly slow), same page-rotation-override handling
        # _edit_shape's own re-open already needs. Gracefully absent
        # (every row's thumbnail cell just stays empty) when no
        # pdf_path was given — same "older caller, column simply
        # unused" convention the ✏ Form column already established.
        thumb_doc = None
        page_scale_cache = {}
        if self._pdf_path:
            try:
                thumb_doc = fitz.open(self._pdf_path)
                equipment_detection.apply_page_rotations(thumb_doc, self.db.get_all_page_rotations())
            except Exception:
                thumb_doc = None
        try:
            for res in self._results:
                r = self._tbl.rowCount()
                self._tbl.insertRow(r)
                self._tbl.setRowHeight(r, self._THUMB_SIZE + 8)

                if thumb_doc is not None:
                    pixmap = self._render_thumbnail(thumb_doc, res, page_scale_cache)
                    if pixmap is not None:
                        lbl = QLabel()
                        lbl.setPixmap(pixmap)
                        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                        self._tbl.setCellWidget(r, self._C_THUMB, lbl)

                chk = QTableWidgetItem()
                untagged_ok = (res.get('tag_status') == 'untagged'
                              and res.get('detection_confidence', 0.0) >= 0.5)
                include_default = res['link_method'] not in ('none', 'not_found') or untagged_ok
                chk.setCheckState(
                    Qt.CheckState.Checked if include_default else Qt.CheckState.Unchecked)
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                self._tbl.setItem(r, self._C_CHK, chk)

                tag_text = res['tag'] or (res.get('temporary_id', '') if res.get('tag_status') == 'untagged' else '')
                tag_item = QTableWidgetItem(tag_text)
                if res.get('tag_status') == 'untagged':
                    tag_item.setToolTip("Ingen tagg hittades nära denna symbol — döp den eller "
                                        "lämna det tillfälliga id:et.")
                self._tbl.setItem(r, self._C_TAG, tag_item)

                pg_item = QTableWidgetItem(str(res['page'] + 1))
                pg_item.setFlags(pg_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                pg_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._tbl.setItem(r, self._C_PAGE, pg_item)

                # Editable dropdown (2026-08-15: made editable; 2026-08-17,
                # see NOTES.md "Typen skall vara en gardinlista enligt vad
                # som finns under standardobjekt": turned into a combobox
                # instead of free text) — _save() below reads this cell's
                # current text, not the frozen res['comp_type'], so a
                # manual correction here (or a mass-tag apply) actually
                # takes effect. Still setEditable(True) so an unusual
                # comp_type value not already in the dropdown (e.g. a
                # custom type from an older scan) is shown rather than
                # silently discarded.
                type_combo = QComboBox()
                type_combo.setEditable(True)
                combo_items = type_options if res['comp_type'] in type_options \
                    else type_options + [res['comp_type']]
                type_combo.addItems(combo_items)
                type_combo.setCurrentText(res['comp_type'])
                self._tbl.setCellWidget(r, self._C_TYPE, type_combo)

                conf = _row_confidence(res)
                pct = int(round(conf * 100))
                conf_item = QTableWidgetItem(f"{pct}%")
                conf_item.setFlags(conf_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                conf_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                breakdown = _row_confidence_breakdown(res)
                if breakdown:
                    conf_item.setToolTip(breakdown)
                if pct >= 70:
                    conf_item.setForeground(QBrush(QColor('#1a7a40')))
                elif pct >= 40:
                    conf_item.setForeground(QBrush(QColor('#b8860b')))
                else:
                    conf_item.setForeground(QBrush(QColor('#8D9299')))
                self._tbl.setItem(r, self._C_CONF, conf_item)

                method_label = self._METHOD_LABELS.get(res['link_method'], res['link_method'])
                method_item = QTableWidgetItem(method_label)
                # Narrow column (2026-08-17, see NOTES.md) — the full
                # label routinely gets clipped, so it's always available
                # on hover instead.
                method_item.setToolTip(method_label)
                method_item.setFlags(method_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if res['link_method'] in ('none', 'not_found'):
                    method_item.setForeground(QBrush(QColor('#aaa')))
                self._tbl.setItem(r, self._C_METHOD, method_item)

                # "Städa bort en ledning" (2026-08-15, see NOTES.md) — only
                # meaningful when there's both a pdf to re-resolve the
                # cluster from and an actual detected outline to prune.
                if self._pdf_path and res.get('outline'):
                    edit_btn = QPushButton("✏ Form")
                    edit_btn.setToolTip(
                        "Ta bort segment (t.ex. en ledning) från den här markörens form")
                    edit_btn.clicked.connect(partial(self._edit_shape, r))
                    self._tbl.setCellWidget(r, self._C_EDIT, edit_btn)
        finally:
            if thumb_doc is not None:
                try:
                    thumb_doc.close()
                except Exception:
                    pass

    def _render_thumbnail(self, doc, res, page_scale_cache):
        """A small, zoomed crop around this row's detected position —
        "Dvs så man kan se grafiskt att det är korrekt och inte bara en
        lista" (2026-08-16, see NOTES.md). Framed on the detected
        outline's own bbox (padded) when available — the same shape a
        real symbol boundary already gives — else a default radius
        around (x, y) scaled to the page's own text size, same
        convention _edit_shape's own nearby-primitives radius already
        uses. Rendered at _PREVIEW_DPI (see that constant's own
        docstring — display-only, has no effect on any matching), then
        scaled down to fit _THUMB_SIZE for a consistent row height
        regardless of how large a region got captured. Returns None on
        any failure (bad page number, non-numeric fields, an image
        library hiccup) — a missing thumbnail must never block
        reviewing/saving the rest of the table."""
        try:
            page_num = res['page']
            if not (0 <= page_num < doc.page_count):
                return None
            page = doc[page_num]
            if res.get('outline'):
                xs = [p[0] for p in res['outline']]
                ys = [p[1] for p in res['outline']]
                x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
                pad = max((x1 - x0), (y1 - y0)) * 0.5 + 8.0
            else:
                if page_num not in page_scale_cache:
                    page_scale_cache[page_num] = symbol_geometry.dominant_text_size(page)
                # "En lite mer utzoomad bild" / "På varje objekt" (2026-08-17,
                # see NOTES.md) — the previous 1.5x/15pt floor cropped tight
                # enough to cut straight through an LKAB-style oblong
                # instrument bubble (confirmed on a real "PI" symbol: the
                # capsule's own rounded ends fell outside the frame
                # entirely, showing only its middle band and label). Wider
                # on both branches so every row's thumbnail shows the whole
                # symbol with a comfortable margin, not just its center.
                pad = max(page_scale_cache[page_num] * 3.0, 28.0)
                x0, y0 = res['x'], res['y']
                x1, y1 = x0, y0
            clip = fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad) & page.rect
            if clip.is_empty or clip.width < 1 or clip.height < 1:
                return None
            gray = image_symbol_matching.render_gray(page, bbox=tuple(clip), dpi=_PREVIEW_DPI)
            if gray.size == 0 or min(gray.shape) < 1:
                return None
            h, w = gray.shape
            qimg = QImage(gray.tobytes(), w, h, w, QImage.Format.Format_Grayscale8)
            pixmap = QPixmap.fromImage(qimg)
            return pixmap.scaled(
                self._THUMB_SIZE, self._THUMB_SIZE,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        except Exception:
            return None

    def _edit_shape(self, row):
        """"✏ Form" button — re-resolves this row's own vector cluster
        (equipment_detection.resolve_reference_cluster, same lookup
        "Hitta liknande symbol" uses) and lets the user prune it via
        MarkerShapeEditDialog, updating this row's outline (and its
        x/y, re-centred on whatever's left) on OK.

        2026-08-15 follow-up (see NOTES.md "'Hitta liknande symbol'
        visar bara ett streck"): also shows every primitive within a
        generous radius of the marker, not just what auto-detection
        grouped together — see _find_similar_symbol's matching note for
        why (densely-fragmented CAD exports can leave a real symbol's
        own strokes split across many small, ungrouped pieces)."""
        res = self._results[row]
        try:
            doc = fitz.open(self._pdf_path)
        except Exception as e:
            QMessageBox.warning(self, "Kunde inte öppna PDF", str(e))
            return
        try:
            # res['x']/['y'] were resolved against the LIVE view's document,
            # which has any manual per-page rotation override already
            # applied — this freshly-opened one needs the same override
            # re-applied, or the lookup misses/misplaces entirely on a
            # page with an override (2026-08-15, see NOTES.md "Hitta
            # liknande symbol placerar fel — sidrotation").
            equipment_detection.apply_page_rotations(doc, self.db.get_all_page_rotations())
            resolved = equipment_detection.resolve_reference_cluster(
                doc, res['page'], res['x'], res['y'])
            if resolved is not None:
                ref_scale = symbol_geometry.dominant_text_size(doc[res['page']])
        finally:
            doc.close()
        if resolved is None:
            QMessageBox.information(
                self, "Ingen form att redigera",
                "Hittade ingen vektorform att redigera på den här positionen.")
            return
        primitives, native_index_group, _cluster = resolved
        radius = max(ref_scale * 1.0, 12.0)
        nearby = symbol_geometry.primitives_near_point(
            primitives, res['x'], res['y'], radius, scale=ref_scale)
        wide_index_group = sorted(symbol_geometry.widen_by_connectivity(
            primitives, native_index_group, nearby))
        initial_excluded = set(wide_index_group) - set(native_index_group)
        editor = MarkerShapeEditDialog(
            primitives, wide_index_group, parent=self, initial_excluded=initial_excluded)
        if editor.exec() != QDialog.DialogCode.Accepted:
            return
        outline = editor.edited_outline()
        if not outline:
            return
        res['outline'] = outline
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        res['x'] = (min(xs) + max(xs)) / 2
        res['y'] = (min(ys) + max(ys)) / 2

    def _save(self):
        saved = 0
        for r, res in enumerate(self._results):
            chk = self._tbl.item(r, self._C_CHK)
            if not (chk and chk.checkState() == Qt.CheckState.Checked):
                continue
            tag_item = self._tbl.item(r, self._C_TAG)
            tag = tag_item.text().strip() if tag_item and tag_item.text().strip() else res['tag']
            # Typ is now an editable dropdown (2026-08-15: made editable;
            # 2026-08-17, see NOTES.md: turned into a combobox) — read the
            # cell widget's current text, not the frozen res['comp_type'],
            # so a manual correction or a mass-tag apply actually takes
            # effect.
            type_widget = self._tbl.cellWidget(r, self._C_TYPE)
            comp_type = type_widget.currentText().strip() if type_widget and type_widget.currentText().strip() \
                else res['comp_type']
            equipment_id = res.get('equipment_id')

            # Rows with no equipment_id yet (shape-anchored hits that
            # weren't already a known tag) — if the user gave it a tag
            # (suggested or typed in), register it as a real
            # equipment_catalog entry now, so it shows up in
            # Utrustningsregistret/nodskapande like any other scanned tag.
            if equipment_id is None and tag:
                # Reuse an already-registered object under the same tag
                # instead of creating a duplicate (2026-08-17, see NOTES.md)
                # — matches place_equipment_marker's own dedup check,
                # which this batch path was missing. Otherwise the same
                # physical object found again by a similarity search
                # spawns a second, node_id=NULL row alongside the
                # original (already node-linked) one.
                existing = self.db.get_equipment_by_tag(tag)
                if existing:
                    equipment_id = existing['id']
                else:
                    prefix = _equip_prefix_from_tag(tag) or ''
                    equipment_id = self.db.add_equipment_item(
                        tag, tag, prefix, res['page'], comp_type, '', 0)
                    if self._active_node_id is not None:
                        self.db.set_equipment_node(equipment_id, self._active_node_id)

            outline_json = json.dumps(res['outline']) if res.get('outline') else ''
            self.db.add_equipment_marker(
                equipment_id, tag, res['page'], res['x'], res['y'],
                comp_type, shape_outline=outline_json,
                confidence=_row_confidence(res), link_method=res['link_method'],
                detection_confidence=res.get('detection_confidence'),
                tag_reading_confidence=res.get('tag_reading_confidence'),
                tag_assignment_confidence=res.get('tag_assignment_confidence'),
                line_assignment_confidence=res.get('line_assignment_confidence'),
                line_number=res.get('line_number', ''),
                medium_code=res.get('medium_code', ''),
                medium_code_verified=int(bool(res.get('medium_code_verified'))),
                nominal_size=res.get('nominal_size', ''),
                tag_status=res.get('tag_status', 'tagged'))
            saved += 1
        if saved == 0:
            QMessageBox.information(self, "Inget sparat", "Inga rader var ikryssade.")
            return
        self.accept()

    def _apply_mass_tag(self):
        """"Tillämpa på ikryssade" (2026-08-15, see NOTES.md) — writes
        the chosen type into every checked row's Typ cell, and (if a
        start tag was given) an auto-incrementing tag sequence into
        their Tagg cells, in table row order. Always overwrites —
        simple and predictable, matching this dialog's existing
        straightforward per-cell-editing model."""
        rows = [r for r in range(self._tbl.rowCount())
                if (chk := self._tbl.item(r, self._C_CHK)) and
                   chk.checkState() == Qt.CheckState.Checked]
        if not rows:
            QMessageBox.information(self, "Inga rader ikryssade",
                "Kryssa i minst en rad att tillämpa på.")
            return
        comp_type = self._mass_type_cb.currentText().strip()
        start_tag = self._mass_tag_edit.text().strip()
        if not comp_type and not start_tag:
            return
        tags = None
        if start_tag:
            existing = {e['tag'] for e in self.db.equipment_items()}
            tags = _next_tag_sequence(start_tag, len(rows), existing_tags=existing)
        for i, r in enumerate(rows):
            if comp_type:
                self._tbl.cellWidget(r, self._C_TYPE).setCurrentText(comp_type)
            if tags:
                self._tbl.item(r, self._C_TAG).setText(tags[i])

    def _autodetect_tags(self):
        """"🔍 Autodetektera tagnummer" (2026-08-17, see NOTES.md) — for
        every checked row, re-searches the PDF's own native text near
        that row's detected position (equipment_detection.
        find_tag_near_point, the same nearest-tag lookup the initial
        scan itself uses) and fills the Tagg cell with whatever it
        finds. Most useful for shape-only ("untagged") rows whose Tagg
        cell still shows a placeholder temporary id — a one-click retry
        instead of hunting the P&ID by eye for each one. Leaves a row's
        Tagg cell untouched if nothing is found nearby, rather than
        blanking an already-reasonable tag."""
        if not self._pdf_path:
            return
        rows = [r for r in range(self._tbl.rowCount())
                if (chk := self._tbl.item(r, self._C_CHK)) and
                   chk.checkState() == Qt.CheckState.Checked]
        if not rows:
            QMessageBox.information(self, "Inga rader ikryssade",
                "Kryssa i minst en rad att autodetektera tagg för.")
            return
        try:
            doc = fitz.open(self._pdf_path)
        except Exception as e:
            QMessageBox.warning(self, "Kunde inte öppna PDF", str(e))
            return
        found = 0
        try:
            equipment_detection.apply_page_rotations(doc, self.db.get_all_page_rotations())
            for r in rows:
                res = self._results[r]
                tag = equipment_detection.find_tag_near_point(doc, res['page'], res['x'], res['y'])
                if tag:
                    self._tbl.item(r, self._C_TAG).setText(tag)
                    found += 1
        finally:
            doc.close()
        QMessageBox.information(
            self, "Klart", f"Hittade en taggtext för {found} av {len(rows)} ikryssade rader.")


class TargetPickerDialog(QDialog):
    def __init__(self, parent=None, suggested_tag='', db=None):
        super().__init__(parent)
        self.setWindowTitle("Välj konsekvens")
        self.setMinimumWidth(500)
        self._db            = db
        self.template       = ''
        self.target         = ''
        self.selected_chain = {}
        self.link_to_id     = None   # set if user picks an existing consequence

        layout = QVBoxLayout(self)

        # Link to existing button (when db is available)
        if db is not None:
            link_btn = QPushButton("Länka till befintlig konsekvens")
            link_btn.setIcon(_icon('link'))
            link_btn.setStyleSheet(
                "background:#2c7bb6; color:white; border:none; border-radius:4px; padding:4px 10px;")
            link_btn.clicked.connect(self._pick_existing)
            layout.addWidget(link_btn)
            sep = QLabel("— eller skapa ny konsekvens —")
            sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sep.setStyleSheet("color:#888; font-size:10px;")
            layout.addWidget(sep)

        # ── Template list ─────────────────────────────────────────────────────
        layout.addWidget(QLabel("Konsekvensmall:"))
        self.template_list = QListWidget()
        self.template_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.template_list.setMaximumHeight(130)
        for tpl in CONSEQUENCE_TEMPLATES:
            self.template_list.addItem(QListWidgetItem(tpl))
        self.template_list.setCurrentRow(0)
        self.template_list.currentRowChanged.connect(self._update_preview)
        layout.addWidget(self.template_list)

        form = QFormLayout()
        self.target_edit = QLineEdit(suggested_tag)
        self.target_edit.setPlaceholderText("t.ex. T-101")
        self.target_edit.textChanged.connect(self._update_preview)
        form.addRow("Målobjekt:", self.target_edit)
        layout.addLayout(form)

        # ── Consequence chain ─────────────────────────────────────────────────
        chain_box = QGroupBox("Konsekvenskedja  (valfritt)")
        chain_grid = QGridLayout(chain_box)
        chain_grid.setSpacing(3)
        self._chain_checks: dict = {}
        row_idx, col_idx, last_group = 0, 0, None

        for key, label, group in _PID_CHAIN_ITEMS:
            if group and group != last_group:
                if col_idx > 0:
                    row_idx += 1; col_idx = 0
                hdr = QLabel(group)
                hdr.setStyleSheet(
                    "color:#1F4E79; font-weight:bold; font-size:10px; margin-top:3px;")
                chain_grid.addWidget(hdr, row_idx, 0, 1, 3)
                row_idx += 1; col_idx = 0
                last_group = group
            chk = QCheckBox(label)
            chk.stateChanged.connect(self._update_preview)
            self._chain_checks[key] = chk
            chain_grid.addWidget(chk, row_idx, col_idx)
            col_idx += 1
            if col_idx >= 3:
                col_idx = 0; row_idx += 1

        layout.addWidget(chain_box)

        # ── Full preview ──────────────────────────────────────────────────────
        layout.addWidget(QLabel("Fullständig text:"))
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(
            "color:#1F4E79; font-weight:bold; padding:4px;"
            "background:#eef4fb; border:1px solid #bee3f8; border-radius:3px;")
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_preview()

    def set_target(self, name):
        self.target_edit.setText(name or '')

    def _current_template(self):
        item = self.template_list.currentItem()
        return item.text() if item else (CONSEQUENCE_TEMPLATES[0] if CONSEQUENCE_TEMPLATES else '{}')

    def _base_text(self):
        tpl  = self._current_template()
        name = self.target_edit.text().strip() or '[okänt objekt]'
        try:
            return tpl.format(name)
        except Exception:
            return tpl.replace('{}', name)

    def _update_preview(self, *_):
        chain = {k: chk.isChecked() for k, chk in self._chain_checks.items()}
        full  = _pid_build_chain_text(self._base_text(), chain)
        self.preview.setText(full or self._base_text())

    def _pick_existing(self):
        if self._db is None:
            return
        dlg = ExistingConsequencePicker(self._db, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_id:
            self.link_to_id = dlg.selected_id
            self.accept()

    def _on_accept(self):
        self.template = self._current_template()
        self.target   = self.target_edit.text().strip()
        self.selected_chain = {k: chk.isChecked()
                               for k, chk in self._chain_checks.items()}
        self.accept()


class ExistingConsequencePicker(QDialog):
    """Pick an existing consequence from the project to link to the current cause."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.selected_id = None
        self.setWindowTitle("Länka till befintlig konsekvens")
        self.setMinimumSize(640, 380)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Välj en befintlig konsekvens att länka den nya orsaken till:"))

        # Search
        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filtrera…")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        # Table
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(['Nod', 'Orsak', 'Konsekvens', 'Risk'])
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 90)
        self._table.setColumnWidth(3, 80)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.doubleClicked.connect(self._accept)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:3px;}")
        layout.addWidget(self._table)

        btns = QHBoxLayout()
        ok_btn = QPushButton("Länka till markerad")
        ok_btn.setIcon(_icon('link'))
        ok_btn.clicked.connect(self._accept)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(ok_btn); btns.addStretch(); btns.addWidget(cancel_btn)
        layout.addLayout(btns)

        self._populate()

    def _populate(self):
        self._rows = []   # list of (cons_id, node_name, cause_desc, cons_desc, risk_label, bg)
        try:
            for node in self.db.nodes():
                for cause in self.db.causes(node['id']):
                    for cons in self.db.consequences(cause['id']):
                        self._rows.append((
                            cons['id'],
                            node['name'],
                            cause['description'],
                            cons['description'],
                        ))
        except Exception:
            pass
        self._apply_filter()

    def _apply_filter(self):
        text = self._filter.text().lower()
        self._table.setRowCount(0)
        for cons_id, node, cause, cons_desc in self._rows:
            if text and text not in (node + cause + cons_desc).lower():
                continue
            r = self._table.rowCount()
            self._table.insertRow(r)
            for col, val in enumerate([node, cause, cons_desc, '']):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, cons_id)
                self._table.setItem(r, col, item)
            self._table.setRowHeight(r, 24)

    def _accept(self):
        row = self._table.currentRow()
        if row < 0:
            return
        item = self._table.item(row, 0)
        if item:
            self.selected_id = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


class ExistingSafeguardPicker(QDialog):
    """Pick an existing safeguard to link to the current consequence."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.selected_id = None
        self.setWindowTitle("Länka till befintlig safeguard")
        self.setMinimumSize(600, 340)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Välj en befintlig safeguard att länka till denna konsekvens:"))

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filtrera…")
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(['Konsekvens', 'Safeguard', 'RRF'])
        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(2, 70)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.doubleClicked.connect(self._accept)
        self._table.setStyleSheet(
            "QHeaderView::section{background:#1F4E79;color:#fff;font-weight:bold;padding:3px;}")
        layout.addWidget(self._table)

        btns = QHBoxLayout()
        ok_btn = QPushButton("Länka till markerad")
        ok_btn.setIcon(_icon('link'))
        ok_btn.clicked.connect(self._accept)
        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(ok_btn); btns.addStretch(); btns.addWidget(cancel_btn)
        layout.addLayout(btns)

        self._populate()

    def _populate(self):
        self._rows = []
        try:
            for node in self.db.nodes():
                for cause in self.db.causes(node['id']):
                    for cons in self.db.consequences(cause['id']):
                        for sg in self.db.safeguards(cons['id']):
                            self._rows.append((
                                sg['id'],
                                cons['description'],
                                sg['description'],
                                sg['rrf'] or 1,
                            ))
        except Exception:
            pass
        self._apply_filter()

    def _apply_filter(self):
        text = self._filter.text().lower()
        self._table.setRowCount(0)
        for sg_id, cons_desc, sg_desc, rrf in self._rows:
            if text and text not in (cons_desc + sg_desc).lower():
                continue
            r = self._table.rowCount()
            self._table.insertRow(r)
            for col, val in enumerate([cons_desc, sg_desc, f"RRF {rrf}" if rrf > 1 else "—"]):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, sg_id)
                self._table.setItem(r, col, item)
            self._table.setRowHeight(r, 24)

    def _accept(self):
        row = self._table.currentRow()
        if row < 0:
            return
        item = self._table.item(row, 0)
        if item:
            self.selected_id = item.data(Qt.ItemDataRole.UserRole)
            self.accept()


class _PageRenderer(QThread):
    """Background thread that pre-renders PDF pages to raw pixel data for the page cache."""
    # page_num, raw_bytes, w, h, stride, scale (the fitz.Matrix scale actually used —
    # callers need this because a page can now be rendered above the caller's
    # nominal _RASTER_SCALE, see PIDGraphicsView._target_raster_scale)
    page_ready = pyqtSignal(int, object, int, int, int, float)

    def __init__(self, pdf_path, pages, scale, parent=None, rotations=None):
        super().__init__(parent)
        self._path  = pdf_path
        self._pages = pages
        self._scale = scale
        # {page_num: total rotation degrees} — this thread opens its OWN fitz
        # document (can't safely share PIDGraphicsView.pdf_doc across
        # threads), so a manual per-page rotation override applied there
        # (see PIDGraphicsView._apply_page_rotation) has to be re-applied
        # here too, or a background-rendered pixmap would silently ignore it.
        self._rotations = rotations or {}

    def run(self):
        if not HAS_PYMUPDF:
            return
        doc = None
        try:
            doc = fitz.open(self._path)
            for pn in self._pages:
                if self.isInterruptionRequested():
                    break
                page = doc.load_page(pn)
                if pn in self._rotations:
                    page.set_rotation(self._rotations[pn])
                mat  = fitz.Matrix(self._scale, self._scale)
                pix  = page.get_pixmap(matrix=mat, alpha=False)
                self.page_ready.emit(pn, bytes(pix.samples), pix.width, pix.height,
                                     pix.stride, self._scale)
        except Exception as e:
            # Log exception silently — thread errors shouldn't crash main UI
            import logging
            logging.debug(f"_PageRenderer failed for {self._path}: {e}")
        finally:
            # Ensure PDF document is always closed, even if exception occurred
            if doc is not None:
                doc.close()


class SmartPipeTracer:
    """
    Traces pipe paths on a rendered P&ID page using A* on a greyscale image.
    Works on both colour and B&W P&IDs; detects dark pixels as pipe material.
    Gap-jumping handles crossings drawn with a small break between lines.
    """
    DARK_THRESHOLD = 110    # pixels darker than this count as "pipe"
    TRACE_SCALE    = 1.0    # render resolution multiplier for tracing
    MAX_GAP        = 7      # max white-pixel gap to jump across (crossing style)
    MAX_EXPLORE    = 300_000  # A* node limit (safety)
    GOAL_RADIUS_SQ = 25     # squared pixel distance that counts as "reached end"

    def __init__(self, pdf_doc, page_n):
        page = pdf_doc[page_n]
        mat  = fitz.Matrix(self.TRACE_SCALE, self.TRACE_SCALE)
        pix  = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        self.width  = pix.width
        self.height = pix.height
        self._data  = bytearray(pix.samples)   # flat greyscale bytes
        self._tmask = self._build_text_mask(page)   # pixels inside text bboxes

    # ------------------------------------------------------------------ helpers

    def _build_text_mask(self, page):
        """
        Return a bytearray (same size as image) where 1 = inside a text bbox.
        Uses PyMuPDF text extraction — works on vector P&IDs natively.
        For scanned rasters with no text layer the result is all-zero (no effect).
        """
        mask = bytearray(self.width * self.height)
        pad  = max(2, int(4 * self.TRACE_SCALE))  # pixel padding around each bbox
        try:
            blocks = page.get_text("dict", flags=0)["blocks"]
            for block in blocks:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        bx0, by0, bx1, by1 = span["bbox"]
                        # Convert PDF coords → tracer pixel coords and expand
                        px0 = max(0,            int(bx0 * self.TRACE_SCALE) - pad)
                        py0 = max(0,            int(by0 * self.TRACE_SCALE) - pad)
                        px1 = min(self.width,   int(bx1 * self.TRACE_SCALE) + pad + 1)
                        py1 = min(self.height,  int(by1 * self.TRACE_SCALE) + pad + 1)
                        W = self.width
                        for y in range(py0, py1):
                            base = y * W
                            for x in range(px0, px1):
                                mask[base + x] = 1
        except Exception:
            pass  # if extraction fails, proceed without masking (graceful degradation)
        return mask

    def _is_dark(self, x, y):
        """Raw dark-pixel check (no text exclusion)."""
        if 0 <= x < self.width and 0 <= y < self.height:
            return self._data[y * self.width + x] < self.DARK_THRESHOLD
        return False

    def _is_pipe(self, x, y):
        """True only if pixel is dark AND not inside a text bounding box."""
        if 0 <= x < self.width and 0 <= y < self.height:
            idx = y * self.width + x
            return self._data[idx] < self.DARK_THRESHOLD and not self._tmask[idx]
        return False

    def _nearest_dark(self, x, y, radius=15):
        """Spiral-search for nearest dark pixel within radius."""
        x, y = int(x), int(y)
        if self._is_pipe(x, y):
            return (x, y)
        best, best_d2 = None, radius * radius + 1
        for r in range(1, radius + 1):
            for dx in range(-r, r + 1):
                for sign in (-1, 1):
                    nx, ny = x + dx, y + sign * r
                    if 0 <= nx < self.width and 0 <= ny < self.height:
                        d2 = dx * dx + r * r
                        if d2 < best_d2 and self._is_pipe(nx, ny):
                            best, best_d2 = (nx, ny), d2
                for dy in range(-r + 1, r):
                    ny, nx = y + dy, x + sign * r
                    if 0 <= nx < self.width and 0 <= ny < self.height:
                        d2 = r * r + dy * dy
                        if d2 < best_d2 and self._is_pipe(nx, ny):
                            best, best_d2 = (nx, ny), d2
        return best

    def _reconstruct(self, came_from, node):
        path = []
        cur  = node
        while cur is not None:
            path.append(cur)
            cur = came_from[cur]
        path.reverse()
        return path

    def _rdp(self, pts, eps=2.5):
        """Iterative Ramer-Douglas-Peucker path simplification."""
        if len(pts) < 3:
            return list(pts)
        keep  = {0, len(pts) - 1}
        stack = [(0, len(pts) - 1)]
        while stack:
            lo, hi = stack.pop()
            if hi - lo < 2:
                continue
            x1, y1 = pts[lo]
            x2, y2 = pts[hi]
            dx, dy  = x2 - x1, y2 - y1
            d2      = dx * dx + dy * dy
            max_d, max_i = 0.0, lo
            for i in range(lo + 1, hi):
                px, py = pts[i]
                if d2 == 0:
                    dist = ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
                else:
                    t    = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / d2))
                    dist = ((px - x1 - t * dx) ** 2 + (py - y1 - t * dy) ** 2) ** 0.5
                if dist > max_d:
                    max_d, max_i = dist, i
            if max_d > eps:
                keep.add(max_i)
                stack.append((lo, max_i))
                stack.append((max_i, hi))
        return [pts[i] for i in sorted(keep)]

    # ------------------------------------------------------------------ core A*

    def _astar(self, start, end, blocked):
        import heapq
        from itertools import count
        _cnt = count()

        ex, ey = end
        def h(x, y): return ((x - ex) ** 2 + (y - ey) ** 2) ** 0.5

        open_h    = [(h(*start), next(_cnt), start)]
        g         = {start: 0.0}
        came_from = {start: None}
        explored  = 0
        W = self.width
        data  = self._data
        tmask = self._tmask
        thr   = self.DARK_THRESHOLD

        while open_h and explored < self.MAX_EXPLORE:
            _, _, cur = heapq.heappop(open_h)
            explored += 1
            cx, cy = cur

            if (cx - ex) ** 2 + (cy - ey) ** 2 <= self.GOAL_RADIUS_SQ:
                return self._reconstruct(came_from, cur)

            gc = g[cur]
            prev = came_from.get(cur)

            # --- 8-directional dark neighbours ---
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == dy == 0:
                        continue
                    nx, ny = cx + dx, cy + dy
                    if nx < 0 or nx >= self.width or ny < 0 or ny >= self.height:
                        continue
                    if data[ny * W + nx] >= thr or tmask[ny * W + nx]:
                        continue
                    nb = (nx, ny)
                    if nb in blocked:
                        continue
                    step = 1.414 if dx and dy else 1.0
                    ng   = gc + step
                    if ng < g.get(nb, 1e18):
                        g[nb]         = ng
                        came_from[nb] = cur
                        f = ng + h(nx, ny)
                        heapq.heappush(open_h, (f, next(_cnt), nb))

            # --- gap jump: continue in last movement direction across short gap ---
            if prev is not None:
                pdx = cx - prev[0]
                pdy = cy - prev[1]
                ndx = (pdx // abs(pdx)) if pdx else 0
                ndy = (pdy // abs(pdy)) if pdy else 0
                if ndx or ndy:
                    # check if current pixel is at the edge of a dark region
                    step_base = 1.414 if ndx and ndy else 1.0
                    for gap in range(2, self.MAX_GAP + 1):
                        jx, jy = cx + ndx * gap, cy + ndy * gap
                        if not (0 <= jx < self.width and 0 <= jy < self.height):
                            break
                        if data[jy * W + jx] < thr and not tmask[jy * W + jx]:
                            jb = (jx, jy)
                            if jb not in blocked:
                                ng = gc + gap * step_base * 0.85  # slight bonus for jumping
                                if ng < g.get(jb, 1e18):
                                    g[jb]         = ng
                                    came_from[jb] = cur
                                    f = ng + h(jx, jy)
                                    heapq.heappush(open_h, (f, next(_cnt), jb))
                            break  # only jump to first dark pixel found

        return None  # path not found

    # ------------------------------------------------------------------ public

    def trace(self, start_pdf, end_pdf, n_alt=2):
        """
        Find up to (n_alt+1) paths from start_pdf to end_pdf.
        Returns list of paths; each path = list of [pdf_x, pdf_y].
        First element is the best path.  Empty list = nothing found.
        """
        sx = int(start_pdf[0] * self.TRACE_SCALE)
        sy = int(start_pdf[1] * self.TRACE_SCALE)
        ex = int(end_pdf[0]   * self.TRACE_SCALE)
        ey = int(end_pdf[1]   * self.TRACE_SCALE)

        start = self._nearest_dark(sx, sy, 15)
        end   = self._nearest_dark(ex, ey, 15)
        if not start or not end:
            return []

        results = []
        blocked = set()

        for _ in range(n_alt + 1):
            px_path = self._astar(start, end, frozenset(blocked))
            if not px_path:
                break
            simplified  = self._rdp(px_path, eps=2.5)
            pdf_path    = [[x / self.TRACE_SCALE, y / self.TRACE_SCALE]
                           for x, y in simplified]
            results.append(pdf_path)
            # Block the middle half of this path so next search takes a different route
            lo = len(px_path) // 4
            hi = 3 * len(px_path) // 4
            blocked.update(px_path[lo:hi])

        return results


class ConnectorAnalyzer(QThread):
    """Scans PDF pages for off-page connectors and proposes a board layout."""
    progress   = pyqtSignal(str)
    # connectors, connections, layout, page_sheet_nums
    finished_analysis = pyqtSignal(list, list, dict, dict)

    def __init__(self, pdf_path, page_count, page_widths_pdf, page_heights_pdf,
                 render_scale, active_pages=None, parent=None):
        super().__init__(parent)
        self._pdf_path        = str(pdf_path)
        self._page_count      = page_count
        self._page_widths_pdf = dict(page_widths_pdf)
        self._page_heights_pdf = dict(page_heights_pdf)
        self._render_scale    = render_scale
        # active_pages: only these get laid out (others are scanned for connectors only)
        self._active_pages    = list(active_pages) if active_pages is not None else None
        self._deadline        = 0.0

    def run(self):
        import time
        self._deadline = time.time() + 45.0   # longer for OCR-heavy sets
        all_connectors = []
        page_sheet_nums = {}  # pn → sheet number string (best guess)
        doc = None

        try:
            try:
                doc = fitz.open(self._pdf_path)
            except Exception as e:
                self.progress.emit(f"Kunde inte öppna PDF: {e}")
                self.finished_analysis.emit([], [], {}, {})
                return

            # ── Auto-detect dialect from first 5 pages ────────────────────────────
            sample_texts = []
            for pn in range(min(5, doc.page_count)):
                sample_texts.append(doc.load_page(pn).get_text("text"))
            self._dialect = _detect_dialect(sample_texts)
            dialect_conf  = _DIALECTS[self._dialect]
            self.progress.emit(f"Dialekt: {dialect_conf['name']}")

            for pn in range(doc.page_count):
                if time.time() > self._deadline:
                    self.progress.emit(f"Tidsgräns — {pn}/{doc.page_count} blad klara")
                    break
                self.progress.emit(f"Blad {pn + 1}/{doc.page_count}…")
                page = doc.load_page(pn)
                pw = float(page.rect.width)
                ph = float(page.rect.height)

                # ── Extract sheet number using dialect title area ──────────────────
                ta = dialect_conf['title_area']
                title_rect = fitz.Rect(pw*ta[0], ph*ta[1], pw*ta[2], ph*ta[3])
                title_text = page.get_text("text", clip=title_rect)
                m = dialect_conf['sheet_num_re'].search(title_text)
                if m:
                    page_sheet_nums[pn] = m.group(1).upper().strip()

                # ── Native text in edge zones ──────────────────────────────────────
                spans = self._get_spans(page)
                native_word_count = len(spans)
                connectors = self._find_in_zones(spans, pn, pw, ph,
                                                 ocr_used=False, page=page)

                # ── OCR: trigger when page has few native words (scanned PDF) ──────
                needs_ocr = (not connectors or native_word_count < 30)
                if needs_ocr and HAS_PYMUPDF and time.time() < self._deadline - 2.0:
                    ocr_text = self._ocr_edges(page, pw, ph)
                    if ocr_text:
                        ocr_spans = self._text_to_spans(ocr_text, pw, ph)
                        ocr_conns = self._find_in_zones(ocr_spans, pn, pw, ph,
                                                        ocr_used=True, page=page)
                        if ocr_conns:
                            connectors = ocr_conns
                        # Also try to extract sheet number from OCR if not found yet
                        if pn not in page_sheet_nums:
                            all_ocr = ' '.join(ocr_text.values())
                            m2 = dialect_conf['sheet_num_re'].search(all_ocr)
                            if m2:
                                page_sheet_nums[pn] = m2.group(1).upper().strip()

                all_connectors.extend(connectors)

            # ── Build sheet-number lookup: sheet_str (and variants) → pn ──
            sheet_lookup = {}
            for k, v in page_sheet_nums.items():
                for variant in _sheet_ref_variants(v.upper()):
                    sheet_lookup.setdefault(variant, k)

            # ── Match connectors into connections ──
            connections = self._match_connections(all_connectors, sheet_lookup,
                                                  self._page_count, page_sheet_nums)

            # ── Propose layout (active pages only) ──
            layout_pages = (self._active_pages if self._active_pages is not None
                            else list(range(self._page_count)))
            layout = _propose_layout(connections, layout_pages,
                                     self._page_widths_pdf, self._page_heights_pdf,
                                     self._render_scale)

            # Convert int keys to str for JSON serialisation
            sheet_num_map_str = {str(k): v for k, v in page_sheet_nums.items()}
            self.finished_analysis.emit(all_connectors, connections, layout, sheet_num_map_str)
        except Exception as e:
            import logging
            logging.error(f"ConnectorAnalyzer.run() failed: {e}", exc_info=True)
            # CRITICAL: still emit finished_analysis so the UI unblocks and
            # the modal progress dialog closes instead of hanging forever.
            self.finished_analysis.emit([], [], {}, {})
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

    # ── helpers ───────────────────────────────────────────────────────────────

    def _get_spans(self, page):
        spans = []
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = span.get("text", "").strip()
                    if not t:
                        continue
                    b = span["bbox"]
                    spans.append({"text": t, "x": (b[0]+b[2])/2, "y": (b[1]+b[3])/2,
                                  "x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]})
        return spans

    def _text_to_spans(self, text_dict, pw, ph):
        """Convert OCR output dict {edge: str} into pseudo-spans with edge positions."""
        spans = []
        for edge, text in text_dict.items():
            if not text.strip():
                continue
            if edge == 'left':
                cx, cy = pw * 0.12, ph * 0.5
            elif edge == 'right':
                cx, cy = pw * 0.88, ph * 0.5
            elif edge == 'top':
                cx, cy = pw * 0.5, ph * 0.06
            else:
                cx, cy = pw * 0.5, ph * 0.94
            for line in text.split('\n'):
                line = line.strip()
                if line:
                    spans.append({"text": line, "x": cx, "y": cy,
                                  "x0": cx-10, "y0": cy-5, "x1": cx+10, "y1": cy+5})
        return spans

    def _find_arrow_shapes(self, page, pw, ph):
        """Detect pentagon/arrow connector shapes near page edges using vector graphics.
        Returns list of (cx, cy, edge) tuples in PDF coordinates."""
        results = []
        try:
            drawings = page.get_drawings()
        except Exception:
            return results
        for d in drawings:
            r = d.get('rect')
            if r is None:
                continue
            w = r.x1 - r.x0
            h = r.y1 - r.y0
            # Skip tiny marks and huge blocks (title boxes etc.)
            if w < 15 or h < 6 or w > pw * 0.55 or h > ph * 0.25:
                continue
            # Must be near an edge
            at_left   = r.x1 < pw * 0.32
            at_right  = r.x0 > pw * 0.68
            at_top    = r.y1 < ph * 0.26
            at_bottom = r.y0 > ph * 0.74
            if not (at_left or at_right or at_top or at_bottom):
                continue
            # Arrow-like aspect ratio: for left/right edges wide>tall; top/bottom tall>wide
            if (at_left or at_right) and not (1.5 < w / max(h, 1) < 20):
                continue
            if (at_top or at_bottom) and not (0.3 < w / max(h, 1) < 3):
                continue
            n_items = len(d.get('items', []))
            if not (3 <= n_items <= 10):
                continue
            cx = (r.x0 + r.x1) / 2
            cy = (r.y0 + r.y1) / 2
            if at_left:
                edge = 'left'
            elif at_right:
                edge = 'right'
            elif at_top:
                edge = 'top'
            else:
                edge = 'bottom'
            results.append((cx, cy, edge, r))
        return results

    def _find_in_zones(self, spans, pn, pw, ph, ocr_used, page=None):
        """Find off-page connectors in the page's edge band.

        Each candidate is assigned to its NEAREST page edge — a connector in
        the top-left corner belongs to whichever edge it is closest to — and
        candidates are deduplicated by (ref_sheet, position) across all
        detection passes, so the same physical symbol can never appear as two
        connectors on different edges (the old per-zone scan produced corner
        duplicates: one 'left'/'in' + one 'top'/'unknown' for the same symbol).
        """
        accepted = []

        ta = _DIALECTS.get(getattr(self, '_dialect', 'classic'), {}).get('title_area')

        def in_title_area(x, y):
            return (ta is not None
                    and pw*ta[0] <= x <= pw*ta[2]
                    and ph*ta[1] <= y <= ph*ta[3])

        def nearest_edge(x, y):
            d = {'left': x, 'right': pw - x, 'top': y, 'bottom': ph - y}
            return min(d, key=d.get)

        def consider(text, x, y):
            conn = self._parse_connector(text, nearest_edge(x, y), pn, x, y,
                                         pw, ph, ocr_used)
            if not conn:
                return
            # Directionless refs inside the title block are almost always the
            # drawing-reference list / title text, not flow connectors.
            if conn['direction'] == 'unknown' and in_title_area(x, y):
                return
            for i, old in enumerate(accepted):
                if (old['ref_sheet'] == conn['ref_sheet']
                        and abs(old['x_pdf'] - x) < pw * 0.05
                        and abs(old['y_pdf'] - y) < ph * 0.05):
                    if conn['confidence'] > old['confidence']:
                        accepted[i] = conn
                    return
            accepted.append(conn)

        # ── Pass 1: text clusters in the edge band ────────────────────────────
        band = [s for s in spans
                if s["x"] <= pw*0.32 or s["x"] >= pw*0.68
                or s["y"] <= ph*0.26 or s["y"] >= ph*0.74]
        for cluster in self._cluster_spans(band, gap=60.0):
            combined = ' '.join(s["text"] for s in cluster)
            cx = sum(s["x"] for s in cluster) / len(cluster)
            cy = sum(s["y"] for s in cluster) / len(cluster)
            consider(combined, cx, cy)

        # ── Pass 2: vector-shape anchored search ──────────────────────────────
        if page is not None:
            for shape_cx, shape_cy, _edge, shape_rect in self._find_arrow_shapes(page, pw, ph):
                margin_x = max(shape_rect.width * 1.5, 40)
                margin_y = max(shape_rect.height * 2.0, 40)
                nearby = [s for s in spans
                          if abs(s["x"] - shape_cx) < margin_x + shape_rect.width
                          and abs(s["y"] - shape_cy) < margin_y + shape_rect.height]
                if nearby:
                    consider(' '.join(s["text"] for s in nearby),
                             shape_cx, shape_cy)
        return accepted

    def _cluster_spans(self, spans, gap=60.0):
        if not spans:
            return []
        spans = sorted(spans, key=lambda s: (s["y"], s["x"]))
        clusters, current = [], [spans[0]]
        for s in spans[1:]:
            prev = current[-1]
            dy = abs(s["y"] - prev["y"])
            dx = abs(s["x"] - prev["x"])
            if dy < gap and dx < gap * 3:
                current.append(s)
            else:
                clusters.append(current)
                current = [s]
        clusters.append(current)
        return clusters

    def _parse_connector(self, text, edge, pn, cx, cy, pw, ph, ocr_used):
        dialect = getattr(self, '_dialect', 'classic')
        keyword = ref_sheet = rds_code = None
        lkab_format = its_format = gryaab_format = False
        ref_span = None          # char span of the sheet ref inside text
        pattern_hit = False      # ref found via dialect pattern (not TO/FROM capture)

        # ── Sheet reference: dialect pattern FIRST ────────────────────────────
        # The drawing number itself (S0000155, 346-0000-001, XFB_40208…) is far
        # more reliable than the word after TILL/FRÅN — Hybrit connectors say
        # "346-0000-001-PS / TILL FACKLA" where FACKLA is equipment, not a sheet.
        if dialect == 'lkab':
            m_rds = _RE_RDS_SHEET.search(text)
            if m_rds:
                rds_code    = m_rds.group(1).upper()
                ref_sheet   = ('S' + m_rds.group(2)).upper()
                ref_span    = m_rds.span()
                lkab_format = pattern_hit = True
            else:
                m2 = re.search(r'\bS(\d{6,8})\b', text, re.I)
                if m2:
                    ref_sheet   = ('S' + m2.group(1)).upper()
                    ref_span    = m2.span()
                    pattern_hit = True

        elif dialect == 'its':
            m_its = _RE_ITS_CONN.search(text)
            if m_its:
                ref_sheet  = m_its.group(1).upper()   # e.g. XFB_40208
                ref_span   = m_its.span()
                its_format = pattern_hit = True
            else:
                m2 = _DIALECTS['its']['sheet_num_re'].search(text)
                if m2:
                    ref_sheet   = m2.group(1).upper()
                    ref_span    = m2.span()
                    pattern_hit = True

        elif dialect == 'gryaab':
            m_gr = _RE_GRYAAB_CONN.search(text)
            if m_gr:
                ref_sheet     = m_gr.group(1).upper()
                ref_span      = m_gr.span()
                gryaab_format = pattern_hit = True

        elif dialect == 'hybrit':
            m2 = _DIALECTS['hybrit']['sheet_num_re'].search(text)
            if m2:
                ref_sheet   = m2.group(1).upper()
                ref_span    = m2.span()
                pattern_hit = True

        else:  # classic / fallback
            m_rds = _RE_RDS_SHEET.search(text)
            if m_rds:
                rds_code    = m_rds.group(1).upper()
                ref_sheet   = ('S' + m_rds.group(2)).upper()
                ref_span    = m_rds.span()
                lkab_format = pattern_hit = True
            else:
                m2 = _RE_SHEET_NUM.search(text)
                if m2:
                    ref_sheet   = m2.group(1).upper().strip()
                    ref_span    = m2.span()
                    pattern_hit = True

        # Fallback: capture the word after TO/FROM/TILL/FRÅN as the reference
        if ref_sheet is None:
            m_kw = _RE_TO_FROM.search(text)
            if m_kw:
                keyword   = m_kw.group(1).upper()
                ref_sheet = m_kw.group(2).upper().strip()
                ref_span  = m_kw.span(2)

        if not ref_sheet:
            return None

        # ── Direction ─────────────────────────────────────────────────────────
        # A TILL/FRÅN keyword decides direction only when it clearly belongs to
        # the reference: first word of the connector text, or directly adjacent
        # to the sheet ref ("TO S0000162", "258-0000-001-PS TILL FACKLA").
        # A keyword inside a service description ("KVÄVE TILL ELFILTER") refers
        # to equipment, not the sheet — there the edge convention decides.
        if keyword is None and ref_span is not None:
            for m_kw in _RE_DIR_KW.finditer(text):
                at_start   = m_kw.start() <= 1
                before_ref = 0 <= ref_span[0] - m_kw.end() <= 2
                after_ref  = 0 <= m_kw.start() - ref_span[1] <= 2
                if at_start or before_ref or after_ref:
                    keyword = m_kw.group(1).upper()
                    break

        kw_upper = (keyword or '').upper().replace('Å', 'A').replace('Ä', 'A')
        if kw_upper in ('TO', "CONT'D", 'CONTD', 'TILL'):
            dir_kw = 'out'
        elif kw_upper in ('FROM', 'FRAN', 'FRAAN'):
            dir_kw = 'in'
        else:
            dir_kw = None

        edge_dir_map = {'right': 'out', 'left': 'in', 'top': 'unknown', 'bottom': 'unknown'}
        direction  = dir_kw or edge_dir_map.get(edge, 'unknown')
        dir_factor = {'out': 1.0, 'in': 1.0, 'unknown': 0.4}.get(direction, 0.4)
        if edge in ('top', 'bottom'):
            dir_factor = 0.5

        # ── Line ID & media ───────────────────────────────────────────────────
        lm = _RE_LINE_ID.search(text)
        ref_line_id = lm.group(1) if lm else None

        media_type = 'unknown'
        for mname, pat in _MEDIA_PATTERNS:
            if pat.search(text):
                media_type = mname
                break

        # ── Confidence ────────────────────────────────────────────────────────
        if lkab_format or its_format:
            conf = 0.85
        elif keyword and pattern_hit:
            conf = 0.90
        elif keyword:
            conf = 0.85          # ref captured via TO/FROM keyword
        elif gryaab_format:
            conf = 0.70
        elif pattern_hit:
            conf = 0.75          # dialect-specific drawing number, no keyword
        else:
            conf = 0.55
        if ref_line_id is None:
            conf -= 0.08
        if ocr_used:
            conf -= 0.10
        if media_type == 'unknown':
            conf -= 0.08
        conf = max(0.10, conf)

        mw = _MEDIA_WEIGHTS.get(media_type, 0.05)
        weight = round(mw * conf * dir_factor, 3)

        import datetime
        return {
            "pid_page": pn,
            "x_pdf": cx, "y_pdf": cy,
            "direction": direction,
            "edge": edge,
            "ref_text": text[:200],
            "ref_sheet": ref_sheet,
            "ref_line_id": ref_line_id,
            "media_type": media_type,
            "weight": weight,
            "confidence": conf,
            "raw_text": text[:500],
            "ocr_used": int(ocr_used),
            "analyzed_at": datetime.datetime.now().isoformat(),
        }

    def _ocr_edges(self, page, pw, ph):
        """OCR all four edges. Returns {edge: text}. Uses 3× scale + preprocessing."""
        if not HAS_PYMUPDF:
            return {}
        strips = {
            'left':   fitz.Rect(0,       0,       pw*0.28, ph),
            'right':  fitz.Rect(pw*0.72, 0,       pw,      ph),
            'top':    fitz.Rect(0,       0,       pw,      ph*0.22),
            'bottom': fitz.Rect(0,       ph*0.78, pw,      ph),
        }
        mat = fitz.Matrix(3.0, 3.0)
        result = {}
        for edge, clip in strips.items():
            try:
                pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
                text = self._ocr_strip(pix)
                if text:
                    result[edge] = text
            except Exception:
                pass
        return result

    def _ocr_strip(self, pix):
        """OCR one pixmap strip. Tries tesseract (multi-PSM + preprocessing) then easyocr."""
        try:
            from PIL import Image as _PILImg, ImageEnhance, ImageFilter
            pil_img = _PILImg.frombytes("RGB", [pix.width, pix.height], pix.samples)
            gray    = pil_img.convert('L')
            gray    = ImageEnhance.Contrast(gray).enhance(2.5)
            gray    = gray.filter(ImageFilter.SHARPEN)

            try:
                import pytesseract
                for psm in (6, 11, 3):
                    text = pytesseract.image_to_string(
                        gray, config=f'--psm {psm} --oem 1').strip()
                    if text:
                        return text
            except Exception:
                pass

            # easyocr fallback (uses centralized lifecycle manager)
            try:
                reader = _get_easyocr_reader()
                if reader is not None:
                    hits = reader.readtext(pil_img,
                                          detail=0,
                                          paragraph=True)
                    text = ' '.join(hits).strip()
                    if text:
                        return text
            except Exception:
                pass
        except Exception:
            pass
        return ''

    def _match_connections(self, connectors, sheet_lookup, page_count,
                          page_sheet_nums=None):
        """Build page→page connections from BOTH ends of each documented flow.

        A physical flow A→B is documented twice on the drawings: an OUT
        connector on sheet A referencing B, and an IN connector on sheet B
        referencing A.  Both ends are used — if extraction missed one end the
        other still creates the connection, and when both were found the
        confidence is boosted (flow confirmed from both sheets).
        """
        # Build own-sheet reverse map: pn → own sheet num (upper)
        own_sheet = {}
        if page_sheet_nums:
            own_sheet = {int(k): v.upper() for k, v in page_sheet_nums.items()}

        def resolve_page(ref):
            for v in _sheet_ref_variants(ref):
                if v in sheet_lookup:
                    return sheet_lookup[v]
            # Fuzzy: try suffix match
            for k, v in sheet_lookup.items():
                if k.endswith(ref) or ref.endswith(k):
                    return v
            return None

        flows = {}   # ('ghost', page, ref) or (from_page, to_page) → record

        for i, c in enumerate(connectors):
            c['_idx'] = i
            ref = (c.get('ref_sheet') or '').upper().strip()
            if not ref:
                continue
            # Skip self-referential connectors (sheet references its own number)
            own = own_sheet.get(c['pid_page'], '__NONE__')
            if ref in _sheet_ref_variants(own):
                continue

            tp = resolve_page(ref)
            # Store resolved page directly on the connector so draw code can
            # look up by (pid_page, ref_page) without the sheet_num_map
            c['ref_page'] = tp

            if tp is None:
                key = ('ghost', c['pid_page'], ref)
                rec = flows.get(key)
                if rec is None:
                    flows[key] = {
                        'from_page': c['pid_page'], 'to_page': None,
                        'from_connector': c['_idx'], 'to_connector': None,
                        'media_type': c['media_type'], 'weight': c['weight'],
                        'confidence': c['confidence'], 'is_bidirectional': 0,
                        'is_ghost': 1, 'ghost_ref': ref, 'warning': None,
                        'from_edge': c.get('edge'), 'to_edge': None,
                        'n_out': 0, 'n_in': 0,
                    }
                else:
                    rec['weight'] = round(
                        1.0 - (1.0 - rec['weight']) * (1.0 - c['weight']), 3)
                continue

            if tp == c['pid_page']:
                continue   # recirculation on the same sheet

            # IN connector on B referencing A means flow A→B
            is_in = (c['direction'] == 'in')
            a, b = (tp, c['pid_page']) if is_in else (c['pid_page'], tp)

            rec = flows.get((a, b))
            if rec is None:
                rec = flows[(a, b)] = {
                    'from_page': a, 'to_page': b,
                    'from_connector': None, 'to_connector': None,
                    'media_type': None, 'weight': 0.0, 'confidence': 0.0,
                    'is_bidirectional': 0, 'is_ghost': 0, 'ghost_ref': None,
                    'warning': None, 'from_edge': None, 'to_edge': None,
                    'n_out': 0, 'n_in': 0,
                }
            if is_in:
                rec['n_in'] += 1
                if rec['to_connector'] is None:
                    rec['to_connector'] = c['_idx']
                    rec['to_edge']      = c.get('edge')
            else:
                rec['n_out'] += 1
                if rec['from_connector'] is None:
                    rec['from_connector'] = c['_idx']
                    rec['from_edge']      = c.get('edge')
            # Accumulate weight: w = 1 - (1-w_old)*(1-w_new)
            rec['weight']     = round(
                1.0 - (1.0 - rec['weight']) * (1.0 - c['weight']), 3)
            rec['confidence'] = max(rec['confidence'], c['confidence'])
            if rec['media_type'] in (None, 'unknown') and c['media_type']:
                rec['media_type'] = c['media_type']

        connections = list(flows.values())
        real_keys = {(r['from_page'], r['to_page'])
                     for r in connections if not r['is_ghost']}
        for r in connections:
            if r['media_type'] is None:
                r['media_type'] = 'unknown'
            if r['is_ghost']:
                continue
            if r['n_out'] and r['n_in']:
                # Flow documented on both sheets — high trust
                r['confidence'] = round(min(0.99, r['confidence'] + 0.08), 3)
            if r['n_out'] > 1 or r['n_in'] > 1:
                r['warning'] = 'multiple'
            if (r['to_page'], r['from_page']) in real_keys:
                r['is_bidirectional'] = 1

        return connections


def _propose_layout(connections, active_pages, page_widths_pdf, page_heights_pdf, render_scale):
    """Layered process-flow layout (Sugiyama-style).

    P&ID sheets follow the convention that flow enters on the left and leaves
    on the right.  The board is arranged the same way so a human reads it like
    the process itself: every sheet sits one column to the right of the sheets
    that feed it, parallel branches stack vertically, and connected sheets are
    aligned so the connection lines run as straight as possible.

    Per connected component:
      1. break cycles            (DFS back-edge removal; recirculation loops)
      2. longest-path layering   → column per sheet (vertical-edge links may
                                   share a column and stack instead)
      3. barycenter sweeps       → row order within each column
      4. alignment passes        → connected sheets line up horizontally
    Very long chains wrap serpentine-style into bands.  Components are stacked
    below each other, largest first; sheets without any connections are placed
    in a grid at the bottom.
    """
    import math
    from collections import deque, defaultdict

    if not active_pages:
        return {}

    rs     = render_scale
    H_GAP  = 650.0    # gap between columns (scene px)
    V_GAP  = 420.0    # gap between sheets in a column
    C_GAP  = 1300.0   # gap between disconnected components
    MARGIN = 300.0
    page_set = set(active_pages)
    ws = {i: page_widths_pdf.get(i,  800) * rs for i in active_pages}
    hs = {i: page_heights_pdf.get(i, 600) * rs for i in active_pages}

    # ── Directed edges between active pages ───────────────────────────────────
    edge_w   = {}     # (a, b) → max weight
    vertical = set()  # pairs connected via top/bottom connectors
    for c in connections:
        a, b = c.get('from_page'), c.get('to_page')
        if a not in page_set or b is None or b not in page_set or a == b:
            continue
        w = float(c.get('weight', 0.5) or 0.5)
        edge_w[(a, b)] = max(edge_w.get((a, b), 0.0), w)
        if (c.get('from_edge') in ('top', 'bottom')
                or c.get('to_edge') in ('top', 'bottom')):
            vertical.add((a, b))

    # For mutual pairs (A⇄B) keep only the dominant direction for layering
    drop = set()
    for (a, b), w in edge_w.items():
        if (b, a) in edge_w and (a, b) not in drop and (b, a) not in drop:
            wr = edge_w[(b, a)]
            drop.add((a, b) if (wr > w or (wr == w and b < a)) else (b, a))

    succ = defaultdict(set)
    und  = defaultdict(set)
    for (a, b) in edge_w:
        und[a].add(b); und[b].add(a)
        if (a, b) not in drop:
            succ[a].add(b)

    # ── Utility hubs ───────────────────────────────────────────────────────────
    # Collection sheets (flare/vent header, effluent, drain) are wired to a
    # large share of the plant.  Kept in the flow they pull long lines through
    # everything; humans park them in a row below the process — so do we.
    und_full  = {i: set(und[i]) for i in active_pages}
    n_connected = sum(1 for i in active_pages if und[i])
    hub_thresh  = max(7, int(n_connected * 0.10))
    hubs = [i for i in sorted(active_pages) if len(und[i]) >= hub_thresh]
    hub_set = set(hubs)
    for h in hubs:
        for nb in list(und[h]):
            und[nb].discard(h)
            succ[nb].discard(h)
        und[h]  = set()
        succ[h] = set()

    def med(vals):
        vals = sorted(vals)
        m = len(vals) // 2
        return vals[m] if len(vals) % 2 else 0.5 * (vals[m - 1] + vals[m])

    def delta(a, b):
        # Vertical connectors may share a column (stacked); horizontal flow
        # always advances one column to the right.
        return 0 if (a, b) in vertical else 1

    # ── Connected components, largest first ───────────────────────────────────
    comps, seen = [], set()
    for start in sorted(active_pages):
        if start in seen or not und[start]:
            continue
        comp, q = [], deque([start])
        seen.add(start)
        while q:
            n = q.popleft()
            comp.append(n)
            for nb in sorted(und[n]):
                if nb not in seen:
                    seen.add(nb)
                    q.append(nb)
        comps.append(sorted(comp))
    comps.sort(key=len, reverse=True)
    isolated = [i for i in sorted(active_pages)
                if not und[i] and i not in hub_set]

    pos = {}              # page → (x_topleft, y_topleft)
    cursor_y = MARGIN

    for comp in comps:
        cset = set(comp)
        dag  = {n: sorted(s for s in succ[n] if s in cset) for n in comp}

        # ── 1. Break cycles: greedy feedback-arc-set ordering (Eades) ─────────
        # Arrange the sheets in a linear order with as few edges as possible
        # pointing backwards; the backward edges are the recycle/return lines
        # and are cut for layering, so the MAIN flow always runs left→right.
        # (Plain DFS back-edge removal can cut the main line instead and push
        # the whole feed chain deep to the right.)
        g_succ = {n: set(dag[n]) for n in comp}
        g_pred = defaultdict(set)
        for n, ss in g_succ.items():
            for s in ss:
                g_pred[s].add(n)
        remaining = set(comp)
        s1, s2 = [], []
        while remaining:
            changed = True
            while changed:
                changed = False
                for n in sorted(remaining):           # sinks → right end
                    if not (g_succ[n] & remaining):
                        s2.append(n); remaining.discard(n); changed = True
                for n in sorted(remaining):           # sources → left end
                    if n in remaining and not (g_pred[n] & remaining):
                        s1.append(n); remaining.discard(n); changed = True
            if remaining:
                # Strongest net outflow first; page number breaks ties —
                # sheet numbering normally follows the process order.
                def net_out(n):
                    o = sum(edge_w.get((n, s), 0.5) for s in g_succ[n] & remaining)
                    i = sum(edge_w.get((p, n), 0.5) for p in g_pred[n] & remaining)
                    return o - i
                n = max(sorted(remaining), key=net_out)
                s1.append(n); remaining.discard(n)
        order_idx = {n: i for i, n in enumerate(s1 + list(reversed(s2)))}
        back = {(a, b) for a in comp for b in dag[a]
                if order_idx[a] > order_idx[b]}
        fwd   = {n: [s for s in dag[n] if (n, s) not in back] for n in comp}
        fpred = defaultdict(list)
        for n, ss in fwd.items():
            for s in ss:
                fpred[s].append(n)

        # ── 2. Longest-path layering (relaxation; components are small) ───────
        layer = {n: 0 for n in comp}
        for _ in range(len(comp)):
            changed = False
            for n in comp:
                for s in fwd[n]:
                    need = layer[n] + delta(n, s)
                    if layer[s] < need:
                        layer[s] = need
                        changed = True
            if not changed:
                break
        # Pull pure feeders right, next to their first consumer, so utility
        # sheets don't all pile up in column 0 with long lines across the board
        for n in sorted(comp, key=lambda v: -layer[v]):
            if fwd[n] and not fpred[n]:
                layer[n] = min(layer[s] - delta(n, s) for s in fwd[n])

        # ── 3. Serpentine wrap for very long chains ───────────────────────────
        max_cols = max(8, math.ceil(math.sqrt(len(comp)) * 2.2))
        band, col = {}, {}
        for n in comp:
            b  = layer[n] // max_cols
            c_ = layer[n] % max_cols
            if b % 2 == 1:                 # odd bands run right→left so the
                c_ = max_cols - 1 - c_     # chain stays adjacent at the fold
            band[n], col[n] = b, c_

        # ── 4. Row order within each column: barycenter sweeps ────────────────
        cols = defaultdict(list)
        for n in comp:
            cols[(band[n], col[n])].append(n)
        order = {}
        for key in cols:
            cols[key].sort()
            for i, n in enumerate(cols[key]):
                order[n] = i
        col_keys = sorted(cols.keys())
        for sweep in range(6):
            keys = col_keys if sweep % 2 == 0 else list(reversed(col_keys))
            for key in keys:
                members = cols[key]

                def bary(n, _key=key):
                    nbs = [order[m] for m in und[n]
                           if m in cset and (band[m], col[m]) != _key]
                    return med(nbs) if nbs else float(order[n])

                members.sort(key=lambda n: (bary(n), order[n]))
                for i, n in enumerate(members):
                    order[n] = i
        # Vertical links in the same column: source stacks above target
        for (a, b) in vertical:
            if (a in cset and b in cset
                    and (band[a], col[a]) == (band[b], col[b])
                    and order[a] > order[b]):
                members = cols[(band[a], col[a])]
                members[order[a]], members[order[b]] = \
                    members[order[b]], members[order[a]]
                order[a], order[b] = order[b], order[a]

        # ── 5. Coordinates ─────────────────────────────────────────────────────
        band_top = cursor_y
        for b in range(max(band.values()) + 1):
            bcols = sorted(c2 for (bb, c2) in cols if bb == b)
            x = MARGIN
            col_x, col_w = {}, {}
            for c2 in bcols:
                wmax = max(ws[n] for n in cols[(b, c2)])
                col_x[c2], col_w[c2] = x, wmax
                x += wmax + H_GAP
            # initial y: stack in barycenter order
            cy = {}
            for c2 in bcols:
                yy = band_top
                for n in sorted(cols[(b, c2)], key=lambda n: order[n]):
                    cy[n] = yy + hs[n] / 2
                    yy += hs[n] + V_GAP
            # alignment passes: move toward neighbour average, keep order,
            # resolve overlaps top-down
            for _pass in range(5):
                for c2 in bcols:
                    members = sorted(cols[(b, c2)], key=lambda n: order[n])
                    desired = []
                    for n in members:
                        nbs = [cy[m] for m in und[n]
                               if m in cy and (band[m], col[m]) != (b, c2)]
                        desired.append(med(nbs) if nbs else cy[n])
                    prev_bottom = None
                    for n, d in zip(members, desired):
                        top = d - hs[n] / 2
                        if prev_bottom is None:
                            top = max(top, band_top)
                        elif top < prev_bottom + V_GAP:
                            top = prev_bottom + V_GAP
                        cy[n] = top + hs[n] / 2
                        prev_bottom = top + hs[n]
            band_nodes = [n for n in comp if band[n] == b]
            for n in band_nodes:
                c2 = col[n]
                px = col_x[c2] + (col_w[c2] - ws[n]) / 2   # centred in column
                pos[n] = (px, cy[n] - hs[n] / 2)
            band_top = max(pos[n][1] + hs[n] for n in band_nodes) + C_GAP * 0.7
        cursor_y = band_top - C_GAP * 0.7 + C_GAP

    # ── 6. Utility-hub row below the process flow ──────────────────────────────
    if hubs:
        hub_y, row_h = cursor_y, 0.0
        entries = []
        for h in hubs:
            nb_x = [pos[m][0] + ws[m] / 2 for m in und_full[h] if m in pos]
            entries.append((med(nb_x) if nb_x else MARGIN, h))
        entries.sort()
        x_next = MARGIN
        for cx, h in entries:
            x = max(x_next, cx - ws[h] / 2)
            pos[h] = (x, hub_y)
            x_next = x + ws[h] + H_GAP
            row_h  = max(row_h, hs[h])
        cursor_y = hub_y + row_h + C_GAP

    # ── 7. Isolated sheets: grid at the bottom ─────────────────────────────────
    if isolated:
        board_right = max((pos[n][0] + ws[n] for n in pos), default=MARGIN)
        typ_w = sum(ws.values()) / len(ws)
        target_w = max(board_right, 3 * (typ_w + H_GAP) + MARGIN)
        x, y, row_h = MARGIN, cursor_y, 0.0
        for i in isolated:
            if x > MARGIN and x + ws[i] > target_w:
                x = MARGIN
                y += row_h + V_GAP
                row_h = 0.0
            pos[i] = (x, y)
            x += ws[i] + H_GAP
            row_h = max(row_h, hs[i])

    return {i: (round(pos[i][0]), round(pos[i][1])) for i in active_pages}


class EquipmentAnalysisWorker(QThread):
    """Runs detect_equipment_and_valves() off the UI thread, modelled
    exactly on ConnectorAnalyzer above: opens its own fitz.open() in run(),
    NEVER touches a Database/sqlite3 connection (sqlite3 defaults to
    check_same_thread=True — the caller does all DB reads/writes in the
    main-thread slot connected to finished_analysis, after this thread has
    already stopped), and always emits finished_analysis even on an
    exception or cancellation so the caller's modal progress dialog can
    never hang.

    tag_points is built entirely on the calling (UI) thread before
    construction — from the Utrustningsregister's already-in-memory rows,
    or from a scan_result via scan_result_to_tag_points() — so this class
    never needs any callback into GUI/DB code from inside run().
    """
    progress          = pyqtSignal(int, int, str)   # (page_num, total, msg) — same
                                                      # contract detect_equipment_and_valves'
                                                      # progress_callback already uses
    finished_analysis = pyqtSignal(list, list)       # (results, rejected) — ALWAYS emitted

    def __init__(self, pdf_path, tag_points, pages=None,
                 min_bowtie_score=0.5, min_confidence=0.3, parent=None):
        super().__init__(parent)
        self._pdf_path         = str(pdf_path)
        self._tag_points       = list(tag_points)
        self._pages            = list(pages) if pages is not None else None
        self._min_bowtie_score = min_bowtie_score
        self._min_confidence   = min_confidence

    def run(self):
        doc = None
        try:
            doc = fitz.open(self._pdf_path)
            results, rejected = detect_equipment_and_valves(
                doc, self._tag_points, pages=self._pages,
                min_bowtie_score=self._min_bowtie_score,
                min_confidence=self._min_confidence,
                progress_callback=lambda pn, total, msg: self.progress.emit(pn, total, msg),
                should_cancel=self.isInterruptionRequested)
            self.finished_analysis.emit(results, rejected)
        except Exception as e:
            import logging
            logging.error(f"EquipmentAnalysisWorker.run() failed: {e}", exc_info=True)
            self.finished_analysis.emit([], [])
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass


class EquipmentTagSearchWorker(QThread):
    """Finds a tag for a freshly placed equipment object off the UI
    thread (2026-08-18, see NOTES.md "kombinerad placeringsmeny") —
    opens its own fitz.Document, modelled exactly on
    EquipmentAnalysisWorker above (NEVER shares the live viewer's
    pdf_doc across threads), and always emits finished_search exactly
    once, even on failure, so the caller's spinner/timeout logic in
    EquipmentPlacementPopup can never hang waiting.

    A rubber-band placement passes `rect` — native text + OCR strictly
    INSIDE it (equipment_detection.extract_tag_from_rect), with NO
    fallback to a nearby-point/whole-page search if that comes up empty
    (2026-08-24, see NOTES.md "Åtta UX/logik-förbättringar" — a
    find_tag_near_point fallback used to run here, but on P&IDs where a
    tag's text is missing or sits far from its symbol it grabbed whatever
    tag-like text happened to be nearest on the page, producing wrong
    objects/tag numbers for a rectangle the user explicitly drew to
    contain the right one). A plain right-click placement has only a
    point to search from, so it passes `point` instead and keeps the
    point-radius-then-whole-page search — that path was never drawn by
    the user around a specific area, so there's no boundary to violate."""
    finished_search = pyqtSignal(str)   # detected tag, '' if none found

    def __init__(self, pdf_path, page, rect=None, point=None, radius=100, parent=None):
        super().__init__(parent)
        self._pdf_path = str(pdf_path)
        self._page     = page
        self._rect     = rect      # (x0, y0, x1, y1) or None
        self._point    = point     # (x, y) or None
        self._radius   = radius

    def run(self):
        doc = None
        try:
            doc = fitz.open(self._pdf_path)
            tag = ''
            if self._rect is not None:
                x0, y0, x1, y1 = self._rect
                tag = extract_tag_from_rect(doc, self._page, x0, y0, x1, y1)
            elif self._point is not None:
                x, y = self._point
                tag = find_tag_near_point(doc, self._page, x, y, radius=self._radius)
            self.finished_search.emit(tag or '')
        except Exception as e:
            import logging
            logging.error(f"EquipmentTagSearchWorker.run() failed: {e}", exc_info=True)
            self.finished_search.emit('')
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass


class SimilarSymbolSearchWorker(QThread):
    """Runs equipment_detection._scan_candidates() off the UI thread for
    SimilarSymbolSearchDialog (2026-08-15, see NOTES.md "Hitta liknande
    symbol" — uppföljningsfunktioner), modelled exactly on
    EquipmentAnalysisWorker above: opens its own fitz.open() in run(),
    never touches Database, always emits finished_scan even on an
    exception or cancellation (self.requestInterruption() + waiting for
    this to finish is how the dialog cancels a running scan) so the
    dialog's inline progress UI can never hang.

    Returns the RAW, unthresholded candidate list — the dialog applies
    min_similarity itself, both for the live match-count/on-canvas
    preview as the threshold slider moves and again when the search is
    finally confirmed, without ever re-scanning the document.

    page_rotations (2026-08-15, see NOTES.md "Hitta liknande symbol
    placerar fel — sidrotation"): {physical_page: extra_degrees}, as
    Database.get_all_page_rotations() returns — this worker's own
    fitz.open() never goes through PIDGraphicsView's manual-rotation-
    override machinery, so without re-applying it here, a page with an
    override gets its geometry computed in the wrong coordinate space
    entirely, silently misplacing every result found on/via that page
    even though the shape matching itself still works correctly."""
    progress      = pyqtSignal(int, int, str)   # (page_num, total, msg)
    finished_scan = pyqtSignal(list)             # ALWAYS emitted

    def __init__(self, pdf_path, ref_features, ref_page, ref_native_index_group,
                 pages=None, ignore_scale=False, rotation_mode='none',
                 page_rotations=None, parent=None):
        super().__init__(parent)
        self._pdf_path              = str(pdf_path)
        self._ref_features          = ref_features
        self._ref_page              = ref_page
        self._ref_native_index_group = list(ref_native_index_group)
        self._pages                 = list(pages) if pages is not None else None
        self._ignore_scale          = ignore_scale
        self._rotation_mode         = rotation_mode
        self._page_rotations        = dict(page_rotations) if page_rotations else {}

    def run(self):
        doc = None
        try:
            doc = fitz.open(self._pdf_path)
            equipment_detection.apply_page_rotations(doc, self._page_rotations)
            candidates = equipment_detection._scan_candidates(
                doc, self._ref_features, self._ref_page, self._ref_native_index_group,
                pages=self._pages, rotation_mode=self._rotation_mode,
                ignore_scale=self._ignore_scale,
                progress_callback=lambda pn, total, msg: self.progress.emit(pn, total, msg),
                should_cancel=self.isInterruptionRequested)
            self.finished_scan.emit(candidates)
        except Exception as e:
            logging.error(f"SimilarSymbolSearchWorker.run() failed: {e}", exc_info=True)
            self.finished_scan.emit([])
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass


class ImageSymbolSearchWorker(QThread):
    """Image/pixel-based counterpart to SimilarSymbolSearchWorker above
    (2026-08-15, see NOTES.md "Bildbaserad 'hitta liknande symbol' — vid
    sidan av vektorlogiken") — same structure exactly (own fitz.open(),
    never touches Database, always emits finished_scan even on an
    exception or cancellation), calling
    image_symbol_matching.find_similar_shapes_visual() instead of
    equipment_detection._scan_candidates(). Returns the same
    (sim, page_num, x, y, outline) tuple contract, so
    SimilarSymbolSearchDialog's threshold/count/preview code needs no
    branching of its own to handle either matching method's results.

    page_rotations — same purpose/reasoning as
    SimilarSymbolSearchWorker's own parameter above: this worker's own
    fitz.open() never sees PIDGraphicsView's manual rotation overrides,
    so ref_bbox itself (already in the LIVE view's rotated coordinate
    space) would be cropped from the WRONG region of a freshly-opened,
    un-rotated page, and any found candidate's outline would land in
    the wrong space too.

    Parallel across CPU-core PROCESSES for "Hela dokumentet" (2026-08-16,
    see NOTES.md "raster-sökning — parallellisering över flera
    processer") — Bildmatchning renders every candidate page at 300 DPI
    and runs cv2.matchTemplate across every (scale, rotation)
    combination, which measured 5-6 seconds for ONE large-format (A0)
    page at default settings and up to 35s with "alla storlekar" on —
    purely sequential before this, so a multi-page document search took
    that times the page count. Modelled EXACTLY on
    ParallelTagScanWorker/_scan_page_range_worker above/in
    equipment_detection.py: _should_parallelize gates whether it's worth
    the ProcessPoolExecutor startup cost at all, _pick_worker_count picks
    how many processes (reused with use_ocr=False — Bildmatchning has no
    OCR engine to budget threads for, so that parameter is simply
    inert), each process opens its OWN fitz.Document and independently
    re-renders+re-matches the reference template
    (image_symbol_matching._match_page_range_worker — Document/Pixmap/
    cv2 state can't cross a process boundary, so there's no cheaper way
    to hand a live template across than to just rebuild it), and only
    already-PENDING (not yet started) chunks can be cancelled — same
    tradeoff the existing tag-scan parallelization already accepts."""
    progress      = pyqtSignal(int, int, str)
    finished_scan = pyqtSignal(list)

    def __init__(self, pdf_path, ref_page, ref_bbox, pages=None,
                 ignore_scale=False, rotation_mode='none',
                 page_rotations=None, parent=None):
        super().__init__(parent)
        self._pdf_path     = str(pdf_path)
        self._ref_page     = ref_page
        self._ref_bbox     = tuple(ref_bbox)
        self._pages        = list(pages) if pages is not None else None
        self._ignore_scale = ignore_scale
        self._rotation_mode = rotation_mode
        self._page_rotations = dict(page_rotations) if page_rotations else {}

    def run(self):
        doc = None
        try:
            doc = fitz.open(self._pdf_path)
            pages = self._pages if self._pages is not None else list(range(doc.page_count))
        except Exception as e:
            logging.error(f"ImageSymbolSearchWorker.run() failed to open PDF: {e}", exc_info=True)
            self.finished_scan.emit([])
            return
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

        n_workers = _pick_worker_count(len(pages), use_ocr=False, ocr_engine='auto')
        if not _should_parallelize(len(pages), n_workers):
            self._run_sequential(pages)
            return
        try:
            self._run_parallel(pages, n_workers)
        except Exception as e:
            logging.error(f"ImageSymbolSearchWorker.run() parallel path failed, "
                          f"falling back to sequential: {e}", exc_info=True)
            try:
                self._run_sequential(pages)
            except Exception as e2:
                logging.error(f"ImageSymbolSearchWorker: sequential fallback also "
                              f"failed: {e2}", exc_info=True)
                self.finished_scan.emit([])

    def _run_sequential(self, pages):
        doc = None
        try:
            doc = fitz.open(self._pdf_path)
            equipment_detection.apply_page_rotations(doc, self._page_rotations)
            candidates = image_symbol_matching.find_similar_shapes_visual(
                doc, self._ref_page, self._ref_bbox, pages=pages,
                ignore_scale=self._ignore_scale, rotation_mode=self._rotation_mode,
                progress_callback=lambda pn, total, msg: self.progress.emit(pn, total, msg),
                should_cancel=self.isInterruptionRequested)
            self.finished_scan.emit(candidates)
        except Exception as e:
            logging.error(f"ImageSymbolSearchWorker._run_sequential failed: {e}", exc_info=True)
            self.finished_scan.emit([])
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

    def _run_parallel(self, pages, n_workers):
        total = len(pages)
        chunks = [[pages[i] for i in idx_chunk]
                  for idx_chunk in equipment_detection._split_into_chunks(total, n_workers)]
        all_candidates = []
        cancelled = False
        manager = multiprocessing.Manager()
        progress_queue = manager.Queue()
        # A cross-process cancellation signal (2026-08-16, see NOTES.md
        # "raster-sökning" follow-up) — a QThread's own
        # isInterruptionRequested is a bound method and can't cross a
        # process boundary, but a Manager-backed Event can. Without this,
        # cancelling PENDING futures below only stops chunks that hadn't
        # started yet; an already-dispatched chunk ran to full completion
        # with ZERO cancellation checking at all inside it (unlike the
        # sequential path, which checks should_cancel once per scale AND
        # once per rotation — see _match_page). Confirmed directly: on a
        # 4-page, A0-sized "Hela dokumentet" scan with "Alla storlekar",
        # requesting cancellation while workers were mid-flight left
        # worker.wait() blocking the UI thread for 63 SECONDS — the exact
        # "hang reads as a crash" complaint this whole investigation
        # started from, just relocated to the new parallel path instead
        # of fixed by it. _match_page_range_worker threads this through
        # to _match_pages' existing should_cancel parameter, so an
        # already-running worker now notices at its next scale/rotation
        # boundary instead of running its entire remaining chunk.
        cancel_event = manager.Event()
        try:
            executor = concurrent.futures.ProcessPoolExecutor(max_workers=len(chunks))
            try:
                futures = [executor.submit(
                    image_symbol_matching._match_page_range_worker,
                    self._pdf_path, self._ref_page, self._ref_bbox, chunk,
                    # min_similarity: the sequential path (find_similar_shapes_visual)
                    # never overrides this either, relying on its own 0.6 default —
                    # NOT truly "unthresholded" the way the vector path's
                    # _scan_candidates is. Confirmed directly: passing 0.0 here
                    # makes cv2.matchTemplate's own np.where(result >= 0.0) match
                    # nearly every pixel position on the page (correlation scores
                    # cluster near/above 0 almost everywhere), producing so many
                    # raw (score, bbox) pairs that _nms's greedy suppression alone
                    # made a single page hang for minutes. Kept in parity with the
                    # sequential path rather than "fixed" to true zero.
                    image_symbol_matching._DEFAULT_MIN_SIMILARITY_FOR_SCAN,
                    self._ignore_scale, self._rotation_mode,
                    image_symbol_matching._DEFAULT_DPI, self._page_rotations,
                    progress_queue, len(chunks), cancel_event)
                    for chunk in chunks]
                pending = set(futures)
                while pending:
                    self._emit_progress(progress_queue, total)
                    if self.isInterruptionRequested():
                        cancelled = True
                        cancel_event.set()
                        for f in pending:
                            f.cancel()
                        break
                    _done, pending = concurrent.futures.wait(
                        pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
                self._emit_progress(progress_queue, total)
                if not cancelled:
                    for f in futures:
                        try:
                            all_candidates.extend(f.result())
                        except concurrent.futures.CancelledError:
                            continue
                        except Exception as e:
                            logging.error(f"ImageSymbolSearchWorker: a worker "
                                          f"process failed: {e}", exc_info=True)
                            continue
            finally:
                # wait=True even when cancelled: cancel_futures=True skips
                # any not-yet-STARTED task, but a worker process already
                # spawned/mid-startup at cancel time keeps running and
                # reaches for the manager's queue proxy — tearing down
                # `manager` before that process finishes connecting to it
                # orphans it with a noisy WinError/FileNotFoundError
                # traceback (confirmed directly: an earlier wait=not
                # cancelled version of this reproduced exactly that on a
                # real cancellation test). Same fix already applied to
                # ParallelTagScanWorker._run_parallel above for the exact
                # same reason — bounds "Avbryt" latency to one in-flight
                # chunk's remaining work, not instant, but always clean.
                executor.shutdown(wait=True, cancel_futures=cancelled)
        finally:
            manager.shutdown()
        if cancelled:
            self.finished_scan.emit([])
            return
        self.finished_scan.emit(all_candidates)

    def _emit_progress(self, progress_queue, total):
        def _emit(page_num, status):
            msg = f"Sida {page_num + 1}/{total} — bildmatchning…" if status == 'running' else ''
            if status == 'running':
                self.progress.emit(page_num, total, msg)
        _drain_progress_queue(progress_queue, _emit)


# ══════════════════════════════════════════════════════════════════════════
# Multi-core parallel analysis (2026-08-07) — see NOTES.md "Flerkärnig
# parallellisering av Analysera P&ID". Neither scan_pdf_for_equipment() nor
# detect_equipment_and_valves() previously used more than one CPU core —
# the latter already ran off the UI thread (EquipmentAnalysisWorker above)
# but still one page at a time. Both iterate pages independently (confirmed
# via code review), so both can be split across several OS PROCESSES
# (not threads — the GIL would otherwise serialize this CPU-bound work
# anyway) using the equipment_detection.py module-level worker functions
# (_scan_page_range_worker / _analyze_page_range_worker — picklable, so
# they survive Windows' 'spawn' start method, and Qt-free, so they're safe
# to run in a child process that never touches the GUI).
# ══════════════════════════════════════════════════════════════════════════

def _pick_worker_count(n_pages, use_ocr, ocr_engine):
    """How many worker PROCESSES to use for a parallel scan/analysis.
    Leaves one core for the UI thread. Capped further when EasyOCR is (or
    would be, under 'auto') the active engine specifically — each process
    loads its own ~1GB model and pays EasyOCR's own ~4-6s torch import
    cost (see NOTES.md "Uppstartsprestanda — lat OCR-import"), so spinning
    up as many EasyOCR workers as CPU cores risks gigabytes of duplicated
    RAM for little extra speed. RapidOCR/Tesseract are cheap enough not to
    need this extra cap."""
    cpu_n = os.cpu_count() or 4
    base = max(1, cpu_n - 1)
    if use_ocr:
        resolved = ocr_engine
        if resolved == 'auto':
            st = ocr_status()
            resolved = ('rapidocr' if st.get('rapidocr') else
                        'tesseract' if st.get('tesseract') else
                        'easyocr' if st.get('easyocr') else None)
        if resolved == 'easyocr':
            base = min(base, 3)
    return min(base, n_pages) if n_pages else base


def _should_parallelize(n_pages, n_workers):
    """Below this, ProcessPoolExecutor/model-load startup cost would
    likely exceed any speedup — run the proven single-process path
    instead."""
    return n_pages >= 4 and n_workers >= 2


def _drain_progress_queue(progress_queue, emit_fn):
    """Pull every currently-available (page_num, status) tuple off a
    multiprocessing progress queue and forward it via emit_fn — shared by
    ParallelTagScanWorker and ParallelEquipmentAnalysisWorker below."""
    try:
        while True:
            page_num, status = progress_queue.get_nowait()
            emit_fn(page_num, status)
    except Exception:
        pass


class ParallelTagScanWorker(QThread):
    """Runs scan_pdf_for_equipment() off the UI thread, across multiple
    CPU-core PROCESSES when the document is large enough to be worth it
    (_should_parallelize) — otherwise falls back to running the exact same
    scan_pdf_for_equipment() call directly, on this thread. Modelled on
    EquipmentAnalysisWorker: every worker process opens its OWN
    fitz.Document (Documents can't cross a process boundary), NEVER
    touches Database/sqlite, and finished_scan is ALWAYS emitted — on
    success, on any exception, or on cancellation (as an empty dict, same
    as today's "Avbryt" during the old synchronous scan silently
    discarding whatever had been found so far).

    page_progress(page_num, status) — status is 'running' or 'done', for
    PageProgressDialog. Fired for every page regardless of which path ran.
    finished_scan(dict) — the scan_pdf_for_equipment()-shaped result.
    """
    page_progress = pyqtSignal(int, str)
    finished_scan = pyqtSignal(dict)

    def __init__(self, pdf_path, use_ocr, ocr_engine='auto', parent=None):
        super().__init__(parent)
        self._pdf_path   = str(pdf_path)
        self._use_ocr    = use_ocr
        self._ocr_engine = ocr_engine

    def run(self):
        doc = None
        try:
            doc = fitz.open(self._pdf_path)
            n_pages = doc.page_count
        except Exception as e:
            logging.error(f"ParallelTagScanWorker.run() failed to open PDF: {e}", exc_info=True)
            self.finished_scan.emit({})
            return
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

        n_workers = _pick_worker_count(n_pages, self._use_ocr, self._ocr_engine)
        if not _should_parallelize(n_pages, n_workers):
            self._run_sequential(n_pages)
            return
        try:
            self._run_parallel(n_pages, n_workers)
        except Exception as e:
            logging.error(f"ParallelTagScanWorker.run() parallel path failed, "
                          f"falling back to sequential: {e}", exc_info=True)
            try:
                self._run_sequential(n_pages)
            except Exception as e2:
                logging.error(f"ParallelTagScanWorker: sequential fallback also "
                              f"failed: {e2}", exc_info=True)
                self.finished_scan.emit({})

    def _run_sequential(self, n_pages):
        doc = None
        last_page = [None]

        def _cb(page_num, _total, _msg):
            if last_page[0] is not None and last_page[0] != page_num:
                self.page_progress.emit(last_page[0], 'done')
            self.page_progress.emit(page_num, 'running')
            last_page[0] = page_num

        try:
            doc = fitz.open(self._pdf_path)
            result = scan_pdf_for_equipment(
                doc, use_ocr=self._use_ocr, ocr_engine=self._ocr_engine, progress_callback=_cb)
            if last_page[0] is not None:
                self.page_progress.emit(last_page[0], 'done')
            self.finished_scan.emit(result)
        except Exception as e:
            logging.error(f"ParallelTagScanWorker._run_sequential failed: {e}", exc_info=True)
            self.finished_scan.emit({})
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

    def _run_parallel(self, n_pages, n_workers):
        chunks = equipment_detection._split_into_chunks(n_pages, n_workers)
        all_rows = []
        ocr_engine_used = None
        cancelled = False
        manager = multiprocessing.Manager()
        progress_queue = manager.Queue()
        try:
            executor = concurrent.futures.ProcessPoolExecutor(max_workers=len(chunks))
            try:
                futures = [executor.submit(
                    equipment_detection._scan_page_range_worker,
                    self._pdf_path, chunk, self._use_ocr, self._ocr_engine, progress_queue,
                    len(chunks))
                    for chunk in chunks]
                pending = set(futures)
                while pending:
                    _drain_progress_queue(progress_queue, self.page_progress.emit)
                    if self.isInterruptionRequested():
                        cancelled = True
                        for f in pending:
                            f.cancel()
                        break
                    _done, pending = concurrent.futures.wait(
                        pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
                _drain_progress_queue(progress_queue, self.page_progress.emit)
                if not cancelled:
                    for f in futures:
                        try:
                            rows, engine_name = f.result()
                        except concurrent.futures.CancelledError:
                            continue
                        except Exception as e:
                            logging.error(f"ParallelTagScanWorker: a worker "
                                          f"process failed: {e}", exc_info=True)
                            continue
                        all_rows.extend(rows)
                        if engine_name:
                            ocr_engine_used = engine_name
            finally:
                # wait=True even when cancelled: cancel_futures=True skips
                # any not-yet-STARTED task, but a worker process already
                # spawned/mid-startup at cancel time keeps running and
                # reaches for the manager's queue proxy — tearing down
                # `manager` before that process finishes connecting to it
                # orphans it with a noisy WinError/FileNotFoundError
                # traceback (confirmed via a real cancellation test).
                # Waiting bounds "Avbryt" latency to one in-flight chunk's
                # remaining work, same order of magnitude as the existing
                # single-thread path's own once-per-page cancel check —
                # not instant, but always clean.
                executor.shutdown(wait=True, cancel_futures=cancelled)
        finally:
            manager.shutdown()

        if cancelled:
            self.finished_scan.emit({})
            return
        result = equipment_detection._merge_scan_page_rows(all_rows)
        result['_meta']['ocr_engine'] = ocr_engine_used
        self.finished_scan.emit(result)


class ParallelEquipmentAnalysisWorker(QThread):
    """Runs detect_equipment_and_valves() across multiple CPU-core
    PROCESSES when the document is large enough to be worth it, otherwise
    delegates to the existing single-thread EquipmentAnalysisWorker (its
    `run()` is a plain method, safe to call directly off its own thread —
    same pattern EquipmentAnalysisWorkerTests already establishes) so this
    class never duplicates that logic. See NOTES.md.

    page_progress(page_num, status) — 'running'|'done', per page.
    finished_analysis(list, list) — (results, rejected), ALWAYS emitted.
    """
    page_progress     = pyqtSignal(int, str)
    finished_analysis = pyqtSignal(list, list)

    def __init__(self, pdf_path, tag_points, pages=None,
                 min_bowtie_score=0.5, min_confidence=0.3, parent=None):
        super().__init__(parent)
        self._pdf_path         = str(pdf_path)
        self._tag_points       = list(tag_points)
        self._pages            = list(pages) if pages is not None else None
        self._min_bowtie_score = min_bowtie_score
        self._min_confidence   = min_confidence

    def run(self):
        doc = None
        try:
            doc = fitz.open(self._pdf_path)
            pages = self._pages if self._pages is not None else list(range(doc.page_count))
        except Exception as e:
            logging.error(f"ParallelEquipmentAnalysisWorker.run() failed to open "
                          f"PDF: {e}", exc_info=True)
            self.finished_analysis.emit([], [])
            return
        finally:
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

        n_workers = _pick_worker_count(len(pages), use_ocr=False, ocr_engine='auto')
        if not _should_parallelize(len(pages), n_workers):
            self._run_sequential(pages)
            return
        try:
            self._run_parallel(pages, n_workers)
        except Exception as e:
            logging.error(f"ParallelEquipmentAnalysisWorker.run() parallel path "
                          f"failed, falling back to sequential: {e}", exc_info=True)
            try:
                self._run_sequential(pages)
            except Exception as e2:
                logging.error(f"ParallelEquipmentAnalysisWorker: sequential "
                              f"fallback also failed: {e2}", exc_info=True)
                self.finished_analysis.emit([], [])

    def _run_sequential(self, pages):
        inner = EquipmentAnalysisWorker(
            self._pdf_path, self._tag_points, pages=pages,
            min_bowtie_score=self._min_bowtie_score, min_confidence=self._min_confidence)
        last_page = [None]

        def _on_progress(page_num, _total, _msg):
            if last_page[0] is not None and last_page[0] != page_num:
                self.page_progress.emit(last_page[0], 'done')
            self.page_progress.emit(page_num, 'running')
            last_page[0] = page_num

        def _on_finished(results, rejected):
            if last_page[0] is not None:
                self.page_progress.emit(last_page[0], 'done')
            self.finished_analysis.emit(results, rejected)

        inner.progress.connect(_on_progress)
        inner.finished_analysis.connect(_on_finished)
        inner.run()

    def _run_parallel(self, pages, n_workers):
        chunks = equipment_detection._split_into_chunks(len(pages), n_workers)
        # _split_into_chunks works on a 0-based page COUNT — map its
        # indices back onto the actual `pages` list so this still works
        # when the caller passed a filtered/non-contiguous subset.
        chunks = [[pages[i] for i in chunk] for chunk in chunks]
        all_results, all_rejected = [], []
        cancelled = False
        manager = multiprocessing.Manager()
        progress_queue = manager.Queue()
        try:
            executor = concurrent.futures.ProcessPoolExecutor(max_workers=len(chunks))
            try:
                futures = [executor.submit(
                    equipment_detection._analyze_page_range_worker,
                    self._pdf_path, chunk, self._tag_points,
                    self._min_bowtie_score, self._min_confidence, progress_queue)
                    for chunk in chunks]
                pending = set(futures)
                while pending:
                    _drain_progress_queue(progress_queue, self.page_progress.emit)
                    if self.isInterruptionRequested():
                        cancelled = True
                        for f in pending:
                            f.cancel()
                        break
                    _done, pending = concurrent.futures.wait(
                        pending, timeout=0.1, return_when=concurrent.futures.FIRST_COMPLETED)
                _drain_progress_queue(progress_queue, self.page_progress.emit)
                if not cancelled:
                    for f in futures:
                        try:
                            results, rejected = f.result()
                        except concurrent.futures.CancelledError:
                            continue
                        except Exception as e:
                            logging.error(f"ParallelEquipmentAnalysisWorker: a "
                                          f"worker process failed: {e}", exc_info=True)
                            continue
                        all_results.extend(results)
                        all_rejected.extend(rejected)
            finally:
                # wait=True even when cancelled: cancel_futures=True skips
                # any not-yet-STARTED task, but a worker process already
                # spawned/mid-startup at cancel time keeps running and
                # reaches for the manager's queue proxy — tearing down
                # `manager` before that process finishes connecting to it
                # orphans it with a noisy WinError/FileNotFoundError
                # traceback (confirmed via a real cancellation test).
                # Waiting bounds "Avbryt" latency to one in-flight chunk's
                # remaining work, same order of magnitude as the existing
                # single-thread path's own once-per-page cancel check —
                # not instant, but always clean.
                executor.shutdown(wait=True, cancel_futures=cancelled)
        finally:
            manager.shutdown()

        if cancelled:
            self.finished_analysis.emit([], [])
            return
        self.finished_analysis.emit(all_results, all_rejected)


class PageProgressDialog(QDialog):
    """Compact "Analyserar…" dialog showing individual status per page —
    replaces the old single-line QProgressDialog now that analysis can run
    across several worker PROCESSES and pages finish out of order (see
    NOTES.md "Flerkärnig parallellisering av Analysera P&ID"). Shared by
    all three analysis entry points: PIDPanel._analyze_pid,
    EquipmentPanel._scan, EquipmentPanel._autodetect.
    """
    _PENDING, _RUNNING, _DONE = '⏳', '⚙', '✓'
    _COLS = 6

    canceled = pyqtSignal()

    def __init__(self, title, n_pages, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(320, 420)
        self.setModal(True)
        self._n_pages = n_pages
        self._done_count = 0

        layout = QVBoxLayout(self)
        self._summary_lbl = QLabel(f"0/{n_pages} sidor klara")
        bold = QFont(); bold.setBold(True)
        self._summary_lbl.setFont(bold)
        layout.addWidget(self._summary_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setSpacing(2)
        self._labels = {}
        for i in range(n_pages):
            lbl = QLabel(f"{self._PENDING} {i + 1}")
            lbl.setStyleSheet("color:#8D9299; padding:2px 4px;")
            self._labels[i] = lbl
            grid.addWidget(lbl, i // self._COLS, i % self._COLS)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        cancel_btn = QPushButton("Avbryt")
        cancel_btn.clicked.connect(self._on_cancel_clicked)
        layout.addWidget(cancel_btn)

    def _on_cancel_clicked(self):
        self._summary_lbl.setText("Avbryter…")
        self.canceled.emit()

    def set_page_status(self, page_num, status):
        lbl = self._labels.get(page_num)
        if lbl is None:
            return
        if status == 'running':
            lbl.setText(f"{self._RUNNING} {page_num + 1}")
            lbl.setStyleSheet("color:#1565C0; padding:2px 4px; font-weight:bold;")
        elif status == 'done':
            lbl.setText(f"{self._DONE} {page_num + 1}")
            lbl.setStyleSheet("color:#2E7D32; padding:2px 4px;")
            self._done_count += 1
            self._summary_lbl.setText(f"{self._done_count}/{self._n_pages} sidor klara")


class ConnectorDotItem(QGraphicsEllipseItem):
    """Draggable dot marking an off-page connector symbol on the P&ID.

    Position is saved back to DB on every drag-release so the user's
    manual adjustment survives across sessions.
    """
    _R = 7.0

    def __init__(self, connector_id: int, db, color_hex: str, scene_pos):
        r = self._R
        super().__init__(-r, -r, r * 2, r * 2)
        self._conn_id = connector_id
        self._db      = db
        self.setPos(scene_pos)
        dot_color = QColor(color_hex); dot_color.setAlpha(230)
        self.setBrush(QBrush(dot_color))
        ring = QPen(QColor(255, 255, 255, 220), 2.0); ring.setCosmetic(True)
        self.setPen(ring)
        self.setZValue(Z_SHEET_CONN + 0.5)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable,            True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Flytta connectorn — dra för att justera position")

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self._conn_id >= 0 and self._db is not None:
            p = self.pos()
            self._db.update_connector_dot_position(self._conn_id, p.x(), p.y())


def _vline():
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


class PIDImportDialog(QDialog):
    """Dialog shown when opening a P&ID — choose Ny revision or Nya blad."""

    def __init__(self, has_existing=True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Importera P&ID")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        if has_existing:
            layout.addWidget(QLabel("En P&ID är redan inläst. Vad vill du göra?"))
            self._new_rev_btn    = QRadioButton("Ny revision — ersätt befintlig PDF")
            self._new_sheets_btn = QRadioButton("Nya blad — sammanfoga och lägg till sidor sist")
            self._new_rev_btn.setChecked(True)
            layout.addWidget(self._new_rev_btn)
            layout.addWidget(self._new_sheets_btn)
        else:
            layout.addWidget(QLabel("Importera P&ID-ritning:"))
            self._new_rev_btn    = None
            self._new_sheets_btn = None

        form = QFormLayout()
        form.setSpacing(8)
        self._label_edit = QLineEdit()
        self._label_edit.setPlaceholderText("t.ex. Rev A, 2024-01-15")
        self._notes_edit = QLineEdit()
        self._notes_edit.setPlaceholderText("Valfri beskrivning")
        form.addRow("Revision/märkning:", self._label_edit)
        form.addRow("Anteckningar:", self._notes_edit)
        layout.addLayout(form)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def is_new_revision(self):
        if self._new_rev_btn is None:
            return True
        return self._new_rev_btn.isChecked()

    def label(self):
        return self._label_edit.text().strip()

    def notes(self):
        return self._notes_edit.text().strip()


# ── PDF-export helpers ─────────────────────────────────────────────────────────

def _hex_to_fitz_rgb(hex_color):
    """Convert '#rrggbb' string to (r, g, b) float tuple (0..1)."""
    h = hex_color.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


def _draw_pid_marker(page, x, y, rgb, letter, label):
    """Draw a rounded card-style marker at (x, y) with a letter + label
    baked inside it (2026-08-17, replaces the old plain circle+dot style
    to read closer to the app's own on-screen equipment markers, see
    NOTES.md "snyggare markörrutor i PDF-exporten").

    (x, y) is in the LIVE VIEW's already-rotated display space (what
    scene_to_pdf() actually stores — see its own docstring) — NOT
    PyMuPDF's raw content-space, which is what draw/insert calls always
    address regardless of the page's /Rotate value (that attribute is a
    pure display hint, never consulted by drawing APIs). Un-rotate via
    derotation_matrix before drawing, or the marker lands in the wrong
    physical spot on any page with a non-zero rotation (2026-08-17, see
    NOTES.md "objektmarkörernas positioner följer inte med rotationen" —
    this was still broken after the separate fix that made the PAGE
    itself rotate correctly in the export).
    """
    pt = fitz.Point(x, y) * page.derotation_matrix
    x, y = pt.x, pt.y

    label_text = str(label)[:40] if label else ''
    fontsize = 7
    text_w = fitz.get_text_length(label_text, fontname='helv', fontsize=fontsize) if label_text else 0
    pad = 3.0
    letter_w = 9.0
    card_h = 14.0
    card_w = letter_w + (text_w + pad * 2 if label_text else 0) + pad
    rect = fitz.Rect(x, y - card_h / 2, x + card_w, y + card_h / 2)

    shape = page.new_shape()
    try:
        shape.draw_rect(rect, radius=0.25)
    except TypeError:
        shape.draw_rect(rect)
    try:
        shape.finish(color=rgb, fill=rgb, width=0.5, fill_opacity=0.85)
    except TypeError:
        shape.finish(color=rgb, fill=rgb, width=0.5)
    shape.commit()

    try:
        page.insert_text(fitz.Point(x + pad * 0.5, y + 3.0), letter,
                         fontsize=9, color=(1.0, 1.0, 1.0), fontname='helv')
    except Exception:
        pass
    if label_text:
        try:
            page.insert_text(fitz.Point(x + letter_w, y + 2.5),
                             label_text, fontsize=fontsize, color=(1.0, 1.0, 1.0), fontname='helv')
        except Exception:
            pass


_INSTR_SECONDARY_EFFECTS = [
    "Pump stoppar",
    "Kompressor stoppar",
    "Reglerventil stänger",
    "Reglerventil öppnar",
    "Spjäll stänger",
    "Spjäll öppnar",
    "Nödstoppar / ESD",
    "Larm aktiveras (ingen automatisk åtgärd)",
]

# Maps secondary effect description → component type for secondary marker
_INSTR_SEC_COMP_TYPES = {
    "Pump stoppar":           "Pump",
    "Kompressor stoppar":     "Kompressor",
    "Reglerventil stänger":   "Ventil",
    "Reglerventil öppnar":    "Ventil",
    "Spjäll stänger":         "Ventil",
    "Spjäll öppnar":          "Ventil",
}

# Simultaneous word-swap table for inverting cause descriptions.
# Sorted longest-first in the regex so e.g. 'stoppar' matches before 'stopp'.
_INVERSION_MAP = {
    # ── High / low ─────────────────────────────────────────────────────────────
    'högt': 'lågt',        'lågt': 'högt',
    'Högt': 'Lågt',        'Lågt': 'Högt',
    'hög': 'låg',          'låg': 'hög',
    'Hög': 'Låg',          'Låg': 'Hög',
    # ── Open / closed — verb (present tense) ────────────────────────────────────
    'stänger': 'öppnar',   'öppnar': 'stänger',
    'Stänger': 'Öppnar',   'Öppnar': 'Stänger',
    # ── Open / closed — adjective common gender ──────────────────────────────────
    'stängd': 'öppen',     'öppen': 'stängd',
    'Stängd': 'Öppen',     'Öppen': 'Stängd',
    # ── Open / closed — adjective neuter gender ──────────────────────────────────
    'stängt': 'öppet',     'öppet': 'stängt',
    'Stängt': 'Öppet',     'Öppet': 'Stängt',
    # ── Open / closed — past participle ("öppnat") ──────────────────────────────
    'öppnat': 'stängt',    'Öppnat': 'Stängt',
    # ── Open / closed — noun ────────────────────────────────────────────────────
    'stängning': 'öppning', 'öppning': 'stängning',
    'Stängning': 'Öppning', 'Öppning': 'Stängning',
    # ── Open / closed — English (fail-open / fail-closed) ───────────────────────
    'closed': 'open',      'open': 'closed',
    # ── Stop / start — verb (present tense) ─────────────────────────────────────
    'stoppar': 'startar',  'startar': 'stoppar',
    'Stoppar': 'Startar',  'Startar': 'Stoppar',
    # ── Stop / start — noun / compound ──────────────────────────────────────────
    'stopp': 'start',      'start': 'stopp',
    'Stopp': 'Start',      'Start': 'Stopp',
}
# Sort keys longest-first so longer tokens (e.g. 'stoppar') match before
# their shorter prefixes (e.g. 'stopp') in the alternation.
_INVERSION_RE = re.compile(
    '|'.join(re.escape(k) for k in sorted(_INVERSION_MAP, key=len, reverse=True))
)


def invert_cause_text(text):
    """Swap directional Swedish words for the inverse deviation.

    Returns the original string unchanged if no invertible words were found
    (caller can compare result == text to detect 'no inverse').
    """
    return _INVERSION_RE.sub(lambda m: _INVERSION_MAP[m.group(0)], text)


class TemplateCausePickerDialog(QDialog):
    """Shown after placing a cause marker — pick component, tag and cause from template.

    Cause list filters dynamically when the user changes component type.
    For Instrument / Sensor type an extra 'secondary effect' section is shown
    so the user can capture the full chain: instrument fails → valve/pump reacts.
    """

    def __init__(self, deviation_name, standard_causes,
                 component_types=None, suggested_tag='', preselect_type='', parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Orsak — {deviation_name}")
        self.setMinimumWidth(460)
        self._all_causes        = list(standard_causes)   # full list, each row has comp_type field
        self._std_rbs           = []                       # dynamically created primary radio buttons
        self._chosen            = None
        self._chosen_std_freq      = None   # frequency from chosen standard cause
        self._chosen_std_cause_id  = None   # id in standard_causes table
        self._comp_type         = ''
        self._comp_tag          = ''
        self._wants_secondary   = False
        self._chosen_secondary  = ''    # secondary effect text (e.g. "Pump stoppar (P-101)")
        self._sec_comp_type_out = ''    # comp_type for secondary marker

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"<b>Avvikelse:</b> {deviation_name}"))

        # ── Component info ────────────────────────────────────────────────────
        form = QFormLayout()
        self._type_combo = QComboBox()
        self._type_combo.addItem("")
        for ct in (component_types or []):
            self._type_combo.addItem(ct)
        form.addRow("Komponenttyp:", self._type_combo)

        self._tag_edit = QLineEdit(suggested_tag)
        self._tag_edit.setPlaceholderText("t.ex. XV-101")
        form.addRow("Komponent-ID:", self._tag_edit)
        layout.addLayout(form)

        # ── Cause list (dynamic) ──────────────────────────────────────────────
        self._cause_header = QLabel("Välj standardorsak eller ange fritext:")
        layout.addWidget(self._cause_header)

        self._cause_container = QWidget()
        self._cause_layout = QVBoxLayout(self._cause_container)
        self._cause_layout.setContentsMargins(0, 0, 0, 0)
        self._cause_layout.setSpacing(3)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._cause_container)
        scroll.setMaximumHeight(180)
        layout.addWidget(scroll)

        # ── Primary button group (radio buttons added dynamically + free text) ─
        self._group = QButtonGroup(self)

        self._rb_free = QRadioButton("Annan:")
        self._rb_free.setProperty('cause_desc', None)
        self._group.addButton(self._rb_free, 9999)
        layout.addWidget(self._rb_free)
        self._free_edit = QLineEdit()
        self._free_edit.setPlaceholderText("Fritext orsak…")
        self._free_edit.textChanged.connect(partial(self._rb_free.setChecked, True))
        layout.addWidget(self._free_edit)

        # ── Instrument secondary section (hidden unless Instrument type) ───────
        self._instr_group = QGroupBox("Sekundär verkan (vad händer som följd av instrumentfelet?)")
        instr_layout = QVBoxLayout(self._instr_group)
        instr_layout.setSpacing(3)
        self._sec_group = QButtonGroup(self)
        for i, eff in enumerate(_INSTR_SECONDARY_EFFECTS):
            rb = QRadioButton(eff)
            rb.setProperty('sec_desc', eff)
            self._sec_group.addButton(rb, i)
            instr_layout.addWidget(rb)
            if i == 0:
                rb.setChecked(True)
        rb_sec_free = QRadioButton("Annan sekundär verkan:")
        rb_sec_free.setProperty('sec_desc', None)
        self._sec_group.addButton(rb_sec_free, len(_INSTR_SECONDARY_EFFECTS))
        instr_layout.addWidget(rb_sec_free)
        self._sec_free_edit = QLineEdit()
        self._sec_free_edit.setPlaceholderText("t.ex. Reglerventil XV-201 stänger")
        self._sec_free_edit.textChanged.connect(partial(rb_sec_free.setChecked, True))
        instr_layout.addWidget(self._sec_free_edit)

        sec_form = QFormLayout()
        self._sec_tag_edit = QLineEdit()
        self._sec_tag_edit.setPlaceholderText("t.ex. P-101  (valfri)")
        sec_form.addRow("Sekundär komponent-ID:", self._sec_tag_edit)
        instr_layout.addLayout(sec_form)

        mark_btn = QPushButton("Markera objekt på P&ID")
        mark_btn.setIcon(_icon('pin'))
        mark_btn.setToolTip(
            "Spara orsaken och gå direkt till P&ID för att klicka på sekundärkomponenten")
        mark_btn.setStyleSheet(
            "QPushButton{background:#6c3483;color:white;border:none;"
            "border-radius:4px;padding:5px 10px;font-weight:bold;}"
            "QPushButton:hover{background:#8e44ad;}")
        mark_btn.clicked.connect(self._accept_with_secondary)
        instr_layout.addWidget(mark_btn)

        self._instr_group.setVisible(False)
        layout.addWidget(self._instr_group)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        # ── Wire up type combo and initial render ─────────────────────────────
        self._type_combo.currentTextChanged.connect(self._on_type_changed)
        if preselect_type and preselect_type in (component_types or []):
            self._type_combo.setCurrentText(preselect_type)
        else:
            self._update_cause_list('')

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _on_type_changed(self, comp_type):
        self._update_cause_list(comp_type)
        is_instrument = 'Instrument' in comp_type
        self._instr_group.setVisible(is_instrument)

    def _update_cause_list(self, comp_type):
        # Remove old standard radio buttons from group and layout
        for rb in self._std_rbs:
            self._group.removeButton(rb)
        self._std_rbs = []
        while self._cause_layout.count():
            item = self._cause_layout.takeAt(0)
            if item and item.widget():
                item.widget().hide()
                item.widget().setParent(None)

        # Filter: only show causes marked for use in cause form.
        # When a component type is selected, prefer type-specific causes; fall back to generic.
        active = [c for c in self._all_causes if dict(c).get('use_in_cause_form', 1)]
        if comp_type:
            filtered = [c for c in active if dict(c).get('comp_type', '') == comp_type]
            if not filtered:
                filtered = [c for c in active if not dict(c).get('comp_type', '')]
        else:
            filtered = [c for c in active if not dict(c).get('comp_type', '')]

        first_rb = None
        for i, c in enumerate(filtered):
            freq = dict(c).get('frequency')
            label = c['description']
            if freq is not None:
                label += f"  ({freq:g}/år)"
            rb = QRadioButton(label)
            rb.setProperty('cause_desc', c['description'])
            rb.setProperty('cause_freq', freq)
            rb.setProperty('cause_id',   dict(c).get('id'))
            self._group.addButton(rb, i)
            self._cause_layout.addWidget(rb)
            self._std_rbs.append(rb)
            if first_rb is None:
                first_rb = rb

        if first_rb:
            first_rb.setChecked(True)
        else:
            self._rb_free.setChecked(True)

        if comp_type:
            self._cause_header.setText(f"Välj orsak för <b>{comp_type}</b>:")
        else:
            self._cause_header.setText("Välj standardorsak eller ange fritext:")

    # ── Accept ────────────────────────────────────────────────────────────────

    def _accept_with_secondary(self):
        self._wants_secondary = True
        self._accept()

    def _accept(self):
        btn = self._group.checkedButton()
        if btn is None:
            self.reject()
            return
        desc = btn.property('cause_desc')
        if desc is None:
            desc = self._free_edit.text().strip()
        if not desc:
            QMessageBox.warning(self, "Tom orsak", "Ange en orsak.")
            return

        # Instrument secondary: build combined description + store secondary info
        comp_type = self._type_combo.currentText().strip()
        if 'Instrument' in comp_type and self._instr_group.isVisible():
            sec_btn = self._sec_group.checkedButton()
            if sec_btn:
                sec_desc = sec_btn.property('sec_desc')
                if sec_desc is None:
                    sec_desc = self._sec_free_edit.text().strip()
                if sec_desc:
                    sec_tag = self._sec_tag_edit.text().strip()
                    suffix = f" ({sec_tag})" if sec_tag else ""
                    self._chosen_secondary  = f"{sec_desc}{suffix}"
                    self._sec_comp_type_out = _INSTR_SEC_COMP_TYPES.get(sec_desc, '')
                    desc = f"{desc} → {self._chosen_secondary}"

        self._chosen    = desc
        self._comp_type = comp_type
        self._comp_tag  = self._tag_edit.text().strip()
        # Frequency from standard cause (None for free-text entries)
        btn2 = self._group.checkedButton()
        raw_desc = btn2.property('cause_desc') if btn2 else None
        self._chosen_std_freq     = btn2.property('cause_freq') if (btn2 and raw_desc is not None) else None
        self._chosen_std_cause_id = btn2.property('cause_id')   if (btn2 and raw_desc is not None) else None
        self.accept()

    @property
    def chosen_description(self):
        return self._chosen

    @property
    def chosen_std_cause_freq(self):
        return self._chosen_std_freq

    @property
    def chosen_std_cause_id(self):
        return self._chosen_std_cause_id

    @property
    def component_type(self):
        return self._comp_type

    @property
    def component_tag(self):
        return self._comp_tag

    @property
    def wants_secondary_placement(self):
        return self._wants_secondary

    @property
    def secondary_description(self):
        """Short text for the secondary effect, e.g. 'Pump stoppar (P-101)'."""
        return self._chosen_secondary

    @property
    def secondary_comp_type(self):
        """Component type for the secondary marker, e.g. 'Pump'."""
        return self._sec_comp_type_out

    @property
    def secondary_component_tag(self):
        return self._sec_tag_edit.text().strip()


# Frequency F=-1..5, stored as integer in causes.likelihood. Defined here
# (rather than hazop.py, which imports it back) because EquipmentDeviationBar
# is the only place pid_viewer.py itself needs to render/edit a frequency —
# see hazop.py's own FREQ_LABELS for the matrix-boundary-aware sibling
# (freq_to_f_level), which stays there since it depends on get_matrix().
FREQ_LABELS = [
    'F-1 – Otänkbar',
    'F0 – Extremt sällan',
    'F1 – Sällan',
    'F2 – Osannolik',
    'F3 – Möjlig',
    'F4 – Trolig',
    'F5 – Frekvent',
]


def freq_to_idx(f: int) -> int:
    """Frequency value (-1..5) → combo-box index (0..6)."""
    return max(0, min(int(f) + 1, 6))


def idx_to_freq(i: int) -> int:
    """Combo-box index (0..6) → frequency value (-1..5)."""
    return i - 1


def _format_freq_per_year(value):
    """Format a numeric frequency (events/year) for display — same
    formatting rules already used for the worksheet's ORS-column
    frequency label (hazop.py's _ScenarioDelegate.paint) and RiskBadge,
    duplicated here for the same reason FREQ_LABELS/_obj_type_matches are
    (pid_viewer.py can't import back from hazop.py). Returns '—' for
    None/falsy so callers can always just set the label text directly."""
    if not value:
        return "—"
    bfv = float(value)
    if bfv >= 0.1:
        return f"{bfv:.2g}/år"
    elif bfv >= 0.001:
        return f"{bfv:.3g}/år"
    return f"{bfv:.1e}".replace('e-0', 'e-') + "/år"


def _obj_type_matches(preselect: str, obj_name: str) -> bool:
    """True when preselect and obj_name refer to the same object type.
    Bidirectional whole-string substring match (case-insensitive) — no
    word-level matching, to avoid e.g. 'ventil' matching 'backventil' when
    preselect is 'Manuell ventil'. Defined here (not hazop.py, which imports
    it back) for the same reason as FREQ_LABELS above — EquipmentDeviationBar
    needs it too.
    """
    if not preselect or not obj_name:
        return False
    p, n = preselect.lower(), obj_name.lower()
    return p in n or n in p


# ══════════════════════════════════════════════════════════════════════════════
# RE-EXPORTS — PIDGraphicsView and PIDPanel/EquipmentDeviationBar moved to
# their own files (2026-08-18, see NOTES.md). Imported at the BOTTOM of this
# module, not the top: both files import shared constants/helpers (MODE_*,
# CONFIG, _icon, etc.) back from this module, so importing them any earlier
# would try to read names this file hasn't defined yet.
# ══════════════════════════════════════════════════════════════════════════════

from pid_graphics_view import PIDGraphicsView
from pid_panel_mod import EquipmentDeviationBar, PIDPanel

