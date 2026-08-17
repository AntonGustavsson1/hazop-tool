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

# Optional OpenGL for GPU-accelerated rendering
try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    HAS_OPENGL = True
except ImportError:
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

_ICONS_DIR = Path(__file__).parent / 'icons'
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

    def __init__(self, results: list, db, parent=None, rejected: list = None, pdf_path=None):
        super().__init__(parent)
        self.db = db
        self._results = results   # dicts: tag,page,comp_type,x,y,confidence(_es),link_method,outline,equipment_id
        self._rejected = rejected or []
        self._pdf_path = pdf_path
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
                pad = max((x1 - x0), (y1 - y0)) * 0.25 + 4.0
            else:
                if page_num not in page_scale_cache:
                    page_scale_cache[page_num] = symbol_geometry.dominant_text_size(page)
                pad = max(page_scale_cache[page_num] * 1.5, 15.0)
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
                prefix = _equip_prefix_from_tag(tag) or ''
                equipment_id = self.db.add_equipment_item(
                    tag, tag, prefix, res['page'], comp_type, '', 0)

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


class PIDGraphicsView(QGraphicsView):
    node_markup_finished    = pyqtSignal(list, int)
    context_action          = pyqtSignal(str, object, int)
    marker_clicked          = pyqtSignal(str, int)
    ref_tag_picked          = pyqtSignal(str)   # MODE_PICK_REF_TAG one-shot result
    annotation_clicked      = pyqtSignal(object)  # QPointF — MODE_ANNOTATION click
    # Markup editing signals
    markup_draw_finished    = pyqtSignal(str, list, int)   # type_, pdf_pts, page
    markup_item_clicked     = pyqtSignal(int)              # markup_id
    markup_moved            = pyqtSignal(int, list)        # mu_id, new PDF points [[x,y],...]
    markup_label_edited     = pyqtSignal(int, str)         # mu_id, new_label
    markup_duplicate_requested = pyqtSignal(int)           # mu_id
    markup_symbol_dims_changed = pyqtSignal(int, float, float, float)  # mu_id, w, h, rot_deg
    board_layout_changed     = pyqtSignal(str)  # JSON {"0": [ox, oy], ...}
    sheet_conn_break_requested = pyqtSignal(int)          # connection row id
    sheet_conn_add_requested   = pyqtSignal(int, int)     # (from_page, to_page)
    zone_drawn    = pyqtSignal(object, int)                # (QRectF pdf_coords, page)
    equipment_drag_finished = pyqtSignal()  # Shift+drag of an equipment marker released (drop accepted or not)
    equipment_edit_requested = pyqtSignal(int)  # equipment_markers.id — right-click "✏️ Redigera objekt"

    # Keys for QGraphicsItem.setData / .data
    _DATA_TYPE      = 0    # 'cause' | 'consequence' | 'safeguard' | 'markup'
    _DATA_ID        = 1    # database id
    _DATA_MARKUP_ID = 2    # markup id (for markup items)
    _DATA_MARKUP_PTS = 3   # stores PDF points list on path/text items
    _DATA_SYMBOL_W   = 6   # float PDF-unit width stored on symbol items
    _DATA_SYMBOL_H   = 7   # float PDF-unit height stored on symbol items
    _DATA_SYMBOL_ROT = 8   # float rotation degrees stored on symbol items

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        # White background — matches typical P&ID drawing background
        self.setBackgroundBrush(QBrush(QColor(255, 255, 255)))

        # ── GPU-accelerated rendering (OpenGL) ────────────────────────────────
        if HAS_OPENGL:
            gl = QOpenGLWidget()
            self.setViewport(gl)
            # With OpenGL, full-viewport update is required (GPU composites all)
            self.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        else:
            # CPU fallback: only repaint changed tiles
            self.setViewportUpdateMode(
                QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)

        # Avoid unnecessary bounding-rect adjustments during pan/zoom
        self.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
        # Don't save/restore painter state per item — slight speedup
        self.setOptimizationFlag(
            QGraphicsView.OptimizationFlag.DontSavePainterState, True)

        self._press_pos  = None
        # Shift+drag of an equipment marker onto a worksheet consequence row
        # (see mousePressEvent/mouseMoveEvent) — armed on Shift+press over a
        # marker, consumed by mouseMoveEvent once the drag distance is exceeded.
        self._equip_drag_candidate = None
        # Multi-select of equipment markers (2026-08-08, see NOTES.md):
        # Ctrl+click toggles one marker in/out, Ctrl+drag rubber-bands
        # several at once; a Shift-drag started from a marker that's part
        # of a >=2-member selection then drags the whole group.
        self._selected_equipment_markers: set = set()
        self._equip_selection_overlays: dict = {}   # marker_id -> highlight QGraphicsItem
        self._ctrl_rband_start_scene  = None
        self._ctrl_rband_dragging     = False
        self._ctrl_rband_preview_item = None
        self._ctrl_rband_count_label  = None   # "N objekt" label shown live during the drag
        # Per-type marker tracking for visibility toggle
        self._type_items: dict = {'cause': [], 'consequence': [], 'safeguard': [], 'equipment': []}
        self._type_visible: dict = {'cause': True, 'consequence': True, 'safeguard': True, 'equipment': True}

        self.mode             = MODE_NAV
        self.pdf_doc          = None
        self.current_page     = 0
        self.page_item        = None
        self.render_scale     = 1.0
        self.page_rect_width  = 0.0
        self.page_rect_height = 0.0
        self._RASTER_SCALE    = 3.0
        self._pdf_path        = None
        self._page_cache: dict = {}
        # Raster scale each cached pixmap was actually rendered at — a page
        # can now be cached above _RASTER_SCALE when the adaptive-zoom
        # re-rasterization (see _target_raster_scale) upgrades it.
        self._page_cache_scale: dict = {}
        self._cache_order: list = []
        self._CACHE_SIZE      = 10
        self._prefetch_thread = None

        # ── Manual per-page rotation override (2026-08-12, see NOTES.md) ──────
        # Extra clockwise degrees (0/90/180/270) the user chose for a physical
        # page, composed with (not replacing) the PDF's own /Rotate flag by
        # mutating the in-memory fitz.Page's rotation — see
        # _apply_page_rotation(). Never written back to the PDF file.
        self._page_rotation_override: dict = {}   # physical_page -> extra degrees
        self._intrinsic_rotation: dict = {}        # physical_page -> page.rotation before any override

        # ── Page level-of-detail (study board) ────────────────────────────────
        # Board pages render at _LOW_SCALE (cheap, small) and are scaled up to
        # the same scene footprint; only pages visible at high zoom are swapped
        # to full _RASTER_SCALE pixmaps in the background. Keeps zoomed-out
        # panning fast and memory flat regardless of page count.
        self._LOW_SCALE       = 0.5
        self._MAX_HIRES       = 6
        self._low_pixmaps: dict = {}    # pn → low-res QPixmap
        self._hires_pages: set  = set() # pages currently showing hi-res
        # Raster scale actually displayed on the hi-res item for each hires
        # page — may exceed _RASTER_SCALE, see adaptive re-rasterization below.
        self._page_display_scale: dict = {}
        self._lod_renderer    = None    # background _PageRenderer for hi-res
        # Supersampling margin: a tier is "crisp enough" once it supplies at
        # least this many raw pixels per screen pixel. Shared by
        # _hires_worthwhile() (low->hires threshold) and _target_raster_scale()
        # (adaptive hires->sharper-hires threshold) so both tiers use the same
        # visual standard for "still blurry".
        self._LOD_MARGIN      = 1.25
        # Adaptive re-rasterization caps (2026-08-12, see NOTES.md "P&ID blir
        # suddig vid inzoomning") — a page can be re-rendered at a scale
        # higher than _RASTER_SCALE once the view zooms in past what
        # _RASTER_SCALE can supply crisply, but never above these safety caps
        # so a pathological zoom level can't render an absurdly large pixmap.
        self._MAX_RASTER_SCALE = self._RASTER_SCALE * 4   # absolute multiplier cap
        self._MAX_RASTER_DIM   = 6000  # cap on the larger pixel dimension of any single raster
        self._lod_timer       = QTimer(self)
        self._lod_timer.setSingleShot(True)
        self._lod_timer.setInterval(150)
        self._lod_timer.timeout.connect(self._update_page_lod)

        self.draw_points        = []
        self.draw_pen           = QPen(QColor(255, 140, 0), 3)
        self.draw_pen.setCosmetic(True)
        self.draw_brush         = QBrush(QColor(255, 140, 0, 60))
        self.temp_items         = []
        self.rubber_line        = None

        # Cache for PDF line segments (for smart snapping to drawing details)
        self._pdf_line_segments: dict = {}  # page_num → list of line tuples (x1,y1,x2,y2)

        # Right-drag rubber-band (NAV mode)
        self._rband_start_scene  = None
        self._rband_preview_item = None
        self._rband_label_item   = None   # QGraphicsTextItem in right-drag rband
        self._rband_dragging     = False

        # Label slot tracker: (qx, qy) → next free row index (reset on clear_overlays)
        self._label_slots: dict = {}
        self._pending_path_item = None

        # Markup overlay tracking: markup_id → list of QGraphicsItems
        self._markup_items: dict = {}
        self._markup_highlighted: int = -1
        self._snap_enabled: bool = True
        self._markup_types: dict   = {}   # mu_id → 'polygon'|'polyline'|'text'|'comment'
        # Red markup overlay tracking (separate from node markup)
        self._red_markup_items: dict = {}
        self._red_markup_types: dict = {}
        self._edit_mu_id            = None
        self._vertex_handles: list  = []
        self._drag_mode             = None   # 'vertex' | 'item' | 'symbol_transform' | None
        self._drag_vertex_idx       = None
        self._drag_start_scene      = None
        self._drag_original_pts: list = []
        self._drag_current_pts: list  = []
        self._drag_threshold_exceeded = False
        self._drag_item_origins: list = []  # [(QGraphicsItem, QPointF)] for text/comment
        # Symbol resize/rotate handles and live state
        self._corner_handles: list   = []
        self._rot_handle              = None   # QGraphicsEllipseItem
        self._rot_handle_line         = None   # QGraphicsLineItem
        self._symbol_bbox_proxy       = None   # QGraphicsPathItem (live preview)
        self._symbol_drag_mode        = None   # 'nw'|'ne'|'sw'|'se'|'rotate'
        self._symbol_orig_w           = 40.0
        self._symbol_orig_h           = 40.0
        self._symbol_orig_rot         = 0.0
        self._symbol_orig_center      = None   # QPointF scene coords
        self._symbol_live_w           = 40.0
        self._symbol_live_h           = 40.0
        self._symbol_live_rot         = 0.0
        self._inline_edit_widget = None

        self._smart_start_pdf   = None   # (pdf_x, pdf_y) first click
        self._smart_end_pdf     = None
        self._smart_paths       = []     # list of paths (each = [[pdf_x,pdf_y],...])
        self._smart_path_idx    = 0
        self._smart_preview     = []     # QGraphicsItem preview items on scene
        self._smart_tracer      = None   # SmartPipeTracer, cached per page
        self._smart_tracer_page = -1

        # Study board: multi-page layout
        self._all_page_items: dict  = {}   # page_idx → QGraphicsPixmapItem
        self._page_offsets: dict    = {}   # page_idx → (ox: float, oy: float)
        self._page_widths_pdf: dict = {}   # page_idx → float PDF width
        self._page_heights_pdf: dict = {}  # page_idx → float PDF height
        self._dragging_page         = None  # page_idx being dragged in MODE_BOARD_LAYOUT
        self._drag_page_start_scene = None  # QPointF where drag began
        self._drag_page_orig_offset = None  # (ox, oy) before drag
        self._add_link_source_page  = None  # page_idx chosen in MODE_ADD_SHEET_LINK

        self._lod_overview = None   # None = unset; True/False = current LOD

        self._placeholder = None
        self._show_placeholder("Öppna en P&ID-fil (PDF) för att börja.")
        self.set_mode(MODE_NAV)
        self.setBackgroundBrush(QBrush(QColor(160, 160, 160)))

    def _show_placeholder(self, text):
        self._clear_placeholder()
        item = self._scene.addSimpleText(text)
        f = QFont(); f.setPointSize(14)
        item.setFont(f)
        item.setBrush(QBrush(QColor(70, 70, 70)))
        item.setZValue(Z_TEMP)
        self._placeholder = item
        self._scene.setSceneRect(item.boundingRect().adjusted(-40, -40, 40, 40))

    def _clear_placeholder(self):
        if self._placeholder is not None:
            try:
                self._scene.removeItem(self._placeholder)
            except Exception:
                pass
            self._placeholder = None

    def __del__(self):
        """Ensure PDF document is properly closed on object destruction."""
        if hasattr(self, 'pdf_doc') and self.pdf_doc:
            try:
                self.pdf_doc.close()
            except Exception:
                pass
            self.pdf_doc = None

    def load_pdf(self, path, page=0, layout_offsets=None, active_pages=None,
                 progress_cb=None, page_rotations=None):
        if not HAS_PYMUPDF:
            self._show_placeholder("Installera PyMuPDF:\n  pip install PyMuPDF")
            return False
        try:
            self.pdf_doc = fitz.open(str(path))
        except Exception as e:
            self._show_placeholder(f"Kunde inte öppna PDF:\n{e}")
            self.pdf_doc = None
            return False
        if self.pdf_doc.page_count == 0:
            self._show_placeholder("PDF saknar sidor.")
            return False
        # A fresh document has no rotation state carried over from whatever
        # was previously open — reset, then repopulate from the caller's
        # persisted overrides (PIDPanel passes db.get_all_page_rotations()).
        self._page_rotation_override.clear()
        self._intrinsic_rotation.clear()
        if page_rotations:
            self._page_rotation_override.update(
                {int(k): int(v) % 360 for k, v in page_rotations.items()})
        self._pdf_path = str(path)
        self._page_cache.clear()
        self._page_cache_scale.clear()
        self._cache_order.clear()
        self._cancel_prefetch()
        self.current_page = max(0, min(page, self.pdf_doc.page_count - 1))
        self._render_all_pages(layout_offsets=layout_offsets, active_pages=active_pages,
                               progress_cb=progress_cb)
        return True

    def _render_page(self):
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return
        self._clear_placeholder()
        page = self.pdf_doc.load_page(self.current_page)
        rect = page.rect
        self.page_rect_width  = float(rect.width)
        self.page_rect_height = float(rect.height)

        if self.page_item is not None:
            try:
                self._scene.removeItem(self.page_item)
            except Exception:
                pass
            self.page_item = None

        pn = self.current_page
        if pn in self._page_cache:
            pixmap = self._page_cache[pn]
            self._update_lru(pn)
        else:
            mat = fitz.Matrix(self._RASTER_SCALE, self._RASTER_SCALE)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = QImage(pix.samples, pix.width, pix.height,
                         pix.stride, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(img.copy())
            self._add_to_cache(pn, pixmap)

        self.page_item = QGraphicsPixmapItem(pixmap)
        self.page_item.setZValue(Z_PAGE)
        self.page_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self._scene.addItem(self.page_item)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self.render_scale = self._RASTER_SCALE

        self._prefetch_adjacent()

    def _render_all_pages(self, layout_offsets=None, active_pages=None,
                          progress_cb=None):
        """Render PDF pages (only active_pages if given) as tiled scene items."""
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return
        self._clear_placeholder()
        self._cancel_lod_render()
        for item in list(self._all_page_items.values()):
            try: self._scene.removeItem(item)
            except RuntimeError as e: logging.warning(f"Failed to remove page item from scene: {e}")
        self._all_page_items.clear()
        self.page_item = None  # Clear reference to avoid double-removal errors
        self._low_pixmaps.clear()
        self._hires_pages.clear()
        self._page_display_scale.clear()
        self._page_offsets.clear()
        self._page_widths_pdf.clear()
        self._page_heights_pdf.clear()
        self.render_scale = self._RASTER_SCALE
        GAP = 100.0  # scene pixels between pages

        pages_to_render = (sorted(active_pages)
                           if active_pages is not None
                           else list(range(self.pdf_doc.page_count)))
        total_pages = len(pages_to_render)
        x_cursor = 0.0
        for render_idx, pn in enumerate(pages_to_render):
            fitz_page = self.pdf_doc.load_page(pn)
            # Compose the user's manual rotation override (if any) with the
            # PDF's own /Rotate BEFORE reading rect/rendering — every
            # coordinate/pixmap derived below (page.rect, get_pixmap()) then
            # reflects it automatically. See _apply_page_rotation().
            self._apply_page_rotation(pn)
            rect = fitz_page.rect
            pw_pdf = float(rect.width)
            ph_pdf = float(rect.height)
            pw_scene = pw_pdf * self.render_scale
            self._page_widths_pdf[pn] = pw_pdf
            self._page_heights_pdf[pn] = ph_pdf

            # Low-res base render: ~36× fewer pixels than _RASTER_SCALE.
            # The item is scaled up so the scene footprint (and every saved
            # coordinate) stays identical to a full-res render.
            mat = fitz.Matrix(self._LOW_SCALE, self._LOW_SCALE)
            pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
            img = QImage(pix.samples, pix.width, pix.height,
                         pix.stride, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(img.copy())
            self._low_pixmaps[pn] = pixmap

            if layout_offsets and pn in layout_offsets:
                ox, oy = float(layout_offsets[pn][0]), float(layout_offsets[pn][1])
            else:
                ox = x_cursor
                oy = 0.0
            x_cursor = ox + pw_scene + GAP
            self._page_offsets[pn] = (ox, oy)

            page_item = QGraphicsPixmapItem(pixmap)
            page_item.setZValue(Z_PAGE)
            page_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            page_item.setScale(self.render_scale / self._LOW_SCALE)
            page_item.setPos(ox, oy)
            self._scene.addItem(page_item)
            self._all_page_items[pn] = page_item

            if progress_cb is not None:
                progress_cb(render_idx + 1, total_pages)
                QApplication.processEvents()

        self.page_item = self._all_page_items.get(self.current_page)
        self.page_rect_width  = self._page_widths_pdf.get(self.current_page, 0.0)
        self.page_rect_height = self._page_heights_pdf.get(self.current_page, 0.0)
        self._update_board_scene_rect()
        self._schedule_lod_update()

    # ── Manual per-page rotation (2026-08-12, see NOTES.md) ───────────────────

    def _apply_page_rotation(self, pn):
        """Compose the user's manual rotation override for physical page `pn`
        with the PDF's own intrinsic /Rotate value, by mutating the
        in-memory fitz.Page's rotation (never written back to the file).

        Every existing rotation-aware code path — page.rect (hence
        page_rect_width/height and the scene footprint), get_pixmap(),
        rotation_matrix/derotation_matrix, and
        equipment_detection._rotate_words — already keys off page.rotation,
        so composing here makes the override transparent to all of them for
        free instead of threading a separate override through each layer.
        """
        if self.pdf_doc is None:
            return
        page = self.pdf_doc.load_page(pn)
        if pn not in self._intrinsic_rotation:
            self._intrinsic_rotation[pn] = page.rotation
        total = (self._intrinsic_rotation[pn] + self._page_rotation_override.get(pn, 0)) % 360
        if page.rotation != total:
            page.set_rotation(total)

    def set_page_rotation_override(self, pn, degrees):
        """Set/replace the manual rotation override (clockwise degrees,
        normalized to 0/90/180/270) for physical page `pn`. Purges any
        cached/displayed raster pixmaps for this page since they were
        rendered at the old orientation and footprint — callers still need
        to re-render (e.g. via _render_all_pages) to see the new page."""
        degrees = int(degrees) % 360
        self._page_rotation_override[pn] = degrees
        self._apply_page_rotation(pn)
        self._page_cache.pop(pn, None)
        self._page_cache_scale.pop(pn, None)
        if pn in self._cache_order:
            self._cache_order.remove(pn)
        self._hires_pages.discard(pn)
        self._page_display_scale.pop(pn, None)
        self._low_pixmaps.pop(pn, None)

    def _update_board_scene_rect(self):
        if not self._page_offsets:
            return
        rs = self.render_scale
        min_x = min(ox for ox, oy in self._page_offsets.values()) - 40
        min_y = min(oy for ox, oy in self._page_offsets.values()) - 40
        max_x = max(ox + self._page_widths_pdf[p]  * rs
                    for p, (ox, oy) in self._page_offsets.items()) + 40
        max_y = max(oy + self._page_heights_pdf[p] * rs
                    for p, (ox, oy) in self._page_offsets.items()) + 40
        self._scene.setSceneRect(QRectF(min_x, min_y, max_x - min_x, max_y - min_y))

    def _cancel_prefetch(self):
        if self._prefetch_thread and self._prefetch_thread.isRunning():
            self._prefetch_thread.requestInterruption()
            self._prefetch_thread.wait(300)

    def _add_to_cache(self, pn, pixmap, scale=None):
        if pn in self._page_cache:
            self._cache_order.remove(pn)
        elif len(self._page_cache) >= self._CACHE_SIZE:
            oldest = self._cache_order.pop(0)
            del self._page_cache[oldest]
            self._page_cache_scale.pop(oldest, None)
        self._page_cache[pn] = pixmap
        self._page_cache_scale[pn] = scale if scale is not None else self._RASTER_SCALE
        self._cache_order.append(pn)

    def _update_lru(self, pn):
        if pn in self._cache_order:
            self._cache_order.remove(pn)
            self._cache_order.append(pn)

    def _prefetch_adjacent(self):
        if not self._pdf_path or self.pdf_doc is None:
            return
        total = self.pdf_doc.page_count
        to_fetch = []
        for offset in (1, -1, 2, -2):
            n = self.current_page + offset
            if 0 <= n < total and n not in self._page_cache:
                to_fetch.append(n)
                if len(to_fetch) >= 2:
                    break
        if not to_fetch:
            return
        self._cancel_prefetch()
        rotations = {n: (self._intrinsic_rotation.get(n, 0) +
                         self._page_rotation_override.get(n, 0)) % 360
                    for n in to_fetch if n in self._page_rotation_override}
        self._prefetch_thread = _PageRenderer(self._pdf_path, to_fetch, self._RASTER_SCALE,
                                              rotations=rotations)
        self._prefetch_thread.page_ready.connect(self._on_page_prefetched)
        self._prefetch_thread.start()

    def _on_page_prefetched(self, pn, raw, width, height, stride, scale):
        if pn not in self._page_cache:
            img = QImage(raw, width, height, stride, QImage.Format.Format_RGB888)
            self._add_to_cache(pn, QPixmap.fromImage(img), scale)

    # ── Page LOD: hi-res swap-in for visible pages at high zoom ──────────────

    def _schedule_lod_update(self):
        """Debounced trigger — call after any zoom/pan/layout change."""
        if self._all_page_items:
            self._lod_timer.start()

    def _cancel_lod_render(self):
        if self._lod_renderer and self._lod_renderer.isRunning():
            self._lod_renderer.requestInterruption()
            self._lod_renderer.wait(300)
        self._lod_renderer = None

    def _hires_worthwhile(self):
        # Hi-res pays off once one low-res pixel covers more than _LOD_MARGIN
        # screen pixels — i.e. the low tier can no longer supply ~1 raw pixel
        # per screen pixel with a small supersampling margin.
        zoom = self.transform().m11()
        return zoom > self._LOD_MARGIN * self._LOW_SCALE / self._RASTER_SCALE

    def _target_raster_scale(self, pw_pdf, ph_pdf):
        """Adaptive re-rasterization (2026-08-12, see NOTES.md "P&ID blir
        suddig vid inzoomning"): the fitz.Matrix scale a page should be
        re-rendered at so its raster stays crisp at the CURRENT view zoom,
        instead of a fixed _RASTER_SCALE that the QGraphicsView transform
        then stretches (blur) once zoom exceeds what it can supply.

        Uses the same "at least _LOD_MARGIN raw pixels per screen pixel"
        standard as _hires_worthwhile(), just applied one tier further up.
        render_scale (the scene-units-per-pdf-point factor every marker/
        click-hit-test coordinate transform assumes, see scene_to_pdf/
        pdf_to_scene) is NEVER changed by this — only which physical
        pixmap resolution is displayed for that same scene footprint
        (see _promote_page's item.setScale compensation).

        Clamped to two safety caps so an extreme zoom can't render an
        absurdly large pixmap: an absolute multiplier (_MAX_RASTER_SCALE)
        and a cap on the resulting pixel dimensions (_MAX_RASTER_DIM),
        whichever is more restrictive for this page's size. Never goes
        below the baseline _RASTER_SCALE.
        """
        zoom = self.transform().m11()
        ideal = zoom * self.render_scale / self._LOD_MARGIN
        ideal = max(ideal, self._RASTER_SCALE)
        dim_cap = self._MAX_RASTER_DIM / max(pw_pdf, ph_pdf, 1.0)
        cap = max(min(self._MAX_RASTER_SCALE, dim_cap), self._RASTER_SCALE)
        return min(ideal, cap)

    # A target scale must exceed what's currently shown/cached by this factor
    # before triggering a re-render — cheap hysteresis so a page that's
    # already crisp enough (or slightly over-crisp from a previous, higher
    # zoom level) isn't re-rendered on every debounce tick.
    _RESCALE_HYSTERESIS = 1.15

    def _update_page_lod(self):
        if not self._all_page_items or self.pdf_doc is None:
            return
        needed = []
        if self._hires_worthwhile():
            vis = self.mapToScene(self.viewport().rect()).boundingRect()
            vis = vis.adjusted(-vis.width() * 0.25, -vis.height() * 0.25,
                               vis.width() * 0.25,  vis.height() * 0.25)
            c = vis.center()
            for pn, item in self._all_page_items.items():
                r = item.sceneBoundingRect()
                if r.intersects(vis):
                    d = abs(r.center().x() - c.x()) + abs(r.center().y() - c.y())
                    needed.append((d, pn))
            needed.sort()
            needed = [pn for _, pn in needed[:self._MAX_HIRES]]
        needed_set = set(needed)

        # Demote pages that left the viewport or are no longer zoomed in
        for pn in list(self._hires_pages):
            if pn not in needed_set:
                item = self._all_page_items.get(pn)
                low  = self._low_pixmaps.get(pn)
                if item is not None and low is not None:
                    item.setPixmap(low)
                    item.setScale(self.render_scale / self._LOW_SCALE)
                self._hires_pages.discard(pn)
                self._page_display_scale.pop(pn, None)

        # Promote/upgrade: a page needs (re-)rendering when what's currently
        # shown (or cached) can't supply the target resolution for the
        # CURRENT zoom — not just once, on the initial low->hires swap.
        to_render = []
        target_scales = {}
        for pn in needed:
            pw = self._page_widths_pdf.get(pn, 0.0)
            ph = self._page_heights_pdf.get(pn, 0.0)
            target = self._target_raster_scale(pw, ph)
            target_scales[pn] = target
            displayed = self._page_display_scale.get(pn, 0.0) if pn in self._hires_pages else 0.0
            if displayed and displayed >= target / self._RESCALE_HYSTERESIS:
                continue  # already crisp enough on screen — nothing to do
            cached_scale = self._page_cache_scale.get(pn, 0.0)
            if pn in self._page_cache and cached_scale >= target / self._RESCALE_HYSTERESIS:
                self._promote_page(pn, self._page_cache[pn], cached_scale)
                self._update_lru(pn)
            else:
                to_render.append(pn)
        if to_render and self._pdf_path:
            # One fitz.Matrix scale per background-render batch — use the
            # smallest of the individually-needed (and already capped)
            # target scales so every page in the batch stays within its own
            # safety cap even if page sizes differ.
            batch_scale = max(min(target_scales[pn] for pn in to_render), self._RASTER_SCALE)
            rotations = {pn: (self._intrinsic_rotation.get(pn, 0) +
                              self._page_rotation_override.get(pn, 0)) % 360
                        for pn in to_render}
            self._cancel_lod_render()
            self._lod_renderer = _PageRenderer(self._pdf_path, to_render,
                                               batch_scale, rotations=rotations)
            self._lod_renderer.page_ready.connect(self._on_lod_page_ready)
            self._lod_renderer.start()

    def _promote_page(self, pn, pixmap, scale=None):
        item = self._all_page_items.get(pn)
        if item is None:
            return
        scale = scale if scale is not None else self._RASTER_SCALE
        item.setPixmap(pixmap)
        # Compensate so the item's scene footprint (render_scale per PDF
        # point) never changes regardless of which raster tier is shown —
        # this is what keeps scene_to_pdf/pdf_to_scene and every marker
        # coordinate correct across adaptive re-rasterization.
        item.setScale(self.render_scale / scale)
        self._hires_pages.add(pn)
        self._page_display_scale[pn] = scale

    def _on_lod_page_ready(self, pn, raw, width, height, stride, scale):
        img = QImage(raw, width, height, stride, QImage.Format.Format_RGB888)
        pm  = QPixmap.fromImage(img)
        self._add_to_cache(pn, pm, scale)
        # Zoom may have changed while rendering — only promote if still useful;
        # a follow-up _update_page_lod demotes/upgrades anything now stale.
        if self._hires_worthwhile():
            self._promote_page(pn, pm, scale)

    def page_count(self):
        return self.pdf_doc.page_count if self.pdf_doc else 0

    def goto_page(self, n):
        if self.pdf_doc is None:
            return
        n = max(0, min(n, self.pdf_doc.page_count - 1))
        self.current_page = n
        self.page_item = self._all_page_items.get(n)
        self.page_rect_width  = self._page_widths_pdf.get(n, 0.0)
        self.page_rect_height = self._page_heights_pdf.get(n, 0.0)
        self._cancel_drawing()
        # Pre-extract PDF lines for smart snapping (background cache)
        QTimer.singleShot(CONFIG['TIMER_PDF_EXTRACT_MS'],
                         partial(self._extract_pdf_lines_for_page, n))
        if n in self._page_offsets:
            ox, oy = self._page_offsets[n]
            rs = self.render_scale
            cx = ox + self._page_widths_pdf.get(n, 0.0) * rs / 2
            cy = oy + self._page_heights_pdf.get(n, 0.0) * rs / 2
            self.centerOn(QPointF(cx, cy))
            self._schedule_lod_update()
        else:
            self._render_all_pages()

    def scene_to_pdf(self, point):
        p = self._hit_test_page(point)
        ox, oy = self._page_offsets.get(p, (0.0, 0.0))
        rs = self.render_scale
        return ((point.x() - ox) / rs, (point.y() - oy) / rs)

    def pdf_to_scene(self, x, y, page=None):
        p = page if page is not None else self.current_page
        ox, oy = self._page_offsets.get(p, (0.0, 0.0))
        return QPointF(x * self.render_scale + ox, y * self.render_scale + oy)

    def _hit_test_page(self, scene_pt):
        """Return the page index whose rendered area contains scene_pt, or current_page."""
        rs = self.render_scale
        for pn, (ox, oy) in self._page_offsets.items():
            pw = self._page_widths_pdf.get(pn, 0) * rs
            ph = self._page_heights_pdf.get(pn, 0) * rs
            if ox <= scene_pt.x() < ox + pw and oy <= scene_pt.y() < oy + ph:
                return pn
        return self.current_page

    def _purge_rubber_band_state(self):
        """Remove any dangling rubber-band scene items and reset drag state.

        Called from both set_mode() and clear_overlays() so there is a single
        canonical cleanup location; add new rubber-band attributes here only.
        """
        for attr in ('_rband_preview_item', '_rband_label_item',
                     '_ctrl_rband_preview_item', '_ctrl_rband_count_label'):
            item = getattr(self, attr, None)
            if item is not None:
                try:
                    self._scene.removeItem(item)
                except Exception:
                    pass
                setattr(self, attr, None)
        self._rband_start_scene      = None
        self._rband_dragging         = False
        self._ctrl_rband_start_scene = None
        self._ctrl_rband_dragging    = False

    def set_mode(self, mode):
        self._purge_rubber_band_state()

        self.mode = mode
        if mode == MODE_NAV:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        elif mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE,
                      MODE_MARKUP_TEXT, MODE_MARKUP_COMMENT):
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_MARKUP_SELECT:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self._clear_edit_handles()
        elif mode == MODE_PICK_REF_TAG:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_ANNOTATION:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_SMART_POLYLINE:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
            self._cancel_smart()
        elif mode == MODE_RED_MARKUP_SYMBOL:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif mode == MODE_BOARD_LAYOUT:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        elif mode == MODE_ADD_SHEET_LINK:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.setCursor(Qt.CursorShape.CrossCursor)
        # Cancel any in-progress freehand draw when leaving draw-modes.
        # MODE_SMART_POLYLINE is only excluded when we're staying in it
        # (e.g. toggling a sub-option); switching *into* it from another
        # mode should still cancel the previous draw.
        staying_in_draw = (mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE)
                           or (mode == MODE_SMART_POLYLINE
                               and self.mode == MODE_SMART_POLYLINE))
        if not staying_in_draw:
            self._cancel_drawing()
        self.setFocus()

    def set_pen_style(self, color, width, alpha):
        c = QColor(color)
        self.draw_pen = QPen(QColor(c.red(), c.green(), c.blue(), alpha), width)
        self.draw_pen.setCosmetic(True)
        self.draw_brush = QBrush(QColor(c.red(), c.green(), c.blue(), max(30, alpha // 4)))

    def set_snap(self, enabled: bool):
        self._snap_enabled = enabled

    def _add_draw_point(self, sp):
        self.draw_points.append(sp)
        r = max(4.0, self.draw_pen.widthF() * 0.6)
        dot = self._scene.addEllipse(
            sp.x() - r, sp.y() - r, 2 * r, 2 * r,
            QPen(Qt.PenStyle.NoPen), QBrush(self.draw_pen.color())
        )
        dot.setZValue(Z_TEMP)
        self.temp_items.append(dot)
        if len(self.draw_points) >= 2:
            p0  = self.draw_points[-2]
            seg = self._scene.addLine(p0.x(), p0.y(), sp.x(), sp.y(), self.draw_pen)
            seg.setZValue(Z_TEMP)
            self.temp_items.append(seg)

    def _update_rubber_band(self, sp):
        if not self.draw_points:
            return
        last = self.draw_points[-1]
        pen  = QPen(self.draw_pen); pen.setStyle(Qt.PenStyle.DashLine)
        if self.rubber_line is None:
            self.rubber_line = self._scene.addLine(
                last.x(), last.y(), sp.x(), sp.y(), pen)
            self.rubber_line.setZValue(Z_TEMP)
        else:
            self.rubber_line.setPen(pen)
            self.rubber_line.setLine(last.x(), last.y(), sp.x(), sp.y())

    def _finish_drawing(self):
        """Legacy finish for MODE_NODE — creates node boundary polygon."""
        self._finish_markup_drawing()

    def _finish_markup_drawing(self):
        """Finish drawing for MODE_NODE, MARKUP_POLYGON and MARKUP_POLYLINE."""
        if len(self.draw_points) < 2:
            self._cancel_drawing()
            return
        mode = self.mode
        path = QPainterPath()
        path.moveTo(self.draw_points[0])
        for pt in self.draw_points[1:]:
            path.lineTo(pt)
        if mode in (MODE_NODE, MODE_MARKUP_POLYGON):
            path.closeSubpath()

        pdf_points = [list(self.scene_to_pdf(pt)) for pt in self.draw_points]
        self._remove_temp_items()

        item = QGraphicsPathItem(path)
        item.setPen(self.draw_pen)
        item.setBrush(self.draw_brush if mode in (MODE_NODE, MODE_MARKUP_POLYGON)
                      else QBrush(Qt.BrushStyle.NoBrush))
        item.setZValue(Z_OVERLAY)
        self._scene.addItem(item)
        self._pending_path_item = item

        self.draw_points = []
        if mode == MODE_NODE:
            self.node_markup_finished.emit(pdf_points, self.current_page)
        elif mode == MODE_MARKUP_POLYGON:
            self.markup_draw_finished.emit('polygon', pdf_points, self.current_page)
        elif mode == MODE_MARKUP_POLYLINE:
            self.markup_draw_finished.emit('polyline', pdf_points, self.current_page)

    def _cancel_drawing(self):
        self._remove_temp_items()
        self.draw_points = []

    def _remove_temp_items(self):
        for item in self.temp_items:
            try: self._scene.removeItem(item)
            except RuntimeError as e: logging.warning(f"Failed to remove temp item from scene: {e}")
        self.temp_items = []
        if self.rubber_line is not None:
            try: self._scene.removeItem(self.rubber_line)
            except RuntimeError as e: logging.warning(f"Failed to remove rubber line from scene: {e}")
            self.rubber_line = None

    def add_node_overlay(self, node_id, points_pdf, style, label):
        if not points_pdf:
            return
        self._pending_path_item = None
        color = QColor(style.get('color', '#FF8C00'))
        width = int(style.get('width', 3))
        alpha = int(style.get('alpha', 220))
        pen   = QPen(QColor(color.red(), color.green(), color.blue(), alpha), width)
        pen.setCosmetic(True)
        brush = QBrush(QColor(color.red(), color.green(), color.blue(), max(25, alpha // 5)))

        path = QPainterPath()
        first = self.pdf_to_scene(*points_pdf[0])
        path.moveTo(first)
        for x, y in points_pdf[1:]:
            path.lineTo(self.pdf_to_scene(x, y))
        path.closeSubpath()

        item = QGraphicsPathItem(path)
        item.setPen(pen); item.setBrush(brush); item.setZValue(Z_OVERLAY)
        item.setToolTip(label or '')
        self._scene.addItem(item)

        if label:
            cx = sum(p[0] for p in points_pdf) / len(points_pdf)
            cy = sum(p[1] for p in points_pdf) / len(points_pdf)
            center = self.pdf_to_scene(cx, cy)
            txt = QGraphicsSimpleTextItem(label)
            f = QFont(); f.setBold(True); f.setPointSize(11)
            txt.setFont(f)
            txt.setBrush(QBrush(QColor(30, 30, 30)))
            br = txt.boundingRect()
            txt.setPos(center.x() - br.width() / 2, center.y() - br.height() / 2)
            txt.setZValue(Z_OVERLAY + 1)
            self._scene.addItem(txt)

    # ── Node markup overlays ──────────────────────────────────────────────────

    def add_markup_overlay(self, mu_id, type_, points_pdf, label,
                           color_hex, opacity, line_width, visible=True, font_size=12,
                           opaque_fill=False, symbol_svg=None,
                           symbol_w=40, symbol_h=40, symbol_rot=0,
                           _items_dict=None):
        """Render a node_markup or red_markup item.  type_: polygon|polyline|text|comment|symbol"""
        if _items_dict is None:
            _items_dict = self._markup_items
        items = []
        c = QColor(color_hex)
        border_alpha = int(opacity * 210)
        fill_alpha   = int(opacity * 210) if opaque_fill else int(opacity * 52)
        # Non-cosmetic pen: width in scene coords so lines scale proportionally with zoom
        pen = QPen(QColor(c.red(), c.green(), c.blue(), border_alpha), line_width)

        if type_ in ('polygon', 'polyline') and len(points_pdf) >= 2:
            path = QPainterPath()
            first = self.pdf_to_scene(*points_pdf[0])
            path.moveTo(first)
            for p in points_pdf[1:]:
                path.lineTo(self.pdf_to_scene(*p))
            if type_ == 'polygon':
                path.closeSubpath()
                brush = QBrush(QColor(c.red(), c.green(), c.blue(), fill_alpha))
            else:
                brush = QBrush(Qt.BrushStyle.NoBrush)
            gi = QGraphicsPathItem(path)
            gi.setPen(pen); gi.setBrush(brush)
            gi.setZValue(Z_OVERLAY)
            gi.setData(self._DATA_MARKUP_ID, mu_id)
            gi.setData(self._DATA_TYPE, 'markup')
            gi.setData(self._DATA_MARKUP_PTS, points_pdf)
            gi.setCursor(Qt.CursorShape.PointingHandCursor)
            gi.setToolTip(f"Klicka för att markera  [{type_}]" + (f": {label}" if label else ""))
            self._scene.addItem(gi)
            items.append(gi)
            if label and type_ == 'polygon':
                cx = sum(p[0] for p in points_pdf) / len(points_pdf)
                cy = sum(p[1] for p in points_pdf) / len(points_pdf)
                items.extend(self._add_markup_label(mu_id, label, cx, cy, c, border_alpha,
                                                    font_size))

        elif type_ in ('text', 'comment') and len(points_pdf) >= 1:
            px, py = points_pdf[0]
            items.extend(self._add_markup_text_item(mu_id, type_, label or '?',
                                                     px, py, c, opacity, line_width, font_size))
            if items:
                items[0].setData(self._DATA_MARKUP_PTS, [[points_pdf[0][0], points_pdf[0][1]]])

        elif type_ == 'symbol' and symbol_svg and len(points_pdf) >= 1:
            items.extend(self._add_markup_symbol_item(
                mu_id, symbol_svg, points_pdf[0], c, opacity,
                symbol_w, symbol_h, symbol_rot, label=label))
            if items:
                items[0].setData(self._DATA_MARKUP_PTS, [[points_pdf[0][0], points_pdf[0][1]]])

        _items_dict[mu_id] = items
        if _items_dict is self._markup_items:
            self._markup_types[mu_id] = type_
        else:
            self._red_markup_types[mu_id] = type_
        if not visible:
            for gi in items:
                gi.setVisible(False)

    def _add_markup_label(self, mu_id, label, cx_pdf, cy_pdf, color, alpha, font_size=12):
        center = self.pdf_to_scene(cx_pdf, cy_pdf)
        txt = QGraphicsSimpleTextItem(label)
        f = QFont(); f.setBold(True); f.setPointSize(font_size)
        txt.setFont(f)
        txt.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(),
                                   min(255, alpha + 30))))
        br = txt.boundingRect()
        txt.setPos(center.x() - br.width() / 2, center.y() - br.height() / 2)
        txt.setZValue(Z_OVERLAY + 1)
        txt.setData(self._DATA_MARKUP_ID, mu_id)
        txt.setData(self._DATA_TYPE, 'markup')
        self._scene.addItem(txt)
        return [txt]

    def _add_markup_text_item(self, mu_id, type_, label, px_pdf, py_pdf,
                              color, opacity, line_width, font_size=12):
        pos = self.pdf_to_scene(px_pdf, py_pdf)
        txt = QGraphicsSimpleTextItem(label)
        f = QFont()
        if type_ == 'comment':
            f.setItalic(True)
        f.setPointSize(font_size)
        txt.setFont(f)
        txt.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(),
                                   int(opacity * 230))))
        br = txt.boundingRect()
        txt.setPos(pos.x(), pos.y())
        txt.setZValue(Z_OVERLAY + 1)
        txt.setData(self._DATA_MARKUP_ID, mu_id)
        txt.setData(self._DATA_TYPE, 'markup')
        items = []
        if type_ == 'comment':
            # Draw a slightly rounded rect behind the text
            pad = 5
            bg_alpha = int(opacity * 90)
            border_alpha = int(opacity * 200)
            bg = QGraphicsRectItem(pos.x() - pad, pos.y() - pad,
                                   br.width() + 2*pad, br.height() + 2*pad)
            bg.setPen(QPen(QColor(color.red(), color.green(), color.blue(), border_alpha),
                           max(1, line_width - 1)))
            bg.setBrush(QBrush(QColor(255, 255, 200, bg_alpha)))
            bg.setZValue(Z_OVERLAY)
            bg.setData(self._DATA_MARKUP_ID, mu_id)
            bg.setData(self._DATA_TYPE, 'markup')
            bg.setCursor(Qt.CursorShape.PointingHandCursor)
            self._scene.addItem(bg)
            items.append(bg)
        self._scene.addItem(txt)
        items.append(txt)
        return items

    def _line_segments_intersect(self, p1, p2, p3, p4):
        """Check if line segment p1-p2 intersects p3-p4. Return (True, intersection_point) or (False, None)."""
        x1, y1 = p1[0], p1[1]
        x2, y2 = p2[0], p2[1]
        x3, y3 = p3[0], p3[1]
        x4, y4 = p4[0], p4[1]

        denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(denom) < 1e-10:
            return False, None

        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
        u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom

        if 0 <= t <= 1 and 0 <= u <= 1:
            ix = x1 + t * (x2 - x1)
            iy = y1 + t * (y2 - y1)
            return True, (ix, iy)
        return False, None

    def _get_boundary_crossings(self, boundary_polygon, pdf_lines):
        """Find all points where PDF lines cross the boundary polygon.
        Returns list of (crossing_point, line_seg) tuples."""
        crossings = []

        # Create boundary segments
        boundary_segs = []
        for i in range(len(boundary_polygon)):
            p1 = boundary_polygon[i]
            p2 = boundary_polygon[(i + 1) % len(boundary_polygon)]
            boundary_segs.append((p1, p2))

        # Find intersections
        for line_seg in pdf_lines:
            x0, y0, x1, y1 = line_seg
            for b_p1, b_p2 in boundary_segs:
                intersects, pt = self._line_segments_intersect((x0, y0), (x1, y1), b_p1, b_p2)
                if intersects and pt:
                    crossings.append((pt, line_seg))

        return crossings

    def _closest_point_on_line_segment(self, p, line_seg):
        """Find closest point on a line segment to point p. Returns (closest_point, distance)."""
        x, y = p.x(), p.y()
        x0, y0, x1, y1 = line_seg

        # Vector from start to end
        dx = x1 - x0
        dy = y1 - y0

        # If line segment is a point
        if dx == 0 and dy == 0:
            dist = ((x - x0) ** 2 + (y - y0) ** 2) ** 0.5
            return QPointF(x0, y0), dist

        # Parameter t for the projection onto the line
        t = ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)
        t = max(0, min(1, t))  # Clamp to [0, 1] to stay on segment

        # Closest point on segment
        closest_x = x0 + t * dx
        closest_y = y0 + t * dy

        dist = ((x - closest_x) ** 2 + (y - closest_y) ** 2) ** 0.5
        return QPointF(closest_x, closest_y), dist

    def _extract_pdf_lines_for_page(self, page_num):
        """Extract all visible line segments from a PDF page for snapping.

        Was previously always returning [] — get_drawings() returns plain
        dicts, so the old hasattr(path, 'rects')/hasattr(path, 'lines')
        checks were always False. Now delegates to the shared primitive
        extractor in symbol_geometry.py (also used for equipment-symbol
        clustering), so there is one source of truth for "what vector
        geometry is really on this page".
        """
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return []
        if page_num in self._pdf_line_segments:
            return self._pdf_line_segments[page_num]

        try:
            page = self.pdf_doc[page_num]
            self._pdf_line_segments[page_num] = symbol_geometry.extract_line_segments(page)
        except Exception:
            self._pdf_line_segments[page_num] = []

        return self._pdf_line_segments[page_num]

    def _snap_to_nearest(self, scene_pos):
        """Smart snapping: PDF lines (priority), markup paths, markers, and in-progress points."""
        if not self._snap_enabled:
            return scene_pos
        SNAP_PX = 18.0
        best_dist = SNAP_PX
        best_pos = scene_pos

        # Convert scene position to PDF coordinates for line snapping
        pdf_pos = self.scene_to_pdf(scene_pos)
        pdf_x, pdf_y = pdf_pos[0], pdf_pos[1]
        pdf_pt = QPointF(pdf_x, pdf_y)

        # 1. PRIORITY: Snap to PDF drawing details (lines, rectangles) — MOST IMPORTANT
        pdf_lines = self._extract_pdf_lines_for_page(self.current_page)
        for line_seg in pdf_lines:
            closest_pdf_pt, dist = self._closest_point_on_line_segment(pdf_pt, line_seg)
            if dist < best_dist:
                best_dist = dist
                # Convert back to scene coordinates
                best_pos = self.pdf_to_scene(closest_pdf_pt.x(), closest_pdf_pt.y())

        # 2. Snap to existing node markup path points
        for mu_id, items in self._markup_items.items():
            for gi in items:
                if not isinstance(gi, QGraphicsPathItem):
                    continue
                path = gi.path()
                for i in range(path.elementCount()):
                    el = path.elementAt(i)
                    pt = QPointF(el.x, el.y)
                    dx = pt.x() - scene_pos.x()
                    dy = pt.y() - scene_pos.y()
                    dist = (dx * dx + dy * dy) ** 0.5
                    if dist < best_dist:
                        best_dist = dist
                        best_pos = pt

        # 3. Snap to cause/consequence/safeguard marker centers
        for marker_type in ('cause', 'consequence', 'safeguard'):
            for item in self._type_items.get(marker_type, []):
                center = item.pos() + item.boundingRect().center()
                dx = center.x() - scene_pos.x()
                dy = center.y() - scene_pos.y()
                dist = (dx * dx + dy * dy) ** 0.5
                if dist < best_dist:
                    best_dist = dist
                    best_pos = center

        # 4. Snap to in-progress draw points
        for pt in self.draw_points:
            dx = pt.x() - scene_pos.x()
            dy = pt.y() - scene_pos.y()
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_pos = pt

        return best_pos

    def clear_markup_overlays(self):
        """Remove all node markup overlay items from the scene."""
        self._clear_edit_handles()
        self._markup_types.clear()
        for mu_id, items in self._markup_items.items():
            for gi in items:
                try: self._scene.removeItem(gi)
                except RuntimeError as e: logging.warning(f"Failed to remove markup item from scene: {e}")
        self._markup_items.clear()
        self._markup_highlighted = -1

    def set_markup_item_visible(self, mu_id, visible):
        for gi in self._markup_items.get(mu_id, []):
            try: gi.setVisible(visible)
            except RuntimeError as e: logging.warning(f"Failed to set markup item visibility: {e}")

    # ── Red markup overlay methods ────────────────────────────────────────────

    def add_red_markup_overlay(self, mu_id, type_, points_pdf, label,
                               color_hex, opacity, line_width, visible=True, font_size=12,
                               symbol_w=40, symbol_h=40, symbol_rot=0):
        """Render a red markup item (same as node markup but fully filled and supports symbols)."""
        svg = _get_red_symbol_svg(label) if type_ == 'symbol' else None
        self.add_markup_overlay(
            mu_id, type_, points_pdf, label, color_hex, opacity, line_width,
            visible, font_size, opaque_fill=True, symbol_svg=svg,
            symbol_w=symbol_w, symbol_h=symbol_h, symbol_rot=symbol_rot,
            _items_dict=self._red_markup_items)

    def clear_red_markup_overlays(self):
        """Remove all red markup overlay items from the scene."""
        self._clear_edit_handles()
        self._red_markup_types.clear()
        for mu_id, items in self._red_markup_items.items():
            for gi in items:
                try: self._scene.removeItem(gi)
                except RuntimeError as e: logging.warning(f"Failed to remove red markup item from scene: {e}")
        self._red_markup_items.clear()

    def set_red_markup_item_visible(self, mu_id, visible):
        for gi in self._red_markup_items.get(mu_id, []):
            try: gi.setVisible(visible)
            except RuntimeError as e: logging.warning(f"Failed to set red markup item visibility: {e}")

    def _add_markup_symbol_item(self, mu_id, svg_str, pos_pdf, color, opacity,
                                symbol_w=40, symbol_h=40, symbol_rot=0, label=''):
        """Render an SVG symbol at pos_pdf with given PDF-unit size and rotation."""
        if not HAS_SVG_RENDERER or QSvgRenderer is None:
            return []
        # Replace placeholder color in SVG with user's chosen color
        colored_svg = svg_str.replace('"red"', f'"{color.name()}"')
        colored_svg = colored_svg.replace("'red'", f"'{color.name()}'")
        renderer = QSvgRenderer()
        renderer.load(colored_svg.encode('utf-8'))
        if not renderer.isValid():
            return []
        # Compute scene coords and size
        scene_pos = self.pdf_to_scene(*pos_pdf)
        scene_pt2 = self.pdf_to_scene(pos_pdf[0] + symbol_w, pos_pdf[1] + symbol_h)
        sw = abs(scene_pt2.x() - scene_pos.x())
        sh = abs(scene_pt2.y() - scene_pos.y())
        pm = QPixmap(max(1, int(sw * 2)), max(1, int(sh * 2)))
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        alpha_val = int(opacity * 255)
        painter.setOpacity(alpha_val / 255.0)
        renderer.render(painter)
        painter.end()
        gi = QGraphicsPixmapItem(pm)
        gi.setTransformOriginPoint(sw, sh)  # center
        gi.setRotation(symbol_rot)
        gi.setPos(scene_pos.x() - sw / 2, scene_pos.y() - sh / 2)
        gi.setScale(0.5)  # we rendered at 2× for crispness
        gi.setZValue(Z_OVERLAY)
        gi.setData(self._DATA_MARKUP_ID, mu_id)
        gi.setData(self._DATA_TYPE, 'red_markup')
        gi.setData(self._DATA_MARKUP_PTS, [[pos_pdf[0], pos_pdf[1]]])
        gi.setData(self._DATA_SYMBOL_W, float(symbol_w))
        gi.setData(self._DATA_SYMBOL_H, float(symbol_h))
        gi.setData(self._DATA_SYMBOL_ROT, float(symbol_rot))
        gi.setCursor(Qt.CursorShape.PointingHandCursor)
        gi.setToolTip(f"Symbol: {label}" if label else "P&ID-symbol")
        self._scene.addItem(gi)
        return [gi]

    def _clear_edit_handles(self):
        for h in self._vertex_handles:
            try: self._scene.removeItem(h)
            except RuntimeError as e: logging.warning(f"Failed to remove vertex handle: {e}")
        for h in self._corner_handles:
            try: self._scene.removeItem(h)
            except RuntimeError as e: logging.warning(f"Failed to remove corner handle: {e}")
        if self._rot_handle is not None:
            try: self._scene.removeItem(self._rot_handle)
            except RuntimeError as e: logging.warning(f"Failed to remove rotation handle: {e}")
            self._rot_handle = None
        if self._rot_handle_line is not None:
            try: self._scene.removeItem(self._rot_handle_line)
            except RuntimeError as e: logging.warning(f"Failed to remove rotation handle line: {e}")
            self._rot_handle_line = None
        if self._symbol_bbox_proxy is not None:
            try: self._scene.removeItem(self._symbol_bbox_proxy)
            except RuntimeError as e: logging.warning(f"Failed to remove symbol bounding box: {e}")
            self._symbol_bbox_proxy = None
        self._corner_handles          = []
        self._vertex_handles          = []
        self._edit_mu_id              = None
        self._drag_mode               = None
        self._drag_vertex_idx         = None
        self._drag_original_pts       = []
        self._drag_current_pts        = []
        self._drag_item_origins       = []
        self._drag_threshold_exceeded = False
        self._symbol_drag_mode        = None

    def _select_for_edit(self, mu_id):
        """Select a markup item and show vertex handles."""
        self._clear_edit_handles()
        self._edit_mu_id = mu_id

        # Look in both node markup and red markup dicts
        items_dict = (self._markup_items if mu_id in self._markup_items
                      else self._red_markup_items)
        types_dict = (self._markup_types if mu_id in self._markup_types
                      else self._red_markup_types)

        pts_pdf = None
        for gi in items_dict.get(mu_id, []):
            pts_pdf = gi.data(self._DATA_MARKUP_PTS)
            if pts_pdf:
                break
        if not pts_pdf:
            return

        pts_scene = [self.pdf_to_scene(*p) for p in pts_pdf]
        self._drag_current_pts = list(pts_scene)

        typ = types_dict.get(mu_id, 'polygon')

        if typ in ('polygon', 'polyline'):
            HANDLE_R = 5
            for pt in pts_scene:
                h = QGraphicsEllipseItem(-HANDLE_R, -HANDLE_R, HANDLE_R * 2, HANDLE_R * 2)
                h.setBrush(QBrush(QColor(255, 255, 255, 220)))
                h.setPen(QPen(QColor(30, 120, 230), 1.5))
                h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
                h.setPos(pt)
                h.setZValue(Z_OVERLAY + 5)
                self._scene.addItem(h)
                self._vertex_handles.append(h)
        elif typ in ('text', 'comment'):
            self._drag_item_origins = [
                (gi, QPointF(gi.pos()))
                for gi in items_dict.get(mu_id, [])
            ]
        elif typ == 'symbol':
            sw_pdf = sh_pdf = rot_deg = None
            for gi in items_dict.get(mu_id, []):
                sw_pdf  = gi.data(self._DATA_SYMBOL_W)
                sh_pdf  = gi.data(self._DATA_SYMBOL_H)
                rot_deg = gi.data(self._DATA_SYMBOL_ROT)
                break
            sw_pdf  = float(sw_pdf  if sw_pdf  is not None else 40.0)
            sh_pdf  = float(sh_pdf  if sh_pdf  is not None else 40.0)
            rot_deg = float(rot_deg if rot_deg is not None else 0.0)
            cx, cy = pts_scene[0].x(), pts_scene[0].y()
            self._symbol_orig_w      = sw_pdf
            self._symbol_orig_h      = sh_pdf
            self._symbol_orig_rot    = rot_deg
            self._symbol_orig_center = QPointF(cx, cy)
            self._symbol_live_w      = sw_pdf
            self._symbol_live_h      = sh_pdf
            self._symbol_live_rot    = rot_deg
            self._add_symbol_handles(cx, cy, sw_pdf, sh_pdf, rot_deg)
            self._drag_item_origins = [
                (gi, QPointF(gi.pos()))
                for gi in items_dict.get(mu_id, [])
            ]

        self.highlight_markup(mu_id)

    # ── Symbol resize/rotate helpers ─────────────────────────────────────────

    def _sym_rpt(self, cx, cy, lx, ly, rot_deg):
        """Rotate local (lx,ly) by rot_deg around (cx,cy) in screen coords."""
        a = math.radians(rot_deg)
        ca, sa = math.cos(a), math.sin(a)
        return QPointF(cx + lx * ca - ly * sa, cy + lx * sa + ly * ca)

    def _add_symbol_handles(self, cx, cy, sw_pdf, sh_pdf, rot_deg):
        """Create resize corner handles and rotation handle for a symbol."""
        rs = self.render_scale
        sw = sw_pdf * rs
        sh = sh_pdf * rs
        SZ = 5
        ROT_DIST = max(22.0, sh / 2 + 18.0)
        corners_local = [(-sw/2, -sh/2), (sw/2, -sh/2), (-sw/2, sh/2), (sw/2, sh/2)]
        for lx, ly in corners_local:
            pt = self._sym_rpt(cx, cy, lx, ly, rot_deg)
            h = QGraphicsRectItem(-SZ, -SZ, SZ * 2, SZ * 2)
            h.setBrush(QBrush(QColor(255, 140, 0, 220)))
            h.setPen(QPen(QColor(180, 80, 0), 1.5))
            h.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
            h.setPos(pt)
            h.setZValue(Z_OVERLAY + 6)
            self._scene.addItem(h)
            self._corner_handles.append(h)
        rot_pt = self._sym_rpt(cx, cy, 0, -ROT_DIST, rot_deg)
        line = QGraphicsLineItem(cx, cy, rot_pt.x(), rot_pt.y())
        line.setPen(QPen(QColor(100, 80, 200, 160), 1.5, Qt.PenStyle.DashLine))
        line.setZValue(Z_OVERLAY + 5)
        self._scene.addItem(line)
        self._rot_handle_line = line
        ROT_R = 6
        rh = QGraphicsEllipseItem(-ROT_R, -ROT_R, ROT_R * 2, ROT_R * 2)
        rh.setBrush(QBrush(QColor(100, 80, 200, 200)))
        rh.setPen(QPen(QColor(60, 40, 180), 1.5))
        rh.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        rh.setPos(rot_pt)
        rh.setZValue(Z_OVERLAY + 6)
        self._scene.addItem(rh)
        self._rot_handle = rh

    def _update_symbol_handles(self, cx, cy, sw_pdf, sh_pdf, rot_deg):
        """Reposition corner handles and rotation handle to match new symbol state."""
        rs = self.render_scale
        sw = sw_pdf * rs
        sh = sh_pdf * rs
        ROT_DIST = max(22.0, sh / 2 + 18.0)
        corners_local = [(-sw/2, -sh/2), (sw/2, -sh/2), (-sw/2, sh/2), (sw/2, sh/2)]
        for i, (lx, ly) in enumerate(corners_local):
            if i < len(self._corner_handles):
                self._corner_handles[i].setPos(self._sym_rpt(cx, cy, lx, ly, rot_deg))
        rot_pt = self._sym_rpt(cx, cy, 0, -ROT_DIST, rot_deg)
        if self._rot_handle is not None:
            self._rot_handle.setPos(rot_pt)
        if self._rot_handle_line is not None:
            self._rot_handle_line.setLine(cx, cy, rot_pt.x(), rot_pt.y())

    def _update_symbol_bbox_proxy(self, cx, cy, sw_pdf, sh_pdf, rot_deg):
        """Show/update the dashed bounding-box preview during resize/rotate."""
        rs = self.render_scale
        sw = sw_pdf * rs
        sh = sh_pdf * rs
        corners = [
            self._sym_rpt(cx, cy, -sw/2, -sh/2, rot_deg),
            self._sym_rpt(cx, cy,  sw/2, -sh/2, rot_deg),
            self._sym_rpt(cx, cy,  sw/2,  sh/2, rot_deg),
            self._sym_rpt(cx, cy, -sw/2,  sh/2, rot_deg),
        ]
        path = QPainterPath()
        path.moveTo(corners[0])
        for p in corners[1:]:
            path.lineTo(p)
        path.closeSubpath()
        if self._symbol_bbox_proxy is None:
            proxy = QGraphicsPathItem(path)
            proxy.setPen(QPen(QColor(255, 140, 0, 200), 2.0, Qt.PenStyle.DashLine))
            proxy.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            proxy.setZValue(Z_OVERLAY + 4)
            self._scene.addItem(proxy)
            self._symbol_bbox_proxy = proxy
        else:
            self._symbol_bbox_proxy.setPath(path)

    def _do_symbol_transform(self, sp):
        """Apply live resize or rotate of symbol during handle drag."""
        cx = self._symbol_orig_center.x()
        cy = self._symbol_orig_center.y()
        dx = sp.x() - cx
        dy = sp.y() - cy
        rs = self.render_scale
        if self._symbol_drag_mode == 'rotate':
            self._symbol_live_rot = math.degrees(math.atan2(dy, dx)) + 90.0
        else:
            a = math.radians(self._symbol_live_rot)
            ca, sa = math.cos(a), math.sin(a)
            lx =  dx * ca + dy * sa   # inverse rotation
            ly = -dx * sa + dy * ca
            min_pdf = 10.0
            if self._symbol_drag_mode == 'se':
                self._symbol_live_w = max(min_pdf, lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, ly * 2 / rs)
            elif self._symbol_drag_mode == 'nw':
                self._symbol_live_w = max(min_pdf, -lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, -ly * 2 / rs)
            elif self._symbol_drag_mode == 'ne':
                self._symbol_live_w = max(min_pdf, lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, -ly * 2 / rs)
            elif self._symbol_drag_mode == 'sw':
                self._symbol_live_w = max(min_pdf, -lx * 2 / rs)
                self._symbol_live_h = max(min_pdf, ly * 2 / rs)
        self._update_symbol_bbox_proxy(
            cx, cy, self._symbol_live_w, self._symbol_live_h, self._symbol_live_rot)
        self._update_symbol_handles(
            cx, cy, self._symbol_live_w, self._symbol_live_h, self._symbol_live_rot)

    def _finish_symbol_transform(self):
        """Save new symbol dims/rotation, emit signal for DB save."""
        mu_id = self._edit_mu_id
        if mu_id is None:
            return
        # Update stored data on item so it survives until the next render
        items_dict = (self._red_markup_items if mu_id in self._red_markup_items
                      else self._markup_items)
        for gi in items_dict.get(mu_id, []):
            gi.setData(self._DATA_SYMBOL_W,   self._symbol_live_w)
            gi.setData(self._DATA_SYMBOL_H,   self._symbol_live_h)
            gi.setData(self._DATA_SYMBOL_ROT, self._symbol_live_rot)
        self.markup_symbol_dims_changed.emit(
            mu_id,
            float(self._symbol_live_w),
            float(self._symbol_live_h),
            float(self._symbol_live_rot))
        self._clear_edit_handles()

    # ── Edit path / drag helpers ──────────────────────────────────────────────

    def _update_edit_path(self, pts_scene):
        """Rebuild the path/positions of the currently edited markup."""
        mu_id = self._edit_mu_id
        if mu_id not in self._markup_items and mu_id not in self._red_markup_items:
            return
        items_dict = (self._markup_items if mu_id in self._markup_items
                      else self._red_markup_items)
        types_dict = (self._markup_types if mu_id in self._markup_types
                      else self._red_markup_types)
        typ = types_dict.get(mu_id, 'polygon')

        if typ in ('polygon', 'polyline') and pts_scene:
            for gi in items_dict[mu_id]:
                if isinstance(gi, QGraphicsPathItem):
                    path = QPainterPath()
                    path.moveTo(pts_scene[0])
                    for pt in pts_scene[1:]:
                        path.lineTo(pt)
                    if typ == 'polygon':
                        path.closeSubpath()
                    gi.setPath(path)
                    break

        elif typ in ('text', 'comment') and pts_scene and self._drag_original_pts:
            delta = pts_scene[0] - self._drag_original_pts[0]
            for gi, orig in self._drag_item_origins:
                gi.setPos(orig + delta)

        elif typ == 'symbol' and pts_scene and self._drag_original_pts:
            delta = pts_scene[0] - self._drag_original_pts[0]
            for gi, orig in self._drag_item_origins:
                gi.setPos(orig + delta)
            # Also move the resize/rotate handles to follow the symbol
            self._update_symbol_handles(
                pts_scene[0].x(), pts_scene[0].y(),
                self._symbol_live_w, self._symbol_live_h, self._symbol_live_rot)

    def _update_handle_positions(self, pts_scene):
        for i, handle in enumerate(self._vertex_handles):
            if i < len(pts_scene):
                handle.setPos(pts_scene[i])

    def _finish_edit_drag(self):
        """Called on mouseRelease after a drag — save new points."""
        if self._edit_mu_id is None:
            return
        mu_id = self._edit_mu_id
        is_red = mu_id in self._red_markup_items
        items_dict = self._red_markup_items if is_red else self._markup_items
        types_dict = self._red_markup_types if is_red else self._markup_types
        new_pdf_pts = [list(self.scene_to_pdf(pt)) for pt in self._drag_current_pts]
        for gi in items_dict.get(mu_id, []):
            if gi.data(self._DATA_MARKUP_PTS) is not None:
                gi.setData(self._DATA_MARKUP_PTS, new_pdf_pts)
                break
        # For symbols moved via 'item' drag, keep _symbol_orig_center in sync
        if types_dict.get(mu_id) == 'symbol' and new_pdf_pts:
            self._symbol_orig_center = self.pdf_to_scene(*new_pdf_pts[0])
        self.markup_moved.emit(mu_id, new_pdf_pts)

    def _start_inline_label_edit(self, mu_id):
        """Show a floating QLineEdit over the text item for in-place label editing."""
        txt_item = None
        items_dict = (self._red_markup_items if mu_id in self._red_markup_items
                      else self._markup_items)
        for gi in items_dict.get(mu_id, []):
            if isinstance(gi, QGraphicsSimpleTextItem):
                txt_item = gi
                break
        if txt_item is None:
            return
        if self._inline_edit_widget is not None:
            self._inline_edit_widget.deleteLater()
            self._inline_edit_widget = None

        vp = self.mapFromScene(txt_item.pos())
        br = txt_item.boundingRect()
        edit = QLineEdit(self.viewport())
        edit.setFont(txt_item.font())
        edit.setText(txt_item.text())
        edit.move(vp.x(), vp.y())
        edit.resize(max(160, int(br.width()) + 20), int(br.height()) + 6)
        edit.selectAll()
        edit.setStyleSheet(
            "background:white;border:2px solid #1e78e6;border-radius:2px;padding:1px;")
        edit.show()
        edit.setFocus()
        self._inline_edit_widget = edit
        committed = [False]

        def commit():
            if committed[0]:
                return
            committed[0] = True
            new_text = edit.text().strip() or edit.text()
            if new_text:
                self.markup_label_edited.emit(mu_id, new_text)
            edit.deleteLater()
            self._inline_edit_widget = None

        edit.returnPressed.connect(commit)
        edit.editingFinished.connect(commit)

    # ---------------------------------------------------------------- smart polyline

    def _clear_smart_preview(self):
        for gi in self._smart_preview:
            try: self._scene.removeItem(gi)
            except RuntimeError as e: logging.warning(f"Failed to remove smart preview item: {e}")
        self._smart_preview.clear()

    def _draw_smart_marker(self, scene_pos, role):
        """Draw start (green) or end (red) marker dot at scene_pos."""
        color  = QColor('#4CAF50') if role == 'start' else QColor('#F44336')
        r      = 6
        dot    = QGraphicsEllipseItem(-r, -r, r * 2, r * 2)
        dot.setBrush(QBrush(color))
        dot.setPen(QPen(color.darker(130), 1.5))
        dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        dot.setPos(scene_pos)
        dot.setZValue(Z_OVERLAY + 8)
        self._scene.addItem(dot)
        self._smart_preview.append(dot)

    def _run_smart_trace(self):
        """Run SmartPipeTracer between the two clicked PDF points."""
        if self._smart_start_pdf is None or self._smart_end_pdf is None:
            return
        # Re-use cached tracer if same page
        if self._smart_tracer is None or self._smart_tracer_page != self.current_page:
            self.setCursor(Qt.CursorShape.WaitCursor)
            QApplication.processEvents()
            self._smart_tracer      = SmartPipeTracer(self.pdf_doc, self.current_page)
            self._smart_tracer_page = self.current_page
        self.setCursor(Qt.CursorShape.WaitCursor)
        QApplication.processEvents()
        try:
            paths = self._smart_tracer.trace(self._smart_start_pdf, self._smart_end_pdf,
                                              n_alt=2)
        finally:
            self.setCursor(Qt.CursorShape.CrossCursor)

        if not paths:
            # No path found — inform user via a temporary scene text
            sp = self.pdf_to_scene(*self._smart_end_pdf)
            msg = QGraphicsSimpleTextItem("Ingen väg hittades – prova igen")
            f   = QFont(); f.setPointSize(9)
            msg.setFont(f)
            msg.setBrush(QBrush(QColor('#F44336')))
            msg.setPos(sp.x() + 10, sp.y() - 20)
            msg.setZValue(Z_OVERLAY + 9)
            self._scene.addItem(msg)
            self._smart_preview.append(msg)
            self._smart_start_pdf = None   # reset for new attempt
            return

        self._smart_paths    = paths
        self._smart_path_idx = 0
        self._show_smart_path(0)

    def _show_smart_path(self, idx):
        """Remove old path preview items and draw path[idx] as a dashed line."""
        # Remove only path items (not the start/end dot markers — keep first 2 items)
        markers = self._smart_preview[:2]
        for gi in self._smart_preview[2:]:
            try: self._scene.removeItem(gi)
            except RuntimeError as e: logging.warning(f"Failed to remove smart path item: {e}")
        self._smart_preview = markers

        if not self._smart_paths or idx >= len(self._smart_paths):
            return

        path_pdf   = self._smart_paths[idx]
        path_scene = [self.pdf_to_scene(*pt) for pt in path_pdf]

        qpath = QPainterPath()
        if path_scene:
            qpath.moveTo(path_scene[0])
            for pt in path_scene[1:]:
                qpath.lineTo(pt)

        # Use current draw_pen colour but dashed for preview
        preview_pen = QPen(self.draw_pen)
        preview_pen.setStyle(Qt.PenStyle.DashLine)
        preview_pen.setCosmetic(True)
        path_item = self._scene.addPath(qpath, preview_pen)
        path_item.setZValue(Z_OVERLAY + 7)
        self._smart_preview.append(path_item)

        # Navigation hint text near end point
        if len(self._smart_paths) > 1:
            ep  = path_scene[-1]
            lbl = f"Väg {idx + 1}/{len(self._smart_paths)}  ←  →  Enter=spara"
            txt = QGraphicsSimpleTextItem(lbl)
            f   = QFont(); f.setPointSize(8)
            txt.setFont(f)
            txt.setBrush(QBrush(QColor('#1565C0')))
            bg_rect = txt.boundingRect()
            bg = QGraphicsRectItem(ep.x() + 8, ep.y() - 18,
                                    bg_rect.width() + 6, bg_rect.height() + 2)
            bg.setBrush(QBrush(QColor(255, 255, 255, 200)))
            bg.setPen(QPen(Qt.PenStyle.NoPen))
            bg.setZValue(Z_OVERLAY + 8)
            txt.setPos(ep.x() + 11, ep.y() - 17)
            txt.setZValue(Z_OVERLAY + 9)
            self._scene.addItem(bg)
            self._scene.addItem(txt)
            self._smart_preview.extend([bg, txt])

    def _confirm_smart(self):
        """Accept current path and emit it as a polyline markup."""
        if not self._smart_paths or self._smart_path_idx >= len(self._smart_paths):
            return
        pts = self._smart_paths[self._smart_path_idx]
        self._clear_smart_preview()
        self._smart_start_pdf = None
        self._smart_end_pdf   = None
        self._smart_paths     = []
        self.markup_draw_finished.emit('polyline', pts, self.current_page)

    def _cancel_smart(self):
        """Cancel the smart trace — reset state and clear preview."""
        self._clear_smart_preview()
        self._smart_start_pdf = None
        self._smart_end_pdf   = None
        self._smart_paths     = []
        self._smart_path_idx  = 0

    def zoom_to_markup_items(self, mu_ids):
        """Zoom and pan the view to fit all given markup items."""
        combined = QRectF()
        for mu_id in mu_ids:
            items_dict = (self._red_markup_items if mu_id in self._red_markup_items
                          else self._markup_items)
            for gi in items_dict.get(mu_id, []):
                br = gi.mapToScene(gi.boundingRect()).boundingRect()
                if combined.isNull():
                    combined = br
                else:
                    combined = combined.united(br)
        if not combined.isNull():
            combined.adjust(-60, -60, 60, 60)
            self.fitInView(combined, Qt.AspectRatioMode.KeepAspectRatio)
            self._apply_lod(self.transform().m11())
            self._schedule_lod_update()

    def contextMenuEvent(self, event):
        if self.mode == MODE_MARKUP_SELECT and self._edit_mu_id is not None:
            sp = self.mapToScene(event.pos())
            for gi in self._scene.items(sp):
                if gi.data(self._DATA_MARKUP_ID) is not None:
                    menu = QMenu(self)
                    menu.addAction(_icon('clipboard'), "Duplicera",
                                   partial(self.markup_duplicate_requested.emit, self._edit_mu_id))
                    menu.exec(event.globalPos())
                    event.accept()
                    return
        super().contextMenuEvent(event)

    def highlight_markup(self, mu_id):
        """Briefly pulse-highlight a markup item (thicken its border)."""
        if self._markup_highlighted == mu_id:
            return
        # Reset previous highlight - check BOTH dicts
        if self._markup_highlighted:
            prev_dict = None
            if self._markup_highlighted in self._red_markup_items:
                prev_dict = self._red_markup_items
            elif self._markup_highlighted in self._markup_items:
                prev_dict = self._markup_items
            if prev_dict:
                for gi in prev_dict[self._markup_highlighted]:
                    if isinstance(gi, (QGraphicsPathItem, QGraphicsRectItem)):
                        p = gi.pen()
                        p.setWidthF(max(1, p.widthF() - 2))
                        gi.setPen(p)
        self._markup_highlighted = mu_id
        # Highlight current - check BOTH dicts
        curr_dict = None
        if mu_id in self._red_markup_items:
            curr_dict = self._red_markup_items
        elif mu_id in self._markup_items:
            curr_dict = self._markup_items
        if curr_dict and mu_id in curr_dict:
            for gi in curr_dict[mu_id]:
                if isinstance(gi, (QGraphicsPathItem, QGraphicsRectItem)):
                    p = gi.pen()
                    p.setWidthF(p.widthF() + 2)
                    gi.setPen(p)

    def _add_tracked(self, item, marker_type: str):
        """Add item to scene and track it for visibility toggling."""
        self._scene.addItem(item)
        self._type_items.setdefault(marker_type, []).append(item)
        if not self._type_visible.get(marker_type, True):
            item.setVisible(False)

    # ── Equipment marker multi-select (2026-08-08, see NOTES.md) ───────────
    def _find_equipment_item(self, marker_id):
        for item in self._type_items.get('equipment', []):
            if item.data(self._DATA_TYPE) == 'equipment' and item.data(self._DATA_ID) == marker_id:
                return item
        return None

    def _select_equipment_marker(self, marker_id):
        if marker_id in self._selected_equipment_markers:
            return
        self._selected_equipment_markers.add(marker_id)
        item = self._find_equipment_item(marker_id)
        if item is None:
            return
        # A separate highlight overlay (not a pen change on the marker
        # itself) — avoids having to know/restore whatever pen color the
        # marker's own "has deviations?" state already uses.
        rect = item.mapRectToScene(item.boundingRect()).adjusted(-3, -3, 3, 3)
        pen = QPen(QColor(30, 110, 220), 2.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        overlay = self._scene.addRect(rect, pen, QBrush(Qt.BrushStyle.NoBrush))
        overlay.setZValue(Z_OVERLAY + 5)
        self._equip_selection_overlays[marker_id] = overlay

    def _deselect_equipment_marker(self, marker_id):
        self._selected_equipment_markers.discard(marker_id)
        overlay = self._equip_selection_overlays.pop(marker_id, None)
        if overlay is not None:
            try: self._scene.removeItem(overlay)
            except RuntimeError as e: logging.warning(f"Failed to remove selection overlay: {e}")

    def _toggle_equipment_selection(self, marker_id):
        if marker_id in self._selected_equipment_markers:
            self._deselect_equipment_marker(marker_id)
        else:
            self._select_equipment_marker(marker_id)

    def _clear_equipment_selection(self):
        for marker_id in list(self._equip_selection_overlays.keys()):
            self._deselect_equipment_marker(marker_id)
        self._selected_equipment_markers.clear()

    def _reapply_equipment_selection_overlays(self):
        """Re-draw the dashed-blue selection overlay for every currently
        selected marker after a full overlay rebuild. clear_overlays()
        (called by PIDPanel._load_overlays() on every marker recolor —
        e.g. right after checking a deviation in EquipmentDeviationBar)
        removes ANY scene item whose zValue falls in the overlay range,
        including these — they're plain addRect() items, not tracked in
        _type_items like the markers themselves. Without this, the
        highlight from a plain click (2026-08-12, see NOTES.md) would
        vanish the instant the user checks a deviation on the very
        marker they just selected, defeating its purpose."""
        ids = list(self._selected_equipment_markers)
        self._equip_selection_overlays.clear()
        self._selected_equipment_markers.clear()
        for marker_id in ids:
            if self._find_equipment_item(marker_id) is not None:
                self._select_equipment_marker(marker_id)

    def _equipment_markers_in_rect(self, band_rect):
        """Return equipment_markers.id values whose scene bounding rect
        intersects band_rect — shared by the Ctrl-drag rubber-band's live
        count label and its release-time selection commit (2026-08-10,
        see NOTES.md)."""
        ids = []
        for item in self._type_items.get('equipment', []):
            marker_id = item.data(self._DATA_ID)
            if (item.data(self._DATA_TYPE) == 'equipment' and marker_id is not None and
                    band_rect.intersects(item.mapRectToScene(item.boundingRect()))):
                ids.append(int(marker_id))
        return ids

    def set_marker_visibility(self, marker_type: str, visible: bool):
        """Show or hide all markers of a given type."""
        self._type_visible[marker_type] = visible
        for item in self._type_items.get(marker_type, []):
            try:
                item.setVisible(visible)
            except Exception:
                pass

    def _show_context_menu(self, sp, global_pos):
        menu = QMenu(self.viewport())

        # ── Explicit "clear selection" (2026-08-10, see NOTES.md) — Escape
        # and a plain click already clear it, but a discoverable menu
        # entry doesn't hurt, and gives a keyboard-free way to do it too.
        if self._selected_equipment_markers:
            n = len(self._selected_equipment_markers)
            act = menu.addAction(_icon('close'), f"Rensa markering ({n} objekt)")
            act.triggered.connect(self._clear_equipment_selection)
            menu.addSeparator()

        # ── Check if right-click landed on a sheet-connection arc ──────────────
        for item in self._scene.items(sp):
            conn_id = getattr(item, '_sheet_conn_id', None)
            if conn_id is not None:
                act = menu.addAction(_icon('unlink'), "Bryt länk")
                _cid = conn_id
                act.triggered.connect(partial(self.sheet_conn_break_requested.emit, _cid))
                menu.exec(global_pos)
                return

        # ── Check if right-click landed on a page (board layout) ───────────────
        clicked_page = self._hit_test_page(sp)
        if self.mode == MODE_BOARD_LAYOUT:
            act = menu.addAction(_icon('link'), "Lägg till länk till annat blad…")
            _cp = clicked_page
            act.triggered.connect(partial(self._start_add_sheet_link, _cp))
            menu.exec(global_pos)
            return

        # If cursor is on an existing equipment marker, offer to edit it at the top
        hovered_type = hovered_id = None
        for item in self._scene.items(sp):
            t = item.data(self._DATA_TYPE)
            i = item.data(self._DATA_ID)
            if t == 'equipment' and i is not None:
                hovered_type, hovered_id = t, int(i)
                break
        if hovered_type == 'equipment':
            # Reported feedback: right-click an existing object to edit its
            # tag number and equipment type (2026-08-12, see NOTES.md) —
            # right-clicking it previously fell through to the generic
            # "add new object here" menu with no way to edit the one
            # already under the cursor.
            act = menu.addAction(_icon('edit'), "Redigera objekt")
            mid = hovered_id
            act.triggered.connect(partial(self.equipment_edit_requested.emit, mid))
            menu.addSeparator()

        menu.addAction("🔧 Objekt",
                       partial(self.context_action.emit, 'equipment', sp, self.current_page))
        menu.addSeparator()
        menu.addAction(_icon('search'), "Hitta liknande symbol",
                       partial(self.context_action.emit, 'find_similar', sp, self.current_page))
        menu.addAction(_icon('search'), "Hitta liknande symbol (från mall)…",
                       partial(self.context_action.emit, 'find_similar_template', sp, self.current_page))
        menu.exec(global_pos)

    def _start_add_sheet_link(self, source_page: int):
        """Enter MODE_ADD_SHEET_LINK — next left-click on a different page creates a link."""
        self._add_link_source_page = source_page
        self.set_mode(MODE_ADD_SHEET_LINK)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def _place_label(self, text: str, x_pdf: float, y_pdf: float,
                     r: float, color: QColor, marker_type: str):
        """Add a text label to the right of a marker circle with white background
        and automatic vertical offset when multiple markers share the same position."""
        center = self.pdf_to_scene(x_pdf, y_pdf)
        slot_key = (round(x_pdf * 5), round(y_pdf * 5))
        slot = self._label_slots.get(slot_key, 0)
        self._label_slots[slot_key] = slot + 1
        ROW_H = 17.0
        x0 = center.x() + r + 3
        y0 = center.y() - 8 + slot * ROW_H

        txt = QGraphicsSimpleTextItem(text[:35])
        f = QFont(); f.setPointSize(8)
        txt.setFont(f)
        txt.setBrush(QBrush(color))
        txt.setPos(x0, y0)
        txt.setZValue(Z_OVERLAY + 2)

        br = txt.boundingRect()
        pad = 2.0
        bg = QGraphicsRectItem(x0 - pad, y0 - pad, br.width() + 2 * pad, br.height() + 2 * pad)
        bg.setPen(QPen(Qt.PenStyle.NoPen))
        bg.setBrush(QBrush(QColor(255, 255, 255, 230)))
        bg.setZValue(Z_OVERLAY + 1)

        self._add_tracked(bg, marker_type)
        self._add_tracked(txt, marker_type)

    def add_equipment_marker(self, marker_id, x_pdf, y_pdf, comp_type, tag='',
                             confidence=0.0, outline_pdf=None, deviation_count=0,
                             consequence_count=0, safeguard_count=0):
        """Draw an auto-detected equipment symbol marker: a semi-transparent
        shape tracing the detected symbol's outline (or a generic
        valve-bowtie icon if no outline was captured), linked to `tag` via
        geometric detection — see symbol_geometry.py / detect_equipment_symbols
        and the "🎯 Hitta på P&ID" flow in EquipmentPanel.

        `deviation_count` (see NOTES.md "Nod → Utrustning → Avvikelse") —
        0 keeps the original red "not analysed yet" colour; >0 switches to
        green and adds a small numbered badge in the marker's top-right
        corner, so a glance at the P&ID shows which equipment already has
        HAZOP deviations recorded against it.

        `consequence_count`/`safeguard_count` (2026-08-11, see NOTES.md
        "Tre räknare på P&ID") — two further badges (bottom-right/
        bottom-left), each only drawn when >0, counting how many times
        this equipment's tag appears in consequences/safeguards
        (Database.equipment_consequence_count/equipment_safeguard_count —
        tag+type match, since those tables have no equipment_id FK to
        join on the way deviations does). Deliberately does NOT change
        the red/green "analysed" colouring above, which stays tied to
        deviation_count alone, unchanged from before this feature."""
        center = self.pdf_to_scene(x_pdf, y_pdf)
        r = 12.0
        has_deviations = deviation_count > 0
        pen = QPen(QColor(0, 130, 60) if has_deviations else QColor(160, 0, 0), 1.5)
        brush = QBrush(QColor(40, 180, 90, 100) if has_deviations else QColor(220, 20, 20, 90))

        points = None
        if outline_pdf:
            try:
                raw = json.loads(outline_pdf) if isinstance(outline_pdf, str) else outline_pdf
                if raw and len(raw) >= 3:
                    points = [self.pdf_to_scene(px, py) for px, py in raw]
            except Exception:
                points = None

        if not points:
            # Fallback: a generic valve-bowtie (two triangles meeting at the
            # center) when no detected outline was captured for this marker.
            cx, cy = center.x(), center.y()
            points = [QPointF(cx - r, cy - r), QPointF(cx - r, cy + r), QPointF(cx, cy),
                      QPointF(cx + r, cy - r), QPointF(cx + r, cy + r), QPointF(cx, cy)]

        item = QGraphicsPolygonItem(QPolygonF(points))
        item.setPen(pen)
        item.setBrush(brush)
        item.setZValue(Z_OVERLAY)
        pct = int(round(confidence * 100))
        tip = f"{tag + ': ' if tag else ''}{comp_type}\nAutodetekterad ({pct}% konfidens)"
        if has_deviations:
            tip += f"\n{deviation_count} avvikelse{'r' if deviation_count != 1 else ''} registrerad{'e' if deviation_count != 1 else ''}"
        if consequence_count > 0:
            tip += f"\n{consequence_count} konsekvens{'er' if consequence_count != 1 else ''}"
        if safeguard_count > 0:
            tip += f"\n{safeguard_count} safeguard{'s' if safeguard_count != 1 else ''}"
        # Gesture hints (2026-08-10, see NOTES.md) — Ctrl/Shift modifiers on
        # equipment markers have no other visible affordance in the UI.
        tip += ("\n\nCtrl+klick: markera flera\nCtrl+drag: gummiband-markera flera\n"
                "Shift+drag: dra (markerade) till konsekvens/safeguard/avvikelse")
        item.setToolTip(tip)
        item.setData(self._DATA_TYPE, 'equipment')
        item.setData(self._DATA_ID, marker_id)
        item.setAcceptHoverEvents(True)
        item.setCursor(Qt.CursorShape.PointingHandCursor)   # matches cause/consequence/safeguard markers
        self._add_tracked(item, 'equipment')

        def _draw_corner_badge(bx, by, count, outline, fill):
            badge_r = 8.0
            badge = QGraphicsEllipseItem(bx - badge_r, by - badge_r, 2 * badge_r, 2 * badge_r)
            badge.setPen(QPen(outline, 1))
            badge.setBrush(QBrush(fill))
            badge.setZValue(Z_OVERLAY + 1)
            badge.setToolTip(tip)
            badge.setCursor(Qt.CursorShape.PointingHandCursor)
            self._add_tracked(badge, 'equipment')
            count_txt = QGraphicsSimpleTextItem(str(count))
            f = QFont(); f.setPointSize(8); f.setBold(True)
            count_txt.setFont(f)
            count_txt.setBrush(QBrush(QColor(255, 255, 255)))
            tb = count_txt.boundingRect()
            count_txt.setPos(bx - tb.width() / 2, by - tb.height() / 2)
            count_txt.setZValue(Z_OVERLAY + 2)
            self._add_tracked(count_txt, 'equipment')

        poly_rect = QPolygonF(points).boundingRect()
        # Three corners, one counter each — colours distinct from the
        # cause/consequence/safeguard markers themselves (red/orange/green)
        # so the two never get visually conflated at a glance.
        if has_deviations:
            _draw_corner_badge(poly_rect.right(), poly_rect.top(), deviation_count,
                               QColor(0, 100, 40), QColor(0, 140, 60))
        if consequence_count > 0:
            _draw_corner_badge(poly_rect.right(), poly_rect.bottom(), consequence_count,
                               QColor(180, 100, 0), QColor(230, 140, 20))
        if safeguard_count > 0:
            _draw_corner_badge(poly_rect.left(), poly_rect.bottom(), safeguard_count,
                               QColor(20, 60, 130), QColor(52, 110, 200))

        if tag:
            self._place_label(tag, x_pdf, y_pdf, r, QColor(140, 0, 0), 'equipment')

    def _extract_tag_from_rect(self, pdf_rect: QRectF) -> tuple:
        """Extract tag text AND classify the P&ID symbol inside the rectangle.

        Returns (tag: str, comp_type: str, symbol_name: str)
        e.g. ('PSV-101', 'Säkerhetsventil (PSV)', 'Säkerhetsventil (PSV/PRV)')
        """
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return '', '', ''
        try:
            page  = self.pdf_doc.load_page(self.current_page)
            frect = fitz.Rect(pdf_rect.x(), pdf_rect.y(),
                               pdf_rect.x() + pdf_rect.width(),
                               pdf_rect.y() + pdf_rect.height())

            # ── 1. Native text extraction with spatial combining ──────────────
            raw_words = page.get_text("words", clip=frect)
            # Try spatially-combined strings first (catches 20 - PCV - 101)
            tag = ''
            for candidate, *_box in _spatial_combine(raw_words):
                t = _pick_best_tag(candidate)
                if t:
                    tag = t
                    break
            # Fallback: all words joined
            if not tag:
                native_text = ' '.join(w[4].strip() for w in raw_words if w[4].strip())
                tag = _pick_best_tag(native_text) or native_text.strip()

            # ── 2. OCR fallback ───────────────────────────────────────────────
            if not tag and HAS_PIL:
                min_dim  = max(pdf_rect.width(), pdf_rect.height(), 10.0)
                scale    = max(4.0, min(16.0, 300.0 / min_dim))
                mat      = fitz.Matrix(scale, scale)
                pix      = page.get_pixmap(matrix=mat, clip=frect, alpha=False)
                pil      = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
                pil      = _preprocess_for_ocr(pil)
                ocr_text = ''
                if HAS_TESSERACT:
                    try:
                        cfg = ('--oem 3 --psm 7 '
                               '-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.')
                        ocr_text = pytesseract.image_to_string(pil, config=cfg).strip()
                    except Exception:
                        pass
                if not ocr_text and HAS_EASYOCR:
                    try:
                        import numpy as np
                        reader = _get_easyocr_reader()
                        if reader:
                            results = reader.readtext(np.array(pil))
                            ocr_text = ' '.join(r[1] for r in results if r[2] > 0.3)
                    except Exception:
                        pass
                tag = _pick_best_tag(ocr_text) or ocr_text.strip()

            return tag

        except Exception:
            pass
        return ''

    def _text_in_rect(self, pdf_rect: QRectF) -> str:
        """Return all text inside pdf_rect. Uses native PDF text first, OCR as fallback."""
        if not HAS_PYMUPDF or self.pdf_doc is None:
            return ''
        try:
            pg    = self.pdf_doc.load_page(self.current_page)
            frect = fitz.Rect(pdf_rect.x(), pdf_rect.y(),
                               pdf_rect.x() + pdf_rect.width(),
                               pdf_rect.y() + pdf_rect.height())
            raw_words = pg.get_text("words", clip=frect)
            native = ' '.join(w[4].strip() for w in raw_words if w[4].strip())
            if native.strip():
                return native.strip()
            # OCR fallback
            if not HAS_PIL:
                return ''
            min_dim = max(pdf_rect.width(), pdf_rect.height(), 10.0)
            scale   = max(4.0, min(16.0, 300.0 / min_dim))
            mat     = fitz.Matrix(scale, scale)
            pix     = pg.get_pixmap(matrix=mat, clip=frect, alpha=False)
            pil     = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
            pil     = _preprocess_for_ocr(pil)
            if HAS_TESSERACT:
                try:
                    ocr = pytesseract.image_to_string(pil).strip()
                    if ocr:
                        return ocr
                except Exception:
                    pass
            if HAS_EASYOCR:
                try:
                    import numpy as np
                    reader = _get_easyocr_reader()
                    if reader:
                        results = reader.readtext(np.array(pil))
                        txt = ' '.join(r[1] for r in results if r[2] > 0.3)
                        if txt.strip():
                            return txt.strip()
                except Exception:
                    pass
        except Exception:
            pass
        return ''

    def add_tag_highlight(self, bbox: 'fitz.Rect', color: str, tooltip: str = ''):
        """Draw a semi-transparent highlight rectangle at the tag's PDF position."""
        r = QRectF(bbox.x0, bbox.y0, bbox.width, bbox.height)
        pen = QPen(Qt.PenStyle.NoPen)
        brush = QBrush(QColor(color))
        item = self._scene.addRect(r, pen, brush)
        item.setOpacity(0.35)
        item.setZValue(Z_HIGHLIGHT)
        if tooltip:
            item.setToolTip(tooltip)
        return item

    def clear_highlights(self):
        """Remove all tag highlights (Z_HIGHLIGHT items)."""
        for item in list(self._scene.items()):
            if item.zValue() == Z_HIGHLIGHT:
                try: self._scene.removeItem(item)
                except RuntimeError as e: logging.warning(f"Failed to remove highlight item: {e}")

    def add_shape_highlight(self, outline_pdf, color='#2F5FD0'):
        """Draw a semi-transparent polygon preview of a candidate's
        outline — used by SimilarSymbolSearchDialog's "Visa på P&ID"
        live preview (2026-08-15, see NOTES.md "Hitta liknande symbol"
        — uppföljningsfunktioner). Modelled on add_equipment_marker's
        outline-drawing, but on Z_TEMP (ephemeral, explicitly cleared —
        same convention as this class's rubber-band/drag previews)
        instead of Z_OVERLAY, and with no marker_id/DB link at all —
        this is a preview, not a saved marker."""
        if not outline_pdf or len(outline_pdf) < 3:
            return None
        points = [self.pdf_to_scene(px, py) for px, py in outline_pdf]
        item = self._scene.addPolygon(
            QPolygonF(points), QPen(QColor(color), 1.5), QBrush(QColor(color)))
        item.setOpacity(0.30)
        item.setZValue(Z_TEMP)
        return item

    def clear_shape_preview(self):
        """Remove all "Visa på P&ID" preview polygons (Z_TEMP items) —
        see add_shape_highlight()."""
        for item in list(self._scene.items()):
            if item.zValue() == Z_TEMP:
                try: self._scene.removeItem(item)
                except RuntimeError as e: logging.warning(f"Failed to remove preview item: {e}")

    def _conn_obstacles(self, src_page, dst_page):
        """Inflated scene rects of every page except the connection's own two."""
        rs = self.render_scale
        rects = []
        for pn, (ox, oy) in self._page_offsets.items():
            if pn == src_page or pn == dst_page:
                continue
            w = self._page_widths_pdf.get(pn, 0.0) * rs
            h = self._page_heights_pdf.get(pn, 0.0) * rs
            rects.append(QRectF(ox - 60, oy - 60, w + 120, h + 120))
        return rects

    @staticmethod
    def _seg_rect_entry(p0, p1, r):
        """Liang-Barsky: param t where segment p0→p1 first enters rect, else None."""
        dx = p1.x() - p0.x()
        dy = p1.y() - p0.y()
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, p0.x() - r.left()), (dx, r.right() - p0.x()),
                     (-dy, p0.y() - r.top()),  (dy, r.bottom() - p0.y())):
            if abs(p) < 1e-9:
                if q < 0:
                    return None
            else:
                t = q / p
                if p < 0:
                    if t > t1:
                        return None
                    if t > t0:
                        t0 = t
                else:
                    if t < t0:
                        return None
                    if t < t1:
                        t1 = t
        return t0 if t0 < t1 else None

    def _route_around_pages(self, p0, p1, obstacles, depth=0, wiggle=0.0):
        """Waypoint list p0→p1 detouring around page rects (greedy, recursive).

        Each blocking page is passed on its nearest free side; the two detour
        corners are then routed recursively. Depth-capped — when the board
        truly forces a crossing the direct segment is kept.
        """
        if depth >= 8:
            return [p0, p1]
        best_t, best_r = None, None
        for r in obstacles:
            if r.contains(p0) and r.contains(p1):
                continue
            t = self._seg_rect_entry(p0, p1, r)
            if t is not None and 0.0 < t < 1.0 and (best_t is None or t < best_t):
                best_t, best_r = t, r
        if best_r is None:
            return [p0, p1]
        r = best_r
        M = 28.0 + abs(wiggle) * 0.5   # clearance beyond the inflated rect
        horiz = abs(p1.x() - p0.x()) >= abs(p1.y() - p0.y())
        if horiz:
            y = (r.top() - M if (p0.y() + p1.y()) / 2 <= r.center().y()
                 else r.bottom() + M) + wiggle
            x_in, x_out = ((r.left() - M, r.right() + M)
                           if p0.x() <= p1.x() else
                           (r.right() + M, r.left() - M))
            w1, w2 = QPointF(x_in, y), QPointF(x_out, y)
        else:
            x = (r.left() - M if (p0.x() + p1.x()) / 2 <= r.center().x()
                 else r.right() + M) + wiggle
            y_in, y_out = ((r.top() - M, r.bottom() + M)
                           if p0.y() <= p1.y() else
                           (r.bottom() + M, r.top() - M))
            w1, w2 = QPointF(x, y_in), QPointF(x, y_out)
        a = self._route_around_pages(p0, w1, obstacles, depth + 1, wiggle)
        b = self._route_around_pages(w2, p1, obstacles, depth + 1, wiggle)
        return a + b

    @staticmethod
    def _rounded_path(pts, radius=130.0):
        """QPainterPath through waypoints with rounded (quad-bezier) corners."""
        import math
        path = QPainterPath()
        path.moveTo(pts[0])
        for i in range(1, len(pts) - 1):
            prev, p, nxt = pts[i - 1], pts[i], pts[i + 1]
            v1x, v1y = p.x() - prev.x(), p.y() - prev.y()
            v2x, v2y = nxt.x() - p.x(), nxt.y() - p.y()
            l1 = math.hypot(v1x, v1y) or 1.0
            l2 = math.hypot(v2x, v2y) or 1.0
            r1 = min(radius, l1 * 0.5)
            r2 = min(radius, l2 * 0.5)
            a = QPointF(p.x() - v1x / l1 * r1, p.y() - v1y / l1 * r1)
            b = QPointF(p.x() + v2x / l2 * r2, p.y() + v2y / l2 * r2)
            path.lineTo(a)
            path.quadTo(p, b)
        path.lineTo(pts[-1])
        return path

    def add_sheet_conn_arc(self, src: QPointF, dst: QPointF,
                           color_hex: str, confidence: float, label: str,
                           bidirectional: bool = False, conn_id: int = -1,
                           src_edge: str = 'right', dst_edge: str = 'left',
                           src_page: int = -1, dst_page: int = -1,
                           arc_index: int = 0, weight: float = 0.5):
        """Draw a routed connection line over the board at 50% opacity.

        Short edge stubs let the line turn steeply toward its target; the
        middle leg detours around other pages (rounded corners) so lines do
        not cut straight across P&IDs. Parallel connections are staggered.
        Pen width scales with weight; drawn above all page pixmaps.
        """
        import math
        color = QColor(color_hex)
        color.setAlpha(128)   # 50 % transparent

        pen_width = round(max(2.0, min(6.0, 2.0 + weight * 5.0)), 1)
        pen = QPen(color, pen_width)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if confidence < 0.50:
            pen.setStyle(Qt.PenStyle.DashLine)
        elif confidence < 0.70:
            pen.setStyle(Qt.PenStyle.DotLine)

        sx, sy = src.x(), src.y()
        ex, ey = dst.x(), dst.y()

        # Stagger parallel connections perpendicular to the chord
        STEP = 20
        if arc_index == 0:
            slot_off = 0
        else:
            slot_off = ((arc_index + 1) // 2) * STEP * (1 if arc_index % 2 == 1 else -1)

        chord = math.hypot(ex - sx, ey - sy) or 1.0
        perp_x = -(ey - sy) / chord
        perp_y =  (ex - sx) / chord
        src_pt = QPointF(sx + perp_x * slot_off, sy + perp_y * slot_off)
        dst_pt = QPointF(ex + perp_x * slot_off, ey + perp_y * slot_off)

        # Short edge stubs — the line may turn steeply once clear of the page
        stub = max(70.0, min(260.0, chord * 0.18))
        _edx = {'right': 1, 'left': -1, 'top': 0, 'bottom': 0}
        _edy = {'right': 0, 'left':  0, 'top': -1, 'bottom': 1}
        src_stub = QPointF(src_pt.x() + _edx.get(src_edge, 1)  * stub,
                           src_pt.y() + _edy.get(src_edge, 0)  * stub)
        dst_stub = QPointF(dst_pt.x() + _edx.get(dst_edge, -1) * stub,
                           dst_pt.y() + _edy.get(dst_edge,  0) * stub)

        # Route the middle leg around other pages for a cleaner board;
        # fall back to the direct curve when the detour grows absurd.
        obstacles = self._conn_obstacles(src_page, dst_page)
        mids = self._route_around_pages(src_stub, dst_stub, obstacles,
                                        wiggle=float(slot_off))
        pts = [src_pt] + mids + [dst_pt]
        clean = [pts[0]]
        for p in pts[1:]:
            if abs(p.x() - clean[-1].x()) + abs(p.y() - clean[-1].y()) > 3.0:
                clean.append(p)
        if len(clean) < 2:
            clean = [src_pt, dst_pt]
        total = sum(math.hypot(clean[i + 1].x() - clean[i].x(),
                               clean[i + 1].y() - clean[i].y())
                    for i in range(len(clean) - 1))
        if total > chord * 3.0 + 1200.0:
            clean = [src_pt, src_stub, dst_stub, dst_pt]

        path = self._rounded_path(clean)

        pi = QGraphicsPathItem(path)
        pi.setPen(pen)
        pi.setZValue(Z_SHEET_CONN)   # above page pixmaps
        pi._sheet_conn_id = conn_id
        pi.setFlag(pi.GraphicsItemFlag.ItemIsSelectable, False)
        self._scene.addItem(pi)

        # Arrowhead tangent follows the final segment into the dot
        arrow_angle = math.atan2(clean[-1].y() - clean[-2].y(),
                                 clean[-1].x() - clean[-2].x())

        def _arrowhead(tip_pt, angle, col):
            AL, AH = 16, 7
            lp = QPointF(tip_pt.x() - AL * math.cos(angle) + AH * math.sin(angle),
                         tip_pt.y() - AL * math.sin(angle) - AH * math.cos(angle))
            rp = QPointF(tip_pt.x() - AL * math.cos(angle) - AH * math.sin(angle),
                         tip_pt.y() - AL * math.sin(angle) + AH * math.cos(angle))
            ah = QGraphicsPolygonItem(QPolygonF([tip_pt, lp, rp]))
            ah.setBrush(QBrush(col))
            ah.setPen(QPen(Qt.PenStyle.NoPen))
            ah.setZValue(Z_SHEET_CONN)
            self._scene.addItem(ah)

        _arrowhead(dst_pt, arrow_angle, color)
        if bidirectional:
            src_angle = math.atan2(clean[0].y() - clean[1].y(),
                                   clean[0].x() - clean[1].x())
            _arrowhead(src_pt, src_angle, color)

        # Label at bezier midpoint with white background
        if label:
            mid = path.pointAtPercent(0.5)
            txt = QGraphicsSimpleTextItem(label)
            label_color = QColor(color_hex)   # opaque for readability
            label_color.setAlpha(200)
            txt.setBrush(QBrush(label_color.darker(130)))
            fnt = txt.font(); fnt.setPointSize(8); fnt.setBold(True); txt.setFont(fnt)
            tr = txt.boundingRect()
            txt.setPos(mid.x() - tr.width() / 2, mid.y() - tr.height() / 2 - 10)
            bg = QGraphicsRectItem(txt.pos().x() - 3, txt.pos().y() - 2,
                                   tr.width() + 6, tr.height() + 4)
            bg.setBrush(QBrush(QColor(250, 250, 250, 210)))
            bg.setPen(QPen(Qt.PenStyle.NoPen))
            bg.setZValue(Z_SHEET_CONN + 0.1)
            txt.setZValue(Z_SHEET_CONN + 0.2)
            self._scene.addItem(bg)
            self._scene.addItem(txt)

    def clear_overlays(self):
        self._purge_rubber_band_state()

        _keep = set(self._all_page_items.values()) | {self._placeholder}
        for item in list(self._scene.items()):
            if item in _keep:
                continue
            if item.zValue() >= Z_SHEET_CONN or item.zValue() < Z_PAGE:
                try: self._scene.removeItem(item)
                except RuntimeError as e: logging.warning(f"Failed to remove overlay item: {e}")
        if self._pending_path_item is not None:
            try: self._scene.removeItem(self._pending_path_item)
            except RuntimeError as e: logging.warning(f"Failed to remove pending path item: {e}")
            self._pending_path_item = None
        # Clear per-type item lists and label slots
        for key in self._type_items:
            self._type_items[key] = []
        self._label_slots.clear()

    def mousePressEvent(self, event):
        # Update current_page to whichever page was clicked
        _sp = self.mapToScene(event.position().toPoint())
        _detected = self._hit_test_page(_sp)
        if _detected != self.current_page:
            self.current_page = _detected
            self.page_item = self._all_page_items.get(_detected)

        # ── Add sheet link: click target page ────────────────────────────────
        if self.mode == MODE_ADD_SHEET_LINK and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            target = self._hit_test_page(sp)
            src = self._add_link_source_page
            self._add_link_source_page = None
            self.set_mode(MODE_BOARD_LAYOUT)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            if src is not None and target != src and target in self._all_page_items:
                self.sheet_conn_add_requested.emit(src, target)
            event.accept(); return
        if self.mode == MODE_ADD_SHEET_LINK and event.button() == Qt.MouseButton.RightButton:
            # Cancel add-link mode
            self._add_link_source_page = None
            self.set_mode(MODE_BOARD_LAYOUT)
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            event.accept(); return

        # ── Board layout: drag page ───────────────────────────────────────────
        if self.mode == MODE_BOARD_LAYOUT and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            page = self._hit_test_page(sp)
            if page in self._all_page_items:
                self._dragging_page        = page
                self._drag_page_orig_offset = self._page_offsets.get(page, (0.0, 0.0))
                self._drag_page_start_scene = sp
                self.current_page           = page
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                # Hide connection lines during drag (re-drawn on release)
                for _ci in list(self._scene.items()):
                    if _ci.zValue() == Z_CONNECT:
                        _ci.setVisible(False)
                event.accept(); return
            super().mousePressEvent(event); return

        if self.mode in (MODE_NAV, MODE_MARKUP_SELECT):
            self._press_pos = event.position()
        sp = self.mapToScene(event.position().toPoint())

        # ── Shift+press on an equipment marker: arm a possible drag-to-worksheet ──
        # (dragging a tag onto a HAZOP consequence row). A plain click (no Shift)
        # never arms this, so it can never interfere with the normal
        # click-opens-EquipmentDeviationBar interaction below.
        self._equip_drag_candidate = None
        if (self.mode == MODE_NAV and event.button() == Qt.MouseButton.LeftButton and
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            for item in self._scene.items(sp):
                itype = item.data(self._DATA_TYPE)
                iid   = item.data(self._DATA_ID)
                if itype == 'equipment' and iid is not None:
                    self._equip_drag_candidate = (int(iid), event.position())
                    break

        # ── Ctrl+click: toggle one marker in/out of the multi-selection.
        # Ctrl+drag over empty canvas: arm a rubber-band to select several
        # at once (2026-08-08, see NOTES.md). Mutually exclusive with the
        # Shift-drag above — Ctrl selects, Shift drags, never both at once.
        if (self.mode == MODE_NAV and event.button() == Qt.MouseButton.LeftButton and
                event.modifiers() & Qt.KeyboardModifier.ControlModifier and
                not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)):
            hit_marker = None
            for item in self._scene.items(sp):
                itype = item.data(self._DATA_TYPE)
                iid   = item.data(self._DATA_ID)
                if itype == 'equipment' and iid is not None:
                    hit_marker = int(iid)
                    break
            if hit_marker is not None:
                self._toggle_equipment_selection(hit_marker)
            else:
                self._ctrl_rband_start_scene = sp
                self._ctrl_rband_dragging    = False
            self._press_pos = None
            event.accept(); return

        # Plain click (no Ctrl, no Shift) clears any existing multi-
        # selection — same convention as file managers/desktop icons. Not
        # an early return: the normal click-dispatch (open
        # EquipmentDeviationBar etc., in mouseReleaseEvent) still applies.
        if (self.mode == MODE_NAV and event.button() == Qt.MouseButton.LeftButton and
                not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier |
                                          Qt.KeyboardModifier.ShiftModifier)) and
                self._selected_equipment_markers):
            self._clear_equipment_selection()

        if self.mode == MODE_NODE:
            if event.button() == Qt.MouseButton.LeftButton:
                self._add_draw_point(sp); event.accept(); return
            elif event.button() == Qt.MouseButton.RightButton:
                self._cancel_drawing(); event.accept(); return
        elif self.mode in (MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE):
            if event.button() == Qt.MouseButton.LeftButton:
                self._add_draw_point(self._snap_to_nearest(sp)); event.accept(); return
            elif event.button() == Qt.MouseButton.RightButton:
                self._finish_markup_drawing(); event.accept(); return
        elif self.mode == MODE_SMART_POLYLINE:
            if event.button() == Qt.MouseButton.LeftButton:
                if self._smart_start_pdf is None:
                    self._smart_start_pdf = list(self.scene_to_pdf(sp))
                    self._smart_end_pdf   = None
                    self._clear_smart_preview()
                    self._draw_smart_marker(sp, 'start')
                else:
                    self._smart_end_pdf = list(self.scene_to_pdf(sp))
                    self._draw_smart_marker(sp, 'end')
                    self._run_smart_trace()
                event.accept(); return
            elif event.button() == Qt.MouseButton.RightButton:
                self._cancel_smart(); event.accept(); return
        elif self.mode in (MODE_MARKUP_TEXT, MODE_MARKUP_COMMENT):
            if event.button() == Qt.MouseButton.LeftButton:
                # Single-click immediately triggers finished signal
                pdf_pt = self.scene_to_pdf(sp)
                type_ = 'text' if self.mode == MODE_MARKUP_TEXT else 'comment'
                self.markup_draw_finished.emit(type_, [list(pdf_pt)], self.current_page)
                event.accept(); return
        elif self.mode == MODE_RED_MARKUP_SYMBOL:
            if event.button() == Qt.MouseButton.LeftButton:
                pdf_pt = self.scene_to_pdf(sp)
                self.markup_draw_finished.emit('symbol', [list(pdf_pt)], self.current_page)
                event.accept(); return
        elif self.mode == MODE_MARKUP_SELECT:
            if event.button() == Qt.MouseButton.LeftButton:
                view_pos = event.position().toPoint()
                # Priority 0a: corner resize handle (symbol)
                _corner_cursors = [
                    Qt.CursorShape.SizeFDiagCursor, Qt.CursorShape.SizeBDiagCursor,
                    Qt.CursorShape.SizeBDiagCursor, Qt.CursorShape.SizeFDiagCursor,
                ]
                for i, handle in enumerate(self._corner_handles):
                    hvp = self.mapFromScene(handle.scenePos())
                    dx = view_pos.x() - hvp.x()
                    dy = view_pos.y() - hvp.y()
                    if dx * dx + dy * dy < 144:   # 12 screen-pixel radius
                        self._symbol_drag_mode = ['nw', 'ne', 'sw', 'se'][i]
                        self._drag_mode = 'symbol_transform'
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(_corner_cursors[i])
                        event.accept(); return
                # Priority 0b: rotation handle (symbol)
                if self._rot_handle is not None:
                    hvp = self.mapFromScene(self._rot_handle.scenePos())
                    dx = view_pos.x() - hvp.x()
                    dy = view_pos.y() - hvp.y()
                    if dx * dx + dy * dy < 196:   # 14 screen-pixel radius
                        self._symbol_drag_mode = 'rotate'
                        self._drag_mode = 'symbol_transform'
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(Qt.CursorShape.OpenHandCursor)
                        event.accept(); return
                # Priority 1: vertex handle hit
                for i, handle in enumerate(self._vertex_handles):
                    hvp = self.mapFromScene(handle.scenePos())
                    dx = view_pos.x() - hvp.x()
                    dy = view_pos.y() - hvp.y()
                    if dx * dx + dy * dy < 144:   # 12 screen-pixel radius
                        self._drag_mode = 'vertex'
                        self._drag_vertex_idx = i
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(Qt.CursorShape.CrossCursor)
                        event.accept(); return
                # Priority 2: markup item hit
                for item in self._scene.items(sp):
                    mu_id = item.data(self._DATA_MARKUP_ID)
                    if mu_id is not None:
                        mu_id_int = int(mu_id)
                        self._select_for_edit(mu_id_int)
                        self._drag_mode = 'item'
                        self._drag_start_scene = sp
                        self._drag_original_pts = list(self._drag_current_pts)
                        self._drag_threshold_exceeded = False
                        self.setCursor(Qt.CursorShape.SizeAllCursor)
                        event.accept(); return
                # Priority 3: empty space → clear selection, fall through for panning
                self._clear_edit_handles()

        if event.button() == Qt.MouseButton.RightButton and self.mode in (MODE_NAV, MODE_BOARD_LAYOUT):
            # Start rubber-band drag; show context menu only if no drag occurs
            self._rband_start_scene = sp
            self._rband_dragging    = False
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if self.mode == MODE_MARKUP_SELECT and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            for gi in self._scene.items(sp):
                mu_id = gi.data(self._DATA_MARKUP_ID)
                if mu_id is not None:
                    mu_id_int = int(mu_id)
                    mtype = self._markup_types.get(mu_id_int,
                                self._red_markup_types.get(mu_id_int))
                    if mtype in ('text', 'comment'):
                        self._start_inline_label_edit(mu_id_int)
                        event.accept()
                        return
        if self.mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE) \
                and event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.position().toPoint())
            self._add_draw_point(sp)
            self._finish_markup_drawing()
            event.accept(); return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        # ── Board layout page drag release ────────────────────────────────────
        if (self.mode == MODE_BOARD_LAYOUT and self._dragging_page is not None and
                event.button() == Qt.MouseButton.LeftButton):
            self._dragging_page = None
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self._update_board_scene_rect()
            layout = {str(p): [off[0], off[1]] for p, off in self._page_offsets.items()}
            self.board_layout_changed.emit(json.dumps(layout))
            event.accept(); return

        # ── Right-drag rubber band end ────────────────────────────────────────
        if event.button() == Qt.MouseButton.RightButton and self._rband_start_scene is not None:
            if self._rband_dragging:
                sp = self.mapToScene(event.position().toPoint())
                if self._rband_preview_item is not None:
                    try: self._scene.removeItem(self._rband_preview_item)
                    except RuntimeError as e: logging.warning(f"Failed to remove rubber-band preview: {e}")
                    self._rband_preview_item = None
                if self._rband_label_item is not None:
                    try: self._scene.removeItem(self._rband_label_item)
                    except RuntimeError as e: logging.warning(f"Failed to remove rubber-band label: {e}")
                    self._rband_label_item = None
                rect = QRectF(self._rband_start_scene, sp).normalized()
                rs = self.render_scale
                ox, oy = self._page_offsets.get(self.current_page, (0.0, 0.0))
                pdf_rect = QRectF((rect.x() - ox) / rs, (rect.y() - oy) / rs,
                                   rect.width() / rs, rect.height() / rs)
                self._rband_start_scene = None
                self._rband_dragging    = False
                self.zone_drawn.emit(pdf_rect, self.current_page)
            else:
                # No drag — show context menu as usual
                sp = self.mapToScene(event.position().toPoint())
                self._rband_start_scene = None
                self._rband_dragging    = False
                self._show_context_menu(sp, event.globalPosition().toPoint())
            event.accept(); return

        # ── Ctrl+drag rubber band end — commit the multi-selection ─────────────
        # (2026-08-08, see NOTES.md). Adds to the existing selection rather
        # than replacing it, so repeated Ctrl-drags can build up a group.
        if (event.button() == Qt.MouseButton.LeftButton and
                self._ctrl_rband_start_scene is not None):
            if self._ctrl_rband_dragging:
                sp = self.mapToScene(event.position().toPoint())
                if self._ctrl_rband_preview_item is not None:
                    try: self._scene.removeItem(self._ctrl_rband_preview_item)
                    except RuntimeError as e: logging.warning(f"Failed to remove ctrl rubber-band preview: {e}")
                    self._ctrl_rband_preview_item = None
                if self._ctrl_rband_count_label is not None:
                    try: self._scene.removeItem(self._ctrl_rband_count_label)
                    except RuntimeError as e: logging.warning(f"Failed to remove ctrl-drag count label: {e}")
                    self._ctrl_rband_count_label = None
                band_rect = QRectF(self._ctrl_rband_start_scene, sp).normalized()
                for marker_id in self._equipment_markers_in_rect(band_rect):
                    self._select_equipment_marker(marker_id)
            self._ctrl_rband_start_scene = None
            self._ctrl_rband_dragging    = False
            self._press_pos = None
            event.accept(); return

        # ── MARKUP_SELECT drag-aware release ──────────────────────────────────
        if self.mode == MODE_MARKUP_SELECT and self._drag_mode is not None and \
                event.button() == Qt.MouseButton.LeftButton:
            if self._drag_threshold_exceeded and self._edit_mu_id is not None:
                if self._drag_mode == 'symbol_transform':
                    self._finish_symbol_transform()
                else:
                    self._finish_edit_drag()
            elif not self._drag_threshold_exceeded and self._edit_mu_id is not None:
                if self._drag_mode != 'symbol_transform':
                    self.markup_item_clicked.emit(self._edit_mu_id)
            self._drag_mode = None
            self._drag_vertex_idx = None
            self._drag_threshold_exceeded = False
            self._press_pos = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        # ── NAV mode: click on marker navigates tree ──────────────────────────
        if (self.mode in (MODE_NAV,) and
                event.button() == Qt.MouseButton.LeftButton and
                self._press_pos is not None):
            p  = event.position()
            dx = p.x() - self._press_pos.x()
            dy = p.y() - self._press_pos.y()
            if dx * dx + dy * dy < 25:
                sp = self.mapToScene(p.toPoint())
                for item in self._scene.items(sp):
                    itype = item.data(self._DATA_TYPE)
                    iid   = item.data(self._DATA_ID)
                    if itype in ('cause', 'consequence', 'safeguard', 'equipment') and iid is not None:
                        if itype == 'equipment':
                            # Plain click on a defined (red/green) object
                            # marker highlights it blue, same dashed
                            # overlay Ctrl-click multi-select already uses
                            # — single-select semantics here: clicking a
                            # new one replaces the old highlight (2026-08-12,
                            # see NOTES.md). Selection is cleared elsewhere
                            # for any other plain click (mousePressEvent).
                            self._clear_equipment_selection()
                            self._select_equipment_marker(int(iid))
                        self.marker_clicked.emit(itype, int(iid))
                        break
        self._press_pos = None
        self._equip_drag_candidate = None
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        # ── Shift+drag of an equipment marker → worksheet consequence row ─────
        if (self._equip_drag_candidate is not None and
                event.buttons() & Qt.MouseButton.LeftButton and
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            marker_id, start_pos = self._equip_drag_candidate
            p  = event.position()
            dx = p.x() - start_pos.x()
            dy = p.y() - start_pos.y()
            if (dx * dx + dy * dy) ** 0.5 >= QApplication.startDragDistance():
                self._equip_drag_candidate = None
                self._press_pos = None  # prevent mouseReleaseEvent's click-dispatch from also firing
                mime = QMimeData()
                # Dragging a marker that's part of a >=2-member multi-
                # selection (2026-08-08, see NOTES.md) drags the WHOLE
                # group, not just the one under the cursor; otherwise
                # unchanged single-marker behaviour.
                if (len(self._selected_equipment_markers) >= 2 and
                        marker_id in self._selected_equipment_markers):
                    ids_text = ",".join(str(i) for i in sorted(self._selected_equipment_markers))
                    mime.setText(f'hzp:equipment-multi:{ids_text}:-1:-1')
                else:
                    mime.setText(f'hzp:equipment:{marker_id}:-1:-1')
                drag = QDrag(self)
                drag.setMimeData(mime)
                drag.exec(Qt.DropAction.CopyAction)
                # drag.exec() blocks until the drop is released; a native
                # drag suppresses normal hover/leave events for widgets the
                # cursor passes over (e.g. the toolbar's "🔍 Navigera"
                # button), which can leave that button visually stuck in
                # its pressed/hover look after the drop even though nothing
                # is actually still held down (reported: the nav button
                # stays "intryckt" after dropping onto the tree/scenario).
                self.equipment_drag_finished.emit()
                # The mousePressEvent that started this gesture fell through
                # to super().mousePressEvent() (needed so a Shift+click that
                # never crosses the drag threshold still click-dispatches
                # normally), which armed Qt's OWN ScrollHandDrag hand-scroll
                # tracking for MODE_NAV — cursor switched to a closed hand,
                # internal "currently panning" flag set. Because drag.exec()
                # took over instead of a normal mouseMoveEvent/mouseReleaseEvent
                # pair, Qt never saw the matching release that would close
                # that out, leaving the view's cursor/pan state stuck as if
                # still mid-drag after the drop (2026-08-13 follow-up report:
                # "musen sitter kvar i dra-läge"). Toggling the drag mode off
                # and back on resets Qt's internal hand-scroll state cleanly.
                if self.mode == MODE_NAV:
                    self.setDragMode(QGraphicsView.DragMode.NoDrag)
                    self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
                    self.setCursor(Qt.CursorShape.OpenHandCursor)
                self._clear_equipment_selection()
                return
        elif self._equip_drag_candidate is not None and not (
                event.buttons() & Qt.MouseButton.LeftButton and
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            # Shift released or button let go before threshold — disarm
            self._equip_drag_candidate = None

        # ── Board layout page drag ────────────────────────────────────────────
        if self.mode == MODE_BOARD_LAYOUT and self._dragging_page is not None:
            sp = self.mapToScene(event.position().toPoint())
            dx = sp.x() - self._drag_page_start_scene.x()
            dy = sp.y() - self._drag_page_start_scene.y()
            orig_ox, orig_oy = self._drag_page_orig_offset
            new_ox = orig_ox + dx
            new_oy = orig_oy + dy
            self._page_offsets[self._dragging_page] = (new_ox, new_oy)
            self._all_page_items[self._dragging_page].setPos(new_ox, new_oy)
            event.accept(); return

        # ── Right-drag rubber band ────────────────────────────────────────────
        if (self._rband_start_scene is not None and
                event.buttons() & Qt.MouseButton.RightButton):
            sp = self.mapToScene(event.position().toPoint())
            dx = sp.x() - self._rband_start_scene.x()
            dy = sp.y() - self._rband_start_scene.y()
            if not self._rband_dragging and dx * dx + dy * dy > 100:
                self._rband_dragging = True
            if self._rband_dragging:
                rect = QRectF(self._rband_start_scene, sp).normalized()
                # Reuse the existing preview item — just update its geometry.
                # Creating/removing scene items on every mouseMoveEvent is slow.
                if self._rband_preview_item is None:
                    pen = QPen(QColor(0, 100, 200), 1.5)
                    pen.setStyle(Qt.PenStyle.DashLine)
                    pen.setCosmetic(True)
                    self._rband_preview_item = self._scene.addRect(
                        rect, pen, QBrush(QColor(0, 100, 200, 28)))
                    self._rband_preview_item.setZValue(Z_TEMP)
                else:
                    self._rband_preview_item.setRect(rect)
                # Remove live tag label during drag — tag is extracted on release.
                # _extract_tag_from_rect reads the PDF on every move event which is slow.
                if self._rband_label_item is not None:
                    try: self._scene.removeItem(self._rband_label_item)
                    except RuntimeError as e: logging.warning(f"Failed to remove dragged label: {e}")
                    self._rband_label_item = None
            event.accept(); return

        # ── Ctrl+drag rubber band — multi-select equipment markers ────────────
        # (2026-08-08, see NOTES.md). Distinct orange colour from the
        # right-drag zone-rubber-band above so the two are never confused.
        if (self._ctrl_rband_start_scene is not None and
                event.buttons() & Qt.MouseButton.LeftButton and
                event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            sp = self.mapToScene(event.position().toPoint())
            dx = sp.x() - self._ctrl_rband_start_scene.x()
            dy = sp.y() - self._ctrl_rband_start_scene.y()
            if not self._ctrl_rband_dragging and dx * dx + dy * dy > 25:
                self._ctrl_rband_dragging = True
            if self._ctrl_rband_dragging:
                rect = QRectF(self._ctrl_rband_start_scene, sp).normalized()
                if self._ctrl_rband_preview_item is None:
                    pen = QPen(QColor(230, 140, 0), 1.5)
                    pen.setStyle(Qt.PenStyle.DashLine)
                    pen.setCosmetic(True)
                    self._ctrl_rband_preview_item = self._scene.addRect(
                        rect, pen, QBrush(QColor(230, 140, 0, 28)))
                    self._ctrl_rband_preview_item.setZValue(Z_TEMP)
                else:
                    self._ctrl_rband_preview_item.setRect(rect)

                # Live "N objekt"-etikett — visar hur många som skulle bli
                # markerade om man släppte just nu (2026-08-10, se
                # NOTES.md), istället för att bara visa antalet EFTER släpp.
                count = len(self._equipment_markers_in_rect(rect))
                if count > 0:
                    text = f"{count} objekt"
                    if self._ctrl_rband_count_label is None:
                        self._ctrl_rband_count_label = QGraphicsSimpleTextItem(text)
                        lbl_font = QFont()
                        lbl_font.setBold(True)
                        lbl_font.setPointSize(9)
                        self._ctrl_rband_count_label.setFont(lbl_font)
                        self._ctrl_rband_count_label.setBrush(QBrush(QColor(180, 100, 0)))
                        self._ctrl_rband_count_label.setZValue(Z_TEMP + 1)
                        self._scene.addItem(self._ctrl_rband_count_label)
                    else:
                        self._ctrl_rband_count_label.setText(text)
                    self._ctrl_rband_count_label.setPos(sp.x() + 12, sp.y() + 12)
                elif self._ctrl_rband_count_label is not None:
                    try: self._scene.removeItem(self._ctrl_rband_count_label)
                    except RuntimeError as e: logging.warning(f"Failed to remove ctrl-drag count label: {e}")
                    self._ctrl_rband_count_label = None
            event.accept(); return

        if self.mode == MODE_MARKUP_SELECT and self._drag_mode is not None:
            sp = self.mapToScene(event.position().toPoint())
            if not self._drag_threshold_exceeded:
                dx = sp.x() - self._drag_start_scene.x()
                dy = sp.y() - self._drag_start_scene.y()
                if dx * dx + dy * dy > 4.0:
                    self._drag_threshold_exceeded = True
            if self._drag_threshold_exceeded:
                delta = sp - self._drag_start_scene
                if self._drag_mode == 'symbol_transform':
                    self._do_symbol_transform(sp)
                elif self._drag_mode == 'vertex' and self._drag_vertex_idx is not None:
                    new_pts = list(self._drag_current_pts)
                    idx = self._drag_vertex_idx
                    new_pts[idx] = self._drag_original_pts[idx] + delta
                    self._drag_current_pts = new_pts
                    self._update_edit_path(self._drag_current_pts)
                    self._update_handle_positions(self._drag_current_pts)
                elif self._drag_mode == 'item':
                    self._drag_current_pts = [p + delta for p in self._drag_original_pts]
                    self._update_edit_path(self._drag_current_pts)
                    self._update_handle_positions(self._drag_current_pts)
                event.accept()
                return
        if self.mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE) \
                and self.draw_points:
            sp = self.mapToScene(event.position().toPoint())
            if self.mode in (MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE):
                sp = self._snap_to_nearest(sp)
            self._update_rubber_band(sp)
        super().mouseMoveEvent(event)

    _LOD_THRESHOLD = 0.12   # view scale below which overview mode activates

    def _apply_lod(self, scale: float, force: bool = False):
        """Switch rendering quality based on zoom level.

        Only iterates scene items when the LOD tier actually changes so there
        is no per-frame overhead on continuous zoom within the same tier.
        """
        overview = scale < self._LOD_THRESHOLD
        if not force and overview == self._lod_overview:
            return
        self._lod_overview = overview

        aa = not overview
        self.setRenderHint(QPainter.RenderHint.Antialiasing, aa)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, aa)

        mode = (Qt.TransformationMode.FastTransformation if overview
                else Qt.TransformationMode.SmoothTransformation)
        for item in self._all_page_items.values():
            item.setTransformationMode(mode)

        # Overlay items (markers, labels, handles): invisible at overview zoom
        for item in self._scene.items():
            if item.zValue() >= Z_OVERLAY:
                item.setVisible(not overview)

    def wheelEvent(self, event):
        # Smooth zoom: scale by a factor proportional to the wheel delta
        # so trackpad pinch-zoom gives fine-grained control
        delta = event.angleDelta().y()
        if delta == 0:
            event.accept(); return
        # 1.001^delta gives ~1.15× per 120-unit tick (standard wheel notch)
        factor = 1.001 ** delta
        # Clamp to prevent extreme zoom
        cur = self.transform().m11()
        if cur * factor < 0.02:
            factor = 0.02 / cur
        elif cur * factor > 200:
            factor = 200 / cur
        self.scale(factor, factor)
        self._apply_lod(self.transform().m11())
        self._schedule_lod_update()
        event.accept()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._schedule_lod_update()

    def keyPressEvent(self, event):
        # Escape clears an active equipment multi-selection (2026-08-10,
        # see NOTES.md) — previously only placement modes/drawing had an
        # Escape handler; a Ctrl-click/Ctrl-drag selection had no keyboard
        # way to back out of short of clicking empty canvas.
        if (self.mode == MODE_NAV and event.key() == Qt.Key.Key_Escape and
                self._selected_equipment_markers):
            self._clear_equipment_selection()
            event.accept(); return
        if self.mode in (MODE_NODE, MODE_MARKUP_POLYGON, MODE_MARKUP_POLYLINE):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._finish_markup_drawing(); event.accept(); return
            elif event.key() == Qt.Key.Key_Escape:
                self._cancel_drawing(); event.accept(); return
        elif self.mode == MODE_SMART_POLYLINE and self._smart_paths:
            k = event.key()
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._confirm_smart(); event.accept(); return
            elif k == Qt.Key.Key_Escape:
                self._cancel_smart(); event.accept(); return
            elif k == Qt.Key.Key_Left:
                self._smart_path_idx = (self._smart_path_idx - 1) % len(self._smart_paths)
                self._show_smart_path(self._smart_path_idx); event.accept(); return
            elif k == Qt.Key.Key_Right:
                self._smart_path_idx = (self._smart_path_idx + 1) % len(self._smart_paths)
                self._show_smart_path(self._smart_path_idx); event.accept(); return
        super().keyPressEvent(event)


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
    """Draw a filled circle at (x, y) with a centred letter and an outside label.

    Coordinates are in PyMuPDF page space (0,0 = top-left, y down).
    """
    R = 7.0
    # Filled circle
    shape = page.new_shape()
    shape.draw_circle(fitz.Point(x, y), R)
    try:
        shape.finish(color=rgb, fill=rgb, width=0.5, fill_opacity=0.80)
    except TypeError:
        shape.finish(color=rgb, fill=rgb, width=0.5)
    shape.commit()
    # Centred letter in white (baseline ≈ circle-centre + cap_height/2 ≈ +3.5 pts)
    try:
        page.insert_text(fitz.Point(x - 2.5, y + 3.5), letter,
                         fontsize=9, color=(1.0, 1.0, 1.0), fontname='helv')
    except Exception:
        pass
    # Label to the right of the circle
    if label:
        try:
            page.insert_text(fitz.Point(x + R + 3, y + 3.5),
                             str(label)[:40], fontsize=7, color=rgb, fontname='helv')
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


class EquipmentDeviationBar(QWidget):
    """Small popup that appears right next to a clicked equipment marker on
    the P&ID and lets the user check off which of the node's deviations
    apply to it — "Lågt flöde", "Högt flöde", etc. Auto-dismisses on an
    outside click (Qt.WindowType.Popup), matching hazop.py's own small
    popups (RiskMatrixPopup etc.) — replaces the earlier persistent
    bottom-docked bar + inline cause/frequency-combo editing (2026-08-12,
    see NOTES.md: "en liten popup på P&ID viewer som kommer upp tillfälligt
    ... Resten av valen får jag nog göra nere i hazop scenario" — the rest
    of the editing already happens in the scenario table once a cause
    exists, so this popup's job is just the deviation checklist).

    The checklist is driven by the equipment's NODE's own already-defined
    deviations (2026-08-13, see NOTES.md "alla avvikelser i noden") — a
    generic standard-catalog suggestion list only kicks in as a bootstrap
    fallback for a brand new node with no deviations of its own yet.

    Retyping the equipment (comp_type) and reassigning it to a different
    node are handled elsewhere now (right-click "Redigera objekt" /
    dragging it in the tree) — dropped from here to keep this popup small.
    An "add a brand-new object" button briefly lived here too (2026-08-12)
    but was removed the same day it shipped: placing a new object doesn't
    belong in a popup anchored to an EXISTING one — right-click "🔧 Objekt"
    and the rubber-band menu already cover that.
    """

    # A deviation was newly created (or already existed) for this equipment —
    # PIDPanel listens to refresh the marker's colour/badge and the tree/worksheet.
    deviation_added = pyqtSignal(int, int)   # (deviation_id, equipment_id)
    # A previously-checked deviation was unchecked and deleted (see
    # "Kryssrutan ska gå att av-/aktivera", NOTES.md) — same refresh needs
    # as deviation_added (marker badge count, tree, worksheet).
    deviation_removed = pyqtSignal(int, int)   # (deviation_id, equipment_id)

    def __init__(self, db, parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.db = db
        self._equipment_id = None
        self._marker_id = None
        self._checklist_checkboxes = []   # display order, for number-key shortcuts
        # Set by PIDPanel right after construction to
        # PIDPanel._create_cause_for_bar — same auto-suggest-a-cause path
        # the old inline cause combo used on first check, just without a
        # dropdown to change it afterward (do that in the scenario table).
        self._create_cause_fn = None
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setStyleSheet(
            "EquipmentDeviationBar { background:#FFFFFF; border:1px solid #CFD1CE;"
            " border-radius:6px; }")
        self.setMaximumWidth(260)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        self._title_lbl = QLabel()
        bold = QFont(); bold.setBold(True)
        self._title_lbl.setFont(bold)
        outer.addWidget(self._title_lbl)

        self._hint_lbl = QLabel("Utrustningen har ingen nod än — välj/skapa\nen nod i trädet först.")
        self._hint_lbl.setStyleSheet("color:#8D9299; font-style:italic; font-size:10px;")
        self._hint_lbl.setWordWrap(True)
        outer.addWidget(self._hint_lbl)

        self._checklist_host = QWidget()
        self._checklist_layout = QVBoxLayout(self._checklist_host)
        self._checklist_layout.setContentsMargins(0, 0, 0, 0)
        self._checklist_layout.setSpacing(2)
        self._checklist_scroll = QScrollArea()
        self._checklist_scroll.setWidget(self._checklist_host)
        self._checklist_scroll.setWidgetResizable(True)
        self._checklist_scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Real height is set per-popup in show_near() from the available
        # screen space (2026-08-13 feedback: a fixed 220px looked "väldigt
        # kort" once the list grew to every deviation in the node, which
        # can be 16+ rows) — this fallback only matters before the first
        # show_near() call.
        self._checklist_scroll.setMaximumHeight(220)
        outer.addWidget(self._checklist_scroll)

        # Number-key shortcuts (1-9, see NOTES.md "snabbknappar") — a
        # QShortcut with WidgetWithChildrenShortcut fires as long as focus
        # is anywhere inside the popup (including its checkboxes), unlike
        # a plain keyPressEvent override which only fires when the popup
        # widget itself literally has focus.
        for i in range(1, 10):
            sc = QShortcut(QKeySequence(str(i)), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda n=i: self._toggle_checkbox_by_number(n))

    def _toggle_checkbox_by_number(self, number):
        idx = number - 1
        if 0 <= idx < len(self._checklist_checkboxes):
            cb = self._checklist_checkboxes[idx]
            if cb.isEnabled():
                cb.setChecked(True)

    @property
    def equipment_id(self):
        return self._equipment_id

    @property
    def marker_id(self):
        return self._marker_id

    def load(self, equipment_id, marker_id, active_node_id=None):
        """Populate the popup for the equipment behind `marker_id` (an
        equipment_markers.id — the caller already has this from the
        marker_clicked signal). Does not show/position the popup itself —
        call show_near() right after.

        `active_node_id` (PIDPanel._active_node_id — the node the user is
        already working in elsewhere in the app, see NOTES.md) is used to
        skip the manual node-picking step entirely when this equipment has
        no node yet: it's assigned immediately, not just pre-filled as a
        suggestion, so checking a deviation works right away — "jag vill
        inte behöva välja nod varje gång" (explicit user request)."""
        eq = self.db.get_equipment_by_id(equipment_id)
        if not eq:
            return
        if eq.get('node_id') is None and active_node_id is not None \
                and self.db.get_node(active_node_id) is not None:
            self.db.set_equipment_node(equipment_id, active_node_id)
            eq = self.db.get_equipment_by_id(equipment_id)
        self._equipment_id = equipment_id
        self._marker_id = marker_id
        self._title_lbl.setText(
            f"{eq['tag'] or f'Utrustning #{equipment_id}'} — {eq['equipment_type'] or '?'}")
        self._rebuild_checklist()

    def show_near(self, global_pos):
        """Show the popup anchored near global_pos (a QPoint), clamped to
        stay on-screen — same screen-clamped positioning hazop.py's own
        popups (RiskMatrixPopup etc.) already use. Clicking anywhere else
        closes it automatically (Qt.WindowType.Popup).

        The checklist's scroll area is sized to whatever room is actually
        available (opening downward or upward, whichever side of
        global_pos has more space) instead of a fixed height (2026-08-13
        feedback: "rulllistan väldigt kort på en liten skärm" — on a small
        screen the old fixed 220px cap left very little of the popup's
        already-small screen share usable, on top of now listing every
        deviation in the node instead of a short suggestion subset)."""
        scr = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()

        # Measure the checklist's true, uncapped height first so the cap
        # below never reserves blank space for a short list.
        self._checklist_scroll.setMaximumHeight(16777215)
        self.adjustSize()
        natural_total_h = self.sizeHint().height()
        natural_scroll_h = self._checklist_host.sizeHint().height()
        chrome_h = natural_total_h - natural_scroll_h   # title + hint + margins

        space_below = scr.bottom() - global_pos.y()
        space_above = global_pos.y() - scr.top()
        open_below = space_below >= space_above
        available = (space_below if open_below else space_above) - chrome_h - 12
        scroll_h = max(120, min(natural_scroll_h, available))
        self._checklist_scroll.setMaximumHeight(int(scroll_h))
        self.adjustSize()

        pw, ph = self.sizeHint().width(), self.sizeHint().height()
        x = min(global_pos.x(), scr.right() - pw)
        y = global_pos.y() if open_below else global_pos.y() - ph
        y = min(max(scr.top(), y), scr.bottom() - ph)
        self.move(max(scr.left(), x), y)
        self.show()
        self.setFocus()   # so number-key shortcuts work immediately

    def _rebuild_checklist(self):
        while self._checklist_layout.count():
            item = self._checklist_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        eq = self.db.get_equipment_by_id(self._equipment_id) if self._equipment_id else None
        if not eq:
            return
        node_id = eq.get('node_id')
        self._hint_lbl.setVisible(node_id is None)

        comp_type = eq['equipment_type'] or ''
        obj_id = self._resolve_object_id(comp_type)

        # Show every deviation already defined in this node (2026-08-13:
        # "jag vill att alla avvikelser i noden kommer upp som val inkl
        # nummer") — a node's deviations are usually decided up front (guide
        # words), so each piece of equipment in it should be checked
        # against ALL of them, not a generic standard-catalog subset for
        # its own equipment TYPE. The old type-based suggestion chain
        # (object → comp_type → full catalogue) only kicks in as a
        # bootstrap fallback for a brand new node that has no deviations
        # of its own yet, so the checklist still isn't empty.
        descriptions = []
        seen = set()

        def _add(desc):
            if desc not in seen:
                seen.add(desc)
                descriptions.append(desc)

        if node_id is not None:
            for d in self.db.deviations(node_id):
                _add(d['description'])
        if not descriptions:
            if obj_id is not None:
                for sd in self.db.deviations_for_object(obj_id):
                    _add(sd['description'])
        if not descriptions:
            rows = self.db.standard_causes_for_comp_type(comp_type) if comp_type else []
            for r in rows:
                _add(r['deviation_name'])
        if not descriptions:
            for sd in self.db.standard_deviations():
                _add(sd['description'])

        # Full row (not just a name-in-set check) so an already-checked
        # deviation's already-saved cause can be looked up and shown —
        # see _build_deviation_row's existing_dev handling.
        existing_by_desc = {d['description']: d
                             for d in self.db.deviations_for_equipment(self._equipment_id)}

        # Tracked in display order so number-key shortcuts (1-9, see
        # keyPressEvent) can toggle the matching row without the mouse —
        # explicit user request ("snabbknappar 1, 2.."). The number LABEL
        # is shown for every row regardless of count (2026-08-13); only
        # the keyboard shortcut itself is capped at 1-9 (a real key limit).
        self._checklist_checkboxes = []
        for i, desc in enumerate(descriptions, 1):
            row_w = self._build_deviation_row(desc, existing_by_desc.get(desc),
                                              enabled=node_id is not None, number=i,
                                              obj_id=obj_id)
            self._checklist_layout.addWidget(row_w)

    def _resolve_object_id(self, comp_type):
        """Match equipment_catalog.equipment_type against standard_objects
        (the richer taxonomy StandardCausesPickerPopup already uses) so the
        bar can offer the same breadth of causes — see NOTES.md."""
        if not comp_type:
            return None
        for o in self.db.standard_objects():
            if _obj_type_matches(comp_type, o['name']):
                return o['id']
        return None

    def _resolve_std_deviation_id(self, description):
        """Best-effort match of a real per-node deviation's description
        text against the standard_deviations catalogue, so the richer
        object-based cause suggestions (standard_causes_for_object) can
        still be tried for it — returns None for a custom/freeform
        deviation with no catalogue match, which just falls through to
        the plain comp_type-based lookup in _build_deviation_row."""
        row = self.db.conn.execute(
            "SELECT id FROM standard_deviations WHERE description=? COLLATE NOCASE LIMIT 1",
            (description,)).fetchone()
        return row[0] if row else None

    def _build_deviation_row(self, description, existing_dev, enabled, number, obj_id=None):
        row_w = QWidget()
        row = QHBoxLayout(row_w)
        row.setContentsMargins(0, 0, 0, 0)
        checked = existing_dev is not None
        num_lbl = QLabel(f"{number}.")
        num_lbl.setFixedWidth(20)
        num_lbl.setStyleSheet("color:#8D9299;")
        row.addWidget(num_lbl)
        cb = QCheckBox(description)
        cb.setChecked(checked)
        cb.setEnabled(enabled)   # can be checked AND unchecked — see NOTES.md "av-/aktivera"
        row.addWidget(cb)
        self._checklist_checkboxes.append(cb)

        # Top-suggested cause for this deviation, used to auto-create a
        # first cause on check (2026-08-12, see NOTES.md — the cause
        # combo/frequency editing this used to render inline is gone;
        # editing a cause's text/frequency now happens in the scenario
        # table, once it exists).
        eq = self.db.get_equipment_by_id(self._equipment_id)
        comp_type = eq['equipment_type'] if eq else ''
        std_dev_id = self._resolve_std_deviation_id(description)
        causes = []
        if obj_id is not None and std_dev_id is not None:
            causes = self.db.standard_causes_for_object(std_dev_id, obj_id)
        if not causes and comp_type:
            causes = self.db.standard_causes_for_comp_type(comp_type, description)
        if not causes and comp_type:
            causes = self.db.standard_causes_for_comp_type(comp_type)
        # standard_causes_for_object already returns plain dicts;
        # standard_causes_for_comp_type returns raw sqlite3.Row objects,
        # which don't support .get() — normalise both to dicts before any
        # .get() call here or in _activate_deviation's cause['frequency'].
        causes = [dict(c) for c in causes]
        if std_dev_id is not None:
            causes = [c for c in causes if c.get('deviation_id', std_dev_id) == std_dev_id]

        cb.toggled.connect(
            lambda on, desc=description, box=cb, cs=causes:
                self._on_deviation_toggled(desc, on, box, cs))
        return row_w

    def _on_deviation_toggled(self, description, checked, checkbox, causes):
        if checked:
            self._activate_deviation(description, checkbox, causes)
        else:
            self._deactivate_deviation(description, checkbox)

    def _activate_deviation(self, description, checkbox, causes):
        eq = self.db.get_equipment_by_id(self._equipment_id)
        node_id = eq.get('node_id') if eq else None
        if node_id is None:
            checkbox.blockSignals(True)
            checkbox.setChecked(False)
            checkbox.blockSignals(False)
            return
        dev_id = self.db.get_or_create_deviation(node_id, description, equipment_id=self._equipment_id)
        self.deviation_added.emit(dev_id, self._equipment_id)
        # Auto-create the top-suggested cause right away, same as before —
        # just without a dropdown here to change it; that's a scenario-
        # table edit now (see NOTES.md "Resten av valen får jag nog göra
        # nere i hazop scenario").
        if causes and self._create_cause_fn is not None:
            top = causes[0]
            self._create_cause_fn(dev_id, eq.get('equipment_type') or '', eq.get('tag') or '',
                                  top['description'], top.get('frequency'))

    def _deactivate_deviation(self, description, checkbox):
        """Kryssrutan ska gå att av-/aktivera (NOTES.md): unchecking now
        actually removes the deviation (db.delete_deviation — the same
        cascading delete the tree's own "Ta bort" already uses, so any
        causes/consequences/safeguards/markers under it go too), instead
        of the old v1 behavior where a checked box was locked forever.
        Confirms first if there's real data to lose, matching the exact
        confirmation pattern ScenarioTablePanel._delete_current_item
        already uses for "Ta bort orsak"/"Ta bort konsekvens".

        Softer confirmation (2026-08-09, see NOTES.md): the old message
        only counted causes ("har N orsak(er) kopplade"), understating
        the real cascade for a deviation whose causes each have their own
        consequences/safeguards — a checkbox uncheck looks like a light
        action but can silently delete a whole scenario tree. The message
        now spells out the full count and states plainly that it can't be
        undone, matching how destructive this action actually is."""
        eq = self.db.get_equipment_by_id(self._equipment_id)
        node_id = eq.get('node_id') if eq else None
        dev_id = (self.db.get_or_create_deviation(node_id, description, equipment_id=self._equipment_id)
                  if node_id is not None else None)
        if dev_id is None:
            return

        causes = self.db.causes_for_deviation(dev_id)
        n_causes = len(causes)
        if n_causes:
            n_cons = 0
            n_sg = 0
            for cause in causes:
                conss = self.db.consequences(cause['id'])
                n_cons += len(conss)
                for cons in conss:
                    n_sg += len(self.db.safeguards(cons['id']))
            parts = [f"{n_causes} orsak(er)"]
            if n_cons:
                parts.append(f"{n_cons} konsekvens(er)")
            if n_sg:
                parts.append(f"{n_sg} barriär(er)")
            reply = QMessageBox.question(
                self, "Ta bort avvikelse",
                f"Avvikelsen '{description}' tas bort tillsammans med "
                f"{', '.join(parts)}. Detta går inte att ångra.\n\n"
                "Vill du fortsätta?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                checkbox.blockSignals(True)
                checkbox.setChecked(True)
                checkbox.blockSignals(False)
                return

        self.db.delete_deviation(dev_id)
        self.deviation_removed.emit(dev_id, self._equipment_id)


class PIDPanel(QWidget):
    node_created            = pyqtSignal(int)
    cause_template_created  = pyqtSignal(int)
    marker_navigated        = pyqtSignal(str, int)
    equipment_deviation_created = pyqtSignal(int, int)   # (deviation_id, equipment_id)
    pid_analysis_done       = pyqtSignal()
    # Emitted when user right-clicks P&ID -> "🔧 Objekt"; MainWindow shows
    # EquipmentTagPopup then calls place_equipment_marker() (2026-08-07,
    # see NOTES.md).
    # Also emitted from the right-drag rubber-band menu's own "Objekt"
    # entry (2026-08-09) — pdf_rect then carries the drawn rectangle (PDF
    # units) so the marker gets a real outline shape instead of the
    # generic bowtie-icon fallback; None for the plain right-click (a
    # single point has no rectangle to give it).
    equipment_placement_requested = pyqtSignal(str, object, int, object)
    # (suggested_tag, scene_pos, page, pdf_rect_or_None)
    equipment_edit_requested = pyqtSignal(int)   # equipment_markers.id — bubbled from viewer
    ref_tag_picked            = pyqtSignal(str)   # forwarded from viewer after MODE_PICK_REF_TAG
    annotation_placed         = pyqtSignal(int)   # annotation id (feature 8)
    # Node markup signals
    markup_draw_finished    = pyqtSignal(str, int, list, int, str)  # type_, node_id, pts, page, label
    markup_item_selected    = pyqtSignal(int)                        # markup_id
    markup_moved            = pyqtSignal(int, list)                  # mu_id, new PDF pts
    markup_label_edited     = pyqtSignal(int, str)                   # mu_id, new_label
    markup_duplicate_requested = pyqtSignal(int)                     # mu_id
    # Red markup signals
    red_markup_draw_finished = pyqtSignal(str, int, list, int, str)  # type_, node_id, pts, page, label/symbol_id
    red_markup_item_selected = pyqtSignal(int)                        # mu_id
    red_markup_moved         = pyqtSignal(int, list)                  # mu_id, new PDF pts
    markup_symbol_dims_changed = pyqtSignal(int, float, float, float)  # mu_id, w, h, rot_deg
    board_layout_changed = pyqtSignal(str)

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db

        self._pen_color             = QColor(255, 140, 0)
        self._active_node_id              = None
        self._active_cause_id             = None
        self._active_consequence_id       = None
        self._active_deviation_id         = None   # kept in sync with the current tree selection
        self._active_markup_class         = 'node' # 'node' or 'red'
        self._active_symbol_id            = None   # set when red markup symbol tool selected
        self._pending_markup_pts          = None
        self._pending_markup_page         = None
        self._current_display_page  = 0
        self._smart_layout_prev      = None   # {page: (ox, oy)} for undo
        self._analyzer_thread        = None
        self._analyzer_progress_dlg  = None
        self._sheet_map: dict       = {}
        # Set by MainWindow to ScenarioTablePanel.active_edit_target
        # (2026-08-13, see NOTES.md) — lets a Shift+click on a marker
        # insert its tag into an already-open ORS/KON/SG cell editor
        # instead of navigating away and destroying it. None (and thus
        # a no-op) when PIDPanel is used standalone, e.g. in tests.
        self._active_edit_query_fn  = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        bar = QHBoxLayout(); bar.setSpacing(4)

        self.open_btn = QPushButton("Importera P&ID")
        self.open_btn.setIcon(_icon('import'))
        self.open_btn.clicked.connect(self._import_pdf)
        bar.addWidget(self.open_btn)

        self.analyze_btn = QPushButton("Analysera P&ID")
        self.analyze_btn.setIcon(_icon('document'))
        self.analyze_btn.setToolTip(
            "Skannar hela P&ID:n, identifierar alla taggnummer-prefix\n"
            "och skapar en nyckel i Inställningar → Identifierade objekt.")
        self.analyze_btn.clicked.connect(self._analyze_pid)
        self.analyze_btn.setEnabled(False)
        bar.addWidget(self.analyze_btn)

        self.export_btn = QPushButton("Exportera PDF")
        self.export_btn.setIcon(_icon('export'))
        self.export_btn.setToolTip(
            "Exportera P&ID med alla HAZOP-markeringar (nodgränser, orsaker,\n"
            "konsekvenser, barriärer och kopplingslinjer) som en ny PDF-fil.")
        self.export_btn.clicked.connect(self._export_pdf)
        self.export_btn.setEnabled(False)
        bar.addWidget(self.export_btn)

        bar.addWidget(_vline())

        self.prev_btn = QPushButton("◀")
        self.prev_btn.setFixedWidth(CONFIG['W_ICON_BTN'])
        self.prev_btn.clicked.connect(lambda: self._goto_page(self._current_display_page - 1))
        bar.addWidget(self.prev_btn)

        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setValue(1)
        self.page_spin.setFixedWidth(CONFIG['W_SPINNER'])
        self.page_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.page_spin.setToolTip("Skriv sidnummer och tryck Enter för att navigera")
        self.page_spin.editingFinished.connect(self._on_page_spin_changed)
        bar.addWidget(self.page_spin)

        self.page_total_label = QLabel("/ —")
        self.page_total_label.setMinimumWidth(35)
        bar.addWidget(self.page_total_label)

        self.next_btn = QPushButton("▶")
        self.next_btn.setFixedWidth(CONFIG['W_ICON_BTN'])
        self.next_btn.clicked.connect(lambda: self._goto_page(self._current_display_page + 1))
        bar.addWidget(self.next_btn)

        bar.addWidget(_vline())

        # Manual per-page rotation (2026-08-12, see NOTES.md) — rotates the
        # CURRENTLY VIEWED sheet 90° at a time, composed with (not replacing)
        # the PDF's own /Rotate flag. Deliberately separate from the
        # three-way "Sid-orientering" dropdown in P&ID-inställningar, which
        # NOTES.md documents as unread by rendering — this is a coarser but
        # actually-wired manual control for a specific rotated sheet.
        self.rotate_left_btn = QPushButton("⟲")
        self.rotate_left_btn.setFixedWidth(CONFIG['W_ICON_BTN'])
        self.rotate_left_btn.setToolTip("Rotera detta blad 90° moturs")
        self.rotate_left_btn.clicked.connect(lambda: self._rotate_page(-90))
        bar.addWidget(self.rotate_left_btn)

        self.rotate_right_btn = QPushButton("⟳")
        self.rotate_right_btn.setFixedWidth(CONFIG['W_ICON_BTN'])
        self.rotate_right_btn.setToolTip("Rotera detta blad 90° medurs")
        self.rotate_right_btn.clicked.connect(lambda: self._rotate_page(90))
        bar.addWidget(self.rotate_right_btn)

        bar.addWidget(_vline())

        # "⚙️ Orsak"/"⚠️ Konsekvens" mode-toggle buttons removed 2026-08-07
        # (see NOTES.md); the P&ID canvas is now equipment-object-placement-
        # only (2026-08-13, see NOTES.md) — Navigera is the only toolbar mode.
        self.mode_buttons = {}
        mode_defs = [
            (MODE_NAV,         "Navigera", 'search'),
        ]
        for mode, label, icon_name in mode_defs:
            btn = QPushButton(label)
            btn.setIcon(_icon(icon_name))
            btn.setCheckable(True)
            btn.clicked.connect(partial(self._set_mode, mode))
            bar.addWidget(btn)
            self.mode_buttons[mode] = btn

        bar.addWidget(_vline())

        self.style_widget = QWidget()
        sl = QHBoxLayout(self.style_widget)
        sl.setContentsMargins(0, 0, 0, 0); sl.setSpacing(4)

        sl.addWidget(QLabel("Tjocklek:"))
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 15); self.width_spin.setValue(3)
        self.width_spin.valueChanged.connect(self._update_pen)
        sl.addWidget(self.width_spin)

        sl.addWidget(QLabel("Transparens:"))
        self.alpha_slider = QSlider(Qt.Orientation.Horizontal)
        self.alpha_slider.setRange(20, 255); self.alpha_slider.setValue(220)
        self.alpha_slider.setFixedWidth(80)
        self.alpha_slider.valueChanged.connect(self._update_pen)
        sl.addWidget(self.alpha_slider)

        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(28, 28)
        self.color_btn.clicked.connect(self._pick_color)
        self._refresh_color_btn()
        sl.addWidget(self.color_btn)

        self.create_node_btn = QPushButton("Skapa Nod")
        self.create_node_btn.setIcon(_icon('check'))
        self.create_node_btn.setEnabled(False)
        self.create_node_btn.clicked.connect(self._create_node_from_markup)
        sl.addWidget(self.create_node_btn)

        self.style_widget.setVisible(False)
        bar.addWidget(self.style_widget)
        bar.addWidget(_vline())
        self.layout_btn = QPushButton("Layout")
        self.layout_btn.setIcon(_icon('resize-rotate'))
        self.layout_btn.setCheckable(True)
        self.layout_btn.setToolTip("Dra ritningsbladen för att ordna om dem")
        self.layout_btn.toggled.connect(self._on_layout_mode_toggled)
        bar.addWidget(self.layout_btn)

        self._annot_btn = QPushButton("Notering")
        self._annot_btn.setIcon(_icon('edit'))
        self._annot_btn.setCheckable(True)
        self._annot_btn.setToolTip("Klicka på brädet för att lägga till en klisterlapps-notering")
        self._annot_btn.toggled.connect(
            lambda on: (self._set_mode(MODE_ANNOTATION) if on
                        else self._set_mode(MODE_NAV)))
        bar.addWidget(self._annot_btn)

        self.smart_btn = QPushButton("Smart layout")
        self.smart_btn.setIcon(_icon('sparkle'))
        self.smart_btn.setToolTip(
            "Analyserar off-page connectors och föreslår optimal bladlayout (max 15 s)")
        self.smart_btn.clicked.connect(self._run_smart_layout)
        bar.addWidget(self.smart_btn)

        bar.addStretch()
        layout.addLayout(bar)

        # ── Viewer ────────────────────────────────────────────────────────────
        self.viewer = PIDGraphicsView()
        self.viewer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.viewer.node_markup_finished.connect(self._on_markup_finished)
        self.viewer.context_action.connect(self._on_context_action)
        self.viewer.zone_drawn.connect(self._on_zone_drawn)
        self.viewer.equipment_drag_finished.connect(self._on_equipment_drag_finished)
        self.viewer.equipment_edit_requested.connect(self.equipment_edit_requested.emit)
        self.viewer.ref_tag_picked.connect(self.ref_tag_picked)
        self.viewer.annotation_clicked.connect(self._on_annotation_click)
        self.viewer.marker_clicked.connect(self._on_marker_clicked)
        self.viewer.markup_moved.connect(self.markup_moved)
        self.viewer.markup_label_edited.connect(self.markup_label_edited)
        self.viewer.markup_duplicate_requested.connect(self.markup_duplicate_requested)
        self.viewer.markup_symbol_dims_changed.connect(self.markup_symbol_dims_changed)
        self.viewer.board_layout_changed.connect(self.board_layout_changed)
        self.viewer.board_layout_changed.connect(self._load_overlays)
        self.viewer.sheet_conn_break_requested.connect(self._break_sheet_link)
        self.viewer.sheet_conn_add_requested.connect(self._add_sheet_link)

        layout.addWidget(self.viewer)

        # A floating popup (Qt.WindowType.Popup), not docked into this
        # layout — see EquipmentDeviationBar's own docstring and
        # PIDPanel._on_marker_clicked/place_equipment_marker, which call
        # show_near() to position and show it (2026-08-12, see NOTES.md).
        self._equipment_bar = EquipmentDeviationBar(self.db, parent=self.viewer)
        self._equipment_bar.deviation_added.connect(self._on_equipment_deviation_added)
        self._equipment_bar.deviation_removed.connect(self._on_equipment_deviation_removed)
        # Plain callback, not a signal, so the popup gets the (created)
        # cause_id back synchronously — see EquipmentDeviationBar._create_cause_fn.
        self._equipment_bar._create_cause_fn = self._create_cause_for_bar

        self._set_mode(MODE_NAV)
        self._update_pen()

    def _analyze_pid(self):
        """Scan all PDF pages via the shared scan_pdf_for_equipment() pipeline
        (same one "🔍 Skanna P&ID" in Utrustningsregistret uses — see
        NOTES.md "Slå ihop Skanna/Analysera P&ID"), collect unique tag
        prefixes, and cross-ref with the tag database.

        Runs on background worker PROCESSES (ParallelTagScanWorker) when
        the document is large enough for multi-core parallelism to be
        worth it, with live per-page progress — see NOTES.md "Flerkärnig
        parallellisering av Analysera P&ID". Falls back to a single
        sequential pass automatically for small documents or if the
        process pool can't start."""
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Ingen P&ID", "Öppna en P&ID-fil först.")
            return

        # Offer OCR auto-install if no engine is available at all, then ask
        # whether to actually use it for this scan (same prompt as "🔍 Skanna
        # P&ID" in EquipmentPanel._scan, hazop.py) -- unless the user has
        # set a specific default engine in Inställningar → P&ID-inställningar
        # ("OCR-standardval"), in which case resolve_ocr_scan_choice() skips
        # the prompt and uses that engine directly.
        st = ocr_status()
        if not st['tesseract'] and not st['easyocr']:
            ensure_ocr_available(self)
        use_ocr, ocr_engine = resolve_ocr_scan_choice(self.db, self)

        path = self.db.get_pid_path()
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "Ingen P&ID", "Öppna en P&ID-fil först.")
            return
        n = self.viewer.pdf_doc.page_count

        dlg = PageProgressDialog("Analyserar P&ID…", n, self)
        worker = ParallelTagScanWorker(path, use_ocr=use_ocr, ocr_engine=ocr_engine)
        self._scan_thread = worker   # keep a reference so it isn't GC'd mid-run

        cancelled_flag = {'v': False}

        def _on_cancel():
            cancelled_flag['v'] = True
            worker.requestInterruption()

        def _on_finished(scan_result):
            dlg.close()
            self._scan_thread = None
            if cancelled_flag['v']:
                return

            real = {k: v for k, v in scan_result.items() if not k.startswith('_')}
            found = {pfx: set(data['tags']) for pfx, data in real.items()}
            if not found:
                QMessageBox.information(self, "Inga taggar",
                    "Inga taggnummer hittades i P&ID:n.")
                return

            # Shared with "🔍 Skanna P&ID" (EquipmentPanel._scan, hazop.py) —
            # both scan entry points now populate BOTH the per-tag equipment
            # register and the per-prefix "Identifierade objekt" list, so
            # results are identical regardless of which button was used.
            apply_scan_result_to_equipment_catalog(self.db, scan_result)
            upsert_identified_tags_from_scan(self.db, scan_result)

            QMessageBox.information(self, "Analys klar ✅",
                f"Hittade {len(found)} unika prefix.\n\n"
                "Öppna Inställningar → Identifierade objekt\n"
                "för att bekräfta typerna och aktivera 'Använd'.\n\n"
                "Utrustningsregistret har också uppdaterats.")
            self.pid_analysis_done.emit()

        worker.page_progress.connect(dlg.set_page_status)
        worker.finished_scan.connect(_on_finished)
        dlg.canceled.connect(_on_cancel)
        worker.start()
        dlg.exec()

    def _refresh_color_btn(self):
        c = self._pen_color
        self.color_btn.setStyleSheet(
            f"background:{c.name()}; border:1px solid #555; border-radius:3px;")

    def _pick_color(self):
        c = QColorDialog.getColor(self._pen_color, self, "Välj färg")
        if c.isValid():
            self._pen_color = c
            self._refresh_color_btn()
            self._update_pen()

    def _working_pdf_path(self):
        """Returns the project-local working copy path, e.g. hazop_project_pid.pdf."""
        db_path = Path(self.db.path)
        return db_path.with_name(db_path.stem + '_pid.pdf')

    def _rebuild_sheet_map(self):
        sheets = self.db.get_sheets()
        self._sheet_map = {i: int(s['physical_page']) for i, s in enumerate(sheets)}

    def _export_pdf(self):
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Export", "Öppna ett P&ID-dokument först.")
            return
        working = self._working_pdf_path()
        if not working.exists():
            QMessageBox.warning(self, "Export", "Ingen P&ID-fil att exportera.")
            return

        out_path, _ = QFileDialog.getSaveFileName(
            self, "Exportera P&ID med markup", "", "PDF-dokument (*.pdf)")
        if not out_path:
            return
        if not out_path.lower().endswith('.pdf'):
            out_path += '.pdf'

        sheets = self.db.get_sheets()
        page_order = ([int(s['physical_page']) for s in sheets]
                      if sheets else list(range(self.viewer.page_count())))

        prog = QProgressDialog("Exporterar P&ID…", None, 0, len(page_order), self)
        prog.setWindowTitle("Export")
        prog.setMinimumDuration(0)
        prog.setValue(0)
        QApplication.processEvents()

        try:
            src_doc = fitz.open(str(working))
        except Exception as e:
            prog.close()
            QMessageBox.critical(self, "Export misslyckades",
                                 f"Kunde inte öppna PDF:\n{e}")
            return

        out_doc = fitz.open()
        for phys in page_order:
            out_doc.insert_pdf(src_doc, from_page=phys, to_page=phys)

        for out_idx, phys_page in enumerate(page_order):
            prog.setValue(out_idx)
            QApplication.processEvents()
            page = out_doc.load_page(out_idx)

            # ── Node markup polygons ──────────────────────────────────────
            for node in self.db.nodes():
                nd = dict(node)
                if int(nd.get('pid_page', 0) or 0) != phys_page:
                    continue
                raw_pts = nd.get('markup_points', '') or ''
                if not raw_pts:
                    continue
                try:
                    pts = [fitz.Point(float(p[0]), float(p[1]))
                           for p in json.loads(raw_pts)]
                    style = json.loads(nd.get('markup_style', '') or '{}')
                except Exception:
                    continue
                if len(pts) < 2:
                    continue
                color = _hex_to_fitz_rgb(style.get('color', '#ff8c00'))
                width = max(0.5, style.get('width', 2) * 0.4)
                alpha = style.get('alpha', 120) / 255
                close = len(pts) >= 3
                shape = page.new_shape()
                shape.draw_polyline(pts + [pts[0]] if close else pts)
                try:
                    shape.finish(color=color, width=width,
                                 fill=color, fill_opacity=alpha * 0.35)
                except TypeError:
                    shape.finish(color=color, width=width)
                name = nd.get('name', '')
                if name and pts:
                    cx = sum(p.x for p in pts) / len(pts)
                    cy = sum(p.y for p in pts) / len(pts)
                    try:
                        shape.insert_text(
                            fitz.Point(cx - len(name) * 2.5, cy + 3.5),
                            name, fontsize=8, color=color, fontname='helv')
                    except Exception:
                        pass
                shape.commit()

            # ── Node markup overlays as editable PDF annotations ──────────
            if hasattr(self.db, 'node_markups_for_page'):
                node_ocgs = {}  # node_id -> OCG xref (one layer per node)

                for mu in self.db.node_markups_for_page(phys_page):
                    m = dict(mu)
                    if not m.get('visible', 1):
                        continue

                    # ── OCG: one layer per node, named after the node ─────
                    node_id = m.get('node_id')
                    if node_id not in node_ocgs:
                        node_row = (self.db.get_node(node_id)
                                    if hasattr(self.db, 'get_node') else None)
                        nname = (dict(node_row)['name'] if node_row
                                 else f'Nod {node_id}')
                        try:
                            node_ocgs[node_id] = out_doc.add_ocg(nname, on=True)
                        except Exception:
                            node_ocgs[node_id] = None
                    ocg_xref = node_ocgs[node_id]

                    # ── Parse geometry & style ─────────────────────────────
                    try:
                        pts_raw = json.loads(m.get('points', '[]') or '[]')
                        pts = [fitz.Point(float(p[0]), float(p[1]))
                               for p in pts_raw]
                    except Exception:
                        pts = []

                    rgb      = _hex_to_fitz_rgb(m.get('color', '#1565C0'))
                    opacity  = float(m.get('opacity', 0.45))
                    width    = max(0.5, int(m.get('line_width', 12)) * 0.4)
                    font_sz  = max(6, int(m.get('font_size', 12)))
                    mu_type  = m.get('type', 'polygon')
                    label    = m.get('label', '') or ''
                    # Light fill: blend stroke colour with white at 30%
                    fill_rgb = tuple(min(1.0, 0.70 + 0.30 * c) for c in rgb)

                    annot = None
                    try:
                        if mu_type == 'polygon' and len(pts) >= 2:
                            annot = page.add_polygon_annot(pts)
                            annot.set_colors({"stroke": list(rgb),
                                              "fill":   list(fill_rgb)})
                            annot.set_border({"width": width})
                            if label:
                                annot.set_info(title=label, content=label)
                            annot.update(opacity=opacity)

                        elif mu_type == 'polyline' and len(pts) >= 2:
                            annot = page.add_polyline_annot(pts)
                            annot.set_colors({"stroke": list(rgb)})
                            annot.set_border({"width": width})
                            if label:
                                annot.set_info(title=label)
                            annot.update(opacity=opacity)

                        elif mu_type in ('text', 'comment') and pts:
                            txt = label or '?'
                            x, y = pts[0].x, pts[0].y
                            rect_w = len(txt) * font_sz * 0.58 + 8
                            rect_h = font_sz * 1.7
                            rect   = fitz.Rect(x, y - rect_h,
                                               x + rect_w, y + 2)
                            bg = ([1.0, 1.0, 0.82] if mu_type == 'comment'
                                  else list(fill_rgb))
                            annot = page.add_freetext_annot(
                                rect, txt,
                                fontsize=font_sz,
                                fontname='helv',
                                text_color=list(rgb),
                                fill_color=bg)
                            annot.set_info(
                                title=('Kommentar' if mu_type == 'comment'
                                       else 'Nodnamn'),
                                content=txt)
                            annot.update(opacity=opacity)

                    except Exception:
                        pass  # skip faulty item; never crash the export

                    if annot is not None and ocg_xref:
                        try:
                            annot.set_oc(ocg_xref)
                        except Exception:
                            pass

            # ── Cause markers ─────────────────────────────────────────────
            cause_pos = {}
            for m in self.db.cause_markers_for_page(phys_page):
                md = dict(m)
                x, y = float(md['x']), float(md['y'])
                cause_pos[md['cause_id']] = (x, y)
                cause  = self.db.get_cause(md['cause_id'])
                desc   = dict(cause).get('description', '') if cause else ''
                tag    = md.get('component_tag', '') or md.get('component_type', '')
                _draw_pid_marker(page, x, y, (0.75, 0.18, 0.09), 'C', tag or desc)

            # ── Consequence markers ───────────────────────────────────────
            cons_pos = {}
            for m in self.db.consequence_markers_for_page(phys_page):
                md = dict(m)
                x, y = float(md['x']), float(md['y'])
                cons_pos[md['consequence_id']] = (x, y)
                cons = self.db.get_consequence(md['consequence_id'])
                desc = dict(cons).get('description', '') if cons else ''
                _draw_pid_marker(page, x, y, (0.87, 0.42, 0.06), 'K', desc)

            # ── Safeguard markers ─────────────────────────────────────────
            sg_pos = {}
            for m in self.db.safeguard_markers_for_page(phys_page):
                md = dict(m)
                x, y = float(md['x']), float(md['y'])
                sg_pos[md['safeguard_id']] = (x, y)
                row = self.db.conn.execute(
                    "SELECT description FROM safeguards WHERE id=?",
                    (md['safeguard_id'],)).fetchone()
                desc = row['description'] if row else ''
                tag  = md.get('tag', '')
                _draw_pid_marker(page, x, y, (0.15, 0.62, 0.27), 'S', tag or desc)

            # ── Connection lines ──────────────────────────────────────────
            shape = page.new_shape()
            for cid, cpos in cons_pos.items():
                c = self.db.get_consequence(cid)
                if c:
                    cause_id = dict(c).get('cause_id') if c else None
                    if cause_id and cause_id in cause_pos:
                        shape.draw_line(fitz.Point(*cause_pos[cause_id]),
                                        fitz.Point(*cpos))
                        shape.finish(color=(0.75, 0.18, 0.09), width=0.8)
            for sid, spos in sg_pos.items():
                s = self.db.get_safeguard(sid)
                if s:
                    cons_id = dict(s).get('consequence_id') if s else None
                    if cons_id and cons_id in cons_pos:
                        shape.draw_line(fitz.Point(*cons_pos[cons_id]),
                                        fitz.Point(*spos))
                    try:
                        shape.finish(color=(0.15, 0.62, 0.27), width=0.8,
                                     dashes="[3 3] 0")
                    except TypeError:
                        shape.finish(color=(0.15, 0.62, 0.27), width=0.8)
            shape.commit()

        src_doc.close()
        prog.setValue(len(page_order))
        QApplication.processEvents()

        try:
            out_doc.save(out_path, garbage=4, deflate=True)
            out_doc.close()
            prog.close()
            QMessageBox.information(self, "Export klar",
                                    f"P&ID exporterat med markup till:\n{out_path}")
        except Exception as e:
            out_doc.close()
            prog.close()
            QMessageBox.critical(self, "Export misslyckades",
                                 f"Kunde inte spara PDF:\n{e}")

    def _import_pdf(self):
        if not HAS_PYMUPDF:
            QMessageBox.warning(self, "PyMuPDF saknas",
                "Installera med:\n    pip install PyMuPDF\nStarta sedan om.")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Importera P&ID", "", "PDF-dokument (*.pdf);;Alla filer (*.*)")
        if not paths:
            return
        paths = sorted(paths)   # alphabetical merge order

        working      = self._working_pdf_path()
        has_existing = working.exists()
        created_at   = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')

        # Import always starts with a fresh (sequential) layout so that
        # smart layout can re-propose unbiased groupings afterwards.
        # Positions are only restored from DB on project reload (try_reload_pdf).

        if has_existing:
            dlg = PIDImportDialog(has_existing=True, parent=self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            rev_label = dlg.label() or created_at
            rev_notes = dlg.notes()

            if dlg.is_new_revision():
                # Merge all selected files into one new PDF → replace working copy
                try:
                    base_doc = fitz.open(paths[0])
                    for p in paths[1:]:
                        ext = fitz.open(p)
                        base_doc.insert_pdf(ext)
                        ext.close()
                    total_pages = base_doc.page_count
                    if self.viewer.pdf_doc is not None:
                        try: self.viewer.pdf_doc.close()
                        except Exception as e: logging.error(f"Failed to close previous PDF document: {e}")
                        self.viewer.pdf_doc = None
                    tmp_fd, tmp_path = tempfile.mkstemp(
                        suffix='.pdf', dir=str(working.parent))
                    os.close(tmp_fd)
                    base_doc.save(tmp_path, garbage=4, deflate=True)
                    base_doc.close()
                    shutil.move(tmp_path, str(working))
                except Exception as e:
                    QMessageBox.critical(self, "Fel", f"Kunde inte skapa PDF:\n{e}")
                    return
                prog = QProgressDialog(
                    f"Renderar P&ID ({total_pages} sidor)…", None, 0, total_pages, self)
                prog.setWindowTitle("Importerar P&ID")
                prog.setWindowModality(Qt.WindowModality.WindowModal)
                prog.setMinimumDuration(0)
                prog.setValue(0)
                QApplication.processEvents()
                if not self.viewer.load_pdf(
                        str(working), page=0,
                        layout_offsets=None,
                        progress_cb=lambda cur, tot: prog.setValue(cur),
                        page_rotations=self.db.get_all_page_rotations()):
                    prog.close()
                    QMessageBox.warning(self, "Fel", "Kunde inte öppna PDF-filen.")
                    return
                prog.setAutoClose(False)
                prog.setValue(total_pages)
                self.db.set_pid_path(str(working))
                self.db.clear_sheets()
                self.db.clear_page_rotations()
                self.db.add_revision(rev_label, rev_notes, str(working), created_at)
                self.db.ensure_sheets_initialized(self.viewer.page_count())
                self._current_display_page = 0

            else:
                # Append all selected files to the existing working PDF
                try:
                    existing_doc    = fitz.open(str(working))
                    existing_pg_cnt = existing_doc.page_count
                    n_new = 0
                    for p in paths:
                        ext = fitz.open(p)
                        n_new += ext.page_count
                        existing_doc.insert_pdf(ext)
                        ext.close()
                    total_pages = existing_pg_cnt + n_new
                    if self.viewer.pdf_doc is not None:
                        try: self.viewer.pdf_doc.close()
                        except Exception as e: logging.error(f"Failed to close existing PDF document during merge: {e}")
                        self.viewer.pdf_doc = None
                    tmp_fd, tmp_path = tempfile.mkstemp(
                        suffix='.pdf', dir=str(working.parent))
                    os.close(tmp_fd)
                    existing_doc.save(tmp_path, garbage=4, deflate=True)
                    existing_doc.close()
                    shutil.move(tmp_path, str(working))
                except Exception as e:
                    QMessageBox.critical(self, "Fel vid sammanslagning",
                                         f"Kunde inte sammanfoga PDF:\n{e}")
                    return

                prog = QProgressDialog(
                    f"Renderar P&ID ({total_pages} sidor)…", None, 0, total_pages, self)
                prog.setWindowTitle("Importerar P&ID")
                prog.setWindowModality(Qt.WindowModality.WindowModal)
                prog.setMinimumDuration(0)
                prog.setValue(0)
                QApplication.processEvents()
                keep_phys = self.viewer.current_page
                if not self.viewer.load_pdf(
                        str(working), page=keep_phys,
                        layout_offsets=None,
                        progress_cb=lambda cur, tot: prog.setValue(cur),
                        page_rotations=self.db.get_all_page_rotations()):
                    prog.close()
                    QMessageBox.warning(self, "Fel", "Kunde inte öppna sammanfogad PDF.")
                    return
                prog.setAutoClose(False)
                prog.setValue(total_pages)

                if self.db.get_display_page_count() == 0:
                    self.db.ensure_sheets_initialized(existing_pg_cnt)

                rev_id = self.db.add_revision(rev_label, rev_notes, str(working), created_at)
                physical_pages = list(range(existing_pg_cnt, existing_pg_cnt + n_new))
                sheet_names    = [f"Blad {existing_pg_cnt + i + 1}"
                                  for i in range(n_new)]
                self.db.append_sheets(physical_pages, sheet_names, rev_id)

        else:
            # First import — merge all selected files into working copy
            try:
                base_doc = fitz.open(paths[0])
                for p in paths[1:]:
                    ext = fitz.open(p)
                    base_doc.insert_pdf(ext)
                    ext.close()
                total_pages = base_doc.page_count
                tmp_fd, tmp_path = tempfile.mkstemp(
                    suffix='.pdf', dir=str(working.parent))
                os.close(tmp_fd)
                base_doc.save(tmp_path, garbage=4, deflate=True)
                base_doc.close()
                shutil.move(tmp_path, str(working))
            except Exception as e:
                QMessageBox.critical(self, "Fel", f"Kunde inte kopiera PDF:\n{e}")
                return
            prog = QProgressDialog(
                f"Renderar P&ID ({total_pages} sidor)…", None, 0, total_pages, self)
            prog.setWindowTitle("Importerar P&ID")
            prog.setWindowModality(Qt.WindowModality.WindowModal)
            prog.setMinimumDuration(0)
            prog.setValue(0)
            QApplication.processEvents()
            if not self.viewer.load_pdf(
                    str(working), page=0,
                    layout_offsets=None,
                    progress_cb=lambda cur, tot: prog.setValue(cur),
                    page_rotations=self.db.get_all_page_rotations()):
                prog.close()
                QMessageBox.warning(self, "Fel", "Kunde inte öppna PDF-filen.")
                return
            prog.setAutoClose(False)
            prog.setValue(total_pages)
            self.db.set_pid_path(str(working))
            self.db.clear_sheets()
            self.db.clear_page_rotations()
            self.db.add_revision(created_at, '', str(working), created_at)
            self.db.ensure_sheets_initialized(self.viewer.page_count())
            self._current_display_page = 0

        # Phase 2: apply active-page filter if needed, then load markers/connections
        self._rebuild_sheet_map()
        self._update_page_label()
        sheets = self.db.get_sheets()
        active = sorted(int(s['physical_page']) for s in sheets) if sheets else None
        already = sorted(self.viewer._all_page_items.keys())
        prog.setMaximum(0)
        prog.setLabelText("Laddar markeringar…")
        QApplication.processEvents()
        if active != already:
            # Active-page set differs from what was rendered (e.g. some sheets
            # were deleted before appending) — re-render to apply the filter.
            n_active = len(active) if active else 0
            prog.setMaximum(n_active)
            prog.setValue(0)
            prog.setLabelText(f"Bygger P&ID-vy ({n_active} sidor)…")
            QApplication.processEvents()
            self.viewer._render_all_pages(
                active_pages=active,
                progress_cb=lambda cur, tot: prog.setValue(cur))
            prog.setMaximum(0)
            prog.setLabelText("Laddar markeringar…")
            QApplication.processEvents()
        self._load_overlays()
        prog.close()
        self.analyze_btn.setEnabled(True)
        self.export_btn.setEnabled(True)

    def _goto_page(self, display_n):
        if self.viewer.pdf_doc is None:
            return
        total = len(self._sheet_map) if self._sheet_map else (
            self.db.get_display_page_count() or self.viewer.page_count())
        display_n = max(0, min(display_n, total - 1))
        # Feature 10: save zoom/scroll for the page we're leaving
        self._save_page_view(self._current_display_page)
        if self._sheet_map:
            physical = self._sheet_map.get(display_n, display_n)
        elif self.db.get_display_page_count() > 0:
            physical = self.db.get_sheet_physical_page(display_n)
        else:
            physical = display_n
        self._current_display_page = display_n
        self.viewer.goto_page(physical)
        self._update_page_label()
        # Feature 10: restore saved zoom/scroll for new page
        self._restore_page_view(display_n)

    def _on_page_spin_changed(self):
        self._goto_page(self.page_spin.value() - 1)

    def _current_physical_page(self):
        """Physical page number behind self._current_display_page — same
        display->physical resolution _goto_page uses, factored out here so
        _rotate_page (2026-08-12) doesn't duplicate/diverge from it."""
        display_n = self._current_display_page
        if self._sheet_map:
            return self._sheet_map.get(display_n, display_n)
        if self.db.get_display_page_count() > 0:
            return self.db.get_sheet_physical_page(display_n)
        return display_n

    def _rotate_page(self, delta_degrees):
        """Rotate the currently-viewed sheet by delta_degrees (+-90),
        composed on top of whatever the PDF file itself already declares via
        /Rotate (2026-08-12, see NOTES.md "P&ID-sidrotation").

        Markers/zones are stored in PDF-space (see cause_markers etc. in
        CLAUDE.md's schema table), which this changes — every position
        recorded for this physical page must be re-anchored to the same
        physical point at the same time, or a rotated page would silently
        move every marker on it. The transform is built from this page's
        own derotation_matrix (old-rotated -> raw/physical-invariant space)
        composed with its new rotation_matrix (raw -> new-rotated space),
        i.e. the exact inverse of how equipment_detection._rotate_words
        already converts raw PyMuPDF coordinates into this app's PDF-space.
        """
        if self.viewer.pdf_doc is None or not HAS_PYMUPDF:
            return
        physical = self._current_physical_page()
        page = self.viewer.pdf_doc.load_page(physical)
        old_extra = self.viewer._page_rotation_override.get(physical, 0)
        old_derot = page.derotation_matrix   # current (pre-change) rotated-space -> raw
        new_extra = (old_extra + delta_degrees) % 360

        self.viewer.set_page_rotation_override(physical, new_extra)  # mutates page.rotation in place
        new_rotmat = page.rotation_matrix    # raw -> new rotated-space, reflects the change above

        def _tf(x, y):
            raw = fitz.Point(float(x), float(y)) * old_derot
            newp = raw * new_rotmat
            return (newp.x, newp.y)

        self.db.set_page_rotation(physical, new_extra)
        self.db.remap_page_rotation_positions(physical, _tf, angle_delta_deg=delta_degrees)

        self._save_page_view(self._current_display_page)
        active = sorted(self.viewer._all_page_items.keys())
        # Preserve whatever board layout (auto-flow or a user-dragged custom
        # arrangement, see "📐 Layout") is currently on screen — only the
        # rotated page's own footprint changes (width/height swap for a
        # +-90 turn), everything else keeps its existing position. A
        # rotated page's new footprint can end up overlapping its neighbour
        # in that case — known limitation, see NOTES.md.
        layout_offsets = dict(self.viewer._page_offsets)
        self.viewer._render_all_pages(active_pages=active, layout_offsets=layout_offsets)
        self._load_overlays()
        self._restore_page_view(self._current_display_page)

    def _update_page_label(self):
        total = len(self._sheet_map) if self._sheet_map else (
            self.db.get_display_page_count() or self.viewer.page_count())
        if total > 0:
            self.page_spin.blockSignals(True)
            try:
                self.page_spin.setRange(1, total)
                self.page_spin.setValue(self._current_display_page + 1)
            finally:
                self.page_spin.blockSignals(False)
            self.page_total_label.setText(f"/ {total}")
        else:
            self.page_spin.blockSignals(True)
            try:
                self.page_spin.setRange(1, 1)
                self.page_spin.setValue(1)
            finally:
                self.page_spin.blockSignals(False)
            self.page_total_label.setText("/ —")

    # ── Feature 10: per-page zoom/scroll state ────────────────────────────────

    def _save_page_view(self, display_n):
        if not hasattr(self, '_page_views'):
            self._page_views = {}
        t = self.viewer.transform()
        self._page_views[display_n] = (t.m11(), t.m12(), t.m21(), t.m22(),
                                       self.viewer.horizontalScrollBar().value(),
                                       self.viewer.verticalScrollBar().value())

    def _restore_page_view(self, display_n):
        if not hasattr(self, '_page_views'):
            return
        state = self._page_views.get(display_n)
        if not state:
            return
        from PyQt6.QtGui import QTransform
        m11, m12, m21, m22, hv, vv = state
        self.viewer.setTransform(QTransform(m11, m12, m21, m22, 0, 0))
        # Block scrollContentsBy from firing _schedule_lod_update while we restore
        self.viewer.horizontalScrollBar().blockSignals(True)
        self.viewer.verticalScrollBar().blockSignals(True)
        self.viewer.horizontalScrollBar().setValue(hv)
        self.viewer.verticalScrollBar().setValue(vv)
        self.viewer.horizontalScrollBar().blockSignals(False)
        self.viewer.verticalScrollBar().blockSignals(False)
        self.viewer._apply_lod(self.viewer.transform().m11())

    def navigate_to_marker(self, physical_page, x_pdf, y_pdf):
        """Navigate to the page containing a marker and zoom in on it."""
        if self.viewer.pdf_doc is None:
            return
        display_n = physical_page
        if self._sheet_map:
            rev = {phys: disp for disp, phys in self._sheet_map.items()}
            display_n = rev.get(physical_page, physical_page)
        # Skip view-state save/restore for navigate_to_marker — we override the
        # transform immediately with resetTransform + scale + centerOn anyway.
        self._save_page_view(self._current_display_page)
        if self._sheet_map:
            physical = self._sheet_map.get(display_n, display_n)
        elif self.db.get_display_page_count() > 0:
            physical = self.db.get_sheet_physical_page(display_n)
        else:
            physical = display_n
        self._current_display_page = display_n
        self.viewer.goto_page(physical)
        self._update_page_label()
        scene_pt = self.viewer.pdf_to_scene(x_pdf, y_pdf, page=physical_page)
        self.viewer.resetTransform()
        self.viewer.scale(2.5, 2.5)
        self.viewer.centerOn(scene_pt)
        self.viewer._apply_lod(self.viewer.transform().m11())
        self.viewer._schedule_lod_update()

    def _set_mode(self, mode):
        for m, btn in self.mode_buttons.items():
            btn.setChecked(m == mode)
        self.viewer.set_mode(mode)
        self.style_widget.setVisible(mode == MODE_NODE)

    def _on_equipment_drag_finished(self):
        """Shift-dragging an equipment marker to the tree/scenario uses
        QDrag.exec(), a native drag loop that suppresses the normal
        hover/leave events toolbar buttons rely on to clear their pressed
        look — the "🔍 Navigera" button could be left visually stuck
        looking pressed in after the drop even though nothing is actually
        held down anymore. Force every mode button's transient down/hover
        state to release right as the drop itself is released."""
        for btn in self.mode_buttons.values():
            btn.setDown(False)
            btn.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
            btn.update()

    def _on_layout_mode_toggled(self, checked):
        if checked:
            self._set_mode(MODE_BOARD_LAYOUT)
        else:
            self._set_mode(MODE_NAV)

    def _break_sheet_link(self, conn_id: int):
        """Delete a sheet connection from DB and redraw arcs."""
        if conn_id < 0:
            return
        self.db.delete_pid_connection(conn_id)
        self._load_overlays()

    def _add_sheet_link(self, from_page: int, to_page: int):
        """Create a manual sheet connection in DB and redraw arcs."""
        self.db.add_manual_pid_connection(from_page, to_page)
        self._load_overlays()

    def _draw_sheet_connections(self):
        """Draw one bezier arc per connector symbol.

        Iterates over pid_connection records (reliable from_page/to_page pairs),
        then for each page-pair draws one line per individual connector symbol —
        exiting from the edge where the symbol was detected.
        """
        import json as _json
        connections = self.db.get_pid_connections()
        if not connections:
            return
        connectors = self.db.get_connectors()
        raw_map = self.db.get_pid_config_value('sheet_num_map') or '{}'
        try:
            sheet_num_map = {int(k): v.upper()
                             for k, v in _json.loads(raw_map).items()}
        except Exception:
            sheet_num_map = {}

        # Build direction-aware lookups.
        # 'out' = outgoing connector (pentagon leaving the page)
        # 'in'  = incoming connector (rectangle receiving flow)
        # 'any' = unknown direction — included in both buckets
        _pair_out: dict = {}  # (pid_page, ref_page) → [out connectors]
        _pair_in:  dict = {}  # (pid_page, ref_page) → [in  connectors]
        _pair_any: dict = {}  # (pid_page, ref_page) → [all connectors]
        _ref_out:  dict = {}  # (pid_page, ref_sheet) → [out]
        _ref_in:   dict = {}  # (pid_page, ref_sheet) → [in]
        _ref_any:  dict = {}  # (pid_page, ref_sheet) → [all]
        for c in connectors:
            cd   = dict(c)
            dirn = (cd.get('direction') or '').lower()
            rp   = cd.get('ref_page')
            ref  = (cd.get('ref_sheet') or '').upper()
            # direction buckets: unknown goes into both out AND in
            buckets_pair = (
                [_pair_out, _pair_any] if dirn == 'out' else
                [_pair_in,  _pair_any] if dirn == 'in'  else
                [_pair_out, _pair_in, _pair_any]
            )
            buckets_ref = (
                [_ref_out, _ref_any] if dirn == 'out' else
                [_ref_in,  _ref_any] if dirn == 'in'  else
                [_ref_out, _ref_in, _ref_any]
            )
            if rp is not None:
                key_p = (cd['pid_page'], int(rp))
                for d in buckets_pair:
                    d.setdefault(key_p, []).append(cd)
            for v in _sheet_ref_variants(ref):
                key_r = (cd['pid_page'], v)
                for d in buckets_ref:
                    d.setdefault(key_r, []).append(cd)

        def _get_connectors(page, target_page, target_sheet_str, prefer='out'):
            """Direction-aware connector lookup.
            prefer='out' for src_list, 'in' for dst_list.
            Falls back: preferred direction → any direction → ref_sheet fuzzy.
            """
            pref_pair = _pair_out if prefer == 'out' else _pair_in
            pref_ref  = _ref_out  if prefer == 'out' else _ref_in
            for lookup_pair, lookup_ref in [(pref_pair, pref_ref),
                                            (_pair_any, _ref_any)]:
                r = lookup_pair.get((page, target_page), [])
                if r:
                    return r
                for v in _sheet_ref_variants(target_sheet_str):
                    r = lookup_ref.get((page, v), [])
                    if r:
                        return r
            return []

        drawn_pairs:      set  = set()
        gap_slot_counter: dict = {}
        rs = self.viewer.render_scale

        def _edge_pt(c, ox, oy, pw, ph, fallback_edge):
            """Scene point at the connector symbol position on the P&ID.

            Uses the raw x_pdf/y_pdf coordinates — the actual location of the
            connector symbol on the drawing.  The bezier control points (in
            add_sheet_conn_arc) still use src_edge/dst_edge to push the curve
            outward in the correct direction, so the bezier exits cleanly even
            though it starts/ends at the symbol rather than the page edge.

            Falls back to the edge midpoint when coordinates are missing.
            """
            if c:
                xp = c.get('x_pdf')
                yp = c.get('y_pdf')
                if xp is not None and yp is not None:
                    return QPointF(ox + xp * rs, oy + yp * rs)
            if fallback_edge == 'right':  return QPointF(ox + pw,    oy + ph / 2)
            if fallback_edge == 'left':   return QPointF(ox,          oy + ph / 2)
            if fallback_edge == 'top':    return QPointF(ox + pw / 2, oy)
            return                               QPointF(ox + pw / 2, oy + ph)

        for row in connections:
            conn = dict(row)
            fp = conn.get('from_page')
            tp = conn.get('to_page')
            if fp is None or tp is None or fp == tp:
                continue
            if fp not in self.viewer._all_page_items or tp not in self.viewer._all_page_items:
                continue

            media      = conn.get('media_type', 'unknown') or 'unknown'
            color_hex  = _MEDIA_COLORS.get(media, _MEDIA_COLORS['unknown'])
            confidence = float(conn.get('confidence', 0.5))
            bidir      = bool(conn.get('is_bidirectional'))
            weight     = float(conn.get('weight', 0.5) or 0.5)
            conn_id    = conn.get('id', -1)

            ox_fp, oy_fp = self.viewer._page_offsets.get(fp, (0, 0))
            ox_tp, oy_tp = self.viewer._page_offsets.get(tp, (0, 0))
            pw_fp = self.viewer._page_widths_pdf.get(fp, 800) * rs
            ph_fp = self.viewer._page_heights_pdf.get(fp, 600) * rs
            pw_tp = self.viewer._page_widths_pdf.get(tp, 800) * rs
            ph_tp = self.viewer._page_heights_pdf.get(tp, 600) * rs

            fp_sheet = sheet_num_map.get(fp, '')
            tp_sheet = sheet_num_map.get(tp, '')

            # Outgoing connectors on fp (departure symbols) and
            # incoming connectors on tp (arrival symbols).
            src_list = _get_connectors(fp, tp, tp_sheet, prefer='out')
            dst_list = _get_connectors(tp, fp, fp_sheet, prefer='in')

            # Fallback edges from relative page positions (horizontal only)
            dx_pages = ox_tp - ox_fp
            def_src = 'right' if dx_pages >= 0 else 'left'
            def_dst = 'left'  if dx_pages >= 0 else 'right'

            def _make_dot(c, fallback_pt, page_ox, page_oy, c_hex):
                """Create a draggable ConnectorDotItem at the connector's P&ID position.

                Priority: 1) manually saved position, 2) x_pdf/y_pdf on the drawing,
                3) fallback to the bezier endpoint.
                """
                if c is not None:
                    sx = c.get('dot_scene_x')
                    sy = c.get('dot_scene_y')
                    if sx is not None and sy is not None:
                        pos = QPointF(sx, sy)
                    else:
                        xp = c.get('x_pdf')
                        yp = c.get('y_pdf')
                        pos = QPointF(page_ox + xp * rs, page_oy + yp * rs) \
                              if xp is not None and yp is not None else fallback_pt
                    cid = c.get('id', -1)
                else:
                    pos = fallback_pt
                    cid = -1
                dot = ConnectorDotItem(cid, self.db, c_hex, pos)
                self.viewer._scene.addItem(dot)

            if not src_list:
                # No connectors detected on fp side — draw one fallback line
                pair_key = (fp, tp, media)
                if pair_key in drawn_pairs:
                    continue
                drawn_pairs.add(pair_key)
                drawn_pairs.add((tp, fp, media))
                dst_c  = dst_list[0] if dst_list else None
                src_pt = _edge_pt(None, ox_fp, oy_fp, pw_fp, ph_fp, def_src)
                dst_pt = _edge_pt(dst_c, ox_tp, oy_tp, pw_tp, ph_tp,
                                  dst_c.get('edge', def_dst) if dst_c else def_dst)
                src_edge = def_src
                dst_edge = dst_c.get('edge', def_dst) if dst_c else def_dst
                label = media.replace('_', ' ').upper()
                gap_key = (min(fp, tp), max(fp, tp))
                arc_idx = gap_slot_counter.get(gap_key, 0)
                gap_slot_counter[gap_key] = arc_idx + 1
                self.viewer.add_sheet_conn_arc(
                    src_pt, dst_pt, color_hex, confidence, label, bidir,
                    conn_id=conn_id, src_edge=src_edge, dst_edge=dst_edge,
                    arc_index=arc_idx, weight=weight)
                _make_dot(None,  src_pt, ox_fp, oy_fp, color_hex)
                _make_dot(dst_c, dst_pt, ox_tp, oy_tp, color_hex)
                continue

            # One line per src connector — match to nearest dst connector by Y
            used_dst = set()
            for sc in src_list:
                pair_key = (fp, id(sc), media)
                if pair_key in drawn_pairs:
                    continue
                drawn_pairs.add(pair_key)

                # Find nearest unmatched dst connector
                avail = [d for d in dst_list if id(d) not in used_dst]
                if avail:
                    sy = sc.get('y_pdf', 0)
                    dc = min(avail, key=lambda d: abs(d.get('y_pdf', 0) - sy))
                    used_dst.add(id(dc))
                else:
                    dc = None

                src_edge = sc.get('edge') or def_src
                dst_edge = dc.get('edge') if dc else def_dst
                if not dst_edge:
                    dst_edge = def_dst

                src_pt = _edge_pt(sc, ox_fp, oy_fp, pw_fp, ph_fp, src_edge)
                dst_pt = _edge_pt(dc, ox_tp, oy_tp, pw_tp, ph_tp, dst_edge)

                rt = sc.get('raw_text', '')
                rt_clean = re.sub(r'=[\w./\-]+', '', rt)
                rt_clean = re.sub(r'\bS\d{6,8}\b', '', rt_clean, flags=re.I)
                label = ' '.join(rt_clean.split())[:28].strip() or \
                        media.replace('_', ' ').upper()

                gap_key = (min(fp, tp), max(fp, tp))
                arc_idx = gap_slot_counter.get(gap_key, 0)
                gap_slot_counter[gap_key] = arc_idx + 1

                self.viewer.add_sheet_conn_arc(
                    src_pt, dst_pt, color_hex, confidence, label, bidir,
                    conn_id=conn_id, src_edge=src_edge, dst_edge=dst_edge,
                    arc_index=arc_idx, weight=weight)
                _make_dot(sc, src_pt, ox_fp, oy_fp, color_hex)
                _make_dot(dc, dst_pt, ox_tp, oy_tp, color_hex)

    def _run_smart_layout(self):
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.information(self, "Smart layout",
                "Öppna en P&ID-fil (PDF) först.")
            return
        if self._analyzer_thread and self._analyzer_thread.isRunning():
            return
        # Note: a running analysis can never reach this point (guard above
        # returns first), so the old thread here — if any — is guaranteed to
        # have already finished or failed. No need to quit()/wait() on it.
        # Disconnect old thread's signal to prevent stale double-fires
        if self._analyzer_thread is not None:
            try:
                self._analyzer_thread.finished_analysis.disconnect(self._on_smart_layout_done)
            except Exception:
                pass
        # Save current layout for undo
        self._smart_layout_prev = dict(self.viewer._page_offsets)
        self.smart_btn.setEnabled(False)
        self.smart_btn.setText("⏳ Analyserar…")

        path         = self.db.get_pid_path()
        active_pages = sorted(self.viewer._all_page_items.keys())
        self._analyzer_thread = ConnectorAnalyzer(
            path,
            self.viewer.pdf_doc.page_count,
            self.viewer._page_widths_pdf,
            self.viewer._page_heights_pdf,
            self.viewer.render_scale,
            active_pages=active_pages,
        )
        self._analyzer_thread.progress.connect(
            lambda msg: self.smart_btn.setText(f"⏳ {msg}"))
        self._analyzer_thread.finished_analysis.connect(self._on_smart_layout_done)
        self._analyzer_thread.start()

        self._analyzer_progress_dlg = QProgressDialog(
            "Analyserar P&ID-kopplingar…", None, 0, 0, self)
        self._analyzer_progress_dlg.setWindowTitle("Smart layout")
        self._analyzer_progress_dlg.setWindowModality(Qt.WindowModality.WindowModal)
        self._analyzer_progress_dlg.setMinimumDuration(0)
        self._analyzer_progress_dlg.show()
        self._analyzer_thread.progress.connect(
            lambda msg: self._analyzer_progress_dlg.setLabelText(msg)
            if self._analyzer_progress_dlg else None)
        QApplication.processEvents()

    def _on_smart_layout_done(self, connectors, connections, layout, sheet_num_map):
        if self._analyzer_progress_dlg is not None:
            self._analyzer_progress_dlg.close()
            self._analyzer_progress_dlg = None
        self.smart_btn.setEnabled(True)
        self.smart_btn.setText("Smart layout")

        if not layout:
            QMessageBox.information(self, "Smart layout",
                "Inga off-page connectors hittades — kan inte föreslå layout.")
            return

        # Save to DB
        self.db.clear_connector_analysis()
        self.db.save_connectors(connectors)
        self.db.save_pid_connections(connections)

        import json as _json
        # Save sheet-number map (page_idx → sheet_num_str) for visual arc drawing
        self.db.set_pid_config_value('sheet_num_map', _json.dumps(sheet_num_map))

        # Apply layout (scene coords = pdf_coords * render_scale already in layout dict)
        for pn, (x, y) in layout.items():
            if pn in self.viewer._all_page_items:
                self.viewer._page_offsets[pn] = (x, y)
                self.viewer._all_page_items[pn].setPos(x, y)

        self.viewer._update_board_scene_rect()
        self._load_overlays()

        # Save board layout to DB
        layout_data = {str(p): list(off)
                       for p, off in self.viewer._page_offsets.items()}
        self.db.set_pid_config_value('board_layout', _json.dumps(layout_data))

        n_conn   = sum(1 for c in connections if not c.get('is_ghost'))
        n_ghost  = sum(1 for c in connections if c.get('is_ghost'))
        n_sheets = self.viewer.pdf_doc.page_count
        msg = (f"Layout klar — {len(connectors)} connectors, "
               f"{n_conn} kopplingar, {n_ghost} externa")
        if n_ghost:
            msg += f"\n({n_ghost} ritningar refererade men ej i workboard)"

        box = QMessageBox(self)
        box.setWindowTitle("Smart layout")
        box.setText(msg)
        undo_btn = box.addButton("Ångra", QMessageBox.ButtonRole.ResetRole)
        undo_btn.setIcon(_icon('undo'))
        box.addButton("OK", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() == undo_btn:
            self._undo_smart_layout()

    def _undo_smart_layout(self):
        if self._smart_layout_prev is None:
            return
        for pn, (ox, oy) in self._smart_layout_prev.items():
            if pn in self.viewer._all_page_items:
                self.viewer._page_offsets[pn] = (ox, oy)
                self.viewer._all_page_items[pn].setPos(ox, oy)
        self.viewer._update_board_scene_rect()
        self._load_overlays()
        self._smart_layout_prev = None

    def _update_pen(self):
        self.viewer.set_pen_style(
            self._pen_color, self.width_spin.value(), self.alpha_slider.value())

    def _on_markup_finished(self, pts, page):
        self._pending_markup_pts  = pts
        self._pending_markup_page = page
        self.create_node_btn.setEnabled(True)

    def _create_node_from_markup(self):
        if not self._pending_markup_pts:
            return
        name, ok = QInputDialog.getText(self, "Ny nod", "Namn på nod:", text="Ny nod")
        if not ok:
            return
        name  = name.strip() or "Ny nod"
        style = {'color': self._pen_color.name(),
                 'width': self.width_spin.value(),
                 'alpha': self.alpha_slider.value()}
        node_id = self.db.add_node_with_markup(
            name, self._pending_markup_pts, style, self._pending_markup_page)
        self._pending_markup_pts  = None
        self._pending_markup_page = None
        self.create_node_btn.setEnabled(False)
        self._load_overlays()
        self.node_created.emit(node_id)

    def _on_zone_drawn(self, pdf_rect, page):
        """Right-drag rubber band completed — places a new equipment object
        with the drawn rectangle as its outline shape (2026-08-09, see
        NOTES.md), instead of the generic bowtie-icon fallback the plain
        right-click "🔧 Objekt" action uses. Used to offer a menu of
        objekt/orsak/konsekvens/safeguard here; the P&ID canvas is now
        object-placement-only (2026-08-13, see NOTES.md), so the drawn
        zone always becomes a new equipment object directly — no menu
        needed for a single choice."""
        rs = self.viewer.render_scale
        center_scene = QPointF(pdf_rect.center().x() * rs, pdf_rect.center().y() * rs)

        tag = ''
        if HAS_PYMUPDF and self.viewer.pdf_doc:
            # _extract_tag_from_rect returns a string (the tag found inside the
            # rectangle).  Do NOT index into it — that yields single characters.
            try:
                tag = self.viewer._extract_tag_from_rect(pdf_rect) or ''
            except Exception:
                pass
            # If no tag found inside the rectangle, search the full page from
            # the rectangle's centre — tag labels often sit outside the symbol.
            if not tag:
                cx = pdf_rect.center().x()
                cy = pdf_rect.center().y()
                tag = find_tag_near_point(self.viewer.pdf_doc, page, cx, cy)

        self.equipment_placement_requested.emit(tag or '', center_scene, page, pdf_rect)

    def _on_annotation_click(self, scene_pos):
        """Feature 8: create a sticky note annotation at scene_pos."""
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, 'Ny notering', 'Anteckning:')
        if not ok or not text.strip():
            self._set_mode(MODE_NAV); self._annot_btn.setChecked(False); return
        ann_id = self.db.add_board_annotation(
            scene_pos.x(), scene_pos.y(), text=text.strip())
        self._draw_annotation(ann_id, scene_pos.x(), scene_pos.y(),
                              200.0, 80.0, text.strip(), '#fff9c4')
        self._set_mode(MODE_NAV); self._annot_btn.setChecked(False)
        self.annotation_placed.emit(ann_id)

    def _draw_annotation(self, ann_id, x, y, w, h, text, color):
        # Use QGraphicsRectItem + child QGraphicsTextItem so they move together
        # and are treated as one unit by clear_overlays (remove parent = remove child)
        rect = self.viewer._scene.addRect(
            QRectF(0, 0, w, h),        # local coords: origin at (0,0)
            QPen(QColor('#f9a825'), 2),
            QBrush(QColor(color)))
        rect.setPos(x, y)
        rect.setFlag(rect.GraphicsItemFlag.ItemIsMovable, True)
        rect.setFlag(rect.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        rect.setZValue(Z_TEMP + 2)
        rect.setData(0, ('annotation', ann_id))

        # Text as child of rect — moves with it, removed when rect is removed
        from PyQt6.QtWidgets import QGraphicsTextItem
        txt = QGraphicsTextItem(text, rect)   # rect is parent → child of scene via rect
        txt.setPos(6, 4)
        txt.setTextWidth(w - 12)
        txt.setZValue(0.1)   # slightly above parent (rect is at Z_TEMP+2 in scene coords)

    def _on_context_action(self, action, pos, page):
        if action == 'node':
            self._set_mode(MODE_NODE)
        elif action == 'equipment':
            pdf_x, pdf_y = self.viewer.scene_to_pdf(pos)
            tag = find_tag_near_point(
                self.viewer.pdf_doc, page, pdf_x, pdf_y, radius=100) \
                if self.viewer.pdf_doc else ''
            self.equipment_placement_requested.emit(tag or '', pos, page, None)
        elif action == 'find_similar':
            self._find_similar_symbol(pos, page)
        elif action == 'find_similar_template':
            self._find_similar_symbol_from_template()

    def _find_similar_symbol(self, pos, page):
        """🔎 Hitta liknande symbol (2026-08-10, see NOTES.md) — the click
        point becomes the reference shape; every other vector-drawn
        cluster in the document is scored against it
        (equipment_detection._scan_candidates / symbol_geometry.
        cluster_similarity) and surfaced through the same
        EquipmentMarkerReviewDialog "🎯 Hitta objekt på P&ID" already
        uses, so confirming/renaming/saving works identically.

        2026-08-14 (see NOTES.md "Hitta liknande symbol" —
        sökparametrar): the reference cluster is now resolved FIRST
        (equipment_detection.resolve_reference_cluster) and shown in
        SimilarSymbolSearchDialog for pruning + search-parameter
        choices (threshold/scale/rotation/scope) before the actual
        search runs — previously this ran immediately with fixed
        defaults and no way to fix a reference cluster that pulled in
        an unwanted neighbour (e.g. a pipe stub next to a valve).

        2026-08-15 (see NOTES.md "Hitta liknande symbol" —
        uppföljningsfunktioner): the dialog now runs the document scan
        itself, in a background thread, with live progress/cancel and
        a live match-count/on-canvas preview as the threshold slider
        moves — so by the time it's accepted the result is already
        computed; final_results() reuses it directly instead of this
        method running find_similar_shapes() a second time.

        2026-08-15 follow-up (see NOTES.md "'Hitta liknande symbol'
        visar bara ett streck"): the reference canvas now also shows
        every primitive within a generous radius of the click point
        (symbol_geometry.primitives_near_point), not just what
        auto-detection grouped together — everything outside the
        auto-detected core starts excluded/unchecked, but is still
        there to click and ADD. Needed on very densely-fragmented CAD
        exports where a real symbol's own strokes can end up split
        across many small, ungrouped pieces that auto-detection alone
        has nothing complete to offer for.

        2026-08-15 follow-up (see NOTES.md "Bildbaserad 'hitta liknande
        symbol' — vid sidan av vektorlogiken"): a click with NO nearby
        vector data at all (a scanned page, or just an empty spot) used
        to be a hard dead end here. It now falls through to opening
        SimilarSymbolSearchDialog in forced image-matching mode instead,
        using a scale-sized square around the click point as the
        reference region — exactly the gap
        equipment_detection.find_similar_shapes()'s own docstring
        already flagged as "a separate, not-yet-built undertaking"."""
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Ingen P&ID", "Öppna en P&ID-fil först.")
            return
        pdf_x, pdf_y = self.viewer.scene_to_pdf(pos)
        resolved = equipment_detection.resolve_reference_cluster(
            self.viewer.pdf_doc, page, pdf_x, pdf_y)
        ref_scale = symbol_geometry.dominant_text_size(self.viewer.pdf_doc[page])
        if resolved is None:
            half = max(ref_scale * 2.0, 20.0)
            fallback_bbox = (pdf_x - half, pdf_y - half, pdf_x + half, pdf_y + half)
            params_dlg = SimilarSymbolSearchDialog(
                None, None, self.viewer._pdf_path, page, ref_scale,
                ref_bbox=fallback_bbox, db=self.db, viewer=self.viewer,
                page_rotations=self.db.get_all_page_rotations(), parent=self)
        else:
            primitives, native_index_group, ref_cluster = resolved
            radius = max(ref_scale * 1.0, 12.0)
            nearby = symbol_geometry.primitives_near_point(
                primitives, pdf_x, pdf_y, radius, scale=ref_scale)
            # Connectivity-grown, not just "nearby" (2026-08-15, see
            # NOTES.md "det jag definerar som ett objekt"): only ACCEPT a
            # nearby primitive once something already accepted touches
            # it (symbol_geometry.widen_by_connectivity), so a tag label
            # or other unrelated content that merely happens to sit
            # within the search radius — but isn't actually connected to
            # the reference — stays out entirely, instead of starting as
            # a "click to add" option the user has to notice and reject.
            wide_index_group = sorted(symbol_geometry.widen_by_connectivity(
                primitives, native_index_group, nearby))
            initial_excluded = set(wide_index_group) - set(native_index_group)

            # Bildmatchning's reference crop is exactly this bbox (see
            # SimilarSymbolSearchDialog._render_image_preview) — using
            # ref_cluster['bbox'] (the auto-detected core ALONE) here cuts
            # the raster reference down to whatever tiny fragment
            # resolve_reference_cluster happened to seed the core from on
            # a densely-fragmented file, so only a sliver of the actual
            # symbol was ever shown or searched with (2026-08-16, see
            # NOTES.md "Bildmatchning klipper fel — visar bara en del av
            # det markerade fältet": confirmed on the active project's own
            # hazop_project_pid.pdf — an instrument bubble's resolved core
            # was a single 6x6pt curve fragment, one corner of the circle,
            # while the wider connectivity-grown group's own bbox tightly
            # covers the whole circle+label). Union over wide_index_group
            # instead — the same set already shown/editable in Vektorform —
            # so switching to Bildmatchning never loses what Vektorform
            # already had.
            # An empty wide_index_group (e.g. resolve_reference_cluster
            # itself returned an empty native group with nothing nearby to
            # widen with — a genuinely empty/degenerate reference) has
            # nothing to union over; fall back to the auto-detected
            # cluster's own bbox rather than crashing on min()/max() of an
            # empty sequence.
            image_ref_bbox = ref_cluster['bbox'] if not wide_index_group else (
                min(primitives[i]['bbox'][0] for i in wide_index_group),
                min(primitives[i]['bbox'][1] for i in wide_index_group),
                max(primitives[i]['bbox'][2] for i in wide_index_group),
                max(primitives[i]['bbox'][3] for i in wide_index_group),
            )

            params_dlg = SimilarSymbolSearchDialog(
                primitives, wide_index_group, self.viewer._pdf_path, page, ref_scale,
                ref_bbox=image_ref_bbox, db=self.db, viewer=self.viewer,
                initial_excluded=initial_excluded, native_index_group=native_index_group,
                page_rotations=self.db.get_all_page_rotations(), parent=self)
        if params_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        results = params_dlg.final_results(comp_type=params_dlg.selected_comp_type())
        if not results:
            QMessageBox.information(
                self, "Inget liknande hittat",
                "Inga tillräckligt lika symboler hittades med de valda "
                "sökinställningarna.")
            return
        review_dlg = EquipmentMarkerReviewDialog(
            results, self.db, parent=self, rejected=[], pdf_path=self.viewer._pdf_path)
        if review_dlg.exec():
            self.reload_overlays()

    def _find_similar_symbol_from_template(self):
        """"🔎 Hitta liknande symbol (från mall)" (2026-08-15, see
        NOTES.md "Hitta liknande symbol" — uppföljningsfunktioner:
        symbolbibliotek) — search using a previously saved
        Database.symbol_templates() row's features instead of clicking
        a reference point on this specific document. Otherwise
        identical to _find_similar_symbol from here on: same
        SimilarSymbolSearchDialog (in "mall-läge" — no
        _ClusterPreviewCanvas, no rotation toggle) and the same
        EquipmentMarkerReviewDialog confirm/save step."""
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            QMessageBox.warning(self, "Ingen P&ID", "Öppna en P&ID-fil först.")
            return
        if not self.db.symbol_templates():
            QMessageBox.information(
                self, "Inga sparade mallar",
                'Inga symbolmallar sparade än. Spara en via "💾 Spara som mall…" '
                'i sökdialogen för "Hitta liknande symbol".')
            return
        picker = SymbolTemplatePickerDialog(self.db, parent=self)
        if picker.exec() != QDialog.DialogCode.Accepted or not picker.selected_template:
            return
        template = picker.selected_template
        ref_features = json.loads(template['features_json'])
        page = self.viewer.current_page

        params_dlg = SimilarSymbolSearchDialog(
            None, None, self.viewer._pdf_path, page, None,
            db=self.db, viewer=self.viewer,
            template_name=template['name'], template_features=ref_features,
            initial_comp_type=template.get('comp_type', ''),
            page_rotations=self.db.get_all_page_rotations(), parent=self)
        if params_dlg.exec() != QDialog.DialogCode.Accepted:
            return

        results = params_dlg.final_results(comp_type=params_dlg.selected_comp_type())
        if not results:
            QMessageBox.information(
                self, "Inget liknande hittat",
                "Inga tillräckligt lika symboler hittades med de valda "
                "sökinställningarna.")
            return
        review_dlg = EquipmentMarkerReviewDialog(
            results, self.db, parent=self, rejected=[], pdf_path=self.viewer._pdf_path)
        if review_dlg.exec():
            self.reload_overlays()

    def _draw_tag_highlights(self):
        """Highlight complete tag numbers found on the current PDF page.

        Yellow  = tag recognised but not yet a HAZOP cause.
        Green   = tag has at least one defined HAZOP cause.
        Only runs when smart database is enabled OR a tag database is loaded.
        """
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            return
        if not hasattr(self.db, 'tag_db_setting'):
            return

        smart_on  = self.db.tag_db_setting('smart_enabled', '0') == '1'
        tag_codes = set(self.db.all_active_tag_codes()) \
                    if hasattr(self.db, 'all_active_tag_codes') else set()

        # Nothing to do if both sources are inactive
        if not smart_on and not tag_codes:
            return

        try:
            self.viewer.clear_highlights()
            page_num  = self.viewer.current_page
            fitz_page = self.viewer.pdf_doc.load_page(page_num)

            # Tags already used as HAZOP causes (→ green)
            used_tags: set = set()
            try:
                for m in self.db.cause_markers_for_page(page_num):
                    t = (m['component_tag'] or '').upper().strip()
                    if t:
                        used_tags.add(t)
                for node in self.db.nodes():
                    for cause in self.db.causes(node['id']):
                        t = _pick_best_tag(cause['description'])
                        if t:
                            used_tags.add(t.upper())
            except Exception:
                pass

            # Scan page text for complete tag numbers using spatial combining
            raw_words = _rotate_words(fitz_page.get_text("words"), fitz_page)
            seen: set = set()

            for candidate, *_box in _spatial_combine(raw_words, gap_limit=22.0):
                tag = _pick_best_tag(candidate)
                if not tag or tag in seen:
                    continue
                pfx = _equip_prefix_from_tag(tag)
                # Only highlight if prefix is known (in DB or confirmed mapping)
                known = (smart_on or pfx in tag_codes or
                         (hasattr(self.db, 'confirmed_comp_for_tag') and
                          self.db.confirmed_comp_for_tag(pfx)))
                if not known:
                    continue
                seen.add(tag)

                # Find exact bounding box on the page. search_for(), like
                # get_text(), returns raw unrotated-mediabox rects — rotate
                # into this app's PDF space (see _rotate_words) before
                # handing bbox to add_tag_highlight, which draws it in
                # scene coordinates via pdf_to_scene().
                try:
                    mat = fitz_page.rotation_matrix
                    hits = fitz_page.search_for(tag)
                    if not hits:
                        # Try just the code part (e.g., PSV-101 from 20-PSV-101)
                        simple = f"{pfx}-" + tag.split('-')[-1] if '-' in tag else tag
                        hits = fitz_page.search_for(simple)
                    hits = [h * mat for h in hits]
                    for bbox in hits:
                        is_used = tag in used_tags or simple in used_tags \
                                  if 'simple' in dir() else tag in used_tags
                        color = '#90EE90' if is_used else '#FFFFE0'
                        label = f"{'✓ HAZOP-orsak' if is_used else '○ Tagg'}: {tag}"
                        self.viewer.add_tag_highlight(bbox, color, label)
                except Exception:
                    continue

        except Exception:
            pass  # Never crash during highlight drawing

    def _load_overlays(self):
        self.viewer.clear_overlays()
        self.viewer.clear_markup_overlays()
        self.viewer.clear_red_markup_overlays()
        if self.viewer.pdf_doc is None:
            return
        orig_page   = self.viewer.current_page
        active_pages = sorted(self.viewer._all_page_items.keys())
        all_nodes   = list(self.db.nodes())

        for page in active_pages:
            self.viewer.current_page = page  # ensures pdf_to_scene uses this page's offset

            for node in all_nodes:
                nd      = dict(node)
                raw_pts = nd.get('markup_points', '') or ''
                nd_page = int(nd.get('pid_page', 0) or 0)
                if not raw_pts or nd_page != page:
                    continue
                try:
                    points = [(float(p[0]), float(p[1])) for p in json.loads(raw_pts)]
                    style  = json.loads(nd.get('markup_style', '') or '{}')
                except Exception:
                    continue
                if points:
                    self.viewer.add_node_overlay(nd['id'], points, style, nd.get('name', ''))

            if hasattr(self.db, 'node_markups_for_page'):
                for mu in self.db.node_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except ValueError as e: logging.warning(f"Failed to parse markup points JSON: {e}"); pts = []
                    self.viewer.add_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#1565C0'),
                        float(m.get('opacity', 0.45)), int(m.get('line_width', 2)),
                        bool(m.get('visible', 1)))

            if hasattr(self.db, 'node_red_markups_for_page'):
                for mu in self.db.node_red_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except ValueError as e: logging.warning(f"Failed to parse markup points JSON: {e}"); pts = []
                    self.viewer.add_red_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#CC0000'),
                        float(m.get('opacity', 1.0)), int(m.get('line_width', 4)),
                        bool(m.get('visible', 1)), int(m.get('font_size', 12)),
                        float(m.get('symbol_w', 40)), float(m.get('symbol_h', 40)),
                        float(m.get('symbol_rot', 0)))

            for m in self.db.equipment_markers_for_page(page):
                md = dict(m)
                dev_count = (self.db.equipment_deviation_count(md['equipment_id'])
                             if md.get('equipment_id') else 0)
                tag_val = md.get('tag', '')
                comp_type_val = md.get('comp_type', '')
                cons_count = (self.db.equipment_consequence_count(tag_val, comp_type_val)
                              if tag_val else 0)
                sg_count = (self.db.equipment_safeguard_count(tag_val, comp_type_val)
                            if tag_val else 0)
                self.viewer.add_equipment_marker(
                    md['id'], md['x'], md['y'], comp_type_val,
                    tag_val, md.get('confidence', 0.0) or 0.0,
                    outline_pdf=md.get('shape_outline'), deviation_count=dev_count,
                    consequence_count=cons_count, safeguard_count=sg_count)

        # Feature 8: load sticky note annotations
        if hasattr(self.db, 'get_board_annotations'):
            for ann in self.db.get_board_annotations():
                self._draw_annotation(ann['id'], ann['x'], ann['y'],
                                      ann['w'], ann['h'], ann['text'], ann['color'])

        self.viewer.current_page = orig_page
        self._draw_tag_highlights()
        self._draw_sheet_connections()
        # Reapply LOD so newly added items get correct visibility at current zoom
        self.viewer._apply_lod(self.viewer.transform().m11(), force=True)
        self.viewer._reapply_equipment_selection_overlays()

    def reload_overlays(self):
        """Public helper to refresh all P&ID markers and connection lines."""
        self._load_overlays()

    # ── Node markup editing API ───────────────────────────────────────────────

    def enter_markup_edit(self, node_id):
        """Enter markup editing mode for a node: show existing markup + enable tools."""
        self._active_markup_class = 'node'
        self.set_active_node(node_id)
        self._set_mode(MODE_MARKUP_SELECT)
        self.viewer.markup_draw_finished.connect(self._on_viewer_markup_drawn)
        self.viewer.markup_item_clicked.connect(self._on_viewer_markup_clicked)

    def exit_markup_mode(self):
        """Return to normal navigation mode."""
        try: self.viewer.markup_draw_finished.disconnect(self._on_viewer_markup_drawn)
        except RuntimeError as e: logging.warning(f"Markup draw finished signal not connected: {e}")
        try: self.viewer.markup_item_clicked.disconnect(self._on_viewer_markup_clicked)
        except RuntimeError as e: logging.warning(f"Markup item clicked signal not connected: {e}")
        self._active_markup_class = 'node'
        self._active_symbol_id = None
        self._set_mode(MODE_NAV)

    def set_markup_tool(self, tool, color=None, opacity=None, width=None):
        """Set drawing tool: 'polygon'|'polyline'|'text'|'comment'|'select'|'smart'."""
        _map = {'polygon':  MODE_MARKUP_POLYGON,
                'polyline': MODE_MARKUP_POLYLINE,
                'text':     MODE_MARKUP_TEXT,
                'comment':  MODE_MARKUP_COMMENT,
                'select':   MODE_MARKUP_SELECT,
                'smart':    MODE_SMART_POLYLINE}
        if tool in _map:
            self._set_mode(_map[tool])
        if color is not None:
            self.viewer.set_pen_style(color, width or 3, int((opacity or 0.45) * 210))

    def refresh_markup_overlays(self):
        """Reload only the markup overlays (cheap — no cause/cons/sg reload)."""
        self.viewer.clear_markup_overlays()
        if self.viewer.pdf_doc is None:
            return
        orig_page = self.viewer.current_page
        for page in sorted(self.viewer._all_page_items.keys()):
            self.viewer.current_page = page
            if hasattr(self.db, 'node_markups_for_page'):
                for mu in self.db.node_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except ValueError as e: logging.warning(f"Failed to parse markup points JSON: {e}"); pts = []
                    self.viewer.add_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#1565C0'),
                        float(m.get('opacity', 0.45)), int(m.get('line_width', 12)),
                        bool(m.get('visible', 1)),
                        int(m.get('font_size', 12)))
        self.viewer.current_page = orig_page

    # ── Red markup editing API ────────────────────────────────────────────────

    def enter_red_markup_edit(self, node_id):
        """Enter red markup editing mode for a node."""
        self._active_markup_class = 'red'
        self._active_symbol_id = None
        self.set_active_node(node_id)
        self._set_mode(MODE_MARKUP_SELECT)
        self.viewer.markup_draw_finished.connect(self._on_viewer_markup_drawn)
        self.viewer.markup_item_clicked.connect(self._on_viewer_markup_clicked)

    def exit_red_markup_mode(self):
        """Return to normal navigation mode from red markup."""
        try: self.viewer.markup_draw_finished.disconnect(self._on_viewer_markup_drawn)
        except RuntimeError as e: logging.warning(f"Red markup draw finished signal not connected: {e}")
        try: self.viewer.markup_item_clicked.disconnect(self._on_viewer_markup_clicked)
        except RuntimeError as e: logging.warning(f"Red markup item clicked signal not connected: {e}")
        self._active_markup_class = 'node'
        self._active_symbol_id = None
        self._set_mode(MODE_NAV)

    def set_red_markup_tool(self, tool, color=None, opacity=None, width=None, symbol_id=None):
        """Set red markup tool: 'polygon'|'polyline'|'comment'|'select'|'smart'|'symbol'."""
        _map = {'polygon':  MODE_MARKUP_POLYGON,
                'polyline': MODE_MARKUP_POLYLINE,
                'comment':  MODE_MARKUP_COMMENT,
                'select':   MODE_MARKUP_SELECT,
                'smart':    MODE_SMART_POLYLINE,
                'symbol':   MODE_RED_MARKUP_SYMBOL}
        if tool in _map:
            self._set_mode(_map[tool])
        if tool == 'symbol' and symbol_id is not None:
            self._active_symbol_id = symbol_id
        elif tool != 'symbol':
            self._active_symbol_id = None
        if color is not None:
            self.viewer.set_pen_style(color, width or 4, int((opacity or 1.0) * 210))

    def refresh_red_markup_overlays(self):
        """Reload only the red markup overlays."""
        self.viewer.clear_red_markup_overlays()
        if self.viewer.pdf_doc is None:
            return
        orig_page = self.viewer.current_page
        for page in sorted(self.viewer._all_page_items.keys()):
            self.viewer.current_page = page
            if hasattr(self.db, 'node_red_markups_for_page'):
                for mu in self.db.node_red_markups_for_page(page):
                    m = dict(mu)
                    try: pts = json.loads(m.get('points', '[]') or '[]')
                    except ValueError as e: logging.warning(f"Failed to parse markup points JSON: {e}"); pts = []
                    self.viewer.add_red_markup_overlay(
                        m['id'], m.get('type', 'polygon'), pts,
                        m.get('label', ''), m.get('color', '#CC0000'),
                        float(m.get('opacity', 1.0)), int(m.get('line_width', 4)),
                        bool(m.get('visible', 1)), int(m.get('font_size', 12)),
                        float(m.get('symbol_w', 40)), float(m.get('symbol_h', 40)),
                        float(m.get('symbol_rot', 0)))
        self.viewer.current_page = orig_page

    def _on_viewer_markup_drawn(self, type_, pts, page):
        """Called when user finishes drawing in the viewer; route to appropriate panel."""
        node_id = self._active_node_id
        if node_id is None:
            return
        if self._active_markup_class == 'red':
            # Red markup mode
            if type_ == 'comment':
                label, ok = QInputDialog.getText(self, 'Kommentar', 'Kommentar:')
                if not ok or not label.strip():
                    self.viewer.clear_red_markup_overlays()
                    self.refresh_red_markup_overlays()
                    return
            elif type_ == 'symbol':
                label = self._active_symbol_id or ''
                self._set_mode(MODE_MARKUP_SELECT)
            else:
                label = ''
            self.red_markup_draw_finished.emit(type_, node_id, pts, page, label)
        else:
            # Node markup mode
            if type_ == 'text':
                node = self.db.get_node(node_id) if hasattr(self.db, 'get_node') else None
                label = node['name'] if node else ''
            elif type_ == 'comment':
                label, ok = QInputDialog.getText(self, 'Kommentar', 'Kommentar:')
                if not ok or not label.strip():
                    self.viewer.clear_markup_overlays()
                    self.refresh_markup_overlays()
                    return
            else:
                label = ''
            self.markup_draw_finished.emit(type_, node_id, pts, page, label)

    def _on_viewer_markup_clicked(self, mu_id):
        if self._active_markup_class == 'red':
            self.red_markup_item_selected.emit(mu_id)
        else:
            self.markup_item_selected.emit(mu_id)
            self.viewer.highlight_markup(mu_id)

    def place_cause_from_template(self, dev_id, comp_type, comp_tag, description, frequency):
        """Called by EquipmentDeviationBar._create_cause_for_bar — the only
        remaining caller since the classic P&ID-click cause flow was
        removed (2026-08-13, see NOTES.md: the P&ID canvas is now
        object-placement-only). No cause marker is drawn on the P&ID — the
        equipment marker's own colour/badge already represents "this
        equipment has causes"."""
        label = description or comp_tag or 'Ny orsak'

        try:
            cause_id = self.db.add_cause(dev_id)
        except Exception as e:
            QMessageBox.critical(self, "Databasfel", f"Kunde inte skapa orsak:\n{e}")
            return None
        self.db.update_cause(cause_id, label, comp_type=comp_type, comp_tag=comp_tag)
        if frequency is not None:
            f_level = self._compute_f_level(frequency)
            self.db.update_cause(cause_id, likelihood=f_level, base_freq=frequency)

        # Auto-create an empty consequence + safeguard (2026-08-09, see
        # NOTES.md) so the HAZOP scenario row is immediately ready for
        # direct inline editing on KON and SG — no separate add-
        # consequence/add-safeguard step.
        cons_id = self.db.add_consequence(cause_id)
        self.db.add_safeguard(cons_id)

        self._load_overlays()
        self.cause_template_created.emit(cause_id)
        return cause_id

    # ── Equipment marker click → EquipmentDeviationBar (2026-08-07) ────────
    # See NOTES.md "Nod → Utrustning → Avvikelse". Clicking an equipment
    # marker used to always navigate away to the Utrustningsregister
    # (marker_navigated.emit('equipment', ...)); it now opens the bottom
    # bar instead. (2026-08-12: also re-emits marker_navigated again — the
    # bar and the filtered-scenario-table navigation are no longer
    # mutually exclusive, see NOTES.md "de orsaker som visas i hazop
    # scenario är de där objektet finns med".)

    def _on_marker_clicked(self, item_type, item_id):
        if item_type == 'equipment' and self._active_edit_query_fn is not None:
            # Shift+click a marker while an ORS/KON/SG cell is being
            # edited inserts its tag right into the open text instead
            # of the normal navigate-to-equipment flow below, which
            # would tear the editor down via a full scenario-table
            # rebuild (2026-08-13, see NOTES.md: "jag hoppar inte ut ur
            # textediteringsvyn"). Checked via QApplication.keyboardModifiers()
            # rather than threading a modifier through marker_clicked's
            # signature — zero risk to that signal's other callers.
            shift_held = bool(QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
            if shift_held:
                target = self._active_edit_query_fn()
                if target is not None:
                    editor, kind, target_id = target
                    row = self.db.conn.execute(
                        "SELECT equipment_id FROM equipment_markers WHERE id=?",
                        (item_id,)).fetchone()
                    eq = self.db.get_equipment_by_id(row['equipment_id']) \
                        if row and row['equipment_id'] is not None else None
                    tag = (eq.get('tag') or '').strip() if eq else ''
                    if tag:
                        self._insert_tag_into_editor(editor, tag)
                        self._sync_tag_ref(kind, target_id, tag,
                                           (eq.get('equipment_type') or '') if eq else '')
                    return   # swallow: no popup, no marker_navigated, no rebuild
        if item_type == 'equipment':
            row = self.db.conn.execute(
                "SELECT equipment_id, x, y, pid_page FROM equipment_markers WHERE id=?",
                (item_id,)).fetchone()
            if row and row['equipment_id'] is not None:
                self._equipment_bar.load(row['equipment_id'], item_id,
                                         active_node_id=self._active_node_id)
                scene_pos = self.viewer.pdf_to_scene(row['x'], row['y'], page=row['pid_page'])
                gp = self.viewer.viewport().mapToGlobal(self.viewer.mapFromScene(scene_pos))
                self._equipment_bar.show_near(gp)
            self.marker_navigated.emit(item_type, item_id)
            return
        self.marker_navigated.emit(item_type, item_id)

    def _insert_tag_into_editor(self, editor, tag):
        """Insert `tag` at the cursor of a live ORS/KON/SG QLineEdit
        editor (Shift+click a P&ID marker while editing, 2026-08-13,
        see NOTES.md) — mutates only the open editor's text, exactly as
        if the user had typed it, so the existing commit-on-
        editingFinished path persists it normally and the cursor lands
        ready to keep typing right after. No DB write, no rebuild."""
        pos = editor.cursorPosition()
        text = editor.text()
        before, after = text[:pos], text[pos:]
        if before and not before.endswith(' '):
            before += ' '
        insert = tag if after.startswith(' ') else tag + ' '
        editor.setText(before + insert + after)
        editor.setCursorPosition(len(before) + len(insert))
        editor.setFocus()

    def _sync_tag_ref(self, kind, id_, tag, comp_type):
        """Immediately records `tag` in tagged_refs/comp_tag/comp_type
        (2026-08-13, see NOTES.md: "att den blir fetstil") so the
        description text _insert_tag_into_editor just inserted gets the
        same bold-tag-highlight treatment the drag-and-drop path
        already gives KON/SG cells (_PidDelegate's paint, via
        find_tag_bold_ranges/parse_tag_refs, hazop.py). Deliberately
        does NOT touch the description column here — the live editor's
        full text (already updated) is what the normal edit-commit path
        saves when editing finishes; writing the STALE pre-edit
        description here too would just get overwritten a moment later
        anyway. 'cause' (ORS) has no tagged_refs column at all — its tag
        still lands as plain, un-bolded text via _insert_tag_into_editor.

        Small local reimplementation of hazop.py's add_tag_ref() — can't
        import it directly (hazop.py imports FROM pid_viewer.py, never
        the reverse)."""
        def _add_ref(raw, t):
            refs = [r for r in (s.strip() for s in (raw or '').split(',')) if r and r != t]
            refs.append(t)
            return ','.join(refs)

        if kind == 'consequence':
            row = self.db.get_consequence(id_)
            if not row:
                return
            new_refs = _add_ref(row.get('tagged_refs'), tag)
            self.db.update_consequence(id_, row['description'], row['severity'],
                                        row['category'] or '', row.get('consequence_chain') or '',
                                        comp_tag=tag, comp_type=comp_type, tagged_refs=new_refs)
        elif kind == 'safeguard':
            row = self.db.get_safeguard(id_)
            if not row:
                return
            new_refs = _add_ref(row.get('tagged_refs'), tag)
            self.db.update_safeguard(id_, tagged_refs=new_refs)
            self.db.set_safeguard_tag(id_, tag, comp_type)

    def place_equipment_marker(self, tag, comp_type, scene_pos, page, pdf_rect=None):
        """Callback for EquipmentTagPopup (P&ID right-click -> "🔧 Objekt",
        2026-08-07, see NOTES.md). Resolves an existing equipment_catalog
        row by tag if one exists (never creates a duplicate for a tag
        that's already catalogued) or creates a new one, places a marker
        at the clicked point, and opens EquipmentDeviationBar immediately —
        the same bar _on_marker_clicked already opens for an existing
        marker, so the very next step (tick a deviation) continues the
        same established flow without an extra click.

        `pdf_rect` (2026-08-09, see NOTES.md) — optional QRectF in PDF
        units from the right-drag rubber-band menu's "🔧 Objekt" entry.
        When given, its four corners become the marker's shape_outline so
        it renders with a real outline (like a scanned/auto-detected
        symbol) instead of the generic bowtie-icon fallback a bare point
        gets."""
        tag = (tag or '').strip().upper()
        existing = self.db.get_equipment_by_tag(tag) if tag else None
        if existing:
            equipment_id = existing['id']
        else:
            prefix = _equip_prefix_from_tag(tag) if tag else ''
            equipment_id = self.db.add_equipment_item(tag, tag, prefix, page, comp_type, '', 0)

        pdf_x, pdf_y = self.viewer.scene_to_pdf(scene_pos)
        outline = None
        if pdf_rect is not None:
            outline = [[pdf_rect.left(), pdf_rect.top()], [pdf_rect.right(), pdf_rect.top()],
                       [pdf_rect.right(), pdf_rect.bottom()], [pdf_rect.left(), pdf_rect.bottom()]]
        outline_json = json.dumps(outline) if outline else ''
        marker_id = self.db.add_equipment_marker(
            equipment_id, tag, page, pdf_x, pdf_y, comp_type, shape_outline=outline_json,
            confidence=1.0, link_method='manual')
        self.viewer.add_equipment_marker(marker_id, pdf_x, pdf_y, comp_type, tag=tag,
                                         outline_pdf=outline)
        self._equipment_bar.load(equipment_id, marker_id, active_node_id=self._active_node_id)
        gp = self.viewer.viewport().mapToGlobal(self.viewer.mapFromScene(scene_pos))
        self._equipment_bar.show_near(gp)

    def _on_equipment_deviation_added(self, deviation_id, equipment_id):
        self._refresh_equipment_marker_visual(equipment_id)
        self.equipment_deviation_created.emit(deviation_id, equipment_id)

    def _on_equipment_deviation_removed(self, deviation_id, equipment_id):
        # Same refresh needs as _on_equipment_deviation_added (marker badge
        # count, tree, worksheet) — see NOTES.md "av-/aktivera".
        self._refresh_equipment_marker_visual(equipment_id)
        self.equipment_deviation_created.emit(deviation_id, equipment_id)

    def _create_cause_for_bar(self, deviation_id, comp_type, comp_tag, description, frequency=None):
        """Callback wired into EquipmentDeviationBar._create_cause_fn — same
        creation path as the normal cause-template flow, but returns
        cause_id synchronously so the bar can enable/set its frequency combo.
        `frequency` (events/year, from standard_causes.frequency when known)
        is passed straight through to place_cause_from_template's existing
        _compute_f_level() conversion — see NOTES.md."""
        marker = self.db.conn.execute(
            "SELECT 1 FROM equipment_markers WHERE id=?",
            (self._equipment_bar.marker_id,)).fetchone()
        if not marker:
            return None
        return self.place_cause_from_template(
            deviation_id, comp_type, comp_tag, description, frequency)


    def _refresh_equipment_marker_visual(self, _equipment_id):
        """Redraw overlays so this equipment's marker picks up its new
        colour/deviation-count badge (or new comp_type/shape after a
        reclassification) — _load_overlays() re-reads every marker's
        current deviation count from the DB, see add_equipment_marker."""
        self._load_overlays()

    def clear_active_selection(self):
        """Reset every id used when placing new cause/consequence/safeguard
        markers on the P&ID, so a deleted-elsewhere cause/consequence can
        never survive as a stale id into a later placement click (root
        cause of the add_consequence FOREIGN KEY crash, 2026-08-07 — see
        NOTES.md). Called on every tree structural change, mirroring the
        equally aggressive reset _on_structure_changed already does for
        the scenario/tree selection."""
        self._active_node_id      = None
        self._active_deviation_id = None
        self._active_cause_id     = None
        self._active_consequence_id = None

    def set_active_node(self, node_id):
        self._active_node_id        = node_id
        self._active_cause_id       = None
        self._active_consequence_id = None

    def set_active_deviation(self, dev_id):
        self._active_deviation_id = dev_id
        dev = self.db.get_deviation(dev_id) if dev_id else None
        if dev:
            self._active_node_id = dict(dev).get('node_id')

    def set_active_cause(self, cause_id):
        self._active_cause_id       = cause_id
        self._active_consequence_id = None
        row = self.db.get_cause(cause_id)
        if row:
            d = dict(row)
            self._active_node_id      = d.get('node_id')
            self._active_deviation_id = d.get('deviation_id')

    def set_active_consequence(self, cons_id):
        self._active_consequence_id = cons_id
        row = self.db.get_consequence(cons_id)
        if not row:
            return
        cause_id = dict(row).get('cause_id')
        self._active_cause_id = cause_id
        if cause_id:
            cause = self.db.get_cause(cause_id)
            if cause:
                self._active_node_id = dict(cause).get('node_id')

    # Maps Excel category strings → component_type keys used in the app
    _CAT_TO_COMP = {
        'instrument':        'Instrument / Sensor',
        'givare':            'Instrument / Sensor',
        'reglerfunktion':    'Instrument / Sensor',
        'larm':              'Instrument / Sensor',
        'brytare':           'Instrument / Sensor',
        'mätvärde':          'Instrument / Sensor',
        'transmitter':       'Instrument / Sensor',
        'reglerventil':      'Ventil',
        'ventil':            'Ventil',
        'pump':              'Pump',
        'kompressor':        'Kompressor',
        'blåsmaskin':        'Kompressor',
        'tank':              'Tank / Kärl',
        'kärl':              'Tank / Kärl',
        'behållare':         'Tank / Kärl',
        'kolonn':            'Tank / Kärl',
        'värmeväxlare':      'Värmeväxlare',
        'kylare':            'Värmeväxlare',
        'kondensor':         'Värmeväxlare',
        'filter':            'Övrigt',
        'sil':               'Övrigt',
        'säkerhetsventil':   'Säkerhetsventil (PSV)',
        'avlastningsventil': 'Säkerhetsventil (PSV)',
        'rörledning':        'Rörledning',
    }

    def _comp_from_db_entry(self, entry: dict) -> str:
        """Map a tag_database entry's category to a component type string."""
        if not entry:
            return ''
        cat = str(entry.get('category', '')).lower()
        for key, comp in self._CAT_TO_COMP.items():
            if key in cat:
                return comp
        name = str(entry.get('name_sv', '') + ' ' + entry.get('name_en', '')).lower()
        for key, comp in self._CAT_TO_COMP.items():
            if key in name:
                return comp
        return ''

    def _learn_tag_type(self, tag: str, comp_type: str):
        """Implicitly learn prefix → comp_type from user's own selection.

        Stored as a confirmed entry in pid_identified_tags so it's used
        automatically next time the same prefix is encountered.
        """
        if not tag or not comp_type:
            return
        pfx = _equip_prefix_from_tag(tag)
        if not pfx or len(pfx) < 2:
            return
        try:
            if hasattr(self.db, 'upsert_pid_tag') and hasattr(self.db, 'confirm_pid_tag'):
                self.db.upsert_pid_tag(pfx, tag, '', comp_type)
                self.db.confirm_pid_tag(pfx, comp_type, True)
        except Exception:
            pass

    def _compute_zone_phash(self, page_num: int,
                            cx_pdf: float, cy_pdf: float,
                            w_pdf: float, h_pdf: float) -> str:
        """Compute a 16×16 average-hash for the PDF zone. Returns hex string or ''."""
        if not HAS_PYMUPDF or self.viewer.pdf_doc is None:
            return ''
        try:
            import fitz as _fitz
            page = self.viewer.pdf_doc.load_page(page_num)
            margin = max(4.0, min(w_pdf, h_pdf) * 0.15)
            clip = _fitz.Rect(cx_pdf - w_pdf / 2 - margin,
                              cy_pdf - h_pdf / 2 - margin,
                              cx_pdf + w_pdf / 2 + margin,
                              cy_pdf + h_pdf / 2 + margin)
            if clip.width <= 0 or clip.height <= 0:
                return ''
            SIZE = 16
            mat = _fitz.Matrix(SIZE / clip.width, SIZE / clip.height)
            pix = page.get_pixmap(matrix=mat, clip=clip,
                                   colorspace=_fitz.csGRAY, alpha=False)
            if pix.width == 0 or pix.height == 0:
                return ''
            pixels = list(pix.samples)
            if not pixels:
                return ''
            mean = sum(pixels) / len(pixels)
            bits = [1 if p >= mean else 0 for p in pixels]
            val = 0
            for b in bits:
                val = (val << 1) | b
            n_hex = (len(bits) + 3) // 4
            return hex(val)[2:].zfill(n_hex)
        except Exception:
            return ''

    def _db_comp_for_tag(self, tag: str) -> str:
        """Return the component type the user has taught for this tag's prefix.

        ONLY uses study_tag_memory — the single source of truth for smart
        recognition.  Populated exclusively by the user's rubber-band markup
        confirmations.  Numbers are ignored (321HV3333 → prefix HV).
        Returns '' if not yet taught or smart recognition is disabled.
        """
        if not tag:
            return ''
        if hasattr(self.db, 'get_config'):
            if self.db.get_config('smart_recognition_enabled', '1') != '1':
                return ''
        pfx = _equip_prefix_from_tag(tag)
        if not pfx:
            return ''
        try:
            return self.db.get_prefix_memory(pfx) if hasattr(self.db, 'get_prefix_memory') else ''
        except Exception:
            return ''

    def _load_mode_freqs(self):
        """Return {comp_type: {mode_desc: freq_per_year}} from DB."""
        if not hasattr(self.db, 'component_types'):
            return {}
        result = {}
        for ct in self.db.component_types():
            freqs = {}
            for fm in self.db.failure_modes(ct['id']):
                if fm['freq_per_year'] is not None:
                    freqs[fm['description']] = fm['freq_per_year']
            result[ct['name']] = freqs
        return result

    def _compute_f_level(self, freq_per_year):
        """Convert frequency (events/year) to F-level using matrix boundaries."""
        if not freq_per_year or freq_per_year <= 0:
            return 3   # default
        cfg        = self.db.get_risk_matrix() if hasattr(self.db, 'get_risk_matrix') else {}
        boundaries = sorted(
            float(b) for b in (cfg or {}).get('freq_boundaries',
                                              [1e-5, 1e-4, 1e-3, 0.01, 0.1, 1.0]))
        for i, b in enumerate(boundaries):
            if float(freq_per_year) < b:
                return i - 1
        return len(boundaries) - 1

    def try_reload_pdf(self, override_path=None):
        path = override_path or self.db.get_pid_path()
        if path and Path(path).exists() and HAS_PYMUPDF:
            layout_offsets = None
            if hasattr(self.db, 'get_pid_config_value'):
                raw = self.db.get_pid_config_value('board_layout')
                if raw:
                    try:
                        data = json.loads(raw)
                        layout_offsets = {int(k): v for k, v in data.items()}
                    except Exception:
                        layout_offsets = None
            # Only render sheets that exist in pid_sheets; fall back to all pages
            sheets = self.db.get_sheets()
            active_pages = ([int(s['physical_page']) for s in sheets]
                            if sheets else None)
            if self.viewer.load_pdf(path, page=0, layout_offsets=layout_offsets,
                                    active_pages=active_pages,
                                    page_rotations=self.db.get_all_page_rotations()):
                self.db.ensure_sheets_initialized(self.viewer.page_count())
                self._rebuild_sheet_map()
                self._current_display_page = 0
                self._update_page_label()
                self._load_overlays()
                self.analyze_btn.setEnabled(True)
        else:
            # No P&ID in database — clear the canvas completely
            if self.viewer.pdf_doc is not None:
                try:
                    self.viewer.pdf_doc.close()
                except Exception:
                    pass
                self.viewer.pdf_doc = None
            for item in list(self.viewer._all_page_items.values()):
                try:
                    self.viewer._scene.removeItem(item)
                except Exception:
                    pass
            self.viewer._all_page_items.clear()
            self.viewer._page_offsets.clear()
            self.viewer._page_cache.clear()
            self.viewer._cache_order.clear()
            self.viewer.page_item = None
            self._load_overlays()   # clears all overlay items (pdf_doc is None → returns early)
            self._rebuild_sheet_map()
            self._current_display_page = 0
            self._update_page_label()
            self.viewer._show_placeholder(
                "Ingen P&ID inläst.\nImportera en PDF-fil med knappen ovan.")
            self.analyze_btn.setEnabled(False)
        self.export_btn.setEnabled(True)
